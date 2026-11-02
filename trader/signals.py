"""Mekaniske setups: åbningsudbrud og lukkemomentum. Ingen AI her."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from trader.config import ET_NAME, MARKET_PROXY

ET = ZoneInfo(ET_NAME)

ORB_RISK_MIN = 0.0015
ORB_RISK_MAX = 0.015
CLOSE_MIN_MOVE = 0.005
CLOSE_STOP_MIN = 0.002
CLOSE_STOP_MAX = 0.008


@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Candidate:
    symbol: str
    side: str
    setup: str
    entry: float
    stop: float
    target: float
    gap_pct: float
    change_from_open_pct: float
    vs_vwap_pct: float
    trend_20d_pct: float
    adr_pct: float
    or_high: float
    or_low: float
    or_mid: float
    vwap: float
    day_open: float
    last_price: float
    bias: int


def market_bias(price: float, day_open: float, vwap_value: float) -> int:
    if price > day_open and price > vwap_value:
        return 1
    if price < day_open and price < vwap_value:
        return -1
    return 0


def vwap(bars: list[Bar]) -> float | None:
    total_pv = 0.0
    total_v = 0.0
    for bar in bars:
        typical = (bar.high + bar.low + bar.close) / 3.0
        total_pv += typical * bar.volume
        total_v += bar.volume
    if total_v <= 0:
        return None
    return total_pv / total_v


def opening_range(bars: list[Bar], day) -> tuple[float, float, float] | None:
    start = datetime.combine(day, time(9, 30), tzinfo=ET)
    end = datetime.combine(day, time(9, 45), tzinfo=ET)
    chosen = [bar for bar in bars if start <= bar.ts < end]
    if not chosen:
        return None
    hi = max(bar.high for bar in chosen)
    lo = min(bar.low for bar in chosen)
    if hi <= lo:
        return None
    return hi, lo, (hi + lo) / 2.0


def vwap_stop(side: str, entry: float, vwap_value: float) -> float | None:
    """Stop i VWAP, højst 0,8 % fra indgangen og mindst 0,2 %."""
    if entry <= 0 or vwap_value <= 0:
        return None
    if side == "long":
        if vwap_value >= entry:
            return None
        distance = (entry - vwap_value) / entry
        if distance > CLOSE_STOP_MAX:
            return entry * (1 - CLOSE_STOP_MAX)
        if distance < CLOSE_STOP_MIN:
            return entry * (1 - CLOSE_STOP_MIN)
        return vwap_value
    if side == "short":
        if vwap_value <= entry:
            return None
        distance = (vwap_value - entry) / entry
        if distance > CLOSE_STOP_MAX:
            return entry * (1 + CLOSE_STOP_MAX)
        if distance < CLOSE_STOP_MIN:
            return entry * (1 + CLOSE_STOP_MIN)
        return vwap_value
    return None


def _round_price(value: float) -> float:
    return round(value + 1e-9, 2)


def _bracket(side: str, entry: float, stop: float, multiple: float) -> tuple[float, float, float] | None:
    entry_r = _round_price(entry)
    stop_r = _round_price(stop)
    if side == "long":
        if stop_r >= entry_r:
            return None
        risk = entry_r - stop_r
        target = _round_price(entry_r + multiple * risk)
        guard = 0
        while target <= entry_r or (target - entry_r) / risk < multiple - 1e-6:
            target = _round_price(target + 0.01)
            guard += 1
            if guard > 5:
                break
        if target <= entry_r:
            return None
        return entry_r, stop_r, target
    if side == "short":
        if stop_r <= entry_r:
            return None
        risk = stop_r - entry_r
        target = _round_price(entry_r - multiple * risk)
        guard = 0
        while target >= entry_r or (entry_r - target) / risk < multiple - 1e-6:
            target = _round_price(target - 0.01)
            guard += 1
            if guard > 5:
                break
        if target >= entry_r:
            return None
        return entry_r, stop_r, target
    return None


def _completed(bars: list[Bar], now: datetime) -> list[Bar]:
    # En 1-minuts bar er først færdig, når minutten er gået. Den bar, der er i gang, bruges ikke.
    return [bar for bar in bars if bar.ts + timedelta(minutes=1) <= now]


def _price_at(bars: list[Bar], ts: datetime) -> float | None:
    for bar in bars:
        if bar.ts == ts:
            return bar.close
    return None


def _trend_and_adr(daily: list[Bar]) -> tuple[float, float]:
    if len(daily) < 2:
        return 0.0, 0.0
    first = daily[0].close
    last = daily[-1].close
    trend = (last - first) / first if first else 0.0
    ranges = [(bar.high - bar.low) / bar.close for bar in daily if bar.close > 0]
    adr = sum(ranges) / len(ranges) if ranges else 0.0
    return trend, adr


def _metrics(symbol_bars: list[Bar], daily: list[Bar], now: datetime) -> dict | None:
    bars = _completed(symbol_bars, now)
    if not bars:
        return None
    day_open = bars[0].open
    price = bars[-1].close
    vwap_value = vwap(bars)
    if day_open <= 0 or price <= 0 or vwap_value is None:
        return None
    prev_close = daily[-1].close if daily else 0.0
    gap = (day_open - prev_close) / prev_close if prev_close else 0.0
    trend, adr = _trend_and_adr(daily)
    or_levels = opening_range(bars, now.date())
    ago = _price_at(bars, bars[-1].ts - timedelta(minutes=30))
    return {
        "bars": bars,
        "day_open": day_open,
        "price": price,
        "vwap": vwap_value,
        "gap_pct": gap,
        "change_from_open_pct": (price - day_open) / day_open,
        "vs_vwap_pct": (price - vwap_value) / vwap_value,
        "trend_20d_pct": trend,
        "adr_pct": adr,
        "or": or_levels,
        "price_30m": ago,
    }


def _candidate(symbol: str, side: str, setup: str, metrics: dict, bias: int, bracket) -> Candidate | None:
    if bracket is None:
        return None
    entry, stop, target = bracket
    or_high, or_low, or_mid = metrics["or"] if metrics["or"] else (0.0, 0.0, 0.0)
    return Candidate(
        symbol=symbol,
        side=side,
        setup=setup,
        entry=entry,
        stop=stop,
        target=target,
        gap_pct=metrics["gap_pct"],
        change_from_open_pct=metrics["change_from_open_pct"],
        vs_vwap_pct=metrics["vs_vwap_pct"],
        trend_20d_pct=metrics["trend_20d_pct"],
        adr_pct=metrics["adr_pct"],
        or_high=or_high,
        or_low=or_low,
        or_mid=or_mid,
        vwap=metrics["vwap"],
        day_open=metrics["day_open"],
        last_price=metrics["price"],
        bias=bias,
    )


def _orb(metrics: dict, bias: int) -> tuple[str, tuple[float, float, float]] | None:
    levels = metrics["or"]
    if levels is None:
        return None
    or_high, or_low, or_mid = levels
    price = metrics["price"]
    vwap_value = metrics["vwap"]
    long_ok = price > or_high and price > vwap_value and bias >= 0
    short_ok = price < or_low and price < vwap_value and bias <= 0
    if not long_ok and not short_ok:
        return None
    side = "long" if long_ok else "short"
    breakout = or_high if side == "long" else or_low
    unit = abs(breakout - or_mid)
    if unit <= 0:
        return None
    # Ingen jagt: prisen må ikke være løbet mere end 1R forbi udbruddet.
    if side == "long" and price > breakout + unit + 1e-9:
        return None
    if side == "short" and price < breakout - unit - 1e-9:
        return None
    bracket = _bracket(side, price, or_mid, 2.0)
    if bracket is None:
        return None
    entry, stop, _target = bracket
    risk_pct = abs(entry - stop) / entry
    if risk_pct < ORB_RISK_MIN or risk_pct > ORB_RISK_MAX:
        return None
    return side, bracket


def _close(metrics: dict, bias: int) -> tuple[str, tuple[float, float, float]] | None:
    price = metrics["price"]
    change = metrics["change_from_open_pct"]
    vwap_value = metrics["vwap"]
    previous = metrics["price_30m"]
    if previous is None:
        return None
    long_ok = change > CLOSE_MIN_MOVE and price > vwap_value and price > previous and bias > 0
    short_ok = change < -CLOSE_MIN_MOVE and price < vwap_value and price < previous and bias < 0
    if not long_ok and not short_ok:
        return None
    side = "long" if long_ok else "short"
    raw_stop = vwap_stop(side, price, vwap_value)
    if raw_stop is None:
        return None
    bracket = _bracket(side, price, raw_stop, 1.5)
    if bracket is None:
        return None
    return side, bracket


def find_candidates(
    intraday: dict[str, list[Bar]],
    daily: dict[str, list[Bar]],
    window: str,
    now: datetime,
    universe: tuple[str, ...] | list[str],
) -> list[Candidate]:
    if window not in {"open", "close"}:
        return []
    spy = _metrics(intraday.get(MARKET_PROXY, []), daily.get(MARKET_PROXY, []), now)
    if spy is None:
        bias = 0
    else:
        bias = market_bias(spy["price"], spy["day_open"], spy["vwap"])

    found: list[Candidate] = []
    for symbol in universe:
        metrics = _metrics(intraday.get(symbol, []), daily.get(symbol, []), now)
        if metrics is None:
            continue
        if window == "open":
            setup = _orb(metrics, bias)
            name = "orb"
        else:
            setup = _close(metrics, bias)
            name = "close"
        if setup is None:
            continue
        side, bracket = setup
        candidate = _candidate(symbol, side, name, metrics, bias, bracket)
        if candidate is not None:
            found.append(candidate)
    return found


def active_window(now: datetime) -> str | None:
    if now.minute % 5 != 0:
        return None
    stamp = (now.hour, now.minute)
    if (9, 45) <= stamp <= (10, 30):
        return "open"
    if (15, 0) <= stamp <= (15, 30):
        return "close"
    return None
