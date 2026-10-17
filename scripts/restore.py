"""Restore a real custom-format backup and compare every COPY table with its archive."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from common import Cluster, digest_rows, executable_dir, private_root, safe_failure, save_json


COPY = re.compile(rb'^COPY (.+) \((.+)\) FROM stdin;\n$')


def archive_tables(cluster, archive, sequences):
    """Yield raw COPY rows privately. Never emit this stream to a terminal."""
    with (cluster.root / "archive-read.log").open("ab") as errors:
        process = subprocess.Popen(
            [str(cluster.pg_bin / "pg_restore"), "--data-only", "--no-owner", "--no-acl",
             "--file=-", str(archive)], stdout=subprocess.PIPE, stderr=errors, env=cluster.env())
        header, rows = None, []
        assert process.stdout is not None
        try:
            for line in process.stdout:
                if header is None:
                    sequence = re.match(rb"SELECT pg_catalog.setval\('((?:[^']|'')+)', (-?\d+), (true|false)\);", line)
                    if sequence:
                        sequences.append((sequence.group(1).decode().replace("''", "'"),
                                          int(sequence.group(2)), sequence.group(3) == b"true"))
                    match = COPY.match(line)
                    if match:
                        header = (match.group(1).decode(), match.group(2).decode())
                elif line == b"\\.\n":
                    yield header, digest_rows(rows)
                    header, rows = None, []
                else:
                    rows.append(line)
            if header is not None:
                raise ValueError("Unterminated COPY section in archive")
            if process.wait() != 0:
                raise RuntimeError("Archive data read failed")
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.terminate()
                process.wait()


def database_rows(conn, table, columns):
    # The SQL identifiers come from pg_dump, which is already trusted for restoration.
    with conn.cursor().copy(f"COPY {table} ({columns}) TO STDOUT") as copy:
        pending = b""
        for block in copy:
            pending += bytes(block)
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                yield line + b"\n"
        if pending:
            raise ValueError("Incomplete COPY output")


def restore(args):
    os.umask(0o077)
    root = private_root(args.workspace)
    source = Path(args.dump).expanduser().resolve(strict=True)
    archive = root / "source.dump"
    if archive.exists():
        raise ValueError("Archive already exists in this workspace; use a fresh workspace")
    before = source.stat()
    with source.open("rb") as src, archive.open("xb") as dest:
        shutil.copyfileobj(src, dest)
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Source backup changed during copy; wait for backup completion")
    if archive.open("rb").read(5) != b"PGDMP":
        raise ValueError("A PostgreSQL custom-format archive is required")
    report = {"status": "running", "verified_at_utc": datetime.now(timezone.utc).isoformat(),
              "backup_bytes": archive.stat().st_size,
              "backup_file_mtime_utc": datetime.fromtimestamp(after.st_mtime, timezone.utc).isoformat(),
              "restore_flags": ["--no-owner", "--no-acl", "--single-transaction", "--exit-on-error"],
              "roles_and_acl_restored": False, "external_document_files_restored": False}
    cluster = Cluster(root, executable_dir(args.pg_bin))
    with cluster:
        toc = subprocess.check_output([str(cluster.pg_bin / "pg_restore"), "--list", str(archive)],
                                      env=cluster.env(), stderr=subprocess.DEVNULL, text=True)
        (root / "archive-toc.txt").write_text(toc)
        toc_tables = len(re.findall(r"^\d+;.* TABLE DATA ", toc, re.M))
        if re.search(r"^\d+;.* (BLOB|BLOBS|MATERIALIZED VIEW DATA) ", toc, re.M):
            raise ValueError("Archive contains additional data objects; extend validation before use")
        cluster.create_database(cluster.state["database"])
        if args.placeholder_role:
            from psycopg import sql
            with cluster.connect("postgres") as conn:
                for role in args.placeholder_role:
                    if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                        conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
        report["placeholder_role_count"] = len(args.placeholder_role)
        start = time.perf_counter()
        cluster.run("pg_restore", "--no-owner", "--no-acl", "--single-transaction",
                    "--exit-on-error", "--dbname", cluster.state["database"], archive)
        report["restore_command_seconds"] = round(time.perf_counter() - start, 4)
        checked, private, sequences = [], [], []
        with cluster.connect() as conn:
            conn.execute("SET TIME ZONE 'UTC'")
            conn.execute("SET DateStyle = 'ISO, YMD'")
            conn.execute("SET IntervalStyle = 'postgres'")
            conn.execute("SET extra_float_digits = 3")
            conn.execute("SET bytea_output = 'hex'")
            report["postgresql_version"] = conn.execute("SHOW server_version").fetchone()[0]
            for (table, columns), expected in archive_tables(cluster, archive, sequences):
                actual = digest_rows(database_rows(conn, table, columns))
                matches = expected == actual
                checked.append({"table": table, "rows": actual["rows"], "matches_archive": matches})
                private.append({"table": table, "archive": expected, "restored": actual})
            sequence_matches = [conn.execute(f"SELECT last_value, is_called FROM {name}").fetchone()
                                == (value, called) for name, value, called in sequences]
            report["sequence_set_objects"] = len(re.findall(r"^\d+;.* SEQUENCE SET ", toc, re.M))
            report["verified_sequences"] = len(sequences)
            report["sequence_values_match_archive"] = all(sequence_matches)
            report["invalid_indexes"] = conn.execute(
                "SELECT count(*) FROM pg_index WHERE NOT indisvalid OR NOT indisready").fetchone()[0]
            report["unvalidated_constraints"] = conn.execute(
                "SELECT count(*) FROM pg_constraint WHERE NOT convalidated").fetchone()[0]
            report["constraint_counts"] = dict(conn.execute(
                "SELECT contype, count(*) FROM pg_constraint c JOIN pg_namespace n "
                "ON n.oid=c.connamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
                "GROUP BY contype ORDER BY contype").fetchall())
            report["database_bytes"] = conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        save_json(root / "row-fingerprints.json", private)
        report.update(toc_table_data_objects=toc_tables, verified_tables=len(checked),
                      restored_rows=sum(item["rows"] for item in checked), tables=checked)
        verified = (len(checked) == toc_tables and toc_tables > 0
                    and all(item["matches_archive"] for item in checked)
                    and report["verified_sequences"] == report["sequence_set_objects"]
                    and report["sequence_values_match_archive"]
                    and report["invalid_indexes"] == 0)
        report["status"] = "passed" if verified else "failed"
        save_json(root / "restore-result.json", report)
        if not verified:
            raise RuntimeError("Restored data did not pass archive comparison")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dump", required=True)
    parser.add_argument("--workspace", required=True, help="New private directory outside Git")
    parser.add_argument("--pg-bin")
    parser.add_argument("--placeholder-role", action="append", default=[],
                        help="Create a NOLOGIN placeholder when restored policies reference a role")
    args = parser.parse_args()
    try:
        restore(args)
    except Exception as exc:
        print(json.dumps(safe_failure(exc)))
        raise SystemExit(1)
