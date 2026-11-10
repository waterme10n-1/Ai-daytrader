"""Forbindelsestest, realistisk papir, skyggehandler, insider-alarm, udgifter, eksekvering, dagsrapport og Telegram."""

from __future__ import annotations

import base64
import time
import random
from datetime import datetime, timedelta, timezone

import pytest

import meme_fakes
from meme_fakes import FakeJup, make_engine, raw_pair, safe_report
from trader.meme.clients import Jupiter, Quote, SourceError
from trader.meme.config import SOL_MINT, USDC_MINT
from trader.meme.diagnostics import KNOWN_TOKEN
from trader.meme.learning import optimize_targets, simulate_path, verdict_for
from trader.meme.store import MemeStore
from trader.meme.telegram import TelegramBot, TelegramCommands
from trader.meme.wallet import PaperWallet, SolanaWallet

MINT = "GoodMint1111"


class ChainRPC:
    """Falsk Solana RPC med token-beholdninger pr. ejer."""

    def __init__(self):
        self.holdings: dict[tuple[str, str], int] = {}
        self.calls = 0

    def block_height(self):
        return 312_000_000

    def balance_lamports(self, pubkey):
        return 0

    def token_accounts(self, owner, mint):
        self.calls += 1
        return [{"pubkey": "acc", "amount": self.holdings.get((owner, mint), 0), "decimals": 6, "program": ""}]

    def signatures_for(self, address, limit=15):
        return []

    def close(self):
        pass


def _arm(dex, rug, mint=MINT, **report):
    dex.pairs[mint] = raw_pair(mint)
    dex.boosts.append(mint)
    rug.reports[mint] = safe_report(**report)


# --------------------------------------------------------------------------- forbindelsestest


def test_diagnostics_report_what_works_and_how_to_fix():
    engine, dex, rug, jup, _notes = make_engine(rpc=ChainRPC())
    dex.pairs[KNOWN_TOKEN] = raw_pair(KNOWN_TOKEN, symbol="BONK", price=0.00002)
    dex.pairs[USDC_MINT] = raw_pair(USDC_MINT, symbol="USDC", price=1.0)
    rug.reports[KNOWN_TOKEN] = safe_report()
    result = engine.run_diagnostics()
    checks = {item["id"]: item for item in result["checks"]}
    assert result["required_ok"] is True, result
    assert checks["jup_quote"]["detail"].startswith("0,01 SOL =")
    assert "Blokhøjde" in checks["rpc"]["detail"]
    assert engine.store.get_json("diagnostics")["total"] == result["total"]

    dex.fail = True
    rug.reports.clear()
    result = engine.run_diagnostics()
    checks = {item["id"]: item for item in result["checks"]}
    assert result["required_ok"] is False
    assert not checks["dex_pairs"]["ok"] and checks["dex_pairs"]["hint"]
    assert not checks["rugcheck"]["ok"] and "RugCheck" in checks["rugcheck"]["hint"]
    assert any("Forbindelsestest" in alert["text"] for alert in engine.status()["alerts"])


# --------------------------------------------------------------------------- realistisk papir


def test_paper_slippage_is_bigger_for_snipes_and_failures_cost_the_fee():
    store = MemeStore(":memory:")
    dex = meme_fakes.FakeDex()
    dex.pairs[MINT] = raw_pair(MINT)
    jup = FakeJup(dex)
    cfg = meme_fakes.settings(paper_slippage_pct=1.0, paper_sniper_extra_pct=2.0)
    wallet = PaperWallet(cfg, store, jup, rng=random.Random(1))
    normal = wallet.buy(MINT, 3.0, 150.0)
    snipe = wallet.buy(MINT, 3.0, 150.0, strategy="sniper")
    assert snipe.token_raw < normal.token_raw
    assert snipe.token_raw / normal.token_raw == pytest.approx(0.97 / 0.99, rel=1e-3)

    failing = PaperWallet(meme_fakes.settings(paper_fail_rate=1.0), MemeStore(":memory:"), jup, rng=random.Random(1))
    before = failing.cash_usd(150.0)
    fill = failing.buy(MINT, 3.0, 150.0)
    assert not fill.ok and "Simuleret" in fill.error
    assert failing.cash_usd(150.0) == pytest.approx(before - fill.fee_usd)


