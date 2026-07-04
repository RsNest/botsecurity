from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from bot.config import FIELD_NAMES
from bot.models import ImageRow, RowChange, ScanResult
from bot.sheets import SheetsClient
from bot.storage import Storage

logger = logging.getLogger(__name__)


class RegistryMonitor:
    def __init__(
        self,
        sheets: SheetsClient | None = None,
        storage: Storage | None = None,
    ) -> None:
        self.sheets = sheets or SheetsClient()
        self.storage = storage or Storage()
        self._last_rows: list[ImageRow] = []

    @property
    def last_rows(self) -> list[ImageRow]:
        return list(self._last_rows)

    async def scan(self) -> ScanResult:
        fetched_at = datetime.now(timezone.utc)
        try:
            rows = await self.sheets.fetch_rows()
            bootstrap = self.storage.snapshot_count() == 0
            changes = self._detect_changes(rows)
            for change in changes:
                self.storage.upsert_snapshot(
                    change.row,
                    change.row.content_hash(),
                )
            if bootstrap:
                changes = []
                logger.info(
                    "Bootstrap complete: seeded %s rows without notifications",
                    len(rows),
                )
            self.storage.log_scan(len(rows), len(changes))
            self._last_rows = rows
            return ScanResult(rows=rows, changes=changes, fetched_at=fetched_at)
        except Exception as exc:
            logger.exception("Scan failed")
            self.storage.log_scan(0, 0, str(exc))
            raise

    def _detect_changes(self, rows: list[ImageRow]) -> list[RowChange]:
        changes: list[RowChange] = []
        for row in rows:
            old_hash = self.storage.get_snapshot(row.row_number)
            new_hash = row.content_hash()
            if old_hash is None:
                changes.append(RowChange(row=row, change_type="new", changed_fields={}))
                continue
            if old_hash != new_hash:
                changes.append(
                    RowChange(
                        row=row,
                        change_type="updated",
                        changed_fields=self._diff_fields(old_hash, row),
                    )
                )
        return changes

    def _diff_fields(self, _old_hash: str, row: ImageRow) -> dict[str, tuple[str, str]]:
        # Without storing full previous payload we report current notable fields.
        return {
            key: ("", current)
            for key, current in {
                "status": row.status or "—",
                "tag": row.tag,
                "release": row.release,
                "corrected_tag": row.corrected_tag,
            }.items()
            if current
        }

    def pending_rows(self, rows: list[ImageRow] | None = None) -> list[ImageRow]:
        source = rows if rows is not None else self._last_rows
        return [row for row in source if row.is_pending_ops()]

    def rows_for_today(self, rows: list[ImageRow] | None = None) -> list[ImageRow]:
        source = rows if rows is not None else self._last_rows
        today = date.today()
        result: list[ImageRow] = []
        for row in source:
            transfer = row.parse_transfer_date()
            if transfer == today:
                result.append(row)
        return result

    def rows_by_developer(
        self,
        developer_query: str,
        rows: list[ImageRow] | None = None,
    ) -> list[ImageRow]:
        source = rows if rows is not None else self._last_rows
        query = developer_query.strip().lower()
        return [
            row
            for row in source
            if query in row.developer.lower()
        ]

    def stale_rows(
        self,
        days: int,
        rows: list[ImageRow] | None = None,
    ) -> list[ImageRow]:
        source = rows if rows is not None else self._last_rows
        today = date.today()
        result: list[ImageRow] = []
        for row in source:
            if not row.is_pending_ops():
                continue
            transfer = row.parse_transfer_date()
            if transfer and (today - transfer).days >= days:
                result.append(row)
        return result

    def status_summary(self, rows: list[ImageRow] | None = None) -> dict[str, int]:
        source = rows if rows is not None else self._last_rows
        summary = {
            "pending": 0,
            "on_review": 0,
            "passed": 0,
            "failed": 0,
            "not_transferred": 0,
            "other": 0,
        }
        for row in source:
            status = row.status_normalized()
            if not status:
                summary["pending"] += 1
            elif status == "на проверке":
                summary["on_review"] += 1
            elif status == "прошло проверку":
                summary["passed"] += 1
            elif status == "не прошло проверку":
                summary["failed"] += 1
            elif status == "не передано":
                summary["not_transferred"] += 1
            else:
                summary["other"] += 1
        return summary
