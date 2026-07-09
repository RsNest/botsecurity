from bot.models import ImageRow
from bot.monitor import RegistryMonitor


def row(**overrides) -> ImageRow:
    fields = dict(
        row_number=4, transfer_date="01.07.2026", developer="Ivanov", tag="registry/api:1.0.0",
        corrected_tag="", release="R1", status="прошло проверку", check_date="03.07.2026",
        final_tag="", uploaded_mf="", actual_release_date="",
    )
    fields.update(overrides)
    return ImageRow(**fields)


def test_metrics_calculates_first_pass_and_duration() -> None:
    monitor = RegistryMonitor.__new__(RegistryMonitor)
    monitor._last_rows = [row(), row(row_number=5, corrected_tag="registry/api:1.0.1", status="не прошло проверку")]
    metrics = monitor.quality_metrics()
    assert metrics["terminal"] == 2
    assert metrics["first_pass_rate"] == 50.0
    assert metrics["avg_check_days"] == 2.0
