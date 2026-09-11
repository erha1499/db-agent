"""Offline HTTP protocol evidence, not proof that a real model catches every omission."""

import asyncio
import hashlib
import json

import httpx
import pytest
import sqlglot
from openai import DefaultAsyncHttpxClient
from sqlglot import exp
from test_agent import captured_http_clients as captured_http_clients
from test_agent import isolated_environment as isolated_environment
from test_agent import stub_server as stub_server
from test_agent import tool_completion

from db_agent import agent as agent_module
from db_agent.config import load_database_settings, load_settings
from db_agent.db import MetadataConnector
from db_agent.query import QueryService
from db_agent.query_conventions import QUERY_CONVENTIONS
from db_agent.records import RunRecord

SQL = "SELECT id FROM orders WHERE id < 10 ORDER BY id"
BAD_SQL = (
    "SELECT c.id, COUNT(o.id) FROM customers c LEFT JOIN orders o ON o.customer_id = c.id "
    "WHERE c.id < 10 GROUP BY c.id ORDER BY c.id"
)
FIXED_SQL = BAD_SQL.replace(" ORDER BY", " HAVING COUNT(o.id) = 0 ORDER BY")
PROMPT = "只查询id小于10且没有订单的客户，返回客户id、订单数，按客户id升序。"


def review(verdict="match", replacement=None):
    return tool_completion(("SemanticReview", {
        "verdict": verdict,
        "checks": {name: "synthetic-review-evidence" for name in (
            "scope", "filters", "time", "aggregation", "columns", "ordering",
        )},
        "issues": [] if verdict == "match" else ["synthetic-missing-filter"],
        "replacement_sql": replacement,
    }))


def intent_payload(sql=SQL):
    """Construct a model fixture from fixed test SQL, never from the production compiler."""
    tree = sqlglot.parse_one(sql, read="mysql")

    def source(table):
        return {"table": table.name, "alias": table.alias or None}

    def predicate(clause):
        node = tree.args.get(clause)
        return {"sql": node.this.sql(dialect="mysql")} if node else None

    return {
        "query": {
            "projections": [node.sql(dialect="mysql") for node in tree.expressions],
            "source": source(tree.args["from_"].this),
            "joins": [{**source(join.this), "kind": "LEFT" if join.side == "LEFT" else "INNER",
                       "on": join.args["on"].sql(dialect="mysql")}
                      for join in tree.args.get("joins", [])],
            "where": predicate("where"), "having": predicate("having"),
            "group_by": (
                [node.sql(dialect="mysql") for node in tree.args["group"].expressions]
                if tree.args.get("group") else []
            ),
            "order_by": [{"expression": node.this.sql(dialect="mysql"),
                          "descending": bool(node.args.get("desc"))}
                         for node in tree.args["order"].expressions]
            if tree.args.get("order") else [],
            "limit": int(tree.args["limit"].expression.this) if tree.args.get("limit") else None,
            "offset": int(tree.args["offset"].expression.this) if tree.args.get("offset") else None,
        },
        "uncertainties": [],
    }


def intent(sql=SQL):
    return tool_completion(("QueryIntent", intent_payload(sql)))


def ast(sql):
    """Ignore quoting and implicit ASC only; preserve every predicate, value and column."""
    tree = sqlglot.parse_one(sql, read="mysql")
    for node in tree.find_all(exp.Identifier):
        node.set("quoted", False)
    for node in tree.find_all(exp.Ordered):
        node.set("desc", bool(node.args.get("desc")))
    return tree


@pytest.fixture
def metadata(monkeypatch):
    calls = []

    async def describe(self, table):
        calls.append(table)
        names = ["id", "name"] if table == "customers" else [
            "id", "order_no", "customer_id", "created_at", "paid_at", "total_amount", "status",
        ]
        return {"database": self.database, "table": table,
                "columns": [{"name": name, "type": "bigint"} for name in names], "indexes": []}

    monkeypatch.setattr(MetadataConnector, "describe_table", describe)
    return calls


@pytest.fixture
def executions(monkeypatch):
    calls = []

    async def execute(self, sql):
        calls.append(sql)
        return {
            "status": "ok", "decision": "ALLOW", "execution_status": "completed",
            "result": {"columns": [{"name": "id", "type": "bigint"}],
                       "rows": [[4]], "row_count": 1, "truncated": False},
        }

    monkeypatch.setattr(QueryService, "execute", execute)
    return calls


def run(prompt=PROMPT, record=None):
    return asyncio.run(agent_module.run_agent_observed(
        prompt, load_settings(), MetadataConnector(load_database_settings()), record,
    ))


