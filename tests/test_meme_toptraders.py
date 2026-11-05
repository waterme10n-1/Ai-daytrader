"""Følg top-traders: hent dagens bedste, køb lige efter dem, og sælg når de sælger."""

from __future__ import annotations

import time

import httpx
import pytest

import meme_fakes
from meme_fakes import make_engine, raw_pair, safe_report
from trader.meme.clients import SolanaTracker, SourceError, TopTrader, describe_shape, parse_top_traders
from trader.meme.config import TopTraderSettings
from trader.meme.risk import top_exit_decision

A = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
B = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
OUTSIDER = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
MINT = "CopyMint11111111111111111111111111111111111"


class ChainRPC:
    """Flere wallets på en falsk kæde: hver har sin egen historik af transaktioner."""

    def __init__(self) -> None:
        self.sigs: dict[str, list[dict]] = {}
        self.txs: dict[str, dict] = {}
        self.calls = 0

    def signatures_for(self, address, limit=15):
        self.calls += 1
        return list(self.sigs.get(address, [{"signature": f"start-{address[:4]}", "blockTime": int(time.time()) - 60, "err": None}]))

    def transaction(self, signature):
        return self.txs[signature]

    def close(self):
        pass

    def swap(self, wallet, signature, *, sol_before, sol_after, before, after, age=0):
        stamp = int(time.time()) - age
        self.sigs.setdefault(wallet, [{"signature": f"start-{wallet[:4]}", "blockTime": stamp - 60, "err": None}])
        self.sigs[wallet].insert(0, {"signature": signature, "blockTime": stamp, "err": None})
        self.txs[signature] = {
            "blockTime": stamp,
            "meta": {
                "err": None,
                "preBalances": [sol_before],
                "postBalances": [sol_after],
                "preTokenBalances": [{"owner": wallet, "mint": m, "uiTokenAmount": {"amount": str(a)}} for m, a in before.items()],
                "postTokenBalances": [{"owner": wallet, "mint": m, "uiTokenAmount": {"amount": str(a)}} for m, a in after.items()],
            },
            "transaction": {"message": {"accountKeys": [{"pubkey": wallet, "signer": True}]}},
        }

    def buy(self, wallet, signature, sol=2.0, mint=MINT, age=0):
        self.swap(wallet, signature, sol_before=10_000_000_000, sol_after=int((10 - sol) * 1e9), before={}, after={mint: 50_000}, age=age)

    def sell(self, wallet, signature, mint=MINT):
        self.swap(wallet, signature, sol_before=8_000_000_000, sol_after=9_900_000_000, before={mint: 50_000}, after={})


class FakeTop:
    def __init__(self, wallets):
        self.wallets = list(wallets)
        self.calls = 0

    def top_traders(self, days=1, limit=10):
        self.calls += 1
        return [TopTrader(wallet=w, rank=i + 1, pnl_usd=5_000.0 - i * 1_000, win_rate=0.6, trades=40) for i, w in enumerate(self.wallets[:limit])]

    def close(self):
        pass


def _engine(top=(A, B), **overrides):
    rpc = ChainRPC()
    engine, dex, rug, jup, notes = make_engine(meme_fakes.settings(**overrides), rpc=rpc)
    engine.clients.top = FakeTop(top) if top is not None else None
    engine.update_sniper({"enabled": False})
    dex.pairs[MINT] = raw_pair(MINT, symbol="COPY")
    rug.reports[MINT] = safe_report()
    return engine, rpc, dex, rug


def _poll(engine):
    engine._last_top_poll = 0.0
    engine.tick()


# --------------------------------------------------------------------------- leaderboardet


def test_parse_top_traders_reads_several_shapes():
    rows = parse_top_traders({"data": {"traders": [{"wallet": A, "pnl": {"realized": 1234.5}, "winRate": 63, "trades": 41}, {"wallet": "ikke-en-wallet"}]}})
    assert [(t.wallet, t.rank, t.pnl_usd, t.win_rate, t.trades) for t in rows] == [(A, 1, 1234.5, 0.63, 41)]
    rows = parse_top_traders([{"address": B, "realizedPnl": "99.5", "win_rate": 0.4, "stats": {"buys": 3, "sells": 2}}, {"address": B}])
    assert len(rows) == 1 and rows[0].pnl_usd == 99.5 and rows[0].win_rate == 0.4
    assert describe_shape({"foo": 1}) == "nøgler: foo"


def test_solanatracker_client_asks_for_today_with_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(path=request.url.path, days=request.url.params["days"], key=request.headers.get("x-api-key"))
        if request.url.params["limit"] == "2":
            return httpx.Response(200, json={"traders": []})
        return httpx.Response(200, json={"traders": [{"wallet": A, "totalPnl": 900}, {"wallet": B, "totalPnl": 400}]})

    cfg = meme_fakes.settings(solanatracker_api_key="st-test")
    client = SolanaTracker(cfg, transport=httpx.MockTransport(handler))
    traders = client.top_traders(days=1, limit=10)
    assert [t.wallet for t in traders] == [A, B]
    assert seen == {"path": "/v2/pnl/leaderboard/top", "days": "1", "key": "st-test"}
    with pytest.raises(SourceError, match="ingen wallets"):
        client.top_traders(days=1, limit=1)
    client.close()


