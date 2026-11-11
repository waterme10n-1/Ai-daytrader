"""Flere handler: prøvekøb for en tredjedel af beløbet, antal handler fra siden og flere tjek pr. runde."""

from __future__ import annotations

import dataclasses
import json
import time

import pytest
from fastapi.testclient import TestClient

import meme_fakes
from meme_fakes import make_engine, raw_pair, safe_report
from test_meme_guard import _closed
from trader.config import Settings
from trader.meme.clients import best_pairs, parse_rug_report
from trader.meme.engine import TRIAL_MAX_OPEN, TRIAL_PER_DAY_DEFAULT
from trader.meme.learning import build_features
from trader.meme.store import now_iso


def _arm(dex, rug, mint):
    dex.pairs[mint] = raw_pair(mint)
    dex.boosts.append(mint)
    rug.reports[mint] = safe_report()


def _score(engine, mint):
    pair = best_pairs([raw_pair(mint)])[mint]
    features = build_features(pair, report=parse_rug_report(mint, safe_report()), boosted=True, now_ms=int(time.time() * 1000))
    return engine.model.score(features)


def _just_below(engine, mint, gap=0.05):
    engine.params.min_score = min(0.85, _score(engine, mint) + gap)


def test_a_score_just_under_the_limit_becomes_a_small_trial_buy():
    engine, dex, rug, _jup, _notes = make_engine()
    _arm(dex, rug, "NearMiss111")
    _just_below(engine, "NearMiss111")
    engine.tick(force_scan=True)
    rows = engine.store.open_positions("paper")
    assert len(rows) == 1
    pos = rows[0]
    assert json.loads(pos["safety"])["trial"] is True
    assert pos["cost_usd"] == pytest.approx(1.0)  # en tredjedel af 3 USD, men mindst 1 USD
    assert pos["reasoning"].startswith("Prøvekøb for 1,00 USD i stedet for 3,00 USD (scoren var lige under grænsen)")
    assert engine.status()["positions"][0]["trial"] is True
    assert engine.last_scan.get("trials") == 1


def test_far_below_the_limit_or_trials_switched_off_means_no_buy():
    engine, dex, rug, _jup, _notes = make_engine()
    _arm(dex, rug, "FarBelow111")
    _just_below(engine, "FarBelow111", gap=0.2)
    engine.tick(force_scan=True)
    assert engine.store.open_positions("paper") == []
    engine2, dex2, rug2, _jup2, _notes2 = make_engine()
    engine2.update_trade_limits({"trial_per_day": 0})
    _arm(dex2, rug2, "NearMiss222")
    _just_below(engine2, "NearMiss222")
    engine2.tick(force_scan=True)
    assert engine2.store.open_positions("paper") == []
    reasons = [row["reason"] for row in engine2.store.recent_candidates("paper")]
    assert any(reason.startswith("Score") for reason in reasons)


def test_trials_have_their_own_limits_and_do_not_block_regular_buys():
    engine, dex, rug, _jup, _notes = make_engine(meme_fakes.settings(max_open=1, bankroll_usd=50.0))
    for index in range(TRIAL_MAX_OPEN + 1):
        mint = f"Trial{index}aaaa"
        _arm(dex, rug, mint)
        _just_below(engine, mint)
        engine.tick(force_scan=True)
    trials = [row for row in engine.store.open_positions("paper") if engine._is_trial(row)]
    assert len(trials) == TRIAL_MAX_OPEN  # den fjerde fik ikke plads
    engine.params.min_score = 0.45  # nu består alt: et almindeligt køb skal stadig kunne ske
    _arm(dex, rug, "Regular1111")
    engine.tick(force_scan=True)
    regular = [row for row in engine.store.open_positions("paper") if not engine._is_trial(row)]
    assert [row["mint"] for row in regular] == ["Regular1111"]


def test_a_paused_strategy_keeps_learning_with_small_trial_buys():
    engine, dex, rug, jup, _notes = make_engine(meme_fakes.settings(strategy_guard=True, max_losses_per_day=0))  # tabsstoppet testes for sig
    _closed(engine, "sniper", [-0.5] * 7 + [0.2])
    engine._check_guard()
    assert engine._on_hold("sniper")
    from test_meme_sniper import NEW, _arm as arm_sniper

    arm_sniper(dex, rug, jup)
    engine.tick(force_scan=True)
    rows = engine.store.open_positions("paper")
    assert len(rows) == 1 and rows[0]["strategy"] == "sniper" and rows[0]["mint"] == NEW
    assert engine._is_trial(rows[0]) and "strategi-vagten" in rows[0]["reasoning"]
    assert rows[0]["cost_usd"] == pytest.approx(1.0)
    # Din egen pause betyder ingen køb, heller ikke prøvekøb.
    engine2, dex2, rug2, jup2, _notes2 = make_engine()
    engine2.set_strategy({"hold": "sniper"})
    arm_sniper(dex2, rug2, jup2)
    engine2.tick(force_scan=True)
    assert engine2.store.open_positions("paper") == []


