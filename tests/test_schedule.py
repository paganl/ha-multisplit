"""A room's week: what it asks for when, and what it may not contain."""

from datetime import datetime, timedelta

import pytest

from core.schedule import (
    Guardrails,
    Mark,
    ScheduleError,
    WeekSchedule,
    check,
    may_edit,
    problems,
    target_problem,
)

RAILS = Guardrails(min_target=16.0, max_target=22.0, min_block=timedelta(minutes=30))

# Monday 26 October 2026 is a Monday.
MON = datetime(2026, 10, 26)


def at(day: int, hh: int, mm: int = 0) -> datetime:
    return MON + timedelta(days=day, hours=hh, minutes=mm)


def week(**days) -> WeekSchedule:
    return WeekSchedule.from_dict(
        {d: [{"at": t, "temperature": v} for t, v in marks] for d, marks in days.items()}
    )


WORKDAY = [("06:30", 20.0), ("08:30", 17.0), ("17:00", 20.5), ("22:30", 17.0)]


# -- what it asks for --------------------------------------------------------


def test_an_empty_schedule_asks_for_nothing():
    schedule = WeekSchedule()
    assert schedule.empty
    assert schedule.target_at(at(0, 12)) is None
    assert schedule.next_change(at(0, 12)) is None


def test_a_mark_holds_until_the_next_one():
    schedule = week(mon=WORKDAY)
    assert schedule.target_at(at(0, 6, 30)) == 20.0
    assert schedule.target_at(at(0, 8, 29)) == 20.0
    assert schedule.target_at(at(0, 8, 30)) == 17.0
    assert schedule.target_at(at(0, 23)) == 17.0


def test_the_last_mark_carries_over_midnight_and_round_the_week():
    schedule = week(mon=[("07:00", 20.0)], sun=[("22:00", 16.5)])
    assert schedule.target_at(at(2, 3)) == 20.0, "Monday's mark holds through Wednesday"
    assert schedule.target_at(at(0, 5)) == 16.5, "Sunday night carries into Monday morning"


def test_one_mark_is_that_temperature_all_week():
    schedule = week(wed=[("12:00", 19.0)])
    for day in range(7):
        assert schedule.target_at(at(day, 3)) == 19.0


def test_the_next_change_is_the_next_mark_wrapping_the_week():
    schedule = week(mon=WORKDAY)
    assert schedule.next_change(at(0, 7)) == at(0, 8, 30)
    assert schedule.next_change(at(0, 8, 30)) == at(0, 17), "strictly after now"
    assert schedule.next_change(at(3, 12)) == at(7, 6, 30), "next Monday"


def test_storage_round_trips():
    schedule = week(mon=WORKDAY, sat=[("09:00", 21.0)])
    assert WeekSchedule.from_dict(schedule.as_dict()) == schedule


def test_replacing_days_keeps_the_others():
    schedule = week(mon=WORKDAY, sat=[("09:00", 21.0)])
    copied = schedule.with_days(["tue", "wed"], schedule.days[0])
    assert copied.days[1] == copied.days[2] == copied.days[0]
    assert copied.days[5] == schedule.days[5]


# -- parsing -----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,needle",
    [
        ({"mon": [{"at": "7am", "temperature": 20}]}, "07:30"),
        ({"mon": [{"at": "25:00", "temperature": 20}]}, "between 00:00 and 23:59"),
        ({"mon": [{"at": "07:00", "temperature": "warm"}]}, "must be a number"),
        ({"mon": [{"at": "07:00", "temperature": 20}, {"at": "07:00", "temperature": 21}]}, "same time"),
        ({"monday": []}, "unknown day"),
    ],
)
def test_a_malformed_schedule_says_what_is_wrong(raw, needle):
    with pytest.raises(ScheduleError) as caught:
        WeekSchedule.from_dict(raw)
    assert needle in str(caught.value)


# -- guardrails --------------------------------------------------------------


def test_a_good_schedule_has_no_problems():
    assert problems(week(mon=WORKDAY, tue=WORKDAY), RAILS) == []


