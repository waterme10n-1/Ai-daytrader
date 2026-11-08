"""trader.quant: risikomotor, memecoin-filter og strategi-motor. Ingen netværk."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from trader.desk.alpaca import Bar
from trader.desk.markets import DK, US
from trader.quant import memecoin_filter as mf
from trader.quant import strategy_engine as se
from trader.quant.risk_engine import (
    DK_FEES,
    US_FEES,
    AssetClass,
    FeeModel,
    PortfolioState,
    RiskConfig,
    RiskEngine,
    SolanaFees,
    TradeSignal,
)

NOW = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
NY = ZoneInfo("America/New_York")
CPH = ZoneInfo("Europe/Copenhagen")
MINT = "So11111111111111111111111111111111111111112"


# =========================================================================== risk_engine


def _portfolio(equity=100_000.0, cash=None):
    return PortfolioState(equity=equity, day_start_equity=equity, cash=equity if cash is None else cash)


def test_stock_size_keeps_risk_including_fees_at_one_percent():
    engine = RiskEngine()
    decision = engine.evaluate(TradeSignal("NVDA", AssetClass.US_STOCK, 180.0, 177.0, 186.0), _portfolio(), NOW)
    assert decision.approved, decision.reasons
    assert decision.quantity == int(decision.quantity)  # hele aktier
    assert decision.risk_amount <= 1_000.0 + 1e-6
    assert decision.risk_amount > 990.0  # så tæt på budgettet som hele aktier tillader
    # én aktie mere ville sprænge budgettet
    assert engine._risk_cost(TradeSignal("NVDA", AssetClass.US_STOCK, 180.0, 177.0, 186.0), decision.quantity + 1) > 1_000.0
    assert decision.net_reward_risk and decision.net_reward_risk >= 1.5
    assert decision.net_reward_after_tax == pytest.approx(decision.net_reward * 0.73)


def test_risk_per_trade_can_never_go_above_two_percent():
    assert RiskConfig(risk_per_trade_pct=0.02).risk_per_trade_pct == 0.02
    with pytest.raises(ValueError, match="højst 2 %"):
        RiskConfig(risk_per_trade_pct=0.021)
    with pytest.raises(ValueError):
        RiskConfig(memecoin_take_profit_multiple=1.0)


def test_memecoin_is_capped_at_half_a_percent_and_takes_the_stake_out_at_2x():
    engine = RiskEngine()
    decision = engine.evaluate(TradeSignal("BONK", AssetClass.MEMECOIN, 0.00002, 0.000013), _portfolio(), NOW)
    assert decision.approved, decision.reasons
    assert decision.notional == pytest.approx(500.0, rel=1e-6)  # 0,5 % af 100.000
    assert decision.take_profit == pytest.approx(0.00004)  # 2x er påkrævet, også uden mål i signalet
    leg = decision.exits[0]
    assert leg.price == pytest.approx(0.00004) and "indsatsen" in leg.reason
    sol = engine.fees.solana
    paid = decision.quantity * 0.00002 + engine.fees.side(AssetClass.MEMECOIN, "buy", decision.quantity, 0.00002)
    received = leg.quantity * leg.price - engine.fees.side(AssetClass.MEMECOIN, "sell", leg.quantity, leg.price)
    assert received == pytest.approx(paid, rel=1e-6)  # indsatsen inkl. gebyrer er hjemme
    assert 0.5 < leg.quantity / decision.quantity < 0.53
    assert sol.network_usd() == pytest.approx(105_000 / 1e9 * 150)
    # Et højere mål i signalet bliver til et ekstra salg af resten.
    higher = engine.evaluate(TradeSignal("BONK", AssetClass.MEMECOIN, 0.00002, 0.000013, take_profit=0.0001), _portfolio(), NOW)
    assert len(higher.exits) == 2 and higher.exits[1].price == 0.0001


def test_tiny_memecoin_trades_are_refused_when_network_fees_eat_the_gain():
    engine = RiskEngine()
    decision = engine.evaluate(TradeSignal("BONK", AssetClass.MEMECOIN, 0.00002, 0.000013), _portfolio(equity=15.0), NOW)
    assert not decision.approved  # 0,5 % af 15 $ = 7,5 cent; gebyrerne alene er ca. 3 cent
    assert any("Gebyrerne" in reason for reason in decision.reasons)


def test_daily_drawdown_over_five_percent_closes_everything_and_pauses_a_day():
    engine = RiskEngine()
    exactly = PortfolioState(equity=95_000, day_start_equity=100_000)
    assert engine.check_daily_drawdown(exactly, NOW).breached is False  # "mere end 5 %"
    portfolio = PortfolioState(equity=94_900, day_start_equity=100_000)
    status = engine.check_daily_drawdown(portfolio, NOW)
    assert status.breached and status.close_all
    assert status.halted_until == NOW + timedelta(hours=24) == portfolio.halted_until
    assert "5,1 %" in status.reason
    later = engine.check_daily_drawdown(portfolio, NOW + timedelta(hours=1))
    assert later.breached and not later.close_all  # allerede lukket
    blocked = engine.evaluate(TradeSignal("AAPL", AssetClass.US_STOCK, 200.0, 198.0, 205.0), portfolio, NOW + timedelta(hours=23))
    assert not blocked.approved and "Handelsstop" in blocked.reasons[0]
    fresh_day = PortfolioState(equity=94_900, day_start_equity=94_900, halted_until=portfolio.halted_until)
    assert engine.evaluate(TradeSignal("AAPL", AssetClass.US_STOCK, 200.0, 198.0, 205.0), fresh_day, NOW + timedelta(hours=24, minutes=1)).approved


def test_fees_us_sell_side_and_danish_minimum_commission():
    assert US_FEES.side("buy", 100, 50.0) == pytest.approx(100 * 50 * 0.0005)
    sell = US_FEES.side("sell", 100_000, 10.0)
    assert sell == pytest.approx(1_000_000 * 0.0000278 + 8.30 + 1_000_000 * 0.0005)  # TAF har et loft
    assert DK_FEES.side("buy", 1, 100.0) == pytest.approx(29.0 + 0.1)
    engine = RiskEngine()
    small = engine.evaluate(TradeSignal("GN", AssetClass.DK_STOCK, 100.0, 98.0, 104.0), _portfolio(equity=20_000), NOW)
    assert not small.approved and any("Gebyrerne" in reason for reason in small.reasons)


def test_signals_without_target_or_with_a_bad_stop_are_refused():
    engine = RiskEngine()
    no_target = engine.evaluate(TradeSignal("AAPL", AssetClass.US_STOCK, 200.0, 198.0), _portfolio(), NOW)
    assert not no_target.approved and "kursmål" in no_target.reasons[0]
    bad_stop = engine.evaluate(TradeSignal("AAPL", AssetClass.US_STOCK, 200.0, 201.0, 210.0), _portfolio(), NOW)
    assert not bad_stop.approved and "Stop-loss" in bad_stop.reasons[0]
    poor = engine.evaluate(TradeSignal("AAPL", AssetClass.US_STOCK, 200.0, 198.0, 201.0), _portfolio(), NOW)
    assert not poor.approved and any("Gevinst mod risiko" in reason and "1,50" in reason for reason in poor.reasons)


def test_cash_and_currency_limit_the_position():
    engine = RiskEngine()
    limited = engine.evaluate(TradeSignal("NVDA", AssetClass.US_STOCK, 180.0, 177.0, 186.0), _portfolio(cash=9_000.0), NOW)
    assert limited.approved and limited.quantity == 49  # 49 aktier + købsgebyr under 9.000 $
    dk_in_usd = engine.evaluate(TradeSignal("NOVO-B", AssetClass.DK_STOCK, 300.0, 295.0, 312.0, fx_to_base=0.145), _portfolio(), NOW)
    assert dk_in_usd.approved and dk_in_usd.risk_amount <= 1_000.0
    assert dk_in_usd.notional == pytest.approx(dk_in_usd.quantity * 300.0 * 0.145)
    assert dk_in_usd.to_dict()["asset_class"] == "dk_stock"


def test_fee_model_can_use_your_own_rates():
    engine = RiskEngine(fees=FeeModel(solana=SolanaFees(sol_price_usd=200.0, priority_fee_lamports=1_000_000, slippage_pct=0.03)))
    decision = engine.evaluate(TradeSignal("WIF", AssetClass.MEMECOIN, 1.0, 0.7), _portfolio(equity=10_000), NOW)
    assert decision.fees > RiskEngine().evaluate(TradeSignal("WIF", AssetClass.MEMECOIN, 1.0, 0.7), _portfolio(equity=10_000), NOW).fees


# =========================================================================== memecoin_filter

CLEAN = {
    "markets": [{"lp": {"lpLockedPct": 100}}],
    "mintAuthority": None,
    "freezeAuthority": None,
    "knownAccounts": {"poolA": {"type": "AMM"}, "binance1": {"type": "CEX"}},
    "topHolders": [{"address": "poolA", "owner": "poolA", "pct": 30}, {"address": "x", "owner": "binance1", "pct": 9}]
    + [{"address": f"h{i}", "owner": f"o{i}", "pct": 1.5} for i in range(15)],
    "tokenMeta": {"symbol": "MEME"},
}


def _posts(n, *, organic=True):
    if organic:
        return [mf.Post(text=f"post nummer {i} om {['kunst', 'holdet', 'roadmap', 'memes'][i % 4]} {i}", author_id=f"a{i}",
                        created_at=NOW - timedelta(minutes=9 * i), author_created_at=NOW - timedelta(days=500), followers=800, likes=4)
                for i in range(n)]
    return [mf.Post(text="BUY $MEME NOW 100x https://t.co/x", author_id=f"bot{i % 3}", created_at=NOW.replace(minute=7),
                    author_created_at=NOW - timedelta(days=2), followers=3) for i in range(n)]


def test_clean_token_scores_full_and_is_allowed():
    result = mf.evaluate(MINT, CLEAN, _posts(40), NOW)
    assert result["safety_score"] == 100 and result["trade_allowed"] is True and result["reasons"] == []
    assert result["checks"]["holders"]["data"]["top10_pct"] == 15.0  # pool og børs tæller ikke
    assert json.loads(json.dumps(result))["trade_allowed"] is True  # JSON-venligt


def test_without_x_the_token_can_still_pass_on_the_hard_checks():
    result = mf.evaluate(MINT, CLEAN, None, NOW)
    assert result["safety_score"] == 75 and result["trade_allowed"] is True
    assert result["checks"]["social"]["passed"] is None


def test_liquidity_must_be_burned_or_locked_for_six_months():
    unlocked = {**CLEAN, "markets": [{"lp": {"lpLockedPct": 40}}]}
    result = mf.evaluate(MINT, unlocked, None, NOW)
    assert not result["trade_allowed"] and "40,0 %" in result["reasons"][0]
    soon = {**CLEAN, "lockers": {"L1": {"unlockDate": int((NOW + timedelta(days=30)).timestamp())}}}
    check = mf.check_liquidity(soon, NOW)
    assert check.passed is False and "30 dage" in check.detail
    long_lock = {**CLEAN, "lockers": {"L1": {"unlockDate": int((NOW + timedelta(days=400)).timestamp() * 1000)}}}
    assert mf.check_liquidity(long_lock, NOW).passed is True
    assert mf.check_liquidity({}, NOW).passed is False


def test_holder_concentration_over_twenty_percent_blocks_the_trade():
    heavy = {**CLEAN, "topHolders": [{"address": f"w{i}", "owner": f"w{i}", "pct": 2.5} for i in range(12)]}
    check = mf.check_holders(heavy)
    assert check.passed is False and check.data["top10_pct"] == 25.0
    assert mf.check_holders(heavy, exclude={"w0", "w1", "w2", "w3"}).passed is True  # dine egne undtagelser
    assert mf.check_holders({}).passed is False


def test_active_authorities_block_the_trade():
    risky = {**CLEAN, "mintAuthority": "Dev111", "freezeAuthority": "Dev111"}
    result = mf.evaluate(MINT, risky, _posts(40), NOW)
    assert not result["trade_allowed"] and result["checks"]["authorities"]["points"] == 0
    assert "mint authority" in result["reasons"][0] and "freeze" in result["reasons"][0]


def test_bot_driven_hype_on_x_blocks_an_otherwise_clean_token():
    result = mf.evaluate(MINT, CLEAN, _posts(60, organic=False), NOW)
    assert result["trade_allowed"] is False
    social = result["checks"]["social"]
    assert social["passed"] is False and social["data"]["bot_share"] == 1.0
    assert "kopieret tekst" in social["detail"]
    few = mf.analyze_social(_posts(10), NOW)
    assert few.passed is None and few.points <= 10


def test_x_payload_is_parsed_with_author_details():
    payload = {
        "data": [{"id": "1", "text": "hej $MEME", "author_id": "u1", "created_at": "2026-11-09T14:00:00.000Z",
                  "public_metrics": {"like_count": 2, "reply_count": 1, "retweet_count": 0, "quote_count": 0}}],
        "includes": {"users": [{"id": "u1", "created_at": "2020-01-01T00:00:00.000Z", "public_metrics": {"followers_count": 1200}, "verified": True}]},
    }
    (post,) = mf.parse_x_posts(payload)
    assert post.followers == 1200 and post.engagement == 3 and post.verified and post.author_created_at.year == 2020


def test_fetchers_use_rugcheck_and_x_and_fail_safely():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request
        if request.url.host == "api.rugcheck.xyz":
            return httpx.Response(200, json=CLEAN)
        return httpx.Response(429, json={})

    transport = httpx.MockTransport(handler)
    report = mf.fetch_report(MINT, api_key="k", client=httpx.Client(base_url=mf.RUGCHECK_URL, transport=transport))
    assert report["tokenMeta"]["symbol"] == "MEME" and seen["api.rugcheck.xyz"].headers["X-API-KEY"] == "k"
    posts = mf.fetch_posts(MINT, "MEME", bearer="b", client=httpx.Client(base_url=mf.X_API_URL, transport=transport))
    assert posts is None  # rate limit: X regnes som ikke tilgængelig
    query = seen["api.x.com"].url.params["query"]
    assert MINT in query and "$MEME" in query and "-is:retweet" in query
    result = mf.evaluate(MINT, None, None, NOW)
    assert result["trade_allowed"] is False and result["safety_score"] == 0


def test_check_token_validates_the_address_and_cli_returns_json(monkeypatch, capsys):
    with pytest.raises(ValueError):
        mf.check_token("ikke-en-adresse")
    monkeypatch.setattr(mf, "fetch_report", lambda mint, **kw: CLEAN)
    monkeypatch.setattr(mf, "fetch_posts", lambda mint, ticker, **kw: _posts(30))
    assert mf.main([MINT]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["trade_allowed"] is True and out["mint"] == MINT


# =========================================================================== strategy_engine


def test_indicators_match_ta_lib_definitions():
    values = [float(v) for v in range(1, 11)]
    assert se._ema_python(values, 3) == [None, None, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]
    assert se._rsi_python(values, 3)[3:] == [100.0] * 7
    assert se._rsi_python([5.0] * 6, 3)[3:] == [0.0] * 3  # TA-Lib giver 0, når kursen står stille
    zigzag = [10, 11, 10, 11, 10, 11, 10, 11]
    assert se._rsi_python([float(v) for v in zigzag], 4)[4] == pytest.approx(50.0)
    bars = [Bar(NOW, 10, 11, 9, 10, 100), Bar(NOW, 10, 13, 11, 12, 300)]
    assert se.session_vwap(bars) == [pytest.approx(10.0), pytest.approx((10 * 100 + 12 * 300) / 400)]


def test_indicators_agree_with_ta_lib_when_it_is_installed():
    talib = pytest.importorskip("talib")
    numpy = pytest.importorskip("numpy")
    values = [100 + ((i * 7) % 11) - 5 + i * 0.1 for i in range(80)]
    for ours, theirs in ((se._ema_python(values, 9), talib.EMA(numpy.array(values), 9)), (se._rsi_python(values, 14), talib.RSI(numpy.array(values), 14))):
        for a, b in zip(ours, theirs):
            assert (a is None and b != b) or a == pytest.approx(b, rel=1e-9)


def test_swing_low_is_a_confirmed_local_bottom():
    lows = [5, 4, 3, 4, 5, 6, 7]
    bars = [Bar(NOW, low + 1, low + 2, low, low + 1, 1) for low in lows]
    assert se.swing_low(bars, 6) == 3
    assert se.swing_low(bars, 3) == 3  # for få lys efter bunden: det laveste af de seneste lys


def _day_bars(day, start, end, price, volume, tz=NY):
    out = []
    moment = datetime.combine(day, start, tzinfo=tz)
    index = 0
    while moment.time() < end:
        close = price(index)
        opening = price(index - 1) if index else close
        out.append(Bar(moment.astimezone(timezone.utc), opening, max(opening, close) + 0.05, min(opening, close) - 0.05, close, volume))
        moment += timedelta(minutes=5)
        index += 1
    return out


DIP = [105.0, 104.7, 104.4, 104.1, 103.8, 103.5, 103.3, 103.1, 103.0, 102.9, 102.95, 103.0, 103.1, 103.2, 103.35, 103.5, 103.7, 103.9, 104.1, 104.3]


def _us_history():
    days = [d for d in (date(2026, 11, 2) + timedelta(days=k) for k in range(12)) if d.weekday() < 5]
    today, bars = days[-1], []
    for day in days[:-1]:
        bars += _day_bars(day, time(4, 0), time(9, 30), lambda i: 100.0, 1_000)
        bars += _day_bars(day, time(9, 30), time(16, 0), lambda i: 100.0 + 0.2 * ((i % 6) - 3) / 3, 50_000)
    bars += _day_bars(today, time(4, 0), time(9, 30), lambda i: 100 + min(5.0, i * 0.08), 3_000)
    bars += _day_bars(today, time(9, 30), time(11, 10), lambda i: DIP[min(i, len(DIP) - 1)], 60_000)
    return today, bars


def test_premarket_momentum_measures_rvol_at_the_same_time_of_day():
    today, bars = _us_history()
    at_920 = datetime.combine(today, time(9, 20), tzinfo=NY)
    move = se.momentum("ABC", bars, US, at_920)
    assert move.window == "før åbningen" and move.rvol == pytest.approx(3.0)
    assert move.change_pct == pytest.approx((105.0 / move.prev_close - 1) * 100) and move.change_pct > 3
    assert se.screen([move])[0].symbol == "ABC"
    assert se.screen([move], min_rvol=3.5) == [] and se.screen([None]) == []
    early = se.momentum("ABC", bars, US, datetime.combine(today, time(3, 0), tzinfo=NY))
    assert early is None  # før handlen før åbningen er startet


def test_buy_signal_on_a_vwap_break_with_trend_and_rsi_in_range():
    today, bars = _us_history()
    before = datetime.combine(today, time(10, 50), tzinfo=NY)
    assert se.find_signal("ABC", bars, US, before) is None  # lyset kl. 10:45 er stadig under VWAP
    at = datetime.combine(today, time(10, 55), tzinfo=NY)
    signal = se.find_signal("ABC", bars, US, at)
    assert signal is not None
    assert (signal.entry, signal.stop_loss, signal.target) == (103.7, 102.74, 105.63)
    assert signal.stop_loss < signal.swing_low < signal.entry
    assert signal.reward_risk >= 2.0 and 50 <= signal.rsi <= 70 and signal.ema9 > signal.ema20
    later = datetime.combine(today, time(11, 5), tzinfo=NY)
    assert se.find_signal("ABC", bars, US, later) is None  # intet nyt brud, og RSI er over 70
    with pytest.raises(ValueError):
        se.find_signal("ABC", bars, US, at, reward_risk=1.5)


def test_copenhagen_uses_the_first_fifteen_minutes_instead_of_premarket():
    days = [d for d in (date(2026, 11, 2) + timedelta(days=k) for k in range(12)) if d.weekday() < 5]
    today, bars = days[-1], []
    for day in days[:-1]:
        bars += _day_bars(day, time(9, 0), time(17, 0), lambda i: 300.0, 2_000, CPH)
    bars += _day_bars(today, time(9, 0), time(9, 30), lambda i: 312.0 + i, 7_000, CPH)
    move = se.momentum("NOVO-B", bars, DK, datetime.combine(today, time(9, 16), tzinfo=CPH))
    assert move.window == "første 15 min." and move.rvol == pytest.approx(3.5)
    assert move.price == 314.0 and move.change_pct == pytest.approx((314 / 300 - 1) * 100)


def test_new_york_opens_at_1530_or_1430_danish_time():
    assert se.open_in_denmark(US, date(2026, 11, 9)).strftime("%H:%M") == "15:30"
    assert se.open_in_denmark(US, date(2026, 10, 27)).strftime("%H:%M") == "14:30"  # Europa har skiftet, USA ikke
    assert se.open_in_denmark(US, date(2026, 3, 10)).strftime("%H:%M") == "14:30"
    assert se.open_in_denmark(DK, date(2026, 11, 9)).strftime("%H:%M") == "09:00"


def test_run_screens_and_signals_with_any_data_source():
    today, bars = _us_history()
    flat = [Bar(bar.ts, 50, 50.1, 49.9, 50, bar.volume) for bar in bars]
    result = se.run("us", ["ABC", "FLAT"], datetime.combine(today, time(10, 55), tzinfo=NY).astimezone(timezone.utc),
                    fetch=lambda symbols, market: {"ABC": bars, "FLAT": flat})
    assert [item["symbol"] for item in result["candidates"]] == ["ABC"]
    assert result["signals"][0]["entry"] == 103.7 and result["opens_danish_time"] == "15:30"
    assert json.dumps(result)


def test_yfinance_frames_become_bars():
    pd = pytest.importorskip("pandas")
    index = pd.DatetimeIndex(["2026-11-09 09:30", "2026-11-09 09:35"]).tz_localize("America/New_York")
    frame = pd.DataFrame({"Open": [1.0, 2.0], "High": [1.5, 2.5], "Low": [0.5, 1.5], "Close": [1.2, float("nan")], "Volume": [10, 20]}, index=index)
    bars = se.frame_to_bars(frame)
    assert len(bars) == 1 and bars[0].ts == datetime(2026, 11, 9, 14, 30, tzinfo=timezone.utc) and bars[0].close == 1.2
