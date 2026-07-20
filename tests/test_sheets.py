from bot.models import ImageRow
from bot.sheets import (
    ReconcileResult,
    _norm_tag,
    _row_tag_keys,
    _sheet_tag_keys,
)


def _row(n: int, tag: str, corrected: str = "", final: str = "") -> ImageRow:
    return ImageRow(
        row_number=n,
        transfer_date="01.01.2026",
        developer="Test",
        tag=tag,
        corrected_tag=corrected,
        release="r1",
        status="",
        check_date="",
        final_tag=final,
        uploaded_mf="",
        actual_release_date="",
    )


def test_norm_tag() -> None:
    assert _norm_tag("  Foo:1.0  ") == "foo:1.0"
    assert _norm_tag("") == ""
    assert _norm_tag(None) == ""


def test_row_and_sheet_tag_keys() -> None:
    rows = [
        _row(4, "api:1.0", corrected="api:1.1"),
        _row(5, "web:2.0", final="web:2.0-final"),
    ]
    assert _row_tag_keys(rows[0]) == {"api:1.0", "api:1.1"}
    keys = _sheet_tag_keys(rows)
    assert "api:1.0" in keys
    assert "api:1.1" in keys
    assert "web:2.0" in keys
    assert "web:2.0-final" in keys


def test_reconcile_result_defaults() -> None:
    result = ReconcileResult()
    assert result.mirror_enabled is False
    assert result.appended_to_canon == 0
    assert result.error is None
