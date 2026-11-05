"""Deterministisk risiko for memebotten. AI og læring kan aldrig tilsidesætte den."""

from __future__ import annotations

from dataclasses import dataclass

from trader.meme.config import MemeSettings, SniperSettings, StrategyParams

MIN_POSITION_USD = 1.0


@dataclass
class EntryContext:
    mint: str
    equity: float
    cash: float
    deployed: float
    open_count: int
    open_mints: set[str]
    trades_today: int
    kill_switch: bool
    halted_day: bool
    halted_drawdown: bool


@dataclass
class EntryDecision:
    allowed: bool
    size_usd: float
    reason: str


def evaluate_entry(
    ctx: EntryContext,
    settings: MemeSettings,
    *,
    position_usd: float | None = None,
    max_open: int | None = None,
    max_trades: int | None = None,
    label: str = "",
) -> EntryDecision:
    position_usd = settings.position_usd if position_usd is None else min(position_usd, settings.position_usd)
    max_open = settings.max_open if max_open is None else max_open
    max_trades = settings.max_trades_per_day if max_trades is None else max_trades

    def no(reason: str) -> EntryDecision:
        return EntryDecision(False, 0.0, reason)

    if ctx.kill_switch:
        return no("Nødstop er aktivt.")
    if ctx.halted_drawdown:
        return no(f"Samlet tab har ramt {settings.max_drawdown_pct * 100:.0f} % af bankroll. Tryk Genoptag for at fortsætte.")
    if ctx.halted_day:
        return no(f"Dagens tab har ramt {settings.daily_loss_pct * 100:.0f} % af bankroll. Pause til i morgen (UTC).")
    if ctx.mint in ctx.open_mints:
        return no("Der er allerede en position i tokenet.")
    if ctx.open_count >= max_open:
        return no(f"Maks. {max_open} åbne {label}positioner.")
    if ctx.trades_today >= max_trades:
        return no(f"Maks. {max_trades} {label}køb i dag er nået.")
    room = settings.bankroll_usd - ctx.deployed
    size = min(position_usd, ctx.cash, room, max(0.0, ctx.equity) * 0.4)
    if size < MIN_POSITION_USD:
        return no(f"For lidt fri kapital ({max(0.0, size):.2f} USD). Gebyrer ville spise handlen.")
    return EntryDecision(True, round(size, 4), "Godkendt af risikomotoren.")


def daily_loss_hit(today_pnl: float, settings: MemeSettings) -> bool:
    return today_pnl <= -settings.daily_loss_pct * settings.bankroll_usd + 1e-9


def drawdown_hit(total_pnl: float, settings: MemeSettings) -> bool:
    return total_pnl <= -settings.max_drawdown_pct * settings.bankroll_usd + 1e-9


@dataclass
class ExitDecision:
    sell: bool
    fraction: float
    kind: str
    reason: str


HOLD = ExitDecision(False, 0.0, "", "")


def exit_decision(
    *,
    ratio: float,
    peak_ratio: float,
    minutes_held: float,
    partial_taken: bool,
    liquidity_entry: float | None,
    liquidity_now: float | None,
    params: StrategyParams,
) -> ExitDecision:
    """ratio = kurs nu / kurs ved køb."""
    if liquidity_entry and liquidity_now is not None and liquidity_now < liquidity_entry * 0.4:
        return ExitDecision(
            True,
            1.0,
            "rug_liquidity",
            f"Likviditeten faldt fra ${liquidity_entry:,.0f} til ${liquidity_now:,.0f}. Sælger alt (rug-alarm).",
        )
    if ratio <= 1.0 - params.stop_loss_pct:
        return ExitDecision(True, 1.0, "stop_loss", f"Stop-loss: {(ratio - 1) * 100:.0f} %.")
    if not partial_taken and ratio >= 1.0 + params.take_profit_pct:
        return ExitDecision(
            True,
            params.partial_fraction,
            "take_profit",
            f"Delvis gevinst ved {(ratio - 1) * 100:+.0f} %: sælger {params.partial_fraction * 100:.0f} %.",
        )
    if peak_ratio >= 1.0 + params.trail_activation_pct and ratio <= peak_ratio * (1.0 - params.trailing_stop_pct):
        return ExitDecision(
            True,
            1.0,
            "trailing",
            f"Trailing stop: {((ratio / peak_ratio) - 1) * 100:.0f} % fra toppen ({(peak_ratio - 1) * 100:+.0f} %).",
        )
    if minutes_held >= params.max_hold_minutes:
        return ExitDecision(True, 1.0, "time", f"Holdt i {minutes_held:.0f} min. Tidsgrænsen er nået.")
    if minutes_held >= params.stale_minutes and ratio < 1.05 and not partial_taken:
        return ExitDecision(True, 1.0, "stale", f"Intet skete på {minutes_held:.0f} min. ({(ratio - 1) * 100:+.0f} %).")
    return HOLD


