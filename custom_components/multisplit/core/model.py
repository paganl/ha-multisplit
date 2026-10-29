"""Data model for multi-split climate control.

This module imports nothing from Home Assistant, and nothing in it performs
I/O. That is deliberate: together with ``planner.py`` it forms a decision
layer that can be unit-tested in milliseconds, including the scenarios that
are painful or slow to reproduce in a real building.

Every dataclass here is frozen. State transitions are expressed by building a
new value, never by mutating an old one, so a plan can be logged, replayed and
compared without any risk that it changed underneath you.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

HEAT = "heat"
COOL = "cool"
MODES = (HEAT, COOL)


def opposite(mode: str) -> str:
    """Return the other mode."""
    return COOL if mode == HEAT else HEAT


# --------------------------------------------------------------------------
# Curves
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Curve:
    """A piecewise-linear function of outdoor temperature.

    Breakpoints rather than a formula, because these numbers are *measured*,
    not derived: how far a given room overshoots depends on its indoor unit's
    minimum capacity relative to the room's heat loss, which nobody has on a
    datasheet. A table also degrades usefully -- when one room misbehaves you
    change one row, instead of re-deriving a function that has to fit every
    room at once.

    Outside the range of the breakpoints the end values are held flat
    (clamped), never extrapolated. Extrapolating a measured curve into weather
    you have never observed is how you discover a bug in February.
    """

    points: tuple[tuple[float, float], ...]

    def __post_init__(self) -> None:
        if not self.points:
            raise ValueError("a curve needs at least one point")
        xs = [x for x, _ in self.points]
        if xs != sorted(xs):
            raise ValueError("curve points must be sorted by x ascending")
        if len(set(xs)) != len(xs):
            raise ValueError("curve points must have distinct x values")

    @classmethod
    def constant(cls, value: float) -> Curve:
        """A curve that ignores its input. Useful as a starting point."""
        return cls(((0.0, value),))

    def at(self, x: float | None) -> float:
        """Evaluate the curve, clamping outside the measured range.

        ``None`` means the outdoor temperature is unknown. The most
        conservative answer is the coldest breakpoint: in heating that is the
        smallest cut-off offset and the shortest rest, so an unknown outdoor
        temperature makes the system cautious rather than confident.
        """
        if x is None:
            return self.points[0][1]
        if x <= self.points[0][0]:
            return self.points[0][1]
        if x >= self.points[-1][0]:
            return self.points[-1][1]
        for (x0, y0), (x1, y1) in zip(self.points, self.points[1:]):
            if x0 <= x <= x1:
                span = x1 - x0
                return y0 + (y1 - y0) * ((x - x0) / span)
        raise AssertionError("unreachable: x is within range but matched no segment")


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RoomMeasurement:
    """What we believe the room's temperature to be, and how much we trust it."""

    room: str
    temperature: float | None
    stale: bool = False
    source: str = "unknown"

    @property
    def trustworthy(self) -> bool:
        return self.temperature is not None and not self.stale


@dataclass(frozen=True)
class RoomIntent:
    """What the room should be, and why. Never how to achieve it.

    ``target`` is the temperature to heat *to*. ``band`` is how far above it
    the room is content to sit before it would rather be cooled, so the room
    is satisfied anywhere in ``[target, target + band]`` and the pair is a
    comfort band: "heat below 20, cool above 23, do not fuss in between".

    This is the honest version of a unit's own dual setpoint. An indoor unit
    set to a band decides heat or cool for itself, from its own room, with no
    sight of the others sharing the compressor -- which on a multi-split is a
    decision it has no business taking. Expressed here, the same intent is
    arbitrated by the one component that can see the whole group.

    A band of zero is a single setpoint, and behaves exactly as before.
    """

    room: str
    target: float
    band: float = 0.0
    source: str = "default"  # schedule | override | away | default
    expires_at: datetime | None = None
    priority: float = 1.0

    @property
    def heat_to(self) -> float:
        return self.target

    @property
    def cool_to(self) -> float:
        return self.target + max(0.0, self.band)

    def target_for(self, mode: str | None) -> float:
        """The setpoint to command for a given mode.

        Heating aims at the bottom of the band and cooling at the top, so
        neither drives the room further into the band than asked.
        """
        return self.cool_to if mode == COOL else self.heat_to


@dataclass(frozen=True)
class ParticipationPolicy:
    """When a room's indoor unit should run, given that it cannot run gently.

    An indoor unit on a multi-split cannot modulate below its minimum
    capacity, and compressor speed is set by whichever room is working
    hardest. A nearly-satisfied room therefore receives output sized for a
    different room's demand, and overshoots.

    The overshoot is worst in mild weather, when minimum capacity is a large
    fraction of actual demand -- so the cut-off is a function of outdoor
    temperature, not a constant.

    ``cut_off_offset`` may be **negative**: in mild weather the correct action
    is to stop *before* the room reaches target, because minimum capacity plus
    the residual heat in the coil will carry it past anyway. A fixed threshold
    cannot express that.
    """

    cut_off_offset: Curve  # outdoor temp -> degrees past target at which to stop
    min_off: Curve  # outdoor temp -> seconds of rest before restarting
    min_on: timedelta = timedelta(minutes=5)
    resume_deadband: float | None = None  # None = use the group's deadband

    def cut_off(self, outdoor: float | None) -> float:
        return self.cut_off_offset.at(outdoor)

    def rest(self, outdoor: float | None) -> timedelta:
        return timedelta(seconds=max(0.0, self.min_off.at(outdoor)))


