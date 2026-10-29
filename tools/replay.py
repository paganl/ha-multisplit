#!/usr/bin/env python3
"""Replay recorded telemetry through the planner, optionally changing the facts.

    python3 tools/replay.py multisplit-telemetry.jsonl
    python3 tools/replay.py telemetry.jsonl --outdoor 21.5
    python3 tools/replay.py telemetry.jsonl --outdoor 21.5 --freshness availability
    python3 tools/replay.py telemetry.jsonl --surplus on

Each telemetry record carries what the planner saw: the outdoor temperature
and, per room, its target, measured temperature and staleness. Fed back
through ``plan()`` with the same state threading the coordinator does, that
reproduces the decisions -- and with one fact changed, shows what *would*
have happened. That is how a heat-against-cool conflict can be tested in
October.

Open loop, and only honest because the log was taken in shadow mode: the
recorded temperatures were not caused by the controller's decisions, so
replaying different decisions against them is fair. A forced outdoor
temperature does not change how the rooms would have behaved in that weather;
it changes only what the controller is permitted to do.

``--surplus on|off`` forces the solar surplus signal for every record, to
see which decisions banking would change. Like ``--outdoor`` it is open
loop: it shows what the controller would have *asked for*, not how warm the
rooms would then have become -- that has to come from running it.

``--freshness`` decides what "stale" means during replay:

* ``recorded`` -- use the stale flag exactly as logged.
* ``availability`` -- stale only when there was no reading at all. This
  approximates a freshness test based on whether the sensor is still
  reporting, rather than on whether its value has changed.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "custom_components" / "multisplit"))

import yaml  # noqa: E402

from core.config import load  # noqa: E402
from core.model import COOL, HEAT, GroupState, RoomIntent, RoomMeasurement  # noqa: E402
from core.planner import plan as make_plan  # noqa: E402
from core.policy import constraints_for  # noqa: E402


def replay(records, config, *, outdoor=None, freshness="recorded", surplus=None):
    """Yield (record, plan, constraints) for every record, threading state."""
    runtime: dict = {}
    group_state = {g.key: GroupState(g.key) for g in config.groups}
    allowed_previously: dict = {}
    rooms = [r.as_room_config() for r in config.rooms]
    known = {r.room for r in rooms}

    for record in records:
        now = datetime.fromisoformat(record["at"])
        out = record["outdoor"] if outdoor is None else outdoor
        solar = record.get("surplus", False) if surplus is None else surplus

        measurements, intents = {}, {}
        for key, room in record["rooms"].items():
            if key not in known:
                continue
            value = room["measured"]
            stale = room["stale"] if freshness == "recorded" else value is None
            measurements[key] = RoomMeasurement(
                key, value, stale=stale, source=room.get("source", "replay")
            )
            if room.get("intent"):
                # Recorded since 0.4.0: the band as asked for, before banking.
                heat_to, cool_to = room["intent"]
                intents[key] = RoomIntent(key, heat_to, band=max(0.0, cool_to - heat_to))
            else:
                band = max(0.0, (room.get("cool_to") or room["target"]) - room["target"])
                intents[key] = RoomIntent(key, room["target"], band=band)

        limits = {
            g.key: constraints_for(
                g.policy,
                outdoor=out,
                previously_allowed=allowed_previously.get(g.key),
                solar=solar,
            )
            for g in config.groups
        }
        allowed_previously = {k: v.allowed_modes for k, v in limits.items()}

        result = make_plan(
            rooms=rooms,
            measurements=measurements,
            intents=intents,
            constraints=limits,
            runtime=runtime,
            group_state=group_state,
            outdoor=out,
            now=now,
        )

        runtime = {r.room: r for r in result.runtime}
        for decision in result.groups:
            if group_state[decision.group].mode != decision.mode:
                group_state[decision.group] = GroupState(decision.group, decision.mode, now)

        yield record, result, limits


def _summary(records, config, args) -> None:
    surplus = {"on": True, "off": False}.get(args.surplus)
    rows = list(
        replay(
            records, config, outdoor=args.outdoor, freshness=args.freshness, surplus=surplus
        )
    )
    groups = [g.key for g in config.groups]

    if args.outdoor is None and args.freshness == "recorded" and surplus is None:
        same = sum(
            all(r["groups"][g]["mode"] == p.group(g).mode for g in groups)
            for r, p, _ in rows
        )
        print(f"fidelity: {same}/{len(rows)} records reproduce the logged group modes\n")

    for g in groups:
        modes = [p.group(g).mode for _, p, _ in rows]
        flips = sum(1 for a, b in zip(modes, modes[1:]) if a != b)
        print(f"group {g}: {flips} mode changes, {dict(collections.Counter(modes))}")

    # Heat against cool inside one group, with both modes permitted.
    print("\nconflicts (heat and cool wanted in one group, both permitted):")
    seen = 0
    for record, result, limits in rows:
        for g in groups:
            if {HEAT, COOL} - limits[g].allowed_modes:
                continue
            members = [r for r in result.rooms if r.room in {s.key for s in config.rooms_in(g)}]
            wanted = {r.wants for r in members}
            if {HEAT, COOL} <= wanted:
                seen += 1
                if seen <= args.show:
                    d = result.group(g)
                    print(f"  {record['at'][5:19]} {g}: {d.mode} — {d.because}")
                    for r in members:
                        print(f"      {r.room:12} wants {r.wants!s:5} → {r.mode!s:5} {r.because}")
    print(f"  {seen} conflicted group-cycles in total")

    # Rooms dropped mid-cycle for lack of a trustworthy reading.
    dropped = collections.Counter()
    previous = {}
    for record, result, _ in rows:
        for r in result.rooms:
            was = previous.get(r.room)
            if was == HEAT and r.mode is None and "sensor is stale" in r.because:
                m = record["rooms"][r.room]
                if m["measured"] is not None and m["measured"] < m["target"]:
                    dropped[r.room] += 1
            previous[r.room] = r.mode
    print(f"\nheating withdrawn on staleness while below target: {dict(dropped) or 'none'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("telemetry", type=Path)
    parser.add_argument("--config", type=Path, default=ROOT / "multisplit.yaml")
    parser.add_argument("--outdoor", type=float, help="force this outdoor temperature")
    parser.add_argument(
        "--freshness", choices=("recorded", "availability"), default="recorded"
    )
    parser.add_argument(
        "--surplus", choices=("on", "off"), help="force the solar surplus signal"
    )
    parser.add_argument("--show", type=int, default=5, help="conflicts to print")
    args = parser.parse_args()

    config = load(yaml.safe_load(args.config.read_text()))
    records = [json.loads(line) for line in args.telemetry.read_text().splitlines() if line]
    _summary(records, config, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
