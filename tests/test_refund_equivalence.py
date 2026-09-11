"""Finite, independent relation examples for the refund JOIN refinement.

SQLite and hand-written integer-cent rows check JOIN multiplicities, NULL and
aggregation only. They do not prove general SQL equivalence, MySQL plans,
DECIMAL serialization, or integrity of the actual MySQL dataset. No product
compiler, data generator, model, or external database is used here.

Sufficient premises: payment id is individually unique, both refund reference
columns are non-NULL, and every (order_id, payment_id) references the complete
payment (order_id, id) key. Then the extra order equality preserves the JOIN
multiset. The counterexamples deliberately remove one premise in memory only.
"""

import sqlite3
from collections import Counter
from contextlib import closing

import pytest

ORIGINAL_JOIN = "r.payment_id = p.id"
COMPLETE_JOIN = "r.payment_id = p.id AND r.order_id = p.order_id"
JOINS = (ORIGINAL_JOIN, COMPLETE_JOIN)


def database(*, nullable_refund_keys=False, unique_payment_id=True, refund_fk=True):
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    primary_key = "id" if unique_payment_id else "order_id, id"
    required = "" if nullable_refund_keys else "NOT NULL"
    foreign_key = (
        ", FOREIGN KEY (order_id, payment_id) REFERENCES payments(order_id, id)"
        if refund_fk else ""
    )
    connection.executescript(f"""
        CREATE TABLE payments (
            id INTEGER NOT NULL,
            order_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL,
            PRIMARY KEY ({primary_key}),
            UNIQUE (order_id, id)
        );
        CREATE TABLE refunds (
            id INTEGER PRIMARY KEY,
            order_id INTEGER {required},
            payment_id INTEGER {required},
            amount INTEGER NOT NULL,
            status TEXT NOT NULL
            {foreign_key}
        );
    """)
    return connection


def insert_rows(connection, payments=(), refunds=()):
    connection.executemany("INSERT INTO payments VALUES (?, ?, ?, ?)", payments)
    connection.executemany("INSERT INTO refunds VALUES (?, ?, ?, ?, ?)", refunds)


def joined_rows(connection, join):
    # Deliberately omit refund id: equal refund values must remain duplicates.
    return Counter(connection.execute(f"""
        SELECT p.id, p.order_id, p.amount, r.amount
        FROM payments AS p INNER JOIN refunds AS r ON {join}
        WHERE p.order_id < 1000
          AND p.status = 'succeeded' AND r.status = 'succeeded'
    """).fetchall())


def balances(connection, join):
    # The complete original business query, with only the ON clause varying.
    return connection.execute(f"""
        SELECT p.id, p.amount, SUM(r.amount) AS refunded,
               p.amount - SUM(r.amount) AS balance
        FROM payments AS p INNER JOIN refunds AS r ON {join}
        WHERE p.order_id < 1000
          AND p.status = 'succeeded' AND r.status = 'succeeded'
        GROUP BY p.id, p.amount
        HAVING SUM(r.amount) > 0 AND SUM(r.amount) < p.amount
        ORDER BY p.amount - SUM(r.amount) DESC, p.id ASC
    """).fetchall()


@pytest.mark.parametrize("join", JOINS, ids=("original", "complete"))
def test_valid_relationship_preserves_duplicates_split_payments_and_aggregation(join):
    with closing(database()) as connection:
        insert_rows(connection, payments=[
            (11, 7, 5000, "succeeded"),
            (12, 7, 4000, "succeeded"),  # Same order, a separate payment.
            (13, 8, 5000, "succeeded"),  # Fully refunded: excluded by HAVING.
            (14, 9, 2000, "failed"),
            (15, 9, 2000, "pending"),
            (16, 10, 1000, "succeeded"),  # No refunds: INNER JOIN excludes it.
            (17, 1000, 10000, "succeeded"),  # Excluded by the order range.
        ], refunds=[
            (1, 7, 11, 1000, "succeeded"),
            (2, 7, 11, 1000, "succeeded"),
            (3, 7, 11, 1000, "succeeded"),
            (4, 7, 12, 500, "succeeded"),
            (5, 7, 12, 1500, "succeeded"),
            (6, 8, 13, 2500, "succeeded"),
            (7, 8, 13, 2500, "succeeded"),
            (8, 7, 12, 250, "failed"),
            (9, 7, 12, 250, "pending"),
            (10, 9, 14, 1000, "succeeded"),
            (11, 9, 15, 1000, "succeeded"),
            (12, 1000, 17, 5000, "succeeded"),
        ])
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        # Independent literal expectations, not one query used as the oracle.
        assert joined_rows(connection, join) == Counter({
            (11, 7, 5000, 1000): 3,
            (12, 7, 4000, 500): 1,
            (12, 7, 4000, 1500): 1,
            (13, 8, 5000, 2500): 2,
        })
        assert balances(connection, join) == [
            (11, 5000, 3000, 2000),
            (12, 4000, 2000, 2000),
        ]


