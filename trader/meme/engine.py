"""Memecoin-motoren. Kører 24/7 i sin egen tråd.

Hver runde (MEME_MONITOR_SECONDS): tjek åbne positioner og sælg efter reglerne.
Hver scanning (MEME_SCAN_SECONDS): find kandidater, filtrér, rug-tjek, score, spørg Claude
(hvis budgettet tillader det), risikotjek og køb. Efter hver lukket handel lærer botten.
"""

from __future__ import annotations

import calendar
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from trader.config import Settings
from trader.meme.clients import (
    DexScreener,
    Jupiter,
    PairInfo,
    RugCheck,
    SolanaRPC,
    SourceError,
    TokenInfo,
    XClient,
)
from trader.meme.config import (
    LAMPORTS_PER_SOL,
    MEME_LIVE_PHRASE,
    PARAM_BOUNDS,
    PARAM_LABELS,
    QUOTE_MINTS,
    SOL_MINT,
    UNLOCK_MIN_DAYS,
    UNLOCK_MIN_PROFIT_FACTOR,
    UNLOCK_MIN_TRADES,
    MemeSettings,
    StrategyParams,
    clamp_param,
    public_meme_settings,
)
from trader.meme.copytrade import CopyTracker
from trader.meme.learning import (
    EXIT_LABELS,
    FEATURES,
    WIN_THRESHOLD_PCT,
    Model,
    adapt_params,
    build_features,
    source_stats,
    trade_lesson,
)
from trader.meme.research import MemeResearcher, Verdict
from trader.meme.risk import EntryContext, daily_loss_hit, drawdown_hit, evaluate_entry, exit_decision
from trader.meme.safety import market_rejections, roundtrip_rejections, rug_rejections
from trader.meme.store import MemeStore, loads, now_iso, utc_day
from trader.meme.wallet import PaperWallet, SolanaWallet, load_keypair

logger = logging.getLogger("trader.meme.engine")

CANDIDATE_COOLDOWN_MINUTES = 20
LOSS_COOLDOWN_HOURS = 12
MAX_DEEP_CHECKS = 6
MAX_BUYS_PER_SCAN = 2
MAX_COPY_WALLETS = 25
EXIT_FAILURE_LIMIT = 6
MISSING_PRICE_LIMIT = 20
ADAPT_EVERY = 5
NO_LEARN_EXITS = {"kill", "manual"}
FEE_MARGIN_USD = 0.05

SOURCE_LABELS = {
    "jup_trending": "Jupiter: trending",
    "jup_organic": "Jupiter: organisk score",
    "dex_boost": "DexScreener: boost",
    "dex_profile": "DexScreener: ny profil",
}


def source_label(source: str) -> str:
    if source.startswith("copy:"):
        wallet = source[5:]
        return f"Copy: {wallet[:4]}…{wallet[-4:]}"
    return SOURCE_LABELS.get(source, source)


def _valid_address(text: str) -> bool:
    if not 32 <= len(text) <= 44:
        return False
    try:
        from solders.pubkey import Pubkey

        Pubkey.from_string(text)
    except Exception:
        return False
    return True


def _downsample(rows: list[dict], limit: int) -> list[dict]:
    if len(rows) <= limit:
        return rows
    step = len(rows) / limit
    picked = [rows[int(index * step)] for index in range(limit)]
    if picked[-1] is not rows[-1]:
        picked.append(rows[-1])
    return picked


def _hold_minutes(row: dict) -> float | None:
    try:
        opened = datetime.fromisoformat(row["opened_at"])
        closed = datetime.fromisoformat(row["closed_at"])
    except (TypeError, ValueError, KeyError):
        return None
    return max(0.0, (closed - opened).total_seconds() / 60.0)


def _minutes_since(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - moment).total_seconds() / 60.0)


