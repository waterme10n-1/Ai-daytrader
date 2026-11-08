"""Morgen-screener og købssignaler til daytrading i USA (NASDAQ/NYSE) og København (C25).

1. **Momentum før åbningen**: aktier med relativ volumen (RVOL) over 2,0 og en kursændring på mindst
   ±3 % i forhold til gårsdagens lukkekurs.
2. **Indikatorer** på 5-minutters lys: EMA 9, EMA 20, VWAP (fra dagens åbning) og RSI 14.
3. **Købssignal**: et lys lukker over VWAP efter at have ligget under (et brud), EMA 9 ligger over EMA 20,
   og RSI er mellem 50 og 70.
4. **Svar**: entry (lukkekursen på signal-lyset), stop-loss lige under seneste swing-bund og et kursmål
   på mindst 2 gange risikoen (1:2).

**Tider.** New York åbner 9:30 lokal tid. Det er normalt 15:30 dansk tid, men 14:30 i de uger forår og
efterår, hvor USA og Europa skifter sommertid på forskellige datoer. Derfor regnes alt i børsens egen
tidszone (`trader.desk.markets`), aldrig med en fast dansk klokkeslæt.

**København** har ingen handel før åbningen (kun en åbningsauktion). Dér måles momentum og RVOL på de
første 15 minutter efter 9:00, så screeneren giver først mening fra 9:15.

**RVOL** er volumen i vinduet i dag delt med gennemsnittet i samme vindue (op til samme klokkeslæt) de
foregående handelsdage.

**Data** hentes med yfinance (`pip install -r requirements-quant.txt`). Indikatorerne bruger TA-Lib, hvis
det er installeret, ellers de samme formler i ren Python (EMA startet med et simpelt gennemsnit, RSI med
Wilders udjævning), så resultaterne er ens.

    python -m trader.quant.strategy_engine --market us
    python -m trader.quant.strategy_engine --market dk NOVO-B MAERSK-B
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from trader.desk import markets
from trader.desk.alpaca import Bar
from trader.desk.config import DEFAULT_WATCHLIST, DK_WATCHLIST
from trader.desk.denmark import yahoo_symbol
from trader.desk.markets import Market

BAR = timedelta(minutes=5)
DANISH = ZoneInfo("Europe/Copenhagen")
OPENING_WINDOW_MINUTES = 15


# --------------------------------------------------------------------------- indikatorer


def _talib():
    try:
        import numpy
        import talib
    except Exception:  # TA-Lib er valgfrit
        return None
    return talib, numpy


def _ema_python(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period < 1 or len(values) < period:
        return out
    current = sum(values[:period]) / period  # som TA-Lib: start med et simpelt gennemsnit
    out[period - 1] = current
    k = 2.0 / (period + 1)
    for index in range(period, len(values)):
        current = values[index] * k + current * (1 - k)
        out[index] = current
    return out


def _rsi_python(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period < 1 or len(values) <= period:
        return out
    gains = losses = 0.0
    for index in range(1, period + 1):
        change = values[index] - values[index - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    gain, loss = gains / period, losses / period

    def value(g: float, l: float) -> float:
        return 100.0 * g / (g + l) if g + l > 0 else 0.0  # som TA-Lib: 0 når intet har bevæget sig

    out[period] = value(gain, loss)
    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        gain = (gain * (period - 1) + max(change, 0.0)) / period
        loss = (loss * (period - 1) + max(-change, 0.0)) / period
        out[index] = value(gain, loss)
    return out


def _clean(series) -> list[float | None]:
    return [None if value is None or math.isnan(float(value)) else float(value) for value in series]


def ema(values: Sequence[float], period: int) -> list[float | None]:
    lib = _talib()
    if lib and len(values) >= period:
        talib, numpy = lib
        return _clean(talib.EMA(numpy.asarray(values, dtype=float), timeperiod=period))
    return _ema_python(values, period)


def rsi(values: Sequence[float], period: int = 14) -> list[float | None]:
    lib = _talib()
    if lib and len(values) > period:
        talib, numpy = lib
        return _clean(talib.RSI(numpy.asarray(values, dtype=float), timeperiod=period))
    return _rsi_python(values, period)


def session_vwap(bars: Sequence[Bar]) -> list[float]:
    """Løbende VWAP fra første lys (dagens åbning). Typisk kurs = (høj + lav + luk) / 3."""
    out = []
    total_pv = total_v = 0.0
    for bar in bars:
        total_pv += (bar.high + bar.low + bar.close) / 3.0 * bar.volume
        total_v += bar.volume
        out.append(total_pv / total_v if total_v > 0 else bar.close)
    return out


def swing_low(bars: Sequence[Bar], end: int, left: int = 2, right: int = 2) -> float | None:
    """Seneste bekræftede swing-bund før eller på lys `end`: et lavpunkt med `left` lys før og `right` lys efter, der
    alle ligger højere eller lige så højt. Findes ingen, bruges det laveste af de seneste seks lys."""
    for index in range(end - right, left - 1, -1):
        low = bars[index].low
        if all(bars[j].low >= low for j in range(index - left, index + right + 1) if j != index):
            return low
    recent = bars[max(0, end - 5) : end + 1]
    return min(bar.low for bar in recent) if recent else None


# --------------------------------------------------------------------------- tid og sessioner


def _local(bar: Bar, market: Market) -> datetime:
    return bar.ts.astimezone(market.tz)


def regular_bars(bars: Sequence[Bar], market: Market) -> list[Bar]:
    """Kun lys i den almindelige handelstid (også på halve dage)."""
    out = []
    for bar in bars:
        local = _local(bar, market)
        if market.open <= local.time() < market.close_on(local.date()):
            out.append(bar)
    return out


def completed(bars: Sequence[Bar], now: datetime) -> list[Bar]:
    return [bar for bar in bars if bar.ts + BAR <= now]


def open_in_denmark(market: Market, day: date) -> datetime:
    """Hvornår børsen åbner i dansk tid den dag (fx 15:30 eller 14:30 for New York)."""
    return datetime.combine(day, market.open, tzinfo=market.tz).astimezone(DANISH)


# --------------------------------------------------------------------------- 1. momentum før åbningen


@dataclass(frozen=True)
class Momentum:
    symbol: str
    market: str
    window: str
    prev_close: float
    price: float
    change_pct: float
    volume: float
    avg_volume: float
    rvol: float | None


def _window(market: Market, day: date, now_local: datetime) -> tuple[datetime, datetime]:
    if market.premarket_trading:
        start, end = datetime.combine(day, market.pre_open, tzinfo=market.tz), datetime.combine(day, market.open, tzinfo=market.tz)
    else:
        start = datetime.combine(day, market.open, tzinfo=market.tz)
        end = start + timedelta(minutes=OPENING_WINDOW_MINUTES)
    # Samme klokkeslæt alle dage: før kl. 9:30 sammenlignes kun op til nu.
    cutoff = datetime.combine(day, min(now_local.time(), end.time()), tzinfo=market.tz)
    return start, min(end, cutoff)


def momentum(symbol: str, bars: Sequence[Bar], market: Market, now: datetime, *, lookback_days: int = 10) -> Momentum | None:
    now_local = now.astimezone(market.tz)
    today = now_local.date()

    def window_volume(day: date) -> tuple[float, list[Bar]]:
        start, end = _window(market, day, now_local)
        inside = [bar for bar in bars if start <= _local(bar, market) and _local(bar, market) + BAR <= end]
        return sum(bar.volume for bar in inside), inside

    volume, today_bars = window_volume(today)
    if not today_bars:
        return None
    past_days = sorted({_local(bar, market).date() for bar in bars if _local(bar, market).date() < today})[-lookback_days:]
    past = [window_volume(day)[0] for day in past_days]
    average = sum(past) / len(past) if past else 0.0
    before = [bar for bar in regular_bars(bars, market) if _local(bar, market).date() < today]
    if not before:
        return None
    prev_close = before[-1].close
    price = today_bars[-1].close
    label = "før åbningen" if market.premarket_trading else f"første {OPENING_WINDOW_MINUTES} min."
    return Momentum(
        symbol=symbol,
        market=market.key,
        window=label,
        prev_close=prev_close,
        price=price,
        change_pct=(price / prev_close - 1.0) * 100.0 if prev_close > 0 else 0.0,
        volume=volume,
        avg_volume=average,
        rvol=volume / average if average > 0 else None,
    )


def screen(items: Sequence[Momentum | None], *, min_rvol: float = 2.0, min_move_pct: float = 3.0) -> list[Momentum]:
    """RVOL over 2,0 og en bevægelse på mindst ±3 %. Højeste RVOL først."""
    hits = [item for item in items if item and item.rvol is not None and item.rvol > min_rvol and abs(item.change_pct) >= min_move_pct]
    return sorted(hits, key=lambda item: item.rvol or 0.0, reverse=True)


# --------------------------------------------------------------------------- 2-4. indikatorer, signal, entry/stop/mål


@dataclass(frozen=True)
class Signal:
    symbol: str
    market: str
    at: str
    entry: float
    stop_loss: float
    target: float
    risk_per_share: float
    reward_risk: float
    swing_low: float
    ema9: float
    ema20: float
    vwap: float
    rsi: float

    def to_dict(self) -> dict:
        return asdict(self)


def analyze(bars: Sequence[Bar], market: Market, now: datetime) -> dict | None:
    """Indikatorerne på det seneste færdige lys i dag, og om betingelserne er opfyldt."""
    regular = completed(regular_bars(bars, market), now)
    today = now.astimezone(market.tz).date()
    today_idx = [index for index, bar in enumerate(regular) if _local(bar, market).date() == today]
    if len(today_idx) < 2 or len(regular) < 21:
        return None
    closes = [bar.close for bar in regular]
    e9, e20, r = ema(closes, 9), ema(closes, 20), rsi(closes, 14)
    first = today_idx[0]
    vw = session_vwap(regular[first:])
    i = today_idx[-1]
    prev_vwap, cur_vwap = vw[i - 1 - first], vw[i - first]
    return {
        "index": i,
        "bars": regular,
        "close": closes[i],
        "vwap": cur_vwap,
        "ema9": e9[i],
        "ema20": e20[i],
        "rsi": r[i],
        "vwap_break": closes[i - 1] <= prev_vwap and closes[i] > cur_vwap,
        "trend_up": e9[i] is not None and e20[i] is not None and e9[i] > e20[i],
        "rsi_ok": r[i] is not None and 50.0 <= r[i] <= 70.0,
    }


def find_signal(
    symbol: str,
    bars: Sequence[Bar],
    market: Market,
    now: datetime,
    *,
    reward_risk: float = 2.0,
    stop_buffer_pct: float = 0.001,
    min_stop_pct: float = 0.001,
    max_stop_pct: float = 0.05,
) -> Signal | None:
    """Købssignal på det seneste færdige lys, eller None.

    Stoppet ligger 0,1 % under swing-bunden. Er stoppet tættere end 0,1 % eller længere væk end 5 % fra entry,
    springes signalet over: for tæt bliver ramt af støj, for langt væk giver en for lille position."""
    if reward_risk < 2.0:
        raise ValueError("Gevinst mod risiko skal være mindst 1:2.")
    state = analyze(bars, market, now)
    if not state or not (state["vwap_break"] and state["trend_up"] and state["rsi_ok"]):
        return None
    i, regular = state["index"], state["bars"]
    low = swing_low(regular, i)
    if low is None:
        return None
    entry = round(state["close"], 2)
    stop = math.floor(low * (1.0 - stop_buffer_pct) * 100) / 100
    risk = entry - stop
    if risk <= entry * min_stop_pct or risk > entry * max_stop_pct:
        return None
    target = math.ceil((entry + reward_risk * risk) * 100) / 100
    return Signal(
        symbol=symbol,
        market=market.key,
        at=_local(regular[i], market).isoformat(timespec="minutes"),
        entry=entry,
        stop_loss=stop,
        target=target,
        risk_per_share=round(risk, 4),
        reward_risk=round((target - entry) / risk, 2),
        swing_low=low,
        ema9=round(state["ema9"], 4),
        ema20=round(state["ema20"], 4),
        vwap=round(state["vwap"], 4),
        rsi=round(state["rsi"], 2),
    )


# --------------------------------------------------------------------------- data og kørsel


def frame_to_bars(frame) -> list[Bar]:
    """En yfinance-tabel (Open/High/Low/Close/Volume, tid som indeks) → Bar-liste i UTC."""
    out = []
    for stamp, row in frame.iterrows():
        close = row.get("Close")
        if close is None or close != close:
            continue
        moment = stamp.to_pydatetime()
        moment = moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)
        volume = row.get("Volume")
        out.append(Bar(moment, float(row["Open"]), float(row["High"]), float(row["Low"]), float(close), float(volume) if volume == volume and volume is not None else 0.0))
    return out


def fetch_bars(symbols: Sequence[str], market: Market, *, days: int = 10, interval: str = "5m") -> dict[str, list[Bar]]:
    try:
        import pandas as pd
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover - afhænger af installationen
        raise RuntimeError("yfinance mangler. Kør: pip install -r requirements-quant.txt") from exc
    tickers = {symbol: yahoo_symbol(symbol) if market.key == "dk" else symbol for symbol in symbols}
    data = yf.download(
        tickers=list(tickers.values()),
        period=f"{days}d",
        interval=interval,
        prepost=market.premarket_trading,
        group_by="ticker",
        auto_adjust=False,
        progress=False,
        threads=True,
    )
    out: dict[str, list[Bar]] = {}
    for symbol, ticker in tickers.items():
        if isinstance(data.columns, pd.MultiIndex):
            if ticker not in data.columns.get_level_values(0):
                continue
            frame = data[ticker]
        else:
            frame = data
        out[symbol] = frame_to_bars(frame.dropna(how="all"))
    return out


def run(
    market_key: str = "us",
    symbols: Sequence[str] | None = None,
    now: datetime | None = None,
    *,
    fetch: Callable[[Sequence[str], Market], dict[str, list[Bar]]] = fetch_bars,
    min_rvol: float = 2.0,
    min_move_pct: float = 3.0,
) -> dict:
    market = markets.get(market_key)
    now = now or datetime.now(timezone.utc)
    symbols = list(symbols or (DK_WATCHLIST if market.key == "dk" else DEFAULT_WATCHLIST))
    bars = fetch(symbols, market)
    moves = [momentum(symbol, bars.get(symbol, []), market, now) for symbol in symbols]
    candidates = screen(moves, min_rvol=min_rvol, min_move_pct=min_move_pct)
    signals = []
    for item in candidates:
        signal = find_signal(item.symbol, bars.get(item.symbol, []), market, now)
        if signal:
            signals.append(signal.to_dict())
    day = now.astimezone(market.tz).date()
    return {
        "market": market.key,
        "checked_at": now.isoformat(timespec="seconds"),
        "opens_danish_time": open_in_denmark(market, day).strftime("%H:%M"),
        "screened": len([item for item in moves if item]),
        "candidates": [asdict(item) for item in candidates],
        "signals": signals,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Morgen-screener og købssignaler. Svarer med JSON.")
    parser.add_argument("--market", choices=["us", "dk"], default="us")
    parser.add_argument("symbols", nargs="*", help="tickere (standard: bordets watchlist)")
    args = parser.parse_args(argv)
    result = run(args.market, args.symbols or None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
