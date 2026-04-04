class TariffSaverConsumerCard extends HTMLElement {
  set hass(hass) {
    this._hass = hass;
    const state = hass.states[this._configEntity];
    const statusKey = this._getStatusKey(hass);
    const stateKey = state ? state.last_updated : '';
    const newKey = stateKey + '|' + statusKey;
    if (newKey !== this._lastKey && !this._editing) {
      this._lastKey = newKey;
      this._render();
    }
  }

  _getStatusKey(hass) {
    let key = '';
    for (let i = 1; i <= 10; i++) {
      const e = hass.states[`binary_sensor.tariff_saver_consumer_${i}_should_run`];
      if (e) key += e.state + e.last_updated;
    }
    return key;
  }

  setConfig(config) {
    this._config = config;
    this._configEntity = config.config_entity || 'sensor.tariff_saver_consumer_config';
    this._lastKey = '';
    this._editing = false;
  }

  _getConsumers() {
    if (!this._hass) return {};
    const state = this._hass.states[this._configEntity];
    if (!state || !state.attributes || !state.attributes.consumers) return {};
    return state.attributes.consumers;
  }

  async _save(slot, key, value) {
    if (!this._hass) return;
    try {
      await this._hass.callService('tariff_saver', 'update_consumer', {
        slot: slot, key: key, value: value,
      });
    } catch (e) {
      alert('Speichern fehlgeschlagen: ' + e.message);
    }
  }

  _render() {
    if (!this._hass) return;
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });

    const consumers = this._getConsumers();
    if (!consumers || Object.keys(consumers).length === 0) {
      this.shadowRoot.innerHTML = `<ha-card style="padding:16px"><p>Warte auf Daten...</p></ha-card>`;
      return;
    }

    const modes = [
      { value: 'auto', label: 'Auto' },
      { value: 'fixed_duration', label: 'Fixe Dauer' },
      { value: 'fixed_energy', label: 'Fixe Energie' },
    ];

    let rows = '';
    for (let i = 1; i <= 10; i++) {
      const c = consumers[`consumer_${i}`] || {};
      const enabled = c.enabled || false;
      const name = c.name || '';
      const power = c.power_kw || 0;
      const duration = c.duration_minutes || 0;
      const maxScore = c.max_grid_score != null ? c.max_grid_score : 100;
      const tariffOnly = c.tariff_only || false;
      const pvOpp = c.pv_opportunist || false;
      const priority = c.priority || 5;
      const mode = c.mode || 'auto';
      const measurement = c.measurement_entity || '';
      const minDays = c.min_days || 0;
      const minRuntime = c.min_runtime_minutes || 0;
      const runOrder = c.run_order || 0;
      const maxDays = c.max_days || 0;

      const entity = this._hass.states[`binary_sensor.tariff_saver_consumer_${i}_should_run`];
      const status = entity ? entity.attributes.status || '' : '';
      const source = entity ? entity.attributes.source || '' : '';
      const chosenStart = entity ? entity.attributes.chosen_start : null;
      const chosenEnd = entity ? entity.attributes.chosen_end : null;
      const running = entity ? entity.state === 'on' : false;

      let statusIcon = '⬜';
      let statusText = '';
      if (enabled) {
        if (status === 'aktiv' || running) { statusIcon = '🟢'; statusText = 'aktiv'; }
        else if (status === 'geplant') { statusIcon = '📋'; statusText = 'geplant'; }
        else if (status === 'blockiert') { statusIcon = '🚫'; statusText = 'blockiert'; }
        else if (status === 'beendet') { statusIcon = '✅'; statusText = 'beendet'; }
        else if (status && status !== 'planned' && status !== 'error_no_plan' && status !== 'kein_plan') {
          // Reported status from automation
          statusIcon = '💬'; statusText = status;
        }
        else if (status === 'error_no_plan' || status === 'kein_plan') { statusIcon = '⏸'; statusText = ''; }
        else { statusIcon = '⏸'; statusText = ''; }
      }

      let window = '';
      if (chosenStart && chosenEnd) {
        const s = new Date(chosenStart);
        const e = new Date(chosenEnd);
        window = `${s.toLocaleDateString('de-CH',{day:'2-digit',month:'2-digit'})} ${s.toLocaleTimeString('de-CH',{hour:'2-digit',minute:'2-digit'})}–${e.toLocaleTimeString('de-CH',{hour:'2-digit',minute:'2-digit'})}`;
      }

      let modeOptions = modes.map(m =>
        `<option value="${m.value}" ${mode === m.value ? 'selected' : ''}>${m.label}</option>`
      ).join('');

      rows += `
        <tr class="${enabled ? 'enabled' : 'disabled'}">
          <td class="status">${statusIcon}</td>
          <td class="slot">${i}</td>
          <td class="toggle"><label class="sw"><input type="checkbox" ${enabled ? 'checked' : ''} data-slot="${i}" data-key="enabled"><span class="sl"></span></label></td>
          <td class="name"><input type="text" value="${this._esc(name)}" data-slot="${i}" data-key="name" placeholder="—"></td>
          <td class="mode"><select data-slot="${i}" data-key="mode">${modeOptions}</select></td>
          <td class="num"><input type="number" value="${power}" data-slot="${i}" data-key="power_kw" min="0" max="25" step="0.1"></td>
          <td class="num"><input type="number" value="${duration}" data-slot="${i}" data-key="duration_minutes" min="0" max="480" step="15"></td>
          <td class="num"><input type="number" value="${priority}" data-slot="${i}" data-key="priority" min="1" max="10"></td>
          <td class="num"><input type="number" value="${maxScore}" data-slot="${i}" data-key="max_grid_score" min="0" max="100" step="5"></td>
          <td class="toggle"><label class="sw"><input type="checkbox" ${tariffOnly ? 'checked' : ''} data-slot="${i}" data-key="tariff_only"><span class="sl"></span></label></td>
          <td class="toggle"><label class="sw"><input type="checkbox" ${pvOpp ? 'checked' : ''} data-slot="${i}" data-key="pv_opportunist"><span class="sl"></span></label></td>
          <td class="num"><input type="text" value="${minDays}-${maxDays}" data-slot="${i}" data-key="days_range" style="width:45px;text-align:center" placeholder="0-0"></td>
          <td class="num"><input type="number" value="${minRuntime}" data-slot="${i}" data-key="min_runtime_minutes" min="0" max="60" step="1" style="width:40px"></td>
          <td class="num"><input type="number" value="${runOrder}" data-slot="${i}" data-key="run_order" min="0" max="10" step="1" style="width:50px"></td>
          <td class="meas"><input type="text" value="${this._esc(measurement)}" data-slot="${i}" data-key="measurement_entity" placeholder="—"></td>
          <td class="live">${source || '–'}</td>
          <td class="live win">${window ? window + (statusText ? '<br><span class="stxt">' + statusText + '</span>' : '') : (statusText || '–')}</td>
        </tr>`;
    }

    this.shadowRoot.innerHTML = `
      <style>
        :host{display:block}
        ha-card{padding:20px;overflow-x:auto}
        h2{margin:0 0 16px 4px;font-size:1.4em;font-weight:500;color:var(--primary-text-color)}
        table{width:100%;border-collapse:collapse;font-size:15px}
        th{text-align:left;padding:8px 6px;border-bottom:2px solid var(--divider-color,#333);color:var(--secondary-text-color);font-weight:500;font-size:13px;white-space:nowrap}
        td{padding:8px 6px;border-bottom:1px solid var(--divider-color,#333);vertical-align:middle}
        tr.disabled td:not(.status):not(.toggle){opacity:.3}
        .status{text-align:center;font-size:1.2em;width:30px}
        .slot{text-align:center;font-weight:bold;width:28px;font-size:15px}
        .name{min-width:200px}
        .name input{width:100%;min-width:180px}
        .mode{width:110px}
        .mode select{width:110px}
        .num{width:70px}
        .num input{width:70px;text-align:right}
        .meas{min-width:150px}
        .meas input{width:100%;min-width:130px;font-size:12px}
        .live{color:var(--secondary-text-color);font-size:14px;white-space:nowrap}
        .win{font-size:13px}
        .stxt{font-size:12px;color:var(--primary-color,#03a9f4)}
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
        .foot{margin:12px 0 0 4px;font-size:13px;color:var(--secondary-text-color)}
      </style>
      <ha-card>
        <h2>Consumer Konfiguration</h2>
        <table><thead><tr>
          <th></th><th>#</th><th>An</th><th>Name</th><th>Modus</th><th>kW</th><th>Min</th><th>Prio</th><th>Score</th><th>Grid</th><th>PV-Opp</th><th>Tage</th><th>LZ</th><th>RF</th><th>Mess-Entity</th><th>Quelle</th><th>Status</th>
        </tr></thead><tbody>${rows}</tbody></table>
        <div class="foot">🟢 aktiv · 📋 geplant · 🚫 blockiert · ✅ abgelaufen · 💬 Rückmeldung · ⏸ wartend · ⬜ aus<br>
        <b>Score</b> = Max Grid Score (nur ohne PV) · <b>Grid</b> = nur Tarif-Fenster, kein PV · <b>PV-Opp</b> = läuft immer bei PV-Überschuss · <b>Tage</b> = min-max Tage zwischen Läufen (0-0 = keine Vorgabe) · <b>LZ</b> = Min. Laufzeit in Minuten</div>
      </ha-card>`;

    this._bindEvents();
  }

  _bindEvents() {
    const self = this;
    this.shadowRoot.querySelectorAll('input[type="checkbox"]').forEach(el => {
      el.addEventListener('change', e => {
        e.stopPropagation();
        self._save(parseInt(e.target.dataset.slot), e.target.dataset.key, e.target.checked);
      });
    });
    this.shadowRoot.querySelectorAll('input[type="number"]').forEach(el => {
      el.addEventListener('change', e => {
        e.stopPropagation();
        let v = parseFloat(e.target.value);
        if (['duration_minutes','priority','max_grid_score','min_runtime_minutes','run_order'].includes(e.target.dataset.key)) v = Math.round(v);
        self._save(parseInt(e.target.dataset.slot), e.target.dataset.key, v);
      });
      el.addEventListener('focus', () => { self._editing = true; });
      el.addEventListener('blur', () => { self._editing = false; });
    });
    this.shadowRoot.querySelectorAll('input[type="text"]').forEach(el => {
      el.addEventListener('change', e => {
        e.stopPropagation();
        if (e.target.dataset.key === 'days_range') {
          const parts = String(e.target.value).split('-');
          const minD = parseInt(parts[0]) || 0;
          const maxD = parseInt(parts[1]) || 0;
          const slot = parseInt(e.target.dataset.slot);
          self._save(slot, 'min_days', minD);
          self._save(slot, 'max_days', maxD);
          return;
        }
        self._save(parseInt(e.target.dataset.slot), e.target.dataset.key, e.target.value);
      });
      el.addEventListener('focus', () => { self._editing = true; });
      el.addEventListener('blur', () => { self._editing = false; });
    });
    this.shadowRoot.querySelectorAll('select').forEach(el => {
      el.addEventListener('change', e => {
        e.stopPropagation();
        self._save(parseInt(e.target.dataset.slot), e.target.dataset.key, e.target.value);
      });
    });
  }

  _esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
  getCardSize(){return 8}
  static getStubConfig(){return{}}
}

customElements.define('tariff-saver-consumer-card', TariffSaverConsumerCard);
window.customCards=window.customCards||[];
window.customCards.push({type:'tariff-saver-consumer-card',name:'Tariff Saver Consumer Card',description:'Configure Tariff Saver consumers'});
