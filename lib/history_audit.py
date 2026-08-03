"""Frozen-corpus M0 L1 history audit contracts."""

import datetime
import hashlib
import re

try:
    from lib import history_contract_v2 as contract
    from lib import history_projection
    from lib import history_store
except ImportError:  # Direct execution with lib/ on sys.path.
    import history_contract_v2 as contract
    import history_projection
    import history_store


SEMANTIC_RELATIONS = (
    "blocking_duplicate",
    "substantive_overlap",
    "related_only",
    "distinct",
    "uncertain",
)
LINEAGE_RELATIONS = (
    "same_revision",
    "evolved_from",
    "recheck_of",
    "supersedes",
    "none",
)
FINAL_STATUSES = (
    "overlap_found",
    "complete_no_match",
    "uncertain",
    "partial",
    "invalid",
)
CURRENT_BATCH_ID_NAMESPACE = contract.STAGING_CANDIDATE_NAMESPACE
_STAGING_ID = re.compile(r"^stg-v2-[0-9a-f]{64}$")
_EXCLUSION_POLICY = {
    "policy_version": "history-audit-exclusion-v2",
    "prior_history_predicate": "source_sequence_lte_watermark",
    "current_batch_id_namespace": CURRENT_BATCH_ID_NAMESPACE,
    "batch_internal_comparison": "separate_before_activation",
}
_RETRIEVAL_FIELDS = frozenset(
    {
        "run_id",
        "plan_hash",
        "candidate_hash",
        "observed_asset_ids_hash",
        "missing_ids",
        "duplicate_ids",
        "extra_ids",
        "invalid_schema",
        "invalid_anchor",
        "truncated",
        "provider_pools_ordered",
        "provider_capability_profile_hashes",
        "capacity_profile_id",
        "risk_policy_version",
        "matched_router_rule_ids",
        "settlement_policy_sha",
        "shard_plan_sha",
        "logical_task_hashes",
        "attempt_manifest_hashes",
        "raw_request_output_cas_hashes",
        "minimum_receipt_sha",
        "coverage_complete",
    }
)
_ADJUDICATION_FIELDS = frozenset(
    {
        "adjudication_complete",
        "verified_hits",
        "unresolved_conflict",
        "exhausted_reason",
        "evidence_anchors",
    }
)
_QUALIFICATION_FIELDS = frozenset(
    {
        "semantic_policy_profile_id",
        "semantic_policy_qualified",
        "no_match_basis",
    }
)


class ActivationCrash(RuntimeError):
    """Fault-injection signal raised only after a durable activation commit."""


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _sha_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _framed(domain, value):
    return contract.framed_sha256(domain, contract.canonical_bytes(value))


def _require_sha(value, name):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def _require_staging_ids(values):
    if (
        not isinstance(values, (list, tuple))
        or not values
        or any(not isinstance(value, str) or not _STAGING_ID.fullmatch(value) for value in values)
        or len(set(values)) != len(values)
    ):
        raise ValueError("current batch IDs must be unique host v2 staging IDs")
    return sorted(values)


def _snapshot_from_row(conn, row):
    asset_ids = history_projection.candidate_ids_as_of(
        conn, row["history_as_of_watermark"]
    )
    expected_hash = contract.ordered_set_sha256(
        "history-snapshot-assets-v2", asset_ids
    )
    if expected_hash != row["expected_asset_ids_hash"]:
        raise ValueError("frozen snapshot asset root does not replay")
    return {
        "run_id": row["run_id"],
        "batch_id": row["batch_id"],
        "snapshot_id": row["snapshot_id"],
        "snapshot_hash": row["snapshot_hash"],
        "history_as_of_watermark": row["history_as_of_watermark"],
        "current_batch_id_namespace": row["current_batch_id_namespace"],
        "current_batch_ids_hash": row["current_batch_ids_hash"],
        "exclusion_policy_sha": row["exclusion_policy_sha"],
        "expected_asset_ids": asset_ids,
        "expected_asset_ids_hash": row["expected_asset_ids_hash"],
    }


def _stored_snapshot(conn, snapshot):
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot must be an object")
    row = conn.execute(
        "SELECT * FROM audit_snapshots WHERE snapshot_id=?",
        (snapshot.get("snapshot_id"),),
    ).fetchone()
    if row is None:
        raise ValueError("snapshot is not persisted")
    replay = _snapshot_from_row(conn, row)
    for field in (
        "run_id",
        "batch_id",
        "snapshot_hash",
        "history_as_of_watermark",
        "current_batch_id_namespace",
        "current_batch_ids_hash",
        "exclusion_policy_sha",
        "expected_asset_ids_hash",
    ):
        if snapshot.get(field) != replay[field]:
            raise ValueError("snapshot identity does not match persisted state")
    current_ids = snapshot.get("current_batch_ids")
    if current_ids is not None:
        current_ids = _require_staging_ids(current_ids)
        if contract.ordered_set_sha256(
            "history-current-batch-ids-v2", current_ids
        ) != replay["current_batch_ids_hash"]:
            raise ValueError("snapshot current batch IDs do not replay")
        replay["current_batch_ids"] = current_ids
    return replay


