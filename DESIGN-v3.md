# Multi-split climate control — clean design

Greenfield. Assumes nothing from the current blueprints survives.

The governing idea: **a pure planner that turns facts into a plan with
reasons, and a reconciler that makes the hardware match the plan.** Every
other concern is an adapter on one side or the other.

---

## 1. The seven concerns

Each has one job, one input contract, one output contract. Nothing reaches
across a boundary.

| # | Concern | Input | Output | Owns |
| --- | --- | --- | --- | --- |
| 1 | **Measurement** | raw sensors | `RoomMeasurement` | which sensor is authoritative, averaging, staleness |
| 2 | **Intent** | schedule, presence, user | `RoomIntent` | what each room should be, and why |
| 3 | **Policy** | config, outdoor temp, tariff | `Constraints` | what the system is *permitted* to do |
| 4 | **Planner** | the three above + clock | `Plan` (+ reasons) | every decision |
| 5 | **Reconciler** | `Plan` + observed hardware | service calls | the only writes to hardware |
| 6 | **Presentation** | `Plan`, reasons, measurements | UI; writes intent only | showing state, capturing intent |
| 7 | **Observability** | plan history | traces, logbook | why the system did what it did |

The critical boundary is 4↔5. **The planner never writes anything and the
reconciler never decides anything.** Violating that is how the current system
ended up with three writers on one setpoint.

---

## 2. Data flow

Strictly one direction. No component reads back from a layer below it.

```
  sensors ──────────► [1 Measurement] ──┐
  schedule/presence ► [2 Intent] ───────┼──► [4 Planner] ──► Plan
  outdoor/config ───► [3 Policy] ───────┘      (pure)         │
                                                              ├──► [5 Reconciler] ──► Mitsubishi
                                                              ├──► [6 UI]
                                                              └──► [7 Observability]
```

The planner is a **pure function**. No I/O, no stored state, no timers.
Time-dependent behaviour (changeover inertia, starvation aging) works because
`now` and `last_mode_change` are *inputs*, not internal state. That is what
makes the whole system testable without Home Assistant.

---

## 3. The model

```python
@dataclass(frozen=True)
class RoomMeasurement:
    room: str
    temperature: float | None
    stale: bool
    source: str              # which sensor won, for display

@dataclass(frozen=True)
class RoomIntent:
    room: str
    target: float
    source: str              # schedule | override | away | default
    expires_at: datetime | None
    priority: float          # occupancy/importance weight, default 1.0

@dataclass(frozen=True)
class Constraints:
    allowed_modes: frozenset[str]     # from outdoor lockouts / season
    setpoint_range: tuple[float, float]
    deadband: float
    inertia: float                    # changeover resistance
    reasons: dict[str, str]           # why a mode is disallowed

@dataclass(frozen=True)
class ParticipationState:
    room: str
    running: bool
    since: datetime          # last on/off transition

@dataclass(frozen=True)
class ParticipationPolicy:
    """
    An indoor unit on a multi-split cannot modulate below its minimum
    capacity, and the compressor speed is set by whichever room is working
    hardest. A nearly-satisfied room therefore receives more output than it
    needs and overshoots. The overshoot is worst in mild weather, when
    minimum capacity is a large fraction of actual demand -- so the cut-off
    is a function of outdoor temperature, not a constant.
    """
    cut_off_offset: Callable[[float], float]   # outdoor -> degrees past target
    min_off: Callable[[float], timedelta]      # outdoor -> rest period
    min_on: timedelta                          # anti short-cycle

@dataclass(frozen=True)
class RoomCommand:
    room: str
    mode: str | None         # None = stand by
    target: float
    because: str

@dataclass(frozen=True)
class GroupDecision:
    group: str
    mode: str | None
    because: str
    scores: dict[str, float]
    unserved: list[str]

@dataclass(frozen=True)
class Plan:
    groups: list[GroupDecision]
    rooms: list[RoomCommand]
    at: datetime
```

`because` is not logging. It is a **first-class output of the decision**,
produced by the code that made it. Nothing downstream ever has to infer why
something happened — which is precisely the failure in the current design.

`Plan` also carries `runtime`: the memory for the next cycle. Computing the
state transitions in the planner rather than the coordinator means the state
machine — when a dwell timer restarts, when a starvation clock starts and
stops — is covered by the same pure tests as the decisions, instead of living
untested in the Home Assistant layer.

