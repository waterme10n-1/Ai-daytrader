"""Strategi-biblioteket, backtest og papirturnering: rigtige regler på syntetiske, men realistiske dage. Intet netværk."""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.desk import backtest as bt
from trader.desk import strategies as S
from trader.desk.alpaca import Bar

ET = ZoneInfo("America/New_York")
T0 = datetime(2026, 11, 10, 9, 30, tzinfo=ET)
DAILY = [Bar(datetime(2026, 11, 1 + k, tzinfo=ET), 19, 21, 18, 20 + 0.1 * k, 390_000) for k in range(25)]  # normal volumen: 1000 pr. minut


def day(seed: int, n: int = 390, drift: float = 0.0, vol: float = 0.0015) -> list[Bar]:
    r = random.Random(seed)
    price, out, mode = 20.0, [], 0
    for i in range(n):
        if i % 60 == 0:
            mode = r.choice([-1, 0, 0, 1, 2])
        ret = r.gauss(drift + mode * 0.0006, vol * (3 if mode == 2 else 1))
        o, c = price, max(1.5, price * (1 + ret))
        h = max(o, c) * (1 + abs(r.gauss(0, vol * 0.6)))
        l = min(o, c) * (1 - abs(r.gauss(0, vol * 0.6)))
        v = 1000 * (1 + abs(r.gauss(0, 1))) * (5 if r.random() < 0.04 else 1) * (3 if mode == 2 else 1)
        out.append(Bar(T0 + timedelta(minutes=i), o, h, l, c, v))
        price = c
    return out


def test_every_strategy_is_documented_without_invented_results():
    assert set(S.LIBRARY) == set(S.DETECTORS) and len(S.LIBRARY) >= 15
    for sid, s in S.LIBRARY.items():
        for text in (s.name, s.origin, s.source, s.description, s.how, s.markets, s.timeframes, s.entry, s.exit, s.stop, s.target, s.rr, s.weakness):
            assert text.strip(), (sid, "mangler tekst")
        assert s.side in {"long", "short"} and s.families
        assert "ikke verificeret" in s.source.lower() or "bottens egen" in s.source.lower(), sid  # aldrig påstået resultat
        assert not any(ch in s.source for ch in ("$", "millioner")), sid
    assert not S.LIBRARY["parabolic_short"].executable and not S.LIBRARY["bear_flag"].executable and S.LIBRARY["orb"].executable
    combo = S.LIBRARY["combo_trend_momentum"]
    assert len(combo.combo) >= 3 and len(set(combo.families)) >= 4  # kombinationen bygger på forskellige slags beviser


def test_gap_and_go_fires_on_a_real_gap_with_volume_and_not_without():
    DAILY = [Bar(datetime(2026, 11, 1 + k, tzinfo=ET), 9.8, 10.2, 9.7, 10.0, 390_000) for k in range(25)]  # i går lukkede aktien på 10,00
    bars = []
    for i in range(12):
        o = 10.6 + 0.01 * i
        high = 10.7 if i < 5 else 10.69
        bars.append(Bar(T0 + timedelta(minutes=i), o, high, o - 0.05, o + 0.02, 4000))
    bars[9] = Bar(T0 + timedelta(minutes=9), 10.68, 10.85, 10.66, 10.82, 4600)
    d = S.DayData(bars, DAILY)
    sig = S.gap_and_go(d, 9)
    assert sig is not None and sig.side == "long" and sig.stop < sig.entry < sig.target
    assert 1.9 <= sig.rr <= 2.1 and "Relativ volumen" in " ".join(sig.reasons)
    quiet = [Bar(b.ts, b.open, b.high, b.low, b.close, 300) for b in bars]
    assert S.gap_and_go(S.DayData(quiet, DAILY), 9) is None  # lav volumen
    no_gap = [Bar(b.ts, b.open - 0.6, b.high - 0.6, b.low - 0.6, b.close - 0.6, b.volume) for b in bars]
    assert S.gap_and_go(S.DayData(no_gap, DAILY), 9) is None


