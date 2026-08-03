#!/usr/bin/env python3
"""Component migrations and fenced state changes for history audit v2."""

import dataclasses
import datetime
import hashlib
import json
import re
import sqlite3
import contextlib

try:
    from lib import history_audit_plan
    from lib import history_contract_v2
except ImportError:
    import history_audit_plan
    import history_contract_v2


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
_L2_TASK_INSERT_GUARDS = {}
_L2_TERMINAL_TRANSITION_GUARDS = {}
_SEMANTIC_RELEASE_GUARDS = {}
_SEMANTIC_EVALUATION_GUARDS = {}
_COST_FACT_GUARDS = {}
_MIGRATION_LEDGER_GUARDS = {}
_EXPECTED_MANAGED_SCHEMA = {}
_RELEASE_RECEIPT_FIELDS = (
    "manifest_schema_version", "canonical_codec_version", "run_id", "plan_hash",
    "candidate_hash", "snapshot_id", "snapshot_hash", "history_as_of_watermark",
    "current_batch_id_namespace", "current_batch_ids_hash", "exclusion_policy_sha",
    "expected_asset_ids_hash", "observed_asset_ids_hash", "missing_ids",
    "duplicate_ids", "extra_ids", "invalid_schema", "invalid_anchor", "truncated",
    "provider_pools_ordered", "provider_capability_profile_hashes",
    "capacity_profile_id", "semantic_policy_profile_id", "risk_policy_version",
    "matched_router_rule_ids", "settlement_policy_sha", "shard_plan_sha",
    "logical_task_hashes", "attempt_manifest_hashes",
    "raw_request_output_cas_hashes", "minimum_receipt_sha", "coverage_complete",
    "adjudication_complete", "semantic_policy_qualified", "no_match_basis",
    "final_status", "stage_reason_code", "evidence_anchors",
)
_RELEASE_JSON_FIELDS = frozenset(
    {
        "missing_ids", "duplicate_ids", "extra_ids", "provider_pools_ordered",
        "provider_capability_profile_hashes", "matched_router_rule_ids",
        "logical_task_hashes", "attempt_manifest_hashes",
        "raw_request_output_cas_hashes", "evidence_anchors",
    }
)
_RELEASE_BOOLEAN_FIELDS = frozenset(
    {
        "invalid_schema", "invalid_anchor", "truncated", "coverage_complete",
        "adjudication_complete", "semantic_policy_qualified",
    }
)


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


_METADATA_SHADOW_SQL = """
CREATE TABLE audit_metadata_profiles_v2(
  profile_id TEXT PRIMARY KEY,
  profile_key TEXT NOT NULL,
  profile_version TEXT NOT NULL,
  profile_sha256 TEXT NOT NULL UNIQUE CHECK(length(profile_sha256) = 64),
  profile_json TEXT NOT NULL,
  producer_kind TEXT NOT NULL,
  producer_id TEXT NOT NULL,
  producer_version TEXT NOT NULL,
  prompt_sha256 TEXT NOT NULL CHECK(length(prompt_sha256) = 64),
  synopsis_max_chars INTEGER NOT NULL CHECK(synopsis_max_chars >= 0),
  supersedes_profile_id TEXT REFERENCES audit_metadata_profiles_v2(profile_id),
  created_at TEXT NOT NULL,
  UNIQUE(profile_key, profile_version)
);
CREATE TABLE audit_metadata_profile_events_v2(
  event_sequence INTEGER PRIMARY KEY,
  event_id TEXT NOT NULL UNIQUE CHECK(length(event_id) = 64),
  profile_id TEXT NOT NULL REFERENCES audit_metadata_profiles_v2(profile_id),
  state TEXT NOT NULL CHECK(state IN ('current','stale')),
  reason TEXT NOT NULL,
  replaced_by_profile_id TEXT REFERENCES audit_metadata_profiles_v2(profile_id),
  created_at TEXT NOT NULL
);
CREATE TABLE audit_metadata_outbox_v2(
  outbox_id TEXT PRIMARY KEY CHECK(length(outbox_id) = 64),
  profile_id TEXT NOT NULL REFERENCES audit_metadata_profiles_v2(profile_id),
  profile_sha256 TEXT NOT NULL CHECK(length(profile_sha256) = 64),
  candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  source_content_sha TEXT NOT NULL CHECK(length(source_content_sha) = 64),
  source_sequence INTEGER NOT NULL CHECK(source_sequence >= 1),
  producer_kind TEXT NOT NULL,
  producer_id TEXT NOT NULL,
  producer_version TEXT NOT NULL,
  prompt_sha256 TEXT NOT NULL CHECK(length(prompt_sha256) = 64),
  state TEXT NOT NULL CHECK(state IN ('pending','claimed','done','failed')),
  fence INTEGER NOT NULL CHECK(fence >= 0),
  claim_token TEXT,
  lease_until TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(candidate_id, source_content_sha, profile_id),
  FOREIGN KEY(candidate_id, source_sequence)
    REFERENCES candidates(candidate_id, source_sequence),
  CHECK(
    (state = 'claimed' AND claim_token IS NOT NULL AND lease_until IS NOT NULL)
    OR
    (state IN ('pending','done','failed')
      AND claim_token IS NULL AND lease_until IS NULL)
  )
);
CREATE TABLE audit_annotation_versions_v2(
  annotation_id TEXT PRIMARY KEY CHECK(length(annotation_id) = 64),
  outbox_id TEXT NOT NULL REFERENCES audit_metadata_outbox_v2(outbox_id),
  profile_id TEXT NOT NULL REFERENCES audit_metadata_profiles_v2(profile_id),
  profile_sha256 TEXT NOT NULL CHECK(length(profile_sha256) = 64),
  candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  source_content_sha TEXT NOT NULL CHECK(length(source_content_sha) = 64),
  source_sequence INTEGER NOT NULL CHECK(source_sequence >= 1),
  family TEXT NOT NULL CHECK(family IN (
    'synopsis','concept','free_tag','cluster','direction'
  )),
  value_json TEXT NOT NULL,
  value_sha256 TEXT NOT NULL CHECK(length(value_sha256) = 64),
  confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
  direction_identity_json TEXT,
  producer_kind TEXT NOT NULL,
  producer_id TEXT NOT NULL,
  producer_version TEXT NOT NULL,
  prompt_sha256 TEXT NOT NULL CHECK(length(prompt_sha256) = 64),
  created_at TEXT NOT NULL,
  stale_state TEXT NOT NULL CHECK(stale_state IN ('current','stale')),
  FOREIGN KEY(candidate_id, source_sequence)
    REFERENCES candidates(candidate_id, source_sequence),
  CHECK(
    (family = 'direction' AND direction_identity_json IS NOT NULL)
    OR (family <> 'direction' AND direction_identity_json IS NULL)
  )
);
""" + _immutable_guards(
    "audit_metadata_profiles_v2",
    "audit_metadata_profile_events_v2",
    "audit_annotation_versions_v2",
) + """
CREATE TRIGGER audit_metadata_outbox_v2_insert_guard
BEFORE INSERT ON audit_metadata_outbox_v2
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM candidates candidate
    WHERE candidate.candidate_id = NEW.candidate_id
      AND candidate.source_sequence = NEW.source_sequence
      AND candidate.raw_sha256 = NEW.source_content_sha
  ) THEN RAISE(ABORT, 'metadata outbox source identity mismatch') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_metadata_profiles_v2 profile
    WHERE profile.profile_id = NEW.profile_id
      AND profile.profile_sha256 = NEW.profile_sha256
      AND profile.producer_kind = NEW.producer_kind
      AND profile.producer_id = NEW.producer_id
      AND profile.producer_version = NEW.producer_version
      AND profile.prompt_sha256 = NEW.prompt_sha256
  ) THEN RAISE(ABORT, 'metadata outbox profile identity mismatch') END;
END;
CREATE TRIGGER audit_metadata_outbox_v2_fenced_update
BEFORE UPDATE ON audit_metadata_outbox_v2
BEGIN
  SELECT CASE WHEN audit_fenced_cas_allowed() <> 1
    THEN RAISE(ABORT, 'metadata shadow outbox update requires fenced CAS') END;
  SELECT CASE WHEN NEW.outbox_id <> OLD.outbox_id
    OR NEW.profile_id <> OLD.profile_id
    OR NEW.profile_sha256 <> OLD.profile_sha256
    OR NEW.candidate_id <> OLD.candidate_id
    OR NEW.source_content_sha <> OLD.source_content_sha
    OR NEW.source_sequence <> OLD.source_sequence
    OR NEW.producer_kind <> OLD.producer_kind
    OR NEW.producer_id <> OLD.producer_id
    OR NEW.producer_version <> OLD.producer_version
    OR NEW.prompt_sha256 <> OLD.prompt_sha256
    OR NEW.created_at <> OLD.created_at
    THEN RAISE(ABORT, 'metadata shadow outbox identity is immutable') END;
  SELECT CASE WHEN NEW.fence <> OLD.fence + 1
    THEN RAISE(ABORT, 'metadata shadow outbox fence must increase by one') END;
END;
CREATE TRIGGER audit_metadata_outbox_v2_no_delete
BEFORE DELETE ON audit_metadata_outbox_v2
BEGIN
  SELECT RAISE(ABORT, 'metadata shadow outbox cannot be deleted');
END;
CREATE TRIGGER audit_annotation_versions_v2_insert_guard
BEFORE INSERT ON audit_annotation_versions_v2
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_metadata_outbox_v2 work
    WHERE work.outbox_id = NEW.outbox_id
      AND work.state = 'claimed'
      AND work.profile_id = NEW.profile_id
      AND work.profile_sha256 = NEW.profile_sha256
      AND work.candidate_id = NEW.candidate_id
      AND work.source_content_sha = NEW.source_content_sha
      AND work.source_sequence = NEW.source_sequence
      AND work.producer_kind = NEW.producer_kind
      AND work.producer_id = NEW.producer_id
      AND work.producer_version = NEW.producer_version
      AND work.prompt_sha256 = NEW.prompt_sha256
  ) THEN RAISE(ABORT, 'annotation is not bound to claimed metadata work') END;
END;
"""


_METADATA_SHADOW_LIFECYCLE_SQL = """
CREATE TABLE audit_metadata_annotation_claims_v2(
  annotation_id TEXT PRIMARY KEY
    REFERENCES audit_annotation_versions_v2(annotation_id)
    DEFERRABLE INITIALLY DEFERRED,
  outbox_id TEXT NOT NULL REFERENCES audit_metadata_outbox_v2(outbox_id),
  claim_fence INTEGER NOT NULL CHECK(claim_fence >= 1),
  claim_token TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE audit_metadata_settlements_v2(
  outbox_id TEXT PRIMARY KEY REFERENCES audit_metadata_outbox_v2(outbox_id),
  claim_fence INTEGER NOT NULL CHECK(claim_fence >= 1),
  claim_token TEXT NOT NULL,
  annotation_ids_json TEXT NOT NULL,
  annotation_ids_sha256 TEXT NOT NULL CHECK(length(annotation_ids_sha256) = 64),
  annotation_count INTEGER NOT NULL CHECK(annotation_count >= 0),
  created_at TEXT NOT NULL
);
""" + _immutable_guards(
    "audit_metadata_annotation_claims_v2",
    "audit_metadata_settlements_v2",
) + """
CREATE TABLE audit_metadata_direction_probe(
  value INTEGER NOT NULL CHECK(value = 0)
);
INSERT INTO audit_metadata_direction_probe(value)
SELECT 1 FROM audit_annotation_versions_v2 annotation
WHERE annotation.family = 'direction'
  AND (
    annotation.direction_identity_json IS NULL
    OR json_valid(annotation.direction_identity_json) <> 1
    OR NOT EXISTS (
      SELECT 1
      FROM audit_activation_maps activation
      JOIN audit_direction_checks direction_check
        ON direction_check.staging_candidate_id = activation.staging_candidate_id
      JOIN audit_direction_contracts direction_contract
        ON direction_contract.run_id = direction_check.run_id
       AND direction_contract.batch_id = direction_check.batch_id
       AND direction_contract.direction_id = direction_check.direction_id
       AND direction_contract.contract_sha = direction_check.contract_sha
       AND direction_contract.validator_version = direction_check.validator_version
       AND direction_contract.artifact_sha = direction_check.artifact_sha
      WHERE activation.legacy_candidate_id = annotation.candidate_id
        AND direction_check.run_id = json_extract(
          annotation.direction_identity_json, '$.run_id'
        )
        AND direction_check.batch_id = json_extract(
          annotation.direction_identity_json, '$.batch_id'
        )
        AND direction_check.direction_id = json_extract(
          annotation.direction_identity_json, '$.direction_id'
        )
        AND direction_check.contract_sha = json_extract(
          annotation.direction_identity_json, '$.contract_sha'
        )
        AND direction_check.validator_version = json_extract(
          annotation.direction_identity_json, '$.validator_version'
        )
        AND direction_check.artifact_sha = json_extract(
          annotation.direction_identity_json, '$.artifact_sha'
        )
    )
  );
DROP TABLE audit_metadata_direction_probe;

DROP TRIGGER audit_metadata_outbox_v2_fenced_update;
CREATE TRIGGER audit_metadata_outbox_v2_fenced_update
BEFORE UPDATE ON audit_metadata_outbox_v2
BEGIN
  SELECT CASE WHEN audit_fenced_cas_allowed() <> 1
    THEN RAISE(ABORT, 'metadata shadow outbox update requires fenced CAS') END;
  SELECT CASE WHEN NEW.outbox_id <> OLD.outbox_id
    OR NEW.profile_id <> OLD.profile_id
    OR NEW.profile_sha256 <> OLD.profile_sha256
    OR NEW.candidate_id <> OLD.candidate_id
    OR NEW.source_content_sha <> OLD.source_content_sha
    OR NEW.source_sequence <> OLD.source_sequence
    OR NEW.producer_kind <> OLD.producer_kind
    OR NEW.producer_id <> OLD.producer_id
    OR NEW.producer_version <> OLD.producer_version
    OR NEW.prompt_sha256 <> OLD.prompt_sha256
    OR NEW.created_at <> OLD.created_at
    THEN RAISE(ABORT, 'metadata shadow outbox identity is immutable') END;
  SELECT CASE WHEN NEW.fence <> OLD.fence + 1
    THEN RAISE(ABORT, 'metadata shadow outbox fence must increase by one') END;
  SELECT CASE WHEN NEW.outbox_id <> audit_metadata_outbox_id()
    THEN RAISE(ABORT, 'metadata shadow transition owner mismatch') END;
  SELECT CASE WHEN NOT (
    OLD.state = 'pending'
    AND NEW.state = 'claimed'
    AND audit_metadata_operation() = 'claim'
    AND OLD.claim_token IS NULL
    AND OLD.lease_until IS NULL
    AND NEW.claim_token = audit_metadata_claim_token()
    AND NEW.fence = audit_metadata_claim_fence()
    AND audit_metadata_lease_live(
      NEW.lease_until, audit_metadata_now()
    ) = 1
  ) AND NOT (
    OLD.state = 'claimed'
    AND NEW.state = 'claimed'
    AND audit_metadata_operation() = 'reclaim'
    AND audit_metadata_lease_expired(
      OLD.lease_until, audit_metadata_now()
    ) = 1
    AND NEW.claim_token = audit_metadata_claim_token()
    AND NEW.claim_token <> OLD.claim_token
    AND NEW.fence = audit_metadata_claim_fence()
    AND audit_metadata_lease_live(
      NEW.lease_until, audit_metadata_now()
    ) = 1
  ) AND NOT (
    OLD.state = 'claimed'
    AND NEW.state = 'done'
    AND audit_metadata_operation() = 'publish'
    AND OLD.claim_token = audit_metadata_claim_token()
    AND OLD.fence = audit_metadata_claim_fence()
    AND NEW.claim_token IS NULL
    AND NEW.lease_until IS NULL
    AND audit_metadata_lease_live(
      OLD.lease_until, audit_metadata_now()
    ) = 1
    AND EXISTS (
      SELECT 1 FROM audit_metadata_settlements_v2 settlement
      WHERE settlement.outbox_id = OLD.outbox_id
        AND settlement.claim_fence = OLD.fence
        AND settlement.claim_token = OLD.claim_token
    )
  ) THEN RAISE(ABORT, 'metadata shadow transition is closed') END;
END;

CREATE TRIGGER audit_metadata_annotation_claims_v2_insert_guard
BEFORE INSERT ON audit_metadata_annotation_claims_v2
BEGIN
  SELECT CASE WHEN audit_metadata_operation() <> 'publish'
    OR NEW.outbox_id <> audit_metadata_outbox_id()
    OR NEW.claim_token <> audit_metadata_claim_token()
    OR NEW.claim_fence <> audit_metadata_claim_fence()
    OR NOT EXISTS (
      SELECT 1 FROM audit_metadata_outbox_v2 work
      WHERE work.outbox_id = NEW.outbox_id
        AND work.state = 'claimed'
        AND work.fence = NEW.claim_fence
        AND work.claim_token = NEW.claim_token
        AND audit_metadata_lease_live(
          work.lease_until, audit_metadata_now()
        ) = 1
    )
    THEN RAISE(ABORT, 'annotation claim binding is stale') END;
END;

DROP TRIGGER audit_annotation_versions_v2_insert_guard;
CREATE TRIGGER audit_annotation_versions_v2_insert_guard
BEFORE INSERT ON audit_annotation_versions_v2
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_metadata_outbox_v2 work
    JOIN audit_metadata_annotation_claims_v2 claim
      ON claim.annotation_id = NEW.annotation_id
     AND claim.outbox_id = work.outbox_id
     AND claim.claim_fence = work.fence
     AND claim.claim_token = work.claim_token
    WHERE work.outbox_id = NEW.outbox_id
      AND work.state = 'claimed'
      AND work.profile_id = NEW.profile_id
      AND work.profile_sha256 = NEW.profile_sha256
      AND work.candidate_id = NEW.candidate_id
      AND work.source_content_sha = NEW.source_content_sha
      AND work.source_sequence = NEW.source_sequence
      AND work.producer_kind = NEW.producer_kind
      AND work.producer_id = NEW.producer_id
      AND work.producer_version = NEW.producer_version
      AND work.prompt_sha256 = NEW.prompt_sha256
      AND audit_metadata_operation() = 'publish'
      AND work.outbox_id = audit_metadata_outbox_id()
      AND work.fence = audit_metadata_claim_fence()
      AND work.claim_token = audit_metadata_claim_token()
      AND audit_metadata_lease_live(
        work.lease_until, audit_metadata_now()
      ) = 1
  ) THEN RAISE(ABORT, 'annotation is not bound to current claimed work') END;
  SELECT CASE WHEN NEW.family = 'direction' AND NOT EXISTS (
    SELECT 1
    FROM audit_activation_maps activation
    JOIN audit_direction_checks direction_check
      ON direction_check.staging_candidate_id = activation.staging_candidate_id
    JOIN audit_direction_contracts direction_contract
      ON direction_contract.run_id = direction_check.run_id
     AND direction_contract.batch_id = direction_check.batch_id
     AND direction_contract.direction_id = direction_check.direction_id
     AND direction_contract.contract_sha = direction_check.contract_sha
     AND direction_contract.validator_version = direction_check.validator_version
     AND direction_contract.artifact_sha = direction_check.artifact_sha
    WHERE activation.legacy_candidate_id = NEW.candidate_id
      AND direction_check.run_id = json_extract(
        NEW.direction_identity_json, '$.run_id'
      )
      AND direction_check.batch_id = json_extract(
        NEW.direction_identity_json, '$.batch_id'
      )
      AND direction_check.direction_id = json_extract(
        NEW.direction_identity_json, '$.direction_id'
      )
      AND direction_check.contract_sha = json_extract(
        NEW.direction_identity_json, '$.contract_sha'
      )
      AND direction_check.validator_version = json_extract(
        NEW.direction_identity_json, '$.validator_version'
      )
      AND direction_check.artifact_sha = json_extract(
        NEW.direction_identity_json, '$.artifact_sha'
      )
  ) THEN RAISE(ABORT, 'direction annotation lacks host-owned provenance') END;
END;

CREATE TRIGGER audit_metadata_settlements_v2_insert_guard
BEFORE INSERT ON audit_metadata_settlements_v2
BEGIN
  SELECT CASE WHEN audit_metadata_operation() <> 'publish'
    OR NEW.outbox_id <> audit_metadata_outbox_id()
    OR NEW.claim_token <> audit_metadata_claim_token()
    OR NEW.claim_fence <> audit_metadata_claim_fence()
    OR NEW.annotation_count <> json_array_length(NEW.annotation_ids_json)
    OR NEW.annotation_ids_sha256
      <> audit_metadata_annotation_ids_sha(NEW.annotation_ids_json)
    OR EXISTS (
      SELECT 1 FROM json_each(NEW.annotation_ids_json) item
      GROUP BY item.value HAVING count(*) <> 1
    )
    OR EXISTS (
      SELECT 1 FROM json_each(NEW.annotation_ids_json) item
      WHERE NOT EXISTS (
        SELECT 1
        FROM audit_annotation_versions_v2 annotation
        JOIN audit_metadata_annotation_claims_v2 claim
          ON claim.annotation_id = annotation.annotation_id
        WHERE annotation.annotation_id = item.value
          AND annotation.outbox_id = NEW.outbox_id
          AND claim.outbox_id = NEW.outbox_id
          AND claim.claim_fence = NEW.claim_fence
          AND claim.claim_token = NEW.claim_token
      )
    )
    OR NEW.annotation_count <> (
      SELECT count(*) FROM audit_annotation_versions_v2 annotation
      WHERE annotation.outbox_id = NEW.outbox_id
    )
    OR NOT EXISTS (
      SELECT 1 FROM audit_metadata_outbox_v2 work
      WHERE work.outbox_id = NEW.outbox_id
        AND work.state = 'claimed'
        AND work.fence = NEW.claim_fence
        AND work.claim_token = NEW.claim_token
        AND audit_metadata_lease_live(
          work.lease_until, audit_metadata_now()
        ) = 1
    )
    THEN RAISE(ABORT, 'metadata settlement evidence is invalid') END;
END;
"""


_METADATA_SHADOW_INTEGRITY_SQL = """
CREATE TABLE audit_metadata_integrity_probe(
  value INTEGER NOT NULL CHECK(value = 0)
);
INSERT INTO audit_metadata_integrity_probe(value)
SELECT 1 FROM audit_annotation_versions_v2 annotation
WHERE NOT EXISTS (
  SELECT 1
  FROM audit_metadata_outbox_v2 work
  JOIN audit_metadata_annotation_claims_v2 claim
    ON claim.annotation_id = annotation.annotation_id
   AND claim.outbox_id = work.outbox_id
  JOIN audit_metadata_settlements_v2 settlement
    ON settlement.outbox_id = work.outbox_id
   AND settlement.claim_fence = claim.claim_fence
   AND settlement.claim_token = claim.claim_token
  WHERE work.outbox_id = annotation.outbox_id
    AND work.state = 'done'
    AND settlement.claim_fence = work.fence - 1
    AND work.profile_id = annotation.profile_id
    AND work.profile_sha256 = annotation.profile_sha256
    AND work.candidate_id = annotation.candidate_id
    AND work.source_content_sha = annotation.source_content_sha
    AND work.source_sequence = annotation.source_sequence
    AND work.producer_kind = annotation.producer_kind
    AND work.producer_id = annotation.producer_id
    AND work.producer_version = annotation.producer_version
    AND work.prompt_sha256 = annotation.prompt_sha256
    AND settlement.annotation_count = json_array_length(
      settlement.annotation_ids_json
    )
    AND settlement.annotation_ids_sha256
      = audit_metadata_annotation_ids_sha(settlement.annotation_ids_json)
    AND settlement.annotation_count = (
      SELECT count(*) FROM audit_annotation_versions_v2 member
      WHERE member.outbox_id = work.outbox_id
    )
    AND 1 = (
      SELECT count(*) FROM json_each(settlement.annotation_ids_json) item
      WHERE item.value = annotation.annotation_id
    )
    AND NOT EXISTS (
      SELECT 1 FROM json_each(settlement.annotation_ids_json) item
      WHERE NOT EXISTS (
        SELECT 1
        FROM audit_annotation_versions_v2 declared
        JOIN audit_metadata_annotation_claims_v2 declared_claim
          ON declared_claim.annotation_id = declared.annotation_id
        WHERE declared.annotation_id = item.value
          AND declared.outbox_id = work.outbox_id
          AND declared_claim.outbox_id = work.outbox_id
          AND declared_claim.claim_fence = settlement.claim_fence
          AND declared_claim.claim_token = settlement.claim_token
      )
    )
);
INSERT INTO audit_metadata_integrity_probe(value)
SELECT 1 FROM audit_metadata_outbox_v2 work
WHERE work.state = 'done'
  AND NOT EXISTS (
    SELECT 1 FROM audit_metadata_settlements_v2 settlement
    WHERE settlement.outbox_id = work.outbox_id
      AND settlement.claim_fence = work.fence - 1
      AND settlement.annotation_count = json_array_length(
        settlement.annotation_ids_json
      )
      AND settlement.annotation_ids_sha256
        = audit_metadata_annotation_ids_sha(settlement.annotation_ids_json)
      AND settlement.annotation_count = (
        SELECT count(*) FROM audit_annotation_versions_v2 annotation
        WHERE annotation.outbox_id = work.outbox_id
      )
      AND NOT EXISTS (
        SELECT 1 FROM json_each(settlement.annotation_ids_json) item
        WHERE NOT EXISTS (
          SELECT 1
          FROM audit_annotation_versions_v2 annotation
          JOIN audit_metadata_annotation_claims_v2 claim
            ON claim.annotation_id = annotation.annotation_id
          WHERE annotation.annotation_id = item.value
            AND annotation.outbox_id = work.outbox_id
            AND annotation.profile_id = work.profile_id
            AND annotation.profile_sha256 = work.profile_sha256
            AND annotation.candidate_id = work.candidate_id
            AND annotation.source_content_sha = work.source_content_sha
            AND annotation.source_sequence = work.source_sequence
            AND annotation.producer_kind = work.producer_kind
            AND annotation.producer_id = work.producer_id
            AND annotation.producer_version = work.producer_version
            AND annotation.prompt_sha256 = work.prompt_sha256
            AND claim.outbox_id = work.outbox_id
            AND claim.claim_fence = settlement.claim_fence
            AND claim.claim_token = settlement.claim_token
        )
      )
  );
INSERT INTO audit_metadata_integrity_probe(value)
SELECT 1 FROM audit_metadata_settlements_v2 settlement
WHERE NOT EXISTS (
  SELECT 1 FROM audit_metadata_outbox_v2 work
  WHERE work.outbox_id = settlement.outbox_id AND work.state = 'done'
);
INSERT INTO audit_metadata_integrity_probe(value)
SELECT 1 FROM audit_metadata_annotation_claims_v2 claim
WHERE NOT EXISTS (
  SELECT 1
  FROM audit_annotation_versions_v2 annotation
  JOIN audit_metadata_settlements_v2 settlement
    ON settlement.outbox_id = annotation.outbox_id
   AND settlement.claim_fence = claim.claim_fence
   AND settlement.claim_token = claim.claim_token
  JOIN json_each(settlement.annotation_ids_json) item
    ON item.value = annotation.annotation_id
  WHERE annotation.annotation_id = claim.annotation_id
    AND annotation.outbox_id = claim.outbox_id
);
DROP TABLE audit_metadata_integrity_probe;
"""


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


_SEMANTIC_RELEASE_SQL = """
CREATE TABLE audit_semantic_qualification_facts_v2(
  qualification_id TEXT PRIMARY KEY
    REFERENCES audit_semantic_qualifications(qualification_id),
  no_match_basis TEXT NOT NULL
    CHECK(no_match_basis IN ('l1_calibrated','l2_exhaustive')),
  scope TEXT NOT NULL,
  policy_sha256 TEXT NOT NULL CHECK(length(policy_sha256) = 64),
  qrels_hash TEXT NOT NULL CHECK(length(qrels_hash) = 64),
  evaluation_hash TEXT NOT NULL CHECK(length(evaluation_hash) = 64),
  metric_report_hash TEXT NOT NULL CHECK(length(metric_report_hash) = 64),
  dependency_hashes_json TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  vetoes_json TEXT NOT NULL,
  production_qualified INTEGER NOT NULL CHECK(production_qualified IN (0,1)),
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE audit_semantic_invalidation_facts_v2(
  invalidation_id TEXT PRIMARY KEY CHECK(length(invalidation_id) = 64),
  qualification_id TEXT NOT NULL
    REFERENCES audit_semantic_qualification_facts_v2(qualification_id),
  changed_dependencies_json TEXT NOT NULL,
  impacts_json TEXT NOT NULL,
  invalidated_at TEXT NOT NULL,
  fact_sha256 TEXT NOT NULL UNIQUE CHECK(length(fact_sha256) = 64)
);
""" + _immutable_guards(
    "audit_semantic_qualification_facts_v2",
    "audit_semantic_invalidation_facts_v2",
)


