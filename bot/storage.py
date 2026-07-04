from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from bot.config import DB_PATH
from bot.models import ImageRow


class Storage:
    def __init__(self, db_path=DB_PATH) -> None:
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS subscribers (
                    chat_id INTEGER PRIMARY KEY,
                    subscribed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS row_snapshots (
                    row_number INTEGER PRIMARY KEY,
                    content_hash TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    developer TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scan_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scanned_at TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    changes_count INTEGER NOT NULL,
                    error TEXT
                );
                """
            )

    def add_subscriber(self, chat_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT 1 FROM subscribers WHERE chat_id = ?",
                (chat_id,),
            )
            if cur.fetchone():
                return False
            conn.execute(
                "INSERT INTO subscribers (chat_id, subscribed_at) VALUES (?, ?)",
                (chat_id, _now_iso()),
            )
            return True

    def remove_subscriber(self, chat_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM subscribers WHERE chat_id = ?",
                (chat_id,),
            )
            return cur.rowcount > 0

    def is_subscriber(self, chat_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT 1 FROM subscribers WHERE chat_id = ?",
                (chat_id,),
            )
            return cur.fetchone() is not None

    def list_subscribers(self) -> list[int]:
        with self._connect() as conn:
            cur = conn.execute("SELECT chat_id FROM subscribers ORDER BY chat_id")
            return [row["chat_id"] for row in cur.fetchall()]

    def subscriber_count(self) -> int:
        with self._connect() as conn:
            cur = conn.execute("SELECT COUNT(*) AS c FROM subscribers")
            return int(cur.fetchone()["c"])

    def snapshot_count(self) -> int:
        with self._connect() as conn:
            cur = conn.execute("SELECT COUNT(*) AS c FROM row_snapshots")
            return int(cur.fetchone()["c"])

    def get_snapshot(self, row_number: int) -> str | None:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT content_hash FROM row_snapshots WHERE row_number = ?",
                (row_number,),
            )
            row = cur.fetchone()
            return row["content_hash"] if row else None

    def upsert_snapshot(self, image_row: ImageRow, content_hash: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO row_snapshots
                    (row_number, content_hash, tag, developer, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(row_number) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    tag = excluded.tag,
                    developer = excluded.developer,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    image_row.row_number,
                    content_hash,
                    image_row.tag,
                    image_row.developer,
                    image_row.status,
                    _now_iso(),
                ),
            )

    def log_scan(
        self,
        row_count: int,
        changes_count: int,
        error: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scan_log (scanned_at, row_count, changes_count, error)
                VALUES (?, ?, ?, ?)
                """,
                (_now_iso(), row_count, changes_count, error),
            )

    def last_scan(self) -> dict | None:
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT scanned_at, row_count, changes_count, error
                FROM scan_log
                ORDER BY id DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            return dict(row) if row else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