def freeze_snapshot(conn, *, run_id, batch_id, current_batch_ids):
    """Persist the source-sequence watermark, exclusion set, and asset root."""
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id is required")
    if not isinstance(batch_id, str) or not batch_id:
        raise ValueError("batch_id is required")
    current_ids = _require_staging_ids(current_batch_ids)
    current_ids_hash = contract.ordered_set_sha256(
        "history-current-batch-ids-v2", current_ids
    )
    exclusion_policy_sha = _framed(
        "history-exclusion-policy-v2", _EXCLUSION_POLICY
    )
    if conn.in_transaction:
        raise ValueError("snapshot freeze requires an idle connection")
    conn.execute("BEGIN IMMEDIATE")
    try:
        run = conn.execute(
            "SELECT 1 FROM audit_run_manifests WHERE run_id=?", (run_id,)
        ).fetchone()
        if run is None:
            raise ValueError("snapshot run manifest is missing")
        existing = conn.execute(
            "SELECT * FROM audit_snapshots WHERE run_id=? AND batch_id=?",
            (run_id, batch_id),
        ).fetchone()
        if existing is not None:
            replay = _snapshot_from_row(conn, existing)
            if replay["current_batch_ids_hash"] != current_ids_hash:
                raise ValueError("snapshot batch exclusion identity conflicts")
            replay["current_batch_ids"] = current_ids
            conn.execute("COMMIT")
            return replay
        watermark = conn.execute(
            "SELECT COALESCE(MAX(source_sequence), 0) FROM candidates"
        ).fetchone()[0]
        asset_ids = history_projection.candidate_ids_as_of(conn, watermark)
        asset_ids_hash = contract.ordered_set_sha256(
            "history-snapshot-assets-v2", asset_ids
        )
        material = {
            "run_id": run_id,
            "batch_id": batch_id,
            "history_as_of_watermark": watermark,
            "current_batch_id_namespace": CURRENT_BATCH_ID_NAMESPACE,
            "current_batch_ids_hash": current_ids_hash,
            "exclusion_policy_sha": exclusion_policy_sha,
            "expected_asset_ids_hash": asset_ids_hash,
        }
        snapshot_hash = _framed("history-snapshot-v2", material)
        snapshot_id = _framed(
            "history-snapshot-id-v2",
            {"run_id": run_id, "batch_id": batch_id, "snapshot_hash": snapshot_hash},
        )
        conn.execute(
            """
            INSERT INTO audit_snapshots(
              snapshot_id, snapshot_hash, history_as_of_watermark,
              current_batch_id_namespace, current_batch_ids_hash,
              exclusion_policy_sha, expected_asset_ids_hash, created_at,
              run_id, batch_id
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                snapshot_hash,
                watermark,
                CURRENT_BATCH_ID_NAMESPACE,
                current_ids_hash,
                exclusion_policy_sha,
                asset_ids_hash,
                _utc_now(),
                run_id,
                batch_id,
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {
        **material,
        "snapshot_id": snapshot_id,
        "snapshot_hash": snapshot_hash,
        "current_batch_ids": current_ids,
        "expected_asset_ids": asset_ids,
    }


def read_frozen_assets(conn, snapshot):
    """Replay the sorted corpus IDs at a persisted snapshot watermark."""
    return _stored_snapshot(conn, snapshot)["expected_asset_ids"]


def read_l1_rankings(conn, snapshot, query, *, depth):
    """Read exact, FTS, and hash-dense v1 indexes at the frozen boundary."""
    frozen = _stored_snapshot(conn, snapshot)
    rankings = history_projection.l1_rankings_as_of(
        conn, query, depth, frozen["history_as_of_watermark"]
    )
    allowed = set(frozen["expected_asset_ids"])
    if any(
        item["candidate_id"] not in allowed
        for values in rankings.values()
        for item in values
    ):
        raise ValueError("L1 projection escaped the frozen asset root")
    return rankings


def _direction_identity(direction_receipt):
    required = {
        "direction_id", "contract_sha", "validator_version", "artifact_sha"
    }
    if not isinstance(direction_receipt, dict) or set(direction_receipt) != required:
        raise ValueError("direction receipt schema is closed")
    for name in ("direction_id", "validator_version"):
        if not isinstance(direction_receipt[name], str) or not direction_receipt[name]:
            raise ValueError("direction receipt text is required")
    _require_sha(direction_receipt["contract_sha"], "contract_sha")
    _require_sha(direction_receipt["artifact_sha"], "artifact_sha")
    return dict(direction_receipt)


def stage_raw_batch(conn, *, snapshot, raw_candidates, direction_receipt):
    """Assign v2 staging IDs and freeze raw/canonical artifact hashes."""
    frozen = _stored_snapshot(conn, snapshot)
    direction = _direction_identity(direction_receipt)
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise ValueError("raw candidates are required")
    staged = []
    for source_order, item in enumerate(raw_candidates):
        if not isinstance(item, dict) or set(item) != {
            "staging_candidate_id", "raw_candidate"
        }:
            raise ValueError("staged candidate input schema is closed")
        staging_id = item["staging_candidate_id"]
        _require_staging_ids([staging_id])
        normalized = history_store._normalize_append_row(item["raw_candidate"])
        staged.append(
            {
                "staging_candidate_id": staging_id,
                "run_id": frozen["run_id"],
                "batch_id": frozen["batch_id"],
                "source_order": source_order,
                "candidate_hash": contract.framed_sha256(
                    "history-candidate-content-v2", normalized
                ),
                "raw_artifact_sha": _sha_bytes(normalized),
                "raw_candidate": normalized,
            }
        )
    staged_ids = sorted(item["staging_candidate_id"] for item in staged)
    if contract.ordered_set_sha256(
        "history-current-batch-ids-v2", staged_ids
    ) != frozen["current_batch_ids_hash"]:
        raise ValueError("staged candidates do not match frozen batch exclusions")
    if conn.in_transaction:
        raise ValueError("batch staging requires an idle connection")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO audit_direction_contracts(
              run_id, batch_id, direction_id, contract_sha,
              validator_version, artifact_sha, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                frozen["run_id"], frozen["batch_id"], direction["direction_id"],
                direction["contract_sha"], direction["validator_version"],
                direction["artifact_sha"], _utc_now(),
            ),
        )
        for item in staged:
            conn.execute(
                """
                INSERT OR IGNORE INTO audit_batch_staging(
                  staging_candidate_id, run_id, batch_id, candidate_hash,
                  raw_artifact_sha, source_order, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["staging_candidate_id"], item["run_id"], item["batch_id"],
                    item["candidate_hash"], item["raw_artifact_sha"],
                    item["source_order"], _utc_now(),
                ),
            )
            stored = conn.execute(
                "SELECT * FROM audit_batch_staging WHERE staging_candidate_id=?",
                (item["staging_candidate_id"],),
            ).fetchone()
            expected = (
                item["run_id"], item["batch_id"], item["candidate_hash"],
                item["raw_artifact_sha"], item["source_order"],
            )
            if stored is None or tuple(stored[name] for name in (
                "run_id", "batch_id", "candidate_hash", "raw_artifact_sha", "source_order"
            )) != expected:
                raise ValueError("staging identity conflicts with durable state")
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {
        "run_id": frozen["run_id"],
        "batch_id": frozen["batch_id"],
        "snapshot_id": frozen["snapshot_id"],
        "direction_receipt": direction,
        "candidates": sorted(staged, key=lambda item: item["staging_candidate_id"]),
    }


