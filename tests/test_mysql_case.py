"""Real read-only MySQL regression of the added CASE subset, using independent values."""

import asyncio
import os

import pytest

from db_agent.config import AnalysisSettings, QuerySettings, load_database_settings
from db_agent.db import MetadataConnector
from db_agent.query import QueryService

pytestmark = pytest.mark.skipif(
    os.environ.get("DB_AGENT_MYSQL_INTEGRATION") != "1",
    reason="requires the existing local MySQL public fixture and reader configuration",
)


@pytest.mark.parametrize(("sql", "expected"), [
    ("SELECT SUM(CASE WHEN status = 'paid' THEN total_amount ELSE 0 END) AS amount "
     "FROM orders", [["130.00"]]),
    ("SELECT COUNT(CASE WHEN status = 'paid' THEN 1 END) AS n FROM orders", [[3]]),
    ("SELECT COUNT(CASE WHEN status = 'paid' THEN 1 ELSE 0 END) AS n FROM orders", [[6]]),
    ("SELECT SUM(CASE WHEN status = 'missing' THEN total_amount END) AS amount "
     "FROM orders", [[None]]),
    ("SELECT c.id, COUNT(CASE WHEN o.status = 'paid' THEN 1 END) AS n "
     "FROM customers c LEFT JOIN orders o ON c.id = o.customer_id GROUP BY c.id ORDER BY c.id",
     [[1, 2], [2, 0], [3, 1], [4, 0], [5, 0]]),
    ("SELECT CASE WHEN total_amount = 0 THEN 'zero' ELSE 'nonzero' END AS kind "
     "FROM orders WHERE id=1004", [["zero"]]),
])
def test_mysql_case_has_independent_expected_result(sql, expected):
    settings = load_database_settings()
    assert settings.kind == "mysql"
    assert (settings.host, settings.port, settings.database, settings.user) == (
        "127.0.0.1", 13306, "db_agent", "db_agent_reader",
    )
    service = QueryService(MetadataConnector(settings), AnalysisSettings(_env_file=None),
                           QuerySettings(_env_file=None))
    report = asyncio.run(service.execute(sql))
    assert report["status"] == "ok", report
    assert report["execution_status"] == "completed"
    assert report["result"]["rows"] == expected
