"""Hele motoren på papir med falske datakilder: køb, delsalg, trailing stop, læring, rug og AI-veto."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import meme_fakes
from meme_fakes import make_engine, raw_pair, safe_report
from trader.config import Settings
from trader.meme.config import MEME_LIVE_PHRASE
from trader.meme.copytrade import CopyTracker
from trader.meme.research import Verdict

MINT = "GoodMint1111"


def _arm(dex, rug, mint=MINT, **pair_kwargs):
    dex.pairs[mint] = raw_pair(mint, **pair_kwargs)
    dex.boosts.append(mint)
    rug.reports[mint] = safe_report()


def test_paper_round_trip_buys_takes_profit_trails_and_learns():
    engine, dex, rug, jup, notes = make_engine()
    _arm(dex, rug)
    engine.tick(force_scan=True)
    open_rows = engine.store.open_positions("paper")
    assert len(open_rows) == 1, engine.store.recent_candidates("paper")
    pos = open_rows[0]
    assert pos["source"] == "dex_boost"
    assert pos["cost_usd"] == pytest.approx(3.0)
    assert engine._paper.cash_usd(150) < 12.1

    dex.set_price(MINT, 0.0017)
    engine.tick()
    pos = engine.store.get_position(pos["id"])
    assert pos["partial_taken"] == 1 and pos["status"] == "open"
    assert pos["remaining_raw"] < pos["token_raw"]

    dex.set_price(MINT, 0.002)
    engine.tick()
    dex.set_price(MINT, 0.0014)
    engine.tick()
    pos = engine.store.get_position(pos["id"])
    assert pos["status"] == "closed"
    assert pos["exit_reason"] == "trailing"
    assert pos["pnl_usd"] > 0
    # Læringen har kørt og gemt en lektion.
    assert engine.model.updates == 1
    assert any(row["kind"] == "handel" for row in engine.store.recent_lessons())
    # Egenkapital = bankroll + realiseret resultat, og papirkassen stemmer.
    totals = engine._totals()
    assert totals["equity"] == pytest.approx(15.0 + pos["pnl_usd"])
    assert engine._paper.cash_usd(150) == pytest.approx(totals["equity"], abs=1e-6)
    assert any("Købt" in text for text in notes)
    status = engine.status()
    assert status["kpis"]["trades"] == 1
    assert status["trades"][0]["exit_reason"] == "trailing stop"


def test_stop_loss_and_no_rebuy_after_loss():
    engine, dex, rug, jup, _notes = make_engine()
    _arm(dex, rug)
    engine.tick(force_scan=True)
    dex.set_price(MINT, 0.0007)
    engine.tick()
    closed = engine.store.closed_positions("paper")
    assert closed and closed[0]["exit_reason"] == "stop_loss" and closed[0]["pnl_usd"] < 0
    dex.set_price(MINT, 0.001)
    engine.tick(force_scan=True)
    assert engine.store.open_positions("paper") == []


def test_rug_flags_block_the_buy_and_are_explained():
    engine, dex, rug, _jup, _notes = make_engine()
    _arm(dex, rug)
    rug.reports[MINT] = safe_report(mintAuthority="Dev1111")
    engine.tick(force_scan=True)
    assert engine.store.open_positions("paper") == []
    row = engine.store.recent_candidates("paper")[0]
    assert row["stage"] == "rug" and "Mint authority" in row["reason"]


def test_missing_rugcheck_means_no_trade():
    engine, dex, rug, _jup, _notes = make_engine()
    _arm(dex, rug)
    rug.reports.clear()
    engine.tick(force_scan=True)
    assert engine.store.open_positions("paper") == []


def test_liquidity_pull_triggers_rug_exit_and_blacklist():
    engine, dex, rug, jup, _notes = make_engine()
    _arm(dex, rug)
    engine.tick(force_scan=True)
    dex.set_price(MINT, 0.0009, liq=10_000)
    engine.tick()
    closed = engine.store.closed_positions("paper")
    assert closed[0]["exit_reason"] == "rug_liquidity"
    assert engine.store.ever_rugged(MINT)


def test_unsellable_position_is_written_off_after_retries():
    engine, dex, rug, jup, _notes = make_engine()
    _arm(dex, rug)
    engine.tick(force_scan=True)
    jup.no_route.add(MINT)
    dex.set_price(MINT, 0.0005)
    for _ in range(6):
        engine.tick()
    closed = engine.store.closed_positions("paper")
    assert closed and closed[0]["exit_reason"] == "rug_no_route"
    assert closed[0]["pnl_usd"] < -2.9


class SkipResearcher:
    def __init__(self):
        self.calls = 0

    def available(self, estimate=0.2):
        return True

    def budget_left(self):
        return 1.0

    def research(self, payload, lessons):
        self.calls += 1
        assert payload["mint"] == MINT and "rugcheck" in payload
        return Verdict("skip", 80, ["Kopi af et kendt token"], [], "", "Ligner en kopi.", 0.05)

    def daily_review(self, trades, params):
        return None


def test_claude_can_veto_but_not_force_a_buy():
    researcher = SkipResearcher()
    engine, dex, rug, _jup, _notes = make_engine(researcher=researcher)
    _arm(dex, rug)
    engine.tick(force_scan=True)
    assert researcher.calls == 1
    assert engine.store.open_positions("paper") == []
    row = engine.store.recent_candidates("paper")[0]
    assert row["stage"] == "claude" and "Kopi" in row["reason"]


def test_risk_limits_cap_open_positions_and_kill_sells_everything():
    engine, dex, rug, _jup, notes = make_engine(meme_fakes.settings(max_open=2))
    for index in range(4):
        _arm(dex, rug, mint=f"Mint{index}xxxx", symbol=f"M{index}")
    engine.tick(force_scan=True)
    engine.tick(force_scan=True)
    assert len(engine.store.open_positions("paper")) == 2
    engine.kill()
    assert engine.store.open_positions("paper") == []
    assert all(row["exit_reason"] == "kill" for row in engine.store.closed_positions("paper"))
    engine.tick()
    assert engine.model.updates == 0  # nødstop lærer ikke modellen noget
    engine.tick(force_scan=True)
    assert engine.store.open_positions("paper") == []
    engine.resume()
    assert not engine.kill_switch


def test_live_stays_locked_without_paper_record_and_phrase():
    engine, *_ = make_engine(meme_fakes.settings(allow_live=True))
    ok, message = engine.request_live("forkert")
    assert not ok
    ok, message = engine.request_live(MEME_LIVE_PHRASE)
    assert not ok and "lukkede papirhandler" in message
    assert engine.mode == "paper"
    items = {item["id"]: item for item in engine.status()["unlock"]["items"]}
    assert items["wallet"]["ok"] is False


class FakeRPC:
    def __init__(self, wallet):
        self.wallet = wallet
        self.sigs: list[dict] = [{"signature": "old", "blockTime": int(time.time()) - 60, "err": None}]
        self.txs: dict[str, dict] = {}

    def signatures_for(self, address, limit=15):
        return list(self.sigs)

    def transaction(self, signature):
        return self.txs[signature]

    def close(self):
        pass

    def push(self, signature, sol_pre, sol_post, pre, post):
        self.sigs.insert(0, {"signature": signature, "blockTime": int(time.time()), "err": None})
        self.txs[signature] = {
            "blockTime": int(time.time()),
            "meta": {
                "err": None,
                "preBalances": [sol_pre],
                "postBalances": [sol_post],
                "preTokenBalances": [{"owner": self.wallet, "mint": m, "uiTokenAmount": {"amount": str(a)}} for m, a in pre.items()],
                "postTokenBalances": [{"owner": self.wallet, "mint": m, "uiTokenAmount": {"amount": str(a)}} for m, a in post.items()],
            },
            "transaction": {"message": {"accountKeys": [{"pubkey": self.wallet, "signer": True}]}},
        }


def test_copy_trading_follows_buys_through_filters_and_exits_on_their_sell():
    whale = "Whale1111111111111111111111111111111111111"
    rpc = FakeRPC(whale)
    engine, dex, rug, _jup, _notes = make_engine(meme_fakes.settings(copy_wallets=(whale,)), rpc=rpc)
    dex.pairs[MINT] = raw_pair(MINT)
    rug.reports[MINT] = safe_report()
    engine.tick(force_scan=True)  # første kig husker kun position i historikken
    assert engine.store.open_positions("paper") == []
    rpc.push("buy1", 10_000_000_000, 8_000_000_000, {}, {MINT: 10_000})
    engine.tick(force_scan=True)
    open_rows = engine.store.open_positions("paper")
    assert len(open_rows) == 1 and open_rows[0]["source"] == f"copy:{whale}"
    rpc.push("sell1", 8_000_000_000, 9_500_000_000, {MINT: 10_000}, {})
    engine.tick(force_scan=True)  # scanning ser salget
    engine.tick()  # overvågningen sælger
    closed = engine.store.closed_positions("paper")
    assert closed and closed[0]["exit_reason"] == "copy_exit"


def test_copy_tracker_ignores_history_on_first_look(tmp_path):
    from trader.meme.store import MemeStore

    rpc = FakeRPC("W")
    rpc.push("buy0", 10_000_000_000, 9_000_000_000, {}, {"X": 1})
    tracker = CopyTracker(rpc, MemeStore(":memory:"), ("W",))
    assert tracker.poll() == []
    rpc.push("buy1", 9_000_000_000, 8_000_000_000, {}, {"Y": 1})
    assert [s.mint for s in tracker.poll()] == ["Y"]


def test_dashboard_routes(tmp_path, monkeypatch):
    engine, dex, rug, _jup, _notes = make_engine()
    monkeypatch.setattr(engine, "start", lambda: None)
    monkeypatch.setattr(engine, "stop", lambda: None)
    _arm(dex, rug)
    engine.tick(force_scan=True)
    settings = Settings.from_env()
    import dataclasses

    settings = dataclasses.replace(settings, db_path=str(tmp_path / "t.db"), host="127.0.0.1", app_username="", app_password="", app_password_hash="", session_secret="")
    from trader.app import create_app

    with TestClient(create_app(settings=settings, meme_engine=engine)) as client:
        page = client.get("/meme")
        assert page.status_code == 200 and "Memebot" in page.text
        body = client.get("/api/meme/status").json()
        assert body["mode"] == "paper" and len(body["positions"]) == 1
        position_id = body["positions"][0]["id"]
        sold = client.post("/api/meme/sell", json={"id": position_id}).json()
        assert sold["positions"] == [] and sold["trades"][0]["exit_reason"] == "solgt manuelt"
        assert client.post("/api/meme/live/unlock", json={"phrase": "nej"}).status_code == 400
        assert client.post("/api/meme/kill").json()["kill_switch"] is True
        assert client.post("/api/meme/resume").json()["kill_switch"] is False


def test_daily_loss_halts_new_entries():
    engine, dex, rug, _jup, notes = make_engine(meme_fakes.settings(daily_loss_pct=0.1, position_usd=3.0))
    _arm(dex, rug)
    engine.tick(force_scan=True)
    dex.set_price(MINT, 0.0007)
    engine.tick()  # stop-loss: ca. -0,9 USD, under 10 % af 15 = 1,5 USD
    _arm(dex, rug, mint="Second11111", symbol="TWO")
    engine.tick(force_scan=True)
    dex.set_price("Second11111", 0.0007)
    engine.tick()
    assert engine.status()["halted_day"] is True
    _arm(dex, rug, mint="Third111111", symbol="THREE")
    engine.tick(force_scan=True)
    assert engine.store.open_positions("paper") == []


def test_pause_blocks_new_buys_but_keeps_managing_positions():
    engine, dex, rug, _jup, _notes = make_engine()
    _arm(dex, rug)
    engine.tick(force_scan=True)
    assert len(engine.store.open_positions("paper")) == 1
    engine.pause(True)
    _arm(dex, rug, mint="Second11111", symbol="TWO")
    engine.tick(force_scan=True)
    assert len(engine.store.open_positions("paper")) == 1
    dex.set_price(MINT, 0.0007)
    engine.tick()
    assert engine.store.open_positions("paper") == []  # stop-loss virker stadig under pause
    assert engine.status()["paused"] is True
    engine.pause(False)
    engine.tick(force_scan=True)
    assert [row["mint"] for row in engine.store.open_positions("paper")] == ["Second11111"]


def test_wallets_and_blacklist_from_dashboard():
    from solders.keypair import Keypair

    engine, dex, rug, _jup, _notes = make_engine()
    good = str(Keypair().pubkey())
    with pytest.raises(ValueError):
        engine.add_wallet("ikke-en-adresse")
    engine.add_wallet(good)
    with pytest.raises(ValueError):
        engine.add_wallet(good)
    wallets = engine.status()["copy_wallets"]
    assert wallets[0]["wallet"] == good and wallets[0]["origin"] == "ui"
    assert engine.copy.wallets == (good,) and engine.clients.rpc is not None
    engine.remove_wallet(good)
    assert engine.status()["copy_wallets"] == []

    token = str(Keypair().pubkey())
    _arm(dex, rug, mint=token, symbol="BAD")
    engine.block_token(token, "BAD")
    engine.tick(force_scan=True)
    assert engine.store.open_positions("paper") == []
    assert engine.status()["blacklist"][0]["mint"] == token
    engine.unblock_token(token)
    engine.tick(force_scan=True)
    assert len(engine.store.open_positions("paper")) == 1


def test_status_has_chart_data_and_levels():
    engine, dex, rug, _jup, _notes = make_engine()
    _arm(dex, rug)
    engine.tick(force_scan=True)
    dex.set_price(MINT, 0.0007)
    engine.tick()
    status = engine.status()
    assert status["equity"]["24h"][-1]["equity"] == pytest.approx(status["kpis"]["equity"])
    assert status["daily_pnl"][0]["trades"] == 1
    assert status["exits"][0]["kind"] == "stop_loss" and status["exits"][0]["count"] == 1
    assert status["kpis"]["streak"] == {"kind": "loss", "count": 1}
    assert status["kpis"]["avg_hold_minutes"] is not None
    assert status["levels"]["stop_loss_pct"] == pytest.approx(0.25)
    funnel = status["engine"]["last_scan"]
    assert funnel["rug_ok"] >= funnel["scored"]


def test_status_is_gzip_compressed(tmp_path, monkeypatch):
    import dataclasses

    from trader.app import create_app

    engine, dex, rug, _jup, _notes = make_engine()
    monkeypatch.setattr(engine, "start", lambda: None)
    monkeypatch.setattr(engine, "stop", lambda: None)
    for index in range(30):
        _arm(dex, rug, mint=f"Mint{index}" + "z" * 30, symbol=f"T{index}")
    engine.tick(force_scan=True)
    settings = dataclasses.replace(Settings.from_env(), db_path=str(tmp_path / "t.db"), host="127.0.0.1", app_username="", app_password="", app_password_hash="", session_secret="")
    with TestClient(create_app(settings=settings, meme_engine=engine)) as client:
        response = client.get("/api/meme/status", headers={"Accept-Encoding": "gzip"})
        assert response.status_code == 200
        assert response.headers.get("content-encoding") == "gzip"
        assert response.json()["mode"] == "paper"
