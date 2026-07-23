from bot.report_export import build_failed_images_report
from bot.reports import Finding, ReportMatch, ScanReport


def _failed_match(**kwargs) -> ReportMatch:
    report = ScanReport(
        image=kwargs.get("image", "registry.example/app:1.0"),
        critical=kwargs.get("critical", 1),
        high=kwargs.get("high", 1),
        medium=0,
        low=0,
        total=2,
        findings=kwargs.get("findings", []),
        source="kaspersky",
    )
    return ReportMatch(report=report, row=None)


def test_build_failed_images_report_contains_findings() -> None:
    findings = [
        Finding("vulnerability", "CVE-2024-1", "critical", "openssl", "1.1.1"),
        Finding("sensitive", "secret.env", "high", "/app/secret.env"),
    ]
    failed = _failed_match(findings=findings, critical=1, high=1)
    passed = ReportMatch(
        report=ScanReport(
            image="registry.example/ok:1",
            critical=0,
            high=0,
            medium=0,
            low=0,
            total=0,
            source="kaspersky",
        ),
        row=None,
    )
    filename, payload = build_failed_images_report([failed, passed])
    assert filename.startswith("ib-failed-")
    assert filename.endswith(".md")
    text = payload.decode("utf-8")
    assert "Непрошедших: 1 из 2" in text
    assert "CVE-2024-1" in text
    assert "secret.env" in text
    assert "ok:1" not in text or "Не прошли" in text
    # passed image should not appear as a section
    assert "## 2." not in text
