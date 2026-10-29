/*
 * Multi-split schedule card.
 *
 * One line per day, Monday to Sunday. A mark on a line sets the room's target
 * from that time until the next mark -- across midnight, and round the week.
 * Tap a line to drop a mark, drag a mark to move it, tap a mark to change its
 * temperature or remove it. Changes are checked against the room's guardrails
 * as you make them, and saved with the multisplit.set_schedule service, which
 * checks them again: the card is a convenience, the integration decides.
 *
 *   type: custom:multisplit-schedule-card
 *   entities:            # optional; every multi-split room when left out
 *     - climate.erin_s_bedroom
 *
 * Served by the integration itself and loaded on every dashboard, so there is
 * nothing to install and no resource to add.
 */

const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const DAY_LONG = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const DAY = 1440;
const WEEK = 7 * DAY;
const SNAP = 15;
const STEP = 0.5;

const pad = (n) => String(n).padStart(2, "0");
const hhmm = (m) => `${pad(Math.floor(m / 60))}:${pad(m % 60)}`;
const toMinute = (t) => {
  const [h, m] = String(t).split(":").map(Number);
  return h * 60 + m;
};
const fmt = (v) => (Math.round(v * 10) / 10).toString();
const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

/* Cool slate for setbacks through to amber and ember for comfort. Positioned
 * on the room's own range, so a narrow range still reads as a gradient. */
const RAMP = [
  [0.0, [74, 111, 165]],
  [0.35, [143, 176, 201]],
  [0.6, [240, 198, 107]],
  [0.82, [232, 145, 58]],
  [1.0, [196, 81, 44]],
];
function tempColour(t, lo, hi) {
  const x = hi > lo ? clamp((t - lo) / (hi - lo), 0, 1) : 0.5;
  for (let i = 1; i < RAMP.length; i++) {
    const [p1, c1] = RAMP[i];
    const [p0, c0] = RAMP[i - 1];
    if (x <= p1) {
      const f = (x - p0) / (p1 - p0);
      const c = c0.map((v, k) => Math.round(v + (c1[k] - v) * f));
      return `rgb(${c[0]}, ${c[1]}, ${c[2]})`;
    }
  }
  return "rgb(196, 81, 44)";
}

/* The same two guardrails as core/schedule.py, for feedback while editing. */
function problems(week, lo, hi, minBlock) {
  const found = [];
  const flat = [];
  week.forEach((marks, d) =>
    marks.forEach((m) => {
      flat.push([d * DAY + m.minute, m, d]);
      if (m.temperature < lo)
        found.push(`${DAY_LONG[d]} ${hhmm(m.minute)}: ${fmt(m.temperature)}° is below this room's minimum of ${fmt(lo)}°`);
      else if (m.temperature > hi)
        found.push(`${DAY_LONG[d]} ${hhmm(m.minute)}: ${fmt(m.temperature)}° is above this room's maximum of ${fmt(hi)}°`);
    })
  );
  if (flat.length > 1 && minBlock > 0) {
    flat.forEach(([at, m, d], i) => {
      const next = i + 1 < flat.length ? flat[i + 1][0] : flat[0][0] + WEEK;
      if (next - at < minBlock)
        found.push(`${DAY_LONG[d]} ${hhmm(m.minute)}: this setting lasts ${next - at} min, shorter than the ${minBlock} min minimum`);
    });
  }
  return found;
}

/* The temperature in force at the start of day d: the last mark before it,
 * wrapping the week. Null for an empty week. */
function carriedInto(week, d) {
  for (let back = 1; back <= 7; back++) {
    const marks = week[(d - back + 7) % 7];
    if (marks.length) return marks[marks.length - 1].temperature;
  }
  return null;
}

/* Wall-clock parts of `date` in Home Assistant's time zone, which is what
 * schedules are written in -- not the browser's, which can differ. */
