"""Command Center Projekt-API v1: skrivebeskyttet status, økonomi, kapitalkurve og handler for alle tre puljer.

Kontrakten står i Command Centers PROJECT_API.md. Kort fortalt:

- Kun GET, og kun med `Authorization: Bearer <CC_API_TOKEN>`. Uden token (eller under 32 tegn) svarer alle
  ruter 404, så API'et er slået helt fra. Login, CSRF og TOTP gælder ikke her, og tokenet giver ikke adgang
  til andet end disse ruter.
- Tallene kommer fra appens egne databaser og den tilstand, motorerne allerede har i hukommelsen. Ingen kald til
  Alpaca, DexScreener, Jupiter eller Yahoo, og ingen ændringer af noget: ruterne kan ikke starte, stoppe eller
  købe noget.
- Bøger: `meme` (memebotten, USD), `desk-us` (Alpacas papirkonto, USD) og `desk-dk` (bordets egen papirkonto i
  kroner, kun når Københavns børs er slået til).
"""

from __future__ import annotations

import base64
import binascii
import hmac
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

import trader

API_VERSION = 1
MIN_TOKEN_LENGTH = 32
MAX_LIMIT = 1000
DEFAULT_LIMIT = 500
ERROR_WINDOW_HOURS = 24
STALE_TICK_SECONDS = 300
PROJECT = {"key": "ai-daytrading", "name": "AI Daytrading", "kind": "trading"}

# ---------------------------------------------------------------------- adgang


def configured_token(request: Request) -> str:
    """Tokenet fra CC_API_TOKEN. Tom streng, når API'et er slået fra (mangler eller er for kort)."""
    settings = request.app.state.settings
    token = str(getattr(settings, "cc_api_token", "") or "")
    return token if len(token) >= MIN_TOKEN_LENGTH else ""


def require_token(request: Request) -> None:
    expected = configured_token(request)
    if not expected:
        raise HTTPException(status_code=404, detail="Not Found")
    header = request.headers.get("authorization", "")
    scheme, _, sent = header.partition(" ")
    sent = sent.strip()
    ok = scheme.lower() == "bearer" and bool(sent) and hmac.compare_digest(sent.encode("utf-8"), expected.encode("utf-8"))
    if not ok:
        raise HTTPException(status_code=401, detail="Manglende eller forkert token.", headers={"WWW-Authenticate": "Bearer"})


def gate(request: Request) -> JSONResponse | None:
    """Kører i login-middlewaren for alle /cc/-stier: slået fra = 404, og kun GET er tilladt.
    Selve tokenet tjekkes af ruterne (require_token)."""
    if not configured_token(request):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    if request.method != "GET":
        return JSONResponse({"detail": "Kun GET."}, status_code=405, headers={"Allow": "GET"})
    return None


# ---------------------------------------------------------------------- små hjælpere


