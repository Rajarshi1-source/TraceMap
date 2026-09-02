import express from 'express';

import { userClient, productClient, paymentClient } from './clients.js';
import { createOrderRow, ping } from './db.js';
import { makeCreateOrder } from './order-service.js';
import { faultConfig, sleep } from './faults.js';

const SERVICE = process.env.SERVICE_NAME ?? 'order-svc';
const VERSION = process.env.SERVICE_VERSION ?? 'dev-local';

export function buildApp() {
  const app = express();
  app.use(express.json({ limit: '64kb' }));

  const createOrder = makeCreateOrder({
    userClient,
    productClient,
    paymentClient,
    createOrderRow,
  });

  // ---- probes -----------------------------------------------------------
  // M17: /healthz answers "should I be restarted?" and must check ONLY that
  // the process is alive. Checking the database here means one slow query
  // fails liveness on every replica at once, Kubernetes restarts them all, and
  // a degradation becomes an outage.
  app.get('/healthz', (_req, res) => {
    res.json({ status: 'ok', service: SERVICE, version: VERSION });
  });

  // /readyz answers "should I get traffic?" - dependencies belong here.
  app.get('/readyz', async (_req, res) => {
    try {
      await ping();
      res.json({ status: 'ready' });
    } catch (err) {
      res.status(503).json({ status: 'not_ready', reason: 'db_unreachable', detail: err.message });
    }
  });

  // ---- fault control plane (M23) ----------------------------------------
  // Internal only. This must never be reachable from the public ingress.
  app.post('/_internal/fault', (req, res) => {
    const { delay_ms = 0, error_rate = 0, cpu_burn_ms = 0, endpoint = '', ttl_s = 600 } = req.body ?? {};
    faultConfig.set(
      { delayMs: Number(delay_ms), errorRate: Number(error_rate), cpuBurnMs: Number(cpu_burn_ms) },
      endpoint,
      Number(ttl_s),
    );
    res.json({ ok: true, ...faultConfig.describe() });
  });

  app.delete('/_internal/fault', (_req, res) => {
    faultConfig.clear();
    res.json({ ok: true, ...faultConfig.describe() });
  });

  app.get('/_internal/fault', (_req, res) => res.json(faultConfig.describe()));

  // ---- the fan-out ------------------------------------------------------
  app.post('/orders', async (req, res, next) => {
    const { userId, items, amount } = req.body ?? {};
    if (!userId || !Array.isArray(items) || items.length === 0 || typeof amount !== 'number') {
      return res.status(400).json({ error: 'bad_request' });
    }

    const fault = faultConfig.forEndpoint('/orders');
    if (fault.delayMs) await sleep(fault.delayMs);
    if (fault.errorRate && Math.random() < fault.errorRate) {
      return res.status(502).json({ error: 'injected_fault' });
    }

    try {
      const result = await createOrder({ userId, items, amount });
      res.status(result.status).json(result.body);
    } catch (err) {
      next(err);
    }
  });

  // ---- error handler ----------------------------------------------------
  app.use((err, _req, res, _next) => {
    const status = err.status && err.status >= 400 && err.status < 600 ? 502 : 500;
    console.error(JSON.stringify({ level: 'error', service: SERVICE, msg: err.message }));
    res.status(status).json({ error: 'upstream_failure', detail: err.message });
  });

  return app;
}
