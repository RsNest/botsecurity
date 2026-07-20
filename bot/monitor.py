from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from bot.audit import AuditIssue, audit_rows
from bot.config import FIELD_NAMES, settings
from bot.models import ImageRow, RowChange, ScanResult
from bot.sheets import ReconcileResult, SheetsClient
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
        self._last_reconcile: ReconcileResult | None = None

    @property
    def last_rows(self) -> list[ImageRow]:
        return list(self._last_rows)

    @property
    def last_fetched_at(self) -> datetime | None:
        return self._last_fetched_at

    @property
    def last_reconcile(self) -> ReconcileResult | None:
        return self._last_reconcile

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
            self._last_reconcile = await self.sheets.reconcile_mirror()
            if self._last_reconcile.error:
                logger.warning(
                    "Mirror reconcile error: %s", self._last_reconcile.error
                )
            elif self._last_reconcile.mirror_enabled and (
                self._last_reconcile.appended_to_canon
                or self._last_reconcile.appended_to_mirror
            ):
                logger.info(
                    "Mirror reconcile: +%s canon, +%s mirror",
                    self._last_reconcile.appended_to_canon,
                    self._last_reconcile.appended_to_mirror,
                )

            rows = await self.sheets.fetch_rows()
            snapshots = self.storage.all_snapshots()
            bootstrap = len(snapshots) == 0

            changes = self._detect_changes(rows, snapshots)

            # Keep an append-only audit trail separate from the current
            # snapshot, so a card can explain how it reached its state.
            if not bootstrap:
                for change in changes:
                    old_row = snapshots.get(change.row.row_number, ("", None))[1]
                    self.storage.log_row_history(
                        change.row.row_number,
                        change.change_type,
                        old_row.to_payload() if old_row else "{}",
                        change.row.to_payload() if change.change_type != "removed" else "{}",
                    )

            to_upsert = [
                (change.row, change.row.content_hash())
                for change in changes
                if change.change_type != "removed"
            ]
            self.storage.upsert_snapshots(to_upsert)

            removed = [c.row.row_number for c in changes if c.change_type == "removed"]
            self.storage.delete_snapshots(removed)

            pruned = self.storage.prune_pending_fixes(rows)
            if pruned:
                logger.info("Pruned %s stale pending-fix flags", pruned)

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
        *,
        exact: bool = False,
    ) -> list[ImageRow]:
        source = rows if rows is not None else self._last_rows
        query = developer_query.strip().lower()
        if exact:
            return [
                row for row in source if row.developer.strip().lower() == query
            ]
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
        """Multi-word AND search across all meaningful fields.

        Every word of the query must occur somewhere in the row, so
        "leadgen api" narrows instead of failing.
        """
        source = rows if rows is not None else self._last_rows
        words = [w for w in query.strip().lower().split() if w]
        if not words:
            return []

        def haystack(row: ImageRow) -> str:
            return " ".join(
                (
                    row.tag,
                    row.corrected_tag,
                    row.final_tag,
                    row.release,
                    row.developer,
                    row.status,
                    row.transfer_date,
                )
            ).lower()

        result = []
        for row in source:
            hs = haystack(row)
            if all(w in hs for w in words):
                result.append(row)
        return result

    def find_duplicate_tag(self, tag: str) -> ImageRow | None:
        target = tag.strip().lower()
        if not target:
            return None
        for row in self._last_rows:
            for value in (row.tag, row.corrected_tag, row.final_tag):
                if value and value.strip().lower() == target:
                    return row
        return None

    def get_row(self, row_number: int) -> ImageRow | None:
        for row in self._last_rows:
            if row.row_number == row_number:
                return row
        return None

    def audit_issues(self, rows: list[ImageRow] | None = None) -> list[AuditIssue]:
        source = rows if rows is not None else self._last_rows
        return audit_rows(source)

    def last_rows_added(self, count: int = 10) -> list[ImageRow]:
        """Newest rows (bottom of the sheet first)."""
        return list(reversed(self._last_rows[-count:]))

    @staticmethod
    def _is_valid_developer(name: str) -> bool:
        # Skip data-entry errors where a tag landed in the developer column.
        return bool(name) and len(name) <= 25 and "/" not in name and ":" not in name

    def developers_summary(self) -> list[tuple[str, int, int]]:
        """[(developer, total, pending_count)] sorted by total desc."""
        stats: dict[str, list[int]] = {}
        for row in self._last_rows:
            name = row.developer.strip()
            if not self._is_valid_developer(name):
                continue
            item = stats.setdefault(name, [0, 0])
            item[0] += 1
            if row.is_pending_ops():
                item[1] += 1
        return sorted(
            ((name, total, pending) for name, (total, pending) in stats.items()),
            key=lambda x: (-x[1], x[0]),
        )

    def releases_summary(self, limit: int = 30) -> list[tuple[str, int]]:
        """[(release, count)] most recent releases first (by last row number)."""
        counts: dict[str, int] = {}
        last_seen: dict[str, int] = {}
        for row in self._last_rows:
            release = row.release.strip()
            if not release or len(release) > 40:
                continue
            counts[release] = counts.get(release, 0) + 1
            last_seen[release] = row.row_number
        ordered = sorted(counts, key=lambda r: -last_seen[r])
        return [(r, counts[r]) for r in ordered[:limit]]

    def rows_by_release(self, release: str) -> list[ImageRow]:
        target = release.strip().lower()
        return [
            row for row in self._last_rows if row.release.strip().lower() == target
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

    def quality_metrics(self, rows: list[ImageRow] | None = None) -> dict[str, float | int]:
        """Operational indicators for the admin dashboard."""
        source = rows if rows is not None else self._last_rows
        terminal = [row for row in source if row.is_terminal()]
        first_pass = [row for row in source if row.is_passed() and not row.corrected_tag]
        durations = []
        for row in terminal:
            transfer, checked = row.parse_transfer_date(), row.parse_check_date()
            if transfer and checked and checked >= transfer:
                durations.append((checked - transfer).days)
        return {
            "total": len(source),
            "terminal": len(terminal),
            "first_pass_rate": round(100 * len(first_pass) / len(terminal), 1) if terminal else 0,
            "avg_check_days": round(sum(durations) / len(durations), 1) if durations else 0,
        }
