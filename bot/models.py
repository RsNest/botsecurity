from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime


from bot.dates import parse_flexible_date

def _normalize(value: str | None) -> str:
    return (value or "").strip()


def normalize_status(value: str | None) -> str:
    return _normalize(value).lower()


@dataclass(frozen=True)
class ImageRow:
    row_number: int
    transfer_date: str
    developer: str
    tag: str
    corrected_tag: str
    release: str
    status: str
    check_date: str
    final_tag: str
    uploaded_mf: str
    actual_release_date: str

    @classmethod
    def from_sheet_row(cls, row_number: int, cells: list[str]) -> ImageRow | None:
        padded = (cells + [""] * 10)[:10]
        tag = _normalize(padded[2])
        developer = _normalize(padded[1])
        if not tag and not developer:
            return None
        return cls(
            row_number=row_number,
            transfer_date=_normalize(padded[0]),
            developer=developer,
            tag=tag,
            corrected_tag=_normalize(padded[3]),
            release=_normalize(padded[4]),
            status=_normalize(padded[5]),
            check_date=_normalize(padded[6]),
            final_tag=_normalize(padded[7]),
            uploaded_mf=_normalize(padded[8]),
            actual_release_date=_normalize(padded[9]),
        )

    def content_hash(self) -> str:
        payload = asdict(self)
        payload.pop("row_number", None)
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    def status_normalized(self) -> str:
        return normalize_status(self.status)

    def is_pending_ops(self) -> bool:
        status = self.status_normalized()
        if not status:
            return True
        return status == "не передано"

    def is_on_review(self) -> bool:
        return self.status_normalized() == "на проверке"

    def is_failed(self) -> bool:
        return self.status_normalized() == "не прошло проверку"

    def is_passed(self) -> bool:
        return self.status_normalized() == "прошло проверку"

    def parse_check_date(self) -> date | None:
        return parse_flexible_date(self.check_date)

    def date_for_field(self, field: str) -> date | None:
        if field == "ch":
            return self.parse_check_date() or parse_flexible_date(self.actual_release_date)
        return self.parse_transfer_date()

    def to_payload(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_payload(cls, raw: str) -> ImageRow | None:
        if not raw or raw == "{}":
            return None
        try:
            data = json.loads(raw)
            return cls(**data)
        except (json.JSONDecodeError, TypeError):
            return None

    def is_terminal(self) -> bool:
        return self.status_normalized() in {
            "прошло проверку",
            "не прошло проверку",
        }

    def short_tag(self, max_len: int = 60) -> str:
        tag = self.tag or "—"
        if len(tag) <= max_len:
            return tag
        return tag[: max_len - 1] + "…"

    def parse_transfer_date(self) -> date | None:
        return parse_flexible_date(self.transfer_date)


@dataclass
class RowChange:
    row: ImageRow
    change_type: str  # new | updated
    changed_fields: dict[str, tuple[str, str]]


@dataclass
class ScanResult:
    rows: list[ImageRow]
    changes: list[RowChange]
    fetched_at: datetime
