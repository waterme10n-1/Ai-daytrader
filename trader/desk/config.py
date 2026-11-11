"""Indstillinger for handelsbordet. Hårde lofter håndhæves her, så ingen variabel kan løsne dem."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
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
# Gamle standarder for én børs: nøgle → (indstilling, gammel værdi). Nøglen skal være ny for hver ændring.
RETIRED_BY_MARKET: dict[str, dict[str, tuple[str, object]]] = {
    "us": {
        "us_move_pct_2": ("move_pct", 2.0),  # New York: 2 % blev til 1,5 %, så flere bevægelser kommer med
        # Flere aktier igennem: 1,5 % blev til 1 %, gaps fra 3 % til 2 %, og 8 markeringer om dagen til 10.
        "us_move_pct_15": ("move_pct", 1.5),
        "us_gap_pct_3": ("gap_pct", 3.0),
        "us_flags_8": ("max_flags_per_day", 8),
    },
    "dk": {
        "dk_move_pct_2": ("move_pct", 2.0),  # København: 2 % blev til 1,5 %
        "dk_move_pct_15": ("move_pct", 1.5),  # og siden til 1 %
        "dk_gap_pct_3": ("gap_pct", 3.0),
        "dk_flags_8": ("max_flags_per_day", 8),
        "dk_scan_minutes_1": ("scan_minutes", 1),  # København: hvert minut blev til hvert 5. (skånsomt for Yahoo)
    },
}
TICKER = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
# Københavns børs: NOVO-B, MAERSK-B, NDA-DK, GN, ROCK-B (som hos Yahoo, uden .CO).
DK_TICKER = re.compile(r"^(?=[A-Z0-9-]*[A-Z])[A-Z0-9]{1,8}(-[A-Z]{1,3})?$")
MAX_WATCHLIST = 30
# Likvide C25-aktier, der flytter sig nok til at være interessante, og som koster under 5 % af kontoen pr. aktie.
DK_WATCHLIST: tuple[str, ...] = (
    "NOVO-B",
    "DSV",
    "DANSKE",
    "ORSTED",
    "VWS",
    "GMAB",
    "PNDORA",
    "CARL-B",
    "COLO-B",
    "NSIS-B",
    "DEMANT",
    "GN",
    "NKT",
    "BAVA",
    "ZEAL",
    "AMBU-B",
)

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


def parse_watchlist(raw: str | list[str] | tuple[str, ...] | None, market: str = "us") -> list[str]:
    """Store bogstaver, ingen dubletter, kun gyldige tickers. Ugyldige ignoreres."""
    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple)) else re.split(r"[\s,;]+", raw)
    pattern = DK_TICKER if market == "dk" else TICKER
    seen: list[str] = []
    for item in items:
        ticker = str(item).strip().upper().lstrip("$")
        if market == "dk":
            ticker = ticker.removesuffix(".CO").replace(".", "-").replace("_", "-")
        if ticker and pattern.match(ticker) and ticker not in seen:
            seen.append(ticker)
    return seen[:MAX_WATCHLIST]


@dataclass(frozen=True)
class DeskSettings:
    enabled: bool = True
    market: str = "us"  # "us" = New York via Alpaca, "dk" = København på bordets egen papirkonto
    start_cash: float = 100_000.0  # kun den danske papirkonto (DKK)
    fee_pct: float = 0.1  # kurtage pr. handel på den danske papirkonto, i % af beløbet
    watchlist: tuple[str, ...] = DEFAULT_WATCHLIST
    move_pct: float = 1.0
    volume_x: float = 2.0
    max_flags_per_day: int = 10
    premarket: bool = True  # SCOUT og NEWS kigger på gaps før åbning (4:00-9:30 ET, 10:00-15:30 dansk tid)
    gap_pct: float = 2.0  # så meget skal en aktie være steget siden i går for at tælle som gap
    stop_max_pct: float = 2.0
    reward_risk: float = 2.0
    position_pct: float = 5.0
    max_open: int = 3
    daily_loss_pct: float = 2.0
    approval_minutes: int = 10
    scan_minutes: int = 1
    auto_approve: bool = True
    # Prøvekøb: siger SKEPTIC WAIT til en handel med en klar grund og en ren opstilling, køber bordet halv størrelse
    # alligevel. Så kommer der flere handler på papir, og bordet lærer af rigtige resultater i stedet for kun skygger.
    trial_buys: bool = True
    market_filter_pct: float = 1.0  # er indekset ned så meget siden åbningen, køber bordet ingen nye aktier (0 = fra)
    time_stop_min: int = 90  # en handel, der stadig ligger under købskursen efter så mange minutter, lukkes (0 = fra)
    max_losses_per_day: int = 2  # efter så mange handler med tab i dag køber bordet ikke mere den dag (0 = fra)
    max_chase_pct: float = 0.5
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    anthropic_api_key: str = ""
    key_source: str = ""
    secret_source: str = ""
    ai_daily_budget_usd: float = 0.0  # 0 = intet dagligt loft
    ai_model: str = "claude-haiku-5-5"  # ca. 40 gange billigere end Opus; følg "Nej med rette" under Læring
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
            move_pct=min(20.0, max(0.5, _float(os.getenv("DESK_MOVE_PCT"), 1.0))),
            volume_x=min(20.0, max(1.2, _float(os.getenv("DESK_VOLUME_X"), 2.0))),
            max_flags_per_day=max(1, min(HARD_MAX_FLAGS, _int(os.getenv("DESK_MAX_FLAGS_PER_DAY"), 10))),
            premarket=_flag(os.getenv("DESK_PREMARKET"), True),
            gap_pct=min(20.0, max(1.0, _float(os.getenv("DESK_GAP_PCT"), 2.0))),
            stop_max_pct=min(HARD_MAX_STOP_PCT, max(0.3, _float(os.getenv("DESK_STOP_MAX_PCT"), 2.0))),
            reward_risk=min(5.0, max(HARD_MIN_REWARD_RISK, _float(os.getenv("DESK_REWARD_RISK"), 2.0))),
            position_pct=min(HARD_MAX_POSITION_PCT, max(0.5, _float(os.getenv("DESK_POSITION_PCT"), 5.0))),
            max_open=max(1, min(HARD_MAX_OPEN, _int(os.getenv("DESK_MAX_OPEN"), 3))),
            daily_loss_pct=min(5.0, max(0.5, _float(os.getenv("DESK_DAILY_LOSS_PCT"), 2.0))),
            approval_minutes=max(2, min(60, _int(os.getenv("DESK_APPROVAL_MINUTES"), 10))),
            scan_minutes=max(1, min(30, _int(os.getenv("DESK_SCAN_MINUTES"), 1))),
            auto_approve=_flag(os.getenv("DESK_AUTO_APPROVE"), True),
            trial_buys=_flag(os.getenv("DESK_TRIAL_BUYS"), True),
            market_filter_pct=min(5.0, max(0.0, _float(os.getenv("DESK_MARKET_FILTER_PCT"), 1.0))),
            time_stop_min=max(0, min(390, _int(os.getenv("DESK_TIME_STOP_MIN"), 90))),
            max_losses_per_day=max(0, min(20, _int(os.getenv("DESK_MAX_LOSSES_PER_DAY"), 2))),
            max_chase_pct=min(2.0, max(0.1, _float(os.getenv("DESK_MAX_CHASE_PCT"), 0.5))),
            # Egen papirkonto til bordet. ALPACA_API_KEY bruges, hvis du allerede har den sat.
            alpaca_api_key=clean_key(os.getenv("DESK_ALPACA_API_KEY") or os.getenv("ALPACA_API_KEY")),
            alpaca_secret_key=clean_key(os.getenv("DESK_ALPACA_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY")),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
            key_source="DESK_ALPACA_API_KEY" if clean_key(os.getenv("DESK_ALPACA_API_KEY")) else ("ALPACA_API_KEY" if clean_key(os.getenv("ALPACA_API_KEY")) else ""),
            secret_source="DESK_ALPACA_SECRET_KEY" if clean_key(os.getenv("DESK_ALPACA_SECRET_KEY")) else ("ALPACA_SECRET_KEY" if clean_key(os.getenv("ALPACA_SECRET_KEY")) else ""),
            ai_daily_budget_usd=max(0.0, _float(os.getenv("DESK_AI_DAILY_BUDGET_USD"), 0.0)),
            ai_model=(os.getenv("DESK_ANTHROPIC_MODEL") or os.getenv("ANTHROPIC_MODEL") or "claude-haiku-5-5").strip(),
            ai_effort=(os.getenv("DESK_ANTHROPIC_EFFORT") or "high").strip(),
            ai_web_search_tool=(os.getenv("ANTHROPIC_WEB_SEARCH_TOOL") or "web_search_20260209").strip(),
            ai_max_search_uses=max(1, min(10, _int(os.getenv("DESK_ANTHROPIC_MAX_SEARCH_USES"), 4))),
            ai_fallbacks=(os.getenv("ANTHROPIC_FALLBACKS") or "default").strip(),
            max_chat_per_day=max(0, min(500, _int(os.getenv("DESK_MAX_CHAT_PER_DAY"), 40))),
            db_path=os.getenv("DESK_DB_PATH", "").strip() or default_db,
        )

    @classmethod
    def from_env_dk(cls) -> DeskSettings:
        """Københavns børs: samme regler for SCOUT, CHART og SIZER, men bordets egen papirkonto i kroner."""
        base = cls.from_env()
        default_db = str(Path(base.db_path).expanduser().parent / "desk_dk.db") if base.db_path != ":memory:" else ":memory:"
        watch = parse_watchlist(os.getenv("DESK_DK_WATCHLIST"), "dk") or list(DK_WATCHLIST)
        return replace(
            base,
            enabled=_flag(os.getenv("DESK_DK_ENABLED"), False),  # fra, indtil Yahoo-kurserne virker stabilt
            market="dk",
            watchlist=tuple(watch),
            # Store danske aktier flytter sig mindre end de livlige amerikanske, så grænsen er lavere.
            move_pct=min(20.0, max(0.5, _float(os.getenv("DESK_DK_MOVE_PCT"), 1.0))),
            # Yahoo er gratis, men siger 429, hvis bordet spørger for tit. Hvert 5. minut er nok til bevægelser på en time.
            scan_minutes=max(1, min(30, _int(os.getenv("DESK_DK_SCAN_MINUTES"), 5))),
            start_cash=min(10_000_000.0, max(10_000.0, _float(os.getenv("DESK_DK_START_CASH"), 100_000.0))),
            fee_pct=min(1.0, max(0.0, _float(os.getenv("DESK_DK_FEE_PCT"), 0.1))),
            alpaca_api_key="",
            alpaca_secret_key="",
            key_source="",
            secret_source="",
            db_path=os.getenv("DESK_DK_DB_PATH", "").strip() or default_db,
        )
