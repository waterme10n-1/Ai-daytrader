"""Handelsbordet: SCOUT → NEWS → CHART → SKEPTIC → SIZER → EXECUTOR, og du godkender hver ordre.

Bordet handler kun på papirpenge hos Alpaca. Der findes ingen kodevej til rigtige penge her.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from trader.config import Settings
from trader.desk.alpaca import ET_NAME, AlpacaPaper, BrokerError
from trader.desk import rules
from trader.desk.agents import BOTS, DeskBrain, bot_rules
from trader.desk.config import (
    HARD_MAX_FLAGS,
    HARD_MAX_OPEN,
    HARD_MAX_POSITION_PCT,
    HARD_MAX_STOP_PCT,
    HARD_MIN_REWARD_RISK,
    DeskSettings,
    key_problem,
    parse_watchlist,
)
from trader.desk.store import DeskStore
from trader.notify import send as notify

logger = logging.getLogger("trader.desk")
ET = ZoneInfo(ET_NAME)

SCAN_START = (9, 45)
SCAN_END = (15, 30)
FLATTEN_AT = (15, 50)
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


def _usd(value: float) -> str:
    return f"{value:+,.2f} USD"


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
        self.main_settings = main_settings
        self.store = store or DeskStore(settings.db_path)
        self._broker = broker
        self._injected_broker = broker is not None
        self.brain = brain or DeskBrain(settings, self.store, settings.anthropic_api_key)
        self._now = now_fn or (lambda: datetime.now(ET))
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
        if self.store.get_state("welcome") is None:
            self.store.set_state("welcome", "1")
            self.store.post(
                "system",
                "info",
                "Handelsbordet er åbnet. Rækkefølgen er altid: SCOUT markerer, NEWS finder grunden, CHART sætter "
                "indgang, mål og stop, SKEPTIC afgør BUY, WAIT eller SKIP, SIZER sætter størrelsen, og EXECUTOR spørger "
                "dig, før ordren lægges. Bordet arbejder i amerikansk åbningstid på hverdage og kun på papirpenge. "
                "Skriv @ og en bot for at tale med den, eller \"Stop nu\" for at sætte alt på pause.",
            )

    # ------------------------------------------------------------------ indstillinger
    @property
    def cfg(self) -> DeskSettings:
        params = self.store.get_json("params", {}) or {}
        values: dict[str, Any] = {}
        watch = parse_watchlist(params.get("watchlist")) if params.get("watchlist") is not None else []
        if watch:
            values["watchlist"] = tuple(watch)
        for name in ("move_pct", "volume_x", "position_pct", "stop_max_pct", "reward_risk"):
            if isinstance(params.get(name), (int, float)):
                values[name] = float(params[name])
        for name in ("max_open", "max_flags_per_day", "scan_minutes"):
            if isinstance(params.get(name), int):
                values[name] = int(params[name])
        if isinstance(params.get("auto_approve"), bool):
            values["auto_approve"] = params["auto_approve"]
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
        }
        changes: list[str] = []
        if "auto_approve" in body and body["auto_approve"] is not None:
            auto = body["auto_approve"] in (True, "true", "1", 1, "on")
            params["auto_approve"] = auto
            changes.append("autopilot " + ("til: bordet køber selv på papir" if auto else "fra: du godkender hver ordre"))
        if "watchlist" in body:
            watch = parse_watchlist(body.get("watchlist"))
            if not watch:
                raise ValueError("Watchlisten skal have mindst én gyldig ticker (fx NVDA).")
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
        self.store.set_json("params", params)
        if changes:
            self.store.post("system", "info", "Indstillinger ændret: " + "; ".join(changes) + ".")

    # ------------------------------------------------------------------ broker
    @property
    def keys_present(self) -> bool:
        return self._injected_broker or self.base.own_keys

    def broker(self):
        if self._broker is None:
            # Altid papir. Bordet har ingen adresse til rigtige penge.
            self._broker = AlpacaPaper(self.base.alpaca_api_key, self.base.alpaca_secret_key)
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
        now = self._now().astimezone(ET)
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
        logger.warning("Handelsbordet: %s", exc)
        stamp = self._now().timestamp()
        if stamp - self._last_error_post > 1800:
            self._last_error_post = stamp
            self.store.post("system", "error", f"Fejl ved forbindelsen til Alpaca: {exc}")

    def _tick(self, now: datetime) -> None:
        self.last_tick = now.isoformat(timespec="seconds")
        broker = self.broker()
        self._expire(now)
        clock = broker.get_clock()
        self._cache["clock"] = (self._now().timestamp(), clock)
        day = now.date().isoformat()
        if clock.is_open:
            self._mark_open_day(day, broker, now)
            self._sync(broker, now)
            if (now.hour, now.minute) >= FLATTEN_AT:
                self._flatten(broker, now, "Klokken er 15:50. Bordets positioner lukkes. Intet holdes natten over.")
                return
            if self.paused:
                return
            if SCAN_START <= (now.hour, now.minute) < SCAN_END and (now.minute % self.cfg.scan_minutes == 0):
                self.scan(now)
            return
        self._sync(broker, now)
        hh, mm = (int(part) for part in self.base.log_time_et.split(":"))
        if (now.hour, now.minute) >= (hh, mm) and self.store.get_state(f"seen_open:{day}") == "1":
            if self.store.get_state(f"log:{day}") is None:
                self.day_log(day, now)

    def _mark_open_day(self, day: str, broker, now: datetime) -> None:
        if self.store.get_state(f"seen_open:{day}") == "1":
            return
        account = broker.get_account()
        self.store.set_state(f"seen_open:{day}", "1")
        self.store.set_state(f"day_start:{day}", str(account.equity))
        if self.store.get_json("desk_start") is None:
            spy = self._last_price(broker, "SPY", now)
            self.store.set_json(
                "desk_start",
                {"date": day, "equity": account.equity, "spy": spy},
            )
            self.store.post(
                "system",
                "info",
                f"Papirmåneden starter i dag med {account.equity:,.0f} USD. Uge 1: godkend intet. Læs med, og tjek at "
                "SKEPTIC faktisk siger nej.",
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
            now = (now or self._now()).astimezone(ET)
            cfg = self.cfg
            day = now.date().isoformat()
            flagged = self.store.flagged_today(day)
            left = cfg.max_flags_per_day - len(flagged)
            if left <= 0:
                return 0
            symbols = [symbol for symbol in cfg.watchlist if symbol not in flagged]
            if not symbols:
                return 0
            broker = self.broker()
            intraday = broker.get_intraday(symbols, now)
            daily = self._daily(broker, symbols, now)
            found = []
            for symbol in symbols:
                flag = rules.scout(
                    symbol,
                    intraday.get(symbol, []),
                    daily.get(symbol, []),
                    now,
                    move_pct=cfg.move_pct,
                    volume_x=cfg.volume_x,
                )
                if flag is not None:
                    found.append(flag)
            found.sort(key=lambda item: (abs(item.move_pct), item.volume_x), reverse=True)
            for flag in found[:left]:
                if self.paused:
                    break
                self._pipeline(flag, intraday.get(flag.symbol, []), now, cfg)
            return min(len(found), left)
        finally:
            self.scanning = False
            self._scan_lock.release()

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
        day = now.date().isoformat()
        symbol = flag.symbol
        pid = self.store.create_proposal(day, symbol, flag.to_dict())
        self.store.post("scout", "flag", rules.scout_text(flag), symbol=symbol, proposal_id=pid, data=flag.to_dict())

        # NEWS
        try:
            items = self.broker().get_news([symbol], now)
        except BrokerError as exc:
            logger.info("Nyheder for %s kunne ikke hentes: %s", symbol, exc)
            items = []
        headlines = [
            {
                "headline": item.headline,
                "created_at": item.created_at.isoformat(timespec="minutes"),
                "summary": (item.summary or "")[:300],
                "url": getattr(item, "url", ""),
            }
            for item in items
            if symbol in (item.symbols or [symbol])
        ]
        news = self.brain.news(flag.to_dict(), headlines)
        self.store.update_proposal(pid, news=news)
        self.store.post(
            "news",
            "news",
            f"{symbol}: {news['reason']}" if news.get("has_reason") else f"{symbol}: Ingen klar grund.",
            symbol=symbol,
            proposal_id=pid,
            data=news,
        )

        # CHART
        plan = rules.chart(flag, bars, now, stop_max_pct=cfg.stop_max_pct, reward_risk=cfg.reward_risk)
        self.store.update_proposal(pid, chart=plan.to_dict())
        self.store.post("chart", "chart", rules.chart_text(symbol, plan), symbol=symbol, proposal_id=pid, data=plan.to_dict())
        if not plan.setup:
            self.store.update_proposal(pid, status="no_setup")
            return
        self.store.update_proposal(pid, entry=plan.entry, stop=plan.stop, target=plan.target)

        # SKEPTIC
        verdict = self.brain.skeptic(flag.to_dict(), news, plan.to_dict(), self.skeptic_record())
        if verdict is None:
            verdict = rules.rules_skeptic(flag.to_dict(), news, plan.to_dict(), now, stop_max_pct=cfg.stop_max_pct)
        if not news.get("has_reason") and verdict["ruling"] != "SKIP":
            verdict["ruling"] = "SKIP"
            verdict["summary"] = "Ingen klar grund fra NEWS, så reglen siger SKIP."
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
            self.store.post("sizer", "size", rules.size_text(symbol, sized), symbol=symbol, proposal_id=pid, data=sized.to_dict())
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
            f"mål {plan.target:.2f}. Udløber kl. {expires.strftime('%H:%M')} ET."
        )
        self.store.post("executor", "approval", text, symbol=symbol, proposal_id=pid, data={"expires_at": expires.isoformat()})
        notify(self.main_settings, f"Handelsbordet: {text} Åbn /desk for at godkende.")

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
            now = self._now().astimezone(ET)
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
            if not broker.get_clock().is_open or (now.hour, now.minute) >= FLATTEN_AT:
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
                self.store.post("executor", "error", f"{symbol}: Alpaca afviste ordren: {exc}", symbol=symbol, proposal_id=proposal_id)
                raise ValueError(f"Alpaca afviste ordren: {exc}") from exc
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
                f"Købt {qty} {symbol} {at} på papirkontoen. Stop {stop:.2f} og mål {target:.2f} ligger hos Alpaca.",
                symbol=symbol,
                proposal_id=proposal_id,
                data={"order_id": order.id, "qty": qty},
            )
            notify(self.main_settings, f"Handelsbordet: købt {qty} {symbol} (papir). Stop {stop:.2f}, mål {target:.2f}.")
            return self.store.proposal(proposal_id) or {}

    def reject(self, proposal_id: int) -> None:
        with self._order_lock:
            row = self.store.proposal(proposal_id)
            if row is None:
                raise ValueError("Ukendt forslag.")
            if row["status"] != "awaiting":
                raise ValueError("Forslaget venter ikke på godkendelse.")
            now = self._now().astimezone(ET)
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
            f"Lukket {qty} {row['symbol']} ved {why} til {exit_price:.2f}. Resultat {_usd(pnl)}.",
            symbol=row["symbol"],
            proposal_id=row["id"],
            data={"pnl": pnl, "reason": reason},
        )
        notify(self.main_settings, f"Handelsbordet: lukket {row['symbol']} ({why}) {_usd(pnl)} (papir).")

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
                price = prices.get(symbol) or self._last_price(broker, symbol, now) or fill
                self._close(row, fill, float(price), reason, now)
                closed += 1
            self.store.post("executor", "info", message)
            return closed

    def close_all(self) -> int:
        """Knappen 'Luk bordets positioner'. Rører kun bordets egne handler."""
        now = self._now().astimezone(ET)
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
                "mål hos Alpaca. Skriv \"Start igen\" for at fortsætte.",
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
        day = self._now().astimezone(ET).date().isoformat()
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
            history = self.store.messages(limit=40)
            answer = self.brain.reply(key, question, history, self._chat_context())
            if answer is None:
                answer = self._rules_answer(key)
            self.store.post(key, "chat", answer)
        except Exception as exc:  # svaret må aldrig vælte bordet
            logger.warning("Chat-svar fra %s fejlede: %s", key, exc)
            self.store.post("system", "error", f"{BOTS[key]['name']} kunne ikke svare: {exc.__class__.__name__}.")

    def _rules_answer(self, key: str) -> str:
        context = self._chat_context()
        prefix = "Claude er ikke sat op (eller dagens AI-budget er brugt), så jeg svarer kun med bordets tal. "
        if key == "skeptic":
            record = context["skeptic"]
            return prefix + f"Jeg har afgjort {record['rulings']} handler: {record['buy']} BUY og {record['refused']} nej. {record['verdict']}"
        if key == "sizer":
            return prefix + f"Højst {self.cfg.position_pct:g}% pr. handel og {self.cfg.max_open} åbne ad gangen. Lige nu: {context['open']} åbne eller ventende."
        if key == "scout":
            return prefix + f"I dag: {context['flags_today']} af {self.cfg.max_flags_per_day} markeringer. Watchlist: {', '.join(self.cfg.watchlist)}."
        if key == "executor":
            return prefix + f"Åbne handler: {context['open']}. Lukket i alt: {context['closed']} med {_usd(context['pnl'])}."
        return prefix + bot_rules(key, self.cfg)

    def _chat_context(self) -> dict:
        now = self._now().astimezone(ET)
        trades = self.store.closed_trades()
        return {
            "time_et": now.isoformat(timespec="minutes"),
            "paused": self.paused,
            "flags_today": len(self.store.flagged_today(now.date().isoformat())),
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
        end = datetime.combine(now.date(), time(16, 0), tzinfo=ET)
        bars = broker.get_intraday(symbols, min(now, end))
        for row in rows:
            flag = row.get("flag") or {}
            since = (_parse_ts(flag.get("ts")) or _parse_ts(row["created_at"]) or now) + timedelta(minutes=1)
            session = [bar for bar in bars.get(row["symbol"], []) if bar.ts < datetime.combine(now.date(), time(15, 50), tzinfo=ET)]
            outcome = rules.shadow_outcome(float(row["entry"]), float(row["stop"]), float(row["target"]), session, since)
            if outcome is not None:
                self.store.update_proposal(row["id"], shadow=outcome)

    def day_log(self, day: str | None = None, now: datetime | None = None) -> str:
        now = (now or self._now()).astimezone(ET)
        day = day or now.date().isoformat()
        try:
            self._shadows(day, now)
        except Exception as exc:
            logger.info("Skyggeregnskab fejlede: %s", exc)
        rows = self.store.proposals_on(day)
        lines = [f"Dagslog {day}."]
        trades = [row for row in rows if row["status"] in ("placed", "closed")]
        if trades:
            for row in trades:
                if row["status"] == "closed":
                    why = EXIT_DA.get(row.get("exit_reason") or "", row.get("exit_reason") or "")
                    lines.append(
                        f"{row['symbol']}: {row['qty']} købt til {float(row['fill_price']):.2f}, solgt til "
                        f"{float(row['exit_price']):.2f} ({why}), {_usd(float(row['pnl']))}."
                    )
                else:
                    lines.append(f"{row['symbol']}: {row['qty']} stadig åben.")
            total = sum(float(row["pnl"]) for row in trades if row.get("pnl") is not None)
            lines.append(f"Resultat i dag: {_usd(total)}.")
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
            lines.append(f"Saldo på papirkontoen: {equity:,.2f} USD.")
        except Exception as exc:
            lines.append(f"Saldoen kunne ikke hentes: {exc}")
        text = " ".join(lines)
        self.store.set_state(f"log:{day}", text)
        self.store.post("executor", "log", text)
        notify(self.main_settings, "Handelsbordet: " + text)
        return text

    def compare(self) -> dict:
        """Bordet mod at holde S&P 500 (SPY) siden papirmånedens første dag."""
        start = self.store.get_json("desk_start")
        trades = self.store.closed_trades()
        pnl = round(sum(float(row["pnl"]) for row in trades), 2)
        result: dict[str, Any] = {"started": bool(start), "trades": len(trades), "pnl": pnl}
        if not start:
            result["text"] = "Papirmåneden er ikke startet endnu. Den starter første gang markedet er åbent."
            return result
        now = self._now().astimezone(ET)
        equity0 = float(start.get("equity") or 0)
        desk_pct = pnl / equity0 * 100.0 if equity0 else 0.0
        spy_now = None
        try:
            spy_now = self._last_price(self.broker(), "SPY", now)
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
                "week": days // 7 + 1,
                "desk_pct": round(desk_pct, 2),
                "spy_pct": round(spy_pct, 2) if spy_pct is not None else None,
                "win_rate": round(len(wins) / len(trades), 3) if trades else None,
                "beats_spy": (desk_pct > spy_pct) if spy_pct is not None else None,
            }
        )
        if spy_pct is None:
            result["text"] = f"Bordet: {desk_pct:+.2f}% siden {start['date']}. SPY kunne ikke hentes."
        elif desk_pct > spy_pct:
            result["text"] = f"Bordet: {desk_pct:+.2f}%, SPY: {spy_pct:+.2f}% siden {start['date']}. Bordet slår indtil videre at holde S&P 500."
        else:
            result["text"] = (
                f"Bordet: {desk_pct:+.2f}%, SPY: {spy_pct:+.2f}% siden {start['date']}. Bordet slår ikke at holde S&P 500. "
                "Gør det det ikke på papir, hjælper rigtige penge ikke."
            )
        return result

    # ------------------------------------------------------------------ dashboard
    def _market(self) -> dict:
        if not self.keys_present:
            return {"is_open": None, "label": "Ukendt uden nøgler"}
        try:
            clock = self._cached("clock", lambda: self.broker().get_clock())
        except Exception as exc:
            return {"is_open": None, "label": "Ukendt", "error": str(exc)}
        return {
            "is_open": clock.is_open,
            "label": "Åbent" if clock.is_open else "Lukket",
            "next_open": clock.next_open.astimezone(ET).isoformat(timespec="minutes") if clock.next_open else None,
            "next_close": clock.next_close.astimezone(ET).isoformat(timespec="minutes") if clock.next_close else None,
        }

    def _account(self) -> dict | None:
        if not self.keys_present:
            return None
        try:
            account = self._cached("account", lambda: self.broker().get_account())
        except Exception:
            return None
        return {"equity": account.equity, "buying_power": account.buying_power}

    def status(self, after: int = 0) -> dict:
        now = self._now().astimezone(ET)
        cfg = self.cfg
        day = now.date().isoformat()
        proposals = self.store.proposals(120)
        trades = self.store.closed_trades()
        pnl = round(sum(float(row["pnl"]) for row in trades), 2)
        wins = [row for row in trades if float(row["pnl"]) > 0]
        record = self.skeptic_record()
        start = self.store.get_json("desk_start")
        week = None
        if start:
            week = (now.date() - datetime.fromisoformat(start["date"]).date()).days // 7 + 1
        alerts: list[dict] = []
        if not self.keys_present:
            alerts.append({"level": "warning", "text": "Mangler Alpaca-papirnøgler. Sæt DESK_ALPACA_API_KEY og DESK_ALPACA_SECRET_KEY i Railway."})
        problem = key_problem(self.base.alpaca_api_key) if not self._injected_broker else ""
        if problem:
            alerts.append({"level": "error", "text": problem})
        if not self.brain.configured:
            alerts.append({"level": "info", "text": "Ingen Claude-nøgle. NEWS og SKEPTIC kører på faste regler, og SKEPTIC er strengere end normalt."})
        elif self.brain.budget_left() < 0.15:
            alerts.append({"level": "info", "text": "Dagens AI-budget er brugt. Reglerne tager over til i morgen."})
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
            "paused": self.paused,
            "scanning": self.scanning,
            "market": self._market(),
            "account": self._account(),
            "keys_present": self.keys_present,
            "own_account": self.base.own_keys,
            "ai": {
                "configured": self.brain.configured,
                "model": self.base.ai_model,
                "budget_usd": self.base.ai_daily_budget_usd,
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
                "daily_loss_pct": cfg.daily_loss_pct,
                "max_chase_pct": cfg.max_chase_pct,
                "log_time_et": cfg.log_time_et,
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
                "flags": len(self.store.flagged_today(day)),
                "max_flags": cfg.max_flags_per_day,
                "open": self._open_count(),
                "max_open": cfg.max_open,
            },
            "kpis": {
                "trades": len(trades),
                "pnl": pnl,
                "win_rate": round(len(wins) / len(trades), 3) if trades else None,
            },
            "skeptic": record,
            "paper_month": {"start": start, "week": week},
            "alerts": alerts,
            "engine": {"last_tick": self.last_tick, "last_error": self.last_error},
        }


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
