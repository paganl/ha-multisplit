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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import comfort
from . import measurement
from .const import DOMAIN
from .core.config import SystemConfig
from .core.model import Constraints, GroupState, Plan, RoomRuntime
from .core.planner import plan as make_plan
from .core.policy import constraints_for
from .comfort import Override
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
        self.excluded: set[str] = set()
        self.pending: tuple[str, ...] = ()
        self.actions: tuple[str, ...] = ()

        self._runtime: dict[str, RoomRuntime] = {}
        self._group_state: dict[str, GroupState] = {
            group.key: GroupState(group.key) for group in config.groups
        }
        self._allowed_previously: dict[str, frozenset[str]] = {}
        self._constraints: dict[str, Constraints] = {}

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

        outdoor = measurement.read_outdoor(self.hass, self.config)
        measurements = measurement.collect(self.hass, self.config, now)
        intents = comfort.resolve(self.hass, self.config, self.overrides, now)

        self._constraints = {
            group.key: constraints_for(
                group.policy,
                outdoor=outdoor,
                previously_allowed=self._allowed_previously.get(group.key),
            )
            for group in self.config.groups
        }
        self._allowed_previously = {
            key: limits.allowed_modes for key, limits in self._constraints.items()
        }

        # A room switched off asks for nothing and votes for nothing. Leaving
        # it out of the plan entirely is the honest encoding: the alternative,
        # a room with demand that is never served, is exactly the starvation
        # the aging term exists to prevent and would age forever.
        participating = [
            room for room in self.config.rooms if room.key not in self.excluded
        ]

        plan = make_plan(
            rooms=[room.as_room_config() for room in participating],
            measurements=measurements,
            intents=intents,
            constraints=self._constraints,
            runtime=self._runtime,
            group_state=self._group_state,
            outdoor=outdoor,
            now=now,
        )

        self._runtime = {runtime.room: runtime for runtime in plan.runtime}
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

        await self._async_record(plan, measurements, outdoor)
        return plan

    async def _async_record(self, plan, measurements, outdoor) -> None:
        """Keep the cycle. Never let recording it break the cycle itself."""
        record = record_for(
            plan,
            measurements,
            outdoor,
            self.pending,
            self.actions,
            self.config.operation.apply_to_hardware,
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

    async def async_set_override(
        self, room: str, target: float, until: datetime | None
    ) -> None:
        self.overrides[room] = Override(target=target, expires_at=until)
        await self.async_request_refresh()

    async def async_clear_override(self, room: str) -> None:
        self.overrides.pop(room, None)
        await self.async_request_refresh()

    def is_excluded(self, room: str) -> bool:
        return room in self.excluded

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
            if room is None or key == room
        }
        rooms = {
            command.room: {
                "mode": command.mode,
                "target": command.target,
                "because": command.because,
                "wants": command.wants,
                "unserved_for_minutes": round(command.unserved_for.total_seconds() / 60, 1),
            }
            for command in plan.rooms
            if room is None or command.room == room
        }
        return {
            "at": plan.at.isoformat(),
            "apply_to_hardware": self.config.operation.apply_to_hardware,
            "groups": groups if room is None else {},
            "rooms": {**rooms, **excluded},
            "pending_writes": list(self.pending),
            "last_actions": list(self.actions),
        }
