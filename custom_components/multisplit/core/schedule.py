"""A room's week, as its owner set it.

A schedule is seven days of *marks*. A mark says "from this time, aim for
this temperature", and holds until the next mark -- across midnight, and
across the end of the week back to Monday. So a single mark at 07:00 on
Monday means that temperature all week; an empty schedule means the room
has no schedule and follows the default.

Pure, like the planner: no Home Assistant, no clock. The integration hands
in local wall-clock time, so a mark at 07:00 is 07:00 in summer and winter.

Guardrails are checked here, once, for every route a schedule can arrive by
-- the card, a service call, an automation -- so none of them can be talked
round. They are:

* **the room's range.** Every mark must sit inside the room's allowed
  minimum and maximum, which are configuration, not the owner's to change.
* **a minimum block.** No setting may last less than the configured
  minimum, counted from one mark to the next, wrapping the week. A schedule
  that changes target every few minutes is a schedule that short-cycles a
  compressor shared with other rooms.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

DAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_NAMES = {
    "mon": "Monday",
    "tue": "Tuesday",
    "wed": "Wednesday",
    "thu": "Thursday",
    "fri": "Friday",
    "sat": "Saturday",
    "sun": "Sunday",
}
_DAY_MINUTES = 24 * 60
_WEEK_MINUTES = 7 * _DAY_MINUTES


class ScheduleError(ValueError):
    """Raised with every problem found, phrased for the person editing."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True)
class Mark:
    """From ``minute`` past midnight, aim for ``target``."""

    minute: int
    target: float

    @property
    def time(self) -> str:
        return f"{self.minute // 60:02d}:{self.minute % 60:02d}"


@dataclass(frozen=True)
class Guardrails:
    """What a room's schedule may contain. Set in configuration."""

    min_target: float
    max_target: float
    min_block: timedelta


@dataclass(frozen=True)
class WeekSchedule:
    """Seven days of marks, Monday first, each day sorted by time."""

    days: tuple[tuple[Mark, ...], ...] = ((),) * 7

    # -- reading ---------------------------------------------------------

    @property
    def empty(self) -> bool:
        return not any(self.days)

    def _week(self) -> list[tuple[int, Mark]]:
        """Every mark as (minute of the week, mark), in order."""
        return [
            (index * _DAY_MINUTES + mark.minute, mark)
            for index, day in enumerate(self.days)
            for mark in day
        ]

    def target_at(self, local: datetime) -> float | None:
        """What the schedule asks for at this wall-clock time, or None if empty.

        Before the first mark of the week, the last mark of the previous week
        still holds: Sunday night's setting carries into Monday morning.
        """
        week = self._week()
        if not week:
            return None
        now = _week_minute(local)
        current = week[-1][1]
        for at, mark in week:
            if at > now:
                break
            current = mark
        return current.target

    def next_change(self, local: datetime) -> datetime | None:
        """When the next mark takes effect, strictly after ``local``.

        None for an empty schedule. A schedule with one mark still has a
        next change: the same mark, a week on.
        """
        week = self._week()
        if not week:
            return None
        now = _week_minute(local)
        ahead = next(((at - now) for at, _ in week if at > now), None)
        if ahead is None:
            ahead = week[0][0] + _WEEK_MINUTES - now
        start = local.replace(second=0, microsecond=0)
        return start + timedelta(minutes=ahead)

    # -- converting ------------------------------------------------------

    def as_dict(self) -> dict[str, list[dict]]:
        """The storage and attribute form: {"mon": [{"at": "07:00", "temperature": 20.0}]}."""
        return {
            day: [{"at": mark.time, "temperature": mark.target} for mark in marks]
            for day, marks in zip(DAYS, self.days)
        }

    @classmethod
    def from_dict(cls, raw: Mapping | None) -> "WeekSchedule":
        """Parse the storage form. Raises ScheduleError, listing every problem."""
        raw = raw or {}
        problems: list[str] = []
        unknown = sorted(set(raw) - set(DAYS))
        if unknown:
            problems.append(f"unknown day {', '.join(unknown)}; days are {', '.join(DAYS)}")
        days = []
        for day in DAYS:
            marks, found = parse_marks(raw.get(day) or [], _DAY_NAMES[day])
            problems.extend(found)
            days.append(marks)
        if problems:
            raise ScheduleError(problems)
        return cls(tuple(days))

    def with_days(self, days: Sequence[str], marks: Sequence[Mark]) -> "WeekSchedule":
        """A copy with each of ``days`` replaced by ``marks``."""
        bad = [d for d in days if d not in DAYS]
        if bad:
            raise ScheduleError([f"unknown day {', '.join(bad)}; days are {', '.join(DAYS)}"])
        ordered = tuple(sorted(marks, key=lambda m: m.minute))
        return WeekSchedule(
            tuple(ordered if name in days else current for name, current in zip(DAYS, self.days))
        )


def _week_minute(local: datetime) -> int:
    return local.weekday() * _DAY_MINUTES + local.hour * 60 + local.minute


