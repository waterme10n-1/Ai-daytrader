"""Stresstest: et tilfældigt marked med pumps, dumps, rugs, nye tokens, nedbrud og knaptryk.

Bruges af test_meme_soak.py og kan også køres direkte med mange seeds:
    python tests/meme_soak.py 50
"""

from __future__ import annotations

import json
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import meme_fakes  # noqa: E402
from meme_fakes import FakeDex, FakeJup, FakeRug, raw_pair, safe_report  # noqa: E402
from trader.meme.clients import SourceError, parse_token_info  # noqa: E402
from trader.meme.engine import MemeClients, MemeEngine  # noqa: E402
from trader.meme.store import MemeStore  # noqa: E402

STEP_MINUTES = 5


class SoakJup(FakeJup):
    def _price(self, mint: str) -> float:
        pair = self.dex.pairs.get(mint)
        if pair is None:
            raise SourceError("Jupiter fandt ingen rute (pool væk)")
        return float(pair["priceUsd"])


class SoakDex(FakeDex):
    def __init__(self) -> None:
        super().__init__()
        self.profiles: list[str] = []

    def latest_profiles(self):
        if self.fail:
            raise SourceError("DexScreener nede")
        return [{"chainId": "solana", "tokenAddress": mint} for mint in self.profiles]

    def latest_boosts(self):
        if self.fail:
            raise SourceError("DexScreener nede")
        return super().latest_boosts()


class SoakRPC:
    def __init__(self) -> None:
        self.holdings: dict[tuple[str, str], int] = {}

    def token_accounts(self, owner, mint):
        return [{"pubkey": "acc", "amount": self.holdings.get((owner, mint), 1_000_000), "decimals": 6, "program": ""}]

    def block_height(self):
        return 1

    def balance_lamports(self, pubkey):
        return 0

    def signatures_for(self, address, limit=15):
        return []

    def close(self):
        pass


class Market:
    def __init__(self, rng: random.Random, dex: SoakDex, rug: FakeRug, jup: SoakJup, rpc: SoakRPC) -> None:
        self.rng, self.dex, self.rug, self.jup, self.rpc = rng, dex, rug, jup, rpc
        self.tokens: dict[str, dict] = {}
        self.counter = 0

    def _mint(self) -> str:
        from solders.pubkey import Pubkey

        self.counter += 1
        return str(Pubkey(bytes(self.rng.getrandbits(8) for _ in range(32))))

    def launch(self, sniper: bool) -> str:
        rng = self.rng
        mint = self._mint()
        age = rng.uniform(0.5, 25) if sniper else rng.uniform(40, 3000)
        kind = rng.choices(["pump", "dump", "rug", "flat"], [0.3, 0.3, 0.2, 0.2])[0]
        self.tokens[mint] = {"kind": kind, "age": age, "price": 0.001, "peak_at": rng.randint(2, 12), "t": 0}
        self.dex.pairs[mint] = raw_pair(
            mint,
            symbol=f"T{self.counter}",
            age_min=age,
            liq=rng.choice([5_000, 15_000, 40_000, 120_000, 400_000]),
            vol_h1=rng.choice([1_000, 20_000, 90_000, 300_000]),
            change_h1=rng.choice([-20, 10, 35, 120, 400]),
            buys=rng.randint(10, 800),
            sells=rng.randint(10, 800),
        )
        if rng.random() < 0.2:
            self.dex.pairs[mint]["info"] = {}
        report = safe_report(creator=f"Dev{self.counter}")
        roll = rng.random()
        if roll < 0.08:
            report["mintAuthority"] = "DevMint"
        elif roll < 0.12:
            report["freezeAuthority"] = "DevFreeze"
        elif roll < 0.16:
            report["risks"] = [{"name": "Large Amount of LP Unlocked", "level": "danger"}]
        if rng.random() > 0.05:
            self.rug.reports[mint] = report
        if rng.random() < 0.04:
            self.jup.no_route.add(mint)
        if sniper:
            created = datetime.now(timezone.utc) - timedelta(minutes=age)
            token = parse_token_info(
                {
                    "id": mint,
                    "symbol": f"T{self.counter}",
                    "holderCount": rng.choice([3, 30, 200]),
                    "audit": {"mintAuthorityDisabled": rng.random() > 0.1, "freezeAuthorityDisabled": True, "topHoldersPercentage": rng.choice([10, 25, 45]), "devBalancePercentage": rng.choice([0, 2, 12])},
                    "twitter": "https://x.com/t" if rng.random() > 0.2 else "",
                    "firstPool": {"createdAt": created.isoformat()},
                }
            )
            self.jup.recent.append(token)
            self.jup.recent = self.jup.recent[-40:]
        else:
            (self.dex.boosts if rng.random() < 0.6 else self.dex.profiles).append(mint)
        return mint

    def step(self) -> None:
        rng = self.rng
        self.dex.fail = rng.random() < 0.05
        for mint, state in list(self.tokens.items()):
            pair = self.dex.pairs.get(mint)
            if pair is None:
                continue
            state["t"] += 1
            pair["pairCreatedAt"] -= STEP_MINUTES * 60_000
            kind = state["kind"]
            if kind == "pump":
                drift = 0.25 if state["t"] < state["peak_at"] else -0.2
            elif kind == "dump":
                drift = -0.12
            elif kind == "rug":
                drift = 0.1
                if state["t"] == state["peak_at"]:
                    pair["liquidity"]["usd"] *= 0.03
                    state["price"] *= 0.08
                    self.rpc.holdings[(f"Dev{mint}", mint)] = 0
            else:
                drift = 0.0
            state["price"] = max(1e-9, state["price"] * (1 + drift + rng.gauss(0, 0.08)))
            pair["priceUsd"] = str(state["price"])
            if rng.random() < 0.01:
                self.dex.pairs.pop(mint, None)  # pool forsvinder helt
        for owner_mint in list(self.rpc.holdings):
            if rng.random() < 0.05:
                self.rpc.holdings[owner_mint] = int(self.rpc.holdings[owner_mint] * rng.choice([0.2, 1.0]))
        self.dex.boosts = self.dex.boosts[-30:]
        self.dex.profiles = self.dex.profiles[-30:]