def plan_batch_pairs(staged_batch):
    """Return deterministic batch-internal exact and semantic pairs."""
    if not isinstance(staged_batch, dict) or not isinstance(
        staged_batch.get("candidates"), list
    ):
        raise ValueError("staged batch is invalid")
    ids = sorted(item["staging_candidate_id"] for item in staged_batch["candidates"])
    if len(set(ids)) != len(ids):
        raise ValueError("staged batch contains duplicate IDs")
    pairs = []
    for left_index, left in enumerate(ids):
        for right in ids[left_index + 1:]:
            pairs.append(
                {
                    "left_staging_candidate_id": left,
                    "right_staging_candidate_id": right,
                    "comparison_kinds": ["exact", "semantic"],
                }
            )
    body = {
        "run_id": staged_batch["run_id"],
        "batch_id": staged_batch["batch_id"],
        "staging_candidate_ids": ids,
        "pairs": pairs,
    }
    return {
        "pair_plan_sha": _framed("history-batch-pair-plan-v2", body),
        "pair_count": len(pairs),
        "pairs": pairs,
    }


def _validate_staged_batch_snapshot(conn, staged_batch):
    if not isinstance(staged_batch, dict):
        raise ValueError("staged batch is invalid")
    run_id = staged_batch.get("run_id")
    batch_id = staged_batch.get("batch_id")
    snapshot_id = staged_batch.get("snapshot_id")
    snapshot = conn.execute(
        "SELECT * FROM audit_snapshots WHERE snapshot_id=?", (snapshot_id,)
    ).fetchone()
    if (
        snapshot is None
        or snapshot["run_id"] != run_id
        or snapshot["batch_id"] != batch_id
        or snapshot["current_batch_id_namespace"]
        != CURRENT_BATCH_ID_NAMESPACE
    ):
        raise ValueError("staged batch snapshot ownership is invalid")
    candidates = staged_batch.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("staged batch candidates are invalid")
    staging_ids = _require_staging_ids(
        [item.get("staging_candidate_id") for item in candidates]
    )
    if contract.ordered_set_sha256(
        "history-current-batch-ids-v2", staging_ids
    ) != snapshot["current_batch_ids_hash"]:
        raise ValueError("staged batch does not match snapshot exclusion hash")
    persisted = {
        row["staging_candidate_id"]
        for row in conn.execute(
            "SELECT staging_candidate_id FROM audit_batch_staging "
            "WHERE run_id=? AND batch_id=? ORDER BY staging_candidate_id",
            (run_id, batch_id),
        )
    }
    if persisted != set(staging_ids):
        raise ValueError("staged batch does not match persisted batch ownership")
    return snapshot


