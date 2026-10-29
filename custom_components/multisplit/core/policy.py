"""Policy: what the system is *permitted* to do, and why it is not.

One place answers "may we cool right now?". The planner never re-asks that
question, which is what stops the answer from being re-derived, slightly
differently, in three places.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from .model import COOL, HEAT, MODES, Constraints, ParticipationPolicy

SEASON_AUTO = "auto"

_NOUN = {HEAT: "heating", COOL: "cooling"}


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

    #: Default taken from the measured cut-off (HARVEST.md §C1): a room is
    #: parked at +1.5° by the duty cycle, so it must not start asking for
    #: cooling before it has cleared that. ``participation_problems`` checks
    #: the pair, because getting this wrong makes the two controllers fight.
    overshoot_tolerance: float = 1.5

    inertia_weight: float = 2.0
    inertia_half_life: timedelta = timedelta(minutes=20)
    aging_rate: float = 0.5
    stale_grace: timedelta = timedelta(minutes=20)
    #: Degrees to move into the comfort band while on solar. 0 = never.
    solar_boost: float = 0.0

    extra_reasons: dict[str, str] = field(default_factory=dict)


def constraints_for(
    config: PolicyConfig,
    *,
    outdoor: float | None,
    previously_allowed: frozenset[str] | None = None,
    solar: bool = False,
) -> Constraints:
    """Build ``Constraints`` for one group.

    ``previously_allowed`` is last cycle's answer, which is what makes the
    lockout hysteretic: without it, an outdoor temperature sitting on the
    threshold would allow and forbid cooling on alternate cycles, and the
    group would change its mind as fast as the sensor noise.

    An unknown outdoor temperature keeps last cycle's answer. Weather moves
    slowly, so a quiet sensor is no reason to change what is permitted --
    and above all no reason to *lift* a lockout. Lifting it was the old
    behaviour, and it failed open: field telemetry recorded a group choosing
    cool at 13° outdoor for the cycles the sensor dropped out.

    With no previous answer (the first cycle after a start), cooling is
    refused and heating permitted. Heating in unknown weather is the
    recoverable mistake; cooling a house that is already cold is not what a
    lockout exists to allow. The reason is recorded so the UI can say so.

    ``solar`` says the house is running on its own generation. It never
    changes what is permitted; it only lets the group bank heat (or cool)
    inside the comfort band -- see ``Constraints.solar_bank``.
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
        if previously_allowed is not None:
            for mode in set(allowed) - set(previously_allowed):
                allowed.discard(mode)
                reasons.setdefault(
                    mode,
                    f"{_NOUN[mode]} locked out: outdoor temperature unknown, "
                    "keeping the last lockout decision",
                )
            reasons.setdefault(
                "outdoor", "outdoor temperature unknown — keeping the last lockout decision"
            )
        else:
            if config.cool_below is not None and COOL in allowed:
                allowed.discard(COOL)
                reasons[COOL] = (
                    "cooling locked out: outdoor temperature unknown, "
                    "and there is no earlier reading to go on"
                )
            reasons.setdefault(
                "outdoor", "outdoor temperature unknown — cooling refused until it is known"
            )
        return _build(config, allowed, reasons, solar)

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

    return _build(config, allowed, reasons, solar)


def _build(
    config: PolicyConfig, allowed: set[str], reasons: dict[str, str], solar: bool
) -> Constraints:
    bank = 0.0
    boost = max(0.0, config.solar_boost)
    if solar and boost > 0:
        if len(allowed) == 1:
            bank = boost
            (only,) = allowed
            direction = "higher" if only == HEAT else "lower"
            reasons["solar"] = (
                f"on solar: {_NOUN[only]} aims {boost:g}° {direction} within "
                "each comfort band, banking it while it is the house's own"
            )
        else:
            reasons["solar"] = (
                "on solar, but not banking: with both modes permitted, moving "
                "into the band would invite heat against cool"
            )
    merged = {**reasons, **config.extra_reasons}
    return Constraints(
        allowed_modes=frozenset(allowed),
        setpoint_range=config.setpoint_range,
        deadband=config.deadband,
        overshoot_tolerance=config.overshoot_tolerance,
        inertia_weight=config.inertia_weight,
        inertia_half_life=config.inertia_half_life,
        aging_rate=config.aging_rate,
        stale_grace=config.stale_grace,
        solar_bank=bank,
        reasons=merged,
    )


def participation_problems(
    policy: ParticipationPolicy, constraints: Constraints
) -> list[str]:
    """Invariants a room's duty cycle has to satisfy to be self-consistent.

    The important one is not obvious and is easy to get wrong by hand: the
    overshoot a room is *permitted* to sit in must be smaller than the
    overshoot at which it starts *asking for cooling*. Otherwise the duty
    cycle parks the room in a state that reads as demand for the opposite
    mode, and the two controllers fight -- every cycle, by construction.

    Returned as a list so the config flow can refuse the combination and say
    which number to change, instead of accepting it and thrashing the
    compressor in February.
    """
    problems: list[str] = []

    worst_cut_off = max(y for _, y in policy.cut_off_offset.points)
    resume = (
        constraints.deadband
        if policy.resume_deadband is None
        else policy.resume_deadband
    )
    asks_for_opposite_at = constraints.deadband + constraints.overshoot_tolerance

    if worst_cut_off >= asks_for_opposite_at:
        problems.append(
            f"a room is allowed to overshoot by {worst_cut_off:g}° but starts "
            f"asking for the opposite mode at {asks_for_opposite_at:g}° "
            f"(deadband {constraints.deadband:g}° + tolerance "
            f"{constraints.overshoot_tolerance:g}°); raise the tolerance above "
            f"{worst_cut_off:g}°"
        )

    if resume > worst_cut_off + max(0.0, constraints.deadband):
        problems.append(
            f"the resume band of {resume:g}° is wider than the cut-off of "
            f"{worst_cut_off:g}°, so a room would have to cool further than it "
            f"was ever heated before it restarts"
        )

    shortest_rest = min(y for _, y in policy.min_off.points)
    if shortest_rest <= 0:
        problems.append("the rest period must be positive, or the unit will short-cycle")

    return problems
