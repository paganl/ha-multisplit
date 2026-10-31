#!/usr/bin/env python3
"""Turn telemetry back into a curve.

    python3 tools/fit_curves.py multisplit-telemetry.jsonl
    python3 tools/fit_curves.py telemetry.jsonl --room living_room

The overshoot cut-off and the rest period are the only values in this system
that cannot be reasoned about: they depend on a particular unit's minimum
capacity against a particular room's heat loss. They were measured once by
living with them. This reads the telemetry back and measures them again.

What it reports, per outdoor temperature band:

* **Observed overshoot** — for every cycle where a unit was stopped, how far
  past target the room actually was. That is the overshoot the controller
  permitted, which is the thing `cut_off_offset` is trying to name.
* **Observed coast** — how much further the room drifted *after* it was
  stopped. This is the number nobody has: if the coast is large, the cut-off
  should come earlier, and in mild weather it may need to come before target
  altogether.
* **Observed recovery** — how long the room took to fall back to its resume
  threshold, which is what `rest_minutes` should be tracking.

It prints a suggested curve in questionnaire.yaml's format. It does not write
anything: a curve fitted from a fortnight of mild weather should be read
before it is trusted.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

#: Outdoor bands to summarise within. Chosen to match the harvested table.
BANDS = ((-99.0, 3.5), (3.5, 10.0), (10.0, 16.0), (16.0, 99.0))
BAND_LABELS = ("cold (< 3.5)", "mild (3.5–10)", "shoulder (10–16)", "warm (> 16)")

#: The outdoor temperature a band's suggested breakpoint is placed at.
BAND_MIDPOINTS = (0.0, 7.0, 13.0, 18.0)


def read(path: Path) -> list[dict]:
    records = []
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"  skipped malformed line {number}", file=sys.stderr)
    return records


def band_of(outdoor: float) -> int | None:
    for index, (low, high) in enumerate(BANDS):
        if low <= outdoor < high:
            return index
    return None


class Cycle:
    """One on-to-off transition for one room."""

    __slots__ = ("room", "outdoor", "overshoot_at_stop", "peak_after_stop", "recovery_minutes")

    def __init__(self, room: str, outdoor: float, overshoot: float) -> None:
        self.room = room
        self.outdoor = outdoor
        self.overshoot_at_stop = overshoot
        self.peak_after_stop = overshoot
        self.recovery_minutes: float | None = None


def extract(records: list[dict], only_room: str | None) -> list[Cycle]:
    """Find every stop, then follow each room until it restarts."""
    cycles: list[Cycle] = []
    open_cycles: dict[str, Cycle] = {}
    stopped_at: dict[str, datetime] = {}

    for index in range(1, len(records)):
        before, after = records[index - 1], records[index]
        outdoor = after.get("outdoor")
        when = _parse(after.get("at"))

        for room, now in after.get("rooms", {}).items():
            if only_room and room != only_room:
                continue
            was = before.get("rooms", {}).get(room)
            if not was:
                continue

            overshoot = now.get("overshoot")
            running_before, running_now = bool(was.get("mode")), bool(now.get("mode"))

            if running_before and not running_now:
                # Stopped for an open window: not a cut-off, so not a cycle.
                if outdoor is None or overshoot is None or now.get("window_open"):
                    continue
                cycle = Cycle(room, float(outdoor), float(overshoot))
                open_cycles[room] = cycle
                stopped_at[room] = when
                cycles.append(cycle)
                continue

            cycle = open_cycles.get(room)
            if cycle is None:
                continue

            if now.get("window") or now.get("window_open"):
                # A window opened while the room was coasting: its drift and
                # recovery are the weather's, not the unit's. Drop the cycle.
                cycles.remove(cycle)
                open_cycles.pop(room, None)
                stopped_at.pop(room, None)
                continue

            if overshoot is not None:
                cycle.peak_after_stop = max(cycle.peak_after_stop, float(overshoot))

            if running_now:
                start = stopped_at.get(room)
                if start and when:
                    cycle.recovery_minutes = (when - start).total_seconds() / 60
                open_cycles.pop(room, None)
                stopped_at.pop(room, None)

    return cycles


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def summarise(cycles: list[Cycle]) -> None:
    if not cycles:
        print("No complete stop-and-restart cycles in this telemetry yet.")
        print("The curves need cycles, not ticks — leave it running for a few days.")
        return

    print(f"{len(cycles)} cycles across {len({c.room for c in cycles})} room(s)\n")
    print(f"{'band':<18}{'n':>4}{'overshoot':>12}{'coast':>9}{'recovery':>11}")
    print("-" * 54)

    suggested_cut_off: list[tuple[float, float]] = []
    suggested_rest: list[tuple[float, float]] = []

    for index, label in enumerate(BAND_LABELS):
        in_band = [c for c in cycles if band_of(c.outdoor) == index]
        if not in_band:
            print(f"{label:<18}{0:>4}{'—':>12}{'—':>9}{'—':>11}")
            continue

        overshoot = statistics.median(c.overshoot_at_stop for c in in_band)
        coast = statistics.median(c.peak_after_stop - c.overshoot_at_stop for c in in_band)
        recoveries = [c.recovery_minutes for c in in_band if c.recovery_minutes is not None]
        recovery = statistics.median(recoveries) if recoveries else None

        print(
            f"{label:<18}{len(in_band):>4}{overshoot:>+11.2f}°"
            f"{coast:>+8.2f}°"
            + (f"{recovery:>10.0f}m" if recovery is not None else f"{'—':>11}")
        )

        # The cut-off that would have produced no coast past where the room
        # ended up: stop earlier by however far it drifted afterwards.
        suggested_cut_off.append((BAND_MIDPOINTS[index], round(overshoot - coast, 2)))
        if recovery is not None:
            suggested_rest.append((BAND_MIDPOINTS[index], round(recovery)))

    print("\nSuggested curves, for questionnaire.yaml:\n")
    print("cut_off_offset:")
    for outdoor, value in suggested_cut_off:
        print(f"  - [{outdoor}, {value}]")
    if suggested_rest:
        print("rest_minutes:")
        for outdoor, value in suggested_rest:
            print(f"  - [{outdoor}, {value}]")

    negative = [v for _, v in suggested_cut_off if v < 0]
    if negative:
        print(
            "\nNote: a negative cut-off means stopping before the room reaches "
            "target, because minimum capacity plus coil residual carries it "
            "past anyway. That is anticipatory cut-off, and this is the "
            "evidence for it."
        )
    print(
        "\nRead these before trusting them. A curve fitted from one fortnight "
        "of one season describes that fortnight."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("telemetry", type=Path, help="the .jsonl file")
    parser.add_argument("--room", help="restrict to one room")
    args = parser.parse_args()

    if not args.telemetry.exists():
        print(f"error: {args.telemetry} does not exist", file=sys.stderr)
        return 1

    records = read(args.telemetry)
    print(f"read {len(records)} cycles from {args.telemetry}")
    summarise(extract(records, args.room))
    return 0


if __name__ == "__main__":
    sys.exit(main())
