"""Policy answers one question: what are we permitted to do, and why not."""

from core.model import COOL, HEAT
from core.policy import PolicyConfig, constraints_for


def test_cooling_allowed_when_warm_enough():
    c = constraints_for(PolicyConfig(cool_below=22.0), outdoor=25.0)
    assert c.allowed_modes == frozenset({HEAT, COOL})
    assert COOL not in c.reasons


def test_cooling_locked_out_below_the_threshold_with_a_reason():
    c = constraints_for(PolicyConfig(cool_below=22.0), outdoor=19.4)
    assert c.allowed_modes == frozenset({HEAT})
    assert "19.4" in c.reasons[COOL] and "22" in c.reasons[COOL]


def test_lockout_is_hysteretic_so_it_cannot_flap():
    """At 22.5 the answer depends on which side it came from."""
    config = PolicyConfig(cool_below=22.0, lockout_hysteresis=1.0)

    coming_down = constraints_for(
        config, outdoor=22.5, previously_allowed=frozenset({HEAT, COOL})
    )
    assert COOL in coming_down.allowed_modes

    coming_up = constraints_for(
        config, outdoor=22.5, previously_allowed=frozenset({HEAT})
    )
    assert COOL not in coming_up.allowed_modes
    assert "23" in coming_up.reasons[COOL]

    recovered = constraints_for(
        config, outdoor=23.1, previously_allowed=frozenset({HEAT})
    )
    assert COOL in recovered.allowed_modes


def test_heating_lockout_mirrors_cooling():
    c = constraints_for(PolicyConfig(heat_above=24.0), outdoor=26.0)
    assert c.allowed_modes == frozenset({COOL})
    assert "26" in c.reasons[HEAT]


def test_unknown_outdoor_never_lifts_a_lockout():
    """Field telemetry: the group chose cool at 13° outdoor while the sensor was out."""
    locked = constraints_for(PolicyConfig(cool_below=20.0), outdoor=13.0)
    assert COOL not in locked.allowed_modes
    c = constraints_for(
        PolicyConfig(cool_below=20.0), outdoor=None, previously_allowed=locked.allowed_modes
    )
    assert c.allowed_modes == frozenset({HEAT})
    assert "unknown" in c.reasons["outdoor"]
    assert "unknown" in c.reasons[COOL]


def test_unknown_outdoor_keeps_a_permission_it_already_had():
    """Weather moves slowly; a quiet sensor is no reason to change what is allowed."""
    c = constraints_for(
        PolicyConfig(cool_below=20.0), outdoor=None, previously_allowed=frozenset({HEAT, COOL})
    )
    assert c.allowed_modes == frozenset({HEAT, COOL})


def test_unknown_outdoor_at_start_refuses_cooling_but_still_heats():
    """Heating in unknown weather is the recoverable mistake."""
    c = constraints_for(PolicyConfig(cool_below=20.0), outdoor=None)
    assert c.allowed_modes == frozenset({HEAT})
    assert "unknown" in c.reasons[COOL]


def test_seasonal_lock_overrides_everything():
    c = constraints_for(PolicyConfig(season=HEAT), outdoor=30.0)
    assert c.allowed_modes == frozenset({HEAT})
    assert "season" in c.reasons[COOL]
