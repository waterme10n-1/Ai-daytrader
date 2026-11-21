"""Sikkerhedsscore, AI-score, marked-regime og ydelse: alt bygger på rigtige tal, og hvert fradrag kan forklares."""

from __future__ import annotations

import time

from trader.meme.clients import RugReport
from trader.meme.scores import ai_score, market_regime, performance, safety_score


def _report(**over) -> RugReport:
    base = dict(mint="m", risk=0.1, risks=[], dangers=[], warnings=[], mint_authority=False, freeze_authority=False, top10_pct=12.0,
                lp_locked_pct=100.0, holders=500, rugged=False, insiders=0, decimals=6, raw_score=100.0)
    base.update(over)
    return RugReport(**base)


def test_a_clean_token_scores_100_and_every_cut_has_a_reason():
    clean = safety_score(_report())
    assert clean["score"] == 100 and clean["grade"] == "Høj" and clean["factors"] == []
    bad = safety_score(_report(mint_authority=True, freeze_authority=True, top10_pct=60.0, lp_locked_pct=10.0, insiders=4, dangers=["x"], holders=20))
    assert bad["score"] < 40 and bad["grade"] == "Farlig"
    assert all(item["label"] and item["points"] > 0 for item in bad["factors"])
    assert sum(item["points"] for item in bad["factors"]) == 100 - bad["score"] or bad["score"] == 0


def test_unknown_is_not_safe_and_a_rugged_token_is_zero():
    assert safety_score(None)["score"] == 75 and safety_score(None)["known"] is False
    assert safety_score(_report(rugged=True))["score"] == 0
    assert safety_score(_report(), roundtrip_loss_pct=25.0)["score"] == 85  # honeypot-tegn


def test_ai_score_follows_the_model_and_a_dangerous_token_is_always_avoid():
    assert ai_score(0.93, 90)["band"] == "Exceptional setup" and ai_score(0.93, 90)["action"] == "Strong Buy"
    assert ai_score(0.93, 30)["action"] == "Avoid"
    assert ai_score(0.75, 70) == {"score": 75, "band": "Potential setup", "action": "Buy"}
    assert ai_score(0.55, 90)["action"] == "Hold" and ai_score(0.2, 90)["action"] == "Avoid"
    assert ai_score(None)["score"] is None


def test_market_regime_needs_data_and_names_a_falling_market_risk_off():
    now = time.time()
    assert market_regime([], now)["label"] == "Ukendt"
    falling = [(now - 3000 + i * 300, 150.0 - i * 1.0) for i in range(10)]
    assert market_regime(falling, now)["label"] == "Risk-off" and market_regime(falling, now)["trade"] is False
    up = [(now - 3000 + i * 300, 150.0 + i * 0.5) for i in range(10)]
    assert market_regime(up, now)["label"] == "Stærkt momentum"
    flat = [(now - 3000 + i * 300, 150.0 + (i % 2) * 0.1) for i in range(10)]
    assert market_regime(flat, now)["label"].startswith("Sidelæns")


def test_performance_groups_real_trades_and_finds_drawdown():
    rows = [
        {"pnl_usd": 2.0, "strategy": "momentum", "source": "dex_boost", "exit_reason": "target", "opened_at": "2026-11-10T09:10:00+00:00"},
        {"pnl_usd": -1.0, "strategy": "momentum", "source": "dex_boost", "exit_reason": "stop_loss", "opened_at": "2026-11-10T09:40:00+00:00"},
        {"pnl_usd": 1.0, "strategy": "sniper", "source": "sniper", "exit_reason": "target", "opened_at": "2026-11-10T09:55:00+00:00"},
        {"pnl_usd": -3.0, "strategy": "sniper", "source": "sniper", "exit_reason": "stop_loss", "opened_at": "2026-11-10T14:00:00+00:00"},
    ]
    equity = [{"equity": 15.0}, {"equity": 18.0}, {"equity": 14.4}]
    result = performance(rows, equity, min_hour_trades=3)
    assert result["overall"]["trades"] == 4 and result["overall"]["pnl"] == -1.0
    assert {item["key"] for item in result["by_strategy"]} == {"momentum", "sniper"}
    assert result["best_hours"][0]["key"] == "9" and result["best_hours"][0]["trades"] == 3
    assert result["current_drawdown_pct"] == 20.0 and result["max_drawdown_pct"] == 20.0
    assert performance([])["overall"]["win_rate"] is None


