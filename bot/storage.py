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
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
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
                    payload TEXT NOT NULL DEFAULT '{}',
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
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(row_snapshots)").fetchall()
        }
        if "payload" not in columns:
            conn.execute(
                "ALTER TABLE row_snapshots ADD COLUMN payload TEXT NOT NULL DEFAULT '{}'"
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

    def get_snapshot_hash(self, row_number: int) -> str | None:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT content_hash FROM row_snapshots WHERE row_number = ?",
                (row_number,),
            )
            row = cur.fetchone()
            return row["content_hash"] if row else None

    def get_snapshot_row(self, row_number: int) -> ImageRow | None:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT payload FROM row_snapshots WHERE row_number = ?",
                (row_number,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return ImageRow.from_payload(row["payload"])

    def all_snapshots(self) -> dict[int, tuple[str, ImageRow | None]]:
        """Load every snapshot at once: {row_number: (content_hash, ImageRow)}."""
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT row_number, content_hash, payload FROM row_snapshots"
            )
            return {
                row["row_number"]: (
                    row["content_hash"],
                    ImageRow.from_payload(row["payload"]),
                )
                for row in cur.fetchall()
            }

    def upsert_snapshots(self, items: list[tuple[ImageRow, str]]) -> None:
        if not items:
            return
        now = _now_iso()
        params = [
            (
                image_row.row_number,
                content_hash,
                image_row.tag,
                image_row.developer,
                image_row.status,
                image_row.to_payload(),
                now,
            )
            for image_row, content_hash in items
        ]
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO row_snapshots
                    (row_number, content_hash, tag, developer, status, payload, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(row_number) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    tag = excluded.tag,
                    developer = excluded.developer,
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                params,
            )

    def delete_snapshots(self, row_numbers: list[int]) -> None:
        if not row_numbers:
            return
        with self._connect() as conn:
            conn.executemany(
                "DELETE FROM row_snapshots WHERE row_number = ?",
                [(rn,) for rn in row_numbers],
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
