# What we build in Home Assistant

Companion to [DESIGN-v3.md](DESIGN-v3.md). That document says *what the
architecture is*; this one says *what gets written, in what order, and how each
piece is proved before the next one starts*.

---

## 0. The shape of it

**One custom integration. No YAML packages, no blueprints, no helper entities.**

Everything in sections 1–7 of the design lives in
`custom_components/multisplit/`. Automations disappear entirely — the
coordinator ticks on a timer and the reconciler converges; there is nothing
left for an automation to trigger.

**Configuration is YAML, and the YAML is generated from a questionnaire.**
`questionnaire.yaml` is the only file anyone edits: it holds each question in
the words it was asked, the answer as given, and `why` — what that number
actually does. `tools/build_config.py` turns the answers into
`multisplit.yaml` (curves, seconds, nested keys) and carries the question text
across as comments, so the config explains itself without two documents to
keep in step. `--ask` walks through whatever is unanswered; `--check` fails if
the generated file has drifted, which CI runs on every push.

Nothing in the Python holds a threshold, a curve, an entity id or a room name.
A missing key is an error, not a silent default — a hidden default is
indistinguishable from a hard-coded value. Where an answer is genuinely
missing, the questionnaire states an `assume` and the generated line is marked
UNMEASURED, so assumptions are visible rather than buried.

Three things stay native Home Assistant and are *consumed*, not rebuilt:

| What | Source | Required? |
| --- | --- | --- |
| Outdoor temperature | existing `sensor.*` or a `weather.*` entity | yes — policy depends on it |
| Per-room schedules | HA `schedule.*` entities, one per room | optional (falls back to a default target) |
| Presence / occupancy | `person.*`, `device_tracker.*`, or a motion sensor | optional (feeds `priority`) |

Reusing `schedule.*` matters: the schedule editor in the HA UI is already
good, and schedule is exactly the kind of thing the user should be able to
change without touching this integration.

---

## 1. The build list

### Pure core — no Home Assistant imports

These are plain Python. They can be written and fully tested before Home
Assistant is involved at all.

| File | Owns | Proved by |
| --- | --- | --- |
| `model.py` | the frozen dataclasses in design §3 | nothing to test — no behaviour |
| `planner.py` | group mode scoring, inertia, aging, participation duty cycle | `tests/test_planner.py` — a scenario table |
| `policy.py` | `Constraints` from config + outdoor temperature, with reasons | unit tests at the lockout boundary |

`planner.py` is the heart. It takes `(measurements, intents, constraints,
participation_state, now)` and returns a `Plan`. It touches no I/O, holds no
state, starts no timers.

### Adapters — Home Assistant aware

| File | Owns | Notes |
| --- | --- | --- |
| `measurement.py` | which sensor is authoritative per room, averaging, staleness | a room with no trustworthy reading must produce `stale=True`, not a guess |
| `intent.py` | resolving schedule → override → away → default into `RoomIntent` | overrides stored in integration storage with an expiry, not in helper entities |
| `reconciler.py` | **the only writes to hardware** | idempotent; mode + setpoint always in one `climate.set_temperature` call; per-unit rate limit |
| `coordinator.py` | the tick: gather → plan → reconcile → publish | `DataUpdateCoordinator`, ~30 s interval, plus an immediate refresh on a source-sensor state change |

### Platforms — what the rest of HA sees

| File | Creates |
| --- | --- |
| `climate.py` | one `climate.*` per room; writable target; `hvac_modes` restricted to what is possible *right now* |
| `sensor.py` | per-room status sensor; per-group mode sensor |
| `binary_sensor.py` | per-group, per-mode "allowed" sensors carrying the reason |
| `diagnostics.py` | last N plans with scores and reasons — download from the device page |

### Plumbing

| File | Contents |
| --- | --- |
| `manifest.json` | `domain: multisplit`, `iot_class: local_polling`, no external deps |
| `__init__.py` | setup/unload, platform forwarding, service registration |
| `const.py` | domain, defaults, config keys |
| `yaml_config.py` | reads `multisplit.yaml` at the edge; `core/config.py` validates it |
| `services.yaml` | service schemas (section 3 below) |
| `strings.json` / `translations/en.json` | service labels |

---

## 2. Entity inventory

For two groups and, say, four rooms this is the whole surface area:

```
climate.multisplit_office                  target writable, hvac_modes live
climate.multisplit_lounge
climate.multisplit_bedroom
climate.multisplit_study

sensor.multisplit_office_status            state: active | standby | waiting | no_data
                                           attrs: reason, wants, participating,
                                                  unserved_for, intent_source,
                                                  measured, target, source

sensor.multisplit_group_a_mode             state: heat | cool | off
                                           attrs: reason, scores, since, unserved

binary_sensor.multisplit_group_a_cool_allowed    attrs: reason
binary_sensor.multisplit_group_a_heat_allowed    attrs: reason
```

That is the entity contract from design §7, and it is the *only* thing the UI
is allowed to depend on. Everything else is internal.

No `input_select`, no `input_text`, no `input_number`, no `input_datetime`.
Every one of those in the current system existed to carry state between
automations; with a single process holding the plan, there is nothing to carry.

---

## 3. Services

```yaml
multisplit.set_room_target:    room, temperature, until?   # the override path
multisplit.clear_override:     room
multisplit.set_room_priority:  room, priority              # manual importance
multisplit.set_season:         season: heat | cool | auto   # policy override
multisplit.replan:             -                           # force a tick
multisplit.explain:            room?                       # returns the plan + reasons
```

