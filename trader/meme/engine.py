"""Memecoin-motoren. Kører 24/7 i sin egen tråd.

Hver runde (MEME_MONITOR_SECONDS): tjek åbne positioner og sælg efter reglerne.
Hver scanning (MEME_SCAN_SECONDS): find kandidater, filtrér, rug-tjek, score, spørg Claude
(hvis Claude er sat op), risikotjek og køb. Efter hver lukket handel lærer botten.
"""

from __future__ import annotations

import calendar
import dataclasses
import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from trader.config import Settings
from trader.meme.clients import (
    DexScreener,
    Jupiter,
    PairInfo,
    RateLimited,
    RugCheck,
    SolanaRPC,
    SolanaTracker,
    SourceError,
    TokenInfo,
    XClient,
)
from trader.meme.config import (
    LAMPORTS_PER_SOL,
    MEME_LIVE_PHRASE,
    PARAM_BOUNDS,
    PARAM_EXITS,
    PARAM_KINDS,
    PARAM_LABELS,
    QUOTE_MINTS,
    SNIPER_BOOLS,
    SNIPER_BOUNDS,
    SNIPER_LABELS,
    SNIPER_LEARNABLE,
    SOL_MINT,
    TOP_BOOLS,
    TOP_BOUNDS,
    TOP_LABELS,
    TOP_TRAIL,
    TOP_TRAIL_ACTIVATION,
    UNLOCK_MIN_DAYS,
    UNLOCK_MIN_PROFIT_FACTOR,
    UNLOCK_MIN_TRADES,
    MemeSettings,
    SniperSettings,
    StrategyParams,
    TopTraderSettings,
    clamp_param,
    clamp_sniper,
    clamp_top,
    public_meme_settings,
    sniper_from,
    top_from,
)
from trader.meme.copytrade import CopyTracker
from trader.meme.learning import (
    EXIT_LABELS,
    FEATURES,
    WIN_THRESHOLD_PCT,
    Model,
    adapt_params,
    adapt_sniper,
    build_features,
    explain_trade,
    MOMENTUM_STOP_GRID,
    MOMENTUM_TARGET_GRID,
    FACT_MIN_ROWS,
    TIGHTEN_RULES,
    fact_report,
    facts_of,
    optimize_targets,
    tighten_suggestion,
    shadow_summary,
    simulate_path,
    verdict_for,
    source_stats,
    trade_lesson,
)
from trader.meme.research import MemeResearcher, Verdict
from trader.meme.risk import (
    MOONBAG_TRAIL,
    SOL_BASELINE_MAX_SECONDS,
    EntryContext,
    daily_loss_hit,
    drawdown_hit,
    evaluate_entry,
    exit_decision,
    losses_halt,
    sniper_exit_decision,
    sol_drop_pct,
    sol_halt,
    top_exit_decision,
)
from trader.meme.safety import (
    BONDING_CURVE_DEXES,
    insider_rejections,
    market_rejections,
    roundtrip_rejections,
    rug_rejections,
    sniper_rejections,
    token_quality_rejections,
)
from trader.meme.store import MemeStore, loads, now_iso, utc_day
from trader.meme.wallet import MAX_DEPOSIT_SOL, PaperWallet, SolanaWallet, b58encode, build_transfer_message, fee_sol, load_keypair

logger = logging.getLogger("trader.meme.engine")

CANDIDATE_COOLDOWN_MINUTES = 20
LOSS_COOLDOWN_HOURS = 12
MAX_DEEP_CHECKS = 6
MAX_BUYS_PER_SCAN = 2
MAX_COPY_WALLETS = 25
SWEEP_INTERVAL_SECONDS = 30 * 60
URGENT_EXITS = {"stop_loss", "rug_liquidity", "rug_no_route", "rug_dev", "kill", "no_price"}
SHADOW_STAGES = {"rug", "score", "rute", "claude", "risiko", "sniper", "købt"}
SHADOW_HORIZON_MINUTES = 360
SHADOW_TRACK_SECONDS = 60
SHADOW_MAX_ACTIVE = 150
SHADOW_MARKED_PER_SCAN = 2
SHADOW_LEARN_RATE = 0.3
SHADOW_OPTIMIZER_STAGES = {"købt", "risiko", "claude"}
INSIDER_CHECK_SECONDS = 60
KILL_LOCK_SECONDS = 3.0
WALLET_CACHE_SECONDS = 20
EXIT_FAILURE_LIMIT = 6
MISSING_PRICE_LIMIT = 20
ADAPT_EVERY = 5
NO_LEARN_EXITS = {"kill", "manual"}
FEE_MARGIN_USD = 0.05

# Vis beskeden om en kildes rate limit (Jupiter, RugCheck, DexScreener) i ti minutter efter seneste 429.
JUPITER_ALERT_SECONDS = 600
SNIPER_LOOP_SECONDS = 5
# Top-traders: kig efter deres nye handler hvert 8. sekund, og hent listen igen hver halve time.
TOP_POLL_SECONDS = 8
TOP_REFRESH_SECONDS = 1800
TOP_SIGNALS_KEPT = 40
# Mini-grafer: et punkt hvert 20. sekund, de seneste 90 punkter (ca. en halv time).
PATH_STEP_SECONDS = 20
PATH_POINTS = 90
# Din egen Top 10-liste: højst så mange traders.
TOP_OWN_MAX = 25
# Strategi-vagt: taber en strategi på de seneste handler, sættes den på hold, mens den lærer videre som skygge.
STRATEGY_LABELS = {"momentum": "Almindelig", "sniper": "Sniper", "toptrader": "Top 10"}
GUARD_WINDOW = 12
GUARD_MIN_TRADES = 8
GUARD_MAX_PF = 0.8
GUARD_HOLD_HOURS = 24
GUARD_MAX_HOURS = 96
GUARD_SKIP_EXITS = {"kill", "manual"}
# Automatisk mål for sniperen: kun med mange kursforløb, kun hvis det klart er bedre og giver overskud.
# Én gang efter opdateringen: en papirkonto med historik starter forfra med 50 USD (brugerens ønske).
PAPER_RESTART_ONCE = ("papir-50-forskning-nov-2026", 50.0)
# Én gang: nye, forskningsbaserede standarder for allerede gemte indstillinger (kun strammere, aldrig løsere).
RESEARCH_UPGRADE = "forskning-nov-2026"
SNIPER_RESEARCH_MIN_AGE_SECONDS = 300.0
ANALYSIS_ROWS = 1500
ANALYSIS_CACHE_SECONDS = 300
TIGHTEN_LOG_KEPT = 20
# Indstillinger gemt som brøk (0,25 = 25 %).
FRACTION_PARAMS = {"stop_loss_pct", "take_profit_pct", "partial_fraction", "trail_activation_pct", "trailing_stop_pct", "min_buy_ratio_h1", "sell_fraction"}
# Én gang: strategi-vagten slås til, og Top 10 slås fra, til tallene viser overskud.
UPGRADE_V2 = "vagt-top10-nov-2026"
# En indstilling, du selv har sat (eller hvor du har fortrudt en automatisk ændring), rører læringen ikke i en uge.
PARAM_LOCK_DAYS = 7
CHANGE_LOG_KEPT = 60
PAPER_MIN_USD, PAPER_MAX_USD = 5.0, 100_000.0
AUTO_TARGET_MIN_PATHS = 40
AUTO_TARGET_GAIN = 0.02
AUTO_TARGET_EVERY_SECONDS = 24 * 3600
AUTO_TARGET_CHECK_SECONDS = 3600
SNIPER_DEEP_CHECKS = 4
SNIPER_SEEN_HOURS = 6
SNIPER_WAITING_MAX = 300

SOURCE_LABELS = {
    "sniper": "Sniper: ny token",
    "jup_trending": "Jupiter: trending",
    "jup_organic": "Jupiter: organisk score",
    "dex_boost": "DexScreener: boost",
    "dex_profile": "DexScreener: ny profil",
}


