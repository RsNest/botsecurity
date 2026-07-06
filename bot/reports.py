"""Parsing IB (Aqua) scan report archives and matching them to sheet rows.

The IB team sends a 7z/zip archive with per-image JSON reports
(result/<image>-[<tag>].json). Each JSON has a ``vulnerability_summary``
with critical/high/... counts. Verdict rule: any critical or high
vulnerability -> "Не прошло проверку", otherwise "Прошло проверку".
"""

from __future__ import annotations

import io
import json
import logging
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import py7zr

from bot.models import ImageRow

logger = logging.getLogger(__name__)

MAX_ARCHIVE_SIZE = 20 * 1024 * 1024  # Telegram bot download limit

_VERSION_RE = re.compile(r"(\d+(?:\.\d+)+)")
# Tokens that carry no identity (registry paths, common noise prefixes)
_NOISE_TOKENS = {"images", "image", "harbor", "uis", "st", "mrkt", "nx"}


@dataclass
class ScanReport:
    image: str          # full image ref from the report
    critical: int
    high: int
    medium: int
    low: int
    total: int

    @property
    def short_name(self) -> str:
        name = self.image
        if "/" in name:
            first, rest = name.split("/", 1)
            if "." in first:  # registry host
                name = rest
        return name

    @property
    def passed(self) -> bool:
        return self.critical == 0 and self.high == 0

    @property
    def verdict_status(self) -> str:
        return "Прошло проверку" if self.passed else "Не прошло проверку"


# Matches below this score need manual review before applying statuses.
LOW_CONFIDENCE_THRESHOLD = 1.15


@dataclass
class ReportMatch:
    report: ScanReport
    row: ImageRow | None
    score: float = 0.0
    candidates: list[ImageRow] = field(default_factory=list)


# --- Archive parsing ---------------------------------------------------------

class ReportParseError(Exception):
    pass


def extract_reports(data: bytes, filename: str) -> list[ScanReport]:
    """Extract per-image scan JSONs from a 7z or zip archive."""
    name = filename.lower()
    if name.endswith(".7z"):
        files = _read_7z(data)
    elif name.endswith(".zip"):
        files = _read_zip(data)
    else:
        raise ReportParseError(
            "Поддерживаются только архивы .7z и .zip с отчётами сканирования."
        )

    reports: list[ScanReport] = []
    for fname, content in files.items():
        if not fname.lower().endswith(".json"):
            continue
        report = _parse_report_json(fname, content)
        if report:
            reports.append(report)

    if not reports:
        raise ReportParseError(
            "В архиве не найдено JSON-отчётов сканирования "
            "(ожидаются файлы result/*.json с vulnerability_summary)."
        )
    reports.sort(key=lambda r: (-(r.critical), -(r.high), r.image))
    return reports


def _read_7z(data: bytes) -> dict[str, bytes]:
    try:
        with tempfile.TemporaryDirectory(prefix="ibscan_") as tmp:
            with py7zr.SevenZipFile(io.BytesIO(data)) as archive:
                archive.extractall(tmp)
            root = Path(tmp)
            return {
                str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
    except py7zr.exceptions.Bad7zFile as exc:
        raise ReportParseError(f"Не удалось открыть 7z-архив: {exc}") from exc


def _read_zip(data: bytes) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return {
                info.filename: archive.read(info)
                for info in archive.infolist()
                if not info.is_dir()
            }
    except zipfile.BadZipFile as exc:
        raise ReportParseError(f"Не удалось открыть zip-архив: {exc}") from exc


def _parse_report_json(fname: str, content: bytes) -> ScanReport | None:
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    summary = data.get("vulnerability_summary")
    image = data.get("image") or data.get("pull_name")
    if not summary or not image:
        return None
    return ScanReport(
        image=str(image),
        critical=int(summary.get("critical", 0) or 0),
        high=int(summary.get("high", 0) or 0),
        medium=int(summary.get("medium", 0) or 0),
        low=int(summary.get("low", 0) or 0),
        total=int(summary.get("total", 0) or 0),
    )


# --- Matching reports to sheet rows ------------------------------------------

def _tokenize(name: str) -> set[str]:
    tokens = {t for t in re.split(r"[/:\-_.\[\]]+", name.lower()) if t}
    return {t for t in tokens if not t.isdigit()} - _NOISE_TOKENS


def _strip_registry(ref: str) -> str:
    ref = ref.strip()
    if "/" in ref:
        first, rest = ref.split("/", 1)
        if "." in first:
            return rest
    return ref


def _sheet_tag_parts(tag: str) -> tuple[set[str], str]:
    stripped = _strip_registry(tag)
    versions = _VERSION_RE.findall(stripped)
    version = versions[-1] if versions else ""
    return _tokenize(_VERSION_RE.sub("", stripped)), version


def _report_parts(image: str) -> tuple[set[str], str]:
    stripped = _strip_registry(image)
    name, _, version = stripped.rpartition(":")
    if not name:
        name, version = stripped, ""
    return _tokenize(name), version


def _score(report: ScanReport, row: ImageRow) -> float:
    """Best similarity between the report image and any of the row's tags."""
    rtoks, rver = _report_parts(report.image)
    if not rtoks:
        return 0.0
    best = 0.0
    for tag in (row.tag, row.corrected_tag, row.final_tag):
        if not tag or "://" in tag:  # skip URLs pasted into tag columns
            continue
        stoks, sver = _sheet_tag_parts(tag)
        if not stoks:
            continue
        # A version mismatch is a hard no: same image, different build.
        if rver and sver and rver != sver:
            continue
        inter = len(rtoks & stoks)
        union = len(rtoks | stoks)
        jaccard = inter / union if union else 0.0
        version_bonus = 0.5 if (rver and sver and rver == sver) else 0.0
        threshold = 0.32 if version_bonus else 0.6
        if jaccard < threshold:
            continue
        best = max(best, jaccard + version_bonus)
    return best


def match_reports(
    reports: list[ScanReport],
    rows: list[ImageRow],
) -> list[ReportMatch]:
    """Greedy one-to-one assignment: strongest matches claim rows first,
    so e.g. "legacy-api" doesn't steal the row from plain "api"."""
    scored: list[tuple[float, int, int]] = []  # (score, report_idx, row_idx)
    for ri, report in enumerate(reports):
        for wi, row in enumerate(rows):
            s = _score(report, row)
            if s > 0:
                scored.append((s, ri, wi))
    # On equal similarity prefer rows still awaiting a verdict, then the
    # newest row: a re-submitted image should claim the fresh row, not the
    # old one whose corrected_tag happens to mention the same version.
    scored.sort(
        key=lambda x: (
            -x[0],
            rows[x[2]].is_terminal(),
            -rows[x[2]].row_number,
        )
    )

    report_match: dict[int, tuple[float, int]] = {}
    taken_rows: set[int] = set()
    for s, ri, wi in scored:
        if ri in report_match or wi in taken_rows:
            continue
        report_match[ri] = (s, wi)
        taken_rows.add(wi)

    result: list[ReportMatch] = []
    for ri, report in enumerate(reports):
        if ri in report_match:
            s, wi = report_match[ri]
            result.append(ReportMatch(report=report, row=rows[wi], score=s))
        else:
            result.append(ReportMatch(report=report, row=None))
    return result


def is_low_confidence(match: ReportMatch) -> bool:
    """True when sheet row mapping is uncertain and should not be auto-applied."""
    return match.row is not None and match.score < LOW_CONFIDENCE_THRESHOLD
