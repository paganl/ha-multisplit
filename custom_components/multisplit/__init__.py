"""Multi-split climate control.

Configuration lives in multisplit.yaml in the Home Assistant configuration
directory, generated from questionnaire.yaml. Nothing in this package holds a
threshold, a curve, an entity id or a room name.
"""

from __future__ import annotations

import logging
from pathlib import Path

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_ROOM,
    ATTR_UNTIL,
    CONFIG_FILE,
    DOMAIN,
    PLATFORMS,
    SERVICE_CLEAR_OVERRIDE,
    SERVICE_EXPLAIN,
    SERVICE_RELOAD,
    SERVICE_SET_ROOM_TARGET,
)
from .coordinator import MultisplitCoordinator
from .core.config import ConfigError, SystemConfig
from .yaml_config import load_file

_LOGGER = logging.getLogger(__name__)

SET_ROOM_TARGET_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ROOM): cv.string,
        vol.Required("temperature"): vol.Coerce(float),
        vol.Optional(ATTR_UNTIL): cv.datetime,
    }
)
ROOM_SCHEMA = vol.Schema({vol.Required(ATTR_ROOM): cv.string})
EXPLAIN_SCHEMA = vol.Schema({vol.Optional(ATTR_ROOM): cv.string})


def _config_path(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(CONFIG_FILE))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Read the configuration, start the coordinator, publish the entities."""
    path = _config_path(hass)

    try:
        config: SystemConfig = await hass.async_add_executor_job(load_file, path)
    except FileNotFoundError as error:
        raise ConfigEntryNotReady(
            f"{CONFIG_FILE} is not in the configuration directory. Copy it there, "
            "or generate it from questionnaire.yaml."
        ) from error
    except ConfigError as error:
        # Not ConfigEntryNotReady: retrying will not fix a typo. Surface the
        # whole list so it can be fixed in one pass.
        raise HomeAssistantError(f"{CONFIG_FILE} is not valid:\n{error}") from error

    coordinator = MultisplitCoordinator(hass, entry, config)
    await coordinator.async_config_entry_first_refresh()
    coordinator.async_watch_sources()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    _async_register_services(hass)

    if not config.operation.apply_to_hardware:
        _LOGGER.warning(
            "multisplit: shadow mode — the plan is published but no unit will be "
            "commanded. Set operation.apply_to_hardware to true when the "
            "divergences from the existing automations have been reviewed."
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            for service in (
                SERVICE_RELOAD,
                SERVICE_EXPLAIN,
                SERVICE_SET_ROOM_TARGET,
                SERVICE_CLEAR_OVERRIDE,
            ):
                hass.services.async_remove(DOMAIN, service)
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _coordinators(hass: HomeAssistant) -> list[MultisplitCoordinator]:
    return list(hass.data.get(DOMAIN, {}).values())


def _resolve(hass: HomeAssistant, room: str) -> MultisplitCoordinator:
    for coordinator in _coordinators(hass):
        if any(spec.key == room for spec in coordinator.config.rooms):
            return coordinator
    known = sorted(
        spec.key for coordinator in _coordinators(hass) for spec in coordinator.config.rooms
    )
    raise HomeAssistantError(f"unknown room {room!r}; configured rooms are {known}")


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_RELOAD):
        return

    async def reload(call: ServiceCall) -> None:
        """Re-read multisplit.yaml without restarting Home Assistant."""
        for entry in hass.config_entries.async_entries(DOMAIN):
            await hass.config_entries.async_reload(entry.entry_id)

    async def explain(call: ServiceCall) -> ServiceResponse:
        room = call.data.get(ATTR_ROOM)
        if room:
            return _resolve(hass, room).explain(room)
        merged: dict = {"groups": {}, "rooms": {}, "pending_writes": [], "last_actions": []}
        for coordinator in _coordinators(hass):
            report = coordinator.explain()
            merged["at"] = report.get("at")
            merged["apply_to_hardware"] = report.get("apply_to_hardware")
            merged["groups"].update(report.get("groups", {}))
            merged["rooms"].update(report.get("rooms", {}))
            merged["pending_writes"].extend(report.get("pending_writes", []))
            merged["last_actions"].extend(report.get("last_actions", []))
        return merged

    async def set_room_target(call: ServiceCall) -> None:
        room = call.data[ATTR_ROOM]
        await _resolve(hass, room).async_set_override(
            room, call.data["temperature"], call.data.get(ATTR_UNTIL)
        )

    async def clear_override(call: ServiceCall) -> None:
        room = call.data[ATTR_ROOM]
        await _resolve(hass, room).async_clear_override(room)

    hass.services.async_register(DOMAIN, SERVICE_RELOAD, reload)
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXPLAIN,
        explain,
        schema=EXPLAIN_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_ROOM_TARGET, set_room_target, schema=SET_ROOM_TARGET_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_OVERRIDE, clear_override, schema=ROOM_SCHEMA
    )
