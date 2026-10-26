# Multi-split climate architecture

Status of every box below:

| Mark | Meaning |
| --- | --- |
| **E** | Exists today — your blueprints and helpers |
| **D** | Drafted in this repo, **never run against Home Assistant** |
| **P** | Proposed only — discussed, not built |

The governing rule (§19): **room autonomy ends at comfort intent. Shared HVAC
mode belongs to the multi-split group.**

---

## 1. Layers of authority

Who is allowed to decide what. Every defect we have hit traces back to a
decision being taken at the wrong layer.

```mermaid
flowchart TB
    subgraph ROOM["ROOM — owns comfort intent"]
        R1["Desired temperature<br/>(dial / schedule)"]
        R2["Actual temperature<br/>(unit sensor or averaged)"]
        R3["Demand<br/>HEATING_REQUIRED / SATISFIED / COOLING_REQUESTED"]
        R4["Participation<br/>in or out of the group"]
    end

    subgraph GROUP["GROUP — owns the shared mode"]
        G1["Authoritative HEAT / COOL"]
        G2["Hysteresis"]
        G3["Minimum mode runtime"]
        G4["Outdoor temperature constraints"]
        G5["Arbitration between competing rooms"]
    end

    subgraph PHYS["PHYSICAL — executes only"]
        P1["Mitsubishi outdoor unit"]
        P2["Indoor unit<br/>group mode + room target"]
    end

    ROOM -->|"asks"| GROUP
    GROUP -->|"grants or refuses"| ROOM
    GROUP --> PHYS
    ROOM --> PHYS

    classDef room fill:#fff4e6,stroke:#ff8100,color:#000
    classDef group fill:#e6f3ff,stroke:#2b9af9,color:#000
    classDef phys fill:#eeeeee,stroke:#777,color:#000
    class R1,R2,R3,R4 room
    class G1,G2,G3,G4,G5 group
    class P1,P2 phys
```

A room may **ask** for a mode. It may never **set** one. That single
constraint is why the card has no mode selector and why the request flow
exists at all.

---

## 2. Component capabilities

What each part is *allowed* to do. The "cannot" column is the load-bearing
one — it is what keeps one room from disrupting every other room.

```mermaid
flowchart LR
    subgraph PRES["Presentation — no authority"]
        CARD["<b>multisplit-climate-card</b> D<br/>━━━━━━━━━<br/>CAN read demand, execution,<br/>reason, group mode, temps<br/>CAN write room target<br/>CAN call the request script<br/>━━━━━━━━━<br/>CANNOT set HVAC mode<br/>CANNOT decide participation<br/>CANNOT arbitrate"]
    end

    subgraph INTENT["Intent capture — records, never decides"]
        SCRIPT["<b>request script</b> D<br/>━━━━━━━━━<br/>CAN record a request<br/>CAN reject on lockout<br/>CAN rejoin a unit when the<br/>group already matches<br/>━━━━━━━━━<br/>CANNOT change group mode<br/>CANNOT overrule arbitration"]
        RESOLVE["<b>resolver automation</b> D<br/>━━━━━━━━━<br/>CAN observe group mode<br/>CAN report granted/declined<br/>━━━━━━━━━<br/>CANNOT change group mode<br/>CANNOT invent a verdict<br/>from elapsed time"]
    end

    subgraph DECIDE["Decision — the only layer that may choose"]
        GROUP["<b>group mode blueprint</b> E<br/>━━━━━━━━━<br/>CAN choose HEAT or COOL<br/>CAN write group_a/b_hvac_mode<br/>CAN apply voting thresholds<br/>━━━━━━━━━<br/>CANNOT be overridden by<br/>a single room"]
        HYST["<b>hysteresis blueprint</b> E<br/>━━━━━━━━━<br/>CAN damp mode changes<br/>CAN enforce minimum runtime<br/>━━━━━━━━━<br/>CANNOT choose a mode"]
        LOCKOUT["<b>cooling lockout</b> D<br/>━━━━━━━━━<br/>CAN forbid COOL below<br/>the outdoor threshold<br/>CAN explain why<br/>━━━━━━━━━<br/>CANNOT request or<br/>select a mode"]
    end

    subgraph EXEC["Execution — acts, never chooses"]
        PART["<b>participation optimiser</b> E<br/>━━━━━━━━━<br/>CAN stop a unit overshooting<br/>CAN resume a unit<br/>CAN set mode + temperature<br/>━━━━━━━━━<br/>CANNOT change group mode"]
        UNIT["<b>Mitsubishi</b><br/>━━━━━━━━━<br/>CAN execute group mode<br/>+ room target<br/>CAN protect its compressor<br/>━━━━━━━━━<br/>CANNOT heat and cool<br/>at once"]
    end

    CARD --> SCRIPT
    SCRIPT --> RESOLVE
    LOCKOUT --> SCRIPT
    LOCKOUT -.->|"P"| GROUP
    HYST --> GROUP
    RESOLVE -.->|"observes"| GROUP
    SCRIPT -.->|"P: as a vote"| GROUP
    GROUP --> PART
    PART --> UNIT
    CARD -->|"room target"| PART

    classDef pres fill:#fff4e6,stroke:#ff8100,color:#000
    classDef intent fill:#fff8e1,stroke:#ffa600,color:#000
    classDef decide fill:#e6f3ff,stroke:#2b9af9,color:#000
    classDef exec fill:#eeeeee,stroke:#777,color:#000
    class CARD pres
    class SCRIPT,RESOLVE intent
    class GROUP,HYST,LOCKOUT decide
    class PART,UNIT exec
```