def test_failed_paper_buy_fee_is_in_the_books():
    engine, dex, rug, _jup, _notes = make_engine(meme_fakes.settings(paper_fail_rate=1.0))
    _arm(dex, rug)
    engine.tick(force_scan=True)
    assert engine.store.open_positions("paper") == []
    totals = engine._totals()
    assert totals["realized"] < 0
    assert engine._paper.cash_usd(150.0) == pytest.approx(totals["equity"], abs=1e-9)


# --------------------------------------------------------------------------- skyggehandler


def test_simulator_follows_the_exit_rules():
    rules = dict(tp=1.0, frac=1.0, sl=0.35, max_hold=60)
    assert simulate_path([[0, 1], [5, 1.5], [9, 2.2]], **rules) == (pytest.approx(1.0), "target")
    pnl, kind = simulate_path([[0, 1], [5, 0.5]], **rules)
    assert kind == "stop_loss" and pnl == pytest.approx(-0.5)  # et hul ned rammer fuldt
    assert simulate_path([[0, 1], [61, 1.2]], **rules)[1] == "time"
    partial = simulate_path([[0, 1], [5, 1.6], [10, 3.0], [15, 2.0]], tp=0.5, frac=0.5, sl=0.35, max_hold=60, trailing=0.3)
    assert partial[1] == "trailing" and partial[0] == pytest.approx(0.5 * 0.5 + 0.5 * 1.0)
    assert simulate_path([[0, 1], [3, 0.9]], rugged=True, **rules)[1] == "rug"
    assert verdict_for("rug", -0.9, True) == "dodged" and verdict_for("score", 0.5, False) == "missed"


def test_optimizer_finds_the_best_target():
    # Typisk nyt token: stiger til 2,5x og styrter.
    paths = [([[0, 1], [5, 1.6], [10, 2.5], [20, 0.4]], False) for _ in range(15)]
    paths += [([[0, 1], [3, 0.5]], True) for _ in range(10)]
    result = optimize_targets(paths, frac=1.0, max_hold=60, cost_pct=0.0)
    assert result["enough"] and result["n"] == 25
    best = result["best"]
    assert best["tp"] in {1.0, 2.0}
    worst_high = next(cell for cell in result["cells"] if cell["tp"] == 5.0 and cell["sl"] == best["sl"])
    assert best["avg"] > worst_high["avg"]


def test_rejected_tokens_are_followed_and_judged():
    engine, dex, rug, _jup, _notes = make_engine(meme_fakes.settings(shadows=True))
    _arm(dex, rug, mintAuthority="Dev1111")
    engine.tick(force_scan=True)
    shadows = engine.store.active_shadows()
    assert len(shadows) == 1 and shadows[0]["stage"] == "rug"
    dex.set_price(MINT, 0.0004)
    engine._track_shadows(force=True)
    old = (datetime.now(timezone.utc) - timedelta(minutes=400)).isoformat(timespec="seconds")
    engine.store.update_shadow(shadows[0]["id"], ts=old)
    engine._track_shadows(force=True)
    done = engine.store.done_shadows("paper")
    assert done and done[0]["verdict"] == "dodged" and done[0]["sim_pnl_pct"] < 0
    status = engine.status()["shadows"]
    assert status["summary"][0]["stage"] == "rug" and status["summary"][0]["dodged"] == 1


