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
from datetime import datetime
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
    *,
    intents: dict | None = None,
    surplus: bool = False,
    surplus_reading: bool | None = None,
    away: bool = False,
    open_windows: frozenset[str] = frozenset(),
) -> dict:
    """One cycle, as a flat JSON-safe object.

    ``surplus`` and each room's ``intent`` -- the band as asked for, before
    any banking moved it -- are what let the replay reproduce a banked cycle
    exactly. The commanded ``target`` alone cannot: it is already moved.
    ``surplus_reading`` is the sensor as read, before settling (None for
    unavailable or no sensor), so the settle time can be judged against how
    the real signal behaves. Each room's ``window`` is its lockout sensor as
    read, and ``window_open`` whether that stood it aside, so a replay can
    feed the same input and check the same outcome.
    """
    intents = intents or {}
    return {
        "at": plan.at.isoformat(),
        "outdoor": outdoor,
        "surplus": surplus,
        "surplus_reading": surplus_reading,
        "away": away,
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
                "intent": (
                    [i.heat_to, i.cool_to] if (i := intents.get(command.room)) else None
                ),
                "intent_source": (i.source if (i := intents.get(command.room)) else None),
                "frost": command.frost,
                "window": command.room in open_windows,
                "window_open": command.window_open,
            }
            for command in plan.rooms
        },
        "pending": list(pending),
        "actions": list(actions),
    }


class Telemetry:
    """Appends cycles to a file, keeps the recent ones in memory."""

    def __init__(self, config, directory: Path) -> None:
        """``config`` is a TelemetryConfig; see core.config for the fields."""
        self._config = config
        self._path = directory / self._config.file
        self.recent: list[dict] = []
        self.written = 0
        self.skipped = 0
        self.last_error: str | None = None
        self._last_written: dict | None = None
        self._last_written_at: str | None = None

    @property
    def path(self) -> Path:
        return self._path

    def remember(self, record: dict) -> None:
        """Keep it in memory. Cheap, and always on -- diagnostics need it."""
        self.recent.append(record)
        if len(self.recent) > _RING:
            del self.recent[: len(self.recent) - _RING]

    def interesting(self, record: dict) -> tuple[bool, str]:
        """Is this cycle worth a line, and why?

        Recording every tick is what makes a telemetry file useless: five
        rooms at a 30 second tick is roughly 5 MB a day, so any sane file size
        holds days, and the curves need a season. Almost all of those records
        are identical.

        So: every change worth having, plus enough of a room's drift to give
        the coast its resolution, plus a heartbeat so a quiet file means a
        quiet system rather than a broken recorder.
        """
        if not self._config.on_change:
            return True, "recording every tick"

        previous = self._last_written
        if previous is None:
            return True, "first record"

        for group, now in record.get("groups", {}).items():
            before = previous.get("groups", {}).get(group, {})
            if before.get("mode") != now.get("mode"):
                return True, f"{group} changed mode"

        for room, now in record.get("rooms", {}).items():
            before = previous.get("rooms", {}).get(room)
            if before is None:
                return True, f"{room} appeared"
            if before.get("mode") != now.get("mode"):
                return True, f"{room} started or stopped"
            if before.get("wants") != now.get("wants"):
                return True, f"{room} changed what it wants"
            if before.get("target") != now.get("target"):
                return True, f"{room} target changed"
            # Neither of these need move the mode or target -- an idle room
            # standing aside changes neither -- and a replay needs to see the
            # sensor flip when it happened to reproduce the grace.
            if before.get("window") != now.get("window"):
                return True, f"{room} window opened or closed"
            if before.get("window_open") != now.get("window_open"):
                return True, f"{room} stood aside or rejoined"

            was, is_now = before.get("measured"), now.get("measured")
            if (was is None) != (is_now is None):
                return True, f"{room} reading appeared or vanished"
            if (
                was is not None
                and is_now is not None
                and abs(is_now - was) >= self._config.movement
            ):
                # This is the one that matters for the curves: a room drifting
                # after it was switched off.
                return True, f"{room} moved {abs(is_now - was):.2f}°"

        # Whether to bank is a decision in its own right, and in cooling season
        # it can change without moving any recorded target. The raw reading is
        # kept too: how often it flips is what the settle time is tuned on.
        if previous.get("away") != record.get("away"):
            return True, "away changed"
        for room, now in record.get("rooms", {}).items():
            before = previous.get("rooms", {}).get(room) or {}
            if before.get("intent_source") != now.get("intent_source"):
                return True, f"{room} target source changed"
        if previous.get("surplus") != record.get("surplus"):
            return True, "surplus changed"
        if previous.get("surplus_reading") != record.get("surplus_reading"):
            return True, "surplus sensor changed"

        if previous.get("pending") != record.get("pending"):
            return True, "the divergence from the hardware changed"

        if self._heartbeat_due(record.get("at")):
            return True, "heartbeat"

        return False, "nothing changed"

    def _heartbeat_due(self, at: object) -> bool:
        if self._last_written_at is None or not isinstance(at, str):
            return True
        try:
            elapsed = datetime.fromisoformat(at) - datetime.fromisoformat(
                self._last_written_at
            )
        except ValueError:
            return True
        return elapsed >= self._config.heartbeat

    def write_many(self, records: list[dict]) -> None:
        """Append several records in one trip to the disk.

        Called from an executor thread. Buffering and writing in batches is
        not about throughput -- it is so that a busy or starved executor pool
        delays the *record* rather than the control loop that produced it.
        """
        for record in records:
            self.write(record)

    def write(self, record: dict) -> None:
        """Append one line. Called in an executor -- this touches the disk.

        A telemetry failure must never take the controller down with it: the
        house staying warm matters more than the record of why.
        """
        if not self._config.enabled:
            return

        worth_it, why = self.interesting(record)
        if not worth_it:
            self.skipped += 1
            return
        record = {**record, "why_recorded": why}

        try:
            self._rotate_if_needed()
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
            self.written += 1
            self._last_written = record
            self._last_written_at = record.get("at")
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
            "ticks_skipped": self.skipped,
            "on_change": self._config.on_change,
            "cycles_in_memory": len(self.recent),
            "last_error": self.last_error,
            "divergence_counts": divergences,
        }


def observed_overshoot(records: list[dict]) -> list[tuple[float, float]]:
    """(outdoor, overshoot) for every cycle where a room was told to stop.

    The moment a unit is stopped is the only one that says anything about the
    cut-off: that is the overshoot the controller actually permitted at that
    outdoor temperature. Anything else is a room on its way somewhere -- and
    so is a room stopped for an open window, which says nothing about where
    the cut-off sits.
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
            if now.get("window_open"):
                continue
            if was.get("mode") and now.get("mode") is None and now.get("overshoot") is not None:
                out.append((float(outdoor), float(now["overshoot"])))
    return out