def record_batch_pair_results(conn, staged_batch, pair_plan, pair_results):
    """Persist one completed pair receipt, including an explicit empty plan."""
    snapshot = _validate_staged_batch_snapshot(conn, staged_batch)
    expected_plan = plan_batch_pairs(staged_batch)
    if pair_plan != expected_plan:
        raise ValueError("batch pair plan does not replay")
    if not isinstance(pair_results, list) or len(pair_results) != pair_plan["pair_count"]:
        raise ValueError("batch pair results do not cover the plan")
    expected_pairs = {
        (item["left_staging_candidate_id"], item["right_staging_candidate_id"])
        for item in pair_plan["pairs"]
    }
    normalized = []
    for item in pair_results:
        if not isinstance(item, dict) or set(item) != {
            "left_staging_candidate_id", "right_staging_candidate_id",
            "semantic_relation", "evidence_sha"
        }:
            raise ValueError("batch pair result schema is closed")
        pair = (item["left_staging_candidate_id"], item["right_staging_candidate_id"])
        if pair not in expected_pairs or item["semantic_relation"] not in SEMANTIC_RELATIONS:
            raise ValueError("batch pair result is outside its plan")
        _require_sha(item["evidence_sha"], "pair evidence_sha")
        normalized.append(dict(item))
    normalized.sort(key=lambda item: (
        item["left_staging_candidate_id"], item["right_staging_candidate_id"]
    ))
    if len({(item["left_staging_candidate_id"], item["right_staging_candidate_id"]) for item in normalized}) != len(normalized):
        raise ValueError("batch pair result is duplicated")
    result_sha = _framed(
        "history-batch-pair-result-v2",
        {"pair_plan_sha": pair_plan["pair_plan_sha"], "results": normalized},
    )
    run_id = staged_batch["run_id"]
    batch_id = staged_batch["batch_id"]
    snapshot_id = snapshot["snapshot_id"]
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO audit_batch_pair_receipts(
              run_id, batch_id, snapshot_id, pair_plan_sha, pair_result_sha,
              pair_count, completed_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, batch_id, snapshot_id, pair_plan["pair_plan_sha"], result_sha,
             len(normalized), _utc_now()),
        )
        receipt = conn.execute(
            "SELECT * FROM audit_batch_pair_receipts WHERE run_id=? AND batch_id=?",
            (run_id, batch_id),
        ).fetchone()
        if receipt is None or tuple(receipt[name] for name in (
            "snapshot_id", "pair_plan_sha", "pair_result_sha", "pair_count"
        )) != (snapshot_id, pair_plan["pair_plan_sha"], result_sha, len(normalized)):
            raise ValueError("batch pair receipt conflicts with durable state")
        for item in normalized:
            conn.execute(
                """
                INSERT OR IGNORE INTO audit_batch_pairs(
                  run_id, batch_id, left_staging_candidate_id,
                  right_staging_candidate_id, pair_plan_sha, pair_result_sha,
                  created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, batch_id, item["left_staging_candidate_id"],
                    item["right_staging_candidate_id"], pair_plan["pair_plan_sha"],
                    result_sha, _utc_now(),
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {
        "run_id": run_id,
        "batch_id": batch_id,
        "snapshot_id": snapshot_id,
        "pair_plan_sha": pair_plan["pair_plan_sha"],
        "pair_result_sha": result_sha,
        "pair_count": len(normalized),
    }


def record_direction_check(
    conn, *, staged_candidate, direction_receipt, semantic_relation,
    lineage_relation, evidence_sha
):
    """Persist one host-owned direction check before activation."""
    direction = _direction_identity(direction_receipt)
    if semantic_relation not in SEMANTIC_RELATIONS:
        raise ValueError("semantic relation is invalid")
    if lineage_relation not in LINEAGE_RELATIONS:
        raise ValueError("lineage relation is invalid")
    _require_sha(evidence_sha, "direction evidence_sha")
    if not isinstance(staged_candidate, dict):
        raise ValueError("staged candidate is invalid")
    staging_id = staged_candidate.get("staging_candidate_id")
    stored = conn.execute(
        "SELECT run_id, batch_id FROM audit_batch_staging WHERE staging_candidate_id=?",
        (staging_id,),
    ).fetchone()
    if stored is None:
        raise ValueError("staged candidate is not persisted")
    conn.execute(
        """
        INSERT OR IGNORE INTO audit_direction_checks(
          run_id, batch_id, direction_id, contract_sha, validator_version,
          artifact_sha, staging_candidate_id, semantic_relation,
          lineage_relation, evidence_sha, checked_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            stored["run_id"], stored["batch_id"], direction["direction_id"],
            direction["contract_sha"], direction["validator_version"],
            direction["artifact_sha"], staging_id, semantic_relation,
            lineage_relation, evidence_sha, _utc_now(),
        ),
    )
    result = {
        "run_id": stored["run_id"],
        "batch_id": stored["batch_id"],
        **direction,
        "staging_candidate_id": staging_id,
        "semantic_relation": semantic_relation,
        "lineage_relation": lineage_relation,
        "evidence_sha": evidence_sha,
    }
    persisted = conn.execute(
        """
        SELECT * FROM audit_direction_checks
        WHERE run_id=? AND batch_id=? AND direction_id=? AND contract_sha=?
          AND validator_version=? AND artifact_sha=? AND staging_candidate_id=?
        """,
        (
            result["run_id"], result["batch_id"], result["direction_id"],
            result["contract_sha"], result["validator_version"],
            result["artifact_sha"], result["staging_candidate_id"],
        ),
    ).fetchone()
    if persisted is None or any(persisted[name] != result[name] for name in (
        "semantic_relation", "lineage_relation", "evidence_sha"
    )):
        raise ValueError("direction check conflicts with durable state")
    return result


def _activation_receipt(
    context, legacy_candidate_id, source_sequence, projection_sequence,
    ledger_snapshot_sha, ledger_row_count, ledger_state, ledger_generation,
    search_canonical_revision
):
    return {
        "schema_version": "history-activation-receipt-v2",
        "run_id": context["run_id"],
        "batch_id": context["batch_id"],
        "snapshot_id": context["snapshot_id"],
        "snapshot_hash": context["snapshot_hash"],
        "history_as_of_watermark": context["history_as_of_watermark"],
        "staging_candidate_id": context["staging_candidate_id"],
        "candidate_hash": context["candidate_hash"],
        "raw_artifact_sha": context["raw_artifact_sha"],
        "direction_check": context["direction_check"],
        "pair_plan_sha": context["pair_plan_sha"],
        "pair_result_sha": context["pair_result_sha"],
        "legacy_candidate_id": legacy_candidate_id,
        "source_sequence": source_sequence,
        "search_projection_outbox": {
            "record_id": legacy_candidate_id,
            "projection_kind": "candidate",
            "content_version": "candidate-v1",
            "canonical_revision": search_canonical_revision,
            "source_sequence": source_sequence,
            "state": "pending",
            "generation": 0,
        },
        "ledger_projection_outbox": {
            "projection_sequence": projection_sequence,
            "snapshot_sha256": ledger_snapshot_sha,
            "row_count": ledger_row_count,
            "state": ledger_state,
            "generation": ledger_generation,
        },
    }


