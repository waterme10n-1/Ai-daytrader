"""Børsernes åbningstider, regnet ud lokalt, så nedtællingen virker, også når mægleren ikke svarer.

New York følger NYSE's faste regler for helligdage og halve dage. Alpacas ur vinder, når det er tilgængeligt,
fordi børsen i sjældne tilfælde lukker ekstraordinært (fx nationale sørgedage), og det kan en kalender ikke vide
på forhånd. København følger Nasdaq Copenhagens faste lukkedage.

Funktionerne tager en børs (se markets.py). Uden børs gælder New York.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

OPEN = time(9, 30)
CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
PRE_OPEN = time(4, 0)
AFTER_CLOSE = time(20, 0)


def _easter(year: int) -> date:
    """Påskedag (gregoriansk, Meeus/Jones/Butcher)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date, *, saturday_to_friday: bool = True) -> date | None:
    if day.weekday() == 5:
        return day - timedelta(days=1) if saturday_to_friday else None
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


@lru_cache(maxsize=16)
def holidays(year: int) -> dict[date, str]:
    days: dict[date, str] = {}

    def add(day: date | None, name: str) -> None:
        if day is not None and day.year == year:
            days[day] = name

    # Nytår på en lørdag flyttes ikke til fredag den 31. december.
    add(_observed(date(year, 1, 1), saturday_to_friday=False), "Nytår")
    add(_nth_weekday(year, 1, 0, 3), "Martin Luther King-dag")
    add(_nth_weekday(year, 2, 0, 3), "Presidents' Day")
    add(_easter(year) - timedelta(days=2), "Langfredag")
    add(_last_weekday(year, 5, 0), "Memorial Day")
    if year >= 2022:
        add(_observed(date(year, 6, 19)), "Juneteenth")
    add(_observed(date(year, 7, 4)), "USA's uafhængighedsdag")
    add(_nth_weekday(year, 9, 0, 1), "Labor Day")
    add(_nth_weekday(year, 11, 3, 4), "Thanksgiving")
    add(_observed(date(year, 12, 25)), "Jul")
    return days


@lru_cache(maxsize=16)
def early_closes(year: int) -> dict[date, str]:
    """Halve dage: børsen lukker kl. 13 ET."""
    closed = holidays(year)
    out: dict[date, str] = {}
    friday = _nth_weekday(year, 11, 3, 4) + timedelta(days=1)
    out[friday] = "Dagen efter Thanksgiving"
    for day, name in ((date(year, 7, 3), "Dagen før uafhængighedsdagen"), (date(year, 12, 24), "Juleaften")):
        if day.weekday() < 5 and day not in closed:
            out[day] = name
    return out


@lru_cache(maxsize=16)
def dk_holidays(year: int) -> dict[date, str]:
    """Nasdaq Copenhagens lukkedage. Store bededag er afskaffet fra 2024."""
    easter = _easter(year)
    days = {
        date(year, 1, 1): "Nytårsdag",
        easter - timedelta(days=3): "Skærtorsdag",
        easter - timedelta(days=2): "Langfredag",
        easter + timedelta(days=1): "2. påskedag",
        easter + timedelta(days=39): "Kristi himmelfartsdag",
        easter + timedelta(days=40): "Dagen efter Kristi himmelfart",
        easter + timedelta(days=50): "2. pinsedag",
        date(year, 6, 5): "Grundlovsdag",
        date(year, 12, 24): "Juleaften",
        date(year, 12, 25): "Juledag",
        date(year, 12, 26): "2. juledag",
        date(year, 12, 31): "Nytårsaften",
    }
    if year < 2024:
        days[easter + timedelta(days=26)] = "Store bededag"
    return dict(sorted(days.items()))


def dk_early_closes(year: int) -> dict[date, str]:
    """København har ingen faste halve dage."""
    return {}


def _market(market):
    if market is None:
        from trader.desk.markets import US

        return US
    return market


def is_trading_day(day: date, market=None) -> bool:
    market = _market(market)
    return day.weekday() < 5 and day not in market.holidays(day.year)


def session(day: date, market=None) -> tuple[datetime, datetime] | None:
    """Åbning og lukning for en handelsdag i børsens egen tid, eller None."""
    market = _market(market)
    if not is_trading_day(day, market):
        return None
    close = market.early_close if day in market.early_closes(day.year) else market.close
    return datetime.combine(day, market.open, tzinfo=market.tz), datetime.combine(day, close, tzinfo=market.tz)


def _next_session(after: date, market=None) -> tuple[datetime, datetime]:
    day = after
    for _ in range(15):
        day += timedelta(days=1)
        found = session(day, market)
        if found:
            return found
    raise RuntimeError("Ingen handelsdag fundet inden for 15 dage.")


def schedule(now: datetime, market=None) -> dict:
    """Markedets fase lige nu og de næste tidspunkter, i børsens egen tid."""
    market = _market(market)
    now = now.astimezone(market.tz)
    today = now.date()
    todays = session(today, market)
    holiday = market.holidays(today.year).get(today)
    early = market.early_closes(today.year).get(today)
    if todays and todays[0] <= now < todays[1]:
        phase, is_open = "open", True
        next_open, next_close = _next_session(today, market)[0], todays[1]
    else:
        is_open = False
        if todays and now < todays[0]:
            next_open, next_close = todays
            phase = "pre" if now.time() >= market.pre_open else "closed"
        else:
            next_open, next_close = _next_session(today, market)
            phase = "after" if todays and now.time() < market.after_close else "closed"
    if not todays:
        phase = "closed"
    return {
        "is_open": is_open,
        "phase": phase,
        "next_open": next_open.isoformat(timespec="seconds"),
        "next_close": next_close.isoformat(timespec="seconds"),
        "session_open": todays[0].isoformat(timespec="seconds") if todays else None,
        "session_close": todays[1].isoformat(timespec="seconds") if todays else None,
        "holiday": holiday,
        "early_close": early if todays else None,
        "weekend": today.weekday() >= 5,
    }
