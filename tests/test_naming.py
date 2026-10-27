"""Suggesting the id someone meant to type.

Prompted by a live instance: `climate.mitsubishi_ac_ischa_mitsubishi_ac_ischa_climat`
was configured and does not exist, where every other room's entity ends in
`climate`. One character, and the room silently never gets a temperature.
"""

from core.naming import closest, describe_missing

REAL = [
    "climate.mitsubishi_ac_livingroom_mitsubishi_ac_livingroom_climate",
    "climate.mitsubishi_ac_ischa_mitsubishi_ac_ischa_climate",
    "climate.mitsubishi_ac_bedroom_mitsubishi_ac_bedroom_climate",
    "climate.mitsubishi_ac_erin_mitsubishi_ac_erin_climate",
    "climate.mitsubishi_ac_office_mitsubishi_ac_office_climate",
    "sensor.average_living_room_temperature",
    "sensor.openweathermap_temperature",
]


def test_it_finds_the_truncated_entity_id():
    """The actual case from the log."""
    assert (
        closest("climate.mitsubishi_ac_ischa_mitsubishi_ac_ischa_climat", REAL)
        == "climate.mitsubishi_ac_ischa_mitsubishi_ac_ischa_climate"
    )


def test_it_never_crosses_domains():
    """A missing climate entity is not a mistyped sensor, and offering one
    would be worse than offering nothing."""
    assert closest("climate.average_living_room_temperature", REAL) is None


def test_it_stays_quiet_when_nothing_is_close():
    assert closest("climate.conservatory_underfloor", REAL) is None


def test_it_does_not_suggest_a_different_room():
    """`erin` and `office` are both real; neither is a correction for the other."""
    suggestion = closest("climate.mitsubishi_ac_attic_mitsubishi_ac_attic_climate", REAL)
    assert suggestion is None, f"should not have offered {suggestion}"


def test_it_handles_an_empty_state_machine():
    assert closest("climate.anything", []) is None


def test_descriptions_carry_the_suggestion_only_when_there_is_one():
    lines = describe_missing(
        [
            "climate.mitsubishi_ac_ischa_mitsubishi_ac_ischa_climat",
            "climate.conservatory_underfloor",
        ],
        REAL,
    )
    assert "did you mean" in lines[0]
    assert lines[1] == "climate.conservatory_underfloor"
