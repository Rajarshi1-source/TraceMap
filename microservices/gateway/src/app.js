import express from 'express';

const SERVICE = process.env.SERVICE_NAME ?? 'gateway-svc';
const VERSION = process.env.SERVICE_VERSION ?? 'dev-local';
const ORDER_URL = process.env.ORDER_URL ?? 'http://order-svc:3001';
const PRODUCT_URL = process.env.PRODUCT_URL ?? 'http://product-svc:8080';

const TIMEOUT_MS = 3000;
const CATALOG_TTL_MS = 300_000;

/** Tiny in-process cache. Redis takes this over in Phase 5. */
const cache = new Map();
const cacheGet = (k) => {
  const hit = cache.get(k);
  if (!hit) return null;
  if (Date.now() > hit.expiresAt) return hit.value; // stale but serveable
  return hit.value;
};
const cacheSet = (k, value, ttlMs) => cache.set(k, { value, expiresAt: Date.now() + ttlMs });

async function proxy(url, { method = 'GET', body, timeoutMs = TIMEOUT_MS } = {}) {
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
    return { status: res.status, body: text ? JSON.parse(text) : null };
  } finally {
    clearTimeout(timer);
  }
}

export function buildApp() {
  const app = express();
  app.use(express.json({ limit: '64kb' }));

  // M17: process liveness only - no dependency checks here.
  app.get('/healthz', (_req, res) => {
    res.json({ status: 'ok', service: SERVICE, version: VERSION });
  });

  // Dependencies are checked HERE. order-svc's own /readyz reports on the
  // database, so this chains correctly without gateway knowing about Postgres.
  app.get('/readyz', async (_req, res) => {
    try {
      const r = await proxy(`${ORDER_URL}/readyz`, { timeoutMs: 2000 });
      if (r.status !== 200) {
        return res.status(503).json({ status: 'not_ready', reason: 'order_svc_not_ready' });
      }
      res.json({ status: 'ready' });
    } catch (err) {
      res.status(503).json({ status: 'not_ready', reason: 'order_svc_unreachable', detail: err.message });
    }
  });

  // ---- the demo path ----------------------------------------------------
  app.post('/checkout', async (req, res) => {
    const { userId, items, amount } = req.body ?? {};
    if (!userId || !Array.isArray(items) || items.length === 0 || typeof amount !== 'number') {
      return res.status(400).json({ error: 'bad_request' });
    }
    try {
      const r = await proxy(`${ORDER_URL}/orders`, { method: 'POST', body: { userId, items, amount } });
      res.status(r.status).json(r.body);
    } catch (err) {
      res.status(504).json({ error: 'order_svc_timeout', detail: err.message });
    }
  });

  // ---- cacheable read path with graceful degradation --------------------
  app.get('/products', async (_req, res) => {
    try {
      const r = await proxy(`${PRODUCT_URL}/products`);
      if (r.status !== 200) throw new Error(`product-svc returned ${r.status}`);
      cacheSet('catalog', r.body, CATALOG_TTL_MS);
      res.json(r.body);
    } catch (err) {
      const stale = cacheGet('catalog');
      if (stale) {
        // Serving stale beats serving nothing - and say so in a header, so a
        // caller can tell the difference. Phase 2 records this as a span event.
        console.log(JSON.stringify({ event: 'degraded', reason: 'product_svc_unavailable', service: SERVICE }));
        return res.set('X-Stale', 'true').json(stale);
      }
      res.status(503).json({ error: 'catalog_unavailable', detail: err.message });
    }
  });

  app.use((err, _req, res, _next) => {
    console.error(JSON.stringify({ level: 'error', service: SERVICE, msg: err.message }));
    res.status(500).json({ error: 'internal' });
  });

  return app;
}