def test_missed_chances_teach_the_model_a_little():
    engine, dex, rug, _jup, _notes = make_engine(meme_fakes.settings(shadows=True))
    engine.params.min_score = 0.85  # alt afvises på score
    _arm(dex, rug)
    engine.tick(force_scan=True)
    shadow = engine.store.active_shadows()[0]
    assert shadow["stage"] == "score" and shadow["features"]
    before = dict(engine.model.weights)
    dex.set_price(MINT, 0.003)
    engine._track_shadows(force=True)
    engine.store.update_shadow(shadow["id"], ts=(datetime.now(timezone.utc) - timedelta(minutes=400)).isoformat(timespec="seconds"))
    engine._track_shadows(force=True)
    engine._learn_shadows()
    done = engine.store.done_shadows("paper")[0]
    assert done["verdict"] == "missed" and done["learned"] == 1
    assert engine.store.unlearned_shadows() == []
    assert engine.model.weights != before


def test_bought_tokens_feed_the_target_optimizer():
    engine, *_ = make_engine(meme_fakes.settings(shadows=True))
    store = engine.store
    for index in range(22):
        shadow_id = store.add_shadow({"mode": "paper", "mint": f"M{index}", "strategy": "sniper", "stage": "købt", "entry_price": 1.0})
        store.update_shadow(shadow_id, status="done", path=[[0, 1], [5, 1.4], [10, 2.3], [15, 0.6]], verdict="taken")
    result = engine.status()["shadows"]["optimizer"]
    assert result["enough"] and result["n"] == 22
    assert result["best"]["tp"] == 1.0
    assert result["current"]["tp"] == engine.sniper.take_profit_pct


# --------------------------------------------------------------------------- insider-alarm


def test_dev_dump_triggers_sell_and_blacklist():
    rpc = ChainRPC()
    engine, dex, rug, _jup, _notes = make_engine(meme_fakes.settings(dev_watch=True), rpc=rpc)
    _arm(dex, rug, creator="DevWallet111")
    engine.tick(force_scan=True)
    pos = engine.store.open_positions("paper")[0]
    watch = __import__("json").loads(pos["safety"])["watch"]
    assert watch["dev"] == "DevWallet111" and watch["holders"][:2] == ["owner0", "owner1"]
    rpc.holdings[("DevWallet111", MINT)] = 1_000_000
    for owner in watch["holders"]:
        rpc.holdings[(owner, MINT)] = 500_000
    engine._last_insider_check = 0
    engine.tick()  # første kig gemmer udgangspunktet
    assert engine.store.open_positions("paper")
    rpc.holdings[("DevWallet111", MINT)] = 200_000  # udvikleren sælger 80 %
    engine._last_insider_check = 0
    engine.tick()
    closed = engine.store.closed_positions("paper")
    assert closed and closed[0]["exit_reason"] == "rug_dev"
    assert engine.store.ever_rugged(MINT)


def test_holders_selling_together_triggers_sell():
    rpc = ChainRPC()
    engine, dex, rug, _jup, _notes = make_engine(meme_fakes.settings(dev_watch=True), rpc=rpc)
    _arm(dex, rug)
    engine.tick(force_scan=True)
    for index in range(3):
        rpc.holdings[(f"owner{index}", MINT)] = 1_000
    engine._last_insider_check = 0
    engine.tick()
    for index in range(3):
        rpc.holdings[(f"owner{index}", MINT)] = 500
    engine._last_insider_check = 0
    engine.tick()
    assert engine.store.closed_positions("paper")[0]["exit_reason"] == "rug_dev"


# --------------------------------------------------------------------------- udgifter


def test_goal_counts_hosting_and_other_costs():
    engine, *_ = make_engine(meme_fakes.settings(hosting_usd_per_month=5.0, other_costs_usd_per_month=10.0))
    goal = engine.status()["goal"]
    assert goal["fixed_monthly"] == 15.0
    assert goal["need_per_month"] == goal["target"] + 15.0
    assert goal["month_net"] == pytest.approx(-goal["month_fixed"])
    assert goal["projection"] == pytest.approx(-15.0)
    assert 0 < goal["month_fixed"] <= 15.0


# --------------------------------------------------------------------------- eksekvering


