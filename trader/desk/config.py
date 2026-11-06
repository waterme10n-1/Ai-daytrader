"""Indstillinger for handelsbordet. Hårde lofter håndhæves her, så ingen variabel kan løsne dem."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from trader.config import ROOT, _flag, _float, _int

DEFAULT_WATCHLIST: tuple[str, ...] = (
    "NVDA",
    "AAPL",
    "MSFT",
    "AMZN",
    "META",
    "GOOGL",
    "TSLA",
    "AMD",
    "AVGO",
    "NFLX",
    "PLTR",
    "COIN",
    # Mere livlige navne, der oftere flytter sig 2 % på en time.
    "SMCI",
    "MSTR",
    "SOFI",
    "RIVN",
)
# Standarder fra før, der blev gemt på siden, når man trykkede Gem. De flyttes til de nye standarder ved opstart.
RETIRED_DEFAULTS: dict[str, object] = {
    "move_pct": 3.0,
    "max_flags_per_day": 5,
    "watchlist": ["NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA", "AMD", "AVGO", "NFLX", "PLTR", "COIN"],
}
TICKER = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
MAX_WATCHLIST = 30

# Lofter, som hverken .env eller dashboardet kan gå ud over.
HARD_MAX_POSITION_PCT = 10.0
HARD_MAX_OPEN = 5
HARD_MAX_STOP_PCT = 5.0
HARD_MIN_REWARD_RISK = 1.5
HARD_MAX_FLAGS = 10


def clean_key(value: str | None) -> str:
    """Nøgler fra Railway uden anførselstegn, mellemrum og et indsat 'NAVN=' foran."""
    text = (value or "").strip()
    if "=" in text and text.split("=", 1)[0].strip().upper().startswith(("DESK_", "ALPACA_", "APCA")):
        text = text.split("=", 1)[1].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    return "".join(text.split())


def key_problem(api_key: str) -> str:
    """En tydelig dansk forklaring, hvis nøglen åbenlyst er forkert. Tom streng, hvis den ser rigtig ud."""
    if not api_key:
        return ""
    if api_key.upper().startswith("AK"):
        return "DESK_ALPACA_API_KEY starter med AK. Det er en live-nøgle. Bordet skal bruge papirnøgler, som starter med PK."
    if not api_key.upper().startswith("PK"):
        return "DESK_ALPACA_API_KEY starter ikke med PK. Måske er Key og Secret byttet om."
    return ""


def parse_watchlist(raw: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Store bogstaver, ingen dubletter, kun gyldige tickers. Ugyldige ignoreres."""
    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple)) else re.split(r"[\s,;]+", raw)
    seen: list[str] = []
    for item in items:
        ticker = str(item).strip().upper().lstrip("$")
        if ticker and TICKER.match(ticker) and ticker not in seen:
            seen.append(ticker)
    return seen[:MAX_WATCHLIST]