def parse_time(value: object) -> int:
    """'07:30' -> 450. Raises ValueError with a readable message."""
    if not isinstance(value, str) or value.count(":") != 1:
        raise ValueError(f"time must look like 07:30, found {value!r}")
    hours, minutes = value.split(":")
    if not (hours.isdigit() and minutes.isdigit()):
        raise ValueError(f"time must look like 07:30, found {value!r}")
    h, m = int(hours), int(minutes)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"time must be between 00:00 and 23:59, found {value!r}")
    return h * 60 + m


def parse_marks(raw: object, where: str) -> tuple[tuple[Mark, ...], list[str]]:
    """Marks from [{"at": "07:00", "temperature": 20}], with every problem found."""
    problems: list[str] = []
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        return (), [f"{where}: expected a list of marks, found {raw!r}"]
    marks: list[Mark] = []
    seen: set[int] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            problems.append(f"{where}: expected {{at, temperature}}, found {item!r}")
            continue
        try:
            minute = parse_time(item.get("at"))
        except ValueError as error:
            problems.append(f"{where}: {error}")
            continue
        value = item.get("temperature")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(f"{where} {item.get('at')}: temperature must be a number, found {value!r}")
            continue
        if minute in seen:
            problems.append(f"{where} {item.get('at')}: two marks at the same time")
            continue
        seen.add(minute)
        marks.append(Mark(minute, float(value)))
    return tuple(sorted(marks, key=lambda m: m.minute)), problems


def problems(schedule: WeekSchedule, rails: Guardrails) -> list[str]:
    """Everything wrong with a schedule for this room, phrased to be fixed.

    Empty for a schedule that may be saved. The card runs the same two rules
    for live feedback, but this is the one that decides.
    """
    found: list[str] = []
    for day, marks in zip(DAYS, schedule.days):
        for mark in marks:
            where = f"{_DAY_NAMES[day]} {mark.time}"
            if mark.target < rails.min_target:
                found.append(
                    f"{where}: {mark.target:g}° is below this room's minimum of "
                    f"{rails.min_target:g}°"
                )
            elif mark.target > rails.max_target:
                found.append(
                    f"{where}: {mark.target:g}° is above this room's maximum of "
                    f"{rails.max_target:g}°"
                )

    week = schedule._week()
    shortest = int(rails.min_block.total_seconds() // 60)
    if len(week) > 1 and shortest > 0:
        for (at, mark), (following, _) in zip(week, week[1:] + [(week[0][0] + _WEEK_MINUTES, None)]):
            length = following - at
            if length < shortest:
                day = DAYS[at // _DAY_MINUTES]
                found.append(
                    f"{_DAY_NAMES[day]} {mark.time}: this setting lasts {length} min, "
                    f"shorter than the {shortest} min minimum"
                )
    return found


def check(schedule: WeekSchedule, rails: Guardrails) -> None:
    """Raise ScheduleError if the schedule breaks a guardrail."""
    found = problems(schedule, rails)
    if found:
        raise ScheduleError(found)


def target_problem(target: float, rails: Guardrails) -> str | None:
    """The range check alone, for a one-off override from a thermostat."""
    if target < rails.min_target:
        return f"{target:g}° is below this room's minimum of {rails.min_target:g}°"
    if target > rails.max_target:
        return f"{target:g}° is above this room's maximum of {rails.max_target:g}°"
    return None


def may_edit(
    owner: str | None, *, user_id: str | None, user_name: str | None, is_admin: bool
) -> bool:
    """Whether this person may change this room's schedule.

    Admins may edit any room. A room's owner -- named in configuration by
    Home Assistant user name or user id -- may edit their own. A room with
    no owner is admins-only. A call with no user at all comes from Home
    Assistant itself (an automation or script an admin wrote) and is allowed.
    """
    if user_id is None:
        return True
    if is_admin:
        return True
    if owner is None:
        return False
    wanted = owner.strip().casefold()
    return wanted in {user_id.casefold(), (user_name or "").strip().casefold()}


def choose_target(
    *,
    override: float | None,
    house_away: bool,
    room_away: bool,
    schedule: WeekSchedule,
    local: datetime,
    default: float,
    away: float,
) -> tuple[float, str]:
    """A room's target and where it came from, most specific first.

    1. an override -- someone turned this room's thermostat. It beats away,
       so a person home early is not left in a cold room;
    2. away -- the house-wide switch, or the room's own presence entity;
    3. the room's weekly schedule;
    4. the shared default.

    The source travels to the UI, because "override until 21:30" and
    "schedule" look the same on a thermostat, and that ambiguity is most of
    what makes a heating system feel haunted.
    """
    if override is not None:
        return override, "override"
    if house_away:
        return away, "away"
    if room_away:
        return away, "away (room)"
    scheduled = schedule.target_at(local)
    if scheduled is not None:
        return scheduled, "schedule"
    return default, "default"
