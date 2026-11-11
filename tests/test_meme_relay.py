"""Relæet: Spejder → Analytiker → Køber → Risiko → Kasserer, bygget af bottens rigtige data,
og alle strategier går gennem den samme analytiker (rug, Jupiter-audit, insidere, X)."""

from __future__ import annotations

import meme_fakes
from meme_fakes import make_engine, raw_pair, safe_report
from test_meme_research import _momentum_token
from test_meme_toptraders import A, MINT as COPY_MINT, _engine as _top_engine, _poll


def _arm(dex, rug, mint, **report):
    dex.pairs[mint] = raw_pair(mint)
    dex.boosts.append(mint)
    rug.reports[mint] = safe_report(**report)


def test_relay_shows_five_agents_from_real_data():
    engine, dex, rug, _jup, _notes = make_engine()
    relay = engine.status()["relay"]
    assert [agent["id"] for agent in relay["agents"]] == ["scout", "analyst", "buyer", "risk", "banker"]
    assert relay["activity"] == [] and relay["multiple"] == 1.0 and relay["next"] == 1.25
    _arm(dex, rug, "Good11111111")
    _arm(dex, rug, "Rugged111111", mintAuthority="Dev111")
    engine.tick(force_scan=True)
    relay = engine.status()["relay"]
    agents = {agent["id"]: agent for agent in relay["agents"]}
    assert agents["scout"]["count"] == 2 and agents["scout"]["active"]
    assert agents["analyst"]["count"] == 50  # én af to bestod
    assert agents["buyer"]["count"] == 1 and "købt for 3,00 $ (almindelig)" in agents["buyer"]["text"]
    assert agents["risk"]["count"] == 1
    texts = [(item["agent"], item["text"]) for item in relay["activity"]]
    assert any(agent == "analyst" and "afviste" in text and "Mint authority" in text for agent, text in texts)
    assert any(agent == "buyer" and "købte" in text for agent, text in texts)
    first_good = [agent for agent, text in texts if "MEME" in text]
    assert first_good.index("buyer") < first_good.index("scout")  # nyeste trin først


def test_closing_a_trade_reaches_the_banker_and_the_goal_ladder():
    engine, dex, rug, _jup, _notes = make_engine()
    _arm(dex, rug, "Winner111111")
    engine.tick(force_scan=True)
    dex.set_price("Winner111111", 0.0007)  # stop-loss
    engine.tick()
    relay = engine.status()["relay"]
    agents = {agent["id"]: agent for agent in relay["agents"]}
    assert agents["banker"]["count"] < 0
    assert "MEME" in agents["banker"]["text"] and "%" in agents["banker"]["text"]
    assert relay["activity"][0]["agent"] == "risk" and "stop-loss" in relay["activity"][0]["text"]
    assert relay["losses"] == 1 and relay["multiple"] < 1


def test_top_10_copies_go_through_the_same_analyst():
    engine, rpc, dex, rug = _top_engine()
    engine.update_toptraders({"enabled": True})
    engine.clients.jup.recent.append(_momentum_token(COPY_MINT, audit={"isSus": True}))
    _poll(engine)
    rpc.buy(A, "sus-buy")
    _poll(engine)
    assert engine.store.open_positions("paper") == []
    detail = engine.status()["toptraders"]["signals"][0]["detail"]
    assert detail.startswith("Analytiker") and "mistænkeligt" in detail


def test_top_10_rejects_insider_heavy_tokens():
    engine, rpc, dex, rug = _top_engine()
    engine.update_toptraders({"enabled": True})
    rug.reports[COPY_MINT] = safe_report(graphInsidersDetected=300)
    _poll(engine)
    rpc.buy(A, "insider-buy")
    _poll(engine)
    assert engine.store.open_positions("paper") == []
    assert "insider-wallets" in engine.status()["toptraders"]["signals"][0]["detail"]
