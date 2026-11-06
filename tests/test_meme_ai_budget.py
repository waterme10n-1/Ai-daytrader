"""Dagligt AI-loft: fra som standard (Claude tjekker hele dagen), men kan slås til i dashboardet."""

from __future__ import annotations

import dataclasses

from fastapi.testclient import TestClient

import meme_fakes
from meme_fakes import make_engine
from trader.meme.config import MemeSettings


def _ai_engine(**overrides):
    values = {"anthropic_api_key": "test-key", "ai_daily_budget_usd": 0.5}
    values.update(overrides)
    cfg = meme_fakes.settings(**values)
    engine, *_ = make_engine(cfg)
    return engine


def _spend(engine, usd):
    engine.store.add_ai_spend("research", "claude-opus-5-5", 1000, 100, 1, usd)


def _texts(engine):
    return [alert["text"] for alert in engine.status()["alerts"]]


def test_no_daily_cap_by_default():
    assert MemeSettings.from_env().ai_budget_limit is False
    engine = _ai_engine()
    _spend(engine, 3.0)  # langt over de 0,50 USD
    assert engine.researcher.available() is True
    status = engine.status()
    assert status["loss_limits"]["ai_budget"] is False
    assert status["kpis"]["ai_budget_left"] is None
    assert not any("AI-budget" in text for text in _texts(engine))


def test_cap_can_be_turned_on_and_stops_claude_for_the_day():
    engine = _ai_engine()
    engine.update_loss_limits({"ai_budget": True})
    assert engine.store.get_json("ai_budget", None) == {"on": True}
    assert engine.researcher.available() is True
    _spend(engine, 0.45)
    assert engine.researcher.available() is False  # et tjek regnes til 0,20 USD
    assert any("AI-budget er brugt" in text for text in _texts(engine))
    engine.update_loss_limits({"ai_budget": False})
    assert engine.researcher.available() is True
    events = [row["message"] for row in engine.store.recent_events(10)]
    assert any("AI-loft slået fra" in text for text in events)


def test_env_can_turn_the_cap_on(monkeypatch):
    monkeypatch.setenv("MEME_AI_BUDGET_LIMIT", "true")
    assert MemeSettings.from_env().ai_budget_limit is True
    engine = _ai_engine(ai_budget_limit=True)
    _spend(engine, 0.6)
    assert engine.researcher.available() is False


def test_budget_zero_still_means_claude_off():
    engine = _ai_engine(ai_daily_budget_usd=0.0)
    assert engine.researcher.available() is False


def test_dashboard_toggle(tmp_path):
    from trader.app import create_app
    from trader.config import Settings

    engine = _ai_engine()
    engine.start = lambda: None
    engine.stop = lambda: None
    settings = dataclasses.replace(Settings(), db_path=str(tmp_path / "t.db"))
    with TestClient(create_app(settings=settings, meme_engine=engine)) as client:
        body = client.post("/api/meme/limits", json={"ai_budget": True}).json()
        assert body["loss_limits"]["ai_budget"] is True
        body = client.post("/api/meme/limits", json={"ai_budget": False}).json()
        assert body["loss_limits"]["ai_budget"] is False
        assert body["loss_limits"]["drawdown"] is True  # de andre grænser røres ikke