@pytest.mark.parametrize("prompt,sql", [
    (
        "帮我查询最近的10个订单",
        "SELECT id, order_no, customer_id, status, total_amount, created_at, paid_at "
        "FROM orders ORDER BY created_at DESC LIMIT 10",
    ),
    (
        "查询最近支付的3个订单，只返回id，按paid_at降序",
        "SELECT id FROM orders WHERE paid_at IS NOT NULL ORDER BY paid_at DESC LIMIT 3",
    ),
])
def test_record_browsing_conventions_reach_all_three_stages_without_bypassing_review(
    stub_server, metadata, executions, prompt, sql,
):
    # These model responses are fixtures; live interpretation is verified separately.
    stub_server["responses"] = [
        tool_completion(("describe_table", {"table": "orders"})),
        tool_completion(("execute_query", {"sql": sql})), intent(sql), review(),
    ]
    observed = run(prompt)
    assert executions == [sql]
    assert observed.model_calls == len(stub_server["requests"]) == 4
    assert observed.query_intents[0]["selection"] == "AST_MATCH"
    assert observed.semantic_reviews[0]["verdict"] == "match"
    for request in stub_server["requests"]:
        assert QUERY_CONVENTIONS in request["body"]["messages"][0]["content"]
    contract_context = json.loads(stub_server["requests"][2]["body"]["messages"][-1]["content"])
    assert contract_context["user_request"] == prompt
    assert set(contract_context) == {"user_request", "schemas"}


@pytest.mark.parametrize("stage", ["intent", "review"])
def test_record_browsing_defaults_cannot_override_unresolved_requirements(
    stub_server, metadata, executions, stage,
):
    sql = "SELECT id FROM orders ORDER BY created_at DESC LIMIT 10"
    unresolved = {"query": None, "uncertainties": ["实际结构缺少可确定的创建时间字段"]}
    stub_server["responses"] = [
        tool_completion(("execute_query", {"sql": sql})),
        tool_completion(("QueryIntent", unresolved)) if stage == "intent" else intent(sql),
        *([review("uncertain")] if stage == "review" else []),
    ]
    observed = run("帮我查询最近的10个订单")
    assert executions == []
    assert observed.queries[0].report["execution_status"] == "not_started"
    assert observed.queries[0].report["error"]["code"] == "SEMANTIC_UNCERTAIN"
    assert len(observed.semantic_reviews) == (0 if stage == "intent" else 1)


def test_contract_replaces_incomplete_candidate_before_the_only_review(
    stub_server, metadata, executions,
):
    producer = tool_completion(("execute_query", {"sql": BAD_SQL}))
    producer["choices"][0]["message"]["content"] = "synthetic-producer-justification"
    stub_server["responses"] = [producer, intent(FIXED_SQL), review()]
    with RunRecord() as record:
        observed = run(record=record)
    assert len(executions) == 1 and ast(executions[0]) == ast(FIXED_SQL)
    assert observed.queries[0].sql == executions[0]
    assert observed.model_calls == len(stub_server["requests"]) == 3
    assert observed.tool_calls.count("execute_query") == 1
    assert sorted(metadata) == ["customers", "orders"]
    assert [item["verdict"] for item in observed.semantic_reviews] == ["match"]
    captured = observed.query_intents[0]
    assert captured["request_sha256"] == hashlib.sha256(PROMPT.encode("utf-8")).hexdigest()
    assert captured["candidate_sql"] == BAD_SQL and captured["selection"] == "AST_DIFFERENT"
    assert captured["selected_sql"] == executions[0]

    for index in (1, 2):
        request = stub_server["requests"][index]["body"]
        assert request["tool_choice"] == "auto"
        messages = request["messages"]
        assert [message["role"] for message in messages] == ["system", "user"]
        context = json.loads(messages[-1]["content"])
        assert context["user_request"] == PROMPT
        assert {schema["table"] for schema in context["schemas"]} == {"orders", "customers"}
        assert set(context) == (
            {"user_request", "schemas"} if index == 1
            else {"user_request", "schemas", "candidate_sql"}
        )
        if index == 2:
            assert context["candidate_sql"] == executions[0]
        for private in (BAD_SQL, "synthetic-producer-justification", "synthetic-review-evidence"):
            assert private not in json.dumps(context)
        assert '"rows"' not in json.dumps(context)
    logs = record.path.read_text()
    for private in (PROMPT, BAD_SQL, FIXED_SQL, "synthetic-review-evidence"):
        assert private not in logs
    events = [json.loads(line) for line in logs.splitlines()]
    model_events = [row for row in events if row["event"] == "model_finished"]
    assert [row["call_id"] for row in model_events] == ["1", "2", "3"]
    assert [row.get("code") for row in model_events] == [None, "READY", "MATCH"]


