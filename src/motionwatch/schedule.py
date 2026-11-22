"""Weekly arming schedules.

A schedule is a list of windows. Each window has a set of days, a start and an
end time. If ``end`` is earlier than ``start`` the window runs past midnight
into the next day (e.g. ``mon 22:00 -> tue 06:00`` belongs to ``mon``).
``end: "24:00"`` means end of day; ``start == end`` (e.g. 00:00-00:00) means
the whole day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, List, Optional, Set

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DAY_ALIASES = {
    "weekdays": DAY_NAMES[:5],
    "weekends": DAY_NAMES[5:],
    "daily": DAY_NAMES,
    "all": DAY_NAMES,
}


def parse_time(value: str) -> int:
    """Parse ``HH:MM`` into minutes since midnight (0..1440)."""
    try:
        hh, mm = str(value).strip().split(":")
        h, m = int(hh), int(mm)
    except ValueError as exc:
        raise ValueError(f"invalid time {value!r}, expected HH:MM") from exc
    if not (0 <= m < 60) or not (0 <= h <= 24) or (h == 24 and m != 0):
        raise ValueError(f"invalid time {value!r}")
    return h * 60 + m


def parse_days(values: Iterable[str]) -> Set[int]:
    days: Set[int] = set()
    for raw in values:
        name = str(raw).strip().lower()
        if name in _DAY_ALIASES:
            days.update(DAY_NAMES.index(d) for d in _DAY_ALIASES[name])
        elif len(name) >= 3 and name[:3] in DAY_NAMES:
            days.add(DAY_NAMES.index(name[:3]))
        else:
            raise ValueError(f"invalid day {raw!r}")
    if not days:
        raise ValueError("schedule window needs at least one day")
    return days


@dataclass(frozen=True)
class Window:
    days: frozenset
    start: int  # minutes
    end: int  # minutes

    def contains(self, now: datetime) -> bool:
        minute = now.hour * 60 + now.minute
        today = now.weekday()
        yesterday = (now - timedelta(days=1)).weekday()
        if self.start == self.end or (self.start == 0 and self.end == 1440):
            return today in self.days  # whole day
        if self.start < self.end:
            return today in self.days and self.start <= minute < self.end
        # Overnight window
        return (today in self.days and minute >= self.start) or (
            yesterday in self.days and minute < self.end
        )


class Schedule:
    """``None`` windows means always armed."""

    def __init__(self, windows: Optional[List[Window]] = None, name: str = "always"):
        self.windows = windows
        self.name = name

    @classmethod
    def always(cls) -> "Schedule":
        return cls(None, "always")

    @classmethod
    def from_config(cls, name: str, raw: list) -> "Schedule":
        if not isinstance(raw, list) or not raw:
            raise ValueError(f"schedule {name!r} must be a non-empty list of windows")
        windows = []
        for item in raw:
            windows.append(
                Window(
                    days=frozenset(parse_days(item.get("days", ["daily"]))),
                    start=parse_time(item["start"]),
                    end=parse_time(item["end"]),
                )
            )
        return cls(windows, name)

    def is_active(self, now: datetime) -> bool:
        if self.windows is None:
            return True
        return any(w.contains(now) for w in self.windows)
