"""An open window or door stands its room aside.

The room's unit already switches itself off on the ESP32 when its lockout
sensor has been open for a couple of minutes. Before this, the planner knew
nothing of it: the cooling room kept voting for heat, was counted as unserved,
and its unit was re-commanded every write interval only to be refused.
"""

from datetime import datetime, timedelta, timezone

from core.model import (
    HEAT,
    Constraints,
    Curve,
    FrostProtection,
    GroupState,
    ParticipationPolicy,
    RoomConfig,
    RoomIntent,
    RoomMeasurement,
    RoomRuntime,
)
from core.planner import plan

NOW = datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)
GRACE = timedelta(minutes=2)
DUTY = ParticipationPolicy(
    cut_off_offset=Curve.constant(0.5), min_off=Curve.constant(0.0), min_on=timedelta(0)
)


def rooms(*names):
    return [RoomConfig(name, "a", DUTY, lockout_grace=GRACE) for name in names]


def run(temps, *, open_windows=(), runtime=None, standby=(), group_mode=None, frost=None):
    return plan(
        rooms=rooms(*temps),
        measurements={n: RoomMeasurement(n, t, source="test") for n, t in temps.items()},
        intents={n: RoomIntent(n, 20.0) for n in temps},
        constraints={"a": Constraints()},
        runtime=runtime or {},
        group_state={"a": GroupState("a", group_mode, NOW - timedelta(hours=1))},
        outdoor=5.0,
        now=NOW,
        frost=frost,
        standby=frozenset(standby),
        open_windows=frozenset(open_windows),
    )


def memory(result, room):
    return next(r for r in result.runtime if r.room == room)


def opened(ago, **kwargs):
    return RoomRuntime("lounge", open_since=NOW - ago, **kwargs)


def test_a_window_just_opened_changes_nothing_yet():
    result = run({"lounge": 18.0}, open_windows={"lounge"})

    command = result.room("lounge")
    assert command.mode == HEAT
    assert not command.window_open
    assert "window open — standing aside in 2 min" in command.because
    assert memory(result, "lounge").open_since == NOW


def test_the_grace_is_counted_from_when_it_was_first_seen_open():
    result = run(
        {"lounge": 18.0},
        open_windows={"lounge"},
        runtime={"lounge": opened(timedelta(seconds=90))},
    )

    assert result.room("lounge").mode == HEAT
    assert "standing aside in 30 s" in result.room("lounge").because
    assert memory(result, "lounge").open_since == NOW - timedelta(seconds=90)


def test_past_the_grace_the_room_stands_aside_and_its_unit_is_commanded_off():
    running = opened(timedelta(minutes=5), running=True, mode=HEAT, since=NOW - timedelta(hours=1))
    result = run({"lounge": 18.0}, open_windows={"lounge"}, runtime={"lounge": running})

    command = result.room("lounge")
    assert command.mode is None
    assert command.window_open
    assert command.because == "stopped — window open for 5 min; rejoins when it closes"
    after = memory(result, "lounge")
    assert not after.running and after.mode is None
    assert after.since == NOW, "stopping starts the rest period"
    assert after.open_since == NOW - timedelta(minutes=5)


def test_a_room_standing_aside_does_not_vote_or_count_as_unserved():
    # The lounge is cold because its window is open; the study is content.
    result = run(
        {"lounge": 16.0, "study": 20.2},
        open_windows={"lounge"},
        runtime={"lounge": opened(timedelta(minutes=10))},
    )

    decision = result.group("a")
    assert decision.mode is None, "an open window must not turn the group on"
    assert "lounge" not in decision.unserved
    assert "standing aside, window open: lounge" in decision.because
    assert memory(result, "lounge").unserved_since is None


def test_the_others_still_decide_the_group_and_the_reason_names_who_is_aside():
    result = run(
        {"lounge": 16.0, "study": 18.0},
        open_windows={"lounge"},
        runtime={"lounge": opened(timedelta(minutes=10))},
    )

    assert result.group("a").mode == HEAT
    assert result.room("study").mode == HEAT
    assert result.room("lounge").mode is None
    assert result.group("a").because.endswith("; standing aside, window open: lounge")


