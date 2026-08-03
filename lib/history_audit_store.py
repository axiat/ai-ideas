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


MIGRATIONS = (
    Migration("migration-ledger", 1, _LEDGER_SQL),
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
            and not set(usage).difference(required | {"currency_micros"})
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


def _clear_metadata_guard(guard):
    guard.update(
        active=False,
        metadata_operation=None,
        metadata_now=None,
        metadata_outbox_id=None,
        metadata_claim_token=None,
        metadata_claim_fence=None,
    )


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
    guard = {}
    _clear_metadata_guard(guard)
    _FENCE_GUARDS[id(conn)] = guard
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
