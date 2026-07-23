"""Parsing IB scan report archives and matching them to sheet rows.

Supports:
- Aqua-style JSON reports (``result/*.json`` with ``vulnerability_summary``)
- Kaspersky Container Security HTML reports (per-image ``*.html``)

Verdict rule: any critical or high finding in vulnerabilities, malware,
sensitive data or misconfiguration → "Не прошло проверку", otherwise
"Прошло проверку".
"""

from __future__ import annotations

import html as html_lib
import io
import json
import logging
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import py7zr

from bot.models import ImageRow

logger = logging.getLogger(__name__)

MAX_ARCHIVE_SIZE = 20 * 1024 * 1024  # Telegram bot download limit
# A small upload may expand to many gigabytes.  These limits apply after
# decompression and keep report parsing bounded even for an admin upload.
MAX_ARCHIVE_FILES = 500
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_REPORT_FILE_BYTES = 2 * 1024 * 1024
MAX_JSON_BYTES = MAX_REPORT_FILE_BYTES  # alias for tests / callers
# Cap stored findings so huge images (thousands of CVEs) stay Telegram-friendly.
MAX_FINDINGS_PER_REPORT = 80

_VERSION_RE = re.compile(r"(\d+(?:\.\d+)+)")
# Tokens that carry no identity (registry paths, common noise prefixes)
_NOISE_TOKENS = {"images", "image", "harbor", "uis", "st", "mrkt", "nx"}

_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.I | re.S)
_STATS_SECTION_RE = re.compile(
    r'<section class="stats">(.*?)(?=<section class="|\Z)', re.S
)
_BLOCK_STAT_RE = re.compile(
    r'<div class="block-header">\s*<span>([^<]+)</span>.*?'
    r'<div class="block-stat">(.*?)</div>\s*</div>',
    re.S,
)
_STAT_COUNT_RE = re.compile(
    r'class="stat (\w+)(?:\s+\w+)*"[^>]*>\s*<span>(\d+)</span>',
)
_VULN_ROW_RE = re.compile(
    r"<tr>\s*<td>([^<]+)</td>\s*"
    r'<td><div class="stat (\w+)[^"]*">\s*([^<]*)</div></td>\s*'
    r"<td>([^<]*)</td>\s*<td>([^<]*)</td>",
    re.S,
)
_SENSITIVE_ROW_RE = re.compile(
    r"<tr>\s*<td>([^<]+)</td>\s*"
    r'<td><div class="stat (\w+)[^"]*">\s*([^<]*)</div></td>\s*'
    r"<td>([^<]*)</td>",
    re.S,
)
_MISCONFIG_ROW_RE = re.compile(
    r"<tr>\s*<td>.*?</td>\s*"
    r"<td>([^<]+)</td>\s*"
    r"<td>([^<]+)</td>\s*"
    r'<td><div class="stat (\w+)[^"]*">\s*([^<]*)</div></td>',
    re.S,
)
_SECTION_RE = {
    "vulnerabilities": re.compile(
        r'<section class="vulnerabilities">(.*?)(?=<section class="|\Z)', re.S
    ),
    "malware": re.compile(
        r'<section class="malware">(.*?)(?=<section class="|\Z)', re.S
    ),
    "sensitive": re.compile(
        r'<section class="sensitive">(.*?)(?=<section class="|\Z)', re.S
    ),
    "misconfigs": re.compile(
        r'<section class="misconfigs">(.*?)(?=<section class="|\Z)', re.S
    ),
}

_SUMMARY_TITLES = {
    "kaspersky container scan summary",
    "container scan summary",
}

_SEVERITY_RANK = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
    "negligible": 4,
    "unknown": 5,
}


@dataclass
class Finding:
    kind: str  # vulnerability | malware | sensitive | misconfiguration
    title: str
    severity: str
    resource: str = ""
    fix: str = ""


