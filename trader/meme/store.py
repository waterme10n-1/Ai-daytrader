"""SQLite-lager for memecoin-botten. Egen fil, så aktiemotoren og memebotten ikke deler forbindelse."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    entry_price_usd REAL NOT NULL,
    cost_usd REAL NOT NULL,
    token_raw INTEGER NOT NULL,
    remaining_raw INTEGER NOT NULL,
    sol_spent REAL,
    fees_usd REAL NOT NULL DEFAULT 0,
    proceeds_usd REAL NOT NULL DEFAULT 0,
    peak_ratio REAL NOT NULL DEFAULT 1,
    last_ratio REAL NOT NULL DEFAULT 1,
    last_price_usd REAL,
    partial_taken INTEGER NOT NULL DEFAULT 0,
    liquidity_entry REAL,
    liquidity_last REAL,
    pnl_usd REAL,
    pnl_pct REAL,
    exit_reason TEXT,
    score REAL,
    features TEXT,
    safety TEXT,
    ai TEXT,
    reasoning TEXT,
    pair_url TEXT,
    signatures TEXT,
    exit_failures INTEGER NOT NULL DEFAULT 0,
    learned INTEGER NOT NULL DEFAULT 0,
    strategy TEXT NOT NULL DEFAULT 'momentum'
);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT,
    source TEXT,
    stage TEXT NOT NULL,
    passed INTEGER NOT NULL,
    reason TEXT,
    score REAL,
    features TEXT,
    pair_url TEXT
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

CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    position_id INTEGER
);

CREATE TABLE IF NOT EXISTS ai_spend (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    purpose TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    searches INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS equity (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    equity REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS days (
    day TEXT NOT NULL,
    mode TEXT NOT NULL,
    start_equity REAL NOT NULL,
    end_equity REAL,
    PRIMARY KEY (day, mode)
);

CREATE TABLE IF NOT EXISTS shadows (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT,
    strategy TEXT NOT NULL,
    stage TEXT NOT NULL,
    reason TEXT,
    entry_price REAL NOT NULL,
    liquidity_entry REAL,
    features TEXT,
    path TEXT,
    peak_ratio REAL NOT NULL DEFAULT 1,
    low_ratio REAL NOT NULL DEFAULT 1,
    last_ratio REAL NOT NULL DEFAULT 1,
    rugged INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'tracking',
    sim_pnl_pct REAL,
    sim_exit TEXT,
    verdict TEXT,
    learned INTEGER NOT NULL DEFAULT 0,
    done_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_shadows_status ON shadows (status, id);
CREATE INDEX IF NOT EXISTS idx_shadows_mint ON shadows (mint, ts);
CREATE INDEX IF NOT EXISTS idx_positions_mode_status ON positions (mode, status);
CREATE INDEX IF NOT EXISTS idx_candidates_mint ON candidates (mint, ts);
CREATE INDEX IF NOT EXISTS idx_equity_mode ON equity (mode, ts);
"""

