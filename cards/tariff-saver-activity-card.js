class TariffSaverActivityCard extends HTMLElement {
  set hass(hass) {
    this._hass = hass;
    const state = hass.states[this._sensorEntity];
    const key = state ? state.last_updated : '';
    if (key !== this._lastKey) {
      this._lastKey = key;
      this._render();
    }
  }

  setConfig(config) {
    this._config = config;
    this._lastKey = '';
    this._renderLock = false;
    this._sensorEntity = config.entity || 'sensor.tariff_saver_activity_log';
    this._serviceDomain = config.service_domain || 'tariff_saver';
  }

  async _clearAll() {
    if (!this._hass) return;
    this._renderLock = true;
    this.shadowRoot.querySelectorAll('.row').forEach(r => r.remove());
    await this._hass.callService(this._serviceDomain, 'clear_activity_log', {});
    setTimeout(() => { this._renderLock = false; this._lastKey = ''; }, 3000);
  }

  async _deleteEntry(index) {
    if (!this._hass) return;
    this._renderLock = true;
    const row = this.shadowRoot.querySelector(`.row[data-idx="${index}"]`);
    if (row) row.remove();
    await this._hass.callService(this._serviceDomain, 'delete_activity_log_entry', { index });
    setTimeout(() => { this._renderLock = false; this._lastKey = ''; }, 3000);
  }

  _render() {
    if (!this._hass || this._renderLock) return;
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });

    const state = this._hass.states[this._sensorEntity];
    const entries = (state && state.attributes && state.attributes.entries) || [];

    let rows = '';
    for (let i = 0; i < entries.length; i++) {
      const e = entries[i];
      const time = (e.time || '').substring(11, 16);
      const date = (e.time || '').substring(8, 10) + '.' + (e.time || '').substring(5, 7);
      rows += `<div class="row" data-idx="${i}">
        <span class="time">${date} ${time}</span>
        <span class="icon">${e.icon || ''}</span>
        <span class="msg">${e.msg || ''}</span>
        <button class="del" data-index="${i}" title="Löschen">✕</button>
      </div>`;
    }

    if (!rows) {
      rows = '<div class="empty">Noch keine Aktivität</div>';
    }

    this.shadowRoot.innerHTML = `
      <style>
        :host{display:block}
        ha-card{padding:16px}
        .header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
        h2{margin:0;font-size:1.3em;font-weight:500;color:var(--primary-text-color)}
        .clear-btn{padding:6px 12px;border:1px solid var(--divider-color,#444);border-radius:6px;background:none;color:var(--primary-text-color);font-size:13px;cursor:pointer}
        .clear-btn:hover{background:rgba(244,67,54,0.1);border-color:#f44336;color:#f44336}
        .row{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--divider-color,#222)}
        .row:last-child{border-bottom:none}
        .time{font-size:12px;color:var(--secondary-text-color);min-width:80px;font-family:monospace}
        .icon{font-size:14px;min-width:20px}
        .msg{flex:1;font-size:13px;color:var(--primary-text-color)}
        .del{background:none;border:none;color:var(--secondary-text-color);cursor:pointer;font-size:14px;padding:4px 6px;border-radius:4px;opacity:0.4}
        .del:hover{opacity:1;color:#f44336;background:rgba(244,67,54,0.1)}
        .empty{font-size:14px;color:var(--secondary-text-color);font-style:italic;padding:8px 0}
      </style>
      <ha-card>
        <div class="header">
          <h2>Aktivität</h2>
          ${entries.length > 0 ? '<button class="clear-btn">Alles löschen</button>' : ''}
        </div>
        ${rows}
      </ha-card>`;

    // Bind events
    const self = this;
    const clearBtn = this.shadowRoot.querySelector('.clear-btn');
    if (clearBtn) {
      clearBtn.addEventListener('click', () => { self._clearAll(); });
    }
    this.shadowRoot.querySelectorAll('.del').forEach(btn => {
      btn.addEventListener('click', function() {
        self._deleteEntry(parseInt(this.dataset.index));
      });
    });
  }

  getCardSize() { return 6; }
  static getStubConfig() { return {}; }
}

customElements.define('tariff-saver-activity-card', TariffSaverActivityCard);
window.customCards = window.customCards || [];
window.customCards.push({type: 'tariff-saver-activity-card', name: 'Tariff Saver Activity', description: 'Activity log with delete buttons'});
