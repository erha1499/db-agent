"""Offline synthetic dataset checks; fixed monetary expectations are hand-calculated."""

import random
import tracemalloc
from collections import Counter, defaultdict
from dataclasses import FrozenInstanceError
from datetime import datetime
from decimal import Decimal
from itertools import islice

import pytest
import sqlglot

from db_agent.ecommerce import (
    DATASET_VERSION,
    SCHEMA_SQL,
    TABLE_COLUMNS,
    TABLES,
    EcommerceScale,
    iter_customers,
    iter_order_bundles,
    iter_products,
)


def records(table, rows):
    return [dict(zip(TABLE_COLUMNS[table], row, strict=True)) for row in rows]


def unpack(bundle):
    return (
        records("ec_orders", [bundle.order])[0],
        records("ec_order_items", bundle.items),
        records("ec_payments", bundle.payments),
        records("ec_refunds", bundle.refunds),
    )


def test_defaults_and_frozen_scale():
    scale = EcommerceScale()
    assert (scale.orders, scale.customers, scale.products, scale.seed) == (
        1_000_000,
        100_000,
        10_000,
        20260910,
    )
    with pytest.raises(FrozenInstanceError):
        scale.orders = 10
    assert DATASET_VERSION == "ecommerce-v1"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("orders", 7),
        ("orders", 5_000_001),
        ("orders", True),
        ("orders", 8.0),
        ("customers", 9),
        ("customers", 1_000_001),
        ("products", 7),
        ("products", 100_001),
        ("seed", -1),
        ("seed", 2**32),
        ("seed", False),
        ("seed", "private-invalid-seed"),
    ],
)
def test_invalid_scale_fields_are_rejected_without_echoing_values(field, value):
    with pytest.raises(ValueError) as caught:
        EcommerceScale(**{field: value})
    assert str(caught.value) == f"invalid ecommerce scale field: {field}"


def test_schema_has_six_create_only_innodb_tables_in_insert_order():
    assert TABLES == (
        "ec_customers",
        "ec_products",
        "ec_orders",
        "ec_order_items",
        "ec_payments",
        "ec_refunds",
    )
    assert tuple(TABLE_COLUMNS) == TABLES
    assert len(SCHEMA_SQL) == 6
    for table, statement in zip(TABLES, SCHEMA_SQL, strict=True):
        parsed = sqlglot.parse_one(statement, read="mysql")
        assert isinstance(parsed, sqlglot.exp.Create)
        assert parsed.this.this.name == table
        assert "ENGINE=InnoDB" in statement
        assert "PRIMARY KEY (id)" in statement
        assert "DROP" not in statement and "INSERT" not in statement
    assert "(customer_id, created_at)" in SCHEMA_SQL[2]
    assert "FOREIGN KEY (order_id, payment_id)" in SCHEMA_SQL[5]
    assert "REFERENCES ec_payments(order_id, id)" in SCHEMA_SQL[5]


def test_minimum_scale_keeps_complete_audit_cases_and_exact_entity_counts():
    scale = EcommerceScale(orders=8, customers=10, products=8)
    customers = list(iter_customers(scale))
    products = list(iter_products(scale))
    bundles = list(iter_order_bundles(scale))
    assert len(customers) == 10 and len(products) == 8 and len(bundles) == 8
    assert {row[0] for row in customers} == {*range(1, 9), 1000, 1001}
    assert {row[0] for row in products} == {*range(1, 7), 1000, 1001}
    assert [bundle.order[0] for bundle in bundles] == list(range(1, 9))


def test_seed_is_reproducible_and_does_not_change_audit_rows_or_global_random():
    scale = EcommerceScale(orders=40, customers=50, products=30, seed=17)
    before = random.getstate()
    first = list(iter_order_bundles(scale))
    assert first == list(iter_order_bundles(scale))
    assert before == random.getstate()
    changed = list(
        iter_order_bundles(EcommerceScale(orders=40, customers=50, products=30, seed=18))
    )
    assert first[:8] == changed[:8]
    assert first[9:] != changed[9:]
    assert list(iter_customers(scale)) == list(iter_customers(scale))
    assert list(iter_products(scale)) == list(iter_products(scale))


