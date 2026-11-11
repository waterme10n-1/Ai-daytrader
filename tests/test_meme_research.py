"""Værn fra research (nov. 2026): mistænkelige tokens, serie-udviklere, bot-handel, insidere,
Top 10-pris, fakta pr. token, den automatiske strammer og data-eksport."""

from __future__ import annotations

import dataclasses
import json
import time

import pytest
from fastapi.testclient import TestClient

import meme_fakes
from meme_fakes import make_engine, raw_pair, safe_report
from test_meme_sniper import NEW, _arm as _arm_sniper, _token
from test_meme_toptraders import A, MINT as COPY_MINT, _engine as _top_engine, _poll
from trader.config import Settings
from trader.meme.clients import PairInfo, best_pairs, parse_rug_report, parse_token_info
from trader.meme.config import SniperSettings, StrategyParams
from trader.meme.copytrade import parse_wallet_swaps
from trader.meme.learning import FACT_MIN_ROWS, build_features, fact_report, facts_of, tighten_suggestion
from trader.meme.safety import insider_rejections, sniper_rejections, token_quality_rejections

GOOD = "GoodMint1111"


def _pair(mint: str = GOOD, **kwargs) -> PairInfo:
    return best_pairs([raw_pair(mint, **kwargs)])[mint]


def _arm(dex, rug, mint=GOOD, **report):
    dex.pairs[mint] = raw_pair(mint)
    dex.boosts.append(mint)
    rug.reports[mint] = safe_report(**report)