def _parse_time(value: Any) -> datetime | None:
    """ISO-tekst, epoch-sekunder eller datetime → datetime i UTC. Uden tidszone regnes med UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, (int, float)):
        moment = datetime.fromtimestamp(float(value), tz=timezone.utc)
    else:
        text = str(value).strip()
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _z(value: Any) -> str | None:
    moment = _parse_time(value)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


def _num(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number or number in (float("inf"), float("-inf")):
        return 0.0
    return round(number, 8)


def _opt(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return round(number, 8)


_URL = re.compile(r"(?i)\b(?:https?|wss?)://\S+")
_SECRET_PAIR = re.compile(r"(?i)\b(api[-_]?key|apikey|token|secret|key|password|signature)\s*[=:]\s*\S+")
_BASE58 = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,90}\b")
_HEX = re.compile(r"\b(?:0x)?[0-9a-fA-F]{32,}\b")
_ALPACA = re.compile(r"\b(?:PK|AK|CK)[A-Z0-9]{14,}\b")


def sanitise(text: Any, limit: int = 240) -> str:
    """Fjern adresser, nøgler, wallet-adresser og signaturer fra en besked, før den sendes ud."""
    clean = str(text or "")
    clean = _URL.sub("[adresse skjult]", clean)
    clean = _SECRET_PAIR.sub(lambda match: f"{match.group(1)}=[skjult]", clean)
    clean = _ALPACA.sub("[skjult]", clean)
    clean = _HEX.sub("[skjult]", clean)
    clean = _BASE58.sub("[skjult]", clean)
    clean = " ".join(clean.split())
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def _alive(engine) -> bool:
    thread = getattr(engine, "_thread", None)
    return bool(thread is not None and thread.is_alive())


def _version() -> str:
    sha = (os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("GIT_COMMIT") or "").strip()
    return sha[:12] if sha else trader.__version__


def _json(content: Any) -> JSONResponse:
    return JSONResponse(content, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------- bøgerne


class Book:
    """Fælles form for en kapitalpulje. Underklasserne læser kun fra databasen og motorens hukommelse."""

    key = ""
    name = ""
    currency = ""
    market = ""

    def __init__(self, engine) -> None:
        self.engine = engine

    @property
    def mode(self) -> str:
        return "paper"

    def info(self) -> dict:
        return {"key": self.key, "name": self.name, "currency": self.currency, "mode": self.mode}

    # Udfyldes af underklasserne.
    def performance(self, now: datetime) -> dict:  # pragma: no cover - abstrakt
        raise NotImplementedError

    def trades(self, now: datetime) -> list[dict]:  # pragma: no cover - abstrakt
        raise NotImplementedError

    def equity_points(self, now: datetime) -> tuple[list[dict], bool]:  # pragma: no cover - abstrakt
        raise NotImplementedError

    def health(self, now: datetime) -> dict:  # pragma: no cover - abstrakt
        raise NotImplementedError

    def problems(self, now: datetime) -> list[dict]:  # pragma: no cover - abstrakt
        raise NotImplementedError


def _trade(book: Book, **fields: Any) -> dict:
    return {"type": "trade", "book": book.key, "market": book.market, "side": "long", **fields}


def _reconstruct(start: float, closed: list[tuple[str, float]]) -> list[dict]:
    """Startbeløb plus kumulativ realiseret P&L efter hver lukket handel (når intet er gemt)."""
    rows = sorted((item for item in closed if _parse_time(item[0])), key=lambda item: _parse_time(item[0]))
    if not rows:
        return []
    total = 0.0
    points = [{"t": _z(rows[0][0]), "equity": _num(start), "cash": None}]
    for when, pnl in rows:
        total += float(pnl)
        points.append({"t": _z(when), "equity": _num(start + total), "cash": None})
    return points


class MemeBook(Book):
    key = "meme"
    name = "Memebot (Solana)"
    currency = "USD"
    market = "SOL"

    @property
    def mode(self) -> str:
        return "live" if getattr(self.engine, "mode", "paper") == "live" else "paper"

    def _open_values(self, pos: dict) -> tuple[float, float, float]:
        """(markedsværdi, investeret andel, urealiseret) for én åben position. Samme regnestykke som siden."""
        token_raw = max(1, int(pos["token_raw"]))
        fraction = int(pos["remaining_raw"]) / token_raw
        mark = float(pos["cost_usd"]) * fraction * float(pos["last_ratio"] or 1.0)
        unrealized = float(pos["proceeds_usd"]) + mark - float(pos["cost_usd"]) - float(pos["fees_usd"])
        return mark, float(pos["cost_usd"]) * fraction, unrealized

    def performance(self, now: datetime) -> dict:
        engine = self.engine
        mode = engine.mode
        totals = engine._totals(mode)  # samme tal som /meme (egenkapital, realiseret, urealiseret)
        open_rows = engine.store.open_positions(mode)
        exposure = sum(self._open_values(pos)[0] for pos in open_rows)
        failed = float(engine.store.get_state(f"failed_fees:{mode}", "0") or 0)
        fees = sum(float(row["fees_usd"] or 0) for row in engine.store.all_positions(mode)) + failed
        equity = float(totals["equity"])
        return {
            "key": self.key,
            "currency": self.currency,
            "mode": self.mode,
            "starting_capital": _num(totals["bankroll"]),
            "deposits": 0.0,
            "withdrawals": 0.0,
            "cash": _num(equity - exposure),
            "open_exposure": _num(exposure),
            "equity": _num(equity),
            "realized_pnl": _num(totals["realized"]),
            "unrealized_pnl": _num(totals["unrealized"]),
            "fees": _num(fees),
            "open_positions": len(open_rows),
            "as_of": _z(now),
            "source": "database",
        }

    def trades(self, now: datetime) -> list[dict]:
        engine = self.engine
        try:  # motortråden kan skrive samtidig; en mislykket kopi betyder bare, at åbningstiden bruges
            priced = dict(getattr(engine, "_priced_at", {}) or {})
        except RuntimeError:
            priced = {}
        items = []
        for pos in engine.store.all_positions(engine.mode):
            entry = float(pos["entry_price_usd"] or 0)
            cost = float(pos["cost_usd"] or 0)
            quantity = cost / entry if entry > 0 else 0.0
            is_open = pos["status"] == "open"
            fees = float(pos["fees_usd"] or 0)
            if is_open:
                mark, _invested, unrealized = self._open_values(pos)
                remaining = quantity * int(pos["remaining_raw"]) / max(1, int(pos["token_raw"]))
                updated = max(
                    (moment for moment in (_parse_time(pos["opened_at"]), _parse_time(priced.get(int(pos["id"])))) if moment),
                    default=now,
                )
                exit_price = None
            else:
                unrealized = 0.0
                remaining = 0.0
                updated = _parse_time(pos["closed_at"]) or _parse_time(pos["opened_at"]) or now
                proceeds = float(pos["proceeds_usd"] or 0)
                exit_price = proceeds / quantity if quantity > 0 else None
            items.append(
                _trade(
                    self,
                    id=f"meme-{pos['id']}",
                    status="open" if is_open else "closed",
                    opened_at=_z(pos["opened_at"]),
                    closed_at=None if is_open else _z(pos["closed_at"]),
                    updated_at=_z(updated),
                    symbol=str(pos["symbol"] or "?"),
                    quantity=_num(quantity),
                    remaining_quantity=_num(remaining),
                    entry_price=_opt(entry),
                    exit_price=_opt(exit_price),
                    mark_price=_opt(pos["last_price_usd"]) if is_open else None,
                    cost=_num(cost),
                    fees=_num(fees),
                    pnl=0.0 if is_open else _num(pos["pnl_usd"]),
                    unrealized_pnl=_num(unrealized),
                    strategy=str(pos["strategy"] or "momentum"),
                    exit_reason=None if is_open else (pos["exit_reason"] or None),
                )
            )
        return items

    def equity_points(self, now: datetime) -> tuple[list[dict], bool]:
        engine = self.engine
        rows = engine.store.equity_series(engine.mode, 20000)
        perf = self.performance(now)
        if rows:
            points = [{"t": _z(row["ts"]), "equity": _num(row["equity"]), "cash": None} for row in rows]
            reconstructed = False
        else:
            closed = [(row["closed_at"], float(row["pnl_usd"])) for row in engine.store.closed_positions(engine.mode)]
            points = _reconstruct(perf["starting_capital"], closed)
            reconstructed = True
        points.append({"t": perf["as_of"], "equity": perf["equity"], "cash": perf["cash"]})
        return points, reconstructed

    def health(self, now: datetime) -> dict:
        engine = self.engine
        last_tick = _parse_time(getattr(engine, "last_tick", None))
        errors = int(getattr(engine, "consecutive_errors", 0) or 0)
        mode = engine.mode
        today = now.date().isoformat()
        halted_dd = engine.store.get_state(f"halt_dd:{mode}", "0") == "1" and engine.loss_limits.get("drawdown")
        halted_day = engine.store.get_state(f"halt_day:{mode}:{today}", "0") == "1" and engine.loss_limits.get("daily")
        if not _alive(engine):
            state, text = "stopped", "Memebotten kører ikke."
        elif errors >= 5:
            state, text = "error", f"Memebotten har fejlet {errors} runder i træk."
        elif engine.kill_switch:
            state, text = "stopped", "Memebotten er stoppet med nødstop."
        elif engine.paused:
            state, text = "degraded", "Memebotten er på pause (ingen nye køb)."
        elif halted_dd or halted_day:
            state, text = "degraded", "Memebotten har ramt en tabsgrænse (ingen nye køb)."
        elif errors or engine.last_error:
            state, text = "degraded", "Memebotten havde en fejl i seneste runde."
        elif last_tick is None or (now - last_tick).total_seconds() > STALE_TICK_SECONDS:
            state, text = "degraded", "Memebotten har ikke kørt en runde for nylig."
        else:
            state, text = "running", f"Memebotten kører på {'papir' if mode == 'paper' else 'rigtige penge'}."
        return {"status": state, "message": text, "last_tick_at": _z(last_tick)}

    def problems(self, now: datetime) -> list[dict]:
        engine = self.engine
        cut = now - timedelta(hours=ERROR_WINDOW_HOURS)
        out: list[dict] = []
        for row in engine.store.recent_events(400):
            level = str(row.get("level") or "")
            if level not in ("error", "warning"):
                continue
            when = _parse_time(row.get("ts"))
            if when is None or when < cut:
                continue
            out.append({"at": when, "severity": level, "message": "Memebot: " + sanitise(row.get("message"))})
        if engine.kill_switch:
            out.append({"at": now, "severity": "warning", "message": "Memebot: nødstop er aktivt. Ingen nye køb."})
        errors = int(getattr(engine, "consecutive_errors", 0) or 0)
        if errors >= 5 and engine.last_error:
            out.append({"at": now, "severity": "critical", "message": f"Memebot: {errors} fejl i træk: " + sanitise(engine.last_error)})
        return out


class DeskBook(Book):
    @property
    def key(self) -> str:  # type: ignore[override]
        return "desk-dk" if self.engine.market.key == "dk" else "desk-us"

    @property
    def name(self) -> str:  # type: ignore[override]
        return "Handelsbordet (København)" if self.engine.market.key == "dk" else "Handelsbordet (USA)"

    @property
    def currency(self) -> str:  # type: ignore[override]
        return self.engine.market.currency

    @property
    def market(self) -> str:  # type: ignore[override]
        return "DK" if self.engine.market.key == "dk" else "US"

    # Bordet har ingen live-tilstand: det taler kun med papirkonti.

    def _rows(self) -> list[dict]:
        """Forslag, der blev til handler: åbne (placed) og lukkede med resultat."""
        rows = self.engine.store.proposals_with_status("placed", "closed")
        return [row for row in rows if row["status"] == "placed" or row.get("pnl") is not None]

    def _quotes(self) -> tuple[dict[str, float], datetime | None]:
        quotes = getattr(self.engine, "_quotes", None) or {}
        prices = {}
        for item in quotes.get("items") or []:
            try:
                prices[str(item["symbol"])] = float(item["price"])
            except (KeyError, TypeError, ValueError):
                continue
        return prices, _parse_time(quotes.get("ts"))

    def _ledger(self) -> dict:
        return self.engine.store.get_json("paper_ledger") or {}

    def _dk_price(self, order: dict) -> float:
        """Samme kurs som papirkontoen selv bruger: seneste kurs i hukommelsen, ellers den gemte."""
        broker = getattr(self.engine, "_broker", None)
        last = getattr(broker, "_last", None) or {}
        return float(last.get(order.get("symbol")) or order.get("last") or order.get("fill_raw") or order.get("fill") or 0)

    def _cached_account(self):
        hit = (getattr(self.engine, "_cache", None) or {}).get("account")
        if not hit:
            return None, None
        stamp, account = hit
        try:
            return account, datetime.fromtimestamp(float(stamp), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            return account, None

    def _start(self) -> float | None:
        start = self.engine.store.get_json("desk_start") or {}
        value = start.get("equity") if isinstance(start, dict) else None
        return float(value) if value is not None else None

    def performance(self, now: datetime) -> dict:
        if self.engine.own_ledger:
            return self._dk_performance(now)
        return self._us_performance(now)

    def _dk_performance(self, now: datetime) -> dict:
        ledger = self._ledger()
        start = float(ledger.get("start") or self.engine.base.start_cash)
        cash = float(ledger["cash"]) if ledger.get("cash") is not None else start
        realized = unrealized = exposure = fees = 0.0
        open_count = 0
        for order in (ledger.get("orders") or {}).values():
            qty = int(order.get("qty") or 0)
            fill = float(order.get("fill") or 0)
            fill_raw = float(order.get("fill_raw") or fill)
            fees += (fill - fill_raw) * qty
            if order.get("status") == "open":
                price = self._dk_price(order)
                exposure += price * qty
                unrealized += (price - fill) * qty
                open_count += 1
            else:
                exit_price = float(order.get("exit") or 0)
                exit_raw = float(order.get("exit_raw") or exit_price)
                fees += (exit_raw - exit_price) * qty
                realized += (exit_price - fill) * qty
        return {
            "key": self.key,
            "currency": self.currency,
            "mode": self.mode,
            "starting_capital": _num(start),
            "deposits": 0.0,
            "withdrawals": 0.0,
            "cash": _num(cash),
            "open_exposure": _num(exposure),
            "equity": _num(cash + exposure),
            "realized_pnl": _num(realized),
            "unrealized_pnl": _num(unrealized),
            "fees": _num(fees),
            "open_positions": open_count,
            "as_of": _z(now),
            "source": "database",
        }

    def _us_performance(self, now: datetime) -> dict:
        rows = self._rows()
        prices, quotes_at = self._quotes()
        realized = sum(float(row["pnl"]) for row in rows if row["status"] == "closed")
        unrealized = invested = marked = 0.0
        open_rows = [row for row in rows if row["status"] == "placed"]
        for row in open_rows:
            qty = int(row.get("qty") or 0)
            fill = float(row.get("fill_price") or row.get("entry") or 0)
            price = prices.get(row["symbol"])
            invested += fill * qty
            marked += (price if price is not None else fill) * qty
            if price is not None:
                unrealized += (price - fill) * qty
        account, account_at = self._cached_account()
        if account is not None:
            # Det, som siden viser: Alpacas papirkonto, sådan som baggrundstråden sidst hentede den.
            equity, cash = float(account.equity), float(account.cash)
            exposure = equity - cash
            as_of, source = account_at or now, "alpaca-cache"
        else:
            start = self._start() or 0.0
            cash = start + realized - invested
            exposure = marked
            equity = cash + exposure
            as_of, source = quotes_at or now, "beregnet"
        start = self._start()
        if start is None:
            start = equity - realized - unrealized
        return {
            "key": self.key,
            "currency": self.currency,
            "mode": self.mode,
            "starting_capital": _num(start),
            "deposits": 0.0,
            "withdrawals": 0.0,
            "cash": _num(cash),
            "open_exposure": _num(exposure),
            "equity": _num(equity),
            "realized_pnl": _num(realized),
            "unrealized_pnl": _num(unrealized),
            "fees": 0.0,  # Alpaca tager ingen kurtage på aktier på papirkontoen
            "open_positions": len(open_rows),
            "as_of": _z(as_of),
            "source": source,
        }

    def trades(self, now: datetime) -> list[dict]:
        prices, quotes_at = self._quotes()
        orders = (self._ledger().get("orders") or {}) if self.engine.own_ledger else {}
        items = []
        for row in self._rows():
            qty = int(row.get("qty") or 0)
            is_open = row["status"] == "placed"
            order = orders.get(str(row.get("order_id") or "")) if orders else None
            opened = _parse_time(row.get("decided_at")) or _parse_time(row.get("created_at")) or now
            fill = float(row.get("fill_price") or row.get("entry") or 0)
            if order is not None:
                # København: kurser uden kurtage, kurtagen for sig og resultatet efter kurtage.
                entry = float(order.get("fill_raw") or order.get("fill") or fill)
                fees = (float(order.get("fill") or entry) - entry) * qty
                exit_price = None
                if not is_open and order.get("exit") is not None:
                    exit_price = float(order.get("exit_raw") or order["exit"])
                    fees += (exit_price - float(order["exit"])) * qty
                elif not is_open:
                    exit_price = _opt(row.get("exit_price"))
                mark = self._dk_price(order) if is_open else None
                mark_at = None
            else:
                entry, fees = fill, 0.0
                exit_price = None if is_open else _opt(row.get("exit_price"))
                mark = prices.get(row["symbol"]) if is_open else None
                mark_at = quotes_at if mark is not None else None
            unrealized = (mark - fill) * qty if is_open and mark is not None else 0.0
            if is_open:
                updated = max((moment for moment in (opened, mark_at) if moment), default=opened)
            else:
                updated = _parse_time(row.get("closed_at")) or opened
            trial = bool((row.get("skeptic") or {}).get("trial")) if isinstance(row.get("skeptic"), dict) else False
            items.append(
                _trade(
                    self,
                    id=f"{self.key}-{row['id']}",
                    status="open" if is_open else "closed",
                    opened_at=_z(opened),
                    closed_at=None if is_open else _z(row.get("closed_at")),
                    updated_at=_z(updated),
                    symbol=str(row["symbol"]),
                    quantity=_num(qty),
                    entry_price=_opt(entry),
                    exit_price=_opt(exit_price),
                    mark_price=_opt(mark),
                    fees=_num(fees),
                    pnl=0.0 if is_open else _num(row.get("pnl")),
                    unrealized_pnl=_num(unrealized),
                    strategy="trial" if trial else "standard",
                    exit_reason=None if is_open else (row.get("exit_reason") or None),
                )
            )
        return items

    def equity_points(self, now: datetime) -> tuple[list[dict], bool]:
        perf = self.performance(now)
        stored = self.engine.store.states_with_prefix("day_start:")
        points = []
        for key, value in stored.items():
            day = key.split(":", 1)[1]
            try:
                market = self.engine.market
                moment = market.at(date.fromisoformat(day), market.open)  # kontoen aflæses ved åbningen
                equity = float(value)
            except (ValueError, TypeError, IndexError, AttributeError):
                continue
            points.append({"t": _z(moment), "equity": _num(equity), "cash": None})
        points.sort(key=lambda point: point["t"])
        reconstructed = False
        if not points:
            closed = [(row.get("closed_at"), float(row["pnl"])) for row in self._rows() if row["status"] == "closed"]
            points = _reconstruct(perf["starting_capital"], closed)
            reconstructed = True
        points.append({"t": perf["as_of"], "equity": perf["equity"], "cash": perf["cash"]})
        return points, reconstructed

    def health(self, now: datetime) -> dict:
        engine = self.engine
        last_tick = _parse_time(getattr(engine, "last_tick", None))
        if not _alive(engine):
            state, text = "stopped", f"{self.name} kører ikke."
        elif not engine.keys_present:
            state, text = "error", f"{self.name} mangler Alpaca-papirnøgler."
        elif engine.paused:
            state, text = "degraded", f"{self.name} er på pause."
        elif engine.last_error:
            state, text = "degraded", f"{self.name} har en fejl ved forbindelsen til {engine.source}."
        elif last_tick is None or (now - last_tick).total_seconds() > STALE_TICK_SECONDS:
            state, text = "degraded", f"{self.name} har ikke kørt en runde for nylig."
        else:
            state, text = "running", f"{self.name} kører på papir."
        return {"status": state, "message": text, "last_tick_at": _z(last_tick)}

    def problems(self, now: datetime) -> list[dict]:
        engine = self.engine
        cut = now - timedelta(hours=ERROR_WINDOW_HOURS)
        prefix = "Handelsbordet (København): " if self.key == "desk-dk" else "Handelsbordet (USA): "
        out: list[dict] = []
        for row in engine.store.messages(limit=300):
            if row.get("kind") != "error":
                continue
            when = _parse_time(row.get("ts"))
            if when is None or when < cut:
                continue
            out.append({"at": when, "severity": "error", "message": prefix + sanitise(row.get("text"))})
        if engine.paused:
            out.append({"at": now, "severity": "warning", "message": prefix + "bordet er på pause."})
        if engine.last_error:
            out.append({"at": now, "severity": "error", "message": prefix + sanitise(engine.last_error)})
        return out


def books_of(app) -> list[Book]:
    books: list[Book] = []
    meme = getattr(app.state, "meme", None)
    if meme is not None:
        books.append(MemeBook(meme))
    for name in ("desk", "desk_dk"):
        engine = getattr(app.state, name, None)
        if engine is not None:
            books.append(DeskBook(engine))
    return books


def _pick(app, key: str | None) -> list[Book]:
    books = books_of(app)
    if not key:
        return books
    chosen = [book for book in books if book.key == key]
    if not chosen:
        raise HTTPException(status_code=404, detail="Ukendt bog.")
    return chosen


def _query_time(value: str | None, name: str) -> datetime | None:
    if value is None or value == "":
        return None
    moment = _parse_time(value)
    if moment is None:
        raise HTTPException(status_code=400, detail=f"Ugyldig tid i {name}. Brug ISO 8601, fx 2026-11-18T07:30:00Z.")
    return moment


def _encode_cursor(updated_at: str, item_id: str) -> str:
    return base64.urlsafe_b64encode(f"{updated_at}|{item_id}".encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("utf-8")
        updated_at, item_id = raw.split("|", 1)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Ugyldig cursor.") from exc
    if _parse_time(updated_at) is None or not item_id:
        raise HTTPException(status_code=400, detail="Ugyldig cursor.")
    return updated_at, item_id


def _bucket(points: list[dict], interval: str) -> list[dict]:
    """Behold det sidste punkt i hver time eller dag (UTC)."""
    width = 13 if interval == "hour" else 10  # "2026-11-18T07" eller "2026-11-18"
    kept: dict[str, dict] = {}
    for point in points:
        kept[point["t"][:width]] = point
    return list(kept.values())


# ---------------------------------------------------------------------- ruterne


def build_router() -> APIRouter:
    router = APIRouter(prefix="/cc/v1", dependencies=[Depends(require_token)])

    @router.get("/status")
    def cc_status(request: Request) -> JSONResponse:
        now = datetime.now(timezone.utc)
        books = books_of(request.app)
        health = {book.key: book.health(now) for book in books}
        states = [item["status"] for item in health.values()]
        if not states or all(state == "stopped" for state in states):
            status = "stopped"
        elif "error" in states:
            status = "error"
        elif any(state != "running" for state in states):
            status = "degraded"
        else:
            status = "running"
        modes = {book.mode for book in books}
        mode = modes.pop() if len(modes) == 1 else ("mixed" if modes else "paper")
        problems: list[dict] = []
        for book in books:
            problems.extend(book.problems(now))
        grouped: dict[tuple[str, str], dict] = {}
        for item in problems:
            slot = grouped.get((item["severity"], item["message"]))
            if slot is None:
                grouped[(item["severity"], item["message"])] = {**item, "count": 1}
            else:
                slot["count"] += 1
                slot["at"] = max(slot["at"], item["at"])
        errors = sorted(grouped.values(), key=lambda item: item["at"], reverse=True)[:50]
        activity: list[datetime] = []
        for book in books:
            tick = _parse_time(health[book.key]["last_tick_at"])
            if tick:
                activity.append(tick)
        started = getattr(request.app.state, "cc_started_at", None)
        message = " ".join(item["message"] for item in health.values()) or "Ingen motorer er slået til."
        return _json(
            {
                "api_version": API_VERSION,
                "project": {**PROJECT, "version": _version()},
                "status": status,
                "mode": mode,
                "started_at": _z(started),
                "last_activity_at": _z(max(activity)) if activity else None,
                "message": message,
                "errors": [
                    {"at": _z(item["at"]), "severity": item["severity"], "message": item["message"], "count": item["count"]}
                    for item in errors
                ],
                "books": [{**book.info(), **health[book.key]} for book in books],
            }
        )

    @router.get("/performance")
    def cc_performance(request: Request) -> JSONResponse:
        now = datetime.now(timezone.utc)
        return _json({"as_of": _z(now), "books": [book.performance(now) for book in books_of(request.app)]})

    @router.get("/equity")
    def cc_equity(request: Request, book: str | None = None, interval: str = "hour") -> JSONResponse:
        params = request.query_params
        start = _query_time(params.get("from"), "from")
        end = _query_time(params.get("to"), "to")
        if interval not in ("hour", "day"):
            raise HTTPException(status_code=400, detail="interval skal være hour eller day.")
        books = books_of(request.app)
        if not book:
            if len(books) != 1:
                raise HTTPException(status_code=400, detail="Angiv book, fx ?book=meme.")
            chosen = books[0]
        else:
            chosen = _pick(request.app, book)[0]
        now = datetime.now(timezone.utc)
        points, reconstructed = chosen.equity_points(now)
        points = [point for point in points if point["t"]]
        points.sort(key=lambda point: point["t"])
        if start is not None:
            points = [point for point in points if _parse_time(point["t"]) >= start]
        if end is not None:
            points = [point for point in points if _parse_time(point["t"]) <= end]
        return _json(
            {
                "book": chosen.key,
                "currency": chosen.currency,
                "interval": interval,
                "reconstructed": reconstructed,
                "points": _bucket(points, interval),
            }
        )

    @router.get("/ledger")
    def cc_ledger(request: Request, book: str | None = None, cursor: str | None = None, limit: int = DEFAULT_LIMIT) -> JSONResponse:
        since = _query_time(request.query_params.get("since"), "since")
        limit = max(1, min(MAX_LIMIT, int(limit)))
        after = _decode_cursor(cursor) if cursor else None
        now = datetime.now(timezone.utc)
        items: list[dict] = []
        for chosen in _pick(request.app, book):
            items.extend(chosen.trades(now))
        if since is not None:
            items = [item for item in items if _parse_time(item["updated_at"]) >= since]
        items.sort(key=lambda item: (item["updated_at"], item["id"]))
        if after is not None:
            mark = (_z(after[0]), after[1])
            items = [item for item in items if (item["updated_at"], item["id"]) > mark]
        page = items[:limit]
        next_cursor = _encode_cursor(page[-1]["updated_at"], page[-1]["id"]) if len(items) > limit else None
        return _json({"items": page, "next_cursor": next_cursor})

    @router.get("/metrics")
    def cc_metrics(request: Request) -> JSONResponse:
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        metrics: list[dict] = []
        for book in books_of(request.app):
            trades = book.trades(now)
            closed = [item for item in trades if item["status"] == "closed"]
            today_closed = [item for item in closed if (item["closed_at"] or "")[:10] == today]
            wins = [item for item in closed if item["pnl"] > 0]
            metrics.extend(
                [
                    {"key": f"{book.key}.trades_today", "label": f"{book.name}: handler lukket i dag", "value": len(today_closed), "unit": "stk"},
                    {
                        "key": f"{book.key}.pnl_today",
                        "label": f"{book.name}: realiseret i dag",
                        "value": _num(sum(item["pnl"] for item in today_closed)),
                        "unit": book.currency,
                    },
                    {
                        "key": f"{book.key}.win_rate",
                        "label": f"{book.name}: andel vundne handler",
                        "value": _num(len(wins) / len(closed)) if closed else None,
                        "unit": "andel",
                    },
                ]
            )
        return _json({"as_of": _z(now), "metrics": metrics})

    return router


def mark_started(app) -> None:
    app.state.cc_started_at = time.time()
