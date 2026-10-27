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

> **Two things to settle from the table above.**
>
> 1. **Four of the five rooms have no dedicated temperature sensor**, so they
>    would fall back to the indoor unit's own reading. That sensor sits in the
>    return air path, which reads warm while heating and cool while cooling —
>    it measures the unit, not the room. Every threshold in §C is relative to
>    *some* temperature, and if four rooms use a biased one the duty cycle
>    inherits that bias. Worth checking whether a room sensor exists under
>    another name before accepting the fallback.
> 2. **Still open, and left exactly as given.**
>    `climate.mitsubishi_ac_ischa_mitsubishi_ac_ischa_climat` ends in
>    `climat`, where the other four end in `climate`. Not corrected, because
>    guessing at an entity id is how a room silently stops being controlled:
>    if the truncated form is the real one, "fixing" it breaks that room and
>    the failure looks like a hardware fault.
>
>    It does not need answering by hand. The integration logs a warning at
>    startup naming every configured entity that does not exist, so the first
>    run says which spelling is real.

**Outdoor temperature source:** `sensor.openweathermap_temperature`

A weather service rather than a local sensor. On balance that is the better
choice here: it is immune to the sun-trap and warm-wall errors that make an
outdoor unit's own sensor read several degrees off, and it is consistent, which
is what the §C curves need since they are indexed against it. Two consequences
worth knowing:

- It is a regional observation, so it lags real conditions at the house and can
  be a degree or two out on a still, sunny day. The curves absorb that, because
  they were tuned against this same entity.
- It can go unavailable when the API call fails. `policy.py` handles an unknown
  outdoor temperature explicitly: lockouts are not applied, the reason says so,
  and the participation curves fall back to their coldest breakpoint — the
  cautious end, meaning the shortest rest.

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
| Cold (around 0 °C) | 1.5 (constant) | 5 min | |
| Mild (around 7 °C) | 1.5 (constant) | 15 min | |
| Shoulder (around 12–14 °C) | 1.5 (constant) | 25 min | |

> **Confirmed 2026-10-27.** The numbers 5 / 15 / 25 were entered in the
> cut-off column and are **minutes of rest period**. The reasoning that led
> to reading them that way:
> five to twenty-five *degrees* past target is not physical; §C1 already gives
> a single cut-off of 1.5 °C with no outdoor dependence; and the original
> description was "powered the unit off after a period of time ... based on
> external factors like outside temperature" — it is the *period off* that is
> both time-based and outdoor-dependent. Rising rest with rising outdoor
> temperature also matches the physics: an overshot room coasts for longer in
> mild weather.
>
> So the mechanism is: **fixed 1.5 °C overshoot, variable rest.** Encoded in
> `questionnaire.yaml` under the duty cycle, as a flat `cut_off_offset` curve
> and a sloping `rest_minutes` one.
>
> Note what this settles beyond the numbers: the cut-off is *not* a function
> of outdoor temperature in this building. The design had assumed it would be,
> and that assumption is withdrawn — see DESIGN-v3.md §4. The rest period is
> the part that varies, and the field data supports that directly.

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
| Cooling lockout — no cooling below | 20 °C (confirm?) |
| Heating lockout — no heating above | 24|
| Allowed setpoint range (min / max) | 16/26 (was 18/26 — see below) |
| Deadband (how far off target before a room wants anything) | |
| Default target when no schedule applies | 20|
| Away / unoccupied target | 16|
| Is there a seasonal lock today, or both modes year-round? | |

> **Resolved 2026-10-27: the range is 16–26.** It was first given as 18–26,
> which could not hold alongside an away target of 16: the setback would have
> been clamped up to 18 and silently never taken effect — indistinguishable,
> from the outside, from a broken schedule. The range was lowered rather than
> the setback raised, so the 16 °C setback does what it says.
>
> The loader now refuses any configuration where a comfort target falls
> outside the setpoint range, so this conflict cannot be reintroduced quietly.
>
> The lockout pair is consistent: no cooling below 20, no heating above 24, so
> between 20 and 24 both are available and arbitration decides. The loader
> rejects the pair if they ever cross and leave an outdoor band where neither
> mode is permitted.

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
| §A rooms and sensors | `questionnaire.yaml` → rooms |
| §A outdoor source | `questionnaire.yaml` → outdoor_sensor; `policy.py` input |
| §B group membership | `questionnaire.yaml` → rooms (each room's group) |
| §C1 / §C2 | `questionnaire.yaml` → defaults.participation |
| §C3 | a per-room `participation:` override in `questionnaire.yaml` |
| §C4 | a scenario in `tests/test_planner.py` |
| §D | starting value for inertia weight and decay |
| §E | `Constraints` in `policy.py` |
| §F | wherever it turns out to belong — possibly a new concern |

Nothing gets deleted from the HA box until the column on the left is full.
