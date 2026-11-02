"""Indstillinger fra miljøet. Hemmeligheder læses kun herfra, aldrig fra koden."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
ET_NAME = "America/New_York"

UNIVERSE: tuple[str, ...] = (
    "SPY",
    "QQQ",
    "IWM",
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "META",
    "GOOGL",
    "TSLA",
    "AMD",
)
MARKET_PROXY = "SPY"

LIVE_CONFIRMATION_PHRASE = "JEG FORSTÅR AT JEG KAN TABE RIGTIGE PENGE"

RISK_PER_TRADE = 0.005
MAX_DAILY_LOSS = 0.02
MAX_TRADES_PER_DAY = 4
MAX_OPEN_POSITIONS = 2
MIN_CONFIDENCE = 65
MIN_REWARD_RISK = 1.5
MAX_NOTIONAL_PCT = 0.25
BUYING_POWER_PCT = 0.90
PDT_EQUITY = 25_000.0
PDT_MAX_DAYTRADES = 3
RULES_CONFIDENCE = 70
SIM_START_EQUITY = 100_000.0


def _flag(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "ja", "on"}


def _float(value: str | None, default: float) -> float:
    if value is None or value.strip() == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _int(value: str | None, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_live_api_key: str
    alpaca_live_secret_key: str
    allow_live: bool
    live_max_capital: float
    anthropic_api_key: str
    anthropic_model: str
    anthropic_web_search_tool: str
    anthropic_effort: str
    anthropic_max_search_uses: int
    anthropic_fallbacks: str
    telegram_bot_token: str
    telegram_chat_id: str
    db_path: str
    port: int
    host: str = "127.0.0.1"
    app_username: str = ""
    app_password: str = ""
    app_password_hash: str = ""
    session_secret: str = ""

    @property
    def paper_keys_present(self) -> bool:
        return bool(self.alpaca_api_key and self.alpaca_secret_key)

    @property
    def live_keys_present(self) -> bool:
        return bool(self.alpaca_live_api_key and self.alpaca_live_secret_key)

    @property
    def ai_configured(self) -> bool:
        return bool(self.anthropic_api_key)

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv(ROOT / ".env", override=False)
        return cls(
            alpaca_api_key=os.getenv("ALPACA_API_KEY", "").strip(),
            alpaca_secret_key=os.getenv("ALPACA_SECRET_KEY", "").strip(),
            alpaca_live_api_key=os.getenv("ALPACA_LIVE_API_KEY", "").strip(),
            alpaca_live_secret_key=os.getenv("ALPACA_LIVE_SECRET_KEY", "").strip(),
            allow_live=_flag(os.getenv("ALLOW_LIVE"), False),
            live_max_capital=_float(os.getenv("LIVE_MAX_CAPITAL"), 1000.0),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5").strip() or "claude-opus-5-5",
            anthropic_web_search_tool=os.getenv("ANTHROPIC_WEB_SEARCH_TOOL", "web_search_20260209").strip()
            or "web_search_20260209",
            anthropic_effort=os.getenv("ANTHROPIC_EFFORT", "high").strip() or "high",
            anthropic_max_search_uses=_int(os.getenv("ANTHROPIC_MAX_SEARCH_USES"), 5),
            anthropic_fallbacks=os.getenv("ANTHROPIC_FALLBACKS", "default").strip() or "default",
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            db_path=os.getenv("DB_PATH", "trader.db").strip() or "trader.db",
            port=_int(os.getenv("PORT"), 8741),
            host=os.getenv("HOST", "127.0.0.1").strip() or "127.0.0.1",
            app_username=os.getenv("APP_USERNAME", "").strip(),
            app_password=os.getenv("APP_PASSWORD", ""),
            app_password_hash=os.getenv("APP_PASSWORD_HASH", "").strip(),
            session_secret=os.getenv("SESSION_SECRET", "").strip(),
        )


def public_settings(settings: Settings) -> dict:
    """Read-only view for the dashboard. No secrets."""
    return {
        "universe": list(UNIVERSE),
        "market_proxy": MARKET_PROXY,
        "data_feed": "iex",
        "paper_endpoint": "https://paper-api.alpaca.markets",
        "live_endpoint": "https://api.alpaca.markets",
        "data_endpoint": "https://data.alpaca.markets",
        "risk_per_trade_pct": RISK_PER_TRADE,
        "max_daily_loss_pct": MAX_DAILY_LOSS,
        "max_trades_per_day": MAX_TRADES_PER_DAY,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "min_confidence": MIN_CONFIDENCE,
        "min_reward_risk": MIN_REWARD_RISK,
        "max_notional_pct": MAX_NOTIONAL_PCT,
        "buying_power_pct": BUYING_POWER_PCT,
        "pdt_equity": PDT_EQUITY,
        "pdt_max_daytrades": PDT_MAX_DAYTRADES,
        "model": settings.anthropic_model,
        "web_search_tool": settings.anthropic_web_search_tool,
        "effort": settings.anthropic_effort,
        "fallbacks": settings.anthropic_fallbacks,
        "max_search_uses": settings.anthropic_max_search_uses,
        "allow_live": settings.allow_live,
        "live_max_capital": settings.live_max_capital,
        "paper_keys_present": settings.paper_keys_present,
        "live_keys_present": settings.live_keys_present,
        "ai_configured": settings.ai_configured,
        "telegram_configured": bool(settings.telegram_bot_token and settings.telegram_chat_id),
        "open_window": "09:45–10:30 ET",
        "close_window": "15:00–15:30 ET",
        "flatten_time": "15:50 ET",
        "confirmation_phrase": LIVE_CONFIRMATION_PHRASE,
    }
