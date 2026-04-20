from datetime import UTC, datetime

from valveye.time_utils import is_summer_time, runtime_offset_hours


def test_winter_offset_is_3():
    dt = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
    assert is_summer_time(dt) is False
    assert runtime_offset_hours(dt) == 3


def test_summer_offset_is_4():
    dt = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    assert is_summer_time(dt) is True
    assert runtime_offset_hours(dt) == 4
