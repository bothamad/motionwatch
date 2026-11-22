from datetime import datetime

import pytest

from motionwatch.schedule import Schedule, parse_days, parse_time


def at(day: str, hhmm: str) -> datetime:
    # 2026-11-23 is a Monday
    offset = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(day)
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 11, 23 + offset, h, m)


def test_parse_time():
    assert parse_time("00:00") == 0
    assert parse_time("07:30") == 450
    assert parse_time("24:00") == 1440
    for bad in ["25:00", "12:60", "noon", "24:01"]:
        with pytest.raises(ValueError):
            parse_time(bad)


def test_parse_days_aliases():
    assert parse_days(["weekdays"]) == {0, 1, 2, 3, 4}
    assert parse_days(["Saturday", "sun"]) == {5, 6}
    with pytest.raises(ValueError):
        parse_days(["funday"])


def test_same_day_window():
    s = Schedule.from_config("work", [{"days": ["mon"], "start": "09:00", "end": "17:00"}])
    assert s.is_active(at("mon", "09:00"))
    assert s.is_active(at("mon", "16:59"))
    assert not s.is_active(at("mon", "17:00"))
    assert not s.is_active(at("tue", "10:00"))


def test_overnight_window_belongs_to_start_day():
    s = Schedule.from_config("night", [{"days": ["fri"], "start": "22:00", "end": "06:00"}])
    assert s.is_active(at("fri", "23:00"))
    assert s.is_active(at("sat", "05:59"))
    assert not s.is_active(at("sat", "06:00"))
    assert not s.is_active(at("fri", "05:00"))  # thursday night not scheduled


def test_full_day_and_always():
    s = Schedule.from_config("wk", [{"days": ["weekends"], "start": "00:00", "end": "24:00"}])
    assert s.is_active(at("sat", "00:00"))
    assert s.is_active(at("sun", "23:59"))
    assert not s.is_active(at("mon", "00:00"))
    assert Schedule.always().is_active(at("wed", "12:00"))