@dataclass
class ScanReport:
    image: str  # full image ref from the report
    critical: int
    high: int
    medium: int
    low: int
    total: int
    findings: list[Finding] = field(default_factory=list)
    malware_critical: int = 0
    malware_high: int = 0
    sensitive_critical: int = 0
    sensitive_high: int = 0
    misconfig_critical: int = 0
    misconfig_high: int = 0
    source: str = "aqua"  # aqua | kaspersky

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
        return (
            self.critical == 0
            and self.high == 0
            and self.malware_critical == 0
            and self.malware_high == 0
            and self.sensitive_critical == 0
            and self.sensitive_high == 0
            and self.misconfig_critical == 0
            and self.misconfig_high == 0
        )

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
    """Extract per-image scan reports from a 7z or zip archive."""
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
        lower = fname.lower()
        if lower.endswith(".json"):
            report = _parse_report_json(fname, content)
        elif lower.endswith(".html"):
            report = _parse_kaspersky_html(fname, content)
        else:
            continue
        if report:
            reports.append(report)

    if not reports:
        raise ReportParseError(
            "В архиве не найдено отчётов сканирования "
            "(ожидаются result/*.json или HTML-отчёты Kaspersky по образам)."
        )
    reports.sort(key=lambda r: (-(r.critical + r.sensitive_critical + r.malware_critical + r.misconfig_critical),
                                 -(r.high + r.sensitive_high + r.malware_high + r.misconfig_high),
                                 r.image))
    return reports


def _is_report_member(name: str) -> bool:
    lower = name.lower().replace("\\", "/")
    base = PurePosixPath(lower).name
    if base in {"report.html", "index.html"}:
        return False
    if base.endswith(".log") or "kcs-scan.log" in lower:
        return False
    return lower.endswith(".json") or lower.endswith(".html")


def _read_7z(data: bytes) -> dict[str, bytes]:
    try:
        with tempfile.TemporaryDirectory(prefix="ibscan_") as tmp:
            with py7zr.SevenZipFile(io.BytesIO(data)) as archive:
                info = archive.list()
                _validate_archive_members(
                    [(item.filename, getattr(item, "uncompressed", None)) for item in info]
                )
                targets = [
                    item.filename for item in info if _is_report_member(item.filename)
                ]
                if targets:
                    archive.extract(path=tmp, targets=targets)
            root = Path(tmp)
            result: dict[str, bytes] = {}
            for path in root.rglob("*"):
                if not path.is_file() or not _is_report_member(str(path.relative_to(root))):
                    continue
                size = path.stat().st_size
                if size > MAX_REPORT_FILE_BYTES:
                    raise ReportParseError(f"Отчёт слишком большой: {path.name}")
                result[str(path.relative_to(root))] = path.read_bytes()
            return result
    except py7zr.exceptions.Bad7zFile as exc:
        raise ReportParseError(f"Не удалось открыть 7z-архив: {exc}") from exc


def _read_zip(data: bytes) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            _validate_archive_members([(info.filename, info.file_size) for info in infos])
            result: dict[str, bytes] = {}
            for info in infos:
                if not _is_report_member(info.filename):
                    continue
                if info.file_size > MAX_REPORT_FILE_BYTES:
                    raise ReportParseError(f"Отчёт слишком большой: {info.filename}")
                result[info.filename] = archive.read(info)
            return result
    except zipfile.BadZipFile as exc:
        raise ReportParseError(f"Не удалось открыть zip-архив: {exc}") from exc


def _validate_archive_members(members: list[tuple[str, int | None]]) -> None:
    """Reject traversal, archive bombs and archives with unknown huge members."""
    if len(members) > MAX_ARCHIVE_FILES:
        raise ReportParseError(f"В архиве слишком много файлов (максимум {MAX_ARCHIVE_FILES}).")
    total = 0
    for name, size in members:
        path = PurePosixPath(name.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts or not name:
            raise ReportParseError("Архив содержит небезопасный путь к файлу.")
        if size is None or size < 0:
            raise ReportParseError("Не удалось безопасно определить размер файла в архиве.")
        total += size
        if total > MAX_UNCOMPRESSED_BYTES:
            raise ReportParseError(
                f"Распакованный архив больше лимита {MAX_UNCOMPRESSED_BYTES // 1024 // 1024} МБ."
            )


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
        source="aqua",
    )


def _norm_severity(value: str) -> str:
    return (value or "").strip().lower()


