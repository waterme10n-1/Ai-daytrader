"""Københavns børs: kalender, bordets egen papirkonto og hele kæden fra SCOUT til lukket handel."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from test_auth import _login, _settings, quiet_engine

from trader.app import create_app
from trader.desk import calendar, markets, rules
from trader.desk.alpaca import Bar, BrokerError, NewsItem
from trader.desk.config import DK_WATCHLIST, DeskSettings, parse_watchlist
from trader.desk.denmark import DanishPaper, yahoo_symbol
from trader.desk.engine import DeskEngine
from trader.desk.store import DeskStore

CPH = ZoneInfo("Europe/Copenhagen")
DAY = date(2026, 11, 4)  # onsdag


def at(hour: int, minute: int, day: date = DAY) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=CPH)


def ramp(*, start=100.0, end=103.5, ramp_from=(10, 0), ramp_to=(11, 0), volume=1000.0, ramp_volume=10_000.0, day=DAY) -> list[Bar]:
    """Flad fra 9:00, så en jævn stigning på tung volumen, derefter flad til 17:00."""
    bars: list[Bar] = []
    moment = at(9, 0, day)
    a, b = at(*ramp_from, day), at(*ramp_to, day)
    while moment < at(17, 0, day):
        if moment < a:
            close = start
        elif moment < b:
            close = start + (end - start) * ((moment - a).total_seconds() / (b - a).total_seconds())
        else:
            close = end
        heavy = a <= moment < b and end != start
        bars.append(Bar(ts=moment, open=close - 0.02, high=close + 0.03, low=close - 0.05, close=close, volume=ramp_volume if heavy else volume))
        moment += timedelta(minutes=1)
    return bars


def daily(close: float = 100.0, volume: float = 480_000.0) -> list[Bar]:
    return [Bar(ts=at(9, 0, DAY - timedelta(days=i)), open=close, high=close + 1, low=close - 1, close=close, volume=volume) for i in range(30, 0, -1)]


class FakeYahoo:
    """Svarer som YahooData, men fra lister i hukommelsen."""

    def __init__(self, intraday: dict[str, list[Bar]], *, news: dict[str, list[NewsItem]] | None = None) -> None:
        self.intraday = intraday
        self.news_items = news or {}
        self.calls: list[tuple[str, str]] = []
        self.fail: set[str] = set()

    def chart(self, symbol: str, interval: str, range_: str):
        self.calls.append((symbol, interval))
        if symbol in self.fail:
            raise BrokerError(f"Yahoo Finance kender ikke {symbol}.")
        if interval == "1d":
            return daily(), {}
        bars = self.intraday.get(symbol, [])
        meta = {"regularMarketPrice": bars[-1].close if bars else None, "chartPreviousClose": 100.0}
        if bars:
            meta["regularMarketTime"] = int(bars[-1].ts.timestamp())
        return list(bars), meta

    def news(self, symbol: str):
        return list(self.news_items.get(symbol, []))

    def close(self) -> None:
        pass


def fresh(symbol: str) -> list[NewsItem]:
    return [NewsItem(headline=f"{symbol} hæver forventningerne", symbols=[symbol], created_at=datetime.now(CPH) - timedelta(minutes=20), summary="", url="https://example.com/dk")]


def make_dk(tmp_path, data: FakeYahoo, **overrides):
    clock = {"now": at(11, 0)}
    values = {"market": "dk", "watchlist": ("NOVO-B", "DSV"), "db_path": str(tmp_path / "desk_dk.db"), "auto_approve": True, "scan_minutes": 1, "fee_pct": 0.1, **overrides}
    cfg = replace(DeskSettings(), **values)
    store = DeskStore(cfg.db_path)
    now = lambda: clock["now"]
    paper = DanishPaper(store, start_cash=cfg.start_cash, fee_pct=cfg.fee_pct, data=data, now_fn=now)
    desk = DeskEngine(cfg, _settings(tmp_path), store=store, broker=paper, now_fn=now, background_replies=False)
    return desk, paper, clock


# ---------------------------------------------------------------------- kalender og børs
def test_copenhagen_holidays_and_hours():
    days = calendar.dk_holidays(2026)
    assert days[date(2026, 4, 2)] == "Skærtorsdag" and days[date(2026, 4, 6)] == "2. påskedag"
    assert days[date(2026, 5, 15)] == "Dagen efter Kristi himmelfart" and days[date(2026, 6, 5)] == "Grundlovsdag"
    assert date(2026, 12, 24) in days and date(2026, 12, 31) in days
    assert date(2023, 5, 5) in calendar.dk_holidays(2023)  # store bededag før 2024
    assert not any(name == "Store bededag" for name in days.values())
    dk = markets.DK
    plan = calendar.schedule(at(10, 0), dk)
    assert plan["phase"] == "open" and plan["next_close"] == "2026-11-04T17:00:00+01:00"
    assert calendar.schedule(at(8, 30), dk)["phase"] == "pre"
    assert calendar.schedule(at(17, 5), dk)["phase"] == "closed"  # ingen handel efter lukning
    holiday = calendar.schedule(datetime(2026, 6, 5, 10, 0, tzinfo=CPH), dk)
    assert not holiday["is_open"] and holiday["holiday"] == "Grundlovsdag"
    assert dk.scan_start == (9, 15) and dk.flatten_at(DAY) == (16, 50) and dk.gap_until == (10, 30) and dk.session_minutes == 480


def test_new_york_times_follow_half_days():
    us = markets.US
    friday = date(2026, 11, 27)  # dagen efter Thanksgiving: lukker 13:00
    assert us.flatten_at(friday) == (12, 50) and us.scan_end(friday) == (12, 30)
    assert us.flatten_at(DAY) == (15, 50)


def test_danish_tickers():
    assert parse_watchlist("novo-b, MAERSK-B.CO nda-dk gn rock.b 123 nvda", "dk") == ["NOVO-B", "MAERSK-B", "NDA-DK", "GN", "ROCK-B", "NVDA"]
    assert parse_watchlist("NOVO-B", "us") == []
    assert yahoo_symbol("NOVO-B") == "NOVO-B.CO" and yahoo_symbol("^OMXC25") == "^OMXC25"
    assert len(DK_WATCHLIST) == 16


# ---------------------------------------------------------------------- papirkontoen
def test_paper_account_fills_with_fee_and_closes_at_target(tmp_path):
    data = FakeYahoo({"NOVO-B": ramp()})
    store = DeskStore(str(tmp_path / "dk.db"))
    clock = {"now": at(11, 0)}
    paper = DanishPaper(store, start_cash=100_000, fee_pct=0.1, data=data, now_fn=lambda: clock["now"])
    order = paper.submit_bracket("NOVO-B", "long", 10, 102.0, 106.0)
    assert order.filled_avg_price == pytest.approx(103.5 * 1.001, abs=1e-3)
    assert paper.get_account().cash == pytest.approx(100_000 - 10 * 103.5 * 1.001, abs=0.01)
    assert paper.get_order(order.id).exit_price is None  # intet ramt endnu
    # Kursen stiger til 107 kl. 11:30: målet rammes, og salget trækker kurtage.
    data.intraday["NOVO-B"] = ramp() [:150] + ramp(start=103.5, end=107.0, ramp_from=(11, 30), ramp_to=(11, 40))[150:]
    clock["now"] = at(11, 45)
    done = paper.get_order(order.id)
    # Målet fyldes til målet eller bedre (springer kursen over målet, fyldes til åbningen), minus kurtage.
    assert done.exit_reason == "target" and done.exit_price >= 106.0 * 0.999 - 1e-6
    assert paper.get_account().cash == pytest.approx(100_000 + 10 * (done.exit_price - 103.5 * 1.001), abs=0.01)
    assert paper.get_positions() == []


def test_paper_account_stops_out_and_refuses_too_big_orders(tmp_path):
    data = FakeYahoo({"DSV": ramp()})
    store = DeskStore(str(tmp_path / "dk.db"))
    clock = {"now": at(11, 0)}
    paper = DanishPaper(store, start_cash=1_000, fee_pct=0.0, data=data, now_fn=lambda: clock["now"])
    with pytest.raises(BrokerError, match="Ikke nok kontanter"):
        paper.submit_bracket("DSV", "long", 20, 102.0, 106.0)
    order = paper.submit_bracket("DSV", "long", 5, 102.0, 106.0)
    assert [p.symbol for p in paper.get_positions()] == ["DSV"]
    data.intraday["DSV"] = ramp()[:150] + ramp(start=103.5, end=101.0, ramp_from=(11, 30), ramp_to=(11, 35))[150:]
    clock["now"] = at(11, 50)
    assert paper.get_order(order.id).exit_reason == "stop"
    second = paper.submit_bracket("DSV", "long", 2, 100.0, 103.0)
    paper.close_position("DSV")
    assert paper.get_order(second.id).exit_reason == "manual" and paper.get_positions() == []


def test_one_bad_ticker_does_not_stop_the_rest(tmp_path):
    data = FakeYahoo({"NOVO-B": ramp()})
    data.fail.add("XYZ")
    paper = DanishPaper(DeskStore(str(tmp_path / "dk.db")), data=data, now_fn=lambda: at(11, 0))
    bars = paper.get_intraday(["NOVO-B", "XYZ"], at(11, 0))
    assert len(bars["NOVO-B"]) == 120 and bars["XYZ"] == []
    with pytest.raises(BrokerError):
        paper.get_intraday(["XYZ"], at(11, 0))


# ---------------------------------------------------------------------- hele kæden
def test_copenhagen_desk_flags_buys_and_closes_on_its_own_paper_account(tmp_path):
    data = FakeYahoo({"NOVO-B": ramp(), "DSV": ramp(end=100.2, ramp_volume=1000), "^OMXC25": ramp(end=100.4, ramp_volume=1000)}, news={"NOVO-B": fresh("NOVO-B")})
    desk, _paper, clock = make_dk(tmp_path, data)
    assert desk.keys_present and desk.own_ledger and desk.market.key == "dk"
    desk.tick()
    row = desk.store.proposals_on(DAY.isoformat())[0]
    assert row["symbol"] == "NOVO-B" and row["ruling"] == "BUY" and row["status"] == "placed"
    assert row["flag"]["to_close_min"] == 360
    texts = [m["text"] for m in desk.store.messages(limit=80)]
    assert any("inkl. 0,1 % kurtage" in t for t in texts) and any("Yahoo Finance" in t for t in texts)
    assert any(t.startswith("NOVO-B:") and " kr." in t and "$" not in t for t in texts if "aktier for" in t)
    # Målet rammes en halv time senere.
    target = float(row["target"])
    data.intraday["NOVO-B"] = ramp()[:150] + ramp(start=103.5, end=target + 0.5, ramp_from=(11, 30), ramp_to=(11, 40))[150:]
    clock["now"] = at(11, 50)
    desk.tick()
    closed = desk.store.proposal(row["id"])
    assert closed["status"] == "closed" and closed["exit_reason"] == "target" and closed["pnl"] > 0
    assert any(" kr." in m["text"] for m in desk.store.messages(limit=80) if m["kind"] == "close")
    desk.tick(force=True)  # baggrundsløkken henter saldoen igen
    status = desk.status()
    assert status["market_info"]["currency"] == "DKK" and status["market_info"]["benchmark_name"] == "C25-indekset"
    assert status["ledger"]["start"] == 100_000 and status["account"]["equity"] == pytest.approx(100_000 + closed["pnl"], abs=0.02)
    assert status["market"]["is_open"] and status["params"]["log_time_et"] == "17:15"


def test_copenhagen_desk_waits_until_9_15_and_closes_everything_at_16_50(tmp_path):
    # Åbner 4 % over gårsdagens 100 og holder sig lige over VWAP: et gap, der holder.
    data = FakeYahoo({"NOVO-B": ramp(start=104, end=104.5, ramp_from=(9, 0), ramp_to=(9, 14)), "DSV": ramp()}, news={"NOVO-B": fresh("NOVO-B")})
    desk, paper, clock = make_dk(tmp_path, data)
    clock["now"] = at(9, 14)
    desk.tick()
    assert desk.store.proposals_on(DAY.isoformat()) == []
    clock["now"] = at(9, 16)
    desk.tick()
    first = desk.store.proposals_on(DAY.isoformat())[0]
    assert first["symbol"] == "NOVO-B" and first["flag"]["by_gap"]  # +4 % siden i går, før 10:30
    assert first["status"] == "placed", first.get("skeptic")
    clock["now"] = at(16, 50)
    desk.tick()
    closed = desk.store.proposal(first["id"])
    assert closed["status"] == "closed" and closed["exit_reason"] == "flatten"
    assert paper.get_positions() == []
    assert any("16:50" in m["text"] for m in desk.store.messages(limit=80))
    # Kontoen stemmer: kontanter = start + resultat (kurtage er med i købs- og salgskursen).
    assert paper.get_account().cash == pytest.approx(100_000 + closed["pnl"], abs=0.02)


def test_copenhagen_has_no_premarket_scan_and_texts_say_copenhagen(tmp_path):
    desk, _paper, clock = make_dk(tmp_path, FakeYahoo({}))
    clock["now"] = at(8, 30)
    assert not desk._premarket_now(at(8, 30))
    rules_text = {bot["key"]: bot["rules"] for bot in desk.status()["bots"]}
    assert "Københavns børs" in rules_text["scout"] and "Før åbning (4:00" not in rules_text["scout"]
    assert "Fra 09:15 til 10:30" in rules_text["scout"] and "bordets egen papirkonto" in rules_text["executor"]
    with pytest.raises(ValueError, match="egen papirkonto"):
        desk.test_keys("PK123", "secret", save=False)
    with pytest.raises(ValueError, match="NOVO-B"):
        desk.update_params({"watchlist": "!!"})


def test_diagnose_checks_yahoo_for_copenhagen(tmp_path):
    data = FakeYahoo({"NOVO-B": ramp(), "^OMXC25": ramp()}, news={"NOVO-B": fresh("NOVO-B")})
    desk, _paper, _clock = make_dk(tmp_path, data)
    result = desk.diagnose()
    assert result["ok"], result
    assert any("C25-indekset" in check["detail"] for check in result["checks"])


# ---------------------------------------------------------------------- web
def test_both_desks_are_served_side_by_side(tmp_path):
    from test_desk import FakeBroker, make_desk, ramp_bars

    settings = _settings(tmp_path)
    us, _ = make_desk(tmp_path / "us", FakeBroker({"NVDA": ramp_bars()}))
    dk, _paper, _clock = make_dk(tmp_path, FakeYahoo({"NOVO-B": ramp()}))
    app = create_app(settings=settings, meme_engine=quiet_engine(), desk_engine=us, desk_dk_engine=dk)
    with TestClient(app) as client:
        _login(client)
        listed = client.get("/api/desk/markets").json()["markets"]
        assert [m["key"] for m in listed] == ["us", "dk"]
        assert client.get("/api/desk/status").json()["market_info"]["currency"] == "USD"
        dk_status = client.get("/api/desk/status?market=dk").json()
        assert dk_status["market_info"]["currency"] == "DKK" and dk_status["market_info"]["name"] == "Københavns børs"
        token = client.cookies.get("dt_csrf")
        response = client.post("/api/desk/pause?market=dk", json={"paused": True}, headers={"X-CSRF-Token": token})
        assert response.status_code == 200 and dk.paused and not us.paused


def test_copenhagen_can_be_switched_off(tmp_path):
    from test_desk import FakeBroker, make_desk

    us, _ = make_desk(tmp_path, FakeBroker({}))
    app = create_app(settings=_settings(tmp_path), meme_engine=quiet_engine(), desk_engine=us)
    with TestClient(app) as client:
        _login(client)
        assert [m["key"] for m in client.get("/api/desk/markets").json()["markets"]] == ["us"]
        assert client.get("/api/desk/status?market=dk").status_code == 404


def test_rules_scale_volume_to_the_length_of_the_session():
    bars = ramp(end=100.3, volume=1000.0, ramp_volume=1000.0)
    per_day = [Bar(ts=b.ts, open=b.open, high=b.high, low=b.low, close=b.close, volume=480_000.0) for b in daily()]
    # 480.000 om dagen på 480 minutter er 1000 pr. minut: normal volumen, ingen markering.
    assert rules.scout("NOVO-B", bars, per_day, at(11, 0), move_pct=2, volume_x=2, session_minutes=480) is None
    # Regnet som New York (390 minutter) ville det se ud som 0,8x og stadig ingen markering, men tallet er forkert.
    flag = rules.scout("NOVO-B", ramp(), per_day, at(11, 0), move_pct=2, volume_x=2, session_minutes=480)
    assert flag is not None and flag.volume_x == pytest.approx(10.0, abs=0.1)


def test_late_day_rule_counts_minutes_to_the_close():
    from trader.desk import learning

    late = next(test for key, _label, test in learning.PATTERNS if key == "late_day")
    assert late({"to_close_min": 90}, {}, {}) and not late({"to_close_min": 300}, {}, {})
    # København kl. 15:30 er 90 minutter før lukning; en gammel New York-markering kl. 14:10 uden minuttal tæller også.
    assert late({"ts": "2026-11-04T14:10:00-05:00"}, {}, {}) and not late({"ts": "2026-11-04T11:00:00-05:00"}, {}, {})


def test_bot_messages_use_danish_numbers():
    from trader.desk import fmt

    assert fmt.num(1234.5) == "1.234,50" and fmt.pct(3.456) == "+3,5 %" and fmt.pct(-0.004, 2) == "0,00 %"
    assert fmt.money(-12.3, "DKK") == "-12,30 kr." and fmt.money(4965.4, "USD", decimals=0) == "4.965 $"
    assert fmt.short(2.0) == "2" and fmt.short(0.5) == "0,5"
    assert fmt.tidy("Største tab ved stop: 42 kr.. Åbne") == "Største tab ved stop: 42 kr. Åbne"
    flag = rules.scout("NOVO-B", ramp(), daily(), at(11, 0), move_pct=2, volume_x=2, session_minutes=480)
    text = rules.scout_text(flag)
    assert "+3,5 %" in text and "10,0x" in text and text.endswith("Kurs 103,44.")


def test_yahoo_parser_reads_the_real_format_and_uses_the_second_host_when_the_first_is_down():
    import httpx

    from trader.desk.denmark import YahooData

    open_ts = int(at(9, 0).timestamp())
    body = {
        "chart": {
            "result": [
                {
                    "meta": {"currency": "DKK", "symbol": "NOVO-B.CO", "regularMarketPrice": 512.4, "regularMarketTime": open_ts + 120, "chartPreviousClose": 505.0},
                    "timestamp": [open_ts, open_ts + 60, open_ts + 120],
                    "indicators": {"quote": [{"open": [505.5, None, 506.0], "high": [507.0, None, 512.9], "low": [505.0, None, 505.8], "close": [506.0, None, 512.4], "volume": [12000, None, 800]}]},
                }
            ],
            "error": None,
        }
    }
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if "search" in request.url.path:
            return httpx.Response(200, json={"news": [{"title": "Novo løfter prognosen", "publisher": "Børsen", "providerPublishTime": open_ts, "link": "https://example.com/novo"}]})
        if "UKENDT" in request.url.path:
            return httpx.Response(404, json={"chart": {"result": None, "error": {"description": "No data found"}}})
        if request.url.host == "query1.finance.yahoo.com":
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, json=body)

    data = YahooData(httpx.Client(transport=httpx.MockTransport(handler)))
    bars, meta = data.chart("NOVO-B", "1m", "1d")
    assert seen[:2] == ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
    assert [bar.close for bar in bars] == [506.0, 512.4]  # minuttet uden handel springes over
    assert bars[0].ts == at(9, 0) and bars[0].volume == 12000 and meta["chartPreviousClose"] == 505.0
    with pytest.raises(BrokerError, match="kender ikke"):
        data.chart("UKENDT", "1m", "1d")
    news = data.news("NOVO-B")
    assert news[0].headline == "Novo løfter prognosen" and news[0].url == "https://example.com/novo"


def test_a_morning_line_says_the_desk_is_awake_and_the_switch_knows_who_is_open(tmp_path):
    from test_desk import FakeBroker, make_desk

    data = FakeYahoo({"NOVO-B": ramp(), "DSV": ramp(end=100.2, ramp_volume=1000)}, news={"NOVO-B": fresh("NOVO-B")})
    desk, _paper, clock = make_dk(tmp_path, data)
    desk.tick()  # første dag: velkomsten om papirkontoen
    clock["now"] = at(9, 1, DAY + timedelta(days=1))
    desk.tick()
    opened = [m for m in desk.store.messages(limit=200) if m["kind"] == "open"]
    assert len(opened) == 1
    assert opened[0]["text"].startswith("Københavns børs er åben. SCOUT kigger 9:15–16:30 på 2 aktier. Papirkontoen:")
    clock["now"] = at(9, 2, DAY + timedelta(days=1))
    desk.tick()
    assert sum(1 for m in desk.store.messages(limit=200) if m["kind"] == "open") == 1  # kun én gang om dagen
    summary = desk.summary()
    assert summary["is_open"] and summary["phase"] == "open" and summary["open_trades"] in (0, 1)
    us, _ = make_desk(tmp_path / "us", FakeBroker({}))
    assert us._window_text(datetime(2026, 11, 4, 10, 0, tzinfo=ZoneInfo("America/New_York"))) == "15:45–21:30 dansk tid"
    app = create_app(settings=_settings(tmp_path), meme_engine=quiet_engine(), desk_engine=us, desk_dk_engine=desk)
    with TestClient(app) as client:
        _login(client)
        listed = {m["key"]: m for m in client.get("/api/desk/markets").json()["markets"]}
        assert listed["dk"]["is_open"] and listed["dk"]["label"] == "Åbent" and "pnl_today" in listed["us"]


def test_copenhagen_moves_at_1_percent_and_an_old_saved_2_percent_gives_way(tmp_path):
    assert DeskSettings.from_env_dk().move_pct == 1.0
    store = DeskStore(str(tmp_path / "desk_dk.db"))
    store.set_state("welcome", "1")
    store.set_json("retired_defaults", ["move_pct", "max_flags_per_day", "watchlist"])  # den første oprydning er sket
    store.set_json("params", {"move_pct": 2.0, "position_pct": 3.0})
    cfg = replace(DeskSettings(), market="dk", move_pct=1.0, db_path=str(tmp_path / "desk_dk.db"))
    desk = DeskEngine(cfg, _settings(tmp_path), store=store, broker=DanishPaper(store, data=FakeYahoo({})), background_replies=False)
    assert desk.cfg.move_pct == 1.0 and desk.cfg.position_pct == 3.0
    # New York rører den danske regel ikke: en gemt 2 % i USA bliver stående.
    from test_desk import FakeBroker, make_desk

    us_store = DeskStore(str(tmp_path / "desk_us.db"))
    us_store.set_json("params", {"move_pct": 2.5})
    us, _ = make_desk(tmp_path, FakeBroker({}), db_path=str(tmp_path / "desk_us.db"))
    assert us.cfg.move_pct == 2.5


def test_yahoo_429_pauses_politely_instead_of_asking_again_and_again():
    import httpx

    from trader.desk.denmark import YahooData

    now = {"t": 1000.0}
    calls: list[str] = []
    answer = {"status": 429, "retry": None}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if answer["status"] == 429:
            headers = {"Retry-After": answer["retry"]} if answer["retry"] else {}
            return httpx.Response(429, text="Too Many Requests", headers=headers)
        return httpx.Response(200, json={"chart": {"result": [{"meta": {}, "timestamp": [], "indicators": {"quote": [{}]}}]}})

    data = YahooData(httpx.Client(transport=httpx.MockTransport(handler)), clock=lambda: now["t"])
    with pytest.raises(BrokerError, match="pause med kurserne i 2 min"):
        data.chart("NOVO-B", "1m", "1d")
    assert calls == ["query1.finance.yahoo.com"]  # ingen omvej via den anden adresse
    # Under pausen spørger bordet slet ikke.
    now["t"] += 60
    with pytest.raises(BrokerError, match="prøver igen om 1 min"):
        data.chart("DSV", "1m", "1d")
    assert len(calls) == 1 and data.blocked_for() == 60
    # Næste 429 giver dobbelt så lang pause, og Retry-After vinder, hvis Yahoo beder om mere.
    now["t"] += 61
    answer["retry"] = "600"
    with pytest.raises(BrokerError, match="10 min"):
        data.chart("DSV", "1m", "1d")
    # Når Yahoo svarer igen, starter pausen forfra.
    now["t"] += 601
    answer["status"] = 200
    data.chart("DSV", "1m", "1d")
    answer["status"], answer["retry"] = 429, None
    with pytest.raises(BrokerError, match="2 min"):
        data.chart("DSV", "1m", "1d")


def test_copenhagen_asks_yahoo_far_less_often(tmp_path):
    assert DeskSettings.from_env_dk().scan_minutes == 5
    # Ingen nyheder: SCOUT markerer, men intet købes, så der er ingen ordrer at tjekke.
    data = FakeYahoo({"NOVO-B": ramp(), "DSV": ramp(end=100.2, ramp_volume=1000), "^OMXC25": ramp(end=100.4, ramp_volume=1000)})
    desk, _paper, clock = make_dk(tmp_path, data, scan_minutes=5, trial_buys=False)
    clock["now"] = at(11, 0)
    desk.tick()
    first = len(data.calls)
    assert first > 0
    for minute in range(1, 5):  # 11:01-11:04: mellem scanningerne spørges Yahoo slet ikke
        clock["now"] = at(11, minute)
        desk.tick()
    assert len(data.calls) == first
    clock["now"] = at(11, 5)
    desk.tick()
    assert len(data.calls) > first  # næste scanning
    # Lukket børs: højst hvert 30. minut.
    calls = len(data.calls)
    clock["now"] = at(17, 30)
    desk.tick()
    after_close = len(data.calls)
    clock["now"] = at(17, 50)
    desk.tick()
    assert len(data.calls) == after_close and after_close >= calls


def test_open_orders_are_checked_every_other_minute_but_never_miss_a_stop(tmp_path):
    data = FakeYahoo({"DSV": ramp()})
    store = DeskStore(str(tmp_path / "dk.db"))
    clock = {"now": at(11, 0)}
    paper = DanishPaper(store, start_cash=100_000, fee_pct=0.0, data=data, now_fn=lambda: clock["now"])
    order = paper.submit_bracket("DSV", "long", 5, 102.0, 106.0)
    clock["now"] = at(11, 30)
    paper.get_order(order.id)
    calls = len(data.calls)
    clock["now"] = at(11, 31)
    paper.get_order(order.id)
    assert len(data.calls) == calls  # under 2 minutter siden sidst: intet kald
    # Stoppet rammes 11:31; lukkes positionen 11:32, tjekkes de seneste minutter først, så salget sker til stoppet.
    data.intraday["DSV"] = ramp()[:151] + ramp(start=103.5, end=101.0, ramp_from=(11, 31), ramp_to=(11, 32))[151:]
    paper._bars.clear()
    clock["now"] = at(11, 33)
    paper.close_position("DSV")
    assert paper.get_order(order.id).exit_reason == "stop"


def test_old_saved_one_minute_scan_gives_way_in_copenhagen(tmp_path):
    store = DeskStore(str(tmp_path / "desk_dk.db"))
    store.set_state("welcome", "1")
    store.set_json("retired_defaults", ["move_pct", "max_flags_per_day", "watchlist", "dk_move_pct_2"])
    store.set_json("params", {"scan_minutes": 1})
    cfg = replace(DeskSettings(), market="dk", scan_minutes=5, db_path=str(tmp_path / "desk_dk.db"))
    desk = DeskEngine(cfg, _settings(tmp_path), store=store, broker=DanishPaper(store, data=FakeYahoo({})), background_replies=False)
    assert desk.cfg.scan_minutes == 5
