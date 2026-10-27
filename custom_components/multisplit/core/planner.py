"""The planner: facts in, a plan with reasons out.

Imports nothing from Home Assistant. Performs no I/O. Holds no state and
starts no timers -- time-dependent behaviour (changeover inertia, starvation
aging, participation dwell times) works because ``now`` and the previous
``RoomRuntime`` are *inputs*.

That constraint is the most valuable one in the project. It means every
scenario that is slow, seasonal or dangerous to reproduce in a real building
can be asserted in a unit test that runs in microseconds: both rooms opposed,
a room starved for an hour, outdoor exactly at the lockout boundary, a sensor
that has gone quiet, a room overshooting in mild weather.

The planner decides everything and writes nothing. ``reconciler.py`` writes
everything and decides nothing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta

from .model import (
    COOL,
    HEAT,
    Constraints,
    GroupDecision,
    GroupState,
    Plan,
    RoomCommand,
    RoomConfig,
    RoomIntent,
    RoomMeasurement,
    RoomRuntime,
    opposite,
)

_HOUR = 3600.0


def _fmt(value: float) -> str:
    """Format a temperature for a reason string, without trailing noise."""
    return f"{value:.1f}".rstrip("0").rstrip(".") if value % 1 else f"{value:.0f}"


def _fmt_delta(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min" if minutes else f"{hours} h"


# --------------------------------------------------------------------------
# Demand
# --------------------------------------------------------------------------


class _Demand:
    """What one room wants, and what it is physically committed to.

    ``wants`` is comfort demand: what the occupant would ask for. ``holding``
    is hardware reality: a unit that is already running and has not yet
    reached its cut-off is still calling on the compressor, and the group
    cannot turn off underneath it without undoing the duty cycle. The group
    mode is decided on ``votes``, which is either; starvation is measured on
    ``wants`` alone, because only unmet comfort counts as being starved.
    """

    __slots__ = (
        "room",
        "wants",
        "holding",
        "votes",
        "error",
        "target",
        "weight",
        "measurement",
        "intent",
    )

    def __init__(
        self,
        room: str,
        wants: str | None,
        holding: str | None,
        error: float,
        target: float,
        weight: float,
        measurement: RoomMeasurement,
        intent: RoomIntent,
    ) -> None:
        self.room = room
        self.wants = wants
        self.holding = holding
        self.votes = wants or holding
        self.error = error
        self.target = target
        self.weight = weight
        self.measurement = measurement
        self.intent = intent


def _demand(
    room: RoomConfig,
    measurement: RoomMeasurement,
    intent: RoomIntent,
    runtime: RoomRuntime,
    constraints: Constraints,
    delivering: str | None,
    outdoor: float | None,
    now: datetime,
) -> _Demand:
    target = constraints.clamp(intent.target)

    if not measurement.trustworthy:
        # A room with no trustworthy temperature does not get a vote. Guessing
        # here would produce a confident wrong decision for the whole group.
        return _Demand(room.room, None, None, 0.0, target, 0.0, measurement, intent)

    temperature = measurement.temperature or 0.0
    error = target - temperature

    # Asymmetric thresholds. Wanting the opposite of what is currently being
    # delivered has to clear the overshoot the participation controller
    # deliberately tolerates -- otherwise structural overshoot reads as demand
    # for the other mode and the hardware thrashes itself.
    want_heat_above = constraints.deadband
    want_cool_above = constraints.deadband
    if delivering == HEAT:
        want_cool_above += constraints.overshoot_tolerance
    elif delivering == COOL:
        want_heat_above += constraints.overshoot_tolerance

    if error > want_heat_above:
        wants: str | None = HEAT
    elif error < -want_cool_above:
        wants = COOL
    else:
        wants = None

    # A unit already running is committed: it is calling on the compressor
    # whether or not comfort demand still exists. That holds while it is short
    # of its cut-off, and also while it is past the cut-off but inside its
    # minimum run time -- it cannot stop yet, so the group must not change
    # mode out from under it.
    holding: str | None = None
    if runtime.running and runtime.mode is not None:
        cut_off = room.participation.cut_off(outdoor)
        if runtime.mode == HEAT:
            short_of_cut_off = temperature <= target + cut_off
        else:
            short_of_cut_off = temperature >= target - cut_off
        if short_of_cut_off or runtime.elapsed(now) < room.participation.min_on:
            holding = runtime.mode

    # Aging: a room whose comfort demand goes unserved gains weight every
    # cycle, so a genuinely starved room eventually wins without anyone asking.
    aging = 1.0 + constraints.aging_rate * (
        runtime.unserved_for(now).total_seconds() / _HOUR
    )
    magnitude = abs(error)
    if (wants or holding) and magnitude < constraints.deadband:
        # A committed room with no error still has to count for something, or
        # the group would switch out from under it.
        magnitude = constraints.deadband
    weight = magnitude * max(0.0, intent.priority) * aging

    return _Demand(
        room.room, wants, holding, error, target, weight, measurement, intent
    )


# --------------------------------------------------------------------------
# Group mode
# --------------------------------------------------------------------------


def _decide_mode(
    group: str,
    demands: Sequence[_Demand],
    constraints: Constraints,
    state: GroupState,
    now: datetime,
) -> GroupDecision:
    scores = {
        mode: sum(d.weight for d in demands if d.votes == mode)
        for mode in sorted(constraints.allowed_modes)
    }

    # Changeover inertia: staying put earns a bonus that decays with time
    # since the last change. A marginal demand cannot flip the mode; a strong
    # sustained one eventually can. Unlike a hard minimum runtime it degrades
    # gracefully -- the system is never forbidden from responding to a real
    # need, only biased against churn.
    #
    # The bonus applies only to a mode that still has demand. Inertia should
    # resist switching between contested modes, not hold an idle one: a group
    # sitting in cool with nobody wanting cool has no reason to resist heating.
    inertia = 0.0
    if state.mode in scores and scores[state.mode] > 0.0:
        half_lives = state.held_for(now) / constraints.inertia_half_life
        inertia = constraints.inertia_weight * (0.5**half_lives)
        scores[state.mode] += inertia

    wanted = {d.wants for d in demands if d.wants is not None}
    blocked = sorted(wanted - set(constraints.allowed_modes))

    best = max(scores, key=lambda m: (scores[m], m == state.mode, m), default=None)
    if best is None or scores[best] <= 0.0:
        unserved = tuple(d.room for d in demands if d.wants is not None)
        if blocked:
            reasons = "; ".join(
                constraints.reasons.get(m, f"{m} is not permitted") for m in blocked
            )
            because = f"off — the only demand is for {'/'.join(blocked)}: {reasons}"
        elif not wanted:
            because = "off — every room is within its deadband"
        else:
            because = "off — no room has trustworthy demand"
        return GroupDecision(group, None, because, scores, unserved)

    served = [d for d in demands if d.votes == best]
    unserved = tuple(d.room for d in demands if d.wants is not None and d.wants != best)

    detail = ", ".join(
        f"{d.room} {_fmt(abs(d.error))}°"
        + (f" ×{d.intent.priority:g}" if d.intent.priority != 1.0 else "")
        + ("" if d.wants else " (finishing its cycle)")
        for d in sorted(served, key=lambda d: -d.weight)
    )
    because = f"{best} — {len(served)} room(s) asking: {detail}"

    rival = opposite(best)
    if rival in scores and scores[rival] > 0.0:
        because += f"; outweighs {rival} at {scores[rival]:.2f} vs {scores[best]:.2f}"
        if inertia:
            because += f" (including {inertia:.2f} of changeover inertia)"
    if blocked:
        reasons = "; ".join(
            constraints.reasons.get(m, f"{m} is not permitted") for m in blocked
        )
        because += f"; {reasons}"

    return GroupDecision(group, best, because, scores, unserved)


# --------------------------------------------------------------------------
# Participation
# --------------------------------------------------------------------------


def _participate(
    room: RoomConfig,
    mode: str | None,
    demand: _Demand,
    constraints: Constraints,
    runtime: RoomRuntime,
    outdoor: float | None,
    now: datetime,
) -> tuple[str | None, str]:
    """Decide whether this room's unit should be running right now.

    Eligibility (``wants == mode``) decides whether the room *may* run. It
    does not decide whether it *should*, because the hardware cannot deliver a
    small amount of heat: the indoor unit has a minimum capacity, and the
    compressor is running at a speed set by some other room. So this is a
    duty-cycle controller with asymmetric thresholds and dwell times.

    The checks are ordered so that the room reports its own reason in
    preference to the group's. "The group is off" is true but useless when the
    real answer is a stale sensor or a reached cut-off.
    """
    policy = room.participation
    target = demand.target

    if not demand.measurement.trustworthy:
        why = "sensor is stale" if demand.measurement.stale else "no temperature reading"
        return None, f"standing by — {why}, so this room is not guessing"

    temperature = demand.measurement.temperature or 0.0
    cut_off = policy.cut_off(outdoor)
    resume = policy.resume_deadband
    if resume is None:
        resume = constraints.deadband

    def cut_off_state(for_mode: str) -> tuple[bool, float]:
        if for_mode == HEAT:
            point = target + cut_off
            return temperature > point, point
        point = target - cut_off
        return temperature < point, point

    anticipatory = (
        " (before target — minimum capacity will carry it past)" if cut_off < 0 else ""
    )

    # Already running: the cut-off and the minimum run time decide, and they
    # are evaluated against the mode it is actually running in.
    if runtime.running and runtime.mode is not None:
        past_cut_off, point = cut_off_state(runtime.mode)
        ran_for = runtime.elapsed(now)

        if past_cut_off and ran_for >= policy.min_on:
            return None, (
                f"stopped — {_fmt(temperature)}° is past the cut-off of "
                f"{_fmt(point)}°{anticipatory}"
            )
        if mode is not None and mode != runtime.mode:
            return None, (
                f"stopped — the group changed to {mode} while this room was "
                f"{runtime.mode}ing"
            )
        if mode is None:
            return None, "stopped — the group turned off"
        if past_cut_off:
            return mode, (
                f"{mode}ing — past the cut-off but only {_fmt_delta(ran_for)} into a "
                f"{_fmt_delta(policy.min_on)} minimum run"
            )
        return mode, (
            f"{mode}ing — {_fmt(temperature)}° has not reached the cut-off of "
            f"{_fmt(point)}°{anticipatory}"
        )

    # Standing by.
    if mode is None:
        # A room whose demand was blocked by policy should say so itself.
        if demand.wants is not None and demand.wants not in constraints.allowed_modes:
            reason = constraints.reasons.get(
                demand.wants, f"{demand.wants} is not permitted"
            )
            return None, f"standing by — wants {demand.wants}, but {reason}"
        if demand.wants is not None:
            return None, f"standing by — wants {demand.wants}, the group is off"
        return None, (
            f"standing by — satisfied at {_fmt(temperature)}° "
            f"(target {_fmt(target)}°)"
        )

    if demand.wants is None:
        return None, (
            f"standing by — satisfied at {_fmt(temperature)}° "
            f"(target {_fmt(target)}°)"
        )
    if demand.wants != mode:
        return None, f"standing by — wants {demand.wants}, the group is {mode}ing"

    rest = policy.rest(outdoor)
    off_for = runtime.elapsed(now)
    if off_for < rest:
        remaining = rest - off_for
        return None, (
            f"standing by — resting for another {_fmt_delta(remaining)} of a "
            f"{_fmt_delta(rest)} rest"
        )

    ready = (
        temperature < target - resume if mode == HEAT else temperature > target + resume
    )
    if not ready:
        return None, (
            f"standing by — {_fmt(temperature)}° is within {_fmt(resume)}° of target"
        )
    return mode, f"{mode}ing — {_fmt(temperature)}° against a target of {_fmt(target)}°"


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------


def plan(
    *,
    rooms: Sequence[RoomConfig],
    measurements: Mapping[str, RoomMeasurement],
    intents: Mapping[str, RoomIntent],
    constraints: Mapping[str, Constraints],
    runtime: Mapping[str, RoomRuntime],
    group_state: Mapping[str, GroupState],
    outdoor: float | None,
    now: datetime,
) -> Plan:
    """Turn facts into a plan. Pure: same inputs, same plan, always."""
    by_group: dict[str, list[RoomConfig]] = {}
    for room in rooms:
        by_group.setdefault(room.group, []).append(room)

    decisions: list[GroupDecision] = []
    commands: list[RoomCommand] = []
    next_runtime: list[RoomRuntime] = []

    for group, members in by_group.items():
        limits = constraints.get(group, Constraints())
        state = group_state.get(group, GroupState(group))

        demands = {
            room.room: _demand(
                room,
                measurements.get(
                    room.room, RoomMeasurement(room.room, None, stale=True)
                ),
                intents.get(room.room, RoomIntent(room.room, 20.0)),
                runtime.get(room.room, RoomRuntime(room.room)),
                limits,
                state.mode,
                outdoor,
                now,
            )
            for room in members
        }

        decision = _decide_mode(group, list(demands.values()), limits, state, now)
        decisions.append(decision)

        for room in members:
            demand = demands[room.room]
            was = runtime.get(room.room, RoomRuntime(room.room))
            mode, because = _participate(
                room, decision.mode, demand, limits, was, outdoor, now
            )

            commands.append(
                RoomCommand(
                    room=room.room,
                    mode=mode,
                    target=demand.target,
                    because=because,
                    wants=demand.wants,
                    unserved_for=was.unserved_for(now),
                )
            )

            # The next cycle's memory. Computed here, in the pure layer, so the
            # state machine is tested rather than trusted.
            running = mode is not None
            changed = running != was.running or (running and was.mode != mode)
            starved = demand.wants is not None and decision.mode != demand.wants
            next_runtime.append(
                replace(
                    was,
                    running=running,
                    mode=mode,
                    # Only a real transition restarts the clock. A room that
                    # has never run has `since` of None, which `elapsed` reads
                    # as "no transition yet" -- stamping it here instead meant
                    # a room that had merely been seen standing by, with no
                    # sensor data, was treated as having just stopped, and had
                    # to sit out a full rest before it could ever heat.
                    since=now if changed else was.since,
                    unserved_since=(was.unserved_since or now) if starved else None,
                )
            )

    return Plan(
        groups=tuple(decisions),
        rooms=tuple(commands),
        at=now,
        runtime=tuple(next_runtime),
    )
