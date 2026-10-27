"""Names shared across the integration. No tunable values live here."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "multisplit"

CONFIG_FILE = "multisplit.yaml"
QUESTIONNAIRE_FILE = "questionnaire.yaml"

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.CLIMATE, Platform.SENSOR]

SERVICE_RELOAD = "reload"
SERVICE_EXPLAIN = "explain"
SERVICE_SET_ROOM_TARGET = "set_room_target"
SERVICE_CLEAR_OVERRIDE = "clear_override"

ATTR_ROOM = "room"
ATTR_UNTIL = "until"

MANUFACTURER = "Multi-split climate control"


def group_device_id(entry_id: str, group: str) -> str:
    return f"{entry_id}:{group}"


def room_device_id(entry_id: str, room: str) -> str:
    return f"{entry_id}:room:{room}"
