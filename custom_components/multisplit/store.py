"""What people set, kept across restarts: room schedules, the away switch and
each room's own away.

Configuration (multisplit.yaml) is what an admin decided once; this is what
the household changes day to day from the dashboard. It lives in Home
Assistant's own storage (``.storage/multisplit.state``), so it survives
restarts, reloads and regenerating the configuration, and is included in
Home Assistant's backups.

A stored schedule that no longer passes the room's guardrails -- because an
admin narrowed the room's range after it was saved -- is kept, not dropped:
it is the owner's work. The marks outside the range are clamped when read,
and a warning says which, so the owner can fix it on the card.
"""

from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .core.config import SystemConfig
from .core.schedule import Mark, ScheduleError, WeekSchedule

_LOGGER = logging.getLogger(__name__)

_VERSION = 1
_KEY = f"{DOMAIN}.state"
_SAVE_DELAY = 2.0


class StateStore:
    """Room schedules and away, loaded once and saved on change."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict] = Store(hass, _VERSION, _KEY)
        self._raw: dict = {"schedules": {}, "away": False, "rooms_away": {}}

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if isinstance(data, dict):
            self._raw = {
                "schedules": dict(data.get("schedules") or {}),
                "away": bool(data.get("away", False)),
                # Absent in anything saved before 0.5.0-beta.5.
                "rooms_away": {
                    str(room): until
                    for room, until in dict(data.get("rooms_away") or {}).items()
                    if until is None or isinstance(until, str)
                },
            }

    async def async_flush(self) -> None:
        """Write now, rather than after the save delay."""
        await self._store.async_save(self._raw)

    def _save(self) -> None:
        self._store.async_delay_save(lambda: self._raw, _SAVE_DELAY)

    # -- away ------------------------------------------------------------

    @property
    def away(self) -> bool:
        return self._raw["away"]

    def set_away(self, away: bool) -> None:
        self._raw["away"] = bool(away)
        self._save()

    # -- one room away ----------------------------------------------------

    def rooms_away(self) -> dict[str, datetime | None]:
        """Each room set away on its own, and until when (None: until cleared).

        An unreadable time is treated as no end rather than dropped: the room
        was set away on purpose, and staying away is the cautious reading.
        """
        out: dict[str, datetime | None] = {}
        for room, until in self._raw["rooms_away"].items():
            out[room] = dt_util.parse_datetime(until) if until else None
        return out

    def set_room_away(self, room: str, away: bool, until: datetime | None = None) -> None:
        if away:
            self._raw["rooms_away"][room] = until.isoformat() if until else None
        else:
            self._raw["rooms_away"].pop(room, None)
        self._save()

    # -- schedules -------------------------------------------------------

    def schedules(self, config: SystemConfig) -> dict[str, WeekSchedule]:
        """Every configured room's schedule, held to its current guardrails."""
        out: dict[str, WeekSchedule] = {}
        for room in config.rooms:
            raw = self._raw["schedules"].get(room.key)
            if not raw:
                out[room.key] = WeekSchedule()
                continue
            try:
                schedule = WeekSchedule.from_dict(raw)
            except ScheduleError as error:
                _LOGGER.warning(
                    "multisplit: the stored schedule for %s could not be read and is "
                    "ignored until it is saved again: %s",
                    room.key,
                    error,
                )
                out[room.key] = WeekSchedule()
                continue
            out[room.key] = _clamped(room.key, schedule, room.min_target, room.max_target)
        return out

    def set_schedule(self, room: str, schedule: WeekSchedule) -> None:
        self._raw["schedules"][room] = schedule.as_dict()
        self._save()


def _clamped(room: str, schedule: WeekSchedule, low: float, high: float) -> WeekSchedule:
    moved: list[str] = []
    days = []
    for day in schedule.days:
        marks = []
        for mark in day:
            target = min(max(mark.target, low), high)
            if target != mark.target:
                moved.append(f"{mark.time} {mark.target:g}°→{target:g}°")
            marks.append(Mark(mark.minute, target))
        days.append(tuple(marks))
    if moved:
        _LOGGER.warning(
            "multisplit: %s's schedule has marks outside its range %g–%g°, used as "
            "%s until the schedule is saved again",
            room,
            low,
            high,
            ", ".join(moved),
        )
    return WeekSchedule(tuple(days))
