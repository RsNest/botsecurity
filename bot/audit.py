"""Registry row validation — detect manual entry / column-shift issues."""

from __future__ import annotations

import re
from dataclasses import dataclass

from bot.config import (
    STATUS_FAILED,
    STATUS_NOT_TRANSFERRED,
    STATUS_ON_REVIEW,
    STATUS_PASSED,
)
from bot.models import ImageRow

_STATUS_PHRASES = (
    STATUS_ON_REVIEW,
    STATUS_PASSED,
    STATUS_FAILED,
    STATUS_NOT_TRANSFERRED,
)

_IMAGE_HINTS = ("harbor", "http://", "https://", "uis.st", "images/")


@dataclass(frozen=True)
class AuditIssue:
    row_number: int
    kind: str
    message: str

    def key(self) -> str:
        return f"{self.row_number}:{self.kind}"


def _contains_status(text: str) -> bool:
    lowered = text.strip().lower()
    if not lowered:
        return False
    return any(phrase in lowered for phrase in _STATUS_PHRASES)


def _looks_like_image_ref(text: str) -> bool:
    text = text.strip()
    if not text:
        return False
    lowered = text.lower()
    if any(hint in lowered for hint in _IMAGE_HINTS):
        return True
    return ":" in text or "/" in text


def _looks_like_surname_in_tag(tag: str) -> bool:
    tag = tag.strip()
    if not tag or _looks_like_image_ref(tag):
        return False
    if len(tag) > 25:
        return False
    if re.search(r"[\u0400-\u04FF]", tag) and not re.search(r"[\d:./\\]", tag):
        return True
    return False


def _invalid_developer(name: str) -> str | None:
    name = name.strip()
    if not name:
        return None
    if len(name) > 25:
        return "слишком длинное значение для фамилии"
    if _looks_like_image_ref(name):
        return "похоже на тег образа, не на фамилию"
    if _contains_status(name):
        return "похоже на статус проверки"
    return None


def _invalid_release(release: str) -> str | None:
    release = release.strip()
    if not release:
        return None
    if _looks_like_image_ref(release):
        return "похоже на тег образа, не на релиз"
    if _contains_status(release):
        return "похоже на статус проверки"
    if len(release) > 80:
        return "слишком длинное значение для релиза"
    return None


def audit_rows(rows: list[ImageRow]) -> list[AuditIssue]:
    """Return structural issues found in registry rows (non-blocking warnings)."""
    issues: list[AuditIssue] = []
    tag_index: dict[str, list[int]] = {}

    for row in rows:
        tag = row.tag.strip()
        developer = row.developer.strip()

        if not tag and (developer or row.transfer_date.strip()):
            issues.append(
                AuditIssue(
                    row.row_number,
                    "empty_tag",
                    "есть дата или разработчик, но тег пустой",
                )
            )

        dev_problem = _invalid_developer(developer)
        if dev_problem:
            issues.append(
                AuditIssue(row.row_number, "invalid_developer", dev_problem)
            )

        if tag and _looks_like_surname_in_tag(tag):
            issues.append(
                AuditIssue(
                    row.row_number,
                    "invalid_tag",
                    "в колонке «Тег» похоже на фамилию, а не образ",
                )
            )

        if tag and _contains_status(tag):
            issues.append(
                AuditIssue(
                    row.row_number,
                    "status_in_tag",
                    "статус попал в колонку «Тег» — вероятен сдвиг столбцов",
                )
            )

        rel_problem = _invalid_release(row.release)
        if rel_problem:
            issues.append(
                AuditIssue(row.row_number, "invalid_release", rel_problem)
            )

        if row.release.strip() and _contains_status(row.release):
            issues.append(
                AuditIssue(
                    row.row_number,
                    "status_in_release",
                    "статус попал в колонку «Релиз» — вероятен сдвиг столбцов",
                )
            )

        if tag:
            key = tag.lower()
            tag_index.setdefault(key, []).append(row.row_number)

    for tag_key, row_numbers in tag_index.items():
        if len(row_numbers) < 2:
            continue
        rows_label = ", ".join(str(n) for n in row_numbers[:5])
        suffix = "…" if len(row_numbers) > 5 else ""
        issues.append(
            AuditIssue(
                row_numbers[0],
                f"duplicate_tag:{tag_key[:40]}",
                f"дубликат тега (стр. {rows_label}{suffix})",
            )
        )

    issues.sort(key=lambda i: (i.row_number, i.kind))
    return issues
