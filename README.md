# Multi-split Climate Card

A Home Assistant Lovelace card for multi-split HVAC systems, where the outdoor
unit dictates heat/cool for every indoor unit and each room only gets a
setpoint.

## Install

1. Copy `multisplit-climate-card.js` into your HA config at
   `config/www/multisplit-climate-card.js`.
2. Settings → Dashboards → Resources → Add resource:
   - URL: `/local/multisplit-climate-card.js?v=2.1.0`
   - Type: JavaScript module
   - Bump `?v=` on every update, or the browser serves the old file.
3. Add the card. It has a visual editor, or use YAML:

```yaml
type: custom:multisplit-climate-card
entity: climate.office                        # required
name: Office                                  # optional title override
group_mode: input_select.group_a_hvac_mode   # the shared heat/cool mode
current_temperature_entity: sensor.office_temp  # overrides the climate attr
target_entity: input_number.office_setpoint   # setpoint helper, if you use one
target_attribute: auto                        # auto | temperature
                                              # | target_temp_low | target_temp_high
min: 16
max: 30
step: 0.5
show_group_mode: true

# Optional: report where the room stands with the group. Display only.
mode_request:
  service: script.multisplit_request_mode    # where "ask the group" goes
room_status:
  demand_entity: input_select.msc_office_demand
  execution_entity: input_select.msc_office_execution
  reason_entity: input_text.msc_office_reason
```

Only `entity` is required; everything else falls back to the climate entity.s
own attributes. Omit `room_status` entirely and the status block does not
render at all.

## Dial geometry

`DIAL` at the top of the card file controls the arc. With the default
`start: 225` the 90° gap sits at the **bottom**: minimum at bottom-left,
maximum at bottom-right. Changing `start` alone rotates the whole dial
(`start: 135` puts the gap on the right).

## Dev harness

`harness.html` runs the card against a stub `hass` object in a plain browser —
no Home Assistant needed. It logs every service call, lets you simulate state
changes, and probes the pointer-angle mapping.

```bash
node serve.js   # then open http://localhost:8765
```

`<ha-card>` renders as a plain div there and `ha-form` does not exist, so the
visual editor can only be tested inside Home Assistant itself.

## Room status

The card never changes HVAC mode. Mode belongs to the group; the room owns
comfort intent only. The dial is how a room asks for something, and
`room_status` is how the backend answers.

It reads two logical states and an explanation, and renders a verdict:

| Execution state     | Shown as    | Meaning                                    |
| ------------------- | ----------- | ------------------------------------------ |
| `ACTIVE`            | `ACCEPTED`  | the unit is acting on the room's intent     |
| `SLEEPING`          | `ACCEPTED`  | intent met; unit stood down deliberately    |
| `WAITING_FOR_GROUP` | `REQUESTED` | asked for, not yet arbitrated               |
| `BLOCKED_BY_GROUP`  | `REJECTED`  | the group mode rules it out                 |

Unknown values are passed through verbatim rather than guessed at, and
synonyms (`heating`, `heat_required`, `blocked`, `waiting`, …) normalise to
the states above. The reason comes from `reason_entity`'s state, or from a
`reason` attribute on any of the three entities; when a room is blocked or
waiting and no reason is published, the card falls back to naming the group
mode so a blocked room is never left unexplained.

All decision logic stays in Home Assistant. The card reports; it does not
arbitrate, and the system behaves identically with no dashboard open.

## Outdoor temperature lockout

`binary_sensor.msc_cooling_available` decides whether cooling may be asked
for at all. It compares `sensor.msc_outdoor_temperature` (a template sensor
naming your outdoor source, in one place) against
`input_number.msc_cool_lockout_below`, with
`input_number.msc_lockout_hysteresis` of deadband so it does not chatter
around the threshold.

A `cool` request made while it is `off` is rejected **immediately** by the
script, carrying the sensor's own `reason` attribute:

> Cooling is locked out below 22.0°C - it is 11.4°C outside

That is the one case where a rejection needs no arbitration and no waiting.
A request already parked when the lockout closes is rejected too.

The threshold lives only in the helper, so when the group mode controller is
eventually taught about it, it reads the same binary sensor rather than
growing a second copy of the number.