MOONBAG_TRAIL = 0.3


def sniper_exit_decision(
    *,
    ratio: float,
    peak_ratio: float,
    minutes_held: float,
    partial_taken: bool,
    liquidity_entry: float | None,
    liquidity_now: float | None,
    cfg: SniperSettings,
) -> ExitDecision:
    """Sniperens udgange: dit gevinstmål, stop-loss, rug-alarm og tidsgrænse."""
    if liquidity_entry and liquidity_now is not None and liquidity_now < liquidity_entry * 0.4:
        return ExitDecision(
            True,
            1.0,
            "rug_liquidity",
            f"Likviditeten faldt fra ${liquidity_entry:,.0f} til ${liquidity_now:,.0f}. Sælger alt (rug-alarm).",
        )
    if ratio <= 1.0 - cfg.stop_loss_pct:
        return ExitDecision(True, 1.0, "stop_loss", f"Stop-loss: {(ratio - 1) * 100:.0f} %.")
    if not partial_taken and ratio >= 1.0 + cfg.take_profit_pct:
        fraction = 1.0 if cfg.sell_fraction >= 0.999 else cfg.sell_fraction
        rest = "" if fraction >= 1.0 else f" Resten ({(1 - fraction) * 100:.0f} %) følger toppen."
        return ExitDecision(
            True,
            fraction,
            "target",
            f"Gevinstmålet på +{cfg.take_profit_pct * 100:.0f} % er nået ({(ratio - 1) * 100:+.0f} %).{rest}",
        )
    if partial_taken and (ratio <= peak_ratio * (1.0 - MOONBAG_TRAIL) or ratio <= 1.0):
        return ExitDecision(True, 1.0, "trailing", f"Resten sælges: {(ratio / peak_ratio - 1) * 100:.0f} % fra toppen.")
    if minutes_held >= cfg.max_hold_minutes:
        return ExitDecision(True, 1.0, "time", f"Holdt i {minutes_held:.0f} min. Tidsgrænsen er nået.")
    return HOLD


def top_exit_decision(
    *,
    ratio: float,
    peak_ratio: float,
    minutes_held: float,
    partial_taken: bool,
    liquidity_entry: float | None,
    liquidity_now: float | None,
    cfg,
) -> ExitDecision:
    """Kopierede top-trader-positioner: hovedreglen er at sælge, når traderen sælger (det sker i motoren).

    Her er kun sikkerhedsnettet: rug-alarm, dit stop-loss, en gevinstsikring hvis kursen er
    fordoblet og falder igen, og en tidsgrænse, hvis traderen aldrig sælger.
    """
    from trader.meme.config import TOP_TRAIL, TOP_TRAIL_ACTIVATION

    if liquidity_entry and liquidity_now is not None and liquidity_now < liquidity_entry * 0.4:
        return ExitDecision(
            True,
            1.0,
            "rug_liquidity",
            f"Likviditeten faldt fra ${liquidity_entry:,.0f} til ${liquidity_now:,.0f}. Sælger alt (rug-alarm).",
        )
    if getattr(cfg, "stop_loss_on", True) and ratio <= 1.0 - cfg.stop_loss_pct:
        return ExitDecision(True, 1.0, "stop_loss", f"Stop-loss: {(ratio - 1) * 100:.0f} %.")
    if peak_ratio >= 1.0 + TOP_TRAIL_ACTIVATION and ratio <= peak_ratio * (1.0 - TOP_TRAIL):
        return ExitDecision(True, 1.0, "trailing", f"Gevinstsikring: {(ratio / peak_ratio - 1) * 100:.0f} % fra toppen på {(peak_ratio - 1) * 100:+.0f} %.")
    if minutes_held >= cfg.max_hold_minutes:
        return ExitDecision(True, 1.0, "time", f"Holdt i {minutes_held:.0f} min. uden at traderen solgte. Tidsgrænsen er nået.")
    return HOLD
