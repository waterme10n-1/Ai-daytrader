"""Alpaca og SimBroker deler samme kald: ur, konto, barer, nyheder og bracketordrer."""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from trader.config import ET_NAME, MARKET_PROXY, Settings, UNIVERSE
from trader.signals import Bar

ET = ZoneInfo(ET_NAME)

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"

BASE_PRICES: dict[str, float] = {
    "SPY": 560.0,
    "QQQ": 480.0,
    "IWM": 220.0,
    "AAPL": 230.0,
    "MSFT": 420.0,
    "NVDA": 140.0,
    "AMZN": 190.0,
    "META": 580.0,
    "GOOGL": 170.0,
    "TSLA": 250.0,
    "AMD": 160.0,
}

DAY_TYPES: tuple[str, ...] = (
    "orb_long_win",
    "orb_short_win",
    "chop",
    "close_long_win",
    "orb_long_loss",
    "close_short_win",
    "chop",
    "orb_short_loss",
)

DAY_NOTES: dict[str, str] = {
    "orb_long_win": "Syntetisk dag: åbningsudbrud op, mål ramt",
    "orb_short_win": "Syntetisk dag: åbningsudbrud ned, mål ramt",
    "orb_long_loss": "Syntetisk dag: åbningsudbrud op, stop ramt",
    "orb_short_loss": "Syntetisk dag: åbningsudbrud ned, stop ramt",
    "close_long_win": "Syntetisk dag: lukkemomentum op, mål ramt",
    "close_short_win": "Syntetisk dag: lukkemomentum ned, mål ramt",
    "chop": "Syntetisk dag: ingen udbrud",
}


class BrokerError(RuntimeError):
    pass


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


def nth_weekday(start: date, index: int) -> date:
    """1-baseret hverdag nummer `index` fra og med start."""
    day = start
    while day.weekday() >= 5:
        day += timedelta(days=1)
    count = 1
    while count < index:
        day += timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
        count += 1
    return day


def day_type_for(seed: int) -> str:
    return DAY_TYPES[(seed - 1) % len(DAY_TYPES)]


def _price(open_price: float, multiplier: float) -> float:
    return round(open_price * round(multiplier, 6), 2)


def _orb_long_closes() -> list[float]:
    closes = [1.0] * 390
    opening = [
        1.0000, 0.9990, 0.9980, 0.9975, 0.9970,
        0.9985, 0.9995, 1.0010, 1.0025, 1.0030,
        1.0020, 1.0010, 1.0005, 1.0015, 1.0020,
    ]
    closes[:15] = opening
    closes[15:20] = [1.0045, 1.0050, 1.0052, 1.0054, 1.0055]
    for index in range(20, 81):
        closes[index] = 1.0055 + (1.020 - 1.0055) * (index - 20) / 60
    for index in range(81, 390):
        closes[index] = 1.020
    return closes


def _orb_long_loss_closes() -> list[float]:
    closes = _orb_long_closes()
    for index in range(20, 36):
        closes[index] = 1.0055 + 0.0015 * (index - 20) / 15
    for index in range(36, 51):
        closes[index] = 1.007 + (0.994 - 1.007) * (index - 36) / 14
    for index in range(51, 390):
        closes[index] = 0.994
    return closes


def _chop_closes() -> list[float]:
    return [1.0 + 0.0008 * math.sin(index / 8.0) for index in range(390)]


def _close_long_closes() -> list[float]:
    closes = [1.0] * 390
    for index in range(300, 330):
        closes[index] = 1.0 + 0.007 * (index - 299) / 30
    for index in range(330, 371):
        closes[index] = 1.007 + (1.028 - 1.007) * (index - 329) / 41
    for index in range(371, 390):
        closes[index] = 1.028
    return closes


def _closes_for(kind: str) -> list[float]:
    if kind == "orb_long_win":
        return _orb_long_closes()
    if kind == "orb_long_loss":
        return _orb_long_loss_closes()
    if kind == "chop":
        return _chop_closes()
    if kind == "close_long_win":
        return _close_long_closes()
    if kind == "orb_short_win":
        return [2 - value for value in _orb_long_closes()]
    if kind == "orb_short_loss":
        return [2 - value for value in _orb_long_loss_closes()]
    if kind == "close_short_win":
        return [2 - value for value in _close_long_closes()]
    raise ValueError(kind)


