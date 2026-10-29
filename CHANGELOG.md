# Changelog

## 0.3.1 — 2026-10-29

**Fixed**

- **Upgrading no longer breaks a configuration that loaded before.** A file
  written before 0.2.0 failed to load on 0.3.0 with `defaults.intent: missing
  required key 'band'`, though the behaviour it had always had is exactly a
  band of zero. Meanwhile `stale_grace_minutes`, added in 0.3.0, quietly fell
  back to a value in the code, so the two new keys disagreed about the rule.

  Both now follow one rule. A missing key is still an error, except a key
  added after a release, which takes the default it has had since: those are
  listed in one table (`ADDED_SINCE` in `core/config.py`), and each one filled
  in is logged as a warning naming the line to add, e.g.
  `defaults.intent.band is not set; using 0, the default since 0.2.0. Add
  'band: 0' to make it explicit.` No default is silent.

## 0.3.0 — 2026-10-29

Fixes found in the first shadow-mode telemetry (27–29 October 2026).

**Fixed**

- **Group mode is arbitrated on demand a room would act on.** A room 0.5°
  below target wanted heat (deadband 0.3) and chose its group's mode, then
  declined to run because it was inside its resume band (0.5). Group A
  switched to heat on Ischa alone 188 times without Ischa ever running, and
  once cooling is permitted the same vote would deny a room that *would* run
  in the other mode. A room not already running in a mode now votes for it
  only once it has cleared its resume band; it also stops accruing
  starvation while it waits inside it. Replaying the telemetry, Group A's
  mode changes fall from 116 to 4.
- **A sensor going quiet mid-cycle no longer abandons a running room.** A
  room that is idle still never starts on an untrustworthy reading. One
  already running now keeps its last decision for `stale_grace_minutes`
  (default 20) — still stopping at its cut-off on the last value seen — and
  then fails safe. Previously Erin was dropped while heating at 18° against
  20°.
- **Freshness is time since the sensor last reported, not since its value
  changed.** Every stale flag in the telemetry came on exactly 30 minutes
  after the value last changed, with a reading present 88–100% of the time:
  a 0.5°-resolution return-air sensor in a steady room was stale for being
  steady (the office for 68% of the log). `last_reported` is now used where
  Home Assistant provides it.
- **An unknown outdoor temperature no longer lifts the cooling lockout.**
  The group chose cool at 13° outdoor on the two cycles the sensor dropped
  out. Unknown now keeps the previous cycle's lockouts; at start-up, with no
  previous answer, cooling is refused and heating permitted.

**Added**

- `tools/replay.py` replays telemetry through the planner, optionally with a
  forced outdoor temperature (`--outdoor`) or a different freshness rule
  (`--freshness availability`). It reproduces all 3,238 recorded decisions
  exactly, which is what makes it a fair way to test the heat-against-cool
  conflict before the weather allows it.

## 0.2.0 — 2026-10-28

**Added**

- **A comfort band.** A room can be given a range rather than a point: heat
  below 20, cool above 23, do not fuss in between. `defaults.intent.band` sets
  it, and a room may override it with its own `band`.

  This is the honest version of setting a unit to its own dual setpoint. A
  unit set to a band decides heat or cool for itself, from its own room, with
  no sight of the others sharing the compressor — a decision it has no
  business taking on a multi-split, where choosing costs every other room its
  mode. Expressed as intent, the same thing is arbitrated by the one component
  that can see the whole group, against the lockouts and the duty cycle, with
  a reason.

  Heating aims at the bottom of the band and cooling at the top, so neither
  drives the room further into it than asked. The cut-off is measured from the
  end being worked towards, so heating stops 1.5 °C above the *bottom*. Demand
  is the distance outside the band, so a room 1 °C short of a wide band is
  exactly as needy as one 1 °C short of a narrow one.

  The band also absorbs the overshoot tolerance rather than adding to it: a
  band wider than the tolerated overshoot already provides that separation,
  and stacking them would count it twice.

  **A band of zero is a single setpoint and behaves exactly as before**, which
  is the shipped default.

## 0.1.10 — 2026-10-27

**Added**

- `dashboard.yaml` is now a complete view rather than loose cards: a tile per
  room for control, a thermostat dial, the markdown card explaining every
  decision, and the outdoor units with their divergence counts.

  The tiles set `state_content: [hvac_action, current_temperature]`. A climate
  tile shows its hvac *mode* by default, which here is always `auto` — so the
  face of the card said nothing. `hvac_action` is what that room's unit is
  actually doing, and it can honestly be `idle` while its group heats.

## 0.1.9 — 2026-10-27

**Fixed**

- A room whose wanted mode is locked out was told "the group is heating".
  True, but not the binding constraint: it would be standing by whatever the
  group were doing. It now names the lockout, which is the difference between
  waiting for a turn and waiting for the weather.
- That room also accrued starvation for ever. Aging exists so a starved room
  eventually wins; a room whose mode is locked out cannot win at any weight,
  so the clock only produced an unbounded number that meant nothing. It now
  runs for genuine contention only.

**Added**

- `dashboard.yaml`: cards built from the integration's own entities, which
  show each group's mode with its reasoning and each room's status, target
  against measured, and why it is doing what it is doing.

  The old `multisplit-climate-card.js` predates this integration. Its mode
  request buttons call a script that no longer exists, so a request sits on
  "requested" for ever and cannot be cancelled or reversed — it is waiting
  for an answer from a system that has been deleted. There is nothing to
  request now: a room that wants the other mode accumulates weight until it
  wins. Remove that card rather than wiring it up.

