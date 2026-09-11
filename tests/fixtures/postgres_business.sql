-- Public synthetic fixture; only scripts/setup_postgres.py may initialize it.
-- All DDL/data/grants are atomic. No DROP, replacement, or incremental repair.
BEGIN;
SET LOCAL search_path = pg_catalog, business;
CREATE TABLE business.customers (
    id bigint PRIMARY KEY,
    customer_code varchar(32) NOT NULL UNIQUE,
    display_name varchar(80) NOT NULL,
    region varchar(16),
    created_at timestamp(6) NOT NULL
);
CREATE INDEX idx_customers_region_created
    ON business.customers (region, created_at);
CREATE TABLE business.orders (
    id bigint PRIMARY KEY,
    order_no varchar(32) NOT NULL UNIQUE,
    customer_id bigint NOT NULL REFERENCES business.customers (id),
    status varchar(16) NOT NULL CHECK (status IN ('pending', 'paid', 'cancelled', 'refunded')),
    total_amount numeric(12, 2) NOT NULL CHECK (total_amount >= 0),
    created_at timestamp(6) NOT NULL,
    paid_at timestamptz(6)
);
CREATE INDEX idx_orders_customer_created ON business.orders (customer_id, created_at);
CREATE INDEX idx_orders_status_created ON business.orders (status, created_at);
CREATE TABLE business.order_items (
    order_id bigint NOT NULL REFERENCES business.orders (id),
    line_no smallint NOT NULL,
    product_sku varchar(32) NOT NULL,
    quantity integer NOT NULL CHECK (quantity > 0),
    unit_price numeric(12, 2) NOT NULL CHECK (unit_price >= 0),
    discount_amount numeric(12, 2) NOT NULL DEFAULT 0
        CHECK (discount_amount >= 0 AND discount_amount <= quantity * unit_price),
    PRIMARY KEY (order_id, line_no)
);
CREATE INDEX idx_order_items_product_order ON business.order_items (product_sku, order_id);

-- Customer 4 has no orders; customer 3 has an unknown region.
INSERT INTO business.customers VALUES
    (1, 'SYN-C001', 'Customer Alpha', 'east', '2026-01-01 09:00:00'),
    (2, 'SYN-C002', 'Customer Beta', 'west', '2026-01-02 09:00:00'),
    (3, 'SYN-C003', 'Customer Gamma', NULL, '2026-01-03 09:00:00'),
    (4, 'SYN-C004', 'Customer Delta', 'east', '2026-01-04 09:00:00'),
    (5, 'SYN-C005', 'Customer Epsilon', 'north', '2026-01-05 09:00:00');

-- Created time and payment time have different month boundaries.
-- The +08:00 payment input denotes 2026-02-01 00:01:00 UTC.
-- Revenue means status='paid'; cancelled/refunded totals are excluded.
INSERT INTO business.orders VALUES
    (1001, 'SYN-O1001', 1, 'paid', 100.00,
     '2026-01-31 23:59:59', '2026-02-01 08:01:00+08:00'),
    (1002, 'SYN-O1002', 1, 'paid', 30.00,
     '2026-02-01 00:00:00', '2026-02-01 00:02:00+00:00'),
    (1003, 'SYN-O1003', 2, 'cancelled', 50.00,
     '2026-02-02 10:00:00', NULL),
    (1004, 'SYN-O1004', 3, 'paid', 0.00,
     '2026-02-02 11:00:00', '2026-02-02 11:00:00+00:00'),
    (1005, 'SYN-O1005', 5, 'pending', 0.00,
     '2026-02-03 12:00:00', NULL),
    (1006, 'SYN-O1006', 2, 'refunded', 50.00,
     '2026-02-04 13:00:00', '2026-02-04 13:05:00+00:00');
INSERT INTO business.order_items VALUES
    (1001, 1, 'SYN-SKU-A', 2, 40.00, 0.00),
    (1001, 2, 'SYN-SKU-B', 1, 20.00, 0.00),
    (1002, 1, 'SYN-SKU-A', 1, 35.00, 5.00),
    (1003, 1, 'SYN-SKU-C', 1, 50.00, 0.00),
    (1004, 1, 'SYN-SKU-GIFT', 1, 0.00, 0.00),
    (1006, 1, 'SYN-SKU-C', 1, 50.00, 0.00);

-- A separate risk fixture, deliberately absent from the normal app allowlist.
CREATE TABLE business.pg_scan_probe (id integer PRIMARY KEY, bucket smallint NOT NULL);
INSERT INTO business.pg_scan_probe SELECT n, 1 FROM generate_series(1, 100001) AS n;
GRANT SELECT ON business.customers, business.orders, business.order_items,
    business.pg_scan_probe TO db_agent_reader;
ANALYZE business.customers;
ANALYZE business.orders;
ANALYZE business.order_items;
ANALYZE business.pg_scan_probe;
COMMIT;
