"""The tick: gather facts, call the planner, hand the plan to the reconciler.

This is the only place where the impure world meets the pure one. It holds the
state the planner is not allowed to hold -- the previous runtime, the group's
current mode, last cycle's lockout answers, the live overrides -- and passes
all of it in as arguments.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import comfort
from . import measurement
from .const import DOMAIN
from .core.config import SystemConfig
from .core.model import Constraints, GroupState, Plan, RoomRuntime
from .core.planner import plan as make_plan
from .core.energy import SurplusLatch
from .core.policy import constraints_for
from .comfort import Override
from .core.model import RoomIntent
from .core.schedule import (
    AWAY_SOURCES,
    Mark,
    ScheduleError,
    WeekSchedule,
    check as check_schedule,
    who_may_edit,
    may_edit,
    target_problem,
)
from .store import StateStore
from .reconciler import Reconciler
from .telemetry import EVENT_CYCLE, Telemetry, record_for

_LOGGER = logging.getLogger(__name__)

#: Cycles held in memory waiting to be written. At a 30 second tick this is
#: over an hour of backlog, which is far longer than any plausible stall.
_MAX_BUFFERED = 240


class MultisplitCoordinator(DataUpdateCoordinator[Plan]):
    """Runs one cycle of the system."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, config: SystemConfig) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=config.operation.tick,
        )
        self.entry = entry
        self.config = config
        self.reconciler = Reconciler(hass, config)
        self.telemetry = Telemetry(config.telemetry, Path(hass.config.path("")))

        self.overrides: dict[str, Override] = {}
        self.store = StateStore(hass)
        self.schedules: dict[str, WeekSchedule] = {}
        #: Rooms set away on their own, and until when (None: until cleared).
        self.rooms_away: dict[str, datetime | None] = {}
        self.intents: dict[str, RoomIntent] = {}
        self.excluded: set[str] = set()
        self.pending: tuple[str, ...] = ()
        self.actions: tuple[str, ...] = ()

        self._runtime: dict[str, RoomRuntime] = {}
        self._group_state: dict[str, GroupState] = {
            group.key: GroupState(group.key) for group in config.groups
        }
        self._allowed_previously: dict[str, frozenset[str]] = {}
        self._constraints: dict[str, Constraints] = {}
        self.surplus = SurplusLatch(config.energy.settle)

        # Telemetry is written off the control loop. Home Assistant's executor
        # pool is shared, and one badly behaved integration can occupy every
        # worker for a minute at a time -- seen in the wild with an nmap-based
        # scanner holding all twenty. Awaiting a file write inside the cycle
        # would make the thermostat's responsiveness depend on that.
        self._to_write: list[dict] = []
        self._writing = False
        self._dropped = 0

    # ------------------------------------------------------------------
    # Watching the inputs
    # ------------------------------------------------------------------

    @callback
    def async_watch_sources(self) -> None:
        """Re-evaluate as soon as an input changes, not just on the tick.

        The tick is the floor, not the mechanism: a room that has just reached
        its cut-off should stop when the sensor says so, not up to a tick
        later.
        """
        watched = {self.config.outdoor_sensor}
        if self.config.energy.surplus_sensor:
            watched.add(self.config.energy.surplus_sensor)
        for room in self.config.rooms:
            watched.add(room.climate_entity)
            watched.update(room.temperature_sensors)
            if room.schedule_entity:
                watched.add(room.schedule_entity)

        self.entry.async_on_unload(
            async_track_state_change_event(
                self.hass, sorted(watched), self._async_source_changed
            )
        )

    @callback
    def _async_source_changed(self, event) -> None:
        old = event.data.get("old_state")
        new = event.data.get("new_state")
        if new is None:
            return
        # Attribute-only churn on a climate entity (fan speed, swing) is not a
        # reason to re-plan; a changed state or temperature is.
        if old is not None and old.state == new.state:
            if old.attributes.get("current_temperature") == new.attributes.get(
                "current_temperature"
            ) and old.attributes.get("temperature") == new.attributes.get("temperature"):
                return
        self.hass.async_create_task(self.async_request_refresh())

    # ------------------------------------------------------------------
    # The cycle
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> Plan:
        now = dt_util.utcnow()

        for room in comfort.expired(self.overrides, now):
            _LOGGER.debug("multisplit: override for %s expired", room)
            self.overrides.pop(room, None)

        for room, until in list(self.rooms_away.items()):
            if until is not None and now >= until:
                _LOGGER.info("multisplit: %s is back from away (it was set until %s)", room, until)
                self.rooms_away.pop(room)
                self.store.set_room_away(room, False)

        outdoor = measurement.read_outdoor(self.hass, self.config)
        surplus_reading = measurement.read_surplus(self.hass, self.config)
        surplus = self.surplus.update(surplus_reading, now)
        measurements = measurement.collect(self.hass, self.config, now)
        intents = comfort.resolve(
            self.hass,
            self.config,
            self.overrides,
            now,
            schedules=self.schedules,
            away=self.store.away,
            rooms_away=frozenset(self.rooms_away),
        )
        self.intents = intents

        self._constraints = {
            group.key: constraints_for(
                group.policy,
                outdoor=outdoor,
                previously_allowed=self._allowed_previously.get(group.key),
                solar=surplus,
            )
            for group in self.config.groups
        }
        self._allowed_previously = {
            key: limits.allowed_modes for key, limits in self._constraints.items()
        }

        # A room switched off asks for nothing, votes for nothing and is given
        # no command -- the planner leaves it out, exactly as before, because
        # a room with demand that is never served is the starvation the aging
        # term exists to prevent and would age forever. It is still passed in,
        # as standby, for one reason: frost protection reaches it too.
        plan = make_plan(
            rooms=[room.as_room_config() for room in self.config.rooms],
            measurements=measurements,
            intents=intents,
            constraints=self._constraints,
            runtime=self._runtime,
            group_state=self._group_state,
            outdoor=outdoor,
            now=now,
            frost=self.config.frost,
            standby=frozenset(self.excluded),
        )

        self._runtime = {
            runtime.room: runtime
            for runtime in plan.runtime
            if not (runtime.releasing and self._unit_is_off(runtime.room))
        }
        for decision in plan.groups:
            previous = self._group_state.get(decision.group, GroupState(decision.group))
            if previous.mode != decision.mode:
                self._group_state[decision.group] = GroupState(
                    decision.group, decision.mode, now
                )

        self.pending = self.reconciler.would_do(plan)
        if self.config.operation.apply_to_hardware:
            self.actions = await self.reconciler.apply(plan, now)
        else:
            self.actions = ()
            if self.pending:
                _LOGGER.debug("multisplit: shadow mode, would write: %s", "; ".join(self.pending))

        await self._async_record(
            plan, measurements, outdoor, intents, surplus, surplus_reading
        )
        return plan

    async def _async_record(
        self, plan, measurements, outdoor, intents, surplus, surplus_reading
    ) -> None:
        """Keep the cycle. Never let recording it break the cycle itself."""
        record = record_for(
            plan,
            measurements,
            outdoor,
            self.pending,
            self.actions,
            self.config.operation.apply_to_hardware,
            intents=intents,
            surplus=surplus,
            surplus_reading=surplus_reading,
            away=self.away,
        )
        self.telemetry.remember(record)

        if self.config.telemetry.enabled:
            self._queue(record)

        if self.config.telemetry.events:
            self.hass.bus.async_fire(EVENT_CYCLE, record)

    @callback
    def _queue(self, record: dict) -> None:
        """Hand the record to a background writer, never to this cycle."""
        self._to_write.append(record)
        if len(self._to_write) > _MAX_BUFFERED:
            # Bounded: a disk that never comes back must not become a memory
            # leak. The count is surfaced in diagnostics so the loss is
            # visible rather than silent.
            dropped = len(self._to_write) - _MAX_BUFFERED
            del self._to_write[:dropped]
            self._dropped += dropped

        if self._writing:
            return
        self._writing = True
        self.hass.async_create_background_task(
            self._async_flush(), name="multisplit telemetry flush"
        )

    async def _async_flush(self) -> None:
        try:
            while self._to_write:
                batch, self._to_write = self._to_write, []
                await self.hass.async_add_executor_job(
                    self.telemetry.write_many, batch
                )
        finally:
            self._writing = False

    @property
    def telemetry_health(self) -> dict:
        return {
            **self.telemetry.summary(),
            "queued": len(self._to_write),
            "dropped": self._dropped,
        }

    # ------------------------------------------------------------------
    # Services reach in here
    # ------------------------------------------------------------------

    def constraints_for_group(self, group: str) -> Constraints:
        return self._constraints.get(group, Constraints())

    def measurement_for(self, room: str):
        return measurement.collect(self.hass, self.config, dt_util.utcnow()).get(room)

    # ------------------------------------------------------------------
    # What people set: schedules, away, overrides
    # ------------------------------------------------------------------

    async def async_load_state(self) -> None:
        """Read stored schedules and away before the first cycle."""
        await self.store.async_load()
        self.schedules = self.store.schedules(self.config)
        known = {room.key for room in self.config.rooms}
        self.rooms_away = {
            room: until for room, until in self.store.rooms_away().items() if room in known
        }

    @property
    def away(self) -> bool:
        return self.store.away

    async def async_set_away(self, away: bool) -> None:
        """Leaving or coming back resets manual overrides.

        An override from this morning should not keep one room at 22° all the
        time the house is empty, and one set while away (someone home early)
        has served its purpose once the house is occupied again.
        """
        if away == self.store.away:
            return
        self.store.set_away(away)
        self.overrides.clear()
        _LOGGER.info("multisplit: away %s; manual overrides cleared", "on" if away else "off")
        await self.async_request_refresh()

    def room_away(self, room: str) -> bool:
        """Whether this room is set away on its own (the house may be too)."""
        return room in self.rooms_away

    def is_away(self, room: str) -> bool:
        """Whether this room is holding the setback, for whichever reason."""
        intent = self.intents.get(room)
        if intent is not None:
            return intent.source in AWAY_SOURCES
        return self.away or self.room_away(room)

    async def async_set_room_away(
        self, room: str, away: bool, until: datetime | None = None
    ) -> None:
        """One room to its setback, or back -- the house switch still wins.

        Like the house switch, it clears the room's own override either way:
        a tweak from this morning should not keep an empty room warm, and one
        made while it was away has served its purpose once it is back.
        """
        name = next(r.name for r in self.config.rooms if r.key == room)
        if not away and self.away:
            raise ServiceValidationError(
                f"{name} cannot come back on its own while the whole house is away; "
                "switch Multi-split away off, or turn the room's thermostat up to "
                "heat it while the house stays away"
            )
        if away and until is not None and until <= dt_util.utcnow():
            raise ServiceValidationError(f"{name}: 'until' is already in the past")
        if away:
            self.rooms_away[room] = until
        elif room not in self.rooms_away:
            return
        else:
            self.rooms_away.pop(room)
        self.store.set_room_away(room, away, until)
        self.overrides.pop(room, None)
        _LOGGER.info(
            "multisplit: %s away %s%s; its override cleared",
            room,
            "on" if away else "off",
            f" until {until.isoformat()}" if away and until else "",
        )
        await self.async_request_refresh()

    def next_change(self, room: str) -> datetime | None:
        """When this room's schedule next moves, in local time; None without one."""
        schedule = self.schedules.get(room)
        if schedule is None or schedule.empty:
            return None
        return schedule.next_change(dt_util.as_local(dt_util.utcnow()))

    async def async_set_schedule(
        self,
        room: str,
        days: list[str],
        marks: list[Mark],
        context: Context | None,
    ) -> None:
        """Replace some days of a room's week, if this person may and it is safe."""
        spec = next(r for r in self.config.rooms if r.key == room)
        user = (
            await self.hass.auth.async_get_user(context.user_id)
            if context is not None and context.user_id
            else None
        )
        if not may_edit(
            spec.owners,
            user_id=context.user_id if context else None,
            user_name=user.name if user else None,
            is_admin=bool(user and user.is_admin),
        ):
            raise ServiceValidationError(
                f"{spec.name}'s schedule can only be changed by "
                f"{who_may_edit(spec.owners)}"
            )
        current = self.schedules.get(room, WeekSchedule())
        try:
            updated = current.with_days(days, marks)
            check_schedule(updated, self.config.guardrails(room))
        except ScheduleError as error:
            raise ServiceValidationError(
                f"{spec.name}'s schedule was not saved: " + "; ".join(error.problems)
            ) from error
        self.store.set_schedule(room, updated)
        self.schedules[room] = updated
        _LOGGER.info(
            "multisplit: schedule for %s changed (%s) by %s",
            room,
            ", ".join(days),
            user.name if user else "Home Assistant",
        )
        await self.async_request_refresh()

    async def async_set_override(
        self, room: str, target: float, until: datetime | None
    ) -> None:
        problem = target_problem(target, self.config.guardrails(room))
        if problem:
            name = next(r.name for r in self.config.rooms if r.key == room)
            raise ServiceValidationError(f"{name}: {problem}")
        self.overrides[room] = Override(target=target, expires_at=until)
        await self.async_request_refresh()

    async def async_set_from_thermostat(self, room: str, target: float) -> None:
        """A thermostat turned by hand holds until the schedule next moves."""
        await self.async_set_override(room, target, self.next_change(room))

    async def async_clear_override(self, room: str) -> None:
        self.overrides.pop(room, None)
        await self.async_request_refresh()

    def _unit_is_off(self, room: str) -> bool:
        """Whether a room's unit is seen off, or no longer exists at all.

        Unavailable is not off: a unit that drops off the network mid-release
        may come back still heating, so the off command keeps repeating.
        """
        spec = next(r for r in self.config.rooms if r.key == room)
        state = self.hass.states.get(spec.climate_entity)
        return state is None or state.state == "off"

    def is_excluded(self, room: str) -> bool:
        return room in self.excluded

    def is_idle_excluded(self, room: str) -> bool:
        """Switched off and left alone -- not being frost-protected right now."""
        return room in self.excluded and self.command_for(room) is None

    def command_for(self, room: str):
        """This cycle's command for a room, or None when it has none."""
        plan = self.data
        if plan is None:
            return None
        try:
            return plan.room(room)
        except KeyError:
            return None

    async def async_exclude(self, room: str) -> None:
        """Take a room out of control entirely, at its own request."""
        self.excluded.add(room)
        await self.async_request_refresh()

    async def async_include(self, room: str) -> None:
        self.excluded.discard(room)
        await self.async_request_refresh()

    def explain(self, room: str | None = None) -> dict:
        """Everything the system believes right now, for developer tools."""
        plan = self.data
        if plan is None:
            return {"error": "no plan has been computed yet"}

        groups = {
            decision.group: {
                "mode": decision.mode,
                "because": decision.because,
                "scores": {k: round(v, 3) for k, v in decision.scores.items()},
                "unserved": list(decision.unserved),
                "allowed_modes": sorted(self.constraints_for_group(decision.group).allowed_modes),
                "policy_reasons": dict(self.constraints_for_group(decision.group).reasons),
            }
            for decision in plan.groups
        }
        excluded = {
            key: {"mode": None, "because": "excluded from control at this room's request"}
            for key in sorted(self.excluded)
            if (room is None or key == room)
            # A switched-off room under frost protection has a real command.
            and not any(command.room == key for command in plan.rooms)
        }
        rooms = {
            command.room: {
                "mode": command.mode,
                "target": command.target,
                "because": command.because,
                "wants": command.wants,
                "unserved_for_minutes": round(command.unserved_for.total_seconds() / 60, 1),
                "target_source": (
                    intent.source if (intent := self.intents.get(command.room)) else None
                ),
                "frost_protection": command.frost,
                "room_away": self.room_away(command.room),
                "room_away_until": (
                    until.isoformat() if (until := self.rooms_away.get(command.room)) else None
                ),
                "next_schedule_change": (
                    change.isoformat() if (change := self.next_change(command.room)) else None
                ),
            }
            for command in plan.rooms
            if room is None or command.room == room
        }
        return {
            "at": plan.at.isoformat(),
            "apply_to_hardware": self.config.operation.apply_to_hardware,
            "away": self.away,
            "rooms_away": sorted(self.rooms_away),
            "frost_protection": (
                {"below": self.config.frost.below, "release": self.config.frost.release}
                if self.config.frost
                else "off"
            ),
            "energy": (
                self.surplus.describe(plan.at)
                if self.config.energy.surplus_sensor
                else "no surplus sensor configured"
            ),
            "groups": groups if room is None else {},
            "rooms": {**rooms, **excluded},
            "pending_writes": list(self.pending),
            "last_actions": list(self.actions),
        }
