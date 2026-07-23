import io
import json
import zipfile

import pytest

from bot.reports import (
    ReportParseError,
    _parse_kaspersky_html,
    _read_zip,
    extract_reports,
)


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


_KASPERSKY_HTML = """<!DOCTYPE html>
<html><head><title>registry.example/app:1.2.3</title></head>
<body>
<section class="stats">
  <div class="block">
    <div class="block-header"><span>Vulnerabilities</span></div>
    <div class="block-sum"><span>2</span></div>
    <div class="block-stat">
      <div class="stat critical"><span>1</span> Critical</div>
      <div class="stat high"><span>1</span> High</div>
      <div class="stat medium"><span>0</span> Medium</div>
      <div class="stat low"><span>0</span> Low</div>
      <div class="stat negligible zero"><span>0</span> Negligible</div>
    </div>
  </div>
  <div class="block">
    <div class="block-header"><span>Malware</span></div>
    <div class="block-sum"><span>0</span></div>
    <div class="block-stat">
      <div class="stat critical"><span>0</span> Critical</div>
      <div class="stat high"><span>0</span> High</div>
      <div class="stat medium"><span>0</span> Medium</div>
      <div class="stat low"><span>0</span> Low</div>
      <div class="stat negligible zero"><span>0</span> Negligible</div>
    </div>
  </div>
  <div class="block">
    <div class="block-header"><span>Sensitive data</span></div>
    <div class="block-sum"><span>0</span></div>
    <div class="block-stat">
      <div class="stat critical"><span>0</span> Critical</div>
      <div class="stat high"><span>0</span> High</div>
      <div class="stat medium"><span>0</span> Medium</div>
      <div class="stat low"><span>0</span> Low</div>
      <div class="stat negligible zero"><span>0</span> Negligible</div>
    </div>
  </div>
  <div class="block">
    <div class="block-header"><span>Misconfiguration</span></div>
    <div class="block-sum"><span>0</span></div>
    <div class="block-stat">
      <div class="stat critical"><span>0</span> Critical</div>
      <div class="stat high"><span>0</span> High</div>
      <div class="stat medium"><span>0</span> Medium</div>
      <div class="stat low"><span>1</span> Low</div>
      <div class="stat negligible zero"><span>0</span> Negligible</div>
    </div>
  </div>
</section>
<section class="vulnerabilities">
  <h2>Vulnerabilities</h2>
  <table><tbody>
    <tr>
      <td>CVE-2024-1111</td>
      <td><div class="stat critical"> Critical</div></td>
      <td>openssl</td>
      <td>1.1.1w</td>
    </tr>
    <tr>
      <td>CVE-2024-2222</td>
      <td><div class="stat high"> High</div></td>
      <td>curl</td>
      <td>-</td>
    </tr>
    <tr>
      <td>CVE-2024-3333</td>
      <td><div class="stat medium"> Medium</div></td>
      <td>zlib</td>
      <td>-</td>
    </tr>
  </tbody></table>
</section>
<section class="misconfigs">
  <table><tbody>
    <tr>
      <td><p>Dockerfile</p></td>
      <td>dockerfile</td>
      <td>No HEALTHCHECK defined</td>
      <td><div class="stat low">Low</div></td>
    </tr>
  </tbody></table>
</section>
</body></html>
"""


def test_parse_kaspersky_html() -> None:
    report = _parse_kaspersky_html("app.html", _KASPERSKY_HTML.encode())
    assert report is not None
    assert report.image == "registry.example/app:1.2.3"
    assert report.source == "kaspersky"
    assert report.critical == 1
    assert report.high == 1
    assert not report.passed
    assert report.verdict_status == "Не прошло проверку"
    assert len(report.findings) == 2
    assert report.findings[0].title == "CVE-2024-1111"
    assert report.findings[0].severity == "critical"
    assert report.findings[1].resource == "curl"


def test_extract_reports_reads_kaspersky_html_zip() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("images/app.html", _KASPERSKY_HTML)
        archive.writestr("report.html", "<title>Kaspersky Container Scan Summary</title>")
    reports = extract_reports(buffer.getvalue(), "kcs.zip")
    assert len(reports) == 1
    assert reports[0].critical == 1


def test_kaspersky_sensitive_fails_even_without_vulns() -> None:
    html = """<!DOCTYPE html><html><head><title>reg/x:1</title></head><body>
<section class="stats">
  <div class="block">
    <div class="block-header"><span>Vulnerabilities</span></div>
    <div class="block-sum"><span>0</span></div>
    <div class="block-stat">
      <div class="stat critical"><span>0</span> Critical</div>
      <div class="stat high"><span>0</span> High</div>
      <div class="stat medium"><span>0</span> Medium</div>
      <div class="stat low"><span>0</span> Low</div>
      <div class="stat negligible zero"><span>0</span> Negligible</div>
    </div>
  </div>
  <div class="block">
    <div class="block-header"><span>Malware</span></div>
    <div class="block-sum"><span>0</span></div>
    <div class="block-stat">
      <div class="stat critical"><span>0</span> Critical</div>
      <div class="stat high"><span>0</span> High</div>
      <div class="stat medium"><span>0</span> Medium</div>
      <div class="stat low"><span>0</span> Low</div>
      <div class="stat negligible zero"><span>0</span> Negligible</div>
    </div>
  </div>
  <div class="block">
    <div class="block-header"><span>Sensitive data</span></div>
    <div class="block-sum"><span>1</span></div>
    <div class="block-stat">
      <div class="stat critical"><span>1</span> Critical</div>
      <div class="stat high"><span>0</span> High</div>
      <div class="stat medium"><span>0</span> Medium</div>
      <div class="stat low"><span>0</span> Low</div>
      <div class="stat negligible zero"><span>0</span> Negligible</div>
    </div>
  </div>
  <div class="block">
    <div class="block-header"><span>Misconfiguration</span></div>
    <div class="block-sum"><span>0</span></div>
    <div class="block-stat">
      <div class="stat critical"><span>0</span> Critical</div>
      <div class="stat high"><span>0</span> High</div>
      <div class="stat medium"><span>0</span> Medium</div>
      <div class="stat low"><span>0</span> Low</div>
      <div class="stat negligible zero"><span>0</span> Negligible</div>
    </div>
  </div>
</section>
<section class="sensitive">
  <table><tbody>
    <tr><td>secret.env</td><td><div class="stat critical">Critical</div></td><td>/app/secret.env</td></tr>
  </tbody></table>
</section>
</body></html>"""
    report = _parse_kaspersky_html("x.html", html.encode())
    assert report is not None
    assert report.critical == 0
    assert report.sensitive_critical == 1
    assert not report.passed
    assert len(report.findings) == 1
    assert report.findings[0].kind == "sensitive"
