from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from bot.config import FIELD_NAMES, settings
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
        self._last_fetched_at: datetime | None = None

    @property
    def last_rows(self) -> list[ImageRow]:
        return list(self._last_rows)

    @property
    def last_fetched_at(self) -> datetime | None:
        return self._last_fetched_at

    def cache_age_label(self) -> str:
        if not self._last_fetched_at:
            return ""
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(settings.timezone)
        local = self._last_fetched_at.astimezone(tz)
        return f"🕐 Данные на {local.strftime('%d.%m.%Y %H:%M')}"

    async def ensure_fresh(self, force: bool = False) -> ScanResult:
        now = datetime.now(timezone.utc)
        if (
            not force
            and self._last_rows
            and self._last_fetched_at
            and (now - self._last_fetched_at).total_seconds() < settings.cache_ttl_seconds
        ):
            return ScanResult(
                rows=self._last_rows,
                changes=[],
                fetched_at=self._last_fetched_at,
            )
        result = await self.scan()
        self._last_fetched_at = result.fetched_at
        return result

    async def scan(self) -> ScanResult:
        fetched_at = datetime.now(timezone.utc)
        try:
            rows = await self.sheets.fetch_rows()
            snapshots = self.storage.all_snapshots()
            bootstrap = len(snapshots) == 0

            changes = self._detect_changes(rows, snapshots)

            to_upsert = [
                (change.row, change.row.content_hash())
                for change in changes
                if change.change_type != "removed"
            ]
            self.storage.upsert_snapshots(to_upsert)

            removed = [c.row.row_number for c in changes if c.change_type == "removed"]
            self.storage.delete_snapshots(removed)

            if bootstrap:
                changes = []
                logger.info(
                    "Bootstrap complete: seeded %s rows without notifications",
                    len(rows),
                )
            self.storage.log_scan(len(rows), len(changes))
            self._last_rows = rows
            self._last_fetched_at = fetched_at
            return ScanResult(rows=rows, changes=changes, fetched_at=fetched_at)
        except Exception as exc:
            logger.exception("Scan failed")
            self.storage.log_scan(0, 0, str(exc))
            raise

    def _detect_changes(
        self,
        rows: list[ImageRow],
        snapshots: dict[int, tuple[str, ImageRow | None]],
    ) -> list[RowChange]:
        changes: list[RowChange] = []
        seen: set[int] = set()

        for row in rows:
            seen.add(row.row_number)
            snapshot = snapshots.get(row.row_number)
            new_hash = row.content_hash()
            if snapshot is None:
                changes.append(RowChange(row=row, change_type="new", changed_fields={}))
                continue
            old_hash, old_row = snapshot
            if old_hash != new_hash:
                changes.append(
                    RowChange(
                        row=row,
                        change_type="updated",
                        changed_fields=self._diff_fields(old_row, row),
                    )
                )

        # Rows that existed before but are gone now → removed
        for row_number, (_, old_row) in snapshots.items():
            if row_number not in seen and old_row is not None:
                changes.append(
                    RowChange(row=old_row, change_type="removed", changed_fields={})
                )

        return changes

    def _diff_fields(
        self,
        old_row: ImageRow | None,
        new_row: ImageRow,
    ) -> dict[str, tuple[str, str]]:
        if old_row is None:
            return {}
        changes: dict[str, tuple[str, str]] = {}
        for field in FIELD_NAMES:
            old_val = getattr(old_row, field) or ""
            new_val = getattr(new_row, field) or ""
            if old_val != new_val:
                changes[field] = (old_val or "—", new_val or "—")
        return changes

    def pending_rows(self, rows: list[ImageRow] | None = None) -> list[ImageRow]:
        source = rows if rows is not None else self._last_rows
        return [row for row in source if row.is_pending_ops()]

    def rows_on_review(self, rows: list[ImageRow] | None = None) -> list[ImageRow]:
        source = rows if rows is not None else self._last_rows
        return [row for row in source if row.is_on_review()]

    def rows_failed(self, rows: list[ImageRow] | None = None) -> list[ImageRow]:
        source = rows if rows is not None else self._last_rows
        return [row for row in source if row.is_failed()]

    def rows_passed(self, rows: list[ImageRow] | None = None) -> list[ImageRow]:
        source = rows if rows is not None else self._last_rows
        return [row for row in source if row.is_passed()]

    def rows_by_date_range(
        self,
        start: date,
        end: date,
        date_field: str = "tr",
        status_filter: str = "all",
        rows: list[ImageRow] | None = None,
    ) -> list[ImageRow]:
        source = rows if rows is not None else self._last_rows
        result: list[ImageRow] = []
        for row in source:
            row_date = row.date_for_field(date_field)
            if not row_date or row_date < start or row_date > end:
                continue
            if status_filter == "ok" and not row.is_passed():
                continue
            if status_filter == "fail" and not row.is_failed():
                continue
            result.append(row)
        return result

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

    def find_rows(
        self,
        query: str,
        rows: list[ImageRow] | None = None,
    ) -> list[ImageRow]:
        source = rows if rows is not None else self._last_rows
        q = query.strip().lower()
        if not q:
            return []
        return [
            row
            for row in source
            if q in row.tag.lower()
            or q in row.corrected_tag.lower()
            or q in row.final_tag.lower()
            or q in row.release.lower()
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
