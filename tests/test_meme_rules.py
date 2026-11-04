"""Rene regler: parsing, filtre, risiko, exits, læring og copy-trading-parsing."""

from __future__ import annotations

import json
import time

import pytest

from meme_fakes import raw_pair, safe_report
from trader.meme.clients import Quote, best_pairs, parse_rug_report, parse_x_search
from trader.meme.config import PARAM_BOUNDS, SOL_MINT, StrategyParams, clamp_param
from trader.meme.copytrade import parse_wallet_swaps
from trader.meme.learning import FEATURES, Model, adapt_params, build_features, source_stats
from trader.meme.research import cost_of
from trader.meme.risk import EntryContext, daily_loss_hit, drawdown_hit, evaluate_entry, exit_decision
from trader.meme.safety import market_rejections, roundtrip_rejections, rug_rejections
from trader.meme.wallet import load_keypair

import meme_fakes


def pair(**kwargs):
    return best_pairs([raw_pair("MintA", **kwargs)])["MintA"]


def test_best_pair_picks_most_liquid_and_ignores_other_chains():
    low = raw_pair("MintA", liq=10_000)
    high = raw_pair("MintA", liq=90_000)
    other = dict(raw_pair("MintA", liq=999_999), chainId="ethereum")
    chosen = best_pairs([low, high, other])["MintA"]
    assert chosen.liquidity_usd == 90_000
    assert chosen.socials["twitter"].startswith("https://x.com")
    assert chosen.buy_ratio("h1") == pytest.approx(420 / 650)


def test_market_filters_accept_healthy_pair_and_explain_rejections():
    params = StrategyParams()
    assert market_rejections(pair(), params) == []
    reasons = market_rejections(pair(liq=5_000, change_h1=400, age_min=5, buys=10, sells=90), params)
    text = " ".join(reasons)
    assert "Likviditet" in text
    assert "Overophedet" in text
    assert "For ny" in text
    assert "sælgere" in text
    assert any("bonding curve" in r for r in market_rejections(pair(dex="pumpfun"), params))


def test_rug_report_parsing_and_rejections():
    params = StrategyParams()
    ok = parse_rug_report("MintA", safe_report())
    assert ok.top10_pct == pytest.approx(15.0)
    assert ok.lp_locked_pct == 100
    assert rug_rejections(ok, None, params) == []

    bad = parse_rug_report(
        "MintA",
        safe_report(
            mintAuthority="Dev111",
            freezeAuthority="Dev111",
            risks=[{"name": "Large Amount of LP Unlocked", "level": "danger", "score": 9000}],
        ),
    )
    reasons = " ".join(rug_rejections(bad, None, params))
    assert "Mint authority" in reasons
    assert "Freeze authority" in reasons
    assert "LP Unlocked" in reasons
    # Ingen rapport = ingen handel.
    assert rug_rejections(None, None, params)


def test_top_holders_exclude_amm_pool_accounts():
    holders = [{"address": "pool", "owner": "amm", "pct": 40.0}] + [
        {"address": f"h{i}", "owner": f"o{i}", "pct": 2.0} for i in range(10)
    ]
    report = parse_rug_report("M", safe_report(topHolders=holders, knownAccounts={"amm": {"name": "Raydium", "type": "AMM"}}))
    assert report.top10_pct == pytest.approx(20.0)


def test_roundtrip_catches_honeypots_and_thin_pools():
    params = StrategyParams()
    buy = Quote(SOL_MINT, "M", 1_000_000, 500, 0.5, {})
    assert roundtrip_rejections(buy, None, params)
    thin = Quote("M", SOL_MINT, 500, 700_000, 0.5, {})
    assert "Køb+salg koster" in roundtrip_rejections(buy, thin, params)[0]
    fine = Quote("M", SOL_MINT, 500, 980_000, 0.5, {})
    assert roundtrip_rejections(buy, fine, params) == []


