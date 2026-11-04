"""Telegram-kommandoer: styr og følg botten fra telefonen.

Kun beskeder fra TELEGRAM_CHAT_ID besvares. Alle andre ignoreres uden svar. Der kan
ikke hæves penge eller slås rigtige penge til herfra; det kræver dashboardet.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx

logger = logging.getLogger("trader.meme.telegram")

HELP = """Kommandoer:
/status – egenkapital, resultat og tilstand
/positioner – åbne positioner
/pause – ingen nye køb (åbne passes stadig)
/fortsaet – køb igen
/saelg SYMBOL – sælg en position nu
/nodstop ja – sælg alt og stop nye køb
/genoptag – ophæv nødstop
/sniper til | fra – tænd eller sluk sniperen
/maal 100 – sniperens gevinstmål i %
/rapport – dagsrapporten nu
/hjaelp – denne liste"""


def _normalize(text: str) -> tuple[str, list[str]]:
    words = text.strip().split()
    if not words:
        return "", []
    command = words[0].lower().split("@", 1)[0].lstrip("/")
    command = command.replace("æ", "ae").replace("ø", "oe").replace("å", "aa")
    return command, words[1:]


def _usd(value: Any) -> str:
    try:
        return f"{float(value):+.2f} USD"
    except (TypeError, ValueError):
        return "—"


class TelegramCommands:
    """Selve kommandoerne. Adskilt fra netværket, så de kan testes."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine

    def handle(self, text: str) -> str:
        command, args = _normalize(text)
        engine = self.engine
        try:
            if command in {"start", "hjaelp", "help"}:
                return HELP
            if command == "status":
                status = engine.status()
                k = status["kpis"]
                state = "NØDSTOP" if status["kill_switch"] else "pause" if status["paused"] else "kører"
                return (
                    f"{'Papir' if status['mode'] == 'paper' else 'RIGTIGE PENGE'} · {state}\n"
                    f"Egenkapital {k['equity']:.2f} USD\n"
                    f"I dag {_usd(k['today_pnl'])} · i alt {_usd(k['total_pnl'])}\n"
                    f"Åbne positioner: {k['open']} · lukkede: {k['trades']}\n"
                    f"Sniper: {'til, mål +' + format(status['sniper']['settings']['take_profit_pct'] * 100, '.0f') + ' %' if status['sniper']['enabled'] else 'fra'}"
                )
            if command in {"positioner", "pos"}:
                rows = engine.status()["positions"]
                if not rows:
                    return "Ingen åbne positioner."
                return "\n".join(
                    f"{row['symbol']}: {row['change_pct'] * 100:+.0f} % ({_usd(row['unrealized_usd'])}) · {row['source']}" for row in rows
                )
            if command == "pause":
                engine.pause(True)
                return "Nye køb er sat på pause. Åbne positioner passes stadig."
            if command in {"fortsaet", "fortsat", "resume"}:
                engine.pause(False)
                return "Botten køber igen, når noget består alle tjek."
            if command in {"saelg", "sell"}:
                if not args:
                    return "Skriv hvilket token, fx /saelg BONK"
                symbol = args[0].upper().lstrip("$")
                matches = [row for row in engine.status()["positions"] if str(row["symbol"]).upper() == symbol]
                if not matches:
                    return f"Ingen åben position i {symbol}."
                engine.sell_position(int(matches[0]["id"]))
                return f"{symbol} er sendt til salg."
            if command in {"noedstop", "nodstop", "stop"}:
                if not args or args[0].lower() != "ja":
                    return "Sikker? Skriv /nodstop ja for at sælge alt og stoppe nye køb."
                engine.kill()
                return "Nødstop: alle positioner sælges, og nye køb er blokeret. Skriv /genoptag for at fortsætte."
            if command == "genoptag":
                engine.resume()
                return "Nødstoppet er ophævet."
            if command == "sniper":
                if not args or args[0].lower() not in {"til", "fra", "on", "off"}:
                    return "Skriv /sniper til eller /sniper fra"
                on = args[0].lower() in {"til", "on"}
                engine.update_sniper({"enabled": on})
                return "Sniperen er tændt." if on else "Sniperen er slukket."
            if command in {"maal", "mal", "target"}:
                if not args:
                    return "Skriv målet i procent, fx /maal 100"
                try:
                    value = float(args[0].replace("%", "").replace(",", ".").lstrip("+"))
                except ValueError:
                    return "Målet skal være et tal, fx /maal 150"
                engine.update_sniper({"take_profit_pct": value / 100.0})
                return f"Sniperens gevinstmål er nu +{engine.sniper.take_profit_pct * 100:.0f} %."
            if command == "rapport":
                return engine.daily_report_text()
        except ValueError as exc:
            return f"Det gik ikke: {exc}"
        return "Den kommando kender jeg ikke. Skriv /hjaelp."


class TelegramBot:
    def __init__(self, engine: Any, token: str, chat_id: str) -> None:
        self.commands = TelegramCommands(engine)
        self.token = token
        self.chat_id = str(chat_id).strip()
        self._client = httpx.Client(timeout=35.0)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="memebot-telegram", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._client.close()

    def _api(self, method: str, payload: dict) -> Any:
        response = self._client.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload)
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description") or f"HTTP {response.status_code}")
        return data.get("result")

    def handle_update(self, update: dict) -> str | None:
        message = update.get("message") or update.get("edited_message") or {}
        chat = str((message.get("chat") or {}).get("id") or "")
        text = str(message.get("text") or "")
        if chat != self.chat_id or not text.startswith("/"):
            return None
        return self.commands.handle(text)

    def _loop(self) -> None:
        delay = 2.0
        while not self._stop.is_set():
            try:
                updates = self._api("getUpdates", {"offset": self._offset, "timeout": 25, "allowed_updates": ["message"]}) or []
                delay = 2.0
                for update in updates:
                    self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                    reply = self.handle_update(update)
                    if reply:
                        self._api("sendMessage", {"chat_id": self.chat_id, "text": reply[:4000]})
            except Exception as exc:  # netværk eller Telegram nede: vent og prøv igen
                logger.info("Telegram: %s", exc)
                self._stop.wait(delay)
                delay = min(60.0, delay * 2)
            else:
                time.sleep(0.2)
