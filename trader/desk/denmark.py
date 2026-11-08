"""Bordets egen papirkonto til Københavns børs, med kurser fra Yahoo Finance.

Alpaca handler kun amerikanske aktier, så her er der ingen mægler. Bordet fører selv regnskabet: et køb fyldes til
seneste kurs, kurtagen trækkes fra, og stop og mål tjekkes mod hvert nyt minut, præcis som en bracket-ordre. Alt
er papirpenge. Der findes ingen kodevej til rigtige penge her.

Klassen har samme metoder som AlpacaPaper, så resten af bordet ikke ved, hvilken børs det handler på.
"""

from __future__ import annotations

import logging
import threading
import time as clock_time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

from trader.desk import calendar
from trader.desk.alpaca import (
    Account,
    Bar,
    BrokerError,
    Clock,
    NewsItem,
    Order,
    Position,
    resolve_intrabar,
    vwap,
)
from trader.desk.fmt import money, num
from trader.desk.markets import DK, Market

logger = logging.getLogger("trader.desk.dk")

# Yahoo har to ens adresser. Svarer den ene ikke (eller beder os vente), prøves den anden.
HOSTS = ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com")
CHART_PATH = "/v8/finance/chart/{symbol}"
SEARCH_PATH = "/v1/finance/search"
# Yahoo afviser kald uden en almindelig browser-User-Agent. Ingen personlige oplysninger sendes med.
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
LEDGER = "paper_ledger"
BAR_CACHE_SECONDS = 20.0
MINUTE = timedelta(minutes=1)


def yahoo_symbol(symbol: str) -> str:
    """NOVO-B → NOVO-B.CO. Indeks (^OMXC25) og fulde Yahoo-symboler bruges som de er."""
    if symbol.startswith("^") or "." in symbol:
        return symbol
    return f"{symbol}.CO"


class YahooData:
    """Minutbarer, dagsbarer, seneste kurs og overskrifter fra Yahoo Finance. Gratis og uden nøgle."""

    def __init__(self, client: httpx.Client | None = None, *, market: Market = DK) -> None:
        self._client = client or httpx.Client(timeout=12.0, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        self.tz = market.tz

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict[str, Any], what: str) -> dict:
        """Ét kald til Yahoo. Netværksfejl, 429 og 5xx prøves én gang til på den anden adresse."""
        problem: BrokerError | None = None
        for host in HOSTS:
            try:
                response = self._client.get(host + path, params=params)
            except httpx.HTTPError as exc:
                problem = BrokerError(f"Yahoo Finance svarede ikke ({what}): {exc.__class__.__name__}.")
                continue
            if response.status_code == 429:
                problem = BrokerError("Yahoo Finance beder bordet vente lidt (429). Kurserne kommer igen om et øjeblik.")
                continue
            if response.status_code >= 500:
                problem = BrokerError(f"Yahoo Finance har en fejl lige nu ({response.status_code}) for {what}.")
                continue
            if response.status_code == 404:
                raise BrokerError(f"Yahoo Finance kender ikke {what}. Tjek tickeren (fx NOVO-B).")
            if response.status_code in (401, 403):
                raise BrokerError(f"Yahoo Finance afviser kaldet ({response.status_code}) for {what}. Prøv igen senere.")
            if response.status_code >= 400:
                raise BrokerError(f"Yahoo Finance svarede {response.status_code} for {what}.")
            try:
                data = response.json()
            except ValueError as exc:
                raise BrokerError(f"Yahoo Finance sendte et svar, bordet ikke kan læse ({what}).") from exc
            return data if isinstance(data, dict) else {}
        raise problem or BrokerError(f"Yahoo Finance svarede ikke ({what}).")

    def chart(self, symbol: str, interval: str, range_: str) -> tuple[list[Bar], dict]:
        data = self._get(
            CHART_PATH.format(symbol=quote(yahoo_symbol(symbol))),
            {"interval": interval, "range": range_, "includePrePost": "false"},
            symbol,
        )
        chart = data.get("chart") or {}
        result = chart.get("result") or []
        if not result:
            error = chart.get("error") or {}
            raise BrokerError(f"Ingen kurser for {symbol} hos Yahoo Finance. {error.get('description') or ''}".strip())
        body = result[0] or {}
        meta = body.get("meta") or {}
        stamps = body.get("timestamp") or []
        quotes = ((body.get("indicators") or {}).get("quote") or [{}])[0] or {}

        def column(name: str) -> list:
            values = quotes.get(name) or []
            return values + [None] * (len(stamps) - len(values))

        opens, highs, lows, closes, volumes = (column(name) for name in ("open", "high", "low", "close", "volume"))
        bars: list[Bar] = []
        for index, stamp in enumerate(stamps):
            o, h, l, c = opens[index], highs[index], lows[index], closes[index]
            if None in (o, h, l, c) or not c:
                continue
            bars.append(
                Bar(
                    ts=datetime.fromtimestamp(int(stamp), tz=self.tz),
                    open=float(o),
                    high=float(h),
                    low=float(l),
                    close=float(c),
                    volume=float(volumes[index] or 0),
                )
            )
        return bars, meta

    def news(self, symbol: str) -> list[NewsItem]:
        data = self._get(SEARCH_PATH, {"q": yahoo_symbol(symbol), "quotesCount": 0, "newsCount": 8}, f"nyheder for {symbol}")
        items: list[NewsItem] = []
        for row in data.get("news") or []:
            title = str(row.get("title") or "").strip()
            stamp = row.get("providerPublishTime")
            if not title or not stamp:
                continue
            items.append(
                NewsItem(
                    headline=title,
                    symbols=[symbol],
                    created_at=datetime.fromtimestamp(int(stamp), tz=self.tz),
                    summary=str(row.get("publisher") or ""),
                    url=str(row.get("link") or ""),
                )
            )
        return items