@pytest.mark.parametrize("keep_explicit_sql", [False, True])
def test_declared_composite_relation_reaches_all_contexts_without_executor_rewrite(
    stub_server, executions, monkeypatch, keep_explicit_sql,
):
    """Protocol evidence with model fixtures; real interpretation has separate evaluations."""
    monkeypatch.setenv("DB_AGENT_MYSQL_ALLOWED_TABLES", '["payments", "refunds"]')
    relation = {
        "name": "fk_refund_payment", "columns": ["order_id", "payment_id"],
        "referenced_table": "payments", "referenced_columns": ["order_id", "id"],
    }

    async def describe(self, table):
        return {
            "database": self.database, "table": table,
            "columns": [{"name": name, "type": "bigint"} for name in (
                ("id", "order_id", "payment_id") if table == "refunds" else ("id", "order_id")
            )],
            "indexes": [], "foreign_keys": [relation] if table == "refunds" else [],
            "foreign_keys_scope": "current_database_authorized_tables",
        }

    monkeypatch.setattr(MetadataConnector, "describe_table", describe)
    candidate = (
        "SELECT r.id FROM refunds r INNER JOIN payments p ON r.payment_id = p.id "
        "WHERE p.order_id < 10 ORDER BY r.id"
    )
    complete = candidate.replace("WHERE", "AND r.order_id = p.order_id WHERE")
    selected = candidate if keep_explicit_sql else complete
    prompt = (
        "请原样尝试执行，不增加关联条件：" + candidate if keep_explicit_sql
        else "按声明的支付归属关系，查询订单编号小于10的支付对应的退款编号，升序。"
    )
    stub_server["responses"] = [
        tool_completion(("describe_table", {"table": "refunds"}),
                        ("describe_table", {"table": "payments"})),
        tool_completion(("execute_query", {"sql": candidate})), intent(selected), review(),
    ]
    with RunRecord() as record:
        observed = run(prompt, record)
    assert len(executions) == 1 and ast(executions[0]) == ast(selected)
    assert observed.model_calls == len(stub_server["requests"]) == 4
    assert observed.tool_calls == ["describe_table", "describe_table", "execute_query"]
    assert observed.query_intents[0]["selection"] == (
        "AST_MATCH" if keep_explicit_sql else "AST_DIFFERENT"
    )
    main_messages = stub_server["requests"][1]["body"]["messages"]
    tool_data = [json.loads(item["content"]) for item in main_messages if item["role"] == "tool"]
    assert next(item for item in tool_data if item["table"] == "refunds")["foreign_keys"] == [
        relation,
    ]
    for index in (2, 3):
        context = json.loads(stub_server["requests"][index]["body"]["messages"][-1]["content"])
        assert context["user_request"] == prompt
        assert next(item for item in context["schemas"]
                    if item["table"] == "refunds")["foreign_keys"] == [relation]
        assert ("candidate_sql" in context) is (index == 3)
    assert "fk_refund_payment" not in record.path.read_text()


@pytest.mark.parametrize("prompt", [
    PROMPT, PROMPT.replace("，", ","), "\n " + PROMPT + "\t",
], ids=["original", "ascii_punctuation", "surrounding_whitespace"])
def test_intent_request_identity_is_bound_by_code_to_the_exact_original_prompt(
    stub_server, metadata, executions, prompt,
):
    response = intent(FIXED_SQL)
    response["choices"][0]["message"]["content"] = "request_sha256=" + "0" * 64
    stub_server["responses"] = [
        tool_completion(("execute_query", {"sql": BAD_SQL})), response, review(),
    ]
    observed = run(prompt)
    captured = observed.query_intents[0]
    assert captured["request_sha256"] == hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    assert captured["contract"] == intent_payload(FIXED_SQL)
    assert "request_sha256" not in captured["contract"]
    assert len(executions) == 1 and ast(executions[0]) == ast(FIXED_SQL)
    assert observed.model_calls == len(stub_server["requests"]) == 3
    request = stub_server["requests"][1]["body"]
    context = json.loads(request["messages"][-1]["content"])
    assert set(context) == {"user_request", "schemas"} and context["user_request"] == prompt
    parameters = request["tools"][0]["function"]["parameters"]
    assert set(parameters["properties"]) == {"query", "uncertainties"}