def activate_staged_candidate(
    conn, *, snapshot, staged_candidate, pair_receipt, direction_check,
    fault_after_commit=False
):
    """Activate through the v1 append allocator and atomically bind v2 identity."""
    frozen = _stored_snapshot(conn, snapshot)
    if not isinstance(staged_candidate, dict):
        raise ValueError("staged candidate is invalid")
    staging_id = staged_candidate.get("staging_candidate_id")
    staging = conn.execute(
        "SELECT * FROM audit_batch_staging WHERE staging_candidate_id=?",
        (staging_id,),
    ).fetchone()
    if staging is None:
        raise ValueError("staged candidate is not persisted")
    normalized = history_store._normalize_append_row(staged_candidate.get("raw_candidate"))
    if (
        staging["run_id"] != frozen["run_id"]
        or staging["batch_id"] != frozen["batch_id"]
        or staging["raw_artifact_sha"] != _sha_bytes(normalized)
        or staging["candidate_hash"] != contract.framed_sha256(
            "history-candidate-content-v2", normalized
        )
    ):
        raise ValueError("staged candidate identity does not replay")
    pair = conn.execute(
        "SELECT * FROM audit_batch_pair_receipts WHERE run_id=? AND batch_id=?",
        (frozen["run_id"], frozen["batch_id"]),
    ).fetchone()
    if (
        pair is None
        or pair["snapshot_id"] != frozen["snapshot_id"]
        or pair["run_id"] != frozen["run_id"]
        or pair["batch_id"] != frozen["batch_id"]
        or any(pair[name] != pair_receipt.get(name) for name in (
        "snapshot_id", "pair_plan_sha", "pair_result_sha", "pair_count"
        ))
    ):
        raise ValueError("pair receipt is not the completed batch receipt")
    direction_fields = (
        "run_id", "batch_id", "direction_id", "contract_sha", "validator_version",
        "artifact_sha", "staging_candidate_id", "semantic_relation",
        "lineage_relation", "evidence_sha",
    )
    if not isinstance(direction_check, dict) or any(
        name not in direction_check for name in direction_fields
    ):
        raise ValueError("direction check is invalid")
    persisted_direction = conn.execute(
        """
        SELECT * FROM audit_direction_checks
        WHERE run_id=? AND batch_id=? AND direction_id=? AND contract_sha=?
          AND validator_version=? AND artifact_sha=? AND staging_candidate_id=?
        """,
        tuple(direction_check[name] for name in direction_fields[:7]),
    ).fetchone()
    if persisted_direction is None or any(
        persisted_direction[name] != direction_check[name] for name in direction_fields
    ):
        raise ValueError("direction check does not match durable state")
    context = {
        "run_id": frozen["run_id"],
        "batch_id": frozen["batch_id"],
        "snapshot_id": frozen["snapshot_id"],
        "snapshot_hash": frozen["snapshot_hash"],
        "history_as_of_watermark": frozen["history_as_of_watermark"],
        "staging_candidate_id": staging_id,
        "candidate_hash": staging["candidate_hash"],
        "raw_artifact_sha": staging["raw_artifact_sha"],
        "direction_check": {name: direction_check[name] for name in direction_fields},
        "pair_plan_sha": pair["pair_plan_sha"],
        "pair_result_sha": pair["pair_result_sha"],
    }
    request = {"schema_version": "history-activation-request-v2", **context}
    request_json = contract.canonical_bytes(request).decode("utf-8").rstrip("\n")
    request_sha = _sha_bytes(request_json.encode("utf-8"))
    commit_key = "history-v2-activation:" + staging_id

    def receipt_json(*values):
        return contract.canonical_bytes(_activation_receipt(context, *values)).decode("utf-8")

    def receipt_sha(*values):
        return _sha_bytes(receipt_json(*values).encode("utf-8"))

    conn.create_function("audit_activation_receipt_json", 8, receipt_json)
    conn.create_function("audit_activation_receipt_sha", 8, receipt_sha)

    def install_atomic_bridge(active_conn):
        active_conn.execute("DROP TRIGGER IF EXISTS temp.audit_activation_capture")
        active_conn.execute("DROP TABLE IF EXISTS temp.audit_activation_context")
        active_conn.execute(
            """
            CREATE TEMP TABLE audit_activation_context(
              staging_candidate_id TEXT NOT NULL,
              raw_artifact_sha TEXT NOT NULL,
              pair_plan_sha TEXT NOT NULL,
              pair_result_sha TEXT NOT NULL,
              history_as_of_watermark INTEGER NOT NULL
            )
            """
        )
        active_conn.execute(
            "INSERT INTO audit_activation_context VALUES(?, ?, ?, ?, ?)",
            (
                staging_id, staging["raw_artifact_sha"], pair["pair_plan_sha"],
                pair["pair_result_sha"], frozen["history_as_of_watermark"],
            ),
        )
        active_conn.execute(
            """
            CREATE TEMP TRIGGER audit_activation_capture
            AFTER INSERT ON main.ledger_projection_outbox
            BEGIN
              INSERT INTO audit_activation_receipts(
                activation_receipt_sha, staging_candidate_id, receipt_json, created_at
              )
              SELECT audit_activation_receipt_sha(
                       candidate.candidate_id, candidate.source_sequence,
                       NEW.projection_sequence, NEW.snapshot_sha256, NEW.row_count,
                       NEW.state, NEW.generation, search.canonical_revision
                     ),
                     context.staging_candidate_id,
                     audit_activation_receipt_json(
                       candidate.candidate_id, candidate.source_sequence,
                       NEW.projection_sequence, NEW.snapshot_sha256, NEW.row_count,
                       NEW.state, NEW.generation, search.canonical_revision
                     ),
                     datetime('now')
              FROM audit_activation_context context
              JOIN candidates candidate
                ON candidate.raw_sha256 = context.raw_artifact_sha
               AND candidate.source_sequence > context.history_as_of_watermark
              JOIN search_projection_outbox search
                ON search.record_id = candidate.candidate_id
               AND search.projection_kind = 'candidate'
               AND search.content_version = 'candidate-v1'
               AND search.source_sequence = candidate.source_sequence
               AND search.state = 'pending'
              ORDER BY candidate.source_sequence DESC
              LIMIT 1;
              INSERT INTO audit_activation_maps(
                staging_candidate_id, legacy_candidate_id, source_sequence,
                raw_artifact_sha, pair_plan_sha, pair_result_sha,
                activation_receipt_sha, activated_at
              )
              SELECT context.staging_candidate_id, candidate.candidate_id,
                     candidate.source_sequence, context.raw_artifact_sha,
                     context.pair_plan_sha, context.pair_result_sha,
                     audit_activation_receipt_sha(
                       candidate.candidate_id, candidate.source_sequence,
                       NEW.projection_sequence, NEW.snapshot_sha256, NEW.row_count,
                       NEW.state, NEW.generation, search.canonical_revision
                     ),
                     datetime('now')
              FROM audit_activation_context context
              JOIN candidates candidate
                ON candidate.raw_sha256 = context.raw_artifact_sha
               AND candidate.source_sequence > context.history_as_of_watermark
              JOIN search_projection_outbox search
                ON search.record_id = candidate.candidate_id
               AND search.projection_kind = 'candidate'
               AND search.content_version = 'candidate-v1'
               AND search.source_sequence = candidate.source_sequence
               AND search.state = 'pending'
              ORDER BY candidate.source_sequence DESC
              LIMIT 1;
            END
            """
        )

    try:
        append_result = history_store.append_rows_idempotent(
            conn,
            [normalized],
            {
                "run_id": frozen["run_id"],
                "batch_id": frozen["batch_id"],
                "staging_candidate_id": staging_id,
                "activation_request_sha": request_sha,
            },
            commit_key=commit_key,
            request_sha256=request_sha,
            request_json=request_json,
            precommit_validator=install_atomic_bridge,
        )
    finally:
        if not conn.in_transaction:
            conn.execute("DROP TRIGGER IF EXISTS temp.audit_activation_capture")
            conn.execute("DROP TABLE IF EXISTS temp.audit_activation_context")
    activation = conn.execute(
        "SELECT * FROM audit_activation_maps WHERE staging_candidate_id=?",
        (staging_id,),
    ).fetchone()
    if activation is None:
        raise ValueError("append allocator committed without activation binding")
    if activation["source_sequence"] <= frozen["history_as_of_watermark"]:
        raise ValueError("activated source sequence is not above the watermark")
    result = {
        "staging_candidate_id": staging_id,
        "legacy_candidate_id": activation["legacy_candidate_id"],
        "source_sequence": activation["source_sequence"],
        "activation_receipt_sha": activation["activation_receipt_sha"],
        "replayed": bool(append_result.get("replayed")),
    }
    if fault_after_commit:
        raise ActivationCrash("fault injected after durable activation commit")
    return result