@dataclass(frozen=True)
class RoomConfig:
    """Static facts about a room: where it is, and how its unit behaves."""

    room: str
    group: str
    participation: ParticipationPolicy


@dataclass(frozen=True)
class RoomRuntime:
    """What we remember about a room between cycles.

    This is the whole of the planner's memory about a room. It is passed *in*,
    which is what keeps the planner a pure function despite its behaviour
    depending on time.
    """

    room: str
    running: bool = False
    mode: str | None = None  # the mode it is running in, None when standing by
    since: datetime | None = None  # last participation transition
    unserved_since: datetime | None = None  # when demand first went unserved
    untrusted_since: datetime | None = None  # when its reading stopped being trustworthy

    def elapsed(self, now: datetime) -> timedelta:
        if self.since is None:
            return timedelta.max
        return now - self.since

    def unserved_for(self, now: datetime) -> timedelta:
        if self.unserved_since is None:
            return timedelta(0)
        return now - self.unserved_since

    def untrusted_for(self, now: datetime) -> timedelta:
        """How long the reading has been untrustworthy; zero on the first cycle."""
        if self.untrusted_since is None:
            return timedelta(0)
        return now - self.untrusted_since


@dataclass(frozen=True)
class GroupState:
    """The outdoor unit's current mode, and when it last changed."""

    group: str
    mode: str | None = None
    since: datetime | None = None

    def held_for(self, now: datetime) -> timedelta:
        if self.since is None:
            return timedelta.max
        return now - self.since


@dataclass(frozen=True)
class Constraints:
    """What the system is *permitted* to do, with reasons for what it is not.

    Produced by ``policy.py`` from configuration and outdoor temperature. The
    planner treats this as given and never second-guesses it -- which is what
    keeps "may we cool?" in one place instead of scattered through the
    decision logic.
    """

    allowed_modes: frozenset[str] = frozenset(MODES)
    setpoint_range: tuple[float, float] = (15.0, 26.0)
    deadband: float = 0.3
    overshoot_tolerance: float = 0.8
    """How far past target a room may sit before it counts as wanting the
    opposite mode.

    Overshoot is structural on a multi-split: the indoor unit cannot modulate
    below its minimum capacity, so a nearly-satisfied room is pushed past its
    target by design. If the plain deadband decided demand, that inevitable
    overshoot would be read as a demand for cooling, and the hardware's own
    behaviour would generate changeover pressure every cycle -- the system
    would thrash because of the very effect the participation controller
    exists to absorb.

    So wanting the opposite of what is currently being delivered has to clear
    a wider band. It must be at least the cut-off offset plus the coast that
    follows it, or the loop closes on itself.
    """
    inertia_weight: float = 2.0
    inertia_half_life: timedelta = timedelta(minutes=20)
    aging_rate: float = 0.5  # extra weight per hour a room goes unserved
    stale_grace: timedelta = timedelta(minutes=20)
    """How long a room *already running* keeps its last decision once its
    reading stops being trustworthy, before it fails safe and stops.

    A room that is idle never starts on an untrustworthy reading; this only
    stops a sensor that goes quiet mid-cycle from abandoning a room that is
    still below target. Zero restores the old behaviour: stale means stop.
    """
    solar_bank: float = 0.0
    """How far to move each room's working end into its comfort band, because
    the house is running on its own solar.

    Heat is cheapest to make when it is the house's own surplus, and the
    building stores it: a room heated to the top of its band on solar needs
    less from the grid in the evening. So on solar, heating aims this much
    higher (and cooling this much lower), never past the other end of the
    band. Policy sets it only when exactly one mode is permitted: with both
    open, narrowing the band would invite the heat-against-cool fight the
    band exists to prevent. Zero is the normal case.
    """
    reasons: dict[str, str] = field(default_factory=dict)

    def clamp(self, target: float) -> float:
        low, high = self.setpoint_range
        return min(max(target, low), high)


# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RoomCommand:
    """A complete desired state for one room.

    ``mode`` and ``target`` always travel together. There is no way to express
    "change the mode and leave the setpoint alone", which is why the class of
    defect where a unit ends up in the right mode at the wrong temperature is
    unrepresentable rather than merely fixed.
    """

    room: str
    mode: str | None  # None = stand by
    target: float
    because: str
    wants: str | None = None
    unserved_for: timedelta = timedelta(0)
    heat_to: float | None = None
    cool_to: float | None = None

    @property
    def standing_by(self) -> bool:
        return self.mode is None


@dataclass(frozen=True)
class GroupDecision:
    group: str
    mode: str | None
    because: str
    scores: dict[str, float] = field(default_factory=dict)
    unserved: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plan:
    groups: tuple[GroupDecision, ...]
    rooms: tuple[RoomCommand, ...]
    at: datetime
    runtime: tuple[RoomRuntime, ...] = ()
    """The memory to carry into the next cycle.

    Computed by the planner rather than by the coordinator, so that the state
    machine -- when a room's dwell timer restarts, when a starvation clock
    starts and stops -- is covered by the same pure tests as the decisions
    themselves.
    """

    def room(self, name: str) -> RoomCommand:
        for command in self.rooms:
            if command.room == name:
                return command
        raise KeyError(name)

    def group(self, name: str) -> GroupDecision:
        for decision in self.groups:
            if decision.group == name:
                return decision
        raise KeyError(name)