@pytest.mark.parametrize("target", ["contract", "query", "predicate"])
def test_model_cannot_supply_an_extra_request_identity_at_any_contract_level(
    stub_server, metadata, executions, target,
):
    payload = intent_payload()
    destination = {
        "contract": payload,
        "query": payload["query"],
        "predicate": payload["query"]["where"],
    }[target]
    destination["request_sha256"] = "0" * 64
    stub_server["responses"] = [
        tool_completion(("execute_query", {"sql": SQL})),
        tool_completion(("QueryIntent", payload)),
    ]
    observed = run()
    assert executions == [] and observed.query_intents == observed.semantic_reviews == []
    assert observed.model_calls == len(stub_server["requests"]) == 2
    report = observed.queries[0].report
    assert report["execution_status"] == "not_started" and report["result"] is None
    assert report["error"]["code"] == "QUERY_INTENT_INVALID"


@pytest.mark.parametrize("prompt,candidate,expected", [
    (
        "实际查id小于10且status为paid的订单，仅返回id，按id升序。",
        SQL, "SELECT id FROM orders WHERE id < 10 AND status = 'paid' ORDER BY id",
    ),
    (PROMPT, BAD_SQL, FIXED_SQL),
    (
        "实际查支付时间paid_at在2026年二月的订单id，含2月1日、不含3月1日，按id升序。",
        "SELECT id FROM orders WHERE created_at >= '2026-02-01' "
        "AND created_at < '2026-03-01' ORDER BY id",
        "SELECT id FROM orders WHERE paid_at >= '2026-02-01' "
        "AND paid_at < '2026-03-01' ORDER BY id",
    ),
    (
        "实际查支付时间paid_at在2026年二月的订单id，含2月1日、不含3月1日，按id升序。",
        "SELECT id FROM orders WHERE paid_at >= '2026-02-01' "
        "AND paid_at <= '2026-03-01' ORDER BY id",
        "SELECT id FROM orders WHERE paid_at >= '2026-02-01' "
        "AND paid_at < '2026-03-01' ORDER BY id",
    ),
    (
        "实际查id小于10的订单，依次返回id、paid_at、total_amount，按id升序。",
        SQL, "SELECT id, paid_at, total_amount FROM orders WHERE id < 10 ORDER BY id",
    ),
    (
        "实际查id小于10的订单，返回id与total_amount，金额降序、相同金额按id升序。",
        "SELECT id, total_amount FROM orders WHERE id < 10 ORDER BY total_amount ASC, id ASC",
        "SELECT id, total_amount FROM orders WHERE id < 10 ORDER BY total_amount DESC, id ASC",
    ),
], ids=["where", "having", "time_field", "time_boundary", "projection", "ordering"])
def test_deleted_or_changed_requirement_selects_the_complete_contract_sql(
    stub_server, metadata, executions, prompt, candidate, expected,
):
    stub_server["responses"] = [
        tool_completion(("execute_query", {"sql": candidate})), intent(expected), review(),
    ]
    observed = run(prompt)
    assert len(executions) == 1 and ast(executions[0]) == ast(expected)
    assert executions[0] != candidate
    assert observed.query_intents[0]["selection"] == "AST_DIFFERENT"
    assert len(observed.semantic_reviews) == 1
    assert json.loads(stub_server["requests"][2]["body"]["messages"][-1]["content"])[
        "candidate_sql"
    ] == executions[0]


@pytest.mark.parametrize("sql", [
    SQL, "SELECT id FROM orders", BAD_SQL.replace("COUNT(o.id)", "COUNT(o.id) AS order_count"),
])
def test_matching_query_is_preserved_without_adding_unrequested_filters(
    stub_server, metadata, executions, sql,
):
    prompt = "实际查询下述范围的全部对象及统计，不额外增加筛选。"
    stub_server["responses"] = [
        tool_completion(("execute_query", {"sql": sql})), intent(sql), review(),
    ]
    observed = run(prompt)
    assert executions == [sql] and observed.query_intents[0]["selection"] == "AST_MATCH"
    assert observed.model_calls == 3


def test_previously_loaded_schema_is_not_hidden_by_a_candidate_omitting_that_table(
    stub_server, metadata, executions,
):
    stub_server["responses"] = [
        tool_completion(("describe_table", {"table": "customers"})),
        tool_completion(("execute_query", {"sql": SQL})), intent(FIXED_SQL), review(),
    ]
    observed = run()
    assert len(executions) == 1 and ast(executions[0]) == ast(FIXED_SQL)
    assert observed.model_calls == 4 and metadata == ["customers", "orders"]
    context = json.loads(stub_server["requests"][2]["body"]["messages"][-1]["content"])
    assert {schema["table"] for schema in context["schemas"]} == {"customers", "orders"}
    assert "candidate_sql" not in context


