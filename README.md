# Multi-split climate control

A Home Assistant integration for a multi-split heat pump: several indoor units
sharing one outdoor unit, which can heat or cool but not both at once.

That shared constraint is the whole problem. A standard thermostat per room
implies each room can have what it asks for, which on this hardware is simply
untrue — and a room that cannot have what it asked for should be able to say
*why*, not sit silently at the wrong temperature.

## What it does

- **Decides one mode per outdoor unit** by weighing every room's demand, with
  resistance to changing its mind and a growing claim for rooms that have gone
  unserved. No request-and-approve protocol: a genuinely starved room wins
  eventually, on its own.
- **Duty-cycles each indoor unit.** A unit on a multi-split cannot modulate
  below its minimum capacity, and compressor speed is set by whichever room is
  working hardest — so a nearly-satisfied room gets more output than it needs
  and overshoots. It is stopped past a measured threshold and rested for a
  period that depends on the outdoor temperature.
- **Refuses what the weather forbids**, with hysteresis so it cannot flap, and
  says so in words rather than just doing nothing. An outdoor sensor that
  drops out never lifts a lockout.
- **Follows each room's own week.** The room's owner sets a schedule on a
  dashboard card: one line per day, a mark wherever the temperature should
  change. Guardrails keep every mark inside the room's allowed range and
  every setting long enough not to short-cycle the compressor.
- **Has an away mode.** One switch drops every room to its setback; a room
  someone turns up by hand still heats.
- **Works to a comfort band, not a point.** Heat below 20, cool above 22, do
  not fuss in between — arbitrated across the whole outdoor unit rather than
  left to each indoor unit's own dual setpoint.
- **Banks solar surplus.** While the house is exporting, rooms heat a little
  further into their band, so the building stores energy that would otherwise
  be sold back cheaply.
- **Fails safe on bad readings.** An idle room never starts on a stale or
  missing temperature; a running one keeps its last decision for a short grace
  and then stops.
- **Explains itself.** Every decision carries the reason produced by the code
  that made it. `multisplit.explain` answers "why is this room standing by?".

## Installation

### HACS

Add this repository as a custom repository (HACS → ⋮ → Custom repositories),
category **Integration**, then install and restart Home Assistant.

**This requires the repository to be public.** HACS cannot install from a
private repository, and its validation action refuses to run against one.
While the repository is private, install manually.

### Manually

Copy `custom_components/multisplit` into your Home Assistant `custom_components`
directory and restart.

### Then

1. Copy `questionnaire.yaml` and `multisplit.yaml` into your Home Assistant
   configuration directory, next to `configuration.yaml`.
2. Add the integration: Settings → Devices & services → Add integration →
   Multi-split climate control. There is nothing to fill in — it reads
   `multisplit.yaml`.
3. It starts in **shadow mode**: the plan is computed and published, but no
   unit is commanded. Leave it there until you have compared what it would
   have done against what your existing automations actually did.

## Configuration

Everything is in `multisplit.yaml`, and that file is generated from
`questionnaire.yaml`. No threshold, curve, entity id or room name appears in
the Python.

`questionnaire.yaml` holds each question in the words it was asked, the answer
as given, and `why` — what the number does. Two ways to change it, both
through the same validation:

```bash
python3 tools/build_config.py --ask     # walk through what is unanswered
python3 tools/build_config.py           # regenerate multisplit.yaml
python3 tools/build_config.py --check   # fail if the config has drifted
```

Or in Home Assistant: Settings → Devices & services → Multi-split climate →
**Configure**, which renders the questionnaire as a form, writes the answers
back, regenerates the config and restarts the controller.

A missing key is an error rather than a silent default — a hidden default is
indistinguishable from a hard-coded value, since you cannot see it, cannot
change it from the config, and it surfaces later as mysterious behaviour.
The one exception is a key added after a release, so an upgrade does not break
a file that loaded before it: it takes the default it has had since, and Home
Assistant logs a warning naming the line to add.
Where an answer is genuinely unknown the questionnaire states an assumption
and the generated line is marked `UNMEASURED`.

### The comfort band

`defaults.intent` sets the target and the band above it: with
`default_target: 20` and `band: 2`, a room heats below 20, cools above 22 and
is left alone in between. Heating aims at the bottom of the band and cooling
at the top. A room may set its own `band`. A band of zero is a single
setpoint.

### Upgrading

Copy the new `multisplit.yaml` across too, or open **Configure** once, which
regenerates it from your answers. An older file still loads: settings added
since it was written take their defaults, chosen to behave as before, and each
one is logged as a warning naming the line to add, e.g.

```
multisplit: defaults.intent.band is not set; using 0, the default since 0.2.0. Add 'band: 0' to make it explicit.
```

### Going live

