from __future__ import annotations

import json
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

                CREATE TABLE IF NOT EXISTS activity_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    full_name TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_activity_at
                    ON activity_log(at);
                CREATE INDEX IF NOT EXISTS idx_activity_user
                    ON activity_log(user_id);

                CREATE TABLE IF NOT EXISTS dev_profiles (
                    user_id INTEGER PRIMARY KEY,
                    surname TEXT NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    full_name TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tag_authors (
                    tag TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    added_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS row_authors (
                    row_number INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    added_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pending_fixes (
                    user_id INTEGER NOT NULL,
                    row_number INTEGER NOT NULL,
                    notified_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, row_number)
                );

                CREATE TABLE IF NOT EXISTS audit_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    issue_keys TEXT NOT NULL DEFAULT '[]',
                    bootstrapped INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS user_roles (
                    user_id INTEGER PRIMARY KEY,
                    role TEXT NOT NULL CHECK (role IN ('developer', 'ib_operator', 'viewer')),
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS notification_preferences (
                    user_id INTEGER PRIMARY KEY,
                    mode TEXT NOT NULL CHECK (mode IN ('all', 'mine', 'fail', 'digest', 'off')),
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS row_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    row_number INTEGER NOT NULL,
                    changed_at TEXT NOT NULL,
                    change_type TEXT NOT NULL,
                    old_payload TEXT NOT NULL DEFAULT '{}',
                    new_payload TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_row_history_row
                    ON row_history(row_number, id DESC);
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO audit_state (id, issue_keys, bootstrapped, updated_at) "
                "VALUES (1, '[]', 0, '')"
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

    # --- User activity -------------------------------------------------------

    def log_activity(
        self,
        user_id: int,
        username: str,
        full_name: str,
        kind: str,
        action: str,
        detail: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO activity_log
                    (at, user_id, username, full_name, kind, action, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (_now_iso(), user_id, username, full_name, kind, action, detail[:200]),
            )

    def activity_overview(self, days: int = 7) -> dict:
        """Aggregate stats for the admin: users, actions, per-user counts."""
        since = f"-{days} days"
        with self._connect() as conn:
            total_users = conn.execute(
                "SELECT COUNT(DISTINCT user_id) AS c FROM activity_log"
            ).fetchone()["c"]
            active = conn.execute(
                "SELECT COUNT(DISTINCT user_id) AS c FROM activity_log "
                "WHERE at >= datetime('now', ?)",
                (since,),
            ).fetchone()["c"]
            actions_period = conn.execute(
                "SELECT COUNT(*) AS c FROM activity_log "
                "WHERE at >= datetime('now', ?)",
                (since,),
            ).fetchone()["c"]
            top_users = conn.execute(
                """
                SELECT user_id, MAX(username) AS username,
                       MAX(full_name) AS full_name,
                       COUNT(*) AS cnt, MAX(at) AS last_at
                FROM activity_log
                WHERE at >= datetime('now', ?)
                GROUP BY user_id
                ORDER BY cnt DESC
                LIMIT 10
                """,
                (since,),
            ).fetchall()
            top_actions = conn.execute(
                """
                SELECT action, COUNT(*) AS cnt
                FROM activity_log
                WHERE at >= datetime('now', ?)
                GROUP BY action
                ORDER BY cnt DESC
                LIMIT 12
                """,
                (since,),
            ).fetchall()
        return {
            "days": days,
            "total_users": int(total_users),
            "active_users": int(active),
            "actions_period": int(actions_period),
            "top_users": [dict(r) for r in top_users],
            "top_actions": [dict(r) for r in top_actions],
        }

    def recent_activity(self, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT at, user_id, username, full_name, kind, action, detail
                FROM activity_log
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]

    def user_activity(self, user_id: int, limit: int = 25) -> list[dict]:
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT at, action, detail
                FROM activity_log
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (user_id, limit),
            )
            return [dict(r) for r in cur.fetchall()]

    def prune_activity(self, keep_days: int = 90) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM activity_log WHERE at < datetime('now', ?)",
                (f"-{keep_days} days",),
            )

    # --- Roles, preferences and change history -----------------------------

    def set_role(self, user_id: int, role: str) -> None:
        if role not in {"developer", "ib_operator", "viewer"}:
            raise ValueError("Unknown role")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_roles (user_id, role, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET role = excluded.role, updated_at = excluded.updated_at
                """,
                (user_id, role, _now_iso()),
            )

    def role_for(self, user_id: int) -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT role FROM user_roles WHERE user_id = ?", (user_id,)).fetchone()
            return str(row["role"]) if row else "developer"

    def is_ib_operator(self, user_id: int) -> bool:
        return self.role_for(user_id) == "ib_operator"

    def set_notification_mode(self, user_id: int, mode: str) -> None:
        if mode not in {"all", "mine", "fail", "digest", "off"}:
            raise ValueError("Unknown notification mode")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO notification_preferences (user_id, mode, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET mode = excluded.mode, updated_at = excluded.updated_at
                """,
                (user_id, mode, _now_iso()),
            )

    def notification_mode(self, user_id: int) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT mode FROM notification_preferences WHERE user_id = ?", (user_id,)
            ).fetchone()
            return str(row["mode"]) if row else "all"

    def log_row_history(
        self,
        row_number: int,
        change_type: str,
        old_payload: str = "{}",
        new_payload: str = "{}",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO row_history (row_number, changed_at, change_type, old_payload, new_payload)
                VALUES (?, ?, ?, ?, ?)
                """,
                (row_number, _now_iso(), change_type, old_payload, new_payload),
            )

    def row_history(self, row_number: int, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT changed_at, change_type, old_payload, new_payload
                FROM row_history WHERE row_number = ? ORDER BY id DESC LIMIT ?
                """,
                (row_number, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    # --- Developer profiles & tag authorship ---------------------------------

    def set_profile(
        self,
        user_id: int,
        surname: str,
        username: str = "",
        full_name: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO dev_profiles (user_id, surname, username, full_name, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    surname = excluded.surname,
                    username = excluded.username,
                    full_name = excluded.full_name,
                    updated_at = excluded.updated_at
                """,
                (user_id, surname.strip(), username, full_name, _now_iso()),
            )

    def get_profile(self, user_id: int) -> dict | None:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT user_id, surname, username, full_name "
                "FROM dev_profiles WHERE user_id = ?",
                (user_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def all_profiles(self) -> list[dict]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT user_id, surname, username, full_name FROM dev_profiles"
            )
            return [dict(r) for r in cur.fetchall()]

    def add_tag_author(self, tag: str, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tag_authors (tag, user_id, added_at)
                VALUES (?, ?, ?)
                ON CONFLICT(tag) DO UPDATE SET
                    user_id = excluded.user_id,
                    added_at = excluded.added_at
                """,
                (tag.strip(), user_id, _now_iso()),
            )

    def tag_author(self, tag: str) -> int | None:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT user_id FROM tag_authors WHERE tag = ?",
                (tag.strip(),),
            )
            row = cur.fetchone()
            return int(row["user_id"]) if row else None

    def tags_by_author(self, user_id: int) -> list[str]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT tag FROM tag_authors WHERE user_id = ? ORDER BY added_at DESC",
                (user_id,),
            )
            return [r["tag"] for r in cur.fetchall()]

    def set_row_author(self, row_number: int, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO row_authors (row_number, user_id, added_at)
                VALUES (?, ?, ?)
                ON CONFLICT(row_number) DO UPDATE SET
                    user_id = excluded.user_id,
                    added_at = excluded.added_at
                """,
                (row_number, user_id, _now_iso()),
            )

    def row_author(self, row_number: int) -> int | None:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT user_id FROM row_authors WHERE row_number = ?",
                (row_number,),
            )
            row = cur.fetchone()
            return int(row["user_id"]) if row else None

    def user_ids_by_surname(self, surname: str) -> list[int]:
        target = surname.strip().lower()
        if not target:
            return []
        with self._connect() as conn:
            cur = conn.execute("SELECT user_id, surname FROM dev_profiles")
            return [
                int(r["user_id"])
                for r in cur.fetchall()
                if (r["surname"] or "").strip().lower() == target
            ]

    def prune_pending_fixes(self, rows: list) -> int:
        """Drop pending-fix flags for rows that are no longer failed."""
        failed = {r.row_number for r in rows if r.is_failed()}
        with self._connect() as conn:
            if failed:
                placeholders = ",".join("?" * len(failed))
                cur = conn.execute(
                    f"DELETE FROM pending_fixes WHERE row_number NOT IN ({placeholders})",
                    list(failed),
                )
            else:
                cur = conn.execute("DELETE FROM pending_fixes")
            return cur.rowcount

    def set_pending_fix(self, user_id: int, row_number: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pending_fixes (user_id, row_number, notified_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, row_number) DO UPDATE SET
                    notified_at = excluded.notified_at
                """,
                (user_id, row_number, _now_iso()),
            )

    def get_pending_fixes(self, user_id: int) -> list[int]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT row_number FROM pending_fixes WHERE user_id = ? "
                "ORDER BY notified_at DESC",
                (user_id,),
            )
            return [int(r["row_number"]) for r in cur.fetchall()]

    def clear_pending_fix(self, user_id: int, row_number: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM pending_fixes WHERE user_id = ? AND row_number = ?",
                (user_id, row_number),
            )

    def clear_all_pending_fixes(self, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM pending_fixes WHERE user_id = ?",
                (user_id,),
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

    def get_audit_issue_keys(self) -> set[str]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT issue_keys FROM audit_state WHERE id = 1"
            )
            row = cur.fetchone()
            if not row:
                return set()
            try:
                data = json.loads(row["issue_keys"] or "[]")
            except json.JSONDecodeError:
                return set()
            return set(data) if isinstance(data, list) else set()

    def audit_bootstrapped(self) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT bootstrapped FROM audit_state WHERE id = 1"
            )
            row = cur.fetchone()
            return bool(row and row["bootstrapped"])

    def set_audit_issue_keys(
        self,
        keys: set[str],
        *,
        bootstrapped: bool | None = None,
    ) -> None:
        payload = json.dumps(sorted(keys), ensure_ascii=False)
        with self._connect() as conn:
            if bootstrapped is None:
                conn.execute(
                    """
                    UPDATE audit_state
                    SET issue_keys = ?, updated_at = ?
                    WHERE id = 1
                    """,
                    (payload, _now_iso()),
                )
            else:
                conn.execute(
                    """
                    UPDATE audit_state
                    SET issue_keys = ?, bootstrapped = ?, updated_at = ?
                    WHERE id = 1
                    """,
                    (payload, int(bootstrapped), _now_iso()),
                )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
