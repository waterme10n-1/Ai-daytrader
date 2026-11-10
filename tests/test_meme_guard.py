"""Tab mindre: strategi-vagt, automatisk mål for sniperen og billigere handler."""

from __future__ import annotations

import time

import meme_fakes
from meme_fakes import make_engine, raw_pair, safe_report
from trader.meme.config import StrategyParams
from trader.meme.store import now_iso


def _closed(engine, strategy: str, pnls: list[float]) -> None:
    for index, pnl in enumerate(pnls):
        position_id = engine.store.open_position(
            {
                "strategy": strategy,
                "mode": "paper",
                "mint": f"{strategy}{index}" + "x" * 30,
                "symbol": f"S{index}",
                "source": "sniper" if strategy == "sniper" else "dex_boost",
                "status": "open",
                "opened_at": now_iso(),
                "entry_price_usd": 1.0,
                "cost_usd": 1.5,
                "token_raw": 1000,
                "remaining_raw": 1000,
                "fees_usd": 0.01,
            }
        )
        engine.store.update_position(position_id, status="closed", closed_at=now_iso(), pnl_usd=pnl, pnl_pct=pnl / 1.5, exit_reason="stop_loss", remaining_raw=0, learned=True)


def test_guard_is_on_by_default_and_can_be_switched_off():
    engine, *_ = make_engine()
    assert engine.guard_on is True
    engine.set_strategy({"guard": False})
    _closed(engine, "sniper", [-0.5] * 10)
    engine._check_guard()
    assert engine._holds() == {}


def test_guard_puts_a_losing_strategy_on_hold_and_keeps_shadowing():
    engine, dex, rug, jup, _notes = make_engine(meme_fakes.settings(strategy_guard=True, max_losses_per_day=0))  # tabsstoppet testes for sig
    _closed(engine, "sniper", [-0.5] * 7 + [0.2])
    engine.tick()
    strategies = {row["id"]: row for row in engine.status()["strategies"]}
    assert strategies["sniper"]["hold"] is not None and strategies["sniper"]["hold"]["auto"] is True
    assert strategies["momentum"]["hold"] is None
    assert any("Sniper er på hold" in alert["text"] for alert in engine.status()["alerts"])

    # En ny token, der ellers ville blive købt, skrives ned i stedet, så skyggehandlerne lærer videre.
    from test_meme_sniper import NEW, _arm

    _arm(dex, rug, jup)
    engine.tick(force_scan=True)
    assert engine.store.open_positions("paper") == []
    reasons = [row["reason"] for row in engine.store.recent_candidates("paper") if row["mint"] == NEW]
    assert reasons and "på hold" in reasons[0]


def test_hold_expires_doubles_next_time_and_resets_on_profit():
    engine, *_ = make_engine(meme_fakes.settings(strategy_guard=True))
    _closed(engine, "momentum", [-0.4] * 8)
    engine._check_guard()
    hold = engine._holds()["momentum"]
    assert hold["hours"] == 24
    hold["until"] = time.time() - 1  # pausen er ovre
    engine._save_holds({"momentum": hold})
    engine._check_guard()
    assert "momentum" not in engine._holds()
    # De gamle tab tæller ikke længere: kun nye handler efter pausen bedømmes.
    engine._check_guard()
    assert "momentum" not in engine._holds()
    time.sleep(1.1)  # nye handler skal lukke efter pausen
    _closed(engine, "momentum", [-0.4] * 8)
    engine._check_guard()
    assert engine._holds()["momentum"]["hours"] == 48  # anden gang i træk: dobbelt så lang pause


def test_manual_hold_resume_and_switches(tmp_path):
    import dataclasses

    from fastapi.testclient import TestClient

    from trader.app import create_app
    from trader.config import Settings

    engine, *_ = make_engine()
    engine.start = lambda: None
    engine.stop = lambda: None
    settings = dataclasses.replace(Settings(), db_path=str(tmp_path / "t.db"))
    with TestClient(create_app(settings=settings, meme_engine=engine)) as client:
        body = client.post("/api/meme/strategies", json={"hold": "toptrader"}).json()
        top = {row["id"]: row for row in body["strategies"]}["toptrader"]
        assert top["hold"] == {"reason": "Sat på hold af dig.", "auto": False, "hours_left": None}
        body = client.post("/api/meme/strategies", json={"resume": "toptrader"}).json()
        assert {row["id"]: row for row in body["strategies"]}["toptrader"]["hold"] is None
        body = client.post("/api/meme/strategies", json={"guard": False, "auto_targets": False}).json()
        assert body["strategy_guard"] is False and body["auto_targets"] is False
        assert client.post("/api/meme/strategies", json={"hold": "noget"}).status_code == 400

    _closed(engine, "sniper", [-0.5] * 10)
    engine._check_guard()
    assert engine._holds() == {}  # vagten er slået fra


def test_sniper_learns_a_better_target_only_when_it_clearly_pays():
    engine, *_ = make_engine(meme_fakes.settings(shadows=True))
    result = {"n": 50, "best": {"tp": 0.5, "sl": 0.25, "avg": 0.06}, "current": {"tp": 1.0, "sl": 0.35, "avg": -0.03}}
    engine._optimizer = lambda: result
    engine._maybe_auto_targets()
    assert (engine.sniper.take_profit_pct, engine.sniper.stop_loss_pct) == (0.5, 0.25)
    assert "Sniperens mål er ændret" in engine.store.recent_lessons(5)[0]["text"]

    engine.sniper.take_profit_pct, engine.sniper.stop_loss_pct = 1.0, 0.35
    engine._last_auto_target_check = 0
    engine._maybe_auto_targets()
    assert engine.sniper.take_profit_pct == 1.0  # højst én gang i døgnet

    fresh, *_ = make_engine(meme_fakes.settings(shadows=True))
    fresh._optimizer = lambda: {"n": 50, "best": {"tp": 0.5, "sl": 0.25, "avg": -0.01}, "current": {"tp": 1.0, "sl": 0.35, "avg": -0.05}}
    fresh._maybe_auto_targets()
    assert fresh.sniper.take_profit_pct != 0.5  # intet sæt giver overskud: intet skift (vagten tager sig af det)
    few, *_ = make_engine(meme_fakes.settings(shadows=True))
    few._optimizer = lambda: {**result, "n": 25}
    few._maybe_auto_targets()
    assert few.sniper.take_profit_pct != 0.5  # for få forløb


def test_round_trip_limit_is_unchanged():
    assert StrategyParams().max_roundtrip_loss_pct == 8.0
    assert StrategyParams.from_dict({"max_roundtrip_loss_pct": 12.0}).max_roundtrip_loss_pct == 12.0


def test_top_10_trades_do_not_move_the_momentum_parameters():
    engine, *_ = make_engine()
    before = engine.params.to_dict()
    for index in range(10):
        position_id = engine.store.open_position(
            {"strategy": "toptrader", "mode": "paper", "mint": f"Top{index}" + "y" * 30, "symbol": "T", "source": "copy:W", "status": "open",
             "opened_at": now_iso(), "entry_price_usd": 1.0, "cost_usd": 2.0, "token_raw": 1000, "remaining_raw": 1000, "fees_usd": 0.01,
             "features": {"liquidity": 0.5}}
        )
        engine.store.update_position(position_id, status="closed", closed_at=now_iso(), pnl_usd=-0.6, pnl_pct=-0.3, exit_reason="stop_loss", remaining_raw=0)
    engine._learn()
    assert engine.params.to_dict() == before