POSITION_FIELDS = (
    "strategy",
    "mode",
    "mint",
    "symbol",
    "name",
    "source",
    "status",
    "opened_at",
    "entry_price_usd",
    "cost_usd",
    "token_raw",
    "remaining_raw",
    "sol_spent",
    "fees_usd",
    "liquidity_entry",
    "liquidity_last",
    "last_price_usd",
    "score",
    "features",
    "safety",
    "ai",
    "reasoning",
    "pair_url",
    "signatures",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_day(moment: datetime | None = None) -> str:
    return (moment or datetime.now(timezone.utc)).astimezone(timezone.utc).date().isoformat()


def _json(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


class MemeStore:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(positions)")}
            if "strategy" not in columns:
                self._conn.execute("ALTER TABLE positions ADD COLUMN strategy TEXT NOT NULL DEFAULT 'momentum'")
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

    # Tilstand ---------------------------------------------------------------
    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self._one("SELECT value FROM state WHERE key = ?", (key,))
        return default if row is None else str(row["value"])

    def set_state(self, key: str, value: str) -> None:
        self._write(
            "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def get_json(self, key: str, default: Any = None) -> Any:
        return loads(self.get_state(key), default)

    def set_json(self, key: str, value: Any) -> None:
        self.set_state(key, _json(value))

    # Hændelser --------------------------------------------------------------
    def add_event(self, level: str, message: str, details: str = "") -> None:
        self._write(
            "INSERT INTO events (ts, level, message, details) VALUES (?, ?, ?, ?)",
            (now_iso(), level, message, details),
        )

    def recent_events(self, limit: int = 150) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    # Kandidater -------------------------------------------------------------
    def add_candidate(
        self,
        *,
        mode: str,
        mint: str,
        symbol: str,
        source: str,
        stage: str,
        passed: bool,
        reason: str,
        score: float | None = None,
        features: dict | None = None,
        pair_url: str = "",
    ) -> int:
        return self._write(
            """
            INSERT INTO candidates (ts, mode, mint, symbol, source, stage, passed, reason, score, features, pair_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (now_iso(), mode, mint, symbol, source, stage, int(passed), reason, score, _json(features), pair_url),
        )

    def recent_candidates(self, mode: str, limit: int = 120) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM candidates WHERE mode = ? ORDER BY id DESC LIMIT ?", (mode, limit))

    def last_candidate_ts(self, mint: str) -> str | None:
        row = self._one("SELECT ts FROM candidates WHERE mint = ? ORDER BY id DESC LIMIT 1", (mint,))
        return row["ts"] if row else None

    def prune(self, keep_candidates: int = 6000, keep_events: int = 4000, keep_equity: int = 20000) -> None:
        for table, keep in (("candidates", keep_candidates), ("events", keep_events), ("equity", keep_equity)):
            self._write(
                f"DELETE FROM {table} WHERE id <= (SELECT COALESCE(MAX(id), 0) - ? FROM {table})",
                (keep,),
            )

    # Positioner -------------------------------------------------------------
    def open_position(self, row: dict[str, Any]) -> int:
        values = []
        for name in POSITION_FIELDS:
            value = row.get(name)
            if name == "strategy":
                value = value or "momentum"
            if name in {"features", "safety", "ai", "signatures"}:
                value = _json(value)
            values.append(value)
        placeholders = ", ".join("?" for _ in POSITION_FIELDS)
        return self._write(
            f"INSERT INTO positions ({', '.join(POSITION_FIELDS)}) VALUES ({placeholders})",
            tuple(values),
        )

    def update_position(self, position_id: int, **changes: Any) -> None:
        if not changes:
            return
        parts = []
        values = []
        for key, value in changes.items():
            if key in {"features", "safety", "ai", "signatures"}:
                value = _json(value)
            if isinstance(value, bool):
                value = int(value)
            parts.append(f"{key} = ?")
            values.append(value)
        values.append(position_id)
        self._write(f"UPDATE positions SET {', '.join(parts)} WHERE id = ?", tuple(values))

    def get_position(self, position_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM positions WHERE id = ?", (position_id,))

    def open_positions(self, mode: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM positions WHERE mode = ? AND status = 'open' ORDER BY id", (mode,))

    def list_positions(self, mode: str, limit: int = 300) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM positions WHERE mode = ? ORDER BY id DESC LIMIT ?", (mode, limit))

    def closed_positions(self, mode: str) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM positions WHERE mode = ? AND status = 'closed' AND pnl_usd IS NOT NULL ORDER BY id",
            (mode,),
        )

    def unlearned_closed(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM positions WHERE status = 'closed' AND learned = 0 ORDER BY id")

    def trades_opened_on(self, mode: str, day: str, strategy: str | None = None) -> int:
        if strategy is None:
            row = self._one(
                "SELECT COUNT(*) AS n FROM positions WHERE mode = ? AND substr(opened_at, 1, 10) = ?",
                (mode, day),
            )
        else:
            row = self._one(
                "SELECT COUNT(*) AS n FROM positions WHERE mode = ? AND substr(opened_at, 1, 10) = ? AND strategy = ?",
                (mode, day, strategy),
            )
        return int(row["n"]) if row else 0

    def recent_loss_on(self, mint: str, since_iso: str) -> bool:
        row = self._one(
            """
            SELECT 1 AS hit FROM positions
            WHERE mint = ? AND status = 'closed' AND pnl_usd < 0 AND closed_at >= ?
            LIMIT 1
            """,
            (mint, since_iso),
        )
        return row is not None

    def ever_rugged(self, mint: str) -> bool:
        row = self._one(
            "SELECT 1 AS hit FROM positions WHERE mint = ? AND exit_reason LIKE 'rug%' LIMIT 1",
            (mint,),
        )
        return row is not None

    # Skyggehandler ----------------------------------------------------------
    def add_shadow(self, row: dict[str, Any]) -> int:
        return self._write(
            """
            INSERT INTO shadows (ts, mode, mint, symbol, strategy, stage, reason, entry_price, liquidity_entry, features, path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_iso(),
                row["mode"],
                row["mint"],
                row.get("symbol") or "?",
                row["strategy"],
                row["stage"],
                row.get("reason") or "",
                float(row["entry_price"]),
                row.get("liquidity_entry"),
                _json(row.get("features")),
                _json([[0.0, 1.0]]),
            ),
        )

    def shadow_recent(self, mint: str, since_iso: str) -> bool:
        return self._one("SELECT 1 AS hit FROM shadows WHERE mint = ? AND ts >= ? LIMIT 1", (mint, since_iso)) is not None

    def active_shadows(self, limit: int = 500) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM shadows WHERE status = 'tracking' ORDER BY id LIMIT ?", (limit,))

    def count_active_shadows(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM shadows WHERE status = 'tracking'")
        return int(row["n"]) if row else 0

    def update_shadow(self, shadow_id: int, **changes: Any) -> None:
        if not changes:
            return
        parts, values = [], []
        for key, value in changes.items():
            if key in {"features", "path"}:
                value = _json(value)
            if isinstance(value, bool):
                value = int(value)
            parts.append(f"{key} = ?")
            values.append(value)
        values.append(shadow_id)
        self._write(f"UPDATE shadows SET {', '.join(parts)} WHERE id = ?", tuple(values))

    def done_shadows(self, mode: str, strategy: str | None = None, limit: int = 2000) -> list[dict[str, Any]]:
        if strategy:
            return self._all(
                "SELECT * FROM shadows WHERE mode = ? AND status = 'done' AND strategy = ? ORDER BY id DESC LIMIT ?",
                (mode, strategy, limit),
            )
        return self._all("SELECT * FROM shadows WHERE mode = ? AND status = 'done' ORDER BY id DESC LIMIT ?", (mode, limit))

    def unlearned_shadows(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM shadows WHERE status = 'done' AND learned = 0 ORDER BY id")

    def recent_shadows(self, mode: str, limit: int = 80) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM shadows WHERE mode = ? ORDER BY id DESC LIMIT ?", (mode, limit))

    def prune_shadows(self, keep: int = 5000) -> None:
        self._write(
            "DELETE FROM shadows WHERE status = 'done' AND id <= (SELECT COALESCE(MAX(id), 0) - ? FROM shadows)",
            (keep,),
        )

    # Læring -----------------------------------------------------------------
    def add_lesson(self, kind: str, text: str, position_id: int | None = None) -> None:
        self._write(
            "INSERT INTO lessons (ts, kind, text, position_id) VALUES (?, ?, ?, ?)",
            (now_iso(), kind, text, position_id),
        )

    def recent_lessons(self, limit: int = 40) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM lessons ORDER BY id DESC LIMIT ?", (limit,))

    # AI-omkostninger --------------------------------------------------------
    def add_ai_spend(self, purpose: str, model: str, input_tokens: int, output_tokens: int, searches: int, cost: float) -> None:
        self._write(
            """
            INSERT INTO ai_spend (ts, day, purpose, model, input_tokens, output_tokens, searches, cost_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (now_iso(), utc_day(), purpose, model, input_tokens, output_tokens, searches, cost),
        )

    def ai_spend_on(self, day: str) -> float:
        row = self._one("SELECT COALESCE(SUM(cost_usd), 0) AS total FROM ai_spend WHERE day = ?", (day,))
        return float(row["total"]) if row else 0.0

    def ai_spend_since(self, day: str) -> float:
        row = self._one("SELECT COALESCE(SUM(cost_usd), 0) AS total FROM ai_spend WHERE day >= ?", (day,))
        return float(row["total"]) if row else 0.0

    def ai_spend_total(self) -> float:
        row = self._one("SELECT COALESCE(SUM(cost_usd), 0) AS total FROM ai_spend")
        return float(row["total"]) if row else 0.0

    # Egenkapital ------------------------------------------------------------
    def add_equity(self, mode: str, equity: float) -> None:
        self._write("INSERT INTO equity (ts, mode, equity) VALUES (?, ?, ?)", (now_iso(), mode, equity))

    def equity_series(self, mode: str, limit: int = 600) -> list[dict[str, Any]]:
        rows = self._all("SELECT ts, equity FROM equity WHERE mode = ? ORDER BY id DESC LIMIT ?", (mode, limit))
        return list(reversed(rows))

    def ensure_day(self, day: str, mode: str, equity: float) -> bool:
        if self._one("SELECT 1 AS ok FROM days WHERE day = ? AND mode = ?", (day, mode)):
            return False
        self._write("INSERT OR IGNORE INTO days (day, mode, start_equity) VALUES (?, ?, ?)", (day, mode, equity))
        return True

    def set_day_end(self, day: str, mode: str, equity: float) -> None:
        self._write("UPDATE days SET end_equity = ? WHERE day = ? AND mode = ?", (equity, day, mode))

    def get_day(self, day: str, mode: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM days WHERE day = ? AND mode = ?", (day, mode))

    def list_days(self, mode: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM days WHERE mode = ? ORDER BY day", (mode,))
