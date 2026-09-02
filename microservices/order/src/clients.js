/**
 * Downstream clients: timeouts, retry with jitter, circuit breaker, bulkhead.
 *
 * These are what make the traces interesting in Phase 2 and what the resilience
 * questions in an interview are about.
 */
import CircuitBreaker from 'opossum';

const USER_URL = process.env.USER_URL ?? 'http://user-svc:8000';
const PRODUCT_URL = process.env.PRODUCT_URL ?? 'http://product-svc:8080';
const PAYMENT_URL = process.env.PAYMENT_URL ?? 'http://payment-svc:8001';

// Payment gets the tightest timeout: it is the slowest hop by design, so a
// generous timeout there would let one sick dependency hold the whole fan-out.
export const TIMEOUTS = { user: 2000, product: 2000, payment: 500 };

const RETRYABLE_STATUS = new Set([502, 503, 504]);

class HttpError extends Error {
  constructor(status, body, url) {
    super(`HTTP ${status} from ${url}`);
    this.name = 'HttpError';
    this.status = status;
    this.body = body;
  }
}

function isRetryable(err) {
  if (err?.name === 'AbortError') return true; // timeout
  if (err instanceof HttpError) return RETRYABLE_STATUS.has(err.status);
  return err?.code === 'ECONNREFUSED' || err?.code === 'ECONNRESET';
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function request(url, { method = 'GET', body, timeoutMs } = {}) {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      method,
      signal: ac.signal,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    const text = await res.text();
    const parsed = text ? JSON.parse(text) : null;
    if (!res.ok) throw new HttpError(res.status, parsed, url);
    return parsed;
  } finally {
    clearTimeout(timer);
  }
}

export async function callWithRetry(fn, { attempts = 3 } = {}) {
  let lastErr;
  for (let i = 0; i < attempts; i++) {
    try {
      return await fn();
    } catch (err) {
      lastErr = err;
      if (!isRetryable(err) || i === attempts - 1) throw err;
      // Exponential backoff WITH jitter. Without jitter, retries synchronise
      // across replicas and you get a thundering herd on an already-sick
      // service - the retry storm becomes the outage.
      const base = 100 * 2 ** i;
      await sleep(base / 2 + Math.random() * base);
    }
  }
  throw lastErr;
}

export const userClient = {
  getUser: (userId) =>
    callWithRetry(() =>
      request(`${USER_URL}/users/${encodeURIComponent(userId)}`, {
        timeoutMs: TIMEOUTS.user,
      }),
    ),
};

export const productClient = {
  checkStock: (items) =>
    callWithRetry(() =>
      request(`${PRODUCT_URL}/stock/check`, {
        method: 'POST',
        body: { items },
        timeoutMs: TIMEOUTS.product,
      }),
    ),
};

const chargeBreaker = new CircuitBreaker(
  (userId, amount) =>
    callWithRetry(() =>
      request(`${PAYMENT_URL}/charge`, {
        method: 'POST',
        body: { userId, amount },
        timeoutMs: TIMEOUTS.payment,
      }),
    ),
  {
    errorThresholdPercentage: 50, // open at 50% errors
    resetTimeout: 30_000, // half-open probe after 30s
    rollingCountTimeout: 60_000, // over a 1 min window
    volumeThreshold: 10,
  },
);

// Breaker transitions belong in the trace. A waterfall showing the breaker
// opening mid-incident is a strong artifact - it makes the resilience work
// visible rather than merely claimed. Phase 2 attaches these as span events;
// for now they are structured logs carrying the same fields.
export const breakerEvents = [];
for (const ev of ['open', 'halfOpen', 'close']) {
  chargeBreaker.on(ev, () => {
    const evt = { event: 'circuit_breaker', state: ev, target: 'payment-svc', at: Date.now() };
    breakerEvents.push(evt);
    console.log(JSON.stringify(evt));
  });
}

export const paymentClient = {
  charge: (userId, amount) => chargeBreaker.fire(userId, amount),
  refund: (chargeId) =>
    request(`${PAYMENT_URL}/refund`, {
      method: 'POST',
      body: { chargeId },
      timeoutMs: TIMEOUTS.payment,
    }),
  breaker: chargeBreaker,
};

export { HttpError };
