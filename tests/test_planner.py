"""Scenarios the planner must get right.

Every one of these is painful, slow or seasonal to reproduce in a real
building: both rooms opposed, a room starved for hours, a sensor that has gone
quiet, a room overshooting in mild weather. They run here in microseconds
because the planner is a pure function.
"""

from datetime import datetime, timedelta, timezone

from core.model import (
    COOL,
    HEAT,
    Constraints,
    Curve,
    GroupState,
    ParticipationPolicy,
    RoomConfig,
    RoomIntent,
    RoomMeasurement,
    RoomRuntime,
)
from core.planner import plan
from core.policy import PolicyConfig, constraints_for

NOW = datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)

# Placeholder curves. These stand in until HARVEST.md §C2 supplies the
# measured values; the shape is the claim, the numbers are not.
CUT_OFF = Curve(((0.0, 0.3), (7.0, 0.0), (14.0, -0.4)))
REST = Curve(((0.0, 600.0), (7.0, 1200.0), (14.0, 2100.0)))


def participation(min_on=timedelta(minutes=5), resume=None):
    return ParticipationPolicy(
        cut_off_offset=CUT_OFF, min_off=REST, min_on=min_on, resume_deadband=resume
    )


def configs(*names, group="a", **kwargs):
    return [RoomConfig(name, group, participation(**kwargs)) for name in names]


def run(
    room_configs,
    temps,
    targets,
    *,
    outdoor=5.0,
    group_mode=None,
    group_since=None,
    runtime=None,
    constraints=None,
    priorities=None,
    stale=(),
    now=NOW,
):
    measurements = {
        name: RoomMeasurement(name, value, stale=name in stale, source="test")
        for name, value in temps.items()
    }
    intents = {
        name: RoomIntent(name, target, priority=(priorities or {}).get(name, 1.0))
        for name, target in targets.items()
    }
    groups = {config.group for config in room_configs}
    return plan(
        rooms=room_configs,
        measurements=measurements,
        intents=intents,
        constraints={g: (constraints or Constraints()) for g in groups},
        runtime=runtime or {},
        group_state={g: GroupState(g, group_mode, group_since) for g in groups},
        outdoor=outdoor,
        now=now,
    )


# --------------------------------------------------------------------------
# Group mode
# --------------------------------------------------------------------------


def test_a_single_cold_room_gets_heat():
    result = run(configs("office"), {"office": 18.0}, {"office": 21.0})
    assert result.group("a").mode == HEAT
    assert result.room("office").mode == HEAT
    assert "office" in result.group("a").because


def test_group_is_off_when_every_room_is_satisfied():
    result = run(configs("office", "lounge"), {"office": 21.0, "lounge": 20.0}, {"office": 21.0, "lounge": 20.0})
    assert result.group("a").mode is None
    assert "deadband" in result.group("a").because
    assert all(command.standing_by for command in result.rooms)


def test_a_stale_sensor_gets_no_vote_and_says_why():
    """Guessing here produces a confident wrong decision for the whole group."""
    result = run(
        configs("office", "lounge"),
        {"office": 15.0, "lounge": 20.0},
        {"office": 21.0, "lounge": 20.0},
        stale={"office"},
    )
    assert result.group("a").mode is None
    office = result.room("office")
    assert office.standing_by and office.wants is None
    assert "stale" in office.because and "not guessing" in office.because


def test_opposed_rooms_the_stronger_demand_wins_and_the_loser_is_named():
    result = run(
        configs("office", "lounge"),
        {"office": 18.0, "lounge": 22.0},
        {"office": 21.0, "lounge": 20.0},
    )
    decision = result.group("a")
    assert decision.mode == HEAT
    assert decision.unserved == ("lounge",)
    assert "outweighs cool" in decision.because

    lounge = result.room("lounge")
    assert lounge.standing_by and lounge.wants == COOL
    assert "wants cool" in lounge.because and "heating" in lounge.because


