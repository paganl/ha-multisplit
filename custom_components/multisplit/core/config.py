"""Turning the YAML into the model.

Takes a plain mapping -- already parsed, so this module imports no YAML
library and no Home Assistant, and stays as testable as the planner. The
integration reads the file at the edge and hands the dict in here.

Two rules shape this module:

* **A missing key is an error.** No key falls back to a value defined in
  Python, because a hidden default is indistinguishable from a hard-coded
  value: you cannot see it, you cannot change it from the config, and it turns
  up as mysterious behaviour later. The configuration is complete or it is
  rejected.
* **Problems are collected, not raised one at a time.** Someone editing YAML
  wants the whole list, not a round trip per typo.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta

from .model import Curve, ParticipationPolicy
from .policy import PolicyConfig, participation_problems


class ConfigError(ValueError):
    """Raised with every problem found, not just the first."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        joined = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(f"{len(self.problems)} problem(s) in the configuration:\n{joined}")


@dataclass(frozen=True)
class RoomSpec:
    """One room: where it is, what it talks to, how its unit behaves."""

    key: str
    name: str
    group: str
    climate_entity: str
    temperature_sensors: tuple[str, ...]
    participation: ParticipationPolicy
    priority: float
    schedule_entity: str | None


@dataclass(frozen=True)
class GroupSpec:
    key: str
    name: str
    policy: PolicyConfig


@dataclass(frozen=True)
class SystemConfig:
    outdoor_sensor: str
    groups: tuple[GroupSpec, ...]
    rooms: tuple[RoomSpec, ...]

    def group(self, key: str) -> GroupSpec:
        for spec in self.groups:
            if spec.key == key:
                return spec
        raise KeyError(key)

    def rooms_in(self, group: str) -> tuple[RoomSpec, ...]:
        return tuple(r for r in self.rooms if r.group == group)

    @property
    def policies(self) -> dict[str, PolicyConfig]:
        return {g.key: g.policy for g in self.groups}


# --------------------------------------------------------------------------
# Readers. Each appends to `problems` rather than raising, so one pass over a
# broken file reports everything wrong with it.
# --------------------------------------------------------------------------


class _Reader:
    def __init__(self) -> None:
        self.problems: list[str] = []

    def fail(self, where: str, message: str) -> None:
        self.problems.append(f"{where}: {message}")

    def mapping(self, raw: object, where: str) -> Mapping:
        if not isinstance(raw, Mapping):
            self.fail(where, f"expected a mapping, found {type(raw).__name__}")
            return {}
        return raw

    def number(self, raw: Mapping, key: str, where: str) -> float:
        if key not in raw:
            self.fail(where, f"missing required key '{key}'")
            return 0.0
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.fail(where, f"'{key}' must be a number, found {value!r}")
            return 0.0
        return float(value)

    def optional_number(self, raw: Mapping, key: str, where: str) -> float | None:
        if key not in raw:
            self.fail(where, f"missing required key '{key}' (use null to disable)")
            return None
        if raw[key] is None:
            return None
        return self.number(raw, key, where)

    def text(self, raw: Mapping, key: str, where: str) -> str:
        if key not in raw:
            self.fail(where, f"missing required key '{key}'")
            return ""
        value = raw[key]
        if not isinstance(value, str) or not value.strip():
            self.fail(where, f"'{key}' must be a non-empty string, found {value!r}")
            return ""
        return value.strip()

    def entity_id(self, raw: Mapping, key: str, where: str, domain: str) -> str:
        value = self.text(raw, key, where)
        if value and (value.count(".") != 1 or not value.startswith(f"{domain}.")):
            self.fail(where, f"'{key}' should look like {domain}.something, found {value!r}")
        return value

    def minutes(self, raw: Mapping, key: str, where: str) -> timedelta:
        value = self.number(raw, key, where)
        if value < 0:
            self.fail(where, f"'{key}' cannot be negative")
            return timedelta(0)
        return timedelta(minutes=value)

    def curve(self, raw: Mapping, key: str, where: str, *, scale: float = 1.0) -> Curve:
        """A curve is a list of [outdoor_temperature, value] pairs."""
        if key not in raw:
            self.fail(where, f"missing required curve '{key}'")
            return Curve.constant(0.0)
        points = raw[key]
        if not isinstance(points, Sequence) or isinstance(points, str) or not points:
            self.fail(where, f"'{key}' must be a non-empty list of [outdoor, value] pairs")
            return Curve.constant(0.0)

        parsed: list[tuple[float, float]] = []
        for index, point in enumerate(points):
            label = f"{where}.{key}[{index}]"
            if (
                not isinstance(point, Sequence)
                or isinstance(point, str)
                or len(point) != 2
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in point)
            ):
                self.fail(label, f"expected [outdoor, value], found {point!r}")
                continue
            parsed.append((float(point[0]), float(point[1]) * scale))

        if not parsed:
            return Curve.constant(0.0)
        try:
            return Curve(tuple(parsed))
        except ValueError as error:
            self.fail(f"{where}.{key}", str(error))
            return Curve.constant(0.0)


