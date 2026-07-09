import io
import json
import zipfile

import pytest

from bot.reports import ReportParseError, _read_zip, extract_reports


def test_extract_reports_reads_valid_zip() -> None:
    payload = {"image": "harbor.example/api:1.2.3", "vulnerability_summary": {"critical": 0, "high": 0}}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("result/api.json", json.dumps(payload))
    reports = extract_reports(buffer.getvalue(), "report.zip")
    assert len(reports) == 1
    assert reports[0].passed


def test_zip_rejects_path_traversal() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../outside.json", "{}")
    with pytest.raises(ReportParseError, match="небезопасный путь"):
        _read_zip(buffer.getvalue())
