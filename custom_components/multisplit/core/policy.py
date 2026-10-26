"""Policy: what the system is *permitted* to do, and why it is not.

One place answers "may we cool right now?". The planner never re-asks that
question, which is what stops the answer from being re-derived, slightly
differently, in three places.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from .model import COOL, HEAT, MODES, Constraints

SEASON_AUTO = "auto"


@dataclass(frozen=True)
class PolicyConfig:
    """Configuration for one group. Comes from the config flow's options."""

    # Outdoor lockouts. None disables that lockout entirely.
    cool_below: float | None = 22.0  # no cooling when outdoor is below this
    heat_above: float | None = None  # no heating when outdoor is above this
    lockout_hysteresis: float = 1.0

    season: str = SEASON_AUTO  # heat | cool | auto

    setpoint_range: tuple[float, float] = (15.0, 26.0)
    deadband: float = 0.3

    inertia_weight: float = 2.0
    inertia_half_life: timedelta = timedelta(minutes=20)
    aging_rate: float = 0.5

    extra_reasons: dict[str, str] = field(default_factory=dict)


def constraints_for(
    config: PolicyConfig,
    *,
    outdoor: float | None,
    previously_allowed: frozenset[str] | None = None,
) -> Constraints:
    """Build ``Constraints`` for one group.

    ``previously_allowed`` is last cycle's answer, which is what makes the
    lockout hysteretic: without it, an outdoor temperature sitting on the
    threshold would allow and forbid cooling on alternate cycles, and the
    group would change its mind as fast as the sensor noise.

    An unknown outdoor temperature does **not** apply the lockouts. The
    alternative -- forbidding both modes -- protects the hardware but leaves
    the house uncontrolled because a sensor went quiet, which is the worse
    failure. The reason is recorded so the UI can say so out loud.
    """
    previous = previously_allowed if previously_allowed is not None else frozenset(MODES)
    allowed = set(MODES)
    reasons: dict[str, str] = {}

    if config.season == HEAT:
        allowed.discard(COOL)
        reasons[COOL] = "seasonal lock: heating season"
    elif config.season == COOL:
        allowed.discard(HEAT)
        reasons[HEAT] = "seasonal lock: cooling season"

    if outdoor is None:
        reasons.setdefault(
            "outdoor", "outdoor temperature unknown — temperature lockouts not applied"
        )
        return _build(config, allowed, reasons)

    hysteresis = max(0.0, config.lockout_hysteresis)

    if config.cool_below is not None and COOL in allowed:
        # Forbid below the threshold; require the full hysteresis band above it
        # before allowing cooling again.
        threshold = (
            config.cool_below
            if COOL in previous
            else config.cool_below + hysteresis
        )
        if outdoor < threshold:
            allowed.discard(COOL)
            if COOL in previous:
                reasons[COOL] = (
                    f"cooling locked out: outdoor {outdoor:g}° is below the "
                    f"{config.cool_below:g}° lockout"
                )
            else:
                reasons[COOL] = (
                    f"cooling still locked out: outdoor {outdoor:g}° has not "
                    f"reached {threshold:g}°, the {config.cool_below:g}° lockout "
                    f"plus {hysteresis:g}° of hysteresis"
                )

    if config.heat_above is not None and HEAT in allowed:
        threshold = (
            config.heat_above
            if HEAT in previous
            else config.heat_above - hysteresis
        )
        if outdoor > threshold:
            allowed.discard(HEAT)
            if HEAT in previous:
                reasons[HEAT] = (
                    f"heating locked out: outdoor {outdoor:g}° is above the "
                    f"{config.heat_above:g}° lockout"
                )
            else:
                reasons[HEAT] = (
                    f"heating still locked out: outdoor {outdoor:g}° has not "
                    f"fallen to {threshold:g}°, the {config.heat_above:g}° lockout "
                    f"minus {hysteresis:g}° of hysteresis"
                )

    return _build(config, allowed, reasons)


def _build(
    config: PolicyConfig, allowed: set[str], reasons: dict[str, str]
) -> Constraints:
    merged = {**reasons, **config.extra_reasons}
    return Constraints(
        allowed_modes=frozenset(allowed),
        setpoint_range=config.setpoint_range,
        deadband=config.deadband,
        inertia_weight=config.inertia_weight,
        inertia_half_life=config.inertia_half_life,
        aging_rate=config.aging_rate,
        reasons=merged,
    )
