"""Away: one switch for the whole house.

While it is on, every room aims for the away setback instead of its
schedule -- except a room someone has just turned up by hand, because a
person home early should not be left in a cold room. Switching it either way
clears manual overrides, so a tweak from this morning does not hold one room
warm all the time the house is empty.

Flip it from the dashboard, or from a presence automation. Its state is kept
across restarts.
"""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import MultisplitCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: MultisplitCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([AwaySwitch(coordinator)])


class AwaySwitch(CoordinatorEntity[MultisplitCoordinator], SwitchEntity):
    """The house is empty: every room to its setback."""

    _attr_icon = "mdi:home-export-outline"
    _attr_name = "Multi-split away"

    def __init__(self, coordinator: MultisplitCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}:away"

    @property
    def is_on(self) -> bool:
        return self.coordinator.away

    @property
    def extra_state_attributes(self) -> dict:
        return {"away_target": self.coordinator.config.intent.away_target}

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_away(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_away(False)
