class TariffSaverLightAutoOffCard extends HTMLElement {
  set hass(hass) {
    this._hass = hass;
    const state = hass.states[this._configEntity];
    if (!state) return;
    // Key hashes Sensor + Live-State aller erfassten Lichter,
    // damit die Card auf state changes der Lichter reagiert (Aus-Button live).
    const lights = state.attributes.lights || [];
    let newKey = state.last_updated;
    for (const l of lights) {
      const ls = hass.states[l.entity_id];
      if (ls) newKey += '|' + l.entity_id + ':' + ls.state + ':' + ls.last_changed;
    }
    if (newKey !== this._lastKey && !this._editing) {
      this._lastKey = newKey;
      this._render();
    } else {
      this._refreshDurations();
    }
  }

  _liveOn(entityId) {
    const s = this._hass && this._hass.states[entityId];
    if (!s || s.state === 'unavailable' || s.state === 'unknown') return false;
    const domain = entityId.split('.')[0];
    if (domain === 'input_select') return s.state !== 'Aus';
    return s.state === 'on';
  }

  _liveOnSince(entityId) {
    if (!this._liveOn(entityId)) return null;
    const s = this._hass.states[entityId];
    return s.last_changed || null;
  }

  setConfig(config) {
    this._config = config;
    this._configEntity = config.entity || 'sensor.tariff_saver_light_auto_off_status';
    this._lastKey = '';
    this._tickInterval = null;
    this._editing = false;
    this._chain = Promise.resolve();
    this._optimistic = new Map();
    this._confirming = new Map();  // entity_id → timeoutId für Doppelklick-Confirm
  }

  connectedCallback() {
    if (!this._tickInterval) {
      this._tickInterval = setInterval(() => this._refreshDurations(), 30000);
    }
  }

  disconnectedCallback() {
    if (this._tickInterval) {
      clearInterval(this._tickInterval);
      this._tickInterval = null;
    }
  }

