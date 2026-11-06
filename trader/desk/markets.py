"""De to børser, bordet kan handle på: New York (Alpacas papirkonto) og København (bordets egen papirkonto).

Alt, der afhænger af børsen, står her: åbningstider, helligdage, valuta, indeks at måle sig mod, og hvornår SCOUT
må kigge, CHART må købe, og EXECUTOR lukker. Tiderne regnes ud fra dagens lukketid, så halve dage virker af sig selv.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from trader.desk import calendar


def _shift(moment: time, minutes: int) -> tuple[int, int]:
    total = moment.hour * 60 + moment.minute + minutes
    return divmod(total, 60)


@dataclass(frozen=True)
class Market:
    key: str
    name: str
    city: str
    country: str
    tz_name: str
    open: time
    close: time
    early_close: time
    pre_open: time
    after_close: time
    premarket_trading: bool  # handles der rigtige handler før åbning, som SCOUT kan læse?
    currency: str
    sign: str
    benchmark: str
    benchmark_name: str
    broker: str  # "alpaca" eller "intern"
    holidays: Callable[[int], dict[date, str]]
    early_closes: Callable[[int], dict[date, str]]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_name)

    @property
    def session_minutes(self) -> int:
        return (self.close.hour * 60 + self.close.minute) - (self.open.hour * 60 + self.open.minute)

    def close_on(self, day: date) -> time:
        return self.early_close if day in self.early_closes(day.year) else self.close

    # Tider som (time, minut) i børsens egen tid.
    @property
    def scan_start(self) -> tuple[int, int]:
        """SCOUT venter et kvarter efter åbningen, så de første vilde minutter ikke tæller."""
        return _shift(self.open, 15)

    @property
    def gap_until(self) -> tuple[int, int]:
        """Et gap ved åbningen tæller den første halvanden time."""
        return _shift(self.open, 90)

    def scan_end(self, day: date) -> tuple[int, int]:
        return _shift(self.close_on(day), -30)

    def latest_entry(self, day: date) -> tuple[int, int]:
        return _shift(self.close_on(day), -30)

    def late_from(self, day: date) -> tuple[int, int]:
        """Under en time til lukning: SKEPTIC's regler siger WAIT."""
        return _shift(self.close_on(day), -60)

    def flatten_at(self, day: date) -> tuple[int, int]:
        """Alt lukkes ti minutter før børsen lukker. Intet holdes natten over."""
        return _shift(self.close_on(day), -10)

    def log_at(self, day: date) -> tuple[int, int]:
        return _shift(self.close_on(day), 15)

    def at(self, day: date, moment: tuple[int, int] | time) -> datetime:
        if isinstance(moment, tuple):
            moment = time(*moment)
        return datetime.combine(day, moment, tzinfo=self.tz)

    def hhmm(self, moment: tuple[int, int]) -> str:
        return f"{moment[0]:02d}:{moment[1]:02d}"

    def local_hint(self, moment: tuple[int, int], day: date) -> str:
        """'15:50 ET (21:50 dansk tid)' for New York, bare '16:50' for København."""
        if self.key == "dk":
            return self.hhmm(moment)
        dk = self.at(day, moment).astimezone(ZoneInfo("Europe/Copenhagen"))
        return f"{self.hhmm(moment)} ET ({dk:%H:%M} dansk tid)"

    def money(self, value: float, *, signed: bool = True) -> str:
        text = f"{value:+,.2f}" if signed else f"{value:,.2f}"
        return f"{text} {self.currency}"


US = Market(
    key="us",
    name="New York-børsen",
    city="New York",
    country="USA",
    tz_name="America/New_York",
    open=time(9, 30),
    close=time(16, 0),
    early_close=time(13, 0),
    pre_open=time(4, 0),
    after_close=time(20, 0),
    premarket_trading=True,
    currency="USD",
    sign="$",
    benchmark="SPY",
    benchmark_name="S&P 500",
    broker="alpaca",
    holidays=calendar.holidays,
    early_closes=calendar.early_closes,
)

DK = Market(
    key="dk",
    name="Københavns børs",
    city="København",
    country="Danmark",
    tz_name="Europe/Copenhagen",
    open=time(9, 0),
    close=time(17, 0),
    early_close=time(17, 0),
    pre_open=time(8, 0),
    after_close=time(17, 0),  # ingen handel efter lukning
    premarket_trading=False,  # kun en åbningsauktion, ingen handler SCOUT kan læse
    currency="DKK",
    sign="kr.",
    benchmark="^OMXC25",
    benchmark_name="C25-indekset",
    broker="intern",
    holidays=calendar.dk_holidays,
    early_closes=calendar.dk_early_closes,
)

MARKETS = {"us": US, "dk": DK}


def get(key: str | None) -> Market:
    return MARKETS.get((key or "us").lower(), US)
