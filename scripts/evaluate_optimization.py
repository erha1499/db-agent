"""Explicit, reader-only optimization acceptance over existing public fixtures."""

import argparse
import asyncio
import hashlib
import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from db_agent.config import AnalysisSettings, QuerySettings, load_database_settings
from db_agent.db import MetadataConnector
from db_agent.optimization import OptimizationService
from db_agent.query import QueryService

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evals/optimization_cases.json"


def hashes():
    paths = [CASES, ROOT / "tests/fixtures/mysql_business.sql", Path(__file__).resolve(),
             *(ROOT / "src/db_agent").glob("*.py")]
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def fixed_limits():
    # Init values override BOTH dotenv and environment sources. _env_file=None
    # alone would still permit inherited shell settings to alter acceptance.
    return tuple(kind(_env_file=None, **{name: field.default
                                        for name, field in kind.model_fields.items()})
                 for kind in (AnalysisSettings, QuerySettings))


def oracle_matches(rows, expected, ordered):
    # Literal human-derived rows, independent of product comparison code.
    if ordered:
        return rows == expected
    return Counter(json.dumps(row) for row in rows) == Counter(json.dumps(row) for row in expected)


async def evaluate(args):
    before = hashes()
    settings = load_database_settings()
    if (settings.host, settings.port, settings.database, settings.user) != (
        "127.0.0.1", 13306, "db_agent", "db_agent_reader",
    ):
        raise ValueError("Only the exact existing local synthetic reader target is accepted")
    analysis, query = fixed_limits()
    # Fixed defaults: evaluator cannot raise budgets or authorize additional tables.
    connector = MetadataConnector(settings)
    service = OptimizationService(connector, analysis, query)
    cases = json.loads(CASES.read_text())["cases"]
    report = {"version": "optimization-v1", "started_at": datetime.now(UTC).isoformat(),
              "environment": {"target": "local db_agent reader", "model_calls": 0,
                              "database_writes": 0, "default_budgets": True},
              "source_hashes": before, "requested_repeat": args.repeat,
              "cases": [], "scale": None, "passed": 0, "planned": len(cases)}
    if args.include_ecommerce:
        # This is one performance scenario in addition to the full boundary suite.
        cases.append({"id": "ecommerce_sargable_bounded_range",
                      "original": "SELECT id, total_amount FROM ec_orders "
                                  "WHERE id <= 20000 AND id + 0 = 2",
                      "candidate": "SELECT id, total_amount FROM ec_orders "
                                   "WHERE id <= 20000 AND id = 2",
                      "expected_original": [[2, "90.00"]], "expected_candidate": [[2, "90.00"]],
                      "outcome": "observed_equal"})
        report["planned"] += 1
        # Count only fixed 10,000-ID ranges through QueryService and its full
        # plan/execution checks. No unbounded COUNT, bypass, admin or manifest read.
        scale_started = datetime.now(UTC).isoformat()
        counts = []
        for lower in range(1, 1010001, 10000):
            observed = await QueryService(connector, analysis, query).execute(
                "SELECT COUNT(*) AS n FROM ec_orders "
                f"WHERE id >= {lower} AND id < {lower + 10000}"
            )
            # Independent documented ID domains: audit 1..8 and background
            # 1000..1000991, not an assumed contiguous 1..1000000 sequence.
            expected = sum(max(0, min(lower + 10000, end) - max(lower, begin))
                           for begin, end in ((1, 9), (1000, 1000992)))
            if (observed["status"] != "ok" or observed["execution_status"] != "completed"
                    or observed["result"]["rows"] != [[expected]]):
                raise ValueError("Existing ecommerce-v1 bounded scale check failed")
            counts.append({"id_from": lower, "count": expected,
                           "duration_ms": observed["duration_ms"]})
        maximum = await QueryService(connector, analysis, query).execute(
            "SELECT MIN(id) AS min_id, MAX(id) AS max_id FROM ec_orders"
        )
        if maximum["status"] != "ok" or maximum["result"]["rows"] != [[1, 1000991]]:
            raise ValueError("Existing ecommerce-v1 ID boundary check failed")
        report["scale"] = {"started_at": scale_started,
                           "finished_at": datetime.now(UTC).isoformat(),
                           "observed_order_count": sum(c["count"] for c in counts),
                           "id_boundary": [1, 1000991], "chunks": counts,
                           "atomic_snapshot": False,
                           "note": "101 bounded reader queries during this window; not one atomic "
                                   "database-wide snapshot or a production-size assertion."}
    for case in cases:
        compared = await service.compare(case["original"], case["candidate"], repeat=args.repeat)
        verified = compared["outcome"] == case["outcome"]
        ordered = (compared["structure"] or {}).get("row_mode") == "ordered"
        for trial in compared["trials"]:
            for side in ("original", "candidate"):
                observed = trial[side]
                verified = verified and observed["execution_status"] == "completed"
                verified = verified and oracle_matches(
                    (observed["result"] or {}).get("rows"), case["expected_" + side], ordered,
                )
        verified = verified and len(compared["trials"]) > 0
        if case["outcome"] == "observed_equal":
            verified = verified and compared["completed_trials"] == args.repeat
        report["cases"].append({"id": case["id"], "passed": verified,
                                "original": case["original"], "candidate": case["candidate"],
                                "expected_original": case["expected_original"],
                                "expected_candidate": case["expected_candidate"],
                                "comparison": compared})
        report["passed"] += int(verified)
    report["finished_at"] = datetime.now(UTC).isoformat()
    report["source_unchanged"] = before == hashes()
    directory = ROOT / "outputs/evals/optimization"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{uuid4().hex}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"report": str(path), "passed": report["passed"],
                      "planned": report["planned"],
                      "source_unchanged": report["source_unchanged"]}))
    return 0 if report["passed"] == report["planned"] and report["source_unchanged"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", required=True,
                        help="Explicitly authorize reader queries on existing synthetic data")
    parser.add_argument("--include-ecommerce", action="store_true",
                        help="Also verify 1M existing orders in bounded chunks and compare access")
    parser.add_argument("--repeat", type=int, choices=(1, 2, 3), default=3)
    args = parser.parse_args()
    try:
        return asyncio.run(evaluate(args))
    except Exception:
        print("Optimization acceptance stopped: missing/invalid local evidence; no success claim.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