_SEMANTIC_RELEASE_AUTHORIZATION_SQL = """
CREATE TABLE audit_semantic_dependency_head_events_v2(
  head_event_id TEXT PRIMARY KEY CHECK(length(head_event_id) = 64),
  dependency_kind TEXT NOT NULL CHECK(dependency_kind IN (
    'semantic_policy','plan','prompt','schema','ordered_provider_pools',
    'capacity','provider','fault','replay','fts','metadata','embedding','tokenizer'
  )),
  sequence INTEGER NOT NULL CHECK(sequence >= 1),
  dependency_sha256 TEXT NOT NULL CHECK(length(dependency_sha256) = 64),
  created_at TEXT NOT NULL,
  UNIQUE(dependency_kind, sequence)
);
CREATE TABLE audit_semantic_release_authorizations_v2(
  authorization_id TEXT PRIMARY KEY CHECK(length(authorization_id) = 64),
  receipt_id TEXT NOT NULL UNIQUE CHECK(length(receipt_id) = 64),
  receipt_material_sha256 TEXT NOT NULL UNIQUE
    CHECK(length(receipt_material_sha256) = 64),
  qualification_id TEXT NOT NULL
    REFERENCES audit_semantic_qualification_facts_v2(qualification_id),
  qualification_sha256 TEXT NOT NULL CHECK(length(qualification_sha256) = 64),
  semantic_policy_profile_id TEXT NOT NULL,
  scope TEXT NOT NULL,
  no_match_basis TEXT NOT NULL
    CHECK(no_match_basis IN ('l1_calibrated','l2_exhaustive')),
  policy_sha256 TEXT NOT NULL CHECK(length(policy_sha256) = 64),
  corpus_snapshot_hash TEXT NOT NULL CHECK(length(corpus_snapshot_hash) = 64),
  evaluation_hash TEXT NOT NULL CHECK(length(evaluation_hash) = 64),
  dependency_hashes_json TEXT NOT NULL,
  dependency_heads_json TEXT NOT NULL,
  authorized_at TEXT NOT NULL,
  FOREIGN KEY(receipt_id) REFERENCES audit_receipts(minimum_receipt_sha)
    DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE audit_semantic_qualification_head_bindings_v2(
  qualification_id TEXT PRIMARY KEY
    REFERENCES audit_semantic_qualification_facts_v2(qualification_id),
  dependency_head_events_json TEXT NOT NULL,
  bound_at TEXT NOT NULL
);
""" + _immutable_guards(
    "audit_semantic_dependency_head_events_v2",
    "audit_semantic_release_authorizations_v2",
    "audit_semantic_qualification_head_bindings_v2",
) + """
CREATE TRIGGER audit_semantic_dependency_head_events_v2_guard
BEFORE INSERT ON audit_semantic_dependency_head_events_v2
BEGIN
  SELECT CASE WHEN audit_semantic_head_insert_allowed(
    NEW.head_event_id, NEW.dependency_kind, NEW.sequence,
    NEW.dependency_sha256, NEW.created_at
  ) <> 1 THEN RAISE(ABORT, 'semantic dependency head requires host authority') END;
END;
CREATE TRIGGER audit_semantic_qualifications_host_guard
BEFORE INSERT ON audit_semantic_qualifications
BEGIN
  SELECT CASE WHEN audit_semantic_qualification_insert_allowed(
    NEW.qualification_id, NEW.semantic_policy_profile_id,
    NEW.qualification_sha256, NEW.corpus_snapshot_hash,
    NEW.provider_capacity_hashes_json, NEW.expires_at,
    NEW.qualification_json, NEW.created_at
  ) <> 1 THEN RAISE(ABORT, 'semantic qualification requires host authority') END;
END;
CREATE TRIGGER audit_semantic_qualification_facts_v2_host_guard
BEFORE INSERT ON audit_semantic_qualification_facts_v2
BEGIN
  SELECT CASE WHEN audit_semantic_qualification_fact_insert_allowed(
    NEW.qualification_id, NEW.no_match_basis, NEW.scope, NEW.policy_sha256,
    NEW.qrels_hash, NEW.evaluation_hash, NEW.metric_report_hash,
    NEW.dependency_hashes_json, NEW.metrics_json, NEW.vetoes_json,
    NEW.production_qualified, NEW.expires_at, NEW.created_at
  ) <> 1 THEN RAISE(ABORT, 'semantic qualification fact requires host authority') END;
END;
CREATE TRIGGER audit_semantic_qualification_head_bindings_v2_guard
BEFORE INSERT ON audit_semantic_qualification_head_bindings_v2
BEGIN
  SELECT CASE WHEN audit_semantic_qualification_binding_insert_allowed(
    NEW.qualification_id, NEW.dependency_head_events_json, NEW.bound_at
  ) <> 1 THEN RAISE(ABORT, 'qualification head binding requires host authority') END;
END;
CREATE TRIGGER audit_semantic_release_authorizations_v2_guard
BEFORE INSERT ON audit_semantic_release_authorizations_v2
BEGIN
  SELECT CASE WHEN audit_semantic_authorization_insert_allowed(
    NEW.authorization_id, NEW.receipt_id, NEW.receipt_material_sha256,
    NEW.qualification_id, NEW.qualification_sha256,
    NEW.semantic_policy_profile_id, NEW.scope,
    NEW.no_match_basis, NEW.policy_sha256, NEW.corpus_snapshot_hash,
    NEW.evaluation_hash, NEW.dependency_hashes_json,
    NEW.dependency_heads_json, NEW.authorized_at
  ) <> 1 THEN RAISE(ABORT, 'semantic release authorization requires host authority') END;
END;

CREATE TABLE audit_semantic_release_upgrade_probe(
  value INTEGER NOT NULL CHECK(value = 0)
);
INSERT INTO audit_semantic_release_upgrade_probe(value)
SELECT 1 FROM audit_receipts receipt
WHERE receipt.final_status = 'complete_no_match'
  AND NOT EXISTS (
    SELECT 1 FROM audit_semantic_release_authorizations_v2 authorization
    WHERE authorization.receipt_id = receipt.minimum_receipt_sha
  );
DROP TABLE audit_semantic_release_upgrade_probe;

DROP TRIGGER audit_receipts_release_and_identity_guard;
CREATE TRIGGER audit_receipts_release_and_identity_guard
BEFORE INSERT ON audit_receipts
BEGIN
  SELECT CASE WHEN NEW.final_status = 'complete_no_match' AND NOT (
    NEW.coverage_complete = 1
    AND NEW.adjudication_complete = 1
    AND NEW.semantic_policy_qualified = 1
  ) THEN RAISE(ABORT, 'complete_no_match release gates are incomplete') END;
  SELECT CASE WHEN NEW.final_status = 'complete_no_match' AND (
    audit_semantic_receipt_insert_allowed(
      NEW.manifest_schema_version, NEW.canonical_codec_version, NEW.run_id,
      NEW.plan_hash, NEW.candidate_hash, NEW.snapshot_id, NEW.snapshot_hash,
      NEW.history_as_of_watermark, NEW.current_batch_id_namespace,
      NEW.current_batch_ids_hash, NEW.exclusion_policy_sha,
      NEW.expected_asset_ids_hash, NEW.observed_asset_ids_hash,
      NEW.missing_ids, NEW.duplicate_ids, NEW.extra_ids, NEW.invalid_schema,
      NEW.invalid_anchor, NEW.truncated, NEW.provider_pools_ordered,
      NEW.provider_capability_profile_hashes, NEW.capacity_profile_id,
      NEW.semantic_policy_profile_id, NEW.risk_policy_version,
      NEW.matched_router_rule_ids, NEW.settlement_policy_sha,
      NEW.shard_plan_sha, NEW.logical_task_hashes,
      NEW.attempt_manifest_hashes, NEW.raw_request_output_cas_hashes,
      NEW.minimum_receipt_sha, NEW.coverage_complete,
      NEW.adjudication_complete, NEW.semantic_policy_qualified,
      NEW.no_match_basis, NEW.final_status, NEW.stage_reason_code,
      NEW.evidence_anchors
    ) <> 1
    OR NOT EXISTS (
      SELECT 1 FROM audit_semantic_release_authorizations_v2 authorization
      WHERE authorization.receipt_id = NEW.minimum_receipt_sha
        AND authorization.receipt_material_sha256
            = audit_semantic_receipt_material_sha()
        AND authorization.qualification_id
            = audit_semantic_receipt_qualification_id()
        AND authorization.no_match_basis = NEW.no_match_basis
        AND authorization.semantic_policy_profile_id
            = NEW.semantic_policy_profile_id
    )
  ) THEN RAISE(ABORT, 'complete_no_match lacks durable release authorization') END;
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
"""


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


_COVERAGE_INTEGRITY_GUARDS_SQL = """
CREATE TABLE audit_coverage_integrity_probe(
  value INTEGER NOT NULL CHECK(value = 0)
);
INSERT INTO audit_coverage_integrity_probe(value)
SELECT 1 FROM audit_receipts receipt
WHERE (
    receipt.coverage_complete = 1
    OR receipt.final_status = 'complete_no_match'
  )
  AND (
    receipt.invalid_schema = 1
    OR receipt.invalid_anchor = 1
    OR receipt.truncated = 1
  );
DROP TABLE audit_coverage_integrity_probe;
CREATE TRIGGER audit_receipts_coverage_fault_guard
BEFORE INSERT ON audit_receipts
WHEN (
    NEW.coverage_complete = 1
    OR NEW.final_status = 'complete_no_match'
  )
  AND (
    NEW.invalid_schema = 1
    OR NEW.invalid_anchor = 1
    OR NEW.truncated = 1
  )
BEGIN
  SELECT RAISE(ABORT, 'complete coverage cannot contain coverage faults');
END;
"""


_L1_FROZEN_IDENTITY_SQL = """
ALTER TABLE audit_snapshots ADD COLUMN run_id TEXT
  REFERENCES audit_run_manifests(run_id);
ALTER TABLE audit_snapshots ADD COLUMN batch_id TEXT;
CREATE UNIQUE INDEX audit_snapshots_run_batch_unique
  ON audit_snapshots(run_id, batch_id);
CREATE TRIGGER audit_snapshots_run_batch_required
BEFORE INSERT ON audit_snapshots
WHEN length(NEW.snapshot_id) = 64
  AND NEW.snapshot_id NOT GLOB '*[^0-9a-f]*'
  AND (NEW.run_id IS NULL OR NEW.batch_id IS NULL OR NEW.batch_id = '')
BEGIN
  SELECT RAISE(ABORT, 'snapshot run and batch ownership is required');
END;

CREATE TABLE audit_batch_pair_receipts(
  run_id TEXT NOT NULL REFERENCES audit_run_manifests(run_id),
  batch_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL REFERENCES audit_snapshots(snapshot_id),
  pair_plan_sha TEXT NOT NULL CHECK(length(pair_plan_sha) = 64),
  pair_result_sha TEXT NOT NULL CHECK(length(pair_result_sha) = 64),
  pair_count INTEGER NOT NULL CHECK(pair_count >= 0),
  completed_at TEXT NOT NULL,
  PRIMARY KEY(run_id, batch_id),
  UNIQUE(pair_plan_sha, pair_result_sha)
);
CREATE TABLE audit_activation_receipts(
  activation_receipt_sha TEXT PRIMARY KEY CHECK(length(activation_receipt_sha) = 64),
  staging_candidate_id TEXT NOT NULL UNIQUE
    REFERENCES audit_batch_staging(staging_candidate_id),
  receipt_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
""" + _immutable_guards(
    "audit_batch_pair_receipts", "audit_activation_receipts"
) + """
DROP TRIGGER IF EXISTS audit_activation_maps_evidence_guard;
CREATE TRIGGER audit_batch_pairs_owner_and_order_guard
BEFORE INSERT ON audit_batch_pairs
BEGIN
  SELECT CASE WHEN NEW.left_staging_candidate_id
      >= NEW.right_staging_candidate_id
    THEN RAISE(ABORT, 'batch pair endpoints must be canonically ordered') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_batch_staging left_item
    JOIN audit_batch_staging right_item
      ON right_item.staging_candidate_id = NEW.right_staging_candidate_id
    WHERE left_item.staging_candidate_id = NEW.left_staging_candidate_id
      AND left_item.run_id = NEW.run_id
      AND left_item.batch_id = NEW.batch_id
      AND right_item.run_id = NEW.run_id
      AND right_item.batch_id = NEW.batch_id
  ) THEN RAISE(ABORT, 'batch pair endpoints do not share one batch') END;
  SELECT CASE WHEN NEW.left_staging_candidate_id GLOB 'stg-v2-*'
    AND NOT EXISTS (
    SELECT 1 FROM audit_batch_pair_receipts receipt
    WHERE receipt.run_id = NEW.run_id
      AND receipt.batch_id = NEW.batch_id
      AND receipt.pair_plan_sha = NEW.pair_plan_sha
      AND receipt.pair_result_sha = NEW.pair_result_sha
  ) THEN RAISE(ABORT, 'batch pair is not bound to its completed receipt') END;
END;
CREATE TRIGGER audit_activation_maps_evidence_guard
BEFORE INSERT ON audit_activation_maps
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_batch_staging staging
    WHERE staging.staging_candidate_id = NEW.staging_candidate_id
      AND staging.raw_artifact_sha = NEW.raw_artifact_sha
  ) THEN RAISE(ABORT, 'activation staging artifact mismatch') END;
  SELECT CASE WHEN NEW.staging_candidate_id GLOB 'stg-v2-*'
    AND NOT EXISTS (
    SELECT 1
    FROM audit_batch_staging staging
    JOIN audit_batch_pair_receipts receipt
      ON receipt.run_id = staging.run_id
     AND receipt.batch_id = staging.batch_id
    JOIN audit_snapshots snapshot
      ON snapshot.snapshot_id = receipt.snapshot_id
    WHERE staging.staging_candidate_id = NEW.staging_candidate_id
      AND receipt.pair_plan_sha = NEW.pair_plan_sha
      AND receipt.pair_result_sha = NEW.pair_result_sha
      AND NEW.source_sequence > snapshot.history_as_of_watermark
  ) THEN RAISE(ABORT, 'activation pair or watermark evidence mismatch') END;
  SELECT CASE WHEN NEW.staging_candidate_id NOT GLOB 'stg-v2-*'
    AND NOT EXISTS (
    SELECT 1
    FROM audit_batch_staging staging
    JOIN audit_batch_pairs pair
      ON pair.run_id = staging.run_id AND pair.batch_id = staging.batch_id
     AND (
       pair.left_staging_candidate_id = staging.staging_candidate_id
       OR pair.right_staging_candidate_id = staging.staging_candidate_id
     )
    WHERE staging.staging_candidate_id = NEW.staging_candidate_id
      AND pair.pair_plan_sha = NEW.pair_plan_sha
      AND pair.pair_result_sha = NEW.pair_result_sha
  ) THEN RAISE(ABORT, 'legacy activation pair evidence mismatch') END;
  SELECT CASE WHEN NEW.staging_candidate_id GLOB 'stg-v2-*'
    AND NOT EXISTS (
    SELECT 1 FROM audit_activation_receipts receipt
    WHERE receipt.staging_candidate_id = NEW.staging_candidate_id
      AND receipt.activation_receipt_sha = NEW.activation_receipt_sha
  ) THEN RAISE(ABORT, 'activation receipt is missing') END;
END;
CREATE TRIGGER audit_receipts_snapshot_run_guard
BEFORE INSERT ON audit_receipts
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_snapshots snapshot
    WHERE snapshot.snapshot_id = NEW.snapshot_id
      AND snapshot.run_id = NEW.run_id
  ) THEN RAISE(ABORT, 'receipt snapshot run ownership mismatch') END;
END;
"""


_PAIR_SNAPSHOT_OWNERSHIP_SQL = """
CREATE TABLE audit_pair_snapshot_owner_probe(
  value INTEGER NOT NULL CHECK(value = 0)
);
INSERT INTO audit_pair_snapshot_owner_probe(value)
SELECT 1
FROM audit_batch_pair_receipts receipt
LEFT JOIN audit_snapshots snapshot
  ON snapshot.snapshot_id = receipt.snapshot_id
WHERE snapshot.snapshot_id IS NULL
   OR snapshot.run_id IS NULL
   OR snapshot.batch_id IS NULL
   OR snapshot.run_id <> receipt.run_id
   OR snapshot.batch_id <> receipt.batch_id;
DROP TABLE audit_pair_snapshot_owner_probe;
CREATE TRIGGER audit_batch_pair_receipts_snapshot_owner_guard
BEFORE INSERT ON audit_batch_pair_receipts
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_snapshots snapshot
    WHERE snapshot.snapshot_id = NEW.snapshot_id
      AND snapshot.run_id = NEW.run_id
      AND snapshot.batch_id = NEW.batch_id
  ) THEN RAISE(ABORT, 'batch pair snapshot ownership mismatch') END;
END;
"""


_SNAPSHOT_BATCH_MEMBERSHIP_SQL = """
CREATE TABLE audit_snapshot_batch_sets(
  snapshot_id TEXT PRIMARY KEY REFERENCES audit_snapshots(snapshot_id),
  run_id TEXT NOT NULL REFERENCES audit_run_manifests(run_id),
  batch_id TEXT NOT NULL,
  current_batch_ids_hash TEXT NOT NULL CHECK(length(current_batch_ids_hash) = 64),
  member_ids_json TEXT NOT NULL,
  member_count INTEGER NOT NULL CHECK(member_count >= 1),
  created_at TEXT NOT NULL,
  UNIQUE(snapshot_id, current_batch_ids_hash),
  UNIQUE(run_id, batch_id)
);
CREATE TABLE audit_batch_pair_set_bindings(
  run_id TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  pair_plan_sha TEXT NOT NULL CHECK(length(pair_plan_sha) = 64),
  pair_result_sha TEXT NOT NULL CHECK(length(pair_result_sha) = 64),
  current_batch_ids_hash TEXT NOT NULL CHECK(length(current_batch_ids_hash) = 64),
  member_count INTEGER NOT NULL CHECK(member_count >= 1),
  created_at TEXT NOT NULL,
  PRIMARY KEY(run_id, batch_id),
  FOREIGN KEY(run_id, batch_id)
    REFERENCES audit_batch_pair_receipts(run_id, batch_id),
  FOREIGN KEY(snapshot_id, current_batch_ids_hash)
    REFERENCES audit_snapshot_batch_sets(snapshot_id, current_batch_ids_hash)
);
""" + _immutable_guards(
    "audit_snapshot_batch_sets", "audit_batch_pair_set_bindings"
) + """
CREATE TABLE audit_snapshot_batch_membership_probe(
  value INTEGER NOT NULL CHECK(value = 0)
);
INSERT INTO audit_snapshot_batch_membership_probe(value)
SELECT 1 FROM audit_snapshots snapshot
WHERE length(snapshot.snapshot_id) = 64
  AND snapshot.snapshot_id NOT GLOB '*[^0-9a-f]*'
  AND NOT EXISTS (
    SELECT 1 FROM audit_snapshot_batch_sets batch_set
    WHERE batch_set.snapshot_id = snapshot.snapshot_id
      AND batch_set.run_id = snapshot.run_id
      AND batch_set.batch_id = snapshot.batch_id
      AND batch_set.current_batch_ids_hash = snapshot.current_batch_ids_hash
      AND batch_set.member_count = json_array_length(batch_set.member_ids_json)
      AND audit_current_batch_ids_sha(batch_set.member_ids_json)
          = snapshot.current_batch_ids_hash
  );
INSERT INTO audit_snapshot_batch_membership_probe(value)
SELECT 1 FROM audit_batch_pair_receipts receipt
JOIN audit_snapshots snapshot ON snapshot.snapshot_id = receipt.snapshot_id
WHERE length(snapshot.snapshot_id) = 64
  AND snapshot.snapshot_id NOT GLOB '*[^0-9a-f]*'
  AND NOT EXISTS (
    SELECT 1 FROM audit_batch_pair_set_bindings binding
    JOIN audit_snapshot_batch_sets batch_set
      ON batch_set.snapshot_id = binding.snapshot_id
     AND batch_set.current_batch_ids_hash = binding.current_batch_ids_hash
    WHERE binding.run_id = receipt.run_id
      AND binding.batch_id = receipt.batch_id
      AND binding.snapshot_id = receipt.snapshot_id
      AND binding.pair_plan_sha = receipt.pair_plan_sha
      AND binding.pair_result_sha = receipt.pair_result_sha
      AND binding.member_count = batch_set.member_count
  );
INSERT INTO audit_snapshot_batch_membership_probe(value)
SELECT 1 FROM audit_activation_maps activation
JOIN audit_batch_staging staging
  ON staging.staging_candidate_id = activation.staging_candidate_id
JOIN audit_batch_pair_receipts receipt
  ON receipt.run_id = staging.run_id AND receipt.batch_id = staging.batch_id
WHERE staging.staging_candidate_id GLOB 'stg-v2-*'
  AND NOT EXISTS (
    SELECT 1
    FROM audit_batch_pair_set_bindings binding
    JOIN audit_snapshot_batch_sets batch_set
      ON batch_set.snapshot_id = binding.snapshot_id
     AND batch_set.current_batch_ids_hash = binding.current_batch_ids_hash
    JOIN json_each(batch_set.member_ids_json) member
      ON member.value = staging.staging_candidate_id
    WHERE binding.run_id = receipt.run_id
      AND binding.batch_id = receipt.batch_id
      AND binding.snapshot_id = receipt.snapshot_id
      AND binding.pair_plan_sha = activation.pair_plan_sha
      AND binding.pair_result_sha = activation.pair_result_sha
  );
DROP TABLE audit_snapshot_batch_membership_probe;
CREATE TRIGGER audit_snapshot_batch_sets_identity_guard
BEFORE INSERT ON audit_snapshot_batch_sets
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_snapshots snapshot
    WHERE snapshot.snapshot_id = NEW.snapshot_id
      AND snapshot.run_id = NEW.run_id
      AND snapshot.batch_id = NEW.batch_id
      AND snapshot.current_batch_ids_hash = NEW.current_batch_ids_hash
      AND snapshot.current_batch_id_namespace = 'history-v2-staging-v1'
  ) THEN RAISE(ABORT, 'snapshot batch set identity mismatch') END;
  SELECT CASE WHEN json_array_length(NEW.member_ids_json) <> NEW.member_count
    OR audit_current_batch_ids_sha(NEW.member_ids_json)
       <> NEW.current_batch_ids_hash
    THEN RAISE(ABORT, 'snapshot batch member set hash mismatch') END;
END;
CREATE TRIGGER audit_batch_pair_receipts_snapshot_set_guard
BEFORE INSERT ON audit_batch_pair_receipts
WHEN length(NEW.snapshot_id) = 64
  AND NEW.snapshot_id NOT GLOB '*[^0-9a-f]*'
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_snapshot_batch_sets batch_set
    WHERE batch_set.snapshot_id = NEW.snapshot_id
      AND batch_set.run_id = NEW.run_id
      AND batch_set.batch_id = NEW.batch_id
  ) THEN RAISE(ABORT, 'batch pair snapshot member set is missing') END;
END;
CREATE TRIGGER audit_batch_pair_set_bindings_identity_guard
BEFORE INSERT ON audit_batch_pair_set_bindings
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_batch_pair_receipts receipt
    JOIN audit_snapshot_batch_sets batch_set
      ON batch_set.snapshot_id = receipt.snapshot_id
    WHERE receipt.run_id = NEW.run_id
      AND receipt.batch_id = NEW.batch_id
      AND receipt.snapshot_id = NEW.snapshot_id
      AND receipt.pair_plan_sha = NEW.pair_plan_sha
      AND receipt.pair_result_sha = NEW.pair_result_sha
      AND batch_set.run_id = NEW.run_id
      AND batch_set.batch_id = NEW.batch_id
      AND batch_set.current_batch_ids_hash = NEW.current_batch_ids_hash
      AND batch_set.member_count = NEW.member_count
      AND receipt.pair_count
          = (batch_set.member_count * (batch_set.member_count - 1)) / 2
  ) THEN RAISE(ABORT, 'batch pair set binding identity mismatch') END;
END;
CREATE TRIGGER audit_batch_pairs_set_binding_guard
BEFORE INSERT ON audit_batch_pairs
WHEN NEW.left_staging_candidate_id GLOB 'stg-v2-*'
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_batch_pair_set_bindings binding
    JOIN audit_snapshot_batch_sets batch_set
      ON batch_set.snapshot_id = binding.snapshot_id
     AND batch_set.current_batch_ids_hash = binding.current_batch_ids_hash
    JOIN json_each(batch_set.member_ids_json) left_member
      ON left_member.value = NEW.left_staging_candidate_id
    JOIN json_each(batch_set.member_ids_json) right_member
      ON right_member.value = NEW.right_staging_candidate_id
    WHERE binding.run_id = NEW.run_id
      AND binding.batch_id = NEW.batch_id
      AND binding.pair_plan_sha = NEW.pair_plan_sha
      AND binding.pair_result_sha = NEW.pair_result_sha
  ) THEN RAISE(ABORT, 'batch pair exact set binding is missing') END;
END;
DROP TRIGGER IF EXISTS audit_activation_maps_evidence_guard;
CREATE TRIGGER audit_activation_maps_evidence_guard
BEFORE INSERT ON audit_activation_maps
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_batch_staging staging
    WHERE staging.staging_candidate_id = NEW.staging_candidate_id
      AND staging.raw_artifact_sha = NEW.raw_artifact_sha
  ) THEN RAISE(ABORT, 'activation staging artifact mismatch') END;
  SELECT CASE WHEN NEW.staging_candidate_id GLOB 'stg-v2-*'
    AND NOT EXISTS (
    SELECT 1
    FROM audit_batch_staging staging
    JOIN audit_batch_pair_receipts receipt
      ON receipt.run_id = staging.run_id
     AND receipt.batch_id = staging.batch_id
    JOIN audit_batch_pair_set_bindings binding
      ON binding.run_id = receipt.run_id
     AND binding.batch_id = receipt.batch_id
     AND binding.snapshot_id = receipt.snapshot_id
     AND binding.pair_plan_sha = receipt.pair_plan_sha
     AND binding.pair_result_sha = receipt.pair_result_sha
    JOIN audit_snapshot_batch_sets batch_set
      ON batch_set.snapshot_id = binding.snapshot_id
     AND batch_set.current_batch_ids_hash = binding.current_batch_ids_hash
     AND batch_set.member_count = binding.member_count
    JOIN audit_snapshots snapshot
      ON snapshot.snapshot_id = batch_set.snapshot_id
    JOIN json_each(batch_set.member_ids_json) member
      ON member.value = staging.staging_candidate_id
    WHERE staging.staging_candidate_id = NEW.staging_candidate_id
      AND binding.pair_plan_sha = NEW.pair_plan_sha
      AND binding.pair_result_sha = NEW.pair_result_sha
      AND NEW.source_sequence > snapshot.history_as_of_watermark
      AND (
        SELECT count(*) FROM audit_batch_pairs pair
        WHERE pair.run_id = receipt.run_id
          AND pair.batch_id = receipt.batch_id
          AND pair.pair_plan_sha = receipt.pair_plan_sha
          AND pair.pair_result_sha = receipt.pair_result_sha
      ) = receipt.pair_count
  ) THEN RAISE(ABORT, 'activation batch membership or watermark mismatch') END;
  SELECT CASE WHEN NEW.staging_candidate_id NOT GLOB 'stg-v2-*'
    AND NOT EXISTS (
    SELECT 1
    FROM audit_batch_staging staging
    JOIN audit_batch_pairs pair
      ON pair.run_id = staging.run_id AND pair.batch_id = staging.batch_id
     AND (
       pair.left_staging_candidate_id = staging.staging_candidate_id
       OR pair.right_staging_candidate_id = staging.staging_candidate_id
     )
    WHERE staging.staging_candidate_id = NEW.staging_candidate_id
      AND pair.pair_plan_sha = NEW.pair_plan_sha
      AND pair.pair_result_sha = NEW.pair_result_sha
  ) THEN RAISE(ABORT, 'legacy activation pair evidence mismatch') END;
  SELECT CASE WHEN NEW.staging_candidate_id GLOB 'stg-v2-*'
    AND NOT EXISTS (
    SELECT 1 FROM audit_activation_receipts receipt
    WHERE receipt.staging_candidate_id = NEW.staging_candidate_id
      AND receipt.activation_receipt_sha = NEW.activation_receipt_sha
  ) THEN RAISE(ABORT, 'activation receipt is missing') END;
END;
"""


