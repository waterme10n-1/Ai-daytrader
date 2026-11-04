"""Test forbindelser: kalder hver datakilde én gang og forklarer, hvad der virker, og hvad der skal rettes."""

from __future__ import annotations

import time
from typing import Any, Callable

import httpx

from trader.meme.clients import SolanaRPC
from trader.meme.config import DEFAULT_RPC, SOL_MINT, USDC_MINT
from trader.meme.store import now_iso

# BONK: et gammelt og likvidt token, kun brugt til at se, om RugCheck og DexScreener svarer.
KNOWN_TOKEN = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"

HINT_JUPITER = (
    "Står der rate limit (429), har Jupiter fået for mange kald. Uden nøgle deles grænsen med andre servere på "
    "samme IP. Opret en gratis nøgle på portal.jup.ag og sæt JUPITER_API_KEY i Railway; så bruges api.jup.ag "
    "automatisk. Ellers: tjek at JUPITER_API_URL er rigtig."
)
HINT_DEX = "DexScreener svarer ikke. Prøv igen om lidt; deres API er gratis og uden nøgle."
HINT_RUG = "RugCheck svarer ikke. Uden RugCheck køber botten intet, så det skal virke."
HINT_RPC = "Sæt SOLANA_RPC_URL til en egen node, fx en gratis nøgle fra helius.dev."


def _short(exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
    return text[:220]


def run_checks(engine: Any) -> dict:
    clients = engine.clients
    settings = engine.settings
    checks: list[dict] = []

    def check(check_id: str, label: str, fn: Callable[[], str], hint: str = "", optional: bool = False) -> None:
        started = time.monotonic()
        try:
            detail = fn()
            checks.append({"id": check_id, "label": label, "ok": True, "detail": detail, "ms": int((time.monotonic() - started) * 1000), "optional": optional})
        except Exception as exc:  # hver kilde fejler for sig, de andre testes stadig
            checks.append(
                {
                    "id": check_id,
                    "label": label,
                    "ok": False,
                    "detail": _short(exc),
                    "hint": hint,
                    "ms": int((time.monotonic() - started) * 1000),
                    "optional": optional,
                }
            )

    check("jup_price", "Jupiter: SOL-kurs", lambda: f"1 SOL = {clients.jup.sol_usd():.2f} USD", HINT_JUPITER)

    def quote() -> str:
        result = clients.jup.quote(SOL_MINT, USDC_MINT, 10_000_000)
        return f"0,01 SOL = {result.out_amount / 1_000_000:.2f} USDC"

    check("jup_quote", "Jupiter: pris for et køb (quote)", quote, HINT_JUPITER)
    check("jup_recent", "Jupiter: nye tokens (sniperen)", lambda: f"{len(clients.jup.recent_tokens(20))} nye tokens", HINT_JUPITER)
    check("jup_trending", "Jupiter: trending tokens", lambda: f"{len(clients.jup.top_tokens('toptrending', '1h', 20))} tokens", HINT_JUPITER)
    check("dex_boosts", "DexScreener: boosts", lambda: f"{len(clients.dex.latest_boosts())} tokens", HINT_DEX)

    def dex_pairs() -> str:
        pair = clients.dex.pairs_for([KNOWN_TOKEN]).get(KNOWN_TOKEN)
        if pair is None:
            raise RuntimeError("Intet handelspar fundet for testtokenet.")
        return f"BONK = {pair.price_usd:.8f} USD, likviditet ${pair.liquidity_usd:,.0f}"

    check("dex_pairs", "DexScreener: kurser", dex_pairs, HINT_DEX)

    def rug() -> str:
        report = clients.rug.report(KNOWN_TOKEN, max_age=0)
        return f"Rapport hentet: risiko {report.risk:.2f}, {len(report.risks)} punkter"

    check("rugcheck", "RugCheck: rapport", rug, HINT_RUG)

    def rpc() -> str:
        client = clients.rpc or SolanaRPC(settings.rpc_url)
        clients.rpc = clients.rpc or client
        height = client.block_height()
        note = " (offentlig, langsom og rate-limited)" if settings.rpc_url == DEFAULT_RPC else ""
        return f"Blokhøjde {height:,}{note}".replace(",", ".")

    check("rpc", "Solana RPC", rpc, HINT_RPC)

    if settings.private_key:

        def wallet() -> str:
            signer = engine._signer()
            return f"{signer.public_key[:4]}…{signer.public_key[-4:]}: {signer.sol_balance():.4f} SOL"

        check("wallet", "Bot-wallet", wallet, "Tjek SOLANA_PRIVATE_KEY (den private nøgle fra din Memebot-konto i Phantom).")

    if clients.x is not None:
        check("x", "X API", lambda: f"{clients.x.buzz(KNOWN_TOKEN, 'BONK').tweets} opslag fundet", "Tjek X_BEARER_TOKEN og dit abonnement hos X.", optional=True)

    if settings.anthropic_api_key:

        def claude() -> str:
            import anthropic

            model = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=0, timeout=15.0).models.retrieve(settings.ai_model)
            return f"Nøglen virker, model {model.id} findes"

        check("claude", "Claude API", claude, "Tjek ANTHROPIC_API_KEY og MEME_ANTHROPIC_MODEL.", optional=True)

    main = engine.main_settings
    if main is not None and main.telegram_bot_token:

        def telegram() -> str:
            response = httpx.get(f"https://api.telegram.org/bot{main.telegram_bot_token}/getMe", timeout=10.0)
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(data.get("description") or f"HTTP {response.status_code}")
            return f"Bot @{data['result'].get('username')}"

        check("telegram", "Telegram", telegram, "Tjek TELEGRAM_BOT_TOKEN fra @BotFather.", optional=True)

    if settings.execution == "jito":

        def jito() -> str:
            url = settings.jito_url.replace("/transactions", "/bundles")
            response = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "getTipAccounts", "params": []}, timeout=10.0)
            accounts = (response.json() or {}).get("result") or []
            if not accounts:
                raise RuntimeError(f"HTTP {response.status_code}: ingen tip-konti")
            return f"{len(accounts)} tip-konti"

        check("jito", "Jito block engine", jito, "Tjek MEME_JITO_URL, eller sæt MEME_EXECUTION=rpc.", optional=True)

    required = [item for item in checks if not item["optional"]]
    result = {
        "at": now_iso(),
        "ok": sum(1 for item in checks if item["ok"]),
        "total": len(checks),
        "required_ok": all(item["ok"] for item in required),
        "checks": checks,
    }
    engine.store.set_json("diagnostics", result)
    return result
