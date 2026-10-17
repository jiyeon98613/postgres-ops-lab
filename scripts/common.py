"""Private, socket-only PostgreSQL instances for restore validation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time

import psycopg
from psycopg import sql


def save_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, default=str)
        stream.write("\n")


def private_root(value: str) -> Path:
    requested = Path(value).expanduser().absolute()
    if requested.is_symlink():
        raise ValueError("Workspace must not be a symbolic link")
    root = requested.resolve()
    repository = Path(__file__).resolve().parents[1]
    if root == repository or repository in root.parents:
        raise ValueError("Private workspace must be outside the repository")
    for parent in (root, *root.parents):
        if (parent / ".git").exists():
            raise ValueError("Private workspace must not be inside a Git checkout")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.stat().st_uid != os.getuid():
        raise ValueError("Workspace must belong to the current operating-system user")
    if root.stat().st_mode & 0o077:
        raise ValueError("Workspace must have mode 0700 before use")
    return root


def executable_dir(value: str | None) -> Path:
    if value:
        path = Path(value).resolve()
    else:
        found = shutil.which("pg_config")
        if not found:
            raise ValueError("Pass --pg-bin pointing to PostgreSQL server binaries")
        path = Path(subprocess.check_output([found, "--bindir"], text=True).strip())
    for name in ("initdb", "pg_ctl", "pg_restore", "pg_dump", "pgbench"):
        if not (path / name).is_file():
            raise ValueError(f"Missing PostgreSQL executable: {name}")
    return path


class Cluster:
    def __init__(self, root: Path, pg_bin: Path):
        self.root = root
        self.pg_bin = pg_bin
        self.data = root / "data"
        self.socket = root / "socket"
        if len(str(self.socket)) > 85:
            raise ValueError("Use a shorter private workspace path for Unix sockets")
        self.socket.mkdir(mode=0o700, exist_ok=True)
        state_file = root / "connection.json"
        if state_file.exists():
            self.state = json.loads(state_file.read_text())
        else:
            if self.data.exists():
                raise ValueError("Existing data directory without matching lab connection state")
            self.state = {"user": "lab_admin", "password": secrets.token_urlsafe(32),
                          "port": 6543, "database": "restore_check"}
            save_json(state_file, self.state)
        self.running = False

    def env(self, database="postgres", user=None, password=None):
        env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
        env.update(PGHOST=str(self.socket), PGPORT=str(self.state["port"]),
                   PGUSER=user or self.state["user"],
                   PGPASSWORD=password or self.state["password"], PGDATABASE=database,
                   PGCONNECT_TIMEOUT="5")
        return env

    def run(self, name, *args, database="postgres", stdout=None):
        with (self.root / "commands.log").open("ab") as log:
            result = subprocess.run([str(self.pg_bin / name), *map(str, args)],
                                    env=self.env(database), stdout=stdout or log, stderr=log)
        if result.returncode:
            raise RuntimeError(f"{name} failed; details are only in the private workspace")

    def start(self):
        if self.running:
            return self
        if (self.data / "postmaster.pid").exists():
            raise RuntimeError("A postmaster.pid already exists; inspect the private instance first")
        if not (self.data / "PG_VERSION").exists():
            password_file = self.root / "init-password"
            fd = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as out:
                out.write(self.state["password"] + "\n")
            try:
                self.run("initdb", "-D", self.data, "-U", self.state["user"],
                         "--auth-local=scram-sha-256", "--auth-host=reject",
                         "--pwfile", password_file, "--encoding=UTF8", "--locale=C.UTF-8")
            finally:
                password_file.unlink(missing_ok=True)
            with (self.data / "postgresql.conf").open("a") as out:
                socket = str(self.socket).replace("'", "''")
                out.write(f"\nlisten_addresses = ''\nport = {self.state['port']}\n"
                          f"unix_socket_directories = '{socket}'\n"
                          "unix_socket_permissions = 0700\n"
                          "shared_preload_libraries = 'pg_stat_statements'\n"
                          "track_io_timing = on\nlog_statement = 'none'\n"
                          "log_min_error_statement = 'panic'\n"
                          "log_parameter_max_length_on_error = 0\n")
        self.run("pg_ctl", "-D", self.data, "-l", self.root / "postgres.log", "-w", "start")
        self.running = True
        with self.connect("postgres") as conn:
            actual = Path(conn.execute("SHOW data_directory").fetchone()[0]).resolve()
            if actual != self.data or conn.execute("SHOW listen_addresses").fetchone()[0] != "":
                self.stop()
                raise RuntimeError("Private-instance identity check failed")
        return self

    def connect(self, database=None, *, user=None, password=None, autocommit=False):
        return psycopg.connect(host=str(self.socket), port=self.state["port"],
                               user=user or self.state["user"],
                               password=password or self.state["password"],
                               dbname=database or self.state["database"],
                               connect_timeout=5, autocommit=autocommit,
                               options="-c statement_timeout=120000 -c lock_timeout=5000")

    def create_database(self, name):
        with self.connect("postgres", autocommit=True) as conn:
            if conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone():
                raise ValueError("Destination database already exists; use a new workspace")
            conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))

    def stop(self):
        if self.running:
            self.run("pg_ctl", "-D", self.data, "-m", "fast", "-w", "stop")
            self.running = False

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.stop()


def digest_rows(rows):
    hashes = sorted(hashlib.sha256(row).digest() for row in rows)
    digest = hashlib.sha256()
    for value in hashes:
        digest.update(value)
    return {"rows": len(hashes), "sha256": digest.hexdigest()}


def safe_failure(error):
    # Database exceptions may contain COPY rows or credentials. Do not print str(error).
    return {"status": "failed", "error_type": type(error).__name__,
            "sqlstate": getattr(error, "sqlstate", None)}
