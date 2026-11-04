"""To-trins-login: kode ved login og igen før rigtige penge og udbetalinger."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from meme_fakes import make_engine
from test_auth import _settings
from trader.app import create_app
from trader.auth import enforce_startup, new_totp_secret, totp_code, totp_ok, totp_uri
from trader.db import Database
from trader.engine import TradingEngine

SECRET = "JBSWY3DPEHPK3PXP"  # kendt testhemmelighed


def test_totp_matches_rfc_vector_and_tolerates_clock_skew():
    # RFC 6238-testvektor (SHA-1, 8 cifre giver 94287082 ved t=59; de sidste 6 er 287082).
    rfc_secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert totp_code(rfc_secret, at=59) == "287082"
    now = 1_700_000_000
    code = totp_code(SECRET, at=now)
    assert totp_ok(SECRET, code, at=now)
    assert totp_ok(SECRET, code, at=now + 30)
    assert not totp_ok(SECRET, code, at=now + 90)
    assert not totp_ok(SECRET, "000000" if code != "000000" else "111111", at=now)
    assert totp_ok(SECRET, code[:3] + " " + code[3:], at=now)
    assert len(new_totp_secret()) == 32
    assert totp_uri(SECRET).startswith("otpauth://totp/")


def test_bad_secret_stops_startup(tmp_path):
    with pytest.raises(SystemExit):
        enforce_startup(_settings(tmp_path, app_totp_secret="ikke base32!"))


def _client(tmp_path, meme=None):
    settings = _settings(tmp_path, app_totp_secret=SECRET)
    engine = TradingEngine(settings=settings, db=Database(settings.db_path))
    return TestClient(create_app(engine=engine, settings=settings, meme_engine=meme))


def _login(client, code):
    page = client.get("/login")
    assert 'name="code"' in page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    return client.post(
        "/login",
        data={"username": "anna", "password": "korrekt-kode", "csrf": csrf, "code": code},
        follow_redirects=False,
    )


def test_login_needs_the_code(tmp_path):
    with _client(tmp_path) as client:
        assert _login(client, "").status_code == 401
        assert _login(client, "123456" if totp_code(SECRET) != "123456" else "654321").status_code == 401
        ok = _login(client, totp_code(SECRET))
        assert ok.status_code == 303


def test_money_actions_need_a_fresh_code(tmp_path, monkeypatch):
    meme, *_ = make_engine()
    monkeypatch.setattr(meme, "start", lambda: None)
    monkeypatch.setattr(meme, "stop", lambda: None)
    with _client(tmp_path, meme) as client:
        assert _login(client, totp_code(SECRET)).status_code == 303
        csrf = client.cookies.get("dt_csrf")
        headers = {"X-CSRF-Token": csrf}
        status = client.get("/api/meme/status").json()
        assert status["security"] == {"totp": True}
        denied = client.post("/api/meme/wallet/withdraw", json={"sol": 0.1}, headers=headers)
        assert denied.status_code == 401 and "kode" in denied.json()["detail"].lower()
        denied = client.post("/api/meme/live/unlock", json={"phrase": "x"}, headers=headers)
        assert denied.status_code == 401
        # Med rigtig kode kommer man forbi kodetjekket og videre til de almindelige krav.
        passed = client.post("/api/meme/wallet/withdraw", json={"sol": 0.1, "code": totp_code(SECRET)}, headers=headers)
        assert passed.status_code == 400 and "MEME_WITHDRAW_ADDRESS" in passed.json()["detail"]
