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
    this._selected = null; // {day, mark}
    this._openDay = null; // the day shown as a list, or null
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
    this._openDay = null;
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
    const user = this._hass.user || {};
    if (user.is_admin) return true;
    const me = [String(user.id || "").toLowerCase(), String(user.name || "").trim().toLowerCase()];
    return this._owners().some((o) => me.includes(o.trim().toLowerCase()));
  }

  /* One or more owners; `owner` is the beta.1/2 attribute, kept for an
   * integration not yet updated alongside the card. */
  _owners() {
    const a = this._attrs();
    const raw = a.owners ?? a.owner ?? [];
    return (Array.isArray(raw) ? raw : [raw]).filter((o) => o && String(o).trim());
  }

  /* "an administrator", "Erin or an administrator", "Paul, Anna or an administrator" */
  _whoMayEdit() {
    const names = [...this._owners().map(esc), "an administrator"];
    return names.length === 1 ? names[0] : `${names.slice(0, -1).join(", ")} or ${names[names.length - 1]}`;
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

  /* A finger rather than a mouse: phones and most tablets. */
  _touchFirst() {
    return typeof matchMedia === "function" && matchMedia("(hover: none)").matches;
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
    while (!this._free(day, at) && at > 0) at -= SNAP;
    if (!this._free(day, at)) return null;
    const mark = { minute: at, temperature: clamp(this._valueAt(day, at), lo, hi) };
    this._week[day].push(mark);
    this._touch(day);
    this._selected = { day, mark };
    return mark;
  }

  /* The list's "Add mark": an hour after the last one, or 07:00 on an empty day. */
  _addToDay(day) {
    const marks = this._week[day];
    const after = marks.length ? Math.min(marks[marks.length - 1].minute + 60, DAY - SNAP) : 7 * 60;
    return this._addMark(day, after);
  }

  _setTemp(day, mark, value) {
    const { lo, hi } = this._limits();
    mark.temperature = clamp(Math.round(value / STEP) * STEP, lo, hi);
    this._touch(day);
  }

  /* Move a mark to a time; refuses a time another mark on that day holds. */
  _setTime(day, mark, minute) {
    const at = clamp(Math.round(minute / SNAP) * SNAP, 0, DAY - SNAP);
    if (at === mark.minute) return true;
    if (!this._free(day, at, mark)) {
      this._error = `${DAY_LONG[day]} already has a mark at ${hhmm(at)}.`;
      return false;
    }
    mark.minute = at;
    this._touch(day);
    return true;
  }

  _removeMark(day, mark) {
    this._week[day] = this._week[day].filter((m) => m !== mark);
    this._touch(day);
    if (this._selected && this._selected.mark === mark) this._selected = null;
  }

  _copyDay(from, targets) {
    for (const d of targets) {
      if (d === from) continue;
      this._week[d] = this._week[from].map((m) => ({ ...m }));
      this._touch(d);
    }
  }

  _discard() {
    this._dirty.clear();
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

  /**
   * Something set by hand that "Follow schedule" would clear. Not
   * "away (room)" on its own: a room's presence schedule reading off produces
   * that too, and there is nothing to clear for it.
   */
  _offSchedule(a) {
    return a.target_source === "override" || !!a.room_away;
  }

  _resumeTitle(a, houseAway) {
    if (this._offSchedule(a)) return "Drop the thermostat setting and room away; back to the schedule now";
    if (houseAway) return "The whole house is away; turn the away switch off to resume";
    if (a.target_source === "away (room)") return "Away because its presence schedule is off";
    return "Already following its schedule";
  }

  async _resumeSchedule() {
    if (this._resuming) return;
    const a = this._attrs();
    this._resuming = true;
    try {
      await this._hass.callService("multisplit", "resume_schedule", { room: a.room });
      this._error = null;
    } catch (err) {
      this._error = (err && err.message) || String(err);
    }
    this._resuming = false;
    this._render();
  }

  _openDayList(day, mark = null) {
    this._openDay = day;
    this._selected = mark ? { day, mark } : null;
  }

  // -- pointer -----------------------------------------------------------

  _minuteFromEvent(track, ev) {
    const r = track.getBoundingClientRect();
    return ((ev.clientX - r.left) / r.width) * DAY;
  }

  /* A press on a day line becomes one of four things, decided when it ends:
   *   - a drag, if it started on a mark and moved sideways;
   *   - nothing, if it moved -- the page was being scrolled;
   *   - with a mouse on an empty spot, a new mark there;
   *   - otherwise, that day opened as a list (with the mark picked, if any).
   * Nothing happens on the way down, so a finger scrolling the dashboard
   * never leaves a mark behind. */
  _onTrackDown(ev, day) {
    if (ev.button > 0) return;
    const track = ev.currentTarget;
    const flag = ev.target.closest(".flag");
    const mark = flag ? this._week[day][Number(flag.dataset.index)] : null;
    const editable = this._canEdit();
    const press = { x: ev.clientX, y: ev.clientY, dragging: false };
    if (mark && editable && ev.pointerType === "mouse") ev.preventDefault();

    const move = (e) => {
      const dx = e.clientX - press.x;
      const dy = e.clientY - press.y;
      if (!press.dragging) {
        if (!(mark && editable) || Math.abs(dx) < 6 || Math.abs(dx) < Math.abs(dy)) return;
        press.dragging = true;
        this._drag = press;
        this._openDayList(day, mark);
      }
      this._setTime(day, mark, this._minuteFromEvent(track, e));
      this._error = null;
      this._renderRow(day);
    };
    const end = (e) => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", end);
      window.removeEventListener("pointercancel", end);
      const dragged = press.dragging;
      this._drag = null;
      if (dragged) return this._render(".flag.selected");
      if (e.type === "pointercancel") return; // the browser took it for a scroll
      if (Math.hypot(e.clientX - press.x, e.clientY - press.y) > 8) return;
      if (mark) this._openDayList(day, mark);
      else if (editable && ev.pointerType === "mouse") {
        const added = this._addMark(day, this._minuteFromEvent(track, e));
        this._openDayList(day, added);
      } else this._openDayList(day);
      this._render();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
  }

  _onFlagKey(ev, day, index) {
    const mark = this._week[day][index];
    if (ev.key === "Enter" || ev.key === " ") {
      ev.preventDefault();
      this._openDayList(day, mark);
      return this._render(".panel li.selected .time");
    }
    if (!this._canEdit()) return;
    this._selected = { day, mark };
    const keys = {
      ArrowLeft: () => this._setTime(day, mark, mark.minute - SNAP),
      ArrowRight: () => this._setTime(day, mark, mark.minute + SNAP),
      ArrowUp: () => this._setTemp(day, mark, mark.temperature + STEP),
      ArrowDown: () => this._setTemp(day, mark, mark.temperature - STEP),
      Delete: () => this._removeMark(day, mark),
      Backspace: () => this._removeMark(day, mark),
    };
    if (!keys[ev.key]) return;
    ev.preventDefault();
    keys[ev.key]();
    this._render(".flag.selected");
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
          aria-label="${DAY_LONG[day]} ${hhmm(m.minute)}, ${fmt(m.temperature)} degrees">
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
    const label = this.shadowRoot.querySelector(`.day[data-day="${day}"]`);
    if (label) label.classList.toggle("changed", this._dirty.has(day));
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

  /* The open day as a list: the way to edit on a phone, and a precise one
   * anywhere. Times are typed (or picked), temperatures stepped. */
  _panel() {
    const d = this._openDay;
    if (d === null) return "";
    const { lo, hi } = this._limits();
    const editable = this._canEdit();
    const marks = this._week[d];
    const sel = this._selected;
    const carried = carriedInto(this._week, d);
    const fromMidnight =
      carried !== null && (!marks.length || marks[0].minute > 0)
        ? `<p class="carried">From midnight ${fmt(carried)}°, carried over from the day before.</p>`
        : "";
    const rows = marks
      .map((m, i) => {
        const on = sel && sel.mark === m ? " selected" : "";
        if (!editable)
          return `<li class="mark${on}" data-index="${i}"><span class="at">${hhmm(m.minute)}</span>
            <span class="value">${fmt(m.temperature)}°</span></li>`;
        return `<li class="mark${on}" data-index="${i}">
          <input type="time" class="time" step="900" value="${hhmm(m.minute)}" aria-label="Time of the ${hhmm(m.minute)} mark">
          <div class="temp">
            <button class="down" aria-label="Cooler" ${m.temperature <= lo ? "disabled" : ""}>−</button>
            <span class="value">${fmt(m.temperature)}°</span>
            <button class="up" aria-label="Warmer" ${m.temperature >= hi ? "disabled" : ""}>+</button>
          </div>
          <button class="remove" aria-label="Remove the ${hhmm(m.minute)} mark" title="Remove">×</button>
        </li>`;
      })
      .join("");
    const none = marks.length
      ? ""
      : `<p class="carried">${
          editable ? "No marks on this day. Add one to change the temperature from a set time." : "No marks on this day."
        }</p>`;
    return `<div class="editor panel" role="region" aria-label="${DAY_LONG[d]}">
      <div class="panel-head">
        <button class="nav prev" aria-label="Previous day">‹</button>
        <h3>${DAY_LONG[d]}</h3>
        <button class="nav next" aria-label="Next day">›</button>
        <button class="done">Done</button>
      </div>
      ${fromMidnight}
      ${rows ? `<ul class="marks">${rows}</ul>` : ""}
      ${none}
      ${
        editable
          ? `<div class="panel-foot">
              <button class="add">Add mark</button>
              ${
                marks.length
                  ? `<span class="copy">Copy ${DAY_LONG[d]} to
                      <button data-copy="weekdays">weekdays</button>
                      <button data-copy="weekend">weekend</button>
                      <button data-copy="all">every day</button></span>`
                  : ""
              }
            </div>`
          : `<p class="carried">Only ${this._whoMayEdit()} can change this schedule.</p>`
      }
    </div>`;
  }

  _footer() {
    const { lo, hi, block, fallback } = this._limits();
    const editable = this._canEdit();
    const found = problems(this._week, lo, hi, block);
    const empty = this._week.every((d) => !d.length);
    const how = this._touchFirst()
      ? "Tap a day to see and change its marks."
      : "Click a line to add a mark and drag a mark to move it, or click a day's name to edit it as a list.";
    const hint = editable
      ? `${empty ? `No schedule yet, so this room follows the default ${fmt(fallback)}°. ` : ""}${how}`
      : `Only ${this._whoMayEdit()} can change this schedule. Tap a day to see its marks.`;
    const messages = [
      ...found.map((p) => `<li>${p}</li>`),
      this._error ? `<li>${esc(this._error)}</li>` : "",
    ].join("");
    return `<p class="hint">${this._openDay === null ? hint : ""}</p>
      ${messages ? `<ul class="problems">${messages}</ul>` : ""}
      ${
        this._dirty.size && editable
          ? `<div class="actions">
              <button class="discard">Discard changes</button>
              <button class="save" ${found.length || this._saving ? "disabled" : ""}>${this._saving ? "Saving…" : "Save schedule"}</button>
            </div>`
          : ""
      }`;
  }

  _renderFooter() {
    const foot = this.shadowRoot.querySelector(".footer");
    if (!foot) return;
    foot.innerHTML = this._footer();
    this._bindFooter();
  }

  _render(focus) {
    const a = this._attrs();
    const state = this._hass.states[this._room];
    const name = (state && state.attributes.friendly_name) || this._room;
    const { lo, hi, block } = this._limits();
    const editable = this._canEdit();
    const rooms = this._rooms();
    const awayId = this._awaySwitch();
    const away = awayId && this._hass.states[awayId].state === "on";

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
      (_, d) => `<div class="row${this._openDay === d ? " open" : ""}">
        <button class="day${this._dirty.has(d) ? " changed" : ""}" data-day="${d}"
          aria-label="${DAY_LONG[d]}: show as a list" aria-expanded="${this._openDay === d}">${DAY_SHORT[d]}</button>
        <div class="track${editable ? " editable" : ""}" data-day="${d}">${this._rowContent(d)}</div>
      </div>`
    ).join("");

    this.shadowRoot.innerHTML = `${STYLE}<ha-card>
      ${tabs}
      <div class="head">
        <div>
          <h2>${esc(name)}</h2>
          <p class="status">${state ? this._status(a) : "Unavailable"}</p>
          <p class="rails">Allowed ${fmt(lo)}–${fmt(hi)}°. Each setting at least ${block} min.${
            this._owners().length
              ? ` ${this._owners().length > 1 ? "Owners" : "Owner"}: ${this._owners().map(esc).join(", ")}.`
              : ""
          }</p>
        </div>
        ${
          state
            ? `<button class="resume" ${this._offSchedule(a) ? "" : "disabled"}
                title="${this._resumeTitle(a, away)}">Follow schedule</button>`
            : ""
        }
      </div>
      ${away ? `<p class="banner">The house is away. Every room holds ${fmt(a.away_target)}° until the away switch is turned off; the schedule resumes then.</p>` : ""}
      <div class="grid">
        <div class="row axis"><span></span><div class="hours">${hours}</div></div>
        ${rows}
      </div>
      ${this._panel()}
      <div class="footer">${this._footer()}</div>
    </ha-card>`;
    this._bind();
    if (focus) {
      const el = this.shadowRoot.querySelector(focus);
      if (el) el.focus({ preventScroll: true });
    }
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
    root.querySelectorAll(".day").forEach((b) =>
      b.addEventListener("click", () => {
        const d = Number(b.dataset.day);
        if (this._openDay === d) this._openDay = null;
        else this._openDayList(d);
        this._render(`.day[data-day="${d}"]`);
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
    on(root, ".resume", () => this._resumeSchedule());
    this._bindPanel();
    this._bindFooter();
  }

  _bindPanel() {
    const root = this.shadowRoot;
    const d = this._openDay;
    if (d === null) return;
    const step = (by) => () => {
      this._openDayList((d + by + 7) % 7);
      this._render(by < 0 ? ".panel .prev" : ".panel .next");
    };
    on(root, ".panel .prev", step(-1));
    on(root, ".panel .next", step(1));
    on(root, ".panel .done", () => {
      this._openDay = null;
      this._selected = null;
      this._render(`.day[data-day="${d}"]`);
    });
    on(root, ".panel .add", () => {
      const mark = this._addToDay(d);
      if (!mark) this._error = `${DAY_LONG[d]} has no free time left for another mark.`;
      this._render(".panel .add");
    });
    root.querySelectorAll(".panel [data-copy]").forEach((b) =>
      b.addEventListener("click", () => {
        const sets = { weekdays: [0, 1, 2, 3, 4], weekend: [5, 6], all: [0, 1, 2, 3, 4, 5, 6] };
        this._copyDay(d, sets[b.dataset.copy]);
        this._render(`.panel [data-copy="${b.dataset.copy}"]`);
      })
    );
    root.querySelectorAll(".panel li.mark").forEach((li) => {
      const mark = this._week[d][Number(li.dataset.index)];
      li._mark = mark;
      const where = () => `.panel li[data-index="${this._week[d].indexOf(mark)}"]`;
      li.querySelector(".up")?.addEventListener("click", () => {
        this._selected = { day: d, mark };
        this._setTemp(d, mark, mark.temperature + STEP);
        this._render(`${where()} .up`);
      });
      li.querySelector(".down")?.addEventListener("click", () => {
        this._selected = { day: d, mark };
        this._setTemp(d, mark, mark.temperature - STEP);
        this._render(`${where()} .down`);
      });
      li.querySelector(".remove")?.addEventListener("click", () => {
        this._removeMark(d, mark);
        this._render(".panel .add");
      });
      const time = li.querySelector(".time");
      if (!time) return;
      // Applied as it changes so the line and the problems keep up, but the
      // list is only redrawn (and re-sorted) when the field is left: a
      // desktop browser reports each half of 06:30 as it is typed.
      time.addEventListener("change", () => {
        if (!time.value) return;
        this._selected = { day: d, mark };
        if (!this._setTime(d, mark, toMinute(time.value))) return this._renderFooter();
        this._renderRow(d);
        this._renderFooter();
      });
      time.addEventListener("blur", () => {
        if (this._drag) return;
        // Redraw to re-sort the list, keeping focus wherever it went next.
        requestAnimationFrame(() => {
          const active = this.shadowRoot.activeElement;
          if (active === time) return;
          this._render(this._selectorFor(active, d));
        });
      });
    });
  }

  /* A selector for a focused element that survives a redraw: rows are found
   * by their mark, which may have moved when the list was re-sorted. */
  _selectorFor(el, day) {
    if (!el || !el.closest || !el.closest(".panel")) return null;
    const li = el.closest("li.mark");
    const cls = [...el.classList][0];
    if (!cls) return null;
    if (!li) return `.panel .${cls}`;
    const mark = li._mark;
    const index = mark ? this._week[day].indexOf(mark) : -1;
    return index < 0 ? null : `.panel li[data-index="${index}"] .${cls}`;
  }

  _bindFooter() {
    const root = this.shadowRoot;
    on(root, ".discard", () => this._discard());
    on(root, ".save", () => this._save());
  }
}

function on(root, selector, fn) {
  const el = root.querySelector(selector);
  if (el) el.addEventListener("click", fn);
}

const STYLE = `<style>
  :host { display: block; -webkit-tap-highlight-color: transparent; }
  ha-card { padding: 16px; overflow: hidden; container-type: inline-size; }
  .empty { color: var(--secondary-text-color); }
  .rooms { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 14px; }
  .room {
    font: inherit; font-size: 0.9em; padding: 6px 12px; border-radius: 16px; cursor: pointer;
    border: 1px solid var(--divider-color); background: none; color: var(--primary-text-color);
  }
  .room.on { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
  .head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
  h2 { margin: 0; font-size: 1.35em; font-weight: 500; letter-spacing: -0.01em; }
  .status { margin: 4px 0 0; color: var(--primary-text-color); }
  .rails { margin: 2px 0 0; color: var(--secondary-text-color); font-size: 0.88em; }
  .resume {
    font: inherit; font-size: 0.9em; padding: 8px 12px; border-radius: 8px; cursor: pointer; white-space: nowrap;
    border: 1px solid var(--primary-color); background: none; color: var(--primary-color);
  }
  .resume:disabled { border-color: var(--divider-color); color: var(--secondary-text-color); cursor: default; opacity: 0.6; }
  .banner {
    margin: 12px 0 0; padding: 8px 12px; border-radius: 8px; font-size: 0.92em;
    background: color-mix(in srgb, rgb(74, 111, 165) 14%, transparent); color: var(--primary-text-color);
  }
  .grid { margin-top: 14px; }
  .row { display: grid; grid-template-columns: 44px 1fr; align-items: end; gap: 6px; }
  .row + .row { margin-top: 2px; }
  .day {
    font: inherit; font-size: 0.9em; text-align: left; color: var(--secondary-text-color);
    background: none; border: 0; border-radius: 6px; padding: 6px 4px; cursor: pointer; min-height: 32px;
  }
  .row.open .day { color: var(--primary-color); font-weight: 600; }
  .day.changed { color: var(--primary-text-color); font-weight: 600; }
  .day.changed::after { content: "•"; color: var(--primary-color); margin-left: 2px; }
  .hours { position: relative; height: 14px; font-size: 0.72em; color: var(--secondary-text-color); }
  .hours span { position: absolute; transform: translateX(-50%); }
  .hours span:first-child { transform: none; }
  .hours span:last-child { transform: translateX(-100%); }
  .track { position: relative; height: 50px; touch-action: pan-y; user-select: none; -webkit-user-select: none; cursor: pointer; }
  @media (hover: hover) { .track.editable { cursor: copy; } }
  .seg { position: absolute; bottom: 0; height: 12px; pointer-events: none; }
  .seg.first { border-top-left-radius: 6px; border-bottom-left-radius: 6px; }
  .seg.last { border-top-right-radius: 6px; border-bottom-right-radius: 6px; }
  .seg.unset { opacity: 0.35; }
  .now { position: absolute; bottom: -3px; height: 18px; width: 2px; margin-left: -1px; background: var(--primary-text-color); border-radius: 1px; pointer-events: none; }
  /* A finger-wide hit area around a 2px pin. */
  .flag {
    position: absolute; bottom: 0; transform: translateX(-14px); padding: 0; border: 0; background: none;
    font: inherit; color: var(--primary-text-color); cursor: pointer; height: 50px; width: 28px;
  }
  .track.editable .flag { cursor: grab; }
  .flag .pin { position: absolute; left: 13px; bottom: 0; width: 2px; height: 40px; border-radius: 1px; }
  .flag .tag {
    position: absolute; left: 14px; top: 0; padding: 0 5px; line-height: 16px; font-size: 0.78em; white-space: nowrap;
    border-radius: 0 6px 6px 0; background: var(--card-background-color); border: 1px solid var(--divider-color); border-left: 0;
  }
  .flag.low .pin { height: 22px; }
  .flag.low .tag { top: 17px; }
  .flag.selected .tag, .flag:focus-visible .tag { border-color: var(--primary-color); color: var(--primary-color); font-weight: 600; }
  .flag:focus { outline: none; }
  .flag:focus-visible .pin { box-shadow: 0 0 0 2px var(--primary-color); }
  .editor {
    margin-top: 12px; padding: 4px 12px 12px; border-radius: 10px; background: var(--secondary-background-color);
  }
  .panel-head { display: flex; align-items: center; gap: 4px; min-height: 48px; }
  .panel-head h3 { margin: 0 4px; font-size: 1.05em; font-weight: 600; flex: 1; }
  .panel-head h3 { order: 1; }
  .nav {
    order: 0; font: inherit; font-size: 1.4em; line-height: 1; width: 40px; height: 40px; border-radius: 50%;
    border: 0; background: none; color: var(--primary-text-color); cursor: pointer;
  }
  .nav.next { order: 2; }
  .done {
    order: 3; font: inherit; font-weight: 500; padding: 8px 14px; border-radius: 8px; border: 0; cursor: pointer;
    background: var(--card-background-color); color: var(--primary-color);
  }
  .carried { margin: 4px 0 6px; color: var(--secondary-text-color); font-size: 0.9em; }
  .marks { list-style: none; margin: 0; padding: 0; }
  .mark {
    display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 12px;
    min-height: 52px; border-bottom: 1px solid var(--divider-color);
  }
  .mark:last-child { border-bottom: 0; }
  .mark.selected { box-shadow: inset 3px 0 0 var(--primary-color); padding-left: 8px; }
  .mark .at { font-variant-numeric: tabular-nums; }
  /* 16px stops iOS zooming the page when the field is focused. */
  .time {
    font: inherit; font-size: 16px; color: inherit; background: var(--card-background-color);
    border: 1px solid var(--divider-color); border-radius: 8px; padding: 8px 10px; min-height: 40px;
  }
  .temp { display: inline-flex; align-items: center; gap: 4px; justify-self: center; min-width: 0; }
  .value { min-width: 3.4em; text-align: center; font-weight: 600; font-variant-numeric: tabular-nums; }
  .temp button {
    font: inherit; font-size: 1.15em; width: 40px; height: 40px; border-radius: 50%; cursor: pointer;
    border: 1px solid var(--divider-color); background: var(--card-background-color); color: inherit;
  }
  .temp button:disabled { opacity: 0.4; cursor: default; }
  .remove {
    font: inherit; font-size: 1.4em; line-height: 1; width: 40px; height: 40px; border-radius: 50%; border: 0;
    background: none; cursor: pointer; color: var(--error-color, #db4437);
  }
  .copy button, .discard, .add {
    font: inherit; background: none; border: 0; padding: 8px 4px; cursor: pointer; color: var(--primary-color); min-height: 40px;
  }
  .panel-foot { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 16px; margin-top: 6px; }
  .add { font-weight: 600; }
  .copy { color: var(--secondary-text-color); font-size: 0.92em; }
  .copy button { margin-left: 4px; }
  .hint { margin: 12px 0 0; color: var(--secondary-text-color); font-size: 0.88em; }
  .hint:empty { display: none; }
  .problems { margin: 8px 0 0; padding-left: 18px; color: var(--error-color, #db4437); font-size: 0.9em; }
  .actions { display: flex; justify-content: flex-end; align-items: center; gap: 16px; margin-top: 12px; }
  .save {
    font: inherit; padding: 10px 18px; border-radius: 8px; border: 0; cursor: pointer;
    background: var(--primary-color); color: var(--text-primary-color, #fff);
  }
  .save:disabled { opacity: 0.45; cursor: default; }
  button:focus-visible, .time:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
  /* Narrow: the lines become an overview. Labels would collide, so only the
   * picked mark keeps one, and the day opens as a list to edit. */
  @container (max-width: 480px) {
    .row { grid-template-columns: 38px 1fr; }
    .track { height: 34px; }
    .flag { height: 34px; }
    .flag .pin, .flag.low .pin { height: 24px; }
    .flag:not(.selected) .tag { display: none; }
    .flag .tag, .flag.low .tag { top: 0; }
    .copy { flex-basis: 100%; }
    .mark { gap: 8px; }
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
