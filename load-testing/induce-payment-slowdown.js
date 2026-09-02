// The manual screen-share demo: add 60ms to payment-svc /charge.
//
// This is the canonical case from the brief and eval case 003. The fault is a
// runtime flag with a TTL, so it self-heals - no restart, and a crashed script
// cannot poison later measurements.
import http from 'k6/http';

export const options = { vus: 1, iterations: 1 };

const PAYMENT = __ENV.PAYMENT_URL || 'http://localhost:8001';
const DELAY_MS = Number(__ENV.DELAY_MS || 60);
const TTL_S = Number(__ENV.TTL_S || 300);

export default function () {
  const res = http.post(
    `${PAYMENT}/_internal/fault`,
    JSON.stringify({ delay_ms: DELAY_MS, endpoint: '/charge', ttl_s: TTL_S }),
    { headers: { 'Content-Type': 'application/json' } },
  );
  console.log(
    `injected +${DELAY_MS}ms into payment-svc /charge; expires in ${TTL_S}s -> ${res.status} ${res.body}`,
  );
}