`explain` returns a service response rather than firing an event — it is the
developer-tools path to "why is this room standing by?", and it is also how
shadow mode (section 6) is inspected.

There is deliberately **no** `request_mode` service. A room expressing that it
wants the other mode is just an intent with a non-zero error; aging decides
when it wins. Nothing to request, nothing to approve, nothing to reject.

---

## 4. What the questionnaire asks

Five sections, seventeen questions, in [questionnaire.yaml](questionnaire.yaml).

| Section | Asks about |
| --- | --- |
| Outdoor temperature | which entity the lockouts and curves are indexed against |
| Outdoor units and rooms | the groups, and each room's climate entity, sensors and group |
| The duty cycle | cut-off, rest period, resume band, minimum run — the measured part |
| What the system is permitted to do | lockouts, hysteresis, season, setpoint range, deadband, overshoot tolerance |
| Changeover resistance | inertia weight, its half-life, aging rate |

Ten are answered from HARVEST.md. Seven are still assumptions, and the
generated config says so on the line:

```
min_on_minutes, heat_above, lockout_hysteresis, deadband,
inertia_weight, inertia_half_life_minutes, aging_rate
```

A room can override any participation value — HARVEST §C3 asks whether any
room needed its own numbers, and the loader merges a room's overrides over the
defaults key by key.

One invariant is checked at load time and refuses the configuration outright:
**the overshoot tolerance must exceed the largest cut-off.** A room parked at
+1.5 °C by the duty cycle must not be read as asking for cooling, or the duty
cycle and the arbitration are permanently opposed and the compressor pays for
it. The error names every room affected and the number to change.

## 5. What gets deleted — and what must be harvested first

Delete:

- `group_a_hvac_mode` / `group_b_hvac_mode` input_selects
- the group controller blueprint and its automations
- the participation optimiser blueprint and its per-unit timers
- the hysteresis blueprint
- `script.multisplit_request_mode`, the resolver automation, and every `msc_*`
  helper
- [ha-package/multisplit_requests.yaml](ha-package/multisplit_requests.yaml) — never deployed, so no loss
- the card's request row

**Harvest before deleting.** Three things in the current system are knowledge,
not code, and cannot be re-derived:

1. **Group membership** — which room is on which outdoor unit. Lives only in
   the blueprint today. → `questionnaire.yaml`, rooms section.
2. **The participation thresholds and timers**, including how they vary with
   outdoor temperature. Measured in the building. → the breakpoint table.
3. **The hysteresis and minimum-runtime values** — not reused directly, but
   they are the only evidence of how fast this hardware can be allowed to
   change its mind. → the starting point for inertia.

Write those down in a file before any blueprint is removed.

---

## 6. Build order

Each phase ends in something observable, and nothing can write to hardware
until phase 2.

**Phase 0 — the planner, offline.** `model.py`, `planner.py`, `policy.py`,
plus a scenario table in `tests/test_planner.py`: both rooms opposed; one room
starved for an hour; outdoor exactly at the lockout; sensor stale; a room
overshooting in mild weather; a room overshooting in cold weather. No Home
Assistant, no house, milliseconds to run.

**Done.** `core/model.py`, `core/planner.py`, `core/policy.py` and 38 tests,
green. The pure core lives in `custom_components/multisplit/core/` so it can
be imported with nothing but the standard library; CI asserts that it never
imports Home Assistant. Two defects surfaced during the write-up and are
recorded in DESIGN-v3.md §4. The participation curves are placeholders until
HARVEST.md §C2 lands.

**Phase 1 — shadow mode.** Integration loads, coordinator ticks, plan is
published to the status and mode sensors, **reconciler disabled**. The old
blueprints keep running the house. Now compare: what would the planner have
done versus what the blueprints actually did? Every divergence is either a bug
in the planner or a behaviour of the old system nobody had written down. This
phase is cheap because the planner is pure, and it is the single highest-value
step in the whole build.

**Phase 2 — reconciler on, one group.** Enable writes for group A only. Old
blueprints disabled for that group, still running group B. A direct A/B in the
same building, same weather.

**Phase 3 — second group, then climate entities and services.** Rooms become
settable through `climate.*`; overrides go through `set_room_target`.

**Phase 4 — delete the old machinery.** Only now, and only after the harvest.

**Phase 5 — the card, if still wanted.** With `hvac_modes` restricted, the
stock HA climate card stops offering impossible modes, so the custom card is
an enhancement rather than a fix. Its remaining job is the one thing stock
cannot do: show *why* — `reason`, `wants`, `unserved_for` — on one line per
room. The existing card loses its request row and most of its state machine
and becomes considerably smaller.

---

## 7. Open decisions

- **Tick interval.** 30 s is a reasonable default; the reconciler's per-unit
  rate limit is what actually protects the compressor, so the tick can be
  fast without risk.
- **Where overrides live.** Integration storage with an expiry is cleaner
  than helper entities, but it is invisible in the UI unless the status sensor
  exposes `intent_source` and `expires_at` — which it should.
- **Whether `auto` season is a thing.** Allowing both modes year-round and
  letting policy decide is more honest, but a seasonal lock is a cheap way to
  stop a sunny February afternoon from asking for cooling.
