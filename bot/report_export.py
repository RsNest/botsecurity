"""Export human-readable IB scan digests for Telegram document download."""

from __future__ import annotations

from datetime import datetime, timezone

from bot.reports import ReportMatch, is_low_confidence

_KIND_LABEL = {
    "vulnerability": "CVE",
    "malware": "malware",
    "sensitive": "secret",
    "misconfiguration": "misconfig",
}


def build_failed_images_report(matches: list[ReportMatch]) -> tuple[str, bytes]:
    """Build a Markdown report for images that failed the scan.

    Returns ``(filename, utf-8 bytes)``.
    """
    failed = [m for m in matches if not m.report.passed]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    filename = f"ib-failed-{stamp}.md"

    lines: list[str] = [
        "# Отчёт ИБ — непрошедшие образы",
        "",
        f"Сформирован: {now}",
        f"Непрошедших: {len(failed)} из {len(matches)}",
        "",
        "Ниже по каждому образу — сводка и список Critical/High "
        "(vulns / secrets / malware / misconfig), которые успели "
        "извлечься из HTML/JSON отчёта.",
        "",
        "---",
        "",
    ]

    for i, match in enumerate(failed, start=1):
        r = match.report
        lines.append(f"## {i}. `{r.short_name}`")
        lines.append("")
        lines.append(f"- Полный тег: `{r.image}`")
        lines.append(f"- Вердикт: **{r.verdict_status}**")
        lines.append(
            f"- Vulns: critical={r.critical}, high={r.high}, "
            f"medium={r.medium}, low={r.low} (total={r.total})"
        )
        if r.sensitive_critical or r.sensitive_high:
            lines.append(
                f"- Secrets: critical={r.sensitive_critical}, "
                f"high={r.sensitive_high}"
            )
        if r.malware_critical or r.malware_high:
            lines.append(
                f"- Malware: critical={r.malware_critical}, "
                f"high={r.malware_high}"
            )
        if r.misconfig_critical or r.misconfig_high:
            lines.append(
                f"- Misconfig: critical={r.misconfig_critical}, "
                f"high={r.misconfig_high}"
            )
        if match.row:
            conf = "сомнительно" if is_low_confidence(match) else "ок"
            lines.append(
                f"- Таблица: стр. {match.row.row_number}, "
                f"`{match.row.tag}` ({conf})"
            )
        else:
            lines.append("- Таблица: нет совпадения")

        findings = r.findings
        expected_ch = (
            r.critical
            + r.high
            + r.sensitive_critical
            + r.sensitive_high
            + r.malware_critical
            + r.malware_high
            + r.misconfig_critical
            + r.misconfig_high
        )
        lines.append("")
        if findings:
            lines.append(f"### Critical / High findings ({len(findings)})")
            lines.append("")
            lines.append("| Severity | Type | Title | Resource | Fix |")
            lines.append("|---|---|---|---|---|")
            for f in findings:
                kind = _KIND_LABEL.get(f.kind, f.kind)
                title = _md_cell(f.title)
                resource = _md_cell(f.resource)
                fix = _md_cell(f.fix) if f.fix else "—"
                lines.append(
                    f"| {f.severity} | {kind} | `{title}` | {resource} | {fix} |"
                )
            if expected_ch > len(findings):
                lines.append("")
                lines.append(
                    f"_Показано {len(findings)} из ~{expected_ch} "
                    "critical/high по сводке; полный список — в исходном HTML._"
                )
        else:
            lines.append("### Findings")
            lines.append("")
            if expected_ch:
                lines.append(
                    f"_В сводке ~{expected_ch} critical/high, но детальный "
                    "список в архиве не распознан._"
                )
            else:
                lines.append("_Детальный список пуст._")
        lines.append("")
        lines.append("---")
        lines.append("")

    payload = "\n".join(lines).encode("utf-8")
    return filename, payload


def _md_cell(value: str) -> str:
    return (value or "").replace("|", "\\|").replace("\n", " ").strip()
