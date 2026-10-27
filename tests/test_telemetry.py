"""Telemetry has to survive its own failures and produce a usable curve.

Two things are asserted here. First, that recording a cycle cannot break the
cycle — a house staying warm matters more than the record of why. Second, that
what is written can be read back into the numbers the questionnaire wants,
because telemetry nobody can fit a curve from is just disk usage.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import fit_curves  # noqa: E402

from core.config import load  # noqa: E402
from core.model import (  # noqa: E402
    HEAT,
    GroupDecision,
    Plan,
    RoomCommand,
    RoomMeasurement,
)
from telemetry import Telemetry, observed_overshoot, record_for  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG = load(yaml.safe_load((ROOT / "multisplit.yaml").read_text(encoding="utf-8")))
NOW = datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)


def cycle(*, at, outdoor, measured, target, mode, pending=(), group_mode=...):
    """One cycle for one room. ``group_mode`` defaults to the room's own mode;
    pass it explicitly to model a room stopping while its group keeps running,
    which is what a multi-room group actually does."""
    if group_mode is ...:
        group_mode = mode
    plan = Plan(
        groups=(GroupDecision("a", group_mode, "because reasons", {"heat": 1.0}, ()),),
        rooms=(RoomCommand("living_room", mode, target, "because reasons", wants=None),),
        at=at,
    )
    measurements = {"living_room": RoomMeasurement("living_room", measured, source="test")}
    return record_for(plan, measurements, outdoor, tuple(pending), (), False)


# --------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------


def test_a_record_carries_everything_needed_to_refit_a_curve():
    record = cycle(at=NOW, outdoor=7.0, measured=22.5, target=21.0, mode=HEAT)
    room = record["rooms"]["living_room"]
    assert record["outdoor"] == 7.0
    assert (room["target"], room["measured"]) == (21.0, 22.5)
    assert room["overshoot"] == 1.5, "the derived figure the curve is fitted against"
    assert room["because"]


def test_a_record_is_json_serialisable():
    """It is written as one line of JSON, so it had better be."""
    record = cycle(at=NOW, outdoor=7.0, measured=22.5, target=21.0, mode=HEAT)
    assert json.loads(json.dumps(record))["outdoor"] == 7.0


def test_a_missing_measurement_does_not_break_the_record():
    plan = Plan(
        groups=(GroupDecision("a", None, "off", {}, ()),),
        rooms=(RoomCommand("living_room", None, 21.0, "standing by"),),
        at=NOW,
    )
    record = record_for(plan, {}, None, (), (), False)
    assert record["rooms"]["living_room"]["overshoot"] is None
    assert record["outdoor"] is None


# --------------------------------------------------------------------------
# Writing it
# --------------------------------------------------------------------------


def every_tick(config):
    """The configuration with change-triggering off, for volume tests."""
    from dataclasses import replace

    return replace(config, on_change=False)


def test_cycles_are_appended_one_json_object_per_line(tmp_path):
    telemetry = Telemetry(every_tick(CONFIG.telemetry), tmp_path)
    for minute in range(3):
        telemetry.write(cycle(at=NOW + timedelta(minutes=minute), outdoor=7.0, measured=21.0, target=21.0, mode=HEAT))

    lines = telemetry.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    assert all(json.loads(line)["outdoor"] == 7.0 for line in lines)
    assert telemetry.written == 3


# --------------------------------------------------------------------------
# Deciding what is worth a line
# --------------------------------------------------------------------------


def test_identical_ticks_are_not_written_twice(tmp_path):
    """Recording every tick is what makes the file useless: 5 MB a day, so any
    sane file size holds days, and the curves need a season."""
    telemetry = Telemetry(CONFIG.telemetry, tmp_path)
    for minute in range(10):
        telemetry.write(
            cycle(at=NOW + timedelta(minutes=minute), outdoor=7.0, measured=21.0, target=21.0, mode=HEAT)
        )

    assert telemetry.written == 1, "the first one, then nothing changed"
    assert telemetry.skipped == 9


def test_a_room_starting_or_stopping_is_always_written(tmp_path):
    telemetry = Telemetry(CONFIG.telemetry, tmp_path)
    telemetry.write(cycle(at=NOW, outdoor=7.0, measured=22.5, target=21.0, mode=HEAT))
    telemetry.write(
        cycle(
            at=NOW + timedelta(seconds=30), outdoor=7.0, measured=22.5, target=21.0,
            mode=None, group_mode=HEAT,
        )
    )

    assert telemetry.written == 2
    last = json.loads(telemetry.path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert last["why_recorded"] == "living_room started or stopped"


def test_a_group_changing_mode_is_always_written(tmp_path):
    telemetry = Telemetry(CONFIG.telemetry, tmp_path)
    telemetry.write(cycle(at=NOW, outdoor=7.0, measured=21.0, target=21.0, mode=HEAT))
    telemetry.write(
        cycle(at=NOW + timedelta(seconds=30), outdoor=7.0, measured=21.0, target=21.0, mode=None)
    )
    last = json.loads(telemetry.path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert last["why_recorded"] == "a changed mode"


def test_a_drifting_room_is_recorded_so_the_coast_has_resolution(tmp_path):
    """The measurement fit_curves needs most, and the one a change-only
    recorder would otherwise miss entirely."""
    telemetry = Telemetry(CONFIG.telemetry, tmp_path)
    telemetry.write(cycle(at=NOW, outdoor=7.0, measured=22.5, target=21.0, mode=None))

    telemetry.write(
        cycle(at=NOW + timedelta(seconds=30), outdoor=7.0, measured=22.6, target=21.0, mode=None)
    )
    assert telemetry.written == 1, "0.1° is below the 0.2° movement threshold"

    telemetry.write(
        cycle(at=NOW + timedelta(seconds=60), outdoor=7.0, measured=22.75, target=21.0, mode=None)
    )
    assert telemetry.written == 2
    last = json.loads(telemetry.path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert "moved 0.25" in last["why_recorded"]


def test_a_heartbeat_is_written_even_when_nothing_moves(tmp_path):
    """So a quiet file means a quiet system, not a broken recorder."""
    telemetry = Telemetry(CONFIG.telemetry, tmp_path)
    telemetry.write(cycle(at=NOW, outdoor=7.0, measured=21.0, target=21.0, mode=HEAT))
    telemetry.write(
        cycle(at=NOW + timedelta(minutes=5), outdoor=7.0, measured=21.0, target=21.0, mode=HEAT)
    )
    assert telemetry.written == 1

    telemetry.write(
        cycle(at=NOW + timedelta(minutes=16), outdoor=7.0, measured=21.0, target=21.0, mode=HEAT)
    )
    assert telemetry.written == 2
    last = json.loads(telemetry.path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert last["why_recorded"] == "heartbeat"


def test_a_changed_divergence_is_written(tmp_path):
    """Shadow mode's whole output is the divergence, so it must not be dropped."""
    telemetry = Telemetry(CONFIG.telemetry, tmp_path)
    telemetry.write(cycle(at=NOW, outdoor=7.0, measured=21.0, target=21.0, mode=HEAT))
    telemetry.write(
        cycle(
            at=NOW + timedelta(seconds=30), outdoor=7.0, measured=21.0, target=21.0, mode=HEAT,
            pending=["living_room: off → heat at 21°"],
        )
    )
    assert telemetry.written == 2