def _dk(value: float, digits: int = 2) -> str:
    """Tal med dansk komma og punktum, fx 1.234,50."""
    return f"{value:,.{digits}f}".replace(",", "§").replace(".", ",").replace("§", ".")


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
        top: Any = None,
    ) -> None:
        self.dex = dex or DexScreener()
        self.top = top if top is not None else (SolanaTracker(settings) if settings.solanatracker_api_key else None)
        self.rug = rug or RugCheck(settings)
        self.jup = jup or Jupiter(settings)
        self.x = x if x is not None else (XClient(settings.x_bearer_token) if settings.x_configured else None)
        if rpc is not None:
            self.rpc = rpc
        elif settings.copy_wallets or settings.wallet_configured:
            self.rpc = SolanaRPC(settings.rpc_url)
        else:
            self.rpc = None

    def close(self) -> None:
        for client in (self.dex, self.rug, self.jup, self.x, self.rpc, self.top):
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
        self._env_settings = self.settings
        self.main_settings = main_settings
        self.store = store or MemeStore(self.settings.db_path)
        if (self.store.get_state("mode", "paper") or "paper") != "live":
            self.settings = self._mode_settings("paper")
        self.clients = clients or MemeClients(self.settings)
        self.researcher = researcher or MemeResearcher(self.settings, self.store)
        stored_ai = self.store.get_json("ai_budget", None) or {}
        self.researcher.limit_on = bool(stored_ai.get("on", self.settings.ai_budget_limit))
        self._notifier = notifier
        self.params = StrategyParams.from_dict(self.store.get_json("params"))
        self.model = Model.from_dict(self.store.get_json("model"))
        self.kill_switch = self.store.get_state("kill_switch", "0") == "1"
        self.paused = self.store.get_state("paused", "0") == "1"
        self.sniper: SniperSettings = sniper_from(self.store.get_json("sniper"), self.settings)
        self._last_snipe = 0.0
        self.last_snipe: dict[str, Any] = {}
        self.copy = CopyTracker(self.clients.rpc, self.store, self._all_wallets())
        self.top_cfg: TopTraderSettings = top_from(self.store.get_json("toptraders"), self.settings)
        stored_limits = self.store.get_json("loss_limits", None) or {}
        self.loss_limits = {
            "daily": bool(stored_limits.get("daily", self.settings.daily_loss_limit)),
            "drawdown": bool(stored_limits.get("drawdown", self.settings.drawdown_limit)),
        }
        self.top_copy = CopyTracker(self.clients.rpc, self.store, (), prefix="top_last")
        self._last_top_poll = 0.0
        self.top_signals: deque = deque(self.store.get_json("top_signals", []) or [], maxlen=TOP_SIGNALS_KEPT)
        self.top_last: dict[str, Any] = {}
        self.guard_on = (self.store.get_state("strategy_guard") or ("1" if self.settings.strategy_guard else "0")) == "1"
        self.auto_targets = (self.store.get_state("auto_targets") or ("1" if self.settings.auto_targets else "0")) == "1"
        self._guard_seen = -1
        self._last_auto_target_check = 0.0
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
        self._sol_hist: list[tuple[float, float]] = []  # (tidspunkt, SOL-kurs), til markedsfilteret
        self._wallet_sol: float | None = None
        self._wallet_sol_at = 0.0
        self._marked_shadow_budget = SHADOW_MARKED_PER_SCAN
        self._last_shadow_track = 0.0
        self._last_insider_check = 0.0
        self._shadow_misses: dict[int, int] = {}
        self._optimizer_cache: tuple[tuple, float, dict] | None = None
        self._mopt_cache: tuple[tuple, float, dict] | None = None
        self._analysis_cache: tuple[float, dict] | None = None
        self._sell_queue: set[int] = set()
        self._missing_price: dict[int, int] = {}
        self._copy_sells: set[tuple[str, str]] = set()
        # Kursforløb pr. åben position til mini-grafer på dashboardet. Gemmes også i databasen.
        self._paths: dict[int, deque] = {}
        self._path_at: dict[int, float] = {}
        self._priced_at: dict[int, float] = {}
        # Uret, som dashboardet tæller ned efter: hvad laver botten lige nu, og hvornår sker det næste.
        self._started_at = time.time()
        self._phase = "starter"
        self._phase_since = time.time()
        self._tick_started = 0.0
        self._last_monitor = 0.0
        self._tick_seconds: float | None = None
        self._jobs: dict[str, dict[str, Any]] = {
            name: {"running": False, "started": 0.0, "duration": None, "runs": 0} for name in ("scan", "snipe", "top")
        }
        self.last_tick: str | None = None
        self.last_scan_at: str | None = None
        self.last_scan: dict[str, Any] = {}
        self.last_error = ""
        self.consecutive_errors = 0
        self.mode = "paper"
        self._restore_mode()
        self.settings = self._mode_settings(self.mode)
        self._maybe_research_upgrade()
        self._maybe_upgrade_v2()
        self._maybe_restart_once()

    # ------------------------------------------------------------------ livscyklus
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="memebot", daemon=True)
        self._thread.start()
        self.store.add_event("info", f"Memebotten er startet i {'papir' if self.mode == 'paper' else 'LIVE'}-tilstand.")
        threading.Thread(target=self._startup_checks, name="memebot-checks", daemon=True).start()

    def _startup_checks(self) -> None:
        try:
            self.run_diagnostics()
        except Exception:
            logger.exception("Forbindelsestesten fejlede")

    def run_diagnostics(self) -> dict:
        from trader.meme.diagnostics import run_checks

        result = run_checks(self)
        failed = [item["label"] for item in result["checks"] if not item["ok"]]
        self.store.add_event(
            "info" if result["required_ok"] else "warning",
            f"Forbindelsestest: {result['ok']} af {result['total']} virker.",
            ("Fejler: " + ", ".join(failed)) if failed else "Alle datakilder svarer.",
        )
        return result

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.clients.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick(from_loop=True)
            except Exception:
                logger.exception("Memebottens løkke fangede en fejl")
            self._wake.wait(self._sleep_seconds())
            self._wake.clear()

    def _sleep_seconds(self) -> float:
        """Vågn præcis, når næste scanning, snipe eller Top 10-tjek skal køre, så nedtællingen passer."""
        base = self._loop_seconds()
        due = [self._last_monitor + base] + [at for at in self._due_times().values() if at is not None]
        return max(0.2, min(base, min(due) - time.time()))

    def _due_times(self) -> dict[str, float | None]:
        return {
            "scan": self._last_scan + self.settings.scan_seconds,
            "snipe": self._last_snipe + self.settings.sniper_scan_seconds if self.sniper.enabled else None,
            "top": self._last_top_poll + TOP_POLL_SECONDS if (self.top_cfg.enabled or self._held_top_wallets()) else None,
        }

    def _set_phase(self, phase: str) -> None:
        self._phase = phase
        self._phase_since = time.time()

    def _run_job(self, name: str, phase: str, fn: Callable[[], None]) -> None:
        job = self._jobs[name]
        job["running"], job["started"] = True, time.time()
        self._set_phase(phase)
        try:
            fn()
        finally:
            job["running"] = False
            job["duration"] = round(time.time() - job["started"], 1)
            job["runs"] += 1

    def _loop_seconds(self) -> float:
        """Sniperen kræver hurtigere runder, så stop og gevinstmål rammes i tide."""
        if self.sniper.enabled or self.top_cfg.enabled:
            return float(min(self.settings.monitor_seconds, SNIPER_LOOP_SECONDS))
        return float(self.settings.monitor_seconds)

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
    # ------------------------------------------------------------------ papirkontoen
    def _mode_settings(self, mode: str) -> MemeSettings:
        """Live bruger altid MEME_BANKROLL_USD som hårdt loft. Papir kan have sit eget startbeløb,
        og så skaleres beløbene pr. handel med, så de faste gebyrer fylder lige så lidt."""
        env = self._env_settings
        if mode != "paper":
            return env
        try:
            amount = float(self.store.get_state("paper_bankroll") or 0)
        except ValueError:
            amount = 0.0
        if amount <= 0 or env.bankroll_usd <= 0:
            return env
        scale = amount / env.bankroll_usd
        position = round(max(0.5, min(amount, env.position_usd * scale)), 2)
        return dataclasses.replace(
            env,
            bankroll_usd=amount,
            position_usd=position,
            sniper_position_usd=round(max(0.5, env.sniper_position_usd * scale), 2),
        )

    def restart_paper(self, amount: float) -> str:
        """Start papirkontoen forfra med et nyt beløb. Gamle handler gemmes i et arkiv, læringen beholdes."""
        try:
            amount = round(float(amount), 2)
        except (TypeError, ValueError) as exc:
            raise ValueError("Beløbet skal være et tal.") from exc
        if not PAPER_MIN_USD <= amount <= PAPER_MAX_USD:
            raise ValueError(f"Beløbet skal være mellem {PAPER_MIN_USD:.0f} og {PAPER_MAX_USD:,.0f} USD.".replace(",", "."))
        with self._lock:
            if self.mode != "paper":
                raise ValueError("Kun papirkontoen kan startes forfra. Med rigtige penge bestemmer din wallet beløbet.")
            old = self.settings.bankroll_usd
            scale = amount / old if old > 0 else 1.0
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            archived = self.store.archive_paper(f"paper-arkiv-{stamp}")
            self.store.set_state("paper_cash", repr(amount))
            self.store.set_state("paper_bankroll", repr(amount))
            for key in ("failed_fees:paper", "dd_reset:paper", "halt_dd:paper", f"halt_day:paper:{utc_day()}"):
                self.store.set_state(key, "0")
            self.settings = self._mode_settings("paper")
            sniper_usd, top_usd = self.sniper.position_usd * scale, self.top_cfg.position_usd * scale
            self.sniper = dataclasses.replace(self.sniper, position_usd=clamp_sniper("position_usd", sniper_usd, self.settings.position_usd))
            self._save_sniper()
            self.top_cfg = dataclasses.replace(self.top_cfg, position_usd=clamp_top("position_usd", top_usd, self.settings.position_usd))
            self.store.set_json("toptraders", self.top_cfg.to_dict())
            for cache in (self._paths, self._path_at, self._priced_at, self._missing_price):
                cache.clear()
            self._sell_queue.clear()
            self._copy_sells.clear()
            self._optimizer_cache = None
            self._last_snapshot = 0.0
            message = (
                f"Papirkontoen er startet forfra med {_dk(amount)} USD. Beløb pr. handel: {_dk(self.settings.position_usd)} USD "
                f"(almindelig), {_dk(self.sniper.position_usd)} USD (sniper) og {_dk(self.top_cfg.position_usd)} USD (Top 10). "
                f"Læringen er beholdt, og de {archived} gamle handler er gemt i et arkiv."
            )
            self.store.add_event("info", message)
            self._wake.set()
            return message

    def _maybe_restart_once(self) -> None:
        key, amount = PAPER_RESTART_ONCE
        if self.store.get_state("paper_restart_once") == key:
            return
        self.store.set_state("paper_restart_once", key)
        # Kun en papirkonto, der allerede har handlet. En helt ny bot starter med MEME_BANKROLL_USD.
        if self.mode == "paper" and (self.store.open_positions("paper") or self.store.closed_positions("paper")):
            try:
                self.restart_paper(amount)
            except ValueError as exc:
                logger.warning("Papirkontoen kunne ikke startes forfra: %s", exc)

    def _maybe_research_upgrade(self) -> None:
        """Nye værn efter research i nov. 2026. Gemte sniper-indstillinger fra før får den nye ventetid."""
        if self.store.get_state("research_upgrade") == RESEARCH_UPGRADE:
            return
        self.store.set_state("research_upgrade", RESEARCH_UPGRADE)
        notes = []
        if self.sniper.min_age_seconds < SNIPER_RESEARCH_MIN_AGE_SECONDS:
            old = self.sniper.min_age_seconds
            self.sniper.min_age_seconds = SNIPER_RESEARCH_MIN_AGE_SECONDS
            self._save_sniper()
            self._log_change(
                "sniper", [("min_age_seconds", old, SNIPER_RESEARCH_MIN_AGE_SECONDS)],
                "De første 5 minutters handel afslører de fleste rug pulls.", "opgradering",
            )
            notes.append(f"Sniperen venter nu {SNIPER_RESEARCH_MIN_AGE_SECONDS:.0f} s i stedet for {old:.0f} s efter lancering")
        text = (
            "Nye værn mod tab (research nov. 2026): Jupiters mistænkelig-markering og serie-udviklere afvises, "
            f"organisk score under {self.params.min_organic_score:.0f} afvises, RugCheck-insidere tæller med, "
            "sniperen kræver Telegram eller X, og Top 10 køber ikke langt over traderens egen pris."
        )
        if notes:
            text += " " + ". ".join(notes) + "."
        if self.store.get_json("params") is not None or notes:  # kun for en bot, der har kørt før
            self.store.add_lesson("parameter", text)
            self.store.add_event("info", text)

    def _maybe_upgrade_v2(self) -> None:
        """Én gang: strategi-vagten slås til, og Top 10 holder pause, til tallene viser overskud."""
        if self.store.get_state("upgrade_v2") == UPGRADE_V2:
            return
        self.store.set_state("upgrade_v2", UPGRADE_V2)
        notes = []
        if not self.guard_on:
            self.guard_on = True
            self.store.set_state("strategy_guard", "1")
            notes.append("Strategi-vagten er slået til: en strategi, der bliver ved med at tabe, holder selv pause og lærer videre som skyggehandler.")
        if self.top_cfg.enabled:
            self.update_toptraders({"enabled": False})
            notes.append("Top 10 er slået fra, fordi kopiering med forsinkelse sjældent giver overskud. Tænd den igen under Strategi → Top 10, hvis tallene viser noget andet.")
        if notes and self.store.get_json("params") is not None:
            text = " ".join(notes)
            self.store.add_lesson("parameter", text)
            self.store.add_event("info", text)

    # ------------------------------------------------------------------ låste indstillinger og ændringslog
    def _locks(self) -> dict[str, float]:
        now = time.time()
        return {key: float(until) for key, until in (self.store.get_json("param_locks", {}) or {}).items() if float(until) > now}

    def _locked(self, strategy: str) -> set[str]:
        prefix = f"{strategy}:"
        return {key[len(prefix):] for key in self._locks() if key.startswith(prefix)}

    def _lock_params(self, strategy: str, names, days: float = PARAM_LOCK_DAYS) -> None:
        locks = self._locks()
        for name in names:
            locks[f"{strategy}:{name}"] = time.time() + days * 86400
        self.store.set_json("param_locks", locks)

    def _unlock_strategy(self, strategy: str) -> None:
        self.store.set_json("param_locks", {key: until for key, until in self._locks().items() if not key.startswith(f"{strategy}:")})

    def _log_change(self, strategy: str, changes, why: str, source: str) -> None:
        """Gem en automatisk ændring, så den kan ses samlet og fortrydes fra siden."""
        labels = SNIPER_LABELS if strategy == "sniper" else PARAM_LABELS
        items = [
            {"param": name, "label": labels.get(name, name), "old": float(old), "new": float(new)}
            for name, old, new in changes
            if abs(float(old) - float(new)) > 1e-9
        ]
        if not items:
            return
        seq = int(self.store.get_state("auto_change_seq", "0") or 0) + 1
        self.store.set_state("auto_change_seq", str(seq))
        log = list(self.store.get_json("auto_changes", []) or [])
        log.append({"id": seq, "at": now_iso(), "strategy": strategy, "changes": items, "why": str(why)[:400], "source": source, "undone_at": None})
        self.store.set_json("auto_changes", log[-CHANGE_LOG_KEPT:])

    def _target_value(self, strategy: str, name: str) -> float:
        value = getattr(self.sniper if strategy == "sniper" else self.params, name, None)
        return float(value) if isinstance(value, (int, float)) else float("nan")  # en indstilling, der ikke findes mere

    @staticmethod
    def _value_text(name: str, value: float) -> str:
        """Et tal, som siden viser det: brøker som procent, sekunder og minutter med enhed."""
        if name in FRACTION_PARAMS:
            return f"{_dk(value * 100, 0)} %"
        if name == "min_age_seconds":
            return f"{_dk(value, 0)} sek."
        if name in {"min_age_minutes", "max_hold_minutes", "stale_minutes", "max_age_minutes"}:
            return f"{_dk(value, 0)} min."
        if name.endswith("_usd"):
            return f"{_dk(value, 0)} $"
        if name in {"max_change_h1_pct", "max_top10_pct", "max_roundtrip_loss_pct"}:
            return f"{_dk(value, 0)} %"
        return _dk(value, 2 if abs(value) < 10 and value != int(value) else 0)

    def _change_view(self, entry: dict) -> dict:
        undone = bool(entry.get("undone_at"))
        current = all(abs(self._target_value(entry["strategy"], item["param"]) - item["new"]) < 1e-6 for item in entry["changes"])
        return {
            **entry,
            "changes": [
                {**item, "old_text": self._value_text(item["param"], item["old"]), "new_text": self._value_text(item["param"], item["new"])}
                for item in entry["changes"]
            ],
            "strategy_label": STRATEGY_LABELS.get(entry["strategy"], entry["strategy"]),
            "can_undo": not undone and current,
            "changed_since": not undone and not current,
        }

    def undo_change(self, change_id: int) -> str:
        """Fortryd en automatisk ændring. Indstillingen låses derefter, så læringen ikke laver den om igen med det samme."""
        log = list(self.store.get_json("auto_changes", []) or [])
        entry = next((item for item in log if int(item.get("id", -1)) == int(change_id)), None)
        if entry is None:
            raise ValueError("Ændringen findes ikke længere.")
        if entry.get("undone_at"):
            raise ValueError("Ændringen er allerede fortrudt.")
        strategy = entry["strategy"]
        for item in entry["changes"]:
            if not abs(self._target_value(strategy, item["param"]) - item["new"]) <= 1e-6:  # også når den ikke findes (NaN)
                raise ValueError(f"{item['label']} er ændret igen siden. Ret den direkte under Strategi.")
        with self._lock:
            if strategy == "sniper":
                updated = dataclasses.replace(self.sniper)
                for item in entry["changes"]:
                    setattr(updated, item["param"], clamp_sniper(item["param"], item["old"], self.settings.position_usd))
                self.sniper = updated
                self._optimizer_cache = None
                self._save_sniper()
            else:
                for item in entry["changes"]:
                    setattr(self.params, item["param"], clamp_param(item["param"], item["old"]))
                self._save_learning()
                self._mopt_cache = None
            self._lock_params(strategy, [item["param"] for item in entry["changes"]])
            entry["undone_at"] = now_iso()
            self.store.set_json("auto_changes", log)
        what = ", ".join(f"{item['label']} {_dk(item['new'])} → {_dk(item['old'])}" for item in entry["changes"])
        message = f"Fortrudt ({STRATEGY_LABELS.get(strategy, strategy)}): {what}. Læringen rører det ikke i {PARAM_LOCK_DAYS} dage."
        self.store.add_event("info", message)
        self.store.add_lesson("parameter", message)
        return message

    def update_params(self, changes: dict) -> None:
        """Fra siden: den almindelige strategis filtre og salgsregler. Altid inden for de faste grænser."""
        if not isinstance(changes, dict):
            raise ValueError("Ugyldige indstillinger.")
        values = {}
        for key, value in changes.items():
            if key not in PARAM_BOUNDS:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{PARAM_LABELS.get(key, key)} skal være et tal.") from exc
            if number != number or number in (float("inf"), float("-inf")):
                raise ValueError(f"{PARAM_LABELS.get(key, key)} skal være et tal.")
            values[key] = clamp_param(key, number)
        with self._lock:
            changed = [key for key, value in values.items() if abs(float(getattr(self.params, key)) - float(value)) > 1e-9]
            if not changed:
                return
            for key in changed:
                setattr(self.params, key, values[key])
            self._save_learning()
            self._mopt_cache = None
            self._analysis_cache = None
            self._lock_params("momentum", changed)
        labels = ", ".join(PARAM_LABELS.get(key, key) for key in changed)
        self.store.add_event("info", f"Almindelig strategi ændret af dig: {labels}. Læringen rører dem ikke i {PARAM_LOCK_DAYS} dage.")

    def reset_params(self) -> None:
        with self._lock:
            self.params = StrategyParams()
            self._save_learning()
            self._mopt_cache = None
            self._unlock_strategy("momentum")
        self.store.add_event("info", "Den almindelige strategis filtre og salgsregler er sat tilbage til standard.")

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
        return self._signer()

    def _signer(self) -> SolanaWallet:
        """Bot-wallet'en (samme nøgle i papir og live). Kun til saldo, ind- og udbetaling i papir."""
        if self._live is None:
            if not self.settings.private_key:
                raise ValueError("SOLANA_PRIVATE_KEY er ikke sat. Se fanen Wallet for, hvordan du laver en bot-wallet i Phantom.")
            rpc = self.clients.rpc or SolanaRPC(self.settings.rpc_url)
            self.clients.rpc = rpc
            self.copy.rpc = rpc
            self._live = SolanaWallet(self.settings, rpc, self.clients.jup)
        return self._live

    def _withdraw_target(self) -> tuple[str, str]:
        """(adresse, fejl). Udbetaling går kun til adressen i MEME_WITHDRAW_ADDRESS."""
        target = self.settings.withdraw_address
        if not target:
            return "", "MEME_WITHDRAW_ADDRESS er ikke sat."
        if not _valid_address(target):
            return "", "MEME_WITHDRAW_ADDRESS er ikke en gyldig Solana-adresse."
        pubkey, _error = self._wallet_pubkey()
        if pubkey and target == pubkey:
            return "", "MEME_WITHDRAW_ADDRESS må ikke være bot-wallet'en selv."
        return target, ""

    def wallet_info(self, *, refresh: bool = False, phantom: str = "") -> dict:
        pubkey, key_error = self._wallet_pubkey()
        target, target_error = self._withdraw_target()
        if refresh and pubkey and time.time() - self._wallet_sol_at >= 3:
            try:
                self._wallet_sol = self._signer().sol_balance()
                self._wallet_sol_at = time.time()
            except (SourceError, ValueError) as exc:
                key_error = key_error or f"Saldo kunne ikke hentes: {exc}"
        sol_usd = self._sol_usd[0] or None
        info: dict[str, Any] = {
            "address": pubkey,
            "configured": bool(pubkey),
            "error": key_error,
            "sol": self._wallet_sol,
            "usd": (self._wallet_sol * sol_usd) if self._wallet_sol is not None and sol_usd else None,
            "updated": datetime.fromtimestamp(self._wallet_sol_at, timezone.utc).isoformat(timespec="seconds") if self._wallet_sol_at else None,
            "reserve_sol": self.settings.sol_reserve,
            "withdraw_address": target,
            "withdraw_error": target_error,
            "sweep_above_usd": self.settings.sweep_above_usd,
            "swept_total_usd": float(self.store.get_state("swept_total_usd", "0") or 0),
            "last_sweep": self.store.get_json("last_sweep", None),
            "max_deposit_sol": MAX_DEPOSIT_SOL,
            "phantom": None,
        }
        phantom = phantom.strip()
        if refresh and phantom and _valid_address(phantom):
            try:
                rpc = self.clients.rpc or SolanaRPC(self.settings.rpc_url)
                self.clients.rpc = rpc
                info["phantom"] = {"address": phantom, "sol": rpc.balance_lamports(phantom) / LAMPORTS_PER_SOL}
            except SourceError as exc:
                info["phantom"] = {"address": phantom, "sol": None, "error": str(exc)}
        return info

    def deposit_transaction(self, from_address: str, sol_amount: float) -> dict:
        """Byg en usigneret overførsel fra din Phantom til bot-wallet'en. Phantom signerer den."""
        from_address = (from_address or "").strip()
        pubkey, key_error = self._wallet_pubkey()
        if not pubkey:
            raise ValueError(key_error or "Bot-wallet'en er ikke sat op.")
        if not _valid_address(from_address):
            raise ValueError("Forbind Phantom først.")
        if from_address == pubkey:
            raise ValueError("Din Phantom-konto er den samme som bot-wallet'en. Vælg din hovedkonto i Phantom.")
        try:
            sol = float(sol_amount)
        except (TypeError, ValueError) as exc:
            raise ValueError("Beløbet skal være et tal.") from exc
        if not 0.001 <= sol <= MAX_DEPOSIT_SOL:
            raise ValueError(f"Indbetal mellem 0,001 og {MAX_DEPOSIT_SOL:g} SOL.")
        rpc = self.clients.rpc or SolanaRPC(self.settings.rpc_url)
        self.clients.rpc = rpc
        blockhash, last_valid = rpc.latest_blockhash()
        lamports = int(round(sol * LAMPORTS_PER_SOL))
        message = build_transfer_message(from_address, pubkey, lamports, blockhash)
        return {
            "message": b58encode(message),
            "to": pubkey,
            "from": from_address,
            "lamports": lamports,
            "sol": lamports / LAMPORTS_PER_SOL,
            "last_valid_block_height": last_valid,
        }

    def withdraw(self, sol_amount: float) -> dict:
        target, target_error = self._withdraw_target()
        if not target:
            raise ValueError(target_error)
        try:
            sol = float(sol_amount)
        except (TypeError, ValueError) as exc:
            raise ValueError("Beløbet skal være et tal.") from exc
        with self._lock:
            wallet = self._signer()
            try:
                balance = wallet.sol_balance()
            except SourceError as exc:
                raise ValueError(f"Saldo kunne ikke hentes: {exc}") from exc
            available = balance - self.settings.sol_reserve - 0.00001
            if sol <= 0:
                raise ValueError("Beløbet skal være over 0.")
            if sol > available + 1e-12:
                raise ValueError(
                    f"Du kan højst hæve {max(0.0, available):.4f} SOL. Reserven på {self.settings.sol_reserve} SOL til gebyrer bliver i wallet'en."
                )
            ok, signature, error = wallet.transfer_sol(target, int(sol * LAMPORTS_PER_SOL))
            if not ok:
                self.store.add_event("error", f"Udbetaling af {sol:.4f} SOL fejlede: {error}")
                raise ValueError(f"Udbetalingen fejlede: {error}")
            self._wallet_sol, self._wallet_sol_at = None, 0.0
            self.store.add_event("info", f"{sol:.4f} SOL er sendt til din Phantom ({target[:4]}…{target[-4:]}).", signature)
            self._notify(f"{sol:.4f} SOL er sendt til din Phantom.")
            return {"signature": signature, "sol": sol, "to": target}

    def _maybe_sweep(self, wallet, sol_usd: float) -> None:
        """Send overskud over bankroll til din Phantom. Kun live, kun til MEME_WITHDRAW_ADDRESS."""
        if self.mode != "live" or self.settings.sweep_above_usd <= 0 or sol_usd <= 0:
            return
        target, _error = self._withdraw_target()
        if not target:
            return
        now = time.time()
        if now - float(self.store.get_state("last_sweep_ts", "0") or 0) < SWEEP_INTERVAL_SECONDS:
            return
        self.store.set_state("last_sweep_ts", repr(now))
        cash = wallet.cash_usd(sol_usd)
        excess = cash - self.settings.bankroll_usd
        if excess < self.settings.sweep_above_usd:
            return
        sol = excess / sol_usd
        ok, signature, error = wallet.transfer_sol(target, int(sol * LAMPORTS_PER_SOL))
        if not ok:
            self.store.add_event("warning", f"Automatisk udbetaling fejlede: {error}")
            return
        total = float(self.store.get_state("swept_total_usd", "0") or 0) + excess
        self.store.set_state("swept_total_usd", repr(total))
        self.store.set_json("last_sweep", {"ts": now_iso(), "usd": excess, "sol": sol, "signature": signature})
        self._wallet_sol, self._wallet_sol_at = None, 0.0
        self.store.add_event("trade", f"Gevinst på {excess:.2f} USD ({sol:.4f} SOL) er sendt til din Phantom.", signature)
        self._notify(f"Gevinst på {excess:.2f} USD er sendt til din Phantom.")

    def _refresh_wallet_sol(self, wallet) -> None:
        """Opdater den viste SOL-saldo efter en rigtig handel."""
        if self.mode != "live" or not hasattr(wallet, "public_key") or not wallet.public_key:
            return
        try:
            self._wallet_sol = wallet.sol_balance()
            self._wallet_sol_at = time.time()
        except SourceError:
            pass

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
        now = time.time()
        self._sol_usd = (fresh, now)
        self._sol_hist = [item for item in self._sol_hist if now - item[0] <= SOL_BASELINE_MAX_SECONDS + 600]
        self._sol_hist.append((now, fresh))
        return fresh

    def _entry_halt(self, day: str) -> str:
        """Hvorfor botten ikke køber nye tokens lige nu. Tom streng, hvis den må købe. Salg berøres aldrig."""
        drop = sol_drop_pct(self._sol_hist, time.time())
        if sol_halt(drop, self.settings):
            return (
                f"SOL er ned {str(round(abs(drop), 1)).replace('.', ',')} % den seneste time "
                f"(grænsen er {str(self.settings.sol_filter_pct).replace('.', ',')} %). Botten køber ikke nye tokens, mens markedet falder."
            )
        losses = self.store.losses_closed_on(self.mode, day)
        if losses_halt(losses, self.settings):
            return f"Der er lukket {losses} handler med tab i dag (grænsen er {self.settings.max_losses_per_day}). Botten køber ikke flere nye tokens i dag."
        return ""

    def _note_entry_halt(self, day: str, why: str) -> None:
        """Én besked om stoppet, højst hvert halve time, så loggen ikke fyldes op."""
        key = f"entry_halt_note:{self.mode}:{day}"
        last = float(self.store.get_state(key, "0") or 0)
        if time.time() - last < 1800:
            return
        self.store.set_state(key, str(time.time()))
        self.store.add_event("warning", why + " Salg fortsætter.")

    # ------------------------------------------------------------------ hovedrunde
    def request_scan(self) -> None:
        self._scan_requested = True
        self._wake.set()

    def tick(self, force_scan: bool = False, *, from_loop: bool = False) -> None:
        with self._lock:
            now = time.time()
            self.last_tick = now_iso()
            # Vågner løkken kun for at scanne, er positionerne lige tjekket. Så starter scanningen til tiden.
            monitor_due = not from_loop or force_scan or self._scan_requested or now - self._last_monitor >= self._loop_seconds() - 0.5
            self._tick_started = now
            self._set_phase("positioner" if monitor_due else "venter")
            try:
                sol_usd = self._sol_price()
                wallet = self._wallet()
                if self.mode == "live":
                    try:
                        self._wallet_sol = wallet.sol_balance()
                        self._wallet_sol_at = time.time()
                    except SourceError as exc:
                        logger.info("SOL-saldo kunne ikke hentes: %s", exc)
                    self._maybe_sweep(wallet, sol_usd)
                if monitor_due:
                    self._last_monitor = time.time()
                    self._monitor(wallet, sol_usd)
                self._learn()
                self._check_guard()
                self._maybe_auto_targets()
                self._watch_insiders(wallet, sol_usd)
                self._track_shadows()
                self._learn_shadows()
                self._maybe_daily_report()
                day = utc_day()
                totals = self._totals()
                if self.store.ensure_day(day, self.mode, totals["equity"]):
                    self.store.add_event("info", f"Ny dag {day}. Egenkapital {totals['equity']:.2f} USD.")
                    self._daily_review()
                halted_day, halted_dd = self._halts(totals, day, wallet, sol_usd)
                requested = force_scan or self._scan_requested
                entry_halt = self._entry_halt(day)
                if entry_halt:
                    self._note_entry_halt(day, entry_halt)
                entries_allowed = not (self.kill_switch or self.paused or halted_day or halted_dd or entry_halt)
                # Top 10 har ingen daglig tabsgrænse, medmindre du slår den til. Nødstop, pause og samlet tabsgrænse gælder altid.
                top_entries_allowed = not (
                    self.kill_switch or self.paused or halted_dd or entry_halt or (halted_day and self.top_cfg.daily_loss_stop)
                )
                # Lidt slæk, så en runde der vågner et øjeblik for tidligt ikke venter en hel runde mere.
                now = time.time() + 0.25
                if requested or now - self._last_scan >= self.settings.scan_seconds:
                    self._scan_requested = False
                    self._last_scan = time.time()
                    self._run_job("scan", "scan", lambda: self._scan(wallet, sol_usd, day, entries_allowed=entries_allowed))
                if self.sniper.enabled and (requested or now - self._last_snipe >= self.settings.sniper_scan_seconds):
                    self._last_snipe = time.time()
                    self._run_job("snipe", "sniper", lambda: self._snipe(wallet, sol_usd, day, entries_allowed=entries_allowed))
                if (self.top_cfg.enabled or self._held_top_wallets()) and (requested or now - self._last_top_poll >= TOP_POLL_SECONDS):
                    self._last_top_poll = time.time()
                    self._run_job("top", "top10", lambda: self._follow_top(wallet, sol_usd, day, entries_allowed=top_entries_allowed))
                if now - self._last_snapshot >= 300:
                    self._last_snapshot = now
                    self.store.add_equity(self.mode, self._totals()["equity"])
                    self.store.set_day_end(day, self.mode, self._totals()["equity"])
                    self.store.prune()
                    self.store.prune_paths(PATH_POINTS)
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
            finally:
                self._tick_seconds = round(time.time() - self._tick_started, 2)
                self._set_phase("venter")

    # ------------------------------------------------------------------ positioner og salg
    def _monitor(self, wallet, sol_usd: float) -> None:
        positions = self.store.open_positions(self.mode)
        if not positions:
            self._copy_sells.clear()
            return
        if self._sell_queue:
            queued, self._sell_queue = set(self._sell_queue), set()
            for pos in positions:
                if int(pos["id"]) in queued:
                    self._sell(pos, 1.0, "manual", "Solgt manuelt fra dashboardet.", wallet, sol_usd)
            positions = self.store.open_positions(self.mode)
            if not positions:
                return
        if self.kill_switch or (self.loss_limits["drawdown"] and self.store.get_state(f"halt_dd:{self.mode}", "0") == "1"):
            # Et salg kan fejle under nødstop. Bliv ved med at prøve, indtil alt er solgt eller skrevet af.
            for pos in positions:
                self._sell(pos, 1.0, "kill", "Nødstop: prøver at sælge igen.", wallet, sol_usd)
            return
        mints = [row["mint"] for row in positions]
        pairs: dict[str, PairInfo] = {}
        try:
            pairs = self.clients.dex.pairs_for(mints, priority="high")
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
            self._priced_at[int(pos["id"])] = time.time()
            self._record_path(int(pos["id"]), price / float(pos["entry_price_usd"]))
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
            common = dict(
                ratio=ratio,
                peak_ratio=peak,
                minutes_held=_minutes_since(pos["opened_at"]),
                partial_taken=bool(pos["partial_taken"]),
                liquidity_entry=pos.get("liquidity_entry"),
                liquidity_now=liquidity,
            )
            if pos.get("strategy") == "sniper":
                decision = sniper_exit_decision(cfg=self.sniper, **common)
            elif pos.get("strategy") == "toptrader":
                decision = top_exit_decision(cfg=self.top_cfg, **common)
            else:
                decision = exit_decision(params=self.params, **common)
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
        fill = wallet.sell(pos["mint"], amount, sol_usd, strategy=pos.get("strategy") or "momentum", urgent=kind in URGENT_EXITS)
        if not fill.ok:
            failures = int(pos.get("exit_failures") or 0) + 1
            fees_now = float(pos["fees_usd"]) + float(fill.fee_usd or 0.0)
            self.store.update_position(int(pos["id"]), exit_failures=failures, fees_usd=fees_now)
            pos["fees_usd"] = fees_now
            pos["exit_failures"] = failures
            self.store.add_event("warning", f"Salg af {pos['symbol']} fejlede ({failures}): {fill.error}")
            if failures >= EXIT_FAILURE_LIMIT and kind not in {"take_profit", "target"}:
                self._close(
                    pos,
                    float(pos["proceeds_usd"]),
                    float(pos["fees_usd"]),
                    "rug_no_route",
                    f"Kunne ikke sælge efter {failures} forsøg. Bogført som tab. Sidste fejl: {fill.error}",
                    loads(pos["signatures"], []),
                )
            return False
        self._refresh_wallet_sol(wallet)
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

    def _path_of(self, position_id: int) -> deque:
        path = self._paths.get(position_id)
        if path is None:
            # Efter en genstart: hent forløbet fra databasen, så mini-grafen ikke starter forfra.
            points = self.store.path_points(position_id, PATH_POINTS)
            path = deque([1.0] + [ratio for _ts, ratio in points], maxlen=PATH_POINTS)
            if points:
                self._path_at[position_id] = points[-1][0]
            self._paths[position_id] = path
        return path

    def _record_path(self, position_id: int, ratio: float) -> None:
        now = time.time()
        path = self._path_of(position_id)
        if now - self._path_at.get(position_id, 0.0) < PATH_STEP_SECONDS:
            return
        self._path_at[position_id] = now
        path.append(round(ratio, 5))
        self.store.add_path_point(position_id, now, round(ratio, 5))

    def _close(self, pos: dict, proceeds: float, fees: float, kind: str, reason: str, signatures: list) -> None:
        self._paths.pop(int(pos["id"]), None)
        self._path_at.pop(int(pos["id"]), None)
        self._priced_at.pop(int(pos["id"]), None)
        self.store.drop_path(int(pos["id"]))
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

    def sell_position(self, position_id: int) -> str:
        pos = self.store.get_position(position_id)
        if pos is None or pos["status"] != "open" or pos["mode"] != self.mode:
            raise ValueError("Positionen findes ikke eller er allerede lukket.")
        if not self._lock.acquire(timeout=KILL_LOCK_SECONDS):
            # Botten er midt i en lang runde. Salget lægges i kø og sker i næste runde.
            self._sell_queue.add(int(position_id))
            self._wake.set()
            self.store.add_event("info", f"{pos['symbol']} sælges, så snart botten er færdig med sin runde.")
            return "queued"
        try:
            pos = self.store.get_position(position_id)
            if pos is None or pos["status"] != "open":
                raise ValueError("Positionen er allerede lukket.")
            self._sell(pos, 1.0, "manual", "Solgt manuelt fra dashboardet.", self._wallet(), self._sol_price())
            return "sold"
        finally:
            self._lock.release()

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
            strategy = pos.get("strategy") or "momentum"
            # Tæl kun op til denne handel: lukker flere i samme runde, justeres der stadig kun hver 5. handel.
            same = [
                row for row in self.store.closed_positions(pos["mode"])
                if (row.get("strategy") or "momentum") == strategy and int(row["id"]) <= int(pos["id"])
            ]
            if len(same) % ADAPT_EVERY == 0:
                target = self.sniper if strategy == "sniper" else self.params
                before = target.to_dict()
                if strategy == "sniper":
                    notes = adapt_sniper(self.sniper, same)
                elif strategy == "momentum":
                    notes = adapt_params(self.params, same)
                else:
                    notes = []  # Top 10 følger traderne, ikke de almindelige parametre
                locked = self._locked(strategy)
                labels = SNIPER_LABELS if strategy == "sniper" else PARAM_LABELS
                for name in locked:
                    if name in before and getattr(target, name) != before[name]:
                        setattr(target, name, before[name])  # du har selv sat den: læringen venter
                        notes = [note for note in notes if labels.get(name, name) not in note]
                moved = [(name, before[name], getattr(target, name)) for name in before
                         if isinstance(before[name], (int, float)) and not isinstance(before[name], bool) and getattr(target, name) != before[name]]
                if moved:
                    if strategy == "sniper":
                        self._save_sniper()
                    self._log_change(strategy, moved, " ".join(notes), "læring")
                for note in notes:
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
        realized = sum(float(row["pnl_usd"]) for row in closed) - float(self.store.get_state(f"failed_fees:{mode}", "0") or 0)
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
        halted_day = self.loss_limits["daily"] and self.store.get_state(day_key, "0") == "1"
        halted_dd = self.loss_limits["drawdown"] and self.store.get_state(dd_key, "0") == "1"
        if self.loss_limits["daily"] and not halted_day and daily_loss_hit(totals["today_pnl"], self.settings):
            self.store.set_state(day_key, "1")
            halted_day = True
            message = (
                f"Dagens tab ({totals['today_pnl']:.2f} USD) har ramt grænsen på "
                f"{self.settings.daily_loss_pct * 100:.0f} % af bankroll. Ingen nye køb før i morgen (UTC)."
            )
            self.store.add_event("warning", message)
            self._notify(message)
        reset = float(self.store.get_state(f"dd_reset:{mode}", "0") or 0)
        if self.loss_limits["drawdown"] and not halted_dd and drawdown_hit(totals["total_pnl"] - reset, self.settings):
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
        self._maybe_shadow(cand, stage, reason, features)

    # ------------------------------------------------------------------ skyggehandler
    def _maybe_shadow(self, cand: dict, stage: str, reason: str, features=None) -> None:
        if not self.settings.shadows:
            return
        pair: PairInfo | None = cand.get("pair")
        if pair is None or pair.price_usd <= 0:
            return
        if stage == "marked":
            if self._marked_shadow_budget <= 0:
                return
            self._marked_shadow_budget -= 1
        elif stage not in SHADOW_STAGES:
            return
        if stage != "købt" and pair.dex_id.lower() in BONDING_CURVE_DEXES:
            return  # tokens på bonding curve kan ikke købes alligevel; de fylder bare pladserne
        if stage != "købt" and self.store.count_active_shadows() >= SHADOW_MAX_ACTIVE:
            return
        since = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat(timespec="seconds")
        if self.store.shadow_recent(cand["mint"], since):
            return
        self.store.add_shadow(
            {
                "mode": self.mode,
                "mint": cand["mint"],
                "symbol": pair.symbol,
                "strategy": self._cand_strategy(cand),
                "stage": stage,
                "reason": reason[:300],
                "entry_price": pair.price_usd,
                "liquidity_entry": pair.liquidity_usd,
                "features": features,
                "facts": self._facts(cand),
            }
        )

    @staticmethod
    def _cand_strategy(cand: dict) -> str:
        if cand.get("strategy"):
            return str(cand["strategy"])
        return "sniper" if cand.get("source") == "sniper" else "momentum"

    def _facts(self, cand: dict) -> dict:
        """De rå tal bag beslutningen. Til faktaanalysen og den automatiske strammer."""
        pair: PairInfo | None = cand.get("pair")
        if pair is None:
            return {}
        try:
            facts = facts_of(pair, token=cand.get("token"), report=cand.get("report"), boosted=bool(cand.get("boosted")))
        except Exception as exc:  # fakta må aldrig stoppe en handel
            logger.info("Fakta kunne ikke samles for %s: %s", cand.get("mint"), exc)
            return {}
        if cand.get("age_minutes") is not None:
            facts["age_min"] = round(float(cand["age_minutes"]), 2)
        lead = cand.get("leader") or {}
        if lead.get("chase") is not None:
            facts["chase"] = round(float(lead["chase"]), 4)
        return facts

    def _cost_pct(self, strategy: str) -> float:
        slip = self.settings.paper_slippage_pct + (self.settings.paper_sniper_extra_pct if strategy == "sniper" else 0.0)
        size = self.sniper.position_usd if strategy == "sniper" else self.settings.position_usd
        sol_usd = self._sol_usd[0] or 150.0
        fee_usd = fee_sol(self.settings) * sol_usd
        return 2 * slip / 100.0 + (2 * fee_usd / size if size > 0 else 0.0)

    def _rules_for(self, strategy: str) -> dict:
        if strategy == "toptrader":
            # Traderens salg kendes ikke i en simulering. Stop-loss, sikring af store gevinster og tidsgrænse gør.
            top = self.top_cfg
            return {
                "tp": 1000.0,
                "frac": 1.0,
                "sl": top.stop_loss_pct if top.stop_loss_on else 0.99,
                "max_hold": top.max_hold_minutes,
                "trail_activation": TOP_TRAIL_ACTIVATION,
                "trailing": TOP_TRAIL,
                "cost_pct": self._cost_pct("momentum"),
            }
        if strategy == "sniper":
            return {
                "tp": self.sniper.take_profit_pct,
                "frac": self.sniper.sell_fraction,
                "sl": self.sniper.stop_loss_pct,
                "max_hold": self.sniper.max_hold_minutes,
                "trailing": MOONBAG_TRAIL,
                "breakeven_after_partial": True,
                "cost_pct": self._cost_pct("sniper"),
            }
        return {
            "tp": self.params.take_profit_pct,
            "frac": self.params.partial_fraction,
            "sl": self.params.stop_loss_pct,
            "max_hold": self.params.max_hold_minutes,
            "trail_activation": self.params.trail_activation_pct,
            "trailing": self.params.trailing_stop_pct,
            "stale": self.params.stale_minutes,
            "cost_pct": self._cost_pct("momentum"),
        }

    def _track_shadows(self, force: bool = False) -> None:
        if not self.settings.shadows:
            return
        now = time.time()
        if not force and now - self._last_shadow_track < SHADOW_TRACK_SECONDS:
            return
        self._last_shadow_track = now
        active = self.store.active_shadows(SHADOW_MAX_ACTIVE + 30)
        if not active:
            return
        try:
            pairs = self.clients.dex.pairs_for([row["mint"] for row in active])
        except SourceError as exc:
            logger.info("Skyggehandler kunne ikke opdateres: %s", exc)
            return
        for row in active:
            shadow_id = int(row["id"])
            age = _minutes_since(row["ts"])
            path = loads(row["path"], []) or [[0.0, 1.0]]
            pair = pairs.get(row["mint"])
            rugged = bool(row["rugged"])
            if pair is None or pair.price_usd <= 0:
                misses = self._shadow_misses.get(shadow_id, 0) + 1
                self._shadow_misses[shadow_id] = misses
                if misses >= 30 or age >= SHADOW_HORIZON_MINUTES:
                    self._finish_shadow(row, path, rugged=misses >= 30)
                continue
            self._shadow_misses.pop(shadow_id, None)
            ratio = pair.price_usd / float(row["entry_price"])
            if (row["liquidity_entry"] and pair.liquidity_usd < float(row["liquidity_entry"]) * 0.4) or ratio < 0.2:
                rugged = True
            if age - float(path[-1][0]) >= 0.9 or len(path) == 1:
                path.append([round(age, 2), round(ratio, 5)])
            changes = {
                "path": path[-420:],
                "peak_ratio": max(float(row["peak_ratio"]), ratio),
                "low_ratio": min(float(row["low_ratio"]), ratio),
                "last_ratio": ratio,
                "rugged": rugged,
            }
            if rugged or age >= SHADOW_HORIZON_MINUTES:
                row.update(changes)
                self._finish_shadow(row, path, rugged=rugged)
            else:
                self.store.update_shadow(shadow_id, **changes)
        self.store.prune_shadows()

    def _finish_shadow(self, row: dict, path: list, *, rugged: bool) -> None:
        pnl, kind = simulate_path(path, rugged=rugged, **self._rules_for(str(row["strategy"])))
        self._shadow_misses.pop(int(row["id"]), None)
        self.store.update_shadow(
            int(row["id"]),
            status="done",
            path=path[-420:],
            rugged=rugged,
            sim_pnl_pct=pnl,
            sim_exit=kind,
            verdict=verdict_for(str(row["stage"]), pnl, rugged),
            done_at=now_iso(),
        )

    def _learn_shadows(self) -> None:
        rows = self.store.unlearned_shadows()
        if not rows:
            return
        changed = False
        for row in rows:
            self.store.update_shadow(int(row["id"]), learned=True)
            features = loads(row["features"], {}) or {}
            if features and row["stage"] in {"score", "claude", "risiko", "rute"} and row["strategy"] == "momentum":
                self.model.update(features, float(row["sim_pnl_pct"] or 0.0) > WIN_THRESHOLD_PCT, rate=SHADOW_LEARN_RATE)
                changed = True
        learned = int(self.store.get_state("shadows_learned", "0") or 0) + len(rows)
        self.store.set_state("shadows_learned", str(learned))
        if learned // 30 > (learned - len(rows)) // 30:
            done = [row for row in self.store.done_shadows(self.mode, limit=30) if row["stage"] != "købt"]
            if done:
                dodged = sum(1 for row in done if row["verdict"] == "dodged")
                missed = sum(1 for row in done if row["verdict"] == "missed")
                self.store.add_lesson(
                    "skygge",
                    f"Skyggehandler: af de seneste {len(done)} afviste tokens faldt {dodged} bagefter (godt valgt), "
                    f"og {missed} steg over 10 % (missede chancer).",
                )
                score_rows = [row for row in done if row["stage"] == "score"]
                if len(score_rows) >= 10:
                    missed_share = sum(1 for row in score_rows if row["verdict"] == "missed") / len(score_rows)
                    average = sum(float(row["sim_pnl_pct"] or 0) for row in score_rows) / len(score_rows)
                    if missed_share >= 0.5 and average >= 0.1 and "min_score" not in self._locked("momentum"):
                        old = self.params.min_score
                        self.params.min_score = clamp_param("min_score", old - 0.01)
                        if self.params.min_score != old:
                            why = (f"Min. score for køb: {old:.2f} → {self.params.min_score:.2f} "
                                   f"(halvdelen af de afviste tokens steg bagefter, i snit {average * 100:+.0f} %).")
                            self.store.add_lesson("parameter", why)
                            self._log_change("momentum", [("min_score", old, self.params.min_score)], why, "skygger")
                            changed = True
        if changed:
            self._save_learning()

    def _optimizer(self) -> dict:
        done = [row for row in self.store.done_shadows(self.mode, "sniper", 600) if row["stage"] in SHADOW_OPTIMIZER_STAGES][:300]
        key = (len(done), int(done[0]["id"]) if done else 0, self.sniper.sell_fraction, self.sniper.max_hold_minutes, self.sniper.take_profit_pct, self.sniper.stop_loss_pct)
        cached = self._optimizer_cache
        if cached and cached[0] == key and time.time() - cached[1] < 300:
            return cached[2]
        paths = [(loads(row["path"], []) or [[0.0, 1.0]], bool(row["rugged"])) for row in done]
        cost = self._cost_pct("sniper")
        result = optimize_targets(paths, frac=self.sniper.sell_fraction, max_hold=self.sniper.max_hold_minutes, cost_pct=cost)
        current = [
            simulate_path(path, tp=self.sniper.take_profit_pct, frac=self.sniper.sell_fraction, sl=self.sniper.stop_loss_pct,
                          max_hold=self.sniper.max_hold_minutes, rugged=rugged, cost_pct=cost, breakeven_after_partial=True)[0]
            for path, rugged in paths
        ]
        result["current"] = {
            "tp": self.sniper.take_profit_pct,
            "sl": self.sniper.stop_loss_pct,
            "n": len(current),
            "avg": sum(current) / len(current) if current else None,
            "win_rate": sum(1 for value in current if value > 0) / len(current) if current else None,
        }
        self._optimizer_cache = (key, time.time(), result)
        return result

    def _momentum_optimizer(self) -> dict:
        """Samme regnestykke som sniperens, men med den almindelige strategis salgsregler og skyggehandler."""
        done = [row for row in self.store.done_shadows(self.mode, "momentum", 600) if row["stage"] in SHADOW_OPTIMIZER_STAGES][:300]
        p = self.params
        key = (len(done), int(done[0]["id"]) if done else 0, p.partial_fraction, p.max_hold_minutes, p.take_profit_pct,
               p.stop_loss_pct, p.trail_activation_pct, p.trailing_stop_pct, p.stale_minutes)
        cached = self._mopt_cache
        if cached and cached[0] == key and time.time() - cached[1] < 300:
            return cached[2]
        paths = [(loads(row["path"], []) or [[0.0, 1.0]], bool(row["rugged"])) for row in done]
        rules = {"trail_activation": p.trail_activation_pct, "trailing": p.trailing_stop_pct, "stale": p.stale_minutes}
        cost = self._cost_pct("momentum")
        result = optimize_targets(paths, frac=p.partial_fraction, max_hold=p.max_hold_minutes, cost_pct=cost, rules=rules,
                                  targets=MOMENTUM_TARGET_GRID, stops=MOMENTUM_STOP_GRID)
        current = [
            simulate_path(path, tp=p.take_profit_pct, frac=p.partial_fraction, sl=p.stop_loss_pct, max_hold=p.max_hold_minutes,
                          rugged=rugged, cost_pct=cost, **rules)[0]
            for path, rugged in paths
        ]
        result["current"] = {
            "tp": p.take_profit_pct,
            "sl": p.stop_loss_pct,
            "n": len(current),
            "avg": sum(current) / len(current) if current else None,
            "win_rate": sum(1 for value in current if value > 0) / len(current) if current else None,
        }
        self._mopt_cache = (key, time.time(), result)
        return result

    def _shadow_view(self, row: dict) -> dict:
        return {
            "id": row["id"],
            "ts": row["ts"],
            "symbol": row["symbol"],
            "mint": row["mint"],
            "strategy": row["strategy"],
            "stage": row["stage"],
            "reason": row["reason"],
            "status": row["status"],
            "peak_pct": float(row["peak_ratio"] or 1) - 1,
            "low_pct": float(row["low_ratio"] or 1) - 1,
            "last_pct": float(row["last_ratio"] or 1) - 1,
            "sim_pnl_pct": row["sim_pnl_pct"],
            "sim_exit": EXIT_LABELS.get(str(row["sim_exit"] or ""), row["sim_exit"]),
            "verdict": row["verdict"],
            "rugged": bool(row["rugged"]),
            "minutes": round(_minutes_since(row["ts"])),
        }

    # ------------------------------------------------------------------ udvikler og topholdere
    def _watch_insiders(self, wallet, sol_usd: float) -> None:
        if not self.settings.dev_watch:
            return
        now = time.time()
        if now - self._last_insider_check < INSIDER_CHECK_SECONDS:
            return
        self._last_insider_check = now
        positions = self.store.open_positions(self.mode)
        if not positions:
            return
        rpc = self.clients.rpc
        if rpc is None:
            rpc = self.clients.rpc = SolanaRPC(self.settings.rpc_url)
            self.copy.rpc = rpc
        for pos in positions:
            safety = loads(pos["safety"], {}) or {}
            watch = safety.get("watch") or {}
            dev = str(watch.get("dev") or "")
            holders = [str(item) for item in watch.get("holders") or [] if item]
            owners = [owner for owner in [dev, *holders] if owner]
            if not owners:
                continue
            current: dict[str, int] = {}
            for owner in owners:
                try:
                    current[owner] = sum(row["amount"] for row in rpc.token_accounts(owner, pos["mint"]))
                except SourceError as exc:
                    logger.info("Kunne ikke læse %s: %s", owner, exc)
            base = safety.get("watch_base")
            if not isinstance(base, dict):
                safety["watch_base"] = current
                self.store.update_position(int(pos["id"]), safety=safety)
                continue
            dev_drop = 0.0
            if dev and float(base.get(dev, 0) or 0) > 0 and dev in current:
                dev_drop = 1.0 - current[dev] / float(base[dev])
            tracked = [owner for owner in holders if float(base.get(owner, 0) or 0) > 0 and owner in current]
            base_sum = sum(float(base[owner]) for owner in tracked)
            hold_drop = 1.0 - sum(current[owner] for owner in tracked) / base_sum if base_sum > 0 else 0.0
            if dev_drop >= 0.5 or hold_drop >= 0.4:
                who = f"udvikleren har solgt {dev_drop * 100:.0f} %" if dev_drop >= 0.5 else f"topholderne har solgt {hold_drop * 100:.0f} %"
                self._sell(pos, 1.0, "rug_dev", f"Insider-alarm: {who} af deres beholdning. Sælger alt.", wallet, sol_usd)

    # ------------------------------------------------------------------ dagsrapport
    def daily_report_text(self) -> str:
        status = self.status()
        k, g = status["kpis"], status["goal"]
        today = utc_day()
        closed_today = [row for row in self.store.closed_positions(self.mode) if str(row["closed_at"] or "")[:10] == today]
        wins = [row for row in closed_today if float(row["pnl_usd"]) > 0]
        lines = [
            f"Dagsrapport {today} ({'papir' if self.mode == 'paper' else 'RIGTIGE PENGE'})",
            f"Egenkapital {k['equity']:.2f} USD · i dag {k['today_pnl']:+.2f} · i alt {k['total_pnl']:+.2f}",
            f"Lukkede handler i dag: {len(closed_today)} ({len(wins)} vindere, {len(closed_today) - len(wins)} tabere)",
        ]
        if closed_today:
            best = max(closed_today, key=lambda row: float(row["pnl_usd"]))
            worst = min(closed_today, key=lambda row: float(row["pnl_usd"]))
            lines.append(f"Bedste {best['symbol']} {float(best['pnl_usd']):+.2f} · værste {worst['symbol']} {float(worst['pnl_usd']):+.2f}")
        snipes = [row for row in closed_today if row.get("strategy") == "sniper"]
        if snipes:
            lines.append(f"Sniper: {len(snipes)} snipes, {sum(1 for row in snipes if row['exit_reason'] == 'target')} ramte målet")
        lines.append(f"Åbne positioner: {k['open']} · AI i dag {k['ai_today']:.2f} USD")
        lines.append(f"Måned: netto {g['month_net']:+.2f} USD efter udgifter på {g['month_costs']:.2f} · mål {g['target']:.0f} USD")
        done_today = [row for row in self.store.done_shadows(self.mode, limit=400) if str(row["done_at"] or "")[:10] == today and row["stage"] != "købt"]
        if done_today:
            dodged = sum(1 for row in done_today if row["verdict"] == "dodged")
            missed = sum(1 for row in done_today if row["verdict"] == "missed")
            lines.append(f"Skyggehandler: {len(done_today)} afviste fulgt; {dodged} faldt, {missed} steg over 10 %")
        lessons = [row["text"] for row in self.store.recent_lessons(10) if str(row["ts"])[:10] == today][:3]
        if lessons:
            lines.append("Lært i dag:")
            lines.extend(f"• {text}" for text in lessons)
        return "\n".join(lines)

    def _maybe_daily_report(self) -> None:
        hour = self.settings.report_hour_utc
        if hour < 0:
            return
        now = datetime.now(timezone.utc)
        today = utc_day(now)
        if now.hour != hour or self.store.get_state("report_sent") == today:
            return
        self.store.set_state("report_sent", today)
        text = self.daily_report_text()
        self.store.set_json("last_report", {"ts": now_iso(), "text": text})
        self._notify(text)

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
            except RateLimited as exc:
                # Jupiter siger stop. Listen hentes igen senere; det er ikke en fejl i botten.
                logger.info("Jupiter %s sprunget over: %s", category, exc)
            except SourceError as exc:
                errors.append(f"Jupiter {category}: {exc}")
        try:
            for row in self.clients.dex.latest_boosts():
                add(str(row.get("tokenAddress")), "dex_boost", boosted=True)
        except RateLimited as exc:
            logger.info("DexScreener boosts sprunget over: %s", exc)
        except SourceError as exc:
            errors.append(f"DexScreener boosts: {exc}")
        try:
            for row in self.clients.dex.latest_profiles():
                add(str(row.get("tokenAddress")), "dex_profile")
        except RateLimited as exc:
            logger.info("DexScreener profiler sprunget over: %s", exc)
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
        self._marked_shadow_budget = SHADOW_MARKED_PER_SCAN
        self.last_scan = summary
        # Wallets, som top-trader-kopieringen følger, håndteres dér (hurtigere og med egne grænser).
        top_wallets = self._top_wallet_set()
        self.copy.wallets = tuple(w for w in self._all_wallets() if w not in top_wallets)
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
        except RateLimited as exc:
            # Intet er afvist endnu, så kandidaterne kommer bare med i næste scanning.
            summary["note"] = "DexScreener holder pause (rate limit). Kandidaterne tjekkes i næste runde."
            logger.info("DexScreener par sprunget over: %s", exc)
            return
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
            except RateLimited as exc:
                logger.info("Jupiter token-info sprunget over: %s", exc)
            except SourceError as exc:
                summary["errors"].append(f"Jupiter token-info: {exc}")

        scored: list[dict] = []
        rug_passed = 0
        for cand in deep:
            pair = cand["pair"]
            token: TokenInfo | None = cand.get("token")
            try:
                report = self.clients.rug.report(cand["mint"])
            except RateLimited as exc:
                # Uden rug-tjek købes der intet. Kandidaten afvises ikke, men tjekkes igen i næste scanning.
                summary["note"] = "RugCheck holder pause (rate limit). Resten af kandidaterne tjekkes i næste runde."
                logger.info("RugCheck sprunget over: %s", exc)
                break
            except SourceError as exc:
                summary["errors"].append(f"RugCheck: {exc}")
                report = None
            cand["report"] = report
            reasons = (
                rug_rejections(report, token, self.params)
                + token_quality_rejections(token, min_organic=self.params.min_organic_score, max_dev_mints=self.params.max_dev_mints)
                + insider_rejections(report, self.params.max_insiders)
            )
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

    def _snipe(self, wallet, sol_usd: float, day: str, *, entries_allowed: bool) -> None:
        """Find helt nye tokens, tjek dem hurtigt og køb dem, der består."""
        cfg = self.sniper
        summary: dict[str, Any] = {"checked": 0, "new": 0, "young": 0, "passed": 0, "bought": 0, "errors": [], "at": now_iso()}
        self.last_snipe = summary
        if not entries_allowed:
            summary["note"] = "Ingen nye køb (pause, nødstop eller tabsgrænse)."
            return
        now = time.time()
        seen = {mint: stamp for mint, stamp in (self.store.get_json("sniper_seen", {}) or {}).items() if now - float(stamp) < SNIPER_SEEN_HOURS * 3600}
        found: dict[str, dict] = {}
        try:
            for token in self.clients.jup.recent_tokens(60):
                found[token.mint] = {"mint": token.mint, "token": token}
        except RateLimited as exc:
            summary["note"] = "Jupiter holder pause (rate limit). Sniperen bruger DexScreener imens."
            logger.info("Jupiter nye tokens sprunget over: %s", exc)
        except SourceError as exc:
            summary["errors"].append(f"Jupiter nye tokens: {exc}")
        try:
            for row in self.clients.dex.latest_profiles():
                mint = str(row.get("tokenAddress") or "")
                if mint:
                    found.setdefault(mint, {"mint": mint, "token": None})
        except RateLimited as exc:
            logger.info("DexScreener profiler sprunget over: %s", exc)
        except SourceError as exc:
            summary["errors"].append(f"DexScreener profiler: {exc}")
        # Tokens, der var for unge sidste gang. De tjekkes, når de er gamle nok, også selv om de
        # er gledet ud af listen over de nyeste tokens imens.
        waiting = {
            mint: float(created)
            for mint, created in (self.store.get_json("sniper_waiting", {}) or {}).items()
            if mint not in seen and now - float(created) < cfg.max_age_minutes * 60
        }
        due = [mint for mint, created in waiting.items() if now - created >= cfg.min_age_seconds]
        for mint in due:
            found.setdefault(mint, {"mint": mint, "token": None})
        summary["checked"] = len(found)
        summary["waiting"] = len(waiting) - len(due)
        open_mints = {row["mint"] for row in self.store.open_positions(self.mode)}
        blocked = {row["mint"] for row in self._blacklist()}
        order = due + [mint for mint in found if mint not in waiting]
        fresh = [
            found[mint]
            for mint in order
            if mint not in seen and mint not in open_mints and mint not in blocked and mint not in QUOTE_MINTS and not self.store.ever_rugged(mint)
        ]
        summary["new"] = len(fresh)
        if not fresh:
            self.store.set_json("sniper_seen", seen)
            self._save_waiting(waiting)
            return
        try:
            pairs = self.clients.dex.pairs_for([cand["mint"] for cand in fresh][:60])
        except RateLimited as exc:
            summary["note"] = "DexScreener holder pause (rate limit). Sniperen prøver igen om lidt."
            logger.info("DexScreener par sprunget over: %s", exc)
            return
        except SourceError as exc:
            summary["errors"].append(f"DexScreener par: {exc}")
            return
        missing_info = [cand["mint"] for cand in fresh if cand["token"] is None and cand["mint"] in pairs]
        if missing_info:
            try:
                infos = self.clients.jup.token_infos(missing_info)
                for cand in fresh:
                    if cand["token"] is None:
                        cand["token"] = infos.get(cand["mint"])
            except RateLimited as exc:
                logger.info("Jupiter token-info sprunget over: %s", exc)
            except SourceError as exc:
                summary["errors"].append(f"Jupiter token-info: {exc}")
        now_ms = int(now * 1000)
        passed: list[dict] = []
        for cand in fresh:
            mint = cand["mint"]
            pair = pairs.get(mint)
            token: TokenInfo | None = cand["token"]
            created = (pair.created_at_ms if pair and pair.created_at_ms else 0) or (token.created_at_ms if token else 0)
            age = (now_ms - created) / 60_000 if created else None
            if pair is None or age is None:
                # DexScreener har måske ikke set poolen endnu. Prøv igen næste runde, hvis tokenet er ungt.
                if not (age is not None and age < cfg.max_age_minutes):
                    seen[mint] = now
                continue
            if age * 60 < cfg.min_age_seconds:
                if pair.dex_id.lower() not in BONDING_CURVE_DEXES:
                    waiting[mint] = created / 1000
                continue
            seen[mint] = now
            waiting.pop(mint, None)
            if age > cfg.max_age_minutes:
                continue
            summary["young"] += 1
            cand.update({"pair": pair, "source": "sniper", "strategy": "sniper", "copy": False, "boosted": False, "age_minutes": age})
            reasons = sniper_rejections(pair, token, cfg, age)
            if reasons:
                self._record(cand, "sniper", False, "; ".join(reasons[:3]))
                continue
            passed.append(cand)
        self.store.set_json("sniper_seen", seen)
        self._save_waiting(waiting)
        passed.sort(key=lambda item: item["pair"].liquidity_usd, reverse=True)
        rug_params = StrategyParams.from_dict({**self.params.to_dict(), "max_top10_pct": cfg.max_top10_pct})
        retry: list[str] = []
        for index, cand in enumerate(passed[:SNIPER_DEEP_CHECKS]):
            try:
                report = self.clients.rug.report(cand["mint"], max_age=60.0)
            except RateLimited as exc:
                # Ingen køb uden et frisk rug-tjek. Glem, at vi har set disse tokens, så de tjekkes igen.
                retry.extend(item["mint"] for item in passed[index:SNIPER_DEEP_CHECKS])
                summary["note"] = "RugCheck holder pause (rate limit). Sniperen tjekker tokenet igen om lidt."
                logger.info("RugCheck sprunget over: %s", exc)
                break
            except SourceError as exc:
                summary["errors"].append(f"RugCheck: {exc}")
                report = None
            cand["report"] = report
            reasons = rug_rejections(report, cand["token"], rug_params) + insider_rejections(report, self.params.max_insiders)
            if reasons:
                self._record(cand, "rug", False, "; ".join(reasons[:3]))
                continue
            features = build_features(cand["pair"], report=report, token=cand["token"], now_ms=now_ms)
            cand.update({"report": report, "buzz": None, "features": features, "score": self.model.score(features)})
            summary["passed"] += 1
            if self._try_buy(cand, wallet, sol_usd, day, strategy="sniper"):
                summary["bought"] += 1
                if summary["bought"] >= MAX_BUYS_PER_SCAN:
                    break
            elif cand.get("retry"):
                # Kun Jupiters kvote stoppede tjekket. Tokenet er ikke afvist; se på det igen næste runde.
                retry.append(cand["mint"])
        if retry:
            ages = {item["mint"]: float(item.get("age_minutes") or 0.0) for item in passed}
            for mint in retry:
                seen.pop(mint, None)
                waiting[mint] = now - ages.get(mint, 0.0) * 60  # tjekkes igen i næste runde
            self.store.set_json("sniper_seen", seen)
            self._save_waiting(waiting)

    def _save_waiting(self, waiting: dict[str, float]) -> None:
        newest = sorted(waiting.items(), key=lambda item: item[1], reverse=True)[:SNIPER_WAITING_MAX]
        self.store.set_json("sniper_waiting", dict(newest))

    # ------------------------------------------------------------------ strategi-vagt og automatisk mål
    def _holds(self) -> dict:
        return dict(self.store.get_json(f"strategy_holds:{self.mode}", {}) or {})

    def _on_hold(self, strategy: str) -> bool:
        hold = self._holds().get(strategy)
        if not hold:
            return False
        until = hold.get("until")
        return until is None or time.time() < float(until)

    def _hold_text(self, strategy: str) -> str:
        hold = self._holds().get(strategy) or {}
        label = STRATEGY_LABELS.get(strategy, strategy)
        until = hold.get("until")
        when = f" i {max(0.1, (float(until) - time.time()) / 3600):.0f} t. mere" if until else ", til du genoptager den"
        return f"{label} er på hold{when}. {hold.get('reason', '')}".strip()

    def _strategy_window(self, strategy: str, closed: list[dict]) -> dict:
        since = self.store.get_state(f"guard_since:{self.mode}:{strategy}", "") or ""
        rows = [
            row for row in closed
            if (row.get("strategy") or "momentum") == strategy
            and row.get("exit_reason") not in GUARD_SKIP_EXITS
            and str(row.get("closed_at") or "") >= since
        ]
        rows.sort(key=lambda row: str(row.get("closed_at") or ""))
        window = rows[-GUARD_WINDOW:]
        pnl = [float(row.get("pnl_usd") or 0.0) for row in window]
        gross_win = sum(value for value in pnl if value > 0)
        gross_loss = -sum(value for value in pnl if value < 0)
        return {
            "n": len(window),
            "pnl": sum(pnl),
            "wins": sum(1 for value in pnl if value > 0),
            "pf": gross_win / gross_loss if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0),
        }

    def _save_holds(self, holds: dict) -> None:
        self.store.set_json(f"strategy_holds:{self.mode}", holds)

    def _check_guard(self) -> None:
        """Sæt en strategi på hold, når dens seneste handler taber. Start den igen efter pausen."""
        holds = self._holds()
        now = time.time()
        changed = False
        for strategy, hold in list(holds.items()):
            until = hold.get("until")
            if until is not None and now >= float(until):
                holds.pop(strategy)
                changed = True
                # Efter pausen bedømmes strategien kun på nye handler.
                self.store.set_state(f"guard_since:{self.mode}:{strategy}", now_iso())
                self.store.add_event("info", f"{STRATEGY_LABELS.get(strategy, strategy)} kører igen efter strategi-vagtens pause.")
        closed = self.store.closed_positions(self.mode)
        if len(closed) != self._guard_seen:
            self._guard_seen = len(closed)
            for strategy, label in STRATEGY_LABELS.items():
                window = self._strategy_window(strategy, closed)
                strikes_key = f"guard_strikes:{self.mode}:{strategy}"
                if window["n"] >= GUARD_MIN_TRADES and window["pnl"] > 0:
                    self.store.set_state(strikes_key, "0")  # den tjener igen: næste pause bliver kort
                if not self.guard_on or strategy in holds:
                    continue
                if window["n"] >= GUARD_MIN_TRADES and window["pnl"] < 0 and window["pf"] < GUARD_MAX_PF:
                    strikes = int(self.store.get_state(strikes_key, "0") or 0) + 1
                    hours = min(GUARD_MAX_HOURS, GUARD_HOLD_HOURS * 2 ** (strikes - 1))
                    reason = (
                        f"De seneste {window['n']} handler gav {_dk(window['pnl'])} USD "
                        f"({window['wins']} vindere, profitfaktor {_dk(window['pf'])})."
                    )
                    holds[strategy] = {"until": now + hours * 3600, "since": now_iso(), "reason": reason, "hours": hours, "auto": True}
                    self.store.set_state(strikes_key, str(strikes))
                    changed = True
                    message = f"Strategi-vagt: {label} er sat på hold i {hours} timer. {reason}"
                    self.store.add_event("warning", message)
                    self.store.add_lesson("parameter", message + " Den følger stadig tokens som skyggehandler.")
                    self._notify(message)
        if changed:
            self._save_holds(holds)

    def set_strategy(self, changes: dict) -> None:
        """Fra dashboardet: hold en strategi, genoptag den, eller slå vagten og det automatiske mål til og fra."""
        if not isinstance(changes, dict):
            raise ValueError("Ugyldige indstillinger.")
        holds = self._holds()
        for key in ("hold", "resume"):
            strategy = changes.get(key)
            if strategy is None:
                continue
            if strategy not in STRATEGY_LABELS:
                raise ValueError("Ukendt strategi.")
            label = STRATEGY_LABELS[strategy]
            if key == "hold":
                holds[strategy] = {"until": None, "since": now_iso(), "reason": "Sat på hold af dig.", "auto": False}
                self.store.add_event("info", f"{label} er sat på hold.")
            else:
                holds.pop(strategy, None)
                self.store.set_state(f"guard_since:{self.mode}:{strategy}", now_iso())
                self.store.add_event("info", f"{label} er genoptaget. Strategi-vagten bedømmer den kun på nye handler.")
        self._save_holds(holds)
        if "guard" in changes:
            self.guard_on = bool(changes["guard"])
            self.store.set_state("strategy_guard", "1" if self.guard_on else "0")
        if "auto_targets" in changes:
            self.auto_targets = bool(changes["auto_targets"])
            self.store.set_state("auto_targets", "1" if self.auto_targets else "0")
        self._guard_seen = -1

    def _maybe_auto_targets(self) -> None:
        """Skift sniperens mål og stop, når skyggehandlerne klart viser et bedre sæt, der giver overskud."""
        if not self.auto_targets or not self.settings.shadows:
            return
        now = time.time()
        if now - self._last_auto_target_check < AUTO_TARGET_CHECK_SECONDS:
            return
        self._last_auto_target_check = now
        self._auto_tighten(now)
        self._auto_momentum_targets(now)
        if now - float(self.store.get_state("auto_target_at", "0") or 0) < AUTO_TARGET_EVERY_SECONDS:
            return
        if self._locked("sniper") & {"take_profit_pct", "stop_loss_pct"}:
            return  # du har selv sat målet eller stoppet
        opt = self._optimizer()
        best, current = opt.get("best") or {}, opt.get("current") or {}
        if not self._clearly_better(opt):
            return
        old_tp, old_sl = self.sniper.take_profit_pct, self.sniper.stop_loss_pct
        self.update_sniper({"take_profit_pct": best["tp"], "stop_loss_pct": best["sl"]}, manual=False)
        self.store.set_state("auto_target_at", repr(now))
        message = (
            f"Sniperens mål er ændret fra +{old_tp * 100:.0f} % / -{old_sl * 100:.0f} % til "
            f"+{self.sniper.take_profit_pct * 100:.0f} % / -{self.sniper.stop_loss_pct * 100:.0f} %. "
            f"På de seneste {opt['n']} kursforløb gav det {best['avg'] * 100:+.1f} % pr. snipe i stedet for {current['avg'] * 100:+.1f} %."
        )
        self._log_change("sniper", [("take_profit_pct", old_tp, self.sniper.take_profit_pct), ("stop_loss_pct", old_sl, self.sniper.stop_loss_pct)], message, "mål")
        self.store.add_event("info", message)
        self.store.add_lesson("parameter", message)

    @staticmethod
    def _clearly_better(opt: dict) -> bool:
        best, current = opt.get("best") or {}, opt.get("current") or {}
        if opt.get("n", 0) < AUTO_TARGET_MIN_PATHS or best.get("avg") is None or current.get("avg") is None:
            return False
        if best["avg"] <= 0 or best["avg"] < current["avg"] + AUTO_TARGET_GAIN:
            return False
        return abs(best["tp"] - current["tp"]) > 1e-9 or abs(best["sl"] - current["sl"]) > 1e-9

    def _auto_momentum_targets(self, now: float) -> None:
        """Den almindelige strategi lærer også sit mål og stop af de kursforløb, botten har fulgt."""
        if now - float(self.store.get_state("auto_target_at:momentum", "0") or 0) < AUTO_TARGET_EVERY_SECONDS:
            return
        if self._locked("momentum") & {"take_profit_pct", "stop_loss_pct"}:
            return
        opt = self._momentum_optimizer()
        if not self._clearly_better(opt):
            return
        best, current = opt["best"], opt["current"]
        old_tp, old_sl = self.params.take_profit_pct, self.params.stop_loss_pct
        self.params.take_profit_pct = clamp_param("take_profit_pct", best["tp"])
        self.params.stop_loss_pct = clamp_param("stop_loss_pct", best["sl"])
        self._save_learning()
        self._mopt_cache = None
        self.store.set_state("auto_target_at:momentum", repr(now))
        message = (
            f"Den almindelige strategis mål er ændret fra +{old_tp * 100:.0f} % / -{old_sl * 100:.0f} % til "
            f"+{self.params.take_profit_pct * 100:.0f} % / -{self.params.stop_loss_pct * 100:.0f} %. "
            f"På de seneste {opt['n']} kursforløb gav det {best['avg'] * 100:+.1f} % pr. handel i stedet for {current['avg'] * 100:+.1f} %."
        )
        self._log_change("momentum", [("take_profit_pct", old_tp, self.params.take_profit_pct), ("stop_loss_pct", old_sl, self.params.stop_loss_pct)], message, "mål")
        self.store.add_event("info", message)
        self.store.add_lesson("parameter", message)

    def _shadow_rows(self, strategy: str) -> list[dict]:
        """Afsluttede skyggehandler med fakta og simuleret resultat, nyeste først."""
        rows = []
        for row in self.store.done_shadows(self.mode, strategy, ANALYSIS_ROWS):
            facts = loads(row.get("facts"), {}) or {}
            if facts and row.get("sim_pnl_pct") is not None and not facts.get("sus"):
                rows.append({"facts": facts, "pnl": float(row["sim_pnl_pct"]), "stage": row["stage"]})
        return rows

    def _auto_tighten(self, now: float) -> None:
        """Stram ét filter pr. strategi pr. døgn, når skyggehandlerne klart viser, at grænsen lukker tabere ind."""
        for strategy, target in (("momentum", self.params), ("sniper", self.sniper)):
            key = f"auto_tighten_at:{strategy}"
            if now - float(self.store.get_state(key, "0") or 0) < AUTO_TARGET_EVERY_SECONDS:
                continue
            rules = TIGHTEN_RULES[strategy]
            current = {rule["param"]: float(getattr(target, rule["param"])) for rule in rules}
            best = tighten_suggestion(self._shadow_rows(strategy), current, rules, frozen=self._locked(strategy))
            if not best:
                continue
            name = best["param"]
            if strategy == "sniper":
                self.update_sniper({name: best["new"]}, manual=False)
                new, label = getattr(self.sniper, name), SNIPER_LABELS.get(name, name)
            else:
                setattr(self.params, name, clamp_param(name, best["new"]))
                self._save_learning()
                self._mopt_cache = None
                new, label = getattr(self.params, name), PARAM_LABELS.get(name, name)
            if abs(float(new) - best["old"]) < 1e-9:
                continue
            self.store.set_state(key, repr(now))
            message = (
                f"{STRATEGY_LABELS[strategy]}: {label} strammet fra {_dk(best['old'], 2 if best['old'] < 10 else 0)} "
                f"til {_dk(float(new), 2 if float(new) < 10 else 0)}. De {best['removed']} tokens, den nye grænse ville have "
                f"sorteret fra, gav i snit {best['removed_avg'] * 100:+.1f} %; de øvrige {best['kept']} gav {best['kept_avg'] * 100:+.1f} %."
            )
            log = list(self.store.get_json("auto_tighten_log", []) or [])
            log.append({"at": now_iso(), "strategy": strategy, "label": label, **best, "new": float(new)})
            self.store.set_json("auto_tighten_log", log[-TIGHTEN_LOG_KEPT:])
            self._log_change(strategy, [(name, best["old"], float(new))], message, "strammer")
            self._analysis_cache = None
            self.store.add_event("info", message)
            self.store.add_lesson("parameter", message)

    def _analysis(self) -> dict:
        """Hvad virker? Resultat pr. egenskab for hver strategi (fra skyggehandlerne). Gemmes i 5 min."""
        cached = self._analysis_cache
        if cached and time.time() - cached[0] < ANALYSIS_CACHE_SECONDS:
            return cached[1]
        result: dict[str, Any] = {"min_rows": FACT_MIN_ROWS, "strategies": []}
        for strategy in ("momentum", "sniper", "toptrader"):
            rows = self._shadow_rows(strategy)
            bought = [row["pnl"] for row in rows if row["stage"] == "købt"]
            skipped = [row["pnl"] for row in rows if row["stage"] != "købt"]
            result["strategies"].append({
                "strategy": strategy,
                "label": STRATEGY_LABELS[strategy],
                "n": len(rows),
                "bought": {"n": len(bought), "avg": sum(bought) / len(bought) if bought else None},
                "skipped": {"n": len(skipped), "avg": sum(skipped) / len(skipped) if skipped else None},
                "facts": fact_report(rows)[:8],
            })
        result["tightened"] = list(reversed(self.store.get_json("auto_tighten_log", []) or []))[:6]
        self._analysis_cache = (time.time(), result)
        return result

    def export_data(self) -> dict:
        """Alt, hvad der skal til for at analysere bottens resultater udefra. Ingen hemmeligheder."""
        def position(row: dict) -> dict:
            return {
                key: (loads(row[key], None) if key in {"features", "safety", "ai"} else row[key])
                for key in row
                if key not in {"signatures", "token_raw", "remaining_raw"}
            }

        def shadow(row: dict) -> dict:
            path = loads(row.get("path"), []) or []
            step = max(1, len(path) // 60)
            return {
                **{key: row[key] for key in ("id", "ts", "mint", "symbol", "strategy", "stage", "reason", "entry_price", "liquidity_entry",
                                             "peak_ratio", "low_ratio", "last_ratio", "rugged", "status", "sim_pnl_pct", "sim_exit", "verdict", "done_at")},
                "facts": loads(row.get("facts"), {}) or {},
                "features": loads(row.get("features"), {}) or {},
                "path": path[::step],
            }

        # Uden motorens lås: databasen har sin egen, og handlen skal ikke vente på en stor eksport.
        return {
            "exported_at": now_iso(),
            "mode": self.mode,
            "settings": public_meme_settings(self.settings),
            "params": self.params.to_dict(),
            "sniper": self.sniper.to_dict(),
            "toptraders": self.top_cfg.to_dict(),
            "model": self.model.to_dict(),
            "positions": [position(row) for row in self.store.all_positions(self.mode)],
            "archived_positions": [position(row) for row in self.store.archived_paper_positions()],
            "shadows": [shadow(row) for row in self.store.done_shadows(self.mode, None, 3000)],
            "lessons": self.store.recent_lessons(200),
            "tightened": self.store.get_json("auto_tighten_log", []) or [],
        }

    def _strategies_status(self, closed: list[dict], open_rows: list[dict]) -> list[dict]:
        holds = self._holds()
        now = time.time()
        enabled = {"momentum": True, "sniper": self.sniper.enabled, "toptrader": self.top_cfg.enabled}
        out = []
        for strategy, label in STRATEGY_LABELS.items():
            rows = [row for row in closed if (row.get("strategy") or "momentum") == strategy]
            pnl = [float(row.get("pnl_usd") or 0.0) for row in rows]
            window = self._strategy_window(strategy, closed)
            hold = holds.get(strategy) if self._on_hold(strategy) else None
            out.append({
                "id": strategy,
                "label": label,
                "enabled": enabled[strategy],
                "trades": len(rows),
                "win_rate": (sum(1 for value in pnl if value > 0) / len(rows)) if rows else None,
                "pnl": sum(pnl),
                "open": sum(1 for row in open_rows if (row.get("strategy") or "momentum") == strategy),
                "recent": {"n": window["n"], "pnl": window["pnl"], "pf": window["pf"] if window["pf"] < 99 else None},
                "hold": None if hold is None else {
                    "reason": hold.get("reason", ""),
                    "auto": bool(hold.get("auto")),
                    "hours_left": round((float(hold["until"]) - now) / 3600, 1) if hold.get("until") else None,
                },
            })
        return out

    # ------------------------------------------------------------------ top-traders
    def _ensure_rpc(self):
        if self.clients.rpc is None:
            self.clients.rpc = SolanaRPC(self.settings.rpc_url)
        return self.clients.rpc

    def _own_traders(self) -> list[dict]:
        return list(self.store.get_json("top_own", []) or [])

    def _own_leaders(self) -> list[dict]:
        return [
            {"wallet": row["wallet"], "name": row.get("name", ""), "rank": i + 1, "pnl_usd": None, "win_rate": None, "trades": None}
            for i, row in enumerate(self._own_traders()[:TOP_OWN_MAX])
        ]

    def _manual_leaders(self) -> list[dict]:
        if self._own_traders():
            return self._own_leaders()
        wallets = list(self._all_wallets())[: self.top_cfg.count]
        return [{"wallet": w, "rank": i + 1, "pnl_usd": None, "win_rate": None, "trades": None} for i, w in enumerate(wallets)]

    def _top_source(self) -> str:
        if self._own_traders():
            return "own"
        return "api" if self.clients.top is not None else "followed"

    def _top_leaders(self) -> list[dict]:
        """Dagens top-traders fra Solana Tracker. Uden nøgle: de wallets, du selv følger."""
        cfg = self.top_cfg
        if self.clients.top is None or self._own_traders():
            return self._manual_leaders()
        cached = self.store.get_json("top_traders", None) or {}
        fresh = cached.get("days") == cfg.days and time.time() - float(cached.get("ts") or 0) < TOP_REFRESH_SECONDS
        # Hent igen hver halve time, eller hvis du har bedt om flere traders end listen blev hentet med.
        if fresh and int(cached.get("count") or 0) >= cfg.count:
            return cached["traders"][: cfg.count]
        try:
            traders = self.clients.top.top_traders(cfg.days, cfg.count)
        except SourceError as exc:
            self.top_last["error"] = f"Solana Tracker: {exc}"
            return (cached.get("traders") or [])[: cfg.count]
        rows = [{"wallet": t.wallet, "rank": t.rank, "pnl_usd": t.pnl_usd, "win_rate": t.win_rate, "trades": t.trades} for t in traders]
        before = {row["wallet"] for row in cached.get("traders") or []}
        added = [row for row in rows if row["wallet"] not in before]
        self.store.set_json("top_traders", {"ts": time.time(), "at": now_iso(), "days": cfg.days, "count": cfg.count, "traders": rows})
        self.top_last.pop("error", None)
        if before and added:
            self.store.add_event("info", f"Listen over top-traders er opdateret: {len(added)} nye.")
        return rows[: cfg.count]

    def _top_wallet_set(self) -> set[str]:
        """Hvem top-trader-kopieringen følger lige nu, uden at kalde API'et."""
        if not self.top_cfg.enabled:
            return set()
        if self.clients.top is None or self._own_traders():
            return {row["wallet"] for row in self._manual_leaders()}
        cached = self.store.get_json("top_traders", None) or {}
        return {row["wallet"] for row in (cached.get("traders") or [])[: self.top_cfg.count]}

    def _held_top_wallets(self) -> list[str]:
        return [
            str(row["source"])[5:]
            for row in self.store.open_positions(self.mode)
            if row.get("strategy") == "toptrader" and str(row.get("source") or "").startswith("copy:")
        ]

    def _note_top(self, signal, action: str, detail: str, symbol: str = "") -> None:
        self.top_signals.appendleft(
            {
                "ts": now_iso(),
                "wallet": signal.wallet,
                "mint": signal.mint,
                "symbol": symbol,
                "side": signal.side,
                "sol": round(float(signal.sol), 4),
                "action": action,
                "detail": detail,
            }
        )
        self.store.set_json("top_signals", list(self.top_signals))

    def _follow_top(self, wallet, sol_usd: float, day: str, *, entries_allowed: bool) -> None:
        """Kig efter nye handler fra top-traderne: køb lige efter dem, og sælg når de sælger."""
        cfg = self.top_cfg
        held = self._held_top_wallets()
        leaders = self._top_leaders() if cfg.enabled else []
        watch = list(dict.fromkeys([row["wallet"] for row in leaders] + held))
        self.top_last.update({"at": now_iso(), "watching": len(watch)})
        if not watch:
            return
        self.top_copy.rpc = self._ensure_rpc()
        self.top_copy.wallets = tuple(watch)
        stats = source_stats(self.store.closed_positions(self.mode))
        muted = {source for source, entry in stats.items() if entry["muted"]}
        try:
            signals = self.top_copy.poll(muted)
        except Exception as exc:  # én dårlig wallet eller RPC-fejl må ikke stoppe runden
            self.top_last["error"] = f"Kunne ikke læse traderne: {exc}"
            return
        leader_set = {row["wallet"] for row in leaders}
        open_rows = self.store.open_positions(self.mode)
        held_pairs = {(str(row["source"])[5:], row["mint"]): row for row in open_rows if str(row.get("source") or "").startswith("copy:")}
        sold = 0
        for signal in signals:
            if signal.side != "sell":
                continue
            row = held_pairs.get((signal.wallet, signal.mint))
            if row is None:
                continue
            if cfg.follow_sells or row.get("strategy") != "toptrader":
                self._copy_sells.add((signal.wallet, signal.mint))
                self._note_top(signal, "sælger", "Traderen solgte. Botten sælger også.", row.get("symbol") or "")
                sold += 1
        if sold:
            self._monitor(wallet, sol_usd)
        buys = [signal for signal in signals if signal.side == "buy" and signal.wallet in leader_set]
        bought = 0
        for signal in buys:
            if not entries_allowed:
                self._note_top(signal, "sprunget over", "Ingen nye køb lige nu (pause, nødstop eller tabsgrænse).")
                continue
            if self._copy_buy(signal, wallet, sol_usd, day, leaders):
                bought += 1
        self.top_last.update({"buys": len(buys), "sells": sold, "bought": bought})

    def _copy_buy(self, signal, wallet, sol_usd: float, day: str, leaders: list[dict]) -> bool:
        cfg = self.top_cfg
        mint = signal.mint
        age = max(0.0, time.time() - signal.block_time) if signal.block_time else 0.0
        if signal.block_time and age > cfg.max_signal_age_seconds:
            self._note_top(signal, "sprunget over", f"Deres køb er {age:.0f} s gammelt. Grænsen er {cfg.max_signal_age_seconds:.0f} s.")
            return False
        if signal.sol < cfg.min_leader_sol:
            self._note_top(signal, "sprunget over", f"Lille køb på {_dk(signal.sol, 3)} SOL (under {_dk(cfg.min_leader_sol)} SOL).")
            return False
        if mint in QUOTE_MINTS or self.store.ever_rugged(mint) or mint in {row["mint"] for row in self._blacklist()}:
            self._note_top(signal, "sprunget over", "Tokenet er blokeret eller har rugget før.")
            return False
        if any(row["mint"] == mint for row in self.store.open_positions(self.mode)):
            self._note_top(signal, "sprunget over", "Botten ejer allerede tokenet.")
            return False
        try:
            pair = self.clients.dex.pairs_for([mint]).get(mint)
        except SourceError as exc:
            self._note_top(signal, "sprunget over", f"Ingen kurs lige nu: {exc}")
            return False
        if pair is None or pair.price_usd <= 0:
            self._note_top(signal, "sprunget over", "Intet handelspar på DexScreener endnu.")
            return False
        leader = next((row for row in leaders if row["wallet"] == signal.wallet), {})
        cand: dict[str, Any] = {
            "mint": mint,
            "pair": pair,
            "source": f"copy:{signal.wallet}",
            "copy": True,
            "boosted": False,
            "token": None,
            "strategy": "toptrader",
            "leader": {
                "rank": leader.get("rank"),
                "pnl_usd": leader.get("pnl_usd"),
                "sol": float(signal.sol),
                "age": age,
                "token_raw": int(getattr(signal, "token_raw", 0) or 0),
            },
        }
        if pair.liquidity_usd < cfg.min_liquidity_usd:
            reason = f"Top-trader købte, men likviditeten er kun {_dk(pair.liquidity_usd, 0)} $ (grænse {_dk(cfg.min_liquidity_usd, 0)} $)."
            self._record(cand, "marked", False, reason)
            self._note_top(signal, "sprunget over", reason, pair.symbol)
            return False
        try:
            report = self.clients.rug.report(mint, max_age=300.0)
        except RateLimited:
            self._note_top(signal, "sprunget over", "RugCheck holder pause. Uden rug-tjek købes der ikke.", pair.symbol)
            return False
        except SourceError:
            report = None
        reasons = rug_rejections(report, None, self.params)
        if reasons:
            self._record(cand, "rug", False, "; ".join(reasons[:3]))
            self._note_top(signal, "sprunget over", "Rug-tjek: " + "; ".join(reasons[:2]), pair.symbol)
            return False
        features = build_features(pair, report=report, copy_signal=True, now_ms=int(time.time() * 1000))
        cand.update({"report": report, "buzz": None, "features": features, "score": self.model.score(features)})
        if self._try_buy(cand, wallet, sol_usd, day, strategy="toptrader"):
            self._note_top(signal, "købt", f"Købt {pair.symbol} for {_dk(cfg.position_usd)} USD, {age:.0f} s efter traderen.", pair.symbol)
            return True
        latest = self.store.recent_candidates(self.mode, 1)
        why = latest[0]["reason"] if latest and latest[0]["mint"] == mint else "Ikke købt."
        if cand.get("retry"):
            why = "Jupiter holder pause (rate limit). Købet blev ikke sendt."
        self._note_top(signal, "sprunget over", why, pair.symbol)
        return False

    def add_top_traders(self, lines) -> dict:
        """Din egen liste. Én trader pr. linje: adressen, gerne efterfulgt af et navn (fx «7xKX… Ansem»)."""
        if isinstance(lines, str):
            lines = lines.splitlines()
        own = self._own_traders()
        known = {row["wallet"] for row in own}
        added, errors = 0, []
        for raw in lines or []:
            parts = str(raw).replace(",", " ").split(None, 1)
            if not parts:
                continue
            wallet, name = parts[0].strip(), (parts[1].strip()[:40] if len(parts) > 1 else "")
            if not _valid_address(wallet):
                errors.append(f"{wallet[:8]}…: ikke en Solana-adresse")
                continue
            if wallet in known:
                errors.append(f"{wallet[:4]}…{wallet[-4:]}: er allerede på listen")
                continue
            if len(own) >= TOP_OWN_MAX:
                errors.append(f"Listen er fuld ({TOP_OWN_MAX} traders).")
                break
            own.append({"wallet": wallet, "name": name, "added": now_iso()})
            known.add(wallet)
            added += 1
        if added:
            self.store.set_json("top_own", own)
            self.store.add_event("info", f"{added} {'trader' if added == 1 else 'traders'} tilføjet til din Top 10-liste.")
        if not added and errors:
            raise ValueError("; ".join(errors[:3]))
        return {"added": added, "errors": errors}

    def remove_top_trader(self, wallet: str) -> None:
        own = self._own_traders()
        rest = [row for row in own if row["wallet"] != str(wallet).strip()]
        if len(rest) == len(own):
            raise ValueError("Den trader er ikke på din liste.")
        self.store.set_json("top_own", rest)
        self.store.add_event("info", f"{str(wallet)[:4]}…{str(wallet)[-4:]} er fjernet fra din Top 10-liste. Åbne kopier sælges stadig, når den sælger.")

    def update_toptraders(self, changes: dict) -> None:
        if not isinstance(changes, dict):
            raise ValueError("Ugyldige indstillinger.")
        if "add_traders" in changes:
            self.add_top_traders(changes["add_traders"])
        if "remove_trader" in changes:
            self.remove_top_trader(str(changes["remove_trader"]))
        updated = dataclasses.replace(self.top_cfg)
        for key, value in changes.items():
            if key in TOP_BOOLS:
                setattr(updated, key, bool(value))
            elif key in TOP_BOUNDS:
                try:
                    setattr(updated, key, clamp_top(key, float(value), self.settings.position_usd))
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{TOP_LABELS.get(key, key)} skal være et tal.") from exc
        was_on = self.top_cfg.enabled
        reload_list = updated.days != self.top_cfg.days or updated.count > self.top_cfg.count
        # Byt hele objektet på én gang: ingen halve ændringer midt i en runde.
        self.top_cfg = updated
        self.store.set_json("toptraders", updated.to_dict())
        if reload_list:
            self.store.set_json("top_traders", {})
        if updated.enabled != was_on:
            self.store.add_event(
                "info",
                "Top-trader-kopiering er slået til." if updated.enabled else "Top-trader-kopiering er slået fra. Åbne kopier sælges stadig, når traderen sælger.",
            )
            self._last_top_poll = 0.0
        self._wake.set()

    def _top_status(self, closed: list[dict], open_rows: list[dict], day: str) -> dict:
        cfg = self.top_cfg
        defaults = top_from({}, self.settings)
        copies = [row for row in closed if row.get("strategy") == "toptrader"]
        pnl = [float(row["pnl_usd"]) for row in copies]
        wins = [value for value in pnl if value > 0]
        kinds = {
            "count": "int", "days": "days", "position_usd": "usd", "max_open": "int", "max_per_day": "int", "stop_loss_pct": "pct",
            "max_hold_minutes": "min", "min_liquidity_usd": "usd", "max_signal_age_seconds": "sec", "min_leader_sol": "sol",
            "max_chase_pct": "pct",
        }
        fields = []
        for name, (low, high) in TOP_BOUNDS.items():
            if name == "position_usd":
                high = min(high, self.settings.position_usd)
            fields.append({"name": name, "label": TOP_LABELS[name], "value": getattr(cfg, name), "default": getattr(defaults, name), "bounds": [low, high], "kind": kinds[name]})
        for name in ("follow_sells", "stop_loss_on", "daily_loss_stop"):
            fields.append({"name": name, "label": TOP_LABELS[name], "value": getattr(cfg, name), "default": getattr(defaults, name), "kind": "bool"})
        api = self.clients.top is not None
        source = self._top_source()
        cached = (self.store.get_json("top_traders", None) or {}) if source == "api" else {}
        leaders = (cached.get("traders") or [])[: cfg.count] if source == "api" else self._manual_leaders()
        stats = source_stats(closed)
        held = {str(row["source"])[5:] for row in open_rows if row.get("strategy") == "toptrader"}
        view = []
        for row in leaders:
            ours = stats.get(f"copy:{row['wallet']}", {})
            view.append({**row, "copied": ours.get("trades", 0), "copied_pnl": ours.get("pnl", 0.0), "muted": bool(ours.get("muted")), "holding": row["wallet"] in held})
        return {
            "enabled": cfg.enabled,
            "settings": cfg.to_dict(),
            "fields": fields,
            "api": api,
            "source": {"own": "din egen liste", "api": "Solana Tracker", "followed": "dine fulgte wallets"}[source],
            "source_kind": source,
            "own": self._own_traders(),
            "own_max": TOP_OWN_MAX,
            "refreshed_at": cached.get("at"),
            "leaders": view,
            "signals": list(self.top_signals)[:25],
            "last": dict(self.top_last),
            "poll_seconds": TOP_POLL_SECONDS,
            "stats": {
                "trades": len(copies),
                "wins": len(wins),
                "win_rate": len(wins) / len(copies) if copies else None,
                "pnl": sum(pnl),
                "followed_exits": sum(1 for row in copies if row.get("exit_reason") == "copy_exit"),
                "open": sum(1 for row in open_rows if row.get("strategy") == "toptrader"),
                "today": self.store.trades_opened_on(self.mode, day, "toptrader"),
            },
        }

    def _save_sniper(self) -> None:
        self.store.set_json("sniper", self.sniper.to_dict())

    def update_sniper(self, changes: dict, *, manual: bool = True) -> None:
        """manual=True: ændret af dig (siden eller Telegram). Så rører læringen ikke tallene i en uge."""
        if not isinstance(changes, dict):
            raise ValueError("Ugyldige indstillinger.")
        # Byg en ny kopi og skift den ind på én gang: ingen halve ændringer, og ingen ventetid på runden.
        updated = dataclasses.replace(self.sniper)
        notes = []
        for key, value in changes.items():
            if key in SNIPER_BOOLS:
                new = bool(value)
            elif key in SNIPER_BOUNDS:
                try:
                    new = clamp_sniper(key, float(value), self.settings.position_usd)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{SNIPER_LABELS.get(key, key)} skal være et tal.") from exc
            else:
                continue
            if getattr(updated, key) != new:
                setattr(updated, key, new)
                notes.append(SNIPER_LABELS.get(key, key))
        if not notes:
            return
        changed = [key for key in SNIPER_BOUNDS if getattr(updated, key) != getattr(self.sniper, key)]
        self.sniper = updated
        self._optimizer_cache = None
        self._save_sniper()
        if manual and changed:
            self._lock_params("sniper", changed)
        self.store.add_event(
            "info",
            f"Sniper-indstillinger ændret: {', '.join(notes)}. Gælder også åbne snipes.",
            f"Mål +{self.sniper.take_profit_pct * 100:.0f} %, stop -{self.sniper.stop_loss_pct * 100:.0f} %, "
            f"{self.sniper.position_usd:.2f} USD pr. snipe.",
        )

    def reset_sniper(self) -> None:
        self.sniper = sniper_from({"enabled": self.sniper.enabled}, self.settings)
        self._unlock_strategy("sniper")
        self._optimizer_cache = None
        self._save_sniper()
        self.store.add_event("info", "Sniper-indstillingerne er sat tilbage til standard.")

    def _sniper_status(self, closed: list[dict], open_rows: list[dict], day: str) -> dict:
        defaults = sniper_from({}, self.settings)
        snipes = [row for row in closed if row.get("strategy") == "sniper"]
        pnl = [float(row["pnl_usd"]) for row in snipes]
        wins = [value for value in pnl if value > 0]
        kinds = {
            "take_profit_pct": "pct", "sell_fraction": "pct", "stop_loss_pct": "pct", "max_hold_minutes": "min",
            "position_usd": "usd", "min_age_seconds": "sec", "max_age_minutes": "min", "min_liquidity_usd": "usd",
            "min_holders": "int", "max_top10_pct": "int", "min_organic_score": "int", "max_dev_mints": "int",
        }
        fields = []
        for name, (low, high) in SNIPER_BOUNDS.items():
            if name == "position_usd":
                high = min(high, self.settings.position_usd)
            fields.append({
                "name": name,
                "label": SNIPER_LABELS[name],
                "value": getattr(self.sniper, name),
                "default": getattr(defaults, name),
                "bounds": [low, high],
                "kind": kinds[name],
                "learnable": name in SNIPER_LEARNABLE,
            })
        for name in SNIPER_BOOLS:
            fields.append({"name": name, "label": SNIPER_LABELS[name], "value": getattr(self.sniper, name), "default": getattr(defaults, name), "kind": "bool"})
        return {
            "enabled": self.sniper.enabled,
            "settings": self.sniper.to_dict(),
            "fields": fields,
            "stats": {
                "trades": len(snipes),
                "wins": len(wins),
                "win_rate": len(wins) / len(snipes) if snipes else None,
                "pnl": sum(pnl),
                "targets": sum(1 for row in snipes if row.get("exit_reason") == "target"),
                "open": sum(1 for row in open_rows if row.get("strategy") == "sniper"),
                "today": self.store.trades_opened_on(self.mode, day, "sniper"),
            },
            "last": self.last_snipe,
            "scan_seconds": self.settings.sniper_scan_seconds,
            "max_open": self.settings.sniper_max_open,
            "max_per_day": self.settings.sniper_max_per_day,
            "position_cap": self.settings.position_usd,
        }

    def _try_buy(self, cand: dict, wallet, sol_usd: float, day: str, strategy: str = "momentum") -> bool:
        pair: PairInfo = cand["pair"]
        if self._on_hold(strategy):
            # Ingen køb, men kandidaten skrives ned, så skyggehandlerne lærer videre af den.
            self._record(cand, "risiko", False, self._hold_text(strategy), cand.get("score"), cand.get("features"))
            return False
        totals = self._totals()
        open_rows = self.store.open_positions(self.mode)
        sniping = strategy == "sniper"
        topping = strategy == "toptrader"
        same_strategy = [row for row in open_rows if (row.get("strategy") or "momentum") == strategy]
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
                open_count=len(same_strategy),
                open_mints={row["mint"] for row in open_rows},
                trades_today=self.store.trades_opened_on(self.mode, day, strategy),
                kill_switch=self.kill_switch,
                halted_day=False,
                halted_drawdown=False,
            ),
            self.settings,
            position_usd=self.sniper.position_usd if sniping else (self.top_cfg.position_usd if topping else None),
            max_open=self.settings.sniper_max_open if sniping else (self.top_cfg.max_open if topping else None),
            max_trades=self.settings.sniper_max_per_day if sniping else (self.top_cfg.max_per_day if topping else None),
            label="sniper-" if sniping else ("top-trader-" if topping else ""),
        )
        if not decision.allowed:
            self._record(cand, "risiko", False, decision.reason, cand["score"], cand["features"])
            return False
        lamports = int(decision.size_usd / sol_usd * LAMPORTS_PER_SOL)
        buy_quote = sell_quote = None
        try:
            buy_quote = self.clients.jup.quote(SOL_MINT, cand["mint"], lamports, priority="normal")
            sell_quote = self.clients.jup.quote(cand["mint"], SOL_MINT, buy_quote.out_amount, priority="normal")
        except RateLimited as exc:
            # Ikke et dårligt token, bare en fuld kvote. Ingen afvisning i journalen, så det prøves igen.
            logger.info("Quote sprunget over for %s: %s", cand["mint"], exc)
            cand["retry"] = True
            return False
        except SourceError as exc:
            logger.info("Quote fejlede for %s: %s", cand["mint"], exc)
        reasons = roundtrip_rejections(buy_quote, sell_quote, self.params)
        if reasons:
            self._record(cand, "rute", False, "; ".join(reasons), cand["score"], cand["features"])
            return False
        chase = self._chase(cand, buy_quote) if topping else None
        if chase is not None and chase > self.top_cfg.max_chase_pct:
            self._record(
                cand,
                "rute",
                False,
                f"Vores pris er {chase * 100:.0f} % over traderens købspris (maks. {self.top_cfg.max_chase_pct * 100:.0f} %). "
                "Kursen er allerede løbet; vi ville købe traderens gevinst.",
                cand["score"],
                cand["features"],
            )
            return False

        verdict: Verdict | None = None
        # Top-trader-køb skal gå hurtigt efter traderen, så Claude springes over dér.
        if (strategy == "momentum" or (sniping and self.sniper.use_claude)) and self.researcher.available():
            verdict = self.researcher.research(self._ai_payload(cand, buy_quote, sell_quote), self._lessons_text())
            # Research kan tage tid. Tjek straks de åbne positioner, så et stop-loss ikke venter.
            self._monitor(wallet, sol_usd)
            if verdict is not None:
                cand["features"]["ai_view"] = verdict.view
                cand["score"] = self.model.score(cand["features"])
                if verdict.decision != "buy":
                    flags = "; ".join(verdict.red_flags[:2])
                    self._record(cand, "claude", False, f"Claude: skip ({verdict.confidence}). {flags or verdict.summary}", cand["score"], cand["features"])
                    return False

        if self.kill_switch or self.paused:
            # Nødstop eller pause kan være trykket, mens Claude researchede.
            self._record(cand, "risiko", False, "Nødstop eller pause blev slået til, før købet nåede at gå igennem.", cand["score"], cand["features"])
            return False
        fill = wallet.buy(cand["mint"], decision.size_usd, sol_usd, strategy=strategy)
        if not fill.ok:
            if fill.fee_usd:
                key = f"failed_fees:{self.mode}"
                self.store.set_state(key, repr(float(self.store.get_state(key, "0") or 0) + fill.fee_usd))
            self._record(cand, "køb", False, f"Købet fejlede: {fill.error}", cand["score"], cand["features"])
            self.store.add_event("warning", f"Køb af {pair.symbol} fejlede: {fill.error}")
            return False
        self._refresh_wallet_sol(wallet)
        drivers = [FEATURES[name][0].lower() for name, value in self.model.contributions(cand["features"]) if value > 0.05][:3]
        if sniping:
            token = cand.get("token")
            holders = f", {token.holders} holdere" if token is not None and token.holders else ""
            exit_plan = f"Sælger {'alt' if self.sniper.sell_fraction >= 0.999 else f'{self.sniper.sell_fraction * 100:.0f} %'} ved +{self.sniper.take_profit_pct * 100:.0f} %, stop ved -{self.sniper.stop_loss_pct * 100:.0f} %."
            reasoning = (
                f"Sniper: ny token, {cand.get('age_minutes', 0):.0f} min. gammel, likviditet ${pair.liquidity_usd:,.0f}{holders}. "
                f"Bestod rug-tjek og exit-rute. {exit_plan}"
            )
        elif topping:
            lead = cand.get("leader") or {}
            who = f"nr. {lead['rank']} på listen" if lead.get("rank") else "en top-trader"
            earned = f", {'+' if lead['pnl_usd'] >= 0 else '-'}{_dk(abs(lead['pnl_usd']), 0)} USD i perioden" if isinstance(lead.get("pnl_usd"), (int, float)) else ""
            exit_plan = "Sælger, når traderen sælger" if self.top_cfg.follow_sells else "Følger ikke traderens salg"
            paid = f" Vores pris: {lead['chase'] * 100:+.0f} % i forhold til traderens." if lead.get("chase") is not None else ""
            reasoning = (
                f"Top-trader {source_label(cand['source'])[6:]} ({who}{earned}) købte for {_dk(lead.get('sol', 0))} SOL for {lead.get('age', 0):.0f} s siden.{paid} "
                f"Bestod rug-tjek og exit-rute. {exit_plan}" + (f", stop ved -{self.top_cfg.stop_loss_pct * 100:.0f} %." if self.top_cfg.stop_loss_on else " (ingen stop-loss).")
            )
        else:
            reasoning = f"Score {cand['score']:.2f}. Kilde: {source_label(cand['source'])}. Stærkeste signaler: {', '.join(drivers) or 'ingen enkeltstående'}."
        if verdict is not None:
            reasoning += f" Claude ({verdict.confidence}): {verdict.summary}"
        report = cand.get("report")
        position_id = self.store.open_position(
            {
                "strategy": strategy,
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
                    "insiders": report.insiders if report else None,
                    "facts": self._facts(cand),
                    "watch": {
                        "dev": report.creator if report else "",
                        "holders": [owner for owner, _pct in (report.top_owners if report else []) if owner and owner != (report.creator if report else "")][:3],
                    },
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
            f"Købt {pair.symbol} for {fill.usd:.2f} USD ({'sniper, ' if sniping else ''}{'papir' if self.mode == 'paper' else 'LIVE'}).",
            reasoning,
        )
        self._notify(f"Købt {pair.symbol} for {fill.usd:.2f} USD. {reasoning[:300]} {pair.url}")
        logger.info("Ny position %s i %s", position_id, pair.symbol)
        return True

    @staticmethod
    def _chase(cand: dict, buy_quote) -> float | None:
        """Hvor meget dyrere end traderen botten køber (0,2 = 20 % dyrere). None hvis det ikke kan regnes ud."""
        lead = cand.get("leader") or {}
        token_raw, sol = int(lead.get("token_raw") or 0), float(lead.get("sol") or 0.0)
        if buy_quote is None or token_raw <= 0 or sol <= 0 or buy_quote.out_amount <= 0:
            return None
        ours = buy_quote.in_amount / buy_quote.out_amount
        theirs = sol * LAMPORTS_PER_SOL / token_raw
        chase = ours / theirs - 1.0
        lead["chase"] = chase
        return chase

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
        # Flaget sættes med det samme, også midt i en lang runde, så intet nyt bliver købt.
        self.kill_switch = True
        self.store.set_state("kill_switch", "1")
        self.store.add_event("warning", "Nødstop: alle positioner sælges, og nye køb er blokeret.")
        self._notify("Nødstop aktiveret. Alle positioner sælges.")
        self._wake.set()
        if self._lock.acquire(timeout=KILL_LOCK_SECONDS):
            try:
                self._sell_all(self.mode, "kill", "Nødstop.")
            finally:
                self._lock.release()
        else:
            # Runden er optaget (fx Claude-research). Næste runde sælger alt, se _monitor.
            self.store.add_event("info", "Botten var midt i en runde. Positionerne sælges, så snart den er færdig.")

    def resume(self) -> None:
        totals = self._totals()
        self.store.set_state(f"dd_reset:{self.mode}", repr(totals["total_pnl"]))
        self.store.set_state(f"halt_dd:{self.mode}", "0")
        self.kill_switch = False
        self.store.set_state("kill_switch", "0")
        self.store.add_event("info", "Genoptaget. Drawdown måles fra nu. Dagens tabsgrænse gælder stadig.")

    def update_loss_limits(self, changes: dict) -> None:
        if not isinstance(changes, dict):
            raise ValueError("Ugyldige indstillinger.")
        limits = dict(self.loss_limits)
        if "ai_budget" in changes:
            self._set_ai_budget(bool(changes["ai_budget"]))
        for key in ("daily", "drawdown"):
            if key in changes:
                limits[key] = bool(changes[key])
        if not limits["daily"]:
            self.store.set_state(f"halt_day:{self.mode}:{utc_day()}", "0")
        if not limits["drawdown"]:
            self.store.set_state(f"halt_dd:{self.mode}", "0")
        elif not self.loss_limits["drawdown"]:
            # Slås den samlede grænse til igen, måles tabet fra nu, så den ikke udløses med det samme.
            self.store.set_state(f"dd_reset:{self.mode}", repr(self._totals()["total_pnl"]))
        if limits != self.loss_limits:
            words = {True: "til", False: "fra"}
            self.store.add_event("info", f"Tabsgrænser: dagligt {words[limits['daily']]}, samlet {words[limits['drawdown']]}.")
        self.loss_limits = limits
        self.store.set_json("loss_limits", limits)

    def _set_ai_budget(self, on: bool) -> None:
        if on != bool(getattr(self.researcher, "limit_on", False)):
            text = (f"Dagligt AI-loft slået til: Claude stopper for i dag ved {self.settings.ai_daily_budget_usd:.2f} USD." if on
                    else "Dagligt AI-loft slået fra: Claude tjekker coins hele dagen.")
            self.store.add_event("info", text)
        self.researcher.limit_on = on
        self.store.set_json("ai_budget", {"on": on})

    def pause(self, paused: bool) -> None:
        # Ingen lås: pausen skal virke med det samme, også midt i en runde.
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

    def add_wallets(self, wallets: list[str]) -> dict:
        added, errors = 0, []
        for raw in wallets[:MAX_COPY_WALLETS]:
            wallet = str(raw).strip()
            if not wallet:
                continue
            try:
                self.add_wallet(wallet)
                added += 1
            except ValueError as exc:
                errors.append(f"{wallet[:6]}…: {exc}")
        return {"added": added, "errors": errors}

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
        with self._lock:
            held = [row for row in self.store.open_positions(self.mode) if row["mint"] == mint]
            if held:
                try:
                    sol_usd = self._sol_price()
                except SourceError:
                    return
                for pos in held:
                    self._sell(pos, 1.0, "manual", "Du blokerede tokenet, så botten sælger det.", self._wallet(), sol_usd)

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
            self.settings = self._mode_settings("live")
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
            self.settings = self._mode_settings("paper")
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
        fixed_monthly = self.settings.hosting_usd_per_month + self.settings.other_costs_usd_per_month
        fixed_so_far = fixed_monthly * elapsed / days_in_month
        month_net = month_realized - ai_month - fixed_so_far
        projection = (month_realized - ai_month) / elapsed * days_in_month - fixed_monthly
        goal = self.settings.goal_usd_per_month
        fees_total = sum(float(row["fees_usd"] or 0) for row in self.store.list_positions(mode, 5000))
        stats = source_stats(closed)
        unlock = self._unlock()
        pubkey, _key_error = self._wallet_pubkey()
        defaults = StrategyParams()
        initial = Model()
        locks = self._locks()
        analysis = self._analysis() if self.settings.shadows else None
        why_from = {item["strategy"]: item["facts"] for item in (analysis or {}).get("strategies", [])}
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
            "halted_day": self.loss_limits["daily"] and self.store.get_state(f"halt_day:{mode}:{today}", "0") == "1",
            "halted_drawdown": self.loss_limits["drawdown"] and self.store.get_state(f"halt_dd:{mode}", "0") == "1",
            "loss_limits": {
                **self.loss_limits,
                "daily_usd": round(self.settings.daily_loss_pct * self.settings.bankroll_usd, 2),
                "drawdown_usd": round(self.settings.max_drawdown_pct * self.settings.bankroll_usd, 2),
                "bankroll_usd": self.settings.bankroll_usd,
                "ai_budget": bool(getattr(self.researcher, "limit_on", False)),
                "ai_budget_usd": self.settings.ai_daily_budget_usd,
                "ai_configured": self.settings.ai_configured,
            },
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
                "ai_budget_left": self.researcher.budget_left() if self.settings.ai_configured and getattr(self.researcher, "limit_on", False) else None,
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
                "month_fixed": fixed_so_far,
                "fixed_monthly": fixed_monthly,
                "hosting_monthly": self.settings.hosting_usd_per_month,
                "other_monthly": self.settings.other_costs_usd_per_month,
                "month_costs": ai_month + fixed_so_far,
                "need_per_month": goal + fixed_monthly,
            },
            "diagnostics": self.store.get_json("diagnostics", None),
            "last_report": self.store.get_json("last_report", None),
            "shadows": {
                "active": self.store.count_active_shadows(),
                "summary": shadow_summary([row for row in self.store.done_shadows(mode, limit=500)]),
                "recent": [self._shadow_view(row) for row in self.store.recent_shadows(mode, 60)],
                "optimizer": self._optimizer() if self.settings.shadows else None,
                "analysis": analysis,
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
            "trades": [self._position_view(row, why_from=why_from) for row in self.store.list_positions(mode, 200) if row["status"] == "closed"],
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
                    "kind": PARAM_KINDS.get(name, "num"),
                    "group": "exit" if name in PARAM_EXITS else "filter",
                    "locked_until": locks.get(f"momentum:{name}"),
                }
                for name in PARAM_BOUNDS
            ],
            "param_lock_days": PARAM_LOCK_DAYS,
            "auto_changes": [self._change_view(entry) for entry in reversed(self.store.get_json("auto_changes", []) or [])][:25],
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
            "sniper": self._sniper_status(closed, self.store.open_positions(mode), today),
            "toptraders": self._top_status(closed, self.store.open_positions(mode), today),
            "strategies": self._strategies_status(closed, self.store.open_positions(mode)),
            "strategy_guard": self.guard_on,
            "auto_targets": self.auto_targets,
            "wallet": self.wallet_info(),
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
                "clock": self._clock_view(),
                "scan_seconds": self.settings.scan_seconds,
                "monitor_seconds": self.settings.monitor_seconds,
                "jupiter": self._jupiter_usage(),
                "dexscreener": self._usage_of(self.clients.dex),
                "rugcheck": self._usage_of(self.clients.rug),
            },
        }

    def _clock_view(self) -> dict:
        """Absolutte tider (sekunder siden 1970), så dashboardet kan tælle ned sekund for sekund."""
        now = time.time()
        due = self._due_times()
        jobs = {}
        for name, job in self._jobs.items():
            at = due[name]
            jobs[name] = {
                "enabled": at is not None,
                "running": bool(job["running"]),
                "started_at": job["started"] or None,
                "next_at": (at if at is not None and self._last_of(name) else now) if at is not None else None,
                "last_duration": job["duration"],
                "every": {"scan": self.settings.scan_seconds, "snipe": self.settings.sniper_scan_seconds, "top": TOP_POLL_SECONDS}[name],
                "runs": job["runs"],
            }
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return {
            "now": now,
            "started_at": self._started_at,
            "phase": self._phase,
            "phase_since": self._phase_since,
            "tick_every": self._loop_seconds(),
            "last_tick_at": self._tick_started or None,
            "last_tick_seconds": self._tick_seconds,
            "day_ends_at": tomorrow.timestamp(),
            "jobs": jobs,
        }

    def _last_of(self, name: str) -> float:
        return {"scan": self._last_scan, "snipe": self._last_snipe, "top": self._last_top_poll}[name]

    def _jupiter_usage(self) -> dict | None:
        return self._usage_of(self.clients.jup)

    @staticmethod
    def _usage_of(client) -> dict | None:
        usage = getattr(client, "usage", None)
        if usage is None:
            return None
        try:
            return usage()
        except Exception:
            return None

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
        if not self.loss_limits["daily"] and not self.loss_limits["drawdown"]:
            alerts.append({"level": "info", "text": f"Tabsgrænserne er slået fra. Botten stopper ikke ved tab, men bruger aldrig mere end din bankroll på {self.settings.bankroll_usd:.2f} USD."})
        if self.loss_limits["daily"] and self.store.get_state(f"halt_day:{self.mode}:{today}", "0") == "1":
            top_runs = self.top_cfg.enabled and not self.top_cfg.daily_loss_stop
            alerts.append({
                "level": "warning",
                "text": "Dagens tabsgrænse er ramt. Ingen nye køb før i morgen (UTC)."
                + (" Top 10 kopierer stadig, fordi den ikke har en daglig tabsgrænse." if top_runs else ""),
            })
        if self.loss_limits["drawdown"] and self.store.get_state(f"halt_dd:{self.mode}", "0") == "1":
            alerts.append({"level": "warning", "text": "Drawdown-grænsen er ramt. Tryk Genoptag, når du har set på det."})
        if not self.settings.anthropic_api_key:
            alerts.append({"level": "info", "text": "Ingen ANTHROPIC_API_KEY: ingen Claude-research af X og nyheder. Botten kører på regler og RugCheck."})
        elif self.settings.ai_configured and getattr(self.researcher, "limit_on", False) and self.researcher.budget_left() < 0.1:
            alerts.append({"level": "info", "text": "Dagens AI-budget er brugt. Resten af dagen kører botten kun på regler."})
        if not self.settings.x_configured and self.settings.anthropic_api_key:
            # Uden Claude dækker beskeden ovenfor allerede X; to beskeder om det samme forvirrer.
            alerts.append({"level": "info", "text": "Ingen X_BEARER_TOKEN: X-omtale tjekkes kun gennem Claudes websøgning."})
        for strategy, hold in self._holds().items():
            if self._on_hold(strategy):
                alerts.append({"level": "warning", "text": self._hold_text(strategy) + " Den følger stadig tokens som skyggehandler og lærer videre."})
        jupiter = self._jupiter_usage()
        if jupiter and jupiter.get("limited_seconds_ago") is not None and jupiter["limited_seconds_ago"] < JUPITER_ALERT_SECONDS:
            if jupiter.get("keyed"):
                text = "Jupiter har sagt stop til for mange kald (429). Botten holder kort pause, sparer på kaldene og prøver igen."
            else:
                text = (
                    "Jupiter begrænser gratis kald uden nøgle (429). Botten holder pause og prøver igen. "
                    "Lav en gratis nøgle på portal.jup.ag og sæt JUPITER_API_KEY i Railway for en fast og højere grænse."
                )
            alerts.append({"level": "info", "text": text})
        rug = self._usage_of(self.clients.rug)
        if rug and rug.get("limited_seconds_ago") is not None and rug["limited_seconds_ago"] < JUPITER_ALERT_SECONDS:
            alerts.append({"level": "info", "text": "RugCheck har sagt stop til for mange kald (429). Botten køber intet uden et frisk rug-tjek, så den holder pause og tjekker kandidaterne igen bagefter."})
        dex = self._usage_of(self.clients.dex)
        if dex and dex.get("limited_seconds_ago") is not None and dex["limited_seconds_ago"] < JUPITER_ALERT_SECONDS:
            alerts.append({"level": "info", "text": "DexScreener har sagt stop til for mange kald (429). Botten holder pause, bruger Jupiters kurser imens og prøver igen."})
        if self.settings.rpc_url.startswith("https://api.mainnet-beta.solana.com") and (self._all_wallets() or self.mode == "live" or self.top_cfg.enabled):
            alerts.append({
                "level": "warning",
                "text": "Den offentlige Solana-forbindelse er langsom og har en lav grænse, så botten kan misse traders' handler. "
                "Lav en gratis nøgle på helius.dev og sæt SOLANA_RPC_URL i Railway, så forsvinder beskeden.",
            })
        diagnostics = self.store.get_json("diagnostics", None)
        if diagnostics and not diagnostics.get("required_ok"):
            failed = [item["label"] for item in diagnostics.get("checks", []) if not item["ok"] and not item.get("optional")]
            alerts.append({"level": "warning", "text": f"Forbindelsestest: {', '.join(failed)} svarer ikke. Se Indstillinger → Forbindelser."})
        if self.last_error:
            alerts.append({"level": "error", "text": f"Seneste fejl: {self.last_error}"})
        return alerts

    def _position_view(self, row: dict, why_from: dict | None = None) -> dict:
        token_raw = max(1, int(row["token_raw"]))
        fraction = int(row["remaining_raw"]) / token_raw
        mark = float(row["cost_usd"]) * fraction * float(row["last_ratio"] or 1.0)
        unrealized = float(row["proceeds_usd"]) + mark - float(row["cost_usd"]) - float(row["fees_usd"])
        ai = loads(row["ai"], None)
        safety = loads(row["safety"], {}) or {}
        watch = safety.get("watch") or {}
        peak = float(row["peak_ratio"] or 1.0)
        sniping = row.get("strategy") == "sniper"
        trail = None
        if sniping:
            if row["partial_taken"]:
                trail = max(0.0, peak * (1.0 - MOONBAG_TRAIL) - 1.0)
            stop_pct, tp_pct = -self.sniper.stop_loss_pct, None if row["partial_taken"] else self.sniper.take_profit_pct
            max_hold = self.sniper.max_hold_minutes
        elif row.get("strategy") == "toptrader":
            if peak >= 1.0 + TOP_TRAIL_ACTIVATION:
                trail = peak * (1.0 - TOP_TRAIL) - 1.0
            stop_pct, tp_pct = (-self.top_cfg.stop_loss_pct if self.top_cfg.stop_loss_on else None), None
            max_hold = self.top_cfg.max_hold_minutes
        else:
            if peak >= 1.0 + self.params.trail_activation_pct:
                trail = peak * (1.0 - self.params.trailing_stop_pct) - 1.0
            stop_pct, tp_pct = -self.params.stop_loss_pct, None if row["partial_taken"] else self.params.take_profit_pct
            max_hold = self.params.max_hold_minutes
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
            "strategy": row.get("strategy") or "momentum",
            "path": list(self._path_of(int(row["id"]))) if row["status"] == "open" else [],
            "priced_at": self._priced_at.get(int(row["id"])) if row["status"] == "open" else None,
            "watching": bool(self.settings.dev_watch and (watch.get("dev") or watch.get("holders"))),
            "stop_pct": stop_pct,
            "tp_pct": tp_pct,
            "trail_pct": trail,
            "max_hold_minutes": max_hold,
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
            "why": self._why(row, safety, why_from) if row["status"] == "closed" and why_from is not None else [],
        }

    @staticmethod
    def _why(row: dict, safety: dict, why_from: dict) -> list[str]:
        strategy = row.get("strategy") or "momentum"
        try:
            return explain_trade(
                safety.get("facts") or {},
                strategy=strategy,
                pnl_pct=float(row["pnl_pct"] or 0.0),
                exit_reason=str(row["exit_reason"] or ""),
                peak_pct=float(row["peak_ratio"] or 1.0) - 1.0,
                analysis=why_from.get(strategy),
            )
        except Exception as exc:  # en forklaring må aldrig vælte siden
            logger.info("Forklaring fejlede for %s: %s", row.get("id"), exc)
            return []

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
