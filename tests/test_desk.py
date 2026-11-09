"""Handelsbordet: hele kæden fra SCOUT til EXECUTOR mod en falsk Alpaca, uden netværk og uden Claude."""

from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from trader.app import create_app
from trader.desk import learning, rules
from trader.desk.alpaca import Account, Bar, Clock, NewsItem, Order, Position
from trader.desk.config import DeskSettings, parse_watchlist
from trader.desk.engine import DeskEngine
from trader.desk.store import DeskStore

from test_auth import _login, _settings, quiet_engine

ET = ZoneInfo("America/New_York")
DAY = datetime(2026, 11, 4, tzinfo=ET).date()


def at(hour: int, minute: int) -> datetime:
    return datetime.combine(DAY, time(hour, minute), tzinfo=ET)


def ramp_bars(*, start: float = 100.0, end: float = 103.5, ramp_from=(10, 30), ramp_to=(11, 30), until=(16, 0), volume=1000.0, ramp_volume=10_000.0) -> list[Bar]:
    """Flad til ramp_from, så en jævn stigning på tung volumen til ramp_to, derefter flad."""
    bars: list[Bar] = []
    moment = at(9, 30)
    a, b = at(*ramp_from), at(*ramp_to)
    stop_at = at(*until)
    while moment < stop_at:
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


def daily_bars(volume: float = 390_000.0) -> list[Bar]:
    return [
        Bar(ts=at(0, 0) - timedelta(days=i), open=100, high=101, low=99, close=100, volume=volume)
        for i in range(20, 0, -1)
    ]


class FakeBroker:
    def __init__(self, intraday: dict[str, list[Bar]], *, equity: float = 100_000.0, news: dict[str, list[NewsItem]] | None = None):
        self.intraday = intraday
        self.now = at(11, 30)
        self.equity = equity
        self.news = news or {}
        self.orders: dict[str, Order] = {}
        self.positions: dict[str, Position] = {}
        self.submitted: list[tuple] = []
        self.closed: list[str] = []
        self.open = True
        self._ids = itertools.count(1)

    def get_clock(self) -> Clock:
        return Clock(is_open=self.open, timestamp=self.now, next_open=None, next_close=at(16, 0))

    def get_account(self) -> Account:
        return Account(equity=self.equity, cash=self.equity, buying_power=self.equity * 2, daytrade_count=0)

    def get_positions(self) -> list[Position]:
        return list(self.positions.values())

    def get_intraday(self, symbols, now=None):
        moment = now or self.now
        return {s: [b for b in self.intraday.get(s, []) if b.ts + timedelta(minutes=1) <= moment] for s in symbols}

    def get_daily(self, symbols, limit=21, now=None):
        return {s: daily_bars()[-limit:] for s in symbols}

    def get_news(self, symbols, now=None):
        return [item for s in symbols for item in self.news.get(s, [])]

    def _price(self, symbol: str) -> float:
        bars = self.get_intraday([symbol], self.now)[symbol]
        return bars[-1].close

    def submit_bracket(self, symbol, side, qty, stop_price, target_price):
        self.submitted.append((symbol, side, qty, stop_price, target_price))
        order = Order(
            id=f"o{next(self._ids)}", symbol=symbol, side=side, qty=qty, status="open",
            filled_avg_price=self._price(symbol), stop_price=stop_price, target_price=target_price,
            exit_price=None, exit_reason=None,
        )
        self.orders[order.id] = order
        self.positions[symbol] = Position(symbol, qty, "long", order.filled_avg_price, order.filled_avg_price, 0.0)
        return order

    def get_order(self, order_id):
        return self.orders[order_id]

    def close_position(self, symbol):
        self.closed.append(symbol)
        self.positions.pop(symbol, None)
        for order in self.orders.values():
            if order.symbol == symbol and order.exit_price is None:
                order.status = "canceled"

    def hit(self, symbol: str, reason: str) -> None:
        for order in self.orders.values():
            if order.symbol == symbol:
                order.exit_price = order.stop_price if reason == "stop" else order.target_price
                order.exit_reason = reason
                order.status = "closed"
        self.positions.pop(symbol, None)


def fresh_news(symbol: str, minutes_ago: int = 30) -> list[NewsItem]:
    return [
        NewsItem(
            headline=f"{symbol} lands a big new contract",
            symbols=[symbol],
            created_at=datetime.now(ET) - timedelta(minutes=minutes_ago),
            summary="",
            url="https://example.com/news",
        )
    ]


def make_desk(tmp_path, broker: FakeBroker, **overrides) -> tuple[DeskEngine, dict]:
    clock = {"now": at(11, 30)}
    values = {"watchlist": ("NVDA", "AAPL"), "db_path": str(tmp_path / "desk.db"), "auto_approve": False, "scan_minutes": 5, **overrides}
    cfg = replace(DeskSettings(), **values)
    main = _settings(tmp_path)

    def now():
        broker.now = clock["now"]
        return clock["now"]

    desk = DeskEngine(cfg, main, store=DeskStore(cfg.db_path), broker=broker, now_fn=now, background_replies=False)
    return desk, clock


def authors(desk: DeskEngine) -> list[str]:
    return [m["author"] for m in desk.store.messages(limit=500)]


# ---------------------------------------------------------------------- regler
def test_scout_flags_a_three_percent_hour_and_ignores_quiet_stocks():
    now = at(11, 31)
    hot = rules.scout("NVDA", ramp_bars(), daily_bars(), now, move_pct=3, volume_x=2)
    assert hot is not None and hot.by_move and hot.move_pct >= 3
    quiet = rules.scout("AAPL", ramp_bars(end=100.5, ramp_volume=1000), daily_bars(), now, move_pct=3, volume_x=2)
    assert quiet is None
    loud = rules.scout("AAPL", ramp_bars(end=100.5, volume=3000, ramp_volume=3000), daily_bars(), now, move_pct=3, volume_x=2)
    assert loud is not None and loud.by_volume and not loud.by_move


def test_chart_keeps_stop_within_two_percent_and_target_twice_as_far():
    bars = ramp_bars()
    now = at(11, 31)
    flag = rules.scout("NVDA", bars, daily_bars(), now, move_pct=3, volume_x=2)
    plan = rules.chart(flag, bars, now, stop_max_pct=2, reward_risk=2)
    assert plan.setup, plan.reason
    assert plan.stop < plan.entry < plan.target
    assert (plan.entry - plan.stop) / plan.entry <= 0.02 + 1e-9
    assert (plan.target - plan.entry) >= 2 * (plan.entry - plan.stop) - 1e-9
    # Bred stopzone: loftet på 2 % vinder over den laveste kurs.
    wide = rules.chart(flag, ramp_bars(end=103.5, ramp_from=(11, 20)), at(11, 31), stop_max_pct=0.5, reward_risk=2)
    if wide.setup:
        assert (wide.entry - wide.stop) / wide.entry <= 0.005 + 1e-9