def test_change_triggering_cuts_the_volume_by_about_a_tenth(tmp_path):
    """A day of a room warming slowly, at a 30 second tick."""
    telemetry = Telemetry(CONFIG.telemetry, tmp_path)
    every = Telemetry(every_tick(CONFIG.telemetry), tmp_path / "every")
    (tmp_path / "every").mkdir()

    measured = 19.0
    for tick in range(2880):
        at = NOW + timedelta(seconds=30 * tick)
        measured += 0.002  # about 0.24° an hour
        record = cycle(at=at, outdoor=7.0, measured=round(measured, 3), target=21.0, mode=HEAT)
        telemetry.write(record)
        every.write(record)

    assert every.written == 2880
    assert telemetry.written < every.written / 8
    assert telemetry.path.stat().st_size < every.path.stat().st_size / 8


def test_an_unwritable_file_is_reported_but_never_raises(tmp_path):
    """A telemetry failure must not take the controller down with it."""
    telemetry = Telemetry(CONFIG.telemetry, tmp_path / "does" / "not" / "exist")
    telemetry.write(cycle(at=NOW, outdoor=7.0, measured=21.0, target=21.0, mode=HEAT))

    assert telemetry.written == 0
    assert telemetry.last_error is not None
    assert telemetry.summary()["last_error"] is not None