def _wicks_for(kind: str) -> list[tuple[float, float]]:
    wicks = [(0.0, 0.0)] * 390
    if kind in {"orb_long_win", "orb_long_loss"}:
        wicks[4] = (0.0, 0.0010)
        wicks[9] = (0.0010, 0.0)
    elif kind in {"orb_short_win", "orb_short_loss"}:
        wicks[4] = (0.0010, 0.0)
        wicks[9] = (0.0, 0.0010)
    else:
        wicks[5] = (0.0, 0.0020)
        wicks[10] = (0.0020, 0.0)
        if kind in {"close_short_win"}:
            wicks[5] = (0.0020, 0.0)
            wicks[10] = (0.0, 0.0020)
    return wicks


def _gap_and_trend(kind: str) -> tuple[float, float]:
    if "long" in kind:
        return 0.004, 0.04
    if "short" in kind:
        return -0.004, -0.04
    return 0.001, 0.005


def _prior_weekdays(day: date, count: int) -> list[date]:
    found: list[date] = []
    cursor = day - timedelta(days=1)
    while len(found) < count:
        if cursor.weekday() < 5:
            found.append(cursor)
        cursor -= timedelta(days=1)
    found.reverse()
    return found


def build_day_bars(day: date, kind: str, symbols: tuple[str, ...] | list[str] = UNIVERSE) -> tuple[
    dict[str, list[Bar]], dict[str, list[Bar]]
]:
    """Et helt dagsforløb af 1-minutters barer plus 21 forudgående dage."""
    closes = _closes_for(kind)
    wicks = _wicks_for(kind)
    session = datetime.combine(day, time(9, 30), tzinfo=ET)
    gap, trend = _gap_and_trend(kind)
    intraday: dict[str, list[Bar]] = {}
    daily: dict[str, list[Bar]] = {}
    history_days = _prior_weekdays(day, 21)

    for symbol in symbols:
        open_price = BASE_PRICES.get(symbol, 100.0)
        bars: list[Bar] = []
        previous = _price(open_price, closes[0])
        for index, close_mult in enumerate(closes):
            close_px = _price(open_price, close_mult)
            open_px = previous if index else _price(open_price, closes[0])
            up, down = wicks[index]
            high_px = _price(open_price, max(closes[index], closes[index - 1] if index else closes[0]) + up)
            low_px = _price(open_price, min(closes[index], closes[index - 1] if index else closes[0]) - down)
            if index == 0:
                open_px = _price(open_price, 1.0)
                high_px = max(_price(open_price, closes[0] + up), open_px, close_px)
                low_px = min(_price(open_price, closes[0] - down), open_px, close_px)
            high_px = max(high_px, open_px, close_px)
            low_px = min(low_px, open_px, close_px)
            bars.append(
                Bar(
                    ts=session + timedelta(minutes=index),
                    open=open_px,
                    high=high_px,
                    low=low_px,
                    close=close_px,
                    volume=10_000,
                )
            )
            previous = close_px
        intraday[symbol] = bars

        yesterday = open_price / (1 + gap)
        oldest = yesterday / (1 + trend)
        daily_bars: list[Bar] = []
        prior = oldest
        for index, hist_day in enumerate(history_days):
            if index == 20:
                close_px = round(yesterday, 2)
            elif index == 0:
                close_px = round(oldest, 2)
            else:
                close_px = round(oldest + (yesterday - oldest) * index / 20, 2)
            high_px = round(close_px * 1.006, 2)
            low_px = round(close_px * 0.994, 2)
            open_px = round(prior, 2)
            high_px = max(high_px, open_px, close_px)
            low_px = min(low_px, open_px, close_px)
            daily_bars.append(
                Bar(
                    ts=datetime.combine(hist_day, time(16, 0), tzinfo=ET),
                    open=open_px,
                    high=high_px,
                    low=low_px,
                    close=close_px,
                    volume=1_000_000,
                )
            )
            prior = close_px
        daily[symbol] = daily_bars
    return intraday, daily


