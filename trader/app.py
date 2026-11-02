"""HTTP-lag. Dashboard, login og API. /healthz er åben, resten kræver session når login er slået til."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel

from trader.auth import (
    LOGIN_COOKIE,
    LOGIN_CSRF_SECONDS,
    SESSION_COOKIE,
    SESSION_SECONDS,
    CSRF_COOKIE,
    AuthMiddleware,
    LoginGuard,
    auth_is_active,
    client_key,
    cookie_is_secure,
    credentials_match,
    tokens_match,
    enforce_startup,
    read_session,
    render_login,
    sign_payload,
    unsign_payload,
    _delete_cookie,
    _set_cookie,
)
from trader.broker import BrokerError
from trader.config import Settings
from trader.engine import TradingEngine

STATIC = Path(__file__).resolve().parent / "static" / "index.html"


class PhraseBody(BaseModel):
    phrase: str = ""


def create_app(engine: TradingEngine | None = None, settings: Settings | None = None) -> FastAPI:
    chosen = settings or Settings.from_env()
    holder = {"engine": engine, "settings": chosen}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        enforce_startup(holder["settings"])
        eng = holder["engine"] or TradingEngine(settings=holder["settings"])
        holder["engine"] = eng
        app.state.engine = eng
        app.state.settings = holder["settings"]
        eng.start()
        try:
            yield
        finally:
            eng.stop()

    app = FastAPI(title="AI Daytrader", lifespan=lifespan)
    app.state.settings = chosen
    app.state.login_guard = LoginGuard()
    app.add_middleware(AuthMiddleware)

    def current() -> TradingEngine:
        return app.state.engine

    def active_settings() -> Settings:
        return app.state.settings

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/login")
    def login_form(request: Request):
        settings = active_settings()
        if not auth_is_active(settings):
            return RedirectResponse("/", status_code=303)
        if read_session(settings, request):
            return RedirectResponse("/", status_code=303)
        return _login_response(request, settings)

    @app.post("/login")
    async def login_submit(request: Request):
        settings = active_settings()
        if not auth_is_active(settings):
            return RedirectResponse("/", status_code=303)
        guard: LoginGuard = request.app.state.login_guard
        key = client_key(request)
        if guard.locked(key):
            return _login_response(
                request,
                settings,
                error="For mange forsøg. Prøv igen om et kvarter.",
                status_code=429,
            )
        form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
        username = form.get("username", [""])[0]
        password = form.get("password", [""])[0]
        csrf = form.get("csrf", [""])[0]
        ticket = unsign_payload(settings.session_secret, request.cookies.get(LOGIN_COOKIE, ""))
        expected = str(ticket.get("csrf")) if ticket and ticket.get("purpose") == "login" else ""
        if not tokens_match(expected, csrf):
            return _login_response(
                request,
                settings,
                error="Siden er udløbet. Genindlæs og prøv igen.",
                username=username,
                status_code=403,
            )
        if not credentials_match(settings, username, password):
            locked = guard.fail(key)
            message = (
                "For mange forsøg. Prøv igen om et kvarter."
                if locked
                else "Forkert brugernavn eller adgangskode."
            )
            return _login_response(
                request,
                settings,
                error=message,
                username=username,
                status_code=429 if locked else 401,
            )
        guard.succeed(key)
        csrf_token = _new_csrf()
        now = int(time.time())
        token = sign_payload(
            settings.session_secret,
            {"u": settings.app_username, "csrf": csrf_token, "exp": now + SESSION_SECONDS},
        )
        response = RedirectResponse("/", status_code=303)
        secure = cookie_is_secure(request)
        _set_cookie(response, SESSION_COOKIE, token, secure=secure, httponly=True, max_age=SESSION_SECONDS, path="/")
        _set_cookie(response, CSRF_COOKIE, csrf_token, secure=secure, httponly=False, max_age=SESSION_SECONDS, path="/")
        _delete_cookie(response, LOGIN_COOKIE, secure=secure, path="/login")
        return response

    @app.post("/logout")
    def logout(request: Request):
        response = RedirectResponse("/login", status_code=303)
        secure = cookie_is_secure(request)
        _delete_cookie(response, SESSION_COOKIE, secure=secure, path="/")
        _delete_cookie(response, CSRF_COOKIE, secure=secure, path="/")
        return response

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(
            STATIC,
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/api/status")
    def status() -> dict:
        return current().status()

    @app.post("/api/kill")
    def kill() -> dict:
        current().kill()
        return current().status()

    @app.post("/api/resume")
    def resume() -> dict:
        current().resume()
        return current().status()

    @app.post("/api/simulate")
    def simulate() -> dict:
        try:
            result = current().simulate_one_day()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"ok": True, "result": result, "status": current().status()}

    @app.post("/api/live/unlock")
    def unlock(body: PhraseBody) -> dict:
        ok, message = current().request_live(body.phrase)
        if not ok:
            raise HTTPException(status_code=400, detail=message)
        return {"ok": True, "message": message, "status": current().status()}

    @app.post("/api/paper")
    def to_paper() -> dict:
        try:
            current().return_to_paper()
        except BrokerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "status": current().status()}

    return app


def _new_csrf() -> str:
    import secrets

    return secrets.token_urlsafe(32)


def _login_response(
    request: Request,
    settings: Settings,
    *,
    error: str = "",
    username: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    import secrets

    csrf = secrets.token_urlsafe(32)
    ticket = sign_payload(
        settings.session_secret,
        {"purpose": "login", "csrf": csrf, "exp": int(time.time()) + LOGIN_CSRF_SECONDS},
    )
    response = HTMLResponse(render_login(csrf, error=error, username=username), status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    _set_cookie(
        response,
        LOGIN_COOKIE,
        ticket,
        secure=cookie_is_secure(request),
        httponly=True,
        max_age=LOGIN_CSRF_SECONDS,
        path="/login",
    )
    return response


app = create_app()
