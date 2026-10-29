"""Which temperature to believe, and whether to believe it.

Owns one decision: for each room, what is the authoritative reading and how
much do we trust it. The planner then treats the answer as given -- a room
whose temperature is not trustworthy stands by and says so, because a stale
sensor otherwise produces a confident wrong decision for the whole group.
"""

from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.const import ATTR_SUPPORTED_FEATURES
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


def read_surplus(hass: HomeAssistant, config: SystemConfig) -> bool | None:
    """Whether the house is exporting, or None when there is no answer.

    None -- no sensor configured, or it is unavailable -- is treated as no
    surplus downstream: banking is an optimisation, and the safe default is
    the behaviour from before it existed.
    """
    entity = config.energy.surplus_sensor
    if entity is None:
        return None
    state = hass.states.get(entity)
    if state is None or state.state in _UNUSABLE:
        return None
    return state.state == "on"


def _age(state, now: datetime):
    """Time since the integration last *reported*, not since the value changed.

    ``last_updated`` only moves when the state or an attribute changes, so a
    0.5°-resolution sensor in a room holding steady looks stale purely for
    being steady. Field telemetry showed exactly that: every stale flag came
    on 30 minutes after the value last changed and cleared when it moved,
    with a reading present 88-100% of the time. ``last_reported`` (Home
    Assistant 2024.3+) also moves when an unchanged state is written again.
    Unavailable and unknown states are rejected before this is consulted.
    """
    reported = getattr(state, "last_reported", None) or state.last_updated
    return now - max(reported, state.last_updated)


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
            stale = _age(state, now) > stale_after
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
                    stale = _age(state, now) > stale_after
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


def uncommandable_entities(hass: HomeAssistant, config: SystemConfig) -> dict[str, str]:
    """Configured units this integration cannot actually command, and why.

    The one that matters in practice is a unit set to a temperature *range* --
    a dual setpoint, low and high, which a unit in its own auto mode uses to
    choose between heating and cooling by itself.

    That is a deeper problem than an unsupported service call. Choosing
    between heating and cooling is the decision this system exists to take, on
    behalf of every room sharing an outdoor unit, because a room cannot see
    what the others need. A unit making that choice privately, from a band, is
    a second controller -- and the two cannot agree except by accident.

    Reported rather than worked around: writing a band would mean inventing
    how wide it should be, and a band wide enough to be accepted is wide
    enough for the unit to start making its own decisions again.
    """
    out: dict[str, str] = {}

    for room in config.rooms:
        state = hass.states.get(room.climate_entity)
        if state is None:
            continue  # absent is a different problem, reported separately

        features = state.attributes.get(ATTR_SUPPORTED_FEATURES) or 0
        single = bool(features & ClimateEntityFeature.TARGET_TEMPERATURE)
        ranged = bool(features & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE)

        if single:
            continue
        if ranged:
            low = state.attributes.get("target_temp_low")
            high = state.attributes.get("target_temp_high")
            band = f" (currently {low}–{high})" if low is not None and high is not None else ""
            out[room.climate_entity] = (
                f"{room.key}: set to a temperature range{band} rather than a single "
                "setpoint. A unit choosing between heating and cooling from a band "
                "is deciding for itself what this system decides for the whole "
                "group, so the two will disagree. Switch the unit to a single "
                "setpoint."
            )
        else:
            out[room.climate_entity] = (
                f"{room.key}: reports no settable target temperature at all "
                f"(supported_features={features})."
            )

    return out
