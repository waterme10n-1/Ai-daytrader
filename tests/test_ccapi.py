"""Command Center Projekt-API v1 (/cc/v1/*): token, kontraktens form, tal der passer med motorerne, paginering
og ingen sideeffekter. Ingen netværk: memebotten kører på falske kilder, bordet på falske mæglere."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from meme_fakes import quiet_engine
from test_auth import _login, _settings
from test_desk import FakeBroker, fresh_news, make_desk, ramp_bars
from test_desk_dk import FakeYahoo, make_dk, ramp

from trader.app import create_app
from trader.ccapi import sanitise
from trader.meme.store import now_iso

TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ENDPOINTS = ("/cc/v1/status", "/cc/v1/performance", "/cc/v1/equity?book=meme", "/cc/v1/ledger", "/cc/v1/metrics")


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def seed_position(engine, *, symbol: str, cost: float = 3.0, entry: float = 0.5, fees: float = 0.02, opened: datetime | None = None,
                  closed: datetime | None = None, pnl: float | None = None, proceeds: float = 0.0, ratio: float = 1.0,
                  remaining: int = 1000, strategy: str = "momentum") -> int:
    opened = opened or datetime.now(timezone.utc) - timedelta(hours=2)
    position_id = engine.store.open_position(
        {
            "strategy": strategy,
            "mode": engine.mode,
            "mint": f"{symbol}Mint" + "x" * 32,
            "symbol": symbol,
            "source": "dex_boost",
            "status": "open",
            "opened_at": iso(opened),
            "entry_price_usd": entry,
            "cost_usd": cost,
            "token_raw": 1000,
            "remaining_raw": remaining,
            "fees_usd": fees,
            "last_price_usd": entry * ratio,
        }
    )
    changes = {"last_ratio": ratio, "proceeds_usd": proceeds}
    if closed is not None:
        changes.update(status="closed", closed_at=iso(closed), pnl_usd=pnl, pnl_pct=(pnl or 0) / cost, exit_reason="target", remaining_raw=0)
    engine.store.update_position(position_id, **changes)
    return position_id


def seeded_meme():
    engine = quiet_engine()
    now = datetime.now(timezone.utc)
    seed_position(engine, symbol="WIN", closed=now - timedelta(hours=1), pnl=1.2, proceeds=4.22)
    seed_position(engine, symbol="LOSS", closed=now - timedelta(minutes=50), pnl=-0.8, proceeds=2.18, fees=0.04)
    seed_position(engine, symbol="OPEN", ratio=1.5)  # åben, +50 %
    seed_position(engine, symbol="HALF", ratio=0.8, remaining=500, proceeds=1.7, fees=0.03)  # halvt solgt
    engine.store.set_state("failed_fees:paper", repr(0.05))
    return engine


def client_for(tmp_path, *, token: str = TOKEN, meme=None, desk=None, desk_dk=None, **overrides) -> TestClient:
    settings = _settings(tmp_path, cc_api_token=token, **overrides)
    for engine in (desk, desk_dk):
        if engine is not None:
            engine.start = lambda: None  # testen styrer selv minutterne
    app = create_app(settings=settings, meme_engine=meme or quiet_engine(), desk_engine=desk, desk_dk_engine=desk_dk)
    return TestClient(app)


def us_desk(tmp_path):
    """Én lukket handel med gevinst og én åben handel på Alpacas (falske) papirkonto."""
    broker = FakeBroker({"NVDA": ramp_bars(), "AAPL": ramp_bars(end=100.2, ramp_volume=1000), "SPY": ramp_bars(end=100.3, ramp_volume=1000)},
                        news={"NVDA": fresh_news("NVDA")})
    desk, clock = make_desk(tmp_path / "us", broker)
    desk.tick()
    first = desk.store.proposals_with_status("awaiting")[0]
    desk.approve(first["id"])
    broker.hit("NVDA", "target")
    from test_desk import at

    clock["now"] = at(12, 0)
    desk.tick()
    # En åben handel bagefter (sat direkte, så testen ikke afhænger af SCOUT's regler).
    pid = desk.store.create_proposal("2026-11-04", "AAPL", {})
    desk.store.update_proposal(pid, status="placed", qty=10, fill_price=100.0, entry=100.0, order_id="o-open",
                               decided_at=at(12, 1).isoformat(timespec="seconds"))
    desk._quotes = {"ts": at(12, 2).isoformat(timespec="seconds"), "mode": "snapshot", "items": [{"symbol": "AAPL", "price": 101.5}]}
    return desk, broker, first["id"]


def dk_desk(tmp_path):
    data = FakeYahoo({"NOVO-B": ramp(), "DSV": ramp(end=100.2, ramp_volume=1000), "^OMXC25": ramp(end=100.4, ramp_volume=1000)})
    from test_desk_dk import at, fresh

    data.news_items = {"NOVO-B": fresh("NOVO-B")}
    desk, paper, clock = make_dk(tmp_path, data)
    desk.tick()
    row = desk.store.proposals_on("2026-11-04")[0]
    assert row["status"] == "placed"
    target = float(row["target"])
    data.intraday["NOVO-B"] = ramp()[:150] + ramp(start=103.5, end=target + 0.5, ramp_from=(11, 30), ramp_to=(11, 40))[150:]
    clock["now"] = at(11, 50)
    desk.tick()
    assert desk.store.proposal(row["id"])["status"] == "closed"
    # Og en åben handel bagefter, købt gennem papirkontoen.
    data.intraday["DSV"] = ramp(start=100.0, end=100.0)
    order = paper.submit_bracket("DSV", "long", 7, 99.0, 102.0)
    pid = desk.store.create_proposal("2026-11-04", "DSV", {})
    desk.store.update_proposal(pid, status="placed", qty=7, fill_price=order.filled_avg_price, order_id=order.id,
                               decided_at=at(11, 50).isoformat(timespec="seconds"))
    desk.tick(force=True)
    return desk, paper


# ---------------------------------------------------------------------- adgang


def test_api_is_off_without_a_token_or_with_a_short_one(tmp_path):
    for token in ("", "kort-token"):
        with client_for(tmp_path, token=token) as client:
            for path in ENDPOINTS:
                assert client.get(path, headers={"Authorization": f"Bearer {token or 'x'}"}).status_code == 404, path
            assert client.post("/cc/v1/status", headers=AUTH).status_code == 404


def test_bearer_token_is_required_and_compared_exactly(tmp_path):
    with client_for(tmp_path) as client:
        for headers in ({}, {"Authorization": "Bearer forkert"}, {"Authorization": f"Basic {TOKEN}"}, {"Authorization": f"Bearer {TOKEN}x"},
                        {"Authorization": "Bearer"}):
            response = client.get("/cc/v1/status", headers=headers)
            assert response.status_code == 401, headers
            assert response.headers["www-authenticate"] == "Bearer"
        for path in ENDPOINTS:
            ok = client.get(path, headers=AUTH)
            assert ok.status_code == 200, path
            assert ok.headers["content-type"].startswith("application/json") and ok.headers["cache-control"] == "no-store"


def test_token_and_login_stay_separate(tmp_path):
    """Login er slået til: tokenet åbner kun /cc/v1, og en session åbner ikke /cc/v1."""
    with client_for(tmp_path) as client:
        assert client.get("/cc/v1/status", headers=AUTH).status_code == 200  # ingen session, ingen CSRF
        assert client.get("/api/meme/status", headers=AUTH).status_code == 401  # tokenet er ikke et login
        assert client.post("/api/meme/kill", headers=AUTH).status_code == 401
        assert _login(client).status_code == 303
        assert client.get("/api/meme/status").status_code == 200
        assert client.get("/cc/v1/status").status_code == 401  # sessionen alene giver ikke adgang
        assert client.get("/cc/v1/status", headers=AUTH).status_code == 200


def test_only_get_is_allowed(tmp_path):
    meme = seeded_meme()
    with client_for(tmp_path, meme=meme) as client:
        for method in ("post", "put", "patch", "delete"):
            response = getattr(client, method)("/cc/v1/status", headers=AUTH)
            assert response.status_code == 405, method
        assert meme.kill_switch is False and meme.paused is False


def test_sanitise_hides_addresses_keys_and_urls():
    text = sanitise(
        "RPC fejl: https://mainnet.helius-rpc.com/?api-key=abc123 wallet 7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU "
        "api_key=hemmelig PKABCDEFGHIJKLMNOPQR sig " + "ab" * 20
    )
    assert "helius" not in text and "abc123" not in text and "7xKXtg2" not in text and "hemmelig" not in text
    assert "PKABCDEF" not in text and "abab" not in text
    assert text.startswith("RPC fejl:")
    assert len(sanitise("x " * 500)) <= 240


# ---------------------------------------------------------------------- memebotten


def test_meme_performance_matches_the_dashboard(tmp_path):
    meme = seeded_meme()
    with client_for(tmp_path, meme=meme) as client:
        body = client.get("/cc/v1/performance", headers=AUTH).json()
    assert body["as_of"].endswith("Z")
    book = {item["key"]: item for item in body["books"]}["meme"]
    kpis = meme.status()["kpis"]
    assert book["currency"] == "USD" and book["mode"] == "paper"
    assert book["starting_capital"] == pytest.approx(kpis["bankroll"])
    assert book["equity"] == pytest.approx(kpis["equity"], abs=1e-4)
    assert book["realized_pnl"] == pytest.approx(kpis["realized"], abs=1e-4)
    assert book["realized_pnl"] == pytest.approx(1.2 - 0.8 - 0.05)
    assert book["unrealized_pnl"] == pytest.approx(kpis["unrealized"], abs=1e-4)
    assert book["fees"] == pytest.approx(kpis["fees_total"] + 0.05)  # gebyrer på mislykkede køb tæller med
    assert book["open_positions"] == kpis["open"] == 2
    assert book["open_exposure"] == pytest.approx(3.0 * 1.5 + 3.0 * 0.5 * 0.8)
    assert book["cash"] + book["open_exposure"] == pytest.approx(book["equity"])
    assert book["equity"] == pytest.approx(book["starting_capital"] + book["realized_pnl"] + book["unrealized_pnl"])
    for key in ("starting_capital", "deposits", "withdrawals", "cash", "open_exposure", "equity", "realized_pnl", "unrealized_pnl", "fees"):
        assert isinstance(book[key], float), key


def test_meme_ledger_has_open_and_closed_trades_in_contract_form(tmp_path):
    meme = seeded_meme()
    with client_for(tmp_path, meme=meme) as client:
        body = client.get("/cc/v1/ledger?book=meme", headers=AUTH).json()
    assert body["next_cursor"] is None
    items = {item["symbol"]: item for item in body["items"]}
    assert set(items) == {"WIN", "LOSS", "OPEN", "HALF"}
    stamps = [(item["updated_at"], item["id"]) for item in body["items"]]
    assert stamps == sorted(stamps)  # ældste først
    win = items["WIN"]
    assert win["type"] == "trade" and win["book"] == "meme" and win["market"] == "SOL" and win["side"] == "long"
    assert win["status"] == "closed" and win["closed_at"].endswith("Z") and win["pnl"] == pytest.approx(1.2)
    assert win["quantity"] == pytest.approx(6.0) and win["entry_price"] == pytest.approx(0.5)
    assert win["exit_price"] == pytest.approx(4.22 / 6.0) and win["mark_price"] is None and win["unrealized_pnl"] == 0.0
    assert win["fees"] == pytest.approx(0.02) and win["id"].startswith("meme-")
    opened = items["OPEN"]
    assert opened["status"] == "open" and opened["closed_at"] is None and opened["pnl"] == 0.0
    assert opened["mark_price"] == pytest.approx(0.75) and opened["exit_price"] is None
    assert opened["unrealized_pnl"] == pytest.approx(3.0 * 1.5 - 3.0 - 0.02)
    half = items["HALF"]
    assert half["remaining_quantity"] == pytest.approx(3.0)
    open_sum = sum(item["unrealized_pnl"] for item in body["items"])
    assert open_sum == pytest.approx(meme._totals()["unrealized"])
    assert sum(item["pnl"] for item in body["items"]) == pytest.approx(1.2 - 0.8)
    for item in body["items"]:
        for key in ("id", "type", "book", "status", "opened_at", "closed_at", "updated_at", "symbol", "market", "side",
                    "quantity", "entry_price", "exit_price", "mark_price", "fees", "pnl", "unrealized_pnl", "strategy"):
            assert key in item, key
        assert "mint" not in item and "signatures" not in item  # ingen adresser


def test_meme_equity_uses_stored_snapshots_and_reconstructs_without_them(tmp_path):
    meme = seeded_meme()
    with client_for(tmp_path, meme=meme) as client:
        before_now = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rebuilt = client.get(f"/cc/v1/equity?book=meme&to={before_now}", headers=AUTH).json()
        assert rebuilt["reconstructed"] is True and rebuilt["currency"] == "USD" and rebuilt["book"] == "meme"
        assert rebuilt["points"][-1]["equity"] == pytest.approx(15.0 + 1.2 - 0.8)  # start + realiseret, uden gebyrer for fejlede køb
        assert {round(point["equity"], 4) for point in rebuilt["points"]} <= {15.0, 16.2, 15.4}
        meme.store.add_equity("paper", 15.4)
        meme.store.add_equity("paper", 15.9)
        stored = client.get("/cc/v1/equity?book=meme&interval=hour", headers=AUTH).json()
        assert stored["reconstructed"] is False
        last = stored["points"][-1]
        assert last["equity"] == pytest.approx(meme._totals()["equity"], abs=1e-6) and last["cash"] is not None
        assert all(point["t"].endswith("Z") for point in stored["points"])
        days = client.get("/cc/v1/equity?book=meme&interval=day", headers=AUTH).json()
        assert len(days["points"]) == 1  # alt er fra i dag
        future = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert client.get(f"/cc/v1/equity?book=meme&from={future}", headers=AUTH).json()["points"] == []
        assert client.get("/cc/v1/equity?book=ukendt", headers=AUTH).status_code == 404
        assert client.get("/cc/v1/equity?book=meme&interval=week", headers=AUTH).status_code == 400
        assert client.get("/cc/v1/equity?book=meme&from=igaar", headers=AUTH).status_code == 400


def test_ledger_pagination_and_since(tmp_path):
    meme = quiet_engine()
    start = datetime.now(timezone.utc) - timedelta(days=3)
    for index in range(7):
        moment = start + timedelta(hours=index)
        seed_position(meme, symbol=f"T{index}", opened=moment - timedelta(minutes=5), closed=moment, pnl=0.1 * index, proceeds=3.0)
    with client_for(tmp_path, meme=meme) as client:
        seen, cursor, pages = [], None, 0
        while True:
            url = "/cc/v1/ledger?limit=3" + (f"&cursor={cursor}" if cursor else "")
            body = client.get(url, headers=AUTH).json()
            pages += 1
            seen.extend(item["symbol"] for item in body["items"])
            cursor = body["next_cursor"]
            if cursor is None:
                break
        assert pages == 3 and seen == [f"T{i}" for i in range(7)]
        since = (start + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
        later = client.get(f"/cc/v1/ledger?since={since}", headers=AUTH).json()["items"]
        assert [item["symbol"] for item in later] == ["T4", "T5", "T6"]
        assert client.get("/cc/v1/ledger?cursor=%%%", headers=AUTH).status_code == 400
        assert client.get("/cc/v1/ledger?cursor=bm9nZXQ", headers=AUTH).status_code == 400
        assert len(client.get("/cc/v1/ledger?limit=5000", headers=AUTH).json()["items"]) == 7  # højst 1000
        assert client.get("/cc/v1/ledger?book=ukendt", headers=AUTH).status_code == 404


# ---------------------------------------------------------------------- status


def test_status_follows_the_engines(tmp_path):
    meme = seeded_meme()
    meme.store.add_event("error", "Fejl i runden (1): https://mainnet.helius-rpc.com/?api-key=hemmelig svarede ikke")
    meme.store.add_event("error", "Fejl i runden (1): https://mainnet.helius-rpc.com/?api-key=hemmelig svarede ikke")
    meme.store.add_event("info", "Ny dag.")
    with client_for(tmp_path, meme=meme) as client:
        body = client.get("/cc/v1/status", headers=AUTH).json()
        assert body["api_version"] == 1 and body["project"]["key"] == "ai-daytrading" and body["project"]["kind"] == "trading"
        assert body["project"]["version"]
        assert body["status"] == "stopped"  # testens motor har ingen baggrundstråd
        assert body["mode"] == "paper" and body["started_at"].endswith("Z")
        assert body["books"] == [{**body["books"][0], "key": "meme", "name": "Memebot (Solana)", "currency": "USD", "mode": "paper"}]
        errors = body["errors"]
        assert len(errors) == 1 and errors[0]["count"] == 2 and errors[0]["severity"] == "error"
        assert "hemmelig" not in errors[0]["message"] and "helius" not in errors[0]["message"]

        meme._thread = type("Alive", (), {"is_alive": lambda self: True})()
        meme.last_tick = now_iso()
        running = client.get("/cc/v1/status", headers=AUTH).json()
        assert running["status"] == "running" and running["last_activity_at"].endswith("Z") and running["message"]
        meme.paused = True
        assert client.get("/cc/v1/status", headers=AUTH).json()["status"] == "degraded"
        meme.paused = False
        meme.kill_switch = True
        stopped = client.get("/cc/v1/status", headers=AUTH).json()
        assert stopped["status"] == "stopped" and any("nødstop" in item["message"] for item in stopped["errors"])
        meme.kill_switch = False
        meme.consecutive_errors, meme.last_error = 6, "Ingen SOL-kurs fra Jupiter eller DexScreener."
        broken = client.get("/cc/v1/status", headers=AUTH).json()
        assert broken["status"] == "error" and broken["errors"][0]["severity"] == "critical"


def test_metrics_shape(tmp_path):
    with client_for(tmp_path, meme=seeded_meme()) as client:
        body = client.get("/cc/v1/metrics", headers=AUTH).json()
    keys = {item["key"]: item for item in body["metrics"]}
    assert keys["meme.trades_today"]["value"] >= 0 and keys["meme.pnl_today"]["unit"] == "USD"
    assert keys["meme.win_rate"]["value"] == pytest.approx(0.5)
    assert all({"key", "label", "value", "unit"} <= set(item) for item in body["metrics"])


# ---------------------------------------------------------------------- handelsbordet


def test_us_desk_numbers_come_from_the_desk(tmp_path):
    desk, broker, closed_id = us_desk(tmp_path)
    with client_for(tmp_path, desk=desk) as client:
        status = client.get("/cc/v1/status", headers=AUTH).json()
        books = {item["key"]: item for item in status["books"]}
        assert books["desk-us"]["currency"] == "USD" and books["desk-us"]["mode"] == "paper"
        perf = {item["key"]: item for item in client.get("/cc/v1/performance", headers=AUTH).json()["books"]}["desk-us"]
        ledger = client.get("/cc/v1/ledger?book=desk-us", headers=AUTH).json()["items"]
        equity = client.get("/cc/v1/equity?book=desk-us", headers=AUTH).json()
    closed = desk.store.proposal(closed_id)
    assert perf["equity"] == pytest.approx(broker.equity) and perf["source"] == "alpaca-cache"
    assert perf["realized_pnl"] == pytest.approx(closed["pnl"]) and closed["pnl"] > 0
    assert perf["unrealized_pnl"] == pytest.approx(15.0) and perf["open_positions"] == 1 and perf["fees"] == 0.0
    assert perf["starting_capital"] == pytest.approx(desk.store.get_json("desk_start")["equity"])
    by_status = {item["status"]: item for item in ledger}
    done = by_status["closed"]
    assert done["id"] == f"desk-us-{closed_id}" and done["market"] == "US" and done["symbol"] == "NVDA"
    assert done["pnl"] == pytest.approx(closed["pnl"]) and done["exit_price"] == pytest.approx(closed["exit_price"])
    assert done["entry_price"] == pytest.approx(closed["fill_price"]) and done["quantity"] == closed["qty"]
    live = by_status["open"]
    assert live["symbol"] == "AAPL" and live["mark_price"] == pytest.approx(101.5) and live["unrealized_pnl"] == pytest.approx(15.0)
    assert equity["reconstructed"] is False and equity["points"][-1]["equity"] == pytest.approx(broker.equity)


def test_danish_desk_numbers_follow_its_own_paper_account(tmp_path):
    desk, paper = dk_desk(tmp_path)
    with client_for(tmp_path, desk_dk=desk) as client:
        perf = {item["key"]: item for item in client.get("/cc/v1/performance", headers=AUTH).json()["books"]}["desk-dk"]
        ledger = client.get("/cc/v1/ledger?book=desk-dk", headers=AUTH).json()["items"]
        status = client.get("/cc/v1/status", headers=AUTH).json()
    account = paper.get_account()
    page = desk.status()
    assert perf["currency"] == "DKK" and perf["mode"] == "paper"
    assert perf["starting_capital"] == pytest.approx(page["ledger"]["start"])
    assert perf["cash"] == pytest.approx(account.cash) and perf["equity"] == pytest.approx(account.equity, abs=0.02)
    assert perf["equity"] == pytest.approx(page["account"]["equity"], abs=0.02)
    pnl = sum(row["pnl"] for row in desk.store.closed_trades())
    assert perf["realized_pnl"] == pytest.approx(pnl, abs=0.02)
    assert perf["equity"] == pytest.approx(perf["starting_capital"] + perf["realized_pnl"] + perf["unrealized_pnl"], abs=0.02)
    assert perf["fees"] > 0 and perf["open_positions"] == 1
    closed = [item for item in ledger if item["status"] == "closed"][0]
    # Kurser uden kurtage, kurtagen for sig og resultatet efter kurtage.
    assert closed["market"] == "DK" and closed["fees"] > 0
    assert closed["pnl"] == pytest.approx((closed["exit_price"] - closed["entry_price"]) * closed["quantity"] - closed["fees"], abs=0.02)
    assert sum(item["fees"] for item in ledger) == pytest.approx(perf["fees"], abs=1e-6)
    assert {book["key"] for book in status["books"]} == {"meme", "desk-dk"}


def test_desk_equity_reconstructs_from_closed_trades_without_snapshots(tmp_path):
    desk, _broker, closed_id = us_desk(tmp_path)
    for key in desk.store.states_with_prefix("day_start:"):
        desk.store._write("DELETE FROM state WHERE key = ?", (key,))
    with client_for(tmp_path, desk=desk) as client:
        body = client.get("/cc/v1/equity?book=desk-us&interval=day", headers=AUTH).json()
    assert body["reconstructed"] is True and body["points"]


# ---------------------------------------------------------------------- ingen sideeffekter


def _dump(conn: sqlite3.Connection) -> dict:
    tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    return {table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in tables}


def test_reading_the_api_changes_nothing(tmp_path):
    meme = seeded_meme()
    us, _broker, _ = us_desk(tmp_path)
    dk, _paper = dk_desk(tmp_path / "dk")
    before = (_dump(meme.store._conn), _dump(us.store._conn), _dump(dk.store._conn))
    flags = (meme.kill_switch, meme.paused, meme.mode, us.paused, dk.paused, us.last_tick, dk.last_tick, meme.last_tick)
    with client_for(tmp_path, meme=meme, desk=us, desk_dk=dk) as client:
        for path in (*ENDPOINTS, "/cc/v1/equity?book=desk-us", "/cc/v1/equity?book=desk-dk", "/cc/v1/ledger?limit=1"):
            assert client.get(path, headers=AUTH).status_code == 200, path
        assert client.post("/cc/v1/ledger", headers=AUTH).status_code == 405
    after = (_dump(meme.store._conn), _dump(us.store._conn), _dump(dk.store._conn))
    assert before == after
    assert flags == (meme.kill_switch, meme.paused, meme.mode, us.paused, dk.paused, us.last_tick, dk.last_tick, meme.last_tick)
