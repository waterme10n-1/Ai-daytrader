"""Genstart, oplåsning af rigtige penge og et helt live-forløb på en falsk Solana-kæde."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import pytest
from solders.keypair import Keypair

import meme_fakes
from meme_fakes import FakeDex, FakeJup, FakeRug, raw_pair, safe_report
from trader.meme.config import MEME_LIVE_PHRASE, SOL_MINT
from trader.meme.engine import MemeClients, MemeEngine
from trader.meme.store import MemeStore

MINT = "GoodMint1111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def _engine_on(path, cfg, dex, rug, jup, rpc=None):
    clients = MemeClients(cfg, dex=dex, rug=rug, jup=jup, x=None, rpc=rpc)
    notes: list[str] = []
    return MemeEngine(cfg, store=MemeStore(str(path)), clients=clients, notifier=notes.append)


def test_everything_survives_a_restart(tmp_path):
    path = tmp_path / "meme.db"
    cfg = meme_fakes.settings(db_path=str(path))
    dex, rug = FakeDex(), FakeRug()
    jup = FakeJup(dex)
    first = _engine_on(path, cfg, dex, rug, jup)
    dex.pairs[MINT] = raw_pair(MINT)
    dex.boosts.append(MINT)
    rug.reports[MINT] = safe_report()
    first.tick(force_scan=True)
    other = "Other1111"
    dex.pairs[other] = raw_pair(other)
    dex.boosts.append(other)
    rug.reports[other] = safe_report()
    first.tick(force_scan=True)
    dex.set_price(other, 0.0007)
    first.tick()  # én lukket handel, så modellen lærer
    first.update_sniper({"take_profit_pct": 2.5})
    wallet = str(Keypair().pubkey())
    first.add_wallet(wallet)
    blocked = str(Keypair().pubkey())
    first.block_token(blocked, "BAD")
    first.pause(True)
    cash = first._paper.cash_usd(0.0)
    weights = dict(first.model.weights)
    first.store.close()

    second = _engine_on(path, cfg, dex, rug, jup)
    assert second.paused and second.sniper.take_profit_pct == 2.5
    assert second._all_wallets() == (wallet,)
    assert [row["mint"] for row in second._blacklist()] == [blocked]
    assert second.model.weights == weights and second.model.updates == 1
    assert second._paper.cash_usd(0.0) == pytest.approx(cash)
    assert [row["mint"] for row in second.store.open_positions("paper")] == [MINT]
    dex.set_price(MINT, 0.0007)
    second.tick()  # positionen fra før genstarten passes stadig
    assert second.store.open_positions("paper") == []


def _paper_record(store: MemeStore, trades: int = 20, days: int = 3):
    now = datetime.now(timezone.utc)
    for index in range(trades):
        position_id = store.open_position(
            {
                "mode": "paper",
                "mint": f"Rec{index}",
                "symbol": f"R{index}",
                "source": "dex_boost",
                "status": "open",
                "opened_at": now.isoformat(),
                "entry_price_usd": 1.0,
                "cost_usd": 3.0,
                "token_raw": 1000,
                "remaining_raw": 1000,
                "fees_usd": 0.01,
            }
        )
        pnl = 0.6 if index % 3 else -0.4
        closed = (now - timedelta(days=index % days)).isoformat(timespec="seconds")
        store.update_position(position_id, status="closed", closed_at=closed, pnl_usd=pnl, pnl_pct=pnl / 3, exit_reason="target", remaining_raw=0, learned=True)


def test_live_unlock_flow_and_restart(tmp_path):
    path = tmp_path / "meme.db"
    key = Keypair()
    cfg = meme_fakes.settings(db_path=str(path), allow_live=True, private_key=str(key))
    dex, rug = FakeDex(), FakeRug()
    jup = FakeJup(dex)
    engine = _engine_on(path, cfg, dex, rug, jup)
    ok, message = engine.request_live(MEME_LIVE_PHRASE)
    assert not ok and "papirhandler" in message
    _paper_record(engine.store)
    assert engine._unlock()["ready_for_phrase"]
    assert not engine.request_live("jeg forstår")[0]
    ok, _message = engine.request_live(MEME_LIVE_PHRASE)
    assert ok and engine.mode == "live"
    engine.store.close()

    again = _engine_on(path, cfg, dex, rug, jup)
    assert again.mode == "live"  # kravene holder stadig efter genstart
    again.return_to_paper()
    assert again.mode == "paper"
    again.store.close()

    without_flag = _engine_on(path, meme_fakes.settings(db_path=str(path), allow_live=False, private_key=str(key)), dex, rug, jup)
    assert without_flag.request_live(MEME_LIVE_PHRASE)[0] is False


class LiveChain:
    """En lille falsk Solana: SOL-saldo, token-saldi og transaktioner fra Jupiter."""

    def __init__(self, owner: str, lamports: int) -> None:
        self.owner = owner
        self.lamports = lamports
        self.tokens: dict[str, int] = {}
        self.pending = None
        self.sent: list[str] = []
        self.closed_accounts = 0

    def balance_lamports(self, pubkey):
        return self.lamports if pubkey == self.owner else 0

    def token_accounts(self, owner, mint):
        if owner != self.owner:
            return [{"pubkey": "x", "amount": 1_000_000, "decimals": 6, "program": TOKEN_PROGRAM}]
        if mint not in self.tokens:
            return []
        return [{"pubkey": str(Keypair().pubkey()), "amount": self.tokens[mint], "decimals": 6, "program": TOKEN_PROGRAM}]

    def latest_blockhash(self):
        return "11111111111111111111111111111111", 100

    def send_transaction(self, b64):
        self.sent.append(b64)
        quote = self.pending
        self.pending = None
        if quote is None:
            self.closed_accounts += 1
            for mint in [mint for mint, amount in self.tokens.items() if amount == 0]:
                del self.tokens[mint]
            self.lamports += 2_039_280  # rent tilbage
            return f"close{len(self.sent)}"
        if quote.input_mint == SOL_MINT:
            self.lamports -= quote.in_amount + 5_000
            self.tokens[quote.output_mint] = self.tokens.get(quote.output_mint, 0) + quote.out_amount
        else:
            self.tokens[quote.input_mint] -= quote.in_amount
            self.lamports += quote.out_amount - 5_000
        return f"swap{len(self.sent)}"

    def signature_status(self, signature):
        return {"confirmationStatus": "confirmed", "err": None}

    def signatures_for(self, address, limit=15):
        return []

    def block_height(self):
        return 1

    def close(self):
        pass


class LiveJup(FakeJup):
    def __init__(self, dex, chain, keypair):
        super().__init__(dex)
        self.chain = chain
        self.keypair = keypair

    def swap_transaction(self, quote, pub, *, urgent=False, jito_tip=None):
        from solders.hash import Hash
        from solders.instruction import Instruction
        from solders.message import MessageV0
        from solders.pubkey import Pubkey
        from solders.signature import Signature
        from solders.transaction import VersionedTransaction

        assert pub == str(self.keypair.pubkey())
        self.chain.pending = quote
        ix = Instruction(Pubkey.from_string("11111111111111111111111111111111"), bytes([2, 0, 0, 0] + [0] * 8), [])
        message = MessageV0.try_compile(self.keypair.pubkey(), [ix], [], Hash.default())
        return base64.b64encode(bytes(VersionedTransaction.populate(message, [Signature.default()]))).decode()


def test_live_buy_and_sell_on_a_fake_chain():
    key = Keypair()
    owner = str(key.pubkey())
    chain = LiveChain(owner, lamports=200_000_000)  # 0,2 SOL = 30 USD
    dex, rug = FakeDex(), FakeRug()
    jup = LiveJup(dex, chain, key)
    cfg = meme_fakes.settings(allow_live=True, private_key=str(key))
    clients = MemeClients(cfg, dex=dex, rug=rug, jup=jup, x=None, rpc=chain)
    notes: list[str] = []
    engine = MemeEngine(cfg, store=MemeStore(":memory:"), clients=clients, notifier=notes.append)
    engine.mode = "live"
    dex.pairs[MINT] = raw_pair(MINT)
    dex.boosts.append(MINT)
    rug.reports[MINT] = safe_report()
    engine.tick(force_scan=True)
    rows = engine.store.open_positions("live")
    assert len(rows) == 1
    position = rows[0]
    assert position["token_raw"] == chain.tokens[MINT] > 0
    assert position["cost_usd"] == pytest.approx(3.0)
    assert chain.lamports == pytest.approx(200_000_000 - 20_000_000 - 5_000)
    assert engine.wallet_info()["sol"] == pytest.approx(chain.lamports / 1e9)

    dex.set_price(MINT, 0.0007)  # stop-loss
    engine.tick()
    closed = engine.store.closed_positions("live")
    assert closed and closed[0]["exit_reason"] == "stop_loss"
    assert MINT not in chain.tokens  # tom token-konto lukket, rent tilbage
    assert chain.closed_accounts == 1
    assert closed[0]["pnl_usd"] == pytest.approx(-0.93, abs=0.05)
    assert engine.store.closed_positions("paper") == []  # papir og live holdes adskilt
    assert any("Lukket MEME" in text for text in notes)


def test_live_buy_respects_sol_reserve():
    key = Keypair()
    chain = LiveChain(str(key.pubkey()), lamports=25_000_000)  # 0,025 SOL: kun 0,005 over reserven
    dex, rug = FakeDex(), FakeRug()
    jup = LiveJup(dex, chain, key)
    cfg = meme_fakes.settings(allow_live=True, private_key=str(key))
    engine = MemeEngine(cfg, store=MemeStore(":memory:"), clients=MemeClients(cfg, dex=dex, rug=rug, jup=jup, x=None, rpc=chain), notifier=lambda _t: None)
    engine.mode = "live"
    dex.pairs[MINT] = raw_pair(MINT)
    dex.boosts.append(MINT)
    rug.reports[MINT] = safe_report()
    engine.tick(force_scan=True)
    assert engine.store.open_positions("live") == []
    assert chain.sent == []
    reasons = [row["reason"] for row in engine.store.recent_candidates("live")]
    assert any("fri kapital" in reason for reason in reasons)
