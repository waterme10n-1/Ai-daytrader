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


def new_totp_secret() -> str:
    """Ny hemmelighed til en godkendelses-app (Google Authenticator, 1Password, Authy …)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _totp_key(secret: str) -> bytes:
    clean = secret.strip().replace(" ", "").upper()
    return base64.b32decode(clean + "=" * (-len(clean) % 8))


def totp_code(secret: str, at: float | None = None, step: int = 30, digits: int = 6) -> str:
    counter = int((time.time() if at is None else at) // step)
    digest = hmac.new(_totp_key(secret), counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return str(value % 10**digits).zfill(digits)


def totp_ok(secret: str, code: str, at: float | None = None, window: int = 1) -> bool:
    """Godkender koden for nu og ét 30-sekunders vindue før og efter (uret kan gå lidt skævt)."""
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if not secret or len(code) != 6:
        return False
    moment = time.time() if at is None else at
    try:
        return any(hmac.compare_digest(totp_code(secret, moment + shift * 30), code) for shift in range(-window, window + 1))
    except (ValueError, TypeError):
        return False


def totp_uri(secret: str, account: str = "memebot", issuer: str = "AI Daytrader") -> str:
    from urllib.parse import quote

    return f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}&digits=6&period=30"


def totp_enabled(settings: Settings) -> bool:
    return bool(getattr(settings, "app_totp_secret", ""))


def enforce_startup(settings: Settings) -> None:
    """Offentligt bind og halve login-opsætninger stopper processen."""
    if totp_enabled(settings):
        try:
            _totp_key(settings.app_totp_secret)
        except (ValueError, TypeError) as exc:
            raise SystemExit("APP_TOTP_SECRET er ikke en gyldig base32-hemmelighed. Lav en ny med new_totp_secret().") from exc
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


def _same_username(entered: str, expected: str) -> bool:
    # Telefoner skriver ofte stort begyndelsesbogstav eller sætter et mellemrum efter ordet.
    return tokens_match(entered.strip().casefold(), expected.strip().casefold())


def _same_password(settings: Settings, entered: str) -> bool:
    if not entered:
        return False
    trimmed = entered.strip()
    if settings.app_password_hash:
        if verify_password(entered, settings.app_password_hash):
            return True
        return trimmed != entered and verify_password(trimmed, settings.app_password_hash)
    if not settings.app_password:
        return False
    return tokens_match(trimmed, settings.app_password)


def credentials_match(settings: Settings, username: str, password: str) -> bool:
    if not username or not password or not settings.app_username:
        return False
    if not _same_username(username, settings.app_username):
        return False
    return _same_password(settings, password)


def login_failure_reason(settings: Settings, username: str, password: str, code_ok: bool = True) -> str:
    """Kun til serverens log, så ejeren kan se i Railway hvad der gik galt. Vises aldrig i browseren."""
    if not settings.app_username:
        return "APP_USERNAME er tom"
    if not _same_username(username, settings.app_username):
        return "brugernavnet passer ikke til APP_USERNAME"
    if not _same_password(settings, password):
        source = "APP_PASSWORD_HASH" if settings.app_password_hash else "APP_PASSWORD"
        return f"adgangskoden passer ikke til {source}"
    if not code_ok:
        return "koden fra godkendelses-appen er forkert eller for gammel"
    return "ukendt"


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


# Åbne uden login: sundhedstjek, login-siden og app-ikonerne. Intet af det afslører data.
PUBLIC_PATHS = frozenset({"/healthz", "/login", "/manifest.webmanifest", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"})


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        settings: Settings = request.app.state.settings
        if not auth_is_active(settings):
            return await call_next(request)
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/icons/"):
            # Ikoner og manifest hentes af telefonen uden cookies, når appen lægges på hjemmeskærmen.
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


def render_login(csrf: str, error: str = "", username: str = "", totp: bool = False) -> str:
    page = _LOGIN_HTML
    hidden = "" if error else "hidden"
    return (
        page.replace("__CSRF__", escape(csrf, quote=True))
        .replace("__ERROR__", escape(error))
        .replace("__USERNAME__", escape(username, quote=True))
        .replace("__ERROR_HIDDEN__", hidden)
        .replace("__TOTP__", _TOTP_FIELD if totp else "")
    )


_TOTP_FIELD = """<label class="field">
      <span>Kode fra din godkendelses-app</span>
      <input name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9 ]{6,7}" maxlength="7" required placeholder="123 456">
    </label>"""

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="da" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<title>Log ind · Memebot</title>
<link rel="icon" type="image/png" href="/icons/icon-192.png">
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#05060d">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<meta name="apple-mobile-web-app-title" content="Memebot">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Oxanium:wght@400..700&family=Saira:wght@400..700&display=swap" rel="stylesheet">
<style>
  :root {
    color-scheme: dark;
    --bg: #05060d; --surface: #0b0d19; --surface-2: #0f1220; --raised: #161a2c;
    --border: rgba(120,190,255,0.12); --border-strong: rgba(120,190,255,0.22);
    --text: #e9f1ff; --muted: #6f7d9e; --accent: #2ee6ff; --accent-soft: rgba(46,230,255,0.14); --on-accent: #03141a;
    --bad: #ff4d6d; --bad-soft: rgba(255,77,109,0.13);
    --font: "Saira", -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
    --mono: "Oxanium", "Saira", system-ui, sans-serif; --dot: rgba(46,230,255,0.09);
  }
  :root[data-theme="light"] {
    color-scheme: light;
    --bg: #eaeef5; --surface: #ffffff; --surface-2: #f5f7fb; --raised: #eceff6;
    --border: rgba(20,30,60,0.08); --border-strong: rgba(20,30,60,0.16);
    --text: #0d1424; --muted: #6b7690; --accent: #2b5bff; --accent-soft: rgba(43,91,255,0.12); --on-accent: #ffffff;
    --bad: #e23d4f; --bad-soft: rgba(226,61,79,0.09); --dot: rgba(20,30,60,0.07);
  }
  * { box-sizing: border-box; }
  html { -webkit-text-size-adjust: 100%; }
  body {
    margin: 0; min-height: 100vh; min-height: 100dvh; display: grid; place-items: center;
    padding: max(24px, env(safe-area-inset-top)) max(20px, env(safe-area-inset-right)) max(24px, env(safe-area-inset-bottom)) max(20px, env(safe-area-inset-left));
    background: var(--bg); color: var(--text); font: 15px/1.45 var(--font);
    -webkit-font-smoothing: antialiased; -webkit-tap-highlight-color: transparent;
  }
  body::before {
    content: ""; position: fixed; inset: 0; z-index: -1; pointer-events: none;
    background-image: radial-gradient(circle, var(--dot) 1px, transparent 1.3px); background-size: 22px 22px;
    -webkit-mask-image: radial-gradient(ellipse 70% 60% at 50% 40%, #000, transparent 75%); mask-image: radial-gradient(ellipse 70% 60% at 50% 40%, #000, transparent 75%);
  }
  main { width: 100%; max-width: 380px; }
  .brand { text-align: center; margin-bottom: 24px; }
  .brand img { width: 72px; height: 72px; border-radius: 18px; display: block; margin: 0 auto 16px; }
  h1 { margin: 0; font: 700 30px var(--mono); letter-spacing: 0.02em; }
  .brand p { margin: 6px 0 0; color: var(--muted); font: 12px var(--mono); letter-spacing: 0.06em; text-transform: uppercase; }
  form { display: grid; gap: 14px; background: linear-gradient(180deg, rgba(46,230,255,0.04), transparent 120px), var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 22px 20px; }
  .field { display: grid; gap: 7px; font: 600 10.5px var(--mono); letter-spacing: 0.09em; text-transform: uppercase; color: var(--muted); }
  input {
    height: 52px; padding: 0 16px; border-radius: 10px; border: 1px solid var(--border-strong); background: var(--surface-2);
    color: var(--text); font: 17px var(--font); outline: none; width: 100%;
  }
  input:focus { border-color: var(--accent); box-shadow: 0 0 0 4px var(--accent-soft); }
  input[name="code"] { letter-spacing: 0.25em; text-align: center; font: 20px var(--mono); }
  button[type="submit"] {
    height: 54px; margin-top: 4px; border: 0; border-radius: 10px; background: var(--accent); color: var(--on-accent);
    font: 700 16px var(--mono); letter-spacing: 0.06em; text-transform: uppercase; cursor: pointer; box-shadow: 0 0 22px -6px var(--accent); touch-action: manipulation;
  }
  button[type="submit"]:active { transform: scale(0.985); }
  .error { margin: 0; padding: 12px 14px; border-radius: 10px; background: var(--bad-soft); color: var(--bad); font-size: 14px; font-weight: 550; }
  .error[hidden] { display: none; }
  .hint { margin: 2px 2px 0; color: var(--muted); font-size: 12.5px; line-height: 1.45; }
  .theme { margin: 22px auto 0; display: block; height: 36px; padding: 0 14px; border-radius: 8px; border: 1px solid var(--border); background: transparent; color: var(--muted); font: 600 11px var(--mono); letter-spacing: 0.06em; text-transform: uppercase; cursor: pointer; }
</style>
</head>
<body>
<main>
  <div class="brand">
    <img src="/icons/icon-192.png" alt="">
    <h1>Memebot</h1>
    <p>Log ind for at følge din bot</p>
  </div>
  <form method="post" action="/login" autocomplete="on">
    <p class="error" __ERROR_HIDDEN__>__ERROR__</p>
    <input type="hidden" name="csrf" value="__CSRF__">
    <label class="field">
      <span>Brugernavn</span>
      <input name="username" autocomplete="username" autocapitalize="none" autocorrect="off" spellcheck="false" required value="__USERNAME__">
    </label>
    <label class="field">
      <span>Adgangskode</span>
      <input name="password" type="password" autocomplete="current-password" required>
    </label>
    __TOTP__
    <button type="submit">Log ind</button>
    <p class="hint">Det er APP_USERNAME og APP_PASSWORD fra Variables i Railway. Store og små bogstaver tæller i adgangskoden.</p>
  </form>
  <button class="theme" id="theme" type="button">Lyst tema</button>
</main>
<script>
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("meme-theme", theme); } catch (e) {}
  document.getElementById("theme").textContent = theme === "dark" ? "Lyst tema" : "Mørkt tema";
  var meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", theme === "dark" ? "#05060d" : "#eaeef5");
}
var stored = null;
try { stored = localStorage.getItem("meme-theme"); } catch (e) {}
var query = new URLSearchParams(location.search).get("theme");
applyTheme(query === "dark" || query === "light" ? query : (stored || "dark"));
document.getElementById("theme").addEventListener("click", function () {
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
});
</script>
</body>
</html>
"""


if __name__ == "__main__":  # python -m trader.auth totp
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "totp":
        new_secret = new_totp_secret()
        print("Sæt denne hemmelige variabel i Railway:")
        print(f"APP_TOTP_SECRET={new_secret}")
        print()
        print("Tilføj den i din godkendelses-app (manuel nøgle eller link):")
        print(totp_uri(new_secret))
    else:
        print("Brug: python -m trader.auth totp")