def test_fixed_money_and_calendar_truth_is_independent_of_random_background():
    bundles = list(islice(iter_order_bundles(EcommerceScale()), 8))
    assert [bundle.order[4] for bundle in bundles] == [
        Decimal("100.00"),
        Decimal("90.00"),
        Decimal("50.00"),
        Decimal("0.00"),
        Decimal("25.00"),
        Decimal("50.00"),
        Decimal("60.00"),
        Decimal("80.00"),
    ]
    created_gross, received, returned = (
        defaultdict(Decimal),
        defaultdict(Decimal),
        defaultdict(Decimal),
    )
    for bundle in bundles:
        order, _, payments, refunds = unpack(bundle)
        if order["paid_at"] is not None:
            created_gross[order["created_at"].month] += order["total_amount"]
        for payment in payments:
            if payment["status"] == "succeeded":
                received[payment["paid_at"].month] += payment["amount"]
        for refund in refunds:
            if refund["status"] == "succeeded":
                returned[refund["completed_at"].month] += refund["amount"]
    # Literal totals come from the eight published scenarios, not a generator summary API.
    assert dict(created_gross) == {1: Decimal("160.00"), 2: Decimal("140.00"), 3: Decimal("80.00")}
    assert dict(received) == {1: Decimal("60.00"), 2: Decimal("190.00"), 3: Decimal("130.00")}
    assert dict(returned) == {2: Decimal("30.00"), 3: Decimal("90.00")}
    assert sum(received.values()) - sum(returned.values()) == Decimal("260.00")
    assert bundles[0].order[5].month == 1 and bundles[0].order[6].month == 2
    assert bundles[5].order[5].month == 2 and bundles[5].order[6].month == 3


def test_audit_case_exposes_many_to_many_join_inflation():
    bundle = list(islice(iter_order_bundles(EcommerceScale()), 2))[1]
    _, items, payments, refunds = unpack(bundle)
    assert (len(items), len(payments), len(refunds)) == (2, 3, 2)
    succeeded = [p for p in payments if p["status"] == "succeeded"]
    correct_payment = sum(p["amount"] for p in succeeded)
    naive_join_payment = sum(p["amount"] for _ in items for p in succeeded for _ in refunds)
    assert correct_payment == Decimal("90.00")
    assert naive_join_payment == Decimal("360.00")
    assert sum(r["amount"] for r in refunds) == Decimal("30.00")


def test_background_foreign_keys_money_and_statuses_remain_consistent():
    scale = EcommerceScale(orders=2008, customers=250, products=150)
    customers = {row[0] for row in iter_customers(scale)}
    products = {row["id"]: row for row in records("ec_products", iter_products(scale))}
    seen = {table: set() for table in TABLES[2:]}
    ordering = []
    for bundle in iter_order_bundles(scale):
        order, items, payments, refunds = unpack(bundle)
        ordering.append(order["id"])
        assert order["customer_id"] in customers
        assert sum(item["line_total"] for item in items) == order["total_amount"]
        assert len(items) >= 1
        for item in items:
            assert item["product_id"] in products
            assert item["unit_price"] == products[item["product_id"]]["unit_price"]
            assert item["line_total"] == (
                item["quantity"] * item["unit_price"] - item["discount_amount"]
            )
            assert item["quantity"] > 0 and item["discount_amount"] >= 0
        captured = {p["id"]: p for p in payments if p["status"] == "succeeded"}
        payment_total = sum(p["amount"] for p in captured.values())
        refund_total = sum(r["amount"] for r in refunds if r["status"] == "succeeded")
        assert payment_total == (order["total_amount"] if order["paid_at"] else 0)
        assert 0 <= refund_total <= payment_total
        if order["status"] == "refunded":
            assert refund_total == payment_total
        elif order["status"] == "partially_refunded":
            assert 0 < refund_total < payment_total
        else:
            assert refund_total == 0
        refunds_by_payment = defaultdict(Decimal)
        for refund in refunds:
            assert refund["payment_id"] in captured
            if refund["status"] == "succeeded":
                refunds_by_payment[refund["payment_id"]] += refund["amount"]
                assert refund["completed_at"] >= refund["created_at"] >= order["paid_at"]
            else:
                assert refund["completed_at"] is None
        assert all(amount <= captured[key]["amount"] for key, amount in refunds_by_payment.items())
        if order["paid_at"]:
            assert order["paid_at"] > order["created_at"]
            assert max(p["paid_at"] for p in captured.values()) == order["paid_at"]
        for table, rows in zip(TABLES[2:], [[order], items, payments, refunds], strict=True):
            for row in rows:
                assert row["id"] not in seen[table]
                seen[table].add(row["id"])
                if table != "ec_orders":
                    assert row["order_id"] == order["id"]
                for value in row.values():
                    assert type(value) in {int, str, Decimal, datetime, type(None)}
                    if isinstance(value, Decimal):
                        assert value.is_finite() and value.as_tuple().exponent == -2
                    if isinstance(value, datetime):
                        assert datetime(2026, 1, 1) <= value < datetime(2026, 4, 1)
    assert len(ordering) == scale.orders and ordering == sorted(ordering)


