/**
 * M1 guard - the checkout fan-out must be genuinely concurrent.
 *
 * This is the specification for `order-service.js`, not an afterthought. A
 * sequential fan-out puts every edge on the critical path, makes wall-clock and
 * critical-path latency identical, and silently removes the project's reason to
 * exist. Nothing else in the suite would notice.
 *
 * Two assertions, deliberately:
 *
 *   1. Wall clock under 150ms - the check named in the build plan.
 *   2. The three call intervals actually OVERLAP in time.
 *
 * (2) matters because (1) alone is flaky: on a loaded CI runner a *correct*
 * concurrent fan-out can drift past a fixed millisecond budget, and the usual
 * response to a flaky guard is to weaken it until it stops guarding. Overlap is
 * a structural property - it does not care how slow the machine is.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { makeCreateOrder } from '../src/order-service.js';

// Real baselines from .env.example. payment is slowest, product a close second.
const BASE = { user: 30, product: 45, payment: 90 };
const SEQUENTIAL_MS = BASE.user + BASE.product + BASE.payment; // 165

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Records when each stubbed call starts and ends. */
function makeTracker() {
  const spans = [];
  const track = (name, ms, result) => async () => {
    const start = performance.now();
    await sleep(ms);
    const end = performance.now();
    spans.push({ name, start, end });
    return result;
  };
  return { spans, track };
}

function overlaps(a, b) {
  return a.start < b.end && b.start < a.end;
}

function buildHarness() {
  const { spans, track } = makeTracker();

  const createOrder = makeCreateOrder({
    userClient: { getUser: track('user', BASE.user, { name: 'Test User 1' }) },
    productClient: { checkStock: track('product', BASE.product, { available: true }) },
    paymentClient: {
      charge: track('payment', BASE.payment, { id: 'chg_1', status: 'captured' }),
      refund: async () => ({ ok: true }),
    },
    createOrderRow: async () => ({ id: 'ord_1', status: 'paid', replayed: false }),
  });

  return { createOrder, spans };
}

const INPUT = { userId: 'user-1', items: [{ sku: 'SKU-1', qty: 1 }], amount: 1999 };

test('fan-out is concurrent, not sequential', async () => {
  const { createOrder } = buildHarness();

  const t0 = Date.now();
  const res = await createOrder(INPUT);
  const elapsed = Date.now() - t0;

  assert.equal(res.status, 201);
  // Sequential would be >= 30 + 45 + 90 = 165ms of downstream time.
  assert.ok(
    elapsed < 150,
    `fan-out took ${elapsed}ms; sequential would be >=${SEQUENTIAL_MS}ms. ` +
      'The fan-out has regressed to sequential awaits (M1).',
  );
});

test('all three downstream calls overlap in time', async () => {
  const { createOrder, spans } = buildHarness();
  await createOrder(INPUT);

  assert.equal(spans.length, 3, 'expected exactly three downstream calls');

  const byName = Object.fromEntries(spans.map((s) => [s.name, s]));
  for (const [a, b] of [
    ['user', 'product'],
    ['user', 'payment'],
    ['product', 'payment'],
  ]) {
    assert.ok(
      overlaps(byName[a], byName[b]),
      `${a} and ${b} did not overlap - the fan-out is running them in sequence (M1). ` +
        `${a}=[${byName[a].start.toFixed(1)}, ${byName[a].end.toFixed(1)}] ` +
        `${b}=[${byName[b].start.toFixed(1)}, ${byName[b].end.toFixed(1)}]`,
    );
  }
});

test('the guard actually fails on a sequential implementation', async () => {
  // Proving the guard works. A test that has never failed is not evidence.
  const { spans, track } = makeTracker();
  const user = track('user', BASE.user, { name: 'u' });
  const product = track('product', BASE.product, { available: true });
  const payment = track('payment', BASE.payment, { id: 'c', status: 'captured' });

  const t0 = Date.now();
  await user();
  await product();
  await payment();
  const elapsed = Date.now() - t0;

  assert.ok(elapsed >= SEQUENTIAL_MS - 15, `sequential control took only ${elapsed}ms`);

  const byName = Object.fromEntries(spans.map((s) => [s.name, s]));
  assert.ok(
    !overlaps(byName.user, byName.payment),
    'sequential control unexpectedly overlapped - the overlap check is broken',
  );
});

test('a failed sibling compensates a successful charge', async () => {
  const refunded = [];
  const createOrder = makeCreateOrder({
    userClient: { getUser: async () => ({ name: 'u' }) },
    productClient: {
      checkStock: async () => {
        await sleep(5);
        throw new Error('product-svc unavailable');
      },
    },
    paymentClient: {
      charge: async () => ({ id: 'chg_leak', status: 'captured' }),
      refund: async (id) => {
        refunded.push(id);
        return { ok: true };
      },
    },
    createOrderRow: async () => ({ id: 'x', status: 'paid' }),
  });

  await assert.rejects(() => createOrder(INPUT), /product-svc unavailable/);
  await sleep(20); // compensation is fire-and-forget off the original promise
  assert.deepEqual(refunded, ['chg_leak'], 'a captured charge was left un-refunded');
});
