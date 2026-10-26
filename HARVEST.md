# Harvest — what to extract before deleting anything

Fill this in, commit it, *then* start deleting. Everything here lives only on
the HA box, and most of it cannot be re-derived from first principles.

The rule that decides what belongs in this file: **if you had to measure it in
the building, or decide it once and forget, it goes here.** Code can be
rewritten. Knowledge about the hardware and the house cannot.

Three categories, in order of how badly it hurts to lose them:

1. **§C participation thresholds** — measured in the building over a heating
   season. Irreplaceable. Losing these means re-living the overshoot problem.
2. **§B group membership** — which room is on which outdoor unit. Physical
   fact, written down in exactly one place (the blueprint).
3. **§A entity inventory** — tedious to reconstruct, trivial to copy.

---

## Quick wins — let HA do most of §A for you

Developer Tools → **Template**, paste each of these into the left pane and copy
the result. All three are read-only; they evaluate and print, nothing else.

**Every climate entity, with its limits and modes:**

```jinja
{% for e in states.climate %}
{{ e.entity_id }} | {{ e.name }} | {{ e.state }} | min={{ e.attributes.min_temp }} max={{ e.attributes.max_temp }} | modes={{ e.attributes.hvac_modes | join(',') }} | current={{ e.attributes.current_temperature }} | target={{ e.attributes.temperature }}
{% endfor %}
```

**Every temperature sensor** (the candidates for authoritative room readings):

```jinja
{% for e in states.sensor if e.attributes.device_class == 'temperature' %}
{{ e.entity_id }} | {{ e.name }} | {{ e.state }}{{ e.attributes.unit_of_measurement }}
{% endfor %}
```

**The existing helpers**, so nothing is deleted that turns out to matter:

```jinja
{% for d in ['input_select','input_number','input_text','input_datetime','input_boolean'] %}
{% for e in states[d] %}
{{ e.entity_id }} = {{ e.state }}
{% endfor %}
{% endfor %}
```

Paste the output into the sections below, or straight into this file as a code
block — raw is fine, it only has to be readable later.

---

## §A Entity inventory

One row per room. `priority` is the base importance weight — leave it 1.0
unless a room genuinely matters more than the others.

| Room | `climate.*` entity | Temperature sensor(s) | Group | Priority | `schedule.*` (if any) |
| --- | --- | --- | --- | --- | --- |
| Office|climate.mitsubishi_ac_office_mitsubishi_ac_office_climate | |B| 1.0 | |
| Living Room| climate.mitsubishi_ac_livingroom_mitsubishi_ac_livingroom_climate|sensor.average_living_room_temperature | A| 1.0 | |
| Bedroom|climate.mitsubishi_ac_bedroom_mitsubishi_ac_bedroom_climate | | B| 1.0 | |
| Erin's Bedroom| climate.mitsubishi_ac_erin_mitsubishi_ac_erin_climate| | B| 1.0 | |
| Ischa's Bedroom| climate.mitsubishi_ac_ischa_mitsubishi_ac_ischa_climat| |A | 1.0 | |

Where a room has more than one candidate sensor, list them in preference
order — `measurement.py` takes the first one that isn't stale.

**Outdoor temperature source:** `sensor.openweathermap_temperature`

Is that a real outdoor sensor, the outdoor unit's own reading, or a
`weather.*` forecast entity? It matters: the unit's own sensor sits in a
sun-trap or against a warm wall often enough that it reads several degrees off,
and the participation curves in §C are indexed against *this* entity, whatever
it is. If it's wrong, the curves are wrong with it, consistently — which is
survivable, but only if it's the same entity afterwards.

---

## §B Group membership — which room is on which outdoor unit

This exists today only inside the group controller blueprint.

To read it: **Settings → Automations & scenes**, open the group controller
automation, then the three-dot menu → **Edit in YAML**. The `input:` block
lists the entities that were selected when it was set up.

| Outdoor unit | Helper entity today | Rooms on it |
| --- | --- | --- |
| Group A | `input_select.group_a_hvac_mode` |LivingRoom,Ischa's Bedroom|
| Group B | `input_select.group_b_hvac_mode` |Bedroom,Erin's Bedroom,Office |

Also worth recording while you are in there:

- Are there any rooms on these outdoor units that the system does **not**
  currently control? `________________________________`
- Outdoor unit model numbers, if you know them: `________________________`

