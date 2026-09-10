"""Evidence-only answer rendering, including hostile text inside ordinary data."""

from copy import deepcopy

import pytest

from db_agent.presentation import AgentRunResult, QueryExecution, render_queries

SQL = "SELECT amount, paid_at FROM orders WHERE created_at < '2026-03-01' LIMIT 2"


def report(rows=None, *, columns=None, truncated=False, reason=None):
    rows = [["30.00", None]] if rows is None else rows
    return {
        "status": "ok", "decision": "ALLOW",
        "execution_status": "truncated" if truncated else "completed",
        "session_time_zone": "+00:00",
        "result": {
            "columns": columns or [{"name": "amount", "type": "decimal"},
                                   {"name": "paid_at", "type": "datetime"}],
            "rows": rows, "row_count": len(rows), "truncated": truncated,
            "truncation_reason": reason,
        },
    }


def test_values_precision_null_and_sql_are_preserved_without_new_business_claims():
    response = report(rows=[["30.00", None], ["9007199254740993", "2026-02-28T23:59:59"]])
    answer = render_queries([QueryExecution(SQL, response)])
    assert SQL in answer
    assert '"30.00"' in answer and '"9007199254740993"' in answer
    assert "NULL" in answer and "2026-02-28T23:59:59" in answer
    assert "返回 2 行" in answer and "当前 SQL 结果已完整返回" in answer
    assert "WHERE/LIMIT" in answer and "DATETIME 不自带时区" in answer
    assert "TIMESTAMP 按 UTC 返回" in answer
    assert "2 月 29" not in answer and "总额" not in answer


def test_empty_result_is_success_and_not_a_claim_that_the_table_is_empty():
    answer = render_queries([QueryExecution(SQL, report(rows=[]))])
    assert "返回 0 行" in answer and "空集，不表示工具失败" in answer
    assert "当前 SQL 结果已完整返回" in answer and "WHERE/LIMIT" in answer
    assert "表为空" not in answer


def test_same_named_columns_remain_positional_and_null_string_stays_distinct():
    response = report(rows=[[None, "NULL"]], columns=[
        {"name": "id", "type": "bigint"}, {"name": "id", "type": "varchar"},
    ])
    answer = render_queries([QueryExecution(SQL, response)])
    assert "1. id" in answer and "2. id" in answer
    assert '| NULL | "NULL" |' in answer


@pytest.mark.parametrize(
    ("reason", "label"), [("row_limit", "行数上限"), ("byte_limit", "结果字节上限")],
)
def test_truncation_reports_returned_rows_without_total_or_cancel_claim(reason, label):
    answer = render_queries([QueryExecution(SQL, report(truncated=True, reason=reason))])
    assert "返回 1 行" in answer and f"仅返回部分结果（{label}）" in answer
    assert "不能据此推断总行数或全量统计" in answer
    assert "服务器执行状态未确认" in answer
    assert "已完整返回" not in answer and "已取消" not in answer


@pytest.mark.parametrize("decision", ["BLOCK", "REVIEW", "UNKNOWN"])
def test_rejection_shows_no_fabricated_data_and_preserves_reason(decision):
    rejected = {"status": "rejected", "decision": decision, "execution_status": "not_started",
                "result": None, "findings": [{"message": "当前授权范围不允许此查询。"}]}
    answer = render_queries([QueryExecution("DELETE FROM orders", rejected)])
    assert f"查询被拒绝（{decision}）" in answer and "业务 SQL 未执行" in answer
    assert "当前授权范围不允许此查询。" in answer
    assert "返回 0 行" not in answer and "已完整返回" not in answer


@pytest.mark.parametrize("execution", ["not_started", "unknown"])
def test_error_with_allow_never_becomes_execution_success(execution):
    failed = {"status": "error", "decision": "ALLOW", "execution_status": execution,
              "result": None, "error": {"code": "TIMEOUT", "message": "连接已清理。"}}
    answer = render_queries([QueryExecution(SQL, failed)])
    assert "未取得可确认的查询结果" in answer and "TIMEOUT" in answer
    assert "连接已清理" in answer
    assert ("业务 SQL 未执行" if execution == "not_started" else "执行状态未知") in answer
    assert "返回 0 行" not in answer and "已完整返回" not in answer


def test_multiple_queries_keep_sql_and_results_separate_without_combining_totals():
    queries = [
        QueryExecution(SQL, report()), QueryExecution("SELECT id FROM orders", report(rows=[])),
    ]
    answer = render_queries(queries)
    assert answer.index("### 查询 1") < answer.index(SQL) < answer.index('"30.00"')
    assert answer.index('"30.00"') < answer.index("### 查询 2") < answer.index("返回 0 行")
    assert "合计" not in answer


