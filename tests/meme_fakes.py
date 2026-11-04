"""Falske datakilder til memebot-tests. Ingen netværk."""

from __future__ import annotations

import dataclasses
import time

from trader.meme.clients import Quote, SourceError, best_pairs, parse_rug_report
from trader.meme.config import LAMPORTS_PER_SOL, SOL_MINT, MemeSettings
from trader.meme.engine import MemeClients, MemeEngine
from trader.meme.store import MemeStore

DECIMALS = 6


def raw_pair(
    mint: str,
    *,
    symbol: str = "MEME",
    price: float = 0.001,
    liq: float = 120_000,
    vol_h1: float = 90_000,
    change_h1: float = 35.0,
    change_m5: float = 3.0,
    buys: int = 420,
    sells: int = 230,
    age_min: float = 300,
    mcap: float = 900_000,
    dex: str = "raydium",
) -> dict:
    now_ms = int(time.time() * 1000)
    return {
        "chainId": "solana",
        "dexId": dex,
        "url": f"https://dexscreener.com/solana/{mint}",
        "pairAddress": f"pair-{mint}",
        "baseToken": {"address": mint, "name": symbol.title(), "symbol": symbol},
        "quoteToken": {"address": SOL_MINT, "name": "Wrapped SOL", "symbol": "SOL"},
        "priceUsd": str(price),
        "txns": {
            "m5": {"buys": 40, "sells": 20},
            "h1": {"buys": buys, "sells": sells},
            "h6": {"buys": buys * 4, "sells": sells * 4},
            "h24": {"buys": buys * 10, "sells": sells * 10},
        },
        "volume": {"m5": vol_h1 / 10, "h1": vol_h1, "h6": vol_h1 * 4, "h24": vol_h1 * 10},
        "priceChange": {"m5": change_m5, "h1": change_h1, "h6": change_h1 * 1.5, "h24": change_h1 * 2},
        "liquidity": {"usd": liq},
        "fdv": mcap,
        "marketCap": mcap,
        "pairCreatedAt": now_ms - int(age_min * 60_000),
        "info": {
            "websites": [{"label": "Website", "url": "https://meme.example"}],
            "socials": [
                {"type": "twitter", "url": "https://x.com/meme"},
                {"type": "telegram", "url": "https://t.me/meme"},
            ],
        },
    }


def safe_report(**overrides) -> dict:
    data = {
        "score": 120,
        "score_normalised": 5,
        "risks": [],
        "mintAuthority": None,
        "freezeAuthority": None,
        "topHolders": [{"address": f"holder{i}", "owner": f"owner{i}", "pct": 1.5} for i in range(12)],
        "totalHolders": 3200,
        "markets": [{"lp": {"lpLockedPct": 100}}],
        "token": {"decimals": DECIMALS},
    }
    data.update(overrides)
    return data


class FakeDex:
    def __init__(self) -> None:
        self.pairs: dict[str, dict] = {}
        self.boosts: list[str] = []
        self.fail = False

    def set_price(self, mint: str, price: float, **changes) -> None:
        self.pairs[mint]["priceUsd"] = str(price)
        if "liq" in changes:
            self.pairs[mint]["liquidity"]["usd"] = changes["liq"]

    def latest_boosts(self) -> list[dict]:
        return [{"chainId": "solana", "tokenAddress": mint} for mint in self.boosts]

    def top_boosts(self) -> list[dict]:
        return []

    def latest_profiles(self) -> list[dict]:
        return []

    def pairs_for(self, mints):
        if self.fail:
            raise SourceError("DexScreener nede")
        return best_pairs([self.pairs[m] for m in mints if m in self.pairs])

    def close(self) -> None:
        pass


class FakeRug:
    def __init__(self) -> None:
        self.reports: dict[str, dict] = {}

    def report(self, mint: str, max_age: float = 600.0):
        if mint not in self.reports:
            raise SourceError("ingen rapport")
        return parse_rug_report(mint, self.reports[mint])

    def close(self) -> None:
        pass


class FakeJup:
    """Quotes ud fra DexScreener-prisen med 1 % tab hver vej."""

    def __init__(self, dex: FakeDex, sol_usd: float = 150.0) -> None:
        self.dex = dex
        self.sol = sol_usd
        self.no_route: set[str] = set()
        self.quotes = 0
        self.recent: list = []

    def sol_usd(self, **_kw) -> float:
        return self.sol

    def prices(self, mints, **_kw):
        return {}

    def top_tokens(self, category, interval="1h", limit=50, **_kw):
        return []

    def token_infos(self, mints, **_kw):
        return {token.mint: token for token in self.recent if token.mint in mints}

    def recent_tokens(self, limit=60, **_kw):
        return list(self.recent)

    def _price(self, mint: str) -> float:
        return float(self.dex.pairs[mint]["priceUsd"])

    def quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps=None, **_kw) -> Quote:
        self.quotes += 1
        token = output_mint if input_mint == SOL_MINT else input_mint
        if token in self.no_route:
            raise SourceError("Jupiter fandt ingen rute")
        if input_mint == SOL_MINT:
            usd = amount / LAMPORTS_PER_SOL * self.sol
            out = int(usd / self._price(output_mint) * 10**DECIMALS * 0.99)
        else:
            usd = amount / 10**DECIMALS * self._price(input_mint) * 0.99
            out = int(usd / self.sol * LAMPORTS_PER_SOL)
        return Quote(input_mint, output_mint, amount, out, 0.5, {"outAmount": str(out)})

    def close(self) -> None:
        pass


def settings(**overrides) -> MemeSettings:
    base = MemeSettings.from_env()
    values = dict(
        enabled=True,
        allow_live=False,
        private_key="",
        rpc_url="https://rpc.example",
        anthropic_api_key="",
        x_bearer_token="",
        copy_wallets=(),
        bankroll_usd=15.0,
        position_usd=3.0,
        max_open=3,
        max_trades_per_day=8,
        daily_loss_pct=0.25,
        max_drawdown_pct=0.5,
        db_path=":memory:",
        paper_fail_rate=0.0,
        paper_slippage_pct=0.0,
        paper_sniper_extra_pct=0.0,
        report_hour_utc=-1,
        dev_watch=False,
        shadows=False,
    )
    values.update(overrides)
    return dataclasses.replace(base, **values)


def make_engine(cfg: MemeSettings | None = None, *, rpc=None, researcher=None):
    cfg = cfg or settings()
    dex = FakeDex()
    rug = FakeRug()
    jup = FakeJup(dex)
    store = MemeStore(":memory:")
    notes: list[str] = []
    clients = MemeClients(cfg, dex=dex, rug=rug, jup=jup, x=None, rpc=rpc)
    engine = MemeEngine(cfg, store=store, clients=clients, researcher=researcher, notifier=notes.append)
    return engine, dex, rug, jup, notes
