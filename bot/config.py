from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "botscan.db"


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    admin_ids: frozenset[int]
    spreadsheet_id: str
    sheet_gid: str
    google_credentials_path: Path
    poll_interval_minutes: int
    reminder_hours: tuple[int, ...]
    timezone: str
    cache_ttl_seconds: int
    force_refresh_cooldown: int

    @classmethod
    def load(cls) -> Settings:
        token = os.getenv("TELEGRAM_TOKEN", "").strip()
        if not token:
            raise ValueError("TELEGRAM_TOKEN is not set")

        admin_raw = os.getenv("ADMIN_IDS", "")
        admin_ids = frozenset(
            int(x.strip()) for x in admin_raw.split(",") if x.strip()
        )

        creds = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")
        creds_path = Path(creds)
        if not creds_path.is_absolute():
            creds_path = BASE_DIR / creds_path

        reminder_raw = os.getenv("REMINDER_HOURS", "10,13,16,18")
        reminder_hours = tuple(
            int(h.strip()) for h in reminder_raw.split(",") if h.strip()
        )

        return cls(
            telegram_token=token,
            admin_ids=admin_ids,
            spreadsheet_id=os.getenv(
                "SPREADSHEET_ID", "1l-FSeC1mfIXqX-bvoKdfRV5txF0TDQ5aD-8GD8Ey-sw"
            ),
            sheet_gid=os.getenv("SHEET_GID", "684739217"),
            google_credentials_path=creds_path,
            poll_interval_minutes=int(os.getenv("POLL_INTERVAL_MINUTES", "60")),
            reminder_hours=reminder_hours,
            timezone=os.getenv("TIMEZONE", "Europe/Moscow"),
            cache_ttl_seconds=int(os.getenv("CACHE_TTL_SECONDS", "300")),
            force_refresh_cooldown=int(os.getenv("FORCE_REFRESH_COOLDOWN", "10")),
        )

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids


settings = Settings.load()

# Column indices (0-based) in data rows after header
COL_DATE = 0
COL_DEVELOPER = 1
COL_TAG = 2
COL_CORRECTED_TAG = 3
COL_RELEASE = 4
COL_STATUS = 5
COL_CHECK_DATE = 6
COL_FINAL_TAG = 7
COL_UPLOADED_MF = 8
COL_ACTUAL_RELEASE = 9

HEADER_ROW = 3  # 1-based row number in sheet
DATA_START_ROW = 4

STATUS_ON_REVIEW = "на проверке"
STATUS_PASSED = "прошло проверку"
STATUS_FAILED = "не прошло проверку"
STATUS_NOT_TRANSFERRED = "не передано"

FIELD_NAMES = {
    "transfer_date": "Дата передачи",
    "developer": "Разработчик",
    "tag": "Тег",
    "corrected_tag": "Исправленный тег",
    "release": "Релиз",
    "status": "Статус проверки ИБ",
    "check_date": "Дата проверки",
    "final_tag": "Итоговый тег",
    "uploaded_mf": "Залито в МФ",
    "actual_release_date": "Дата релиза фактическая",
}