def test_trials_never_count_towards_real_money_and_never_happen_live():
    engine, *_ = make_engine()
    for index in range(25):
        position_id = engine.store.open_position({
            "strategy": "momentum", "mode": "paper", "mint": f"T{index}" + "x" * 30, "symbol": "T", "source": "dex_boost", "status": "open",
            "opened_at": now_iso(), "entry_price_usd": 1.0, "cost_usd": 1.0, "token_raw": 1, "remaining_raw": 1, "fees_usd": 0.0,
            "safety": {"trial": True},
        })
        engine.store.update_position(position_id, status="closed", closed_at=now_iso(), pnl_usd=1.0, pnl_pct=1.0, exit_reason="target", remaining_raw=0)
    items = {item["id"]: item for item in engine._unlock()["items"]}
    assert items["trades"]["detail"].startswith("0 /") and items["pnl"]["ok"] is False
    engine.mode = "live"
    assert engine._trial_room("2026-11-09") is False


def test_trade_limits_can_be_set_from_the_page(tmp_path):
    from trader.app import create_app

    engine, *_ = make_engine()
    engine.start = lambda: None
    engine.stop = lambda: None
    settings = dataclasses.replace(Settings.from_env(), db_path=str(tmp_path / "t.db"), host="127.0.0.1", app_username="",
                                   app_password="", app_password_hash="", session_secret="")
    with TestClient(create_app(settings=settings, meme_engine=engine)) as client:
        body = client.post("/api/meme/trade-limits", json={"changes": {"max_open": 6, "max_trades_per_day": 999, "trial_per_day": 4, "ukendt": 1}}).json()
        fields = {field["name"]: field for field in body["trade_limits"]["fields"]}
        assert fields["max_open"]["value"] == 6 and fields["max_trades_per_day"]["value"] == 50  # klemt ind under loftet
        assert fields["trial_per_day"]["value"] == 4 and fields["trial_per_day"]["default"] == TRIAL_PER_DAY_DEFAULT
        assert engine.settings.max_open == 6 and engine.settings.max_trades_per_day == 50
        assert client.post("/api/meme/trade-limits", json={"changes": {"max_open": "mange"}}).status_code == 400
    assert engine._mode_settings("live").max_open == engine._env_settings.max_open  # rigtige penge: miljøet gælder


def test_running_bots_get_more_trades_once_and_new_bots_use_the_new_defaults():
    engine, *_ = make_engine(meme_fakes.settings(max_open=3, max_trades_per_day=8, sniper_max_open=2, sniper_max_per_day=10))
    assert engine.settings.max_open == 3  # ny bot: ingen opgradering, standarderne fra miljøet
    engine.store.set_json("params", engine.params.to_dict())
    engine.store.set_state("upgrade_v3", "")
    engine._maybe_upgrade_v3()
    assert (engine.settings.max_open, engine.settings.max_trades_per_day) == (4, 15)
    assert (engine.settings.sniper_max_open, engine.settings.sniper_max_per_day) == (3, 15)
    assert "Flere handler på papir" in engine.store.recent_lessons(1)[0]["text"]
    engine.update_trade_limits({"max_open": 2})
    engine._maybe_upgrade_v3()  # kun én gang: dit eget valg står ved magt
    assert engine.settings.max_open == 2


def test_running_bots_let_more_coins_through_once_but_keep_your_own_numbers():
    engine, *_ = make_engine()
    engine.params.min_liquidity_usd = 25_000.0
    engine.params.min_organic_score = 30.0
    engine.params.min_score = 0.6
    engine.update_params({"min_score": 0.7})  # din egen: låst
    engine.sniper.min_holders = 25
    engine.store.set_json("params", engine.params.to_dict())
    engine.store.set_state("upgrade_v4", "")
    engine._maybe_upgrade_v4()
    assert engine.params.min_liquidity_usd == 15_000 and engine.params.min_organic_score == 20
    assert engine.params.min_score == 0.7
    assert engine.sniper.min_holders == 15
    assert engine.store.get_json("params")["min_liquidity_usd"] == 15_000
    log = engine.store.get_json("auto_changes")
    assert {item["param"] for entry in log for item in entry["changes"]} >= {"min_liquidity_usd", "min_organic_score", "min_holders"}
    assert "Rug-" in engine.store.recent_lessons(1)[0]["text"]
    engine.params.min_liquidity_usd = 30_000
    engine._maybe_upgrade_v4()  # kun én gang
    assert engine.params.min_liquidity_usd == 30_000