def test_detectors_only_use_data_up_to_the_signal_bar():
    """Look-ahead-test: afkortes dagen ved bar i, skal signalet være det samme."""
    checked = 0
    for seed in range(12):
        bars = day(seed, drift=0.0003)
        full = S.DayData(bars, DAILY)
        for i in range(30, 360, 7):
            cut = S.DayData(bars[: i + 1], DAILY)
            a = {s.strategy: (s.entry, s.stop, s.target) for s in S.scan_day("X", full, i)}
            b = {s.strategy: (s.entry, s.stop, s.target) for s in S.scan_day("X", cut, i)}
            assert a == b, (seed, i)
            checked += len(a)
    assert checked > 0


def test_every_signal_has_a_valid_bracket_and_a_readable_reason():
    seen = set()
    for seed in range(40):
        bars = day(seed, drift=random.Random(seed).choice([-0.0002, 0.0, 0.0003]))
        d = S.DayData(bars, DAILY)
        for i in range(16, len(bars) - 1, 3):
            for sig in S.scan_day("X", d, i):
                seen.add(sig.strategy)
                if sig.side == "long":
                    assert sig.stop < sig.entry < sig.target
                else:
                    assert sig.target < sig.entry < sig.stop
                assert sig.rr >= 1.0 and 0 <= sig.quality <= 100 and sig.reasons and sig.checks
                assert (abs(sig.entry - sig.stop) / sig.entry * 100.0) <= 8.0 + 1e-6
    assert len(seen) >= 8  # mange af strategierne finder noget på tilfældige dage


def test_two_strategies_on_the_same_signal_family_count_once():
    def sig(strategy: str, quality: int) -> S.Signal:
        return S.Signal(strategy=strategy, symbol="X", side="long", i=10, ts="t", price=10, entry=10, stop=9.8, target=10.4, rr=2.0, quality=quality)

    shared = S.independent_confirmations([sig("vwap_bounce", 90), sig("vwap_reclaim", 80)])
    assert shared["count"] == 1 and shared["shared"] == ["vwap_reclaim"]
    independent = S.independent_confirmations([sig("orb", 80), sig("vwap_bounce", 90), sig("ema_pullback", 70)])
    # orb: range+volume, vwap_bounce: vwap+price_action, ema_pullback: ma_trend+price_action (deler price_action med vwap_bounce)
    assert independent["count"] == 2 and "ema_pullback" in independent["shared"]
    assert S.independent_confirmations([])["count"] == 0


def test_market_regime_tags_and_strategy_fit():
    rising = [Bar(T0 + timedelta(minutes=i), 100 + i * 0.02, 100.03 + i * 0.02, 99.99 + i * 0.02, 100.02 + i * 0.02, 5000) for i in range(60)]
    regime = S.stock_regime(rising, DAILY)
    assert "bullish" in regime["tags"] and "strong_momentum" in regime["tags"]
    assert S.stock_regime(rising[:5])["tags"] == []
    assert S.fits_regime("orb", regime["tags"])["fits"] is True
    assert S.fits_regime("mean_reversion", regime["tags"])["fits"] is False
    assert S.fits_regime("orb", [])["fits"] is None


# --------------------------------------------------------------------------- backtest


def _first_trade(strategy: str, costs: bt.Costs) -> dict | None:
    for seed in range(60):
        bars = day(seed, drift=0.0003)
        result = bt.run_backtest([bt.SymbolDay("X", f"2026-11-{10 + seed % 15:02d}", bars, DAILY)], [strategy], costs)
        sample = result["results"][strategy]["sample"]
        if sample:
            return sample[0]
    return None


def test_a_trade_is_entered_at_the_next_open_and_costs_reduce_the_result():
    free = bt.Costs(slippage_bps=0.0, commission_per_share=0.0, short_borrow_bps_per_day=0.0)
    heavy = bt.Costs(slippage_bps=30.0, commission_per_share=0.01, short_borrow_bps_per_day=0.0)
    a, b = _first_trade("momentum_breakout", free), _first_trade("momentum_breakout", heavy)
    assert a and b and a["symbol"] == b["symbol"] == "X"
    assert b["r"] < a["r"] and b["cost_r"] > a["cost_r"] and a["cost_r"] == 0.0
    assert a["reason"] in {"stop", "target", "flatten"} and a["bars"] >= 1