def _momentum_token(mint: str, **raw):
    data = {"id": mint, "symbol": "MEME", "decimals": 6, "organicScore": 60, "holderCount": 900,
            "audit": {"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True, "topHoldersPercentage": 20.0}}
    audit = raw.pop("audit", {})
    data["audit"].update(audit)
    data.update(raw)
    return parse_token_info(data)


# --------------------------------------------------------------------------- Jupiter-felterne


def test_token_info_reads_suspicious_flag_dev_mints_and_short_stats():
    token = parse_token_info({"id": "M1", "dev": "Dev1", "audit": {"isSus": True, "devMints": 37}, "stats5m": {"numBuys": 9}})
    assert token.sus is True and token.dev_mints == 37 and token.dev == "Dev1" and token.stats_5m == {"numBuys": 9}
    # Jupiter: kun tilstedeværelsen tæller, også hvis værdien er null.
    assert parse_token_info({"id": "M2", "audit": {"isSus": None}}).sus is True
    clean = parse_token_info({"id": "M3", "audit": {"mintAuthorityDisabled": True}})
    assert clean.sus is False and clean.dev_mints is None


def test_quality_rules_reject_suspicious_bot_driven_and_factory_tokens():
    sus = _momentum_token("S", audit={"isSus": True})
    assert "mistænkeligt" in token_quality_rejections(sus, min_organic=30, max_dev_mints=50)[0]
    bots = _momentum_token("B", organicScore=12)
    assert "Organisk score 12" in token_quality_rejections(bots, min_organic=30, max_dev_mints=50)[0]
    assert token_quality_rejections(bots, min_organic=0, max_dev_mints=50) == []  # 0 = fra
    factory = _momentum_token("F", audit={"devMints": 140})
    assert "token-fabrik" in token_quality_rejections(factory, min_organic=30, max_dev_mints=50)[0]
    assert token_quality_rejections(None, min_organic=30, max_dev_mints=50) == []
    report = parse_rug_report("X", safe_report(graphInsidersDetected=75))
    assert "75 insider-wallets" in insider_rejections(report, 50)[0]
    assert insider_rejections(report, 80) == [] and insider_rejections(None, 1) == []


def test_sniper_needs_x_or_telegram_a_website_is_not_enough():
    pair = _pair(NEW)
    pair = dataclasses.replace(pair, socials={}, websites=["https://only.example"])
    token = _token(socials=False)
    cfg = SniperSettings()
    assert any("Hverken X eller Telegram" in r for r in sniper_rejections(pair, token, cfg, 8))
    with_tg = dataclasses.replace(pair, socials={"telegram": "https://t.me/x"})
    assert not any("Telegram" in r for r in sniper_rejections(with_tg, token, cfg, 8))


def test_features_weigh_telegram_and_x_over_a_website_and_count_insiders():
    pair = _pair()
    only_web = dataclasses.replace(pair, socials={}, websites=["https://w.example"])
    assert build_features(only_web)["socials"] == pytest.approx(0.15)
    assert build_features(pair)["socials"] == pytest.approx(1.0)
    many = parse_rug_report(GOOD, safe_report(graphInsidersDetected=99))
    assert build_features(pair, report=many)["insiders"] == pytest.approx(1.0)
    assert build_features(pair, report=parse_rug_report(GOOD, safe_report()))["insiders"] == 0.0
    factory = _momentum_token(GOOD, audit={"devMints": 100}, stats1h={"holderChange": 15})
    feats = build_features(pair, token=factory)
    assert feats["dev_mints"] == pytest.approx(1.0) and feats["holder_growth"] == pytest.approx(0.5)


# --------------------------------------------------------------------------- motoren


def test_momentum_skips_bot_driven_suspicious_and_insider_heavy_tokens():
    engine, dex, rug, jup, _notes = make_engine()
    assert engine.params.min_organic_score == 20
    cases = {
        "LowOrganic11": ({"organicScore": 10}, {}),
        "Suspicious11": ({"audit": {"isSus": True}}, {}),
        "Factory11111": ({"audit": {"devMints": 300}}, {}),
        "Insiders1111": ({}, {"graphInsidersDetected": 200}),
    }
    for mint, (token_raw, report) in cases.items():
        _arm(dex, rug, mint, **report)
        jup.recent.append(_momentum_token(mint, **token_raw))
    engine.tick(force_scan=True)
    assert engine.store.open_positions("paper") == []
    reasons = {row["mint"]: row["reason"] for row in engine.store.recent_candidates("paper")}
    assert "Organisk score 10" in reasons["LowOrganic11"]
    assert "mistænkeligt" in reasons["Suspicious11"]
    assert "token-fabrik" in reasons["Factory11111"]
    assert "insider-wallets" in reasons["Insiders1111"]
    # Et rent token med samme data købes stadig.
    _arm(dex, rug, GOOD)
    jup.recent.append(_momentum_token(GOOD))
    engine.tick(force_scan=True)
    assert [row["mint"] for row in engine.store.open_positions("paper")] == [GOOD]


def test_sniper_skips_suspicious_and_factory_tokens():
    engine, dex, rug, jup, _notes = make_engine()
    _arm_sniper(dex, rug, jup, mint="SusSnipe1111111111111111111111111111111111", audit={"isSus": True})
    _arm_sniper(dex, rug, jup, mint="FactorySnipe11111111111111111111111111111", audit={"devMints": 45})
    engine.tick(force_scan=True)
    assert engine.store.open_positions("paper") == []
    reasons = " ".join(row["reason"] for row in engine.store.recent_candidates("paper"))
    assert "mistænkeligt" in reasons and "token-fabrik" in reasons


def test_copy_signal_keeps_the_token_amount_when_paid_in_sol():
    tx = {
        "blockTime": 1,
        "meta": {"err": None, "preBalances": [5_000_000_000], "postBalances": [4_000_000_000],
                 "preTokenBalances": [], "postTokenBalances": [{"owner": A, "mint": "Tok", "uiTokenAmount": {"amount": "777"}}]},
        "transaction": {"message": {"accountKeys": [A]}},
    }
    (signal,) = parse_wallet_swaps(tx, A, "sig")
    assert signal.side == "buy" and signal.token_raw == 777 and signal.sol == pytest.approx(1.0)
    usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    tx["meta"].update(postBalances=[4_999_995_000], preTokenBalances=[{"owner": A, "mint": usdc, "uiTokenAmount": {"amount": "5000000"}}],
                      postTokenBalances=[{"owner": A, "mint": "Tok", "uiTokenAmount": {"amount": "777"}}])
    (paid_usdc,) = parse_wallet_swaps(tx, A, "sig2")
    assert paid_usdc.token_raw == 0  # traderens pris i SOL kan ikke regnes ud: værnet springes over


def _leader_buy(rpc, signature, token_raw, sol=2.0):
    rpc.swap(A, signature, sol_before=10_000_000_000, sol_after=int((10 - sol) * 1e9), before={}, after={COPY_MINT: token_raw})


def test_top_10_does_not_chase_far_above_the_traders_price():
    engine, rpc, dex, _rug = _top_engine()
    engine.update_toptraders({"enabled": True, "position_usd": 2.0})
    assert engine.top_cfg.max_chase_pct == pytest.approx(0.15)
    _poll(engine)
    # Vores quote: ca. 0,00673 lamports pr. rå enhed. Traderen betalte 30 % mindre.
    _leader_buy(rpc, "cheap", 386_000_000_000)
    _poll(engine)
    assert engine.store.open_positions("paper") == []
    detail = engine.status()["toptraders"]["signals"][0]["detail"]
    assert "over traderens købspris" in detail
    # Samme pris som traderen: købes, og begrundelsen viser forskellen.
    _leader_buy(rpc, "fair", 296_000_000_000)
    _poll(engine)
    rows = engine.store.open_positions("paper")
    assert len(rows) == 1 and "i forhold til traderens" in rows[0]["reasoning"]
    facts = json.loads(rows[0]["safety"])["facts"]
    assert abs(facts["chase"]) < 0.05


# --------------------------------------------------------------------------- fakta, analyse og strammer


def test_facts_capture_the_raw_numbers_behind_a_decision():
    pair = _pair()
    token = _momentum_token(GOOD, organicScore=44, stats1h={"holderChange": 3.5, "numOrganicBuyers": 120, "numNetBuyers": 40})
    report = parse_rug_report(GOOD, safe_report(graphInsidersDetected=7))
    facts = facts_of(pair, token=token, report=report, boosted=True)
    assert facts["liq"] == 120_000 and facts["organic"] == 44 and facts["insiders"] == 7
    assert facts["tg"] == 1 and facts["x"] == 1 and facts["boosted"] == 1
    assert facts["organic_buyers_h1"] == 120 and facts["holder_chg_h1"] == 3.5
    assert 0 <= facts["hour"] < 24 and facts["age_min"] == pytest.approx(300, abs=1)


def test_shadows_store_facts_and_skip_bonding_curve_tokens():
    engine, dex, rug, jup, _notes = make_engine(meme_fakes.settings(shadows=True))
    _arm(dex, rug, "Insiders1111", graphInsidersDetected=200)
    jup.recent.append(_momentum_token("Insiders1111"))
    engine.tick(force_scan=True)
    rows = engine.store.recent_shadows("paper")
    assert rows and json.loads(rows[0]["facts"])["insiders"] == 200
    assert rows[0]["strategy"] == "momentum"
    _arm_sniper(dex, rug, jup, mint="Curve1111111111111111111111111111111111111")
    dex.pairs["Curve1111111111111111111111111111111111111"]["dexId"] = "pumpfun"
    engine.tick(force_scan=True)
    assert not [row for row in engine.store.recent_shadows("paper") if row["mint"].startswith("Curve")]


def _rows(values_and_pnl, key="organic"):
    return [{"facts": {key: value}, "pnl": pnl} for value, pnl in values_and_pnl]


def test_fact_report_shows_three_groups_sorted_by_difference():
    rows = _rows([(v, -0.3 if v < 30 else 0.1) for v in range(0, 90)])
    rows += [{"facts": {"liq": 10_000 + i * 1000}, "pnl": 0.0} for i in range(60)]
    report = fact_report(rows)
    assert report[0]["fact"] == "organic" and len(report[0]["buckets"]) == 3
    low, mid, high = report[0]["buckets"]
    assert low["avg"] == pytest.approx(-0.3) and high["avg"] == pytest.approx(0.1)
    assert low["label"].startswith("under") and report[0]["buckets"][2]["label"].endswith("+")
    assert report[-1]["fact"] == "liq" and report[-1]["spread"] == 0
    assert fact_report(rows[: FACT_MIN_ROWS - 1]) == [] or fact_report(rows[: FACT_MIN_ROWS - 1])[0]["n"] < FACT_MIN_ROWS + 1


RULE = [{"fact": "organic", "param": "min_organic_score", "kind": "min", "grid": (20, 30, 40, 50)}]


def test_tightener_only_tightens_on_clear_losses_with_enough_data():
    losers = [(v, -0.25) for v in range(30, 40) for _ in range(4)]  # 40 tokens med score 30-39 tabte 25 %
    winners = [(v, 0.12) for v in range(40, 90)]
    best = tighten_suggestion(_rows(losers + winners), {"min_organic_score": 30}, RULE)
    assert best["param"] == "min_organic_score" and best["new"] == 40
    assert best["removed"] == 40 and best["removed_avg"] == pytest.approx(-0.25)
    # For få tabere: ingen ændring.
    far_winners = [(60 + v % 30, 0.12) for v in range(50)]
    assert tighten_suggestion(_rows(losers[:20] + far_winners), {"min_organic_score": 30}, RULE) is None
    # Den mindste stramning, der fjerner taberne, vælges (50 ville også tage vindere med).
    assert tighten_suggestion(_rows(losers + winners), {"min_organic_score": 30}, RULE)["new"] == 40
    # Tabet er småt (-2 %): ingen ændring.
    assert tighten_suggestion(_rows([(v, -0.02) for v, _ in losers] + winners), {"min_organic_score": 30}, RULE) is None
    # Aldrig løsere: står grænsen allerede på 50, foreslås intet lavere.
    assert tighten_suggestion(_rows(losers + winners), {"min_organic_score": 50}, RULE) is None


def test_tightener_respects_the_other_filters():
    rules = RULE + [{"fact": "liq", "param": "min_liquidity_usd", "kind": "min", "grid": (25_000, 40_000)}]
    # Taberne med lav score har alle lav likviditet, som allerede er filtreret fra: intet at hente.
    rows = [{"facts": {"organic": 35, "liq": 10_000}, "pnl": -0.3} for _ in range(40)]
    rows += [{"facts": {"organic": 60, "liq": 90_000}, "pnl": 0.1} for _ in range(40)]
    assert tighten_suggestion(rows, {"min_organic_score": 30, "min_liquidity_usd": 25_000}, rules) is None


def _done_shadow(engine, strategy, facts, pnl, i):
    shadow_id = engine.store.add_shadow({"mode": "paper", "mint": f"S{strategy}{i}", "symbol": "S", "strategy": strategy, "stage": "score",
                                         "entry_price": 1.0, "facts": facts})
    engine.store.update_shadow(shadow_id, status="done", sim_pnl_pct=pnl, verdict="neutral", done_at="2026-11-07T00:00:00+00:00")


def test_engine_tightens_one_filter_a_day_and_explains_why():
    engine, *_ = make_engine(meme_fakes.settings(shadows=True))
    for i in range(40):
        _done_shadow(engine, "momentum", {"organic": 32 + (i % 8), "liq": 90_000}, -0.3, i)
    for i in range(60):
        _done_shadow(engine, "momentum", {"organic": 45 + i % 40, "liq": 90_000}, 0.08, 100 + i)
    engine._auto_tighten(time.time())
    assert engine.params.min_organic_score == 40
    lesson = engine.store.recent_lessons(1)[0]["text"]
    assert "Min. organisk score" in lesson and "-30.0 %" in lesson
    assert engine.store.get_json("params")["min_organic_score"] == 40
    # Højst én stramning i døgnet pr. strategi.
    for i in range(40):
        _done_shadow(engine, "momentum", {"organic": 41 + (i % 8), "liq": 90_000}, -0.3, 300 + i)
    engine._auto_tighten(time.time())
    assert engine.params.min_organic_score == 40
    analysis = engine.status()["shadows"]["analysis"]
    momentum = [s for s in analysis["strategies"] if s["strategy"] == "momentum"][0]
    assert momentum["n"] >= 100 and momentum["facts"][0]["fact"] == "organic"
    assert analysis["tightened"][0]["param"] == "min_organic_score"


def test_engine_tightens_the_sniper_wait_from_its_own_shadows():
    engine, *_ = make_engine(meme_fakes.settings(shadows=True))
    for i in range(40):
        _done_shadow(engine, "sniper", {"age_min": 5 + (i % 4) * 0.5, "liq": 30_000, "holders": 500, "top10": 10}, -0.4, i)
    for i in range(40):
        _done_shadow(engine, "sniper", {"age_min": 12 + i % 10, "liq": 30_000, "holders": 500, "top10": 10}, 0.15, 100 + i)
    engine._auto_tighten(time.time())
    assert engine.sniper.min_age_seconds == 420  # 7 min. er nok til at fjerne taberne (5-6,5 min.)
    assert engine.store.get_json("sniper")["min_age_seconds"] == 420


# --------------------------------------------------------------------------- engangs-opgradering og eksport


def test_research_upgrade_gives_saved_sniper_settings_the_new_wait_once():
    cfg = meme_fakes.settings()
    engine, *_ = make_engine(cfg)
    engine.sniper.min_age_seconds = 60
    engine._save_sniper()
    engine.store.set_json("params", engine.params.to_dict())
    engine.store.set_state("research_upgrade", "")
    engine._maybe_research_upgrade()
    assert engine.sniper.min_age_seconds == 300
    assert engine.store.get_json("sniper")["min_age_seconds"] == 300
    assert "Nye værn mod tab" in engine.store.recent_lessons(1)[0]["text"]
    engine.sniper.min_age_seconds = 120  # brugeren vælger selv bagefter: respekteres
    engine._maybe_research_upgrade()
    assert engine.sniper.min_age_seconds == 120


def test_old_params_without_the_new_fields_get_the_new_defaults():
    params = StrategyParams.from_dict({"min_liquidity_usd": 40_000})
    assert params.min_organic_score == 20 and params.max_insiders == 50 and params.max_dev_mints == 50
    assert StrategyParams.from_dict({"max_insiders": 9.6}).max_insiders == 10


def test_export_has_trades_shadows_and_settings_but_no_secrets(tmp_path, monkeypatch):
    from trader.app import create_app

    engine, dex, rug, jup, _notes = make_engine(meme_fakes.settings(shadows=True, private_key="hemmelig-nøgle", anthropic_api_key="sk-hemmelig"))
    monkeypatch.setattr(engine, "start", lambda: None)
    monkeypatch.setattr(engine, "stop", lambda: None)
    _arm(dex, rug, GOOD)
    jup.recent.append(_momentum_token(GOOD))
    engine.tick(force_scan=True)
    _done_shadow(engine, "momentum", {"organic": 50}, 0.1, 1)
    settings = dataclasses.replace(Settings.from_env(), db_path=str(tmp_path / "t.db"), host="127.0.0.1", app_username="",
                                   app_password="", app_password_hash="", session_secret="")
    with TestClient(create_app(settings=settings, meme_engine=engine)) as client:
        response = client.get("/api/meme/export")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    body = response.text
    assert "hemmelig" not in body
    data = response.json()
    assert data["positions"][0]["safety"]["facts"]["organic"] == 60
    assert data["shadows"][0]["facts"] == {"organic": 50}
    assert data["params"]["min_organic_score"] == 20 and "weights" in data["model"]
    engine.restart_paper(50)
    archived = engine.export_data()["archived_positions"]
    assert archived and archived[0]["mode"].startswith("paper-arkiv-")
