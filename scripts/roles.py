"""Test authenticated read-only, application and owner roles on the isolated query database."""
import argparse
import json
import os
import secrets

from psycopg import sql

from common import Cluster, executable_dir, private_root, safe_failure, save_json


def verify(args):
    os.umask(0o077)
    root = private_root(args.workspace)
    passwords = {name: secrets.token_urlsafe(32) for name in ("lab_readonly", "lab_application", "lab_owner")}
    checks = []
    with Cluster(root, executable_dir(args.pg_bin)) as cluster:
        with cluster.connect("query_check") as conn:
            conn.execute("REVOKE CONNECT ON DATABASE query_check FROM PUBLIC")
            conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            conn.execute("REVOKE ALL ON SCHEMA staging FROM PUBLIC")
            conn.execute("REVOKE ALL ON ALL TABLES IN SCHEMA staging FROM PUBLIC")
            for name, password in passwords.items():
                conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}")
                             .format(sql.Identifier(name), sql.Literal(password)))
                conn.execute(sql.SQL("GRANT CONNECT ON DATABASE query_check TO {}").format(sql.Identifier(name)))
                conn.execute(sql.SQL("GRANT USAGE ON SCHEMA staging TO {}").format(sql.Identifier(name)))
            conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA staging TO lab_readonly")
            conn.execute("GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA staging TO lab_application")
            conn.execute("CREATE SCHEMA role_probe AUTHORIZATION lab_owner")
            conn.execute("GRANT USAGE ON SCHEMA role_probe TO lab_readonly, lab_application")
            conn.execute("SET ROLE lab_owner")
            conn.execute("CREATE TABLE role_probe.permissions (id integer PRIMARY KEY, value text NOT NULL)")
            conn.execute("INSERT INTO role_probe.permissions VALUES (1, 'baseline')")
            conn.execute("GRANT SELECT ON role_probe.permissions TO lab_readonly")
            conn.execute("GRANT SELECT, INSERT, UPDATE ON role_probe.permissions TO lab_application")
            conn.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA role_probe GRANT SELECT ON TABLES TO lab_readonly")
            conn.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA role_probe GRANT SELECT, INSERT, UPDATE ON TABLES TO lab_application")
            conn.execute("CREATE TABLE role_probe.future_table (id integer)")
            conn.execute("RESET ROLE")
        tests = [
            ("lab_readonly", "read_history", "SELECT count(*) FROM staging.change_items", None),
            ("lab_readonly", "read_future_table", "SELECT count(*) FROM role_probe.future_table", None),
            ("lab_readonly", "insert_denied", "INSERT INTO role_probe.permissions VALUES (2, 'test')", "42501"),
            ("lab_readonly", "update_denied", "UPDATE role_probe.permissions SET value='test' WHERE id=1", "42501"),
            ("lab_readonly", "delete_denied", "DELETE FROM role_probe.permissions WHERE id=1", "42501"),
            ("lab_readonly", "drop_denied", "DROP TABLE role_probe.permissions", "42501"),
            ("lab_application", "read_history", "SELECT count(*) FROM staging.change_items", None),
            ("lab_application", "insert_allowed", "INSERT INTO role_probe.permissions VALUES (2, 'test')", None),
            ("lab_application", "update_allowed", "UPDATE role_probe.permissions SET value='test' WHERE id=1", None),
            ("lab_application", "delete_denied", "DELETE FROM role_probe.permissions WHERE id=1", "42501"),
            ("lab_application", "ddl_denied", "CREATE TABLE staging.forbidden (id integer)", "42501"),
            ("lab_application", "drop_denied", "DROP TABLE role_probe.permissions", "42501"),
            ("lab_application", "escalation_denied", "SET ROLE lab_owner", "42501"),
            ("lab_owner", "ddl_allowed", "CREATE TABLE role_probe.allowed (id integer)", None),
            ("lab_owner", "delete_allowed", "DELETE FROM role_probe.permissions WHERE id=1", None),
            ("lab_owner", "role_creation_denied", "CREATE ROLE forbidden_role", "42501"),
        ]
        for role, name, statement, expected in tests:
            with cluster.connect("query_check", user=role, password=passwords[role]) as conn:
                observed = None
                try:
                    conn.execute(statement)
                except Exception as exc:
                    observed = getattr(exc, "sqlstate", None) or type(exc).__name__
                finally:
                    conn.rollback()
                checks.append({"role": role, "check": name, "expected_sqlstate": expected,
                               "observed_sqlstate": observed, "passed": observed == expected})
        try:
            with cluster.connect("query_check", user="lab_readonly", password="incorrect-password"):
                denied = False
        except Exception as exc:
            # libpq may omit SQLSTATE for startup/authentication errors.
            denied = type(exc).__name__ == "OperationalError" and "password authentication failed" in str(exc)
        checks.append({"role": "lab_readonly", "check": "wrong_password_denied", "passed": denied})
        report = {"status": "passed" if all(item["passed"] for item in checks) else "failed",
                  "scope": "isolated query database; does not assert production privileges",
                  "authentication": "SCRAM over a private Unix socket", "checks": checks}
        save_json(root / "roles-result.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "passed":
        raise RuntimeError("Role verification failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--pg-bin")
    try:
        verify(parser.parse_args())
    except Exception as exc:
        print(json.dumps(safe_failure(exc)))
        raise SystemExit(1)