### Authority, ownership, accountability

Three different things, and each of our three real defects is a failure of a
different one:

- **Authority** — who may *decide*. Confusion here causes **thrashing**: two
  parties both think they may choose the mode.
- **Ownership** — who may *write* the state, and who you change to alter
  behaviour. Confusion here caused the **23°C Office**: three writers, no owner.
- **Accountability** — who must *explain* the outcome. Absence here is why a
  request can sit on **REQUESTED forever** with nothing able to say why.

| Concern | Authority — decides | Ownership — single writer | Accountability — explains |
| --- | --- | --- | --- |
| Group HEAT / COOL | group mode blueprint **E** | group blueprint → `group_a/b_hvac_mode` **E** | ⚠️ **nobody** — it decides silently (**P**: publish `last_evaluated` + cause) |
| Mode change damping | hysteresis blueprint **E** | hysteresis blueprint **E** | ⚠️ nobody — invisible to the user |
| Cooling permitted | outdoor lockout rule **D** | `input_number.msc_cool_lockout_below` **D** | ✅ `binary_sensor.msc_cooling_available` `reason` **D** |
| Room comfort target | the person, via dial or schedule | ⚠️ **split three ways** — card, schedule, participation blueprint | ✅ card displays it (but cannot reconcile the three) |
| Room participation | participation blueprint **E** | participation blueprint **E** | ⚠️ gap — it does not write `msc_*_execution`, so the card's ACTIVE/SLEEPING reflects only request outcomes |
| Room demand | demand calculation **P** | request script **D** | ✅ `msc_*_reason` **D** |
| Request outcome | group blueprint, implicitly | resolver automation **D** | 🟡 resolver *infers* from observed behaviour; it cannot see arbitration's reasoning |
| Physical execution | Mitsubishi firmware | Mitsubishi | ⚠️ opaque — compressor protection and changeover are unobservable (**P**: measure it) |