def _parse_ts(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(ET)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class AlpacaBroker:
    def __init__(self, settings: Settings, *, live: bool) -> None:
        self.live = live
        self.settings = settings
        if live:
            if not settings.live_keys_present:
                raise BrokerError("Mangler live-nøgler (ALPACA_LIVE_API_KEY og ALPACA_LIVE_SECRET_KEY).")
            key, secret = settings.alpaca_live_api_key, settings.alpaca_live_secret_key
            self.base = LIVE_URL
        else:
            if not settings.paper_keys_present:
                raise BrokerError("Mangler Alpaca-nøgler til papirhandel (ALPACA_API_KEY og ALPACA_SECRET_KEY).")
            key, secret = settings.alpaca_api_key, settings.alpaca_secret_key
            self.base = PAPER_URL
        self._client = httpx.Client(
            timeout=20.0,
            headers={
                "APCA-API-KEY-ID": key,
                "APCA-API-SECRET-KEY": secret,
            },
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

    def close_all_positions(self) -> None:
        self._request("DELETE", f"{self.base}/v2/positions")

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


class _SimOrder:
    def __init__(self, order: Order, entry_ts: datetime) -> None:
        self.order = order
        self.entry_ts = entry_ts


class SimBroker:
    """Syntetisk børs med samme grænseflade som Alpaca.

    Fyld er pessimistiske: rammes stop og mål i samme bar, fyldes stoppet.
    """

    def __init__(self, day: date, seed: int, starting_equity: float, kind: str | None = None) -> None:
        self.day = day
        self.seed = seed
        self.kind = kind or day_type_for(seed)
        self.note = DAY_NOTES.get(self.kind, "Syntetisk dag")
        self.now = datetime.combine(day, time(9, 30), tzinfo=ET)
        self.intraday, self.daily = build_day_bars(day, self.kind)
        self.cash = float(starting_equity)
        self.daytrade_count = 0
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, _SimOrder] = {}
        self._ids = itertools.count(1)
        self._processed_until = self.now

    def set_time(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=ET)
        for symbol, bars in self.intraday.items():
            for bar in bars:
                if self._processed_until <= bar.ts < moment:
                    self._apply_bar(symbol, bar)
        self._processed_until = moment
        self.now = moment

    def get_clock(self) -> Clock:
        stamp = self.now.timetz().replace(tzinfo=None)
        is_open = time(9, 30) <= stamp < time(16, 0) and self.now.weekday() < 5
        close_at = datetime.combine(self.day, time(16, 0), tzinfo=ET)
        return Clock(is_open=is_open, timestamp=self.now, next_open=self.now, next_close=close_at)

    def get_account(self) -> Account:
        equity = self._equity()
        gross = sum(pos.qty * pos.market_price for pos in self._positions.values())
        return Account(
            equity=equity,
            cash=self.cash,
            buying_power=max(0.0, equity * 2 - gross),
            daytrade_count=self.daytrade_count,
        )

    def get_positions(self) -> list[Position]:
        self._mark()
        return list(self._positions.values())

    def get_intraday(self, symbols: list[str], now: datetime | None = None) -> dict[str, list[Bar]]:
        moment = now or self.now
        return {
            symbol: [bar for bar in self.intraday.get(symbol, []) if bar.ts + timedelta(minutes=1) <= moment]
            for symbol in symbols
        }

    def get_daily(self, symbols: list[str], limit: int = 21, now: datetime | None = None) -> dict[str, list[Bar]]:
        return {symbol: self.daily.get(symbol, [])[-limit:] for symbol in symbols}

    def get_news(self, symbols: list[str], now: datetime | None = None) -> list[NewsItem]:
        moment = now or self.now
        items: list[NewsItem] = []
        for symbol in symbols:
            if symbol == MARKET_PROXY:
                headline = "Syntetisk testdata: ingen rigtig makrobegivenhed er indlæst for SPY."
            else:
                headline = f"Syntetisk testdata: ingen rigtig nyhed for {symbol}."
            items.append(
                NewsItem(
                    headline=headline,
                    symbols=[symbol],
                    created_at=moment - timedelta(hours=2),
                    summary="Genereret af simulatoren. Må ikke bruges som markedsinformation.",
                )
            )
        return items

    def submit_bracket(self, symbol: str, side: str, qty: int, stop_price: float, target_price: float) -> Order:
        if symbol in self._positions:
            raise BrokerError(f"SimBroker har allerede en position i {symbol}.")
        if qty < 1:
            raise BrokerError("Antal aktier skal være mindst 1.")
        fill = self._open_price(symbol, self.now)
        if fill is None:
            raise BrokerError(f"Ingen kurs for {symbol}.")
        if side == "long" and not (stop_price < fill < target_price):
            raise BrokerError("Bracket er på den forkerte side af fyldet.")
        if side == "short" and not (target_price < fill < stop_price):
            raise BrokerError("Bracket er på den forkerte side af fyldet.")
        order_id = f"sim-{next(self._ids)}"
        order = Order(
            id=order_id,
            symbol=symbol,
            side=side,
            qty=qty,
            status="open",
            filled_avg_price=fill,
            stop_price=stop_price,
            target_price=target_price,
            exit_price=None,
            exit_reason=None,
        )
        if side == "long":
            self.cash -= fill * qty
        else:
            self.cash += fill * qty
        self._positions[symbol] = Position(
            symbol=symbol,
            qty=qty,
            side=side,
            avg_entry=fill,
            market_price=fill,
            unrealized_pl=0.0,
        )
        self._orders[order_id] = _SimOrder(order, self.now)
        self._mark()
        return order

    def close_all_positions(self) -> None:
        for symbol in list(self._positions):
            price = self._open_price(symbol, self.now) or self._positions[symbol].market_price
            self._exit_symbol(symbol, price, "flatten")

    def get_order(self, order_id: str) -> Order:
        found = self._orders.get(order_id)
        if found is None:
            raise BrokerError(f"Ukendt ordre {order_id}.")
        return found.order

    def _apply_bar(self, symbol: str, bar: Bar) -> None:
        if symbol in self._positions:
            self._positions[symbol].market_price = bar.close
        for sim in self._orders.values():
            order = sim.order
            if order.symbol != symbol or order.exit_price is not None:
                continue
            if bar.ts < sim.entry_ts:
                continue
            hit = resolve_intrabar(order.side, order.stop_price, order.target_price, bar)
            if hit is None:
                continue
            reason, price = hit
            self._exit_symbol(symbol, price, reason)
            break

    def _exit_symbol(self, symbol: str, price: float, reason: str) -> None:
        position = self._positions.get(symbol)
        if position is None:
            return
        if position.side == "long":
            self.cash += price * position.qty
        else:
            self.cash -= price * position.qty
        self.daytrade_count += 1
        del self._positions[symbol]
        for sim in self._orders.values():
            if sim.order.symbol == symbol and sim.order.exit_price is None and sim.order.status == "open":
                sim.order.exit_price = price
                sim.order.exit_reason = reason
                sim.order.status = "closed"
                break

    def _open_price(self, symbol: str, moment: datetime) -> float | None:
        for bar in self.intraday.get(symbol, []):
            if bar.ts == moment:
                return bar.open
        prior = [bar for bar in self.intraday.get(symbol, []) if bar.ts < moment]
        if prior:
            return prior[-1].close
        return None

    def _last_close(self, symbol: str) -> float:
        prior = [bar for bar in self.intraday.get(symbol, []) if bar.ts < self.now]
        if prior:
            return prior[-1].close
        return self.intraday[symbol][0].open

    def _mark(self) -> None:
        for symbol, position in self._positions.items():
            price = self._last_close(symbol)
            position.market_price = price
            if position.side == "long":
                position.unrealized_pl = (price - position.avg_entry) * position.qty
            else:
                position.unrealized_pl = (position.avg_entry - price) * position.qty

    def _equity(self) -> float:
        self._mark()
        equity = self.cash
        for position in self._positions.values():
            if position.side == "long":
                equity += position.qty * position.market_price
            else:
                equity -= position.qty * position.market_price
        return equity