_MANDATORY_L1_FAMILIES = frozenset(
    {"exact", "authoritative_alias", "declared_parent"}
)
_L1_FAMILY_SEMANTICS = {
    "exact": "normalized_exact",
    "authoritative_alias": "authoritative_alias",
    "declared_parent": "declared_parent",
    "lineage": "typed_lineage",
    "fts": "lexical",
    "near_duplicate": "lexical_near_duplicate",
    "hash_dense": "lexical_approximation",
    "semantic": "semantic_rank_only",
    "metadata": "additive_shadow",
}


def fair_family_fusion(channel_rankings, lineage_by_candidate):
    """Deduplicate views/revisions and return one score per lineage/family."""
    if not isinstance(channel_rankings, dict) or not isinstance(
        lineage_by_candidate, dict
    ):
        raise ValueError("L1 rankings and lineage map must be objects")
    family_lineage_scores = {}
    lineage_candidates = {}
    mandatory = set()
    semantics = {}
    for family in sorted(channel_rankings):
        values = channel_rankings[family]
        if family not in _L1_FAMILY_SEMANTICS:
            raise ValueError("unknown L1 retrieval family")
        if not isinstance(values, list):
            raise ValueError("L1 family ranking must be an array")
        if values:
            semantics[family] = _L1_FAMILY_SEMANTICS[family]
        view_scores = {}
        for item in values:
            if not isinstance(item, dict) or set(item) != {
                "candidate_id", "query_view_id", "score"
            }:
                raise ValueError("L1 rank item schema is closed")
            candidate_id = item["candidate_id"]
            view_id = item["query_view_id"]
            score = item["score"]
            if (
                not isinstance(candidate_id, str)
                or not candidate_id
                or not isinstance(view_id, str)
                or not view_id
                or type(score) not in {int, float}
                or isinstance(score, bool)
                or score < 0
                or score != score
                or score in {float("inf"), float("-inf")}
            ):
                raise ValueError("L1 rank item is invalid")
            lineage_id = lineage_by_candidate.get(candidate_id)
            if not isinstance(lineage_id, str) or not lineage_id:
                raise ValueError("every ranked candidate requires one lineage")
            key = (candidate_id, view_id)
            view_scores[key] = max(float(score), view_scores.get(key, 0.0))
            lineage_candidates.setdefault(lineage_id, set()).add(candidate_id)
            if family in _MANDATORY_L1_FAMILIES:
                mandatory.add(lineage_id)
        for (candidate_id, _), score in view_scores.items():
            lineage_id = lineage_by_candidate[candidate_id]
            key = (family, lineage_id)
            family_lineage_scores[key] = max(
                score, family_lineage_scores.get(key, 0.0)
            )
    lineages = []
    for lineage_id in sorted(lineage_candidates):
        scores = {
            family: family_lineage_scores[(family, lineage_id)]
            for family in sorted(channel_rankings)
            if (family, lineage_id) in family_lineage_scores
        }
        lineages.append(
            {
                "lineage_id": lineage_id,
                "candidate_ids": sorted(lineage_candidates[lineage_id]),
                "family_scores": scores,
                "score": sum(scores.values()),
                "mandatory": lineage_id in mandatory,
            }
        )
    lineages.sort(key=lambda item: (-item["score"], item["lineage_id"]))
    return {"lineages": lineages, "family_semantics": semantics}