_STRICT_PAIR_COMPLETION_SQL = """
CREATE TABLE audit_strict_pair_completion_probe(
  value INTEGER NOT NULL CHECK(value = 0)
);
INSERT INTO audit_strict_pair_completion_probe(value)
SELECT 1
FROM audit_batch_pair_receipts receipt
JOIN audit_snapshots snapshot ON snapshot.snapshot_id = receipt.snapshot_id
WHERE length(snapshot.snapshot_id) = 64
  AND snapshot.snapshot_id NOT GLOB '*[^0-9a-f]*'
  AND NOT EXISTS (
    SELECT 1
    FROM audit_batch_pair_set_bindings binding
    JOIN audit_snapshot_batch_sets batch_set
      ON batch_set.snapshot_id = binding.snapshot_id
     AND batch_set.current_batch_ids_hash = binding.current_batch_ids_hash
    WHERE binding.run_id = receipt.run_id
      AND binding.batch_id = receipt.batch_id
      AND binding.snapshot_id = receipt.snapshot_id
      AND binding.pair_plan_sha = receipt.pair_plan_sha
      AND binding.pair_result_sha = receipt.pair_result_sha
      AND binding.member_count = batch_set.member_count
      AND receipt.pair_count
          = (batch_set.member_count * (batch_set.member_count - 1)) / 2
      AND NOT EXISTS (
        SELECT 1 FROM audit_batch_pairs pair
        WHERE pair.run_id = receipt.run_id
          AND pair.batch_id = receipt.batch_id
          AND NOT EXISTS (
            SELECT 1
            FROM json_each(batch_set.member_ids_json) left_member
            JOIN json_each(batch_set.member_ids_json) right_member
              ON left_member.value < right_member.value
            WHERE pair.pair_plan_sha = receipt.pair_plan_sha
              AND pair.pair_result_sha = receipt.pair_result_sha
              AND pair.left_staging_candidate_id = left_member.value
              AND pair.right_staging_candidate_id = right_member.value
          )
      )
      AND NOT EXISTS (
        SELECT 1
        FROM json_each(batch_set.member_ids_json) left_member
        JOIN json_each(batch_set.member_ids_json) right_member
          ON left_member.value < right_member.value
        WHERE NOT EXISTS (
          SELECT 1 FROM audit_batch_pairs pair
          WHERE pair.run_id = receipt.run_id
            AND pair.batch_id = receipt.batch_id
            AND pair.pair_plan_sha = receipt.pair_plan_sha
            AND pair.pair_result_sha = receipt.pair_result_sha
            AND pair.left_staging_candidate_id = left_member.value
            AND pair.right_staging_candidate_id = right_member.value
        )
      )
  );
INSERT INTO audit_strict_pair_completion_probe(value)
SELECT 1
FROM audit_activation_maps activation
JOIN audit_batch_staging staging
  ON staging.staging_candidate_id = activation.staging_candidate_id
JOIN audit_batch_pair_receipts receipt
  ON receipt.run_id = staging.run_id AND receipt.batch_id = staging.batch_id
JOIN audit_snapshots snapshot ON snapshot.snapshot_id = receipt.snapshot_id
WHERE length(snapshot.snapshot_id) = 64
  AND snapshot.snapshot_id NOT GLOB '*[^0-9a-f]*'
  AND NOT EXISTS (
    SELECT 1
    FROM audit_batch_pair_set_bindings binding
    JOIN audit_snapshot_batch_sets batch_set
      ON batch_set.snapshot_id = binding.snapshot_id
     AND batch_set.current_batch_ids_hash = binding.current_batch_ids_hash
    JOIN json_each(batch_set.member_ids_json) selected_member
      ON selected_member.value = activation.staging_candidate_id
    WHERE binding.run_id = receipt.run_id
      AND binding.batch_id = receipt.batch_id
      AND binding.snapshot_id = receipt.snapshot_id
      AND binding.pair_plan_sha = activation.pair_plan_sha
      AND binding.pair_result_sha = activation.pair_result_sha
      AND (
        batch_set.member_count = 1
        OR EXISTS (
          SELECT 1 FROM audit_batch_pairs pair
          WHERE pair.run_id = receipt.run_id
            AND pair.batch_id = receipt.batch_id
            AND pair.pair_plan_sha = receipt.pair_plan_sha
            AND pair.pair_result_sha = receipt.pair_result_sha
            AND (
              pair.left_staging_candidate_id = activation.staging_candidate_id
              OR pair.right_staging_candidate_id = activation.staging_candidate_id
            )
        )
      )
  );
DROP TABLE audit_strict_pair_completion_probe;

DROP TRIGGER IF EXISTS audit_batch_pair_receipts_snapshot_set_guard;
CREATE TRIGGER audit_batch_pair_receipts_snapshot_set_guard
BEFORE INSERT ON audit_batch_pair_receipts
WHEN length(NEW.snapshot_id) = 64
  AND NEW.snapshot_id NOT GLOB '*[^0-9a-f]*'
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_snapshot_batch_sets batch_set
    WHERE batch_set.snapshot_id = NEW.snapshot_id
      AND batch_set.run_id = NEW.run_id
      AND batch_set.batch_id = NEW.batch_id
      AND NEW.pair_count
          = (batch_set.member_count * (batch_set.member_count - 1)) / 2
      AND NOT EXISTS (
        SELECT 1 FROM audit_batch_pairs pair
        WHERE pair.run_id = NEW.run_id
          AND pair.batch_id = NEW.batch_id
          AND NOT EXISTS (
            SELECT 1
            FROM json_each(batch_set.member_ids_json) left_member
            JOIN json_each(batch_set.member_ids_json) right_member
              ON left_member.value < right_member.value
            WHERE pair.pair_plan_sha = NEW.pair_plan_sha
              AND pair.pair_result_sha = NEW.pair_result_sha
              AND pair.left_staging_candidate_id = left_member.value
              AND pair.right_staging_candidate_id = right_member.value
          )
      )
  ) THEN RAISE(ABORT, 'strict batch pair receipt set mismatch') END;
END;

DROP TRIGGER IF EXISTS audit_batch_pair_set_bindings_identity_guard;
CREATE TRIGGER audit_batch_pair_set_bindings_identity_guard
BEFORE INSERT ON audit_batch_pair_set_bindings
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_batch_pair_receipts receipt
    JOIN audit_snapshot_batch_sets batch_set
      ON batch_set.snapshot_id = receipt.snapshot_id
    WHERE receipt.run_id = NEW.run_id
      AND receipt.batch_id = NEW.batch_id
      AND receipt.snapshot_id = NEW.snapshot_id
      AND receipt.pair_plan_sha = NEW.pair_plan_sha
      AND receipt.pair_result_sha = NEW.pair_result_sha
      AND batch_set.run_id = NEW.run_id
      AND batch_set.batch_id = NEW.batch_id
      AND batch_set.current_batch_ids_hash = NEW.current_batch_ids_hash
      AND batch_set.member_count = NEW.member_count
      AND receipt.pair_count
          = (batch_set.member_count * (batch_set.member_count - 1)) / 2
      AND NOT EXISTS (
        SELECT 1 FROM audit_batch_pairs pair
        WHERE pair.run_id = NEW.run_id
          AND pair.batch_id = NEW.batch_id
          AND NOT EXISTS (
            SELECT 1
            FROM json_each(batch_set.member_ids_json) left_member
            JOIN json_each(batch_set.member_ids_json) right_member
              ON left_member.value < right_member.value
            WHERE pair.pair_plan_sha = NEW.pair_plan_sha
              AND pair.pair_result_sha = NEW.pair_result_sha
              AND pair.left_staging_candidate_id = left_member.value
              AND pair.right_staging_candidate_id = right_member.value
          )
      )
  ) THEN RAISE(ABORT, 'batch pair set binding identity mismatch') END;
END;

DROP TRIGGER IF EXISTS audit_batch_pairs_set_binding_guard;
CREATE TRIGGER audit_batch_pairs_set_binding_guard
BEFORE INSERT ON audit_batch_pairs
BEGIN
  SELECT CASE WHEN EXISTS (
    SELECT 1
    FROM audit_batch_pair_receipts receipt
    JOIN audit_snapshots snapshot ON snapshot.snapshot_id = receipt.snapshot_id
    WHERE receipt.run_id = NEW.run_id
      AND receipt.batch_id = NEW.batch_id
      AND length(snapshot.snapshot_id) = 64
      AND snapshot.snapshot_id NOT GLOB '*[^0-9a-f]*'
  ) AND NOT EXISTS (
    SELECT 1
    FROM audit_batch_pair_receipts receipt
    JOIN audit_batch_pair_set_bindings binding
      ON binding.run_id = receipt.run_id
     AND binding.batch_id = receipt.batch_id
     AND binding.snapshot_id = receipt.snapshot_id
     AND binding.pair_plan_sha = receipt.pair_plan_sha
     AND binding.pair_result_sha = receipt.pair_result_sha
    JOIN audit_snapshot_batch_sets batch_set
      ON batch_set.snapshot_id = binding.snapshot_id
     AND batch_set.current_batch_ids_hash = binding.current_batch_ids_hash
    JOIN json_each(batch_set.member_ids_json) left_member
      ON left_member.value = NEW.left_staging_candidate_id
    JOIN json_each(batch_set.member_ids_json) right_member
      ON right_member.value = NEW.right_staging_candidate_id
    WHERE receipt.run_id = NEW.run_id
      AND receipt.batch_id = NEW.batch_id
      AND receipt.pair_plan_sha = NEW.pair_plan_sha
      AND receipt.pair_result_sha = NEW.pair_result_sha
      AND left_member.value < right_member.value
  ) THEN RAISE(ABORT, 'strict batch pair is outside the frozen pair set') END;
END;

DROP TRIGGER IF EXISTS audit_activation_maps_evidence_guard;
CREATE TRIGGER audit_activation_maps_evidence_guard
BEFORE INSERT ON audit_activation_maps
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_batch_staging staging
    WHERE staging.staging_candidate_id = NEW.staging_candidate_id
      AND staging.raw_artifact_sha = NEW.raw_artifact_sha
  ) THEN RAISE(ABORT, 'activation staging artifact mismatch') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1
    FROM audit_batch_staging staging
    JOIN audit_batch_pair_receipts receipt
      ON receipt.run_id = staging.run_id AND receipt.batch_id = staging.batch_id
    JOIN audit_batch_pair_set_bindings binding
      ON binding.run_id = receipt.run_id
     AND binding.batch_id = receipt.batch_id
     AND binding.snapshot_id = receipt.snapshot_id
     AND binding.pair_plan_sha = receipt.pair_plan_sha
     AND binding.pair_result_sha = receipt.pair_result_sha
    JOIN audit_snapshots snapshot ON snapshot.snapshot_id = binding.snapshot_id
    WHERE staging.staging_candidate_id = NEW.staging_candidate_id
      AND binding.pair_plan_sha = NEW.pair_plan_sha
      AND binding.pair_result_sha = NEW.pair_result_sha
      AND length(snapshot.snapshot_id) = 64
      AND snapshot.snapshot_id NOT GLOB '*[^0-9a-f]*'
  ) AND NOT EXISTS (
    SELECT 1
    FROM audit_batch_staging staging
    JOIN audit_batch_pair_receipts receipt
      ON receipt.run_id = staging.run_id AND receipt.batch_id = staging.batch_id
    JOIN audit_batch_pair_set_bindings binding
      ON binding.run_id = receipt.run_id
     AND binding.batch_id = receipt.batch_id
     AND binding.snapshot_id = receipt.snapshot_id
     AND binding.pair_plan_sha = receipt.pair_plan_sha
     AND binding.pair_result_sha = receipt.pair_result_sha
    JOIN audit_snapshot_batch_sets batch_set
      ON batch_set.snapshot_id = binding.snapshot_id
     AND batch_set.current_batch_ids_hash = binding.current_batch_ids_hash
     AND batch_set.member_count = binding.member_count
    JOIN audit_snapshots snapshot ON snapshot.snapshot_id = binding.snapshot_id
    JOIN json_each(batch_set.member_ids_json) selected_member
      ON selected_member.value = staging.staging_candidate_id
    JOIN audit_activation_receipts activation_receipt
      ON activation_receipt.staging_candidate_id = staging.staging_candidate_id
     AND activation_receipt.activation_receipt_sha = NEW.activation_receipt_sha
    WHERE staging.staging_candidate_id = NEW.staging_candidate_id
      AND binding.pair_plan_sha = NEW.pair_plan_sha
      AND binding.pair_result_sha = NEW.pair_result_sha
      AND NEW.source_sequence > snapshot.history_as_of_watermark
      AND receipt.pair_count
          = (batch_set.member_count * (batch_set.member_count - 1)) / 2
      AND NOT EXISTS (
        SELECT 1 FROM audit_batch_pairs pair
        WHERE pair.run_id = receipt.run_id
          AND pair.batch_id = receipt.batch_id
          AND NOT EXISTS (
            SELECT 1
            FROM json_each(batch_set.member_ids_json) left_member
            JOIN json_each(batch_set.member_ids_json) right_member
              ON left_member.value < right_member.value
            WHERE pair.pair_plan_sha = receipt.pair_plan_sha
              AND pair.pair_result_sha = receipt.pair_result_sha
              AND pair.left_staging_candidate_id = left_member.value
              AND pair.right_staging_candidate_id = right_member.value
          )
      )
      AND NOT EXISTS (
        SELECT 1
        FROM json_each(batch_set.member_ids_json) left_member
        JOIN json_each(batch_set.member_ids_json) right_member
          ON left_member.value < right_member.value
        WHERE NOT EXISTS (
          SELECT 1 FROM audit_batch_pairs pair
          WHERE pair.run_id = receipt.run_id
            AND pair.batch_id = receipt.batch_id
            AND pair.pair_plan_sha = receipt.pair_plan_sha
            AND pair.pair_result_sha = receipt.pair_result_sha
            AND pair.left_staging_candidate_id = left_member.value
            AND pair.right_staging_candidate_id = right_member.value
        )
      )
      AND (
        batch_set.member_count = 1
        OR EXISTS (
          SELECT 1 FROM audit_batch_pairs pair
          WHERE pair.run_id = receipt.run_id
            AND pair.batch_id = receipt.batch_id
            AND pair.pair_plan_sha = receipt.pair_plan_sha
            AND pair.pair_result_sha = receipt.pair_result_sha
            AND (
              pair.left_staging_candidate_id = NEW.staging_candidate_id
              OR pair.right_staging_candidate_id = NEW.staging_candidate_id
            )
        )
      )
  ) THEN RAISE(ABORT, 'strict activation pair completion mismatch') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_batch_staging staging
    JOIN audit_batch_pair_receipts receipt
      ON receipt.run_id = staging.run_id AND receipt.batch_id = staging.batch_id
    JOIN audit_batch_pair_set_bindings binding
      ON binding.run_id = receipt.run_id
     AND binding.batch_id = receipt.batch_id
     AND binding.snapshot_id = receipt.snapshot_id
     AND binding.pair_plan_sha = receipt.pair_plan_sha
     AND binding.pair_result_sha = receipt.pair_result_sha
    JOIN audit_snapshots snapshot ON snapshot.snapshot_id = binding.snapshot_id
    WHERE staging.staging_candidate_id = NEW.staging_candidate_id
      AND binding.pair_plan_sha = NEW.pair_plan_sha
      AND binding.pair_result_sha = NEW.pair_result_sha
      AND length(snapshot.snapshot_id) = 64
      AND snapshot.snapshot_id NOT GLOB '*[^0-9a-f]*'
  ) AND NOT EXISTS (
    SELECT 1
    FROM audit_batch_staging staging
    JOIN audit_batch_pairs pair
      ON pair.run_id = staging.run_id AND pair.batch_id = staging.batch_id
     AND (
       pair.left_staging_candidate_id = staging.staging_candidate_id
       OR pair.right_staging_candidate_id = staging.staging_candidate_id
     )
    WHERE staging.staging_candidate_id = NEW.staging_candidate_id
      AND pair.pair_plan_sha = NEW.pair_plan_sha
      AND pair.pair_result_sha = NEW.pair_result_sha
  ) THEN RAISE(ABORT, 'legacy activation pair evidence mismatch') END;
END;
"""


_L2_RUNTIME_SQL = """
CREATE TABLE audit_task_bindings_v2(
  task_hash TEXT PRIMARY KEY REFERENCES audit_logical_tasks(task_hash),
  plan_sha TEXT NOT NULL CHECK(length(plan_sha) = 64),
  snapshot_id TEXT NOT NULL REFERENCES audit_snapshots(snapshot_id),
  snapshot_hash TEXT NOT NULL CHECK(length(snapshot_hash) = 64),
  shard_input_sha TEXT NOT NULL CHECK(length(shard_input_sha) = 64),
  assigned_item_ids_json TEXT NOT NULL,
  frozen_records_json TEXT NOT NULL,
  provider_pool_json TEXT NOT NULL,
  parent_task_hash TEXT REFERENCES audit_logical_tasks(task_hash),
  split_depth INTEGER NOT NULL CHECK(split_depth >= 0),
  created_at TEXT NOT NULL
);
CREATE TABLE audit_attempt_completions_v2(
  attempt_id TEXT PRIMARY KEY REFERENCES audit_task_attempts(attempt_id),
  output_cas_object_id TEXT NOT NULL REFERENCES audit_cas_objects(object_id),
  outcome TEXT NOT NULL CHECK(outcome IN (
    'valid','timeout','429','5xx','overflow','syntax','schema',
    'item_set','truncated','invalid_anchor','provider_error'
  )),
  normalized_result_json TEXT,
  usage_json TEXT NOT NULL,
  completed_at TEXT NOT NULL,
  CHECK((outcome = 'valid') = (normalized_result_json IS NOT NULL))
);
CREATE TABLE audit_task_settlements_v2(
  task_hash TEXT PRIMARY KEY REFERENCES audit_logical_tasks(task_hash),
  settlement_sha256 TEXT NOT NULL UNIQUE CHECK(length(settlement_sha256) = 64),
  settlement_kind TEXT NOT NULL CHECK(settlement_kind IN ('equal','conflict')),
  normalized_result_json TEXT,
  valid_attempt_ids_json TEXT NOT NULL,
  valid_output_cas_ids_json TEXT NOT NULL,
  settled_at TEXT NOT NULL,
  CHECK((settlement_kind = 'equal') = (normalized_result_json IS NOT NULL))
);
CREATE TABLE audit_task_edges_v2(
  parent_task_hash TEXT NOT NULL REFERENCES audit_logical_tasks(task_hash),
  child_task_hash TEXT NOT NULL UNIQUE REFERENCES audit_logical_tasks(task_hash),
  position INTEGER NOT NULL CHECK(position IN (0,1)),
  edge_sha256 TEXT NOT NULL UNIQUE CHECK(length(edge_sha256) = 64),
  created_at TEXT NOT NULL,
  PRIMARY KEY(parent_task_hash, position),
  CHECK(parent_task_hash <> child_task_hash)
);
CREATE TABLE audit_task_terminal_facts_v2(
  task_hash TEXT PRIMARY KEY REFERENCES audit_logical_tasks(task_hash),
  terminal_state TEXT NOT NULL CHECK(terminal_state IN ('superseded','exhausted')),
  reason TEXT NOT NULL,
  fact_sha256 TEXT NOT NULL UNIQUE CHECK(length(fact_sha256) = 64),
  created_at TEXT NOT NULL
);
""" + _immutable_guards(
    "audit_task_bindings_v2",
    "audit_attempt_completions_v2",
    "audit_task_settlements_v2",
    "audit_task_edges_v2",
    "audit_task_terminal_facts_v2",
) + """
CREATE TRIGGER audit_task_bindings_v2_owner_guard
BEFORE INSERT ON audit_task_bindings_v2
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_logical_tasks task
    JOIN audit_run_manifests run ON run.run_id = task.run_id
    JOIN audit_snapshots snapshot ON snapshot.snapshot_id = NEW.snapshot_id
    WHERE task.task_hash = NEW.task_hash
      AND run.plan_hash = NEW.plan_sha
      AND snapshot.run_id = task.run_id
      AND snapshot.snapshot_hash = NEW.snapshot_hash
  ) THEN RAISE(ABORT, 'L2 task binding ownership mismatch') END;
END;
CREATE TRIGGER audit_attempt_completions_v2_owner_guard
BEFORE INSERT ON audit_attempt_completions_v2
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_task_attempts attempt
    JOIN audit_logical_tasks task ON task.task_hash = attempt.task_hash
    JOIN audit_task_bindings_v2 binding ON binding.task_hash = task.task_hash
    JOIN audit_cas_objects output ON output.object_id = NEW.output_cas_object_id
    WHERE attempt.attempt_id = NEW.attempt_id
      AND output.integrity_state = 'verified'
  ) THEN RAISE(ABORT, 'L2 attempt completion ownership mismatch') END;
END;
CREATE TRIGGER audit_task_settlements_v2_state_guard
BEFORE INSERT ON audit_task_settlements_v2
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_logical_tasks task
    WHERE task.task_hash = NEW.task_hash AND task.state = 'settling'
  ) THEN RAISE(ABORT, 'L2 settlement requires settling state') END;
END;
"""


_L2_RUNTIME_AUTHORITY_SQL = """
CREATE TABLE audit_l2_plans_v2(
  plan_sha TEXT PRIMARY KEY CHECK(length(plan_sha) = 64),
  run_id TEXT NOT NULL UNIQUE REFERENCES audit_run_manifests(run_id),
  candidate_id TEXT NOT NULL UNIQUE REFERENCES audit_batch_staging(staging_candidate_id),
  candidate_hash TEXT NOT NULL CHECK(length(candidate_hash) = 64),
  snapshot_id TEXT NOT NULL UNIQUE REFERENCES audit_snapshots(snapshot_id),
  snapshot_hash TEXT NOT NULL CHECK(length(snapshot_hash) = 64),
  shard_plan_sha TEXT NOT NULL UNIQUE CHECK(length(shard_plan_sha) = 64),
  budget_policy_sha TEXT NOT NULL CHECK(length(budget_policy_sha) = 64),
  intent TEXT NOT NULL,
  plan_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE audit_l2_snapshot_records_v2(
  snapshot_id TEXT PRIMARY KEY REFERENCES audit_snapshots(snapshot_id),
  records_sha TEXT NOT NULL UNIQUE CHECK(length(records_sha) = 64),
  records_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE audit_l2_task_inputs_v2(
  task_hash TEXT PRIMARY KEY REFERENCES audit_logical_tasks(task_hash),
  input_id TEXT NOT NULL,
  request_sha TEXT NOT NULL CHECK(length(request_sha) = 64),
  request_text TEXT NOT NULL,
  item_ids_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE audit_runtime_budget_reservations_v2(
  attempt_id TEXT PRIMARY KEY CHECK(length(attempt_id) = 64),
  task_hash TEXT NOT NULL REFERENCES audit_logical_tasks(task_hash),
  plan_sha TEXT NOT NULL REFERENCES audit_l2_plans_v2(plan_sha),
  candidate_id TEXT NOT NULL,
  intent TEXT NOT NULL,
  attempt_kind TEXT NOT NULL CHECK(attempt_kind IN (
    'initial','retry','failover','split','detail','reduce','cancel'
  )),
  reserved_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE audit_runtime_budget_settlements_v2(
  attempt_id TEXT PRIMARY KEY
    REFERENCES audit_runtime_budget_reservations_v2(attempt_id),
  usage_verified INTEGER NOT NULL CHECK(usage_verified IN (0,1)),
  actual_json TEXT,
  created_at TEXT NOT NULL,
  CHECK((usage_verified = 1) = (actual_json IS NOT NULL))
);
""" + _immutable_guards(
    "audit_l2_plans_v2",
    "audit_l2_snapshot_records_v2",
    "audit_l2_task_inputs_v2",
    "audit_runtime_budget_reservations_v2",
    "audit_runtime_budget_settlements_v2",
) + """
CREATE TRIGGER audit_l2_plans_v2_canonical_guard
BEFORE INSERT ON audit_l2_plans_v2
BEGIN
  SELECT CASE WHEN audit_l2_plan_valid(
    NEW.plan_json, NEW.plan_sha, NEW.run_id, NEW.candidate_id,
    NEW.candidate_hash, NEW.snapshot_id, NEW.snapshot_hash,
    NEW.shard_plan_sha, NEW.budget_policy_sha, NEW.intent
  ) <> 1 THEN RAISE(ABORT, 'L2 plan canonical identity mismatch') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_run_manifests run
    JOIN audit_batch_staging candidate
      ON candidate.staging_candidate_id = NEW.candidate_id
    JOIN audit_snapshots snapshot ON snapshot.snapshot_id = NEW.snapshot_id
    JOIN audit_l2_snapshot_records_v2 records
      ON records.snapshot_id = snapshot.snapshot_id
    WHERE run.run_id = NEW.run_id
      AND run.plan_hash = NEW.plan_sha
      AND run.manifest_json = NEW.plan_json
      AND candidate.run_id = NEW.run_id
      AND candidate.candidate_hash = NEW.candidate_hash
      AND snapshot.run_id = NEW.run_id
      AND snapshot.snapshot_hash = NEW.snapshot_hash
      AND json_extract(NEW.plan_json, '$.snapshot.records_sha') = records.records_sha
  ) THEN RAISE(ABORT, 'L2 plan authority facts mismatch') END;
END;
CREATE TRIGGER audit_l2_snapshot_records_v2_guard
BEFORE INSERT ON audit_l2_snapshot_records_v2
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_snapshots snapshot
    WHERE snapshot.snapshot_id = NEW.snapshot_id
      AND audit_l2_records_valid(
        NEW.records_json, NEW.records_sha, snapshot.expected_asset_ids_hash
      ) = 1
  ) THEN RAISE(ABORT, 'L2 snapshot records mismatch') END;
END;
CREATE TRIGGER audit_l2_task_inputs_v2_guard
BEFORE INSERT ON audit_l2_task_inputs_v2
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_task_bindings_v2 binding
    JOIN audit_l2_plans_v2 plan ON plan.plan_sha = binding.plan_sha
    WHERE binding.task_hash = NEW.task_hash
      AND audit_l2_task_input_valid(
        plan.plan_json, binding.parent_task_hash, NEW.input_id,
        NEW.request_text, NEW.request_sha, NEW.item_ids_json
      ) = 1
      AND NEW.input_id = (
        SELECT task.input_id FROM audit_logical_tasks task
        WHERE task.task_hash = NEW.task_hash
      )
      AND NEW.request_sha = binding.shard_input_sha
      AND NEW.item_ids_json = binding.assigned_item_ids_json
  ) THEN RAISE(ABORT, 'L2 task input mismatch') END;
END;
CREATE TRIGGER audit_task_attempts_budget_reservation_guard
BEFORE INSERT ON audit_task_attempts
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_runtime_budget_reservations_v2 reservation
    WHERE reservation.attempt_id = NEW.attempt_id
      AND reservation.task_hash = NEW.task_hash
  ) THEN RAISE(ABORT, 'attempt lacks durable budget reservation') END;
END;
CREATE TRIGGER audit_runtime_budget_reservations_v2_guard
BEFORE INSERT ON audit_runtime_budget_reservations_v2
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_logical_tasks task
    JOIN audit_task_bindings_v2 binding ON binding.task_hash=task.task_hash
    JOIN audit_l2_plans_v2 plan ON plan.plan_sha=binding.plan_sha
    JOIN audit_l2_task_inputs_v2 task_input ON task_input.task_hash=task.task_hash
    WHERE task.task_hash=NEW.task_hash
      AND plan.plan_sha=NEW.plan_sha
      AND plan.candidate_id=NEW.candidate_id
      AND plan.intent=NEW.intent
      AND audit_l2_budget_reservation_valid(
        plan.plan_json, NEW.candidate_id, NEW.intent,
        NEW.reserved_json, task_input.request_text
      )=1
  ) THEN RAISE(ABORT, 'budget reservation authority mismatch') END;
  SELECT CASE WHEN (
    SELECT count(*) FROM audit_runtime_budget_reservations_v2 prior
    JOIN audit_l2_plans_v2 prior_plan ON prior_plan.plan_sha=prior.plan_sha
    WHERE prior_plan.run_id=(
      SELECT run_id FROM audit_l2_plans_v2 WHERE plan_sha=NEW.plan_sha
    ) AND prior.intent=NEW.intent
  ) + 1 > (
    SELECT audit_l2_budget_limit(plan_json, NEW.intent, 'round', 'started_attempts')
    FROM audit_l2_plans_v2 WHERE plan_sha=NEW.plan_sha
  ) THEN RAISE(ABORT, 'round attempt budget exceeded') END;
  SELECT CASE WHEN (
    SELECT count(*) FROM audit_runtime_budget_reservations_v2 prior
    WHERE prior.candidate_id=NEW.candidate_id AND prior.intent=NEW.intent
  ) + 1 > (
    SELECT audit_l2_budget_limit(plan_json, NEW.intent, 'candidate', 'started_attempts')
    FROM audit_l2_plans_v2 WHERE plan_sha=NEW.plan_sha
  ) THEN RAISE(ABORT, 'candidate attempt budget exceeded') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM json_each(NEW.reserved_json) requested
    WHERE requested.value + COALESCE((
      SELECT sum(audit_l2_budget_effective(
        prior.reserved_json, settlement.usage_verified,
        settlement.actual_json, requested.key
      ))
      FROM audit_runtime_budget_reservations_v2 prior
      JOIN audit_l2_plans_v2 prior_plan ON prior_plan.plan_sha=prior.plan_sha
      LEFT JOIN audit_runtime_budget_settlements_v2 settlement
        ON settlement.attempt_id=prior.attempt_id
      WHERE prior_plan.run_id=(
        SELECT run_id FROM audit_l2_plans_v2 WHERE plan_sha=NEW.plan_sha
      ) AND prior.intent=NEW.intent
    ), 0) > (
      SELECT audit_l2_budget_limit(plan_json, NEW.intent, 'round', requested.key)
      FROM audit_l2_plans_v2 WHERE plan_sha=NEW.plan_sha
    )
  ) THEN RAISE(ABORT, 'round resource budget exceeded') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM json_each(NEW.reserved_json) requested
    WHERE requested.value + COALESCE((
      SELECT sum(audit_l2_budget_effective(
        prior.reserved_json, settlement.usage_verified,
        settlement.actual_json, requested.key
      ))
      FROM audit_runtime_budget_reservations_v2 prior
      LEFT JOIN audit_runtime_budget_settlements_v2 settlement
        ON settlement.attempt_id=prior.attempt_id
      WHERE prior.candidate_id=NEW.candidate_id AND prior.intent=NEW.intent
    ), 0) > (
      SELECT audit_l2_budget_limit(plan_json, NEW.intent, 'candidate', requested.key)
      FROM audit_l2_plans_v2 WHERE plan_sha=NEW.plan_sha
    )
  ) THEN RAISE(ABORT, 'candidate resource budget exceeded') END;
END;
CREATE TRIGGER audit_runtime_budget_settlements_v2_owner_guard
BEFORE INSERT ON audit_runtime_budget_settlements_v2
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_task_attempts attempt
    WHERE attempt.attempt_id = NEW.attempt_id
  ) THEN RAISE(ABORT, 'budget settlement attempt is missing') END;
  SELECT CASE WHEN audit_l2_budget_settlement_valid(
    NEW.usage_verified, NEW.actual_json
  ) <> 1 THEN RAISE(ABORT, 'budget settlement usage is invalid') END;
END;
"""


_L2_RUNTIME_INTEGRITY_SQL = """
CREATE TABLE audit_l2_authority_upgrade_probe(
  value INTEGER NOT NULL CHECK(value = 0)
);
INSERT INTO audit_l2_authority_upgrade_probe(value)
SELECT 1
FROM audit_task_bindings_v2 binding
LEFT JOIN audit_l2_plans_v2 plan ON plan.plan_sha=binding.plan_sha
LEFT JOIN audit_l2_snapshot_records_v2 records
  ON records.snapshot_id=binding.snapshot_id
LEFT JOIN audit_l2_task_inputs_v2 input ON input.task_hash=binding.task_hash
WHERE plan.plan_sha IS NULL
   OR records.snapshot_id IS NULL
   OR input.task_hash IS NULL;
INSERT INTO audit_l2_authority_upgrade_probe(value)
SELECT 1
FROM audit_task_attempts attempt
JOIN audit_task_bindings_v2 binding ON binding.task_hash=attempt.task_hash
LEFT JOIN audit_runtime_budget_reservations_v2 reservation
  ON reservation.attempt_id=attempt.attempt_id
WHERE reservation.attempt_id IS NULL;
INSERT INTO audit_l2_authority_upgrade_probe(value)
SELECT 1
FROM audit_attempt_completions_v2 completion
LEFT JOIN audit_runtime_budget_settlements_v2 settlement
  ON settlement.attempt_id=completion.attempt_id
WHERE settlement.attempt_id IS NULL;
DROP TABLE audit_l2_authority_upgrade_probe;
CREATE TRIGGER audit_task_attempts_capability_authority_guard_v2
BEFORE INSERT ON audit_task_attempts
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_task_bindings_v2 binding
    JOIN audit_l2_plans_v2 plan ON plan.plan_sha=binding.plan_sha
    WHERE binding.task_hash=NEW.task_hash
      AND audit_l2_attempt_capability_valid(
        plan.plan_json, binding.provider_pool_json, NEW.provenance_json
      )=1
  ) THEN RAISE(ABORT, 'attempt capability authority mismatch') END;
END;
"""


