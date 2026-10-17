"""Streaming replication and PITR against the isolated, restored PostgreSQL cluster."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import time

from psycopg import sql

from common import Cluster, digest_rows, executable_dir, private_root, safe_failure, save_json
from restore import archive_tables, database_rows


def wait_for(predicate, description, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise TimeoutError(description)


def configure(cluster, settings):
    settings = {"listen_addresses": "", "port": cluster.state["port"],
                "unix_socket_directories": str(cluster.socket), "archive_mode": "off", **settings}
    with (cluster.data / "postgresql.conf").open("a") as out:
        out.write("\n# Isolated recovery exercise\n")
        for key, value in settings.items():
            if isinstance(value, int):
                out.write(f"{key} = {value}\n")
            else:
                out.write(f"{key} = '{str(value).replace(chr(39), chr(39)*2)}'\n")


def basebackup(source, destination, password, standby=False):
    destination.state = dict(source.state)
    save_json(destination.root / "connection.json", destination.state)
    env = source.env(user="lab_replicator", password=password)
    argv = [str(source.pg_bin / "pg_basebackup"), "-D", str(destination.data), "-X", "stream", "-c", "fast"]
    if standby:
        argv += ["-R", "-C", "-S", "pgops_replica"]
    with (source.root / "basebackup.log").open("ab") as log:
        result = subprocess.run(argv, env=env, stdout=log, stderr=log)
    if result.returncode:
        raise RuntimeError("Physical backup failed; details remain in the private workspace")


def verify_archive_tables(cluster, archive):
    checked, sequences = [], []
    with cluster.connect() as conn:
        conn.execute("SET TIME ZONE 'UTC'")
        conn.execute("SET DateStyle = 'ISO, YMD'")
        conn.execute("SET IntervalStyle = 'postgres'")
        conn.execute("SET extra_float_digits = 3")
        conn.execute("SET bytea_output = 'hex'")
        for (table, columns), expected in archive_tables(cluster, archive, sequences):
            checked.append(digest_rows(database_rows(conn, table, columns)) == expected)
        sequences_ok = all(conn.execute(f"SELECT last_value,is_called FROM {name}").fetchone()
                           == (value, called) for name, value, called in sequences)
    return len(checked), all(checked) and sequences_ok


def exercise(args):
    os.umask(0o077)
    root = private_root(args.workspace)
    baseline = json.loads((root / "restore-result.json").read_text())
    if baseline["status"] != "passed":
        raise ValueError("Logical restore validation must pass first")
    archive = root / "wal_archive"
    archive.mkdir(mode=0o700)  # A fresh directory is required; never reuse another cluster's WAL.
    bin_dir = executable_dir(args.pg_bin)
    source = Cluster(root, bin_dir)
    replica = Cluster(private_root(str(root / "replica")), bin_dir)
    recovered = Cluster(private_root(str(root / "pitr")), bin_dir)
    wal_target = str(archive / "%f")
    configure(source, {"archive_mode": "on", "wal_level": "replica",
        "archive_command": f"test ! -f {shlex.quote(wal_target)} && cp %p {shlex.quote(wal_target)}"})
    replication_password = secrets.token_urlsafe(32)
    report = {"verified_at_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "isolated copies on one host; production configuration unchanged"}
    try:
        with source:
            with source.connect(autocommit=True) as primary:
                primary.execute(sql.SQL("CREATE ROLE lab_replicator LOGIN REPLICATION NOSUPERUSER PASSWORD {}")
                                .format(sql.Literal(replication_password)))
                primary.execute("CREATE SCHEMA pgops_recovery_probe")
                primary.execute("CREATE TABLE pgops_recovery_probe.markers (id integer PRIMARY KEY, label text NOT NULL)")
                primary.execute("INSERT INTO pgops_recovery_probe.markers VALUES (1,'before_base')")
                basebackup(source, replica, replication_password, standby=True)
                configure(replica, {})
                replica.start()
                with replica.connect(autocommit=True) as standby:
                    assert standby.execute("SELECT pg_is_in_recovery()").fetchone()[0]
                    wait_for(lambda: standby.execute("SELECT count(*) FROM pgops_recovery_probe.markers").fetchone()[0] == 1,
                             "Initial replication did not catch up")
                    previous_lsn = standby.execute("SELECT pg_last_wal_replay_lsn()::text").fetchone()[0]
                replica.stop()
                primary.execute("INSERT INTO pgops_recovery_probe.markers SELECT i,'while_replica_stopped' FROM generate_series(2,1001) i")
                target_lsn = primary.execute("SELECT pg_current_wal_lsn()::text").fetchone()[0]
                lag_bytes = int(primary.execute("SELECT pg_wal_lsn_diff(%s::pg_lsn,%s::pg_lsn)",
                                               (target_lsn, previous_lsn)).fetchone()[0])
                started = time.perf_counter()
                replica.start()
                with replica.connect(autocommit=True) as standby:
                    wait_for(lambda: standby.execute("SELECT pg_last_wal_replay_lsn()>=%s::pg_lsn", (target_lsn,)).fetchone()[0],
                             "Replica did not replay the target WAL")
                    caught_up_seconds = time.perf_counter() - started
                    marker_count = standby.execute("SELECT count(*) FROM pgops_recovery_probe.markers").fetchone()[0]
                tables, equal = verify_archive_tables(replica, root / "source.dump")
                if marker_count != 1001 or not equal or tables != baseline["verified_tables"]:
                    raise RuntimeError("Replica content verification failed")
                report["replication"] = {"new_rows_while_stopped": 1000, "marker_rows_after_restart": marker_count,
                    "wal_gap_bytes": lag_bytes, "restart_to_target_lsn_seconds": round(caught_up_seconds, 4),
                    "verified_application_tables": tables, "all_application_tables_match_backup": equal,
                    "transport": "Unix socket; asynchronous streaming replication"}
                replica.stop()
                primary.execute("SELECT pg_drop_replication_slot('pgops_replica')")

                basebackup(source, recovered, replication_password)
                primary.execute("INSERT INTO pgops_recovery_probe.markers VALUES (1002,'after_base_before_target')")
                target_time = primary.execute("SELECT clock_timestamp()").fetchone()[0]
                primary.execute("DROP TABLE staging.change_items CASCADE")
                primary.execute("INSERT INTO pgops_recovery_probe.markers VALUES (1003,'after_target')")
                dropped = primary.execute("SELECT to_regclass('staging.change_items') IS NULL").fetchone()[0]
                segment = primary.execute("SELECT pg_walfile_name(pg_current_wal_lsn())").fetchone()[0]
                primary.execute("SELECT pg_switch_wal()")
                wait_for(lambda: (archive / segment).is_file(), "Required WAL segment was not archived")
                archived, failed = primary.execute("SELECT archived_count,failed_count FROM pg_stat_archiver").fetchone()
                configure(recovered, {"restore_command": f"cp {shlex.quote(str(archive / '%f'))} %p",
                    "recovery_target_time": target_time.isoformat(), "recovery_target_timeline": "current",
                    "recovery_target_inclusive": "on", "recovery_target_action": "pause"})
                (recovered.data / "recovery.signal").touch(mode=0o600)
                started = time.perf_counter()
                recovered.start()
                with recovered.connect(autocommit=True) as target:
                    wait_for(lambda: target.execute("SELECT pg_get_wal_replay_pause_state() = 'paused'").fetchone()[0],
                             "Recovery did not pause at the requested time")
                    target_seconds = time.perf_counter() - started
                # At a time target, uncommitted later WAL can still hold relation locks.
                # Finish recovery (and abort incomplete transactions) before checking tables.
                recovered.run("pg_ctl", "-D", recovered.data, "-w", "promote")
                with recovered.connect(autocommit=True) as target:
                    wait_for(lambda: not target.execute("SELECT pg_is_in_recovery()").fetchone()[0], "Promotion failed")
                    count = target.execute("SELECT count(*) FROM pgops_recovery_probe.markers").fetchone()[0]
                    after_base_present = target.execute("SELECT EXISTS(SELECT 1 FROM pgops_recovery_probe.markers WHERE id=1002)").fetchone()[0]
                    after_target_absent = not target.execute("SELECT EXISTS(SELECT 1 FROM pgops_recovery_probe.markers WHERE id=1003)").fetchone()[0]
                    table_present = target.execute("SELECT to_regclass('staging.change_items') IS NOT NULL").fetchone()[0]
                tables, equal = verify_archive_tables(recovered, root / "source.dump")
                if not (dropped and table_present and after_base_present and after_target_absent and count == 1002
                        and equal and tables == baseline["verified_tables"] and failed == 0):
                    raise RuntimeError("Point-in-time recovery verification failed")
                with recovered.connect(autocommit=True) as target:
                    target.execute("INSERT INTO pgops_recovery_probe.markers VALUES (1004,'write_after_recovery')")
                    writable = target.execute("SELECT EXISTS(SELECT 1 FROM pgops_recovery_probe.markers WHERE id=1004)").fetchone()[0]
                report["pitr"] = {"target_time_utc": target_time.isoformat(), "table_drop_confirmed": dropped,
                    "dropped_table_restored": table_present, "post_base_backup_row_recovered": after_base_present,
                    "post_target_row_excluded": after_target_absent, "marker_rows_at_target": count,
                    "verified_application_tables": tables, "all_application_tables_match_backup": equal,
                    "startup_to_target_pause_seconds": round(target_seconds, 4), "write_after_promotion_succeeded": writable,
                    "archived_segments": archived, "archive_failures": failed}
        report["status"] = "passed"
        save_json(root / "physical-result.json", report)
    finally:
        recovered.stop()
        replica.stop()
        source.stop()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--pg-bin")
    try:
        exercise(parser.parse_args())
    except Exception as exc:
        import traceback
        detail = safe_failure(exc)
        detail["frames"] = [{"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
                            for frame in traceback.extract_tb(exc.__traceback__)]
        print(json.dumps(detail))
        raise SystemExit(1)
