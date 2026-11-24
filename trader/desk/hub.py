"""Forbindelsen mellem strategi-systemet og bordets motor.

- on_scan: ved hver scanning køres alle slåede-til strategier på de samme rigtige kurser, bordet allerede har hentet.
  Signalerne vises på siden, og papirturneringen handler dem på virtuelle konti. Der sendes ingen ordrer herfra.
- Først når en strategi er godkendt (se backtest.approval) og du selv har slået "Send godkendte signaler til bordet" til,
  kan et signal blive en markering i bordets almindelige flow (NEWS, CHART, SKEPTIC, SIZER, EXECUTOR), som kun handler papir.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from trader.desk import backtest as bt
from trader.desk import strategies as st
from trader.desk.alpaca import ET, Bar

logger = logging.getLogger(__name__)

MAX_SIGNALS = 60
SAVE_EVERY_SECONDS = 600
BACKTEST_MAX_DAYS = 20
BACKTEST_MAX_SYMBOLS = 20

NOT_RUN = "Ikke kørt endnu. Tryk Kør backtest, så hentes historiske minutkurser fra Alpaca."


class StrategyHub:
    def __init__(self, store: Any, market: Any, now_fn: Any) -> None:
        self.store = store
        self.market = market
        self._now = now_fn
        saved = store.get_json("strategy_enabled", None)
        self.enabled: set[str] = set(saved) if isinstance(saved, list) else set(st.LIBRARY)
        self.enabled &= set(st.LIBRARY)
        self.tournament = bt.Tournament(store.get_json("tournament", None))
        self.backtests: dict = store.get_json("strategy_backtests", None) or {}
        self.pipeline_on = store.get_state("strategy_pipeline", "0") == "1"
        self.signals: list[dict] = []
        self.regime: dict = {"tags": [], "label": "Ukendt", "detail": "Ingen scanning endnu.", "note": st.REGIME_NOTE}
        self.last_step: str | None = None
        self._dirty = False
        self._saved_at = 0.0
        self.job: dict[str, Any] = {"running": False, "message": "", "started": None}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ indstillinger
    def set_enabled(self, strategy_id: str, on: bool) -> None:
        if strategy_id not in st.LIBRARY:
            raise ValueError("Ukendt strategi.")
        self.enabled.add(strategy_id) if on else self.enabled.discard(strategy_id)
        self.store.set_json("strategy_enabled", sorted(self.enabled))

    def set_pipeline(self, on: bool) -> None:
        self.pipeline_on = bool(on)
        self.store.set_state("strategy_pipeline", "1" if on else "0")

    # ------------------------------------------------------------------ scanning
    def on_scan(self, intraday: dict[str, list[Bar]], daily: dict[str, list[Bar]], now: datetime, *, benchmark: str, flatten: bool, entries: bool, session_minutes: int) -> None:
        """Fejl her må aldrig stoppe bordets scanning. Kalderen fanger dem, men vi er også forsigtige."""
        day = now.date().isoformat()
        symbols = {s: [b for b in bars if b.ts + timedelta(minutes=1) <= now] for s, bars in intraday.items() if s != benchmark}
        bench = [b for b in intraday.get(benchmark, []) if b.ts + timedelta(minutes=1) <= now]
        self.regime = st.stock_regime(bench, daily.get(benchmark), session_minutes)
        tags = self.regime["tags"]
        rows: list[dict] = []
        for symbol, bars in symbols.items():
            if len(bars) < st.MIN_BARS:
                continue
            d = st.DayData(bars, daily.get(symbol, []), session_minutes)
            found = st.scan_day(symbol, d, len(bars) - 1, self.enabled)
            if not found:
                continue
            conf = st.independent_confirmations(found)
            for sig in found:
                fit = st.fits_regime(sig.strategy, tags)
                row = sig.to_dict()
                row.update({
                    "name": st.LIBRARY[sig.strategy].name, "origin": st.LIBRARY[sig.strategy].origin, "executable": st.LIBRARY[sig.strategy].executable,
                    "fits": fit["fits"], "fit_why": fit["why"], "independent": conf["count"] if conf.get("side") == sig.side else 1,
                    "also": [s.strategy for s in found if s is not sig and s.side == sig.side],
                })
                rows.append(row)
        rows.sort(key=lambda r: (r["independent"], r["quality"]), reverse=True)
        self.signals = rows[:MAX_SIGNALS]
        with self._lock:
            result = self.tournament.step({s: b for s, b in symbols.items()}, daily, day=day, enabled=self.enabled, regime=tags, flatten=flatten, entries=entries, session_minutes=session_minutes)
        self.last_step = now.isoformat(timespec="seconds")
        if result["opened"] or result["closed"]:
            self._dirty = True
        if self._dirty and time.time() - self._saved_at > 30 or time.time() - self._saved_at > SAVE_EVERY_SECONDS:
            self.save()

    def flatten(self, broker: Any, now: datetime, *, session_minutes: int) -> None:
        """Ved lukketid: afslut de virtuelle positioner til de seneste rigtige kurser. Ét kald kun for de symboler, der har en åben position."""
        open_symbols = sorted({p["symbol"] for acct in self.tournament.state["accounts"].values() for p in acct["open"]})
        if not open_symbols:
            return
        intraday = broker.get_intraday(open_symbols, now)
        with self._lock:
            self.tournament.step(intraday, {}, day=now.date().isoformat(), enabled=set(), regime=self.regime.get("tags", []), flatten=True, entries=False, session_minutes=session_minutes)
        self.save()

    def save(self) -> None:
        with self._lock:
            self.store.set_json("tournament", self.tournament.state)
        self._saved_at, self._dirty = time.time(), False

    # ------------------------------------------------------------------ godkendte signaler til bordets flow
    def pipeline_candidates(self) -> list[dict]:
        """Signaler, der må blive til en markering: kun long, kun godkendte strategier, kun med mindst to uafhængige bekræftelser."""
        if not self.pipeline_on:
            return []
        board = {row["id"]: row for row in self.tournament.leaderboard(self._bt_results())}
        out = []
        for row in self.signals:
            s = st.LIBRARY[row["strategy"]]
            if not (s.executable and s.side == "long" and row["independent"] >= 2 and row["fits"] is not False):
                continue
            if not (board.get(row["strategy"], {}).get("approval") or {}).get("approved"):
                continue
            out.append(row)
        return out

    # ------------------------------------------------------------------ backtest
    def _bt_results(self) -> dict:
        return (self.backtests.get("results") or {}) if self.backtests else {}

    def start_backtest(self, broker: Any, symbols: list[str], days: int, benchmark: str, session_minutes: int) -> None:
        if self.job["running"]:
            raise ValueError("Der kører allerede en backtest.")
        if not hasattr(broker, "_bars"):
            raise ValueError("Backtest bruger Alpacas historiske minutkurser og findes kun for USA. København har ingen historiske minutkurser her.")
        days = max(3, min(BACKTEST_MAX_DAYS, int(days)))
        symbols = [s for s in dict.fromkeys(symbols) if s != benchmark][:BACKTEST_MAX_SYMBOLS]
        if not symbols:
            raise ValueError("Watchlisten er tom.")
        self.job = {"running": True, "message": "Henter historiske kurser …", "started": self._now().isoformat(timespec="seconds")}
        threading.Thread(target=self._run_backtest, args=(broker, symbols, days, benchmark, session_minutes), daemon=True, name="desk-backtest").start()

    def _run_backtest(self, broker: Any, symbols: list[str], days: int, benchmark: str, session_minutes: int) -> None:
        try:
            result = self.backtest_history(broker, symbols, days, benchmark, session_minutes)
            self.backtests = result
            self.store.set_json("strategy_backtests", result)
            self.job = {"running": False, "message": f"Færdig: {result['days']} symbol-dage.", "started": self.job.get("started")}
        except Exception as exc:  # fejlen vises på siden, i stedet for at tråden dør lydløst
            logger.warning("Backtest fejlede: %s", exc)
            self.job = {"running": False, "message": f"Backtest fejlede: {exc}", "started": self.job.get("started")}

    def backtest_history(self, broker: Any, symbols: list[str], days: int, benchmark: str, session_minutes: int) -> dict:
        end = self._now().astimezone(ET)
        start = end - timedelta(days=int(days * 1.7) + 4)
        minutes = broker._bars(list(dict.fromkeys([*symbols, benchmark])), "1Min", start, end)
        dailies = broker._bars(list(dict.fromkeys([*symbols, benchmark])), "1Day", start - timedelta(days=45), end)
        by_day: dict[str, dict[str, list[Bar]]] = {}
        for symbol, bars in minutes.items():
            for bar in bars:
                local = bar.ts.astimezone(ET)
                if (9, 30) <= (local.hour, local.minute) < (16, 0):
                    by_day.setdefault(local.date().isoformat(), {}).setdefault(symbol, []).append(Bar(local, bar.open, bar.high, bar.low, bar.close, bar.volume))
        dates = sorted(day for day, items in by_day.items() if day < end.date().isoformat() and len(items.get(benchmark, [])) >= 300)[-days:]
        items: list[bt.SymbolDay] = []
        for day in dates:
            bench_bars = by_day[day].get(benchmark, [])
            tags = st.stock_regime(bench_bars, None, session_minutes)["tags"]  # beskrivende: hele dagens indeks, ikke det signalet så
            for symbol in symbols:
                bars = by_day[day].get(symbol, [])
                if len(bars) < 300:
                    continue
                prior = [b for b in dailies.get(symbol, []) if b.ts.astimezone(ET).date().isoformat() < day][-25:]
                items.append(bt.SymbolDay(symbol, day, bars, prior, tags))
        if not items:
            raise ValueError("Ingen hele handelsdage fundet. Tjek Alpaca-nøglerne og watchlisten.")
        run = bt.run_backtest(items, None, bt.Costs(), session_minutes=session_minutes)
        run.update({"ran_at": self._now().isoformat(timespec="seconds"), "symbols": symbols, "data": "Alpaca, 1-minutskurser fra IEX-feedet (kun en del af al handel, så volumen er lavere end på hele børsen)",
                    "regime_note": "Markedsforhold er sat ud fra hele dagens indeks. Det er en beskrivelse efter dagen, ikke noget strategien kunne vide undervejs."})
        return run

    # ------------------------------------------------------------------ til siden
    def view(self, today: str, last_scan: str | None, executable_only: bool = False) -> dict[str, Any]:
        board = {row["id"]: row for row in self.tournament.leaderboard(self._bt_results())}
        tags = self.regime.get("tags", [])
        results = self._bt_results()
        library = []
        for sid, s in st.LIBRARY.items():
            row = board.get(sid)
            bt_entry = results.get(sid)
            fit = st.fits_regime(sid, tags)
            library.append({
                **s.to_dict(), "enabled": sid in self.enabled, "fits": fit["fits"], "fit_why": fit["why"],
                "paper": {k: row[k] for k in ("trades", "win_rate", "profit_factor", "expectancy_r", "max_drawdown_r", "pnl", "return_pct", "drawdown_pct", "avg_win_r", "avg_loss_r", "reliable")} if row else None,
                "paper_by_regime": bt.by_regime(self.tournament.state["accounts"].get(sid, {}).get("trades", [])) if row else {},
                "score": (row or {}).get("score") or bt.strategy_score({"trades": 0}), "approval": (row or {}).get("approval") or bt.approval({}, bt_entry),
                "backtest": ({"metrics": bt_entry["metrics"], "in_sample": bt_entry["in_sample"], "out_sample": bt_entry["out_sample"], "walk_forward": bt_entry["walk_forward"], "by_regime": bt_entry["by_regime"]} if bt_entry else None),
            })
        fitting = [s for s in library if s["enabled"] and s["fits"] is True]
        wait = None
        if tags and self.enabled and not fitting:
            wait = "Ingen af de slåede-til strategier passer til markedet lige nu. Så venter botten."
        return {
            "library": library, "signals": self.signals, "regime": self.regime, "leaderboard": list(board.values()),
            "best": {p: self.tournament.best(p, today) for p in ("day", "week", "month")}, "wait": wait,
            "pipeline": {"on": self.pipeline_on, "candidates": len(self.pipeline_candidates()),
                         "rule": f"Kun long, kun strategier der er godkendt (mindst {bt.APPROVAL['min_trades']} papirhandler, forventning mindst {bt.APPROVAL['min_expectancy_r']} R, profit factor mindst {bt.APPROVAL['min_profit_factor']}, positiv out-of-sample) og mindst to uafhængige bekræftelser. Bordet handler kun papir."},
            "backtest": {"ran_at": self.backtests.get("ran_at"), "days": self.backtests.get("days"), "dates": (self.backtests.get("dates") or [])[:1] + (self.backtests.get("dates") or [])[-1:], "symbols": self.backtests.get("symbols"),
                         "costs": self.backtests.get("costs"), "data": self.backtests.get("data"), "regime_note": self.backtests.get("regime_note"), "job": self.job, "empty": NOT_RUN},
            "tournament": {"start": self.tournament.state["start"], "risk_pct": self.tournament.risk_pct, "cap_pct": self.tournament.cap_pct, "last_step": self.last_step, "last_scan": last_scan,
                           "costs": self.tournament.costs.to_dict()},
            "notes": [
                "Strategierne er bottens egne, forenklede fortolkninger af offentligt beskrevne metoder. Appen har ingen verificerede resultater for personerne.",
                "Tal på siden er kun dem, appen selv har målt: papirturneringen (virtuelle konti på rigtige live-kurser) og backtesten (historik). De blandes aldrig, og ingen af dem er rigtige penge.",
                "Bordet køber kun (ingen short). Short-strategier måles, men udføres aldrig.",
            ],
        }
