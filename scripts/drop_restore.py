"""Reproduce table loss in a disposable restored database and recover from the same archive."""
import argparse
import json
import os
import time

from common import Cluster, digest_rows, executable_dir, private_root, safe_failure, save_json
from restore import archive_tables, database_rows


def drill(args):
    os.umask(0o077)
    root = private_root(args.workspace)
    baseline = json.loads((root / "restore-result.json").read_text())
    if baseline["status"] != "passed":
        raise ValueError("Initial restore validation must pass")
    archive = root / "source.dump"
    with Cluster(root, executable_dir(args.pg_bin)) as cluster:
        for name in ("incident_check", "incident_recovered"):
            cluster.create_database(name)
        flags = ["--no-owner", "--no-acl", "--exit-on-error", "--single-transaction"]
        cluster.run("pg_restore", *flags, "-d", "incident_check", archive)
        with cluster.connect("incident_check") as conn:
            before = conn.execute("SELECT count(*) FROM staging.change_items").fetchone()[0]
            conn.execute("DROP TABLE staging.change_items CASCADE")
            gone = conn.execute("SELECT to_regclass('staging.change_items') IS NULL").fetchone()[0]
            if not gone:
                raise RuntimeError("Table-loss scenario was not reproduced")
        started = time.perf_counter()
        cluster.run("pg_restore", *flags, "-d", "incident_recovered", archive)
        restore_seconds = time.perf_counter() - started
        tables, sequences = [], []
        with cluster.connect("incident_recovered") as conn:
            conn.execute("SET TIME ZONE 'UTC'")
            conn.execute("SET DateStyle = 'ISO, YMD'")
            conn.execute("SET IntervalStyle = 'postgres'")
            conn.execute("SET extra_float_digits = 3")
            conn.execute("SET bytea_output = 'hex'")
            for (table, columns), expected in archive_tables(cluster, archive, sequences):
                actual = digest_rows(database_rows(conn, table, columns))
                tables.append({"table": table, "rows": actual["rows"], "matches_archive": actual == expected})
            after = conn.execute("SELECT count(*) FROM staging.change_items").fetchone()[0]
            sequence_matches = all(conn.execute(f"SELECT last_value,is_called FROM {name}").fetchone()
                                   == (value, called) for name, value, called in sequences)
        passed = (gone and before == after and len(tables) == baseline["verified_tables"]
                  and all(item["matches_archive"] for item in tables) and sequence_matches)
        report = {"status": "passed" if passed else "failed", "table_drop_confirmed": gone,
            "dropped_table": "staging.change_items", "rows_before_drop": before, "rows_after_restore": after,
            "verified_tables": len(tables), "all_tables_match_archive": all(item["matches_archive"] for item in tables),
            "sequence_values_match_archive": sequence_matches,
            "restore_command_seconds": round(restore_seconds, 4),
            "recovery_point": "snapshot in the completed logical backup", "pitr": False,
            "production_database_modified": False, "application_restart_and_cutover_tested": False}
        save_json(root / "drop-restore-result.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not passed:
        raise RuntimeError("Table-loss recovery verification failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--pg-bin")
    try:
        drill(parser.parse_args())
    except Exception as exc:
        print(json.dumps(safe_failure(exc)))
        raise SystemExit(1)
