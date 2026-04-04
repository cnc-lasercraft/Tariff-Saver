class TariffSaverSettingsCard extends HTMLElement {
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
        title: 'Verbrauch',
        icon: 'mdi:flash',
        desc: 'Sensor für den Gesamtstromverbrauch und optionale Saison-Erkennung.',
        fields: [
          { key: 'consumption_energy_entity', label: 'Verbrauchs-Sensor', type: 'text', hint: 'Entity die den täglichen Gesamtverbrauch misst (kWh, total_increasing)' },
          { key: 'season_entity', label: 'Saison-Entity', type: 'text', hint: 'Optional. Wenn leer: automatisch aus Monat (Frühling/Sommer/Herbst/Winter)' },
        ]
      },
      {
        title: 'Energielieferant',
        icon: 'mdi:transmission-tower',
        desc: 'Verbindung zum Tarifanbieter für dynamische Strompreise.',
        fields: [
          { key: 'ekz_entry_id', label: 'Provider Config Entry ID', type: 'text', hint: 'Leer = automatisch erster gefundener Provider' },
          { key: 'publish_time', label: 'Publish Time', type: 'text', hint: 'Uhrzeit wann der Provider die Tarife für morgen veröffentlicht (z.B. 18:15)' },
          { key: 'min_valid_price', label: 'Min. gültiger Preis (CHF/kWh)', type: 'number', min: -0.5, max: 0.5, step: 0.01, hint: 'Darunter = Dummy-Preis ignoriert (DE: negative Preise möglich)' },
          { key: 'max_valid_price', label: 'Max. gültiger Preis (CHF/kWh)', type: 'number', min: 0.1, max: 5.0, step: 0.1, hint: 'Darüber = Dummy-Preis ignoriert' },
        ]
      },
      {
        title: 'PV Anlage',
        icon: 'mdi:solar-power',
        desc: 'PV-Prognose für die Planung von Verbrauchern in Sonnenstunden.',
        fields: [
          { key: 'ampel_pv_entity', label: 'PV Leistungs-Entity', type: 'text', hint: 'Entity die aktuelle PV-Leistung in W oder kW liefert' },
          { key: 'pv_surplus_entity', label: 'PV-Überschuss Entity', type: 'text', hint: 'Netto PV-Überschuss für PV-Opportunist Consumer (W oder kW)' },
          { key: 'pv_forecast_entity', label: 'Forecast Entity', type: 'text', hint: 'z.B. sensor.solcast_pv_forecast_prognose_heute' },
          { key: 'pv_forecast_attribute', label: 'Forecast Attribut', type: 'text', hint: 'Name des Attributs mit den Forecast-Slots (z.B. detailedForecast)' },
        ]
      },
      {
        title: 'Batterie',
        icon: 'mdi:battery-charging',
        desc: 'Batteriespeicher für die Berücksichtigung bei der Verbrauchsplanung.',
        fields: [
          { key: 'battery_enabled', label: 'Batterie aktiv', type: 'toggle' },
          { key: 'battery_soc_entity', label: 'SOC Entity', type: 'text', hint: 'Entity die den aktuellen Ladestand in % liefert' },
          { key: 'battery_capacity_kwh', label: 'Kapazität (kWh)', type: 'number', min: 0, max: 200, step: 0.5 },
          { key: 'battery_min_soc_percent', label: 'Min. SOC (%)', type: 'number', min: 0, max: 100, step: 1 },
          { key: 'battery_soc_margin_percent', label: 'SOC Sicherheitsmarge (%)', type: 'number', min: 0, max: 50, step: 1, hint: 'Wird zum berechneten Target SOC addiert' },
          { key: 'battery_round_trip_loss_percent', label: 'Speicherverlust (%)', type: 'number', min: 0, max: 30, step: 1, hint: 'Round-Trip Verlust Grid→Akku→Verbrauch (Standard 10%)' },
          { key: 'battery_max_charge_kw', label: 'Max. Ladeleistung (kW)', type: 'number', min: 0.5, max: 25, step: 0.5, hint: 'Maximale Grid-Ladeleistung' },
          { key: 'battery_pv_charge_threshold_kw', label: 'PV Lade-Schwelle (kW)', type: 'number', min: 0.5, max: 10, step: 0.5, hint: 'Ab diesem PV-Überschuss lädt der Akku gratis (kein Grid-Laden nötig)' },
        ]
      },
      {
        title: 'Einspeisung',
        icon: 'mdi:transmission-tower-export',
        desc: 'Einspeisevergütung für die Berechnung der optimalen Eigenverbrauchsstrategie.',
        fields: [
          { key: 'feed_in_price_mode', label: 'Modus', type: 'select', options: [{v:'fixed',l:'Fixpreis'},{v:'entity',l:'Entity'}] },
          { key: 'feed_in_fixed_price', label: 'Fixpreis (CHF/kWh)', type: 'number', min: 0, max: 1, step: 0.001 },
          { key: 'feed_in_price_entity', label: 'Preis-Entity', type: 'text', hint: 'Nur wenn Modus = Entity' },
        ]
      },
      {
        title: 'Lastprofil',
        icon: 'mdi:chart-bar',
        desc: 'Grundlast-Profil pro 30-Minuten-Slot. Wird täglich um 23:55 erfasst und saisonal gemittelt. Basis für die Akku-Ladestrategie.',
        fields: [
          { key: '_profile_status', label: 'Status', type: 'info' },
        ]
      },
      {
        title: 'Strom-Ampel',
        icon: 'mdi:traffic-light',
        desc: 'Universelle Empfehlung ob jetzt ein guter Zeitpunkt ist Geräte zu starten. Wird auf E-Papern und Dashboards angezeigt.',
        fields: [
          { key: 'ampel_pv_threshold_kw', label: 'PV Schwelle (kW)', type: 'number', min: 0.5, max: 20, step: 0.5, hint: 'Ab dieser PV-Leistung = grün (Gratis Solarstrom)' },
          { key: 'ampel_score_good', label: 'Score Grenze günstig', type: 'number', min: 0, max: 100, step: 5, hint: 'Darunter = grün' },
          { key: 'ampel_score_bad', label: 'Score Grenze teuer', type: 'number', min: 0, max: 100, step: 5, hint: 'Darüber = rot' },
          { key: 'strom_sparen_score', label: 'Strom sparen ab Score', type: 'number', min: 0, max: 100, step: 5, hint: 'Über diesem Score: binary_sensor.tariff_saver_strom_sparen = on → nicht-essentielle Geräte abschalten' },
        ]
      },
      {
        title: 'Heizung / Wärmepumpe',
        icon: 'mdi:heat-pump-outline',
        desc: 'WP-Steuerung mit PV-Überschuss-Heizen, Komfort-Minimum und Standby-Sparfunktion.',
        fields: [
          { key: 'heating_wp_entity', label: 'WP Entity (Switch)', type: 'text', hint: 'Entity zum Ein-/Ausschalten der WP' },
          { key: 'heating_wp_power_kw', label: 'WP Leistung (kW)', type: 'number', min: 0.5, max: 15, step: 0.1, hint: 'Wird erlernt, Startwert für Consumer-Koordination' },
          { key: 'heating_temp_sensor_1', label: 'Temp Sensor 1', type: 'text', hint: 'Raumtemperatur-Sensor (mind. 1 nötig)' },
          { key: 'heating_temp_sensor_2', label: 'Temp Sensor 2', type: 'text', hint: 'Optional — Durchschnitt wird berechnet' },
          { key: 'heating_temp_sensor_3', label: 'Temp Sensor 3', type: 'text', hint: 'Optional — Durchschnitt wird berechnet' },
          { key: 'heating_comfort_min', label: 'Komfort-Minimum (°C)', type: 'number', min: 15, max: 25, step: 0.5, hint: 'Darunter heizt WP immer (auch vom Netz)' },
          { key: 'heating_pv_max', label: 'PV-Heiz-Maximum (°C)', type: 'number', min: 18, max: 28, step: 0.5, hint: 'Darüber stoppt PV-Heizen' },
          { key: 'heating_max_score', label: 'Teuer-Grenze Score', type: 'number', min: 0, max: 100, step: 5, hint: 'WP aus wenn Score darüber (ausser unter Komfort-Min)' },
          { key: 'heating_max_score_absolute', label: 'Absolut-Grenze Score', type: 'number', min: 0, max: 100, step: 5, hint: 'WP aus auch bei Kälte (nur Frostschutz bleibt)' },
          { key: 'heating_frost_min', label: 'Frostschutz-Min (°C)', type: 'number', min: 10, max: 20, step: 0.5, hint: 'Unter diesem Wert wird IMMER geheizt' },
          { key: 'heating_pv_wait_minutes', label: 'PV-Wartezeit (Min)', type: 'number', min: 0, max: 120, step: 5, hint: 'Minuten auf PV warten bevor Grid-Heizen' },
          { key: 'heating_wp_min_runtime', label: 'Min. Laufzeit WP (Min)', type: 'number', min: 0, max: 60, step: 5, hint: 'Mindestlaufzeit bevor WP gestoppt wird' },
          { key: 'heating_seasons', label: 'Aktive Saisons', type: 'text', hint: 'Kommagetrennt, z.B. Frühling,Herbst' },
        ]
      }
    ];

    let html = '';
    for (const g of groups) {
      html += `<div class="group">
        <div class="group-header"><ha-icon icon="${g.icon}"></ha-icon> ${g.title}</div>
        <div class="group-desc">${g.desc}</div>`;
      for (const f of g.fields) {
        const val = s[f.key] !== undefined ? s[f.key] : '';
        let input = '';
        if (f.type === 'info') {
          let infoText = '';
          const bl = this._hass.states['sensor.tariff_saver_base_load_daily'];
          if (bl && bl.attributes) {
            const records = bl.attributes.total_records || 0;
            const profiles = bl.attributes.profile_records || 0;
            const season = bl.attributes.current_season || '-';
            const lastDate = bl.attributes.last_date || '-';
            infoText = records + ' Tage · ' + profiles + ' Profile · Saison: ' + season + ' · Letzter: ' + lastDate;
          } else {
            infoText = 'Noch keine Daten (erster Eintrag heute 23:55)';
          }
          input = `<span class="info-val">${infoText}</span>`;
        } else if (f.type === 'toggle') {
          input = `<label class="sw"><input type="checkbox" ${val ? 'checked' : ''} data-key="${f.key}"><span class="sl"></span></label>`;
        } else if (f.type === 'select') {
          const opts = f.options.map(o => `<option value="${o.v}" ${val === o.v ? 'selected' : ''}>${o.l}</option>`).join('');
          input = `<select data-key="${f.key}">${opts}</select>`;
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
        .field-label{min-width:160px;font-size:14px;color:var(--primary-text-color)}
        .field-input{flex:1}
        .field-input input[type="text"]{width:100%;min-width:200px}
        .field-input input[type="number"]{width:100px;text-align:right}
        .field-hint{font-size:12px;color:var(--secondary-text-color);max-width:250px}
        input[type="text"],input[type="number"],select{
          padding:6px 8px;border:1px solid var(--divider-color,#444);border-radius:4px;
          background:var(--input-fill-color,var(--card-background-color,#1c1c1c));
          color:var(--primary-text-color);font-size:14px;box-sizing:border-box;
        }
        input:focus,select:focus{outline:none;border-color:var(--primary-color,#03a9f4);box-shadow:0 0 0 1px var(--primary-color,#03a9f4)}
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
        <h2>Tariff Saver Einstellungen</h2>
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
    this.shadowRoot.querySelectorAll('select').forEach(el => {
      el.addEventListener('change', e => { self._save(e.target.dataset.key, e.target.value); });
    });
  }

  _esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
  getCardSize(){return 12}
  static getStubConfig(){return{}}
}

customElements.define('tariff-saver-settings-card', TariffSaverSettingsCard);
window.customCards=window.customCards||[];
window.customCards.push({type:'tariff-saver-settings-card',name:'Tariff Saver Settings',description:'Configure Tariff Saver general settings'});
