"""Stop for nye køb i Memebot: SOL-filteret og tabsstoppet pr. dag. Salg berøres aldrig."""

from __future__ import annotations

import time
from dataclasses import replace

import meme_fakes
from meme_fakes import make_engine
from trader.meme import risk
from trader.meme.store import now_iso, utc_day


def _cfg(**changes):
    return replace(meme_fakes.settings(), **changes)


def test_sol_drop_is_measured_over_the_last_hour_only():
    now = 10_000.0
    # Kursen var 100 for 60 minutter siden og er 94 nu: -6 % den seneste time.
    samples = [(now - 3600, 100.0), (now - 1800, 98.0), (now - 60, 94.0)]
    assert round(risk.sol_drop_pct(samples, now), 6) == -6.0
    # Uden en kurs, der er gammel nok, er der ingen sammenligning, og så stopper filteret ikke.
    assert risk.sol_drop_pct([(now - 60, 94.0)], now) is None
    assert risk.sol_drop_pct([], now) is None


def test_sol_filter_stops_new_buys_at_the_limit_and_can_be_switched_off():
    assert risk.sol_halt(-5.0, _cfg(sol_filter_pct=5.0))
    assert not risk.sol_halt(-4.9, _cfg(sol_filter_pct=5.0))
    assert not risk.sol_halt(None, _cfg(sol_filter_pct=5.0))
    assert not risk.sol_halt(-30.0, _cfg(sol_filter_pct=0.0))


def test_losses_stop_new_buys_at_the_limit_and_can_be_switched_off():
    assert risk.losses_halt(3, _cfg(max_losses_per_day=3))
    assert not risk.losses_halt(2, _cfg(max_losses_per_day=3))
    assert not risk.losses_halt(10, _cfg(max_losses_per_day=0))


def _closed_loss(engine, index: int, pnl: float) -> None:
    position_id = engine.store.open_position(
        {
            "strategy": "momentum", "mode": "paper", "mint": f"Halt{index}" + "x" * 30, "symbol": f"H{index}",
            "source": "dex_boost", "status": "open", "opened_at": now_iso(), "entry_price_usd": 1.0,
            "cost_usd": 1.5, "token_raw": 1000, "remaining_raw": 1000, "fees_usd": 0.01,
        }
    )
    engine.store.update_position(position_id, status="closed", closed_at=now_iso(), pnl_usd=pnl, pnl_pct=pnl / 1.5, exit_reason="stop_loss", remaining_raw=0, learned=True)


def test_two_losing_trades_today_stop_new_entries_but_not_exits(tmp_path):
    engine, *_ = make_engine(_cfg(max_losses_per_day=2, sol_filter_pct=0.0))
    day = utc_day()
    _closed_loss(engine, 1, -0.5)
    assert engine._entry_halt(day) == ""  # ett tab: stadig plads
    _closed_loss(engine, 2, -0.4)
    text = engine._entry_halt(day)
    assert "2 handler med tab i dag" in text
    # Vindere tæller ikke som tab, og et tab på en anden dag tæller slet ikke.
    assert engine.store.losses_closed_on("paper", "2000-01-01") == 0
    assert engine.store.losses_closed_on("paper", day) == 2


def test_sol_drop_stops_entries_with_a_message_that_is_not_repeated(tmp_path):
    engine, *_ = make_engine(_cfg(sol_filter_pct=5.0, max_losses_per_day=0))
    now = time.time()
    engine._sol_hist = [(now - 3600, 100.0), (now - 60, 93.0)]
    text = engine._entry_halt(utc_day())
    assert text.startswith("SOL er ned 7,0 %")
    engine._note_entry_halt(utc_day(), text)
    engine._note_entry_halt(utc_day(), text)  # samme halve time: kun én besked
    notes = [event for event in engine.store.recent_events(50) if "SOL er ned" in event["message"]]
    assert len(notes) == 1
