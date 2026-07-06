from __future__ import annotations

import asyncio
import csv
import io
import logging
from urllib.parse import urlencode

import aiohttp
import gspread
from google.oauth2.service_account import Credentials

from bot.config import (
    COL_CHECK_DATE,
    COL_CORRECTED_TAG,
    COL_DATE,
    COL_DEVELOPER,
    COL_RELEASE,
    COL_STATUS,
    COL_TAG,
    DATA_START_ROW,
    STATUS_ON_REVIEW,
    settings,
)
from bot.models import ImageRow

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


def _col_letter(index: int) -> str:
    """0-based column index -> A1 letter."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2


class SheetsClient:
    def __init__(self) -> None:
        self._gc: gspread.Client | None = None

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

    async def _fetch_via_api(self) -> list[ImageRow]:
        rows = await asyncio.to_thread(self._fetch_sync_api)
        return self._parse_rows(rows)

    def _fetch_sync_api(self) -> list[list[str]]:
        if self._gc is None:
            creds = Credentials.from_service_account_file(
                str(settings.google_credentials_path),
                scopes=SCOPES,
            )
            self._gc = gspread.authorize(creds)

        spreadsheet = self._gc.open_by_key(settings.spreadsheet_id)
        worksheet = self._find_worksheet(spreadsheet)
        values = worksheet.get_all_values()
        return values

    def _find_worksheet(self, spreadsheet: gspread.Spreadsheet):
        target_gid = str(settings.sheet_gid)
        for ws in spreadsheet.worksheets():
            if str(ws.id) == target_gid:
                return ws
        return spreadsheet.sheet1

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
        if self._gc is None:
            creds = Credentials.from_service_account_file(
                str(settings.google_credentials_path),
                scopes=SCOPES,
            )
            self._gc = gspread.authorize(creds)
        spreadsheet = self._gc.open_by_key(settings.spreadsheet_id)
        worksheet = self._find_worksheet(spreadsheet)

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
        worksheet.batch_update(batch, value_input_option="USER_ENTERED")

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
        if self._gc is None:
            creds = Credentials.from_service_account_file(
                str(settings.google_credentials_path),
                scopes=SCOPES,
            )
            self._gc = gspread.authorize(creds)
        spreadsheet = self._gc.open_by_key(settings.spreadsheet_id)
        worksheet = self._find_worksheet(spreadsheet)
        existing = worksheet.get_all_values()
        start_row = len(existing) + 1
        rows = []
        for entry in entries:
            row = [""] * 10
            row[COL_DATE] = entry["transfer_date"]
            row[COL_DEVELOPER] = entry["developer"]
            row[COL_TAG] = entry["tag"]
            row[COL_RELEASE] = entry["release"]
            rows.append(row)
        worksheet.append_rows(rows, value_input_option="USER_ENTERED")
        return list(range(start_row, start_row + len(entries)))

    async def submit_corrected_tag(
        self,
        row_number: int,
        corrected_tag: str,
    ) -> None:
        """Write corrected tag and reset status back to on-review."""
        if not self.can_write:
            raise RuntimeError(
                "Запись в таблицу недоступна: нет credentials.json."
            )
        await asyncio.to_thread(
            self._submit_corrected_tag_sync, row_number, corrected_tag
        )

    def _submit_corrected_tag_sync(self, row_number: int, corrected_tag: str) -> None:
        if self._gc is None:
            creds = Credentials.from_service_account_file(
                str(settings.google_credentials_path),
                scopes=SCOPES,
            )
            self._gc = gspread.authorize(creds)
        spreadsheet = self._gc.open_by_key(settings.spreadsheet_id)
        worksheet = self._find_worksheet(spreadsheet)
        corrected_col = _col_letter(COL_CORRECTED_TAG)
        status_col = _col_letter(COL_STATUS)
        date_col = _col_letter(COL_CHECK_DATE)
        worksheet.batch_update(
            [
                {"range": f"{corrected_col}{row_number}", "values": [[corrected_tag]]},
                {"range": f"{status_col}{row_number}", "values": [[STATUS_ON_REVIEW]]},
                {"range": f"{date_col}{row_number}", "values": [[""]]},
            ],
            value_input_option="USER_ENTERED",
        )

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