_L2_RUNTIME_TASK_AUTHORITY_SQL = """
CREATE VIEW audit_l2_valid_task_authority_v2 AS
SELECT task.task_hash
FROM audit_logical_tasks task
JOIN audit_task_bindings_v2 binding ON binding.task_hash=task.task_hash
JOIN audit_l2_task_inputs_v2 input ON input.task_hash=task.task_hash
JOIN audit_l2_plans_v2 plan ON plan.plan_sha=binding.plan_sha
JOIN audit_l2_snapshot_records_v2 records
  ON records.snapshot_id=binding.snapshot_id
LEFT JOIN audit_logical_tasks parent_task
  ON parent_task.task_hash=binding.parent_task_hash
LEFT JOIN audit_task_bindings_v2 parent_binding
  ON parent_binding.task_hash=binding.parent_task_hash
WHERE audit_l2_binding_authority_valid(
  plan.plan_json, records.records_json,
  json_object(
    'task_hash', task.task_hash, 'run_id', task.run_id,
    'stage', task.stage, 'candidate_id', task.staging_candidate_id,
    'input_id', task.input_id, 'plan_sha', binding.plan_sha,
    'snapshot_id', binding.snapshot_id,
    'snapshot_hash', binding.snapshot_hash,
    'shard_input_sha', binding.shard_input_sha,
    'assigned_json', binding.assigned_item_ids_json,
    'frozen_json', binding.frozen_records_json,
    'pool_json', binding.provider_pool_json,
    'parent_hash', binding.parent_task_hash,
    'split_depth', binding.split_depth,
    'parent_input_id', parent_task.input_id,
    'parent_assigned_json', parent_binding.assigned_item_ids_json,
    'parent_plan_sha', parent_binding.plan_sha,
    'parent_snapshot_id', parent_binding.snapshot_id,
    'parent_candidate_id', parent_task.staging_candidate_id,
    'parent_split_depth', parent_binding.split_depth
  )
)=1
AND audit_l2_input_authority_valid(
  plan.plan_json,
  json_object(
    'task_hash', task.task_hash, 'stage', task.stage,
    'candidate_id', task.staging_candidate_id,
    'input_id', input.input_id, 'plan_sha', binding.plan_sha,
    'parent_hash', binding.parent_task_hash,
    'request_sha', input.request_sha, 'request_text', input.request_text,
    'item_ids_json', input.item_ids_json
  )
)=1;
CREATE TABLE audit_l2_task_authority_upgrade_probe(
  value INTEGER NOT NULL CHECK(value = 0)
);
INSERT INTO audit_l2_task_authority_upgrade_probe(value)
SELECT 1
FROM audit_task_bindings_v2 binding
LEFT JOIN audit_l2_valid_task_authority_v2 valid
  ON valid.task_hash=binding.task_hash
WHERE valid.task_hash IS NULL;
DROP TABLE audit_l2_task_authority_upgrade_probe;
CREATE TRIGGER audit_logical_tasks_l2_insert_authority_guard_v2
BEFORE INSERT ON audit_logical_tasks
WHEN EXISTS (
  SELECT 1 FROM audit_l2_plans_v2 plan WHERE plan.run_id=NEW.run_id
)
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_l2_plans_v2 plan
    WHERE plan.run_id=NEW.run_id
      AND plan.candidate_id=NEW.staging_candidate_id
      AND (
        audit_l2_root_task_valid(
          plan.plan_json, NEW.task_hash, NEW.run_id, NEW.stage,
          NEW.staging_candidate_id, NEW.input_id
        )=1
        OR audit_l2_split_task_insert_allowed(
          NEW.task_hash, NEW.run_id, NEW.stage,
          NEW.staging_candidate_id, NEW.input_id
        )=1
      )
  ) THEN RAISE(ABORT, 'forged task authority') END;
END;
CREATE TRIGGER audit_task_bindings_v2_full_authority_guard
BEFORE INSERT ON audit_task_bindings_v2
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_logical_tasks task
    JOIN audit_l2_plans_v2 plan ON plan.plan_sha=NEW.plan_sha
    JOIN audit_l2_snapshot_records_v2 records
      ON records.snapshot_id=NEW.snapshot_id
    LEFT JOIN audit_logical_tasks parent_task
      ON parent_task.task_hash=NEW.parent_task_hash
    LEFT JOIN audit_task_bindings_v2 parent_binding
      ON parent_binding.task_hash=NEW.parent_task_hash
    WHERE task.task_hash=NEW.task_hash
      AND audit_l2_binding_authority_valid(
        plan.plan_json, records.records_json,
        json_object(
          'task_hash', task.task_hash, 'run_id', task.run_id,
          'stage', task.stage, 'candidate_id', task.staging_candidate_id,
          'input_id', task.input_id, 'plan_sha', NEW.plan_sha,
          'snapshot_id', NEW.snapshot_id,
          'snapshot_hash', NEW.snapshot_hash,
          'shard_input_sha', NEW.shard_input_sha,
          'assigned_json', NEW.assigned_item_ids_json,
          'frozen_json', NEW.frozen_records_json,
          'pool_json', NEW.provider_pool_json,
          'parent_hash', NEW.parent_task_hash,
          'split_depth', NEW.split_depth,
          'parent_input_id', parent_task.input_id,
          'parent_assigned_json', parent_binding.assigned_item_ids_json,
          'parent_plan_sha', parent_binding.plan_sha,
          'parent_snapshot_id', parent_binding.snapshot_id,
          'parent_candidate_id', parent_task.staging_candidate_id,
          'parent_split_depth', parent_binding.split_depth
        )
      )=1
  ) THEN RAISE(ABORT, 'forged task binding authority') END;
END;
CREATE TRIGGER audit_l2_task_inputs_v2_full_authority_guard
BEFORE INSERT ON audit_l2_task_inputs_v2
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_logical_tasks task
    JOIN audit_task_bindings_v2 binding ON binding.task_hash=task.task_hash
    JOIN audit_l2_plans_v2 plan ON plan.plan_sha=binding.plan_sha
    WHERE task.task_hash=NEW.task_hash
      AND audit_l2_input_authority_valid(
        plan.plan_json,
        json_object(
          'task_hash', task.task_hash, 'stage', task.stage,
          'candidate_id', task.staging_candidate_id,
          'input_id', NEW.input_id, 'plan_sha', binding.plan_sha,
          'parent_hash', binding.parent_task_hash,
          'request_sha', NEW.request_sha, 'request_text', NEW.request_text,
          'item_ids_json', NEW.item_ids_json
        )
      )=1
  ) THEN RAISE(ABORT, 'forged task input authority') END;
END;
CREATE TRIGGER audit_task_attempts_full_task_authority_guard
BEFORE INSERT ON audit_task_attempts
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_l2_valid_task_authority_v2 valid
    JOIN audit_task_bindings_v2 binding ON binding.task_hash=valid.task_hash
    JOIN audit_l2_plans_v2 plan ON plan.plan_sha=binding.plan_sha
    WHERE valid.task_hash=NEW.task_hash
      AND audit_l2_attempt_capability_valid(
        plan.plan_json, binding.provider_pool_json, NEW.provenance_json
      )=1
  ) THEN RAISE(ABORT, 'attempt lacks validated task authority') END;
END;
"""


_L2_RUNTIME_SPLIT_AUTHORITY_SQL = """
CREATE TABLE audit_l2_terminal_transition_authority_v2(
  parent_task_hash TEXT PRIMARY KEY REFERENCES audit_logical_tasks(task_hash),
  transition_kind TEXT NOT NULL CHECK(transition_kind IN ('split','exhaust')),
  authority_kind TEXT NOT NULL CHECK(
    authority_kind IN ('claimed-v1','legacy-complete-v1')
  ),
  claim_fence INTEGER CHECK(claim_fence IS NULL OR claim_fence >= 0),
  claim_token TEXT,
  lease_until TEXT,
  child0_task_hash TEXT REFERENCES audit_logical_tasks(task_hash)
    DEFERRABLE INITIALLY DEFERRED,
  child1_task_hash TEXT REFERENCES audit_logical_tasks(task_hash)
    DEFERRABLE INITIALLY DEFERRED,
  authorization_sha256 TEXT NOT NULL UNIQUE CHECK(length(authorization_sha256)=64),
  created_at TEXT NOT NULL,
  CHECK(
    (transition_kind='split' AND child0_task_hash IS NOT NULL
      AND child1_task_hash IS NOT NULL AND child0_task_hash<>child1_task_hash)
    OR
    (transition_kind='exhaust' AND child0_task_hash IS NULL
      AND child1_task_hash IS NULL)
  )
);
INSERT INTO audit_l2_terminal_transition_authority_v2(
  parent_task_hash, transition_kind, authority_kind, claim_fence,
  claim_token, lease_until, child0_task_hash, child1_task_hash,
  authorization_sha256, created_at
)
SELECT task.task_hash, 'split', 'legacy-complete-v1', NULL, NULL, NULL,
       edge0.child_task_hash, edge1.child_task_hash,
       audit_l2_transition_authorization_sha(
         task.task_hash, 'split', NULL, NULL, NULL,
         edge0.child_task_hash, edge1.child_task_hash
       ), terminal.created_at
FROM audit_logical_tasks task
JOIN audit_task_terminal_facts_v2 terminal ON terminal.task_hash=task.task_hash
JOIN audit_task_edges_v2 edge0
  ON edge0.parent_task_hash=task.task_hash AND edge0.position=0
JOIN audit_task_edges_v2 edge1
  ON edge1.parent_task_hash=task.task_hash AND edge1.position=1
WHERE task.state='superseded' AND terminal.terminal_state='superseded';
INSERT INTO audit_l2_terminal_transition_authority_v2(
  parent_task_hash, transition_kind, authority_kind, claim_fence,
  claim_token, lease_until, child0_task_hash, child1_task_hash,
  authorization_sha256, created_at
)
SELECT task.task_hash, 'exhaust', 'legacy-complete-v1', NULL, NULL, NULL,
       NULL, NULL,
       audit_l2_transition_authorization_sha(
         task.task_hash, 'exhaust', NULL, NULL, NULL, NULL, NULL
       ), terminal.created_at
FROM audit_logical_tasks task
JOIN audit_task_terminal_facts_v2 terminal ON terminal.task_hash=task.task_hash
WHERE task.state='exhausted' AND terminal.terminal_state='exhausted';
""" + _immutable_guards(
    "audit_l2_terminal_transition_authority_v2",
) + """
DROP TRIGGER audit_task_attempts_full_task_authority_guard;
DROP TRIGGER audit_logical_tasks_l2_insert_authority_guard_v2;
DROP VIEW audit_l2_valid_task_authority_v2;
CREATE VIEW audit_l2_split_family_facts_v2 AS
SELECT parent.task_hash AS parent_task_hash,
       child0.task_hash AS child0_task_hash,
       child1.task_hash AS child1_task_hash,
       parent_binding.plan_sha AS plan_sha,
       audit_l2_split_family_valid(
         plan.plan_json,
         records.records_json,
         json_object(
           'parent_task_hash', parent.task_hash,
           'parent_state', parent.state,
           'parent_fence', parent.fence,
           'parent_input_id', parent.input_id,
           'parent_assigned_json', parent_binding.assigned_item_ids_json,
           'terminal_state', terminal.terminal_state,
           'terminal_reason', terminal.reason,
           'terminal_sha', terminal.fact_sha256,
           'authority_kind', authority.authority_kind,
           'claim_fence', authority.claim_fence,
           'claim_token', authority.claim_token,
           'lease_until', authority.lease_until,
           'authority_child0', authority.child0_task_hash,
           'authority_child1', authority.child1_task_hash,
           'authority_sha', authority.authorization_sha256,
           'children', json_array(
             json_object(
               'position', 0, 'task_hash', child0.task_hash,
               'run_id', child0.run_id, 'stage', child0.stage,
               'candidate_id', child0.staging_candidate_id,
               'input_id', child0.input_id, 'plan_sha', binding0.plan_sha,
               'snapshot_id', binding0.snapshot_id,
               'snapshot_hash', binding0.snapshot_hash,
               'shard_input_sha', binding0.shard_input_sha,
               'assigned_json', binding0.assigned_item_ids_json,
               'frozen_json', binding0.frozen_records_json,
               'pool_json', binding0.provider_pool_json,
               'parent_hash', binding0.parent_task_hash,
               'split_depth', binding0.split_depth,
               'parent_plan_sha', parent_binding.plan_sha,
               'parent_snapshot_id', parent_binding.snapshot_id,
               'parent_candidate_id', parent.staging_candidate_id,
               'parent_split_depth', parent_binding.split_depth,
               'request_sha', input0.request_sha,
               'request_text', input0.request_text,
               'item_ids_json', input0.item_ids_json,
               'edge_sha', edge0.edge_sha256
             ),
             json_object(
               'position', 1, 'task_hash', child1.task_hash,
               'run_id', child1.run_id, 'stage', child1.stage,
               'candidate_id', child1.staging_candidate_id,
               'input_id', child1.input_id, 'plan_sha', binding1.plan_sha,
               'snapshot_id', binding1.snapshot_id,
               'snapshot_hash', binding1.snapshot_hash,
               'shard_input_sha', binding1.shard_input_sha,
               'assigned_json', binding1.assigned_item_ids_json,
               'frozen_json', binding1.frozen_records_json,
               'pool_json', binding1.provider_pool_json,
               'parent_hash', binding1.parent_task_hash,
               'split_depth', binding1.split_depth,
               'parent_plan_sha', parent_binding.plan_sha,
               'parent_snapshot_id', parent_binding.snapshot_id,
               'parent_candidate_id', parent.staging_candidate_id,
               'parent_split_depth', parent_binding.split_depth,
               'request_sha', input1.request_sha,
               'request_text', input1.request_text,
               'item_ids_json', input1.item_ids_json,
               'edge_sha', edge1.edge_sha256
             )
           )
         )
       ) AS authority_valid
FROM audit_logical_tasks parent
JOIN audit_task_bindings_v2 parent_binding
  ON parent_binding.task_hash=parent.task_hash
JOIN audit_l2_task_inputs_v2 parent_input
  ON parent_input.task_hash=parent.task_hash
JOIN audit_l2_plans_v2 plan ON plan.plan_sha=parent_binding.plan_sha
JOIN audit_l2_snapshot_records_v2 records
  ON records.snapshot_id=parent_binding.snapshot_id
JOIN audit_task_terminal_facts_v2 terminal
  ON terminal.task_hash=parent.task_hash
JOIN audit_l2_terminal_transition_authority_v2 authority
  ON authority.parent_task_hash=parent.task_hash
 AND authority.transition_kind='split'
JOIN audit_task_edges_v2 edge0
  ON edge0.parent_task_hash=parent.task_hash AND edge0.position=0
JOIN audit_task_edges_v2 edge1
  ON edge1.parent_task_hash=parent.task_hash AND edge1.position=1
JOIN audit_logical_tasks child0 ON child0.task_hash=edge0.child_task_hash
JOIN audit_task_bindings_v2 binding0 ON binding0.task_hash=child0.task_hash
JOIN audit_l2_task_inputs_v2 input0 ON input0.task_hash=child0.task_hash
JOIN audit_logical_tasks child1 ON child1.task_hash=edge1.child_task_hash
JOIN audit_task_bindings_v2 binding1 ON binding1.task_hash=child1.task_hash
JOIN audit_l2_task_inputs_v2 input1 ON input1.task_hash=child1.task_hash;
CREATE VIEW audit_l2_valid_split_families_v2 AS
SELECT parent_task_hash, child0_task_hash, child1_task_hash, plan_sha
FROM audit_l2_split_family_facts_v2 WHERE authority_valid=1;
CREATE VIEW audit_l2_valid_task_authority_v2 AS
WITH RECURSIVE valid(task_hash) AS (
  SELECT task.task_hash
  FROM audit_logical_tasks task
  JOIN audit_task_bindings_v2 binding ON binding.task_hash=task.task_hash
  JOIN audit_l2_task_inputs_v2 input ON input.task_hash=task.task_hash
  JOIN audit_l2_plans_v2 plan ON plan.plan_sha=binding.plan_sha
  JOIN audit_l2_snapshot_records_v2 records
    ON records.snapshot_id=binding.snapshot_id
  WHERE binding.parent_task_hash IS NULL
    AND audit_l2_binding_authority_valid(
      plan.plan_json, records.records_json,
      json_object(
        'task_hash', task.task_hash, 'run_id', task.run_id,
        'stage', task.stage, 'candidate_id', task.staging_candidate_id,
        'input_id', task.input_id, 'plan_sha', binding.plan_sha,
        'snapshot_id', binding.snapshot_id,
        'snapshot_hash', binding.snapshot_hash,
        'shard_input_sha', binding.shard_input_sha,
        'assigned_json', binding.assigned_item_ids_json,
        'frozen_json', binding.frozen_records_json,
        'pool_json', binding.provider_pool_json,
        'parent_hash', NULL, 'split_depth', binding.split_depth,
        'parent_input_id', NULL, 'parent_assigned_json', NULL,
        'parent_plan_sha', NULL, 'parent_snapshot_id', NULL,
        'parent_candidate_id', NULL, 'parent_split_depth', NULL
      )
    )=1
    AND audit_l2_input_authority_valid(
      plan.plan_json,
      json_object(
        'task_hash', task.task_hash, 'stage', task.stage,
        'candidate_id', task.staging_candidate_id,
        'input_id', input.input_id, 'plan_sha', binding.plan_sha,
        'parent_hash', NULL, 'request_sha', input.request_sha,
        'request_text', input.request_text,
        'item_ids_json', input.item_ids_json
      )
    )=1
  UNION ALL
  SELECT member.value
  FROM valid parent_valid
  JOIN audit_l2_valid_split_families_v2 family
    ON family.parent_task_hash=parent_valid.task_hash
  JOIN json_each(json_array(
    family.child0_task_hash, family.child1_task_hash
  )) member
)
SELECT DISTINCT task_hash FROM valid;
CREATE VIEW audit_l2_valid_exhaustions_v2 AS
SELECT task.task_hash
FROM audit_l2_valid_task_authority_v2 valid
JOIN audit_logical_tasks task ON task.task_hash=valid.task_hash
JOIN audit_task_bindings_v2 binding ON binding.task_hash=task.task_hash
JOIN audit_task_terminal_facts_v2 terminal ON terminal.task_hash=task.task_hash
JOIN audit_l2_terminal_transition_authority_v2 authority
  ON authority.parent_task_hash=task.task_hash
 AND authority.transition_kind='exhaust'
WHERE task.state='exhausted'
  AND terminal.terminal_state='exhausted'
  AND terminal.fact_sha256=audit_l2_terminal_fact_sha(
    task.task_hash, terminal.terminal_state, terminal.reason
  )
  AND authority.authorization_sha256=audit_l2_transition_authorization_sha(
    task.task_hash, 'exhaust', authority.claim_fence,
    authority.claim_token, authority.lease_until, NULL, NULL
  )
  AND NOT EXISTS (
    SELECT 1 FROM audit_task_edges_v2 edge
    WHERE edge.parent_task_hash=task.task_hash
  )
  AND (
    authority.authority_kind='legacy-complete-v1'
    OR (
      authority.authority_kind='claimed-v1'
      AND authority.claim_fence=task.fence-1
      AND authority.claim_token IS NOT NULL
      AND authority.lease_until IS NOT NULL
      AND (
        terminal.reason<>'single_item_overflow'
        OR (
          json_array_length(binding.assigned_item_ids_json)=1
          AND EXISTS (
            SELECT 1
            FROM audit_task_attempts attempt
            JOIN audit_attempt_completions_v2 completion
              ON completion.attempt_id=attempt.attempt_id
            JOIN audit_cas_objects output
              ON output.object_id=completion.output_cas_object_id
            WHERE attempt.task_hash=task.task_hash
              AND completion.outcome='overflow'
              AND output.integrity_state='verified'
              AND json_extract(attempt.provenance_json, '$.claim_fence')
                    =authority.claim_fence
              AND json_extract(attempt.provenance_json, '$.claim_token')
                    =authority.claim_token
          )
        )
      )
    )
  );
CREATE TABLE audit_l2_split_authority_upgrade_probe(
  value INTEGER NOT NULL CHECK(value=0)
);
INSERT INTO audit_l2_split_authority_upgrade_probe(value)
SELECT 1 FROM audit_task_bindings_v2 binding
LEFT JOIN audit_l2_valid_task_authority_v2 valid
  ON valid.task_hash=binding.task_hash
WHERE valid.task_hash IS NULL;
INSERT INTO audit_l2_split_authority_upgrade_probe(value)
SELECT 1 FROM audit_logical_tasks task
LEFT JOIN audit_l2_valid_split_families_v2 split
  ON split.parent_task_hash=task.task_hash
LEFT JOIN audit_l2_valid_exhaustions_v2 exhausted
  ON exhausted.task_hash=task.task_hash
WHERE (task.state='superseded' AND split.parent_task_hash IS NULL)
   OR (task.state='exhausted' AND exhausted.task_hash IS NULL);
INSERT INTO audit_l2_split_authority_upgrade_probe(value)
SELECT 1 FROM audit_task_terminal_facts_v2 terminal
LEFT JOIN audit_l2_valid_split_families_v2 split
  ON split.parent_task_hash=terminal.task_hash
LEFT JOIN audit_l2_valid_exhaustions_v2 exhausted
  ON exhausted.task_hash=terminal.task_hash
WHERE (terminal.terminal_state='superseded' AND split.parent_task_hash IS NULL)
   OR (terminal.terminal_state='exhausted' AND exhausted.task_hash IS NULL);
INSERT INTO audit_l2_split_authority_upgrade_probe(value)
SELECT 1 FROM audit_task_edges_v2 edge
LEFT JOIN audit_l2_valid_split_families_v2 split
  ON split.parent_task_hash=edge.parent_task_hash
 AND (split.child0_task_hash=edge.child_task_hash
      OR split.child1_task_hash=edge.child_task_hash)
WHERE split.parent_task_hash IS NULL;
INSERT INTO audit_l2_split_authority_upgrade_probe(value)
SELECT 1 FROM audit_l2_terminal_transition_authority_v2 authority
LEFT JOIN audit_l2_valid_split_families_v2 split
  ON split.parent_task_hash=authority.parent_task_hash
LEFT JOIN audit_l2_valid_exhaustions_v2 exhausted
  ON exhausted.task_hash=authority.parent_task_hash
WHERE (authority.transition_kind='split' AND split.parent_task_hash IS NULL)
   OR (authority.transition_kind='exhaust' AND exhausted.task_hash IS NULL);
DROP TABLE audit_l2_split_authority_upgrade_probe;
DROP TRIGGER IF EXISTS audit_logical_tasks_fenced_update;
CREATE TRIGGER audit_logical_tasks_fenced_update
BEFORE UPDATE ON audit_logical_tasks
BEGIN
  SELECT CASE WHEN audit_fenced_cas_allowed()<>1
    THEN RAISE(ABORT, 'logical task update requires fenced CAS') END;
  SELECT CASE WHEN NEW.task_hash<>OLD.task_hash OR NEW.run_id<>OLD.run_id
    OR NEW.stage<>OLD.stage
    OR NEW.staging_candidate_id<>OLD.staging_candidate_id
    OR NEW.input_id<>OLD.input_id OR NEW.created_at<>OLD.created_at
    THEN RAISE(ABORT, 'logical task identity is immutable') END;
  SELECT CASE WHEN NEW.fence<>OLD.fence+1
    THEN RAISE(ABORT, 'logical task fence must increase by one') END;
  SELECT CASE WHEN OLD.state IN ('settled','superseded','exhausted')
    THEN RAISE(ABORT, 'logical task terminal state is closed') END;
  SELECT CASE WHEN NEW.state IN ('superseded','exhausted')
    AND audit_l2_terminal_transition_allowed(
      OLD.task_hash, OLD.state, OLD.fence, OLD.claim_token, OLD.lease_until,
      NEW.state, NEW.fence, NEW.claim_token, NEW.lease_until
    )<>1
    THEN RAISE(ABORT, 'logical task terminal transition lacks authority') END;
END;
CREATE TRIGGER audit_logical_tasks_l2_insert_authority_guard_v2
BEFORE INSERT ON audit_logical_tasks
WHEN EXISTS (SELECT 1 FROM audit_l2_plans_v2 plan WHERE plan.run_id=NEW.run_id)
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_l2_plans_v2 plan
    WHERE plan.run_id=NEW.run_id
      AND plan.candidate_id=NEW.staging_candidate_id
      AND (
        audit_l2_root_task_valid(
          plan.plan_json, NEW.task_hash, NEW.run_id, NEW.stage,
          NEW.staging_candidate_id, NEW.input_id
        )=1
        OR audit_l2_split_task_insert_allowed(
          NEW.task_hash, NEW.run_id, NEW.stage,
          NEW.staging_candidate_id, NEW.input_id
        )=1
      )
  ) THEN RAISE(ABORT, 'forged task authority') END;
END;
CREATE TRIGGER audit_task_terminal_facts_v2_authority_guard
BEFORE INSERT ON audit_task_terminal_facts_v2
BEGIN
  SELECT CASE WHEN audit_l2_terminal_fact_insert_allowed(
    NEW.task_hash, NEW.terminal_state, NEW.reason,
    NEW.fact_sha256, NEW.created_at
  )<>1 THEN RAISE(ABORT, 'terminal fact lacks transition authority') END;
END;
CREATE TRIGGER audit_task_edges_v2_authority_guard
BEFORE INSERT ON audit_task_edges_v2
BEGIN
  SELECT CASE WHEN audit_l2_edge_insert_allowed(
    NEW.parent_task_hash, NEW.child_task_hash, NEW.position,
    NEW.edge_sha256, NEW.created_at
  )<>1 THEN RAISE(ABORT, 'split edge lacks transition authority') END;
END;
CREATE TRIGGER audit_l2_terminal_transition_authority_v2_insert_guard
BEFORE INSERT ON audit_l2_terminal_transition_authority_v2
BEGIN
  SELECT CASE WHEN audit_l2_transition_authority_insert_allowed(
    NEW.parent_task_hash, NEW.transition_kind, NEW.authority_kind,
    NEW.claim_fence, NEW.claim_token, NEW.lease_until,
    NEW.child0_task_hash, NEW.child1_task_hash,
    NEW.authorization_sha256, NEW.created_at
  )<>1 THEN RAISE(ABORT, 'terminal authorization context is invalid') END;
END;
CREATE TRIGGER audit_task_attempts_full_task_authority_guard
BEFORE INSERT ON audit_task_attempts
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_l2_valid_task_authority_v2 valid
    JOIN audit_task_bindings_v2 binding ON binding.task_hash=valid.task_hash
    JOIN audit_l2_plans_v2 plan ON plan.plan_sha=binding.plan_sha
    WHERE valid.task_hash=NEW.task_hash
      AND audit_l2_attempt_capability_valid(
        plan.plan_json, binding.provider_pool_json, NEW.provenance_json
      )=1
  ) THEN RAISE(ABORT, 'attempt lacks validated task authority') END;
END;
"""


