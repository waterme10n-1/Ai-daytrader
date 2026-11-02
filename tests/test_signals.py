"""Mekaniske setups: bias, åbningsudbrud og lukkemomentum."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from trader.broker import build_day_bars
from trader.config import UNIVERSE
from trader.signals import Bar, find_candidates, market_bias, vwap_stop

ET = ZoneInfo("America/New_York")
DAY = date(2026, 6, 1)


def _bar(offset: int, price: float, high: float | None = None, low: float | None = None, volume: float = 10_000) -> Bar:
    start = datetime.combine(DAY, time(9, 30), tzinfo=ET)
    return Bar(
        ts=start + timedelta(minutes=offset),
        open=price,
        high=price if high is None else high,
        low=price if low is None else low,
        close=price,
        volume=volume,
    )


def test_market_bias():
    assert market_bias(101, 100, 100.5) == 1
    assert market_bias(99, 100, 99.5) == -1
    assert market_bias(101, 100, 101.5) == 0
    assert market_bias(99, 100, 98) == 0


def test_vwap_stop_caps_distance():
    # Mere end 0,8 % væk skæres til 0,8 %.
    assert abs(vwap_stop("long", 102, 100) - 102 * 0.992) < 1e-9
    # Tættere end 0,2 % skubbes ud til 0,2 %.
    assert abs(vwap_stop("long", 100.10, 100.05) - 100.10 * 0.998) < 1e-9
    # Inden for båndet bruges VWAP.
    assert vwap_stop("long", 101, 100.4) == 100.4
    assert vwap_stop("long", 100, 100.5) is None
    assert abs(vwap_stop("short", 98, 100) - 98 * 1.008) < 1e-9
    assert vwap_stop("short", 100, 99) is None


def test_opening_range_breakout_long_and_filters():
    bars = []
    for offset in range(15):
        price = 100.0
        high = 101.0 if offset == 8 else 100.2
        low = 99.0 if offset == 4 else 99.8
        bars.append(_bar(offset, price, high, low))
    for offset, price in enumerate([101.1, 101.15, 101.2, 101.25, 101.3], start=15):
        bars.append(_bar(offset, price, price + 0.05, price - 0.05))
    now = datetime.combine(DAY, time(9, 50), tzinfo=ET)
    found = find_candidates({"SPY": bars}, {"SPY": []}, "open", now, ("SPY",))
    assert len(found) == 1
    assert found[0].side == "long"
    assert found[0].setup == "orb"
    assert found[0].stop == 100.0
    assert found[0].entry == 101.3
    assert found[0].target == round(101.3 + 2 * (101.3 - 100), 2)

    # Allerede løbet mere end 1R forbi udbruddet.
    chased = bars[:-1] + [_bar(19, 102.5, 102.6, 102.2)]
    assert find_candidates({"SPY": chased}, {"SPY": []}, "open", now, ("SPY",)) == []

    # Risiko under 0,15 % af kursen.
    tight = []
    for offset in range(15):
        tight.append(_bar(offset, 100, 100.10 if offset == 8 else 100.02, 99.90 if offset == 4 else 99.98))
    for offset in range(15, 20):
        tight.append(_bar(offset, 100.12, 100.14, 100.10))
    assert find_candidates({"SPY": tight}, {"SPY": []}, "open", now, ("SPY",)) == []


def test_close_momentum_long_on_synthetic_afternoon():
    intraday, daily = build_day_bars(DAY, "close_long_win", ("SPY", "QQQ"))
    now = datetime.combine(DAY, time(15, 0), tzinfo=ET)
    morning = datetime.combine(DAY, time(10, 0), tzinfo=ET)
    assert find_candidates(intraday, daily, "open", morning, ("SPY", "QQQ")) == []
    found = find_candidates(intraday, daily, "close", now, ("SPY", "QQQ"))
    assert {item.symbol for item in found} == {"SPY", "QQQ"}
    assert all(item.side == "long" and item.setup == "close" for item in found)


def test_chop_day_has_no_setup():
    intraday, daily = build_day_bars(DAY, "chop", ("SPY",))
    morning = datetime.combine(DAY, time(10, 0), tzinfo=ET)
    afternoon = datetime.combine(DAY, time(15, 0), tzinfo=ET)
    assert find_candidates(intraday, daily, "open", morning, ("SPY",)) == []
    assert find_candidates(intraday, daily, "close", afternoon, ("SPY",)) == []
