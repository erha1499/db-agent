"""Local HTTP/metadata/SQL substitutes prove protocol wiring, not model semantics."""

import asyncio
import json
from html import unescape

import pytest
from test_agent import completion, tool_completion
from test_agent import isolated_environment as isolated_environment
from test_agent import stub_server as stub_server
from test_agent_semantics import intent, review
from test_agent_semantics import metadata as metadata
from test_knowledge import confirm, create

from db_agent.agent import AgentResponseError, run_agent_observed
from db_agent.config import load_database_settings, load_settings
from db_agent.db import MetadataConnector
from db_agent.knowledge import KNOWLEDGE_RULES, KnowledgeStore
from db_agent.query import QueryService

SQL = "SELECT id FROM orders WHERE id < 10 ORDER BY id"


@pytest.fixture
def prepared(isolated_environment, metadata, monkeypatch):
    monkeypatch.setenv("DB_AGENT_MAX_OUTPUT_TOKENS", "1024")
    store = KnowledgeStore()
    connector = MetadataConnector(load_database_settings())
    item = confirm(store, connector, create(store, connector))
    executions = []
    original_describe = MetadataConnector.describe_table

    async def describe_on_connection(self, connection, table):
        return await original_describe(self, table)

    async def execute(self, sql):
        checked = self.connector.check_sql(sql, self.analysis_limits)
        if checked.decision != "ALLOW":
            return {"status": "rejected", "decision": checked.decision,
                    "execution_status": "not_started", "result": None}
        if self.before_select:
            await self.before_select(object())
        executions.append(sql)
        return {"status": "ok", "decision": "ALLOW", "execution_status": "completed",
                "result": {"columns": [{"name": "id", "type": "bigint"}],
                           "rows": [[4]], "row_count": 1, "truncated": False}}

    monkeypatch.setattr(MetadataConnector, "_describe_on_connection", describe_on_connection)
    monkeypatch.setattr(QueryService, "execute", execute)
    metadata.clear()
    return store, item, executions


def run(prompt, previous=None):
    return asyncio.run(run_agent_observed(
        prompt, load_settings(), MetadataConnector(load_database_settings()),
        previous_requests=previous,
    ))


def test_new_runs_reload_same_confirmed_knowledge_in_all_model_stages(
    prepared, stub_server, metadata,
):
    store, item, executions = prepared
    prompt = f"[[knowledge:{item['id']}]] 当前明确改成只查id小于10的订单id，按id排序。"
    for _ in range(2):
        stub_server["responses"] = [tool_completion(("execute_query", {"sql": SQL})),
                                    intent(SQL), review()]
        result = run(prompt)
        assert result.model_calls == 3
        assert result.tool_calls == ["describe_table", "execute_query", "describe_table"]
        evidence = result.queries[0].report["business_knowledge"][0]
        assert evidence["id"] == item["id"] and evidence["digest"] == item["digest"]
        assert item["id"] in result.answer and item["payload"]["source"] in unescape(result.answer)
        assert result.query_intents[0]["request_sha256"]
    assert executions == [SQL, SQL]
    assert metadata == ["orders"] * 4
    for batch in (stub_server["requests"][:3], stub_server["requests"][3:]):
        packages = []
        for request in batch:
            messages = request["body"]["messages"]
            assert KNOWLEDGE_RULES in messages[0]["content"]
            assert item["payload"]["definition"] not in messages[0]["content"]
            users = [message["content"] for message in messages if message["role"] == "user"]
            package = json.loads(users[0])
            packages.append(package["confirmed_business_knowledge"])
            assert "synthetic-reader-secret" not in json.dumps(messages)
            assert '"rows"' not in json.dumps(messages, ensure_ascii=False)
        assert packages[0] == packages[1] == packages[2]


@pytest.mark.parametrize("state", ["draft", "revoked", "wrong_scope", "missing"])
def test_unusable_reference_stops_before_model_or_business_execution(prepared, stub_server, state):
    store, item, executions = prepared
    connector = MetadataConnector(load_database_settings())
    if state == "draft":
        item = create(store, connector)
    elif state == "revoked":
        store.revoke(item["id"], connector.knowledge_scope, "changed")
    elif state == "wrong_scope":
        with store.connection() as db:
            db.execute("UPDATE knowledge SET scope='other'")
    else:
        store.path.unlink()
    with pytest.raises(AgentResponseError):
        run(f"[[knowledge:{item['id']}]] 查询")
    assert not stub_server["requests"] and not executions


def test_explicit_new_turn_required_and_no_auto_memory_for_plain_prompt(prepared, stub_server):
    _, item, executions = prepared
    old = f"[[knowledge:{item['id']}]] 查询成交额"
    with pytest.raises(AgentResponseError) as exc:
        run("沿用口径", [old])
    assert exc.value.code == "KNOWLEDGE_REFERENCE_REQUIRED"
    stub_server["response"] = completion("metadata-free response")
    result = run("你好")
    assert result.answer == "metadata-free response"
    assert item["id"] not in json.dumps(stub_server["requests"])
    assert not executions


def test_revocation_during_model_wait_vetoes_dispatch(prepared, stub_server, monkeypatch):
    store, item, executions = prepared
    from db_agent.agent import RuntimeMiddleware

    original = RuntimeMiddleware.review_sql

    async def review_then_revoke(self, *args):
        value = await original(self, *args)
        store.revoke(item["id"], self.connector.knowledge_scope, "changed during model call")
        return value

    monkeypatch.setattr(RuntimeMiddleware, "review_sql", review_then_revoke)
    stub_server["responses"] = [tool_completion(("execute_query", {"sql": SQL})),
                                intent(SQL), review()]
    result = run(f"[[knowledge:{item['id']}]] 查询订单id")
    assert not executions
    assert result.tool_calls.count("execute_query") == 1
    assert "未取得" in result.answer


def test_knowledge_cannot_turn_forbidden_sql_into_allowed_candidate(prepared, stub_server):
    _, item, executions = prepared
    # Original argument is still delivered to the deterministic service rejection;
    # no independent interpreter or repair call may replace it.
    stub_server["responses"] = [tool_completion(("execute_query", {"sql": "DELETE FROM orders"}))]
    result = run(f"[[knowledge:{item['id']}]] 执行 DELETE FROM orders")
    assert result.model_calls == 1
    assert not executions and result.queries[0].report["decision"] == "BLOCK"
    assert not result.query_intents and not result.semantic_reviews


def test_schema_change_after_model_response_blocks_with_current_metadata(
    prepared, stub_server, monkeypatch,
):
    _, item, executions = prepared
    original = MetadataConnector._describe_on_connection

    async def changed(self, connection, table):
        schema = await original(self, connection, table)
        schema["columns"][0]["type"] = "varchar(40)"
        return schema

    monkeypatch.setattr(MetadataConnector, "_describe_on_connection", changed)
    stub_server["responses"] = [tool_completion(("execute_query", {"sql": SQL})),
                                intent(SQL), review()]
    result = run(f"[[knowledge:{item['id']}]] 查询订单id")
    assert not executions and "未取得" in result.answer


def test_final_metadata_still_counts_against_tool_budget(prepared, stub_server, monkeypatch):
    _, item, executions = prepared
    monkeypatch.setenv("DB_AGENT_MAX_TOOL_CALLS", "2")
    stub_server["responses"] = [tool_completion(("execute_query", {"sql": SQL})),
                                intent(SQL), review()]
    result = run(f"[[knowledge:{item['id']}]] 查询订单id")
    assert not executions
    assert result.tool_calls == ["describe_table", "execute_query"]
    assert "未取得" in result.answer
