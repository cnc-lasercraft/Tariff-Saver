class EkzTariffSettingsCard extends HTMLElement {
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
    this._configEntity = config.entity || 'sensor.ekz_tariff_settings';
    this._lastKey = '';
    this._editing = false;
  }

  _getSettings() {
    if (!this._hass) return {};
    const state = this._hass.states[this._configEntity];
    if (!state || !state.attributes || !state.attributes.settings) return {};
    return state.attributes.settings;
  }

  _save(key, value) {
    if (!this._debounceTimers) this._debounceTimers = {};
    clearTimeout(this._debounceTimers[key]);
    this._debounceTimers[key] = setTimeout(async () => {
      if (!this._hass) return;
      try {
        await this._hass.callService('ekz_tariff', 'update_setting', { key, value });
      } catch (e) {
        alert('Speichern fehlgeschlagen: ' + e.message);
      }
    }, 500);
  }

  _render() {
    if (!this._hass) return;
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });

    const s = this._getSettings();
    if (!s || Object.keys(s).length === 0) {
      this.shadowRoot.innerHTML = `<ha-card style="padding:16px"><p>Warte auf Daten...</p></ha-card>`;
      return;
    }

    const groups = [
      {
        title: 'Publikation',
        icon: 'mdi:clock-outline',
        desc: 'Wann EKZ die Tarife für morgen veröffentlicht.',
        fields: [
          { key: 'publish_time', label: 'Publish Time (HH:MM)', type: 'text', hint: 'Standard: 18:15' },
        ]
      },
      {
        title: 'Validierung',
        icon: 'mdi:check-decagram',
        desc: 'Preisgrenzen für die Qualitätsprüfung. Slot-Anzahl wird automatisch geprüft (96, DST-aware).',
        fields: [
          { key: 'min_price_chf_per_kwh', label: 'Min. Preis (CHF/kWh)', type: 'number', min: 0, max: 1, step: 0.01, hint: 'Darunter = Dummy-Preis' },
          { key: 'max_price_chf_per_kwh', label: 'Max. Preis (CHF/kWh)', type: 'number', min: 0.1, max: 5, step: 0.01, hint: 'Darüber = Ausreisser' },
        ]
      },
      {
        title: 'Retry',
        icon: 'mdi:refresh',
        desc: 'Wiederholungsversuche bei Fehlern (keine Daten oder ungültig).',
        fields: [
          { key: 'max_retries', label: 'Max. Retries', type: 'number', min: 0, max: 20, step: 1, hint: 'Standard: 4' },
          { key: 'retry_interval_minutes', label: 'Retry-Intervall (Min.)', type: 'number', min: 1, max: 120, step: 1, hint: 'Standard: 15' },
        ]
      },
      {
        title: 'Public-API-Fallback',
        icon: 'mdi:cloud-refresh',
        desc: 'Wenn die Kunden-API nicht liefert (Auth-Fehler, 5xx, leer), werden die Preise aus der öffentlichen EKZ-API rekonstruiert: electricity_dynamic (identisch) + Netztarif + Offset.',
        fields: [
          { key: 'public_fallback_enabled', label: 'Fallback aktivieren', type: 'toggle' },
          { key: 'public_fallback_grid_tariff', label: 'Netztarif-Name (Public API)', type: 'text', hint: 'Standard: grid_400d' },
          { key: 'public_fallback_grid_offset_chf_per_kwh', label: 'Netz-Offset (CHF/kWh)', type: 'number', min: 0, max: 0.2, step: 0.0001, hint: 'Kundenpreis − Public-Preis. Standard: 0.0276' },
          { key: 'public_fallback_regional_fees_chf_per_kwh', label: 'Regionale Abgaben (CHF/kWh)', type: 'number', min: 0, max: 0.1, step: 0.0001, hint: 'Standard: 0.0016' },
        ]
      },
      {
        title: 'Debug',
        icon: 'mdi:bug',
        desc: 'Jeder API-Call wird als einzelne JSON-Datei in /config/ekz_tariff_debug/ gespeichert.',
        fields: [
          { key: 'debug_mode', label: 'API-Dump aktivieren', type: 'toggle' },
        ]
      },
    ];

    let html = '';
    for (const g of groups) {
      html += `<div class="group">
        <div class="group-header"><ha-icon icon="${g.icon}"></ha-icon> ${g.title}</div>
        <div class="group-desc">${g.desc}</div>`;
      for (const f of g.fields) {
        const val = s[f.key] !== undefined && s[f.key] !== null ? s[f.key] : '';
        let input = '';
        if (f.type === 'info') {
          input = `<span class="info-val">${val || '—'}</span>`;
        } else if (f.type === 'toggle') {
          input = `<label class="sw"><input type="checkbox" ${val ? 'checked' : ''} data-key="${f.key}"><span class="sl"></span></label>`;
        } else if (f.type === 'number') {
          input = `<input type="number" value="${val}" data-key="${f.key}" min="${f.min||0}" max="${f.max||100}" step="${f.step||1}">`;
        } else {
          input = `<input type="text" value="${this._esc(String(val))}" data-key="${f.key}" placeholder="${f.hint || ''}">`;
        }
        html += `<div class="field">
          <div class="field-label">${f.label}</div>
          <div class="field-input">${input}</div>
          ${f.hint && f.type !== 'text' ? `<div class="field-hint">${f.hint}</div>` : ''}
        </div>`;
      }
      html += '</div>';
    }

    this.shadowRoot.innerHTML = `
      <style>
        :host{display:block}
        ha-card{padding:20px}
        h2{margin:0 0 16px 0;font-size:1.4em;font-weight:500;color:var(--primary-text-color)}
        .group{margin-bottom:24px;padding:16px;border:1px solid var(--divider-color,#333);border-radius:8px}
        .group-header{font-size:1.1em;font-weight:500;color:var(--primary-text-color);margin-bottom:4px;display:flex;align-items:center;gap:8px}
        .group-desc{font-size:13px;color:var(--secondary-text-color);margin-bottom:12px}
        .field{display:flex;align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid var(--divider-color,#222)}
        .field:last-child{border-bottom:none}
        .field-label{min-width:200px;font-size:14px;color:var(--primary-text-color)}
        .field-input{flex:1}
        .field-input input[type="text"]{width:100%;min-width:200px}
        .field-input input[type="number"]{width:100px;text-align:right}
        .field-hint{font-size:12px;color:var(--secondary-text-color);max-width:250px}
        input[type="text"],input[type="number"]{
          padding:6px 8px;border:1px solid var(--divider-color,#444);border-radius:4px;
          background:var(--input-fill-color,var(--card-background-color,#1c1c1c));
          color:var(--primary-text-color);font-size:14px;box-sizing:border-box;
        }
        input:focus{outline:none;border-color:var(--primary-color,#03a9f4);box-shadow:0 0 0 1px var(--primary-color,#03a9f4)}
        .sw{position:relative;display:inline-block;width:40px;height:22px}
        .sw input{opacity:0;width:0;height:0}
        .sl{position:absolute;cursor:pointer;top:0;left:0;right:0;bottom:0;background:var(--disabled-color,#555);transition:.2s;border-radius:22px}
        .sl:before{position:absolute;content:"";height:16px;width:16px;left:3px;bottom:3px;background:#fff;transition:.2s;border-radius:50%}
        input:checked+.sl{background:var(--primary-color,#03a9f4)}
        input:checked+.sl:before{transform:translateX(18px)}
        .info-val{font-size:14px;color:var(--secondary-text-color)}
        .foot{margin-top:12px;font-size:13px;color:var(--secondary-text-color)}
      </style>
      <ha-card>
        <h2>EKZ Tariff Einstellungen</h2>
        ${html}
        <div class="foot">Änderungen werden sofort gespeichert</div>
      </ha-card>`;

    this._bindEvents();
  }

  _bindEvents() {
    const self = this;
    this.shadowRoot.querySelectorAll('input[type="checkbox"]').forEach(el => {
      el.addEventListener('change', e => { self._save(e.target.dataset.key, e.target.checked); });
    });
    this.shadowRoot.querySelectorAll('input[type="number"]').forEach(el => {
      el.addEventListener('change', e => { self._save(e.target.dataset.key, parseFloat(e.target.value)); });
      el.addEventListener('focus', () => { self._editing = true; });
      el.addEventListener('blur', () => { self._editing = false; });
    });
    this.shadowRoot.querySelectorAll('input[type="text"]').forEach(el => {
      el.addEventListener('change', e => { self._save(e.target.dataset.key, e.target.value); });
      el.addEventListener('focus', () => { self._editing = true; });
      el.addEventListener('blur', () => { self._editing = false; });
    });
  }

  _esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
  getCardSize(){return 8}
  static getStubConfig(){return{}}
}

customElements.define('ekz-tariff-settings-card', EkzTariffSettingsCard);
window.customCards=window.customCards||[];
window.customCards.push({type:'ekz-tariff-settings-card',name:'EKZ Tariff Settings',description:'Configure EKZ Tariff settings'});