def test_swap_request_uses_jito_tip_or_urgent_priority():
    cfg = meme_fakes.settings()
    jup = Jupiter(cfg)
    sent = []
    jup.http.post = lambda path, payload, **_kw: sent.append(payload) or {"swapTransaction": "dHg="}
    quote = Quote(SOL_MINT, MINT, 1, 1, 0.1, {"outAmount": "1"})
    jup.swap_transaction(quote, "Pub", urgent=True)
    assert sent[-1]["prioritizationFeeLamports"]["priorityLevelWithMaxLamports"]["priorityLevel"] == "veryHigh"
    jup.swap_transaction(quote, "Pub", jito_tip=50_000)
    assert sent[-1]["prioritizationFeeLamports"] == {"jitoTipLamports": 50_000}
    jup.close()


class SwapChain:
    """Falsk kæde til live-salg: første salg fejler, andet lander."""

    def __init__(self, owner, mint):
        self.owner, self.mint = owner, mint
        self.tokens = 1_000_000
        self.sol = 1_000_000_000
        self.sent: list[str] = []

    def token_accounts(self, owner, mint):
        return [{"pubkey": "acc", "amount": self.tokens, "decimals": 6, "program": ""}]

    def balance_lamports(self, pubkey):
        return self.sol

    def send_transaction(self, b64):
        self.sent.append(b64)
        self.tokens = 0
        self.sol += 20_000_000
        return "sig" + str(len(self.sent))

    def signature_status(self, signature):
        return {"confirmationStatus": "confirmed", "err": None}


class RetryJup:
    def __init__(self, keypair):
        self.keypair = keypair
        self.slippages: list[int] = []
        self.urgent: list[bool] = []

    def quote(self, inp, out, amount, slippage_bps=None):
        self.slippages.append(slippage_bps)
        return Quote(inp, out, amount, 20_000_000, 0.1, {"outAmount": "20000000"})

    def swap_transaction(self, quote, pub, *, urgent=False, jito_tip=None):
        from solders.hash import Hash
        from solders.instruction import Instruction
        from solders.message import MessageV0
        from solders.pubkey import Pubkey
        from solders.transaction import VersionedTransaction

        self.urgent.append(urgent)
        if len(self.slippages) == 1:
            raise SourceError("Slippage overskredet")
        ix = Instruction(Pubkey.from_string("11111111111111111111111111111111"), bytes([2, 0, 0, 0] + [0] * 8), [])
        message = MessageV0.try_compile(self.keypair.pubkey(), [ix], [], Hash.default())
        return base64.b64encode(bytes(VersionedTransaction(message, [self.keypair]))).decode()


def test_urgent_live_sell_retries_with_double_slippage():
    from solders.keypair import Keypair

    keypair = Keypair()
    cfg = meme_fakes.settings(private_key=str(keypair), slippage_bps=300)
    chain = SwapChain(str(keypair.pubkey()), MINT)
    jup = RetryJup(keypair)
    wallet = SolanaWallet(cfg, chain, jup)
    fill = wallet.sell(MINT, 1_000_000, 150.0, urgent=True)
    assert fill.ok and fill.token_raw == 1_000_000
    assert jup.slippages == [300, 600] and jup.urgent == [True, True]
    calm = SolanaWallet(cfg, SwapChain(str(keypair.pubkey()), MINT), RetryJup(keypair))
    assert not calm.sell(MINT, 1_000_000, 150.0).ok  # normale salg prøves først igen næste runde


# --------------------------------------------------------------------------- dagsrapport og Telegram


def test_daily_report_is_sent_once_at_the_chosen_hour():
    import dataclasses

    engine, dex, rug, _jup, notes = make_engine()
    _arm(dex, rug)
    engine.tick(force_scan=True)
    dex.set_price(MINT, 0.0007)
    engine.tick()
    assert not [text for text in notes if text.startswith("Dagsrapport")]
    engine.settings = dataclasses.replace(engine.settings, report_hour_utc=datetime.now(timezone.utc).hour)
    engine.tick()
    reports = [text for text in notes if text.startswith("Dagsrapport")]
    assert len(reports) == 1
    assert "Lukkede handler i dag: 1" in reports[0] and "udgifter" in reports[0]
    engine.tick()
    assert len([text for text in notes if text.startswith("Dagsrapport")]) == 1
    assert engine.status()["last_report"]["text"] == reports[0]