_L2_RUNTIME_SPLIT_INPUT_AUTHORITY_SQL = """
DROP TRIGGER audit_task_attempts_full_task_authority_guard;
DROP TRIGGER audit_l2_task_inputs_v2_full_authority_guard;
DROP VIEW audit_l2_valid_task_authority_v2;
DROP VIEW audit_l2_valid_split_families_v2;
DROP VIEW audit_l2_split_family_facts_v2;
CREATE VIEW audit_l2_split_family_facts_v2 AS
SELECT parent.task_hash AS parent_task_hash,
       child0.task_hash AS child0_task_hash,
       child1.task_hash AS child1_task_hash,
       parent_binding.plan_sha AS plan_sha,
       audit_l2_split_family_valid(
         plan.plan_json,
         records.records_json,
         json_object(
           'parent_task_hash', parent.task_hash,
           'parent_state', parent.state,
           'parent_fence', parent.fence,
           'parent_input_id', parent.input_id,
           'parent_stored_input_id', parent_input.input_id,
           'parent_assigned_json', parent_binding.assigned_item_ids_json,
           'terminal_state', terminal.terminal_state,
           'terminal_reason', terminal.reason,
           'terminal_sha', terminal.fact_sha256,
           'authority_kind', authority.authority_kind,
           'claim_fence', authority.claim_fence,
           'claim_token', authority.claim_token,
           'lease_until', authority.lease_until,
           'authority_child0', authority.child0_task_hash,
           'authority_child1', authority.child1_task_hash,
           'authority_sha', authority.authorization_sha256,
           'children', json_array(
             json_object(
               'position', 0, 'task_hash', child0.task_hash,
               'run_id', child0.run_id, 'stage', child0.stage,
               'candidate_id', child0.staging_candidate_id,
               'input_id', child0.input_id,
               'stored_input_id', input0.input_id,
               'plan_sha', binding0.plan_sha,
               'snapshot_id', binding0.snapshot_id,
               'snapshot_hash', binding0.snapshot_hash,
               'shard_input_sha', binding0.shard_input_sha,
               'assigned_json', binding0.assigned_item_ids_json,
               'frozen_json', binding0.frozen_records_json,
               'pool_json', binding0.provider_pool_json,
               'parent_hash', binding0.parent_task_hash,
               'split_depth', binding0.split_depth,
               'parent_plan_sha', parent_binding.plan_sha,
               'parent_snapshot_id', parent_binding.snapshot_id,
               'parent_candidate_id', parent.staging_candidate_id,
               'parent_split_depth', parent_binding.split_depth,
               'request_sha', input0.request_sha,
               'request_text', input0.request_text,
               'item_ids_json', input0.item_ids_json,
               'edge_sha', edge0.edge_sha256
             ),
             json_object(
               'position', 1, 'task_hash', child1.task_hash,
               'run_id', child1.run_id, 'stage', child1.stage,
               'candidate_id', child1.staging_candidate_id,
               'input_id', child1.input_id,
               'stored_input_id', input1.input_id,
               'plan_sha', binding1.plan_sha,
               'snapshot_id', binding1.snapshot_id,
               'snapshot_hash', binding1.snapshot_hash,
               'shard_input_sha', binding1.shard_input_sha,
               'assigned_json', binding1.assigned_item_ids_json,
               'frozen_json', binding1.frozen_records_json,
               'pool_json', binding1.provider_pool_json,
               'parent_hash', binding1.parent_task_hash,
               'split_depth', binding1.split_depth,
               'parent_plan_sha', parent_binding.plan_sha,
               'parent_snapshot_id', parent_binding.snapshot_id,
               'parent_candidate_id', parent.staging_candidate_id,
               'parent_split_depth', parent_binding.split_depth,
               'request_sha', input1.request_sha,
               'request_text', input1.request_text,
               'item_ids_json', input1.item_ids_json,
               'edge_sha', edge1.edge_sha256
             )
           )
         )
       ) AS authority_valid
FROM audit_logical_tasks parent
JOIN audit_task_bindings_v2 parent_binding
  ON parent_binding.task_hash=parent.task_hash
JOIN audit_l2_task_inputs_v2 parent_input
  ON parent_input.task_hash=parent.task_hash
JOIN audit_l2_plans_v2 plan ON plan.plan_sha=parent_binding.plan_sha
JOIN audit_l2_snapshot_records_v2 records
  ON records.snapshot_id=parent_binding.snapshot_id
JOIN audit_task_terminal_facts_v2 terminal
  ON terminal.task_hash=parent.task_hash
JOIN audit_l2_terminal_transition_authority_v2 authority
  ON authority.parent_task_hash=parent.task_hash
 AND authority.transition_kind='split'
JOIN audit_task_edges_v2 edge0
  ON edge0.parent_task_hash=parent.task_hash AND edge0.position=0
JOIN audit_task_edges_v2 edge1
  ON edge1.parent_task_hash=parent.task_hash AND edge1.position=1
JOIN audit_logical_tasks child0 ON child0.task_hash=edge0.child_task_hash
JOIN audit_task_bindings_v2 binding0 ON binding0.task_hash=child0.task_hash
JOIN audit_l2_task_inputs_v2 input0 ON input0.task_hash=child0.task_hash
JOIN audit_logical_tasks child1 ON child1.task_hash=edge1.child_task_hash
JOIN audit_task_bindings_v2 binding1 ON binding1.task_hash=child1.task_hash
JOIN audit_l2_task_inputs_v2 input1 ON input1.task_hash=child1.task_hash;
CREATE VIEW audit_l2_valid_split_families_v2 AS
SELECT parent_task_hash, child0_task_hash, child1_task_hash, plan_sha
FROM audit_l2_split_family_facts_v2 WHERE authority_valid=1;
CREATE VIEW audit_l2_valid_task_authority_v2 AS
WITH RECURSIVE valid(task_hash) AS (
  SELECT task.task_hash
  FROM audit_logical_tasks task
  JOIN audit_task_bindings_v2 binding ON binding.task_hash=task.task_hash
  JOIN audit_l2_task_inputs_v2 input ON input.task_hash=task.task_hash
  JOIN audit_l2_plans_v2 plan ON plan.plan_sha=binding.plan_sha
  JOIN audit_l2_snapshot_records_v2 records
    ON records.snapshot_id=binding.snapshot_id
  WHERE binding.parent_task_hash IS NULL
    AND audit_l2_binding_authority_valid(
      plan.plan_json, records.records_json,
      json_object(
        'task_hash', task.task_hash, 'run_id', task.run_id,
        'stage', task.stage, 'candidate_id', task.staging_candidate_id,
        'input_id', task.input_id, 'plan_sha', binding.plan_sha,
        'snapshot_id', binding.snapshot_id,
        'snapshot_hash', binding.snapshot_hash,
        'shard_input_sha', binding.shard_input_sha,
        'assigned_json', binding.assigned_item_ids_json,
        'frozen_json', binding.frozen_records_json,
        'pool_json', binding.provider_pool_json,
        'parent_hash', NULL, 'split_depth', binding.split_depth,
        'parent_input_id', NULL, 'parent_assigned_json', NULL,
        'parent_plan_sha', NULL, 'parent_snapshot_id', NULL,
        'parent_candidate_id', NULL, 'parent_split_depth', NULL
      )
    )=1
    AND audit_l2_input_authority_valid(
      plan.plan_json,
      json_object(
        'task_hash', task.task_hash, 'stage', task.stage,
        'candidate_id', task.staging_candidate_id,
        'input_id', input.input_id, 'task_input_id', task.input_id,
        'parent_input_id', NULL, 'plan_sha', binding.plan_sha,
        'parent_hash', NULL, 'request_sha', input.request_sha,
        'request_text', input.request_text,
        'item_ids_json', input.item_ids_json
      )
    )=1
  UNION ALL
  SELECT member.value
  FROM valid parent_valid
  JOIN audit_l2_valid_split_families_v2 family
    ON family.parent_task_hash=parent_valid.task_hash
  JOIN json_each(json_array(
    family.child0_task_hash, family.child1_task_hash
  )) member
)
SELECT DISTINCT task_hash FROM valid;
CREATE TABLE audit_l2_split_input_authority_upgrade_probe(
  value INTEGER NOT NULL CHECK(value=0)
);
INSERT INTO audit_l2_split_input_authority_upgrade_probe(value)
SELECT 1 FROM audit_task_bindings_v2 binding
LEFT JOIN audit_l2_valid_task_authority_v2 valid
  ON valid.task_hash=binding.task_hash
WHERE valid.task_hash IS NULL;
INSERT INTO audit_l2_split_input_authority_upgrade_probe(value)
SELECT 1 FROM audit_logical_tasks task
LEFT JOIN audit_l2_valid_split_families_v2 split
  ON split.parent_task_hash=task.task_hash
LEFT JOIN audit_l2_valid_exhaustions_v2 exhausted
  ON exhausted.task_hash=task.task_hash
WHERE (task.state='superseded' AND split.parent_task_hash IS NULL)
   OR (task.state='exhausted' AND exhausted.task_hash IS NULL);
INSERT INTO audit_l2_split_input_authority_upgrade_probe(value)
SELECT 1 FROM audit_task_terminal_facts_v2 terminal
LEFT JOIN audit_l2_valid_split_families_v2 split
  ON split.parent_task_hash=terminal.task_hash
LEFT JOIN audit_l2_valid_exhaustions_v2 exhausted
  ON exhausted.task_hash=terminal.task_hash
WHERE (terminal.terminal_state='superseded' AND split.parent_task_hash IS NULL)
   OR (terminal.terminal_state='exhausted' AND exhausted.task_hash IS NULL);
INSERT INTO audit_l2_split_input_authority_upgrade_probe(value)
SELECT 1 FROM audit_task_edges_v2 edge
LEFT JOIN audit_l2_valid_split_families_v2 split
  ON split.parent_task_hash=edge.parent_task_hash
 AND (split.child0_task_hash=edge.child_task_hash
      OR split.child1_task_hash=edge.child_task_hash)
WHERE split.parent_task_hash IS NULL;
INSERT INTO audit_l2_split_input_authority_upgrade_probe(value)
SELECT 1 FROM audit_l2_terminal_transition_authority_v2 authority
LEFT JOIN audit_l2_valid_split_families_v2 split
  ON split.parent_task_hash=authority.parent_task_hash
LEFT JOIN audit_l2_valid_exhaustions_v2 exhausted
  ON exhausted.task_hash=authority.parent_task_hash
WHERE (authority.transition_kind='split' AND split.parent_task_hash IS NULL)
   OR (authority.transition_kind='exhaust' AND exhausted.task_hash IS NULL);
DROP TABLE audit_l2_split_input_authority_upgrade_probe;
CREATE TRIGGER audit_l2_task_inputs_v2_full_authority_guard
BEFORE INSERT ON audit_l2_task_inputs_v2
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_logical_tasks task
    JOIN audit_task_bindings_v2 binding ON binding.task_hash=task.task_hash
    JOIN audit_l2_plans_v2 plan ON plan.plan_sha=binding.plan_sha
    LEFT JOIN audit_logical_tasks parent_task
      ON parent_task.task_hash=binding.parent_task_hash
    WHERE task.task_hash=NEW.task_hash
      AND audit_l2_input_authority_valid(
        plan.plan_json,
        json_object(
          'task_hash', task.task_hash, 'stage', task.stage,
          'candidate_id', task.staging_candidate_id,
          'input_id', NEW.input_id, 'task_input_id', task.input_id,
          'parent_input_id', parent_task.input_id,
          'plan_sha', binding.plan_sha,
          'parent_hash', binding.parent_task_hash,
          'request_sha', NEW.request_sha, 'request_text', NEW.request_text,
          'item_ids_json', NEW.item_ids_json
        )
      )=1
  ) THEN RAISE(ABORT, 'forged task input authority') END;
END;
CREATE TRIGGER audit_task_attempts_full_task_authority_guard
BEFORE INSERT ON audit_task_attempts
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_l2_valid_task_authority_v2 valid
    JOIN audit_task_bindings_v2 binding ON binding.task_hash=valid.task_hash
    JOIN audit_l2_plans_v2 plan ON plan.plan_sha=binding.plan_sha
    WHERE valid.task_hash=NEW.task_hash
      AND audit_l2_attempt_capability_valid(
        plan.plan_json, binding.provider_pool_json, NEW.provenance_json
      )=1
  ) THEN RAISE(ABORT, 'attempt lacks validated task authority') END;
END;
"""


_MIGRATION_LEDGER_GUARD_SQL = """
CREATE TRIGGER audit_schema_migrations_insert_guard
BEFORE INSERT ON audit_schema_migrations
BEGIN
  SELECT CASE WHEN audit_migration_ledger_insert_allowed(
    NEW.component, NEW.version, NEW.migration_sha256, NEW.applied_at
  ) != 1 THEN RAISE(ABORT, 'migration ledger insert lacks host authority') END;
END;
CREATE TRIGGER audit_schema_migrations_immutable_update
BEFORE UPDATE ON audit_schema_migrations
BEGIN
  SELECT RAISE(ABORT, 'migration ledger is immutable');
END;
CREATE TRIGGER audit_schema_migrations_immutable_delete
BEFORE DELETE ON audit_schema_migrations
BEGIN
  SELECT RAISE(ABORT, 'migration ledger is immutable');
END;
"""


_DURABLE_COST_FACTS_SQL = """
CREATE TABLE audit_attempt_launch_facts_v2(
  attempt_id TEXT PRIMARY KEY REFERENCES audit_task_attempts(attempt_id),
  queued_at TEXT NOT NULL,
  queue_latency_ms INTEGER CHECK(queue_latency_ms IS NULL OR queue_latency_ms >= 0),
  fact_sha256 TEXT NOT NULL UNIQUE CHECK(length(fact_sha256) = 64),
  created_at TEXT NOT NULL
);
CREATE TABLE audit_attempt_cost_settlements_v2(
  attempt_id TEXT PRIMARY KEY REFERENCES audit_attempt_launch_facts_v2(attempt_id),
  outcome TEXT NOT NULL CHECK(outcome IN ('success','failed','cancelled')),
  error_class TEXT,
  billing_state TEXT NOT NULL CHECK(billing_state IN ('billable','nonbillable','unknown')),
  usage_source TEXT NOT NULL CHECK(usage_source IN ('verified_actual','reservation')),
  price_source TEXT,
  currency TEXT,
  run_latency_ms INTEGER CHECK(run_latency_ms IS NULL OR run_latency_ms >= 0),
  fact_sha256 TEXT NOT NULL UNIQUE CHECK(length(fact_sha256) = 64),
  completed_at TEXT NOT NULL,
  CHECK((price_source IS NULL) = (currency IS NULL))
);
CREATE TABLE audit_legacy_unaccounted_attempts_v2(
  attempt_id TEXT PRIMARY KEY REFERENCES audit_task_attempts(attempt_id),
  reason TEXT NOT NULL CHECK(reason = 'pre_durable_cost_facts'),
  quarantined_at TEXT NOT NULL
);
""" + _immutable_guards(
    "audit_attempt_launch_facts_v2",
    "audit_attempt_cost_settlements_v2",
    "audit_legacy_unaccounted_attempts_v2",
) + """
CREATE TRIGGER audit_attempt_launch_facts_v2_guard
BEFORE INSERT ON audit_attempt_launch_facts_v2
BEGIN
  SELECT CASE WHEN audit_cost_launch_insert_allowed(
    NEW.attempt_id, NEW.queued_at, NEW.queue_latency_ms,
    NEW.fact_sha256, NEW.created_at
  ) <> 1 THEN RAISE(ABORT, 'attempt launch fact requires host authority') END;
END;
CREATE TRIGGER audit_attempt_cost_settlements_v2_guard
BEFORE INSERT ON audit_attempt_cost_settlements_v2
BEGIN
  SELECT CASE WHEN audit_cost_settlement_insert_allowed(
    NEW.attempt_id, NEW.outcome, NEW.error_class, NEW.billing_state,
    NEW.usage_source, NEW.price_source, NEW.currency,
    NEW.run_latency_ms, NEW.fact_sha256, NEW.completed_at
  ) <> 1 THEN RAISE(ABORT, 'attempt cost settlement requires host authority') END;
END;
INSERT INTO audit_legacy_unaccounted_attempts_v2(
  attempt_id, reason, quarantined_at
)
SELECT attempt_id, 'pre_durable_cost_facts', CURRENT_TIMESTAMP
FROM audit_task_attempts;
"""


_CANDIDATE_ROUTE_FACTS_SQL = """
CREATE UNIQUE INDEX audit_batch_staging_run_candidate_v2
  ON audit_batch_staging(run_id, staging_candidate_id);
CREATE TABLE audit_candidate_route_cohorts_v2(
  run_id TEXT PRIMARY KEY REFERENCES audit_run_manifests(run_id),
  batch_id TEXT NOT NULL,
  intent TEXT NOT NULL,
  candidate_ids_json TEXT NOT NULL,
  risk_policy_json TEXT NOT NULL,
  risk_policy_sha256 TEXT NOT NULL CHECK(length(risk_policy_sha256) = 64),
  risk_slice_policy_json TEXT NOT NULL,
  risk_slice_policy_sha256 TEXT NOT NULL CHECK(length(risk_slice_policy_sha256) = 64),
  cohort_sha256 TEXT NOT NULL UNIQUE CHECK(length(cohort_sha256) = 64),
  created_at TEXT NOT NULL,
  UNIQUE(run_id, cohort_sha256)
);
CREATE TABLE audit_candidate_route_facts_v2(
  run_id TEXT NOT NULL REFERENCES audit_candidate_route_cohorts_v2(run_id),
  candidate_id TEXT NOT NULL REFERENCES audit_batch_staging(staging_candidate_id),
  intent TEXT NOT NULL,
  cohort_sha256 TEXT NOT NULL REFERENCES audit_candidate_route_cohorts_v2(cohort_sha256),
  router_facts_json TEXT NOT NULL,
  risk_slices_json TEXT NOT NULL,
  matched_rule_ids_json TEXT NOT NULL,
  route TEXT NOT NULL CHECK(route IN ('routine','guarded','exhaustive')),
  call_l1_model INTEGER NOT NULL CHECK(call_l1_model IN (0,1)),
  dispatch_allowed INTEGER NOT NULL CHECK(dispatch_allowed IN (0,1)),
  rule_table_sha256 TEXT NOT NULL CHECK(length(rule_table_sha256) = 64),
  risk_policy_version TEXT NOT NULL,
  fact_sha256 TEXT NOT NULL UNIQUE CHECK(length(fact_sha256) = 64),
  created_at TEXT NOT NULL,
  PRIMARY KEY(run_id, candidate_id),
  UNIQUE(run_id, candidate_id, fact_sha256),
  FOREIGN KEY(run_id, cohort_sha256)
    REFERENCES audit_candidate_route_cohorts_v2(run_id, cohort_sha256),
  FOREIGN KEY(run_id, candidate_id)
    REFERENCES audit_batch_staging(run_id, staging_candidate_id)
);
CREATE TABLE audit_candidate_l2_dispatch_facts_v2(
  plan_sha TEXT PRIMARY KEY REFERENCES audit_l2_plans_v2(plan_sha),
  run_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  route_fact_sha256 TEXT NOT NULL REFERENCES audit_candidate_route_facts_v2(fact_sha256),
  dispatch_sha256 TEXT NOT NULL UNIQUE CHECK(length(dispatch_sha256) = 64),
  created_at TEXT NOT NULL,
  FOREIGN KEY(run_id, candidate_id)
    REFERENCES audit_candidate_route_facts_v2(run_id, candidate_id),
  FOREIGN KEY(run_id, candidate_id, route_fact_sha256)
    REFERENCES audit_candidate_route_facts_v2(run_id, candidate_id, fact_sha256)
);
""" + _immutable_guards(
    "audit_candidate_route_cohorts_v2",
    "audit_candidate_route_facts_v2",
    "audit_candidate_l2_dispatch_facts_v2",
) + """
CREATE TRIGGER audit_candidate_route_cohorts_v2_guard
BEFORE INSERT ON audit_candidate_route_cohorts_v2
BEGIN
  SELECT CASE WHEN audit_candidate_cohort_insert_allowed(
    NEW.run_id, NEW.batch_id, NEW.intent, NEW.candidate_ids_json,
    NEW.risk_policy_json, NEW.risk_policy_sha256,
    NEW.risk_slice_policy_json, NEW.risk_slice_policy_sha256,
    NEW.cohort_sha256, NEW.created_at
  ) <> 1 THEN RAISE(ABORT, 'candidate route cohort requires host authority') END;
END;
CREATE TRIGGER audit_candidate_route_facts_v2_guard
BEFORE INSERT ON audit_candidate_route_facts_v2
BEGIN
  SELECT CASE WHEN audit_candidate_route_insert_allowed(
    NEW.run_id, NEW.candidate_id, NEW.intent, NEW.cohort_sha256,
    NEW.router_facts_json, NEW.risk_slices_json, NEW.matched_rule_ids_json,
    NEW.route, NEW.call_l1_model, NEW.dispatch_allowed,
    NEW.rule_table_sha256, NEW.risk_policy_version, NEW.fact_sha256,
    NEW.created_at
  ) <> 1 THEN RAISE(ABORT, 'candidate route fact requires host authority') END;
END;
CREATE TRIGGER audit_candidate_l2_dispatch_facts_v2_guard
BEFORE INSERT ON audit_candidate_l2_dispatch_facts_v2
BEGIN
  SELECT CASE WHEN audit_candidate_dispatch_insert_allowed(
    NEW.plan_sha, NEW.run_id, NEW.candidate_id, NEW.route_fact_sha256,
    NEW.dispatch_sha256, NEW.created_at
  ) <> 1 THEN RAISE(ABORT, 'candidate L2 dispatch requires host authority') END;
END;
"""


