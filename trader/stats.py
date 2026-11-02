"""Resultatmål og tjekliste for oplåsning af rigtige penge.

Syntetiske handler må ikke indgå. Kald kun med papirhandler uden synthetic-flag.
"""

from __future__ import annotations

from typing import Any

from trader.config import LIVE_CONFIRMATION_PHRASE


def _closed(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in trades if row.get("status") == "closed" and row.get("pnl") is not None]


def max_drawdown(equity_rows: list[dict[str, Any]]) -> float:
    peak: float | None = None
    worst = 0.0
    for row in equity_rows:
        equity = row["end_equity"] if row.get("end_equity") is not None else row.get("start_equity")
        if equity is None:
            continue
        equity = float(equity)
        if peak is None or equity > peak:
            peak = equity
        if peak and peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return worst


def summarize(trades: list[dict[str, Any]], equity_rows: list[dict[str, Any]]) -> dict[str, Any]:
    closed = _closed(trades)
    wins = [row for row in closed if float(row["pnl"]) > 0]
    losses = [row for row in closed if float(row["pnl"]) < 0]
    gross_profit = sum(float(row["pnl"]) for row in wins)
    gross_loss = abs(sum(float(row["pnl"]) for row in losses))
    if gross_loss > 0:
        profit_factor: float | None = gross_profit / gross_loss
        infinite = False
    elif gross_profit > 0:
        profit_factor = None
        infinite = True
    else:
        profit_factor = None
        infinite = False
    net = sum(float(row["pnl"]) for row in closed)
    win_rate = (len(wins) / len(closed)) if closed else None
    curve = []
    if equity_rows and equity_rows[0].get("start_equity") is not None:
        curve.append(
            {
                "date": "Start",
                "equity": float(equity_rows[0]["start_equity"]),
                "pnl": 0.0,
                "synthetic": bool(equity_rows[0].get("synthetic")),
            }
        )
    for row in equity_rows:
        equity = row["end_equity"] if row.get("end_equity") is not None else row.get("start_equity")
        curve.append(
            {
                "date": row["trading_day"],
                "equity": float(equity) if equity is not None else None,
                "pnl": row.get("pnl"),
                "synthetic": bool(row.get("synthetic")),
            }
        )
    latest = curve[-1]["equity"] if curve else None
    today_pnl = None
    if equity_rows:
        last = equity_rows[-1]
        if last.get("pnl") is not None:
            today_pnl = float(last["pnl"])
        elif last.get("end_equity") is not None:
            today_pnl = float(last["end_equity"]) - float(last["start_equity"])
    trading_days = len(equity_rows)
    return {
        "equity": latest,
        "today_pnl": today_pnl,
        "net_pnl": net,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "profit_factor_infinite": infinite,
        "max_drawdown": max_drawdown(equity_rows),
        "trades": len(closed),
        "trading_days": trading_days,
        "equity_curve": [point for point in curve if point["equity"] is not None],
    }


def unlock_checklist(
    *,
    trades: list[dict[str, Any]],
    equity_rows: list[dict[str, Any]],
    allow_live: bool,
    live_keys_present: bool,
    phrase_accepted: bool,
) -> dict[str, Any]:
    summary = summarize(trades, equity_rows)
    days = summary["trading_days"]
    count = summary["trades"]
    net = summary["net_pnl"]
    factor = summary["profit_factor"]
    infinite = summary["profit_factor_infinite"]
    drawdown = summary["max_drawdown"]
    factor_ok = infinite or (factor is not None and factor >= 1.2)
    items = [
        {
            "id": "days",
            "label": "Mindst 60 papirhandelsdage",
            "ok": days >= 60,
            "detail": f"{days} / 60",
        },
        {
            "id": "trades",
            "label": "Mindst 50 lukkede handler",
            "ok": count >= 50,
            "detail": f"{count} / 50",
        },
        {
            "id": "pnl",
            "label": "Nettoresultat over 0",
            "ok": net > 0,
            "detail": f"{net:.2f} USD",
        },
        {
            "id": "profit_factor",
            "label": "Profitfaktor mindst 1,2",
            "ok": factor_ok and count > 0,
            "detail": "∞" if infinite else ("—" if factor is None else f"{factor:.2f}"),
        },
        {
            "id": "drawdown",
            "label": "Maksimalt drawdown under 10 %",
            "ok": drawdown < 0.10 and days > 0,
            "detail": f"{drawdown * 100:.1f} %",
        },
        {
            "id": "allow_live",
            "label": "ALLOW_LIVE=true i .env",
            "ok": allow_live,
            "detail": "ja" if allow_live else "nej",
        },
        {
            "id": "live_keys",
            "label": "Live-nøgler findes i .env",
            "ok": live_keys_present,
            "detail": "ja" if live_keys_present else "nej",
        },
        {
            "id": "phrase",
            "label": "Bekræftelsessætning er afgivet",
            "ok": phrase_accepted,
            "detail": LIVE_CONFIRMATION_PHRASE,
        },
    ]
    return {
        "items": items,
        "all_passed": all(item["ok"] for item in items),
        "ready_for_phrase": all(item["ok"] for item in items if item["id"] != "phrase"),
        "phrase": LIVE_CONFIRMATION_PHRASE,
        "summary": summary,
    }