def test_telegram_commands():
    engine, dex, rug, _jup, _notes = make_engine()
    _arm(dex, rug)
    engine.tick(force_scan=True)
    commands = TelegramCommands(engine)
    assert "Egenkapital" in commands.handle("/status")
    assert "MEME" in commands.handle("/positioner")
    assert "pause" in commands.handle("/pause") and engine.paused
    commands.handle("/fortsæt")
    assert not engine.paused
    assert "+150 %" in commands.handle("/maal 150") and engine.sniper.take_profit_pct == 1.5
    assert "Sikker" in commands.handle("/nodstop")
    assert not engine.kill_switch
    assert "sendt til salg" in commands.handle("/sælg meme")
    assert engine.store.open_positions("paper") == []
    commands.handle("/nodstop ja")
    assert engine.kill_switch
    assert "kender jeg ikke" in commands.handle("/hæv 100")
    assert "Dagsrapport" in commands.handle("/rapport")


def test_telegram_ignores_other_chats():
    engine, *_ = make_engine()
    bot = TelegramBot(engine, "token", "4242")
    assert bot.handle_update({"message": {"chat": {"id": 999}, "text": "/nodstop ja"}}) is None
    assert not engine.kill_switch
    assert "Egenkapital" in bot.handle_update({"message": {"chat": {"id": 4242}, "text": "/status@memebot"}})
    assert bot.handle_update({"message": {"chat": {"id": 4242}, "text": "hej"}}) is None
    bot._client.close()


def test_drawdown_halt_sells_everything_and_blocks_until_resume():
    engine, dex, rug, _jup, notes = make_engine(meme_fakes.settings(daily_loss_pct=0.9, max_drawdown_pct=0.5, max_trades_per_day=20, max_losses_per_day=0))  # tabsstoppet testes for sig
    mints = [f"Crash{i}" + "x" * 30 for i in range(5)]

    def buy_and_crash(mint):
        dex.pairs[mint] = raw_pair(mint)
        dex.boosts.append(mint)
        rug.reports[mint] = safe_report()
        engine.tick(force_scan=True)
        dex.set_price(mint, 0.00005)  # -95 % i ét hug

    for mint in mints[:2]:
        buy_and_crash(mint)
    engine.tick()
    assert engine.store.get_state("halt_dd:paper") is None  # 5,8 USD tabt, grænsen er 7,5
    buy_and_crash(mints[2])
    dex.pairs[mints[3]] = raw_pair(mints[3])
    dex.boosts.append(mints[3])
    rug.reports[mints[3]] = safe_report()
    engine.tick(force_scan=True)
    assert engine.store.get_state("halt_dd:paper") == "1"
    assert engine.store.open_positions("paper") == []
    assert any("Samlet tab" in text for text in notes)
    engine.tick(force_scan=True)
    assert engine.store.open_positions("paper") == []  # ingen nye køb
    engine.resume()
    from trader.meme.learning import Model

    engine.model = Model()  # modellen har lært af de tre tab; her testes kun, at køb er tilladt igen
    dex.pairs[mints[4]] = raw_pair(mints[4])
    dex.boosts.append(mints[4])
    rug.reports[mints[4]] = safe_report()
    engine.tick(force_scan=True)
    assert engine.store.open_positions("paper")  # målt fra nu efter Genoptag


def test_kill_keeps_retrying_failed_sells():
    engine, dex, rug, jup, _notes = make_engine()
    _arm(dex, rug)
    engine.tick(force_scan=True)
    jup.no_route.add(MINT)
    engine.kill()
    assert len(engine.store.open_positions("paper")) == 1  # salget fejlede
    jup.no_route.clear()
    engine.tick()
    assert engine.store.open_positions("paper") == []
    assert engine.store.closed_positions("paper")[0]["exit_reason"] == "kill"


