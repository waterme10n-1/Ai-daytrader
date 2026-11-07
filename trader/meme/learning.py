"""Scoring og læring.

Hver kandidat bliver til et lille sæt tal (features). En logistisk model giver en score
mellem 0 og 1. Efter hver lukket handel flyttes vægtene mod det, der faktisk virkede, og
strategiparametrene justeres inden for faste grænser. Alt gemmes, så man kan se, hvad
botten har lært, og hvorfor.
"""

from __future__ import annotations

import math
import time
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
from trader.meme.safety import has_real_socials

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
    "socials": ("Har Telegram og X", 0.4),
    "x_buzz": ("Omtale på X", 0.5),
    "copy_signal": ("Købt af en fulgt trader", 0.7),
    "boosted": ("Betalt promovering (boost)", -0.1),
    "verified": ("Verificeret på Jupiter", 0.3),
    "ai_view": ("Claudes vurdering", 0.6),
    "insiders": ("Insider-wallets (RugCheck)", -0.6),
    "dev_mints": ("Udvikleren har lavet mange tokens", -0.5),
    "holder_growth": ("Flere holdere sidste time", 0.3),
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
    has_x, has_tg, has_web = has_real_socials(pair, token)
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
        # Telegram og X vejer mest; en hjemmeside alene siger næsten intet.
        "socials": 0.45 * has_tg + 0.4 * has_x + 0.15 * has_web,
        "x_buzz": 0.0 if buzz is None else _clip(math.log10(1 + buzz.authors) / 2.0, 0.0, 1.0),
        "copy_signal": 1.0 if copy_signal else 0.0,
        "boosted": 1.0 if (boosted or pair.boosts_active > 0) else 0.0,
        "verified": 1.0 if token is not None and token.verified else 0.0,
        "ai_view": _clip(ai_view, -1.0, 1.0),
        "insiders": _clip(math.log10(1 + max(0, report.insiders)) / 2.0, 0.0, 1.0) if report is not None else 0.0,
        "dev_mints": _clip(math.log10(max(1, token.dev_mints)) / 2.0, 0.0, 1.0) if token is not None and token.dev_mints else 0.0,
        "holder_growth": _clip(_num_or(token.stats_1h.get("holderChange"), 0.0) / 30.0, -1.0, 1.0) if token is not None else 0.0,
    }


