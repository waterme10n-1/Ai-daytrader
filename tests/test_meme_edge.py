"""Handl kun med en fordel: strategi-vagten starter først igen, når tokens fra pausen viser overskud,
holder pause tidligere, når både handler og skyggehandler taber klart, og scoren kalibreres af data."""

from __future__ import annotations

import time

import meme_fakes
from meme_fakes import make_engine
from test_meme_guard import _closed
from trader.meme.store import now_iso


def _shadows(engine, strategy, pnls, stage="risiko"):
    for index, pnl in enumerate(pnls):
        shadow = engine.store.add_shadow({"mode": "paper", "mint": f"{strategy}{stage}{index}{pnl}", "symbol": "S", "strategy": strategy,
                                          "stage": stage, "entry_price": 1.0, "features": {"liquidity": 0.5}})
        engine.store.update_shadow(shadow, status="done", sim_pnl_pct=pnl, verdict="neutral", done_at=now_iso())


def _paused(engine, strategy="momentum"):
    _closed(engine, strategy, [-0.4] * 8)
    engine._check_guard()
    return engine._holds()[strategy]


def _expire(engine, strategy="momentum"):
    holds = engine._holds()
    holds[strategy]["until"] = time.time() - 1
    engine._save_holds(holds)
    engine._check_guard()
    return engine._holds().get(strategy)


def test_pause_is_extended_while_the_strategy_would_still_lose():
    engine, *_ = make_engine(meme_fakes.settings(strategy_guard=True))
    _paused(engine)
    _shadows(engine, "momentum", [-0.08] * 20)
    hold = _expire(engine)
    assert hold is not None and hold["until"] > time.time() + 23 * 3600
    assert "-8,0 %" in hold["reason"] and "fortsat pause" in hold["reason"]
    assert engine._on_hold("momentum")
    view = {row["id"]: row for row in engine.status()["strategies"]}["momentum"]
    assert "fortsat pause" in view["hold"]["reason"]


def test_pause_ends_when_the_tokens_during_the_pause_show_a_profit():
    engine, *_ = make_engine(meme_fakes.settings(strategy_guard=True))
    _paused(engine)
    _shadows(engine, "momentum", [-0.05] * 5 + [0.06] * 15)  # i snit +3,25 %
    assert _expire(engine) is None
    events = [row["message"] for row in engine.store.recent_events(5)]
    assert any("kører igen" in text and "20 tokens" in text and "3,2 %" in text for text in events)


def test_too_little_evidence_waits_but_not_forever():
    engine, *_ = make_engine(meme_fakes.settings(strategy_guard=True))
    hold = _paused(engine)
    _shadows(engine, "momentum", [0.2] * 3)
    hold = _expire(engine)
    assert hold is not None and "kun 3 af 15" in hold["reason"]
    holds = engine._holds()
    holds["momentum"]["since"] = "2026-01-01T00:00:00+00:00"  # har holdt pause i flere døgn
    engine._save_holds(holds)
    assert _expire(engine) is None


def test_manual_holds_and_a_switched_off_guard_are_left_alone():
    engine, *_ = make_engine(meme_fakes.settings(strategy_guard=True))
    engine.set_strategy({"hold": "sniper"})
    _shadows(engine, "sniper", [-0.3] * 20)
    engine._check_guard()
    assert engine._holds()["sniper"]["until"] is None  # din pause: kun du kan genoptage
    _paused(engine)
    _shadows(engine, "momentum", [-0.3] * 20)
    engine.set_strategy({"guard": False})
    assert _expire(engine) is None  # vagten er slået fra: pausen slutter som før


def test_clear_losses_in_trades_and_shadows_pause_before_eight_trades():
    engine, *_ = make_engine(meme_fakes.settings(strategy_guard=True))
    _closed(engine, "sniper", [-0.4] * 4)
    engine._check_guard()
    assert "sniper" not in engine._holds()  # fire tab alene er ikke nok
    _shadows(engine, "sniper", [-0.12] * 30)
    engine._guard_seen = -1
    engine._check_guard()
    hold = engine._holds()["sniper"]
    assert hold["auto"] and "-12,0 %" in hold["reason"] and "ikke bare uheld" in hold["reason"]


def test_edge_is_shown_per_strategy_and_counts_only_tokens_it_would_buy():
    engine, *_ = make_engine(meme_fakes.settings(shadows=True))
    _shadows(engine, "momentum", [0.1] * 10, stage="købt")
    _shadows(engine, "momentum", [-0.2] * 10, stage="risiko")
    _shadows(engine, "momentum", [5.0] * 10, stage="score")  # afvist på score: tæller ikke
    edge = engine._edge("momentum")
    assert edge["n"] == 20 and abs(edge["avg"] - (-0.05)) < 1e-9 and edge["win_rate"] == 0.5
    view = {row["id"]: row for row in engine.status()["strategies"]}["momentum"]
    assert view["edge"]["n"] == 20 and abs(view["edge"]["avg"] + 0.05) < 1e-9


def test_score_is_a_fact_so_the_tightener_can_raise_the_minimum_score():
    engine, *_ = make_engine(meme_fakes.settings(shadows=True))
    low = {"liquidity": 0.5, "buy_pressure": 0.5, "organic": 0.5, "socials": 0.5, "rug_risk": 0.3}  # lige over min. score
    high = {"liquidity": 1.0, "buy_pressure": 1.0, "organic": 1.0, "socials": 1.0, "rug_risk": 0.0}
    assert engine.params.min_score < engine.model.score(low) < 0.6 < engine.model.score(high)
    for index in range(40):
        shadow = engine.store.add_shadow({"mode": "paper", "mint": f"L{index}", "symbol": "L", "strategy": "momentum", "stage": "risiko",
                                          "entry_price": 1.0, "features": low, "facts": {"liq": 90_000}})
        engine.store.update_shadow(shadow, status="done", sim_pnl_pct=-0.2, verdict="dodged", done_at=now_iso())
    for index in range(40):
        shadow = engine.store.add_shadow({"mode": "paper", "mint": f"H{index}", "symbol": "H", "strategy": "momentum", "stage": "købt",
                                          "entry_price": 1.0, "features": high, "facts": {"liq": 90_000}})
        engine.store.update_shadow(shadow, status="done", sim_pnl_pct=0.1, verdict="taken", done_at=now_iso())
    rows = engine._shadow_rows("momentum")
    assert all("score" in row["facts"] for row in rows)
    old = engine.params.min_score
    engine._auto_tighten(time.time())
    assert engine.params.min_score > old
    assert engine.store.get_json("auto_changes")[-1]["changes"][0]["param"] == "min_score"