def test_priority_can_outweigh_a_larger_error():
    """The occupied room wins the shoulder-season argument."""
    result = run(
        configs("office", "lounge"),
        {"office": 20.0, "lounge": 22.0},
        {"office": 21.0, "lounge": 20.0},
        priorities={"office": 3.0},
    )
    assert result.group("a").mode == HEAT


# --------------------------------------------------------------------------
# Changeover inertia
# --------------------------------------------------------------------------


def test_inertia_resists_a_marginal_flip():
    """Heat demand of 1.5 loses to cool demand of 0.4 plus a fresh bonus."""
    result = run(
        configs("office", "lounge"),
        {"office": 19.5, "lounge": 20.4},
        {"office": 21.0, "lounge": 20.0},
        group_mode=COOL,
        group_since=NOW,
    )
    assert result.group("a").mode == COOL
    assert "inertia" in result.group("a").because


def test_inertia_decays_so_sustained_demand_eventually_wins():
    """The identical opposition, two hours later: no longer protected."""
    temps = {"office": 19.5, "lounge": 20.4}
    targets = {"office": 21.0, "lounge": 20.0}

    fresh = run(configs("office", "lounge"), temps, targets, group_mode=COOL, group_since=NOW)
    assert fresh.group("a").mode == COOL

    aged = run(
        configs("office", "lounge"), temps, targets,
        group_mode=COOL, group_since=NOW - timedelta(hours=2),
    )
    assert aged.group("a").mode == HEAT


def test_inertia_does_not_hold_a_mode_nobody_wants():
    """A group idling in cool has no reason to resist heating."""
    result = run(
        configs("office", "lounge"),
        {"office": 19.5, "lounge": 20.0},
        {"office": 21.0, "lounge": 20.0},
        group_mode=COOL,
        group_since=NOW,
    )
    assert result.group("a").mode == HEAT


# --------------------------------------------------------------------------
# Starvation and aging
# --------------------------------------------------------------------------


def test_aging_eventually_flips_a_starved_room():
    temps = {"office": 19.5, "lounge": 22.0}
    targets = {"office": 21.0, "lounge": 20.0}
    settled = GroupState("a", COOL, NOW - timedelta(hours=4))

    fresh = run(
        configs("office", "lounge"), temps, targets,
        group_mode=settled.mode, group_since=settled.since,
    )
    assert fresh.group("a").mode == COOL

    starved = run(
        configs("office", "lounge"), temps, targets,
        group_mode=settled.mode, group_since=settled.since,
        runtime={"office": RoomRuntime("office", unserved_since=NOW - timedelta(hours=3))},
    )
    assert starved.group("a").mode == HEAT, "a genuinely starved room must win eventually"


def test_the_unserved_clock_starts_and_clears_itself():
    temps = {"office": 19.5, "lounge": 22.0}
    targets = {"office": 21.0, "lounge": 20.0}

    starving = run(configs("office", "lounge"), temps, targets, group_mode=COOL, group_since=NOW)
    office = next(r for r in starving.runtime if r.room == "office")
    assert office.unserved_since == NOW

    # Now the group serves heat: the clock must clear, not keep accumulating.
    served = run(
        configs("office", "lounge"),
        {"office": 20.0, "lounge": 20.0},
        targets,
        runtime={"office": RoomRuntime("office", unserved_since=NOW - timedelta(hours=1))},
    )
    assert served.group("a").mode == HEAT
    office = next(r for r in served.runtime if r.room == "office")
    assert office.unserved_since is None


def test_a_resting_room_is_not_counted_as_starved():
    """Aging is for mode contention, not for a room's own duty cycle."""
    result = run(
        configs("office"),
        {"office": 20.0},
        {"office": 21.0},
        outdoor=7.0,
        runtime={"office": RoomRuntime("office", running=False, since=NOW - timedelta(minutes=2))},
    )
    assert result.group("a").mode == HEAT
    assert result.room("office").standing_by  # resting
    office = next(r for r in result.runtime if r.room == "office")
    assert office.unserved_since is None


