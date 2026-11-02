"""Handelsmotoren. Samme sti for papir, live og simulering."""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from trader.broker import AlpacaBroker, BrokerError, NewsItem, SimBroker, nth_weekday
from trader.config import (
    ET_NAME,
    LIVE_CONFIRMATION_PHRASE,
    SIM_START_EQUITY,
    UNIVERSE,
    Settings,
    public_settings,
)
from trader.db import Database
from trader.notify import send as notify
from trader.research import analyze
from trader.risk import RiskContext, daily_loss_hit, evaluate, next_error_count
from trader.signals import active_window, find_candidates, market_bias, vwap
from trader.stats import summarize, unlock_checklist

logger = logging.getLogger("trader.engine")
ET = ZoneInfo(ET_NAME)

MODE_LABELS = {
    "paper": "PAPIRPENGE",
    "live": "RIGTIGE PENGE",
    "sim": "SIMULERING",
}


def _pnl(side: str, entry: float, exit_price: float, qty: int) -> float:
    if side == "long":
        return (exit_price - entry) * qty
    return (entry - exit_price) * qty


def _iso(moment: datetime) -> str:
    return moment.astimezone(ET).isoformat(timespec="seconds")


class TradingEngine:
    def __init__(self, settings: Settings | None = None, db: Database | None = None) -> None:
        self.settings = settings or Settings.from_env()
        self.db = db or Database(self.settings.db_path)
        self.kill_switch = self.db.get_state("kill_switch", "0") == "1"
        self.mode = "paper"
        self.consecutive_errors = 0
        self.last_error = ""
        self.broker_error = ""
        self.last_tick: str | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._simulating = False
        self._last_minute = ""
        self._alpaca: AlpacaBroker | None = None
        self._alpaca_live: bool | None = None
        self._restore_mode()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="daytrader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._alpaca is not None:
            self._alpaca.close()
            self._alpaca = None

    def _restore_mode(self) -> None:
        stored = self.db.get_state("mode", "paper") or "paper"
        if stored == "live" and self._live_allowed():
            self.mode = "live"
            return
        if stored == "live":
            self.db.set_state("mode", "paper")
            self.db.add_event(
                "warning",
                "Tilbage på papirpenge: live-kriterierne er ikke længere opfyldt.",
            )
        self.mode = "paper"

    def _live_allowed(self) -> bool:
        check = self._unlock()
        return bool(check["all_passed"])

    def _unlock(self) -> dict:
        return unlock_checklist(
            trades=self.db.closed_trades("paper", False),
            equity_rows=self.db.list_equity("paper", False),
            allow_live=self.settings.allow_live,
            live_keys_present=self.settings.live_keys_present,
            phrase_accepted=self.db.get_state("phrase_accepted", "0") == "1",
        )

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick_live()
            except Exception:
                logger.exception("Baggrundsløkken fangede en fejl")
            self._stop.wait(self._seconds_to_next())

    def _seconds_to_next(self) -> float:
        now = datetime.now(ET)
        if now.second < 2:
            nxt = now.replace(second=2, microsecond=0)
        else:
            nxt = (now + timedelta(minutes=1)).replace(second=2, microsecond=0)
        return max(0.4, (nxt - datetime.now(ET)).total_seconds())

    def tick_live(self) -> None:
        if self._simulating:
            return
        with self._lock:
            if self._simulating:
                return
            now = datetime.now(ET)
            minute = now.strftime("%Y-%m-%d %H:%M")
            if minute == self._last_minute:
                return
            self._last_minute = minute
            if self.mode == "live":
                if not self.settings.live_keys_present:
                    self.broker_error = "Mangler live-nøgler."
                    return
            elif not self.settings.paper_keys_present:
                self.broker_error = "Mangler Alpaca-nøgler til papirhandel."
                return
            broker: AlpacaBroker | None = None
            try:
                broker = self._alpaca_broker()
                self._tick(
                    now,
                    broker,
                    mode=self.mode,
                    synthetic=False,
                    rules_only=not self.settings.ai_configured,
                )
                self.consecutive_errors = 0
                self.last_error = ""
                self.broker_error = ""
            except Exception as exc:
                self._handle_error(exc, broker)

    def _handle_error(self, exc: Exception, broker) -> None:
        count, flatten = next_error_count(self.consecutive_errors)
        self.last_error = str(exc)
        self.broker_error = str(exc)
        self.db.add_event("error", f"Motorfejl ({count}): {exc}")
        notify(self.settings, f"AI Daytrader fejl ({count}): {exc}")
        logger.warning("Motorfejl %s: %s", count, exc)
        if flatten:
            self.consecutive_errors = 0
            self.db.add_event("error", "Tre fejl i træk. Alle positioner lukkes.")
            notify(self.settings, "AI Daytrader: tre fejl i træk. Alle positioner lukkes.")
            if broker is not None:
                try:
                    broker.close_all_positions()
                except Exception as close_exc:
                    self.db.add_event("error", f"Kunne ikke flade ud: {close_exc}")
        else:
            self.consecutive_errors = count

    def _alpaca_broker(self) -> AlpacaBroker:
        live = self.mode == "live"
        if self._alpaca is None or self._alpaca_live != live:
            if self._alpaca is not None:
                self._alpaca.close()
            self._alpaca = AlpacaBroker(self.settings, live=live)
            self._alpaca_live = live
        return self._alpaca

    def _keys_for_mode(self) -> bool:
        if self.mode == "live":
            return self.settings.live_keys_present
        return self.settings.paper_keys_present

    def _tick(self, now: datetime, broker, *, mode: str, synthetic: bool, rules_only: bool) -> None:
        now = now.astimezone(ET)
        clock = broker.get_clock()
        self.last_tick = _iso(now)
        if not clock.is_open:
            return
        day = now.date().isoformat()
        self._ensure_day(day, broker, mode, synthetic)
        self._sync(broker, mode, synthetic, now)

        account = broker.get_account()
        row = self.db.get_day(day, mode, synthetic)
        start_equity = float(row["start_equity"]) if row else account.equity
        halt_key = f"halt:{mode}:{day}"
        flat_key = f"flat:{mode}:{day}"
        halted = self.db.get_state(halt_key, "0") == "1"

        if daily_loss_hit(start_equity, account.equity) and not halted:
            self._flatten(
                broker,
                mode,
                synthetic,
                now,
                "Dagligt tab på 2 % er nået. Alle positioner lukkes, og dagen standses.",
            )
            self.db.set_state(halt_key, "1")
            self.db.set_state(flat_key, "1")
            notify(self.settings, "AI Daytrader: dagligt tab på 2 % er nået. Handel standset.")
            halted = True

        if (now.hour, now.minute) >= (15, 50):
            if self.db.get_state(flat_key, "0") != "1":
                self._flatten(
                    broker,
                    mode,
                    synthetic,
                    now,
                    "Klokken er 15:50. Alle positioner lukkes. Intet holdes overnight.",
                )
                self.db.set_state(flat_key, "1")
            self.db.set_day_end(day, mode, synthetic, broker.get_account().equity)
            return

        if self.kill_switch or halted:
            return

        window = active_window(now)
        if window is None:
            return
        self._scan(
            now,
            broker,
            mode=mode,
            synthetic=synthetic,
            rules_only=rules_only,
            window=window,
            day=day,
            start_equity=start_equity,
            halted=halted,
        )

    def _ensure_day(self, day: str, broker, mode: str, synthetic: bool) -> None:
        if self.db.get_day(day, mode, synthetic) is not None:
            return
        equity = broker.get_account().equity
        self.db.insert_day_start(day, mode, synthetic, equity)
        label = "simulering" if synthetic else mode
        self.db.add_event("info", f"Ny dag {day} ({label}). Startkapital {equity:,.2f} USD.")

    def _flatten(self, broker, mode: str, synthetic: bool, now: datetime, message: str) -> None:
        broker.close_all_positions()
        self._sync(broker, mode, synthetic, now)
        self.db.add_event("warning", message)

    def _sync(self, broker, mode: str, synthetic: bool, now: datetime) -> None:
        for trade in self.db.open_trades(mode, synthetic):
            order_id = trade.get("order_id")
            if not order_id:
                continue
            try:
                order = broker.get_order(order_id)
            except BrokerError as exc:
                logger.info("Kunne ikke læse ordre %s: %s", order_id, exc)
                continue
            if order.exit_price is None or trade.get("entry_price") is None:
                continue
            entry = float(trade["entry_price"])
            result = _pnl(trade["side"], entry, float(order.exit_price), int(trade["qty"]))
            reason = order.exit_reason or "close"
            self.db.close_trade(int(trade["id"]), float(order.exit_price), result, reason, _iso(now))
            self.db.add_event(
                "trade",
                f"Lukket {trade['symbol']} ({reason}) til {order.exit_price:.2f}. Resultat {result:+.2f} USD.",
            )
            notify(
                self.settings,
                f"Lukket {trade['symbol']} ({reason}) {result:+.2f} USD.",
            )

    def _spy(self, intraday: dict, now: datetime) -> dict:
        bars = [bar for bar in intraday.get("SPY", []) if bar.ts + timedelta(minutes=1) <= now]
        if not bars:
            return {"symbol": "SPY", "bias": 0}
        price = bars[-1].close
        day_open = bars[0].open
        value = vwap(bars)
        bias = market_bias(price, day_open, value) if value else 0
        return {
            "symbol": "SPY",
            "price": price,
            "open": day_open,
            "vwap": value,
            "bias": bias,
            "change_from_open_pct": (price - day_open) / day_open if day_open else 0.0,
        }

    def _scan(
        self,
        now: datetime,
        broker,
        *,
        mode: str,
        synthetic: bool,
        rules_only: bool,
        window: str,
        day: str,
        start_equity: float,
        halted: bool,
    ) -> None:
        symbols = list(UNIVERSE)
        intraday = broker.get_intraday(symbols, now)
        daily = broker.get_daily(symbols, 21, now)
        news = _news_payload(broker.get_news(symbols, now))
        candidates = find_candidates(intraday, daily, window, now, UNIVERSE)
        fresh = [
            item
            for item in candidates
            if not self.db.is_researched(day, window, item.symbol, synthetic)
        ]
        if not fresh:
            return
        spy = self._spy(intraday, now)
        batch = analyze(
            self.settings,
            now,
            window,
            fresh,
            spy,
            news,
            rules_only_mode=rules_only,
        )
        self.db.add_event(
            "info",
            f"Research ({'kun regler' if batch.source == 'rules' else 'Claude'}) "
            f"{'åbning' if window == 'open' else 'lukning'} {day}: {batch.regime}.",
            batch.market_summary,
        )
        account = broker.get_account()
        for candidate in fresh:
            self.db.mark_researched(day, window, candidate.symbol, synthetic)
            verdict = batch.for_symbol(candidate.symbol)
            decision_id = self.db.insert_decision(
                {
                    "trading_day": day,
                    "window": window,
                    "mode": mode,
                    "synthetic": synthetic,
                    "symbol": candidate.symbol,
                    "decision": verdict.decision,
                    "confidence": verdict.confidence,
                    "reasoning": verdict.reasoning,
                    "tighter_stop": verdict.tighter_stop,
                    "regime": batch.regime,
                    "market_summary": batch.market_summary,
                    "source": batch.source,
                    "outcome": "pending",
                }
            )
            if verdict.decision != "take":
                self.db.update_decision(decision_id, "skipped", None)
                continue
            open_rows = self.db.open_trades(mode, synthetic, day)
            context = RiskContext(
                equity=account.equity,
                buying_power=account.buying_power,
                entry=candidate.entry,
                stop=candidate.stop,
                target=candidate.target,
                side=candidate.side,
                symbol=candidate.symbol,
                confidence=verdict.confidence,
                open_positions=len(open_rows),
                open_symbols={row["symbol"] for row in open_rows},
                trades_today=self.db.count_trades_today(day, mode, synthetic),
                day_start_equity=start_equity,
                kill_switch=self.kill_switch,
                halted=halted,
                mode=mode,
                daytrade_count=account.daytrade_count,
                live_max_capital=self.settings.live_max_capital,
                tighter_stop=verdict.tighter_stop,
            )
            decision = evaluate(context)
            if not decision.allowed:
                self.db.update_decision(decision_id, "blocked", decision.reason)
                self.db.add_event("info", f"{candidate.symbol} blokeret: {decision.reason}")
                continue
            try:
                order = broker.submit_bracket(
                    candidate.symbol,
                    candidate.side,
                    decision.qty,
                    decision.stop,
                    decision.target,
                )
            except Exception as exc:
                self.db.update_decision(decision_id, "blocked", f"Ordren blev afvist: {exc}")
                raise
            entry = order.filled_avg_price if order.filled_avg_price is not None else candidate.entry
            self.db.insert_trade(
                {
                    "trading_day": day,
                    "mode": mode,
                    "synthetic": synthetic,
                    "symbol": candidate.symbol,
                    "side": candidate.side,
                    "qty": decision.qty,
                    "entry_price": entry,
                    "stop_price": decision.stop,
                    "target_price": decision.target,
                    "status": "open",
                    "window": window,
                    "setup": candidate.setup,
                    "confidence": verdict.confidence,
                    "reasoning": verdict.reasoning,
                    "order_id": order.id,
                    "opened_at": _iso(now),
                    "ai_decision_id": decision_id,
                }
            )
            self.db.update_decision(decision_id, "taken", None)
            side_da = "Lang" if candidate.side == "long" else "Kort"
            self.db.add_event(
                "trade",
                f"{side_da} {decision.qty} {candidate.symbol} til {entry:.2f}. "
                f"Stop {decision.stop:.2f}, mål {decision.target:.2f}.",
            )
            notify(
                self.settings,
                f"{side_da} {decision.qty} {candidate.symbol} @ {entry:.2f} "
                f"(stop {decision.stop:.2f}, mål {decision.target:.2f}).",
            )
            account = broker.get_account()

    def simulate_one_day(self) -> dict:
        with self._lock:
            if self._simulating:
                raise RuntimeError("En simulering kører allerede.")
            self._simulating = True
            try:
                index = int(self.db.get_state("sim_day_index", "0") or "0") + 1
                self.db.set_state("sim_day_index", str(index))
                day = nth_weekday(date(2026, 6, 1), index)
                equity = self.db.last_end_equity("sim", True, SIM_START_EQUITY)
                broker = SimBroker(day, index, equity)
                self.db.add_event(
                    "info",
                    f"Simulering starter {day.isoformat()}. {broker.note}. "
                    "Syntetiske resultater siger intet om rigtig performance.",
                )
                moment = datetime.combine(day, time(9, 30), tzinfo=ET)
                end = datetime.combine(day, time(16, 0), tzinfo=ET)
                while moment <= end:
                    broker.set_time(moment)
                    self._tick(moment, broker, mode="sim", synthetic=True, rules_only=True)
                    moment += timedelta(minutes=1)
                account = broker.get_account()
                row = self.db.get_day(day.isoformat(), "sim", True)
                pnl = float(row["pnl"]) if row and row.get("pnl") is not None else 0.0
                trades = [
                    trade
                    for trade in self.db.list_trades("sim", True, limit=50)
                    if trade["trading_day"] == day.isoformat()
                ]
                result = {
                    "trading_day": day.isoformat(),
                    "note": broker.note,
                    "trades": len(trades),
                    "pnl": pnl,
                    "equity": account.equity,
                    "kind": broker.kind,
                }
                self.db.set_state("last_simulation", json.dumps(result))
                self.consecutive_errors = 0
                return result
            finally:
                self._simulating = False

    def kill(self) -> None:
        with self._lock:
            self.kill_switch = True
            self.db.set_state("kill_switch", "1")
            self.db.add_event("warning", "Nødstop aktiveret. Alle positioner lukkes, og nye handler blokeres.")
            notify(self.settings, "AI Daytrader: nødstop aktiveret.")
            if self._keys_for_mode():
                try:
                    self._alpaca_broker().close_all_positions()
                except Exception as exc:
                    self.broker_error = str(exc)
                    self.db.add_event("error", f"Nødstop kunne ikke lukke positioner: {exc}")

    def resume(self) -> None:
        with self._lock:
            self.kill_switch = False
            self.db.set_state("kill_switch", "0")
            self.db.add_event("info", "Nødstop ophævet. Dagens tabsgrænse gælder stadig, hvis den er ramt.")

    def request_live(self, phrase: str) -> tuple[bool, str]:
        with self._lock:
            if phrase.strip() != LIVE_CONFIRMATION_PHRASE:
                return False, "Sætningen matcher ikke præcist."
            current = self._unlock()
            if not current["ready_for_phrase"]:
                missing = [item["label"] for item in current["items"] if item["id"] != "phrase" and not item["ok"]]
                return False, "Ikke alle kriterier er opfyldt: " + ", ".join(missing)
            self.db.set_state("phrase_accepted", "1")
            self.mode = "live"
            self.db.set_state("mode", "live")
            if self._alpaca is not None:
                self._alpaca.close()
                self._alpaca = None
            self.db.add_event(
                "warning",
                "Live-tilstand er slået til. Størrelsen begrænses af live-loftet.",
            )
            return True, "Rigtige penge er slået til. Størrelsen bruger højst live-loftet."

    def return_to_paper(self) -> None:
        with self._lock:
            if self.mode == "live":
                if not self.settings.live_keys_present:
                    raise BrokerError("Mangler live-nøgler, så åbne live-positioner kan ikke lukkes.")
                self._alpaca_broker().close_all_positions()
            self.mode = "paper"
            self.db.set_state("mode", "paper")
            self.db.set_state("phrase_accepted", "0")
            if self._alpaca is not None:
                self._alpaca.close()
                self._alpaca = None
            self.db.add_event("info", "Tilbage på papirpenge. Live-positioner er lukket.")

    def status(self) -> dict:
        unlock = self._unlock()
        books = {
            "paper": _book(self.db, "paper", False),
            "live": _book(self.db, "live", False),
            "sim": _book(self.db, "sim", True),
        }
        paper_trades = books["paper"]["kpis"]["trades"]
        sim_days = books["sim"]["kpis"]["trading_days"]
        if self.mode == "live":
            default_book = "live"
        elif sim_days and not paper_trades:
            default_book = "sim"
        else:
            default_book = "paper"
        alerts = self._alerts(unlock)
        positions, position_note = self._positions()
        market = self._market()
        last_raw = self.db.get_state("last_simulation")
        try:
            last_simulation = json.loads(last_raw) if last_raw else None
        except json.JSONDecodeError:
            last_simulation = None
        halted_today = False
        halt_reason = ""
        today = datetime.now(ET).date().isoformat()
        if self.db.get_state(f"halt:{self.mode}:{today}", "0") == "1":
            halted_today = True
            halt_reason = "Dagligt tab på 2 % er nået."
        return {
            "server_time_et": _iso(datetime.now(ET)),
            "mode": self.mode,
            "mode_label": MODE_LABELS.get(self.mode, self.mode),
            "default_book": default_book,
            "market": market,
            "kill_switch": self.kill_switch,
            "halted": halted_today,
            "halt_reason": halt_reason,
            "alerts": alerts,
            "books": books,
            "positions": positions,
            "position_note": position_note,
            "unlock": unlock,
            "events": self.db.recent_events(180),
            "settings": public_settings(self.settings),
            "disclaimer": "Syntetiske resultater siger intet om rigtig performance.",
            "last_simulation": last_simulation,
            "engine": {
                "consecutive_errors": self.consecutive_errors,
                "last_error": self.last_error,
                "last_tick": self.last_tick,
                "simulating": self._simulating,
            },
        }

    def _alerts(self, unlock: dict) -> list[dict]:
        alerts: list[dict] = []
        if self.mode == "live" and not self.settings.live_keys_present:
            alerts.append({"level": "warning", "text": "Mangler live-nøgler i .env."})
        elif self.mode != "live" and not self.settings.paper_keys_present:
            alerts.append(
                {
                    "level": "warning",
                    "text": "Mangler Alpaca-nøgler til papirhandel (ALPACA_API_KEY og ALPACA_SECRET_KEY).",
                }
            )
        if self.broker_error and self._keys_for_mode():
            alerts.append({"level": "error", "text": f"Broker-fejl: {self.broker_error}"})
        if not self.settings.ai_configured:
            alerts.append(
                {
                    "level": "info",
                    "text": "Ingen Anthropic-nøgle. Analysen kører kun på regler, ikke Claude.",
                }
            )
        if self.kill_switch:
            alerts.append({"level": "warning", "text": "Nødstop er aktivt. Nye handler er blokeret."})
        today = datetime.now(ET).date().isoformat()
        if self.db.get_state(f"halt:{self.mode}:{today}", "0") == "1":
            alerts.append({"level": "warning", "text": "Handel er standset for resten af dagen efter tab på 2 %."})
        if not unlock["all_passed"]:
            alerts.append(
                {
                    "level": "info",
                    "text": "Rigtige penge er låst, indtil alle punkter på tjeklisten er opfyldt.",
                }
            )
        return alerts

    def _positions(self) -> tuple[list[dict], str]:
        if not self._keys_for_mode():
            return [], "Kontoen kan ikke hentes uden nøgler. Simulatoren efterlader ingen positioner efter kl. 15:50."
        try:
            broker = self._alpaca_broker()
            rows = []
            for position in broker.get_positions():
                rows.append(
                    {
                        "symbol": position.symbol,
                        "side": position.side,
                        "qty": position.qty,
                        "avg_entry": position.avg_entry,
                        "market_price": position.market_price,
                        "unrealized_pl": position.unrealized_pl,
                    }
                )
            return rows, ""
        except Exception as exc:
            self.broker_error = str(exc)
            return [], f"Kunne ikke hente positioner: {exc}"

    def _market(self) -> dict:
        if not self._keys_for_mode():
            return {"is_open": None, "label": "Ukendt uden nøgler"}
        try:
            clock = self._alpaca_broker().get_clock()
            return {
                "is_open": clock.is_open,
                "label": "Åbent" if clock.is_open else "Lukket",
                "timestamp": _iso(clock.timestamp),
                "next_open": _iso(clock.next_open) if clock.next_open else None,
                "next_close": _iso(clock.next_close) if clock.next_close else None,
            }
        except Exception as exc:
            return {"is_open": None, "label": "Ukendt", "error": str(exc)}