Set `apply_to_hardware` to true — through Configure, or in the questionnaire
and regenerate. Until then nothing is written to any unit.

## Schedules and away

Each room follows a weekly schedule set by the person whose room it is. Add
the card to a dashboard; the integration serves it, so there is nothing to
install and no resource to add:

```yaml
type: custom:multisplit-schedule-card
# entities: [climate.erin_s_bedroom]   # optional; every room when left out
```

It shows one line per day, Monday to Sunday. A mark on a line sets the room's
target from that time until the next mark, across midnight and round the week
— a single mark on Monday at 07:00 means that temperature all week.

- **On a phone**, the lines are an overview: tap a day to open it as a list,
  where each mark has a time field, − and + for the temperature, and × to
  remove it, with *Add mark*, ‹ › to step through the week, and copy to the
  weekdays, the weekend or every day. Swiping over the card scrolls the page
  and never adds a mark.
- **With a mouse**, click a line to drop a mark there, drag a mark to move it
  (in 15-minute steps), or click a day's name for the same list.
- **With a keyboard**, a focused mark moves with ← →, changes temperature with
  ↑ ↓, opens its day with Enter, and is removed with Delete.

Changes are kept until **Save schedule**; *Discard changes* drops them.

**Who may edit.** A room's owners may edit that room; administrators may edit
every room. A room with no owner is admins-only, and everyone else sees the
schedule read-only. Owners are set per room in the questionnaire's rooms list —
in Home Assistant, Configure → *Outdoor units and rooms* — as a Home Assistant
user name or id, or a list for a shared room:

```yaml
- key: erin
  # …
  owner: Erin
- key: bedroom
  # …
  owner: [Paul, Anna]
```

