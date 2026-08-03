#!/usr/bin/env python3
"""Component migrations and fenced state changes for history audit v2."""

import dataclasses
import datetime
import hashlib
import json
import sqlite3


MIGRATION_ID = "history-v1-receipt-quarantine-v1"
_KNOWN_LEGACY_STATUSES = frozenset(
    {
        "complete_match",
        "complete_no_match",
        "uncertain",
        "partial",
        "backend_failed",
        "budget_exceeded",
        "conflicting_evidence",
    }
)
_KNOWN_LEGACY_RELATIONS = frozenset(
    {
        "same_core_idea",
        "same_lineage_revision",
        "related_component",
        "same_failure_mechanism",
        "related_failure_pattern",
        "distinct",
        "uncertain",
    }
)
_FENCE_GUARDS = {}


class AuditMigrationError(RuntimeError):
    pass


class StaleFence(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class Migration:
    component: str
    version: int
    sql: str

    @property
    def sha256(self):
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def _immutable_guards(*tables):
    statements = []
    for table in tables:
        statements.extend(
            (
                f"""CREATE TRIGGER IF NOT EXISTS {table}_immutable_update
BEFORE UPDATE ON {table}
BEGIN
  SELECT RAISE(ABORT, '{table} is immutable');
END;""",
                f"""CREATE TRIGGER IF NOT EXISTS {table}_immutable_delete
BEFORE DELETE ON {table}
BEGIN
  SELECT RAISE(ABORT, '{table} is immutable');
END;""",
            )
        )
    return "\n".join(statements) + "\n"


_LEDGER_SQL = """
CREATE TABLE IF NOT EXISTS audit_schema_migrations(
  component TEXT NOT NULL,
  version INTEGER NOT NULL CHECK(version >= 1),
  migration_sha256 TEXT NOT NULL CHECK(length(migration_sha256) = 64),
  applied_at TEXT NOT NULL,
  PRIMARY KEY(component, version)
);
"""


_IDENTITY_SQL = """
CREATE TABLE audit_run_manifests(
  run_id TEXT PRIMARY KEY,
  manifest_schema_version TEXT NOT NULL
    CHECK(manifest_schema_version = 'history-audit-manifest-v2'),
  plan_hash TEXT NOT NULL CHECK(length(plan_hash) = 64),
  manifest_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE audit_snapshots(
  snapshot_id TEXT PRIMARY KEY,
  snapshot_hash TEXT NOT NULL UNIQUE CHECK(length(snapshot_hash) = 64),
  history_as_of_watermark INTEGER NOT NULL CHECK(history_as_of_watermark >= 0),
  current_batch_id_namespace TEXT NOT NULL
    CHECK(current_batch_id_namespace = 'history-v2-staging-v1'),
  current_batch_ids_hash TEXT NOT NULL CHECK(length(current_batch_ids_hash) = 64),
  exclusion_policy_sha TEXT NOT NULL CHECK(length(exclusion_policy_sha) = 64),
  expected_asset_ids_hash TEXT NOT NULL CHECK(length(expected_asset_ids_hash) = 64),
  created_at TEXT NOT NULL
);
CREATE TABLE audit_batch_staging(
  staging_candidate_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES audit_run_manifests(run_id),
  batch_id TEXT NOT NULL,
  candidate_hash TEXT NOT NULL CHECK(length(candidate_hash) = 64),
  raw_artifact_sha TEXT NOT NULL CHECK(length(raw_artifact_sha) = 64),
  source_order INTEGER NOT NULL CHECK(source_order >= 0),
  created_at TEXT NOT NULL,
  UNIQUE(run_id, batch_id, source_order),
  UNIQUE(run_id, batch_id, candidate_hash)
);
CREATE TABLE audit_batch_pairs(
  run_id TEXT NOT NULL REFERENCES audit_run_manifests(run_id),
  batch_id TEXT NOT NULL,
  left_staging_candidate_id TEXT NOT NULL
    REFERENCES audit_batch_staging(staging_candidate_id),
  right_staging_candidate_id TEXT NOT NULL
    REFERENCES audit_batch_staging(staging_candidate_id),
  pair_plan_sha TEXT NOT NULL CHECK(length(pair_plan_sha) = 64),
  pair_result_sha TEXT NOT NULL CHECK(length(pair_result_sha) = 64),
  created_at TEXT NOT NULL,
  PRIMARY KEY(run_id, batch_id, left_staging_candidate_id,
              right_staging_candidate_id),
  CHECK(left_staging_candidate_id <> right_staging_candidate_id)
);
CREATE TABLE audit_activation_maps(
  staging_candidate_id TEXT PRIMARY KEY
    REFERENCES audit_batch_staging(staging_candidate_id),
  legacy_candidate_id TEXT NOT NULL UNIQUE,
  source_sequence INTEGER NOT NULL UNIQUE CHECK(source_sequence >= 1),
  raw_artifact_sha TEXT NOT NULL CHECK(length(raw_artifact_sha) = 64),
  pair_plan_sha TEXT NOT NULL CHECK(length(pair_plan_sha) = 64),
  pair_result_sha TEXT NOT NULL CHECK(length(pair_result_sha) = 64),
  activation_receipt_sha TEXT NOT NULL CHECK(length(activation_receipt_sha) = 64),
  activated_at TEXT NOT NULL,
  FOREIGN KEY(legacy_candidate_id, source_sequence)
    REFERENCES candidates(candidate_id, source_sequence)
);
CREATE TABLE audit_direction_contracts(
  run_id TEXT NOT NULL REFERENCES audit_run_manifests(run_id),
  batch_id TEXT NOT NULL,
  direction_id TEXT NOT NULL,
  contract_sha TEXT NOT NULL CHECK(length(contract_sha) = 64),
  validator_version TEXT NOT NULL,
  artifact_sha TEXT NOT NULL CHECK(length(artifact_sha) = 64),
  created_at TEXT NOT NULL,
  PRIMARY KEY(run_id, batch_id, direction_id, contract_sha,
              validator_version, artifact_sha)
);
CREATE TABLE audit_direction_checks(
  run_id TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  direction_id TEXT NOT NULL,
  contract_sha TEXT NOT NULL,
  validator_version TEXT NOT NULL,
  artifact_sha TEXT NOT NULL,
  staging_candidate_id TEXT NOT NULL
    REFERENCES audit_batch_staging(staging_candidate_id),
  semantic_relation TEXT NOT NULL CHECK(semantic_relation IN (
    'blocking_duplicate','substantive_overlap','related_only','distinct','uncertain'
  )),
  lineage_relation TEXT NOT NULL CHECK(lineage_relation IN (
    'same_revision','evolved_from','recheck_of','supersedes','none'
  )),
  evidence_sha TEXT NOT NULL CHECK(length(evidence_sha) = 64),
  checked_at TEXT NOT NULL,
  PRIMARY KEY(run_id, batch_id, direction_id, contract_sha,
              validator_version, artifact_sha, staging_candidate_id),
  FOREIGN KEY(run_id, batch_id, direction_id, contract_sha,
              validator_version, artifact_sha)
    REFERENCES audit_direction_contracts(
      run_id, batch_id, direction_id, contract_sha, validator_version, artifact_sha
    )
);
""" + _immutable_guards(
    "audit_run_manifests",
    "audit_snapshots",
    "audit_batch_staging",
    "audit_batch_pairs",
    "audit_activation_maps",
    "audit_direction_contracts",
    "audit_direction_checks",
)


_CAS_SQL = """
CREATE TABLE audit_cas_objects(
  object_id TEXT PRIMARY KEY CHECK(length(object_id) = 64),
  raw_sha256 TEXT NOT NULL UNIQUE CHECK(length(raw_sha256) = 64),
  compressed_sha256 TEXT NOT NULL CHECK(length(compressed_sha256) = 64),
  codec TEXT NOT NULL CHECK(codec = 'zlib-v1'),
  raw_length INTEGER NOT NULL CHECK(raw_length >= 0),
  compressed_length INTEGER NOT NULL CHECK(compressed_length >= 0),
  retention_profile TEXT NOT NULL,
  relative_path TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  integrity_state TEXT NOT NULL
    CHECK(integrity_state IN ('verified','missing','corrupt'))
);
CREATE TABLE audit_cas_pins(
  object_id TEXT NOT NULL REFERENCES audit_cas_objects(object_id),
  pin_reason TEXT NOT NULL,
  pinned_at TEXT NOT NULL,
  PRIMARY KEY(object_id, pin_reason)
);
CREATE TABLE audit_cas_tombstones(
  object_id TEXT PRIMARY KEY REFERENCES audit_cas_objects(object_id),
  tombstone_sha256 TEXT NOT NULL UNIQUE CHECK(length(tombstone_sha256) = 64),
  reason TEXT NOT NULL,
  marked_at TEXT NOT NULL,
  delete_after TEXT NOT NULL
);
""" + _immutable_guards(
    "audit_cas_objects", "audit_cas_pins", "audit_cas_tombstones"
)


_EXECUTION_SQL = """
CREATE TABLE audit_provider_profiles(
  profile_hash TEXT PRIMARY KEY CHECK(length(profile_hash) = 64),
  provider TEXT NOT NULL,
  profile_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE audit_capacity_profiles(
  capacity_profile_id TEXT PRIMARY KEY,
  profile_sha256 TEXT NOT NULL UNIQUE CHECK(length(profile_sha256) = 64),
  profile_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE audit_shard_plans(
  shard_plan_sha TEXT PRIMARY KEY CHECK(length(shard_plan_sha) = 64),
  run_id TEXT NOT NULL REFERENCES audit_run_manifests(run_id),
  snapshot_id TEXT NOT NULL REFERENCES audit_snapshots(snapshot_id),
  expected_asset_ids_hash TEXT NOT NULL CHECK(length(expected_asset_ids_hash) = 64),
  plan_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE audit_logical_tasks(
  task_hash TEXT PRIMARY KEY CHECK(length(task_hash) = 64),
  run_id TEXT NOT NULL REFERENCES audit_run_manifests(run_id),
  stage TEXT NOT NULL,
  staging_candidate_id TEXT NOT NULL,
  input_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN (
    'planned','claimed','settling','settled','superseded','exhausted'
  )),
  fence INTEGER NOT NULL CHECK(fence >= 0),
  claim_token TEXT,
  lease_until TEXT,
  created_at TEXT NOT NULL,
  CHECK(
    (state IN ('claimed','settling') AND claim_token IS NOT NULL
      AND lease_until IS NOT NULL)
    OR
    (state IN ('planned','settled','superseded','exhausted')
      AND claim_token IS NULL AND lease_until IS NULL)
  )
);
CREATE TABLE audit_task_attempts(
  attempt_id TEXT PRIMARY KEY CHECK(length(attempt_id) = 64),
  task_hash TEXT NOT NULL REFERENCES audit_logical_tasks(task_hash),
  ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
  provenance_json TEXT NOT NULL,
  request_cas_object_id TEXT NOT NULL REFERENCES audit_cas_objects(object_id),
  output_cas_object_id TEXT REFERENCES audit_cas_objects(object_id),
  state TEXT NOT NULL CHECK(state IN ('started','completed','failed','cancelled')),
  created_at TEXT NOT NULL,
  UNIQUE(task_hash, ordinal)
);
CREATE TABLE audit_task_settlements(
  task_hash TEXT PRIMARY KEY REFERENCES audit_logical_tasks(task_hash),
  settlement_sha256 TEXT NOT NULL UNIQUE CHECK(length(settlement_sha256) = 64),
  semantic_relation TEXT NOT NULL CHECK(semantic_relation IN (
    'blocking_duplicate','substantive_overlap','related_only','distinct','uncertain'
  )),
  lineage_relation TEXT NOT NULL CHECK(lineage_relation IN (
    'same_revision','evolved_from','recheck_of','supersedes','none'
  )),
  valid_attempt_ids_json TEXT NOT NULL,
  settled_at TEXT NOT NULL
);
CREATE TABLE audit_budget_events(
  event_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES audit_run_manifests(run_id),
  intent TEXT NOT NULL,
  round_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN ('reserved','settled','released')),
  counters_json TEXT NOT NULL,
  event_sha256 TEXT NOT NULL UNIQUE CHECK(length(event_sha256) = 64),
  created_at TEXT NOT NULL
);
""" + _immutable_guards(
    "audit_provider_profiles",
    "audit_capacity_profiles",
    "audit_shard_plans",
    "audit_task_attempts",
    "audit_task_settlements",
    "audit_budget_events",
)


_RECEIPT_SQL = """
CREATE TABLE audit_receipts(
  manifest_schema_version TEXT NOT NULL
    CHECK(manifest_schema_version = 'history-audit-manifest-v2'),
  canonical_codec_version TEXT NOT NULL
    CHECK(canonical_codec_version = 'history-canonical-json-v2'),
  run_id TEXT NOT NULL REFERENCES audit_run_manifests(run_id),
  plan_hash TEXT NOT NULL CHECK(length(plan_hash) = 64),
  candidate_hash TEXT NOT NULL CHECK(length(candidate_hash) = 64),
  snapshot_id TEXT NOT NULL REFERENCES audit_snapshots(snapshot_id),
  snapshot_hash TEXT NOT NULL CHECK(length(snapshot_hash) = 64),
  history_as_of_watermark INTEGER NOT NULL CHECK(history_as_of_watermark >= 0),
  current_batch_id_namespace TEXT NOT NULL
    CHECK(current_batch_id_namespace = 'history-v2-staging-v1'),
  current_batch_ids_hash TEXT NOT NULL CHECK(length(current_batch_ids_hash) = 64),
  exclusion_policy_sha TEXT NOT NULL CHECK(length(exclusion_policy_sha) = 64),
  expected_asset_ids_hash TEXT NOT NULL CHECK(length(expected_asset_ids_hash) = 64),
  observed_asset_ids_hash TEXT NOT NULL CHECK(length(observed_asset_ids_hash) = 64),
  missing_ids TEXT NOT NULL,
  duplicate_ids TEXT NOT NULL,
  extra_ids TEXT NOT NULL,
  invalid_schema INTEGER NOT NULL CHECK(invalid_schema IN (0,1)),
  invalid_anchor INTEGER NOT NULL CHECK(invalid_anchor IN (0,1)),
  truncated INTEGER NOT NULL CHECK(truncated IN (0,1)),
  provider_pools_ordered TEXT NOT NULL,
  provider_capability_profile_hashes TEXT NOT NULL,
  capacity_profile_id TEXT NOT NULL,
  semantic_policy_profile_id TEXT NOT NULL,
  risk_policy_version TEXT NOT NULL,
  matched_router_rule_ids TEXT NOT NULL,
  settlement_policy_sha TEXT NOT NULL CHECK(length(settlement_policy_sha) = 64),
  shard_plan_sha TEXT NOT NULL CHECK(length(shard_plan_sha) = 64),
  logical_task_hashes TEXT NOT NULL,
  attempt_manifest_hashes TEXT NOT NULL,
  raw_request_output_cas_hashes TEXT NOT NULL,
  minimum_receipt_sha TEXT PRIMARY KEY CHECK(length(minimum_receipt_sha) = 64),
  coverage_complete INTEGER NOT NULL CHECK(coverage_complete IN (0,1)),
  adjudication_complete INTEGER NOT NULL CHECK(adjudication_complete IN (0,1)),
  semantic_policy_qualified INTEGER NOT NULL
    CHECK(semantic_policy_qualified IN (0,1)),
  no_match_basis TEXT,
  final_status TEXT NOT NULL CHECK(final_status IN (
    'overlap_found','complete_no_match','uncertain','partial','invalid'
  )),
  stage_reason_code TEXT NOT NULL,
  evidence_anchors TEXT NOT NULL,
  CHECK(
    (final_status = 'complete_no_match'
      AND no_match_basis IN ('l1_calibrated','l2_exhaustive'))
    OR (final_status <> 'complete_no_match' AND no_match_basis IS NULL)
  )
);
CREATE TABLE audit_legacy_receipts(
  legacy_receipt_id TEXT NOT NULL,
  legacy_json_sha256 TEXT NOT NULL CHECK(length(legacy_json_sha256) = 64),
  pack_publication_id TEXT NOT NULL,
  legacy_status_token TEXT NOT NULL,
  legacy_relation_tokens_json TEXT NOT NULL,
  migration_id TEXT NOT NULL,
  compatibility_state TEXT NOT NULL
    CHECK(compatibility_state IN ('unqualified','ambiguous')),
  quarantined_at TEXT NOT NULL,
  PRIMARY KEY(legacy_receipt_id, migration_id)
);
""" + _immutable_guards("audit_receipts", "audit_legacy_receipts")


_METADATA_SQL = """
CREATE TABLE audit_metadata_profiles(
  profile_id TEXT PRIMARY KEY,
  profile_sha256 TEXT NOT NULL UNIQUE CHECK(length(profile_sha256) = 64),
  profile_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE audit_annotations(
  annotation_id TEXT PRIMARY KEY,
  profile_id TEXT NOT NULL REFERENCES audit_metadata_profiles(profile_id),
  candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  annotation_sha256 TEXT NOT NULL UNIQUE CHECK(length(annotation_sha256) = 64),
  annotation_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE audit_metadata_outbox(
  outbox_id TEXT PRIMARY KEY,
  profile_id TEXT NOT NULL REFERENCES audit_metadata_profiles(profile_id),
  candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  state TEXT NOT NULL CHECK(state IN ('pending','claimed','done','failed')),
  fence INTEGER NOT NULL CHECK(fence >= 0),
  claim_token TEXT,
  lease_until TEXT,
  created_at TEXT NOT NULL,
  CHECK(
    (state = 'claimed' AND claim_token IS NOT NULL AND lease_until IS NOT NULL)
    OR
    (state IN ('pending','done','failed')
      AND claim_token IS NULL AND lease_until IS NULL)
  )
);
""" + _immutable_guards("audit_metadata_profiles", "audit_annotations")


_SEMANTIC_SQL = """
CREATE TABLE audit_semantic_qualifications(
  qualification_id TEXT PRIMARY KEY,
  semantic_policy_profile_id TEXT NOT NULL,
  qualification_sha256 TEXT NOT NULL UNIQUE CHECK(length(qualification_sha256) = 64),
  corpus_snapshot_hash TEXT NOT NULL CHECK(length(corpus_snapshot_hash) = 64),
  provider_capacity_hashes_json TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  qualification_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
""" + _immutable_guards("audit_semantic_qualifications")


_INTEGRITY_GUARDS_SQL = """
CREATE TABLE audit_integrity_guard_probe(
  value INTEGER NOT NULL CHECK(value = 0)
);
INSERT INTO audit_integrity_guard_probe(value)
SELECT 1 FROM audit_receipts receipt
WHERE (
    receipt.final_status = 'complete_no_match'
    AND NOT (
      receipt.coverage_complete = 1
      AND receipt.adjudication_complete = 1
      AND receipt.semantic_policy_qualified = 1
    )
  )
  OR NOT EXISTS (
    SELECT 1 FROM audit_run_manifests run
    WHERE run.run_id = receipt.run_id AND run.plan_hash = receipt.plan_hash
  )
  OR NOT EXISTS (
    SELECT 1 FROM audit_snapshots snapshot
    WHERE snapshot.snapshot_id = receipt.snapshot_id
      AND snapshot.snapshot_hash = receipt.snapshot_hash
      AND snapshot.history_as_of_watermark = receipt.history_as_of_watermark
      AND snapshot.current_batch_id_namespace
          = receipt.current_batch_id_namespace
      AND snapshot.current_batch_ids_hash = receipt.current_batch_ids_hash
      AND snapshot.exclusion_policy_sha = receipt.exclusion_policy_sha
      AND snapshot.expected_asset_ids_hash = receipt.expected_asset_ids_hash
  );
INSERT INTO audit_integrity_guard_probe(value)
SELECT 1 FROM audit_activation_maps activation
WHERE NOT EXISTS (
    SELECT 1 FROM audit_batch_staging staging
    WHERE staging.staging_candidate_id = activation.staging_candidate_id
      AND staging.raw_artifact_sha = activation.raw_artifact_sha
  )
  OR NOT EXISTS (
    SELECT 1
    FROM audit_batch_staging staging
    JOIN audit_batch_pairs pair
      ON pair.run_id = staging.run_id AND pair.batch_id = staging.batch_id
     AND (
       pair.left_staging_candidate_id = staging.staging_candidate_id
       OR pair.right_staging_candidate_id = staging.staging_candidate_id
     )
    WHERE staging.staging_candidate_id = activation.staging_candidate_id
      AND pair.pair_plan_sha = activation.pair_plan_sha
      AND pair.pair_result_sha = activation.pair_result_sha
  );
INSERT INTO audit_integrity_guard_probe(value)
SELECT 1 FROM audit_direction_checks direction_check
WHERE NOT EXISTS (
  SELECT 1 FROM audit_batch_staging staging
  WHERE staging.staging_candidate_id = direction_check.staging_candidate_id
    AND staging.run_id = direction_check.run_id
    AND staging.batch_id = direction_check.batch_id
);
DROP TABLE audit_integrity_guard_probe;
CREATE TRIGGER audit_receipts_release_and_identity_guard
BEFORE INSERT ON audit_receipts
BEGIN
  SELECT CASE WHEN NEW.final_status = 'complete_no_match' AND NOT (
    NEW.coverage_complete = 1
    AND NEW.adjudication_complete = 1
    AND NEW.semantic_policy_qualified = 1
  ) THEN RAISE(ABORT, 'complete_no_match release gates are incomplete') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_run_manifests r
    WHERE r.run_id = NEW.run_id AND r.plan_hash = NEW.plan_hash
  ) THEN RAISE(ABORT, 'receipt run and plan identity mismatch') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_snapshots s
    WHERE s.snapshot_id = NEW.snapshot_id
      AND s.snapshot_hash = NEW.snapshot_hash
      AND s.history_as_of_watermark = NEW.history_as_of_watermark
      AND s.current_batch_id_namespace = NEW.current_batch_id_namespace
      AND s.current_batch_ids_hash = NEW.current_batch_ids_hash
      AND s.exclusion_policy_sha = NEW.exclusion_policy_sha
      AND s.expected_asset_ids_hash = NEW.expected_asset_ids_hash
  ) THEN RAISE(ABORT, 'receipt frozen snapshot identity mismatch') END;
END;
CREATE TRIGGER audit_activation_maps_evidence_guard
BEFORE INSERT ON audit_activation_maps
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_batch_staging s
    WHERE s.staging_candidate_id = NEW.staging_candidate_id
      AND s.raw_artifact_sha = NEW.raw_artifact_sha
  ) THEN RAISE(ABORT, 'activation staging artifact mismatch') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_batch_staging s
    JOIN audit_batch_pairs p
      ON p.run_id = s.run_id AND p.batch_id = s.batch_id
     AND (
       p.left_staging_candidate_id = s.staging_candidate_id
       OR p.right_staging_candidate_id = s.staging_candidate_id
     )
    WHERE s.staging_candidate_id = NEW.staging_candidate_id
      AND p.pair_plan_sha = NEW.pair_plan_sha
      AND p.pair_result_sha = NEW.pair_result_sha
  ) THEN RAISE(ABORT, 'activation pair evidence mismatch') END;
END;
CREATE TRIGGER audit_direction_checks_staging_owner_guard
BEFORE INSERT ON audit_direction_checks
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_batch_staging s
    WHERE s.staging_candidate_id = NEW.staging_candidate_id
      AND s.run_id = NEW.run_id
      AND s.batch_id = NEW.batch_id
  ) THEN RAISE(ABORT, 'direction check staging ownership mismatch') END;
END;
CREATE TRIGGER audit_logical_tasks_fenced_update
BEFORE UPDATE ON audit_logical_tasks
BEGIN
  SELECT CASE WHEN audit_fenced_cas_allowed() <> 1
    THEN RAISE(ABORT, 'logical task update requires fenced CAS') END;
  SELECT CASE WHEN NEW.task_hash <> OLD.task_hash
    OR NEW.run_id <> OLD.run_id
    OR NEW.stage <> OLD.stage
    OR NEW.staging_candidate_id <> OLD.staging_candidate_id
    OR NEW.input_id <> OLD.input_id
    OR NEW.created_at <> OLD.created_at
    THEN RAISE(ABORT, 'logical task identity is immutable') END;
  SELECT CASE WHEN NEW.fence <> OLD.fence + 1
    THEN RAISE(ABORT, 'logical task fence must increase by one') END;
END;
CREATE TRIGGER audit_logical_tasks_no_delete
BEFORE DELETE ON audit_logical_tasks
BEGIN
  SELECT RAISE(ABORT, 'logical task cannot be deleted');
END;
CREATE TRIGGER audit_metadata_outbox_fenced_update
BEFORE UPDATE ON audit_metadata_outbox
BEGIN
  SELECT CASE WHEN audit_fenced_cas_allowed() <> 1
    THEN RAISE(ABORT, 'metadata outbox update requires fenced CAS') END;
  SELECT CASE WHEN NEW.outbox_id <> OLD.outbox_id
    OR NEW.profile_id <> OLD.profile_id
    OR NEW.candidate_id <> OLD.candidate_id
    OR NEW.created_at <> OLD.created_at
    THEN RAISE(ABORT, 'metadata outbox identity is immutable') END;
  SELECT CASE WHEN NEW.fence <> OLD.fence + 1
    THEN RAISE(ABORT, 'metadata outbox fence must increase by one') END;
END;
CREATE TRIGGER audit_metadata_outbox_no_delete
BEFORE DELETE ON audit_metadata_outbox
BEGIN
  SELECT RAISE(ABORT, 'metadata outbox cannot be deleted');
END;
"""


MIGRATIONS = (
    Migration("migration-ledger", 1, _LEDGER_SQL),
    Migration("identity", 1, _IDENTITY_SQL),
    Migration("cas-foundation", 1, _CAS_SQL),
    Migration("execution", 1, _EXECUTION_SQL),
    Migration("receipts", 1, _RECEIPT_SQL),
    Migration("metadata", 1, _METADATA_SQL),
    Migration("semantic-qualification", 1, _SEMANTIC_SQL),
    Migration("integrity-guards", 1, _INTEGRITY_GUARDS_SQL),
)


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _execute_sql_script(conn, source):
    pending = ""
    for line in source.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            pending = ""
            if statement:
                conn.execute(statement)
    if pending.strip():
        raise sqlite3.OperationalError("incomplete migration statement")


def _apply_migration(conn, migration):
    try:
        conn.execute("BEGIN IMMEDIATE")
        ledger_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='audit_schema_migrations'"
        ).fetchone()
        if ledger_exists is not None:
            applied = conn.execute(
                "SELECT migration_sha256 FROM audit_schema_migrations "
                "WHERE component=? AND version=?",
                (migration.component, migration.version),
            ).fetchone()
            if applied is not None:
                if applied[0] != migration.sha256:
                    raise AuditMigrationError(
                        f"migration SHA drift: {migration.component} v{migration.version}"
                    )
                conn.execute("COMMIT")
                return
        _execute_sql_script(conn, migration.sql)
        conn.execute(
            "INSERT INTO audit_schema_migrations(" 
            "component, version, migration_sha256, applied_at) VALUES(?, ?, ?, ?)",
            (
                migration.component,
                migration.version,
                migration.sha256,
                _utc_now(),
            ),
        )
        conn.execute("COMMIT")
    except Exception as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if isinstance(exc, AuditMigrationError):
            raise
        raise AuditMigrationError(
            f"migration failed: {migration.component} v{migration.version}"
        ) from exc