def test_params_are_clamped_to_bounds():
    assert clamp_param("stop_loss_pct", 0.9) == PARAM_BOUNDS["stop_loss_pct"][1]
    params = StrategyParams.from_dict({"min_score": 0.01, "min_txns_h1": 3.7, "ukendt": 5})
    assert params.min_score == PARAM_BOUNDS["min_score"][0]
    assert params.min_txns_h1 == PARAM_BOUNDS["min_txns_h1"][0]


def test_entry_risk_limits():
    cfg = meme_fakes.settings()
    base = dict(
        mint="M",
        equity=15.0,
        cash=15.0,
        deployed=0.0,
        open_count=0,
        open_mints=set(),
        trades_today=0,
        kill_switch=False,
        halted_day=False,
        halted_drawdown=False,
    )
    decision = evaluate_entry(EntryContext(**base), cfg)
    assert decision.allowed and decision.size_usd == 3.0
    # Aldrig mere end bankroll i alt.
    capped = evaluate_entry(EntryContext(**{**base, "deployed": 13.5}), cfg)
    assert capped.allowed and capped.size_usd == pytest.approx(1.5)
    assert not evaluate_entry(EntryContext(**{**base, "deployed": 14.5}), cfg).allowed
    assert not evaluate_entry(EntryContext(**{**base, "kill_switch": True}), cfg).allowed
    assert not evaluate_entry(EntryContext(**{**base, "open_count": 3}), cfg).allowed
    assert not evaluate_entry(EntryContext(**{**base, "trades_today": 8}), cfg).allowed
    assert not evaluate_entry(EntryContext(**{**base, "open_mints": {"M"}}), cfg).allowed
    assert daily_loss_hit(-3.75, cfg) and not daily_loss_hit(-3.0, cfg)
    assert drawdown_hit(-7.5, cfg) and not drawdown_hit(-7.0, cfg)


def test_exit_rules():
    params = StrategyParams()
    common = dict(minutes_held=10, partial_taken=False, liquidity_entry=100_000, liquidity_now=100_000, params=params)
    assert not exit_decision(ratio=1.1, peak_ratio=1.1, **common).sell
    assert exit_decision(ratio=0.74, peak_ratio=1.0, **common).kind == "stop_loss"
    partial = exit_decision(ratio=1.7, peak_ratio=1.7, **common)
    assert partial.kind == "take_profit" and partial.fraction == params.partial_fraction
    trail = exit_decision(ratio=1.4, peak_ratio=2.0, **{**common, "partial_taken": True})
    assert trail.kind == "trailing" and trail.fraction == 1.0
    rug = exit_decision(ratio=1.0, peak_ratio=1.0, **{**common, "liquidity_now": 20_000})
    assert rug.kind == "rug_liquidity"
    assert exit_decision(ratio=1.01, peak_ratio=1.02, **{**common, "minutes_held": 80}).kind == "stale"
    assert exit_decision(ratio=1.3, peak_ratio=1.32, **{**common, "minutes_held": 400, "partial_taken": True}).kind == "time"


def test_model_learns_from_losses_and_wins():
    model = Model()
    good = build_features(pair())
    risky = build_features(pair(change_h1=240, buys=200, sells=210))
    assert model.score(good) > model.score(risky)
    before = model.weights["buy_pressure"]
    for _ in range(10):
        model.update(good, won=False)
    assert model.weights["buy_pressure"] < before
    restored = Model.from_dict(json.loads(json.dumps(model.to_dict())))
    assert restored.weights == model.weights and restored.updates == 10
    assert set(restored.weights) == set(FEATURES)


def test_adapt_params_tightens_after_rugs_and_stays_in_bounds():
    params = StrategyParams()
    trades = [{"pnl_usd": -1.0, "exit_reason": "rug_liquidity"} for _ in range(4)] + [
        {"pnl_usd": -0.5, "exit_reason": "stop_loss"} for _ in range(6)
    ]
    notes = adapt_params(params, trades)
    assert notes
    assert params.max_rug_score < StrategyParams().max_rug_score
    assert params.min_score > StrategyParams().min_score
    for _ in range(200):
        adapt_params(params, trades)
    for name, (low, high) in PARAM_BOUNDS.items():
        assert low <= getattr(params, name) <= high


