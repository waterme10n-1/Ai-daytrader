"""Login, afvisning uden session, CSRF og lås efter forkerte forsøg."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from meme_fakes import quiet_engine
from trader.app import create_app
from trader.auth import enforce_startup, hash_password, verify_password
from trader.config import Settings


def _settings(tmp_path, **overrides) -> Settings:
    values = dict(
        db_path=str(tmp_path / "data" / "trader.db"),
        port=8741,
        host="127.0.0.1",
        app_username="anna",
        app_password="korrekt-kode",
        app_password_hash="",
        session_secret="s" * 32,
    )
    values.update(overrides)
    return Settings(**values)


def _client(tmp_path, meme=None, **overrides) -> TestClient:
    settings = _settings(tmp_path, **overrides)
    return TestClient(create_app(settings=settings, meme_engine=meme or quiet_engine()))


def _csrf_from_login(client: TestClient) -> str:
    page = client.get("/login")
    assert page.status_code == 200
    match = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert match, page.text[:200]
    return match.group(1)


def _login(client: TestClient, username: str = "anna", password: str = "korrekt-kode", **extra):
    csrf = _csrf_from_login(client)
    return client.post(
        "/login",
        data={"username": username, "password": password, "csrf": csrf},
        follow_redirects=False,
        **extra,
    )


@pytest.fixture
def client(tmp_path):
    with _client(tmp_path) as started:
        yield started


def test_healthz_is_open_and_everything_else_requires_login(client):
    assert client.get("/healthz").json() == {"ok": True}
    page = client.get("/", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"] == "/login"
    for path in ("/api/meme/status", "/api/meme/kill", "/api/meme/resume", "/api/meme/scan", "/api/meme/live/unlock", "/api/meme/paper"):
        method = client.get if path == "/api/meme/status" else client.post
        response = method(path, follow_redirects=False)
        assert response.status_code == 401, path
        assert "Log ind" in response.json()["detail"]


def test_login_then_csrf_on_state_changes(client):
    denied = client.post("/api/meme/kill")
    assert denied.status_code == 401

    wrong = _login(client, password="forkert")
    assert wrong.status_code == 401
    assert "Forkert" in wrong.text

    ok = _login(client)
    assert ok.status_code == 303
    assert ok.headers["location"] == "/meme"
    set_cookie = " ".join(ok.headers.get_list("set-cookie")).lower()
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie
    session_cookie = next(value for value in ok.headers.get_list("set-cookie") if value.startswith("dt_session="))
    csrf_cookie = next(value for value in ok.headers.get_list("set-cookie") if value.startswith("dt_csrf="))
    assert "HttpOnly" in session_cookie
    assert "HttpOnly" not in csrf_cookie

    assert client.get("/api/meme/status").status_code == 200
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 307 and home.headers["location"] == "/meme"
    page = client.get("/meme")
    assert page.status_code == 200 and "Memebot" in page.text
    assert "Daytrader" not in page.text

    blocked = client.post("/api/meme/kill")
    assert blocked.status_code == 403
    assert "CSRF" in blocked.json()["detail"]
    for path in ("/api/meme/resume", "/api/meme/scan", "/api/meme/live/unlock", "/api/meme/paper", "/logout"):
        assert client.post(path).status_code == 403

    csrf = client.cookies.get("dt_csrf")
    killed = client.post("/api/meme/kill", headers={"X-CSRF-Token": csrf})
    assert killed.status_code == 200
    assert killed.json()["kill_switch"] is True
    resumed = client.post("/api/meme/resume", headers={"X-CSRF-Token": csrf})
    assert resumed.status_code == 200
    assert resumed.json()["kill_switch"] is False


def test_secure_cookie_when_proxy_says_https(client):
    response = _login(client, headers={"x-forwarded-proto": "https"})
    assert response.status_code == 303
    for header in response.headers.get_list("set-cookie"):
        if header.startswith("dt_session=") or header.startswith("dt_csrf="):
            assert "Secure" in header


def test_login_lockout_after_repeated_failures(client):
    for _ in range(4):
        response = _login(client, password="nej")
        assert response.status_code == 401
    locked = _login(client, password="nej")
    assert locked.status_code == 429
    assert "For mange forsøg" in locked.text
    still = _login(client, password="korrekt-kode")
    assert still.status_code == 429


def test_password_hash_login(tmp_path):
    stored = hash_password("hemmelig-kode", iterations=10_000)
    assert verify_password("hemmelig-kode", stored)
    assert not verify_password("anden", stored)
    with _client(tmp_path, app_password="", app_password_hash=stored) as client:
        bad = _login(client, password="anden")
        assert bad.status_code == 401
        good = _login(client, password="hemmelig-kode")
        assert good.status_code == 303


def test_expired_login_form_is_rejected(client):
    response = client.post(
        "/login",
        data={"username": "anna", "password": "korrekt-kode", "csrf": "forkert"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert "udløbet" in response.text


def test_public_bind_refuses_to_start_without_secrets(tmp_path):
    public = _settings(tmp_path, host="0.0.0.0", app_username="", app_password="", session_secret="")
    with pytest.raises(SystemExit) as caught:
        enforce_startup(public)
    assert "SESSION_SECRET" in str(caught.value)
    enforce_startup(_settings(tmp_path, host="0.0.0.0"))
    enforce_startup(_settings(tmp_path, host="127.0.0.1", app_username="", app_password="", session_secret=""))
    with pytest.raises(SystemExit):
        enforce_startup(_settings(tmp_path, host="127.0.0.1", app_username="anna", session_secret=""))


def test_login_forgives_phone_typing_but_not_wrong_password(client, caplog):
    # Telefonen skriver "Anna " med stort og mellemrum; det er stadig brugeren.
    assert _login(client, username="Anna ", password="korrekt-kode ").status_code == 303
    client.cookies.clear()
    with caplog.at_level("WARNING", logger="trader.app"):
        bad = _login(client, username="anna", password="Korrekt-kode")
    assert bad.status_code == 401
    assert "adgangskoden passer ikke til APP_PASSWORD" in caplog.text
    assert "Korrekt-kode" not in caplog.text  # adgangskoden selv skrives aldrig i loggen
    with caplog.at_level("WARNING", logger="trader.app"):
        _login(client, username="bent", password="korrekt-kode")
    assert "brugernavnet passer ikke" in caplog.text


def test_login_values_from_railway_lose_quotes_and_spaces(monkeypatch):
    monkeypatch.setenv("APP_USERNAME", ' "anna" ')
    monkeypatch.setenv("APP_PASSWORD", "'korrekt kode' ")
    settings = Settings.from_env()
    assert settings.app_username == "anna"
    assert settings.app_password == "korrekt kode"  # mellemrum inde i koden bevares


def test_login_form_does_not_autocapitalize_username(client):
    page = client.get("/login")
    assert 'autocapitalize="none"' in page.text
    assert "APP_PASSWORD" in page.text