def _policy(reader: _Reader, raw: Mapping, where: str) -> PolicyConfig:
    season = reader.text(raw, "season", where)
    if season and season not in ("heat", "cool", "auto"):
        reader.fail(where, f"'season' must be heat, cool or auto, found {season!r}")
        season = "auto"

    span = raw.get("setpoint_range")
    low, high = 15.0, 26.0
    if (
        not isinstance(span, Sequence)
        or isinstance(span, str)
        or len(span) != 2
        or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in span)
    ):
        reader.fail(where, f"'setpoint_range' must be [min, max], found {span!r}")
    else:
        low, high = float(span[0]), float(span[1])
        if low >= high:
            reader.fail(where, f"'setpoint_range' min {low:g} is not below max {high:g}")

    return PolicyConfig(
        cool_below=reader.optional_number(raw, "cool_below", where),
        heat_above=reader.optional_number(raw, "heat_above", where),
        lockout_hysteresis=reader.number(raw, "lockout_hysteresis", where),
        season=season or "auto",
        setpoint_range=(low, high),
        deadband=reader.number(raw, "deadband", where),
        overshoot_tolerance=reader.number(raw, "overshoot_tolerance", where),
        inertia_weight=reader.number(raw, "inertia_weight", where),
        inertia_half_life=reader.minutes(raw, "inertia_half_life_minutes", where),
        aging_rate=reader.number(raw, "aging_rate", where),
    )


def _participation(reader: _Reader, raw: Mapping, where: str) -> ParticipationPolicy:
    return ParticipationPolicy(
        cut_off_offset=reader.curve(raw, "cut_off_offset", where),
        min_off=reader.curve(raw, "rest_minutes", where, scale=60.0),
        min_on=reader.minutes(raw, "min_on_minutes", where),
        resume_deadband=reader.number(raw, "resume_deadband", where),
    )


def load(raw: object) -> SystemConfig:
    """Build a validated ``SystemConfig``, or raise ``ConfigError``."""
    reader = _Reader()
    root = reader.mapping(raw, "multisplit.yaml")

    outdoor = reader.entity_id(root, "outdoor_sensor", "multisplit.yaml", "sensor")

    defaults = reader.mapping(root.get("defaults"), "defaults")
    default_policy = _policy(reader, reader.mapping(defaults.get("policy"), "defaults.policy"), "defaults.policy")
    default_participation = _participation(
        reader,
        reader.mapping(defaults.get("participation"), "defaults.participation"),
        "defaults.participation",
    )

    raw_groups = reader.mapping(root.get("groups"), "groups")
    if not raw_groups:
        reader.fail("groups", "at least one group is required")

    groups: list[GroupSpec] = []
    for key in raw_groups:
        where = f"groups.{key}"
        body = reader.mapping(raw_groups[key], where)
        overrides = body.get("policy")
        policy = (
            _policy(reader, reader.mapping(overrides, f"{where}.policy"), f"{where}.policy")
            if overrides is not None
            else default_policy
        )
        groups.append(GroupSpec(key=key, name=reader.text(body, "name", where), policy=policy))

    raw_rooms = reader.mapping(root.get("rooms"), "rooms")
    if not raw_rooms:
        reader.fail("rooms", "at least one room is required")

    known = {g.key for g in groups}
    rooms: list[RoomSpec] = []
    for key in raw_rooms:
        where = f"rooms.{key}"
        body = reader.mapping(raw_rooms[key], where)

        group = reader.text(body, "group", where)
        if group and group not in known:
            reader.fail(where, f"group {group!r} is not defined in groups: {sorted(known)}")

        sensors = body.get("temperature_sensors", None)
        if sensors is None:
            reader.fail(where, "missing required key 'temperature_sensors' (use [] for none)")
            sensors = []
        elif not isinstance(sensors, Sequence) or isinstance(sensors, str):
            reader.fail(where, f"'temperature_sensors' must be a list, found {sensors!r}")
            sensors = []
        for index, sensor in enumerate(sensors):
            if not isinstance(sensor, str) or not sensor.startswith("sensor."):
                reader.fail(f"{where}.temperature_sensors[{index}]", f"expected sensor.*, found {sensor!r}")

        overrides = body.get("participation")
        participation = (
            _participation(
                reader,
                {**reader.mapping(defaults.get("participation"), "defaults.participation"),
                 **reader.mapping(overrides, f"{where}.participation")},
                f"{where}.participation",
            )
            if overrides is not None
            else default_participation
        )

        priority = float(body.get("priority", 1.0) or 1.0)
        if priority <= 0:
            reader.fail(where, f"'priority' must be positive, found {priority:g}")

        schedule = body.get("schedule_entity")
        if schedule is not None and (not isinstance(schedule, str) or not schedule.startswith("schedule.")):
            reader.fail(where, f"'schedule_entity' should look like schedule.something, found {schedule!r}")
            schedule = None

        rooms.append(
            RoomSpec(
                key=key,
                name=reader.text(body, "name", where),
                group=group,
                climate_entity=reader.entity_id(body, "climate_entity", where, "climate"),
                temperature_sensors=tuple(s for s in sensors if isinstance(s, str)),
                participation=participation,
                priority=priority,
                schedule_entity=schedule,
            )
        )

    # The cross-cutting check: a room's tolerated overshoot must not read as
    # demand for the opposite mode under its own group's policy.
    by_group = {g.key: g for g in groups}
    for room in rooms:
        group = by_group.get(room.group)
        if group is None:
            continue
        from .policy import constraints_for

        limits = constraints_for(group.policy, outdoor=None)
        for problem in participation_problems(room.participation, limits):
            reader.fail(f"rooms.{room.key}", problem)

    for group in groups:
        if not any(r.group == group.key for r in rooms):
            reader.fail(f"groups.{group.key}", "no rooms are assigned to this group")

    if reader.problems:
        raise ConfigError(reader.problems)

    return SystemConfig(
        outdoor_sensor=outdoor,
        groups=tuple(groups),
        rooms=tuple(rooms),
    )