def test_background_contains_skew_promotion_no_order_customers_and_large_row_counts():
    scale = EcommerceScale(orders=20008, customers=2500, products=1000)
    orders, items, payments = 0, 0, 0
    months, statuses, customers = Counter(), Counter(), set()
    promotion_count = hot_customers = hot_products = 0
    for bundle in islice(iter_order_bundles(scale), 8, None):
        order, lines, attempts, _ = unpack(bundle)
        orders += 1
        items += len(lines)
        payments += len(attempts)
        customers.add(order["customer_id"])
        assert order["customer_id"] >= 1000
        assert all(line["product_id"] >= 1000 for line in lines)
        statuses[order["status"]] += 1
        months[order["created_at"].month] += 1
        promotion_count += datetime(2026, 2, 15) <= order["created_at"] < datetime(2026, 2, 18)
        hot_customers += 1001 <= order["customer_id"] <= 1100
        hot_products += sum(1001 <= line["product_id"] <= 1100 for line in lines)
    assert orders == 20000 and items > orders * 2 and payments > orders
    assert set(months) == {1, 2, 3}
    assert set(statuses) == {"pending", "cancelled", "paid", "refunded", "partially_refunded"}
    assert promotion_count > orders * 0.38
    assert hot_customers > orders * 0.60
    assert hot_products > items * 0.65
    all_customers = {row[0] for row in iter_customers(scale)}
    assert len(all_customers - customers) > 240
    assert {4, 8}.issubset(all_customers - customers)
    assert any(row[3] is None for row in iter_customers(scale))


@pytest.mark.parametrize("background_count", [0, 1, 999, 1000, 1001, 3000])
def test_indexed_background_cohort_has_an_independent_scale_formula(background_count):
    scale = EcommerceScale(orders=background_count + 8, customers=50, products=30)
    matches = []
    for bundle in iter_order_bundles(scale):
        order = bundle.order
        if order[2] == 1000:
            matches.append(order)
            assert order[3] == "paid"
            assert order[4] == Decimal("100.00")
            assert datetime(2026, 2, 15) <= order[5] < datetime(2026, 2, 16)
            assert len(bundle.items) == len(bundle.payments) == 1 and not bundle.refunds
    assert len(matches) == (background_count + 999) // 1000
    assert sum(row[4] for row in matches) == Decimal(100) * ((background_count + 999) // 1000)


def test_default_million_scale_yields_without_materializing_the_dataset():
    tracemalloc.start()
    try:
        stream = iter_order_bundles(EcommerceScale())
        assert iter(stream) is stream
        for bundle in islice(stream, 1008):
            assert len(bundle.items) <= 4
            assert len(bundle.payments) <= 3
            assert len(bundle.refunds) <= 4
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2 * 1024 * 1024