## 0.1.8 — 2026-10-27

**Fixed**

- `Unable to remove unknown job listener` on every reload. The deferred
  startup check listened for `homeassistant_started` with a once-listener and
  registered its unsubscribe as an unload callback. A once-listener removes
  itself when it fires, so unloading afterwards asked Home Assistant to remove
  a listener it no longer had.
- The `via_device` deprecation introduced in 0.1.6. Linking a room's device to
  its outdoor unit from an entity's `DeviceInfo` goes through a path Home
  Assistant is removing in 2027.8. Both devices are now created during setup,
  where the parent's registry id exists, and linked with `via_device_id`;
  entities carry identifiers and nothing else.

## 0.1.7 — 2026-10-27

**Added**

- **A unit set to a temperature range is now detected and named**, at startup
  and in diagnostics, instead of failing silently. The reconciler reads and
  writes a single `temperature`; a dual-setpoint unit exposes
  `target_temp_low` and `target_temp_high` and no `temperature` at all, so
  the comparison reported a difference on every cycle and the write was
  refused on every attempt — for ever, quietly, with the divergence sensor
  permanently wrong.

  It is not worked around, because the clash is not really about service
  calls. Choosing between heating and cooling is the decision this system
  exists to take on behalf of every room sharing an outdoor unit, precisely
  because a room cannot see what the others need. A unit making that choice
  privately, from a band, is a second controller, and the two can only agree
  by accident. Writing a band would mean inventing how wide it should be —
  and a band wide enough to be accepted is wide enough for the unit to start
  deciding again.

## 0.1.6 — 2026-10-27

**Fixed**

- **A room that had never run was made to serve a rest period.** At startup
  the climate entities are not registered yet, so the first cycles see no
  temperature and every room stands by. The planner stamped the participation
  clock on those cycles, so when readings arrived each room looked as though
  it had just stopped and had to sit out a full rest — 25 minutes in mild
  weather — before it could heat. After every restart. `since` is now set
  only by a real transition, which is what `RoomRuntime.elapsed` already
  assumed.

**Changed**

- **Each room is its own device**, linked to its outdoor unit with
  `via_device`, instead of every room entity hanging off the compressor it
  shares. `climate.group_b_office` becomes `climate.office`, and
  `sensor.group_a_living_room_status` becomes `sensor.living_room_status`.
  Home Assistant still shows the rooms nested under the unit that constrains
  them, which is the relationship worth modelling — it just stops putting the
  unit's name in every room's id.

  Home Assistant keeps an entity id once assigned, so an existing install
  keeps its old ids. To pick up the new ones, delete the integration from
  Settings → Devices & services and add it again; nothing is lost, because
  all configuration lives in `multisplit.yaml`.

## 0.1.5 — 2026-10-27

**Fixed**

- The missing-entity check ran at setup and reported every room. Home
  Assistant does not order integration startup, so when this one came up the
  integration providing the climate entities had often registered none of
  them — and the check said all five were absent. Alarming, wrong, and worse
  than silence, because it teaches you to ignore the warning that will one
  day be right. It now waits for Home Assistant to finish starting and then
  for a grace period derived from the tick.

  The giveaway in the report was that not one of the five carried a "did you
  mean" suggestion: with an empty climate domain there was nothing to compare
  against.

  Nothing depended on the check. `measurement.collect` re-reads every entity
  every cycle, so a room whose entity appears late starts working on its own;
  the warning only ever explained one that never does.

## 0.1.4 — 2026-10-27

**Fixed**

- Reconfiguring the groups or rooms section corrupted its own answer.
  Validating a `records` answer returned the shape the *configuration* wants
  — entries keyed by id — and that was written back into
  `questionnaire.yaml` as the answer. It is not a valid answer, so the field
  showed the wrong thing and the next edit of that section failed with
  "expected a non-empty list of entries". Checking an answer and
  transforming it are now separate: `validate` keeps the authored list,
  `to_config` keys it for the generated file.
- A questionnaire already damaged by that bug repairs itself: a keyed mapping
  where a list belongs is accepted and normalised on read.
- Group and room fields in the reconfigure form were rendered as one long
  line of inline YAML. They are now block style, one entry per line group,
  while curve points stay inline as the `[outdoor, value]` pairs they are.

## 0.1.3 — 2026-10-27

**Fixed**

- The missing-entity warning said "1 configured entity do not exist". The
  noun was pluralised and the verb was not.

**Added**

- That warning now suggests the id the entity was probably meant to be. On
  the instance this came from, a room was configured as
  `…_ischa_climat` where every other room ends in `climate`: one character,
  and that room silently never got a temperature. Matching is on absolute
  character difference rather than a similarity ratio, because these ids are
  long and almost entirely boilerplate — two completely different rooms score
  over 0.85 similar, so a ratio would confidently suggest the wrong room.

## 0.1.2 — 2026-10-27

Upgrading from 0.1.0 failed, because the configuration file is versioned
with the code and nothing said so.

**Fixed**

- A missing key was reported twice — once as missing, once as failing the
  range check that followed — so a file with three things wrong with it
  reported four problems that read as unrelated faults. Range checks are now
  part of reading the value.
- Configuration problems are logged one per line before the exception is
  raised. A multi-line exception message is the first thing a log viewer
  collapses, and the list is the only useful part: a count of four with the
  four lines missing tells nobody anything.
- `priority: 0` was silently becoming `1.0` instead of being rejected.

**Added**

- When required keys are absent rather than wrong, the error says that this
  usually means the file was generated by an earlier version, and gives the
  two ways to regenerate it.

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