# --------------------------------------------------------------------------
# Policy reaching the room
# --------------------------------------------------------------------------


def test_a_locked_out_room_is_told_why_not_merely_that_the_group_is_off():
    constraints = constraints_for(PolicyConfig(cool_below=22.0), outdoor=19.0)
    result = run(
        configs("lounge"),
        {"lounge": 23.0},
        {"lounge": 20.0},
        outdoor=19.0,
        constraints=constraints,
    )
    assert result.group("a").mode is None
    assert "locked out" in result.group("a").because

    lounge = result.room("lounge")
    assert lounge.standing_by and lounge.wants == COOL
    assert "wants cool" in lounge.because and "locked out" in lounge.because


# --------------------------------------------------------------------------
# Participation: the duty cycle
# --------------------------------------------------------------------------


def test_in_cold_weather_the_room_runs_slightly_past_target():
    running = {"office": RoomRuntime("office", running=True, mode=HEAT, since=NOW - timedelta(minutes=30))}

    just_short = run(configs("office"), {"office": 21.2}, {"office": 21.0}, outdoor=0.0, runtime=running)
    assert just_short.room("office").mode == HEAT

    past = run(configs("office"), {"office": 21.4}, {"office": 21.0}, outdoor=0.0, runtime=running)
    assert past.room("office").standing_by
    assert "past the cut-off" in past.room("office").because


def test_in_mild_weather_the_cut_off_is_anticipatory():
    """Minimum capacity plus coil residual will carry the room past target."""
    running = {"office": RoomRuntime("office", running=True, mode=HEAT, since=NOW - timedelta(minutes=30))}
    result = run(configs("office"), {"office": 20.7}, {"office": 21.0}, outdoor=14.0, runtime=running)

    command = result.room("office")
    assert command.standing_by, "should stop before reaching target in mild weather"
    assert "before target" in command.because


def test_minimum_run_time_prevents_a_short_cycle():
    """And the group must hold its mode while a room cannot yet stop."""
    result = run(
        configs("office"),
        {"office": 21.4},
        {"office": 21.0},
        outdoor=0.0,
        group_mode=HEAT,
        group_since=NOW - timedelta(minutes=2),
        runtime={"office": RoomRuntime("office", running=True, mode=HEAT, since=NOW - timedelta(minutes=2))},
    )
    assert result.group("a").mode == HEAT
    command = result.room("office")
    assert command.mode == HEAT
    assert "minimum run" in command.because


def test_a_rested_room_waits_before_restarting():
    result = run(
        configs("office"),
        {"office": 20.0},
        {"office": 21.0},
        outdoor=7.0,
        runtime={"office": RoomRuntime("office", running=False, since=NOW - timedelta(minutes=5))},
    )
    command = result.room("office")
    assert command.standing_by
    assert "resting" in command.because


def test_the_rest_period_is_longer_in_mild_weather():
    off_for_25 = {"office": RoomRuntime("office", running=False, since=NOW - timedelta(minutes=25))}

    cold = run(configs("office"), {"office": 20.0}, {"office": 21.0}, outdoor=7.0, runtime=off_for_25)
    assert cold.room("office").mode == HEAT

    mild = run(configs("office"), {"office": 20.0}, {"office": 21.0}, outdoor=14.0, runtime=off_for_25)
    assert mild.room("office").standing_by
    assert "resting" in mild.room("office").because


def test_unknown_outdoor_uses_the_cautious_end_of_the_curve():
    off_for_15 = {"office": RoomRuntime("office", running=False, since=NOW - timedelta(minutes=15))}

    unknown = run(configs("office"), {"office": 20.0}, {"office": 21.0}, outdoor=None, runtime=off_for_15)
    assert unknown.room("office").mode == HEAT, "10 min rest at the cold end"

    mild = run(configs("office"), {"office": 20.0}, {"office": 21.0}, outdoor=14.0, runtime=off_for_15)
    assert mild.room("office").standing_by


