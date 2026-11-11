"""Mere kontrol fra siden: den almindelige strategis filtre, fortryd automatiske ændringer,
forklaringer på hver handel og strategi-vagten slået til."""

from __future__ import annotations

import dataclasses
import json
import time

import pytest
from fastapi.testclient import TestClient

import meme_fakes
from meme_fakes import make_engine
from test_meme_research import _done_shadow
from trader.config import Settings
from trader.meme.config import StrategyParams
from trader.meme.learning import explain_trade, fact_report
from trader.meme.store import now_iso


def _client(engine, tmp_path):
    from trader.app import create_app

    engine.start = lambda: None
    engine.stop = lambda: None
    settings = dataclasses.replace(Settings.from_env(), db_path=str(tmp_path / "t.db"), host="127.0.0.1", app_username="",
                                   app_password="", app_password_hash="", session_secret="")
    return TestClient(create_app(settings=settings, meme_engine=engine))


def _closed(engine, strategy, exit_reason, pnl_pct, *, facts=None, learned=True, peak=1.0, index=0):
    position_id = engine.store.open_position(
        {
            "strategy": strategy,
            "mode": "paper",
            "mint": f"{strategy}{exit_reason}{index}" + "x" * 30,
            "symbol": f"T{index}",
            "source": "sniper" if strategy == "sniper" else "dex_boost",
            "status": "open",
            "opened_at": now_iso(),
            "entry_price_usd": 1.0,
            "cost_usd": 3.0,
            "token_raw": 1000,
            "remaining_raw": 1000,
            "fees_usd": 0.01,
            "features": {"liquidity": 0.5},
            "safety": {"facts": facts or {}},
        }
    )
    engine.store.update_position(position_id, status="closed", closed_at=now_iso(), pnl_usd=3.0 * pnl_pct, pnl_pct=pnl_pct,
                                 exit_reason=exit_reason, remaining_raw=0, learned=learned, peak_ratio=peak)
    return position_id


# --------------------------------------------------------------------------- filtre fra siden


def test_regular_strategy_filters_can_be_set_from_the_page_and_are_locked(tmp_path):
    engine, *_ = make_engine()
    with _client(engine, tmp_path) as client:
        body = client.post("/api/meme/params", json={"changes": {"min_organic_score": 15, "stop_loss_pct": 0.9, "ukendt": 3}}).json()
        params = {row["name"]: row for row in body["params"]}
        assert params["min_organic_score"]["value"] == 15
        assert params["stop_loss_pct"]["value"] == 0.4  # klemt ind under den faste grænse
        assert params["min_organic_score"]["locked_until"] > time.time() + 6 * 86400
        assert params["stop_loss_pct"]["kind"] == "pct" and params["stop_loss_pct"]["group"] == "exit"
        assert params["min_liquidity_usd"]["locked_until"] is None
        assert client.post("/api/meme/params", json={"changes": {"min_score": "abc"}}).status_code == 400
        assert engine.store.get_json("params")["min_organic_score"] == 15
        body = client.post("/api/meme/params", json={"reset": True}).json()
        params = {row["name"]: row for row in body["params"]}
        assert params["min_organic_score"]["value"] == StrategyParams().min_organic_score
        assert params["min_organic_score"]["locked_until"] is None


def test_the_tightener_leaves_a_filter_you_set_alone():
    engine, *_ = make_engine(meme_fakes.settings(shadows=True))
    for i in range(40):
        _done_shadow(engine, "momentum", {"organic": 32 + (i % 8), "liq": 90_000}, -0.3, i)
    for i in range(60):
        _done_shadow(engine, "momentum", {"organic": 45 + i % 40, "liq": 90_000}, 0.08, 100 + i)
    engine.update_params({"min_organic_score": 20})  # samme tal, men ingen ændring = ingen lås
    assert engine._locked("momentum") == set()
    engine.update_params({"min_organic_score": 31})
    engine._auto_tighten(time.time())
    assert engine.params.min_organic_score == 31


