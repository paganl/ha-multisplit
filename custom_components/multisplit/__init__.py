"""Multi-split climate control.

Configuration lives in multisplit.yaml in the Home Assistant configuration
directory, generated from questionnaire.yaml. Nothing in this package holds a
threshold, a curve, an entity id or a room name.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import (
    CoreState,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from . import measurement
from .const import (
    ATTR_ROOM,
    ATTR_UNTIL,
    CONFIG_FILE,
    DOMAIN,
    PLATFORMS,
    SERVICE_CLEAR_OVERRIDE,
    SERVICE_EXPLAIN,
    SERVICE_RESUME_SCHEDULE,
    SERVICE_RELOAD,
    SERVICE_SET_AWAY,
    SERVICE_SET_ROOM_TARGET,
    SERVICE_SET_SCHEDULE,
    CARD_FILE,
    CARD_URL,
    MANUFACTURER,
    group_device_id,
    room_device_id,
)
from .coordinator import MultisplitCoordinator
from .core.config import ConfigError, SystemConfig
from .core.schedule import DAYS, Mark, parse_time
from .core.naming import describe_missing
from .yaml_config import load_file

_LOGGER = logging.getLogger(__name__)

#: How long after Home Assistant has started to wait before deciding a
#: configured entity is genuinely absent rather than merely late.
_STARTUP_GRACE = 60.0

SET_ROOM_TARGET_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ROOM): cv.string,
        vol.Required("temperature"): vol.All(vol.Coerce(float), vol.Range(min=0, max=40)),
        vol.Optional(ATTR_UNTIL): cv.datetime,
    }
)
ROOM_SCHEMA = vol.Schema({vol.Required(ATTR_ROOM): cv.string})
SET_AWAY_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ROOM): vol.All(cv.ensure_list, [cv.string]),
        vol.Required("away"): cv.boolean,
        vol.Optional(ATTR_UNTIL): cv.datetime,
    }
)


def _time(value):
    """'07:30', or minutes past midnight -- which is what an unquoted 07:30
    becomes in Home Assistant's YAML (1.1 reads it as base-60: 450)."""
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 1440:
        return f"{value // 60:02d}:{value % 60:02d}"
    try:
        parse_time(value)
    except ValueError as error:
        raise vol.Invalid(str(error)) from error
    return value


#: A temperature that is a number at all. NaN passes every range check by
#: failing every comparison, so it is refused here, before it can be stored.
_TEMPERATURE = vol.All(vol.Coerce(float), vol.Range(min=0, max=40))


SET_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ROOM): cv.string,
        vol.Required("days"): vol.All(cv.ensure_list, [vol.In(DAYS)], vol.Length(min=1)),
        vol.Required("marks"): vol.All(
            cv.ensure_list,
            [
                vol.Schema(
                    {
                        vol.Required("at"): _time,
                        vol.Required("temperature"): _TEMPERATURE,
                    }
                )
            ],
        ),
    }
)
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
        # Log each problem as its own record before raising. A multi-line
        # exception message is the first thing a log viewer collapses, and the
        # list is the only useful part of it -- a count of four with the four
        # lines missing tells nobody anything.
        _LOGGER.error(
            "multisplit: %s is not valid — %d problem(s):", path, len(error.problems)
        )
        for problem in error.problems:
            _LOGGER.error("multisplit:   %s", problem)
        if error.missing:
            _LOGGER.error("multisplit: %s", ConfigError.STALE_HINT)

        # Not ConfigEntryNotReady: retrying will not fix a typo.
        raise HomeAssistantError(f"{CONFIG_FILE} is not valid:\n{error}") from error

    # Keys this file predates, filled with their documented defaults. A
    # warning rather than an error: the file means what it always meant, but
    # it should say so.
    for notice in config.notices:
        _LOGGER.warning("multisplit: %s", notice)

    _async_register_devices(hass, entry, config)

    coordinator = MultisplitCoordinator(hass, entry, config)
    await coordinator.async_load_state()
    await coordinator.async_config_entry_first_refresh()
    coordinator.async_watch_sources()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    _async_register_services(hass)
    await _async_register_card(hass)

    _async_check_entities_later(hass, entry, config)

    if not config.operation.apply_to_hardware:
        _LOGGER.warning(
            "multisplit: shadow mode — the plan is published but no unit will be "
            "commanded. Set operation.apply_to_hardware to true when the "
            "divergences from the existing automations have been reviewed."
        )

    return True


