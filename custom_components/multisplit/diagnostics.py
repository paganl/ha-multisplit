"""The diagnostics download: everything needed to answer "why did it do that?".

Downloaded from the device page as JSON. It carries the configuration as
loaded, the recent cycles with their reasons and scores, and -- the part worth
having -- the divergences between what this planner would do and what the
hardware is actually set to.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from . import measurement
from .const import DOMAIN
from .coordinator import MultisplitCoordinator
from .telemetry import observed_overshoot


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator: MultisplitCoordinator = hass.data[DOMAIN][entry.entry_id]
    config = coordinator.config

    overshoot = observed_overshoot(coordinator.telemetry.recent)

    return {
        "configuration": {
            "outdoor_sensor": config.outdoor_sensor,
            "apply_to_hardware": config.operation.apply_to_hardware,
            "tick_seconds": config.operation.tick.total_seconds(),
            "write_interval_seconds": config.operation.write_interval.total_seconds(),
            "stale_after_minutes": config.operation.stale_after.total_seconds() / 60,
            "groups": {
                group.key: {
                    "name": group.name,
                    "rooms": [room.key for room in config.rooms_in(group.key)],
                    "cool_below": group.policy.cool_below,
                    "heat_above": group.policy.heat_above,
                    "deadband": group.policy.deadband,
                    "overshoot_tolerance": group.policy.overshoot_tolerance,
                    "inertia_weight": group.policy.inertia_weight,
                    "inertia_half_life_minutes": (
                        group.policy.inertia_half_life.total_seconds() / 60
                    ),
                    "aging_rate": group.policy.aging_rate,
                }
                for group in config.groups
            },
            "rooms": {
                room.key: {
                    "group": room.group,
                    "climate_entity": room.climate_entity,
                    "temperature_sensors": list(room.temperature_sensors),
                    "priority": room.priority,
                    "cut_off_offset": list(room.participation.cut_off_offset.points),
                    "rest_minutes": [
                        (x, y / 60) for x, y in room.participation.min_off.points
                    ],
                    "min_on_minutes": room.participation.min_on.total_seconds() / 60,
                    "resume_deadband": room.participation.resume_deadband,
                }
                for room in config.rooms
            },
        },
        "health": {
            "missing_entities": measurement.unavailable_entities(hass, config),
            "excluded_rooms": sorted(coordinator.excluded),
            "overrides": {
                room: {
                    "target": override.target,
                    "until": override.expires_at.isoformat() if override.expires_at else None,
                }
                for room, override in coordinator.overrides.items()
            },
        },
        "now": coordinator.explain(),
        "telemetry": coordinator.telemetry.summary(),
        "observed_overshoot": {
            "samples": len(overshoot),
            "points": [{"outdoor": o, "overshoot": v} for o, v in overshoot[-200:]],
            "note": (
                "Each point is a cycle where a unit was stopped: the overshoot "
                "the controller permitted at that outdoor temperature. Fit these "
                "against the configured cut_off_offset curve — "
                "tools/fit_curves.py does it from the telemetry file."
            ),
        },
    }