function houseClock(hass, date = new Date()) {
  const zone = (hass && hass.config && hass.config.time_zone) || undefined;
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-GB", {
      timeZone: zone, weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }).formatToParts(date).map((p) => [p.type, p.value])
  );
  return {
    day: DAY_SHORT.indexOf(parts.weekday),
    minute: Number(parts.hour) * 60 + Number(parts.minute),
    time: `${parts.hour}:${parts.minute}`,
  };
}

class MultisplitScheduleCard extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    this._room = null;
    this._week = null; // [[{minute, temperature}], ...] being edited
    this._saved = null; // JSON of the week as last read from the entity
    this._dirty = new Set();
    this._selected = null; // {day, index}
    this._drag = null;
    this._error = null;
    this._saving = false;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
  }

  getCardSize() {
    return 9;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6 };
  }

  static getStubConfig() {
    return {};
  }

  set hass(hass) {
    this._hass = hass;
    const rooms = this._rooms();
    if (!rooms.length) return this._renderEmpty();
    if (!this._room || !rooms.includes(this._room)) this._selectRoom(rooms[0]);
    // Home Assistant sets hass on every state change anywhere in the house.
    // Only this room, the away switch and the room list matter here, and a
    // redraw while someone is typing a time would throw their input away.
    const awayId = this._awaySwitch();
    const watched = [hass.states[this._room], awayId && hass.states[awayId], rooms.join()];
    const same = this._watched && watched.every((v, i) => v === this._watched[i]);
    if (same && this.shadowRoot.querySelector("ha-card")) return;
    const active = this.shadowRoot.activeElement;
    if (active && active.closest && active.closest(".editor")) return; // redraw on the next change
    this._watched = watched;
    const attrs = this._attrs();
    const incoming = JSON.stringify(attrs.schedule || {});
    // Follow changes made elsewhere -- unless this person is mid-edit, when
    // their unsaved work wins and the save will tell them if it conflicts.
    if (!this._drag && !this._dirty.size && incoming !== this._saved) this._load();
    if (this._drag) return;
    this._render();
  }

  // -- data --------------------------------------------------------------

  _rooms() {
    if (this._config.entities && this._config.entities.length) return this._config.entities;
    return Object.keys(this._hass.states)
      .filter((id) => {
        const a = this._hass.states[id].attributes;
        return id.startsWith("climate.") && a.room !== undefined && a.min_block_minutes !== undefined;
      })
      .sort();
  }

  _attrs() {
    const s = this._hass.states[this._room];
    return s ? s.attributes : {};
  }

  _awaySwitch() {
    return Object.keys(this._hass.states).find(
      (id) => id.startsWith("switch.") && this._hass.states[id].attributes.multisplit_away === true
    );
  }

  _selectRoom(entity) {
    this._room = entity;
    this._dirty.clear();
    this._selected = null;
    this._error = null;
    this._load();
  }

  _load() {
    this._selected = null;
    const raw = this._attrs().schedule || {};
    this._saved = JSON.stringify(raw);
    this._week = DAYS.map((d) =>
      (raw[d] || []).map((m) => ({ minute: toMinute(m.at), temperature: Number(m.temperature) }))
    );
  }

  _canEdit() {
    const a = this._attrs();
    const user = this._hass.user || {};
    if (user.is_admin) return true;
    if (!a.owner) return false;
    const owner = String(a.owner).trim().toLowerCase();
    return owner === String(user.id || "").toLowerCase() || owner === String(user.name || "").trim().toLowerCase();
  }

  _limits() {
    const a = this._attrs();
    return {
      lo: Number(a.min_target ?? 15),
      hi: Number(a.max_target ?? 26),
      block: Number(a.min_block_minutes ?? 30),
      fallback: Number(a.default_target ?? 20),
    };
  }

  // -- editing -----------------------------------------------------------

  _touch(day) {
    this._week[day].sort((a, b) => a.minute - b.minute);
    this._dirty.add(day);
    this._error = null;
  }

  _valueAt(day, minute) {
    const marks = this._week[day];
    let v = carriedInto(this._week, day);
    for (const m of marks) if (m.minute <= minute) v = m.temperature;
    return v ?? this._limits().fallback;
  }

  _free(day, minute, except) {
    return !this._week[day].some((m) => m !== except && m.minute === minute);
  }

  _addMark(day, minute) {
    const { lo, hi } = this._limits();
    let at = clamp(Math.round(minute / SNAP) * SNAP, 0, DAY - SNAP);
    while (!this._free(day, at) && at < DAY - SNAP) at += SNAP;
    if (!this._free(day, at)) return null;
    const mark = { minute: at, temperature: clamp(this._valueAt(day, at), lo, hi) };
    this._week[day].push(mark);
    this._touch(day);
    this._selected = { day, mark };
    return mark;
  }

  _removeSelected() {
    if (!this._selected) return;
    const { day, mark } = this._selected;
    this._week[day] = this._week[day].filter((m) => m !== mark);
    this._touch(day);
    this._selected = null;
    this._render();
  }

  _nudgeTemp(delta) {
    if (!this._selected) return;
    const { lo, hi } = this._limits();
    const { day, mark } = this._selected;
    mark.temperature = clamp(Math.round((mark.temperature + delta) / STEP) * STEP, lo, hi);
    this._touch(day);
    this._render();
  }

  _moveSelected(minute) {
    if (!this._selected) return;
    const { day, mark } = this._selected;
    const at = clamp(Math.round(minute / SNAP) * SNAP, 0, DAY - SNAP);
    if (at === mark.minute || !this._free(day, at, mark)) return;
    mark.minute = at;
    this._touch(day);
  }

  _copyDay(from, targets) {
    for (const d of targets) {
      if (d === from) continue;
      this._week[d] = this._week[from].map((m) => ({ ...m }));
      this._touch(d);
    }
    this._render();
  }

  _discard() {
    this._dirty.clear();
    this._selected = null;
    this._error = null;
    this._load();
    this._render();
  }

  async _save() {
    const a = this._attrs();
    // One call per distinct day pattern, so "copy to weekdays" is one change.
    const groups = new Map();
    for (const d of [...this._dirty].sort()) {
      const marks = this._week[d].map((m) => ({ at: hhmm(m.minute), temperature: m.temperature }));
      const key = JSON.stringify(marks);
      if (!groups.has(key)) groups.set(key, { marks, days: [] });
      groups.get(key).days.push(DAYS[d]);
    }
    this._saving = true;
    this._render();
    try {
      for (const { marks, days } of groups.values())
        await this._hass.callService("multisplit", "set_schedule", { room: a.room, days, marks });
      this._dirty.clear();
      this._selected = null;
      this._error = null;
    } catch (err) {
      this._error = (err && err.message) || String(err);
    }
    this._saving = false;
    this._render();
  }

  async _toggleAway() {
    const id = this._awaySwitch();
    if (!id) return;
    const on = this._hass.states[id].state === "on";
    await this._hass.callService("switch", on ? "turn_off" : "turn_on", { entity_id: id });
  }

  // -- pointer -----------------------------------------------------------

  _minuteFromEvent(track, ev) {
    const r = track.getBoundingClientRect();
    return ((ev.clientX - r.left) / r.width) * DAY;
  }

  _onTrackDown(ev, day) {
    if (!this._canEdit() || ev.button > 0) return;
    const track = ev.currentTarget;
    const flag = ev.target.closest(".flag");
    let mark;
    if (flag) {
      mark = this._week[day][Number(flag.dataset.index)];
      this._selected = { day, mark };
    } else {
      mark = this._addMark(day, this._minuteFromEvent(track, ev));
      if (!mark) return;
    }
    ev.preventDefault();
    this._drag = { day, mark, track, startX: ev.clientX, moved: false };
    const move = (e) => {
      if (Math.abs(e.clientX - this._drag.startX) > 3) this._drag.moved = true;
      if (!this._drag.moved) return;
      this._moveSelected(this._minuteFromEvent(track, e));
      this._renderRow(day);
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
      this._drag = null;
      this._render();
      const sel = this.shadowRoot.querySelector(".flag.selected");
      if (sel) sel.focus({ preventScroll: true });
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
    this._renderRow(day);
  }

  _onFlagKey(ev, day, index) {
    if (!this._canEdit()) return;
    const mark = this._week[day][index];
    this._selected = { day, mark };
    const keys = {
      ArrowLeft: () => this._moveSelected(mark.minute - SNAP),
      ArrowRight: () => this._moveSelected(mark.minute + SNAP),
      ArrowUp: () => this._nudgeTemp(STEP),
      ArrowDown: () => this._nudgeTemp(-STEP),
      Delete: () => this._removeSelected(),
      Backspace: () => this._removeSelected(),
    };
    if (!keys[ev.key]) return;
    ev.preventDefault();
    keys[ev.key]();
    this._render();
    const sel = this.shadowRoot.querySelector(".flag.selected");
    if (sel) sel.focus({ preventScroll: true });
  }

  // -- rendering ---------------------------------------------------------

  _renderEmpty() {
    this.shadowRoot.innerHTML = `${STYLE}<ha-card><div class="empty">
      No multi-split rooms found. This card shows the rooms of the Multi-split
      climate control integration once it is set up.</div></ha-card>`;
  }

  _rowContent(day) {
    const { lo, hi, fallback } = this._limits();
    const marks = this._week[day];
    const carried = carriedInto(this._week, day);
    const stops = [];
    let at = 0;
    let temp = carried ?? fallback;
    for (const m of marks) {
      if (m.minute > at) stops.push([at, m.minute, temp]);
      at = m.minute;
      temp = m.temperature;
    }
    stops.push([at, DAY, temp]);
    const unset = carried === null;
    const segs = stops
      .map(
        ([a, b, t], i) =>
          `<div class="seg${unset ? " unset" : ""}${i === 0 ? " first" : ""}${i === stops.length - 1 ? " last" : ""}" style="left:${(a / DAY) * 100}%;width:${((b - a) / DAY) * 100}%;background:${tempColour(t, lo, hi)}"></div>`
      )
      .join("");
    const sel = this._selected;
    // Labels sit to the right of their pin. Two marks closer than a label is
    // wide would overlap, so the second steps down a level.
    const width = this._trackWidth || 640;
    const near = (46 / width) * DAY;
    let level = 0;
    const flags = marks
      .map((m, i) => {
        const on = sel && sel.day === day && sel.mark === m;
        level = i > 0 && m.minute - marks[i - 1].minute < near ? 1 - level : 0;
        return `<button class="flag${on ? " selected" : ""}${level ? " low" : ""}" data-index="${i}"
          style="left:${(m.minute / DAY) * 100}%"
          aria-label="${DAY_LONG[day]} ${hhmm(m.minute)}, ${fmt(m.temperature)} degrees"
          ${this._canEdit() ? "" : 'tabindex="-1"'}>
          <span class="pin" style="background:${tempColour(m.temperature, lo, hi)}"></span>
          <span class="tag">${fmt(m.temperature)}°</span></button>`;
      })
      .join("");
    const now = houseClock(this._hass);
    const nowLine =
      now.day === day ? `<div class="now" style="left:${(now.minute / DAY) * 100}%"></div>` : "";
    return `${segs}${nowLine}${flags}`;
  }

  _renderRow(day) {
    const track = this.shadowRoot.querySelector(`.track[data-day="${day}"]`);
    if (track) track.innerHTML = this._rowContent(day);
  }

  _status(a) {
    const target = a.heat_to !== undefined ? `${fmt(a.heat_to)}°` : "";
    const until = (iso) => (iso ? houseClock(this._hass, new Date(iso)).time : null);
    switch (a.target_source) {
      case "override": {
        const t = until(a.override_until);
        return `Turned by hand to ${target}${t ? `, until ${t}` : ""}`;
      }
      case "away":
        return `Away: holding ${fmt(a.away_target)}°`;
      case "away (room)":
        return `Room away: holding ${fmt(a.away_target)}°`;
      case "schedule": {
        const t = until(a.next_schedule_change);
        return `Aiming for ${target} from the schedule${t ? `, next change ${t}` : ""}`;
      }
      default:
        return `No schedule: following the default ${fmt(a.default_target)}°`;
    }
  }

  _render() {
    const a = this._attrs();
    const state = this._hass.states[this._room];
    const name = (state && state.attributes.friendly_name) || this._room;
    const { lo, hi, block } = this._limits();
    const editable = this._canEdit();
    const rooms = this._rooms();
    const found = problems(this._week, lo, hi, block);
    const awayId = this._awaySwitch();
    const away = awayId && this._hass.states[awayId].state === "on";
    const empty = this._week.every((d) => !d.length);
    const sel = this._selected;

    const tabs =
      rooms.length > 1
        ? `<div class="rooms" role="tablist">${rooms
            .map((id) => {
              const s = this._hass.states[id];
              const label = (s && s.attributes.friendly_name) || id;
              return `<button role="tab" class="room${id === this._room ? " on" : ""}" data-room="${esc(id)}"
                aria-selected="${id === this._room}">${esc(label)}</button>`;
            })
            .join("")}</div>`
        : "";

    const hours = [0, 3, 6, 9, 12, 15, 18, 21, 24]
      .map((h) => `<span style="left:${(h / 24) * 100}%">${pad(h)}</span>`)
      .join("");

    const rows = DAYS.map(
      (_, d) => `<div class="row">
        <span class="day${this._dirty.has(d) ? " changed" : ""}" title="${DAY_LONG[d]}">${DAY_SHORT[d]}</span>
        <div class="track${editable ? " editable" : ""}" data-day="${d}">${this._rowContent(d)}</div>
      </div>`
    ).join("");

    const editor =
      sel && editable
        ? `<div class="editor">
            <span class="when">${DAY_LONG[sel.day]}</span>
            <input type="time" class="time" step="900" value="${hhmm(sel.mark.minute)}" aria-label="Time">
            <div class="temp">
              <button class="down" aria-label="Cooler" ${sel.mark.temperature <= lo ? "disabled" : ""}>−</button>
              <span>${fmt(sel.mark.temperature)}°</span>
              <button class="up" aria-label="Warmer" ${sel.mark.temperature >= hi ? "disabled" : ""}>+</button>
            </div>
            <button class="remove">Remove mark</button>
            <span class="copy">Copy ${DAY_SHORT[sel.day]} to
              <button data-copy="weekdays">weekdays</button>
              <button data-copy="weekend">weekend</button>
              <button data-copy="all">every day</button></span>
          </div>`
        : "";

    const hint = editable
      ? empty
        ? `No schedule yet, so this room follows the default ${fmt(this._limits().fallback)}°. Tap a day to drop the first mark.`
        : "Tap a day to add a mark. Drag a mark to move it, or tap it to change it."
      : `Only ${a.owner ? `${esc(a.owner)} or ` : ""}an administrator can change this schedule.`;

    const messages = [
      ...found.map((p) => `<li>${p}</li>`),
      this._error ? `<li>${esc(this._error)}</li>` : "",
    ].join("");

    this.shadowRoot.innerHTML = `${STYLE}<ha-card>
      ${tabs}
      <div class="head">
        <div>
          <h2>${esc(name)}</h2>
          <p class="status">${state ? this._status(a) : "Unavailable"}</p>
          <p class="rails">Allowed ${fmt(lo)}–${fmt(hi)}°. Each setting at least ${block} min.${a.owner ? ` Owner: ${esc(a.owner)}.` : ""}</p>
        </div>
        ${
          awayId
            ? `<button class="away${away ? " on" : ""}" aria-pressed="${!!away}">${away ? "Away is on" : "Away"}</button>`
            : ""
        }
      </div>
      ${away ? `<p class="banner">Away is on. Every room holds ${fmt(a.away_target)}° until it is switched off; the schedule resumes then.</p>` : ""}
      <div class="grid">
        <div class="row axis"><span></span><div class="hours">${hours}</div></div>
        ${rows}
      </div>
      ${editor}
      <p class="hint">${hint}</p>
      ${messages ? `<ul class="problems">${messages}</ul>` : ""}
      ${
        this._dirty.size && editable
          ? `<div class="actions">
              <button class="discard">Discard changes</button>
              <button class="save" ${found.length || this._saving ? "disabled" : ""}>${this._saving ? "Saving…" : "Save schedule"}</button>
            </div>`
          : ""
      }
    </ha-card>`;
    this._bind();
  }

  _bind() {
    const root = this.shadowRoot;
    const first = root.querySelector(".track");
    if (first && first.clientWidth && first.clientWidth !== this._trackWidth) {
      this._trackWidth = first.clientWidth;
      requestAnimationFrame(() => DAYS.forEach((_, d) => this._renderRow(d)));
    }
    root.querySelectorAll(".room").forEach((b) =>
      b.addEventListener("click", () => {
        if (this._dirty.size && !confirm("Discard unsaved changes to this room's schedule?")) return;
        this._selectRoom(b.dataset.room);
        this._render();
      })
    );
    root.querySelectorAll(".track").forEach((t) => {
      const day = Number(t.dataset.day);
      t.addEventListener("pointerdown", (ev) => this._onTrackDown(ev, day));
      t.addEventListener("keydown", (ev) => {
        const flag = ev.target.closest(".flag");
        if (flag) this._onFlagKey(ev, day, Number(flag.dataset.index));
      });
    });
    const on = (sel, fn) => {
      const el = root.querySelector(sel);
      if (el) el.addEventListener("click", fn);
    };
    on(".up", () => this._nudgeTemp(STEP));
    on(".down", () => this._nudgeTemp(-STEP));
    on(".remove", () => this._removeSelected());
    on(".discard", () => this._discard());
    on(".save", () => this._save());
    on(".away", () => this._toggleAway());
    const time = root.querySelector(".time");
    if (time)
      time.addEventListener("change", () => {
        if (time.value) this._moveSelected(toMinute(time.value));
        this._render();
      });
    root.querySelectorAll("[data-copy]").forEach((b) =>
      b.addEventListener("click", () => {
        const sets = { weekdays: [0, 1, 2, 3, 4], weekend: [5, 6], all: [0, 1, 2, 3, 4, 5, 6] };
        this._copyDay(this._selected.day, sets[b.dataset.copy]);
      })
    );
  }
}