def test_a_running_room_stops_when_the_group_changes_mode_under_it():
    """Office is still short of its cut-off, but is outweighed four to one."""
    result = run(
        configs("office", "lounge"),
        {"office": 20.0, "lounge": 24.0},
        {"office": 21.0, "lounge": 20.0},
        outdoor=7.0,
        group_mode=HEAT,
        group_since=NOW - timedelta(hours=2),
        runtime={"office": RoomRuntime("office", running=True, mode=HEAT, since=NOW - timedelta(hours=1))},
    )
    assert result.group("a").mode == COOL
    office = result.room("office")
    assert office.standing_by
    assert "changed to cool" in office.because and "heating" in office.because


def test_a_committed_room_keeps_the_group_in_its_mode():
    """A unit short of its cut-off is still calling on the compressor."""
    result = run(
        configs("office"),
        {"office": 20.9},
        {"office": 21.0},
        outdoor=7.0,
        group_mode=HEAT,
        group_since=NOW - timedelta(minutes=30),
        runtime={"office": RoomRuntime("office", running=True, mode=HEAT, since=NOW - timedelta(minutes=30))},
    )
    assert result.room("office").wants is None, "comfort demand is satisfied"
    assert result.group("a").mode == HEAT, "but the group must not turn off under it"
    assert "finishing its cycle" in result.group("a").because


# --------------------------------------------------------------------------
# Structural guarantees
# --------------------------------------------------------------------------


def test_every_command_carries_a_complete_desired_state():
    """The 23 °C defect is unrepresentable: mode and target always travel together."""
    result = run(
        configs("office", "lounge"),
        {"office": 18.0, "lounge": 22.0},
        {"office": 21.0, "lounge": 20.0},
    )
    for command in result.rooms:
        assert isinstance(command.target, float)
        assert command.because


def test_setpoints_are_clamped_to_the_permitted_range():
    result = run(
        configs("office"),
        {"office": 18.0},
        {"office": 30.0},
        constraints=Constraints(setpoint_range=(15.0, 26.0)),
    )
    assert result.room("office").target == 26.0


def test_the_same_inputs_always_produce_the_same_plan():
    args = (configs("office", "lounge"), {"office": 18.0, "lounge": 22.0}, {"office": 21.0, "lounge": 20.0})
    assert run(*args) == run(*args)


def test_rooms_are_planned_per_group_not_globally():
    result = run(
        configs("office", group="a") + configs("lounge", group="b"),
        {"office": 18.0, "lounge": 23.0},
        {"office": 21.0, "lounge": 20.0},
        outdoor=25.0,
    )
    assert result.group("a").mode == HEAT
    assert result.group("b").mode == COOL


# --------------------------------------------------------------------------
# Regression: the problem the thresholds were added to solve
# --------------------------------------------------------------------------


def test_overshoot_regression_from_the_field():
    """The failure that justified the participation controller in the first place.

    A room near its target, in mild weather, on a compressor driven hard by
    another room. Without a cut-off it overshoots; with one it must stop
    before target.

    This asserts the *mechanism*, with placeholder curves. Replace the numbers
    with the measured ones from HARVEST.md §C4 and it becomes the test that
    proves the rewrite has not regressed to the old behaviour.
    """
    running = {"office": RoomRuntime("office", running=True, mode=HEAT, since=NOW - timedelta(minutes=20))}
    result = run(configs("office"), {"office": 20.9}, {"office": 21.0}, outdoor=12.0, runtime=running)
    assert result.room("office").standing_by


