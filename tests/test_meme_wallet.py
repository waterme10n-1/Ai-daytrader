"""Phantom: bot-wallet, indbetaling fra din Phantom, udbetaling og automatisk udbetaling af gevinst."""

from __future__ import annotations

import base64
import dataclasses
import struct

import pytest
from fastapi.testclient import TestClient
from solders.keypair import Keypair
from solders.message import Message
from solders.transaction import Transaction

import meme_fakes
from meme_fakes import make_engine
from trader.meme.wallet import b58encode, build_transfer_message

BLOCKHASH = "11111111111111111111111111111111"
SOL = 1_000_000_000


class FakeChain:
    def __init__(self):
        self.balances: dict[str, int] = {}
        self.sent: list[Transaction] = []

    def balance_lamports(self, pubkey):
        return self.balances.get(pubkey, 0)

    def latest_blockhash(self):
        return BLOCKHASH, 1000

    def send_transaction(self, b64):
        tx = Transaction.from_bytes(base64.b64decode(b64))
        self.sent.append(tx)
        return str(tx.signatures[0])

    def signature_status(self, signature):
        return {"confirmationStatus": "confirmed", "err": None}

    def token_accounts(self, owner, mint):
        return []

    def signatures_for(self, address, limit=15):
        return []

    def close(self):
        pass


def _lamports(message: Message) -> tuple[str, str, int]:
    ix = message.instructions[0]
    keys = message.account_keys
    kind, amount = struct.unpack("<IQ", bytes(ix.data))
    assert kind == 2  # System Program: transfer
    return str(keys[ix.accounts[0]]), str(keys[ix.accounts[1]]), amount


def _setup(**overrides):
    bot = Keypair()
    phantom = str(Keypair().pubkey())
    chain = FakeChain()
    cfg = meme_fakes.settings(private_key=str(bot), **overrides)
    engine, *_ = make_engine(cfg, rpc=chain)
    return engine, chain, str(bot.pubkey()), phantom


def test_b58_matches_solana_addresses():
    key = Keypair().pubkey()
    assert b58encode(bytes(key)) == str(key)
    assert b58encode(b"\0\0\x01") == "112"


def test_transfer_message_is_a_plain_sol_transfer():
    sender, receiver = str(Keypair().pubkey()), str(Keypair().pubkey())
    message = Message.from_bytes(build_transfer_message(sender, receiver, 123_456, BLOCKHASH))
    assert _lamports(message) == (sender, receiver, 123_456)
    assert str(message.account_keys[0]) == sender  # din Phantom betaler gebyret


def test_deposit_builds_unsigned_transfer_from_phantom_to_bot():
    engine, chain, bot, phantom = _setup()
    result = engine.deposit_transaction(phantom, 0.1)
    assert result["to"] == bot and result["lamports"] == 100_000_000
    expected = build_transfer_message(phantom, bot, 100_000_000, BLOCKHASH)
    assert result["message"] == b58encode(expected)
    assert chain.sent == []  # serveren sender intet; Phantom signerer og sender
    for bad, amount in [("ikke-en-adresse", 0.1), (bot, 0.1), (phantom, 0), (phantom, 999)]:
        with pytest.raises(ValueError):
            engine.deposit_transaction(bad, amount)


def test_deposit_needs_a_bot_wallet():
    engine, *_ = make_engine()
    with pytest.raises(ValueError, match="SOLANA_PRIVATE_KEY"):
        engine.deposit_transaction(str(Keypair().pubkey()), 0.1)


def test_withdraw_only_to_whitelisted_phantom_and_keeps_reserve():
    phantom = str(Keypair().pubkey())
    engine, chain, bot, _ = _setup(withdraw_address=phantom)
    chain.balances[bot] = int(0.5 * SOL)
    result = engine.withdraw(0.2)
    assert len(chain.sent) == 1
    assert _lamports(chain.sent[0].message) == (bot, phantom, int(0.2 * SOL))
    assert result["signature"] == str(chain.sent[0].signatures[0])
    with pytest.raises(ValueError, match="højst"):
        engine.withdraw(0.49)  # reserven på 0,02 SOL skal blive
    assert len(chain.sent) == 1


@pytest.mark.parametrize("target", ["", "ugyldig"])
def test_withdraw_refuses_without_valid_address(target):
    engine, chain, bot, _ = _setup(withdraw_address=target)
    chain.balances[bot] = SOL
    with pytest.raises(ValueError, match="MEME_WITHDRAW_ADDRESS"):
        engine.withdraw(0.1)
    assert chain.sent == []