@pytest.mark.parametrize("verdict", ["mismatch", "uncertain"])
def test_review_failure_stops_without_a_second_repair_loop(
    stub_server, metadata, executions, verdict,
):
    stub_server["responses"] = [
        tool_completion(("execute_query", {"sql": BAD_SQL})), intent(FIXED_SQL),
        review(verdict, SQL if verdict == "mismatch" else None),
    ]
    observed = run()
    assert executions == [] and observed.model_calls == 3
    assert len(observed.semantic_reviews) == len(observed.query_intents) == 1
    report = observed.queries[0].report
    assert report["execution_status"] == "not_started" and report["result"] is None
    assert report["error"]["code"] == f"SEMANTIC_{verdict.upper()}"
    assert "业务 SQL 未执行" in observed.answer


@pytest.mark.parametrize("failure", ["multiple", "wrong_name", "length", "inconsistent"])
def test_invalid_review_cannot_authorize_execution(stub_server, metadata, executions, failure):
    response = review()
    message = response["choices"][0]["message"]
    if failure == "multiple":
        message["tool_calls"].append(message["tool_calls"][0].copy())
    elif failure == "wrong_name":
        message["tool_calls"][0]["function"]["name"] = "execute_query"
    elif failure == "length":
        response["choices"][0]["finish_reason"] = "length"
    else:
        args = json.loads(message["tool_calls"][0]["function"]["arguments"])
        args["issues"] = ["mismatch despite claimed pass"]
        message["tool_calls"][0]["function"]["arguments"] = json.dumps(args)
    stub_server["responses"] = [
        tool_completion(("execute_query", {"sql": SQL})), intent(), response,
    ]
    observed = run()
    assert executions == [] and len(stub_server["requests"]) == 3
    assert observed.queries[0].report["execution_status"] == "not_started"
    assert observed.queries[0].report["error"]["code"] == "SEMANTIC_REVIEW_INVALID"


@pytest.mark.parametrize("failure", [
    "uncertain", "missing_clause", "unknown_field", "unknown_table", "length",
])
def test_uncertain_or_invalid_contract_stops_before_review_or_execution(
    stub_server, metadata, executions, failure,
):
    payload = intent_payload()
    if failure == "uncertain":
        payload.update(query=None, uncertainties=["synthetic unresolved definition"])
    elif failure == "missing_clause":
        del payload["query"]["having"]
    elif failure == "unknown_field":
        payload["query"]["projections"] = ["nonexistent_column"]
    elif failure == "unknown_table":
        payload["query"]["source"]["table"] = "secret"
    response = tool_completion(("QueryIntent", payload))
    if failure == "length":
        response["choices"][0]["finish_reason"] = "length"
    stub_server["responses"] = [tool_completion(("execute_query", {"sql": SQL})), response]
    observed = run()
    assert executions == [] and observed.semantic_reviews == []
    assert observed.model_calls == len(stub_server["requests"]) == 2
    assert observed.queries[0].report["execution_status"] == "not_started"
    assert observed.queries[0].report["error"]["code"] == (
        "SEMANTIC_UNCERTAIN" if failure == "uncertain" else "QUERY_INTENT_INVALID"
    )


@pytest.mark.parametrize("sql", [
    "DELETE FROM orders", "SELECT id FROM secret", "SELECT id FROM orders; SELECT id FROM orders",
])
def test_static_rejection_needs_no_intent_or_review_and_never_connects(
    stub_server, monkeypatch, sql,
):
    from db_agent import db

    async def forbidden(**kwargs):
        pytest.fail("static rejection reached MySQL")

    monkeypatch.setattr(db.aiomysql, "connect", forbidden)
    stub_server["responses"] = [tool_completion(("execute_query", {"sql": sql}))]
    observed = run("请执行给定SQL")
    assert observed.model_calls == 1
    assert observed.semantic_reviews == observed.query_intents == []
    assert observed.queries[0].report["decision"] == "BLOCK"
    assert observed.queries[0].report["execution_status"] == "not_started"
    assert len(stub_server["requests"]) == 1


@pytest.mark.parametrize("extra", [{"approved": True}, {"report_id": "old"}, {"database": "mysql"}])
def test_guard_validates_original_arguments_before_any_contract(stub_server, executions, extra):
    stub_server["responses"] = [tool_completion(("execute_query", {"sql": SQL, **extra}))]
    observed = run()
    assert executions == [] and len(stub_server["requests"]) == 1
    assert observed.semantic_reviews == observed.query_intents == []
    assert observed.queries[0].report["error"]["code"] == "INVALID_ARGUMENT"