def select_l1_comparisons(fused, *, routine_cutoff):
    """Keep every mandatory lineage and then the bounded routine ranking."""
    if (
        not isinstance(fused, dict)
        or not isinstance(fused.get("lineages"), list)
        or type(routine_cutoff) is not int
        or routine_cutoff < 0
    ):
        raise ValueError("L1 fused result or routine cutoff is invalid")
    mandatory = sorted(
        item["lineage_id"] for item in fused["lineages"] if item.get("mandatory")
    )
    routine = [
        item["lineage_id"]
        for item in fused["lineages"]
        if not item.get("mandatory")
    ][:routine_cutoff]
    return {
        "mandatory_lineage_ids": mandatory,
        "routine_lineage_ids": routine,
        "selected_lineage_ids": mandatory + [
            lineage_id for lineage_id in routine if lineage_id not in mandatory
        ],
    }


def evaluate_l1_coverage(selection, adjudicated_lineage_ids):
    """Report mandatory-comparison coverage independently of semantic rank."""
    if not isinstance(selection, dict) or set(selection) != {
        "mandatory_lineage_ids", "routine_lineage_ids", "selected_lineage_ids"
    }:
        raise ValueError("L1 comparison selection is invalid")
    if (
        not isinstance(adjudicated_lineage_ids, (list, tuple))
        or any(not isinstance(value, str) or not value for value in adjudicated_lineage_ids)
        or len(set(adjudicated_lineage_ids)) != len(adjudicated_lineage_ids)
    ):
        raise ValueError("adjudicated lineage IDs are invalid")
    observed = set(adjudicated_lineage_ids)
    missing = sorted(
        set(selection["mandatory_lineage_ids"]).difference(observed)
    )
    return {
        "coverage_complete": not missing,
        "missing_mandatory_lineage_ids": missing,
        "exhausted_reason": "missing_mandatory_comparison" if missing else None,
    }


def derive_final_status(
    *, identity_valid, verified_hits, coverage_complete,
    adjudication_complete, semantic_policy_qualified,
    unresolved_conflict, exhausted_reason, no_match_basis
):
    """Return final_status and stage_reason_code in fixed priority order."""
    for value, name in (
        (identity_valid, "identity_valid"),
        (coverage_complete, "coverage_complete"),
        (adjudication_complete, "adjudication_complete"),
        (semantic_policy_qualified, "semantic_policy_qualified"),
        (unresolved_conflict, "unresolved_conflict"),
    ):
        if type(value) is not bool:
            raise ValueError(f"{name} must be boolean")
    if not isinstance(verified_hits, (list, tuple)):
        raise ValueError("verified_hits must be a sequence")
    if exhausted_reason is not None and (
        not isinstance(exhausted_reason, str) or not exhausted_reason
    ):
        raise ValueError("exhausted_reason must be null or nonempty text")
    if no_match_basis not in {None, "l1_calibrated", "l2_exhaustive"}:
        raise ValueError("no_match_basis is invalid")
    if not identity_valid:
        return "invalid", "invalid_identity"
    if verified_hits:
        complete = coverage_complete and adjudication_complete and exhausted_reason is None
        return (
            ("overlap_found", "match_found")
            if complete
            else ("overlap_found", "match_found_partial_coverage")
        )
    if not coverage_complete or exhausted_reason is not None:
        return "partial", exhausted_reason or "incomplete_coverage"
    if unresolved_conflict:
        return "uncertain", "conflict"
    if not adjudication_complete:
        return "partial", "adjudication_incomplete"
    if not semantic_policy_qualified:
        return "uncertain", "semantic_policy_unqualified"
    if no_match_basis is None:
        return "uncertain", "no_match_basis_missing"
    return "complete_no_match", "complete_no_match"


def _require_closed(value, fields, name):
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{name} fields are invalid")


