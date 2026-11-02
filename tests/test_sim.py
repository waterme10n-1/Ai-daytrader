"""Simulatoren kører gennem den rigtige motor og tæller ikke med til live-oplåsning."""

from datetime import datetime
from zoneinfo import ZoneInfo

from trader.broker import SimBroker, resolve_intrabar
from trader.config import SIM_START_EQUITY, Settings
from trader.db import Database
from trader.engine import TradingEngine
from trader.research import analyze
from trader.signals import Bar, Candidate

ET = ZoneInfo("America/New_York")


def _engine(tmp_path) -> TradingEngine:
    settings = Settings.from_env()
    return TradingEngine(settings=settings, db=Database(str(tmp_path / "trader.db")))


def test_stop_wins_when_both_levels_are_touched():
    start = datetime(2026, 6, 1, 10, 0, tzinfo=ET)
    bar = Bar(ts=start, open=100.2, high=104, low=99, close=101, volume=1)
    assert resolve_intrabar("long", 99.5, 103, bar) == ("stop", 99.5)
    gap = Bar(ts=start, open=99, high=104, low=98, close=103.5, volume=1)
    assert resolve_intrabar("long", 99.5, 103, gap) == ("stop", 99)
    short_bar = Bar(ts=start, open=100, high=102, low=96, close=97, volume=1)
    assert resolve_intrabar("short", 101, 97, short_bar) == ("stop", 101)


def test_rules_fallback_is_labelled_and_fixed_confidence():
    candidate = Candidate(
        symbol="SPY",
        side="long",
        setup="orb",
        entry=100,
        stop=99,
        target=102,
        gap_pct=0.01,
        change_from_open_pct=0.01,
        vs_vwap_pct=0.004,
        trend_20d_pct=0.02,
        adr_pct=0.01,
        or_high=100.4,
        or_low=99.6,
        or_mid=100,
        vwap=99.8,
        day_open=99.5,
        last_price=100,
        bias=1,
    )
    moment = datetime(2026, 6, 1, 9, 50, tzinfo=ET)
    batch = analyze(Settings.from_env(), moment, "open", [candidate], {"bias": 1}, [], rules_only_mode=True)
    assert batch.source == "rules"
    assert batch.fallback
    assert "Regelbaseret" in batch.market_summary
    assert batch.verdicts[0].decision == "take"
    assert batch.verdicts[0].confidence == 70


def test_claude_errors_fall_back_to_rules(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("api nede")

    monkeypatch.setattr("trader.research._call_claude", boom)
    candidate = Candidate(
        symbol="QQQ",
        side="short",
        setup="close",
        entry=100,
        stop=101,
        target=98.5,
        gap_pct=-0.01,
        change_from_open_pct=-0.01,
        vs_vwap_pct=-0.004,
        trend_20d_pct=-0.02,
        adr_pct=0.01,
        or_high=101,
        or_low=99,
        or_mid=100,
        vwap=100.4,
        day_open=101,
        last_price=100,
        bias=-1,
    )
    settings = Settings(
        alpaca_api_key="",
        alpaca_secret_key="",
        alpaca_live_api_key="",
        alpaca_live_secret_key="",
        allow_live=False,
        live_max_capital=1000,
        anthropic_api_key="test-key",
        anthropic_model="claude-opus-5-5",
        anthropic_web_search_tool="web_search_20260209",
        anthropic_effort="high",
        anthropic_max_search_uses=5,
        anthropic_fallbacks="default",
        telegram_bot_token="",
        telegram_chat_id="",
        db_path=":memory:",
        port=8741,
    )
    batch = analyze(
        settings,
        datetime(2026, 6, 1, 15, tzinfo=ET),
        "close",
        [candidate],
        {"bias": -1},
        [],
        rules_only_mode=False,
    )
    assert batch.source == "rules"
    assert batch.fallback
    assert "fejlede" in batch.market_summary
    assert batch.verdicts[0].confidence == 70


def test_several_simulated_days_do_not_unlock_live(tmp_path):
    engine = _engine(tmp_path)
    notes = []
    for _ in range(8):
        result = engine.simulate_one_day()
        notes.append(result)
        assert result["trades"] >= 0
        assert engine.mode == "paper"
    assert engine.db.get_state("mode") in {None, "paper"}
    trades = engine.db.list_trades("sim", True, limit=500)
    assert trades, "simuleringen burde have handlet"
    assert all(trade["synthetic"] for trade in trades)
    assert all(trade["status"] == "closed" for trade in trades)
    setups = {trade["setup"] for trade in trades}
    assert "orb" in setups
    assert "close" in setups
    assert any((trade["pnl"] or 0) > 0 for trade in trades)
    assert any((trade["pnl"] or 0) < 0 for trade in trades)
    decisions = engine.db.list_decisions("sim", True, limit=500)
    assert decisions
    assert all(row["source"] == "rules" for row in decisions)
    assert any("Regelbaseret" in (row["market_summary"] or "") for row in decisions)
    assert any(row["outcome"] == "blocked" for row in decisions)
    equity = engine.db.list_equity("sim", True)
    assert len(equity) == 8
    assert all(row["end_equity"] is not None for row in equity)
    unlock = engine._unlock()
    assert unlock["summary"]["trading_days"] == 0
    assert unlock["summary"]["trades"] == 0
    assert not unlock["all_passed"]
    assert engine.db.open_trades("sim", True) == []
    # Egenkapitalen er faktisk flyttet af de lukkede handler.
    assert equity[-1]["end_equity"] != SIM_START_EQUITY
    assert {item["note"] for item in notes}


def test_three_engine_errors_flatten(tmp_path):
    engine = _engine(tmp_path)

    class Broker:
        def __init__(self) -> None:
            self.closed = 0

        def close_all_positions(self) -> None:
            self.closed += 1

    broker = Broker()
    engine._handle_error(RuntimeError("første"), broker)
    engine._handle_error(RuntimeError("anden"), broker)
    assert broker.closed == 0
    engine._handle_error(RuntimeError("tredje"), broker)
    assert broker.closed == 1
    assert engine.consecutive_errors == 0
    assert any("Tre fejl" in event["message"] for event in engine.db.recent_events())


def test_live_phrase_is_exact_and_rejected_without_track_record(tmp_path):
    engine = _engine(tmp_path)
    ok, message = engine.request_live("jeg forstår")
    assert not ok
    ok, message = engine.request_live("JEG FORSTÅR AT JEG KAN TABE RIGTIGE PENGE")
    assert not ok
    assert "kriterier" in message.lower() or "opfyldt" in message.lower()
    assert engine.mode == "paper"
