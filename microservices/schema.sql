-- TraceMap application schema (PostgreSQL 18).
--
-- Deliberately small: this is a trace generator, not an e-commerce product.
-- Real foreign keys and a real transaction around order creation, because a DB
-- span with a rollback is a more interesting trace than one without.

CREATE TABLE users (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    handle     TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    email      TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE products (
    sku         TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    price_cents INT  NOT NULL CHECK (price_cents >= 0),
    stock       INT  NOT NULL CHECK (stock >= 0)
);

CREATE TABLE orders (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id),
    amount_cents INT  NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('pending','paid','failed','refunded')),
    -- Retries must not double-charge. Keyed on (user_id, cart_hash) upstream.
    idem_key     TEXT UNIQUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE order_items (
    order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    sku      TEXT NOT NULL REFERENCES products(sku),
    qty      INT  NOT NULL CHECK (qty > 0),
    PRIMARY KEY (order_id, sku)
);

CREATE TABLE payments (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id   UUID REFERENCES orders(id),
    status     TEXT NOT NULL CHECK (status IN ('captured','refunded','failed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ON orders (user_id, created_at DESC);

-- --------------------------------------------------------------------------
-- Seed data. The k6 script draws from user-1..50 and SKU-1..20, so both ranges
-- must exist or the load test measures 404s instead of latency.
-- --------------------------------------------------------------------------

INSERT INTO users (handle, name, email)
SELECT 'user-' || i,
       'Test User ' || i,
       'user' || i || '@tracemap.local'
FROM generate_series(1, 50) AS i;

INSERT INTO products (sku, name, price_cents, stock)
SELECT 'SKU-' || i,
       'Product ' || i,
       499 + (i * 100),
       1000000            -- effectively unlimited: stock-outs are not the demo
FROM generate_series(1, 20) AS i;
