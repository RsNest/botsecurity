from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
import threading
from dataclasses import dataclass
from urllib.parse import urlencode

import aiohttp
import gspread
from google.oauth2.service_account import Credentials

from bot.config import (
    COL_ACTUAL_RELEASE,
    COL_CHECK_DATE,
    COL_CORRECTED_TAG,
    COL_DATE,
    COL_DEVELOPER,
    COL_FINAL_TAG,
    COL_RELEASE,
    COL_STATUS,
    COL_TAG,
    COL_UPLOADED_MF,
    DATA_START_ROW,
    STATUS_NOT_TRANSFERRED,
    settings,
)
from bot.models import ImageRow

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


@dataclass(frozen=True)
class ReconcileResult:
    canon_count: int = 0
    mirror_count: int = 0
    appended_to_canon: int = 0
    appended_to_mirror: int = 0
    mirror_enabled: bool = False
    error: str | None = None


def _col_letter(index: int) -> str:
    """0-based column index -> A1 letter."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _rows_from_updated_range(updated_range: str, expected_count: int) -> list[int]:
    """Extract start/end rows from a Sheets API updatedRange value."""
    numbers = [int(value) for value in re.findall(r"[A-Z]+(\d+)", updated_range)]
    if not numbers:
        return []
    start = numbers[0]
    end = numbers[-1] if len(numbers) > 1 else start + expected_count - 1
    if end - start + 1 != expected_count:
        return []
    return list(range(start, end + 1))


def _norm_tag(value: str | None) -> str:
    return (value or "").strip().lower()


def _row_tag_keys(row: ImageRow) -> set[str]:
    keys: set[str] = set()
    for value in (row.tag, row.corrected_tag, row.final_tag):
        key = _norm_tag(value)
        if key:
            keys.add(key)
    return keys


def _sheet_tag_keys(rows: list[ImageRow]) -> set[str]:
    keys: set[str] = set()
    for row in rows:
        keys |= _row_tag_keys(row)
    return keys


def _image_row_to_cells(row: ImageRow) -> list[str]:
    cells = [""] * 10
    cells[COL_DATE] = row.transfer_date
    cells[COL_DEVELOPER] = row.developer
    cells[COL_TAG] = row.tag
    cells[COL_CORRECTED_TAG] = row.corrected_tag
    cells[COL_RELEASE] = row.release
    cells[COL_STATUS] = row.status
    cells[COL_CHECK_DATE] = row.check_date
    cells[COL_FINAL_TAG] = row.final_tag
    cells[COL_UPLOADED_MF] = row.uploaded_mf
    cells[COL_ACTUAL_RELEASE] = row.actual_release_date
    return cells


def _entry_to_cells(entry: dict) -> list[str]:
    row = [""] * 10
    row[COL_DATE] = entry.get("transfer_date", "")
    row[COL_DEVELOPER] = entry.get("developer", "")
    row[COL_TAG] = entry.get("tag", "")
    row[COL_CORRECTED_TAG] = entry.get("corrected_tag", "")
    row[COL_RELEASE] = entry.get("release", "")
    row[COL_STATUS] = entry.get("status", "")
    row[COL_CHECK_DATE] = entry.get("check_date", "")
    row[COL_FINAL_TAG] = entry.get("final_tag", "")
    row[COL_UPLOADED_MF] = entry.get("uploaded_mf", "")
    row[COL_ACTUAL_RELEASE] = entry.get("actual_release_date", "")
    return row


MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2


class SheetsClient:
    def __init__(self) -> None:
        self._gc: gspread.Client | None = None
        # Serialises writes made by this process. Google Sheets remains the
        # source of truth, but this prevents two bot confirmations racing.
        self._write_lock = threading.Lock()

    def _ensure_client(self) -> gspread.Client:
        if self._gc is None:
            creds = Credentials.from_service_account_file(
                str(settings.google_credentials_path),
                scopes=SCOPES,
            )
            self._gc = gspread.authorize(creds)
        return self._gc

    def _find_worksheet(
        self,
        spreadsheet: gspread.Spreadsheet,
        *,
        gid: str | None = None,
    ):
        target_gid = str(gid if gid is not None else settings.sheet_gid)
        for ws in spreadsheet.worksheets():
            if str(ws.id) == target_gid:
                return ws
        return spreadsheet.sheet1

    def _open_worksheet(
        self,
        spreadsheet_id: str,
        *,
        gid: str | None = None,
    ):
        spreadsheet = self._ensure_client().open_by_key(spreadsheet_id)
        return self._find_worksheet(spreadsheet, gid=gid)

    def _open_canon(self):
        return self._open_worksheet(settings.spreadsheet_id, gid=settings.sheet_gid)

    def _open_mirror(self):
        if not settings.mirror_enabled:
            raise RuntimeError("Mirror spreadsheet is not configured")
        return self._open_worksheet(
            settings.spreadsheet_mirror_id,
            gid=settings.sheet_mirror_gid,
        )

    async def fetch_rows(self) -> list[ImageRow]:
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if settings.google_credentials_path.exists():
                    return await self._fetch_via_api()
                return await self._fetch_via_csv_export()
            except Exception as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    delay = RETRY_BACKOFF_SECONDS * attempt
                    logger.warning(
                        "Sheet fetch attempt %s/%s failed: %s (retry in %ss)",
                        attempt, MAX_RETRIES, exc, delay,
                    )
                    await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc

    async def fetch_mirror_rows(self) -> list[ImageRow]:
        if not settings.mirror_enabled:
            return []
        return await asyncio.to_thread(self._fetch_mirror_sync)

    def _fetch_mirror_sync(self) -> list[ImageRow]:
        worksheet = self._open_mirror()
        return self._parse_rows(worksheet.get_all_values())

    async def _fetch_via_api(self) -> list[ImageRow]:
        rows = await asyncio.to_thread(self._fetch_sync_api)
        return self._parse_rows(rows)

    def _fetch_sync_api(self) -> list[list[str]]:
        worksheet = self._open_canon()
        return worksheet.get_all_values()

    async def _fetch_via_csv_export(self) -> list[ImageRow]:
        params = urlencode({"format": "csv", "gid": settings.sheet_gid})
        url = (
            f"https://docs.google.com/spreadsheets/d/"
            f"{settings.spreadsheet_id}/export?{params}"
        )
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    text = await response.text()
                    raise RuntimeError(
                        "Не удалось скачать таблицу через CSV export "
                        f"(HTTP {response.status}). "
                        "Добавьте credentials.json или откройте доступ к таблице. "
                        f"Ответ: {text[:200]}"
                    )
                content = await response.text(encoding="utf-8")
        reader = csv.reader(io.StringIO(content))
        rows = list(reader)
        return self._parse_rows(rows)

    @property
    def can_write(self) -> bool:
        return settings.google_credentials_path.exists()

    def _append_rows_to_worksheet(
        self,
        worksheet,
        values: list[list[str]],
    ) -> list[int]:
        if not values:
            return []
        with self._write_lock:
            response = worksheet.append_rows(
                values,
                table_range=f"A{DATA_START_ROW}:{_col_letter(COL_ACTUAL_RELEASE)}",
                value_input_option="USER_ENTERED",
                insert_data_option="INSERT_ROWS",
                include_values_in_response=True,
            )
        updated_range = response.get("updates", {}).get("updatedRange", "")
        row_numbers = _rows_from_updated_range(updated_range, len(values))
        if not row_numbers:
            raise RuntimeError("Sheets API не вернул номера добавленных строк.")
        return row_numbers

    def _mirror_append_best_effort(self, values: list[list[str]], context: str) -> None:
        if not settings.mirror_enabled or not values:
            return
        try:
            worksheet = self._open_mirror()
            self._append_rows_to_worksheet(worksheet, values)
            logger.info("Mirror append OK (%s): %s row(s)", context, len(values))
        except Exception:
            logger.exception("Mirror append failed (%s)", context)

    def _find_mirror_rows_by_tags(self, tags: list[str]) -> dict[str, int]:
        """Map normalized tag -> first matching mirror row number."""
        wanted = {_norm_tag(t) for t in tags if _norm_tag(t)}
        if not wanted:
            return {}
        worksheet = self._open_mirror()
        values = worksheet.get_all_values()
        found: dict[str, int] = {}
        for idx, cells in enumerate(values):
            row_number = idx + 1
            if row_number < DATA_START_ROW:
                continue
            padded = (cells + [""] * 10)[:10]
            for col in (COL_TAG, COL_CORRECTED_TAG, COL_FINAL_TAG):
                key = _norm_tag(padded[col])
                if key in wanted and key not in found:
                    found[key] = row_number
        return found

    async def update_statuses(
        self,
        updates: list[tuple[int, str, str]],
    ) -> None:
        """Write (row_number, status, check_date) to the sheet.

        Requires a service account with Editor access to the spreadsheet.
        """
        if not updates:
            return
        if not self.can_write:
            raise RuntimeError(
                "Запись в таблицу недоступна: нет credentials.json. "
                "Добавьте сервисный аккаунт с правами редактора."
            )
        await asyncio.to_thread(self._update_statuses_sync, updates)

    def _update_statuses_sync(self, updates: list[tuple[int, str, str]]) -> None:
        worksheet = self._open_canon()
        status_col = _col_letter(COL_STATUS)
        date_col = _col_letter(COL_CHECK_DATE)
        batch = []
        for row_number, status, check_date in updates:
            batch.append({
                "range": f"{status_col}{row_number}",
                "values": [[status]],
            })
            if check_date:
                batch.append({
                    "range": f"{date_col}{row_number}",
                    "values": [[check_date]],
                })
        with self._write_lock:
            worksheet.batch_update(batch, value_input_option="USER_ENTERED")

        if not settings.mirror_enabled:
            return
        try:
            values = worksheet.get_all_values()
            tag_updates: list[tuple[str, str, str]] = []
            for row_number, status, check_date in updates:
                idx = row_number - 1
                if idx < 0 or idx >= len(values):
                    continue
                padded = (values[idx] + [""] * 10)[:10]
                tag = padded[COL_TAG].strip()
                if not tag:
                    continue
                tag_updates.append((tag, status, check_date))
            self._mirror_update_statuses_by_tag(tag_updates)
        except Exception:
            logger.exception("Mirror status update failed")

    def _mirror_update_statuses_by_tag(
        self,
        tag_updates: list[tuple[str, str, str]],
    ) -> None:
        if not tag_updates:
            return
        found = self._find_mirror_rows_by_tags([t for t, _, _ in tag_updates])
        status_col = _col_letter(COL_STATUS)
        date_col = _col_letter(COL_CHECK_DATE)
        batch = []
        for tag, status, check_date in tag_updates:
            row_number = found.get(_norm_tag(tag))
            if not row_number:
                logger.warning("Mirror: no row for tag %s", tag[:80])
                continue
            batch.append({
                "range": f"{status_col}{row_number}",
                "values": [[status]],
            })
            if check_date:
                batch.append({
                    "range": f"{date_col}{row_number}",
                    "values": [[check_date]],
                })
        if not batch:
            return
        mirror = self._open_mirror()
        with self._write_lock:
            mirror.batch_update(batch, value_input_option="USER_ENTERED")

    async def append_registry_rows(
        self,
        entries: list[dict],
    ) -> list[int]:
        """Append new registry rows. Each entry: transfer_date, developer, tag, release."""
        if not entries:
            return []
        if not self.can_write:
            raise RuntimeError(
                "Запись в таблицу недоступна: нет credentials.json."
            )
        return await asyncio.to_thread(self._append_registry_rows_sync, entries)

    def _append_registry_rows_sync(self, entries: list[dict]) -> list[int]:
        worksheet = self._open_canon()
        values = [_entry_to_cells(entry) for entry in entries]

        # append_rows delegates row selection to the Sheets API.  Unlike a
        # stale read + batch_update it never overwrites a row added manually
        # while a user is confirming the bot preview.
        row_numbers = self._append_rows_to_worksheet(worksheet, values)

        # Verify each tag landed in the intended row (catches race with manual edits).
        verify = worksheet.get_all_values()
        for row_number, entry in zip(row_numbers, entries, strict=True):
            idx = row_number - 1
            if idx >= len(verify):
                raise RuntimeError(
                    f"Строка {row_number} не появилась после записи. Попробуйте ещё раз."
                )
            actual_tag = (verify[idx] + [""] * 10)[COL_TAG].strip()
            if actual_tag != entry["tag"].strip():
                shown = actual_tag or "(пусто)"
                if len(shown) > 60:
                    shown = shown[:60] + "…"
                raise RuntimeError(
                    f"Строка {row_number} уже занята ({shown}). "
                    "Запись отменена — обновите таблицу и попробуйте снова."
                )

        self._mirror_append_best_effort(values, "append_registry_rows")
        return row_numbers

    async def submit_corrected_tag(
        self,
        row_number: int,
        corrected_tag: str,
    ) -> None:
        """Write corrected tag and reset status to not-transferred."""
        if not self.can_write:
            raise RuntimeError(
                "Запись в таблицу недоступна: нет credentials.json."
            )
        await asyncio.to_thread(
            self._submit_corrected_tag_sync, row_number, corrected_tag
        )

    def _submit_corrected_tag_sync(self, row_number: int, corrected_tag: str) -> None:
        worksheet = self._open_canon()
        corrected_col = _col_letter(COL_CORRECTED_TAG)
        status_col = _col_letter(COL_STATUS)
        date_col = _col_letter(COL_CHECK_DATE)
        with self._write_lock:
            worksheet.batch_update(
                [
                    {"range": f"{corrected_col}{row_number}", "values": [[corrected_tag]]},
                    {"range": f"{status_col}{row_number}", "values": [[STATUS_NOT_TRANSFERRED]]},
                    {"range": f"{date_col}{row_number}", "values": [[""]]},
                ],
                value_input_option="USER_ENTERED",
            )

        if not settings.mirror_enabled:
            return
        try:
            values = worksheet.get_all_values()
            idx = row_number - 1
            if idx < 0 or idx >= len(values):
                return
            padded = (values[idx] + [""] * 10)[:10]
            original_tag = padded[COL_TAG].strip()
            if not original_tag:
                return
            found = self._find_mirror_rows_by_tags([original_tag])
            mirror_row = found.get(_norm_tag(original_tag))
            if not mirror_row:
                logger.warning(
                    "Mirror: no row for corrected-tag source %s",
                    original_tag[:80],
                )
                return
            mirror = self._open_mirror()
            with self._write_lock:
                mirror.batch_update(
                    [
                        {
                            "range": f"{corrected_col}{mirror_row}",
                            "values": [[corrected_tag]],
                        },
                        {
                            "range": f"{status_col}{mirror_row}",
                            "values": [[STATUS_NOT_TRANSFERRED]],
                        },
                        {"range": f"{date_col}{mirror_row}", "values": [[""]]},
                    ],
                    value_input_option="USER_ENTERED",
                )
        except Exception:
            logger.exception("Mirror corrected-tag update failed")

    async def reconcile_mirror(self) -> ReconcileResult:
        if not settings.mirror_enabled:
            return ReconcileResult(mirror_enabled=False)
        if not self.can_write:
            return ReconcileResult(
                mirror_enabled=True,
                error="нет credentials.json для записи",
            )
        return await asyncio.to_thread(self._reconcile_mirror_sync)

    def _reconcile_mirror_sync(self) -> ReconcileResult:
        try:
            canon_ws = self._open_canon()
            mirror_ws = self._open_mirror()
            canon_rows = self._parse_rows(canon_ws.get_all_values())
            mirror_rows = self._parse_rows(mirror_ws.get_all_values())
            canon_keys = _sheet_tag_keys(canon_rows)
            mirror_keys = _sheet_tag_keys(mirror_rows)

            to_canon: list[list[str]] = []
            for row in mirror_rows:
                primary = _norm_tag(row.tag)
                if not primary:
                    continue
                if primary in canon_keys:
                    continue
                to_canon.append(_image_row_to_cells(row))
                canon_keys |= _row_tag_keys(row)

            to_mirror: list[list[str]] = []
            for row in canon_rows:
                primary = _norm_tag(row.tag)
                if not primary:
                    continue
                if primary in mirror_keys:
                    continue
                to_mirror.append(_image_row_to_cells(row))
                mirror_keys |= _row_tag_keys(row)

            appended_to_canon = 0
            appended_to_mirror = 0
            if to_canon:
                self._append_rows_to_worksheet(canon_ws, to_canon)
                appended_to_canon = len(to_canon)
                logger.info("Reconcile: appended %s row(s) to canon", appended_to_canon)
            if to_mirror:
                self._append_rows_to_worksheet(mirror_ws, to_mirror)
                appended_to_mirror = len(to_mirror)
                logger.info("Reconcile: appended %s row(s) to mirror", appended_to_mirror)

            return ReconcileResult(
                canon_count=len(canon_rows) + appended_to_canon,
                mirror_count=len(mirror_rows) + appended_to_mirror,
                appended_to_canon=appended_to_canon,
                appended_to_mirror=appended_to_mirror,
                mirror_enabled=True,
            )
        except Exception as exc:
            logger.exception("Reconcile mirror failed")
            return ReconcileResult(mirror_enabled=True, error=str(exc))

    def _parse_rows(self, values: list[list[str]]) -> list[ImageRow]:
        result: list[ImageRow] = []
        for idx, cells in enumerate(values):
            row_number = idx + 1
            if row_number < DATA_START_ROW:
                continue
            row = ImageRow.from_sheet_row(row_number, cells)
            if row:
                result.append(row)
        return result
