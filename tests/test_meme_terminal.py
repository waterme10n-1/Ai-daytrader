"""Terminalen på Hjem: logo fra DexScreener, kursforløbet gemt på lukkede handler og den røde baggrund."""

from __future__ import annotations

from pathlib import Path

import trader.meme.engine as meme_engine
from meme_fakes import make_engine, raw_pair, safe_report
from trader.meme.clients import parse_pair

STATIC = Path(__file__).resolve().parents[1] / "trader" / "static"
MINT = "Term1nalMint1111111111111111111111111111111"


def _pair(image: str) -> dict:
    raw = raw_pair(MINT, symbol="MAME", mcap=620_000)
    raw["info"] = {"imageUrl": image}
    return raw


def test_the_token_logo_comes_from_dexscreener_and_only_over_https():
    assert parse_pair(_pair("https://cdn.example/logo.png")).image_url == "https://cdn.example/logo.png"
    assert parse_pair(_pair("http://cdn.example/logo.png")).image_url == ""
    assert parse_pair(_pair("javascript:alert(1)")).image_url == ""
    assert parse_pair(raw_pair(MINT)).image_url == ""


def test_a_closed_trade_keeps_its_price_path_logo_and_market_cap(monkeypatch):
    monkeypatch.setattr(meme_engine, "PATH_STEP_SECONDS", 0)
    engine, dex, rug, _jup, _notes = make_engine()
    dex.pairs[MINT] = _pair("https://cdn.example/mame.png")
    dex.boosts.append(MINT)
    rug.reports[MINT] = safe_report()
    engine.tick(force_scan=True)
    position = engine.status()["positions"][0]
    assert position["image"] == "https://cdn.example/mame.png"
    assert position["mcap_entry"] == 620_000 and position["volume_h1"] == 90_000
    for price in (0.0011, 0.00125, 0.0012):
        dex.set_price(MINT, price)
        engine.tick()
    engine.sell_position(position["id"])
    engine.tick()
    trade = engine.status()["trades"][0]
    assert trade["status"] == "closed" and trade["image"] == "https://cdn.example/mame.png"
    assert trade["path"][0] == 1.0 and len(trade["path"]) >= 4
    assert abs(trade["path"][-1] - 1.2) < 0.05


def test_the_terminal_is_the_home_view_with_deposit_boot_and_red_look():
    html = (STATIC / "meme.html").read_text(encoding="utf-8")
    for marker in ('id="panel-terminal"', 'id="tm-deposit"', 'id="tm-agents"', 'id="tm-act"', 'id="boot"', "/api/meme/paper/restart",
                   'setTab(location.hash.slice(1) || "terminal"', '["rod", "Farve"', '["hvid", "Hvid"', 'data-bg="rod"',
                   'id="tm-best"', 'id="tm-scan-bar"', 'id="tm-stats"', "function tmRollEquity", "function tmBubble", 'L.getItem("look-v")!=="2"'):
        assert marker in html, marker
    desk = (STATIC / "desk.html").read_text(encoding="utf-8")
    for marker in ('["rod", "Farve"', '["hvid", "Hvid"', 'data-bg="rod"', 't==="rod"', 'L.getItem("look-v")!=="2"', 'class="brand-logo"'):
        assert marker in desk, marker


def test_new_red_app_icon_and_red_manifest(tmp_path):
    import dataclasses

    from fastapi.testclient import TestClient

    from trader.app import MANIFEST, create_app
    from trader.config import Settings

    assert MANIFEST["theme_color"] == "#9a1119"
    assert all(icon["src"].endswith("?v=3") for icon in MANIFEST["icons"])
    engine, *_ = make_engine()
    engine.start = lambda: None
    engine.stop = lambda: None
    settings = dataclasses.replace(Settings.from_env(), db_path=str(tmp_path / "t.db"), host="127.0.0.1", app_username="",
                                   app_password="", app_password_hash="", session_secret="")
    with TestClient(create_app(settings=settings, meme_engine=engine)) as client:
        icon = client.get("/icons/icon-192.png?v=3")
        assert icon.status_code == 200 and icon.content[:8] == b"\x89PNG\r\n\x1a\n"
        assert client.get("/apple-touch-icon.png?v=3").status_code == 200


def test_every_colour_has_a_brand_tint_for_the_fade_and_the_logo():
    for page in ("meme.html", "desk.html"):
        html = (STATIC / page).read_text(encoding="utf-8")
        for colour in ("hvid", "roed", "blaa", "indigo", "lilla", "pink", "orange", "guld", "groen", "mint", "solana"):
            assert f':root[data-accent="{colour}"] {{ --brand:' in html, (page, colour)
        assert "var(--logo-1)" in html and "function lookSwap" in html and "startViewTransition" in html
        assert 'data-bg="rod"]:not([data-theme="light"]) body' in html and 'data-theme="dark"]:not([data-bg="rod"]) body' in html


def test_the_regular_scan_runs_every_10_seconds_by_default(monkeypatch):
    from trader.meme.config import MemeSettings

    monkeypatch.delenv("MEME_SCAN_SECONDS", raising=False)
    assert MemeSettings.from_env().scan_seconds == 10
    monkeypatch.setenv("MEME_SCAN_SECONDS", "3")
    assert MemeSettings.from_env().scan_seconds == 10  # aldrig hurtigere end hvert 10. sekund
    monkeypatch.setenv("MEME_SCAN_SECONDS", "60")
    assert MemeSettings.from_env().scan_seconds == 60
