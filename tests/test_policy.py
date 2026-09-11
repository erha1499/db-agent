"""Offline boundary tests: no model, network, real database or environment secrets."""

from dataclasses import FrozenInstanceError, asdict

import pytest

from db_agent.config import AnalysisSettings
from db_agent.policy import check_sql


@pytest.fixture
def limits(monkeypatch):
    # Explicitly isolate both settings sources; never load a developer's .env.
    for name in AnalysisSettings.model_fields:
        monkeypatch.delenv(f"DB_AGENT_ANALYSIS_{name.upper()}", raising=False)
    return AnalysisSettings(_env_file=None)


def check(sql, limits, *, database="db_agent", tables=("orders", "customers")):
    return check_sql(sql, database, tables, limits)


def test_join_aggregate_report_has_only_resolved_tables_and_structural_fingerprint(limits):
    result = check(
        "SELECT o.customer_id AS customer, SUM(o.total_amount) AS total "
        "FROM db_agent.orders AS o LEFT JOIN customers c ON o.customer_id = c.id "
        "WHERE o.total_amount >= 10 AND c.region IN ('east', 'west') "
        "GROUP BY o.customer_id HAVING SUM(o.total_amount) > 20 "
        "ORDER BY total DESC LIMIT 10 OFFSET 2;", limits,
    )
    assert result.decision == "ALLOW"
    assert result.tables == ("orders", "customers")
    assert result.aliases == {"o": "orders", "c": "customers"}
    assert len(result.sql_fingerprint) == 64
    assert "east" not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.decision = "BLOCK"


@pytest.mark.parametrize("sql", [
    "SELECT * FROM orders",
    "SELECT sql_id FROM orders",
    "SELECT `orders`.`id`, 'constant', TRUE, NULL FROM `orders`",
    "SELECT COUNT(*), SUM(total_amount), AVG(total_amount), MIN(id), MAX(id) FROM orders",
    "SELECT -(id + 1) * 2 / 3 % 2 AS calculated FROM orders WHERE NOT (id < 1 OR id >= 8)",
    "SELECT id FROM orders WHERE id BETWEEN 1 AND 9 AND id != 4 AND id IS NOT NULL",
    "SELECT id FROM orders WHERE status LIKE 'open%' AND id NOT IN (1, 2)",
    "SELECT o.id FROM orders o INNER JOIN orders p ON o.id = p.id",
    "SELECT orders.id FROM orders JOIN customers ON orders.customer_id = customers.id",
    "SELECT db_agent.orders.id FROM db_agent.orders LIMIT 2, 5",
    "SELECT 'a;--#/* text */', 'it''s valid', 'quoted\"text' FROM orders;  \n",
])
def test_supported_query_subset(sql, limits):
    assert check(sql, limits).decision == "ALLOW"


@pytest.mark.parametrize("sql,rule", [
    ("SELECT id FROM orders; DELETE FROM orders", "MULTIPLE_STATEMENTS"),
    ("SELECT id FROM orders;;", "MULTIPLE_STATEMENTS"),
    ("SELECT id FROM orders /*!50000 INTO OUTFILE 'file' */", "SQL_COMMENT"),
    ("SELECT /*+ MAX_EXECUTION_TIME(0) */ id FROM orders", "SQL_COMMENT"),
    ("SELECT id FROM orders # comment", "SQL_COMMENT"),
    ("SELECT id FROM orders--comment", "SQL_COMMENT"),
    ("SELECT id INTO OUTFILE 'file' FROM orders", "SELECT_SIDE_EFFECT"),
    ("SELECT id INTO DUMPFILE 'file' FROM orders", "SELECT_SIDE_EFFECT"),
    ("SELECT id FROM orders FOR UPDATE", "SELECT_MODIFIER"),
    ("SELECT id FROM orders LOCK IN SHARE MODE", "SELECT_SIDE_EFFECT"),
    ("SELECT SQL_CALC_FOUND_ROWS id FROM orders", "SELECT_MODIFIER"),
    ("SELECT HIGH_PRIORITY id FROM orders", "SELECT_MODIFIER"),
    ("SELECT @secret := id FROM orders", "SQL_VARIABLE"),
    ("SELECT @@version FROM orders", "SQL_VARIABLE"),
    ("UPDATE orders SET id=1", "STATEMENT_TYPE"),
    ("EXPLAIN SELECT id FROM orders", "STATEMENT_TYPE"),
    ("SELECT id FROM orders USE INDEX (PRIMARY)", "SQL_HINT"),
])
def test_forbidden_raw_syntax_is_blocked(sql, rule, limits):
    result = check(sql, limits)
    assert result.decision == "BLOCK"
    assert result.findings[0]["rule_id"] == rule