class DanishPaper:
    """Papirkonto i kroner, ført af bordet selv. Samme metoder som AlpacaPaper."""

    def __init__(
        self,
        store,
        *,
        start_cash: float = 100_000.0,
        fee_pct: float = 0.1,
        data: YahooData | Any = None,
        now_fn: Callable[[], datetime] | None = None,
        market: Market = DK,
    ) -> None:
        self.store = store
        self.market = market
        self.tz = market.tz
        self.data = data or YahooData(market=market)
        self.fee = max(0.0, fee_pct) / 100.0
        self._now = now_fn or (lambda: datetime.now(self.tz))
        self._lock = threading.RLock()
        self._bars: dict[str, tuple[float, str, list[Bar]]] = {}
        self._last: dict[str, float] = {}
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="desk-dk-data")
        with self._lock:
            if self.store.get_json(LEDGER) is None:
                self.store.set_json(LEDGER, {"cash": float(start_cash), "start": float(start_cash), "orders": {}, "next": 1})

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        closer = getattr(self.data, "close", None)
        if closer:
            closer()

    # ------------------------------------------------------------------ regnskab
    def _ledger(self) -> dict:
        return self.store.get_json(LEDGER) or {"cash": 0.0, "start": 0.0, "orders": {}, "next": 1}

    def _save(self, ledger: dict) -> None:
        self.store.set_json(LEDGER, ledger)

    def ledger(self) -> dict:
        with self._lock:
            return self._ledger()

    def reset(self, start_cash: float) -> None:
        """Ny papirkonto. Kun når der ingen åbne positioner er."""
        with self._lock:
            ledger = self._ledger()
            if any(order["status"] == "open" for order in ledger["orders"].values()):
                raise BrokerError("Luk de åbne positioner, før papirkontoen nulstilles.")
            self._save({"cash": float(start_cash), "start": float(start_cash), "orders": {}, "next": 1})

    def _order(self, row: dict) -> Order:
        return Order(
            id=row["id"],
            symbol=row["symbol"],
            side="long",
            qty=int(row["qty"]),
            status=row["status"],
            filled_avg_price=row["fill"],
            stop_price=row["stop"],
            target_price=row["target"],
            exit_price=row.get("exit"),
            exit_reason=row.get("reason"),
        )

    def _exit(self, ledger: dict, row: dict, raw_price: float, reason: str, when: datetime) -> None:
        price = round(raw_price * (1 - self.fee), 4)  # salgskurs efter kurtage
        ledger["cash"] = round(float(ledger["cash"]) + price * int(row["qty"]), 2)
        row.update({"status": "closed", "exit": price, "exit_raw": round(raw_price, 4), "reason": reason, "closed_at": when.isoformat(timespec="seconds")})

    # ------------------------------------------------------------------ kurser
    def _fetch_today(self, symbol: str) -> list[Bar]:
        """Minutbarer for i dag. Genbruges inden for samme minut (scan, ordretjek og kurs i samme runde)."""
        stamp = clock_time.monotonic()
        minute = self._now().strftime("%Y%m%d%H%M")
        hit = self._bars.get(symbol)
        if hit and hit[1] == minute and stamp - hit[0] < BAR_CACHE_SECONDS:
            return hit[2]
        bars, _meta = self.data.chart(symbol, "1m", "1d")
        self._bars[symbol] = (stamp, minute, bars)
        if bars:
            self._last[symbol] = bars[-1].close
        return bars

    def _many(self, symbols: list[str], fetch: Callable[[str], Any]) -> dict[str, Any]:
        """Hent flere tickers på én gang. Én ticker, der fejler, stopper ikke resten; fejler alle, er det en fejl."""
        results: dict[str, Any] = {}
        errors: list[Exception] = []
        futures = {symbol: self._pool.submit(fetch, symbol) for symbol in symbols}
        for symbol, future in futures.items():
            try:
                results[symbol] = future.result(timeout=40)
            except Exception as exc:
                logger.info("Yahoo-kurser for %s fejlede: %s", symbol, exc)
                errors.append(exc)
                results[symbol] = None
        if symbols and len(errors) == len(symbols):
            first = errors[0]
            raise first if isinstance(first, BrokerError) else BrokerError(f"Yahoo Finance fejlede: {first}")
        return results

    def _session_bars(self, bars: list[Bar], day, now: datetime | None = None) -> list[Bar]:
        """Dagens barer fra åbningen. Med now: kun barer, der er begyndt (aldrig en kurs fra fremtiden)."""
        opened = self.market.at(day, self.market.open)
        return [bar for bar in bars if bar.ts.date() == day and bar.ts >= opened and (now is None or bar.ts <= now)]

    def get_intraday(self, symbols: list[str], now: datetime | None = None) -> dict[str, list[Bar]]:
        moment = (now or self._now()).astimezone(self.tz)
        raw = self._many(symbols, self._fetch_today)
        out: dict[str, list[Bar]] = {}
        for symbol in symbols:
            bars = self._session_bars(raw.get(symbol) or [], moment.date())
            out[symbol] = [bar for bar in bars if bar.ts + MINUTE <= moment]
        return out

    def get_daily(self, symbols: list[str], limit: int = 21, now: datetime | None = None) -> dict[str, list[Bar]]:
        moment = (now or self._now()).astimezone(self.tz)
        raw = self._many(symbols, lambda symbol: self.data.chart(symbol, "1d", "3mo")[0])
        return {symbol: [bar for bar in (raw.get(symbol) or []) if bar.ts.date() < moment.date()][-limit:] for symbol in symbols}

    def get_snapshots(self, symbols: list[str]) -> dict[str, dict]:
        def one(symbol: str) -> dict | None:
            bars, meta = self.data.chart(symbol, "1m", "1d")
            self._bars[symbol] = (clock_time.monotonic(), self._now().strftime("%Y%m%d%H%M"), bars)
            price = meta.get("regularMarketPrice") or (bars[-1].close if bars else None)
            if not price:
                return None
            self._last[symbol] = float(price)
            stamp = meta.get("regularMarketTime")
            when = datetime.fromtimestamp(int(stamp), tz=timezone.utc).isoformat() if stamp else (bars[-1].ts.isoformat() if bars else None)
            return {
                "price": float(price),
                "prev_close": float(meta["chartPreviousClose"]) if meta.get("chartPreviousClose") else None,
                "day_open": bars[0].open if bars else None,
                "day_volume": float(sum(bar.volume for bar in bars)),
                "vwap": vwap(bars),
                "ts": when,
            }

        return {symbol: snap for symbol, snap in self._many(symbols, one).items() if snap}

    def get_news(self, symbols: list[str], now: datetime | None = None) -> list[NewsItem]:
        moment = now or self._now()
        found = self._many(symbols, self.data.news)
        cutoff = moment - timedelta(hours=18)
        return [item for items in found.values() for item in (items or []) if item.created_at >= cutoff]

    def _price(self, symbol: str) -> float | None:
        now = self._now().astimezone(self.tz)
        try:
            bars = [bar for bar in self._fetch_today(symbol) if bar.ts <= now]
        except BrokerError as exc:
            logger.info("Seneste kurs for %s fejlede: %s", symbol, exc)
            bars = []
        if bars:
            return bars[-1].close
        return self._last.get(symbol)

    # ------------------------------------------------------------------ konto og ur
    def get_clock(self) -> Clock:
        now = self._now().astimezone(self.tz)
        plan = calendar.schedule(now, self.market)
        return Clock(
            is_open=plan["is_open"],
            timestamp=now,
            next_open=datetime.fromisoformat(plan["next_open"]),
            next_close=datetime.fromisoformat(plan["next_close"]),
        )

    def get_account(self) -> Account:
        with self._lock:
            ledger = self._ledger()
        held = sum(int(row["qty"]) * float(self._last.get(row["symbol"]) or row.get("last") or row["fill_raw"]) for row in ledger["orders"].values() if row["status"] == "open")
        cash = float(ledger["cash"])
        return Account(equity=round(cash + held, 2), cash=round(cash, 2), buying_power=round(cash, 2), daytrade_count=0)

    def get_positions(self) -> list[Position]:
        with self._lock:
            ledger = self._ledger()
        positions: dict[str, Position] = {}
        for row in ledger["orders"].values():
            if row["status"] != "open":
                continue
            price = float(self._last.get(row["symbol"]) or row.get("last") or row["fill_raw"])
            qty = int(row["qty"])
            held = positions.get(row["symbol"])
            if held:
                total = held.qty + qty
                entry = (held.avg_entry * held.qty + float(row["fill"]) * qty) / total
                positions[row["symbol"]] = Position(row["symbol"], total, "long", entry, price, round((price - entry) * total, 2))
            else:
                positions[row["symbol"]] = Position(row["symbol"], qty, "long", float(row["fill"]), price, round((price - float(row["fill"])) * qty, 2))
        return list(positions.values())

    # ------------------------------------------------------------------ ordrer
    def submit_bracket(self, symbol: str, side: str, qty: int, stop_price: float, target_price: float) -> Order:
        if side != "long":
            raise BrokerError("Bordet køber kun (ingen short).")
        if qty < 1:
            raise BrokerError("Antal aktier skal være mindst 1.")
        now = self._now().astimezone(self.tz)
        bars = self._session_bars(self._fetch_today(symbol), now.date(), now)
        if not bars:
            raise BrokerError(f"Ingen kurs for {symbol} i dag.")
        raw = bars[-1].close
        if not (stop_price < raw < target_price):
            raise BrokerError(f"Kursen {num(raw)} ligger ikke mellem stop {num(stop_price)} og mål {num(target_price)}.")
        fill = round(raw * (1 + self.fee), 4)  # købskurs inklusive kurtage
        with self._lock:
            ledger = self._ledger()
            cost = fill * qty
            if cost > float(ledger["cash"]) + 1e-6:
                raise BrokerError(f"Ikke nok kontanter: købet koster {money(cost, 'DKK')}, og der er {money(float(ledger['cash']), 'DKK')}.")
            order_id = f"dk-{int(ledger['next'])}"
            ledger["next"] = int(ledger["next"]) + 1
            ledger["cash"] = round(float(ledger["cash"]) - cost, 2)
            row = {
                "id": order_id,
                "symbol": symbol,
                "qty": int(qty),
                "fill": fill,
                "fill_raw": round(raw, 4),
                "stop": float(stop_price),
                "target": float(target_price),
                "status": "open",
                "opened_at": now.isoformat(timespec="seconds"),
                # Stop og mål tjekkes fra minuttet efter købet.
                "checked": bars[-1].ts.isoformat(),
                "last": raw,
            }
            ledger["orders"][order_id] = row
            self._save(ledger)
        self._last[symbol] = raw
        return self._order(row)

    def get_order(self, order_id: str) -> Order:
        with self._lock:
            row = self._ledger()["orders"].get(order_id)
        if row is None:
            raise BrokerError(f"Ukendt ordre {order_id}.")
        if row["status"] != "open":
            return self._order(row)
        now = self._now().astimezone(self.tz)
        try:
            bars = self._session_bars(self._fetch_today(row["symbol"]), now.date(), now)
        except BrokerError as exc:
            logger.info("Ordre %s kunne ikke tjekkes: %s", order_id, exc)
            return self._order(row)
        with self._lock:
            ledger = self._ledger()
            row = ledger["orders"][order_id]
            if row["status"] != "open":
                return self._order(row)
            since = datetime.fromisoformat(row["checked"])
            for bar in bars:
                if bar.ts <= since or bar.ts + MINUTE > now:
                    continue
                row["checked"] = bar.ts.isoformat()
                hit = resolve_intrabar("long", float(row["stop"]), float(row["target"]), bar)
                if hit:
                    reason, raw = hit
                    self._exit(ledger, row, raw, reason, bar.ts + MINUTE)
                    break
            if bars:
                row["last"] = bars[-1].close
            self._save(ledger)
            return self._order(row)

    def close_position(self, symbol: str, reason: str = "manual") -> None:
        price = self._price(symbol)
        now = self._now().astimezone(self.tz)
        with self._lock:
            ledger = self._ledger()
            rows = [row for row in ledger["orders"].values() if row["symbol"] == symbol and row["status"] == "open"]
            if not rows:
                return
            for row in rows:
                self._exit(ledger, row, float(price or row.get("last") or row["fill_raw"]), reason, now)
            self._save(ledger)
