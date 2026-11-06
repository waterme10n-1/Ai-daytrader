"""Handelsbordet: SCOUT → NEWS → CHART → SKEPTIC → SIZER → EXECUTOR, og du godkender hver ordre.

Bordet handler kun på papirpenge: i New York hos Alpaca, i København på bordets egen papirkonto. Der findes ingen
kodevej til rigtige penge her. Én motor pr. børs; alt børsspecifikt (tider, valuta, indeks) står i markets.py.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from trader.config import Settings
from trader.desk.alpaca import ET_NAME, AlpacaPaper, BrokerError
from trader.desk import calendar, learning, markets, rules
from trader.desk.agents import BOTS, DeskBrain, bot_rules
from trader.desk.config import (
    HARD_MAX_FLAGS,
    HARD_MAX_OPEN,
    HARD_MAX_POSITION_PCT,
    HARD_MAX_STOP_PCT,
    HARD_MIN_REWARD_RISK,
    RETIRED_DEFAULTS,
    DeskSettings,
    clean_key,
    key_problem,
    parse_watchlist,
)
from trader.desk.store import DeskStore
from trader.notify import send as notify

logger = logging.getLogger("trader.desk")
ET = ZoneInfo(ET_NAME)

PRE_SCAN_MINUTES = 5  # før åbning kigger SCOUT hvert 5. minut
CACHE_SECONDS = 30.0
REFUSALS = ("wait", "skip", "rejected", "expired", "sized_zero")
STOP_WORDS = {"stop nu", "stop", "stop now", "pause", "stop alt"}
START_WORDS = {"start igen", "start", "fortsæt", "genoptag"}
STATUS_DA = {
    "flagged": "Markeret",
    "no_setup": "Ingen setup",
    "skip": "SKIP",
    "wait": "WAIT",
    "buy": "BUY",
    "sized_zero": "Størrelse 0",
    "awaiting": "Venter på dig",
    "rejected": "Afvist af dig",
    "expired": "Udløbet",
    "placed": "Åben",
    "closed": "Lukket",
    "failed": "Fejlede",
}
EXIT_DA = {"stop": "stop", "target": "mål", "flatten": "lukketid", "outside": "lukket udefra", "manual": "lukket af dig"}


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=ET)


class DeskEngine:
    def __init__(
        self,
        settings: DeskSettings,
        main_settings: Settings,
        *,
        store: DeskStore | None = None,
        broker: Any = None,
        brain: DeskBrain | None = None,
        now_fn: Callable[[], datetime] | None = None,
        background_replies: bool = True,
    ) -> None:
        self.base = settings
        self.market = markets.get(settings.market)
        self.tz = self.market.tz
        self.main_settings = main_settings
        self.store = store or DeskStore(settings.db_path)
        self._broker = broker
        self._injected_broker = broker is not None
        self.brain = brain or DeskBrain(settings, self.store, settings.anthropic_api_key)
        self._now = now_fn or (lambda: datetime.now(self.tz))
        self._background = background_replies
        self._order_lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="desk-chat")
        self._last_minute = ""
        self._cache: dict[str, tuple[float, Any]] = {}
        self.last_error = ""
        self._last_error_post = 0.0
        self.last_tick: str | None = None
        self.scanning = False
        self._quotes: dict = {"ts": None, "mode": None, "items": []}
        self.last_scan: str | None = None
        existing = self.store.get_state("welcome") is not None
        self._retire_old_defaults(announce=existing)
        if self.store.get_state("welcome") is None:
            self.store.set_state("welcome", "1")
            self.store.post(
                "system",
                "info",
                "Handelsbordet er åbnet. Rækkefølgen er altid: SCOUT markerer, NEWS finder grunden, CHART sætter "
                "indgang, mål og stop, SKEPTIC afgør BUY, WAIT eller SKIP, SIZER sætter størrelsen, og EXECUTOR spørger "
                f"dig, før ordren lægges. Bordet arbejder i {self.market.name}s åbningstid på hverdage og kun på papirpenge"
                + (" (bordets egen papirkonto i kroner, kurser fra Yahoo Finance). " if self.market.broker == "intern" else ". ")
                + 
                "Skriv @ og en bot for at tale med den, eller \"Stop nu\" for at sætte alt på pause.",
            )

    # ------------------------------------------------------------------ indstillinger
    def _retire_old_defaults(self, *, announce: bool) -> None:
        """Én gang pr. værdi: gemte værdier, der bare var de gamle standarder, fjernes, så de nye gælder.

        Trykker man Gem regler, gemmes alle felter. Uden det her ville en gammel standard hænge fast for altid.
        """
        done = list(self.store.get_json("retired_defaults", []) or [])
        todo = [name for name in RETIRED_DEFAULTS if name not in done]
        if not todo:
            return
        self.store.set_json("retired_defaults", done + todo)
        params = self.store.get_json("params", {}) or {}
        dropped = [name for name in todo if name in params and params[name] == RETIRED_DEFAULTS[name]]
        for name in dropped:
            params.pop(name)
        if dropped:
            self.store.set_json("params", params)
        if announce:
            cfg = self.cfg
            self.store.post(
                "system",
                "info",
                f"SCOUT er opdateret: grænsen er {cfg.move_pct:g} % på en time, højst {cfg.max_flags_per_day} markeringer "
                f"om dagen, og watchlisten har {len(cfg.watchlist)} aktier ({', '.join(cfg.watchlist)}). Markeringer kun på "
                "volumen bruger ikke længere pladserne. "
                + (
                    f"Før åbning (fra kl. 10 dansk tid) kigger SCOUT efter aktier, der er steget {cfg.gap_pct:g} % eller mere "
                    "siden i går, og NEWS finder grunden. Ved åbningen kan sådan et gap blive købt, hvis det holder."
                    if cfg.premarket
                    else ""
                ),
            )

    @property
    def cfg(self) -> DeskSettings:
        params = self.store.get_json("params", {}) or {}
        values: dict[str, Any] = {}
        watch = parse_watchlist(params.get("watchlist"), self.market.key) if params.get("watchlist") is not None else []
        if watch:
            values["watchlist"] = tuple(watch)
        for name in ("move_pct", "volume_x", "position_pct", "stop_max_pct", "reward_risk", "gap_pct"):
            if isinstance(params.get(name), (int, float)) and not isinstance(params.get(name), bool):
                values[name] = float(params[name])
        for name in ("max_open", "max_flags_per_day", "scan_minutes"):
            if isinstance(params.get(name), int):
                values[name] = int(params[name])
        for name in ("auto_approve", "premarket"):
            if isinstance(params.get(name), bool):
                values[name] = params[name]
        return dataclasses.replace(self.base, **values) if values else self.base

    def update_params(self, body: dict) -> None:
        """Ændringer fra dashboardet. Værdier uden for de hårde lofter afvises."""
        params = self.store.get_json("params", {}) or {}
        limits = {
            "move_pct": (0.5, 20.0, float),
            "volume_x": (1.2, 20.0, float),
            "position_pct": (0.5, HARD_MAX_POSITION_PCT, float),
            "stop_max_pct": (0.3, HARD_MAX_STOP_PCT, float),
            "reward_risk": (HARD_MIN_REWARD_RISK, 5.0, float),
            "max_open": (1, HARD_MAX_OPEN, int),
            "max_flags_per_day": (1, HARD_MAX_FLAGS, int),
            "scan_minutes": (1, 30, int),
            "gap_pct": (1.0, 20.0, float),
        }
        changes: list[str] = []
        if "premarket" in body and body["premarket"] is not None:
            early = body["premarket"] in (True, "true", "1", 1, "on")
            params["premarket"] = early
            if self.market.premarket_trading:
                changes.append("før åbning " + ("til: SCOUT og NEWS kigger på gaps fra kl. 10 dansk tid" if early else "fra"))
            else:
                changes.append("gaps ved åbningen " + ("til" if early else "fra"))
        if "auto_approve" in body and body["auto_approve"] is not None:
            auto = body["auto_approve"] in (True, "true", "1", 1, "on")
            params["auto_approve"] = auto
            changes.append("autopilot " + ("til: bordet køber selv på papir" if auto else "fra: du godkender hver ordre"))
        if "watchlist" in body:
            watch = parse_watchlist(body.get("watchlist"), self.market.key)
            if not watch:
                example = "NOVO-B" if self.market.key == "dk" else "NVDA"
                raise ValueError(f"Watchlisten skal have mindst én gyldig ticker (fx {example}).")
            params["watchlist"] = watch
            changes.append(f"watchlist: {', '.join(watch)}")
        for name, (low, high, kind) in limits.items():
            if name not in body or body[name] in (None, ""):
                continue
            try:
                value = kind(float(body[name])) if kind is int else float(body[name])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} skal være et tal.") from exc
            if not (low <= value <= high):
                raise ValueError(f"{name} skal ligge mellem {low:g} og {high:g}.")
            params[name] = value
            changes.append(f"{name}={value:g}")
        if body.get("reset"):
            params = {}
            changes = ["tilbage til standard"]
        # Værdier, der er lig standarden, gemmes ikke. Så følger de med, hvis standarden ændres senere.
        for name in list(params):
            default = getattr(self.base, name, None)
            if isinstance(default, tuple):
                default = list(default)
            if params[name] == default:
                params.pop(name)
        self.store.set_json("params", params)
        if changes:
            self.store.post("system", "info", "Indstillinger ændret: " + "; ".join(changes) + ".")

    # ------------------------------------------------------------------ broker
    @property
    def own_ledger(self) -> bool:
        """København: bordet fører selv papirkontoen. Ingen nøgler at sætte."""
        return self.market.broker == "intern"

    @property
    def source(self) -> str:
        return "Yahoo Finance" if self.own_ledger else "Alpaca"

    @property
    def title(self) -> str:
        return "Handelsbordet (København)" if self.market.key == "dk" else "Handelsbordet"

    @property
    def keys_present(self) -> bool:
        if self._injected_broker or self.own_ledger:
            return True
        key, secret, _, _ = self.keys()
        return bool(key and secret)

    def keys(self) -> tuple[str, str, str, str]:
        """(key, secret, hvor key kommer fra, hvor secret kommer fra). Nøgler gemt på siden vinder over Railway."""
        saved = self.store.get_json("alpaca_keys") or {}
        if saved.get("key") and saved.get("secret"):
            return saved["key"], saved["secret"], "gemt på siden", "gemt på siden"
        return self.base.alpaca_api_key, self.base.alpaca_secret_key, self.base.key_source, self.base.secret_source

    def test_keys(self, key: str, secret: str, *, save: bool) -> dict:
        """Prøver nøgler fra siden hos Alpaca. Gemmes kun, hvis Alpaca godtager dem og save er sat."""
        if self.own_ledger:
            raise ValueError("Københavns børs bruger bordets egen papirkonto. Der er ingen nøgler at sætte.")
        key, secret = clean_key(key), clean_key(secret)
        if not key or not secret:
            raise ValueError("Indsæt både Key og Secret.")
        info = {"key_start": key[:2], "key_length": len(key), "secret_length": len(secret), "hint": key_problem(key)}
        if secret.upper().startswith(("PK", "AK")) and not key.upper().startswith(("PK", "AK")):
            info["hint"] = "Secret starter med PK eller AK. Key og Secret er nok byttet om."
        client = AlpacaPaper(key, secret)
        try:
            account = client.get_account()
        except BrokerError as exc:
            return {"ok": False, "saved": False, **info, "text": str(exc)}
        finally:
            client.close()
        if save:
            self.store.set_json("alpaca_keys", {"key": key, "secret": secret})
            self._reset_broker()
            self.last_error = ""
            self.store.post("system", "info", "Nye Alpaca-nøgler er gemt på siden og bruges fra nu af.")
        return {
            "ok": True,
            "saved": save,
            **info,
            "text": f"Alpaca godtager nøglerne. Papirkontoen har {account.equity:,.2f} USD." + (" Nøglerne er gemt og bruges nu." if save else ""),
        }

    def forget_keys(self) -> None:
        self.store.set_json("alpaca_keys", {})
        self._reset_broker()
        self.store.post("system", "info", "Nøglerne gemt på siden er slettet. Bordet bruger igen nøglerne fra Railway.")

    def _reset_broker(self) -> None:
        if self._broker is not None and not self._injected_broker:
            try:
                self._broker.close()
            except Exception:
                pass
            self._broker = None
        self._cache.clear()

    def broker(self):
        if self._broker is None:
            # Altid papir. Bordet har ingen adresse til rigtige penge.
            if self.own_ledger:
                from trader.desk.denmark import DanishPaper

                self._broker = DanishPaper(
                    self.store, start_cash=self.base.start_cash, fee_pct=self.base.fee_pct, now_fn=self._now, market=self.market
                )
            else:
                key, secret, _, _ = self.keys()
                self._broker = AlpacaPaper(key, secret)
        return self._broker

    def _cached(self, name: str, fetch: Callable[[], Any]) -> Any:
        stamp = self._now().timestamp()
        hit = self._cache.get(name)
        if hit and stamp - hit[0] < CACHE_SECONDS:
            return hit[1]
        value = fetch()
        self._cache[name] = (stamp, value)
        return value

    # ------------------------------------------------------------------ tråd
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="desk", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._pool.shutdown(wait=False, cancel_futures=True)
        if self._broker is not None and not self._injected_broker:
            self._broker.close()
            self._broker = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("Handelsbordets løkke fangede en fejl")
            self._stop.wait(15)

    @property
    def paused(self) -> bool:
        return self.store.get_state("paused", "0") == "1"

    # ------------------------------------------------------------------ hvert minut
    def tick(self, *, force: bool = False) -> None:
        now = self._now().astimezone(self.tz)
        minute = now.strftime("%Y-%m-%d %H:%M")
        if minute == self._last_minute and not force:
            return
        self._last_minute = minute
        if not self.keys_present:
            return
        try:
            self._tick(now)
            self.last_error = ""
        except Exception as exc:
            self._error(exc)

    def _error(self, exc: Exception) -> None:
        self.last_error = str(exc)
        logger.warning("%s: %s", self.title, exc)
        stamp = self._now().timestamp()
        if stamp - self._last_error_post > 1800:
            self._last_error_post = stamp
            self.store.post("system", "error", f"Fejl ved forbindelsen til {self.source}: {exc}")

    def _tick(self, now: datetime) -> None:
        self.last_tick = now.isoformat(timespec="seconds")
        broker = self.broker()
        self._expire(now)
        clock = broker.get_clock()
        self._cache["clock"] = (self._now().timestamp(), clock)
        try:
            self._cache["account"] = (self._now().timestamp(), broker.get_account())
        except Exception as exc:  # kontoen er pænt at have, ikke nødvendig for resten af runden
            logger.info("Kontoen kunne ikke hentes: %s", exc)
        day = now.date().isoformat()
        if clock.is_open:
            self._mark_open_day(day, broker, now)
            self._sync(broker, now)
            flatten = self.market.flatten_at(now.date())
            if (now.hour, now.minute) >= flatten:
                self._flatten(
                    broker, now, f"Klokken er {self.market.local_hint(flatten, now.date())}. Bordets positioner lukkes. Intet holdes natten over."
                )
                return
            if self.paused:
                return
            in_window = self.market.scan_start <= (now.hour, now.minute) < self.market.scan_end(now.date())
            if in_window and (now.minute % self.cfg.scan_minutes == 0):
                self.scan(now)
            self._refresh_quotes(broker, now, max_age=120)
            return
        self._sync(broker, now)
        if self._premarket_now(now, clock):
            if not self.paused and now.minute % PRE_SCAN_MINUTES == 0:
                self.premarket_scan(now)
            self._refresh_quotes(broker, now, max_age=120)
            return
        self._refresh_quotes(broker, now, max_age=600)
        if (now.hour, now.minute) >= self.market.log_at(now.date()) and self.store.get_state(f"seen_open:{day}") == "1":
            if self.store.get_state(f"log:{day}") is None:
                self.day_log(day, now)

    def _refresh_quotes(self, broker, now: datetime, *, max_age: int) -> None:
        """Kurser til radar og kursbånd, også når SCOUT ikke kigger (lukket marked, pause, før 9:45)."""
        stamp = _parse_ts(self._quotes.get("ts"))
        if stamp is not None and (now - stamp).total_seconds() < max_age:
            return
        if not hasattr(broker, "get_snapshots"):
            return
        everything = list(dict.fromkeys([*self.cfg.watchlist, self.market.benchmark]))
        try:
            snaps = broker.get_snapshots(everything)
            daily = self._daily(broker, everything, now)
        except Exception as exc:
            logger.info("Kurser kunne ikke hentes: %s", exc)
            return
        flagged = self.store.flagged_today(now.date().isoformat())
        self._quotes = {
            "ts": now.isoformat(timespec="seconds"),
            "mode": "snapshot",
            "items": rules.snapshot_quotes(everything, snaps, daily, flagged, now=now),
        }

    def _mark_open_day(self, day: str, broker, now: datetime) -> None:
        if self.store.get_state(f"seen_open:{day}") == "1":
            return
        account = broker.get_account()
        self.store.set_state(f"seen_open:{day}", "1")
        self.store.set_state(f"day_start:{day}", str(account.equity))
        if self.store.get_json("desk_start") is None:
            spy = self._last_price(broker, self.market.benchmark, now)
            self.store.set_json(
                "desk_start",
                {"date": day, "equity": account.equity, "spy": spy},
            )
            self.store.post(
                "system",
                "info",
                f"Bordet er i gang med {account.equity:,.0f} {self.market.currency} på papirkontoen. Det lærer af hver handel: "
                "taber den samme slags køb flere gange, får den slags WAIT fremover.",
            )

    def _last_price(self, broker, symbol: str, now: datetime) -> float | None:
        bars = broker.get_intraday([symbol], now).get(symbol) or []
        done = rules.completed(bars, now)
        if done:
            return done[-1].close
        daily = broker.get_daily([symbol], 1, now).get(symbol) or []
        return daily[-1].close if daily else None

    # ------------------------------------------------------------------ SCOUT og resten af kæden
    def scan(self, now: datetime | None = None) -> int:
        """Én runde. Returnerer antal nye markeringer."""
        if not self._scan_lock.acquire(blocking=False):
            return 0
        self.scanning = True
        try:
            now = (now or self._now()).astimezone(self.tz)
            cfg = self.cfg
            day = now.date().isoformat()
            flags = self.flags_on(day)
            # Kun markeringer på kursbevægelse tæller i dagens kvote. Dem kun på volumen (typisk i første time,
            # hvor alt handler tungt) har deres egen, så de ikke bruger pladserne op på aktier, der står stille.
            left_move = cfg.max_flags_per_day - len(flags["move"])
            left_volume = cfg.max_flags_per_day - len(flags["volume"])
            broker = self.broker()
            # Ét kald for hele watchlisten (+ indekset): giver radaren kurser og SCOUT sine barer.
            everything = list(dict.fromkeys([*cfg.watchlist, self.market.benchmark]))
            intraday = broker.get_intraday(everything, now)
            daily = self._daily(broker, everything, now)
            minutes = self.market.session_minutes
            self._quotes = {
                "ts": now.isoformat(timespec="seconds"),
                "mode": "live",
                "items": rules.quotes(everything, intraday, daily, now, flags["all"], session_minutes=minutes),
            }
            self.last_scan = now.isoformat(timespec="seconds")
            if left_move <= 0 and left_volume <= 0:
                return 0
            # En ticker, der kun er markeret på volumen, kan markeres igen, når kursen faktisk flytter sig.
            moved, loud = set(flags["move"]), set(flags["volume"])
            moves: list[rules.Flag] = []
            volume_only: list[rules.Flag] = []
            for symbol in cfg.watchlist:
                if symbol in moved:
                    continue
                bars, days = intraday.get(symbol, []), daily.get(symbol, [])
                # Først: et gap ved åbningen, der holder (den første halvanden time). Så: en almindelig bevægelse på en time.
                flag = (
                    rules.gap_flag(
                        symbol, bars, days, now, gap_pct=cfg.gap_pct, volume_x=cfg.volume_x,
                        gap_until=self.market.gap_until, session_minutes=minutes,
                    )
                    if cfg.premarket
                    else None
                )
                if flag is None:
                    flag = rules.scout(symbol, bars, days, now, move_pct=cfg.move_pct, volume_x=cfg.volume_x, session_minutes=minutes)
                if flag is None:
                    continue
                flag.to_close_min = int((self.market.at(now.date(), self.market.close_on(now.date())) - now).total_seconds() // 60)
                if flag.by_move:
                    moves.append(flag)
                elif symbol not in loud:
                    volume_only.append(flag)
            # Gaps med en grund fra før åbning først, så de største bevægelser.
            moves.sort(key=lambda item: (item.by_gap, abs(item.move_pct), item.volume_x), reverse=True)
            volume_only.sort(key=lambda item: item.volume_x, reverse=True)
            chosen = moves[: max(0, left_move)] + volume_only[: max(0, left_volume)]
            done = 0
            for flag in chosen:
                if self.paused:
                    break
                self._pipeline(flag, intraday.get(flag.symbol, []), now, cfg)
                done += 1
            marked = set(self.store.flagged_today(day))
            for item in self._quotes["items"]:
                item["flagged"] = item["symbol"] in marked
            return done
        finally:
            self.scanning = False
            self._scan_lock.release()

    # ------------------------------------------------------------------ før åbning
    def _premarket_now(self, now: datetime, clock=None) -> bool:
        """Før åbning på en handelsdag: 4:00-9:30 ET (10:00-15:30 dansk tid). København har ingen handel før åbning."""
        if not self.cfg.premarket or not self.market.premarket_trading:
            return False
        now = now.astimezone(self.tz)
        todays = calendar.session(now.date(), self.market)
        if not todays or not (self.market.pre_open <= now.time() and now < todays[0]):
            return False
        # Alpacas ur vinder: lukker børsen ekstraordinært i dag, siger det, at næste åbning er en anden dag.
        next_open = getattr(clock, "next_open", None)
        return next_open is None or next_open.astimezone(self.tz).date() == now.date()

    def premarket_on(self, day: str) -> dict[str, dict]:
        return self.store.get_json(f"premarket:{day}", {}) or {}

    def premarket_scan(self, now: datetime | None = None) -> int:
        """Før åbning: SCOUT finder aktier, der er steget gap_pct siden i går, og NEWS finder grunden. Ingen køb før åbning."""
        if not self._scan_lock.acquire(blocking=False):
            return 0
        self.scanning = True
        try:
            now = (now or self._now()).astimezone(self.tz)
            cfg = self.cfg
            day = now.date().isoformat()
            broker = self.broker()
            if not hasattr(broker, "get_snapshots"):
                return 0
            everything = list(dict.fromkeys([*cfg.watchlist, self.market.benchmark]))
            snaps = broker.get_snapshots(everything)
            daily = self._daily(broker, everything, now)
            self._quotes = {
                "ts": now.isoformat(timespec="seconds"),
                "mode": "snapshot",
                "items": rules.snapshot_quotes(everything, snaps, daily, self.flags_on(day)["all"], now=now),
            }
            self.last_scan = now.isoformat(timespec="seconds")
            notes = self.premarket_on(day)
            left = cfg.max_flags_per_day - len(notes)
            if left <= 0:
                return 0
            fresh = [item for item in rules.premarket_gaps(list(cfg.watchlist), snaps, daily, now, gap_pct=cfg.gap_pct) if item["symbol"] not in notes]
            done = 0
            for item in fresh[:left]:
                if self.paused:
                    break
                self._premarket_note(item, now, cfg)
                done += 1
            return done
        finally:
            self.scanning = False
            self._scan_lock.release()

    def _premarket_note(self, item: dict, now: datetime, cfg: DeskSettings) -> None:
        self.brain.settings = cfg
        day = now.date().isoformat()
        symbol = item["symbol"]
        notes = self.premarket_on(day)
        notes[symbol] = {**item, "news": None}
        self.store.set_json(f"premarket:{day}", notes)
        self.store.post("scout", "premarket", rules.premarket_text(item), symbol=symbol, data=item)
        flag = {"symbol": symbol, "move_pct": None, "volume_x": None, "day_change_pct": item["gap_pct"], "gap_pct": item["gap_pct"], "session": "før åbning"}
        news = self.brain.news(flag, self._headlines(symbol, now))
        notes = self.premarket_on(day)
        notes[symbol] = {**item, "news": news}
        self.store.set_json(f"premarket:{day}", notes)
        self.store.post(
            "news",
            "news",
            f"{symbol}: {news['reason']}" if news.get("has_reason") else f"{symbol}: Ingen klar grund endnu.",
            symbol=symbol,
            data=news,
        )

    def _premarket_list(self, day: str) -> list[dict]:
        out = []
        for item in self.premarket_on(day).values():
            news = item.get("news") or {}
            out.append(
                {
                    "symbol": item.get("symbol"),
                    "gap_pct": item.get("gap_pct"),
                    "price": item.get("price"),
                    "prev_close": item.get("prev_close"),
                    "ts": item.get("ts"),
                    "has_reason": bool(news.get("has_reason")),
                    "reason": news.get("reason") if item.get("news") else None,
                    "source_url": news.get("source_url") or "",
                    "source_title": news.get("source_title") or "",
                }
            )
        out.sort(key=lambda row: float(row.get("gap_pct") or 0), reverse=True)
        return out

    def _headlines(self, symbol: str, now: datetime) -> list[dict]:
        try:
            items = self.broker().get_news([symbol], now)
        except BrokerError as exc:
            logger.info("Nyheder for %s kunne ikke hentes: %s", symbol, exc)
            items = []
        return [
            {
                "headline": item.headline,
                "created_at": item.created_at.isoformat(timespec="minutes"),
                "summary": (item.summary or "")[:300],
                "url": getattr(item, "url", ""),
                "source": "Yahoo Finance" if self.own_ledger else "Alpaca",
            }
            for item in items
            if symbol in (item.symbols or [symbol])
        ]

    def flags_on(self, day: str) -> dict[str, list[str]]:
        """Dagens markeringer delt op: på kursbevægelse (tæller i kvoten) og kun på volumen."""
        rows = self.store.proposals_on(day)
        move = [row["symbol"] for row in rows if (row.get("flag") or {}).get("by_move", True)]
        volume = [row["symbol"] for row in rows if not (row.get("flag") or {}).get("by_move", True)]
        return {"all": [row["symbol"] for row in rows], "move": move, "volume": volume}

    def _daily(self, broker, symbols: list[str], now: datetime) -> dict:
        """Dagsbarer ændrer sig ikke i løbet af dagen. De hentes én gang pr. dag, så SCOUT kan kigge hvert minut."""
        day = now.date().isoformat()
        cache = self._cache.get("daily")
        stored: dict = cache[1] if cache and cache[1].get("day") == day else {"day": day, "bars": {}}
        missing = [symbol for symbol in symbols if symbol not in stored["bars"]]
        if missing:
            stored["bars"].update(broker.get_daily(missing, 20, now))
            for symbol in missing:
                stored["bars"].setdefault(symbol, [])
            self._cache["daily"] = (float("inf"), stored)
        return {symbol: stored["bars"].get(symbol, []) for symbol in symbols}

    def _pipeline(self, flag: rules.Flag, bars: list, now: datetime, cfg: DeskSettings) -> None:
        self.brain.settings = cfg  # Claude skal kende de regler, der gælder lige nu (også dem ændret på siden).
        day = now.date().isoformat()
        symbol = flag.symbol
        pid = self.store.create_proposal(day, symbol, flag.to_dict())
        self.store.post("scout", "flag", rules.scout_text(flag), symbol=symbol, proposal_id=pid, data=flag.to_dict())

        # NEWS. Et gap, NEWS allerede har fundet grunden til før åbning, slås ikke op igen.
        early = (self.premarket_on(day).get(symbol) or {}).get("news") if flag.by_gap else None
        if early and early.get("has_reason"):
            news = {**early, "premarket": True}
            text = f"{symbol}: {news['reason']} (fundet før åbning)"
        else:
            news = self.brain.news(flag.to_dict(), self._headlines(symbol, now))
            text = f"{symbol}: {news['reason']}" if news.get("has_reason") else f"{symbol}: Ingen klar grund."
        self.store.update_proposal(pid, news=news)
        self.store.post("news", "news", text, symbol=symbol, proposal_id=pid, data=news)

        # CHART
        plan = rules.chart(
            flag, bars, now, stop_max_pct=cfg.stop_max_pct, reward_risk=cfg.reward_risk, latest_entry=self.market.latest_entry(now.date())
        )
        self.store.update_proposal(pid, chart=plan.to_dict())
        self.store.post("chart", "chart", rules.chart_text(symbol, plan), symbol=symbol, proposal_id=pid, data=plan.to_dict())
        if not plan.setup:
            self.store.update_proposal(pid, status="no_setup")
            return
        self.store.update_proposal(pid, entry=plan.entry, stop=plan.stop, target=plan.target)

        # SKEPTIC
        learned = self.learned()
        record = {**self.skeptic_record(), **learning.for_prompt(self.store.get_json("lessons", []) or [], learned)}
        verdict = self.brain.skeptic(flag.to_dict(), news, plan.to_dict(), record)
        if verdict is None:
            verdict = rules.rules_skeptic(
                flag.to_dict(), news, plan.to_dict(), now, stop_max_pct=cfg.stop_max_pct, late_from=self.market.late_from(now.date())
            )
        if not news.get("has_reason") and verdict["ruling"] != "SKIP":
            verdict["ruling"] = "SKIP"
            verdict["summary"] = "Ingen klar grund fra NEWS, så reglen siger SKIP."
        blocked = learning.blocking(learned, flag.to_dict(), news, plan.to_dict()) if verdict["ruling"] == "BUY" else []
        if blocked:
            verdict["ruling"] = "WAIT"
            verdict["learned"] = [rule["key"] for rule in blocked]
            verdict["reasons"] = list(verdict.get("reasons") or [])[:2] + ["Lært af fejl: " + learning.rule_text(blocked[0])]
            verdict["summary"] = "En lært regel siger WAIT."
        reasons = verdict.get("reasons") or []
        numbered = " ".join(f"{index}) {text}" for index, text in enumerate(reasons, start=1))
        label = "" if verdict.get("source") == "claude" else " (regelbaseret, ikke Claude)"
        self.store.update_proposal(pid, skeptic=verdict, ruling=verdict["ruling"])
        self.store.post(
            "skeptic",
            "ruling",
            f"{symbol}: {numbered} {verdict['ruling']}.{label}".strip(),
            symbol=symbol,
            proposal_id=pid,
            data=verdict,
        )
        if verdict["ruling"] != "BUY":
            self.store.update_proposal(pid, status=verdict["ruling"].lower())
            return

        # SIZER
        with self._order_lock:
            sized = self._size(plan.entry, plan.stop, now, cfg)
            self.store.update_proposal(pid, size=sized.to_dict(), qty=sized.qty)
            self.store.post("sizer", "size", rules.size_text(symbol, sized, self.market.currency), symbol=symbol, proposal_id=pid, data=sized.to_dict())
            if sized.qty < 1:
                self.store.update_proposal(pid, status="sized_zero")
                return
            # EXECUTOR: venter på dig
            expires = now + timedelta(minutes=cfg.approval_minutes)
            self.store.update_proposal(pid, status="awaiting", expires_at=expires.isoformat(timespec="seconds"))
        if cfg.auto_approve:
            # Autopilot: samme tjek som når du trykker Godkend (kurs, stop, mål, lofter), bare uden at vente på dig.
            self.store.post(
                "executor",
                "info",
                f"Autopilot: SKEPTIC sagde BUY, så jeg køber {sized.qty} {symbol} uden at spørge (papirpenge).",
                symbol=symbol,
                proposal_id=pid,
            )
            try:
                self.approve(pid)
            except ValueError as exc:
                logger.info("Autopilot købte ikke %s: %s", symbol, exc)
            return
        text = (
            f"Godkendelse nødvendig: KØB {sized.qty} {symbol} til ca. {plan.entry:.2f}, stop {plan.stop:.2f}, "
            f"mål {plan.target:.2f}. Udløber kl. {self.market.local_hint((expires.hour, expires.minute), expires.date())}."
        )
        self.store.post("executor", "approval", text, symbol=symbol, proposal_id=pid, data={"expires_at": expires.isoformat()})
        notify(self.main_settings, f"{self.title}: {text} Åbn /desk for at godkende.")

    def _open_count(self) -> int:
        return len(self.store.proposals_with_status("placed", "awaiting"))

    def _loss_hit(self, now: datetime, equity: float, cfg: DeskSettings) -> bool:
        start = self.store.get_state(f"day_start:{now.date().isoformat()}")
        try:
            base = float(start) if start else 0.0
        except ValueError:
            base = 0.0
        if base <= 0:
            return False
        return (base - equity) / base * 100.0 >= cfg.daily_loss_pct - 1e-9

    def _size(self, entry: float, stop: float, now: datetime, cfg: DeskSettings, *, exclude: int | None = None) -> rules.Size:
        account = self.broker().get_account()
        open_count = len([row for row in self.store.proposals_with_status("placed", "awaiting") if row["id"] != exclude])
        return rules.size(
            equity=account.equity,
            buying_power=account.buying_power,
            entry=entry,
            stop=stop,
            open_count=open_count,
            position_pct=cfg.position_pct,
            max_open=cfg.max_open,
            loss_limit_hit=self._loss_hit(now, account.equity, cfg),
        )

    # ------------------------------------------------------------------ godkendelser
    def approve(self, proposal_id: int) -> dict:
        with self._order_lock:
            now = self._now().astimezone(self.tz)
            row = self.store.proposal(proposal_id)
            if row is None:
                raise ValueError("Ukendt forslag.")
            if row["status"] != "awaiting":
                raise ValueError(f"Forslaget venter ikke på godkendelse (status: {STATUS_DA.get(row['status'], row['status'])}).")
            expires = _parse_ts(row.get("expires_at"))
            if expires and now >= expires:
                self._set_expired(row, "Godkendelsen nåede at udløbe.")
                raise ValueError("Godkendelsen er udløbet. Kursen kan have flyttet sig.")
            if self.paused:
                raise ValueError("Bordet er sat på pause. Skriv \"Start igen\" først.")
            broker = self.broker()
            if not broker.get_clock().is_open or (now.hour, now.minute) >= self.market.flatten_at(now.date()):
                self._set_expired(row, "Markedet er lukket.")
                raise ValueError("Markedet er lukket.")
            symbol = row["symbol"]
            entry, stop, target = float(row["entry"]), float(row["stop"]), float(row["target"])
            price = self._last_price(broker, symbol, now)
            cfg = self.cfg
            problem = ""
            if price is None:
                problem = "Ingen aktuel kurs."
            elif price <= stop:
                problem = f"Kursen {price:.2f} er allerede under stoppet {stop:.2f}."
            elif price >= target:
                problem = f"Kursen {price:.2f} har allerede nået målet {target:.2f}."
            elif price > entry * (1 + cfg.max_chase_pct / 100.0):
                problem = f"Kursen {price:.2f} er løbet mere end {cfg.max_chase_pct:g}% over indgangen {entry:.2f}. Jeg jagter ikke."
            if problem:
                self.store.update_proposal(proposal_id, status="expired", decided_at=now.isoformat(timespec="seconds"))
                self.store.post("executor", "info", f"{symbol}: ordren er ikke lagt. {problem}", symbol=symbol, proposal_id=proposal_id)
                raise ValueError(problem)
            sized = self._size(price, stop, now, cfg, exclude=proposal_id)
            qty = min(int(row.get("qty") or 0), sized.qty)
            if qty < 1:
                reason = sized.reason or "Størrelsen blev nul."
                self.store.update_proposal(proposal_id, status="sized_zero", decided_at=now.isoformat(timespec="seconds"))
                self.store.post("sizer", "size", f"{symbol}: 0. {reason}", symbol=symbol, proposal_id=proposal_id)
                raise ValueError(reason)
            try:
                order = broker.submit_bracket(symbol, "long", qty, stop, target)
            except Exception as exc:
                self.store.update_proposal(proposal_id, status="failed", decided_at=now.isoformat(timespec="seconds"))
                who = "Papirkontoen" if self.own_ledger else "Alpaca"
                self.store.post("executor", "error", f"{symbol}: {who} afviste ordren: {exc}", symbol=symbol, proposal_id=proposal_id)
                raise ValueError(f"{who} afviste ordren: {exc}") from exc
            fill = order.filled_avg_price
            self.store.update_proposal(
                proposal_id,
                status="placed",
                qty=qty,
                order_id=order.id,
                fill_price=fill,
                decided_at=now.isoformat(timespec="seconds"),
            )
            at = f"til {fill:.2f}" if fill else f"(markedsordre, kurs ca. {price:.2f})"
            self.store.post(
                "executor",
                "fill",
                f"Købt {qty} {symbol} {at} på papirkontoen"
                + (
                    f" (inkl. {self.base.fee_pct:g} % kurtage). Bordet holder selv øje med stop {stop:.2f} og mål {target:.2f}."
                    if self.own_ledger
                    else f". Stop {stop:.2f} og mål {target:.2f} ligger hos Alpaca."
                ),
                symbol=symbol,
                proposal_id=proposal_id,
                data={"order_id": order.id, "qty": qty},
            )
            notify(self.main_settings, f"{self.title}: købt {qty} {symbol} (papir). Stop {stop:.2f}, mål {target:.2f}.")
            return self.store.proposal(proposal_id) or {}

    def reject(self, proposal_id: int) -> None:
        with self._order_lock:
            row = self.store.proposal(proposal_id)
            if row is None:
                raise ValueError("Ukendt forslag.")
            if row["status"] != "awaiting":
                raise ValueError("Forslaget venter ikke på godkendelse.")
            now = self._now().astimezone(self.tz)
            self.store.update_proposal(proposal_id, status="rejected", decided_at=now.isoformat(timespec="seconds"))
            self.store.post("executor", "info", f"{row['symbol']}: afvist af dig. Ingen ordre.", symbol=row["symbol"], proposal_id=proposal_id)

    def _set_expired(self, row: dict, why: str) -> None:
        self.store.update_proposal(row["id"], status="expired", decided_at=self._now().isoformat(timespec="seconds"))
        self.store.post("executor", "info", f"{row['symbol']}: godkendelsen er udløbet. {why} Ingen ordre.", symbol=row["symbol"], proposal_id=row["id"])

    def _expire(self, now: datetime) -> None:
        with self._order_lock:
            for row in self.store.proposals_with_status("awaiting"):
                expires = _parse_ts(row.get("expires_at"))
                if expires is None or now >= expires:
                    self._set_expired(row, "Du nåede ikke at svare.")

    # ------------------------------------------------------------------ åbne handler
    def _sync(self, broker, now: datetime) -> None:
        placed = self.store.proposals_with_status("placed")
        if not placed:
            return
        held: set[str] | None = None
        for row in placed:
            order_id = row.get("order_id")
            if not order_id:
                continue
            try:
                order = broker.get_order(order_id)
            except BrokerError as exc:
                logger.info("Ordre %s kunne ikke læses: %s", order_id, exc)
                continue
            fill = row.get("fill_price") or order.filled_avg_price
            if fill and not row.get("fill_price"):
                self.store.update_proposal(row["id"], fill_price=float(fill))
            if order.exit_price is not None and fill:
                self._close(row, float(fill), float(order.exit_price), order.exit_reason or "close", now)
                continue
            if order.status == "canceled" and not fill:
                self.store.update_proposal(row["id"], status="failed", closed_at=now.isoformat(timespec="seconds"))
                self.store.post("executor", "error", f"{row['symbol']}: ordren blev ikke fyldt ({order.exit_reason}).", symbol=row["symbol"], proposal_id=row["id"])
                continue
            if order.status == "canceled" and fill:
                # Lukket uden om bordet, fx manuelt i Alpaca.
                if held is None:
                    held = {position.symbol for position in broker.get_positions()}
                if row["symbol"] not in held:
                    price = self._last_price(broker, row["symbol"], now) or float(fill)
                    self._close(row, float(fill), price, "outside", now)

    def _close(self, row: dict, fill: float, exit_price: float, reason: str, now: datetime) -> None:
        qty = int(row.get("qty") or 0)
        pnl = round((exit_price - fill) * qty, 2)
        self.store.update_proposal(
            row["id"],
            status="closed",
            fill_price=fill,
            exit_price=exit_price,
            exit_reason=reason,
            pnl=pnl,
            closed_at=now.isoformat(timespec="seconds"),
        )
        why = EXIT_DA.get(reason, reason)
        self.store.post(
            "executor",
            "close",
            f"Lukket {qty} {row['symbol']} ved {why} til {exit_price:.2f}. Resultat {self.market.money(pnl)}.",
            symbol=row["symbol"],
            proposal_id=row["id"],
            data={"pnl": pnl, "reason": reason},
        )
        notify(self.main_settings, f"{self.title}: lukket {row['symbol']} ({why}) {self.market.money(pnl)} (papir).")

    def _flatten(self, broker, now: datetime, message: str, *, reason: str = "flatten") -> int:
        with self._order_lock:
            for row in self.store.proposals_with_status("awaiting"):
                self._set_expired(row, "Bordet lukker for i dag.")
            placed = self.store.proposals_with_status("placed")
            if not placed:
                return 0
            self._sync(broker, now)
            placed = self.store.proposals_with_status("placed")
            if not placed:
                return 0
            prices = {position.symbol: position.market_price for position in broker.get_positions()}
            closed = 0
            for row in placed:
                symbol = row["symbol"]
                try:
                    broker.close_position(symbol)
                except BrokerError as exc:
                    self.store.post("executor", "error", f"{symbol} kunne ikke lukkes: {exc}", symbol=symbol, proposal_id=row["id"])
                    continue
                fill = float(row.get("fill_price") or row.get("entry") or 0)
                price = self._exit_after_close(broker, row) or prices.get(symbol) or self._last_price(broker, symbol, now) or fill
                self._close(row, fill, float(price), reason, now)
                closed += 1
            self.store.post("executor", "info", message)
            return closed

    @staticmethod
    def _exit_after_close(broker, row: dict) -> float | None:
        """Salgskursen, som mægleren selv bogførte (København: efter kurtage). None, hvis den ikke kender den."""
        if not row.get("order_id"):
            return None
        try:
            order = broker.get_order(row["order_id"])
        except Exception:
            return None
        return float(order.exit_price) if order.exit_price is not None else None

    def close_all(self) -> int:
        """Knappen 'Luk bordets positioner'. Rører kun bordets egne handler."""
        now = self._now().astimezone(self.tz)
        return self._flatten(self.broker(), now, "Bordets positioner er lukket efter din besked.", reason="manual")

    # ------------------------------------------------------------------ pause og chat
    def pause(self, paused: bool, *, by: str = "dig") -> None:
        self.store.set_state("paused", "1" if paused else "0")
        if paused:
            with self._order_lock:
                for row in self.store.proposals_with_status("awaiting"):
                    self._set_expired(row, "Bordet er sat på pause.")
            self.store.post(
                "system",
                "pause",
                f"Alt er sat på pause af {by}. Ingen nye markeringer og ingen ordrer. Åbne positioner beholder stop og "
                "mål. Skriv \"Start igen\" for at fortsætte.",
            )
        else:
            self.store.post("system", "pause", f"Bordet arbejder igen (startet af {by}).")

    def post_message(self, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            raise ValueError("Beskeden er tom.")
        text = text[:1000]
        message_id = self.store.post("me", "chat", text)
        plain = re.sub(r"[.!]+$", "", text.lower()).strip()
        if plain in STOP_WORDS:
            self.pause(True)
            return {"id": message_id, "replies": []}
        if plain in START_WORDS:
            self.pause(False)
            return {"id": message_id, "replies": []}
        mentioned = self._mentions(text)
        if not mentioned:
            self.store.post("system", "info", "Ingen bot er nævnt. Skriv @ og vælg en bot, fx @SKEPTIC hvorfor sagde du WAIT til NVDA?")
            return {"id": message_id, "replies": []}
        day = self._now().astimezone(self.tz).date().isoformat()
        asked = int(self.store.get_state(f"asked:{day}", "0") or "0")
        if asked >= self.base.max_chat_per_day:
            self.store.post("system", "info", f"Dagens grænse på {self.base.max_chat_per_day} spørgsmål til bottene er nået. Den nulstilles i morgen.")
            return {"id": message_id, "replies": []}
        self.store.set_state(f"asked:{day}", str(asked + 1))
        for key in mentioned[:2]:
            if self._background:
                self._pool.submit(self._answer, key, text)
            else:
                self._answer(key, text)
        return {"id": message_id, "replies": mentioned[:2]}

    @staticmethod
    def _mentions(text: str) -> list[str]:
        names = {info["name"].lower(): key for key, info in BOTS.items()}
        names.update({"skeptiker": "skeptic", "skeptikeren": "skeptic", "nyheder": "news", "alle": "*"})
        found: list[str] = []
        for word in re.findall(r"@([A-Za-zÆØÅæøå]+)", text):
            key = names.get(word.lower())
            if key == "*":
                return list(BOTS)[:2]
            if key and key not in found:
                found.append(key)
        return found

    def _answer(self, key: str, question: str) -> None:
        try:
            self.brain.settings = self.cfg
            history = self.store.messages(limit=40)
            answer = self.brain.reply(key, question, history, self._chat_context())
            if answer is None:
                answer = self._rules_answer(key)
            self.store.post(key, "chat", answer)
        except Exception as exc:  # svaret må aldrig vælte bordet
            logger.warning("Chat-svar fra %s fejlede: %s", key, exc)
            self.store.post("system", "error", f"{BOTS[key]['name']} kunne ikke svare: {exc.__class__.__name__}.")

    def _rules_answer(self, key: str) -> str:
        """Svar uden Claude: sig ærligt hvorfor, og giv bordets egne tal i stedet for regelteksten."""
        context = self._chat_context()
        cfg = self.cfg
        reason = self.brain.why_unavailable() or "Claude svarede ikke."
        prefix = f"Jeg kan ikke bruge Claude lige nu. {reason} Indtil da svarer jeg med bordets tal. "
        latest = next(iter(self.store.proposals(1)), None)
        if key == "news":
            news = (latest or {}).get("news")
            if news:
                return prefix + f"Seneste aktie, jeg undersøgte, var {latest['symbol']}: {news.get('reason') or 'Ingen klar grund.'}"
            return prefix + "Jeg har ikke undersøgt nogen aktie endnu. Jeg går i gang, så snart SCOUT markerer en."
        if key == "chart":
            plan = (latest or {}).get("chart")
            if plan and plan.get("setup"):
                return prefix + f"Seneste plan, {latest['symbol']}: indgang {plan['entry']:.2f}, stop {plan['stop']:.2f}, mål {plan['target']:.2f}."
            if plan:
                return prefix + f"Seneste, {latest['symbol']}: ingen setup. {plan.get('reason') or ''}".rstrip()
            return prefix + f"Ingen planer endnu. Jeg sætter stop højst {cfg.stop_max_pct:g} % under indgangen og målet mindst {cfg.reward_risk:g} gange så langt væk."
        if key == "skeptic":
            record = context["skeptic"]
            return prefix + f"Jeg har afgjort {record['rulings']} handler: {record['buy']} BUY og {record['refused']} nej. {record['verdict']}"
        if key == "sizer":
            return prefix + f"Højst {cfg.position_pct:g} % pr. handel og {cfg.max_open} åbne ad gangen. Lige nu: {context['open']} åbne eller ventende."
        if key == "scout":
            extra = f" (+{context['volume_flags_today']} kun på volumen)" if context["volume_flags_today"] else ""
            early = context["premarket"]
            gaps = (
                " Før åbning: " + ", ".join(f"{row['symbol']} {float(row['gap_pct']):+.1f} %" for row in early[:5]) + "."
                if early
                else (
                    f" Før åbning kigger jeg efter gaps på {cfg.gap_pct:g} % fra kl. 10 dansk tid."
                    if cfg.premarket and self.market.premarket_trading
                    else (f" Gaps på {cfg.gap_pct:g} % ved åbningen tæller også." if cfg.premarket else "")
                )
            )
            return prefix + (
                f"I dag: {context['flags_today']} af {cfg.max_flags_per_day} markeringer på kursbevægelse{extra}. "
                f"Grænse: {cfg.move_pct:g} % på en time.{gaps} Watchlist: {', '.join(cfg.watchlist)}."
            )
        if key == "executor":
            return prefix + f"Åbne handler: {context['open']}. Lukket i alt: {context['closed']} med {self.market.money(context['pnl'])}."
        return prefix

    def test_claude(self) -> dict:
        self.brain.settings = self.cfg
        return self.brain.test()

    def _chat_context(self) -> dict:
        now = self._now().astimezone(self.tz)
        trades = self.store.closed_trades()
        flags = self.flags_on(now.date().isoformat())
        return {
            "time_et": now.isoformat(timespec="minutes"),
            "market": f"{self.market.name} ({self.market.currency})",
            "paused": self.paused,
            "flags_today": len(flags["move"]),
            "volume_flags_today": len(flags["volume"]),
            "premarket": self._premarket_list(now.date().isoformat()),
            "open": self._open_count(),
            "closed": len(trades),
            "pnl": round(sum(float(row["pnl"]) for row in trades), 2),
            "skeptic": self.skeptic_record(),
            "watchlist": list(self.cfg.watchlist),
        }

    # ------------------------------------------------------------------ SKEPTIC's regnskab
    def skeptic_record(self) -> dict:
        rows = [row for row in self.store.proposals(500) if row.get("ruling")]
        buy = [row for row in rows if row["ruling"] == "BUY"]
        refused = [row for row in rows if row["ruling"] in ("WAIT", "SKIP")]
        judged = [row for row in refused if row.get("shadow")]
        right = [row for row in judged if (row["shadow"] or {}).get("result") == "loss"]
        bought = [row for row in buy if row.get("status") == "closed" and row.get("pnl") is not None]
        winners = [row for row in bought if float(row["pnl"]) > 0]
        rate = len(buy) / len(rows) if rows else 0.0
        if len(rows) >= 5 and rate > 0.5:
            verdict = "Jeg siger ja for tit. Stram min beskrivelse."
        elif rows:
            verdict = "Jeg afviser mere, end jeg godkender, som jeg skal."
        else:
            verdict = "Ingen afgørelser endnu."
        return {
            "rulings": len(rows),
            "buy": len(buy),
            "refused": len(refused),
            "buy_rate": round(rate, 3),
            "refusals_checked": len(judged),
            "refusals_right": len(right),
            "buys_closed": len(bought),
            "buys_won": len(winners),
            "too_soft": len(rows) >= 5 and rate > 0.5,
            "verdict": verdict,
        }

    # ------------------------------------------------------------------ dagslog og måned
    def _shadows(self, day: str, now: datetime) -> None:
        rows = [
            row
            for row in self.store.proposals_on(day)
            if row["status"] in REFUSALS and row.get("entry") and row.get("stop") and row.get("target") and not row.get("shadow")
        ]
        if not rows:
            return
        broker = self.broker()
        symbols = sorted({row["symbol"] for row in rows})
        end = self.market.at(now.date(), self.market.close_on(now.date()))
        flatten = self.market.at(now.date(), self.market.flatten_at(now.date()))
        bars = broker.get_intraday(symbols, min(now, end))
        for row in rows:
            flag = row.get("flag") or {}
            since = (_parse_ts(flag.get("ts")) or _parse_ts(row["created_at"]) or now) + timedelta(minutes=1)
            session = [bar for bar in bars.get(row["symbol"], []) if bar.ts < flatten]
            outcome = rules.shadow_outcome(float(row["entry"]), float(row["stop"]), float(row["target"]), session, since)
            if outcome is not None:
                self.store.update_proposal(row["id"], shadow=outcome)

    def learned(self) -> list[dict]:
        return learning.evaluate(self.store.proposals(500))

    def day_log(self, day: str | None = None, now: datetime | None = None) -> str:
        now = (now or self._now()).astimezone(self.tz)
        day = day or now.date().isoformat()
        before = self.learned()
        try:
            self._shadows(day, now)
        except Exception as exc:
            logger.info("Skyggeregnskab fejlede: %s", exc)
        after = self.learned()
        lessons = learning.day_lessons(self.store.proposals_on(day), before, after)
        if lessons:
            self.store.set_json("lessons", learning.add_lessons(self.store.get_json("lessons", []) or [], day, lessons))
            self.store.post("skeptic", "lesson", "Lært i dag: " + " ".join(lessons))
        rows = self.store.proposals_on(day)
        lines = [f"Dagslog {day}."]
        trades = [row for row in rows if row["status"] in ("placed", "closed")]
        if trades:
            for row in trades:
                if row["status"] == "closed":
                    why = EXIT_DA.get(row.get("exit_reason") or "", row.get("exit_reason") or "")
                    lines.append(
                        f"{row['symbol']}: {row['qty']} købt til {float(row['fill_price']):.2f}, solgt til "
                        f"{float(row['exit_price']):.2f} ({why}), {self.market.money(float(row['pnl']))}."
                    )
                else:
                    lines.append(f"{row['symbol']}: {row['qty']} stadig åben.")
            total = sum(float(row["pnl"]) for row in trades if row.get("pnl") is not None)
            lines.append(f"Resultat i dag: {self.market.money(total)}.")
        else:
            lines.append("Ingen handler i dag.")
        refused = [row for row in rows if row["status"] in REFUSALS]
        if rows:
            lines.append(f"{len(rows)} markeret, {len(refused)} sagt nej til eller ikke godkendt.")
        checked = [row for row in refused if row.get("shadow")]
        if checked:
            right = [row for row in checked if row["shadow"]["result"] == "loss"]
            lines.append(f"Skyggeregnskab: {len(right)} af {len(checked)} afviste handler ville have tabt.")
        try:
            equity = self.broker().get_account().equity
            lines.append(f"Saldo på papirkontoen: {equity:,.2f} {self.market.currency}.")
        except Exception as exc:
            lines.append(f"Saldoen kunne ikke hentes: {exc}")
        text = " ".join(lines)
        self.store.set_state(f"log:{day}", text)
        self.store.post("executor", "log", text)
        notify(self.main_settings, f"{self.title}: " + text)
        return text

    def diagnose(self) -> dict:
        """Knappen 'Test Alpaca-forbindelsen': hver del for sig, så man kan se præcis, hvad der fejler."""
        key, secret, key_source, secret_source = self.keys()
        keys = {
            "key_source": key_source or ("testmægler" if self._injected_broker else "mangler"),
            "secret_source": secret_source or ("testmægler" if self._injected_broker else "mangler"),
            "key_start": key[:2] if key else "",
            "key_length": len(key),
            "secret_length": len(secret),
            "hints": [],
        }
        if key_source and secret_source and key_source.startswith("DESK") != secret_source.startswith("DESK"):
            keys["hints"].append(f"Key kommer fra {key_source}, men Secret fra {secret_source}. De skal være et par fra samme sted.")
        problem = key_problem(key)
        if problem:
            keys["hints"].append(problem)
        if key and len(key) < 16:
            keys["hints"].append(f"Key er kun {len(key)} tegn. Alpaca-nøgler er normalt omkring 20 tegn, så den er måske ikke kopieret helt.")
        if secret and len(secret) < 30:
            keys["hints"].append(f"Secret er kun {len(secret)} tegn. Den er normalt omkring 40 tegn, så den er måske ikke kopieret helt.")
        if secret.upper().startswith(("PK", "AK")) and not key.upper().startswith(("PK", "AK")):
            keys["hints"].append("Secret starter med PK eller AK. Key og Secret er nok byttet om.")
        if self.own_ledger:
            keys = {"key_source": "ingen (bordets egen papirkonto)", "secret_source": "", "key_start": "", "key_length": 0, "secret_length": 0, "hints": []}
        elif not self.keys_present:
            return {"ok": False, "keys": keys, "checks": [], "text": "Ingen nøgler fundet. Sæt DESK_ALPACA_API_KEY og DESK_ALPACA_SECRET_KEY i Railway og genstart."}
        try:
            broker = self.broker()
        except BrokerError as exc:
            return {"ok": False, "keys": keys, "checks": [], "text": str(exc)}
        now = self._now().astimezone(self.tz)
        bench = self.market.benchmark
        probe = self.cfg.watchlist[0] if self.cfg.watchlist else bench
        tests = [
            ("Handelskontoen", lambda: f"Saldo {broker.get_account().equity:,.2f} {self.market.currency}"),
            ("Markedets ur", lambda: "Åbent" if broker.get_clock().is_open else "Lukket lige nu (det er i orden)"),
            ("Kursdata", lambda: f"{len(broker.get_daily([bench], 5, now).get(bench) or [])} dagsbarer for {self.market.benchmark_name}"),
            ("Nyheder", lambda: f"{len(broker.get_news([probe], now))} overskrifter for {probe}"),
        ]
        checks = []
        for name, run in tests:
            try:
                checks.append({"name": name, "ok": True, "detail": run()})
            except Exception as exc:
                checks.append({"name": name, "ok": False, "detail": str(exc)})
        ok = all(item["ok"] for item in checks)
        if ok:
            self.last_error = ""
            text = "Alt virker. Bordet kan handle på papirkontoen."
        elif self.own_ledger:
            text = "Yahoo Finance svarer ikke på alt lige nu. Se detaljerne. Bordet prøver igen hvert minut."
        elif not checks[0]["ok"]:
            text = "Handelskontoen afviser nøglerne. Se tipsene ovenfor, eller lav et nyt nøglesæt på Paper-kontoen."
        else:
            text = "Handelskontoen virker, men en af de andre dele fejler. Se detaljerne."
        return {"ok": ok, "keys": keys, "checks": checks, "text": text}

    def compare(self) -> dict:
        """Bordet mod at holde børsens indeks (S&P 500 eller C25) siden bordets første dag."""
        start = self.store.get_json("desk_start")
        trades = self.store.closed_trades()
        pnl = round(sum(float(row["pnl"]) for row in trades), 2)
        result: dict[str, Any] = {"started": bool(start), "trades": len(trades), "pnl": pnl}
        if not start:
            result["text"] = f"Bordet starter første gang markedet er åbent. Så måles det mod {self.market.benchmark_name} derfra."
            return result
        now = self._now().astimezone(self.tz)
        equity0 = float(start.get("equity") or 0)
        desk_pct = pnl / equity0 * 100.0 if equity0 else 0.0
        spy_now = None
        try:
            spy_now = self._last_price(self.broker(), self.market.benchmark, now)
        except Exception as exc:
            result["error"] = str(exc)
        spy0 = start.get("spy")
        spy_pct = (spy_now - spy0) / spy0 * 100.0 if spy_now and spy0 else None
        started = datetime.fromisoformat(start["date"]).date()
        days = (now.date() - started).days
        wins = [row for row in trades if float(row["pnl"]) > 0]
        result.update(
            {
                "start_date": start["date"],
                "days": days,
                "desk_pct": round(desk_pct, 2),
                "spy_pct": round(spy_pct, 2) if spy_pct is not None else None,
                "win_rate": round(len(wins) / len(trades), 3) if trades else None,
                "beats_spy": (desk_pct > spy_pct) if spy_pct is not None else None,
            }
        )
        index = self.market.benchmark_name
        result["benchmark_name"] = index
        if spy_pct is None:
            result["text"] = f"Bordet: {desk_pct:+.2f}% siden {start['date']}. {index} kunne ikke hentes."
        elif desk_pct > spy_pct:
            result["text"] = f"Bordet: {desk_pct:+.2f}%, {index}: {spy_pct:+.2f}% siden {start['date']}. Bordet slår indtil videre at holde {index}."
        else:
            result["text"] = f"Bordet: {desk_pct:+.2f}%, {index}: {spy_pct:+.2f}% siden {start['date']}. Bordet slår ikke at holde {index} endnu."
        return result

    # ------------------------------------------------------------------ dashboard
    def _market(self) -> dict:
        """Børsens fase og næste tider. Den lokale kalender giver altid et svar; Alpacas ur vinder, når det svarer."""
        out: dict[str, Any] = {**calendar.schedule(self._now(), self.market), "source": "kalender"}
        # Status må aldrig vente på Alpaca: uret kommer fra baggrundstråden, der henter det hvert minut.
        hit = self._cache.get("clock") if self.keys_present else None
        # Højst to minutter gammelt, så åbning og lukning ikke halter; ellers bruges kalenderen.
        clock = hit[1] if hit and self._now().timestamp() - hit[0] < 120 else None
        if clock is not None:
            if clock.is_open != out["is_open"]:
                out["phase"] = "open" if clock.is_open else ("closed" if out["phase"] == "open" else out["phase"])
            out["is_open"] = clock.is_open
            if clock.next_open:
                out["next_open"] = clock.next_open.astimezone(self.tz).isoformat(timespec="seconds")
            if clock.next_close:
                out["next_close"] = clock.next_close.astimezone(self.tz).isoformat(timespec="seconds")
            out["source"] = "alpaca" if not self.own_ledger else "kalender"
        labels = {"open": "Åbent", "pre": "Før åbning", "after": "Efter lukning", "closed": "Lukket"}
        out["label"] = labels.get(out["phase"], "Lukket")
        if out.get("holiday") and not out["is_open"]:
            out["label"] = f"Lukket · {out['holiday']}"
        return out

    def _ledger_info(self) -> dict | None:
        """Den danske papirkonto: startbeløb, kontanter og kurtage. None i New York (Alpaca fører kontoen)."""
        if not self.own_ledger:
            return None
        ledger = self.store.get_json("paper_ledger") or {}
        return {
            "start": float(ledger.get("start") or self.base.start_cash),
            "cash": float(ledger["cash"]) if ledger.get("cash") is not None else float(self.base.start_cash),
            "fee_pct": self.base.fee_pct,
            "trades": len(ledger.get("orders") or {}),
        }

    def _account(self) -> dict | None:
        """Kontoen, som baggrundstråden sidst hentede. Ingen netværkskald her, så siden altid svarer hurtigt."""
        if not self.keys_present:
            return None
        hit = self._cache.get("account")
        if not hit or self._now().timestamp() - hit[0] > 600:
            return None
        account = hit[1]
        return {"equity": account.equity, "buying_power": account.buying_power}

    def status(self, after: int = 0) -> dict:
        now = self._now().astimezone(self.tz)
        cfg = self.cfg
        day = now.date().isoformat()
        flags = self.flags_on(day)
        proposals = self.store.proposals(120)
        trades = self.store.closed_trades()
        pnl = round(sum(float(row["pnl"]) for row in trades), 2)
        wins = [row for row in trades if float(row["pnl"]) > 0]
        record = self.skeptic_record()
        alerts: list[dict] = []
        if not self.keys_present:
            alerts.append({"level": "warning", "text": "Mangler Alpaca-papirnøgler. Sæt DESK_ALPACA_API_KEY og DESK_ALPACA_SECRET_KEY i Railway."})
        problem = key_problem(self.keys()[0]) if not self._injected_broker and not self.own_ledger else ""
        if problem:
            alerts.append({"level": "error", "text": problem})
        if not self.brain.configured:
            alerts.append({"level": "info", "text": "Ingen Claude-nøgle. NEWS og SKEPTIC kører på faste regler, og SKEPTIC er strengere end normalt."})
        elif self.brain.limited and self.brain.budget_left() < 0.15:
            alerts.append({"level": "info", "text": "Dagens AI-budget er brugt. Reglerne tager over til i morgen."})
        elif self.brain.last_error:
            alerts.append({"level": "error", "text": f"Claude: {self.brain.last_error}"})
        if record["too_soft"]:
            alerts.append({"level": "warning", "text": "SKEPTIC godkender mere end halvdelen. Den skal sige nej langt oftere end ja."})
        if cfg.auto_approve:
            alerts.append({"level": "info", "text": "Autopilot er slået til: bordet køber selv på papir, når SKEPTIC siger BUY. \"Stop nu\" sætter alt på pause."})
        if self.paused:
            alerts.append({"level": "warning", "text": "Bordet er på pause. Skriv \"Start igen\" i chatten for at fortsætte."})
        if self.last_error:
            alerts.append({"level": "error", "text": f"Seneste fejl: {self.last_error}"})
        messages = self.store.messages(after=after) if after else self.store.messages(limit=250)
        return {
            "server_time_et": now.isoformat(timespec="seconds"),
            "mode": "paper",
            "market_info": {
                "key": self.market.key,
                "name": self.market.name,
                "city": self.market.city,
                "country": self.market.country,
                "tz": self.market.tz_name,
                "currency": self.market.currency,
                "sign": self.market.sign,
                "benchmark": self.market.benchmark,
                "benchmark_name": self.market.benchmark_name,
                "broker": self.market.broker,
                "source": self.source,
                "premarket_trading": self.market.premarket_trading,
                "scan_start": self.market.hhmm(self.market.scan_start),
                "fee_pct": self.base.fee_pct if self.own_ledger else 0.0,
            },
            "ledger": self._ledger_info(),
            "paused": self.paused,
            "scanning": self.scanning,
            "market": self._market(),
            "account": self._account(),
            "keys_present": self.keys_present,
            "own_account": self.keys_present,
            "keys_saved_on_page": bool((self.store.get_json("alpaca_keys") or {}).get("key")),
            "ai": {
                "configured": self.brain.configured,
                "model": self.base.ai_model,
                "budget_usd": self.base.ai_daily_budget_usd,
                "limited": self.brain.limited,
                "reason": self.brain.why_unavailable(),
                "last_error": self.brain.last_error,
                "last_ok": self.brain.last_ok,
                "spent_today_usd": round(self.store.spend_on(day), 4),
            },
            "params": {
                "watchlist": list(cfg.watchlist),
                "move_pct": cfg.move_pct,
                "volume_x": cfg.volume_x,
                "max_flags_per_day": cfg.max_flags_per_day,
                "stop_max_pct": cfg.stop_max_pct,
                "reward_risk": cfg.reward_risk,
                "position_pct": cfg.position_pct,
                "max_open": cfg.max_open,
                "approval_minutes": cfg.approval_minutes,
                "scan_minutes": cfg.scan_minutes,
                "auto_approve": cfg.auto_approve,
                "premarket": cfg.premarket,
                "gap_pct": cfg.gap_pct,
                "daily_loss_pct": cfg.daily_loss_pct,
                "max_chase_pct": cfg.max_chase_pct,
                "log_time_et": self.market.hhmm(self.market.log_at(now.date())),
                "hard": {
                    "position_pct": HARD_MAX_POSITION_PCT,
                    "max_open": HARD_MAX_OPEN,
                    "stop_max_pct": HARD_MAX_STOP_PCT,
                    "reward_risk_min": HARD_MIN_REWARD_RISK,
                    "max_flags_per_day": HARD_MAX_FLAGS,
                },
            },
            "bots": [
                {"key": key, "name": info["name"], "job": info["job"], "tag": info["tag"], "engine": info["engine"], "rules": bot_rules(key, cfg)}
                for key, info in BOTS.items()
            ],
            "messages": messages,
            "last_message_id": messages[-1]["id"] if messages else after,
            "approvals": [_proposal(row) for row in self.store.proposals_with_status("awaiting")],
            "open": [_proposal(row) for row in self.store.proposals_with_status("placed")],
            "proposals": [_proposal(row) for row in proposals],
            "today": {
                "flags": len(flags["move"]),
                "volume_flags": len(flags["volume"]),
                "max_flags": cfg.max_flags_per_day,
                "open": self._open_count(),
                "max_open": cfg.max_open,
            },
            "quotes": self._quotes,
            "premarket": self._premarket_list(day),
            "systems": {
                "last_scan": self.last_scan,
                "last_tick": self.last_tick,
                "telegram": bool(self.main_settings.telegram_bot_token and self.main_settings.telegram_chat_id),
            },
            "equity_curve": _curve(trades),
            "kpis": {
                "trades": len(trades),
                "pnl": pnl,
                "win_rate": round(len(wins) / len(trades), 3) if trades else None,
            },
            "skeptic": record,
            "learning": {
                "rules": self.learned(),
                "lessons": (self.store.get_json("lessons", []) or [])[:12],
                "min_trades": learning.LEARN_MIN_TRADES,
            },
            "alerts": alerts,
            "engine": {"last_tick": self.last_tick, "last_error": self.last_error},
        }


def _curve(trades: list[dict], points: int = 60) -> list[float]:
    """Bordets samlede resultat efter hver lukket handel (de seneste `points`)."""
    total = 0.0
    curve = [0.0]
    for row in trades:
        total += float(row["pnl"])
        curve.append(round(total, 2))
    return curve[-points:]


def _proposal(row: dict) -> dict:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "trading_day": row["trading_day"],
        "symbol": row["symbol"],
        "status": row["status"],
        "status_label": STATUS_DA.get(row["status"], row["status"]),
        "ruling": row.get("ruling"),
        "entry": row.get("entry"),
        "stop": row.get("stop"),
        "target": row.get("target"),
        "qty": row.get("qty"),
        "fill_price": row.get("fill_price"),
        "exit_price": row.get("exit_price"),
        "exit_reason": row.get("exit_reason"),
        "pnl": row.get("pnl"),
        "expires_at": row.get("expires_at"),
        "flag": row.get("flag"),
        "news": row.get("news"),
        "chart": row.get("chart"),
        "skeptic": row.get("skeptic"),
        "size": row.get("size"),
        "shadow": row.get("shadow"),
    }
