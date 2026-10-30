"""The loader's job is to reject bad configuration clearly.

A missing key must be an error rather than a silent default, and a broken file
should report everything wrong with it in one pass -- someone editing YAML
wants the whole list, not a round trip per typo.
"""

from pathlib import Path

import pytest
import yaml

from core.config import ConfigError, load
from core.model import FrostProtection

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
        "operation.tick_seconds",
        "operation.stale_after_minutes",
        "telemetry.file",
        "telemetry.max_mb",
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


def test_an_unreachable_away_target_is_rejected():
    """Clamped silently, a setback looks exactly like a broken schedule.

    This is the conflict in HARVEST §E: a range starting at 18 with an away
    target of 16 would clamp the setback to 18 and it would never take effect.
    """
    with pytest.raises(ConfigError) as caught:
        load(changed("defaults.policy.setpoint_range", [18.0, 26.0]))
    assert "away_target" in str(caught.value)
    assert "never take effect" in str(caught.value)


def test_lockouts_that_leave_no_permitted_mode_are_rejected():
    """Cooling refused below 25 and heating above 24 leaves a dead band."""
    with pytest.raises(ConfigError) as caught:
        load(changed("defaults.policy.cool_below", 25.0))
    assert "no mode would be permitted" in str(caught.value)


@pytest.mark.parametrize("path", ["defaults.intent.default_target", "defaults.intent.away_target"])
def test_a_missing_comfort_target_is_an_error(path):
    with pytest.raises(ConfigError) as caught:
        load(without(path))
    assert path.split(".")[-1] in str(caught.value)


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


def test_telemetry_flags_must_be_booleans():
    with pytest.raises(ConfigError) as caught:
        load(changed("telemetry.enabled", "yes"))
    assert "'enabled' must be true or false" in str(caught.value)


def test_a_telemetry_path_must_be_a_plain_filename():
    """Writing anywhere other than the configuration directory is not ours to do."""
    with pytest.raises(ConfigError) as caught:
        load(changed("telemetry.file", "../../etc/passwd"))
    assert "plain filename" in str(caught.value)


def test_a_tick_that_would_hammer_the_state_machine_is_rejected():
    with pytest.raises(ConfigError) as caught:
        load(changed("operation.tick_seconds", 1))
    assert "hammer" in str(caught.value)


def test_shadow_mode_must_be_an_explicit_choice():
    """Whether this thing touches the hardware is not a key to leave implicit."""
    with pytest.raises(ConfigError) as caught:
        load(without("operation.apply_to_hardware"))
    assert "apply_to_hardware" in str(caught.value)


def test_a_missing_key_is_reported_once_not_twice():
    """A missing value must not also trip the range check that follows it.

    The regression: a configuration missing `telemetry.movement` reported both
    "missing required key" and "must be positive", which reads as two
    unrelated faults in a file with one thing wrong with it.
    """
    with pytest.raises(ConfigError) as caught:
        load(without("telemetry.movement"))
    about_movement = [p for p in caught.value.problems if "movement" in p]
    assert len(about_movement) == 1
    assert "missing required key" in about_movement[0]


def test_a_configuration_from_an_earlier_version_says_how_to_fix_itself():
    """Keys absent rather than wrong almost always means a stale file."""
    import copy

    raw = copy.deepcopy(GOOD)
    for key in ("on_change", "movement", "heartbeat_minutes"):
        del raw["telemetry"][key]

    with pytest.raises(ConfigError) as caught:
        load(raw)
    assert caught.value.missing, "the absent keys should be recorded as missing"
    assert "generated by an earlier version" in str(caught.value)
    assert "build_config.py" in str(caught.value)


def test_a_wrong_value_does_not_claim_the_file_is_stale():
    """The hint is for absent keys; a bad value is a different mistake."""
    with pytest.raises(ConfigError) as caught:
        load(changed("telemetry.movement", -1))
    assert not caught.value.missing
    assert "earlier version" not in str(caught.value)


