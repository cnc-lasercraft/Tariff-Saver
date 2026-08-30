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

  _save(key, value) {
    if (!this._debounceTimers) this._debounceTimers = {};
    clearTimeout(this._debounceTimers[key]);
    this._debounceTimers[key] = setTimeout(async () => {
      if (!this._hass) return;
      try {
        if (key.startsWith('input_number.')) {
          await this._hass.callService('input_number', 'set_value', { entity_id: key, value: Number(value) });
        } else if (key.startsWith('input_boolean.')) {
          await this._hass.callService('input_boolean', value ? 'turn_on' : 'turn_off', { entity_id: key });
        } else {
          await this._hass.callService('tariff_saver', 'update_setting', { key, value });
        }
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
        title: 'Verbrauch',
        icon: 'mdi:flash',
        desc: 'Sensor für den Gesamtstromverbrauch und optionale Saison-Erkennung.',
        fields: [
          { key: 'consumption_energy_entity', label: 'Verbrauchs-Sensor', type: 'text', hint: 'Entity die den täglichen Gesamtverbrauch misst (kWh, total_increasing)' },
          { key: 'season_entity', label: 'Saison-Entity', type: 'text', hint: 'Optional. Wenn leer: automatisch aus Monat (Frühling/Sommer/Herbst/Winter)' },
        ]
      },
      {
        title: 'Abrechnung',
        icon: 'mdi:receipt-text',
        desc: 'Abrechnungsperiode, MWST und Fixkosten.',
        fields: [
          { key: 'billing_period_months', label: 'Abrechnungsperiode (Monate)', type: 'number', min: 1, max: 12, step: 1, hint: '1 oder 3 (Standard: 3)' },
          { key: 'billing_start_date', label: 'Beginn aktuelle Periode', type: 'text', hint: 'z.B. 2026-04-01' },
          { key: 'mwst_percent', label: 'MWST (%)', type: 'number', min: 0, max: 20, step: 0.1, hint: 'Standard: 8.1' },
          { key: 'billing_fixed_costs', label: 'Fixkosten', type: 'fixed_costs', hint: 'Monatliche Gebühren oder einmalig pro Abrechnungsperiode' },
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
          { key: 'max_grid_power_kw', label: 'Max. Grid-Leistung (kW)', type: 'number', min: 1, max: 50, step: 1, hint: 'Maximale gleichzeitige Netzleistung für Consumer-Planung (Standard 25 kW)' },
          { key: 'flat_tariff_spread_rp', label: 'Flat-Tarif-Schwelle (Rp)', type: 'number', min: 0, max: 20, step: 0.5, hint: 'Tages-Spread unter diesem Wert → Tarif gilt als flat (Standard 3 Rp). Trigger für binary_sensor.tariff_saver_tariff_flat.' },
          { key: 'flat_grid_earliest_hour', label: 'Flat: Grid frühestens ab (Stunde)', type: 'number', min: 0, max: 23, step: 1, hint: 'Bei flat-Tarif: Grid-Consumer frühestens ab dieser Stunde planen. Default 15 (nachmittags statt nachts).' },
        ]
      },
      {
        title: 'Baseline Tarif',
        icon: 'mdi:chart-line-variant',
        desc: 'EKZ Standard-Tarif pro Saison (netto, ohne MWST, in CHF/kWh). Score 50 = Baseline.',
        fields: [
          { key: 'baseline_winter', label: 'Winter (Jan-März)', type: 'number', min: 0.05, max: 1.0, step: 0.0001, hint: 'CHF/kWh netto' },
          { key: 'baseline_fruehling', label: 'Frühling (Apr-Juni)', type: 'number', min: 0.05, max: 1.0, step: 0.0001, hint: 'CHF/kWh netto' },
          { key: 'baseline_sommer', label: 'Sommer (Juli-Sept)', type: 'number', min: 0.05, max: 1.0, step: 0.0001, hint: 'CHF/kWh netto' },
          { key: 'baseline_herbst', label: 'Herbst (Okt-Dez)', type: 'number', min: 0.05, max: 1.0, step: 0.0001, hint: 'CHF/kWh netto' },
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
          { key: 'emergency_pv_run_enabled', label: 'PV-Notlauf ohne Tarifdaten', type: 'toggle', hint: 'Fällige Consumer laufen bei PV-Überschuss auch wenn keine Tarif-Daten da sind' },
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
          { key: 'battery_device_id', label: 'Huawei Device ID', type: 'text', hint: 'Device ID für forcible_charge_soc (32-stellige Hex-ID des Wechselrichters aus den HA-Geräte-Einstellungen)' },
          { key: 'battery_hold_enter_score', label: 'Hold Ein (Score)', type: 'number', min: 0, max: 100, step: 5, hint: 'Akku schonen wenn Score ≤ diesem Wert (Standard 30)' },
          { key: 'battery_hold_exit_score', label: 'Hold Aus (Score)', type: 'number', min: 0, max: 100, step: 5, hint: 'Akku wieder nutzen wenn Score ≥ diesem Wert (Standard 40)' },
          { key: 'battery_pv_full_charge_ratio', label: 'PV-Voll-Lade-Schwelle (Ratio)', type: 'number', min: 0, max: 3, step: 0.1, hint: 'PV-Prognose / Tages-Grundlast. Unter diesem Wert wird der Akku nachts auf 100% geladen (Standard 1.0)' },
          { key: 'battery_charge_cost_tolerance_pct', label: 'Lade-Kostentoleranz (%)', type: 'number', min: 0, max: 100, step: 1, hint: 'Reicht der Akku nicht bis PV-Start, wird nachts trotzdem geladen, solange es höchstens X % teurer ist als der Morgen-Netzbezug (Standard 15)' },
        ]
      },
      {
        title: 'Wallbox / Ladeplanung',
        icon: 'mdi:ev-station',
        desc: 'EV-Laden: Hausakku-Vorrang und Einspeisungs-Override für die Wallbox-Freigabe. Ladeermahnung: binary_sensor.tariff_saver_ladeempfehlung wird ON wenn Anstecken lohnt (günstiges Nachtfenster oder PV-Überschuss morgen) — die Pushes pro Fahrzeug sind Automationen.',
        fields: [
          { key: 'input_number.ladeplanung_akku_vorrang_soc', label: 'Akku-Vorrang bis SOC (%)', type: 'helper_number', min: 0, max: 100, step: 1, hint: 'Hausakku lädt erst bis zu diesem SOC, bevor die Wallbox freigegeben wird (KANN-Laden)' },
          { key: 'input_number.ladeplanung_einspeisung_override_w', label: 'Einspeisungs-Override EIN (W)', type: 'helper_number', min: 0, max: 10000, step: 50, hint: 'Über dieser Einspeisung (für 3 min) darf die Wallbox laden, auch wenn Akku noch nicht voll' },
          { key: 'input_number.ladeplanung_einspeisung_override_off_w', label: 'Einspeisungs-Override AUS (W)', type: 'helper_number', min: 0, max: 10000, step: 50, hint: 'Unter dieser Einspeisung (für 3 min) wird das Override-Flag wieder deaktiviert (Hysterese-Untergrenze)' },
          { key: 'ladeempfehlung_enabled', label: 'Ladeermahnung aktiv', type: 'toggle', hint: 'Signal-Sensor für die Ermahnungs-Pushes (Zeekr + WN7)' },
          { key: 'ladeempfehlung_max_score', label: 'Ermahnung: Max. Fenster-Score', type: 'number', min: 0, max: 100, step: 5, hint: 'Nachtfenster gilt als günstig wenn sein Score (Jahres-Skala) ≤ diesem Wert (Standard 30)' },
          { key: 'ladeempfehlung_window_hours', label: 'Ermahnung: Fensterlänge (h)', type: 'number', min: 1, max: 8, step: 1, hint: 'Zusammenhängendes Nachtfenster 21:00–07:00 dieser Länge (Standard 3)' },
          { key: 'ladeempfehlung_min_pv_kwh', label: 'Ermahnung: Min. PV-Überschuss (kWh)', type: 'number', min: 0, max: 50, step: 0.5, hint: 'PV-Prognose morgen minus Grundlast ≥ diesem Wert → Anstecken lohnt (Standard 5, WN7 braucht 4-5 kWh)' },
          { key: 'input_number.ladeermahnung_zeekr_soc', label: 'Ermahnung Zeekr unter SOC (%)', type: 'helper_number', min: 0, max: 100, step: 5, hint: 'Push nur wenn Zeekr-SOC unter diesem Wert (Standard 80 — ab 20% Ladebedarf lohnt sich Anstecken)' },
          { key: 'input_number.ladeermahnung_wn7_soc', label: 'Ermahnung WN7 unter SOC (%)', type: 'helper_number', min: 0, max: 100, step: 5, hint: 'Push nur wenn WN7-SOC unter diesem Wert (Standard 80 — ab 20% Ladebedarf lohnt sich Anstecken)' },
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
          { key: 'ampel_battery_margin_kwh', label: 'Akku Sicherheitsmarge (kWh)', type: 'number', min: 0, max: 20, step: 0.5, hint: 'Reserve für Waschmaschine etc. bei grün (Standard: 5 kWh)' },
          { key: 'strom_sparen_score', label: 'Strom sparen ab Score', type: 'number', min: 0, max: 100, step: 5, hint: 'Über diesem Score: binary_sensor.tariff_saver_strom_sparen = on → nicht-essentielle Geräte abschalten' },
        ]
      },
      {
        title: 'Heizung / Wärmepumpe',
        icon: 'mdi:heat-pump-outline',
        desc: 'WP-Steuerung mit PV-Überschuss-Heizen, Komfort-Minimum und Standby-Sparfunktion.',
        fields: [
          { key: 'heating_enabled', label: 'Heizungssteuerung aktiv', type: 'toggle' },
          { key: 'heating_wp_entity', label: 'WP Entity (Switch)', type: 'text', hint: 'Entity zum Ein-/Ausschalten der WP' },
          { key: 'heating_block_entity', label: 'Sperr-Entity (WW/Hygiene)', type: 'text', hint: 'binary_sensor/input_boolean: solange on greift das Heizungsmodul nicht ein (z.B. input_boolean.boiler_hygiene_aktiv)' },
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
      },
      {
        title: 'Pool',
        icon: 'mdi:pool',
        desc: 'Freigabe-Signale für Filterpumpe und Pool-Wärmepumpe basierend auf PV-Überschuss.',
        fields: [
          { key: 'pool_filter_min_surplus_kw', label: 'Filter: Min. PV-Überschuss (kW)', type: 'number', min: 0.1, max: 10, step: 0.1, hint: 'Filterpumpe wird bei diesem Überschuss freigegeben' },
          { key: 'pool_wp_min_surplus_kw', label: 'WP: Min. PV-Überschuss (kW)', type: 'number', min: 0.5, max: 20, step: 0.5, hint: 'Pool-WP wird bei diesem Überschuss freigegeben (inkl. Filter)' },
          { key: 'pool_wp_min_runtime', label: 'WP: Min. Laufzeit (Min)', type: 'number', min: 0, max: 60, step: 5, hint: 'Verdichter-Schutz: hält WP-Freigabe nach Start mind. so lange ON (Filter-Off übersteuert)' },
          { key: 'pool_dwell_minutes', label: 'Min. Verweilzeit (Min)', type: 'number', min: 1, max: 30, step: 1, hint: 'Gegen Flattern: Mindestzeit in einem Zustand' },
        ]
      },
      {
        title: 'PV-Überschuss (Dump-Loads)',
        icon: 'mdi:solar-power-variant',
        desc: 'Geräte mit Label pv_ueberschuss werden GEMEINSAM eingeschaltet sobald real Strom ins Netz fliesst (Akku versorgt → echter Überschuss) und wieder aus sobald Netzbezug nötig ist. Neues Gerät = einfach das Label setzen, kein Consumer anlegen. Status: sensor.tariff_saver_pv_uberschuss_status.',
        fields: [
          { key: 'pv_dump_enabled', label: 'Aktiv', type: 'toggle' },
          { key: 'pv_dump_grid_out_entity', label: 'Netzeinspeisung-Sensor (W)', type: 'text', hint: 'Leistung ins Netz, positiv = Einspeisung. Default: sensor.emma_grid_out_power' },
          { key: 'pv_dump_grid_in_entity', label: 'Netzbezug-Sensor (W)', type: 'text', hint: 'Leistung aus dem Netz, positiv = Bezug. Default: sensor.emma_grid_in_power' },
          { key: 'pv_dump_pv_power_entity', label: 'PV-Produktion-Sensor (W)', type: 'text', hint: 'Abend-Backstop: PV-Produktion < 50 W → Geräte aus (Akku deckt sonst die Last, Netzbezug bleibt 0). Default: sensor.emma_pv_output_power' },
          { key: 'pv_dump_on_watts', label: 'Einschalten ab (W Einspeisung)', type: 'number', min: 0, max: 20000, step: 100, hint: 'Über dieser Einspeisung (für Dwell) → alle Geräte ein (Standard 2000)' },
          { key: 'pv_dump_off_watts', label: 'Ausschalten ab (W Netzbezug)', type: 'number', min: 0, max: 20000, step: 100, hint: 'Über diesem Netzbezug (für Dwell) → alle Geräte aus (Standard 500)' },
          { key: 'pv_dump_dwell_seconds', label: 'Dwell (Sek)', type: 'number', min: 0, max: 600, step: 10, hint: 'Schwelle muss so lange anhalten, gegen Wolken-Flackern (Standard 120)' },
          { key: 'pv_dump_min_on_minutes', label: 'Min. Laufzeit (Min)', type: 'number', min: 0, max: 120, step: 5, hint: 'Einmal ein, mind. so lange an — verhindert Flattern (Standard 15)' },
          { key: 'pv_dump_pause_on_vacation', label: 'Im Ferienmodus pausieren', type: 'toggle', hint: 'Dump-Loads bleiben aus solange der Ferienmodus aktiv ist (Standard: an)' },
        ]
      },
      {
        title: 'Standby-Wächter',
        icon: 'mdi:power-sleep',
        desc: 'Switches mit Label standby_aus werden abgeschaltet, wenn sie eingeschaltet sind, aber über die eingestellte Dauer nur noch Standby-Leistung ziehen (Ladegerät fertig, Kompressor vergessen). Der Leistungssensor wird automatisch am gleichen Gerät gefunden. Abschalten erfolgt direkt (Activity-Log + Push). Status: sensor.virtuell_tariff_saver_standby_wachter_status.',
        fields: [
          { key: 'standby_watch_enabled', label: 'Aktiv', type: 'toggle' },
          { key: 'standby_watch_max_watts', label: 'Standby-Schwelle (W)', type: 'number', min: 1, max: 100, step: 1, hint: 'Darunter gilt das Gerät als "dümpelt nur" (Standard 10 W)' },
          { key: 'standby_watch_default_hours', label: 'Standard-Dauer (h)', type: 'number', min: 0.5, max: 24, step: 0.5, hint: 'So lange muss die Leistung ununterbrochen unter der Schwelle bleiben (Standard 2 h)' },
          { key: 'standby_watch_hours', label: 'Pro Gerät', type: 'standby_devices', hint: 'Schwelle (W) und Dauer pro Gerät übersteuerbar — leer/Standard = obige globale Werte' },
        ]
      },
      {
        title: 'Ferienmodus',
        icon: 'mdi:beach',
        desc: 'Overlay-Unterdrückung während längerer Abwesenheit: Consumer mit gesetztem Ferien-Flag (Consumer-Card) und optional die PV-Dump-Loads werden pausiert, solange die Entity den konfigurierten State hat. An/Aus-Flags bleiben unangetastet — kein Restore nötig.',
        fields: [
          { key: 'vacation_entity', label: 'Ferien-Entity', type: 'text', hint: 'z.B. input_select.system_status oder ein input_boolean. Leer = Funktion aus. Nach Änderung: Integration neu laden.' },
          { key: 'vacation_state', label: 'Aktiv bei State', type: 'text', hint: 'State-Wert bei dem der Ferienmodus gilt, z.B. "Längere Abwesenheit". Bei input_boolean: on (Standard)' },
        ]
      },
      {
        title: 'Licht Auto-Off',
        icon: 'mdi:lightbulb-alert',
        desc: 'Vergessene Lichter abschalten ohne Komforteinbusse. Lichter mit Label auto_off_1h / 2h / 4h / 6h werden nach Ablauf der Schwelle per Push gemeldet (Buttons: Aus / +1h / +2h / +4h / +6h). Keine Reaktion → automatisch aus + Bestätigungs-Push.',
        fields: [
          { key: 'light_helper_pattern', label: 'Such-Pattern (Glob)', type: 'text', hint: 'Default: input_select.licht_mode_*. Entities die matchen UND Lichter mit auto_off_*h-Label werden erfasst.' },
          { key: 'light_auto_off_timeout_minutes', label: 'Push-Timeout (Min)', type: 'number', min: 1, max: 60, step: 1, hint: 'Wenn keine Reaktion auf Push, wird das Licht nach dieser Zeit automatisch ausgeschaltet (Standard 10).' },
        ]
      }
    ];

    let html = '';
    for (const g of groups) {
      html += `<div class="group">
        <div class="group-header"><ha-icon icon="${g.icon}"></ha-icon> ${g.title}</div>
        <div class="group-desc">${g.desc}</div>`;
      for (const f of g.fields) {
        let val;
        if (f.type === 'helper_number' || f.type === 'helper_toggle') {
          const st = this._hass.states[f.key];
          val = st ? st.state : '';
        } else {
          val = s[f.key] !== undefined ? s[f.key] : '';
        }
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
        } else if (f.type === 'helper_number') {
          const numVal = val === '' || val === 'unknown' || val === 'unavailable' ? '' : parseFloat(val);
          input = `<input type="number" value="${numVal}" data-key="${f.key}" data-helper="number" min="${f.min||0}" max="${f.max||100}" step="${f.step||1}">`;
        } else if (f.type === 'helper_toggle') {
          const on = val === 'on' || val === true;
          input = `<label class="sw"><input type="checkbox" ${on ? 'checked' : ''} data-key="${f.key}" data-helper="toggle"><span class="sl"></span></label>`;
        } else if (f.type === 'standby_devices') {
          const sbEntity = this._config.standby_entity
            || Object.keys(this._hass.states).find(k => k.startsWith('sensor.') && k.endsWith('standby_wachter_status'));
          const sbState = sbEntity ? this._hass.states[sbEntity] : null;
          const devs = (sbState && sbState.attributes && sbState.attributes.devices) || [];
          const choices = (sbState && sbState.attributes && sbState.attributes.hour_choices) || [1, 2, 4, 8];
          let cur = {};
          try { cur = typeof val === 'string' ? JSON.parse(val || '{}') : (val || {}); } catch (e) { cur = {}; }
          const globalWatts = (sbState && sbState.attributes && sbState.attributes.max_watts) || 10;
          if (!devs.length) {
            input = `<span class="info-val">Keine Geräte mit Label standby_aus gefunden</span>`;
          } else {
            const rows = devs.map(d => {
              const sel = cur[d.entity_id];
              const opts = [`<option value="" ${!sel ? 'selected' : ''}>Standard</option>`]
                .concat(choices.map(h => `<option value="${h}" ${Number(sel) === h ? 'selected' : ''}>${h} h</option>`)).join('');
              const wVal = (d.watts_limit !== null && d.watts_limit !== undefined) ? d.watts_limit : '';
              let info;
              if (!d.power_entity) info = '⚠️ kein Leistungssensor';
              else if (d.state !== 'on') info = 'aus';
              else info = (d.watts !== null && d.watts !== undefined) ? d.watts + ' W' : 'an';
              return `<div class="sb-row">
                <span class="sb-name" title="${this._esc(d.entity_id)}">${this._esc(d.friendly_name)}</span>
                <span class="sb-info">${info}</span>
                <input type="number" class="sb-watts" data-eid="${d.entity_id}" value="${wVal}" min="1" max="500" step="1" placeholder="${globalWatts} W" title="Schwelle in W, leer = Standard (${globalWatts} W)">
                <select class="sb-hours" data-eid="${d.entity_id}">${opts}</select>
              </div>`;
            }).join('');
            input = `<div class="sb-list" data-key="${f.key}">${rows}</div>`;
          }
        } else if (f.type === 'fixed_costs') {
          let items = [];
          try { items = typeof val === 'string' ? JSON.parse(val || '[]') : (Array.isArray(val) ? val : []); } catch (e) { items = []; }
          const rows = items.map((it, idx) => `
            <div class="fc-row" data-idx="${idx}">
              <input type="text" class="fc-label" data-idx="${idx}" value="${this._esc(String(it.label||''))}" placeholder="Bezeichnung">
              <input type="number" class="fc-amount" data-idx="${idx}" value="${it.amount||0}" step="0.01" min="0" placeholder="CHF">
              <select class="fc-period" data-idx="${idx}">
                <option value="monthly" ${(it.period||'monthly')==='monthly'?'selected':''}>Monatlich</option>
                <option value="period" ${(it.period||'')==='period'?'selected':''}>Pro Periode</option>
              </select>
              <button class="fc-del" data-idx="${idx}" title="Löschen">✕</button>
            </div>`).join('');
          input = `<div class="fc-list" data-key="${f.key}">
            ${rows}
            <button class="fc-add">+ Zeile</button>
          </div>`;
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
        .sb-list{display:flex;flex-direction:column;gap:6px;width:100%}
        .sb-row{display:grid;grid-template-columns:1fr 70px 80px 100px;gap:6px;align-items:center}
        .sb-row input.sb-watts{width:100%;padding:6px 8px;border:1px solid var(--divider-color,#444);border-radius:4px;background:var(--input-fill-color,var(--card-background-color,#1c1c1c));color:var(--primary-text-color);font-size:14px;text-align:right;box-sizing:border-box}
        .sb-name{font-size:14px;color:var(--primary-text-color);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .sb-info{font-size:13px;color:var(--secondary-text-color);text-align:right}
        .sb-row select.sb-hours{width:100%;padding:6px 8px;border:1px solid var(--divider-color,#444);border-radius:4px;background:var(--input-fill-color,var(--card-background-color,#1c1c1c));color:var(--primary-text-color);font-size:14px}
        .fc-list{display:flex;flex-direction:column;gap:6px;width:100%}
        .fc-row{display:grid;grid-template-columns:1fr 100px 120px 32px;gap:6px;align-items:center}
        .fc-row input.fc-label{min-width:0}
        .fc-row input.fc-amount{text-align:right;width:100%}
        .fc-row select.fc-period{width:100%;padding:6px 8px;border:1px solid var(--divider-color,#444);border-radius:4px;background:var(--input-fill-color,var(--card-background-color,#1c1c1c));color:var(--primary-text-color);font-size:14px}
        .fc-del{background:transparent;border:1px solid var(--divider-color,#444);border-radius:4px;color:var(--error-color,#c62828);cursor:pointer;font-size:14px;width:32px;height:32px;padding:0}
        .fc-del:hover{background:rgba(200,0,0,.1)}
        .fc-add{background:transparent;border:1px dashed var(--divider-color,#555);border-radius:4px;color:var(--secondary-text-color);cursor:pointer;padding:6px;margin-top:4px;width:fit-content}
        .fc-add:hover{border-color:var(--primary-color,#03a9f4);color:var(--primary-color,#03a9f4)}
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
    this.shadowRoot.querySelectorAll('input[type="number"][data-key]').forEach(el => {
      el.addEventListener('change', e => { self._save(e.target.dataset.key, parseFloat(e.target.value)); });
      el.addEventListener('focus', () => { self._editing = true; });
      el.addEventListener('blur', () => { self._editing = false; });
    });
    this.shadowRoot.querySelectorAll('input[type="text"][data-key]').forEach(el => {
      el.addEventListener('change', e => { self._save(e.target.dataset.key, e.target.value); });
      el.addEventListener('focus', () => { self._editing = true; });
      el.addEventListener('blur', () => { self._editing = false; });
    });
    this.shadowRoot.querySelectorAll('select[data-key]').forEach(el => {
      el.addEventListener('change', e => { self._save(e.target.dataset.key, e.target.value); });
    });
    // Standby-Wächter: Dauer + Schwelle pro Gerät
    this.shadowRoot.querySelectorAll('.sb-list').forEach(list => {
      const key = list.dataset.key;
      list.querySelectorAll('.sb-hours').forEach(el => {
        el.addEventListener('change', () => {
          const result = {};
          list.querySelectorAll('.sb-hours').forEach(sel => {
            if (sel.value) result[sel.dataset.eid] = Number(sel.value);
          });
          self._save(key, JSON.stringify(result));
        });
      });
      list.querySelectorAll('.sb-watts').forEach(el => {
        el.addEventListener('change', () => {
          const result = {};
          list.querySelectorAll('.sb-watts').forEach(inp => {
            const v = parseFloat(inp.value);
            if (inp.value !== '' && !isNaN(v) && v > 0) result[inp.dataset.eid] = v;
          });
          self._save('standby_watch_watts', JSON.stringify(result));
        });
        el.addEventListener('focus', () => { self._editing = true; });
        el.addEventListener('blur', () => { self._editing = false; });
      });
    });
    // Fixkosten-Liste
    this.shadowRoot.querySelectorAll('.fc-list').forEach(list => {
      const key = list.dataset.key;
      const commit = () => {
        const items = [];
        list.querySelectorAll('.fc-row').forEach(r => {
          const lbl = r.querySelector('.fc-label').value.trim();
          const amt = parseFloat(r.querySelector('.fc-amount').value) || 0;
          const per = r.querySelector('.fc-period').value;
          if (lbl && amt > 0) items.push({ label: lbl, amount: amt, period: per });
        });
        self._save(key, JSON.stringify(items));
      };
      list.querySelectorAll('.fc-label, .fc-amount').forEach(el => {
        el.addEventListener('change', commit);
        el.addEventListener('focus', () => { self._editing = true; });
        el.addEventListener('blur', () => { self._editing = false; });
      });
      list.querySelectorAll('.fc-period').forEach(el => el.addEventListener('change', commit));
      list.querySelectorAll('.fc-del').forEach(btn => {
        btn.addEventListener('click', e => {
          e.preventDefault();
          btn.closest('.fc-row').remove();
          commit();
        });
      });
      const addBtn = list.querySelector('.fc-add');
      if (addBtn) addBtn.addEventListener('click', e => {
        e.preventDefault();
        const idx = list.querySelectorAll('.fc-row').length;
        const row = document.createElement('div');
        row.className = 'fc-row';
        row.dataset.idx = idx;
        row.innerHTML = `
          <input type="text" class="fc-label" data-idx="${idx}" placeholder="Bezeichnung">
          <input type="number" class="fc-amount" data-idx="${idx}" value="0" step="0.01" min="0" placeholder="CHF">
          <select class="fc-period" data-idx="${idx}">
            <option value="monthly" selected>Monatlich</option>
            <option value="period">Pro Periode</option>
          </select>
          <button class="fc-del" data-idx="${idx}" title="Löschen">✕</button>`;
        addBtn.parentNode.insertBefore(row, addBtn);
        row.querySelector('.fc-label').addEventListener('change', commit);
        row.querySelector('.fc-amount').addEventListener('change', commit);
        row.querySelector('.fc-period').addEventListener('change', commit);
        row.querySelector('.fc-del').addEventListener('click', ev => { ev.preventDefault(); row.remove(); commit(); });
      });
    });
  }

  _esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
  getCardSize(){return 12}
  static getStubConfig(){return{}}
}

customElements.define('tariff-saver-settings-card', TariffSaverSettingsCard);
window.customCards=window.customCards||[];
window.customCards.push({type:'tariff-saver-settings-card',name:'Tariff Saver Settings',description:'Configure Tariff Saver general settings'});