class MemeClients:
    def __init__(
        self,
        settings: MemeSettings,
        *,
        dex: Any = None,
        rug: Any = None,
        jup: Any = None,
        x: Any = None,
        rpc: Any = None,
    ) -> None:
        self.dex = dex or DexScreener()
        self.rug = rug or RugCheck()
        self.jup = jup or Jupiter(settings)
        self.x = x if x is not None else (XClient(settings.x_bearer_token) if settings.x_configured else None)
        if rpc is not None:
            self.rpc = rpc
        elif settings.copy_wallets or settings.wallet_configured:
            self.rpc = SolanaRPC(settings.rpc_url)
        else:
            self.rpc = None

    def close(self) -> None:
        for client in (self.dex, self.rug, self.jup, self.x, self.rpc):
            closer = getattr(client, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass


class MemeEngine:
    def __init__(
        self,
        settings: MemeSettings | None = None,
        *,
        main_settings: Settings | None = None,
        store: MemeStore | None = None,
        clients: MemeClients | None = None,
        researcher: MemeResearcher | None = None,
        notifier: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings or MemeSettings.from_env()
        self.main_settings = main_settings
        self.store = store or MemeStore(self.settings.db_path)
        self.clients = clients or MemeClients(self.settings)
        self.researcher = researcher or MemeResearcher(self.settings, self.store)
        self._notifier = notifier
        self.params = StrategyParams.from_dict(self.store.get_json("params"))
        self.model = Model.from_dict(self.store.get_json("model"))
        self.kill_switch = self.store.get_state("kill_switch", "0") == "1"
        self.paused = self.store.get_state("paused", "0") == "1"
        self.copy = CopyTracker(self.clients.rpc, self.store, self._all_wallets())
        self._paper = PaperWallet(self.settings, self.store, self.clients.jup)
        self._live: SolanaWallet | None = None
        self._pubkey_cache: tuple[str, str] | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_scan = 0.0
        self._scan_requested = False
        self._last_snapshot = 0.0
        self._sol_usd = (0.0, 0.0)
        self._wallet_sol: float | None = None
        self._missing_price: dict[int, int] = {}
        self._copy_sells: set[tuple[str, str]] = set()
        self.last_tick: str | None = None
        self.last_scan_at: str | None = None
        self.last_scan: dict[str, Any] = {}
        self.last_error = ""
        self.consecutive_errors = 0
        self.mode = "paper"
        self._restore_mode()

    # ------------------------------------------------------------------ livscyklus
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="memebot", daemon=True)
        self._thread.start()
        self.store.add_event("info", f"Memebotten er startet i {'papir' if self.mode == 'paper' else 'LIVE'}-tilstand.")

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.clients.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("Memebottens løkke fangede en fejl")
            self._wake.wait(self.settings.monitor_seconds)
            self._wake.clear()

    def _notify(self, text: str) -> None:
        if self._notifier is not None:
            try:
                self._notifier(text)
            except Exception:
                logger.info("Notifikation fejlede")
            return
        if self.main_settings is not None:
            from trader.notify import send

            send(self.main_settings, f"Memebot: {text}")

    # ------------------------------------------------------------------ tilstand
    def _restore_mode(self) -> None:
        stored = self.store.get_state("mode", "paper") or "paper"
        if stored == "live" and self._unlock()["all_passed"]:
            self.mode = "live"
            return
        if stored == "live":
            self.store.set_state("mode", "paper")
            self.store.add_event("warning", "Tilbage på papir: kravene til rigtige penge er ikke længere opfyldt.")
        self.mode = "paper"

    def _wallet(self):
        if self.mode != "live":
            return self._paper
        if self._live is None:
            rpc = self.clients.rpc or SolanaRPC(self.settings.rpc_url)
            self.clients.rpc = rpc
            self._live = SolanaWallet(self.settings, rpc, self.clients.jup)
        return self._live

    def _wallet_pubkey(self) -> tuple[str, str]:
        """(pubkey, fejl). Aldrig selve nøglen."""
        if not self.settings.private_key:
            return "", "SOLANA_PRIVATE_KEY er ikke sat."
        if self._pubkey_cache is None:
            try:
                self._pubkey_cache = (str(load_keypair(self.settings.private_key).pubkey()), "")
            except ValueError as exc:
                self._pubkey_cache = ("", str(exc))
        return self._pubkey_cache

    def _save_learning(self) -> None:
        self.store.set_json("params", self.params.to_dict())
        self.store.set_json("model", self.model.to_dict())

    # ------------------------------------------------------------------ priser
    def _sol_price(self) -> float:
        price, stamp = self._sol_usd
        if price > 0 and time.time() - stamp < 60:
            return price
        fresh = 0.0
        try:
            fresh = self.clients.jup.sol_usd()
        except SourceError:
            try:
                pair = self.clients.dex.pairs_for([SOL_MINT]).get(SOL_MINT)
                fresh = pair.price_usd if pair else 0.0
            except SourceError:
                fresh = 0.0
        if fresh <= 0:
            if price > 0:
                return price
            raise SourceError("Ingen SOL-kurs fra Jupiter eller DexScreener.")
        self._sol_usd = (fresh, time.time())
        return fresh

    # ------------------------------------------------------------------ hovedrunde
    def request_scan(self) -> None:
        self._scan_requested = True
        self._wake.set()

    def tick(self, force_scan: bool = False) -> None:
        with self._lock:
            now = time.time()
            self.last_tick = now_iso()
            try:
                sol_usd = self._sol_price()
                wallet = self._wallet()
                if self.mode == "live":
                    try:
                        self._wallet_sol = wallet.sol_balance()
                    except SourceError as exc:
                        logger.info("SOL-saldo kunne ikke hentes: %s", exc)
                self._monitor(wallet, sol_usd)
                self._learn()
                day = utc_day()
                totals = self._totals()
                if self.store.ensure_day(day, self.mode, totals["equity"]):
                    self.store.add_event("info", f"Ny dag {day}. Egenkapital {totals['equity']:.2f} USD.")
                    self._daily_review()
                halted_day, halted_dd = self._halts(totals, day, wallet, sol_usd)
                if force_scan or self._scan_requested or now - self._last_scan >= self.settings.scan_seconds:
                    self._scan_requested = False
                    self._last_scan = now
                    self._scan(wallet, sol_usd, day, entries_allowed=not (self.kill_switch or self.paused or halted_day or halted_dd))
                if now - self._last_snapshot >= 300:
                    self._last_snapshot = now
                    self.store.add_equity(self.mode, self._totals()["equity"])
                    self.store.set_day_end(day, self.mode, self._totals()["equity"])
                    self.store.prune()
                self.consecutive_errors = 0
                self.last_error = ""
            except Exception as exc:
                self.consecutive_errors += 1
                message = str(exc) or exc.__class__.__name__
                if message != self.last_error or self.consecutive_errors % 20 == 0:
                    self.store.add_event("error", f"Fejl i runden ({self.consecutive_errors}): {message}")
                self.last_error = message
                if self.consecutive_errors in (5, 60):
                    self._notify(f"{self.consecutive_errors} fejl i træk: {message}")
                logger.warning("Memebot-fejl: %s", message)

    # ------------------------------------------------------------------ positioner og salg
    def _monitor(self, wallet, sol_usd: float) -> None:
        positions = self.store.open_positions(self.mode)
        if not positions:
            self._copy_sells.clear()
            return
        mints = [row["mint"] for row in positions]
        pairs: dict[str, PairInfo] = {}
        try:
            pairs = self.clients.dex.pairs_for(mints)
        except SourceError as exc:
            logger.info("DexScreener utilgængelig under overvågning: %s", exc)
        prices = {mint: pair.price_usd for mint, pair in pairs.items()}
        missing = [mint for mint in mints if mint not in prices]
        if missing:
            try:
                prices.update(self.clients.jup.prices(missing))
            except SourceError:
                pass
        for pos in positions:
            mint = pos["mint"]
            price = prices.get(mint)
            if not price:
                count = self._missing_price.get(pos["id"], 0) + 1
                self._missing_price[pos["id"]] = count
                if count >= MISSING_PRICE_LIMIT:
                    self._sell(pos, 1.0, "no_price", "Ingen kurs i lang tid. Forsøger at sælge.", wallet, sol_usd)
                continue
            self._missing_price.pop(pos["id"], None)
            ratio = price / float(pos["entry_price_usd"])
            peak = max(float(pos["peak_ratio"] or 1.0), ratio)
            liquidity = pairs[mint].liquidity_usd if mint in pairs else pos.get("liquidity_last")
            changes = {"last_ratio": ratio, "peak_ratio": peak, "last_price_usd": price, "liquidity_last": liquidity}
            self.store.update_position(int(pos["id"]), **changes)
            pos.update(changes)
            source = str(pos["source"])
            if source.startswith("copy:") and (source[5:], mint) in self._copy_sells:
                self._sell(pos, 1.0, "copy_exit", "Den fulgte trader solgte. Botten sælger også.", wallet, sol_usd)
                continue
            decision = exit_decision(
                ratio=ratio,
                peak_ratio=peak,
                minutes_held=_minutes_since(pos["opened_at"]),
                partial_taken=bool(pos["partial_taken"]),
                liquidity_entry=pos.get("liquidity_entry"),
                liquidity_now=liquidity,
                params=self.params,
            )
            if decision.sell:
                self._sell(pos, decision.fraction, decision.kind, decision.reason, wallet, sol_usd)
        self._copy_sells.clear()

    def _sell(self, pos: dict, fraction: float, kind: str, reason: str, wallet, sol_usd: float) -> bool:
        remaining = int(pos["remaining_raw"])
        if remaining <= 0:
            self._close(pos, float(pos["proceeds_usd"]), float(pos["fees_usd"]), kind, reason, loads(pos["signatures"], []))
            return True
        full = fraction >= 0.999
        amount = remaining if full else max(1, int(remaining * fraction))
        fill = wallet.sell(pos["mint"], amount, sol_usd)
        if not fill.ok:
            failures = int(pos.get("exit_failures") or 0) + 1
            self.store.update_position(int(pos["id"]), exit_failures=failures)
            pos["exit_failures"] = failures
            self.store.add_event("warning", f"Salg af {pos['symbol']} fejlede ({failures}): {fill.error}")
            if failures >= EXIT_FAILURE_LIMIT and kind != "take_profit":
                self._close(
                    pos,
                    float(pos["proceeds_usd"]),
                    float(pos["fees_usd"]),
                    "rug_no_route",
                    f"Kunne ikke sælge efter {failures} forsøg. Bogført som tab. Sidste fejl: {fill.error}",
                    loads(pos["signatures"], []),
                )
            return False
        sold = min(remaining, fill.token_raw or amount)
        left = remaining - sold
        proceeds = float(pos["proceeds_usd"]) + fill.usd
        fees = float(pos["fees_usd"]) + fill.fee_usd
        signatures = (loads(pos["signatures"], []) or []) + ([fill.signature] if fill.signature else [])
        dust = max(1, int(int(pos["token_raw"]) * 0.001))
        if full or left <= dust:
            self._close(pos, proceeds, fees, kind, reason, signatures)
            wallet.after_close(pos["mint"])
            return True
        self.store.update_position(
            int(pos["id"]),
            remaining_raw=left,
            proceeds_usd=proceeds,
            fees_usd=fees,
            partial_taken=True,
            signatures=signatures,
            exit_failures=0,
        )
        pos.update({"remaining_raw": left, "proceeds_usd": proceeds, "fees_usd": fees, "partial_taken": 1})
        self.store.add_event("trade", f"Solgt {fraction * 100:.0f} % af {pos['symbol']} for {fill.usd:.2f} USD. {reason}")
        self._notify(f"Solgt {fraction * 100:.0f} % af {pos['symbol']} (+{fill.usd:.2f} USD). {reason}")
        return True

    def _close(self, pos: dict, proceeds: float, fees: float, kind: str, reason: str, signatures: list) -> None:
        cost = float(pos["cost_usd"])
        pnl = proceeds - cost - fees
        pnl_pct = pnl / cost if cost > 0 else 0.0
        self.store.update_position(
            int(pos["id"]),
            status="closed",
            closed_at=now_iso(),
            remaining_raw=0,
            proceeds_usd=proceeds,
            fees_usd=fees,
            pnl_usd=pnl,
            pnl_pct=pnl_pct,
            exit_reason=kind,
            signatures=signatures,
        )
        self._missing_price.pop(int(pos["id"]), None)
        label = EXIT_LABELS.get(kind, kind)
        self.store.add_event(
            "trade",
            f"Lukket {pos['symbol']} ({label}): {pnl:+.2f} USD ({pnl_pct * 100:+.0f} %).",
            reason,
        )
        self._notify(f"Lukket {pos['symbol']} ({label}): {pnl:+.2f} USD ({pnl_pct * 100:+.0f} %).")

    def sell_position(self, position_id: int) -> None:
        with self._lock:
            pos = self.store.get_position(position_id)
            if pos is None or pos["status"] != "open" or pos["mode"] != self.mode:
                raise ValueError("Positionen findes ikke eller er allerede lukket.")
            self._sell(pos, 1.0, "manual", "Solgt manuelt fra dashboardet.", self._wallet(), self._sol_price())

    def _sell_all(self, mode: str, kind: str, reason: str) -> None:
        wallet = self._paper if mode == "paper" else self._wallet()
        try:
            sol_usd = self._sol_price()
        except SourceError as exc:
            self.store.add_event("error", f"Kunne ikke sælge alt: {exc}")
            return
        for pos in self.store.open_positions(mode):
            try:
                self._sell(pos, 1.0, kind, reason, wallet, sol_usd)
            except Exception as exc:
                self.store.add_event("error", f"Salg af {pos['symbol']} fejlede: {exc}")

    # ------------------------------------------------------------------ læring
    def _learn(self) -> None:
        changed = False
        for pos in self.store.unlearned_closed():
            features = loads(pos["features"], {}) or {}
            self.store.update_position(int(pos["id"]), learned=True)
            # Nødstop, manuelle salg og drawdown-salg siger intet om signalernes kvalitet.
            if not features or pos["exit_reason"] in NO_LEARN_EXITS:
                continue
            before = Model.from_dict(self.model.to_dict())
            won = float(pos["pnl_pct"] or 0.0) > WIN_THRESHOLD_PCT
            changes = self.model.update(features, won)
            self.store.add_lesson("handel", trade_lesson(pos, features, before, changes), int(pos["id"]))
            changed = True
            closed = self.store.closed_positions(pos["mode"])
            if len(closed) % ADAPT_EVERY == 0:
                for note in adapt_params(self.params, closed):
                    self.store.add_lesson("parameter", note, int(pos["id"]))
        if changed:
            self._save_learning()

    def _daily_review(self) -> None:
        last_id = int(self.store.get_state("review_last_id", "0") or 0)
        closed = [row for row in self.store.closed_positions(self.mode) if int(row["id"]) > last_id]
        if len(closed) < 3 or not self.researcher.available(0.1):
            return
        trades = []
        for row in closed:
            features = loads(row["features"], {}) or {}
            ai = loads(row["ai"], None)
            trades.append(
                {
                    "symbol": row["symbol"],
                    "source": row["source"],
                    "score": round(float(row["score"] or 0), 3),
                    "pnl_pct": round(float(row["pnl_pct"] or 0) * 100, 1),
                    "exit": row["exit_reason"],
                    "minutes_held": round(
                        (datetime.fromisoformat(row["closed_at"]) - datetime.fromisoformat(row["opened_at"])).total_seconds() / 60
                    )
                    if row["closed_at"]
                    else None,
                    "peak_pct": round((float(row["peak_ratio"] or 1) - 1) * 100, 1),
                    "features": {key: round(float(value), 3) for key, value in features.items()},
                    "claude": (ai or {}).get("decision"),
                }
            )
        review = self.researcher.daily_review(trades, self.params.to_dict())
        self.store.set_state("review_last_id", str(max(int(row["id"]) for row in closed)))
        if review is None:
            return
        for lesson in review.lessons[:5]:
            self.store.add_lesson("claude", lesson.strip()[:400])
        for change in review.param_changes[:3]:
            if change.name not in PARAM_BOUNDS:
                continue
            old = getattr(self.params, change.name)
            new = clamp_param(change.name, float(change.value))
            if abs(new - old) < 1e-9:
                continue
            setattr(self.params, change.name, new)
            self.store.add_lesson(
                "parameter",
                f"Claude: {PARAM_LABELS.get(change.name, change.name)} {old:g} → {new:g}. {change.why.strip()[:240]}",
            )
        self._save_learning()
        self.store.add_event("info", "Daglig gennemgang fra Claude er gemt under 'Hvad botten har lært'.", review.summary[:600])

    # ------------------------------------------------------------------ regnskab og stop
    def _totals(self, mode: str | None = None) -> dict[str, float]:
        mode = mode or self.mode
        unrealized = 0.0
        deployed = 0.0
        for pos in self.store.open_positions(mode):
            token_raw = max(1, int(pos["token_raw"]))
            fraction = int(pos["remaining_raw"]) / token_raw
            mark = float(pos["cost_usd"]) * fraction * float(pos["last_ratio"] or 1.0)
            deployed += float(pos["cost_usd"]) * fraction
            unrealized += float(pos["proceeds_usd"]) + mark - float(pos["cost_usd"]) - float(pos["fees_usd"])
        closed = self.store.closed_positions(mode)
        realized = sum(float(row["pnl_usd"]) for row in closed)
        today = utc_day()
        realized_today = sum(float(row["pnl_usd"]) for row in closed if str(row["closed_at"] or "")[:10] == today)
        bankroll = self.settings.bankroll_usd
        return {
            "bankroll": bankroll,
            "realized": realized,
            "unrealized": unrealized,
            "total_pnl": realized + unrealized,
            "today_pnl": realized_today + unrealized,
            "equity": bankroll + realized + unrealized,
            "deployed": deployed,
        }

    def _halts(self, totals: dict, day: str, wallet, sol_usd: float) -> tuple[bool, bool]:
        mode = self.mode
        day_key = f"halt_day:{mode}:{day}"
        dd_key = f"halt_dd:{mode}"
        halted_day = self.store.get_state(day_key, "0") == "1"
        halted_dd = self.store.get_state(dd_key, "0") == "1"
        if not halted_day and daily_loss_hit(totals["today_pnl"], self.settings):
            self.store.set_state(day_key, "1")
            halted_day = True
            message = (
                f"Dagens tab ({totals['today_pnl']:.2f} USD) har ramt grænsen på "
                f"{self.settings.daily_loss_pct * 100:.0f} % af bankroll. Ingen nye køb før i morgen (UTC)."
            )
            self.store.add_event("warning", message)
            self._notify(message)
        reset = float(self.store.get_state(f"dd_reset:{mode}", "0") or 0)
        if not halted_dd and drawdown_hit(totals["total_pnl"] - reset, self.settings):
            self.store.set_state(dd_key, "1")
            halted_dd = True
            message = (
                f"Samlet tab har ramt {self.settings.max_drawdown_pct * 100:.0f} % af bankroll. "
                "Alle positioner sælges, og botten stopper, indtil du trykker Genoptag."
            )
            self.store.add_event("warning", message)
            self._notify(message)
            for pos in self.store.open_positions(mode):
                self._sell(pos, 1.0, "kill", "Drawdown-grænsen er ramt.", wallet, sol_usd)
        return halted_day, halted_dd

    # ------------------------------------------------------------------ scanning
    def _record(self, cand: dict, stage: str, passed: bool, reason: str, score: float | None = None, features=None) -> None:
        pair: PairInfo | None = cand.get("pair")
        self.store.add_candidate(
            mode=self.mode,
            mint=cand["mint"],
            symbol=(pair.symbol if pair else (cand.get("token").symbol if cand.get("token") else cand.get("symbol") or "?")),
            source=cand["source"],
            stage=stage,
            passed=passed,
            reason=reason,
            score=score,
            features=features,
            pair_url=pair.url if pair else "",
        )

    def _discover(self, copy_buys: list) -> tuple[dict[str, dict], list[str]]:
        found: dict[str, dict] = {}
        errors: list[str] = []

        def add(mint: str, source: str, **extra: Any) -> None:
            if not mint or mint in QUOTE_MINTS:
                return
            entry = found.get(mint)
            if entry is None:
                found[mint] = {"mint": mint, "source": source, "copy": False, "boosted": False, "token": None, **extra}
                return
            if extra.get("copy"):
                entry["source"] = source
                entry["copy"] = True
            if extra.get("boosted"):
                entry["boosted"] = True
            if extra.get("token") is not None and entry.get("token") is None:
                entry["token"] = extra["token"]

        for signal in copy_buys:
            add(signal.mint, f"copy:{signal.wallet}", copy=True)
        for category, source in (("toptrending", "jup_trending"), ("toporganicscore", "jup_organic")):
            try:
                for token in self.clients.jup.top_tokens(category, "1h", 50):
                    add(token.mint, source, token=token)
            except SourceError as exc:
                errors.append(f"Jupiter {category}: {exc}")
        try:
            for row in self.clients.dex.latest_boosts():
                add(str(row.get("tokenAddress")), "dex_boost", boosted=True)
        except SourceError as exc:
            errors.append(f"DexScreener boosts: {exc}")
        try:
            for row in self.clients.dex.latest_profiles():
                add(str(row.get("tokenAddress")), "dex_profile")
        except SourceError as exc:
            errors.append(f"DexScreener profiler: {exc}")
        return found, errors

    def _x_buzz(self, mint: str, symbol: str):
        if self.clients.x is None or self.settings.x_daily_requests <= 0:
            return None
        key = f"x_used:{utc_day()}"
        used = int(self.store.get_state(key, "0") or 0)
        if used >= self.settings.x_daily_requests:
            return None
        self.store.set_state(key, str(used + 1))
        try:
            return self.clients.x.buzz(mint, symbol)
        except SourceError as exc:
            logger.info("X-søgning fejlede: %s", exc)
            return None

    def _lessons_text(self) -> list[str]:
        return [row["text"] for row in self.store.recent_lessons(12) if row["kind"] in {"claude", "handel"}][:8]

    def _scan(self, wallet, sol_usd: float, day: str, *, entries_allowed: bool) -> None:
        self.last_scan_at = now_iso()
        summary: dict[str, Any] = {"discovered": 0, "fresh": 0, "market_ok": 0, "rug_ok": 0, "scored": 0, "bought": 0, "errors": []}
        self.last_scan = summary
        self.copy.wallets = self._all_wallets()
        self.copy.rpc = self.clients.rpc
        closed_all = self.store.closed_positions(self.mode)
        stats = source_stats(closed_all)
        muted = {source for source, entry in stats.items() if entry["muted"]}

        signals = []
        try:
            signals = self.copy.poll(muted)
        except Exception as exc:
            summary["errors"].append(f"Copy-trading: {exc}")
        for signal in signals:
            if signal.side == "sell":
                self._copy_sells.add((signal.wallet, signal.mint))
        copy_buys = [signal for signal in signals if signal.side == "buy"]
        for signal in copy_buys:
            self.store.add_event("info", f"Fulgt wallet {signal.wallet[:4]}…{signal.wallet[-4:]} købte {signal.mint[:6]}… for {signal.sol:.2f} SOL.")

        if not entries_allowed:
            summary["note"] = "Ingen nye køb (pause, nødstop eller tabsgrænse)."
            return

        found, errors = self._discover(copy_buys)
        summary["errors"].extend(errors)
        summary["discovered"] = len(found)
        open_mints = {row["mint"] for row in self.store.open_positions(self.mode)}
        cutoff_loss = (datetime.now(timezone.utc) - timedelta(hours=LOSS_COOLDOWN_HOURS)).isoformat(timespec="seconds")
        cutoff_seen = (datetime.now(timezone.utc) - timedelta(minutes=CANDIDATE_COOLDOWN_MINUTES)).isoformat(timespec="seconds")
        blocked = {row["mint"] for row in self._blacklist()}
        fresh: list[dict] = []
        for cand in found.values():
            mint = cand["mint"]
            if mint in open_mints or mint in blocked:
                continue
            if cand["source"] in muted and not cand["copy"]:
                continue
            if self.store.ever_rugged(mint) or self.store.recent_loss_on(mint, cutoff_loss):
                continue
            seen = self.store.last_candidate_ts(mint)
            if seen and seen >= cutoff_seen and not cand["copy"]:
                continue
            fresh.append(cand)
        summary["fresh"] = len(fresh)
        if not fresh:
            return

        try:
            pairs = self.clients.dex.pairs_for([cand["mint"] for cand in fresh])
        except SourceError as exc:
            summary["errors"].append(f"DexScreener par: {exc}")
            return
        now_ms = int(time.time() * 1000)
        market_ok: list[dict] = []
        for cand in fresh:
            pair = pairs.get(cand["mint"])
            cand["pair"] = pair
            if pair is None:
                self._record(cand, "marked", False, "Ingen handelspar på DexScreener endnu")
                continue
            reasons = market_rejections(pair, self.params, now_ms)
            if reasons:
                self._record(cand, "marked", False, "; ".join(reasons[:3]))
                continue
            prelim = build_features(pair, token=cand.get("token"), copy_signal=cand["copy"], boosted=cand["boosted"], now_ms=now_ms)
            cand["prelim"] = self.model.score(prelim)
            market_ok.append(cand)
        summary["market_ok"] = len(market_ok)
        market_ok.sort(key=lambda item: (item["copy"], item["prelim"]), reverse=True)
        deep = market_ok[:MAX_DEEP_CHECKS]
        for cand in market_ok[MAX_DEEP_CHECKS:]:
            self._record(cand, "marked", False, "Bestod markedsfiltre, men andre kandidater så stærkere ud i denne runde", cand["prelim"])

        need_info = [cand["mint"] for cand in deep if cand.get("token") is None]
        if need_info:
            try:
                infos = self.clients.jup.token_infos(need_info)
                for cand in deep:
                    if cand.get("token") is None:
                        cand["token"] = infos.get(cand["mint"])
            except SourceError as exc:
                summary["errors"].append(f"Jupiter token-info: {exc}")

        scored: list[dict] = []
        rug_passed = 0
        for cand in deep:
            pair = cand["pair"]
            token: TokenInfo | None = cand.get("token")
            try:
                report = self.clients.rug.report(cand["mint"])
            except SourceError as exc:
                summary["errors"].append(f"RugCheck: {exc}")
                report = None
            cand["report"] = report
            reasons = rug_rejections(report, token, self.params)
            if reasons:
                self._record(cand, "rug", False, "; ".join(reasons[:3]), cand["prelim"])
                continue
            rug_passed += 1
            buzz = self._x_buzz(cand["mint"], pair.symbol)
            cand["buzz"] = buzz
            features = build_features(
                pair,
                report=report,
                token=token,
                buzz=buzz,
                copy_signal=cand["copy"],
                boosted=cand["boosted"],
                now_ms=now_ms,
            )
            score = self.model.score(features)
            cand["features"] = features
            cand["score"] = score
            if score < self.params.min_score:
                top = ", ".join(FEATURES[name][0].lower() for name, value in self.model.contributions(features)[-2:] if value < 0)
                self._record(cand, "score", False, f"Score {score:.2f} under {self.params.min_score:.2f}. Trak ned: {top or 'intet særligt'}", score, features)
                continue
            scored.append(cand)
        summary["rug_ok"] = rug_passed
        summary["scored"] = len(scored)
        scored.sort(key=lambda item: item["score"], reverse=True)
        for cand in scored[MAX_BUYS_PER_SCAN:]:
            self._record(cand, "score", False, "God score, men maks. to køb pr. runde", cand["score"], cand["features"])
        for cand in scored[:MAX_BUYS_PER_SCAN]:
            if self._try_buy(cand, wallet, sol_usd, day):
                summary["bought"] += 1

    def _try_buy(self, cand: dict, wallet, sol_usd: float, day: str) -> bool:
        pair: PairInfo = cand["pair"]
        totals = self._totals()
        open_rows = self.store.open_positions(self.mode)
        try:
            cash = wallet.cash_usd(sol_usd)
        except SourceError as exc:
            self._record(cand, "risiko", False, f"Saldo kunne ikke hentes: {exc}", cand["score"], cand["features"])
            return False
        decision = evaluate_entry(
            EntryContext(
                mint=cand["mint"],
                equity=totals["equity"],
                cash=max(0.0, cash - FEE_MARGIN_USD),
                deployed=totals["deployed"],
                open_count=len(open_rows),
                open_mints={row["mint"] for row in open_rows},
                trades_today=self.store.trades_opened_on(self.mode, day),
                kill_switch=self.kill_switch,
                halted_day=False,
                halted_drawdown=False,
            ),
            self.settings,
        )
        if not decision.allowed:
            self._record(cand, "risiko", False, decision.reason, cand["score"], cand["features"])
            return False
        lamports = int(decision.size_usd / sol_usd * LAMPORTS_PER_SOL)
        buy_quote = sell_quote = None
        try:
            buy_quote = self.clients.jup.quote(SOL_MINT, cand["mint"], lamports)
            sell_quote = self.clients.jup.quote(cand["mint"], SOL_MINT, buy_quote.out_amount)
        except SourceError as exc:
            logger.info("Quote fejlede for %s: %s", cand["mint"], exc)
        reasons = roundtrip_rejections(buy_quote, sell_quote, self.params)
        if reasons:
            self._record(cand, "rute", False, "; ".join(reasons), cand["score"], cand["features"])
            return False

        verdict: Verdict | None = None
        if self.researcher.available():
            verdict = self.researcher.research(self._ai_payload(cand, buy_quote, sell_quote), self._lessons_text())
            if verdict is not None:
                cand["features"]["ai_view"] = verdict.view
                cand["score"] = self.model.score(cand["features"])
                if verdict.decision != "buy":
                    flags = "; ".join(verdict.red_flags[:2])
                    self._record(cand, "claude", False, f"Claude: skip ({verdict.confidence}). {flags or verdict.summary}", cand["score"], cand["features"])
                    return False

        fill = wallet.buy(cand["mint"], decision.size_usd, sol_usd)
        if not fill.ok:
            self._record(cand, "køb", False, f"Købet fejlede: {fill.error}", cand["score"], cand["features"])
            self.store.add_event("warning", f"Køb af {pair.symbol} fejlede: {fill.error}")
            return False
        drivers = [FEATURES[name][0].lower() for name, value in self.model.contributions(cand["features"]) if value > 0.05][:3]
        reasoning = f"Score {cand['score']:.2f}. Kilde: {source_label(cand['source'])}. Stærkeste signaler: {', '.join(drivers) or 'ingen enkeltstående'}."
        if verdict is not None:
            reasoning += f" Claude ({verdict.confidence}): {verdict.summary}"
        report = cand.get("report")
        position_id = self.store.open_position(
            {
                "mode": self.mode,
                "mint": cand["mint"],
                "symbol": pair.symbol,
                "name": pair.name,
                "source": cand["source"],
                "status": "open",
                "opened_at": now_iso(),
                "entry_price_usd": pair.price_usd,
                "cost_usd": fill.usd,
                "token_raw": fill.token_raw,
                "remaining_raw": fill.token_raw,
                "sol_spent": fill.sol,
                "fees_usd": fill.fee_usd,
                "liquidity_entry": pair.liquidity_usd,
                "liquidity_last": pair.liquidity_usd,
                "last_price_usd": pair.price_usd,
                "score": cand["score"],
                "features": cand["features"],
                "safety": {
                    "rug_risk": report.risk if report else None,
                    "top10_pct": report.top10_pct if report else None,
                    "lp_locked_pct": report.lp_locked_pct if report else None,
                    "warnings": report.warnings[:5] if report else [],
                    "roundtrip_loss_pct": round((1 - sell_quote.out_amount / buy_quote.in_amount) * 100, 2) if buy_quote and sell_quote and buy_quote.in_amount else None,
                },
                "ai": verdict.to_dict() if verdict else None,
                "reasoning": reasoning,
                "pair_url": pair.url,
                "signatures": [fill.signature] if fill.signature else [],
            }
        )
        self._record(cand, "købt", True, reasoning, cand["score"], cand["features"])
        self.store.add_event(
            "trade",
            f"Købt {pair.symbol} for {fill.usd:.2f} USD ({'papir' if self.mode == 'paper' else 'LIVE'}).",
            reasoning,
        )
        self._notify(f"Købt {pair.symbol} for {fill.usd:.2f} USD. {reasoning[:300]} {pair.url}")
        logger.info("Ny position %s i %s", position_id, pair.symbol)
        return True

    def _ai_payload(self, cand: dict, buy_quote, sell_quote) -> dict:
        pair: PairInfo = cand["pair"]
        token: TokenInfo | None = cand.get("token")
        report = cand.get("report")
        buzz = cand.get("buzz")
        age = pair.age_minutes()
        payload: dict[str, Any] = {
            "mint": pair.mint,
            "symbol": pair.symbol,
            "name": pair.name,
            "dex": pair.dex_id,
            "pair_url": pair.url,
            "price_usd": pair.price_usd,
            "liquidity_usd": round(pair.liquidity_usd),
            "market_cap_usd": round(pair.mcap),
            "age_hours": round(age / 60, 1) if age is not None else None,
            "volume_usd": {key: round(value) for key, value in pair.volume.items()},
            "price_change_pct": pair.change,
            "buys": pair.buys,
            "sells": pair.sells,
            "socials": pair.socials,
            "websites": pair.websites[:3],
            "found_via": source_label(cand["source"]),
            "bot_score": round(cand["score"], 3),
            "roundtrip_loss_pct": round((1 - sell_quote.out_amount / buy_quote.in_amount) * 100, 2) if buy_quote.in_amount else None,
        }
        if token is not None:
            payload["jupiter"] = {
                "organic_score": token.organic_score,
                "verified": token.verified,
                "holders": token.holders,
                "twitter": token.twitter,
                "website": token.website,
                "telegram": token.telegram,
                "launchpad": token.launchpad,
                "dev_balance_pct": token.dev_balance_pct,
            }
        if report is not None:
            payload["rugcheck"] = {
                "risk_0_to_1": round(report.risk, 2),
                "warnings": report.warnings[:6],
                "top10_pct": report.top10_pct,
                "lp_locked_pct": report.lp_locked_pct,
                "holders": report.holders,
                "insiders_detected": report.insiders,
            }
        if buzz is not None:
            payload["x_recent_search"] = {
                "tweets": buzz.tweets,
                "unique_authors": buzz.authors,
                "likes": buzz.likes,
                "reposts": buzz.reposts,
                "sample_tweets_untrusted": buzz.samples,
            }
        return payload

    # ------------------------------------------------------------------ styring
    def kill(self) -> None:
        with self._lock:
            self.kill_switch = True
            self.store.set_state("kill_switch", "1")
            self.store.add_event("warning", "Nødstop: alle positioner sælges, og nye køb er blokeret.")
            self._notify("Nødstop aktiveret. Alle positioner sælges.")
            self._sell_all(self.mode, "kill", "Nødstop.")

    def resume(self) -> None:
        with self._lock:
            self.kill_switch = False
            self.store.set_state("kill_switch", "0")
            totals = self._totals()
            self.store.set_state(f"dd_reset:{self.mode}", repr(totals["total_pnl"]))
            self.store.set_state(f"halt_dd:{self.mode}", "0")
            self.store.add_event("info", "Genoptaget. Drawdown måles fra nu. Dagens tabsgrænse gælder stadig.")

    def pause(self, paused: bool) -> None:
        with self._lock:
            self.paused = bool(paused)
            self.store.set_state("paused", "1" if self.paused else "0")
            if self.paused:
                self.store.add_event("info", "Nye køb er sat på pause. Åbne positioner passes stadig.")
            else:
                self.store.add_event("info", "Pausen er ophævet. Botten køber igen, når noget består alle tjek.")

    def _extra_wallets(self) -> list[str]:
        value = self.store.get_json("copy_wallets_extra", [])
        return [str(item) for item in value] if isinstance(value, list) else []

    def _all_wallets(self) -> tuple[str, ...]:
        merged = list(self.settings.copy_wallets)
        for wallet in self._extra_wallets():
            if wallet not in merged:
                merged.append(wallet)
        return tuple(merged)

    def add_wallet(self, wallet: str) -> None:
        wallet = wallet.strip()
        if not _valid_address(wallet):
            raise ValueError("Det ligner ikke en Solana-adresse.")
        if wallet in self._all_wallets():
            raise ValueError("Den wallet følges allerede.")
        if len(self._all_wallets()) >= MAX_COPY_WALLETS:
            raise ValueError(f"Højst {MAX_COPY_WALLETS} wallets kan følges.")
        with self._lock:
            extra = self._extra_wallets() + [wallet]
            self.store.set_json("copy_wallets_extra", extra)
            if self.clients.rpc is None:
                self.clients.rpc = SolanaRPC(self.settings.rpc_url)
            self.copy.rpc = self.clients.rpc
            self.copy.wallets = self._all_wallets()
            self.store.add_event("info", f"Følger nu {wallet[:4]}…{wallet[-4:]}. Kun nye handler fra nu af kopieres.")

    def remove_wallet(self, wallet: str) -> None:
        wallet = wallet.strip()
        if wallet in self.settings.copy_wallets:
            raise ValueError("Den wallet står i MEME_COPY_WALLETS og fjernes dér.")
        extra = self._extra_wallets()
        if wallet not in extra:
            raise ValueError("Den wallet følges ikke.")
        with self._lock:
            self.store.set_json("copy_wallets_extra", [item for item in extra if item != wallet])
            self.copy.wallets = self._all_wallets()
            self.store.add_event("info", f"Følger ikke længere {wallet[:4]}…{wallet[-4:]}.")

    def _blacklist(self) -> list[dict]:
        value = self.store.get_json("blacklist", [])
        return [row for row in value if isinstance(row, dict) and row.get("mint")] if isinstance(value, list) else []

    def block_token(self, mint: str, symbol: str = "") -> None:
        mint = mint.strip()
        if not _valid_address(mint):
            raise ValueError("Det ligner ikke en token-adresse.")
        rows = [row for row in self._blacklist() if row["mint"] != mint]
        rows.append({"mint": mint, "symbol": symbol.strip()[:24] or "?", "ts": now_iso()})
        self.store.set_json("blacklist", rows[-500:])
        self.store.add_event("info", f"{symbol or mint[:6]} er blokeret. Botten køber det aldrig.")

    def unblock_token(self, mint: str) -> None:
        rows = [row for row in self._blacklist() if row["mint"] != mint.strip()]
        self.store.set_json("blacklist", rows)
        self.store.add_event("info", f"{mint[:6]}… er ikke længere blokeret.")

    def _unlock(self) -> dict:
        closed = self.store.closed_positions("paper")
        pnl = [float(row["pnl_usd"]) for row in closed]
        wins = sum(value for value in pnl if value > 0)
        losses = -sum(value for value in pnl if value < 0)
        factor = wins / losses if losses > 0 else (float("inf") if wins > 0 else 0.0)
        days = len({str(row["closed_at"] or "")[:10] for row in closed if row["closed_at"]})
        net = sum(pnl)
        pubkey, key_error = self._wallet_pubkey()
        phrase_ok = self.store.get_state("phrase_accepted", "0") == "1"
        items = [
            {"id": "trades", "label": f"Mindst {UNLOCK_MIN_TRADES} lukkede papirhandler", "ok": len(closed) >= UNLOCK_MIN_TRADES, "detail": f"{len(closed)} / {UNLOCK_MIN_TRADES}"},
            {"id": "days", "label": f"Handler på mindst {UNLOCK_MIN_DAYS} forskellige dage", "ok": days >= UNLOCK_MIN_DAYS, "detail": f"{days} / {UNLOCK_MIN_DAYS}"},
            {"id": "pnl", "label": "Papirresultat over 0", "ok": net > 0, "detail": f"{net:+.2f} USD".replace(".", ",")},
            {
                "id": "pf",
                "label": f"Profitfaktor mindst {UNLOCK_MIN_PROFIT_FACTOR:g}".replace(".", ","),
                "ok": factor >= UNLOCK_MIN_PROFIT_FACTOR and len(closed) > 0,
                "detail": "∞" if factor == float("inf") else f"{factor:.2f}".replace(".", ","),
            },
            {"id": "allow", "label": "MEME_ALLOW_LIVE=true", "ok": self.settings.allow_live, "detail": "ja" if self.settings.allow_live else "nej"},
            {
                "id": "wallet",
                "label": "Wallet-nøgle kan læses (separat Phantom-konto)",
                "ok": bool(pubkey),
                "detail": f"{pubkey[:4]}…{pubkey[-4:]}" if pubkey else key_error,
            },
            {"id": "phrase", "label": "Bekræftelsessætning er afgivet", "ok": phrase_ok, "detail": MEME_LIVE_PHRASE},
        ]
        return {
            "items": items,
            "all_passed": all(item["ok"] for item in items),
            "ready_for_phrase": all(item["ok"] for item in items if item["id"] != "phrase"),
            "phrase": MEME_LIVE_PHRASE,
        }

    def request_live(self, phrase: str) -> tuple[bool, str]:
        with self._lock:
            if phrase.strip() != MEME_LIVE_PHRASE:
                return False, "Sætningen matcher ikke præcist."
            check = self._unlock()
            if not check["ready_for_phrase"]:
                missing = [item["label"] for item in check["items"] if item["id"] != "phrase" and not item["ok"]]
                return False, "Ikke alle krav er opfyldt: " + ", ".join(missing)
            self._sell_all("paper", "manual", "Papirpositioner lukkes ved skift til rigtige penge.")
            self.store.set_state("phrase_accepted", "1")
            self.mode = "live"
            self.store.set_state("mode", "live")
            self.store.set_state("dd_reset:live", repr(self._totals("live")["total_pnl"]))
            self.store.add_event("warning", f"RIGTIGE PENGE slået til. Botten bruger højst {self.settings.bankroll_usd:.2f} USD.")
            self._notify(f"Rigtige penge er slået til. Loft {self.settings.bankroll_usd:.2f} USD.")
            return True, f"Rigtige penge er slået til. Botten bruger højst {self.settings.bankroll_usd:.2f} USD."

    def return_to_paper(self) -> None:
        with self._lock:
            if self.mode == "live":
                self._sell_all("live", "manual", "Tilbage til papir: live-positioner sælges.")
            self.mode = "paper"
            self.store.set_state("mode", "paper")
            self.store.set_state("phrase_accepted", "0")
            self.store.add_event("info", "Tilbage på papirpenge.")

    # ------------------------------------------------------------------ dashboard
    def status(self) -> dict:
        mode = self.mode
        totals = self._totals(mode)
        closed = self.store.closed_positions(mode)
        pnl = [float(row["pnl_usd"]) for row in closed]
        wins = [value for value in pnl if value > 0]
        losses = [value for value in pnl if value < 0]
        gross_win = sum(wins)
        gross_loss = -sum(losses)
        today = utc_day()
        month_start = today[:8] + "01"
        month_realized = sum(float(row["pnl_usd"]) for row in closed if str(row["closed_at"] or "")[:10] >= month_start)
        ai_month = self.store.ai_spend_since(month_start)
        now = datetime.now(timezone.utc)
        days_in_month = calendar.monthrange(now.year, now.month)[1]
        elapsed = max(1.0, now.day - 1 + now.hour / 24)
        month_net = month_realized - ai_month
        projection = month_net / elapsed * days_in_month
        goal = self.settings.goal_usd_per_month
        fees_total = sum(float(row["fees_usd"] or 0) for row in self.store.list_positions(mode, 5000))
        stats = source_stats(closed)
        unlock = self._unlock()
        pubkey, _key_error = self._wallet_pubkey()
        defaults = StrategyParams()
        initial = Model()
        holds = [value for value in (_hold_minutes(row) for row in closed) if value is not None]
        streak_kind, streak = "", 0
        for value in reversed(pnl):
            kind = "win" if value > 0 else "loss"
            if streak and kind != streak_kind:
                break
            streak_kind, streak = kind, streak + 1
        daily: dict[str, dict[str, float]] = {}
        for row in closed:
            day = str(row["closed_at"] or "")[:10]
            entry = daily.setdefault(day, {"pnl": 0.0, "trades": 0})
            entry["pnl"] += float(row["pnl_usd"])
            entry["trades"] += 1
        exits: dict[str, dict[str, Any]] = {}
        for row in closed:
            kind = str(row["exit_reason"] or "ukendt")
            entry = exits.setdefault(kind, {"kind": kind, "label": EXIT_LABELS.get(kind, kind), "count": 0, "pnl": 0.0})
            entry["count"] += 1
            entry["pnl"] += float(row["pnl_usd"])
        series = sorted(self.store.equity_series(mode, 20000), key=lambda row: row["ts"])
        series.append({"ts": now_iso(), "equity": totals["equity"]})
        cut_day = (now - timedelta(days=1)).isoformat(timespec="seconds")
        cut_week = (now - timedelta(days=7)).isoformat(timespec="seconds")
        extra_wallets = set(self._extra_wallets())
        next_scan = max(0.0, self.settings.scan_seconds - (time.time() - self._last_scan)) if self._last_scan else 0.0
        return {
            "mode": mode,
            "kill_switch": self.kill_switch,
            "paused": self.paused,
            "halted_day": self.store.get_state(f"halt_day:{mode}:{today}", "0") == "1",
            "halted_drawdown": self.store.get_state(f"halt_dd:{mode}", "0") == "1",
            "alerts": self._alerts(),
            "kpis": {
                **{key: round(value, 4) for key, value in totals.items()},
                "trades": len(closed),
                "open": len(self.store.open_positions(mode)),
                "win_rate": len(wins) / len(closed) if closed else None,
                "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
                "profit_factor_infinite": gross_loss == 0 and gross_win > 0,
                "avg_win": gross_win / len(wins) if wins else None,
                "avg_loss": -gross_loss / len(losses) if losses else None,
                "best": max(pnl) if pnl else None,
                "worst": min(pnl) if pnl else None,
                "fees_total": fees_total,
                "ai_today": self.store.ai_spend_on(today),
                "ai_month": ai_month,
                "ai_total": self.store.ai_spend_total(),
                "ai_budget_left": self.researcher.budget_left() if self.settings.ai_configured else 0.0,
                "net_after_ai": totals["realized"] - self.store.ai_spend_total(),
                "wins": len(wins),
                "losses": len(losses),
                "avg_hold_minutes": sum(holds) / len(holds) if holds else None,
                "streak": {"kind": streak_kind, "count": streak},
                "return_pct": (totals["total_pnl"] / totals["bankroll"]) if totals["bankroll"] else 0.0,
            },
            "goal": {
                "target": goal,
                "month_realized": month_realized,
                "month_ai": ai_month,
                "month_net": month_net,
                "projection": projection,
                "progress": max(0.0, month_net) / goal if goal else 0.0,
                "days_in_month": days_in_month,
            },
            "equity": {
                "24h": _downsample([row for row in series if row["ts"] >= cut_day], 300),
                "7d": _downsample([row for row in series if row["ts"] >= cut_week], 300),
                "all": _downsample(series, 300),
            },
            "daily_pnl": [{"day": day, **values} for day, values in sorted(daily.items())[-30:]],
            "exits": sorted(exits.values(), key=lambda item: item["count"], reverse=True),
            "levels": {
                "stop_loss_pct": self.params.stop_loss_pct,
                "take_profit_pct": self.params.take_profit_pct,
                "trail_activation_pct": self.params.trail_activation_pct,
                "trailing_stop_pct": self.params.trailing_stop_pct,
                "max_hold_minutes": self.params.max_hold_minutes,
                "stale_minutes": self.params.stale_minutes,
            },
            "positions": [self._position_view(row) for row in self.store.open_positions(mode)],
            "trades": [self._position_view(row) for row in self.store.list_positions(mode, 200) if row["status"] == "closed"],
            "candidates": [self._candidate_view(row) for row in self.store.recent_candidates(mode, 300)],
            "lessons": self.store.recent_lessons(60),
            "model": {
                "bias": self.model.bias,
                "updates": self.model.updates,
                "weights": [
                    {
                        "name": name,
                        "label": label,
                        "weight": self.model.weights.get(name, start),
                        "start": initial.weights[name],
                    }
                    for name, (label, start) in FEATURES.items()
                ],
            },
            "params": [
                {
                    "name": name,
                    "label": PARAM_LABELS.get(name, name),
                    "value": getattr(self.params, name),
                    "default": getattr(defaults, name),
                    "bounds": list(PARAM_BOUNDS[name]),
                }
                for name in PARAM_BOUNDS
            ],
            "sources": [
                {"source": source, "label": source_label(source), **entry}
                for source, entry in sorted(stats.items(), key=lambda item: item[1]["pnl"], reverse=True)
            ],
            "copy_wallets": [
                {
                    "wallet": wallet,
                    "label": source_label(f"copy:{wallet}"),
                    "origin": "ui" if wallet in extra_wallets and wallet not in self.settings.copy_wallets else "env",
                    **stats.get(f"copy:{wallet}", {"trades": 0, "wins": 0, "pnl": 0.0, "win_rate": None, "muted": False}),
                }
                for wallet in self._all_wallets()
            ],
            "blacklist": list(reversed(self._blacklist())),
            "unlock": unlock,
            "events": self.store.recent_events(150),
            "settings": public_meme_settings(self.settings),
            "engine": {
                "last_tick": self.last_tick,
                "last_scan_at": self.last_scan_at,
                "last_scan": self.last_scan,
                "last_error": self.last_error,
                "consecutive_errors": self.consecutive_errors,
                "sol_usd": self._sol_usd[0] or None,
                "wallet": pubkey,
                "wallet_sol": self._wallet_sol if mode == "live" else None,
                "paper_cash": self._paper.cash_usd(0.0),
                "running": bool(self._thread and self._thread.is_alive()),
                "next_scan_in": round(next_scan),
                "scan_seconds": self.settings.scan_seconds,
                "monitor_seconds": self.settings.monitor_seconds,
            },
        }

    def _alerts(self) -> list[dict]:
        alerts: list[dict] = []
        if self.mode == "live":
            alerts.append({"level": "error", "text": f"RIGTIGE PENGE. Botten handler med op til {self.settings.bankroll_usd:.2f} USD fra din wallet."})
        else:
            alerts.append({"level": "info", "text": "Papirhandel: rigtige kurser og rigtige Jupiter-quotes, men ingen rigtige penge."})
        if self.kill_switch:
            alerts.append({"level": "warning", "text": "Nødstop er aktivt. Ingen nye køb."})
        if self.paused:
            alerts.append({"level": "warning", "text": "Nye køb er sat på pause. Åbne positioner passes stadig."})
        today = utc_day()
        if self.store.get_state(f"halt_day:{self.mode}:{today}", "0") == "1":
            alerts.append({"level": "warning", "text": "Dagens tabsgrænse er ramt. Ingen nye køb før i morgen (UTC)."})
        if self.store.get_state(f"halt_dd:{self.mode}", "0") == "1":
            alerts.append({"level": "warning", "text": "Drawdown-grænsen er ramt. Tryk Genoptag, når du har set på det."})
        if not self.settings.anthropic_api_key:
            alerts.append({"level": "info", "text": "Ingen ANTHROPIC_API_KEY: ingen Claude-research af X og nyheder. Botten kører på regler og RugCheck."})
        elif self.settings.ai_configured and self.researcher.budget_left() < 0.1:
            alerts.append({"level": "info", "text": "Dagens AI-budget er brugt. Resten af dagen kører botten kun på regler."})
        if not self.settings.x_configured:
            alerts.append({"level": "info", "text": "Ingen X_BEARER_TOKEN: X-omtale tjekkes kun gennem Claudes websøgning."})
        if self.settings.rpc_url.startswith("https://api.mainnet-beta.solana.com") and (self._all_wallets() or self.mode == "live"):
            alerts.append({"level": "warning", "text": "Offentlig Solana RPC er langsom og rate-limited. Brug en gratis Helius-nøgle i SOLANA_RPC_URL."})
        if self.last_error:
            alerts.append({"level": "error", "text": f"Seneste fejl: {self.last_error}"})
        return alerts

    def _position_view(self, row: dict) -> dict:
        token_raw = max(1, int(row["token_raw"]))
        fraction = int(row["remaining_raw"]) / token_raw
        mark = float(row["cost_usd"]) * fraction * float(row["last_ratio"] or 1.0)
        unrealized = float(row["proceeds_usd"]) + mark - float(row["cost_usd"]) - float(row["fees_usd"])
        ai = loads(row["ai"], None)
        peak = float(row["peak_ratio"] or 1.0)
        trail = None
        if peak >= 1.0 + self.params.trail_activation_pct:
            trail = peak * (1.0 - self.params.trailing_stop_pct) - 1.0
        return {
            "id": row["id"],
            "symbol": row["symbol"],
            "name": row["name"],
            "mint": row["mint"],
            "source": source_label(str(row["source"])),
            "status": row["status"],
            "opened_at": row["opened_at"],
            "closed_at": row["closed_at"],
            "minutes": round(_minutes_since(row["opened_at"])) if row["status"] == "open" else None,
            "cost_usd": row["cost_usd"],
            "fees_usd": row["fees_usd"],
            "proceeds_usd": row["proceeds_usd"],
            "change_pct": (float(row["last_ratio"] or 1.0) - 1.0),
            "peak_pct": (float(row["peak_ratio"] or 1.0) - 1.0),
            "partial": bool(row["partial_taken"]),
            "stop_pct": -self.params.stop_loss_pct,
            "tp_pct": None if row["partial_taken"] else self.params.take_profit_pct,
            "trail_pct": trail,
            "max_hold_minutes": self.params.max_hold_minutes,
            "hold_minutes": _hold_minutes(row) if row["status"] == "closed" else None,
            "mark_usd": mark,
            "unrealized_usd": unrealized if row["status"] == "open" else None,
            "pnl_usd": row["pnl_usd"],
            "pnl_pct": row["pnl_pct"],
            "exit_reason": EXIT_LABELS.get(str(row["exit_reason"] or ""), row["exit_reason"]),
            "score": row["score"],
            "reasoning": row["reasoning"],
            "ai_summary": (ai or {}).get("summary"),
            "pair_url": row["pair_url"],
            "liquidity_usd": row["liquidity_last"],
            "signatures": [sig for sig in (loads(row["signatures"], []) or []) if sig and sig != "papir"],
        }

    def _candidate_view(self, row: dict) -> dict:
        return {
            "id": row["id"],
            "ts": row["ts"],
            "symbol": row["symbol"],
            "mint": row["mint"],
            "source": source_label(str(row["source"] or "")),
            "stage": row["stage"],
            "passed": bool(row["passed"]),
            "reason": row["reason"],
            "score": row["score"],
            "pair_url": row["pair_url"],
        }