def test_a_tick_below_the_floor_explains_why():
    with pytest.raises(ConfigError) as caught:
        load(changed("operation.tick_seconds", 2))
    assert "at least 5" in str(caught.value)
    assert "hammer the state machine" in str(caught.value)


def test_a_zero_priority_is_rejected_rather_than_silently_becoming_one():
    with pytest.raises(ConfigError) as caught:
        load(changed("rooms.office.priority", 0))
    assert "greater than zero" in str(caught.value)


def test_an_omitted_priority_still_defaults_to_one():
    config = load(GOOD)
    assert all(room.priority == 1.0 for room in config.rooms)


# --------------------------------------------------------------------------
# Keys added after a release: an upgrade must not break a file that loaded
# before it, and the default it takes must be said out loud.
# --------------------------------------------------------------------------


def test_the_shipped_configuration_needs_no_defaults():
    assert load(GOOD).notices == ()


def test_a_file_from_before_the_comfort_band_still_loads():
    """The regression: a 0.1.x file failed on 0.3.0 with "missing required
    key 'band'", though zero -- a single setpoint -- is what it meant."""
    config = load(without("defaults.intent.band"))
    assert config.intent.band == 0.0
    assert any("defaults.intent.band" in n and "0.2.0" in n for n in config.notices)


def test_a_file_from_before_the_stale_grace_still_loads():
    from datetime import timedelta

    config = load(without("defaults.policy.stale_grace_minutes"))
    assert config.groups[0].policy.stale_grace == timedelta(minutes=20)
    assert any("stale_grace_minutes" in n and "0.3.0" in n for n in config.notices)


def test_a_defaulted_key_is_still_checked_when_present():
    with pytest.raises(ConfigError) as caught:
        load(changed("defaults.intent.band", -1))
    assert "band" in str(caught.value)


def test_only_keys_added_since_a_release_may_default():
    """The table is the whole of the exception, and each entry must agree
    with what the code did before the key existed."""
    from datetime import timedelta

    from core.config import ADDED_SINCE
    from core.policy import PolicyConfig

    assert set(ADDED_SINCE) == {
        "band", "stale_grace_minutes", "surplus_sensor", "settle_minutes", "solar_boost",
        "min_block_minutes", "frost_below", "frost_release",
    }
    assert ADDED_SINCE["band"][1] == 0.0
    # The deliberate exception: a file from before frost protection loads
    # with it on, because "no floor" is not a safe thing to have meant.
    from core.config import SystemConfig

    assert SystemConfig.frost == FrostProtection(
        ADDED_SINCE["frost_below"][1], ADDED_SINCE["frost_release"][1]
    )
    assert timedelta(minutes=ADDED_SINCE["stale_grace_minutes"][1]) == PolicyConfig.stale_grace
    # Every 0.4.0 default must mean "behave as before": never bank.
    assert ADDED_SINCE["surplus_sensor"][1] is None
    assert ADDED_SINCE["solar_boost"][1] == PolicyConfig.solar_boost == 0.0


# --------------------------------------------------------------------------
# Schedules: who owns a room, and what its targets may be.
# --------------------------------------------------------------------------


def test_a_room_without_settings_is_admin_only_within_the_group_range():
    config = load(GOOD)
    room = config.rooms[0]
    assert room.owners == ()
    assert (room.min_target, room.max_target) == config.groups[0].policy.setpoint_range
    assert config.guardrails(room.key).min_block.total_seconds() == 30 * 60


def test_a_room_can_name_its_owner_and_range():
    import copy

    raw = copy.deepcopy(GOOD)
    raw["rooms"]["erin"].update(owner="Erin", min_target=16, max_target=21)
    rails = load(raw).guardrails("erin")
    assert (rails.min_target, rails.max_target) == (16.0, 21.0)
    assert next(r for r in load(raw).rooms if r.key == "erin").owners == ("Erin",)