  _esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  _formatDuration(seconds) {
    if (seconds == null || seconds < 0) return '—';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m`;
  }

  _formatTimeUntil(iso) {
    if (!iso) return '—';
    const t = new Date(iso).getTime();
    const now = Date.now();
    if (t <= now) return 'jetzt';
    return this._formatDuration(Math.floor((t - now) / 1000));
  }

  _action(entityId, action) {
    if (!this._hass) return;
    this._hass.callService('tariff_saver', 'light_auto_off_action', {
      entity_id: entityId,
      action: action,
    });
  }

  _setThreshold(entityId, labelSuffix) {
    // labelSuffix: null = clear, '1m' = test, '1h'/'2h'/'4h'/'6h' = normal
    if (!this._hass) return;
    // Optimistic UI: threshold_h Wert für Anzeige (1/60 für '1m', n für 'Nh')
    let optHours = null;
    if (labelSuffix === '1m') optHours = 1/60;
    else if (labelSuffix && labelSuffix.endsWith('h')) optHours = parseInt(labelSuffix, 10);
    this._optimistic.set(entityId, optHours);
    this._pendingCount = (this._pendingCount || 0) + 1;
    this._editing = true;
    this._render();
    this._chain = this._chain.then(async () => {
      try {
        const reg = await this._hass.callWS({
          type: 'config/entity_registry/get',
          entity_id: entityId,
        });
        const current = (reg && reg.labels) ? reg.labels : [];
        const other = current.filter((l) => !/^auto_off_(\d+h|\d+m)$/.test(l));
        const next = labelSuffix ? [...other, `auto_off_${labelSuffix}`] : other;
        await this._hass.callWS({
          type: 'config/entity_registry/update',
          entity_id: entityId,
          labels: next,
        });
      } catch (e) {
        console.error('Label setzen fehlgeschlagen:', e);
        this._optimistic.delete(entityId);
      } finally {
        this._pendingCount -= 1;
        if (this._pendingCount === 0) {
          this._editing = false;
          this._lastKey = '';
        }
      }
    });
  }

  _refreshDurations() {
    if (!this.shadowRoot) return;
    const rows = this.shadowRoot.querySelectorAll('[data-on-since]');
    const now = Date.now();
    rows.forEach((td) => {
      const iso = td.dataset.onSince;
      if (!iso) {
        td.textContent = '—';
        return;
      }
      const sec = Math.max(0, Math.floor((now - new Date(iso).getTime()) / 1000));
      td.textContent = this._formatDuration(sec);
    });
    const snoozes = this.shadowRoot.querySelectorAll('[data-snooze-until]');
    snoozes.forEach((td) => {
      td.textContent = this._formatTimeUntil(td.dataset.snoozeUntil);
    });
  }

  _render() {
    if (!this._hass) return;
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    const state = this._hass.states[this._configEntity];
    if (!state || !state.attributes || !state.attributes.lights) {
      this.shadowRoot.innerHTML = `<ha-card style="padding:16px"><p>Warte auf Daten…</p></ha-card>`;
      return;
    }
    const lights = state.attributes.lights || [];
    const monitored = state.attributes.monitored_count || 0;
    const total = state.attributes.total_count || lights.length;

    let rows = '';
    if (lights.length === 0) {
      rows = `<tr><td colspan="7" style="text-align:center;padding:16px;color:#888">Keine Lichter erfasst — Pattern in Einstellungen prüfen.</td></tr>`;
    } else {
      for (const l of lights) {
        // Live-State direkt aus hass.states, nicht aus dem Sensor-Snapshot
        const on = this._liveOn(l.entity_id);
        const onSinceIso = this._liveOnSince(l.entity_id);
        const monitoredFlag = l.threshold_h != null;
        const snoozed = l.snooze_until_iso != null;
        const stateChip = on
          ? '<span class="chip on">an</span>'
          : '<span class="chip off">aus</span>';
        const sourceChip = (() => {
          if (l.source === 'label') return '<span class="src lbl">Label</span>';
          if (l.source === 'both') return '<span class="src lbl">Label+Pattern</span>';
          return '<span class="src ptn">Pattern</span>';
        })();
        // Optionen: 1m als Test (value="1m"), dann Stunden (value="1"…"6")
        const opts = [
          { v: '1m', label: '1m', match: (t) => t != null && t > 0 && t < 1 },
          { v: '1',  label: '1h', match: (t) => t === 1 },
          { v: '2',  label: '2h', match: (t) => t === 2 },
          { v: '4',  label: '4h', match: (t) => t === 4 },
          { v: '6',  label: '6h', match: (t) => t === 6 },
        ];
        let curThreshold = monitoredFlag ? l.threshold_h : null;
        if (this._optimistic.has(l.entity_id)) {
          const opt = this._optimistic.get(l.entity_id);
          if (opt === l.threshold_h || (opt == null && l.threshold_h == null)) {
            this._optimistic.delete(l.entity_id);
          } else {
            curThreshold = opt;
          }
        }
        let optionsHtml = `<option value="0"${curThreshold == null ? ' selected' : ''}>—</option>`;
        for (const o of opts) {
          const sel = o.match(curThreshold) ? ' selected' : '';
          optionsHtml += `<option value="${o.v}"${sel}>${o.label}</option>`;
        }
        const thresholdCell = `<select class="thr-sel" data-eid="${this._esc(l.entity_id)}">${optionsHtml}</select>`;
        // Snooze nur anzeigen wenn Licht aktuell an (sonst irrelevant + verwirrend)
        const snoozeCell = (snoozed && on)
          ? `<span data-snooze-until="${this._esc(l.snooze_until_iso)}" title="${this._esc(l.snooze_until_iso)}">…</span>`
          : '—';
        const durationCell = on && onSinceIso
          ? `<span data-on-since="${this._esc(onSinceIso)}">…</span>`
          : '—';
        // Aus immer wenn Licht an (Schnellzugriff). Snooze nur wenn Schwelle gesetzt.
        const isConfirming = this._confirming.has(l.entity_id);
        const offBtn = on
          ? `<button data-eid="${this._esc(l.entity_id)}" data-action="off" class="${isConfirming ? 'confirming' : ''}" title="Sofort aus">${isConfirming ? 'Sicher?' : 'Aus'}</button>`
          : '';
        const snoozeBtns = on && monitoredFlag
          ? `
            <button data-eid="${this._esc(l.entity_id)}" data-action="1h" title="+1h">+1h</button>
            <button data-eid="${this._esc(l.entity_id)}" data-action="2h">+2h</button>
            <button data-eid="${this._esc(l.entity_id)}" data-action="4h">+4h</button>
            <button data-eid="${this._esc(l.entity_id)}" data-action="6h">+6h</button>`
          : '';
        const buttons = offBtn + snoozeBtns;
        rows += `
          <tr class="${on ? 'is-on' : 'is-off'} ${monitoredFlag ? '' : 'unmonitored'}">
            <td class="name" title="${this._esc(l.entity_id)}">${this._esc(l.friendly_name)}</td>
            <td>${stateChip}</td>
            <td class="dur" data-on-since="${this._esc(onSinceIso || '')}">${durationCell.includes('data-on-since') ? '…' : '—'}</td>
            <td>${thresholdCell}</td>
            <td>${snoozeCell}</td>
            <td>${sourceChip}</td>
            <td class="actions">${buttons}</td>
          </tr>`;
      }
    }

    this.shadowRoot.innerHTML = `
      <style>
        ha-card{padding:12px}
        .header{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
        .header h3{margin:0;font-size:1.1em}
        .summary{font-size:0.85em;color:#888}
        table{width:100%;border-collapse:collapse;font-size:13px}
        th,td{padding:6px 8px;border-bottom:1px solid var(--divider-color,#3a3a3a);text-align:left;vertical-align:middle}
        th{font-weight:600;font-size:12px;color:#aaa;text-transform:uppercase;letter-spacing:0.5px}
        .name{font-weight:500;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .chip{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600}
        .chip.on{background:#ffd54f;color:#000}
        .chip.off{background:#444;color:#aaa}
        .src{display:inline-block;padding:1px 6px;border-radius:6px;font-size:10px;font-weight:500}
        .src.lbl{background:#3a4a5a;color:#90caf9}
        .src.ptn{background:#3a3a3a;color:#999}
        .threshold{color:#90caf9;font-weight:500}
        .no-threshold{color:#666}
        tr.unmonitored{opacity:0.55}
        tr.is-on{background:rgba(255,213,79,0.05)}
        .actions{white-space:nowrap}
        .actions button{
          padding:3px 8px;margin-right:2px;border:1px solid #555;background:#2a2a2a;color:#ddd;
          border-radius:4px;font-size:11px;cursor:pointer
        }
        .actions button:hover{background:#3a3a3a;border-color:#777}
        .actions button[data-action="off"]{border-color:#a55;color:#fa9}
        .actions button[data-action="off"]:hover{background:#5a2a2a}
        .actions button.confirming{background:#5a2a2a;border-color:#f55;color:#fff;font-weight:600}
        select.thr-sel{
          background:#2a2a2a;color:#ddd;border:1px solid #555;border-radius:4px;
          padding:2px 4px;font-size:12px;cursor:pointer
        }
        select.thr-sel:focus{outline:none;border-color:#90caf9}
      </style>
      <ha-card>
        <div class="header">
          <h3>💡 Licht-Wächter</h3>
          <span class="summary">${monitored} überwacht / ${total} erfasst</span>
        </div>
        <table>
          <thead><tr>
            <th>Licht</th><th>Status</th><th>Brennt seit</th><th>Schwelle</th><th>Snooze</th><th>Quelle</th><th>Aktion</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </ha-card>
    `;

    this.shadowRoot.querySelectorAll('button[data-eid]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const action = btn.dataset.action;
        const entityId = btn.dataset.eid;
        // Doppelklick-Confirm nur für 'off' (destruktiv)
        if (action === 'off') {
          if (this._confirming.has(entityId)) {
            // Bestätigt → ausführen
            clearTimeout(this._confirming.get(entityId));
            this._confirming.delete(entityId);
            this._action(entityId, action);
          } else {
            // Erster Klick → Confirm-Mode für 3s
            const tid = setTimeout(() => {
              this._confirming.delete(entityId);
              this._render();
            }, 3000);
            this._confirming.set(entityId, tid);
            this._render();
          }
          return;
        }
        this._action(entityId, action);
      });
    });
    this.shadowRoot.querySelectorAll('select.thr-sel').forEach((sel) => {
      sel.addEventListener('change', () => {
        const v = sel.value;
        // value: '0' = aus, '1m' = Test 1min, '1'/'2'/'4'/'6' = Stunden
        let label = null;
        if (v === '1m') label = '1m';
        else if (v !== '0') label = parseInt(v, 10) + 'h';
        this._setThreshold(sel.dataset.eid, label);
      });
    });
    this._refreshDurations();
  }

  getCardSize() {
    return 4;
  }
}

customElements.define('tariff-saver-light-auto-off-card', TariffSaverLightAutoOffCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: 'tariff-saver-light-auto-off-card',
  name: 'Tariff Saver Licht-Wächter',
  description: 'Übersicht erfasste Lichter mit Schwellen + Snooze + Aktionen',
});
