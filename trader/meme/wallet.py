"""Wallets. Papir og live deler samme kald, så papirhandel bruger rigtige Jupiter-quotes.

Live-wallet'en signerer lokalt. Den private nøgle forlader aldrig processen, logges aldrig og
vises aldrig i dashboardet. Brug en separat Phantom-konto med kun det beløb, botten må tabe.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass

from trader.meme.clients import Jupiter, SolanaRPC, SourceError
from trader.meme.config import BASE_FEE_LAMPORTS, LAMPORTS_PER_SOL, SOL_MINT, MemeSettings
from trader.meme.store import MemeStore

logger = logging.getLogger("trader.meme.wallet")

TOKEN_PROGRAMS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
}


@dataclass
class Fill:
    ok: bool
    usd: float = 0.0
    fee_usd: float = 0.0
    token_raw: int = 0
    sol: float = 0.0
    signature: str = ""
    error: str = ""


def fee_sol(settings: MemeSettings) -> float:
    return (BASE_FEE_LAMPORTS + settings.priority_fee_lamports) / LAMPORTS_PER_SOL


class PaperWallet:
    """Simulerer handler mod rigtige Jupiter-quotes. Ingen transaktioner sendes."""

    mode = "paper"
    public_key = ""

    def __init__(self, settings: MemeSettings, store: MemeStore, jupiter: Jupiter) -> None:
        self.settings = settings
        self.store = store
        self.jupiter = jupiter
        if self.store.get_state("paper_cash") is None:
            self.store.set_state("paper_cash", repr(settings.bankroll_usd))

    def cash_usd(self, sol_usd: float) -> float:
        try:
            return float(self.store.get_state("paper_cash") or 0.0)
        except ValueError:
            return 0.0

    def _set_cash(self, value: float) -> None:
        self.store.set_state("paper_cash", repr(round(value, 8)))

    def sol_balance(self) -> float | None:
        return None

    def buy(self, mint: str, usd: float, sol_usd: float) -> Fill:
        if sol_usd <= 0:
            return Fill(False, error="Ingen SOL-kurs.")
        fee = fee_sol(self.settings) * sol_usd
        cash = self.cash_usd(sol_usd)
        if usd + fee > cash + 1e-9:
            return Fill(False, error=f"Ikke nok papirpenge ({cash:.2f} USD).")
        lamports = int(usd / sol_usd * LAMPORTS_PER_SOL)
        try:
            quote = self.jupiter.quote(SOL_MINT, mint, lamports)
        except SourceError as exc:
            return Fill(False, error=str(exc))
        if quote.out_amount <= 0:
            return Fill(False, error="Quoten gav 0 tokens.")
        self._set_cash(cash - usd - fee)
        return Fill(True, usd=usd, fee_usd=fee, token_raw=quote.out_amount, sol=lamports / LAMPORTS_PER_SOL, signature="papir")

    def sell(self, mint: str, raw_amount: int, sol_usd: float) -> Fill:
        if raw_amount <= 0:
            return Fill(False, error="Intet at sælge.")
        try:
            quote = self.jupiter.quote(mint, SOL_MINT, raw_amount)
        except SourceError as exc:
            return Fill(False, error=str(exc))
        sol = quote.out_amount / LAMPORTS_PER_SOL
        usd = sol * sol_usd
        fee = fee_sol(self.settings) * sol_usd
        self._set_cash(self.cash_usd(sol_usd) + usd - fee)
        return Fill(True, usd=usd, fee_usd=fee, token_raw=raw_amount, sol=sol, signature="papir")

    def after_close(self, mint: str) -> None:
        return None

    def close(self) -> None:
        return None


def load_keypair(secret: str):
    """Phantom eksporterer en base58-streng. Solana CLI bruger et JSON-array med 64 tal."""
    from solders.keypair import Keypair

    text = secret.strip()
    try:
        if text.startswith("["):
            values = json.loads(text)
            return Keypair.from_bytes(bytes(int(v) for v in values))
        return Keypair.from_base58_string(text)
    except Exception as exc:  # beskeden må aldrig indeholde nøglen
        raise ValueError("SOLANA_PRIVATE_KEY kunne ikke læses. Brug den private nøgle fra Phantom (base58).") from exc


class SolanaWallet:
    """Rigtige handler via Jupiter. Signerer lokalt og venter på bekræftelse."""

    mode = "live"

    def __init__(self, settings: MemeSettings, rpc: SolanaRPC, jupiter: Jupiter) -> None:
        self.settings = settings
        self.rpc = rpc
        self.jupiter = jupiter
        self._keypair = load_keypair(settings.private_key)
        self.public_key = str(self._keypair.pubkey())

    def sol_balance(self) -> float:
        return self.rpc.balance_lamports(self.public_key) / LAMPORTS_PER_SOL

    def cash_usd(self, sol_usd: float) -> float:
        return max(0.0, self.sol_balance() - self.settings.sol_reserve) * sol_usd

    def token_raw(self, mint: str) -> int:
        return sum(row["amount"] for row in self.rpc.token_accounts(self.public_key, mint))

    def _sign_and_send(self, b64_tx: str) -> str:
        from solders.transaction import VersionedTransaction

        unsigned = VersionedTransaction.from_bytes(base64.b64decode(b64_tx))
        signed = VersionedTransaction(unsigned.message, [self._keypair])
        return self.rpc.send_transaction(base64.b64encode(bytes(signed)).decode())

    def _confirm(self, signature: str, timeout: float = 75.0) -> tuple[bool, str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                status = self.rpc.signature_status(signature)
            except SourceError as exc:
                logger.info("Status for %s kunne ikke hentes: %s", signature, exc)
                status = None
            if status:
                if status.get("err"):
                    return False, f"Transaktionen fejlede on-chain: {status['err']}"
                if status.get("confirmationStatus") in {"confirmed", "finalized"}:
                    return True, ""
            time.sleep(2.0)
        return False, "Transaktionen blev ikke bekræftet i tide."

    def buy(self, mint: str, usd: float, sol_usd: float) -> Fill:
        if sol_usd <= 0:
            return Fill(False, error="Ingen SOL-kurs.")
        lamports = int(usd / sol_usd * LAMPORTS_PER_SOL)
        balance = self.sol_balance()
        if balance - lamports / LAMPORTS_PER_SOL < self.settings.sol_reserve:
            return Fill(False, error=f"For lidt SOL ({balance:.4f}). Reserven på {self.settings.sol_reserve} SOL til gebyrer røres ikke.")
        before = self.token_raw(mint)
        try:
            quote = self.jupiter.quote(SOL_MINT, mint, lamports)
            signature = self._sign_and_send(self.jupiter.swap_transaction(quote, self.public_key))
        except (SourceError, ValueError) as exc:
            return Fill(False, error=str(exc))
        ok, error = self._confirm(signature)
        after = self.token_raw(mint)
        received = max(0, after - before)
        if not ok and received <= 0:
            return Fill(False, error=error, signature=signature)
        # Jupiter bruger præcis det input, vi beder om. Rent til den nye token-konto (~0,002 SOL)
        # er et depositum, der kommer tilbage, når kontoen lukkes efter salget, så det tælles ikke som tab.
        fee = fee_sol(self.settings) * sol_usd
        sol_in = lamports / LAMPORTS_PER_SOL
        return Fill(True, usd=sol_in * sol_usd, fee_usd=fee, token_raw=received, sol=sol_in, signature=signature)

    def sell(self, mint: str, raw_amount: int, sol_usd: float) -> Fill:
        held = self.token_raw(mint)
        amount = min(raw_amount, held)
        if amount <= 0:
            return Fill(False, error="Wallet'en har ingen tokens at sælge.")
        before = self.sol_balance()
        try:
            quote = self.jupiter.quote(mint, SOL_MINT, amount)
            signature = self._sign_and_send(self.jupiter.swap_transaction(quote, self.public_key))
        except (SourceError, ValueError) as exc:
            return Fill(False, error=str(exc))
        ok, error = self._confirm(signature)
        left = self.token_raw(mint)
        sold = max(0, held - left)
        if not ok and sold <= 0:
            return Fill(False, error=error, signature=signature)
        net_sol = max(0.0, self.sol_balance() - before)
        fee = fee_sol(self.settings) * sol_usd
        return Fill(True, usd=net_sol * sol_usd + fee, fee_usd=fee, token_raw=sold, sol=net_sol, signature=signature)

    def after_close(self, mint: str) -> None:
        """Luk tomme token-konti, så rent-depositummet (~0,002 SOL) kommer tilbage."""
        from solders.hash import Hash
        from solders.instruction import AccountMeta, Instruction
        from solders.message import MessageV0
        from solders.pubkey import Pubkey
        from solders.transaction import VersionedTransaction

        try:
            accounts = [row for row in self.rpc.token_accounts(self.public_key, mint) if row["amount"] == 0]
            if not accounts:
                return
            owner = self._keypair.pubkey()
            instructions = []
            for row in accounts:
                program = str(row.get("program") or "")
                if program not in TOKEN_PROGRAMS:
                    continue
                instructions.append(
                    Instruction(
                        Pubkey.from_string(program),
                        bytes([9]),
                        [
                            AccountMeta(Pubkey.from_string(row["pubkey"]), False, True),
                            AccountMeta(owner, False, True),
                            AccountMeta(owner, True, False),
                        ],
                    )
                )
            if not instructions:
                return
            blockhash, _height = self.rpc.latest_blockhash()
            message = MessageV0.try_compile(owner, instructions, [], Hash.from_string(blockhash))
            tx = VersionedTransaction(message, [self._keypair])
            self.rpc.send_transaction(base64.b64encode(bytes(tx)).decode())
        except Exception as exc:  # rent er en lille gevinst, aldrig en grund til at stoppe
            logger.info("Kunne ikke lukke tom token-konto for %s: %s", mint, exc)

    def close(self) -> None:
        return None