def _validated_verified_hits(values):
    if not isinstance(values, list):
        raise ValueError("verified_hits must be an array")
    result = []
    seen = set()
    for item in values:
        if not isinstance(item, dict) or set(item) != {
            "lineage_id", "source", "semantic_relation"
        }:
            raise ValueError("verified hit schema is closed")
        if (
            not isinstance(item["lineage_id"], str)
            or not item["lineage_id"]
            or item["source"] not in {
                "normalized_exact", "confirmed_typed_relation"
            }
            or item["semantic_relation"] not in {
                "blocking_duplicate", "substantive_overlap"
            }
        ):
            raise ValueError("verified hit lacks qualifying evidence")
        identity = (
            item["lineage_id"], item["source"], item["semantic_relation"]
        )
        if identity in seen:
            raise ValueError("verified hit is duplicated")
        seen.add(identity)
        result.append(dict(item))
    return result


def build_l1_receipt(snapshot, retrieval, adjudication, qualification):
    """Build a closed v2 L1 receipt without legacy permanence."""
    _require_closed(retrieval, _RETRIEVAL_FIELDS, "retrieval")
    _require_closed(adjudication, _ADJUDICATION_FIELDS, "adjudication")
    _require_closed(qualification, _QUALIFICATION_FIELDS, "qualification")
    required_snapshot = {
        "snapshot_id", "snapshot_hash", "history_as_of_watermark",
        "current_batch_id_namespace", "current_batch_ids_hash",
        "exclusion_policy_sha", "expected_asset_ids_hash",
    }
    if not isinstance(snapshot, dict) or not required_snapshot.issubset(snapshot):
        raise ValueError("snapshot fields are incomplete")
    verified_hits = _validated_verified_hits(adjudication["verified_hits"])
    if retrieval["coverage_complete"] and (
        retrieval["missing_ids"]
        or retrieval["duplicate_ids"]
        or retrieval["extra_ids"]
        or retrieval["invalid_schema"]
        or retrieval["invalid_anchor"]
        or retrieval["truncated"]
    ):
        raise ValueError("complete L1 coverage contains a coverage fault")
    if qualification["no_match_basis"] == "l2_exhaustive":
        raise ValueError("an L1 receipt cannot claim an L2 no-match basis")
    identity_valid = not (
        retrieval["invalid_schema"] or retrieval["invalid_anchor"]
    )
    final_status, stage_reason_code = derive_final_status(
        identity_valid=identity_valid,
        verified_hits=verified_hits,
        coverage_complete=retrieval["coverage_complete"],
        adjudication_complete=adjudication["adjudication_complete"],
        semantic_policy_qualified=qualification["semantic_policy_qualified"],
        unresolved_conflict=adjudication["unresolved_conflict"],
        exhausted_reason=adjudication["exhausted_reason"],
        no_match_basis=qualification["no_match_basis"],
    )
    basis = (
        qualification["no_match_basis"]
        if final_status == "complete_no_match"
        else None
    )
    receipt = {
        "manifest_schema_version": contract.MANIFEST_SCHEMA_VERSION,
        "canonical_codec_version": contract.CANONICAL_CODEC_VERSION,
        "run_id": retrieval["run_id"],
        "plan_hash": retrieval["plan_hash"],
        "candidate_hash": retrieval["candidate_hash"],
        "snapshot_id": snapshot["snapshot_id"],
        "snapshot_hash": snapshot["snapshot_hash"],
        "history_as_of_watermark": snapshot["history_as_of_watermark"],
        "current_batch_id_namespace": snapshot["current_batch_id_namespace"],
        "current_batch_ids_hash": snapshot["current_batch_ids_hash"],
        "exclusion_policy_sha": snapshot["exclusion_policy_sha"],
        "expected_asset_ids_hash": snapshot["expected_asset_ids_hash"],
        "observed_asset_ids_hash": retrieval["observed_asset_ids_hash"],
        "missing_ids": retrieval["missing_ids"],
        "duplicate_ids": retrieval["duplicate_ids"],
        "extra_ids": retrieval["extra_ids"],
        "invalid_schema": retrieval["invalid_schema"],
        "invalid_anchor": retrieval["invalid_anchor"],
        "truncated": retrieval["truncated"],
        "provider_pools_ordered": retrieval["provider_pools_ordered"],
        "provider_capability_profile_hashes": retrieval["provider_capability_profile_hashes"],
        "capacity_profile_id": retrieval["capacity_profile_id"],
        "semantic_policy_profile_id": qualification["semantic_policy_profile_id"],
        "risk_policy_version": retrieval["risk_policy_version"],
        "matched_router_rule_ids": retrieval["matched_router_rule_ids"],
        "settlement_policy_sha": retrieval["settlement_policy_sha"],
        "shard_plan_sha": retrieval["shard_plan_sha"],
        "logical_task_hashes": retrieval["logical_task_hashes"],
        "attempt_manifest_hashes": retrieval["attempt_manifest_hashes"],
        "raw_request_output_cas_hashes": retrieval["raw_request_output_cas_hashes"],
        "minimum_receipt_sha": retrieval["minimum_receipt_sha"],
        "coverage_complete": retrieval["coverage_complete"],
        "adjudication_complete": adjudication["adjudication_complete"],
        "semantic_policy_qualified": qualification["semantic_policy_qualified"],
        "no_match_basis": basis,
        "final_status": final_status,
        "stage_reason_code": stage_reason_code,
        "evidence_anchors": adjudication["evidence_anchors"],
    }
    return contract.validate_receipt(receipt)