def test_blocking_a_held_token_sells_it():
    from solders.keypair import Keypair

    engine, dex, rug, _jup, _notes = make_engine()
    mint = str(Keypair().pubkey())
    _arm(dex, rug, mint=mint)
    engine.tick(force_scan=True)
    assert len(engine.store.open_positions("paper")) == 1
    engine.block_token(mint, "MEME")
    assert engine.store.open_positions("paper") == []


class SlowResearcher:
    """Claude, der tænker længe. Imens trykker brugeren på nødstop."""

    def __init__(self, engine_ref):
        self.engine_ref = engine_ref
        self.started = __import__("threading").Event()
        self.release = __import__("threading").Event()

    def available(self, estimate=0.2):
        return True

    def budget_left(self):
        return 1.0

    def research(self, payload, lessons):
        from trader.meme.research import Verdict

        self.started.set()
        self.release.wait(10)
        return Verdict("buy", 90, [], ["fint"], "", "Ser godt ud.", 0.05)

    def daily_review(self, trades, params):
        return None


def test_kill_and_pause_work_during_a_long_round():
    import threading

    holder = {}
    researcher = SlowResearcher(holder)
    engine, dex, rug, _jup, _notes = make_engine(researcher=researcher)
    _arm(dex, rug)
    worker = threading.Thread(target=lambda: engine.tick(force_scan=True))
    worker.start()
    assert researcher.started.wait(5)
    started = time.monotonic()
    engine.kill()
    assert time.monotonic() - started < 5  # knappen hænger ikke
    assert engine.kill_switch
    researcher.release.set()
    worker.join(10)
    assert engine.store.open_positions("paper") == []  # Claude sagde køb, men nødstoppet vandt
    reasons = [row["reason"] for row in engine.store.recent_candidates("paper")]
    assert any("Nødstop eller pause" in reason for reason in reasons)


def test_manual_sell_and_settings_do_not_wait_for_a_long_round():
    import threading

    researcher = SlowResearcher({})
    engine, dex, rug, _jup, _notes = make_engine(researcher=researcher)
    _arm(dex, rug)
    researcher.release.set()
    engine.tick(force_scan=True)  # første køb uden ventetid
    first = engine.store.open_positions("paper")[0]
    researcher.release.clear()
    researcher.started.clear()
    second = "Second11111"
    _arm(dex, rug, mint=second, symbol="TWO")
    worker = threading.Thread(target=lambda: engine.tick(force_scan=True))
    worker.start()
    assert researcher.started.wait(5)
    started = time.monotonic()
    assert engine.sell_position(first["id"]) == "queued"
    engine.update_sniper({"take_profit_pct": 2.0})
    engine.pause(True)
    assert time.monotonic() - started < 5
    assert engine.sniper.take_profit_pct == 2.0
    researcher.release.set()
    worker.join(10)
    assert engine.store.get_position(first["id"])["status"] == "closed"  # solgt lige efter research
    assert [row["mint"] for row in engine.store.open_positions("paper")] == []  # pausen stoppede det nye køb


def test_positions_are_checked_right_after_research():
    import threading

    researcher = SlowResearcher({})
    engine, dex, rug, _jup, _notes = make_engine(researcher=researcher)
    _arm(dex, rug)
    researcher.release.set()
    engine.tick(force_scan=True)
    held = engine.store.open_positions("paper")[0]
    researcher.release.clear()
    researcher.started.clear()
    _arm(dex, rug, mint="Second11111", symbol="TWO")
    worker = threading.Thread(target=lambda: engine.tick(force_scan=True))
    worker.start()
    assert researcher.started.wait(5)
    dex.set_price(MINT, 0.0006)  # styrtdyk mens Claude tænker
    researcher.release.set()
    worker.join(10)
    assert engine.store.get_position(held["id"])["exit_reason"] == "stop_loss"
