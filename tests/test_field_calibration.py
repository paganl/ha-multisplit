"""The measured numbers, asserted as behaviour.

Everything here is read from the shipped `multisplit.yaml` -- nothing is
restated in Python. If a value changes in the questionnaire, these tests
follow it, which is the point: they assert that the configuration in the
repository reproduces the behaviour found to work in the building, not that
some constant still equals itself.

HARVEST.md §C is where the numbers came from.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from core.config import load
from core.model import (
    COOL,
    HEAT,
    GroupState,
    RoomConfig,
    RoomIntent,
    RoomMeasurement,
    RoomRuntime,
)
from core.planner import plan
from core.policy import constraints_for, participation_problems

NOW = datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parent.parent

CONFIG = load(yaml.safe_load((ROOT / "multisplit.yaml").read_text(encoding="utf-8")))
LIVING_ROOM = next(r for r in CONFIG.rooms if r.key == "living_room")
DUTY = LIVING_ROOM.participation


def limits(group="a", outdoor=7.0):
    return constraints_for(CONFIG.group(group).policy, outdoor=outdoor)


def house():
    return [RoomConfig(r.key, r.group, r.participation) for r in CONFIG.rooms]


def run(temps, targets, *, outdoor, runtime=None, group_mode=None):
    rooms = house()
    groups = {c.group for c in rooms}
    return plan(
        rooms=rooms,
        measurements={n: RoomMeasurement(n, v, source="test") for n, v in temps.items()},
        intents={n: RoomIntent(n, t) for n, t in targets.items()},
        constraints={g: limits(g, outdoor) for g in groups},
        runtime=runtime or {},
        group_state={
            g: GroupState(g, group_mode, NOW - timedelta(hours=3)) for g in groups
        },
        outdoor=outdoor,
        now=NOW,
    )


def everyone_at(temperature, **overrides):
    temps = {r.key: temperature for r in CONFIG.rooms}
    temps.update(overrides)
    return temps


# --------------------------------------------------------------------------
# The layout, as configured
# --------------------------------------------------------------------------


def test_the_configuration_describes_the_real_house():
    assert CONFIG.outdoor_sensor == "sensor.openweathermap_temperature"
    assert [r.key for r in CONFIG.rooms_in("a")] == ["living_room", "ischa"]
    assert [r.key for r in CONFIG.rooms_in("b")] == ["bedroom", "erin", "office"]


def test_only_the_living_room_has_a_dedicated_sensor():
    """The other four fall back to the indoor unit's own reading.

    Recorded as a test because it is a live risk, not a settled fact: a return
    air sensor reads warm while heating, so those four rooms' thresholds
    inherit that bias. When a room sensor is added, this test should change.
    """
    with_sensors = [r.key for r in CONFIG.rooms if r.temperature_sensors]
    assert with_sensors == ["living_room"]


# --------------------------------------------------------------------------
# The numbers themselves
# --------------------------------------------------------------------------


def test_the_cut_off_does_not_vary_with_outdoor_temperature():
    """Measured as a constant 1.5°. It was the rest period that varied."""
    assert {DUTY.cut_off(t) for t in (-5.0, 0.0, 7.0, 13.0, 20.0)} == {1.5}


@pytest.mark.parametrize("outdoor,minutes", [(-5.0, 5), (0.0, 5), (7.0, 15), (13.0, 25), (20.0, 25)])
def test_the_rest_period_matches_the_harvested_table(outdoor, minutes):
    assert DUTY.rest(outdoor) == timedelta(minutes=minutes)


def test_the_rest_period_lengthens_as_it_gets_milder():
    """The physics behind the measurement: an overshot room coasts longer."""
    assert DUTY.rest(0.0) < DUTY.rest(7.0) < DUTY.rest(13.0)


def test_every_room_shares_the_measured_duty_cycle():
    """No per-room override yet — HARVEST §C3 was left blank."""
    assert {r.participation for r in CONFIG.rooms} == {DUTY}


# --------------------------------------------------------------------------
# The invariant the real numbers expose
# --------------------------------------------------------------------------


def test_the_shipped_configuration_is_self_consistent():
    for room in CONFIG.rooms:
        assert participation_problems(room.participation, limits(room.group)) == []


def test_a_narrower_tolerance_would_have_fought_the_duty_cycle():
    """With a 1.5° cut-off, a tolerance below it is self-defeating.

    The room is parked at +1.5° by the duty cycle, and at +1.1° it starts
    asking for cooling. Both controllers correct, permanently opposed.
    """
    narrowed = replace(CONFIG.group("a").policy, deadband=0.3, overshoot_tolerance=0.8)
    problems = participation_problems(DUTY, constraints_for(narrowed, outdoor=7.0))
    assert problems and "raise the tolerance above 1.5" in problems[0]


def test_a_room_parked_at_its_cut_off_does_not_ask_for_cooling():
    """The same thing, asserted as behaviour rather than as validation."""
    result = run(
        everyone_at(20.0, living_room=22.5),
        {**{r.key: 20.0 for r in CONFIG.rooms}, "living_room": 21.0},
        outdoor=7.0,
        group_mode=HEAT,
    )
    assert result.room("living_room").wants is None
    assert result.group("a").mode is None


# --------------------------------------------------------------------------
# The duty cycle, with the real numbers
# --------------------------------------------------------------------------


def test_a_heating_room_runs_until_one_and_a_half_degrees_over():
    running = {
        "living_room": RoomRuntime(
            "living_room", running=True, mode=HEAT, since=NOW - timedelta(minutes=30)
        )
    }
    targets = {**{r.key: 20.0 for r in CONFIG.rooms}, "living_room": 21.0}

    short = run(everyone_at(20.0, living_room=22.4), targets, outdoor=7.0, runtime=running, group_mode=HEAT)
    assert short.room("living_room").mode == HEAT

    past = run(everyone_at(20.0, living_room=22.6), targets, outdoor=7.0, runtime=running, group_mode=HEAT)
    assert past.room("living_room").standing_by
    assert "past the cut-off of 22.5" in past.room("living_room").because


def test_the_rest_is_five_minutes_in_the_cold_and_twenty_five_in_the_shoulder():
    rested_ten = {
        "living_room": RoomRuntime("living_room", running=False, since=NOW - timedelta(minutes=10))
    }
    temps = everyone_at(20.0)
    targets = {**{r.key: 20.0 for r in CONFIG.rooms}, "living_room": 21.0}

    cold = run(temps, targets, outdoor=0.0, runtime=rested_ten)
    assert cold.room("living_room").mode == HEAT, "the 5 minute rest is over"

    shoulder = run(temps, targets, outdoor=13.0, runtime=rested_ten)
    assert shoulder.room("living_room").standing_by, "the 25 minute rest is not"
    assert "resting" in shoulder.room("living_room").because


def test_a_room_resumes_half_a_degree_below_target():
    long_rested = {
        "living_room": RoomRuntime("living_room", running=False, since=NOW - timedelta(hours=2))
    }
    targets = {**{r.key: 20.0 for r in CONFIG.rooms}, "living_room": 21.0}

    not_yet = run(everyone_at(20.0, living_room=20.6), targets, outdoor=7.0, runtime=long_rested)
    assert not_yet.room("living_room").standing_by
    assert "within 0.5°" in not_yet.room("living_room").because

    ready = run(everyone_at(20.0, living_room=20.4), targets, outdoor=7.0, runtime=long_rested)
    assert ready.room("living_room").mode == HEAT


# --------------------------------------------------------------------------
# The real layout under contention
# --------------------------------------------------------------------------


def test_the_two_groups_decide_independently():
    result = run(
        {"living_room": 24.0, "ischa": 23.5, "bedroom": 18.0, "erin": 18.5, "office": 19.0},
        {r.key: (21.0 if r.group == "a" else 20.0) for r in CONFIG.rooms},
        outdoor=23.0,
    )
    assert result.group("a").mode == COOL
    assert result.group("b").mode == HEAT


def test_group_b_contention_names_the_losing_room():
    """Three rooms on one outdoor unit is where arbitration earns its keep."""
    result = run(
        everyone_at(20.0, bedroom=23.0, erin=18.0, office=18.5),
        {r.key: 20.0 for r in CONFIG.rooms},
        outdoor=10.0,
    )
    decision = result.group("b")
    assert decision.mode == HEAT, "two cold rooms outweigh one warm one"
    assert decision.unserved == ("bedroom",)
    assert "wants cool" in result.room("bedroom").because


def test_the_outdoor_lockouts_from_the_configuration_apply():
    """20 °C and 24 °C, from the questionnaire, not from constants in here."""
    policy = CONFIG.group("a").policy
    assert (policy.cool_below, policy.heat_above) == (20.0, 24.0)

    too_cold_to_cool = run(
        everyone_at(20.0, living_room=24.0),
        {r.key: 20.0 for r in CONFIG.rooms},
        outdoor=18.0,
    )
    assert too_cold_to_cool.group("a").mode is None
    assert "locked out" in too_cold_to_cool.group("a").because
    assert "locked out" in too_cold_to_cool.room("living_room").because

    too_warm_to_heat = run(
        everyone_at(20.0, living_room=17.0),
        {r.key: 20.0 for r in CONFIG.rooms},
        outdoor=26.0,
    )
    assert too_warm_to_heat.group("a").mode is None
    assert "locked out" in too_warm_to_heat.room("living_room").because


def test_between_the_lockouts_both_modes_are_available():
    """20–24 °C outdoor is the band where arbitration, not weather, decides."""
    both = constraints_for(CONFIG.group("a").policy, outdoor=22.0)
    assert both.allowed_modes == frozenset({HEAT, COOL})


def test_the_away_setback_is_reachable():
    """A target outside the setpoint range is clamped and never takes effect."""
    low, high = CONFIG.group("a").policy.setpoint_range
    assert low <= CONFIG.intent.away_target <= high
    assert low <= CONFIG.intent.default_target <= high
    assert CONFIG.intent.away_target < CONFIG.intent.default_target
