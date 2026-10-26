/**
 * Multi-split Climate Card
 *
 * Room control for shared-mode multi-split HVAC systems, where the outdoor
 * unit dictates heat/cool for every indoor unit and each room only gets a
 * setpoint.
 *
 * Config:
 *   type:                        custom:multisplit-climate-card
 *   entity:                      climate.* (required)
 *   name:                        override title
 *   group_mode:                  entity whose state is the shared mode
 *   current_temperature_entity:  sensor to use instead of the climate attr
 *   target_entity:               input_number/number holding the setpoint
 *   target_attribute:            auto | temperature | target_temp_low
 *                                | target_temp_high
 *   min / max / step:            override the climate entity's limits
 *   show_group_mode:             bool, default true
 */

const CARD_VERSION = "2.1.0";

/**
 * Dial geometry. Angles are degrees clockwise from 12 o'clock.
 *
 * With start: 225 / sweep: 270 the track runs from the 7-8 o'clock position,
 * clockwise through the left, top and right, to the 4-5 o'clock position — so
 * the 90 degree gap sits at the BOTTOM of the dial, with minimum at the
 * bottom-left and maximum at the bottom-right.
 *
 * Everything else derives from these constants, so changing start alone
 * rotates the whole dial (start: 135 puts the gap on the right).
 */
const DIAL = {
  start: 225,
  sweep: 270,
  cx: 120,
  cy: 120,
  radius: 91,
  hitWidth: 44,
};

const DIAL_END = DIAL.start + DIAL.sweep;
const GAP_MID = DIAL_END + (360 - DIAL.sweep) / 2;

const MODE_COLORS = {
  heat: "var(--state-climate-heat-color, #ff8100)",
  heating: "var(--state-climate-heat-color, #ff8100)",
  cool: "var(--state-climate-cool-color, #2b9af9)",
  cooling: "var(--state-climate-cool-color, #2b9af9)",
  dry: "var(--state-climate-dry-color, #efbd07)",
  drying: "var(--state-climate-dry-color, #efbd07)",
  fan_only: "var(--state-climate-fan_only-color, #8a8a8a)",
  fan: "var(--state-climate-fan_only-color, #8a8a8a)",
  auto: "var(--state-climate-auto-color, var(--primary-color))",
  heat_cool: "var(--state-climate-heat_cool-color, var(--primary-color))",
};

const UNAVAILABLE_STATES = new Set([
  "unavailable",
  "unknown",
  "none",
  "",
]);

const COMMIT_DEBOUNCE_MS = 350;
const PENDING_HOLD_MS = 1200;

const MODE_LABELS = {
  off: "OFF",
  heat: "HEAT",
  cool: "COOL",
  heat_cool: "AUTO",
  auto: "AUTO",
  dry: "DRY",
  fan_only: "FAN",
};

/**
 * Room comfort intent, as the backend publishes it. The room asks; the group
 * arbitrates. The card only reports what came back.
 */
const DEMAND_STATES = {
  heating_required: { label: "Heating requested", tone: "heat" },
  heat_required: { label: "Heating requested", tone: "heat" },
  heating: { label: "Heating requested", tone: "heat" },
  cooling_requested: { label: "Cooling requested", tone: "cool" },
  cooling_required: { label: "Cooling requested", tone: "cool" },
  cooling: { label: "Cooling requested", tone: "cool" },
  satisfied: { label: "Satisfied", tone: "ok" },
  none: { label: "No demand", tone: "idle" },
};

/**
 * What is actually happening to the indoor unit, which is not the same
 * question — a room can want heat and still be asleep or outvoted.
 */
const EXECUTION_STATES = {
  active: { label: "Active", tone: "ok", accepted: true },
  running: { label: "Active", tone: "ok", accepted: true },
  sleeping: { label: "Sleeping", tone: "idle", accepted: true },
  off: { label: "Sleeping", tone: "idle", accepted: true },
  waiting_for_group: { label: "Waiting for group mode", tone: "wait" },
  waiting: { label: "Waiting for group mode", tone: "wait" },
  blocked_by_group: { label: "Blocked by group mode", tone: "blocked" },
  blocked: { label: "Blocked by group mode", tone: "blocked" },
};

const VERDICT_LABELS = {
  accepted: "Accepted",
  pending: "Requested",
  rejected: "Rejected",
};

/**
 * A room may ask the group for a mode; it may not set one. The request goes
 * to a script in Home Assistant, which records it and lets the existing
 * arbitration decide. The card only reports what comes back.
 */
const REQUEST_DEFAULTS = {
  service: null,
  service_data: null,
  modes: ["heat", "cool"],
  timeout: 45,
};

const ROOM_STATUS_DEFAULTS = {
  demand_entity: null,
  demand_attribute: null,
  execution_entity: null,
  execution_attribute: null,
  reason_entity: null,
  reason_attribute: "reason",
};

class MultiSplitClimateCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });

    this._hass = null;
    this._config = null;
    this._els = null;
    this._built = false;

    this._dragging = false;
    this._pendingTarget = null;

    this._commitTimer = null;
    this._releaseTimer = null;
    this._toastTimer = null;
    this._commitSeq = 0;

    // A mode request made from this card, until the backend answers it.
    this._request = null;
    this._requestTimer = null;
    this._modeKey = null;
  }

  // ---------------------------------------------------------------- lifecycle

  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error("You must define a climate entity");
    }

    if (!String(config.entity).startsWith("climate.")) {
      throw new Error("`entity` must be a climate entity");
    }

    this._config = {
      name: null,
      group_mode: null,
      current_temperature_entity: null,
      target_entity: null,
      target_attribute: "auto",
      min: null,
      max: null,
      step: null,
      show_group_mode: true,
      ...config,
    };

    this._config.room_status = config.room_status
      ? { ...ROOM_STATUS_DEFAULTS, ...config.room_status }
      : null;

    this._config.mode_request = this._normaliseRequest(config.mode_request);

    // Config can change the static DOM (chip visibility), so rebuild once.
    this._built = false;
    this._pendingTarget = null;
    this._request = null;
    this._modeKey = null;
    this._update();
  }

  _normaliseRequest(raw) {
    if (!raw) return null;

    const mr = { ...REQUEST_DEFAULTS, ...raw };

    if (!mr.service || !String(mr.service).includes(".")) {
      throw new Error(
        "`mode_request.service` must be set, like `script.multisplit_request_mode`"
      );
    }

    if (!Array.isArray(mr.modes) || !mr.modes.length) {
      throw new Error("`mode_request.modes` must be a non-empty list");
    }

    mr.modes = mr.modes.map((m) => String(m));
    mr.timeout = this._num(mr.timeout) ?? REQUEST_DEFAULTS.timeout;

    return mr;
  }

  set hass(hass) {
    const prev = this._hass;
    this._hass = hass;

    if (!this._config) return;

    if (!prev || !this._built || this._watchedChanged(prev, hass)) {
      this._update();
    }
  }

  get hass() {
    return this._hass;
  }

  connectedCallback() {
    if (this._config) this._update();
  }

  disconnectedCallback() {
    clearTimeout(this._commitTimer);
    clearTimeout(this._releaseTimer);
    clearTimeout(this._toastTimer);
    clearTimeout(this._requestTimer);
    this._dragging = false;
  }

  getCardSize() {
    return 5;
  }

  getGridOptions() {
    return {
      columns: 12,
      rows: 5,
      min_columns: 6,
      min_rows: 4,
    };
  }

  static getStubConfig() {
    return { entity: "climate.office" };
  }

  static getConfigElement() {
    return document.createElement("multisplit-climate-card-editor");
  }

  // ------------------------------------------------------------------- state

  _watchedEntities() {
    const c = this._config;
    const rs = c.room_status;

    return [
      c.entity,
      c.group_mode,
      c.current_temperature_entity,
      c.target_entity,
      rs?.demand_entity,
      rs?.execution_entity,
      rs?.reason_entity,
    ].filter(Boolean);
  }

  _watchedChanged(prev, next) {
    // Home Assistant replaces the state object whenever anything about the
    // entity changes, so identity comparison is enough.
    return this._watchedEntities().some(
      (id) => prev.states[id] !== next.states[id]
    );
  }

  _state(entityId) {
    return entityId && this._hass ? this._hass.states[entityId] : null;
  }

  _climate() {
    return this._state(this._config.entity);
  }

  _num(value) {
    if (value === null || value === undefined || value === "") return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }

  /** Numeric state of an entity, or null when it is unavailable. */
  _entityValue(entityId) {
    const state = this._state(entityId);
    if (!state || UNAVAILABLE_STATES.has(String(state.state).toLowerCase())) {
      return null;
    }
    return this._num(state.state);
  }

  _groupMode() {
    const state = this._state(this._config.group_mode);
    if (!state) return null;

    const value = String(state.state || "").toLowerCase();
    return UNAVAILABLE_STATES.has(value) ? null : value;
  }

  _currentTemperature() {
    if (this._config.current_temperature_entity) {
      const value = this._entityValue(
        this._config.current_temperature_entity
      );
      if (value !== null) return value;
    }

    const climate = this._climate();
    return climate ? this._num(climate.attributes.current_temperature) : null;
  }

  /**
   * Which climate attribute holds the setpoint. Only meaningful when
   * `target_entity` is not configured.
   */
  _resolvedTargetAttribute() {
    const configured = this._config.target_attribute;
    if (configured && configured !== "auto") return configured;

    const climate = this._climate();
    if (!climate) return "temperature";

    const attrs = climate.attributes;

    if (this._num(attrs.temperature) !== null) return "temperature";

    const mode = this._groupMode() || String(climate.state || "").toLowerCase();

    if (mode === "heat" || mode === "heating") return "target_temp_low";
    if (mode === "cool" || mode === "cooling") return "target_temp_high";

    if (this._num(attrs.target_temp_low) !== null) return "target_temp_low";
    if (this._num(attrs.target_temp_high) !== null) return "target_temp_high";

    return "temperature";
  }

  _targetTemperature() {
    if (this._pendingTarget !== null) return this._pendingTarget;

    // A configured helper is authoritative — never fall through to the
    // climate entity, or an unavailable helper would silently show the
    // wrong number.
    if (this._config.target_entity) {
      return this._entityValue(this._config.target_entity);
    }

    const climate = this._climate();
    if (!climate) return null;

    return this._num(climate.attributes[this._resolvedTargetAttribute()]);
  }

  _limits() {
    const climate = this._climate();
    const attrs = climate ? climate.attributes : {};

    const step =
      this._num(this._config.step) ??
      this._num(attrs.target_temp_step) ??
      0.5;

    const min = this._num(this._config.min) ?? this._num(attrs.min_temp) ?? 16;

    let max = this._num(this._config.max) ?? this._num(attrs.max_temp) ?? 30;

    // Guard against a degenerate range producing NaN geometry.
    if (max <= min) max = min + Math.abs(step || 1);

    return { min, max, step: Math.abs(step) || 0.5 };
  }

  _isUnavailable() {
    const climate = this._climate();
    if (!climate) return true;
    return UNAVAILABLE_STATES.has(String(climate.state).toLowerCase());
  }

  _effectiveMode() {
    const group = this._groupMode();
    if (group) return group;

    const climate = this._climate();
    return climate ? String(climate.state || "").toLowerCase() : "";
  }

  _activeColor() {
    if (this._isUnavailable()) return "var(--disabled-color, #8a8a8a)";

    const climate = this._climate();
    const action = climate?.attributes?.hvac_action;

    const key = String(action || this._effectiveMode()).toLowerCase();

    if (key === "off" || key === "idle") {
      return "var(--disabled-color, #8a8a8a)";
    }

    return MODE_COLORS[key] || "var(--primary-color)";
  }

  _name() {
    if (this._config.name) return this._config.name;

    const climate = this._climate();
    return climate?.attributes?.friendly_name || this._config.entity;
  }

  _modeLabel() {
    const group = this._groupMode();
    if (group) return this._modeText(group);

    const climate = this._climate();
    return climate ? this._modeText(climate.state) : "UNKNOWN";
  }

  _actionLabel() {
    const climate = this._climate();
    const action = climate?.attributes?.hvac_action;

    if (action) return String(action).replace(/_/g, " ");

    return climate
      ? String(climate.state || "").replace(/_/g, " ")
      : "unavailable";
  }

  // ------------------------------------------------------------ room status

  _modeText(mode) {
    if (!mode) return "";
    return MODE_LABELS[mode] || String(mode).replace(/_/g, " ").toUpperCase();
  }

  /** Read one logical state from either an entity's state or an attribute. */
  _statusValue(entityId, attribute) {
    const state = this._state(entityId);
    if (!state) return null;

    const raw = attribute ? state.attributes[attribute] : state.state;
    const value = String(raw ?? "").trim().toLowerCase();

    if (!value || UNAVAILABLE_STATES.has(value)) return null;

    return value.replace(/[\s-]+/g, "_");
  }

  /**
   * The room's position in the group, as the backend reports it. The card
   * decides nothing here — it only translates the published states into
   * something a person can read.
   */
  _roomStatus() {
    const rs = this._config.room_status;
    if (!rs) return null;

    const demandKey = this._statusValue(rs.demand_entity, rs.demand_attribute);
    const executionKey = this._statusValue(
      rs.execution_entity,
      rs.execution_attribute
    );

    if (!demandKey && !executionKey) return null;

    const demand = demandKey
      ? DEMAND_STATES[demandKey] || {
          label: demandKey.replace(/_/g, " "),
          tone: "idle",
        }
      : null;

    const execution = executionKey
      ? EXECUTION_STATES[executionKey] || {
          label: executionKey.replace(/_/g, " "),
          tone: "idle",
        }
      : null;

    return {
      demand,
      execution,
      reason: this._statusReason(rs),
      tone: this._statusTone(demand, execution),
      verdict: this._statusVerdict(demand, execution),
      changedAt: this._statusChangedAt(rs),
    };
  }

  /** Newest last_changed across the status entities, in epoch ms. */
  _statusChangedAt(rs) {
    let newest = 0;

    for (const entityId of [rs.demand_entity, rs.execution_entity]) {
      const state = this._state(entityId);
      if (!state) continue;

      const changed = Date.parse(state.last_changed || "");
      if (Number.isFinite(changed) && changed > newest) newest = changed;
    }

    return newest || null;
  }

  _statusReason(rs) {
    for (const entityId of [rs.reason_entity, rs.execution_entity, rs.demand_entity]) {
      if (!entityId) continue;

      const state = this._state(entityId);
      if (!state) continue;

      // Prefer the attribute wherever it exists, but a dedicated reason
      // entity usually carries its text as the state.
      let raw = rs.reason_attribute
        ? state.attributes[rs.reason_attribute]
        : null;

      if (
        (raw === undefined || raw === null || raw === "") &&
        entityId === rs.reason_entity
      ) {
        raw = state.state;
      }

      const value = raw === undefined || raw === null ? "" : String(raw).trim();

      if (value && !UNAVAILABLE_STATES.has(value.toLowerCase())) return value;
    }

    return null;
  }

  /**
   * Whether the room's intent is currently being acted on. This is the
   * accepted / rejected distinction, derived from the execution state rather
   * than invented by the card.
   */
  _statusVerdict(demand, execution) {
    if (!execution) return null;

    if (execution.tone === "blocked") return "rejected";
    if (execution.tone === "wait") return "pending";

    // Being asleep is a legitimate way to satisfy a room, not a refusal.
    return execution.accepted ? "accepted" : null;
  }

  /**
   * The status block, with a request this card just made folded in. A local
   * request only shows until Home Assistant publishes a verdict of its own.
   */
  _statusView() {
    const status = this._roomStatus();
    const request = this._request;

    if (!request) return status;

    if (request.timedOut) {
      return {
        demand: {
          label: `${this._modeText(request.mode)} requested`,
          tone: "idle",
        },
        execution: { label: "No response from the system" },
        reason: status?.reason || null,
        tone: "idle",
        verdict: null,
      };
    }

    return {
      demand: {
        label: `${this._modeText(request.mode)} requested`,
        tone: "idle",
      },
      execution: { label: "Sent to the group — waiting for a decision" },
      reason: status?.reason || null,
      tone: "wait",
      verdict: "pending",
    };
  }

  /** Drop the local request once the backend has ruled on it. */
  _retireAnsweredRequest() {
    const request = this._request;
    if (!request) return;

    const status = this._roomStatus();
    if (!status || !status.verdict) return;

    // Only a verdict published *after* the request counts, or a stale
    // "accepted" from before would swallow the new request instantly.
    if (status.changedAt && status.changedAt < request.at) return;

    clearTimeout(this._requestTimer);
    this._request = null;
  }

  async _requestMode(mode) {
    const mr = this._config.mode_request;
    if (!mr || !this._hass || !mode) return;

    this._request = { mode, at: Date.now(), timedOut: false };
    this._update();

    clearTimeout(this._requestTimer);
    this._requestTimer = setTimeout(() => {
      if (this._request && !this._request.timedOut) {
        this._request.timedOut = true;
        this._update();
      }
    }, mr.timeout * 1000);

    const [domain, service] = mr.service.split(".");

    try {
      // Deliberately not `entity_id`: that would make Home Assistant treat
      // the climate entity as the service target rather than a variable.
      await this._hass.callService(domain, service, {
        mode,
        climate_entity: this._config.entity,
        // The card already knows the room's group; passing it keeps group
        // membership defined in one place — the blueprint — rather than
        // copied into the backend package.
        ...(this._config.group_mode
          ? { group_entity: this._config.group_mode }
          : {}),
        ...(mr.service_data || {}),
      });
    } catch (err) {
      console.error("multisplit-climate-card: mode request failed", err);
      this._toast(err?.message || "Could not send the mode request");

      clearTimeout(this._requestTimer);
      this._request = null;
      this._update();
    }
  }

  _statusTone(demand, execution) {
    if (execution && (execution.tone === "blocked" || execution.tone === "wait")) {
      return execution.tone;
    }

    return demand ? demand.tone : "idle";
  }

  // ------------------------------------------------------------------ numbers

  _decimals(step) {
    const text = String(step);
    if (!text.includes(".")) return 0;
    return Math.min(text.split(".")[1].length, 2);
  }

  _roundToStep(value) {
    const { min, max, step } = this._limits();

    const bounded = Math.max(min, Math.min(max, value));
    const rounded = Math.round((bounded - min) / step) * step + min;

    return Number(rounded.toFixed(this._decimals(step)));
  }

  _formatTemp(value) {
    if (value === null || value === undefined || Number.isNaN(value)) {
      return "–";
    }

    const { step } = this._limits();
    return Number(value).toFixed(step < 1 ? 1 : 0);
  }

  // ----------------------------------------------------------------- geometry

  _polar(radius, angleDeg) {
    const rad = ((angleDeg - 90) * Math.PI) / 180;
    return {
      x: DIAL.cx + radius * Math.cos(rad),
      y: DIAL.cy + radius * Math.sin(rad),
    };
  }

  _arcPath(radius, fromAngle, toAngle) {
    const sweep = toAngle - fromAngle;
    const start = this._polar(radius, fromAngle);

    // Degenerate arc: emit a move-only path so nothing is painted.
    if (sweep <= 0.01) {
      return `M ${start.x.toFixed(2)} ${start.y.toFixed(2)}`;
    }

    const end = this._polar(radius, toAngle);
    const large = sweep > 180 ? 1 : 0;

    return [
      `M ${start.x.toFixed(2)} ${start.y.toFixed(2)}`,
      `A ${radius} ${radius} 0 ${large} 1`,
      `${end.x.toFixed(2)} ${end.y.toFixed(2)}`,
    ].join(" ");
  }

  _angleForValue(value) {
    const { min, max } = this._limits();
    const clamped = Math.max(min, Math.min(max, value ?? min));

    return DIAL.start + ((clamped - min) / (max - min)) * DIAL.sweep;
  }

  _valueForPointer(event) {
    const rect = this._els.dial.getBoundingClientRect();

    const x = event.clientX - rect.left - rect.width / 2;
    const y = event.clientY - rect.top - rect.height / 2;

    // atan2 gives 0 at 3 o'clock; +90 converts to clockwise-from-12.
    let angle = (Math.atan2(y, x) * 180) / Math.PI + 90;

    while (angle < DIAL.start) angle += 360;
    while (angle >= DIAL.start + 360) angle -= 360;

    // Inside the gap, snap to whichever end of the track is nearer rather
    // than clamping — clamping sent the whole gap to the minimum.
    if (angle > DIAL_END) {
      angle = angle < GAP_MID ? DIAL_END : DIAL.start;
    }

    const { min, max } = this._limits();
    const ratio = (angle - DIAL.start) / DIAL.sweep;

    return this._roundToStep(min + ratio * (max - min));
  }

  // ------------------------------------------------------------------ writing

  _setPending(value) {
    if (value === null || value === this._pendingTarget) return;

    this._pendingTarget = value;
    this._syncTarget();
  }

  _queueCommit(value, delay = COMMIT_DEBOUNCE_MS) {
    clearTimeout(this._commitTimer);
    this._commitTimer = setTimeout(() => this._commitTarget(value), delay);
  }

  async _commitTarget(value) {
    clearTimeout(this._commitTimer);

    if (!this._hass || value === null) return;

    const rounded = this._roundToStep(value);
    const token = ++this._commitSeq;

    try {
      if (this._config.target_entity) {
        const [domain] = this._config.target_entity.split(".");

        await this._hass.callService(domain, "set_value", {
          entity_id: this._config.target_entity,
          value: rounded,
        });
      } else {
        const attribute = this._resolvedTargetAttribute();
        const data = { entity_id: this._config.entity };

        if (attribute === "target_temp_low") {
          data.target_temp_low = rounded;
        } else if (attribute === "target_temp_high") {
          data.target_temp_high = rounded;
        } else {
          data.temperature = rounded;
        }

        await this._hass.callService("climate", "set_temperature", data);
      }
    } catch (err) {
      // Surface it instead of becoming an unhandled rejection.
      console.error("multisplit-climate-card: set temperature failed", err);
      this._toast(err?.message || "Could not set the temperature");

      this._pendingTarget = null;
      this._update();
      return;
    }

    // Hold the optimistic value briefly so the dial does not snap back
    // before the backend echoes the new setpoint.
    clearTimeout(this._releaseTimer);
    this._releaseTimer = setTimeout(() => {
      if (token !== this._commitSeq || this._dragging) return;

      this._pendingTarget = null;
      this._update();
    }, PENDING_HOLD_MS);
  }

  _nudge(delta) {
    const current = this._targetTemperature();
    const { min } = this._limits();

    const next = this._roundToStep((current ?? min) + delta);

    this._setPending(next);
    this._queueCommit(next);
  }

  _toast(message) {
    if (!this._els) return;

    this._els.toast.textContent = message;
    this._els.toast.hidden = false;

    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => {
      this._els.toast.hidden = true;
    }, 5000);
  }

  // -------------------------------------------------------------------- events

  _attachEvents() {
    const { dial, hit, minus, plus, more } = this._els;

    hit.addEventListener("pointerdown", (event) => {
      if (event.pointerType === "mouse" && event.button !== 0) return;
      if (this._isUnavailable()) return;

      event.preventDefault();

      this._dragging = true;
      hit.setPointerCapture?.(event.pointerId);
      dial.focus?.({ preventScroll: true });

      this._setPending(this._valueForPointer(event));
    });

    hit.addEventListener("pointermove", (event) => {
      if (!this._dragging) return;

      event.preventDefault();
      this._setPending(this._valueForPointer(event));
    });

    const finish = (event) => {
      if (!this._dragging) return;

      this._dragging = false;
      hit.releasePointerCapture?.(event.pointerId);

      this._commitTarget(this._pendingTarget);
    };

    hit.addEventListener("pointerup", finish);

    const cancel = () => {
      if (!this._dragging) return;

      this._dragging = false;
      this._pendingTarget = null;
      this._update();
    };

    hit.addEventListener("pointercancel", cancel);
    // Safety net: if capture is lost for any reason, do not stay stuck in
    // dragging state and start tracking an unpressed pointer.
    hit.addEventListener("lostpointercapture", () => {
      if (this._dragging) {
        this._dragging = false;
        this._commitTarget(this._pendingTarget);
      }
    });

    dial.addEventListener("keydown", (event) => {
      if (this._isUnavailable()) return;

      const { min, max, step } = this._limits();
      const current = this._targetTemperature() ?? min;

      let next = null;

      switch (event.key) {
        case "ArrowUp":
        case "ArrowRight":
        case "Up": // legacy key names
        case "Right":
          next = current + step;
          break;
        case "ArrowDown":
        case "ArrowLeft":
        case "Down":
        case "Left":
          next = current - step;
          break;
        case "PageUp":
          next = current + step * 5;
          break;
        case "PageDown":
          next = current - step * 5;
          break;
        case "Home":
          next = min;
          break;
        case "End":
          next = max;
          break;
        default:
          return;
      }

      event.preventDefault();

      const rounded = this._roundToStep(next);
      this._setPending(rounded);
      this._queueCommit(rounded);
    });

    minus.addEventListener("click", () => this._nudge(-this._limits().step));
    plus.addEventListener("click", () => this._nudge(this._limits().step));

    this._els.requestRow.addEventListener("click", (event) => {
      const button = event.target.closest(".request-button");
      if (!button || button.disabled) return;

      this._requestMode(button.dataset.mode);
    });

    more.addEventListener("click", () => {
      this.dispatchEvent(
        new CustomEvent("hass-more-info", {
          detail: { entityId: this._config.entity },
          bubbles: true,
          composed: true,
        })
      );
    });
  }

  // --------------------------------------------------------------------- DOM

  _build() {
    const trackPath = this._arcPath(DIAL.radius, DIAL.start, DIAL_END);

    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }

        ha-card {
          padding: 16px 16px 14px;
          overflow: hidden;
          --msc-active: var(--primary-color);
        }

        .error {
          padding: 16px;
          color: var(--error-color, #db4437);
        }

        .header {
          display: flex;
          align-items: center;
          justify-content: space-between;
          margin-bottom: 4px;
        }

        .name {
          font-size: 18px;
          font-weight: 500;
          color: var(--primary-text-color);
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }

        .more {
          border: 0;
          background: transparent;
          color: var(--secondary-text-color);
          cursor: pointer;
          font-size: 22px;
          line-height: 1;
          padding: 4px 8px;
          border-radius: 50%;
          flex: 0 0 auto;
        }

        .more:hover { background: var(--secondary-background-color); }

        .dial-wrap {
          display: flex;
          justify-content: center;
          align-items: center;
        }

        svg {
          width: min(100%, 320px);
          height: auto;
          touch-action: none;
          user-select: none;
          -webkit-user-select: none;
          outline: none;
        }

        .hit {
          fill: none;
          stroke: transparent;
          stroke-width: ${DIAL.hitWidth};
          stroke-linecap: butt;
          pointer-events: stroke;
          cursor: pointer;
        }

        .track {
          fill: none;
          stroke: var(--divider-color);
          stroke-width: 13;
          stroke-linecap: round;
          opacity: 0.7;
          pointer-events: none;
        }

        .active {
          fill: none;
          stroke: var(--msc-active);
          stroke-width: 13;
          stroke-linecap: round;
          opacity: 0.75;
          pointer-events: none;
          transition: stroke 180ms ease-in-out;
        }

        .target-dot {
          fill: var(--primary-text-color);
          stroke: var(--card-background-color, var(--ha-card-background));
          stroke-width: 4;
          pointer-events: none;
        }

        .current-dot {
          fill: var(--card-background-color, var(--ha-card-background));
          stroke: var(--primary-text-color);
          stroke-width: 3;
          pointer-events: none;
        }

        svg:focus-visible .focus-ring {
          stroke: var(--msc-active);
          stroke-width: 2;
          fill: none;
          opacity: 1;
        }

        .focus-ring { opacity: 0; pointer-events: none; }

        .hidden { visibility: hidden; }

        text { pointer-events: none; }

        .mode {
          text-anchor: middle;
          font-size: 11px;
          text-transform: uppercase;
          letter-spacing: 1.1px;
          fill: var(--secondary-text-color);
        }

        .current {
          text-anchor: middle;
          font-size: 42px;
          font-weight: 400;
          fill: var(--primary-text-color);
        }

        .current-label {
          text-anchor: middle;
          font-size: 12px;
          fill: var(--secondary-text-color);
        }

        .target {
          text-anchor: middle;
          font-size: 17px;
          font-weight: 500;
          fill: var(--primary-text-color);
        }

        .controls {
          display: flex;
          align-items: center;
          justify-content: center;
          gap: 26px;
          margin-top: -17px;
        }

        .temp-button {
          width: 46px;
          height: 46px;
          border-radius: 50%;
          border: 1px solid var(--divider-color);
          background: var(--secondary-background-color);
          color: var(--primary-text-color);
          font-size: 25px;
          line-height: 1;
          cursor: pointer;
        }

        .temp-button:disabled {
          cursor: default;
          opacity: 0.45;
        }

        .request-row {
          display: flex;
          flex-wrap: wrap;
          justify-content: center;
          gap: 6px;
          margin-top: 14px;
        }

        .request-row-label {
          flex: 1 0 100%;
          text-align: center;
          font-size: 11px;
          letter-spacing: 0.5px;
          text-transform: uppercase;
          color: var(--secondary-text-color);
          margin-bottom: 2px;
        }

        .request-button {
          border: 1px solid var(--divider-color);
          background: transparent;
          color: var(--primary-text-color);
          border-radius: 14px;
          padding: 7px 14px;
          font-size: 12px;
          letter-spacing: 0.6px;
          line-height: 1;
          cursor: pointer;
        }

        .request-button:hover:not(:disabled) {
          border-color: var(--msc-active);
        }

        .request-button[data-current="true"] {
          border-color: var(--msc-active);
          color: var(--secondary-text-color);
        }

        .request-button:disabled {
          cursor: default;
          opacity: 0.45;
        }

        .room-status {
          margin-top: 14px;
          padding: 9px 11px;
          border-radius: 10px;
          font-size: 12px;
          line-height: 1.4;
          background: var(--secondary-background-color);
          border-left: 3px solid var(--secondary-text-color);
        }

        .room-status.heat { border-left-color: var(--state-climate-heat-color, #ff8100); }
        .room-status.cool { border-left-color: var(--state-climate-cool-color, #2b9af9); }
        .room-status.ok { border-left-color: var(--success-color, #43a047); }
        .room-status.wait { border-left-color: var(--warning-color, #ffa600); }
        .room-status.blocked { border-left-color: var(--error-color, #db4437); }

        .room-status-line {
          display: flex;
          align-items: baseline;
          flex-wrap: wrap;
          gap: 6px;
          color: var(--primary-text-color);
        }

        .room-status-demand { font-weight: 600; }

        .room-status-verdict {
          font-size: 11px;
          letter-spacing: 0.5px;
          text-transform: uppercase;
          padding: 2px 7px;
          border-radius: 10px;
          background: var(--card-background-color, var(--ha-card-background));
          color: var(--secondary-text-color);
        }

        .room-status.ok .room-status-verdict { color: var(--success-color, #43a047); }
        .room-status.wait .room-status-verdict { color: var(--warning-color, #ffa600); }
        .room-status.blocked .room-status-verdict { color: var(--error-color, #db4437); }

        .room-status-execution,
        .room-status-why {
          display: block;
          margin-top: 3px;
          color: var(--secondary-text-color);
        }

        .status {
          margin-top: 14px;
          display: flex;
          gap: 8px;
          flex-wrap: wrap;
          justify-content: center;
        }

        .chip {
          background: var(--secondary-background-color);
          color: var(--secondary-text-color);
          border-radius: 16px;
          padding: 6px 10px;
          font-size: 12px;
          line-height: 1;
        }

        .chip strong {
          color: var(--primary-text-color);
          font-weight: 500;
        }

        .toast {
          margin-top: 10px;
          text-align: center;
          font-size: 12px;
          color: var(--error-color, #db4437);
        }

        .unavailable .dial-wrap,
        .unavailable .controls { opacity: 0.5; }
      </style>

      <ha-card>
        <div class="error" hidden></div>

        <div class="content">
          <div class="header">
            <div class="name"></div>
            <button
              id="more"
              class="more"
              type="button"
              aria-label="More info"
            >⋮</button>
          </div>

          <div class="dial-wrap">
            <svg
              id="dial"
              viewBox="0 0 240 240"
              role="slider"
              tabindex="0"
              aria-valuemin="0"
              aria-valuemax="0"
            >
              <path class="track" d="${trackPath}"></path>
              <path class="active" d=""></path>

              <circle class="current-dot" r="5" cx="0" cy="0"></circle>
              <circle class="target-dot" r="7" cx="0" cy="0"></circle>

              <circle
                class="focus-ring"
                cx="${DIAL.cx}"
                cy="${DIAL.cy}"
                r="${DIAL.radius + DIAL.hitWidth / 2}"
              ></circle>

              <text class="mode" x="120" y="82"></text>
              <text class="current" x="120" y="126"></text>
              <text class="current-label" x="120" y="145">CURRENT</text>
              <text class="target" x="120" y="169"></text>

              <path class="hit" d="${trackPath}"></path>
            </svg>
          </div>

          <div class="controls">
            <button
              id="minus"
              class="temp-button"
              type="button"
              aria-label="Decrease target temperature"
            >−</button>
            <button
              id="plus"
              class="temp-button"
              type="button"
              aria-label="Increase target temperature"
            >+</button>
          </div>

          <div class="request-row" hidden>
            <span class="request-row-label">Ask the group for</span>
          </div>

          <div class="room-status" hidden>
            <div class="room-status-line">
              <span class="room-status-demand"></span>
              <span class="room-status-verdict" hidden></span>
            </div>
            <span class="room-status-execution" hidden></span>
            <span class="room-status-why" hidden></span>
          </div>

          <div class="status">
            <div class="chip" id="chip-group">Group <strong></strong></div>
            <div class="chip" id="chip-unit">Unit <strong></strong></div>
          </div>

          <div class="toast" hidden></div>
        </div>
      </ha-card>
    `;

    const root = this.shadowRoot;

    this._els = {
      card: root.querySelector("ha-card"),
      error: root.querySelector(".error"),
      content: root.querySelector(".content"),
      name: root.querySelector(".name"),
      more: root.querySelector("#more"),
      dial: root.querySelector("#dial"),
      hit: root.querySelector(".hit"),
      active: root.querySelector(".active"),
      currentDot: root.querySelector(".current-dot"),
      targetDot: root.querySelector(".target-dot"),
      modeText: root.querySelector(".mode"),
      currentText: root.querySelector(".current"),
      targetText: root.querySelector(".target"),
      minus: root.querySelector("#minus"),
      plus: root.querySelector("#plus"),
      requestRow: root.querySelector(".request-row"),
      roomStatus: root.querySelector(".room-status"),
      roomStatusDemand: root.querySelector(".room-status-demand"),
      roomStatusVerdict: root.querySelector(".room-status-verdict"),
      roomStatusExecution: root.querySelector(".room-status-execution"),
      roomStatusWhy: root.querySelector(".room-status-why"),
      chipGroup: root.querySelector("#chip-group"),
      chipGroupValue: root.querySelector("#chip-group strong"),
      chipUnitValue: root.querySelector("#chip-unit strong"),
      toast: root.querySelector(".toast"),
    };

    this._attachEvents();
    this._built = true;
  }

  /** Patch only the target-dependent bits — called on every drag sample. */
  _syncTarget() {
    if (!this._built) return;

    const target = this._targetTemperature();
    const { min, max } = this._limits();
    const els = this._els;

    const hasTarget = target !== null;
    const angle = this._angleForValue(target);
    const point = this._polar(DIAL.radius, angle);

    els.active.setAttribute(
      "d",
      hasTarget ? this._arcPath(DIAL.radius, DIAL.start, angle) : ""
    );

    els.targetDot.setAttribute("cx", point.x.toFixed(2));
    els.targetDot.setAttribute("cy", point.y.toFixed(2));
    els.targetDot.classList.toggle("hidden", !hasTarget);

    els.targetText.textContent = `Target ${this._formatTemp(target)}°`;

    els.dial.setAttribute("aria-valuemin", String(min));
    els.dial.setAttribute("aria-valuemax", String(max));

    if (hasTarget) {
      els.dial.setAttribute("aria-valuenow", String(target));
      els.dial.setAttribute(
        "aria-valuetext",
        `${this._formatTemp(target)} degrees`
      );
    } else {
      els.dial.removeAttribute("aria-valuenow");
      els.dial.removeAttribute("aria-valuetext");
    }
  }

  _syncRequestRow() {
    const els = this._els;
    const mr = this._config.mode_request;

    if (!mr) {
      els.requestRow.hidden = true;
      return;
    }

    els.requestRow.hidden = false;

    const key = mr.modes.join("|");

    if (key !== this._modeKey) {
      this._modeKey = key;

      for (const stale of els.requestRow.querySelectorAll(".request-button")) {
        stale.remove();
      }

      for (const mode of mr.modes) {
        const button = document.createElement("button");

        button.type = "button";
        button.className = "request-button";
        button.dataset.mode = mode;
        button.textContent = this._modeText(mode);

        els.requestRow.appendChild(button);
      }
    }

    const group = this._groupMode();
    const busy = Boolean(this._request && !this._request.timedOut);
    const unavailable = this._isUnavailable();

    for (const button of els.requestRow.querySelectorAll(".request-button")) {
      const isCurrent = group === button.dataset.mode;

      button.dataset.current = String(isCurrent);
      button.disabled = busy || unavailable || isCurrent;
      button.title = isCurrent
        ? `The group is already in ${this._modeText(group)}`
        : `Ask the group to switch to ${this._modeText(button.dataset.mode)}`;
    }
  }

  _syncRoomStatus() {
    const els = this._els;
    const status = this._statusView();

    if (!status) {
      els.roomStatus.hidden = true;
      return;
    }

    els.roomStatus.hidden = false;
    els.roomStatus.className = `room-status ${status.tone}`;

    els.roomStatusDemand.textContent = status.demand
      ? status.demand.label
      : "Room status";

    const verdict = status.verdict
      ? VERDICT_LABELS[status.verdict]
      : null;

    els.roomStatusVerdict.textContent = verdict || "";
    els.roomStatusVerdict.hidden = !verdict;

    els.roomStatusExecution.textContent = status.execution
      ? status.execution.label
      : "";
    els.roomStatusExecution.hidden = !status.execution;

    // The backend owns the explanation; fall back to naming the group mode
    // so a blocked room is never left unexplained.
    let why = status.reason;

    if (!why && (status.tone === "blocked" || status.tone === "wait")) {
      const group = this._groupMode();
      why = group ? `Group mode is ${this._modeText(group)}` : null;
    }

    els.roomStatusWhy.textContent = why || "";
    els.roomStatusWhy.hidden = !why;
  }

  _update() {
    if (!this.shadowRoot || !this._config) return;

    if (!this._built) this._build();

    const els = this._els;

    if (!this._hass) {
      els.error.textContent = "Loading…";
      els.error.hidden = false;
      els.content.hidden = true;
      return;
    }

    const climate = this._climate();

    if (!climate) {
      els.error.textContent = `Entity not found: ${this._config.entity}`;
      els.error.hidden = false;
      els.content.hidden = true;
      return;
    }

    els.error.hidden = true;
    els.content.hidden = false;

    const unavailable = this._isUnavailable();
    const current = this._currentTemperature();

    els.card.classList.toggle("unavailable", unavailable);
    els.card.style.setProperty("--msc-active", this._activeColor());

    els.name.textContent = this._name();
    els.dial.setAttribute("aria-label", `${this._name()} target temperature`);

    els.modeText.textContent = this._actionLabel();
    els.currentText.textContent = `${this._formatTemp(current)}°`;

    const currentPoint = this._polar(
      DIAL.radius,
      this._angleForValue(current)
    );

    els.currentDot.setAttribute("cx", currentPoint.x.toFixed(2));
    els.currentDot.setAttribute("cy", currentPoint.y.toFixed(2));
    els.currentDot.classList.toggle("hidden", current === null);

    els.minus.disabled = unavailable;
    els.plus.disabled = unavailable;
    els.dial.style.cursor = unavailable ? "default" : "pointer";

    this._retireAnsweredRequest();
    this._syncRequestRow();
    this._syncRoomStatus();

    els.chipGroup.hidden = this._config.show_group_mode === false;
    els.chipGroupValue.textContent = this._modeLabel();
    els.chipUnitValue.textContent = this._modeText(climate.state);

    this._syncTarget();
  }
}

// ---------------------------------------------------------------------- editor

const EDITOR_SCHEMA = [
  { name: "entity", required: true, selector: { entity: { domain: "climate" } } },
  { name: "name", selector: { text: {} } },
  {
    name: "group_mode",
    selector: { entity: { domain: ["sensor", "input_select", "select", "climate"] } },
  },
  {
    name: "current_temperature_entity",
    selector: { entity: { domain: ["sensor", "number", "input_number"] } },
  },
  {
    name: "target_entity",
    selector: { entity: { domain: ["input_number", "number"] } },
  },
  {
    name: "target_attribute",
    selector: {
      select: {
        mode: "dropdown",
        options: [
          { value: "auto", label: "Auto-detect" },
          { value: "temperature", label: "temperature" },
          { value: "target_temp_low", label: "target_temp_low" },
          { value: "target_temp_high", label: "target_temp_high" },
        ],
      },
    },
  },
  {
    type: "grid",
    name: "",
    schema: [
      { name: "min", selector: { number: { mode: "box", step: "any" } } },
      { name: "max", selector: { number: { mode: "box", step: "any" } } },
      { name: "step", selector: { number: { mode: "box", step: "any" } } },
    ],
  },
  { name: "show_group_mode", selector: { boolean: {} } },
];

const EDITOR_LABELS = {
  entity: "Climate entity (required)",
  name: "Name",
  group_mode: "Shared mode entity",
  current_temperature_entity: "Current temperature override",
  target_entity: "Setpoint helper",
  target_attribute: "Setpoint attribute",
  min: "Min",
  max: "Max",
  step: "Step",
  show_group_mode: "Show group mode chip",
};

class MultiSplitClimateCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = config;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _render() {
    if (!this._hass || !this._config) return;

    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (schema) =>
        EDITOR_LABELS[schema.name] ?? schema.name;

      this._form.addEventListener("value-changed", (event) => {
        event.stopPropagation();

        const config = { ...event.detail.value };

        for (const [key, value] of Object.entries(config)) {
          if (value === "" || value === null || value === undefined) {
            delete config[key];
          }
        }

        this.dispatchEvent(
          new CustomEvent("config-changed", {
            detail: { config },
            bubbles: true,
            composed: true,
          })
        );
      });

      this.appendChild(this._form);
    }

    this._form.hass = this._hass;
    this._form.schema = EDITOR_SCHEMA;
    this._form.data = this._config;
  }
}

// -------------------------------------------------------------- registration

if (!customElements.get("multisplit-climate-card")) {
  customElements.define("multisplit-climate-card", MultiSplitClimateCard);
}

if (!customElements.get("multisplit-climate-card-editor")) {
  customElements.define(
    "multisplit-climate-card-editor",
    MultiSplitClimateCardEditor
  );
}

window.customCards = window.customCards || [];

if (!window.customCards.some((c) => c.type === "multisplit-climate-card")) {
  window.customCards.push({
    type: "multisplit-climate-card",
    name: "Multi-split Climate Card",
    description: "Room control for shared-mode multi-split HVAC systems.",
    preview: false,
    documentationURL: "https://github.com/",
  });
}

console.info(
  `%c MULTISPLIT-CLIMATE-CARD %c ${CARD_VERSION} `,
  "color: white; background: #2b9af9; font-weight: 700;",
  "color: #2b9af9; background: white; font-weight: 700;"
);
