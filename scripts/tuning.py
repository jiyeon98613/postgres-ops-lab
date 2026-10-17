"""Measure the application's existing history index on a private masked restore."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import subprocess
from uuid import UUID

from psycopg import sql

from common import Cluster, executable_dir, private_root, safe_failure, save_json
from mask import prepare


INDEX = "ix_change_items_vendor_business_created"
ROOT = Path(__file__).resolve().parents[1]
SAFE_PLAN_FIELDS = {"Node Type", "Parent Relationship", "Parallel Aware", "Async Capable", "Join Type",
    "Relation Name", "Alias", "Index Name", "Scan Direction", "Startup Cost", "Total Cost",
    "Plan Rows", "Plan Width", "Actual Startup Time", "Actual Total Time", "Actual Rows", "Actual Loops",
    "Rows Removed by Filter", "Rows Removed by Join Filter", "Shared Hit Blocks", "Shared Read Blocks",
    "Shared Dirtied Blocks", "Shared Written Blocks", "Temp Read Blocks", "Temp Written Blocks",
    "Heap Fetches", "Sort Method", "Sort Space Used", "Sort Space Type", "Planning Time", "Execution Time"}


def sanitized_plan(value):
    # Allowlist: filter expressions, constants, sort keys and query text cannot leak through new fields.
    if isinstance(value, list):
        return [sanitized_plan(item) for item in value]
    return {key: sanitized_plan(item) if key in {"Plan", "Plans"} else item
            for key, item in value.items() if key in SAFE_PLAN_FIELDS or key in {"Plan", "Plans"}}


def make_query(conn, vendor, current_set, keys):
    return sql.SQL("""SELECT i.*, s.* FROM staging.change_items AS i
        JOIN staging.change_sets AS s ON s.change_set_id=i.change_set_id AND s.vendor_id=i.vendor_id
        WHERE i.vendor_id={} AND s.change_type='PATIENT_DOCUMENT_DISCOVERY'
          AND s.change_set_id<>{} AND i.business_key IN ({})
          AND i.deleted_at IS NULL AND s.deleted_at IS NULL
        ORDER BY s.created_at DESC, i.created_at DESC""").format(
            sql.Literal(vendor), sql.Literal(current_set), sql.SQL(", ").join(map(sql.Literal, keys))).as_string(conn)


def cases(conn):
    vendors = conn.execute("SELECT i.vendor_id, count(*) FROM staging.change_items i "
        "JOIN staging.change_sets s USING(change_set_id,vendor_id) "
        "WHERE s.change_type='PATIENT_DOCUMENT_DISCOVERY' AND i.deleted_at IS NULL AND s.deleted_at IS NULL "
        "GROUP BY i.vendor_id ORDER BY count(*) DESC, i.vendor_id LIMIT 3").fetchall()
    result = []
    for ordinal, (vendor, _) in enumerate(vendors, 1):
        current = conn.execute("SELECT change_set_id FROM staging.change_sets WHERE vendor_id=%s "
            "AND change_type='PATIENT_DOCUMENT_DISCOVERY' AND deleted_at IS NULL "
            "ORDER BY created_at DESC, change_set_id LIMIT 1", (vendor,)).fetchone()[0]
        keys = [row[0] for row in conn.execute("SELECT i.business_key FROM staging.change_items i "
            "JOIN staging.change_sets s USING(change_set_id,vendor_id) "
            "WHERE i.vendor_id=%s AND s.change_type='PATIENT_DOCUMENT_DISCOVERY' "
            "AND s.change_set_id<>%s AND i.deleted_at IS NULL AND s.deleted_at IS NULL "
            "GROUP BY i.business_key ORDER BY count(*) DESC, i.business_key LIMIT 100", (vendor, current))]
        if not keys:
            continue
        for size in sorted({1, min(10, len(keys)), min(100, len(keys))}):
            result.append((f"vendor_{ordinal}_keys_{size}", make_query(conn, vendor, current, keys[:size])))
    if not result:
        raise ValueError("No previous discovery history exists for this query; choose another candidate")
    return result


def benchmark(cluster, conn, case_name, query, phase, round_number, args):
    for _ in range(3):
        conn.execute(query).fetchall()
    conn.execute("SELECT pg_stat_statements_reset()")
    conn.commit()
    workload = cluster.root / "workload.sql"
    workload.write_text(query + ";\n")
    output = cluster.root / f"pgbench-{case_name}-{phase}-{round_number}.log"
    with output.open("wb") as log:
        result = subprocess.run([str(cluster.pg_bin / "pgbench"), "-n", "-c", str(args.clients),
            "-j", str(min(args.clients, 2)), "-t", str(args.transactions), "-f", str(workload), "query_check"],
            env=cluster.env("query_check"), stdout=log, stderr=log)
    if result.returncode:
        raise RuntimeError("pgbench failed; details remain private")
    columns = ["calls", "rows", "mean_exec_time", "min_exec_time", "max_exec_time", "stddev_exec_time",
               "shared_blks_hit", "shared_blks_read", "shared_blk_read_time", "shared_blk_write_time"]
    stats = conn.execute("SELECT " + ", ".join(columns) + " FROM pg_stat_statements "
        "WHERE dbid=(SELECT oid FROM pg_database WHERE datname=current_database()) "
        "AND query LIKE 'SELECT i.*, s.* FROM staging.change_items AS i%'").fetchall()
    if len(stats) != 1 or stats[0][0] != args.clients * args.transactions:
        raise RuntimeError("Target SQL call count did not match the workload")
    raw_plan = conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query).fetchone()[0]
    return {"case": case_name, "phase": phase, "round": round_number,
            "statistics": dict(zip(columns, stats[0])), "plan": sanitized_plan(raw_plan)}


def tune(args):
    os.umask(0o077)
    root = private_root(args.workspace)
    with Cluster(root, executable_dir(args.pg_bin)) as cluster:
        masking = prepare(cluster)
        with cluster.connect("query_check", autocommit=True) as conn:
            conn.prepare_threshold = None
            definition = conn.execute("SELECT pg_get_indexdef(indexrelid), indisunique "
                "FROM pg_index WHERE indexrelid=to_regclass(%s)", (f"staging.{INDEX}",)).fetchone()
            if definition is None or definition[1]:
                raise ValueError("Expected existing nonunique index is missing or has changed")
            query_cases = cases(conn)
            references = {name: conn.execute(query).fetchall() for name, query in query_cases}
            measurements = []
            has_index = True
            try:
                for round_number in range(1, args.rounds + 1):
                    phases = ("with_index", "without_index") if round_number % 2 else ("without_index", "with_index")
                    for phase in phases:
                        wanted = phase == "with_index"
                        if wanted != has_index:
                            conn.execute(definition[0] if wanted else sql.SQL("DROP INDEX staging.{}").format(sql.Identifier(INDEX)))
                            has_index = wanted
                        for name, query in query_cases:
                            # Same values and multiplicities, independent of nondeterministic tie order.
                            current = conn.execute(query).fetchall()
                            canonical = lambda rows: sorted(json.dumps(row, default=str, sort_keys=True) for row in rows)
                            if canonical(current) != canonical(references[name]):
                                raise RuntimeError("Query results changed between index conditions")
                            measurements.append(benchmark(cluster, conn, name, query, phase, round_number, args))
            finally:
                if not has_index:
                    conn.execute(definition[0])
            summaries = []
            for name, _ in query_cases:
                medians = {phase: statistics.median(item["statistics"]["mean_exec_time"] for item in measurements
                    if item["case"] == name and item["phase"] == phase) for phase in ("with_index", "without_index")}
                summaries.append({"case": name, "returned_rows": len(references[name]), "median_mean_exec_ms": medians,
                                  "without_divided_by_with": medians["without_index"] / medians["with_index"]})
            report = {"status": "passed", "measured_at_utc": datetime.now(timezone.utc).isoformat(),
                "candidate_index": INDEX, "index_preexisted_in_backup": True,
                "selection": "application query predicates and existing index; not production slow-query ranking",
                "clients": args.clients, "transactions_per_client": args.transactions, "rounds": args.rounds,
                "same_result_multiset": True, "masking": masking, "summaries": summaries,
                "measurements": measurements, "production_performance_improvement_claimed": False,
                "limitations": ["warm-cache comparison; no OS cache flush", "local isolated PostgreSQL",
                    "pseudonymized payload can change row size and compression", "index write overhead not measured",
                    "most frequent historical keys; live request distribution is not established"]}
            save_json(root / "tuning-result.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--pg-bin")
    parser.add_argument("--clients", type=int, default=2)
    parser.add_argument("--transactions", type=int, default=100)
    parser.add_argument("--rounds", type=int, default=4)
    args = parser.parse_args()
    if not (1 <= args.clients <= 4 and 10 <= args.transactions <= 1000 and 2 <= args.rounds <= 8):
        parser.error("Use 1–4 clients, 10–1000 transactions, and 2–8 rounds")
    try:
        tune(args)
    except Exception as exc:
        print(json.dumps(safe_failure(exc)))
        raise SystemExit(1)