def test_every_room_aside_turns_the_group_off_and_says_why():
    result = run(
        {"lounge": 16.0},
        open_windows={"lounge"},
        runtime={"lounge": opened(timedelta(minutes=10))},
    )

    decision = result.group("a")
    assert decision.mode is None
    assert decision.because == (
        "off — no room in this group is taking part; standing aside, window open: lounge"
    )


def test_closing_the_window_rejoins_the_room_on_the_next_cycle():
    aside = run(
        {"lounge": 18.0},
        open_windows={"lounge"},
        runtime={"lounge": opened(timedelta(minutes=10))},
    )
    closed = run({"lounge": 18.0}, runtime={"lounge": memory(aside, "lounge")})

    assert closed.room("lounge").mode == HEAT
    assert not closed.room("lounge").window_open
    assert memory(closed, "lounge").open_since is None


def test_frost_protection_outranks_an_open_window():
    result = run(
        {"lounge": 6.0},
        open_windows={"lounge"},
        runtime={"lounge": opened(timedelta(minutes=30))},
        frost=FrostProtection(below=8.0, release=10.0),
    )

    command = result.room("lounge")
    assert command.mode == HEAT
    assert command.frost
    assert not command.window_open
    assert "heated anyway for frost protection" in command.because


def test_a_switched_off_room_is_left_alone_but_keeps_its_window_clock():
    off = run({"lounge": 18.0}, open_windows={"lounge"}, standby={"lounge"})

    assert all(c.room != "lounge" for c in off.rooms), "switched off means left alone"
    assert memory(off, "lounge").open_since == NOW

    later = NOW + timedelta(minutes=5)
    back_on = plan(
        rooms=rooms("lounge"),
        measurements={"lounge": RoomMeasurement("lounge", 18.0, source="test")},
        intents={"lounge": RoomIntent("lounge", 20.0)},
        constraints={"a": Constraints()},
        runtime={"lounge": memory(off, "lounge")},
        group_state={"a": GroupState("a")},
        outdoor=5.0,
        now=later,
        open_windows=frozenset({"lounge"}),
    )
    assert back_on.room("lounge").window_open, "switched back on with the window still open"


def test_a_closed_switched_off_room_still_has_no_memory_at_all():
    result = run({"lounge": 18.0}, standby={"lounge"})

    assert result.runtime == ()


def test_with_no_grace_the_room_stands_aside_on_the_first_cycle():
    """Production: the device's Window Lockout has already waited its grace."""
    result = plan(
        rooms=[RoomConfig("lounge", "a", DUTY)],
        measurements={"lounge": RoomMeasurement("lounge", 18.0, source="test")},
        intents={"lounge": RoomIntent("lounge", 20.0)},
        constraints={"a": Constraints()},
        runtime={},
        group_state={"a": GroupState("a")},
        outdoor=5.0,
        now=NOW,
        open_windows=frozenset({"lounge"}),
    )

    command = result.room("lounge")
    assert command.window_open and command.mode is None
    assert "standing aside in" not in command.because


def test_when_frost_protection_releases_with_the_window_still_open_the_room_goes_aside():
    protected = opened(timedelta(minutes=30), running=True, mode=HEAT, frost=True,
                       since=NOW - timedelta(minutes=20))
    result = run(
        {"lounge": 10.5},  # above the release threshold
        open_windows={"lounge"},
        runtime={"lounge": protected},
        frost=FrostProtection(below=8.0, release=10.0),
    )

    command = result.room("lounge")
    assert command.window_open and command.mode is None
    after = memory(result, "lounge")
    assert not after.frost and not after.running


def test_a_stale_reading_does_not_keep_a_room_in_while_its_window_is_open():
    stale_running = opened(
        timedelta(minutes=5), running=True, mode=HEAT, since=NOW - timedelta(hours=1),
        untrusted_since=NOW - timedelta(minutes=1),
    )
    result = plan(
        rooms=rooms("lounge"),
        measurements={"lounge": RoomMeasurement("lounge", 18.0, stale=True, source="test")},
        intents={"lounge": RoomIntent("lounge", 20.0)},
        constraints={"a": Constraints()},
        runtime={"lounge": stale_running},
        group_state={"a": GroupState("a", HEAT, NOW - timedelta(hours=1))},
        outdoor=5.0,
        now=NOW,
        open_windows=frozenset({"lounge"}),
    )

    assert result.room("lounge").window_open
    assert result.room("lounge").mode is None, "the stale-sensor grace must not hold it on"
