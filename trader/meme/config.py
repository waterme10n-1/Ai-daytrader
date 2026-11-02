"""Indstillinger for memecoin-botten.

Hårde risikogrænser (bankroll, positionsstørrelse, tabsgrænser) kommer kun fra miljøet og
kan aldrig ændres af AI eller læring. Strategiparametre (filtre og exits) må justeres af
læringen, men altid inden for faste grænser i PARAM_BOUNDS.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from dotenv import load_dotenv

from trader.config import ROOT, _flag, _float, _int

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTE_MINTS = frozenset({SOL_MINT, USDC_MINT, USDT_MINT})
LAMPORTS_PER_SOL = 1_000_000_000
BASE_FEE_LAMPORTS = 5_000

MEME_LIVE_PHRASE = "JEG FORSTÅR AT JEG KAN TABE HELE INDSATSEN"

# Krav før rigtige penge. Papirhandler bruger rigtige kurser og rigtige Jupiter-quotes.
UNLOCK_MIN_TRADES = 20
UNLOCK_MIN_DAYS = 3
UNLOCK_MIN_PROFIT_FACTOR = 1.2

DEFAULT_RPC = "https://api.mainnet-beta.solana.com"
DEFAULT_JUPITER = "https://lite-api.jup.ag"
DEXSCREENER_URL = "https://api.dexscreener.com"
RUGCHECK_URL = "https://api.rugcheck.xyz"
X_API_URL = "https://api.x.com"


def _list(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    parts = [item.strip() for item in value.replace("\n", ",").replace(";", ",").split(",")]
    seen: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.append(part)
    return tuple(seen)


@dataclass(frozen=True)
class MemeSettings:
    enabled: bool
    allow_live: bool
    private_key: str
    rpc_url: str
    jupiter_url: str
    jupiter_api_key: str
    x_bearer_token: str
    x_daily_requests: int
    bankroll_usd: float
    position_usd: float
    max_open: int
    max_trades_per_day: int
    daily_loss_pct: float
    max_drawdown_pct: float
    sol_reserve: float
    slippage_bps: int
    priority_fee_lamports: int
    scan_seconds: int
    monitor_seconds: int
    copy_wallets: tuple[str, ...]
    ai_daily_budget_usd: float
    ai_model: str
    ai_effort: str
    ai_web_search_tool: str
    ai_max_search_uses: int
    ai_fallbacks: str
    anthropic_api_key: str
    goal_usd_per_month: float
    db_path: str

    @property
    def wallet_configured(self) -> bool:
        return bool(self.private_key)

    @property
    def ai_configured(self) -> bool:
        return bool(self.anthropic_api_key) and self.ai_daily_budget_usd > 0

    @property
    def x_configured(self) -> bool:
        return bool(self.x_bearer_token)

    @classmethod
    def from_env(cls) -> MemeSettings:
        load_dotenv(ROOT / ".env", override=False)
        main_db = os.getenv("DB_PATH", "trader.db").strip() or "trader.db"
        default_db = str(Path(main_db).expanduser().parent / "meme.db") if main_db != ":memory:" else ":memory:"
        bankroll = max(1.0, _float(os.getenv("MEME_BANKROLL_USD"), 15.0))
        return cls(
            enabled=_flag(os.getenv("MEME_ENABLED"), True),
            allow_live=_flag(os.getenv("MEME_ALLOW_LIVE"), False),
            private_key=os.getenv("SOLANA_PRIVATE_KEY", "").strip(),
            rpc_url=os.getenv("SOLANA_RPC_URL", "").strip() or DEFAULT_RPC,
            jupiter_url=(os.getenv("JUPITER_API_URL", "").strip() or DEFAULT_JUPITER).rstrip("/"),
            jupiter_api_key=os.getenv("JUPITER_API_KEY", "").strip(),
            x_bearer_token=os.getenv("X_BEARER_TOKEN", "").strip(),
            x_daily_requests=max(0, _int(os.getenv("X_DAILY_REQUESTS"), 40)),
            bankroll_usd=bankroll,
            position_usd=min(bankroll, max(0.5, _float(os.getenv("MEME_POSITION_USD"), 3.0))),
            max_open=max(1, min(10, _int(os.getenv("MEME_MAX_OPEN"), 3))),
            max_trades_per_day=max(1, min(50, _int(os.getenv("MEME_MAX_TRADES_PER_DAY"), 8))),
            daily_loss_pct=min(0.9, max(0.05, _float(os.getenv("MEME_DAILY_LOSS_PCT"), 0.25))),
            max_drawdown_pct=min(0.95, max(0.1, _float(os.getenv("MEME_MAX_DRAWDOWN_PCT"), 0.5))),
            sol_reserve=max(0.005, _float(os.getenv("MEME_SOL_RESERVE"), 0.02)),
            slippage_bps=max(50, min(1500, _int(os.getenv("MEME_SLIPPAGE_BPS"), 300))),
            priority_fee_lamports=max(0, min(5_000_000, _int(os.getenv("MEME_PRIORITY_FEE_LAMPORTS"), 100_000))),
            scan_seconds=max(20, _int(os.getenv("MEME_SCAN_SECONDS"), 60)),
            monitor_seconds=max(5, _int(os.getenv("MEME_MONITOR_SECONDS"), 15)),
            copy_wallets=_list(os.getenv("MEME_COPY_WALLETS")),
            ai_daily_budget_usd=max(0.0, _float(os.getenv("MEME_AI_DAILY_BUDGET_USD"), 0.5)),
            ai_model=(os.getenv("MEME_ANTHROPIC_MODEL") or os.getenv("ANTHROPIC_MODEL") or "claude-opus-5-5").strip(),
            ai_effort=(os.getenv("MEME_ANTHROPIC_EFFORT") or "medium").strip(),
            ai_web_search_tool=(
                os.getenv("MEME_ANTHROPIC_WEB_SEARCH_TOOL")
                or os.getenv("ANTHROPIC_WEB_SEARCH_TOOL")
                or "web_search_20260209"
            ).strip(),
            ai_max_search_uses=max(1, min(10, _int(os.getenv("MEME_ANTHROPIC_MAX_SEARCH_USES"), 4))),
            ai_fallbacks=(os.getenv("ANTHROPIC_FALLBACKS") or "default").strip(),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
            goal_usd_per_month=max(1.0, _float(os.getenv("MEME_GOAL_USD_PER_MONTH"), 100.0)),
            db_path=os.getenv("MEME_DB_PATH", "").strip() or default_db,
        )


@dataclass
class StrategyParams:
    """Filtre og exits. Læringen må flytte dem, men kun inden for PARAM_BOUNDS."""

    min_liquidity_usd: float = 25_000.0
    min_volume_h1_usd: float = 10_000.0
    min_txns_h1: int = 60
    min_age_minutes: float = 30.0
    max_age_hours: float = 168.0
    min_mcap_usd: float = 60_000.0
    max_mcap_usd: float = 30_000_000.0
    max_change_h1_pct: float = 250.0
    min_buy_ratio_h1: float = 0.48
    max_top10_pct: float = 35.0
    max_rug_score: float = 0.55
    max_roundtrip_loss_pct: float = 8.0
    min_score: float = 0.55
    stop_loss_pct: float = 0.25
    take_profit_pct: float = 0.6
    partial_fraction: float = 0.5
    trail_activation_pct: float = 0.3
    trailing_stop_pct: float = 0.25
    max_hold_minutes: float = 360.0
    stale_minutes: float = 75.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> StrategyParams:
        params = cls()
        if not data:
            return params
        for item in fields(cls):
            if item.name in data:
                try:
                    value = float(data[item.name])
                except (TypeError, ValueError):
                    continue
                setattr(params, item.name, clamp_param(item.name, value))
        return params


PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "min_liquidity_usd": (10_000.0, 500_000.0),
    "min_volume_h1_usd": (2_000.0, 500_000.0),
    "min_txns_h1": (20, 2_000),
    "min_age_minutes": (10.0, 1_440.0),
    "max_age_hours": (6.0, 720.0),
    "min_mcap_usd": (20_000.0, 2_000_000.0),
    "max_mcap_usd": (500_000.0, 500_000_000.0),
    "max_change_h1_pct": (40.0, 1_000.0),
    "min_buy_ratio_h1": (0.35, 0.75),
    "max_top10_pct": (10.0, 50.0),
    "max_rug_score": (0.2, 0.7),
    "max_roundtrip_loss_pct": (2.0, 15.0),
    "min_score": (0.45, 0.85),
    "stop_loss_pct": (0.1, 0.4),
    "take_profit_pct": (0.2, 3.0),
    "partial_fraction": (0.25, 0.75),
    "trail_activation_pct": (0.1, 1.5),
    "trailing_stop_pct": (0.1, 0.4),
    "max_hold_minutes": (30.0, 2_880.0),
    "stale_minutes": (15.0, 600.0),
}

PARAM_LABELS: dict[str, str] = {
    "min_liquidity_usd": "Min. likviditet (USD)",
    "min_volume_h1_usd": "Min. volumen sidste time (USD)",
    "min_txns_h1": "Min. handler sidste time",
    "min_age_minutes": "Min. alder (min.)",
    "max_age_hours": "Maks. alder (timer)",
    "min_mcap_usd": "Min. markedsværdi (USD)",
    "max_mcap_usd": "Maks. markedsværdi (USD)",
    "max_change_h1_pct": "Maks. stigning sidste time (%)",
    "min_buy_ratio_h1": "Min. andel køb sidste time",
    "max_top10_pct": "Maks. top-10 ejerandel (%)",
    "max_rug_score": "Maks. rug-risiko (0-1)",
    "max_roundtrip_loss_pct": "Maks. tab ved køb+salg (%)",
    "min_score": "Min. score for køb",
    "stop_loss_pct": "Stop-loss",
    "take_profit_pct": "Delvis gevinst ved",
    "partial_fraction": "Andel solgt ved delvis gevinst",
    "trail_activation_pct": "Trailing stop aktiveres ved",
    "trailing_stop_pct": "Trailing stop fra top",
    "max_hold_minutes": "Maks. holdetid (min.)",
    "stale_minutes": "Sælg hvis intet sker efter (min.)",
}

INT_PARAMS = {"min_txns_h1"}


def clamp_param(name: str, value: float) -> float:
    low, high = PARAM_BOUNDS.get(name, (value, value))
    clamped = min(high, max(low, value))
    if name in INT_PARAMS:
        return int(round(clamped))
    return clamped


def public_meme_settings(settings: MemeSettings) -> dict:
    """Til dashboardet. Ingen hemmeligheder."""
    return {
        "bankroll_usd": settings.bankroll_usd,
        "position_usd": settings.position_usd,
        "max_open": settings.max_open,
        "max_trades_per_day": settings.max_trades_per_day,
        "daily_loss_pct": settings.daily_loss_pct,
        "max_drawdown_pct": settings.max_drawdown_pct,
        "sol_reserve": settings.sol_reserve,
        "slippage_bps": settings.slippage_bps,
        "priority_fee_lamports": settings.priority_fee_lamports,
        "scan_seconds": settings.scan_seconds,
        "monitor_seconds": settings.monitor_seconds,
        "copy_wallets": list(settings.copy_wallets),
        "ai_configured": settings.ai_configured,
        "ai_model": settings.ai_model,
        "ai_effort": settings.ai_effort,
        "ai_daily_budget_usd": settings.ai_daily_budget_usd,
        "ai_web_search_tool": settings.ai_web_search_tool,
        "x_configured": settings.x_configured,
        "x_daily_requests": settings.x_daily_requests,
        "wallet_configured": settings.wallet_configured,
        "allow_live": settings.allow_live,
        "rpc_is_public": settings.rpc_url == DEFAULT_RPC,
        "jupiter_url": settings.jupiter_url,
        "goal_usd_per_month": settings.goal_usd_per_month,
        "confirmation_phrase": MEME_LIVE_PHRASE,
        "unlock_min_trades": UNLOCK_MIN_TRADES,
        "unlock_min_days": UNLOCK_MIN_DAYS,
        "unlock_min_profit_factor": UNLOCK_MIN_PROFIT_FACTOR,
    }