def init_schema(conn):
    """Apply every v2 component migration without invoking v1 initialization."""
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3 connection")
    if conn.in_transaction:
        raise AuditMigrationError("v2 migration requires an idle connection")
    guard = {"active": False}
    _FENCE_GUARDS[id(conn)] = guard
    conn.create_function(
        "audit_fenced_cas_allowed", 0, lambda: 1 if guard["active"] else 0
    )
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA recursive_triggers = ON")
    for migration in MIGRATIONS:
        _apply_migration(conn, migration)


def _legacy_relation_tokens(receipt_json):
    try:
        value = json.loads(receipt_json)
    except (TypeError, ValueError):
        return [], True
    if not isinstance(value, dict) or not isinstance(value.get("relations", []), list):
        return [], True
    tokens = []
    ambiguous = False
    for relation in value.get("relations", []):
        if not isinstance(relation, dict) or not isinstance(
            relation.get("relation"), str
        ):
            ambiguous = True
            continue
        token = relation["relation"]
        tokens.append(token)
        if token not in _KNOWN_LEGACY_RELATIONS:
            ambiguous = True
    return tokens, ambiguous


def quarantine_legacy_receipts(conn):
    """Copy exact legacy receipt identities into compatibility-only rows."""
    if conn.in_transaction:
        raise AuditMigrationError("legacy quarantine requires an idle connection")
    inserted = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        source_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='history_receipts'"
        ).fetchone()
        if source_exists is None:
            conn.execute("COMMIT")
            return 0
        for row in conn.execute(
            "SELECT receipt_id, receipt_json, pack_publication_id, status "
            "FROM history_receipts ORDER BY receipt_id"
        ):
            receipt_id, receipt_json, publication_id, status = tuple(row)
            relations, malformed = _legacy_relation_tokens(receipt_json)
            state = "unqualified"
            if status not in _KNOWN_LEGACY_STATUSES or malformed:
                state = "ambiguous"
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO audit_legacy_receipts(
                  legacy_receipt_id, legacy_json_sha256, pack_publication_id,
                  legacy_status_token, legacy_relation_tokens_json,
                  migration_id, compatibility_state, quarantined_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    hashlib.sha256(receipt_json.encode("utf-8")).hexdigest(),
                    publication_id,
                    status,
                    json.dumps(
                        relations,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ),
                    MIGRATION_ID,
                    state,
                    _utc_now(),
                ),
            )
            inserted += cursor.rowcount
        conn.execute("COMMIT")
        return inserted
    except Exception as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if isinstance(exc, AuditMigrationError):
            raise
        raise AuditMigrationError("legacy receipt quarantine failed") from exc