def test_metrics_match_hand_calculation_and_flag_small_samples():
    trades = [{"r": r, "date": f"2026-11-{d:02d}", "cost_r": 0.05} for r, d in [(2.0, 1), (-1.0, 2), (2.0, 3), (-1.0, 4), (-1.0, 5)]]
    m = bt.metrics(trades)
    assert m["trades"] == 5 and m["reliable"] is False
    assert m["win_rate"] == pytest.approx(0.4) and m["profit_factor"] == pytest.approx(4.0 / 3.0)
    assert m["avg_win_r"] == 2.0 and m["avg_loss_r"] == -1.0 and m["expectancy_r"] == pytest.approx(0.2)
    assert m["total_r"] == 1.0 and m["max_drawdown_r"] == 2.0 and m["sharpe"] is None  # under 10 dage: ingen Sharpe
    assert bt.metrics([])["win_rate"] is None


def test_in_and_out_of_sample_and_walk_forward_split_by_date():
    trades = [{"r": 1.0 if i % 3 else -1.0, "date": f"2026-11-{i + 1:02d}", "cost_r": 0.0, "regime": ["bullish"]} for i in range(20)]
    split = bt.split_in_out(trades)
    assert split["split_date"] and split["in_sample"]["trades"] + split["out_sample"]["trades"] == 20
    assert max(t["date"] for t in trades if t["date"] < split["split_date"]) < split["split_date"]
    wf = bt.walk_forward(trades, windows=4)
    assert len(wf) == 4 and sum(w["trades"] for w in wf) == 20
    assert bt.walk_forward(trades[:3]) == [] and bt.split_in_out(trades[:2])["out_sample"] is None
    assert bt.by_regime(trades)["bullish"]["trades"] == 20


def test_backtest_runs_all_strategies_over_many_days_without_errors():
    days = [bt.SymbolDay("X", f"2026-11-{1 + k:02d}", day(100 + k, drift=0.0002), DAILY, ["sideways"]) for k in range(10)]
    result = bt.run_backtest(days)
    assert set(result["results"]) == set(S.DETECTORS) and result["days"] == 10 and result["costs"]["slippage_bps"] == 5.0
    for entry in result["results"].values():
        assert {"metrics", "in_sample", "out_sample", "walk_forward", "by_regime"} <= set(entry)


# --------------------------------------------------------------------------- score, godkendelse og turnering


def test_score_and_approval_need_enough_proven_trades():
    few = {"trades": 5, "expectancy_r": 1.0, "profit_factor": 3.0}
    assert bt.strategy_score(few)["score"] is None
    good = {"trades": 40, "expectancy_r": 0.4, "profit_factor": 1.8, "max_drawdown_r": 4.0}
    assert 60 <= bt.strategy_score(good)["score"] <= 100
    assert bt.strategy_score({"trades": 40, "expectancy_r": -0.3, "profit_factor": 0.6, "max_drawdown_r": 15.0})["score"] < 30
    assert bt.approval(few)["approved"] is False and len(bt.approval(few)["missing"]) == 2
    out_good = {"out_sample": {"trades": 15, "expectancy_r": 0.2}}
    assert bt.approval(good, out_good)["approved"] is True
    assert bt.approval(good, {"out_sample": {"trades": 15, "expectancy_r": -0.1}})["approved"] is False
    assert bt.approval(good)["approved"] is False  # uden backtest ingen godkendelse


def test_the_tournament_opens_and_closes_virtual_trades_and_ranks_strategies():
    tour = bt.Tournament(start_equity=10_000.0, costs=bt.Costs(slippage_bps=0.0))
    opened = 0
    for seed in range(80):
        bars = day(seed, drift=0.0003)
        for end in range(40, 360, 40):
            res = tour.step({"X": bars[:end]}, {"X": DAILY}, day=f"2026-11-{10 + seed % 20:02d}", enabled={"momentum_breakout", "orb"}, regime=["bullish"], flatten=False)
            opened += res["opened"]
        tour.step({"X": bars[:380]}, {"X": DAILY}, day=f"2026-11-{10 + seed % 20:02d}", enabled=set(), regime=["bullish"], flatten=True)
    assert opened > 0
    rows = tour.leaderboard()
    assert rows and all(r["id"] in S.LIBRARY for r in rows)
    first = rows[0]
    assert first["trades"] == len([t for t in tour.state["accounts"][first["id"]]["trades"]])
    assert all(not acct["open"] for acct in tour.state["accounts"].values())  # alt lukket efter flatten
    for acct in tour.state["accounts"].values():
        for t in acct["trades"]:
            assert t["qty"] >= 1 and abs(t["r"]) < 20 and t["reason"] in {"stop", "target", "flatten", "stale"}
            risk_usd = abs(t["entry"] - t["stop"]) * t["qty"]
            assert risk_usd <= 10_000 * 0.006 * 3  # risikoen pr. handel er begrænset
    assert tour.best("month", "2026-11-30") is None or tour.best("month", "2026-11-30")["name"]


