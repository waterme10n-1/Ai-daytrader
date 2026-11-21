"""Scorer og analyser, der kun bygger på rigtige data fra botten. Ingen tilfældige tal og ingen pyntede procenter.

- safety_score: 0-100 for et tokens sikkerhed. Hvert fradrag er en forklaring, man kan læse på siden.
- ai_score: 0-100 fra bottens egen lærte model (ikke en AI-"confidence" fra et sprogmodel-gæt) og et handlingsforslag.
- market_regime: hvilken slags marked SOL er i lige nu, ud fra den kursrække botten selv har målt.
- performance: ydelsen opdelt efter strategi, kilde, tidspunkt og salgsgrund, kun fra lukkede handler.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from typing import Any

# --------------------------------------------------------------------------- sikkerhedsscore


def safety_score(
    report: Any | None,
    token: Any | None = None,
    pair: Any | None = None,
    *,
    roundtrip_loss_pct: float | None = None,
) -> dict[str, Any]:
    """0-100. 100 = ingen kendte problemer. Hvert fradrag står i `factors`, og manglende data trækker fra, fordi ukendt ikke er sikkert."""
    factors: list[dict[str, Any]] = []

    def cut(points: int, label: str) -> None:
        if points > 0:
            factors.append({"label": label, "points": int(points)})

    if report is None:
        cut(25, "Intet rug-tjek (ukendt er ikke sikkert)")
    else:
        if getattr(report, "rugged", False):
            return {"score": 0, "grade": "Farlig", "factors": [{"label": "RugCheck: tokenet er rugget", "points": 100}], "known": True}
        dangers = list(getattr(report, "dangers", []) or [])
        cut(min(45, 15 * len(dangers)), f"{len(dangers)} farlige punkter hos RugCheck" if dangers else "")
        if getattr(report, "mint_authority", False):
            cut(20, "Mint authority er ikke slået fra (nye tokens kan skabes)")
        if getattr(report, "freeze_authority", False):
            cut(20, "Freeze authority er ikke slået fra (wallets kan fryses)")
        locked = getattr(report, "lp_locked_pct", None)
        if locked is None:
            cut(5, "Ukendt hvor meget af likviditeten der er låst eller brændt")
        elif locked < 50:
            cut(15, f"Kun {locked:.0f} % af likviditeten er låst eller brændt")
        elif locked < 90:
            cut(5, f"{locked:.0f} % af likviditeten er låst eller brændt")
        top10 = getattr(report, "top10_pct", None)
        if top10 is not None:
            if top10 > 50:
                cut(20, f"Top 10 ejer {top10:.0f} %")
            elif top10 > 35:
                cut(12, f"Top 10 ejer {top10:.0f} %")
            elif top10 > 20:
                cut(5, f"Top 10 ejer {top10:.0f} %")
        insiders = int(getattr(report, "insiders", 0) or 0)
        cut(min(15, insiders * 3), f"{insiders} insider-wallets" if insiders else "")
    if token is not None:
        if getattr(token, "sus", False):
            cut(25, "Jupiter har markeret tokenet som mistænkeligt")
        dev_mints = getattr(token, "dev_mints", None)
        if dev_mints is not None and dev_mints > 5:
            cut(10, f"Udvikleren har lavet {dev_mints} tokens (token-fabrik)")
        dev_pct = getattr(token, "dev_balance_pct", None)
        if dev_pct is not None and dev_pct > 10:
            cut(10, f"Udvikleren ejer stadig {dev_pct:.0f} %")
        holders = getattr(token, "holders", None)
    else:
        holders = None
    if holders is None and report is not None:
        holders = getattr(report, "holders", None)
    if holders is not None:
        if holders < 30:
            cut(15, f"Kun {holders} holdere")
        elif holders < 100:
            cut(8, f"Kun {holders} holdere")
    liquidity = getattr(pair, "liquidity_usd", None) if pair is not None else None
    if liquidity is not None and liquidity < 10_000:
        cut(10, f"Lav likviditet (${liquidity:,.0f})".replace(",", "."))
    if roundtrip_loss_pct is not None and roundtrip_loss_pct > 10:
        cut(15, f"Køb og salg koster {roundtrip_loss_pct:.0f} % (honeypot-tegn eller tynd pulje)")
    total = max(0, 100 - sum(item["points"] for item in factors))
    grade = "Høj" if total >= 80 else "Middel" if total >= 60 else "Lav" if total >= 40 else "Farlig"
    return {"score": total, "grade": grade, "factors": factors, "known": report is not None}


# --------------------------------------------------------------------------- AI-score


BANDS = ((90, "Exceptional setup"), (80, "Strong setup"), (70, "Potential setup"), (50, "Weak setup"), (0, "Avoid"))


def ai_score(model_score: float | None, safety: int | None = None) -> dict[str, Any]:
    """Bottens egen lærte model som 0-100 med band og forslag. Scoren bestemmer aldrig positionens størrelse.

    Handlingsforslaget kræver også en brugbar sikkerhedsscore: en høj model-score på et farligt token er Avoid."""
    if model_score is None or not math.isfinite(float(model_score)):
        return {"score": None, "band": "Ingen score", "action": "Hold"}
    score = int(round(max(0.0, min(1.0, float(model_score))) * 100))
    band = next(label for limit, label in BANDS if score >= limit)
    if safety is not None and safety < 40:
        action = "Avoid"
    elif score >= 90 and (safety is None or safety >= 80):
        action = "Strong Buy"
    elif score >= 70 and (safety is None or safety >= 60):
        action = "Buy"
    elif score >= 50:
        action = "Hold"
    else:
        action = "Avoid"
    return {"score": score, "band": band, "action": action}


# --------------------------------------------------------------------------- marked-regime

REGIME_MIN_SECONDS = 600


def market_regime(samples: list[tuple[float, float]], now: float, *, sol_filter_pct: float = 5.0) -> dict[str, Any]:
    """Regime ud fra SOL-kurserne botten selv har målt (op til ca. 80 minutter). Længere regimer (bull/bear over dage) kan botten
    ikke se endnu, og det siger den."""
    points = [(t, p) for t, p in samples if p > 0 and now - t <= 2 * 3600]
    if len(points) < 3 or points[-1][0] - points[0][0] < REGIME_MIN_SECONDS:
        return {"label": "Ukendt", "detail": "Botten har målt SOL i under 10 minutter og kan ikke se regimet endnu.", "trade": True, "minutes": 0, "change_pct": None, "range_pct": None}
    first, last = points[0][1], points[-1][1]
    change = (last - first) / first * 100.0
    low, high = min(p for _, p in points), max(p for _, p in points)
    spread = (high - low) / low * 100.0
    minutes = int((points[-1][0] - points[0][0]) / 60)
    window = f"over de seneste {minutes} min."
    if change <= -sol_filter_pct:
        label, detail, trade = "Risk-off", f"SOL er {change:.1f} % {window}. Det er et faldende marked.", False
    elif spread >= 3.0 and abs(change) < 1.0:
        label, detail, trade = "Høj volatilitet", f"SOL svinger {spread:.1f} % {window} uden retning.", True
    elif change >= 2.0:
        label, detail, trade = "Stærkt momentum", f"SOL er {change:+.1f} % {window}.", True
    elif change <= -2.0:
        label, detail, trade = "Bear", f"SOL er {change:.1f} % {window}.", True
    elif spread < 1.0:
        label, detail, trade = "Sidelæns, lav volatilitet", f"SOL bevæger sig kun {spread:.1f} % {window}.", True
    else:
        label, detail, trade = "Sidelæns", f"SOL er {change:+.1f} % {window}.", True
    return {"label": label, "detail": detail.replace(".", ","), "trade": trade, "minutes": minutes, "change_pct": round(change, 2), "range_pct": round(spread, 2)}


# --------------------------------------------------------------------------- ydelse


def _hour(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).hour
    except ValueError:
        return None


def _bucket(rows: list[dict]) -> dict[str, Any]:
    wins = [float(r["pnl_usd"]) for r in rows if float(r["pnl_usd"]) > 0]
    losses = [float(r["pnl_usd"]) for r in rows if float(r["pnl_usd"]) < 0]
    total = sum(float(r["pnl_usd"]) for r in rows)
    gross_loss = -sum(losses)
    return {
        "trades": len(rows),
        "wins": len(wins),
        "win_rate": len(wins) / len(rows) if rows else None,
        "pnl": round(total, 4),
        "avg_win": (sum(wins) / len(wins)) if wins else None,
        "avg_loss": (sum(losses) / len(losses)) if losses else None,
        "profit_factor": (sum(wins) / gross_loss) if gross_loss > 0 else None,
        "expectancy": total / len(rows) if rows else None,
    }


def performance(closed: list[dict], equity_series: list[dict] | None = None, *, min_hour_trades: int = 3) -> dict[str, Any]:
    """Ydelsen fra lukkede handler. closed er række fra positions-tabellen for ÉN tilstand (papir eller live), så de aldrig blandes."""
    groups: dict[str, dict[str, list[dict]]] = {"strategy": defaultdict(list), "source": defaultdict(list), "exit": defaultdict(list), "hour": defaultdict(list)}
    for row in closed:
        groups["strategy"][str(row.get("strategy") or "momentum")].append(row)
        groups["source"][str(row.get("source") or "ukendt")].append(row)
        groups["exit"][str(row.get("exit_reason") or "ukendt")].append(row)
        hour = _hour(row.get("opened_at"))
        if hour is not None:
            groups["hour"][str(hour)].append(row)

    def table(name: str) -> list[dict]:
        return sorted(({"key": key, **_bucket(rows)} for key, rows in groups[name].items()), key=lambda item: item["pnl"], reverse=True)

    hours = [item for item in table("hour") if item["trades"] >= min_hour_trades]
    peak = drawdown = 0.0
    current_dd = 0.0
    values = [float(point["equity"]) for point in (equity_series or []) if point.get("equity") is not None]
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            drawdown = max(drawdown, (peak - value) / peak)
    if values and peak > 0:
        current_dd = (peak - values[-1]) / peak
    return {
        "overall": _bucket(closed),
        "by_strategy": table("strategy"),
        "by_source": table("source"),
        "by_exit": table("exit"),
        "by_hour": sorted(table("hour"), key=lambda item: int(item["key"])),
        "best_hours": hours[:3],
        "worst_hours": list(reversed(hours[-3:])) if len(hours) > 3 else [],
        "hour_note": f"Kun timer med mindst {min_hour_trades} handler tæller. Tiderne er UTC (dansk tid er 1-2 timer senere).",
        "current_drawdown_pct": round(current_dd * 100, 2),
        "max_drawdown_pct": round(drawdown * 100, 2),
        "equity_points": len(values),
    }
