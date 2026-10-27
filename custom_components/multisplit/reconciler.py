"""The only component that writes to hardware.

It decides nothing. Every cycle it compares what the plan says each unit's
state should be against what that unit's state actually is, and writes the
difference. That has four consequences worth naming:

* **Idempotent.** Running it again changes nothing, so it can run on every
  tick without a thought about whether it already ran.
* **Mode and setpoint always travel together.** A command is a complete
  desired state, so the failure where a unit ends up in the right mode at the
  wrong temperature cannot be expressed, let alone emitted.
* **Self-healing.** A unit changed at the wall, or one that forgot its
  setpoint over a power cut, is corrected within one cycle. Nothing can be
  "missed", because nothing is event-driven.
* **Respectful of the hardware.** Rate-limited per unit, so it stays clear of
  the compressor's own anti-cycle lockout however fast something upstream
  changes its mind.
"""

from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.const import ATTR_ENTITY_ID, ATTR_SUPPORTED_FEATURES, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant

from .core.config import SystemConfig
from .core.model import COOL, HEAT, Plan

_LOGGER = logging.getLogger(__name__)

#: Setpoints that differ by less than this are treated as equal, so floating
#: point noise and a unit that rounds to the nearest half degree do not cause
#: a write on every single tick.
_SETPOINT_TOLERANCE = 0.05

_HVAC = {HEAT: HVACMode.HEAT, COOL: HVACMode.COOL}


class Reconciler:
    """Makes the hardware match the plan, and reports what it did."""

    def __init__(self, hass: HomeAssistant, config: SystemConfig) -> None:
        self._hass = hass
        self._config = config
        self._rooms = {room.key: room for room in config.rooms}
        self._last_write: dict[str, datetime] = {}
        self._uncommandable: set[str] = set()
        self.last_actions: tuple[str, ...] = ()

    async def apply(self, plan: Plan, now: datetime) -> tuple[str, ...]:
        """Write every difference between the plan and the hardware."""
        actions: list[str] = []

        for command in plan.rooms:
            room = self._rooms.get(command.room)
            if room is None:
                continue

            state = self._hass.states.get(room.climate_entity)
            if state is None:
                actions.append(f"{command.room}: {room.climate_entity} does not exist")
                continue

            if not _takes_a_setpoint(state) and command.mode is not None:
                # Retrying a call the unit cannot accept, every interval, for
                # ever, is worse than saying so once: it fills the log and
                # leaves the divergence sensor permanently wrong.
                if command.room not in self._uncommandable:
                    self._uncommandable.add(command.room)
                    _LOGGER.warning(
                        "multisplit: %s takes a temperature range, not a single "
                        "setpoint, so this integration cannot command it. Switch "
                        "the unit to a single setpoint.",
                        room.climate_entity,
                    )
                actions.append(
                    f"{command.room}: cannot be commanded — it takes a temperature range"
                )
                continue

            desired_mode = _HVAC[command.mode] if command.mode else HVACMode.OFF
            observed_mode = state.state
            observed_target = state.attributes.get(ATTR_TEMPERATURE)

            mode_matches = observed_mode == desired_mode
            target_matches = desired_mode == HVACMode.OFF or (
                observed_target is not None
                and abs(float(observed_target) - command.target) < _SETPOINT_TOLERANCE
            )
            if mode_matches and target_matches:
                continue

            last = self._last_write.get(command.room)
            if last is not None and (now - last) < self._config.operation.write_interval:
                waited = now - last
                actions.append(
                    f"{command.room}: holding off, only {int(waited.total_seconds())}s "
                    f"since the last write"
                )
                continue

            try:
                if desired_mode == HVACMode.OFF:
                    await self._hass.services.async_call(
                        CLIMATE_DOMAIN,
                        SERVICE_SET_HVAC_MODE,
                        {ATTR_ENTITY_ID: room.climate_entity, ATTR_HVAC_MODE: HVACMode.OFF},
                        blocking=True,
                    )
                    actions.append(f"{command.room}: off ({command.because})")
                else:
                    # One call carrying both, so the unit is never briefly in
                    # the new mode at the old setpoint.
                    await self._hass.services.async_call(
                        CLIMATE_DOMAIN,
                        SERVICE_SET_TEMPERATURE,
                        {
                            ATTR_ENTITY_ID: room.climate_entity,
                            ATTR_HVAC_MODE: desired_mode,
                            ATTR_TEMPERATURE: command.target,
                        },
                        blocking=True,
                    )
                    actions.append(
                        f"{command.room}: {desired_mode} at {command.target:g}° "
                        f"({command.because})"
                    )
            except Exception as error:  # noqa: BLE001 - one bad unit must not stop the rest
                _LOGGER.warning(
                    "multisplit: could not command %s: %s", room.climate_entity, error
                )
                actions.append(f"{command.room}: write failed — {error}")
                continue

            self._last_write[command.room] = now

        self.last_actions = tuple(actions)
        return self.last_actions

    def would_do(self, plan: Plan) -> tuple[str, ...]:
        """What `apply` would write, without writing it. Shadow mode's output."""
        pending: list[str] = []
        for command in plan.rooms:
            room = self._rooms.get(command.room)
            if room is None:
                continue
            state = self._hass.states.get(room.climate_entity)
            if state is not None and not _takes_a_setpoint(state):
                pending.append(
                    f"{command.room}: takes a temperature range, so its setpoint "
                    "cannot be compared or commanded"
                )
                continue
            desired_mode = _HVAC[command.mode] if command.mode else HVACMode.OFF
            observed_mode = state.state if state else "missing"
            observed_target = state.attributes.get(ATTR_TEMPERATURE) if state else None

            if observed_mode == desired_mode and (
                desired_mode == HVACMode.OFF
                or (
                    observed_target is not None
                    and abs(float(observed_target) - command.target) < _SETPOINT_TOLERANCE
                )
            ):
                continue

            observed = (
                observed_mode
                if observed_target is None
                else f"{observed_mode} at {float(observed_target):g}°"
            )
            wanted = (
                str(desired_mode)
                if desired_mode == HVACMode.OFF
                else f"{desired_mode} at {command.target:g}°"
            )
            pending.append(f"{command.room}: {observed} → {wanted}")
        return tuple(pending)


def _takes_a_setpoint(state) -> bool:
    """Whether this unit accepts a single target temperature.

    A unit set to a range exposes target_temp_low and target_temp_high and no
    `temperature` at all, so there is nothing to compare against and nothing
    to write -- the comparison would report a difference on every cycle and
    the write would be refused on every attempt.
    """
    features = state.attributes.get(ATTR_SUPPORTED_FEATURES) or 0
    return bool(features & ClimateEntityFeature.TARGET_TEMPERATURE)
