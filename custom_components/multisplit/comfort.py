"""What each room should be, and why. Never how to achieve it.

Named ``comfort`` rather than ``intent``, which is what the concern is called
everywhere else in this project, because ``intent`` is a reserved platform
name in Home Assistant: the voice-assistant intent component walks every
loaded integration looking for a module of that name and calls
``async_setup_intents`` on it. A module called ``intent.py`` here is picked up
as a voice platform and raises AttributeError on every start. The types keep
the project's vocabulary -- ``RoomIntent``, ``intent_source`` -- only the file
is renamed.

Resolution order, most specific first: an unexpired override, then the room's
schedule, then the configured default. The source is carried through to the UI
so a room can say *why* it is aiming at what it is aiming at -- "override
until 21:30" and "schedule" look identical on a thermostat, and that ambiguity
is most of what makes a heating system feel haunted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from homeassistant.core import HomeAssistant

from .core.config import SystemConfig
from .core.model import RoomIntent


@dataclass(frozen=True)
class Override:
    """A target set by hand, optionally until a time."""

    target: float
    expires_at: datetime | None = None

    def active(self, now: datetime) -> bool:
        return self.expires_at is None or now < self.expires_at


def resolve(
    hass: HomeAssistant,
    config: SystemConfig,
    overrides: dict[str, Override],
    now: datetime,
) -> dict[str, RoomIntent]:
    """One intent per room."""
    out: dict[str, RoomIntent] = {}

    for room in config.rooms:
        override = overrides.get(room.key)
        if override is not None and override.active(now):
            out[room.key] = RoomIntent(
                room=room.key,
                target=override.target,
                source="override",
                expires_at=override.expires_at,
                priority=room.priority,
            )
            continue

        target = config.intent.default_target
        source = "default"

        if room.schedule_entity:
            state = hass.states.get(room.schedule_entity)
            if state is not None and state.state in ("on", "off"):
                if state.state == "on":
                    source = "schedule"
                else:
                    target = config.intent.away_target
                    source = "schedule (away)"

        out[room.key] = RoomIntent(
            room=room.key,
            target=target,
            source=source,
            expires_at=None,
            priority=room.priority,
        )

    return out


def expired(overrides: dict[str, Override], now: datetime) -> list[str]:
    """Rooms whose override has run out, so the coordinator can drop them."""
    return [room for room, override in overrides.items() if not override.active(now)]
