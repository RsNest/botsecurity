from __future__ import annotations

import re
from datetime import date, datetime, timedelta

DATE_PATTERN = re.compile(
    r"^(\d{1,2})\.(\d{1,2})\.(\d{2,4})(?:\s*[-–—]\s*(\d{1,2})\.(\d{1,2})\.(\d{2,4}))?$"
)


def parse_flexible_date(raw: str) -> date | None:
    value = (raw or "").strip()
    if not value:
        return None
    for fmt in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def parse_date_range(text: str) -> tuple[date, date] | None:
    match = DATE_PATTERN.match(text.strip())
    if not match:
        return None
    d1, m1, y1, d2, m2, y2 = match.groups()
    start = _parts_to_date(int(d1), int(m1), y1)
    if not start:
        return None
    if d2 and m2 and y2:
        end = _parts_to_date(int(d2), int(m2), y2)
        if not end:
            return None
        if end < start:
            start, end = end, start
        return start, end
    return start, start


def _parts_to_date(day: int, month: int, year_raw: str) -> date | None:
    year = int(year_raw)
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def period_to_range(period: str, today: date | None = None) -> tuple[date, date]:
    today = today or date.today()
    if period == "td":
        return today, today
    if period == "yd":
        day = today - timedelta(days=1)
        return day, day
    if period == "7d":
        return today - timedelta(days=6), today
    if period == "30d":
        return today - timedelta(days=29), today
    raise ValueError(f"Unknown period: {period}")


def format_period_label(start: date, end: date) -> str:
    if start == end:
        return start.strftime("%d.%m.%Y")
    return f"{start.strftime('%d.%m.%Y')} — {end.strftime('%d.%m.%Y')}"


FIELD_LABELS = {"tr": "дата передачи", "ch": "дата проверки ИБ"}
STATUS_LABELS = {"ok": "прошли проверку", "fail": "не прошли", "all": "все"}