The curves are `Curve` objects holding sorted breakpoints with linear
interpolation, not Python callables. Breakpoints are configurable from the UI,
serialisable, and clamped rather than extrapolated at the ends — extrapolating
a measured curve into weather you have never observed is how you find a bug in
February.

---

## 4. The planner

```
for each group:
    allowed = constraints.allowed_modes          # policy already applied

    for each room in group:
        error = intent.target - measurement.temperature
        wants = heat  if error >  deadband
                cool  if error < -deadband
                none  otherwise

    for each mode in allowed:
        score[mode] = Σ over rooms wanting that mode of
                        |error| × priority × aging(room)

    score[current_mode] += inertia × decay(now - last_mode_change)

    mode = argmax(score)   or None if every score is 0

    for each room in group:
        command = participate(room, mode, now) ? (mode, target) : stand by
```

### Participation is a duty-cycle controller, not a predicate

`wants == mode` decides whether a room is *eligible*. It does not decide
whether the unit should be running right now, because the hardware cannot
deliver a small amount of heat. Participation is therefore a controller with
asymmetric thresholds and dwell times:

```
heating, room currently running:
    stop when  actual > target + cut_off_offset(outdoor)
          and  running_for >= min_on

heating, room currently standing by:
    start when actual < target - deadband
          and wants == mode
          and off_for >= min_off(outdoor)

cooling: mirrored.
```

`cut_off_offset` **may be negative** — in mild weather the correct action is
to stop before the room reaches target, because minimum capacity plus the
residual heat in the coil will carry it past anyway. That is anticipatory
cut-off, and it is the part a fixed threshold cannot express.

`min_off` widens in mild weather too: a room that has been overshot coasts
for longer before it genuinely needs heat again, and resting the unit avoids
short-cycling the shared compressor.

This remains a **pure function**: `ParticipationState` and `now` are inputs,
so there is still no hidden state and it is still unit-testable.

### Two corrections the tests forced into the model

Both were found by writing the scenario table in phase 0, not by reasoning
about the design. Both are the kind of defect that would have presented in a
real house as "it thrashes sometimes".

**Structural overshoot must not read as demand for the opposite mode.** If
comfort demand used one symmetric deadband, a room pushed past its target by
minimum capacity -- which is guaranteed, by design -- would immediately vote
for cooling. The hardware's own behaviour would generate changeover pressure
every cycle, and the system would thrash because of the very effect the
participation controller exists to absorb. So wanting the opposite of what is
currently being delivered has to clear a wider band:
`Constraints.overshoot_tolerance`, which must exceed the cut-off offset plus
the coast that follows it.

**A running unit is committed, and the group cannot turn off underneath it.**
Participation keeps a room running until its cut-off, and through its minimum
run time. But comfort demand reaches zero the moment the room is inside the
deadband -- so a group scoring purely on comfort would drop to off while a
unit was still running, undoing the duty cycle on the next tick. Demand is
therefore `wants or holding`, where `holding` is "running, and either short of
the cut-off or inside the minimum run". Group mode scores on either;
starvation counts only unmet `wants`, because only unmet comfort is being
starved.

Two mechanisms replace four pieces of existing machinery.

**Changeover inertia replaces hysteresis, minimum runtime and vote
thresholds.** Staying put gets a score bonus that decays with time since the
last change. A marginal demand can't flip the mode; a strong, sustained one
eventually can. Unlike a hard minimum runtime it degrades gracefully — the
system is never *forbidden* from responding to a genuine need, just biased
against churn. One tunable instead of three.

**Aging replaces the request/approve protocol.** A room whose demand goes
unserved accumulates weight each cycle. A room that genuinely needs the other
mode wins eventually, automatically, without anyone pressing a button — and
starvation becomes impossible by construction rather than by escalation. The
UI shows `unserved_for`, so the wait is visible while it happens.

Both are pure: aging and inertia are computed from timestamps passed in.

---

## 5. The reconciler

The only component that writes to hardware.

```
for each room command in plan:
    observed = current (hvac_mode, temperature) of the climate entity
    desired  = (command.mode, command.target)  or  (off, _)
    if observed != desired and not rate_limited(room):
        climate.set_temperature(entity, hvac_mode=..., temperature=...)
```

Properties that matter:

- **Idempotent.** Runs every cycle, writes only differences.
- **Mode and setpoint always travel together.** The 23°C defect is not fixed
  here — it is *unrepresentable*, because a command is always a complete
  desired state, never a mode change on its own.
- **Self-healing.** A unit changed at the wall, or one that forgot its
  setpoint, is corrected within one cycle. No event can be "missed", because
  nothing is event-driven.
