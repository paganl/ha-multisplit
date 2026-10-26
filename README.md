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
  says so in words rather than just doing nothing.
- **Explains itself.** Every decision carries the reason produced by the code
  that made it. `multisplit.explain` answers "why is this room standing by?".

## Installation

### HACS

Add this repository as a custom repository (HACS → ⋮ → Custom repositories),
category **Integration**, then install and restart Home Assistant.

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
Where an answer is genuinely unknown the questionnaire states an assumption
and the generated line is marked `UNMEASURED`.

### Going live

Set `apply_to_hardware` to true — through Configure, or in the questionnaire
and regenerate. Until then nothing is written to any unit.

## Telemetry

The overshoot cut-off and the rest period are the only values here that cannot
be reasoned about — they depend on a particular unit's minimum capacity
against a particular room's heat loss. They were measured once by living with
them. Telemetry is what makes measuring them again possible.

One JSON object per cycle in `multisplit-telemetry.jsonl`, rotated at a
configured size: outdoor temperature, and for each room its target, measured
temperature, what it wanted, what it was told to do and why. Then:

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
| `climate.<room>` | the room's comfort intent: a target, and auto or off. Writes intent, never hardware |
| `sensor.<room>_status` | heating / cooling / waiting / standby / no data, with `reason`, `wants`, `unserved_for_minutes` |
| `sensor.<group>_mode` | heat / cool / off, with `reason`, `scores`, `unserved` |
| `binary_sensor.<group>_<mode>_allowed` | whether the weather permits it, with `reason` |
| `sensor.<room>_overshoot` | measured minus target, with statistics kept — the raw material for refitting the cut-off |
| `sensor.<group>_divergence` | how many units are not set the way the plan says |

The room thermostat offers only **auto** and **off**, not heat and cool. A
room cannot choose heating while the outdoor unit is cooling, so a mode picker
that offers it is wrong at the moment it is drawn. The room says what it wants
to be; the system decides the mode; `hvac_action` reports what is actually
happening.

## Services

| Service | What it does |
| --- | --- |
| `multisplit.explain` | everything the system believes right now, and why |
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
    config.py      validated mapping → model
  measurement.py   which sensor to believe, and whether to
  intent.py        override → schedule → default
  reconciler.py    the only writes to hardware
  coordinator.py   the tick: gather, plan, reconcile
  telemetry.py     one record per cycle, so the curves can be refitted
  config_flow.py   setup, and the questionnaire rendered as a form
tools/
  build_config.py  questionnaire → multisplit.yaml
  fit_curves.py    telemetry → a suggested curve
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
- [ARCHITECTURE.md](ARCHITECTURE.md) — review of the system this replaces

## The card

`multisplit-climate-card.js` is a Lovelace card from the previous iteration. It
is not needed: with the thermostat offering only auto and off, the stock climate
card no longer misrepresents anything. The card's remaining value is showing
`reason` per room on one line, and it has not yet been rewritten against these
entities.
