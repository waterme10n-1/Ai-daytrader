"""Scoring og læring.

Hver kandidat bliver til et lille sæt tal (features). En logistisk model giver en score
mellem 0 og 1. Efter hver lukket handel flyttes vægtene mod det, der faktisk virkede, og
strategiparametrene justeres inden for faste grænser. Alt gemmes, så man kan se, hvad
botten har lært, og hvorfor.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from trader.meme.clients import PairInfo, RugReport, TokenInfo, XBuzz
from trader.meme.config import (
    PARAM_LABELS,
    SNIPER_LABELS,
    SniperSettings,
    StrategyParams,
    clamp_param,
    clamp_sniper,
)

FEATURES: dict[str, tuple[str, float]] = {
    # navn: (dansk forklaring, startvægt)
    "liquidity": ("Høj likviditet", 0.6),
    "volume_liq": ("Volumen i forhold til likviditet", 0.5),
    "buy_pressure": ("Købspres sidste time", 0.8),
    "buy_pressure_5m": ("Købspres sidste 5 min.", 0.5),
    "momentum_1h": ("Kursstigning sidste time", 0.3),
    "momentum_5m": ("Kursstigning sidste 5 min.", 0.2),
    "overheated": ("Overophedet (steget for meget)", -0.8),
    "young": ("Ungt token (under et døgn)", -0.1),
    "rug_risk": ("RugCheck-risiko", -1.5),
    "concentration": ("Ejerkoncentration i top-10", -1.0),
    "organic": ("Organisk handel (Jupiter)", 0.8),
    "socials": ("Har X, Telegram og hjemmeside", 0.4),
    "x_buzz": ("Omtale på X", 0.5),
    "copy_signal": ("Købt af en fulgt trader", 0.7),
    "boosted": ("Betalt promovering (boost)", -0.1),
    "verified": ("Verificeret på Jupiter", 0.3),
    "ai_view": ("Claudes vurdering", 0.6),
}
INITIAL_BIAS = -0.6
LEARNING_RATE = 0.08
PRIOR_PULL = 0.01
WEIGHT_LIMIT = 3.0
WIN_THRESHOLD_PCT = 0.02


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def build_features(
    pair: PairInfo,
    *,
    report: RugReport | None = None,
    token: TokenInfo | None = None,
    buzz: XBuzz | None = None,
    copy_signal: bool = False,
    boosted: bool = False,
    ai_view: float = 0.0,
    now_ms: int | None = None,
) -> dict[str, float]:
    liquidity = max(1.0, pair.liquidity_usd)
    ratio_h1 = pair.buy_ratio("h1")
    ratio_m5 = pair.buy_ratio("m5")
    age = pair.age_minutes(now_ms)
    change_h1 = pair.change.get("h1", 0.0)
    socials = 0
    has_x = bool(pair.socials.get("twitter") or pair.socials.get("x") or (token and token.twitter))
    has_tg = bool(pair.socials.get("telegram") or (token and token.telegram))
    has_web = bool(pair.websites or (token and token.website))
    socials = int(has_x) + int(has_tg) + int(has_web)
    top10 = None
    if report is not None and report.top10_pct is not None:
        top10 = report.top10_pct
    elif token is not None and token.top_holders_pct is not None:
        top10 = token.top_holders_pct
    organic = token.organic_score if token is not None and token.organic_score is not None else 50.0
    return {
        "liquidity": _clip((math.log10(liquidity) - 4.0) / 2.0, 0.0, 1.0),
        "volume_liq": _clip(pair.volume.get("h1", 0.0) / liquidity / 1.5, 0.0, 1.0),
        "buy_pressure": 0.0 if ratio_h1 is None else _clip(2 * ratio_h1 - 1, -1.0, 1.0),
        "buy_pressure_5m": 0.0 if ratio_m5 is None else _clip(2 * ratio_m5 - 1, -1.0, 1.0),
        "momentum_1h": _clip(change_h1 / 100.0, -1.0, 1.0),
        "momentum_5m": _clip(pair.change.get("m5", 0.0) / 15.0, -1.0, 1.0),
        "overheated": _clip((change_h1 - 80.0) / 200.0, 0.0, 1.0),
        "young": 0.0 if age is None else 1.0 - _clip(age / 1440.0, 0.0, 1.0),
        "rug_risk": report.risk if report is not None else 0.6,
        "concentration": _clip((top10 if top10 is not None else 30.0) / 50.0, 0.0, 1.0),
        "organic": _clip(organic / 100.0, 0.0, 1.0),
        "socials": socials / 3.0,
        "x_buzz": 0.0 if buzz is None else _clip(math.log10(1 + buzz.authors) / 2.0, 0.0, 1.0),
        "copy_signal": 1.0 if copy_signal else 0.0,
        "boosted": 1.0 if (boosted or pair.boosts_active > 0) else 0.0,
        "verified": 1.0 if token is not None and token.verified else 0.0,
        "ai_view": _clip(ai_view, -1.0, 1.0),
    }


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


@dataclass
class Model:
    weights: dict[str, float] = field(default_factory=lambda: {name: w for name, (_l, w) in FEATURES.items()})
    bias: float = INITIAL_BIAS
    updates: int = 0

    def score(self, features: dict[str, float]) -> float:
        total = self.bias
        for name, value in features.items():
            total += self.weights.get(name, 0.0) * value
        return _sigmoid(total)

    def contributions(self, features: dict[str, float]) -> list[tuple[str, float]]:
        items = [(name, self.weights.get(name, 0.0) * value) for name, value in features.items()]
        return sorted(items, key=lambda item: item[1], reverse=True)

    def update(self, features: dict[str, float], won: bool, rate: float = 1.0) -> dict[str, float]:
        """Ét SGD-skridt. rate < 1 for skyggehandler, der tæller mindre end rigtige handler."""
        target = 1.0 if won else 0.0
        error = (target - self.score(features)) * max(0.0, min(1.0, rate))
        changes: dict[str, float] = {}
        for name, value in features.items():
            if name not in FEATURES:
                continue
            prior = FEATURES[name][1]
            old = self.weights.get(name, prior)
            new = old + LEARNING_RATE * (error * value - PRIOR_PULL * (old - prior))
            new = _clip(new, -WEIGHT_LIMIT, WEIGHT_LIMIT)
            self.weights[name] = new
            if abs(new - old) > 1e-9:
                changes[name] = new - old
        self.bias = _clip(self.bias + LEARNING_RATE * error * 0.5, -3.0, 1.0)
        self.updates += 1
        return changes

    def to_dict(self) -> dict[str, Any]:
        return {"weights": self.weights, "bias": self.bias, "updates": self.updates}

    @classmethod
    def from_dict(cls, data: dict | None) -> Model:
        model = cls()
        if not data:
            return model
        for name, value in (data.get("weights") or {}).items():
            if name in FEATURES:
                try:
                    model.weights[name] = _clip(float(value), -WEIGHT_LIMIT, WEIGHT_LIMIT)
                except (TypeError, ValueError):
                    continue
        try:
            model.bias = _clip(float(data.get("bias", INITIAL_BIAS)), -3.0, 1.0)
            model.updates = int(data.get("updates", 0))
        except (TypeError, ValueError):
            pass
        return model


EXIT_LABELS = {
    "rug_liquidity": "likviditeten blev trukket",
    "rug_no_route": "ingen exit-rute (sandsynligvis rug)",
    "rug_dev": "udvikleren eller topholdere solgte ud",
    "horisont": "fulgt i 6 timer",
    "rug": "rug",
    "stop_loss": "stop-loss",
    "take_profit": "delvis gevinst",
    "target": "gevinstmålet nået",
    "trailing": "trailing stop",
    "time": "tidsgrænse",
    "stale": "intet skete",
    "copy_exit": "den fulgte trader solgte",
    "manual": "solgt manuelt",
    "kill": "nødstop",
    "no_price": "ingen kurs",
}


def trade_lesson(position: dict, features: dict[str, float], model_before: Model, changes: dict[str, float]) -> str:
    pnl_pct = float(position.get("pnl_pct") or 0.0) * 100
    symbol = position.get("symbol") or "?"
    reason = EXIT_LABELS.get(str(position.get("exit_reason") or ""), str(position.get("exit_reason") or ""))
    contributions = model_before.contributions(features)
    won = pnl_pct / 100 > WIN_THRESHOLD_PCT
    if won:
        drivers = [FEATURES[name][0].lower() for name, value in contributions if value > 0.05][:2]
        text = f"Gevinst {pnl_pct:+.0f} % på {symbol} ({reason})."
        if drivers:
            text += f" Det der bar handlen: {' og '.join(drivers)}. De vægte er hævet en smule."
        return text
    drivers = [FEATURES[name][0].lower() for name, value in contributions if value > 0.05][:2]
    text = f"Tab {pnl_pct:+.0f} % på {symbol} ({reason})."
    if drivers:
        text += f" Botten stolede mest på {' og '.join(drivers)}, så de vægte er sænket."
    biggest = sorted(changes.items(), key=lambda item: abs(item[1]), reverse=True)[:1]
    if biggest and abs(biggest[0][1]) > 0.01:
        name, delta = biggest[0]
        text += f" Største ændring: {FEATURES[name][0].lower()} {delta:+.2f}."
    return text


def adapt_params(params: StrategyParams, recent: list[dict]) -> list[str]:
    """Regelbaseret tilpasning ud fra de seneste lukkede handler. Returnerer forklaringer."""
    if len(recent) < 8:
        return []
    window = recent[-15:]
    pnl = [float(row.get("pnl_usd") or 0.0) for row in window]
    wins = [value for value in pnl if value > 0]
    expectancy = sum(pnl) / len(pnl)
    win_rate = len(wins) / len(pnl)
    reasons = [str(row.get("exit_reason") or "") for row in window[-10:]]
    notes: list[str] = []

    def move(name: str, value: float, why: str) -> None:
        old = getattr(params, name)
        new = clamp_param(name, value)
        if abs(new - old) < 1e-9:
            return
        setattr(params, name, new)
        notes.append(f"{PARAM_LABELS.get(name, name)}: {_fmt(old)} → {_fmt(new)} ({why}).")

    if expectancy < 0:
        move("min_score", params.min_score + 0.01, "negativ forventning i de seneste handler, så botten bliver mere kræsen")
    elif win_rate >= 0.5:
        move("min_score", params.min_score - 0.005, "positiv forventning, så botten tør lidt flere handler")
    if reasons.count("stop_loss") >= 5:
        move("max_change_h1_pct", params.max_change_h1_pct * 0.9, "mange stop-loss tyder på, at botten køber for sent")
        move("min_liquidity_usd", params.min_liquidity_usd * 1.1, "mange stop-loss — kræver mere likviditet")
    rugs = sum(1 for row in window if str(row.get("exit_reason") or "").startswith("rug"))
    if rugs >= 2:
        move("max_rug_score", params.max_rug_score - 0.03, f"{rugs} rugs i de seneste {len(window)} handler")
        move("max_top10_pct", params.max_top10_pct - 2.0, "rugs — strammere krav til ejerkoncentration")
        move("min_age_minutes", params.min_age_minutes * 1.2, "rugs — venter længere efter lancering")
    if reasons.count("stale") >= 5:
        move("min_volume_h1_usd", params.min_volume_h1_usd * 1.15, "mange handler hvor intet skete — kræver mere volumen")
    if reasons.count("trailing") + reasons.count("take_profit") >= 5 and expectancy > 0:
        move("trailing_stop_pct", params.trailing_stop_pct + 0.02, "vindere bliver stoppet ud tidligt — giver mere luft")
    return notes


def _fmt(value: float) -> str:
    if abs(value) >= 1000:
        return f"{value:,.0f}".replace(",", ".")
    if abs(value) >= 10:
        return f"{value:.0f}"
    return f"{value:.2f}"


def source_stats(closed: list[dict]) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    for row in closed:
        source = str(row.get("source") or "ukendt")
        entry = stats.setdefault(source, {"trades": 0, "wins": 0, "pnl": 0.0})
        pnl = float(row.get("pnl_usd") or 0.0)
        entry["trades"] += 1
        entry["wins"] += 1 if pnl > 0 else 0
        entry["pnl"] += pnl
    for entry in stats.values():
        entry["win_rate"] = entry["wins"] / entry["trades"] if entry["trades"] else None
        entry["muted"] = entry["trades"] >= 6 and (entry["win_rate"] or 0) < 0.3 and entry["pnl"] < 0
    return stats


def adapt_sniper(cfg: SniperSettings, recent: list[dict]) -> list[str]:
    """Strammer sniperens filtre efter rugs og mange stop-loss. Rører aldrig mål, stop eller beløb."""
    if len(recent) < 5:
        return []
    window = recent[-10:]
    rugs = sum(1 for row in window if str(row.get("exit_reason") or "").startswith("rug"))
    stops = sum(1 for row in window if row.get("exit_reason") == "stop_loss")
    notes: list[str] = []

    def move(name: str, value: float, why: str) -> None:
        old = getattr(cfg, name)
        new = clamp_sniper(name, value)
        if abs(new - old) < 1e-9:
            return
        setattr(cfg, name, new)
        notes.append(f"Sniper · {SNIPER_LABELS.get(name, name)}: {_fmt(old)} → {_fmt(new)} ({why}).")

    if rugs >= 2:
        why = f"{rugs} rugs i de seneste {len(window)} snipes"
        move("min_liquidity_usd", cfg.min_liquidity_usd * 1.25, why)
        move("min_holders", cfg.min_holders + 10, why)
        move("max_top10_pct", cfg.max_top10_pct - 3.0, why)
        move("min_age_seconds", cfg.min_age_seconds + 30.0, why)
    elif stops >= 5:
        why = f"{stops} stop-loss i de seneste {len(window)} snipes"
        move("min_holders", cfg.min_holders + 5, why)
        move("min_liquidity_usd", cfg.min_liquidity_usd * 1.1, why)
    return notes


# --------------------------------------------------------------------------- skyggehandler

TARGET_GRID = (0.25, 0.5, 1.0, 2.0, 3.0, 5.0)
STOP_GRID = (0.2, 0.3, 0.35, 0.5)
MIN_PATHS = 20


def simulate_path(
    path: list,
    *,
    tp: float,
    frac: float,
    sl: float,
    max_hold: float,
    trail_activation: float | None = None,
    trailing: float = 0.3,
    stale: float | None = None,
    rugged: bool = False,
    cost_pct: float = 0.0,
    breakeven_after_partial: bool = False,
) -> tuple[float, str]:
    """Kør bottens salgsregler på en kurs-sti [[minutter, kurs/købskurs], ...].

    Returnerer (resultat i %, årsag). Mellem målingerne kendes kursen ikke, så et stop
    sælger til den målte kurs (et hul ned rammer fuldt), mens et mål sælger til målet.
    """
    remaining, realized, peak, partial = 1.0, 0.0, 1.0, False
    for minutes, ratio in path or []:
        minutes, ratio = float(minutes), float(ratio)
        peak = max(peak, ratio)
        if ratio <= 1.0 - sl:
            return realized + remaining * (ratio - 1.0) - cost_pct, "stop_loss"
        if not partial and ratio >= 1.0 + tp:
            sold = 1.0 if frac >= 0.999 else frac
            realized += sold * tp
            remaining -= sold
            if remaining <= 1e-9:
                return realized - cost_pct, "target"
            partial = True
            continue
        if partial and (ratio <= peak * (1.0 - trailing) or (breakeven_after_partial and ratio <= 1.0)):
            return realized + remaining * (ratio - 1.0) - cost_pct, "trailing"
        if not partial and trail_activation is not None and peak >= 1.0 + trail_activation and ratio <= peak * (1.0 - trailing):
            return realized + remaining * (ratio - 1.0) - cost_pct, "trailing"
        if minutes >= max_hold:
            return realized + remaining * (ratio - 1.0) - cost_pct, "time"
        if stale is not None and not partial and minutes >= stale and ratio < 1.05:
            return realized + remaining * (ratio - 1.0) - cost_pct, "stale"
    last = float(path[-1][1]) if path else 1.0
    return realized + remaining * (last - 1.0) - cost_pct, "rug" if rugged else "horisont"


def verdict_for(stage: str, pnl: float, rugged: bool) -> str:
    if stage == "købt":
        return "taken"
    if rugged or pnl <= -0.1:
        return "dodged"
    if pnl >= 0.1:
        return "missed"
    return "neutral"


def optimize_targets(paths: list[tuple[list, bool]], *, frac: float, max_hold: float, cost_pct: float) -> dict:
    """Hvilket gevinstmål og stop ville have givet det bedste gennemsnit på de observerede kurs-stier?"""
    cells = []
    for tp in TARGET_GRID:
        for sl in STOP_GRID:
            results = [
                simulate_path(path, tp=tp, frac=frac, sl=sl, max_hold=max_hold, rugged=rugged, cost_pct=cost_pct, breakeven_after_partial=True)[0]
                for path, rugged in paths
            ]
            n = len(results)
            cells.append(
                {
                    "tp": tp,
                    "sl": sl,
                    "n": n,
                    "avg": sum(results) / n if n else None,
                    "win_rate": sum(1 for value in results if value > 0) / n if n else None,
                }
            )
    ranked = [cell for cell in cells if cell["avg"] is not None]
    best = max(ranked, key=lambda cell: cell["avg"]) if ranked else None
    return {"n": len(paths), "enough": len(paths) >= MIN_PATHS, "cells": cells, "best": best, "targets": list(TARGET_GRID), "stops": list(STOP_GRID)}


def shadow_summary(done: list[dict]) -> list[dict]:
    """Pr. trin: hvor mange afviste tokens faldt, steg eller stod stille bagefter."""
    stages: dict[str, dict] = {}
    for row in done:
        stage = str(row.get("stage") or "?")
        entry = stages.setdefault(stage, {"stage": stage, "n": 0, "missed": 0, "dodged": 0, "neutral": 0, "taken": 0, "sum": 0.0})
        entry["n"] += 1
        entry[str(row.get("verdict") or "neutral")] = entry.get(str(row.get("verdict") or "neutral"), 0) + 1
        entry["sum"] += float(row.get("sim_pnl_pct") or 0.0)
    for entry in stages.values():
        entry["avg"] = entry["sum"] / entry["n"] if entry["n"] else None
    return sorted(stages.values(), key=lambda item: item["n"], reverse=True)
