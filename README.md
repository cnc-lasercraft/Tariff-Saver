# Tariff Saver
Home Assistant integration to analyse, learn from and reduce electricity costs using dynamic tariffs

<img width="1273" height="1187" alt="Bildschirmfoto 2026-02-02 um 16 34 22" src="https://github.com/user-attachments/assets/82cf771d-bee3-4da7-ae9e-aeab4e4ec7d6" />

---

## What it does

Tariff Saver is the optimisation layer on top of a dynamic tariff provider. It takes 15-minute price slots and a
PV forecast and decides **when** each appliance should run, so that grid consumption is avoided where possible and
bundled into the cheapest moment where it is not.

It requires a tariff provider integration; the reference provider is
[EKZ Tariff](https://github.com/cnc-lasercraft/ha-ekz-tariff).

---

## Features

**Scheduling**

- Global scheduler: all consumers optimised together for minimum grid cost, at 15-minute slot granularity
- Rolling plan — re-planned every 15 minutes and on events (new tariffs, SOC change, PV forecast change)
- Three-tier cost model: PV (free) → battery budget (at its replacement price) → grid (slot price)
- Constraints per consumer: priority, run order, time window, duration or energy, maximum grid price score
- Pre-flight dispatcher verifies the plan's assumptions against live power before a consumer actually starts

**Consumer types**

- Scheduled consumers (hot water, pool, hygiene cycles)
- On-demand consumers — appliances that announce themselves (e.g. via a start signal) and get the cheapest window
- Opportunistic consumers that start on real PV surplus
- Battery grid-charging with a forward-projected state-of-charge assessment

**Around the edges**

- PV surplus dump loads: devices tagged with a label are switched on together on genuine grid export
- Standby watchdog: switches drawing only standby power for hours are turned off
- Heating module with seasonal dispatch and price-score thresholds
- Holiday overlay that pauses selected consumers while you are away
- Billing sensor with VAT, fixed costs and period comparison; 400 days of 15-minute readings retained

---

## Disclaimer

This is a personal project shared as-is, not affiliated with any energy provider.
