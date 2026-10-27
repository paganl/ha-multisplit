"""Shared bases for every entity this integration publishes.

Two kinds of device, because there are two kinds of thing. An outdoor unit is
a real object with a mode and a reason; a room is a real object with a
temperature and an indoor unit. Modelling only the first made every room
entity a child of its compressor, which is how `climate.office` came out as
`climate.group_b_office` -- a room named after the machine it happens to share.

Rooms are linked to their outdoor unit with ``via_device`` instead. Home
Assistant then shows the rooms nested under the unit they share, which is the
relationship that actually constrains them, without putting the unit's name
into every room's id.
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import MultisplitCoordinator

MANUFACTURER = "Multi-split climate control"


class MultisplitEntity(CoordinatorEntity[MultisplitCoordinator]):
    """An entity belonging to one outdoor unit."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: MultisplitCoordinator, group: str) -> None:
        super().__init__(coordinator)
        self._group = group
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._group_id(coordinator, group))},
            name=self._group_name(coordinator, group),
            manufacturer=MANUFACTURER,
            model="Outdoor unit",
        )

    @staticmethod
    def _group_id(coordinator: MultisplitCoordinator, group: str) -> str:
        return f"{coordinator.entry.entry_id}:{group}"

    @staticmethod
    def _group_name(coordinator: MultisplitCoordinator, group: str) -> str:
        return next(
            (spec.name for spec in coordinator.config.groups if spec.key == group), group
        )


class MultisplitRoomEntity(MultisplitEntity):
    """An entity belonging to one room, shown under its outdoor unit."""

    def __init__(self, coordinator: MultisplitCoordinator, room: str) -> None:
        spec = next(r for r in coordinator.config.rooms if r.key == room)
        super().__init__(coordinator, spec.group)
        self._room = room
        self._spec = spec
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.entry.entry_id}:room:{room}")},
            name=spec.name,
            manufacturer=MANUFACTURER,
            model="Indoor unit",
            via_device=(DOMAIN, self._group_id(coordinator, spec.group)),
        )