def compare_and_set_logical_task(
    conn,
    task_hash,
    *,
    expected_state,
    expected_fence,
    new_state,
    new_fence,
    claim_token=None,
    lease_until=None,
):
    """Advance one logical task only when state and fence both match."""
    if (
        type(expected_fence) is not int
        or expected_fence < 0
        or type(new_fence) is not int
        or new_fence != expected_fence + 1
    ):
        raise ValueError("logical task fence must increase by exactly one")
    guard = _FENCE_GUARDS.get(id(conn))
    if guard is None:
        raise AuditMigrationError("fenced CAS is not initialized for connection")
    guard["active"] = True
    try:
        cursor = conn.execute(
            """
            UPDATE audit_logical_tasks
            SET state=?, fence=?, claim_token=?, lease_until=?
            WHERE task_hash=? AND state=? AND fence=?
            """,
            (
                new_state,
                new_fence,
                claim_token,
                lease_until,
                task_hash,
                expected_state,
                expected_fence,
            ),
        )
    finally:
        guard["active"] = False
    if cursor.rowcount != 1:
        raise StaleFence("logical task state or fence is stale")
    return True


def compare_and_set_metadata_outbox(
    conn,
    outbox_id,
    *,
    expected_state,
    expected_fence,
    new_state,
    new_fence,
    claim_token=None,
    lease_until=None,
):
    """Advance one metadata outbox row with the same fenced CAS contract."""
    if (
        type(expected_fence) is not int
        or expected_fence < 0
        or type(new_fence) is not int
        or new_fence != expected_fence + 1
    ):
        raise ValueError("metadata outbox fence must increase by exactly one")
    guard = _FENCE_GUARDS.get(id(conn))
    if guard is None:
        raise AuditMigrationError("fenced CAS is not initialized for connection")
    guard["active"] = True
    try:
        cursor = conn.execute(
            """
            UPDATE audit_metadata_outbox
            SET state=?, fence=?, claim_token=?, lease_until=?
            WHERE outbox_id=? AND state=? AND fence=?
            """,
            (
                new_state,
                new_fence,
                claim_token,
                lease_until,
                outbox_id,
                expected_state,
                expected_fence,
            ),
        )
    finally:
        guard["active"] = False
    if cursor.rowcount != 1:
        raise StaleFence("metadata outbox state or fence is stale")
    return True
