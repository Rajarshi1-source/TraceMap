/**
 * PostgreSQL access for order-svc.
 *
 * M28: every query is parameterised, and the text we ever log or attach to a
 * span is the parameterised form - never the interpolated one. Spans are
 * retained for days and readable by anyone with dashboard access, so a literal
 * card amount or email in `db.statement` is a data leak with a long tail.
 */
import pg from 'pg';

const { Pool } = pg;

// Bulkhead: order-svc gets its own bounded pool, so one slow dependency cannot
// starve the others of connections.
export const pool = new Pool({
  host: process.env.PGHOST ?? 'postgres',
  port: Number(process.env.PGPORT ?? 5432),
  user: process.env.PGUSER ?? 'tracemap',
  password: process.env.PGPASSWORD ?? 'change-me-locally',
  database: process.env.PGDATABASE ?? 'tracemap',
  max: 10,
  idleTimeoutMillis: 30_000,
  connectionTimeoutMillis: 3_000,
});

pool.on('error', (err) => {
  console.error(JSON.stringify({ level: 'error', msg: 'idle pg client error', err: err.message }));
});

/** Readiness only. Never called from /healthz - see M17. */
export async function ping() {
  const res = await pool.query('SELECT 1 AS ok');
  return res.rows[0]?.ok === 1;
}

const INSERT_ORDER = `
  INSERT INTO orders (user_id, amount_cents, status, idem_key)
  VALUES ($1, $2, $3, $4)
  ON CONFLICT (idem_key) DO NOTHING
  RETURNING id, status`;

const SELECT_BY_IDEM = 'SELECT id, status FROM orders WHERE idem_key = $1';
const INSERT_ITEM = 'INSERT INTO order_items (order_id, sku, qty) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING';
const INSERT_PAYMENT = 'INSERT INTO payments (order_id, status) VALUES ($1, $2)';
const SELECT_USER_ID = 'SELECT id FROM users WHERE handle = $1';

/**
 * Create an order inside a real transaction, with a real rollback path.
 * A DB span with a rollback is a more interesting trace than one without.
 */
export async function createOrderRow({ userHandle, amountCents, items, idemKey, paid }) {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    const userRes = await client.query(SELECT_USER_ID, [userHandle]);
    if (userRes.rowCount === 0) {
      await client.query('ROLLBACK');
      return { notFound: true };
    }
    const userId = userRes.rows[0].id;

    let orderRes = await client.query(INSERT_ORDER, [
      userId,
      amountCents,
      paid ? 'paid' : 'failed',
      idemKey,
    ]);

    // Idempotency: a retry with the same key must not create a second order.
    if (orderRes.rowCount === 0) {
      const existing = await client.query(SELECT_BY_IDEM, [idemKey]);
      await client.query('COMMIT');
      return { id: existing.rows[0].id, status: existing.rows[0].status, replayed: true };
    }

    const orderId = orderRes.rows[0].id;
    for (const item of items) {
      await client.query(INSERT_ITEM, [orderId, item.sku, item.qty]);
    }
    await client.query(INSERT_PAYMENT, [orderId, paid ? 'captured' : 'failed']);

    await client.query('COMMIT');
    return { id: orderId, status: orderRes.rows[0].status, replayed: false };
  } catch (err) {
    await client.query('ROLLBACK').catch(() => {});
    throw err;
  } finally {
    client.release();
  }
}

export async function close() {
  await pool.end();
}