# --------------------------------------------------------------------------- motoren: scorer, risiko og helbred
import meme_fakes  # noqa: E402
from meme_fakes import make_engine, raw_pair, safe_report  # noqa: E402
from test_meme_guard import _closed  # noqa: E402


def test_new_positions_keep_their_safety_and_ai_scores_and_the_status_has_the_new_views():
    engine, dex, rug, _jup, _notes = make_engine()
    mint = "Scored1111" + "x" * 30
    dex.pairs[mint] = raw_pair(mint)
    dex.boosts.append(mint)
    rug.reports[mint] = safe_report()
    engine.tick(force_scan=True)
    status = engine.status()
    position = status["positions"][0]
    assert 0 <= position["safety_score"]["score"] <= 100 and position["safety_score"]["factors"] is not None
    assert position["ai"]["band"] and position["ai"]["action"] in {"Strong Buy", "Buy", "Hold", "Avoid"}
    assert {"regime", "performance", "risk", "health", "top_ideas"} <= set(status)
    assert {row["key"] for row in status["risk"]["limits"]} >= {"daily", "weekly", "drawdown", "open", "sol"}
    assert status["health"]["items"] and status["health"]["overall"] in {"ok", "warn", "bad"}
    assert status["top_ideas"] == [] or status["top_ideas"][0]["score"] is not None


def test_stale_prices_stop_new_buys_even_on_paper():
    engine, *_ = make_engine(meme_fakes.settings(paper_loss_stops=False))
    engine._sol_usd = (150.0, time.time() - 1000)
    reason = engine._entry_halt("2026-01-01")
    assert "forældede" in reason
    engine._sol_usd = (150.0, time.time())
    assert engine._entry_halt("2026-01-01") == ""


def test_weekly_loss_limit_and_loss_streak_pause_buying_until_resume():
    engine, *_ = make_engine(meme_fakes.settings(weekly_loss_pct=0.1, max_loss_streak=0, max_losses_per_day=0))
    engine._sol_usd = (150.0, time.time())
    assert engine._entry_halt("x") == ""
    _closed(engine, "momentum", [-1.0, -1.0])  # -2 USD af 15 = over 10 %
    assert "Ugens tab" in engine._entry_halt("x")
    engine.resume()
    assert engine._entry_halt("x") == ""
    streak, *_ = make_engine(meme_fakes.settings(weekly_loss_pct=0.0, max_loss_streak=3, loss_cooldown_minutes=30, max_losses_per_day=0))
    streak._sol_usd = (150.0, time.time())
    _closed(streak, "momentum", [-0.1, -0.1])
    assert streak._entry_halt("x") == ""
    _closed(streak, "momentum", [-0.1])
    assert "tab i træk" in streak._entry_halt("x")
    streak.resume()
    assert streak._entry_halt("x") == ""


def test_the_risk_center_shows_losses_as_used_and_profit_as_zero_used():
    engine, *_ = make_engine()
    _closed(engine, "momentum", [2.0])
    used = {row["key"]: row["used"] for row in engine.status()["risk"]["limits"]}
    assert used["daily"] == 0.0 and used["weekly"] == 0.0 and used["drawdown"] == 0.0
    _closed(engine, "momentum", [-1.0, -2.5])
    used = {row["key"]: row["used"] for row in engine.status()["risk"]["limits"]}
    assert used["daily"] == 1.5 and used["drawdown"] == 1.5