def test_sniper_changes_from_you_are_locked_but_the_learnings_are_not():
    engine, *_ = make_engine()
    engine.update_sniper({"min_holders": 60})
    assert "min_holders" in engine._locked("sniper")
    engine.update_sniper({"min_liquidity_usd": 12_000}, manual=False)
    assert "min_liquidity_usd" not in engine._locked("sniper")
    engine.reset_sniper()
    assert engine._locked("sniper") == set()


def test_learning_skips_locked_sniper_filters_and_logs_the_rest():
    engine, *_ = make_engine()
    engine.update_sniper({"min_liquidity_usd": 9_000})
    for index in range(5):
        _closed(engine, "sniper", "rug_liquidity", -0.9, learned=False, index=index)
    engine._learn()
    assert engine.sniper.min_liquidity_usd == 9_000  # låst af dig
    assert engine.sniper.min_holders == 25  # strammet efter rugs
    entry = engine.store.get_json("auto_changes")[-1]
    assert entry["source"] == "læring" and {item["param"] for item in entry["changes"]} == {"min_holders", "max_top10_pct", "min_age_seconds"}


# --------------------------------------------------------------------------- automatiske ændringer og fortryd


def test_automatic_changes_are_logged_and_can_be_undone(tmp_path):
    engine, *_ = make_engine(meme_fakes.settings(shadows=True))
    for i in range(40):
        _done_shadow(engine, "momentum", {"organic": 32 + (i % 8), "liq": 90_000}, -0.3, i)
    for i in range(60):
        _done_shadow(engine, "momentum", {"organic": 45 + i % 40, "liq": 90_000}, 0.08, 100 + i)
    engine._auto_tighten(time.time())
    assert engine.params.min_organic_score == 40
    with _client(engine, tmp_path) as client:
        change = client.get("/api/meme/status").json()["auto_changes"][0]
        assert change["source"] == "strammer" and change["can_undo"] is True
        assert change["changes"][0]["old_text"] == "20" and change["changes"][0]["new_text"] == "40"
        body = client.post("/api/meme/changes/undo", json={"id": change["id"]}).json()
        assert "Fortrudt" in body["message"]
        assert engine.params.min_organic_score == 20
        assert "min_organic_score" in engine._locked("momentum")
        after = body["status"]["auto_changes"][0]
        assert after["undone_at"] and after["can_undo"] is False
        assert client.post("/api/meme/changes/undo", json={"id": change["id"]}).status_code == 400
        assert client.post("/api/meme/changes/undo", json={"id": "x"}).status_code == 400


def test_undo_is_refused_when_the_value_has_changed_since():
    engine, *_ = make_engine()
    engine._log_change("sniper", [("take_profit_pct", 1.0, 0.5)], "test", "mål")
    engine.sniper.take_profit_pct = 0.5
    engine.sniper.take_profit_pct = 2.0  # ændret igen bagefter
    entry = engine.status()["auto_changes"][0]
    assert entry["can_undo"] is False and entry["changed_since"] is True
    assert entry["changes"][0]["new_text"] == "50 %"
    with pytest.raises(ValueError, match="ændret igen"):
        engine.undo_change(entry["id"])


def test_an_old_change_for_a_removed_setting_does_not_break_the_page():
    engine, *_ = make_engine()
    engine._log_change("momentum", [("gammel_indstilling", 1.0, 2.0)], "fra en ældre version", "læring")
    entry = engine.status()["auto_changes"][0]
    assert entry["can_undo"] is False
    with pytest.raises(ValueError):
        engine.undo_change(entry["id"])


def test_sniper_target_change_is_logged_and_undo_restores_both_numbers():
    engine, *_ = make_engine()
    engine.update_sniper({"take_profit_pct": 0.5, "stop_loss_pct": 0.25}, manual=False)
    engine._log_change("sniper", [("take_profit_pct", 1.0, 0.5), ("stop_loss_pct", 0.35, 0.25)], "bedre mål", "mål")
    entry = engine.status()["auto_changes"][0]
    engine.undo_change(entry["id"])
    assert (engine.sniper.take_profit_pct, engine.sniper.stop_loss_pct) == (1.0, 0.35)
    assert engine.store.get_json("sniper")["take_profit_pct"] == 1.0
    assert {"take_profit_pct", "stop_loss_pct"} <= engine._locked("sniper")