def test_a_room_that_has_never_run_is_not_made_to_rest():
    """The post-restart bug, from a live instance.

    At startup the climate entities are not registered yet, so the first
    cycles see no temperature and every room stands by. Stamping the
    participation clock on those cycles made each room look as though it had
    just stopped, so when readings arrived it had to sit out a full rest —
    25 minutes in mild weather — before it could heat. After every restart.
    """
    blind = {"office": RoomMeasurement("office", None, stale=True, source="missing")}
    first = plan(
        rooms=configs("office"),
        measurements=blind,
        intents={"office": RoomIntent("office", 23.5)},
        constraints={"a": Constraints()},
        runtime={},
        group_state={"a": GroupState("a")},
        outdoor=13.0,
        now=NOW,
    )
    remembered = {r.room: r for r in first.runtime}
    assert remembered["office"].since is None, "no transition has happened yet"

    arrived = run(
        configs("office"),
        {"office": 22.5},
        {"office": 23.5},
        outdoor=13.0,
        runtime=remembered,
        now=NOW + timedelta(seconds=30),
    )
    assert arrived.room("office").mode == HEAT, "it should heat, not serve a rest it never earned"


def test_stopping_does_start_the_rest_clock():
    """The other half: a room that genuinely stops must rest."""
    running = {"office": RoomRuntime("office", running=True, mode=HEAT, since=NOW - timedelta(hours=1))}
    stopped = run(
        configs("office"), {"office": 22.6}, {"office": 21.0}, outdoor=13.0, runtime=running
    )
    assert stopped.room("office").standing_by
    remembered = {r.room: r for r in stopped.runtime}
    assert remembered["office"].since == NOW, "the transition restarts the clock"

    too_soon = run(
        configs("office"),
        {"office": 20.0},
        {"office": 21.0},
        outdoor=13.0,
        runtime=remembered,
        now=NOW + timedelta(minutes=5),
    )
    assert "resting" in too_soon.room("office").because


def test_a_room_whose_mode_is_locked_out_is_told_that_not_about_the_group():
    """Name the binding constraint.

    "The group is heating" is true, but if the wanted mode is locked out the
    room would be standing by whatever the group were doing — and that is the
    difference between waiting for a turn and waiting for the weather.
    """
    locked = constraints_for(PolicyConfig(cool_below=20.0), outdoor=13.0)
    result = run(
        configs("office", "bedroom"),
        {"office": 22.5, "bedroom": 19.0},
        {"office": 20.0, "bedroom": 20.0},
        outdoor=13.0,
        constraints=locked,
    )
    assert result.group("a").mode == HEAT
    office = result.room("office")
    assert office.wants == COOL
    assert "locked out" in office.because
    assert "the group is heating" not in office.because


def test_a_room_waiting_for_the_weather_does_not_accrue_starvation():
    """Aging exists so a starved room eventually wins. A room whose mode is
    locked out cannot win at any weight, so ageing it produces an unbounded
    clock that means nothing."""
    locked = constraints_for(PolicyConfig(cool_below=20.0), outdoor=13.0)
    result = run(
        configs("office", "bedroom"),
        {"office": 22.5, "bedroom": 19.0},
        {"office": 20.0, "bedroom": 20.0},
        outdoor=13.0,
        constraints=locked,
        runtime={"office": RoomRuntime("office", unserved_since=NOW - timedelta(hours=6))},
    )
    office = next(r for r in result.runtime if r.room == "office")
    assert office.unserved_since is None, "the clock should stop, not run for ever"


def test_a_room_waiting_its_turn_still_accrues_starvation():
    """The other half: genuine contention must still age, or nothing ever
    gets its turn."""
    result = run(
        configs("office", "bedroom"),
        {"office": 22.5, "bedroom": 19.0},
        {"office": 20.0, "bedroom": 20.0},
        outdoor=22.0,
        constraints=constraints_for(PolicyConfig(cool_below=20.0), outdoor=22.0),
    )
    assert result.group("a").mode == COOL, "2.5° of cool demand outweighs 1° of heat"
    bedroom = next(r for r in result.runtime if r.room == "bedroom")
    assert bedroom.unserved_since == NOW, "the room that lost the argument is waiting its turn"
    office = next(r for r in result.runtime if r.room == "office")
    assert office.unserved_since is None, "the room being served is not starved"
