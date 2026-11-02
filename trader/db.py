"""SQLite-lager for handler, AI-beslutninger, egenkapital, tilstand og hændelser."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    trading_day TEXT NOT NULL,
    mode TEXT NOT NULL,
    synthetic INTEGER NOT NULL DEFAULT 0,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty INTEGER NOT NULL,
    entry_price REAL,
    stop_price REAL,
    target_price REAL,
    exit_price REAL,
    pnl REAL,
    status TEXT NOT NULL,
    window TEXT NOT NULL,
    setup TEXT NOT NULL,
    confidence INTEGER,
    reasoning TEXT,
    order_id TEXT,
    exit_reason TEXT,
    opened_at TEXT,
    closed_at TEXT,
    ai_decision_id INTEGER
);

CREATE TABLE IF NOT EXISTS ai_decisions (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    trading_day TEXT NOT NULL,
    window TEXT NOT NULL,
    mode TEXT NOT NULL,
    synthetic INTEGER NOT NULL DEFAULT 0,
    symbol TEXT NOT NULL,
    decision TEXT NOT NULL,
    confidence INTEGER,
    reasoning TEXT,
    tighter_stop REAL,
    regime TEXT,
    market_summary TEXT,
    source TEXT NOT NULL,
    block_reason TEXT,
    outcome TEXT
);

CREATE TABLE IF NOT EXISTS daily_equity (
    id INTEGER PRIMARY KEY,
    trading_day TEXT NOT NULL,
    mode TEXT NOT NULL,
    synthetic INTEGER NOT NULL DEFAULT 0,
    start_equity REAL NOT NULL,
    end_equity REAL,
    pnl REAL,
    UNIQUE (trading_day, mode, synthetic)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    details TEXT
);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS researched (
    trading_day TEXT NOT NULL,
    window TEXT NOT NULL,
    symbol TEXT NOT NULL,
    synthetic INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (trading_day, window, symbol, synthetic)
);
"""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _write(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self._all(sql, params)
        return rows[0] if rows else None

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self._one("SELECT value FROM state WHERE key = ?", (key,))
        if row is None:
            return default
        return str(row["value"])

    def set_state(self, key: str, value: str) -> None:
        self._write(
            "INSERT INTO state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def add_event(self, level: str, message: str, details: str = "") -> None:
        self._write(
            "INSERT INTO events (ts, level, message, details) VALUES (?, ?, ?, ?)",
            (_now(), level, message, details),
        )

    def recent_events(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._all(
            "SELECT id, ts, level, message, details FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    def is_researched(self, trading_day: str, window: str, symbol: str, synthetic: bool) -> bool:
        row = self._one(
            "SELECT 1 AS ok FROM researched WHERE trading_day = ? AND window = ? AND symbol = ? AND synthetic = ?",
            (trading_day, window, symbol, int(synthetic)),
        )
        return row is not None

    def mark_researched(self, trading_day: str, window: str, symbol: str, synthetic: bool) -> None:
        self._write(
            "INSERT OR IGNORE INTO researched (trading_day, window, symbol, synthetic) VALUES (?, ?, ?, ?)",
            (trading_day, window, symbol, int(synthetic)),
        )

    def insert_decision(self, row: dict[str, Any]) -> int:
        return self._write(
            """
            INSERT INTO ai_decisions (
                created_at, trading_day, window, mode, synthetic, symbol, decision,
                confidence, reasoning, tighter_stop, regime, market_summary, source,
                block_reason, outcome
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.get("created_at") or _now(),
                row["trading_day"],
                row["window"],
                row["mode"],
                int(row.get("synthetic") or 0),
                row["symbol"],
                row["decision"],
                row.get("confidence"),
                row.get("reasoning") or "",
                row.get("tighter_stop"),
                row.get("regime") or "",
                row.get("market_summary") or "",
                row["source"],
                row.get("block_reason"),
                row.get("outcome"),
            ),
        )

    def update_decision(self, decision_id: int, outcome: str, block_reason: str | None = None) -> None:
        self._write(
            "UPDATE ai_decisions SET outcome = ?, block_reason = ? WHERE id = ?",
            (outcome, block_reason, decision_id),
        )

    def list_decisions(self, mode: str, synthetic: bool, limit: int = 300) -> list[dict[str, Any]]:
        return self._all(
            """
            SELECT * FROM ai_decisions
            WHERE mode = ? AND synthetic = ?
            ORDER BY id DESC LIMIT ?
            """,
            (mode, int(synthetic), limit),
        )

    def insert_trade(self, row: dict[str, Any]) -> int:
        return self._write(
            """
            INSERT INTO trades (
                created_at, trading_day, mode, synthetic, symbol, side, qty,
                entry_price, stop_price, target_price, exit_price, pnl, status,
                window, setup, confidence, reasoning, order_id, exit_reason,
                opened_at, closed_at, ai_decision_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.get("created_at") or _now(),
                row["trading_day"],
                row["mode"],
                int(row.get("synthetic") or 0),
                row["symbol"],
                row["side"],
                int(row["qty"]),
                row.get("entry_price"),
                row.get("stop_price"),
                row.get("target_price"),
                row.get("exit_price"),
                row.get("pnl"),
                row["status"],
                row["window"],
                row["setup"],
                row.get("confidence"),
                row.get("reasoning") or "",
                row.get("order_id"),
                row.get("exit_reason"),
                row.get("opened_at") or _now(),
                row.get("closed_at"),
                row.get("ai_decision_id"),
            ),
        )

    def open_trades(self, mode: str, synthetic: bool, trading_day: str | None = None) -> list[dict[str, Any]]:
        if trading_day is None:
            return self._all(
                "SELECT * FROM trades WHERE mode = ? AND synthetic = ? AND status = 'open' ORDER BY id",
                (mode, int(synthetic)),
            )
        return self._all(
            """
            SELECT * FROM trades
            WHERE mode = ? AND synthetic = ? AND status = 'open' AND trading_day = ?
            ORDER BY id
            """,
            (mode, int(synthetic), trading_day),
        )

    def count_trades_today(self, trading_day: str, mode: str, synthetic: bool) -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM trades WHERE trading_day = ? AND mode = ? AND synthetic = ?",
            (trading_day, mode, int(synthetic)),
        )
        return int(row["n"]) if row else 0

    def close_trade(
        self,
        trade_id: int,
        exit_price: float,
        pnl: float,
        reason: str,
        closed_at: str,
    ) -> None:
        self._write(
            """
            UPDATE trades
            SET status = 'closed', exit_price = ?, pnl = ?, exit_reason = ?, closed_at = ?
            WHERE id = ?
            """,
            (exit_price, pnl, reason, closed_at, trade_id),
        )

    def list_trades(self, mode: str, synthetic: bool, limit: int = 400) -> list[dict[str, Any]]:
        return self._all(
            """
            SELECT * FROM trades
            WHERE mode = ? AND synthetic = ?
            ORDER BY id DESC LIMIT ?
            """,
            (mode, int(synthetic), limit),
        )

    def closed_trades(self, mode: str, synthetic: bool) -> list[dict[str, Any]]:
        return self._all(
            """
            SELECT * FROM trades
            WHERE mode = ? AND synthetic = ? AND status = 'closed' AND pnl IS NOT NULL
            ORDER BY id
            """,
            (mode, int(synthetic)),
        )

    def get_day(self, trading_day: str, mode: str, synthetic: bool) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM daily_equity WHERE trading_day = ? AND mode = ? AND synthetic = ?",
            (trading_day, mode, int(synthetic)),
        )

    def insert_day_start(self, trading_day: str, mode: str, synthetic: bool, start_equity: float) -> None:
        self._write(
            """
            INSERT OR IGNORE INTO daily_equity (trading_day, mode, synthetic, start_equity)
            VALUES (?, ?, ?, ?)
            """,
            (trading_day, mode, int(synthetic), start_equity),
        )

    def set_day_end(self, trading_day: str, mode: str, synthetic: bool, end_equity: float) -> None:
        row = self.get_day(trading_day, mode, synthetic)
        start = float(row["start_equity"]) if row else end_equity
        if row is None:
            self.insert_day_start(trading_day, mode, synthetic, end_equity)
        self._write(
            """
            UPDATE daily_equity
            SET end_equity = ?, pnl = ?
            WHERE trading_day = ? AND mode = ? AND synthetic = ?
            """,
            (end_equity, end_equity - start, trading_day, mode, int(synthetic)),
        )

    def list_equity(self, mode: str, synthetic: bool) -> list[dict[str, Any]]:
        return self._all(
            """
            SELECT * FROM daily_equity
            WHERE mode = ? AND synthetic = ?
            ORDER BY trading_day
            """,
            (mode, int(synthetic)),
        )

    def last_end_equity(self, mode: str, synthetic: bool, default: float) -> float:
        rows = self.list_equity(mode, synthetic)
        if not rows:
            return default
        last = rows[-1]
        if last["end_equity"] is not None:
            return float(last["end_equity"])
        return float(last["start_equity"])