MIGRATIONS = (
    Migration("migration-ledger", 1, _LEDGER_SQL),
    Migration("migration-ledger-guard", 1, _MIGRATION_LEDGER_GUARD_SQL),
    Migration("identity", 1, _IDENTITY_SQL),
    Migration("cas-foundation", 1, _CAS_SQL),
    Migration("execution", 1, _EXECUTION_SQL),
    Migration("receipts", 1, _RECEIPT_SQL),
    Migration("metadata", 1, _METADATA_SQL),
    Migration("metadata-shadow", 1, _METADATA_SHADOW_SQL),
    Migration(
        "metadata-shadow-lifecycle", 1, _METADATA_SHADOW_LIFECYCLE_SQL
    ),
    Migration(
        "metadata-shadow-integrity", 1, _METADATA_SHADOW_INTEGRITY_SQL
    ),
    Migration("semantic-qualification", 1, _SEMANTIC_SQL),
    Migration("semantic-release", 1, _SEMANTIC_RELEASE_SQL),
    Migration("integrity-guards", 1, _INTEGRITY_GUARDS_SQL),
    Migration(
        "coverage-integrity-guards", 1, _COVERAGE_INTEGRITY_GUARDS_SQL
    ),
    Migration("l1-frozen-identity", 1, _L1_FROZEN_IDENTITY_SQL),
    Migration(
        "l1-pair-snapshot-ownership", 1, _PAIR_SNAPSHOT_OWNERSHIP_SQL
    ),
    Migration(
        "l1-snapshot-batch-membership", 1,
        _SNAPSHOT_BATCH_MEMBERSHIP_SQL,
    ),
    Migration(
        "l1-strict-pair-completion", 1,
        _STRICT_PAIR_COMPLETION_SQL,
    ),
    Migration("l2-runtime", 1, _L2_RUNTIME_SQL),
    Migration("l2-runtime-authority", 1, _L2_RUNTIME_AUTHORITY_SQL),
    Migration("l2-runtime-integrity", 1, _L2_RUNTIME_INTEGRITY_SQL),
    Migration(
        "l2-runtime-task-authority", 1, _L2_RUNTIME_TASK_AUTHORITY_SQL
    ),
    Migration(
        "l2-runtime-split-authority", 1, _L2_RUNTIME_SPLIT_AUTHORITY_SQL
    ),
    Migration(
        "l2-runtime-split-input-authority", 1,
        _L2_RUNTIME_SPLIT_INPUT_AUTHORITY_SQL,
    ),
    Migration(
        "semantic-release-authorization", 1,
        _SEMANTIC_RELEASE_AUTHORIZATION_SQL,
    ),
    Migration("durable-cost-facts", 1, _DURABLE_COST_FACTS_SQL),
    Migration("candidate-route-facts", 1, _CANDIDATE_ROUTE_FACTS_SQL),
)


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _current_batch_ids_sha(member_ids_json):
    if not isinstance(member_ids_json, str):
        raise ValueError("snapshot batch member set must be JSON text")
    try:
        values = json.loads(member_ids_json)
    except (TypeError, ValueError) as exc:
        raise ValueError("snapshot batch member set is invalid JSON") from exc
    if (
        not isinstance(values, list)
        or not values
        or values != sorted(values)
        or len(set(values)) != len(values)
        or any(
            not isinstance(value, str)
            or re.fullmatch(r"stg-v2-[0-9a-f]{64}", value) is None
            for value in values
        )
        or json.dumps(
            values, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        != member_ids_json
    ):
        raise ValueError("snapshot batch member set is not canonical")
    return history_contract_v2.ordered_set_sha256(
        "history-current-batch-ids-v2", values
    )


def _metadata_timestamp(value):
    if not isinstance(value, str) or not value:
        raise ValueError("metadata transition timestamp is required")
    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("metadata transition timestamp needs timezone")
    return parsed.astimezone(datetime.timezone.utc)


def _metadata_lease_live(lease_until, now):
    try:
        return 1 if _metadata_timestamp(lease_until) > _metadata_timestamp(now) else 0
    except (TypeError, ValueError):
        return 0


def _metadata_lease_expired(lease_until, now):
    try:
        return 1 if _metadata_timestamp(lease_until) <= _metadata_timestamp(now) else 0
    except (TypeError, ValueError):
        return 0


def _metadata_annotation_ids_sha(annotation_ids_json):
    try:
        values = json.loads(annotation_ids_json)
    except (TypeError, ValueError) as exc:
        raise ValueError("metadata annotation set is invalid JSON") from exc
    if (
        not isinstance(values, list)
        or len(set(values)) != len(values)
        or any(
            not isinstance(value, str)
            or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in values
        )
        or history_contract_v2.canonical_bytes(values).decode("utf-8")
        != annotation_ids_json
    ):
        raise ValueError("metadata annotation set is not canonical")
    return history_contract_v2.framed_sha256(
        "history-metadata-annotation-set-v1",
        history_contract_v2.canonical_bytes(values),
    )


def _closed_json(text):
    if not isinstance(text, str):
        raise ValueError("canonical JSON must be text")

    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    value = json.loads(text, object_pairs_hook=pairs)
    if history_contract_v2.canonical_bytes(value).decode("utf-8") != text:
        raise ValueError("JSON is not canonical")
    return value


def _l2_plan_valid(
    plan_json, plan_sha, run_id, candidate_id, candidate_hash,
    snapshot_id, snapshot_hash, shard_plan_sha, budget_policy_sha, intent,
):
    try:
        material = _closed_json(plan_json)
        normalized = history_audit_plan.validate_runtime_plan_material(material)
        return 1 if (
            history_audit_plan.runtime_plan_sha_from_material(normalized) == plan_sha
            and normalized["run_id"] == run_id
            and normalized["candidate"]["candidate_id"] == candidate_id
            and normalized["candidate"]["candidate_hash"] == candidate_hash
            and normalized["snapshot"]["snapshot_id"] == snapshot_id
            and normalized["snapshot"]["snapshot_hash"] == snapshot_hash
            and normalized["shard_plan_sha"] == shard_plan_sha
            and normalized["budget_policy_sha"] == budget_policy_sha
            and normalized["intent"] == intent
        ) else 0
    except (ValueError, TypeError, history_audit_plan.AuditPlanError):
        return 0


def _l2_records_valid(records_json, records_sha, expected_asset_ids_hash):
    try:
        records = history_audit_plan.runtime_snapshot_records(
            _closed_json(records_json)
        )
        asset_ids = [item["item_id"] for item in records]
        return 1 if (
            history_audit_plan.runtime_snapshot_records_sha(records) == records_sha
            and history_contract_v2.ordered_set_sha256(
                "history-snapshot-assets-v2", asset_ids
            ) == expected_asset_ids_hash
        ) else 0
    except (ValueError, TypeError, history_audit_plan.AuditPlanError):
        return 0


def _l2_task_input_valid(
    plan_json, parent_task_hash, input_id, request_text, request_sha, item_ids_json
):
    try:
        material = _closed_json(plan_json)
        item_ids = _closed_json(item_ids_json)
        if (
            not isinstance(input_id, str)
            or not isinstance(request_text, str)
            or hashlib.sha256(request_text.encode("utf-8")).hexdigest() != request_sha
        ):
            return 0
        if parent_task_hash is None:
            matches = [
                shard for shard in material["shards"]
                if shard["shard_id"] == input_id
            ]
            return 1 if len(matches) == 1 and (
                matches[0]["serialized_request"] == request_text
                and matches[0]["request_sha256"] == request_sha
                and matches[0]["item_ids"] == item_ids
            ) else 0
        request = _closed_json(request_text)
        try:
            position = int(input_id.rsplit(".", 1)[1])
        except (IndexError, ValueError):
            return 0
        return 1 if request == {
            "parent_task_hash": parent_task_hash,
            "position": position,
            "item_ids": item_ids,
        } else 0
    except (ValueError, TypeError, KeyError, history_audit_plan.AuditPlanError):
        return 0


def _l2_budget_reservation_valid(
    plan_json, candidate_id, intent, reserved_json, request_text
):
    try:
        plan = _closed_json(plan_json)
        reserved = _closed_json(reserved_json)
        history_audit_plan._intent_policy(plan["budget_policy"], intent)
        maximum = plan["capacity_profile"]["max_output_tokens"]
        expected_input = len(request_text.encode("utf-8"))
        return 1 if (
            candidate_id == plan["candidate"]["candidate_id"]
            and intent == plan["intent"]
            and type(maximum) is int
            and maximum >= 0
            and reserved == {
                "input_tokens": expected_input,
                "output_tokens": maximum,
                "provider_usage_units": expected_input + maximum,
            }
        ) else 0
    except (ValueError, TypeError, KeyError, history_audit_plan.AuditPlanError):
        return 0


def _l2_budget_limit(plan_json, intent, scope, field):
    try:
        plan = _closed_json(plan_json)
        policy = history_audit_plan._intent_policy(plan["budget_policy"], intent)
        value = policy[scope][field]
        return value if type(value) is int and value >= 0 else -1
    except (ValueError, TypeError, KeyError, history_audit_plan.AuditPlanError):
        return -1


def _l2_budget_effective(reserved_json, usage_verified, actual_json, field):
    try:
        usage = _closed_json(actual_json) if usage_verified == 1 else _closed_json(reserved_json)
        value = usage.get(field, 0)
        return value if type(value) is int and value >= 0 else 2 ** 63 - 1
    except (ValueError, TypeError, KeyError):
        return 2 ** 63 - 1


def _l2_budget_settlement_valid(usage_verified, actual_json):
    try:
        if usage_verified == 0:
            return 1 if actual_json is None else 0
        usage = _closed_json(actual_json)
        required = {"input_tokens", "output_tokens", "provider_usage_units"}
        return 1 if (
            usage_verified == 1
            and required.issubset(usage)
            and not set(usage).difference(
                required | {"cache_tokens", "currency_micros"}
            )
            and all(type(value) is int and value >= 0 for value in usage.values())
        ) else 0
    except (ValueError, TypeError):
        return 0


def _l2_attempt_capability_valid(plan_json, provider_pool_json, provenance_json):
    try:
        plan = history_audit_plan.validate_runtime_plan_material(
            _closed_json(plan_json)
        )
        pool = _closed_json(provider_pool_json)
        provenance = _closed_json(provenance_json)
        capability_fields = {
            "provider", "capability_profile_hash", "model_identity",
            "reasoning_identity", "model_default", "reasoning_default",
            "executable", "cli_revision",
        }
        runtime_fields = {
            "attempt_kind", "ordinal", "claim_token", "claim_fence"
        }
        if (
            not isinstance(pool, list)
            or not pool
            or set(provenance) != capability_fields | runtime_fields
            or provenance["provider"] not in pool
            or provenance["attempt_kind"] not in {
                "initial", "retry", "failover", "split", "detail", "reduce", "cancel"
            }
            or type(provenance["ordinal"]) is not int
            or provenance["ordinal"] < 0
            or not isinstance(provenance["claim_token"], str)
            or not provenance["claim_token"]
            or type(provenance["claim_fence"]) is not int
            or provenance["claim_fence"] < 0
        ):
            return 0
        expected_provider = (
            pool[min(provenance["ordinal"], len(pool) - 1)]
            if provenance["attempt_kind"] == "failover"
            else pool[0]
        )
        bound = plan["provider_capabilities"].get(expected_provider)
        return 1 if (
            provenance["provider"] == expected_provider
            and bound is not None
            and all(provenance[field] == bound[field] for field in capability_fields)
        ) else 0
    except (ValueError, TypeError, KeyError, history_audit_plan.AuditPlanError):
        return 0


def _l2_root_task_valid(
    plan_json, task_hash, run_id, stage, candidate_id, input_id
):
    try:
        plan = history_audit_plan.validate_runtime_plan_material(
            _closed_json(plan_json)
        )
        matches = [
            shard for shard in plan["shards"] if shard["shard_id"] == input_id
        ]
        plan_sha = history_audit_plan.runtime_plan_sha_from_material(plan)
        return 1 if (
            run_id == plan["run_id"]
            and stage == "map"
            and candidate_id == plan["candidate"]["candidate_id"]
            and len(matches) == 1
            and task_hash == history_contract_v2.logical_task_key(
                plan_sha, stage, candidate_id, matches[0]["request_sha256"]
            )
        ) else 0
    except (
        ValueError, TypeError, KeyError,
        history_audit_plan.AuditPlanError,
        history_contract_v2.ContractV2Error,
    ):
        return 0


def _l2_binding_authority_valid(plan_json, records_json, facts_json):
    try:
        plan = history_audit_plan.validate_runtime_plan_material(
            _closed_json(plan_json)
        )
        records = history_audit_plan.runtime_snapshot_records(
            _closed_json(records_json)
        )
        facts = json.loads(facts_json)
        item_ids = _closed_json(facts["assigned_json"])
        frozen = history_audit_plan.runtime_snapshot_records(
            _closed_json(facts["frozen_json"])
        )
        pool = _closed_json(facts["pool_json"])
        plan_sha = history_audit_plan.runtime_plan_sha_from_material(plan)
        record_by_id = {record["item_id"]: record for record in records}
        if (
            facts["run_id"] != plan["run_id"]
            or facts["stage"] != "map"
            or facts["candidate_id"] != plan["candidate"]["candidate_id"]
            or facts["plan_sha"] != plan_sha
            or facts["snapshot_id"] != plan["snapshot"]["snapshot_id"]
            or facts["snapshot_hash"] != plan["snapshot"]["snapshot_hash"]
            or pool != plan["provider_pools_ordered"]["map"]
            or not isinstance(item_ids, list)
            or not item_ids
            or item_ids != sorted(item_ids)
            or len(set(item_ids)) != len(item_ids)
            or any(item_id not in record_by_id for item_id in item_ids)
            or frozen != [record_by_id[item_id] for item_id in item_ids]
        ):
            return 0
        if facts["parent_hash"] is None:
            matches = [
                shard for shard in plan["shards"]
                if shard["shard_id"] == facts["input_id"]
            ]
            if len(matches) != 1 or facts["split_depth"] != 0:
                return 0
            shard = matches[0]
            expected_ids = shard["item_ids"]
            request_sha = shard["request_sha256"]
        else:
            if (
                facts["parent_plan_sha"] != plan_sha
                or facts["parent_snapshot_id"] != facts["snapshot_id"]
                or facts["parent_candidate_id"] != facts["candidate_id"]
                or type(facts["parent_split_depth"]) is not int
                or facts["split_depth"] != facts["parent_split_depth"] + 1
            ):
                return 0
            parent_ids = _closed_json(facts["parent_assigned_json"])
            if (
                not isinstance(parent_ids, list)
                or len(parent_ids) < 2
                or not isinstance(facts["parent_input_id"], str)
            ):
                return 0
            try:
                position = int(facts["input_id"].rsplit(".", 1)[1])
            except (IndexError, ValueError):
                return 0
            if (
                position not in (0, 1)
                or facts["input_id"] != facts["parent_input_id"] + f".{position}"
            ):
                return 0
            midpoint = len(parent_ids) // 2
            groups = (parent_ids[:midpoint], parent_ids[midpoint:])
            expected_ids = groups[position]
            request_sha = hashlib.sha256(
                history_contract_v2.canonical_bytes(
                    {
                        "parent_task_hash": facts["parent_hash"],
                        "position": position,
                        "item_ids": expected_ids,
                    }
                )
            ).hexdigest()
        return 1 if (
            item_ids == expected_ids
            and facts["shard_input_sha"] == request_sha
            and facts["task_hash"] == history_contract_v2.logical_task_key(
                plan_sha, facts["stage"], facts["candidate_id"], request_sha
            )
        ) else 0
    except (
        ValueError, TypeError, KeyError,
        history_audit_plan.AuditPlanError,
        history_contract_v2.ContractV2Error,
    ):
        return 0


def _l2_input_authority_valid(plan_json, facts_json):
    try:
        plan = history_audit_plan.validate_runtime_plan_material(
            _closed_json(plan_json)
        )
        facts = json.loads(facts_json)
        item_ids = _closed_json(facts["item_ids_json"])
        plan_sha = history_audit_plan.runtime_plan_sha_from_material(plan)
        has_task_input_id = "task_input_id" in facts
        has_parent_input_id = "parent_input_id" in facts
        if has_task_input_id != has_parent_input_id:
            return 0
        strict_input_identity = has_task_input_id
        task_input_id = facts.get("task_input_id", facts["input_id"])
        if (
            facts["stage"] != "map"
            or facts["candidate_id"] != plan["candidate"]["candidate_id"]
            or facts["plan_sha"] != plan_sha
            or (
                strict_input_identity
                and facts["input_id"] != task_input_id
            )
            or hashlib.sha256(facts["request_text"].encode("utf-8")).hexdigest()
            != facts["request_sha"]
        ):
            return 0
        if facts["parent_hash"] is None:
            if strict_input_identity and facts["parent_input_id"] is not None:
                return 0
            matches = [
                shard for shard in plan["shards"]
                if shard["shard_id"] == task_input_id
            ]
            valid_request = len(matches) == 1 and (
                matches[0]["serialized_request"] == facts["request_text"]
                and matches[0]["request_sha256"] == facts["request_sha"]
                and matches[0]["item_ids"] == item_ids
            )
        else:
            request_material = _closed_json(facts["request_text"])
            if (
                not isinstance(request_material, dict)
                or set(request_material) != {
                    "parent_task_hash", "position", "item_ids"
                }
                or request_material["parent_task_hash"]
                != facts["parent_hash"]
                or type(request_material["position"]) is not int
                or request_material["position"] not in (0, 1)
                or request_material["item_ids"] != item_ids
            ):
                return 0
            position = request_material["position"]
            if strict_input_identity:
                parent_input_id = facts["parent_input_id"]
                valid_input_id = (
                    isinstance(parent_input_id, str)
                    and parent_input_id
                    and task_input_id == parent_input_id + f".{position}"
                )
            else:
                try:
                    stored_position = int(task_input_id.rsplit(".", 1)[1])
                except (AttributeError, IndexError, ValueError):
                    return 0
                valid_input_id = stored_position == position
            valid_request = (
                valid_input_id
                and facts["request_text"]
                == history_contract_v2.canonical_bytes(
                    request_material
                ).decode("utf-8")
            )
        return 1 if (
            valid_request
            and facts["task_hash"] == history_contract_v2.logical_task_key(
                plan_sha, facts["stage"], facts["candidate_id"],
                facts["request_sha"],
            )
        ) else 0
    except (
        ValueError, TypeError, KeyError,
        history_audit_plan.AuditPlanError,
        history_contract_v2.ContractV2Error,
    ):
        return 0


def _l2_fact_sha(domain, value):
    return history_contract_v2.framed_sha256(
        domain, history_contract_v2.canonical_bytes(value)
    )


def _l2_terminal_fact_sha(task_hash, terminal_state, reason):
    try:
        return _l2_fact_sha(
            "history-task-terminal-v2",
            {
                "task_hash": task_hash,
                "terminal_state": terminal_state,
                "reason": reason,
            },
        )
    except (TypeError, ValueError, history_contract_v2.ContractV2Error):
        return ""


def _l2_edge_sha(parent_task_hash, child_task_hash, position):
    try:
        return _l2_fact_sha(
            "history-task-edge-v2",
            {
                "parent_task_hash": parent_task_hash,
                "child_task_hash": child_task_hash,
                "position": position,
            },
        )
    except (TypeError, ValueError, history_contract_v2.ContractV2Error):
        return ""


def _l2_transition_authorization_sha(
    parent_task_hash, transition_kind, claim_fence, claim_token,
    lease_until, child0_task_hash, child1_task_hash,
):
    try:
        return _l2_fact_sha(
            "history-l2-terminal-transition-authority-v1",
            {
                "parent_task_hash": parent_task_hash,
                "transition_kind": transition_kind,
                "claim_fence": claim_fence,
                "claim_token": claim_token,
                "lease_until": lease_until,
                "child_task_hashes": (
                    [child0_task_hash, child1_task_hash]
                    if transition_kind == "split" else []
                ),
            },
        )
    except (TypeError, ValueError, history_contract_v2.ContractV2Error):
        return ""


def _l2_split_family_valid(plan_json, records_json, facts_json):
    """Validate one whole two-child split family without reading caller state."""
    try:
        facts = json.loads(facts_json)
        parent_ids = _closed_json(facts["parent_assigned_json"])
        children = facts["children"]
        strict_input_identity = "parent_stored_input_id" in facts
        expected_fact_keys = {
            "parent_task_hash", "parent_state", "parent_fence",
            "parent_input_id", "parent_assigned_json", "terminal_state",
            "terminal_reason", "terminal_sha", "authority_kind",
            "claim_fence", "claim_token", "lease_until",
            "authority_child0", "authority_child1", "authority_sha",
            "children",
        }
        if strict_input_identity:
            expected_fact_keys.add("parent_stored_input_id")
        if (
            set(facts) != expected_fact_keys
            or (
                strict_input_identity
                and facts["parent_stored_input_id"]
                != facts["parent_input_id"]
            )
            or facts["parent_state"] != "superseded"
            or facts["terminal_state"] != "superseded"
            or facts["terminal_reason"] != "invalid_parent_split"
            or facts["terminal_sha"] != _l2_terminal_fact_sha(
                facts["parent_task_hash"], "superseded", "invalid_parent_split"
            )
            or not isinstance(parent_ids, list)
            or len(parent_ids) < 2
            or not isinstance(children, list)
            or len(children) != 2
            or [child["position"] for child in children] != [0, 1]
        ):
            return 0
        midpoint = len(parent_ids) // 2
        groups = (parent_ids[:midpoint], parent_ids[midpoint:])
        child_hashes = []
        for position, child in enumerate(children):
            expected_child_keys = {
                "position", "task_hash", "run_id", "stage", "candidate_id",
                "input_id", "plan_sha", "snapshot_id", "snapshot_hash",
                "shard_input_sha", "assigned_json", "frozen_json", "pool_json",
                "parent_hash", "split_depth", "parent_plan_sha",
                "parent_snapshot_id", "parent_candidate_id",
                "parent_split_depth", "request_sha", "request_text",
                "item_ids_json", "edge_sha",
            }
            if strict_input_identity:
                expected_child_keys.add("stored_input_id")
            if set(child) != expected_child_keys:
                return 0
            binding_facts = {
                "task_hash": child["task_hash"],
                "run_id": child["run_id"],
                "stage": child["stage"],
                "candidate_id": child["candidate_id"],
                "input_id": child["input_id"],
                "plan_sha": child["plan_sha"],
                "snapshot_id": child["snapshot_id"],
                "snapshot_hash": child["snapshot_hash"],
                "shard_input_sha": child["shard_input_sha"],
                "assigned_json": child["assigned_json"],
                "frozen_json": child["frozen_json"],
                "pool_json": child["pool_json"],
                "parent_hash": child["parent_hash"],
                "split_depth": child["split_depth"],
                "parent_input_id": facts["parent_input_id"],
                "parent_assigned_json": facts["parent_assigned_json"],
                "parent_plan_sha": child["parent_plan_sha"],
                "parent_snapshot_id": child["parent_snapshot_id"],
                "parent_candidate_id": child["parent_candidate_id"],
                "parent_split_depth": child["parent_split_depth"],
            }
            input_facts = {
                "task_hash": child["task_hash"],
                "stage": child["stage"],
                "candidate_id": child["candidate_id"],
                "input_id": child.get("stored_input_id", child["input_id"]),
                "plan_sha": child["plan_sha"],
                "parent_hash": child["parent_hash"],
                "request_sha": child["request_sha"],
                "request_text": child["request_text"],
                "item_ids_json": child["item_ids_json"],
            }
            if strict_input_identity:
                input_facts.update(
                    {
                        "task_input_id": child["input_id"],
                        "parent_input_id": facts["parent_input_id"],
                    }
                )
            if (
                child["parent_hash"] != facts["parent_task_hash"]
                or (
                    strict_input_identity
                    and (
                        child["stored_input_id"] != child["input_id"]
                        or child["input_id"]
                        != facts["parent_input_id"] + f".{position}"
                    )
                )
                or _closed_json(child["assigned_json"]) != groups[position]
                or _closed_json(child["item_ids_json"]) != groups[position]
                or _l2_binding_authority_valid(
                    plan_json, records_json,
                    json.dumps(binding_facts, sort_keys=True, separators=(",", ":")),
                ) != 1
                or _l2_input_authority_valid(
                    plan_json,
                    json.dumps(input_facts, sort_keys=True, separators=(",", ":")),
                ) != 1
                or child["edge_sha"] != _l2_edge_sha(
                    facts["parent_task_hash"], child["task_hash"], position
                )
            ):
                return 0
            child_hashes.append(child["task_hash"])
        if (
            facts["authority_child0"] != child_hashes[0]
            or facts["authority_child1"] != child_hashes[1]
            or facts["authority_kind"] not in {"claimed-v1", "legacy-complete-v1"}
            or facts["authority_sha"] != _l2_transition_authorization_sha(
                facts["parent_task_hash"], "split", facts["claim_fence"],
                facts["claim_token"], facts["lease_until"],
                child_hashes[0], child_hashes[1],
            )
        ):
            return 0
        if facts["authority_kind"] == "claimed-v1" and (
            facts["claim_fence"] != facts["parent_fence"] - 1
            or not isinstance(facts["claim_token"], str)
            or not facts["claim_token"]
            or not isinstance(facts["lease_until"], str)
        ):
            return 0
        return 1
    except (
        ValueError, TypeError, KeyError, IndexError,
        history_contract_v2.ContractV2Error,
    ):
        return 0


@contextlib.contextmanager
def l2_split_task_insert_guard(
    conn, *, task_hash, run_id, stage, candidate_id, input_id
):
    """Reject the retired caller-selected split-child authorization surface."""
    del conn, task_hash, run_id, stage, candidate_id, input_id
    raise AuditMigrationError(
        "split child authorization is derived by the storage transition"
    )
    yield


def _clear_l2_terminal_transition_guard(guard):
    guard.update(
        active=False,
        expected_children=frozenset(),
        expected_transition=None,
        expected_terminal=None,
        expected_edges=frozenset(),
        expected_authority=None,
    )


@contextlib.contextmanager
def _l2_terminal_transition_guard(
    conn, *, children, transition, terminal, edges, authority
):
    guard = _L2_TERMINAL_TRANSITION_GUARDS.get(id(conn))
    if guard is None or guard["active"] or not conn.in_transaction:
        raise AuditMigrationError("L2 terminal transition guard is unavailable")
    guard.update(
        active=True,
        expected_children=frozenset(children),
        expected_transition=transition,
        expected_terminal=terminal,
        expected_edges=frozenset(edges),
        expected_authority=authority,
    )
    try:
        yield
    finally:
        _clear_l2_terminal_transition_guard(guard)


def _clear_metadata_guard(guard):
    guard.update(
        active=False,
        metadata_operation=None,
        metadata_now=None,
        metadata_outbox_id=None,
        metadata_claim_token=None,
        metadata_claim_fence=None,
    )


def _clear_semantic_release_guard(guard):
    guard.update(
        expected_head_events=frozenset(),
        expected_qualification_binding=None,
        expected_qualification=None,
        expected_qualification_fact=None,
        expected_authorization=None,
        receipt_id=None,
        receipt_material_sha256=None,
        qualification_id=None,
    )


def _semantic_receipt_from_sql(values):
    if len(values) != len(_RELEASE_RECEIPT_FIELDS):
        raise ValueError("receipt SQL tuple is invalid")
    receipt = dict(zip(_RELEASE_RECEIPT_FIELDS, values))
    for field in _RELEASE_JSON_FIELDS:
        raw = receipt[field]
        if not isinstance(raw, str):
            raise ValueError("receipt JSON field is invalid")
        receipt[field] = history_contract_v2.parse_json_bytes(raw.encode("utf-8"))
    for field in _RELEASE_BOOLEAN_FIELDS:
        if receipt[field] not in (0, 1):
            raise ValueError("receipt Boolean field is invalid")
        receipt[field] = bool(receipt[field])
    return history_contract_v2.validate_receipt(receipt)


def _semantic_receipt_insert_allowed(guard, *values):
    try:
        receipt = _semantic_receipt_from_sql(values)
        material_sha = history_contract_v2.framed_sha256(
            "history-semantic-release-receipt-v2",
            history_contract_v2.canonical_bytes(receipt),
        )
        return 1 if (
            guard["receipt_id"] == receipt["minimum_receipt_sha"]
            and guard["receipt_material_sha256"] == material_sha
            and guard["qualification_id"] is not None
        ) else 0
    except (ValueError, TypeError, history_contract_v2.ContractV2Error):
        return 0


@contextlib.contextmanager
def _metadata_transition_guard(
    conn, *, operation, now, outbox_id, claim_token, claim_fence
):
    guard = _FENCE_GUARDS.get(id(conn))
    if guard is None:
        raise AuditMigrationError("fenced CAS is not initialized for connection")
    if guard["active"]:
        raise AuditMigrationError("another fenced transition is active")
    guard.update(
        active=True,
        metadata_operation=operation,
        metadata_now=now,
        metadata_outbox_id=outbox_id,
        metadata_claim_token=claim_token,
        metadata_claim_fence=claim_fence,
    )
    try:
        yield
    finally:
        _clear_metadata_guard(guard)


@contextlib.contextmanager
def metadata_shadow_publish_guard(
    conn, *, outbox_id, claim_token, claim_fence, now
):
    """Authorize one atomic annotation-set publication and terminal settle."""
    if not conn.in_transaction:
        raise AuditMigrationError("metadata publication requires a transaction")
    _metadata_timestamp(now)
    row = conn.execute(
        "SELECT state, fence, claim_token, lease_until "
        "FROM audit_metadata_outbox_v2 WHERE outbox_id=?",
        (outbox_id,),
    ).fetchone()
    if (
        row is None
        or row["state"] != "claimed"
        or row["fence"] != claim_fence
        or row["claim_token"] != claim_token
        or _metadata_lease_live(row["lease_until"], now) != 1
    ):
        raise StaleFence("metadata publish claim is stale")
    with _metadata_transition_guard(
        conn,
        operation="publish",
        now=now,
        outbox_id=outbox_id,
        claim_token=claim_token,
        claim_fence=claim_fence,
    ):
        yield
        settled = conn.execute(
            """
            SELECT 1
            FROM audit_metadata_outbox_v2 work
            JOIN audit_metadata_settlements_v2 settlement
              ON settlement.outbox_id=work.outbox_id
            WHERE work.outbox_id=? AND work.state='done'
              AND settlement.claim_fence=? AND settlement.claim_token=?
            """,
            (outbox_id, claim_fence, claim_token),
        ).fetchone()
        if settled is None:
            raise AuditMigrationError(
                "metadata publication did not atomically settle its annotation set"
            )


def record_metadata_shadow_settlement(
    conn, *, outbox_id, claim_fence, claim_token, annotation_ids, created_at
):
    """Persist the exact annotation set before the guarded terminal transition."""
    annotation_ids_json = history_contract_v2.canonical_bytes(
        annotation_ids
    ).decode("utf-8")
    annotation_ids_sha = _metadata_annotation_ids_sha(annotation_ids_json)
    conn.execute(
        """
        INSERT INTO audit_metadata_settlements_v2(
          outbox_id, claim_fence, claim_token, annotation_ids_json,
          annotation_ids_sha256, annotation_count, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            outbox_id,
            claim_fence,
            claim_token,
            annotation_ids_json,
            annotation_ids_sha,
            len(annotation_ids),
            created_at,
        ),
    )
    return annotation_ids_sha


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
    ledger_guard = _MIGRATION_LEDGER_GUARDS.get(id(conn))
    if ledger_guard is None or ledger_guard["expected"] is not None:
        raise AuditMigrationError("migration ledger guard is unavailable")
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
        applied_at = _utc_now()
        ledger_guard["expected"] = (
            migration.component, migration.version, migration.sha256, applied_at
        )
        try:
            conn.execute(
                "INSERT INTO audit_schema_migrations("
                "component, version, migration_sha256, applied_at) VALUES(?, ?, ?, ?)",
                (
                    migration.component,
                    migration.version,
                    migration.sha256,
                    applied_at,
                ),
            )
        finally:
            ledger_guard["expected"] = None
        conn.execute("COMMIT")
    except Exception as exc:
        ledger_guard["expected"] = None
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if isinstance(exc, AuditMigrationError):
            raise
        raise AuditMigrationError(
            f"migration failed: {migration.component} v{migration.version}"
        ) from exc


def _managed_schema_rows(conn):
    return {
        (row[0], row[1]): (row[2], row[3])
        for row in conn.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE name GLOB 'audit_*' AND sql IS NOT NULL
            ORDER BY type, name
            """
        )
    }


def _expected_managed_schema():
    migration_key = tuple(
        (migration.component, migration.version, migration.sha256)
        for migration in MIGRATIONS
    )
    expected = _EXPECTED_MANAGED_SCHEMA.get(migration_key)
    if expected is not None:
        return expected
    try:
        from lib import history_store
    except ImportError:
        import history_store
    reference = sqlite3.connect(":memory:")
    try:
        history_store.init_schema(reference)
        reference.commit()
        _initialize_schema(reference, verify=False)
        expected = _managed_schema_rows(reference)
    finally:
        reference.close()
    _EXPECTED_MANAGED_SCHEMA[migration_key] = expected
    return expected


def _verify_managed_schema(conn):
    expected = _expected_managed_schema()
    conn.execute("BEGIN IMMEDIATE")
    try:
        observed = _managed_schema_rows(conn)
        missing = sorted(set(expected).difference(observed))
        mismatched = sorted(
            key for key in set(expected).intersection(observed)
            if observed[key] != expected[key]
        )
        if missing or mismatched:
            detail = []
            if missing:
                detail.append("missing=" + ",".join(name for _, name in missing))
            if mismatched:
                detail.append(
                    "mismatched=" + ",".join(name for _, name in mismatched)
                )
            raise AuditMigrationError(
                "managed audit schema postcondition failed: " + " ".join(detail)
            )
        for migration in MIGRATIONS:
            row = conn.execute(
                "SELECT migration_sha256 FROM audit_schema_migrations "
                "WHERE component=? AND version=?",
                (migration.component, migration.version),
            ).fetchone()
            if row is None or row[0] != migration.sha256:
                raise AuditMigrationError(
                    f"migration ledger postcondition failed: "
                    f"{migration.component} v{migration.version}"
                )
        _replay_migration_probes(conn)
        foreign_key_faults = [
            row for row in conn.execute("PRAGMA foreign_key_check").fetchall()
            if isinstance(row[0], str) and row[0].startswith("audit_")
        ]
        if foreign_key_faults:
            raise AuditMigrationError(
                "audit schema foreign-key postcondition failed"
            )
        conn.execute("COMMIT")
    except Exception as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if isinstance(exc, AuditMigrationError):
            raise
        raise AuditMigrationError(
            "audit migration invariant probe failed"
        ) from exc


def _replay_migration_probes(conn):
    pattern = re.compile(
        r"(CREATE TABLE (audit_[A-Za-z0-9_]+_probe)\s*\(.*?"
        r"DROP TABLE \2;)",
        re.DOTALL,
    )
    probe_index = 0
    for migration in MIGRATIONS:
        for match in pattern.finditer(migration.sql):
            probe_index += 1
            savepoint = f"audit_migration_probe_{probe_index}"
            conn.execute("SAVEPOINT " + savepoint)
            try:
                _execute_sql_script(conn, match.group(1))
            finally:
                conn.execute("ROLLBACK TO SAVEPOINT " + savepoint)
                conn.execute("RELEASE SAVEPOINT " + savepoint)


def _initialize_schema(conn, *, verify):
    """Apply every v2 component migration without invoking v1 initialization."""
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3 connection")
    if conn.in_transaction:
        raise AuditMigrationError("v2 migration requires an idle connection")
    guard = {}
    _clear_metadata_guard(guard)
    _FENCE_GUARDS[id(conn)] = guard
    split_guard = {}
    _clear_l2_terminal_transition_guard(split_guard)
    _L2_TASK_INSERT_GUARDS[id(conn)] = split_guard
    _L2_TERMINAL_TRANSITION_GUARDS[id(conn)] = split_guard
    release_guard = {}
    _clear_semantic_release_guard(release_guard)
    _SEMANTIC_RELEASE_GUARDS[id(conn)] = release_guard
    _SEMANTIC_EVALUATION_GUARDS[id(conn)] = {"expected": None}
    cost_guard = {
        "launch": None, "settlement": None, "cohort": None,
        "route": None, "dispatch": None,
    }
    _COST_FACT_GUARDS[id(conn)] = cost_guard
    ledger_guard = {"expected": None}
    _MIGRATION_LEDGER_GUARDS[id(conn)] = ledger_guard
    conn.create_function(
        "audit_migration_ledger_insert_allowed", 4,
        lambda *values: 1 if ledger_guard["expected"] == tuple(values) else 0,
    )
    conn.create_function(
        "audit_cost_launch_insert_allowed", 5,
        lambda *values: 1 if cost_guard["launch"] == tuple(values) else 0,
    )
    conn.create_function(
        "audit_cost_settlement_insert_allowed", 10,
        lambda *values: 1 if cost_guard["settlement"] == tuple(values) else 0,
    )
    conn.create_function(
        "audit_candidate_route_insert_allowed", 14,
        lambda *values: 1 if cost_guard["route"] == tuple(values) else 0,
    )
    conn.create_function(
        "audit_candidate_cohort_insert_allowed", 10,
        lambda *values: 1 if cost_guard["cohort"] == tuple(values) else 0,
    )
    conn.create_function(
        "audit_candidate_dispatch_insert_allowed", 6,
        lambda *values: 1 if cost_guard["dispatch"] == tuple(values) else 0,
    )
    conn.create_function(
        "audit_fenced_cas_allowed", 0, lambda: 1 if guard["active"] else 0
    )
    conn.create_function(
        "audit_current_batch_ids_sha", 1, _current_batch_ids_sha
    )
    conn.create_function(
        "audit_metadata_operation", 0,
        lambda: guard["metadata_operation"],
    )
    conn.create_function(
        "audit_metadata_now", 0, lambda: guard["metadata_now"]
    )
    conn.create_function(
        "audit_metadata_outbox_id", 0,
        lambda: guard["metadata_outbox_id"],
    )
    conn.create_function(
        "audit_metadata_claim_token", 0,
        lambda: guard["metadata_claim_token"],
    )
    conn.create_function(
        "audit_metadata_claim_fence", 0,
        lambda: guard["metadata_claim_fence"],
    )
    conn.create_function(
        "audit_metadata_lease_live", 2, _metadata_lease_live
    )
    conn.create_function(
        "audit_metadata_lease_expired", 2, _metadata_lease_expired
    )
    conn.create_function(
        "audit_metadata_annotation_ids_sha", 1,
        _metadata_annotation_ids_sha,
    )
    conn.create_function("audit_l2_plan_valid", 10, _l2_plan_valid)
    conn.create_function("audit_l2_records_valid", 3, _l2_records_valid)
    conn.create_function("audit_l2_task_input_valid", 6, _l2_task_input_valid)
    conn.create_function(
        "audit_l2_budget_reservation_valid", 5, _l2_budget_reservation_valid
    )
    conn.create_function("audit_l2_budget_limit", 4, _l2_budget_limit)
    conn.create_function("audit_l2_budget_effective", 4, _l2_budget_effective)
    conn.create_function(
        "audit_l2_budget_settlement_valid", 2, _l2_budget_settlement_valid
    )
    conn.create_function(
        "audit_l2_attempt_capability_valid", 3, _l2_attempt_capability_valid
    )
    conn.create_function("audit_l2_root_task_valid", 6, _l2_root_task_valid)
    conn.create_function(
        "audit_l2_binding_authority_valid", 3, _l2_binding_authority_valid
    )
    conn.create_function(
        "audit_l2_input_authority_valid", 2, _l2_input_authority_valid
    )
    conn.create_function(
        "audit_l2_split_family_valid", 3, _l2_split_family_valid
    )
    conn.create_function(
        "audit_l2_terminal_fact_sha", 3, _l2_terminal_fact_sha
    )
    conn.create_function("audit_l2_edge_sha", 3, _l2_edge_sha)
    conn.create_function(
        "audit_l2_transition_authorization_sha", 7,
        _l2_transition_authorization_sha,
    )
    conn.create_function(
        "audit_l2_split_task_insert_allowed", 5,
        lambda *values: 1 if (
            split_guard["active"]
            and tuple(values) in split_guard["expected_children"]
        ) else 0,
    )
    conn.create_function(
        "audit_l2_terminal_transition_allowed", 9,
        lambda *values: 1 if (
            split_guard["active"]
            and split_guard["expected_transition"] == tuple(values)
        ) else 0,
    )
    conn.create_function(
        "audit_l2_terminal_fact_insert_allowed", 5,
        lambda *values: 1 if (
            split_guard["active"]
            and split_guard["expected_terminal"] == tuple(values)
        ) else 0,
    )
    conn.create_function(
        "audit_l2_edge_insert_allowed", 5,
        lambda *values: 1 if (
            split_guard["active"]
            and tuple(values) in split_guard["expected_edges"]
        ) else 0,
    )
    conn.create_function(
        "audit_l2_transition_authority_insert_allowed", 10,
        lambda *values: 1 if (
            split_guard["active"]
            and split_guard["expected_authority"] == tuple(values)
        ) else 0,
    )
    conn.create_function(
        "audit_semantic_head_insert_allowed", 5,
        lambda *values: 1 if (
            tuple(values) in release_guard["expected_head_events"]
        ) else 0,
    )
    conn.create_function(
        "audit_semantic_authorization_insert_allowed", 14,
        lambda *values: 1 if (
            release_guard["expected_authorization"] == tuple(values)
        ) else 0,
    )
    conn.create_function(
        "audit_semantic_qualification_binding_insert_allowed", 3,
        lambda *values: 1 if (
            release_guard["expected_qualification_binding"] == tuple(values)
        ) else 0,
    )
    conn.create_function(
        "audit_semantic_qualification_insert_allowed", 8,
        lambda *values: 1 if (
            release_guard["expected_qualification"] == tuple(values)
        ) else 0,
    )
    conn.create_function(
        "audit_semantic_qualification_fact_insert_allowed", 13,
        lambda *values: 1 if (
            release_guard["expected_qualification_fact"] == tuple(values)
        ) else 0,
    )
    conn.create_function(
        "audit_semantic_receipt_insert_allowed", len(_RELEASE_RECEIPT_FIELDS),
        lambda *values: _semantic_receipt_insert_allowed(release_guard, *values),
    )
    conn.create_function(
        "audit_semantic_receipt_material_sha", 0,
        lambda: release_guard["receipt_material_sha256"],
    )
    conn.create_function(
        "audit_semantic_receipt_qualification_id", 0,
        lambda: release_guard["qualification_id"],
    )
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA recursive_triggers = ON")
    for migration in MIGRATIONS:
        _apply_migration(conn, migration)
    if verify:
        _verify_managed_schema(conn)


def init_schema(conn):
    """Apply and independently verify every managed v2 audit migration."""
    return _initialize_schema(conn, verify=True)


