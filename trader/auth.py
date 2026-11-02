"""Én bruger, signeret session og CSRF. Ingen login på loopback, medmindre det er sat op."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from html import escape

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from trader.config import Settings

SESSION_COOKIE = "dt_session"
CSRF_COOKIE = "dt_csrf"
LOGIN_COOKIE = "dt_login"
SESSION_SECONDS = 7 * 24 * 60 * 60
LOGIN_CSRF_SECONDS = 60 * 60
LOCKOUT_LIMIT = 5
LOCKOUT_SECONDS = 15 * 60
MIN_SECRET_LENGTH = 32
PBKDF2_ITERATIONS = 600_000

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def is_loopback(host: str) -> bool:
    return host.strip().lower() in LOOPBACK_HOSTS


def auth_is_active(settings: Settings) -> bool:
    return bool(
        settings.app_username
        and (settings.app_password or settings.app_password_hash)
        and len(settings.session_secret) >= MIN_SECRET_LENGTH
    )


def enforce_startup(settings: Settings) -> None:
    """Offentligt bind og halve login-opsætninger stopper processen."""
    has_user = bool(settings.app_username)
    has_password = bool(settings.app_password or settings.app_password_hash)
    has_secret = len(settings.session_secret) >= MIN_SECRET_LENGTH
    anything = has_user or has_password or bool(settings.session_secret)
    if is_loopback(settings.host) and not anything:
        return
    if has_user and has_password and has_secret:
        return
    missing: list[str] = []
    if not has_user:
        missing.append("APP_USERNAME")
    if not has_password:
        missing.append("APP_PASSWORD eller APP_PASSWORD_HASH")
    if not has_secret:
        missing.append(f"SESSION_SECRET på mindst {MIN_SECRET_LENGTH} tegn")
    raise SystemExit(
        f"Afviser at starte på {settings.host}: mangler {', '.join(missing)}. "
        "Et bind uden for loopback kræver login og SESSION_SECRET."
    )


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        salt = _b64d(salt_text)
        expected = _b64d(digest_text)
    except (ValueError, TypeError):
        return False
    if iterations < 1 or not password:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return hmac.compare_digest(actual, expected)


def tokens_match(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return hmac.compare_digest(left.encode(), right.encode())


def credentials_match(settings: Settings, username: str, password: str) -> bool:
    if not username or not password or not settings.app_username:
        return False
    if not tokens_match(username, settings.app_username):
        return False
    if settings.app_password_hash:
        return verify_password(password, settings.app_password_hash)
    if not settings.app_password:
        return False
    return tokens_match(password, settings.app_password)


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + ("=" * (-len(text) % 4)))


def sign_payload(secret: str, payload: dict) -> str:
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def unsign_payload(secret: str, token: str) -> dict | None:
    if not secret or not token or "." not in token:
        return None
    body, signature = token.rsplit(".", 1)
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        data = json.loads(_b64d(body))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or float(data.get("exp", 0)) < time.time():
        return None
    return data


def cookie_is_secure(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "")
    if forwarded.split(",")[0].strip().lower() == "https":
        return True
    return request.url.scheme == "https"


def read_session(settings: Settings, request: Request) -> dict | None:
    data = unsign_payload(settings.session_secret, request.cookies.get(SESSION_COOKIE, ""))
    if not data or data.get("u") != settings.app_username or not data.get("csrf"):
        return None
    return data


class LoginGuard:
    """Låser login efter gentagne forkerte forsøg. Én proces, én bruger."""

    def __init__(self, limit: int = LOCKOUT_LIMIT, lock_seconds: float = LOCKOUT_SECONDS) -> None:
        self.limit = limit
        self.lock_seconds = lock_seconds
        self._lock = threading.Lock()
        self._failures: dict[str, tuple[int, float]] = {}

    def locked(self, key: str) -> bool:
        with self._lock:
            count, until = self._failures.get(key, (0, 0.0))
            if until and time.monotonic() < until:
                return True
            if until and time.monotonic() >= until:
                self._failures.pop(key, None)
            return False

    def fail(self, key: str) -> bool:
        """Registrer et forsøg. True betyder, at grænsen nu er nået."""
        with self._lock:
            count, until = self._failures.get(key, (0, 0.0))
            if until and time.monotonic() < until:
                return True
            count += 1
            if count >= self.limit:
                self._failures[key] = (count, time.monotonic() + self.lock_seconds)
                return True
            self._failures[key] = (count, 0.0)
            return False

    def succeed(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


def client_key(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "ukendt"


def _set_cookie(response: Response, name: str, value: str, *, secure: bool, httponly: bool, max_age: int, path: str) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=httponly,
        secure=secure,
        samesite="strict",
        path=path,
    )


def _delete_cookie(response: Response, name: str, *, secure: bool, path: str) -> None:
    response.delete_cookie(name, path=path, secure=secure, samesite="strict")


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        settings: Settings = request.app.state.settings
        if not auth_is_active(settings):
            return await call_next(request)
        path = request.url.path
        if path == "/healthz" or path == "/login":
            return await call_next(request)
        session = read_session(settings, request)
        if session is None:
            if path.startswith("/api/") or path == "/logout":
                return JSONResponse({"detail": "Log ind for at fortsætte."}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            sent = request.headers.get("x-csrf-token", "")
            expected = str(session.get("csrf") or "")
            if not tokens_match(sent, expected):
                return JSONResponse({"detail": "Ugyldig CSRF-token. Genindlæs siden."}, status_code=403)
        request.state.session = session
        return await call_next(request)


def render_login(csrf: str, error: str = "", username: str = "") -> str:
    page = _LOGIN_HTML
    hidden = "" if error else "hidden"
    return (
        page.replace("__CSRF__", escape(csrf, quote=True))
        .replace("__ERROR__", escape(error))
        .replace("__USERNAME__", escape(username, quote=True))
        .replace("__ERROR_HIDDEN__", hidden)
    )


_LOGIN_HTML = """<!DOCTYPE html>
<html lang="da">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Log ind · AI Daytrader</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' fill='%231c1712'/%3E%3Ctext x='16' y='22' text-anchor='middle' font-size='13' fill='%23f3eee6' font-family='Georgia'%3EDT%3C/text%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,560;9..144,640&family=Outfit:wght@380;500;640&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #e8e1d4; --bg-2: #f7f3eb; --ink: #1c1712; --muted: #665e54;
    --line: #d8d0c3; --accent: #1d4636; --bad: #9d342c; --chip: #efe7da;
    color-scheme: light;
  }
  html[data-theme="dark"] {
    --bg: #121410; --bg-2: #1c1f1a; --ink: #f3eee6; --muted: #b7ae9f;
    --line: #32382e; --accent: #e0c27a; --bad: #f0a49c; --chip: #262b22;
    color-scheme: dark;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh;
    font-family: "Outfit", "Avenir Next", "Segoe UI", sans-serif;
    color: var(--ink);
    background:
      radial-gradient(900px 420px at 0% -10%, rgba(196, 161, 90, 0.22), transparent 55%),
      var(--bg);
  }
  main { width: min(440px, calc(100% - 32px)); margin: 0 auto; padding: 64px 0 48px; }
  .brand { display: flex; gap: 14px; align-items: center; }
  .mark {
    width: 46px; height: 46px; display: grid; place-items: center;
    border: 1.5px solid var(--ink);
    font-family: "Fraunces", Georgia, serif; font-weight: 640;
  }
  h1 { font-family: "Fraunces", Georgia, serif; font-weight: 560; font-size: 1.7rem; margin: 0; }
  .brand p { margin: 2px 0 0; color: var(--muted); }
  .badge {
    display: inline-block; margin: 28px 0 18px;
    border: 2px solid var(--accent); color: var(--accent);
    padding: 8px 14px 6px;
    font-family: "Fraunces", Georgia, serif;
    font-size: 1.7rem; letter-spacing: 0.14em; line-height: 1;
    transform: rotate(-1.4deg);
  }
  form {
    background: var(--bg-2); padding: 18px 18px 16px;
    box-shadow: 0 16px 40px rgba(70, 48, 16, 0.06);
    display: grid; gap: 12px;
  }
  label { display: grid; gap: 6px; font-size: 0.92rem; color: var(--muted); }
  input {
    font: inherit; color: var(--ink); background: var(--bg);
    border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px;
  }
  input:focus-visible, button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  button[type="submit"] {
    font: inherit; cursor: pointer; border: 1px solid var(--accent);
    background: var(--accent); color: #f7f3eb; border-radius: 8px; padding: 10px 12px;
  }
  html[data-theme="dark"] button[type="submit"] { color: #1c1712; }
  .error { margin: 0; padding: 10px 12px; border-left: 3px solid var(--bad); background: var(--chip); }
  .theme {
    margin-top: 18px; background: transparent; color: var(--ink);
    border: 1px solid var(--line); border-radius: 999px; padding: 4px 12px; cursor: pointer; font: inherit;
  }
  [hidden] { display: none; }
</style>
</head>
<body>
<main>
  <div class="brand">
    <div class="mark">DT</div>
    <div>
      <h1>AI Daytrader</h1>
      <p>Åbning og lukning · kun for dig</p>
    </div>
  </div>
  <div class="badge">LOG IND</div>
  <form method="post" action="/login" autocomplete="on">
    <p class="error" __ERROR_HIDDEN__>__ERROR__</p>
    <input type="hidden" name="csrf" value="__CSRF__">
    <label>Brugernavn
      <input name="username" autocomplete="username" required value="__USERNAME__">
    </label>
    <label>Adgangskode
      <input name="password" type="password" autocomplete="current-password" required>
    </label>
    <button type="submit">Log ind</button>
  </form>
  <button class="theme" id="theme" type="button">Mørk</button>
</main>
<script>
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("daytrader-theme", theme);
  document.getElementById("theme").textContent = theme === "dark" ? "Lys" : "Mørk";
}
const query = new URLSearchParams(location.search).get("theme");
const stored = localStorage.getItem("daytrader-theme");
const preferred = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
applyTheme(query === "dark" || query === "light" ? query : (stored || preferred));
document.getElementById("theme").addEventListener("click", () => {
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
});
</script>
</body>
</html>
"""
