// Tariff Saver: shared price-curve fetcher with short TTL cache.
// ApexCharts data_generators call window.tsPriceCurve(hass) to avoid
// flooding the WS with one request per series per refresh.

(function () {
  const TTL_MS = 5000;
  let cache = null;
  let cacheAt = 0;
  let inflight = null;

  window.tsPriceCurve = async function (hass) {
    const now = Date.now();
    if (cache && (now - cacheAt) < TTL_MS) return cache;
    if (inflight) return inflight;
    inflight = hass.callWS({ type: "tariff_saver/price_curve" })
      .then((res) => {
        cache = res || { slots: [], baseline_chf_per_kwh: null, interval_minutes: 15 };
        cacheAt = Date.now();
        inflight = null;
        return cache;
      })
      .catch((err) => {
        inflight = null;
        console.error("tsPriceCurve WS failed:", err);
        return cache || { slots: [], baseline_chf_per_kwh: null, interval_minutes: 15 };
      });
    return inflight;
  };
})();
