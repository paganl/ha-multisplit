"""Status sensors: the system's account of itself.

Every reason string here was produced by the code that made the decision, not
reconstructed afterwards. That is the whole point of the design: nothing
downstream has to infer why something happened.
"""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import MultisplitCoordinator
from .entity import MultisplitEntity, MultisplitRoomEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: MultisplitCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = [
        GroupModeSensor(coordinator, group.key) for group in coordinator.config.groups
    ]
    entities += [GroupDivergenceSensor(coordinator, group.key) for group in coordinator.config.groups]
    for room in coordinator.config.rooms:
        entities.append(RoomStatusSensor(coordinator, room.key))
        entities.append(RoomOvershootSensor(coordinator, room.key))
    async_add_entities(entities)


class GroupModeSensor(MultisplitEntity, SensorEntity):
    """What the outdoor unit is doing, and why it chose that."""

    _attr_icon = "mdi:hvac"
    _attr_translation_key = "group_mode"

    # Scores and reasons change on almost every cycle. Recording them would
    # write a database row per tick and, past 16 kB of attributes, Home
    # Assistant stops storing them at all and warns about it. The state is
    # worth keeping history for; the commentary is not.
    _unrecorded_attributes = frozenset(
        {"scores", "reason", "policy_reasons", "unserved", "rooms", "allowed_modes"}
    )

    def __init__(self, coordinator: MultisplitCoordinator, group: str) -> None:
        super().__init__(coordinator, group)
        self._attr_unique_id = f"{coordinator.entry.entry_id}:{group}:mode"
        self._attr_name = "Mode"

    @property
    def native_value(self) -> str:
        plan = self.coordinator.data
        if plan is None:
            return "unknown"
        return plan.group(self._group).mode or "off"

    @property
    def extra_state_attributes(self) -> dict:
        plan = self.coordinator.data
        if plan is None:
            return {}
        decision = plan.group(self._group)
        limits = self.coordinator.constraints_for_group(self._group)
        return {
            "reason": decision.because,
            "scores": {mode: round(score, 3) for mode, score in decision.scores.items()},
            "unserved": list(decision.unserved),
            "allowed_modes": sorted(limits.allowed_modes),
            "policy_reasons": dict(limits.reasons),
            "rooms": [r.key for r in self.coordinator.config.rooms_in(self._group)],
        }


class RoomStatusSensor(MultisplitRoomEntity, SensorEntity):
    """What one room's unit is doing, what it wanted, and why."""

    _attr_icon = "mdi:thermometer"
    _attr_translation_key = "room_status"

    #: ``measured`` moves continuously and ``reason`` is prose, so recording
    #: either turns a status sensor that changes a few times an hour into one
    #: database row per tick. The overshoot sensor is the one built for
    #: history.
    _unrecorded_attributes = frozenset(
        {
            "reason",
            "measured",
            "measured_from",
            "stale",
            "target",
            "heat_to",
            "cool_to",
            "wants",
            "unserved_for_minutes",
            "override_until",
            "climate_entity",
            "group",
            "intent_source",
            "participating",
        }
    )

    def __init__(self, coordinator: MultisplitCoordinator, room: str) -> None:
        super().__init__(coordinator, room)
        self._attr_unique_id = f"{coordinator.entry.entry_id}:{room}:status"
        self._attr_name = "Status"

    @property
    def native_value(self) -> str:
        plan = self.coordinator.data
        if self.coordinator.is_idle_excluded(self._room):
            return "off"
        if plan is None:
            return "unknown"
        command = plan.room(self._room)
        if command.mode:
            return "heating" if command.mode == "heat" else "cooling"
        if command.window_open:
            return "window open"
        if "not guessing" in command.because:
            return "no data"
        if command.wants is not None:
            return "waiting"
        return "standby"

    @property
    def extra_state_attributes(self) -> dict:
        plan = self.coordinator.data
        if self.coordinator.is_idle_excluded(self._room):
            return {"reason": "excluded from control at this room's request"}
        if plan is None:
            return {}
        command = plan.room(self._room)
        measurement = self.coordinator.measurement_for(self._room)
        override = self.coordinator.overrides.get(self._room)
        return {
            "reason": command.because,
            "wants": command.wants,
            "participating": command.mode is not None,
            "target": command.target,
            "heat_to": command.heat_to,
            "cool_to": command.cool_to,
            "measured": measurement.temperature if measurement else None,
            "measured_from": measurement.source if measurement else None,
            "stale": measurement.stale if measurement else None,
            "unserved_for_minutes": round(command.unserved_for.total_seconds() / 60, 1),
            "intent_source": (
                intent.source
                if (intent := self.coordinator.intents.get(self._room))
                else None
            ),
            "override_until": override.expires_at.isoformat()
            if override and override.expires_at
            else None,
            "group": self._spec.group,
            "climate_entity": self._spec.climate_entity,
            "frost_protection": command.frost,
            "window_open": command.window_open,
        }


class RoomOvershootSensor(MultisplitRoomEntity, SensorEntity):
    """How far a room is from its target, signed.

    A plain number with a measurement state class, so Home Assistant's own
    recorder keeps long-term statistics for it without any further work. That
    is the cheapest possible route to refitting the cut-off curve: this
    against the outdoor temperature, over a season, in the history UI.
    """

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = True

    def __init__(self, coordinator: MultisplitCoordinator, room: str) -> None:
        super().__init__(coordinator, room)
        self._attr_unique_id = f"{coordinator.entry.entry_id}:{room}:overshoot"
        self._attr_name = "Overshoot"

    @property
    def native_value(self) -> float | None:
        plan = self.coordinator.data
        if plan is None or self.coordinator.is_idle_excluded(self._room):
            return None
        measurement = self.coordinator.measurement_for(self._room)
        if measurement is None or measurement.temperature is None:
            return None
        return round(measurement.temperature - plan.room(self._room).target, 2)


class GroupDivergenceSensor(MultisplitEntity, SensorEntity):
    """How many of this group's units are not set the way the plan says.

    In shadow mode this is the whole point: it counts the rooms where this
    planner disagrees with whatever is currently running the house. A number
    that settles near zero means the two agree; one that does not is either a
    bug here or a behaviour of the old system nobody wrote down.

    Once writes are enabled it should sit at zero and only blip when something
    is changed at the wall.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:not-equal-variant"

    #: The count is worth history. The lists behind it are not, and they are
    #: the part that could grow.
    _unrecorded_attributes = frozenset(
        {
            "differences",
            "last_actions",
            "telemetry_records",
            "telemetry_queued",
            "telemetry_dropped",
            "telemetry_error",
        }
    )

    def __init__(self, coordinator: MultisplitCoordinator, group: str) -> None:
        super().__init__(coordinator, group)
        self._attr_unique_id = f"{coordinator.entry.entry_id}:{group}:divergence"
        self._attr_name = "Divergence"

    def _lines(self) -> list[str]:
        rooms = {room.key for room in self.coordinator.config.rooms_in(self._group)}
        return [
            line for line in self.coordinator.pending if line.split(":", 1)[0] in rooms
        ]

    @property
    def native_value(self) -> int:
        return len(self._lines())

    @property
    def extra_state_attributes(self) -> dict:
        telemetry = self.coordinator.telemetry_health
        return {
            "differences": self._lines(),
            "applying": self.coordinator.config.operation.apply_to_hardware,
            "last_actions": list(self.coordinator.actions),
            "telemetry_records": telemetry["records_written"],
            "telemetry_queued": telemetry["queued"],
            "telemetry_dropped": telemetry["dropped"],
            "telemetry_error": telemetry["last_error"],
        }