def test_the_tournament_never_opens_after_flatten_and_never_twice_for_the_same_symbol():
    tour = bt.Tournament(costs=bt.Costs(slippage_bps=0.0))
    bars = day(3, drift=0.0004)
    for end in range(30, 300, 10):
        tour.step({"X": bars[:end]}, {"X": DAILY}, day="2026-11-12", enabled=set(S.DETECTORS), regime=[], flatten=False)
        for acct in tour.state["accounts"].values():
            assert len([p for p in acct["open"] if p["symbol"] == "X"]) <= 1
    n = sum(len(a["trades"]) + len(a["open"]) for a in tour.state["accounts"].values())
    tour.step({"X": bars[:300]}, {"X": DAILY}, day="2026-11-12", enabled=set(S.DETECTORS), regime=[], flatten=True)
    assert sum(len(a["trades"]) + len(a["open"]) for a in tour.state["accounts"].values()) <= n + 0  # flatten åbner intet nyt
    assert all(not a["open"] for a in tour.state["accounts"].values())


# --------------------------------------------------------------------------- motoren og siden
from test_desk import FakeBroker, make_desk  # noqa: E402


def test_the_desk_engine_runs_strategies_on_each_scan_and_never_breaks_the_scan(tmp_path):
    desk, _ = make_desk(tmp_path, FakeBroker({}))
    view = desk.strategies_view()
    assert len(view["library"]) == len(S.LIBRARY) and view["market"] and view["backtest"]["empty"]
    assert all(row["enabled"] for row in view["library"]) and view["pipeline"]["on"] is False
    assert any("kun papir" in line.lower() or "kun dem, appen selv" in line.lower() for line in view["notes"]) or view["notes"]
    desk.hub.set_enabled("orb", False)
    assert next(r for r in desk.strategies_view()["library"] if r["id"] == "orb")["enabled"] is False
    # tilstanden gemmes, så den overlever en genstart
    from trader.desk.hub import StrategyHub

    assert "orb" not in StrategyHub(desk.store, desk.market, lambda: desk._now()).enabled
    with pytest.raises(ValueError):
        desk.hub.set_enabled("findes_ikke", True)


def test_hub_scan_collects_signals_regime_and_paper_trades_from_real_bar_data(tmp_path):
    desk, _ = make_desk(tmp_path, FakeBroker({}))
    bars = day(5, drift=0.0004)
    spy = [Bar(b.ts, 100 + i * 0.02, 100.03 + i * 0.02, 99.99 + i * 0.02, 100.02 + i * 0.02, 5000) for i, b in enumerate(bars)]
    for end in range(40, 361, 20):
        now = bars[end - 1].ts + timedelta(minutes=1)
        desk.hub.on_scan({"X": bars[:end], desk.market.benchmark: spy[:end]}, {"X": DAILY, desk.market.benchmark: DAILY}, now, benchmark=desk.market.benchmark,
                         flatten=False, entries=True, session_minutes=390)
    assert desk.hub.regime["tags"] and "bullish" in desk.hub.regime["tags"]
    assert desk.hub.last_step
    view = desk.strategies_view()
    for sig in view["signals"]:
        assert sig["symbol"] == "X" and sig["stop"] != sig["entry"] and sig["independent"] >= 1 and sig["reasons"]
    now = bars[399 if len(bars) > 399 else -1].ts
    desk.hub.on_scan({"X": bars[:360]}, {"X": DAILY}, now, benchmark=desk.market.benchmark, flatten=True, entries=False, session_minutes=390)
    assert all(not acct["open"] for acct in desk.hub.tournament.state["accounts"].values())