**Guardrails.** Every mark must sit inside the room's `min_target` and
`max_target` (by default the group's setpoint range), and no setting may last
less than `min_block_minutes` (default 30), measured mark to mark, wrapping the
week. The card flags a problem as you make it and will not save; the
integration checks again, so a service call or automation cannot get round
them either. A thermostat turned by hand is held to the same range.

**What wins.** Most specific first:

1. **turned by hand** — the room's thermostat; holds until the schedule next
   changes;
2. **away** — the house-wide switch, or the room's own `schedule_entity`
   being off;
3. **the room's schedule**;
4. **the default target**, for a room with no schedule yet.

**Away.** `switch.multisplit_away` sets every room to the away target. Flip
it from the card, the dashboard or a presence automation; it survives
restarts. Switching it either way clears manual overrides, so a tweak from the
morning does not hold one room warm all the time the house is empty. A room
turned up while away still heats — someone home early is not left cold.

Schedules and the away state are kept in Home Assistant's storage
(`.storage/multisplit.state`), so they survive restarts and are in its
backups. The `multisplit.set_schedule` service does the same as the card:

```yaml
action: multisplit.set_schedule
data:
  room: erin
  days: [mon, tue, wed, thu, fri]
  marks:
    - { at: "06:30", temperature: 19.5 }
    - { at: "08:00", temperature: 17 }
    - { at: "16:00", temperature: 20 }
    - { at: "21:00", temperature: 17 }
```

## Solar surplus

Heat is cheapest to make when it is the house's own surplus, and the building
stores it: a room heated to the top of its band in the afternoon needs less
from the grid in the evening. In the Netherlands this matters more from
1 January 2027, when net metering (*salderingsregeling*) ends and exported
power is worth far less than imported.

```yaml
energy:
  surplus_sensor: binary_sensor.exporting_to_grid   # on while exporting; null for none
  settle_minutes: 10
defaults:
  policy:
    solar_boost: 1.0     # degrees into the band while on surplus; 0 turns it off
  intent:
    band: 2.0            # banking needs a band to bank into
```

While the sensor is on, heating aims `solar_boost` above the bottom of the
band, and cooling the same below the top, never past the other end. With the
values above, a room holds 20° on the grid and heats to 21° on surplus.

The rules that keep it safe:

- **Only while one mode is permitted.** Narrowing the band with both heating
  and cooling open would invite the two to fight, so then it declines and says
  why.
- **Settled both ways.** The sensor must hold a state for `settle_minutes`
  before the plan follows it. Banking uses the surplus, so export can stop
  *because* banking started; without the settle the two would oscillate.
- **Unknown is no surplus.** An unavailable sensor, or a restart before the
  sensor has settled, means the plan behaves exactly as it would without this.

It never touches the lockouts, the duty cycle or the minimum run and rest
times. Reasons say when it applies:
`heating — 20.5° against a target of 21° (banking solar: 1° into the band)`,
and `multisplit.explain` shows the surplus state and any change it is waiting
on.

## Telemetry

The overshoot cut-off and the rest period are the only values here that cannot
be reasoned about — they depend on a particular unit's minimum capacity
against a particular room's heat loss. They were measured once by living with
them. Telemetry is what makes measuring them again possible.

### Where it is written

Everything goes in the Home Assistant **configuration directory** — the one
holding `configuration.yaml`. On Home Assistant OS and in the container that
is `/config`; on a core install it is wherever you pointed it, typically
`~/.homeassistant`.

| Path | What |
| --- | --- |
| `multisplit-telemetry.jsonl` | one JSON object per recorded cycle |
| `multisplit-telemetry.jsonl.1` | the previous file, kept on rotation |
| `home-assistant.log` | Home Assistant's own log, including this integration's warnings |

The telemetry filename is configurable but is validated to be a plain
filename — no slashes, no leading dot — so it cannot be made to write outside
the configuration directory, and no `allowlist_external_dirs` entry is needed.

To raise or lower what reaches `home-assistant.log`:

```yaml
logger:
  logs:
    custom_components.multisplit: debug
```

If you have no file access to the box, the **diagnostics download** on the
device page carries the last 120 cycles with their reasons and scores, plus
the observed overshoot points — enough to see what is happening, though not
enough to fit a season's curve.

### What is recorded

Not every tick. Five rooms at a 30 second tick is about 1.8 kB a record and
4.9 MB a day, which would mean any sane file size holds days while the curves
need a season — and almost all of those records are identical.

So a line is written when something changes: a group or room mode, what a room
wants, a target, the divergence from the hardware, the solar surplus (settled
or raw), or **a room's temperature
moving more than 0.2 °C** — that last one is what gives a room's coast after
being switched off its resolution, and it is the measurement `fit_curves.py`
needs most. Plus a heartbeat every 15 minutes, so a quiet file means a quiet
system rather than a broken recorder. Each line carries `why_recorded`.

A simulated day comes out at **129 records, 0.13 MB** instead of 2,880 — so
the default 100 MB holds well over a year, twice that counting the rotated
file. Real sensors are noisier than a simulation, so expect more than that,
but not the twenty-fold more that recording every tick would cost.

Recording every tick is still available (`on_change: false`) if you want the
raw stream for a few days.

Each record holds outdoor temperature, the solar surplus (`surplus`, as the
planner used it, and `surplus_reading`, the sensor before settling), and for
each room its target, its comfort band as asked for before any banking
(`intent`), measured temperature, what it wanted, what it was told to do and
why. Then:

```bash
python3 tools/fit_curves.py multisplit-telemetry.jsonl
```

That reports, per outdoor band, the overshoot actually permitted when a unit
was stopped, how much further the room **coasted afterwards**, and how long it
took to recover — and prints a suggested curve in questionnaire.yaml's format.

The coast is the number nobody has, and it is the interesting one. If a room
drifts a further 0.4 °C after being stopped at +1.5 °C, the cut-off is 0.4 °C
too late; in mild weather the correction can push it below zero, meaning stop
*before* target because minimum capacity will carry the room past anyway.

There is also a per-room `overshoot` sensor with a measurement state class, so
Home Assistant's own recorder keeps long-term statistics for it with no
further work — the cheapest route to the same curve is that sensor plotted
against outdoor temperature over a season.

Each cycle is also fired as a `multisplit_cycle` event, and the diagnostics
download from the device page carries the configuration as loaded, the recent
cycles with their reasons and scores, and the observed overshoot points.

### Replaying it

```bash
python3 tools/replay.py multisplit-telemetry.jsonl
python3 tools/replay.py multisplit-telemetry.jsonl --outdoor 21.5
python3 tools/replay.py multisplit-telemetry.jsonl --surplus on
```

Feeds recorded cycles back through the planner with the state threaded as the
coordinator does, so a change to the code or the config can be tested against
real history. Unchanged, it reproduces the logged decisions exactly. With one
fact forced — the outdoor temperature, the solar surplus, or
`--freshness availability` — it shows what the planner would have decided
instead: how often each group changes mode, any heat-against-cool conflict,
and any room dropped mid-cycle on a stale sensor.

It is open loop, and honest only because the log was taken in shadow mode: it
shows what the planner would have *asked for*, not how warm the rooms would
then have become.

### Divergence

Each group gets a `divergence` sensor: how many of its units are not set the
way the plan says. In shadow mode that is the entire point — it counts where
this planner disagrees with whatever is currently running the house. A number
that settles near zero means they agree; one that does not is either a bug
here or a behaviour of the old system nobody wrote down. Once writes are
enabled it should sit at zero and only blip when something is changed at the
wall.

## Entities

| Entity | What it is |
| --- | --- |
| `climate.<room>` | the room's comfort intent: a target, and auto or off. Writes intent, never hardware; turned by hand, it holds until the schedule next changes |
| `sensor.<room>_status` | heating / cooling / waiting / standby / no data, with `reason`, `wants`, `unserved_for_minutes` |
| `sensor.<group>_mode` | heat / cool / off, with `reason`, `scores`, `unserved` |
| `binary_sensor.<group>_<mode>_allowed` | whether the weather permits it, with `reason` |
| `sensor.<room>_overshoot` | measured minus target, with statistics kept — the raw material for refitting the cut-off |
| `sensor.<group>_divergence` | how many units are not set the way the plan says |
| `switch.multisplit_away` | away mode for the whole house |

The room thermostat offers only **auto** and **off**, not heat and cool. A
room cannot choose heating while the outdoor unit is cooling, so a mode picker
that offers it is wrong at the moment it is drawn. The room says what it wants
to be; the system decides the mode; `hvac_action` reports what is actually
happening.

## Services

| Service | What it does |
| --- | --- |
| `multisplit.explain` | everything the system believes right now, and why — including the solar surplus state |
| `multisplit.set_schedule` | replace days of a room's weekly schedule (owner or admin) |
| `multisplit.set_room_target` | override a room's target, optionally until a time |
| `multisplit.clear_override` | back to the schedule or default |
| `multisplit.reload` | re-read `multisplit.yaml` without restarting |

## How it is put together

```
custom_components/multisplit/
  core/            the decision layer — standard library only, no Home Assistant
    model.py       frozen dataclasses
    planner.py     every decision, as a pure function
    policy.py      what is permitted, and why not
    energy.py      the solar surplus, settled against passing clouds
    schedule.py    a room's week, its guardrails, who may edit it
    config.py      validated mapping → model
  measurement.py   which sensor to believe, and whether to
  comfort.py       override → away → schedule → default
  store.py         schedules and away, kept across restarts
  switch.py        the away switch
  frontend/        the schedule card, served by the integration
  reconciler.py    the only writes to hardware
  coordinator.py   the tick: gather, plan, reconcile
  telemetry.py     one record per cycle, so the curves can be refitted
  config_flow.py   setup, and the questionnaire rendered as a form
  generate.py      questionnaire → config, shared by the form and build_config
tools/
  build_config.py  questionnaire → multisplit.yaml
  fit_curves.py    telemetry → a suggested curve
  replay.py        telemetry → the decisions again, with one fact changed
```

The planner decides everything and writes nothing; the reconciler writes
everything and decides nothing. The planner is a pure function — `now` and the
previous state are arguments — so both rooms opposed, a room starved for an
hour, a stale sensor and a unit overshooting in mild weather are all unit
tests that run in microseconds. CI fails the build if anything under `core/`
ever imports Home Assistant.

Because a command is always a complete desired state and the reconciler
re-asserts it every cycle, a unit changed at the wall or one that forgot its
setpoint is corrected within a cycle, and the class of bug where a unit ends
up in the right mode at the wrong temperature cannot be expressed.

## Documents

- [DESIGN-v3.md](DESIGN-v3.md) — the architecture and why it is shaped this way
- [BUILD-v3.md](BUILD-v3.md) — what gets built, in what order
- [HARVEST.md](HARVEST.md) — the measured values, and what is still unanswered
- [CHANGELOG.md](CHANGELOG.md) — what changed in each release, and why
- [ARCHITECTURE.md](ARCHITECTURE.md) — review of the system this replaces

## Dashboard

[dashboard.yaml](dashboard.yaml) is a complete view using built-in cards: a
tile per room for control, a thermostat dial, a markdown card explaining every
decision, and the outdoor units with their divergence counts.

The one option worth knowing about is `state_content` on the tiles:

```yaml
state_content:
  - hvac_action
  - current_temperature
```

By default a climate tile shows its hvac **mode**, which here is always
`auto` — deliberately, because a room cannot choose heating while the outdoor
unit is cooling. What you want on the face of the card is `hvac_action`: what
that room's unit is actually doing, which can honestly be `idle` while its
group heats. That is the duty cycle resting the unit, and the markdown card
says which case it is.

## The old card

`multisplit-climate-card.js` belongs to the system this replaces. Its mode
request buttons call a script that no longer exists, so a request sits on
"requested" for ever and can be neither cancelled nor reversed — it is waiting
for an answer from something that has been deleted. There is nothing to
request now: a room that wants the other mode accumulates weight until it
wins.

It is **not** installed by HACS, which only copies `custom_components/`, and it
could not be: HACS treats a repository as one category, so a Lovelace card
would need a repository of its own. If it is on your system it was copied to
`config/www/` by hand. Remove the card from the dashboard, delete its entry
under Settings → Dashboards → Resources, and delete the file.