# --------------------------------------------------------------------------- køb og salg


def test_buys_right_after_a_top_trader_and_sells_when_they_sell():
    engine, rpc, dex, _rug = _engine()
    engine.update_toptraders({"enabled": True, "position_usd": 2.0})
    _poll(engine)  # første kig: husk kun, hvor de er i deres historik
    assert engine.store.open_positions("paper") == []
    assert engine.clients.top.calls == 1

    rpc.buy(A, "a-buy-1")
    _poll(engine)
    rows = engine.store.open_positions("paper")
    assert len(rows) == 1
    pos = rows[0]
    assert pos["strategy"] == "toptrader" and pos["source"] == f"copy:{A}"
    assert pos["cost_usd"] == pytest.approx(2.0)
    assert "nr. 1" in pos["reasoning"] and "Sælger, når traderen sælger" in pos["reasoning"]
    status = engine.status()["toptraders"]
    assert status["signals"][0]["action"] == "købt"
    assert status["leaders"][0]["holding"] is True
    view = [p for p in engine.status()["positions"] if p["id"] == pos["id"]][0]
    assert view["tp_pct"] is None and view["stop_pct"] == pytest.approx(-0.3)  # stop-loss som standard

    rpc.sell(A, "a-sell-1")
    _poll(engine)  # salget ses og udføres i samme runde
    closed = engine.store.closed_positions("paper")
    assert closed and closed[0]["exit_reason"] == "copy_exit"
    assert engine.status()["toptraders"]["stats"]["followed_exits"] == 1
    assert engine.clients.top.calls == 1  # listen hentes kun hver halve time


def test_skips_old_small_unknown_and_unsafe_buys():
    engine, rpc, dex, rug = _engine()
    engine.update_toptraders({"enabled": True, "max_signal_age_seconds": 60, "min_leader_sol": 0.5})
    _poll(engine)
    rpc.buy(A, "old", age=300)
    rpc.buy(B, "tiny", sol=0.1)
    rpc.buy(OUTSIDER, "outsider")  # ikke på listen: ignoreres helt
    _poll(engine)
    assert engine.store.open_positions("paper") == []
    details = [s["detail"] for s in engine.status()["toptraders"]["signals"]]
    assert any("gammelt" in d for d in details) and any("Lille køb" in d for d in details)

    risky = "RiskyMint1111111111111111111111111111111111"
    dex.pairs[risky] = raw_pair(risky, symbol="RISK")
    rug.reports[risky] = safe_report(mintAuthority="Dev111")
    rpc.buy(A, "risky", mint=risky)
    _poll(engine)
    assert engine.store.open_positions("paper") == []
    assert engine.status()["toptraders"]["signals"][0]["detail"].startswith("Rug-tjek")


def test_pause_and_limits_apply_to_copies():
    engine, rpc, dex, rug = _engine()
    engine.update_toptraders({"enabled": True, "max_open": 1})
    _poll(engine)
    engine.pause(True)
    rpc.buy(A, "while-paused")
    _poll(engine)
    assert engine.store.open_positions("paper") == []
    assert "pause" in engine.status()["toptraders"]["signals"][0]["detail"]
    engine.pause(False)
    second = "SecondMint111111111111111111111111111111111"
    dex.pairs[second] = raw_pair(second, symbol="TWO")
    rug.reports[second] = safe_report()
    rpc.buy(A, "first", mint=MINT)
    rpc.buy(B, "second", mint=second)
    _poll(engine)
    assert len(engine.store.open_positions("paper")) == 1  # maks. én åben kopi


def test_still_sells_after_the_trader_drops_off_the_list():
    engine, rpc, _dex, _rug = _engine()
    engine.update_toptraders({"enabled": True})
    _poll(engine)
    rpc.buy(A, "a-buy")
    _poll(engine)
    assert len(engine.store.open_positions("paper")) == 1
    engine.clients.top.wallets = [B]
    engine.store.set_json("top_traders", {})  # ny liste: A er ikke længere i top
    engine.update_toptraders({"enabled": False})  # selv slået fra følges salget af det, botten ejer
    rpc.sell(A, "a-sell")
    _poll(engine)
    assert engine.store.open_positions("paper") == []
    assert engine.store.closed_positions("paper")[0]["exit_reason"] == "copy_exit"


