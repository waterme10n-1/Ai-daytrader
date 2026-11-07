"""Copy-trading: følg udvalgte wallets og brug deres køb som signal (aldrig som blind ordre).

Et køb fra en fulgt wallet gør kun tokenet til en kandidat. Det skal stadig bestå alle
rug-tjek og risikogrænser. Sælger den fulgte wallet et token, vi har købt på grund af den,
sælger botten også. Wallets, der giver tab over tid, bliver automatisk slået fra.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from trader.meme.clients import SolanaRPC, SourceError
from trader.meme.config import LAMPORTS_PER_SOL, QUOTE_MINTS, SOL_MINT
from trader.meme.store import MemeStore

logger = logging.getLogger("trader.meme.copytrade")

MAX_SIGNAL_AGE_SECONDS = 15 * 60
MAX_FETCH_PER_WALLET = 8


@dataclass(frozen=True)
class CopySignal:
    wallet: str
    mint: str
    side: str
    sol: float
    signature: str
    block_time: int
    # Hvor mange tokens (rå enheder) traderen fik for sine SOL. 0 hvis købet ikke var i SOL.
    # Bruges til traderens købspris, så botten ikke køber langt over den.
    token_raw: int = 0


def _keys(message: dict) -> list[str]:
    keys = []
    for item in message.get("accountKeys") or []:
        if isinstance(item, dict):
            keys.append(str(item.get("pubkey") or ""))
        else:
            keys.append(str(item))
    return keys


def _token_amounts(rows: list[dict], wallet: str) -> dict[str, int]:
    amounts: dict[str, int] = {}
    for row in rows or []:
        if row.get("owner") != wallet:
            continue
        mint = str(row.get("mint") or "")
        try:
            raw = int((row.get("uiTokenAmount") or {}).get("amount") or 0)
        except (TypeError, ValueError):
            raw = 0
        amounts[mint] = amounts.get(mint, 0) + raw
    return amounts


def parse_wallet_swaps(tx: dict, wallet: str, signature: str = "") -> list[CopySignal]:
    if not tx:
        return []
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return []
    message = (tx.get("transaction") or {}).get("message") or {}
    keys = _keys(message)
    sol_delta = 0.0
    if wallet in keys:
        index = keys.index(wallet)
        pre = meta.get("preBalances") or []
        post = meta.get("postBalances") or []
        if index < len(pre) and index < len(post):
            sol_delta = (int(post[index]) - int(pre[index])) / LAMPORTS_PER_SOL
    before = _token_amounts(meta.get("preTokenBalances") or [], wallet)
    after = _token_amounts(meta.get("postTokenBalances") or [], wallet)
    deltas = {mint: after.get(mint, 0) - before.get(mint, 0) for mint in set(before) | set(after)}
    wsol = deltas.get(SOL_MINT, 0) / LAMPORTS_PER_SOL
    quote_out = sol_delta + wsol < -0.001 or any(deltas.get(mint, 0) < 0 for mint in QUOTE_MINTS if mint != SOL_MINT)
    quote_in = sol_delta + wsol > 0.0 or any(deltas.get(mint, 0) > 0 for mint in QUOTE_MINTS if mint != SOL_MINT)
    block_time = int(tx.get("blockTime") or 0)
    signals = []
    for mint, delta in deltas.items():
        if mint in QUOTE_MINTS or delta == 0:
            continue
        if delta > 0 and quote_out:
            paid_sol = sol_delta + wsol < -0.001
            signals.append(CopySignal(wallet, mint, "buy", abs(sol_delta + wsol), signature, block_time, delta if paid_sol else 0))
        elif delta < 0 and quote_in:
            signals.append(CopySignal(wallet, mint, "sell", abs(sol_delta + wsol), signature, block_time))
    return signals


class CopyTracker:
    def __init__(self, rpc: SolanaRPC | None, store: MemeStore, wallets: tuple[str, ...], *, prefix: str = "copy_last") -> None:
        self.rpc = rpc
        self.store = store
        self.wallets = wallets
        # Hver tracker husker sin egen plads i hver wallets historik, så to trackers ikke stjæler signaler fra hinanden.
        self.prefix = prefix

    def poll(self, muted: set[str] | None = None, now: float | None = None) -> list[CopySignal]:
        if self.rpc is None or not self.wallets:
            return []
        muted = muted or set()
        moment = now if now is not None else time.time()
        signals: list[CopySignal] = []
        for wallet in self.wallets:
            key = f"{self.prefix}:{wallet}"
            try:
                rows = self.rpc.signatures_for(wallet, limit=15)
            except SourceError as exc:
                logger.info("Kunne ikke læse %s: %s", wallet, exc)
                continue
            if not rows:
                continue
            last = self.store.get_state(key)
            newest = str(rows[0].get("signature") or "")
            if last is None:
                # Første gang: husk kun hvor vi er. Gamle handler kopieres aldrig.
                self.store.set_state(key, newest)
                continue
            fresh = []
            for row in rows:
                if row.get("signature") == last:
                    break
                fresh.append(row)
            self.store.set_state(key, newest)
            for row in reversed(fresh[:MAX_FETCH_PER_WALLET]):
                if row.get("err"):
                    continue
                block_time = int(row.get("blockTime") or 0)
                if block_time and moment - block_time > MAX_SIGNAL_AGE_SECONDS:
                    continue
                signature = str(row.get("signature") or "")
                try:
                    tx = self.rpc.transaction(signature)
                except SourceError as exc:
                    logger.info("Kunne ikke hente %s: %s", signature, exc)
                    continue
                for signal in parse_wallet_swaps(tx or {}, wallet, signature):
                    if signal.side == "buy" and f"copy:{wallet}" in muted:
                        continue
                    signals.append(signal)
        return signals
