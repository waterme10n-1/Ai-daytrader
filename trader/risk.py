"""Deterministisk risikomotor. AI kan aldrig tilsidesætte den."""

from __future__ import annotations

import math
from dataclasses import dataclass

from trader.config import (
    BUYING_POWER_PCT,
    MAX_DAILY_LOSS,
    MAX_NOTIONAL_PCT,
    MAX_OPEN_POSITIONS,
    MAX_TRADES_PER_DAY,
    MIN_CONFIDENCE,
    MIN_REWARD_RISK,
    PDT_EQUITY,
    PDT_MAX_DAYTRADES,
    RISK_PER_TRADE,
)


@dataclass
class RiskContext:
    equity: float
    buying_power: float
    entry: float
    stop: float
    target: float
    side: str
    symbol: str
    confidence: int
    open_positions: int
    open_symbols: set[str]
    trades_today: int
    day_start_equity: float
    kill_switch: bool
    halted: bool
    mode: str
    daytrade_count: int
    live_max_capital: float
    tighter_stop: float | None = None


@dataclass
class RiskDecision:
    allowed: bool
    qty: int
    stop: float
    target: float
    reason: str


def daily_loss_hit(day_start_equity: float, equity: float) -> bool:
    if day_start_equity <= 0:
        return False
    return (day_start_equity - equity) / day_start_equity >= MAX_DAILY_LOSS - 1e-12


def next_error_count(current: int) -> tuple[int, bool]:
    """Returnerer (ny tæller, skal flades ud). Tre fejl i træk udløser fladning."""
    count = current + 1
    return count, count >= 3


def reward_risk(side: str, entry: float, stop: float, target: float) -> float | None:
    if side == "long":
        risk = entry - stop
        reward = target - entry
    elif side == "short":
        risk = stop - entry
        reward = entry - target
    else:
        return None
    if risk <= 0 or reward <= 0:
        return None
    return reward / risk


def apply_tighter_stop(side: str, entry: float, stop: float, tighter: float | None) -> float:
    """AI må kun stramme stoppet, og det skal blive på den rigtige side af indgangen.

    Et løsere eller forkert stop ignoreres. Det oprindelige stop beholdes.
    """
    if tighter is None or not math.isfinite(tighter):
        return stop
    if side == "long":
        if stop < tighter < entry:
            return tighter
        return stop
    if side == "short":
        if entry < tighter < stop:
            return tighter
        return stop
    return stop


def position_qty(
    equity: float,
    buying_power: float,
    entry: float,
    stop: float,
    *,
    live_cap: float | None = None,
) -> int:
    """Hele aktier. Mindste af risikostørrelse, 25 % notional og 90 % købekraft.

    I live bruges min(equity, live-loft) som grundlag for risiko og notional,
    og købekraften skæres til samme loft.
    """
    if entry <= 0 or equity <= 0 or buying_power <= 0:
        return 0
    basis = equity
    power = buying_power
    if live_cap is not None:
        basis = min(basis, live_cap)
        power = min(power, live_cap)
    per_share = abs(entry - stop)
    if per_share <= 0 or basis <= 0 or power <= 0:
        return 0
    qty_risk = math.floor((basis * RISK_PER_TRADE) / per_share)
    qty_notional = math.floor((basis * MAX_NOTIONAL_PCT) / entry)
    qty_power = math.floor((power * BUYING_POWER_PCT) / entry)
    return max(0, int(min(qty_risk, qty_notional, qty_power)))


def evaluate(ctx: RiskContext) -> RiskDecision:
    stop = apply_tighter_stop(ctx.side, ctx.entry, ctx.stop, ctx.tighter_stop)
    target = ctx.target
    rejected = RiskDecision(False, 0, stop, target, "")

    if ctx.kill_switch:
        rejected.reason = "Nødstop er aktivt. Nye handler er blokeret."
        return rejected
    if ctx.halted or daily_loss_hit(ctx.day_start_equity, ctx.equity):
        rejected.reason = "Dagen er standset efter tab på 2 % af dagens startkapital."
        return rejected
    if ctx.confidence < MIN_CONFIDENCE:
        rejected.reason = f"Sikkerhed {ctx.confidence} er under minimum {MIN_CONFIDENCE}."
        return rejected
    if ctx.side not in {"long", "short"}:
        rejected.reason = "Ukendt retning."
        return rejected
    if not all(math.isfinite(x) and x > 0 for x in (ctx.entry, stop, target)):
        rejected.reason = "Indgang, stop eller mål er ugyldigt."
        return rejected

    ratio = reward_risk(ctx.side, ctx.entry, stop, target)
    if ratio is None:
        rejected.reason = "Stop eller mål ligger på den forkerte side af indgangen."
        return rejected
    if ratio < MIN_REWARD_RISK - 1e-9:
        rejected.reason = f"Gevinst/risiko {ratio:.2f} er under minimum {MIN_REWARD_RISK:.1f}."
        return rejected
    if ctx.trades_today >= MAX_TRADES_PER_DAY:
        rejected.reason = f"Maks. {MAX_TRADES_PER_DAY} handler i dag er nået."
        return rejected
    if ctx.open_positions >= MAX_OPEN_POSITIONS:
        rejected.reason = f"Maks. {MAX_OPEN_POSITIONS} åbne positioner."
        return rejected
    if ctx.symbol in ctx.open_symbols:
        rejected.reason = f"Der er allerede en position i {ctx.symbol}."
        return rejected
    if ctx.mode == "live" and ctx.equity < PDT_EQUITY and ctx.daytrade_count >= PDT_MAX_DAYTRADES:
        rejected.reason = (
            f"PDT-regel: egenkapital under {PDT_EQUITY:.0f} USD og {ctx.daytrade_count} daytrades."
        )
        return rejected

    live_cap = ctx.live_max_capital if ctx.mode == "live" else None
    qty = position_qty(ctx.equity, ctx.buying_power, ctx.entry, stop, live_cap=live_cap)
    if qty < 1:
        rejected.reason = "Positionsstørrelsen er under 1 aktie."
        rejected.qty = 0
        return rejected

    return RiskDecision(True, qty, stop, target, "Godkendt af risikomotoren.")