def _num_or(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def facts_of(
    pair: PairInfo,
    *,
    token: TokenInfo | None = None,
    report: RugReport | None = None,
    boosted: bool = False,
    now_ms: int | None = None,
) -> dict[str, float]:
    """De rå tal bag en beslutning. Gemmes med hver skyggehandel og position, så man bagefter
    kan se, hvilke egenskaber der faktisk hang sammen med gevinst og tab."""
    facts: dict[str, float] = {}

    def put(key: str, value: Any, digits: int = 3) -> None:
        if value is None:
            return
        number = _num_or(value, float("nan"))
        if math.isfinite(number):
            facts[key] = round(number, digits)

    liquidity = pair.liquidity_usd
    put("liq", liquidity, 0)
    put("mcap", pair.mcap or None, 0)
    put("age_min", pair.age_minutes(now_ms), 2)
    put("chg_m5", pair.change.get("m5"), 2)
    put("chg_h1", pair.change.get("h1"), 2)
    put("vol_h1", pair.volume.get("h1"), 0)
    put("txns_h1", pair.txns("h1"), 0)
    put("buy_ratio_h1", pair.buy_ratio("h1"))
    put("buy_ratio_m5", pair.buy_ratio("m5"))
    if liquidity > 0:
        put("vol_liq", pair.volume.get("h1", 0.0) / liquidity)
    has_x, has_tg, has_web = has_real_socials(pair, token)
    facts.update({"x": int(has_x), "tg": int(has_tg), "web": int(has_web), "boosted": int(bool(boosted or pair.boosts_active > 0))})
    if token is not None:
        put("organic", token.organic_score, 1)
        put("holders", token.holders, 0)
        put("top10", token.top_holders_pct, 2)
        put("dev_pct", token.dev_balance_pct, 2)
        put("dev_mints", token.dev_mints, 0)
        facts["sus"] = int(token.sus)
        facts["verified"] = int(token.verified)
        put("holder_chg_h1", token.stats_1h.get("holderChange"), 2)
        put("organic_buyers_h1", token.stats_1h.get("numOrganicBuyers"), 0)
        put("net_buyers_h1", token.stats_1h.get("numNetBuyers"), 0)
    if report is not None:
        put("rug_risk", report.risk)
        put("insiders", report.insiders, 0)
        put("top10", report.top10_pct, 2)
        put("lp_locked", report.lp_locked_pct, 1)
        put("rc_holders", report.holders, 0)
        facts["dangers"] = len(report.dangers)
        facts["warnings"] = len(report.warnings)
    facts["hour"] = int(((now_ms or int(time.time() * 1000)) // 3_600_000) % 24)
    return facts


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


# Den almindelige strategis mål og stop skal holde sig inden for PARAM_BOUNDS.
MOMENTUM_TARGET_GRID = (0.25, 0.4, 0.6, 1.0, 2.0)
MOMENTUM_STOP_GRID = (0.15, 0.2, 0.25, 0.3, 0.35)


def optimize_targets(
    paths: list[tuple[list, bool]],
    *,
    frac: float,
    max_hold: float,
    cost_pct: float,
    rules: dict | None = None,
    targets: tuple[float, ...] = TARGET_GRID,
    stops: tuple[float, ...] = STOP_GRID,
) -> dict:
    """Hvilket gevinstmål og stop ville have givet det bedste gennemsnit på de observerede kurs-stier?

    `rules` er strategiens øvrige salgsregler (trailing, stale osv.), så kun mål og stop varieres."""
    extra = {"breakeven_after_partial": True} if rules is None else dict(rules)
    cells = []
    for tp in targets:
        for sl in stops:
            results = [
                simulate_path(path, tp=tp, frac=frac, sl=sl, max_hold=max_hold, rugged=rugged, cost_pct=cost_pct, **extra)[0]
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
    return {"n": len(paths), "enough": len(paths) >= MIN_PATHS, "cells": cells, "best": best, "targets": list(targets), "stops": list(stops)}


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


# --------------------------------------------------------------------------- hvad virker? (faktaanalyse)

FACT_LABELS: dict[str, str] = {
    "organic": "Organisk score (Jupiter)",
    "liq": "Likviditet (USD)",
    "mcap": "Markedsværdi (USD)",
    "age_min": "Alder (min.)",
    "chg_h1": "Stigning sidste time (%)",
    "chg_m5": "Stigning sidste 5 min. (%)",
    "buy_ratio_h1": "Andel køb sidste time",
    "vol_liq": "Volumen/likviditet",
    "top10": "Top-10 ejerandel (%)",
    "insiders": "Insider-wallets",
    "holders": "Holdere",
    "holder_chg_h1": "Holder-vækst sidste time (%)",
    "dev_mints": "Tokens lavet af udvikleren",
    "rug_risk": "Rug-risiko (0-1)",
    "tg": "Har Telegram",
    "x": "Har X",
    "web": "Har hjemmeside",
    "boosted": "Betalt boost",
}
BINARY_FACTS = {"tg", "x", "web", "boosted", "sus", "verified"}
FACT_MIN_ROWS = 30
BUCKET_MIN_ROWS = 8


def _nice(value: float) -> float:
    """Rund en grænse til to betydende cifre, så den kan læses ("31.000" i stedet for "30.874")."""
    if value == 0 or not math.isfinite(value):
        return 0.0
    digits = 1 - int(math.floor(math.log10(abs(value))))
    return round(value, digits)


def _edge(value: float) -> str:
    """Grænse i en gruppe-etiket: hele tal uden decimaler ("3", ikke "3.00")."""
    if value == int(value) and abs(value) < 1000:
        return str(int(value))
    return _fmt(value)


def _bucket_stats(label: str, values: list[float]) -> dict:
    n = len(values)
    enough = n >= BUCKET_MIN_ROWS
    return {
        "label": label,
        "n": n,
        "avg": sum(values) / n if enough else None,
        "win_rate": sum(1 for value in values if value > 0) / n if enough else None,
    }


def fact_report(rows: list[dict]) -> list[dict]:
    """rows: [{"facts": {...}, "pnl": resultat i brøk}]. For hver egenskab: resultat i tre lige store grupper
    (eller ja/nej). Sorteret, så egenskaberne med størst forskel mellem grupperne står øverst."""
    out = []
    for key, label in FACT_LABELS.items():
        pairs = [(float(row["facts"][key]), float(row["pnl"])) for row in rows if key in (row.get("facts") or {})]
        if len(pairs) < FACT_MIN_ROWS:
            continue
        if key in BINARY_FACTS:
            buckets = [
                _bucket_stats("Nej", [pnl for value, pnl in pairs if value < 0.5]),
                _bucket_stats("Ja", [pnl for value, pnl in pairs if value >= 0.5]),
            ]
        else:
            ordered = sorted(value for value, _pnl in pairs)
            low = _nice(ordered[len(ordered) // 3])
            high = _nice(ordered[(2 * len(ordered)) // 3])
            if high <= low:
                continue
            buckets = [
                _bucket_stats(f"under {_edge(low)}", [pnl for value, pnl in pairs if value < low]),
                _bucket_stats(f"{_edge(low)}–{_edge(high)}", [pnl for value, pnl in pairs if low <= value < high]),
                _bucket_stats(f"{_edge(high)}+", [pnl for value, pnl in pairs if value >= high]),
            ]
        avgs = [bucket["avg"] for bucket in buckets if bucket["avg"] is not None]
        if len(avgs) < 2:
            continue
        out.append({"fact": key, "label": label, "n": len(pairs), "buckets": buckets, "spread": max(avgs) - min(avgs)})
    return sorted(out, key=lambda item: item["spread"], reverse=True)


# Hvilke filtre læringen må stramme ud fra skyggehandlernes fakta. "min": højere er strengere; "max": lavere er strengere.
# scale omregner faktaens enhed til indstillingens (alder i minutter → sniperens sekunder).
TIGHTEN_RULES: dict[str, list[dict]] = {
    "momentum": [
        {"fact": "organic", "param": "min_organic_score", "kind": "min", "grid": (20, 30, 40, 50, 60, 70)},
        {"fact": "liq", "param": "min_liquidity_usd", "kind": "min", "grid": (25_000, 40_000, 60_000, 100_000, 150_000)},
        {"fact": "mcap", "param": "min_mcap_usd", "kind": "min", "grid": (60_000, 100_000, 250_000, 500_000)},
        {"fact": "age_min", "param": "min_age_minutes", "kind": "min", "grid": (30, 60, 120, 240)},
        {"fact": "chg_h1", "param": "max_change_h1_pct", "kind": "max", "grid": (250, 150, 100, 60)},
        {"fact": "top10", "param": "max_top10_pct", "kind": "max", "grid": (35, 30, 25, 20)},
        {"fact": "insiders", "param": "max_insiders", "kind": "max", "grid": (50, 30, 15, 8)},
        {"fact": "rug_risk", "param": "max_rug_score", "kind": "max", "grid": (0.55, 0.45, 0.35)},
    ],
    "sniper": [
        {"fact": "age_min", "param": "min_age_seconds", "kind": "min", "scale": 60.0, "grid": (300, 420, 600, 900)},
        {"fact": "liq", "param": "min_liquidity_usd", "kind": "min", "grid": (8_000, 12_000, 20_000, 35_000)},
        {"fact": "holders", "param": "min_holders", "kind": "min", "grid": (25, 50, 100, 200, 400)},
        {"fact": "top10", "param": "max_top10_pct", "kind": "max", "grid": (30, 25, 20, 15)},
        {"fact": "organic", "param": "min_organic_score", "kind": "min", "grid": (10, 20, 30, 40)},
    ],
}
TIGHTEN_MIN_REMOVED = 25
TIGHTEN_MIN_KEPT = 20
TIGHTEN_MAX_REMOVED_AVG = -0.05
TIGHTEN_MIN_GAP = 0.05


def _passes(row: dict, rule: dict, threshold: float) -> bool:
    value = (row.get("facts") or {}).get(rule["fact"])
    if value is None:
        return True
    value = float(value) * float(rule.get("scale", 1.0))
    return value >= threshold if rule["kind"] == "min" else value <= threshold


def tighten_suggestion(rows: list[dict], current: dict[str, float], rules: list[dict]) -> dict | None:
    """Det ene filter, hvor en strammere grænse ville have fjernet flest tabende handler.

    Kun strammere end nu, kun med mindst TIGHTEN_MIN_REMOVED fjernede tokens, der i snit tabte
    mindst 5 %, og kun hvis de resterende klarede sig tydeligt bedre. Ellers None."""
    best: dict | None = None
    for rule in rules:
        name = rule["param"]
        if name not in current:
            continue
        now = float(current[name])
        others = [other for other in rules if other is not rule and other["param"] in current]
        pool = [
            row
            for row in rows
            if rule["fact"] in (row.get("facts") or {})
            and _passes(row, rule, now)
            and all(_passes(row, other, float(current[other["param"]])) for other in others)
        ]
        for candidate in rule["grid"]:
            stricter = candidate > now if rule["kind"] == "min" else candidate < now
            if not stricter:
                continue
            removed = [float(row["pnl"]) for row in pool if not _passes(row, rule, candidate)]
            kept = [float(row["pnl"]) for row in pool if _passes(row, rule, candidate)]
            if len(removed) < TIGHTEN_MIN_REMOVED or len(kept) < TIGHTEN_MIN_KEPT:
                continue
            removed_avg = sum(removed) / len(removed)
            kept_avg = sum(kept) / len(kept)
            if removed_avg > TIGHTEN_MAX_REMOVED_AVG or kept_avg - removed_avg < TIGHTEN_MIN_GAP:
                continue
            saved = -sum(removed)
            if best is None or saved > best["saved"]:
                best = {
                    "param": name,
                    "fact": rule["fact"],
                    "old": now,
                    "new": float(candidate),
                    "removed": len(removed),
                    "removed_avg": removed_avg,
                    "kept": len(kept),
                    "kept_avg": kept_avg,
                    "saved": saved,
                }
    return best
