"""The loader's job is to reject bad configuration clearly.

A missing key must be an error rather than a silent default, and a broken file
should report everything wrong with it in one pass -- someone editing YAML
wants the whole list, not a round trip per typo.
"""

from pathlib import Path

import pytest
import yaml

from core.config import ConfigError, load

ROOT = Path(__file__).resolve().parent.parent
GOOD = yaml.safe_load((ROOT / "multisplit.yaml").read_text(encoding="utf-8"))


def without(path):
    """A copy of the shipped config with one key removed."""
    import copy

    raw = copy.deepcopy(GOOD)
    node = raw
    *parents, last = path.split(".")
    for part in parents:
        node = node[part]
    del node[last]
    return raw


def changed(path, value):
    import copy

    raw = copy.deepcopy(GOOD)
    node = raw
    *parents, last = path.split(".")
    for part in parents:
        node = node[part]
    node[last] = value
    return raw


def test_the_shipped_configuration_loads():
    config = load(GOOD)
    assert len(config.rooms) == 5
    assert len(config.groups) == 2


@pytest.mark.parametrize(
    "path",
    [
        "outdoor_sensor",
        "defaults.policy.deadband",
        "defaults.policy.overshoot_tolerance",
        "defaults.policy.cool_below",
        "defaults.participation.cut_off_offset",
        "defaults.participation.rest_minutes",
        "defaults.participation.min_on_minutes",
        "defaults.participation.resume_deadband",
    ],
)
def test_a_missing_value_is_an_error_not_a_hidden_default(path):
    with pytest.raises(ConfigError) as caught:
        load(without(path))
    assert path.split(".")[-1] in str(caught.value)


def test_every_problem_is_reported_in_one_pass():
    raw = without("outdoor_sensor")
    del raw["defaults"]["policy"]["deadband"]
    del raw["defaults"]["participation"]["resume_deadband"]

    with pytest.raises(ConfigError) as caught:
        load(raw)
    assert len(caught.value.problems) >= 3


def test_a_room_in_an_undefined_group_is_rejected():
    with pytest.raises(ConfigError) as caught:
        load(changed("rooms.office.group", "c"))
    assert "'c' is not defined" in str(caught.value)


def test_a_group_with_no_rooms_is_rejected():
    import copy

    raw = copy.deepcopy(GOOD)
    raw["groups"]["c"] = {"name": "Group C"}
    with pytest.raises(ConfigError) as caught:
        load(raw)
    assert "no rooms are assigned" in str(caught.value)


def test_an_entity_id_in_the_wrong_domain_is_rejected():
    with pytest.raises(ConfigError) as caught:
        load(changed("rooms.office.climate_entity", "sensor.not_a_climate_entity"))
    assert "climate.something" in str(caught.value)


def test_a_temperature_sensor_in_the_wrong_domain_is_rejected():
    with pytest.raises(ConfigError) as caught:
        load(changed("rooms.living_room.temperature_sensors", ["binary_sensor.nope"]))
    assert "expected sensor.*" in str(caught.value)


def test_an_unsorted_curve_is_rejected():
    with pytest.raises(ConfigError) as caught:
        load(changed("defaults.participation.rest_minutes", [[13.0, 25], [0.0, 5]]))
    assert "sorted" in str(caught.value)


def test_an_inconsistent_overshoot_tolerance_is_rejected_per_room():
    """The invariant that makes the two controllers agree, enforced at load."""
    with pytest.raises(ConfigError) as caught:
        load(changed("defaults.policy.overshoot_tolerance", 0.5))
    problems = caught.value.problems
    assert len(problems) == 5, "every room should be named"
    assert "raise the tolerance above 1.5" in problems[0]


def test_a_backwards_setpoint_range_is_rejected():
    with pytest.raises(ConfigError) as caught:
        load(changed("defaults.policy.setpoint_range", [26.0, 15.0]))
    assert "not below max" in str(caught.value)


def test_an_unknown_season_is_rejected():
    with pytest.raises(ConfigError) as caught:
        load(changed("defaults.policy.season", "summer"))
    assert "heat, cool or auto" in str(caught.value)


def test_a_room_may_override_the_duty_cycle():
    """HARVEST §C3: a room whose unit is sized differently needs its own."""
    raw = changed("rooms.office.participation", {"rest_minutes": [[0.0, 30]]})
    config = load(raw)
    office = next(r for r in config.rooms if r.key == "office")
    living_room = next(r for r in config.rooms if r.key == "living_room")
    assert office.participation.rest(0.0).total_seconds() == 30 * 60
    assert living_room.participation.rest(0.0).total_seconds() == 5 * 60
    assert office.participation.cut_off(0.0) == 1.5, "unoverridden keys still inherit"
