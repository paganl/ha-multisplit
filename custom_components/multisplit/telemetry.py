"""Recording each cycle, so the measured numbers can be re-measured.

Two of the values this system runs on cannot be derived from anything: the
overshoot cut-off and the rest period are properties of a particular unit in a
particular room in a particular building, and the inertia weight is a
judgement about how fast this hardware tolerates changing its mind. They were
measured once, by living with them. Telemetry is what turns that into
something repeatable.

One JSON object per cycle, holding everything needed to refit a curve:
outdoor temperature, and for each room its target, its measured temperature,
what it wanted, what it was told to do and why. ``tools/fit_curves.py`` reads
it back.

It is also the record that makes shadow mode worth running: `pending` is the
list of writes the reconciler would have made, which is precisely the
divergence between this planner and whatever is currently running the house.

This module deliberately imports nothing from the rest of the package. It
reads attributes off whatever it is given, so it can be imported and tested on
its own -- and so writing a file, which is I/O, stays out of ``core/``, where
the rule is that there is none.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

EVENT_CYCLE = "multisplit_cycle"

#: How many cycles to keep in memory for the diagnostics download.
_RING = 120


def record_for(
    plan,
    measurements: dict,
    outdoor: float | None,
    pending: tuple[str, ...],
    actions: tuple[str, ...],
    applied: bool,
) -> dict:
    """One cycle, as a flat JSON-safe object."""
    return {
        "at": plan.at.isoformat(),
        "outdoor": outdoor,
        "applied": applied,
        "groups": {
            decision.group: {
                "mode": decision.mode,
                "because": decision.because,
                "scores": {k: round(v, 4) for k, v in decision.scores.items()},
                "unserved": list(decision.unserved),
            }
            for decision in plan.groups
        },
        "rooms": {
            command.room: {
                "mode": command.mode,
                "target": command.target,
                "measured": (m.temperature if (m := measurements.get(command.room)) else None),
                "source": (m.source if (m := measurements.get(command.room)) else None),
                "stale": (m.stale if (m := measurements.get(command.room)) else None),
                "wants": command.wants,
                "overshoot": (
                    round(m.temperature - command.target, 3)
                    if (m := measurements.get(command.room)) and m.temperature is not None
                    else None
                ),
                "unserved_minutes": round(command.unserved_for.total_seconds() / 60, 2),
                "because": command.because,
            }
            for command in plan.rooms
        },
        "pending": list(pending),
        "actions": list(actions),
    }


class Telemetry:
    """Appends cycles to a file, keeps the recent ones in memory."""

    def __init__(self, config, directory: Path) -> None:
        """``config`` is a TelemetryConfig: enabled, file, max_bytes, events."""
        self._config = config
        self._path = directory / self._config.file
        self.recent: list[dict] = []
        self.written = 0
        self.last_error: str | None = None

    @property
    def path(self) -> Path:
        return self._path

    def remember(self, record: dict) -> None:
        """Keep it in memory. Cheap, and always on -- diagnostics need it."""
        self.recent.append(record)
        if len(self.recent) > _RING:
            del self.recent[: len(self.recent) - _RING]

    def write(self, record: dict) -> None:
        """Append one line. Called in an executor -- this touches the disk.

        A telemetry failure must never take the controller down with it: the
        house staying warm matters more than the record of why.
        """
        if not self._config.enabled:
            return
        try:
            self._rotate_if_needed()
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
            self.written += 1
            self.last_error = None
        except OSError as error:
            if self.last_error != str(error):
                _LOGGER.warning("multisplit: could not write telemetry: %s", error)
            self.last_error = str(error)

    def _rotate_if_needed(self) -> None:
        """Keep one previous file, so recent history always survives."""
        try:
            if self._path.exists() and self._path.stat().st_size >= self._config.max_bytes:
                previous = self._path.with_suffix(self._path.suffix + ".1")
                previous.unlink(missing_ok=True)
                self._path.rename(previous)
                _LOGGER.info("multisplit: rotated telemetry to %s", previous.name)
        except OSError as error:
            _LOGGER.debug("multisplit: could not rotate telemetry: %s", error)

    def summary(self) -> dict:
        """What the diagnostics download and the status sensors report."""
        divergences: dict[str, int] = {}
        for record in self.recent:
            for line in record.get("pending", []):
                room = line.split(":", 1)[0]
                divergences[room] = divergences.get(room, 0) + 1
        return {
            "enabled": self._config.enabled,
            "file": str(self._path),
            "records_written": self.written,
            "cycles_in_memory": len(self.recent),
            "last_error": self.last_error,
            "divergence_counts": divergences,
        }


def observed_overshoot(records: list[dict]) -> list[tuple[float, float]]:
    """(outdoor, overshoot) for every cycle where a room was told to stop.

    The moment a unit is stopped is the only one that says anything about the
    cut-off: that is the overshoot the controller actually permitted at that
    outdoor temperature. Anything else is a room on its way somewhere.
    """
    out: list[tuple[float, float]] = []
    for index in range(1, len(records)):
        before, after = records[index - 1], records[index]
        outdoor = after.get("outdoor")
        if outdoor is None:
            continue
        for room, now in after.get("rooms", {}).items():
            was = before.get("rooms", {}).get(room)
            if not was:
                continue
            if was.get("mode") and now.get("mode") is None and now.get("overshoot") is not None:
                out.append((float(outdoor), float(now["overshoot"])))
    return out