def record_attempt_launch_cost_fact(
    conn, attempt_id, *, queued_at=None, queue_latency_ms=None, created_at=None
):
    if not conn.in_transaction:
        raise AuditMigrationError("attempt launch cost fact requires a transaction")
    authority = conn.execute(
        """
        SELECT attempt.task_hash, attempt.ordinal, attempt.provenance_json,
               attempt.created_at AS started_at,
               task.created_at AS task_ready_at,
               task.state, task.claim_token, task.fence,
               reservation.attempt_kind
        FROM audit_task_attempts attempt
        JOIN audit_logical_tasks task ON task.task_hash=attempt.task_hash
        JOIN audit_runtime_budget_reservations_v2 reservation
          ON reservation.attempt_id=attempt.attempt_id
        WHERE attempt.attempt_id=?
        """,
        (attempt_id,),
    ).fetchone()
    if authority is None:
        raise AuditMigrationError("attempt launch authority is missing")
    try:
        provenance = history_contract_v2.parse_json_bytes(
            authority["provenance_json"].encode("utf-8")
        )
        expected_attempt_id = history_contract_v2.attempt_id(
            authority["task_hash"], authority["ordinal"], provenance
        )
    except history_contract_v2.ContractV2Error as exc:
        raise AuditMigrationError("attempt launch identity is invalid") from exc
    if (
        expected_attempt_id != attempt_id
        or authority["state"] != "claimed"
        or provenance.get("claim_token") != authority["claim_token"]
        or provenance.get("claim_fence") != authority["fence"]
        or provenance.get("attempt_kind") != authority["attempt_kind"]
    ):
        raise AuditMigrationError("attempt launch authority is inconsistent")
    if authority["ordinal"] == 0:
        durable_queued_at = authority["task_ready_at"]
    else:
        prior = conn.execute(
            """
            SELECT terminal.completed_at
            FROM audit_task_attempts attempt
            JOIN audit_attempt_cost_settlements_v2 terminal USING(attempt_id)
            WHERE attempt.task_hash=? AND attempt.ordinal=?
            """,
            (authority["task_hash"], authority["ordinal"] - 1),
        ).fetchone()
        if prior is None:
            raise AuditMigrationError("prior terminal ready time is missing")
        durable_queued_at = prior["completed_at"]
    if queued_at is not None and queued_at != durable_queued_at:
        raise AuditMigrationError("attempt queued time conflicts with durable authority")
    queued_at = durable_queued_at
    started = _semantic_timestamp(authority["started_at"], "started_at")
    ready = _semantic_timestamp(queued_at, "queued_at")
    derived_queue_latency_ms = int((started - ready).total_seconds() * 1000)
    if derived_queue_latency_ms < 0:
        raise AuditMigrationError("attempt launch precedes durable ready time")
    if queue_latency_ms is not None and queue_latency_ms != derived_queue_latency_ms:
        raise AuditMigrationError("queue latency conflicts with durable timestamps")
    queue_latency_ms = derived_queue_latency_ms
    created_at = created_at or authority["started_at"]
    _semantic_timestamp(created_at, "created_at")
    material = {
        "attempt_id": attempt_id, "queued_at": queued_at,
        "queue_latency_ms": queue_latency_ms, "created_at": created_at,
    }
    values = (
        attempt_id, queued_at, queue_latency_ms,
        _semantic_sha("history-attempt-launch-cost-v2", material), created_at,
    )
    existing = conn.execute(
        "SELECT * FROM audit_attempt_launch_facts_v2 WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) != values:
            raise AuditMigrationError("attempt launch cost fact conflicts")
        return values[3]
    guard = _COST_FACT_GUARDS.get(id(conn))
    if guard is None or guard["launch"] is not None:
        raise AuditMigrationError("cost fact guard is unavailable")
    guard["launch"] = values
    try:
        conn.execute(
            "INSERT INTO audit_attempt_launch_facts_v2 VALUES(?,?,?,?,?)", values
        )
    finally:
        guard["launch"] = None
    return values[3]


def record_attempt_terminal_cost_fact(
    conn, attempt_id, *, completed_at, cancellation=False,
    error_class=None, run_latency_ms=None,
):
    if not conn.in_transaction:
        raise AuditMigrationError("attempt terminal cost fact requires a transaction")
    authority = conn.execute(
        """
        SELECT budget.usage_verified, completion.outcome,
               attempt.created_at AS started_at
        FROM audit_runtime_budget_settlements_v2 budget
        JOIN audit_task_attempts attempt USING(attempt_id)
        LEFT JOIN audit_attempt_completions_v2 completion USING(attempt_id)
        WHERE budget.attempt_id=?
        """,
        (attempt_id,),
    ).fetchone()
    if authority is None:
        raise AuditMigrationError("attempt terminal cost authority is missing")
    if cancellation:
        if authority["outcome"] is not None:
            raise AuditMigrationError("completed attempt cannot be cancelled")
        outcome = "cancelled"
        error_class = error_class or "cancelled"
    else:
        if authority["outcome"] is None:
            raise AuditMigrationError("attempt completion authority is missing")
        outcome = "success" if authority["outcome"] == "valid" else "failed"
        error_class = None if outcome == "success" else authority["outcome"]
    billing_state = "unknown"
    usage_source = (
        "verified_actual" if authority["usage_verified"] == 1 else "reservation"
    )
    price_source = None
    currency = None
    completed = _semantic_timestamp(completed_at, "completed_at")
    started = _semantic_timestamp(authority["started_at"], "started_at")
    derived_run_latency_ms = int((completed - started).total_seconds() * 1000)
    if derived_run_latency_ms < 0:
        raise AuditMigrationError("attempt terminal precedes launch")
    if run_latency_ms is not None and run_latency_ms != derived_run_latency_ms:
        raise AuditMigrationError("run latency conflicts with durable timestamps")
    run_latency_ms = derived_run_latency_ms
    material = {
        "attempt_id": attempt_id, "outcome": outcome,
        "error_class": error_class, "billing_state": billing_state,
        "usage_source": usage_source, "price_source": price_source,
        "currency": currency, "run_latency_ms": run_latency_ms,
        "completed_at": completed_at,
    }
    values = (
        attempt_id, outcome, error_class, billing_state, usage_source,
        price_source, currency, run_latency_ms,
        _semantic_sha("history-attempt-terminal-cost-v2", material), completed_at,
    )
    existing = conn.execute(
        "SELECT * FROM audit_attempt_cost_settlements_v2 WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) != values:
            raise AuditMigrationError("attempt terminal cost fact conflicts")
        return values[8]
    guard = _COST_FACT_GUARDS.get(id(conn))
    if guard is None or guard["settlement"] is not None:
        raise AuditMigrationError("cost fact guard is unavailable")
    guard["settlement"] = values
    try:
        conn.execute(
            "INSERT INTO audit_attempt_cost_settlements_v2 VALUES(?,?,?,?,?,?,?,?,?,?)",
            values,
        )
    finally:
        guard["settlement"] = None
    return values[8]


def record_candidate_route_facts(
    conn, run_id, batch_id, intent, route_authority, *, created_at
):
    """Persist one selected cohort and replayable host-issued route decisions."""
    if not conn.in_transaction:
        raise AuditMigrationError("candidate route facts require a transaction")
    if not isinstance(route_authority, dict) or set(route_authority) != {
        "risk_policy", "risk_slice_policy", "candidate_routes"
    }:
        raise AuditMigrationError("candidate route authority is invalid")
    try:
        from lib import history_audit_eval_v2
    except ImportError:
        import history_audit_eval_v2
    slice_policy = route_authority["risk_slice_policy"]
    if not isinstance(slice_policy, dict) or set(slice_policy) != {
        "schema_version", "policy_version", "allowed_slices"
    } or slice_policy["schema_version"] != "history-risk-slice-policy-v1":
        raise AuditMigrationError("risk slice policy is invalid")
    allowed_slices = slice_policy["allowed_slices"]
    if (
        not isinstance(slice_policy["policy_version"], str)
        or not slice_policy["policy_version"]
        or not isinstance(allowed_slices, list)
        or allowed_slices != sorted(allowed_slices)
        or len(set(allowed_slices)) != len(allowed_slices)
        or any(
            not isinstance(value, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", value) is None
            for value in allowed_slices
        )
        or slice_policy != history_audit_eval_v2.RISK_SLICE_POLICY_V1
    ):
        raise AuditMigrationError("risk slice policy is invalid")
    routes = route_authority["candidate_routes"]
    if not isinstance(routes, list) or not routes:
        raise AuditMigrationError("selected route cohort is empty")
    candidates = [
        item.get("candidate") if isinstance(item, dict) else None
        for item in routes
    ]
    candidate_ids = [
        item.get("candidate_id") if isinstance(item, dict) else None
        for item in candidates
    ]
    try:
        candidates_valid = all(
            isinstance(candidate, dict)
            and set(candidate) == {
                "candidate_id", "candidate_hash", "raw_artifact_sha",
                "source_order",
            }
            and history_audit_plan.runtime_candidate_hash(candidate)
                == candidate["candidate_hash"]
            for candidate in candidates
        )
    except (KeyError, TypeError, ValueError, history_audit_plan.AuditPlanError):
        candidates_valid = False
    if (
        candidate_ids != sorted(candidate_ids)
        or len(set(candidate_ids)) != len(candidate_ids)
        or any(not isinstance(value, str) or not value for value in candidate_ids)
        or not candidates_valid
        or len({candidate["source_order"] for candidate in candidates})
            != len(candidates)
    ):
        raise AuditMigrationError("selected route cohort is invalid")
    run = conn.execute(
        "SELECT manifest_json FROM audit_run_manifests WHERE run_id=?", (run_id,)
    ).fetchone()
    batch_set = conn.execute(
        """
        SELECT member_ids_json, member_count, current_batch_ids_hash
        FROM audit_snapshot_batch_sets
        WHERE run_id=? AND batch_id=?
        """,
        (run_id, batch_id),
    ).fetchone()
    if run is None or batch_set is None:
        raise AuditMigrationError("selected route cohort authority is missing")
    try:
        plan = history_audit_plan.validate_runtime_plan_material(
            _closed_json(run["manifest_json"])
        )
        durable_ids = json.loads(batch_set["member_ids_json"])
        durable_ids_sha = _current_batch_ids_sha(batch_set["member_ids_json"])
    except (TypeError, ValueError, history_audit_plan.AuditPlanError) as exc:
        raise AuditMigrationError("selected route cohort authority is invalid") from exc
    if (
        intent != plan["intent"]
        or batch_id != plan["batch_id"]
        or durable_ids_sha != batch_set["current_batch_ids_hash"]
        or not isinstance(durable_ids, list)
        or durable_ids != sorted(durable_ids)
        or len(set(durable_ids)) != len(durable_ids)
        or not set(candidate_ids).issubset(durable_ids)
        or batch_set["member_count"] != len(durable_ids)
    ):
        raise AuditMigrationError("selected route cohort does not match frozen batch")
    for candidate in candidates:
        conn.execute(
            """
            INSERT OR IGNORE INTO audit_batch_staging(
              staging_candidate_id, run_id, batch_id, candidate_hash,
              raw_artifact_sha, source_order, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate["candidate_id"], run_id, batch_id,
                candidate["candidate_hash"], candidate["raw_artifact_sha"],
                candidate["source_order"], created_at,
            ),
        )
        stored = conn.execute(
            "SELECT run_id, batch_id, candidate_hash, raw_artifact_sha, "
            "source_order FROM audit_batch_staging "
            "WHERE staging_candidate_id=?",
            (candidate["candidate_id"],),
        ).fetchone()
        if stored is None or tuple(stored) != (
            run_id, batch_id, candidate["candidate_hash"],
            candidate["raw_artifact_sha"], candidate["source_order"],
        ):
            raise AuditMigrationError("selected route candidate staging conflicts")
    if conn.execute(
        "SELECT count(*) FROM audit_batch_staging WHERE run_id=? "
        "AND batch_id=? AND staging_candidate_id IN (%s)" % (
            ",".join("?" for _ in candidate_ids)
        ),
        (run_id, batch_id, *candidate_ids),
    ).fetchone()[0] != len(candidate_ids):
        raise AuditMigrationError("selected route cohort staging is incomplete")
    risk_policy = route_authority["risk_policy"]
    risk_policy_sha = _semantic_sha("history-risk-policy-v1", risk_policy)
    if risk_policy_sha != plan["risk_policy_sha"]:
        raise AuditMigrationError("route risk policy does not match frozen plan")
    slice_policy_sha = _semantic_sha(
        "history-risk-slice-policy-v1", slice_policy
    )
    candidate_ids_json = _semantic_canonical(candidate_ids)
    risk_policy_json = _semantic_canonical(risk_policy)
    slice_policy_json = _semantic_canonical(slice_policy)
    cohort_material = {
        "run_id": run_id, "batch_id": batch_id, "intent": intent,
        "candidate_ids": candidate_ids,
        "risk_policy_sha256": risk_policy_sha,
        "risk_slice_policy_sha256": slice_policy_sha,
        "created_at": created_at,
    }
    cohort_sha = _semantic_sha(
        "history-candidate-route-cohort-v2", cohort_material
    )
    cohort_values = (
        run_id, batch_id, intent, candidate_ids_json, risk_policy_json,
        risk_policy_sha, slice_policy_json, slice_policy_sha, cohort_sha,
        created_at,
    )
    guard = _COST_FACT_GUARDS.get(id(conn))
    if guard is None or any(
        guard[name] is not None for name in ("cohort", "route")
    ):
        raise AuditMigrationError("candidate route guard is unavailable")
    existing = conn.execute(
        "SELECT * FROM audit_candidate_route_cohorts_v2 WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if existing is None:
        guard["cohort"] = cohort_values
        try:
            conn.execute(
                "INSERT INTO audit_candidate_route_cohorts_v2 "
                "VALUES(?,?,?,?,?,?,?,?,?,?)", cohort_values,
            )
        finally:
            guard["cohort"] = None
    elif tuple(existing) != cohort_values:
        raise AuditMigrationError("candidate route cohort conflicts")
    fact_shas = []
    for item in routes:
        if set(item) != {"candidate", "router_facts", "risk_slices"}:
            raise AuditMigrationError("candidate route input is invalid")
        risk_slices = item["risk_slices"]
        if (
            not isinstance(risk_slices, list)
            or risk_slices != sorted(risk_slices)
            or len(set(risk_slices)) != len(risk_slices)
            or any(value not in allowed_slices for value in risk_slices)
        ):
            raise AuditMigrationError("candidate risk slices are invalid")
        route = history_audit_eval_v2.route_candidate(
            item["router_facts"], risk_policy
        )
        if bool(risk_slices) != item["router_facts"]["bad_slice_membership"]:
            raise AuditMigrationError("candidate risk slices contradict router facts")
        material = {
            "run_id": run_id, "candidate_id": item["candidate"]["candidate_id"],
            "intent": intent, "cohort_sha256": cohort_sha,
            "router_facts": item["router_facts"],
            "risk_slices": risk_slices,
            "matched_rule_ids": route["matched_rule_ids"],
            "route": route["route"],
            "call_l1_model": route["call_l1_model"],
            "dispatch_allowed": route["dispatch_allowed"],
            "rule_table_sha256": route["rule_table_sha256"],
            "risk_policy_version": route["receipt_risk_policy_version"],
            "created_at": created_at,
        }
        fact_sha = _semantic_sha("history-candidate-route-fact-v2", material)
        values = (
            run_id, item["candidate"]["candidate_id"], intent, cohort_sha,
            _semantic_canonical(item["router_facts"]),
            _semantic_canonical(risk_slices),
            _semantic_canonical(route["matched_rule_ids"]), route["route"],
            int(route["call_l1_model"]), int(route["dispatch_allowed"]),
            route["rule_table_sha256"], route["receipt_risk_policy_version"],
            fact_sha, created_at,
        )
        existing = conn.execute(
            "SELECT * FROM audit_candidate_route_facts_v2 "
            "WHERE run_id=? AND candidate_id=?",
            (run_id, item["candidate"]["candidate_id"]),
        ).fetchone()
        if existing is None:
            guard["route"] = values
            try:
                conn.execute(
                    "INSERT INTO audit_candidate_route_facts_v2 "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values,
                )
            finally:
                guard["route"] = None
        elif tuple(existing) != values:
            raise AuditMigrationError("candidate route fact conflicts")
        fact_shas.append(fact_sha)
    return {"cohort_sha256": cohort_sha, "route_fact_sha256": fact_shas[0]}


def record_candidate_l2_dispatch_fact(conn, plan_sha, *, created_at):
    """Bind an authorized route decision to the exact durable L2 plan."""
    if not conn.in_transaction:
        raise AuditMigrationError("candidate dispatch fact requires a transaction")
    row = conn.execute(
        """
        SELECT plan.plan_sha, plan.run_id, plan.candidate_id,
               route.fact_sha256, route.dispatch_allowed, route.intent
        FROM audit_l2_plans_v2 plan
        JOIN audit_candidate_route_facts_v2 route
          ON route.run_id=plan.run_id AND route.candidate_id=plan.candidate_id
         AND route.intent=plan.intent
        WHERE plan.plan_sha=?
        """,
        (plan_sha,),
    ).fetchone()
    if row is None or row["dispatch_allowed"] != 1:
        raise AuditMigrationError("L2 plan lacks an authorized route decision")
    material = {
        "plan_sha": plan_sha, "run_id": row["run_id"],
        "candidate_id": row["candidate_id"],
        "route_fact_sha256": row["fact_sha256"], "created_at": created_at,
    }
    values = (
        plan_sha, row["run_id"], row["candidate_id"], row["fact_sha256"],
        _semantic_sha("history-candidate-l2-dispatch-v2", material), created_at,
    )
    existing = conn.execute(
        "SELECT * FROM audit_candidate_l2_dispatch_facts_v2 WHERE plan_sha=?",
        (plan_sha,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) != values:
            raise AuditMigrationError("candidate L2 dispatch fact conflicts")
        return values[4]
    guard = _COST_FACT_GUARDS.get(id(conn))
    if guard is None or guard["dispatch"] is not None:
        raise AuditMigrationError("candidate dispatch guard is unavailable")
    guard["dispatch"] = values
    try:
        conn.execute(
            "INSERT INTO audit_candidate_l2_dispatch_facts_v2 "
            "VALUES(?,?,?,?,?,?)", values,
        )
    finally:
        guard["dispatch"] = None
    return values[4]


def _semantic_decimal_identity(value):
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("qualification bounds must be finite")
        return format(value, ".17g")
    if isinstance(value, list):
        return [_semantic_decimal_identity(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _semantic_decimal_identity(item)
            for key, item in value.items()
        }
    return value


def _semantic_canonical(value):
    normalized = _semantic_decimal_identity(value)
    return history_contract_v2.canonical_bytes(normalized).decode("utf-8").rstrip("\n")


def _semantic_sha(domain, value):
    return history_contract_v2.framed_sha256(
        domain,
        history_contract_v2.canonical_bytes(_semantic_decimal_identity(value)),
    )


def _semantic_timestamp(value, name):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a timezone-aware timestamp")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be a timezone-aware timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware timestamp")
    return parsed.astimezone(datetime.timezone.utc)


def _semantic_dependencies(value):
    required = {
        "semantic_policy", "plan", "prompt", "schema", "ordered_provider_pools",
        "capacity", "provider", "fault", "replay",
    }
    allowed = required | {"fts", "metadata", "embedding", "tokenizer"}
    if (
        not isinstance(value, dict)
        or not required.issubset(value)
        or set(value).difference(allowed)
        or any(
            not isinstance(name, str)
            or not name
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            for name, digest in value.items()
        )
    ):
        raise ValueError("qualification dependencies are invalid")
    return dict(sorted(value.items()))


def _current_semantic_dependency_heads(conn, dependency_kinds=None):
    rows = conn.execute(
        """
        SELECT event.dependency_kind, event.dependency_sha256
        FROM audit_semantic_dependency_head_events_v2 event
        JOIN (
          SELECT dependency_kind, max(sequence) AS sequence
          FROM audit_semantic_dependency_head_events_v2
          GROUP BY dependency_kind
        ) head
          ON head.dependency_kind=event.dependency_kind
         AND head.sequence=event.sequence
        ORDER BY event.dependency_kind
        """
    ).fetchall()
    result = {row[0]: row[1] for row in rows}
    if dependency_kinds is not None:
        result = {
            kind: result[kind] for kind in sorted(dependency_kinds)
            if kind in result
        }
    return result


def _current_semantic_dependency_head_events(conn, dependency_kinds=None):
    rows = conn.execute(
        """
        SELECT event.dependency_kind, event.sequence, event.head_event_id,
               event.dependency_sha256
        FROM audit_semantic_dependency_head_events_v2 event
        JOIN (
          SELECT dependency_kind, max(sequence) AS sequence
          FROM audit_semantic_dependency_head_events_v2
          GROUP BY dependency_kind
        ) head
          ON head.dependency_kind=event.dependency_kind
         AND head.sequence=event.sequence
        ORDER BY event.dependency_kind
        """
    ).fetchall()
    result = [
        {
            "dependency_kind": row[0], "sequence": row[1],
            "head_event_id": row[2], "dependency_sha256": row[3],
        }
        for row in rows
    ]
    if dependency_kinds is not None:
        allowed = set(dependency_kinds)
        result = [item for item in result if item["dependency_kind"] in allowed]
    return result


def current_semantic_dependency_heads(conn):
    """Return the current closed dependency digests for diagnostics."""
    return _current_semantic_dependency_heads(conn)


def _publish_semantic_dependency_heads_in_transaction(
    conn, dependencies, created_at
):
    if not conn.in_transaction:
        raise AuditMigrationError("semantic dependency publication requires a transaction")
    dependencies = _semantic_dependencies(dependencies)
    current = _current_semantic_dependency_heads(conn)
    events = []
    for kind, digest in dependencies.items():
        if current.get(kind) == digest:
            continue
        sequence = conn.execute(
            "SELECT COALESCE(max(sequence),0)+1 FROM audit_semantic_dependency_head_events_v2 WHERE dependency_kind=?",
            (kind,),
        ).fetchone()[0]
        material = {
            "dependency_kind": kind,
            "sequence": sequence,
            "dependency_sha256": digest,
            "created_at": created_at,
        }
        event_id = _semantic_sha("history-semantic-dependency-head-v2", material)
        events.append((event_id, kind, sequence, digest, created_at))
    guard = _SEMANTIC_RELEASE_GUARDS.get(id(conn))
    if guard is None or guard["expected_head_events"]:
        raise AuditMigrationError("semantic dependency guard is unavailable")
    guard["expected_head_events"] = frozenset(events)
    try:
        for event in events:
            conn.execute(
                """
                INSERT INTO audit_semantic_dependency_head_events_v2(
                  head_event_id, dependency_kind, sequence,
                  dependency_sha256, created_at
                ) VALUES(?,?,?,?,?)
                """,
                event,
            )
    finally:
        guard["expected_head_events"] = frozenset()
    return [event[0] for event in events]


def publish_semantic_dependency_heads(conn, changed_dependencies, *, now=None):
    """Append changed closed dependency heads under one host-owned transaction."""
    if conn.in_transaction:
        raise AuditMigrationError("semantic dependency publication requires an idle connection")
    if not isinstance(changed_dependencies, dict) or not changed_dependencies:
        raise ValueError("changed dependencies are required")
    created_at = now or _utc_now()
    _semantic_timestamp(created_at, "created_at")
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = _current_semantic_dependency_heads(conn)
        merged = dict(current)
        merged.update(changed_dependencies)
        dependencies = _semantic_dependencies(merged)
        event_ids = _publish_semantic_dependency_heads_in_transaction(
            conn, dependencies, created_at
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {"event_ids": event_ids, "heads": _current_semantic_dependency_heads(conn)}


def persist_semantic_qualification(
    conn, qrels, outputs=None, policy=None, evidence=None, *, now=None
):
    """Recompute and append one immutable semantic release qualification."""
    if outputs is None or policy is None or evidence is None:
        raise ValueError("raw evaluation materials are required")
    try:
        from lib import history_audit_eval_v2
    except ImportError:
        import history_audit_eval_v2
    qualification = history_audit_eval_v2.evaluate_production_qualification(
        qrels, outputs, policy, evidence
    )
    evaluation_guard = _SEMANTIC_EVALUATION_GUARDS.get(id(conn))
    if evaluation_guard is None or evaluation_guard["expected"] is not None:
        raise AuditMigrationError("semantic evaluator guard is unavailable")
    evaluation_guard["expected"] = _semantic_sha(
        "history-semantic-evaluator-issuance-v2", qualification
    )
    try:
        return _persist_semantic_qualification(conn, qualification, now=now)
    finally:
        evaluation_guard["expected"] = None


def _persist_semantic_qualification(conn, qualification, *, now=None):
    """Persist only a qualification recomputed by the host-owned evaluator."""
    evaluation_guard = _SEMANTIC_EVALUATION_GUARDS.get(id(conn))
    expected = _semantic_sha(
        "history-semantic-evaluator-issuance-v2", qualification
    )
    if evaluation_guard is None or evaluation_guard["expected"] != expected:
        raise ValueError("qualification lacks evaluator issuance authority")
    evaluation_guard["expected"] = None
    fields = {
        "schema_version", "semantic_policy_profile_id", "production_qualified",
        "no_match_basis", "scope", "policy_sha256", "qrels_hash",
        "corpus_snapshot_hash", "evaluation_hash", "metric_report_hash",
        "dependency_hashes", "metrics", "vetoes", "expires_at",
    }
    if not isinstance(qualification, dict) or set(qualification) != fields:
        raise ValueError("semantic qualification fields are invalid")
    if (
        qualification["schema_version"] != "semantic-qualification-v2"
        or not isinstance(qualification["semantic_policy_profile_id"], str)
        or not qualification["semantic_policy_profile_id"]
        or type(qualification["production_qualified"]) is not bool
        or qualification["no_match_basis"] not in {
            "l1_calibrated", "l2_exhaustive"
        }
        or not isinstance(qualification["scope"], str)
        or not qualification["scope"]
        or not isinstance(qualification["metrics"], dict)
        or not isinstance(qualification["vetoes"], list)
        or qualification["vetoes"] != sorted(set(qualification["vetoes"]))
        or any(not isinstance(veto, str) or not veto for veto in qualification["vetoes"])
        or qualification["production_qualified"] == bool(qualification["vetoes"])
    ):
        raise ValueError("semantic qualification state is invalid")
    for name in (
        "policy_sha256", "qrels_hash", "corpus_snapshot_hash",
        "evaluation_hash", "metric_report_hash",
    ):
        if re.fullmatch(r"[0-9a-f]{64}", qualification[name] or "") is None:
            raise ValueError(f"{name} is invalid")
    dependencies = _semantic_dependencies(qualification["dependency_hashes"])
    if dependencies["semantic_policy"] != qualification["policy_sha256"]:
        raise ValueError("semantic policy dependency is not exact")
    created_at = now or _utc_now()
    _semantic_timestamp(created_at, "created_at")
    _semantic_timestamp(qualification["expires_at"], "expires_at")
    material = dict(qualification)
    material["dependency_hashes"] = dependencies
    qualification_sha = _semantic_sha(
        "history-semantic-qualification-v2", material
    )
    qualification_id = "semantic-v2-" + qualification_sha
    qualification_json = _semantic_canonical(material)
    capacity_bindings = _semantic_canonical(
        {
            "capacity": dependencies["capacity"],
            "provider": dependencies["provider"],
            "ordered_provider_pools": dependencies["ordered_provider_pools"],
        }
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        release_guard = _SEMANTIC_RELEASE_GUARDS.get(id(conn))
        if release_guard is None:
            raise AuditMigrationError("semantic release guard is unavailable")
        qualification_values = (
            qualification_id, material["semantic_policy_profile_id"],
            qualification_sha, material["corpus_snapshot_hash"],
            capacity_bindings, material["expires_at"], qualification_json,
            created_at,
        )
        fact_values = (
            qualification_id, material["no_match_basis"], material["scope"],
            material["policy_sha256"], material["qrels_hash"],
            material["evaluation_hash"], material["metric_report_hash"],
            _semantic_canonical(dependencies),
            _semantic_canonical(material["metrics"]),
            _semantic_canonical(material["vetoes"]),
            int(material["production_qualified"]), material["expires_at"],
            created_at,
        )
        release_guard["expected_qualification"] = qualification_values
        release_guard["expected_qualification_fact"] = fact_values
        conn.execute(
            """
            INSERT OR IGNORE INTO audit_semantic_qualifications(
              qualification_id, semantic_policy_profile_id,
              qualification_sha256, corpus_snapshot_hash,
              provider_capacity_hashes_json, expires_at,
              qualification_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            qualification_values,
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO audit_semantic_qualification_facts_v2(
              qualification_id, no_match_basis, scope, policy_sha256,
              qrels_hash, evaluation_hash, metric_report_hash,
              dependency_hashes_json, metrics_json, vetoes_json,
              production_qualified, expires_at, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            fact_values,
        )
        release_guard["expected_qualification"] = None
        release_guard["expected_qualification_fact"] = None
        base_row = conn.execute(
            """
            SELECT qualification_id, semantic_policy_profile_id,
                   qualification_sha256, corpus_snapshot_hash,
                   provider_capacity_hashes_json, expires_at,
                   qualification_json, created_at
            FROM audit_semantic_qualifications WHERE qualification_id=?
            """,
            (qualification_id,),
        ).fetchone()
        fact_row = conn.execute(
            """
            SELECT qualification_id, no_match_basis, scope, policy_sha256,
                   qrels_hash, evaluation_hash, metric_report_hash,
                   dependency_hashes_json, metrics_json, vetoes_json,
                   production_qualified, expires_at, created_at
            FROM audit_semantic_qualification_facts_v2 WHERE qualification_id=?
            """,
            (qualification_id,),
        ).fetchone()
        if (
            base_row is None or tuple(base_row) != qualification_values
            or fact_row is None or tuple(fact_row) != fact_values
        ):
            raise ValueError("semantic qualification identity conflicts")
        if _current_semantic_dependency_heads(conn, dependencies) != dependencies:
            raise ValueError("qualification dependencies are not current")
        head_events_json = _semantic_canonical(
            _current_semantic_dependency_head_events(conn, dependencies)
        )
        expected_binding = (qualification_id, head_events_json, created_at)
        release_guard["expected_qualification_binding"] = expected_binding
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO audit_semantic_qualification_head_bindings_v2(
                  qualification_id, dependency_head_events_json, bound_at
                ) VALUES(?,?,?)
                """,
                expected_binding,
            )
        finally:
            release_guard["expected_qualification_binding"] = None
        binding = conn.execute(
            "SELECT dependency_head_events_json FROM audit_semantic_qualification_head_bindings_v2 WHERE qualification_id=?",
            (qualification_id,),
        ).fetchone()
        if binding is None or binding[0] != head_events_json:
            raise ValueError("semantic qualification head binding conflicts")
        conn.execute("COMMIT")
    except Exception:
        release_guard = _SEMANTIC_RELEASE_GUARDS.get(id(conn))
        if release_guard is not None:
            release_guard["expected_qualification"] = None
            release_guard["expected_qualification_fact"] = None
            release_guard["expected_qualification_binding"] = None
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {
        "qualification_id": qualification_id,
        "qualification_sha256": qualification_sha,
        "production_qualified": material["production_qualified"],
    }


def lookup_semantic_qualification(
    conn, *, semantic_policy_profile_id, no_match_basis, policy_sha256,
    corpus_snapshot_hash, evaluation_hash, dependency_hashes, now=None
):
    """Return only a live, non-invalidated qualification with exact dependencies."""
    dependencies = _semantic_dependencies(dependency_hashes)
    if dependencies["semantic_policy"] != policy_sha256:
        return None
    current = _semantic_timestamp(now or _utc_now(), "now")
    row = conn.execute(
        """
        SELECT qualification.qualification_id,
               qualification.qualification_sha256,
               qualification.semantic_policy_profile_id,
               fact.no_match_basis, fact.policy_sha256,
               qualification.corpus_snapshot_hash, fact.evaluation_hash,
               fact.dependency_hashes_json, fact.expires_at,
               qualification.qualification_json,
               binding.dependency_head_events_json,
               fact.scope, fact.qrels_hash, fact.metric_report_hash,
               fact.metrics_json, fact.vetoes_json,
               fact.production_qualified, qualification.expires_at
        FROM audit_semantic_qualifications qualification
        JOIN audit_semantic_qualification_facts_v2 fact
          USING(qualification_id)
        JOIN audit_semantic_qualification_head_bindings_v2 binding
          USING(qualification_id)
        WHERE qualification.semantic_policy_profile_id=?
          AND fact.no_match_basis=?
          AND fact.policy_sha256=?
          AND qualification.corpus_snapshot_hash=?
          AND fact.evaluation_hash=?
          AND fact.dependency_hashes_json=?
          AND fact.production_qualified=1
          AND fact.vetoes_json='[]'
          AND NOT EXISTS (
            SELECT 1 FROM audit_semantic_invalidation_facts_v2 invalidation
            WHERE invalidation.qualification_id=qualification.qualification_id
          )
        ORDER BY fact.created_at DESC, qualification.qualification_id DESC
        """,
        (
            semantic_policy_profile_id, no_match_basis, policy_sha256,
            corpus_snapshot_hash, evaluation_hash,
            _semantic_canonical(dependencies),
        ),
    ).fetchone()
    if row is None or _semantic_timestamp(row[8], "expires_at") <= current:
        return None
    if (
        _current_semantic_dependency_heads(conn, dependencies) != dependencies
        or row[10] != _semantic_canonical(
            _current_semantic_dependency_head_events(conn, dependencies)
        )
    ):
        return None
    try:
        qualification_material = json.loads(row[9])
    except (TypeError, ValueError):
        return None
    if (
        _semantic_sha("history-semantic-qualification-v2", qualification_material)
        != row[1]
        or row[0] != "semantic-v2-" + row[1]
        or qualification_material.get("semantic_policy_profile_id") != row[2]
        or qualification_material.get("no_match_basis") != row[3]
        or qualification_material.get("policy_sha256") != row[4]
        or qualification_material.get("corpus_snapshot_hash") != row[5]
        or qualification_material.get("evaluation_hash") != row[6]
        or qualification_material.get("dependency_hashes") != json.loads(row[7])
        or qualification_material.get("expires_at") != row[8]
        or qualification_material.get("scope") != row[11]
        or qualification_material.get("qrels_hash") != row[12]
        or qualification_material.get("metric_report_hash") != row[13]
        or _semantic_canonical(qualification_material.get("metrics")) != row[14]
        or _semantic_canonical(qualification_material.get("vetoes")) != row[15]
        or int(bool(qualification_material.get("production_qualified"))) != row[16]
        or qualification_material.get("expires_at") != row[17]
    ):
        return None
    return {
        "qualification_id": row[0],
        "qualification_sha256": row[1],
        "semantic_policy_profile_id": row[2],
        "no_match_basis": row[3],
        "policy_sha256": row[4],
        "corpus_snapshot_hash": row[5],
        "evaluation_hash": row[6],
        "dependency_hashes": json.loads(row[7]),
        "dependency_head_events_json": row[10],
        "scope": row[11],
        "expires_at": row[8],
    }


def _authorize_complete_no_match_receipt(
    conn, receipt, release_context, *, now=None
):
    """Fail closed until production runtime authority is durably available."""
    if not conn.in_transaction:
        raise AuditMigrationError("semantic release authorization requires a transaction")
    try:
        normalized = history_contract_v2.validate_receipt(receipt)
    except history_contract_v2.ContractV2Error as exc:
        raise ValueError("semantic release receipt is invalid") from exc
    if normalized["final_status"] != "complete_no_match":
        raise ValueError("semantic release receipt is not complete_no_match")
    raise ValueError("production_runtime_authority_unavailable")


def clear_semantic_receipt_authorization(conn):
    guard = _SEMANTIC_RELEASE_GUARDS.get(id(conn))
    if guard is not None:
        guard.update(
            expected_authorization=None,
            receipt_id=None,
            receipt_material_sha256=None,
            qualification_id=None,
        )


def insert_authorized_complete_no_match_receipt(
    conn, receipt, release_context, *, now=None
):
    """Atomically authorize and insert one exact complete-no-match receipt."""
    if not conn.in_transaction:
        raise AuditMigrationError("authorized receipt insert requires a transaction")
    normalized = history_contract_v2.validate_receipt(receipt)
    if normalized["final_status"] != "complete_no_match":
        raise ValueError("authorized receipt insert is only for complete_no_match")
    fields = _RELEASE_RECEIPT_FIELDS
    values = []
    for field in fields:
        value = normalized[field]
        if field in _RELEASE_JSON_FIELDS:
            value = history_contract_v2.canonical_bytes(value).decode("utf-8")
        elif field in _RELEASE_BOOLEAN_FIELDS:
            value = int(value)
        values.append(value)
    conn.execute("SAVEPOINT semantic_release_receipt")
    try:
        authorization = _authorize_complete_no_match_receipt(
            conn, normalized, release_context, now=now
        )
        conn.execute(
            "INSERT INTO audit_receipts(" + ",".join(fields) + ") VALUES(" +
            ",".join("?" for _ in fields) + ")",
            tuple(values),
        )
        conn.execute("RELEASE SAVEPOINT semantic_release_receipt")
        return authorization
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT semantic_release_receipt")
        conn.execute("RELEASE SAVEPOINT semantic_release_receipt")
        raise
    finally:
        clear_semantic_receipt_authorization(conn)


def verify_semantic_release_authorization(
    conn, receipt, *, require_current=False, expected_context=None, now=None
):
    """Verify historical issuance and optionally current no-match authority."""
    normalized = history_contract_v2.validate_receipt(receipt)
    if normalized["final_status"] != "complete_no_match":
        return {"historically_authorized": False, "current_authority": False}
    material_sha = history_contract_v2.framed_sha256(
        "history-semantic-release-receipt-v2",
        history_contract_v2.canonical_bytes(normalized),
    )
    authorization = conn.execute(
        """
        SELECT * FROM audit_semantic_release_authorizations_v2
        WHERE receipt_id=?
        """,
        (normalized["minimum_receipt_sha"],),
    ).fetchone()
    if (
        authorization is None
        or authorization["receipt_material_sha256"] != material_sha
        or authorization["semantic_policy_profile_id"]
            != normalized["semantic_policy_profile_id"]
        or authorization["no_match_basis"] != normalized["no_match_basis"]
        or authorization["corpus_snapshot_hash"] != normalized["snapshot_hash"]
    ):
        raise ValueError("receipt release authorization is missing or substituted")
    qualification = conn.execute(
        """
        SELECT qualification.qualification_sha256,
               qualification.qualification_json,
               qualification.semantic_policy_profile_id,
               qualification.corpus_snapshot_hash,
               fact.no_match_basis, fact.scope, fact.policy_sha256,
               fact.evaluation_hash, fact.dependency_hashes_json,
               fact.production_qualified, fact.vetoes_json, fact.expires_at,
               binding.dependency_head_events_json
        FROM audit_semantic_qualifications qualification
        JOIN audit_semantic_qualification_facts_v2 fact USING(qualification_id)
        JOIN audit_semantic_qualification_head_bindings_v2 binding
          USING(qualification_id)
        WHERE qualification.qualification_id=?
        """,
        (authorization["qualification_id"],),
    ).fetchone()
    if qualification is None:
        raise ValueError("receipt qualification is missing")
    try:
        material = history_contract_v2.parse_json_bytes(
            (qualification["qualification_json"] + "\n").encode("utf-8")
        )
    except history_contract_v2.ContractV2Error as exc:
        raise ValueError("receipt qualification is not canonical") from exc
    if (
        set(material) != {
            "schema_version", "semantic_policy_profile_id", "production_qualified",
            "no_match_basis", "scope", "policy_sha256", "qrels_hash",
            "corpus_snapshot_hash", "evaluation_hash", "metric_report_hash",
            "dependency_hashes", "metrics", "vetoes", "expires_at",
        }
        or type(material.get("production_qualified")) is not bool
        or not material["production_qualified"]
        or material.get("vetoes") != []
        or _semantic_sha("history-semantic-qualification-v2", material)
            != qualification["qualification_sha256"]
        or authorization["qualification_sha256"]
            != qualification["qualification_sha256"]
        or authorization["qualification_id"]
            != "semantic-v2-" + qualification["qualification_sha256"]
        or material.get("semantic_policy_profile_id") != qualification["semantic_policy_profile_id"]
        or material.get("corpus_snapshot_hash") != qualification["corpus_snapshot_hash"]
        or material.get("no_match_basis") != qualification["no_match_basis"]
        or material.get("scope") != qualification["scope"]
        or material.get("policy_sha256") != qualification["policy_sha256"]
        or material.get("evaluation_hash") != qualification["evaluation_hash"]
        or material.get("dependency_hashes") != json.loads(qualification["dependency_hashes_json"])
        or qualification["production_qualified"] != 1
        or qualification["vetoes_json"] != "[]"
        or material.get("expires_at") != qualification["expires_at"]
        or authorization["policy_sha256"] != qualification["policy_sha256"]
        or authorization["evaluation_hash"] != qualification["evaluation_hash"]
        or authorization["dependency_hashes_json"] != qualification["dependency_hashes_json"]
        or authorization["dependency_heads_json"] != qualification["dependency_head_events_json"]
    ):
        raise ValueError("receipt qualification identity is inconsistent")
    if expected_context is not None:
        fields = {"run_id", "plan_hash", "candidate_hash", "snapshot_id", "snapshot_hash"}
        if not isinstance(expected_context, dict) or set(expected_context) != fields:
            raise ValueError("expected receipt context is invalid")
        if any(normalized[field] != expected_context[field] for field in fields):
            raise ValueError("receipt context does not match replay request")
    if require_current:
        raise ValueError("production_runtime_authority_unavailable")
    return {
        "historically_authorized": True,
        "current_authority": False,
        "qualification_id": authorization["qualification_id"],
        "receipt_material_sha256": material_sha,
    }


def record_qualification_invalidation(
    conn, qualification_id, changed_dependencies, *, now=None
):
    """Append a targeted invalidation fact; old profile IDs never revive."""
    if not isinstance(qualification_id, str) or not qualification_id:
        raise ValueError("qualification_id is required")
    changed = _semantic_dependencies({
        **{
            "semantic_policy": "0" * 64, "prompt": "0" * 64,
            "plan": "0" * 64,
            "schema": "0" * 64, "ordered_provider_pools": "0" * 64,
            "capacity": "0" * 64, "provider": "0" * 64,
            "fault": "0" * 64, "replay": "0" * 64,
        },
        **changed_dependencies,
    })
    changed = {
        key: value for key, value in changed.items()
        if key in changed_dependencies
    }
    if not changed:
        raise ValueError("changed dependencies are required")
    invalidated_at = now or _utc_now()
    _semantic_timestamp(invalidated_at, "invalidated_at")
    bound_row = conn.execute(
        "SELECT dependency_hashes_json FROM audit_semantic_qualification_facts_v2 WHERE qualification_id=?",
        (qualification_id,),
    ).fetchone()
    if bound_row is None:
        raise ValueError("qualification does not exist")
    bound = json.loads(bound_row[0])
    changed = {
        key: value for key, value in changed.items()
        if key in bound and bound[key] != value
    }
    if not changed:
        raise ValueError("changed dependencies do not affect qualification")
    search_generations = [
        name for name in ("fts", "metadata", "embedding", "tokenizer")
        if name in changed
    ]
    impacts = {
        "qualification_stale": True,
        "adjudication_stale": any(
            name in {
                "prompt", "schema", "ordered_provider_pools", "capacity",
                "provider",
            }
            for name in changed
        ),
        "search_generations_stale": search_generations,
        "flat_generation_stale": "fts" in search_generations,
    }
    material = {
        "qualification_id": qualification_id,
        "changed_dependencies": changed,
        "impacts": impacts,
        "invalidated_at": invalidated_at,
    }
    fact_sha = _semantic_sha("history-semantic-invalidation-v2", material)
    conn.execute(
        """
        INSERT OR IGNORE INTO audit_semantic_invalidation_facts_v2(
          invalidation_id, qualification_id, changed_dependencies_json,
          impacts_json, invalidated_at, fact_sha256
        ) VALUES(?, ?, ?, ?, ?, ?)
        """,
        (
            fact_sha, qualification_id, _semantic_canonical(changed),
            _semantic_canonical(impacts),
            invalidated_at, fact_sha,
        ),
    )
    return {
        "invalidation_id": fact_sha,
        "qualification_id": qualification_id,
        "changed_dependencies": changed,
        "impacts": impacts,
    }


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


def _l2_transition_timestamp(value):
    if not isinstance(value, str) or not value:
        raise ValueError("L2 terminal transition timestamp is required")
    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("L2 terminal transition timestamp needs timezone")
    return parsed.astimezone(datetime.timezone.utc).isoformat()


def _l2_transition_parent(conn, parent_task_hash):
    row = conn.execute(
        """
        SELECT task.*, binding.plan_sha, binding.snapshot_id,
               binding.snapshot_hash, binding.assigned_item_ids_json,
               binding.frozen_records_json, binding.provider_pool_json,
               binding.split_depth
        FROM audit_l2_valid_task_authority_v2 valid
        JOIN audit_logical_tasks task ON task.task_hash=valid.task_hash
        JOIN audit_task_bindings_v2 binding ON binding.task_hash=task.task_hash
        WHERE task.task_hash=?
        """,
        (parent_task_hash,),
    ).fetchone()
    if row is None:
        raise AuditMigrationError("split parent lacks durable task authority")
    return dict(row)


def _validated_split_children(conn, parent_task_hash):
    family = conn.execute(
        """
        SELECT child0_task_hash, child1_task_hash
        FROM audit_l2_valid_split_families_v2
        WHERE parent_task_hash=?
        """,
        (parent_task_hash,),
    ).fetchone()
    if family is None:
        raise AuditMigrationError("superseded task has malformed split authority")
    rows = conn.execute(
        """
        SELECT edge.position, binding.task_hash, binding.assigned_item_ids_json
        FROM audit_task_edges_v2 edge
        JOIN audit_task_bindings_v2 binding
          ON binding.task_hash=edge.child_task_hash
        WHERE edge.parent_task_hash=? ORDER BY edge.position
        """,
        (parent_task_hash,),
    ).fetchall()
    if (
        len(rows) != 2
        or [row["position"] for row in rows] != [0, 1]
        or [row["task_hash"] for row in rows]
           != [family["child0_task_hash"], family["child1_task_hash"]]
    ):
        raise AuditMigrationError("superseded task has incomplete split children")
    return [
        {
            "position": row["position"],
            "task_hash": row["task_hash"],
            "item_ids": _closed_json(row["assigned_item_ids_json"]),
        }
        for row in rows
    ]


def _l2_claim_is_live(parent, expected_fence, claim_token, now):
    return (
        parent["state"] == "claimed"
        and type(expected_fence) is int
        and parent["fence"] == expected_fence
        and isinstance(claim_token, str)
        and claim_token
        and parent["claim_token"] == claim_token
        and _metadata_lease_live(parent["lease_until"], now) == 1
    )


def _l2_current_claim_has_overflow(conn, parent):
    return conn.execute(
        """
        SELECT 1
        FROM audit_task_attempts attempt
        JOIN audit_attempt_completions_v2 completion
          ON completion.attempt_id=attempt.attempt_id
        JOIN audit_cas_objects output
          ON output.object_id=completion.output_cas_object_id
        WHERE attempt.task_hash=? AND completion.outcome='overflow'
          AND output.integrity_state='verified'
          AND json_extract(attempt.provenance_json, '$.claim_fence')=?
          AND json_extract(attempt.provenance_json, '$.claim_token')=?
        LIMIT 1
        """,
        (parent["task_hash"], parent["fence"], parent["claim_token"]),
    ).fetchone() is not None


def transition_l2_split_task(
    conn, parent_task_hash, *, expected_fence, claim_token, now
):
    """Atomically derive and persist one exact two-child split transition."""
    if conn.in_transaction:
        raise AuditMigrationError("split transition requires an idle connection")
    now = _l2_transition_timestamp(now)
    parent = _l2_transition_parent(conn, parent_task_hash)
    if parent["state"] == "superseded":
        return {
            "state": "superseded",
            "children": _validated_split_children(conn, parent_task_hash),
        }
    if parent["state"] in {"settled", "exhausted"}:
        raise StaleFence("logical task is already terminal")
    if not _l2_claim_is_live(parent, expected_fence, claim_token, now):
        raise StaleFence("split requires a live matching claim")
    item_ids = _closed_json(parent["assigned_item_ids_json"])
    if len(item_ids) < 2:
        raise AuditMigrationError("split parent is not divisible")
    midpoint = len(item_ids) // 2
    groups = (item_ids[:midpoint], item_ids[midpoint:])
    if not all(groups):
        raise AuditMigrationError("split produced an empty child")
    frozen_records = _closed_json(parent["frozen_records_json"])
    record_by_id = {record["item_id"]: record for record in frozen_records}
    child_rows = []
    for position, child_ids in enumerate(groups):
        request_material = {
            "parent_task_hash": parent_task_hash,
            "position": position,
            "item_ids": child_ids,
        }
        request_text = history_contract_v2.canonical_bytes(
            request_material
        ).decode("utf-8")
        request_sha = hashlib.sha256(request_text.encode("utf-8")).hexdigest()
        child_rows.append(
            {
                "position": position,
                "task_hash": history_contract_v2.logical_task_key(
                    parent["plan_sha"], parent["stage"],
                    parent["staging_candidate_id"], request_sha,
                ),
                "input_id": parent["input_id"] + f".{position}",
                "request_sha": request_sha,
                "request_text": request_text,
                "item_ids": child_ids,
                "frozen_records": [record_by_id[item_id] for item_id in child_ids],
            }
        )
    created_at = now
    terminal_sha = _l2_terminal_fact_sha(
        parent_task_hash, "superseded", "invalid_parent_split"
    )
    edges = []
    children = []
    for child in child_rows:
        children.append(
            (
                child["task_hash"], parent["run_id"], parent["stage"],
                parent["staging_candidate_id"], child["input_id"],
            )
        )
        edges.append(
            (
                parent_task_hash, child["task_hash"], child["position"],
                _l2_edge_sha(
                    parent_task_hash, child["task_hash"], child["position"]
                ),
                created_at,
            )
        )
    authorization_sha = _l2_transition_authorization_sha(
        parent_task_hash, "split", parent["fence"], parent["claim_token"],
        parent["lease_until"], child_rows[0]["task_hash"],
        child_rows[1]["task_hash"],
    )
    transition = (
        parent_task_hash, "claimed", parent["fence"], parent["claim_token"],
        parent["lease_until"], "superseded", parent["fence"] + 1, None, None,
    )
    terminal = (
        parent_task_hash, "superseded", "invalid_parent_split",
        terminal_sha, created_at,
    )
    authority = (
        parent_task_hash, "split", "claimed-v1", parent["fence"],
        parent["claim_token"], parent["lease_until"],
        child_rows[0]["task_hash"], child_rows[1]["task_hash"],
        authorization_sha, created_at,
    )
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT state, fence, claim_token, lease_until "
            "FROM audit_logical_tasks WHERE task_hash=?",
            (parent_task_hash,),
        ).fetchone()
        if current is None or tuple(current) != (
            "claimed", parent["fence"], parent["claim_token"],
            parent["lease_until"],
        ):
            raise StaleFence("split claim changed before transition")
        with _l2_terminal_transition_guard(
            conn, children=children, transition=transition,
            terminal=terminal, edges=edges, authority=authority,
        ):
            compare_and_set_logical_task(
                conn, parent_task_hash,
                expected_state="claimed", expected_fence=parent["fence"],
                new_state="superseded", new_fence=parent["fence"] + 1,
                claim_token=None, lease_until=None,
            )
            conn.execute(
                "INSERT INTO audit_task_terminal_facts_v2 VALUES(?, ?, ?, ?, ?)",
                terminal,
            )
            for child in child_rows:
                conn.execute(
                    """
                    INSERT INTO audit_logical_tasks(
                      task_hash, run_id, stage, staging_candidate_id, input_id,
                      state, fence, claim_token, lease_until, created_at
                    ) VALUES(?, ?, ?, ?, ?, 'planned', 0, NULL, NULL, ?)
                    """,
                    (
                        child["task_hash"], parent["run_id"], parent["stage"],
                        parent["staging_candidate_id"], child["input_id"],
                        created_at,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO audit_task_bindings_v2(
                      task_hash, plan_sha, snapshot_id, snapshot_hash,
                      shard_input_sha, assigned_item_ids_json,
                      frozen_records_json, provider_pool_json,
                      parent_task_hash, split_depth, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        child["task_hash"], parent["plan_sha"],
                        parent["snapshot_id"], parent["snapshot_hash"],
                        child["request_sha"],
                        history_contract_v2.canonical_bytes(
                            child["item_ids"]
                        ).decode("utf-8"),
                        history_contract_v2.canonical_bytes(
                            child["frozen_records"]
                        ).decode("utf-8"),
                        parent["provider_pool_json"], parent_task_hash,
                        parent["split_depth"] + 1, created_at,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO audit_l2_task_inputs_v2(
                      task_hash, input_id, request_sha, request_text,
                      item_ids_json, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        child["task_hash"], child["input_id"],
                        child["request_sha"], child["request_text"],
                        history_contract_v2.canonical_bytes(
                            child["item_ids"]
                        ).decode("utf-8"),
                        created_at,
                    ),
                )
            for edge in edges:
                conn.execute(
                    "INSERT INTO audit_task_edges_v2 VALUES(?, ?, ?, ?, ?)", edge
                )
            conn.execute(
                """
                INSERT INTO audit_l2_terminal_transition_authority_v2(
                  parent_task_hash, transition_kind, authority_kind,
                  claim_fence, claim_token, lease_until,
                  child0_task_hash, child1_task_hash,
                  authorization_sha256, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                authority,
            )
            if conn.execute(
                "SELECT 1 FROM audit_l2_valid_split_families_v2 "
                "WHERE parent_task_hash=?", (parent_task_hash,),
            ).fetchone() is None:
                raise AuditMigrationError("split transition failed durable validation")
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {
        "state": "superseded",
        "children": _validated_split_children(conn, parent_task_hash),
    }


def transition_l2_exhaust_task(
    conn, task_hash, reason, *, expected_fence, claim_token, now
):
    """Atomically persist one claimed task exhaustion with exact authority."""
    if conn.in_transaction:
        raise AuditMigrationError("exhaust transition requires an idle connection")
    now = _l2_transition_timestamp(now)
    parent = _l2_transition_parent(conn, task_hash)
    if parent["state"] == "exhausted":
        if conn.execute(
            "SELECT 1 FROM audit_l2_valid_exhaustions_v2 WHERE task_hash=?",
            (task_hash,),
        ).fetchone() is None:
            raise AuditMigrationError("exhausted task has malformed authority")
        return {"state": "exhausted", "children": []}
    if parent["state"] in {"settled", "superseded"}:
        raise StaleFence("logical task is already terminal")
    if not _l2_claim_is_live(parent, expected_fence, claim_token, now):
        raise StaleFence("exhaustion requires a live matching claim")
    item_ids = _closed_json(parent["assigned_item_ids_json"])
    if reason == "single_item_overflow" and (
        len(item_ids) != 1 or not _l2_current_claim_has_overflow(conn, parent)
    ):
        raise AuditMigrationError("single-item exhaustion lacks overflow evidence")
    created_at = now
    terminal_sha = _l2_terminal_fact_sha(task_hash, "exhausted", reason)
    authorization_sha = _l2_transition_authorization_sha(
        task_hash, "exhaust", parent["fence"], parent["claim_token"],
        parent["lease_until"], None, None,
    )
    transition = (
        task_hash, "claimed", parent["fence"], parent["claim_token"],
        parent["lease_until"], "exhausted", parent["fence"] + 1, None, None,
    )
    terminal = (task_hash, "exhausted", reason, terminal_sha, created_at)
    authority = (
        task_hash, "exhaust", "claimed-v1", parent["fence"],
        parent["claim_token"], parent["lease_until"], None, None,
        authorization_sha, created_at,
    )
    try:
        conn.execute("BEGIN IMMEDIATE")
        with _l2_terminal_transition_guard(
            conn, children=(), transition=transition, terminal=terminal,
            edges=(), authority=authority,
        ):
            compare_and_set_logical_task(
                conn, task_hash,
                expected_state="claimed", expected_fence=parent["fence"],
                new_state="exhausted", new_fence=parent["fence"] + 1,
                claim_token=None, lease_until=None,
            )
            conn.execute(
                "INSERT INTO audit_task_terminal_facts_v2 VALUES(?, ?, ?, ?, ?)",
                terminal,
            )
            conn.execute(
                """
                INSERT INTO audit_l2_terminal_transition_authority_v2(
                  parent_task_hash, transition_kind, authority_kind,
                  claim_fence, claim_token, lease_until,
                  child0_task_hash, child1_task_hash,
                  authorization_sha256, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                authority,
            )
            if conn.execute(
                "SELECT 1 FROM audit_l2_valid_exhaustions_v2 WHERE task_hash=?",
                (task_hash,),
            ).fetchone() is None:
                raise AuditMigrationError("exhaustion failed durable validation")
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {"state": "exhausted", "children": []}


def validate_l2_terminal_graph(conn, plan_sha=None):
    """Require every binding and terminal fact to have complete durable authority."""
    plan_clause = "" if plan_sha is None else "AND binding.plan_sha=?"
    parameters = () if plan_sha is None else (plan_sha,)
    invalid_binding = conn.execute(
        f"""
        SELECT 1 FROM audit_task_bindings_v2 binding
        LEFT JOIN audit_l2_valid_task_authority_v2 valid
          ON valid.task_hash=binding.task_hash
        WHERE valid.task_hash IS NULL {plan_clause} LIMIT 1
        """,
        parameters,
    ).fetchone()
    invalid_terminal = conn.execute(
        f"""
        SELECT 1
        FROM audit_logical_tasks task
        JOIN audit_task_bindings_v2 binding ON binding.task_hash=task.task_hash
        LEFT JOIN audit_l2_valid_split_families_v2 split
          ON split.parent_task_hash=task.task_hash
        LEFT JOIN audit_l2_valid_exhaustions_v2 exhausted
          ON exhausted.task_hash=task.task_hash
        WHERE ((task.state='superseded' AND split.parent_task_hash IS NULL)
           OR (task.state='exhausted' AND exhausted.task_hash IS NULL))
          {plan_clause}
        LIMIT 1
        """,
        parameters,
    ).fetchone()
    invalid_fact = conn.execute(
        f"""
        SELECT 1
        FROM audit_task_terminal_facts_v2 terminal
        JOIN audit_task_bindings_v2 binding
          ON binding.task_hash=terminal.task_hash
        LEFT JOIN audit_l2_valid_split_families_v2 split
          ON split.parent_task_hash=terminal.task_hash
        LEFT JOIN audit_l2_valid_exhaustions_v2 exhausted
          ON exhausted.task_hash=terminal.task_hash
        WHERE ((terminal.terminal_state='superseded'
                AND split.parent_task_hash IS NULL)
           OR (terminal.terminal_state='exhausted'
                AND exhausted.task_hash IS NULL))
          {plan_clause}
        LIMIT 1
        """,
        parameters,
    ).fetchone()
    invalid_edge = conn.execute(
        f"""
        SELECT 1
        FROM audit_task_edges_v2 edge
        JOIN audit_task_bindings_v2 binding
          ON binding.task_hash=edge.parent_task_hash
        LEFT JOIN audit_l2_valid_split_families_v2 split
          ON split.parent_task_hash=edge.parent_task_hash
         AND (split.child0_task_hash=edge.child_task_hash
              OR split.child1_task_hash=edge.child_task_hash)
        WHERE split.parent_task_hash IS NULL {plan_clause}
        LIMIT 1
        """,
        parameters,
    ).fetchone()
    invalid_authority = conn.execute(
        f"""
        SELECT 1
        FROM audit_l2_terminal_transition_authority_v2 authority
        JOIN audit_task_bindings_v2 binding
          ON binding.task_hash=authority.parent_task_hash
        LEFT JOIN audit_l2_valid_split_families_v2 split
          ON split.parent_task_hash=authority.parent_task_hash
        LEFT JOIN audit_l2_valid_exhaustions_v2 exhausted
          ON exhausted.task_hash=authority.parent_task_hash
        WHERE ((authority.transition_kind='split'
                AND split.parent_task_hash IS NULL)
           OR (authority.transition_kind='exhaust'
                AND exhausted.task_hash IS NULL))
          {plan_clause}
        LIMIT 1
        """,
        parameters,
    ).fetchone()
    return all(
        value is None for value in (
            invalid_binding, invalid_terminal, invalid_fact,
            invalid_edge, invalid_authority,
        )
    )


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


def compare_and_set_metadata_shadow_outbox(
    conn,
    outbox_id,
    *,
    expected_state,
    expected_fence,
    new_state,
    new_fence,
    claim_token=None,
    lease_until=None,
    transition_now=None,
):
    """Apply only claim, expired reclaim, or guarded terminal publication."""
    if (
        type(expected_fence) is not int
        or expected_fence < 0
        or type(new_fence) is not int
        or new_fence != expected_fence + 1
    ):
        raise ValueError("metadata shadow fence must increase by exactly one")
    if (expected_state, new_state) not in {
        ("pending", "claimed"),
        ("claimed", "claimed"),
        ("claimed", "done"),
    }:
        raise ValueError("metadata shadow transition is closed")
    if new_state == "claimed":
        if not isinstance(claim_token, str) or not claim_token or not isinstance(
            lease_until, str
        ) or not lease_until:
            raise ValueError("claimed metadata work requires token and lease")
    elif claim_token is not None or lease_until is not None:
        raise ValueError("unclaimed metadata work cannot retain token or lease")
    guard = _FENCE_GUARDS.get(id(conn))
    if guard is None:
        raise AuditMigrationError("fenced CAS is not initialized for connection")

    def execute_update():
        cursor = conn.execute(
            """
            UPDATE audit_metadata_outbox_v2
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
        if cursor.rowcount != 1:
            raise StaleFence("metadata shadow state or fence is stale")
        return True

    if (expected_state, new_state) == ("claimed", "done"):
        if (
            not guard["active"]
            or guard["metadata_operation"] != "publish"
            or guard["metadata_outbox_id"] != outbox_id
            or guard["metadata_claim_token"] is None
            or guard["metadata_claim_fence"] != expected_fence
        ):
            raise StaleFence("metadata terminal settlement lacks publish guard")
        return execute_update()

    now = transition_now or _utc_now()
    _metadata_timestamp(now)
    operation = (
        "claim" if expected_state == "pending" else "reclaim"
    )
    with _metadata_transition_guard(
        conn,
        operation=operation,
        now=now,
        outbox_id=outbox_id,
        claim_token=claim_token,
        claim_fence=new_fence,
    ):
        return execute_update()