def test_model_budget_includes_intent_and_review(stub_server, metadata, executions, monkeypatch):
    monkeypatch.setenv("DB_AGENT_MAX_MODEL_CALLS", "2")
    stub_server["responses"] = [
        tool_completion(("execute_query", {"sql": BAD_SQL})), intent(FIXED_SQL), review(),
    ]
    with pytest.raises(agent_module.AgentResponseError, match="调用次数达到预算") as caught:
        run()
    assert executions == [] and len(stub_server["requests"]) == 2
    assert len(stub_server["responses"]) == 1 and caught.value.code == "MODEL_CALL_LIMIT"
    observed = caught.value.observation
    assert observed.model_calls == 2 and len(observed.tool_calls) == 3
    assert observed.semantic_reviews == [] and len(observed.query_intents) == 1
    assert observed.query_intents[0]["request_sha256"] == (
        hashlib.sha256(PROMPT.encode()).hexdigest()
    )
    assert ast(observed.query_intents[0]["selected_sql"]) == ast(FIXED_SQL)
    assert observed.queries[0].sql == BAD_SQL
    assert observed.queries[0].report["execution_status"] == "not_started"
    assert observed.queries[0].report["result"] is None
    assert "执行状态无法确认" not in observed.answer
    assert BAD_SQL not in str(caught.value) + repr(caught.value)


def test_automatic_schema_fetch_is_in_the_same_tool_budget(stub_server, executions, monkeypatch):
    monkeypatch.setenv("DB_AGENT_MAX_TOOL_CALLS", "1")
    stub_server["responses"] = [tool_completion(("execute_query", {"sql": SQL}))]
    with pytest.raises(agent_module.AgentResponseError, match="调用次数达到预算") as caught:
        run()
    assert executions == [] and len(stub_server["requests"]) == 1
    assert caught.value.code == "TOOL_CALL_LIMIT"
    observed = caught.value.observation
    assert observed.tool_calls == ["execute_query"] and observed.model_calls == 1
    assert observed.semantic_reviews == observed.query_intents == []
    assert observed.queries[0].report["execution_status"] == "not_started"


@pytest.mark.parametrize("budget", ["model", "tool"])
def test_later_query_budget_failure_retains_completed_query_and_undispatched_status(
    stub_server, metadata, executions, monkeypatch, budget,
):
    monkeypatch.setenv("DB_AGENT_MAX_MODEL_CALLS", "3" if budget == "model" else "4")
    monkeypatch.setenv("DB_AGENT_MAX_TOOL_CALLS", "3" if budget == "tool" else "6")
    second_sql = "SELECT id FROM orders WHERE id = 11" if budget == "model" else BAD_SQL
    stub_server["responses"] = [
        tool_completion(("execute_query", {"sql": SQL}),
                        ("execute_query", {"sql": second_sql})), intent(), review(), intent(),
    ]
    with pytest.raises(agent_module.AgentResponseError, match="调用次数达到预算") as caught:
        with RunRecord() as record:
            run(record=record)
    assert executions == [SQL] and metadata == ["orders"]
    assert len(stub_server["requests"]) == 3 and len(stub_server["responses"]) == 1
    assert caught.value.code == f"{budget.upper()}_CALL_LIMIT"
    observed = caught.value.observation
    assert observed.model_calls == 3
    assert observed.tool_calls == ["execute_query", "describe_table", "execute_query"]
    assert [query.sql for query in observed.queries] == [SQL, second_sql]
    assert [query.report["execution_status"] for query in observed.queries] == [
        "completed", "not_started",
    ]
    assert observed.queries[0].report["result"]["rows"] == [[4]]
    assert observed.queries[1].report["result"] is None
    assert [item["sql"] for item in observed.semantic_reviews] == [SQL]
    assert len(observed.query_intents) == 1
    assert "执行状态无法确认" not in observed.answer
    logs = record.path.read_text()
    events = [json.loads(line) for line in logs.splitlines()]
    failed_tool = [row for row in events if row["event"] == "tool_finished"][-1]
    assert failed_tool["code"] == caught.value.code
    assert events[-1]["status"] == "error"
    for private in (SQL, second_sql, "synthetic-review-evidence"):
        assert private not in logs + str(caught.value) + repr(caught.value)


