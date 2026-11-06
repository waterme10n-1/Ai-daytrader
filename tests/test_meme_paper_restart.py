"""Start papirkontoen forfra med et nyt beløb, og lad den almindelige strategi lære sit mål."""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

import meme_fakes
from meme_fakes import make_engine, raw_pair, safe_report
from test_meme_guard import _closed
from trader.meme.engine import MemeEngine, PAPER_RESTART_ONCE

MINT = "RestartMint111111"


def _buy_one(engine, dex, rug):
    dex.pairs[MINT] = raw_pair(MINT)
    dex.boosts.append(MINT)
    rug.reports[MINT] = safe_report()
    engine.tick(force_scan=True)
    return engine.store.open_positions("paper")


def test_restart_with_50_usd_archives_trades_scales_sizes_and_keeps_learning():
    engine, dex, rug, jup, notes = make_engine()
    _closed(engine, "momentum", [-0.4, 0.2])
    assert _buy_one(engine, dex, rug)
    engine.params.min_score = 0.61
    engine._save_learning()
    engine.store.add_lesson("handel", "En lektion fra før.")
    assert engine.settings.bankroll_usd == 15 and engine.settings.position_usd == 3

    message = engine.restart_paper(50)
    assert "50,00 USD" in message and "Læringen er beholdt" in message
    assert engine.store.open_positions("paper") == [] and engine.store.closed_positions("paper") == []
    assert engine._paper.cash_usd(0) == pytest.approx(50.0)
    status = engine.status()
    assert status["kpis"]["bankroll"] == 50 and status["kpis"]["equity"] == pytest.approx(50.0)
    assert status["settings"]["position_usd"] == pytest.approx(10.0)  # samme andel som 3 af 15
    assert engine.sniper.position_usd == pytest.approx(5.0) and engine.top_cfg.position_usd == pytest.approx(6.67, abs=0.01)
    assert engine.params.min_score == pytest.approx(0.61)  # læringen er der stadig
    assert any("En lektion fra før" in row["text"] for row in engine.store.recent_lessons(10))

    # Beløbet overlever en genstart, og den nye bot køber med de større beløb.
    again = MemeEngine(engine._env_settings, store=engine.store, clients=engine.clients, researcher=engine.researcher)
    assert again.settings.bankroll_usd == 50 and again.settings.position_usd == pytest.approx(10.0)
    assert again.sniper.position_usd == pytest.approx(5.0)


def test_restart_refuses_live_and_bad_amounts():
    engine, *_ = make_engine()
    with pytest.raises(ValueError):
        engine.restart_paper(2)
    with pytest.raises(ValueError):
        engine.restart_paper("mange")
    engine.mode = "live"
    with pytest.raises(ValueError):
        engine.restart_paper(50)


def test_live_always_uses_the_hard_cap_from_the_environment():
    engine, *_ = make_engine()
    engine.restart_paper(50)
    assert engine._mode_settings("live").bankroll_usd == 15
    assert engine._mode_settings("paper").bankroll_usd == 50


def test_existing_paper_run_restarts_once_with_50_after_the_update():
    engine, dex, rug, jup, notes = make_engine()
    _closed(engine, "momentum", [-0.4])
    engine.store.set_state("paper_restart_once", "")  # som en bot fra før opdateringen
    fresh = MemeEngine(engine._env_settings, store=engine.store, clients=engine.clients, researcher=engine.researcher)
    assert fresh.settings.bankroll_usd == PAPER_RESTART_ONCE[1] == 50
    assert fresh.store.closed_positions("paper") == []
    third = MemeEngine(engine._env_settings, store=engine.store, clients=engine.clients, researcher=engine.researcher)
    assert third.settings.bankroll_usd == 50  # kun én gang
    empty, *_ = make_engine()
    assert empty.settings.bankroll_usd == 15  # en helt ny bot rører vi ikke


def test_dashboard_restart(tmp_path):
    from trader.app import create_app
    from trader.config import Settings

    engine, *_ = make_engine()
    engine.start = lambda: None
    engine.stop = lambda: None
    settings = dataclasses.replace(Settings(), db_path=str(tmp_path / "t.db"))
    with TestClient(create_app(settings=settings, meme_engine=engine)) as client:
        body = client.post("/api/meme/paper/restart", json={"amount": 50}).json()
        assert body["ok"] and body["status"]["kpis"]["bankroll"] == 50
        assert client.post("/api/meme/paper/restart", json={"amount": 1}).status_code == 400


def test_momentum_learns_a_better_target_only_when_it_clearly_pays():
    engine, *_ = make_engine(meme_fakes.settings(shadows=True))
    engine._optimizer = lambda: {"n": 0}
    good = {"n": 60, "best": {"tp": 0.4, "sl": 0.2, "avg": 0.05}, "current": {"tp": 0.6, "sl": 0.25, "avg": -0.02}}
    engine._momentum_optimizer = lambda: good
    engine._maybe_auto_targets()
    assert (engine.params.take_profit_pct, engine.params.stop_loss_pct) == (0.4, 0.2)
    assert engine.store.get_json("params")["take_profit_pct"] == 0.4
    assert "almindelige strategis mål er ændret" in engine.store.recent_lessons(5)[0]["text"]
    engine.params.take_profit_pct = 0.6
    engine._last_auto_target_check = 0
    engine._maybe_auto_targets()
    assert engine.params.take_profit_pct == 0.6  # højst én gang i døgnet
    off, *_ = make_engine(meme_fakes.settings(shadows=True))
    off._optimizer = lambda: {"n": 0}
    off._momentum_optimizer = lambda: {**good, "best": {"tp": 0.4, "sl": 0.2, "avg": -0.01}}
    off._maybe_auto_targets()
    assert off.params.take_profit_pct == 0.6  # intet sæt giver overskud


def test_momentum_optimizer_runs_on_real_shadow_paths():
    engine, *_ = make_engine(meme_fakes.settings(shadows=True))
    for i in range(45):
        sid = engine.store.add_shadow({"mode": "paper", "mint": f"Sh{i}" + "x" * 30, "symbol": f"S{i}", "strategy": "momentum", "stage": "risiko", "entry_price": 1.0})
        engine.store.update_shadow(sid, status="done", path=[[0, 1.0], [20, 1.45], [60, 0.7], [361, 0.6]], peak_ratio=1.45, last_ratio=0.6)
    opt = engine._momentum_optimizer()
    assert opt["n"] == 45 and opt["best"]["tp"] <= 0.4  # tag gevinsten, før den forsvinder
    assert opt["best"]["avg"] > opt["current"]["avg"]