@pytest.mark.parametrize("join", JOINS, ids=("original", "complete"))
@pytest.mark.parametrize("payment_exists", [False, True], ids=("empty", "no-refund"))
def test_empty_input_or_unmatched_payment_has_no_aggregate_row(join, payment_exists):
    with closing(database()) as connection:
        if payment_exists:
            insert_rows(connection, payments=[(11, 7, 5000, "succeeded")])
        assert joined_rows(connection, join) == Counter()
        assert balances(connection, join) == []


@pytest.mark.parametrize("order_id,payment_id", [(None, 11), (7, None), (8, 11)])
def test_strict_reference_rejects_null_or_mismatched_order(order_id, payment_id):
    with closing(database()) as connection:
        insert_rows(connection, payments=[
            (11, 7, 5000, "succeeded"), (12, 8, 5000, "succeeded"),
        ])
        with pytest.raises(sqlite3.IntegrityError):
            insert_rows(connection, refunds=[
                (1, order_id, payment_id, 1000, "succeeded"),
            ])


def test_nullable_order_reference_is_not_equivalent_even_with_composite_fk():
    # A composite FK with a NULL component does not establish order equality.
    with closing(database(nullable_refund_keys=True)) as connection:
        insert_rows(connection, payments=[(11, 7, 5000, "succeeded")], refunds=[
            (1, None, 11, 1000, "succeeded"),
        ])
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert joined_rows(connection, ORIGINAL_JOIN) == Counter({(11, 7, 5000, 1000): 1})
        assert joined_rows(connection, COMPLETE_JOIN) == Counter()
        assert balances(connection, ORIGINAL_JOIN) == [(11, 5000, 1000, 4000)]
        assert balances(connection, COMPLETE_JOIN) == []


def test_null_payment_reference_matches_neither_join():
    # NULL payment id alone is not a counterexample: the original equality fails too.
    with closing(database(nullable_refund_keys=True)) as connection:
        insert_rows(connection, payments=[(11, 7, 5000, "succeeded")], refunds=[
            (1, 7, None, 1000, "succeeded"),
        ])
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        for join in JOINS:
            assert joined_rows(connection, join) == Counter()
            assert balances(connection, join) == []


def test_composite_fk_without_individual_payment_id_uniqueness_is_not_equivalent():
    # Both parent rows and the FK are valid; payment id alone is ambiguous.
    with closing(database(unique_payment_id=False)) as connection:
        insert_rows(connection, payments=[
            (11, 7, 5000, "succeeded"), (11, 8, 5000, "succeeded"),
        ], refunds=[(1, 7, 11, 1000, "succeeded")])
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert joined_rows(connection, ORIGINAL_JOIN) == Counter({
            (11, 7, 5000, 1000): 1, (11, 8, 5000, 1000): 1,
        })
        assert joined_rows(connection, COMPLETE_JOIN) == Counter({(11, 7, 5000, 1000): 1})
        assert balances(connection, ORIGINAL_JOIN) == [(11, 5000, 2000, 3000)]
        assert balances(connection, COMPLETE_JOIN) == [(11, 5000, 1000, 4000)]


def test_dirty_cross_order_reference_without_composite_fk_is_not_equivalent():
    # Deliberately invalid business relationship in this in-memory fixture only.
    # Order 8 exists, so existence of each order/payment separately is insufficient.
    with closing(database(refund_fk=False)) as connection:
        insert_rows(connection, payments=[
            (11, 7, 5000, "succeeded"), (12, 8, 5000, "succeeded"),
        ], refunds=[
            (1, 7, 11, 1000, "succeeded"),
            (2, 8, 11, 2000, "succeeded"),
        ])
        assert joined_rows(connection, ORIGINAL_JOIN) == Counter({
            (11, 7, 5000, 1000): 1, (11, 7, 5000, 2000): 1,
        })
        assert joined_rows(connection, COMPLETE_JOIN) == Counter({(11, 7, 5000, 1000): 1})
        assert balances(connection, ORIGINAL_JOIN) == [(11, 5000, 3000, 2000)]
        assert balances(connection, COMPLETE_JOIN) == [(11, 5000, 1000, 4000)]
