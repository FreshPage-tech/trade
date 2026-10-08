from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trading_engine.core.broker import Bar


class SQLiteStore:
    """Single-host SQLite cache and append-only audit log; WAL supports concurrent readers."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS bars (
                    symbol TEXT NOT NULL, timeframe TEXT NOT NULL, timestamp TEXT NOT NULL,
                    payload TEXT NOT NULL, cached_at TEXT NOT NULL,
                    PRIMARY KEY(symbol, timeframe, timestamp));
                CREATE INDEX IF NOT EXISTS idx_bars_lookup ON bars(symbol, timeframe, timestamp);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL, details TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS managed_trades (
                    symbol TEXT PRIMARY KEY, quantity REAL NOT NULL, entry_price REAL NOT NULL,
                    stop_price REAL NOT NULL, target_price REAL NOT NULL, opened_at TEXT NOT NULL,
                    entry_order_id TEXT NOT NULL, stop_order_id TEXT,
                    strategy TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', exit_order_id TEXT);
                CREATE TABLE IF NOT EXISTS runtime_state (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
            """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(managed_trades)")}
            if "exit_order_id" not in columns:
                conn.execute("ALTER TABLE managed_trades ADD COLUMN exit_order_id TEXT")

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def get_bars(self, symbol: str, timeframe: str, start: datetime, end: datetime,
                 ttl_seconds: int) -> list[Bar] | None:
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            rows = conn.execute("SELECT payload,cached_at FROM bars WHERE symbol=? AND timeframe=? AND timestamp>=? AND timestamp<=? ORDER BY timestamp",
                                (symbol, timeframe, start.isoformat(), end.isoformat())).fetchall()
        if not rows or any(now - datetime.fromisoformat(row["cached_at"]) > timedelta(seconds=ttl_seconds) for row in rows):
            return None
        return [Bar.model_validate_json(row["payload"]) for row in rows]

    def put_bars(self, symbol: str, timeframe: str, bars: list[Bar]) -> None:
        cached_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.executemany("INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?)",
                             [(symbol, timeframe, bar.timestamp.isoformat(), bar.model_dump_json(), cached_at) for bar in bars])

    def audit(self, event_type: str, details: dict) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO audit_log(timestamp,event_type,details) VALUES(?,?,?)",
                         (datetime.now(timezone.utc).isoformat(), event_type, json.dumps(details, default=str, sort_keys=True)))

    def save_managed_trade(self, trade: dict) -> None:
        with self._connect() as conn:
            conn.execute("""INSERT OR REPLACE INTO managed_trades
                (symbol,quantity,entry_price,stop_price,target_price,opened_at,entry_order_id,stop_order_id,strategy,status,exit_order_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (trade["symbol"], trade["quantity"], trade["entry_price"],
                trade["stop_price"], trade["target_price"], trade["opened_at"], trade["entry_order_id"],
                trade.get("stop_order_id"), trade["strategy"], trade.get("status", "open"), trade.get("exit_order_id")))

    def managed_trades(self, status: str | None = "open") -> list[dict]:
        with self._connect() as conn:
            if status is None:
                rows = conn.execute("SELECT * FROM managed_trades ORDER BY opened_at").fetchall()
            else:
                rows = conn.execute("SELECT * FROM managed_trades WHERE status=? ORDER BY opened_at", (status,)).fetchall()
        return [dict(row) for row in rows]

    def update_managed_trade(self, symbol: str, **fields) -> None:
        allowed = {"stop_order_id", "status", "quantity", "entry_price", "exit_order_id"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        sql = ",".join(f"{key}=?" for key in updates)
        with self._connect() as conn:
            conn.execute(f"UPDATE managed_trades SET {sql} WHERE symbol=?", (*updates.values(), symbol))

    def recent_audit(self, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT timestamp,event_type,details FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(row), "details": json.loads(row["details"])} for row in rows]

    def get_runtime_state(self, key: str, default=None):
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_runtime_state(self, key: str, value) -> None:
        with self._connect() as conn:
            conn.execute("""INSERT INTO runtime_state(key,value,updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, json.dumps(value), datetime.now(timezone.utc).isoformat()))