@pytest.mark.parametrize("function", [
    "SLEEP(1)", "GET_LOCK('lock', 1)", "LOAD_FILE('file')", "NOW()",
    "CURRENT_USER()", "CAST(id AS CHAR)", "COALESCE(id, 0)", "MOD(id, 2)",
    "ISNULL(id)", "LIKE(id, id)", "TO_DAYS(id)", "database()", "private_function(id)",
])
def test_named_and_specialized_function_nodes_are_blocked(function, limits):
    result = check(f"SELECT {function} FROM orders", limits)
    assert result.decision == "BLOCK"
    assert result.findings[0]["rule_id"] == "FUNCTION_NOT_ALLOWED"


@pytest.mark.parametrize("sql", [
    "WITH recent AS (SELECT id FROM private_table) SELECT id FROM recent",
    "SELECT id FROM orders WHERE EXISTS (SELECT 1 FROM private_table)",
    "SELECT (SELECT id FROM private_table) FROM orders",
    "SELECT o.id FROM orders o JOIN (SELECT id FROM private_table) p ON o.id=p.id",
    "SELECT id FROM orders UNION SELECT id FROM private_table",
    "SELECT SUM(total_amount) OVER (PARTITION BY customer_id) FROM orders",
])
def test_nested_and_set_queries_never_authorize_hidden_sources(sql, limits):
    result = check(sql, limits)
    assert result.decision == "UNKNOWN"
    assert result.findings[0]["rule_id"] == "QUERY_SHAPE"
    assert result.tables == ()


@pytest.mark.parametrize("sql", [
    'SELECT "id" FROM orders',
    "SELECT 'back\\slash' FROM orders",
    "SELECT id || total_amount FROM orders",
    "SELECT !id FROM orders",
    "SELECT id FROM orders WHERE id = ?",
    "SELECT id FROM orders WHERE id = :id",
    "SELECT id FROM orders ORDER BY id NULLS FIRST",
    "SELECT id FROM orders WHERE id ISNULL",
    "SELECT id FROM orders WHERE id NOTNULL",
    "SELECT id FROM orders WHERE id == 1",
    "SELECT `odd``identifier` FROM orders",
    "SELECT id FROM orders\x00",
])
def test_sql_mode_and_unbound_parameter_syntax_is_unknown(sql, limits):
    assert check(sql, limits).decision == "UNKNOWN"


@pytest.mark.parametrize("function", [
    "`COUNT`(id)", "`sum` (id)", "db_agent.COUNT(id)", "db_agent.`SUM`(id)",
    "COUNT (id)", "SUM\n(id)", "AvG\t(id)",
])
def test_function_name_normalization_cannot_authorize_a_routine(function, limits):
    assert check(f"SELECT {function} FROM orders", limits).decision != "ALLOW"


def test_case_insensitive_unqualified_builtin_spelling_is_allowed(limits):
    assert check("SELECT cOuNt(id), sUm(total_amount) FROM orders", limits).decision == "ALLOW"


@pytest.mark.parametrize("sql", [
    "SELECT id FROM Orders",
    "SELECT id FROM DB_AGENT.orders",
    "SELECT id FROM mysql.user",
    "SELECT id FROM private_table",
    "SELECT private.orders.id FROM orders",
    "SELECT server.db_agent.orders.id FROM orders",
    "SELECT id FROM server.db_agent.orders",
    "SELECT orders.id FROM orders JOIN private_table p ON orders.id = p.id",
])
def test_physical_scope_is_server_owned_and_case_exact(sql, limits):
    assert check(sql, limits).decision == "BLOCK"
    assert check("SELECT id FROM orders", limits, tables=()).decision == "BLOCK"


@pytest.mark.parametrize("sql", [
    "SELECT orders.id FROM orders o",
    "SELECT O.id FROM orders o",
    "SELECT missing.id FROM orders",
    "SELECT db_agent.o.id FROM orders o",
    "SELECT o.id FROM orders o JOIN customers o ON o.id=o.id",
    "SELECT id FROM orders, customers",
    "SELECT o.id FROM orders o CROSS JOIN customers c",
    "SELECT o.id FROM orders o RIGHT JOIN customers c ON o.id=c.id",
    "SELECT id FROM orders NATURAL JOIN customers",
    "SELECT id FROM orders JOIN customers USING(id)",
    "SELECT id FROM orders JOIN customers",
    "SELECT DISTINCT id FROM orders",
    "SELECT id FROM orders GROUP BY id WITH ROLLUP",
    "SELECT MIN(id, 2) FROM orders",
    "SELECT COUNT(id, 2) FROM orders",
    "SELECT COUNT(DISTINCT id) FROM orders",
    "SELECT id FROM orders LIMIT -1",
    "SELECT 1",
])
def test_unimplemented_scope_and_ast_arguments_are_not_silently_accepted(sql, limits):
    assert check(sql, limits).decision == "UNKNOWN"


