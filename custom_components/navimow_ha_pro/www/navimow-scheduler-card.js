/*
 * Navimow HA Pro — graphical mowing-schedule card.
 *
 * A weekly, app-like editor for the Segway Navimow mowing plan:
 *   - one section per weekday, with an on/off toggle;
 *   - one or more time periods per day (multiple mowing sessions);
 *   - per-period zone selection (no zones selected = whole map / all zones);
 *   - a per-day "Save" that writes only that day via navimow_ha_pro.set_schedule,
 *     and a per-day "Discard" that reverts unsaved edits for that day.
 *
 * Zero external dependencies (vanilla custom element) so it is robust across
 * Home Assistant frontend versions. It reads everything from ONE entity, the
 * schedule sensor (attributes: `days` = parsed plan, `zones` = available zones),
 * and writes with the integration's set_schedule service.
 *
 * The card is auto-registered by the integration (add_extra_js_url), so no
 * manual Lovelace resource step is normally needed.
 *
 * End-of-day convention: the mower's last slot (96) is 24:00. It round-trips
 * from the backend as "00:00"; this card treats an END time of "00:00" as
 * end-of-day (1440 min), and the set_schedule service applies the same rule, so
 * a "mow until midnight" window stays editable and savable.
 */

const STRINGS = {
  en: {
    title: "Mowing schedule",
    add: "Add period",
    save: "Save",
    discard: "Discard",
    saved: "Saved",
    saving: "Saving...",
    error: "Save failed",
    allZones: "All zones",
    off: "Off",
    remove: "Remove period",
    noSensor: "Schedule sensor not found.",
    invalid: "End must be after start.",
    incomplete: "Fill in both times.",
    slot: "period",
    slots: "periods",
    dash: "&#8594;",
  },
  it: {
    title: "Piano di taglio",
    add: "Aggiungi fascia",
    save: "Salva",
    discard: "Annulla",
    saved: "Salvato",
    saving: "Salvataggio...",
    error: "Salvataggio non riuscito",
    allZones: "Tutte le zone",
    off: "Off",
    remove: "Rimuovi fascia",
    noSensor: "Sensore schedule non trovato.",
    invalid: "La fine deve essere dopo l'inizio.",
    incomplete: "Compila entrambi gli orari.",
    slot: "fascia",
    slots: "fasce",
    dash: "&#8594;",
  },
};

// Display order Monday-first; `num` is the Navimow weekday number (1=Sun..7=Sat),
// `key` is the weekday name the set_schedule service expects.
const DAYS = [
  { num: 2, key: "monday" },
  { num: 3, key: "tuesday" },
  { num: 4, key: "wednesday" },
  { num: 5, key: "thursday" },
  { num: 6, key: "friday" },
  { num: 7, key: "saturday" },
  { num: 1, key: "sunday" },
];

const DAY_LABELS = {
  en: {
    monday: "Monday", tuesday: "Tuesday", wednesday: "Wednesday",
    thursday: "Thursday", friday: "Friday", saturday: "Saturday", sunday: "Sunday",
  },
  it: {
    monday: "Luned&igrave;", tuesday: "Marted&igrave;", wednesday: "Mercoled&igrave;",
    thursday: "Gioved&igrave;", friday: "Venerd&igrave;", saturday: "Sabato", sunday: "Domenica",
  },
};

class NavimowSchedulerCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._config = null;
    this._draft = null;       // [{num,key,enabled,periods:[{start,end,zones:[id]}],_dirty,_saving}]
    this._serverDays = [];    // last-seen `days` attribute (for per-day discard/merge)
    this._zones = [];         // [{id,name}]
    this._sig = null;         // last-seen schedule signature (to detect real changes)
    this._status = {};        // dayKey -> {kind:'saving'|'saved'|'error', text}
    this._clearTimers = {};   // dayKey -> timeout id (auto-clear a 'saved' badge)
    this._rendered = false;
  }

  // ---- Lovelace config -----------------------------------------------------
  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error("navimow-scheduler-card: `entity` (a *_schedule sensor) is required");
    }
    this._config = { title: null, ...config };
    this._rendered = false;
    this._sig = null;
    this._draft = null;
    this._status = {};
  }

  static getStubConfig(hass) {
    const match = Object.keys(hass.states || {}).find(
      (e) =>
        e.startsWith("sensor.") &&
        e.endsWith("_schedule") &&
        (hass.entities?.[e]?.platform === "navimow_ha_pro")
    );
    return { entity: match || "sensor.navimow_schedule" };
  }

  getCardSize() {
    return 3 + DAYS.length;
  }

  // ---- hass updates --------------------------------------------------------
  set hass(hass) {
    this._hass = hass;
    if (!this._config) return;

    const st = hass.states[this._config.entity];
    if (!st) {
      this._renderMessage(this._t().noSensor);
      return;
    }

    const days = Array.isArray(st.attributes.days) ? st.attributes.days : [];
    const zones = Array.isArray(st.attributes.zones) ? st.attributes.zones : [];
    const sig = JSON.stringify([days, zones]);

    if (!this._rendered) {
      this._sig = sig;
      this._serverDays = days;
      this._zones = zones;
      this._draft = this._buildDraft(days);
      this._render();
      return;
    }

    if (sig === this._sig) return; // nothing relevant changed on the server

    // A genuine server-side plan change arrived. Never rebuild while the user
    // is focused inside the card — that would steal focus or discard a
    // half-entered value. Leave _sig unchanged so a later (unfocused) poll
    // still applies the change.
    if (this.shadowRoot.activeElement) return;

    this._sig = sig;
    this._serverDays = days;
    this._zones = zones;
    // Merge: refresh days with NO unsaved edits from the server; keep days that
    // the user is still editing. So one dirty day no longer freezes the rest.
    // Carry the accordion expand state so a poll never collapses an open day.
    this._draft = this._draft.map((d, i) =>
      d._dirty || d._saving ? d : { ...this._buildDayDraft(days, i), _expanded: d._expanded }
    );
    this._render();
  }

  get hass() {
    return this._hass;
  }

  // ---- helpers -------------------------------------------------------------
  _lang() {
    const l = (this._hass?.language || "en").toLowerCase();
    return l.startsWith("it") ? "it" : "en";
  }
  _t() {
    return STRINGS[this._lang()];
  }
  _dayLabel(key) {
    return DAY_LABELS[this._lang()][key] || key;
  }

  _buildDraft(days) {
    return DAYS.map((_, i) => this._buildDayDraft(days, i));
  }

  _buildDayDraft(days, i) {
    const def = DAYS[i];
    const src = (days || []).find((d) => d && d.day === def.num);
    const periods = [];
    if (src && Array.isArray(src.periods)) {
      for (const p of src.periods) {
        const start = p.start_hhmm || this._minToHHMM(p.start_min);
        const end = p.end_hhmm || this._minToHHMM(p.end_min);
        if (!start || !end) continue;
        periods.push({
          start,
          end,
          zones: Array.isArray(p.zone_ids) ? p.zone_ids.slice() : [],
        });
      }
    }
    return {
      num: def.num,
      key: def.key,
      enabled: !!(src && src.enabled),
      periods,
      _dirty: false,
      _saving: false,
      _rev: 0,
      _expanded: false,
    };
  }

  _minToHHMM(min) {
    if (typeof min !== "number" || isNaN(min)) return null;
    const h = Math.floor(min / 60) % 24;
    const m = min % 60;
    return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
  }

  // Snap "HH:MM" to the nearest 15-minute slot (the mower's resolution).
  // A value rounding up to 1440 (24:00) becomes "00:00" — read as end-of-day
  // in the END position (see the end-of-day convention above).
  _snap15(hhmm) {
    const [h, m] = String(hhmm).split(":").map((x) => parseInt(x, 10));
    if (isNaN(h) || isNaN(m)) return hhmm;
    let total = Math.round((h * 60 + m) / 15) * 15;
    // Cap at 23:45 (the last valid start/end slot below midnight). End-of-day
    // (24:00) is expressed by the literal "00:00" end value, which _endMin
    // reads as 1440 — so _snap15 never emits a 1440/"00:00" that would be
    // ambiguous with a real midnight start.
    if (total >= 1440) total = 1425;
    return this._minToHHMM(total);
  }

  _hhmmToMin(hhmm) {
    const [h, m] = String(hhmm).split(":").map((x) => parseInt(x, 10));
    return (isNaN(h) ? 0 : h) * 60 + (isNaN(m) ? 0 : m);
  }

  // End minutes, treating "00:00" as end-of-day (1440).
  _endMin(hhmm) {
    const v = this._hhmmToMin(hhmm);
    return v === 0 ? 1440 : v;
  }

  // How an end time is shown in the summary line ("00:00" -> "24:00").
  _dispEnd(hhmm) {
    return this._hhmmToMin(hhmm) === 0 ? "24:00" : hhmm;
  }

  _deviceId() {
    const ent = this._hass?.entities?.[this._config.entity];
    return ent?.device_id || null;
  }

  _cardTitle() {
    if (this._config && Object.prototype.hasOwnProperty.call(this._config, "title")) return this._config.title || "";
    return this._t().title;
  }

  // ---- rendering -----------------------------------------------------------
  _renderMessage(msg) {
    this.shadowRoot.innerHTML = `
      <ha-card header="${this._escape(this._cardTitle())}">
        <div class="pad">${this._escape(msg)}</div>
      </ha-card>
      ${this._styleTag()}
    `;
    this._rendered = false;
  }

  _render() {
    const rows = this._draft.map((day, di) => this._renderDay(day, di)).join("");
    this.shadowRoot.innerHTML = `
      <ha-card header="${this._escape(this._cardTitle())}">
        <div class="days">${rows}</div>
      </ha-card>
      ${this._styleTag()}
    `;
    this._attachEvents();
    this._rendered = true;
  }

  _renderDay(day, di) {
    const t = this._t();
    const s = this._status[day.key];
    const statusText = s ? s.text : "";
    const statusClass = s ? `status ${s.kind}` : "status";
    const periods = day.periods.map((p, pi) => this._renderPeriod(day, di, p, pi)).join("");
    // Collapsed sub-label: period COUNT (or Off) — the times themselves stay
    // hidden until the row is expanded via the chevron.
    const n = day.periods.length;
    const sub = day.enabled ? (n ? `${n} ${n === 1 ? t.slot : t.slots}` : t.off) : t.off;
    const canSave = day._dirty && !day._saving;
    return `
      <div class="day ${day.enabled ? "on" : "off"} ${day._expanded ? "expanded" : ""}" data-di="${di}">
        <div class="day-head">
          <ha-switch data-act="toggle-day" data-di="${di}"></ha-switch>
          <div class="day-name" data-act="toggle-expand" data-di="${di}">
            <div class="day-title">${this._dayLabel(day.key)}</div>
            <div class="day-sub">${this._escape(sub)}</div>
          </div>
          <span class="${statusClass}">${this._escape(statusText)}</span>
          <ha-icon class="chev" data-act="toggle-expand" data-di="${di}" icon="mdi:chevron-down"></ha-icon>
        </div>
        <div class="day-body" ${day._expanded ? "" : "hidden"}>
          <div class="periods">
            ${periods}
            <button class="add" data-act="add-period" data-di="${di}">+ ${this._escape(t.add)}</button>
          </div>
        </div>
        <div class="day-actions" ${day._dirty ? "" : "hidden"}>
          <button class="save" data-act="save-day" data-di="${di}" ${canSave ? "" : "disabled"}>${this._escape(t.save)}</button>
          <button class="discard" data-act="discard-day" data-di="${di}" ${canSave ? "" : "hidden"}>${this._escape(t.discard)}</button>
        </div>
      </div>
    `;
  }

  _renderPeriod(day, di, p, pi) {
    const t = this._t();
    const zoneChips =
      this._zones.length > 0
        ? `<div class="zones">
             <button class="chip ${p.zones.length === 0 ? "active" : ""}"
                     data-act="zone-all" data-di="${di}" data-pi="${pi}">${this._escape(t.allZones)}</button>
             ${this._zones
               .map(
                 (z) =>
                   `<button class="chip ${p.zones.includes(z.id) ? "active" : ""}"
                            data-act="zone" data-di="${di}" data-pi="${pi}" data-zid="${z.id}">${this._escape(
                     z.name || "Zone " + z.id
                   )}</button>`
               )
               .join("")}
           </div>`
        : "";
    return `
      <div class="period" data-di="${di}" data-pi="${pi}">
        <div class="times">
          <input type="time" step="900" value="${this._escape(p.start)}" data-act="start" data-di="${di}" data-pi="${pi}">
          <span class="arrow">${t.dash}</span>
          <input type="time" step="900" value="${this._escape(p.end)}" data-act="end" data-di="${di}" data-pi="${pi}">
          <button class="del" title="${this._escape(t.remove)}" data-act="del-period" data-di="${di}" data-pi="${pi}">&#10005;</button>
        </div>
        ${zoneChips}
      </div>
    `;
  }

  _touch(day) {
    day._dirty = true;
    day._rev = (day._rev || 0) + 1;
    this._clearStatus(day.key);
  }

  _attachEvents() {
    const root = this.shadowRoot;

    root.querySelectorAll("[data-act='toggle-expand']").forEach((el) =>
      el.addEventListener("click", (e) => {
        const d = this._draft[+e.currentTarget.dataset.di];
        d._expanded = !d._expanded;
        this._render();
      })
    );
    root.querySelectorAll("[data-act='toggle-day']").forEach((el) => {
      const dd = this._draft[+el.dataset.di];
      el.checked = dd.enabled; // native ha-switch initial state
      el.addEventListener("change", (e) => {
        dd.enabled = e.target.checked;
        if (dd.enabled) {
          dd._expanded = true; // open the editor when a day is enabled
          if (dd.periods.length === 0) {
            dd.periods.push({ start: "09:00", end: "18:00", zones: [] });
          }
        }
        this._touch(dd);
        this._render();
      });
    });
    root.querySelectorAll("[data-act='add-period']").forEach((el) =>
      el.addEventListener("click", (e) => {
        const d = this._draft[+e.currentTarget.dataset.di];
        d.periods.push({ start: "09:00", end: "18:00", zones: [] });
        this._touch(d);
        this._render();
      })
    );
    root.querySelectorAll("[data-act='del-period']").forEach((el) =>
      el.addEventListener("click", (e) => {
        const d = this._draft[+e.currentTarget.dataset.di];
        d.periods.splice(+e.currentTarget.dataset.pi, 1);
        this._touch(d);
        this._render();
      })
    );
    root.querySelectorAll("[data-act='zone']").forEach((el) =>
      el.addEventListener("click", (e) => {
        const d = this._draft[+e.currentTarget.dataset.di];
        const p = d.periods[+e.currentTarget.dataset.pi];
        const zid = +e.currentTarget.dataset.zid;
        const idx = p.zones.indexOf(zid);
        if (idx >= 0) p.zones.splice(idx, 1);
        else p.zones.push(zid);
        this._touch(d);
        this._render();
      })
    );
    root.querySelectorAll("[data-act='zone-all']").forEach((el) =>
      el.addEventListener("click", (e) => {
        const d = this._draft[+e.currentTarget.dataset.di];
        d.periods[+e.currentTarget.dataset.pi].zones = [];
        this._touch(d);
        this._render();
      })
    );

    // Time inputs: update the draft silently (no re-render) so focus/typing is
    // never interrupted; mark dirty and reveal Save/Discard in place.
    root.querySelectorAll("[data-act='start'],[data-act='end']").forEach((el) =>
      el.addEventListener("change", (e) => {
        const d = this._draft[+e.target.dataset.di];
        const p = d.periods[+e.target.dataset.pi];
        const val = e.target.value ? this._snap15(e.target.value) : "";
        if (e.target.dataset.act === "start") p.start = val;
        else p.end = val;
        e.target.value = val;
        this._touch(d);
        this._markDirtyUI(+e.target.dataset.di);
      })
    );

    root.querySelectorAll("[data-act='save-day']").forEach((el) =>
      el.addEventListener("click", (e) => this._saveDay(+e.currentTarget.dataset.di))
    );
    root.querySelectorAll("[data-act='discard-day']").forEach((el) =>
      el.addEventListener("click", (e) => this._discardDay(+e.currentTarget.dataset.di))
    );
  }

  // Reveal Save/Discard + enable Save without a full re-render (keeps input focus).
  _markDirtyUI(di) {
    const dayEl = this.shadowRoot.querySelector(`.day[data-di='${di}']`);
    if (!dayEl) return;
    const actions = dayEl.querySelector(".day-actions");
    if (actions) actions.removeAttribute("hidden");
    const save = dayEl.querySelector("[data-act='save-day']");
    if (save) save.disabled = false;
    const discard = dayEl.querySelector("[data-act='discard-day']");
    if (discard) discard.removeAttribute("hidden");
    const status = dayEl.querySelector(".status");
    if (status) {
      status.textContent = "";
      status.className = "status";
    }
  }

  _clearStatus(key) {
    if (this._clearTimers[key]) {
      clearTimeout(this._clearTimers[key]);
      delete this._clearTimers[key];
    }
    delete this._status[key];
  }

  _discardDay(di) {
    const key = this._draft[di].key;
    const wasExpanded = this._draft[di]._expanded;
    this._draft[di] = this._buildDayDraft(this._serverDays, di);
    this._draft[di]._expanded = wasExpanded; // keep the row open after reverting
    this._clearStatus(key);
    this._render();
  }

  _setStatus(key, kind, text) {
    this._status[key] = { kind, text };
  }

  async _saveDay(di) {
    const t = this._t();
    const day = this._draft[di];
    if (day._saving) return; // re-entrancy guard: one write in flight at a time

    const periods = [];
    for (const p of day.periods) {
      if (!p.start || !p.end) {
        if (day.enabled) {
          this._setStatus(day.key, "error", t.incomplete);
          this._render();
          return;
        }
        continue; // disabled day: silently skip an incomplete row
      }
      const start = this._snap15(p.start);
      const end = this._snap15(p.end);
      if (this._endMin(end) <= this._hhmmToMin(start)) {
        if (day.enabled) {
          this._setStatus(day.key, "error", t.invalid);
          this._render();
          return;
        }
        continue;
      }
      periods.push({ start, end, zones: p.zones.slice() });
    }

    const data = { day: day.key, enabled: day.enabled, periods };
    const deviceId = this._deviceId();
    if (deviceId) data.device_id = deviceId;

    const rev = day._rev;
    day._saving = true;
    this._setStatus(day.key, "saving", t.saving);
    this._render();
    try {
      await this._hass.callService("navimow_ha_pro", "set_schedule", data);
      day._saving = false;
      if (day._rev === rev) {
        // No edit landed while the write was in flight -> this day is now clean.
        day._dirty = false;
        this._setStatus(day.key, "saved", t.saved);
        this._render();
        this._scheduleStatusClear(day.key);
      } else {
        // The user edited during the save; keep it dirty/savable, no 'saved'.
        this._clearStatus(day.key);
        this._render();
      }
    } catch (err) {
      day._saving = false;
      this._setStatus(day.key, "error", t.error);
      // eslint-disable-next-line no-console
      console.error("navimow-scheduler-card: set_schedule failed", err);
      this._render();
    }
  }

  // Auto-clear a 'saved' badge in place after a few seconds (no re-render, so
  // it never steals focus from another day being edited).
  _scheduleStatusClear(key) {
    if (this._clearTimers[key]) clearTimeout(this._clearTimers[key]);
    this._clearTimers[key] = setTimeout(() => {
      delete this._clearTimers[key];
      if (this._status[key] && this._status[key].kind === "saved") {
        delete this._status[key];
        const dayEl = [...this.shadowRoot.querySelectorAll(".day")].find(
          (el) => this._draft[+el.dataset.di] && this._draft[+el.dataset.di].key === key
        );
        const status = dayEl && dayEl.querySelector(".status");
        if (status) {
          status.textContent = "";
          status.className = "status";
        }
      }
    }, 3000);
  }

  _escape(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  _styleTag() {
    return `<style>
      :host{display:block;--orange:#ff641e;--orange-dark:#e95413;--ink:#15191e;--muted:#7c848c;--line:#e6e8eb;--soft:#f5f6f7;font-family:var(--paper-font-body1_-_font-family,Arial,sans-serif)}
      ha-card{background:#fff!important;border-radius:24px!important;box-shadow:none!important;overflow:hidden;color:var(--ink)}
      .pad{padding:24px;color:var(--muted)}
      .days{padding:clamp(8px,1vw,16px);display:grid;gap:clamp(9px,.9vw,14px)}
      .day{border:1px solid var(--line);border-radius:clamp(16px,1.3vw,22px);padding:clamp(11px,1vw,16px);background:#fff;transition:border-color .15s ease,box-shadow .15s ease}
      .day.on{border-color:#ffd4c1}.day.expanded{box-shadow:0 7px 24px rgba(22,27,32,.07)}
      .day-head{display:grid;grid-template-columns:auto minmax(0,1fr) auto auto;align-items:center;gap:clamp(10px,1vw,16px);min-height:52px}
      .day-name{min-width:0;cursor:pointer}.day-title{font-size:clamp(15px,1.15vw,20px);font-weight:800;color:var(--ink)}.day-sub{margin-top:3px;font-size:clamp(11px,.82vw,14px);color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.day.off .day-title{color:#8a9198}
      ha-switch{--switch-checked-button-color:#fff;--switch-checked-track-color:var(--orange);--switch-unchecked-button-color:#fff;--switch-unchecked-track-color:#cfd3d7;flex:none}
      .chev{cursor:pointer;flex:none;color:#9ca2a8;--mdc-icon-size:28px;transform:rotate(-90deg);transition:transform .16s ease,color .16s}.day.expanded .chev{transform:rotate(0)}.chev:hover{color:var(--orange)}
      .status{font-size:clamp(10px,.75vw,13px);font-weight:750;color:var(--muted);min-height:1em;text-align:right}.status.saved{color:#2e9d58}.status.error{color:#d13d32}.status.saving{color:var(--orange)}
      .day-body[hidden]{display:none}.periods{margin:12px 0 3px clamp(0px,3vw,54px)}
      .period{background:var(--soft);border:1px solid #eceef0;border-radius:18px;padding:clamp(12px,1.1vw,17px);margin-bottom:10px}
      .times{display:grid;grid-template-columns:minmax(120px,1fr) auto minmax(120px,1fr) auto;align-items:center;gap:10px}.times input[type="time"]{width:100%;box-sizing:border-box;font-size:clamp(15px,1.08vw,19px);font-weight:750;padding:11px 13px;border-radius:13px;border:1px solid #dadddf;background:#fff;color:var(--ink);color-scheme:light;outline:none}.times input[type="time"]:focus{border-color:var(--orange);box-shadow:0 0 0 3px rgba(255,100,30,.12)}.arrow{color:var(--orange);font-size:20px;font-weight:900}.del{border:0;background:#fff;color:#9aa0a6;cursor:pointer;font-size:16px;border-radius:50%;width:38px;height:38px;box-shadow:inset 0 0 0 1px #e0e2e4}.del:hover,.del:active{background:#fff0e9;color:var(--orange)}
      .zones{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}.chip{border:1px solid #ffb996;background:#fff;color:#c94a13;border-radius:999px;padding:7px 14px;font-size:clamp(11px,.82vw,14px);font-weight:750;cursor:pointer;transition:background .14s,color .14s,border-color .14s}.chip.active{background:var(--orange);border-color:var(--orange);color:#fff;box-shadow:0 3px 10px rgba(255,100,30,.18)}
      .add{border:1px dashed #ff9f72;background:#fff8f4;color:var(--orange);border-radius:14px;padding:10px 15px;cursor:pointer;font-size:clamp(12px,.88vw,15px);font-weight:800}.add:active{background:#fff0e8}
      .day-actions{margin:10px 0 2px clamp(0px,3vw,54px);display:flex;gap:9px;align-items:center}.day-actions[hidden]{display:none}.save{background:var(--orange);color:#fff;border:0;border-radius:13px;padding:10px 22px;cursor:pointer;font-size:clamp(13px,.92vw,16px);font-weight:850;box-shadow:0 4px 12px rgba(255,100,30,.18)}.save[disabled]{opacity:.42;cursor:default}.discard{background:#fff;color:#666f77;border:1px solid #dadddf;border-radius:13px;padding:10px 18px;cursor:pointer;font-size:clamp(13px,.92vw,16px);font-weight:750}.discard[hidden]{display:none}
      @media(max-width:700px){.days{padding:7px}.day{padding:10px}.day-head{grid-template-columns:auto minmax(0,1fr) auto}.status{display:none}.times{grid-template-columns:1fr auto 1fr auto;gap:6px}.times input[type="time"]{padding:9px 7px;font-size:14px}.periods,.day-actions{margin-left:0}.chip{padding:6px 10px}}
    </style>`;
  }

}

if (!customElements.get("navimow-scheduler-card")) {
  customElements.define("navimow-scheduler-card", NavimowSchedulerCard);
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "navimow-scheduler-card",
  name: "Navimow Scheduler",
  description: "Weekly graphical mowing-schedule editor for the Navimow HA Pro integration.",
  preview: false,
});