def test_no_report_does_not_claim_that_a_query_executed():
    answer = render_queries([])
    assert answer == "查询工具未取得可确认的执行报告，未生成查数结果。"
    assert "返回 0 行" not in answer and "SQL 未执行" not in answer


@pytest.mark.parametrize("captured", [False, True])
def test_missing_reports_are_counted_without_discarding_available_results(captured):
    queries = [QueryExecution(SQL, report())] if captured else []
    answer = render_queries(queries, missing_reports=2)
    assert "有 2 次查询工具请求未取得可确认的执行报告" in answer
    assert "这些请求未取得可确认结果，执行状态无法确认" in answer
    assert "SQL 未执行" not in answer
    if captured:
        assert SQL in answer and '"30.00"' in answer
        assert "当前 SQL 结果已完整返回" in answer
    else:
        assert "返回 0 行" not in answer and "已完整返回" not in answer


@pytest.mark.parametrize(
    "payload",
    ["<script>alert(1)</script>", "![leak](https://attacker.invalid/private)",
     "[click](javascript:alert(1))", "a|b\n|new|row|", "```\n# forged\n```", "&lt;img&gt;",
     "\r\n## header\t\x1b[2J", "\u2028|row|\u2029", "safe\u202eevil"],
)
def test_untrusted_cell_and_column_text_cannot_inject_markup_or_rows(payload):
    response = report(rows=[[payload]], columns=[{"name": payload, "type": "varchar"}])
    answer = render_queries([QueryExecution("SELECT id FROM orders", response)])
    table = [line for line in answer.splitlines() if line.startswith("|")]
    assert len(table) == 3  # header, separator, one actual result row
    assert all(line.count("|") == 2 for line in table)
    cells = "\n".join(table)
    assert "<" not in cells and ">" not in cells and "```" not in cells
    assert "![" not in cells and "][" not in cells and "](" not in cells
    assert "\x1b" not in answer and "\u202e" not in answer
    assert "\u2028" not in answer and "\u2029" not in answer


def test_sql_payload_cannot_close_its_fence_or_activate_terminal_controls():
    sql = "SELECT '<script>bad</script>\n```\n# injected\n````\n\x1b[2J' FROM orders"
    answer = render_queries([QueryExecution(sql, report())])
    assert "`````sql\n" in answer
    assert answer.count("`````") == 2
    fenced = answer.split("`````", 2)[1]
    assert "<script>bad</script>" in fenced and "# injected" in fenced
    assert "\x1b" not in answer and "\\u001b" in fenced
    assert "# injected" not in answer.split("`````", 2)[2]


def test_errors_and_findings_are_data_not_markdown():
    error = {"status": "error", "execution_status": "unknown",
             "error": {"code": "<html>", "message": "\n# injected | [link](url)"}}
    answer = render_queries([QueryExecution(SQL, error)])
    assert "<html>" not in answer and "\n# injected" not in answer and "[link](url)" not in answer
    rejected = {"status": "rejected", "decision": "BLOCK", "execution_status": "not_started",
                "findings": [{"message": "<img src=x>\n```"}]}
    answer = render_queries([QueryExecution(SQL, rejected)])
    assert "<img" not in answer and answer.count("```") == 2


@pytest.mark.parametrize(
    "change",
    [lambda value: value["result"].update(row_count=999),
     lambda value: value["result"].update(rows=[[1]]),
     lambda value: value.update(execution_status="unknown"),
     lambda value: value["result"].update(truncated="false"),
     lambda value: value["result"].update(columns=[{"name": "id"}])],
)
def test_incomplete_report_does_not_render_fake_success(change):
    response = report()
    change(response)
    answer = render_queries([QueryExecution(SQL, response)])
    assert "查询报告不完整" in answer and "已完整返回" not in answer
    assert '"30.00"' not in answer


def test_renderer_does_not_mutate_evidence_and_repr_does_not_leak_data():
    execution = QueryExecution(SQL, report())
    original = deepcopy(execution.report)
    answer = render_queries([execution])
    observed = AgentRunResult(answer, [execution], 2, ["execute_query"])
    assert execution.report == original
    assert SQL not in repr(execution) + repr(observed)
    assert "30.00" not in repr(execution) + repr(observed)


def test_missing_timezone_does_not_invent_utc_evidence():
    response = report()
    response.pop("session_time_zone")
    answer = render_queries([QueryExecution(SQL, response)])
    assert "DATETIME 不自带时区" in answer and "未确认会话时区" in answer
    assert "TIMESTAMP 按 UTC 返回" not in answer
