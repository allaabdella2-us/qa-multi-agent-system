-- 001_init.sql — initial schema for the Corvid Orders API.

BEGIN;

CREATE TABLE organizations (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id             SERIAL PRIMARY KEY,
    org_id         INTEGER NOT NULL REFERENCES organizations (id) ON DELETE CASCADE,
    email          TEXT NOT NULL UNIQUE,
    password_hash  TEXT NOT NULL,
    role           TEXT NOT NULL CHECK (role IN ('admin', 'member', 'viewer'))
);

CREATE INDEX users_org_id_idx ON users (org_id);

CREATE TABLE orders (
    id           SERIAL PRIMARY KEY,
    org_id       INTEGER NOT NULL REFERENCES organizations (id) ON DELETE CASCADE,
    reference    TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'draft'
                 CHECK (status IN ('draft', 'placed', 'paid', 'refunded', 'cancelled')),
    total_cents  INTEGER NOT NULL DEFAULT 0 CHECK (total_cents >= 0),
    currency     CHAR(3) NOT NULL DEFAULT 'USD',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, reference)
);

CREATE INDEX orders_org_id_idx ON orders (org_id);
CREATE INDEX orders_org_id_status_idx ON orders (org_id, status);
CREATE INDEX orders_created_at_idx ON orders (created_at DESC);

CREATE TABLE order_items (
    id                SERIAL PRIMARY KEY,
    order_id          INTEGER NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    sku               TEXT NOT NULL,
    description       TEXT NOT NULL,
    quantity          INTEGER NOT NULL CHECK (quantity >= 1),
    unit_price_cents  INTEGER NOT NULL CHECK (unit_price_cents >= 0)
);

CREATE INDEX order_items_order_id_idx ON order_items (order_id);

CREATE TABLE invoices (
    id            SERIAL PRIMARY KEY,
    order_id      INTEGER NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    number        TEXT NOT NULL UNIQUE,
    amount_cents  INTEGER NOT NULL CHECK (amount_cents >= 0),
    currency      CHAR(3) NOT NULL,
    issued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    status        TEXT NOT NULL CHECK (status IN ('open', 'paid', 'void'))
);

CREATE INDEX invoices_order_id_idx ON invoices (order_id);
CREATE INDEX invoices_status_idx ON invoices (status);

COMMIT;
