-- Public synthetic data for local metadata and SQL behavior tests.
-- Apply only through scripts/seed_local_mysql.py to an empty set of these tables.
-- DDL commits independently; this file never drops or replaces existing objects.

CREATE TABLE customers (
    id BIGINT UNSIGNED NOT NULL,
    customer_code VARCHAR(32) NOT NULL,
    display_name VARCHAR(80) NOT NULL COMMENT 'Synthetic customer label, not a real person',
    region VARCHAR(16) NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_customers_code (customer_code),
    KEY idx_customers_region_created (region, created_at)
) ENGINE=InnoDB COMMENT='Synthetic customer accounts';

CREATE TABLE orders (
    id BIGINT UNSIGNED NOT NULL,
    order_no VARCHAR(32) NOT NULL,
    customer_id BIGINT UNSIGNED NOT NULL,
    status VARCHAR(16) NOT NULL,
    total_amount DECIMAL(12, 2) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    paid_at DATETIME(6) NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_orders_number (order_no),
    KEY idx_orders_customer_created (customer_id, created_at),
    KEY idx_orders_status_created (status, created_at),
    CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers (id),
    CONSTRAINT chk_orders_amount CHECK (total_amount >= 0),
    CONSTRAINT chk_orders_status CHECK (status IN ('pending', 'paid', 'cancelled', 'refunded'))
) ENGINE=InnoDB COMMENT='Synthetic orders; cancelled and refunded totals are not revenue';

CREATE TABLE order_items (
    order_id BIGINT UNSIGNED NOT NULL,
    line_no SMALLINT UNSIGNED NOT NULL,
    product_sku VARCHAR(32) NOT NULL,
    quantity INT UNSIGNED NOT NULL,
    unit_price DECIMAL(12, 2) NOT NULL,
    discount_amount DECIMAL(12, 2) NOT NULL DEFAULT 0,
    PRIMARY KEY (order_id, line_no),
    KEY idx_order_items_product_order (product_sku, order_id),
    CONSTRAINT fk_order_items_order FOREIGN KEY (order_id) REFERENCES orders (id),
    CONSTRAINT chk_order_items_quantity CHECK (quantity > 0),
    CONSTRAINT chk_order_items_price CHECK (unit_price >= 0),
    CONSTRAINT chk_order_items_discount CHECK (
        discount_amount >= 0 AND discount_amount <= quantity * unit_price
    )
) ENGINE=InnoDB COMMENT='Synthetic order lines; discount_amount applies to the whole line';

START TRANSACTION;

-- Includes a customer without orders and a missing region.
INSERT INTO customers (id, customer_code, display_name, region, created_at) VALUES
    (1, 'SYN-C001', 'Customer Alpha', 'east', '2026-01-01 09:00:00'),
    (2, 'SYN-C002', 'Customer Beta', 'west', '2026-01-02 09:00:00'),
    (3, 'SYN-C003', 'Customer Gamma', NULL, '2026-01-03 09:00:00'),
    (4, 'SYN-C004', 'Customer Delta', 'east', '2026-01-04 09:00:00'),
    (5, 'SYN-C005', 'Customer Epsilon', 'north', '2026-01-05 09:00:00');

-- Includes a month boundary, cancellation, refund, zero total, and a pending empty order.
INSERT INTO orders (
    id, order_no, customer_id, status, total_amount, created_at, paid_at
) VALUES
    (1001, 'SYN-O1001', 1, 'paid', 100.00, '2026-01-31 23:59:59', '2026-02-01 00:01:00'),
    (1002, 'SYN-O1002', 1, 'paid', 30.00, '2026-02-01 00:00:00', '2026-02-01 00:02:00'),
    (1003, 'SYN-O1003', 2, 'cancelled', 50.00, '2026-02-02 10:00:00', NULL),
    (1004, 'SYN-O1004', 3, 'paid', 0.00, '2026-02-02 11:00:00', '2026-02-02 11:00:00'),
    (1005, 'SYN-O1005', 5, 'pending', 0.00, '2026-02-03 12:00:00', NULL),
    (1006, 'SYN-O1006', 2, 'refunded', 50.00, '2026-02-04 13:00:00', '2026-02-04 13:05:00');

-- Includes multiple lines per order, a repeated product, a line discount, and a free item.
INSERT INTO order_items (
    order_id, line_no, product_sku, quantity, unit_price, discount_amount
) VALUES
    (1001, 1, 'SYN-SKU-A', 2, 40.00, 0.00),
    (1001, 2, 'SYN-SKU-B', 1, 20.00, 0.00),
    (1002, 1, 'SYN-SKU-A', 1, 35.00, 5.00),
    (1003, 1, 'SYN-SKU-C', 1, 50.00, 0.00),
    (1004, 1, 'SYN-SKU-GIFT', 1, 0.00, 0.00),
    (1006, 1, 'SYN-SKU-C', 1, 50.00, 0.00);

COMMIT;
