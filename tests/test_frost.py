"""Frost protection: the floor under every other setting.

A room below the threshold is heated until it reaches release -- whatever
its target, away state or lockouts say, with a stale sensor, and even when
it has been switched off -- because each of those can otherwise leave a unit
off while the room freezes.
"""

from datetime import datetime, timedelta, timezone

from core.model import (
    COOL,
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

NOW = datetime(2026, 1, 15, 3, 0, tzinfo=timezone.utc)
FROST = FrostProtection(below=7.0, release=10.0)
RANGE = (16.0, 26.0)


def room(name, group="a"):
    return RoomConfig(
        name,
        group,
        ParticipationPolicy(
            cut_off_offset=Curve.constant(0.3), min_off=Curve.constant(1200.0)
        ),
    )


def run(
    temps,
    *,
    targets=None,
    allowed=frozenset({HEAT, COOL}),
    reasons=None,
    stale=(),
    runtime=None,
    standby=frozenset(),
    frost=FROST,
    sources=None,
    group_mode=None,
):
    names = list(temps)
    measurements = {
        name: RoomMeasurement(name, value, stale=name in stale, source="test")
        for name, value in temps.items()
    }
    intents = {
        name: RoomIntent(
            name,
            (targets or {}).get(name, 20.0),
            source=(sources or {}).get(name, "default"),
        )
        for name in names
    }
    limits = Constraints(
        allowed_modes=frozenset(allowed), setpoint_range=RANGE, reasons=reasons or {}
    )
    return plan(
        rooms=[room(name) for name in names],
        measurements=measurements,
        intents=intents,
        constraints={"a": limits},
        runtime=runtime or {},
        group_state={"a": GroupState("a", group_mode)},
        outdoor=2.0,
        now=NOW,
        frost=frost,
        standby=standby,
    )


def frost_runtime(name, mode=HEAT, since=NOW - timedelta(minutes=30)):
    return RoomRuntime(name, running=True, mode=mode, since=since, frost=True)


# --------------------------------------------------------------------------
# The latch
# --------------------------------------------------------------------------


def test_the_latch_holds_between_the_thresholds():
    assert FROST.active(6.9, False)
    assert not FROST.active(7.0, False)
    assert FROST.active(9.9, True)
    assert not FROST.active(10.0, True)
    # No reading: keep whatever it was. Dropping protection because a sensor
    # died is exactly the failure this exists to prevent.
    assert FROST.active(None, True)
    assert not FROST.active(None, False)


def test_a_freezing_room_heats_at_the_units_minimum():
    result = run({"erin": 6.0})
    erin = result.room("erin")
    assert result.group("a").mode == HEAT
    assert erin.mode == HEAT and erin.frost
    # Its own target is higher, so it heats to that.
    assert erin.target == 20.0
    assert "frost protection" in erin.because
    assert "frost protection" in result.group("a").because
    assert result.runtime[0].frost


def test_it_keeps_heating_until_release_then_lets_go():
    held = run({"erin": 8.5}, runtime={"erin": frost_runtime("erin")})
    assert held.room("erin").frost and held.room("erin").mode == HEAT

    released = run({"erin": 10.5}, runtime={"erin": frost_runtime("erin")})
    assert not released.room("erin").frost
    assert not released.runtime[0].frost


def test_rest_does_not_delay_frost_protection():
    just_stopped = RoomRuntime("erin", running=False, since=NOW - timedelta(minutes=1))
    result = run({"erin": 6.0}, runtime={"erin": just_stopped})
    assert result.room("erin").mode == HEAT


# --------------------------------------------------------------------------
# What it overrides
# --------------------------------------------------------------------------


def test_it_overrides_a_heating_lockout_and_says_so():
    result = run(
        {"erin": 6.0},
        allowed={COOL},
        reasons={HEAT: "seasonal lock: cooling season"},
    )
    assert result.group("a").mode == HEAT
    assert result.room("erin").mode == HEAT
    assert "overriding: seasonal lock: cooling season" in result.group("a").because


def test_it_overrides_away_and_heats_to_the_floor():
    result = run({"erin": 6.0}, targets={"erin": 16.0}, sources={"erin": "away"})
    erin = result.room("erin")
    assert erin.mode == HEAT and erin.frost
    assert erin.target == 16.0


def test_it_heats_a_room_on_a_stale_reading():
    """Normally a stale room stands by, 'not guessing'. Not this cold."""
    result = run({"erin": 6.0}, stale={"erin"})
    assert result.room("erin").mode == HEAT
    assert result.room("erin").frost


def test_without_frost_protection_a_stale_cold_room_still_stands_by():
    result = run({"erin": 6.0}, stale={"erin"}, frost=None)
    assert result.room("erin").mode is None
    assert "not guessing" in result.room("erin").because


def test_it_wins_the_group_against_a_room_wanting_cool():
    result = run(
        {"erin": 6.0, "office": 26.0},
        targets={"erin": 20.0, "office": 20.0},
        group_mode=COOL,
    )
    assert result.group("a").mode == HEAT
    assert result.room("erin").mode == HEAT
    assert result.room("office").mode is None
    assert "outranks office" in result.group("a").because
    assert "office" in result.group("a").unserved


def test_other_rooms_still_honour_a_heating_lockout():
    """The group heats for the freezing room; that is not licence for the rest."""
    result = run(
        {"erin": 6.0, "office": 17.0},
        allowed={COOL},
        reasons={HEAT: "seasonal lock: cooling season"},
    )
    assert result.room("erin").mode == HEAT
    office = result.room("office")
    assert office.mode is None and not office.frost
    assert "only for frost protection" in office.because


# --------------------------------------------------------------------------
# A room switched off
# --------------------------------------------------------------------------


def test_a_switched_off_room_is_left_alone_while_it_is_warm_enough():
    result = run({"erin": 12.0, "office": 18.0}, standby=frozenset({"erin"}))
    assert [command.room for command in result.rooms] == ["office"]
    assert [runtime.room for runtime in result.runtime] == ["office"]


def test_a_switched_off_room_is_still_protected():
    result = run({"erin": 6.0}, standby=frozenset({"erin"}))
    erin = result.room("erin")
    assert erin.mode == HEAT and erin.frost
    # Only the floor: its intent is not what it is being heated for.
    assert erin.target == 16.0


def test_a_switched_off_room_keeps_being_turned_off_until_it_is():
    """One write can be rate-limited or fail; nothing else would retry it."""
    released = run(
        {"erin": 10.5}, standby=frozenset({"erin"}), runtime={"erin": frost_runtime("erin")}
    )
    erin = released.room("erin")
    assert erin.mode is None
    assert "frost protection released at 10.5°" in erin.because
    (memory,) = released.runtime
    assert memory.releasing and not memory.frost and not memory.running

    # Still releasing: the caller has not yet seen the unit off.
    again = run({"erin": 11.0}, standby=frozenset({"erin"}), runtime={"erin": memory})
    assert again.room("erin").mode is None
    assert again.runtime[0].releasing

    # The caller drops the memory once the unit is off; then it is left alone.
    after = run({"erin": 11.0}, standby=frozenset({"erin"}))
    assert after.rooms == ()


def test_a_room_switched_back_on_mid_release_is_simply_planned():
    memory = RoomRuntime("erin", releasing=True)
    result = run({"erin": 19.0}, runtime={"erin": memory})
    assert not result.runtime[0].releasing


def test_a_releasing_room_that_freezes_again_is_protected_again():
    memory = RoomRuntime("erin", releasing=True)
    result = run({"erin": 6.0}, standby=frozenset({"erin"}), runtime={"erin": memory})
    assert result.room("erin").mode == HEAT and result.room("erin").frost
    assert not result.runtime[0].releasing


def test_a_whole_group_switched_off_still_has_a_decision():
    """Downstream (the mode sensor, a room's attributes) reads its group."""
    result = run({"erin": 12.0}, standby=frozenset({"erin"}))
    assert result.rooms == ()
    assert result.group("a").mode is None
    assert "switched off" in result.group("a").because

    releasing = run(
        {"erin": 10.5}, standby=frozenset({"erin"}), runtime={"erin": frost_runtime("erin")}
    )
    assert releasing.group("a").mode is None and releasing.room("erin").mode is None