def test_the_file_rotates_rather_than_growing_without_limit(tmp_path):
    from dataclasses import replace

    tiny = replace(CONFIG.telemetry, max_bytes=400)
    telemetry = Telemetry(tiny, tmp_path)
    for minute in range(40):
        telemetry.write(cycle(at=NOW + timedelta(minutes=minute), outdoor=7.0, measured=21.0, target=21.0, mode=HEAT))

    rotated = telemetry.path.with_suffix(telemetry.path.suffix + ".1")
    assert rotated.exists(), "the previous file is kept, so recent history survives"
    assert telemetry.path.stat().st_size < 1000


def test_recent_cycles_are_capped_in_memory(tmp_path):
    telemetry = Telemetry(CONFIG.telemetry, tmp_path)
    for minute in range(300):
        telemetry.remember(cycle(at=NOW + timedelta(minutes=minute), outdoor=7.0, measured=21.0, target=21.0, mode=HEAT))
    assert len(telemetry.recent) == 120


def test_the_summary_counts_divergences_per_room(tmp_path):
    telemetry = Telemetry(CONFIG.telemetry, tmp_path)
    for minute in range(3):
        telemetry.remember(
            cycle(
                at=NOW + timedelta(minutes=minute),
                outdoor=7.0,
                measured=21.0,
                target=21.0,
                mode=HEAT,
                pending=["living_room: off → heat at 21°"],
            )
        )
    assert telemetry.summary()["divergence_counts"] == {"living_room": 3}


# --------------------------------------------------------------------------
# Reading it back
# --------------------------------------------------------------------------


def stop_cycle_records():
    """Running, then stopped at +1.5°, then coasting to +1.9°, then restarting."""
    return [
        cycle(at=NOW, outdoor=7.0, measured=22.3, target=21.0, mode=HEAT),
        cycle(at=NOW + timedelta(minutes=1), outdoor=7.0, measured=22.5, target=21.0, mode=None),
        cycle(at=NOW + timedelta(minutes=5), outdoor=7.0, measured=22.9, target=21.0, mode=None),
        cycle(at=NOW + timedelta(minutes=16), outdoor=7.0, measured=20.4, target=21.0, mode=HEAT),
    ]


def test_only_the_moment_a_unit_stops_tells_you_about_the_cut_off():
    """A room on its way somewhere says nothing about the permitted overshoot."""
    points = observed_overshoot(stop_cycle_records())
    assert points == [(7.0, 1.5)]


def test_the_fitting_tool_recovers_overshoot_coast_and_recovery():
    cycles = fit_curves.extract(stop_cycle_records(), None)
    assert len(cycles) == 1
    found = cycles[0]
    assert found.overshoot_at_stop == pytest.approx(1.5)
    assert found.peak_after_stop == pytest.approx(1.9), "it coasted a further 0.4°"
    assert found.recovery_minutes == pytest.approx(15.0)


def test_the_fitting_tool_suggests_an_earlier_cut_off_when_a_room_coasts(capsys):
    """The coast is the number nobody has, and it is why 1.5 may be too late."""
    fit_curves.summarise(fit_curves.extract(stop_cycle_records(), None))
    printed = capsys.readouterr().out
    assert "cut_off_offset" in printed
    assert "[7.0, 1.1]" in printed, "1.5 permitted minus 0.4 coasted"
    assert "rest_minutes" in printed


def test_the_fitting_tool_says_so_when_there_is_nothing_to_fit(capsys):
    fit_curves.summarise([])
    assert "need cycles, not ticks" in capsys.readouterr().out


def test_the_fitting_tool_can_restrict_to_one_room():
    assert fit_curves.extract(stop_cycle_records(), "bedroom") == []
    assert len(fit_curves.extract(stop_cycle_records(), "living_room")) == 1
