"""Valgfrie Telegram-beskeder. Uden token sker der ingenting."""

from __future__ import annotations

import logging

import httpx

from trader.config import Settings

logger = logging.getLogger("trader.notify")


def send(settings: Settings, text: str) -> None:
    token = settings.telegram_bot_token
    chat = settings.telegram_chat_id
    if not token or not chat:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        response = httpx.post(url, json={"chat_id": chat, "text": text}, timeout=8.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("Telegram-besked blev ikke sendt: %s", exc)