def test_without_api_key_it_uses_followed_wallets_once():
    engine, rpc, _dex, _rug = _engine(top=None, copy_wallets=(A,))
    engine.update_toptraders({"enabled": True})
    engine.tick(force_scan=True)
    rpc.buy(A, "a-buy")
    engine.tick(force_scan=True)
    rows = engine.store.open_positions("paper")
    # Både scanningen og top-trader-kopieringen ser købet, men der købes kun én gang.
    assert len(rows) == 1 and rows[0]["strategy"] == "toptrader"
    status = engine.status()["toptraders"]
    assert status["api"] is False and status["source"] == "dine fulgte wallets"
    assert [row["wallet"] for row in status["leaders"]] == [A]


def test_settings_are_clamped_saved_and_reachable_over_http(tmp_path):
    import dataclasses

    from fastapi.testclient import TestClient

    from trader.app import create_app
    from trader.config import Settings

    engine, *_ = _engine()
    engine.start = lambda: None
    engine.stop = lambda: None
    engine.update_toptraders({"count": 99, "days": 30, "position_usd": 500, "stop_loss_pct": 0.05})
    cfg = engine.top_cfg
    assert (cfg.count, cfg.days, cfg.stop_loss_pct) == (25, 7, 0.1)
    assert cfg.position_usd <= engine.settings.position_usd
    assert engine.store.get_json("toptraders")["count"] == 25
    with pytest.raises(ValueError, match="Antal traders"):
        engine.update_toptraders({"count": "mange"})

    settings = dataclasses.replace(Settings(), db_path=str(tmp_path / "t.db"))
    with TestClient(create_app(settings=settings, meme_engine=engine)) as client:
        body = client.post("/api/meme/toptraders", json={"enabled": True, "count": 5}).json()
        assert body["toptraders"]["enabled"] is True and body["toptraders"]["settings"]["count"] == 5
        assert {f["name"] for f in body["toptraders"]["fields"]} >= {"count", "position_usd", "follow_sells"}
        assert client.post("/api/meme/toptraders", json={"max_open": "x"}).status_code == 400


def test_exit_rules_for_copies():
    common = dict(partial_taken=False, liquidity_entry=50_000, liquidity_now=50_000)
    off = TopTraderSettings(stop_loss_pct=0.3, max_hold_minutes=60, stop_loss_on=False)
    assert not top_exit_decision(ratio=0.1, peak_ratio=1.0, minutes_held=5, cfg=off, **common).sell  # stop-loss slået fra
    cfg = TopTraderSettings(stop_loss_pct=0.3, max_hold_minutes=60)
    assert cfg.stop_loss_on is True
    assert top_exit_decision(ratio=0.65, peak_ratio=1.0, minutes_held=5, cfg=cfg, **common).kind == "stop_loss"
    assert not top_exit_decision(ratio=1.8, peak_ratio=1.9, minutes_held=5, cfg=cfg, **common).sell  # ingen fast gevinstgrænse
    assert top_exit_decision(ratio=1.9, peak_ratio=3.0, minutes_held=5, cfg=cfg, **common).kind == "trailing"
    assert top_exit_decision(ratio=1.1, peak_ratio=1.2, minutes_held=61, cfg=cfg, **common).kind == "time"
    assert top_exit_decision(ratio=1.0, peak_ratio=1.0, minutes_held=1, cfg=cfg, partial_taken=False, liquidity_entry=50_000, liquidity_now=10_000).kind == "rug_liquidity"


def test_top_10_ignores_the_daily_loss_limit_unless_you_turn_it_on():
    from trader.meme.store import utc_day

    engine, rpc, _dex, _rug = _engine()
    engine.update_toptraders({"enabled": True})
    _poll(engine)
    engine.store.set_state(f"halt_day:paper:{utc_day()}", "1")  # resten af botten har ramt dagens tabsgrænse
    rpc.buy(A, "after-limit")
    _poll(engine)
    assert len(engine.store.open_positions("paper")) == 1  # Top 10 køber stadig
    assert any("Top 10 kopierer stadig" in a["text"] for a in engine.status()["alerts"])

    engine.update_toptraders({"daily_loss_stop": True})
    second = "SecondMint111111111111111111111111111111111"
    _dex.pairs[second] = raw_pair(second, symbol="TWO")
    _rug.reports[second] = safe_report()
    rpc.buy(B, "blocked", mint=second)
    _poll(engine)
    assert len(engine.store.open_positions("paper")) == 1
    assert "tabsgrænse" in engine.status()["toptraders"]["signals"][0]["detail"]

    engine.update_toptraders({"daily_loss_stop": False})
    engine.store.set_state("halt_dd:paper", "1")  # den samlede tabsgrænse stopper altid
    rpc.buy(B, "after-drawdown", mint=second)
    _poll(engine)
    rows = engine.store.open_positions("paper") + engine.store.closed_positions("paper")
    assert all(row["mint"] != second for row in rows)  # intet nyt køb efter den samlede tabsgrænse