async def _async_register_card(hass: HomeAssistant) -> None:
    """Serve the schedule card and load it on every dashboard, once per run.

    Shipping the card inside the integration is what lets HACS install it:
    HACS treats a repository as one category, so a separate Lovelace card
    would need a repository of its own, and a resource entry by hand.
    """
    if hass.data.get(f"{DOMAIN}_card"):
        return
    from homeassistant.components.frontend import add_extra_js_url
    from homeassistant.components.http import StaticPathConfig

    version = json.loads(
        await hass.async_add_executor_job(
            (Path(__file__).parent / "manifest.json").read_text, "utf-8"
        )
    )["version"]
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(Path(__file__).parent / CARD_FILE), True)]
    )
    # The version busts the browser cache on upgrade; the file itself is
    # served with long-lived cache headers.
    add_extra_js_url(hass, f"{CARD_URL}?v={version}")
    hass.data[f"{DOMAIN}_card"] = True


@callback
def _async_register_devices(
    hass: HomeAssistant, entry: ConfigEntry, config: SystemConfig
) -> None:
    """Create a device per outdoor unit, and one per room linked to it.

    Done here rather than from each entity's ``DeviceInfo`` because linking a
    device to its parent from an entity goes through ``via_device``, which
    Home Assistant has deprecated in favour of ``via_device_id``. The id only
    exists once the parent device has been created, which an entity cannot
    guarantee and setup can.
    """
    registry = dr.async_get(hass)

    for group in config.groups:
        outdoor = registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, group_device_id(entry.entry_id, group.key))},
            name=group.name,
            manufacturer=MANUFACTURER,
            model="Outdoor unit",
        )
        for room in config.rooms_in(group.key):
            registry.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, room_device_id(entry.entry_id, room.key))},
                name=room.name,
                manufacturer=MANUFACTURER,
                model="Indoor unit",
                via_device_id=outdoor.id,
            )


