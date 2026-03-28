class TariffSaverHeatingCard extends HTMLElement {
  set hass(hass) {
    this._hass = hass;
    const state = hass.states[this._configEntity];
    const key = state ? state.last_updated : '';
    if (key !== this._lastKey && !this._editing) {
      this._lastKey = key;
      this._render();
    }
  }

  setConfig(config) {
    this._config = config;
    this._configEntity = config.config_entity || 'sensor.tariff_saver_consumer_config';
    this._lastKey = '';
    this._editing = false;
  }

  _getSettings() {
    if (!this._hass) return {};
    const state = this._hass.states[this._configEntity];
    if (!state || !state.attributes || !state.attributes.general) return {};
    return state.attributes.general;
  }

  async _save(key, value) {
    if (!this._hass) return;
    try {
      await this._hass.callService('tariff_saver', 'update_setting', { key, value });
    } catch (e) {
      alert('Speichern fehlgeschlagen: ' + e.message);
    }
  }

  _validateEntity(entityId) {
    if (!entityId || !entityId.trim()) return null;
    entityId = entityId.trim();
    if (!entityId.includes('.')) {
      // Try to find a matching entity
      const domains = ['select', 'switch', 'climate', 'sensor', 'input_select', 'input_number', 'input_boolean'];
      const matches = domains.filter(d => this._hass.states[d + '.' + entityId]);
      if (matches.length === 1) {
        return { valid: false, suggestion: matches[0] + '.' + entityId, msg: 'Domain fehlt. Meinst du ' + matches[0] + '.' + entityId + '?' };
      } else if (matches.length > 1) {
        return { valid: false, suggestion: null, msg: 'Domain fehlt. Gefunden: ' + matches.map(d => d + '.' + entityId).join(', ') };
      }
      return { valid: false, suggestion: null, msg: 'Entity nicht gefunden. Domain fehlt (z.B. sensor., select.)' };
    }
    if (!this._hass.states[entityId]) {
      return { valid: false, suggestion: null, msg: 'Entity nicht gefunden in HA' };
    }
    return { valid: true };
  }

  _getAvgTemp(s) {
    if (!this._hass) return null;
    const sensors = [
      s.heating_temp_sensor_1,
      s.heating_temp_sensor_2,
      s.heating_temp_sensor_3,
    ].filter(e => e && e.trim());
    const temps = [];
    for (const entity of sensors) {
      const st = this._hass.states[entity];
      if (st && !['unavailable','unknown',''].includes(st.state)) {
        const v = parseFloat(st.state);
        if (!isNaN(v)) temps.push(v);
      }
    }
    if (!temps.length) return null;
    return { avg: (temps.reduce((a,b) => a+b, 0) / temps.length).toFixed(1), count: temps.length, values: temps };
  }

  _render() {
    if (!this._hass) return;
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });

    const s = this._getSettings();
    const wpEntity = s.heating_wp_entity || '';
    const wpState = wpEntity ? (this._hass.states[wpEntity] || {}).state || '?' : '';
    const comfort = parseFloat(s.heating_comfort_min) || 21;
    const pvmax = parseFloat(s.heating_pv_max) || 23;
    const seasons = s.heating_seasons || 'Frühling,Herbst';
    const bl = this._hass.states['sensor.tariff_saver_base_load_daily'];
    const currentSeason = bl && bl.attributes ? bl.attributes.current_season || '-' : '-';
    const seasonActive = seasons.split(',').map(x=>x.trim()).includes(currentSeason);
    const temp = this._getAvgTemp(s);

    const heatingEnabled = !!s.heating_enabled;
    const wpHeatValue = s.heating_wp_heat_value || 'Heizung & Warmwasser';
    const wpOffValue = s.heating_wp_off_value || 'Aus';
    const isHeating = wpState === wpHeatValue;
    const isOff = wpState === wpOffValue;

    // Get PV surplus
    const pvEntity = s.heating_pv_surplus_entity || '';
    let pvSurplus = 0;
    if (pvEntity) {
      const pvState = this._hass.states[pvEntity];
      if (pvState && !['unavailable','unknown'].includes(pvState.state)) {
        const v = parseFloat(pvState.state);
        pvSurplus = v > 100 ? v / 1000 : v;
      }
    }
    const pvThreshold = parseFloat(s.ampel_pv_threshold_kw) || 2;

    let statusIcon = '⚪';
    let statusText = 'Keine Sensoren konfiguriert';
    let bannerClass = 'banner-none';
    if (temp) {
      const t = parseFloat(temp.avg);
      if (t < comfort) {
        if (isHeating && pvSurplus >= pvThreshold) {
          statusIcon = '☀️'; statusText = 'WP heizt mit PV-Überschuss (' + pvSurplus.toFixed(1) + ' kW)'; bannerClass = 'banner-ok';
        } else if (isHeating) {
          statusIcon = '⚡'; statusText = 'WP heizt vom Netz'; bannerClass = 'banner-pv';
        } else if (isOff) {
          statusIcon = '⏳'; statusText = 'Warte auf PV-Überschuss (' + t.toFixed(1) + '°C)'; bannerClass = 'banner-wait';
        } else {
          statusIcon = '🔄'; statusText = 'WP auf ' + wpState + ' (' + t.toFixed(1) + '°C)'; bannerClass = 'banner-pv';
        }
      } else if (t < pvmax) {
        if (isHeating && pvSurplus >= pvThreshold) {
          statusIcon = '☀️'; statusText = 'PV-Heizen aktiv (' + pvSurplus.toFixed(1) + ' kW)'; bannerClass = 'banner-ok';
        } else {
          statusIcon = '👍'; statusText = 'Temperatur OK (' + t.toFixed(1) + '°C)'; bannerClass = 'banner-ok';
        }
      } else {
        statusIcon = '✅'; statusText = 'Warm genug (' + t.toFixed(1) + '°C)'; bannerClass = 'banner-ok';
      }
    }

    this.shadowRoot.innerHTML = `
      <style>
        :host{display:block}
        ha-card{padding:20px}
        h2{margin:0 0 16px 0;font-size:1.4em;font-weight:500;color:var(--primary-text-color)}
        .status{padding:16px;border:1px solid var(--divider-color,#333);border-radius:8px}
        .status-row{display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--divider-color,#222)}
        .status-row:last-child{border-bottom:none}
        .status-label{font-size:14px;color:var(--secondary-text-color)}
        .status-value{font-size:14px;font-weight:500;color:var(--primary-text-color)}
        .status-banner{padding:12px;border-radius:8px;margin-top:12px;font-size:14px;font-weight:500;text-align:center}
        .banner-warn{background:rgba(255,152,0,0.15);color:#ff9800}
        .banner-pv{background:rgba(255,235,59,0.15);color:#fdd835}
        .banner-ok{background:rgba(76,175,80,0.15);color:#4caf50}
        .banner-wait{background:rgba(33,150,243,0.15);color:#2196f3}
        .banner-none{background:rgba(158,158,158,0.15);color:#9e9e9e}
        .foot{margin-top:12px;font-size:13px;color:var(--secondary-text-color)}
      </style>
      <ha-card>
        <h2>🔥 Heizung / Wärmepumpe</h2>
        <div class="status">
          <div class="status-row">
            <span class="status-label">Ø Temperatur</span>
            <span class="status-value">${temp ? temp.avg + ' °C (' + temp.count + ' Sensor' + (temp.count > 1 ? 'en' : '') + ')' : 'Keine Sensoren'}</span>
          </div>
          <div class="status-row">
            <span class="status-label">WP Status</span>
            <span class="status-value">${wpEntity ? (wpState === 'on' ? '🟢 An' : wpState === 'off' ? '🔴 Aus' : wpState) : 'Nicht konfiguriert'}</span>
          </div>
          <div class="status-row">
            <span class="status-label">Saison</span>
            <span class="status-value">${currentSeason} ${seasonActive ? '✅ aktiv' : '⚪ inaktiv'}</span>
          </div>
          <div class="status-banner ${!heatingEnabled ? 'banner-none' : bannerClass}">
            ${!heatingEnabled ? '⏸️ Heizungssteuerung deaktiviert' : statusIcon + ' ' + statusText}
          </div>
        </div>
        <div class="foot">Einstellungen unter Tariff Saver → Einstellungen → Heizung / Wärmepumpe</div>
      </ha-card>`;
  }

  _esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
  getCardSize(){return 8}
  static getStubConfig(){return{}}
}

customElements.define('tariff-saver-heating-card', TariffSaverHeatingCard);
window.customCards=window.customCards||[];
window.customCards.push({type:'tariff-saver-heating-card',name:'Tariff Saver Heating',description:'Heating / heat pump control for Tariff Saver'});