def test_a_mark_outside_the_rooms_range_is_refused_with_the_limit():
    found = problems(week(mon=[("07:00", 23.5), ("22:00", 15.0)]), RAILS)
    assert "Monday 07:00: 23.5° is above this room's maximum of 22°" in found
    assert "Monday 22:00: 15° is below this room's minimum of 16°" in found


def test_a_setting_shorter_than_the_minimum_block_is_refused():
    found = problems(week(mon=[("07:00", 20.0), ("07:15", 18.0), ("22:00", 17.0)]), RAILS)
    assert found == ["Monday 07:00: this setting lasts 15 min, shorter than the 30 min minimum"]


def test_the_block_rule_wraps_round_the_week():
    """Sunday 23:50 to Monday 00:10 is twenty minutes, even across the join."""
    found = problems(week(mon=[("00:10", 20.0)], sun=[("23:50", 17.0)]), RAILS)
    assert found == ["Sunday 23:50: this setting lasts 20 min, shorter than the 30 min minimum"]


def test_a_single_mark_cannot_be_too_short():
    assert problems(week(mon=[("07:00", 20.0)]), RAILS) == []


def test_check_raises_with_every_problem():
    with pytest.raises(ScheduleError) as caught:
        check(week(mon=[("07:00", 25.0), ("07:10", 14.0)]), RAILS)
    assert len(caught.value.problems) == 3


def test_an_override_is_held_to_the_same_range():
    assert target_problem(21.0, RAILS) is None
    assert "maximum of 22°" in target_problem(23.0, RAILS)


# -- who may edit -------------------------------------------------------------


@pytest.mark.parametrize(
    "owner,user_id,user_name,is_admin,allowed",
    [
        ("Erin", "u1", "Erin", False, True),  # the owner, by name
        ("erin", "u1", "Erin", False, True),  # names are not case-sensitive
        ("u1", "u1", "Someone", False, True),  # the owner, by user id
        ("Erin", "u2", "Ischa", False, False),  # someone else's room
        ("Erin", "u3", "Paul", True, True),  # admins edit every room
        (None, "u2", "Ischa", False, False),  # no owner: admins only
        (None, None, None, False, True),  # Home Assistant itself
    ],
)
def test_owners_and_admins_may_edit(owner, user_id, user_name, is_admin, allowed):
    assert may_edit(owner, user_id=user_id, user_name=user_name, is_admin=is_admin) is allowed


def test_marks_sort_themselves():
    schedule = WeekSchedule().with_days(["mon"], [Mark(600, 18.0), Mark(420, 20.0)])
    assert [m.minute for m in schedule.days[0]] == [420, 600]


# -- which target wins ----------------------------------------------------------

from core.schedule import choose_target  # noqa: E402


def choose(**kw):
    base = dict(
        override=None,
        house_away=False,
        room_away=False,
        schedule=week(mon=WORKDAY),
        local=at(0, 7),
        default=20.0,
        away=16.0,
    )
    base.update(kw)
    return choose_target(**base)


def test_the_schedule_beats_the_default():
    assert choose() == (20.0, "schedule")
    assert choose(local=at(0, 9)) == (17.0, "schedule")


def test_with_no_schedule_the_default_applies():
    assert choose(schedule=WeekSchedule()) == (20.0, "default")


def test_away_beats_the_schedule():
    assert choose(house_away=True) == (16.0, "away")
    assert choose(room_away=True) == (16.0, "away (room)")


def test_an_override_beats_away_so_someone_home_early_is_warm():
    assert choose(override=21.0, house_away=True) == (21.0, "override")


def test_marks_in_storage_form_round_trip_through_the_service_shape():
    """The card and the service send {"at": "07:00", "temperature": 20}."""
    raw = {"mon": [{"at": "07:00", "temperature": 20}], "sun": []}
    assert WeekSchedule.from_dict(raw).as_dict()["mon"] == [{"at": "07:00", "temperature": 20.0}]
