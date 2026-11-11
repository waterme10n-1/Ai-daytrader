"""De tre bots, der regner: SCOUT, CHART og SIZER. Ren kode, så tallene aldrig er opdigtet.

Plus skyggeregnskabet, der bagefter tjekker, om de handler, SKEPTIC sagde nej til, ville have tabt.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from trader.desk.alpaca import Bar, _bracket, _parse_ts, _round_price, resolve_intrabar, vwap
from trader.desk.fmt import money, num, pct, short

MIN_BARS = 15
SWING_BARS = 15
MIN_STOP_PCT = 0.3
LATEST_ENTRY = (15, 30)
# Gap ved åbningen tæller kun den første halvanden time. Derefter er det en almindelig dagsbevægelse.
GAP_UNTIL = (11, 0)
# Et lille dyk de sidste fem minutter (højst 0,2 %) er et tilbagefald i en stigning, ikke en vending.
MOMENTUM_SLACK_PCT = 0.2


def completed(bars: list[Bar], now: datetime) -> list[Bar]:
    return [bar for bar in bars if bar.ts + timedelta(minutes=1) <= now]


# ---------------------------------------------------------------------- markedsfilter
def index_move(bars: list[Bar], now: datetime) -> float | None:
    """Indekset i dag: fra første åbning til seneste afsluttede minut. None, hvis der er for få minutter."""
    done = completed(bars, now)
    if len(done) < MIN_BARS or done[0].open <= 0:
        return None
    return (done[-1].close - done[0].open) / done[0].open * 100.0


def market_weak(move: float | None, *, limit_pct: float) -> bool:
    """Indekset er ned mere end limit_pct siden åbningen. Et limit på 0 slår filteret fra."""
    return limit_pct > 0 and move is not None and move <= -limit_pct + 1e-9


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
    by_gap: bool = False
    gap_pct: float | None = None  # siden gårsdagens lukkekurs
    to_close_min: int | None = None  # minutter til børsen lukker, da SCOUT markerede
    second: bool = False  # anden chance: CHART havde ingen opstilling sidst, aktien blev markeret i dag

    def to_dict(self) -> dict:
        return asdict(self)


def normal_volume_per_minute(daily: list[Bar], session_minutes: int = 390) -> float:
    """Gennemsnitlig dagsvolumen fordelt på børsens handelsminutter (New York 390, København 480)."""
    volumes = [bar.volume for bar in daily if bar.volume > 0]
    if not volumes:
        return 0.0
    return (sum(volumes) / len(volumes)) / float(session_minutes)


def _read(
    symbol: str, intraday: list[Bar], daily: list[Bar], now: datetime, session_minutes: int = 390
) -> tuple[Flag, float, float, float | None] | None:
    """Tallene SCOUT ser på: (flag uden afgørelse, bevægelse på en time, volumen i gange normal, gap siden i går)."""
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
    per_minute = normal_volume_per_minute(daily, session_minutes)
    ratio = (sum(bar.volume for bar in window) / minutes) / per_minute if per_minute > 0 else 0.0
    prev = daily[-1].close if daily and daily[-1].close > 0 else None
    gap = (last.close - prev) / prev * 100.0 if prev else None
    level = vwap(bars) or last.close
    day_open = bars[0].open
    flag = Flag(
        symbol=symbol,
        price=round(last.close, 2),
        move_pct=round(move, 2),
        window_minutes=minutes,
        volume_x=round(ratio, 1),
        vwap=round(level, 2),
        vs_vwap_pct=round((last.close - level) / level * 100.0, 2) if level else 0.0,
        day_change_pct=round((last.close - day_open) / day_open * 100.0, 2) if day_open else 0.0,
        by_move=False,
        by_volume=False,
        ts=last.ts.isoformat(timespec="minutes"),
        gap_pct=round(gap, 2) if gap is not None else None,
    )
    return flag, move, ratio, gap


def scout(
    symbol: str,
    intraday: list[Bar],
    daily: list[Bar],
    now: datetime,
    *,
    move_pct: float,
    volume_x: float,
    session_minutes: int = 390,
) -> Flag | None:
    """Flag ved bevægelse på mindst move_pct på en time eller volumen på mindst volume_x gange det normale."""
    read = _read(symbol, intraday, daily, now, session_minutes)
    if read is None:
        return None
    flag, move, ratio, _gap = read
    flag.by_move = abs(move) >= move_pct - 1e-9
    flag.by_volume = ratio >= volume_x - 1e-9
    return flag if flag.by_move or flag.by_volume else None


def gap_flag(
    symbol: str,
    intraday: list[Bar],
    daily: list[Bar],
    now: datetime,
    *,
    gap_pct: float,
    volume_x: float,
    gap_until: tuple[int, int] = GAP_UNTIL,
    session_minutes: int = 390,
) -> Flag | None:
    """Gap ved åbningen: steget mindst gap_pct siden i går og stadig over VWAP. Kun den første halvanden time.

    En aktie, der åbner 4 % højere og holder sig over VWAP, har købere med fra start, selv om den har ligget
    stille det første kvarter og derfor ikke har flyttet sig move_pct på en time.
    """
    if (now.hour, now.minute) >= gap_until:
        return None
    read = _read(symbol, intraday, daily, now, session_minutes)
    if read is None:
        return None
    flag, _move, ratio, gap = read
    if gap is None or gap < gap_pct - 1e-9 or flag.vs_vwap_pct <= 0:
        return None
    flag.by_gap = True
    flag.by_move = True  # kursen har flyttet sig, bare natten over
    flag.by_volume = ratio >= volume_x - 1e-9
    return flag


def scout_text(flag: Flag) -> str:
    move = pct(flag.move_pct)
    span = "den sidste time" if flag.window_minutes >= 60 else f"de sidste {flag.window_minutes} min"
    again = " Anden chance: CHART havde ingen opstilling sidst." if flag.second else ""
    if flag.by_gap:
        return (
            f"GAP: {flag.symbol} {pct(flag.gap_pct)} siden i går og over VWAP ({move} {span}), handler "
            f"{num(flag.volume_x, 1)}x normal volumen. Kurs {num(flag.price)}.{again}"
        )
    return f"FLAG: {flag.symbol} {move} {span}, handler {num(flag.volume_x, 1)}x normal volumen. Kurs {num(flag.price)}.{again}"


def premarket_gaps(symbols: list[str], snapshots: dict, daily: dict, now: datetime, *, gap_pct: float) -> list[dict]:
    """Før åbning: seneste handel i dag mod gårsdagens lukkekurs. Kun handler fra i dag tæller, ellers er kursen gammel."""
    out = []
    for symbol in symbols:
        snap = snapshots.get(symbol)
        bars = daily.get(symbol) or []
        if not snap or not bars or bars[-1].close <= 0:
            continue
        try:
            stamp = _parse_ts(str(snap.get("ts") or ""))
        except ValueError:
            continue
        if stamp.date() != now.date():
            continue
        prev = bars[-1].close
        gap = (float(snap["price"]) - prev) / prev * 100.0
        if gap >= gap_pct - 1e-9:
            out.append(
                {
                    "symbol": symbol,
                    "price": round(float(snap["price"]), 2),
                    "prev_close": round(prev, 2),
                    "gap_pct": round(gap, 2),
                    "ts": stamp.isoformat(timespec="minutes"),
                }
            )
    out.sort(key=lambda item: item["gap_pct"], reverse=True)
    return out


def premarket_text(item: dict) -> str:
    return (
        f"Før åbning: {item['symbol']} {pct(item['gap_pct'])} mod i går (kurs {num(item['price'])}, lukkede "
        f"{num(item['prev_close'])}). Jeg holder øje med den, når børsen åbner."
    )


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
    latest_entry: tuple[int, int] = LATEST_ENTRY,
) -> ChartPlan:
    """Indgang, mål og stop for et køb. Stoppet er højst stop_max_pct under indgangen, målet mindst reward_risk gange så langt væk."""
    bars = completed(intraday, now)
    if len(bars) < MIN_BARS:
        return ChartPlan(False, reason="For få minutter med data til et chart.")
    if (now.hour, now.minute) >= latest_entry:
        return ChartPlan(False, reason="For sent på dagen. Alt lukkes ti minutter før børsen lukker, så der er ikke tid til målet.")
    price = bars[-1].close
    level = vwap(bars) or price
    rising = flag.gap_pct if flag.by_gap and flag.gap_pct is not None else flag.move_pct
    if rising < 0:
        return ChartPlan(False, reason="Kursen falder, og bordet køber kun (ingen short).")
    if price <= level:
        return ChartPlan(False, reason=f"Kursen {num(price)} er under VWAP {num(level)}. Køberne har ikke kontrollen.")
    extension = (price - level) / level * 100.0
    if extension > 2 * stop_max_pct:
        return ChartPlan(False, reason=f"Kursen er {pct(extension, sign=False)} over VWAP. Det er at jagte en løbet bevægelse.")
    if len(bars) > 5 and price < bars[-6].close * (1 - MOMENTUM_SLACK_PCT / 100.0):
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
        reason=(
            f"Over VWAP {num(level)}, stop under laveste kurs de sidste {SWING_BARS} min."
            if stop_r > floor + 0.005
            else f"Over VWAP {num(level)}, stop ved loftet på {short(stop_max_pct)} %."
        ),
    )


def chart_text(symbol: str, plan: ChartPlan) -> str:
    if not plan.setup:
        return f"{symbol}: Ingen setup. {plan.reason}"
    return (
        f"{symbol}: indgang {num(plan.entry)}, mål {num(plan.target)}, stop {num(plan.stop)}. "
        f"Stoppet er {pct(plan.stop_pct, sign=False)} væk, og målet er {num(plan.reward_risk, 1)}x så langt."
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
        zero.reason = f"{short(position_pct)} % af kontoen rækker ikke til én aktie. Svaret er nul."
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


def size_text(symbol: str, sized: Size, currency: str = "USD") -> str:
    if sized.qty < 1:
        return f"{symbol}: 0. {sized.reason}"
    return (
        f"{symbol}: {sized.qty} aktier for {money(sized.notional, currency, decimals=0)} ({pct(sized.pct, sign=False)} af kontoen). "
        f"Største tab ved stop: {money(sized.max_loss, currency, decimals=0)}. Åbne handler efter denne: {sized.open_after} af {sized.max_open}."
    )


# ---------------------------------------------------------------------- SKEPTIC uden Claude
def rules_skeptic(
    flag: dict, news: dict, plan: dict, now: datetime, *, stop_max_pct: float, late_from: tuple[int, int] = (15, 0)
) -> dict:
    """Reserve når Claude ikke er sat op. Den er strengere end Claude, ikke mildere."""
    reasons: list[str] = []
    move = float(flag.get("move_pct") or 0)
    vs_vwap = float(flag.get("vs_vwap_pct") or 0)
    if flag.get("by_gap"):
        gap = float(flag.get("gap_pct") or 0)
        reasons.append(f"Aktien er allerede steget {pct(gap)} siden i går. Gaps lukker tit igen i løbet af dagen.")
    elif flag.get("by_move"):
        reasons.append(f"Aktien har allerede flyttet sig {pct(move)}. En stor del af bevægelsen skete, før vi så den.")
    else:
        reasons.append("Flaget kommer kun fra volumen. Kursen har ikke bekræftet noget endnu.")
    if vs_vwap > stop_max_pct / 2:
        reasons.append(f"Kursen ligger {pct(vs_vwap, sign=False)} over VWAP. Et helt normalt tilbagefald rammer stoppet.")
    else:
        reasons.append("Et udbrud midt på dagen fader ofte, når de første købere tager gevinst.")
    if (now.hour, now.minute) >= late_from:
        reasons.append("Under en time til lukning. Målet når måske ikke at blive ramt, før bordet lukker alt.")
    else:
        reasons.append("Holdetiden er kort: alt lukkes ti minutter før børsen lukker, uanset hvor kursen står.")
    if not news.get("has_reason"):
        ruling = "SKIP"
        summary = "Ingen klar grund fra NEWS. SKIP."
    elif not flag.get("by_move") or vs_vwap > stop_max_pct * 1.5 or (now.hour, now.minute) >= late_from:
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
    change = (last - entry) / entry * 100.0
    return {
        "result": "loss" if change < 0 else "win",
        "exit": round(last, 2),
        "pct": round(change, 2),
        "reason": "close",
    }


# ---------------------------------------------------------------------- radar
def quotes(
    symbols: list[str], intraday: dict, daily: dict, now: datetime, flagged: list[str], points: int = 24, *, session_minutes: int = 390
) -> list[dict]:
    """Kurs, dagens bevægelse, timens bevægelse, volumen og en lille kurve pr. ticker. Kun fra barer, vi allerede har."""
    out = []
    for symbol in symbols:
        bars = completed(intraday.get(symbol, []), now)
        if not bars:
            continue
        last = bars[-1]
        prior = daily.get(symbol) or []
        # "I dag" er mod gårsdagens lukkekurs, som i enhver aktie-app. Uden dagsbarer: mod åbningen.
        day_open = prior[-1].close if prior and prior[-1].close > 0 else bars[0].open
        start = last.ts - timedelta(minutes=60)
        window = [bar for bar in bars if bar.ts > start]
        ref = window[0].open if window else bars[0].open
        per_minute = normal_volume_per_minute(daily.get(symbol, []), session_minutes)
        vol_x = (sum(bar.volume for bar in window) / max(1, len(window))) / per_minute if per_minute > 0 else 0.0
        level = vwap(bars) or last.close
        step = max(1, len(bars) // points)
        spark = [round(bar.close, 2) for bar in bars[::step]][-points:]
        if spark and spark[-1] != round(last.close, 2):
            spark.append(round(last.close, 2))
        out.append(
            {
                "symbol": symbol,
                "price": round(last.close, 2),
                "day_pct": round((last.close - day_open) / day_open * 100.0, 2) if day_open else 0.0,
                "move_pct": round((last.close - ref) / ref * 100.0, 2) if ref else 0.0,
                "volume_x": round(vol_x, 1),
                "vs_vwap_pct": round((last.close - level) / level * 100.0, 2) if level else 0.0,
                "flagged": symbol in flagged,
                "spark": spark,
            }
        )
    return out


def snapshot_quotes(
    symbols: list[str], snapshots: dict, daily: dict, flagged: list[str], points: int = 24, *, now: datetime | None = None
) -> list[dict]:
    """Radarens tal uden for åbningstid: seneste kurs mod gårsdagens lukkekurs, kurve over de sidste dages lukkekurser."""
    out = []
    for symbol in symbols:
        snap = snapshots.get(symbol)
        if not snap:
            continue
        price = snap["price"]
        closes = [bar.close for bar in daily.get(symbol, [])][-(points - 1):]
        prev = snap.get("prev_close") or (closes[-1] if closes else None)
        # Er der handlet i dag (før åbning eller efter lukning), er referencen gårsdagens lukkekurs fra vores egne
        # dagsbarer. Alpacas prevDailyBar kan før åbning stadig være dagen før i går.
        if now is not None and closes:
            try:
                traded_today = _parse_ts(str(snap.get("ts") or "")).date() == now.date()
            except ValueError:
                traded_today = False
            if traded_today:
                prev = closes[-1]
        per_day = sum(bar.volume for bar in daily.get(symbol, [])) / max(1, len(daily.get(symbol, [])))
        vwap_value = snap.get("vwap")
        out.append(
            {
                "symbol": symbol,
                "price": round(price, 2),
                "day_pct": round((price - prev) / prev * 100.0, 2) if prev else 0.0,
                "move_pct": None,
                "volume_x": round(snap.get("day_volume", 0) / per_day, 1) if per_day > 0 else 0.0,
                "vs_vwap_pct": round((price - vwap_value) / vwap_value * 100.0, 2) if vwap_value else None,
                "flagged": symbol in flagged,
                "spark": [round(value, 2) for value in closes] + [round(price, 2)],
            }
        )
    return out