const STYLE = `<style>
  :host { display: block; }
  ha-card { padding: 16px; overflow: hidden; }
  .empty { color: var(--secondary-text-color); }
  .rooms { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 14px; }
  .room {
    font: inherit; font-size: 0.9em; padding: 5px 12px; border-radius: 16px; cursor: pointer;
    border: 1px solid var(--divider-color); background: none; color: var(--primary-text-color);
  }
  .room.on { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
  .head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
  h2 { margin: 0; font-size: 1.35em; font-weight: 500; letter-spacing: -0.01em; }
  .status { margin: 4px 0 0; color: var(--primary-text-color); }
  .rails { margin: 2px 0 0; color: var(--secondary-text-color); font-size: 0.88em; }
  .away {
    font: inherit; font-size: 0.9em; padding: 6px 12px; border-radius: 8px; cursor: pointer; white-space: nowrap;
    border: 1px solid var(--divider-color); background: none; color: var(--primary-text-color);
  }
  .away.on { background: rgb(74, 111, 165); border-color: rgb(74, 111, 165); color: #fff; }
  .banner {
    margin: 12px 0 0; padding: 8px 12px; border-radius: 8px; font-size: 0.92em;
    background: color-mix(in srgb, rgb(74, 111, 165) 14%, transparent); color: var(--primary-text-color);
  }
  .grid { margin-top: 14px; }
  .row { display: grid; grid-template-columns: 40px 1fr; align-items: end; gap: 8px; }
  .row + .row { margin-top: 2px; }
  .day { font-size: 0.9em; color: var(--secondary-text-color); padding-bottom: 3px; }
  .day.changed { color: var(--primary-text-color); font-weight: 600; }
  .day.changed::after { content: "•"; color: var(--primary-color); margin-left: 2px; }
  .hours { position: relative; height: 14px; font-size: 0.72em; color: var(--secondary-text-color); }
  .hours span { position: absolute; transform: translateX(-50%); }
  .hours span:first-child { transform: none; }
  .hours span:last-child { transform: translateX(-100%); }
  .track { position: relative; height: 50px; touch-action: none; user-select: none; -webkit-user-select: none; }
  .track.editable { cursor: copy; }
  .seg { position: absolute; bottom: 0; height: 12px; }
  .seg.first { border-top-left-radius: 6px; border-bottom-left-radius: 6px; }
  .seg.last { border-top-right-radius: 6px; border-bottom-right-radius: 6px; }
  .seg.unset { opacity: 0.35; }
  .now { position: absolute; bottom: -3px; height: 18px; width: 2px; margin-left: -1px; background: var(--primary-text-color); border-radius: 1px; }
  .flag {
    position: absolute; bottom: 0; transform: translateX(-1px); padding: 0; border: 0; background: none;
    font: inherit; color: var(--primary-text-color); cursor: grab; height: 50px; width: 2px;
  }
  .flag.low .pin { height: 22px; }
  .flag.low .tag { top: 17px; }
  .track:not(.editable) .flag { cursor: default; }
  .flag .pin { position: absolute; left: 0; bottom: 0; width: 2px; height: 40px; border-radius: 1px; }
  .flag .tag {
    position: absolute; left: 0; top: 0; padding: 0 5px; line-height: 16px; font-size: 0.78em; white-space: nowrap;
    border-radius: 0 6px 6px 0; background: var(--card-background-color); border: 1px solid var(--divider-color); border-left: 0;
  }
  .flag.selected .tag, .flag:focus-visible .tag { border-color: var(--primary-color); color: var(--primary-color); font-weight: 600; }
  .flag:focus { outline: none; }
  .flag:focus-visible .pin { box-shadow: 0 0 0 2px var(--primary-color); }
  .editor {
    display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; margin-top: 14px; padding: 10px 12px;
    border-radius: 8px; background: var(--secondary-background-color);
  }
  .when { font-weight: 500; }
  .time { font: inherit; color: inherit; background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 6px; padding: 3px 6px; }
  .temp { display: inline-flex; align-items: center; gap: 6px; }
  .temp span { min-width: 3.2em; text-align: center; font-weight: 600; font-variant-numeric: tabular-nums; }
  .temp button { font: inherit; width: 30px; height: 30px; border-radius: 50%; border: 1px solid var(--divider-color); background: var(--card-background-color); color: inherit; cursor: pointer; }
  .temp button:disabled { opacity: 0.4; cursor: default; }
  .remove, .copy button, .discard {
    font: inherit; background: none; border: 0; padding: 2px 0; cursor: pointer; color: var(--primary-color);
  }
  .remove { color: var(--error-color, #db4437); }
  .copy { color: var(--secondary-text-color); font-size: 0.92em; }
  .copy button { margin-left: 6px; }
  .hint { margin: 12px 0 0; color: var(--secondary-text-color); font-size: 0.88em; }
  .problems { margin: 8px 0 0; padding-left: 18px; color: var(--error-color, #db4437); font-size: 0.9em; }
  .actions { display: flex; justify-content: flex-end; align-items: center; gap: 16px; margin-top: 12px; }
  .save {
    font: inherit; padding: 8px 16px; border-radius: 8px; border: 0; cursor: pointer;
    background: var(--primary-color); color: var(--text-primary-color, #fff);
  }
  .save:disabled { opacity: 0.45; cursor: default; }
  button:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
  @media (max-width: 420px) {
    .row { grid-template-columns: 32px 1fr; }
    .copy { flex-basis: 100%; }
  }
</style>`;

customElements.define("multisplit-schedule-card", MultisplitScheduleCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "multisplit-schedule-card",
  name: "Multi-split schedule",
  description: "Each room's week as day lines: drop a mark to set a temperature from that time.",
  preview: false,
});