def _counts_from_block(block_html: str) -> dict[str, int]:
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "negligible": 0}
    for sev, num in _STAT_COUNT_RE.findall(block_html):
        key = sev.lower()
        if key in counts:
            counts[key] = int(num)
    return counts


def _parse_kaspersky_html(fname: str, content: bytes) -> ScanReport | None:
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception:
        return None

    title_m = _TITLE_RE.search(text)
    if not title_m:
        return None
    image = html_lib.unescape(title_m.group(1)).strip()
    if not image or image.lower() in _SUMMARY_TITLES:
        return None

    vuln = {"critical": 0, "high": 0, "medium": 0, "low": 0, "negligible": 0}
    malware = dict(vuln)
    sensitive = dict(vuln)
    misconfig = dict(vuln)

    stats_m = _STATS_SECTION_RE.search(text)
    if stats_m:
        for title, block in _BLOCK_STAT_RE.findall(stats_m.group(1)):
            counts = _counts_from_block(block)
            key = title.strip().lower()
            if key.startswith("vulnerabilit"):
                vuln = counts
            elif key.startswith("malware"):
                malware = counts
            elif key.startswith("sensitive"):
                sensitive = counts
            elif key.startswith("misconfig"):
                misconfig = counts

    findings: list[Finding] = []

    vuln_body = _SECTION_RE["vulnerabilities"].search(text)
    if vuln_body:
        for title, sev, _label, resource, fix in _VULN_ROW_RE.findall(vuln_body.group(1)):
            severity = _norm_severity(sev)
            if severity not in ("critical", "high"):
                continue
            findings.append(
                Finding(
                    kind="vulnerability",
                    title=html_lib.unescape(title.strip()),
                    severity=severity,
                    resource=html_lib.unescape(resource.strip()),
                    fix=html_lib.unescape(fix.strip().strip("-")),
                )
            )

    mal_body = _SECTION_RE["malware"].search(text)
    if mal_body:
        for title, sev, _label, resource, fix in _VULN_ROW_RE.findall(mal_body.group(1)):
            severity = _norm_severity(sev)
            if severity not in ("critical", "high"):
                continue
            findings.append(
                Finding(
                    kind="malware",
                    title=html_lib.unescape(title.strip()),
                    severity=severity,
                    resource=html_lib.unescape(resource.strip()),
                    fix=html_lib.unescape(fix.strip().strip("-")),
                )
            )

    sens_body = _SECTION_RE["sensitive"].search(text)
    if sens_body:
        for title, sev, _label, path in _SENSITIVE_ROW_RE.findall(sens_body.group(1)):
            severity = _norm_severity(sev)
            if severity not in ("critical", "high"):
                continue
            findings.append(
                Finding(
                    kind="sensitive",
                    title=html_lib.unescape(title.strip()),
                    severity=severity,
                    resource=html_lib.unescape(path.strip()),
                )
            )

    misc_body = _SECTION_RE["misconfigs"].search(text)
    if misc_body:
        for _type, problem, sev, _label in _MISCONFIG_ROW_RE.findall(misc_body.group(1)):
            severity = _norm_severity(sev)
            if severity not in ("critical", "high"):
                continue
            findings.append(
                Finding(
                    kind="misconfiguration",
                    title=html_lib.unescape(problem.strip()),
                    severity=severity,
                    resource=html_lib.unescape(_type.strip()),
                )
            )

    findings.sort(
        key=lambda f: (_SEVERITY_RANK.get(f.severity, 9), f.kind, f.title, f.resource)
    )
    if len(findings) > MAX_FINDINGS_PER_REPORT:
        findings = findings[:MAX_FINDINGS_PER_REPORT]

    total = sum(vuln.get(k, 0) for k in ("critical", "high", "medium", "low", "negligible"))
    return ScanReport(
        image=image,
        critical=vuln["critical"],
        high=vuln["high"],
        medium=vuln["medium"],
        low=vuln["low"],
        total=total,
        findings=findings,
        malware_critical=malware["critical"],
        malware_high=malware["high"],
        sensitive_critical=sensitive["critical"],
        sensitive_high=sensitive["high"],
        misconfig_critical=misconfig["critical"],
        misconfig_high=misconfig["high"],
        source="kaspersky",
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
