/**
 * THE CONCURRENT FAN-OUT (M1) - the single most important file in Phase 1.
 *
 * If this fan-out ever becomes sequential, every edge lands on the critical
 * path, wall-clock and critical-path latency become identical, and the
 * project's flagship differentiator has nothing to demonstrate. Nothing else
 * would fail: no test breaks, no error appears, the demo just quietly stops
 * making its point. That is why `test/fanout.test.js` exists and why it runs in
 * CI.
 *
 * Dependencies are injected so the guard test can drive the orchestration with
 * stub clients at the real baseline latencies, without a network.
 */
import { createHash } from 'node:crypto';

/** Stable cart hash so a retry produces the same idempotency key. */
export function hashCart(items) {
  const canonical = [...items]
    .map((i) => `${i.sku}x${i.qty}`)
    .sort()
    .join('|');
  return createHash('sha256').update(canonical).digest('hex').slice(0, 16);
}

export function makeCreateOrder({ userClient, productClient, paymentClient, createOrderRow }) {
  /**
   * @returns {Promise<{status:number, body:object}>}
   */
  return async function createOrder({ userId, items, amount }) {
    const idemKey = `${userId}:${hashCart(items)}`;

    // ---------------------------------------------------------------------
    // The fan-out. All three calls are STARTED here, before anything is
    // awaited - that is what makes them concurrent, and writing it this way
    // makes an accidental `await` on one line visible in review.
    //
    // NEVER refactor this into:
    //     const user   = await userClient.getUser(userId);
    //     const stock  = await productClient.checkStock(items);
    //     const charge = await paymentClient.charge(userId, amount);
    // ---------------------------------------------------------------------
    const userP = userClient.getUser(userId);
    const stockP = productClient.checkStock(items);
    const chargeP = paymentClient.charge(userId, amount);

    let user, stock, charge;
    try {
      [user, stock, charge] = await Promise.all([userP, stockP, chargeP]);
    } catch (err) {
      // Saga compensation on partial failure: if the charge succeeded while a
      // sibling leg failed, the money must not be left captured. Promise.all
      // discards the other results on rejection, so compensate off the
      // original promise.
      chargeP
        .then((c) => (c?.id ? paymentClient.refund(c.id) : null))
        .catch(() => {}); // best-effort; never mask the original error
      throw err;
    }

    if (!stock.available) {
      // Compensating action for a business-level failure.
      await paymentClient.refund(charge.id).catch(() => {});
      return { status: 409, body: { error: 'out_of_stock', skus: stock.unavailable ?? [] } };
    }

    const order = await createOrderRow({
      userHandle: userId,
      amountCents: amount,
      items,
      idemKey,
      paid: charge.status === 'captured',
    });

    if (order.notFound) {
      await paymentClient.refund(charge.id).catch(() => {});
      return { status: 404, body: { error: 'user_not_found' } };
    }

    return {
      status: order.replayed ? 200 : 201,
      body: {
        orderId: order.id,
        status: order.status,
        user: user.name,
        chargeId: charge.id,
        replayed: order.replayed,
      },
    };
  };
}
