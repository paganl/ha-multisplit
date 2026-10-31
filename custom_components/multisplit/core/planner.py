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
    FrostProtection,
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


def _band(demand: "_Demand") -> str:
    """How the room's comfort is described in a reason."""
    if demand.cool_to > demand.heat_to:
        return f"(wanted between {_fmt(demand.heat_to)}° and {_fmt(demand.cool_to)}°)"
    return f"(target {_fmt(demand.heat_to)}°)"


def _fmt_grace(left: timedelta) -> str:
    """Time left on a short grace; whole minutes would say "0 min" for most of it."""
    seconds = max(0, int(left.total_seconds()))
    return f"{seconds} s" if seconds < 60 else _fmt_delta(left)


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

    ``wants`` is comfort demand: what the occupant would ask for. ``acts``
    is the part of it the room would actually act on -- for a room not
    already running in that mode, demand that has also cleared its resume
    band, because a room inside its resume band will not start whatever the
    group does. ``holding`` is hardware reality: a unit that is already
    running and has not yet reached its cut-off is still calling on the
    compressor, and the group cannot turn off underneath it without undoing
    the duty cycle.

    The group mode is decided on ``votes``, which is ``acts`` or ``holding``
    -- never bare ``wants``. A room that would not switch on must not choose
    the group's mode: with both modes permitted, that would deny a room that
    *would* run in the other one. Starvation is measured on ``acts``, because
    a room that would not run if served is not being starved.
    """

    __slots__ = (
        "room",
        "wants",
        "acts",
        "holding",
        "votes",
        "error",
        "heat_to",
        "cool_to",
        "weight",
        "measurement",
        "intent",
        "banked",
        "frost",
    )

    def __init__(
        self,
        room: str,
        wants: str | None,
        acts: str | None,
        holding: str | None,
        error: float,
        heat_to: float,
        cool_to: float,
        weight: float,
        measurement: RoomMeasurement,
        intent: RoomIntent,
        banked: float = 0.0,
        frost: bool = False,
    ) -> None:
        self.room = room
        self.wants = wants
        self.acts = acts
        self.holding = holding
        self.votes = acts or holding
        self.error = error
        self.heat_to = heat_to
        self.cool_to = cool_to
        self.weight = weight
        self.measurement = measurement
        self.intent = intent
        self.banked = banked
        self.frost = frost

    def target_for(self, mode: str | None) -> float:
        """Heating aims at the bottom of the band, cooling at the top."""
        return self.cool_to if mode == COOL else self.heat_to


def _demand(
    room: RoomConfig,
    measurement: RoomMeasurement,
    intent: RoomIntent,
    runtime: RoomRuntime,
    constraints: Constraints,
    delivering: str | None,
    outdoor: float | None,
    now: datetime,
    *,
    frost: FrostProtection | None = None,
    freezing: bool = False,
    standby: bool = False,
) -> _Demand:
    heat_to = constraints.clamp(intent.heat_to)
    cool_to = max(heat_to, constraints.clamp(intent.cool_to))

    if freezing and frost is not None:
        # Below the frost floor nothing else gets a say: not the schedule, not
        # away, not a lockout, not a stale sensor, not the room being switched
        # off. The unit is asked for its lowest setpoint -- or the room's own
        # target, if that is higher and the room is taking part -- and the
        # release threshold stops it, not the setpoint.
        floor = constraints.clamp(frost.release)
        heat_to = floor if standby else max(heat_to, floor)
        cool_to = max(heat_to, cool_to)
        reading = measurement.temperature
        error = max(constraints.deadband, frost.release - reading) if reading is not None else (
            constraints.deadband
        )
        return _Demand(
            room.room,
            HEAT,
            HEAT,
            HEAT,
            error,
            heat_to,
            cool_to,
            error * max(0.0, intent.priority) or constraints.deadband,
            measurement,
            intent,
            0.0,
            frost=True,
        )

    # On solar, work towards the far end of the band: heat banked now is heat
    # not bought later. Only ever within the band, and policy offers it only
    # when one mode is permitted, so the other end cannot start asking.
    banked = 0.0
    if constraints.solar_bank > 0:
        if constraints.allowed_modes == frozenset({HEAT}):
            raised = min(cool_to, heat_to + constraints.solar_bank)
            banked, heat_to = raised - heat_to, raised
        elif constraints.allowed_modes == frozenset({COOL}):
            lowered = max(heat_to, cool_to - constraints.solar_bank)
            banked, cool_to = cool_to - lowered, lowered

    if not measurement.trustworthy:
        # A room with no trustworthy temperature has no comfort demand: an idle
        # room never starts a cycle on a guess. But a room already running is
        # committed hardware, and abandoning it the moment its sensor goes
        # quiet withdraws heat from a room that may still be below target. So
        # for a bounded grace it keeps holding its mode -- the last safe
        # decision -- and after that it fails safe and stops.
        holding_stale: str | None = None
        if _in_grace(measurement, runtime, constraints, now):
            holding_stale = runtime.mode
        weight = constraints.deadband * max(0.0, intent.priority) if holding_stale else 0.0
        return _Demand(
            room.room,
            None,
            None,
            holding_stale,
            0.0,
            heat_to,
            cool_to,
            weight,
            measurement,
            intent,
            banked,
        )

    temperature = measurement.temperature or 0.0

    # The room is content anywhere inside its band; demand is the distance
    # outside it, in whichever direction it has left.
    below = heat_to - temperature
    above = temperature - cool_to

    # Wanting the opposite of what is being delivered has to clear the
    # overshoot the participation controller deliberately tolerates, or
    # structural overshoot reads as demand for the other mode and the hardware
    # thrashes itself.
    #
    # Measured from the mode's own end of the band, and combined with the band
    # by taking whichever threshold is further out. A wide band already
    # separates the two demands, so adding the tolerance on top of it would
    # double-count the same separation twice over.
    want_heat_above = constraints.deadband
    want_cool_above = constraints.deadband
    if delivering == HEAT:
        want_cool_above = max(
            want_cool_above,
            constraints.deadband + constraints.overshoot_tolerance - (cool_to - heat_to),
        )
    elif delivering == COOL:
        want_heat_above = max(
            want_heat_above,
            constraints.deadband + constraints.overshoot_tolerance - (cool_to - heat_to),
        )

    if below > want_heat_above:
        wants: str | None = HEAT
        error = below
    elif above > want_cool_above:
        wants = COOL
        error = above
    else:
        wants = None
        error = 0.0

    # Demand the room would act on. A room already running in the wanted mode
    # acts on it by definition; one that is not must also be outside its
    # resume band, the same threshold ``_participate`` uses to start it.
    # Without this, a room 0.5° below target wanted heat (deadband 0.3) and
    # chose the group's mode, then declined to run (resume band 0.5).
    acts = wants
    if wants is not None and not (runtime.running and runtime.mode == wants):
        if error <= _resume(room, constraints):
            acts = None

    # A unit already running is committed: it is calling on the compressor
    # whether or not comfort demand still exists. That holds while it is short
    # of its cut-off, and also while it is past the cut-off but inside its
    # minimum run time -- it cannot stop yet, so the group must not change
    # mode out from under it.
    holding: str | None = None
    if runtime.running and runtime.mode is not None:
        cut_off = room.participation.cut_off(outdoor)
        if runtime.mode == HEAT:
            short_of_cut_off = temperature <= heat_to + cut_off
        else:
            short_of_cut_off = temperature >= cool_to - cut_off
        if short_of_cut_off or runtime.elapsed(now) < room.participation.min_on:
            holding = runtime.mode

    # Aging: a room whose comfort demand goes unserved gains weight every
    # cycle, so a genuinely starved room eventually wins without anyone asking.
    aging = 1.0 + constraints.aging_rate * (
        runtime.unserved_for(now).total_seconds() / _HOUR
    )
    magnitude = abs(error)
    if (acts or holding) and magnitude < constraints.deadband:
        # A committed room with no error still has to count for something, or
        # the group would switch out from under it.
        magnitude = constraints.deadband
    weight = magnitude * max(0.0, intent.priority) * aging

    if not (acts or holding):
        weight = 0.0

    return _Demand(
        room.room,
        wants,
        acts,
        holding,
        error,
        heat_to,
        cool_to,
        weight,
        measurement,
        intent,
        banked,
    )


def _resume(room: RoomConfig, constraints: Constraints) -> float:
    resume = room.participation.resume_deadband
    return constraints.deadband if resume is None else resume


def _in_grace(
    measurement: RoomMeasurement,
    runtime: RoomRuntime,
    constraints: Constraints,
    now: datetime,
) -> bool:
    """Whether a running room may keep its mode on an untrustworthy reading.

    Applies with no reading at all, too. Holding is safe for a bounded time
    because the unit was commanded a setpoint as well as a mode, and
    regulates to it on its own sensor while ours is quiet.
    """
    del measurement  # the decision is the same whatever the reading was
    return (
        runtime.running
        and runtime.mode is not None
        and runtime.untrusted_for(now) < constraints.stale_grace
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
    frost: FrostProtection | None = None,
) -> GroupDecision:
    scores = {
        mode: sum(d.weight for d in demands if d.votes == mode)
        for mode in sorted(constraints.allowed_modes)
    }

    freezing = [d for d in demands if d.frost]
    if freezing and frost is not None:
        # Not a vote. A room this cold is heated whatever the others want and
        # whatever is permitted: a lockout or a season exists to save energy
        # or comfort, and neither is worth a frozen room.
        detail = ", ".join(
            f"{d.room} at {_fmt(d.measurement.temperature)}°"
            if d.measurement.temperature is not None
            else f"{d.room} (no reading, still protected)"
            for d in freezing
        )
        because = (
            f"heat — frost protection: {detail}, heating below {_fmt(frost.below)}° "
            f"until {_fmt(frost.release)}°"
        )
        if HEAT not in constraints.allowed_modes:
            reason = constraints.reasons.get(HEAT, "heating is not permitted")
            because += f"; overriding: {reason}"
        others = [
            d.room for d in demands if not d.frost and d.acts is not None and d.acts != HEAT
        ]
        if others:
            because += f"; outranks {', '.join(others)}"
        unserved = tuple(d.room for d in demands if d.acts is not None and d.acts != HEAT)
        return GroupDecision(group, HEAT, because, scores, unserved)

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

    # Blocked demand is judged on bare comfort, so a locked-out room still
    # says why; arbitration and "unserved" are judged on what rooms would act on.
    wanted = {d.wants for d in demands if d.wants is not None}
    blocked = sorted(wanted - set(constraints.allowed_modes))
    acting = {d.acts for d in demands if d.acts is not None}

    best = max(scores, key=lambda m: (scores[m], m == state.mode, m), default=None)
    if best is None or scores[best] <= 0.0:
        unserved = tuple(d.room for d in demands if d.wants is not None)
        if blocked and not (acting - set(blocked)):
            reasons = "; ".join(
                constraints.reasons.get(m, f"{m} is not permitted") for m in blocked
            )
            because = f"off — the only demand is for {'/'.join(blocked)}: {reasons}"
        elif not acting:
            because = "off — every room is within its deadband"
        else:
            because = "off — no room has trustworthy demand"
        return GroupDecision(group, None, because, scores, unserved)

    served = [d for d in demands if d.votes == best]
    unserved = tuple(d.room for d in demands if d.acts is not None and d.acts != best)

    detail = ", ".join(
        f"{d.room} {_fmt(abs(d.error))}°"
        + (f" ×{d.intent.priority:g}" if d.intent.priority != 1.0 else "")
        + (
            ""
            if d.acts
            else " (holding on a stale reading)"
            if not d.measurement.trustworthy
            else " (finishing its cycle)"
        )
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
    frost_release: float | None = None,
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

    if demand.frost:
        reading = demand.measurement.temperature
        if reading is None:
            return HEAT, (
                "heating — frost protection, with no reading: holding "
                f"{_fmt(demand.heat_to)}° on the unit's own sensor"
            )
        return HEAT, (
            f"heating — frost protection: {_fmt(reading)}° is too cold, heating "
            f"until {_fmt(frost_release)}°"
            if frost_release is not None
            else f"heating — frost protection at {_fmt(reading)}°"
        )

    if mode is not None and mode not in constraints.allowed_modes:
        # The group is heating only because another room is below the frost
        # floor. Everyone else still honours the lockout.
        reason = constraints.reasons.get(mode, f"{mode} is not permitted")
        verb = "stopped" if runtime.running else "standing by"
        return None, (
            f"{verb} — the group is heating only for frost protection elsewhere; "
            f"{reason}"
        )

    temperature = demand.measurement.temperature or 0.0
    cut_off = policy.cut_off(outdoor)
    resume = _resume(room, constraints)

    if not demand.measurement.trustworthy:
        why = "sensor is stale" if demand.measurement.stale else "no temperature reading"
        if not (runtime.running and runtime.mode is not None):
            return None, f"standing by — {why}, so this room is not guessing"
        if not _in_grace(demand.measurement, runtime, constraints, now):
            return None, (
                f"stopped — {why} for longer than the "
                f"{_fmt_delta(constraints.stale_grace)} grace, failing safe"
            )
        if mode != runtime.mode:
            return None, (
                "stopped — the group turned off"
                if mode is None
                else f"stopped — the group changed to {mode} while this room was "
                f"{runtime.mode}ing"
            )
        # Holding the last decision, but still honouring the cut-off against
        # the last value seen: a grace period is not licence to overshoot.
        last = demand.measurement.temperature
        if last is not None:
            end = demand.target_for(runtime.mode)
            point = end + cut_off if runtime.mode == HEAT else end - cut_off
            past = last > point if runtime.mode == HEAT else last < point
            if past and runtime.elapsed(now) >= policy.min_on:
                return None, (
                    f"stopped — last reading {_fmt(last)}° is past the cut-off "
                    f"of {_fmt(point)}° ({why})"
                )
        left = constraints.stale_grace - runtime.untrusted_for(now)
        seen = f" (last reading {_fmt(last)}°)" if last is not None else ""
        return runtime.mode, (
            f"{runtime.mode}ing — {why}; keeping the last decision for up to "
            f"another {_fmt_delta(left)}{seen}"
        )

    def cut_off_state(for_mode: str) -> tuple[bool, float]:
        """Judged against the end of the band that mode is working towards."""
        end = demand.target_for(for_mode)
        if for_mode == HEAT:
            point = end + cut_off
            return temperature > point, point
        point = end - cut_off
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
        if demand.wants is not None and demand.acts is None:
            # Leaning towards a mode, but not far enough to start a cycle --
            # which is why it did not turn the group on.
            return None, (
                f"standing by — {_fmt(temperature)}° is within {_fmt(resume)}° of target"
            )
        if demand.wants is not None:
            return None, f"standing by — wants {demand.wants}, the group is off"
        return None, f"standing by — satisfied at {_fmt(temperature)}° {_band(demand)}"

    if demand.wants is None:
        return None, f"standing by — satisfied at {_fmt(temperature)}° {_band(demand)}"
    if demand.wants != mode:
        # Name the binding constraint, not the visible one. "The group is
        # heating" is true, but if the wanted mode is locked out the room
        # would be standing by whatever the group were doing -- and that is
        # the difference between waiting for a turn and waiting for weather.
        if demand.wants not in constraints.allowed_modes:
            reason = constraints.reasons.get(
                demand.wants, f"{demand.wants} is not permitted"
            )
            return None, f"standing by — wants {demand.wants}, but {reason}"
        return None, f"standing by — wants {demand.wants}, the group is {mode}ing"

    rest = policy.rest(outdoor)
    off_for = runtime.elapsed(now)
    if off_for < rest:
        remaining = rest - off_for
        return None, (
            f"standing by — resting for another {_fmt_delta(remaining)} of a "
            f"{_fmt_delta(rest)} rest"
        )

    end = demand.target_for(mode)
    ready = (
        temperature < end - resume if mode == HEAT else temperature > end + resume
    )
    if not ready:
        return None, (
            f"standing by — {_fmt(temperature)}° is within {_fmt(resume)}° of target"
        )
    return mode, f"{mode}ing — {_fmt(temperature)}° against a target of {_fmt(end)}°"


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
    frost: FrostProtection | None = None,
    standby: frozenset[str] = frozenset(),
    open_windows: frozenset[str] = frozenset(),
) -> Plan:
    """Turn facts into a plan. Pure: same inputs, same plan, always.

    ``standby`` names rooms switched off at their own request. They ask for
    nothing, vote for nothing and are given no command -- their unit is left
    alone -- unless they fall below ``frost``'s threshold, when they are
    heated like any other frost-protected room. When that protection ends,
    such a room is commanded off, undoing what frost protection did, and
    keeps being commanded off (``RoomRuntime.releasing``) until the caller
    sees the unit off and drops that memory; then it is left alone again.

    ``open_windows`` names rooms whose window or door reads open right now.
    Once one has been open for its ``lockout_grace`` it stands aside: it
    votes for nothing, is not counted as unserved, and its unit is commanded
    off -- unlike a switched-off room, which is left alone, because heating
    the street is the thing to stop. Until the grace runs out it takes part
    as normal, so a door walked through changes nothing. Frost protection
    still wins over an open window; a switched-off room stays left alone.

    Every group gets a decision, even one whose rooms are all switched off,
    so nothing downstream has to cope with a group missing from the plan.
    """
    by_group: dict[str, list[RoomConfig]] = {}
    for room in rooms:
        by_group.setdefault(room.group, []).append(room)

    decisions: list[GroupDecision] = []
    commands: list[RoomCommand] = []
    next_runtime: list[RoomRuntime] = []

    def measured(name: str) -> RoomMeasurement:
        return measurements.get(name, RoomMeasurement(name, None, stale=True))

    for group, all_members in by_group.items():
        limits = constraints.get(group, Constraints())
        state = group_state.get(group, GroupState(group))

        # Judged on the last reading even when it is stale: a room last seen
        # at 6° is no less cold for its sensor having gone quiet.
        freezing = {
            room.room: frost is not None
            and frost.active(
                measured(room.room).temperature,
                runtime.get(room.room, RoomRuntime(room.room)).frost,
            )
            for room in all_members
        }

        # When each room's window was first seen open, carried into the next
        # cycle for every room -- a room in its grace, or switched off, must
        # not have its clock restart.
        opened_at = {
            room.room: (
                (runtime.get(room.room, RoomRuntime(room.room)).open_since or now)
                if room.room in open_windows
                else None
            )
            for room in all_members
        }

        members: list[RoomConfig] = []
        aside: list[str] = []
        for room in all_members:
            was = runtime.get(room.room, RoomRuntime(room.room))
            opened = opened_at[room.room]
            if room.room not in standby and not freezing[room.room] and (
                opened is not None and now - opened >= room.lockout_grace
            ):
                # Standing aside for an open window: off, and out of the vote.
                intent = intents.get(room.room, RoomIntent(room.room, 20.0))
                heat_to = limits.clamp(intent.heat_to)
                verb = "stopped" if was.running else "standing by"
                commands.append(
                    RoomCommand(
                        room=room.room,
                        mode=None,
                        target=heat_to,
                        because=(
                            f"{verb} — window open for {_fmt_delta(now - opened)}; "
                            "rejoins when it closes"
                        ),
                        heat_to=heat_to,
                        cool_to=max(heat_to, limits.clamp(intent.cool_to)),
                        window_open=True,
                    )
                )
                next_runtime.append(
                    replace(
                        was,
                        running=False,
                        mode=None,
                        # Stopping is a transition: the rest period starts now.
                        since=now if was.running else was.since,
                        unserved_since=None,
                        frost=False,
                        releasing=False,
                        open_since=opened,
                    )
                )
                aside.append(room.room)
                continue
            if room.room not in standby or freezing[room.room]:
                members.append(room)
                continue
            if was.frost or was.releasing:
                # Protection has ended for a room that is switched off: put
                # its unit back the way it is meant to be. Repeated until the
                # caller has seen it off -- see ``RoomRuntime.releasing``.
                reading = measured(room.room).temperature
                commands.append(
                    RoomCommand(
                        room=room.room,
                        mode=None,
                        target=limits.clamp(frost.release) if frost else 0.0,
                        because=(
                            "stopped — frost protection released"
                            + (f" at {_fmt(reading)}°" if reading is not None else "")
                            + "; this room is switched off"
                        ),
                    )
                )
                next_runtime.append(
                    RoomRuntime(room.room, releasing=True, open_since=opened)
                )
            elif opened is not None:
                # Switched off and left alone, but its window clock must
                # survive, or switching it back on would restart the grace.
                next_runtime.append(RoomRuntime(room.room, open_since=opened))

        window_note = f"standing aside, window open: {', '.join(aside)}" if aside else ""

        if not members:
            decisions.append(
                GroupDecision(
                    group,
                    None,
                    f"off — no room in this group is taking part; {window_note}"
                    if aside
                    else "off — every room in this group is switched off",
                )
            )
            continue

        demands = {
            room.room: _demand(
                room,
                measured(room.room),
                intents.get(room.room, RoomIntent(room.room, 20.0)),
                runtime.get(room.room, RoomRuntime(room.room)),
                limits,
                state.mode,
                outdoor,
                now,
                frost=frost,
                freezing=freezing[room.room],
                standby=room.room in standby,
            )
            for room in members
        }

        decision = _decide_mode(group, list(demands.values()), limits, state, now, frost)
        if window_note:
            decision = replace(decision, because=f"{decision.because}; {window_note}")
        decisions.append(decision)

        for room in members:
            demand = demands[room.room]
            was = runtime.get(room.room, RoomRuntime(room.room))
            mode, because = _participate(
                room,
                decision.mode,
                demand,
                limits,
                was,
                outdoor,
                now,
                frost.release if frost is not None else None,
            )
            opened = opened_at[room.room]
            if opened is not None and room.room not in standby:
                if demand.frost:
                    because += " (window open, heated anyway for frost protection)"
                else:
                    left = room.lockout_grace - (now - opened)
                    because += f" (window open — standing aside in {_fmt_grace(left)})"

            commands.append(
                RoomCommand(
                    room=room.room,
                    mode=mode,
                    target=demand.target_for(mode or demand.wants),
                    because=(
                        f"{because} (banking solar: {_fmt(demand.banked)}° into the band)"
                        if demand.banked and demand.measurement.trustworthy
                        else because
                    ),
                    wants=demand.wants,
                    unserved_for=was.unserved_for(now),
                    heat_to=demand.heat_to,
                    cool_to=demand.cool_to,
                    frost=demand.frost,
                )
            )

            # The next cycle's memory. Computed here, in the pure layer, so the
            # state machine is tested rather than trusted.
            running = mode is not None
            changed = running != was.running or (running and was.mode != mode)
            # Starvation is for a room waiting its turn, not one waiting for
            # the weather. Aging exists to let a starved room eventually win;
            # a room whose mode is locked out cannot win at any weight, so
            # ageing it only produces an unbounded clock that means nothing.
            starved = (
                demand.acts is not None
                and demand.acts in limits.allowed_modes
                and decision.mode != demand.acts
            )
            trusted = demand.measurement.trustworthy
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
                    untrusted_since=None if trusted else (was.untrusted_since or now),
                    frost=demand.frost,
                    releasing=False,
                    open_since=opened,
                )
            )

    return Plan(
        groups=tuple(decisions),
        rooms=tuple(commands),
        at=now,
        runtime=tuple(next_runtime),
    )
