"""Børsens kalender: helligdage, halve dage og nedtællingens tider uden Alpaca."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from trader.desk import calendar

ET = ZoneInfo("America/New_York")


def et(*args) -> datetime:
    return datetime(*args, tzinfo=ET)


def test_holidays_follow_the_nyse_rules():
    h26 = calendar.holidays(2026)
    assert date(2026, 1, 1) in h26 and date(2026, 1, 19) in h26 and date(2026, 2, 16) in h26
    assert h26[date(2026, 4, 3)] == "Langfredag"
    assert date(2026, 5, 25) in h26 and date(2026, 6, 19) in h26
    assert date(2026, 7, 3) in h26  # 4. juli er en lørdag
    assert date(2026, 9, 7) in h26 and date(2026, 11, 26) in h26 and date(2026, 12, 25) in h26
    h27 = calendar.holidays(2027)
    assert date(2027, 3, 26) in h27 and date(2027, 6, 18) in h27 and date(2027, 7, 5) in h27 and date(2027, 12, 24) in h27
    # Nytår på en lørdag (2028) giver ingen fridag fredag den 31. december.
    assert date(2027, 12, 31) not in h27 and date(2028, 1, 1) not in calendar.holidays(2028)


def test_early_closes():
    assert date(2026, 11, 27) in calendar.early_closes(2026)
    assert date(2026, 12, 24) in calendar.early_closes(2026)
    assert date(2026, 7, 3) not in calendar.early_closes(2026)  # fridag, ikke halv dag
    assert date(2025, 7, 3) in calendar.early_closes(2025)
    assert calendar.session(date(2026, 11, 27))[1].hour == 13


def test_schedule_open_closed_pre_and_after():
    open_ = calendar.schedule(et(2026, 11, 6, 15, 0))
    assert open_["is_open"] and open_["phase"] == "open" and open_["next_close"].startswith("2026-11-06T16:00")
    pre = calendar.schedule(et(2026, 11, 6, 8, 0))
    assert not pre["is_open"] and pre["phase"] == "pre" and pre["next_open"].startswith("2026-11-06T09:30")
    after = calendar.schedule(et(2026, 11, 6, 17, 0))
    assert after["phase"] == "after" and after["next_open"].startswith("2026-11-09T09:30")
    weekend = calendar.schedule(et(2026, 11, 7, 12, 0))
    assert weekend["phase"] == "closed" and weekend["weekend"] and weekend["next_open"].startswith("2026-11-09T09:30")
    thanks = calendar.schedule(et(2026, 11, 26, 12, 0))
    assert thanks["holiday"] == "Thanksgiving" and not thanks["is_open"]
    assert thanks["next_open"].startswith("2026-11-27T09:30") and thanks["next_close"].startswith("2026-11-27T13:00")


def test_desk_market_uses_the_calendar_without_alpaca(tmp_path):
    from dataclasses import replace

    from test_auth import _settings
    from trader.desk.config import DeskSettings
    from trader.desk.engine import DeskEngine
    from trader.desk.store import DeskStore

    cfg = replace(DeskSettings(), db_path=str(tmp_path / "desk.db"))
    desk = DeskEngine(cfg, _settings(tmp_path), store=DeskStore(cfg.db_path), now_fn=lambda: et(2026, 11, 7, 12, 0))
    market = desk.status()["market"]
    assert market["source"] == "kalender" and market["is_open"] is False
    assert market["next_open"].startswith("2026-11-09T09:30")