def test_source_stats_mute_bad_sources():
    closed = [{"source": "copy:W", "pnl_usd": -0.5} for _ in range(6)] + [{"source": "jup_trending", "pnl_usd": 1.0}]
    stats = source_stats(closed)
    assert stats["copy:W"]["muted"] is True
    assert stats["jup_trending"]["muted"] is False


def _tx(wallet, sol_pre, sol_post, pre_tokens, post_tokens):
    return {
        "blockTime": int(time.time()),
        "meta": {
            "err": None,
            "preBalances": [sol_pre, 0],
            "postBalances": [sol_post, 0],
            "preTokenBalances": [{"owner": wallet, "mint": m, "uiTokenAmount": {"amount": str(a)}} for m, a in pre_tokens.items()],
            "postTokenBalances": [{"owner": wallet, "mint": m, "uiTokenAmount": {"amount": str(a)}} for m, a in post_tokens.items()],
        },
        "transaction": {"message": {"accountKeys": [{"pubkey": wallet, "signer": True}, {"pubkey": "Other"}]}},
    }


def test_copy_trade_parsing_detects_buys_and_sells():
    wallet = "Whale111"
    buy = parse_wallet_swaps(_tx(wallet, 10_000_000_000, 9_000_000_000, {}, {"MintA": 5000}), wallet, "sig1")
    assert [(s.side, s.mint) for s in buy] == [("buy", "MintA")]
    assert buy[0].sol == pytest.approx(1.0)
    sell = parse_wallet_swaps(_tx(wallet, 9_000_000_000, 9_800_000_000, {"MintA": 5000}, {}), wallet, "sig2")
    assert [(s.side, s.mint) for s in sell] == [("sell", "MintA")]
    # En airdrop (kun gebyr betalt) er ikke et køb.
    assert parse_wallet_swaps(_tx(wallet, 9_000_000_000, 8_999_995_000, {}, {"Spam": 1}), wallet) == []
    failed = _tx(wallet, 10_000_000_000, 9_000_000_000, {}, {"MintA": 5000})
    failed["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    assert parse_wallet_swaps(failed, wallet) == []


def test_x_search_parsing():
    data = {
        "data": [
            {"id": "1", "author_id": "a", "text": "MintA to the moon", "public_metrics": {"like_count": 5, "retweet_count": 1}},
            {"id": "2", "author_id": "b", "text": "rug?", "public_metrics": {"like_count": 50, "retweet_count": 4}},
            {"id": "3", "author_id": "a", "text": "again", "public_metrics": {"like_count": 0, "retweet_count": 0}},
        ]
    }
    buzz = parse_x_search(data)
    assert (buzz.tweets, buzz.authors, buzz.likes, buzz.reposts) == (3, 2, 55, 5)
    assert buzz.samples[0] == "rug?"


def test_ai_cost_accounting():
    usage = {"input_tokens": 10_000, "output_tokens": 2_000, "server_tool_use": {"web_search_requests": 3}}
    cost, inp, out, searches = cost_of("claude-opus-5-5", usage)
    assert (inp, out, searches) == (10_000, 2_000, 3)
    assert cost == pytest.approx(10_000 * 4 / 1e6 + 2_000 * 20 / 1e6 + 0.03)
    cheap, *_ = cost_of("claude-haiku-5-5", usage)
    assert cheap < cost


def test_keypair_formats_and_errors_do_not_leak_secret():
    from solders.keypair import Keypair

    kp = Keypair()
    assert load_keypair(str(kp)).pubkey() == kp.pubkey()
    assert load_keypair(json.dumps(list(bytes(kp)))).pubkey() == kp.pubkey()
    secret = "ikke-en-rigtig-nøgle-123"
    with pytest.raises(ValueError) as info:
        load_keypair(secret)
    assert secret not in str(info.value)