- **Respects hardware.** Rate-limited per unit so it never fights the
  compressor's own anti-cycle lockout.

---

## 6. Module layout

```
custom_components/multisplit/
    model.py          dataclasses above — no HA imports
    planner.py        pure decision logic — no HA imports  ← the testable heart
    policy.py         Constraints from config + outdoor temperature
    intent.py         schedule / override / presence resolution
    measurement.py    sensor selection, averaging, staleness
    reconciler.py     the only writer
    coordinator.py    gathers inputs, calls planner, hands plan to reconciler
    climate.py        one entity per room
    sensor.py         status + reason per room; mode + reason per group
    config_flow.py    groups, rooms, entity mapping — UI, not YAML
    services.yaml     set_room_target, set_season, clear_override
```

`planner.py` and `model.py` importing nothing from Home Assistant is the
single most valuable constraint in this document. The entire decision layer
can be unit-tested in milliseconds, including every scenario that is painful
to reproduce in a live house: both rooms opposed, sensor stale, outdoor at
the lockout boundary, a room starved for an hour.

---

## 7. The entity contract

What the rest of the world sees. The UI depends on this, nothing else.

| Entity | Writable | Attributes |
| --- | --- | --- |
| `climate.<room>` | target temperature | `hvac_modes` restricted to what is actually possible right now; `hvac_action` reflects reality |
| `sensor.<room>_status` | — | `state`, `reason`, `wants`, `participating`, `unserved_for`, `intent_source` |
| `sensor.<group>_mode` | — | `reason`, `scores`, `since`, `mode_locked_until` |
| `binary_sensor.<group>_<mode>_allowed` | — | `reason` |

Because `hvac_modes` is restricted to what the group permits, **the stock Home
Assistant climate card stops lying** — it simply won't offer a mode the room
cannot have. The custom card becomes an enhancement, not a requirement.

---

## 8. What this deletes

- The demand and execution state machines — both derived, not stored.
- The hysteresis blueprint, vote thresholds and minimum runtime — replaced by
  one inertia term.
- The mode request script, its helpers and the resolver automation.
- The card's request row.
- The accountability chain repair work — unnecessary, because every decision
  emits its own reason at the point it is made.

## 8a. What this explicitly keeps

**The participation controller.** Overshoot is not a tuning failure, it is a
hardware constraint: a multi-split indoor unit cannot modulate below its
minimum capacity, and the compressor speed is set by whichever room is
working hardest. Its thresholds and dwell times stay, and the dependence on
outdoor temperature is promoted from a tuning parameter to part of the model
-- see the participation controller in section 4.

This is the one piece of the existing system that encodes something the
design could not have derived from first principles. It was measured in a
real building, and it survives the rewrite intact.

The request/approve protocol was a symptom. It existed because mode was
contested and undecidable from the room's point of view. With mode driven by
policy, inertia and aging, a room that wants the opposite mode is simply a
room standing by, and the UI says so in one line.

---

## 9. What stays genuinely hard

**Shoulder season.** When outdoor sits between the lockouts and rooms
genuinely oppose, no rule serves everyone. Priority weighting is the only
principled tiebreak — serve the occupied room. This is the one place weighted
arbitration earns its complexity, and it is the piece the current design
lacks entirely.

**Sensor trust.** A stale or unrepresentative sensor produces confident wrong
decisions. Hence `stale` on the measurement and an explicit source — a room
with no trustworthy temperature should stand by, loudly, not guess.

**Calibrating the overshoot cut-off.** `cut_off_offset` and `min_off` are
curves against outdoor temperature, not numbers. They encode how far a given
unit overshoots at a given load ratio, which is specific to the room, the
unit size and the building. They have to be measured: log target, actual and
outdoor across cycles and fit the curve. The principled version compares the
unit's minimum capacity against the room's heat loss at that outdoor
temperature; outdoor temperature alone is a good proxy when capacity data is
not available.

**Tuning inertia.** Too low: churn. Too high: slow to respond. This wants
measurement, which is cheap once plans are logged — replay history against
candidate values offline, since the planner is pure.

---

## 10. What it is built to absorb later

Each slots in as an extra input to policy or an extra term in the score —
never as a change to actuation:

- PV surplus or tariff-aware setpoint shifting (policy input)
- Weather-compensated or predictive pre-heat (intent input)
- Occupancy-driven priority (already a field)
- A second hardware type (reconciler only)
- Demand-response / load shedding (constraint)