```mermaid
flowchart TB
    PERSON["The person<br/><i>owns comfort intent</i>"]
    CARD["Card<br/><i>no authority — presents and asks</i>"]
    GROUPB["Group mode blueprint<br/><b>sole authority over mode</b>"]
    PARTB["Participation blueprint<br/><i>authority over in/out only</i>"]
    MIT["Mitsubishi<br/><i>no authority — executes</i>"]

    PERSON -->|"delegates: 'keep me comfortable'"| CARD
    CARD -->|"delegates: asks, never commands"| GROUPB
    GROUPB -->|"delegates: mode is settled"| PARTB
    PARTB -->|"commands"| MIT

    MIT -.->|"accountable for: executing<br/>⚠️ explains nothing"| PARTB
    PARTB -.->|"accountable for: in or out<br/>⚠️ does not report it"| GROUPB
    GROUPB -.->|"accountable for: the mode<br/>⚠️ explains nothing today"| CARD
    CARD -.->|"accountable for: showing<br/>✅ requested / accepted / rejected"| PERSON

    classDef ok fill:#e8f5e9,stroke:#43a047,color:#000
    classDef gap fill:#ffebee,stroke:#db4437,color:#000
    classDef neutral fill:#eeeeee,stroke:#777,color:#000
    class PERSON,CARD ok
    class GROUPB,PARTB,MIT gap
```

Solid arrows are **authority delegating downward**. Dotted arrows are
**accountability reporting back upward** — and three of the four are broken.
The card is accountable to the person and does its job; everything below it
decides without explaining, which is why the card can only ever report what it
can *infer*.

Closing the chain is one small, additive change per layer: each writes a
reason when it acts. That is the highest-value backend work available, and it
is strictly cheaper than the integration rewrite.

### Who may write what

One writer per piece of state. Two writers is how the 23°C defect happened.

| State | Written by | Read by |
| --- | --- | --- |
| `input_select.group_a/b_hvac_mode` | group blueprint **only** | card, script, resolver, participation |
| `binary_sensor.msc_cooling_available` | derived from outdoor temp | script, resolver, group blueprint (**P**) |
| `msc_*_demand` | request script | card |
| `msc_*_execution` | request script, resolver | card |
| `msc_*_reason` | request script, resolver | card |
| `climate.*` hvac_mode | participation blueprint; request script on rejoin only | — |
| `climate.*` target temperature | card, schedule, participation blueprint | card |

The last row is the one to watch: **three writers**, which is exactly the
condition that produced the 23°C Office. Collapsing it to a single room-target
helper per room (**P**, §18) removes the ambiguity.

---

## 3. Control flow

```mermaid
flowchart TD
    S["Schedule / presence<br/><b>E</b>"] --> T["Room desired temperature<br/><b>E</b> via climate entity<br/><b>P</b> via input_number per room"]
    T --> D["Room demand calculation<br/><b>P</b>"]
    D --> ARB{"Group mode arbitration<br/><b>E</b> group blueprint"}

    OUT["Outdoor temperature<br/><b>E</b>"] --> LOCK["Cooling lockout<br/>binary_sensor.msc_cooling_available<br/><b>D</b>"]
    LOCK --> ARB
    HYST["Hysteresis<br/><b>E</b> separate blueprint"] --> ARB
    RUN["Minimum mode runtime<br/><b>E</b>"] --> ARB

    REQ["Room mode request<br/><b>D</b> script + card"] -.->|"advisory only today<br/><b>P</b> as a vote"| ARB

    ARB --> MODE["input_select.group_a/b_hvac_mode<br/><b>E</b>"]
    MODE --> PART{"Participation optimiser<br/><b>E</b> blueprint"}
    T --> PART
    PART --> UNIT["Indoor unit<br/>set_temperature(hvac_mode + temperature)"]
    UNIT --> MIT["Mitsubishi outdoor unit"]

    classDef exists fill:#e8f5e9,stroke:#43a047,color:#000
    classDef draft fill:#fff8e1,stroke:#ffa600,color:#000
    classDef prop fill:#f3e5f5,stroke:#8e24aa,color:#000
    class S,OUT,HYST,RUN,MODE,PART,ARB exists
    class LOCK,REQ draft
    class D prop
```

**The dotted line is the honest gap.** A request is recorded and watched, but
nothing feeds it into arbitration. Until the group blueprint accepts requests
as an input, asking for the opposite mode only succeeds if arbitration
independently decides to switch.

---

## 4. What a mode request actually does

