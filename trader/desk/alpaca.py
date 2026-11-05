"""Handelsbordets egen forbindelse til Alpaca. Kun papirkontoen: adressen til rigtige penge findes ikke her.

Indeholder også de små kursværktøjer, bordet regner med (barer, VWAP, bracket og første berøring).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

ET_NAME = "America/New_York"
ET = ZoneInfo(ET_NAME)

PAPER_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"


class BrokerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Clock:
    is_open: bool
    timestamp: datetime
    next_open: datetime | None
    next_close: datetime | None


@dataclass
class Account:
    equity: float
    cash: float
    buying_power: float
    daytrade_count: int


@dataclass
class Position:
    symbol: str
    qty: int
    side: str
    avg_entry: float
    market_price: float
    unrealized_pl: float


@dataclass
class NewsItem:
    headline: str
    symbols: list[str]
    created_at: datetime
    summary: str
    url: str = ""


@dataclass
class Order:
    id: str
    symbol: str
    side: str
    qty: int
    status: str
    filled_avg_price: float | None
    stop_price: float
    target_price: float
    exit_price: float | None
    exit_reason: str | None


def vwap(bars: list[Bar]) -> float | None:
    total_pv = 0.0
    total_v = 0.0
    for bar in bars:
        typical = (bar.high + bar.low + bar.close) / 3.0
        total_pv += typical * bar.volume
        total_v += bar.volume
    if total_v <= 0:
        return None
    return total_pv / total_v


def _round_price(value: float) -> float:
    return round(value + 1e-9, 2)


def _bracket(side: str, entry: float, stop: float, multiple: float) -> tuple[float, float, float] | None:
    entry_r = _round_price(entry)
    stop_r = _round_price(stop)
    if side == "long":
        if stop_r >= entry_r:
            return None
        risk = entry_r - stop_r
        target = _round_price(entry_r + multiple * risk)
        guard = 0
        while target <= entry_r or (target - entry_r) / risk < multiple - 1e-6:
            target = _round_price(target + 0.01)
            guard += 1
            if guard > 5:
                break
        if target <= entry_r:
            return None
        return entry_r, stop_r, target
    if side == "short":
        if stop_r <= entry_r:
            return None
        risk = stop_r - entry_r
        target = _round_price(entry_r - multiple * risk)
        guard = 0
        while target >= entry_r or (entry_r - target) / risk < multiple - 1e-6:
            target = _round_price(target - 0.01)
            guard += 1
            if guard > 5:
                break
        if target >= entry_r:
            return None
        return entry_r, stop_r, target
    return None


def resolve_intrabar(side: str, stop: float, target: float, bar: Bar) -> tuple[str, float] | None:
    """Stop vinder, hvis både stop og mål rammes i samme bar.

    Et hul gennem stoppet fyldes til den dårligere åbningskurs.
    Et hul gennem målet fyldes til åbningen, som er bedre for en limitordre.
    """
    if side == "long":
        stop_hit = bar.low <= stop
        target_hit = bar.high >= target
        if stop_hit:
            fill = bar.open if bar.open <= stop else stop
            return "stop", fill
        if target_hit:
            fill = bar.open if bar.open >= target else target
            return "target", fill
        return None
    if side == "short":
        stop_hit = bar.high >= stop
        target_hit = bar.low <= target
        if stop_hit:
            fill = bar.open if bar.open >= stop else stop
            return "stop", fill
        if target_hit:
            fill = bar.open if bar.open <= target else target
            return "target", fill
        return None
    return None


def _parse_ts(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(ET)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def service_name(url: str) -> str:
    if url.startswith(DATA_URL):
        return "nyheder" if "/news" in url else "kursdata (data.alpaca.markets)"
    return "handelskontoen (paper-api.alpaca.markets)"


def short_body(text: str) -> str:
    """Alpacas svar uden HTML, kort nok til en besked."""
    plain = re.sub(r"<[^>]+>", " ", text or "")
    plain = " ".join(plain.split())
    return plain[:120] or "tomt svar"


class AlpacaPaper:
    """Papirkontoen hos Alpaca. Der er ingen live-parameter, og adressen er altid paper-api."""

    def __init__(self, api_key: str, secret_key: str) -> None:
        if not (api_key and secret_key):
            raise BrokerError("Mangler Alpaca-papirnøgler (DESK_ALPACA_API_KEY og DESK_ALPACA_SECRET_KEY).")
        self.base = PAPER_URL
        self._client = httpx.Client(
            timeout=20.0,
            headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key},
        )

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, url: str, **kwargs) -> dict | list:
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise BrokerError(f"Alpaca kunne ikke kontaktes: {exc}") from exc
        if response.status_code == 404 and method == "DELETE":
            return {}
        if response.status_code in (401, 403):
            raise BrokerError(
                f"Alpaca afviser nøglerne ved {service_name(url)} ({response.status_code}: {short_body(response.text)}). "
                "Brug papirnøgler (Key starter med PK) fra app.alpaca.markets, tjek at Key og Secret ikke er byttet om, "
                "og at de er fra samme gang du trykkede Generate."
            )
        if response.status_code >= 400:
            detail = response.text[:300]
            raise BrokerError(f"Alpaca svarede {response.status_code}: {detail}")
        if not response.content:
            return {}
        return response.json()

    def get_clock(self) -> Clock:
        payload = self._request("GET", f"{self.base}/v2/clock")
        assert isinstance(payload, dict)
        return Clock(
            is_open=bool(payload.get("is_open")),
            timestamp=_parse_ts(payload["timestamp"]),
            next_open=_parse_ts(payload["next_open"]) if payload.get("next_open") else None,
            next_close=_parse_ts(payload["next_close"]) if payload.get("next_close") else None,
        )

    def get_account(self) -> Account:
        payload = self._request("GET", f"{self.base}/v2/account")
        assert isinstance(payload, dict)
        return Account(
            equity=float(payload["equity"]),
            cash=float(payload["cash"]),
            buying_power=float(payload["buying_power"]),
            daytrade_count=int(payload.get("daytrade_count") or 0),
        )

    def get_positions(self) -> list[Position]:
        payload = self._request("GET", f"{self.base}/v2/positions")
        assert isinstance(payload, list)
        positions: list[Position] = []
        for row in payload:
            qty = abs(int(float(row["qty"])))
            side = "short" if row.get("side") == "short" or float(row["qty"]) < 0 else "long"
            positions.append(
                Position(
                    symbol=row["symbol"],
                    qty=qty,
                    side=side,
                    avg_entry=float(row["avg_entry_price"]),
                    market_price=float(row["current_price"]),
                    unrealized_pl=float(row.get("unrealized_pl") or 0),
                )
            )
        return positions

    def _bars(self, symbols: list[str], timeframe: str, start: datetime, end: datetime) -> dict[str, list[Bar]]:
        collected: dict[str, list[Bar]] = {symbol: [] for symbol in symbols}
        token: str | None = None
        while True:
            params: dict[str, str | int] = {
                "symbols": ",".join(symbols),
                "timeframe": timeframe,
                "start": _iso(start),
                "end": _iso(end),
                "feed": "iex",
                "adjustment": "raw",
                "limit": 10000,
                "sort": "asc",
            }
            if token:
                params["page_token"] = token
            payload = self._request("GET", f"{DATA_URL}/v2/stocks/bars", params=params)
            assert isinstance(payload, dict)
            grouped = payload.get("bars") or {}
            for symbol, rows in grouped.items():
                for row in rows:
                    moment = _parse_ts(row["t"])
                    collected.setdefault(symbol, []).append(
                        Bar(
                            ts=moment,
                            open=float(row["o"]),
                            high=float(row["h"]),
                            low=float(row["l"]),
                            close=float(row["c"]),
                            volume=float(row["v"]),
                        )
                    )
            token = payload.get("next_page_token")
            if not token:
                break
        return collected

    def get_intraday(self, symbols: list[str], now: datetime | None = None) -> dict[str, list[Bar]]:
        moment = now or datetime.now(ET)
        start = datetime.combine(moment.date(), time(9, 30), tzinfo=ET)
        bars = self._bars(symbols, "1Min", start, moment)
        cleaned: dict[str, list[Bar]] = {}
        for symbol, rows in bars.items():
            cleaned[symbol] = [bar for bar in rows if start <= bar.ts and bar.ts + timedelta(minutes=1) <= moment]
        return cleaned

    def get_daily(self, symbols: list[str], limit: int = 21, now: datetime | None = None) -> dict[str, list[Bar]]:
        moment = now or datetime.now(ET)
        start = moment - timedelta(days=50)
        end = datetime.combine(moment.date(), time(0, 0), tzinfo=ET)
        bars = self._bars(symbols, "1Day", start, end)
        trimmed: dict[str, list[Bar]] = {}
        for symbol, rows in bars.items():
            prior = [bar for bar in rows if bar.ts.date() < moment.date()]
            trimmed[symbol] = prior[-limit:]
        return trimmed

    def get_news(self, symbols: list[str], now: datetime | None = None) -> list[NewsItem]:
        moment = now or datetime.now(ET)
        params = {
            "symbols": ",".join(symbols),
            "start": _iso(moment - timedelta(hours=18)),
            "end": _iso(moment),
            "limit": 50,
            "sort": "desc",
        }
        payload = self._request("GET", f"{DATA_URL}/v1beta1/news", params=params)
        assert isinstance(payload, dict)
        items: list[NewsItem] = []
        for row in payload.get("news") or []:
            created = row.get("created_at") or row.get("updated_at")
            items.append(
                NewsItem(
                    headline=row.get("headline") or "",
                    symbols=list(row.get("symbols") or []),
                    created_at=_parse_ts(created) if created else moment,
                    summary=row.get("summary") or "",
                    url=row.get("url") or "",
                )
            )
        return items

    def submit_bracket(self, symbol: str, side: str, qty: int, stop_price: float, target_price: float) -> Order:
        if qty < 1:
            raise BrokerError("Antal aktier skal være mindst 1.")
        payload = {
            "symbol": symbol,
            "qty": str(qty),
            "side": "buy" if side == "long" else "sell",
            "type": "market",
            "time_in_force": "day",
            "order_class": "bracket",
            "take_profit": {"limit_price": f"{target_price:.2f}"},
            "stop_loss": {"stop_price": f"{stop_price:.2f}"},
        }
        body = self._request("POST", f"{self.base}/v2/orders", json=payload)
        assert isinstance(body, dict)
        return self._order_from_payload(body, side=side, stop_price=stop_price, target_price=target_price, qty=qty)

    def close_position(self, symbol: str) -> None:
        """Lukker én position. Åbne ordrer på symbolet (bracket-ben) annulleres først, ellers er aktierne låst."""
        orders = self._request("GET", f"{self.base}/v2/orders", params={"status": "open", "symbols": symbol, "nested": "false"})
        for order in orders if isinstance(orders, list) else []:
            if order.get("id"):
                self._request("DELETE", f"{self.base}/v2/orders/{order['id']}")
        self._request("DELETE", f"{self.base}/v2/positions/{symbol}")

    def get_order(self, order_id: str) -> Order:
        body = self._request("GET", f"{self.base}/v2/orders/{order_id}", params={"nested": "true"})
        assert isinstance(body, dict)
        side = "long" if body.get("side") == "buy" else "short"
        return self._order_from_payload(body, side=side, stop_price=0.0, target_price=0.0, qty=int(float(body.get("qty") or 0)))

    def _order_from_payload(self, body: dict, *, side: str, stop_price: float, target_price: float, qty: int) -> Order:
        filled = body.get("filled_avg_price")
        exit_price = None
        exit_reason = None
        legs = body.get("legs") or []
        for leg in legs:
            leg_type = (leg.get("type") or leg.get("order_type") or "").lower()
            if leg.get("stop_price") and not stop_price:
                stop_price = float(leg["stop_price"])
            if leg.get("limit_price") and not target_price and "stop" not in leg_type:
                target_price = float(leg["limit_price"])
            if leg.get("status") == "filled" and leg.get("filled_avg_price"):
                price = float(leg["filled_avg_price"])
                reason = "stop" if "stop" in leg_type else "target"
                # Stop vinder, hvis begge ben mod forventning er fyldt.
                if exit_reason == "stop":
                    continue
                exit_price = price
                exit_reason = reason
        status = "closed" if exit_price is not None else "open"
        parent_status = body.get("status") or ""
        if parent_status in {"canceled", "cancelled", "expired", "rejected"} and exit_price is None:
            status = "canceled"
            exit_reason = parent_status
        return Order(
            id=str(body.get("id")),
            symbol=str(body.get("symbol")),
            side=side,
            qty=qty,
            status=status,
            filled_avg_price=float(filled) if filled else None,
            stop_price=stop_price,
            target_price=target_price,
            exit_price=exit_price,
            exit_reason=exit_reason,
        )