def age_database(store: MemeStore, minutes: float) -> None:
    """Skub alle tidsstempler bagud, så motoren oplever, at tiden går."""
    shift = timedelta(minutes=minutes)

    def older(value: str | None) -> str | None:
        if not value:
            return value
        return (datetime.fromisoformat(value) - shift).isoformat(timespec="seconds")

    for row in store._all("SELECT id, opened_at FROM positions WHERE status = 'open'"):
        store.update_position(row["id"], opened_at=older(row["opened_at"]))
    for row in store._all("SELECT id, ts FROM shadows WHERE status = 'tracking'"):
        store.update_shadow(row["id"], ts=older(row["ts"]))
    store._write("UPDATE candidates SET ts = ?", ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds"),))
    seen = store.get_json("sniper_seen", {}) or {}
    store.set_json("sniper_seen", {mint: stamp - minutes * 60 for mint, stamp in seen.items()})


class Violation(AssertionError):
    pass


def check_invariants(engine: MemeEngine, blocked: dict[str, str], tick: int) -> None:
    def fail(message: str) -> None:
        raise Violation(f"runde {tick}: {message}")

    if engine.last_error:
        fail(f"fejl i runden: {engine.last_error}")
    store = engine.store
    totals = engine._totals()
    cash = engine._paper.cash_usd(0.0)
    marks = 0.0
    open_rows = store.open_positions("paper")
    for row in open_rows:
        frac = int(row["remaining_raw"]) / max(1, int(row["token_raw"]))
        marks += float(row["cost_usd"]) * frac * float(row["last_ratio"] or 1.0)
        if not 0 <= int(row["remaining_raw"]) <= int(row["token_raw"]):
            fail(f"ugyldig rest i position {row['id']}")
    if cash < -1e-6:
        fail(f"negativ papirkasse {cash}")
    if abs(cash + marks - totals["equity"]) > 1e-6:
        fail(f"regnskab stemmer ikke: kasse {cash:.6f} + positioner {marks:.6f} != egenkapital {totals['equity']:.6f}")
    if totals["deployed"] > engine.settings.bankroll_usd + 1e-6:
        fail(f"mere i spil ({totals['deployed']:.2f}) end bankroll")
    mints = [row["mint"] for row in open_rows]
    if len(mints) != len(set(mints)):
        fail("to åbne positioner i samme token")
    momentum = [row for row in open_rows if row["strategy"] == "momentum"]
    snipes = [row for row in open_rows if row["strategy"] == "sniper"]
    if len(momentum) > engine.settings.max_open:
        fail(f"{len(momentum)} momentum-positioner > {engine.settings.max_open}")
    if len(snipes) > engine.settings.sniper_max_open:
        fail(f"{len(snipes)} snipes > {engine.settings.sniper_max_open}")
    day = datetime.now(timezone.utc).date().isoformat()
    if store.trades_opened_on("paper", day, "momentum") > engine.settings.max_trades_per_day:
        fail("for mange momentum-køb i dag")
    if store.trades_opened_on("paper", day, "sniper") > engine.settings.sniper_max_per_day:
        fail("for mange snipes i dag")
    for row in open_rows:
        if row["mint"] in blocked and row["opened_at"] > blocked[row["mint"]]:
            fail(f"blokeret token {row['mint']} blev købt efter blokeringen")
        if float(row["cost_usd"]) > engine.settings.position_usd + 1e-6:
            fail(f"position større end MEME_POSITION_USD: {row['cost_usd']}")
    for row in store.closed_positions("paper"):
        if row["exit_reason"] is None or row["closed_at"] is None:
            fail(f"lukket position {row['id']} mangler årsag eller tid")
    status = engine.status()
    json.dumps(status)  # dashboardet skal kunne serialisere alt


PROFILES = {
    "normal": dict(paper_fail_rate=0.05, max_trades_per_day=50, sniper_max_per_day=50, daily_loss_pct=0.9, max_drawdown_pct=0.95),
    "hard": dict(paper_fail_rate=0.3, max_trades_per_day=8, sniper_max_per_day=10, daily_loss_pct=0.25, max_drawdown_pct=0.5),
}


def run(seed: int, ticks: int = 120, verbose: bool = False, profile: str = "normal") -> dict:
    rng = random.Random(seed)
    cfg = meme_fakes.settings(
        paper_slippage_pct=1.0,
        paper_sniper_extra_pct=2.0,
        shadows=True,
        dev_watch=True,
        **PROFILES[profile],
    )
    dex, rug = SoakDex(), FakeRug()
    jup = SoakJup(dex)
    rpc = SoakRPC()
    store = MemeStore(":memory:")
    notes: list[str] = []
    clients = MemeClients(cfg, dex=dex, rug=rug, jup=jup, x=None, rpc=rpc)
    engine = MemeEngine(cfg, store=store, clients=clients, notifier=notes.append)
    engine._paper.rng = random.Random(seed + 1)
    market = Market(rng, dex, rug, jup, rpc)
    blocked: dict[str, str] = {}
    actions = []
    for tick in range(ticks):
        for _ in range(rng.randint(0, 3)):
            market.launch(sniper=rng.random() < 0.5)
        market.step()
        roll = rng.random()
        if roll < 0.02:
            engine.pause(not engine.paused)
            actions.append((tick, "pause", engine.paused))
        elif roll < 0.03:
            engine.kill()
            for _retry in range(8):  # fejlede salg prøves igen i de næste runder
                if not store.open_positions("paper"):
                    break
                engine.tick()
            sellable = [row for row in store.open_positions("paper") if row["mint"] not in jup.no_route and row["mint"] in dex.pairs]
            if sellable:
                raise Violation(f"runde {tick}: nødstop efterlod {len(sellable)} positioner, der kunne sælges")
            engine.resume()
            actions.append((tick, "kill", None))
        elif roll < 0.05:
            rows = store.open_positions("paper")
            if rows:
                try:
                    engine.sell_position(rng.choice(rows)["id"])
                except ValueError:
                    pass
                actions.append((tick, "sell", None))
        elif roll < 0.08:
            engine.update_sniper({"take_profit_pct": rng.choice([0.25, 0.5, 1.0, 3.0]), "stop_loss_pct": rng.choice([0.2, 0.35, 0.5]), "sell_fraction": rng.choice([0.5, 1.0])})
            actions.append((tick, "sniper", engine.sniper.take_profit_pct))
        elif roll < 0.09 and market.tokens:
            mint = rng.choice(list(market.tokens))
            engine.block_token(mint, "X")
            blocked[mint] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            actions.append((tick, "block", mint))
        engine._last_insider_check = 0
        engine._last_shadow_track = 0
        before = time.monotonic()
        engine.tick(force_scan=True)
        if time.monotonic() - before > 5:
            raise Violation(f"runde {tick}: en runde tog over 5 sekunder")
        check_invariants(engine, blocked, tick)
        age_database(store, STEP_MINUTES)
    closed = store.closed_positions("paper")
    result = {
        "seed": seed,
        "trades": len(closed),
        "open": len(store.open_positions("paper")),
        "snipes": sum(1 for row in closed if row["strategy"] == "sniper"),
        "exits": sorted({row["exit_reason"] for row in closed}),
        "equity": round(engine._totals()["equity"], 4),
        "shadows_done": len(store.done_shadows("paper")),
        "lessons": len(store.recent_lessons(500)),
        "model_updates": engine.model.updates,
        "halted": engine.store.get_state("halt_dd:paper", "0") == "1",
    }
    if verbose:
        print(json.dumps(result))
    return result


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    ticks = int(sys.argv[2]) if len(sys.argv) > 2 else 120
    profile = sys.argv[3] if len(sys.argv) > 3 else "normal"
    failures = 0
    for seed in range(count):
        try:
            run(seed, ticks, verbose=True, profile=profile)
        except Violation as exc:
            failures += 1
            print(f"SEED {seed}: {exc}")
    print(f"{count - failures}/{count} seeds uden fejl")