def test_later_intent_does_not_receive_previous_review_or_query_rows(
    stub_server, metadata, executions,
):
    unresolved = {"query": None, "uncertainties": ["未明确第二项口径"]}
    stub_server["responses"] = [
        tool_completion(("execute_query", {"sql": SQL}),
                        ("execute_query", {"sql": "SELECT id FROM orders WHERE id = 11"})),
        intent(), review(), tool_completion(("QueryIntent", unresolved)),
    ]
    observed = run()
    assert executions == [SQL] and observed.model_calls == 4
    assert len(observed.query_intents) == 2 and len(observed.semantic_reviews) == 1
    assert {item["request_sha256"] for item in observed.query_intents} == {
        hashlib.sha256(PROMPT.encode()).hexdigest(),
    }
    messages = stub_server["requests"][3]["body"]["messages"]
    assert [message["role"] for message in messages] == ["system", "user"]
    context = json.loads(messages[-1]["content"])
    assert set(context) == {"user_request", "schemas"}
    serialized = json.dumps(context)
    for private in (SQL, "synthetic-review-evidence", '"rows"', '"result"'):
        assert private not in serialized


def test_partial_budget_observation_does_not_turn_cli_failure_into_success(
    stub_server, metadata, executions, monkeypatch, capsys,
):
    from db_agent.cli import main

    prompt = "分别查询两项订单数据"
    monkeypatch.setenv("DB_AGENT_MAX_MODEL_CALLS", "3")
    stub_server["responses"] = [
        tool_completion(("execute_query", {"sql": SQL}),
                        ("execute_query", {"sql": "SELECT id FROM orders WHERE id = 11"})),
        intent(SQL), review(),
    ]
    assert main(["chat", prompt]) == 1
    assert executions == [SQL]
    output = capsys.readouterr()
    assert output.out == "" and "调用次数达到预算" in output.err
    assert SQL not in output.err


@pytest.mark.parametrize("stage", ["QueryIntent", "SemanticReview"])
def test_cancellation_during_model_transport_stops_execution_and_closes_clients(
    stub_server, metadata, executions, monkeypatch, captured_http_clients, stage,
):
    async def exercise():
        entered = asyncio.Event()
        original_send = DefaultAsyncHttpxClient.send

        async def blocked_transport(self, request, *args, **kwargs):
            body = json.loads(request.content)
            names = {tool["function"]["name"] for tool in body.get("tools", [])}
            if names == {stage}:
                entered.set()
                await asyncio.Event().wait()
            return await original_send(self, request, *args, **kwargs)

        monkeypatch.setattr(DefaultAsyncHttpxClient, "send", blocked_transport)
        task = asyncio.create_task(agent_module.run_agent_observed(
            PROMPT, load_settings(), MetadataConnector(load_database_settings()),
        ))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    stub_server["responses"] = [tool_completion(("execute_query", {"sql": SQL})), intent()]
    asyncio.run(exercise())
    assert executions == []
    assert len(stub_server["requests"]) == (1 if stage == "QueryIntent" else 2)
    assert len(captured_http_clients) == 2
    assert all(client.is_closed for client in captured_http_clients)


@pytest.mark.parametrize("stage", ["main", "QueryIntent", "SemanticReview"])
def test_provider_request_timeout_is_a_fixed_budget_failure_with_partial_evidence(
    stub_server, metadata, executions, monkeypatch, captured_http_clients, stage,
):
    original_send = DefaultAsyncHttpxClient.send
    attempts = []

    async def timeout_transport(self, request, *args, **kwargs):
        body = json.loads(request.content)
        names = {tool["function"]["name"] for tool in body.get("tools", [])}
        if stage == "main" or names == {stage}:
            attempts.append(stage)
            raise httpx.ReadTimeout("private-provider-timeout-body", request=request)
        return await original_send(self, request, *args, **kwargs)

    monkeypatch.setattr(DefaultAsyncHttpxClient, "send", timeout_transport)
    stub_server["responses"] = [
        tool_completion(("execute_query", {"sql": SQL})), intent(), review(),
    ]
    with pytest.raises(agent_module.AgentResponseError) as caught:
        with RunRecord() as record:
            run(record=record)
    assert caught.value.code == "TIMEOUT" and attempts == [stage]
    observed = caught.value.observation
    expected_calls = {"main": 1, "QueryIntent": 2, "SemanticReview": 3}[stage]
    assert observed.model_calls == expected_calls
    assert len(stub_server["requests"]) == expected_calls - 1
    assert executions == [] and observed.semantic_reviews == []
    assert len(observed.query_intents) == (1 if stage == "SemanticReview" else 0)
    if stage == "main":
        assert observed.queries == [] and observed.tool_calls == []
    else:
        assert observed.tool_calls == ["execute_query", "describe_table"]
        assert observed.queries[0].report["execution_status"] == "not_started"
        assert observed.queries[0].report["error"]["code"] == "TIMEOUT"
        assert "执行状态无法确认" not in observed.answer
    logs = record.path.read_text()
    events = [json.loads(line) for line in logs.splitlines()]
    models = [event for event in events if event["event"] == "model_finished"]
    assert models[-1]["code"] == "TIMEOUT"
    for private in (PROMPT, SQL, "private-provider-timeout-body", "synthetic-review-evidence"):
        assert private not in logs + str(caught.value) + repr(caught.value)
    assert len(captured_http_clients) == 2
    assert all(client.is_closed for client in captured_http_clients)


