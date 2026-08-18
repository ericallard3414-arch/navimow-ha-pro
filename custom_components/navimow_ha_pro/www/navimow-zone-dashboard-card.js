class NavimowZoneDashboardCard extends HTMLElement {
  setConfig(config) {
    this._explicitConfig = {...config};
    this.config = {
      camera: config.camera || config.entity || null,
      mower: null,
      battery: null,
      status: null,
      progress: null,
      coverage: null,
      week_area: null,
      cutting_height: null,
      work_mode: null,
      schedule: null,
      home_path: "/the-551/The551",
      settings_match: null,
      ...config,
    };
    this._darkMode = typeof config.dark_mode === "boolean" ? config.dark_mode : this._loadDarkMode();
    this._heightUnits = ["auto", "metric", "imperial"].includes(config.height_units)
      ? config.height_units
      : this._loadHeightUnits();
    this._selected = new Set();
    this._lastMapSignature = "";
    this._rendered = false;
    this._settingsBuilt = false;
    this._schedulerBuilt = false;
    this._schedulerLoadPromise = null;
    this._settingsUpdateRaf = 0;
    this._lastImageRefresh = 0;
    this._pendingSettings = new Map();
    this._resumeSeeded = false;
    this._pendingMowZones = [];
    this._commandBusy = false;
    this._operatingState = {paused:false,mowing:false,returning:false,atBase:false,raw:"unknown"};
    this._mowerAnimRaf = 0;
    this._mowerAnim = null;
    this._lastMowerTargetAt = 0;
    this._lastMowerPoseSignature = "";
  }

  _loadDarkMode() {
    try { return localStorage.getItem("navimow-ha-pro-dark-mode") === "true"; }
    catch (_error) { return false; }
  }

  _applyDarkMode() {
    this.querySelector(".shell")?.classList.toggle("dark", !!this._darkMode);
    this.setAttribute("data-theme", this._darkMode ? "dark" : "light");
  }

  _setDarkMode(enabled) {
    this._darkMode = !!enabled;
    try { localStorage.setItem("navimow-ha-pro-dark-mode", String(this._darkMode)); }
    catch (_error) {}
    this._applyDarkMode();
    const btn=this.querySelector('[data-dark-mode]');
    if(btn){
      btn.classList.toggle('on',this._darkMode);
      btn.setAttribute('aria-pressed',this._darkMode?'true':'false');
      const state=btn.closest('.toggleWrap')?.querySelector('.toggleState');
      if(state){state.textContent=this._darkMode?'ON':'OFF';state.classList.toggle('on',this._darkMode);}
    }
  }

  _loadHeightUnits() {
    try {
      const saved = localStorage.getItem("navimow-ha-pro-height-units");
      return ["auto", "metric", "imperial"].includes(saved) ? saved : "auto";
    } catch (_error) { return "auto"; }
  }

  _usesImperialHeight() {
    if (this._heightUnits === "imperial") return true;
    if (this._heightUnits === "metric") return false;
    return this._hass?.config?.unit_system?.length !== "km";
  }

  _heightEntityUsesImperial(entityOrUnit) {
    const unit = typeof entityOrUnit === "string"
      ? entityOrUnit
      : entityOrUnit?.attributes?.unit_of_measurement;
    return /^(in|inch|inches)$/i.test(String(unit || "").trim());
  }

  _heightNativeToMm(value, entityOrUnit) {
    const native = Number(value);
    if (!Number.isFinite(native)) return NaN;
    if (!this._heightEntityUsesImperial(entityOrUnit)) return native;
    // Home Assistant exposes converted height entities at one decimal inch
    // precision (for example the mower's real 65 mm value appears as 2.6 in).
    // Snap the reconstructed millimetres to Navimow's 5 mm height grid.
    return Math.round((native * 25.4) / 5) * 5;
  }

  _heightMmToNative(valueMm, entityOrUnit) {
    const mm = Number(valueMm);
    if (!Number.isFinite(mm)) return NaN;
    return this._heightEntityUsesImperial(entityOrUnit) ? mm / 25.4 : mm;
  }

  _heightFromNative(value, entityOrUnit) {
    const mm = this._heightNativeToMm(value, entityOrUnit);
    // Navimow labels each 5 mm position as a 0.2 in step (25 mm per
    // displayed inch), so 90 mm is shown as 3.6 in in the official app.
    return Number.isFinite(mm) ? (this._usesImperialHeight() ? mm / 25 : mm) : NaN;
  }

  _heightToNative(value, entityOrUnit) {
    const shown = Number(value);
    if (!Number.isFinite(shown)) return NaN;
    // Reverse the official app's nominal inch labels back onto the exact
    // 5 mm mower grid before converting to Home Assistant's native unit.
    const mm = this._usesImperialHeight() ? shown * 25 : shown;
    return this._heightMmToNative(mm, entityOrUnit);
  }

  _heightStepFromNative(value, entityOrUnit) {
    const native = Number(value);
    if (!Number.isFinite(native)) return NaN;
    const mm = this._heightEntityUsesImperial(entityOrUnit)
      ? Math.max(1, Math.round(native * 25.4))
      : native;
    return this._usesImperialHeight() ? mm / 25 : mm;
  }

  _heightUnit() { return this._usesImperialHeight() ? "in" : "mm"; }

  _setHeightUnits(value) {
    if (!["auto", "metric", "imperial"].includes(value)) return;
    this._heightUnits = value;
    try { localStorage.setItem("navimow-ha-pro-height-units", value); }
    catch (_error) {}
    this._settingsBuilt = false;
    this._pendingSettings.delete(this.config.cutting_height);
    this._update();
    if (this.querySelector("#settingsOverlay")?.classList.contains("open")) this._renderSettings();
  }

  set hass(hass) {
    this._hass = hass;
    this._resolveEntitiesFromStates();
    if (!this._rendered) this._build();
    this._update();
    this._resolveEntitiesFromRegistry();
  }

  _isExplicit(key) {
    return Object.prototype.hasOwnProperty.call(this._explicitConfig || {}, key)
      || (key === "camera" && Object.prototype.hasOwnProperty.call(this._explicitConfig || {}, "entity"));
  }

  _entitySearchText(entity) {
    return `${entity?.entity_id || ""} ${entity?.attributes?.friendly_name || ""}`.toLowerCase();
  }

  _pickStateEntity(domain, tests, anchor) {
    const states = Object.values(this._hass?.states || {});
    const candidates = states.filter(entity => entity.entity_id.startsWith(`${domain}.`));
    const anchored = anchor ? candidates.filter(entity => this._entitySearchText(entity).includes(anchor)) : candidates;
    const pool = anchored.length ? anchored : candidates.filter(entity => /navimow/.test(this._entitySearchText(entity)));
    return pool.find(entity => tests.every(test => test.test(this._entitySearchText(entity))))?.entity_id || null;
  }

  _resolveEntitiesFromStates() {
    if (!this._hass?.states) return;
    if (!this.config.camera || !this._hass.states[this.config.camera]) {
      const cameras = Object.values(this._hass.states).filter(entity =>
        entity.entity_id.startsWith("camera.") &&
        (/_live_mowing_map$/.test(entity.entity_id) || (entity.attributes?.map_view && /live mowing map/i.test(entity.attributes?.friendly_name || "")))
      );
      if (!this._isExplicit("camera") && cameras.length) this.config.camera = cameras[0].entity_id;
    }
    if (!this.config.camera) return;
    const anchor = this.config.camera.replace(/^camera\./, "").replace(/_live_mowing_map$/, "").toLowerCase();
    const assign = (key, domain, tests) => {
      if (!this._isExplicit(key)) this.config[key] = this._pickStateEntity(domain, tests, anchor) || this.config[key];
    };
    assign("mower", "lawn_mower", [/navimow/]);
    assign("battery", "sensor", [/batter/]);
    assign("status", "sensor", [/status/]);
    assign("progress", "sensor", [/mowing/, /progress/]);
    assign("coverage", "sensor", [/coverage/]);
    assign("week_area", "sensor", [/area/, /week/]);
    assign("cutting_height", "number", [/cutting/, /height/]);
    assign("work_mode", "select", [/work/, /mode/]);
    assign("schedule", "sensor", [/schedule/]);
    if (!this._isExplicit("settings_match")) this.config.settings_match = anchor;
  }

  _registryText(entry) {
    return `${entry?.entity_id || ""} ${entry?.original_name || ""} ${entry?.unique_id || ""}`.toLowerCase();
  }

  async _resolveEntitiesFromRegistry() {
    if (this._registryResolveStarted || !this._hass?.callWS || !this.config.camera) return;
    this._registryResolveStarted = true;
    try {
      const entries = await this._hass.callWS({type: "config/entity_registry/list"});
      const cameraEntry = entries.find(entry => entry.entity_id === this.config.camera);
      if (!cameraEntry?.device_id) return;
      const related = entries.filter(entry => entry.device_id === cameraEntry.device_id);
      this._deviceEntityIds = new Set(related.map(entry => entry.entity_id));
      const pick = (domain, tests) => related.find(entry =>
        entry.entity_id.startsWith(`${domain}.`) && tests.every(test => test.test(this._registryText(entry)))
      )?.entity_id || null;
      const assign = (key, domain, tests) => {
        if (!this._isExplicit(key)) this.config[key] = pick(domain, tests) || this.config[key];
      };
      assign("mower", "lawn_mower", []);
      assign("battery", "sensor", [/batter/]);
      assign("status", "sensor", [/status/]);
      assign("progress", "sensor", [/mowing/, /progress/]);
      assign("coverage", "sensor", [/coverage/]);
      assign("week_area", "sensor", [/area/, /week/]);
      assign("cutting_height", "number", [/cutting/, /height/]);
      assign("work_mode", "select", [/work/, /mode/]);
      assign("schedule", "sensor", [/schedule/]);
      this._settingsBuilt = false;
      this._update();
    } catch (error) {
      console.debug("Navimow card entity-registry discovery unavailable; using state discovery.", error);
    }
  }

  getCardSize() { return 18; }

  disconnectedCallback() {
    if (this._timer) clearInterval(this._timer);
    if (this._settingsUpdateRaf) cancelAnimationFrame(this._settingsUpdateRaf);
    if (this._mowerAnimRaf) cancelAnimationFrame(this._mowerAnimRaf);
  }

  _build() {
    this._rendered = true;
    this.innerHTML = `
      <ha-card>
        <style>
          :host{display:block;width:100%;height:100dvh;max-height:100dvh;overflow:hidden;box-sizing:border-box;--orange:#ff641e;--ink:#13171c;--muted:#7b838c;--line:#e7e9ec;--soft:#f4f5f6;--orange-dark:#e95413}
          ha-card{overflow:hidden;border-radius:0;background:#fff;color:var(--ink);height:100%;min-height:0;box-shadow:none;box-sizing:border-box}
          .shell{position:relative;height:100%;min-height:0;display:flex;flex-direction:column;background:#fff;overflow:hidden;box-sizing:border-box}
          .top{flex:0 0 auto;display:flex;align-items:center;justify-content:space-between;padding:clamp(14px,2.2vh,34px) clamp(18px,2.2vw,42px) clamp(10px,1.4vh,22px);gap:clamp(10px,1.2vw,20px)}
          .topLeft{display:flex;align-items:center;gap:clamp(10px,1.1vw,18px);min-width:0}.homeBtn{width:clamp(48px,3.4vw,66px);height:clamp(48px,3.4vw,66px);flex:0 0 auto;border:0;border-radius:50%;background:var(--orange);color:#fff;display:flex;align-items:center;justify-content:center;cursor:pointer;font-size:clamp(22px,1.8vw,30px);box-shadow:0 4px 14px rgba(255,100,30,.28)}.homeBtn:active{transform:scale(.96);background:var(--orange-dark)}
          .model{font-size:clamp(22px,2.0vw,34px);font-weight:700;letter-spacing:-.7px}.sub{font-size:clamp(12px,1vw,17px);color:var(--muted);margin-top:4px}
          .topRight{display:flex;align-items:center;gap:clamp(8px,.8vw,14px);min-width:0}.topstats{display:flex;gap:clamp(6px,.7vw,12px);min-width:0}.roundAction{width:clamp(48px,3.4vw,66px);height:clamp(48px,3.4vw,66px);flex:0 0 auto;border:0;border-radius:50%;background:var(--orange);color:#fff;display:flex;align-items:center;justify-content:center;cursor:pointer;box-shadow:0 4px 14px rgba(255,100,30,.28)}.roundAction ha-icon{--mdc-icon-size:clamp(24px,1.9vw,32px)}.roundAction:active{transform:scale(.96);background:var(--orange-dark)}.pill{height:clamp(38px,3.2vh,48px);padding:0 clamp(10px,1vw,17px);border:1px solid var(--line);border-radius:24px;display:flex;align-items:center;gap:7px;font-size:clamp(12px,.95vw,16px);font-weight:650;background:#fff;white-space:nowrap}
          .dot{width:10px;height:10px;border-radius:50%;background:#39b86a;transition:background .18s ease,box-shadow .18s ease}.pill.activeStatus{border-color:#ffd3c0;background:#fff7f3}.pill.activeStatus .dot{background:var(--orange);box-shadow:0 0 0 5px rgba(255,100,30,.12)}.battery{font-variant-numeric:tabular-nums}
          .mapWrap{position:relative;margin:0 clamp(8px,1.1vw,20px);flex:1 1 auto;min-height:0;display:flex;align-items:center;justify-content:center;overflow:hidden;background:linear-gradient(#f7f8f8,#f0f1f2);border-radius:clamp(18px,1.7vw,32px)}
          .mapStage{position:relative;width:100%;height:100%;min-height:0;line-height:0}.mapStage img{width:100%;height:100%;object-fit:contain;display:block;user-select:none;-webkit-user-drag:none}
          .mapExpand{position:absolute;right:clamp(14px,1.3vw,24px);bottom:clamp(14px,1.3vw,24px);z-index:9;width:clamp(44px,3.1vw,58px);height:clamp(44px,3.1vw,58px);border:0;border-radius:50%;background:rgba(255,255,255,.96);color:var(--ink);display:flex;align-items:center;justify-content:center;cursor:pointer;box-shadow:0 6px 22px rgba(0,0,0,.18);backdrop-filter:blur(8px)}.mapExpand ha-icon{--mdc-icon-size:clamp(24px,1.8vw,31px)}.mapExpand:active{transform:scale(.96)}
          .fullscreenBar{display:none;position:absolute;left:50%;bottom:max(18px,env(safe-area-inset-bottom));transform:translateX(-50%);z-index:12;align-items:center;gap:12px;padding:9px 10px 9px 16px;border-radius:999px;background:rgba(18,22,27,.86);color:#fff;box-shadow:0 10px 34px rgba(0,0,0,.28);backdrop-filter:blur(12px);font-size:14px;font-weight:800;white-space:nowrap}.fullscreenDone{border:0;border-radius:999px;background:var(--orange);color:#fff;padding:10px 17px;font:800 13px inherit;cursor:pointer}
          .mapWrap.fullscreen{position:fixed!important;inset:0!important;margin:0!important;width:100vw!important;height:100dvh!important;min-height:100dvh!important;max-height:none!important;z-index:100000!important;border-radius:0!important;background:#f4f5f6!important;overflow:hidden!important}.mapWrap.fullscreen .mapStage{width:100%;height:100%}.mapWrap.fullscreen .mapExpand{right:max(18px,env(safe-area-inset-right));top:max(18px,env(safe-area-inset-top));bottom:auto;background:var(--orange);color:#fff}.mapWrap.fullscreen .liveBadge{right:max(76px,calc(env(safe-area-inset-right) + 76px));top:max(20px,env(safe-area-inset-top))}.mapWrap.fullscreen .mapBadge{left:max(18px,env(safe-area-inset-left));top:max(18px,env(safe-area-inset-top))}.mapWrap.fullscreen .fullscreenBar{display:flex}.mapWrap.fullscreen .delayAlert{bottom:max(78px,calc(env(safe-area-inset-bottom) + 78px))}
          .zoneOverlay{position:absolute;inset:0;width:100%;height:100%;overflow:visible}
          .liveTrailOverlay{fill:none;stroke:#e4e7eb;stroke-width:clamp(6px,.32vw,12px);stroke-linecap:round;stroke-linejoin:round;opacity:.62;pointer-events:none;vector-effect:non-scaling-stroke}
          @media (min-width:2000px){.liveTrailOverlay{stroke-width:16px}}
          @media (min-width:3000px){.liveTrailOverlay{stroke-width:18px}}.smoothMower{pointer-events:none;filter:drop-shadow(0 3px 5px rgba(0,0,0,.26))}.smoothMowerBody{fill:#aeb8c9;stroke:#fff;stroke-width:2}.smoothMowerCore{fill:#202735}.smoothMowerLidar{fill:#111723;stroke:#eef2f7;stroke-width:3}.smoothMowerStatus,.smoothMowerNose{fill:var(--orange)}
          .zone{fill:transparent;stroke:transparent;stroke-width:4;cursor:pointer;transition:fill .16s ease,stroke .16s ease,filter .16s ease;pointer-events:all}
          .zone.selected{fill:rgba(255,100,30,.27);stroke:var(--orange);filter:drop-shadow(0 0 5px rgba(255,100,30,.45));outline:none}.zone:focus{outline:none}.zone:focus-visible{outline:none;stroke:var(--orange)}
          .zone:hover{fill:rgba(255,100,30,.10);stroke:rgba(255,100,30,.55)}
          .zoneLabelGroup{pointer-events:none}.zoneLabelBox{fill:rgba(20,24,28,.62);stroke:rgba(255,255,255,.28);stroke-width:1.2}.zoneNameLabel{pointer-events:none;font:800 23px sans-serif;fill:#fff;text-anchor:middle;letter-spacing:-.2px}.zoneIdLabel{pointer-events:none;font:800 11px sans-serif;fill:rgba(255,255,255,.76);text-anchor:middle;letter-spacing:1.1px}.liveBadge{position:absolute;right:clamp(16px,1.5vw,28px);top:clamp(16px,1.6vh,28px);z-index:6;display:flex;align-items:center;gap:8px;padding:9px 13px;border-radius:999px;background:rgba(20,24,28,.72);color:#fff;font-size:clamp(10px,.78vw,13px);font-weight:800;letter-spacing:.25px;backdrop-filter:blur(8px);box-shadow:0 4px 18px rgba(0,0,0,.16)}.liveDot{width:9px;height:9px;border-radius:50%;background:var(--orange);box-shadow:0 0 0 0 rgba(255,100,30,.65);animation:livePulse 1.5s infinite}.liveBadge.stale .liveDot{background:#9ba1a7;animation:none;box-shadow:none}.liveBadge.stale{opacity:.7}@keyframes livePulse{0%{box-shadow:0 0 0 0 rgba(255,100,30,.65)}70%{box-shadow:0 0 0 8px rgba(255,100,30,0)}100%{box-shadow:0 0 0 0 rgba(255,100,30,0)}}
          .mapBadge{position:absolute;left:26px;top:24px;background:rgba(255,255,255,.94);border:1px solid rgba(0,0,0,.08);border-radius:18px;padding:12px 16px;line-height:1.2;box-shadow:0 4px 18px rgba(0,0,0,.09)}
          .mapBadge strong{display:block;font-size:18px}.mapBadge span{font-size:14px;color:var(--muted)}
          .delayAlert{position:absolute;left:clamp(16px,1.5vw,28px);bottom:clamp(16px,1.6vh,28px);z-index:7;display:none;align-items:center;gap:clamp(10px,1vw,16px);max-width:min(72%,760px);padding:clamp(10px,1.05vh,16px) clamp(14px,1.25vw,22px);border-radius:clamp(16px,1.25vw,22px);color:#fff;background:linear-gradient(100deg,#ff4b19 0%,#ff731d 58%,#ff9a23 100%);box-shadow:0 10px 30px rgba(219,62,18,.34),0 0 0 1px rgba(255,255,255,.18) inset;line-height:1.15;overflow:hidden;isolation:isolate;animation:delayBreath 1.9s ease-in-out infinite}
          .delayAlert.show{display:flex}.delayAlert:after{content:"";position:absolute;inset:-35% auto -35% -25%;width:26%;background:linear-gradient(90deg,transparent,rgba(255,255,255,.28),transparent);transform:skewX(-18deg);animation:delayShine 2.7s ease-in-out infinite;z-index:-1}.delayIcon{width:clamp(38px,3.1vw,54px);height:clamp(38px,3.1vw,54px);flex:0 0 auto;border-radius:50%;background:rgba(255,255,255,.2);display:flex;align-items:center;justify-content:center;box-shadow:0 0 0 1px rgba(255,255,255,.28) inset}.delayIcon ha-icon{--mdc-icon-size:clamp(23px,1.8vw,31px)}.delayCopy{min-width:0}.delayTitle{font-size:clamp(13px,1.02vw,18px);font-weight:900;letter-spacing:.55px;text-transform:uppercase}.delayText{font-size:clamp(11px,.9vw,16px);font-weight:650;margin-top:3px;opacity:.96;white-space:normal}.delayPulse{width:9px;height:9px;margin-left:auto;flex:0 0 auto;border-radius:50%;background:#fff;box-shadow:0 0 0 0 rgba(255,255,255,.72);animation:delayDot 1.35s infinite}
          @keyframes delayBreath{0%,100%{transform:translateZ(0) scale(1);filter:saturate(1)}50%{transform:translateZ(0) scale(1.008);filter:saturate(1.12)}}@keyframes delayDot{0%{box-shadow:0 0 0 0 rgba(255,255,255,.7)}70%{box-shadow:0 0 0 10px rgba(255,255,255,0)}100%{box-shadow:0 0 0 0 rgba(255,255,255,0)}}@keyframes delayShine{0%,38%{left:-30%}68%,100%{left:125%}}
          .panel{flex:0 0 auto;background:#fff;border-radius:clamp(20px,1.8vw,34px) clamp(20px,1.8vw,34px) 0 0;margin-top:-2px;position:relative;z-index:2;padding:clamp(12px,1.5vh,28px) clamp(16px,2vw,38px) clamp(12px,1.6vh,30px);border-top:1px solid var(--line);box-sizing:border-box}
          .statusline{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:clamp(10px,1.3vh,24px)}.state{font-size:clamp(19px,1.6vw,27px);font-weight:720}.state small{font-size:clamp(11px,.95vw,16px);font-weight:500;color:var(--muted);display:block;margin-top:3px}
          .metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:clamp(6px,.7vw,12px);margin-bottom:clamp(7px,.9vh,14px)}.metric{padding:clamp(9px,1vh,17px) clamp(10px,1vw,18px);background:var(--soft);border-radius:clamp(12px,1.1vw,20px);min-width:0}.metric .v{font-size:clamp(18px,1.6vw,27px);font-weight:740;white-space:nowrap}.metric .k{font-size:clamp(9px,.78vw,13px);color:var(--muted);margin-top:2px;text-transform:uppercase;letter-spacing:.5px}.metric.clickable{cursor:pointer;position:relative}.metric.clickable:active{transform:scale(.99)}.progressTrack{height:5px;border-radius:999px;background:#e5e7e9;overflow:hidden;margin:0 0 clamp(10px,1.2vh,22px)}.progressFill{height:100%;width:0;background:var(--orange);border-radius:inherit;transition:width .35s ease}.metric.clickable:after{content:"⌄";position:absolute;right:clamp(9px,.8vw,14px);top:50%;transform:translateY(-50%);font-size:clamp(18px,1.2vw,24px);color:var(--orange);font-weight:800}
          .selectedHead{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:clamp(7px,.7vh,11px);margin-bottom:clamp(8px,.9vh,14px);text-align:center}.selectedTitle{width:100%;text-align:center;font-size:clamp(14px,1.12vw,19px);font-weight:800;letter-spacing:.35px}.minorActions{display:grid;grid-template-columns:repeat(2,minmax(150px,220px));justify-content:center;gap:clamp(8px,.75vw,13px);width:100%}.minor{min-height:clamp(42px,4vh,56px);border:1px solid var(--line);background:var(--soft);border-radius:clamp(14px,1vw,18px);padding:clamp(9px,.8vh,13px) clamp(14px,1.1vw,20px);font:800 clamp(13px,1vw,17px) inherit;letter-spacing:.15px;cursor:pointer;color:var(--ink);display:flex;align-items:center;justify-content:center;text-align:center}.minor:active{transform:scale(.99);background:#eceef0}
          .chips{display:flex;gap:6px;flex-wrap:wrap;min-height:30px;margin-bottom:clamp(8px,.9vh,18px)}.chip{border:1px solid #ffd0bd;background:#fff4ef;color:#c74712;border-radius:17px;padding:clamp(6px,.6vh,9px) clamp(9px,.75vw,13px);font-size:clamp(11px,.82vw,14px);font-weight:700}.empty{color:var(--muted);font-size:clamp(11px,.88vw,15px);padding:5px 0}
          .mainBtn{width:100%;height:clamp(52px,5.1vh,78px);border:0;border-radius:clamp(16px,1.3vw,24px);background:var(--orange);color:#fff;font-size:clamp(17px,1.45vw,24px);font-weight:800;letter-spacing:.2px;cursor:pointer;box-shadow:0 6px 20px rgba(255,100,30,.23)}
          .mainBtn:disabled{background:#d7d9dc;box-shadow:none;color:#8d9297;cursor:not-allowed}
          .rowActions{display:grid;grid-template-columns:1fr 1fr;gap:clamp(7px,.7vw,12px);margin-top:clamp(7px,.75vh,13px)}.secondary{height:clamp(42px,3.8vh,58px);border:0;border-radius:clamp(13px,1vw,19px);background:var(--orange);font-size:clamp(13px,1vw,17px);font-weight:800;cursor:pointer;color:#fff;box-shadow:0 4px 14px rgba(255,100,30,.18);display:flex;align-items:center;justify-content:center;gap:10px}.secondary:active{background:var(--orange-dark);transform:scale(.995)}.secondary:disabled{background:#d7d9dc;color:#8d9297;box-shadow:none;cursor:not-allowed;transform:none}.resumeInlineBtn:disabled,.resumeAction:disabled{opacity:.48;cursor:not-allowed}.pauseIcon{display:inline-flex;gap:4px;align-items:center}.pauseIcon i{display:block;width:4px;height:17px;border-radius:2px;background:currentColor}
          .modePanel{display:none;margin:clamp(8px,.9vh,16px) 0 clamp(8px,.9vh,16px);padding:clamp(12px,1.1vh,18px) clamp(12px,1.2vw,20px);border:1px solid var(--line);border-radius:clamp(14px,1.1vw,20px);background:#fff}.modePanel.open{display:block}.modeTitle{font-weight:800;font-size:clamp(13px,1vw,17px);margin-bottom:10px}.modeChoices{display:grid;grid-template-columns:repeat(3,1fr);gap:clamp(6px,.7vw,12px)}.modeChoice{min-height:clamp(42px,4.2vh,58px);border:1px solid var(--line);border-radius:clamp(12px,1vw,18px);background:var(--soft);color:var(--ink);font-size:clamp(11px,.9vw,15px);font-weight:760;cursor:pointer;padding:8px}.modeChoice.active{background:var(--orange);border-color:var(--orange);color:#fff;box-shadow:0 4px 14px rgba(255,100,30,.18)}.modeChoice:active{transform:scale(.99)}
          .heightPanel{display:none;margin:clamp(8px,.9vh,16px) 0 clamp(8px,.9vh,16px);padding:clamp(12px,1.1vh,18px) clamp(12px,1.2vw,20px);border:1px solid var(--line);border-radius:clamp(14px,1.1vw,20px);background:#fff}.heightPanel.open{display:block}.heightTop{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}.heightTitle{font-weight:800;font-size:clamp(13px,1vw,17px)}.heightValue{font-weight:850;color:var(--orange);font-size:clamp(18px,1.45vw,25px)}.heightSlider{--pct:0%;-webkit-appearance:none;appearance:none;width:100%;height:36px;margin:0;padding:0;background:transparent;cursor:pointer;touch-action:none}.heightSlider::-webkit-slider-runnable-track{height:7px;border-radius:999px;background:linear-gradient(to right,var(--orange) 0,var(--orange) var(--pct),#d9dde1 var(--pct),#d9dde1 100%)}.heightSlider::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;width:26px;height:26px;border-radius:50%;background:#fff;border:3px solid var(--orange);box-shadow:0 2px 8px rgba(0,0,0,.24);margin-top:-9.5px}.heightSlider::-moz-range-track{height:7px;border-radius:999px;background:#d9dde1}.heightSlider::-moz-range-progress{height:7px;border-radius:999px;background:var(--orange)}.heightSlider::-moz-range-thumb{width:22px;height:22px;border-radius:50%;background:#fff;border:3px solid var(--orange);box-shadow:0 2px 8px rgba(0,0,0,.24)}.heightScale{display:flex;justify-content:space-between;color:var(--muted);font-size:clamp(9px,.72vw,12px);margin-top:1px}.resumeInline{display:none;margin:0 0 10px;padding:12px;border:1px solid #ffd8c3;border-radius:16px;background:#fff8f4}.resumeInline.show{display:block}.resumeInlineText{font-size:13px;font-weight:800;color:#5b4539;margin-bottom:9px}.resumeInlineActions{display:grid;grid-template-columns:1fr 1fr;gap:8px}.resumeInlineBtn{height:44px;border-radius:13px;font-weight:900;font-size:13px;cursor:pointer}.resumeInlineBtn.resume{background:var(--orange);border:1px solid var(--orange);color:#fff}.resumeInlineBtn.fresh{background:#fff;border:1px solid var(--line);color:var(--ink)}@media(max-width:600px){.resumeInlineActions{grid-template-columns:1fr}}.resumeOverlay{position:absolute;inset:0;z-index:100020;display:none;align-items:center;justify-content:center;padding:22px;background:rgba(13,17,21,.42);backdrop-filter:blur(8px)}.resumeOverlay.open{display:flex}.resumeDialog{width:min(92%,560px);background:#fff;border-radius:26px;padding:24px;box-shadow:0 24px 80px rgba(0,0,0,.28);border:1px solid rgba(0,0,0,.08)}.resumeIcon{width:54px;height:54px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:#fff1e9;color:var(--orange);margin-bottom:14px}.resumeIcon ha-icon{--mdc-icon-size:30px}.resumeTitle{font-size:24px;font-weight:900;letter-spacing:-.4px}.resumeText{margin-top:7px;color:var(--muted);font-size:15px;line-height:1.45}.resumeProgress{margin-top:16px;padding:12px 14px;border-radius:15px;background:var(--soft);font-weight:800}.resumeActions{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:18px}.resumeAction{height:52px;border-radius:16px;border:1px solid var(--line);font-weight:900;font-size:14px;cursor:pointer}.resumeAction.resume{background:var(--orange);border-color:var(--orange);color:#fff}.resumeAction.fresh{background:#fff;color:var(--ink)}.resumeCancel{width:100%;height:42px;margin-top:8px;border:0;background:transparent;color:var(--muted);font-weight:800;cursor:pointer}@media(max-width:600px){.resumeActions{grid-template-columns:1fr}.resumeDialog{padding:20px}.resumeTitle{font-size:21px}}.hint{text-align:center;color:var(--muted);font-size:clamp(9px,.75vw,13px);margin-top:clamp(6px,.7vh,14px)}.error{display:none;color:#b42318;background:#fff0ed;padding:11px 14px;border-radius:14px;margin-top:12px;font-size:14px}
          .shell.dark{--ink:#f2f5f7;--muted:#aeb6bf;--line:#343c45;--soft:#252c34;background:#11161b;color:var(--ink);color-scheme:dark}
          .shell.dark .top,.shell.dark .panel,.shell.dark .settingsDrawer,.shell.dark .settingsHeader,.shell.dark .settingsBody,.shell.dark .schedulerDrawer,.shell.dark .schedulerHeader,.shell.dark .schedulerBody{background:#11161b;color:var(--ink)}
          .shell.dark .mapWrap{background:linear-gradient(#11171d,#090d11)}
          .shell.dark .pill,.shell.dark .metric,.shell.dark .minor,.shell.dark .modePanel,.shell.dark .heightPanel,.shell.dark .settingsList,.shell.dark .settingRow,.shell.dark .resumeDialog,.shell.dark .resumeProgress,.shell.dark .modeChoice,.shell.dark .resumeAction.fresh{background:#1c232a;color:var(--ink);border-color:var(--line)}
          .shell.dark .settingRow:active,.shell.dark .minor:active{background:#272f38}
          .shell.dark .settingsGroupTitle{color:#aeb6bf}
          .shell.dark .settingIcon,.shell.dark .resumeIcon{background:#3a241b;color:var(--orange)}
          .shell.dark .mapBadge,.shell.dark .mapExpand{background:rgba(25,31,38,.94);color:var(--ink);border-color:rgba(255,255,255,.1)}
          .shell.dark .progressTrack{background:#343c45}
          .shell.dark .chip{background:#35251f;color:#ff9a6c;border-color:#70432f}
          .shell.dark .error{background:#3b1f1c;color:#ffb4aa}
          .shell.dark .settingEntity{color:#77818b}
          .shell.dark .toggleSwitch{background:#4a535d}
          .shell.dark .toggleSwitch.on{background:var(--orange)}
          .shell.dark .heightSlider::-webkit-slider-thumb,.shell.dark .settingSlider::-webkit-slider-thumb{background:#e9edf1}
          .shell.dark .heightSlider::-moz-range-thumb,.shell.dark .settingSlider::-moz-range-thumb{background:#e9edf1}
          .shell.dark .settingsOverlay{background:rgba(0,0,0,.62)}
          .shell.dark .resumeOverlay{background:rgba(0,0,0,.62)}
          .shell.dark .resumeInline{background:linear-gradient(135deg,#211914,#181d22);border-color:#563421;box-shadow:inset 0 1px 0 rgba(255,255,255,.025)}
          .shell.dark .resumeInlineText{color:#ffc2a1}
          .shell.dark .resumeInlineBtn.fresh{background:#252d35;color:#dbe2e8;border-color:#46515c}
          .shell.dark .resumeInlineBtn.fresh:hover:not(:disabled){background:#303943;border-color:#65717d}
          .shell.dark .resumeInlineBtn.resume{box-shadow:0 4px 16px rgba(255,100,30,.18)}
          .shell.dark .resumeInlineBtn:disabled{background:#1b2229!important;color:#68737e!important;border-color:#303942!important;box-shadow:none;opacity:1}
          .shell.dark .secondary:disabled{background:#20272e;color:#69747f;border:1px solid #303942;box-shadow:none}
          .shell.dark .mapWrap.fullscreen{background:#0b1014!important}
          .settingsOverlay{position:absolute;inset:0;z-index:50;background:rgba(17,23,28,.30);backdrop-filter:blur(4px);display:none;align-items:stretch;justify-content:flex-end;overflow:hidden}.settingsOverlay.open{display:flex}.settingsDrawer{position:absolute;top:0;right:0;width:min(90vw,1080px);max-width:calc(100% - 12px);height:100%;box-sizing:border-box;background:#fff;box-shadow:-12px 0 44px rgba(0,0,0,.18);display:flex;flex-direction:column;overflow:hidden}.settingsHeader{flex:0 0 auto;display:flex;align-items:center;justify-content:space-between;gap:16px;padding:clamp(20px,2.2vh,34px) clamp(20px,2.2vw,34px);border-bottom:1px solid var(--line)}.settingsHeaderTitle{display:flex;align-items:center;gap:12px;font-size:clamp(22px,1.8vw,32px);font-weight:800}.settingsHeaderTitle ha-icon{color:var(--orange);--mdc-icon-size:clamp(28px,2.1vw,38px)}.settingsClose{width:clamp(46px,3.2vw,60px);height:clamp(46px,3.2vw,60px);border:0;border-radius:50%;background:var(--soft);color:var(--ink);display:flex;align-items:center;justify-content:center;cursor:pointer}.settingsClose ha-icon{--mdc-icon-size:clamp(24px,1.8vw,30px)}.settingsBody{flex:1 1 auto;min-width:0;overflow-y:auto;overflow-x:hidden;padding:clamp(16px,1.7vh,26px) clamp(18px,2vw,32px) clamp(28px,3vh,48px);box-sizing:border-box;-webkit-overflow-scrolling:touch;overscroll-behavior:contain}.settingsIntro{color:var(--muted);font-size:clamp(12px,.95vw,16px);margin:0 0 18px}.settingsGroup{margin-bottom:clamp(18px,2.1vh,30px)}.settingsGroupTitle{font-size:clamp(13px,1vw,17px);font-weight:850;letter-spacing:.45px;text-transform:uppercase;margin:0 0 9px;color:#555e66}.settingsList{border:1px solid var(--line);border-radius:clamp(16px,1.2vw,22px);overflow:hidden;background:#fff}.settingRow{display:grid;grid-template-columns:auto minmax(0,1fr) auto;align-items:center;gap:clamp(12px,1.1vw,18px);min-height:clamp(58px,5.8vh,82px);padding:clamp(10px,1vh,15px) clamp(14px,1.25vw,20px);border:0;border-bottom:1px solid var(--line);width:100%;max-width:100%;box-sizing:border-box;background:#fff;color:var(--ink);text-align:left;cursor:pointer;font:inherit;overflow:hidden}.settingRow:last-child{border-bottom:0}.settingRow:active{background:var(--soft)}.settingIcon{width:clamp(38px,2.8vw,50px);height:clamp(38px,2.8vw,50px);border-radius:50%;background:#fff1ea;color:var(--orange);display:flex;align-items:center;justify-content:center}.settingIcon ha-icon{--mdc-icon-size:clamp(21px,1.55vw,27px)}.settingName{font-size:clamp(14px,1.05vw,18px);font-weight:720}.settingEntity{font-size:clamp(9px,.68vw,11px);color:#a0a6ac;margin-top:2px;display:none}.settingValue{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:clamp(12px,.95vw,16px);font-weight:650;max-width:260px;text-align:right}.settingValue .on{color:var(--orange)}.settingValue ha-icon{--mdc-icon-size:clamp(18px,1.3vw,23px);color:#a5abb1}.unitSelect{appearance:none;border:1px solid var(--line);border-radius:999px;background:var(--soft);color:var(--ink);font:inherit;font-weight:750;padding:9px 34px 9px 14px;cursor:pointer;background-image:linear-gradient(45deg,transparent 50%,var(--muted) 50%),linear-gradient(135deg,var(--muted) 50%,transparent 50%);background-position:calc(100% - 16px) 50%,calc(100% - 11px) 50%;background-size:5px 5px,5px 5px;background-repeat:no-repeat}.settingsEmpty{padding:24px;text-align:center;color:var(--muted)}
          .settingRow.switchRow{cursor:default}.settingRow.numberRow{grid-template-columns:auto minmax(0,1fr);cursor:default;padding-top:clamp(12px,1.2vh,18px);padding-bottom:clamp(12px,1.2vh,18px)}.settingRow.numberRow:active,.settingRow.switchRow:active{background:#fff}.numberSetting{min-width:0}.numberHead{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:clamp(8px,.8vh,12px)}.numberCurrent{font-size:clamp(13px,1vw,17px);font-weight:850;color:var(--orange);white-space:nowrap}.settingSlider{--pct:0%;-webkit-appearance:none;appearance:none;width:100%;height:34px;margin:0;padding:0;background:transparent;cursor:pointer;touch-action:none}.settingSlider::-webkit-slider-runnable-track{height:6px;border-radius:999px;background:linear-gradient(to right,var(--orange) 0,var(--orange) var(--pct),#d9dde1 var(--pct),#d9dde1 100%)}.settingSlider::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;width:24px;height:24px;border-radius:50%;background:#fff;border:3px solid var(--orange);box-shadow:0 2px 7px rgba(0,0,0,.22);margin-top:-9px}.settingSlider::-moz-range-track{height:6px;border-radius:999px;background:#d9dde1}.settingSlider::-moz-range-progress{height:6px;border-radius:999px;background:var(--orange)}.settingSlider::-moz-range-thumb{width:20px;height:20px;border-radius:50%;background:#fff;border:3px solid var(--orange);box-shadow:0 2px 7px rgba(0,0,0,.22)}.settingSlider{transition:filter .15s ease}.settingSlider:active{filter:drop-shadow(0 2px 5px rgba(255,100,30,.18))}.settingScale{display:flex;justify-content:space-between;gap:12px;color:#a0a6ac;font-size:clamp(9px,.7vw,12px);margin-top:1px}.toggleWrap{display:flex;align-items:center;gap:clamp(8px,.7vw,12px)}.toggleState{min-width:30px;color:var(--muted);font-size:clamp(11px,.85vw,14px);font-weight:800;text-align:right}.toggleState.on{color:var(--orange)}.toggleSwitch{position:relative;width:clamp(52px,3.8vw,66px);height:clamp(30px,2.35vw,38px);border:0;border-radius:999px;background:#cfd3d7;cursor:pointer;padding:0;transition:background .18s ease,box-shadow .18s ease;box-shadow:inset 0 0 0 1px rgba(0,0,0,.04)}.toggleSwitch.on{background:var(--orange);box-shadow:0 3px 12px rgba(255,100,30,.22)}.toggleKnob{position:absolute;top:3px;left:3px;width:calc(100% / 2 - 3px);height:calc(100% - 6px);border-radius:50%;background:#fff;box-shadow:0 1px 5px rgba(0,0,0,.25);transition:transform .18s ease}.toggleSwitch.on .toggleKnob{transform:translateX(100%)}
          .schedulerOverlay{position:absolute;inset:0;z-index:70;background:rgba(18,22,26,.42);backdrop-filter:blur(7px);display:none;align-items:center;justify-content:center;padding:clamp(16px,2.2vw,42px);box-sizing:border-box;overflow:hidden}.schedulerOverlay.open{display:flex}.schedulerDrawer{position:relative;width:min(92vw,1180px);height:min(88vh,1500px);max-width:100%;max-height:100%;background:#fff;border-radius:clamp(24px,2vw,38px);box-shadow:0 24px 80px rgba(0,0,0,.28);display:flex;flex-direction:column;overflow:hidden;box-sizing:border-box;animation:schedulePop .16s ease-out}.schedulerHeader{flex:0 0 auto;display:flex;align-items:center;justify-content:space-between;gap:16px;padding:clamp(18px,2vh,30px) clamp(20px,2.2vw,34px);border-bottom:1px solid var(--line);background:#fff}.schedulerHeaderTitle{display:flex;align-items:center;gap:12px;font-size:clamp(23px,1.9vw,34px);font-weight:850;color:var(--ink)}.schedulerHeaderTitle ha-icon{color:var(--orange);--mdc-icon-size:clamp(29px,2.1vw,39px)}.schedulerClose{position:relative;z-index:5;width:clamp(50px,3.5vw,64px);height:clamp(50px,3.5vw,64px);border:0;border-radius:50%;background:var(--orange);color:#fff;display:flex;align-items:center;justify-content:center;cursor:pointer;touch-action:manipulation;box-shadow:0 4px 14px rgba(255,100,30,.24)}.schedulerClose ha-icon{pointer-events:none;--mdc-icon-size:clamp(26px,1.9vw,32px)}.schedulerClose:active{transform:scale(.95);background:var(--orange-dark)}.schedulerBody{flex:1 1 auto;min-height:0;overflow:auto;background:#f5f6f7;padding:clamp(12px,1.3vw,22px);box-sizing:border-box;-webkit-overflow-scrolling:touch;overscroll-behavior:contain}.schedulerHost{min-height:100%;max-width:100%;box-sizing:border-box}.schedulerMissing{margin:30px;padding:24px;background:#fff;border:1px solid var(--line);border-radius:20px;text-align:center;color:var(--muted)}@keyframes schedulePop{from{opacity:0;transform:scale(.975) translateY(8px)}to{opacity:1;transform:scale(1) translateY(0)}}
          /* v0.6.26 proportional responsive layout */
          @media (min-width:701px) and (orientation:landscape){
            .top{padding:clamp(7px,1.05vh,14px) clamp(14px,1.45vw,28px) clamp(6px,.8vh,11px)}
            .homeBtn,.roundAction{width:clamp(44px,3.2vmin,56px);height:clamp(44px,3.2vmin,56px)}
            .model{font-size:clamp(22px,2.05vmin,30px)}
            .sub{font-size:clamp(11px,1.05vmin,14px)}
            .pill{height:clamp(34px,3.7vmin,42px);font-size:clamp(11px,1.05vmin,14px)}
            .mapWrap{margin:0 clamp(8px,.9vw,16px);border-radius:clamp(16px,1.35vw,25px);flex:1 1 62%;min-height:40dvh}
            .panel{padding:clamp(7px,.8vh,12px) clamp(14px,1.55vw,28px) clamp(7px,.9vh,13px);flex:0 0 auto}
            .statusline{margin-bottom:clamp(5px,.65vh,9px)}
            .state{font-size:clamp(17px,1.55vmin,23px)}
            .metrics{gap:clamp(5px,.55vw,9px);margin-bottom:clamp(5px,.55vh,8px)}
            .metric{padding:clamp(7px,.75vh,11px) clamp(8px,.75vw,13px);border-radius:clamp(10px,.85vw,15px)}
            .metric .v{font-size:clamp(17px,1.55vmin,23px)}
            .metric .k{font-size:clamp(9px,.78vmin,11px)}
            .progressTrack{margin-bottom:clamp(5px,.6vh,9px);height:4px}
            .selectedHead{flex-direction:row;justify-content:center;gap:clamp(10px,1.1vw,20px);margin-bottom:clamp(4px,.5vh,7px)}
            .selectedTitle{width:auto;white-space:nowrap;font-size:clamp(13px,1.15vmin,17px)}
            .minorActions{width:auto;grid-template-columns:repeat(2,minmax(145px,220px));gap:clamp(6px,.55vw,9px)}
            .minor{min-height:clamp(34px,3.7vmin,44px);padding:5px 12px;font-size:clamp(11px,1vmin,14px)}
            .chips{min-height:20px;max-height:28px;overflow:hidden;margin-bottom:clamp(4px,.5vh,7px)}
            .chip{padding:4px 9px;font-size:clamp(10px,.9vmin,12px)}
            .empty{font-size:clamp(10px,.95vmin,13px);padding:2px 0}
            .mainBtn{height:clamp(42px,4.8vmin,56px);font-size:clamp(15px,1.35vmin,19px)}
            .rowActions{margin-top:clamp(4px,.45vh,7px)}
            .secondary{height:clamp(34px,3.8vmin,44px);font-size:clamp(12px,1.05vmin,15px)}
            .hint{display:none}
          }
          @media (max-width:700px){
            .mapWrap{flex:1 1 56dvh;min-height:42dvh}
            .panel{max-height:47dvh;overflow:auto;overscroll-behavior:contain;-webkit-overflow-scrolling:touch}
            .top{padding:10px 10px 7px}
            .homeBtn,.roundAction{width:44px;height:44px}
            .model{font-size:clamp(19px,6vw,25px)}
            .metrics{margin-bottom:5px}
            .metric{padding:7px 7px}
            .selectedHead{gap:5px;margin-bottom:5px}
            .minor{min-height:38px}
            .chips{margin-bottom:5px;min-height:22px;max-height:48px;overflow:auto}
            .mainBtn{height:48px}
            .secondary{height:38px}
            .hint{display:none}
          }
          @media (min-width:701px) and (orientation:portrait){
            .mapWrap{flex:1 1 61%;min-height:0}
            .panel{flex:0 0 auto;max-height:39%;overflow:auto;overscroll-behavior:contain}
          }
          @media(max-width:700px){.settingsDrawer{width:100vw}.schedulerOverlay{padding:8px}.schedulerDrawer{width:calc(100vw - 16px);height:calc(100dvh - 16px);border-radius:22px}.topstats .pill:first-child{display:none}.settingValue{max-width:130px}.settingsEntity{display:none}}
          @media(max-width:700px){.minorActions{grid-template-columns:repeat(2,minmax(0,1fr))}.metrics{grid-template-columns:repeat(2,1fr)}.modeChoices{grid-template-columns:1fr}.top{padding:14px 12px 10px}.sub{display:none}.pill{padding:0 9px}.mapWrap{margin:0 6px;border-radius:18px}.panel{padding:10px 12px 12px}.mapBadge{left:12px;top:12px;padding:8px 10px}.mapBadge strong{font-size:14px}.mapBadge span{font-size:11px}.zoneNameLabel{font-size:16px}.zoneIdLabel{font-size:9px}.liveBadge{right:10px;top:10px;padding:7px 9px}.metrics{gap:5px}.metric{padding:8px 7px}.rowActions{gap:6px}}
          @media(max-width:480px){#schedulerBtn,#settingsBtn{width:38px;height:38px}#schedulerBtn ha-icon,#settingsBtn ha-icon{--mdc-icon-size:21px}.topRight{gap:6px}}
          @media(max-height:900px){.top{padding-top:8px;padding-bottom:7px}.sub{display:none}.mapBadge{padding:7px 10px}.mapBadge span{display:none}.panel{padding-top:8px;padding-bottom:8px}.statusline{margin-bottom:7px}.metrics{margin-bottom:7px}.selectedHead{margin-bottom:5px}.chips{margin-bottom:6px;min-height:24px}.mainBtn{height:46px}.secondary{height:38px}.hint{display:none}}
          @media(max-height:700px){.topstats .pill:first-child{display:none}.mapBadge{display:none}.metrics{grid-template-columns:repeat(4,1fr)}.panel{padding-top:6px}.state small{display:none}.chips{max-height:28px;overflow:hidden}.rowActions{margin-top:5px}.secondary{height:34px}}
        </style>
        <div class="shell">
          <div class="top">
            <div class="topLeft"><button class="homeBtn" id="homeBtn" type="button" aria-label="Return to The 551 dashboard">⌂</button><div><div class="model" id="mowerTitle">Navimow</div><div class="sub" id="online">Interactive zone mowing</div></div></div>
            <div class="topRight"><div class="topstats"><div class="pill"><span class="dot"></span><span id="statusTop">—</span></div><div class="pill battery">🔋 <span id="batteryTop">—%</span></div></div><button class="roundAction" id="schedulerBtn" type="button" title="Mowing schedule" aria-label="Open mowing schedule"><ha-icon icon="mdi:calendar-clock"></ha-icon></button><button class="roundAction" id="settingsBtn" type="button" title="Configuration" aria-label="Open mower configuration"><ha-icon icon="mdi:cog"></ha-icon></button></div>
          </div>
          <div class="mapWrap" id="mapWrap">
            <div class="mapStage" id="mapStage"><img id="mapImage" alt="Navimow live mowing map"><svg id="overlay" class="zoneOverlay" aria-label="Selectable mowing zones"></svg></div><div class="liveBadge stale" id="liveBadge"><span class="liveDot"></span><span id="liveText">LIVE · waiting</span></div>
            <button class="mapExpand" id="mapExpand" type="button" title="Full screen map" aria-label="Open full screen map"><ha-icon icon="mdi:fullscreen"></ha-icon></button>
            <div class="fullscreenBar"><span id="fullscreenSelection">No zones selected</span><button class="fullscreenDone" id="fullscreenDone" type="button">DONE</button></div>
            <div class="mapBadge"><strong>Tap lawn zones</strong><span>Multiple zones can be selected</span></div><div class="delayAlert" id="delayAlert" role="status" aria-live="polite"><div class="delayIcon"><ha-icon id="delayIcon" icon="mdi:weather-pouring"></ha-icon></div><div class="delayCopy"><div class="delayTitle" id="delayTitle">MOWING DELAYED</div><div class="delayText" id="delayText">Waiting for conditions to improve</div></div><span class="delayPulse" aria-hidden="true"></span></div>
          </div>
          <div class="panel">
            <div class="statusline"><div class="state" id="stateText">—<small id="stateSub">Select the areas you want to mow</small></div></div>
            <div class="metrics">
              <div class="metric"><div class="v" id="progress">—</div><div class="k">Progress</div></div>
              <div class="metric"><div class="v" id="weekArea">—</div><div class="k">This week</div></div>
              <div class="metric clickable" id="heightMetric" role="button" tabindex="0" aria-label="Adjust cutting height"><div class="v" id="height">—</div><div class="k">Cut height</div></div>
              <div class="metric clickable" id="modeMetric" role="button" tabindex="0" aria-label="Select mowing work mode"><div class="v" id="workMode">—</div><div class="k">Work mode</div></div>
            </div><div class="progressTrack" aria-hidden="true"><div class="progressFill" id="progressFill"></div></div>
            <div class="modePanel" id="modePanel"><div class="modeTitle">Work mode</div><div class="modeChoices"><button class="modeChoice" data-mode="Precision Mowing">Precision Mowing</button><button class="modeChoice" data-mode="Standard Mowing">Standard Mowing</button><button class="modeChoice" data-mode="Efficient Mowing">Efficient Mowing</button></div></div>
            <div class="heightPanel" id="heightPanel"><div class="heightTop"><div class="heightTitle">Cutting height</div><div class="heightValue" id="heightValue">—</div></div><input class="heightSlider" id="heightSlider" type="range" min="1" max="4" step="0.1" value="2.6" aria-label="Cutting height"><div class="heightScale"><span id="heightMin">1.0 in</span><span id="heightMax">4.0 in</span></div></div>
            <div class="selectedHead"><div class="selectedTitle" id="selectedTitle">SELECTED AREAS</div><div class="minorActions"><button class="minor" id="allBtn">SELECT ALL ZONES</button><button class="minor" id="clearBtn">CLEAR ZONE(S)</button></div></div>
            <div class="chips" id="chips"><span class="empty">Tap a zone on the map.</span></div>
            <div class="resumeInline" id="resumeInline"><div class="resumeInlineText" id="resumeInlineText">Existing progress found.</div><div class="resumeInlineActions"><button class="resumeInlineBtn resume" id="resumeInlineBtn" type="button">▶ RESUME WORK</button><button class="resumeInlineBtn fresh" id="freshInlineBtn" type="button">↻ START FRESH</button></div></div>
            <button class="mainBtn" id="mowBtn" disabled>SELECT A ZONE</button>
            <div class="rowActions"><button class="secondary" id="pauseBtn"><span class="pauseIcon" aria-hidden="true"><i></i><i></i></span><span>PAUSE</span></button><button class="secondary" id="dockBtn"><span aria-hidden="true">⌂</span><span>RETURN HOME</span></button></div>
            <div class="error" id="error"></div>
            <div class="hint">If a selected zone already has mowing progress, choose Resume or Start fresh.</div>
          </div>
          <div class="resumeOverlay" id="resumeOverlay" aria-hidden="true"><div class="resumeDialog" role="dialog" aria-modal="true" aria-label="Resume mowing or start fresh"><div class="resumeIcon"><ha-icon icon="mdi:progress-clock"></ha-icon></div><div class="resumeTitle">Existing mowing progress found</div><div class="resumeText" id="resumeText">This zone already has work completed.</div><div class="resumeProgress" id="resumeProgress">—</div><div class="resumeActions"><button class="resumeAction resume" id="resumeBtn" type="button">▶ RESUME WORK</button><button class="resumeAction fresh" id="freshBtn" type="button">↻ START FRESH</button></div><button class="resumeCancel" id="resumeCancel" type="button">CANCEL</button></div></div>
          <div class="settingsOverlay" id="settingsOverlay" aria-hidden="true"><div class="settingsDrawer" role="dialog" aria-modal="true" aria-label="Navimow configuration"><div class="settingsHeader"><div class="settingsHeaderTitle"><ha-icon icon="mdi:cog"></ha-icon><span>Configuration</span></div><button class="settingsClose" id="settingsClose" type="button" aria-label="Close configuration"><ha-icon icon="mdi:close"></ha-icon></button></div><div class="settingsBody"><p class="settingsIntro">All Home Assistant configuration entities for this mower. Tap any setting to adjust it.</p><div id="settingsContent"></div></div></div></div>
          <div class="schedulerOverlay" id="schedulerOverlay" aria-hidden="true"><div class="schedulerDrawer" role="dialog" aria-modal="true" aria-label="Navimow mowing schedule"><div class="schedulerHeader"><div class="schedulerHeaderTitle"><ha-icon icon="mdi:calendar-clock"></ha-icon><span>Mowing schedule</span></div><button class="schedulerClose" id="schedulerClose" type="button" aria-label="Close mowing schedule"><ha-icon icon="mdi:close"></ha-icon></button></div><div class="schedulerBody"><div class="schedulerHost" id="schedulerHost"></div></div></div></div>
        </div>`;
    this._applyDarkMode();
    this.querySelector('#homeBtn').addEventListener('click',()=>this._navigateHome());
    this.querySelector('#mapExpand').addEventListener('click',()=>this._toggleMapFullscreen());
    this.querySelector('#fullscreenDone').addEventListener('click',()=>this._toggleMapFullscreen(false));
    this.querySelector('#schedulerBtn').addEventListener('click',()=>this._openScheduler());
    const schedulerClose=this.querySelector('#schedulerClose');
    schedulerClose.addEventListener('click',(e)=>{e.preventDefault();e.stopPropagation();this._closeScheduler();});
    schedulerClose.addEventListener('pointerup',(e)=>{e.preventDefault();e.stopPropagation();this._closeScheduler();});
    this.querySelector('#schedulerOverlay').addEventListener('click',e=>{if(e.target===this.querySelector('#schedulerOverlay'))this._closeScheduler();});
    this._scheduleKeyHandler=(e)=>{if(e.key!=='Escape')return;if(this.querySelector('#schedulerOverlay')?.classList.contains('open'))this._closeScheduler();else if(this.querySelector('#settingsOverlay')?.classList.contains('open'))this._closeSettings();else if(this.querySelector('#mapWrap')?.classList.contains('fullscreen'))this._toggleMapFullscreen(false);};
    window.addEventListener('keydown',this._scheduleKeyHandler);
    this.querySelector('#settingsBtn').addEventListener('click',()=>this._openSettings());
    this.querySelector('#settingsClose').addEventListener('click',()=>this._closeSettings());
    this.querySelector('#settingsOverlay').addEventListener('click',e=>{if(e.target===this.querySelector('#settingsOverlay'))this._closeSettings();});
    this.querySelector('#allBtn').addEventListener('click',()=>this._selectAll());
    this.querySelector('#clearBtn').addEventListener('click',()=>{this._selected.clear();this._paintSelection();});
    this.querySelector('#mowBtn').addEventListener('click',()=>this._mow());
    this.querySelector('#resumeInlineBtn').addEventListener('click',()=>this._confirmMow(false,[...this._selected]));
    this.querySelector('#freshInlineBtn').addEventListener('click',()=>this._confirmMow(true,[...this._selected]));
    this.querySelector('#resumeBtn').addEventListener('click',()=>this._confirmMow(false));
    this.querySelector('#freshBtn').addEventListener('click',()=>this._confirmMow(true));
    this.querySelector('#resumeCancel').addEventListener('click',()=>this._closeResumeDialog());
    this.querySelector('#resumeOverlay').addEventListener('click',e=>{if(e.target===this.querySelector('#resumeOverlay'))this._closeResumeDialog();});
    this.querySelector('#pauseBtn').addEventListener('click',()=>this._togglePauseResume());
    this.querySelector('#dockBtn').addEventListener('click',()=>{
      this._manualDockAt=Date.now();
      this.querySelector('#delayAlert')?.classList.remove('show');
      this._service('lawn_mower','dock',{entity_id:this.config.mower});
    });
    const heightMetric=this.querySelector('#heightMetric');
    heightMetric.addEventListener('click',()=>this._toggleHeightPanel());
    heightMetric.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();this._toggleHeightPanel();}});
    const heightSlider=this.querySelector('#heightSlider');
    heightSlider.addEventListener('pointerdown',()=>{heightSlider.dataset.dragging='1';});
    heightSlider.addEventListener('input',()=>{
      heightSlider.dataset.dragging='1';
      this._previewHeight(this._heightSliderDisplayedValue(heightSlider));
      this._paintSettingSlider(heightSlider);
    });
    heightSlider.addEventListener('change',()=>{
      const selected=this._heightSliderDisplayedValue(heightSlider);
      heightSlider.dataset.dragging='0';
      this._setHeight(selected);
    });
    heightSlider.addEventListener('pointerup',()=>{heightSlider.dataset.dragging='0';});
    const modeMetric=this.querySelector('#modeMetric');
    modeMetric.addEventListener('click',()=>this._toggleModePanel());
    modeMetric.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();this._toggleModePanel();}});
    this.querySelectorAll('.modeChoice').forEach(btn=>btn.addEventListener('click',()=>this._setWorkMode(btn.dataset.mode)));
    this._refreshImage();
    const prep=()=>{if(this._hass&&!this._settingsBuilt)this._renderSettings();};
    if('requestIdleCallback' in window) requestIdleCallback(prep,{timeout:1200}); else setTimeout(prep,250);
    // Terrain imagery is essentially static. Refreshing the whole camera every
    // two seconds made Chromium cache/swap frames unpredictably and forced the
    // mower to jump. Dynamic trail/pose now live in the SVG overlay below.
    this._timer=setInterval(()=>{this._refreshImage();this._updateLiveBadge();},20000);
  }

  _entity(id){ return this._hass?.states?.[id]; }
  _state(id,fallback='—'){ const e=this._entity(id); return e ? e.state : fallback; }
  _mowerDisplayName(cam=null){
    const configured=String(this.config.name||'').trim();
    if(configured) return configured;
    const mower=this._entity(this.config.mower);
    const friendly=String(mower?.attributes?.friendly_name||'').trim();
    if(friendly) return friendly;
    const camera=cam||this._entity(this.config.camera);
    const deviceName=String(camera?.attributes?.mower_name||'').trim();
    if(deviceName) return deviceName;
    const model=String(camera?.attributes?.mower_model||'').trim();
    if(model && model.toLowerCase()!=='unknown') return /^navimow\b/i.test(model)?model:`Navimow ${model}`;
    const cameraName=String(camera?.attributes?.friendly_name||'').replace(/\s+Live mowing map$/i,'').trim();
    return cameraName||'Navimow';
  }
  _prettyState(v){ return String(v||'unknown').replaceAll('_',' ').replace(/\b\w/g,c=>c.toUpperCase()); }
  _fmtArea(v){ const n=Number(v); return Number.isFinite(n) ? `${Math.round(n).toLocaleString()} ft²` : '—'; }

  _update(){
    const cam=this._entity(this.config.camera);
    if(!cam) return;
    const mowerTitle=this.querySelector('#mowerTitle');
    if(mowerTitle) mowerTitle.textContent=this._mowerDisplayName(cam);
    const battery=this._state(this.config.battery, cam.attributes.battery ?? '—');
    const rawStatus=String(this._state(this.config.status,this._state(this.config.mower))||'unknown').trim().toLowerCase();
    const paused=/pause/.test(rawStatus);
    const mowing=!paused && /mow|working|running/.test(rawStatus);
    const returning=/return|docking/.test(rawStatus);
    const atBase=/docked|idle|charging|ischarging|isidle/.test(rawStatus) && !returning;
    this._operatingState={paused,mowing,returning,atBase,raw:rawStatus};
    if(this._commandBusy && (mowing||paused||returning)) this._commandBusy=false;
    const status=this._prettyState(rawStatus);
    this.querySelector('#batteryTop').textContent=`${battery}%`;
    this.querySelector('#statusTop').textContent=status;
    const statusPill=this.querySelector('#statusTop')?.closest('.pill'); if(statusPill) statusPill.classList.toggle('activeStatus',/mow|return|pause|delay/.test(String(status).toLowerCase()));
    this.querySelector('#stateText').childNodes[0].nodeValue=status;
    const pauseBtn=this.querySelector('#pauseBtn');
    if(pauseBtn){
      pauseBtn.disabled=!(paused||mowing);
      pauseBtn.innerHTML=paused?'<span aria-hidden="true">▶</span><span>RESUME</span>':'<span class="pauseIcon" aria-hidden="true"><i></i><i></i></span><span>PAUSE</span>';
    }
    const dockBtn=this.querySelector('#dockBtn'); if(dockBtn) dockBtn.disabled=atBase||returning;
    this._updateDelayAlert(status,cam);
    const progress=this._state(this.config.progress,'unknown');
    this.querySelector('#progress').textContent=(progress==='unknown'||progress==='unavailable')?'—':`${Number(progress).toFixed(0)}%`;
    const pn=Number(progress); const pf=this.querySelector('#progressFill'); if(pf) pf.style.width=`${Number.isFinite(pn)?Math.max(0,Math.min(100,pn)):0}%`;
    this.querySelector('#weekArea').textContent=this._fmtArea(this._state(this.config.week_area));
    const wm=this._entity(this.config.work_mode);
    const wmState=wm && !['unknown','unavailable'].includes(wm.state) ? wm.state : '—';
    this.querySelector('#workMode').textContent=wmState==='—'?'—':wmState.replace(' Mowing','');
    this.querySelectorAll('.modeChoice').forEach(btn=>btn.classList.toggle('active',btn.dataset.mode===wm?.state));
    const h=this._entity(this.config.cutting_height);
    if(h){
      const unit=this._heightUnit();
      const actual=this._heightFromNative(h.state,h);
      const pending=this._pendingSettings.get(this.config.cutting_height);
      let value=actual;
      if(pending?.type==='number'){
        // Cutting height is displayed in converted user units.  Use a tight
        // tolerance; the entity's backend step can be 5 mm and is far too
        // large to use as an acknowledgement tolerance in inches.
        if(Number.isFinite(actual) && Math.abs(actual-Number(pending.value))<=0.06){
          this._pendingSettings.delete(this.config.cutting_height);
          value=actual;
        }else if(Date.now()<pending.expires){
          value=Number(pending.value);
        }else{
          this._pendingSettings.delete(this.config.cutting_height);
        }
      }
      this.querySelector('#height').textContent=Number.isFinite(value)?`${value.toFixed(1)} ${unit}`:`${h.state} ${unit}`;
      const slider=this.querySelector('#heightSlider');
      const nativeMin=Number(h.attributes.min), nativeMax=Number(h.attributes.max), nativeStep=Number(h.attributes.step);
      const min=this._heightFromNative(nativeMin,h), max=this._heightFromNative(nativeMax,h);
      // Some Home Assistant clients convert the state/min/max to inches
      // while leaving the backend's 5 mm step untouched. Using that mixed
      // attribute would create only minimum/maximum slider positions.
      const displayStep=this._usesImperialHeight()?5/25.4:5;
      const choices=[];
      if(Number.isFinite(min)&&Number.isFinite(max)&&Number.isFinite(displayStep)&&displayStep>0){
        const count=Math.max(1,Math.round((max-min)/displayStep));
        for(let index=0;index<=count;index+=1){
          choices.push(Number((index===count?max:min+(index*displayStep)).toFixed(4)));
        }
      }
      if(!choices.length&&Number.isFinite(value)) choices.push(value);
      slider.dataset.heightValues=choices.join(',');
      slider.min='0';
      slider.max=String(Math.max(0,choices.length-1));
      slider.step='1';
      if(Number.isFinite(value)&&slider.dataset.dragging!=='1'){
        let closest=0;
        choices.forEach((choice,index)=>{if(Math.abs(choice-value)<Math.abs(choices[closest]-value))closest=index;});
        slider.value=String(closest);
      }
      this._paintSettingSlider(slider);
      this.querySelector('#heightValue').textContent=Number.isFinite(value)?`${value.toFixed(1)} ${unit}`:`${h.state} ${unit}`;
      this.querySelector('#heightMin').textContent=`${Number.isFinite(min)?min.toFixed(1):'1.0'} ${unit}`;
      this.querySelector('#heightMax').textContent=`${Number.isFinite(max)?max.toFixed(1):'4.0'} ${unit}`;
    }else{
      this.querySelector('#height').textContent='—';
    }
    this._renderZones(cam);
    this._updateDynamicMap(cam);
    const resumable=Array.isArray(cam?.attributes?.resumable_zone_ids)?cam.attributes.resumable_zone_ids.map(Number).filter(Number.isFinite):[];
    if(!this._resumeSeeded && !this._selected.size && resumable.length){
      resumable.forEach(id=>this._selected.add(id));
      this._resumeSeeded=true;
    }
    this._paintSelection();
    this._updateResumeControls(cam);
    if(this.querySelector('#settingsOverlay')?.classList.contains('open') && !this._settingsUpdateRaf){
      this._settingsUpdateRaf=requestAnimationFrame(()=>{this._settingsUpdateRaf=0;this._updateSettingsControls();});
    }
    if(this.querySelector('#schedulerOverlay')?.classList.contains('open')){
      const scheduler=this.querySelector('#schedulerHost navimow-scheduler-card');
      if(scheduler) scheduler.hass=this._hass;
    }
  }

  _updateDelayAlert(status,cam){
    const el=this.querySelector('#delayAlert'); if(!el) return;
    // An explicit RETURN HOME is user intent, not a weather interruption.
    // Hide any stale latched alert immediately while HA/cloud state catches up.
    if(this._manualDockAt && (Date.now()-this._manualDockAt)<90000){ el.classList.remove('show'); return; }
    const raw=cam?.attributes?.task_delay;
    const lastRaw=cam?.attributes?.last_task_delay;
    const lastAge=Number(cam?.attributes?.last_task_delay_age_s);
    const interruption=cam?.attributes?.interruption_notice;
    const rawActive=raw!==undefined && raw!==null && raw!==false && raw!==0 && raw!=='0' && raw!=='' && raw!=='00';
    // Keep the last real Navimow delay reason visible for up to two hours.  The
    // mower often clears taskDelay immediately when it turns around for home.
    const latchedActive=Number.isFinite(lastAge) && lastAge>=0 && lastAge<=7200 && lastRaw!==undefined && lastRaw!==null;
    const effectiveRaw=rawActive ? raw : (latchedActive ? lastRaw : null);
    const rawText=(effectiveRaw && typeof effectiveRaw==='object') ? JSON.stringify(effectiveRaw) : String(effectiveRaw ?? '');
    const interruptionText=(interruption && typeof interruption==='object') ? JSON.stringify(interruption) : String(interruption ?? '');
    const statusText=String(status||'').toLowerCase();
    const combined=`${statusText} ${rawText.toLowerCase()} ${interruptionText.toLowerCase()}`;

    let related='';
    let inferredWind=false;
    const match=String(this.config.settings_match||'').toLowerCase();
    if(this._hass?.states){
      for(const [id,e] of Object.entries(this._hass.states)){
        if(this._deviceEntityIds?.size ? !this._deviceEntityIds.has(id) : (match && !id.toLowerCase().includes(match))) continue;
        const st=String(e?.state||'').toLowerCase();
        const friendly=String(e?.attributes?.friendly_name||'').toLowerCase();
        const ident=id.toLowerCase();
        if(/rain|raining|frost|snow|wind|temperature|weather|delay|delayed|paused|waiting|storm/.test(st)) related+=` ${st}`;
        const windEntity=/strong[_ ]?wind|wind[_ ]?delay|storm/.test(`${ident} ${friendly}`);
        const domain=ident.split('.')[0];
        const activeState=/active|triggered|detected|delay|delayed|waiting|hold|wind|storm/.test(st);
        if(windEntity && ((domain!=='switch' && domain!=='input_boolean' && activeState) || /triggered|detected|delay|delayed|waiting|hold/.test(st))){
          inferredWind=true; related+=` ${ident} ${friendly}`;
        }
      }
    }
    const text=`${combined} ${related}`;
    const mentionsDelay=/delay|delayed|waiting|weather hold|rain hold/.test(text);
    const interrupted=!!interruption;
    const active=rawActive || latchedActive || mentionsDelay || interrupted;
    if(!active){ el.classList.remove('show'); return; }

    const atBase=/charging|docked|returning|return home|idle/.test(statusText) || /charging|docked|returning|early_return/.test(interruptionText.toLowerCase());
    let icon='mdi:timer-alert-outline';
    let title=atBase ? 'MOWING INTERRUPTED' : 'MOWING DELAYED';
    let message=atBase ? 'Mower returned to base before the mowing task was complete' : 'Mowing is temporarily delayed · waiting for safe conditions';
    if(/rain|raining|shower|precip/.test(text)){
      icon='mdi:weather-pouring';
      title=atBase ? 'RAIN DELAY · AT BASE' : 'MOWING DELAYED';
      message=atBase ? 'Rain interrupted mowing · Navimow returned to the charging base' : 'Rain detected · mowing is temporarily delayed';
    }else if(/frost|freez|ice/.test(text)){
      icon='mdi:snowflake-alert'; message=atBase?'Frost delay · Navimow returned to base':'Frost conditions · mowing is temporarily delayed';
    }else if(/snow/.test(text)){
      icon='mdi:weather-snowy-heavy'; message=atBase?'Snow delay · Navimow returned to base':'Snow conditions · mowing is temporarily delayed';
    }else if(/wind|storm/.test(text) || inferredWind){
      icon='mdi:weather-windy'; message=atBase?'Strong wind delay · Navimow returned to base':'Strong wind detected · mowing is temporarily delayed';
    }else if(/temperature|high temp|heat/.test(text)){
      icon='mdi:thermometer-alert'; message=atBase?'Temperature delay · Navimow returned to base':'Temperature conditions · mowing is temporarily delayed';
    }
    this.querySelector('#delayIcon')?.setAttribute('icon',icon);
    this.querySelector('#delayTitle').textContent=title;
    this.querySelector('#delayText').textContent=message;
    el.classList.add('show');
  }

  _refreshImage(){
    const cam=this._entity(this.config.camera); if(!cam) return;
    const backgroundId=cam.attributes.background_camera_entity_id;
    const imageCam=(backgroundId && this._entity(backgroundId)) || cam;
    let src=imageCam.attributes.entity_picture;
    if(!src) return;
    src += (src.includes('?')?'&':'?') + `_navimow=${Date.now()}`;
    const img=this.querySelector('#mapImage');
    if(img && img.src!==src){
      // Decode off the interaction path where supported, then swap the image.
      const preload=new Image();
      preload.decoding='async'; preload.src=src;
      preload.onload=()=>{if(img) img.src=src; this._lastLiveImage=Date.now(); this._updateLiveBadge();};
    }
  }

  _projectMapPoint(view,x,y){
    if(!view) return null;
    const scale=Number(view.scale), minX=Number(view.min_x), maxY=Number(view.max_y);
    const nx=Number(x), ny=Number(y);
    if(![scale,minX,maxY,nx,ny].every(Number.isFinite)) return null;
    return {x:(nx-minX)*scale,y:(maxY-ny)*scale};
  }

  _ensureDynamicMapElements(){
    const svg=this.querySelector('#overlay'); if(!svg) return null;
    let trail=svg.querySelector('#liveTrailOverlay');
    if(!trail){
      trail=document.createElementNS('http://www.w3.org/2000/svg','path');
      trail.id='liveTrailOverlay'; trail.setAttribute('class','liveTrailOverlay');
      svg.insertBefore(trail,svg.firstChild);
    }
    let mower=svg.querySelector('#smoothMower');
    if(!mower){
      mower=document.createElementNS('http://www.w3.org/2000/svg','g'); mower.id='smoothMower'; mower.setAttribute('class','smoothMower');
      mower.innerHTML='<ellipse cx="0" cy="3" rx="20" ry="16" fill="#000" opacity=".24"></ellipse><path class="smoothMowerBody" d="M -15 -14 H 9 Q 18 -14 20 -5 V 5 Q 18 14 9 14 H -15 Q -20 10 -20 5 V -5 Q -20 -10 -15 -14 Z"></path><path class="smoothMowerCore" d="M -12 -10 H 8 Q 14 -10 15 -4 V 5 Q 14 10 8 10 H -12 Z"></path><circle class="smoothMowerLidar" cx="7" cy="-2" r="8"></circle><circle class="smoothMowerStatus" cx="-10" cy="6" r="3.5"></circle><path class="smoothMowerNose" d="M 20 -5 L 25 0 L 20 5 Z" stroke="#fff" stroke-width="1.5"></path>';
      svg.insertBefore(mower,svg.firstChild?.nextSibling||svg.firstChild);
    }
    return {svg,trail,mower};
  }

  _angleLerp(a,b,t){
    let d=((b-a+540)%360)-180;
    return a+d*t;
  }

  _drawMowerPose(pose){
    const els=this._ensureDynamicMapElements(); if(!els||!pose) return;
    const projected=this._projectMapPoint(this._entity(this.config.camera)?.attributes?.map_view,pose.x,pose.y);
    if(!projected) return;
    els.mower.style.display='';
    els.mower.setAttribute('transform',`translate(${projected.x.toFixed(2)} ${projected.y.toFixed(2)}) rotate(${Number(pose.heading||0).toFixed(2)})`);
  }

  _animateMowerTo(target){
    if(!target) return;
    const now=performance.now();
    const prev=this._mowerAnim?.current||this._mowerAnim?.to||target;
    const wallNow=Date.now();
    const gap=this._lastMowerTargetAt?wallNow-this._lastMowerTargetAt:0;
    this._lastMowerTargetAt=wallNow;
    const duration=gap?Math.max(450,Math.min(2400,gap*1.08)):0;
    this._mowerAnim={from:{...prev},to:{...target},start:now,duration,current:{...prev}};
    if(this._mowerAnimRaf) cancelAnimationFrame(this._mowerAnimRaf);
    const tick=(ts)=>{
      const a=this._mowerAnim; if(!a) return;
      const raw=a.duration?Math.min(1,(ts-a.start)/a.duration):1;
      const t=raw<.5?2*raw*raw:1-Math.pow(-2*raw+2,2)/2;
      const cur={x:a.from.x+(a.to.x-a.from.x)*t,y:a.from.y+(a.to.y-a.from.y)*t,heading:this._angleLerp(Number(a.from.heading||0),Number(a.to.heading||0),t)};
      a.current=cur; this._drawMowerPose(cur);
      if(raw<1) this._mowerAnimRaf=requestAnimationFrame(tick); else {this._mowerAnimRaf=0;a.current={...a.to};}
    };
    this._mowerAnimRaf=requestAnimationFrame(tick);
  }

  _updateDynamicMap(cam){
    const els=this._ensureDynamicMapElements(); if(!els) return;
    // beta.19+ standalone camera frames already contain their own mower and
    // trail. Suppress this card's dynamic copies so exactly one of each is
    // visible. Older camera entities keep the original attribute overlays.
    if(cam?.attributes?.camera_overlays_baked===true && !cam?.attributes?.background_camera_entity_id){
      els.trail.setAttribute('d',''); els.trail.style.display='none';
      els.mower.style.display='none';
      this._lastLiveImage=Date.now();
      return;
    }
    const path=String(cam?.attributes?.live_trail_path||'');
    els.trail.setAttribute('d',path);
    els.trail.style.display=path?'':'none';
    const pose=cam?.attributes?.mower_pose;
    if(pose && Number.isFinite(Number(pose.x)) && Number.isFinite(Number(pose.y))){
      const target={x:Number(pose.x),y:Number(pose.y),heading:Number(pose.heading)||0};
      const sig=`${target.x.toFixed(4)}:${target.y.toFixed(4)}:${target.heading.toFixed(1)}`;
      if(sig!==this._lastMowerPoseSignature){this._lastMowerPoseSignature=sig;this._animateMowerTo(target);}
      this._lastLiveImage=Date.now();
    }else{
      els.mower.style.display='none';
    }
  }

  _updateLiveBadge(){
    const badge=this.querySelector('#liveBadge'), text=this.querySelector('#liveText'); if(!badge||!text) return;
    const age=this._lastLiveImage ? Math.max(0,Math.round((Date.now()-this._lastLiveImage)/1000)) : null;
    const stale=age===null || age>8; badge.classList.toggle('stale',stale);
    text.textContent=age===null?'LIVE · waiting':`LIVE · ${age}s ago`;
  }

  _renderZones(cam){
    const zones=Array.isArray(cam.attributes.selectable_zones)?cam.attributes.selectable_zones:[];
    const view=cam.attributes.map_view;
    if(!view||!zones.length) return;
    const sig=JSON.stringify([view,zones]); if(sig===this._lastMapSignature) return; this._lastMapSignature=sig;
    this._zones=zones;
    const svg=this.querySelector('#overlay');
    const width=Number(view.width), height=Number(view.height), scale=Number(view.scale), minX=Number(view.min_x), maxY=Number(view.max_y);
    if(![width,height,scale,minX,maxY].every(Number.isFinite)) return;
    svg.setAttribute('viewBox',`0 0 ${width} ${height}`);
    svg.setAttribute('preserveAspectRatio','xMidYMid meet');
    svg.innerHTML='';
    const px=x=>(Number(x)-minX)*scale, py=y=>(maxY-Number(y))*scale;
    const polygonCentroid=(points)=>{
      const pts=(points||[]).map(p=>[Number(p[0]),Number(p[1])]).filter(p=>Number.isFinite(p[0])&&Number.isFinite(p[1]));
      if(!pts.length) return [0,0];
      if(pts.length<3){
        return [pts.reduce((s,p)=>s+p[0],0)/pts.length,pts.reduce((s,p)=>s+p[1],0)/pts.length];
      }
      let twiceArea=0,cx=0,cy=0;
      for(let i=0;i<pts.length;i++){
        const a=pts[i],b=pts[(i+1)%pts.length];
        const cross=a[0]*b[1]-b[0]*a[1];
        twiceArea+=cross; cx+=(a[0]+b[0])*cross; cy+=(a[1]+b[1])*cross;
      }
      if(Math.abs(twiceArea)<1e-9){
        return [pts.reduce((s,p)=>s+p[0],0)/pts.length,pts.reduce((s,p)=>s+p[1],0)/pts.length];
      }
      return [cx/(3*twiceArea),cy/(3*twiceArea)];
    };
    zones.forEach(z=>{
      const pts=(z.points||[]).map(p=>`${px(p[0]).toFixed(1)},${py(p[1]).toFixed(1)}`).join(' ');
      if(!pts) return;
      const poly=document.createElementNS('http://www.w3.org/2000/svg','polygon'); poly.setAttribute('points',pts); poly.classList.add('zone'); poly.dataset.id=String(z.id); poly.setAttribute('tabindex','0'); poly.setAttribute('role','button'); poly.setAttribute('aria-label',`Select ${z.name}`);
      const toggle=()=>{const id=Number(z.id);this._selected.has(id)?this._selected.delete(id):this._selected.add(id);this._paintSelection();};
      poly.addEventListener('click',toggle); poly.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();toggle();}}); svg.appendChild(poly);
      const [cx,cy]=polygonCentroid(z.points||[]);
      const lx=px(cx), ly=py(cy);
      const group=document.createElementNS('http://www.w3.org/2000/svg','g'); group.setAttribute('class','zoneLabelGroup');
      const name=String(z.name||`Zone ${z.id}`); const boxW=Math.max(82,Math.min(290,name.length*13+34)), boxH=52;
      const box=document.createElementNS('http://www.w3.org/2000/svg','rect'); box.setAttribute('x',(lx-boxW/2).toFixed(1)); box.setAttribute('y',(ly-boxH/2).toFixed(1)); box.setAttribute('width',boxW.toFixed(1)); box.setAttribute('height',boxH); box.setAttribute('rx','15'); box.setAttribute('class','zoneLabelBox'); group.appendChild(box);
      const label=document.createElementNS('http://www.w3.org/2000/svg','text'); label.setAttribute('x',lx); label.setAttribute('y',ly-5); label.setAttribute('class','zoneNameLabel'); label.setAttribute('dominant-baseline','middle'); label.textContent=name; group.appendChild(label);
      const idLabel=document.createElementNS('http://www.w3.org/2000/svg','text'); idLabel.setAttribute('x',lx); idLabel.setAttribute('y',ly+14); idLabel.setAttribute('class','zoneIdLabel'); idLabel.setAttribute('dominant-baseline','middle'); idLabel.textContent=`ZONE ${z.id}`; group.appendChild(idLabel);
      svg.appendChild(group);
    });
  }

  _toggleMapFullscreen(force){
    const wrap=this.querySelector('#mapWrap');
    const btn=this.querySelector('#mapExpand');
    if(!wrap||!btn) return;
    const next=typeof force==='boolean'?force:!wrap.classList.contains('fullscreen');
    wrap.classList.toggle('fullscreen',next);
    btn.setAttribute('aria-label',next?'Exit full screen map':'Open full screen map');
    btn.setAttribute('title',next?'Exit full screen map':'Full screen map');
    btn.querySelector('ha-icon')?.setAttribute('icon',next?'mdi:fullscreen-exit':'mdi:fullscreen');
    // Repaint after the viewport geometry changes so SVG labels/polygons line
    // up perfectly with the contain-scaled camera image on phone/tablet/laptop.
    requestAnimationFrame(()=>{
      this._lastMapSignature='';
      const cam=this._entity(this.config.camera);
      if(!cam) return;
      this._renderZones(cam);
      // _renderZones rebuilds the SVG, including the live mower/trail layers.
      // Force the unchanged docked pose to be drawn again after fullscreen
      // geometry changes instead of leaving the new mower group at SVG 0,0.
      this._lastMowerPoseSignature='';
      this._updateDynamicMap(cam);
    });
  }

  _navigateHome(){
    const path=this.config.home_path||'/the-551/The551';
    if(window.location.pathname===path) return;
    window.history.pushState(null,'',path);
    window.dispatchEvent(new Event('location-changed'));
  }

  _selectAll(){ (this._zones||[]).forEach(z=>this._selected.add(Number(z.id))); this._paintSelection(); }
  _paintSelection(){
    this.querySelectorAll('.zone').forEach(p=>p.classList.toggle('selected',this._selected.has(Number(p.dataset.id))));
    const zones=this._zones||[]; const selected=zones.filter(z=>this._selected.has(Number(z.id)));
    const chips=this.querySelector('#chips'); chips.innerHTML=selected.length?selected.map(z=>`<span class="chip">${this._escape(z.name)}</span>`).join(''):'<span class="empty">Tap a zone on the map.</span>';
    const title=this.querySelector('#selectedTitle'); title.textContent=selected.length?`${selected.length} ZONE${selected.length===1?'':'S'} SELECTED`:'SELECTED AREAS';
    const fs=this.querySelector('#fullscreenSelection'); if(fs) fs.textContent=selected.length?`${selected.length} zone${selected.length===1?'':'s'} selected`:'No zones selected';
    const btn=this.querySelector('#mowBtn');
    const canLaunch=!!this._operatingState?.atBase && !this._commandBusy;
    btn.disabled=!selected.length||!canLaunch;
    btn.textContent=this._commandBusy?'STARTING…':(selected.length===1?`▶ MOW ${selected[0].name.toUpperCase()}`:`▶ MOW ${selected.length} SELECTED ZONES`);
    const coverage=this._entity(this.config.coverage); const cz=coverage?.attributes?.zones||[]; const area=selected.reduce((sum,z)=>{const m=cz.find(x=>Number(x.id)===Number(z.id));return sum+(m?Number(m.area)||0:0)},0);
    this.querySelector('#stateSub').textContent=selected.length?`${selected.map(z=>z.name).join(' + ')}${area?` · ${this._fmtArea(area*10.76391041671)}`:''}`:'Select the areas you want to mow';
    const cam=this._entity(this.config.camera);
    if(cam) this._updateResumeControls(cam);
  }

  _updateResumeControls(cam){
    const host=this.querySelector('#resumeInline');
    if(!host) return;
    const progress=cam?.attributes?.zone_progress||{};
    const selected=(this._zones||[]).filter(z=>this._selected.has(Number(z.id)));
    const partial=selected.map(z=>({z,pct:Number(progress[String(z.id)]??progress[z.id]??0)})).filter(x=>Number.isFinite(x.pct)&&x.pct>0&&x.pct<100);
    // Resume/Start Fresh is a *job launch* choice. While the mower is actively
    // mowing, paused, or returning, use PAUSE/RESUME/RETURN HOME instead.
    if(!this._operatingState?.atBase || !partial.length){
      host.classList.remove('show');
      this.querySelector('#mowBtn').style.display='';
      return;
    }
    const summary=partial.map(x=>`${x.z.name}: ${Math.round(x.pct)}%`).join(' · ');
    this.querySelector('#resumeInlineText').textContent=`Unfinished mowing available · ${summary}`;
    this.querySelector('#resumeInlineBtn').textContent=partial.length===1?`▶ RESUME ${Math.round(partial[0].pct)}% WORK`:'▶ RESUME UNFINISHED WORK';
    host.classList.add('show');
    const disabled=!!this._commandBusy;
    this.querySelector('#resumeInlineBtn').disabled=disabled;
    this.querySelector('#freshInlineBtn').disabled=disabled;
    // The two explicit choices replace the ambiguous generic MOW button.
    this.querySelector('#mowBtn').style.display='none';
  }


  _configurationEntities(){
    const match=String(this.config.settings_match||'').toLowerCase();
    const allowedDomains=new Set(['number','select','switch','text']);
    return Object.values(this._hass?.states||{}).filter(entity=>{
      const [domain]=entity.entity_id.split('.');
      if(!allowedDomains.has(domain)) return false;
      if(this._deviceEntityIds?.size ? !this._deviceEntityIds.has(entity.entity_id) : (match && !entity.entity_id.toLowerCase().includes(match))) return false;
      // Prepared mowing zone is an operating control, not an EntityCategory.CONFIG entity.
      if(entity.entity_id.includes('prepared_mowing_zone')) return false;
      return true;
    }).sort((a,b)=>this._settingLabel(a).localeCompare(this._settingLabel(b)));
  }
  _settingLabel(entity){
    let name=entity?.attributes?.friendly_name||entity?.entity_id||'Setting';
    const mowerName=this._mowerDisplayName();
    for(const prefix of [`Outdoor ${mowerName}`,mowerName,'Outdoor']){
      if(prefix && name.toLowerCase().startsWith(prefix.toLowerCase())) name=name.slice(prefix.length).trim();
    }
    return name||'Setting';
  }
  _settingGroup(entity){
    const t=`${this._settingLabel(entity)} ${entity.entity_id}`.toLowerCase();
    if(/zone.*name/.test(t)) return 'Zone names';
    if(/rain|frost|snow|wind|temperature|weather|animal/.test(t)) return 'Weather & environment';
    if(/battery|charging|power/.test(t)) return 'Battery & power';
    if(/light|sound|volume/.test(t)) return 'Lighting & sound';
    if(/lock|alarm|obstacle|traction|camera|efls|lift/.test(t)) return 'Safety & navigation';
    return 'Mowing';
  }
  _settingValue(entity){
    if(!entity) return '—';
    const domain=entity.entity_id.split('.')[0];
    if(domain==='switch') return entity.state==='on'?'ON':'OFF';
    if(domain==='number'){
      const unit=entity.attributes.unit_of_measurement||'';
      const n=Number(entity.state);
      return `${Number.isFinite(n)?n.toLocaleString():this._prettyState(entity.state)}${unit?` ${unit}`:''}`;
    }
    return this._prettyState(entity.state);
  }
  _findScheduleEntity(){
    if(this.config.schedule && this._entity(this.config.schedule)) return this.config.schedule;
    const states=this._hass?.states||{};
    const match=Object.keys(states).find(id=>{
      if(!id.startsWith('sensor.')||!id.endsWith('_schedule')) return false;
      const st=states[id];
      return Array.isArray(st?.attributes?.days) && Array.isArray(st?.attributes?.zones);
    });
    return match||null;
  }

  async _openScheduler(){
    const overlay=this.querySelector('#schedulerOverlay');
    if(!overlay) return;
    overlay.classList.add('open');
    overlay.setAttribute('aria-hidden','false');
    const host=this.querySelector('#schedulerHost');
    if(host && !this._schedulerBuilt){
      host.innerHTML='<div class="schedulerMissing">Loading mowing schedule…</div>';
    }
    try{
      await this._ensureSchedulerCardLoaded();
      if(!this._schedulerBuilt) this._buildScheduler();
      const scheduler=this.querySelector('#schedulerHost navimow-scheduler-card');
      if(scheduler) scheduler.hass=this._hass;
    }catch(err){
      console.error('[Navimow] Unable to load scheduler card',err);
      if(host) host.innerHTML='<div class="schedulerMissing">Unable to load the schedule editor. Close and reopen Schedule, or hard-refresh once.</div>';
    }
  }

  _ensureSchedulerCardLoaded(){
    if(customElements.get('navimow-scheduler-card')) return Promise.resolve();
    if(this._schedulerLoadPromise) return this._schedulerLoadPromise;
    const url='/local/navimow_ha_pro/navimow-scheduler-card.js?v=0620';
    this._schedulerLoadPromise=import(url).then(()=>customElements.whenDefined('navimow-scheduler-card')).catch(err=>{
      this._schedulerLoadPromise=null;
      throw err;
    });
    return this._schedulerLoadPromise;
  }

  _closeScheduler(){
    const overlay=this.querySelector('#schedulerOverlay');
    if(!overlay) return;
    overlay.classList.remove('open');
    overlay.setAttribute('aria-hidden','true');
  }

  _buildScheduler(){
    const host=this.querySelector('#schedulerHost'); if(!host) return;
    const entity=this._findScheduleEntity();
    if(!entity){
      host.innerHTML='<div class="schedulerMissing">Schedule sensor not found. Restart Home Assistant after installing this version and try again.</div>';
      return;
    }
    if(!customElements.get('navimow-scheduler-card')){
      host.innerHTML='<div class="schedulerMissing">Loading mowing schedule…</div>';
      this._schedulerBuilt=false;
      return;
    }
    host.innerHTML='';
    const card=document.createElement('navimow-scheduler-card');
    card.setConfig({entity,title:''});
    card.hass=this._hass;
    host.appendChild(card);
    this._schedulerBuilt=true;
  }

  _openSettings(){
    this.querySelector('#modePanel')?.classList.remove('open');
    this.querySelector('#heightPanel')?.classList.remove('open');
    const overlay=this.querySelector('#settingsOverlay');
    overlay.classList.add('open'); overlay.setAttribute('aria-hidden','false');
    if(!this._settingsBuilt) this._renderSettings(); else this._updateSettingsControls();
  }
  _closeSettings(){
    const overlay=this.querySelector('#settingsOverlay');
    overlay.classList.remove('open'); overlay.setAttribute('aria-hidden','true');
  }
  _renderSettings(){
    const root=this.querySelector('#settingsContent'); if(!root) return;
    const entities=this._configurationEntities();
    const appearanceHtml=`<section class="settingsGroup"><h3 class="settingsGroupTitle">Appearance</h3><div class="settingsList"><div class="settingRow switchRow"><span class="settingIcon"><ha-icon icon="mdi:theme-light-dark"></ha-icon></span><span><span class="settingName">Dark mode</span><span class="settingEntity">Saved on this browser</span></span><span class="toggleWrap"><span class="toggleState ${this._darkMode?'on':''}">${this._darkMode?'ON':'OFF'}</span><button class="toggleSwitch ${this._darkMode?'on':''}" type="button" data-dark-mode aria-pressed="${this._darkMode?'true':'false'}" aria-label="Toggle dark mode"><span class="toggleKnob"></span></button></span></div><div class="settingRow switchRow"><span class="settingIcon"><ha-icon icon="mdi:ruler"></ha-icon></span><span><span class="settingName">Measurement units</span><span class="settingEntity">Automatic follows Home Assistant</span></span><select class="unitSelect" data-height-units aria-label="Measurement units"><option value="auto" ${this._heightUnits==='auto'?'selected':''}>Automatic</option><option value="metric" ${this._heightUnits==='metric'?'selected':''}>Metric</option><option value="imperial" ${this._heightUnits==='imperial'?'selected':''}>Imperial</option></select></div></div></section>`;
    if(!entities.length){
      root.innerHTML=appearanceHtml+'<div class="settingsEmpty">No mower configuration entities were found.</div>';
      this._settingsBuilt=true;
      root.querySelector('[data-dark-mode]')?.addEventListener('click',()=>this._setDarkMode(!this._darkMode));
      root.querySelector('[data-height-units]')?.addEventListener('change',e=>this._setHeightUnits(e.target.value));
      return;
    }
    const order=['Mowing','Weather & environment','Safety & navigation','Battery & power','Lighting & sound','Zone names'];
    const groups=new Map(order.map(x=>[x,[]]));
    entities.forEach(e=>{const g=this._settingGroup(e);if(!groups.has(g))groups.set(g,[]);groups.get(g).push(e);});
    const rowHtml=e=>{
      const domain=e.entity_id.split('.')[0];
      const icon=e.attributes.icon||({switch:'mdi:toggle-switch',number:'mdi:tune-variant',select:'mdi:format-list-bulleted',text:'mdi:form-textbox'}[domain]||'mdi:cog');
      const label=this._escape(this._settingLabel(e));
      const eid=this._escape(e.entity_id);
      if(domain==='switch'){
        const on=e.state==='on';
        return `<div class="settingRow switchRow" data-entity="${eid}"><span class="settingIcon"><ha-icon icon="${this._escape(icon)}"></ha-icon></span><span><span class="settingName">${label}</span><span class="settingEntity">${eid}</span></span><span class="toggleWrap"><span class="toggleState ${on?'on':''}">${on?'ON':'OFF'}</span><button class="toggleSwitch ${on?'on':''}" type="button" data-switch="${eid}" aria-pressed="${on?'true':'false'}" aria-label="Toggle ${label}"><span class="toggleKnob"></span></button></span></div>`;
      }
      if(domain==='number'){
        const isCutHeight=e.entity_id===this.config.cutting_height || e.entity_id.includes('cutting_height');
        const nativeUnit=e.attributes.unit_of_measurement||'';
        const rawNative=Number(e.state);
        const raw=isCutHeight?this._heightFromNative(rawNative,e):rawNative;
        const value=Number.isFinite(raw)?raw:0;
        const aminNative=Number(e.attributes.min), amaxNative=Number(e.attributes.max), astepNative=Number(e.attributes.step);
        const amin=isCutHeight?this._heightFromNative(aminNative,e):aminNative;
        const amax=isCutHeight?this._heightFromNative(amaxNative,e):amaxNative;
        const astep=isCutHeight?(this._usesImperialHeight()?5/25.4:5):astepNative;
        const min=Number.isFinite(amin)?amin:0; const max=Number.isFinite(amax)?amax:100;
        const backendStep=Number.isFinite(astep)&&astep>0?astep:1;
        const unit=isCutHeight?this._heightUnit():nativeUnit;
        const isWholePercent=(unit==='%' && (/charging_limit|return.*dock.*battery|return_battery/.test(e.entity_id) || /charging limit|return-to-dock battery/i.test(this._settingLabel(e))));
        const span=Math.max(max-min,1);
        const visualStep=isWholePercent?1:(isCutHeight?backendStep:Math.max(span/1000,Math.min(backendStep/10,0.1)));
        const shown=Number.isFinite(raw)?`${raw.toLocaleString(undefined,{maximumFractionDigits:isCutHeight?1:2})}${unit?` ${this._escape(unit)}`:''}`:'—';
        const pct=max>min?Math.max(0,Math.min(100,(value-min)/(max-min)*100)):0;
        return `<div class="settingRow numberRow" data-entity="${eid}"><span class="settingIcon"><ha-icon icon="${this._escape(icon)}"></ha-icon></span><span class="numberSetting"><span class="numberHead"><span><span class="settingName">${label}</span><span class="settingEntity">${eid}</span></span><span class="numberCurrent" data-number-value="${eid}">${shown}</span></span><input class="settingSlider" style="--pct:${pct}%" type="range" data-number="${eid}" min="${min}" max="${max}" step="${visualStep}" value="${value}" aria-label="${label}"><span class="settingScale"><span>${min.toLocaleString(undefined,{maximumFractionDigits:isCutHeight?1:2})}${unit?` ${this._escape(unit)}`:''}</span><span>${max.toLocaleString(undefined,{maximumFractionDigits:isCutHeight?1:2})}${unit?` ${this._escape(unit)}`:''}</span></span></span></div>`;
      }
      const value=this._settingValue(e);
      return `<button class="settingRow" data-entity="${eid}"><span class="settingIcon"><ha-icon icon="${this._escape(icon)}"></ha-icon></span><span><span class="settingName">${label}</span><span class="settingEntity">${eid}</span></span><span class="settingValue"><span>${this._escape(value)}</span><ha-icon icon="mdi:chevron-right"></ha-icon></span></button>`;
    };
    root.innerHTML=appearanceHtml+[...groups.entries()].filter(([,items])=>items.length).map(([group,items])=>`<section class="settingsGroup"><h3 class="settingsGroupTitle">${this._escape(group)}</h3><div class="settingsList">${items.map(rowHtml).join('')}</div></section>`).join('');
    this._settingsBuilt=true;
    root.querySelector('[data-dark-mode]')?.addEventListener('click',()=>this._setDarkMode(!this._darkMode));
    root.querySelector('[data-height-units]')?.addEventListener('change',e=>this._setHeightUnits(e.target.value));
    root.querySelectorAll('.settingRow:not(.switchRow):not(.numberRow)').forEach(row=>row.addEventListener('click',()=>this._showMoreInfo(row.dataset.entity)));
    root.querySelectorAll('.toggleSwitch[data-switch]').forEach(btn=>btn.addEventListener('click',async e=>{
      e.stopPropagation(); const entityId=btn.dataset.switch; const current=btn.classList.contains('on'); const next=!current;
      btn.classList.toggle('on',next); btn.setAttribute('aria-pressed',next?'true':'false');
      const state=btn.closest('.toggleWrap')?.querySelector('.toggleState'); if(state){state.textContent=next?'ON':'OFF';state.classList.toggle('on',next);}
      this._pendingSettings.set(entityId,{type:'switch',value:next?'on':'off',expires:Date.now()+15000});
      try{await this._service('switch',next?'turn_on':'turn_off',{entity_id:entityId});}
      catch(_e){
        this._pendingSettings.delete(entityId);
        btn.classList.toggle('on',current); btn.setAttribute('aria-pressed',current?'true':'false');
        if(state){state.textContent=current?'ON':'OFF';state.classList.toggle('on',current);}
      }
    }));
    root.querySelectorAll('.settingSlider').forEach(slider=>{
      const entityId=slider.dataset.number; let raf=0;
      const preview=()=>{
        raf=0; this._paintSettingSlider(slider);
        const entity=this._entity(entityId); const isCutHeight=entityId===this.config.cutting_height || entityId.includes('cutting_height'); const unit=isCutHeight?this._heightUnit():(entity?.attributes?.unit_of_measurement||''); const n=Number(slider.value);
        const wholePct=unit==='%' && (/charging_limit|return.*dock.*battery|return_battery/.test(entityId)); const out=root.querySelector(`[data-number-value="${CSS.escape(entityId)}"]`); if(out) out.textContent=`${Number.isFinite(n)?(wholePct?Math.round(n).toLocaleString():n.toLocaleString(undefined,{maximumFractionDigits:2})):'—'}${unit?` ${unit}`:''}`;
      };
      slider.addEventListener('pointerdown',()=>{slider.dataset.dragging='1';});
      slider.addEventListener('input',()=>{if(!raf) raf=requestAnimationFrame(preview);});
      const commit=async()=>{
        slider.dataset.dragging='0'; if(raf){cancelAnimationFrame(raf);raf=0;} preview();
        let n=Number(slider.value); if(!Number.isFinite(n)) return;
        const entity=this._entity(entityId); const isCutHeight=entityId===this.config.cutting_height || entityId.includes('cutting_height');
        const nativeMin=Number(entity?.attributes?.min), nativeMax=Number(entity?.attributes?.max), astep=Number(entity?.attributes?.step);
        const amin=isCutHeight?this._heightFromNative(nativeMin,entity):nativeMin, amax=isCutHeight?this._heightFromNative(nativeMax,entity):nativeMax;
        const unit=isCutHeight?this._heightUnit():(entity?.attributes?.unit_of_measurement||''); const isWholePercent=(unit==='%' && (/charging_limit|return.*dock.*battery|return_battery/.test(entityId) || /charging limit|return-to-dock battery/i.test(this._settingLabel(entity||{}))));
        if(isWholePercent){n=Math.round(n);}
        else if(!isCutHeight && Number.isFinite(astep)&&astep>0 && Number.isFinite(amin)){n=amin+Math.round((n-amin)/astep)*astep;}
        if(Number.isFinite(amin)) n=Math.max(amin,n); if(Number.isFinite(amax)) n=Math.min(amax,n);
        const target=Number(n.toFixed(3));
        const serviceTarget=isCutHeight?this._heightToNative(n,entity):target;
        this._pendingSettings.set(entityId,{type:'number',value:target,expires:Date.now()+45000});
        slider.value=String(target); preview();
        try{await this._service('number','set_value',{entity_id:entityId,value:serviceTarget});}
        catch(_e){this._pendingSettings.delete(entityId); this._updateSettingsControls();}
      };
      slider.addEventListener('change',commit);
      slider.addEventListener('pointerup',()=>{slider.dataset.dragging='0';});
      this._paintSettingSlider(slider);
    });
  }
  _updateSettingsControls(){
    const root=this.querySelector('#settingsContent'); if(!root) return;
    const now=Date.now();
    const darkBtn=root.querySelector('[data-dark-mode]');
    if(darkBtn){
      darkBtn.classList.toggle('on',this._darkMode);
      darkBtn.setAttribute('aria-pressed',this._darkMode?'true':'false');
      const darkState=darkBtn.closest('.toggleWrap')?.querySelector('.toggleState');
      if(darkState){darkState.textContent=this._darkMode?'ON':'OFF';darkState.classList.toggle('on',this._darkMode);}
    }
    root.querySelectorAll('.toggleSwitch[data-switch]').forEach(btn=>{
      const entityId=btn.dataset.switch; const e=this._entity(entityId); if(!e) return;
      const pending=this._pendingSettings.get(entityId);
      let on=e.state==='on';
      if(pending?.type==='switch'){
        if(e.state===pending.value){this._pendingSettings.delete(entityId); on=e.state==='on';}
        else if(now<pending.expires){on=pending.value==='on';}
        else{this._pendingSettings.delete(entityId);}
      }
      btn.classList.toggle('on',on); btn.setAttribute('aria-pressed',on?'true':'false');
      const state=btn.closest('.toggleWrap')?.querySelector('.toggleState'); if(state){state.textContent=on?'ON':'OFF';state.classList.toggle('on',on);}
    });
    root.querySelectorAll('.settingSlider[data-number]').forEach(slider=>{
      const entityId=slider.dataset.number; const e=this._entity(entityId); if(!e) return;
      const isCutHeight=entityId===this.config.cutting_height || entityId.includes('cutting_height');
      const actual=isCutHeight?this._heightFromNative(e.state,e):Number(e.state); if(!Number.isFinite(actual)) return;
      const pending=this._pendingSettings.get(entityId);
      let value=actual;
      if(pending?.type==='number'){
        const tolerance=isCutHeight?(this._usesImperialHeight()?0.06:0.1):0.01;
        if(Math.abs(actual-Number(pending.value))<=tolerance){this._pendingSettings.delete(entityId); value=actual;}
        else if(now<pending.expires){value=Number(pending.value);}
        else{this._pendingSettings.delete(entityId);}
      }
      if(slider.dataset.dragging!=='1') slider.value=String(value);
      this._paintSettingSlider(slider);
      if(slider.dataset.dragging!=='1'){
        const unit=isCutHeight?this._heightUnit():(e.attributes.unit_of_measurement||''); const out=root.querySelector(`[data-number-value="${CSS.escape(entityId)}"]`);
        if(out) out.textContent=`${value.toLocaleString()}${unit?` ${unit}`:''}`;
      }
    });
  }
  _paintSettingSlider(slider){
    const min=Number(slider.min), max=Number(slider.max), value=Number(slider.value);
    const pct=Number.isFinite(min)&&Number.isFinite(max)&&max>min&&Number.isFinite(value)?Math.max(0,Math.min(100,(value-min)/(max-min)*100)):0;
    slider.style.setProperty('--pct',`${pct}%`);
  }

  _showMoreInfo(entityId){
    if(!entityId) return;
    const ev=new CustomEvent('hass-more-info',{detail:{entityId},bubbles:true,composed:true});
    this.dispatchEvent(ev);
  }

  _toggleModePanel(){
    const panel=this.querySelector('#modePanel');
    panel.classList.toggle('open');
    if(panel.classList.contains('open')) this.querySelector('#heightPanel').classList.remove('open');
  }
  async _setWorkMode(option){
    if(!option) return;
    await this._service('select','select_option',{entity_id:this.config.work_mode,option});
  }

  _toggleHeightPanel(){
    this.querySelector('#modePanel').classList.remove('open');
    const panel=this.querySelector('#heightPanel');
    panel.classList.toggle('open');
  }
  _heightSliderDisplayedValue(slider){
    const values=String(slider?.dataset?.heightValues||'').split(',').map(Number).filter(Number.isFinite);
    const index=Math.max(0,Math.min(values.length-1,Math.round(Number(slider?.value)||0)));
    return values[index] ?? Number(slider?.value);
  }
  _previewHeight(raw){
    const n=Number(raw); if(!Number.isFinite(n)) return;
    const unit=this._heightUnit();
    this.querySelector('#heightValue').textContent=`${n.toFixed(1)} ${unit}`;
    this.querySelector('#height').textContent=`${n.toFixed(1)} ${unit}`;
  }
  async _setHeight(raw){
    const n=Number(raw); if(!Number.isFinite(n)) return;
    const target=Number(n.toFixed(1));
    const entity=this._entity(this.config.cutting_height);
    const nativeTarget=this._heightToNative(n,entity);
    this._pendingSettings.set(this.config.cutting_height,{type:'number',value:target,expires:Date.now()+45000});
    this._previewHeight(target);
    const slider=this.querySelector('#heightSlider');
    if(slider){
      const values=String(slider.dataset.heightValues||'').split(',').map(Number).filter(Number.isFinite);
      let closest=0;
      values.forEach((choice,index)=>{if(Math.abs(choice-target)<Math.abs(values[closest]-target))closest=index;});
      slider.value=String(closest);
      this._paintSettingSlider(slider);
    }
    await this._service('number','set_value',{entity_id:this.config.cutting_height,value:nativeTarget});
  }

  async _mow(){
    const zones=[...this._selected]; if(!zones.length)return;
    const cam=this._entity(this.config.camera);
    const progress=cam?.attributes?.zone_progress||{};
    const partial=zones.map(id=>({id,pct:Number(progress[String(id)]??progress[id]??0)})).filter(x=>Number.isFinite(x.pct)&&x.pct>0&&x.pct<100);
    if(!partial.length){ await this._confirmMow(true,zones); return; }
    this._pendingMowZones=zones;
    const names=(this._zones||[]).filter(z=>zones.includes(Number(z.id)));
    const pieces=partial.map(x=>{const z=names.find(n=>Number(n.id)===x.id);return `${z?.name||`Zone ${x.id}`}: ${Math.round(x.pct)}% complete`;});
    this.querySelector('#resumeText').textContent=partial.length===1?'This zone already has mowing progress. Continue from where it stopped, or erase that progress and begin again.':'One or more selected zones already have mowing progress. Continue the unfinished work, or erase progress and begin all selected zones again.';
    this.querySelector('#resumeProgress').textContent=pieces.join(' · ');
    const overlay=this.querySelector('#resumeOverlay'); overlay.classList.add('open'); overlay.setAttribute('aria-hidden','false');
  }
  _closeResumeDialog(){ const overlay=this.querySelector('#resumeOverlay'); if(overlay){overlay.classList.remove('open');overlay.setAttribute('aria-hidden','true');} this._pendingMowZones=[]; }
  async _confirmMow(reset,zones=null){
    const chosen=zones||this._pendingMowZones||[]; if(!chosen.length)return;
    if(!this._operatingState?.atBase){
      const err=this.querySelector('#error');
      err.textContent='Finish the current action first. While paused, use RESUME; use zone Resume / Start Fresh after the mower returns to the dock.';
      err.style.display='block';
      return;
    }
    this._closeResumeDialog();
    this._commandBusy=true;
    this._paintSelection();
    const ok=await this._service('navimow_ha_pro','mow',{zones:chosen,reset});
    if(!ok){this._commandBusy=false;this._paintSelection();}
  }
  async _togglePauseResume(){
    if(this._operatingState?.paused){
      await this._service('lawn_mower','start_mowing',{entity_id:this.config.mower});
    }else if(this._operatingState?.mowing){
      await this._service('lawn_mower','pause',{entity_id:this.config.mower});
    }
  }
  async _service(domain,service,data){
    const err=this.querySelector('#error'); err.style.display='none';
    try{await this._hass.callService(domain,service,data);return true;}catch(e){err.textContent=e?.message||String(e);err.style.display='block';return false;}
  }
  _escape(v){ const d=document.createElement('div');d.textContent=String(v??'');return d.innerHTML; }
}
if(!customElements.get('navimow-zone-dashboard-card')) customElements.define('navimow-zone-dashboard-card',NavimowZoneDashboardCard);
window.customCards=window.customCards||[];
window.customCards.push({type:'navimow-zone-dashboard-card',name:'Navimow Zone Dashboard',description:'Responsive Navimow dashboard with direct multi-zone map selection.'});