def test_each_reference_counts_against_table_budget_including_self_joins(limits):
    limited = limits.model_copy(update={"max_tables": 1})
    result = check("SELECT a.id FROM orders a JOIN orders b ON a.id=b.id", limited)
    assert result.decision == "UNKNOWN"
    assert result.findings[0]["rule_id"] == "TABLE_LIMIT"


@pytest.mark.parametrize("sql,overrides,rule", [
    ("SELECT '" + "界" * 30 + "' FROM orders", {"max_sql_bytes": 64}, "SQL_SIZE"),
    ("SELECT id, id, id, id FROM orders", {"max_ast_nodes": 8}, "AST_LIMIT"),
    ("SELECT (((((id))))) FROM orders", {"max_ast_depth": 4}, "AST_LIMIT"),
    ("SELECT id + id + id + id FROM orders", {"max_ast_depth": 4}, "AST_LIMIT"),
])
def test_budget_limits_are_checked_without_database_access(sql, overrides, rule, limits):
    result = check(sql, limits.model_copy(update=overrides))
    assert result.decision == "UNKNOWN"
    assert result.findings[0]["rule_id"] == rule


@pytest.mark.parametrize("sql", [None, "", "  ", "SELECT '", "SELECT FROM", "SELECT '\ud800'"])
def test_invalid_input_has_fixed_safe_findings(sql, limits):
    result = check(sql, limits)
    assert result.decision == "UNKNOWN"
    assert result.tables == ()
    assert result.aliases == {}


def test_literal_values_do_not_enter_result_or_fingerprint(limits):
    first = check("SELECT id FROM orders WHERE status='secret-one' AND id=123 LIMIT 9", limits)
    second = check("SELECT id FROM orders WHERE status='secret-two' AND id=456 LIMIT 1", limits)
    changed = check("SELECT id FROM orders WHERE status='secret-two' OR id=456 LIMIT 1", limits)
    assert first.decision == second.decision == changed.decision == "ALLOW"
    assert first.sql_fingerprint == second.sql_fingerprint
    assert first.sql_fingerprint != changed.sql_fingerprint
    assert "secret" not in str(asdict(first))


def test_parser_failure_does_not_disclose_input_or_exception(monkeypatch, caplog, limits):
    def fail(*args, **kwargs):
        assert kwargs["error_message_context"] == 0
        raise RuntimeError("synthetic-secret-from-parser")

    monkeypatch.setattr("db_agent.policy.sqlglot.parse", fail)
    result = check("SELECT 'synthetic-secret-in-sql' FROM orders", limits)
    assert result.decision == "UNKNOWN"
    assert "synthetic-secret" not in repr(result) + caplog.text


@pytest.mark.parametrize("dialect,database", [("mysql", "db_agent"), ("postgres", "public")])
@pytest.mark.parametrize("expression", [
    "COUNT(CASE WHEN status = 'paid' THEN 1 END)",
    "SUM(CASE WHEN status = 'paid' THEN total_amount ELSE 0 END)",
    "SUM(CASE WHEN (status IS NULL) THEN total_amount ELSE NULL END)",
    "CASE WHEN id < 1 THEN NULL WHEN id = 1 THEN 2 ELSE 3 END",
    "CASE WHEN id > 0 THEN CASE WHEN status IS NULL THEN 1 END ELSE 0 END",
])
def test_searched_case_subset_preserves_full_expression(expression, dialect, database, limits):
    result = check_sql(f"SELECT {expression} FROM orders", database, ("orders",),
                       limits, dialect=dialect)
    assert result.decision == "ALLOW"
    assert result.tables == ("orders",)


@pytest.mark.parametrize("dialect", ["mysql", "postgres"])
@pytest.mark.parametrize("expression", [
    "CASE status WHEN 'paid' THEN 1 ELSE 0 END", "IF(id > 0, 1, 0)",
    "CASE WHEN id > 0 THEN IF(id > 1, 2, 1) ELSE 0 END",
    "CASE WHEN id > 0 THEN COALESCE(total_amount, 0) END",
    "CASE WHEN id > 0 THEN private_function(id) END",
    "CASE WHEN id > 0 THEN (SELECT id FROM private_table) END",
    "CASE WHEN id > 0 THEN @secret END",
    "SUM(CASE WHEN id > 0 THEN 1 END) OVER ()",
])
def test_case_is_not_an_escape_from_existing_boundaries(expression, dialect, limits):
    assert check_sql(f"SELECT {expression} FROM orders", "public", ("orders",),
                     limits, dialect=dialect).decision != "ALLOW"


