"""Whether each mode is permitted right now, and why not.

These exist so that "nothing is happening" is never a mystery: a card, an
automation or a person can read the reason without needing to know what the
lockout thresholds are.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import MultisplitCoordinator
from .core.model import COOL, HEAT
from .entity import MultisplitEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: MultisplitCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        ModeAllowedSensor(coordinator, group.key, mode)
        for group in coordinator.config.groups
        for mode in (HEAT, COOL)
    )


class ModeAllowedSensor(MultisplitEntity, BinarySensorEntity):
    """Is this mode available on this outdoor unit at the moment?"""

    _unrecorded_attributes = frozenset({"reason", "outdoor_unknown"})

    def __init__(self, coordinator: MultisplitCoordinator, group: str, mode: str) -> None:
        super().__init__(coordinator, group)
        self._mode = mode
        self._attr_unique_id = f"{coordinator.entry.entry_id}:{group}:{mode}_allowed"
        self._attr_name = f"{mode.capitalize()} allowed"
        self._attr_icon = "mdi:fire" if mode == HEAT else "mdi:snowflake"

    @property
    def is_on(self) -> bool:
        return self._mode in self.coordinator.constraints_for_group(self._group).allowed_modes

    @property
    def extra_state_attributes(self) -> dict:
        limits = self.coordinator.constraints_for_group(self._group)
        reason = limits.reasons.get(self._mode)
        if reason is None and self._mode in limits.allowed_modes:
            reason = f"{self._mode} is permitted"
        return {
            "reason": reason or f"{self._mode} is not permitted",
            "outdoor_unknown": "outdoor" in limits.reasons,
        }
