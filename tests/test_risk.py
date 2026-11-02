"""Risikomotoren er deterministisk og kan ikke tilsidesættes af AI."""

from trader.risk import (
    RiskContext,
    apply_tighter_stop,
    daily_loss_hit,
    evaluate,
    next_error_count,
    position_qty,
    reward_risk,
)


def ctx(**overrides) -> RiskContext:
    base = dict(
        equity=100_000.0,
        buying_power=200_000.0,
        entry=100.0,
        stop=99.0,
        target=102.0,
        side="long",
        symbol="SPY",
        confidence=70,
        open_positions=0,
        open_symbols=set(),
        trades_today=0,
        day_start_equity=100_000.0,
        kill_switch=False,
        halted=False,
        mode="paper",
        daytrade_count=0,
        live_max_capital=1000.0,
        tighter_stop=None,
    )
    base.update(overrides)
    return RiskContext(**base)


def test_qty_is_min_of_risk_notional_and_buying_power():
    # 0,5 % af 100.000 = 500 USD. Risiko 1 USD/aktie -> 500 aktier.
    # 25 % notional = 25.000 / 100 = 250. Købekraft binder ikke.
    assert position_qty(100_000, 200_000, 100, 99) == 250

    # Købekraft binder: 90 % af 5.000 / 10 = 450.
    assert position_qty(100_000, 5_000, 10, 9.9) == 450

    # Risikostørrelsen binder, og der rundes ned til hele aktier.
    # 500 / 3 = 166, mens notional 25.000 / 300 = 83.
    assert position_qty(100_000, 500_000, 300, 297) == 83

    assert position_qty(1_000, 1_000, 500, 450) == 0


def test_live_cap_limits_size():
    qty = position_qty(100_000, 200_000, 100, 99, live_cap=1000)
    # Grundlag 1.000 USD: notional 250 / 100 = 2 aktier.
    assert qty == 2
    decision = evaluate(ctx(mode="live", equity=100_000, buying_power=200_000))
    assert decision.allowed
    assert decision.qty == 2


def test_confidence_reward_and_geometry():
    assert evaluate(ctx(confidence=65)).allowed
    blocked = evaluate(ctx(confidence=64))
    assert not blocked.allowed
    assert "65" in blocked.reason

    assert reward_risk("long", 100, 99, 101.5) == 1.5
    assert evaluate(ctx(target=101.5)).allowed
    too_small = evaluate(ctx(target=101.49))
    assert not too_small.allowed

    wrong = evaluate(ctx(target=99))
    assert not wrong.allowed
    assert "forkerte side" in wrong.reason


def test_trade_position_and_symbol_limits():
    assert evaluate(ctx(trades_today=3)).allowed
    assert not evaluate(ctx(trades_today=4)).allowed
    assert evaluate(ctx(open_positions=1)).allowed
    assert not evaluate(ctx(open_positions=2)).allowed
    assert not evaluate(ctx(open_symbols={"SPY"})).allowed
    assert evaluate(ctx(open_symbols={"QQQ"}, symbol="SPY")).allowed


def test_daily_loss_kill_switch_and_halt():
    assert not daily_loss_hit(100_000, 98_001)
    assert daily_loss_hit(100_000, 98_000)
    assert not evaluate(ctx(equity=98_000)).allowed
    assert not evaluate(ctx(kill_switch=True)).allowed
    assert not evaluate(ctx(halted=True)).allowed
    assert "Nødstop" in evaluate(ctx(kill_switch=True)).reason


def test_tighter_stop_only():
    assert apply_tighter_stop("long", 100, 98, 99) == 99
    assert apply_tighter_stop("long", 100, 98, 97) == 98
    assert apply_tighter_stop("long", 100, 98, 100) == 98
    assert apply_tighter_stop("long", 100, 98, 101) == 98
    assert apply_tighter_stop("short", 100, 102, 101) == 101
    assert apply_tighter_stop("short", 100, 102, 103) == 102
    assert apply_tighter_stop("short", 100, 102, 99) == 102

    tightened = evaluate(ctx(stop=98, target=104, tighter_stop=99))
    assert tightened.allowed
    assert tightened.stop == 99
    # Løsere stop ignoreres. Målet bliver stående, så forholdet ikke forringes.
    ignored = evaluate(ctx(stop=99, target=102, tighter_stop=97))
    assert ignored.allowed
    assert ignored.stop == 99


def test_pdt_only_in_live_under_25k():
    blocked = evaluate(
        ctx(mode="live", equity=20_000, buying_power=20_000, day_start_equity=20_000, daytrade_count=3)
    )
    assert not blocked.allowed
    assert "PDT" in blocked.reason
    assert evaluate(
        ctx(mode="live", equity=20_000, buying_power=20_000, day_start_equity=20_000, daytrade_count=2)
    ).allowed
    assert evaluate(
        ctx(mode="paper", equity=20_000, buying_power=20_000, day_start_equity=20_000, daytrade_count=5)
    ).allowed
    assert evaluate(
        ctx(mode="live", equity=25_000, buying_power=50_000, day_start_equity=25_000, daytrade_count=5)
    ).allowed
    assert evaluate(
        ctx(mode="sim", equity=10_000, buying_power=10_000, day_start_equity=10_000, daytrade_count=5)
    ).allowed


def test_three_errors_request_flatten():
    count, flatten = next_error_count(0)
    assert (count, flatten) == (1, False)
    count, flatten = next_error_count(count)
    assert (count, flatten) == (2, False)
    count, flatten = next_error_count(count)
    assert (count, flatten) == (3, True)


def test_short_reward_risk_boundary():
    assert evaluate(ctx(side="short", entry=100, stop=101, target=98.5)).allowed
    assert not evaluate(ctx(side="short", entry=100, stop=101, target=98.51)).allowed
