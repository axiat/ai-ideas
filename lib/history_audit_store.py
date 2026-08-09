#!/usr/bin/env python3
"""Component migrations and fenced state changes for history audit v2."""

import dataclasses
import copy
import datetime
import hashlib
import json
import re
import sqlite3
import contextlib

try:
    from lib import direction_contract
    from lib import history_audit_plan
    from lib import history_contract_v2
except ImportError:
    import direction_contract
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
_L2_ADJUDICATION_GUARDS = {}
_RECEIPT_ISSUANCE_GUARDS = {}
_DIRECTION_VERDICT_GUARDS = {}
_CANDIDATE_BUDGET_GUARDS = {}
_STAGING_AUTHORITY_GUARDS = {}
_ATTEMPT_TERMINAL_GUARDS = {}
_ROUTER_SOURCE_GUARDS = {}
_VERIFIED_USAGE_AUTHORITY_GUARDS = {}
_L1_ATTEMPT_FACT_GUARDS = {}
_PAIR_RESULT_AUTHORITY_GUARDS = {}
_TEST_ROUTER_ROUND_AUTHORITIES = {}
_EXPECTED_MANAGED_SCHEMA = {}
_INVALID_AUTHORITY_JSON = object()
DIRECTION_VERDICT_PARSER_REVISION = "direction-verdict-tsv-v1"
MAX_DIRECTION_VERDICT_BYTES = 65536
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


_COVERAGE_SET_INTEGRITY_GUARDS_SQL = """
CREATE TABLE audit_coverage_set_integrity_probe(
  value INTEGER NOT NULL CHECK(value = 0)
);
INSERT INTO audit_coverage_set_integrity_probe(value)
SELECT 1 FROM audit_receipts receipt
WHERE (
    receipt.coverage_complete = 1
    OR receipt.final_status = 'complete_no_match'
  )
  AND (
    json_type(receipt.missing_ids) <> 'array'
    OR json_array_length(receipt.missing_ids) <> 0
    OR json_type(receipt.duplicate_ids) <> 'array'
    OR json_array_length(receipt.duplicate_ids) <> 0
    OR json_type(receipt.extra_ids) <> 'array'
    OR json_array_length(receipt.extra_ids) <> 0
  );
DROP TABLE audit_coverage_set_integrity_probe;
CREATE TRIGGER audit_receipts_coverage_set_guard
BEFORE INSERT ON audit_receipts
WHEN (
    NEW.coverage_complete = 1
    OR NEW.final_status = 'complete_no_match'
  )
  AND (
    json_type(NEW.missing_ids) <> 'array'
    OR json_array_length(NEW.missing_ids) <> 0
    OR json_type(NEW.duplicate_ids) <> 'array'
    OR json_array_length(NEW.duplicate_ids) <> 0
    OR json_type(NEW.extra_ids) <> 'array'
    OR json_array_length(NEW.extra_ids) <> 0
  )
BEGIN
  SELECT RAISE(ABORT, 'complete coverage cannot contain coverage set faults');
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


_L1_BATCH_DIRECTION_AUTHORITY_SQL = """
CREATE UNIQUE INDEX audit_direction_contracts_one_per_batch_v2
  ON audit_direction_contracts(run_id, batch_id);

CREATE TABLE audit_batch_direction_verdicts_v2(
  verdict_sha256 TEXT PRIMARY KEY CHECK(length(verdict_sha256) = 64),
  run_id TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  current_batch_ids_hash TEXT NOT NULL CHECK(length(current_batch_ids_hash) = 64),
  direction_id TEXT NOT NULL,
  contract_sha TEXT NOT NULL CHECK(length(contract_sha) = 64),
  validator_version TEXT NOT NULL,
  artifact_sha TEXT NOT NULL CHECK(length(artifact_sha) = 64),
  staging_candidate_id TEXT NOT NULL
    REFERENCES audit_batch_staging(staging_candidate_id),
  direction_fit TEXT NOT NULL CHECK(direction_fit IN ('in-scope','out-of-scope')),
  evidence_json TEXT NOT NULL,
  evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256) = 64),
  checked_at TEXT NOT NULL,
  UNIQUE(
    run_id, batch_id, snapshot_id, current_batch_ids_hash,
    direction_id, contract_sha, validator_version, artifact_sha,
    staging_candidate_id
  ),
  FOREIGN KEY(snapshot_id, current_batch_ids_hash)
    REFERENCES audit_snapshot_batch_sets(snapshot_id, current_batch_ids_hash),
  FOREIGN KEY(
    run_id, batch_id, direction_id, contract_sha,
    validator_version, artifact_sha
  ) REFERENCES audit_direction_contracts(
    run_id, batch_id, direction_id, contract_sha,
    validator_version, artifact_sha
  )
);
""" + _immutable_guards("audit_batch_direction_verdicts_v2") + """

CREATE TABLE audit_batch_direction_authority_probe(
  value INTEGER NOT NULL CHECK(value = 0)
);
INSERT INTO audit_batch_direction_authority_probe(value)
SELECT 1
FROM audit_activation_maps activation
JOIN audit_batch_staging staging
  ON staging.staging_candidate_id = activation.staging_candidate_id
JOIN audit_batch_pair_receipts receipt
  ON receipt.run_id = staging.run_id AND receipt.batch_id = staging.batch_id
JOIN audit_batch_pair_set_bindings binding
  ON binding.run_id = receipt.run_id
 AND binding.batch_id = receipt.batch_id
 AND binding.snapshot_id = receipt.snapshot_id
 AND binding.pair_plan_sha = activation.pair_plan_sha
 AND binding.pair_result_sha = activation.pair_result_sha
JOIN audit_snapshot_batch_sets batch_set
  ON batch_set.snapshot_id = binding.snapshot_id
 AND batch_set.current_batch_ids_hash = binding.current_batch_ids_hash
WHERE staging.staging_candidate_id GLOB 'stg-v2-*'
  AND NOT EXISTS (
    SELECT 1
    FROM audit_direction_contracts contract
    WHERE contract.run_id = staging.run_id
      AND contract.batch_id = staging.batch_id
      AND (
        SELECT count(*)
        FROM audit_batch_direction_verdicts_v2 verdict
        WHERE verdict.run_id = contract.run_id
          AND verdict.batch_id = contract.batch_id
          AND verdict.snapshot_id = batch_set.snapshot_id
          AND verdict.current_batch_ids_hash = batch_set.current_batch_ids_hash
          AND verdict.direction_id = contract.direction_id
          AND verdict.contract_sha = contract.contract_sha
          AND verdict.validator_version = contract.validator_version
          AND verdict.artifact_sha = contract.artifact_sha
      ) = batch_set.member_count
      AND NOT EXISTS (
        SELECT 1 FROM json_each(batch_set.member_ids_json) member
        WHERE NOT EXISTS (
          SELECT 1 FROM audit_batch_direction_verdicts_v2 verdict
          WHERE verdict.run_id = contract.run_id
            AND verdict.batch_id = contract.batch_id
            AND verdict.snapshot_id = batch_set.snapshot_id
            AND verdict.current_batch_ids_hash = batch_set.current_batch_ids_hash
            AND verdict.direction_id = contract.direction_id
            AND verdict.contract_sha = contract.contract_sha
            AND verdict.validator_version = contract.validator_version
            AND verdict.artifact_sha = contract.artifact_sha
            AND verdict.staging_candidate_id = member.value
            AND verdict.direction_fit = 'in-scope'
        )
      )
      AND NOT EXISTS (
        SELECT 1 FROM audit_batch_direction_verdicts_v2 veto
        WHERE veto.run_id = contract.run_id
          AND veto.batch_id = contract.batch_id
          AND veto.direction_fit = 'out-of-scope'
      )
  );
DROP TABLE audit_batch_direction_authority_probe;

CREATE TRIGGER audit_batch_direction_verdict_insert_guard
BEFORE INSERT ON audit_batch_direction_verdicts_v2
BEGIN
  SELECT CASE WHEN audit_direction_verdict_insert_allowed(
    NEW.verdict_sha256, NEW.run_id, NEW.batch_id, NEW.snapshot_id,
    NEW.current_batch_ids_hash, NEW.direction_id, NEW.contract_sha,
    NEW.validator_version, NEW.artifact_sha, NEW.staging_candidate_id,
    NEW.direction_fit, NEW.evidence_json, NEW.evidence_sha256, NEW.checked_at
  ) <> 1 THEN RAISE(ABORT, 'direction verdict requires host issuance') END;
  SELECT CASE WHEN audit_direction_verdict_valid(
    NEW.verdict_sha256, NEW.run_id, NEW.batch_id, NEW.snapshot_id,
    NEW.current_batch_ids_hash, NEW.direction_id, NEW.contract_sha,
    NEW.validator_version, NEW.artifact_sha, NEW.staging_candidate_id,
    NEW.direction_fit, NEW.evidence_json, NEW.evidence_sha256
  ) <> 1 THEN RAISE(ABORT, 'direction verdict canonical identity mismatch') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_batch_staging staging
    JOIN audit_snapshot_batch_sets batch_set
      ON batch_set.snapshot_id = NEW.snapshot_id
     AND batch_set.current_batch_ids_hash = NEW.current_batch_ids_hash
     AND batch_set.run_id = NEW.run_id
     AND batch_set.batch_id = NEW.batch_id
    JOIN json_each(batch_set.member_ids_json) member
      ON member.value = NEW.staging_candidate_id
    JOIN audit_direction_contracts contract
      ON contract.run_id = NEW.run_id
     AND contract.batch_id = NEW.batch_id
     AND contract.direction_id = NEW.direction_id
     AND contract.contract_sha = NEW.contract_sha
     AND contract.validator_version = NEW.validator_version
     AND contract.artifact_sha = NEW.artifact_sha
    WHERE staging.staging_candidate_id = NEW.staging_candidate_id
      AND staging.run_id = NEW.run_id
      AND staging.batch_id = NEW.batch_id
  ) THEN RAISE(ABORT, 'direction verdict batch authority mismatch') END;
END;

CREATE TRIGGER audit_activation_maps_batch_direction_guard
BEFORE INSERT ON audit_activation_maps
WHEN NEW.staging_candidate_id GLOB 'stg-v2-*'
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_batch_staging staging
    JOIN audit_batch_pair_receipts receipt
      ON receipt.run_id = staging.run_id AND receipt.batch_id = staging.batch_id
    JOIN audit_batch_pair_set_bindings binding
      ON binding.run_id = receipt.run_id
     AND binding.batch_id = receipt.batch_id
     AND binding.snapshot_id = receipt.snapshot_id
     AND binding.pair_plan_sha = NEW.pair_plan_sha
     AND binding.pair_result_sha = NEW.pair_result_sha
    JOIN audit_snapshot_batch_sets batch_set
      ON batch_set.snapshot_id = binding.snapshot_id
     AND batch_set.current_batch_ids_hash = binding.current_batch_ids_hash
    JOIN audit_direction_contracts contract
      ON contract.run_id = staging.run_id
     AND contract.batch_id = staging.batch_id
    WHERE staging.staging_candidate_id = NEW.staging_candidate_id
      AND (
        SELECT count(*)
        FROM audit_batch_direction_verdicts_v2 verdict
        WHERE verdict.run_id = contract.run_id
          AND verdict.batch_id = contract.batch_id
          AND verdict.snapshot_id = batch_set.snapshot_id
          AND verdict.current_batch_ids_hash = batch_set.current_batch_ids_hash
          AND verdict.direction_id = contract.direction_id
          AND verdict.contract_sha = contract.contract_sha
          AND verdict.validator_version = contract.validator_version
          AND verdict.artifact_sha = contract.artifact_sha
      ) = batch_set.member_count
      AND NOT EXISTS (
        SELECT 1 FROM json_each(batch_set.member_ids_json) member
        WHERE NOT EXISTS (
          SELECT 1 FROM audit_batch_direction_verdicts_v2 verdict
          WHERE verdict.run_id = contract.run_id
            AND verdict.batch_id = contract.batch_id
            AND verdict.snapshot_id = batch_set.snapshot_id
            AND verdict.current_batch_ids_hash = batch_set.current_batch_ids_hash
            AND verdict.direction_id = contract.direction_id
            AND verdict.contract_sha = contract.contract_sha
            AND verdict.validator_version = contract.validator_version
            AND verdict.artifact_sha = contract.artifact_sha
            AND verdict.staging_candidate_id = member.value
            AND verdict.direction_fit = 'in-scope'
        )
      )
      AND NOT EXISTS (
        SELECT 1 FROM audit_batch_direction_verdicts_v2 veto
        WHERE veto.run_id = contract.run_id
          AND veto.batch_id = contract.batch_id
          AND veto.direction_fit = 'out-of-scope'
      )
  ) THEN RAISE(ABORT, 'activation lacks unanimous batch direction authority') END;
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


_L2_SNAPSHOT_RECORDS_PER_SNAPSHOT_SQL = """
PRAGMA legacy_alter_table=ON;
DROP TRIGGER audit_l2_snapshot_records_v2_guard;
DROP TRIGGER audit_l2_snapshot_records_v2_immutable_update;
DROP TRIGGER audit_l2_snapshot_records_v2_immutable_delete;
CREATE TABLE audit_l2_snapshot_records_v2_replacement(
  snapshot_id TEXT PRIMARY KEY REFERENCES audit_snapshots(snapshot_id),
  records_sha TEXT NOT NULL CHECK(length(records_sha) = 64),
  records_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
INSERT INTO audit_l2_snapshot_records_v2_replacement(
  snapshot_id,records_sha,records_json,created_at
)
SELECT snapshot_id,records_sha,records_json,created_at
FROM audit_l2_snapshot_records_v2;
DROP TABLE audit_l2_snapshot_records_v2;
ALTER TABLE audit_l2_snapshot_records_v2_replacement
  RENAME TO audit_l2_snapshot_records_v2;
""" + _immutable_guards("audit_l2_snapshot_records_v2") + """
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
CREATE TABLE audit_l2_snapshot_records_per_snapshot_probe(
  value INTEGER NOT NULL CHECK(value=0)
);
INSERT INTO audit_l2_snapshot_records_per_snapshot_probe(value)
SELECT 1
FROM audit_l2_snapshot_records_v2 records
LEFT JOIN audit_snapshots snapshot
  ON snapshot.snapshot_id=records.snapshot_id
WHERE snapshot.snapshot_id IS NULL
   OR audit_l2_records_valid(
        records.records_json,records.records_sha,
        snapshot.expected_asset_ids_hash
      ) <> 1;
DROP TABLE audit_l2_snapshot_records_per_snapshot_probe;
PRAGMA legacy_alter_table=OFF;
"""


_L2_PLANS_BUDGET_GUARD_BEGIN = (
    "-- audit-optional-begin candidate-budget-authority-v1"
)
_L2_PLANS_BUDGET_GUARD_END = (
    "-- audit-optional-end candidate-budget-authority-v1"
)


_L2_PLANS_PER_RUN_SQL = """
PRAGMA legacy_alter_table=ON;
PRAGMA defer_foreign_keys=ON;
DROP TRIGGER audit_l2_plans_v2_canonical_guard;
DROP TRIGGER IF EXISTS audit_l2_plans_v2_candidate_budget_guard;
DROP TRIGGER audit_l2_plans_v2_immutable_update;
DROP TRIGGER audit_l2_plans_v2_immutable_delete;
CREATE TABLE audit_l2_plans_v2_replacement(
  plan_sha TEXT PRIMARY KEY CHECK(length(plan_sha) = 64),
  run_id TEXT NOT NULL UNIQUE REFERENCES audit_run_manifests(run_id),
  candidate_id TEXT NOT NULL UNIQUE REFERENCES audit_batch_staging(staging_candidate_id),
  candidate_hash TEXT NOT NULL CHECK(length(candidate_hash) = 64),
  snapshot_id TEXT NOT NULL UNIQUE REFERENCES audit_snapshots(snapshot_id),
  snapshot_hash TEXT NOT NULL CHECK(length(snapshot_hash) = 64),
  shard_plan_sha TEXT NOT NULL CHECK(length(shard_plan_sha) = 64),
  budget_policy_sha TEXT NOT NULL CHECK(length(budget_policy_sha) = 64),
  intent TEXT NOT NULL,
  plan_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TRIGGER audit_l2_plans_v2_immutable_update
BEFORE UPDATE ON audit_l2_plans_v2_replacement
BEGIN
  SELECT RAISE(ABORT, 'audit_l2_plans_v2 is immutable');
END;
CREATE TRIGGER audit_l2_plans_v2_immutable_delete
BEFORE DELETE ON audit_l2_plans_v2_replacement
BEGIN
  SELECT RAISE(ABORT, 'audit_l2_plans_v2 is immutable');
END;
CREATE TRIGGER audit_l2_plans_v2_canonical_guard
BEFORE INSERT ON audit_l2_plans_v2_replacement
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
INSERT INTO audit_l2_plans_v2_replacement(
  plan_sha,run_id,candidate_id,candidate_hash,snapshot_id,snapshot_hash,
  shard_plan_sha,budget_policy_sha,intent,plan_json,created_at
)
SELECT plan_sha,run_id,candidate_id,candidate_hash,snapshot_id,snapshot_hash,
       shard_plan_sha,budget_policy_sha,intent,plan_json,created_at
FROM audit_l2_plans_v2;
DROP TABLE audit_l2_plans_v2;
ALTER TABLE audit_l2_plans_v2_replacement RENAME TO audit_l2_plans_v2;
""" + _L2_PLANS_BUDGET_GUARD_BEGIN + """
CREATE TRIGGER audit_l2_plans_v2_candidate_budget_guard
BEFORE INSERT ON audit_l2_plans_v2
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_candidate_budget_receipts_v2 receipt
    WHERE receipt.run_id=NEW.run_id
      AND receipt.batch_id=json_extract(NEW.plan_json, '$.batch_id')
      AND receipt.intent=NEW.intent
      AND receipt.plan_sha=NEW.plan_sha
      AND receipt.budget_policy_sha=NEW.budget_policy_sha
      AND receipt.candidate_ids_json=
          json_extract(NEW.plan_json, '$.snapshot.current_batch_ids')
      AND receipt.requested_candidates=
          json_array_length(json_extract(
            NEW.plan_json, '$.snapshot.current_batch_ids'
          ))
      AND receipt.round_candidate_limit=json_extract(
        NEW.plan_json,
        '$.budget_policy.intents.' || NEW.intent || '.round.candidates'
      )
      AND receipt.decision='accepted'
  ) THEN RAISE(ABORT, 'L2 plan lacks exact candidate budget authority') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_candidate_route_facts_v2 route
    WHERE route.run_id=NEW.run_id
      AND route.candidate_id=NEW.candidate_id
      AND route.intent=NEW.intent
      AND route.matched_rule_ids_json=
          json_extract(NEW.plan_json, '$.matched_router_rule_ids')
      AND route.risk_policy_version=
          json_extract(NEW.plan_json, '$.risk_policy_version')
  ) THEN RAISE(ABORT, 'L2 plan selected route identity mismatch') END;
END;
""" + _L2_PLANS_BUDGET_GUARD_END + """

DROP TRIGGER audit_shard_plans_immutable_update;
DROP TRIGGER audit_shard_plans_immutable_delete;
CREATE TABLE audit_shard_plans_replacement(
  shard_plan_sha TEXT NOT NULL CHECK(length(shard_plan_sha) = 64),
  run_id TEXT NOT NULL REFERENCES audit_run_manifests(run_id),
  snapshot_id TEXT NOT NULL REFERENCES audit_snapshots(snapshot_id),
  expected_asset_ids_hash TEXT NOT NULL CHECK(length(expected_asset_ids_hash) = 64),
  plan_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(run_id,shard_plan_sha)
);
CREATE TRIGGER audit_shard_plans_immutable_update
BEFORE UPDATE ON audit_shard_plans_replacement
BEGIN
  SELECT RAISE(ABORT, 'audit_shard_plans is immutable');
END;
CREATE TRIGGER audit_shard_plans_immutable_delete
BEFORE DELETE ON audit_shard_plans_replacement
BEGIN
  SELECT RAISE(ABORT, 'audit_shard_plans is immutable');
END;
INSERT INTO audit_shard_plans_replacement(
  shard_plan_sha,run_id,snapshot_id,expected_asset_ids_hash,plan_json,created_at
)
SELECT shard_plan_sha,run_id,snapshot_id,expected_asset_ids_hash,plan_json,created_at
FROM audit_shard_plans;
DROP TABLE audit_shard_plans;
ALTER TABLE audit_shard_plans_replacement RENAME TO audit_shard_plans;
PRAGMA defer_foreign_keys=OFF;
PRAGMA legacy_alter_table=OFF;
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


_CANDIDATE_ROUTE_OBSERVATION_BOUNDARY_SQL = """
CREATE TABLE audit_candidate_route_observation_boundaries_v2(
  run_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  route_fact_sha256 TEXT NOT NULL,
  observation_scope TEXT NOT NULL CHECK(observation_scope='host_issued_shadow'),
  production_authority INTEGER NOT NULL CHECK(production_authority=0),
  boundary_sha256 TEXT NOT NULL UNIQUE CHECK(length(boundary_sha256)=64),
  created_at TEXT NOT NULL,
  PRIMARY KEY(run_id, candidate_id),
  FOREIGN KEY(run_id, candidate_id, route_fact_sha256)
    REFERENCES audit_candidate_route_facts_v2(
      run_id, candidate_id, fact_sha256
    )
);
""" + _immutable_guards(
    "audit_candidate_route_observation_boundaries_v2",
) + """
CREATE TRIGGER audit_candidate_route_observation_boundaries_v2_guard
BEFORE INSERT ON audit_candidate_route_observation_boundaries_v2
BEGIN
  SELECT CASE WHEN audit_candidate_route_observation_insert_allowed(
    NEW.run_id, NEW.candidate_id, NEW.route_fact_sha256,
    NEW.observation_scope, NEW.production_authority,
    NEW.boundary_sha256, NEW.created_at
  ) <> 1 THEN RAISE(ABORT, 'candidate route observation requires host authority') END;
END;
"""


_L2_ADJUDICATION_AUTHORITY_SQL = """
CREATE TABLE audit_l2_adjudication_generations_v2(
  generation_id TEXT PRIMARY KEY CHECK(length(generation_id)=64),
  plan_sha TEXT NOT NULL REFERENCES audit_l2_plans_v2(plan_sha),
  material_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE audit_l2_derived_task_authority_v2(
  task_hash TEXT PRIMARY KEY REFERENCES audit_logical_tasks(task_hash),
  generation_id TEXT NOT NULL
    REFERENCES audit_l2_adjudication_generations_v2(generation_id),
  plan_sha TEXT NOT NULL REFERENCES audit_l2_plans_v2(plan_sha),
  stage TEXT NOT NULL CHECK(stage IN ('detail','reduce')),
  authority_json TEXT NOT NULL,
  authority_sha256 TEXT NOT NULL UNIQUE CHECK(length(authority_sha256)=64),
  created_at TEXT NOT NULL
);
""" + _immutable_guards(
    "audit_l2_adjudication_generations_v2",
    "audit_l2_derived_task_authority_v2",
) + """
CREATE TRIGGER audit_l2_adjudication_generations_v2_guard
BEFORE INSERT ON audit_l2_adjudication_generations_v2
BEGIN
  SELECT CASE WHEN audit_l2_adjudication_generation_insert_allowed(
    NEW.generation_id, NEW.plan_sha, NEW.material_json, NEW.created_at
  )<>1 THEN RAISE(ABORT, 'adjudication generation requires host authority') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_l2_plans_v2 plan
    WHERE plan.plan_sha=NEW.plan_sha
      AND audit_l2_adjudication_generation_valid(
        plan.plan_json, NEW.material_json, NEW.generation_id
      )=1
  ) THEN RAISE(ABORT, 'adjudication generation identity mismatch') END;
END;
CREATE TRIGGER audit_l2_derived_task_authority_v2_guard
BEFORE INSERT ON audit_l2_derived_task_authority_v2
BEGIN
  SELECT CASE WHEN audit_l2_derived_authority_insert_allowed(
    NEW.task_hash, NEW.generation_id, NEW.plan_sha, NEW.stage,
    NEW.authority_json, NEW.authority_sha256, NEW.created_at
  )<>1 THEN RAISE(ABORT, 'derived task authority requires host authority') END;
END;
CREATE VIEW audit_l2_valid_adjudication_task_authority_v2 AS
SELECT task.task_hash
FROM audit_logical_tasks task
JOIN audit_task_bindings_v2 binding ON binding.task_hash=task.task_hash
JOIN audit_l2_task_inputs_v2 input ON input.task_hash=task.task_hash
JOIN audit_l2_derived_task_authority_v2 authority
  ON authority.task_hash=task.task_hash AND authority.stage=task.stage
JOIN audit_l2_adjudication_generations_v2 generation
  ON generation.generation_id=authority.generation_id
 AND generation.plan_sha=authority.plan_sha
JOIN audit_l2_plans_v2 plan ON plan.plan_sha=authority.plan_sha
JOIN audit_l2_snapshot_records_v2 records
  ON records.snapshot_id=binding.snapshot_id
WHERE audit_l2_derived_task_valid(
  plan.plan_json, records.records_json, generation.material_json,
  authority.authority_json,
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
    'request_sha', input.request_sha,
    'request_text', input.request_text,
    'item_ids_json', input.item_ids_json,
    'authority_sha', authority.authority_sha256
  )
)=1
AND NOT EXISTS (
  SELECT 1
  FROM json_each(authority.authority_json, '$.source_task_hashes') source
  LEFT JOIN json_each(
    authority.authority_json, '$.source_settlement_hashes'
  ) source_sha ON source_sha.key=source.key
  LEFT JOIN audit_task_settlements_v2 settlement
    ON settlement.task_hash=source.value
  LEFT JOIN audit_logical_tasks source_task
    ON source_task.task_hash=source.value
  WHERE settlement.task_hash IS NULL
     OR settlement.settlement_kind<>'equal'
     OR settlement.settlement_sha256<>source_sha.value
     OR (task.stage='detail' AND source_task.stage<>'map')
     OR (task.stage='reduce' AND source_task.stage<>'detail')
);
CREATE VIEW audit_l2_valid_runtime_task_authority_v2 AS
SELECT task_hash FROM audit_l2_valid_task_authority_v2
UNION
SELECT task_hash FROM audit_l2_valid_adjudication_task_authority_v2;
CREATE TABLE audit_l2_adjudication_upgrade_probe(
  value INTEGER NOT NULL CHECK(value=0)
);
INSERT INTO audit_l2_adjudication_upgrade_probe(value)
SELECT 1
FROM audit_logical_tasks task
LEFT JOIN audit_l2_derived_task_authority_v2 authority
  ON authority.task_hash=task.task_hash
WHERE task.stage IN ('detail','reduce') AND authority.task_hash IS NULL;
DROP TABLE audit_l2_adjudication_upgrade_probe;
DROP TRIGGER audit_logical_tasks_l2_insert_authority_guard_v2;
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
        OR audit_l2_adjudication_task_insert_allowed(
          NEW.task_hash, NEW.run_id, NEW.stage,
          NEW.staging_candidate_id, NEW.input_id
        )=1
      )
  ) THEN RAISE(ABORT, 'forged task authority') END;
END;
DROP TRIGGER audit_task_bindings_v2_full_authority_guard;
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
      AND (
        audit_l2_binding_authority_valid(
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
        OR audit_l2_adjudication_binding_insert_allowed(
          NEW.task_hash, NEW.plan_sha, NEW.snapshot_id, NEW.snapshot_hash,
          NEW.shard_input_sha, NEW.assigned_item_ids_json,
          NEW.frozen_records_json, NEW.provider_pool_json,
          NEW.parent_task_hash, NEW.split_depth, NEW.created_at
        )=1
      )
  ) THEN RAISE(ABORT, 'forged task binding authority') END;
END;
DROP TRIGGER audit_l2_task_inputs_v2_guard;
CREATE TRIGGER audit_l2_task_inputs_v2_guard
BEFORE INSERT ON audit_l2_task_inputs_v2
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_task_bindings_v2 binding
    JOIN audit_l2_plans_v2 plan ON plan.plan_sha=binding.plan_sha
    WHERE binding.task_hash=NEW.task_hash
      AND NEW.input_id=(
        SELECT task.input_id FROM audit_logical_tasks task
        WHERE task.task_hash=NEW.task_hash
      )
      AND NEW.request_sha=binding.shard_input_sha
      AND NEW.item_ids_json=binding.assigned_item_ids_json
      AND (
        audit_l2_task_input_valid(
          plan.plan_json, binding.parent_task_hash, NEW.input_id,
          NEW.request_text, NEW.request_sha, NEW.item_ids_json
        )=1
        OR audit_l2_adjudication_input_insert_allowed(
          NEW.task_hash, NEW.input_id, NEW.request_sha, NEW.request_text,
          NEW.item_ids_json, NEW.created_at
        )=1
      )
  ) THEN RAISE(ABORT, 'L2 task input mismatch') END;
END;
DROP TRIGGER audit_l2_task_inputs_v2_full_authority_guard;
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
      AND (
        audit_l2_input_authority_valid(
          plan.plan_json,
          json_object(
            'task_hash', task.task_hash, 'stage', task.stage,
            'candidate_id', task.staging_candidate_id,
            'input_id', NEW.input_id, 'task_input_id', task.input_id,
            'parent_input_id', parent_task.input_id,
            'plan_sha', binding.plan_sha,
            'parent_hash', binding.parent_task_hash,
            'request_sha', NEW.request_sha,
            'request_text', NEW.request_text,
            'item_ids_json', NEW.item_ids_json
          )
        )=1
        OR audit_l2_adjudication_input_insert_allowed(
          NEW.task_hash, NEW.input_id, NEW.request_sha, NEW.request_text,
          NEW.item_ids_json, NEW.created_at
        )=1
      )
  ) THEN RAISE(ABORT, 'forged task input authority') END;
END;
DROP TRIGGER audit_task_attempts_full_task_authority_guard;
CREATE TRIGGER audit_task_attempts_full_task_authority_guard
BEFORE INSERT ON audit_task_attempts
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM (
      SELECT task_hash FROM audit_l2_valid_task_authority_v2
      UNION
      SELECT task_hash FROM audit_l2_valid_adjudication_task_authority_v2
    ) valid
    JOIN audit_task_bindings_v2 binding ON binding.task_hash=valid.task_hash
    JOIN audit_l2_plans_v2 plan ON plan.plan_sha=binding.plan_sha
    WHERE valid.task_hash=NEW.task_hash
      AND audit_l2_attempt_capability_valid(
        plan.plan_json, binding.provider_pool_json, NEW.provenance_json
      )=1
  ) THEN RAISE(ABORT, 'attempt lacks validated task authority') END;
END;
DROP VIEW audit_l2_valid_exhaustions_v2;
CREATE VIEW audit_l2_valid_exhaustions_v2 AS
SELECT task.task_hash
FROM (
  SELECT task_hash FROM audit_l2_valid_task_authority_v2
  UNION
  SELECT task_hash FROM audit_l2_valid_adjudication_task_authority_v2
) valid
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
"""


_RECEIPT_ISSUANCE_AUTHORITY_SQL = """
CREATE TABLE audit_receipt_issuances_v2(
  issuance_id TEXT PRIMARY KEY CHECK(length(issuance_id)=64),
  receipt_id TEXT NOT NULL UNIQUE CHECK(length(receipt_id)=64),
  receipt_material_sha256 TEXT NOT NULL UNIQUE
    CHECK(length(receipt_material_sha256)=64),
  authority_kind TEXT NOT NULL CHECK(authority_kind IN ('l1','l2')),
  provenance_json TEXT NOT NULL,
  provenance_sha256 TEXT NOT NULL UNIQUE CHECK(length(provenance_sha256)=64),
  issued_at TEXT NOT NULL,
  FOREIGN KEY(receipt_id) REFERENCES audit_receipts(minimum_receipt_sha)
    DEFERRABLE INITIALLY DEFERRED
);
""" + _immutable_guards("audit_receipt_issuances_v2") + """
CREATE TRIGGER audit_receipt_issuances_v2_guard
BEFORE INSERT ON audit_receipt_issuances_v2
BEGIN
  SELECT CASE WHEN audit_receipt_issuance_insert_allowed(
    NEW.issuance_id, NEW.receipt_id, NEW.receipt_material_sha256,
    NEW.authority_kind, NEW.provenance_json, NEW.provenance_sha256,
    NEW.issued_at
  )<>1 THEN RAISE(ABORT, 'receipt issuance requires host authority') END;
END;
CREATE TABLE audit_receipt_issuance_upgrade_probe(
  value INTEGER NOT NULL CHECK(value=0)
);
INSERT INTO audit_receipt_issuance_upgrade_probe(value)
SELECT 1
FROM audit_receipts receipt
LEFT JOIN audit_receipt_issuances_v2 issuance
  ON issuance.receipt_id=receipt.minimum_receipt_sha
WHERE issuance.receipt_id IS NULL
   OR audit_receipt_row_valid(
      receipt.manifest_schema_version, receipt.canonical_codec_version,
      receipt.run_id, receipt.plan_hash, receipt.candidate_hash,
      receipt.snapshot_id, receipt.snapshot_hash,
      receipt.history_as_of_watermark,
      receipt.current_batch_id_namespace, receipt.current_batch_ids_hash,
      receipt.exclusion_policy_sha, receipt.expected_asset_ids_hash,
      receipt.observed_asset_ids_hash, receipt.missing_ids,
      receipt.duplicate_ids, receipt.extra_ids, receipt.invalid_schema,
      receipt.invalid_anchor, receipt.truncated,
      receipt.provider_pools_ordered,
      receipt.provider_capability_profile_hashes,
      receipt.capacity_profile_id, receipt.semantic_policy_profile_id,
      receipt.risk_policy_version, receipt.matched_router_rule_ids,
      receipt.settlement_policy_sha, receipt.shard_plan_sha,
      receipt.logical_task_hashes, receipt.attempt_manifest_hashes,
      receipt.raw_request_output_cas_hashes,
      receipt.minimum_receipt_sha, receipt.coverage_complete,
      receipt.adjudication_complete, receipt.semantic_policy_qualified,
      receipt.no_match_basis, receipt.final_status,
      receipt.stage_reason_code, receipt.evidence_anchors
   )<>1;
DROP TABLE audit_receipt_issuance_upgrade_probe;
DROP TRIGGER audit_receipts_release_and_identity_guard;
CREATE TRIGGER audit_receipts_release_and_identity_guard
BEFORE INSERT ON audit_receipts
BEGIN
  SELECT CASE WHEN NEW.final_status = 'complete_no_match' AND NOT (
    NEW.coverage_complete = 1
    AND NEW.adjudication_complete = 1
    AND NEW.semantic_policy_qualified = 1
  ) THEN RAISE(ABORT, 'complete_no_match release gates are incomplete') END;
  SELECT CASE WHEN (
    audit_receipt_insert_allowed(
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
    )<>1
    OR NOT EXISTS (
      SELECT 1 FROM audit_receipt_issuances_v2 issuance
      WHERE issuance.receipt_id=NEW.minimum_receipt_sha
        AND issuance.receipt_material_sha256=audit_receipt_material_sha()
        AND issuance.issuance_id=audit_receipt_issuance_id()
    )
  ) THEN RAISE(ABORT, 'receipt lacks durable host issuance') END;
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
    SELECT 1 FROM audit_run_manifests run
    WHERE run.run_id=NEW.run_id AND run.plan_hash=NEW.plan_hash
  ) THEN RAISE(ABORT, 'receipt run and plan identity mismatch') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_snapshots snapshot
    WHERE snapshot.snapshot_id=NEW.snapshot_id
      AND snapshot.snapshot_hash=NEW.snapshot_hash
      AND snapshot.history_as_of_watermark=NEW.history_as_of_watermark
      AND snapshot.current_batch_id_namespace=NEW.current_batch_id_namespace
      AND snapshot.current_batch_ids_hash=NEW.current_batch_ids_hash
      AND snapshot.exclusion_policy_sha=NEW.exclusion_policy_sha
      AND snapshot.expected_asset_ids_hash=NEW.expected_asset_ids_hash
  ) THEN RAISE(ABORT, 'receipt frozen snapshot identity mismatch') END;
END;
"""


_RUNTIME_USAGE_AUTHORITY_SQL = """
CREATE TABLE audit_runtime_usage_authority_upgrade_probe(
  value INTEGER NOT NULL CHECK(value=0)
);
INSERT INTO audit_runtime_usage_authority_upgrade_probe(value)
SELECT 1
FROM audit_runtime_budget_settlements_v2 settlement
WHERE settlement.usage_verified<>0
   OR settlement.actual_json IS NOT NULL;
DROP TABLE audit_runtime_usage_authority_upgrade_probe;

DROP TRIGGER audit_runtime_budget_settlements_v2_owner_guard;
CREATE TRIGGER audit_runtime_budget_settlements_v2_owner_guard
BEFORE INSERT ON audit_runtime_budget_settlements_v2
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_task_attempts attempt
    WHERE attempt.attempt_id=NEW.attempt_id
  ) THEN RAISE(ABORT, 'budget settlement attempt is missing') END;
  SELECT CASE WHEN NEW.usage_verified<>0 OR NEW.actual_json IS NOT NULL
    THEN RAISE(ABORT, 'provider usage receipt authority is unavailable') END;
  SELECT CASE WHEN audit_l2_budget_settlement_valid(
    NEW.usage_verified, NEW.actual_json
  )<>1 THEN RAISE(ABORT, 'budget settlement usage is invalid') END;
END;
"""


_ATTEMPT_TERMINAL_AUTHORITY_SQL = """
CREATE TABLE audit_attempt_terminal_authority_upgrade_probe(
  value INTEGER NOT NULL CHECK(value=0)
);
INSERT INTO audit_attempt_terminal_authority_upgrade_probe(value)
SELECT 1
FROM audit_attempt_completions_v2 completion
WHERE audit_completion_usage_valid(completion.usage_json)<>1;
INSERT INTO audit_attempt_terminal_authority_upgrade_probe(value)
SELECT 1
FROM audit_attempt_completions_v2 completion
JOIN audit_attempt_cost_settlements_v2 cost USING(attempt_id)
WHERE cost.outcome='cancelled';
INSERT INTO audit_attempt_terminal_authority_upgrade_probe(value)
SELECT 1
FROM audit_runtime_budget_settlements_v2 budget
LEFT JOIN audit_attempt_completions_v2 completion USING(attempt_id)
LEFT JOIN audit_attempt_cost_settlements_v2 cost USING(attempt_id)
WHERE budget.usage_verified<>0
   OR budget.actual_json IS NOT NULL
   OR COALESCE((
     (
       completion.attempt_id IS NOT NULL
       AND budget.created_at=completion.completed_at
       AND (cost.attempt_id IS NULL OR cost.outcome<>'cancelled')
     )
     OR
     (
       completion.attempt_id IS NULL
       AND cost.outcome='cancelled'
       AND cost.billing_state='unknown'
       AND cost.usage_source='reservation'
       AND budget.created_at=cost.completed_at
     )
   ), 0)<>1;
DROP TABLE audit_attempt_terminal_authority_upgrade_probe;

CREATE TRIGGER audit_attempt_completions_v2_terminal_authority_guard
BEFORE INSERT ON audit_attempt_completions_v2
BEGIN
  SELECT CASE WHEN audit_attempt_completion_insert_allowed(
    NEW.attempt_id, NEW.output_cas_object_id, NEW.outcome,
    NEW.normalized_result_json, NEW.usage_json, NEW.completed_at
  )<>1 THEN RAISE(ABORT, 'attempt completion requires host authority') END;
  SELECT CASE WHEN audit_completion_usage_valid(NEW.usage_json)<>1
    THEN RAISE(ABORT, 'attempt completion usage authority is invalid') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM audit_attempt_cost_settlements_v2 cost
    WHERE cost.attempt_id=NEW.attempt_id AND cost.outcome='cancelled'
  ) THEN RAISE(ABORT, 'attempt completion conflicts with cancellation') END;
END;

CREATE TRIGGER audit_attempt_cost_settlements_v2_completion_exclusion_guard
BEFORE INSERT ON audit_attempt_cost_settlements_v2
WHEN NEW.outcome='cancelled'
BEGIN
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM audit_attempt_completions_v2 completion
    WHERE completion.attempt_id=NEW.attempt_id
  ) THEN RAISE(ABORT, 'attempt cancellation conflicts with completion') END;
END;

DROP TRIGGER audit_runtime_budget_settlements_v2_owner_guard;
CREATE TRIGGER audit_runtime_budget_settlements_v2_owner_guard
BEFORE INSERT ON audit_runtime_budget_settlements_v2
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_task_attempts attempt
    WHERE attempt.attempt_id=NEW.attempt_id
  ) THEN RAISE(ABORT, 'budget settlement attempt is missing') END;
  SELECT CASE WHEN NEW.usage_verified<>0 OR NEW.actual_json IS NOT NULL
    THEN RAISE(ABORT, 'provider usage receipt authority is unavailable') END;
  SELECT CASE WHEN audit_l2_budget_settlement_valid(
    NEW.usage_verified, NEW.actual_json
  )<>1 THEN RAISE(ABORT, 'budget settlement usage is invalid') END;
  SELECT CASE WHEN audit_runtime_budget_settlement_insert_allowed(
    NEW.attempt_id, NEW.usage_verified, NEW.actual_json, NEW.created_at
  )<>1 THEN RAISE(ABORT, 'budget settlement requires host authority') END;
  SELECT CASE WHEN NOT (
    EXISTS (
      SELECT 1 FROM audit_attempt_completions_v2 completion
      WHERE completion.attempt_id=NEW.attempt_id
        AND completion.completed_at=NEW.created_at
        AND NOT EXISTS (
          SELECT 1 FROM audit_attempt_cost_settlements_v2 cost
          WHERE cost.attempt_id=NEW.attempt_id AND cost.outcome='cancelled'
        )
    )
    OR
    EXISTS (
      SELECT 1 FROM audit_attempt_cost_settlements_v2 cost
      WHERE cost.attempt_id=NEW.attempt_id
        AND cost.outcome='cancelled'
        AND cost.billing_state='unknown'
        AND cost.usage_source='reservation'
        AND cost.completed_at=NEW.created_at
        AND NOT EXISTS (
          SELECT 1 FROM audit_attempt_completions_v2 completion
          WHERE completion.attempt_id=NEW.attempt_id
        )
    )
  ) THEN RAISE(ABORT, 'budget settlement lacks exact terminal authority') END;
END;
"""


_CANDIDATE_BUDGET_AUTHORITY_SQL = """
CREATE TABLE audit_candidate_budget_receipts_v2(
  decision_sha256 TEXT PRIMARY KEY CHECK(length(decision_sha256)=64),
  run_id TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  intent TEXT NOT NULL,
  plan_sha TEXT NOT NULL UNIQUE CHECK(length(plan_sha)=64),
  budget_policy_sha TEXT NOT NULL CHECK(length(budget_policy_sha)=64),
  candidate_ids_json TEXT NOT NULL,
  requested_candidates INTEGER NOT NULL CHECK(requested_candidates>=1),
  round_candidate_limit INTEGER NOT NULL CHECK(round_candidate_limit>=0),
  decision TEXT NOT NULL CHECK(decision IN ('accepted','rejected')),
  decided_at TEXT NOT NULL,
  UNIQUE(run_id, batch_id, intent, plan_sha),
  UNIQUE(run_id, intent),
  CHECK(
    (decision='accepted' AND requested_candidates<=round_candidate_limit)
    OR
    (decision='rejected' AND requested_candidates>round_candidate_limit)
  )
);
""" + _immutable_guards("audit_candidate_budget_receipts_v2") + """
CREATE TRIGGER audit_candidate_budget_receipts_v2_guard
BEFORE INSERT ON audit_candidate_budget_receipts_v2
BEGIN
  SELECT CASE WHEN audit_candidate_budget_receipt_insert_allowed(
    NEW.decision_sha256, NEW.run_id, NEW.batch_id, NEW.intent,
    NEW.plan_sha, NEW.budget_policy_sha, NEW.candidate_ids_json,
    NEW.requested_candidates, NEW.round_candidate_limit,
    NEW.decision, NEW.decided_at
  )<>1 THEN RAISE(ABORT, 'candidate budget receipt requires host authority') END;
END;
CREATE TRIGGER audit_candidate_route_cohorts_v2_budget_guard
BEFORE INSERT ON audit_candidate_route_cohorts_v2
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_candidate_budget_receipts_v2 receipt
    WHERE receipt.run_id=NEW.run_id
      AND receipt.batch_id=NEW.batch_id
      AND receipt.intent=NEW.intent
      AND receipt.candidate_ids_json=NEW.candidate_ids_json
      AND receipt.requested_candidates=json_array_length(NEW.candidate_ids_json)
      AND receipt.decision='accepted'
  ) THEN RAISE(ABORT, 'candidate route cohort lacks accepted budget authority') END;
END;
CREATE TRIGGER audit_l2_plans_v2_candidate_budget_guard
BEFORE INSERT ON audit_l2_plans_v2
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_candidate_budget_receipts_v2 receipt
    WHERE receipt.run_id=NEW.run_id
      AND receipt.batch_id=json_extract(NEW.plan_json, '$.batch_id')
      AND receipt.intent=NEW.intent
      AND receipt.plan_sha=NEW.plan_sha
      AND receipt.budget_policy_sha=NEW.budget_policy_sha
      AND receipt.candidate_ids_json=
          json_extract(NEW.plan_json, '$.snapshot.current_batch_ids')
      AND receipt.requested_candidates=
          json_array_length(json_extract(
            NEW.plan_json, '$.snapshot.current_batch_ids'
          ))
      AND receipt.round_candidate_limit=json_extract(
        NEW.plan_json,
        '$.budget_policy.intents.' || NEW.intent || '.round.candidates'
      )
      AND receipt.decision='accepted'
  ) THEN RAISE(ABORT, 'L2 plan lacks exact candidate budget authority') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_candidate_route_facts_v2 route
    WHERE route.run_id=NEW.run_id
      AND route.candidate_id=NEW.candidate_id
      AND route.intent=NEW.intent
      AND route.matched_rule_ids_json=
          json_extract(NEW.plan_json, '$.matched_router_rule_ids')
      AND route.risk_policy_version=
          json_extract(NEW.plan_json, '$.risk_policy_version')
  ) THEN RAISE(ABORT, 'L2 plan selected route identity mismatch') END;
END;
CREATE TABLE audit_candidate_budget_authority_probe(
  value INTEGER NOT NULL CHECK(value=0)
);
INSERT INTO audit_candidate_budget_authority_probe(value)
SELECT 1
FROM audit_l2_plans_v2 plan
WHERE NOT EXISTS (
  SELECT 1 FROM audit_candidate_budget_receipts_v2 receipt
  JOIN audit_candidate_route_facts_v2 route
    ON route.run_id=plan.run_id
   AND route.candidate_id=plan.candidate_id
   AND route.intent=plan.intent
  WHERE receipt.run_id=plan.run_id
    AND receipt.batch_id=json_extract(plan.plan_json, '$.batch_id')
    AND receipt.intent=plan.intent
    AND receipt.plan_sha=plan.plan_sha
    AND receipt.budget_policy_sha=plan.budget_policy_sha
    AND receipt.candidate_ids_json=
        json_extract(plan.plan_json, '$.snapshot.current_batch_ids')
    AND receipt.requested_candidates=json_array_length(
      json_extract(plan.plan_json, '$.snapshot.current_batch_ids')
    )
    AND receipt.round_candidate_limit=json_extract(
      plan.plan_json,
      '$.budget_policy.intents.' || plan.intent || '.round.candidates'
    )
    AND receipt.decision='accepted'
    AND route.matched_rule_ids_json=
        json_extract(plan.plan_json, '$.matched_router_rule_ids')
    AND route.risk_policy_version=
        json_extract(plan.plan_json, '$.risk_policy_version')
);
DROP TABLE audit_candidate_budget_authority_probe;
"""


_STAGING_AUTHORITY_SQL = """
CREATE TABLE audit_batch_staging_authorities_v2(
  authority_sha256 TEXT PRIMARY KEY CHECK(length(authority_sha256)=64),
  staging_candidate_id TEXT NOT NULL UNIQUE,
  run_id TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  candidate_hash TEXT NOT NULL CHECK(length(candidate_hash)=64),
  raw_artifact_sha TEXT NOT NULL CHECK(length(raw_artifact_sha)=64),
  source_order INTEGER NOT NULL CHECK(source_order>=0),
  authority_kind TEXT NOT NULL
    CHECK(authority_kind IN (
      'host_issued','migration_v2','migration_legacy'
    )),
  issued_at TEXT NOT NULL,
  FOREIGN KEY(staging_candidate_id)
    REFERENCES audit_batch_staging(staging_candidate_id)
    DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE audit_batch_staging_classification_probe(
  value INTEGER NOT NULL CHECK(value=0)
);
INSERT INTO audit_batch_staging_classification_probe(value)
SELECT 1
FROM audit_batch_staging staging
WHERE NOT EXISTS (
  SELECT 1
  FROM audit_snapshot_batch_sets batch_set
  JOIN json_each(batch_set.member_ids_json) member
    ON member.value=staging.staging_candidate_id
  WHERE batch_set.run_id=staging.run_id
    AND batch_set.batch_id=staging.batch_id
    AND length(staging.staging_candidate_id)=71
    AND substr(staging.staging_candidate_id,1,7)='stg-v2-'
    AND substr(staging.staging_candidate_id,8) NOT GLOB '*[^0-9a-f]*'
)
AND NOT EXISTS (
  SELECT 1 FROM audit_activation_maps activation
  WHERE activation.staging_candidate_id=staging.staging_candidate_id
    AND activation.raw_artifact_sha=staging.raw_artifact_sha
);
DROP TABLE audit_batch_staging_classification_probe;
INSERT INTO audit_batch_staging_authorities_v2(
  authority_sha256, staging_candidate_id, run_id, batch_id,
  candidate_hash, raw_artifact_sha, source_order, authority_kind, issued_at
)
SELECT audit_batch_staging_authority_sha(
         staging_candidate_id, run_id, batch_id, candidate_hash,
         raw_artifact_sha, source_order,
         CASE WHEN EXISTS (
           SELECT 1
           FROM audit_snapshot_batch_sets batch_set
           JOIN json_each(batch_set.member_ids_json) member
             ON member.value=audit_batch_staging.staging_candidate_id
           WHERE batch_set.run_id=audit_batch_staging.run_id
             AND batch_set.batch_id=audit_batch_staging.batch_id
             AND length(audit_batch_staging.staging_candidate_id)=71
             AND substr(audit_batch_staging.staging_candidate_id,1,7)='stg-v2-'
             AND substr(audit_batch_staging.staging_candidate_id,8)
                 NOT GLOB '*[^0-9a-f]*'
         ) THEN 'migration_v2' ELSE 'migration_legacy' END,
         created_at
       ),
       staging_candidate_id, run_id, batch_id, candidate_hash,
       raw_artifact_sha, source_order,
       CASE WHEN EXISTS (
         SELECT 1
         FROM audit_snapshot_batch_sets batch_set
         JOIN json_each(batch_set.member_ids_json) member
           ON member.value=audit_batch_staging.staging_candidate_id
         WHERE batch_set.run_id=audit_batch_staging.run_id
           AND batch_set.batch_id=audit_batch_staging.batch_id
           AND length(audit_batch_staging.staging_candidate_id)=71
           AND substr(audit_batch_staging.staging_candidate_id,1,7)='stg-v2-'
           AND substr(audit_batch_staging.staging_candidate_id,8)
               NOT GLOB '*[^0-9a-f]*'
       ) THEN 'migration_v2' ELSE 'migration_legacy' END,
       created_at
FROM audit_batch_staging;
""" + _immutable_guards("audit_batch_staging_authorities_v2") + """
CREATE TRIGGER audit_batch_staging_authorities_v2_guard
BEFORE INSERT ON audit_batch_staging_authorities_v2
BEGIN
  SELECT CASE WHEN audit_batch_staging_authority_insert_allowed(
    NEW.authority_sha256, NEW.staging_candidate_id, NEW.run_id, NEW.batch_id,
    NEW.candidate_hash, NEW.raw_artifact_sha, NEW.source_order,
    NEW.authority_kind, NEW.issued_at
  )<>1 THEN RAISE(ABORT, 'batch staging authority requires host issuance') END;
  SELECT CASE WHEN audit_batch_staging_authority_valid(
    NEW.authority_sha256, NEW.staging_candidate_id, NEW.run_id, NEW.batch_id,
    NEW.candidate_hash, NEW.raw_artifact_sha, NEW.source_order,
    NEW.authority_kind, NEW.issued_at
  )<>1 THEN RAISE(ABORT, 'batch staging authority identity mismatch') END;
END;
CREATE TRIGGER audit_batch_staging_host_insert_guard_v2
BEFORE INSERT ON audit_batch_staging
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_batch_staging_authorities_v2 authority
    WHERE authority.staging_candidate_id=NEW.staging_candidate_id
      AND authority.run_id=NEW.run_id
      AND authority.batch_id=NEW.batch_id
      AND authority.candidate_hash=NEW.candidate_hash
      AND authority.raw_artifact_sha=NEW.raw_artifact_sha
      AND authority.source_order=NEW.source_order
      AND authority.authority_kind='host_issued'
      AND authority.issued_at=NEW.created_at
  ) THEN RAISE(ABORT, 'batch staging row lacks host authority') END;
END;
CREATE TABLE audit_batch_staging_authority_probe(
  value INTEGER NOT NULL CHECK(value=0)
);
INSERT INTO audit_batch_staging_authority_probe(value)
SELECT 1 FROM audit_batch_staging staging
WHERE NOT EXISTS (
  SELECT 1 FROM audit_batch_staging_authorities_v2 authority
  WHERE authority.staging_candidate_id=staging.staging_candidate_id
    AND authority.run_id=staging.run_id
    AND authority.batch_id=staging.batch_id
    AND authority.candidate_hash=staging.candidate_hash
    AND authority.raw_artifact_sha=staging.raw_artifact_sha
    AND authority.source_order=staging.source_order
    AND authority.issued_at=staging.created_at
    AND audit_batch_staging_authority_valid(
      authority.authority_sha256, authority.staging_candidate_id,
      authority.run_id, authority.batch_id, authority.candidate_hash,
      authority.raw_artifact_sha, authority.source_order,
      authority.authority_kind, authority.issued_at
    )=1
);
DROP TABLE audit_batch_staging_authority_probe;

DROP TRIGGER IF EXISTS audit_batch_pairs_owner_and_order_guard;
CREATE TRIGGER audit_batch_pairs_owner_and_order_guard
BEFORE INSERT ON audit_batch_pairs
BEGIN
  SELECT CASE WHEN NEW.left_staging_candidate_id>=NEW.right_staging_candidate_id
    THEN RAISE(ABORT, 'batch pair endpoints must be canonically ordered') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_batch_staging left_item
    JOIN audit_batch_staging right_item
      ON right_item.staging_candidate_id=NEW.right_staging_candidate_id
    WHERE left_item.staging_candidate_id=NEW.left_staging_candidate_id
      AND left_item.run_id=NEW.run_id AND left_item.batch_id=NEW.batch_id
      AND right_item.run_id=NEW.run_id AND right_item.batch_id=NEW.batch_id
  ) THEN RAISE(ABORT, 'batch pair endpoints do not share one batch') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM audit_batch_staging_authorities_v2 authority
    WHERE authority.staging_candidate_id IN (
      NEW.left_staging_candidate_id, NEW.right_staging_candidate_id
    ) AND authority.authority_kind IN ('host_issued','migration_v2')
  ) AND NOT EXISTS (
    SELECT 1
    FROM audit_batch_staging_authorities_v2 left_authority
    JOIN audit_batch_staging_authorities_v2 right_authority
      ON right_authority.staging_candidate_id=NEW.right_staging_candidate_id
     AND right_authority.authority_kind IN ('host_issued','migration_v2')
    JOIN audit_batch_pair_receipts receipt
      ON receipt.run_id=NEW.run_id AND receipt.batch_id=NEW.batch_id
     AND receipt.pair_plan_sha=NEW.pair_plan_sha
     AND receipt.pair_result_sha=NEW.pair_result_sha
    WHERE left_authority.staging_candidate_id=NEW.left_staging_candidate_id
      AND left_authority.authority_kind IN ('host_issued','migration_v2')
  ) THEN RAISE(ABORT, 'strict batch pair lacks completed receipt authority') END;
END;

DROP TRIGGER IF EXISTS audit_batch_pairs_set_binding_guard;
CREATE TRIGGER audit_batch_pairs_set_binding_guard
BEFORE INSERT ON audit_batch_pairs
WHEN EXISTS (
  SELECT 1 FROM audit_batch_staging_authorities_v2 authority
  WHERE authority.staging_candidate_id=NEW.left_staging_candidate_id
    AND authority.authority_kind IN ('host_issued','migration_v2')
)
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_batch_pair_set_bindings binding
    JOIN audit_snapshot_batch_sets batch_set
      ON batch_set.snapshot_id=binding.snapshot_id
     AND batch_set.current_batch_ids_hash=binding.current_batch_ids_hash
    JOIN json_each(batch_set.member_ids_json) left_member
      ON left_member.value=NEW.left_staging_candidate_id
    JOIN json_each(batch_set.member_ids_json) right_member
      ON right_member.value=NEW.right_staging_candidate_id
    WHERE binding.run_id=NEW.run_id AND binding.batch_id=NEW.batch_id
      AND binding.pair_plan_sha=NEW.pair_plan_sha
      AND binding.pair_result_sha=NEW.pair_result_sha
      AND left_member.value<right_member.value
  ) THEN RAISE(ABORT, 'host batch pair exact set binding is missing') END;
END;
"""


_DIRECTION_GATE_AUTHORITY_SQL = """
CREATE TABLE audit_batch_direction_gates_v2(
  gate_sha256 TEXT PRIMARY KEY CHECK(length(gate_sha256)=64),
  run_id TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  current_batch_ids_hash TEXT NOT NULL CHECK(length(current_batch_ids_hash)=64),
  direction_id TEXT NOT NULL,
  contract_sha TEXT NOT NULL CHECK(length(contract_sha)=64),
  validator_version TEXT NOT NULL,
  artifact_sha TEXT NOT NULL CHECK(length(artifact_sha)=64),
  parser_revision TEXT NOT NULL
    CHECK(parser_revision='direction-verdict-tsv-v1'),
  raw_selector_artifact_sha256 TEXT NOT NULL
    CHECK(length(raw_selector_artifact_sha256)=64),
  member_count INTEGER NOT NULL CHECK(member_count>=1),
  candidate_mapping_json TEXT NOT NULL,
  verdict_set_json TEXT NOT NULL,
  verdict_set_sha256 TEXT NOT NULL CHECK(length(verdict_set_sha256)=64),
  verdict_tsv BLOB NOT NULL
    CHECK(typeof(verdict_tsv)='blob' AND length(verdict_tsv) BETWEEN 1 AND 65536),
  issued_at TEXT NOT NULL,
  UNIQUE(run_id,batch_id),
  FOREIGN KEY(snapshot_id,current_batch_ids_hash)
    REFERENCES audit_snapshot_batch_sets(snapshot_id,current_batch_ids_hash),
  FOREIGN KEY(
    run_id,batch_id,direction_id,contract_sha,validator_version,artifact_sha
  ) REFERENCES audit_direction_contracts(
    run_id,batch_id,direction_id,contract_sha,validator_version,artifact_sha
  )
);
CREATE TABLE audit_batch_direction_gate_bindings_v2(
  gate_sha256 TEXT NOT NULL,
  selector_id TEXT NOT NULL,
  source_order INTEGER NOT NULL CHECK(source_order>=0),
  staging_candidate_id TEXT NOT NULL,
  verdict_sha256 TEXT NOT NULL,
  PRIMARY KEY(gate_sha256,selector_id),
  UNIQUE(gate_sha256,source_order),
  UNIQUE(gate_sha256,staging_candidate_id),
  UNIQUE(gate_sha256,verdict_sha256),
  FOREIGN KEY(gate_sha256)
    REFERENCES audit_batch_direction_gates_v2(gate_sha256)
    DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY(staging_candidate_id)
    REFERENCES audit_batch_staging(staging_candidate_id),
  FOREIGN KEY(verdict_sha256)
    REFERENCES audit_batch_direction_verdicts_v2(verdict_sha256)
);
""" + _immutable_guards(
    "audit_batch_direction_gates_v2",
    "audit_batch_direction_gate_bindings_v2",
) + """

DROP TRIGGER IF EXISTS audit_batch_direction_verdict_insert_guard;
CREATE TRIGGER audit_batch_direction_verdict_insert_guard
BEFORE INSERT ON audit_batch_direction_verdicts_v2
BEGIN
  SELECT CASE WHEN audit_direction_verdict_insert_allowed(
    NEW.verdict_sha256,NEW.run_id,NEW.batch_id,NEW.snapshot_id,
    NEW.current_batch_ids_hash,NEW.direction_id,NEW.contract_sha,
    NEW.validator_version,NEW.artifact_sha,NEW.staging_candidate_id,
    NEW.direction_fit,NEW.evidence_json,NEW.evidence_sha256,NEW.checked_at
  )<>1 THEN RAISE(ABORT,'direction verdict requires batch gate issuance') END;
  SELECT CASE WHEN audit_direction_verdict_valid(
    NEW.verdict_sha256,NEW.run_id,NEW.batch_id,NEW.snapshot_id,
    NEW.current_batch_ids_hash,NEW.direction_id,NEW.contract_sha,
    NEW.validator_version,NEW.artifact_sha,NEW.staging_candidate_id,
    NEW.direction_fit,NEW.evidence_json,NEW.evidence_sha256
  )<>1 THEN RAISE(ABORT,'direction verdict canonical identity mismatch') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_snapshot_batch_sets batch_set
    JOIN json_each(batch_set.member_ids_json) member
      ON member.value=NEW.staging_candidate_id
    JOIN audit_batch_staging staging
      ON staging.staging_candidate_id=NEW.staging_candidate_id
     AND staging.run_id=NEW.run_id AND staging.batch_id=NEW.batch_id
    JOIN audit_batch_staging_authorities_v2 authority
      ON authority.staging_candidate_id=staging.staging_candidate_id
     AND authority.run_id=staging.run_id
     AND authority.batch_id=staging.batch_id
     AND authority.candidate_hash=staging.candidate_hash
     AND authority.raw_artifact_sha=staging.raw_artifact_sha
     AND authority.source_order=staging.source_order
     AND authority.issued_at=staging.created_at
     AND authority.authority_kind IN ('host_issued','migration_v2')
    JOIN audit_direction_contracts direction
      ON direction.run_id=NEW.run_id AND direction.batch_id=NEW.batch_id
     AND direction.direction_id=NEW.direction_id
     AND direction.contract_sha=NEW.contract_sha
     AND direction.validator_version=NEW.validator_version
     AND direction.artifact_sha=NEW.artifact_sha
    WHERE batch_set.run_id=NEW.run_id AND batch_set.batch_id=NEW.batch_id
      AND batch_set.snapshot_id=NEW.snapshot_id
      AND batch_set.current_batch_ids_hash=NEW.current_batch_ids_hash
  ) THEN RAISE(ABORT,'direction verdict strict batch authority mismatch') END;
END;

CREATE TRIGGER audit_batch_direction_gate_binding_insert_guard_v2
BEFORE INSERT ON audit_batch_direction_gate_bindings_v2
BEGIN
  SELECT CASE WHEN audit_direction_gate_binding_insert_allowed(
    NEW.gate_sha256,NEW.selector_id,NEW.source_order,
    NEW.staging_candidate_id,NEW.verdict_sha256
  )<>1 THEN RAISE(ABORT,'direction gate binding requires host issuance') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_batch_staging staging
    JOIN audit_batch_staging_authorities_v2 authority
      ON authority.staging_candidate_id=staging.staging_candidate_id
     AND authority.run_id=staging.run_id
     AND authority.batch_id=staging.batch_id
     AND authority.candidate_hash=staging.candidate_hash
     AND authority.raw_artifact_sha=staging.raw_artifact_sha
     AND authority.source_order=staging.source_order
     AND authority.issued_at=staging.created_at
     AND authority.authority_kind IN ('host_issued','migration_v2')
    JOIN audit_batch_direction_verdicts_v2 verdict
      ON verdict.verdict_sha256=NEW.verdict_sha256
     AND verdict.staging_candidate_id=staging.staging_candidate_id
    WHERE staging.staging_candidate_id=NEW.staging_candidate_id
      AND staging.source_order=NEW.source_order
      AND NEW.selector_id='I'||CAST(NEW.source_order+1 AS TEXT)
  ) THEN RAISE(ABORT,'direction gate binding identity mismatch') END;
END;

CREATE TRIGGER audit_batch_direction_gate_insert_guard_v2
BEFORE INSERT ON audit_batch_direction_gates_v2
BEGIN
  SELECT CASE WHEN audit_direction_gate_insert_allowed(
    NEW.gate_sha256,NEW.run_id,NEW.batch_id,NEW.snapshot_id,
    NEW.current_batch_ids_hash,NEW.direction_id,NEW.contract_sha,
    NEW.validator_version,NEW.artifact_sha,NEW.parser_revision,
    NEW.raw_selector_artifact_sha256,NEW.member_count,
    NEW.candidate_mapping_json,NEW.verdict_set_json,
    NEW.verdict_set_sha256,NEW.verdict_tsv,NEW.issued_at
  )<>1 THEN RAISE(ABORT,'direction gate requires host issuance') END;
  SELECT CASE WHEN audit_direction_gate_valid(
    NEW.gate_sha256,NEW.run_id,NEW.batch_id,NEW.snapshot_id,
    NEW.current_batch_ids_hash,NEW.direction_id,NEW.contract_sha,
    NEW.validator_version,NEW.artifact_sha,NEW.parser_revision,
    NEW.raw_selector_artifact_sha256,NEW.member_count,
    NEW.candidate_mapping_json,NEW.verdict_set_json,
    NEW.verdict_set_sha256,NEW.verdict_tsv,NEW.issued_at
  )<>1 THEN RAISE(ABORT,'direction gate canonical identity mismatch') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_snapshot_batch_sets batch_set
    JOIN audit_direction_contracts direction
      ON direction.run_id=NEW.run_id AND direction.batch_id=NEW.batch_id
     AND direction.direction_id=NEW.direction_id
     AND direction.contract_sha=NEW.contract_sha
     AND direction.validator_version=NEW.validator_version
     AND direction.artifact_sha=NEW.artifact_sha
    WHERE batch_set.run_id=NEW.run_id AND batch_set.batch_id=NEW.batch_id
      AND batch_set.snapshot_id=NEW.snapshot_id
      AND batch_set.current_batch_ids_hash=NEW.current_batch_ids_hash
      AND batch_set.member_count=NEW.member_count
      AND (SELECT count(*)
           FROM audit_batch_direction_gate_bindings_v2 binding
           WHERE binding.gate_sha256=NEW.gate_sha256)=NEW.member_count
      AND NOT EXISTS (
        SELECT 1 FROM json_each(batch_set.member_ids_json) member
        WHERE NOT EXISTS (
          SELECT 1
          FROM json_each(NEW.candidate_mapping_json) mapping
          JOIN audit_batch_staging staging
            ON staging.staging_candidate_id=
               json_extract(mapping.value,'$.staging_candidate_id')
           AND staging.run_id=NEW.run_id AND staging.batch_id=NEW.batch_id
           AND staging.source_order=
               json_extract(mapping.value,'$.source_order')
          JOIN audit_batch_staging_authorities_v2 authority
            ON authority.staging_candidate_id=staging.staging_candidate_id
           AND authority.run_id=staging.run_id
           AND authority.batch_id=staging.batch_id
           AND authority.candidate_hash=staging.candidate_hash
           AND authority.raw_artifact_sha=staging.raw_artifact_sha
           AND authority.source_order=staging.source_order
           AND authority.issued_at=staging.created_at
           AND authority.authority_kind IN ('host_issued','migration_v2')
          JOIN audit_batch_direction_gate_bindings_v2 binding
            ON binding.gate_sha256=NEW.gate_sha256
           AND binding.selector_id=json_extract(mapping.value,'$.selector_id')
           AND binding.source_order=staging.source_order
           AND binding.staging_candidate_id=staging.staging_candidate_id
          JOIN audit_batch_direction_verdicts_v2 verdict
            ON verdict.verdict_sha256=binding.verdict_sha256
           AND verdict.run_id=NEW.run_id AND verdict.batch_id=NEW.batch_id
           AND verdict.snapshot_id=NEW.snapshot_id
           AND verdict.current_batch_ids_hash=NEW.current_batch_ids_hash
           AND verdict.direction_id=NEW.direction_id
           AND verdict.contract_sha=NEW.contract_sha
           AND verdict.validator_version=NEW.validator_version
           AND verdict.artifact_sha=NEW.artifact_sha
           AND verdict.staging_candidate_id=staging.staging_candidate_id
          JOIN json_each(NEW.verdict_set_json) verdict_item
            ON json_extract(verdict_item.value,'$.selector_id')=
               binding.selector_id
           AND json_extract(verdict_item.value,'$.staging_candidate_id')=
               binding.staging_candidate_id
           AND json_extract(verdict_item.value,'$.source_order')=
               binding.source_order
           AND json_extract(verdict_item.value,'$.verdict_sha256')=
               binding.verdict_sha256
           AND json_extract(verdict_item.value,'$.direction_fit')=
               verdict.direction_fit
           AND json_extract(verdict_item.value,'$.evidence_sha256')=
               verdict.evidence_sha256
           AND json_quote(
                 json_extract(verdict_item.value,'$.direction_evidence')
               )||char(10)=verdict.evidence_json
          WHERE staging.staging_candidate_id=member.value
        )
      )
  ) THEN RAISE(ABORT,'direction gate full frozen coverage mismatch') END;
END;

CREATE VIEW audit_valid_batch_direction_gates_v2 AS
SELECT gate.*
FROM audit_batch_direction_gates_v2 gate
JOIN audit_snapshot_batch_sets batch_set
  ON batch_set.snapshot_id=gate.snapshot_id
 AND batch_set.current_batch_ids_hash=gate.current_batch_ids_hash
 AND batch_set.run_id=gate.run_id AND batch_set.batch_id=gate.batch_id
WHERE batch_set.member_count=gate.member_count
  AND audit_direction_gate_valid(
    gate.gate_sha256,gate.run_id,gate.batch_id,gate.snapshot_id,
    gate.current_batch_ids_hash,gate.direction_id,gate.contract_sha,
    gate.validator_version,gate.artifact_sha,gate.parser_revision,
    gate.raw_selector_artifact_sha256,gate.member_count,
    gate.candidate_mapping_json,gate.verdict_set_json,
    gate.verdict_set_sha256,gate.verdict_tsv,gate.issued_at
  )=1
  AND (SELECT count(*)
       FROM audit_batch_direction_gate_bindings_v2 binding
       WHERE binding.gate_sha256=gate.gate_sha256)=gate.member_count
  AND NOT EXISTS (
    SELECT 1
    FROM audit_batch_direction_gate_bindings_v2 binding
    JOIN audit_batch_direction_verdicts_v2 verdict
      ON verdict.verdict_sha256=binding.verdict_sha256
    WHERE binding.gate_sha256=gate.gate_sha256
      AND verdict.direction_fit<>'in-scope'
  );

CREATE VIEW audit_valid_strict_pair_completions_v2 AS
SELECT staging.staging_candidate_id,receipt.run_id,receipt.batch_id,
       receipt.pair_plan_sha,receipt.pair_result_sha,binding.snapshot_id,
       binding.current_batch_ids_hash,snapshot.history_as_of_watermark
FROM audit_batch_staging staging
JOIN audit_batch_staging_authorities_v2 authority
  ON authority.staging_candidate_id=staging.staging_candidate_id
 AND authority.run_id=staging.run_id AND authority.batch_id=staging.batch_id
 AND authority.candidate_hash=staging.candidate_hash
 AND authority.raw_artifact_sha=staging.raw_artifact_sha
 AND authority.source_order=staging.source_order
 AND authority.issued_at=staging.created_at
 AND authority.authority_kind IN ('host_issued','migration_v2')
JOIN audit_batch_pair_receipts receipt
  ON receipt.run_id=staging.run_id AND receipt.batch_id=staging.batch_id
JOIN audit_batch_pair_set_bindings binding
  ON binding.run_id=receipt.run_id AND binding.batch_id=receipt.batch_id
 AND binding.snapshot_id=receipt.snapshot_id
 AND binding.pair_plan_sha=receipt.pair_plan_sha
 AND binding.pair_result_sha=receipt.pair_result_sha
JOIN audit_snapshot_batch_sets batch_set
  ON batch_set.snapshot_id=binding.snapshot_id
 AND batch_set.current_batch_ids_hash=binding.current_batch_ids_hash
 AND batch_set.member_count=binding.member_count
JOIN audit_snapshots snapshot ON snapshot.snapshot_id=binding.snapshot_id
JOIN json_each(batch_set.member_ids_json) selected_member
  ON selected_member.value=staging.staging_candidate_id
WHERE receipt.pair_count=(batch_set.member_count*(batch_set.member_count-1))/2
  AND NOT EXISTS (
    SELECT 1 FROM audit_batch_pairs pair
    WHERE pair.run_id=receipt.run_id AND pair.batch_id=receipt.batch_id
      AND (pair.pair_plan_sha<>receipt.pair_plan_sha
           OR pair.pair_result_sha<>receipt.pair_result_sha
           OR NOT EXISTS (
             SELECT 1 FROM json_each(batch_set.member_ids_json) left_member
             JOIN json_each(batch_set.member_ids_json) right_member
               ON left_member.value<right_member.value
             WHERE pair.left_staging_candidate_id=left_member.value
               AND pair.right_staging_candidate_id=right_member.value
           ))
  )
  AND NOT EXISTS (
    SELECT 1 FROM json_each(batch_set.member_ids_json) left_member
    JOIN json_each(batch_set.member_ids_json) right_member
      ON left_member.value<right_member.value
    WHERE NOT EXISTS (
      SELECT 1 FROM audit_batch_pairs pair
      WHERE pair.run_id=receipt.run_id AND pair.batch_id=receipt.batch_id
        AND pair.pair_plan_sha=receipt.pair_plan_sha
        AND pair.pair_result_sha=receipt.pair_result_sha
        AND pair.left_staging_candidate_id=left_member.value
        AND pair.right_staging_candidate_id=right_member.value
    )
  )
  AND (batch_set.member_count=1 OR EXISTS (
    SELECT 1 FROM audit_batch_pairs pair
    WHERE pair.run_id=receipt.run_id AND pair.batch_id=receipt.batch_id
      AND pair.pair_plan_sha=receipt.pair_plan_sha
      AND pair.pair_result_sha=receipt.pair_result_sha
      AND staging.staging_candidate_id IN (
        pair.left_staging_candidate_id,pair.right_staging_candidate_id
      )
  ));

DROP TRIGGER IF EXISTS audit_activation_maps_evidence_guard;
CREATE TRIGGER audit_activation_maps_evidence_guard
BEFORE INSERT ON audit_activation_maps
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_batch_staging staging
    JOIN audit_batch_staging_authorities_v2 authority
      ON authority.staging_candidate_id=staging.staging_candidate_id
     AND authority.run_id=staging.run_id AND authority.batch_id=staging.batch_id
     AND authority.candidate_hash=staging.candidate_hash
     AND authority.raw_artifact_sha=staging.raw_artifact_sha
     AND authority.source_order=staging.source_order
     AND authority.issued_at=staging.created_at
    WHERE staging.staging_candidate_id=NEW.staging_candidate_id
      AND staging.raw_artifact_sha=NEW.raw_artifact_sha
  ) THEN RAISE(ABORT,'activation staging authority mismatch') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM audit_batch_staging_authorities_v2 authority
    WHERE authority.staging_candidate_id=NEW.staging_candidate_id
      AND authority.authority_kind IN ('host_issued','migration_v2')
  ) AND NOT EXISTS (
    SELECT 1 FROM audit_valid_strict_pair_completions_v2 completion
    JOIN audit_activation_receipts activation_receipt
      ON activation_receipt.staging_candidate_id=completion.staging_candidate_id
     AND activation_receipt.activation_receipt_sha=NEW.activation_receipt_sha
    WHERE completion.staging_candidate_id=NEW.staging_candidate_id
      AND completion.pair_plan_sha=NEW.pair_plan_sha
      AND completion.pair_result_sha=NEW.pair_result_sha
      AND NEW.source_sequence>completion.history_as_of_watermark
  ) THEN RAISE(ABORT,'strict activation pair completion mismatch') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM audit_batch_staging_authorities_v2 authority
    WHERE authority.staging_candidate_id=NEW.staging_candidate_id
      AND authority.authority_kind='migration_legacy'
  ) THEN RAISE(ABORT,'legacy staging activation boundary is frozen') END;
END;

DROP TRIGGER IF EXISTS audit_activation_maps_batch_direction_guard;
CREATE TRIGGER audit_activation_maps_batch_direction_guard
BEFORE INSERT ON audit_activation_maps
WHEN EXISTS (
  SELECT 1 FROM audit_batch_staging_authorities_v2 authority
  WHERE authority.staging_candidate_id=NEW.staging_candidate_id
    AND authority.authority_kind IN ('host_issued','migration_v2')
)
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_batch_staging staging
    JOIN audit_batch_pair_set_bindings pair_binding
      ON pair_binding.run_id=staging.run_id
     AND pair_binding.batch_id=staging.batch_id
     AND pair_binding.pair_plan_sha=NEW.pair_plan_sha
     AND pair_binding.pair_result_sha=NEW.pair_result_sha
    JOIN audit_valid_batch_direction_gates_v2 gate
      ON gate.run_id=staging.run_id AND gate.batch_id=staging.batch_id
     AND gate.snapshot_id=pair_binding.snapshot_id
     AND gate.current_batch_ids_hash=pair_binding.current_batch_ids_hash
    JOIN audit_batch_direction_gate_bindings_v2 gate_binding
      ON gate_binding.gate_sha256=gate.gate_sha256
     AND gate_binding.staging_candidate_id=staging.staging_candidate_id
    WHERE staging.staging_candidate_id=NEW.staging_candidate_id
  ) THEN RAISE(ABORT,'activation lacks exact unanimous direction gate') END;
END;

DROP TRIGGER IF EXISTS audit_direction_checks_staging_owner_guard;
CREATE TRIGGER audit_direction_checks_staging_owner_guard
BEFORE INSERT ON audit_direction_checks
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_batch_staging staging
    JOIN audit_batch_staging_authorities_v2 authority
      ON authority.staging_candidate_id=staging.staging_candidate_id
     AND authority.authority_kind='migration_legacy'
    WHERE staging.staging_candidate_id=NEW.staging_candidate_id
      AND staging.run_id=NEW.run_id AND staging.batch_id=NEW.batch_id
  ) THEN RAISE(ABORT,'legacy direction check is outside migration boundary') END;
  SELECT RAISE(ABORT,'legacy direction migration boundary is immutable');
END;

CREATE TABLE audit_direction_gate_upgrade_probe(
  value INTEGER NOT NULL CHECK(value=0)
);
INSERT INTO audit_direction_gate_upgrade_probe(value)
SELECT 1
FROM audit_activation_maps activation
JOIN audit_batch_staging_authorities_v2 authority
  ON authority.staging_candidate_id=activation.staging_candidate_id
WHERE authority.authority_kind IN ('host_issued','migration_v2')
  AND NOT EXISTS (
    SELECT 1 FROM audit_batch_direction_gates_v2 gate
    WHERE gate.run_id=authority.run_id AND gate.batch_id=authority.batch_id
  );
DROP TABLE audit_direction_gate_upgrade_probe;
"""


_METADATA_DIRECTION_GATE_PROVENANCE_SQL = """
CREATE VIEW audit_valid_metadata_direction_provenance_v2 AS
SELECT activation.legacy_candidate_id AS candidate_id,
       gate.run_id,gate.batch_id,gate.direction_id,gate.contract_sha,
       gate.validator_version,gate.artifact_sha,
       'batch_gate_v2' AS provenance_kind
FROM audit_activation_maps activation
JOIN audit_batch_staging staging
  ON staging.staging_candidate_id=activation.staging_candidate_id
 AND staging.raw_artifact_sha=activation.raw_artifact_sha
JOIN audit_batch_staging_authorities_v2 authority
  ON authority.staging_candidate_id=staging.staging_candidate_id
 AND authority.run_id=staging.run_id AND authority.batch_id=staging.batch_id
 AND authority.candidate_hash=staging.candidate_hash
 AND authority.raw_artifact_sha=staging.raw_artifact_sha
 AND authority.source_order=staging.source_order
 AND authority.issued_at=staging.created_at
 AND authority.authority_kind IN ('host_issued','migration_v2')
JOIN audit_valid_batch_direction_gates_v2 gate
  ON gate.run_id=staging.run_id AND gate.batch_id=staging.batch_id
JOIN audit_batch_pair_set_bindings pair_binding
  ON pair_binding.run_id=staging.run_id
 AND pair_binding.batch_id=staging.batch_id
 AND pair_binding.pair_plan_sha=activation.pair_plan_sha
 AND pair_binding.pair_result_sha=activation.pair_result_sha
 AND pair_binding.snapshot_id=gate.snapshot_id
 AND pair_binding.current_batch_ids_hash=gate.current_batch_ids_hash
JOIN audit_batch_direction_gate_bindings_v2 binding
  ON binding.gate_sha256=gate.gate_sha256
 AND binding.staging_candidate_id=staging.staging_candidate_id
 AND binding.source_order=staging.source_order
JOIN audit_batch_direction_verdicts_v2 verdict
  ON verdict.verdict_sha256=binding.verdict_sha256
 AND verdict.run_id=gate.run_id AND verdict.batch_id=gate.batch_id
 AND verdict.snapshot_id=gate.snapshot_id
 AND verdict.current_batch_ids_hash=gate.current_batch_ids_hash
 AND verdict.direction_id=gate.direction_id
 AND verdict.contract_sha=gate.contract_sha
 AND verdict.validator_version=gate.validator_version
 AND verdict.artifact_sha=gate.artifact_sha
 AND verdict.staging_candidate_id=staging.staging_candidate_id
 AND verdict.direction_fit='in-scope'
JOIN audit_direction_contracts direction_contract
  ON direction_contract.run_id=gate.run_id
 AND direction_contract.batch_id=gate.batch_id
 AND direction_contract.direction_id=gate.direction_id
 AND direction_contract.contract_sha=gate.contract_sha
 AND direction_contract.validator_version=gate.validator_version
 AND direction_contract.artifact_sha=gate.artifact_sha
UNION ALL
SELECT activation.legacy_candidate_id AS candidate_id,
       direction_check.run_id,direction_check.batch_id,
       direction_check.direction_id,direction_check.contract_sha,
       direction_check.validator_version,direction_check.artifact_sha,
       'migration_legacy' AS provenance_kind
FROM audit_activation_maps activation
JOIN audit_batch_staging staging
  ON staging.staging_candidate_id=activation.staging_candidate_id
 AND staging.raw_artifact_sha=activation.raw_artifact_sha
JOIN audit_batch_staging_authorities_v2 authority
  ON authority.staging_candidate_id=staging.staging_candidate_id
 AND authority.run_id=staging.run_id AND authority.batch_id=staging.batch_id
 AND authority.candidate_hash=staging.candidate_hash
 AND authority.raw_artifact_sha=staging.raw_artifact_sha
 AND authority.source_order=staging.source_order
 AND authority.issued_at=staging.created_at
 AND authority.authority_kind='migration_legacy'
JOIN audit_direction_checks direction_check
  ON direction_check.staging_candidate_id=staging.staging_candidate_id
 AND direction_check.run_id=staging.run_id
 AND direction_check.batch_id=staging.batch_id
JOIN audit_direction_contracts direction_contract
  ON direction_contract.run_id=direction_check.run_id
 AND direction_contract.batch_id=direction_check.batch_id
 AND direction_contract.direction_id=direction_check.direction_id
 AND direction_contract.contract_sha=direction_check.contract_sha
 AND direction_contract.validator_version=direction_check.validator_version
 AND direction_contract.artifact_sha=direction_check.artifact_sha;

CREATE TABLE audit_metadata_direction_gate_provenance_probe(
  value INTEGER NOT NULL CHECK(value=0)
);
INSERT INTO audit_metadata_direction_gate_provenance_probe(value)
SELECT 1
FROM audit_annotation_versions_v2 annotation
WHERE annotation.family='direction'
  AND (
    annotation.direction_identity_json IS NULL
    OR json_valid(annotation.direction_identity_json)<>1
    OR (SELECT count(*)
        FROM audit_valid_metadata_direction_provenance_v2 provenance
        WHERE provenance.candidate_id=annotation.candidate_id
          AND provenance.run_id=json_extract(
            annotation.direction_identity_json,'$.run_id'
          )
          AND provenance.batch_id=json_extract(
            annotation.direction_identity_json,'$.batch_id'
          )
          AND provenance.direction_id=json_extract(
            annotation.direction_identity_json,'$.direction_id'
          )
          AND provenance.contract_sha=json_extract(
            annotation.direction_identity_json,'$.contract_sha'
          )
          AND provenance.validator_version=json_extract(
            annotation.direction_identity_json,'$.validator_version'
          )
          AND provenance.artifact_sha=json_extract(
            annotation.direction_identity_json,'$.artifact_sha'
          ))<>1
  );
DROP TABLE audit_metadata_direction_gate_provenance_probe;

DROP TRIGGER audit_annotation_versions_v2_insert_guard;
CREATE TRIGGER audit_annotation_versions_v2_insert_guard
BEFORE INSERT ON audit_annotation_versions_v2
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_metadata_outbox_v2 work
    JOIN audit_metadata_annotation_claims_v2 claim
      ON claim.annotation_id=NEW.annotation_id
     AND claim.outbox_id=work.outbox_id
     AND claim.claim_fence=work.fence
     AND claim.claim_token=work.claim_token
    WHERE work.outbox_id=NEW.outbox_id
      AND work.state='claimed'
      AND work.profile_id=NEW.profile_id
      AND work.profile_sha256=NEW.profile_sha256
      AND work.candidate_id=NEW.candidate_id
      AND work.source_content_sha=NEW.source_content_sha
      AND work.source_sequence=NEW.source_sequence
      AND work.producer_kind=NEW.producer_kind
      AND work.producer_id=NEW.producer_id
      AND work.producer_version=NEW.producer_version
      AND work.prompt_sha256=NEW.prompt_sha256
      AND audit_metadata_operation()='publish'
      AND work.outbox_id=audit_metadata_outbox_id()
      AND work.fence=audit_metadata_claim_fence()
      AND work.claim_token=audit_metadata_claim_token()
      AND audit_metadata_lease_live(
        work.lease_until,audit_metadata_now()
      )=1
  ) THEN RAISE(ABORT,'annotation is not bound to current claimed work') END;
  SELECT CASE WHEN NEW.family='direction' AND NOT EXISTS (
    SELECT 1
    FROM audit_valid_metadata_direction_provenance_v2 provenance
    WHERE provenance.candidate_id=NEW.candidate_id
      AND provenance.run_id=json_extract(
        NEW.direction_identity_json,'$.run_id'
      )
      AND provenance.batch_id=json_extract(
        NEW.direction_identity_json,'$.batch_id'
      )
      AND provenance.direction_id=json_extract(
        NEW.direction_identity_json,'$.direction_id'
      )
      AND provenance.contract_sha=json_extract(
        NEW.direction_identity_json,'$.contract_sha'
      )
      AND provenance.validator_version=json_extract(
        NEW.direction_identity_json,'$.validator_version'
      )
      AND provenance.artifact_sha=json_extract(
        NEW.direction_identity_json,'$.artifact_sha'
      )
  ) THEN RAISE(ABORT,'direction annotation lacks exact durable provenance') END;
END;
"""


_ROUTER_SOURCE_AUTHORITY_SQL = """
CREATE TABLE audit_router_rounds_v2(
  route_round_sha256 TEXT PRIMARY KEY CHECK(length(route_round_sha256)=64),
  run_id TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  intent TEXT NOT NULL,
  snapshot_id TEXT NOT NULL CHECK(length(snapshot_id)=64),
  snapshot_hash TEXT NOT NULL CHECK(length(snapshot_hash)=64),
  current_batch_ids_hash TEXT NOT NULL CHECK(length(current_batch_ids_hash)=64),
  candidate_ids_json TEXT NOT NULL,
  round_json TEXT NOT NULL,
  risk_policy_sha TEXT NOT NULL CHECK(length(risk_policy_sha)=64),
  risk_slice_policy_sha TEXT NOT NULL CHECK(length(risk_slice_policy_sha)=64),
  budget_policy_sha TEXT NOT NULL CHECK(length(budget_policy_sha)=64),
  authority_scope TEXT NOT NULL CHECK(authority_scope='test_fake'),
  created_at TEXT NOT NULL,
  UNIQUE(run_id,batch_id,intent)
);
CREATE TABLE audit_router_budget_facts_v2(
  budget_fact_sha256 TEXT PRIMARY KEY CHECK(length(budget_fact_sha256)=64),
  route_round_sha256 TEXT NOT NULL UNIQUE
    REFERENCES audit_router_rounds_v2(route_round_sha256),
  candidate_ids_json TEXT NOT NULL,
  requested_candidates INTEGER NOT NULL CHECK(requested_candidates>=1),
  round_candidate_limit INTEGER NOT NULL CHECK(round_candidate_limit>=0),
  candidate_budget_decision TEXT NOT NULL
    CHECK(candidate_budget_decision IN ('accepted','rejected')),
  started_attempts_used INTEGER NOT NULL CHECK(started_attempts_used>=0),
  round_started_attempt_limit INTEGER NOT NULL
    CHECK(round_started_attempt_limit>=0),
  candidate_started_attempt_limit INTEGER NOT NULL
    CHECK(candidate_started_attempt_limit>=0),
  attempt_budget_available INTEGER NOT NULL
    CHECK(attempt_budget_available IN (0,1)),
  usage_root_sha256 TEXT NOT NULL CHECK(length(usage_root_sha256)=64),
  fact_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  CHECK(
    (candidate_budget_decision='accepted'
      AND requested_candidates<=round_candidate_limit)
    OR
    (candidate_budget_decision='rejected'
      AND requested_candidates>round_candidate_limit)
  )
);
CREATE TABLE audit_router_domain_sources_v2(
  source_sha256 TEXT PRIMARY KEY CHECK(length(source_sha256)=64),
  route_round_sha256 TEXT NOT NULL
    REFERENCES audit_router_rounds_v2(route_round_sha256),
  source_kind TEXT NOT NULL CHECK(source_kind IN (
    'selection','l1_observation','calibration','qualification',
    'risk_assignment','dependency_heads','permanent_request'
  )),
  source_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(route_round_sha256,source_kind)
);
CREATE TABLE audit_router_source_sets_v2(
  source_set_sha256 TEXT PRIMARY KEY CHECK(length(source_set_sha256)=64),
  route_round_sha256 TEXT NOT NULL
    REFERENCES audit_router_rounds_v2(route_round_sha256),
  phase TEXT NOT NULL CHECK(phase IN ('pre_l1','final')),
  source_refs_json TEXT NOT NULL,
  budget_fact_sha256 TEXT NOT NULL
    REFERENCES audit_router_budget_facts_v2(budget_fact_sha256),
  dependency_head_events_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(route_round_sha256,phase)
);
CREATE TABLE audit_router_phase_facts_v2(
  phase_fact_sha256 TEXT PRIMARY KEY CHECK(length(phase_fact_sha256)=64),
  route_round_sha256 TEXT NOT NULL
    REFERENCES audit_router_rounds_v2(route_round_sha256),
  phase TEXT NOT NULL CHECK(phase IN ('pre_l1','final')),
  candidate_id TEXT NOT NULL,
  source_set_sha256 TEXT NOT NULL
    REFERENCES audit_router_source_sets_v2(source_set_sha256),
  router_facts_json TEXT NOT NULL,
  risk_slices_json TEXT NOT NULL,
  matched_rule_ids_json TEXT NOT NULL,
  route TEXT NOT NULL CHECK(route IN ('routine','guarded','exhaustive')),
  call_l1_model INTEGER NOT NULL CHECK(call_l1_model IN (0,1)),
  dispatch_allowed INTEGER NOT NULL CHECK(dispatch_allowed IN (0,1)),
  release_authorized INTEGER NOT NULL CHECK(release_authorized IN (0,1)),
  rule_table_sha256 TEXT NOT NULL CHECK(length(rule_table_sha256)=64),
  risk_policy_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(route_round_sha256,phase,candidate_id)
);
CREATE TABLE audit_candidate_route_source_bindings_v2(
  run_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  route_fact_sha256 TEXT NOT NULL,
  final_phase_fact_sha256 TEXT NOT NULL
    REFERENCES audit_router_phase_facts_v2(phase_fact_sha256),
  source_set_sha256 TEXT NOT NULL
    REFERENCES audit_router_source_sets_v2(source_set_sha256),
  bound_at TEXT NOT NULL,
  PRIMARY KEY(run_id,candidate_id),
  UNIQUE(route_fact_sha256),
  FOREIGN KEY(run_id,candidate_id,route_fact_sha256)
    REFERENCES audit_candidate_route_facts_v2(
      run_id,candidate_id,fact_sha256
    )
);
CREATE TABLE audit_legacy_candidate_route_authorities_v2(
  route_fact_sha256 TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  cohort_sha256 TEXT NOT NULL
    REFERENCES audit_candidate_route_cohorts_v2(cohort_sha256),
  observation_boundary_sha256 TEXT
    REFERENCES audit_candidate_route_observation_boundaries_v2(boundary_sha256),
  dispatch_sha256 TEXT
    REFERENCES audit_candidate_l2_dispatch_facts_v2(dispatch_sha256),
  plan_sha TEXT REFERENCES audit_l2_plans_v2(plan_sha),
  reason TEXT NOT NULL CHECK(reason='pre_source_authority'),
  quarantined_at TEXT NOT NULL,
  FOREIGN KEY(run_id,candidate_id,route_fact_sha256)
    REFERENCES audit_candidate_route_facts_v2(
      run_id,candidate_id,fact_sha256
    )
);
INSERT INTO audit_legacy_candidate_route_authorities_v2(
  route_fact_sha256,run_id,candidate_id,cohort_sha256,
  observation_boundary_sha256,dispatch_sha256,plan_sha,
  reason,quarantined_at
)
SELECT route.fact_sha256,route.run_id,route.candidate_id,route.cohort_sha256,
       observation.boundary_sha256,dispatch.dispatch_sha256,dispatch.plan_sha,
       'pre_source_authority',route.created_at
FROM audit_candidate_route_facts_v2 route
LEFT JOIN audit_candidate_route_observation_boundaries_v2 observation
  ON observation.run_id=route.run_id
 AND observation.candidate_id=route.candidate_id
 AND observation.route_fact_sha256=route.fact_sha256
LEFT JOIN audit_candidate_l2_dispatch_facts_v2 dispatch
  ON dispatch.run_id=route.run_id
 AND dispatch.candidate_id=route.candidate_id
 AND dispatch.route_fact_sha256=route.fact_sha256;
""" + _immutable_guards(
    "audit_router_rounds_v2",
    "audit_router_budget_facts_v2",
    "audit_router_domain_sources_v2",
    "audit_router_source_sets_v2",
    "audit_router_phase_facts_v2",
    "audit_candidate_route_source_bindings_v2",
    "audit_legacy_candidate_route_authorities_v2",
) + """
CREATE TRIGGER audit_router_rounds_v2_guard
BEFORE INSERT ON audit_router_rounds_v2
BEGIN
  SELECT CASE WHEN audit_router_round_insert_allowed(
    NEW.route_round_sha256,NEW.run_id,NEW.batch_id,NEW.intent,
    NEW.snapshot_id,NEW.snapshot_hash,NEW.current_batch_ids_hash,
    NEW.candidate_ids_json,NEW.round_json,NEW.risk_policy_sha,
    NEW.risk_slice_policy_sha,NEW.budget_policy_sha,
    NEW.authority_scope,NEW.created_at
  )<>1 THEN RAISE(ABORT,'router round requires host authority') END;
  SELECT CASE WHEN audit_router_round_valid(
    NEW.route_round_sha256,NEW.run_id,NEW.batch_id,NEW.intent,
    NEW.snapshot_id,NEW.snapshot_hash,NEW.current_batch_ids_hash,
    NEW.candidate_ids_json,NEW.round_json,NEW.risk_policy_sha,
    NEW.risk_slice_policy_sha,NEW.budget_policy_sha,
    NEW.authority_scope
  )<>1 THEN RAISE(ABORT,'router round identity mismatch') END;
END;
CREATE TRIGGER audit_router_budget_facts_v2_guard
BEFORE INSERT ON audit_router_budget_facts_v2
BEGIN
  SELECT CASE WHEN audit_router_budget_insert_allowed(
    NEW.budget_fact_sha256,NEW.route_round_sha256,NEW.candidate_ids_json,
    NEW.requested_candidates,NEW.round_candidate_limit,
    NEW.candidate_budget_decision,NEW.started_attempts_used,
    NEW.round_started_attempt_limit,NEW.candidate_started_attempt_limit,
    NEW.attempt_budget_available,NEW.usage_root_sha256,
    NEW.fact_json,NEW.created_at
  )<>1 THEN RAISE(ABORT,'router budget fact requires host authority') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_router_rounds_v2 round
    WHERE round.route_round_sha256=NEW.route_round_sha256
      AND audit_router_budget_valid(
        round.round_json,NEW.budget_fact_sha256,NEW.route_round_sha256,
        NEW.candidate_ids_json,NEW.requested_candidates,
        NEW.round_candidate_limit,NEW.candidate_budget_decision,
        NEW.started_attempts_used,NEW.round_started_attempt_limit,
        NEW.candidate_started_attempt_limit,NEW.attempt_budget_available,
        NEW.usage_root_sha256,NEW.fact_json,NEW.created_at
      )=1
  ) THEN RAISE(ABORT,'router budget fact identity mismatch') END;
END;
CREATE TRIGGER audit_router_domain_sources_v2_guard
BEFORE INSERT ON audit_router_domain_sources_v2
BEGIN
  SELECT CASE WHEN audit_router_source_insert_allowed(
    NEW.source_sha256,NEW.route_round_sha256,NEW.source_kind,
    NEW.source_json,NEW.created_at
  )<>1 THEN RAISE(ABORT,'router domain source requires host authority') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_router_rounds_v2 round
    WHERE round.route_round_sha256=NEW.route_round_sha256
      AND audit_router_domain_source_valid(
        round.round_json,NEW.source_sha256,NEW.route_round_sha256,
        NEW.source_kind,NEW.source_json
      )=1
  ) THEN RAISE(ABORT,'router domain source identity mismatch') END;
  SELECT CASE WHEN NEW.source_kind='l1_observation' AND EXISTS (
    SELECT 1
    FROM json_each(NEW.source_json,'$.members') member
    WHERE json_extract(member.value,'$.observation_kind')='pre_l1_skip'
      AND NOT EXISTS (
        SELECT 1 FROM audit_router_phase_facts_v2 phase
        WHERE phase.phase_fact_sha256=json_extract(
                member.value,'$.pre_phase_fact_sha256'
              )
          AND phase.route_round_sha256=NEW.route_round_sha256
          AND phase.phase='pre_l1'
          AND phase.candidate_id=json_extract(member.value,'$.candidate_id')
          AND phase.call_l1_model=0
          AND EXISTS (
            SELECT 1 FROM json_each(phase.matched_rule_ids_json) matched
            WHERE matched.value=json_extract(member.value,'$.skip_reason')
          )
      )
  ) THEN RAISE(ABORT,'router L1 skip lacks exact pre-phase fact') END;
END;
CREATE TRIGGER audit_router_source_sets_v2_guard
BEFORE INSERT ON audit_router_source_sets_v2
BEGIN
  SELECT CASE WHEN audit_router_source_set_insert_allowed(
    NEW.source_set_sha256,NEW.route_round_sha256,NEW.phase,
    NEW.source_refs_json,NEW.budget_fact_sha256,
    NEW.dependency_head_events_json,NEW.created_at
  )<>1 THEN RAISE(ABORT,'router source set requires host authority') END;
END;
CREATE TRIGGER audit_router_phase_facts_v2_guard
BEFORE INSERT ON audit_router_phase_facts_v2
BEGIN
  SELECT CASE WHEN audit_router_phase_fact_insert_allowed(
    NEW.phase_fact_sha256,NEW.route_round_sha256,NEW.phase,
    NEW.candidate_id,NEW.source_set_sha256,NEW.router_facts_json,
    NEW.risk_slices_json,NEW.matched_rule_ids_json,NEW.route,
    NEW.call_l1_model,NEW.dispatch_allowed,NEW.release_authorized,
    NEW.rule_table_sha256,NEW.risk_policy_version,NEW.created_at
  )<>1 THEN RAISE(ABORT,'router phase fact requires host authority') END;
END;
CREATE TRIGGER audit_candidate_route_source_bindings_v2_guard
BEFORE INSERT ON audit_candidate_route_source_bindings_v2
BEGIN
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM audit_legacy_candidate_route_authorities_v2 legacy
    WHERE legacy.route_fact_sha256=NEW.route_fact_sha256
       OR (legacy.run_id=NEW.run_id AND legacy.candidate_id=NEW.candidate_id)
  ) THEN RAISE(ABORT,'legacy candidate route cannot receive source authority') END;
  SELECT CASE WHEN audit_router_binding_insert_allowed(
    NEW.run_id,NEW.candidate_id,NEW.route_fact_sha256,
    NEW.final_phase_fact_sha256,NEW.source_set_sha256,NEW.bound_at
  )<>1 THEN RAISE(ABORT,'candidate route binding requires host authority') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_candidate_route_facts_v2 route
    JOIN audit_router_phase_facts_v2 phase
      ON phase.phase_fact_sha256=NEW.final_phase_fact_sha256
     AND phase.phase='final'
     AND phase.candidate_id=NEW.candidate_id
    JOIN audit_router_source_sets_v2 source_set
      ON source_set.source_set_sha256=NEW.source_set_sha256
     AND source_set.source_set_sha256=phase.source_set_sha256
     AND source_set.route_round_sha256=phase.route_round_sha256
     AND source_set.phase='final'
    JOIN audit_router_rounds_v2 round
      ON round.route_round_sha256=phase.route_round_sha256
     AND round.run_id=NEW.run_id
     AND round.intent=route.intent
    WHERE route.run_id=NEW.run_id
      AND route.candidate_id=NEW.candidate_id
      AND route.fact_sha256=NEW.route_fact_sha256
      AND route.router_facts_json=phase.router_facts_json
      AND route.risk_slices_json=phase.risk_slices_json
      AND route.matched_rule_ids_json=phase.matched_rule_ids_json
      AND route.route=phase.route
      AND route.call_l1_model=phase.call_l1_model
      AND route.dispatch_allowed=phase.dispatch_allowed
      AND route.rule_table_sha256=phase.rule_table_sha256
      AND route.risk_policy_version=phase.risk_policy_version
  ) THEN RAISE(ABORT,'candidate route binding identity mismatch') END;
END;
CREATE TRIGGER audit_legacy_candidate_route_authorities_v2_guard
BEFORE INSERT ON audit_legacy_candidate_route_authorities_v2
BEGIN
  SELECT CASE WHEN audit_router_legacy_insert_allowed(
    NEW.route_fact_sha256,NEW.run_id,NEW.candidate_id,
    NEW.cohort_sha256,NEW.observation_boundary_sha256,
    NEW.dispatch_sha256,NEW.plan_sha,NEW.reason,NEW.quarantined_at
  )<>1 THEN RAISE(ABORT,'legacy route quarantine requires migration authority') END;
END;
CREATE TABLE audit_router_source_authority_probe(
  value INTEGER NOT NULL CHECK(value=0)
);
INSERT INTO audit_router_source_authority_probe(value)
SELECT 1
FROM audit_legacy_candidate_route_authorities_v2 legacy
JOIN audit_candidate_route_facts_v2 route
  ON route.run_id=legacy.run_id
 AND route.candidate_id=legacy.candidate_id
 AND route.fact_sha256=legacy.route_fact_sha256
WHERE legacy.cohort_sha256<>route.cohort_sha256
   OR legacy.observation_boundary_sha256 IS NOT (
     SELECT observation.boundary_sha256
     FROM audit_candidate_route_observation_boundaries_v2 observation
     WHERE observation.run_id=route.run_id
       AND observation.candidate_id=route.candidate_id
       AND observation.route_fact_sha256=route.fact_sha256
   )
   OR legacy.dispatch_sha256 IS NOT (
     SELECT dispatch.dispatch_sha256
     FROM audit_candidate_l2_dispatch_facts_v2 dispatch
     WHERE dispatch.run_id=route.run_id
       AND dispatch.candidate_id=route.candidate_id
       AND dispatch.route_fact_sha256=route.fact_sha256
   )
   OR legacy.plan_sha IS NOT (
     SELECT dispatch.plan_sha
     FROM audit_candidate_l2_dispatch_facts_v2 dispatch
     WHERE dispatch.run_id=route.run_id
       AND dispatch.candidate_id=route.candidate_id
       AND dispatch.route_fact_sha256=route.fact_sha256
   );
INSERT INTO audit_router_source_authority_probe(value)
SELECT 1
FROM audit_legacy_candidate_route_authorities_v2 legacy
JOIN audit_candidate_route_source_bindings_v2 binding
  ON binding.run_id=legacy.run_id
 AND binding.candidate_id=legacy.candidate_id
 AND binding.route_fact_sha256=legacy.route_fact_sha256;
DROP TABLE audit_router_source_authority_probe;
"""


_VERIFIED_USAGE_AUTHORITY_SQL = """
CREATE TABLE audit_verified_usage_authorities_v2(
  usage_authority_sha256 TEXT PRIMARY KEY CHECK(length(usage_authority_sha256)=64),
  attempt_id TEXT NOT NULL UNIQUE REFERENCES audit_task_attempts(attempt_id),
  run_id TEXT NOT NULL,
  intent TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  capability_profile_hash TEXT NOT NULL CHECK(length(capability_profile_hash)=64),
  request_cas_object_id TEXT NOT NULL REFERENCES audit_cas_objects(object_id),
  output_cas_object_id TEXT REFERENCES audit_cas_objects(object_id),
  terminal_outcome TEXT NOT NULL CHECK(terminal_outcome IN (
    'valid','timeout','429','5xx','overflow','syntax','schema',
    'item_set','truncated','invalid_anchor','provider_error','cancelled'
  )),
  actual_json TEXT NOT NULL,
  billing_state TEXT NOT NULL CHECK(billing_state IN (
    'billable','nonbillable','unknown'
  )),
  price_source TEXT,
  currency TEXT,
  terminal_at TEXT NOT NULL,
  authority_scope TEXT NOT NULL CHECK(authority_scope='test_fake'),
  CHECK((terminal_outcome='cancelled')=(output_cas_object_id IS NULL)),
  CHECK(
    (billing_state='billable')=
    (price_source IS NOT NULL AND currency IS NOT NULL)
  )
);
""" + _immutable_guards("audit_verified_usage_authorities_v2") + """
CREATE TRIGGER audit_verified_usage_authorities_v2_guard
BEFORE INSERT ON audit_verified_usage_authorities_v2
BEGIN
  SELECT CASE WHEN audit_verified_usage_authority_insert_allowed(
    NEW.usage_authority_sha256,NEW.attempt_id,NEW.run_id,NEW.intent,
    NEW.candidate_id,NEW.provider,NEW.capability_profile_hash,
    NEW.request_cas_object_id,NEW.output_cas_object_id,NEW.terminal_outcome,
    NEW.actual_json,NEW.billing_state,NEW.price_source,NEW.currency,
    NEW.terminal_at,NEW.authority_scope
  )<>1 THEN RAISE(ABORT,'verified usage authority requires host authority') END;
  SELECT CASE WHEN audit_verified_usage_authority_valid(
    NEW.usage_authority_sha256,NEW.attempt_id,NEW.run_id,NEW.intent,
    NEW.candidate_id,NEW.provider,NEW.capability_profile_hash,
    NEW.request_cas_object_id,NEW.output_cas_object_id,NEW.terminal_outcome,
    NEW.actual_json,NEW.billing_state,NEW.price_source,NEW.currency,
    NEW.terminal_at,NEW.authority_scope
  )<>1 THEN RAISE(ABORT,'verified usage authority identity mismatch') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_task_attempts attempt
    JOIN audit_logical_tasks task ON task.task_hash=attempt.task_hash
    JOIN audit_task_bindings_v2 binding ON binding.task_hash=task.task_hash
    JOIN audit_l2_plans_v2 plan ON plan.plan_sha=binding.plan_sha
    JOIN audit_runtime_budget_reservations_v2 reservation
      ON reservation.attempt_id=attempt.attempt_id
    JOIN audit_cas_objects request
      ON request.object_id=attempt.request_cas_object_id
    WHERE attempt.attempt_id=NEW.attempt_id
      AND task.run_id=NEW.run_id
      AND plan.intent=NEW.intent
      AND task.staging_candidate_id=NEW.candidate_id
      AND reservation.candidate_id=NEW.candidate_id
      AND reservation.intent=NEW.intent
      AND json_extract(attempt.provenance_json,'$.provider')=NEW.provider
      AND json_extract(
        attempt.provenance_json,'$.capability_profile_hash'
      )=NEW.capability_profile_hash
      AND attempt.request_cas_object_id=NEW.request_cas_object_id
      AND request.integrity_state='verified'
      AND julianday(NEW.terminal_at)>=julianday(attempt.created_at)
      AND (
        NEW.output_cas_object_id IS NULL
        OR EXISTS (
          SELECT 1 FROM audit_cas_objects output
          WHERE output.object_id=NEW.output_cas_object_id
            AND output.integrity_state='verified'
        )
      )
      AND NOT EXISTS (
        SELECT 1 FROM audit_attempt_completions_v2 completion
        WHERE completion.attempt_id=NEW.attempt_id
      )
      AND NOT EXISTS (
        SELECT 1 FROM audit_runtime_budget_settlements_v2 budget
        WHERE budget.attempt_id=NEW.attempt_id
      )
      AND NOT EXISTS (
        SELECT 1 FROM audit_attempt_cost_settlements_v2 cost
        WHERE cost.attempt_id=NEW.attempt_id
      )
  ) THEN RAISE(ABORT,'verified usage authority lacks exact attempt authority') END;
END;

DROP TRIGGER IF EXISTS audit_attempt_completions_v2_terminal_authority_guard;
CREATE TRIGGER audit_attempt_completions_v2_terminal_authority_guard
BEFORE INSERT ON audit_attempt_completions_v2
BEGIN
  SELECT CASE WHEN audit_attempt_completion_insert_allowed(
    NEW.attempt_id,NEW.output_cas_object_id,NEW.outcome,
    NEW.normalized_result_json,NEW.usage_json,NEW.completed_at
  )<>1 THEN RAISE(ABORT,'attempt completion requires host authority') END;
  SELECT CASE WHEN audit_completion_usage_valid(NEW.usage_json)<>1
    THEN RAISE(ABORT,'attempt completion usage authority is invalid') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM audit_attempt_cost_settlements_v2 cost
    WHERE cost.attempt_id=NEW.attempt_id AND cost.outcome='cancelled'
  ) THEN RAISE(ABORT,'attempt completion conflicts with cancellation') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM audit_verified_usage_authorities_v2 authority
    WHERE authority.attempt_id=NEW.attempt_id
      AND (
        authority.terminal_outcome<>NEW.outcome
        OR authority.output_cas_object_id<>NEW.output_cas_object_id
        OR authority.terminal_at<>NEW.completed_at
      )
  ) THEN RAISE(ABORT,'completion lacks exact verified usage sidecar') END;
END;

DROP TRIGGER IF EXISTS audit_runtime_budget_settlements_v2_owner_guard;
CREATE TRIGGER audit_runtime_budget_settlements_v2_owner_guard
BEFORE INSERT ON audit_runtime_budget_settlements_v2
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_task_attempts attempt
    WHERE attempt.attempt_id=NEW.attempt_id
  ) THEN RAISE(ABORT,'budget settlement attempt is missing') END;
  SELECT CASE WHEN audit_l2_budget_settlement_valid(
    NEW.usage_verified,NEW.actual_json
  )<>1 THEN RAISE(ABORT,'budget settlement usage is invalid') END;
  SELECT CASE WHEN audit_runtime_budget_settlement_insert_allowed(
    NEW.attempt_id,NEW.usage_verified,NEW.actual_json,NEW.created_at
  )<>1 THEN RAISE(ABORT,'budget settlement requires host authority') END;
  SELECT CASE WHEN NOT (
    (
      NEW.usage_verified=1
      AND EXISTS (
        SELECT 1 FROM audit_verified_usage_authorities_v2 authority
        WHERE authority.attempt_id=NEW.attempt_id
          AND authority.actual_json=NEW.actual_json
          AND authority.terminal_at=NEW.created_at
          AND (
            (
              authority.terminal_outcome<>'cancelled'
              AND EXISTS (
                SELECT 1 FROM audit_attempt_completions_v2 completion
                WHERE completion.attempt_id=NEW.attempt_id
                  AND completion.output_cas_object_id=
                      authority.output_cas_object_id
                  AND completion.outcome=authority.terminal_outcome
                  AND completion.completed_at=authority.terminal_at
              )
            )
            OR
            (
              authority.terminal_outcome='cancelled'
              AND NOT EXISTS (
                SELECT 1 FROM audit_attempt_completions_v2 completion
                WHERE completion.attempt_id=NEW.attempt_id
              )
            )
          )
      )
    )
    OR
    (
      NEW.usage_verified=0 AND NEW.actual_json IS NULL
      AND NOT EXISTS (
        SELECT 1 FROM audit_verified_usage_authorities_v2 authority
        WHERE authority.attempt_id=NEW.attempt_id
      )
      AND (
        EXISTS (
          SELECT 1 FROM audit_attempt_completions_v2 completion
          WHERE completion.attempt_id=NEW.attempt_id
            AND completion.completed_at=NEW.created_at
            AND NOT EXISTS (
              SELECT 1 FROM audit_attempt_cost_settlements_v2 cost
              WHERE cost.attempt_id=NEW.attempt_id AND cost.outcome='cancelled'
            )
        )
        OR
        EXISTS (
          SELECT 1 FROM audit_attempt_cost_settlements_v2 cost
          WHERE cost.attempt_id=NEW.attempt_id
            AND cost.outcome='cancelled'
            AND cost.billing_state='unknown'
            AND cost.usage_source='reservation'
            AND cost.completed_at=NEW.created_at
            AND NOT EXISTS (
              SELECT 1 FROM audit_attempt_completions_v2 completion
              WHERE completion.attempt_id=NEW.attempt_id
            )
        )
      )
    )
  ) THEN RAISE(ABORT,'budget settlement lacks exact terminal authority') END;
END;

CREATE TRIGGER audit_attempt_cost_settlements_v2_verified_usage_guard
BEFORE INSERT ON audit_attempt_cost_settlements_v2
BEGIN
  SELECT CASE WHEN NOT (
    (
      NEW.usage_source='verified_actual'
      AND EXISTS (
        SELECT 1
        FROM audit_verified_usage_authorities_v2 authority
        JOIN audit_runtime_budget_settlements_v2 budget
          ON budget.attempt_id=authority.attempt_id
         AND budget.usage_verified=1
         AND budget.actual_json=authority.actual_json
         AND budget.created_at=authority.terminal_at
        WHERE authority.attempt_id=NEW.attempt_id
          AND authority.billing_state=NEW.billing_state
          AND authority.price_source IS NEW.price_source
          AND authority.currency IS NEW.currency
          AND authority.terminal_at=NEW.completed_at
          AND NEW.outcome=CASE authority.terminal_outcome
            WHEN 'valid' THEN 'success'
            WHEN 'cancelled' THEN 'cancelled'
            ELSE 'failed'
          END
      )
    )
    OR
    (
      NEW.usage_source='reservation'
      AND NEW.billing_state='unknown'
      AND NEW.price_source IS NULL
      AND NEW.currency IS NULL
      AND NOT EXISTS (
        SELECT 1 FROM audit_verified_usage_authorities_v2 authority
        WHERE authority.attempt_id=NEW.attempt_id
      )
    )
  ) THEN RAISE(ABORT,'attempt cost lacks exact usage authority') END;
END;

CREATE TABLE audit_verified_usage_authority_upgrade_probe(
  value INTEGER NOT NULL CHECK(value=0)
);
INSERT INTO audit_verified_usage_authority_upgrade_probe(value)
SELECT 1
FROM audit_verified_usage_authorities_v2 authority
LEFT JOIN audit_task_attempts attempt USING(attempt_id)
LEFT JOIN audit_logical_tasks task ON task.task_hash=attempt.task_hash
LEFT JOIN audit_task_bindings_v2 binding ON binding.task_hash=task.task_hash
LEFT JOIN audit_l2_plans_v2 plan ON plan.plan_sha=binding.plan_sha
LEFT JOIN audit_runtime_budget_reservations_v2 reservation USING(attempt_id)
WHERE audit_verified_usage_authority_valid(
    authority.usage_authority_sha256,authority.attempt_id,authority.run_id,
    authority.intent,authority.candidate_id,authority.provider,
    authority.capability_profile_hash,authority.request_cas_object_id,
    authority.output_cas_object_id,authority.terminal_outcome,
    authority.actual_json,authority.billing_state,authority.price_source,
    authority.currency,authority.terminal_at,authority.authority_scope
  )<>1
   OR attempt.attempt_id IS NULL
   OR task.task_hash IS NULL
   OR binding.task_hash IS NULL
   OR plan.plan_sha IS NULL
   OR reservation.attempt_id IS NULL
   OR task.run_id IS NOT authority.run_id
   OR plan.intent IS NOT authority.intent
   OR task.staging_candidate_id IS NOT authority.candidate_id
   OR reservation.candidate_id IS NOT authority.candidate_id
   OR reservation.intent IS NOT authority.intent
   OR json_extract(
        attempt.provenance_json,'$.provider'
      ) IS NOT authority.provider
   OR json_extract(
        attempt.provenance_json,'$.capability_profile_hash'
      ) IS NOT authority.capability_profile_hash
   OR attempt.request_cas_object_id IS NOT authority.request_cas_object_id;
INSERT INTO audit_verified_usage_authority_upgrade_probe(value)
SELECT 1
FROM audit_runtime_budget_settlements_v2 budget
LEFT JOIN audit_verified_usage_authorities_v2 authority USING(attempt_id)
LEFT JOIN audit_attempt_completions_v2 completion USING(attempt_id)
LEFT JOIN audit_attempt_cost_settlements_v2 cost USING(attempt_id)
WHERE (
    budget.usage_verified=1
    AND (
      authority.attempt_id IS NULL
      OR budget.actual_json IS NOT authority.actual_json
      OR budget.created_at IS NOT authority.terminal_at
      OR cost.attempt_id IS NULL
      OR cost.usage_source<>'verified_actual'
      OR cost.billing_state IS NOT authority.billing_state
      OR cost.price_source IS NOT authority.price_source
      OR cost.currency IS NOT authority.currency
      OR cost.completed_at IS NOT authority.terminal_at
      OR cost.outcome IS NOT CASE authority.terminal_outcome
        WHEN 'valid' THEN 'success'
        WHEN 'cancelled' THEN 'cancelled'
        ELSE 'failed'
      END
      OR (
        authority.terminal_outcome='cancelled'
        AND completion.attempt_id IS NOT NULL
      )
      OR (
        authority.terminal_outcome<>'cancelled'
        AND (
          completion.attempt_id IS NULL
          OR completion.output_cas_object_id
            IS NOT authority.output_cas_object_id
          OR completion.outcome IS NOT authority.terminal_outcome
          OR completion.completed_at IS NOT authority.terminal_at
        )
      )
    )
  )
   OR (
     budget.usage_verified=0
     AND (budget.actual_json IS NOT NULL OR authority.attempt_id IS NOT NULL)
   );
INSERT INTO audit_verified_usage_authority_upgrade_probe(value)
SELECT 1
FROM audit_attempt_cost_settlements_v2 cost
LEFT JOIN audit_verified_usage_authorities_v2 authority USING(attempt_id)
LEFT JOIN audit_runtime_budget_settlements_v2 budget USING(attempt_id)
WHERE (
    cost.usage_source='verified_actual'
    AND (
      authority.attempt_id IS NULL
      OR budget.attempt_id IS NULL
      OR budget.usage_verified IS NOT 1
      OR budget.actual_json IS NOT authority.actual_json
      OR budget.created_at IS NOT authority.terminal_at
      OR cost.billing_state IS NOT authority.billing_state
      OR cost.price_source IS NOT authority.price_source
      OR cost.currency IS NOT authority.currency
      OR cost.completed_at IS NOT authority.terminal_at
      OR cost.outcome IS NOT CASE authority.terminal_outcome
        WHEN 'valid' THEN 'success'
        WHEN 'cancelled' THEN 'cancelled'
        ELSE 'failed'
      END
    )
  )
   OR (
     cost.usage_source='reservation' AND authority.attempt_id IS NOT NULL
   );
INSERT INTO audit_verified_usage_authority_upgrade_probe(value)
SELECT 1
FROM audit_verified_usage_authorities_v2 authority
LEFT JOIN audit_runtime_budget_settlements_v2 budget USING(attempt_id)
LEFT JOIN audit_attempt_completions_v2 completion USING(attempt_id)
LEFT JOIN audit_attempt_cost_settlements_v2 cost USING(attempt_id)
WHERE (
    budget.attempt_id IS NOT NULL
    OR completion.attempt_id IS NOT NULL
    OR cost.attempt_id IS NOT NULL
  )
  AND (
    budget.attempt_id IS NULL
    OR budget.usage_verified IS NOT 1
    OR budget.actual_json IS NOT authority.actual_json
    OR budget.created_at IS NOT authority.terminal_at
    OR cost.attempt_id IS NULL
    OR cost.usage_source<>'verified_actual'
    OR cost.billing_state IS NOT authority.billing_state
    OR cost.price_source IS NOT authority.price_source
    OR cost.currency IS NOT authority.currency
    OR cost.completed_at IS NOT authority.terminal_at
    OR cost.outcome IS NOT CASE authority.terminal_outcome
      WHEN 'valid' THEN 'success'
      WHEN 'cancelled' THEN 'cancelled'
      ELSE 'failed'
    END
    OR (
      authority.terminal_outcome='cancelled'
      AND completion.attempt_id IS NOT NULL
    )
    OR (
      authority.terminal_outcome<>'cancelled'
      AND (
        completion.attempt_id IS NULL
        OR completion.output_cas_object_id
          IS NOT authority.output_cas_object_id
        OR completion.outcome IS NOT authority.terminal_outcome
        OR completion.completed_at IS NOT authority.terminal_at
      )
    )
  );
DROP TABLE audit_verified_usage_authority_upgrade_probe;
"""


_L1_COST_AUTHORITY_SQL = """
CREATE VIEW audit_l1_candidate_route_authorities_v2 AS
SELECT route.run_id,route.candidate_id,route.intent,
       route.fact_sha256 AS route_fact_sha256,
       binding.final_phase_fact_sha256,
       binding.source_set_sha256,
       pre.phase_fact_sha256 AS pre_phase_fact_sha256,
       l1_source.source_sha256 AS l1_source_sha256,
       json_extract(
         member.value,'$.comparator_receipt_sha256'
       ) AS comparator_receipt_sha256,
       pre.created_at AS pre_phase_created_at,
       l1_source.created_at AS l1_source_created_at,
       final.created_at AS final_phase_created_at,
       binding.bound_at,
       plan.plan_sha,plan.plan_json,
       pool.value AS provider,
       CAST(pool.key AS INTEGER) AS comparator_pool_index,
       json_extract(
         capability.value,'$.capability_profile_hash'
       ) AS capability_profile_hash
FROM audit_candidate_route_cohorts_v2 cohort
JOIN audit_candidate_route_facts_v2 route
  ON route.run_id=cohort.run_id
 AND route.intent=cohort.intent
 AND route.cohort_sha256=cohort.cohort_sha256
JOIN audit_candidate_route_source_bindings_v2 binding
  ON binding.run_id=route.run_id
 AND binding.candidate_id=route.candidate_id
 AND binding.route_fact_sha256=route.fact_sha256
JOIN audit_router_phase_facts_v2 final
  ON final.phase_fact_sha256=binding.final_phase_fact_sha256
 AND final.phase='final'
 AND final.candidate_id=route.candidate_id
 AND final.source_set_sha256=binding.source_set_sha256
JOIN audit_router_source_sets_v2 source_set
  ON source_set.source_set_sha256=binding.source_set_sha256
 AND source_set.phase='final'
 AND source_set.route_round_sha256=final.route_round_sha256
JOIN audit_router_rounds_v2 round
  ON round.route_round_sha256=final.route_round_sha256
 AND round.run_id=route.run_id
 AND round.intent=route.intent
JOIN audit_router_phase_facts_v2 pre
  ON pre.route_round_sha256=round.route_round_sha256
 AND pre.phase='pre_l1'
 AND pre.candidate_id=route.candidate_id
 AND pre.call_l1_model=1
JOIN audit_router_domain_sources_v2 l1_source
  ON l1_source.route_round_sha256=round.route_round_sha256
 AND l1_source.source_kind='l1_observation'
 AND l1_source.source_sha256=json_extract(
       source_set.source_refs_json,'$.l1_observation'
     )
JOIN json_each(l1_source.source_json,'$.members') member
  ON json_extract(member.value,'$.candidate_id')=route.candidate_id
 AND json_extract(member.value,'$.observation_kind')='comparator'
 AND json_extract(member.value,'$.coverage_state')='complete'
JOIN audit_l2_plans_v2 plan
  ON plan.run_id=route.run_id
 AND plan.intent=route.intent
 AND plan.budget_policy_sha=round.budget_policy_sha
 AND json_extract(plan.plan_json,'$.batch_id')=round.batch_id
 AND json_extract(plan.plan_json,'$.snapshot.snapshot_id')=round.snapshot_id
 AND json_extract(plan.plan_json,'$.snapshot.snapshot_hash')=
       round.snapshot_hash
JOIN audit_run_manifests manifest
  ON manifest.run_id=plan.run_id
 AND manifest.plan_hash=plan.plan_sha
 AND manifest.manifest_json=plan.plan_json
JOIN json_each(plan.plan_json,'$.provider_pools_ordered.comparator') pool
JOIN json_each(plan.plan_json,'$.provider_capabilities') capability
  ON capability.key=pool.value
 AND json_extract(capability.value,'$.provider')=pool.value
JOIN json_each(
  plan.plan_json,'$.provider_capability_profile_hashes'
) capability_hash
  ON capability_hash.key=pool.value
 AND capability_hash.value=json_extract(
       capability.value,'$.capability_profile_hash'
     )
WHERE route.call_l1_model=1
  AND final.call_l1_model=1
  AND route.router_facts_json=final.router_facts_json
  AND route.risk_slices_json=final.risk_slices_json
  AND route.matched_rule_ids_json=final.matched_rule_ids_json
  AND route.route=final.route
  AND route.call_l1_model=final.call_l1_model
  AND route.dispatch_allowed=final.dispatch_allowed
  AND route.rule_table_sha256=final.rule_table_sha256
  AND route.risk_policy_version=final.risk_policy_version
  AND length(json_extract(
        member.value,'$.comparator_receipt_sha256'
      ))=64
  AND (
    SELECT count(*)
    FROM json_each(l1_source.source_json,'$.members') exact_member
    WHERE json_extract(exact_member.value,'$.candidate_id')=
          route.candidate_id
  )=1
  AND NOT EXISTS (
    SELECT 1 FROM audit_legacy_candidate_route_authorities_v2 legacy
    WHERE legacy.route_fact_sha256=route.fact_sha256
       OR (legacy.run_id=route.run_id
           AND legacy.candidate_id=route.candidate_id)
  );

CREATE TABLE audit_l1_verified_usage_authorities_v2(
  usage_authority_sha256 TEXT PRIMARY KEY CHECK(length(usage_authority_sha256)=64),
  attempt_id TEXT NOT NULL UNIQUE CHECK(length(attempt_id)=64),
  ordinal INTEGER NOT NULL CHECK(ordinal>=0),
  previous_attempt_id TEXT REFERENCES audit_l1_attempt_facts_v2(attempt_id),
  run_id TEXT NOT NULL REFERENCES audit_run_manifests(run_id),
  candidate_id TEXT NOT NULL,
  intent TEXT NOT NULL,
  provider TEXT NOT NULL,
  capability_profile_hash TEXT NOT NULL CHECK(length(capability_profile_hash)=64),
  request_evidence_sha256 TEXT NOT NULL CHECK(length(request_evidence_sha256)=64),
  result_evidence_sha256 TEXT CHECK(
    result_evidence_sha256 IS NULL OR length(result_evidence_sha256)=64
  ),
  terminal_outcome TEXT NOT NULL CHECK(terminal_outcome IN (
    'success','failed','cancelled'
  )),
  actual_json TEXT NOT NULL,
  billing_state TEXT NOT NULL CHECK(billing_state IN (
    'billable','nonbillable','unknown'
  )),
  price_source TEXT,
  currency TEXT,
  terminal_at TEXT NOT NULL,
  route_fact_sha256 TEXT NOT NULL
    REFERENCES audit_candidate_route_facts_v2(fact_sha256),
  final_phase_fact_sha256 TEXT NOT NULL
    REFERENCES audit_router_phase_facts_v2(phase_fact_sha256),
  source_set_sha256 TEXT NOT NULL
    REFERENCES audit_router_source_sets_v2(source_set_sha256),
  authority_scope TEXT NOT NULL CHECK(authority_scope='test_fake'),
  CHECK((ordinal=0 AND previous_attempt_id IS NULL)
     OR (ordinal>0 AND previous_attempt_id IS NOT NULL)),
  CHECK((terminal_outcome='success' AND result_evidence_sha256 IS NOT NULL)
     OR (terminal_outcome<>'success' AND result_evidence_sha256 IS NULL)),
  CHECK((billing_state='billable')=
        (price_source IS NOT NULL AND currency IS NOT NULL)),
  FOREIGN KEY(run_id,candidate_id,route_fact_sha256)
    REFERENCES audit_candidate_route_facts_v2(run_id,candidate_id,fact_sha256)
);
""" + _immutable_guards(
    "audit_l1_verified_usage_authorities_v2"
) + """
CREATE TRIGGER audit_l1_verified_usage_authorities_v2_guard
BEFORE INSERT ON audit_l1_verified_usage_authorities_v2
BEGIN
  SELECT CASE WHEN audit_l1_verified_usage_authority_insert_allowed(
    NEW.usage_authority_sha256,NEW.attempt_id,NEW.ordinal,
    NEW.previous_attempt_id,NEW.run_id,NEW.candidate_id,NEW.intent,
    NEW.provider,NEW.capability_profile_hash,
    NEW.request_evidence_sha256,NEW.result_evidence_sha256,
    NEW.terminal_outcome,NEW.actual_json,NEW.billing_state,
    NEW.price_source,NEW.currency,NEW.terminal_at,
    NEW.route_fact_sha256,NEW.final_phase_fact_sha256,
    NEW.source_set_sha256,NEW.authority_scope
  )<>1 THEN RAISE(ABORT,'L1 verified usage requires host authority') END;
  SELECT CASE WHEN audit_l1_verified_usage_authority_valid(
    NEW.usage_authority_sha256,NEW.attempt_id,NEW.ordinal,
    NEW.previous_attempt_id,NEW.run_id,NEW.candidate_id,NEW.intent,
    NEW.provider,NEW.capability_profile_hash,
    NEW.request_evidence_sha256,NEW.result_evidence_sha256,
    NEW.terminal_outcome,NEW.actual_json,NEW.billing_state,
    NEW.price_source,NEW.currency,NEW.terminal_at,
    NEW.route_fact_sha256,NEW.final_phase_fact_sha256,
    NEW.source_set_sha256,NEW.authority_scope
  )<>1 THEN RAISE(ABORT,'L1 verified usage identity mismatch') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_l1_candidate_route_authorities_v2 route
    WHERE route.run_id=NEW.run_id
      AND route.candidate_id=NEW.candidate_id
      AND route.intent=NEW.intent
      AND route.provider=NEW.provider
      AND route.capability_profile_hash=NEW.capability_profile_hash
      AND route.route_fact_sha256=NEW.route_fact_sha256
      AND route.final_phase_fact_sha256=NEW.final_phase_fact_sha256
      AND route.source_set_sha256=NEW.source_set_sha256
      AND julianday(NEW.terminal_at)>=julianday(route.pre_phase_created_at)
      AND julianday(NEW.terminal_at)<=julianday(route.l1_source_created_at)
      AND (
        (NEW.terminal_outcome='success'
          AND NEW.result_evidence_sha256=route.comparator_receipt_sha256)
        OR
        (NEW.terminal_outcome IN ('failed','cancelled')
          AND NEW.result_evidence_sha256 IS NULL)
      )
  ) THEN RAISE(ABORT,'L1 verified usage lacks route authority') END;
  SELECT CASE WHEN NOT (
    (NEW.ordinal=0
      AND NEW.previous_attempt_id IS NULL
      AND EXISTS (
        SELECT 1 FROM audit_l1_candidate_route_authorities_v2 initial_route
        WHERE initial_route.run_id=NEW.run_id
          AND initial_route.candidate_id=NEW.candidate_id
          AND initial_route.intent=NEW.intent
          AND initial_route.provider=NEW.provider
          AND initial_route.capability_profile_hash=
                NEW.capability_profile_hash
          AND initial_route.comparator_pool_index=0
      )
      AND audit_l1_attempt_id_valid(
        NEW.attempt_id,NEW.run_id,NEW.candidate_id,NEW.intent,
        NEW.ordinal,NEW.provider,NEW.capability_profile_hash,
        NEW.request_evidence_sha256,NULL,NULL,NULL,NULL
      )=1)
    OR
    (NEW.ordinal>0 AND EXISTS (
      SELECT 1
      FROM audit_l1_attempt_facts_v2 previous
      JOIN audit_l1_candidate_route_authorities_v2 current_route
        ON current_route.run_id=NEW.run_id
       AND current_route.candidate_id=NEW.candidate_id
       AND current_route.intent=NEW.intent
       AND current_route.provider=NEW.provider
       AND current_route.capability_profile_hash=NEW.capability_profile_hash
      JOIN json_each(
        current_route.plan_json,'$.provider_pools_ordered.comparator'
      ) previous_pool ON previous_pool.value=previous.provider
      JOIN json_each(
        current_route.plan_json,'$.provider_pools_ordered.comparator'
      ) current_pool ON current_pool.value=NEW.provider
      WHERE previous.attempt_id=NEW.previous_attempt_id
        AND previous.run_id=NEW.run_id
        AND previous.candidate_id=NEW.candidate_id
        AND previous.intent=NEW.intent
        AND previous.ordinal=NEW.ordinal-1
        AND previous.outcome IN ('failed','cancelled')
        AND julianday(previous.terminal_at)<=julianday(NEW.terminal_at)
        AND CAST(current_pool.key AS INTEGER) IN (
          CAST(previous_pool.key AS INTEGER),
          CAST(previous_pool.key AS INTEGER)+1
        )
        AND audit_l1_attempt_id_valid(
          NEW.attempt_id,NEW.run_id,NEW.candidate_id,NEW.intent,
          NEW.ordinal,NEW.provider,NEW.capability_profile_hash,
          NEW.request_evidence_sha256,previous.attempt_id,
          previous.fact_sha256,previous.outcome,previous.terminal_at
        )=1
    ))
  ) THEN RAISE(ABORT,'L1 verified usage chain is incomplete') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM audit_l1_attempt_facts_v2 fact
    WHERE fact.attempt_id=NEW.attempt_id
  ) THEN RAISE(ABORT,'L1 verified usage cannot follow settlement') END;
END;

CREATE TABLE audit_l1_attempt_facts_v2(
  attempt_id TEXT PRIMARY KEY CHECK(length(attempt_id)=64),
  ordinal INTEGER NOT NULL CHECK(ordinal>=0),
  previous_attempt_id TEXT REFERENCES audit_l1_attempt_facts_v2(attempt_id),
  run_id TEXT NOT NULL REFERENCES audit_run_manifests(run_id),
  candidate_id TEXT NOT NULL,
  intent TEXT NOT NULL,
  provider TEXT NOT NULL,
  capability_profile_hash TEXT NOT NULL CHECK(length(capability_profile_hash)=64),
  request_evidence_sha256 TEXT NOT NULL CHECK(length(request_evidence_sha256)=64),
  result_evidence_sha256 TEXT CHECK(
    result_evidence_sha256 IS NULL OR length(result_evidence_sha256)=64
  ),
  usage_source TEXT NOT NULL CHECK(usage_source IN ('reservation','verified_actual')),
  reserved_json TEXT NOT NULL,
  usage_authority_sha256 TEXT
    REFERENCES audit_l1_verified_usage_authorities_v2(usage_authority_sha256),
  actual_json TEXT,
  queue_latency_ms INTEGER NOT NULL CHECK(queue_latency_ms>=0),
  run_latency_ms INTEGER NOT NULL CHECK(run_latency_ms>=0),
  outcome TEXT NOT NULL CHECK(outcome IN ('success','failed','cancelled')),
  billing_state TEXT NOT NULL CHECK(billing_state IN ('billable','nonbillable','unknown')),
  price_source TEXT,
  currency TEXT,
  fact_sha256 TEXT NOT NULL UNIQUE CHECK(length(fact_sha256)=64),
  terminal_at TEXT NOT NULL,
  route_fact_sha256 TEXT NOT NULL
    REFERENCES audit_candidate_route_facts_v2(fact_sha256),
  final_phase_fact_sha256 TEXT NOT NULL
    REFERENCES audit_router_phase_facts_v2(phase_fact_sha256),
  source_set_sha256 TEXT NOT NULL
    REFERENCES audit_router_source_sets_v2(source_set_sha256),
  UNIQUE(run_id,candidate_id,ordinal),
  UNIQUE(previous_attempt_id),
  CHECK((ordinal=0 AND previous_attempt_id IS NULL)
     OR (ordinal>0 AND previous_attempt_id IS NOT NULL)),
  CHECK((outcome='success' AND result_evidence_sha256 IS NOT NULL)
     OR (outcome<>'success' AND result_evidence_sha256 IS NULL)),
  CHECK(
    (usage_source='reservation'
      AND usage_authority_sha256 IS NULL
      AND actual_json IS NULL
      AND billing_state='unknown'
      AND price_source IS NULL
      AND currency IS NULL)
    OR
    (usage_source='verified_actual'
      AND usage_authority_sha256 IS NOT NULL
      AND actual_json IS NOT NULL)
  ),
  FOREIGN KEY(run_id,candidate_id)
    REFERENCES audit_candidate_route_source_bindings_v2(run_id,candidate_id),
  FOREIGN KEY(run_id,candidate_id,route_fact_sha256)
    REFERENCES audit_candidate_route_facts_v2(run_id,candidate_id,fact_sha256)
);
""" + _immutable_guards("audit_l1_attempt_facts_v2") + """
CREATE VIEW audit_l1_valid_attempt_facts_v2 AS
SELECT fact.*
FROM audit_l1_attempt_facts_v2 fact
JOIN audit_l1_candidate_route_authorities_v2 route
  ON route.run_id=fact.run_id
 AND route.candidate_id=fact.candidate_id
 AND route.intent=fact.intent
 AND route.provider=fact.provider
 AND route.capability_profile_hash=fact.capability_profile_hash
 AND route.route_fact_sha256=fact.route_fact_sha256
 AND route.final_phase_fact_sha256=fact.final_phase_fact_sha256
 AND route.source_set_sha256=fact.source_set_sha256
LEFT JOIN audit_l1_attempt_facts_v2 previous
  ON previous.attempt_id=fact.previous_attempt_id
LEFT JOIN audit_l1_verified_usage_authorities_v2 usage
  ON usage.usage_authority_sha256=fact.usage_authority_sha256
WHERE audit_l1_attempt_fact_valid(
    fact.attempt_id,fact.ordinal,fact.previous_attempt_id,
    fact.run_id,fact.candidate_id,fact.intent,
    fact.provider,fact.capability_profile_hash,
    fact.request_evidence_sha256,fact.result_evidence_sha256,
    fact.usage_source,fact.reserved_json,fact.usage_authority_sha256,
    fact.actual_json,
    fact.queue_latency_ms,fact.run_latency_ms,fact.outcome,
    fact.billing_state,fact.price_source,fact.currency,
    fact.fact_sha256,fact.terminal_at,fact.route_fact_sha256,
    fact.final_phase_fact_sha256,fact.source_set_sha256
  )=1
  AND julianday(fact.terminal_at)>=julianday(route.pre_phase_created_at)
  AND julianday(fact.terminal_at)<=julianday(route.l1_source_created_at)
  AND (
    (fact.outcome='success'
      AND fact.result_evidence_sha256=route.comparator_receipt_sha256)
    OR
    (fact.outcome IN ('failed','cancelled')
      AND fact.result_evidence_sha256 IS NULL)
  )
  AND (
    (fact.ordinal=0
      AND fact.previous_attempt_id IS NULL
      AND route.comparator_pool_index=0
      AND audit_l1_attempt_id_valid(
        fact.attempt_id,fact.run_id,fact.candidate_id,fact.intent,
        fact.ordinal,fact.provider,fact.capability_profile_hash,
        fact.request_evidence_sha256,NULL,NULL,NULL,NULL
      )=1)
    OR
    (fact.ordinal>0
      AND previous.run_id=fact.run_id
      AND previous.candidate_id=fact.candidate_id
      AND previous.intent=fact.intent
      AND previous.ordinal=fact.ordinal-1
      AND previous.outcome IN ('failed','cancelled')
      AND julianday(previous.terminal_at)<=julianday(fact.terminal_at)
      AND EXISTS (
        SELECT 1
        FROM json_each(
          route.plan_json,'$.provider_pools_ordered.comparator'
        ) previous_pool
        JOIN json_each(
          route.plan_json,'$.provider_pools_ordered.comparator'
        ) current_pool
          ON current_pool.value=fact.provider
        WHERE previous_pool.value=previous.provider
          AND CAST(current_pool.key AS INTEGER) IN (
            CAST(previous_pool.key AS INTEGER),
            CAST(previous_pool.key AS INTEGER)+1
          )
      )
      AND audit_l1_attempt_id_valid(
        fact.attempt_id,fact.run_id,fact.candidate_id,fact.intent,
        fact.ordinal,fact.provider,fact.capability_profile_hash,
        fact.request_evidence_sha256,previous.attempt_id,
        previous.fact_sha256,previous.outcome,previous.terminal_at
      )=1)
  )
  AND (
    (fact.usage_source='reservation'
      AND fact.usage_authority_sha256 IS NULL
      AND fact.actual_json IS NULL
      AND fact.billing_state='unknown'
      AND fact.price_source IS NULL
      AND fact.currency IS NULL)
    OR
    (fact.usage_source='verified_actual'
      AND audit_l1_verified_usage_authority_valid(
        usage.usage_authority_sha256,usage.attempt_id,usage.ordinal,
        usage.previous_attempt_id,usage.run_id,usage.candidate_id,
        usage.intent,usage.provider,usage.capability_profile_hash,
        usage.request_evidence_sha256,usage.result_evidence_sha256,
        usage.terminal_outcome,usage.actual_json,usage.billing_state,
        usage.price_source,usage.currency,usage.terminal_at,
        usage.route_fact_sha256,usage.final_phase_fact_sha256,
        usage.source_set_sha256,usage.authority_scope
      )=1
      AND usage.attempt_id=fact.attempt_id
      AND usage.ordinal=fact.ordinal
      AND usage.previous_attempt_id IS fact.previous_attempt_id
      AND usage.run_id=fact.run_id
      AND usage.candidate_id=fact.candidate_id
      AND usage.intent=fact.intent
      AND usage.provider=fact.provider
      AND usage.capability_profile_hash=fact.capability_profile_hash
      AND usage.request_evidence_sha256=fact.request_evidence_sha256
      AND usage.result_evidence_sha256 IS fact.result_evidence_sha256
      AND usage.terminal_outcome=fact.outcome
      AND usage.actual_json=fact.actual_json
      AND usage.billing_state=fact.billing_state
      AND usage.price_source IS fact.price_source
      AND usage.currency IS fact.currency
      AND usage.terminal_at=fact.terminal_at
      AND usage.route_fact_sha256=fact.route_fact_sha256
      AND usage.final_phase_fact_sha256=fact.final_phase_fact_sha256
      AND usage.source_set_sha256=fact.source_set_sha256)
  );

CREATE TRIGGER audit_l1_attempt_facts_v2_guard
BEFORE INSERT ON audit_l1_attempt_facts_v2
BEGIN
  SELECT CASE WHEN audit_l1_attempt_fact_insert_allowed(
    NEW.attempt_id,NEW.ordinal,NEW.previous_attempt_id,
    NEW.run_id,NEW.candidate_id,NEW.intent,
    NEW.provider,NEW.capability_profile_hash,
    NEW.request_evidence_sha256,NEW.result_evidence_sha256,
    NEW.usage_source,NEW.reserved_json,NEW.usage_authority_sha256,
    NEW.actual_json,
    NEW.queue_latency_ms,NEW.run_latency_ms,NEW.outcome,
    NEW.billing_state,NEW.price_source,NEW.currency,
    NEW.fact_sha256,NEW.terminal_at,NEW.route_fact_sha256,
    NEW.final_phase_fact_sha256,NEW.source_set_sha256
  )<>1 THEN RAISE(ABORT,'L1 attempt fact requires host authority') END;
  SELECT CASE WHEN audit_l1_attempt_fact_valid(
    NEW.attempt_id,NEW.ordinal,NEW.previous_attempt_id,
    NEW.run_id,NEW.candidate_id,NEW.intent,
    NEW.provider,NEW.capability_profile_hash,
    NEW.request_evidence_sha256,NEW.result_evidence_sha256,
    NEW.usage_source,NEW.reserved_json,NEW.usage_authority_sha256,
    NEW.actual_json,
    NEW.queue_latency_ms,NEW.run_latency_ms,NEW.outcome,
    NEW.billing_state,NEW.price_source,NEW.currency,
    NEW.fact_sha256,NEW.terminal_at,NEW.route_fact_sha256,
    NEW.final_phase_fact_sha256,NEW.source_set_sha256
  )<>1 THEN RAISE(ABORT,'L1 attempt fact identity mismatch') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_l1_candidate_route_authorities_v2 route
    WHERE route.run_id=NEW.run_id
      AND route.candidate_id=NEW.candidate_id
      AND route.intent=NEW.intent
      AND route.provider=NEW.provider
      AND route.capability_profile_hash=NEW.capability_profile_hash
      AND route.route_fact_sha256=NEW.route_fact_sha256
      AND route.final_phase_fact_sha256=NEW.final_phase_fact_sha256
      AND route.source_set_sha256=NEW.source_set_sha256
      AND julianday(NEW.terminal_at)>=julianday(route.pre_phase_created_at)
      AND julianday(NEW.terminal_at)<=julianday(route.l1_source_created_at)
      AND (
        (NEW.outcome='success'
          AND NEW.result_evidence_sha256=route.comparator_receipt_sha256)
        OR
        (NEW.outcome IN ('failed','cancelled')
          AND NEW.result_evidence_sha256 IS NULL)
      )
  ) THEN RAISE(ABORT,'L1 attempt fact lacks final route authority') END;
  SELECT CASE WHEN NOT (
    (NEW.ordinal=0
      AND NEW.previous_attempt_id IS NULL
      AND EXISTS (
        SELECT 1 FROM audit_l1_candidate_route_authorities_v2 initial_route
        WHERE initial_route.run_id=NEW.run_id
          AND initial_route.candidate_id=NEW.candidate_id
          AND initial_route.intent=NEW.intent
          AND initial_route.provider=NEW.provider
          AND initial_route.capability_profile_hash=
                NEW.capability_profile_hash
          AND initial_route.comparator_pool_index=0
      )
      AND audit_l1_attempt_id_valid(
        NEW.attempt_id,NEW.run_id,NEW.candidate_id,NEW.intent,
        NEW.ordinal,NEW.provider,NEW.capability_profile_hash,
        NEW.request_evidence_sha256,NULL,NULL,NULL,NULL
      )=1)
    OR
    (NEW.ordinal>0 AND EXISTS (
      SELECT 1
      FROM audit_l1_attempt_facts_v2 previous
      JOIN audit_l1_candidate_route_authorities_v2 current_route
        ON current_route.run_id=NEW.run_id
       AND current_route.candidate_id=NEW.candidate_id
       AND current_route.intent=NEW.intent
       AND current_route.provider=NEW.provider
       AND current_route.capability_profile_hash=NEW.capability_profile_hash
      JOIN json_each(
        current_route.plan_json,'$.provider_pools_ordered.comparator'
      ) previous_pool ON previous_pool.value=previous.provider
      JOIN json_each(
        current_route.plan_json,'$.provider_pools_ordered.comparator'
      ) current_pool ON current_pool.value=NEW.provider
      WHERE previous.attempt_id=NEW.previous_attempt_id
        AND previous.run_id=NEW.run_id
        AND previous.candidate_id=NEW.candidate_id
        AND previous.intent=NEW.intent
        AND previous.ordinal=NEW.ordinal-1
        AND previous.outcome IN ('failed','cancelled')
        AND julianday(previous.terminal_at)<=julianday(NEW.terminal_at)
        AND CAST(current_pool.key AS INTEGER) IN (
          CAST(previous_pool.key AS INTEGER),
          CAST(previous_pool.key AS INTEGER)+1
        )
        AND audit_l1_attempt_id_valid(
          NEW.attempt_id,NEW.run_id,NEW.candidate_id,NEW.intent,
          NEW.ordinal,NEW.provider,NEW.capability_profile_hash,
          NEW.request_evidence_sha256,previous.attempt_id,
          previous.fact_sha256,previous.outcome,previous.terminal_at
        )=1
    ))
  ) THEN RAISE(ABORT,'L1 attempt chain is incomplete') END;
  SELECT CASE WHEN NEW.usage_source='verified_actual' AND NOT EXISTS (
    SELECT 1 FROM audit_l1_verified_usage_authorities_v2 usage
    WHERE usage.usage_authority_sha256=NEW.usage_authority_sha256
      AND usage.attempt_id=NEW.attempt_id
      AND usage.ordinal=NEW.ordinal
      AND usage.previous_attempt_id IS NEW.previous_attempt_id
      AND usage.run_id=NEW.run_id
      AND usage.candidate_id=NEW.candidate_id
      AND usage.intent=NEW.intent
      AND usage.provider=NEW.provider
      AND usage.capability_profile_hash=NEW.capability_profile_hash
      AND usage.request_evidence_sha256=NEW.request_evidence_sha256
      AND usage.result_evidence_sha256 IS NEW.result_evidence_sha256
      AND usage.terminal_outcome=NEW.outcome
      AND usage.actual_json=NEW.actual_json
      AND usage.billing_state=NEW.billing_state
      AND usage.price_source IS NEW.price_source
      AND usage.currency IS NEW.currency
      AND usage.terminal_at=NEW.terminal_at
      AND usage.route_fact_sha256=NEW.route_fact_sha256
      AND usage.final_phase_fact_sha256=NEW.final_phase_fact_sha256
      AND usage.source_set_sha256=NEW.source_set_sha256
      AND audit_l1_verified_usage_authority_valid(
        usage.usage_authority_sha256,usage.attempt_id,usage.ordinal,
        usage.previous_attempt_id,usage.run_id,usage.candidate_id,
        usage.intent,usage.provider,usage.capability_profile_hash,
        usage.request_evidence_sha256,usage.result_evidence_sha256,
        usage.terminal_outcome,usage.actual_json,usage.billing_state,
        usage.price_source,usage.currency,usage.terminal_at,
        usage.route_fact_sha256,usage.final_phase_fact_sha256,
        usage.source_set_sha256,usage.authority_scope
      )=1
  ) THEN RAISE(ABORT,'L1 attempt fact lacks verified usage authority') END;
END;

CREATE TABLE audit_l1_cost_authority_upgrade_probe(
  value INTEGER NOT NULL CHECK(value=0)
);
INSERT INTO audit_l1_cost_authority_upgrade_probe(value)
SELECT 1
FROM audit_l1_attempt_facts_v2 fact
LEFT JOIN audit_l1_valid_attempt_facts_v2 valid USING(attempt_id)
WHERE valid.attempt_id IS NULL;
INSERT INTO audit_l1_cost_authority_upgrade_probe(value)
SELECT 1
FROM audit_l1_verified_usage_authorities_v2 usage
LEFT JOIN audit_l1_attempt_facts_v2 previous
  ON previous.attempt_id=usage.previous_attempt_id
WHERE audit_l1_verified_usage_authority_valid(
    usage.usage_authority_sha256,usage.attempt_id,usage.ordinal,
    usage.previous_attempt_id,usage.run_id,usage.candidate_id,
    usage.intent,usage.provider,usage.capability_profile_hash,
    usage.request_evidence_sha256,usage.result_evidence_sha256,
    usage.terminal_outcome,usage.actual_json,usage.billing_state,
    usage.price_source,usage.currency,usage.terminal_at,
    usage.route_fact_sha256,usage.final_phase_fact_sha256,
    usage.source_set_sha256,usage.authority_scope
  )<>1
   OR NOT (
     (usage.ordinal=0
       AND usage.previous_attempt_id IS NULL
       AND EXISTS (
         SELECT 1 FROM audit_l1_candidate_route_authorities_v2 initial_route
         WHERE initial_route.run_id=usage.run_id
           AND initial_route.candidate_id=usage.candidate_id
           AND initial_route.intent=usage.intent
           AND initial_route.provider=usage.provider
           AND initial_route.capability_profile_hash=
                 usage.capability_profile_hash
           AND initial_route.comparator_pool_index=0
       )
       AND audit_l1_attempt_id_valid(
         usage.attempt_id,usage.run_id,usage.candidate_id,usage.intent,
         usage.ordinal,usage.provider,usage.capability_profile_hash,
         usage.request_evidence_sha256,NULL,NULL,NULL,NULL
       )=1)
     OR
     (usage.ordinal>0
       AND previous.run_id=usage.run_id
       AND previous.candidate_id=usage.candidate_id
       AND previous.intent=usage.intent
       AND previous.ordinal=usage.ordinal-1
       AND previous.outcome IN ('failed','cancelled')
       AND julianday(previous.terminal_at)<=julianday(usage.terminal_at)
       AND audit_l1_attempt_id_valid(
         usage.attempt_id,usage.run_id,usage.candidate_id,usage.intent,
         usage.ordinal,usage.provider,usage.capability_profile_hash,
         usage.request_evidence_sha256,previous.attempt_id,
         previous.fact_sha256,previous.outcome,previous.terminal_at
       )=1
       AND EXISTS (
         SELECT 1
         FROM audit_l1_candidate_route_authorities_v2 route
         JOIN json_each(
           route.plan_json,'$.provider_pools_ordered.comparator'
         ) previous_pool ON previous_pool.value=previous.provider
         JOIN json_each(
           route.plan_json,'$.provider_pools_ordered.comparator'
         ) current_pool ON current_pool.value=usage.provider
         WHERE route.run_id=usage.run_id
           AND route.candidate_id=usage.candidate_id
           AND route.intent=usage.intent
           AND route.provider=usage.provider
           AND route.capability_profile_hash=usage.capability_profile_hash
           AND CAST(current_pool.key AS INTEGER) IN (
             CAST(previous_pool.key AS INTEGER),
             CAST(previous_pool.key AS INTEGER)+1
           )
       ))
   )
   OR NOT EXISTS (
     SELECT 1 FROM audit_l1_candidate_route_authorities_v2 route
     WHERE route.run_id=usage.run_id
       AND route.candidate_id=usage.candidate_id
       AND route.intent=usage.intent
       AND route.provider=usage.provider
       AND route.capability_profile_hash=usage.capability_profile_hash
       AND route.route_fact_sha256=usage.route_fact_sha256
       AND route.final_phase_fact_sha256=usage.final_phase_fact_sha256
       AND route.source_set_sha256=usage.source_set_sha256
       AND julianday(usage.terminal_at)>=julianday(route.pre_phase_created_at)
       AND julianday(usage.terminal_at)<=julianday(route.l1_source_created_at)
       AND (
         (usage.terminal_outcome='success'
           AND usage.result_evidence_sha256=route.comparator_receipt_sha256)
         OR
         (usage.terminal_outcome IN ('failed','cancelled')
           AND usage.result_evidence_sha256 IS NULL)
       )
   );
DROP TABLE audit_l1_cost_authority_upgrade_probe;
"""


_SEMANTIC_PRODUCTION_EVIDENCE_SQL = """
CREATE TABLE audit_semantic_production_evidence_v2(
  evidence_id TEXT PRIMARY KEY CHECK(length(evidence_id)=64),
  plan_sha TEXT NOT NULL REFERENCES audit_l2_plans_v2(plan_sha),
  capacity_profile_id TEXT NOT NULL
    REFERENCES audit_capacity_profiles(capacity_profile_id),
  capacity_sha256 TEXT NOT NULL CHECK(length(capacity_sha256)=64),
  provider_profile_hashes_json TEXT NOT NULL,
  provider_sha256 TEXT NOT NULL CHECK(length(provider_sha256)=64),
  ordered_provider_pools_sha256 TEXT NOT NULL
    CHECK(length(ordered_provider_pools_sha256)=64),
  prompt_sha256 TEXT NOT NULL CHECK(length(prompt_sha256)=64),
  schema_sha256 TEXT NOT NULL CHECK(length(schema_sha256)=64),
  fault_report_json TEXT NOT NULL,
  fault_sha256 TEXT NOT NULL CHECK(length(fault_sha256)=64),
  replay_report_json TEXT NOT NULL,
  replay_sha256 TEXT NOT NULL CHECK(length(replay_sha256)=64),
  created_at TEXT NOT NULL,
  UNIQUE(plan_sha,fault_sha256,replay_sha256)
);
""" + _immutable_guards("audit_semantic_production_evidence_v2") + """
CREATE TRIGGER audit_semantic_production_evidence_v2_guard
BEFORE INSERT ON audit_semantic_production_evidence_v2
BEGIN
  SELECT CASE WHEN audit_semantic_production_evidence_insert_allowed(
    NEW.evidence_id,NEW.plan_sha,
    NEW.capacity_profile_id,NEW.capacity_sha256,
    NEW.provider_profile_hashes_json,NEW.provider_sha256,
    NEW.ordered_provider_pools_sha256,NEW.prompt_sha256,NEW.schema_sha256,
    NEW.fault_report_json,NEW.fault_sha256,
    NEW.replay_report_json,NEW.replay_sha256,NEW.created_at
  )<>1 THEN RAISE(ABORT,'semantic production evidence requires host authority') END;
END;
"""


_ROUTER_HOST_PRODUCTION_AUTHORITY_SQL = """
CREATE TABLE audit_router_host_preplan_batches_v2(
  preplan_sha256 TEXT PRIMARY KEY CHECK(length(preplan_sha256)=64),
  run_id TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  intent TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  candidates_json TEXT NOT NULL,
  records_sha256 TEXT NOT NULL CHECK(length(records_sha256)=64),
  records_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(run_id,batch_id,intent)
);
CREATE TABLE audit_router_host_observation_sets_v2(
  observation_set_sha256 TEXT PRIMARY KEY
    CHECK(length(observation_set_sha256)=64),
  route_round_sha256 TEXT NOT NULL UNIQUE
    REFERENCES audit_router_rounds_v2(route_round_sha256),
  run_id TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL CHECK(length(snapshot_id)=64),
  snapshot_hash TEXT NOT NULL CHECK(length(snapshot_hash)=64),
  candidate_ids_json TEXT NOT NULL,
  selected_candidate_id TEXT NOT NULL,
  observations_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE audit_router_host_round_authorities_v2(
  authority_sha256 TEXT PRIMARY KEY CHECK(length(authority_sha256)=64),
  route_round_sha256 TEXT NOT NULL UNIQUE
    REFERENCES audit_router_rounds_v2(route_round_sha256),
  observation_set_sha256 TEXT NOT NULL UNIQUE
    REFERENCES audit_router_host_observation_sets_v2(observation_set_sha256),
  authority_scope TEXT NOT NULL CHECK(authority_scope='host_production'),
  issued_at TEXT NOT NULL
);
CREATE TABLE audit_router_host_l1_comparator_facts_v2(
  comparator_fact_sha256 TEXT PRIMARY KEY CHECK(length(comparator_fact_sha256)=64),
  route_round_sha256 TEXT NOT NULL
    REFERENCES audit_router_rounds_v2(route_round_sha256),
  host_round_authority_sha256 TEXT NOT NULL
    REFERENCES audit_router_host_round_authorities_v2(authority_sha256),
  run_id TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  intent TEXT NOT NULL,
  snapshot_id TEXT NOT NULL CHECK(length(snapshot_id)=64),
  snapshot_hash TEXT NOT NULL CHECK(length(snapshot_hash)=64),
  candidate_id TEXT NOT NULL,
  candidate_hash TEXT NOT NULL CHECK(length(candidate_hash)=64),
  candidate_raw_artifact_sha256 TEXT NOT NULL
    CHECK(length(candidate_raw_artifact_sha256)=64),
  source_order INTEGER NOT NULL CHECK(source_order>=0),
  pre_phase_fact_sha256 TEXT NOT NULL UNIQUE
    REFERENCES audit_router_phase_facts_v2(phase_fact_sha256),
  parser_revision TEXT NOT NULL
    CHECK(parser_revision='history-router-host-l1-parser-v1'),
  raw_comparator_artifact_sha256 TEXT NOT NULL
    CHECK(length(raw_comparator_artifact_sha256)=64),
  raw_comparator_artifact BLOB NOT NULL
    CHECK(typeof(raw_comparator_artifact)='blob'
      AND length(raw_comparator_artifact)>0),
  comparator_outcome TEXT NOT NULL
    CHECK(comparator_outcome IN ('match','no_match','uncertain')),
  coverage_state TEXT NOT NULL CHECK(coverage_state='complete'),
  observed_at TEXT NOT NULL,
  UNIQUE(route_round_sha256,candidate_id)
);
CREATE TABLE audit_router_host_source_authorities_v2(
  authority_sha256 TEXT PRIMARY KEY CHECK(length(authority_sha256)=64),
  source_sha256 TEXT NOT NULL UNIQUE
    REFERENCES audit_router_domain_sources_v2(source_sha256),
  route_round_sha256 TEXT NOT NULL
    REFERENCES audit_router_rounds_v2(route_round_sha256),
  source_kind TEXT NOT NULL CHECK(source_kind IN (
    'selection','l1_observation','calibration','qualification',
    'risk_assignment','dependency_heads','permanent_request'
  )),
  observation_set_sha256 TEXT NOT NULL
    REFERENCES audit_router_host_observation_sets_v2(observation_set_sha256),
  derivation_inputs_json TEXT NOT NULL,
  authority_scope TEXT NOT NULL CHECK(authority_scope='host_production'),
  issued_at TEXT NOT NULL,
  UNIQUE(route_round_sha256,source_kind)
);
""" + _immutable_guards(
    "audit_router_host_preplan_batches_v2",
    "audit_router_host_observation_sets_v2",
    "audit_router_host_round_authorities_v2",
    "audit_router_host_l1_comparator_facts_v2",
    "audit_router_host_source_authorities_v2",
) + """
CREATE TRIGGER audit_router_host_preplan_batches_v2_guard
BEFORE INSERT ON audit_router_host_preplan_batches_v2
BEGIN
  SELECT CASE WHEN audit_router_host_preplan_insert_allowed(
    NEW.preplan_sha256,NEW.run_id,NEW.batch_id,NEW.intent,
    NEW.snapshot_json,NEW.candidates_json,NEW.records_sha256,
    NEW.records_json,NEW.created_at
  )<>1 THEN RAISE(ABORT,'host router preplan requires host authority') END;
  SELECT CASE WHEN audit_router_host_preplan_valid(
    NEW.preplan_sha256,NEW.run_id,NEW.batch_id,NEW.intent,
    NEW.snapshot_json,NEW.candidates_json,NEW.records_sha256,
    NEW.records_json,NEW.created_at
  )<>1 THEN RAISE(ABORT,'host router preplan identity mismatch') END;
END;
CREATE TRIGGER audit_router_host_observation_sets_v2_guard
BEFORE INSERT ON audit_router_host_observation_sets_v2
BEGIN
  SELECT CASE WHEN audit_router_host_observation_insert_allowed(
    NEW.observation_set_sha256,NEW.route_round_sha256,
    NEW.run_id,NEW.batch_id,NEW.snapshot_id,NEW.snapshot_hash,
    NEW.candidate_ids_json,NEW.selected_candidate_id,
    NEW.observations_json,NEW.created_at
  )<>1 THEN RAISE(ABORT,'host router observation requires host authority') END;
  SELECT CASE WHEN audit_router_host_observation_valid(
    NEW.observation_set_sha256,NEW.route_round_sha256,
    NEW.run_id,NEW.batch_id,NEW.snapshot_id,NEW.snapshot_hash,
    NEW.candidate_ids_json,NEW.selected_candidate_id,
    NEW.observations_json,NEW.created_at
  )<>1 THEN RAISE(ABORT,'host router observation identity mismatch') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_router_rounds_v2 round
    WHERE round.route_round_sha256=NEW.route_round_sha256
      AND round.run_id=NEW.run_id
      AND round.batch_id=NEW.batch_id
      AND round.snapshot_id=NEW.snapshot_id
      AND round.snapshot_hash=NEW.snapshot_hash
      AND round.candidate_ids_json=NEW.candidate_ids_json
  ) THEN RAISE(ABORT,'host router observation round mismatch') END;
END;
CREATE TRIGGER audit_router_host_round_authorities_v2_guard
BEFORE INSERT ON audit_router_host_round_authorities_v2
BEGIN
  SELECT CASE WHEN audit_router_host_round_insert_allowed(
    NEW.authority_sha256,NEW.route_round_sha256,
    NEW.observation_set_sha256,NEW.authority_scope,NEW.issued_at
  )<>1 THEN RAISE(ABORT,'host router round requires host authority') END;
  SELECT CASE WHEN audit_router_host_round_valid(
    NEW.authority_sha256,NEW.route_round_sha256,
    NEW.observation_set_sha256,NEW.authority_scope,NEW.issued_at
  )<>1 THEN RAISE(ABORT,'host router round identity mismatch') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_router_host_observation_sets_v2 observation
    WHERE observation.observation_set_sha256=NEW.observation_set_sha256
      AND observation.route_round_sha256=NEW.route_round_sha256
  ) THEN RAISE(ABORT,'host router round observation mismatch') END;
END;
CREATE TRIGGER audit_router_host_l1_comparator_facts_v2_guard
BEFORE INSERT ON audit_router_host_l1_comparator_facts_v2
BEGIN
  SELECT CASE WHEN audit_router_host_l1_fact_insert_allowed(
    NEW.comparator_fact_sha256,NEW.route_round_sha256,
    NEW.host_round_authority_sha256,NEW.run_id,NEW.batch_id,NEW.intent,
    NEW.snapshot_id,NEW.snapshot_hash,NEW.candidate_id,NEW.candidate_hash,
    NEW.candidate_raw_artifact_sha256,NEW.source_order,
    NEW.pre_phase_fact_sha256,NEW.parser_revision,
    NEW.raw_comparator_artifact_sha256,NEW.raw_comparator_artifact,
    NEW.comparator_outcome,NEW.coverage_state,NEW.observed_at
  )<>1 THEN RAISE(ABORT,'host router L1 fact requires host authority') END;
  SELECT CASE WHEN audit_router_host_l1_fact_valid(
    NEW.comparator_fact_sha256,NEW.route_round_sha256,
    NEW.host_round_authority_sha256,NEW.run_id,NEW.batch_id,NEW.intent,
    NEW.snapshot_id,NEW.snapshot_hash,NEW.candidate_id,NEW.candidate_hash,
    NEW.candidate_raw_artifact_sha256,NEW.source_order,
    NEW.pre_phase_fact_sha256,NEW.parser_revision,
    NEW.raw_comparator_artifact_sha256,NEW.raw_comparator_artifact,
    NEW.comparator_outcome,NEW.coverage_state,NEW.observed_at
  )<>1 THEN RAISE(ABORT,'host router L1 fact identity mismatch') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_router_host_round_authorities_v2 host_round
    JOIN audit_router_rounds_v2 round
      ON round.route_round_sha256=host_round.route_round_sha256
    JOIN json_each(round.round_json,'$.candidates') candidate
    JOIN audit_router_phase_facts_v2 pre
      ON pre.phase_fact_sha256=NEW.pre_phase_fact_sha256
    WHERE host_round.authority_sha256=NEW.host_round_authority_sha256
      AND host_round.route_round_sha256=NEW.route_round_sha256
      AND host_round.authority_scope='host_production'
      AND round.run_id=NEW.run_id
      AND round.batch_id=NEW.batch_id
      AND round.intent=NEW.intent
      AND round.snapshot_id=NEW.snapshot_id
      AND round.snapshot_hash=NEW.snapshot_hash
      AND json_extract(candidate.value,'$.candidate_id')=NEW.candidate_id
      AND json_extract(candidate.value,'$.candidate_hash')=NEW.candidate_hash
      AND json_extract(candidate.value,'$.raw_artifact_sha')=
          NEW.candidate_raw_artifact_sha256
      AND json_extract(candidate.value,'$.source_order')=NEW.source_order
      AND pre.route_round_sha256=NEW.route_round_sha256
      AND pre.phase='pre_l1'
      AND pre.candidate_id=NEW.candidate_id
      AND pre.call_l1_model=1
  ) THEN RAISE(ABORT,'host router L1 fact binding mismatch') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM audit_router_domain_sources_v2 source
    WHERE source.route_round_sha256=NEW.route_round_sha256
      AND source.source_kind='l1_observation'
  ) THEN RAISE(ABORT,'host router L1 source is already final') END;
END;
CREATE TRIGGER audit_router_host_source_authorities_v2_guard
BEFORE INSERT ON audit_router_host_source_authorities_v2
BEGIN
  SELECT CASE WHEN audit_router_host_source_insert_allowed(
    NEW.authority_sha256,NEW.source_sha256,NEW.route_round_sha256,
    NEW.source_kind,NEW.observation_set_sha256,
    NEW.derivation_inputs_json,NEW.authority_scope,NEW.issued_at
  )<>1 THEN RAISE(ABORT,'host router source requires host authority') END;
  SELECT CASE WHEN audit_router_host_source_valid(
    NEW.authority_sha256,NEW.source_sha256,NEW.route_round_sha256,
    NEW.source_kind,NEW.observation_set_sha256,
    NEW.derivation_inputs_json,NEW.authority_scope,NEW.issued_at
  )<>1 THEN RAISE(ABORT,'host router source identity mismatch') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_router_domain_sources_v2 source
    JOIN audit_router_host_round_authorities_v2 host_round
      ON host_round.route_round_sha256=source.route_round_sha256
    WHERE source.source_sha256=NEW.source_sha256
      AND source.route_round_sha256=NEW.route_round_sha256
      AND source.source_kind=NEW.source_kind
      AND host_round.observation_set_sha256=NEW.observation_set_sha256
      AND host_round.authority_scope='host_production'
  ) THEN RAISE(ABORT,'host router source binding mismatch') END;
END;
CREATE TABLE audit_router_host_production_authority_probe(
  value INTEGER NOT NULL CHECK(value=0)
);
INSERT INTO audit_router_host_production_authority_probe(value)
SELECT 1
FROM audit_router_host_source_authorities_v2 authority
WHERE NOT EXISTS (
  SELECT 1
  FROM audit_router_domain_sources_v2 source
  JOIN audit_router_host_round_authorities_v2 round_authority
    ON round_authority.route_round_sha256=source.route_round_sha256
  WHERE source.source_sha256=authority.source_sha256
    AND source.route_round_sha256=authority.route_round_sha256
    AND source.source_kind=authority.source_kind
    AND round_authority.observation_set_sha256=
        authority.observation_set_sha256
);
DROP TABLE audit_router_host_production_authority_probe;
"""


_LOGICAL_TASK_TRANSITION_INTEGRITY_SQL = """
CREATE TABLE audit_logical_task_transition_upgrade_probe(
  value INTEGER NOT NULL CHECK(value=0)
);
INSERT INTO audit_logical_task_transition_upgrade_probe(value)
SELECT 1
FROM audit_logical_tasks task
WHERE task.state='settled'
  AND NOT EXISTS (
    SELECT 1 FROM audit_task_settlements_v2 settlement
    WHERE settlement.task_hash=task.task_hash
  );
DROP TABLE audit_logical_task_transition_upgrade_probe;
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
  SELECT CASE WHEN NOT (
    (OLD.state='planned' AND NEW.state='claimed')
    OR (OLD.state='claimed' AND NEW.state IN ('claimed','planned','settling'))
    OR (OLD.state='settling' AND NEW.state='planned')
    OR (
      OLD.state='settling' AND NEW.state='settled'
      AND EXISTS (
        SELECT 1 FROM audit_task_settlements_v2 settlement
        WHERE settlement.task_hash=OLD.task_hash
      )
    )
    OR (
      OLD.state='claimed' AND NEW.state IN ('superseded','exhausted')
      AND audit_l2_terminal_transition_allowed(
        OLD.task_hash, OLD.state, OLD.fence, OLD.claim_token, OLD.lease_until,
        NEW.state, NEW.fence, NEW.claim_token, NEW.lease_until
      )=1
    )
  ) THEN RAISE(ABORT, 'illegal logical task transition') END;
END;
"""

_CORE_AUTHORITY_REPAIR_SQL = """
CREATE TABLE audit_batch_pair_receipt_authority_v3(
  run_id TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  event_state TEXT NOT NULL CHECK(event_state IN ('pending','authorized','quarantined')),
  reason TEXT NOT NULL,
  authority_sha256 TEXT NOT NULL UNIQUE CHECK(length(authority_sha256)=64),
  created_at TEXT NOT NULL,
  PRIMARY KEY(run_id, batch_id, event_state),
  FOREIGN KEY(run_id, batch_id)
    REFERENCES audit_batch_pair_receipts(run_id, batch_id)
);
CREATE TABLE audit_activation_receipt_authority_v3(
  activation_receipt_sha TEXT NOT NULL
    REFERENCES audit_activation_receipts(activation_receipt_sha),
  event_state TEXT NOT NULL CHECK(event_state IN ('pending','authorized','quarantined')),
  reason TEXT NOT NULL,
  authority_sha256 TEXT NOT NULL UNIQUE CHECK(length(authority_sha256)=64),
  created_at TEXT NOT NULL,
  PRIMARY KEY(activation_receipt_sha, event_state)
);
CREATE TABLE audit_task_settlement_authority_v3(
  task_hash TEXT NOT NULL REFERENCES audit_task_settlements_v2(task_hash),
  event_state TEXT NOT NULL CHECK(event_state IN ('authorized','quarantined')),
  reason TEXT NOT NULL,
  authority_sha256 TEXT NOT NULL UNIQUE CHECK(length(authority_sha256)=64),
  created_at TEXT NOT NULL,
  PRIMARY KEY(task_hash, event_state)
);
""" + _immutable_guards(
    "audit_batch_pair_receipt_authority_v3",
    "audit_activation_receipt_authority_v3",
    "audit_task_settlement_authority_v3",
) + """
CREATE VIEW audit_pair_receipt_material_candidates_v3 AS
SELECT receipt.*
FROM audit_batch_pair_receipts receipt
JOIN audit_snapshot_batch_sets batch_set
  ON batch_set.snapshot_id=receipt.snapshot_id
 AND batch_set.run_id=receipt.run_id
 AND batch_set.batch_id=receipt.batch_id
WHERE receipt.pair_plan_sha=audit_pair_plan_sha(
        receipt.run_id, receipt.batch_id, batch_set.member_ids_json)
  AND receipt.pair_plan_sha GLOB replace(hex(zeroblob(64)), '00', '[0123456789abcdef]')
  AND receipt.pair_result_sha GLOB replace(hex(zeroblob(64)), '00', '[0123456789abcdef]')
  AND receipt.pair_count=(batch_set.member_count*(batch_set.member_count-1))/2
  AND NOT EXISTS (
    SELECT 1 FROM audit_batch_pairs pair
    WHERE pair.run_id=receipt.run_id AND pair.batch_id=receipt.batch_id
      AND (pair.pair_plan_sha<>receipt.pair_plan_sha
           OR pair.pair_result_sha<>receipt.pair_result_sha
           OR NOT EXISTS (
             SELECT 1 FROM json_each(batch_set.member_ids_json) left_member
             JOIN json_each(batch_set.member_ids_json) right_member
               ON left_member.value<right_member.value
             WHERE left_member.value=pair.left_staging_candidate_id
               AND right_member.value=pair.right_staging_candidate_id
           ))
  )
  AND NOT EXISTS (
    SELECT 1 FROM json_each(batch_set.member_ids_json) left_member
    JOIN json_each(batch_set.member_ids_json) right_member
      ON left_member.value<right_member.value
    WHERE NOT EXISTS (
      SELECT 1 FROM audit_batch_pairs pair
      WHERE pair.run_id=receipt.run_id AND pair.batch_id=receipt.batch_id
        AND pair.pair_plan_sha=receipt.pair_plan_sha
        AND pair.pair_result_sha=receipt.pair_result_sha
        AND pair.left_staging_candidate_id=left_member.value
        AND pair.right_staging_candidate_id=right_member.value
    )
  );

CREATE VIEW audit_valid_batch_pair_receipt_authority_v3 AS
SELECT receipt.*
FROM audit_pair_receipt_material_candidates_v3 receipt
JOIN audit_batch_pair_receipt_authority_v3 authority
  ON authority.run_id=receipt.run_id AND authority.batch_id=receipt.batch_id
 AND authority.event_state='authorized'
WHERE authority.authority_sha256=audit_pair_receipt_authority_sha(
        receipt.run_id, receipt.batch_id, receipt.snapshot_id,
        receipt.pair_plan_sha, receipt.pair_result_sha, receipt.pair_count,
        receipt.completed_at, authority.event_state, authority.reason)
  AND NOT EXISTS (
    SELECT 1 FROM audit_batch_pair_receipt_authority_v3 quarantine
    WHERE quarantine.run_id=receipt.run_id
      AND quarantine.batch_id=receipt.batch_id
      AND quarantine.event_state='quarantined'
  );

CREATE VIEW audit_activation_receipt_material_candidates_v3 AS
SELECT receipt.*
FROM audit_activation_receipts receipt
JOIN audit_activation_maps activation
  ON activation.activation_receipt_sha=receipt.activation_receipt_sha
 AND activation.staging_candidate_id=receipt.staging_candidate_id
JOIN audit_batch_staging staging
  ON staging.staging_candidate_id=receipt.staging_candidate_id
JOIN audit_snapshots snapshot
  ON snapshot.run_id=staging.run_id AND snapshot.batch_id=staging.batch_id
JOIN audit_valid_batch_pair_receipt_authority_v3 pair
  ON pair.run_id=staging.run_id AND pair.batch_id=staging.batch_id
 AND pair.snapshot_id=snapshot.snapshot_id
JOIN audit_batch_direction_gates_v2 gate
  ON gate.run_id=staging.run_id AND gate.batch_id=staging.batch_id
 AND gate.snapshot_id=snapshot.snapshot_id
JOIN audit_batch_direction_gate_bindings_v2 gate_binding
  ON gate_binding.gate_sha256=gate.gate_sha256
 AND gate_binding.staging_candidate_id=staging.staging_candidate_id
JOIN audit_batch_direction_verdicts_v2 verdict
  ON verdict.verdict_sha256=gate_binding.verdict_sha256
WHERE audit_activation_receipt_core_valid(
        receipt.activation_receipt_sha, receipt.staging_candidate_id,
        receipt.receipt_json, staging.run_id, staging.batch_id,
        snapshot.snapshot_id, snapshot.snapshot_hash,
        snapshot.history_as_of_watermark, staging.candidate_hash,
        staging.raw_artifact_sha, pair.pair_plan_sha, pair.pair_result_sha)=1
  AND json_extract(receipt.receipt_json, '$.legacy_candidate_id')
        =activation.legacy_candidate_id
  AND json_extract(receipt.receipt_json, '$.source_sequence')
        =activation.source_sequence
  AND activation.raw_artifact_sha=staging.raw_artifact_sha
  AND activation.pair_plan_sha=pair.pair_plan_sha
  AND activation.pair_result_sha=pair.pair_result_sha
  AND json_extract(receipt.receipt_json,
        '$.direction_check.schema_version')='history-direction-verdict-v2'
  AND json_extract(receipt.receipt_json,
        '$.direction_check.run_id')=verdict.run_id
  AND json_extract(receipt.receipt_json,
        '$.direction_check.batch_id')=verdict.batch_id
  AND json_extract(receipt.receipt_json,
        '$.direction_check.snapshot_id')=verdict.snapshot_id
  AND json_extract(receipt.receipt_json,
        '$.direction_check.current_batch_ids_hash')
      =verdict.current_batch_ids_hash
  AND json_extract(receipt.receipt_json,
        '$.direction_check.direction_id')=verdict.direction_id
  AND json_extract(receipt.receipt_json,
        '$.direction_check.contract_sha')=verdict.contract_sha
  AND json_extract(receipt.receipt_json,
        '$.direction_check.validator_version')=verdict.validator_version
  AND json_extract(receipt.receipt_json,
        '$.direction_check.artifact_sha')=verdict.artifact_sha
  AND json_extract(receipt.receipt_json,
        '$.direction_check.staging_candidate_id')=verdict.staging_candidate_id
  AND json_extract(receipt.receipt_json,
        '$.direction_check.direction_fit')=verdict.direction_fit
  AND json_extract(receipt.receipt_json,
        '$.direction_check.direction_evidence')
      =json_extract(verdict.evidence_json,'$')
  AND json_extract(receipt.receipt_json,
        '$.direction_check.evidence_sha256')=verdict.evidence_sha256
  AND json_extract(receipt.receipt_json,
        '$.direction_check.verdict_sha256')=verdict.verdict_sha256
  AND json_extract(receipt.receipt_json,
        '$.direction_check.gate_sha256')=gate.gate_sha256
  AND json_extract(receipt.receipt_json,
        '$.direction_check.selector_id')=gate_binding.selector_id
  AND json_extract(receipt.receipt_json,
        '$.direction_check.source_order')=gate_binding.source_order;

CREATE VIEW audit_valid_activation_receipt_authority_v3 AS
SELECT receipt.*
FROM audit_activation_receipt_material_candidates_v3 receipt
JOIN audit_activation_receipt_authority_v3 authority
  ON authority.activation_receipt_sha=receipt.activation_receipt_sha
 AND authority.event_state='authorized'
WHERE authority.authority_sha256=audit_activation_receipt_authority_sha(
        receipt.activation_receipt_sha, receipt.staging_candidate_id,
        receipt.receipt_json, receipt.created_at,
        authority.event_state, authority.reason)
  AND NOT EXISTS (
    SELECT 1 FROM audit_activation_receipt_authority_v3 quarantine
    WHERE quarantine.activation_receipt_sha=receipt.activation_receipt_sha
      AND quarantine.event_state='quarantined'
  );

CREATE VIEW audit_valid_activation_maps_authority_v3 AS
SELECT activation.*
FROM audit_activation_maps activation
JOIN audit_valid_activation_receipt_authority_v3 receipt
  ON receipt.activation_receipt_sha=activation.activation_receipt_sha
 AND receipt.staging_candidate_id=activation.staging_candidate_id;

CREATE VIEW audit_task_settlement_material_candidates_v3 AS
SELECT settlement.*
FROM audit_task_settlements_v2 settlement
WHERE audit_task_settlement_material_valid(
        settlement.task_hash, settlement.settlement_sha256,
        settlement.settlement_kind, settlement.normalized_result_json,
        settlement.valid_attempt_ids_json,
        settlement.valid_output_cas_ids_json)=1
  AND json_array_length(settlement.valid_attempt_ids_json)
      =json_array_length(settlement.valid_output_cas_ids_json)
  AND NOT EXISTS (
    SELECT 1
    FROM json_each(settlement.valid_attempt_ids_json) attempt_id
    JOIN json_each(settlement.valid_output_cas_ids_json) output_id
      ON output_id.key=attempt_id.key
    LEFT JOIN audit_task_attempts attempt
      ON attempt.attempt_id=attempt_id.value
     AND attempt.task_hash=settlement.task_hash
    LEFT JOIN audit_attempt_completions_v2 completion
      ON completion.attempt_id=attempt.attempt_id
     AND completion.outcome='valid'
     AND completion.output_cas_object_id=output_id.value
    WHERE completion.attempt_id IS NULL
       OR (settlement.settlement_kind='equal'
           AND completion.normalized_result_json
               <>settlement.normalized_result_json)
  )
  AND NOT EXISTS (
    SELECT 1
    FROM audit_attempt_completions_v2 completion
    JOIN audit_task_attempts attempt ON attempt.attempt_id=completion.attempt_id
    WHERE attempt.task_hash=settlement.task_hash
      AND completion.outcome='valid'
      AND NOT EXISTS (
        SELECT 1 FROM json_each(settlement.valid_attempt_ids_json) selected
        WHERE selected.value=attempt.attempt_id
      )
  )
  AND (settlement.settlement_kind='equal' OR 2<=(
    SELECT count(DISTINCT completion.normalized_result_json)
    FROM json_each(settlement.valid_attempt_ids_json) selected
    JOIN audit_attempt_completions_v2 completion
      ON completion.attempt_id=selected.value AND completion.outcome='valid'
  ));

CREATE VIEW audit_valid_task_settlement_authority_v3 AS
SELECT settlement.*
FROM audit_task_settlement_material_candidates_v3 settlement
JOIN audit_task_settlement_authority_v3 authority
  ON authority.task_hash=settlement.task_hash
 AND authority.event_state='authorized'
WHERE authority.authority_sha256=audit_task_settlement_authority_sha(
        settlement.task_hash, settlement.settlement_sha256,
        settlement.settlement_kind, settlement.normalized_result_json,
        settlement.valid_attempt_ids_json,
        settlement.valid_output_cas_ids_json, settlement.settled_at,
        authority.event_state, authority.reason)
  AND NOT EXISTS (
    SELECT 1 FROM audit_task_settlement_authority_v3 quarantine
    WHERE quarantine.task_hash=settlement.task_hash
      AND quarantine.event_state='quarantined'
  );

INSERT INTO audit_batch_pair_receipt_authority_v3(
  run_id,batch_id,event_state,reason,authority_sha256,created_at)
SELECT receipt.run_id,receipt.batch_id,
       CASE WHEN candidate.run_id IS NULL THEN 'quarantined' ELSE 'authorized' END,
       CASE WHEN candidate.run_id IS NULL THEN 'legacy_pair_evidence_invalid'
            ELSE 'legacy_pair_evidence_backfill' END,
       audit_pair_receipt_authority_sha(
         receipt.run_id,receipt.batch_id,receipt.snapshot_id,
         receipt.pair_plan_sha,receipt.pair_result_sha,receipt.pair_count,
         receipt.completed_at,
         CASE WHEN candidate.run_id IS NULL THEN 'quarantined' ELSE 'authorized' END,
         CASE WHEN candidate.run_id IS NULL THEN 'legacy_pair_evidence_invalid'
              ELSE 'legacy_pair_evidence_backfill' END),
       receipt.completed_at
FROM audit_batch_pair_receipts receipt
LEFT JOIN audit_pair_receipt_material_candidates_v3 candidate
  ON candidate.run_id=receipt.run_id AND candidate.batch_id=receipt.batch_id;

INSERT INTO audit_activation_receipt_authority_v3(
  activation_receipt_sha,event_state,reason,authority_sha256,created_at)
SELECT receipt.activation_receipt_sha,
       CASE WHEN candidate.activation_receipt_sha IS NULL
            THEN 'quarantined' ELSE 'authorized' END,
       CASE WHEN candidate.activation_receipt_sha IS NULL
            THEN 'legacy_activation_receipt_invalid'
            ELSE 'legacy_activation_receipt_backfill' END,
       audit_activation_receipt_authority_sha(
         receipt.activation_receipt_sha,receipt.staging_candidate_id,
         receipt.receipt_json,receipt.created_at,
         CASE WHEN candidate.activation_receipt_sha IS NULL
              THEN 'quarantined' ELSE 'authorized' END,
         CASE WHEN candidate.activation_receipt_sha IS NULL
              THEN 'legacy_activation_receipt_invalid'
              ELSE 'legacy_activation_receipt_backfill' END),
       receipt.created_at
FROM audit_activation_receipts receipt
LEFT JOIN audit_activation_receipt_material_candidates_v3 candidate
  ON candidate.activation_receipt_sha=receipt.activation_receipt_sha;

INSERT INTO audit_task_settlement_authority_v3(
  task_hash,event_state,reason,authority_sha256,created_at)
SELECT settlement.task_hash,
       CASE WHEN candidate.task_hash IS NULL THEN 'quarantined' ELSE 'authorized' END,
       CASE WHEN candidate.task_hash IS NULL THEN 'legacy_settlement_invalid'
            ELSE 'legacy_settlement_backfill' END,
       audit_task_settlement_authority_sha(
         settlement.task_hash,settlement.settlement_sha256,
         settlement.settlement_kind,settlement.normalized_result_json,
         settlement.valid_attempt_ids_json,
         settlement.valid_output_cas_ids_json,settlement.settled_at,
         CASE WHEN candidate.task_hash IS NULL THEN 'quarantined' ELSE 'authorized' END,
         CASE WHEN candidate.task_hash IS NULL THEN 'legacy_settlement_invalid'
              ELSE 'legacy_settlement_backfill' END),
       settlement.settled_at
FROM audit_task_settlements_v2 settlement
LEFT JOIN audit_task_settlement_material_candidates_v3 candidate
  ON candidate.task_hash=settlement.task_hash;

CREATE TRIGGER audit_batch_pair_receipts_authority_guard_v3
BEFORE INSERT ON audit_batch_pair_receipts
WHEN 0
BEGIN
  SELECT CASE WHEN NEW.pair_plan_sha NOT GLOB replace(hex(zeroblob(64)), '00', '[0123456789abcdef]')
    OR NEW.pair_result_sha NOT GLOB replace(hex(zeroblob(64)), '00', '[0123456789abcdef]')
    OR NOT EXISTS (
      SELECT 1 FROM audit_snapshot_batch_sets batch_set
      WHERE batch_set.snapshot_id=NEW.snapshot_id
        AND batch_set.run_id=NEW.run_id AND batch_set.batch_id=NEW.batch_id
        AND NEW.pair_plan_sha=audit_pair_plan_sha(
          NEW.run_id,NEW.batch_id,batch_set.member_ids_json)
        AND NEW.pair_count=(batch_set.member_count*(batch_set.member_count-1))/2
    ) THEN RAISE(ABORT, 'batch pair receipt authority material is invalid') END;
END;
CREATE TRIGGER audit_batch_pair_receipts_authority_capture_v3
AFTER INSERT ON audit_batch_pair_receipts
BEGIN
  INSERT INTO audit_batch_pair_receipt_authority_v3(
    run_id,batch_id,event_state,reason,authority_sha256,created_at)
  VALUES(
    NEW.run_id,NEW.batch_id,'pending','pair_evidence_pending',
    audit_pair_receipt_authority_sha(
      NEW.run_id,NEW.batch_id,NEW.snapshot_id,NEW.pair_plan_sha,
      NEW.pair_result_sha,NEW.pair_count,NEW.completed_at,
      'pending','pair_evidence_pending'),NEW.completed_at);
END;
CREATE TRIGGER audit_batch_pair_binding_authority_capture_v3
AFTER INSERT ON audit_batch_pair_set_bindings
BEGIN
  INSERT OR IGNORE INTO audit_batch_pair_receipt_authority_v3(
    run_id,batch_id,event_state,reason,authority_sha256,created_at)
  SELECT receipt.run_id,receipt.batch_id,'authorized','pair_evidence_complete',
         audit_pair_receipt_authority_sha(
           receipt.run_id,receipt.batch_id,receipt.snapshot_id,
           receipt.pair_plan_sha,receipt.pair_result_sha,receipt.pair_count,
           receipt.completed_at,'authorized','pair_evidence_complete'),
         receipt.completed_at
  FROM audit_pair_receipt_material_candidates_v3 receipt
  WHERE receipt.run_id=NEW.run_id AND receipt.batch_id=NEW.batch_id;
END;
CREATE TRIGGER audit_batch_pair_authority_capture_v3
AFTER INSERT ON audit_batch_pairs
BEGIN
  INSERT OR IGNORE INTO audit_batch_pair_receipt_authority_v3(
    run_id,batch_id,event_state,reason,authority_sha256,created_at)
  SELECT receipt.run_id,receipt.batch_id,'authorized','pair_evidence_complete',
         audit_pair_receipt_authority_sha(
           receipt.run_id,receipt.batch_id,receipt.snapshot_id,
           receipt.pair_plan_sha,receipt.pair_result_sha,receipt.pair_count,
           receipt.completed_at,'authorized','pair_evidence_complete'),
         receipt.completed_at
  FROM audit_pair_receipt_material_candidates_v3 receipt
  WHERE receipt.run_id=NEW.run_id AND receipt.batch_id=NEW.batch_id;
END;

CREATE TRIGGER audit_activation_receipts_authority_guard_v3
BEFORE INSERT ON audit_activation_receipts
WHEN 0
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_batch_staging staging
    JOIN audit_snapshots snapshot
      ON snapshot.run_id=staging.run_id AND snapshot.batch_id=staging.batch_id
    JOIN audit_valid_batch_pair_receipt_authority_v3 pair
      ON pair.run_id=staging.run_id AND pair.batch_id=staging.batch_id
     AND pair.snapshot_id=snapshot.snapshot_id
    WHERE staging.staging_candidate_id=NEW.staging_candidate_id
      AND audit_activation_receipt_core_valid(
        NEW.activation_receipt_sha,NEW.staging_candidate_id,NEW.receipt_json,
        staging.run_id,staging.batch_id,snapshot.snapshot_id,
        snapshot.snapshot_hash,snapshot.history_as_of_watermark,
        staging.candidate_hash,staging.raw_artifact_sha,
        pair.pair_plan_sha,pair.pair_result_sha)=1
  ) THEN RAISE(ABORT, 'activation receipt authority material is invalid') END;
END;
CREATE TRIGGER audit_activation_receipts_authority_capture_v3
AFTER INSERT ON audit_activation_receipts
BEGIN
  INSERT INTO audit_activation_receipt_authority_v3(
    activation_receipt_sha,event_state,reason,authority_sha256,created_at)
  VALUES(
    NEW.activation_receipt_sha,'pending','activation_mapping_pending',
    audit_activation_receipt_authority_sha(
      NEW.activation_receipt_sha,NEW.staging_candidate_id,NEW.receipt_json,
      NEW.created_at,'pending','activation_mapping_pending'),NEW.created_at);
END;
CREATE TRIGGER audit_activation_maps_authority_guard_v3
BEFORE INSERT ON audit_activation_maps
WHEN NEW.staging_candidate_id GLOB 'stg-v2-*'
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_activation_receipts receipt
    JOIN audit_batch_staging staging
      ON staging.staging_candidate_id=receipt.staging_candidate_id
    WHERE receipt.activation_receipt_sha=NEW.activation_receipt_sha
      AND receipt.staging_candidate_id=NEW.staging_candidate_id
      AND json_extract(receipt.receipt_json,'$.legacy_candidate_id')
          =NEW.legacy_candidate_id
      AND json_extract(receipt.receipt_json,'$.source_sequence')=NEW.source_sequence
      AND json_extract(receipt.receipt_json,'$.raw_artifact_sha')=NEW.raw_artifact_sha
      AND json_extract(receipt.receipt_json,'$.pair_plan_sha')=NEW.pair_plan_sha
      AND json_extract(receipt.receipt_json,'$.pair_result_sha')=NEW.pair_result_sha
  ) THEN RAISE(ABORT, 'activation map is not bound to canonical receipt material') END;
END;
CREATE TRIGGER audit_activation_maps_authority_capture_v3
AFTER INSERT ON audit_activation_maps
BEGIN
  INSERT OR IGNORE INTO audit_activation_receipt_authority_v3(
    activation_receipt_sha,event_state,reason,authority_sha256,created_at)
  SELECT receipt.activation_receipt_sha,'authorized','activation_mapping_complete',
         audit_activation_receipt_authority_sha(
           receipt.activation_receipt_sha,receipt.staging_candidate_id,
           receipt.receipt_json,receipt.created_at,
           'authorized','activation_mapping_complete'),receipt.created_at
  FROM audit_activation_receipt_material_candidates_v3 receipt
  WHERE receipt.activation_receipt_sha=NEW.activation_receipt_sha;
END;

CREATE TRIGGER audit_task_settlements_authority_guard_v3
BEFORE INSERT ON audit_task_settlements_v2
BEGIN
  SELECT CASE WHEN audit_task_settlement_material_valid(
      NEW.task_hash,NEW.settlement_sha256,NEW.settlement_kind,
      NEW.normalized_result_json,NEW.valid_attempt_ids_json,
      NEW.valid_output_cas_ids_json)<>1
    OR json_array_length(NEW.valid_attempt_ids_json)
       <>json_array_length(NEW.valid_output_cas_ids_json)
    OR EXISTS (
      SELECT 1
      FROM json_each(NEW.valid_attempt_ids_json) attempt_id
      JOIN json_each(NEW.valid_output_cas_ids_json) output_id
        ON output_id.key=attempt_id.key
      LEFT JOIN audit_task_attempts attempt
        ON attempt.attempt_id=attempt_id.value AND attempt.task_hash=NEW.task_hash
      LEFT JOIN audit_attempt_completions_v2 completion
        ON completion.attempt_id=attempt.attempt_id
       AND completion.outcome='valid'
       AND completion.output_cas_object_id=output_id.value
      WHERE completion.attempt_id IS NULL
         OR (NEW.settlement_kind='equal'
             AND completion.normalized_result_json<>NEW.normalized_result_json)
    )
    OR EXISTS (
      SELECT 1 FROM audit_attempt_completions_v2 completion
      JOIN audit_task_attempts attempt ON attempt.attempt_id=completion.attempt_id
      WHERE attempt.task_hash=NEW.task_hash AND completion.outcome='valid'
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.valid_attempt_ids_json) selected
          WHERE selected.value=attempt.attempt_id
        )
    )
    OR (NEW.settlement_kind='conflict' AND 2>(
      SELECT count(DISTINCT completion.normalized_result_json)
      FROM json_each(NEW.valid_attempt_ids_json) selected
      JOIN audit_attempt_completions_v2 completion
        ON completion.attempt_id=selected.value AND completion.outcome='valid'
    ))
    THEN RAISE(ABORT, 'task settlement canonical attempt/output binding is invalid') END;
END;
CREATE TRIGGER audit_task_settlements_authority_capture_v3
AFTER INSERT ON audit_task_settlements_v2
BEGIN
  INSERT INTO audit_task_settlement_authority_v3(
    task_hash,event_state,reason,authority_sha256,created_at)
  VALUES(
    NEW.task_hash,'authorized','canonical_settlement',
    audit_task_settlement_authority_sha(
      NEW.task_hash,NEW.settlement_sha256,NEW.settlement_kind,
      NEW.normalized_result_json,NEW.valid_attempt_ids_json,
      NEW.valid_output_cas_ids_json,NEW.settled_at,
      'authorized','canonical_settlement'),NEW.settled_at);
END;

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
  SELECT CASE WHEN NOT (
    (OLD.state='planned' AND NEW.state='claimed')
    OR (OLD.state='claimed' AND NEW.state IN ('claimed','planned','settling'))
    OR (OLD.state='settling' AND NEW.state='planned')
    OR (
      OLD.state='settling' AND NEW.state='settled'
      AND EXISTS (
        SELECT 1 FROM audit_valid_task_settlement_authority_v3 settlement
        WHERE settlement.task_hash=OLD.task_hash
      )
    )
    OR (
      OLD.state='claimed' AND NEW.state IN ('superseded','exhausted')
      AND audit_l2_terminal_transition_allowed(
        OLD.task_hash, OLD.state, OLD.fence, OLD.claim_token, OLD.lease_until,
        NEW.state, NEW.fence, NEW.claim_token, NEW.lease_until
      )=1
    )
  ) THEN RAISE(ABORT, 'illegal logical task transition') END;
END;
"""


_PAIR_RESULT_AUTHORITY_SQL = """
CREATE TABLE audit_batch_pair_result_manifests_v4(
  run_id TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL REFERENCES audit_snapshots(snapshot_id),
  pair_plan_sha TEXT NOT NULL CHECK(length(pair_plan_sha)=64),
  pair_result_sha TEXT NOT NULL CHECK(length(pair_result_sha)=64),
  current_batch_ids_hash TEXT NOT NULL CHECK(length(current_batch_ids_hash)=64),
  member_count INTEGER NOT NULL CHECK(member_count>=1),
  results_json TEXT NOT NULL,
  authority_sha256 TEXT NOT NULL UNIQUE CHECK(length(authority_sha256)=64),
  issued_at TEXT NOT NULL,
  PRIMARY KEY(run_id,batch_id),
  FOREIGN KEY(run_id,batch_id)
    REFERENCES audit_batch_pair_receipts(run_id,batch_id)
);
CREATE TABLE audit_batch_pair_receipt_quarantine_v4(
  run_id TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  quarantined_at TEXT NOT NULL,
  PRIMARY KEY(run_id,batch_id),
  FOREIGN KEY(run_id,batch_id)
    REFERENCES audit_batch_pair_receipts(run_id,batch_id)
);
""" + _immutable_guards(
    "audit_batch_pair_result_manifests_v4",
    "audit_batch_pair_receipt_quarantine_v4",
) + """
CREATE TRIGGER audit_batch_pair_result_manifest_guard_v4
BEFORE INSERT ON audit_batch_pair_result_manifests_v4
BEGIN
  SELECT CASE WHEN audit_pair_result_manifest_insert_allowed(
    NEW.run_id,NEW.batch_id,NEW.snapshot_id,NEW.pair_plan_sha,
    NEW.pair_result_sha,NEW.current_batch_ids_hash,NEW.member_count,
    NEW.results_json,NEW.authority_sha256,NEW.issued_at)<>1
    OR audit_pair_result_manifest_valid(
    NEW.run_id,NEW.batch_id,NEW.snapshot_id,NEW.pair_plan_sha,
    NEW.pair_result_sha,NEW.current_batch_ids_hash,NEW.member_count,
    NEW.results_json,NEW.authority_sha256,NEW.issued_at)<>1
  THEN RAISE(ABORT, 'pair result manifest requires canonical issuance') END;
END;
CREATE TRIGGER audit_batch_pair_receipt_quarantine_capture_v4
AFTER INSERT ON audit_batch_pair_receipts
BEGIN
  INSERT OR IGNORE INTO audit_batch_pair_receipt_quarantine_v4(
    run_id,batch_id,reason,quarantined_at
  ) VALUES(NEW.run_id,NEW.batch_id,'result_evidence_unverified',NEW.completed_at);
END;

CREATE VIEW audit_valid_batch_pair_receipt_authority_v4 AS
SELECT receipt.*,manifest.results_json,
       manifest.authority_sha256 AS result_authority_sha256
FROM audit_batch_pair_receipts receipt
JOIN audit_batch_pair_set_bindings binding
  ON binding.run_id=receipt.run_id AND binding.batch_id=receipt.batch_id
 AND binding.snapshot_id=receipt.snapshot_id
 AND binding.pair_plan_sha=receipt.pair_plan_sha
 AND binding.pair_result_sha=receipt.pair_result_sha
JOIN audit_snapshot_batch_sets batch_set
  ON batch_set.snapshot_id=binding.snapshot_id
 AND batch_set.current_batch_ids_hash=binding.current_batch_ids_hash
 AND batch_set.member_count=binding.member_count
 AND batch_set.run_id=receipt.run_id AND batch_set.batch_id=receipt.batch_id
JOIN audit_batch_pair_result_manifests_v4 manifest
  ON manifest.run_id=receipt.run_id AND manifest.batch_id=receipt.batch_id
 AND manifest.snapshot_id=binding.snapshot_id
 AND manifest.pair_plan_sha=binding.pair_plan_sha
 AND manifest.pair_result_sha=binding.pair_result_sha
 AND manifest.current_batch_ids_hash=binding.current_batch_ids_hash
 AND manifest.member_count=binding.member_count
WHERE receipt.pair_count=(batch_set.member_count*(batch_set.member_count-1))/2
  AND receipt.pair_plan_sha=audit_pair_plan_sha(
        receipt.run_id,receipt.batch_id,batch_set.member_ids_json)
  AND audit_pair_result_manifest_valid(
        manifest.run_id,manifest.batch_id,manifest.snapshot_id,
        manifest.pair_plan_sha,manifest.pair_result_sha,
        manifest.current_batch_ids_hash,manifest.member_count,
        manifest.results_json,manifest.authority_sha256,manifest.issued_at)=1
  AND json_array_length(manifest.results_json)=receipt.pair_count
  AND NOT EXISTS (
    SELECT 1 FROM json_each(manifest.results_json) result
    WHERE NOT EXISTS (
      SELECT 1 FROM json_each(batch_set.member_ids_json) left_member
      JOIN json_each(batch_set.member_ids_json) right_member
        ON left_member.value<right_member.value
      WHERE left_member.value=json_extract(
              result.value,'$.left_staging_candidate_id')
        AND right_member.value=json_extract(
              result.value,'$.right_staging_candidate_id')
    ) OR NOT EXISTS (
      SELECT 1 FROM audit_batch_pairs pair
      WHERE pair.run_id=receipt.run_id AND pair.batch_id=receipt.batch_id
        AND pair.pair_plan_sha=receipt.pair_plan_sha
        AND pair.pair_result_sha=receipt.pair_result_sha
        AND pair.left_staging_candidate_id=json_extract(
              result.value,'$.left_staging_candidate_id')
        AND pair.right_staging_candidate_id=json_extract(
              result.value,'$.right_staging_candidate_id')
    )
  )
  AND NOT EXISTS (
    SELECT 1 FROM json_each(batch_set.member_ids_json) left_member
    JOIN json_each(batch_set.member_ids_json) right_member
      ON left_member.value<right_member.value
    WHERE NOT EXISTS (
      SELECT 1 FROM json_each(manifest.results_json) result
      WHERE json_extract(result.value,'$.left_staging_candidate_id')
              =left_member.value
        AND json_extract(result.value,'$.right_staging_candidate_id')
              =right_member.value
    )
  );

INSERT INTO audit_batch_pair_receipt_quarantine_v4(
  run_id,batch_id,reason,quarantined_at
)
SELECT receipt.run_id,receipt.batch_id,'legacy_result_evidence_unverifiable',
       receipt.completed_at
FROM audit_batch_pair_receipts receipt
WHERE NOT EXISTS (
  SELECT 1 FROM audit_batch_pair_result_manifests_v4 manifest
  WHERE manifest.run_id=receipt.run_id AND manifest.batch_id=receipt.batch_id
);

CREATE TRIGGER audit_activation_maps_pair_authority_guard_v4
BEFORE INSERT ON audit_activation_maps
WHEN NEW.staging_candidate_id GLOB 'stg-v2-*'
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_batch_staging staging
    JOIN audit_valid_batch_pair_receipt_authority_v4 pair
      ON pair.run_id=staging.run_id AND pair.batch_id=staging.batch_id
    WHERE staging.staging_candidate_id=NEW.staging_candidate_id
      AND NEW.pair_plan_sha=pair.pair_plan_sha
      AND NEW.pair_result_sha=pair.pair_result_sha
  ) THEN RAISE(ABORT, 'activation map lacks canonical pair result authority') END;
END;
"""


_L2_TERMINAL_EVIDENCE_SQL = """
CREATE TABLE audit_l2_split_quarantine_v3(
  parent_task_hash TEXT PRIMARY KEY REFERENCES audit_logical_tasks(task_hash),
  reason TEXT NOT NULL,
  quarantined_at TEXT NOT NULL
);
""" + _immutable_guards("audit_l2_split_quarantine_v3") + """
CREATE VIEW audit_l2_valid_split_families_v3 AS
SELECT family.*
FROM audit_l2_valid_split_families_v2 family
JOIN audit_l2_terminal_transition_authority_v2 authority
  ON authority.parent_task_hash=family.parent_task_hash
 AND authority.transition_kind='split'
WHERE EXISTS (
  SELECT 1
  FROM audit_task_attempts attempt
  JOIN audit_attempt_completions_v2 completion
    ON completion.attempt_id=attempt.attempt_id
  JOIN audit_cas_objects output
    ON output.object_id=completion.output_cas_object_id
  WHERE attempt.task_hash=family.parent_task_hash
    AND completion.outcome IN ('overflow','item_set','truncated')
    AND output.integrity_state='verified'
    AND json_extract(attempt.provenance_json,'$.claim_fence')
        =authority.claim_fence
    AND json_extract(attempt.provenance_json,'$.claim_token')
        =authority.claim_token
);
INSERT INTO audit_l2_split_quarantine_v3(
  parent_task_hash,reason,quarantined_at
)
SELECT family.parent_task_hash,'split_failure_evidence_missing',authority.created_at
FROM audit_l2_valid_split_families_v2 family
JOIN audit_l2_terminal_transition_authority_v2 authority
  ON authority.parent_task_hash=family.parent_task_hash
 AND authority.transition_kind='split'
WHERE NOT EXISTS (
  SELECT 1 FROM audit_l2_valid_split_families_v3 valid
  WHERE valid.parent_task_hash=family.parent_task_hash
);
"""


_L2_FAILURE_CLAIM_TRANSFER_SQL = """
CREATE TABLE audit_l2_failure_claim_transfers_v3(
  task_hash TEXT NOT NULL REFERENCES audit_logical_tasks(task_hash),
  attempt_id TEXT NOT NULL REFERENCES audit_task_attempts(attempt_id),
  outcome TEXT NOT NULL CHECK(outcome IN ('overflow','item_set','truncated')),
  source_claim_fence INTEGER NOT NULL CHECK(source_claim_fence>=0),
  source_claim_token TEXT NOT NULL CHECK(length(source_claim_token)>0),
  target_claim_fence INTEGER NOT NULL CHECK(target_claim_fence>source_claim_fence),
  target_claim_token TEXT NOT NULL CHECK(length(target_claim_token)>0),
  target_lease_until TEXT NOT NULL,
  authorization_sha256 TEXT PRIMARY KEY CHECK(length(authorization_sha256)=64),
  created_at TEXT NOT NULL,
  UNIQUE(task_hash,target_claim_fence)
);
""" + _immutable_guards("audit_l2_failure_claim_transfers_v3") + """
CREATE TRIGGER audit_l2_failure_claim_transfers_v3_guard
BEFORE INSERT ON audit_l2_failure_claim_transfers_v3
BEGIN
  SELECT CASE WHEN audit_l2_failure_claim_transfer_insert_allowed(
    NEW.task_hash,NEW.attempt_id,NEW.outcome,
    NEW.source_claim_fence,NEW.source_claim_token,
    NEW.target_claim_fence,NEW.target_claim_token,NEW.target_lease_until,
    NEW.authorization_sha256,NEW.created_at
  )<>1 THEN RAISE(ABORT,'failure claim transfer requires store authority') END;
  SELECT CASE WHEN NEW.authorization_sha256<>
    audit_l2_failure_claim_transfer_sha(
      NEW.task_hash,NEW.attempt_id,NEW.outcome,
      NEW.source_claim_fence,NEW.source_claim_token,
      NEW.target_claim_fence,NEW.target_claim_token,NEW.target_lease_until,
      NEW.created_at
    ) THEN RAISE(ABORT,'failure claim transfer identity mismatch') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_logical_tasks task
    WHERE task.task_hash=NEW.task_hash
      AND task.state='claimed'
      AND task.fence=NEW.target_claim_fence
      AND task.claim_token=NEW.target_claim_token
      AND task.lease_until=NEW.target_lease_until
      AND audit_metadata_lease_live(task.lease_until,NEW.created_at)=1
  ) THEN RAISE(ABORT,'failure claim transfer lacks live target claim') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM audit_task_attempts attempt
    JOIN audit_attempt_completions_v2 completion
      ON completion.attempt_id=attempt.attempt_id
    JOIN audit_cas_objects output
      ON output.object_id=completion.output_cas_object_id
    WHERE attempt.task_hash=NEW.task_hash
      AND attempt.attempt_id=NEW.attempt_id
      AND completion.outcome=NEW.outcome
      AND output.integrity_state='verified'
      AND json_extract(attempt.provenance_json,'$.claim_fence')=
          NEW.source_claim_fence
      AND json_extract(attempt.provenance_json,'$.claim_token')=
          NEW.source_claim_token
      AND NOT EXISTS (
        SELECT 1 FROM audit_task_attempts later
        WHERE later.task_hash=attempt.task_hash
          AND later.ordinal>attempt.ordinal
      )
  ) THEN RAISE(ABORT,'failure claim transfer lacks exact terminal evidence') END;
END;

CREATE VIEW audit_l2_valid_failure_claim_transfers_v3 AS
SELECT transfer.*
FROM audit_l2_failure_claim_transfers_v3 transfer
JOIN audit_task_attempts attempt
  ON attempt.attempt_id=transfer.attempt_id
 AND attempt.task_hash=transfer.task_hash
JOIN audit_attempt_completions_v2 completion
  ON completion.attempt_id=attempt.attempt_id
 AND completion.outcome=transfer.outcome
JOIN audit_cas_objects output
  ON output.object_id=completion.output_cas_object_id
 AND output.integrity_state='verified'
WHERE json_extract(attempt.provenance_json,'$.claim_fence')=
        transfer.source_claim_fence
  AND json_extract(attempt.provenance_json,'$.claim_token')=
        transfer.source_claim_token
  AND NOT EXISTS (
    SELECT 1 FROM audit_task_attempts later
    WHERE later.task_hash=attempt.task_hash AND later.ordinal>attempt.ordinal
  )
  AND transfer.authorization_sha256=audit_l2_failure_claim_transfer_sha(
    transfer.task_hash,transfer.attempt_id,transfer.outcome,
    transfer.source_claim_fence,transfer.source_claim_token,
    transfer.target_claim_fence,transfer.target_claim_token,
    transfer.target_lease_until,transfer.created_at
  );

DROP VIEW audit_l2_valid_split_families_v3;
CREATE VIEW audit_l2_valid_split_families_v3 AS
SELECT family.*
FROM audit_l2_valid_split_families_v2 family
JOIN audit_l2_terminal_transition_authority_v2 authority
  ON authority.parent_task_hash=family.parent_task_hash
 AND authority.transition_kind='split'
WHERE EXISTS (
  SELECT 1
  FROM audit_task_attempts attempt
  JOIN audit_attempt_completions_v2 completion
    ON completion.attempt_id=attempt.attempt_id
  JOIN audit_cas_objects output
    ON output.object_id=completion.output_cas_object_id
  WHERE attempt.task_hash=family.parent_task_hash
    AND completion.outcome IN ('overflow','item_set','truncated')
    AND output.integrity_state='verified'
    AND (
      (
        json_extract(attempt.provenance_json,'$.claim_fence')=
          authority.claim_fence
        AND json_extract(attempt.provenance_json,'$.claim_token')=
          authority.claim_token
      )
      OR EXISTS (
        SELECT 1 FROM audit_l2_valid_failure_claim_transfers_v3 transfer
        WHERE transfer.task_hash=family.parent_task_hash
          AND transfer.attempt_id=attempt.attempt_id
          AND transfer.outcome=completion.outcome
          AND transfer.target_claim_fence=authority.claim_fence
          AND transfer.target_claim_token=authority.claim_token
          AND transfer.target_lease_until=authority.lease_until
      )
    )
);
"""


_AUTHORITY_INPUT_HARDENING_SQL = """
CREATE TABLE audit_attempt_completion_quarantine_v4(
  attempt_id TEXT PRIMARY KEY REFERENCES audit_attempt_completions_v2(attempt_id),
  reason TEXT NOT NULL,
  quarantined_at TEXT NOT NULL
);
CREATE TABLE audit_activation_candidate_quarantine_v4(
  staging_candidate_id TEXT PRIMARY KEY
    REFERENCES audit_activation_maps(staging_candidate_id),
  reason TEXT NOT NULL,
  quarantined_at TEXT NOT NULL
);
""" + _immutable_guards(
    "audit_attempt_completion_quarantine_v4",
    "audit_activation_candidate_quarantine_v4",
) + """
INSERT INTO audit_attempt_completion_quarantine_v4(
  attempt_id,reason,quarantined_at
)
SELECT attempt_id,'normalized_result_noncanonical',completed_at
FROM audit_attempt_completions_v2
WHERE outcome='valid'
  AND audit_normalized_result_json_valid(normalized_result_json)<>1;

-- activation-candidate-backfill-begin
INSERT INTO audit_activation_candidate_quarantine_v4(
  staging_candidate_id,reason,quarantined_at
)
SELECT activation.staging_candidate_id,'legacy_candidate_content_mismatch',
       activation.activated_at
FROM audit_activation_maps activation
LEFT JOIN candidates candidate
  ON candidate.candidate_id=activation.legacy_candidate_id
 AND candidate.source_sequence=activation.source_sequence
 AND candidate.raw_sha256=activation.raw_artifact_sha
WHERE candidate.candidate_id IS NULL;
-- activation-candidate-backfill-end

CREATE TRIGGER audit_attempt_completion_canonical_result_guard_v4
BEFORE INSERT ON audit_attempt_completions_v2
WHEN NEW.outcome='valid'
 AND audit_normalized_result_json_valid(NEW.normalized_result_json)<>1
BEGIN
  SELECT RAISE(ABORT, 'valid completion result must be canonical JSON');
END;
CREATE TRIGGER audit_activation_maps_candidate_content_guard_v4
BEFORE INSERT ON audit_activation_maps
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM candidates candidate
    WHERE candidate.candidate_id=NEW.legacy_candidate_id
      AND candidate.source_sequence=NEW.source_sequence
      AND candidate.raw_sha256=NEW.raw_artifact_sha
  ) THEN RAISE(ABORT, 'activation candidate durable content mismatch') END;
END;

CREATE TRIGGER audit_pair_receipt_authority_insert_guard_v5
BEFORE INSERT ON audit_batch_pair_receipt_authority_v3
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_batch_pair_receipts receipt
    WHERE receipt.run_id=NEW.run_id AND receipt.batch_id=NEW.batch_id
      AND NEW.created_at=receipt.completed_at
      AND (
        (NEW.event_state='pending' AND NEW.reason='pair_evidence_pending')
        OR (NEW.event_state='authorized' AND NEW.reason IN (
          'pair_evidence_complete','legacy_pair_evidence_backfill'))
      )
      AND NEW.authority_sha256=audit_pair_receipt_authority_sha(
        receipt.run_id,receipt.batch_id,receipt.snapshot_id,
        receipt.pair_plan_sha,receipt.pair_result_sha,receipt.pair_count,
        receipt.completed_at,NEW.event_state,NEW.reason)
  ) THEN RAISE(ABORT, 'pair receipt authority event is invalid') END;
END;
CREATE TRIGGER audit_activation_receipt_authority_insert_guard_v5
BEFORE INSERT ON audit_activation_receipt_authority_v3
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_activation_receipts receipt
    WHERE receipt.activation_receipt_sha=NEW.activation_receipt_sha
      AND NEW.created_at=receipt.created_at
      AND (
        (NEW.event_state='pending' AND NEW.reason='activation_mapping_pending')
        OR (NEW.event_state='authorized' AND NEW.reason IN (
          'activation_mapping_complete','legacy_activation_receipt_backfill'))
      )
      AND NEW.authority_sha256=audit_activation_receipt_authority_sha(
        receipt.activation_receipt_sha,receipt.staging_candidate_id,
        receipt.receipt_json,receipt.created_at,NEW.event_state,NEW.reason)
  ) THEN RAISE(ABORT, 'activation receipt authority event is invalid') END;
END;
CREATE TRIGGER audit_task_settlement_authority_insert_guard_v5
BEFORE INSERT ON audit_task_settlement_authority_v3
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_task_settlements_v2 settlement
    WHERE settlement.task_hash=NEW.task_hash
      AND NEW.created_at=settlement.settled_at
      AND NEW.event_state='authorized'
      AND NEW.reason IN ('canonical_settlement','legacy_settlement_backfill')
      AND NEW.authority_sha256=audit_task_settlement_authority_sha(
        settlement.task_hash,settlement.settlement_sha256,
        settlement.settlement_kind,settlement.normalized_result_json,
        settlement.valid_attempt_ids_json,
        settlement.valid_output_cas_ids_json,settlement.settled_at,
        NEW.event_state,NEW.reason)
  ) THEN RAISE(ABORT, 'task settlement authority event is invalid') END;
END;

CREATE VIEW audit_valid_batch_pair_receipt_authority_v5 AS
SELECT receipt.*
FROM audit_valid_batch_pair_receipt_authority_v4 receipt;

CREATE VIEW audit_valid_activation_receipt_authority_v5 AS
SELECT receipt.*
FROM audit_activation_receipt_material_candidates_v3 receipt
JOIN audit_activation_receipt_authority_v3 authority
  ON authority.activation_receipt_sha=receipt.activation_receipt_sha
 AND authority.event_state='authorized'
JOIN audit_activation_maps activation
  ON activation.activation_receipt_sha=receipt.activation_receipt_sha
 AND activation.staging_candidate_id=receipt.staging_candidate_id
JOIN audit_batch_staging staging
  ON staging.staging_candidate_id=receipt.staging_candidate_id
JOIN audit_valid_batch_pair_receipt_authority_v4 pair
  ON pair.run_id=staging.run_id AND pair.batch_id=staging.batch_id
 AND pair.pair_plan_sha=activation.pair_plan_sha
 AND pair.pair_result_sha=activation.pair_result_sha
JOIN candidates candidate
  ON candidate.candidate_id=activation.legacy_candidate_id
 AND candidate.source_sequence=activation.source_sequence
 AND candidate.raw_sha256=activation.raw_artifact_sha
WHERE authority.reason IN (
        'activation_mapping_complete','legacy_activation_receipt_backfill')
  AND authority.authority_sha256=audit_activation_receipt_authority_sha(
        receipt.activation_receipt_sha,receipt.staging_candidate_id,
        receipt.receipt_json,receipt.created_at,
        authority.event_state,authority.reason);

CREATE VIEW audit_task_settlement_material_candidates_v4 AS
SELECT settlement.*
FROM audit_task_settlement_material_candidates_v3 settlement
WHERE NOT EXISTS (
  SELECT 1 FROM json_each(settlement.valid_attempt_ids_json) selected
  JOIN audit_attempt_completions_v2 completion
    ON completion.attempt_id=selected.value AND completion.outcome='valid'
  WHERE audit_normalized_result_json_valid(
          completion.normalized_result_json)<>1
)
AND (
  settlement.settlement_kind='equal'
  OR 2<=(
    SELECT count(DISTINCT completion.normalized_result_json)
    FROM json_each(settlement.valid_attempt_ids_json) selected
    JOIN audit_attempt_completions_v2 completion
      ON completion.attempt_id=selected.value AND completion.outcome='valid'
    WHERE audit_normalized_result_json_valid(
            completion.normalized_result_json)=1
  )
);
CREATE VIEW audit_valid_task_settlement_authority_v5 AS
SELECT settlement.*
FROM audit_task_settlement_material_candidates_v4 settlement
JOIN audit_task_settlement_authority_v3 authority
  ON authority.task_hash=settlement.task_hash
 AND authority.event_state='authorized'
WHERE authority.reason IN ('canonical_settlement','legacy_settlement_backfill')
  AND authority.authority_sha256=audit_task_settlement_authority_sha(
        settlement.task_hash,settlement.settlement_sha256,
        settlement.settlement_kind,settlement.normalized_result_json,
        settlement.valid_attempt_ids_json,
        settlement.valid_output_cas_ids_json,settlement.settled_at,
        authority.event_state,authority.reason);
CREATE TRIGGER audit_logical_tasks_settlement_authority_guard_v5
BEFORE UPDATE ON audit_logical_tasks
WHEN OLD.state='settling' AND NEW.state='settled'
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_valid_task_settlement_authority_v5 settlement
    WHERE settlement.task_hash=OLD.task_hash
  ) THEN RAISE(ABORT, 'settlement lacks canonical completion authority') END;
END;
"""


_L2_DURABLE_ATTEMPT_VALIDATION_SQL = f"""
CREATE TABLE audit_l2_durable_attempt_validation_probe(
  value INTEGER NOT NULL CHECK(value=0)
);
INSERT INTO audit_l2_durable_attempt_validation_probe(value)
SELECT 1
FROM audit_task_attempts attempt
WHERE attempt.ordinal>={history_audit_plan.MAX_ATTEMPTS}
   OR json_type(attempt.provenance_json,'$.ordinal')<>'integer'
   OR json_extract(attempt.provenance_json,'$.ordinal')<>attempt.ordinal
   OR json_extract(attempt.provenance_json,'$.ordinal')>={history_audit_plan.MAX_ATTEMPTS};
INSERT INTO audit_l2_durable_attempt_validation_probe(value)
SELECT 1
FROM audit_runtime_budget_settlements_v2 settlement
JOIN audit_runtime_budget_reservations_v2 reservation USING(attempt_id)
WHERE settlement.usage_verified=1
  AND (
    (json_type(reservation.reserved_json,'$.currency_micros') IS NOT NULL)
    <>
    (json_type(settlement.actual_json,'$.currency_micros') IS NOT NULL)
  );
DROP TABLE audit_l2_durable_attempt_validation_probe;

DROP TRIGGER audit_task_attempts_full_task_authority_guard;
CREATE TRIGGER audit_task_attempts_full_task_authority_guard
BEFORE INSERT ON audit_task_attempts
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM (
      SELECT task_hash FROM audit_l2_valid_task_authority_v2
      UNION
      SELECT task_hash FROM audit_l2_valid_adjudication_task_authority_v2
    ) valid
    JOIN audit_task_bindings_v2 binding ON binding.task_hash=valid.task_hash
    JOIN audit_l2_plans_v2 plan ON plan.plan_sha=binding.plan_sha
    WHERE valid.task_hash=NEW.task_hash
      AND audit_l2_attempt_capability_valid(
        plan.plan_json,binding.provider_pool_json,
        NEW.provenance_json,NEW.ordinal
      )=1
  ) THEN RAISE(ABORT,'attempt lacks validated task authority') END;
END;

DROP TRIGGER audit_runtime_budget_settlements_v2_owner_guard;
CREATE TRIGGER audit_runtime_budget_settlements_v2_owner_guard
BEFORE INSERT ON audit_runtime_budget_settlements_v2
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM audit_task_attempts attempt
    WHERE attempt.attempt_id=NEW.attempt_id
  ) THEN RAISE(ABORT,'budget settlement attempt is missing') END;
  SELECT CASE WHEN audit_l2_budget_settlement_valid(
    NEW.usage_verified,NEW.actual_json,(
      SELECT reservation.reserved_json
      FROM audit_runtime_budget_reservations_v2 reservation
      WHERE reservation.attempt_id=NEW.attempt_id
    )
  )<>1 THEN RAISE(ABORT,'budget settlement usage is invalid') END;
  SELECT CASE WHEN audit_runtime_budget_settlement_insert_allowed(
    NEW.attempt_id,NEW.usage_verified,NEW.actual_json,NEW.created_at
  )<>1 THEN RAISE(ABORT,'budget settlement requires host authority') END;
  SELECT CASE WHEN NOT (
    (
      NEW.usage_verified=1
      AND EXISTS (
        SELECT 1 FROM audit_verified_usage_authorities_v2 authority
        WHERE authority.attempt_id=NEW.attempt_id
          AND authority.actual_json=NEW.actual_json
          AND authority.terminal_at=NEW.created_at
          AND (
            (
              authority.terminal_outcome<>'cancelled'
              AND EXISTS (
                SELECT 1 FROM audit_attempt_completions_v2 completion
                WHERE completion.attempt_id=NEW.attempt_id
                  AND completion.output_cas_object_id=
                      authority.output_cas_object_id
                  AND completion.outcome=authority.terminal_outcome
                  AND completion.completed_at=authority.terminal_at
              )
            )
            OR
            (
              authority.terminal_outcome='cancelled'
              AND NOT EXISTS (
                SELECT 1 FROM audit_attempt_completions_v2 completion
                WHERE completion.attempt_id=NEW.attempt_id
              )
            )
          )
      )
    )
    OR
    (
      NEW.usage_verified=0 AND NEW.actual_json IS NULL
      AND NOT EXISTS (
        SELECT 1 FROM audit_verified_usage_authorities_v2 authority
        WHERE authority.attempt_id=NEW.attempt_id
      )
      AND (
        EXISTS (
          SELECT 1 FROM audit_attempt_completions_v2 completion
          WHERE completion.attempt_id=NEW.attempt_id
            AND completion.completed_at=NEW.created_at
            AND NOT EXISTS (
              SELECT 1 FROM audit_attempt_cost_settlements_v2 cost
              WHERE cost.attempt_id=NEW.attempt_id AND cost.outcome='cancelled'
            )
        )
        OR
        EXISTS (
          SELECT 1 FROM audit_attempt_cost_settlements_v2 cost
          WHERE cost.attempt_id=NEW.attempt_id
            AND cost.outcome='cancelled'
            AND cost.billing_state='unknown'
            AND cost.usage_source='reservation'
            AND cost.completed_at=NEW.created_at
            AND NOT EXISTS (
              SELECT 1 FROM audit_attempt_completions_v2 completion
              WHERE completion.attempt_id=NEW.attempt_id
            )
        )
      )
    )
  ) THEN RAISE(ABORT,'budget settlement lacks exact terminal authority') END;
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
    Migration(
        "candidate-route-observation-boundary", 1,
        _CANDIDATE_ROUTE_OBSERVATION_BOUNDARY_SQL,
    ),
    Migration(
        "coverage-set-integrity-guards", 1,
        _COVERAGE_SET_INTEGRITY_GUARDS_SQL,
    ),
    Migration(
        "l2-adjudication-authority", 1,
        _L2_ADJUDICATION_AUTHORITY_SQL,
    ),
    Migration(
        "receipt-issuance-authority", 1,
        _RECEIPT_ISSUANCE_AUTHORITY_SQL,
    ),
    Migration(
        "l1-batch-direction-authority", 1,
        _L1_BATCH_DIRECTION_AUTHORITY_SQL,
    ),
    Migration(
        "candidate-budget-authority", 1,
        _CANDIDATE_BUDGET_AUTHORITY_SQL,
    ),
    Migration(
        "runtime-usage-authority", 1,
        _RUNTIME_USAGE_AUTHORITY_SQL,
    ),
    Migration(
        "batch-staging-authority", 1,
        _STAGING_AUTHORITY_SQL,
    ),
    Migration(
        "batch-direction-gate-authority", 1,
        _DIRECTION_GATE_AUTHORITY_SQL,
    ),
    Migration(
        "attempt-terminal-authority", 1,
        _ATTEMPT_TERMINAL_AUTHORITY_SQL,
    ),
    Migration(
        "metadata-direction-gate-provenance", 1,
        _METADATA_DIRECTION_GATE_PROVENANCE_SQL,
    ),
    Migration(
        "router-source-authority", 1,
        _ROUTER_SOURCE_AUTHORITY_SQL,
    ),
    Migration(
        "verified-usage-authority", 1,
        _VERIFIED_USAGE_AUTHORITY_SQL,
    ),
    Migration("l1-cost-authority", 1, _L1_COST_AUTHORITY_SQL),
    Migration(
        "semantic-production-evidence-authority", 1,
        _SEMANTIC_PRODUCTION_EVIDENCE_SQL,
    ),
    Migration(
        "router-host-production-authority", 1,
        _ROUTER_HOST_PRODUCTION_AUTHORITY_SQL,
    ),
    Migration(
        "l2-snapshot-records-per-snapshot", 1,
        _L2_SNAPSHOT_RECORDS_PER_SNAPSHOT_SQL,
    ),
    Migration("l2-plans-per-run", 1, _L2_PLANS_PER_RUN_SQL),
    Migration(
        "logical-task-transition-integrity", 1,
        _LOGICAL_TASK_TRANSITION_INTEGRITY_SQL,
    ),
    Migration(
        "core-authority-repair", 1, _CORE_AUTHORITY_REPAIR_SQL,
    ),
    Migration(
        "pair-result-authority", 1, _PAIR_RESULT_AUTHORITY_SQL,
    ),
    Migration(
        "l2-terminal-failure-evidence", 1, _L2_TERMINAL_EVIDENCE_SQL,
    ),
    Migration(
        "authority-input-hardening", 1, _AUTHORITY_INPUT_HARDENING_SQL,
    ),
    Migration(
        "l2-failure-claim-transfer", 1, _L2_FAILURE_CLAIM_TRANSFER_SQL,
    ),
    Migration(
        "l2-durable-attempt-validation", 1,
        _L2_DURABLE_ATTEMPT_VALIDATION_SQL,
    ),
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


def _pair_plan_sha(run_id, batch_id, member_ids_json):
    try:
        _current_batch_ids_sha(member_ids_json)
        member_ids = json.loads(member_ids_json)
    except (TypeError, ValueError):
        return None
    pairs = [
        {
            "left_staging_candidate_id": left,
            "right_staging_candidate_id": right,
            "comparison_kinds": ["exact", "semantic"],
        }
        for index, left in enumerate(member_ids)
        for right in member_ids[index + 1:]
    ]
    return history_contract_v2.framed_sha256(
        "history-batch-pair-plan-v2",
        history_contract_v2.canonical_bytes({
            "run_id": run_id,
            "batch_id": batch_id,
            "staging_candidate_ids": member_ids,
            "pairs": pairs,
        }),
    )


def _authority_event_sha(domain, values):
    parts = []
    for value in values:
        if isinstance(value, str):
            parts.append(b"s\0" + value.encode("utf-8"))
        elif type(value) is int:
            parts.append(b"i\0" + str(value).encode("ascii"))
        elif value is None:
            parts.append(b"n\0")
        else:
            raise ValueError("authority event material contains an invalid value")
    return history_contract_v2.framed_sha256(domain, *parts)


def _pair_receipt_authority_sha(*values):
    return _authority_event_sha(
        "history-batch-pair-receipt-authority-v3", values
    )


def _normalized_pair_result_evidence(results_json):
    parsed = _authority_canonical_json_text(results_json, newline=True)
    required = {
        "left_staging_candidate_id", "right_staging_candidate_id",
        "semantic_relation", "evidence_sha",
    }
    if (
        not isinstance(parsed, list)
        or any(not isinstance(item, dict) or set(item) != required for item in parsed)
        or any(
            not all(isinstance(item[name], str) for name in required)
            or re.fullmatch(
                r"stg-v2-[0-9a-f]{64}", item["left_staging_candidate_id"]
            ) is None
            or re.fullmatch(
                r"stg-v2-[0-9a-f]{64}", item["right_staging_candidate_id"]
            ) is None
            or item["left_staging_candidate_id"] >= item["right_staging_candidate_id"]
            or item["semantic_relation"] not in {
                "blocking_duplicate", "substantive_overlap", "related_only",
                "distinct", "uncertain",
            }
            or re.fullmatch(r"[0-9a-f]{64}", item["evidence_sha"] or "") is None
            for item in parsed
        )
    ):
        return _INVALID_AUTHORITY_JSON
    keys = [
        (item["left_staging_candidate_id"], item["right_staging_candidate_id"])
        for item in parsed
    ]
    if keys != sorted(keys) or len(set(keys)) != len(keys):
        return _INVALID_AUTHORITY_JSON
    return parsed


def _pair_result_sha(pair_plan_sha, results_json):
    parsed = _normalized_pair_result_evidence(results_json)
    if parsed is _INVALID_AUTHORITY_JSON:
        return None
    return history_contract_v2.framed_sha256(
        "history-batch-pair-result-v2",
        history_contract_v2.canonical_bytes({
            "pair_plan_sha": pair_plan_sha,
            "results": parsed,
        }),
    )


def _pair_result_manifest_authority_sha(*values):
    return _authority_event_sha(
        "history-batch-pair-result-authority-v4", values
    )


def _pair_result_manifest_valid(
    run_id, batch_id, snapshot_id, pair_plan_sha, pair_result_sha,
    current_batch_ids_hash, member_count, results_json, authority_sha, issued_at,
):
    if (
        _pair_result_sha(pair_plan_sha, results_json) != pair_result_sha
        or type(member_count) is not int
        or member_count < 1
    ):
        return 0
    expected = _pair_result_manifest_authority_sha(
        run_id, batch_id, snapshot_id, pair_plan_sha, pair_result_sha,
        current_batch_ids_hash, member_count, results_json, issued_at,
    )
    return 1 if authority_sha == expected else 0


def _authority_canonical_json_text(value, *, newline=False):
    if not isinstance(value, str):
        return _INVALID_AUTHORITY_JSON
    try:
        parsed = json.loads(
            value,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"nonfinite JSON constant: {token}")
            ),
        )
        canonical = history_contract_v2.canonical_bytes(parsed).decode("utf-8")
    except (
        history_contract_v2.ContractV2Error, TypeError, ValueError,
    ):
        return _INVALID_AUTHORITY_JSON
    if not newline:
        canonical = canonical.rstrip("\n")
    return parsed if canonical == value else _INVALID_AUTHORITY_JSON


def _activation_receipt_core_valid(
    receipt_sha, staging_candidate_id, receipt_json, run_id, batch_id,
    snapshot_id, snapshot_hash, watermark, candidate_hash, raw_artifact_sha,
    pair_plan_sha, pair_result_sha,
):
    parsed = _authority_canonical_json_text(receipt_json, newline=True)
    if (
        not isinstance(parsed, dict)
        or hashlib.sha256(receipt_json.encode("utf-8")).hexdigest() != receipt_sha
        or parsed.get("schema_version") != "history-activation-receipt-v2"
    ):
        return 0
    expected = {
        "run_id": run_id,
        "batch_id": batch_id,
        "snapshot_id": snapshot_id,
        "snapshot_hash": snapshot_hash,
        "history_as_of_watermark": watermark,
        "staging_candidate_id": staging_candidate_id,
        "candidate_hash": candidate_hash,
        "raw_artifact_sha": raw_artifact_sha,
        "pair_plan_sha": pair_plan_sha,
        "pair_result_sha": pair_result_sha,
    }
    return 1 if all(parsed.get(key) == value for key, value in expected.items()) else 0


def _activation_receipt_authority_sha(*values):
    return _authority_event_sha(
        "history-activation-receipt-authority-v3", values
    )


def _task_settlement_material_valid(
    task_hash, settlement_sha, settlement_kind, normalized_result_json,
    valid_attempt_ids_json, valid_output_cas_ids_json,
):
    attempt_ids = _authority_canonical_json_text(
        valid_attempt_ids_json, newline=True
    )
    output_ids = _authority_canonical_json_text(
        valid_output_cas_ids_json, newline=True
    )
    if (
        settlement_kind not in {"equal", "conflict"}
        or not isinstance(attempt_ids, list)
        or not attempt_ids
        or attempt_ids != sorted(attempt_ids)
        or len(set(attempt_ids)) != len(attempt_ids)
        or not isinstance(output_ids, list)
        or len(output_ids) != len(attempt_ids)
        or any(
            not isinstance(value, str)
            or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in attempt_ids + output_ids
        )
    ):
        return 0
    if settlement_kind == "equal":
        normalized = _authority_canonical_json_text(
            normalized_result_json, newline=True
        )
        if normalized is _INVALID_AUTHORITY_JSON:
            return 0
    else:
        if normalized_result_json is not None:
            return 0
        normalized = None
    material = {
        "task_hash": task_hash,
        "settlement_kind": settlement_kind,
        "normalized_result": normalized,
        "valid_attempt_ids": attempt_ids,
        "valid_output_cas_ids": output_ids,
    }
    try:
        expected = history_contract_v2.framed_sha256(
            "history-task-settlement-v2",
            history_contract_v2.canonical_bytes(material),
        )
    except (
        history_contract_v2.ContractV2Error, TypeError, ValueError,
    ):
        return 0
    return 1 if settlement_sha == expected else 0


def _normalized_result_json_valid(value):
    return 1 if _authority_canonical_json_text(
        value, newline=True
    ) is not _INVALID_AUTHORITY_JSON else 0


def _task_settlement_authority_sha(*values):
    return _authority_event_sha(
        "history-task-settlement-authority-v3", values
    )


def batch_staging_authority_sha256(
    staging_candidate_id, run_id, batch_id, candidate_hash,
    raw_artifact_sha, source_order, authority_kind, issued_at,
):
    if (
        not isinstance(staging_candidate_id, str)
        or not staging_candidate_id
        or not isinstance(run_id, str) or not run_id
        or not isinstance(batch_id, str) or not batch_id
        or re.fullmatch(r"[0-9a-f]{64}", candidate_hash or "") is None
        or re.fullmatch(r"[0-9a-f]{64}", raw_artifact_sha or "") is None
        or type(source_order) is not int or source_order < 0
        or authority_kind not in {
            "host_issued", "migration_v2", "migration_legacy"
        }
    ):
        raise ValueError("batch staging authority material is invalid")
    _semantic_timestamp(issued_at, "issued_at")
    material = {
        "schema_version": "history-batch-staging-authority-v2",
        "staging_candidate_id": staging_candidate_id,
        "run_id": run_id,
        "batch_id": batch_id,
        "candidate_hash": candidate_hash,
        "raw_artifact_sha": raw_artifact_sha,
        "source_order": source_order,
        "authority_kind": authority_kind,
        "issued_at": issued_at,
    }
    return _semantic_sha("history-batch-staging-authority-v2", material)


def _batch_staging_authority_row_valid(*values):
    if len(values) != 9:
        return 0
    try:
        return 1 if values[0] == batch_staging_authority_sha256(
            *values[1:]
        ) else 0
    except (TypeError, ValueError):
        return 0


_DIRECTION_VERDICT_MATERIAL_FIELDS = frozenset({
    "schema_version", "run_id", "batch_id", "snapshot_id",
    "current_batch_ids_hash", "direction_id", "contract_sha",
    "validator_version", "artifact_sha", "staging_candidate_id",
    "direction_fit", "direction_evidence",
})


def normalize_direction_evidence(value):
    """Return one non-empty canonical JSON direction evidence value."""
    if (
        value is None
        or type(value) is bool
        or isinstance(value, (int, float))
        or value == ""
        or value == []
        or value == {}
    ):
        raise ValueError("direction evidence must be non-empty JSON evidence")
    try:
        encoded = history_contract_v2.canonical_bytes(value)
        return history_contract_v2.parse_json_bytes(encoded)
    except history_contract_v2.ContractV2Error as exc:
        raise ValueError("direction evidence is not canonical JSON") from exc


def direction_evidence_sha256(value):
    evidence = normalize_direction_evidence(value)
    return history_contract_v2.framed_sha256(
        "history-direction-evidence-v2",
        history_contract_v2.canonical_bytes(evidence),
    )


def direction_verdict_sha256(material):
    """Compute the host canonical identity of one closed direction verdict."""
    if not isinstance(material, dict) or set(material) != _DIRECTION_VERDICT_MATERIAL_FIELDS:
        raise ValueError("direction verdict material schema is closed")
    normalized = copy.deepcopy(material)
    normalized["direction_evidence"] = normalize_direction_evidence(
        normalized["direction_evidence"]
    )
    if normalized["schema_version"] != "history-direction-verdict-v2":
        raise ValueError("direction verdict schema version is invalid")
    for name in (
        "run_id", "batch_id", "direction_id", "validator_version"
    ):
        if not isinstance(normalized[name], str) or not normalized[name]:
            raise ValueError(f"direction verdict {name} is invalid")
    for name in (
        "snapshot_id", "current_batch_ids_hash", "contract_sha", "artifact_sha"
    ):
        if (
            not isinstance(normalized[name], str)
            or re.fullmatch(r"[0-9a-f]{64}", normalized[name]) is None
        ):
            raise ValueError(f"direction verdict {name} is invalid")
    if (
        not isinstance(normalized["staging_candidate_id"], str)
        or re.fullmatch(
            r"stg-v2-[0-9a-f]{64}", normalized["staging_candidate_id"]
        ) is None
    ):
        raise ValueError("direction verdict staging candidate is invalid")
    if normalized["direction_fit"] not in {"in-scope", "out-of-scope"}:
        raise ValueError("direction fit is invalid")
    return history_contract_v2.framed_sha256(
        "history-direction-verdict-v2",
        history_contract_v2.canonical_bytes(normalized),
    )


def _direction_verdict_row_valid(*values):
    if len(values) != 13:
        return 0
    (
        verdict_sha, run_id, batch_id, snapshot_id, current_batch_ids_hash,
        direction_id, contract_sha, validator_version, artifact_sha,
        staging_candidate_id, direction_fit, evidence_json, evidence_sha,
    ) = values
    try:
        if not isinstance(evidence_json, str):
            return 0
        evidence = history_contract_v2.parse_json_bytes(
            evidence_json.encode("utf-8")
        )
        if history_contract_v2.canonical_bytes(evidence).decode("utf-8") != evidence_json:
            return 0
        material = {
            "schema_version": "history-direction-verdict-v2",
            "run_id": run_id,
            "batch_id": batch_id,
            "snapshot_id": snapshot_id,
            "current_batch_ids_hash": current_batch_ids_hash,
            "direction_id": direction_id,
            "contract_sha": contract_sha,
            "validator_version": validator_version,
            "artifact_sha": artifact_sha,
            "staging_candidate_id": staging_candidate_id,
            "direction_fit": direction_fit,
            "direction_evidence": evidence,
        }
        return 1 if (
            verdict_sha == direction_verdict_sha256(material)
            and evidence_sha == direction_evidence_sha256(evidence)
        ) else 0
    except (TypeError, ValueError, history_contract_v2.ContractV2Error):
        return 0


def _clear_direction_verdict_guard(guard):
    guard["active"] = False
    guard["expected_verdicts"] = set()
    guard["expected_bindings"] = set()
    guard["expected_gate"] = None


_DIRECTION_GATE_MATERIAL_FIELDS = frozenset({
    "schema_version", "run_id", "batch_id", "snapshot_id",
    "current_batch_ids_hash", "direction_id", "contract_sha",
    "validator_version", "artifact_sha", "parser_revision",
    "raw_selector_artifact_sha256", "member_count",
    "candidate_mapping", "verdict_set", "verdict_set_sha256", "issued_at",
})
_DIRECTION_GATE_MAPPING_FIELDS = frozenset({
    "selector_id", "staging_candidate_id", "source_order",
})
_DIRECTION_GATE_VERDICT_FIELDS = frozenset({
    "selector_id", "staging_candidate_id", "source_order",
    "direction_fit", "direction_evidence", "evidence_sha256",
    "verdict_sha256",
})


def _canonical_json_text(value):
    return history_contract_v2.canonical_bytes(value).decode("utf-8").rstrip("\n")


def _parse_canonical_json_text(value, label):
    if not isinstance(value, str):
        raise ValueError(f"{label} must be canonical JSON text")
    try:
        parsed = history_contract_v2.parse_json_bytes(value.encode("utf-8"))
    except history_contract_v2.ContractV2Error as exc:
        raise ValueError(f"{label} is invalid JSON") from exc
    if _canonical_json_text(parsed) != value:
        raise ValueError(f"{label} is not canonical JSON")
    return parsed


def direction_gate_sha256(material):
    """Compute the closed identity of one full-batch direction gate."""
    if not isinstance(material, dict) or set(material) != _DIRECTION_GATE_MATERIAL_FIELDS:
        raise ValueError("direction gate material schema is closed")
    normalized = copy.deepcopy(material)
    if normalized["schema_version"] != "history-batch-direction-gate-v2":
        raise ValueError("direction gate schema version is invalid")
    for name in ("run_id", "batch_id", "direction_id", "validator_version"):
        if not isinstance(normalized[name], str) or not normalized[name]:
            raise ValueError(f"direction gate {name} is invalid")
    for name in (
        "snapshot_id", "current_batch_ids_hash", "contract_sha", "artifact_sha",
        "raw_selector_artifact_sha256", "verdict_set_sha256",
    ):
        if re.fullmatch(r"[0-9a-f]{64}", normalized.get(name) or "") is None:
            raise ValueError(f"direction gate {name} is invalid")
    if normalized["parser_revision"] != DIRECTION_VERDICT_PARSER_REVISION:
        raise ValueError("direction gate parser revision is invalid")
    member_count = normalized["member_count"]
    mapping = normalized["candidate_mapping"]
    verdict_set = normalized["verdict_set"]
    if (
        type(member_count) is not int or member_count < 1
        or not isinstance(mapping, list) or len(mapping) != member_count
        or not isinstance(verdict_set, list) or len(verdict_set) != member_count
    ):
        raise ValueError("direction gate member coverage is invalid")
    normalized_mapping = []
    normalized_verdicts = []
    staging_ids = set()
    verdict_shas = set()
    for source_order, (mapping_item, verdict_item) in enumerate(
        zip(mapping, verdict_set)
    ):
        selector_id = f"I{source_order + 1}"
        if (
            not isinstance(mapping_item, dict)
            or set(mapping_item) != _DIRECTION_GATE_MAPPING_FIELDS
            or mapping_item.get("selector_id") != selector_id
            or mapping_item.get("source_order") != source_order
            or not isinstance(mapping_item.get("staging_candidate_id"), str)
            or not mapping_item["staging_candidate_id"]
            or mapping_item["staging_candidate_id"] in staging_ids
        ):
            raise ValueError("direction gate candidate mapping is invalid")
        if (
            not isinstance(verdict_item, dict)
            or set(verdict_item) != _DIRECTION_GATE_VERDICT_FIELDS
            or any(
                verdict_item.get(name) != mapping_item[name]
                for name in _DIRECTION_GATE_MAPPING_FIELDS
            )
            or verdict_item.get("direction_fit") not in {
                "in-scope", "out-of-scope"
            }
            or not isinstance(verdict_item.get("direction_evidence"), str)
            or not verdict_item["direction_evidence"]
            or len(verdict_item["direction_evidence"].encode("utf-8")) > 2048
            or re.fullmatch(
                r"[0-9a-f]{64}", verdict_item.get("evidence_sha256") or ""
            ) is None
            or re.fullmatch(
                r"[0-9a-f]{64}", verdict_item.get("verdict_sha256") or ""
            ) is None
            or verdict_item["verdict_sha256"] in verdict_shas
        ):
            raise ValueError("direction gate verdict set is invalid")
        evidence = verdict_item["direction_evidence"]
        verdict_material = {
            "schema_version": "history-direction-verdict-v2",
            "run_id": normalized["run_id"],
            "batch_id": normalized["batch_id"],
            "snapshot_id": normalized["snapshot_id"],
            "current_batch_ids_hash": normalized["current_batch_ids_hash"],
            "direction_id": normalized["direction_id"],
            "contract_sha": normalized["contract_sha"],
            "validator_version": normalized["validator_version"],
            "artifact_sha": normalized["artifact_sha"],
            "staging_candidate_id": mapping_item["staging_candidate_id"],
            "direction_fit": verdict_item["direction_fit"],
            "direction_evidence": evidence,
        }
        if (
            verdict_item["evidence_sha256"] != direction_evidence_sha256(evidence)
            or verdict_item["verdict_sha256"]
            != direction_verdict_sha256(verdict_material)
        ):
            raise ValueError("direction gate verdict identity is invalid")
        staging_ids.add(mapping_item["staging_candidate_id"])
        verdict_shas.add(verdict_item["verdict_sha256"])
        normalized_mapping.append(copy.deepcopy(mapping_item))
        normalized_verdicts.append(copy.deepcopy(verdict_item))
    expected_verdict_set_sha = history_contract_v2.framed_sha256(
        "history-batch-direction-verdict-set-v2",
        history_contract_v2.canonical_bytes(normalized_verdicts),
    )
    if normalized["verdict_set_sha256"] != expected_verdict_set_sha:
        raise ValueError("direction gate verdict set hash is invalid")
    _semantic_timestamp(normalized["issued_at"], "issued_at")
    normalized["candidate_mapping"] = normalized_mapping
    normalized["verdict_set"] = normalized_verdicts
    return history_contract_v2.framed_sha256(
        "history-batch-direction-gate-v2",
        history_contract_v2.canonical_bytes(normalized),
    )


def _direction_gate_row_valid(*values):
    if len(values) != 17:
        return 0
    (
        gate_sha, run_id, batch_id, snapshot_id, current_batch_ids_hash,
        direction_id, contract_sha, validator_version, artifact_sha,
        parser_revision, raw_selector_artifact_sha, member_count,
        candidate_mapping_json, verdict_set_json, verdict_set_sha,
        verdict_tsv, issued_at,
    ) = values
    try:
        if (
            not isinstance(verdict_tsv, bytes)
            or not 0 < len(verdict_tsv) <= MAX_DIRECTION_VERDICT_BYTES
            or hashlib.sha256(verdict_tsv).hexdigest()
            != raw_selector_artifact_sha
        ):
            return 0
        mapping = _parse_canonical_json_text(
            candidate_mapping_json, "direction gate candidate mapping"
        )
        verdict_set = _parse_canonical_json_text(
            verdict_set_json, "direction gate verdict set"
        )
        selector_ids = [item["selector_id"] for item in mapping]
        parsed = direction_contract.parse_direction_verdicts(
            verdict_tsv, selector_ids
        )
        if any(
            parsed_item != {
                "candidate_id": verdict_item["selector_id"],
                "direction_fit": verdict_item["direction_fit"],
                "evidence": verdict_item["direction_evidence"],
            }
            for parsed_item, verdict_item in zip(parsed, verdict_set)
        ):
            return 0
        material = {
            "schema_version": "history-batch-direction-gate-v2",
            "run_id": run_id,
            "batch_id": batch_id,
            "snapshot_id": snapshot_id,
            "current_batch_ids_hash": current_batch_ids_hash,
            "direction_id": direction_id,
            "contract_sha": contract_sha,
            "validator_version": validator_version,
            "artifact_sha": artifact_sha,
            "parser_revision": parser_revision,
            "raw_selector_artifact_sha256": raw_selector_artifact_sha,
            "member_count": member_count,
            "candidate_mapping": mapping,
            "verdict_set": verdict_set,
            "verdict_set_sha256": verdict_set_sha,
            "issued_at": issued_at,
        }
        return 1 if gate_sha == direction_gate_sha256(material) else 0
    except (
        KeyError, TypeError, ValueError, UnicodeError,
        direction_contract.DirectionContractError,
    ):
        return 0


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
        if usage_verified in (None, 0) and actual_json is None:
            usage = _closed_json(reserved_json)
        elif usage_verified == 1 and actual_json is not None:
            usage = _verified_actual_usage(_closed_json(actual_json))
        else:
            return 2 ** 63 - 1
        value = usage.get(field, 0)
        return value if type(value) is int and value >= 0 else 2 ** 63 - 1
    except (ValueError, TypeError, KeyError):
        return 2 ** 63 - 1


def _l2_budget_settlement_valid(
    usage_verified, actual_json, reserved_json=_INVALID_AUTHORITY_JSON
):
    if reserved_json is _INVALID_AUTHORITY_JSON:
        if usage_verified == 0:
            return 1 if actual_json is None else 0
        if usage_verified != 1 or not isinstance(actual_json, str):
            return 0
        try:
            _verified_actual_usage(_closed_json(actual_json))
            return 1
        except (TypeError, ValueError):
            return 0
    try:
        reserved = _closed_json(reserved_json)
    except (TypeError, ValueError):
        return 0
    if not isinstance(reserved, dict):
        return 0
    if usage_verified == 0:
        return 1 if actual_json is None else 0
    if usage_verified != 1 or not isinstance(actual_json, str):
        return 0
    try:
        actual = _verified_actual_usage(_closed_json(actual_json))
        return 1 if (
            ("currency_micros" in reserved)
            == ("currency_micros" in actual)
        ) else 0
    except (TypeError, ValueError):
        return 0


def _completion_usage_valid(usage_json):
    return 1 if usage_json == history_contract_v2.canonical_bytes({}).decode(
        "utf-8"
    ) else 0


def _l2_attempt_capability_valid(
    plan_json, provider_pool_json, provenance_json, ordinal=None
):
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
        if (
            history_audit_plan._runtime_authority_revision(plan)
            != history_audit_plan._LEGACY_AUTHORITY_REVISION
        ):
            capability_fields.update({
                "max_output_tokens", "output_token_cap_binding",
                "output_token_cap_semantics",
            })
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
            or provenance["ordinal"] >= history_audit_plan.MAX_ATTEMPTS
            or (
                ordinal is not None
                and (
                    type(ordinal) is not int
                    or ordinal != provenance["ordinal"]
                )
            )
            or not isinstance(provenance["claim_token"], str)
            or not provenance["claim_token"]
            or type(provenance["claim_fence"]) is not int
            or provenance["claim_fence"] < 0
        ):
            return 0
        expected_provider = history_audit_plan.runtime_attempt_provider(
            pool, provenance["ordinal"], provenance["attempt_kind"]
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


def _l2_sha_text(value):
    return (
        type(value) is str
        and re.fullmatch(r"[0-9a-f]{64}", value) is not None
    )


def _l2_generation_identity_sha(material):
    return _l2_fact_sha("history-l2-adjudication-generation-v1", material)


def _l2_adjudication_generation_valid(plan_json, material_json, generation_id):
    try:
        plan = history_audit_plan.validate_runtime_plan_material(
            _closed_json(plan_json)
        )
        material = _closed_json(material_json)
        fields = {
            "schema_version", "plan_sha", "snapshot_id", "snapshot_hash",
            "candidate_id", "candidate_hash", "map_terminal_facts",
            "map_terminal_set_sha", "exceptional_cards",
            "exceptional_cards_sha", "detail_provider_pool",
            "reduce_provider_pool", "capacity_profile_sha",
            "prompt_identity_sha", "schema_identity_sha",
            "budget_policy_sha", "risk_policy_sha",
            "settlement_policy_sha",
        }
        if (
            type(material) is not dict
            or set(material) != fields
            or material["schema_version"]
            != "history-l2-adjudication-generation-v1"
        ):
            return 0
        plan_sha = history_audit_plan.runtime_plan_sha_from_material(plan)
        if (
            material["plan_sha"] != plan_sha
            or material["snapshot_id"] != plan["snapshot"]["snapshot_id"]
            or material["snapshot_hash"] != plan["snapshot"]["snapshot_hash"]
            or material["candidate_id"] != plan["candidate"]["candidate_id"]
            or material["candidate_hash"] != plan["candidate"]["candidate_hash"]
            or material["detail_provider_pool"]
            != plan["provider_pools_ordered"]["detail"]
            or material["reduce_provider_pool"]
            != plan["provider_pools_ordered"]["reduce"]
            or material["capacity_profile_sha"] != _l2_fact_sha(
                "history-l2-capacity-profile-identity-v1",
                plan["capacity_profile"],
            )
            or material["prompt_identity_sha"] != _l2_fact_sha(
                "history-l2-prompt-identity-v1",
                plan["capacity_profile"].get("prompt"),
            )
            or material["schema_identity_sha"] != _l2_fact_sha(
                "history-l2-schema-identity-v1",
                plan["capacity_profile"].get("schema"),
            )
            or material["budget_policy_sha"] != plan["budget_policy_sha"]
            or material["risk_policy_sha"] != plan["risk_policy_sha"]
            or material["settlement_policy_sha"]
            != plan["settlement_policy_sha"]
        ):
            return 0
        terminal_facts = material["map_terminal_facts"]
        if (
            type(terminal_facts) is not list
            or not terminal_facts
            or terminal_facts
            != sorted(terminal_facts, key=lambda value: value["task_hash"])
            or len({value["task_hash"] for value in terminal_facts})
            != len(terminal_facts)
        ):
            return 0
        for fact in terminal_facts:
            if (
                type(fact) is not dict
                or set(fact) != {"task_hash", "state", "fact_sha256"}
                or not _l2_sha_text(fact["task_hash"])
                or fact["state"] not in {"settled", "exhausted"}
                or not _l2_sha_text(fact["fact_sha256"])
            ):
                return 0
        if material["map_terminal_set_sha"] != _l2_fact_sha(
            "history-l2-map-terminal-set-v1", terminal_facts
        ):
            return 0
        cards = material["exceptional_cards"]
        if type(cards) is not list or cards != sorted(
            cards, key=lambda value: value["lineage_id"]
        ):
            return 0
        for card in cards:
            if (
                type(card) is not dict
                or set(card) != {
                    "lineage_id", "semantic_relation", "item_ids", "evidence"
                }
                or type(card["lineage_id"]) is not str
                or not card["lineage_id"]
                or card["semantic_relation"] not in {
                    "blocking_duplicate", "substantive_overlap", "uncertain"
                }
                or type(card["item_ids"]) is not list
                or not card["item_ids"]
                or card["item_ids"] != sorted(set(card["item_ids"]))
                or type(card["evidence"]) is not list
                or not card["evidence"]
            ):
                return 0
        if (
            material["exceptional_cards_sha"] != _l2_fact_sha(
                "history-l2-exceptional-cards-v1", cards
            )
            or generation_id != _l2_generation_identity_sha(material)
        ):
            return 0
        return 1
    except (
        ValueError, TypeError, KeyError,
        history_audit_plan.AuditPlanError,
        history_contract_v2.ContractV2Error,
    ):
        return 0


def _l2_derived_task_valid(
    plan_json, records_json, generation_json, authority_json, facts_json
):
    try:
        plan = history_audit_plan.validate_runtime_plan_material(
            _closed_json(plan_json)
        )
        records = history_audit_plan.runtime_snapshot_records(
            _closed_json(records_json)
        )
        generation = _closed_json(generation_json)
        authority = _closed_json(authority_json)
        facts = json.loads(facts_json)
        generation_id = _l2_generation_identity_sha(generation)
        if _l2_adjudication_generation_valid(
            plan_json, generation_json, generation_id
        ) != 1:
            return 0
        authority_fields = {
            "schema_version", "generation_id", "plan_sha", "stage",
            "task_hash", "input_id", "request_sha256", "assigned_item_ids",
            "source_task_hashes", "source_settlement_hashes",
        }
        if (
            type(authority) is not dict
            or set(authority) != authority_fields
            or authority["schema_version"]
            != "history-l2-derived-task-authority-v1"
            or authority["generation_id"] != generation_id
            or authority["plan_sha"] != generation["plan_sha"]
            or authority["stage"] not in {"detail", "reduce"}
            or not _l2_sha_text(authority["task_hash"])
            or type(authority["input_id"]) is not str
            or not authority["input_id"]
            or not _l2_sha_text(authority["request_sha256"])
            or type(authority["assigned_item_ids"]) is not list
            or not authority["assigned_item_ids"]
            or authority["assigned_item_ids"]
            != sorted(set(authority["assigned_item_ids"]))
            or type(authority["source_task_hashes"]) is not list
            or not authority["source_task_hashes"]
            or authority["source_task_hashes"]
            != sorted(set(authority["source_task_hashes"]))
            or type(authority["source_settlement_hashes"]) is not list
            or len(authority["source_settlement_hashes"])
            != len(authority["source_task_hashes"])
            or any(
                not _l2_sha_text(value)
                for value in authority["source_settlement_hashes"]
            )
        ):
            return 0
        request = _closed_json(facts["request_text"])
        assigned = _closed_json(facts["assigned_json"])
        frozen = _closed_json(facts["frozen_json"])
        pool = _closed_json(facts["pool_json"])
        stage = authority["stage"]
        if (
            facts["task_hash"] != authority["task_hash"]
            or facts["run_id"] != plan["run_id"]
            or facts["stage"] != stage
            or facts["candidate_id"] != plan["candidate"]["candidate_id"]
            or facts["input_id"] != authority["input_id"]
            or facts["plan_sha"] != authority["plan_sha"]
            or facts["snapshot_id"] != plan["snapshot"]["snapshot_id"]
            or facts["snapshot_hash"] != plan["snapshot"]["snapshot_hash"]
            or facts["request_sha"] != authority["request_sha256"]
            or hashlib.sha256(facts["request_text"].encode("utf-8")).hexdigest()
            != authority["request_sha256"]
            or facts["shard_input_sha"] != authority["request_sha256"]
            or assigned != authority["assigned_item_ids"]
            or pool != plan["provider_pools_ordered"][stage]
            or facts["parent_hash"] is not None
            or facts["split_depth"] != 0
            or facts["authority_sha"] != _l2_fact_sha(
                "history-l2-derived-task-authority-v1", authority
            )
            or authority["task_hash"] != history_contract_v2.logical_task_key(
                authority["plan_sha"], stage,
                plan["candidate"]["candidate_id"],
                authority["request_sha256"],
            )
        ):
            return 0
        common_request = {
            "schema_version", "generation_id", "plan_sha", "snapshot_id",
            "snapshot_hash", "candidate_id", "candidate_hash",
            "source_task_hashes", "source_settlement_hashes",
        }
        if any(
            request[field] != authority[field]
            for field in (
                "generation_id", "plan_sha",
                "source_task_hashes", "source_settlement_hashes",
            )
        ) or (
            request["snapshot_id"] != generation["snapshot_id"]
            or request["snapshot_hash"] != generation["snapshot_hash"]
            or request["candidate_id"] != generation["candidate_id"]
            or request["candidate_hash"] != generation["candidate_hash"]
        ):
            return 0
        record_by_id = {record["item_id"]: record for record in records}
        if stage == "detail":
            if set(request) != common_request | {
                "exceptional_card", "full_records"
            } or request["schema_version"] != "history-detail-request-v1":
                return 0
            card = request["exceptional_card"]
            generation_card = next(
                (
                    value for value in generation["exceptional_cards"]
                    if type(card) is dict
                    and value["lineage_id"] == card.get("lineage_id")
                ),
                None,
            )
            if (
                type(card) is not dict
                or generation_card is None
                or card.get("semantic_relation")
                != generation_card["semantic_relation"]
                or not isinstance(card.get("item_ids"), list)
                or not card["item_ids"]
                or not set(card["item_ids"]).issubset(
                    generation_card["item_ids"]
                )
                or card.get("evidence") != [
                    anchor for anchor in generation_card["evidence"]
                    if anchor["asset_id"] in card["item_ids"]
                ]
            ):
                return 0
            expected_records = [
                record_by_id[item_id] for item_id in card["item_ids"]
            ]
            terminal_pairs = {
                (item["task_hash"], item["fact_sha256"])
                for item in generation["map_terminal_facts"]
                if item["state"] == "settled"
            }
            return 1 if (
                authority["assigned_item_ids"] == card["item_ids"]
                and frozen == expected_records
                and request["full_records"] == expected_records
                and set(zip(
                    authority["source_task_hashes"],
                    authority["source_settlement_hashes"],
                )).issubset(terminal_pairs)
            ) else 0
        if set(request) != common_request | {"detail_cards"} or (
            request["schema_version"] != "history-reduce-request-v1"
            or type(request["detail_cards"]) is not list
            or not request["detail_cards"]
            or frozen != []
        ):
            return 0
        reduced_ids = sorted({
            item_id
            for card in request["detail_cards"]
            for item_id in card["item_ids"]
        })
        return 1 if reduced_ids == authority["assigned_item_ids"] else 0
    except (
        ValueError, TypeError, KeyError,
        history_audit_plan.AuditPlanError,
        history_contract_v2.ContractV2Error,
    ):
        return 0


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


def _l2_failure_claim_transfer_sha(
    task_hash, attempt_id, outcome, source_claim_fence, source_claim_token,
    target_claim_fence, target_claim_token, target_lease_until, created_at,
):
    try:
        return _l2_fact_sha(
            "history-l2-failure-claim-transfer-v1",
            {
                "task_hash": task_hash,
                "attempt_id": attempt_id,
                "outcome": outcome,
                "source_claim": {
                    "fence": source_claim_fence,
                    "token": source_claim_token,
                },
                "target_claim": {
                    "fence": target_claim_fence,
                    "token": target_claim_token,
                    "lease_until": target_lease_until,
                },
                "created_at": created_at,
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
        expected_failure_claim_transfer=None,
    )


@contextlib.contextmanager
def _l2_failure_claim_transfer_guard(conn, transfer):
    guard = _L2_TERMINAL_TRANSITION_GUARDS.get(id(conn))
    if (
        guard is None
        or guard["active"]
        or guard["expected_failure_claim_transfer"] is not None
        or not conn.in_transaction
    ):
        raise AuditMigrationError("failure claim transfer guard is unavailable")
    guard["expected_failure_claim_transfer"] = transfer
    try:
        yield
    finally:
        guard["expected_failure_claim_transfer"] = None


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


def _clear_l2_adjudication_guard(guard):
    guard.update(
        active=False,
        expected_generation=None,
        expected_tasks=frozenset(),
        expected_bindings=frozenset(),
        expected_inputs=frozenset(),
        expected_authorities=frozenset(),
    )


@contextlib.contextmanager
def l2_adjudication_materialization_guard(
    conn, *, generation=None, tasks=(), bindings=(), inputs=(), authorities=()
):
    """Authorize one exact host-derived adjudication materialization batch."""
    guard = _L2_ADJUDICATION_GUARDS.get(id(conn))
    if guard is None or guard["active"] or not conn.in_transaction:
        raise AuditMigrationError("L2 adjudication materialization guard is unavailable")
    guard.update(
        active=True,
        expected_generation=generation,
        expected_tasks=frozenset(tasks),
        expected_bindings=frozenset(bindings),
        expected_inputs=frozenset(inputs),
        expected_authorities=frozenset(authorities),
    )
    try:
        yield
    finally:
        _clear_l2_adjudication_guard(guard)


def _clear_metadata_guard(guard):
    guard.update(
        active=False,
        metadata_operation=None,
        metadata_now=None,
        metadata_outbox_id=None,
        metadata_claim_token=None,
        metadata_claim_fence=None,
    )


def _clear_attempt_terminal_guard(guard):
    guard.update(completion=None, budget_settlement=None)


def _clear_router_source_guard(guard):
    guard.update(
        round=None,
        budget=None,
        sources=frozenset(),
        source_set=None,
        phase_fact=None,
        binding=None,
        legacy=None,
        host_observation=None,
        host_round=None,
        host_sources=frozenset(),
        host_preplan=None,
        host_l1_fact=None,
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


def _receipt_material_sha(receipt):
    return history_contract_v2.framed_sha256(
        "history-receipt-material-v2",
        history_contract_v2.canonical_bytes(receipt),
    )


def _receipt_row_valid(*values):
    try:
        _semantic_receipt_from_sql(values)
        return 1
    except (ValueError, TypeError, history_contract_v2.ContractV2Error):
        return 0


def _clear_receipt_issuance_guard(guard):
    guard.update(
        expected_issuance=None,
        expected_receipt=None,
        receipt_id=None,
        receipt_material_sha256=None,
        issuance_id=None,
    )


def _receipt_insert_allowed(guard, *values):
    try:
        receipt = _semantic_receipt_from_sql(values)
        return 1 if (
            guard["expected_receipt"] == tuple(values)
            and guard["receipt_id"] == receipt["minimum_receipt_sha"]
            and guard["receipt_material_sha256"] == _receipt_material_sha(receipt)
            and guard["issuance_id"] is not None
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


def _migration_sql_for_state(conn, migration):
    if (
        migration.component == "core-authority-repair"
        and migration.version == 1
    ):
        required = {
            "audit_snapshot_batch_sets",
            "audit_batch_pair_set_bindings",
            "audit_task_settlements_v2",
        }
        present = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        required_components = {
            "l1-strict-pair-completion": _STRICT_PAIR_COMPLETION_SQL,
            "l1-batch-direction-authority": _L1_BATCH_DIRECTION_AUTHORITY_SQL,
            "batch-staging-authority": _STAGING_AUTHORITY_SQL,
            "batch-direction-gate-authority": _DIRECTION_GATE_AUTHORITY_SQL,
            "logical-task-transition-integrity":
                _LOGICAL_TASK_TRANSITION_INTEGRITY_SQL,
        }
        applied_components = {
            row[0]: row[1] for row in conn.execute(
                "SELECT component,migration_sha256 FROM audit_schema_migrations"
            )
        }
        if (
            not required.issubset(present)
            or any(
                applied_components.get(component)
                != hashlib.sha256(source.encode("utf-8")).hexdigest()
                for component, source in required_components.items()
            )
        ):
            return None
    if (
        migration.component == "pair-result-authority"
        and migration.version == 1
        and (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='audit_batch_pair_receipt_authority_v3'"
            ).fetchone() is None
            or conn.execute(
                "SELECT 1 FROM audit_schema_migrations "
                "WHERE component='core-authority-repair' AND version=1 "
                "AND migration_sha256=?",
                (hashlib.sha256(
                    _CORE_AUTHORITY_REPAIR_SQL.encode("utf-8")
                ).hexdigest(),),
            ).fetchone() is None
        )
    ):
        return None
    if (
        migration.component == "authority-input-hardening"
        and migration.version == 1
        and (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='audit_task_settlement_authority_v3'"
            ).fetchone() is None
            or conn.execute(
                "SELECT 1 FROM audit_schema_migrations "
                "WHERE component='core-authority-repair' AND version=1 "
                "AND migration_sha256=?",
                (hashlib.sha256(
                    _CORE_AUTHORITY_REPAIR_SQL.encode("utf-8")
                ).hexdigest(),),
            ).fetchone() is None
            or conn.execute(
                "SELECT 1 FROM audit_schema_migrations "
                "WHERE component='pair-result-authority' AND version=1 "
                "AND migration_sha256=?",
                (hashlib.sha256(
                    _PAIR_RESULT_AUTHORITY_SQL.encode("utf-8")
                ).hexdigest(),),
            ).fetchone() is None
        )
    ):
        return None
    if (
        migration.component == "authority-input-hardening"
        and migration.version == 1
        and conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='candidates'"
        ).fetchone() is None
    ):
        begin = "-- activation-candidate-backfill-begin"
        end = "-- activation-candidate-backfill-end"
        prefix, remainder = migration.sql.split(begin, 1)
        _, suffix = remainder.split(end, 1)
        return prefix + suffix
    if (
        migration.component != "l2-plans-per-run"
        or migration.version != 1
    ):
        return migration.sql
    begin = _L2_PLANS_BUDGET_GUARD_BEGIN
    end = _L2_PLANS_BUDGET_GUARD_END
    if migration.sql.count(begin) != 1 or migration.sql.count(end) != 1:
        raise AuditMigrationError(
            "l2 plans optional candidate budget guard block is malformed"
        )
    budget_ledger = conn.execute(
        "SELECT migration_sha256 FROM audit_schema_migrations "
        "WHERE component='candidate-budget-authority' AND version=1"
    ).fetchone()
    budget_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='audit_candidate_budget_receipts_v2'"
    ).fetchone() is not None
    budget_guard = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' "
        "AND name='audit_l2_plans_v2_candidate_budget_guard'"
    ).fetchone() is not None
    if budget_ledger is not None:
        expected_sha = hashlib.sha256(
            _CANDIDATE_BUDGET_AUTHORITY_SQL.encode("utf-8")
        ).hexdigest()
        if budget_ledger[0] != expected_sha:
            raise AuditMigrationError(
                "migration SHA drift: candidate-budget-authority v1"
            )
        if not budget_table or not budget_guard:
            raise AuditMigrationError(
                "candidate budget authority schema is inconsistent"
            )
        return migration.sql
    if budget_table or budget_guard:
        raise AuditMigrationError(
            "candidate budget authority schema is inconsistent"
        )
    prefix, remainder = migration.sql.split(begin, 1)
    _, suffix = remainder.split(end, 1)
    return prefix + suffix


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
                repair_table = {
                    "core-authority-repair":
                        "audit_batch_pair_receipt_authority_v3",
                    "pair-result-authority":
                        "audit_batch_pair_result_manifests_v4",
                    "authority-input-hardening":
                        "audit_attempt_completion_quarantine_v4",
                }.get(migration.component)
                if (
                    repair_table is not None
                    and conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (repair_table,),
                    ).fetchone() is None
                ):
                    migration_sql = _migration_sql_for_state(conn, migration)
                    if migration_sql:
                        _execute_sql_script(conn, migration_sql)
                conn.execute("COMMIT")
                return
        migration_sql = _migration_sql_for_state(conn, migration)
        if migration_sql is None:
            conn.execute("COMMIT")
            return
        _execute_sql_script(conn, migration_sql)
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
            if _migration_sql_for_state(conn, migration) is None:
                continue
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
    has_derived_task_authority = conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type='view' AND name='audit_l2_valid_runtime_task_authority_v2'
        """
    ).fetchone() is not None
    has_metadata_direction_gate_provenance = conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type='view'
          AND name='audit_valid_metadata_direction_provenance_v2'
        """
    ).fetchone() is not None
    has_verified_usage_authority = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='audit_verified_usage_authorities_v2'"
    ).fetchone() is not None
    receipt_authority_view = None
    for candidate in (
        "audit_valid_batch_pair_receipt_authority_v4",
        "audit_valid_batch_pair_receipt_authority_v3",
    ):
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='view' AND name=?",
            (candidate,),
        ).fetchone() is not None:
            receipt_authority_view = candidate
            break
    verified_usage_superseded_probes = {
        "audit_runtime_usage_authority_upgrade_probe",
        "audit_attempt_terminal_authority_upgrade_probe",
    }
    probe_index = 0
    for migration in MIGRATIONS:
        for match in pattern.finditer(migration.sql):
            if (
                has_metadata_direction_gate_provenance
                and match.group(2) == "audit_metadata_direction_probe"
            ):
                continue
            if (
                has_verified_usage_authority
                and match.group(2) in verified_usage_superseded_probes
            ):
                continue
            probe_index += 1
            savepoint = f"audit_migration_probe_{probe_index}"
            conn.execute("SAVEPOINT " + savepoint)
            try:
                probe_sql = match.group(1)
                if receipt_authority_view is not None:
                    probe_sql = probe_sql.replace(
                        "audit_batch_pair_receipts", receipt_authority_view,
                    )
                if has_derived_task_authority:
                    probe_sql = probe_sql.replace(
                        "audit_l2_valid_task_authority_v2",
                        "audit_l2_valid_runtime_task_authority_v2",
                    )
                try:
                    _execute_sql_script(conn, probe_sql)
                except Exception as exc:
                    raise AuditMigrationError(
                        "migration invariant probe failed: "
                        f"{migration.component} v{migration.version}"
                    ) from exc
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
    adjudication_guard = {}
    _clear_l2_adjudication_guard(adjudication_guard)
    _L2_ADJUDICATION_GUARDS[id(conn)] = adjudication_guard
    release_guard = {}
    _clear_semantic_release_guard(release_guard)
    _SEMANTIC_RELEASE_GUARDS[id(conn)] = release_guard
    receipt_guard = {}
    _clear_receipt_issuance_guard(receipt_guard)
    _RECEIPT_ISSUANCE_GUARDS[id(conn)] = receipt_guard
    direction_verdict_guard = {}
    _clear_direction_verdict_guard(direction_verdict_guard)
    _DIRECTION_VERDICT_GUARDS[id(conn)] = direction_verdict_guard
    candidate_budget_guard = {"expected": None}
    _CANDIDATE_BUDGET_GUARDS[id(conn)] = candidate_budget_guard
    staging_authority_guard = {"expected": None}
    _STAGING_AUTHORITY_GUARDS[id(conn)] = staging_authority_guard
    semantic_evaluation_guard = {
        "expected": None,
        "issuance": None,
        "evidence": None,
    }
    _SEMANTIC_EVALUATION_GUARDS[id(conn)] = semantic_evaluation_guard
    cost_guard = {
        "launch": None, "settlement": None, "cohort": None,
        "route": None, "route_observation": None, "dispatch": None,
        "dispatch_issuance": None,
    }
    _COST_FACT_GUARDS[id(conn)] = cost_guard
    terminal_guard = {}
    _clear_attempt_terminal_guard(terminal_guard)
    _ATTEMPT_TERMINAL_GUARDS[id(conn)] = terminal_guard
    ledger_guard = {"expected": None}
    _MIGRATION_LEDGER_GUARDS[id(conn)] = ledger_guard
    router_source_guard = {}
    _clear_router_source_guard(router_source_guard)
    _ROUTER_SOURCE_GUARDS[id(conn)] = router_source_guard
    verified_usage_guard = {"expected": None}
    _VERIFIED_USAGE_AUTHORITY_GUARDS[id(conn)] = verified_usage_guard
    l1_attempt_guard = {"expected": None, "usage": None}
    _L1_ATTEMPT_FACT_GUARDS[id(conn)] = l1_attempt_guard
    pair_result_guard = {"expected": None}
    _PAIR_RESULT_AUTHORITY_GUARDS[id(conn)] = pair_result_guard
    conn.create_function("audit_pair_plan_sha", 3, _pair_plan_sha)
    conn.create_function(
        "audit_pair_result_manifest_valid", 10,
        _pair_result_manifest_valid,
    )
    conn.create_function(
        "audit_pair_result_manifest_insert_allowed", 10,
        lambda *values: 1 if pair_result_guard["expected"] == tuple(values) else 0,
    )
    conn.create_function(
        "audit_pair_receipt_authority_sha", 9,
        _pair_receipt_authority_sha,
    )
    conn.create_function(
        "audit_activation_receipt_core_valid", 12,
        _activation_receipt_core_valid,
    )
    conn.create_function(
        "audit_activation_receipt_authority_sha", 6,
        _activation_receipt_authority_sha,
    )
    conn.create_function(
        "audit_task_settlement_material_valid", 6,
        _task_settlement_material_valid,
    )
    conn.create_function(
        "audit_normalized_result_json_valid", 1,
        _normalized_result_json_valid,
    )
    conn.create_function(
        "audit_task_settlement_authority_sha", 9,
        _task_settlement_authority_sha,
    )
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
        "audit_attempt_completion_insert_allowed", 6,
        lambda *values: 1 if (
            terminal_guard["completion"] == tuple(values)
        ) else 0,
    )
    conn.create_function(
        "audit_runtime_budget_settlement_insert_allowed", 4,
        lambda *values: 1 if (
            terminal_guard["budget_settlement"] == tuple(values)
        ) else 0,
    )
    conn.create_function(
        "audit_completion_usage_valid", 1, _completion_usage_valid
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
        "audit_candidate_route_observation_insert_allowed", 7,
        lambda *values: (
            1 if cost_guard["route_observation"] == tuple(values) else 0
        ),
    )
    conn.create_function(
        "audit_candidate_dispatch_insert_allowed", 6,
        lambda *values: 1 if cost_guard["dispatch"] == tuple(values) else 0,
    )
    conn.create_function(
        "audit_candidate_budget_receipt_insert_allowed", 11,
        lambda *values: 1 if (
            candidate_budget_guard["expected"] == tuple(values)
        ) else 0,
    )
    conn.create_function(
        "audit_batch_staging_authority_sha", 8,
        batch_staging_authority_sha256,
    )
    conn.create_function(
        "audit_batch_staging_authority_valid", 9,
        _batch_staging_authority_row_valid,
    )
    conn.create_function(
        "audit_batch_staging_authority_insert_allowed", 9,
        lambda *values: 1 if (
            staging_authority_guard["expected"] == tuple(values)
        ) else 0,
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
        "audit_l2_budget_settlement_valid", 3, _l2_budget_settlement_valid
    )
    conn.create_function(
        "audit_l2_attempt_capability_valid", 3, _l2_attempt_capability_valid
    )
    conn.create_function(
        "audit_l2_attempt_capability_valid", 4, _l2_attempt_capability_valid
    )
    conn.create_function("audit_l2_root_task_valid", 6, _l2_root_task_valid)
    conn.create_function(
        "audit_l2_binding_authority_valid", 3, _l2_binding_authority_valid
    )
    conn.create_function(
        "audit_l2_input_authority_valid", 2, _l2_input_authority_valid
    )
    conn.create_function(
        "audit_l2_adjudication_generation_valid", 3,
        _l2_adjudication_generation_valid,
    )
    conn.create_function(
        "audit_l2_derived_task_valid", 5, _l2_derived_task_valid
    )
    conn.create_function(
        "audit_l2_adjudication_generation_insert_allowed", 4,
        lambda *values: 1 if (
            adjudication_guard["active"]
            and adjudication_guard["expected_generation"] == tuple(values)
        ) else 0,
    )
    conn.create_function(
        "audit_l2_adjudication_task_insert_allowed", 5,
        lambda *values: 1 if (
            adjudication_guard["active"]
            and tuple(values) in adjudication_guard["expected_tasks"]
        ) else 0,
    )
    conn.create_function(
        "audit_l2_adjudication_binding_insert_allowed", 11,
        lambda *values: 1 if (
            adjudication_guard["active"]
            and tuple(values) in adjudication_guard["expected_bindings"]
        ) else 0,
    )
    conn.create_function(
        "audit_l2_adjudication_input_insert_allowed", 6,
        lambda *values: 1 if (
            adjudication_guard["active"]
            and tuple(values) in adjudication_guard["expected_inputs"]
        ) else 0,
    )
    conn.create_function(
        "audit_l2_derived_authority_insert_allowed", 7,
        lambda *values: 1 if (
            adjudication_guard["active"]
            and tuple(values) in adjudication_guard["expected_authorities"]
        ) else 0,
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
        "audit_l2_failure_claim_transfer_sha", 9,
        _l2_failure_claim_transfer_sha,
    )
    conn.create_function(
        "audit_l2_failure_claim_transfer_insert_allowed", 10,
        lambda *values: 1 if (
            split_guard["expected_failure_claim_transfer"] == tuple(values)
        ) else 0,
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
        "audit_semantic_production_evidence_insert_allowed", 14,
        lambda *values: 1 if (
            semantic_evaluation_guard["evidence"] == tuple(values)
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
    conn.create_function(
        "audit_receipt_row_valid", len(_RELEASE_RECEIPT_FIELDS),
        _receipt_row_valid,
    )
    conn.create_function(
        "audit_receipt_issuance_insert_allowed", 7,
        lambda *values: 1 if (
            receipt_guard["expected_issuance"] == tuple(values)
        ) else 0,
    )
    conn.create_function(
        "audit_receipt_insert_allowed", len(_RELEASE_RECEIPT_FIELDS),
        lambda *values: _receipt_insert_allowed(receipt_guard, *values),
    )
    conn.create_function(
        "audit_receipt_material_sha", 0,
        lambda: receipt_guard["receipt_material_sha256"],
    )
    conn.create_function(
        "audit_receipt_issuance_id", 0,
        lambda: receipt_guard["issuance_id"],
    )
    conn.create_function(
        "audit_direction_verdict_insert_allowed", 14,
        lambda *values: 1 if (
            direction_verdict_guard["active"]
            and tuple(values) in direction_verdict_guard["expected_verdicts"]
        ) else 0,
    )
    conn.create_function(
        "audit_direction_verdict_valid", 13,
        _direction_verdict_row_valid,
    )
    conn.create_function(
        "audit_direction_gate_binding_insert_allowed", 5,
        lambda *values: 1 if (
            direction_verdict_guard["active"]
            and tuple(values) in direction_verdict_guard["expected_bindings"]
        ) else 0,
    )
    conn.create_function(
        "audit_direction_gate_insert_allowed", 17,
        lambda *values: 1 if (
            direction_verdict_guard["active"]
            and direction_verdict_guard["expected_gate"] == tuple(values)
        ) else 0,
    )
    conn.create_function(
        "audit_direction_gate_valid", 17,
        _direction_gate_row_valid,
    )
    conn.create_function(
        "audit_router_round_insert_allowed", 14,
        lambda *values: 1 if (
            router_source_guard["round"] == tuple(values)
        ) else 0,
    )
    conn.create_function(
        "audit_router_round_valid", 13, _router_round_row_valid
    )
    conn.create_function(
        "audit_router_budget_insert_allowed", 13,
        lambda *values: 1 if (
            router_source_guard["budget"] == tuple(values)
        ) else 0,
    )
    conn.create_function(
        "audit_router_budget_valid", 14, _router_budget_row_valid
    )
    conn.create_function(
        "audit_router_source_insert_allowed", 5,
        lambda *values: 1 if (
            tuple(values) in router_source_guard["sources"]
        ) else 0,
    )
    conn.create_function(
        "audit_router_domain_source_valid", 5,
        _router_domain_source_row_valid,
    )
    conn.create_function(
        "audit_router_source_set_insert_allowed", 7,
        lambda *values: 1 if (
            router_source_guard["source_set"] == tuple(values)
        ) else 0,
    )
    conn.create_function(
        "audit_router_phase_fact_insert_allowed", 15,
        lambda *values: 1 if (
            router_source_guard["phase_fact"] == tuple(values)
        ) else 0,
    )
    conn.create_function(
        "audit_router_binding_insert_allowed", 6,
        lambda *values: 1 if (
            router_source_guard["binding"] == tuple(values)
        ) else 0,
    )
    conn.create_function(
        "audit_router_legacy_insert_allowed", 9,
        lambda *values: 1 if (
            router_source_guard["legacy"] == tuple(values)
        ) else 0,
    )
    conn.create_function(
        "audit_router_host_observation_insert_allowed", 10,
        lambda *values: 1 if (
            router_source_guard["host_observation"] == tuple(values)
        ) else 0,
    )
    conn.create_function(
        "audit_router_host_observation_valid", 10,
        _router_host_observation_row_valid,
    )
    conn.create_function(
        "audit_router_host_round_insert_allowed", 5,
        lambda *values: 1 if (
            router_source_guard["host_round"] == tuple(values)
        ) else 0,
    )
    conn.create_function(
        "audit_router_host_round_valid", 5,
        _router_host_round_authority_row_valid,
    )
    conn.create_function(
        "audit_router_host_source_insert_allowed", 8,
        lambda *values: 1 if (
            tuple(values) in router_source_guard["host_sources"]
        ) else 0,
    )
    conn.create_function(
        "audit_router_host_source_valid", 8,
        _router_host_source_authority_row_valid,
    )
    conn.create_function(
        "audit_router_host_preplan_insert_allowed", 9,
        lambda *values: 1 if (
            router_source_guard["host_preplan"] == tuple(values)
        ) else 0,
    )
    conn.create_function(
        "audit_router_host_preplan_valid", 9,
        _router_host_preplan_row_valid,
    )
    conn.create_function(
        "audit_router_host_l1_fact_insert_allowed", 19,
        lambda *values: 1 if (
            router_source_guard["host_l1_fact"] == tuple(values)
        ) else 0,
    )
    conn.create_function(
        "audit_router_host_l1_fact_valid", 19,
        _router_host_l1_comparator_row_valid,
    )
    conn.create_function(
        "audit_verified_usage_authority_insert_allowed", 16,
        lambda *values: 1 if (
            verified_usage_guard["expected"] == tuple(values)
        ) else 0,
    )
    conn.create_function(
        "audit_verified_usage_authority_valid", 16,
        _verified_usage_authority_row_valid,
    )
    conn.create_function(
        "audit_l1_verified_usage_authority_insert_allowed", 21,
        lambda *values: 1 if (
            l1_attempt_guard["usage"] == tuple(values)
        ) else 0,
    )
    conn.create_function(
        "audit_l1_verified_usage_authority_valid", 21,
        _l1_verified_usage_authority_row_valid,
    )
    conn.create_function(
        "audit_l1_attempt_id_valid", 12,
        _l1_attempt_id_row_valid,
    )
    conn.create_function(
        "audit_l1_attempt_fact_insert_allowed", 25,
        lambda *values: 1 if (
            l1_attempt_guard["expected"] == tuple(values)
        ) else 0,
    )
    conn.create_function(
        "audit_l1_attempt_fact_valid", 25,
        _l1_attempt_fact_row_valid,
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


def issue_batch_pair_result_authority(
    conn, *, run_id, batch_id, pair_plan_sha, pair_result_sha, results,
):
    """Persist canonical pair evidence after its frozen set is fully durable."""
    if not conn.in_transaction:
        raise AuditMigrationError("pair result authority requires a transaction")
    schema_present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='audit_batch_pair_result_manifests_v4'"
    ).fetchone() is not None
    if not schema_present:
        return None
    results_json = history_contract_v2.canonical_bytes(results).decode("utf-8")
    normalized = _normalized_pair_result_evidence(results_json)
    if normalized is _INVALID_AUTHORITY_JSON:
        raise ValueError("pair result evidence is invalid")
    if _pair_result_sha(pair_plan_sha, results_json) != pair_result_sha:
        raise ValueError("pair result evidence does not match its commitment")
    row = conn.execute(
        """
        SELECT receipt.snapshot_id,receipt.completed_at,receipt.pair_count,
               binding.current_batch_ids_hash,binding.member_count,
               batch_set.member_ids_json
        FROM audit_batch_pair_receipts receipt
        JOIN audit_batch_pair_set_bindings binding
          ON binding.run_id=receipt.run_id AND binding.batch_id=receipt.batch_id
         AND binding.snapshot_id=receipt.snapshot_id
         AND binding.pair_plan_sha=receipt.pair_plan_sha
         AND binding.pair_result_sha=receipt.pair_result_sha
        JOIN audit_snapshot_batch_sets batch_set
          ON batch_set.snapshot_id=binding.snapshot_id
         AND batch_set.current_batch_ids_hash=binding.current_batch_ids_hash
         AND batch_set.member_count=binding.member_count
         AND batch_set.run_id=receipt.run_id
         AND batch_set.batch_id=receipt.batch_id
        WHERE receipt.run_id=? AND receipt.batch_id=?
          AND receipt.pair_plan_sha=? AND receipt.pair_result_sha=?
        """,
        (run_id, batch_id, pair_plan_sha, pair_result_sha),
    ).fetchone()
    if row is None:
        raise AuditMigrationError("pair result lacks frozen set binding")
    member_ids = json.loads(row["member_ids_json"])
    expected_pairs = {
        (left, right)
        for index, left in enumerate(member_ids)
        for right in member_ids[index + 1:]
    }
    result_pairs = {
        (item["left_staging_candidate_id"], item["right_staging_candidate_id"])
        for item in normalized
    }
    stored_pairs = {
        (item[0], item[1])
        for item in conn.execute(
            """
            SELECT left_staging_candidate_id,right_staging_candidate_id
            FROM audit_batch_pairs
            WHERE run_id=? AND batch_id=? AND pair_plan_sha=?
              AND pair_result_sha=?
            """,
            (run_id, batch_id, pair_plan_sha, pair_result_sha),
        )
    }
    if (
        row["pair_count"] != len(expected_pairs)
        or row["member_count"] != len(member_ids)
        or result_pairs != expected_pairs
        or stored_pairs != expected_pairs
        or len(normalized) != len(expected_pairs)
    ):
        raise AuditMigrationError("pair result evidence does not cover frozen set")
    authority_sha = _pair_result_manifest_authority_sha(
        run_id, batch_id, row["snapshot_id"], pair_plan_sha, pair_result_sha,
        row["current_batch_ids_hash"], row["member_count"], results_json,
        row["completed_at"],
    )
    values = (
        run_id, batch_id, row["snapshot_id"], pair_plan_sha, pair_result_sha,
        row["current_batch_ids_hash"], row["member_count"], results_json,
        authority_sha, row["completed_at"],
    )
    existing = conn.execute(
        "SELECT * FROM audit_batch_pair_result_manifests_v4 "
        "WHERE run_id=? AND batch_id=?", (run_id, batch_id),
    ).fetchone()
    if existing is not None:
        if tuple(existing) != values:
            raise AuditMigrationError("pair result authority replay conflicts")
        return authority_sha
    guard = _PAIR_RESULT_AUTHORITY_GUARDS.get(id(conn))
    if guard is None or guard["expected"] is not None:
        raise AuditMigrationError("pair result authority guard is unavailable")
    guard["expected"] = values
    try:
        conn.execute(
            "INSERT INTO audit_batch_pair_result_manifests_v4 "
            "VALUES(?,?,?,?,?,?,?,?,?,?)", values,
        )
    finally:
        guard["expected"] = None
    if conn.execute(
        "SELECT 1 FROM audit_valid_batch_pair_receipt_authority_v4 "
        "WHERE run_id=? AND batch_id=?", (run_id, batch_id),
    ).fetchone() is None:
        raise AuditMigrationError("pair result authority failed durable validation")
    return authority_sha


def insert_authorized_batch_staging(
    conn, *, staging_candidate_id, run_id, batch_id, candidate_hash,
    raw_artifact_sha, source_order, created_at=None,
):
    """Insert or exactly replay one host-issued staging row in an active tx."""
    if not conn.in_transaction:
        raise AuditMigrationError("batch staging issuance requires a transaction")
    authority_schema_present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='audit_batch_staging_authorities_v2'"
    ).fetchone() is not None
    stored = conn.execute(
        "SELECT * FROM audit_batch_staging WHERE staging_candidate_id=?",
        (staging_candidate_id,),
    ).fetchone()
    if stored is not None:
        identity = (
            run_id, batch_id, candidate_hash, raw_artifact_sha, source_order,
        )
        if tuple(stored[name] for name in (
            "run_id", "batch_id", "candidate_hash", "raw_artifact_sha",
            "source_order",
        )) != identity:
            raise AuditMigrationError("batch staging identity conflicts")
        if not authority_schema_present:
            return None
        authority = conn.execute(
            "SELECT * FROM audit_batch_staging_authorities_v2 "
            "WHERE staging_candidate_id=?",
            (staging_candidate_id,),
        ).fetchone()
        if authority is None or tuple(authority[name] for name in (
            "run_id", "batch_id", "candidate_hash", "raw_artifact_sha",
            "source_order", "issued_at",
        )) != (*identity, stored["created_at"]) or (
            authority["authority_sha256"] != batch_staging_authority_sha256(
                staging_candidate_id, *identity,
                authority["authority_kind"], stored["created_at"],
            )
        ):
            raise AuditMigrationError("batch staging authority conflicts")
        return authority["authority_sha256"]
    if not authority_schema_present:
        created_at = created_at or _utc_now()
        conn.execute(
            """
            INSERT INTO audit_batch_staging(
              staging_candidate_id,run_id,batch_id,candidate_hash,
              raw_artifact_sha,source_order,created_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                staging_candidate_id, run_id, batch_id, candidate_hash,
                raw_artifact_sha, source_order, created_at,
            ),
        )
        return None
    orphan = conn.execute(
        "SELECT 1 FROM audit_batch_staging_authorities_v2 "
        "WHERE staging_candidate_id=?",
        (staging_candidate_id,),
    ).fetchone()
    if orphan is not None:
        raise AuditMigrationError("batch staging authority is orphaned")
    created_at = created_at or _utc_now()
    authority_sha = batch_staging_authority_sha256(
        staging_candidate_id, run_id, batch_id, candidate_hash,
        raw_artifact_sha, source_order, "host_issued", created_at,
    )
    authority_values = (
        authority_sha, staging_candidate_id, run_id, batch_id,
        candidate_hash, raw_artifact_sha, source_order, "host_issued",
        created_at,
    )
    guard = _STAGING_AUTHORITY_GUARDS.get(id(conn))
    if guard is None or guard["expected"] is not None:
        raise AuditMigrationError("batch staging authority guard is unavailable")
    guard["expected"] = authority_values
    try:
        conn.execute(
            "INSERT INTO audit_batch_staging_authorities_v2 "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            authority_values,
        )
    finally:
        guard["expected"] = None
    conn.execute(
        """
        INSERT INTO audit_batch_staging(
          staging_candidate_id, run_id, batch_id, candidate_hash,
          raw_artifact_sha, source_order, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            staging_candidate_id, run_id, batch_id, candidate_hash,
            raw_artifact_sha, source_order, created_at,
        ),
    )
    return authority_sha


def insert_direction_verdict(conn, material, *, checked_at=None):
    """Reject caller-shaped v2 verdicts; only a full batch gate may issue them."""
    del conn, material, checked_at
    raise ValueError("direction_gate_authority_required")


def _direction_gate_result(conn, gate):
    values = tuple(gate[name] for name in (
        "gate_sha256", "run_id", "batch_id", "snapshot_id",
        "current_batch_ids_hash", "direction_id", "contract_sha",
        "validator_version", "artifact_sha", "parser_revision",
        "raw_selector_artifact_sha256", "member_count",
        "candidate_mapping_json", "verdict_set_json", "verdict_set_sha256",
        "verdict_tsv", "issued_at",
    ))
    if _direction_gate_row_valid(*values) != 1:
        raise AuditMigrationError("direction gate canonical identity drifted")
    mapping = _parse_canonical_json_text(
        gate["candidate_mapping_json"], "direction gate candidate mapping"
    )
    verdict_set = _parse_canonical_json_text(
        gate["verdict_set_json"], "direction gate verdict set"
    )
    bindings = conn.execute(
        """
        SELECT binding.*,verdict.*
        FROM audit_batch_direction_gate_bindings_v2 binding
        JOIN audit_batch_direction_verdicts_v2 verdict
          ON verdict.verdict_sha256=binding.verdict_sha256
        WHERE binding.gate_sha256=?
        ORDER BY binding.source_order
        """,
        (gate["gate_sha256"],),
    ).fetchall()
    if len(bindings) != gate["member_count"]:
        raise AuditMigrationError("direction gate binding coverage drifted")
    verdicts = []
    for index, (mapping_item, set_item, row) in enumerate(
        zip(mapping, verdict_set, bindings)
    ):
        expected_binding = (
            gate["gate_sha256"], mapping_item["selector_id"], index,
            mapping_item["staging_candidate_id"], set_item["verdict_sha256"],
        )
        if tuple(row[name] for name in (
            "gate_sha256", "selector_id", "source_order",
            "staging_candidate_id", "verdict_sha256",
        )) != expected_binding:
            raise AuditMigrationError("direction gate binding identity drifted")
        try:
            evidence = history_contract_v2.parse_json_bytes(
                row["evidence_json"].encode("utf-8")
            )
        except history_contract_v2.ContractV2Error as exc:
            raise AuditMigrationError("direction verdict evidence drifted") from exc
        if (
            evidence != set_item["direction_evidence"]
            or row["direction_fit"] != set_item["direction_fit"]
            or row["evidence_sha256"] != set_item["evidence_sha256"]
        ):
            raise AuditMigrationError("direction gate verdict binding drifted")
        verdicts.append({
            "schema_version": "history-direction-verdict-v2",
            "run_id": gate["run_id"],
            "batch_id": gate["batch_id"],
            "snapshot_id": gate["snapshot_id"],
            "current_batch_ids_hash": gate["current_batch_ids_hash"],
            "direction_id": gate["direction_id"],
            "contract_sha": gate["contract_sha"],
            "validator_version": gate["validator_version"],
            "artifact_sha": gate["artifact_sha"],
            "staging_candidate_id": row["staging_candidate_id"],
            "direction_fit": row["direction_fit"],
            "direction_evidence": evidence,
            "evidence_sha256": row["evidence_sha256"],
            "verdict_sha256": row["verdict_sha256"],
            "gate_sha256": gate["gate_sha256"],
            "selector_id": row["selector_id"],
            "source_order": row["source_order"],
        })
    return {
        "schema_version": "history-batch-direction-gate-v2",
        "gate_sha256": gate["gate_sha256"],
        "run_id": gate["run_id"],
        "batch_id": gate["batch_id"],
        "snapshot_id": gate["snapshot_id"],
        "current_batch_ids_hash": gate["current_batch_ids_hash"],
        "direction_id": gate["direction_id"],
        "contract_sha": gate["contract_sha"],
        "validator_version": gate["validator_version"],
        "artifact_sha": gate["artifact_sha"],
        "parser_revision": gate["parser_revision"],
        "raw_selector_artifact_sha256": gate["raw_selector_artifact_sha256"],
        "member_count": gate["member_count"],
        "candidate_mapping": mapping,
        "verdict_set": verdict_set,
        "verdict_set_sha256": gate["verdict_set_sha256"],
        "issued_at": gate["issued_at"],
        "verdicts": verdicts,
    }


def read_batch_direction_gate(conn, *, run_id, batch_id):
    gate = conn.execute(
        "SELECT * FROM audit_batch_direction_gates_v2 WHERE run_id=? AND batch_id=?",
        (run_id, batch_id),
    ).fetchone()
    if gate is None:
        return None
    return _direction_gate_result(conn, gate)


def insert_batch_direction_gate(
    conn, *, run_id, batch_id, snapshot_id, current_batch_ids_hash,
    direction_id, contract_sha, validator_version, artifact_sha, verdict_tsv,
    issued_at=None,
):
    """Parse and atomically issue one full-batch direction gate."""
    if conn.in_transaction:
        raise AuditMigrationError("direction gate issuance requires an idle connection")
    if (
        not isinstance(verdict_tsv, bytes)
        or not 0 < len(verdict_tsv) <= MAX_DIRECTION_VERDICT_BYTES
    ):
        raise ValueError("direction verdict TSV is empty or exceeds the byte limit")
    batch_set = conn.execute(
        """
        SELECT * FROM audit_snapshot_batch_sets
        WHERE run_id=? AND batch_id=? AND snapshot_id=?
          AND current_batch_ids_hash=?
        """,
        (run_id, batch_id, snapshot_id, current_batch_ids_hash),
    ).fetchone()
    contract_row = conn.execute(
        """
        SELECT 1 FROM audit_direction_contracts
        WHERE run_id=? AND batch_id=? AND direction_id=? AND contract_sha=?
          AND validator_version=? AND artifact_sha=?
        """,
        (
            run_id, batch_id, direction_id, contract_sha,
            validator_version, artifact_sha,
        ),
    ).fetchone()
    if batch_set is None or contract_row is None:
        raise ValueError("direction gate frozen identity is missing")
    staging_rows = conn.execute(
        """
        SELECT staging.*,authority.authority_kind
        FROM audit_batch_staging staging
        JOIN audit_batch_staging_authorities_v2 authority
          ON authority.staging_candidate_id=staging.staging_candidate_id
         AND authority.run_id=staging.run_id
         AND authority.batch_id=staging.batch_id
         AND authority.candidate_hash=staging.candidate_hash
         AND authority.raw_artifact_sha=staging.raw_artifact_sha
         AND authority.source_order=staging.source_order
         AND authority.issued_at=staging.created_at
        WHERE staging.run_id=? AND staging.batch_id=?
        ORDER BY staging.source_order
        """,
        (run_id, batch_id),
    ).fetchall()
    frozen_ids = set(json.loads(batch_set["member_ids_json"]))
    if (
        len(staging_rows) != batch_set["member_count"]
        or [row["source_order"] for row in staging_rows]
        != list(range(batch_set["member_count"]))
        or {row["staging_candidate_id"] for row in staging_rows} != frozen_ids
        or any(
            row["authority_kind"] not in {"host_issued", "migration_v2"}
            for row in staging_rows
        )
    ):
        raise ValueError("direction gate staging authority or source order drifted")
    selector_ids = [f"I{index + 1}" for index in range(len(staging_rows))]
    parsed = direction_contract.parse_direction_verdicts(
        verdict_tsv, selector_ids
    )
    mapping = []
    verdict_set = []
    verdict_rows = []
    for source_order, (staging, parsed_item) in enumerate(
        zip(staging_rows, parsed)
    ):
        mapping_item = {
            "selector_id": parsed_item["candidate_id"],
            "staging_candidate_id": staging["staging_candidate_id"],
            "source_order": source_order,
        }
        verdict_material = {
            "schema_version": "history-direction-verdict-v2",
            "run_id": run_id,
            "batch_id": batch_id,
            "snapshot_id": snapshot_id,
            "current_batch_ids_hash": current_batch_ids_hash,
            "direction_id": direction_id,
            "contract_sha": contract_sha,
            "validator_version": validator_version,
            "artifact_sha": artifact_sha,
            "staging_candidate_id": staging["staging_candidate_id"],
            "direction_fit": parsed_item["direction_fit"],
            "direction_evidence": parsed_item["evidence"],
        }
        evidence_sha = direction_evidence_sha256(parsed_item["evidence"])
        verdict_sha = direction_verdict_sha256(verdict_material)
        mapping.append(mapping_item)
        verdict_set.append({
            **mapping_item,
            "direction_fit": parsed_item["direction_fit"],
            "direction_evidence": parsed_item["evidence"],
            "evidence_sha256": evidence_sha,
            "verdict_sha256": verdict_sha,
        })
    raw_selector_sha = hashlib.sha256(verdict_tsv).hexdigest()
    existing = read_batch_direction_gate(conn, run_id=run_id, batch_id=batch_id)
    if existing is not None:
        expected_identity = {
            "snapshot_id": snapshot_id,
            "current_batch_ids_hash": current_batch_ids_hash,
            "direction_id": direction_id,
            "contract_sha": contract_sha,
            "validator_version": validator_version,
            "artifact_sha": artifact_sha,
            "parser_revision": DIRECTION_VERDICT_PARSER_REVISION,
            "raw_selector_artifact_sha256": raw_selector_sha,
            "member_count": len(staging_rows),
            "candidate_mapping": mapping,
            "verdict_set": verdict_set,
        }
        if any(existing[name] != value for name, value in expected_identity.items()):
            raise ValueError("direction gate conflicts with durable state")
        gate_row = conn.execute(
            "SELECT verdict_tsv FROM audit_batch_direction_gates_v2 "
            "WHERE gate_sha256=?",
            (existing["gate_sha256"],),
        ).fetchone()
        if gate_row is None or gate_row["verdict_tsv"] != verdict_tsv:
            raise ValueError("direction gate raw selector artifact conflicts")
        return existing
    if conn.execute(
        "SELECT 1 FROM audit_batch_direction_verdicts_v2 "
        "WHERE run_id=? AND batch_id=? LIMIT 1",
        (run_id, batch_id),
    ).fetchone() is not None:
        raise AuditMigrationError(
            "preexisting individual direction verdicts cannot become a batch gate"
        )
    issued_at = issued_at or _utc_now()
    _semantic_timestamp(issued_at, "issued_at")
    verdict_set_sha = history_contract_v2.framed_sha256(
        "history-batch-direction-verdict-set-v2",
        history_contract_v2.canonical_bytes(verdict_set),
    )
    gate_material = {
        "schema_version": "history-batch-direction-gate-v2",
        "run_id": run_id,
        "batch_id": batch_id,
        "snapshot_id": snapshot_id,
        "current_batch_ids_hash": current_batch_ids_hash,
        "direction_id": direction_id,
        "contract_sha": contract_sha,
        "validator_version": validator_version,
        "artifact_sha": artifact_sha,
        "parser_revision": DIRECTION_VERDICT_PARSER_REVISION,
        "raw_selector_artifact_sha256": raw_selector_sha,
        "member_count": len(staging_rows),
        "candidate_mapping": mapping,
        "verdict_set": verdict_set,
        "verdict_set_sha256": verdict_set_sha,
        "issued_at": issued_at,
    }
    gate_sha = direction_gate_sha256(gate_material)
    for item in verdict_set:
        evidence_json = history_contract_v2.canonical_bytes(
            item["direction_evidence"]
        ).decode("utf-8")
        verdict_rows.append((
            item["verdict_sha256"], run_id, batch_id, snapshot_id,
            current_batch_ids_hash, direction_id, contract_sha,
            validator_version, artifact_sha, item["staging_candidate_id"],
            item["direction_fit"], evidence_json, item["evidence_sha256"],
            issued_at,
        ))
    binding_rows = [
        (
            gate_sha, item["selector_id"], item["source_order"],
            item["staging_candidate_id"], item["verdict_sha256"],
        )
        for item in verdict_set
    ]
    gate_values = (
        gate_sha, run_id, batch_id, snapshot_id, current_batch_ids_hash,
        direction_id, contract_sha, validator_version, artifact_sha,
        DIRECTION_VERDICT_PARSER_REVISION, raw_selector_sha,
        len(staging_rows), _canonical_json_text(mapping),
        _canonical_json_text(verdict_set), verdict_set_sha, verdict_tsv,
        issued_at,
    )
    guard = _DIRECTION_VERDICT_GUARDS.get(id(conn))
    if guard is None or guard["active"]:
        raise AuditMigrationError("direction gate issuance guard is unavailable")
    conn.execute("BEGIN IMMEDIATE")
    try:
        guard["active"] = True
        guard["expected_verdicts"] = set(verdict_rows)
        guard["expected_bindings"] = set(binding_rows)
        guard["expected_gate"] = gate_values
        conn.executemany(
            "INSERT INTO audit_batch_direction_verdicts_v2 "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            verdict_rows,
        )
        conn.executemany(
            "INSERT INTO audit_batch_direction_gate_bindings_v2 "
            "VALUES(?,?,?,?,?)",
            binding_rows,
        )
        conn.execute(
            "INSERT INTO audit_batch_direction_gates_v2 "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            gate_values,
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        _clear_direction_verdict_guard(guard)
    return read_batch_direction_gate(conn, run_id=run_id, batch_id=batch_id)


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


def insert_attempt_completion(
    conn, attempt_id, output_cas_object_id, outcome, normalized_result_json,
    *, completed_at,
):
    """Insert or exactly replay one host-issued completion in an active tx."""
    if not conn.in_transaction:
        raise AuditMigrationError("attempt completion requires a transaction")
    _semantic_timestamp(completed_at, "completed_at")
    usage_json = history_contract_v2.canonical_bytes({}).decode("utf-8")
    values = (
        attempt_id, output_cas_object_id, outcome, normalized_result_json,
        usage_json, completed_at,
    )
    existing = conn.execute(
        "SELECT * FROM audit_attempt_completions_v2 WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) != values:
            raise AuditMigrationError("attempt completion conflicts")
        return values
    guard = _ATTEMPT_TERMINAL_GUARDS.get(id(conn))
    if guard is None or guard["completion"] is not None:
        raise AuditMigrationError("attempt completion guard is unavailable")
    guard["completion"] = values
    try:
        conn.execute(
            "INSERT INTO audit_attempt_completions_v2 VALUES(?,?,?,?,?,?)",
            values,
        )
    finally:
        guard["completion"] = None
    return values


def insert_attempt_budget_settlement(
    conn, attempt_id, *, usage_authority_sha256=None, created_at=None
):
    """Insert or exactly replay reservation or verified-actual accounting."""
    if not conn.in_transaction:
        raise AuditMigrationError("budget settlement requires a transaction")
    terminal = conn.execute(
        """
        SELECT attempt.attempt_id,
               completion.output_cas_object_id,
               completion.outcome AS completion_outcome,
               completion.completed_at AS completion_at,
               completion.usage_json AS completion_usage_json,
               cost.outcome AS cost_outcome,
               cost.billing_state, cost.usage_source,
               cost.completed_at AS cost_completed_at
        FROM audit_task_attempts attempt
        LEFT JOIN audit_attempt_completions_v2 completion USING(attempt_id)
        LEFT JOIN audit_attempt_cost_settlements_v2 cost USING(attempt_id)
        WHERE attempt.attempt_id=?
        """,
        (attempt_id,),
    ).fetchone()
    if terminal is None:
        raise AuditMigrationError("budget settlement attempt is missing")
    if usage_authority_sha256 is None:
        if conn.execute(
            "SELECT 1 FROM audit_verified_usage_authorities_v2 "
            "WHERE attempt_id=?", (attempt_id,)
        ).fetchone() is not None:
            raise AuditMigrationError("verified_usage_authority_mismatch")
        if terminal["completion_at"] is not None:
            if (
                _completion_usage_valid(
                    terminal["completion_usage_json"]
                ) != 1
                or terminal["cost_outcome"] == "cancelled"
            ):
                raise AuditMigrationError("budget terminal authority conflicts")
            terminal_at = terminal["completion_at"]
        elif terminal["cost_outcome"] == "cancelled":
            if (
                terminal["billing_state"] != "unknown"
                or terminal["usage_source"] != "reservation"
            ):
                raise AuditMigrationError(
                    "cancellation accounting authority conflicts"
                )
            terminal_at = terminal["cost_completed_at"]
        else:
            raise AuditMigrationError("budget terminal authority is missing")
        values = (attempt_id, 0, None, terminal_at)
    else:
        authority_row = conn.execute(
            "SELECT * FROM audit_verified_usage_authorities_v2 "
            "WHERE usage_authority_sha256=?",
            (usage_authority_sha256,),
        ).fetchone()
        if authority_row is None:
            raise AuditMigrationError("verified_usage_authority_mismatch")
        authority = _verified_usage_authority_for_terminal(
            conn, usage_authority_sha256, attempt_id=attempt_id,
            output_cas_object_id=authority_row["output_cas_object_id"],
            terminal_outcome=authority_row["terminal_outcome"],
            terminal_at=authority_row["terminal_at"],
        )
        terminal_at = authority["terminal_at"]
        if authority["terminal_outcome"] == "cancelled":
            if terminal["completion_at"] is not None:
                raise AuditMigrationError(
                    "attempt cancellation conflicts with completion"
                )
        elif (
            terminal["completion_at"] != terminal_at
            or terminal["completion_outcome"]
                != authority["terminal_outcome"]
            or terminal["output_cas_object_id"]
                != authority["output_cas_object_id"]
            or _completion_usage_valid(
                terminal["completion_usage_json"]
            ) != 1
        ):
            raise AuditMigrationError(
                "verified usage completion authority conflicts"
            )
        values = (
            attempt_id, 1, authority["actual_json"], terminal_at,
        )
    _semantic_timestamp(terminal_at, "terminal_at")
    if created_at is not None and created_at != terminal_at:
        raise AuditMigrationError("budget settlement timestamp conflicts")
    existing = conn.execute(
        "SELECT * FROM audit_runtime_budget_settlements_v2 WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) != values:
            raise AuditMigrationError("budget settlement conflicts")
        return values
    guard = _ATTEMPT_TERMINAL_GUARDS.get(id(conn))
    if guard is None or guard["budget_settlement"] is not None:
        raise AuditMigrationError("budget settlement guard is unavailable")
    guard["budget_settlement"] = values
    try:
        conn.execute(
            "INSERT INTO audit_runtime_budget_settlements_v2 VALUES(?,?,?,?)",
            values,
        )
    finally:
        guard["budget_settlement"] = None
    return values


def insert_unverified_budget_settlement(
    conn, attempt_id, *, created_at=None
):
    """Compatibility wrapper for reservation-based terminal accounting."""
    return insert_attempt_budget_settlement(
        conn, attempt_id, created_at=created_at
    )



def record_attempt_terminal_cost_fact(
    conn, attempt_id, *, completed_at, cancellation=False,
    error_class=None, run_latency_ms=None, usage_authority_sha256=None,
):
    if not conn.in_transaction:
        raise AuditMigrationError("attempt terminal cost fact requires a transaction")
    terminal = conn.execute(
        """
        SELECT budget.usage_verified, budget.actual_json,
               budget.created_at AS budget_created_at,
               completion.output_cas_object_id,
               completion.outcome AS completion_outcome,
               completion.completed_at AS completion_at,
               attempt.created_at AS started_at
        FROM audit_task_attempts attempt
        LEFT JOIN audit_runtime_budget_settlements_v2 budget USING(attempt_id)
        LEFT JOIN audit_attempt_completions_v2 completion USING(attempt_id)
        WHERE attempt.attempt_id=?
        """,
        (attempt_id,),
    ).fetchone()
    if terminal is None:
        raise AuditMigrationError("attempt terminal cost authority is missing")
    if cancellation:
        if terminal["completion_outcome"] is not None:
            raise AuditMigrationError("completed attempt cannot be cancelled")
        expected_terminal_outcome = "cancelled"
        expected_output = None
        outcome = "cancelled"
        error_class = error_class or "cancelled"
    else:
        if terminal["usage_verified"] is None:
            raise AuditMigrationError("attempt terminal cost authority is missing")
        if terminal["completion_outcome"] is None:
            raise AuditMigrationError("attempt completion authority is missing")
        expected_terminal_outcome = terminal["completion_outcome"]
        expected_output = terminal["output_cas_object_id"]
        outcome = (
            "success" if expected_terminal_outcome == "valid" else "failed"
        )
        error_class = None if outcome == "success" else expected_terminal_outcome
    if usage_authority_sha256 is None:
        if conn.execute(
            "SELECT 1 FROM audit_verified_usage_authorities_v2 "
            "WHERE attempt_id=?", (attempt_id,)
        ).fetchone() is not None:
            raise AuditMigrationError("verified_usage_authority_mismatch")
        if terminal["usage_verified"] is not None and (
            terminal["usage_verified"] != 0
            or terminal["actual_json"] is not None
        ):
            raise AuditMigrationError(
                "provider usage receipt authority is unavailable"
            )
        billing_state = "unknown"
        usage_source = "reservation"
        price_source = None
        currency = None
    else:
        verified = _verified_usage_authority_for_terminal(
            conn, usage_authority_sha256, attempt_id=attempt_id,
            output_cas_object_id=expected_output,
            terminal_outcome=expected_terminal_outcome,
            terminal_at=completed_at,
        )
        if (
            terminal["usage_verified"] != 1
            or terminal["actual_json"] != verified["actual_json"]
            or terminal["budget_created_at"] != completed_at
        ):
            raise AuditMigrationError(
                "verified usage budget authority conflicts"
            )
        billing_state = verified["billing_state"]
        usage_source = "verified_actual"
        price_source = verified["price_source"]
        currency = verified["currency"]
    completed = _semantic_timestamp(completed_at, "completed_at")
    if (
        terminal["budget_created_at"] is not None
        and terminal["budget_created_at"] != completed_at
    ):
        raise AuditMigrationError("budget settlement timestamp conflicts")
    started = _semantic_timestamp(terminal["started_at"], "started_at")
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
            "INSERT INTO audit_attempt_cost_settlements_v2 "
            "VALUES(?,?,?,?,?,?,?,?,?,?)", values,
        )
    finally:
        guard["settlement"] = None
    return values[8]



def _verified_usage_authority_for_terminal(
    conn, usage_authority_sha256, *, attempt_id, output_cas_object_id,
    terminal_outcome, terminal_at,
):
    if not isinstance(usage_authority_sha256, str) or not _router_is_sha(
        usage_authority_sha256
    ):
        raise AuditMigrationError("verified_usage_authority_mismatch")
    row = conn.execute(
        "SELECT * FROM audit_verified_usage_authorities_v2 "
        "WHERE usage_authority_sha256=?",
        (usage_authority_sha256,),
    ).fetchone()
    if (
        row is None
        or _verified_usage_authority_row_valid(*tuple(row)) != 1
        or row["attempt_id"] != attempt_id
        or row["output_cas_object_id"] != output_cas_object_id
        or row["terminal_outcome"] != terminal_outcome
        or row["terminal_at"] != terminal_at
    ):
        raise AuditMigrationError("verified_usage_authority_mismatch")
    result = dict(row)
    try:
        result["actual_usage"] = _verified_actual_usage(
            _closed_json(row["actual_json"])
        )
    except (TypeError, ValueError) as exc:
        raise AuditMigrationError("verified_usage_authority_mismatch") from exc
    return result


def _verified_usage_authority_for_settlement(
    conn, *, attempt_id, actual_json, terminal_at
):
    """Return exact durable usage authority, including on prefix schemas."""
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='audit_verified_usage_authorities_v2'"
    ).fetchone()
    if present is None:
        return None
    row = conn.execute(
        "SELECT * FROM audit_verified_usage_authorities_v2 "
        "WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    if (
        row is None
        or _verified_usage_authority_row_valid(*tuple(row)) != 1
        or row["actual_json"] != actual_json
        or row["terminal_at"] != terminal_at
    ):
        return None
    return dict(row)


def _l1_route_authority(conn, material):
    try:
        _l1_public_material(material)
    except (KeyError, TypeError, ValueError) as exc:
        raise AuditMigrationError("L1 attempt fact input is not closed") from exc
    rows = conn.execute(
        "SELECT * FROM audit_l1_candidate_route_authorities_v2 "
        "WHERE run_id=? AND candidate_id=? AND intent=? "
        "AND provider=? AND capability_profile_hash=?",
        (
            material["run_id"], material["candidate_id"],
            material["intent"], material["provider"],
            material["capability_profile_hash"],
        ),
    ).fetchall()
    if len(rows) != 1:
        raise AuditMigrationError("L1 attempt fact lacks final route authority")
    route = rows[0]
    try:
        plan = history_audit_plan.validate_runtime_plan_material(
            _closed_json(route["plan_json"])
        )
        provider = material["provider"]
        terminal = _semantic_timestamp(material["terminal_at"], "terminal_at")
        pre_created = _semantic_timestamp(
            route["pre_phase_created_at"], "pre_phase_created_at"
        )
        source_created = _semantic_timestamp(
            route["l1_source_created_at"], "l1_source_created_at"
        )
        if (
            history_audit_plan.runtime_plan_sha_from_material(plan)
            != route["plan_sha"]
            or plan["run_id"] != material["run_id"]
            or plan["intent"] != material["intent"]
            or provider not in plan["provider_pools_ordered"]["comparator"]
            or (
                material["ordinal"] == 0
                and provider
                    != plan["provider_pools_ordered"]["comparator"][0]
            )
            or plan["provider_capabilities"].get(provider, {}).get("provider")
                != provider
            or plan["provider_capabilities"].get(provider, {}).get(
                "capability_profile_hash"
            ) != material["capability_profile_hash"]
            or plan["provider_capability_profile_hashes"].get(provider)
                != material["capability_profile_hash"]
            or terminal < pre_created
            or terminal > source_created
            or (
                material["outcome"] == "success"
                and material["result_evidence_sha256"]
                    != route["comparator_receipt_sha256"]
            )
            or (
                material["outcome"] != "success"
                and material["result_evidence_sha256"] is not None
            )
        ):
            raise ValueError("L1 route identity is not exact")
    except (
        KeyError, TypeError, ValueError, history_audit_plan.AuditPlanError,
    ) as exc:
        raise AuditMigrationError(
            "L1 attempt fact lacks final route authority"
        ) from exc
    return route


def _l1_require_contiguous_previous(conn, material, route):
    if material["attempt_id"] != _l1_attempt_id_for_material(conn, material):
        raise AuditMigrationError("L1 attempt id is not host-derived")
    if material["ordinal"] == 0:
        if material["previous_attempt_id"] is not None:
            raise AuditMigrationError("L1 attempt chain is incomplete")
    else:
        previous = conn.execute(
            "SELECT * FROM audit_l1_attempt_facts_v2 WHERE attempt_id=?",
            (material["previous_attempt_id"],),
        ).fetchone()
        if (
            previous is None
            or previous["run_id"] != material["run_id"]
            or previous["candidate_id"] != material["candidate_id"]
            or previous["intent"] != material["intent"]
            or previous["ordinal"] != material["ordinal"] - 1
            or previous["outcome"] not in {"failed", "cancelled"}
            or _semantic_timestamp(previous["terminal_at"], "terminal_at")
                > _semantic_timestamp(material["terminal_at"], "terminal_at")
        ):
            raise AuditMigrationError("L1 attempt chain is incomplete")
        try:
            plan = history_audit_plan.validate_runtime_plan_material(
                _closed_json(route["plan_json"])
            )
            pool = plan["provider_pools_ordered"]["comparator"]
            previous_index = pool.index(previous["provider"])
            current_index = pool.index(material["provider"])
        except (
            KeyError, TypeError, ValueError, history_audit_plan.AuditPlanError,
        ) as exc:
            raise AuditMigrationError(
                "L1 comparator failover authority is invalid"
            ) from exc
        if current_index not in {previous_index, previous_index + 1}:
            raise AuditMigrationError(
                "L1 comparator failover is out of frozen order"
            )
    occupied = conn.execute(
        "SELECT attempt_id FROM audit_l1_attempt_facts_v2 "
        "WHERE run_id=? AND candidate_id=? AND ordinal=?",
        (material["run_id"], material["candidate_id"], material["ordinal"]),
    ).fetchone()
    if occupied is not None and occupied["attempt_id"] != material["attempt_id"]:
        raise AuditMigrationError("L1 attempt chain conflicts")


def _issue_test_l1_verified_usage_authority(
    conn, material, *, actual_usage, billing_state="unknown",
    price_source=None, currency=None,
):
    """Mint an immutable fake-provider L1 usage token before settlement."""
    if conn.in_transaction:
        raise AuditMigrationError("L1 verified usage issuance requires idle DB")
    if (
        not isinstance(material, dict)
        or material.get("usage_source") != "reservation"
        or material.get("usage_authority_sha256") is not None
    ):
        raise AuditMigrationError("L1 verified usage request is invalid")
    conn.execute("BEGIN IMMEDIATE")
    try:
        route = _l1_route_authority(conn, material)
        _l1_require_contiguous_previous(conn, material, route)
        values = _l1_verified_usage_authority_values(
            material, actual_usage=actual_usage, billing_state=billing_state,
            price_source=price_source, currency=currency,
            route_fact_sha256=route["route_fact_sha256"],
            final_phase_fact_sha256=route["final_phase_fact_sha256"],
            source_set_sha256=route["source_set_sha256"],
        )
        existing = conn.execute(
            "SELECT * FROM audit_l1_verified_usage_authorities_v2 "
            "WHERE attempt_id=?", (material["attempt_id"],),
        ).fetchone()
        if existing is not None:
            if tuple(existing) != values:
                raise AuditMigrationError("L1 verified usage conflicts")
        else:
            guard = _L1_ATTEMPT_FACT_GUARDS.get(id(conn))
            if (
                guard is None
                or guard["usage"] is not None
                or guard["expected"] is not None
            ):
                raise AuditMigrationError("L1 usage guard is unavailable")
            guard["usage"] = values
            try:
                conn.execute(
                    "INSERT INTO audit_l1_verified_usage_authorities_v2 "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    values,
                )
            finally:
                guard["usage"] = None
        conn.execute("COMMIT")
        return values[0]
    except Exception:
        guard = _L1_ATTEMPT_FACT_GUARDS.get(id(conn))
        if guard is not None:
            guard["usage"] = None
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def record_l1_attempt_fact(conn, material):
    """Append or exactly replay one source- and usage-bound L1 cost fact."""
    if not conn.in_transaction:
        raise AuditMigrationError("L1 attempt fact requires an active transaction")
    route = _l1_route_authority(conn, material)
    _l1_require_contiguous_previous(conn, material, route)
    actual = None
    billing_state = "unknown"
    price_source = None
    currency = None
    if material["usage_source"] == "verified_actual":
        usage = conn.execute(
            "SELECT * FROM audit_l1_verified_usage_authorities_v2 "
            "WHERE usage_authority_sha256=?",
            (material["usage_authority_sha256"],),
        ).fetchone()
        if usage is None:
            raise AuditMigrationError("L1 verified usage authority mismatch")
        try:
            actual = _closed_json(usage["actual_json"])
            expected_usage = _l1_verified_usage_authority_values(
                material, actual_usage=actual,
                billing_state=usage["billing_state"],
                price_source=usage["price_source"], currency=usage["currency"],
                route_fact_sha256=route["route_fact_sha256"],
                final_phase_fact_sha256=route["final_phase_fact_sha256"],
                source_set_sha256=route["source_set_sha256"],
                authority_scope=usage["authority_scope"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AuditMigrationError(
                "L1 verified usage authority mismatch"
            ) from exc
        if tuple(usage) != expected_usage:
            raise AuditMigrationError("L1 verified usage authority mismatch")
        billing_state = usage["billing_state"]
        price_source = usage["price_source"]
        currency = usage["currency"]
    try:
        values = _l1_attempt_fact_values(
            material, actual_usage=actual, billing_state=billing_state,
            price_source=price_source, currency=currency,
            route_fact_sha256=route["route_fact_sha256"],
            final_phase_fact_sha256=route["final_phase_fact_sha256"],
            source_set_sha256=route["source_set_sha256"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AuditMigrationError("L1 attempt fact is invalid") from exc
    existing = conn.execute(
        "SELECT * FROM audit_l1_attempt_facts_v2 WHERE attempt_id=?",
        (material["attempt_id"],),
    ).fetchone()
    if existing is not None:
        if tuple(existing) != values:
            raise AuditMigrationError("L1 attempt fact conflicts")
        return values[20]
    guard = _L1_ATTEMPT_FACT_GUARDS.get(id(conn))
    if (
        guard is None
        or guard["expected"] is not None
        or guard["usage"] is not None
    ):
        raise AuditMigrationError("L1 attempt fact guard is unavailable")
    guard["expected"] = values
    try:
        conn.execute(
            "INSERT INTO audit_l1_attempt_facts_v2 "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
    finally:
        guard["expected"] = None
    return values[20]


def _issue_test_verified_usage_authority(
    conn, *, attempt_id, output_cas_object_id, terminal_outcome,
    terminal_at, actual_usage, billing_state="unknown", price_source=None,
    currency=None,
):
    """Mint or exactly replay one fake-provider verified-usage capability."""
    if conn.in_transaction:
        raise AuditMigrationError(
            "verified usage authority issuance requires an idle connection"
        )
    durable = conn.execute(
        """
        SELECT attempt.attempt_id,attempt.request_cas_object_id,
               attempt.provenance_json,attempt.created_at AS attempt_created_at,
               task.run_id,task.staging_candidate_id,
               plan.intent,reservation.candidate_id AS reserved_candidate_id,
               reservation.intent AS reserved_intent,
               request.integrity_state AS request_integrity_state
        FROM audit_task_attempts attempt
        JOIN audit_logical_tasks task ON task.task_hash=attempt.task_hash
        JOIN audit_task_bindings_v2 binding ON binding.task_hash=task.task_hash
        JOIN audit_l2_plans_v2 plan ON plan.plan_sha=binding.plan_sha
        JOIN audit_runtime_budget_reservations_v2 reservation
          ON reservation.attempt_id=attempt.attempt_id
        JOIN audit_cas_objects request
          ON request.object_id=attempt.request_cas_object_id
        WHERE attempt.attempt_id=?
        """,
        (attempt_id,),
    ).fetchone()
    if durable is None or durable["request_integrity_state"] != "verified":
        raise AuditMigrationError("verified usage attempt authority is missing")
    try:
        provenance = history_contract_v2.parse_json_bytes(
            durable["provenance_json"].encode("utf-8")
        )
    except history_contract_v2.ContractV2Error as exc:
        raise AuditMigrationError(
            "verified usage attempt provenance is invalid"
        ) from exc
    if (
        durable["staging_candidate_id"] != durable["reserved_candidate_id"]
        or durable["intent"] != durable["reserved_intent"]
        or _semantic_timestamp(terminal_at, "terminal_at")
            < _semantic_timestamp(
                durable["attempt_created_at"], "attempt_created_at"
            )
    ):
        raise AuditMigrationError("verified usage attempt authority is invalid")
    if output_cas_object_id is not None:
        output = conn.execute(
            "SELECT integrity_state FROM audit_cas_objects WHERE object_id=?",
            (output_cas_object_id,),
        ).fetchone()
        if output is None or output["integrity_state"] != "verified":
            raise AuditMigrationError("verified usage output authority is missing")
    try:
        values = _verified_usage_authority_values(
            attempt_id=attempt_id, run_id=durable["run_id"],
            intent=durable["intent"],
            candidate_id=durable["staging_candidate_id"],
            provider=provenance["provider"],
            capability_profile_hash=provenance["capability_profile_hash"],
            request_cas_object_id=durable["request_cas_object_id"],
            output_cas_object_id=output_cas_object_id,
            terminal_outcome=terminal_outcome, actual_usage=actual_usage,
            billing_state=billing_state, price_source=price_source,
            currency=currency, terminal_at=terminal_at,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AuditMigrationError("verified usage authority is invalid") from exc
    existing = conn.execute(
        "SELECT * FROM audit_verified_usage_authorities_v2 "
        "WHERE attempt_id=? OR usage_authority_sha256=?",
        (attempt_id, values[0]),
    ).fetchall()
    if existing:
        if len(existing) != 1 or tuple(existing[0]) != values:
            raise AuditMigrationError("verified usage authority conflicts")
        return values[0]
    terminal_count = conn.execute(
        """
        SELECT
          (SELECT count(*) FROM audit_attempt_completions_v2
           WHERE attempt_id=?),
          (SELECT count(*) FROM audit_runtime_budget_settlements_v2
           WHERE attempt_id=?),
          (SELECT count(*) FROM audit_attempt_cost_settlements_v2
           WHERE attempt_id=?)
        """,
        (attempt_id, attempt_id, attempt_id),
    ).fetchone()
    if tuple(terminal_count) != (0, 0, 0):
        raise AuditMigrationError(
            "verified usage authority cannot retrofit a terminal attempt"
        )
    guard = _VERIFIED_USAGE_AUTHORITY_GUARDS.get(id(conn))
    if guard is None or guard["expected"] is not None:
        raise AuditMigrationError("verified usage authority guard is unavailable")
    conn.execute("BEGIN IMMEDIATE")
    try:
        guard["expected"] = values
        try:
            conn.execute(
                "INSERT INTO audit_verified_usage_authorities_v2 "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values,
            )
        finally:
            guard["expected"] = None
        conn.execute("COMMIT")
        return values[0]
    except Exception:
        guard["expected"] = None
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def issue_candidate_budget_receipt(
    conn, plan_material, plan_sha, *, decided_at=None
):
    """Durably admit or reject one frozen candidate cohort before lifecycle state."""
    if conn.in_transaction:
        raise AuditMigrationError(
            "candidate budget admission requires an idle connection"
        )
    try:
        material = history_audit_plan.validate_runtime_plan_material(
            plan_material
        )
        if (
            history_audit_plan.runtime_plan_sha_from_material(material)
            != plan_sha
        ):
            raise AuditMigrationError("candidate budget plan identity mismatch")
        candidate_ids = material["snapshot"]["current_batch_ids"]
        if (
            not isinstance(candidate_ids, list)
            or not candidate_ids
            or candidate_ids != sorted(candidate_ids)
            or len(set(candidate_ids)) != len(candidate_ids)
        ):
            raise AuditMigrationError("candidate budget cohort is invalid")
        round_limit = material["budget_policy"]["intents"][
            material["intent"]
        ]["round"]["candidates"]
        if type(round_limit) is not int or round_limit < 0:
            raise AuditMigrationError("candidate budget policy is invalid")
    except (
        KeyError, TypeError, ValueError,
        history_audit_plan.AuditPlanError,
    ) as exc:
        if isinstance(exc, AuditMigrationError):
            raise
        raise AuditMigrationError("candidate budget authority is invalid") from exc
    requested = len(candidate_ids)
    decision = "accepted" if requested <= round_limit else "rejected"
    candidate_ids_json = _semantic_canonical(candidate_ids)
    static_values = (
        material["run_id"], material["batch_id"], material["intent"],
        plan_sha, material["budget_policy_sha"], candidate_ids_json,
        requested, round_limit, decision,
    )
    columns = (
        "decision_sha256", "run_id", "batch_id", "intent", "plan_sha",
        "budget_policy_sha", "candidate_ids_json", "requested_candidates",
        "round_candidate_limit", "decision", "decided_at",
    )
    guard = _CANDIDATE_BUDGET_GUARDS.get(id(conn))
    if guard is None or guard["expected"] is not None:
        raise AuditMigrationError("candidate budget receipt guard is unavailable")
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT decision_sha256, run_id, batch_id, intent, plan_sha, "
            "budget_policy_sha, candidate_ids_json, requested_candidates, "
            "round_candidate_limit, decision, decided_at "
            "FROM audit_candidate_budget_receipts_v2 "
            "WHERE run_id=? AND intent=?",
            (material["run_id"], material["intent"]),
        ).fetchone()
        if existing is not None:
            stored = tuple(existing)
            stored_material = {
                "schema_version": "history-candidate-budget-receipt-v2",
                "run_id": stored[1], "batch_id": stored[2],
                "intent": stored[3], "plan_sha": stored[4],
                "budget_policy_sha": stored[5],
                "candidate_ids": json.loads(stored[6]),
                "requested_candidates": stored[7],
                "round_candidate_limit": stored[8],
                "decision": stored[9], "decided_at": stored[10],
            }
            expected_sha = _semantic_sha(
                "history-candidate-budget-receipt-v2", stored_material
            )
            if stored[4] != plan_sha:
                raise AuditMigrationError(
                    "candidate budget round identity conflicts"
                )
            if stored[1:10] != static_values or stored[0] != expected_sha:
                raise AuditMigrationError("candidate budget receipt conflicts")
            conn.execute("COMMIT")
            return dict(zip(columns, stored))
        decided_at = decided_at or _utc_now()
        try:
            parsed = datetime.datetime.fromisoformat(
                decided_at.replace("Z", "+00:00")
            )
        except (AttributeError, ValueError) as exc:
            raise AuditMigrationError(
                "candidate budget timestamp is invalid"
            ) from exc
        if parsed.tzinfo is None:
            raise AuditMigrationError("candidate budget timestamp is invalid")
        receipt_material = {
            "schema_version": "history-candidate-budget-receipt-v2",
            "run_id": static_values[0], "batch_id": static_values[1],
            "intent": static_values[2], "plan_sha": static_values[3],
            "budget_policy_sha": static_values[4],
            "candidate_ids": candidate_ids,
            "requested_candidates": requested,
            "round_candidate_limit": round_limit,
            "decision": decision, "decided_at": decided_at,
        }
        values = (
            _semantic_sha(
                "history-candidate-budget-receipt-v2", receipt_material
            ),
            *static_values,
            decided_at,
        )
        guard["expected"] = values
        try:
            conn.execute(
                "INSERT INTO audit_candidate_budget_receipts_v2 "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
        finally:
            guard["expected"] = None
        conn.execute("COMMIT")
        return dict(zip(columns, values))
    except Exception:
        guard["expected"] = None
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _accepted_candidate_budget_receipt_matches(conn, plan_material):
    """Verify the exact accepted decision for a validated frozen plan."""
    try:
        material = history_audit_plan.validate_runtime_plan_material(
            plan_material
        )
        plan_sha = history_audit_plan.runtime_plan_sha_from_material(material)
        candidate_ids = material["snapshot"]["current_batch_ids"]
        candidate_ids_json = _semantic_canonical(candidate_ids)
        round_limit = material["budget_policy"]["intents"][
            material["intent"]
        ]["round"]["candidates"]
        row = conn.execute(
            "SELECT decision_sha256, run_id, batch_id, intent, plan_sha, "
            "budget_policy_sha, candidate_ids_json, requested_candidates, "
            "round_candidate_limit, decision, decided_at "
            "FROM audit_candidate_budget_receipts_v2 WHERE plan_sha=?",
            (plan_sha,),
        ).fetchone()
        if row is None:
            return False
        values = tuple(row)
        receipt_material = {
            "schema_version": "history-candidate-budget-receipt-v2",
            "run_id": values[1], "batch_id": values[2],
            "intent": values[3], "plan_sha": values[4],
            "budget_policy_sha": values[5],
            "candidate_ids": json.loads(values[6]),
            "requested_candidates": values[7],
            "round_candidate_limit": values[8],
            "decision": values[9], "decided_at": values[10],
        }
        return (
            values[0] == _semantic_sha(
                "history-candidate-budget-receipt-v2", receipt_material
            )
            and values[1:] == (
                material["run_id"], material["batch_id"], material["intent"],
                plan_sha, material["budget_policy_sha"], candidate_ids_json,
                len(candidate_ids), round_limit, "accepted", values[10],
            )
        )
    except (
        KeyError, TypeError, ValueError,
        history_audit_plan.AuditPlanError,
    ):
        return False


def _legacy_router_authority_exists(
    conn, *, run_id=None, candidate_id=None, route_fact_sha256=None,
    plan_sha=None,
):
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='audit_legacy_candidate_route_authorities_v2'"
    ).fetchone() is None:
        return False
    clauses = []
    values = []
    for column, value in (
        ("run_id", run_id),
        ("candidate_id", candidate_id),
        ("route_fact_sha256", route_fact_sha256),
        ("plan_sha", plan_sha),
    ):
        if value is not None:
            clauses.append(column + "=?")
            values.append(value)
    if not clauses:
        raise ValueError("legacy router authority lookup needs an identity")
    return conn.execute(
        "SELECT 1 FROM audit_legacy_candidate_route_authorities_v2 WHERE "
        + " AND ".join(clauses) + " LIMIT 1",
        tuple(values),
    ).fetchone() is not None


def record_candidate_route_facts(
    conn, run_id, batch_id, intent, route_authority, *, created_at
):
    """Reject caller-shaped route facts; only durable source derivation is valid."""
    del conn, run_id, batch_id, intent, route_authority, created_at
    raise AuditMigrationError("caller_route_authority_forbidden")


def candidate_route_authority_replay_matches(
    conn, run_id, batch_id, intent, route_authority
):
    """Caller-shaped route facts never reopen durable route authority."""
    del conn, run_id, batch_id, intent, route_authority
    return False


def record_candidate_l2_dispatch_fact(conn, plan_sha, *, created_at):
    """Bind an authorized route decision to the exact durable L2 plan."""
    if not conn.in_transaction:
        raise AuditMigrationError("candidate dispatch fact requires a transaction")
    guard = _COST_FACT_GUARDS.get(id(conn))
    if guard is None or guard["dispatch_issuance"] is None:
        raise AuditMigrationError(
            "candidate dispatch requires active plan issuance"
        )
    row = conn.execute(
        """
        SELECT plan.plan_sha, plan.run_id, plan.candidate_id,
               plan.created_at AS plan_created_at,
               route.fact_sha256, route.dispatch_allowed, route.intent
        FROM audit_l2_plans_v2 plan
        JOIN audit_candidate_route_facts_v2 route
          ON route.run_id=plan.run_id AND route.candidate_id=plan.candidate_id
         AND route.intent=plan.intent
        JOIN audit_candidate_route_source_bindings_v2 binding
          ON binding.run_id=route.run_id
         AND binding.candidate_id=route.candidate_id
         AND binding.route_fact_sha256=route.fact_sha256
        JOIN audit_router_phase_facts_v2 phase
          ON phase.phase_fact_sha256=binding.final_phase_fact_sha256
         AND phase.phase='final'
         AND phase.candidate_id=route.candidate_id
         AND phase.source_set_sha256=binding.source_set_sha256
        JOIN audit_router_source_sets_v2 source_set
          ON source_set.source_set_sha256=binding.source_set_sha256
         AND source_set.route_round_sha256=phase.route_round_sha256
         AND source_set.phase='final'
        JOIN audit_router_rounds_v2 round
          ON round.route_round_sha256=phase.route_round_sha256
         AND round.run_id=route.run_id
         AND round.intent=route.intent
        JOIN audit_candidate_route_observation_boundaries_v2 observation
          ON observation.run_id=route.run_id
         AND observation.candidate_id=route.candidate_id
         AND observation.route_fact_sha256=route.fact_sha256
        WHERE plan.plan_sha=?
          AND route.router_facts_json=phase.router_facts_json
          AND route.risk_slices_json=phase.risk_slices_json
          AND route.matched_rule_ids_json=phase.matched_rule_ids_json
          AND route.route=phase.route
          AND route.call_l1_model=phase.call_l1_model
          AND route.dispatch_allowed=phase.dispatch_allowed
          AND route.rule_table_sha256=phase.rule_table_sha256
          AND route.risk_policy_version=phase.risk_policy_version
          AND route.created_at=plan.created_at
          AND observation.observation_scope='host_issued_shadow'
          AND observation.production_authority=0
          AND observation.created_at=plan.created_at
          AND binding.bound_at=plan.created_at
        """,
        (plan_sha,),
    ).fetchone()
    if row is None or row["dispatch_allowed"] != 1:
        raise AuditMigrationError("L2 plan lacks an authorized route decision")
    if _legacy_router_authority_exists(
        conn,
        run_id=row["run_id"],
        candidate_id=row["candidate_id"],
        route_fact_sha256=row["fact_sha256"],
    ):
        raise AuditMigrationError("legacy candidate route authority is quarantined")
    expected_issuance = (
        plan_sha, row["run_id"], row["candidate_id"], row["plan_created_at"]
    )
    if (
        guard["dispatch_issuance"] != expected_issuance
        or created_at != row["plan_created_at"]
    ):
        raise AuditMigrationError("candidate dispatch plan issuance conflicts")
    guard["dispatch_issuance"] = None
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
        raise AuditMigrationError("candidate L2 dispatch is not new")
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


def _insert_new_l2_plan_with_dispatch(conn, plan_values):
    """Insert one new exact L2 plan and consume its dispatch issuance once."""
    if not conn.in_transaction:
        raise AuditMigrationError("L2 plan issuance requires a transaction")
    if not isinstance(plan_values, tuple) or len(plan_values) != 11:
        raise AuditMigrationError("L2 plan issuance values are invalid")
    plan_sha, run_id, candidate_id = plan_values[:3]
    created_at = plan_values[-1]
    guard = _COST_FACT_GUARDS.get(id(conn))
    if (
        guard is None
        or guard["dispatch_issuance"] is not None
        or conn.execute(
            "SELECT 1 FROM audit_l2_plans_v2 WHERE plan_sha=? OR run_id=?",
            (plan_sha, run_id),
        ).fetchone() is not None
    ):
        raise AuditMigrationError("L2 plan issuance is not new")
    guard["dispatch_issuance"] = (
        plan_sha, run_id, candidate_id, created_at
    )
    try:
        conn.execute(
            "INSERT INTO audit_l2_plans_v2 VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            plan_values,
        )
        return record_candidate_l2_dispatch_fact(
            conn, plan_sha, created_at=created_at
        )
    finally:
        guard["dispatch_issuance"] = None


def candidate_l2_dispatch_replay_matches(conn, plan_sha, *, created_at):
    """Verify an existing dispatch fact without granting issuance authority."""
    if _legacy_router_authority_exists(conn, plan_sha=plan_sha):
        return False
    row = conn.execute(
        """
        SELECT plan.run_id, plan.candidate_id, route.fact_sha256,
               dispatch.plan_sha, dispatch.run_id, dispatch.candidate_id,
               dispatch.route_fact_sha256, dispatch.dispatch_sha256,
               dispatch.created_at
        FROM audit_l2_plans_v2 plan
        JOIN audit_candidate_route_facts_v2 route
          ON route.run_id=plan.run_id AND route.candidate_id=plan.candidate_id
         AND route.intent=plan.intent
        JOIN audit_candidate_route_source_bindings_v2 binding
          ON binding.run_id=route.run_id
         AND binding.candidate_id=route.candidate_id
         AND binding.route_fact_sha256=route.fact_sha256
        JOIN audit_router_phase_facts_v2 phase
          ON phase.phase_fact_sha256=binding.final_phase_fact_sha256
         AND phase.phase='final'
         AND phase.candidate_id=route.candidate_id
         AND phase.source_set_sha256=binding.source_set_sha256
        JOIN audit_router_source_sets_v2 source_set
          ON source_set.source_set_sha256=binding.source_set_sha256
         AND source_set.route_round_sha256=phase.route_round_sha256
         AND source_set.phase='final'
        JOIN audit_router_rounds_v2 round
          ON round.route_round_sha256=phase.route_round_sha256
         AND round.run_id=route.run_id
         AND round.intent=route.intent
        JOIN audit_candidate_route_observation_boundaries_v2 observation
          ON observation.run_id=route.run_id
         AND observation.candidate_id=route.candidate_id
         AND observation.route_fact_sha256=route.fact_sha256
        JOIN audit_candidate_l2_dispatch_facts_v2 dispatch
          ON dispatch.plan_sha=plan.plan_sha
         AND dispatch.run_id=plan.run_id
         AND dispatch.candidate_id=plan.candidate_id
         AND dispatch.route_fact_sha256=route.fact_sha256
        WHERE plan.plan_sha=?
          AND route.router_facts_json=phase.router_facts_json
          AND route.risk_slices_json=phase.risk_slices_json
          AND route.matched_rule_ids_json=phase.matched_rule_ids_json
          AND route.route=phase.route
          AND route.call_l1_model=phase.call_l1_model
          AND route.dispatch_allowed=phase.dispatch_allowed
          AND route.rule_table_sha256=phase.rule_table_sha256
          AND route.risk_policy_version=phase.risk_policy_version
          AND route.created_at=plan.created_at
          AND observation.observation_scope='host_issued_shadow'
          AND observation.production_authority=0
          AND observation.created_at=plan.created_at
          AND binding.bound_at=plan.created_at
        """,
        (plan_sha,),
    ).fetchone()
    if row is None:
        return False
    material = {
        "plan_sha": plan_sha, "run_id": row["run_id"],
        "candidate_id": row["candidate_id"],
        "route_fact_sha256": row["fact_sha256"], "created_at": created_at,
    }
    expected = (
        plan_sha, row["run_id"], row["candidate_id"], row["fact_sha256"],
        _semantic_sha("history-candidate-l2-dispatch-v2", material), created_at,
    )
    return tuple(row)[3:] == expected


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


_VERIFIED_USAGE_TERMINAL_OUTCOMES = frozenset({
    "valid", "timeout", "429", "5xx", "overflow", "syntax", "schema",
    "item_set", "truncated", "invalid_anchor", "provider_error",
    "cancelled",
})


def _verified_actual_usage(value):
    required = {"input_tokens", "output_tokens", "provider_usage_units"}
    optional = {"cache_tokens", "currency_micros"}
    if (
        not isinstance(value, dict)
        or not required.issubset(value)
        or set(value).difference(required | optional)
        or any(type(amount) is not int or amount < 0 for amount in value.values())
    ):
        raise ValueError("verified actual usage is invalid")
    return copy.deepcopy(value)


def _verified_usage_authority_values(
    *, attempt_id, run_id, intent, candidate_id, provider,
    capability_profile_hash, request_cas_object_id, output_cas_object_id,
    terminal_outcome, actual_usage, billing_state, price_source, currency,
    terminal_at, authority_scope="test_fake",
):
    if (
        not _router_is_sha(attempt_id)
        or not isinstance(run_id, str) or not run_id
        or not isinstance(intent, str) or not intent
        or not isinstance(candidate_id, str) or not candidate_id
        or not isinstance(provider, str) or not provider
        or not _router_is_sha(capability_profile_hash)
        or not _router_is_sha(request_cas_object_id)
        or terminal_outcome not in _VERIFIED_USAGE_TERMINAL_OUTCOMES
        or authority_scope != "test_fake"
        or billing_state not in {"billable", "nonbillable", "unknown"}
    ):
        raise ValueError("verified usage authority identity is invalid")
    if (
        (terminal_outcome == "cancelled")
        != (output_cas_object_id is None)
        or (
            output_cas_object_id is not None
            and not _router_is_sha(output_cas_object_id)
        )
    ):
        raise ValueError("verified usage terminal output is invalid")
    actual = _verified_actual_usage(actual_usage)
    if billing_state == "billable":
        if (
            not isinstance(price_source, str) or not price_source
            or not isinstance(currency, str)
            or re.fullmatch(r"[A-Z]{3}", currency) is None
            or "currency_micros" not in actual
        ):
            raise ValueError("billable verified usage price authority is incomplete")
    elif (
        price_source is not None
        or currency is not None
        or "currency_micros" in actual
    ):
        raise ValueError("unpriced verified usage contains currency authority")
    _semantic_timestamp(terminal_at, "terminal_at")
    actual_json = history_contract_v2.canonical_bytes(actual).decode("utf-8")
    material = {
        "schema_version": "history-verified-usage-authority-v1",
        "attempt_id": attempt_id, "run_id": run_id, "intent": intent,
        "candidate_id": candidate_id, "provider": provider,
        "capability_profile_hash": capability_profile_hash,
        "request_cas_object_id": request_cas_object_id,
        "output_cas_object_id": output_cas_object_id,
        "terminal_outcome": terminal_outcome,
        "actual_usage": actual,
        "billing_state": billing_state,
        "price_source": price_source, "currency": currency,
        "terminal_at": terminal_at, "authority_scope": authority_scope,
    }
    return (
        _semantic_sha("history-verified-usage-authority-v1", material),
        attempt_id, run_id, intent, candidate_id, provider,
        capability_profile_hash, request_cas_object_id, output_cas_object_id,
        terminal_outcome, actual_json, billing_state, price_source, currency,
        terminal_at, authority_scope,
    )


def _verified_usage_authority_row_valid(*values):
    if len(values) != 16:
        return 0
    try:
        stored = tuple(values)
        actual = _router_closed_json(stored[10])
        expected = _verified_usage_authority_values(
            attempt_id=stored[1], run_id=stored[2], intent=stored[3],
            candidate_id=stored[4], provider=stored[5],
            capability_profile_hash=stored[6],
            request_cas_object_id=stored[7],
            output_cas_object_id=stored[8], terminal_outcome=stored[9],
            actual_usage=actual, billing_state=stored[11],
            price_source=stored[12], currency=stored[13],
            terminal_at=stored[14], authority_scope=stored[15],
        )
        return 1 if stored == expected else 0
    except (TypeError, ValueError):
        return 0


_L1_ATTEMPT_FACT_INPUT_FIELDS = frozenset({
    "schema_version", "attempt_id", "ordinal", "previous_attempt_id",
    "run_id", "candidate_id", "intent", "provider",
    "capability_profile_hash", "request_evidence_sha256",
    "result_evidence_sha256", "usage_source", "reserved",
    "usage_authority_sha256", "queue_latency_ms", "run_latency_ms",
    "outcome", "terminal_at",
})


def _l1_usage(value, *, allow_currency):
    required = {"input_tokens", "output_tokens", "provider_usage_units"}
    optional = {"cache_tokens", "currency_micros"}
    if (
        not isinstance(value, dict)
        or not required.issubset(value)
        or set(value).difference(required | optional)
        or any(type(amount) is not int or amount < 0 for amount in value.values())
        or (not allow_currency and "currency_micros" in value)
    ):
        raise ValueError("L1 usage is invalid")
    return copy.deepcopy(value)


def _l1_attempt_id_value(
    *, run_id, candidate_id, intent, ordinal, provider,
    capability_profile_hash, request_evidence_sha256,
    previous_attempt_id, previous_fact_sha256, previous_outcome,
    previous_terminal_at,
):
    previous_terminal = None
    if ordinal == 0:
        if any(value is not None for value in (
            previous_attempt_id, previous_fact_sha256,
            previous_outcome, previous_terminal_at,
        )):
            raise ValueError("initial L1 attempt has previous terminal")
    else:
        if (
            not _router_is_sha(previous_attempt_id)
            or not _router_is_sha(previous_fact_sha256)
            or previous_outcome not in {"success", "failed", "cancelled"}
        ):
            raise ValueError("L1 previous terminal is invalid")
        _semantic_timestamp(previous_terminal_at, "previous_terminal_at")
        previous_terminal = {
            "attempt_id": previous_attempt_id,
            "fact_sha256": previous_fact_sha256,
            "outcome": previous_outcome,
            "terminal_at": previous_terminal_at,
        }
    if (
        not isinstance(run_id, str) or not run_id
        or not isinstance(candidate_id, str) or not candidate_id
        or not isinstance(intent, str) or not intent
        or type(ordinal) is not int or ordinal < 0
        or not isinstance(provider, str) or not provider
        or not _router_is_sha(capability_profile_hash)
        or not _router_is_sha(request_evidence_sha256)
    ):
        raise ValueError("L1 attempt provenance is invalid")
    return _semantic_sha(
        "history-l1-attempt-id-v1",
        {
            "schema_version": "history-l1-attempt-id-v1",
            "run_id": run_id,
            "candidate_id": candidate_id,
            "intent": intent,
            "ordinal": ordinal,
            "provider": provider,
            "capability_profile_hash": capability_profile_hash,
            "request_evidence_sha256": request_evidence_sha256,
            "previous_terminal": previous_terminal,
        },
    )


def _l1_attempt_id_row_valid(
    attempt_id, run_id, candidate_id, intent, ordinal, provider,
    capability_profile_hash, request_evidence_sha256,
    previous_attempt_id, previous_fact_sha256, previous_outcome,
    previous_terminal_at,
):
    try:
        expected = _l1_attempt_id_value(
            run_id=run_id, candidate_id=candidate_id, intent=intent,
            ordinal=ordinal, provider=provider,
            capability_profile_hash=capability_profile_hash,
            request_evidence_sha256=request_evidence_sha256,
            previous_attempt_id=previous_attempt_id,
            previous_fact_sha256=previous_fact_sha256,
            previous_outcome=previous_outcome,
            previous_terminal_at=previous_terminal_at,
        )
        return 1 if attempt_id == expected else 0
    except (TypeError, ValueError):
        return 0


def _l1_attempt_id_for_material(conn, material):
    """Derive an L1 attempt id from frozen provenance and prior terminal."""
    ordinal = material.get("ordinal") if isinstance(material, dict) else None
    previous = None
    if type(ordinal) is int and ordinal > 0:
        previous = conn.execute(
            "SELECT attempt_id,fact_sha256,outcome,terminal_at "
            "FROM audit_l1_attempt_facts_v2 WHERE attempt_id=?",
            (material.get("previous_attempt_id"),),
        ).fetchone()
        if previous is None:
            raise AuditMigrationError("L1 previous terminal is unavailable")
    try:
        return _l1_attempt_id_value(
            run_id=material.get("run_id"),
            candidate_id=material.get("candidate_id"),
            intent=material.get("intent"), ordinal=ordinal,
            provider=material.get("provider"),
            capability_profile_hash=material.get("capability_profile_hash"),
            request_evidence_sha256=material.get("request_evidence_sha256"),
            previous_attempt_id=(
                None if previous is None else previous["attempt_id"]
            ),
            previous_fact_sha256=(
                None if previous is None else previous["fact_sha256"]
            ),
            previous_outcome=(
                None if previous is None else previous["outcome"]
            ),
            previous_terminal_at=(
                None if previous is None else previous["terminal_at"]
            ),
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise AuditMigrationError("L1 attempt provenance is invalid") from exc


def _l1_attempt_identity(material):
    outcome = material.get("outcome")
    ordinal = material.get("ordinal")
    previous = material.get("previous_attempt_id")
    result_evidence = material.get("result_evidence_sha256")
    if (
        material.get("schema_version") != "history-l1-attempt-fact-v2"
        or not _router_is_sha(material.get("attempt_id"))
        or type(ordinal) is not int
        or ordinal < 0
        or (ordinal == 0 and previous is not None)
        or (ordinal > 0 and not _router_is_sha(previous))
        or not isinstance(material.get("run_id"), str)
        or not material["run_id"]
        or not isinstance(material.get("candidate_id"), str)
        or not material["candidate_id"]
        or not isinstance(material.get("intent"), str)
        or not material["intent"]
        or not isinstance(material.get("provider"), str)
        or not material["provider"]
        or not _router_is_sha(material.get("capability_profile_hash"))
        or not _router_is_sha(material.get("request_evidence_sha256"))
        or outcome not in {"success", "failed", "cancelled"}
        or (outcome == "success" and not _router_is_sha(result_evidence))
        or (outcome != "success" and result_evidence is not None)
        or type(material.get("queue_latency_ms")) is not int
        or material["queue_latency_ms"] < 0
        or type(material.get("run_latency_ms")) is not int
        or material["run_latency_ms"] < 0
    ):
        raise ValueError("L1 attempt identity is invalid")
    _semantic_timestamp(material["terminal_at"], "terminal_at")


def _l1_public_material(material):
    if (
        not isinstance(material, dict)
        or set(material) != _L1_ATTEMPT_FACT_INPUT_FIELDS
        or material.get("usage_source") not in {
            "reservation", "verified_actual"
        }
        or (
            material.get("usage_source") == "reservation"
            and material.get("usage_authority_sha256") is not None
        )
        or (
            material.get("usage_source") == "verified_actual"
            and not _router_is_sha(material.get("usage_authority_sha256"))
        )
    ):
        raise ValueError("L1 attempt fact input is not closed")
    _l1_attempt_identity(material)
    return _l1_usage(material["reserved"], allow_currency=False)


def _l1_verified_usage_authority_values(
    material, *, actual_usage, billing_state, price_source, currency,
    route_fact_sha256, final_phase_fact_sha256, source_set_sha256,
    authority_scope="test_fake",
):
    _l1_attempt_identity(material)
    actual = _l1_usage(actual_usage, allow_currency=True)
    if (
        billing_state not in {"billable", "nonbillable", "unknown"}
        or authority_scope != "test_fake"
        or not _router_is_sha(route_fact_sha256)
        or not _router_is_sha(final_phase_fact_sha256)
        or not _router_is_sha(source_set_sha256)
    ):
        raise ValueError("L1 verified usage authority is invalid")
    if billing_state == "billable":
        if (
            not isinstance(price_source, str)
            or not price_source
            or not isinstance(currency, str)
            or re.fullmatch(r"[A-Z]{3}", currency) is None
            or "currency_micros" not in actual
        ):
            raise ValueError("billable L1 usage lacks price authority")
    elif (
        price_source is not None
        or currency is not None
        or "currency_micros" in actual
    ):
        raise ValueError("unpriced L1 usage contains currency authority")
    actual_json = history_contract_v2.canonical_bytes(actual).decode("utf-8")
    authority_material = {
        "schema_version": "history-l1-verified-usage-authority-v1",
        "attempt_id": material["attempt_id"],
        "ordinal": material["ordinal"],
        "previous_attempt_id": material["previous_attempt_id"],
        "run_id": material["run_id"],
        "candidate_id": material["candidate_id"],
        "intent": material["intent"],
        "provider": material["provider"],
        "capability_profile_hash": material["capability_profile_hash"],
        "request_evidence_sha256": material["request_evidence_sha256"],
        "result_evidence_sha256": material["result_evidence_sha256"],
        "terminal_outcome": material["outcome"],
        "actual_usage": actual,
        "billing_state": billing_state,
        "price_source": price_source,
        "currency": currency,
        "terminal_at": material["terminal_at"],
        "route_fact_sha256": route_fact_sha256,
        "final_phase_fact_sha256": final_phase_fact_sha256,
        "source_set_sha256": source_set_sha256,
        "authority_scope": authority_scope,
    }
    return (
        _semantic_sha(
            "history-l1-verified-usage-authority-v1", authority_material
        ),
        material["attempt_id"], material["ordinal"],
        material["previous_attempt_id"], material["run_id"],
        material["candidate_id"], material["intent"], material["provider"],
        material["capability_profile_hash"],
        material["request_evidence_sha256"],
        material["result_evidence_sha256"], material["outcome"],
        actual_json, billing_state, price_source, currency,
        material["terminal_at"], route_fact_sha256,
        final_phase_fact_sha256, source_set_sha256, authority_scope,
    )


def _l1_verified_usage_authority_row_valid(*values):
    if len(values) != 21:
        return 0
    try:
        stored = tuple(values)
        material = {
            "schema_version": "history-l1-attempt-fact-v2",
            "attempt_id": stored[1], "ordinal": stored[2],
            "previous_attempt_id": stored[3], "run_id": stored[4],
            "candidate_id": stored[5], "intent": stored[6],
            "provider": stored[7], "capability_profile_hash": stored[8],
            "request_evidence_sha256": stored[9],
            "result_evidence_sha256": stored[10],
            "queue_latency_ms": 0, "run_latency_ms": 0,
            "outcome": stored[11], "terminal_at": stored[16],
        }
        expected = _l1_verified_usage_authority_values(
            material, actual_usage=_closed_json(stored[12]),
            billing_state=stored[13], price_source=stored[14],
            currency=stored[15], route_fact_sha256=stored[17],
            final_phase_fact_sha256=stored[18],
            source_set_sha256=stored[19], authority_scope=stored[20],
        )
        return 1 if stored == expected else 0
    except (TypeError, ValueError):
        return 0


def _l1_attempt_fact_values(
    material, *, actual_usage, billing_state, price_source, currency,
    route_fact_sha256, final_phase_fact_sha256, source_set_sha256,
):
    reserved = _l1_public_material(material)
    if (
        billing_state not in {"billable", "nonbillable", "unknown"}
        or not _router_is_sha(route_fact_sha256)
        or not _router_is_sha(final_phase_fact_sha256)
        or not _router_is_sha(source_set_sha256)
    ):
        raise ValueError("L1 attempt fact identity is invalid")
    if material["usage_source"] == "reservation":
        if (
            actual_usage is not None
            or billing_state != "unknown"
            or price_source is not None
            or currency is not None
        ):
            raise ValueError("reservation L1 usage cannot claim authority")
        actual = None
        effective = reserved
    else:
        actual = _l1_usage(actual_usage, allow_currency=True)
        effective = actual
    if billing_state == "billable":
        if (
            not isinstance(price_source, str)
            or not price_source
            or not isinstance(currency, str)
            or re.fullmatch(r"[A-Z]{3}", currency) is None
            or "currency_micros" not in effective
        ):
            raise ValueError("billable L1 usage lacks price authority")
    elif (
        price_source is not None
        or currency is not None
        or "currency_micros" in effective
    ):
        raise ValueError("unpriced L1 usage contains currency authority")
    fact_material = {
        "schema_version": material["schema_version"],
        "attempt_id": material["attempt_id"],
        "ordinal": material["ordinal"],
        "previous_attempt_id": material["previous_attempt_id"],
        "run_id": material["run_id"],
        "candidate_id": material["candidate_id"],
        "intent": material["intent"],
        "provider": material["provider"],
        "capability_profile_hash": material["capability_profile_hash"],
        "request_evidence_sha256": material["request_evidence_sha256"],
        "result_evidence_sha256": material["result_evidence_sha256"],
        "usage_source": material["usage_source"],
        "reserved": reserved,
        "usage_authority_sha256": material["usage_authority_sha256"],
        "actual": actual,
        "queue_latency_ms": material["queue_latency_ms"],
        "run_latency_ms": material["run_latency_ms"],
        "outcome": material["outcome"],
        "billing_state": billing_state,
        "price_source": price_source,
        "currency": currency,
        "terminal_at": material["terminal_at"],
        "route_fact_sha256": route_fact_sha256,
        "final_phase_fact_sha256": final_phase_fact_sha256,
        "source_set_sha256": source_set_sha256,
    }
    return (
        material["attempt_id"], material["ordinal"],
        material["previous_attempt_id"], material["run_id"],
        material["candidate_id"], material["intent"], material["provider"],
        material["capability_profile_hash"],
        material["request_evidence_sha256"],
        material["result_evidence_sha256"], material["usage_source"],
        history_contract_v2.canonical_bytes(reserved).decode("utf-8"),
        material["usage_authority_sha256"],
        (
            None if actual is None
            else history_contract_v2.canonical_bytes(actual).decode("utf-8")
        ),
        material["queue_latency_ms"], material["run_latency_ms"],
        material["outcome"], billing_state, price_source, currency,
        _semantic_sha("history-l1-attempt-fact-v2", fact_material),
        material["terminal_at"], route_fact_sha256,
        final_phase_fact_sha256, source_set_sha256,
    )


def _l1_attempt_fact_row_valid(*values):
    if len(values) != 25:
        return 0
    try:
        stored = tuple(values)
        material = {
            "schema_version": "history-l1-attempt-fact-v2",
            "attempt_id": stored[0], "ordinal": stored[1],
            "previous_attempt_id": stored[2], "run_id": stored[3],
            "candidate_id": stored[4], "intent": stored[5],
            "provider": stored[6], "capability_profile_hash": stored[7],
            "request_evidence_sha256": stored[8],
            "result_evidence_sha256": stored[9],
            "usage_source": stored[10], "reserved": _closed_json(stored[11]),
            "usage_authority_sha256": stored[12],
            "queue_latency_ms": stored[14], "run_latency_ms": stored[15],
            "outcome": stored[16], "terminal_at": stored[21],
        }
        expected = _l1_attempt_fact_values(
            material,
            actual_usage=(
                None if stored[13] is None else _closed_json(stored[13])
            ),
            billing_state=stored[17], price_source=stored[18],
            currency=stored[19], route_fact_sha256=stored[22],
            final_phase_fact_sha256=stored[23],
            source_set_sha256=stored[24],
        )
        return 1 if stored == expected else 0
    except (TypeError, ValueError):
        return 0


_ROUTER_ROUND_FIELDS = frozenset({
    "schema_version", "run_id", "batch_id", "intent", "snapshot",
    "candidates", "semantic_policy_profile_id", "risk_policy_sha",
    "risk_slice_policy_sha", "budget_policy_sha", "authority_scope",
})
_ROUTER_SNAPSHOT_FIELDS = frozenset({
    "snapshot_id", "snapshot_hash", "history_as_of_watermark",
    "current_batch_id_namespace", "current_batch_ids_hash",
    "current_batch_ids", "exclusion_policy_sha", "expected_asset_ids_hash",
    "expected_asset_ids",
})
_ROUTER_CANDIDATE_FIELDS = frozenset({
    "candidate_id", "candidate_hash", "raw_artifact_sha", "source_order",
})
_ROUTER_SOURCE_KINDS = frozenset({
    "selection", "l1_observation", "calibration", "qualification",
    "risk_assignment", "dependency_heads", "permanent_request",
})
_ROUTER_SOURCE_COMMON_FIELDS = frozenset({
    "schema_version", "route_round_sha256", "run_id", "batch_id",
    "snapshot_id", "snapshot_hash",
})
_ROUTER_FINAL_AUTHORITY_KEYS = frozenset({
    "router_facts", "risk_slices", "matched_rule_ids", "route",
    "call_l1_model", "dispatch_allowed", "release_authorized",
    "candidate_budget_available", "attempt_budget_available",
})


def _router_host_observation_sha(
    route_round_sha256, run_id, batch_id, snapshot_id, snapshot_hash,
    candidate_ids, selected_candidate_id, observations, created_at,
):
    _semantic_timestamp(created_at, "created_at")
    material = {
        "schema_version": "history-router-host-observation-set-v2",
        "route_round_sha256": route_round_sha256,
        "run_id": run_id,
        "batch_id": batch_id,
        "snapshot_id": snapshot_id,
        "snapshot_hash": snapshot_hash,
        "candidate_ids": candidate_ids,
        "selected_candidate_id": selected_candidate_id,
        "observations": observations,
        "created_at": created_at,
    }
    return _semantic_sha("history-router-host-observation-set-v2", material)


def _router_host_observation_values(
    route_round_sha256, material, observations, created_at,
):
    candidate_ids = [item["candidate_id"] for item in material["candidates"]]
    snapshot = material["snapshot"]
    selected_candidate_id = observations["selected_candidate_id"]
    observation_sha = _router_host_observation_sha(
        route_round_sha256, material["run_id"], material["batch_id"],
        snapshot["snapshot_id"], snapshot["snapshot_hash"], candidate_ids,
        selected_candidate_id, observations, created_at,
    )
    return (
        observation_sha, route_round_sha256, material["run_id"],
        material["batch_id"], snapshot["snapshot_id"],
        snapshot["snapshot_hash"], _semantic_canonical(candidate_ids),
        selected_candidate_id, _semantic_canonical(observations), created_at,
    )


def _router_host_observation_row_valid(*values):
    if len(values) != 10:
        return 0
    try:
        candidate_ids = _router_closed_json(values[6])
        observations = _router_closed_json(values[8])
        if (
            _semantic_canonical(candidate_ids) != values[6]
            or _semantic_canonical(observations) != values[8]
            or observations.get("selected_candidate_id") != values[7]
        ):
            return 0
        expected = _router_host_observation_sha(
            values[1], values[2], values[3], values[4], values[5],
            candidate_ids, values[7], observations, values[9],
        )
        return 1 if values[0] == expected else 0
    except Exception:
        return 0


def _router_host_round_authority_values(
    route_round_sha256, observation_set_sha256, issued_at,
):
    _semantic_timestamp(issued_at, "issued_at")
    material = {
        "schema_version": "history-router-host-round-authority-v2",
        "route_round_sha256": route_round_sha256,
        "observation_set_sha256": observation_set_sha256,
        "authority_scope": "host_production",
        "issued_at": issued_at,
    }
    return (
        _semantic_sha("history-router-host-round-authority-v2", material),
        route_round_sha256, observation_set_sha256, "host_production",
        issued_at,
    )


def _router_host_round_authority_row_valid(*values):
    if len(values) != 5 or values[3] != "host_production":
        return 0
    try:
        return 1 if tuple(values) == _router_host_round_authority_values(
            values[1], values[2], values[4]
        ) else 0
    except Exception:
        return 0


def _router_host_source_authority_values(
    source_sha256, route_round_sha256, source_kind,
    observation_set_sha256, derivation_inputs, issued_at,
):
    _semantic_timestamp(issued_at, "issued_at")
    material = {
        "schema_version": "history-router-host-source-authority-v2",
        "source_sha256": source_sha256,
        "route_round_sha256": route_round_sha256,
        "source_kind": source_kind,
        "observation_set_sha256": observation_set_sha256,
        "derivation_inputs": derivation_inputs,
        "authority_scope": "host_production",
        "issued_at": issued_at,
    }
    return (
        _semantic_sha("history-router-host-source-authority-v2", material),
        source_sha256, route_round_sha256, source_kind,
        observation_set_sha256, _semantic_canonical(derivation_inputs),
        "host_production", issued_at,
    )


def _router_host_source_authority_row_valid(*values):
    if len(values) != 8 or values[6] != "host_production":
        return 0
    try:
        derivation_inputs = _router_closed_json(values[5])
        if _semantic_canonical(derivation_inputs) != values[5]:
            return 0
        return 1 if tuple(values) == _router_host_source_authority_values(
            values[1], values[2], values[3], values[4],
            derivation_inputs, values[7],
        ) else 0
    except Exception:
        return 0


_ROUTER_HOST_L1_PARSER_REVISION = "history-router-host-l1-parser-v1"
_ROUTER_HOST_L1_RAW_FIELDS = frozenset({
    "schema_version", "route_round_sha256", "host_round_authority_sha256",
    "run_id", "batch_id", "intent", "snapshot_id", "snapshot_hash",
    "candidate_id", "candidate_hash", "candidate_raw_artifact_sha256",
    "source_order", "pre_phase_fact_sha256", "comparator_outcome",
    "coverage_state",
})


def _router_host_l1_raw_observation(raw_observation_bytes):
    if type(raw_observation_bytes) is not bytes or not raw_observation_bytes:
        raise AuditMigrationError("router_host_l1_raw_bytes_required")
    try:
        observation = history_contract_v2.parse_json_bytes(
            raw_observation_bytes
        )
        canonical = history_contract_v2.canonical_bytes(observation)
    except history_contract_v2.ContractV2Error as exc:
        raise AuditMigrationError("router_host_l1_observation_invalid") from exc
    if (
        canonical != raw_observation_bytes
        or not isinstance(observation, dict)
        or set(observation) != _ROUTER_HOST_L1_RAW_FIELDS
        or observation.get("schema_version")
            != "history-router-host-l1-observation-v2"
        or any(
            not isinstance(observation.get(name), str)
            or not observation[name]
            for name in ("run_id", "batch_id", "intent", "candidate_id")
        )
        or any(
            not _router_is_sha(observation.get(name))
            for name in (
                "route_round_sha256", "host_round_authority_sha256",
                "snapshot_id", "snapshot_hash", "candidate_hash",
                "candidate_raw_artifact_sha256", "pre_phase_fact_sha256",
            )
        )
        or type(observation.get("source_order")) is not int
        or observation["source_order"] < 0
        or observation.get("comparator_outcome")
            not in {"match", "no_match", "uncertain"}
        or observation.get("coverage_state") != "complete"
    ):
        raise AuditMigrationError("router_host_l1_observation_invalid")
    return observation


def _router_host_l1_comparator_values(raw_observation_bytes, observed_at):
    observation = _router_host_l1_raw_observation(raw_observation_bytes)
    _semantic_timestamp(observed_at, "observed_at")
    raw_sha = hashlib.sha256(raw_observation_bytes).hexdigest()
    material = {
        "schema_version": "history-router-host-l1-comparator-fact-v2",
        "route_round_sha256": observation["route_round_sha256"],
        "host_round_authority_sha256": observation[
            "host_round_authority_sha256"
        ],
        "run_id": observation["run_id"],
        "batch_id": observation["batch_id"],
        "intent": observation["intent"],
        "snapshot_id": observation["snapshot_id"],
        "snapshot_hash": observation["snapshot_hash"],
        "candidate_id": observation["candidate_id"],
        "candidate_hash": observation["candidate_hash"],
        "candidate_raw_artifact_sha256": observation[
            "candidate_raw_artifact_sha256"
        ],
        "source_order": observation["source_order"],
        "pre_phase_fact_sha256": observation["pre_phase_fact_sha256"],
        "parser_revision": _ROUTER_HOST_L1_PARSER_REVISION,
        "raw_comparator_artifact_sha256": raw_sha,
        "comparator_outcome": observation["comparator_outcome"],
        "coverage_state": observation["coverage_state"],
        "observed_at": observed_at,
    }
    return (
        _semantic_sha("history-router-host-l1-comparator-fact-v2", material),
        observation["route_round_sha256"],
        observation["host_round_authority_sha256"], observation["run_id"],
        observation["batch_id"], observation["intent"],
        observation["snapshot_id"], observation["snapshot_hash"],
        observation["candidate_id"], observation["candidate_hash"],
        observation["candidate_raw_artifact_sha256"],
        observation["source_order"], observation["pre_phase_fact_sha256"],
        _ROUTER_HOST_L1_PARSER_REVISION, raw_sha,
        raw_observation_bytes, observation["comparator_outcome"],
        observation["coverage_state"], observed_at,
    )


def _router_host_l1_comparator_row_valid(*values):
    if len(values) != 19:
        return 0
    try:
        raw = values[15]
        if type(raw) is not bytes:
            raw = bytes(raw)
        return 1 if tuple(values) == _router_host_l1_comparator_values(
            raw, values[18]
        ) else 0
    except Exception:
        return 0


def _router_is_sha(value):
    return (
        isinstance(value, str)
        and re.fullmatch(r"[0-9a-f]{64}", value) is not None
    )


def _router_closed_json(text):
    if not isinstance(text, str):
        raise ValueError("router canonical JSON must be text")
    return _closed_json(text if text.endswith("\n") else text + "\n")


def _router_policy_authority(material):
    try:
        host = history_audit_plan._host_runtime_authority()
        if (
            history_audit_plan.runtime_budget_policy_sha(
                host["budget_policy"]
            ) == material["budget_policy_sha"]
            and host["risk_policy_sha"] == material["risk_policy_sha"]
            and host["semantic_policy_profile_id"]
                == material["semantic_policy_profile_id"]
        ):
            return copy.deepcopy(host)
    except (KeyError, TypeError, history_audit_plan.AuditPlanError):
        pass
    authorities = getattr(history_audit_plan, "_TEST_RUNTIME_AUTHORITIES", {})
    matches = []
    for authority in authorities.values():
        try:
            if (
                history_audit_plan.runtime_budget_policy_sha(
                    authority["budget_policy"]
                ) == material["budget_policy_sha"]
                and authority["risk_policy_sha"] == material["risk_policy_sha"]
                and authority["semantic_policy_profile_id"]
                    == material["semantic_policy_profile_id"]
            ):
                matches.append(authority)
        except (KeyError, TypeError, history_audit_plan.AuditPlanError):
            continue
    if not matches:
        raise AuditMigrationError("router_round_policy_mismatch")
    first = matches[0]
    if any(
        history_contract_v2.canonical_bytes(item["budget_policy"])
        != history_contract_v2.canonical_bytes(first["budget_policy"])
        for item in matches[1:]
    ):
        raise AuditMigrationError("router_round_policy_ambiguous")
    return copy.deepcopy(first)


def _router_risk_slice_policy():
    try:
        from lib import history_audit_eval_v2
    except ImportError:
        import history_audit_eval_v2
    policy = copy.deepcopy(history_audit_eval_v2.RISK_SLICE_POLICY_V1)
    return policy, _semantic_sha("history-risk-slice-policy-v1", policy)


def _router_validate_string_set(values, *, name, pattern=None, empty=False):
    if (
        not isinstance(values, list)
        or (not empty and not values)
        or values != sorted(values)
        or len(set(values)) != len(values)
        or any(
            not isinstance(value, str)
            or not value
            or (pattern is not None and re.fullmatch(pattern, value) is None)
            for value in values
        )
    ):
        raise AuditMigrationError(f"router_round_{name}_mismatch")
    return list(values)


def _router_validate_round_material(material):
    if not isinstance(material, dict) or set(material) != _ROUTER_ROUND_FIELDS:
        raise AuditMigrationError("router_round_schema_mismatch")
    if (
        material["schema_version"] != "history-router-round-v1"
        or material["authority_scope"] != "test_fake"
        or any(
            not isinstance(material[name], str) or not material[name]
            for name in (
                "run_id", "batch_id", "intent",
                "semantic_policy_profile_id",
            )
        )
        or any(
            not _router_is_sha(material[name])
            for name in (
                "risk_policy_sha", "risk_slice_policy_sha",
                "budget_policy_sha",
            )
        )
    ):
        raise AuditMigrationError("router_round_schema_mismatch")
    snapshot = material["snapshot"]
    if (
        not isinstance(snapshot, dict)
        or set(snapshot) != _ROUTER_SNAPSHOT_FIELDS
        or not _router_is_sha(snapshot.get("snapshot_id"))
        or not _router_is_sha(snapshot.get("snapshot_hash"))
        or type(snapshot.get("history_as_of_watermark")) is not int
        or snapshot["history_as_of_watermark"] < 0
        or snapshot.get("current_batch_id_namespace")
            != "history-v2-staging-v1"
        or not _router_is_sha(snapshot.get("current_batch_ids_hash"))
        or not _router_is_sha(snapshot.get("exclusion_policy_sha"))
        or not _router_is_sha(snapshot.get("expected_asset_ids_hash"))
    ):
        raise AuditMigrationError("router_round_snapshot_mismatch")
    current_ids = _router_validate_string_set(
        snapshot["current_batch_ids"], name="candidate_set",
        pattern=r"stg-v2-[0-9a-f]{64}",
    )
    expected_ids = _router_validate_string_set(
        snapshot["expected_asset_ids"], name="expected_asset_set", empty=True,
    )
    if (
        history_contract_v2.ordered_set_sha256(
            "history-current-batch-ids-v2", current_ids
        ) != snapshot["current_batch_ids_hash"]
        or history_contract_v2.ordered_set_sha256(
            "history-snapshot-assets-v2", expected_ids
        ) != snapshot["expected_asset_ids_hash"]
    ):
        raise AuditMigrationError("router_round_snapshot_mismatch")
    snapshot_material = {
        "run_id": material["run_id"],
        "batch_id": material["batch_id"],
        "history_as_of_watermark": snapshot["history_as_of_watermark"],
        "current_batch_id_namespace": snapshot[
            "current_batch_id_namespace"
        ],
        "current_batch_ids_hash": snapshot["current_batch_ids_hash"],
        "exclusion_policy_sha": snapshot["exclusion_policy_sha"],
        "expected_asset_ids_hash": snapshot["expected_asset_ids_hash"],
    }
    snapshot_hash = _semantic_sha("history-snapshot-v2", snapshot_material)
    snapshot_id = _semantic_sha(
        "history-snapshot-id-v2",
        {
            "run_id": material["run_id"],
            "batch_id": material["batch_id"],
            "snapshot_hash": snapshot_hash,
        },
    )
    if (
        snapshot["snapshot_hash"] != snapshot_hash
        or snapshot["snapshot_id"] != snapshot_id
    ):
        raise AuditMigrationError("router_round_snapshot_mismatch")
    candidates = material["candidates"]
    if (
        not isinstance(candidates, list)
        or not candidates
        or any(
            not isinstance(candidate, dict)
            or set(candidate) != _ROUTER_CANDIDATE_FIELDS
            for candidate in candidates
        )
    ):
        raise AuditMigrationError("router_round_candidate_mismatch")
    candidate_ids = [candidate["candidate_id"] for candidate in candidates]
    if (
        candidate_ids != current_ids
        or len({candidate["source_order"] for candidate in candidates})
            != len(candidates)
        or any(
            type(candidate["source_order"]) is not int
            or candidate["source_order"] < 0
            or not _router_is_sha(candidate["candidate_hash"])
            or not _router_is_sha(candidate["raw_artifact_sha"])
            or history_audit_plan.runtime_candidate_hash(candidate)
                != candidate["candidate_hash"]
            for candidate in candidates
        )
    ):
        raise AuditMigrationError("router_round_candidate_mismatch")
    authority = _router_policy_authority(material)
    _, slice_sha = _router_risk_slice_policy()
    if slice_sha != material["risk_slice_policy_sha"]:
        raise AuditMigrationError("router_round_policy_mismatch")
    try:
        intent_policy = authority["budget_policy"]["intents"][material["intent"]]
        round_policy = intent_policy["round"]
        candidate_policy = intent_policy["candidate"]
        required_budget_fields = {
            "candidates", "started_attempts", "input_tokens",
            "output_tokens", "provider_usage_units",
        }
        if (
            set(round_policy) != required_budget_fields
            or set(candidate_policy)
                != required_budget_fields.difference({"candidates"})
            or any(type(value) is not int or value < 0 for value in round_policy.values())
            or any(
                type(value) is not int or value < 0
                for value in candidate_policy.values()
            )
        ):
            raise KeyError("invalid policy")
    except (KeyError, TypeError):
        raise AuditMigrationError("router_round_policy_mismatch") from None
    history_contract_v2.canonical_bytes(material)
    return copy.deepcopy(material), authority


def _router_round_sha(material):
    return _semantic_sha("history-router-round-v1", material)


def _router_budget_values(material, authority, route_round_sha, created_at):
    policy = authority["budget_policy"]["intents"][material["intent"]]
    candidate_ids = [item["candidate_id"] for item in material["candidates"]]
    requested = len(candidate_ids)
    round_limit = policy["round"]["candidates"]
    decision = "accepted" if requested <= round_limit else "rejected"
    started_attempts_used = 0
    round_attempt_limit = policy["round"]["started_attempts"]
    candidate_attempt_limit = policy["candidate"]["started_attempts"]
    attempt_available = (
        round_attempt_limit > started_attempts_used
        and candidate_attempt_limit > 0
    )
    usage_root_sha = _semantic_sha(
        "history-router-budget-usage-root-v1",
        {"route_round_sha256": route_round_sha, "started_attempt_ids": []},
    )
    fact = {
        "schema_version": "history-router-budget-fact-v1",
        "route_round_sha256": route_round_sha,
        "candidate_ids": candidate_ids,
        "requested_candidates": requested,
        "round_candidate_limit": round_limit,
        "candidate_budget_decision": decision,
        "started_attempts_used": started_attempts_used,
        "round_started_attempt_limit": round_attempt_limit,
        "candidate_started_attempt_limit": candidate_attempt_limit,
        "attempt_budget_available": attempt_available,
        "usage_root_sha256": usage_root_sha,
        "created_at": created_at,
    }
    fact_sha = _semantic_sha("history-router-budget-fact-v1", fact)
    values = (
        fact_sha, route_round_sha, _semantic_canonical(candidate_ids),
        requested, round_limit, decision, started_attempts_used,
        round_attempt_limit, candidate_attempt_limit, int(attempt_available),
        usage_root_sha, _semantic_canonical(fact), created_at,
    )
    return fact, values


def _router_round_row_valid(*values):
    if len(values) != 13:
        return 0
    try:
        (
            route_sha, run_id, batch_id, intent, snapshot_id, snapshot_hash,
            current_ids_sha, candidate_ids_json, round_json, risk_sha,
            slice_sha, budget_sha, scope,
        ) = values
        material, _ = _router_validate_round_material(
            _router_closed_json(round_json)
        )
        candidate_ids = [item["candidate_id"] for item in material["candidates"]]
        return 1 if (
            route_sha == _router_round_sha(material)
            and (
                run_id, batch_id, intent, snapshot_id, snapshot_hash,
                current_ids_sha, candidate_ids_json, risk_sha, slice_sha,
                budget_sha, scope,
            ) == (
                material["run_id"], material["batch_id"], material["intent"],
                material["snapshot"]["snapshot_id"],
                material["snapshot"]["snapshot_hash"],
                material["snapshot"]["current_batch_ids_hash"],
                _semantic_canonical(candidate_ids), material["risk_policy_sha"],
                material["risk_slice_policy_sha"],
                material["budget_policy_sha"], material["authority_scope"],
            )
        ) else 0
    except Exception:
        return 0


def _router_budget_row_valid(*values):
    if len(values) != 14:
        return 0
    try:
        round_json = values[0]
        stored = tuple(values[1:])
        material, authority = _router_validate_round_material(
            _router_closed_json(round_json)
        )
        route_sha = _router_round_sha(material)
        _, expected = _router_budget_values(
            material, authority, route_sha, stored[-1]
        )
        return 1 if stored == expected else 0
    except Exception:
        return 0


def _router_contains_final_authority(value):
    if isinstance(value, dict):
        if set(value).intersection(_ROUTER_FINAL_AUTHORITY_KEYS):
            return True
        return any(_router_contains_final_authority(item) for item in value.values())
    if isinstance(value, list):
        return any(_router_contains_final_authority(item) for item in value)
    return False


def _router_require_source_identity(material, route_sha, source):
    snapshot = material["snapshot"]
    expected = {
        "route_round_sha256": route_sha,
        "run_id": material["run_id"],
        "batch_id": material["batch_id"],
        "snapshot_id": snapshot["snapshot_id"],
        "snapshot_hash": snapshot["snapshot_hash"],
    }
    if any(source.get(name) != value for name, value in expected.items()):
        raise AuditMigrationError("router_source_identity_mismatch")


def _router_source_members(source, candidate_ids, member_fields):
    members = source.get("members")
    if (
        not isinstance(members, list)
        or [
            member.get("candidate_id") if isinstance(member, dict) else None
            for member in members
        ] != candidate_ids
        or any(
            not isinstance(member, dict) or set(member) != member_fields
            for member in members
        )
    ):
        raise AuditMigrationError("router_source_schema_mismatch")
    return members


def _router_validate_domain_source(material, route_sha, source_kind, source):
    if (
        source_kind not in _ROUTER_SOURCE_KINDS
        or not isinstance(source, dict)
        or _router_contains_final_authority(source)
    ):
        raise AuditMigrationError("router_source_schema_mismatch")
    _router_require_source_identity(material, route_sha, source)
    candidate_ids = [item["candidate_id"] for item in material["candidates"]]
    schema_by_kind = {
        "selection": "history-router-selection-source-v1",
        "l1_observation": "history-router-l1-source-v1",
        "calibration": "history-router-calibration-source-v1",
        "qualification": "history-router-qualification-source-v1",
        "risk_assignment": "history-router-risk-assignment-source-v1",
        "dependency_heads": "history-router-dependency-heads-source-v1",
        "permanent_request": "history-router-permanent-request-source-v1",
    }
    if source.get("schema_version") != schema_by_kind[source_kind]:
        raise AuditMigrationError("router_source_schema_mismatch")
    common = set(_ROUTER_SOURCE_COMMON_FIELDS)
    if source_kind == "selection":
        if set(source) != common | {
            "selected_candidate_id", "candidate_ids", "members"
        } or source.get("candidate_ids") != candidate_ids or (
            source.get("selected_candidate_id") not in candidate_ids
        ):
            raise AuditMigrationError("router_source_identity_mismatch")
        members = _router_source_members(
            source, candidate_ids,
            {"candidate_id", "selection_class", "channel_states"},
        )
        for member in members:
            channels = member["channel_states"]
            if (
                member["selection_class"] not in {"screened", "finalist", "sa"}
                or not isinstance(channels, list)
                or [item.get("channel_id") for item in channels]
                    != ["dense_core", "exact_lineage", "fts"]
                or any(
                    not isinstance(item, dict)
                    or set(item) != {"channel_id", "state"}
                    or item["state"] not in {"complete", "failed", "missing"}
                    for item in channels
                )
            ):
                raise AuditMigrationError("router_source_schema_mismatch")
        selected = next(
            item for item in members
            if item["candidate_id"] == source["selected_candidate_id"]
        )
        if selected["selection_class"] not in {"finalist", "sa"}:
            raise AuditMigrationError("router_source_schema_mismatch")
    elif source_kind == "l1_observation":
        if set(source) != common | {"candidate_ids", "members"} or (
            source.get("candidate_ids") != candidate_ids
        ):
            raise AuditMigrationError("router_source_identity_mismatch")
        members = source.get("members")
        if (
            not isinstance(members, list)
            or [
                item.get("candidate_id") if isinstance(item, dict) else None
                for item in members
            ] != candidate_ids
        ):
            raise AuditMigrationError("router_source_identity_mismatch")
        for member in members:
            kind = member.get("observation_kind") if isinstance(member, dict) else None
            if kind == "pre_l1_skip":
                if set(member) != {
                    "candidate_id", "observation_kind", "skip_reason",
                    "coverage_state", "pre_phase_fact_sha256",
                } or (
                    not isinstance(member["skip_reason"], str)
                    or not member["skip_reason"]
                    or member["coverage_state"] != "not_run"
                    or not _router_is_sha(member["pre_phase_fact_sha256"])
                ):
                    raise AuditMigrationError("router_source_schema_mismatch")
            elif kind == "comparator":
                if set(member) != {
                    "candidate_id", "observation_kind", "comparator_outcome",
                    "coverage_state", "comparator_receipt_sha256",
                } or (
                    not isinstance(member["comparator_outcome"], str)
                    or not member["comparator_outcome"]
                    or member["coverage_state"] != "complete"
                    or not _router_is_sha(member["comparator_receipt_sha256"])
                ):
                    raise AuditMigrationError("router_source_schema_mismatch")
            elif kind == "unavailable":
                if set(member) != {
                    "candidate_id", "observation_kind",
                    "unavailable_reason", "coverage_state",
                    "pre_phase_fact_sha256",
                } or (
                    member["unavailable_reason"]
                        != "comparator_fact_missing"
                    or member["coverage_state"] != "unavailable"
                    or not _router_is_sha(
                        member["pre_phase_fact_sha256"]
                    )
                ):
                    raise AuditMigrationError("router_source_schema_mismatch")
            else:
                raise AuditMigrationError("router_source_schema_mismatch")
    elif source_kind == "calibration":
        if set(source) != common | {
            "semantic_policy_profile_id", "qrels_hash", "calibration_state"
        }:
            raise AuditMigrationError("router_source_schema_mismatch")
        if (
            source["semantic_policy_profile_id"]
                != material["semantic_policy_profile_id"]
            or not _router_is_sha(source["qrels_hash"])
            or source["calibration_state"] not in {"unqualified", "shadow_ready"}
        ):
            raise AuditMigrationError("router_source_identity_mismatch")
    elif source_kind == "qualification":
        if set(source) != common | {
            "semantic_policy_profile_id", "qrels_hash", "qualification_id",
            "lookup_state", "dependency_heads",
        }:
            raise AuditMigrationError("router_source_schema_mismatch")
        try:
            dependencies = _semantic_dependencies(source["dependency_heads"])
        except (TypeError, ValueError) as exc:
            raise AuditMigrationError("router_source_schema_mismatch") from exc
        if (
            source["semantic_policy_profile_id"]
                != material["semantic_policy_profile_id"]
            or not _router_is_sha(source["qrels_hash"])
            or source["lookup_state"] not in {"available", "unavailable"}
            or (
                source["lookup_state"] == "unavailable"
                and source["qualification_id"] is not None
            )
            or (
                source["lookup_state"] == "available"
                and (
                    not isinstance(source["qualification_id"], str)
                    or re.fullmatch(
                        r"semantic-v2-[0-9a-f]{64}",
                        source["qualification_id"],
                    ) is None
                )
            )
            or dependencies != source["dependency_heads"]
        ):
            raise AuditMigrationError("router_source_identity_mismatch")
    elif source_kind == "risk_assignment":
        if set(source) != common | {"candidate_ids", "members"} or (
            source.get("candidate_ids") != candidate_ids
        ):
            raise AuditMigrationError("router_source_identity_mismatch")
        members = _router_source_members(
            source, candidate_ids, {"candidate_id", "assigned_slice_ids"}
        )
        slice_policy, _ = _router_risk_slice_policy()
        allowed = set(slice_policy["allowed_slices"])
        if any(
            not isinstance(member["assigned_slice_ids"], list)
            or member["assigned_slice_ids"] != sorted(member["assigned_slice_ids"])
            or len(set(member["assigned_slice_ids"]))
                != len(member["assigned_slice_ids"])
            or set(member["assigned_slice_ids"]).difference(allowed)
            for member in members
        ):
            raise AuditMigrationError("router_source_schema_mismatch")
    elif source_kind == "dependency_heads":
        if set(source) != common | {"heads", "observed_index_profile_sha256"}:
            raise AuditMigrationError("router_source_schema_mismatch")
        try:
            heads = _semantic_dependencies(source["heads"])
        except (TypeError, ValueError) as exc:
            raise AuditMigrationError("router_source_schema_mismatch") from exc
        if (
            heads != source["heads"]
            or source["observed_index_profile_sha256"] != heads.get("fts")
        ):
            raise AuditMigrationError("router_source_identity_mismatch")
    else:
        if set(source) != common | {"candidate_ids", "members"} or (
            source.get("candidate_ids") != candidate_ids
        ):
            raise AuditMigrationError("router_source_identity_mismatch")
        members = _router_source_members(
            source, candidate_ids,
            {"candidate_id", "request_state", "request_id"},
        )
        if any(
            member["request_state"] not in {"requested", "not_requested"}
            or (
                member["request_state"] == "not_requested"
                and member["request_id"] is not None
            )
            or (
                member["request_state"] == "requested"
                and not _router_is_sha(member["request_id"])
            )
            for member in members
        ):
            raise AuditMigrationError("router_source_schema_mismatch")
    history_contract_v2.canonical_bytes(source)
    return copy.deepcopy(source)


def _router_domain_source_sha(route_sha, source_kind, source):
    return _semantic_sha(
        "history-router-domain-source-v1",
        {
            "route_round_sha256": route_sha,
            "source_kind": source_kind,
            "source": source,
        },
    )


def _router_domain_source_row_valid(*values):
    if len(values) != 5:
        return 0
    try:
        round_json, source_sha, route_sha, source_kind, source_json = values
        material, _ = _router_validate_round_material(
            _router_closed_json(round_json)
        )
        if route_sha != _router_round_sha(material):
            return 0
        source = _router_validate_domain_source(
            material, route_sha, source_kind, _router_closed_json(source_json)
        )
        return 1 if source_sha == _router_domain_source_sha(
            route_sha, source_kind, source
        ) else 0
    except Exception:
        return 0


def _router_round_receipt(round_row, budget_row):
    return {
        "schema_version": "history-router-round-receipt-v1",
        "route_round_sha256": round_row["route_round_sha256"],
        "budget_fact_sha256": budget_row["budget_fact_sha256"],
        "candidate_budget_decision": budget_row["candidate_budget_decision"],
        "attempt_budget_available": bool(budget_row["attempt_budget_available"]),
        "created_at": round_row["created_at"],
    }


def prepare_router_round(conn, round_material, *, created_at=None):
    """Freeze a pre-plan router cohort under an exact registered policy."""
    if conn.in_transaction:
        raise AuditMigrationError("router round preparation requires an idle connection")
    try:
        material, authority = _router_validate_round_material(round_material)
        round_json = _semantic_canonical(material)
        route_sha = _router_round_sha(material)
        candidate_ids = [item["candidate_id"] for item in material["candidates"]]
        guard = _ROUTER_SOURCE_GUARDS.get(id(conn))
        if guard is None or guard["round"] is not None or guard["budget"] is not None:
            raise AuditMigrationError("router round guard is unavailable")
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM audit_router_rounds_v2 "
            "WHERE run_id=? AND batch_id=? AND intent=?",
            (material["run_id"], material["batch_id"], material["intent"]),
        ).fetchone()
        if existing is not None:
            if conn.execute(
                "SELECT 1 FROM audit_router_host_round_authorities_v2 "
                "WHERE route_round_sha256=?",
                (existing["route_round_sha256"],),
            ).fetchone() is not None:
                raise AuditMigrationError(
                    "router_source_test_authority_forbidden"
                )
            if (
                existing["route_round_sha256"] != route_sha
                or existing["round_json"] != round_json
            ):
                raise AuditMigrationError("router_round_identity_mismatch")
            budget = conn.execute(
                "SELECT * FROM audit_router_budget_facts_v2 "
                "WHERE route_round_sha256=?", (route_sha,)
            ).fetchone()
            if budget is None or _router_budget_row_valid(
                round_json, *tuple(budget)
            ) != 1:
                raise AuditMigrationError("router_round_budget_mismatch")
            conn.execute("COMMIT")
            _TEST_ROUTER_ROUND_AUTHORITIES[route_sha] = round_json
            return _router_round_receipt(existing, budget)
        created_at = created_at or _utc_now()
        _semantic_timestamp(created_at, "created_at")
        round_values = (
            route_sha, material["run_id"], material["batch_id"],
            material["intent"], material["snapshot"]["snapshot_id"],
            material["snapshot"]["snapshot_hash"],
            material["snapshot"]["current_batch_ids_hash"],
            _semantic_canonical(candidate_ids), round_json,
            material["risk_policy_sha"], material["risk_slice_policy_sha"],
            material["budget_policy_sha"], material["authority_scope"],
            created_at,
        )
        _, budget_values = _router_budget_values(
            material, authority, route_sha, created_at
        )
        guard["round"] = round_values
        try:
            conn.execute(
                "INSERT INTO audit_router_rounds_v2 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                round_values,
            )
        finally:
            guard["round"] = None
        guard["budget"] = budget_values
        try:
            conn.execute(
                "INSERT INTO audit_router_budget_facts_v2 "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", budget_values,
            )
        finally:
            guard["budget"] = None
        round_row = conn.execute(
            "SELECT * FROM audit_router_rounds_v2 WHERE route_round_sha256=?",
            (route_sha,),
        ).fetchone()
        budget_row = conn.execute(
            "SELECT * FROM audit_router_budget_facts_v2 WHERE route_round_sha256=?",
            (route_sha,),
        ).fetchone()
        conn.execute("COMMIT")
        _TEST_ROUTER_ROUND_AUTHORITIES[route_sha] = round_json
        return _router_round_receipt(round_row, budget_row)
    except Exception:
        guard = _ROUTER_SOURCE_GUARDS.get(id(conn))
        if guard is not None:
            guard["round"] = None
            guard["budget"] = None
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


_ROUTER_HOST_OBSERVATION_FIELDS = frozenset({
    "schema_version", "selected_candidate_id", "members",
})
_ROUTER_HOST_OBSERVATION_MEMBER_FIELDS = frozenset({
    "candidate_id", "selection_class", "channel_states",
    "assigned_slice_ids", "permanent_request_id",
})
_ROUTER_HOST_PREPLAN_CANDIDATE_INPUT_FIELDS = frozenset({
    "candidate_id", "raw_artifact_sha", "source_order",
})


def _router_host_preplan_values(
    run_id, batch_id, intent, snapshot, candidates, records, created_at,
):
    _semantic_timestamp(created_at, "created_at")
    records_sha = history_audit_plan.runtime_snapshot_records_sha(records)
    material = {
        "schema_version": "history-router-host-preplan-batch-v2",
        "run_id": run_id,
        "batch_id": batch_id,
        "intent": intent,
        "snapshot": snapshot,
        "candidates": candidates,
        "records_sha256": records_sha,
        "records": records,
        "created_at": created_at,
    }
    return (
        _semantic_sha("history-router-host-preplan-batch-v2", material),
        run_id, batch_id, intent, _semantic_canonical(snapshot),
        _semantic_canonical(candidates), records_sha,
        _semantic_canonical(records), created_at,
    )


def _router_host_preplan_row_valid(*values):
    if len(values) != 9:
        return 0
    try:
        snapshot = _router_closed_json(values[4])
        candidates = _router_closed_json(values[5])
        records = history_audit_plan.runtime_snapshot_records(
            _router_closed_json(values[7])
        )
        if (
            not isinstance(snapshot, dict)
            or set(snapshot) != _ROUTER_SNAPSHOT_FIELDS
            or not isinstance(candidates, list)
            or not candidates
            or any(
                not isinstance(candidate, dict)
                or set(candidate) != _ROUTER_CANDIDATE_FIELDS
                or history_audit_plan.runtime_candidate_hash(candidate)
                    != candidate["candidate_hash"]
                for candidate in candidates
            )
            or [item["candidate_id"] for item in candidates]
                != snapshot["current_batch_ids"]
            or values[6]
                != history_audit_plan.runtime_snapshot_records_sha(records)
            or sorted(item["item_id"] for item in records)
                != snapshot["expected_asset_ids"]
        ):
            return 0
        expected = _router_host_preplan_values(
            values[1], values[2], values[3], snapshot, candidates,
            records, values[8],
        )
        return 1 if tuple(values) == expected else 0
    except Exception:
        return 0


def record_host_router_preplan(
    conn, *, run_id, batch_id, intent, history_as_of_watermark,
    exclusion_policy_sha, records, candidates, created_at=None,
):
    """Freeze identity/raw pre-plan facts before any manifest or L2 plan."""
    if conn.in_transaction:
        raise AuditMigrationError(
            "host router preplan requires an idle connection"
        )
    if (
        any(not isinstance(value, str) or not value for value in (
            run_id, batch_id, intent,
        ))
        or type(history_as_of_watermark) is not int
        or history_as_of_watermark < 0
        or not _router_is_sha(exclusion_policy_sha)
        or not isinstance(candidates, list)
        or not candidates
        or any(
            not isinstance(candidate, dict)
            or set(candidate)
                != _ROUTER_HOST_PREPLAN_CANDIDATE_INPUT_FIELDS
            for candidate in candidates
        )
    ):
        raise AuditMigrationError("router_host_preplan_schema_mismatch")
    try:
        frozen_records = history_audit_plan.runtime_snapshot_records(records)
    except history_audit_plan.AuditPlanError as exc:
        raise AuditMigrationError("router_host_preplan_schema_mismatch") from exc
    normalized_candidates = []
    for candidate in candidates:
        full = {
            "candidate_id": candidate["candidate_id"],
            "candidate_hash": "",
            "raw_artifact_sha": candidate["raw_artifact_sha"],
            "source_order": candidate["source_order"],
        }
        try:
            full["candidate_hash"] = history_audit_plan.runtime_candidate_hash(
                full
            )
        except history_audit_plan.AuditPlanError as exc:
            raise AuditMigrationError(
                "router_host_preplan_candidate_mismatch"
            ) from exc
        normalized_candidates.append(full)
    normalized_candidates.sort(key=lambda item: item["candidate_id"])
    candidate_ids = [item["candidate_id"] for item in normalized_candidates]
    if (
        len(set(candidate_ids)) != len(candidate_ids)
        or len({item["source_order"] for item in normalized_candidates})
            != len(normalized_candidates)
    ):
        raise AuditMigrationError("router_host_preplan_candidate_mismatch")
    expected_ids = sorted(item["item_id"] for item in frozen_records)
    current_hash = history_contract_v2.ordered_set_sha256(
        "history-current-batch-ids-v2", candidate_ids
    )
    expected_hash = history_contract_v2.ordered_set_sha256(
        "history-snapshot-assets-v2", expected_ids
    )
    snapshot_material = {
        "run_id": run_id,
        "batch_id": batch_id,
        "history_as_of_watermark": history_as_of_watermark,
        "current_batch_id_namespace": "history-v2-staging-v1",
        "current_batch_ids_hash": current_hash,
        "exclusion_policy_sha": exclusion_policy_sha,
        "expected_asset_ids_hash": expected_hash,
    }
    snapshot_hash = _semantic_sha("history-snapshot-v2", snapshot_material)
    snapshot = {
        "snapshot_id": _semantic_sha(
            "history-snapshot-id-v2",
            {
                "run_id": run_id,
                "batch_id": batch_id,
                "snapshot_hash": snapshot_hash,
            },
        ),
        "snapshot_hash": snapshot_hash,
        "history_as_of_watermark": history_as_of_watermark,
        "current_batch_id_namespace": "history-v2-staging-v1",
        "current_batch_ids_hash": current_hash,
        "current_batch_ids": candidate_ids,
        "exclusion_policy_sha": exclusion_policy_sha,
        "expected_asset_ids_hash": expected_hash,
        "expected_asset_ids": expected_ids,
    }
    created_at = created_at or _utc_now()
    values = _router_host_preplan_values(
        run_id, batch_id, intent, snapshot, normalized_candidates,
        frozen_records, created_at,
    )
    guard = _ROUTER_SOURCE_GUARDS.get(id(conn))
    if guard is None or guard["host_preplan"] is not None:
        raise AuditMigrationError("host router preplan guard is unavailable")
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT * FROM audit_router_host_preplan_batches_v2 "
            "WHERE run_id=? AND batch_id=? AND intent=?",
            (run_id, batch_id, intent),
        ).fetchone()
        if existing is not None:
            replay_values = _router_host_preplan_values(
                run_id, batch_id, intent, snapshot, normalized_candidates,
                frozen_records, existing["created_at"],
            )
            if tuple(existing) != replay_values:
                raise AuditMigrationError(
                    "router_host_preplan_identity_mismatch"
                )
            conn.execute("COMMIT")
            values = replay_values
        else:
            guard["host_preplan"] = values
            try:
                conn.execute(
                    "INSERT INTO audit_router_host_preplan_batches_v2 "
                    "VALUES(?,?,?,?,?,?,?,?,?)", values,
                )
            finally:
                guard["host_preplan"] = None
            conn.execute("COMMIT")
        return {
            "schema_version": "history-router-host-preplan-batch-v2",
            "preplan_sha256": values[0],
            "run_id": run_id,
            "batch_id": batch_id,
            "intent": intent,
            "snapshot": snapshot,
            "candidates": copy.deepcopy(normalized_candidates),
            "records_sha256": values[6],
            "created_at": values[8],
        }
    except Exception:
        guard["host_preplan"] = None
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _router_host_round_material(conn, run_id, batch_id, intent):
    if any(not isinstance(value, str) or not value for value in (
        run_id, batch_id, intent,
    )):
        raise AuditMigrationError("router_host_identity_mismatch")
    preplan = conn.execute(
        "SELECT * FROM audit_router_host_preplan_batches_v2 "
        "WHERE run_id=? AND batch_id=? AND intent=?",
        (run_id, batch_id, intent),
    ).fetchone()
    if preplan is not None:
        if _router_host_preplan_row_valid(*tuple(preplan)) != 1:
            raise AuditMigrationError("router_host_preplan_identity_mismatch")
        try:
            snapshot = _router_closed_json(preplan["snapshot_json"])
            candidates = _router_closed_json(preplan["candidates_json"])
            records = history_audit_plan.runtime_snapshot_records(
                _router_closed_json(preplan["records_json"])
            )
        except (
            TypeError, ValueError, history_audit_plan.AuditPlanError,
        ) as exc:
            raise AuditMigrationError(
                "router_host_preplan_identity_mismatch"
            ) from exc
        if (
            preplan["records_sha256"]
                != history_audit_plan.runtime_snapshot_records_sha(records)
            or sorted(item["item_id"] for item in records)
                != snapshot["expected_asset_ids"]
        ):
            raise AuditMigrationError("router_host_preplan_identity_mismatch")
        authority = history_audit_plan._host_runtime_authority()
        _, slice_sha = _router_risk_slice_policy()
        material = {
            "schema_version": "history-router-round-v1",
            "run_id": run_id,
            "batch_id": batch_id,
            "intent": intent,
            "snapshot": snapshot,
            "candidates": candidates,
            "semantic_policy_profile_id": authority[
                "semantic_policy_profile_id"
            ],
            "risk_policy_sha": authority["risk_policy_sha"],
            "risk_slice_policy_sha": slice_sha,
            "budget_policy_sha": history_audit_plan.runtime_budget_policy_sha(
                authority["budget_policy"]
            ),
            "authority_scope": "test_fake",
        }
        validated, validated_authority = _router_validate_round_material(material)
        if (
            validated_authority.get("private_test_authority") is True
            or validated_authority["risk_policy_sha"]
                != authority["risk_policy_sha"]
        ):
            raise AuditMigrationError("router_host_policy_authority_unavailable")
        return validated, authority
    snapshots = conn.execute(
        "SELECT * FROM audit_snapshots WHERE run_id=? AND batch_id=?",
        (run_id, batch_id),
    ).fetchall()
    if len(snapshots) != 1:
        raise AuditMigrationError("router_host_snapshot_authority_unavailable")
    snapshot_row = snapshots[0]
    batch_set = conn.execute(
        "SELECT * FROM audit_snapshot_batch_sets WHERE snapshot_id=? "
        "AND run_id=? AND batch_id=?",
        (snapshot_row["snapshot_id"], run_id, batch_id),
    ).fetchone()
    records_row = conn.execute(
        "SELECT records_sha,records_json FROM audit_l2_snapshot_records_v2 "
        "WHERE snapshot_id=?",
        (snapshot_row["snapshot_id"],),
    ).fetchone()
    if batch_set is None or records_row is None:
        raise AuditMigrationError("router_host_snapshot_authority_unavailable")
    try:
        candidate_ids = _router_closed_json(batch_set["member_ids_json"])
        records = history_audit_plan.runtime_snapshot_records(
            _router_closed_json(records_row["records_json"])
        )
    except (
        TypeError, ValueError, history_audit_plan.AuditPlanError,
    ) as exc:
        raise AuditMigrationError("router_host_snapshot_identity_mismatch") from exc
    expected_asset_ids = sorted(item["item_id"] for item in records)
    if (
        candidate_ids != sorted(candidate_ids)
        or len(set(candidate_ids)) != len(candidate_ids)
        or batch_set["member_count"] != len(candidate_ids)
        or batch_set["current_batch_ids_hash"]
            != snapshot_row["current_batch_ids_hash"]
        or history_contract_v2.ordered_set_sha256(
            "history-current-batch-ids-v2", candidate_ids
        ) != snapshot_row["current_batch_ids_hash"]
        or records_row["records_sha"]
            != history_audit_plan.runtime_snapshot_records_sha(records)
        or history_contract_v2.ordered_set_sha256(
            "history-snapshot-assets-v2", expected_asset_ids
        ) != snapshot_row["expected_asset_ids_hash"]
    ):
        raise AuditMigrationError("router_host_snapshot_identity_mismatch")
    staging_rows = conn.execute(
        """
        SELECT staging.*,authority.authority_sha256,
               authority.authority_kind,authority.issued_at
        FROM audit_batch_staging staging
        JOIN audit_batch_staging_authorities_v2 authority
          ON authority.staging_candidate_id=staging.staging_candidate_id
        WHERE staging.run_id=? AND staging.batch_id=?
        ORDER BY staging.staging_candidate_id
        """,
        (run_id, batch_id),
    ).fetchall()
    if [row["staging_candidate_id"] for row in staging_rows] != candidate_ids:
        raise AuditMigrationError("router_host_candidate_authority_unavailable")
    candidates = []
    for row in staging_rows:
        if (
            row["authority_kind"] != "host_issued"
            or row["issued_at"] != row["created_at"]
            or row["authority_sha256"] != batch_staging_authority_sha256(
                row["staging_candidate_id"], run_id, batch_id,
                row["candidate_hash"], row["raw_artifact_sha"],
                row["source_order"], "host_issued", row["created_at"],
            )
        ):
            raise AuditMigrationError(
                "router_host_candidate_authority_unavailable"
            )
        candidates.append({
            "candidate_id": row["staging_candidate_id"],
            "candidate_hash": row["candidate_hash"],
            "raw_artifact_sha": row["raw_artifact_sha"],
            "source_order": row["source_order"],
        })
    authority = history_audit_plan._host_runtime_authority()
    _, slice_sha = _router_risk_slice_policy()
    material = {
        "schema_version": "history-router-round-v1",
        "run_id": run_id,
        "batch_id": batch_id,
        "intent": intent,
        "snapshot": {
            "snapshot_id": snapshot_row["snapshot_id"],
            "snapshot_hash": snapshot_row["snapshot_hash"],
            "history_as_of_watermark": snapshot_row[
                "history_as_of_watermark"
            ],
            "current_batch_id_namespace": snapshot_row[
                "current_batch_id_namespace"
            ],
            "current_batch_ids_hash": snapshot_row[
                "current_batch_ids_hash"
            ],
            "current_batch_ids": candidate_ids,
            "exclusion_policy_sha": snapshot_row["exclusion_policy_sha"],
            "expected_asset_ids_hash": snapshot_row[
                "expected_asset_ids_hash"
            ],
            "expected_asset_ids": expected_asset_ids,
        },
        "candidates": candidates,
        "semantic_policy_profile_id": authority[
            "semantic_policy_profile_id"
        ],
        "risk_policy_sha": authority["risk_policy_sha"],
        "risk_slice_policy_sha": slice_sha,
        "budget_policy_sha": history_audit_plan.runtime_budget_policy_sha(
            authority["budget_policy"]
        ),
        "authority_scope": "test_fake",
    }
    validated, validated_authority = _router_validate_round_material(material)
    if (
        validated_authority.get("private_test_authority") is True
        or validated_authority["risk_policy_sha"] != authority["risk_policy_sha"]
    ):
        raise AuditMigrationError("router_host_policy_authority_unavailable")
    return validated, authority


def _router_validate_host_observations(material, raw_observations):
    if (
        not isinstance(raw_observations, dict)
        or set(raw_observations) != _ROUTER_HOST_OBSERVATION_FIELDS
        or raw_observations.get("schema_version")
            != "history-router-host-observations-v1"
    ):
        raise AuditMigrationError("router_host_observation_schema_mismatch")
    candidate_ids = [item["candidate_id"] for item in material["candidates"]]
    members = raw_observations.get("members")
    if (
        not isinstance(members, list)
        or [
            member.get("candidate_id") if isinstance(member, dict) else None
            for member in members
        ] != candidate_ids
        or raw_observations.get("selected_candidate_id") not in candidate_ids
    ):
        raise AuditMigrationError("router_host_observation_cohort_mismatch")
    slice_policy, _ = _router_risk_slice_policy()
    allowed_slices = set(slice_policy["allowed_slices"])
    selected_class_ids = []
    for member in members:
        if set(member) != _ROUTER_HOST_OBSERVATION_MEMBER_FIELDS:
            raise AuditMigrationError("router_host_observation_schema_mismatch")
        channels = member["channel_states"]
        slices = member["assigned_slice_ids"]
        if (
            member["selection_class"] not in {"finalist", "sa", "screened"}
            or not isinstance(channels, list)
            or [item.get("channel_id") for item in channels]
                != ["dense_core", "exact_lineage", "fts"]
            or any(
                not isinstance(item, dict)
                or set(item) != {"channel_id", "state"}
                or item["state"] not in {"complete", "failed", "missing"}
                for item in channels
            )
            or not isinstance(slices, list)
            or slices != sorted(slices)
            or len(set(slices)) != len(slices)
            or set(slices).difference(allowed_slices)
            or (
                member["permanent_request_id"] is not None
                and not _router_is_sha(member["permanent_request_id"])
            )
        ):
            raise AuditMigrationError("router_host_observation_schema_mismatch")
        if member["selection_class"] in {"finalist", "sa"}:
            selected_class_ids.append(member["candidate_id"])
    if selected_class_ids != [raw_observations["selected_candidate_id"]]:
        raise AuditMigrationError("router_host_observation_cohort_mismatch")
    history_contract_v2.canonical_bytes(raw_observations)
    return copy.deepcopy(raw_observations)


def prepare_host_router_round(
    conn, *, run_id, batch_id, intent, raw_observations, created_at=None,
):
    """Derive one host production router round from durable pre-plan facts."""
    if conn.in_transaction:
        raise AuditMigrationError(
            "host router round preparation requires an idle connection"
        )
    material, authority = _router_host_round_material(
        conn, run_id, batch_id, intent
    )
    observations = _router_validate_host_observations(
        material, raw_observations
    )
    created_at = created_at or _utc_now()
    _semantic_timestamp(created_at, "created_at")
    round_json = _semantic_canonical(material)
    route_sha = _router_round_sha(material)
    candidate_ids = [item["candidate_id"] for item in material["candidates"]]
    round_values = (
        route_sha, run_id, batch_id, intent,
        material["snapshot"]["snapshot_id"],
        material["snapshot"]["snapshot_hash"],
        material["snapshot"]["current_batch_ids_hash"],
        _semantic_canonical(candidate_ids), round_json,
        material["risk_policy_sha"], material["risk_slice_policy_sha"],
        material["budget_policy_sha"], "test_fake", created_at,
    )
    _, budget_values = _router_budget_values(
        material, authority, route_sha, created_at
    )
    observation_values = _router_host_observation_values(
        route_sha, material, observations, created_at
    )
    host_round_values = _router_host_round_authority_values(
        route_sha, observation_values[0], created_at
    )
    guard = _ROUTER_SOURCE_GUARDS.get(id(conn))
    if guard is None or any(guard[name] is not None for name in (
        "round", "budget", "host_observation", "host_round",
    )):
        raise AuditMigrationError("host router round guard is unavailable")
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT * FROM audit_router_rounds_v2 WHERE run_id=? "
            "AND batch_id=? AND intent=?",
            (run_id, batch_id, intent),
        ).fetchone()
        if existing is not None:
            observation_row = conn.execute(
                "SELECT * FROM audit_router_host_observation_sets_v2 "
                "WHERE route_round_sha256=?", (route_sha,),
            ).fetchone()
            host_round_row = conn.execute(
                "SELECT * FROM audit_router_host_round_authorities_v2 "
                "WHERE route_round_sha256=?", (route_sha,),
            ).fetchone()
            budget_row = conn.execute(
                "SELECT * FROM audit_router_budget_facts_v2 "
                "WHERE route_round_sha256=?", (route_sha,),
            ).fetchone()
            if (
                existing["route_round_sha256"] != route_sha
                or existing["round_json"] != round_json
                or observation_row is None
                or host_round_row is None
                or budget_row is None
                or observation_row["observations_json"]
                    != _semantic_canonical(observations)
                or host_round_row["observation_set_sha256"]
                    != observation_row["observation_set_sha256"]
                or host_round_row["authority_scope"] != "host_production"
                or _router_budget_row_valid(
                    round_json, *tuple(budget_row)
                ) != 1
            ):
                raise AuditMigrationError("router_host_round_identity_mismatch")
            conn.execute("COMMIT")
            result = _router_round_receipt(existing, budget_row)
            result.update({
                "observation_set_sha256": observation_row[
                    "observation_set_sha256"
                ],
                "host_round_authority_sha256": host_round_row[
                    "authority_sha256"
                ],
                "authority_scope": "host_production",
            })
            return result
        guard["round"] = round_values
        try:
            conn.execute(
                "INSERT INTO audit_router_rounds_v2 "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", round_values,
            )
        finally:
            guard["round"] = None
        guard["budget"] = budget_values
        try:
            conn.execute(
                "INSERT INTO audit_router_budget_facts_v2 "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", budget_values,
            )
        finally:
            guard["budget"] = None
        guard["host_observation"] = observation_values
        try:
            conn.execute(
                "INSERT INTO audit_router_host_observation_sets_v2 "
                "VALUES(?,?,?,?,?,?,?,?,?,?)", observation_values,
            )
        finally:
            guard["host_observation"] = None
        guard["host_round"] = host_round_values
        try:
            conn.execute(
                "INSERT INTO audit_router_host_round_authorities_v2 "
                "VALUES(?,?,?,?,?)", host_round_values,
            )
        finally:
            guard["host_round"] = None
        conn.execute("COMMIT")
        round_row = conn.execute(
            "SELECT * FROM audit_router_rounds_v2 "
            "WHERE route_round_sha256=?", (route_sha,),
        ).fetchone()
        budget_row = conn.execute(
            "SELECT * FROM audit_router_budget_facts_v2 "
            "WHERE route_round_sha256=?", (route_sha,),
        ).fetchone()
        result = _router_round_receipt(round_row, budget_row)
        result.update({
            "observation_set_sha256": observation_values[0],
            "host_round_authority_sha256": host_round_values[0],
            "authority_scope": "host_production",
        })
        return result
    except Exception:
        for name in (
            "round", "budget", "host_observation", "host_round",
        ):
            guard[name] = None
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _require_host_router_pre_l1_source_authority(
    conn, route_round_sha256, observation_set_sha256,
):
    source_set = conn.execute(
        "SELECT * FROM audit_router_source_sets_v2 "
        "WHERE route_round_sha256=? AND phase='pre_l1'",
        (route_round_sha256,),
    ).fetchone()
    if source_set is None:
        raise AuditMigrationError("router_host_pre_l1_phase_unavailable")
    try:
        refs = _router_closed_json(source_set["source_refs_json"])
    except (TypeError, ValueError) as exc:
        raise AuditMigrationError(
            "router_host_pre_l1_source_authority_unavailable"
        ) from exc
    expected_kinds = set(_ROUTER_SOURCE_KINDS).difference({"l1_observation"})
    if set(refs) != expected_kinds or len(refs) != len(expected_kinds):
        raise AuditMigrationError(
            "router_host_pre_l1_source_authority_unavailable"
        )
    dependency_heads = None
    for source_kind in sorted(expected_kinds):
        source = conn.execute(
            "SELECT * FROM audit_router_domain_sources_v2 "
            "WHERE route_round_sha256=? AND source_kind=?",
            (route_round_sha256, source_kind),
        ).fetchone()
        authority = conn.execute(
            "SELECT * FROM audit_router_host_source_authorities_v2 "
            "WHERE route_round_sha256=? AND source_kind=?",
            (route_round_sha256, source_kind),
        ).fetchone()
        if (
            source is None
            or authority is None
            or source["source_sha256"] != refs[source_kind]
            or authority["source_sha256"] != source["source_sha256"]
            or authority["observation_set_sha256"]
                != observation_set_sha256
            or _router_host_source_authority_row_valid(
                *tuple(authority)
            ) != 1
        ):
            raise AuditMigrationError(
                "router_host_pre_l1_source_authority_unavailable"
            )
        if source_kind == "dependency_heads":
            try:
                dependency_heads = _router_closed_json(
                    source["source_json"]
                )["heads"]
            except (KeyError, TypeError, ValueError) as exc:
                raise AuditMigrationError(
                    "router_host_pre_l1_source_authority_unavailable"
                ) from exc
    if (
        dependency_heads is None
        or _current_semantic_dependency_heads(conn, dependency_heads)
            != dependency_heads
    ):
        raise AuditMigrationError("router_source_dependency_drift")
    return source_set


def record_host_router_l1_observation(
    conn, *, route_round_sha256, candidate_id, raw_observation_bytes,
):
    """Persist exact raw comparator bytes under the host production round."""
    if conn.in_transaction:
        raise AuditMigrationError(
            "host router L1 observation requires an idle connection"
        )
    if (
        not _router_is_sha(route_round_sha256)
        or not isinstance(candidate_id, str)
        or not candidate_id
    ):
        raise AuditMigrationError("router_host_l1_identity_mismatch")
    observation = _router_host_l1_raw_observation(raw_observation_bytes)
    if (
        observation["route_round_sha256"] != route_round_sha256
        or observation["candidate_id"] != candidate_id
    ):
        raise AuditMigrationError("router_host_l1_identity_mismatch")
    guard = _ROUTER_SOURCE_GUARDS.get(id(conn))
    if guard is None or guard["host_l1_fact"] is not None:
        raise AuditMigrationError("host router L1 fact guard is unavailable")
    conn.execute("BEGIN IMMEDIATE")
    try:
        round_row = conn.execute(
            "SELECT * FROM audit_router_rounds_v2 "
            "WHERE route_round_sha256=?", (route_round_sha256,),
        ).fetchone()
        host_round = conn.execute(
            "SELECT * FROM audit_router_host_round_authorities_v2 "
            "WHERE route_round_sha256=?", (route_round_sha256,),
        ).fetchone()
        observation_row = conn.execute(
            "SELECT * FROM audit_router_host_observation_sets_v2 "
            "WHERE route_round_sha256=?", (route_round_sha256,),
        ).fetchone()
        pre = conn.execute(
            "SELECT * FROM audit_router_phase_facts_v2 "
            "WHERE phase_fact_sha256=? AND route_round_sha256=? "
            "AND phase='pre_l1' AND candidate_id=?",
            (
                observation["pre_phase_fact_sha256"],
                route_round_sha256, candidate_id,
            ),
        ).fetchone()
        if (
            round_row is None
            or host_round is None
            or observation_row is None
            or pre is None
            or pre["call_l1_model"] != 1
        ):
            raise AuditMigrationError(
                "router_host_l1_authority_unavailable"
            )
        try:
            material, authority = _router_validate_round_material(
                _router_closed_json(round_row["round_json"])
            )
        except (TypeError, ValueError) as exc:
            raise AuditMigrationError(
                "router_host_l1_authority_unavailable"
            ) from exc
        candidate = next((
            item for item in material["candidates"]
            if item["candidate_id"] == candidate_id
        ), None)
        snapshot = material["snapshot"]
        expected_identity = {
            "route_round_sha256": route_round_sha256,
            "host_round_authority_sha256": host_round["authority_sha256"],
            "run_id": material["run_id"],
            "batch_id": material["batch_id"],
            "intent": material["intent"],
            "snapshot_id": snapshot["snapshot_id"],
            "snapshot_hash": snapshot["snapshot_hash"],
            "candidate_id": candidate_id,
            "candidate_hash": None if candidate is None else candidate[
                "candidate_hash"
            ],
            "candidate_raw_artifact_sha256": (
                None if candidate is None else candidate["raw_artifact_sha"]
            ),
            "source_order": None if candidate is None else candidate[
                "source_order"
            ],
            "pre_phase_fact_sha256": pre["phase_fact_sha256"],
        }
        if (
            authority.get("private_test_authority") is True
            or _router_round_sha(material) != route_round_sha256
            or _router_host_round_authority_row_valid(
                *tuple(host_round)
            ) != 1
            or _router_host_observation_row_valid(
                *tuple(observation_row)
            ) != 1
            or host_round["observation_set_sha256"]
                != observation_row["observation_set_sha256"]
            or candidate is None
            or any(
                observation[name] != value
                for name, value in expected_identity.items()
            )
        ):
            raise AuditMigrationError("router_host_l1_identity_mismatch")
        _require_host_router_pre_l1_source_authority(
            conn, route_round_sha256,
            observation_row["observation_set_sha256"],
        )
        existing = conn.execute(
            "SELECT * FROM audit_router_host_l1_comparator_facts_v2 "
            "WHERE route_round_sha256=? AND candidate_id=?",
            (route_round_sha256, candidate_id),
        ).fetchone()
        if existing is not None:
            replay = _router_host_l1_comparator_values(
                raw_observation_bytes, existing["observed_at"]
            )
            if tuple(existing) != replay:
                raise AuditMigrationError(
                    "router_host_l1_observation_conflict"
                )
            conn.execute("COMMIT")
            values = replay
        else:
            if conn.execute(
                "SELECT 1 FROM audit_router_domain_sources_v2 "
                "WHERE route_round_sha256=? "
                "AND source_kind='l1_observation'",
                (route_round_sha256,),
            ).fetchone() is not None:
                raise AuditMigrationError(
                    "router_host_l1_source_already_final"
                )
            values = _router_host_l1_comparator_values(
                raw_observation_bytes, _utc_now()
            )
            guard["host_l1_fact"] = values
            try:
                conn.execute(
                    "INSERT INTO audit_router_host_l1_comparator_facts_v2 "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values,
                )
            finally:
                guard["host_l1_fact"] = None
            conn.execute("COMMIT")
        return {
            "schema_version": "history-router-host-l1-comparator-receipt-v2",
            "route_round_sha256": route_round_sha256,
            "candidate_id": candidate_id,
            "pre_phase_fact_sha256": values[12],
            "raw_comparator_artifact_sha256": values[14],
            "comparator_fact_sha256": values[0],
            "observed_at": values[18],
            "authority_scope": "host_production",
        }
    except Exception:
        guard["host_l1_fact"] = None
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _router_host_l1_receipt(route_round_sha256, values):
    return {
        "schema_version": "history-router-host-l1-comparator-receipt-v2",
        "route_round_sha256": route_round_sha256,
        "candidate_id": values[8],
        "pre_phase_fact_sha256": values[12],
        "raw_comparator_artifact_sha256": values[14],
        "comparator_fact_sha256": values[0],
        "observed_at": values[18],
        "authority_scope": "host_production",
    }


def _insert_host_router_l1_comparator_fact(conn, values):
    conn.execute(
        "INSERT INTO audit_router_host_l1_comparator_facts_v2 "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values,
    )


def verify_host_router_prepare_receipt(conn, receipt):
    """Verify CLI receipt authority against the durable pre-L1 chain."""
    mismatch = "router_host_prepare_receipt_mismatch"
    schema = "history-router-host-cli-prepare-receipt-v1"
    fields = {
        "schema_version", "authority_scope", "input_sha256",
        "preplan_sha256", "route_round_sha256",
        "observation_set_sha256", "host_round_authority_sha256",
        "pre_l1_source_set_sha256", "run_id", "batch_id", "intent",
        "snapshot_id", "snapshot_hash", "candidates", "receipt_sha256",
    }
    candidate_fields = {
        "candidate_id", "candidate_hash", "raw_artifact_sha",
        "source_order", "pre_phase_fact_sha256", "call_l1_model",
    }
    try:
        if not isinstance(receipt, dict) or set(receipt) != fields:
            raise AuditMigrationError(mismatch)
        material = copy.deepcopy(receipt)
        receipt_sha = material.pop("receipt_sha256")
        if (
            receipt["schema_version"] != schema
            or receipt["authority_scope"] != "host_production"
            or not _router_is_sha(receipt_sha)
            or receipt_sha != history_contract_v2.framed_sha256(
                schema, history_contract_v2.canonical_bytes(material)
            )
            or any(
                not _router_is_sha(receipt[name])
                for name in (
                    "input_sha256", "preplan_sha256",
                    "route_round_sha256", "observation_set_sha256",
                    "host_round_authority_sha256",
                    "pre_l1_source_set_sha256", "snapshot_id",
                    "snapshot_hash",
                )
            )
            or any(
                not isinstance(receipt[name], str) or not receipt[name]
                for name in ("run_id", "batch_id", "intent")
            )
            or not isinstance(receipt["candidates"], list)
            or not receipt["candidates"]
            or any(
                not isinstance(candidate, dict)
                or set(candidate) != candidate_fields
                or not isinstance(candidate["candidate_id"], str)
                or not candidate["candidate_id"]
                or not _router_is_sha(candidate["candidate_hash"])
                or not _router_is_sha(candidate["raw_artifact_sha"])
                or not _router_is_sha(
                    candidate["pre_phase_fact_sha256"]
                )
                or type(candidate["source_order"]) is not int
                or candidate["source_order"] < 0
                or type(candidate["call_l1_model"]) is not bool
                for candidate in receipt["candidates"]
            )
        ):
            raise AuditMigrationError(mismatch)
        preplan = conn.execute(
            "SELECT * FROM audit_router_host_preplan_batches_v2 "
            "WHERE preplan_sha256=?", (receipt["preplan_sha256"],),
        ).fetchone()
        round_row, round_material, authority, budget = (
            _router_derivation_round(
                conn, receipt["run_id"], receipt["batch_id"],
                receipt["intent"],
            )
        )
        observation_row = conn.execute(
            "SELECT * FROM audit_router_host_observation_sets_v2 "
            "WHERE observation_set_sha256=?",
            (receipt["observation_set_sha256"],),
        ).fetchone()
        host_round = conn.execute(
            "SELECT * FROM audit_router_host_round_authorities_v2 "
            "WHERE authority_sha256=?",
            (receipt["host_round_authority_sha256"],),
        ).fetchone()
        source_set = conn.execute(
            "SELECT * FROM audit_router_source_sets_v2 "
            "WHERE source_set_sha256=? AND phase='pre_l1'",
            (receipt["pre_l1_source_set_sha256"],),
        ).fetchone()
        if (
            preplan is None
            or observation_row is None
            or host_round is None
            or source_set is None
            or _router_host_preplan_row_valid(*tuple(preplan)) != 1
            or _router_host_observation_row_valid(
                *tuple(observation_row)
            ) != 1
            or _router_host_round_authority_row_valid(
                *tuple(host_round)
            ) != 1
        ):
            raise AuditMigrationError(mismatch)
        preplan_snapshot = _router_closed_json(preplan["snapshot_json"])
        preplan_candidates = _router_closed_json(preplan["candidates_json"])
        preplan_records = history_audit_plan.runtime_snapshot_records(
            _router_closed_json(preplan["records_json"])
        )
        host_observations = _router_closed_json(
            observation_row["observations_json"]
        )
        prepare_input_material = {
            "schema_version": "history-router-host-cli-prepare-input-v1",
            "authority_scope": "host_production",
            "preplan": {
                "run_id": preplan["run_id"],
                "batch_id": preplan["batch_id"],
                "intent": preplan["intent"],
                "history_as_of_watermark": preplan_snapshot[
                    "history_as_of_watermark"
                ],
                "exclusion_policy_sha": preplan_snapshot[
                    "exclusion_policy_sha"
                ],
                "records": preplan_records,
                "candidates": [
                    {
                        "candidate_id": candidate["candidate_id"],
                        "raw_artifact_sha": candidate[
                            "raw_artifact_sha"
                        ],
                        "source_order": candidate["source_order"],
                    }
                    for candidate in preplan_candidates
                ],
            },
            "observations": host_observations,
        }
        expected_input_sha = history_contract_v2.framed_sha256(
            "history-router-host-cli-prepare-input-v1",
            history_contract_v2.canonical_bytes(prepare_input_material),
        )
        route_sha = round_row["route_round_sha256"]
        if (
            route_sha != receipt["route_round_sha256"]
            or receipt["input_sha256"] != expected_input_sha
            or _router_round_sha(round_material) != route_sha
            or preplan["run_id"] != receipt["run_id"]
            or preplan["batch_id"] != receipt["batch_id"]
            or preplan["intent"] != receipt["intent"]
            or preplan_snapshot != round_material["snapshot"]
            or preplan_candidates != round_material["candidates"]
            or round_material["snapshot"]["snapshot_id"]
                != receipt["snapshot_id"]
            or round_material["snapshot"]["snapshot_hash"]
                != receipt["snapshot_hash"]
            or observation_row["route_round_sha256"] != route_sha
            or host_round["route_round_sha256"] != route_sha
            or host_round["observation_set_sha256"]
                != observation_row["observation_set_sha256"]
            or source_set["route_round_sha256"] != route_sha
        ):
            raise AuditMigrationError(mismatch)
        refs, sources = _router_source_refs_for_phase(
            conn, route_sha, "pre_l1", source_set
        )
        for source_kind, source in sources.items():
            if _router_validate_domain_source(
                round_material, route_sha, source_kind, source
            ) != source:
                raise AuditMigrationError(mismatch)
        current_heads, current_events = _router_dependency_binding(
            conn, sources.get("dependency_heads"), source_set
        )
        source_set_values = _router_source_set_values(
            route_sha, "pre_l1", refs, budget["budget_fact_sha256"],
            current_events, source_set["created_at"],
        )
        routes = _router_derived_candidate_facts(
            conn, round_material, authority, budget, "pre_l1", sources,
            current_heads, current_events, source_set["created_at"],
        )
        phase_values = [
            _router_phase_fact_values(
                route_sha, "pre_l1", source_set_values[0], route,
                source_set["created_at"],
            )
            for route in routes
        ]
        stored_phase = conn.execute(
            "SELECT * FROM audit_router_phase_facts_v2 "
            "WHERE route_round_sha256=? AND phase='pre_l1' "
            "ORDER BY candidate_id", (route_sha,),
        ).fetchall()
        if (
            tuple(source_set) != source_set_values
            or [tuple(row) for row in stored_phase] != phase_values
        ):
            raise AuditMigrationError(mismatch)
        routes_by_candidate = {
            route["candidate_id"]: route for route in routes
        }
        phase_by_candidate = {
            values[3]: values for values in phase_values
        }
        expected_candidates = [
            {
                "candidate_id": candidate["candidate_id"],
                "candidate_hash": candidate["candidate_hash"],
                "raw_artifact_sha": candidate["raw_artifact_sha"],
                "source_order": candidate["source_order"],
                "pre_phase_fact_sha256": phase_by_candidate[
                    candidate["candidate_id"]
                ][0],
                "call_l1_model": bool(routes_by_candidate[
                    candidate["candidate_id"]
                ]["call_l1_model"]),
            }
            for candidate in round_material["candidates"]
        ]
        if receipt["candidates"] != expected_candidates:
            raise AuditMigrationError(mismatch)
        return copy.deepcopy(receipt)
    except AuditMigrationError as exc:
        if str(exc) == mismatch:
            raise
        raise AuditMigrationError(mismatch) from exc
    except (
        KeyError, TypeError, ValueError, history_contract_v2.ContractV2Error,
    ) as exc:
        raise AuditMigrationError(mismatch) from exc


def record_host_router_l1_observations(
    conn, *, route_round_sha256, observations,
):
    """Persist one complete comparator cohort under a single transaction."""
    if conn.in_transaction:
        raise AuditMigrationError(
            "host router L1 observation batch requires an idle connection"
        )
    if (
        not _router_is_sha(route_round_sha256)
        or not isinstance(observations, list)
        or any(
            not isinstance(item, dict)
            or set(item) != {"candidate_id", "raw_observation_bytes"}
            or not isinstance(item["candidate_id"], str)
            or not item["candidate_id"]
            for item in observations
        )
    ):
        raise AuditMigrationError("router_host_l1_cohort_mismatch")
    parsed = []
    for item in observations:
        observation = _router_host_l1_raw_observation(
            item["raw_observation_bytes"]
        )
        if (
            observation["route_round_sha256"] != route_round_sha256
            or observation["candidate_id"] != item["candidate_id"]
        ):
            raise AuditMigrationError("router_host_l1_identity_mismatch")
        parsed.append(observation)
    guard = _ROUTER_SOURCE_GUARDS.get(id(conn))
    if guard is None or guard["host_l1_fact"] is not None:
        raise AuditMigrationError("host router L1 fact guard is unavailable")
    conn.execute("BEGIN IMMEDIATE")
    try:
        round_row = conn.execute(
            "SELECT * FROM audit_router_rounds_v2 "
            "WHERE route_round_sha256=?", (route_round_sha256,),
        ).fetchone()
        host_round = conn.execute(
            "SELECT * FROM audit_router_host_round_authorities_v2 "
            "WHERE route_round_sha256=?", (route_round_sha256,),
        ).fetchone()
        observation_row = conn.execute(
            "SELECT * FROM audit_router_host_observation_sets_v2 "
            "WHERE route_round_sha256=?", (route_round_sha256,),
        ).fetchone()
        if round_row is None or host_round is None or observation_row is None:
            raise AuditMigrationError(
                "router_host_l1_authority_unavailable"
            )
        try:
            material, authority = _router_validate_round_material(
                _router_closed_json(round_row["round_json"])
            )
        except (TypeError, ValueError) as exc:
            raise AuditMigrationError(
                "router_host_l1_authority_unavailable"
            ) from exc
        candidates = {
            item["candidate_id"]: item for item in material["candidates"]
        }
        pre_rows = conn.execute(
            "SELECT * FROM audit_router_phase_facts_v2 "
            "WHERE route_round_sha256=? AND phase='pre_l1' "
            "ORDER BY candidate_id", (route_round_sha256,),
        ).fetchall()
        pre_by_candidate = {
            row["candidate_id"]: row for row in pre_rows
        }
        material_candidate_ids = [
            item["candidate_id"] for item in material["candidates"]
        ]
        expected_ids = [
            candidate_id for candidate_id in material_candidate_ids
            if candidate_id in pre_by_candidate
            and pre_by_candidate[candidate_id]["call_l1_model"] == 1
        ]
        supplied_ids = [item["candidate_id"] for item in observations]
        if (
            [row["candidate_id"] for row in pre_rows]
                != material_candidate_ids
            or supplied_ids != expected_ids
        ):
            raise AuditMigrationError("router_host_l1_cohort_mismatch")
        if (
            authority.get("private_test_authority") is True
            or _router_round_sha(material) != route_round_sha256
            or _router_host_round_authority_row_valid(
                *tuple(host_round)
            ) != 1
            or _router_host_observation_row_valid(
                *tuple(observation_row)
            ) != 1
            or host_round["observation_set_sha256"]
                != observation_row["observation_set_sha256"]
        ):
            raise AuditMigrationError("router_host_l1_identity_mismatch")
        _require_host_router_pre_l1_source_authority(
            conn, route_round_sha256,
            observation_row["observation_set_sha256"],
        )
        final_exists = conn.execute(
            "SELECT 1 FROM audit_router_domain_sources_v2 "
            "WHERE route_round_sha256=? "
            "AND source_kind='l1_observation'",
            (route_round_sha256,),
        ).fetchone() is not None
        planned = []
        observed_at = None
        snapshot = material["snapshot"]
        for item, observation in zip(observations, parsed):
            candidate_id = item["candidate_id"]
            candidate = candidates[candidate_id]
            pre = pre_by_candidate[candidate_id]
            expected_identity = {
                "route_round_sha256": route_round_sha256,
                "host_round_authority_sha256": host_round[
                    "authority_sha256"
                ],
                "run_id": material["run_id"],
                "batch_id": material["batch_id"],
                "intent": material["intent"],
                "snapshot_id": snapshot["snapshot_id"],
                "snapshot_hash": snapshot["snapshot_hash"],
                "candidate_id": candidate_id,
                "candidate_hash": candidate["candidate_hash"],
                "candidate_raw_artifact_sha256": candidate[
                    "raw_artifact_sha"
                ],
                "source_order": candidate["source_order"],
                "pre_phase_fact_sha256": pre["phase_fact_sha256"],
            }
            if any(
                observation[name] != value
                for name, value in expected_identity.items()
            ):
                raise AuditMigrationError(
                    "router_host_l1_identity_mismatch"
                )
            existing = conn.execute(
                "SELECT * FROM "
                "audit_router_host_l1_comparator_facts_v2 "
                "WHERE route_round_sha256=? AND candidate_id=?",
                (route_round_sha256, candidate_id),
            ).fetchone()
            if existing is not None:
                values = _router_host_l1_comparator_values(
                    item["raw_observation_bytes"], existing["observed_at"]
                )
                if tuple(existing) != values:
                    raise AuditMigrationError(
                        "router_host_l1_observation_conflict"
                    )
                planned.append((values, False))
                continue
            if final_exists:
                raise AuditMigrationError(
                    "router_host_l1_source_already_final"
                )
            if observed_at is None:
                observed_at = _utc_now()
            values = _router_host_l1_comparator_values(
                item["raw_observation_bytes"], observed_at
            )
            planned.append((values, True))
        for values, insert_required in planned:
            if not insert_required:
                continue
            guard["host_l1_fact"] = values
            try:
                _insert_host_router_l1_comparator_fact(conn, values)
            finally:
                guard["host_l1_fact"] = None
        conn.execute("COMMIT")
        return {
            "schema_version": (
                "history-router-host-l1-comparator-batch-receipt-v2"
            ),
            "route_round_sha256": route_round_sha256,
            "receipts": [
                _router_host_l1_receipt(route_round_sha256, values)
                for values, _ in planned
            ],
            "authority_scope": "host_production",
        }
    except Exception:
        guard["host_l1_fact"] = None
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _router_stored_source(conn, route_sha, source_kind):
    row = conn.execute(
        "SELECT source_json FROM audit_router_domain_sources_v2 "
        "WHERE route_round_sha256=? AND source_kind=?",
        (route_sha, source_kind),
    ).fetchone()
    return None if row is None else _router_closed_json(row[0])


def _router_validate_l1_phase_bindings(conn, route_sha, source):
    if source is None:
        return
    for member in source["members"]:
        kind = member["observation_kind"]
        if kind == "comparator":
            fact = conn.execute(
                "SELECT * FROM audit_router_host_l1_comparator_facts_v2 "
                "WHERE comparator_fact_sha256=? "
                "AND route_round_sha256=? AND candidate_id=?",
                (
                    member["comparator_receipt_sha256"], route_sha,
                    member["candidate_id"],
                ),
            ).fetchone()
            if fact is None:
                # This validator is used only by the private test issuer.
                # Production host sources are derived through the raw-byte
                # comparator-fact path and never enter this branch.
                continue
            if (
                _router_host_l1_comparator_row_valid(*tuple(fact)) != 1
                or fact["comparator_outcome"]
                    != member["comparator_outcome"]
                or fact["coverage_state"] != member["coverage_state"]
            ):
                raise AuditMigrationError("router_source_identity_mismatch")
            continue
        pre_phase_sha = member["pre_phase_fact_sha256"]
        row = conn.execute(
            "SELECT call_l1_model,matched_rule_ids_json "
            "FROM audit_router_phase_facts_v2 "
            "WHERE phase_fact_sha256=? AND route_round_sha256=? "
            "AND phase='pre_l1' AND candidate_id=?",
            (
                pre_phase_sha, route_sha,
                member["candidate_id"],
            ),
        ).fetchone()
        try:
            matched_rule_ids = (
                [] if row is None
                else _router_closed_json(row["matched_rule_ids_json"])
            )
        except (TypeError, ValueError) as exc:
            raise AuditMigrationError("router_source_identity_mismatch") from exc
        if kind == "pre_l1_skip" and (
            row is None or row["call_l1_model"] != 0
            or member["skip_reason"] not in matched_rule_ids
        ):
            raise AuditMigrationError("router_source_identity_mismatch")
        if kind == "unavailable" and (
            row is None or row["call_l1_model"] != 1
            or conn.execute(
                "SELECT 1 FROM audit_router_host_l1_comparator_facts_v2 "
                "WHERE route_round_sha256=? AND candidate_id=?",
                (route_sha, member["candidate_id"]),
            ).fetchone() is not None
        ):
            raise AuditMigrationError("router_source_identity_mismatch")


def _issue_test_router_domain_sources(
    conn, route_round_sha256, *, sources, created_at=None
):
    """Issue closed raw sources only for a process-created test router round."""
    if conn.in_transaction:
        raise AuditMigrationError("router source issuance requires an idle connection")
    if not _router_is_sha(route_round_sha256):
        raise AuditMigrationError("router_source_identity_mismatch")
    if conn.execute(
        "SELECT 1 FROM audit_router_host_round_authorities_v2 "
        "WHERE route_round_sha256=?", (route_round_sha256,),
    ).fetchone() is not None:
        raise AuditMigrationError("router_source_test_authority_forbidden")
    round_json = _TEST_ROUTER_ROUND_AUTHORITIES.get(route_round_sha256)
    if round_json is None:
        raise AuditMigrationError("router_source_test_authority_unavailable")
    if (
        not isinstance(sources, dict)
        or not sources
        or set(sources).difference(_ROUTER_SOURCE_KINDS)
    ):
        raise AuditMigrationError("router_source_schema_mismatch")
    material, _ = _router_validate_round_material(
        _router_closed_json(round_json)
    )
    if _router_round_sha(material) != route_round_sha256:
        raise AuditMigrationError("router_source_identity_mismatch")
    prepared = {}
    for source_kind in sorted(sources):
        source = _router_validate_domain_source(
            material, route_round_sha256, source_kind, sources[source_kind]
        )
        source_json = _semantic_canonical(source)
        source_sha = _router_domain_source_sha(
            route_round_sha256, source_kind, source
        )
        prepared[source_kind] = (source, source_json, source_sha)
    _router_validate_l1_phase_bindings(
        conn,
        route_round_sha256,
        prepared.get("l1_observation", (None,))[0],
    )
    qualification = (
        prepared.get("qualification", (None,))[0]
        or _router_stored_source(conn, route_round_sha256, "qualification")
    )
    dependency_source = (
        prepared.get("dependency_heads", (None,))[0]
        or _router_stored_source(conn, route_round_sha256, "dependency_heads")
    )
    if qualification is not None and dependency_source is not None and (
        qualification["dependency_heads"] != dependency_source["heads"]
    ):
        raise AuditMigrationError("router_source_identity_mismatch")
    created_at = created_at or _utc_now()
    _semantic_timestamp(created_at, "created_at")
    guard = _ROUTER_SOURCE_GUARDS.get(id(conn))
    if guard is None or guard["sources"]:
        raise AuditMigrationError("router source guard is unavailable")
    conn.execute("BEGIN IMMEDIATE")
    try:
        round_row = conn.execute(
            "SELECT round_json,authority_scope FROM audit_router_rounds_v2 "
            "WHERE route_round_sha256=?", (route_round_sha256,)
        ).fetchone()
        if (
            round_row is None
            or tuple(round_row) != (round_json, "test_fake")
        ):
            raise AuditMigrationError("router_source_identity_mismatch")
        values_to_insert = []
        result = {}
        replay_created = []
        for source_kind, (_, source_json, source_sha) in prepared.items():
            existing = conn.execute(
                "SELECT source_sha256,source_json,created_at "
                "FROM audit_router_domain_sources_v2 "
                "WHERE route_round_sha256=? AND source_kind=?",
                (route_round_sha256, source_kind),
            ).fetchone()
            if existing is not None:
                if tuple(existing)[:2] != (source_sha, source_json):
                    raise AuditMigrationError("router_source_identity_mismatch")
                replay_created.append(existing["created_at"])
            else:
                values_to_insert.append(
                    (
                        source_sha, route_round_sha256, source_kind,
                        source_json, created_at,
                    )
                )
            result[source_kind] = source_sha
        if values_to_insert and replay_created and any(
            value != created_at for value in replay_created
        ):
            raise AuditMigrationError("router_source_replay_timestamp_mismatch")
        guard["sources"] = frozenset(values_to_insert)
        try:
            for values in values_to_insert:
                conn.execute(
                    "INSERT INTO audit_router_domain_sources_v2 VALUES(?,?,?,?,?)",
                    values,
                )
        finally:
            guard["sources"] = frozenset()
        if "dependency_heads" in prepared and any(
            values[2] == "dependency_heads" for values in values_to_insert
        ):
            _publish_semantic_dependency_heads_in_transaction(
                conn, prepared["dependency_heads"][0]["heads"], created_at
            )
        conn.execute("COMMIT")
        receipt_created = replay_created[0] if (
            replay_created and not values_to_insert
        ) else created_at
        return {
            "schema_version": "history-router-domain-source-set-v1",
            "route_round_sha256": route_round_sha256,
            "source_sha256_by_kind": dict(sorted(result.items())),
            "created_at": receipt_created,
        }
    except Exception:
        guard["sources"] = frozenset()
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _router_host_default_dependency_heads(route_round_sha256, authority):
    required = (
        "semantic_policy", "plan", "prompt", "schema",
        "ordered_provider_pools", "capacity", "provider", "fault",
        "replay", "fts", "metadata",
    )
    result = {}
    for kind in required:
        if kind == "plan":
            result[kind] = route_round_sha256
        elif kind == "semantic_policy":
            result[kind] = _semantic_sha(
                "history-router-host-semantic-policy-v1",
                {
                    "semantic_policy_profile_id": authority[
                        "semantic_policy_profile_id"
                    ]
                },
            )
        else:
            result[kind] = _semantic_sha(
                "history-router-host-unavailable-dependency-v1",
                {
                    "route_round_sha256": route_round_sha256,
                    "dependency_kind": kind,
                    "availability": "unavailable",
                },
            )
    return dict(sorted(result.items()))


def _router_host_dependency_heads(conn, route_round_sha256, authority):
    defaults = _router_host_default_dependency_heads(
        route_round_sha256, authority
    )
    current = _current_semantic_dependency_heads(conn)
    merged = dict(defaults)
    merged.update(current)
    try:
        return _semantic_dependencies(merged)
    except ValueError as exc:
        raise AuditMigrationError(
            "router_host_dependency_authority_unavailable"
        ) from exc


def _router_host_qualification_source(
    conn, material, identity, dependency_heads, route_round_sha256, *,
    dependency_head_events=None, observed_at=None,
):
    profile_id = material["semantic_policy_profile_id"]
    dependency_json = _semantic_canonical(dependency_heads)
    if dependency_head_events is None:
        dependency_head_events = _current_semantic_dependency_head_events(
            conn, dependency_heads
        )
    dependency_events_json = _semantic_canonical(dependency_head_events)
    if observed_at is not None:
        _semantic_timestamp(observed_at, "observed_at")
    production_row = conn.execute(
        """
        SELECT qualification.qualification_id,fact.qrels_hash,
               fact.policy_sha256,fact.metrics_json,fact.scope,
               fact.expires_at,fact.created_at
        FROM audit_semantic_qualifications qualification
        JOIN audit_semantic_qualification_facts_v2 fact
          USING(qualification_id)
        JOIN audit_semantic_qualification_head_bindings_v2 binding
          USING(qualification_id)
        WHERE qualification.semantic_policy_profile_id=?
          AND fact.dependency_hashes_json=?
          AND fact.production_qualified=1
          AND fact.vetoes_json='[]'
          AND binding.dependency_head_events_json=?
          AND (? IS NULL OR fact.created_at<=?)
          AND NOT EXISTS (
            SELECT 1 FROM audit_semantic_invalidation_facts_v2 invalidation
            WHERE invalidation.qualification_id=qualification.qualification_id
              AND (? IS NULL OR invalidation.invalidated_at<=?)
          )
        ORDER BY fact.created_at DESC,qualification.qualification_id DESC
        LIMIT 1
        """,
        (
            profile_id, dependency_json, dependency_events_json,
            observed_at, observed_at, observed_at, observed_at,
        ),
    ).fetchone()
    calibration_row = conn.execute(
        """
        SELECT qualification.qualification_id,fact.qrels_hash,
               fact.policy_sha256,fact.metrics_json,fact.scope,
               fact.expires_at,fact.created_at
        FROM audit_semantic_qualifications qualification
        JOIN audit_semantic_qualification_facts_v2 fact
          USING(qualification_id)
        JOIN audit_semantic_qualification_head_bindings_v2 binding
          USING(qualification_id)
        WHERE qualification.semantic_policy_profile_id=?
          AND fact.dependency_hashes_json=?
          AND binding.dependency_head_events_json=?
          AND (? IS NULL OR fact.created_at<=?)
          AND NOT EXISTS (
            SELECT 1 FROM audit_semantic_invalidation_facts_v2 invalidation
            WHERE invalidation.qualification_id=qualification.qualification_id
              AND (? IS NULL OR invalidation.invalidated_at<=?)
          )
        ORDER BY fact.production_qualified DESC,fact.created_at DESC,
                 qualification.qualification_id DESC
        LIMIT 1
        """,
        (
            profile_id, dependency_json, dependency_events_json,
            observed_at, observed_at, observed_at, observed_at,
        ),
    ).fetchone()
    calibration_ready = False
    if calibration_row is not None:
        try:
            policy = history_audit_plan._load_host_policy(
                "semantic-release-policy-v1.json"
            )
            from lib import history_audit_eval_v2
            policy_sha = history_audit_eval_v2.semantic_policy_sha256(policy)
            metrics = _router_closed_json(calibration_row["metrics_json"])
            shadow = policy["shadow"]
            calibration_ready = (
                policy["semantic_policy_profile_id"] == profile_id
                and calibration_row["policy_sha256"] == policy_sha
                and calibration_row["scope"]
                    in {"real", "production", "real_qrels"}
                and _semantic_timestamp(
                    calibration_row["expires_at"], "expires_at"
                ) > _semantic_timestamp(
                    observed_at or _utc_now(), "host_now"
                )
                and metrics["aggregate_recall"]["denominator"]
                    >= shadow["minimum_positive_lineages"]
                and metrics["negative_lineages"]
                    >= shadow["minimum_negative_lineages"]
                and all(
                    metrics["slices"][name]["recall"]["denominator"]
                        >= minimum
                    for name, minimum
                    in shadow["critical_slices"].items()
                )
            )
        except (
            ImportError, KeyError, TypeError, ValueError,
            history_audit_plan.AuditPlanError,
        ):
            calibration_ready = False
    if calibration_row is None or not calibration_ready:
        qrels_hash = _semantic_sha(
            "history-router-host-qrels-unavailable-v1",
            {
                "route_round_sha256": route_round_sha256,
                "semantic_policy_profile_id": profile_id,
                "availability": "unavailable",
            },
        )
        calibration_state = "unqualified"
    else:
        qrels_hash = calibration_row["qrels_hash"]
        calibration_state = "shadow_ready"
    if production_row is None:
        qualification_id = None
        lookup_state = "unavailable"
    else:
        qualification_id = production_row["qualification_id"]
        lookup_state = "available"
        qrels_hash = production_row["qrels_hash"]
        calibration_state = "shadow_ready"
    calibration = {
        "schema_version": "history-router-calibration-source-v1",
        **identity,
        "semantic_policy_profile_id": profile_id,
        "qrels_hash": qrels_hash,
        "calibration_state": calibration_state,
    }
    qualification = {
        "schema_version": "history-router-qualification-source-v1",
        **identity,
        "semantic_policy_profile_id": profile_id,
        "qrels_hash": qrels_hash,
        "qualification_id": qualification_id,
        "lookup_state": lookup_state,
        "dependency_heads": dependency_heads,
    }
    calibration_binding = {
        "lookup_state": (
            "available" if calibration_state == "shadow_ready"
            else "unavailable"
        ),
        "qualification_id": (
            None if calibration_row is None or not calibration_ready
            else calibration_row["qualification_id"]
        ),
        "qrels_hash": qrels_hash,
    }
    return calibration, qualification, calibration_binding


def _router_host_l1_source(conn, material, identity, route_round_sha256):
    candidate_ids = [item["candidate_id"] for item in material["candidates"]]
    phase_rows = conn.execute(
        "SELECT phase_fact_sha256,candidate_id,call_l1_model,"
        "matched_rule_ids_json FROM audit_router_phase_facts_v2 "
        "WHERE route_round_sha256=? AND phase='pre_l1' "
        "ORDER BY candidate_id",
        (route_round_sha256,),
    ).fetchall()
    if [row["candidate_id"] for row in phase_rows] != candidate_ids:
        raise AuditMigrationError("router_host_pre_l1_phase_unavailable")
    authority = history_audit_plan._host_runtime_authority()
    pre_l1_rules = [
        rule["rule_id"] for rule in authority["risk_policy"]["rules"]
        if rule["pre_l1"]
    ]
    members = []
    bindings = []
    for row in phase_rows:
        if row["call_l1_model"] == 1:
            fact = conn.execute(
                "SELECT * FROM audit_router_host_l1_comparator_facts_v2 "
                "WHERE route_round_sha256=? AND candidate_id=?",
                (route_round_sha256, row["candidate_id"]),
            ).fetchone()
            if fact is None:
                members.append({
                    "candidate_id": row["candidate_id"],
                    "observation_kind": "unavailable",
                    "unavailable_reason": "comparator_fact_missing",
                    "coverage_state": "unavailable",
                    "pre_phase_fact_sha256": row["phase_fact_sha256"],
                })
                bindings.append({
                    "candidate_id": row["candidate_id"],
                    "pre_phase_fact_sha256": row["phase_fact_sha256"],
                    "observation_kind": "unavailable",
                    "comparator_fact_sha256": None,
                    "raw_comparator_artifact_sha256": None,
                })
            else:
                if (
                    _router_host_l1_comparator_row_valid(*tuple(fact)) != 1
                    or fact["pre_phase_fact_sha256"]
                        != row["phase_fact_sha256"]
                ):
                    raise AuditMigrationError(
                        "router_host_l1_observation_authority_unavailable"
                    )
                members.append({
                    "candidate_id": row["candidate_id"],
                    "observation_kind": "comparator",
                    "comparator_outcome": fact["comparator_outcome"],
                    "coverage_state": fact["coverage_state"],
                    "comparator_receipt_sha256": fact[
                        "comparator_fact_sha256"
                    ],
                })
                bindings.append({
                    "candidate_id": row["candidate_id"],
                    "pre_phase_fact_sha256": row["phase_fact_sha256"],
                    "observation_kind": "comparator",
                    "comparator_fact_sha256": fact[
                        "comparator_fact_sha256"
                    ],
                    "raw_comparator_artifact_sha256": fact[
                        "raw_comparator_artifact_sha256"
                    ],
                })
        else:
            matched = _router_closed_json(row["matched_rule_ids_json"])
            skip_reason = next(
                (rule_id for rule_id in pre_l1_rules if rule_id in matched),
                None,
            )
            if skip_reason is None:
                raise AuditMigrationError(
                    "router_host_l1_observation_authority_unavailable"
                )
            members.append({
                "candidate_id": row["candidate_id"],
                "observation_kind": "pre_l1_skip",
                "skip_reason": skip_reason,
                "coverage_state": "not_run",
                "pre_phase_fact_sha256": row["phase_fact_sha256"],
            })
            bindings.append({
                "candidate_id": row["candidate_id"],
                "pre_phase_fact_sha256": row["phase_fact_sha256"],
                "observation_kind": "pre_l1_skip",
                "comparator_fact_sha256": None,
                "raw_comparator_artifact_sha256": None,
            })
    return ({
        "schema_version": "history-router-l1-source-v1",
        **identity,
        "candidate_ids": candidate_ids,
        "members": members,
    }, bindings)


def _router_host_sources(
    conn, material, route_round_sha256, observations, observation_set_sha256,
    phase, dependency_heads, *, dependency_head_events=None,
    observed_at=None,
):
    candidate_ids = [item["candidate_id"] for item in material["candidates"]]
    identity = {
        "route_round_sha256": route_round_sha256,
        "run_id": material["run_id"],
        "batch_id": material["batch_id"],
        "snapshot_id": material["snapshot"]["snapshot_id"],
        "snapshot_hash": material["snapshot"]["snapshot_hash"],
    }
    selection_members = []
    risk_members = []
    request_members = []
    for member in observations["members"]:
        selection_members.append({
            "candidate_id": member["candidate_id"],
            "selection_class": member["selection_class"],
            "channel_states": copy.deepcopy(member["channel_states"]),
        })
        risk_members.append({
            "candidate_id": member["candidate_id"],
            "assigned_slice_ids": copy.deepcopy(
                member["assigned_slice_ids"]
            ),
        })
        request_id = member["permanent_request_id"]
        request_members.append({
            "candidate_id": member["candidate_id"],
            "request_state": (
                "requested" if request_id is not None else "not_requested"
            ),
            "request_id": request_id,
        })
    calibration, qualification, calibration_binding = (
        _router_host_qualification_source(
            conn, material, identity, dependency_heads, route_round_sha256,
            dependency_head_events=dependency_head_events,
            observed_at=observed_at,
        )
    )
    sources = {
        "selection": {
            "schema_version": "history-router-selection-source-v1",
            **identity,
            "selected_candidate_id": observations["selected_candidate_id"],
            "candidate_ids": candidate_ids,
            "members": selection_members,
        },
        "calibration": calibration,
        "qualification": qualification,
        "risk_assignment": {
            "schema_version": "history-router-risk-assignment-source-v1",
            **identity,
            "candidate_ids": candidate_ids,
            "members": risk_members,
        },
        "dependency_heads": {
            "schema_version": "history-router-dependency-heads-source-v1",
            **identity,
            "heads": dependency_heads,
            "observed_index_profile_sha256": dependency_heads["fts"],
        },
        "permanent_request": {
            "schema_version": "history-router-permanent-request-source-v1",
            **identity,
            "candidate_ids": candidate_ids,
            "members": request_members,
        },
    }
    common_inputs = {
        "schema_version": "history-router-host-source-derivation-v1",
        "route_round_sha256": route_round_sha256,
        "observation_set_sha256": observation_set_sha256,
    }
    events = (
        _current_semantic_dependency_head_events(conn, dependency_heads)
        if dependency_head_events is None
        else copy.deepcopy(dependency_head_events)
    )
    derivation_inputs = {
        kind: {
            **common_inputs,
            "source_kind": kind,
            "dependency_head_events": events,
        }
        for kind in sources
    }
    derivation_inputs["calibration"]["calibration_binding"] = (
        calibration_binding
    )
    if phase == "final":
        l1_source, l1_bindings = _router_host_l1_source(
            conn, material, identity, route_round_sha256
        )
        sources["l1_observation"] = l1_source
        derivation_inputs["l1_observation"] = {
            **common_inputs,
            "source_kind": "l1_observation",
            "pre_l1_bindings": l1_bindings,
        }
    return sources, derivation_inputs


def issue_host_router_domain_sources(
    conn, route_round_sha256, *, phase, created_at=None,
):
    """Derive an exact host source phase without caller source payloads."""
    if conn.in_transaction:
        raise AuditMigrationError(
            "host router source issuance requires an idle connection"
        )
    if phase not in {"pre_l1", "final"} or not _router_is_sha(
        route_round_sha256
    ):
        raise AuditMigrationError("router_host_source_schema_mismatch")
    round_row = conn.execute(
        "SELECT * FROM audit_router_rounds_v2 WHERE route_round_sha256=?",
        (route_round_sha256,),
    ).fetchone()
    host_round = conn.execute(
        "SELECT * FROM audit_router_host_round_authorities_v2 "
        "WHERE route_round_sha256=?", (route_round_sha256,),
    ).fetchone()
    observation_row = conn.execute(
        "SELECT * FROM audit_router_host_observation_sets_v2 "
        "WHERE route_round_sha256=?", (route_round_sha256,),
    ).fetchone()
    if round_row is None or host_round is None or observation_row is None:
        raise AuditMigrationError("router_host_round_authority_unavailable")
    try:
        material, authority = _router_validate_round_material(
            _router_closed_json(round_row["round_json"])
        )
        observations = _router_validate_host_observations(
            material, _router_closed_json(observation_row["observations_json"])
        )
    except (TypeError, ValueError) as exc:
        raise AuditMigrationError("router_host_round_identity_mismatch") from exc
    if (
        _router_round_sha(material) != route_round_sha256
        or _router_host_observation_row_valid(*tuple(observation_row)) != 1
        or _router_host_round_authority_row_valid(*tuple(host_round)) != 1
        or host_round["observation_set_sha256"]
            != observation_row["observation_set_sha256"]
        or authority.get("private_test_authority") is True
    ):
        raise AuditMigrationError("router_host_round_identity_mismatch")
    created_at = created_at or _utc_now()
    _semantic_timestamp(created_at, "created_at")
    guard = _ROUTER_SOURCE_GUARDS.get(id(conn))
    if guard is None or guard["sources"] or guard["host_sources"]:
        raise AuditMigrationError("host router source guard is unavailable")
    conn.execute("BEGIN IMMEDIATE")
    try:
        dependency_heads = _router_host_dependency_heads(
            conn, route_round_sha256, authority
        )
        current_heads = _current_semantic_dependency_heads(conn)
        if current_heads != dependency_heads:
            _publish_semantic_dependency_heads_in_transaction(
                conn, dependency_heads, created_at
            )
        sources, derivation_inputs = _router_host_sources(
            conn, material, route_round_sha256, observations,
            observation_row["observation_set_sha256"], phase,
            dependency_heads,
        )
        expected_kinds = set(_ROUTER_SOURCE_KINDS)
        if phase == "pre_l1":
            expected_kinds.remove("l1_observation")
        if set(sources) != expected_kinds:
            raise AuditMigrationError("router_host_source_set_incomplete")
        source_values = []
        host_values = []
        result = {}
        for source_kind in sorted(sources):
            source = _router_validate_domain_source(
                material, route_round_sha256, source_kind,
                sources[source_kind],
            )
            source_json = _semantic_canonical(source)
            source_sha = _router_domain_source_sha(
                route_round_sha256, source_kind, source
            )
            existing = conn.execute(
                "SELECT * FROM audit_router_domain_sources_v2 "
                "WHERE route_round_sha256=? AND source_kind=?",
                (route_round_sha256, source_kind),
            ).fetchone()
            if existing is None:
                source_created_at = created_at
                source_values.append((
                    source_sha, route_round_sha256, source_kind,
                    source_json, source_created_at,
                ))
            else:
                if (
                    existing["source_sha256"] != source_sha
                    or existing["source_json"] != source_json
                ):
                    raise AuditMigrationError(
                        "router_host_source_identity_mismatch"
                    )
                source_created_at = existing["created_at"]
            authority_values = _router_host_source_authority_values(
                source_sha, route_round_sha256, source_kind,
                observation_row["observation_set_sha256"],
                derivation_inputs[source_kind], source_created_at,
            )
            stored_authority = conn.execute(
                "SELECT * FROM audit_router_host_source_authorities_v2 "
                "WHERE route_round_sha256=? AND source_kind=?",
                (route_round_sha256, source_kind),
            ).fetchone()
            if stored_authority is None:
                host_values.append(authority_values)
            elif tuple(stored_authority) != authority_values:
                raise AuditMigrationError(
                    "router_host_source_authority_mismatch"
                )
            result[source_kind] = source_sha
        guard["sources"] = frozenset(source_values)
        try:
            for values in source_values:
                conn.execute(
                    "INSERT INTO audit_router_domain_sources_v2 "
                    "VALUES(?,?,?,?,?)", values,
                )
        finally:
            guard["sources"] = frozenset()
        guard["host_sources"] = frozenset(host_values)
        try:
            for values in host_values:
                conn.execute(
                    "INSERT INTO audit_router_host_source_authorities_v2 "
                    "VALUES(?,?,?,?,?,?,?,?)", values,
                )
        finally:
            guard["host_sources"] = frozenset()
        conn.execute("COMMIT")
        return {
            "schema_version": "history-router-host-domain-source-set-v1",
            "route_round_sha256": route_round_sha256,
            "phase": phase,
            "source_sha256_by_kind": dict(sorted(result.items())),
            "authority_scope": "host_production",
            "created_at": created_at,
        }
    except Exception:
        guard["sources"] = frozenset()
        guard["host_sources"] = frozenset()
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _router_derivation_round(conn, run_id, batch_id, intent):
    rows = conn.execute(
        "SELECT * FROM audit_router_rounds_v2 "
        "WHERE run_id=? AND batch_id=? AND intent=?",
        (run_id, batch_id, intent),
    ).fetchall()
    if len(rows) != 1:
        raise AuditMigrationError("router_round_authority_unavailable")
    row = rows[0]
    if _router_round_row_valid(*tuple(row)[:-1]) != 1:
        raise AuditMigrationError("router_round_identity_mismatch")
    material, authority = _router_validate_round_material(
        _router_closed_json(row["round_json"])
    )
    budget = conn.execute(
        "SELECT * FROM audit_router_budget_facts_v2 "
        "WHERE route_round_sha256=?",
        (row["route_round_sha256"],),
    ).fetchone()
    if budget is None or _router_budget_row_valid(
        row["round_json"], *tuple(budget)
    ) != 1:
        raise AuditMigrationError("candidate_budget_authority_unavailable")
    if budget["candidate_budget_decision"] != "accepted":
        raise AuditMigrationError("candidate_budget_exceeded")
    return row, material, authority, budget


def _router_source_refs_for_phase(conn, route_sha, phase, existing):
    allowed = set(_ROUTER_SOURCE_KINDS)
    if phase == "pre_l1":
        allowed.remove("l1_observation")
    if existing is None:
        rows = conn.execute(
            "SELECT source_kind,source_sha256,source_json "
            "FROM audit_router_domain_sources_v2 "
            "WHERE route_round_sha256=? ORDER BY source_kind",
            (route_sha,),
        ).fetchall()
        refs = {
            row["source_kind"]: row["source_sha256"]
            for row in rows if row["source_kind"] in allowed
        }
    else:
        try:
            refs = _router_closed_json(existing["source_refs_json"])
        except (TypeError, ValueError) as exc:
            raise AuditMigrationError("router_source_set_identity_mismatch") from exc
        if (
            not isinstance(refs, dict)
            or set(refs).difference(allowed)
            or any(
                kind not in _ROUTER_SOURCE_KINDS or not _router_is_sha(digest)
                for kind, digest in refs.items()
            )
            or list(refs) != sorted(refs)
        ):
            raise AuditMigrationError("router_source_set_identity_mismatch")
    if "selection" not in refs:
        raise AuditMigrationError("router_source_selection_unavailable")
    sources = {}
    for kind, digest in sorted(refs.items()):
        row = conn.execute(
            "SELECT source_sha256,source_json FROM "
            "audit_router_domain_sources_v2 "
            "WHERE route_round_sha256=? AND source_kind=?",
            (route_sha, kind),
        ).fetchone()
        if row is None or row["source_sha256"] != digest:
            raise AuditMigrationError("router_source_set_identity_mismatch")
        source = _router_closed_json(row["source_json"])
        if _router_domain_source_sha(route_sha, kind, source) != digest:
            raise AuditMigrationError("router_source_set_identity_mismatch")
        sources[kind] = source
    return refs, sources


def _router_dependency_binding(
    conn, dependency_source, existing, *, require_current=True,
):
    if not require_current:
        if dependency_source is None or existing is None:
            raise AuditMigrationError("router_source_dependency_drift")
        try:
            stored_events = _router_closed_json(
                existing["dependency_head_events_json"]
            )
        except (TypeError, ValueError) as exc:
            raise AuditMigrationError(
                "router_source_set_identity_mismatch"
            ) from exc
        expected_heads = dependency_source["heads"]
        _router_historical_dependency_head_events(
            conn, expected_heads,
            {"dependency_head_events": stored_events},
        )
        return expected_heads, stored_events
    if dependency_source is None:
        current_heads = _current_semantic_dependency_heads(conn)
        current_events = _current_semantic_dependency_head_events(conn)
    else:
        expected_heads = dependency_source["heads"]
        current_heads = _current_semantic_dependency_heads(
            conn, expected_heads
        )
        current_events = _current_semantic_dependency_head_events(
            conn, expected_heads
        )
        if current_heads != expected_heads:
            raise AuditMigrationError("router_source_dependency_drift")
    if existing is not None:
        try:
            stored_events = _router_closed_json(
                existing["dependency_head_events_json"]
            )
        except (TypeError, ValueError) as exc:
            raise AuditMigrationError("router_source_set_identity_mismatch") from exc
        if stored_events != current_events:
            raise AuditMigrationError("router_source_dependency_drift")
    return current_heads, current_events


def _router_qualification_is_current(
    conn, source, dependency_source, current_heads, current_events, now, *,
    require_current=True,
):
    if (
        source is None
        or source["lookup_state"] != "available"
        or dependency_source is None
        or source["dependency_heads"] != dependency_source["heads"]
        or source["dependency_heads"] != current_heads
    ):
        return False
    row = conn.execute(
        """
        SELECT qualification.semantic_policy_profile_id,
               qualification.qualification_sha256,
               qualification.qualification_json,
               fact.policy_sha256,fact.qrels_hash,
               fact.dependency_hashes_json,fact.vetoes_json,
               fact.production_qualified,fact.expires_at,
               binding.dependency_head_events_json
        FROM audit_semantic_qualifications qualification
        JOIN audit_semantic_qualification_facts_v2 fact
          USING(qualification_id)
        JOIN audit_semantic_qualification_head_bindings_v2 binding
          USING(qualification_id)
        WHERE qualification.qualification_id=?
          AND NOT EXISTS (
            SELECT 1 FROM audit_semantic_invalidation_facts_v2 invalidation
            WHERE invalidation.qualification_id=qualification.qualification_id
              AND (?=1 OR invalidation.invalidated_at<=?)
          )
        """,
        (source["qualification_id"], int(require_current), now),
    ).fetchone()
    if row is None:
        return False
    try:
        qualification_material = _router_closed_json(row["qualification_json"])
        expires_at = _semantic_timestamp(row["expires_at"], "expires_at")
        current = _semantic_timestamp(
            _utc_now() if require_current else now, "host_now"
        )
    except (TypeError, ValueError):
        return False
    dependencies_json = _semantic_canonical(source["dependency_heads"])
    events_json = _semantic_canonical(current_events)
    identity_current = (
        row["semantic_policy_profile_id"]
            == source["semantic_policy_profile_id"]
        and row["qrels_hash"] == source["qrels_hash"]
        and row["policy_sha256"] == source["dependency_heads"]["semantic_policy"]
        and row["dependency_hashes_json"] == dependencies_json
        and row["dependency_head_events_json"] == events_json
        and row["vetoes_json"] == "[]"
        and row["production_qualified"] == 1
        and expires_at > current
        and source["qualification_id"]
            == "semantic-v2-" + row["qualification_sha256"]
        and _semantic_sha(
            "history-semantic-qualification-v2", qualification_material
        ) == row["qualification_sha256"]
        and _router_is_sha(
            qualification_material.get("evaluation_root_sha256")
        )
    )
    if not identity_current:
        return False
    try:
        _require_durable_semantic_production_evidence(
            conn, source["dependency_heads"],
            corpus_snapshot_hash=qualification_material[
                "corpus_snapshot_hash"
            ],
        )
        if require_current:
            _require_semantic_evaluation_root(
                _load_production_evidence_roots(), qualification_material,
                expected_root_sha256=qualification_material[
                    "evaluation_root_sha256"
                ],
            )
    except (ValueError, AuditMigrationError):
        return False
    return True


def _router_source_members_by_candidate(source):
    if source is None:
        return {}
    return {item["candidate_id"]: item for item in source["members"]}


def _router_comparator_uncertain(phase, member):
    if phase == "pre_l1" or member is None:
        return True
    if member["observation_kind"] == "pre_l1_skip":
        return False
    if member["observation_kind"] == "unavailable":
        return True
    outcome = member["comparator_outcome"].strip().lower()
    return outcome not in {
        "certain", "confirmed", "match", "no_match", "same", "distinct",
    }


def _router_derived_candidate_facts(
    conn, material, authority, budget, phase, sources, current_heads,
    current_events, created_at, *, require_current_qualification=True,
):
    try:
        from lib import history_audit_eval_v2
    except ImportError:
        import history_audit_eval_v2
    selection = sources["selection"]
    selected_id = selection["selected_candidate_id"]
    selection_members = _router_source_members_by_candidate(selection)
    l1_members = _router_source_members_by_candidate(
        sources.get("l1_observation") if phase == "final" else None
    )
    risk_members = _router_source_members_by_candidate(
        sources.get("risk_assignment")
    )
    request_members = _router_source_members_by_candidate(
        sources.get("permanent_request")
    )
    calibration = sources.get("calibration")
    dependency_source = sources.get("dependency_heads")
    release_qualified = _router_qualification_is_current(
        conn, sources.get("qualification"), dependency_source,
        current_heads, current_events, created_at,
        require_current=require_current_qualification,
    )
    allowed_slices = sorted(
        history_audit_eval_v2.RISK_SLICE_POLICY_V1["allowed_slices"]
    )
    result = []
    for candidate in material["candidates"]:
        candidate_id = candidate["candidate_id"]
        selection_member = selection_members[candidate_id]
        risk_member = risk_members.get(candidate_id)
        request_member = request_members.get(candidate_id)
        risk_slices = (
            allowed_slices
            if risk_member is None
            else list(risk_member["assigned_slice_ids"])
        )
        facts = {
            "retriever_calibrated": bool(
                calibration is not None
                and calibration["calibration_state"] == "shadow_ready"
            ),
            "finalist_or_sa": (
                selection_member["selection_class"] in {"finalist", "sa"}
            ),
            "mandatory_channel_failed": any(
                item["state"] != "complete"
                for item in selection_member["channel_states"]
            ),
            "comparator_uncertain": _router_comparator_uncertain(
                phase, l1_members.get(candidate_id)
            ),
            "bad_slice_membership": bool(risk_slices),
            "index_profile_recently_changed": dependency_source is None,
            "permanent_no_match_requested": (
                request_member is None
                or request_member["request_state"] == "requested"
            ),
            "release_qualified": release_qualified,
            "candidate_budget_available": True,
            "attempt_budget_available": bool(
                budget["attempt_budget_available"]
                and candidate_id == selected_id
            ),
        }
        route = history_audit_eval_v2.route_candidate(
            facts, authority["risk_policy"]
        )
        result.append(
            {
                "candidate_id": candidate_id,
                "router_facts": facts,
                "risk_slices": risk_slices,
                "matched_rule_ids": route["matched_rule_ids"],
                "route": route["route"],
                "call_l1_model": route["call_l1_model"],
                "dispatch_allowed": route["dispatch_allowed"],
                "release_authorized": route["release_authorized"],
                "rule_table_sha256": route["rule_table_sha256"],
                "risk_policy_version": route[
                    "receipt_risk_policy_version"
                ],
            }
        )
    return result


def _router_source_set_values(
    route_sha, phase, refs, budget_sha, events, created_at
):
    material = {
        "schema_version": "history-router-source-set-v1",
        "route_round_sha256": route_sha,
        "phase": phase,
        "source_sha256_by_kind": dict(sorted(refs.items())),
        "budget_fact_sha256": budget_sha,
        "dependency_head_events": events,
        "created_at": created_at,
    }
    source_set_sha = _semantic_sha("history-router-source-set-v1", material)
    return (
        source_set_sha, route_sha, phase,
        _semantic_canonical(material["source_sha256_by_kind"]), budget_sha,
        _semantic_canonical(events), created_at,
    )


def _router_phase_fact_values(
    route_sha, phase, source_set_sha, route, created_at
):
    material = {
        "schema_version": "history-router-phase-fact-v1",
        "route_round_sha256": route_sha,
        "phase": phase,
        "candidate_id": route["candidate_id"],
        "source_set_sha256": source_set_sha,
        "router_facts": route["router_facts"],
        "risk_slices": route["risk_slices"],
        "matched_rule_ids": route["matched_rule_ids"],
        "route": route["route"],
        "call_l1_model": route["call_l1_model"],
        "dispatch_allowed": route["dispatch_allowed"],
        "release_authorized": route["release_authorized"],
        "rule_table_sha256": route["rule_table_sha256"],
        "risk_policy_version": route["risk_policy_version"],
        "created_at": created_at,
    }
    phase_sha = _semantic_sha("history-router-phase-fact-v1", material)
    return (
        phase_sha, route_sha, phase, route["candidate_id"], source_set_sha,
        _semantic_canonical(route["router_facts"]),
        _semantic_canonical(route["risk_slices"]),
        _semantic_canonical(route["matched_rule_ids"]), route["route"],
        int(route["call_l1_model"]), int(route["dispatch_allowed"]),
        int(route["release_authorized"]), route["rule_table_sha256"],
        route["risk_policy_version"], created_at,
    )


def _router_derivation_receipt(
    material, phase, source_set_sha, routes, phase_values, created_at
):
    values_by_candidate = {value[3]: value for value in phase_values}
    candidate_routes = []
    for route in routes:
        value = values_by_candidate[route["candidate_id"]]
        candidate_routes.append(
            {
                "candidate_id": route["candidate_id"],
                "phase_fact_sha256": value[0],
                "source_set_sha256": source_set_sha,
                **{
                    name: copy.deepcopy(route[name])
                    for name in (
                        "router_facts", "risk_slices", "matched_rule_ids",
                        "route", "call_l1_model", "dispatch_allowed",
                        "release_authorized", "rule_table_sha256",
                        "risk_policy_version",
                    )
                },
            }
        )
    return {
        "schema_version": "history-router-derivation-v1",
        "run_id": material["run_id"],
        "batch_id": material["batch_id"],
        "intent": material["intent"],
        "phase": phase,
        "route_round_sha256": _router_round_sha(material),
        "source_set_sha256": source_set_sha,
        "candidate_routes": candidate_routes,
        "created_at": created_at,
    }


def derive_candidate_route_facts(
    conn, run_id, batch_id, intent, *, phase, created_at=None
):
    """Derive one immutable pre-plan route phase from durable raw sources."""
    if conn.in_transaction:
        raise AuditMigrationError("router derivation requires an idle connection")
    if phase not in {"pre_l1", "final"}:
        raise AuditMigrationError("router_phase_invalid")
    if created_at is not None:
        _semantic_timestamp(created_at, "created_at")
    guard = _ROUTER_SOURCE_GUARDS.get(id(conn))
    if (
        guard is None
        or guard["source_set"] is not None
        or guard["phase_fact"] is not None
    ):
        raise AuditMigrationError("router derivation guard is unavailable")
    conn.execute("BEGIN IMMEDIATE")
    try:
        round_row, material, authority, budget = _router_derivation_round(
            conn, run_id, batch_id, intent
        )
        route_sha = round_row["route_round_sha256"]
        existing_set = conn.execute(
            "SELECT * FROM audit_router_source_sets_v2 "
            "WHERE route_round_sha256=? AND phase=?",
            (route_sha, phase),
        ).fetchone()
        refs, sources = _router_source_refs_for_phase(
            conn, route_sha, phase, existing_set
        )
        current_heads, current_events = _router_dependency_binding(
            conn, sources.get("dependency_heads"), existing_set
        )
        effective_created_at = (
            existing_set["created_at"]
            if existing_set is not None
            else (created_at or _utc_now())
        )
        _semantic_timestamp(effective_created_at, "created_at")
        source_set_values = _router_source_set_values(
            route_sha, phase, refs, budget["budget_fact_sha256"],
            current_events, effective_created_at,
        )
        if existing_set is None:
            guard["source_set"] = source_set_values
            try:
                conn.execute(
                    "INSERT INTO audit_router_source_sets_v2 "
                    "VALUES(?,?,?,?,?,?,?)", source_set_values,
                )
            finally:
                guard["source_set"] = None
        elif tuple(existing_set) != source_set_values:
            raise AuditMigrationError("router_source_set_identity_mismatch")
        routes = _router_derived_candidate_facts(
            conn, material, authority, budget, phase, sources,
            current_heads, current_events, effective_created_at,
        )
        phase_values = [
            _router_phase_fact_values(
                route_sha, phase, source_set_values[0], route,
                effective_created_at,
            )
            for route in routes
        ]
        existing_facts = {
            row["candidate_id"]: row
            for row in conn.execute(
                "SELECT * FROM audit_router_phase_facts_v2 "
                "WHERE route_round_sha256=? AND phase=? ORDER BY candidate_id",
                (route_sha, phase),
            )
        }
        if existing_set is not None and set(existing_facts) != {
            route["candidate_id"] for route in routes
        }:
            raise AuditMigrationError("router_phase_fact_incomplete")
        for values in phase_values:
            existing = existing_facts.get(values[3])
            if existing is None:
                guard["phase_fact"] = values
                try:
                    conn.execute(
                        "INSERT INTO audit_router_phase_facts_v2 "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values,
                    )
                finally:
                    guard["phase_fact"] = None
            elif tuple(existing) != values:
                raise AuditMigrationError("router_phase_fact_identity_mismatch")
        conn.execute("COMMIT")
        return _router_derivation_receipt(
            material, phase, source_set_values[0], routes, phase_values,
            effective_created_at,
        )
    except Exception:
        guard["source_set"] = None
        guard["phase_fact"] = None
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _router_durable_plan_and_round(conn, run_id):
    """Load one exact frozen plan and its matching router round in an active tx."""
    if not conn.in_transaction:
        raise AuditMigrationError("route materialization requires a transaction")
    run = conn.execute(
        "SELECT plan_hash,manifest_json FROM audit_run_manifests WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if run is None:
        raise AuditMigrationError("router_plan_authority_unavailable")
    try:
        plan = history_audit_plan.validate_runtime_plan_material(
            _closed_json(run["manifest_json"])
        )
        plan_sha = history_audit_plan.runtime_plan_sha_from_material(plan)
    except (TypeError, ValueError, history_audit_plan.AuditPlanError) as exc:
        raise AuditMigrationError("router_plan_identity_mismatch") from exc
    if plan_sha != run["plan_hash"] or plan["run_id"] != run_id:
        raise AuditMigrationError("router_plan_identity_mismatch")
    round_row, round_material, authority, budget = _router_derivation_round(
        conn, plan["run_id"], plan["batch_id"], plan["intent"]
    )
    plan_snapshot = plan["snapshot"]
    round_snapshot = round_material["snapshot"]
    selected = [
        candidate for candidate in round_material["candidates"]
        if candidate["candidate_id"] == plan["candidate"]["candidate_id"]
    ]
    if (
        len(selected) != 1
        or selected[0] != plan["candidate"]
        or any(
            round_snapshot[name] != plan_snapshot[name]
            for name in _ROUTER_SNAPSHOT_FIELDS
        )
        or round_material["semantic_policy_profile_id"]
            != plan["semantic_policy_profile_id"]
        or round_material["risk_policy_sha"] != plan["risk_policy_sha"]
        or round_material["budget_policy_sha"] != plan["budget_policy_sha"]
        or history_contract_v2.canonical_bytes(authority["budget_policy"])
            != history_contract_v2.canonical_bytes(plan["budget_policy"])
    ):
        raise AuditMigrationError("router_round_plan_identity_mismatch")
    candidate_ids = [
        candidate["candidate_id"] for candidate in round_material["candidates"]
    ]
    batch_set = conn.execute(
        "SELECT snapshot_id,current_batch_ids_hash,member_ids_json,member_count "
        "FROM audit_snapshot_batch_sets WHERE run_id=? AND batch_id=?",
        (plan["run_id"], plan["batch_id"]),
    ).fetchone()
    snapshot = conn.execute(
        "SELECT snapshot_hash,run_id,batch_id FROM audit_snapshots "
        "WHERE snapshot_id=?",
        (plan_snapshot["snapshot_id"],),
    ).fetchone()
    if (
        batch_set is None
        or tuple(batch_set) != (
            plan_snapshot["snapshot_id"],
            plan_snapshot["current_batch_ids_hash"],
            _semantic_canonical(candidate_ids),
            len(candidate_ids),
        )
        or snapshot is None
        or tuple(snapshot) != (
            plan_snapshot["snapshot_hash"], plan["run_id"], plan["batch_id"]
        )
        or not _accepted_candidate_budget_receipt_matches(conn, plan)
    ):
        raise AuditMigrationError("router_round_plan_identity_mismatch")
    return plan, round_row, round_material, authority, budget


def _router_candidate_cohort_for_plan_persistence(conn, run_id):
    """Return the host-validated full cohort that must already be staged."""
    _, _, round_material, _, _ = _router_durable_plan_and_round(conn, run_id)
    return copy.deepcopy(round_material["candidates"])


def _router_revalidated_final_phase(
    conn, round_row, round_material, authority, budget, *,
    require_current_dependencies=True,
):
    route_sha = round_row["route_round_sha256"]
    source_set = conn.execute(
        "SELECT * FROM audit_router_source_sets_v2 "
        "WHERE route_round_sha256=? AND phase='final'",
        (route_sha,),
    ).fetchone()
    if source_set is None:
        raise AuditMigrationError("router_final_phase_unavailable")
    refs, sources = _router_source_refs_for_phase(
        conn, route_sha, "final", source_set
    )
    for source_kind, source in sources.items():
        validated = _router_validate_domain_source(
            round_material, route_sha, source_kind, source
        )
        if validated != source:
            raise AuditMigrationError("router_source_identity_mismatch")
    _router_validate_l1_phase_bindings(
        conn, route_sha, sources.get("l1_observation")
    )
    current_heads, current_events = _router_dependency_binding(
        conn, sources.get("dependency_heads"), source_set,
        require_current=require_current_dependencies,
    )
    source_set_values = _router_source_set_values(
        route_sha, "final", refs, budget["budget_fact_sha256"],
        current_events, source_set["created_at"],
    )
    if tuple(source_set) != source_set_values:
        raise AuditMigrationError("router_source_set_identity_mismatch")
    routes = _router_derived_candidate_facts(
        conn, round_material, authority, budget, "final", sources,
        current_heads, current_events, source_set["created_at"],
        require_current_qualification=require_current_dependencies,
    )
    phase_values = [
        _router_phase_fact_values(
            route_sha, "final", source_set_values[0], route,
            source_set["created_at"],
        )
        for route in routes
    ]
    stored = conn.execute(
        "SELECT * FROM audit_router_phase_facts_v2 "
        "WHERE route_round_sha256=? AND phase='final' ORDER BY candidate_id",
        (route_sha,),
    ).fetchall()
    if [tuple(row) for row in stored] != phase_values:
        raise AuditMigrationError("router_phase_fact_identity_mismatch")
    return source_set_values, routes, phase_values


def _materialize_final_candidate_routes_for_plan(conn, run_id, *, created_at):
    """Project exact durable final router facts into compatibility route tables."""
    if not conn.in_transaction:
        raise AuditMigrationError("route materialization requires a transaction")
    _semantic_timestamp(created_at, "created_at")
    plan, round_row, round_material, authority, budget = (
        _router_durable_plan_and_round(conn, run_id)
    )
    candidates = round_material["candidates"]
    candidate_ids = [candidate["candidate_id"] for candidate in candidates]
    staged = conn.execute(
        "SELECT staging_candidate_id,candidate_hash,raw_artifact_sha,source_order "
        "FROM audit_batch_staging WHERE run_id=? AND batch_id=? "
        "ORDER BY staging_candidate_id",
        (plan["run_id"], plan["batch_id"]),
    ).fetchall()
    expected_staging = [
        (
            candidate["candidate_id"], candidate["candidate_hash"],
            candidate["raw_artifact_sha"], candidate["source_order"],
        )
        for candidate in candidates
    ]
    if [tuple(row) for row in staged] != expected_staging:
        raise AuditMigrationError("router_candidate_staging_incomplete")
    source_set_values, routes, phase_values = _router_revalidated_final_phase(
        conn, round_row, round_material, authority, budget
    )
    route_by_candidate = {route["candidate_id"]: route for route in routes}
    phase_by_candidate = {values[3]: values for values in phase_values}
    selected_id = plan["candidate"]["candidate_id"]
    selected = route_by_candidate.get(selected_id)
    if selected is None or (
        selected["matched_rule_ids"] != plan["matched_router_rule_ids"]
        or selected["risk_policy_version"] != plan["risk_policy_version"]
    ):
        raise AuditMigrationError("selected_route_identity_mismatch")
    if _legacy_router_authority_exists(conn, run_id=plan["run_id"]):
        raise AuditMigrationError("legacy candidate route authority is quarantined")
    risk_policy = authority["risk_policy"]
    risk_policy_sha = _semantic_sha("history-risk-policy-v1", risk_policy)
    slice_policy, slice_policy_sha = _router_risk_slice_policy()
    if (
        risk_policy_sha != round_material["risk_policy_sha"]
        or slice_policy_sha != round_material["risk_slice_policy_sha"]
    ):
        raise AuditMigrationError("router_round_policy_mismatch")
    cohort_material = {
        "run_id": plan["run_id"], "batch_id": plan["batch_id"],
        "intent": plan["intent"], "candidate_ids": candidate_ids,
        "risk_policy_sha256": risk_policy_sha,
        "risk_slice_policy_sha256": slice_policy_sha,
        "created_at": created_at,
    }
    cohort_sha = _semantic_sha(
        "history-candidate-route-cohort-v2", cohort_material
    )
    cohort_values = (
        plan["run_id"], plan["batch_id"], plan["intent"],
        _semantic_canonical(candidate_ids), _semantic_canonical(risk_policy),
        risk_policy_sha, _semantic_canonical(slice_policy), slice_policy_sha,
        cohort_sha, created_at,
    )
    prepared = []
    for candidate_id in candidate_ids:
        route = route_by_candidate[candidate_id]
        phase_values_for_candidate = phase_by_candidate[candidate_id]
        route_material = {
            "run_id": plan["run_id"], "candidate_id": candidate_id,
            "intent": plan["intent"], "cohort_sha256": cohort_sha,
            "router_facts": route["router_facts"],
            "risk_slices": route["risk_slices"],
            "matched_rule_ids": route["matched_rule_ids"],
            "route": route["route"],
            "call_l1_model": route["call_l1_model"],
            "dispatch_allowed": route["dispatch_allowed"],
            "rule_table_sha256": route["rule_table_sha256"],
            "risk_policy_version": route["risk_policy_version"],
            "created_at": created_at,
        }
        route_sha = _semantic_sha(
            "history-candidate-route-fact-v2", route_material
        )
        route_values = (
            plan["run_id"], candidate_id, plan["intent"], cohort_sha,
            _semantic_canonical(route["router_facts"]),
            _semantic_canonical(route["risk_slices"]),
            _semantic_canonical(route["matched_rule_ids"]), route["route"],
            int(route["call_l1_model"]), int(route["dispatch_allowed"]),
            route["rule_table_sha256"], route["risk_policy_version"],
            route_sha, created_at,
        )
        observation_material = {
            "run_id": plan["run_id"], "candidate_id": candidate_id,
            "route_fact_sha256": route_sha,
            "observation_scope": "host_issued_shadow",
            "production_authority": False, "created_at": created_at,
        }
        observation_values = (
            plan["run_id"], candidate_id, route_sha, "host_issued_shadow", 0,
            _semantic_sha(
                "history-candidate-route-observation-boundary-v1",
                observation_material,
            ),
            created_at,
        )
        binding_values = (
            plan["run_id"], candidate_id, route_sha,
            phase_values_for_candidate[0], source_set_values[0], created_at,
        )
        prepared.append((route_values, observation_values, binding_values))
    counts = (
        conn.execute(
            "SELECT count(*) FROM audit_candidate_route_cohorts_v2 "
            "WHERE run_id=?", (plan["run_id"],)
        ).fetchone()[0],
        conn.execute(
            "SELECT count(*) FROM audit_candidate_route_facts_v2 "
            "WHERE run_id=?", (plan["run_id"],)
        ).fetchone()[0],
        conn.execute(
            "SELECT count(*) FROM audit_candidate_route_observation_boundaries_v2 "
            "WHERE run_id=?", (plan["run_id"],)
        ).fetchone()[0],
        conn.execute(
            "SELECT count(*) FROM audit_candidate_route_source_bindings_v2 "
            "WHERE run_id=?", (plan["run_id"],)
        ).fetchone()[0],
    )
    expected_complete = (1, len(candidates), len(candidates), len(candidates))
    if counts not in {(0, 0, 0, 0), expected_complete}:
        raise AuditMigrationError("candidate_route_materialization_partial")
    if counts == expected_complete:
        cohort = conn.execute(
            "SELECT * FROM audit_candidate_route_cohorts_v2 WHERE run_id=?",
            (plan["run_id"],),
        ).fetchone()
        route_rows = conn.execute(
            "SELECT * FROM audit_candidate_route_facts_v2 WHERE run_id=? "
            "ORDER BY candidate_id", (plan["run_id"],)
        ).fetchall()
        observation_rows = conn.execute(
            "SELECT * FROM audit_candidate_route_observation_boundaries_v2 "
            "WHERE run_id=? ORDER BY candidate_id", (plan["run_id"],)
        ).fetchall()
        binding_rows = conn.execute(
            "SELECT * FROM audit_candidate_route_source_bindings_v2 "
            "WHERE run_id=? ORDER BY candidate_id", (plan["run_id"],)
        ).fetchall()
        if (
            tuple(cohort) != cohort_values
            or [tuple(row) for row in route_rows]
                != [item[0] for item in prepared]
            or [tuple(row) for row in observation_rows]
                != [item[1] for item in prepared]
            or [tuple(row) for row in binding_rows]
                != [item[2] for item in prepared]
        ):
            raise AuditMigrationError("candidate_route_materialization_conflict")
    else:
        cost_guard = _COST_FACT_GUARDS.get(id(conn))
        router_guard = _ROUTER_SOURCE_GUARDS.get(id(conn))
        if (
            cost_guard is None or router_guard is None
            or any(
                cost_guard[name] is not None
                for name in ("cohort", "route", "route_observation")
            )
            or router_guard["binding"] is not None
        ):
            raise AuditMigrationError("candidate route guard is unavailable")
        cost_guard["cohort"] = cohort_values
        try:
            conn.execute(
                "INSERT INTO audit_candidate_route_cohorts_v2 "
                "VALUES(?,?,?,?,?,?,?,?,?,?)", cohort_values,
            )
        finally:
            cost_guard["cohort"] = None
        for route_values, observation_values, binding_values in prepared:
            cost_guard["route"] = route_values
            try:
                conn.execute(
                    "INSERT INTO audit_candidate_route_facts_v2 "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", route_values,
                )
            finally:
                cost_guard["route"] = None
            cost_guard["route_observation"] = observation_values
            try:
                conn.execute(
                    "INSERT INTO audit_candidate_route_observation_boundaries_v2 "
                    "VALUES(?,?,?,?,?,?,?)", observation_values,
                )
            finally:
                cost_guard["route_observation"] = None
            router_guard["binding"] = binding_values
            try:
                conn.execute(
                    "INSERT INTO audit_candidate_route_source_bindings_v2 "
                    "VALUES(?,?,?,?,?,?)", binding_values,
                )
            finally:
                router_guard["binding"] = None
    return {
        "cohort_sha256": cohort_sha,
        "route_fact_sha256": next(
            item[0][12] for item in prepared if item[0][1] == selected_id
        ),
    }


def _candidate_route_materialization_replay_matches(
    conn, plan_sha, *, created_at
):
    """Verify the complete durable final projection without reopening issuance."""
    try:
        plan_row = conn.execute(
            "SELECT * FROM audit_l2_plans_v2 WHERE plan_sha=?", (plan_sha,)
        ).fetchone()
        if plan_row is None or plan_row["created_at"] != created_at:
            return False
        plan = history_audit_plan.validate_runtime_plan_material(
            _closed_json(plan_row["plan_json"])
        )
        if history_audit_plan.runtime_plan_sha_from_material(plan) != plan_sha:
            return False
        if _legacy_router_authority_exists(conn, plan_sha=plan_sha):
            return False
        round_rows = conn.execute(
            "SELECT * FROM audit_router_rounds_v2 "
            "WHERE run_id=? AND batch_id=? AND intent=?",
            (plan["run_id"], plan["batch_id"], plan["intent"]),
        ).fetchall()
        if len(round_rows) != 1:
            return False
        round_row = round_rows[0]
        round_material = _router_closed_json(round_row["round_json"])
        candidates = round_material["candidates"]
        candidate_ids = [candidate["candidate_id"] for candidate in candidates]
        if (
            _router_round_sha(round_material)
                != round_row["route_round_sha256"]
            or candidate_ids != plan["snapshot"]["current_batch_ids"]
            or _semantic_canonical(candidate_ids)
                != round_row["candidate_ids_json"]
            or round_material["run_id"] != plan["run_id"]
            or round_material["batch_id"] != plan["batch_id"]
            or round_material["intent"] != plan["intent"]
            or round_material["risk_policy_sha"] != plan["risk_policy_sha"]
            or round_material["budget_policy_sha"] != plan["budget_policy_sha"]
            or any(
                round_material["snapshot"][name] != plan["snapshot"][name]
                for name in _ROUTER_SNAPSHOT_FIELDS
            )
            or [
                candidate for candidate in candidates
                if candidate["candidate_id"] == plan["candidate"]["candidate_id"]
            ] != [plan["candidate"]]
        ):
            return False
        staged = conn.execute(
            "SELECT staging_candidate_id,candidate_hash,raw_artifact_sha,source_order "
            "FROM audit_batch_staging WHERE run_id=? AND batch_id=? "
            "ORDER BY staging_candidate_id",
            (plan["run_id"], plan["batch_id"]),
        ).fetchall()
        if [tuple(row) for row in staged] != [
            (
                candidate["candidate_id"], candidate["candidate_hash"],
                candidate["raw_artifact_sha"], candidate["source_order"],
            )
            for candidate in candidates
        ]:
            return False
        cohort = conn.execute(
            "SELECT * FROM audit_candidate_route_cohorts_v2 WHERE run_id=?",
            (plan["run_id"],),
        ).fetchone()
        if cohort is None:
            return False
        risk_policy = _router_closed_json(cohort["risk_policy_json"])
        slice_policy = _router_closed_json(cohort["risk_slice_policy_json"])
        risk_policy_sha = _semantic_sha("history-risk-policy-v1", risk_policy)
        expected_slice_policy, slice_policy_sha = _router_risk_slice_policy()
        cohort_material = {
            "run_id": plan["run_id"], "batch_id": plan["batch_id"],
            "intent": plan["intent"], "candidate_ids": candidate_ids,
            "risk_policy_sha256": risk_policy_sha,
            "risk_slice_policy_sha256": slice_policy_sha,
            "created_at": created_at,
        }
        cohort_sha = _semantic_sha(
            "history-candidate-route-cohort-v2", cohort_material
        )
        expected_cohort = (
            plan["run_id"], plan["batch_id"], plan["intent"],
            _semantic_canonical(candidate_ids), _semantic_canonical(risk_policy),
            risk_policy_sha, _semantic_canonical(expected_slice_policy),
            slice_policy_sha, cohort_sha, created_at,
        )
        if (
            tuple(cohort) != expected_cohort
            or risk_policy_sha != plan["risk_policy_sha"]
            or slice_policy != expected_slice_policy
        ):
            return False
        source_set = conn.execute(
            "SELECT * FROM audit_router_source_sets_v2 "
            "WHERE route_round_sha256=? AND phase='final'",
            (round_row["route_round_sha256"],),
        ).fetchone()
        if source_set is None:
            return False
        refs = _router_closed_json(source_set["source_refs_json"])
        events = _router_closed_json(
            source_set["dependency_head_events_json"]
        )
        if tuple(source_set) != _router_source_set_values(
            round_row["route_round_sha256"], "final", refs,
            source_set["budget_fact_sha256"], events,
            source_set["created_at"],
        ):
            return False
        budget = conn.execute(
            "SELECT budget_fact_sha256,candidate_budget_decision "
            "FROM audit_router_budget_facts_v2 WHERE route_round_sha256=?",
            (round_row["route_round_sha256"],),
        ).fetchone()
        if budget is None or tuple(budget) != (
            source_set["budget_fact_sha256"], "accepted"
        ):
            return False
        phase_rows = conn.execute(
            "SELECT * FROM audit_router_phase_facts_v2 "
            "WHERE route_round_sha256=? AND phase='final' ORDER BY candidate_id",
            (round_row["route_round_sha256"],),
        ).fetchall()
        if [row["candidate_id"] for row in phase_rows] != candidate_ids:
            return False
        expected_routes = []
        expected_observations = []
        expected_bindings = []
        for phase in phase_rows:
            route = {
                "candidate_id": phase["candidate_id"],
                "router_facts": _router_closed_json(
                    phase["router_facts_json"]
                ),
                "risk_slices": _router_closed_json(
                    phase["risk_slices_json"]
                ),
                "matched_rule_ids": _router_closed_json(
                    phase["matched_rule_ids_json"]
                ),
                "route": phase["route"],
                "call_l1_model": bool(phase["call_l1_model"]),
                "dispatch_allowed": bool(phase["dispatch_allowed"]),
                "release_authorized": bool(phase["release_authorized"]),
                "rule_table_sha256": phase["rule_table_sha256"],
                "risk_policy_version": phase["risk_policy_version"],
            }
            if tuple(phase) != _router_phase_fact_values(
                round_row["route_round_sha256"], "final",
                source_set["source_set_sha256"], route, phase["created_at"],
            ):
                return False
            if phase["candidate_id"] == plan["candidate"]["candidate_id"] and (
                route["matched_rule_ids"] != plan["matched_router_rule_ids"]
                or route["risk_policy_version"] != plan["risk_policy_version"]
            ):
                return False
            route_material = {
                "run_id": plan["run_id"],
                "candidate_id": phase["candidate_id"],
                "intent": plan["intent"], "cohort_sha256": cohort_sha,
                "router_facts": route["router_facts"],
                "risk_slices": route["risk_slices"],
                "matched_rule_ids": route["matched_rule_ids"],
                "route": route["route"],
                "call_l1_model": route["call_l1_model"],
                "dispatch_allowed": route["dispatch_allowed"],
                "rule_table_sha256": route["rule_table_sha256"],
                "risk_policy_version": route["risk_policy_version"],
                "created_at": created_at,
            }
            route_sha = _semantic_sha(
                "history-candidate-route-fact-v2", route_material
            )
            expected_routes.append(
                (
                    plan["run_id"], phase["candidate_id"], plan["intent"],
                    cohort_sha, _semantic_canonical(route["router_facts"]),
                    _semantic_canonical(route["risk_slices"]),
                    _semantic_canonical(route["matched_rule_ids"]),
                    route["route"], int(route["call_l1_model"]),
                    int(route["dispatch_allowed"]), route["rule_table_sha256"],
                    route["risk_policy_version"], route_sha, created_at,
                )
            )
            observation_material = {
                "run_id": plan["run_id"],
                "candidate_id": phase["candidate_id"],
                "route_fact_sha256": route_sha,
                "observation_scope": "host_issued_shadow",
                "production_authority": False, "created_at": created_at,
            }
            expected_observations.append(
                (
                    plan["run_id"], phase["candidate_id"], route_sha,
                    "host_issued_shadow", 0,
                    _semantic_sha(
                        "history-candidate-route-observation-boundary-v1",
                        observation_material,
                    ),
                    created_at,
                )
            )
            expected_bindings.append(
                (
                    plan["run_id"], phase["candidate_id"], route_sha,
                    phase["phase_fact_sha256"],
                    source_set["source_set_sha256"], created_at,
                )
            )
        routes = conn.execute(
            "SELECT * FROM audit_candidate_route_facts_v2 WHERE run_id=? "
            "ORDER BY candidate_id", (plan["run_id"],)
        ).fetchall()
        observations = conn.execute(
            "SELECT * FROM audit_candidate_route_observation_boundaries_v2 "
            "WHERE run_id=? ORDER BY candidate_id", (plan["run_id"],)
        ).fetchall()
        bindings = conn.execute(
            "SELECT * FROM audit_candidate_route_source_bindings_v2 "
            "WHERE run_id=? ORDER BY candidate_id", (plan["run_id"],)
        ).fetchall()
        return (
            [tuple(row) for row in routes] == expected_routes
            and [tuple(row) for row in observations] == expected_observations
            and [tuple(row) for row in bindings] == expected_bindings
        )
    except (
        AuditMigrationError, KeyError, TypeError, ValueError,
        history_audit_plan.AuditPlanError,
    ):
        return False


def _semantic_closed_json(text):
    if not isinstance(text, str):
        raise ValueError("semantic canonical JSON must be text")
    return _closed_json(text if text.endswith("\n") else text + "\n")


def _semantic_plan_dependencies(plan):
    provider_hashes = sorted(
        plan["provider_capability_profile_hashes"].values()
    )
    if not provider_hashes or len(set(provider_hashes)) != len(provider_hashes):
        raise ValueError("production provider evidence is invalid")
    return {
        "plan": plan["plan_sha"],
        "prompt": plan["capacity_profile"]["prompt"]["sha256"],
        "schema": plan["capacity_profile"]["schema"]["sha256"],
        "ordered_provider_pools": history_contract_v2.framed_sha256(
            "history-provider-pools-v2",
            history_contract_v2.canonical_bytes(
                plan["provider_pools_ordered"]
            ),
        ),
        "capacity": _semantic_sha(
            "history-capacity-profile-v1", plan["capacity_profile"]
        ),
        "provider": history_contract_v2.framed_sha256(
            "history-provider-capabilities-v2",
            history_contract_v2.canonical_bytes(provider_hashes),
        ),
        "provider_profile_hashes": provider_hashes,
    }


def _router_historical_dependency_head_events(
    conn, dependency_heads, derivation_inputs,
):
    try:
        events = derivation_inputs["dependency_head_events"]
    except (KeyError, TypeError) as exc:
        raise AuditMigrationError(
            "production host router source authority is invalid"
        ) from exc
    if (
        not isinstance(events, list)
        or len(events) != len(dependency_heads)
        or events != sorted(events, key=lambda item: item.get(
            "dependency_kind", ""
        ))
    ):
        raise AuditMigrationError(
            "production host router source authority is invalid"
        )
    seen = set()
    for event in events:
        if (
            not isinstance(event, dict)
            or set(event) != {
                "dependency_kind", "sequence", "head_event_id",
                "dependency_sha256",
            }
            or event["dependency_kind"] in seen
            or dependency_heads.get(event["dependency_kind"])
                != event["dependency_sha256"]
            or type(event["sequence"]) is not int
            or event["sequence"] < 1
            or not _router_is_sha(event["head_event_id"])
        ):
            raise AuditMigrationError(
                "production host router source authority is invalid"
            )
        row = conn.execute(
            "SELECT dependency_kind,sequence,head_event_id,"
            "dependency_sha256 FROM "
            "audit_semantic_dependency_head_events_v2 "
            "WHERE head_event_id=?",
            (event["head_event_id"],),
        ).fetchone()
        if row is None or tuple(row) != (
            event["dependency_kind"], event["sequence"],
            event["head_event_id"], event["dependency_sha256"],
        ):
            raise AuditMigrationError(
                "production host router source authority is invalid"
            )
        seen.add(event["dependency_kind"])
    if seen != set(dependency_heads):
        raise AuditMigrationError(
            "production host router source authority is invalid"
        )
    return copy.deepcopy(events)


def _require_host_router_preplan_chain(
    conn, plan, *, require_current_dependencies=True,
):
    preplan = conn.execute(
        "SELECT * FROM audit_router_host_preplan_batches_v2 "
        "WHERE run_id=? AND batch_id=? AND intent=?",
        (plan["run_id"], plan["batch_id"], plan["intent"]),
    ).fetchone()
    round_rows = conn.execute(
        "SELECT * FROM audit_router_rounds_v2 WHERE run_id=? "
        "AND batch_id=? AND intent=?",
        (plan["run_id"], plan["batch_id"], plan["intent"]),
    ).fetchall()
    if (
        preplan is None
        or _router_host_preplan_row_valid(*tuple(preplan)) != 1
        or len(round_rows) != 1
    ):
        raise AuditMigrationError(
            "production host router authority is unavailable"
        )
    round_row = round_rows[0]
    route_sha = round_row["route_round_sha256"]
    observation_row = conn.execute(
        "SELECT * FROM audit_router_host_observation_sets_v2 "
        "WHERE route_round_sha256=?", (route_sha,),
    ).fetchone()
    host_round = conn.execute(
        "SELECT * FROM audit_router_host_round_authorities_v2 "
        "WHERE route_round_sha256=?", (route_sha,),
    ).fetchone()
    if observation_row is None or host_round is None:
        raise AuditMigrationError(
            "production host router authority is unavailable"
        )
    try:
        round_material, authority = _router_validate_round_material(
            _router_closed_json(round_row["round_json"])
        )
        observations = _router_validate_host_observations(
            round_material,
            _router_closed_json(observation_row["observations_json"]),
        )
    except (TypeError, ValueError) as exc:
        raise AuditMigrationError(
            "production host router authority is invalid"
        ) from exc
    candidate_ids = [
        item["candidate_id"] for item in round_material["candidates"]
    ]
    plan_snapshot = {
        name: plan["snapshot"][name] for name in _ROUTER_SNAPSHOT_FIELDS
    }
    try:
        preplan_snapshot = _router_closed_json(preplan["snapshot_json"])
        preplan_candidates = _router_closed_json(preplan["candidates_json"])
    except (TypeError, ValueError) as exc:
        raise AuditMigrationError(
            "production host router preplan authority is invalid"
        ) from exc
    if (
        _router_round_sha(round_material) != route_sha
        or _router_host_observation_row_valid(*tuple(observation_row)) != 1
        or _router_host_round_authority_row_valid(*tuple(host_round)) != 1
        or host_round["observation_set_sha256"]
            != observation_row["observation_set_sha256"]
        or authority.get("private_test_authority") is True
        or round_material["run_id"] != plan["run_id"]
        or round_material["batch_id"] != plan["batch_id"]
        or round_material["intent"] != plan["intent"]
        or round_material["snapshot"] != plan_snapshot
        or preplan_snapshot != round_material["snapshot"]
        or preplan_candidates != round_material["candidates"]
        or preplan["records_sha256"] != plan["snapshot"]["records_sha"]
        or candidate_ids != plan["snapshot"]["current_batch_ids"]
        or [
            item for item in round_material["candidates"]
            if item["candidate_id"] == plan["candidate"]["candidate_id"]
        ] != [plan["candidate"]]
        or round_material["risk_policy_sha"] != plan["risk_policy_sha"]
        or round_material["budget_policy_sha"] != plan["budget_policy_sha"]
        or round_material["semantic_policy_profile_id"]
            != plan["semantic_policy_profile_id"]
    ):
        raise AuditMigrationError(
            "production host router authority is invalid"
        )
    budget = conn.execute(
        "SELECT * FROM audit_router_budget_facts_v2 "
        "WHERE route_round_sha256=?", (route_sha,),
    ).fetchone()
    source_set = conn.execute(
        "SELECT * FROM audit_router_source_sets_v2 "
        "WHERE route_round_sha256=? AND phase='final'", (route_sha,),
    ).fetchone()
    if budget is None or source_set is None:
        raise AuditMigrationError(
            "production host router source authority is unavailable"
        )
    try:
        refs = _router_closed_json(source_set["source_refs_json"])
    except (TypeError, ValueError) as exc:
        raise AuditMigrationError(
            "production host router source authority is invalid"
        ) from exc
    if set(refs) != set(_ROUTER_SOURCE_KINDS) or len(refs) != 7:
        raise AuditMigrationError(
            "production host router source authority is incomplete"
        )
    stored_sources = {}
    dependency_heads = None
    for source_kind in sorted(_ROUTER_SOURCE_KINDS):
        row = conn.execute(
            "SELECT * FROM audit_router_domain_sources_v2 "
            "WHERE route_round_sha256=? AND source_kind=?",
            (route_sha, source_kind),
        ).fetchone()
        host_source = conn.execute(
            "SELECT * FROM audit_router_host_source_authorities_v2 "
            "WHERE route_round_sha256=? AND source_kind=?",
            (route_sha, source_kind),
        ).fetchone()
        if row is None or host_source is None:
            raise AuditMigrationError(
                "production host router source authority is unavailable"
            )
        try:
            source = _router_validate_domain_source(
                round_material, route_sha, source_kind,
                _router_closed_json(row["source_json"]),
            )
        except (TypeError, ValueError) as exc:
            raise AuditMigrationError(
                "production host router source authority is invalid"
            ) from exc
        if (
            refs[source_kind] != row["source_sha256"]
            or row["source_sha256"] != _router_domain_source_sha(
                route_sha, source_kind, source
            )
            or _router_host_source_authority_row_valid(
                *tuple(host_source)
            ) != 1
            or host_source["source_sha256"] != row["source_sha256"]
            or host_source["observation_set_sha256"]
                != observation_row["observation_set_sha256"]
        ):
            raise AuditMigrationError(
                "production host router source authority is invalid"
            )
        stored_sources[source_kind] = (source, row, host_source)
        if source_kind == "dependency_heads":
            dependency_heads = source["heads"]
    if dependency_heads is None:
        raise AuditMigrationError(
            "production host router source authority is invalid"
        )
    historical_events = None
    if require_current_dependencies:
        if _current_semantic_dependency_heads(
            conn, dependency_heads
        ) != dependency_heads:
            raise AuditMigrationError("router_source_dependency_drift")
    else:
        try:
            dependency_inputs = _router_closed_json(
                stored_sources["dependency_heads"][2][
                    "derivation_inputs_json"
                ]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AuditMigrationError(
                "production host router source authority is invalid"
            ) from exc
        historical_events = _router_historical_dependency_head_events(
            conn, dependency_heads, dependency_inputs
        )
    expected_sources, expected_inputs = _router_host_sources(
        conn, round_material, route_sha, observations,
        observation_row["observation_set_sha256"], "final",
        dependency_heads,
        dependency_head_events=historical_events,
        observed_at=(
            None if require_current_dependencies
            else stored_sources["qualification"][2]["issued_at"]
        ),
    )
    if set(expected_sources) != set(_ROUTER_SOURCE_KINDS):
        raise AuditMigrationError(
            "production host router source authority is incomplete"
        )
    for source_kind in sorted(_ROUTER_SOURCE_KINDS):
        source, row, host_source = stored_sources[source_kind]
        expected_source = _router_validate_domain_source(
            round_material, route_sha, source_kind,
            expected_sources[source_kind],
        )
        expected_authority = _router_host_source_authority_values(
            row["source_sha256"], route_sha, source_kind,
            observation_row["observation_set_sha256"],
            expected_inputs[source_kind], row["created_at"],
        )
        if source != expected_source or tuple(host_source) != expected_authority:
            raise AuditMigrationError(
                "production host router source derivation mismatch"
            )
    _, routes, _ = _router_revalidated_final_phase(
        conn, round_row, round_material, authority, budget,
        require_current_dependencies=require_current_dependencies,
    )
    selected = next(
        (
            route for route in routes
            if route["candidate_id"] == plan["candidate"]["candidate_id"]
        ),
        None,
    )
    if (
        selected is None
        or not selected["dispatch_allowed"]
        or selected["matched_rule_ids"]
            != plan["matched_router_rule_ids"]
        or selected["risk_policy_version"] != plan["risk_policy_version"]
    ):
        raise AuditMigrationError(
            "production host router dispatch is unavailable"
        )
    if _legacy_router_authority_exists(conn, run_id=plan["run_id"]):
        raise AuditMigrationError(
            "production host router chain is invalid"
        )
    return route_sha


def _require_host_router_production_chain(conn, plan, *, created_at):
    route_sha = _require_host_router_preplan_chain(
        conn, plan, require_current_dependencies=False
    )
    try:
        plan_sha = history_audit_plan.runtime_plan_sha_from_material(plan)
    except history_audit_plan.AuditPlanError as exc:
        raise AuditMigrationError(
            "production host router chain is invalid"
        ) from exc
    if not _candidate_route_materialization_replay_matches(
        conn, plan_sha, created_at=created_at
    ) or not candidate_l2_dispatch_replay_matches(
        conn, plan_sha, created_at=created_at
    ):
        raise AuditMigrationError(
            "production host router chain is invalid"
        )
    return route_sha


def _durable_semantic_production_plan(conn, plan_sha):
    if not _router_is_sha(plan_sha):
        raise ValueError("production plan evidence is invalid")
    row = conn.execute(
        """
        SELECT plan.*,run.manifest_schema_version,run.plan_hash,
               run.manifest_json,records.records_sha,records.records_json,
               snapshot.run_id AS snapshot_run_id,
               snapshot.batch_id AS snapshot_batch_id,
               snapshot.snapshot_hash AS durable_snapshot_hash,
               candidate.run_id AS candidate_run_id,
               candidate.candidate_hash AS durable_candidate_hash,
               candidate.raw_artifact_sha, candidate.source_order
        FROM audit_l2_plans_v2 plan
        JOIN audit_run_manifests run ON run.run_id=plan.run_id
        JOIN audit_l2_snapshot_records_v2 records
          ON records.snapshot_id=plan.snapshot_id
        JOIN audit_snapshots snapshot ON snapshot.snapshot_id=plan.snapshot_id
        JOIN audit_batch_staging candidate
          ON candidate.staging_candidate_id=plan.candidate_id
        WHERE plan.plan_sha=?
        """,
        (plan_sha,),
    ).fetchone()
    if row is None:
        raise ValueError("production plan evidence is unavailable")
    try:
        plan = history_audit_plan.validate_runtime_plan_material(
            _closed_json(row["plan_json"])
        )
        replayed_sha = history_audit_plan.runtime_plan_sha_from_material(plan)
        records = history_audit_plan.runtime_snapshot_records(
            _closed_json(row["records_json"])
        )
    except (TypeError, ValueError, history_audit_plan.AuditPlanError) as exc:
        raise ValueError("production plan evidence is invalid") from exc
    expected_plan_row = (
        replayed_sha, plan["run_id"], plan["candidate"]["candidate_id"],
        plan["candidate"]["candidate_hash"], plan["snapshot"]["snapshot_id"],
        plan["snapshot"]["snapshot_hash"], plan["shard_plan_sha"],
        plan["budget_policy_sha"], plan["intent"], row["plan_json"],
        row["created_at"],
    )
    stored_plan_row = tuple(row[name] for name in (
        "plan_sha", "run_id", "candidate_id", "candidate_hash",
        "snapshot_id", "snapshot_hash", "shard_plan_sha",
        "budget_policy_sha", "intent", "plan_json", "created_at",
    ))
    if (
        plan["authority_scope"] != "production"
        or replayed_sha != plan_sha
        or stored_plan_row != expected_plan_row
        or row["manifest_schema_version"] != "history-audit-manifest-v2"
        or row["plan_hash"] != plan_sha
        or row["manifest_json"] != row["plan_json"]
        or row["records_sha"]
            != history_audit_plan.runtime_snapshot_records_sha(records)
        or sorted(item["item_id"] for item in records)
            != plan["snapshot"]["expected_asset_ids"]
        or row["snapshot_run_id"] != plan["run_id"]
        or row["snapshot_batch_id"] != plan["batch_id"]
        or row["durable_snapshot_hash"] != plan["snapshot"]["snapshot_hash"]
        or row["candidate_run_id"] != plan["run_id"]
        or row["durable_candidate_hash"] != plan["candidate"]["candidate_hash"]
        or row["raw_artifact_sha"] != plan["candidate"]["raw_artifact_sha"]
        or row["source_order"] != plan["candidate"]["source_order"]
        or not _accepted_candidate_budget_receipt_matches(conn, plan)
    ):
        raise ValueError("production plan evidence is invalid")
    dependencies = _semantic_plan_dependencies(dict(plan, plan_sha=plan_sha))
    capacity_row = conn.execute(
        "SELECT profile_sha256,profile_json FROM audit_capacity_profiles "
        "WHERE capacity_profile_id=?", (plan["capacity_profile_id"],),
    ).fetchone()
    if capacity_row is None:
        raise ValueError("production capacity evidence is unavailable")
    try:
        registered_capacity = _closed_json(capacity_row["profile_json"])
    except (TypeError, ValueError) as exc:
        raise ValueError("production capacity evidence is invalid") from exc
    if (
        registered_capacity != plan["capacity_profile"]
        or capacity_row["profile_sha256"] != dependencies["capacity"]
    ):
        raise ValueError("production capacity evidence is invalid")
    for provider, capability in sorted(plan["provider_capabilities"].items()):
        provider_row = conn.execute(
            "SELECT provider,profile_json FROM audit_provider_profiles "
            "WHERE profile_hash=?",
            (capability["capability_profile_hash"],),
        ).fetchone()
        if provider_row is None:
            raise ValueError("production provider evidence is unavailable")
        try:
            registered_provider = _closed_json(provider_row["profile_json"])
        except (TypeError, ValueError) as exc:
            raise ValueError("production provider evidence is invalid") from exc
        if (
            provider_row["provider"] != provider
            or registered_provider != capability
        ):
            raise ValueError("production provider evidence is invalid")
    try:
        _require_host_router_production_chain(
            conn, plan, created_at=row["created_at"]
        )
    except AuditMigrationError as exc:
        raise ValueError(str(exc)) from exc
    return dict(plan, plan_sha=plan_sha), dependencies


def _semantic_production_result(value):
    if (
        not isinstance(value, dict)
        or set(value) != {
            "state_sha256", "completion_count", "integrity_fault_count"
        }
        or not _router_is_sha(value["state_sha256"])
        or type(value["completion_count"]) is not int
        or value["completion_count"] < 0
        or type(value["integrity_fault_count"]) is not int
        or value["integrity_fault_count"] < 0
    ):
        raise ValueError("semantic production evidence result is invalid")
    return copy.deepcopy(value)


_PRODUCTION_EVIDENCE_ROOT_FIELDS = frozenset({
    "schema_version", "evidence_kind", "report_sha256", "plan_sha",
    "capacity_profile_id", "capacity_sha256", "provider_profile_hashes",
    "provider_sha256", "ordered_provider_pools_sha256", "prompt_sha256",
    "schema_sha256", "issuer_id", "expires_at",
})

_SEMANTIC_EVALUATION_ROOT_FIELDS = frozenset({
    "schema_version", "qrels_hash", "evaluation_hash",
    "metric_report_hash", "plan_sha", "corpus_snapshot_hash",
    "semantic_policy_profile_id", "policy_sha256", "no_match_basis",
    "scope", "issuer_id", "expires_at",
})


def _load_production_evidence_roots():
    """Load and validate the repository-owned production evidence roots."""
    try:
        registry = history_audit_plan._load_host_policy(
            "production-evidence-roots-v1.json"
        )
    except history_audit_plan.AuditPlanError as exc:
        raise ValueError("production evidence root registry is invalid") from exc
    if (
        not isinstance(registry, dict)
        or set(registry) != {
            "schema_version", "registry_revision",
            "fault_reports", "replay_reports",
            "semantic_evaluation_reports",
        }
        or registry.get("schema_version")
            != "history-production-evidence-roots-v1"
        or not isinstance(registry.get("registry_revision"), str)
        or not registry["registry_revision"]
        or not isinstance(registry.get("fault_reports"), list)
        or not isinstance(registry.get("replay_reports"), list)
        or not isinstance(registry.get("semantic_evaluation_reports"), list)
    ):
        raise ValueError("production evidence root registry is invalid")
    seen = set()
    for kind in ("fault", "replay"):
        for entry in registry[f"{kind}_reports"]:
            if (
                not isinstance(entry, dict)
                or set(entry) != _PRODUCTION_EVIDENCE_ROOT_FIELDS
                or entry.get("schema_version")
                    != "history-production-evidence-root-v1"
                or entry.get("evidence_kind") != kind
                or not isinstance(entry.get("capacity_profile_id"), str)
                or not entry["capacity_profile_id"]
                or not isinstance(entry.get("issuer_id"), str)
                or not entry["issuer_id"]
            ):
                raise ValueError("production evidence root registry is invalid")
            sha_fields = (
                "report_sha256", "plan_sha", "capacity_sha256",
                "provider_sha256", "ordered_provider_pools_sha256",
                "prompt_sha256", "schema_sha256",
            )
            provider_hashes = entry.get("provider_profile_hashes")
            if (
                any(not _router_is_sha(entry.get(name)) for name in sha_fields)
                or not isinstance(provider_hashes, list)
                or not provider_hashes
                or provider_hashes != sorted(provider_hashes)
                or len(set(provider_hashes)) != len(provider_hashes)
                or any(not _router_is_sha(value) for value in provider_hashes)
            ):
                raise ValueError("production evidence root registry is invalid")
            try:
                _semantic_timestamp(entry["expires_at"], "expires_at")
                root_sha = _semantic_sha(
                    "history-production-evidence-root-v1", entry
                )
            except (
                KeyError, TypeError, ValueError,
                history_contract_v2.ContractV2Error,
            ) as exc:
                raise ValueError(
                    "production evidence root registry is invalid"
                ) from exc
            if root_sha in seen:
                raise ValueError("production evidence root registry is invalid")
            seen.add(root_sha)
    for entry in registry["semantic_evaluation_reports"]:
        if (
            not isinstance(entry, dict)
            or set(entry) != _SEMANTIC_EVALUATION_ROOT_FIELDS
            or entry.get("schema_version")
                != "history-semantic-evaluation-root-v1"
            or entry.get("no_match_basis") not in {
                "l1_calibrated", "l2_exhaustive"
            }
            or entry.get("scope") not in {
                "real", "production", "real_qrels"
            }
            or not isinstance(entry.get("semantic_policy_profile_id"), str)
            or not entry["semantic_policy_profile_id"]
            or not isinstance(entry.get("issuer_id"), str)
            or not entry["issuer_id"]
            or any(
                not _router_is_sha(entry.get(name))
                for name in (
                    "qrels_hash", "evaluation_hash", "metric_report_hash",
                    "plan_sha", "corpus_snapshot_hash", "policy_sha256",
                )
            )
        ):
            raise ValueError("production evidence root registry is invalid")
        try:
            _semantic_timestamp(entry["expires_at"], "expires_at")
            root_sha = _semantic_sha(
                "history-semantic-evaluation-root-v1", entry
            )
        except (
            KeyError, TypeError, ValueError,
            history_contract_v2.ContractV2Error,
        ) as exc:
            raise ValueError(
                "production evidence root registry is invalid"
            ) from exc
        if root_sha in seen:
            raise ValueError("production evidence root registry is invalid")
        seen.add(root_sha)
    return copy.deepcopy(registry)


def _require_production_evidence_root(
    roots, *, kind, report_sha256, plan, dependencies,
):
    if kind not in {"fault", "replay"}:
        raise ValueError("production evidence root kind is invalid")
    expected = {
        "schema_version": "history-production-evidence-root-v1",
        "evidence_kind": kind,
        "report_sha256": report_sha256,
        "plan_sha": plan["plan_sha"],
        "capacity_profile_id": plan["capacity_profile_id"],
        "capacity_sha256": dependencies["capacity"],
        "provider_profile_hashes": dependencies["provider_profile_hashes"],
        "provider_sha256": dependencies["provider"],
        "ordered_provider_pools_sha256": dependencies[
            "ordered_provider_pools"
        ],
        "prompt_sha256": dependencies["prompt"],
        "schema_sha256": dependencies["schema"],
    }
    matches = [
        entry for entry in roots[f"{kind}_reports"]
        if all(entry[name] == value for name, value in expected.items())
    ]
    host_now = _semantic_timestamp(_utc_now(), "host_now")
    if (
        len(matches) != 1
        or _semantic_timestamp(matches[0]["expires_at"], "expires_at")
            <= host_now
    ):
        raise ValueError("production evidence root is unavailable or expired")
    return _semantic_sha(
        "history-production-evidence-root-v1", matches[0]
    )


def _require_semantic_evaluation_root(
    roots, qualification, *, expected_root_sha256=None,
):
    dependencies = _semantic_dependencies(
        qualification["dependency_hashes"]
    )
    expected = {
        "schema_version": "history-semantic-evaluation-root-v1",
        "qrels_hash": qualification["qrels_hash"],
        "evaluation_hash": qualification["evaluation_hash"],
        "metric_report_hash": qualification["metric_report_hash"],
        "plan_sha": dependencies["plan"],
        "corpus_snapshot_hash": qualification["corpus_snapshot_hash"],
        "semantic_policy_profile_id": qualification[
            "semantic_policy_profile_id"
        ],
        "policy_sha256": qualification["policy_sha256"],
        "no_match_basis": qualification["no_match_basis"],
        "scope": qualification["scope"],
        "expires_at": qualification["expires_at"],
    }
    matches = [
        entry for entry in roots["semantic_evaluation_reports"]
        if all(entry[name] == value for name, value in expected.items())
    ]
    host_now = _semantic_timestamp(_utc_now(), "host_now")
    if (
        len(matches) != 1
        or _semantic_timestamp(matches[0]["expires_at"], "expires_at")
            <= host_now
    ):
        raise ValueError(
            "semantic evaluation root is unavailable or expired"
        )
    root_sha = _semantic_sha(
        "history-semantic-evaluation-root-v1", matches[0]
    )
    if (
        expected_root_sha256 is not None
        and root_sha != expected_root_sha256
    ):
        raise ValueError("semantic evaluation root has changed")
    return root_sha


def _semantic_production_cases(kind, cases):
    if kind not in {"fault", "replay"} or not isinstance(cases, list) or not cases:
        raise ValueError("semantic production evidence cases are required")
    normalized = []
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("semantic production evidence case is invalid")
        if kind == "fault":
            expected_fields = {
                "case_id", "scenario", "baseline_result",
                "faulted_checkpoint_sha256", "recovered_result",
            }
            if (
                set(case) != expected_fields
                or case.get("scenario") != "crash_after_durable_output"
                or not _router_is_sha(case.get("faulted_checkpoint_sha256"))
            ):
                raise ValueError("semantic production evidence case is invalid")
            baseline = _semantic_production_result(case["baseline_result"])
            recovered = _semantic_production_result(case["recovered_result"])
            raw = {
                "case_id": case.get("case_id"),
                "scenario": case["scenario"],
                "baseline_result": baseline,
                "faulted_checkpoint_sha256": case[
                    "faulted_checkpoint_sha256"
                ],
                "recovered_result": recovered,
            }
            passed = (
                baseline == recovered
                and baseline["completion_count"] >= 1
                and baseline["integrity_fault_count"] == 0
                and case["faulted_checkpoint_sha256"]
                    != baseline["state_sha256"]
            )
        else:
            expected_fields = {
                "case_id", "scenario", "first_result", "replayed_result"
            }
            if (
                set(case) != expected_fields
                or case.get("scenario") != "restart_replay"
            ):
                raise ValueError("semantic production evidence case is invalid")
            first = _semantic_production_result(case["first_result"])
            replayed = _semantic_production_result(case["replayed_result"])
            raw = {
                "case_id": case.get("case_id"),
                "scenario": case["scenario"],
                "first_result": first,
                "replayed_result": replayed,
            }
            passed = (
                first == replayed
                and first["completion_count"] >= 1
                and first["integrity_fault_count"] == 0
            )
        if not isinstance(raw["case_id"], str) or not raw["case_id"]:
            raise ValueError("semantic production evidence case is invalid")
        result_sha = _semantic_sha(
            f"history-semantic-{kind}-case-result-v1", raw
        )
        normalized.append(dict(
            raw, result_sha256=result_sha,
            outcome="passed" if passed else "failed",
        ))
    normalized.sort(key=lambda item: item["case_id"])
    if len({item["case_id"] for item in normalized}) != len(normalized):
        raise ValueError("semantic production evidence case is duplicated")
    return normalized


def _semantic_production_report(kind, cases, plan_sha):
    report = {
        "schema_version": f"history-production-{kind}-evidence-v1",
        "scope": "production",
        "evidence_kind": kind,
        "plan_sha": plan_sha,
        "cases": _semantic_production_cases(kind, cases),
    }
    return report, _semantic_sha(
        f"history-semantic-{kind}-evidence-v1", report
    )


def _semantic_production_evidence_values(
    *, plan_sha, capacity_profile_id, capacity_sha256,
    provider_profile_hashes, provider_sha256,
    ordered_provider_pools_sha256, prompt_sha256, schema_sha256,
    fault_report, fault_sha256, replay_report, replay_sha256, created_at,
):
    material = {
        "schema_version": "history-semantic-production-evidence-v1",
        "plan_sha": plan_sha,
        "capacity_profile_id": capacity_profile_id,
        "capacity_sha256": capacity_sha256,
        "provider_profile_hashes": provider_profile_hashes,
        "provider_sha256": provider_sha256,
        "ordered_provider_pools_sha256": ordered_provider_pools_sha256,
        "prompt_sha256": prompt_sha256,
        "schema_sha256": schema_sha256,
        "fault_report": fault_report,
        "fault_sha256": fault_sha256,
        "replay_report": replay_report,
        "replay_sha256": replay_sha256,
        "created_at": created_at,
    }
    evidence_id = _semantic_sha(
        "history-semantic-production-evidence-v1", material
    )
    return (
        evidence_id, plan_sha, capacity_profile_id, capacity_sha256,
        _semantic_canonical(provider_profile_hashes), provider_sha256,
        ordered_provider_pools_sha256, prompt_sha256, schema_sha256,
        _semantic_canonical(fault_report), fault_sha256,
        _semantic_canonical(replay_report), replay_sha256, created_at,
    )


def _semantic_production_evidence_row_valid(row):
    try:
        provider_hashes = _semantic_closed_json(
            row["provider_profile_hashes_json"]
        )
        fault_report = _semantic_closed_json(row["fault_report_json"])
        replay_report = _semantic_closed_json(row["replay_report_json"])
        if (
            not isinstance(provider_hashes, list)
            or not provider_hashes
            or provider_hashes != sorted(provider_hashes)
            or len(set(provider_hashes)) != len(provider_hashes)
            or any(not _router_is_sha(value) for value in provider_hashes)
            or not isinstance(fault_report, dict)
            or not isinstance(replay_report, dict)
        ):
            return False
        fault_cases = [
            {key: value for key, value in case.items()
             if key not in {"result_sha256", "outcome"}}
            for case in fault_report.get("cases", [])
        ]
        replay_cases = [
            {key: value for key, value in case.items()
             if key not in {"result_sha256", "outcome"}}
            for case in replay_report.get("cases", [])
        ]
        expected_fault, fault_sha = _semantic_production_report(
            "fault", fault_cases, row["plan_sha"]
        )
        expected_replay, replay_sha = _semantic_production_report(
            "replay", replay_cases, row["plan_sha"]
        )
        provider_sha = history_contract_v2.framed_sha256(
            "history-provider-capabilities-v2",
            history_contract_v2.canonical_bytes(provider_hashes),
        )
        expected = _semantic_production_evidence_values(
            plan_sha=row["plan_sha"],
            capacity_profile_id=row["capacity_profile_id"],
            capacity_sha256=row["capacity_sha256"],
            provider_profile_hashes=provider_hashes,
            provider_sha256=provider_sha,
            ordered_provider_pools_sha256=row[
                "ordered_provider_pools_sha256"
            ],
            prompt_sha256=row["prompt_sha256"],
            schema_sha256=row["schema_sha256"],
            fault_report=expected_fault, fault_sha256=fault_sha,
            replay_report=expected_replay, replay_sha256=replay_sha,
            created_at=row["created_at"],
        )
        return tuple(row) == expected
    except (
        KeyError, TypeError, ValueError, history_contract_v2.ContractV2Error,
    ):
        return False


def issue_semantic_production_evidence(
    conn, *, plan_sha, fault_cases, replay_cases, now=None,
):
    """Derive durable production evidence from one replayed plan and raw cases."""
    if conn.in_transaction:
        raise AuditMigrationError(
            "semantic production evidence issuance requires an idle connection"
        )
    plan, dependencies = _durable_semantic_production_plan(conn, plan_sha)
    fault_report, fault_sha = _semantic_production_report(
        "fault", fault_cases, plan_sha
    )
    replay_report, replay_sha = _semantic_production_report(
        "replay", replay_cases, plan_sha
    )
    if any(
        case["outcome"] != "passed"
        for report in (fault_report, replay_report)
        for case in report["cases"]
    ):
        raise ValueError("semantic production evidence did not pass")
    roots = _load_production_evidence_roots()
    _require_production_evidence_root(
        roots, kind="fault", report_sha256=fault_sha,
        plan=plan, dependencies=dependencies,
    )
    _require_production_evidence_root(
        roots, kind="replay", report_sha256=replay_sha,
        plan=plan, dependencies=dependencies,
    )
    created_at = now or _utc_now()
    _semantic_timestamp(created_at, "created_at")
    values = _semantic_production_evidence_values(
        plan_sha=plan_sha,
        capacity_profile_id=plan["capacity_profile_id"],
        capacity_sha256=dependencies["capacity"],
        provider_profile_hashes=dependencies["provider_profile_hashes"],
        provider_sha256=dependencies["provider"],
        ordered_provider_pools_sha256=dependencies[
            "ordered_provider_pools"
        ],
        prompt_sha256=dependencies["prompt"],
        schema_sha256=dependencies["schema"],
        fault_report=fault_report, fault_sha256=fault_sha,
        replay_report=replay_report, replay_sha256=replay_sha,
        created_at=created_at,
    )
    guard = _SEMANTIC_EVALUATION_GUARDS.get(id(conn))
    if guard is None or guard["evidence"] is not None:
        raise AuditMigrationError("semantic production evidence guard is unavailable")
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT * FROM audit_semantic_production_evidence_v2 "
            "WHERE plan_sha=? AND fault_sha256=? AND replay_sha256=?",
            (plan_sha, fault_sha, replay_sha),
        ).fetchone()
        if existing is None:
            collision = conn.execute(
                "SELECT * FROM audit_semantic_production_evidence_v2 "
                "WHERE evidence_id=?", (values[0],),
            ).fetchone()
            if collision is not None:
                raise ValueError("semantic production evidence conflicts")
            guard["evidence"] = values
            try:
                conn.execute(
                    "INSERT INTO audit_semantic_production_evidence_v2 "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values,
                )
            finally:
                guard["evidence"] = None
        else:
            replay_values = _semantic_production_evidence_values(
                plan_sha=plan_sha,
                capacity_profile_id=plan["capacity_profile_id"],
                capacity_sha256=dependencies["capacity"],
                provider_profile_hashes=dependencies[
                    "provider_profile_hashes"
                ],
                provider_sha256=dependencies["provider"],
                ordered_provider_pools_sha256=dependencies[
                    "ordered_provider_pools"
                ],
                prompt_sha256=dependencies["prompt"],
                schema_sha256=dependencies["schema"],
                fault_report=fault_report,
                fault_sha256=fault_sha,
                replay_report=replay_report,
                replay_sha256=replay_sha,
                created_at=existing["created_at"],
            )
            if tuple(existing) != replay_values:
                raise ValueError("semantic production evidence conflicts")
            values = replay_values
        conn.execute("COMMIT")
    except Exception:
        guard["evidence"] = None
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {
        "evidence_id": values[0],
        "dependency_hashes": {
            key: dependencies[key]
            for key in (
                "plan", "prompt", "schema", "ordered_provider_pools",
                "capacity", "provider",
            )
        } | {
            "fault": fault_sha,
            "replay": replay_sha,
        },
        "production_ready": True,
    }


def _require_durable_semantic_production_evidence(
    conn, dependencies, *, corpus_snapshot_hash=None,
):
    rows = conn.execute(
        "SELECT * FROM audit_semantic_production_evidence_v2 "
        "WHERE plan_sha=? AND prompt_sha256=? AND schema_sha256=? "
        "AND ordered_provider_pools_sha256=? "
        "AND capacity_sha256=? AND provider_sha256=? "
        "AND fault_sha256=? AND replay_sha256=?",
        (
            dependencies["plan"], dependencies["prompt"],
            dependencies["schema"], dependencies["ordered_provider_pools"],
            dependencies["capacity"], dependencies["provider"],
            dependencies["fault"], dependencies["replay"],
        ),
    ).fetchall()
    if len(rows) != 1 or not _semantic_production_evidence_row_valid(rows[0]):
        raise ValueError("durable production evidence is unavailable")
    plan, plan_dependencies = _durable_semantic_production_plan(
        conn, rows[0]["plan_sha"]
    )
    for kind in (
        "plan", "prompt", "schema", "ordered_provider_pools",
        "capacity", "provider",
    ):
        if plan_dependencies[kind] != dependencies[kind]:
            raise ValueError("durable production evidence is stale")
    if (
        corpus_snapshot_hash is not None
        and plan["snapshot"]["snapshot_hash"] != corpus_snapshot_hash
    ):
        raise ValueError("production corpus snapshot is not exact")
    fault_report = _semantic_closed_json(rows[0]["fault_report_json"])
    replay_report = _semantic_closed_json(rows[0]["replay_report_json"])
    if any(
        case["outcome"] != "passed"
        for report in (fault_report, replay_report)
        for case in report["cases"]
    ):
        raise ValueError("durable production evidence did not pass")
    roots = _load_production_evidence_roots()
    _require_production_evidence_root(
        roots, kind="fault", report_sha256=rows[0]["fault_sha256"],
        plan=plan, dependencies=plan_dependencies,
    )
    _require_production_evidence_root(
        roots, kind="replay", report_sha256=rows[0]["replay_sha256"],
        plan=plan, dependencies=plan_dependencies,
    )
    return rows[0]["evidence_id"]


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
    identities = history_audit_eval_v2.semantic_evaluation_identities(
        qrels, outputs, policy
    )
    if (
        identities["qrels_hash"] != qualification["qrels_hash"]
        or identities["policy_sha256"] != qualification["policy_sha256"]
        or identities["metrics"] != qualification["metrics"]
    ):
        raise ValueError("semantic evaluation identity is inconsistent")
    if qualification["production_qualified"] and (
        evidence["evaluation_hash"] != identities["evaluation_hash"]
        or evidence["metric_report_hash"]
            != identities["metric_report_hash"]
    ):
        raise ValueError("semantic evaluation identity is not exact")
    qualification = copy.deepcopy(qualification)
    qualification["evaluation_hash"] = identities["evaluation_hash"]
    qualification["metric_report_hash"] = identities[
        "metric_report_hash"
    ]
    evaluation_guard = _SEMANTIC_EVALUATION_GUARDS.get(id(conn))
    if evaluation_guard is None or any(
        evaluation_guard[name] is not None for name in ("expected", "issuance")
    ):
        raise AuditMigrationError("semantic evaluator guard is unavailable")
    issuance = object()
    evaluation_guard["expected"] = _semantic_sha(
        "history-semantic-evaluator-issuance-v2", qualification
    )
    evaluation_guard["issuance"] = issuance
    try:
        return _persist_semantic_qualification(
            conn, qualification, now=now, _evaluation_issuance=issuance
        )
    finally:
        evaluation_guard["expected"] = None
        evaluation_guard["issuance"] = None


def _persist_semantic_qualification(
    conn, qualification, *, now=None, _evaluation_issuance=None
):
    """Persist only a qualification recomputed by the host-owned evaluator."""
    evaluation_guard = _SEMANTIC_EVALUATION_GUARDS.get(id(conn))
    expected = _semantic_sha(
        "history-semantic-evaluator-issuance-v2", qualification
    )
    if (
        evaluation_guard is None
        or evaluation_guard["expected"] != expected
        or _evaluation_issuance is None
        or evaluation_guard["issuance"] is not _evaluation_issuance
    ):
        raise ValueError("qualification lacks evaluator issuance authority")
    evaluation_guard["expected"] = None
    evaluation_guard["issuance"] = None
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
    if (
        qualification["production_qualified"]
        and qualification["scope"] not in {"real", "production", "real_qrels"}
    ):
        raise ValueError("production qualification scope is invalid")
    for name in (
        "policy_sha256", "qrels_hash", "corpus_snapshot_hash",
        "evaluation_hash", "metric_report_hash",
    ):
        if re.fullmatch(r"[0-9a-f]{64}", qualification[name] or "") is None:
            raise ValueError(f"{name} is invalid")
    dependencies = _semantic_dependencies(qualification["dependency_hashes"])
    if dependencies["semantic_policy"] != qualification["policy_sha256"]:
        raise ValueError("semantic policy dependency is not exact")
    if qualification["production_qualified"]:
        _require_durable_semantic_production_evidence(
            conn, dependencies,
            corpus_snapshot_hash=qualification["corpus_snapshot_hash"],
        )
        if _current_semantic_dependency_heads(conn, dependencies) != dependencies:
            raise ValueError("qualification dependencies are not current")
    created_at = now or _utc_now()
    _semantic_timestamp(created_at, "created_at")
    expires = _semantic_timestamp(qualification["expires_at"], "expires_at")
    host_current = _semantic_timestamp(_utc_now(), "host_now")
    if qualification["production_qualified"] and expires <= host_current:
        raise ValueError("production qualification is expired")
    material = dict(qualification)
    material["dependency_hashes"] = dependencies
    material["evaluation_root_sha256"] = None
    if qualification["production_qualified"]:
        material["evaluation_root_sha256"] = (
            _require_semantic_evaluation_root(
                _load_production_evidence_roots(), material
            )
        )
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
    current = _semantic_timestamp(_utc_now(), "host_now")
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
        or not _router_is_sha(
            qualification_material.get("evaluation_root_sha256")
        )
    ):
        return None
    try:
        _require_durable_semantic_production_evidence(
            conn, dependencies,
            corpus_snapshot_hash=qualification_material[
                "corpus_snapshot_hash"
            ],
        )
        _require_semantic_evaluation_root(
            _load_production_evidence_roots(), qualification_material,
            expected_root_sha256=qualification_material[
                "evaluation_root_sha256"
            ],
        )
    except (ValueError, AuditMigrationError):
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


def _receipt_json_text(value, name):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be canonical JSON text")
    raw = value if value.endswith("\n") else value + "\n"
    try:
        parsed = history_contract_v2.parse_json_bytes(raw.encode("utf-8"))
    except (UnicodeError, history_contract_v2.ContractV2Error) as exc:
        raise ValueError(f"{name} is invalid") from exc
    if history_contract_v2.canonical_bytes(parsed) != raw.encode("utf-8"):
        raise ValueError(f"{name} is not canonical")
    return parsed


def _receipt_binding_sha(domain, material):
    return history_contract_v2.framed_sha256(
        domain, history_contract_v2.canonical_bytes(material)
    )


def _receipt_attempt_outcome(row):
    """Return one exact completion or host-cancel outcome for receipt replay."""
    values = dict(row)
    if (
        values.get("launch_fact_sha256") is None
        or values.get("cost_fact_sha256") is None
    ):
        raise ValueError("receipt attempt accounting authority is incomplete")
    usage_verified = values.get("usage_verified")
    actual_json = values.get("actual_json")
    usage_source = values.get("usage_source")
    authority_sha = values.get("authority_usage_authority_sha256")
    if usage_verified == 0:
        if (
            actual_json is not None
            or usage_source != "reservation"
            or authority_sha is not None
            or values.get("billing_state") != "unknown"
            or values.get("price_source") is not None
            or values.get("currency") is not None
        ):
            raise ValueError("receipt attempt accounting authority is incomplete")
        authority = None
    elif usage_verified == 1:
        authority_fields = (
            "authority_usage_authority_sha256", "authority_attempt_id",
            "authority_run_id", "authority_intent",
            "authority_candidate_id", "authority_provider",
            "authority_capability_profile_hash",
            "authority_request_cas_object_id",
            "authority_output_cas_object_id",
            "authority_terminal_outcome", "authority_actual_json",
            "authority_billing_state", "authority_price_source",
            "authority_currency", "authority_terminal_at",
            "authority_scope",
        )
        authority_values = tuple(values.get(name) for name in authority_fields)
        if (
            actual_json is None
            or usage_source != "verified_actual"
            or _verified_usage_authority_row_valid(*authority_values) != 1
            or values.get("authority_attempt_id") != values.get("attempt_id")
            or values.get("authority_run_id") != values.get("attempt_run_id")
            or values.get("authority_intent")
                != values.get("reservation_intent")
            or values.get("authority_candidate_id")
                != values.get("reservation_candidate_id")
            or values.get("authority_candidate_id")
                != values.get("attempt_candidate_id")
            or values.get("authority_request_cas_object_id")
                != values.get("request_cas_object_id")
            or values.get("authority_actual_json") != actual_json
            or values.get("authority_terminal_at")
                != values.get("budget_created_at")
            or values.get("authority_terminal_at")
                != values.get("cost_completed_at")
            or values.get("authority_billing_state")
                != values.get("billing_state")
            or values.get("authority_price_source")
                != values.get("price_source")
            or values.get("authority_currency") != values.get("currency")
        ):
            raise ValueError("receipt attempt accounting authority is incomplete")
        provenance = _receipt_json_text(
            values.get("provenance_json"), "attempt provenance"
        )
        if (
            provenance.get("provider") != values.get("authority_provider")
            or provenance.get("capability_profile_hash")
                != values.get("authority_capability_profile_hash")
        ):
            raise ValueError("receipt attempt accounting authority is incomplete")
        authority = values
    else:
        raise ValueError("receipt attempt accounting authority is incomplete")
    completion_outcome = values.get("completion_outcome")
    completion_usage_json = values.get("completion_usage_json")
    output_id = values.get("output_cas_object_id")
    cost_outcome = values.get("cost_outcome")
    if cost_outcome == "cancelled":
        if (
            completion_outcome is not None
            or completion_usage_json is not None
            or output_id is not None
            or (
                authority is not None
                and (
                    values.get("authority_terminal_outcome") != "cancelled"
                    or values.get("authority_output_cas_object_id") is not None
                )
            )
        ):
            raise ValueError("receipt cancellation authority conflicts")
        return "cancelled"
    expected_cost_outcome = (
        "success" if completion_outcome == "valid" else "failed"
    )
    if (
        completion_outcome is None
        or output_id is None
        or _completion_usage_valid(completion_usage_json) != 1
        or cost_outcome != expected_cost_outcome
        or (
            authority is not None
            and (
                values.get("authority_terminal_outcome")
                    != completion_outcome
                or values.get("authority_output_cas_object_id") != output_id
                or values.get("authority_terminal_at")
                    != values.get("completion_at")
            )
        )
    ):
        raise ValueError("receipt completion authority is incomplete")
    return completion_outcome


def _require_receipt_fields(receipt, expected):
    mismatched = sorted(
        name for name, value in expected.items() if receipt.get(name) != value
    )
    if mismatched:
        raise ValueError(
            "receipt conflicts with durable provenance: " + ",".join(mismatched)
        )


def _derive_l2_receipt_provenance(conn, receipt, plan_row):
    try:
        from lib import history_execution
    except ImportError:
        import history_execution

    plan_material = _receipt_json_text(plan_row["plan_json"], "L2 plan")
    try:
        history_audit_plan.validate_runtime_plan_material(plan_material)
    except history_audit_plan.AuditPlanError as exc:
        raise ValueError("receipt L2 plan is invalid") from exc
    records_row = conn.execute(
        "SELECT * FROM audit_l2_snapshot_records_v2 WHERE snapshot_id=?",
        (plan_row["snapshot_id"],),
    ).fetchone()
    if records_row is None:
        raise ValueError("receipt L2 snapshot records are missing")
    records = _receipt_json_text(records_row["records_json"], "L2 records")
    try:
        if history_audit_plan.runtime_snapshot_records_sha(records) != records_row[
            "records_sha"
        ]:
            raise ValueError("receipt L2 snapshot records drifted")
    except history_audit_plan.AuditPlanError as exc:
        raise ValueError("receipt L2 snapshot records are invalid") from exc
    plan = copy.deepcopy(plan_material)
    plan["plan_sha"] = plan_row["plan_sha"]
    plan["snapshot"]["records"] = records

    route = conn.execute(
        """
        SELECT cohort.cohort_sha256, cohort.risk_policy_sha256,
               cohort.risk_slice_policy_sha256,
               route.fact_sha256 AS route_fact_sha256,
               route.matched_rule_ids_json, route.rule_table_sha256,
               route.risk_policy_version, route.intent,
               dispatch.dispatch_sha256,
               observation.boundary_sha256
        FROM audit_candidate_l2_dispatch_facts_v2 dispatch
        JOIN audit_candidate_route_facts_v2 route
          ON route.run_id=dispatch.run_id
         AND route.candidate_id=dispatch.candidate_id
         AND route.fact_sha256=dispatch.route_fact_sha256
        JOIN audit_candidate_route_source_bindings_v2 binding
          ON binding.run_id=route.run_id
         AND binding.candidate_id=route.candidate_id
         AND binding.route_fact_sha256=route.fact_sha256
        JOIN audit_router_phase_facts_v2 phase
          ON phase.phase_fact_sha256=binding.final_phase_fact_sha256
         AND phase.phase='final'
         AND phase.candidate_id=route.candidate_id
         AND phase.source_set_sha256=binding.source_set_sha256
        JOIN audit_router_source_sets_v2 source_set
          ON source_set.source_set_sha256=binding.source_set_sha256
         AND source_set.route_round_sha256=phase.route_round_sha256
         AND source_set.phase='final'
        JOIN audit_candidate_route_cohorts_v2 cohort
          ON cohort.run_id=route.run_id
         AND cohort.cohort_sha256=route.cohort_sha256
        JOIN audit_candidate_route_observation_boundaries_v2 observation
          ON observation.run_id=route.run_id
         AND observation.candidate_id=route.candidate_id
         AND observation.route_fact_sha256=route.fact_sha256
        WHERE dispatch.plan_sha=?
          AND route.router_facts_json=phase.router_facts_json
          AND route.risk_slices_json=phase.risk_slices_json
          AND route.matched_rule_ids_json=phase.matched_rule_ids_json
          AND route.route=phase.route
          AND route.call_l1_model=phase.call_l1_model
          AND route.dispatch_allowed=phase.dispatch_allowed
          AND route.rule_table_sha256=phase.rule_table_sha256
          AND route.risk_policy_version=phase.risk_policy_version
          AND NOT EXISTS (
            SELECT 1 FROM audit_legacy_candidate_route_authorities_v2 legacy
            WHERE legacy.route_fact_sha256=route.fact_sha256
               OR (legacy.run_id=route.run_id
                   AND legacy.candidate_id=route.candidate_id)
          )
        """,
        (plan_row["plan_sha"],),
    ).fetchone()
    if route is None or route["intent"] != plan_material["intent"]:
        raise ValueError("receipt route authority is missing")
    matched_rules = _receipt_json_text(
        route["matched_rule_ids_json"], "matched router rules"
    )

    if not validate_l2_terminal_graph(conn, plan_row["plan_sha"]):
        raise ValueError("receipt L2 terminal graph is invalid")
    task_rows = conn.execute(
        """
        SELECT task.task_hash, task.stage, task.state,
               settlement.settlement_sha256,
               terminal.fact_sha256 AS terminal_fact_sha256
        FROM audit_logical_tasks task
        JOIN audit_task_bindings_v2 binding ON binding.task_hash=task.task_hash
        LEFT JOIN audit_valid_task_settlement_authority_v5 settlement
          ON settlement.task_hash=task.task_hash
        LEFT JOIN audit_task_terminal_facts_v2 terminal
          ON terminal.task_hash=task.task_hash
        WHERE binding.plan_sha=?
        ORDER BY task.task_hash
        """,
        (plan_row["plan_sha"],),
    ).fetchall()
    if not task_rows or any(
        row["state"] not in {"settled", "superseded", "exhausted"}
        or (row["state"] == "settled") != (row["settlement_sha256"] is not None)
        or (row["state"] in {"superseded", "exhausted"})
            != (row["terminal_fact_sha256"] is not None)
        for row in task_rows
    ):
        raise ValueError("receipt tasks are not durably terminal")
    task_ids = [row["task_hash"] for row in task_rows]

    attempt_rows = conn.execute(
        """
        SELECT attempt.attempt_id, attempt.task_hash, attempt.provenance_json,
               attempt.request_cas_object_id, attempt.state,
               task.run_id AS attempt_run_id,
               task.staging_candidate_id AS attempt_candidate_id,
               completion.output_cas_object_id,
               completion.outcome AS completion_outcome,
               completion.usage_json AS completion_usage_json,
               completion.completed_at AS completion_at,
               reservation.plan_sha AS reservation_plan_sha,
               reservation.candidate_id AS reservation_candidate_id,
               reservation.intent AS reservation_intent,
               reservation.attempt_kind, reservation.reserved_json,
               budget.usage_verified, budget.actual_json,
               budget.created_at AS budget_created_at,
               launch.fact_sha256 AS launch_fact_sha256,
               cost.outcome AS cost_outcome,
               cost.billing_state, cost.usage_source,
               cost.price_source, cost.currency,
               cost.completed_at AS cost_completed_at,
               cost.fact_sha256 AS cost_fact_sha256,
               authority.usage_authority_sha256
                 AS authority_usage_authority_sha256,
               authority.attempt_id AS authority_attempt_id,
               authority.run_id AS authority_run_id,
               authority.intent AS authority_intent,
               authority.candidate_id AS authority_candidate_id,
               authority.provider AS authority_provider,
               authority.capability_profile_hash
                 AS authority_capability_profile_hash,
               authority.request_cas_object_id
                 AS authority_request_cas_object_id,
               authority.output_cas_object_id
                 AS authority_output_cas_object_id,
               authority.terminal_outcome AS authority_terminal_outcome,
               authority.actual_json AS authority_actual_json,
               authority.billing_state AS authority_billing_state,
               authority.price_source AS authority_price_source,
               authority.currency AS authority_currency,
               authority.terminal_at AS authority_terminal_at,
               authority.authority_scope AS authority_scope
        FROM audit_task_attempts attempt
        JOIN audit_logical_tasks task ON task.task_hash=attempt.task_hash
        JOIN audit_task_bindings_v2 binding ON binding.task_hash=attempt.task_hash
        LEFT JOIN audit_attempt_completions_v2 completion
          ON completion.attempt_id=attempt.attempt_id
        LEFT JOIN audit_runtime_budget_reservations_v2 reservation
          ON reservation.attempt_id=attempt.attempt_id
        LEFT JOIN audit_runtime_budget_settlements_v2 budget
          ON budget.attempt_id=attempt.attempt_id
        LEFT JOIN audit_attempt_launch_facts_v2 launch
          ON launch.attempt_id=attempt.attempt_id
        LEFT JOIN audit_attempt_cost_settlements_v2 cost
          ON cost.attempt_id=attempt.attempt_id
        LEFT JOIN audit_verified_usage_authorities_v2 authority
          ON authority.attempt_id=attempt.attempt_id
        WHERE binding.plan_sha=?
        ORDER BY attempt.attempt_id
        """,
        (plan_row["plan_sha"],),
    ).fetchall()
    attempt_facts = []
    cas_ids = set()
    for row in attempt_rows:
        if (
            row["reservation_plan_sha"] != plan_row["plan_sha"]
            or row["reservation_candidate_id"] != plan_row["candidate_id"]
            or row["reservation_intent"] != plan_material["intent"]
        ):
            raise ValueError("receipt attempt provenance is incomplete")
        try:
            durable_outcome = _receipt_attempt_outcome(row)
        except ValueError as exc:
            raise ValueError("receipt attempt provenance is incomplete") from exc
        provenance = _receipt_json_text(
            row["provenance_json"], "attempt provenance"
        )
        reserved = _receipt_json_text(row["reserved_json"], "budget reservation")
        actual = (
            _receipt_json_text(row["actual_json"], "budget settlement")
            if row["actual_json"] is not None else None
        )
        reservation_sha = _receipt_binding_sha(
            "history-receipt-budget-reservation-v2",
            {
                "attempt_id": row["attempt_id"],
                "plan_sha": row["reservation_plan_sha"],
                "candidate_id": row["reservation_candidate_id"],
                "intent": row["reservation_intent"],
                "attempt_kind": row["attempt_kind"],
                "reserved": reserved,
            },
        )
        settlement_sha = _receipt_binding_sha(
            "history-receipt-budget-settlement-v2",
            {
                "attempt_id": row["attempt_id"],
                "usage_verified": bool(row["usage_verified"]),
                "actual": actual,
            },
        )
        completion_usage_sha = (
            None
            if durable_outcome == "cancelled"
            else _receipt_binding_sha(
                "history-receipt-completion-usage-v2",
                {"attempt_id": row["attempt_id"], "usage": {}},
            )
        )
        attempt_facts.append(
            {
                "attempt_id": row["attempt_id"],
                "task_hash": row["task_hash"],
                "provenance_sha256": _receipt_binding_sha(
                    "history-receipt-attempt-provenance-v2", provenance
                ),
                "request_cas_object_id": row["request_cas_object_id"],
                "output_cas_object_id": row["output_cas_object_id"],
                "outcome": durable_outcome,
                "reservation_sha256": reservation_sha,
                "budget_settlement_sha256": settlement_sha,
                "completion_usage_sha256": completion_usage_sha,
                "launch_fact_sha256": row["launch_fact_sha256"],
                "cost_fact_sha256": row["cost_fact_sha256"],
                "usage_authority_sha256": row[
                    "authority_usage_authority_sha256"
                ],
                "billing_state": row["billing_state"],
                "price_source": row["price_source"],
                "currency": row["currency"],
            }
        )
        cas_ids.add(row["request_cas_object_id"])
        if row["output_cas_object_id"] is not None:
            cas_ids.add(row["output_cas_object_id"])
    attempt_ids = [row["attempt_id"] for row in attempt_rows]

    budget_rows = conn.execute(
        """
        SELECT event_id, intent, event_sha256
        FROM audit_budget_events WHERE run_id=?
        ORDER BY event_id
        """,
        (plan_row["run_id"],),
    ).fetchall()
    if any(row["intent"] != plan_material["intent"] for row in budget_rows):
        raise ValueError("receipt budget intent is inconsistent")

    generation = conn.execute(
        """
        SELECT generation_id, material_json
        FROM audit_l2_adjudication_generations_v2 WHERE plan_sha=?
        """,
        (plan_row["plan_sha"],),
    ).fetchone()
    if generation is None:
        raise ValueError("receipt adjudication generation is missing")
    generation_material = _receipt_json_text(
        generation["material_json"], "adjudication generation"
    )
    derived_authority_hashes = [
        row[0]
        for row in conn.execute(
            """
            SELECT authority_sha256
            FROM audit_l2_derived_task_authority_v2
            WHERE plan_sha=? ORDER BY authority_sha256
            """,
            (plan_row["plan_sha"],),
        )
    ]
    adjudication_state = history_execution.load_adjudication_state(
        conn, plan_row["plan_sha"]
    )
    if adjudication_state["generation_id"] != generation["generation_id"]:
        raise ValueError("receipt adjudication generation drifted")
    terminal = history_execution.load_terminal_states(
        conn, plan_row["plan_sha"]
    )
    summary = history_execution.build_coverage_receipt(
        plan,
        terminal,
        {"qualified": False, "profile_id": receipt["semantic_policy_profile_id"]},
        conn=conn,
    )
    semantic_material_sha = history_contract_v2.framed_sha256(
        "history-semantic-release-receipt-v2",
        history_contract_v2.canonical_bytes(receipt),
    )
    release_authorized = conn.execute(
        "SELECT 1 FROM audit_semantic_release_authorizations_v2 "
        "WHERE receipt_id=? AND receipt_material_sha256=? "
        "AND semantic_policy_profile_id=? AND no_match_basis='l2_exhaustive'",
        (
            receipt["minimum_receipt_sha"], semantic_material_sha,
            receipt["semantic_policy_profile_id"],
        ),
    ).fetchone() is not None
    if release_authorized:
        durable_no_hit = (
            summary["coverage_complete"]
            and summary["adjudication_complete"]
            and summary["final_status"] == "uncertain"
            and summary["stage_reason_code"] == "semantic_policy_unqualified"
            and summary["reducer_input"] == []
            and summary["evidence_anchors"] == []
            and not summary["missing_ids"]
            and not summary["duplicate_ids"]
            and not summary["extra_ids"]
            and not summary["invalid_schema"]
            and not summary["invalid_anchor"]
            and not summary["truncated"]
        )
        if durable_no_hit:
            summary.update(
                semantic_policy_qualified=True,
                no_match_basis="l2_exhaustive",
                final_status="complete_no_match",
                stage_reason_code="complete_no_match",
            )

    expected = {
        "run_id": plan_material["run_id"],
        "plan_hash": plan_row["plan_sha"],
        "candidate_hash": plan_material["candidate"]["candidate_hash"],
        "snapshot_id": plan_material["snapshot"]["snapshot_id"],
        "snapshot_hash": plan_material["snapshot"]["snapshot_hash"],
        "history_as_of_watermark": plan_material["snapshot"][
            "history_as_of_watermark"
        ],
        "current_batch_id_namespace": plan_material["snapshot"][
            "current_batch_id_namespace"
        ],
        "current_batch_ids_hash": plan_material["snapshot"][
            "current_batch_ids_hash"
        ],
        "exclusion_policy_sha": plan_material["snapshot"][
            "exclusion_policy_sha"
        ],
        "expected_asset_ids_hash": plan_material["snapshot"][
            "expected_asset_ids_hash"
        ],
        "observed_asset_ids_hash": history_contract_v2.ordered_set_sha256(
            "history-observed-assets-v2", summary["observed_ids"]
        ),
        "missing_ids": summary["missing_ids"],
        "duplicate_ids": summary["duplicate_ids"],
        "extra_ids": summary["extra_ids"],
        "invalid_schema": summary["invalid_schema"],
        "invalid_anchor": summary["invalid_anchor"],
        "truncated": summary["truncated"],
        "provider_pools_ordered": plan_material["provider_pools_ordered"],
        "provider_capability_profile_hashes": sorted(
            plan_material["provider_capability_profile_hashes"].values()
        ),
        "capacity_profile_id": plan_material["capacity_profile_id"],
        "semantic_policy_profile_id": plan_material[
            "semantic_policy_profile_id"
        ],
        "risk_policy_version": route["risk_policy_version"],
        "matched_router_rule_ids": matched_rules,
        "settlement_policy_sha": plan_material["settlement_policy_sha"],
        "shard_plan_sha": plan_material["shard_plan_sha"],
        "logical_task_hashes": task_ids,
        "attempt_manifest_hashes": attempt_ids,
        "raw_request_output_cas_hashes": sorted(cas_ids),
        "coverage_complete": summary["coverage_complete"],
        "adjudication_complete": summary["adjudication_complete"],
        "semantic_policy_qualified": summary[
            "semantic_policy_qualified"
        ],
        "no_match_basis": summary["no_match_basis"],
        "final_status": summary["final_status"],
        "stage_reason_code": summary["stage_reason_code"],
        "evidence_anchors": summary["evidence_anchors"],
    }
    _require_receipt_fields(receipt, expected)

    status_material = {
        name: receipt[name]
        for name in (
            "observed_asset_ids_hash", "missing_ids", "duplicate_ids",
            "extra_ids", "invalid_schema", "invalid_anchor", "truncated",
            "coverage_complete", "adjudication_complete",
            "semantic_policy_qualified", "no_match_basis", "final_status",
            "stage_reason_code", "evidence_anchors",
        )
    }
    return {
        "schema_version": "history-receipt-provenance-v2",
        "authority_kind": "l2",
        "run_id": plan_row["run_id"],
        "plan_sha": plan_row["plan_sha"],
        "plan_material_sha256": _receipt_binding_sha(
            "history-receipt-plan-material-v2", plan_material
        ),
        "snapshot_records_sha256": records_row["records_sha"],
        "candidate_id": plan_row["candidate_id"],
        "route": {
            "cohort_sha256": route["cohort_sha256"],
            "route_fact_sha256": route["route_fact_sha256"],
            "dispatch_sha256": route["dispatch_sha256"],
            "observation_boundary_sha256": route["boundary_sha256"],
            "risk_policy_sha256": route["risk_policy_sha256"],
            "risk_slice_policy_sha256": route["risk_slice_policy_sha256"],
            "rule_table_sha256": route["rule_table_sha256"],
        },
        "budget_policy_sha256": plan_material["budget_policy_sha"],
        "budget_event_sha256s": [row["event_sha256"] for row in budget_rows],
        "tasks": [
            {
                "task_hash": row["task_hash"],
                "stage": row["stage"],
                "state": row["state"],
                "terminal_identity_sha256": (
                    row["settlement_sha256"] or row["terminal_fact_sha256"]
                ),
            }
            for row in task_rows
        ],
        "attempts": attempt_facts,
        "adjudication": {
            "generation_id": generation["generation_id"],
            "generation_material_sha256": _receipt_binding_sha(
                "history-receipt-adjudication-generation-v2",
                generation_material,
            ),
            "derived_authority_sha256s": derived_authority_hashes,
        },
        "status_derivation_sha256": _receipt_binding_sha(
            "history-receipt-status-derivation-v2", status_material
        ),
    }


def derive_receipt_provenance(conn, receipt):
    """Rebuild compact receipt authority exclusively from durable host facts."""
    normalized = history_contract_v2.validate_receipt(receipt)
    plan_row = conn.execute(
        """
        SELECT * FROM audit_l2_plans_v2
        WHERE plan_sha=? AND run_id=? AND candidate_hash=?
          AND snapshot_id=? AND snapshot_hash=?
        """,
        (
            normalized["plan_hash"], normalized["run_id"],
            normalized["candidate_hash"], normalized["snapshot_id"],
            normalized["snapshot_hash"],
        ),
    ).fetchone()
    if plan_row is not None:
        return _derive_l2_receipt_provenance(conn, normalized, plan_row)
    if normalized["final_status"] == "overlap_found":
        raise ValueError("l1_positive_authority_unavailable")
    raise ValueError("receipt_execution_authority_unavailable")


def _receipt_sql_values(receipt):
    values = []
    for field in _RELEASE_RECEIPT_FIELDS:
        value = receipt[field]
        if field in _RELEASE_JSON_FIELDS:
            value = history_contract_v2.canonical_bytes(value).decode("utf-8")
        elif field in _RELEASE_BOOLEAN_FIELDS:
            value = int(value)
        values.append(value)
    return tuple(values)


def clear_receipt_issuance_authorization(conn):
    guard = _RECEIPT_ISSUANCE_GUARDS.get(id(conn))
    if guard is not None:
        _clear_receipt_issuance_guard(guard)


def insert_authorized_receipt(
    conn, receipt, release_context=None, *, now=None
):
    """Atomically issue and insert one receipt from replayed durable facts."""
    if not conn.in_transaction:
        raise AuditMigrationError("authorized receipt insert requires a transaction")
    normalized = history_contract_v2.validate_receipt(receipt)
    material_sha = _receipt_material_sha(normalized)
    issued_at = now or _utc_now()
    _semantic_timestamp(issued_at, "issued_at")
    receipt_values = _receipt_sql_values(normalized)
    guard = _RECEIPT_ISSUANCE_GUARDS.get(id(conn))
    if guard is None or guard["expected_issuance"] is not None:
        raise AuditMigrationError("receipt issuance guard is unavailable")
    conn.execute("SAVEPOINT receipt_issuance")
    try:
        if normalized["final_status"] == "complete_no_match":
            _authorize_complete_no_match_receipt(
                conn, normalized, release_context, now=now
            )
        provenance = derive_receipt_provenance(conn, normalized)
        provenance_json = history_contract_v2.canonical_bytes(
            provenance
        ).decode("utf-8")
        provenance_sha = _receipt_binding_sha(
            "history-receipt-provenance-v2", provenance
        )
        issuance_id = history_contract_v2.framed_sha256(
            "history-receipt-issuance-v2",
            bytes.fromhex(normalized["minimum_receipt_sha"]),
            bytes.fromhex(provenance_sha),
        )
        issuance_values = (
            issuance_id, normalized["minimum_receipt_sha"], material_sha,
            provenance["authority_kind"], provenance_json, provenance_sha,
            issued_at,
        )
        guard.update(
            expected_issuance=issuance_values,
            receipt_id=normalized["minimum_receipt_sha"],
            receipt_material_sha256=material_sha,
            issuance_id=issuance_id,
        )
        conn.execute(
            "INSERT INTO audit_receipt_issuances_v2 VALUES(?,?,?,?,?,?,?)",
            issuance_values,
        )
        guard["expected_receipt"] = receipt_values
        conn.execute(
            "INSERT INTO audit_receipts(" + ",".join(_RELEASE_RECEIPT_FIELDS)
            + ") VALUES(" + ",".join("?" for _ in _RELEASE_RECEIPT_FIELDS)
            + ")",
            receipt_values,
        )
        conn.execute("RELEASE SAVEPOINT receipt_issuance")
        return {
            "issuance_id": issuance_id,
            "receipt_material_sha256": material_sha,
            "provenance_sha256": provenance_sha,
            "authority_kind": provenance["authority_kind"],
        }
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT receipt_issuance")
        conn.execute("RELEASE SAVEPOINT receipt_issuance")
        raise
    finally:
        clear_receipt_issuance_authorization(conn)
        clear_semantic_receipt_authorization(conn)


def verify_receipt_issuance(conn, receipt):
    """Replay one receipt issuance against current immutable execution facts."""
    normalized = history_contract_v2.validate_receipt(receipt)
    try:
        row = conn.execute(
            "SELECT * FROM audit_receipt_issuances_v2 WHERE receipt_id=?",
            (normalized["minimum_receipt_sha"],),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        raise ValueError("receipt issuance schema is missing") from exc
    if row is None:
        raise ValueError("receipt issuance is missing")
    provenance = _receipt_json_text(row["provenance_json"], "receipt provenance")
    provenance_sha = _receipt_binding_sha(
        "history-receipt-provenance-v2", provenance
    )
    issuance_id = history_contract_v2.framed_sha256(
        "history-receipt-issuance-v2",
        bytes.fromhex(normalized["minimum_receipt_sha"]),
        bytes.fromhex(provenance_sha),
    )
    replay = derive_receipt_provenance(conn, normalized)
    if (
        row["receipt_material_sha256"] != _receipt_material_sha(normalized)
        or row["provenance_sha256"] != provenance_sha
        or row["issuance_id"] != issuance_id
        or row["authority_kind"] != provenance.get("authority_kind")
        or replay != provenance
    ):
        raise ValueError("receipt issuance is missing, substituted, or stale")
    return {
        "issuance_id": row["issuance_id"],
        "receipt_material_sha256": row["receipt_material_sha256"],
        "provenance_sha256": row["provenance_sha256"],
        "authority_kind": row["authority_kind"],
    }


def _authorize_complete_no_match_receipt(
    conn, receipt, release_context, *, now=None
):
    """Bind one exact complete-no-match receipt to current production authority."""
    if not conn.in_transaction:
        raise AuditMigrationError("semantic release authorization requires a transaction")
    try:
        normalized = history_contract_v2.validate_receipt(receipt)
    except history_contract_v2.ContractV2Error as exc:
        raise ValueError("semantic release receipt is invalid") from exc
    if normalized["final_status"] != "complete_no_match":
        raise ValueError("semantic release receipt is not complete_no_match")
    context_fields = {
        "scope", "policy_sha256", "corpus_snapshot_hash",
        "evaluation_hash", "dependency_hashes",
    }
    if not isinstance(release_context, dict) or set(release_context) != context_fields:
        raise ValueError("semantic release context is invalid")
    dependencies = _semantic_dependencies(release_context["dependency_hashes"])
    if (
        release_context["scope"] not in {"real", "production", "real_qrels"}
        or dependencies["semantic_policy"] != release_context["policy_sha256"]
        or release_context["corpus_snapshot_hash"] != normalized["snapshot_hash"]
        or dependencies["plan"] != normalized["plan_hash"]
    ):
        raise ValueError("semantic release context does not match receipt")
    plan, plan_dependencies = _durable_semantic_production_plan(
        conn, normalized["plan_hash"]
    )
    for kind in (
        "plan", "prompt", "schema", "ordered_provider_pools",
        "capacity", "provider",
    ):
        if dependencies[kind] != plan_dependencies[kind]:
            raise ValueError("semantic release plan dependencies are stale")
    if (
        normalized["provider_pools_ordered"]
            != plan["provider_pools_ordered"]
        or normalized["provider_capability_profile_hashes"]
            != plan_dependencies["provider_profile_hashes"]
        or normalized["capacity_profile_id"]
            != plan["capacity_profile_id"]
        or normalized["semantic_policy_profile_id"]
            != plan["semantic_policy_profile_id"]
        or normalized["no_match_basis"] != "l2_exhaustive"
    ):
        raise ValueError("semantic release receipt plan binding is invalid")
    qualification = lookup_semantic_qualification(
        conn,
        semantic_policy_profile_id=normalized[
            "semantic_policy_profile_id"
        ],
        no_match_basis=normalized["no_match_basis"],
        policy_sha256=release_context["policy_sha256"],
        corpus_snapshot_hash=release_context["corpus_snapshot_hash"],
        evaluation_hash=release_context["evaluation_hash"],
        dependency_hashes=dependencies,
    )
    if qualification is None or qualification["scope"] != release_context["scope"]:
        raise ValueError("semantic_policy_unqualified")
    _require_durable_semantic_production_evidence(conn, dependencies)
    authorized_at = _utc_now()
    _semantic_timestamp(authorized_at, "authorized_at")
    material_sha = history_contract_v2.framed_sha256(
        "history-semantic-release-receipt-v2",
        history_contract_v2.canonical_bytes(normalized),
    )
    dependency_hashes_json = _semantic_canonical(dependencies)
    authorization_material = {
        "receipt_id": normalized["minimum_receipt_sha"],
        "receipt_material_sha256": material_sha,
        "qualification_id": qualification["qualification_id"],
        "qualification_sha256": qualification["qualification_sha256"],
        "semantic_policy_profile_id": normalized[
            "semantic_policy_profile_id"
        ],
        "scope": release_context["scope"],
        "no_match_basis": normalized["no_match_basis"],
        "policy_sha256": release_context["policy_sha256"],
        "corpus_snapshot_hash": release_context["corpus_snapshot_hash"],
        "evaluation_hash": release_context["evaluation_hash"],
        "dependency_hashes": dependencies,
        "dependency_heads": _semantic_closed_json(
            qualification["dependency_head_events_json"]
        ),
        "authorized_at": authorized_at,
    }
    authorization_id = _semantic_sha(
        "history-semantic-release-authorization-v2",
        authorization_material,
    )
    values = (
        authorization_id, normalized["minimum_receipt_sha"], material_sha,
        qualification["qualification_id"],
        qualification["qualification_sha256"],
        normalized["semantic_policy_profile_id"],
        release_context["scope"], normalized["no_match_basis"],
        release_context["policy_sha256"],
        release_context["corpus_snapshot_hash"],
        release_context["evaluation_hash"], dependency_hashes_json,
        qualification["dependency_head_events_json"], authorized_at,
    )
    guard = _SEMANTIC_RELEASE_GUARDS.get(id(conn))
    if guard is None or any(
        guard[name] is not None
        for name in (
            "expected_authorization", "receipt_id",
            "receipt_material_sha256", "qualification_id",
        )
    ):
        raise AuditMigrationError("semantic release guard is unavailable")
    existing = conn.execute(
        "SELECT * FROM audit_semantic_release_authorizations_v2 "
        "WHERE receipt_id=?", (normalized["minimum_receipt_sha"],),
    ).fetchone()
    if existing is not None and tuple(existing) != values:
        raise ValueError("semantic release authorization conflicts")
    guard.update(
        expected_authorization=values,
        receipt_id=normalized["minimum_receipt_sha"],
        receipt_material_sha256=material_sha,
        qualification_id=qualification["qualification_id"],
    )
    try:
        if existing is None:
            conn.execute(
                "INSERT INTO audit_semantic_release_authorizations_v2 "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values,
            )
        guard["expected_authorization"] = None
    except Exception:
        clear_semantic_receipt_authorization(conn)
        raise
    return {
        "authorization_id": authorization_id,
        "qualification_id": qualification["qualification_id"],
        "receipt_material_sha256": material_sha,
        "authorized_at": authorized_at,
    }


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
    """Compatibility entry point for the host-owned receipt issuance path."""
    normalized = history_contract_v2.validate_receipt(receipt)
    if normalized["final_status"] != "complete_no_match":
        raise ValueError("authorized receipt insert is only for complete_no_match")
    return insert_authorized_receipt(
        conn, normalized, release_context, now=now
    )


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
    try:
        authorization_dependencies = _semantic_closed_json(
            authorization["dependency_hashes_json"]
        )
        authorization_heads = _semantic_closed_json(
            authorization["dependency_heads_json"]
        )
    except ValueError as exc:
        raise ValueError("receipt authorization is not canonical") from exc
    authorization_material = {
        "receipt_id": authorization["receipt_id"],
        "receipt_material_sha256": authorization[
            "receipt_material_sha256"
        ],
        "qualification_id": authorization["qualification_id"],
        "qualification_sha256": authorization["qualification_sha256"],
        "semantic_policy_profile_id": authorization[
            "semantic_policy_profile_id"
        ],
        "scope": authorization["scope"],
        "no_match_basis": authorization["no_match_basis"],
        "policy_sha256": authorization["policy_sha256"],
        "corpus_snapshot_hash": authorization["corpus_snapshot_hash"],
        "evaluation_hash": authorization["evaluation_hash"],
        "dependency_hashes": authorization_dependencies,
        "dependency_heads": authorization_heads,
        "authorized_at": authorization["authorized_at"],
    }
    if (
        set(material) != {
            "schema_version", "semantic_policy_profile_id", "production_qualified",
            "no_match_basis", "scope", "policy_sha256", "qrels_hash",
            "corpus_snapshot_hash", "evaluation_hash", "metric_report_hash",
            "dependency_hashes", "metrics", "vetoes", "expires_at",
            "evaluation_root_sha256",
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
        or not _router_is_sha(material.get("evaluation_root_sha256"))
        or authorization["policy_sha256"] != qualification["policy_sha256"]
        or authorization["evaluation_hash"] != qualification["evaluation_hash"]
        or authorization["dependency_hashes_json"] != qualification["dependency_hashes_json"]
        or authorization["dependency_heads_json"] != qualification["dependency_head_events_json"]
        or authorization["scope"] != qualification["scope"]
        or authorization["scope"] not in {"real", "production", "real_qrels"}
        or authorization["authorization_id"] != _semantic_sha(
            "history-semantic-release-authorization-v2",
            authorization_material,
        )
    ):
        raise ValueError("receipt qualification identity is inconsistent")
    if expected_context is not None:
        fields = {"run_id", "plan_hash", "candidate_hash", "snapshot_id", "snapshot_hash"}
        if not isinstance(expected_context, dict) or set(expected_context) != fields:
            raise ValueError("expected receipt context is invalid")
        if any(normalized[field] != expected_context[field] for field in fields):
            raise ValueError("receipt context does not match replay request")
    if require_current:
        dependencies = _semantic_dependencies(material["dependency_hashes"])
        current = _semantic_timestamp(_utc_now(), "host_now")
        if _semantic_timestamp(material["expires_at"], "expires_at") <= current:
            raise ValueError("receipt qualification is expired")
        if conn.execute(
            "SELECT 1 FROM audit_semantic_invalidation_facts_v2 "
            "WHERE qualification_id=? LIMIT 1",
            (authorization["qualification_id"],),
        ).fetchone() is not None:
            raise ValueError("receipt qualification is invalidated")
        current_heads = _current_semantic_dependency_heads(conn, dependencies)
        current_events = _current_semantic_dependency_head_events(
            conn, dependencies
        )
        if (
            current_heads != dependencies
            or authorization_heads != current_events
            or qualification["dependency_head_events_json"]
                != _semantic_canonical(current_events)
        ):
            raise ValueError("receipt qualification dependencies are stale")
        current_qualification = lookup_semantic_qualification(
            conn,
            semantic_policy_profile_id=normalized[
                "semantic_policy_profile_id"
            ],
            no_match_basis=normalized["no_match_basis"],
            policy_sha256=material["policy_sha256"],
            corpus_snapshot_hash=normalized["snapshot_hash"],
            evaluation_hash=material["evaluation_hash"],
            dependency_hashes=dependencies,
        )
        if (
            current_qualification is None
            or current_qualification["qualification_id"]
                != authorization["qualification_id"]
        ):
            raise ValueError("receipt qualification is not current")
        verify_receipt_issuance(conn, normalized)
        _require_durable_semantic_production_evidence(conn, dependencies)
        plan, plan_dependencies = _durable_semantic_production_plan(
            conn, normalized["plan_hash"]
        )
        if any(
            dependencies[kind] != plan_dependencies[kind]
            for kind in (
                "plan", "prompt", "schema", "ordered_provider_pools",
                "capacity", "provider",
            )
        ) or (
            normalized["provider_pools_ordered"]
                != plan["provider_pools_ordered"]
            or normalized["provider_capability_profile_hashes"]
                != plan_dependencies["provider_profile_hashes"]
            or normalized["capacity_profile_id"]
                != plan["capacity_profile_id"]
        ):
            raise ValueError("receipt production plan authority is stale")
        return {
            "historically_authorized": True,
            "current_authority": True,
            "qualification_id": authorization["qualification_id"],
            "receipt_material_sha256": material_sha,
        }
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


_LOGICAL_TASK_TRANSITIONS = {
    "planned": frozenset({"claimed"}),
    "claimed": frozenset({
        "claimed", "planned", "settling", "superseded", "exhausted",
    }),
    "settling": frozenset({"planned", "settled"}),
    "settled": frozenset(),
    "superseded": frozenset(),
    "exhausted": frozenset(),
}


def _logical_task_transition_allowed(conn, task_hash, old_state, new_state):
    """Validate the generic task lifecycle and durable settlement authority."""
    enforced = conn.execute(
        "SELECT 1 FROM audit_schema_migrations "
        "WHERE component='logical-task-transition-integrity' AND version=1"
    ).fetchone()
    if enforced is None:
        return True
    if new_state not in _LOGICAL_TASK_TRANSITIONS.get(old_state, frozenset()):
        return False
    if new_state == "settled":
        return conn.execute(
            "SELECT 1 FROM audit_task_settlements_v2 WHERE task_hash=?",
            (task_hash,),
        ).fetchone() is not None
    return True


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
    if not _logical_task_transition_allowed(
        conn, task_hash, expected_state, new_state
    ):
        raise sqlite3.IntegrityError(
            f"illegal logical task transition: {expected_state}->{new_state}"
        )
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
        FROM (
          SELECT task_hash FROM audit_l2_valid_task_authority_v2
          UNION
          SELECT task_hash FROM audit_l2_valid_adjudication_task_authority_v2
        ) valid
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
        FROM audit_l2_valid_split_families_v3
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


def _l2_current_claim_has_failure(conn, parent, outcomes):
    if not outcomes:
        return False
    placeholders = ",".join("?" for _ in outcomes)
    return conn.execute(
        f"""
        SELECT 1
        FROM audit_task_attempts attempt
        JOIN audit_attempt_completions_v2 completion
          ON completion.attempt_id=attempt.attempt_id
        JOIN audit_cas_objects output
          ON output.object_id=completion.output_cas_object_id
        WHERE attempt.task_hash=?
          AND completion.outcome IN ({placeholders})
          AND output.integrity_state='verified'
          AND (
            (
              json_extract(attempt.provenance_json, '$.claim_fence')=?
              AND json_extract(attempt.provenance_json, '$.claim_token')=?
            )
            OR EXISTS (
              SELECT 1 FROM audit_l2_valid_failure_claim_transfers_v3 transfer
              WHERE transfer.task_hash=attempt.task_hash
                AND transfer.attempt_id=attempt.attempt_id
                AND transfer.outcome=completion.outcome
                AND transfer.target_claim_fence=?
                AND transfer.target_claim_token=?
                AND transfer.target_lease_until=?
            )
          )
        LIMIT 1
        """,
        (
            parent["task_hash"], *sorted(outcomes),
            parent["fence"], parent["claim_token"],
            parent["fence"], parent["claim_token"], parent["lease_until"],
        ),
    ).fetchone() is not None


def _l2_current_claim_has_overflow(conn, parent):
    return _l2_current_claim_has_failure(conn, parent, {"overflow"})


def _l2_current_claim_has_split_failure(conn, parent):
    return _l2_current_claim_has_failure(
        conn, parent, {"overflow", "item_set", "truncated"}
    )


def _l2_failure_transfer_source(conn, task_hash, attempt_id, outcome):
    if outcome not in {"overflow", "item_set", "truncated"}:
        return None
    row = conn.execute(
        """
        SELECT attempt.provenance_json
        FROM audit_task_attempts attempt
        JOIN audit_attempt_completions_v2 completion
          ON completion.attempt_id=attempt.attempt_id
        JOIN audit_cas_objects output
          ON output.object_id=completion.output_cas_object_id
        WHERE attempt.task_hash=? AND attempt.attempt_id=?
          AND completion.outcome=? AND output.integrity_state='verified'
          AND NOT EXISTS (
            SELECT 1 FROM audit_task_attempts later
            WHERE later.task_hash=attempt.task_hash
              AND later.ordinal>attempt.ordinal
          )
        """,
        (task_hash, attempt_id, outcome),
    ).fetchone()
    if row is None:
        return None
    try:
        provenance = _closed_json(row["provenance_json"])
    except (TypeError, ValueError):
        return None
    source_fence = provenance.get("claim_fence")
    source_token = provenance.get("claim_token")
    if (
        type(source_fence) is not int
        or source_fence < 0
        or not isinstance(source_token, str)
        or not source_token
    ):
        return None
    return source_fence, source_token


def claim_l2_failure_recovery(
    conn, task_hash, attempt_id, outcome, worker_id, lease_seconds, *,
    expected_fence, now,
):
    """Atomically transfer exact terminal failure evidence to a fresh claim."""
    if conn.in_transaction:
        raise AuditMigrationError("failure claim transfer requires an idle connection")
    if not isinstance(worker_id, str) or not worker_id:
        raise ValueError("failure claim transfer worker is invalid")
    if type(lease_seconds) is not int or lease_seconds <= 0:
        raise ValueError("failure claim transfer lease is invalid")
    if type(expected_fence) is not int or expected_fence < 0:
        raise ValueError("failure claim transfer fence is invalid")
    created_at = _l2_transition_timestamp(now)
    current_time = datetime.datetime.fromisoformat(created_at)
    target_lease_until = (
        current_time + datetime.timedelta(seconds=lease_seconds)
    ).isoformat()
    parent = _l2_transition_parent(conn, task_hash)
    if (
        parent["state"] == "claimed"
        and parent["fence"] == expected_fence + 1
        and parent["claim_token"] == worker_id
        and _metadata_lease_live(parent["lease_until"], created_at) == 1
    ):
        replay = conn.execute(
            """
            SELECT 1 FROM audit_l2_valid_failure_claim_transfers_v3
            WHERE task_hash=? AND attempt_id=? AND outcome=?
              AND target_claim_fence=? AND target_claim_token=?
              AND target_lease_until=?
            """,
            (
                task_hash, attempt_id, outcome, parent["fence"], worker_id,
                parent["lease_until"],
            ),
        ).fetchone()
        if replay is not None:
            return {
                "task_hash": task_hash,
                "fence": parent["fence"],
                "claim_token": worker_id,
                "lease_until": parent["lease_until"],
            }
    if parent["state"] != "planned" or parent["fence"] != expected_fence:
        raise StaleFence("failure recovery task state or fence is stale")
    source = _l2_failure_transfer_source(conn, task_hash, attempt_id, outcome)
    if source is None:
        raise AuditMigrationError(
            "failure claim transfer lacks exact terminal evidence"
        )
    source_fence, source_token = source
    target_fence = expected_fence + 1
    authorization_sha = _l2_failure_claim_transfer_sha(
        task_hash, attempt_id, outcome, source_fence, source_token,
        target_fence, worker_id, target_lease_until, created_at,
    )
    transfer = (
        task_hash, attempt_id, outcome, source_fence, source_token,
        target_fence, worker_id, target_lease_until, authorization_sha,
        created_at,
    )
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT state,fence,claim_token,lease_until "
            "FROM audit_logical_tasks WHERE task_hash=?",
            (task_hash,),
        ).fetchone()
        if current is None or tuple(current) != (
            "planned", expected_fence, None, None,
        ):
            raise StaleFence("failure recovery claim changed before transfer")
        if _l2_failure_transfer_source(
            conn, task_hash, attempt_id, outcome
        ) != source:
            raise AuditMigrationError(
                "failure claim transfer evidence changed before transfer"
            )
        with _l2_failure_claim_transfer_guard(conn, transfer):
            compare_and_set_logical_task(
                conn, task_hash,
                expected_state="planned", expected_fence=expected_fence,
                new_state="claimed", new_fence=target_fence,
                claim_token=worker_id, lease_until=target_lease_until,
            )
            conn.execute(
                "INSERT INTO audit_l2_failure_claim_transfers_v3 "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                transfer,
            )
            if conn.execute(
                "SELECT 1 FROM audit_l2_valid_failure_claim_transfers_v3 "
                "WHERE authorization_sha256=?", (authorization_sha,),
            ).fetchone() is None:
                raise AuditMigrationError(
                    "failure claim transfer failed durable validation"
                )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {
        "task_hash": task_hash,
        "fence": target_fence,
        "claim_token": worker_id,
        "lease_until": target_lease_until,
    }


def transition_l2_split_task(
    conn, parent_task_hash, *, expected_fence, claim_token, now,
    refresh_authority=False,
):
    """Atomically derive and persist one exact two-child split transition."""
    if conn.in_transaction:
        raise AuditMigrationError("split transition requires an idle connection")
    if type(refresh_authority) is not bool:
        raise ValueError("split transition authority refresh is invalid")
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
    if not _l2_current_claim_has_split_failure(conn, parent):
        raise AuditMigrationError(
            "multi-item split lacks current-claim failure evidence"
        )
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
        transaction_now = (
            _l2_transition_timestamp(_utc_now())
            if refresh_authority else now
        )
        current = conn.execute(
            "SELECT state, fence, claim_token, lease_until "
            "FROM audit_logical_tasks WHERE task_hash=?",
            (parent_task_hash,),
        ).fetchone()
        if (
            current is None
            or tuple(current) != (
                "claimed", parent["fence"], parent["claim_token"],
                parent["lease_until"],
            )
            or _metadata_lease_live(
                current["lease_until"], transaction_now
            ) != 1
        ):
            raise StaleFence("split claim expired or changed before transition")
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
                "SELECT 1 FROM audit_l2_valid_split_families_v3 "
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
    conn, task_hash, reason, *, expected_fence, claim_token, now,
    refresh_authority=False,
):
    """Atomically persist one claimed task exhaustion with exact authority."""
    if conn.in_transaction:
        raise AuditMigrationError("exhaust transition requires an idle connection")
    if type(refresh_authority) is not bool:
        raise ValueError("exhaust transition authority refresh is invalid")
    now = _l2_transition_timestamp(now)
    parent = _l2_transition_parent(conn, task_hash)
    if parent["state"] == "exhausted":
        durable = conn.execute(
            """
            SELECT terminal.reason
            FROM audit_l2_valid_exhaustions_v2 exhausted
            JOIN audit_task_terminal_facts_v2 terminal
              ON terminal.task_hash=exhausted.task_hash
             AND terminal.terminal_state='exhausted'
            WHERE exhausted.task_hash=?
            """,
            (task_hash,),
        ).fetchone()
        if durable is None:
            raise AuditMigrationError("exhausted task has malformed authority")
        if durable["reason"] != reason:
            raise AuditMigrationError("exhaustion replay reason conflicts")
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
        transaction_now = (
            _l2_transition_timestamp(_utc_now())
            if refresh_authority else now
        )
        current = conn.execute(
            "SELECT state,fence,claim_token,lease_until "
            "FROM audit_logical_tasks WHERE task_hash=?",
            (task_hash,),
        ).fetchone()
        if (
            current is None
            or tuple(current) != (
                "claimed", parent["fence"], parent["claim_token"],
                parent["lease_until"],
            )
            or _metadata_lease_live(
                current["lease_until"], transaction_now
            ) != 1
        ):
            raise StaleFence("exhaustion claim expired before transition")
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
        LEFT JOIN (
          SELECT task_hash FROM audit_l2_valid_task_authority_v2
          UNION
          SELECT task_hash FROM audit_l2_valid_adjudication_task_authority_v2
        ) valid
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
    invalid_split_evidence = conn.execute(
        f"""
        SELECT 1
        FROM audit_logical_tasks task
        JOIN audit_task_bindings_v2 binding ON binding.task_hash=task.task_hash
        LEFT JOIN audit_l2_valid_split_families_v3 split
          ON split.parent_task_hash=task.task_hash
        WHERE task.state='superseded' AND split.parent_task_hash IS NULL
          {plan_clause}
        LIMIT 1
        """,
        parameters,
    ).fetchone()
    invalid_settlement = conn.execute(
        f"""
        SELECT 1
        FROM audit_logical_tasks task
        JOIN audit_task_bindings_v2 binding ON binding.task_hash=task.task_hash
        LEFT JOIN audit_valid_task_settlement_authority_v5 settlement
          ON settlement.task_hash=task.task_hash
        WHERE task.state='settled' AND settlement.task_hash IS NULL
          {plan_clause}
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
            invalid_edge, invalid_split_evidence, invalid_settlement,
            invalid_authority,
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
