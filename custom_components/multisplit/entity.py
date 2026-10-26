"""Shared base for every entity this integration publishes."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import MultisplitCoordinator


class MultisplitEntity(CoordinatorEntity[MultisplitCoordinator]):
    """Groups entities under one device per outdoor unit.

    One device per group rather than per room, because the outdoor unit is the
    thing that actually constrains behaviour -- seeing the rooms that share a
    compressor listed together is most of the explanation for why one of them
    is standing by.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: MultisplitCoordinator, group: str) -> None:
        super().__init__(coordinator)
        self._group = group
        name = next(
            (g.name for g in coordinator.config.groups if g.key == group), group
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.entry.entry_id}:{group}")},
            name=name,
            manufacturer="Multi-split climate control",
            model="Outdoor unit",
        )
