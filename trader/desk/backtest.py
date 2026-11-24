"""Backtest og papirturnering for strategierne. Rene funktioner uden netværk.

Ærlighed først:
- Signalet på bar i bruger kun data til og med bar i. Handlen udføres til åbningen af bar i+1 (ingen look-ahead).
- Stop vinder, hvis stop og mål rammes i samme bar (konservativt). Stop og flatten fyldes med slippage mod os.
- Omkostninger (slippage, provision, short-lån) er antagelser og står i resultatet. Backtest er ikke en garanti.
- Ingen parametre optimeres, så der er intet at overfitte. Walk-forward her betyder: samme faste regler målt i fortløbende, adskilte perioder.
- Mange handler giver tillid, få gør ikke: resultater under 30 handler markeres som upålidelige.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from trader.desk.alpaca import Bar, resolve_intrabar
from trader.desk.strategies import DETECTORS, LIBRARY, MIN_BARS, DayData, Signal, scan_day

RELIABLE_TRADES = 30
FLATTEN_BEFORE_END = 10  # minutter før sidste bar
ENTRY_CUTOFF_BEFORE_END = 30


@dataclass
class Costs:
    slippage_bps: float = 5.0  # pr. side, mod os
    commission_per_share: float = 0.0
    short_borrow_bps_per_day: float = 3.0

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


# --------------------------------------------------------------------------- én handel


def simulate_trade(sig: Signal, d: DayData, costs: Costs, *, flatten_index: int, date: str, regime: list[str] | None = None) -> dict[str, Any] | None:
    """Udfør signalet på bar sig.i til åbningen af næste bar, og følg det til stop, mål eller flatten. None, hvis det ikke kan udføres."""
    j0 = sig.i + 1
    if j0 >= len(d.bars) or j0 > flatten_index:
        return None
    slip = costs.slippage_bps / 10_000.0
    entry = d.bars[j0].open * (1 + slip) if sig.side == "long" else d.bars[j0].open * (1 - slip)
    stop, target = sig.stop, sig.target
    if sig.side == "long" and not (stop < entry < target):
        return None  # kursen sprang forbi stop eller mål
    if sig.side == "short" and not (target < entry < stop):
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    exit_price, reason, held = None, "flatten", 0
    for j in range(j0, min(len(d.bars), flatten_index + 1)):
        bar = d.bars[j]
        hit = resolve_intrabar(sig.side, stop, target, bar)
        held = j - j0 + 1
        if hit is not None:
            kind, fill = hit
            reason = kind
            exit_price = fill * (1 - slip) if (kind == "stop" and sig.side == "long") else fill * (1 + slip) if (kind == "stop" and sig.side == "short") else fill
            break
    if exit_price is None:
        last = d.bars[min(len(d.bars) - 1, flatten_index)]
        exit_price = last.close * (1 - slip) if sig.side == "long" else last.close * (1 + slip)
        held = max(1, min(len(d.bars) - 1, flatten_index) - j0 + 1)
    gross = (exit_price - entry) if sig.side == "long" else (entry - exit_price)
    fees = 2 * costs.commission_per_share + (entry * costs.short_borrow_bps_per_day / 10_000.0 if sig.side == "short" else 0.0)
    net = gross - fees
    # omkostning i R: hvad slippage og gebyrer kostede, målt i risiko
    cost_r = (entry * slip * 2 + fees) / risk
    return {
        "strategy": sig.strategy, "symbol": sig.symbol, "date": date, "side": sig.side, "entry": round(entry, 4), "exit": round(exit_price, 4),
        "stop": sig.stop, "target": sig.target, "reason": reason, "r": round(net / risk, 3), "pnl_pct": round(net / entry * 100.0, 3),
        "bars": held, "quality": sig.quality, "cost_r": round(cost_r, 3), "regime": list(regime or []),
    }


# --------------------------------------------------------------------------- nøgletal


def metrics(trades: list[dict]) -> dict[str, Any]:
    """Nøgletal i R (gevinst målt i risiko). R gør strategier med forskellig kursstørrelse sammenlignelige."""
    n = len(trades)
    out: dict[str, Any] = {"trades": n, "reliable": n >= RELIABLE_TRADES}
    if n == 0:
        return {**out, "win_rate": None, "profit_factor": None, "avg_win_r": None, "avg_loss_r": None, "expectancy_r": None, "total_r": 0.0,
                "max_drawdown_r": 0.0, "sharpe": None, "avg_cost_r": None}
    rs = [float(t["r"]) for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    gross_loss = -sum(losses)
    peak = equity = dd = 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    daily: dict[str, float] = {}
    for t in trades:
        daily[str(t["date"])] = daily.get(str(t["date"]), 0.0) + float(t["r"])
    sharpe = None
    if len(daily) >= 10:
        values = list(daily.values())
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
        if var > 0:
            sharpe = round(mean / math.sqrt(var) * math.sqrt(252), 2)
    return {
        **out, "win_rate": len(wins) / n, "profit_factor": (sum(wins) / gross_loss) if gross_loss > 0 else None,
        "avg_win_r": (sum(wins) / len(wins)) if wins else None, "avg_loss_r": (sum(losses) / len(losses)) if losses else None,
        "expectancy_r": sum(rs) / n, "total_r": round(sum(rs), 3), "max_drawdown_r": round(dd, 3), "sharpe": sharpe,
        "avg_cost_r": sum(float(t.get("cost_r") or 0.0) for t in trades) / n,
    }


def by_regime(trades: list[dict]) -> dict[str, dict]:
    tags: dict[str, list[dict]] = {}
    for t in trades:
        for tag in t.get("regime") or ["ukendt"]:
            tags.setdefault(tag, []).append(t)
    return {tag: metrics(rows) for tag, rows in sorted(tags.items())}


def split_in_out(trades: list[dict], ratio: float = 0.6) -> dict[str, Any]:
    """Første del af dagene (ældst) og sidste del adskilt. Out-of-sample er den del, strategien aldrig er tilpasset til."""
    dates = sorted({str(t["date"]) for t in trades})
    if len(dates) < 4:
        return {"in_sample": metrics(trades), "out_sample": None, "split_date": None}
    cut = dates[max(1, int(len(dates) * ratio))]
    return {"in_sample": metrics([t for t in trades if str(t["date"]) < cut]), "out_sample": metrics([t for t in trades if str(t["date"]) >= cut]), "split_date": cut}


def walk_forward(trades: list[dict], windows: int = 4) -> list[dict]:
    dates = sorted({str(t["date"]) for t in trades})
    if len(dates) < windows * 2:
        return []
    size = len(dates) // windows
    out = []
    for w in range(windows):
        part = dates[w * size: (w + 1) * size if w < windows - 1 else len(dates)]
        out.append({"from": part[0], "to": part[-1], **metrics([t for t in trades if str(t["date"]) in part])})
    return out


# --------------------------------------------------------------------------- backtest


@dataclass
class SymbolDay:
    symbol: str
    date: str
    bars: list[Bar]
    daily: list[Bar]
    regime: list[str] | None = None


def run_backtest(days: list[SymbolDay], strategy_ids: list[str] | None = None, costs: Costs | None = None, *, session_minutes: int = 390) -> dict[str, Any]:
    """Kør strategierne over historiske dage. En handel pr. strategi, symbol og dag (den første signal)."""
    costs = costs or Costs()
    ids = [s for s in (strategy_ids or list(DETECTORS)) if s in DETECTORS]
    trades: dict[str, list[dict]] = {sid: [] for sid in ids}
    for item in days:
        n = len(item.bars)
        if n < MIN_BARS + ENTRY_CUTOFF_BEFORE_END + 2:
            continue
        d = DayData(item.bars, item.daily, session_minutes)
        flatten_index = n - 1 - FLATTEN_BEFORE_END
        last_entry = n - 1 - ENTRY_CUTOFF_BEFORE_END
        waiting = set(ids)
        for i in range(MIN_BARS, last_entry + 1):
            if not waiting:
                break
            for sig in scan_day(item.symbol, d, i, waiting):
                trade = simulate_trade(sig, d, costs, flatten_index=flatten_index, date=item.date, regime=item.regime)
                waiting.discard(sig.strategy)
                if trade is not None:
                    trades[sig.strategy].append(trade)
    results: dict[str, Any] = {}
    for sid in ids:
        rows = trades[sid]
        results[sid] = {"metrics": metrics(rows), **split_in_out(rows), "walk_forward": walk_forward(rows), "by_regime": by_regime(rows), "sample": rows[-25:]}
    return {"costs": costs.to_dict(), "days": len({(x.symbol, x.date) for x in days}), "dates": sorted({x.date for x in days}), "results": results}


# --------------------------------------------------------------------------- strategi-score og godkendelse

APPROVAL = {"min_trades": 30, "min_expectancy_r": 0.1, "min_profit_factor": 1.3}


def strategy_score(paper: dict[str, Any], backtest: dict[str, Any] | None = None) -> dict[str, Any]:
    """0-100 ud fra bottens egne målinger. Er der under 20 handler i turneringen, står der ingen score, fordi den ville være støj.
    Score = 50 + forventning i R + profit factor - drawdown. Win rate indgår ikke (høj win rate alene er ikke godt)."""
    n = int(paper.get("trades") or 0)
    if n < 20:
        return {"score": None, "why": f"Kun {n} handler i papirturneringen. Der skal mindst 20 til, før en score siger noget."}
    exp = float(paper.get("expectancy_r") or 0.0)
    pf = float(paper.get("profit_factor") or (3.0 if exp > 0 else 0.0))
    dd = float(paper.get("max_drawdown_r") or 0.0)
    score = 50 + 25 * math.tanh(exp / 0.3) + 15 * math.tanh((pf - 1.0) / 0.5) - min(20.0, dd)
    oos = (backtest or {}).get("out_sample") or {}
    if oos.get("trades", 0) >= 10 and (oos.get("expectancy_r") or 0.0) < 0:
        score -= 10
    return {"score": int(round(max(0.0, min(100.0, score)))), "why": "Forventning i R, profit factor og drawdown. Win rate tæller ikke."}


def approval(paper: dict[str, Any], backtest: dict[str, Any] | None = None) -> dict[str, Any]:
    """En strategi må først gå gennem bordets rigtige flow (stadig kun papir hos Alpaca), når papirturneringen har bevist den."""
    reasons: list[str] = []
    if int(paper.get("trades") or 0) < APPROVAL["min_trades"]:
        reasons.append(f"mindst {APPROVAL['min_trades']} handler i papirturneringen (har {int(paper.get('trades') or 0)})")
    if (paper.get("expectancy_r") or 0.0) < APPROVAL["min_expectancy_r"]:
        reasons.append(f"forventning mindst {APPROVAL['min_expectancy_r']} R pr. handel")
    pf = paper.get("profit_factor")
    if pf is None or pf < APPROVAL["min_profit_factor"]:
        reasons.append(f"profit factor mindst {APPROVAL['min_profit_factor']}")
    oos = (backtest or {}).get("out_sample") or {}
    if not oos or oos.get("trades", 0) < 10:
        reasons.append("en backtest med mindst 10 handler uden for stikprøven")
    elif (oos.get("expectancy_r") or 0.0) <= 0:
        reasons.append("positiv forventning i backtestens out-of-sample")
    return {"approved": not reasons, "missing": reasons}


# --------------------------------------------------------------------------- papirturnering


class Tournament:
    """Hver strategi har en virtuel konto og handler de samme live-kurser. Ingen rigtige penge, og ingen ordrer sendes nogen steder."""

    def __init__(self, state: dict | None = None, *, start_equity: float = 10_000.0, risk_pct: float = 0.5, cap_pct: float = 25.0,
                 max_open: int = 5, costs: Costs | None = None) -> None:
        self.state = state or {"start": start_equity, "accounts": {}, "day": None, "done": {}}
        self.risk_pct, self.cap_pct, self.max_open = risk_pct, cap_pct, max_open
        self.costs = costs or Costs()

    def account(self, sid: str) -> dict:
        return self.state["accounts"].setdefault(sid, {"equity": self.state["start"], "open": [], "trades": [], "curve": []})

    def _close(self, acct: dict, pos: dict, price: float, reason: str, ts: str, day: str) -> None:
        slip = self.costs.slippage_bps / 10_000.0
        exit_price = price * (1 - slip) if pos["side"] == "long" else price * (1 + slip)
        if reason == "target":
            exit_price = price
        gross = (exit_price - pos["entry"]) if pos["side"] == "long" else (pos["entry"] - exit_price)
        fees = 2 * self.costs.commission_per_share + (pos["entry"] * self.costs.short_borrow_bps_per_day / 10_000.0 if pos["side"] == "short" else 0.0)
        pnl = (gross - fees) * pos["qty"]
        risk = abs(pos["entry"] - pos["stop"]) or 1e-9
        acct["equity"] = round(acct["equity"] + pnl, 2)
        acct["trades"].append({
            "strategy": pos["strategy"], "symbol": pos["symbol"], "date": day, "side": pos["side"], "entry": pos["entry"], "exit": round(exit_price, 4),
            "stop": pos["stop"], "target": pos["target"], "reason": reason, "r": round((gross - fees) / risk, 3), "pnl": round(pnl, 2), "qty": pos["qty"],
            "pnl_pct": round((gross - fees) / pos["entry"] * 100.0, 3), "quality": pos["quality"], "cost_r": round((pos["entry"] * slip * 2 + fees) / risk, 3),
            "regime": pos.get("regime", []), "opened": pos["ts"], "closed": ts,
        })
        acct["trades"] = acct["trades"][-400:]
        acct["curve"] = (acct["curve"] + [[ts, acct["equity"]]])[-400:]
        acct["open"] = [p for p in acct["open"] if p is not pos]

    def step(self, intraday: dict[str, list[Bar]], daily: dict[str, list[Bar]], *, day: str, enabled: set[str], regime: list[str], flatten: bool,
             entries: bool = True, session_minutes: int = 390) -> dict[str, int]:
        if self.state.get("day") != day:
            # ny dag: gamle åbne positioner (fx hvis appen var nede ved lukketid) skrives af til seneste kendte kurs
            for acct in self.state["accounts"].values():
                for pos in list(acct["open"]):
                    self._close(acct, pos, pos.get("last", pos["entry"]), "stale", pos["ts"], str(self.state.get("day") or day))
            self.state["day"], self.state["done"] = day, {}
        opened = closed = 0
        for symbol, bars in intraday.items():
            if len(bars) < MIN_BARS:
                continue
            d = DayData(bars, daily.get(symbol, []), session_minutes)
            last = len(bars) - 1
            # 1) følg åbne positioner
            for acct in self.state["accounts"].values():
                for pos in [p for p in acct["open"] if p["symbol"] == symbol]:
                    done = False
                    for k in range(last + 1):
                        stamp = bars[k].ts.isoformat(timespec="minutes")
                        if stamp <= pos["seen"]:
                            continue
                        pos["seen"] = stamp
                        hit = resolve_intrabar(pos["side"], pos["stop"], pos["target"], bars[k])
                        if hit is not None:
                            self._close(acct, pos, hit[1], hit[0], stamp, day)
                            closed += 1
                            done = True
                            break
                    if not done:
                        pos["last"] = bars[last].close
                        if flatten:
                            self._close(acct, pos, bars[last].close, "flatten", bars[last].ts.isoformat(timespec="minutes"), day)
                            closed += 1
            # 2) nye signaler på den seneste afsluttede bar
            if flatten or not entries:
                continue
            for sig in scan_day(symbol, d, last, enabled):
                key = f"{sig.strategy}:{symbol}"
                acct = self.account(sig.strategy)
                if key in self.state["done"] or len(acct["open"]) >= self.max_open or any(p["symbol"] == symbol for p in acct["open"]):
                    continue
                slip = self.costs.slippage_bps / 10_000.0
                entry = sig.entry * (1 + slip) if sig.side == "long" else sig.entry * (1 - slip)
                if sig.side == "long" and not (sig.stop < entry < sig.target) or sig.side == "short" and not (sig.target < entry < sig.stop):
                    continue
                risk = abs(entry - sig.stop)
                qty = int(min(acct["equity"] * self.risk_pct / 100.0 / risk, acct["equity"] * self.cap_pct / 100.0 / entry))
                if qty < 1:
                    continue
                stamp = bars[last].ts.isoformat(timespec="minutes")
                acct["open"].append({"strategy": sig.strategy, "symbol": symbol, "side": sig.side, "entry": round(entry, 4), "stop": sig.stop, "target": sig.target,
                                     "qty": qty, "ts": stamp, "seen": stamp, "quality": sig.quality, "regime": list(regime), "last": bars[last].close})
                self.state["done"][key] = day
                opened += 1
        return {"opened": opened, "closed": closed}

    def leaderboard(self, backtests: dict | None = None) -> list[dict[str, Any]]:
        rows = []
        for sid, acct in self.state["accounts"].items():
            if sid not in LIBRARY:
                continue
            m = metrics(acct["trades"])
            by_day: dict[str, float] = {}
            for t in acct["trades"]:
                by_day[t["date"]] = by_day.get(t["date"], 0.0) + float(t.get("pnl") or 0.0)
            peak, dd = self.state["start"], 0.0
            for _ts, eq in acct["curve"]:
                peak = max(peak, eq)
                dd = max(dd, (peak - eq) / peak * 100.0 if peak else 0.0)
            bt = (backtests or {}).get(sid)
            rows.append({
                "id": sid, "name": LIBRARY[sid].name, "equity": acct["equity"], "return_pct": round((acct["equity"] / self.state["start"] - 1) * 100.0, 2),
                "pnl": round(acct["equity"] - self.state["start"], 2), "open": len(acct["open"]), "drawdown_pct": round(dd, 2),
                "consistency": (sum(1 for v in by_day.values() if v > 0) / len(by_day)) if by_day else None, "days": len(by_day),
                **m, "score": strategy_score(m, bt), "approval": approval(m, bt),
            })
        return sorted(rows, key=lambda r: (r["score"]["score"] is not None, r["score"]["score"] or 0, r["pnl"]), reverse=True)

    def best(self, period: str, today: str) -> dict[str, Any] | None:
        """Bedste strategi i dag/denne uge/denne måned ud fra virtuel P&L i perioden."""
        from datetime import date, timedelta

        now = date.fromisoformat(today)
        start = {"day": now, "week": now - timedelta(days=now.weekday()), "month": now.replace(day=1)}[period]
        best: tuple[float, str] | None = None
        for sid, acct in self.state["accounts"].items():
            total = sum(float(t.get("pnl") or 0.0) for t in acct["trades"] if date.fromisoformat(t["date"]) >= start)
            has = any(date.fromisoformat(t["date"]) >= start for t in acct["trades"])
            if has and (best is None or total > best[0]):
                best = (total, sid)
        return {"id": best[1], "name": LIBRARY[best[1]].name, "pnl": round(best[0], 2)} if best else None
