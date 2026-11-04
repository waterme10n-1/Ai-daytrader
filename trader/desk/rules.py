"""De tre bots, der regner: SCOUT, CHART og SIZER. Ren kode, så tallene aldrig er opdigtet.

Plus skyggeregnskabet, der bagefter tjekker, om de handler, SKEPTIC sagde nej til, ville have tabt.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from trader.broker import resolve_intrabar
from trader.signals import Bar, _bracket, _round_price, vwap

MIN_BARS = 15
SWING_BARS = 15
MIN_STOP_PCT = 0.3
LATEST_ENTRY = (15, 30)


def completed(bars: list[Bar], now: datetime) -> list[Bar]:
    return [bar for bar in bars if bar.ts + timedelta(minutes=1) <= now]


# ---------------------------------------------------------------------- SCOUT
@dataclass
class Flag:
    symbol: str
    price: float
    move_pct: float
    window_minutes: int
    volume_x: float
    vwap: float
    vs_vwap_pct: float
    day_change_pct: float
    by_move: bool
    by_volume: bool
    ts: str

    def to_dict(self) -> dict:
        return asdict(self)


def normal_volume_per_minute(daily: list[Bar]) -> float:
    volumes = [bar.volume for bar in daily if bar.volume > 0]
    if not volumes:
        return 0.0
    return (sum(volumes) / len(volumes)) / 390.0


def scout(symbol: str, intraday: list[Bar], daily: list[Bar], now: datetime, *, move_pct: float, volume_x: float) -> Flag | None:
    """Flag ved bevægelse på mindst move_pct på en time eller volumen på mindst volume_x gange det normale."""
    bars = completed(intraday, now)
    if len(bars) < MIN_BARS:
        return None
    last = bars[-1]
    start = last.ts - timedelta(minutes=60)
    window = [bar for bar in bars if bar.ts > start]
    ref = window[0].open if window else bars[0].open
    if ref <= 0 or last.close <= 0:
        return None
    move = (last.close - ref) / ref * 100.0
    minutes = max(1, len(window))
    per_minute = normal_volume_per_minute(daily)
    ratio = (sum(bar.volume for bar in window) / minutes) / per_minute if per_minute > 0 else 0.0
    by_move = abs(move) >= move_pct - 1e-9
    by_volume = ratio >= volume_x - 1e-9
    if not by_move and not by_volume:
        return None
    level = vwap(bars) or last.close
    day_open = bars[0].open
    return Flag(
        symbol=symbol,
        price=round(last.close, 2),
        move_pct=round(move, 2),
        window_minutes=minutes,
        volume_x=round(ratio, 1),
        vwap=round(level, 2),
        vs_vwap_pct=round((last.close - level) / level * 100.0, 2) if level else 0.0,
        day_change_pct=round((last.close - day_open) / day_open * 100.0, 2) if day_open else 0.0,
        by_move=by_move,
        by_volume=by_volume,
        ts=last.ts.isoformat(timespec="minutes"),
    )


def scout_text(flag: Flag) -> str:
    move = f"{flag.move_pct:+.1f}%"
    span = "den sidste time" if flag.window_minutes >= 60 else f"de sidste {flag.window_minutes} min"
    return f"FLAG: {flag.symbol} {move} {span}, handler {flag.volume_x:.1f}x normal volumen. Kurs {flag.price:.2f}."


# ---------------------------------------------------------------------- CHART
@dataclass
class ChartPlan:
    setup: bool
    entry: float = 0.0
    stop: float = 0.0
    target: float = 0.0
    stop_pct: float = 0.0
    reward_risk: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def chart(
    flag: Flag,
    intraday: list[Bar],
    now: datetime,
    *,
    stop_max_pct: float,
    reward_risk: float,
) -> ChartPlan:
    """Indgang, mål og stop for et køb. Stoppet er højst stop_max_pct under indgangen, målet mindst reward_risk gange så langt væk."""
    bars = completed(intraday, now)
    if len(bars) < MIN_BARS:
        return ChartPlan(False, reason="For få minutter med data til et chart.")
    if (now.hour, now.minute) >= LATEST_ENTRY:
        return ChartPlan(False, reason="For sent på dagen. Alt lukkes kl. 15:50, så der er ikke tid til målet.")
    price = bars[-1].close
    level = vwap(bars) or price
    if flag.move_pct < 0:
        return ChartPlan(False, reason="Kursen falder, og bordet køber kun (ingen short).")
    if price <= level:
        return ChartPlan(False, reason=f"Kursen {price:.2f} er under VWAP {level:.2f}. Køberne har ikke kontrollen.")
    extension = (price - level) / level * 100.0
    if extension > 2 * stop_max_pct:
        return ChartPlan(False, reason=f"Kursen er {extension:.1f}% over VWAP. Det er at jagte en løbet bevægelse.")
    if len(bars) > 5 and price < bars[-6].close:
        return ChartPlan(False, reason="Momentum er vendt de sidste fem minutter.")
    swing = min(bar.low for bar in bars[-SWING_BARS:]) - 0.01
    floor = price * (1 - stop_max_pct / 100.0)
    stop = max(swing, floor)
    if price - stop < price * MIN_STOP_PCT / 100.0:
        stop = price * (1 - MIN_STOP_PCT / 100.0)
    bracket = _bracket("long", price, stop, reward_risk)
    if bracket is None:
        return ChartPlan(False, reason="Ingen ren opstilling: stop og mål kunne ikke placeres.")
    entry, stop_r, target = bracket
    # Afrunding må aldrig gøre stoppet bredere end loftet.
    guard = 0
    while (entry - stop_r) / entry * 100.0 > stop_max_pct + 1e-9 and guard < 5:
        stop_r = _round_price(stop_r + 0.01)
        guard += 1
    if stop_r >= entry:
        return ChartPlan(False, reason="Ingen ren opstilling: stoppet ville ligge over indgangen.")
    risk = entry - stop_r
    while (target - entry) / risk < reward_risk - 1e-9:
        target = _round_price(target + 0.01)
    stop_pct = risk / entry * 100.0
    if stop_pct > stop_max_pct + 1e-9:
        return ChartPlan(False, reason="Stoppet ville være for bredt.")
    return ChartPlan(
        True,
        entry=entry,
        stop=stop_r,
        target=target,
        stop_pct=round(stop_pct, 2),
        reward_risk=round((target - entry) / risk, 2),
        reason=f"Over VWAP {level:.2f}, stop under laveste kurs de sidste {SWING_BARS} min." if stop_r > floor + 0.005 else f"Over VWAP {level:.2f}, stop ved loftet på {stop_max_pct:g}%.",
    )


def chart_text(symbol: str, plan: ChartPlan) -> str:
    if not plan.setup:
        return f"{symbol}: Ingen setup. {plan.reason}"
    return (
        f"{symbol}: indgang {plan.entry:.2f}, mål {plan.target:.2f}, stop {plan.stop:.2f}. "
        f"Stoppet er {plan.stop_pct:.1f}% væk, og målet er {plan.reward_risk:.1f}x så langt."
    )


# ---------------------------------------------------------------------- SIZER
@dataclass
class Size:
    qty: int
    notional: float
    pct: float
    open_after: int
    max_open: int
    max_loss: float
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def size(
    *,
    equity: float,
    buying_power: float,
    entry: float,
    stop: float,
    open_count: int,
    position_pct: float,
    max_open: int,
    loss_limit_hit: bool,
) -> Size:
    """Dollarbeløb under et hårdt loft. Er en grænse ramt, er svaret nul."""
    zero = Size(0, 0.0, 0.0, open_count, max_open, 0.0, "")
    if loss_limit_hit:
        zero.reason = "Dagens tabsgrænse er ramt. Svaret er nul."
        return zero
    if open_count >= max_open:
        zero.reason = f"Der er allerede {open_count} af {max_open} handler åbne eller ventende. Svaret er nul."
        return zero
    if equity <= 0 or entry <= 0 or not math.isfinite(entry):
        zero.reason = "Kontoen eller kursen kunne ikke læses. Svaret er nul."
        return zero
    budget = min(equity * position_pct / 100.0, max(0.0, buying_power) * 0.9)
    qty = int(math.floor(budget / entry))
    if qty < 1:
        zero.reason = f"{position_pct:g}% af kontoen rækker ikke til én aktie. Svaret er nul."
        return zero
    notional = qty * entry
    return Size(
        qty=qty,
        notional=round(notional, 2),
        pct=round(notional / equity * 100.0, 2),
        open_after=open_count + 1,
        max_open=max_open,
        max_loss=round(qty * max(0.0, entry - stop), 2),
        reason="",
    )


def size_text(symbol: str, sized: Size) -> str:
    if sized.qty < 1:
        return f"{symbol}: 0. {sized.reason}"
    return (
        f"{symbol}: {sized.qty} aktier for {sized.notional:,.0f} USD ({sized.pct:.1f}% af kontoen). "
        f"Største tab ved stop: {sized.max_loss:,.0f} USD. Åbne handler efter denne: {sized.open_after} af {sized.max_open}."
    )


# ---------------------------------------------------------------------- SKEPTIC uden Claude
def rules_skeptic(flag: dict, news: dict, plan: dict, now: datetime, *, stop_max_pct: float) -> dict:
    """Reserve når Claude ikke er sat op. Den er strengere end Claude, ikke mildere."""
    reasons: list[str] = []
    move = float(flag.get("move_pct") or 0)
    vs_vwap = float(flag.get("vs_vwap_pct") or 0)
    if flag.get("by_move"):
        reasons.append(f"Aktien har allerede flyttet sig {move:+.1f}%. En stor del af bevægelsen skete, før vi så den.")
    else:
        reasons.append("Flaget kommer kun fra volumen. Kursen har ikke bekræftet noget endnu.")
    if vs_vwap > stop_max_pct / 2:
        reasons.append(f"Kursen ligger {vs_vwap:.1f}% over VWAP. Et helt normalt tilbagefald rammer stoppet.")
    else:
        reasons.append("Et udbrud midt på dagen fader ofte, når de første købere tager gevinst.")
    if (now.hour, now.minute) >= (15, 0):
        reasons.append("Under en time til lukning. Målet når måske ikke at blive ramt før 15:50.")
    else:
        reasons.append("Holdetiden er kort: alt lukkes kl. 15:50, uanset hvor kursen står.")
    if not news.get("has_reason"):
        ruling = "SKIP"
        summary = "Ingen klar grund fra NEWS. SKIP."
    elif not flag.get("by_move") or vs_vwap > stop_max_pct or (now.hour, now.minute) >= (15, 0):
        ruling = "WAIT"
        summary = "Grunden er der, men opstillingen er ikke god nok. WAIT."
    else:
        ruling = "BUY"
        summary = "Grunden er klar, og risikoen er lille i forhold til målet. BUY."
    return {"reasons": reasons[:3], "ruling": ruling, "summary": summary, "source": "rules"}


# ---------------------------------------------------------------------- skyggeregnskab
def shadow_outcome(entry: float, stop: float, target: float, bars: list[Bar], since: datetime) -> dict | None:
    """Hvad ville der være sket, hvis købet var gået igennem? Stop vinder, hvis begge rammes i samme minut."""
    later = [bar for bar in bars if bar.ts >= since]
    if not later or entry <= 0 or not (stop < entry < target):
        return None
    for bar in later:
        hit = resolve_intrabar("long", stop, target, bar)
        if hit is not None:
            reason, price = hit
            return {
                "result": "loss" if reason == "stop" else "win",
                "exit": round(price, 2),
                "pct": round((price - entry) / entry * 100.0, 2),
                "reason": reason,
            }
    last = later[-1].close
    pct = (last - entry) / entry * 100.0
    return {
        "result": "loss" if pct < 0 else "win",
        "exit": round(last, 2),
        "pct": round(pct, 2),
        "reason": "close",
    }
