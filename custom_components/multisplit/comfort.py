"""What each room should be, and why. Never how to achieve it.

Named ``comfort`` rather than ``intent``, which is what the concern is called
everywhere else in this project, because ``intent`` is a reserved platform
name in Home Assistant: the voice-assistant intent component walks every
loaded integration looking for a module of that name and calls
``async_setup_intents`` on it. A module called ``intent.py`` here is picked up
as a voice platform and raises AttributeError on every start. The types keep
the project's vocabulary -- ``RoomIntent``, ``intent_source`` -- only the file
is renamed.

Resolution order, most specific first: an unexpired override, then away (the
house-wide switch or the room's own presence entity), then the room's weekly
schedule, then the configured default -- see ``core.schedule.choose_target``. The source is carried through to the UI
so a room can say *why* it is aiming at what it is aiming at -- "override
until 21:30" and "schedule" look identical on a thermostat, and that ambiguity
is most of what makes a heating system feel haunted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .core.config import SystemConfig
from .core.model import RoomIntent
from .core.schedule import WeekSchedule, choose_target


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
    *,
    schedules: dict[str, WeekSchedule] | None = None,
    away: bool = False,
) -> dict[str, RoomIntent]:
    """One intent per room: override, then away, then schedule, then default.

    ``now`` is UTC; schedules are wall-clock, so they are read in local time.
    """
    schedules = schedules or {}
    local = dt_util.as_local(now)
    out: dict[str, RoomIntent] = {}

    for room in config.rooms:
        band = config.intent.band if room.band is None else room.band

        override = overrides.get(room.key)
        live = override if override is not None and override.active(now) else None

        room_away = False
        if room.schedule_entity:
            state = hass.states.get(room.schedule_entity)
            room_away = state is not None and state.state == "off"

        target, source = choose_target(
            override=live.target if live else None,
            house_away=away,
            room_away=room_away,
            schedule=schedules.get(room.key, WeekSchedule()),
            local=local.replace(tzinfo=None),
            default=config.intent.default_target,
            away=config.intent.away_target,
        )
        out[room.key] = RoomIntent(
            room=room.key,
            target=target,
            band=band,
            source=source,
            expires_at=live.expires_at if live else None,
            priority=room.priority,
        )

    return out


def expired(overrides: dict[str, Override], now: datetime) -> list[str]:
    """Rooms whose override has run out, so the coordinator can drop them."""
    return [room for room, override in overrides.items() if not override.active(now)]
