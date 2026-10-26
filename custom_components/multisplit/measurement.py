"""Which temperature to believe, and whether to believe it.

Owns one decision: for each room, what is the authoritative reading and how
much do we trust it. The planner then treats the answer as given -- a room
whose temperature is not trustworthy stands by and says so, because a stale
sensor otherwise produces a confident wrong decision for the whole group.
"""

from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.core import HomeAssistant
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.util import dt as dt_util

from .core.config import SystemConfig
from .core.model import RoomMeasurement

_LOGGER = logging.getLogger(__name__)

_UNUSABLE = (None, STATE_UNAVAILABLE, STATE_UNKNOWN, "")


def _as_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def read_outdoor(hass: HomeAssistant, config: SystemConfig) -> float | None:
    """The outdoor temperature, or None when it cannot be trusted.

    None is a meaningful answer rather than a failure: policy declines to
    apply the lockouts and says so, and the participation curves fall back to
    their coldest breakpoint.
    """
    state = hass.states.get(config.outdoor_sensor)
    if state is None or state.state in _UNUSABLE:
        return None
    return _as_float(state.state)


def collect(
    hass: HomeAssistant, config: SystemConfig, now: datetime | None = None
) -> dict[str, RoomMeasurement]:
    """One measurement per room, in the room's own preference order."""
    now = now or dt_util.utcnow()
    stale_after = config.operation.stale_after
    out: dict[str, RoomMeasurement] = {}

    for room in config.rooms:
        measurement: RoomMeasurement | None = None
        last_resort: RoomMeasurement | None = None

        for entity_id in room.temperature_sensors:
            state = hass.states.get(entity_id)
            if state is None or state.state in _UNUSABLE:
                continue
            value = _as_float(state.state)
            if value is None:
                continue
            stale = (now - state.last_updated) > stale_after
            candidate = RoomMeasurement(room.key, value, stale=stale, source=entity_id)
            if not stale:
                measurement = candidate
                break
            # Remember it, but keep looking for something fresher.
            last_resort = last_resort or candidate

        if measurement is None:
            # Fall back to the indoor unit's own reading. It sits in the return
            # air path, so it reads warm while heating and cool while cooling:
            # it measures the unit, not the room. Usable, but every threshold
            # relative to it inherits that bias.
            state = hass.states.get(room.climate_entity)
            if state is not None:
                value = _as_float(state.attributes.get("current_temperature"))
                if value is not None:
                    stale = (now - state.last_updated) > stale_after
                    measurement = RoomMeasurement(
                        room.key,
                        value,
                        stale=stale,
                        source=f"{room.climate_entity} (return air)",
                    )

        if measurement is None:
            measurement = last_resort or RoomMeasurement(
                room.key, None, stale=True, source="no reading available"
            )

        out[room.key] = measurement

    return out


def unavailable_entities(hass: HomeAssistant, config: SystemConfig) -> list[str]:
    """Configured entities that do not exist. Surfaced in diagnostics."""
    wanted = [config.outdoor_sensor]
    for room in config.rooms:
        wanted.append(room.climate_entity)
        wanted.extend(room.temperature_sensors)
        if room.schedule_entity:
            wanted.append(room.schedule_entity)
    return [entity_id for entity_id in wanted if hass.states.get(entity_id) is None]
