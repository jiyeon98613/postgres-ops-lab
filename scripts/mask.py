"""Prepare private pseudonymized copies of the two discovery-history tables."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import secrets
from uuid import UUID

from psycopg import sql
from psycopg.types.json import Jsonb

from common import save_json


SHARED = {"vendor_id", "created_at", "updated_at", "deleted_at", "created_by", "updated_by"}
KNOWN = {
    "change_sets": SHARED | {"change_set_id", "change_type", "processing_stage", "processing_status",
        "review_status", "apply_status", "revision", "lock_version", "source_fingerprint",
        "db_snapshot_fingerprint", "approved_revision", "approved_scope_hash", "approved_by",
        "approved_at", "applied_by", "applied_at", "error_code"},
    "change_items": SHARED | {"change_item_id", "change_set_id", "patient_id", "source_doc_id", "action",
        "business_key", "content_sha256", "semantic_fingerprint", "before_fingerprint", "proposed_values",
        "validation_codes", "review_status", "review_reason_code", "reviewed_by", "reviewed_at",
        "item_revision", "idempotency_key"},
}
CONTROLLED = {"processing_stage", "processing_status", "review_status", "apply_status"}
ALLOWED_STATUS = {"DISCOVERY", "EXTRACTION", "VALIDATION", "MATCHING", "REVIEW", "APPLY",
                  "PENDING", "RUNNING", "SUCCEEDED", "FAILED", "PARTIAL", "COMPLETED", "SKIPPED",
                  "NEEDS_REVIEW", "APPROVED", "REJECTED", "CANCELLED", "NOT_READY", "READY",
                  "APPLYING", "APPLIED", "SUPERSEDED", "NOT_REQUIRED", "EXTRACT", "RECONCILE",
                  "MANUAL_RECOVERY", "INTERRUPTED", "DEFERRED", "PARTIAL_APPLY", "COMPENSATION_REQUIRED"}


class Masker:
    def __init__(self, key, shift):
        self.key, self.shift = key, shift

    def token(self, value, length):
        result, counter = "", 0
        while len(result) < length:
            result += hmac.new(self.key, f"{counter}:{value}".encode(), hashlib.sha256).hexdigest()
            counter += 1
        return result[:length]

    def json_value(self, value):
        if isinstance(value, dict):
            # Keys can contain file names or personal identifiers too.
            return {self.token(key, max(16, len(key))): self.json_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.json_value(item) for item in value]
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, str):
            return self.token(value, len(value))
        if isinstance(value, (int, float)):
            return 0
        raise ValueError("Unsupported JSON value type")

    def value(self, column, value):
        if value is None:
            return None
        if isinstance(value, UUID):
            return UUID(hex=self.token(str(value), 32))
        if isinstance(value, datetime):
            return value + self.shift
        if column in {"proposed_values", "validation_codes"}:
            return Jsonb(self.json_value(value))
        if column in CONTROLLED:
            if value not in ALLOWED_STATUS:
                raise ValueError("An unrecognized state requires masking-rule review")
            return value
        if column == "change_type" and value == "PATIENT_DOCUMENT_DISCOVERY":
            return value
        if isinstance(value, str):
            return self.token(value, len(value))
        if isinstance(value, (int, bool)):
            return value
        raise ValueError("An unrecognized column type requires masking-rule review")


def prepare(cluster):
    result = json.loads((cluster.root / "restore-result.json").read_text())
    if result["status"] != "passed":
        raise ValueError("Restore validation must pass first")
    key = secrets.token_bytes(32)
    (cluster.root / "mask-key").write_bytes(key)
    with cluster.connect() as source:
        source.execute("CREATE SCHEMA pgops_masked")
        origin = source.execute("SELECT min(created_at) FROM staging.change_sets").fetchone()[0]
        if origin is None:
            raise ValueError("The backup has no change-set records for this query")
        anchor = datetime(2020, 1, 1, tzinfo=origin.tzinfo)
        masker = Masker(key, anchor - origin)
        metadata = {}
        for table, allowed in KNOWN.items():
            columns = [row[0] for row in source.execute(
                "SELECT attname FROM pg_attribute WHERE attrelid=%s::regclass "
                "AND attnum>0 AND NOT attisdropped ORDER BY attnum", (f"staging.{table}",))]
            if not columns or set(columns) - allowed:
                raise ValueError("Schema differs from reviewed masking rules")
            identifiers = sql.SQL(", ").join(map(sql.Identifier, columns))
            source.execute(sql.SQL("CREATE TABLE pgops_masked.{} (LIKE staging.{} INCLUDING ALL EXCLUDING INDEXES)")
                           .format(sql.Identifier(table), sql.Identifier(table)))
            # Materialize only these two small tables. No rows or parameter values leave this process.
            rows = source.execute(sql.SQL("SELECT {} FROM staging.{}")
                                  .format(identifiers, sql.Identifier(table))).fetchall()
            with source.cursor().copy(sql.SQL("COPY pgops_masked.{} ({}) FROM STDIN")
                                      .format(sql.Identifier(table), identifiers)) as copy:
                for row in rows:
                    copy.write_row(tuple(masker.value(name, value) for name, value in zip(columns, row)))
            indexes = source.execute("SELECT indexdef FROM pg_indexes WHERE schemaname='staging' "
                                     "AND tablename=%s ORDER BY indexname", (table,)).fetchall()
            for (definition,) in indexes:
                if f" ON staging.{table} " not in definition:
                    raise ValueError("Unexpected index definition")
                source.execute(definition.replace(f" ON staging.{table} ", f" ON pgops_masked.{table} ", 1))
            metadata[table] = {"rows": len(rows), "columns": len(columns), "indexes": len(indexes)}
        def profile(schema):
            return source.execute(sql.SQL("""SELECT count(*), count(DISTINCT i.business_key),
                count(DISTINCT i.vendor_id), count(DISTINCT i.change_set_id),
                count(*) FILTER (WHERE i.deleted_at IS NULL),
                count(*) FILTER (WHERE s.change_type='PATIENT_DOCUMENT_DISCOVERY')
                FROM {}.change_items i JOIN {}.change_sets s USING(change_set_id,vendor_id)""")
                .format(sql.Identifier(schema), sql.Identifier(schema))).fetchone()
        if profile("staging") != profile("pgops_masked"):
            raise ValueError("Masking changed query-key cardinality or join membership")
        source.execute("ANALYZE pgops_masked.change_items")
        source.execute("ANALYZE pgops_masked.change_sets")
    archive = cluster.root / "pgops_masked.dump"
    cluster.run("pg_dump", "-d", cluster.state["database"], "-Fc", "-n", "pgops_masked", "-f", archive)
    cluster.create_database("query_check")
    cluster.run("pg_restore", "--no-owner", "--no-acl", "--exit-on-error", "--single-transaction",
                "-d", "query_check", archive, database="query_check")
    with cluster.connect("query_check") as target:
        target.execute("ALTER SCHEMA pgops_masked RENAME TO staging")
        target.execute("CREATE EXTENSION pg_stat_statements")
        target.execute("ANALYZE")
    report = {"tables": metadata, "row_counts_preserved": True,
              "foreign_keys_to_other_tables_copied": False, "rls_policies_copied": False,
              "timestamps_shifted_by_constant": True, "payload_values_replaced": True,
              "row_width_and_compression_identical": False,
              "data_exported_to_public_repository": False}
    save_json(cluster.root / "mask-result.json", report)
    return report
