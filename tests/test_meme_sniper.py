"""Sniper: køb helt nye tokens, der består rug-tjek, og sælg ved gevinstmålet."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import meme_fakes
from meme_fakes import make_engine, raw_pair, safe_report
from trader.meme.clients import parse_token_info
from trader.meme.config import SniperSettings, sniper_from
from trader.meme.learning import adapt_sniper
from trader.meme.risk import sniper_exit_decision
from trader.meme.store import MemeStore

NEW = "NewToken1111111111111111111111111111111111"


def _token(mint=NEW, *, age_min=4.0, mint_disabled=True, holders=150, top=18.0, socials=True):
    created = datetime.now(timezone.utc) - timedelta(minutes=age_min)
    raw = {
        "id": mint,
        "symbol": "NEW",
        "name": "New Coin",
        "decimals": 6,
        "holderCount": holders,
        "organicScore": 45,
        "audit": {
            "mintAuthorityDisabled": mint_disabled,
            "freezeAuthorityDisabled": True,
            "topHoldersPercentage": top,
            "devBalancePercentage": 1.0,
        },
        "firstPool": {"createdAt": created.isoformat().replace("+00:00", "Z")},
    }
    if socials:
        raw["twitter"] = "https://x.com/newcoin"
    return parse_token_info(raw)


def _arm(dex, rug, jup, mint=NEW, *, age_min=4.0, **token_kwargs):
    jup.recent.append(_token(mint, age_min=age_min, **token_kwargs))
    dex.pairs[mint] = raw_pair(mint, symbol="NEW", age_min=age_min, liq=20_000, vol_h1=30_000, change_h1=60)
    if not token_kwargs.get("socials", True):
        dex.pairs[mint]["info"] = {}
    rug.reports[mint] = safe_report()


def test_token_info_reads_first_pool_time():
    token = _token(age_min=3)
    assert token.created_at_ms and abs(time.time() * 1000 - token.created_at_ms - 180_000) < 5_000


def test_sniper_buys_new_token_and_sells_at_target():
    engine, dex, rug, jup, notes = make_engine()
    _arm(dex, rug, jup)
    engine.tick(force_scan=True)
    rows = engine.store.open_positions("paper")
    assert len(rows) == 1, engine.store.recent_candidates("paper")
    pos = rows[0]
    assert pos["strategy"] == "sniper" and pos["source"] == "sniper"
    assert pos["cost_usd"] == pytest.approx(1.5)
    assert engine.last_snipe["bought"] == 1
    dex.set_price(NEW, 0.0019)
    engine.tick()
    assert engine.store.get_position(pos["id"])["status"] == "open"  # +90 % er under målet på +100 %
    dex.set_price(NEW, 0.00205)
    engine.tick()
    closed = engine.store.get_position(pos["id"])
    assert closed["status"] == "closed" and closed["exit_reason"] == "target"
    assert closed["pnl_usd"] > 1.0
    view = engine.status()["trades"][0]
    assert view["strategy"] == "sniper" and view["exit_reason"] == "gevinstmålet nået"


def test_sniper_partial_target_lets_the_rest_ride():
    engine, dex, rug, jup, _notes = make_engine()
    engine.update_sniper({"take_profit_pct": 0.5, "sell_fraction": 0.5})
    _arm(dex, rug, jup)
    engine.tick(force_scan=True)
    pos = engine.store.open_positions("paper")[0]
    dex.set_price(NEW, 0.0016)
    engine.tick()
    pos = engine.store.get_position(pos["id"])
    assert pos["status"] == "open" and pos["partial_taken"] == 1
    dex.set_price(NEW, 0.003)
    engine.tick()
    dex.set_price(NEW, 0.002)  # 33 % fra toppen
    engine.tick()
    pos = engine.store.get_position(pos["id"])
    assert pos["status"] == "closed" and pos["exit_reason"] == "trailing" and pos["pnl_usd"] > 0


def test_sniper_stop_loss_uses_sniper_setting():
    engine, dex, rug, jup, _notes = make_engine()
    _arm(dex, rug, jup)
    engine.tick(force_scan=True)
    dex.set_price(NEW, 0.0007)  # -30 %: momentum ville stoppe, sniperen venter til -35 %
    engine.tick()
    assert len(engine.store.open_positions("paper")) == 1
    dex.set_price(NEW, 0.00064)
    engine.tick()
    assert engine.store.closed_positions("paper")[0]["exit_reason"] == "stop_loss"


def test_sniper_waits_for_minimum_age_then_buys():
    engine, dex, rug, jup, _notes = make_engine()
    _arm(dex, rug, jup, age_min=0.3)
    engine.tick(force_scan=True)
    assert engine.store.open_positions("paper") == []
    assert NEW not in (engine.store.get_json("sniper_seen", {}) or {})
    dex.pairs[NEW]["pairCreatedAt"] = int(time.time() * 1000) - 3 * 60_000
    engine.tick(force_scan=True)
    assert len(engine.store.open_positions("paper")) == 1


def test_sniper_rejects_unsafe_and_old_tokens():
    engine, dex, rug, jup, _notes = make_engine()
    _arm(dex, rug, jup, mint="MintAuth11111111111111111111111111111111111", mint_disabled=False)
    _arm(dex, rug, jup, mint="Lonely111111111111111111111111111111111111", holders=6)
    _arm(dex, rug, jup, mint="NoSocial11111111111111111111111111111111111", socials=False)
    _arm(dex, rug, jup, mint="OldToken11111111111111111111111111111111111", age_min=90)
    engine.tick(force_scan=True)
    assert engine.store.open_positions("paper") == []
    reasons = {row["mint"][:8]: row["reason"] for row in engine.store.recent_candidates("paper")}
    assert "Mint authority" in reasons["MintAuth"]
    assert "holdere" in reasons["Lonely11"]
    assert "Ingen X" in reasons["NoSocial"]
    assert "OldToken" not in reasons  # for gamle tokens springes stille over
    # Set tokens tjekkes ikke igen hver runde.
    engine.tick(force_scan=True)
    assert len(engine.store.recent_candidates("paper")) == len(reasons)


def test_sniper_respects_rugcheck_and_limits():
    engine, dex, rug, jup, _notes = make_engine(meme_fakes.settings(sniper_max_open=1))
    _arm(dex, rug, jup)
    rug.reports[NEW] = safe_report(freezeAuthority="Dev111")
    second = "Second11111111111111111111111111111111111"
    third = "Third111111111111111111111111111111111111"
    _arm(dex, rug, jup, mint=second)
    _arm(dex, rug, jup, mint=third)
    engine.tick(force_scan=True)
    rows = engine.store.open_positions("paper")
    assert [row["mint"] for row in rows] == [second] or [row["mint"] for row in rows] == [third]
    stages = {row["mint"]: row["stage"] for row in engine.store.recent_candidates("paper")}
    assert stages[NEW] == "rug"
    assert "risiko" in stages.values()  # den tredje stoppes af maks. 1 åben snipe


def test_sniper_obeys_pause_and_can_be_turned_off():
    engine, dex, rug, jup, _notes = make_engine()
    engine.pause(True)
    _arm(dex, rug, jup)
    engine.tick(force_scan=True)
    assert engine.store.open_positions("paper") == []
    engine.pause(False)
    engine.update_sniper({"enabled": False})
    engine.tick(force_scan=True)
    assert engine.store.open_positions("paper") == []
    assert engine._loop_seconds() == engine.settings.monitor_seconds
    engine.update_sniper({"enabled": True})
    assert engine._loop_seconds() == 5
    engine.tick(force_scan=True)
    assert len(engine.store.open_positions("paper")) == 1


def test_sniper_settings_are_clamped_and_capped():
    engine, *_ = make_engine(meme_fakes.settings(position_usd=2.0))
    engine.update_sniper({"take_profit_pct": 50, "stop_loss_pct": 0.01, "position_usd": 9, "min_holders": 3.6, "ukendt": 1})
    cfg = engine.sniper
    assert cfg.take_profit_pct == 10.0 and cfg.stop_loss_pct == 0.1
    assert cfg.position_usd == 2.0  # aldrig over MEME_POSITION_USD
    assert cfg.min_holders == 5
    with pytest.raises(ValueError):
        engine.update_sniper({"take_profit_pct": "meget"})
    restored = sniper_from(engine.store.get_json("sniper"), engine.settings)
    assert restored.take_profit_pct == 10.0
    engine.reset_sniper()
    assert engine.sniper.take_profit_pct == SniperSettings().take_profit_pct
    fields = {field["name"]: field for field in engine.status()["sniper"]["fields"]}
    assert fields["position_usd"]["bounds"][1] == 2.0


def test_sniper_exit_rules():
    cfg = SniperSettings()
    common = dict(minutes_held=5, partial_taken=False, liquidity_entry=20_000, liquidity_now=20_000, cfg=cfg)
    assert not sniper_exit_decision(ratio=1.5, peak_ratio=1.5, **common).sell
    assert sniper_exit_decision(ratio=2.0, peak_ratio=2.0, **common).kind == "target"
    assert sniper_exit_decision(ratio=0.64, peak_ratio=1.0, **common).kind == "stop_loss"
    assert sniper_exit_decision(ratio=1.0, peak_ratio=1.0, **{**common, "liquidity_now": 5_000}).kind == "rug_liquidity"
    assert sniper_exit_decision(ratio=1.2, peak_ratio=1.3, **{**common, "minutes_held": 61}).kind == "time"


def test_adapt_sniper_tightens_after_rugs():
    cfg = SniperSettings()
    trades = [{"exit_reason": "rug_liquidity", "pnl_usd": -1.0}] * 3 + [{"exit_reason": "target", "pnl_usd": 1.5}] * 2
    notes = adapt_sniper(cfg, trades)
    assert notes and cfg.min_liquidity_usd > SniperSettings().min_liquidity_usd
    assert cfg.take_profit_pct == SniperSettings().take_profit_pct  # målet er dit, ikke læringens


def test_old_database_gets_strategy_column(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY, mode TEXT NOT NULL, mint TEXT NOT NULL, symbol TEXT NOT NULL, name TEXT, source TEXT NOT NULL, status TEXT NOT NULL, opened_at TEXT NOT NULL, closed_at TEXT, entry_price_usd REAL NOT NULL, cost_usd REAL NOT NULL, token_raw INTEGER NOT NULL, remaining_raw INTEGER NOT NULL, sol_spent REAL, fees_usd REAL NOT NULL DEFAULT 0, proceeds_usd REAL NOT NULL DEFAULT 0, peak_ratio REAL NOT NULL DEFAULT 1, last_ratio REAL NOT NULL DEFAULT 1, last_price_usd REAL, partial_taken INTEGER NOT NULL DEFAULT 0, liquidity_entry REAL, liquidity_last REAL, pnl_usd REAL, pnl_pct REAL, exit_reason TEXT, score REAL, features TEXT, safety TEXT, ai TEXT, reasoning TEXT, pair_url TEXT, signatures TEXT, exit_failures INTEGER NOT NULL DEFAULT 0, learned INTEGER NOT NULL DEFAULT 0)")
    conn.execute("INSERT INTO positions (mode, mint, symbol, source, status, opened_at, entry_price_usd, cost_usd, token_raw, remaining_raw) VALUES ('paper', 'M', 'OLD', 'dex_boost', 'open', '2026-11-01T00:00:00+00:00', 1, 3, 10, 10)")
    conn.commit()
    conn.close()
    store = MemeStore(str(path))
    assert store.open_positions("paper")[0]["strategy"] == "momentum"


def test_sniper_api_route(tmp_path, monkeypatch):
    import dataclasses

    from trader.app import create_app
    from trader.config import Settings
    from trader.db import Database
    from trader.engine import TradingEngine

    engine, *_ = make_engine()
    monkeypatch.setattr(engine, "start", lambda: None)
    monkeypatch.setattr(engine, "stop", lambda: None)
    settings = dataclasses.replace(Settings.from_env(), db_path=str(tmp_path / "t.db"), host="127.0.0.1", app_username="", app_password="", app_password_hash="", session_secret="")
    stock = TradingEngine(settings=settings, db=Database(settings.db_path))
    with TestClient(create_app(engine=stock, settings=settings, meme_engine=engine)) as client:
        body = client.post("/api/meme/sniper", json={"take_profit_pct": 2.0, "enabled": False}).json()
        assert body["sniper"]["settings"]["take_profit_pct"] == 2.0 and body["sniper"]["enabled"] is False
        assert client.post("/api/meme/sniper", json={"stop_loss_pct": "x"}).status_code == 400
        reset = client.post("/api/meme/sniper", json={"reset": True}).json()
        assert reset["sniper"]["settings"]["take_profit_pct"] == 1.0