# --------------------------------------------------------------------------- hvorfor vandt eller tabte den?


def test_explanations_use_the_fact_analysis_when_there_is_enough_data():
    rows = [{"facts": {"organic": v}, "pnl": -0.3 if v < 30 else 0.1 if v < 60 else 0.2} for v in range(0, 90)]
    analysis = fact_report(rows)
    lines = explain_trade({"organic": 12}, strategy="momentum", pnl_pct=-0.4, exit_reason="stop_loss", peak_pct=0.02, analysis=analysis)
    assert lines[0].startswith("Kom aldrig over +5 %")
    assert "Organisk score (Jupiter) 12: tokens i gruppen under 30 gav i snit -30 %." == lines[1]
    win = explain_trade({"organic": 80}, strategy="momentum", pnl_pct=0.5, exit_reason="target", peak_pct=0.9, analysis=analysis)
    assert win == ["Gevinstmålet blev ramt.", "Organisk score (Jupiter) 80: tokens i gruppen 60+ gav i snit +20 %."]


def test_explanations_fall_back_to_known_warning_signs():
    lines = explain_trade({"organic": 18, "insiders": 45, "chg_h1": 220, "tg": 0, "x": 0}, strategy="momentum",
                          pnl_pct=-0.3, exit_reason="stop_loss", peak_pct=0.3)
    assert lines[0] == "Nåede +30 %, men faldt til stop-loss, før målet blev ramt."
    assert lines[1].startswith("Lav organisk score (18)") and "45 insider-wallets" in lines[2]
    assert explain_trade({}, strategy="momentum", pnl_pct=-0.2, exit_reason="stale", peak_pct=0.0)[-1].startswith("Købt før botten gemte")
    copy = explain_trade({"chase": 0.22}, strategy="toptrader", pnl_pct=-0.3, exit_reason="copy_exit", peak_pct=0.0)
    assert copy == ["Traderen solgte, så botten solgte også.", "Købt 22 % over traderens egen pris."]


def test_closed_trades_on_the_page_say_why():
    engine, *_ = make_engine()
    _closed(engine, "momentum", "rug_liquidity", -0.85, facts={"organic": 15, "tg": 0, "x": 0})
    trade = engine.status()["trades"][0]
    assert trade["why"][0] == "Likviditeten blev trukket: et rug pull."
    assert any("organisk score" in line for line in trade["why"])
    assert engine.status()["positions"] == [] or "why" in engine.status()["positions"][0]


# --------------------------------------------------------------------------- strategi-vagten og Top 10


def test_upgrade_turns_the_guard_on_and_pauses_top_10_once():
    engine, *_ = make_engine()
    engine.store.set_json("params", engine.params.to_dict())  # en bot, der har kørt før
    engine.set_strategy({"guard": False})
    engine.update_toptraders({"enabled": True})
    engine.store.set_state("upgrade_v2", "")
    engine._maybe_upgrade_v2()
    assert engine.guard_on is True and engine.store.get_state("strategy_guard") == "1"
    assert engine.top_cfg.enabled is False
    assert "Strategi-vagten er slået til" in engine.store.recent_lessons(1)[0]["text"]
    # Kun én gang: dit eget valg bagefter står ved magt.
    engine.set_strategy({"guard": False})
    engine.update_toptraders({"enabled": True})
    engine._maybe_upgrade_v2()
    assert engine.guard_on is False and engine.top_cfg.enabled is True


def test_guard_switch_is_on_the_status_and_over_http(tmp_path):
    engine, *_ = make_engine()
    with _client(engine, tmp_path) as client:
        assert client.get("/api/meme/status").json()["strategy_guard"] is True
        assert client.post("/api/meme/strategies", json={"guard": False}).json()["strategy_guard"] is False
    page = (meme_fakes.__file__.rsplit("/tests/", 1)[0] + "/trader/static/meme.html")
    html = open(page, encoding="utf-8").read()
    for marker in ('id="guard-switch"', 'id="panel-almindelig"', 'id="changes"', "/api/meme/changes/undo", "/api/meme/params"):
        assert marker in html
    assert json.dumps(StrategyParams().to_dict())  # standarderne kan altid sendes til siden
