"""Banking heat on solar surplus.

On surplus a room works towards the far end of its comfort band, so the
building stores heat while it is the house's own. Never past the band, never
while both modes are permitted, and nothing at all with a band of zero or no
surplus -- which is how every existing configuration behaves.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import replay  # noqa: E402

from core.config import load  # noqa: E402
from core.energy import SurplusLatch  # noqa: E402
from core.model import (  # noqa: E402
    COOL,
    HEAT,
    Constraints,
    Curve,
    GroupState,
    ParticipationPolicy,
    RoomConfig,
    RoomIntent,
    RoomMeasurement,
)
from core.planner import plan  # noqa: E402
from core.policy import PolicyConfig, constraints_for  # noqa: E402
from telemetry import record_for  # noqa: E402

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parent.parent

PARTICIPATION = ParticipationPolicy(
    cut_off_offset=Curve(((0.0, 0.3), (14.0, -0.4))),
    min_off=Curve(((0.0, 600.0), (14.0, 2100.0))),
    min_on=timedelta(minutes=5),
)


def limits(allowed, bank):
    return Constraints(
        allowed_modes=frozenset(allowed),
        deadband=0.3,
        overshoot_tolerance=1.5,
        solar_bank=bank,
    )


def office(temperature, *, band, constraints):
    return plan(
        rooms=[RoomConfig("office", "a", PARTICIPATION)],
        measurements={"office": RoomMeasurement("office", temperature, source="test")},
        intents={"office": RoomIntent("office", 20.0, band=band)},
        constraints={"a": constraints},
        runtime={},
        group_state={"a": GroupState("a", None, NOW - timedelta(hours=3))},
        outdoor=7.0,
        now=NOW,
    ).room("office")


# -- the planner ------------------------------------------------------------


def test_on_surplus_a_content_room_banks_heat():
    """20.5° is inside a 20–23 band: content on the grid, worth heating on solar."""
    assert office(20.5, band=3.0, constraints=limits({HEAT}, 0.0)).wants is None

    banked = office(20.5, band=3.0, constraints=limits({HEAT}, 1.0))
    assert banked.wants == HEAT
    assert banked.target == 21.0, "heating aims 1° into the band"
    assert "banking solar: 1° into the band" in banked.because


def test_banking_never_goes_past_the_other_end_of_the_band():
    command = office(19.0, band=0.5, constraints=limits({HEAT}, 1.0))
    assert command.target == 20.5


def test_a_band_of_zero_has_nothing_to_bank_into():
    plain = office(19.5, band=0.0, constraints=limits({HEAT}, 0.0))
    banked = office(19.5, band=0.0, constraints=limits({HEAT}, 1.0))
    assert (banked.wants, banked.target, banked.because) == (
        plain.wants,
        plain.target,
        plain.because,
    )


def test_in_cooling_season_banking_cools_towards_the_bottom():
    command = office(22.5, band=3.0, constraints=limits({COOL}, 1.0))
    assert command.wants == COOL
    assert command.target == 22.0


def test_with_both_modes_permitted_the_planner_does_not_bank():
    """Belt and braces: policy never offers it then, and the planner checks too."""
    command = office(20.5, band=3.0, constraints=limits({HEAT, COOL}, 1.0))
    assert command.wants is None
    assert "banking" not in command.because


# -- policy -----------------------------------------------------------------


def test_policy_banks_only_on_surplus_with_one_mode_permitted():
    config = PolicyConfig(cool_below=22.0, solar_boost=1.0)
    heating_season = constraints_for(config, outdoor=8.0, solar=True)
    assert heating_season.allowed_modes == {HEAT}
    assert heating_season.solar_bank == 1.0
    assert "heating aims 1° higher" in heating_season.reasons["solar"]

    assert constraints_for(config, outdoor=8.0, solar=False).solar_bank == 0.0


def test_policy_refuses_to_bank_with_both_modes_open_and_says_why():
    config = PolicyConfig(cool_below=None, heat_above=None, solar_boost=1.0)
    both = constraints_for(config, outdoor=8.0, solar=True)
    assert both.solar_bank == 0.0
    assert "heat against cool" in both.reasons["solar"]


def test_a_boost_of_zero_is_silent():
    quiet = constraints_for(PolicyConfig(solar_boost=0.0), outdoor=8.0, solar=True)
    assert quiet.solar_bank == 0.0
    assert "solar" not in quiet.reasons


# -- settling ---------------------------------------------------------------


def test_the_latch_follows_only_a_state_that_has_held():
    latch = SurplusLatch(timedelta(minutes=10))
    assert latch.update(True, NOW) is False
    assert latch.update(True, NOW + timedelta(minutes=9)) is False
    assert latch.update(True, NOW + timedelta(minutes=10)) is True


def test_a_passing_cloud_does_not_stop_banking():
    latch = SurplusLatch(timedelta(minutes=10))
    latch.update(True, NOW)
    latch.update(True, NOW + timedelta(minutes=10))
    assert latch.update(False, NOW + timedelta(minutes=11)) is True
    assert latch.update(True, NOW + timedelta(minutes=14)) is True
    # The off-timer restarted when the sun came back.
    assert latch.update(False, NOW + timedelta(minutes=15)) is True
    assert latch.update(False, NOW + timedelta(minutes=24)) is True
    assert latch.update(False, NOW + timedelta(minutes=25)) is False


def test_an_unavailable_sensor_counts_as_no_surplus():
    latch = SurplusLatch(timedelta(0))
    assert latch.update(True, NOW) is True
    assert latch.update(None, NOW + timedelta(minutes=1)) is False


def test_a_restart_never_begins_by_banking():
    assert SurplusLatch(timedelta(minutes=10)).update(True, NOW) is False


# -- configuration and replay ----------------------------------------------


def test_the_shipped_configuration_watches_the_export_sensor():
    config = load(yaml.safe_load((ROOT / "multisplit.yaml").read_text(encoding="utf-8")))
    assert config.energy.surplus_sensor == "binary_sensor.exporting_to_grid"
    assert config.energy.settle == timedelta(minutes=10)


def test_a_file_from_before_banking_never_banks():
    raw = yaml.safe_load((ROOT / "multisplit.yaml").read_text(encoding="utf-8"))
    del raw["energy"]
    del raw["defaults"]["policy"]["solar_boost"]
    config = load(raw)
    assert config.energy.surplus_sensor is None
    assert all(g.policy.solar_boost == 0.0 for g in config.groups)
    assert any("energy.surplus_sensor" in n for n in config.notices)


def test_a_banked_cycle_replays_exactly():
    """The record keeps the band as asked, so replay does not bank twice."""
    config = load(yaml.safe_load((ROOT / "multisplit.yaml").read_text(encoding="utf-8")))
    rooms = [r.as_room_config() for r in config.rooms]
    group_keys = {g.key for g in config.groups}
    intents = {r.room: RoomIntent(r.room, 20.0, band=3.0) for r in rooms}
    measurements = {r.room: RoomMeasurement(r.room, 20.5, source="test") for r in rooms}
    boosted = PolicyConfig(cool_below=22.0, solar_boost=1.0)
    limits_ = {g: constraints_for(boosted, outdoor=8.0, solar=True) for g in group_keys}
    live = plan(
        rooms=rooms,
        measurements=measurements,
        intents=intents,
        constraints=limits_,
        runtime={},
        group_state={g: GroupState(g) for g in group_keys},
        outdoor=8.0,
        now=NOW,
    )
    record = json.loads(
        json.dumps(record_for(live, measurements, 8.0, (), (), False, intents=intents, surplus=True))
    )
    assert record["surplus"] is True

    for g in config.groups:
        object.__setattr__(g, "policy", boosted)
    (_, replayed, _), = replay.replay([record], config)
    for command in live.rooms:
        assert replayed.room(command.room).target == command.target == 21.0