@pytest.mark.parametrize("stage", ["QueryIntent", "SemanticReview"])
def test_other_provider_failures_keep_their_existing_safe_failure_codes(
    stub_server, metadata, executions, monkeypatch, stage,
):
    original_send = DefaultAsyncHttpxClient.send

    async def failed_transport(self, request, *args, **kwargs):
        body = json.loads(request.content)
        names = {tool["function"]["name"] for tool in body.get("tools", [])}
        if names == {stage}:
            return httpx.Response(
                500, request=request, json={"error": {"message": "private-provider-error-body"}},
            )
        return await original_send(self, request, *args, **kwargs)

    monkeypatch.setattr(DefaultAsyncHttpxClient, "send", failed_transport)
    stub_server["responses"] = [
        tool_completion(("execute_query", {"sql": SQL})), intent(), review(),
    ]
    observed = run()
    assert executions == []
    assert observed.queries[0].report["error"]["code"] == (
        "QUERY_INTENT_FAILED" if stage == "QueryIntent" else "SEMANTIC_REVIEW_FAILED"
    )
    assert "private-provider-error-body" not in observed.answer


def test_total_timeout_retains_completed_query_and_marks_only_pending_review_not_started(
    stub_server, metadata, executions, monkeypatch, captured_http_clients,
):
    original_send = DefaultAsyncHttpxClient.send
    intent_calls = []

    async def blocked_second_intent(self, request, *args, **kwargs):
        body = json.loads(request.content)
        names = {tool["function"]["name"] for tool in body.get("tools", [])}
        if names == {"QueryIntent"}:
            intent_calls.append(1)
            if len(intent_calls) == 2:
                await asyncio.Event().wait()
        return await original_send(self, request, *args, **kwargs)

    monkeypatch.setattr(DefaultAsyncHttpxClient, "send", blocked_second_intent)
    monkeypatch.setenv("DB_AGENT_RUN_TIMEOUT_SECONDS", "0.3")
    second_sql = "SELECT id FROM orders WHERE id = 11"
    stub_server["responses"] = [
        tool_completion(("execute_query", {"sql": SQL}),
                        ("execute_query", {"sql": second_sql})), intent(), review(),
    ]
    with pytest.raises(agent_module.AgentResponseError) as caught:
        run()
    assert caught.value.code == "TIMEOUT" and intent_calls == [1, 1]
    observed = caught.value.observation
    assert executions == [SQL] and len(stub_server["requests"]) == 3
    assert observed.model_calls == 4
    assert [item.sql for item in observed.queries] == [SQL, second_sql]
    assert [item.report["execution_status"] for item in observed.queries] == [
        "completed", "not_started",
    ]
    assert observed.queries[0].report["result"]["rows"] == [[4]]
    assert observed.queries[1].report["result"] is None
    assert len(observed.query_intents) == len(observed.semantic_reviews) == 1
    assert "执行状态无法确认" not in observed.answer
    assert all(client.is_closed for client in captured_http_clients)


def test_total_timeout_after_handler_starts_does_not_claim_sql_was_never_dispatched(
    stub_server, metadata, monkeypatch, captured_http_clients,
):
    state = {"started": False, "cancelled": False}

    async def pending_query(self, sql):
        state["started"] = True
        try:
            await asyncio.Event().wait()
        finally:
            state["cancelled"] = True

    monkeypatch.setattr(QueryService, "execute", pending_query)
    monkeypatch.setenv("DB_AGENT_RUN_TIMEOUT_SECONDS", "0.3")
    stub_server["responses"] = [
        tool_completion(("execute_query", {"sql": SQL})), intent(), review(),
    ]
    with pytest.raises(agent_module.AgentResponseError) as caught:
        run()
    assert caught.value.code == "TIMEOUT" and state == {"started": True, "cancelled": True}
    observed = caught.value.observation
    assert observed.model_calls == 3 and len(stub_server["requests"]) == 3
    assert observed.queries == [] and observed.tool_calls.count("execute_query") == 1
    assert len(observed.query_intents) == len(observed.semantic_reviews) == 1
    assert "执行状态无法确认" in observed.answer and "业务 SQL 未执行" not in observed.answer
    assert all(client.is_closed for client in captured_http_clients)