def test_withdraw_refuses_the_bot_wallet_itself():
    bot_key = Keypair()
    chain = FakeChain()
    engine, *_ = make_engine(meme_fakes.settings(private_key=str(bot_key), withdraw_address=str(bot_key.pubkey())), rpc=chain)
    with pytest.raises(ValueError, match="bot-wallet"):
        engine.withdraw(0.1)


def test_auto_sweep_sends_profit_above_bankroll_once_per_interval():
    phantom = str(Keypair().pubkey())
    engine, chain, bot, _ = _setup(withdraw_address=phantom, sweep_above_usd=5.0)
    chain.balances[bot] = int(0.25 * SOL)  # 0,23 SOL fri efter reserve = 34,50 USD ved 150 USD/SOL
    engine._maybe_sweep(engine._signer(), 150.0)
    assert chain.sent == []  # kun i live
    engine.mode = "live"
    engine._maybe_sweep(engine._signer(), 150.0)
    assert len(chain.sent) == 1
    sender, receiver, lamports = _lamports(chain.sent[0].message)
    assert (sender, receiver) == (bot, phantom)
    assert lamports == pytest.approx(0.13 * SOL, rel=1e-6)  # 19,50 USD over bankroll på 15 USD
    assert engine.wallet_info()["swept_total_usd"] == pytest.approx(19.5)
    engine._maybe_sweep(engine._signer(), 150.0)
    assert len(chain.sent) == 1  # højst én gang pr. halve time


def test_auto_sweep_waits_for_threshold():
    phantom = str(Keypair().pubkey())
    engine, chain, bot, _ = _setup(withdraw_address=phantom, sweep_above_usd=5.0)
    engine.mode = "live"
    chain.balances[bot] = int(0.14 * SOL)  # 18 USD fri: kun 3 USD over bankroll
    engine._maybe_sweep(engine._signer(), 150.0)
    assert chain.sent == []


def test_wallet_info_shows_bot_and_phantom_balances():
    engine, chain, bot, phantom = _setup()
    chain.balances[bot] = int(0.3 * SOL)
    chain.balances[phantom] = int(2 * SOL)
    engine._sol_usd = (150.0, 0.0)
    info = engine.wallet_info(refresh=True, phantom=phantom)
    assert info["address"] == bot and info["sol"] == pytest.approx(0.3) and info["usd"] == pytest.approx(45.0)
    assert info["phantom"] == {"address": phantom, "sol": 2.0}
    assert "SOLANA_PRIVATE_KEY" not in str(info) or info["configured"]
    assert str(engine.settings.private_key) not in str(engine.status())  # nøglen vises aldrig


def test_wallet_routes_and_bulk_import(tmp_path, monkeypatch):
    from trader.app import create_app
    from trader.config import Settings

    phantom = str(Keypair().pubkey())
    engine, chain, bot, _ = _setup(withdraw_address=phantom)
    chain.balances[bot] = SOL
    monkeypatch.setattr(engine, "start", lambda: None)
    monkeypatch.setattr(engine, "stop", lambda: None)
    settings = dataclasses.replace(Settings.from_env(), db_path=str(tmp_path / "t.db"), host="127.0.0.1", app_username="", app_password="", app_password_hash="", session_secret="")
    with TestClient(create_app(settings=settings, meme_engine=engine)) as client:
        info = client.get("/api/meme/wallet", params={"phantom": phantom}).json()
        assert info["address"] == bot and info["withdraw_address"] == phantom
        deposit = client.post("/api/meme/wallet/deposit", json={"from_address": phantom, "sol": 0.05}).json()
        assert deposit["lamports"] == 50_000_000 and deposit["message"]
        assert client.post("/api/meme/wallet/deposit", json={"from_address": "x", "sol": 0.05}).status_code == 400
        paid = client.post("/api/meme/wallet/withdraw", json={"sol": 0.1}).json()
        assert paid["signature"] and paid["to"] == phantom
        assert client.post("/api/meme/wallet/withdraw", json={"sol": 5}).status_code == 400
        wallets = [str(Keypair().pubkey()) for _ in range(3)] + ["skrald"]
        body = client.post("/api/meme/wallets", json={"wallets": wallets}).json()
        assert body["import"]["added"] == 3 and len(body["import"]["errors"]) == 1
        assert len(body["copy_wallets"]) == 3
