"""HTTP-lag for memebotten: dashboard, login og API. /healthz er åben, resten kræver session når login er slået til."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
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
    login_failure_reason,
    tokens_match,
    enforce_startup,
    read_session,
    render_login,
    sign_payload,
    totp_enabled,
    totp_ok,
    unsign_payload,
    _delete_cookie,
    _set_cookie,
)
from trader.config import Settings
from trader.desk.alpaca import BrokerError as DeskBrokerError
from trader.desk.config import DeskSettings
from trader.meme.clients import SourceError
from trader.meme.config import MemeSettings

logger = logging.getLogger("trader.app")

MEME_STATIC = Path(__file__).resolve().parent / "static" / "meme.html"
DESK_STATIC = Path(__file__).resolve().parent / "static" / "desk.html"
ICONS = Path(__file__).resolve().parent / "static" / "icons"
ICON_FILES = frozenset({"icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png"})
# Gør dashboardet til en app på hjemmeskærmen (PWA). Åbner direkte på memebotten.
MANIFEST = {
    "name": "Memebot",
    "short_name": "Memebot",
    "description": "Din memecoin-bot på Solana: egenkapital, positioner, sniper og læring.",
    "lang": "da",
    "id": "/meme",
    "start_url": "/meme",
    "scope": "/",
    "display": "standalone",
    "background_color": "#05060d",
    "theme_color": "#05060d",
    "icons": [
        {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}


class PhraseBody(BaseModel):
    phrase: str = ""
    code: str = ""


class MessageBody(BaseModel):
    text: str


class IdBody(BaseModel):
    id: int


class PauseBody(BaseModel):
    paused: bool


class WalletBody(BaseModel):
    wallet: str = ""
    wallets: list[str] = []
    action: str = "add"


class DepositBody(BaseModel):
    from_address: str
    sol: float


class WithdrawBody(BaseModel):
    sol: float
    code: str = ""


class TokenBody(BaseModel):
    mint: str
    symbol: str = ""
    action: str = "block"


def create_app(
    settings: Settings | None = None,
    meme_engine=None,
    meme_settings: MemeSettings | None = None,
    desk_engine=None,
    desk_settings: DeskSettings | None = None,
) -> FastAPI:
    """Byg appen. I produktion startes memebotten selv; tests sprøjter en motor med falske kilder ind."""
    chosen = settings or Settings.from_env()
    holder = {"settings": chosen, "meme": meme_engine, "meme_settings": meme_settings}
    injected = meme_engine is not None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        enforce_startup(holder["settings"])
        app.state.settings = holder["settings"]
        meme = holder["meme"]
        if meme is None:
            meme_cfg = holder["meme_settings"] or MemeSettings.from_env()
            if meme_cfg.enabled:
                from trader.meme.engine import MemeEngine

                meme = MemeEngine(meme_cfg, main_settings=holder["settings"])
        holder["meme"] = meme
        app.state.meme = meme
        # Handelsbordet startes selv i produktion. I tests kun, når en motor eller indstillinger gives med.
        desk = desk_engine
        if desk is None and (not injected or desk_settings is not None):
            desk_cfg = desk_settings or DeskSettings.from_env()
            if desk_cfg.enabled:
                from trader.desk.engine import DeskEngine

                desk = DeskEngine(desk_cfg, holder["settings"])
        app.state.desk = desk
        if desk is not None:
            desk.start()
        bot = None
        if meme is not None:
            meme.start()
            cfg = holder["settings"]
            if not injected and meme.settings.telegram_commands and cfg.telegram_bot_token and cfg.telegram_chat_id:
                from trader.meme.telegram import TelegramBot

                bot = TelegramBot(meme, cfg.telegram_bot_token, cfg.telegram_chat_id)
                bot.start()
        try:
            yield
        finally:
            if bot is not None:
                bot.stop()
            if meme is not None:
                meme.stop()
            if desk is not None:
                desk.stop()

    app = FastAPI(title="Memebot", lifespan=lifespan)
    app.state.settings = chosen
    app.state.login_guard = LoginGuard()
    app.add_middleware(AuthMiddleware)
    # Dashboardet henter status ofte. Komprimering gør svaret ca. 9 gange mindre (vigtigt på mobil).
    app.add_middleware(GZipMiddleware, minimum_size=2000)

    def require_code(request: Request, code: str) -> None:
        """Ekstra kode fra godkendelses-appen før rigtige penge og udbetalinger, når to-trins-login er slået til."""
        settings = active_settings()
        if not totp_enabled(settings):
            return
        guard: LoginGuard = request.app.state.login_guard
        key = "totp:" + client_key(request)
        if guard.locked(key):
            raise HTTPException(status_code=429, detail="For mange forkerte koder. Prøv igen om et kvarter.")
        if not totp_ok(settings.app_totp_secret, code):
            guard.fail(key)
            raise HTTPException(status_code=401, detail="Koden fra din godkendelses-app mangler eller er forkert.")
        guard.succeed(key)

    def active_settings() -> Settings:
        return app.state.settings

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/manifest.webmanifest")
    def manifest() -> JSONResponse:
        return JSONResponse(MANIFEST, media_type="application/manifest+json", headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/icons/{name}")
    def app_icon(name: str) -> FileResponse:
        if name not in ICON_FILES:
            raise HTTPException(status_code=404, detail="Ukendt ikon.")
        return FileResponse(ICONS / name, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/apple-touch-icon.png")
    @app.get("/apple-touch-icon-precomposed.png")
    def apple_icon() -> FileResponse:
        # iPhone spørger selv efter disse stier, når siden lægges på hjemmeskærmen.
        return FileResponse(ICONS / "apple-touch-icon.png", media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/login")
    def login_form(request: Request):
        settings = active_settings()
        if not auth_is_active(settings):
            return RedirectResponse("/meme", status_code=303)
        if read_session(settings, request):
            return RedirectResponse("/meme", status_code=303)
        return _login_response(request, settings)

    @app.post("/login")
    async def login_submit(request: Request):
        settings = active_settings()
        if not auth_is_active(settings):
            return RedirectResponse("/meme", status_code=303)
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
        code = form.get("code", [""])[0]
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
        code_ok = not totp_enabled(settings) or totp_ok(settings.app_totp_secret, code)
        if not credentials_match(settings, username, password) or not code_ok:
            logger.warning("Login afvist: %s.", login_failure_reason(settings, username, password, code_ok))
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
        response = RedirectResponse("/meme", status_code=303)
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
    def index() -> RedirectResponse:
        return RedirectResponse("/meme", status_code=307)

    def meme():
        engine_ = getattr(app.state, "meme", None)
        if engine_ is None:
            raise HTTPException(status_code=404, detail="Memebotten er slået fra (MEME_ENABLED=false).")
        return engine_

    @app.get("/meme")
    def meme_page() -> FileResponse:
        return FileResponse(
            MEME_STATIC,
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/api/meme/status")
    def meme_status() -> dict:
        status = meme().status()
        status["security"] = {"totp": totp_enabled(active_settings())}
        return status

    @app.post("/api/meme/kill")
    def meme_kill() -> dict:
        meme().kill()
        return meme().status()

    @app.post("/api/meme/resume")
    def meme_resume() -> dict:
        meme().resume()
        return meme().status()

    @app.post("/api/meme/scan")
    def meme_scan() -> dict:
        meme().request_scan()
        return {"ok": True}

    @app.post("/api/meme/sell")
    def meme_sell(body: IdBody) -> dict:
        try:
            result = meme().sell_position(body.id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        status = meme().status()
        status["sell"] = result
        return status

    @app.post("/api/meme/pause")
    def meme_pause(body: PauseBody) -> dict:
        meme().pause(body.paused)
        return meme().status()

    @app.post("/api/meme/wallets")
    def meme_wallets(body: WalletBody) -> dict:
        try:
            if body.action == "remove":
                meme().remove_wallet(body.wallet)
            elif body.wallets:
                result = meme().add_wallets(body.wallets)
                if not result["added"] and result["errors"]:
                    raise ValueError("; ".join(result["errors"][:3]))
                status = meme().status()
                status["import"] = result
                return status
            else:
                meme().add_wallet(body.wallet)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return meme().status()

    @app.get("/api/meme/wallet")
    def meme_wallet(phantom: str = "") -> dict:
        return meme().wallet_info(refresh=True, phantom=phantom)

    @app.post("/api/meme/wallet/deposit")
    def meme_deposit(body: DepositBody) -> dict:
        try:
            return meme().deposit_transaction(body.from_address, body.sol)
        except (ValueError, SourceError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/meme/wallet/withdraw")
    def meme_withdraw(body: WithdrawBody, request: Request) -> dict:
        require_code(request, body.code)
        try:
            result = meme().withdraw(body.sol)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {**result, "wallet": meme().wallet_info(refresh=True)}

    @app.post("/api/meme/blacklist")
    def meme_blacklist(body: TokenBody) -> dict:
        try:
            if body.action == "unblock":
                meme().unblock_token(body.mint)
            else:
                meme().block_token(body.mint, body.symbol)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return meme().status()

    @app.post("/api/meme/sniper")
    async def meme_sniper(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Ugyldig JSON.") from exc
        try:
            if isinstance(body, dict) and body.get("reset"):
                meme().reset_sniper()
            else:
                meme().update_sniper(body if isinstance(body, dict) else {})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return meme().status()

    @app.post("/api/meme/strategies")
    async def meme_strategies(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Ugyldig JSON.") from exc
        try:
            meme().set_strategy(body if isinstance(body, dict) else {})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return meme().status()

    @app.post("/api/meme/limits")
    async def meme_limits(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Ugyldig JSON.") from exc
        try:
            meme().update_loss_limits(body if isinstance(body, dict) else {})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return meme().status()

    @app.post("/api/meme/toptraders")
    async def meme_toptraders(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Ugyldig JSON.") from exc
        try:
            meme().update_toptraders(body if isinstance(body, dict) else {})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return meme().status()

    @app.post("/api/meme/diagnostics")
    def meme_diagnostics() -> dict:
        return meme().run_diagnostics()

    @app.post("/api/meme/report")
    def meme_report() -> dict:
        text = meme().daily_report_text()
        return {"text": text}

    @app.post("/api/meme/live/unlock")
    def meme_unlock(body: PhraseBody, request: Request) -> dict:
        require_code(request, body.code)
        ok, message = meme().request_live(body.phrase)
        if not ok:
            raise HTTPException(status_code=400, detail=message)
        return {"ok": True, "message": message, "status": meme().status()}

    def desk():
        engine_ = getattr(app.state, "desk", None)
        if engine_ is None:
            raise HTTPException(status_code=404, detail="Handelsbordet er slået fra (DESK_ENABLED=false).")
        return engine_

    @app.get("/desk")
    def desk_page() -> FileResponse:
        return FileResponse(
            DESK_STATIC,
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/api/desk/status")
    def desk_status(after: int = 0) -> dict:
        return desk().status(after=max(0, after))

    @app.post("/api/desk/message")
    def desk_message(body: MessageBody) -> dict:
        try:
            result = desk().post_message(body.text)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **result}

    @app.post("/api/desk/approve")
    def desk_approve(body: IdBody) -> dict:
        try:
            desk().approve(body.id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/desk/reject")
    def desk_reject(body: IdBody) -> dict:
        try:
            desk().reject(body.id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/desk/pause")
    def desk_pause(body: PauseBody) -> dict:
        desk().pause(body.paused)
        return {"ok": True}

    @app.post("/api/desk/params")
    async def desk_params(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Ugyldig JSON.") from exc
        try:
            desk().update_params(body if isinstance(body, dict) else {})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/desk/scan")
    def desk_scan() -> dict:
        engine_ = desk()
        if not engine_.keys_present:
            raise HTTPException(status_code=409, detail="Mangler Alpaca-papirnøgler.")
        market = engine_._market()
        if not market.get("is_open"):
            raise HTTPException(status_code=409, detail="Markedet er lukket. SCOUT arbejder kun i åbningstiden.")
        if engine_.paused:
            raise HTTPException(status_code=409, detail="Bordet er på pause.")
        import threading

        threading.Thread(target=engine_.scan, name="desk-scan", daemon=True).start()
        return {"ok": True}

    @app.post("/api/desk/close-all")
    def desk_close_all() -> dict:
        try:
            closed = desk().close_all()
        except DeskBrokerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "closed": closed}

    @app.post("/api/desk/log")
    def desk_log() -> dict:
        try:
            text = desk().day_log()
        except DeskBrokerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "text": text}

    @app.post("/api/desk/keys")
    async def desk_keys(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Ugyldig JSON.") from exc
        body = body if isinstance(body, dict) else {}
        if body.get("forget"):
            desk().forget_keys()
            return {"ok": True, "text": "Slettet. Bordet bruger igen nøglerne fra Railway."}
        save = bool(body.get("save"))
        if save:
            require_code(request, str(body.get("code") or ""))
        try:
            return desk().test_keys(str(body.get("key") or ""), str(body.get("secret") or ""), save=save)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/desk/claude-test")
    def desk_claude_test() -> dict:
        return desk().test_claude()

    @app.post("/api/desk/diagnose")
    def desk_diagnose() -> dict:
        return desk().diagnose()

    @app.get("/api/desk/compare")
    def desk_compare() -> dict:
        return desk().compare()

    @app.post("/api/meme/paper")
    def meme_paper() -> dict:
        meme().return_to_paper()
        return {"ok": True, "status": meme().status()}

    @app.post("/api/meme/paper/restart")
    async def meme_paper_restart(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Ugyldig JSON.") from exc
        amount = (body or {}).get("amount") if isinstance(body, dict) else None
        try:
            message = meme().restart_paper(amount)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "message": message, "status": meme().status()}

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
    response = HTMLResponse(render_login(csrf, error=error, username=username, totp=totp_enabled(settings)), status_code=status_code)
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