@callback
def _async_check_entities_later(
    hass: HomeAssistant, entry: ConfigEntry, config: SystemConfig
) -> None:
    """Warn about configured entities that do not exist -- once it is fair to.

    Not at setup. Home Assistant does not order integration startup, so at the
    moment this one comes up the integration that provides the climate
    entities may not have registered any of them yet. Checking then reported
    every room as missing, which is alarming, wrong, and worse than silence:
    it teaches you to ignore the warning that will one day be right.

    So the check waits for Home Assistant to finish starting, and then for a
    grace period derived from the tick -- long enough for a slow integration,
    short enough that a genuine typo is reported while you are still looking.

    Nothing depends on this check. ``measurement.collect`` re-reads every
    entity on every cycle, so a room whose entity appears late starts working
    on its own; this only explains one that never does.
    """
    grace = max(_STARTUP_GRACE, 2 * config.operation.tick.total_seconds())

    async def _check(_now=None) -> None:
        for entity_id, why in measurement.uncommandable_entities(hass, config).items():
            _LOGGER.warning("multisplit: %s — %s", entity_id, why)

        missing = measurement.unavailable_entities(hass, config)
        if not missing:
            return
        # The suggestion is only worth offering once the state machine is
        # populated -- with an empty climate domain there is nothing to
        # compare against, which is exactly what happened when this ran too
        # early.
        described = describe_missing(missing, hass.states.async_entity_ids())
        _LOGGER.warning(
            "multisplit: %d configured %s not exist; a room missing its "
            "temperature has no reading, and one missing its window sensor is "
            "treated as closed: %s",
            len(missing),
            "entity does" if len(missing) == 1 else "entities do",
            "; ".join(described),
        )

    if hass.state is CoreState.running:
        entry.async_on_unload(async_call_later(hass, grace, _check))
        return

    started = False

    @callback
    def _after_start(_event) -> None:
        nonlocal started
        started = True
        entry.async_on_unload(async_call_later(hass, grace, _check))

    unsubscribe = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _after_start)

    @callback
    def _cancel_if_not_fired() -> None:
        # A once-listener removes itself when it fires, so unsubscribing after
        # that asks Home Assistant to remove a listener it no longer has --
        # which it logs as "Unable to remove unknown job listener" on every
        # reload.
        if not started:
            unsubscribe()

    entry.async_on_unload(_cancel_if_not_fired)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None:
        # A schedule saved in the last couple of seconds is still waiting in
        # the delayed save; a reload would otherwise read the file without it.
        await coordinator.store.async_flush()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            for service in (
                SERVICE_RELOAD,
                SERVICE_EXPLAIN,
                SERVICE_SET_ROOM_TARGET,
                SERVICE_CLEAR_OVERRIDE,
                SERVICE_RESUME_SCHEDULE,
                SERVICE_SET_SCHEDULE,
                SERVICE_SET_AWAY,
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
            merged["away"] = report.get("away")
            merged["groups"].update(report.get("groups", {}))
            merged["rooms"].update(report.get("rooms", {}))
            merged["pending_writes"].extend(report.get("pending_writes", []))
            merged["last_actions"].extend(report.get("last_actions", []))
        return merged

    async def set_room_target(call: ServiceCall) -> None:
        room = call.data[ATTR_ROOM]
        until = call.data.get(ATTR_UNTIL)
        # The UI's datetime picker sends wall-clock time with no zone. Compared
        # with an aware "now" that raises on every cycle, so read it as local.
        if until is not None and until.tzinfo is None:
            until = until.replace(tzinfo=dt_util.get_default_time_zone())
        await _resolve(hass, room).async_set_override(
            room, call.data["temperature"], until
        )

    async def set_schedule(call: ServiceCall) -> None:
        room = call.data[ATTR_ROOM]
        marks = [
            Mark(parse_time(item["at"]), float(item["temperature"]))
            for item in call.data["marks"]
        ]
        if len({m.minute for m in marks}) != len(marks):
            raise HomeAssistantError("two marks at the same time")
        await _resolve(hass, room).async_set_schedule(
            room, list(call.data["days"]), marks, call.context
        )

    async def set_away(call: ServiceCall) -> None:
        """Rooms named: those rooms alone, optionally until a time. None: the house."""
        rooms = call.data.get(ATTR_ROOM) or []
        away = call.data["away"]
        until = call.data.get(ATTR_UNTIL)
        if until is not None and until.tzinfo is None:
            until = until.replace(tzinfo=dt_util.get_default_time_zone())
        if until is not None and not (rooms and away):
            raise HomeAssistantError(
                "'until' needs one or more rooms and away: true; the whole house "
                "stays away until the switch is turned off"
            )
        if not rooms:
            for coordinator in _coordinators(hass):
                await coordinator.async_set_away(away)
            return
        # Every name checked before anything changes, so a typo in the third
        # room does not leave the first two away.
        targets = [(_resolve(hass, room), room) for room in rooms]
        for coordinator, room in targets:
            await coordinator.async_set_room_away(room, away, until)

    async def clear_override(call: ServiceCall) -> None:
        room = call.data[ATTR_ROOM]
        await _resolve(hass, room).async_clear_override(room)

    async def resume_schedule(call: ServiceCall) -> None:
        room = call.data[ATTR_ROOM]
        await _resolve(hass, room).async_resume_schedule(room)

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
    hass.services.async_register(
        DOMAIN, SERVICE_RESUME_SCHEDULE, resume_schedule, schema=ROOM_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_SCHEDULE, set_schedule, schema=SET_SCHEDULE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_AWAY, set_away, schema=SET_AWAY_SCHEMA
    )
