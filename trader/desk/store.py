"""SQLite for handelsbordet: gruppechatten, forslag (et pr. markeret ticker) og tilstand."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from trader.desk.fmt import tidy

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    author TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    symbol TEXT,
    proposal_id INTEGER,
    data TEXT
);
CREATE INDEX IF NOT EXISTS messages_proposal ON messages (proposal_id);

CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    trading_day TEXT NOT NULL,
    symbol TEXT NOT NULL,
    status TEXT NOT NULL,
    flag TEXT,
    news TEXT,
    chart TEXT,
    skeptic TEXT,
    size TEXT,
    entry REAL,
    stop REAL,
    target REAL,
    qty INTEGER,
    ruling TEXT,
    expires_at TEXT,
    decided_at TEXT,
    order_id TEXT,
    fill_price REAL,
    exit_price REAL,
    exit_reason TEXT,
    pnl REAL,
    closed_at TEXT,
    shadow TEXT
);
CREATE INDEX IF NOT EXISTS proposals_day ON proposals (trading_day, symbol);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_spend (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    purpose TEXT NOT NULL,
    model TEXT NOT NULL,
    cost REAL NOT NULL
);
"""

JSON_FIELDS = ("flag", "news", "chart", "skeptic", "size", "shadow")
OPEN_STATUSES = ("placed",)
PENDING_STATUSES = ("awaiting",)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _decode(row: dict[str, Any]) -> dict[str, Any]:
    for field in JSON_FIELDS:
        if field in row:
            raw = row.get(field)
            try:
                row[field] = json.loads(raw) if raw else None
            except (TypeError, json.JSONDecodeError):
                row[field] = None
    if "data" in row:
        raw = row.get("data")
        try:
            row["data"] = json.loads(raw) if raw else None
        except (TypeError, json.JSONDecodeError):
            row["data"] = None
    return row


class DeskStore:
    def __init__(self, path: str) -> None:
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
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    # ------------------------------------------------------------------ tilstand
    def get_state(self, key: str, default: str | None = None) -> str | None:
        rows = self._all("SELECT value FROM state WHERE key = ?", (key,))
        return str(rows[0]["value"]) if rows else default

    def set_state(self, key: str, value: str) -> None:
        self._write(
            "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def get_json(self, key: str, default: Any = None) -> Any:
        raw = self.get_state(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default

    def set_json(self, key: str, value: Any) -> None:
        self.set_state(key, json.dumps(value, ensure_ascii=False))

    # ------------------------------------------------------------------ chatten
    def post(
        self,
        author: str,
        kind: str,
        text: str,
        *,
        symbol: str | None = None,
        proposal_id: int | None = None,
        data: dict | None = None,
    ) -> int:
        text = tidy(text)
        return self._write(
            "INSERT INTO messages (ts, author, kind, text, symbol, proposal_id, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                now_iso(),
                author,
                kind,
                text,
                symbol,
                proposal_id,
                json.dumps(data, ensure_ascii=False, default=str) if data else None,
            ),
        )

    def messages(self, after: int = 0, limit: int = 300) -> list[dict[str, Any]]:
        if after > 0:
            rows = self._all("SELECT * FROM messages WHERE id > ? ORDER BY id LIMIT ?", (after, limit))
        else:
            rows = self._all("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,))
            rows.reverse()
        return [_decode(row) for row in rows]

    def count_messages(self, author: str, day_prefix: str, kind: str = "chat") -> int:
        rows = self._all(
            "SELECT COUNT(*) AS n FROM messages WHERE author = ? AND kind = ? AND ts LIKE ?",
            (author, kind, day_prefix + "%"),
        )
        return int(rows[0]["n"]) if rows else 0

    # ------------------------------------------------------------------ forslag
    def create_proposal(self, trading_day: str, symbol: str, flag: dict) -> int:
        return self._write(
            "INSERT INTO proposals (created_at, trading_day, symbol, status, flag) VALUES (?, ?, ?, ?, ?)",
            (now_iso(), trading_day, symbol, "flagged", json.dumps(flag, ensure_ascii=False, default=str)),
        )

    def update_proposal(self, proposal_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = []
        values: list[Any] = []
        for name, value in fields.items():
            cols.append(f"{name} = ?")
            if name in JSON_FIELDS and value is not None:
                value = json.dumps(value, ensure_ascii=False, default=str)
            values.append(value)
        values.append(proposal_id)
        self._write(f"UPDATE proposals SET {', '.join(cols)} WHERE id = ?", tuple(values))

    def proposal(self, proposal_id: int) -> dict[str, Any] | None:
        rows = self._all("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
        return _decode(rows[0]) if rows else None

    def proposals(self, limit: int = 200) -> list[dict[str, Any]]:
        return [_decode(row) for row in self._all("SELECT * FROM proposals ORDER BY id DESC LIMIT ?", (limit,))]

    def proposals_on(self, trading_day: str) -> list[dict[str, Any]]:
        return [
            _decode(row)
            for row in self._all("SELECT * FROM proposals WHERE trading_day = ? ORDER BY id", (trading_day,))
        ]

    def proposals_with_status(self, *statuses: str) -> list[dict[str, Any]]:
        marks = ", ".join("?" for _ in statuses)
        return [
            _decode(row)
            for row in self._all(f"SELECT * FROM proposals WHERE status IN ({marks}) ORDER BY id", tuple(statuses))
        ]

    def flagged_today(self, trading_day: str) -> list[str]:
        rows = self._all("SELECT symbol FROM proposals WHERE trading_day = ?", (trading_day,))
        return [row["symbol"] for row in rows]

    def closed_trades(self) -> list[dict[str, Any]]:
        return [
            _decode(row)
            for row in self._all("SELECT * FROM proposals WHERE status = 'closed' AND pnl IS NOT NULL ORDER BY id")
        ]

    # ------------------------------------------------------------------ AI-forbrug
    def add_spend(self, day: str, purpose: str, model: str, cost: float) -> None:
        self._write(
            "INSERT INTO ai_spend (ts, day, purpose, model, cost) VALUES (?, ?, ?, ?, ?)",
            (now_iso(), day, purpose, model, float(cost)),
        )

    def spend_on(self, day: str) -> float:
        rows = self._all("SELECT COALESCE(SUM(cost), 0) AS total FROM ai_spend WHERE day = ?", (day,))
        return float(rows[0]["total"]) if rows else 0.0
