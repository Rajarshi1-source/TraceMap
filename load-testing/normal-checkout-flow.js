import http from 'k6/http';
import { check, sleep } from 'k6';

// SHORT=1 runs a brief constant-rate burst instead of the 20-minute diurnal
// ramp. Used by the Phase 1 exit check and the Phase 7 CI SLO gate, where a
// full ramp would dominate the pipeline.
const SHORT = __ENV.SHORT === '1';
const RPS = Number(__ENV.RPS || 20);

export const options = {
  scenarios: SHORT
    ? {
        burst: {
          executor: 'constant-arrival-rate',
          rate: RPS,
          timeUnit: '1s',
          duration: __ENV.DURATION || '60s',
          preAllocatedVUs: 30,
          maxVUs: 100,
        },
      }
    : {
    // Mild diurnal variation on purpose. A detector tuned against perfectly
    // flat traffic produces false positives the moment real traffic breathes,
    // and the EWMA baselines in Phase 6 would look better than they are.
    baseline: {
      executor: 'ramping-arrival-rate',
      startRate: 8,
      timeUnit: '1s',
      preAllocatedVUs: 20,
      maxVUs: 60,
      stages: [
        { target: 12, duration: '5m' },
        { target: 20, duration: '5m' },
        { target: 12, duration: '5m' },
        { target: 8, duration: '5m' },
      ],
    },
  },
  // These double as the CI SLO gate in Phase 7.
  thresholds: {
    http_req_duration: ['p(95)<400'],
    http_req_failed: ['rate<0.01'],
  },
};

const BASE = __ENV.BASE_URL || 'http://localhost:3000';

export default function () {
  // 30% browse - exercises the cacheable read path and its degradation.
  if (Math.random() < 0.3) {
    const res = http.get(`${BASE}/products`);
    check(res, { 'browse 200': (r) => r.status === 200 });
    sleep(0.5 + Math.random());
    return;
  }

  // 70% checkout - the demo path, and the trace shape the whole project is about.
  // Cart size stays at or below PRODUCT_N_PLUS_ONE_THRESHOLD most of the time;
  // roughly one in five carts crosses it and triggers the N+1 in product-svc.
  const cartSize = Math.random() < 0.2 ? 5 : 1 + Math.floor(Math.random() * 3);
  const items = [];
  for (let i = 0; i < cartSize; i++) {
    items.push({ sku: `SKU-${1 + Math.floor(Math.random() * 20)}`, qty: 1 });
  }

  const payload = JSON.stringify({
    userId: `user-${1 + Math.floor(Math.random() * 50)}`,
    items,
    amount: 1999,
  });

  const res = http.post(`${BASE}/checkout`, payload, {
    headers: { 'Content-Type': 'application/json' },
  });
  check(res, { 'checkout 2xx': (r) => r.status >= 200 && r.status < 300 });
  sleep(0.5 + Math.random());
}
