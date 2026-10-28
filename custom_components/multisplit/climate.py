"""One climate entity per room — the thermostat a person actually touches.

A deliberate departure from a conventional climate entity, and from an earlier
draft of DESIGN-v3.md §7, which proposed restricting ``hvac_modes`` to the
modes the group currently permits. The modes offered here are only **auto** and
**off**, because offering heat or cool at all is the lie:

* A room cannot choose heating while the outdoor unit is cooling, so a mode
  picker that offers it is wrong at the moment it is drawn.
* Restricting the list to what is possible *right now* is better, but it still
  invites someone to set a mode and then watch it be overruled a minute later
  when another room's demand changes.

So the room expresses a target and whether it wants to take part at all, and
the system decides the mode. ``hvac_action`` reports what is actually
happening, and the status sensor carries the reason. Writes go to intent --
this entity never commands a unit.
"""

from __future__ import annotations

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import MultisplitCoordinator
from .core.model import HEAT
from .entity import MultisplitRoomEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: MultisplitCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(RoomClimate(coordinator, room.key) for room in coordinator.config.rooms)


class RoomClimate(MultisplitRoomEntity, ClimateEntity):
    """A room's comfort intent, not a unit's control panel."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.AUTO, HVACMode.OFF]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_target_temperature_step = 0.5
    _unrecorded_attributes = frozenset(
        {"reason", "wants", "heat_to", "cool_to", "group", "group_mode", "group_reason"}
    )

    def __init__(self, coordinator: MultisplitCoordinator, room: str) -> None:
        super().__init__(coordinator, room)
        self._attr_unique_id = f"{coordinator.entry.entry_id}:{room}:climate"
        # None means "the device's own name", so this is climate.<room> rather
        # than climate.<room>_<room>.
        self._attr_name = None
        spec = self._spec
        low, high = coordinator.config.group(spec.group).policy.setpoint_range
        self._attr_min_temp = low
        self._attr_max_temp = high

    # -- what it reads -------------------------------------------------

    @property
    def current_temperature(self) -> float | None:
        measurement = self.coordinator.measurement_for(self._room)
        return measurement.temperature if measurement else None

    @property
    def target_temperature(self) -> float | None:
        plan = self.coordinator.data
        if self.coordinator.is_excluded(self._room) or plan is None:
            override = self.coordinator.overrides.get(self._room)
            return override.target if override else None
        return plan.room(self._room).target

    @property
    def hvac_mode(self) -> HVACMode:
        return HVACMode.OFF if self.coordinator.is_excluded(self._room) else HVACMode.AUTO

    @property
    def hvac_action(self) -> HVACAction:
        if self.coordinator.is_excluded(self._room):
            return HVACAction.OFF
        plan = self.coordinator.data
        if plan is None:
            return HVACAction.IDLE
        command = plan.room(self._room)
        if command.mode is None:
            return HVACAction.IDLE
        return HVACAction.HEATING if command.mode == HEAT else HVACAction.COOLING

    @property
    def extra_state_attributes(self) -> dict:
        plan = self.coordinator.data
        if plan is None or self.coordinator.is_excluded(self._room):
            return {"reason": "excluded from control at this room's request"}
        command = plan.room(self._room)
        return {
            "reason": command.because,
            "wants": command.wants,
            "heat_to": command.heat_to,
            "cool_to": command.cool_to,
            "group": self._spec.group,
            "group_mode": plan.group(self._spec.group).mode or "off",
            "group_reason": plan.group(self._spec.group).because,
        }

    # -- what it writes: intent, never hardware ------------------------

    async def async_set_temperature(self, **kwargs) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        await self.coordinator.async_set_override(self._room, float(temperature), None)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self.coordinator.async_exclude(self._room)
        else:
            await self.coordinator.async_include(self._room)

    async def async_turn_off(self) -> None:
        await self.coordinator.async_exclude(self._room)

    async def async_turn_on(self) -> None:
        await self.coordinator.async_include(self._room)
