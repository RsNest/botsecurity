from pathlib import Path

from bot.storage import Storage


def test_roles_preferences_and_history_are_persisted(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "test.db")
    storage.set_role(42, "ib_operator")
    storage.set_notification_mode(42, "fail")
    storage.log_row_history(7, "updated", "{}", "{}")

    assert storage.is_ib_operator(42)
    assert storage.notification_mode(42) == "fail"
    assert storage.row_history(7)[0]["change_type"] == "updated"