The model numbers are not needed to build anything, but they are the only
route to the *principled* version of §C — minimum capacity versus room heat
loss, instead of a measured curve. Worth five minutes with a torch.

---

## §C Participation thresholds — the irreplaceable part

The reason this file exists. These numbers encode a hardware constraint
discovered by living with it: an indoor unit on a multi-split cannot modulate
below its minimum capacity, and compressor speed is set by whichever room is
working hardest — so a nearly-satisfied room gets more output than it needs
and overshoots.

To read them: open the participation optimiser automation → three-dot menu →
**Edit in YAML**. The `input:` block holds the values you chose. The blueprint's
own defaults are in `config/blueprints/automation/…` if you want to see what
you changed.

### C1 — What the thresholds actually are

| What | Value today | Where it came from |
| --- | --- | --- |
| Overshoot cut-off (how far past target before the unit is powered off) | 1.5| |
| Run duration before cut-off is allowed (anti short-cycle) | | |
| Rest period before the unit may restart | | |
| Resume threshold (how far below target before restarting) | 0.5| |
| Anything else with a number in it | | |

### C2 — How it varies with outdoor temperature

You said the cut-off was "also based on external factors like outside
temperature". This is the part worth the most care.

| Outdoor temperature | Cut-off offset | Rest period | Notes |
| --- | --- | --- | --- |
| Cold (around 0 °C) |5 | | |
| Mild (around 7 °C) | 15| | |
| Shoulder (around 12–14 °C) |25 | | |

If it was a formula rather than a table, write the formula. If it was a single
threshold with a separate outdoor-temperature *condition* gating it (below X,
don't bother), write that instead — it's a different mechanism and it should be
reproduced as what it is, not reinterpreted.

### C3 — Per-room differences

Does every room use the same numbers, or did some need their own?

```
____________________________________________________________
____________________________________________________________
```

A room that needed different numbers is telling you something about its size
relative to its indoor unit, and that difference should survive into the
per-room breakpoint table rather than being averaged away.

### C4 — What went wrong before you added them

Worth a sentence or two, because it defines the regression test. How far did a
room overshoot, how long did it take to recover, and in what weather was it
worst?

```
____________________________________________________________
____________________________________________________________
```

---

## §D Hysteresis, minimum runtime, vote thresholds

These are **not** carried across directly — the v3 design replaces all three
with one changeover-inertia term. But they are the only evidence of how fast
this hardware tolerates changing its mind, so they set inertia's starting
value instead of a guess.

Open the hysteresis blueprint's automation → Edit in YAML.

| What | Value today |
| --- | --- |
| Minimum time in a mode before changeover is allowed | |
| Temperature hysteresis / deadband | |
| Vote threshold, or however demand is counted | |
| Any timer entity or `for:` duration in the triggers | |

And the question those numbers can't answer:

- Did you ever observe the outdoor unit refusing a mode change, or taking a
  long time to respond after one? Roughly how long?
  `________________________________`

That's the compressor's own changeover inertia, and it's a floor the software
cannot go below no matter what inertia is set to.

---

## §E Policy numbers

| What | Value |
| --- | --- |
| Cooling lockout — no cooling below | 22 °C (confirm?) |
| Heating lockout — no heating above | |
| Allowed setpoint range (min / max) | |
| Deadband (how far off target before a room wants anything) | |
| Default target when no schedule applies | |
| Away / unoccupied target | |
| Is there a seasonal lock today, or both modes year-round? | |

---

## §F The open question

The participation controller nearly got deleted, because from the outside it
looked like a crude controller fighting a modulating one. It survived because
you said why it was there.

So the same question about everything else: **is there anything in the current
system — a threshold, a delay, a condition, an ordering, a workaround that
looks arbitrary — that is there because something went wrong once?**

```
____________________________________________________________
____________________________________________________________
____________________________________________________________
```

Even a vague answer is worth having. "Something to do with the study in the
mornings" is enough to go looking.

---

## Where each number ends up

| From | Becomes |
| --- | --- |
| §A rooms and sensors | config flow, rooms step |
| §A outdoor source | config flow, setup step; `policy.py` input |
| §B group membership | config flow, groups step |
| §C1 / §C2 / §C3 | per-room participation breakpoint table in the options flow |
| §C4 | a scenario in `tests/test_planner.py` |
| §D | starting value for inertia weight and decay |
| §E | `Constraints` in `policy.py` |
| §F | wherever it turns out to belong — possibly a new concern |

Nothing gets deleted from the HA box until the column on the left is full.