def _news_payload(items: list[NewsItem]) -> list[dict]:
    return [
        {
            "headline": item.headline,
            "symbols": item.symbols,
            "created_at": item.created_at.isoformat(timespec="minutes"),
            "summary": item.summary,
        }
        for item in items
    ]


def _book(db: Database, mode: str, synthetic: bool) -> dict:
    trades = db.list_trades(mode, synthetic)
    equity_rows = db.list_equity(mode, synthetic)
    decisions = db.list_decisions(mode, synthetic)
    summary = summarize(trades, equity_rows)
    curve = summary.pop("equity_curve")
    return {
        "kpis": summary,
        "equity_curve": curve,
        "trades": [_trade(row) for row in trades],
        "decisions": [_decision(row) for row in decisions],
    }


def _trade(row: dict) -> dict:
    return {
        "id": row["id"],
        "trading_day": row["trading_day"],
        "symbol": row["symbol"],
        "side": row["side"],
        "qty": row["qty"],
        "entry_price": row["entry_price"],
        "stop_price": row["stop_price"],
        "target_price": row["target_price"],
        "exit_price": row["exit_price"],
        "pnl": row["pnl"],
        "status": row["status"],
        "window": row["window"],
        "setup": row["setup"],
        "confidence": row["confidence"],
        "reasoning": row["reasoning"],
        "exit_reason": row["exit_reason"],
        "opened_at": row["opened_at"],
        "closed_at": row["closed_at"],
        "synthetic": bool(row["synthetic"]),
    }


def _decision(row: dict) -> dict:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "trading_day": row["trading_day"],
        "window": row["window"],
        "symbol": row["symbol"],
        "decision": row["decision"],
        "confidence": row["confidence"],
        "reasoning": row["reasoning"],
        "tighter_stop": row["tighter_stop"],
        "regime": row["regime"],
        "market_summary": row["market_summary"],
        "source": row["source"],
        "block_reason": row["block_reason"],
        "outcome": row["outcome"],
        "synthetic": bool(row["synthetic"]),
    }