@dataclass(frozen=True)
class DeskSettings:
    enabled: bool = True
    watchlist: tuple[str, ...] = DEFAULT_WATCHLIST
    move_pct: float = 2.0
    volume_x: float = 2.0
    max_flags_per_day: int = 8
    premarket: bool = True  # SCOUT og NEWS kigger på gaps før åbning (4:00-9:30 ET, 10:00-15:30 dansk tid)
    gap_pct: float = 3.0  # så meget skal en aktie være steget siden i går for at tælle som gap
    stop_max_pct: float = 2.0
    reward_risk: float = 2.0
    position_pct: float = 5.0
    max_open: int = 3
    daily_loss_pct: float = 2.0
    approval_minutes: int = 10
    scan_minutes: int = 1
    auto_approve: bool = True
    max_chase_pct: float = 0.5
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    anthropic_api_key: str = ""
    key_source: str = ""
    secret_source: str = ""
    ai_daily_budget_usd: float = 0.0  # 0 = intet dagligt loft
    ai_model: str = "claude-opus-5-5"
    ai_effort: str = "high"
    ai_web_search_tool: str = "web_search_20260209"
    ai_max_search_uses: int = 4
    ai_fallbacks: str = "default"
    max_chat_per_day: int = 40
    log_time_et: str = "16:15"
    db_path: str = "desk.db"

    @property
    def own_keys(self) -> bool:
        return bool(self.alpaca_api_key and self.alpaca_secret_key)

    @classmethod
    def from_env(cls) -> DeskSettings:
        load_dotenv(ROOT / ".env", override=False)
        main_db = os.getenv("DB_PATH", "trader.db").strip() or "trader.db"
        default_db = str(Path(main_db).expanduser().parent / "desk.db") if main_db != ":memory:" else ":memory:"
        watch = parse_watchlist(os.getenv("DESK_WATCHLIST")) or list(DEFAULT_WATCHLIST)
        return cls(
            enabled=_flag(os.getenv("DESK_ENABLED"), True),
            watchlist=tuple(watch),
            move_pct=min(20.0, max(0.5, _float(os.getenv("DESK_MOVE_PCT"), 2.0))),
            volume_x=min(20.0, max(1.2, _float(os.getenv("DESK_VOLUME_X"), 2.0))),
            max_flags_per_day=max(1, min(HARD_MAX_FLAGS, _int(os.getenv("DESK_MAX_FLAGS_PER_DAY"), 8))),
            premarket=_flag(os.getenv("DESK_PREMARKET"), True),
            gap_pct=min(20.0, max(1.0, _float(os.getenv("DESK_GAP_PCT"), 3.0))),
            stop_max_pct=min(HARD_MAX_STOP_PCT, max(0.3, _float(os.getenv("DESK_STOP_MAX_PCT"), 2.0))),
            reward_risk=min(5.0, max(HARD_MIN_REWARD_RISK, _float(os.getenv("DESK_REWARD_RISK"), 2.0))),
            position_pct=min(HARD_MAX_POSITION_PCT, max(0.5, _float(os.getenv("DESK_POSITION_PCT"), 5.0))),
            max_open=max(1, min(HARD_MAX_OPEN, _int(os.getenv("DESK_MAX_OPEN"), 3))),
            daily_loss_pct=min(5.0, max(0.5, _float(os.getenv("DESK_DAILY_LOSS_PCT"), 2.0))),
            approval_minutes=max(2, min(60, _int(os.getenv("DESK_APPROVAL_MINUTES"), 10))),
            scan_minutes=max(1, min(30, _int(os.getenv("DESK_SCAN_MINUTES"), 1))),
            auto_approve=_flag(os.getenv("DESK_AUTO_APPROVE"), True),
            max_chase_pct=min(2.0, max(0.1, _float(os.getenv("DESK_MAX_CHASE_PCT"), 0.5))),
            # Egen papirkonto til bordet. ALPACA_API_KEY bruges, hvis du allerede har den sat.
            alpaca_api_key=clean_key(os.getenv("DESK_ALPACA_API_KEY") or os.getenv("ALPACA_API_KEY")),
            alpaca_secret_key=clean_key(os.getenv("DESK_ALPACA_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY")),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
            key_source="DESK_ALPACA_API_KEY" if clean_key(os.getenv("DESK_ALPACA_API_KEY")) else ("ALPACA_API_KEY" if clean_key(os.getenv("ALPACA_API_KEY")) else ""),
            secret_source="DESK_ALPACA_SECRET_KEY" if clean_key(os.getenv("DESK_ALPACA_SECRET_KEY")) else ("ALPACA_SECRET_KEY" if clean_key(os.getenv("ALPACA_SECRET_KEY")) else ""),
            ai_daily_budget_usd=max(0.0, _float(os.getenv("DESK_AI_DAILY_BUDGET_USD"), 0.0)),
            ai_model=(os.getenv("DESK_ANTHROPIC_MODEL") or os.getenv("ANTHROPIC_MODEL") or "claude-opus-5-5").strip(),
            ai_effort=(os.getenv("DESK_ANTHROPIC_EFFORT") or "high").strip(),
            ai_web_search_tool=(os.getenv("ANTHROPIC_WEB_SEARCH_TOOL") or "web_search_20260209").strip(),
            ai_max_search_uses=max(1, min(10, _int(os.getenv("DESK_ANTHROPIC_MAX_SEARCH_USES"), 4))),
            ai_fallbacks=(os.getenv("ANTHROPIC_FALLBACKS") or "default").strip(),
            max_chat_per_day=max(0, min(500, _int(os.getenv("DESK_MAX_CHAT_PER_DAY"), 40))),
            db_path=os.getenv("DESK_DB_PATH", "").strip() or default_db,
        )
