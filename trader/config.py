"""Fælles indstillinger fra miljøet: server, login og Telegram. Memebottens egne ligger i trader/meme/config.py.

Hemmeligheder læses kun herfra, aldrig fra koden.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def _flag(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "ja", "on"}


def _float(value: str | None, default: float) -> float:
    if value is None or value.strip() == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _login_text(value: str | None) -> str:
    """Brugernavn og adgangskode fra miljøet uden mellemrum og anførselstegn omkring.

    I Railway skriver man let `"min-kode"` eller får et mellemrum med, og så passer login aldrig.
    """
    text = (value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def _int(value: str | None, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    db_path: str = "trader.db"
    port: int = 8741
    host: str = "127.0.0.1"
    app_username: str = ""
    app_password: str = ""
    app_password_hash: str = ""
    session_secret: str = ""
    app_totp_secret: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv(ROOT / ".env", override=False)
        return cls(
            db_path=os.getenv("DB_PATH", "trader.db").strip() or "trader.db",
            port=_int(os.getenv("PORT"), 8741),
            host=os.getenv("HOST", "127.0.0.1").strip() or "127.0.0.1",
            app_username=_login_text(os.getenv("APP_USERNAME")),
            app_password=_login_text(os.getenv("APP_PASSWORD")),
            app_password_hash=os.getenv("APP_PASSWORD_HASH", "").strip(),
            session_secret=os.getenv("SESSION_SECRET", "").strip(),
            app_totp_secret=os.getenv("APP_TOTP_SECRET", "").strip().replace(" ", "").upper(),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        )