def test_a_shared_room_can_have_several_owners():
    import copy

    raw = copy.deepcopy(GOOD)
    raw["rooms"]["bedroom"]["owner"] = ["Paul", "Anna", "Paul"]
    room = next(r for r in load(raw).rooms if r.key == "bedroom")
    assert room.owners == ("Paul", "Anna"), "duplicates dropped, order kept"


@pytest.mark.parametrize("bad", ["", [], ["Erin", ""], 7, {"name": "Erin"}])
def test_a_malformed_owner_is_rejected(bad):
    import copy

    raw = copy.deepcopy(GOOD)
    raw["rooms"]["erin"]["owner"] = bad
    with pytest.raises(ConfigError, match="owner"):
        load(raw)


def test_a_backwards_room_range_is_rejected():
    import copy

    raw = copy.deepcopy(GOOD)
    raw["rooms"]["erin"].update(min_target=21, max_target=18)
    with pytest.raises(ConfigError, match="must be below"):
        load(raw)


def test_a_room_range_that_excludes_the_shared_targets_is_rejected():
    """The default would be refused the first time it applied to this room."""
    import copy

    raw = copy.deepcopy(GOOD)
    raw["rooms"]["erin"].update(min_target=17, max_target=19)
    with pytest.raises(ConfigError) as caught:
        load(raw)
    text = str(caught.value)
    assert "default_target of 20°" in text and "away_target of 16°" in text


def test_a_room_range_outside_the_group_range_is_rejected():
    import copy

    raw = copy.deepcopy(GOOD)
    raw["rooms"]["erin"].update(min_target=10, max_target=21)
    with pytest.raises(ConfigError, match="setpoint range"):
        load(raw)


def test_a_group_with_its_own_narrower_range_still_loads():
    """Review finding: rooms on their group's range were newly held to the
    shared away target, so a file that loaded before stopped loading."""
    import copy

    raw = copy.deepcopy(GOOD)
    policy = copy.deepcopy(raw["defaults"]["policy"])
    policy["setpoint_range"] = [18.0, 26.0]
    raw["groups"]["b"]["policy"] = policy
    config = load(raw)  # away_target 16 sits below group b's 18, as it could before
    assert config.guardrails("erin").min_target == 18.0


# --------------------------------------------------------------------------
# Frost protection
# --------------------------------------------------------------------------


def _intent(raw):
    return raw["defaults"]["intent"]


def _copy():
    import copy

    return copy.deepcopy(GOOD)


def test_the_shipped_configuration_protects_against_frost():
    assert load(GOOD).frost == FrostProtection(7.0, 10.0)


def test_a_file_from_before_frost_protection_still_gets_it_and_is_told():
    raw = _copy()
    del _intent(raw)["frost_below"], _intent(raw)["frost_release"]
    config = load(raw)
    assert config.frost == FrostProtection(7.0, 10.0)
    assert sum("frost_" in notice for notice in config.notices) == 2


def test_frost_protection_can_be_turned_off_but_only_whole():
    raw = _copy()
    _intent(raw)["frost_below"] = None
    _intent(raw)["frost_release"] = None
    assert load(raw).frost is None

    _intent(raw)["frost_release"] = 10.0
    with pytest.raises(ConfigError, match="both be set or both be null"):
        load(raw)


@pytest.mark.parametrize(
    "below, release, message",
    [
        (10.0, 8.0, "must be above 'frost_below'"),
        (7.0, 7.0, "must be above 'frost_below'"),
        (16.0, 18.0, "must be below the away target"),
    ],
)
def test_frost_thresholds_that_could_not_work_are_refused(below, release, message):
    raw = _copy()
    _intent(raw)["frost_below"] = below
    _intent(raw)["frost_release"] = release
    with pytest.raises(ConfigError, match=message):
        load(raw)
