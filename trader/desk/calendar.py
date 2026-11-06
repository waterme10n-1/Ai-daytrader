"""New York-børsens åbningstider, regnet ud lokalt, så nedtællingen virker, også når Alpaca ikke svarer.

Helligdage og halve dage følger NYSE's faste regler. Alpacas ur vinder, når det er tilgængeligt, fordi børsen
i sjældne tilfælde lukker ekstraordinært (fx nationale sørgedage), og det kan en kalender ikke vide på forhånd.
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


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in holidays(day.year)


def session(day: date) -> tuple[datetime, datetime] | None:
    """Åbning og lukning for en handelsdag (ET), eller None."""
    if not is_trading_day(day):
        return None
    close = EARLY_CLOSE if day in early_closes(day.year) else CLOSE
    return datetime.combine(day, OPEN, tzinfo=ET), datetime.combine(day, close, tzinfo=ET)


def _next_session(after: date) -> tuple[datetime, datetime]:
    day = after
    for _ in range(15):
        day += timedelta(days=1)
        found = session(day)
        if found:
            return found
    raise RuntimeError("Ingen handelsdag fundet inden for 15 dage.")


def schedule(now: datetime) -> dict:
    """Markedets fase lige nu og de næste tidspunkter. Alle tider er ET."""
    now = now.astimezone(ET)
    today = now.date()
    todays = session(today)
    holiday = holidays(today.year).get(today)
    early = early_closes(today.year).get(today)
    if todays and todays[0] <= now < todays[1]:
        phase, is_open = "open", True
        next_open, next_close = _next_session(today)[0], todays[1]
    else:
        is_open = False
        if todays and now < todays[0]:
            next_open, next_close = todays
            phase = "pre" if now.time() >= PRE_OPEN else "closed"
        else:
            next_open, next_close = _next_session(today)
            phase = "after" if todays and now.time() < AFTER_CLOSE else "closed"
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