def test_chart_says_no_setup_for_falling_stocks_and_late_in_the_day():
    bars = ramp_bars(start=103.5, end=100)
    now = at(11, 31)
    flag = rules.scout("NVDA", bars, daily_bars(), now, move_pct=3, volume_x=2)
    assert flag is not None and flag.move_pct < 0
    assert not rules.chart(flag, bars, now, stop_max_pct=2, reward_risk=2).setup
    up = ramp_bars(ramp_from=(14, 30), ramp_to=(15, 30))
    late = rules.scout("NVDA", up, daily_bars(), at(15, 35), move_pct=3, volume_x=2)
    assert not rules.chart(late, up, at(15, 35), stop_max_pct=2, reward_risk=2).setup


def test_sizer_caps_at_five_percent_and_says_zero_at_the_limits():
    sized = rules.size(equity=100_000, buying_power=200_000, entry=100, stop=98, open_count=0, position_pct=5, max_open=3, loss_limit_hit=False)
    assert sized.qty == 50 and sized.notional <= 5_000
    assert rules.size(equity=100_000, buying_power=200_000, entry=100, stop=98, open_count=3, position_pct=5, max_open=3, loss_limit_hit=False).qty == 0
    assert rules.size(equity=100_000, buying_power=200_000, entry=100, stop=98, open_count=0, position_pct=5, max_open=3, loss_limit_hit=True).qty == 0
    assert rules.size(equity=1_000, buying_power=2_000, entry=900, stop=890, open_count=0, position_pct=5, max_open=3, loss_limit_hit=False).qty == 0


def test_watchlist_parsing_and_hard_caps(tmp_path):
    assert parse_watchlist("nvda, $aapl;msft  NVDA brk.b 123 toolongticker") == ["NVDA", "AAPL", "MSFT", "BRK.B"]
    desk, _ = make_desk(tmp_path, FakeBroker({}))
    with pytest.raises(ValueError):
        desk.update_params({"position_pct": 50})
    with pytest.raises(ValueError):
        desk.update_params({"max_open": 9})
    with pytest.raises(ValueError):
        desk.update_params({"watchlist": "123, !!"})
    desk.update_params({"watchlist": "tsla, amd", "position_pct": 3})
    assert desk.cfg.watchlist == ("TSLA", "AMD") and desk.cfg.position_pct == 3


# ---------------------------------------------------------------------- hele kæden
def test_full_chain_waits_for_approval_then_places_bracket_and_closes(tmp_path):
    broker = FakeBroker({"NVDA": ramp_bars(), "AAPL": ramp_bars(end=100.2, ramp_volume=1000), "SPY": ramp_bars(end=100.3, ramp_volume=1000)}, news={"NVDA": fresh_news("NVDA")})
    desk, clock = make_desk(tmp_path, broker)
    clock["now"] = at(11, 30)
    desk.tick()
    order = authors(desk)
    for bot in ("scout", "news", "chart", "skeptic", "sizer", "executor"):
        assert bot in order
    assert order.index("scout") < order.index("news") < order.index("chart") < order.index("skeptic") < order.index("sizer") < order.index("executor")
    assert broker.submitted == []  # intet uden godkendelse
    pending = desk.store.proposals_with_status("awaiting")
    assert len(pending) == 1 and pending[0]["symbol"] == "NVDA"
    assert desk.store.proposals_on(DAY.isoformat())[0]["skeptic"]["source"] == "rules"

    desk.approve(pending[0]["id"])
    assert len(broker.submitted) == 1
    symbol, side, qty, stop, target = broker.submitted[0]
    assert symbol == "NVDA" and side == "long" and qty >= 1 and stop < target
    assert qty * pending[0]["entry"] <= 100_000 * 0.05 + 1

    broker.hit("NVDA", "target")
    clock["now"] = at(12, 0)
    desk.tick()
    row = desk.store.proposal(pending[0]["id"])
    assert row["status"] == "closed" and row["exit_reason"] == "target" and row["pnl"] > 0
    assert desk.status()["kpis"]["trades"] == 1


def test_no_news_reason_means_skip_even_if_claude_says_buy(tmp_path):
    broker = FakeBroker({"NVDA": ramp_bars()})
    desk, clock = make_desk(tmp_path, broker, trial_buys=False)
    desk.brain.skeptic = lambda *args, **kwargs: {"reasons": ["a", "b", "c"], "ruling": "BUY", "summary": "BUY", "source": "claude"}
    desk.tick()
    row = desk.store.proposals_on(DAY.isoformat())[0]
    assert row["status"] == "skip" and row["ruling"] == "SKIP"
    assert desk.store.proposals_with_status("awaiting") == []
    assert broker.submitted == []


def test_approval_refused_when_price_has_run(tmp_path):
    bars = ramp_bars(until=(11, 30)) + [
        Bar(ts=at(11, 30) + timedelta(minutes=i), open=105, high=105.1, low=104.9, close=105, volume=1000) for i in range(30)
    ]
    broker = FakeBroker({"NVDA": bars}, news={"NVDA": fresh_news("NVDA")})
    desk, clock = make_desk(tmp_path, broker)
    clock["now"] = at(11, 30)
    desk.tick()
    pending = desk.store.proposals_with_status("awaiting")
    assert pending
    clock["now"] = at(11, 35)
    with pytest.raises(ValueError):
        desk.approve(pending[0]["id"])
    assert broker.submitted == []
    assert desk.store.proposal(pending[0]["id"])["status"] == "expired"


def test_approval_expires_and_stop_now_pauses(tmp_path):
    broker = FakeBroker({"NVDA": ramp_bars()}, news={"NVDA": fresh_news("NVDA")})
    desk, clock = make_desk(tmp_path, broker)
    desk.tick()
    pid = desk.store.proposals_with_status("awaiting")[0]["id"]
    clock["now"] = at(11, 45)
    desk.tick()
    assert desk.store.proposal(pid)["status"] == "expired"
    with pytest.raises(ValueError):
        desk.approve(pid)

    desk.post_message("Stop nu")
    assert desk.paused
    broker.intraday["AAPL"] = ramp_bars()
    clock["now"] = at(12, 0)
    desk.tick()
    assert [row["symbol"] for row in desk.store.proposals(10)] == ["NVDA"]
    desk.post_message("Start igen")
    assert not desk.paused


def test_flatten_at_1550_and_day_log_with_shadow(tmp_path):
    broker = FakeBroker(
        {"NVDA": ramp_bars(), "AAPL": ramp_bars(), "SPY": ramp_bars(end=100.3)},
        news={"NVDA": fresh_news("NVDA")},
    )
    desk, clock = make_desk(tmp_path, broker, trial_buys=False)
    desk.tick()
    rows = {row["symbol"]: row for row in desk.store.proposals_on(DAY.isoformat())}
    assert rows["AAPL"]["status"] == "skip"  # ingen nyhed
    desk.approve(rows["NVDA"]["id"])
    clock["now"] = at(15, 50)
    desk.tick()
    assert broker.closed == ["NVDA"]
    assert desk.store.proposal(rows["NVDA"]["id"])["exit_reason"] == "flatten"

    broker.open = False
    clock["now"] = at(16, 15)
    desk.tick()
    log = desk.store.get_state(f"log:{DAY.isoformat()}")
    assert log and "Saldo" in log and "NVDA" in log
    assert desk.store.proposal(rows["AAPL"]["id"])["shadow"] is not None
    record = desk.skeptic_record()
    assert record["rulings"] == 2 and record["refused"] == 1
    clock["now"] = at(16, 30)
    desk.tick()
    assert sum(1 for m in desk.store.messages(limit=500) if m["kind"] == "log") == 1


