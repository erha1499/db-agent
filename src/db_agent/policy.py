"""Deterministic, offline admission checks for supported MySQL/PostgreSQL SELECT.

SQLGlot is a parser, not an authorization boundary. Raw lexical checks, an exact
AST allowlist and server-owned object scope are all required before plan capture.
No SQL is rewritten here and ALLOW never authorizes business-query execution.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel

from db_agent.config import AnalysisSettings

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_INTEGER = re.compile(r"[0-9]+\Z")
_POSTGRES_NUMBER = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_WRITE_STARTS = {
    "ALTER", "ANALYZE", "CALL", "COMMIT", "CREATE", "DELETE", "DO", "DROP",
    "EXECUTE", "EXPLAIN", "GRANT", "INSERT", "KILL", "LOAD", "LOCK", "MERGE",
    "OPTIMIZE", "RENAME", "REPAIR", "REPLACE", "RESET", "REVOKE", "ROLLBACK",
    "SET", "START", "TRUNCATE", "UNLOCK", "UPDATE", "USE",
}
_AGGREGATES = {exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max}
_AGGREGATE_NAMES = {"COUNT", "SUM", "AVG", "MIN", "MAX"}
_PAREN_KEYWORDS = {"SELECT", "FROM", "WHERE", "ON", "AND", "OR", "NOT", "IN",
                   "HAVING", "BY", "AS", "BETWEEN", "USING", "INDEX", "WHEN", "THEN",
                   "ELSE", "CASE"}
_POSTGRES_SYSTEM_COLUMNS = {"tableoid", "xmin", "cmin", "xmax", "cmax", "ctid"}
_BINARY = {
    exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.EQ, exp.NEQ, exp.GT, exp.GTE,
    exp.LT, exp.LTE, exp.And, exp.Or,
}
_ALLOWED_ARGS: dict[type[exp.Expression], set[str]] = {
    exp.Select: {"expressions", "from_", "joins", "where", "group", "having",
                 "order", "limit", "offset"},
    exp.From: {"this"},
    exp.Join: {"this", "on", "side", "kind"},
    exp.Table: {"this", "db", "alias"},
    exp.TableAlias: {"this"},
    exp.Column: {"this", "table", "db"},
    exp.Identifier: {"this", "quoted"},
    exp.Literal: {"this", "is_string"},
    exp.Alias: {"this", "alias"},
    exp.Star: set(),
    exp.Count: {"this", "big_int"},
    exp.Sum: {"this"},
    exp.Avg: {"this"},
    exp.Min: {"this"},
    exp.Max: {"this"},
    # If is the parser's WHEN container, never permission to invoke IF(...).
    exp.Case: {"ifs", "default"},
    exp.If: {"this", "true"},
    exp.Neg: {"this"},
    exp.Paren: {"this"},
    exp.Not: {"this"},
    exp.Is: {"this", "expression"},
    exp.In: {"this", "expressions"},
    exp.Between: {"this", "low", "high"},
    exp.Like: {"this", "expression"},
    exp.Boolean: {"this"},
    exp.Null: set(),
    exp.Where: {"this"},
    exp.Group: {"expressions"},
    exp.Having: {"this"},
    exp.Order: {"expressions"},
    exp.Ordered: {"this", "desc", "nulls_first"},
    exp.Limit: {"expression"},
    exp.Offset: {"expression"},
    **{kind: {"this", "expression"} for kind in _BINARY},
}
_ALLOWED_ARGS[exp.Div] = {"this", "expression", "safe"}


@dataclass(frozen=True)
class SqlCheck:
    decision: str
    findings: tuple[dict[str, str], ...]
    tables: tuple[str, ...] = ()
    aliases: dict[str, str] = field(default_factory=dict)
    sql_fingerprint: str = ""


def _result(decision: str, rule_id: str, message: str) -> SqlCheck:
    return SqlCheck(decision, ({"rule_id": rule_id, "message": message},))


def _lexical_check(
    sql: str, limits: AnalysisSettings, dialect: str,
) -> tuple[SqlCheck | None, list[str]]:
    """Reject unsafe raw syntax before a permissive parser can erase its meaning."""
    words: list[str] = []
    index = 0
    depth = 0
    ended = False
    while index < len(sql):
        char = sql[index]
        if char in " \t\r\n":
            index += 1
            continue
        if ended:
            return _result("BLOCK", "MULTIPLE_STATEMENTS", "仅支持一条 SELECT 语句。"), words
        if char == ";":
            ended = True
            index += 1
            continue
        if char == "#" or sql.startswith(("--", "/*"), index):
            return _result("BLOCK", "SQL_COMMENT", "本阶段不接受 SQL 注释或优化器提示。"), words
        if char == "@" or sql.startswith(":=", index):
            return _result("BLOCK", "SQL_VARIABLE", "不允许变量读取、赋值或会话操作。"), words
        if char == "\\" or (char == '"' and dialect == "mysql") or sql.startswith("||", index) or (
            char == "!" and not sql.startswith("!=", index)
        ):
            return _result("UNKNOWN", "SQL_MODE_SYNTAX", "该语法存在未支持的模式差异。"), words
        if sql.startswith("==", index):
            return _result("UNKNOWN", "UNSUPPORTED_SYNTAX", "仅支持标准比较运算符。"), words
        if char == "'":
            index += 1
            while index < len(sql):
                if sql[index] == "\\":
                    return _result("UNKNOWN", "SQL_MODE_SYNTAX", "不支持反斜杠转义字符串。"), words
                if sql[index] == "'":
                    if index + 1 < len(sql) and sql[index + 1] == "'":
                        index += 2
                        continue
                    index += 1
                    break
                if ord(sql[index]) < 32 and sql[index] not in "\t\r\n":
                    return _result("UNKNOWN", "SQL_CHARACTER", "SQL 包含未支持的控制字符。"), words
                index += 1
            else:
                return _result("UNKNOWN", "SQL_PARSE", "SQL 无法按支持的方言语法解析。"), words
            if dialect == "postgres" and sql[index:].lstrip().startswith("'"):
                return _result("UNKNOWN", "STRING_SYNTAX", "不支持相邻字符串隐式拼接。"), words
            continue
        if char == "`" and dialect == "postgres":
            return _result("UNKNOWN", "IDENTIFIER_SYNTAX", "PostgreSQL 不支持反引号标识符。"), words
        if char == "`" or char == '"' and dialect == "postgres":
            end = sql.find(char, index + 1)
            if end < 0 or _IDENTIFIER.fullmatch(sql[index + 1:end]) is None or (
                dialect == "postgres" and end - index - 1 > 63
            ):
                return _result("UNKNOWN", "IDENTIFIER_SYNTAX", "仅支持简单 ASCII 标识符。"), words
            if sql[end + 1:].lstrip().startswith("("):
                return _result("BLOCK", "FUNCTION_NOT_ALLOWED", "不允许引用或限定的函数名。"), words
            index = end + 1
            continue
        if dialect == "postgres" and (char.isascii() and char.isdigit() or (
            char == "." and index + 1 < len(sql) and sql[index + 1].isascii()
            and sql[index + 1].isdigit()
        )):
            number = _POSTGRES_NUMBER.match(sql, index)
            end = number.end()
            if end < len(sql) and (sql[end].isalnum() or sql[end] in "_."):
                # PostgreSQL accepts numeric underscores; SQLGlot can instead
                # interpret 1_000 as literal 1 plus alias _000. Do not rewrite it.
                return _result("UNKNOWN", "NUMBER_SYNTAX", "不支持该数字字面量形式。"), words
            index = end
            continue
        if char.isascii() and (char.isalpha() or char == "_"):
            end = index + 1
            while end < len(sql) and sql[end].isascii() and (
                sql[end].isalnum() or sql[end] == "_"
            ):
                end += 1
            words.append(sql[index:end].upper())
            if dialect == "postgres" and sql[end:].startswith("'"):
                return _result("UNKNOWN", "STRING_SYNTAX", "不支持带前缀的字符串。"), words
            if sql[end:].lstrip().startswith("("):
                if sql[:index].rstrip().endswith("."):
                    return _result("BLOCK", "FUNCTION_NOT_ALLOWED", "不允许限定的函数名。"), words
                if (
                    dialect == "mysql" and words[-1] in _AGGREGATE_NAMES
                    and not sql[end:].startswith("(")
                ):
                    return _result("UNKNOWN", "FUNCTION_SYNTAX", "函数名必须紧接左括号。"), words
                # MySQL builders can turn function calls such as MOD, ISNULL
                # and LIKE into ordinary operator ASTs, erasing the call name.
                if words[-1] not in _PAREN_KEYWORDS:
                    words.append("FUNCTION:" + words[-1])
            index = end
            continue
        if char == "(":
            depth += 1
            if depth > limits.max_ast_depth:
                return _result("UNKNOWN", "AST_LIMIT", "SQL 结构超过静态检查预算。"), words
        elif char == ")":
            depth -= 1
        elif char == "," and dialect == "postgres":
            words.append("COMMA")
        elif char not in "0123456789.,+-*/%=<>!":
            return _result("UNKNOWN", "SQL_CHARACTER", "SQL 包含未支持的语法字符。"), words
        index += 1
    if not words:
        return _result("UNKNOWN", "SQL_PARSE", "缺少可解析的 SELECT 语句。"), words
    if words[0] in _WRITE_STARTS:
        return _result("BLOCK", "STATEMENT_TYPE", "仅允许检查只读 SELECT，禁止其他语句。"), words
    if words[0] not in {"SELECT", "WITH"}:
        return _result("UNKNOWN", "STATEMENT_TYPE", "该语句类型不在本阶段支持范围。"), words
    if any(word in {"INTO", "OUTFILE", "DUMPFILE", "PROCEDURE", "LOCK"} for word in words):
        return _result("BLOCK", "SELECT_SIDE_EFFECT", "禁止文件操作、锁定读取及其他副作用。"), words
    if "FOR" in words or any(word in {
        "SQL_SMALL_RESULT", "SQL_BIG_RESULT", "SQL_BUFFER_RESULT", "SQL_CACHE",
        "SQL_NO_CACHE", "SQL_CALC_FOUND_ROWS",
    } for word in words):
        return _result("BLOCK", "SELECT_MODIFIER", "不支持锁定读取或 SELECT 执行修饰符。"), words
    if any(word in {"HIGH_PRIORITY", "LOW_PRIORITY", "STRAIGHT_JOIN"} for word in words):
        return _result("BLOCK", "SELECT_MODIFIER", "不支持改变执行行为的查询修饰符。"), words
    if dialect == "postgres" and "LIMIT" in words and any(
        token in words[words.index("LIMIT"):] for token in ("COMMA", "ALL", "NULL")
    ):
        return _result("UNKNOWN", "LIMIT_SYNTAX", "LIMIT 仅支持非负整数字面量。"), words
    if ("NULLS" in words and dialect == "mysql") or any(
        word in words and "FUNCTION:" + word not in words for word in {"ISNULL", "NOTNULL"}
    ):
        return _result("UNKNOWN", "UNSUPPORTED_SYNTAX", "该语法不在支持的 SQL 子集。"), words
    return None, words


def _nodes(root: exp.Expression, limits: AnalysisSettings) -> list[exp.Expression] | None:
    pending = [(root, 1)]
    nodes = []
    while pending:
        node, depth = pending.pop()
        nodes.append(node)
        if len(nodes) > limits.max_ast_nodes or depth > limits.max_ast_depth:
            return None
        pending.extend((child, depth + 1) for child in node.iter_expressions())
    return nodes


def _active(value: object) -> bool:
    return value is not None and value is not False and value != []


def _fingerprint(root: exp.Expression) -> str:
    def shape(value: object) -> object:
        if isinstance(value, exp.Literal):
            return ["Literal", "string" if value.is_string else "number"]
        if isinstance(value, exp.Boolean):
            return ["Boolean"]
        if isinstance(value, exp.Expression):
            return [type(value).__name__, {
                key: shape(item) for key, item in sorted(value.args.items()) if _active(item)
            }]
        if isinstance(value, list):
            return [shape(item) for item in value]
        return value

    encoded = json.dumps(shape(root), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def check_sql(
    sql: str,
    database: str,
    allowed_tables: tuple[str, ...],
    limits: AnalysisSettings,
    *,
    dialect: str = "mysql",
) -> SqlCheck:
    """Check SQL without I/O; database is the authorized schema for PostgreSQL.

    Dialect and object scope come from the trusted connector, never tool arguments.
    ALLOW permits only guarded EXPLAIN capture, not query execution.
    """
    if dialect not in ("mysql", "postgres"):
        return _result("UNKNOWN", "UNSUPPORTED_DIALECT", "未支持的数据源 SQL 方言。")
    if not isinstance(sql, str) or not sql.strip():
        return _result("UNKNOWN", "SQL_EMPTY", "SQL 不能为空。")
    try:
        size = len(sql.encode("utf-8"))
    except UnicodeError:
        return _result("UNKNOWN", "SQL_CHARACTER", "SQL 包含未支持的字符编码。")
    if size > limits.max_sql_bytes:
        return _result("UNKNOWN", "SQL_SIZE", "SQL 长度超过静态检查预算。")
    failure, words = _lexical_check(sql, limits, dialect)
    if failure is not None:
        return failure
    try:
        # Context zero also prevents SQLGlot's fallback-to-Command warning from
        # logging raw SQL. We never expose parser exceptions to a caller.
        statements = sqlglot.parse(
            sql, read=dialect, error_level=ErrorLevel.RAISE, error_message_context=0,
        )
    except Exception:
        return _result("UNKNOWN", "SQL_PARSE", "SQL 无法按支持的方言语法解析。")
    if len(statements) != 1 or statements[0] is None:
        return _result("BLOCK", "MULTIPLE_STATEMENTS", "仅支持一条 SELECT 语句。")
    root = statements[0]
    nodes = _nodes(root, limits)
    if nodes is None:
        return _result("UNKNOWN", "AST_LIMIT", "SQL 结构超过静态检查预算。")
    if dialect == "postgres":
        # SQLGlot preserves token spelling. PostgreSQL resolves unquoted names
        # in lower case; authorize the server's actual name, not the raw token.
        for node in nodes:
            if isinstance(node, exp.Identifier) and not node.args.get("quoted"):
                node.set("this", node.this.lower())
    if any(isinstance(node, (exp.DML, exp.DDL, exp.Into, exp.Lock)) for node in nodes):
        return _result("BLOCK", "SELECT_SIDE_EFFECT", "禁止写入、文件操作和锁定读取。")
    if not isinstance(root, exp.Select) or any(
        isinstance(node, (exp.Subquery, exp.CTE, exp.Union, exp.Intersect, exp.Except,
                          exp.Window, exp.With))
        or (isinstance(node, exp.Select) and node is not root)
        for node in nodes
    ):
        return _result("UNKNOWN", "QUERY_SHAPE", "本阶段不支持 CTE、子查询、集合运算或窗口。")
    if any(
        word.startswith("FUNCTION:") and word.removeprefix("FUNCTION:") not in _AGGREGATE_NAMES
        for word in words
    ):
        return _result("BLOCK", "FUNCTION_NOT_ALLOWED", "仅允许批准的基础聚合函数。")
    if any(isinstance(node, (exp.Hint, exp.IndexTableHint)) for node in nodes):
        return _result("BLOCK", "SQL_HINT", "本阶段不接受查询或索引提示。")
    if any(
        isinstance(node, (exp.Table, exp.Column))
        and (node.catalog or node.db and node.db != database)
        for node in nodes
    ):
        return _result("BLOCK", "TABLE_SCOPE", "SQL 引用了未授权的数据库或表。")
    for node in nodes:
        if isinstance(node, exp.Func) and type(node) not in _ALLOWED_ARGS:
            return _result("BLOCK", "FUNCTION_NOT_ALLOWED", "仅允许批准的基础聚合函数。")
        if isinstance(node, (exp.Parameter, exp.Placeholder, exp.Var)):
            return _result("BLOCK", "SQL_VARIABLE", "不允许变量、占位参数或会话操作。")
        if isinstance(node, exp.If) and (
            not isinstance(node.parent, exp.Case) or node.arg_key != "ifs"
            or node.this is None or node.args.get("true") is None
        ):
            return _result("BLOCK", "FUNCTION_NOT_ALLOWED", "只支持 CASE 中的 WHEN 条件。")
        if isinstance(node, exp.Case) and not node.args.get("ifs"):
            return _result("UNKNOWN", "CASE_SYNTAX", "CASE 至少需要一个 WHEN 条件。")
        allowed_args = _ALLOWED_ARGS.get(type(node))
        if isinstance(node, exp.Div) and dialect == "postgres":
            allowed_args = allowed_args | {"typed"}
        if allowed_args is None or any(
            key not in allowed_args and _active(value) for key, value in node.args.items()
        ):
            return _result("UNKNOWN", "UNSUPPORTED_SYNTAX", "SQL 包含未支持的节点或语法参数。")
        if isinstance(node, exp.Identifier) and (
            _IDENTIFIER.fullmatch(node.this) is None
            or dialect == "postgres" and len(node.this) > 63
        ):
            return _result("UNKNOWN", "IDENTIFIER_SYNTAX", "仅支持简单 ASCII 标识符。")
        if dialect == "postgres" and isinstance(node, exp.Identifier) and node.args.get("quoted"):
            start, end = node.meta.get("start"), node.meta.get("end")
            if (
                not isinstance(start, int) or not isinstance(end, int)
                or sql[start:end + 1] != f'"{node.this}"'
            ):
                return _result("UNKNOWN", "IDENTIFIER_SYNTAX", "引用标识符必须使用双引号。")
        if dialect == "postgres" and isinstance(node, exp.Column) and (
            node.name in _POSTGRES_SYSTEM_COLUMNS
        ):
            return _result("BLOCK", "SYSTEM_COLUMN", "不允许读取 PostgreSQL 系统列。")
        if isinstance(node, (exp.Limit, exp.Offset)):
            value = node.expression
            if not isinstance(value, exp.Literal) or value.is_string or not _INTEGER.fullmatch(
                value.this
            ):
                return _result("UNKNOWN", "LIMIT_SYNTAX", "LIMIT 和 OFFSET 仅支持非负整数字面量。")
        if type(node) in _AGGREGATES and node.this is None:
            return _result("UNKNOWN", "FUNCTION_ARGUMENTS", "聚合函数参数不在支持范围。")

    source = root.args.get("from_")
    if not isinstance(source, exp.From) or not isinstance(source.this, exp.Table):
        return _result("UNKNOWN", "TABLE_SOURCE", "仅支持以授权物理表为来源的 SELECT。")
    joins = root.args.get("joins") or []
    # A comma join may be represented by the same Join node as explicit JOIN.
    if words.count("JOIN") != len(joins):
        return _result("UNKNOWN", "JOIN_SYNTAX", "仅支持显式 INNER 或 LEFT JOIN ON。")
    for join in joins:
        side = join.args.get("side") or ""
        kind = join.args.get("kind") or ""
        if (
            not isinstance(join.this, exp.Table) or join.args.get("on") is None
            or (side, kind) not in {("", ""), ("", "INNER"), ("LEFT", ""), ("LEFT", "OUTER")}
        ):
            return _result("UNKNOWN", "JOIN_SYNTAX", "仅支持显式 INNER 或 LEFT JOIN ON。")
    tables = [source.this, *(join.this for join in joins)]
    if len(tables) > limits.max_tables:
        return _result("UNKNOWN", "TABLE_LIMIT", "表引用数量超过静态检查预算。")
    aliases: dict[str, str] = {}
    unaliased: set[str] = set()
    for table in tables:
        if table.db and table.db != database or table.catalog or table.name not in allowed_tables:
            return _result("BLOCK", "TABLE_SCOPE", "SQL 引用了未授权的数据库或表。")
        alias = table.alias_or_name
        if alias in aliases:
            return _result("UNKNOWN", "ALIAS_SCOPE", "表别名重复或无法唯一解析。")
        aliases[alias] = table.name
        if not table.alias:
            unaliased.add(table.name)
    for column in (node for node in nodes if isinstance(node, exp.Column)):
        if column.db and column.db != database or column.catalog:
            return _result("BLOCK", "TABLE_SCOPE", "SQL 引用了未授权的数据库或表。")
        if column.table and column.table not in aliases:
            return _result("UNKNOWN", "ALIAS_SCOPE", "列的表限定符无法在当前作用域解析。")
        if column.db and column.table not in unaliased:
            return _result("UNKNOWN", "ALIAS_SCOPE", "带数据库限定的列无法关联到当前物理表。")
    return SqlCheck(
        decision="ALLOW",
        findings=({"rule_id": "STATIC_ALLOW", "message": "静态检查通过，仅可继续受控计划采集。"},),
        tables=tuple(dict.fromkeys(table.name for table in tables)),
        aliases=aliases,
        sql_fingerprint=_fingerprint(root),
    )
