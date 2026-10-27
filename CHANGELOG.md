# Changelog

## 0.1.1 — 2026-10-27

First version to have actually run inside Home Assistant. All three fixes
came from a live log rather than from testing.

**Fixed**

- `intent.py` renamed to `comfort.py`. `intent` is a reserved name: Home
  Assistant's voice-assistant component scans every integration for a module
  called `intent` and calls `async_setup_intents` on it, so the comfort-intent
  resolver was loaded as a voice platform and raised `AttributeError` on every
  start. A test now fails the build if any module takes a reserved name.
- Volatile attributes are no longer recorded. `reason` is prose and `measured`
  moves continuously, so recording them wrote a database row every tick on
  sensors that genuinely change a few times an hour. States, and the
  `overshoot` sensor built for history, are unaffected.
- Telemetry writes moved off the control loop. They went through Home
  Assistant's shared executor, which one badly behaved integration can occupy
  entirely for a minute at a time — making the thermostat's responsiveness
  depend on it. Records are now buffered, batched and flushed by a background
  task, bounded at 240 cycles, with queued and dropped counts in diagnostics.

**Changed**

- Cycles are recorded on change rather than on every tick. The old behaviour
  was 4.9 MB a day, so the file held five days while the participation curves
  need a season — it would have rotated away the data it was collecting. A
  line is written when a mode, target, want or divergence changes, or a room's
  temperature moves more than a threshold (which is what gives a room's coast
  after switch-off its resolution), plus a heartbeat. About 0.13 MB a day.
- The cooling lockout is 20 °C and the heating lockout 24 °C, from HARVEST §E.
  The setpoint range is 16–26: it was given as 18–26, which could not hold
  alongside a 16 °C away target, because the setback would have been clamped
  to 18 and silently never taken effect.
- Comfort targets are configurable: default 20 °C, away 16 °C.

**Added**

- Cross-field validation at load time: a comfort target outside the setpoint
  range, and lockouts that cross over to leave an outdoor band where neither
  mode is permitted, are both refused with the number to change.
- A startup warning naming every configured entity that does not exist.

## 0.1.0 — 2026-10-26

First installable release. Ships in shadow mode: the plan is computed and
published every tick and nothing is written to any unit.

- The decision layer: `core/model.py`, `core/planner.py`, `core/policy.py` —
  standard library only, no Home Assistant, so every scenario that is slow or
  seasonal in a building is a unit test that runs in microseconds.
- The integration: coordinator, reconciler as sole writer, climate, sensor and
  binary_sensor platforms, diagnostics, and services (`explain`,
  `set_room_target`, `clear_override`, `reload`).
- Configuration is `multisplit.yaml`, generated from `questionnaire.yaml`,
  editable from the command line or through the Configure form.
- Telemetry, and `tools/fit_curves.py` to read it back into a suggested curve.