# ---------------------------------------------------------------------- prøvekøb
def late_ramp() -> list[Bar]:
    """Stiger 3,5 % fra 14:00 til 15:00 ET. Kl. 15:05 siger reglerne WAIT (under en time til lukning), men opstillingen er ren."""
    return ramp_bars(ramp_from=(14, 0), ramp_to=(15, 0))


def test_trial_buys_turn_a_wait_with_a_reason_into_a_half_size_buy(tmp_path):
    broker = FakeBroker({"NVDA": late_ramp()}, news={"NVDA": fresh_news("NVDA")})
    desk, clock = make_desk(tmp_path, broker, watchlist=("NVDA",))
    assert desk.cfg.trial_buys and desk.status()["params"]["trial_buys"]
    clock["now"] = at(15, 5)
    assert desk.scan() == 1
    row = desk.store.proposals_on(DAY.isoformat())[0]
    assert row["ruling"] == "WAIT" and row["skeptic"]["trial"] and row["status"] == "awaiting"
    # Halv størrelse: 2,5 % af 100.000 i stedet for 5 %.
    assert row["size"]["pct"] <= 2.6 and row["qty"] == int(2_500 // row["entry"])
    assert any(m["text"].startswith("Prøvekøb: SKEPTIC sagde WAIT") for m in desk.store.messages(limit=50))
    assert desk.status()["approvals"][0]["trial"]


def test_trial_buys_also_take_a_move_without_news_but_never_volume_alone_or_when_switched_off(tmp_path):
    # Ingen nyhed: med prøvekøb købes en ren kursbevægelse alligevel, til halv størrelse.
    broker = FakeBroker({"NVDA": late_ramp()})
    desk, clock = make_desk(tmp_path / "a", broker, watchlist=("NVDA",))
    clock["now"] = at(15, 5)
    desk.scan()
    row = desk.store.proposals_on(DAY.isoformat())[0]
    assert row["ruling"] == "SKIP" and row["status"] == "awaiting"
    assert row["skeptic"]["trial"] and row["skeptic"]["trial_reason"] == "no_news" and row["size"]["pct"] <= 2.6
    assert any(m["text"].startswith("Prøvekøb: NEWS fandt ingen grund") for m in desk.store.messages(limit=50))
    # Slået fra: WAIT forbliver WAIT, og uden nyhed er det SKIP som før.
    broker = FakeBroker({"NVDA": late_ramp()}, news={"NVDA": fresh_news("NVDA")})
    desk, clock = make_desk(tmp_path / "b", broker, watchlist=("NVDA",))
    desk.update_params({"trial_buys": False})
    assert desk.store.get_json("params") == {"trial_buys": False} and not desk.cfg.trial_buys
    clock["now"] = at(15, 5)
    desk.scan()
    row = desk.store.proposals_on(DAY.isoformat())[0]
    assert row["status"] == "wait" and not (row["skeptic"] or {}).get("trial")
    # Kun volumen: kursen har ikke bekræftet noget, så ingen prøvekøb, heller ikke uden nyhed.
    loud = ramp_bars(end=100.5, volume=3000.0, ramp_volume=3000.0)
    for news in ({"NVDA": fresh_news("NVDA")}, {}):
        broker = FakeBroker({"NVDA": loud}, news=news)
        desk, clock = make_desk(tmp_path / f"c{len(news)}", broker, watchlist=("NVDA",))
        desk.scan()
        rows = desk.store.proposals_on(DAY.isoformat())
        assert rows and all(row["status"] != "awaiting" for row in rows)


def test_buys_without_news_stop_once_the_desk_has_seen_them_lose():
    rows = [
        {"status": "skip", "flag": {"by_move": True}, "news": {"has_reason": False}, "shadow": {"result": "loss", "pct": -1.0}}
        for _ in range(3)
    ]
    rule = next(item for item in learning.evaluate(rows) if item["key"] == "no_reason")
    assert rule["active"] and learning.blocking(learning.evaluate(rows), {"by_move": True}, {"has_reason": False}, {})


def test_a_learned_rule_stops_trial_buys_too(tmp_path):
    broker = FakeBroker({"NVDA": late_ramp()}, news={"NVDA": fresh_news("NVDA")})
    desk, clock = make_desk(tmp_path, broker, watchlist=("NVDA",))
    # Tre tabte køb sent på dagen: reglen "late_day" er lært.
    for index in range(3):
        pid = desk.store.create_proposal("2026-11-03", f"X{index}", {"symbol": f"X{index}", "move_pct": 3, "by_move": True, "to_close_min": 60})
        desk.store.update_proposal(pid, status="closed", fill_price=100.0, exit_price=98.0, pnl=-20.0, ruling="BUY")
    assert any(rule["key"] == "late_day" and rule["active"] for rule in desk.learned())
    clock["now"] = at(15, 5)
    desk.scan()
    row = desk.store.proposals_on(DAY.isoformat())[0]
    assert row["status"] == "wait" and not (row["skeptic"] or {}).get("trial")


def test_max_flags_and_max_open_are_respected(tmp_path):
    names = ["A", "B", "C", "D", "E", "F", "G"]
    broker = FakeBroker({n: ramp_bars() for n in names}, news={n: fresh_news(n) for n in names})
    desk, clock = make_desk(tmp_path, broker, watchlist=tuple(names), max_flags_per_day=5, max_open=3)
    desk.tick()
    rows = desk.store.proposals_on(DAY.isoformat())
    assert len(rows) == 5
    assert len(desk.store.proposals_with_status("awaiting")) == 3
    assert sum(1 for r in rows if r["status"] == "sized_zero") == 2


def loud_flat(volume: float = 3000.0) -> list[Bar]:
    """Handler tre gange normal volumen hele dagen, men kursen står næsten stille."""
    return ramp_bars(end=100.5, volume=volume, ramp_volume=volume)


def test_volume_only_flags_do_not_use_up_the_daily_quota(tmp_path):
    loud = ["V1", "V2", "V3", "V4", "V5", "V6"]
    data = {name: loud_flat() for name in loud} | {"M": ramp_bars()}
    broker = FakeBroker(data, news={name: fresh_news(name) for name in [*loud, "M"]})
    desk, clock = make_desk(tmp_path, broker, watchlist=(*loud, "M"), max_flags_per_day=5)
    clock["now"] = at(10, 15)
    assert desk.scan() == 5  # første time: alt handler tungt, men kun volumen-kvoten bliver brugt
    flags = desk.flags_on(DAY.isoformat())
    assert len(flags["volume"]) == 5 and flags["move"] == [] and "V6" not in flags["all"]
    clock["now"] = at(10, 16)
    assert desk.scan() == 0  # samme aktier markeres ikke igen på volumen
    clock["now"] = at(11, 30)
    assert desk.scan() == 1  # M flytter sig, og der er stadig plads, selv om fem er markeret på volumen
    flags = desk.flags_on(DAY.isoformat())
    assert flags["move"] == ["M"]
    status = desk.status()
    assert status["today"]["flags"] == 1 and status["today"]["volume_flags"] == 5


def test_a_volume_flag_can_be_followed_by_a_real_move(tmp_path):
    broker = FakeBroker({"NVDA": ramp_bars(volume=3000.0)}, news={"NVDA": fresh_news("NVDA")})
    desk, clock = make_desk(tmp_path, broker, watchlist=("NVDA",))
    clock["now"] = at(10, 15)
    assert desk.scan() == 1
    clock["now"] = at(11, 30)
    assert desk.scan() == 1
    rows = desk.store.proposals_on(DAY.isoformat())
    assert [bool(row["flag"]["by_move"]) for row in rows] == [False, True]
    clock["now"] = at(11, 45)
    assert desk.scan() == 0  # markeret på kursbevægelse: ikke igen samme dag


def test_move_quota_still_caps_real_moves(tmp_path):
    names = ["A", "B", "C"]
    broker = FakeBroker({n: ramp_bars() for n in names}, news={n: fresh_news(n) for n in names})
    desk, _ = make_desk(tmp_path, broker, watchlist=tuple(names), max_flags_per_day=2)
    assert desk.scan() == 2
    assert desk.scan() == 0
    assert len(desk.flags_on(DAY.isoformat())["move"]) == 2


def dip_then_rise() -> list[Bar]:
    """Falder 3 % fra 10:30 til 11:00 (ingen opstilling: bordet køber kun), står stille, og stiger så 4 % fra 12:00 til 12:45."""
    bars: list[Bar] = []
    moment = at(9, 30)
    while moment < at(16, 0):
        if moment < at(10, 30):
            close, volume = 100.0, 1000.0
        elif moment < at(11, 0):
            close, volume = 100.0 - 3.0 * (moment - at(10, 30)).total_seconds() / 1800, 10_000.0
        elif moment < at(12, 0):
            close, volume = 97.0, 1000.0
        elif moment < at(12, 45):
            close, volume = 97.0 + 4.0 * (moment - at(12, 0)).total_seconds() / 2700, 10_000.0
        else:
            close, volume = 101.0, 1000.0
        bars.append(Bar(ts=moment, open=close - 0.02, high=close + 0.03, low=close - 0.05, close=close, volume=volume))
        moment += timedelta(minutes=1)
    return bars


def test_a_stock_chart_said_no_to_gets_one_second_chance_and_does_not_use_the_quota(tmp_path):
    broker = FakeBroker({"NVDA": dip_then_rise()}, news={"NVDA": fresh_news("NVDA")})
    desk, clock = make_desk(tmp_path, broker, watchlist=("NVDA",), max_flags_per_day=1)
    clock["now"] = at(11, 0)
    assert desk.scan() == 1
    row = desk.store.proposals_on(DAY.isoformat())[0]
    assert row["status"] == "no_setup" and "falder" in row["chart"]["reason"]
    today = desk.status()["today"]
    assert today["flags"] == 0 and today["no_setup"] == 1  # en faldende aktie bruger ikke dagens eneste plads
    clock["now"] = at(11, 20)
    assert desk.scan() == 0  # under en halv time siden: ingen ny markering
    clock["now"] = at(11, 45)
    assert desk.scan() == 0  # stadig ikke på vej op: anden chance gemmes
    clock["now"] = at(12, 46)
    assert desk.scan() == 1
    rows = desk.store.proposals_on(DAY.isoformat())
    assert len(rows) == 2 and rows[1]["flag"]["second"] and rows[1]["status"] == "awaiting"
    assert any("Anden chance" in m["text"] for m in desk.store.messages(limit=50))
    assert desk.status()["today"]["flags"] == 1
    clock["now"] = at(13, 30)
    assert desk.scan() == 0  # højst to forsøg pr. aktie, og kvoten er brugt


def test_chart_allows_a_tiny_dip_but_not_a_real_turn():
    bars = ramp_bars()
    now = at(11, 31)
    flag = rules.scout("NVDA", bars, daily_bars(), now, move_pct=1.5, volume_x=2)
    dipped = [*bars[:-1]]
    done = rules.completed(dipped, now)
    last = done[-1]
    small = Bar(ts=last.ts, open=last.open, high=last.high, low=last.low, close=round(done[-6].close * 0.999, 2), volume=last.volume)
    big = Bar(ts=last.ts, open=last.open, high=last.high, low=last.low - 1, close=round(done[-6].close * 0.995, 2), volume=last.volume)
    swap = lambda bar: [b if b.ts != last.ts else bar for b in bars]
    assert rules.chart(flag, swap(small), now, stop_max_pct=2, reward_risk=2).setup
    turned = rules.chart(flag, swap(big), now, stop_max_pct=2, reward_risk=2)
    assert not turned.setup and "Momentum" in turned.reason


def test_new_scout_defaults():
    from trader.desk.config import DEFAULT_WATCHLIST

    assert DeskSettings().move_pct == 1.5 and DeskSettings().max_flags_per_day == 8
    assert DeskSettings().premarket and DeskSettings().gap_pct == 3.0
    assert {"SMCI", "MSTR", "SOFI", "RIVN"} <= set(DEFAULT_WATCHLIST) and len(DEFAULT_WATCHLIST) == 16


def test_old_saved_defaults_give_way_to_the_new_ones(tmp_path):
    from trader.desk.config import DEFAULT_WATCHLIST, RETIRED_DEFAULTS

    broker = FakeBroker({})
    store = DeskStore(str(tmp_path / "desk.db"))
    store.set_state("welcome", "1")
    store.set_json("params", {"move_pct": 3.0, "max_flags_per_day": 5, "watchlist": list(RETIRED_DEFAULTS["watchlist"]), "position_pct": 3.0})
    desk, _ = make_desk(tmp_path, broker, watchlist=DEFAULT_WATCHLIST)
    assert desk.cfg.move_pct == desk.base.move_pct and desk.cfg.watchlist == DEFAULT_WATCHLIST and desk.cfg.position_pct == 3.0
    assert desk.cfg.max_flags_per_day == 8
    assert "SCOUT er opdateret" in desk.store.messages(limit=5)[-1]["text"]
    # Kun én gang: vælger man selv 3 % bagefter, bliver det ved med at gælde.
    desk.update_params({"move_pct": 3})
    again, _ = make_desk(tmp_path, broker, watchlist=DEFAULT_WATCHLIST)
    assert again.cfg.move_pct == 3.0


def test_saving_the_defaults_does_not_pin_them(tmp_path):
    desk, _ = make_desk(tmp_path, FakeBroker({}))
    desk.update_params({"move_pct": desk.base.move_pct, "watchlist": ",".join(desk.base.watchlist), "auto_approve": desk.base.auto_approve, "max_open": 2})
    assert desk.store.get_json("params") == {"max_open": 2}


# ---------------------------------------------------------------------- før åbning og gaps
def snapshots(prices: dict[str, float], when: datetime) -> callable:
    return lambda symbols: {
        s: {"price": prices[s], "prev_close": 99.0, "day_open": None, "day_volume": 50_000.0, "vwap": None, "ts": when.isoformat()}
        for s in symbols
        if s in prices
    }


def gap_bars(start: float = 104.0, end: float = 104.5) -> list[Bar]:
    """Åbner med et gap (gårsdagens lukkekurs er 100) og stiger lidt det første kvarter."""
    return ramp_bars(start=start, end=end, ramp_from=(9, 30), ramp_to=(9, 45))


def test_premarket_scout_and_news_find_gaps_before_the_open(tmp_path):
    broker = FakeBroker({}, news={"MSTR": fresh_news("MSTR")})
    broker.open = False
    broker.get_snapshots = snapshots({"MSTR": 104.2, "NVDA": 101.0, "AAPL": 106.0}, at(7, 58))
    desk, clock = make_desk(tmp_path, broker, watchlist=("MSTR", "NVDA"))
    clock["now"] = at(8, 0)
    desk.tick()
    notes = desk.premarket_on(DAY.isoformat())
    assert list(notes) == ["MSTR"]  # NVDA er kun +1 %, AAPL er ikke på watchlisten
    assert notes["MSTR"]["gap_pct"] == 4.2 and notes["MSTR"]["news"]["has_reason"]
    said = [(m["author"], m["kind"]) for m in desk.store.messages(limit=50)]
    assert ("scout", "premarket") in said and ("news", "news") in said
    assert desk.store.proposals_on(DAY.isoformat()) == []  # ingen køb før åbning
    status = desk.status()
    assert status["premarket"][0]["symbol"] == "MSTR" and status["premarket"][0]["has_reason"]
    assert status["quotes"]["mode"] == "snapshot"
    mstr = next(q for q in status["quotes"]["items"] if q["symbol"] == "MSTR")
    assert mstr["day_pct"] == 4.2  # mod gårsdagens lukkekurs, ikke Alpacas prevDailyBar
    # Samme gap noteres ikke igen, og uden for hvert 5. minut kigger SCOUT ikke.
    clock["now"] = at(8, 5)
    desk.tick()
    assert sum(1 for m in desk.store.messages(limit=80) if m["kind"] == "premarket") == 1
    assert "Før åbning: MSTR +4,2 %" in desk._rules_answer("scout")


def test_premarket_ignores_old_prices_and_can_be_switched_off(tmp_path):
    broker = FakeBroker({})
    broker.open = False
    broker.get_snapshots = snapshots({"MSTR": 108.0}, at(7, 58) - timedelta(days=1))
    desk, clock = make_desk(tmp_path, broker, watchlist=("MSTR",))
    clock["now"] = at(8, 0)
    assert desk.premarket_scan() == 0  # kursen er fra i går og siger intet om i dag
    broker.get_snapshots = snapshots({"MSTR": 108.0}, at(7, 58))
    desk.update_params({"premarket": False})
    clock["now"] = at(8, 10)
    desk.tick()
    assert desk.premarket_on(DAY.isoformat()) == {}
    clock["now"] = at(3, 55)
    assert not desk._premarket_now(at(3, 55))


def test_a_gap_that_holds_is_flagged_at_the_open_and_reuses_the_early_news(tmp_path):
    broker = FakeBroker({"MSTR": gap_bars()}, news={"MSTR": fresh_news("MSTR")})
    broker.get_snapshots = snapshots({"MSTR": 104.2}, at(7, 58))
    desk, clock = make_desk(tmp_path, broker, watchlist=("MSTR",))
    clock["now"] = at(8, 0)
    assert desk.premarket_scan() == 1
    broker.news = {}  # ingen nye overskrifter: NEWS må ikke slå op igen
    clock["now"] = at(9, 45)
    assert desk.scan() == 1
    row = desk.store.proposals_on(DAY.isoformat())[0]
    assert row["flag"]["by_gap"] and row["flag"]["by_move"] and row["flag"]["gap_pct"] >= 4
    assert row["news"]["premarket"] and row["news"]["has_reason"]
    assert row["chart"]["setup"] and row["ruling"] == "BUY" and row["status"] == "awaiting"
    texts = [m["text"] for m in desk.store.messages(limit=80)]
    assert any(t.startswith("GAP: MSTR") for t in texts) and any("(fundet før åbning)" in t for t in texts)


def test_gaps_only_count_early_and_only_when_above_vwap(tmp_path):
    late = rules.gap_flag("MSTR", gap_bars(), daily_bars(), at(11, 0), gap_pct=3, volume_x=2)
    assert late is None
    fading = ramp_bars(start=106.0, end=103.5, ramp_from=(9, 30), ramp_to=(9, 45))
    assert rules.gap_flag("MSTR", fading, daily_bars(), at(9, 45), gap_pct=3, volume_x=2) is None
    small = rules.gap_flag("MSTR", gap_bars(start=101.0, end=101.5), daily_bars(), at(9, 45), gap_pct=3, volume_x=2)
    assert small is None
    held = rules.gap_flag("MSTR", gap_bars(), daily_bars(), at(9, 45), gap_pct=3, volume_x=2)
    assert held is not None and held.by_gap and held.gap_pct >= 4 and held.vs_vwap_pct > 0


def test_chart_judges_a_gap_on_the_gap_not_the_first_minutes(tmp_path):
    held = rules.gap_flag("MSTR", gap_bars(), daily_bars(), at(9, 45), gap_pct=3, volume_x=2)
    held.move_pct = -0.2  # et lille dyk efter åbningen må ikke afvise et gap, der stadig er +4 %
    plan = rules.chart(held, gap_bars(), at(9, 45), stop_max_pct=2, reward_risk=2)
    assert plan.setup, plan.reason


def test_live_day_change_is_against_yesterdays_close():
    items = rules.quotes(["MSTR"], {"MSTR": gap_bars()}, {"MSTR": daily_bars()}, at(9, 45), [])
    assert items[0]["day_pct"] >= 4  # gårsdagens lukkekurs er 100, ikke dagens åbning på 104


def test_gap_buys_are_a_pattern_the_desk_can_learn_from():
    rows = [
        {"status": "closed", "pnl": -40, "fill_price": 100, "exit_price": 98, "flag": {"by_gap": True, "by_move": True}}
        for _ in range(3)
    ]
    rule = next(item for item in learning.evaluate(rows) if item["key"] == "gap_open")
    assert rule["active"] and rule["n"] == 3


def test_mentions_get_an_answer_without_claude(tmp_path):
    desk, _ = make_desk(tmp_path, FakeBroker({}))
    desk.post_message("@skeptic hvordan går det?")
    last = desk.store.messages(limit=5)[-1]
    assert last["author"] == "skeptic" and "ANTHROPIC_API_KEY mangler" in last["text"] and "afgjort" in last["text"]
    desk.post_message("hej")
    assert desk.store.messages(limit=1)[-1]["author"] == "system"


# ---------------------------------------------------------------------- web
def test_desk_routes_need_login_and_work(tmp_path):
    settings = _settings(tmp_path)
    broker = FakeBroker({"NVDA": ramp_bars()}, news={"NVDA": fresh_news("NVDA")})
    desk, clock = make_desk(tmp_path, broker)
    desk.start = lambda: None  # testen styrer selv minutterne
    app = create_app(settings=settings, meme_engine=quiet_engine(), desk_engine=desk)
    with TestClient(app) as client:
        assert client.get("/desk", follow_redirects=False).status_code == 303
        assert client.get("/api/desk/status").status_code == 401
        assert _login(client).status_code == 303
        page = client.get("/desk")
        assert page.status_code == 200 and "Handelsbordet" in page.text
        csrf = client.cookies.get("dt_csrf")
        headers = {"X-CSRF-Token": csrf}
        assert client.post("/api/desk/message", json={"text": "@sizer?"}).status_code == 403  # uden CSRF
        desk.tick()
        status = client.get("/api/desk/status").json()
        assert status["mode"] == "paper" and len(status["bots"]) == 6
        pid = status["approvals"][0]["id"]
        newer = client.get(f"/api/desk/status?after={status['last_message_id']}").json()
        assert newer["messages"] == []
        assert client.post("/api/desk/approve", json={"id": pid}, headers=headers).status_code == 200
        assert client.post("/api/desk/approve", json={"id": pid}, headers=headers).status_code == 409
        assert client.post("/api/desk/params", json={"position_pct": 99}, headers=headers).status_code == 400
        assert client.post("/api/desk/pause", json={"paused": True}, headers=headers).status_code == 200
        assert client.get("/api/desk/status").json()["paused"] is True
        compare = client.get("/api/desk/compare").json()
        assert compare["started"] is True


# ---------------------------------------------------------------------- Claude-stien (falsk klient)
class _FakeResponse:
    def __init__(self, parsed):
        self.parsed_output = parsed
        self.stop_reason = "end_turn"
        self.content = []
        self.model = "claude-opus-5-5"
        self.usage = {"input_tokens": 1000, "output_tokens": 200}


class _FakeClient:
    def __init__(self, parsed):
        self.calls = []
        outer = self

        class _Messages:
            def parse(self, **kwargs):
                outer.calls.append(kwargs)
                return _FakeResponse(parsed)

        class _Beta:
            messages = _Messages()

        self.beta = _Beta()


def test_claude_news_needs_a_real_source_and_spend_is_counted(tmp_path):
    from trader.desk.agents import DeskBrain, NewsOut, SkepticOut

    store = DeskStore(str(tmp_path / "desk.db"))
    brain = DeskBrain(DeskSettings(db_path=str(tmp_path / "desk.db")), store, api_key="test")
    flag = {"symbol": "NVDA", "move_pct": 4.2, "volume_x": 2.3}

    brain._client = lambda: _FakeClient(NewsOut(has_reason=True, reason="Rygte på X.", source_url=""))
    assert brain.news(flag, [])["has_reason"] is False

    fake = _FakeClient(NewsOut(has_reason=True, reason="Nye eksportlicenser godkendt i morges.", source_url="https://example.com/a", source_title="Reuters"))
    brain._client = lambda: fake
    news = brain.news(flag, [])
    assert news["has_reason"] and news["source"] == "claude" and news["source_url"] == "https://example.com/a"
    assert fake.calls[0]["tools"][0]["name"] == "web_search"
    assert "SKEPTIC" not in fake.calls[0]["system"] or "NEWS" in fake.calls[0]["system"]
    assert store.spend_on(datetime.now(ET).date().isoformat()) > 0

    brain._client = lambda: _FakeClient(SkepticOut(reasons=["1", "2", "3"], ruling="WAIT", summary="WAIT."))
    verdict = brain.skeptic(flag, news, {"entry": 1}, {})
    assert verdict["ruling"] == "WAIT" and len(verdict["reasons"]) == 3

    tiny = DeskBrain(DeskSettings(ai_daily_budget_usd=0.0), store, api_key="test")
    assert tiny.skeptic(flag, news, {}, {}) is None  # intet budget: reglerne afgør


def test_autopilot_buys_on_its_own_but_only_after_skeptic_says_buy(tmp_path):
    broker = FakeBroker(
        {"NVDA": ramp_bars(), "AAPL": ramp_bars(), "SPY": ramp_bars(end=100.3, ramp_volume=1000)},
        news={"NVDA": fresh_news("NVDA")},
    )
    desk, clock = make_desk(tmp_path, broker, auto_approve=True, scan_minutes=1, trial_buys=False)
    clock["now"] = at(11, 31)
    desk.tick()
    rows = {row["symbol"]: row for row in desk.store.proposals_on(DAY.isoformat())}
    assert rows["NVDA"]["status"] == "placed"
    assert rows["AAPL"]["status"] == "skip"  # ingen grund fra NEWS, så intet køb
    assert [order[0] for order in broker.submitted] == ["NVDA"]
    assert desk.store.proposals_with_status("awaiting") == []
    desk.post_message("Stop nu")
    broker.intraday["AMD"] = ramp_bars()
    clock["now"] = at(11, 32)
    desk.tick()
    assert len(broker.submitted) == 1


def test_autopilot_and_scan_interval_can_be_changed(tmp_path):
    desk, _ = make_desk(tmp_path, FakeBroker({}), auto_approve=True)
    desk.update_params({"auto_approve": False, "scan_minutes": 2})
    assert desk.cfg.auto_approve is False and desk.cfg.scan_minutes == 2
    with pytest.raises(ValueError):
        desk.update_params({"scan_minutes": 0})
    assert DeskSettings().auto_approve is True and DeskSettings().scan_minutes == 1


def test_keys_are_cleaned_and_wrong_keys_are_explained():
    from trader.desk.config import clean_key, key_problem

    assert clean_key(' "PKABC 123" ') == "PKABC123"
    assert clean_key("DESK_ALPACA_API_KEY=PKXYZ") == "PKXYZ"
    assert "live-nøgle" in key_problem("AKLIVE")
    assert "byttet om" in key_problem("abc123secret")
    assert key_problem("PKFINE") == ""


def test_diagnose_reports_each_part_and_key_hints(tmp_path):
    broker = FakeBroker({"SPY": ramp_bars(end=100.3, ramp_volume=1000)})
    desk, _ = make_desk(tmp_path, broker)
    result = desk.diagnose()
    assert result["ok"] and [c["name"] for c in result["checks"]] == ["Handelskontoen", "Markedets ur", "Kursdata", "Nyheder"]

    def boom():
        raise RuntimeError("Alpaca afviser nøglerne ved handelskontoen (401: unauthorized)")

    broker.get_account = boom
    bad = desk.diagnose()
    assert not bad["ok"] and not bad["checks"][0]["ok"] and "afviser" in bad["text"]

    swapped = make_desk(tmp_path / "b", FakeBroker({}), alpaca_api_key="x" * 40, alpaca_secret_key="PKSHORTKEY12345678", key_source="DESK_ALPACA_API_KEY", secret_source="ALPACA_SECRET_KEY")[0]
    hints = " ".join(swapped.diagnose()["keys"]["hints"])
    assert "byttet om" in hints and "samme sted" in hints


def test_401_message_names_the_service():
    from trader.desk.alpaca import DATA_URL, PAPER_URL, service_name, short_body

    assert "handelskontoen" in service_name(PAPER_URL + "/v2/account")
    assert "kursdata" in service_name(DATA_URL + "/v2/stocks/bars")
    assert short_body("<html><h1>401 Authorization Required</h1></html>") == "401 Authorization Required"


# ---------------------------------------------------------------------- læring
def _done_row(symbol, *, vs_vwap, win, real=True):
    flag = {"symbol": symbol, "move_pct": 3.5, "vs_vwap_pct": vs_vwap, "by_move": True, "ts": "2026-11-04T11:30:00-05:00"}
    row = {"symbol": symbol, "flag": flag, "news": {"source": "claude", "has_reason": True}, "chart": {"stop_pct": 0.8}}
    if real:
        row.update(status="closed", fill_price=100.0, exit_price=102.0 if win else 99.0, pnl=20.0 if win else -10.0, exit_reason="target" if win else "stop")
    else:
        row.update(status="skip", shadow={"result": "win" if win else "loss", "pct": 2.0 if win else -1.0})
    return row


def test_learning_turns_on_after_repeated_losses_and_off_again():
    from trader.desk import learning

    rows = [_done_row("A", vs_vwap=2.0, win=False), _done_row("B", vs_vwap=2.2, win=False, real=False), _done_row("C", vs_vwap=1.8, win=False)]
    rule = {r["key"]: r for r in learning.evaluate(rows)}["far_above_vwap"]
    assert rule["active"] and rule["n"] == 3 and rule["real"] == 2
    assert learning.blocking(learning.evaluate(rows), {"vs_vwap_pct": 2.5, "by_move": True}, {}, {})
    assert not learning.blocking(learning.evaluate(rows), {"vs_vwap_pct": 0.5, "by_move": True}, {}, {})
    rows += [_done_row(x, vs_vwap=2.0, win=True) for x in "DEFG"]
    assert not {r["key"]: r for r in learning.evaluate(rows)}["far_above_vwap"]["active"]


def test_learned_rule_turns_a_buy_into_wait_and_day_log_writes_lessons(tmp_path):
    broker = FakeBroker({"NVDA": ramp_bars(), "SPY": ramp_bars(end=100.3, ramp_volume=1000)}, news={"NVDA": fresh_news("NVDA")})
    desk, clock = make_desk(tmp_path, broker, watchlist=("NVDA",))
    for symbol in ("X1", "X2", "X3"):
        pid = desk.store.create_proposal("2026-11-03", symbol, {"symbol": symbol, "move_pct": 3.4, "vs_vwap_pct": 1.9, "by_move": True, "ts": "2026-11-03T11:00:00-05:00"})
        desk.store.update_proposal(pid, status="closed", news={"source": "rules"}, chart={"stop_pct": 0.8}, fill_price=100.0, exit_price=99.0, pnl=-10.0, exit_reason="stop", ruling="BUY")
    assert any(r["active"] for r in desk.learned())
    desk.tick()
    row = desk.store.proposals_on(DAY.isoformat())[0]
    assert row["ruling"] == "WAIT" and row["skeptic"].get("learned")
    assert "Lært af fejl" in " ".join(row["skeptic"]["reasons"])
    assert broker.submitted == []
    status = desk.status()
    assert "learning" in status and "paper_month" not in status

    broker.open = False
    clock["now"] = at(16, 15)
    desk.tick()
    lessons = desk.store.get_json("lessons")
    assert lessons and lessons[0]["day"] == DAY.isoformat()
    assert any(m["kind"] == "lesson" and m["text"].startswith("Lært i dag") for m in desk.store.messages(limit=500))


def test_keys_from_the_page_are_tested_saved_and_used(tmp_path, monkeypatch):
    from trader.desk import engine as desk_engine
    from trader.desk.alpaca import BrokerError

    seen = []

    class FakePaper:
        def __init__(self, key, secret):
            seen.append((key, secret))
            self.key = key

        def get_account(self):
            if self.key != "PKGOODKEY":
                raise BrokerError("Alpaca afviser nøglerne ved handelskontoen (401: unauthorized)")
            return Account(equity=100_000.0, cash=100_000.0, buying_power=200_000.0, daytrade_count=0)

        def close(self):
            pass

    monkeypatch.setattr(desk_engine, "AlpacaPaper", FakePaper)
    cfg = DeskSettings(db_path=str(tmp_path / "desk.db"), alpaca_api_key="PKRAILWAY", alpaca_secret_key="s" * 40, key_source="DESK_ALPACA_API_KEY", secret_source="DESK_ALPACA_SECRET_KEY")
    desk = DeskEngine(cfg, _settings(tmp_path), store=DeskStore(cfg.db_path), background_replies=False)

    bad = desk.test_keys("PKWRONG", "x" * 40, save=True)
    assert not bad["ok"] and not bad["saved"] and desk.keys()[0] == "PKRAILWAY"
    ok = desk.test_keys(' "PKGOODKEY" ', "y" * 40, save=True)
    assert ok["ok"] and ok["saved"] and desk.keys()[:3] == ("PKGOODKEY", "y" * 40, "gemt på siden")
    desk.broker()
    assert seen[-1] == ("PKGOODKEY", "y" * 40)
    assert "PKGOODKEY" not in str(desk.status())  # nøglen vises aldrig
    desk.forget_keys()
    assert desk.keys()[0] == "PKRAILWAY"
    with pytest.raises(ValueError):
        desk.test_keys("", "", save=False)


def test_quotes_also_arrive_when_the_market_is_closed(tmp_path):
    broker = FakeBroker({"NVDA": ramp_bars()})
    broker.open = False
    broker.get_snapshots = lambda symbols: {
        s: {"price": 105.0, "prev_close": 100.0, "day_open": 101.0, "day_volume": 780_000.0, "vwap": 104.0, "ts": "x"}
        for s in symbols if s in ("NVDA", "SPY")
    }
    desk, clock = make_desk(tmp_path, broker)
    clock["now"] = at(18, 0)
    desk.tick()
    quotes = desk.status()["quotes"]
    assert quotes["mode"] == "snapshot"
    nvda = next(q for q in quotes["items"] if q["symbol"] == "NVDA")
    assert nvda["day_pct"] == 5.0 and nvda["volume_x"] == 2.0 and nvda["move_pct"] is None and len(nvda["spark"]) >= 2
    # Ny runde inden for 10 minutter henter ikke igen.
    calls = []
    broker.get_snapshots = lambda symbols: calls.append(1) or {}
    clock["now"] = at(18, 5)
    desk.tick()
    assert calls == []
    clock["now"] = at(18, 11)
    desk.tick()
    assert calls == [1]


def test_snapshot_parsing_handles_alpaca_shapes():
    from trader.desk.alpaca import AlpacaPaper

    client = AlpacaPaper.__new__(AlpacaPaper)
    body = {
        "AAPL": {"latestTrade": {"p": 190.5, "t": "2026-11-06T20:59:59Z"}, "dailyBar": {"o": 188, "c": 190.4, "v": 1000, "vw": 189.9}, "prevDailyBar": {"c": 187.0}},
        "BAD": None,
        "EMPTY": {"latestTrade": {}},
    }
    client._request = lambda *a, **k: body
    snaps = client.get_snapshots(["AAPL", "BAD", "EMPTY"])
    assert list(snaps) == ["AAPL"] and snaps["AAPL"]["price"] == 190.5 and snaps["AAPL"]["prev_close"] == 187.0
    client._request = lambda *a, **k: {"snapshots": body}
    assert client.get_snapshots(["AAPL"])["AAPL"]["vwap"] == 189.9


def test_executor_rule_follows_autopilot_and_reaches_claude(tmp_path):
    from trader.desk.agents import bot_rules

    on = bot_rules("executor", DeskSettings(auto_approve=True))
    off = bot_rules("executor", DeskSettings(auto_approve=False))
    assert "Autopiloten er slået til" in on and "uden min godkendelse" in off
    desk, _ = make_desk(tmp_path, FakeBroker({}), auto_approve=False)
    desk.update_params({"auto_approve": True})
    rules_text = next(b["rules"] for b in desk.status()["bots"] if b["key"] == "executor")
    assert "Autopiloten er slået til" in rules_text
    desk.post_message("@executor hej")
    assert desk.brain.settings.auto_approve is True


def test_claude_problems_are_explained_and_fallback_answers_are_useful(tmp_path):
    from trader.desk.agents import DeskBrain, explain_error

    class ApiError(Exception):
        def __init__(self, status, message):
            super().__init__(message)
            self.status_code, self.message = status, message

    assert "401" in explain_error(ApiError(401, "invalid x-api-key"), "m")
    assert "kredit" in explain_error(ApiError(400, "Your credit balance is too low"), "m")
    assert "findes ikke" in explain_error(ApiError(404, "model: claude-x"), "claude-x")

    desk, _ = make_desk(tmp_path, FakeBroker({}))
    desk.post_message("@news hvad sker der?")
    last = desk.store.messages(limit=1)[-1]
    assert last["author"] == "news" and "ANTHROPIC_API_KEY mangler" in last["text"]
    assert "Du er NEWS" not in last["text"]  # ikke regelteksten

    brain = DeskBrain(DeskSettings(), desk.store, api_key="sk-ant-test")
    assert brain.configured and not brain.limited and brain.available(10)

    def boom():
        raise ApiError(401, "invalid x-api-key")

    class Client:
        class beta:
            class messages:
                @staticmethod
                def create(**kwargs):
                    boom()

    brain._client = lambda: Client()
    result = brain.test()
    assert not result["ok"] and "401" in result["text"] and brain.last_error
    desk.brain = brain
    status = desk.status()
    assert "401" in status["ai"]["reason"] and any("Claude" in a["text"] for a in status["alerts"])


def test_status_never_waits_for_alpaca(tmp_path):
    broker = FakeBroker({"NVDA": ramp_bars()})
    desk, _ = make_desk(tmp_path, broker)
    desk.tick()
    calls = []

    def slow(*args, **kwargs):
        calls.append(1)
        raise AssertionError("status må ikke kalde Alpaca")

    broker.get_clock = broker.get_account = broker.get_positions = slow
    status = desk.status()
    assert calls == [] and status["account"]["equity"] == 100_000.0 and status["market"]["source"] == "alpaca"
    # Uden et frisk ur bruges kalenderen, stadig uden netværk.
    desk._cache.pop("clock")
    assert desk.status()["market"]["source"] == "kalender" and calls == []


def test_status_errors_come_back_as_readable_json(tmp_path):
    settings = _settings(tmp_path)
    desk, _ = make_desk(tmp_path, FakeBroker({}))
    desk.start = lambda: None
    desk.status = lambda after=0: (_ for _ in ()).throw(RuntimeError("database er låst"))
    with TestClient(create_app(settings=settings, meme_engine=quiet_engine(), desk_engine=desk)) as client:
        assert _login(client).status_code == 303
        response = client.get("/api/desk/status")
        assert response.status_code == 500 and "database er låst" in response.json()["detail"]


# ---------------------------------------------------------------------- Claude uden kredit
class _ClaudeError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status
        self.message = message


class _FailingClaude:
    def __init__(self, exc: Exception) -> None:
        self.exc, self.calls = exc, 0
        self.beta = self
        self.messages = self

    def parse(self, **_kwargs):
        self.calls += 1
        raise self.exc

    create = parse


def test_no_credit_rests_claude_for_a_while_and_the_rules_take_over(tmp_path):
    from trader.desk.agents import DeskBrain

    cfg = replace(DeskSettings(), db_path=str(tmp_path / "desk.db"))
    brain = DeskBrain(cfg, DeskStore(cfg.db_path), "sk-ant-test")
    claude = _FailingClaude(_ClaudeError(400, "Your credit balance is too low to access the Anthropic API."))
    brain._client = lambda: claude
    headlines = [{"headline": "NVDA lands a big new contract", "created_at": datetime.now(ET).isoformat(), "url": "https://example.com"}]
    first = brain.news({"symbol": "NVDA"}, headlines)
    assert claude.calls == 1 and first["source"] == "rules" and first["has_reason"]
    assert brain.resting and "ingen kredit" in brain.last_error and 14 <= brain.rest_minutes() <= 15
    # Under pausen kaldes Claude slet ikke: reglerne svarer med det samme.
    assert brain.news({"symbol": "NVDA"}, headlines)["source"] == "rules"
    assert brain.skeptic({"symbol": "NVDA"}, {"has_reason": True}, {}, {}) is None
    assert claude.calls == 1
    # Test Claude prøver altid, og et svar uden fejl slutter pausen.
    brain._ok()
    assert not brain.resting and brain.last_error == ""


def test_a_passing_claude_problem_does_not_pause_it(tmp_path):
    from trader.desk.agents import DeskBrain

    cfg = replace(DeskSettings(), db_path=str(tmp_path / "desk.db"))
    brain = DeskBrain(cfg, DeskStore(cfg.db_path), "sk-ant-test")
    claude = _FailingClaude(_ClaudeError(429, "rate_limit_error"))
    brain._client = lambda: claude
    brain.news({"symbol": "NVDA"}, [])
    brain.news({"symbol": "NVDA"}, [])
    assert claude.calls == 2 and not brain.resting


def test_status_says_the_desk_trades_on_without_claude(tmp_path):
    desk, _ = make_desk(tmp_path, FakeBroker({}))
    desk.brain.api_key = "sk-ant-test"
    desk.brain._failed(_ClaudeError(400, "Your credit balance is too low"))
    status = desk.status()
    assert status["ai"]["resting"] and status["ai"]["retry_minutes"] >= 14
    claude = [a for a in status["alerts"] if a["text"].startswith("Claude:")]
    assert claude and claude[0]["level"] == "warning" and "bordet handler videre" in claude[0]["text"]


def test_without_claude_a_headline_from_last_night_still_counts():
    from trader.desk.agents import DeskBrain

    now = datetime.now(ET)
    evening = [{"headline": "NVDA wins a big contract", "created_at": (now - timedelta(hours=14)).isoformat(), "url": "https://example.com"}]
    assert DeskBrain.rules_news(evening)["has_reason"]
    old = [{"headline": "NVDA old story", "created_at": (now - timedelta(hours=20)).isoformat(), "url": "https://example.com"}]
    news = DeskBrain.rules_news(old)
    assert not news["has_reason"] and "18 timer" in news["reason"]