@pytest.mark.parametrize("sql", [
    'SELECT "o"."id" FROM "public"."orders" AS "o"',
    'SELECT PUBLIC.ORDERS.ID FROM PUBLIC.ORDERS',
    'SELECT O.ID FROM ORDERS O',
    'SELECT SUM (total_amount) FROM orders',
    'SELECT id FROM orders ORDER BY id ASC NULLS LAST',
    'SELECT id FROM orders ORDER BY id DESC NULLS FIRST',
    'SELECT total_amount / 2, 1 / 2, 1e3, 2.5, .5, 5. FROM orders',
])
def test_postgres_lexing_and_identifier_folding(sql, limits):
    result = check_sql(sql, "public", ("orders",), limits, dialect="postgres")
    assert result.decision == "ALLOW"
    assert result.tables == ("orders",)


@pytest.mark.parametrize("sql", [
    'SELECT id FROM "Orders"', 'SELECT id FROM "PUBLIC".orders',
    'SELECT id FROM other.orders', 'SELECT id FROM app.public.orders',
    'SELECT orders.id FROM orders AS "Orders"',
    'SELECT "O".id FROM orders o',
    'SELECT id FROM "' + "a" * 64 + '"',
    'SELECT id FROM ' + "a" * 64,
    'SELECT id FROM `orders`', 'SELECT id::text FROM orders',
    "SELECT E'abc' FROM orders", "SELECT N'abc' FROM orders",
    "SELECT B'01' FROM orders", "SELECT $$abc$$ FROM orders",
    "SELECT $tag$abc$tag$ FROM orders", "SELECT $1 FROM orders",
    "SELECT id FROM orders WHERE id = %(id)s", "SELECT id FROM orders WHERE id = :id",
    'SELECT pg_catalog.sum(total_amount) FROM orders',
    'SELECT "sum"(total_amount) FROM orders',
    "SELECT id FROM orders FOR SHARE", "SELECT id INTO other FROM orders",
    "SELECT id FROM orders; SELECT id FROM orders",
    "SELECT DISTINCT id FROM orders", "SELECT COUNT(DISTINCT id) FROM orders",
    "SELECT id FROM orders LIMIT 1,2", "SELECT id FROM ONLY orders",
    "SELECT id FROM orders TABLESAMPLE SYSTEM (1)",
    "SELECT id FROM orders LIMIT ALL", "SELECT id FROM orders LIMIT NULL",
    "SELECT 1_000 FROM orders", "SELECT 1.2_3 FROM orders", "SELECT 1e1_2 FROM orders",
    "SELECT 0x12 FROM orders", "SELECT 0o12 FROM orders", "SELECT 0b11 FROM orders",
    "SELECT id AS 'Label' FROM orders", "SELECT id FROM orders AS 'o'",
    "SELECT 'a'\n'b' FROM orders",
])
def test_postgres_unsupported_or_unauthorized_raw_forms_fail_closed(sql, limits):
    assert check_sql(sql, "public", ("orders", "a" * 64), limits,
                     dialect="postgres").decision != "ALLOW"


def test_postgres_quoted_names_remain_case_sensitive(limits):
    allowed = check_sql('SELECT "Orders"."Id" FROM "Orders"', "public", ("Orders",),
                        limits, dialect="postgres")
    denied = check_sql('SELECT id FROM Orders', "public", ("Orders",), limits,
                       dialect="postgres")
    assert allowed.decision == "ALLOW" and allowed.tables == ("Orders",)
    assert denied.decision == "BLOCK"


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql", "MYSQL", None, [], {}])
def test_unknown_dialects_do_not_fall_back_to_mysql(dialect, limits):
    result = check_sql("SELECT id FROM orders", "public", ("orders",), limits,
                       dialect=dialect)
    assert result.decision == "UNKNOWN"
    assert result.findings[0]["rule_id"] == "UNSUPPORTED_DIALECT"


@pytest.mark.parametrize("column", ["tableoid", "xmin", "cmin", "xmax", "cmax", "ctid"])
@pytest.mark.parametrize("spelling", ["unquoted", "upper_unquoted", "quoted"])
def test_postgres_system_columns_are_explicitly_blocked(column, spelling, limits):
    value = column.upper() if spelling == "upper_unquoted" else (
        f'"{column}"' if spelling == "quoted" else column
    )
    result = check_sql(f"SELECT orders.{value} FROM orders", "public", ("orders",),
                       limits, dialect="postgres")
    assert result.decision == "BLOCK" and result.findings[0]["rule_id"] == "SYSTEM_COLUMN"
