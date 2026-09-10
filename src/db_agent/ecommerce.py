"""Public deterministic ecommerce data: eight audit orders plus a streamed background.

All amounts are CNY. Order gross equals sum(quantity * unit_price - discount_amount).
Only succeeded payments/refunds count as cash received/returned; order creation,
payment paid_at and refund completed_at are three different calendar definitions.
DATETIME values represent UTC. No database, files, environment or model is accessed.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from random import Random

DATASET_VERSION = "ecommerce-v1"
TABLES = (
    "ec_customers",
    "ec_products",
    "ec_orders",
    "ec_order_items",
    "ec_payments",
    "ec_refunds",
)
TABLE_COLUMNS = {
    "ec_customers": ("id", "customer_code", "display_name", "region", "created_at"),
    "ec_products": ("id", "sku", "name", "category", "unit_price", "created_at", "retired_at"),
    "ec_orders": (
        "id",
        "order_no",
        "customer_id",
        "status",
        "total_amount",
        "created_at",
        "paid_at",
        "cancelled_at",
    ),
    "ec_order_items": (
        "id",
        "order_id",
        "line_no",
        "product_id",
        "quantity",
        "unit_price",
        "discount_amount",
        "line_total",
    ),
    "ec_payments": (
        "id",
        "order_id",
        "status",
        "amount",
        "created_at",
        "paid_at",
        "failure_reason",
    ),
    "ec_refunds": (
        "id",
        "order_id",
        "payment_id",
        "status",
        "amount",
        "created_at",
        "completed_at",
        "reason",
    ),
}

# Each statement is independent DDL; the caller must guard the target and partial creation.
SCHEMA_SQL = (
    """CREATE TABLE ec_customers (
        id BIGINT UNSIGNED NOT NULL,
        customer_code VARCHAR(32) NOT NULL,
        display_name VARCHAR(80) NOT NULL,
        region VARCHAR(16) NULL,
        created_at DATETIME(6) NOT NULL,
        PRIMARY KEY (id),
        UNIQUE KEY uk_ec_customers_code (customer_code),
        KEY idx_ec_customers_region_created (region, created_at)
    ) ENGINE=InnoDB""",
    """CREATE TABLE ec_products (
        id BIGINT UNSIGNED NOT NULL,
        sku VARCHAR(32) NOT NULL,
        name VARCHAR(80) NOT NULL,
        category VARCHAR(24) NOT NULL,
        unit_price DECIMAL(12,2) NOT NULL,
        created_at DATETIME(6) NOT NULL,
        retired_at DATETIME(6) NULL,
        PRIMARY KEY (id),
        UNIQUE KEY uk_ec_products_sku (sku),
        KEY idx_ec_products_category_id (category, id),
        CONSTRAINT chk_ec_products_price CHECK (unit_price >= 0),
        CONSTRAINT chk_ec_products_retired CHECK (retired_at IS NULL OR retired_at >= created_at)
    ) ENGINE=InnoDB""",
    """CREATE TABLE ec_orders (
        id BIGINT UNSIGNED NOT NULL,
        order_no VARCHAR(32) NOT NULL,
        customer_id BIGINT UNSIGNED NOT NULL,
        status VARCHAR(24) NOT NULL,
        total_amount DECIMAL(14,2) NOT NULL,
        created_at DATETIME(6) NOT NULL,
        paid_at DATETIME(6) NULL,
        cancelled_at DATETIME(6) NULL,
        PRIMARY KEY (id),
        UNIQUE KEY uk_ec_orders_number (order_no),
        KEY idx_ec_orders_customer_created (customer_id, created_at),
        KEY idx_ec_orders_status_created (status, created_at),
        KEY idx_ec_orders_paid (paid_at, id),
        CONSTRAINT fk_ec_orders_customer FOREIGN KEY (customer_id) REFERENCES ec_customers(id),
        CONSTRAINT chk_ec_orders_amount CHECK (total_amount >= 0),
        CONSTRAINT chk_ec_orders_status CHECK (
            status IN ('pending', 'cancelled', 'paid', 'refunded', 'partially_refunded')),
        CONSTRAINT chk_ec_orders_paid CHECK (
            (status IN ('pending', 'cancelled') AND paid_at IS NULL) OR
            (status IN ('paid', 'refunded', 'partially_refunded')
                AND paid_at IS NOT NULL AND paid_at >= created_at)),
        CONSTRAINT chk_ec_orders_cancelled CHECK (
            (status = 'cancelled' AND cancelled_at IS NOT NULL AND cancelled_at >= created_at) OR
            (status <> 'cancelled' AND cancelled_at IS NULL))
    ) ENGINE=InnoDB""",
    """CREATE TABLE ec_order_items (
        id BIGINT UNSIGNED NOT NULL,
        order_id BIGINT UNSIGNED NOT NULL,
        line_no SMALLINT UNSIGNED NOT NULL,
        product_id BIGINT UNSIGNED NOT NULL,
        quantity INT UNSIGNED NOT NULL,
        unit_price DECIMAL(12,2) NOT NULL,
        discount_amount DECIMAL(14,2) NOT NULL,
        line_total DECIMAL(14,2) NOT NULL,
        PRIMARY KEY (id),
        UNIQUE KEY uk_ec_items_order_line (order_id, line_no),
        KEY idx_ec_items_product_order (product_id, order_id),
        CONSTRAINT fk_ec_items_order FOREIGN KEY (order_id) REFERENCES ec_orders(id),
        CONSTRAINT fk_ec_items_product FOREIGN KEY (product_id) REFERENCES ec_products(id),
        CONSTRAINT chk_ec_items_quantity CHECK (quantity BETWEEN 1 AND 20),
        CONSTRAINT chk_ec_items_price CHECK (unit_price >= 0),
        CONSTRAINT chk_ec_items_discount CHECK (
            discount_amount >= 0 AND discount_amount <= quantity * unit_price),
        CONSTRAINT chk_ec_items_total CHECK (line_total = quantity * unit_price - discount_amount)
    ) ENGINE=InnoDB""",
    """CREATE TABLE ec_payments (
        id BIGINT UNSIGNED NOT NULL,
        order_id BIGINT UNSIGNED NOT NULL,
        status VARCHAR(16) NOT NULL,
        amount DECIMAL(14,2) NOT NULL,
        created_at DATETIME(6) NOT NULL,
        paid_at DATETIME(6) NULL,
        failure_reason VARCHAR(64) NULL,
        PRIMARY KEY (id),
        UNIQUE KEY uk_ec_payments_order_id (order_id, id),
        KEY idx_ec_payments_order_status (order_id, status, paid_at),
        KEY idx_ec_payments_status_paid (status, paid_at, order_id),
        CONSTRAINT fk_ec_payments_order FOREIGN KEY (order_id) REFERENCES ec_orders(id),
        CONSTRAINT chk_ec_payments_amount CHECK (amount >= 0),
        CONSTRAINT chk_ec_payments_status CHECK (status IN ('pending', 'failed', 'succeeded')),
        CONSTRAINT chk_ec_payments_paid CHECK (
            (status = 'succeeded' AND paid_at IS NOT NULL AND paid_at >= created_at) OR
            (status <> 'succeeded' AND paid_at IS NULL))
    ) ENGINE=InnoDB""",
    """CREATE TABLE ec_refunds (
        id BIGINT UNSIGNED NOT NULL,
        order_id BIGINT UNSIGNED NOT NULL,
        payment_id BIGINT UNSIGNED NOT NULL,
        status VARCHAR(16) NOT NULL,
        amount DECIMAL(14,2) NOT NULL,
        created_at DATETIME(6) NOT NULL,
        completed_at DATETIME(6) NULL,
        reason VARCHAR(64) NULL,
        PRIMARY KEY (id),
        KEY idx_ec_refunds_order_payment (order_id, payment_id),
        KEY idx_ec_refunds_order_status (order_id, status, completed_at),
        KEY idx_ec_refunds_status_completed (status, completed_at, order_id),
        CONSTRAINT fk_ec_refunds_order FOREIGN KEY (order_id) REFERENCES ec_orders(id),
        CONSTRAINT fk_ec_refunds_payment FOREIGN KEY (order_id, payment_id)
            REFERENCES ec_payments(order_id, id),
        CONSTRAINT chk_ec_refunds_amount CHECK (amount > 0),
        CONSTRAINT chk_ec_refunds_status CHECK (status IN ('pending', 'failed', 'succeeded')),
        CONSTRAINT chk_ec_refunds_completed CHECK (
            (status = 'succeeded' AND completed_at IS NOT NULL AND completed_at >= created_at) OR
            (status <> 'succeeded' AND completed_at IS NULL))
    ) ENGINE=InnoDB""",
)

_START = datetime(2026, 1, 1)
_PROMOTION = datetime(2026, 2, 15)
_FIXED_PRICES = {1: 4000, 2: 2000, 3: 5000, 4: 0, 5: 1500, 6: 2500}
_CATEGORIES = ("electronics", "home", "books", "gift", "sports")
_REGIONS = ("east", "west", "north", "south")


@dataclass(frozen=True)
class EcommerceScale:
    orders: int = 1_000_000
    customers: int = 100_000
    products: int = 10_000
    seed: int = 20260910

    def __post_init__(self):
        for name, low, high in (
            ("orders", 8, 5_000_000),
            ("customers", 10, 1_000_000),
            ("products", 8, 100_000),
            ("seed", 0, 2**32 - 1),
        ):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"invalid ecommerce scale field: {name}")


@dataclass(frozen=True)
class OrderBundle:
    order: tuple
    items: list[tuple]
    payments: list[tuple]
    refunds: list[tuple]


def _money(cents: int) -> Decimal:
    return Decimal(cents).scaleb(-2)


def _product_cents(product_id: int, seed: int) -> int:
    if product_id in _FIXED_PRICES:
        return _FIXED_PRICES[product_id]
    if product_id == 1000:
        return 2500
    return 500 + ((product_id - 1000) * 137 + seed % 997) % 99500


def iter_customers(scale: EcommerceScale):
    """Yield exactly scale.customers; customers 4/8 and a background tail have no orders."""
    fixed_regions = ("east", "west", None, "east", "north", "east", "south", None)
    for customer_id, region in enumerate(fixed_regions, 1):
        yield (
            customer_id,
            f"EC-C{customer_id:08d}",
            f"Audit Customer {customer_id}",
            region,
            _START,
        )
    for i in range(scale.customers - 8):
        customer_id = 1000 + i
        region = None if i % 11 == 0 else _REGIONS[i % len(_REGIONS)]
        yield (customer_id, f"EC-C{customer_id:08d}", f"Background Customer {i}", region, _START)


def iter_products(scale: EcommerceScale):
    for product_id in range(1, 7):
        yield (
            product_id,
            f"EC-SKU-{product_id:08d}",
            f"Audit Product {product_id}",
            _CATEGORIES[(product_id - 1) % len(_CATEGORIES)],
            _money(_product_cents(product_id, scale.seed)),
            _START,
            None,
        )
    for i in range(scale.products - 6):
        product_id = 1000 + i
        yield (
            product_id,
            f"EC-SKU-{product_id:08d}",
            f"Background Product {i}",
            _CATEGORIES[i % len(_CATEGORIES)],
            _money(_product_cents(product_id, scale.seed)),
            _START,
            datetime(2026, 3, 31) if i % 97 == 0 else None,
        )


def _items(order_id: int, specifications: list[tuple[int, int, int, int]]) -> tuple[list, int]:
    rows, total = [], 0
    for line, (product_id, quantity, price, discount) in enumerate(specifications, 1):
        amount = quantity * price - discount
        rows.append(
            (
                order_id * 10 + line,
                order_id,
                line,
                product_id,
                quantity,
                _money(price),
                _money(discount),
                _money(amount),
            )
        )
        total += amount
    return rows, total


def _payment(order_id, sequence, status, cents, created, paid=None):
    return (
        order_id * 10 + sequence,
        order_id,
        status,
        _money(cents),
        created,
        paid,
        "synthetic_decline" if status == "failed" else None,
    )


def _refund(order_id, sequence, payment_id, status, cents, requested):
    return (
        order_id * 10 + sequence,
        order_id,
        payment_id,
        status,
        _money(cents),
        requested,
        requested + timedelta(minutes=5) if status == "succeeded" else None,
        "synthetic_return" if status != "pending" else None,
    )


def _order(order_id, customer_id, status, total, created, paid):
    return (
        order_id,
        f"EC-O{order_id:010d}",
        customer_id,
        status,
        _money(total),
        created,
        paid,
        created + timedelta(minutes=5) if status == "cancelled" else None,
    )


def _fixed_bundles():
    # These literal scenarios are an auditable specification, isolated from all background IDs.
    scenarios = (
        (
            1,
            1,
            "paid",
            "2026-01-31T23:59:00",
            "2026-02-01T00:02:00",
            [(1, 2, 4000, 0), (2, 1, 2000, 0)],
            [("failed", 10000), ("succeeded", 10000)],
        ),
        (
            2,
            1,
            "partially_refunded",
            "2026-02-01T09:00:00",
            "2026-02-01T09:03:00",
            [(1, 2, 4000, 1000), (2, 1, 2000, 0)],
            [("failed", 9000), ("succeeded", 4000), ("succeeded", 5000)],
        ),
        (3, 2, "cancelled", "2026-02-02T10:00:00", None, [(3, 1, 5000, 0)], [("failed", 5000)]),
        (
            4,
            3,
            "paid",
            "2026-02-10T11:00:00",
            "2026-02-10T11:01:00",
            [(4, 1, 0, 0)],
            [("succeeded", 0)],
        ),
        (5, 5, "pending", "2026-02-20T12:00:00", None, [(6, 1, 2500, 0)], [("pending", 2500)]),
        (
            6,
            2,
            "refunded",
            "2026-02-28T23:59:00",
            "2026-03-01T00:01:00",
            [(3, 1, 5000, 0)],
            [("succeeded", 5000)],
        ),
        (
            7,
            6,
            "paid",
            "2026-01-15T10:00:00",
            "2026-01-15T10:02:00",
            [(5, 4, 1500, 0)],
            [("succeeded", 6000)],
        ),
        (
            8,
            7,
            "partially_refunded",
            "2026-03-15T12:00:00",
            "2026-03-15T12:05:00",
            [(1, 2, 4000, 0)],
            [("succeeded", 4000), ("succeeded", 4000)],
        ),
    )
    refunds = {
        2: [
            (23, "succeeded", 1000, "2026-02-03T12:00:00"),
            (23, "succeeded", 2000, "2026-02-04T12:00:00"),
        ],
        6: [
            (61, "succeeded", 2500, "2026-03-03T12:00:00"),
            (61, "succeeded", 2500, "2026-03-05T12:00:00"),
        ],
        7: [(71, "pending", 1000, "2026-02-06T12:00:00")],
        8: [
            (81, "succeeded", 2000, "2026-03-20T12:00:00"),
            (82, "succeeded", 2000, "2026-03-20T12:00:00"),
            (82, "failed", 500, "2026-03-17T12:00:00"),
        ],
    }
    for order_id, customer, status, created_text, paid_text, item_specs, payment_specs in scenarios:
        created = datetime.fromisoformat(created_text)
        paid = datetime.fromisoformat(paid_text) if paid_text else None
        items, total = _items(order_id, item_specs)
        payments = []
        for sequence, (payment_status, cents) in enumerate(payment_specs, 1):
            settled = (
                paid - timedelta(minutes=len(payment_specs) - sequence)
                if (payment_status == "succeeded")
                else None
            )
            payments.append(
                _payment(
                    order_id,
                    sequence,
                    payment_status,
                    cents,
                    created + timedelta(seconds=sequence * 10),
                    settled,
                )
            )
        refund_rows = [
            _refund(order_id, i, payment_id, state, cents, datetime.fromisoformat(requested))
            for i, (payment_id, state, cents, requested) in enumerate(refunds.get(order_id, ()), 1)
        ]
        yield OrderBundle(
            _order(order_id, customer, status, total, created, paid),
            items,
            payments,
            refund_rows,
        )


def _background_bundle(scale, rng, index):
    order_id = 1000 + index
    if index % 1000 == 0:
        # Independent scale oracle: ceil((orders-8)/1000) orders, each exactly CNY 100.
        created = _PROMOTION + timedelta(hours=12, seconds=index // 1000)
        paid = created + timedelta(minutes=1)
        items, total = _items(order_id, [(1000, 4, 2500, 0)])
        return OrderBundle(
            _order(order_id, 1000, "paid", total, created, paid),
            items,
            [_payment(order_id, 1, "succeeded", total, created, paid)],
            [],
        )
    # Reserve the last 10% of background customers, except at the smallest scale.
    active_customers = max(2, (scale.customers - 8) * 9 // 10) - 1
    customer_span = min(100, active_customers) if rng.randrange(100) < 65 else active_customers
    customer_id = 1001 + rng.randrange(customer_span)
    if index % 5 < 2:
        created = _PROMOTION + timedelta(seconds=rng.randrange(3 * 86400))
    else:
        # Leave six days after March 25 for settlement and successful refund completion.
        created = _START + timedelta(seconds=rng.randrange(84 * 86400))
    bucket = index % 100
    status = (
        "pending"
        if bucket < 10
        else "cancelled"
        if bucket < 20
        else "refunded"
        if bucket < 30
        else "partially_refunded"
        if bucket < 45
        else "paid"
    )
    item_specs = []
    available_products = scale.products - 7  # Product 1000 is the isolated cohort item.
    for _ in range(2 + rng.randrange(3)):
        product_span = (
            min(100, available_products) if rng.randrange(100) < 70 else available_products
        )
        product_id = 1001 + rng.randrange(product_span)
        quantity = 1 + rng.randrange(4)
        price = _product_cents(product_id, scale.seed)
        discount = quantity * price // 10 if index % 5 < 2 else 0
        item_specs.append((product_id, quantity, price, discount))
    items, total = _items(order_id, item_specs)
    paid = (
        None
        if status in {"pending", "cancelled"}
        else (created + timedelta(seconds=60 + rng.randrange(36 * 3600)))
    )
    payments, captures = [], []
    if paid is None:
        payments.append(
            _payment(
                order_id,
                1,
                "pending" if status == "pending" else "failed",
                total,
                created,
            )
        )
    else:
        if index % 3 == 0:
            payments.append(_payment(order_id, 1, "failed", total, created))
        amounts = [total // 2, total - total // 2] if index % 5 == 0 else [total]
        for position, amount in enumerate(amounts):
            sequence = len(payments) + 1
            payments.append(
                _payment(
                    order_id,
                    sequence,
                    "succeeded",
                    amount,
                    created + timedelta(seconds=10),
                    paid - timedelta(seconds=30 * (len(amounts) - position - 1)),
                )
            )
            captures.append((order_id * 10 + sequence, amount))
    refunds = []
    remaining = (
        total if status == "refunded" else total // 3 if status == "partially_refunded" else 0
    )
    for payment_id, captured in captures:
        share = min(remaining, captured)
        if share:
            for amount in (share // 2, share - share // 2):
                if amount:
                    sequence = len(refunds) + 1
                    refunds.append(
                        _refund(
                            order_id,
                            sequence,
                            payment_id,
                            "succeeded",
                            amount,
                            paid + timedelta(days=sequence),
                        )
                    )
            remaining -= share
    if status == "paid" and index % 17 == 0:
        refunds.append(_refund(order_id, 1, captures[0][0], "pending", total // 10, paid))
    return OrderBundle(
        _order(order_id, customer_id, status, total, created, paid),
        items,
        payments,
        refunds,
    )


def iter_order_bundles(scale: EcommerceScale):
    """Yield exactly scale.orders bundles; memory holds only one order and its child rows."""
    yield from _fixed_bundles()
    rng = Random(scale.seed)
    for index in range(scale.orders - 8):
        yield _background_bundle(scale, rng, index)