def test_the_pipeline_switch_needs_approved_strategies_and_two_independent_confirmations(tmp_path):
    desk, _ = make_desk(tmp_path, FakeBroker({}))
    desk.hub.signals = [{"strategy": "orb", "symbol": "X", "independent": 3, "fits": True, "name": "ORB"}]
    desk.hub.set_pipeline(True)
    assert desk.hub.pipeline_candidates() == []  # ingen strategi er godkendt endnu
    desk.hub.set_pipeline(False)
    assert desk.hub.pipeline_candidates() == [] and desk.strategies_view()["pipeline"]["on"] is False


def test_strategy_endpoints_toggle_pipeline_and_refuse_backtest_without_history(tmp_path):
    import dataclasses

    from fastapi.testclient import TestClient

    from trader.app import create_app
    from trader.config import Settings

    desk, _ = make_desk(tmp_path, FakeBroker({}))
    settings = dataclasses.replace(Settings.from_env(), db_path=str(tmp_path / "t.db"), host="127.0.0.1", app_username="", app_password="", app_password_hash="", session_secret="")
    desk.start = lambda: None
    desk.stop = lambda: None
    app = create_app(settings=settings, desk_engine=desk)
    with TestClient(app) as client:
        assert len(client.get("/api/desk/strategies").json()["library"]) == len(S.LIBRARY)
        assert client.post("/api/desk/strategies/toggle", json={"id": "orb", "enabled": False}).status_code == 200
        assert client.post("/api/desk/strategies/toggle", json={"id": "nope", "enabled": True}).status_code == 400
        assert client.post("/api/desk/strategies/pipeline", json={"on": True}).json()["ok"] is True
        assert client.post("/api/desk/strategies/backtest", json={"days": 5}).status_code == 400  # falsk mægler uden historiske kurser


class _HistoryBroker:
    """Falsk Alpaca med historiske minutkurser (UTC-tider, som Alpaca leverer dem)."""

    def __init__(self, dates: list[str]) -> None:
        self.dates = dates

    def _bars(self, symbols, timeframe, start, end):
        out = {}
        for n, symbol in enumerate(symbols):
            rows = []
            if timeframe == "1Min":
                for k, date in enumerate(self.dates):
                    start_et = datetime.fromisoformat(date).replace(hour=9, minute=30, tzinfo=ET)
                    for b in day(200 + k + n, drift=0.0003):
                        rows.append(Bar(start_et.astimezone(ZoneInfo("UTC")) + (b.ts - T0), b.open, b.high, b.low, b.close, b.volume))
            else:
                rows = [Bar(datetime(2026, 10, 1, 5, tzinfo=ZoneInfo("UTC")) + timedelta(days=k), 19, 21, 18, 20 + 0.1 * k, 390_000) for k in range(45)]
            out[symbol] = rows
        return out


def test_backtest_history_builds_whole_days_with_prior_daily_bars_and_stores_results(tmp_path):
    desk, _ = make_desk(tmp_path, FakeBroker({}))
    dates = [f"2026-11-{d:02d}" for d in (9, 10, 11, 12, 13, 16, 17, 18)]
    desk.hub._now = lambda: datetime(2026, 11, 20, 12, 0, tzinfo=ET)
    result = desk.hub.backtest_history(_HistoryBroker(dates), ["X", "Y"], 6, desk.market.benchmark, 390)
    assert result["days"] == 12 and result["dates"] == dates[-6:] and result["symbols"] == ["X", "Y"]
    assert result["costs"]["slippage_bps"] == 5.0 and "IEX" in result["data"] and "beskrivelse" in result["regime_note"]
    assert set(result["results"]) == set(S.DETECTORS)
    desk.hub.backtests = result
    view = desk.strategies_view()
    with_data = [row for row in view["library"] if row["backtest"] and row["backtest"]["metrics"]["trades"] > 0]
    assert with_data and view["backtest"]["days"] == 12
    for row in with_data:
        assert row["backtest"]["metrics"]["reliable"] is False  # få dage: aldrig stærke påstande