```mermaid
sequenceDiagram
    participant U as User
    participant C as Card
    participant S as script.multisplit_request_mode
    participant H as msc_* helpers
    participant B as Group blueprint
    participant R as Resolver automation

    U->>C: tap COOL
    C->>S: mode, climate_entity, group_entity
    S->>H: demand = COOLING_REQUESTED

    alt cooling locked out by outdoor temp
        S->>H: execution = BLOCKED_BY_GROUP + reason
        H-->>C: REJECTED, immediately
    else group already in COOL
        S->>S: set_temperature(hvac_mode + temperature)
        S->>H: execution = ACTIVE
        H-->>C: ACCEPTED
    else group in the other mode
        S->>H: execution = WAITING_FOR_GROUP
        H-->>C: REQUESTED
        Note over B: decides on its own inputs<br/>— the request is not one of them
        B->>R: group mode changed
        alt changed to COOL
            R->>H: execution = ACTIVE
            H-->>C: ACCEPTED
        else changed to something else
            R->>H: execution = BLOCKED_BY_GROUP
            H-->>C: REJECTED, with cause
        end
        Note over R: if it never changes, the request<br/>stays REQUESTED — no verdict invented
    end
```

Rejection is only ever reported on **evidence**: a lockout that forbids it, or
the group demonstrably moving elsewhere. Elapsed time never produces a
verdict, because hysteresis and minimum runtime live in separate blueprints
and a clock cannot distinguish "not considered yet" from "turned down".

---

## 5. Component inventory

| Component | Kind | Lives | Status |
| --- | --- | --- | --- |
| Group HVAC mode controller | blueprint | HA | **E** |
| Hysteresis | blueprint (separate) | HA | **E** |
| Participation optimiser | blueprint | HA | **E** |
| `input_select.group_a/b_hvac_mode` | helper | HA | **E** |
| Averaged room temperature (Living Room) | sensor | HA | **E** |
| `multisplit-climate-card.js` 2.1.0 | Lovelace card | `/config/www/` | **D** |
| `multisplit_requests.yaml` | package | `/config/packages/` | **D** |
| `binary_sensor.msc_cooling_available` | template | package | **D** |
| Request as an arbitration vote | blueprint input | — | **P** |
| Group publishing `last_evaluated` | blueprint output | — | **P** |
| Outdoor-temp-primary changeover | blueprint logic | — | **P** |
| Changeover duration measurement | automation | — | **P** |
| Heat lockout above a threshold | template | — | **P** |
| Custom integration | Python | `custom_components/` | **P** |

---

## 6. Known defects and gaps

| # | Issue | Where it belongs |
| --- | --- | --- |
| 1 | Unit resumes at its remembered setpoint (the 23°C Office) — mode restored without temperature | participation blueprint (**P**); the request script already pairs them (**D**) |
| 2 | Requests do not influence arbitration | group blueprint (**P**) |
| 3 | A request arbitration silently declines stays `REQUESTED` forever | needs the group to publish that it evaluated (**P**) |
| 4 | Mode thrashing when opposing rooms alternate participation | mitigated by outdoor lockout (**D**) and long minimum runtime (**E**) |
| 5 | Helper sprawl: six helpers per room | the reason to consider an integration (**P**) |
| 6 | Participation blueprint does not write `msc_*_execution`, so ACTIVE/SLEEPING reflects request outcomes, not real participation | participation blueprint (**P**) |
| 7 | No layer below the card explains itself (accountability chain broken) | one reason-write per blueprint (**P**) |

---

## 7. Why the card survives a backend rewrite

The card talks to a **contract**, not an implementation:

- reads three entities — demand, execution, reason
- reads one group mode helper
- calls one service with `mode`, `climate_entity`, `group_entity`
- writes the target via `climate.set_temperature` or an `input_number`

Whether those entities are `input_select` helpers or a custom integration's
sensors, the card is byte-identical. Only the entity ids in the dashboard
config change. That is why validating the model with YAML first costs
nothing if the backend is later rewritten in Python.
