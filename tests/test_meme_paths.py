"""Mini-grafer på Hjem: motoren gemmer kursforløbet for åbne positioner, højst ét punkt hvert 20. sekund."""

from __future__ import annotations

import pytest

from meme_fakes import make_engine, raw_pair, safe_report
from trader.meme import engine as engine_module

MINT = "PathMint1111"


def _open_position():
    engine, dex, rug, jup, notes = make_engine()
    dex.pairs[MINT] = raw_pair(MINT)
    dex.boosts.append(MINT)
    rug.reports[MINT] = safe_report()
    engine.tick(force_scan=True)
    pos = engine.store.open_positions("paper")[0]
    return engine, dex, pos


def _view(engine, pos_id):
    return [p for p in engine.status()["positions"] if p["id"] == pos_id][0]


def test_open_position_has_a_throttled_price_path(monkeypatch):
    engine, dex, pos = _open_position()
    clock = [1_000_000.0]
    monkeypatch.setattr(engine_module.time, "time", lambda: clock[0])
    engine._record_path(pos["id"], 1.1)
    engine._record_path(pos["id"], 1.3)  # samme øjeblik: springes over
    clock[0] += engine_module.PATH_STEP_SECONDS
    engine._record_path(pos["id"], 1.2)
    path = _view(engine, pos["id"])["path"]
    assert path[0] == 1.0 and path[-2:] == [pytest.approx(1.1), pytest.approx(1.2)]
    assert 1.3 not in path


def test_path_is_capped_and_dropped_when_the_position_closes(monkeypatch):
    engine, dex, pos = _open_position()
    clock = [2_000_000.0]
    monkeypatch.setattr(engine_module.time, "time", lambda: clock[0])
    for i in range(engine_module.PATH_POINTS + 25):
        clock[0] += engine_module.PATH_STEP_SECONDS
        engine._record_path(pos["id"], 1 + i / 1000)
    assert len(_view(engine, pos["id"])["path"]) == engine_module.PATH_POINTS
    engine.sell_position(pos["id"])
    assert pos["id"] not in engine._paths
    assert engine.store.get_position(pos["id"])["status"] != "open"


def test_path_survives_a_restart(monkeypatch):
    engine, dex, pos = _open_position()
    clock = [3_000_000.0]
    monkeypatch.setattr(engine_module.time, "time", lambda: clock[0])
    for ratio in (1.05, 1.1, 0.97):
        clock[0] += engine_module.PATH_STEP_SECONDS
        engine._record_path(pos["id"], ratio)
    engine._paths.clear()  # som efter en genstart: hukommelsen er tom, databasen er der stadig
    engine._path_at.clear()
    assert _view(engine, pos["id"])["path"] == [1.0, pytest.approx(1.05), pytest.approx(1.1), pytest.approx(0.97)]
    engine.sell_position(pos["id"])
    assert engine.store.path_points(pos["id"], 10) == []


def test_clock_tells_the_dashboard_what_happens_next():
    engine, dex, pos = _open_position()
    clock = engine.status()["engine"]["clock"]
    scan = clock["jobs"]["scan"]
    assert scan["enabled"] and not scan["running"] and scan["runs"] >= 1
    assert scan["next_at"] == pytest.approx(engine._last_scan + engine.settings.scan_seconds)
    assert clock["phase"] == "venter" and clock["day_ends_at"] > clock["now"]
    view = _view(engine, pos["id"])
    assert view["priced_at"] is None or view["priced_at"] <= clock["now"]


def test_loop_wakes_exactly_when_the_scan_is_due(monkeypatch):
    engine, dex, pos = _open_position()
    clock = [5_000_000.0]
    monkeypatch.setattr(engine_module.time, "time", lambda: clock[0])
    engine._last_monitor = clock[0]
    engine._last_scan = clock[0] - engine.settings.scan_seconds + 2.0  # scanning om 2 sekunder
    engine._last_snipe = engine._last_top_poll = clock[0]
    assert engine._sleep_seconds() == pytest.approx(2.0)
    engine._last_scan = clock[0] - engine.settings.scan_seconds - 5  # for sent: vågn med det samme
    assert engine._sleep_seconds() == pytest.approx(0.2)
