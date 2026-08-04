#!/usr/bin/env python3
"""Deterministic, fake-provider-only L2 audit execution runtime."""

import copy
import datetime
import hashlib
import json
import sqlite3

try:
    from lib import history_audit
    from lib import history_audit_plan
    from lib import history_audit_store
    from lib import history_cas
    from lib import history_contract_v2
except ImportError:
    import history_audit
    import history_audit_plan
    import history_audit_store
    import history_cas
    import history_contract_v2


MAP_SCHEMA = "history-map-output-v1"
DETAIL_REQUEST_SCHEMA = "history-detail-request-v1"
DETAIL_OUTPUT_SCHEMA = "history-detail-output-v1"
REDUCE_REQUEST_SCHEMA = "history-reduce-request-v1"
REDUCE_OUTPUT_SCHEMA = "history-reduce-output-v1"
SEMANTIC_RELATIONS = frozenset(
    {"blocking_duplicate", "substantive_overlap", "related_only", "distinct", "uncertain"}
)
LINEAGE_RELATIONS = frozenset(
    {"same_revision", "evolved_from", "recheck_of", "supersedes", "none"}
)
TERMINAL_STATES = frozenset({"settled", "superseded", "exhausted"})
MAX_ATTEMPTS = 2


class ExecutionError(RuntimeError):
    def __init__(self, code, detail=None):
        self.code = code
        self.detail = detail
        super().__init__(code)


class MapValidationError(ExecutionError):
    pass


class ExecutionCrash(RuntimeError):
    pass


def _now(value=None):
    value = value or datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ExecutionError("invalid_timestamp") from exc
    if parsed.tzinfo is None:
        raise ExecutionError("invalid_timestamp")
    return parsed.astimezone(datetime.timezone.utc)


def _canonical(value):
    return history_contract_v2.canonical_bytes(value).decode("utf-8")


def _attempt_expiry(task):
    return (_now(task["created_at"]) + datetime.timedelta(days=7)).isoformat()


def _sha(domain, value):
    return history_contract_v2.framed_sha256(
        domain, history_contract_v2.canonical_bytes(value)
    )


def _json(text):
    return json.loads(text)


def _require_sha(value, name):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ExecutionError("invalid_identity", name)


def persist_plan(conn, plan, *, route_authority=None):
    """Persist frozen plan/task bindings once before any claim or attempt."""
    if route_authority is not None:
        raise ExecutionError("caller_route_authority_forbidden")
    required = {
        "run_id", "batch_id", "plan_sha", "candidate", "snapshot",
        "provider_pools_ordered", "shard_plan_sha", "shards", "logical_task_keys",
    }
    if not isinstance(plan, dict) or not required.issubset(plan):
        raise ExecutionError("invalid_plan")
    if len(plan["shards"]) != len(plan["logical_task_keys"]):
        raise ExecutionError("invalid_plan")
    try:
        material = history_audit_plan.build_runtime_plan_material(plan)
        computed_plan_sha = history_audit_plan.runtime_plan_sha_from_material(material)
        records = history_audit_plan.runtime_snapshot_records(
            plan["snapshot"]["records"]
        )
    except history_audit_plan.AuditPlanError as exc:
        raise ExecutionError("frozen_identity_mismatch", exc.code) from exc
    if (
        plan.get("plan_sha") != computed_plan_sha
        or plan.get("shard_plan_sha") != material["shard_plan_sha"]
        or sorted(item["item_id"] for item in records)
        != material["snapshot"]["expected_asset_ids"]
        or plan["candidate"]["candidate_id"]
        not in material["snapshot"]["current_batch_ids"]
    ):
        raise ExecutionError("frozen_identity_mismatch")
    snapshot = plan["snapshot"]
    record_by_id = {item["item_id"]: copy.deepcopy(item) for item in records}
    plan_json = _canonical(material)
    records_json = _canonical(records)
    if conn.in_transaction:
        raise ExecutionError("persist_plan_requires_idle_connection")
    if material.get("authority_scope") == "production":
        try:
            history_audit_store._require_host_router_preplan_chain(
                conn, material
            )
        except history_audit_store.AuditMigrationError as exc:
            raise ExecutionError(
                "host_route_authority_required", str(exc)
            ) from exc
    try:
        budget_receipt = history_audit_store.issue_candidate_budget_receipt(
            conn, material, plan["plan_sha"]
        )
    except history_audit_store.AuditMigrationError as exc:
        raise ExecutionError("candidate_budget_authority_unavailable") from exc
    if budget_receipt["decision"] == "rejected":
        raise ExecutionError("candidate_budget_exceeded")
    created_at = budget_receipt["decided_at"]
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT plan_hash, manifest_json, created_at "
            "FROM audit_run_manifests WHERE run_id=?",
            (plan["run_id"],),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO audit_run_manifests VALUES(?, ?, ?, ?, ?)",
                (
                    plan["run_id"], "history-audit-manifest-v2", plan["plan_sha"],
                    plan_json, created_at,
                ),
            )
        else:
            if tuple(existing)[:2] != (plan["plan_sha"], plan_json):
                raise ExecutionError("run_plan_conflict")
            created_at = existing["created_at"]
        stored_snapshot = conn.execute(
            "SELECT snapshot_hash, run_id FROM audit_snapshots WHERE snapshot_id=?",
            (snapshot["snapshot_id"],),
        ).fetchone()
        if stored_snapshot is None:
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
                    snapshot["snapshot_id"], snapshot["snapshot_hash"],
                    snapshot["history_as_of_watermark"],
                    snapshot["current_batch_id_namespace"],
                    snapshot["current_batch_ids_hash"], snapshot["exclusion_policy_sha"],
                    snapshot["expected_asset_ids_hash"], created_at,
                    plan["run_id"], plan["batch_id"],
                ),
            )
        elif tuple(stored_snapshot) != (snapshot["snapshot_hash"], plan["run_id"]):
            raise ExecutionError("snapshot_conflict")
        conn.execute(
            """
            INSERT OR IGNORE INTO audit_snapshot_batch_sets(
              snapshot_id, run_id, batch_id, current_batch_ids_hash,
              member_ids_json, member_count, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot["snapshot_id"], plan["run_id"], plan["batch_id"],
                snapshot["current_batch_ids_hash"],
                json.dumps(
                    snapshot["current_batch_ids"], sort_keys=True,
                    separators=(",", ":"), ensure_ascii=False,
                ),
                len(snapshot["current_batch_ids"]), created_at,
            ),
        )
        stored_l2_plan = conn.execute(
            "SELECT * FROM audit_l2_plans_v2 WHERE run_id=?",
            (plan["run_id"],),
        ).fetchone()
        if stored_l2_plan is None:
            try:
                route_candidates = (
                    history_audit_store
                    ._router_candidate_cohort_for_plan_persistence(
                        conn, plan["run_id"]
                    )
                )
            except history_audit_store.AuditMigrationError as exc:
                code = str(exc)
                if code in {
                    "router_round_authority_unavailable",
                    "router_plan_authority_unavailable",
                }:
                    raise ExecutionError("route_authority_required") from exc
                raise ExecutionError("invalid_route_authority", code) from exc
            for candidate in route_candidates:
                history_audit_store.insert_authorized_batch_staging(
                    conn,
                    staging_candidate_id=candidate["candidate_id"],
                    run_id=plan["run_id"], batch_id=plan["batch_id"],
                    candidate_hash=candidate["candidate_hash"],
                    raw_artifact_sha=candidate["raw_artifact_sha"],
                    source_order=candidate["source_order"],
                    created_at=created_at,
                )
        conn.execute(
            """
            INSERT OR IGNORE INTO audit_l2_snapshot_records_v2(
              snapshot_id, records_sha, records_json, created_at
            ) VALUES(?, ?, ?, ?)
            """,
            (
                snapshot["snapshot_id"], material["snapshot"]["records_sha"],
                records_json, created_at,
            ),
        )
        plan_values = (
            plan["plan_sha"], plan["run_id"],
            plan["candidate"]["candidate_id"],
            plan["candidate"]["candidate_hash"],
            snapshot["snapshot_id"], snapshot["snapshot_hash"],
            plan["shard_plan_sha"], material["budget_policy_sha"],
            plan["intent"], plan_json, created_at,
        )
        if stored_l2_plan is None:
            try:
                history_audit_store._materialize_final_candidate_routes_for_plan(
                    conn, plan["run_id"], created_at=created_at,
                )
            except history_audit_store.AuditMigrationError as exc:
                code = str(exc)
                if code in {
                    "router_final_phase_unavailable",
                    "router_round_authority_unavailable",
                }:
                    raise ExecutionError("route_authority_required") from exc
                if code == "selected_route_identity_mismatch":
                    raise ExecutionError(code) from exc
                raise ExecutionError("invalid_route_authority", code) from exc
        elif (
            tuple(stored_l2_plan) != plan_values
            or not history_audit_store._candidate_route_materialization_replay_matches(
                conn, plan["plan_sha"], created_at=created_at,
            )
        ):
            raise ExecutionError("invalid_route_authority")
        conn.execute(
            """
            INSERT OR IGNORE INTO audit_shard_plans(
              shard_plan_sha, run_id, snapshot_id, expected_asset_ids_hash,
              plan_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                plan["shard_plan_sha"], plan["run_id"], snapshot["snapshot_id"],
                snapshot["expected_asset_ids_hash"], _canonical(plan["shards"]), created_at,
            ),
        )
        if stored_l2_plan is None:
            try:
                history_audit_store._insert_new_l2_plan_with_dispatch(
                    conn, plan_values,
                )
            except history_audit_store.AuditMigrationError as exc:
                raise ExecutionError("invalid_route_dispatch") from exc
        elif not history_audit_store.candidate_l2_dispatch_replay_matches(
            conn, plan["plan_sha"], created_at=created_at,
        ):
            raise ExecutionError("invalid_route_dispatch")
        for task_hash, shard in zip(plan["logical_task_keys"], plan["shards"]):
            _require_sha(task_hash, "task_hash")
            item_ids = shard.get("item_ids")
            if (
                not isinstance(item_ids, list)
                or not item_ids
                or item_ids != sorted(item_ids)
                or len(set(item_ids)) != len(item_ids)
                or any(item_id not in record_by_id for item_id in item_ids)
            ):
                raise ExecutionError("invalid_shard")
            request_bytes = shard["serialized_request"].encode("utf-8")
            request_sha = hashlib.sha256(request_bytes).hexdigest()
            if shard.get("request_sha256") != request_sha:
                raise ExecutionError("shard_request_hash_mismatch")
            expected_task_hash = history_contract_v2.logical_task_key(
                plan["plan_sha"], "map", plan["candidate"]["candidate_id"], request_sha
            )
            if task_hash != expected_task_hash:
                raise ExecutionError("logical_task_identity_mismatch")
            conn.execute(
                """
                INSERT OR IGNORE INTO audit_logical_tasks(
                  task_hash, run_id, stage, staging_candidate_id, input_id,
                  state, fence, claim_token, lease_until, created_at
                ) VALUES(?, ?, 'map', ?, ?, 'planned', 0, NULL, NULL, ?)
                """,
                (
                    task_hash, plan["run_id"], plan["candidate"]["candidate_id"],
                    shard["shard_id"], created_at,
                ),
            )
            frozen = [record_by_id[item_id] for item_id in item_ids]
            conn.execute(
                """
                INSERT OR IGNORE INTO audit_task_bindings_v2(
                  task_hash, plan_sha, snapshot_id, snapshot_hash, shard_input_sha,
                  assigned_item_ids_json, frozen_records_json, provider_pool_json,
                  parent_task_hash, split_depth, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?)
                """,
                (
                    task_hash, plan["plan_sha"], snapshot["snapshot_id"],
                    snapshot["snapshot_hash"], request_sha, _canonical(item_ids),
                    _canonical(frozen), _canonical(plan["provider_pools_ordered"]["map"]),
                    created_at,
                ),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO audit_l2_task_inputs_v2(
                  task_hash, input_id, request_sha, request_text,
                  item_ids_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    task_hash, shard["shard_id"], request_sha,
                    shard["serialized_request"], _canonical(item_ids), created_at,
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return list(plan["logical_task_keys"])


def load_task(conn, task_key):
    row = conn.execute(
        """
        SELECT task.*, binding.plan_sha, binding.snapshot_id, binding.snapshot_hash,
               binding.shard_input_sha, binding.assigned_item_ids_json,
               binding.frozen_records_json, binding.provider_pool_json,
               binding.parent_task_hash, binding.split_depth,
               snapshot.history_as_of_watermark,
               snapshot.current_batch_id_namespace,
               snapshot.current_batch_ids_hash,
               snapshot.exclusion_policy_sha,
               snapshot.expected_asset_ids_hash,
               records.records_json AS durable_snapshot_records_json,
               plan.plan_json AS durable_plan_json,
               task_input.request_text AS durable_request_text,
               task_input.request_sha AS durable_request_sha
        FROM audit_logical_tasks task
        JOIN audit_task_bindings_v2 binding ON binding.task_hash=task.task_hash
        JOIN audit_snapshots snapshot ON snapshot.snapshot_id=binding.snapshot_id
        JOIN audit_l2_snapshot_records_v2 records
          ON records.snapshot_id=binding.snapshot_id
        JOIN audit_l2_plans_v2 plan ON plan.plan_sha=binding.plan_sha
        JOIN audit_l2_task_inputs_v2 task_input
          ON task_input.task_hash=task.task_hash
        WHERE task.task_hash=?
        """,
        (task_key,),
    ).fetchone()
    if row is None:
        stranded = conn.execute(
            """
            SELECT 1 FROM audit_logical_tasks task
            JOIN audit_task_bindings_v2 binding ON binding.task_hash=task.task_hash
            WHERE task.task_hash=?
            """,
            (task_key,),
        ).fetchone()
        if stranded is not None:
            raise ExecutionError("stranded_l2_authority")
        raise ExecutionError("unknown_task")
    result = dict(row)
    result["assigned_item_ids"] = _json(result.pop("assigned_item_ids_json"))
    result["frozen_records"] = _json(result.pop("frozen_records_json"))
    result["provider_pool"] = _json(result.pop("provider_pool_json"))
    result["durable_snapshot_records"] = _json(
        result.pop("durable_snapshot_records_json")
    )
    result["durable_plan"] = _json(result.pop("durable_plan_json"))
    return result


def _map_leaf_material(conn, plan_sha):
    rows = conn.execute(
        """
        SELECT task.task_hash, task.state,
               binding.assigned_item_ids_json,
               settlement.settlement_sha256, settlement.settlement_kind,
               settlement.normalized_result_json,
               terminal.fact_sha256 AS terminal_fact_sha,
               terminal.reason AS terminal_reason
        FROM audit_logical_tasks task
        JOIN audit_task_bindings_v2 binding ON binding.task_hash=task.task_hash
        LEFT JOIN audit_task_settlements_v2 settlement
          ON settlement.task_hash=task.task_hash
        LEFT JOIN audit_task_terminal_facts_v2 terminal
          ON terminal.task_hash=task.task_hash
        WHERE binding.plan_sha=? AND task.stage='map'
          AND NOT EXISTS (
            SELECT 1 FROM audit_task_edges_v2 edge
            WHERE edge.parent_task_hash=task.task_hash
          )
        ORDER BY task.task_hash
        """,
        (plan_sha,),
    ).fetchall()
    if not rows:
        raise ExecutionError("missing_map_leaves")
    if any(row["state"] not in {"settled", "exhausted"} for row in rows):
        return None
    terminal_facts = []
    map_rows = []
    source_pairs = {}
    for row in rows:
        if row["state"] == "settled":
            fact_sha = row["settlement_sha256"]
            if fact_sha is None:
                raise ExecutionError("settled_task_missing_settlement")
            if row["settlement_kind"] == "equal":
                normalized = _json(row["normalized_result_json"])
                if (
                    not isinstance(normalized, dict)
                    or normalized.get("schema_version") != MAP_SCHEMA
                    or not isinstance(normalized.get("items"), list)
                ):
                    raise ExecutionError("invalid_map_settlement")
                for item in normalized["items"]:
                    map_rows.append(copy.deepcopy(item))
                    if item.get("semantic_relation") in {
                        "blocking_duplicate", "substantive_overlap", "uncertain",
                    }:
                        source_pairs.setdefault(item["item_id"], set()).add(
                            (row["task_hash"], fact_sha)
                        )
        else:
            fact_sha = row["terminal_fact_sha"]
            if fact_sha is None:
                raise ExecutionError("exhausted_task_missing_terminal_fact")
        terminal_facts.append(
            {
                "task_hash": row["task_hash"],
                "state": row["state"],
                "fact_sha256": fact_sha,
            }
        )
    cards = history_audit._collapse_l2_exceptional_cards(map_rows)
    return terminal_facts, cards, source_pairs


def _adjudication_generation_material(plan, terminal_facts, cards):
    try:
        material = history_audit_plan.build_runtime_plan_material(plan)
        plan_sha = history_audit_plan.runtime_plan_sha_from_material(material)
    except history_audit_plan.AuditPlanError as exc:
        raise ExecutionError("frozen_identity_mismatch", exc.code) from exc
    if plan_sha != plan.get("plan_sha"):
        raise ExecutionError("frozen_identity_mismatch")
    terminal_facts = sorted(
        copy.deepcopy(terminal_facts), key=lambda value: value["task_hash"]
    )
    cards = sorted(copy.deepcopy(cards), key=lambda value: value["lineage_id"])
    generation = {
        "schema_version": "history-l2-adjudication-generation-v1",
        "plan_sha": plan_sha,
        "snapshot_id": material["snapshot"]["snapshot_id"],
        "snapshot_hash": material["snapshot"]["snapshot_hash"],
        "candidate_id": material["candidate"]["candidate_id"],
        "candidate_hash": material["candidate"]["candidate_hash"],
        "map_terminal_facts": terminal_facts,
        "map_terminal_set_sha": _sha(
            "history-l2-map-terminal-set-v1", terminal_facts
        ),
        "exceptional_cards": cards,
        "exceptional_cards_sha": _sha(
            "history-l2-exceptional-cards-v1", cards
        ),
        "detail_provider_pool": copy.deepcopy(
            material["provider_pools_ordered"]["detail"]
        ),
        "reduce_provider_pool": copy.deepcopy(
            material["provider_pools_ordered"]["reduce"]
        ),
        "capacity_profile_sha": _sha(
            "history-l2-capacity-profile-identity-v1",
            material["capacity_profile"],
        ),
        "prompt_identity_sha": _sha(
            "history-l2-prompt-identity-v1",
            material["capacity_profile"].get("prompt"),
        ),
        "schema_identity_sha": _sha(
            "history-l2-schema-identity-v1",
            material["capacity_profile"].get("schema"),
        ),
        "budget_policy_sha": material["budget_policy_sha"],
        "risk_policy_sha": material["risk_policy_sha"],
        "settlement_policy_sha": material["settlement_policy_sha"],
    }
    return generation, _sha("history-l2-adjudication-generation-v1", generation)


def _detail_task_specs(plan, generation, generation_id, source_pairs):
    records = {
        record["item_id"]: copy.deepcopy(record)
        for record in plan["snapshot"]["records"]
    }
    specs = []
    index = 0
    for card in generation["exceptional_cards"]:
        for item_id in card["item_ids"]:
            pairs = sorted(source_pairs.get(item_id, ()))
            if not pairs:
                raise ExecutionError("exceptional_card_missing_map_source")
            full_records = [records[item_id]]
            detail_card = {
                "lineage_id": card["lineage_id"],
                "semantic_relation": card["semantic_relation"],
                "item_ids": [item_id],
                "evidence": [
                    copy.deepcopy(anchor) for anchor in card["evidence"]
                    if anchor["asset_id"] == item_id
                ],
            }
            if not detail_card["evidence"]:
                raise ExecutionError("exceptional_card_missing_map_anchor")
            request = {
                "schema_version": DETAIL_REQUEST_SCHEMA,
                "generation_id": generation_id,
                "plan_sha": plan["plan_sha"],
                "snapshot_id": plan["snapshot"]["snapshot_id"],
                "snapshot_hash": plan["snapshot"]["snapshot_hash"],
                "candidate_id": plan["candidate"]["candidate_id"],
                "candidate_hash": plan["candidate"]["candidate_hash"],
                "source_task_hashes": [pair[0] for pair in pairs],
                "source_settlement_hashes": [pair[1] for pair in pairs],
                "exceptional_card": detail_card,
                "full_records": full_records,
            }
            request_text = _canonical(request)
            request_sha = hashlib.sha256(request_text.encode("utf-8")).hexdigest()
            task_hash = history_contract_v2.logical_task_key(
                plan["plan_sha"], "detail", plan["candidate"]["candidate_id"],
                request_sha,
            )
            input_id = f"detail-{index:04d}"
            authority = {
                "schema_version": "history-l2-derived-task-authority-v1",
                "generation_id": generation_id,
                "plan_sha": plan["plan_sha"],
                "stage": "detail",
                "task_hash": task_hash,
                "input_id": input_id,
                "request_sha256": request_sha,
                "assigned_item_ids": [item_id],
                "source_task_hashes": request["source_task_hashes"],
                "source_settlement_hashes": request["source_settlement_hashes"],
            }
            specs.append(
                {
                    "task_hash": task_hash,
                    "input_id": input_id,
                    "request_text": request_text,
                    "request_sha": request_sha,
                    "assigned_item_ids": [item_id],
                    "frozen_records": full_records,
                    "provider_pool": copy.deepcopy(
                        plan["provider_pools_ordered"]["detail"]
                    ),
                    "authority": authority,
                    "authority_sha": _sha(
                        "history-l2-derived-task-authority-v1", authority
                    ),
                }
            )
            index += 1
    return specs


def _stored_adjudication_replay(conn, plan_sha, generation_id, generation, specs):
    row = conn.execute(
        """
        SELECT generation_id, material_json
        FROM audit_l2_adjudication_generations_v2 WHERE plan_sha=?
        """,
        (plan_sha,),
    ).fetchone()
    if row is None:
        return False
    expected_hashes = sorted(spec["task_hash"] for spec in specs)
    stored_hashes = [
        item["task_hash"]
        for item in conn.execute(
            """
            SELECT authority.task_hash
            FROM audit_l2_derived_task_authority_v2 authority
            WHERE authority.generation_id=? AND authority.stage='detail'
            ORDER BY authority.task_hash
            """,
            (generation_id,),
        )
    ]
    valid_hashes = [
        item["task_hash"]
        for item in conn.execute(
            """
            SELECT valid.task_hash
            FROM audit_l2_valid_adjudication_task_authority_v2 valid
            JOIN audit_l2_derived_task_authority_v2 authority
              ON authority.task_hash=valid.task_hash
            WHERE authority.generation_id=? AND authority.stage='detail'
            ORDER BY valid.task_hash
            """,
            (generation_id,),
        )
    ]
    if (
        row["generation_id"] != generation_id
        or row["material_json"] != _canonical(generation)
        or stored_hashes != expected_hashes
        or valid_hashes != expected_hashes
    ):
        raise ExecutionError("adjudication_generation_conflict")
    return True


def materialize_adjudication_tasks(conn, cas_root, plan, *, now=None):
    """Create an immutable generation and its required detail tasks once."""
    if conn.in_transaction:
        raise ExecutionError("adjudication_materialization_requires_idle_connection")
    leaf_material = _map_leaf_material(conn, plan["plan_sha"])
    if leaf_material is None:
        return {"state": "waiting_for_map", "detail_task_hashes": []}
    if not history_audit_store.validate_l2_terminal_graph(conn, plan["plan_sha"]):
        raise ExecutionError("malformed_l2_terminal_graph")
    recover_run(conn, plan["plan_sha"], cas_root=cas_root, now=now)
    terminal_facts, cards, source_pairs = leaf_material
    generation, generation_id = _adjudication_generation_material(
        plan, terminal_facts, cards
    )
    specs = _detail_task_specs(plan, generation, generation_id, source_pairs)
    if _stored_adjudication_replay(
        conn, plan["plan_sha"], generation_id, generation, specs
    ):
        return {
            "state": "materialized",
            "generation_id": generation_id,
            "detail_task_hashes": [spec["task_hash"] for spec in specs],
        }
    created_at = _now(now).isoformat()
    generation_values = (
        generation_id, plan["plan_sha"], _canonical(generation), created_at,
    )
    task_values = []
    binding_values = []
    input_values = []
    authority_values = []
    for spec in specs:
        task_values.append(
            (
                spec["task_hash"], plan["run_id"], "detail",
                plan["candidate"]["candidate_id"], spec["input_id"],
            )
        )
        binding_values.append(
            (
                spec["task_hash"], plan["plan_sha"],
                plan["snapshot"]["snapshot_id"],
                plan["snapshot"]["snapshot_hash"], spec["request_sha"],
                _canonical(spec["assigned_item_ids"]),
                _canonical(spec["frozen_records"]),
                _canonical(spec["provider_pool"]), None, 0, created_at,
            )
        )
        input_values.append(
            (
                spec["task_hash"], spec["input_id"], spec["request_sha"],
                spec["request_text"], _canonical(spec["assigned_item_ids"]),
                created_at,
            )
        )
        authority = spec["authority"]
        authority_values.append(
            (
                spec["task_hash"], generation_id, plan["plan_sha"], "detail",
                _canonical(authority), spec["authority_sha"], created_at,
            )
        )
    try:
        conn.execute("BEGIN IMMEDIATE")
        with history_audit_store.l2_adjudication_materialization_guard(
            conn,
            generation=generation_values,
            tasks=task_values,
            bindings=binding_values,
            inputs=input_values,
            authorities=authority_values,
        ):
            conn.execute(
                """
                INSERT INTO audit_l2_adjudication_generations_v2(
                  generation_id, plan_sha, material_json, created_at
                ) VALUES(?, ?, ?, ?)
                """,
                generation_values,
            )
            for task, binding, task_input, authority in zip(
                task_values, binding_values, input_values, authority_values
            ):
                conn.execute(
                    """
                    INSERT INTO audit_logical_tasks(
                      task_hash, run_id, stage, staging_candidate_id, input_id,
                      state, fence, claim_token, lease_until, created_at
                    ) VALUES(?, ?, ?, ?, ?, 'planned', 0, NULL, NULL, ?)
                    """,
                    (*task, created_at),
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
                    binding,
                )
                conn.execute(
                    """
                    INSERT INTO audit_l2_task_inputs_v2(
                      task_hash, input_id, request_sha, request_text,
                      item_ids_json, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    task_input,
                )
                conn.execute(
                    """
                    INSERT INTO audit_l2_derived_task_authority_v2(
                      task_hash, generation_id, plan_sha, stage,
                      authority_json, authority_sha256, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    authority,
                )
            valid_count = conn.execute(
                """
                SELECT count(*)
                FROM audit_l2_valid_adjudication_task_authority_v2 valid
                JOIN audit_l2_derived_task_authority_v2 authority
                  ON authority.task_hash=valid.task_hash
                WHERE authority.generation_id=? AND authority.stage='detail'
                """,
                (generation_id,),
            ).fetchone()[0]
            if valid_count != len(specs):
                raise ExecutionError("invalid_derived_task_authority")
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {
        "state": "materialized",
        "generation_id": generation_id,
        "detail_task_hashes": [spec["task_hash"] for spec in specs],
    }


def _reduce_task_specs(plan, generation_id, entries):
    item_cap = plan["capacity_profile"].get("item_cap", 1)
    if type(item_cap) is not int or item_cap <= 0:
        raise ExecutionError("invalid_capacity_profile")
    specs = []
    entries = sorted(
        copy.deepcopy(entries),
        key=lambda value: (
            value["detail_card"]["lineage_id"], value["task_hash"]
        ),
    )
    for index, start in enumerate(range(0, len(entries), item_cap)):
        chunk = entries[start:start + item_cap]
        pairs = sorted(
            (entry["task_hash"], entry["settlement_sha256"])
            for entry in chunk
        )
        detail_cards = [entry["detail_card"] for entry in chunk]
        assigned = sorted({
            item_id
            for card in detail_cards
            for item_id in card["item_ids"]
        })
        request = {
            "schema_version": REDUCE_REQUEST_SCHEMA,
            "generation_id": generation_id,
            "plan_sha": plan["plan_sha"],
            "snapshot_id": plan["snapshot"]["snapshot_id"],
            "snapshot_hash": plan["snapshot"]["snapshot_hash"],
            "candidate_id": plan["candidate"]["candidate_id"],
            "candidate_hash": plan["candidate"]["candidate_hash"],
            "source_task_hashes": [pair[0] for pair in pairs],
            "source_settlement_hashes": [pair[1] for pair in pairs],
            "detail_cards": detail_cards,
        }
        request_text = _canonical(request)
        request_sha = hashlib.sha256(request_text.encode("utf-8")).hexdigest()
        task_hash = history_contract_v2.logical_task_key(
            plan["plan_sha"], "reduce", plan["candidate"]["candidate_id"],
            request_sha,
        )
        input_id = f"reduce-{index:04d}"
        authority = {
            "schema_version": "history-l2-derived-task-authority-v1",
            "generation_id": generation_id,
            "plan_sha": plan["plan_sha"],
            "stage": "reduce",
            "task_hash": task_hash,
            "input_id": input_id,
            "request_sha256": request_sha,
            "assigned_item_ids": assigned,
            "source_task_hashes": request["source_task_hashes"],
            "source_settlement_hashes": request["source_settlement_hashes"],
        }
        specs.append(
            {
                "task_hash": task_hash,
                "input_id": input_id,
                "request_text": request_text,
                "request_sha": request_sha,
                "assigned_item_ids": assigned,
                "frozen_records": [],
                "provider_pool": copy.deepcopy(
                    plan["provider_pools_ordered"]["reduce"]
                ),
                "authority": authority,
                "authority_sha": _sha(
                    "history-l2-derived-task-authority-v1", authority
                ),
            }
        )
    return specs


def _stored_reduce_replay(conn, generation_id, specs):
    expected_hashes = sorted(spec["task_hash"] for spec in specs)
    stored_hashes = [
        row["task_hash"]
        for row in conn.execute(
            """
            SELECT task_hash FROM audit_l2_derived_task_authority_v2
            WHERE generation_id=? AND stage='reduce' ORDER BY task_hash
            """,
            (generation_id,),
        )
    ]
    if not stored_hashes:
        return False
    valid_hashes = [
        row["task_hash"]
        for row in conn.execute(
            """
            SELECT valid.task_hash
            FROM audit_l2_valid_adjudication_task_authority_v2 valid
            JOIN audit_l2_derived_task_authority_v2 authority
              ON authority.task_hash=valid.task_hash
            WHERE authority.generation_id=? AND authority.stage='reduce'
            ORDER BY valid.task_hash
            """,
            (generation_id,),
        )
    ]
    if stored_hashes != expected_hashes or valid_hashes != expected_hashes:
        raise ExecutionError("adjudication_generation_conflict")
    return True


def materialize_reduce_tasks(conn, cas_root, plan, *, now=None):
    """Create bounded reducers only after every required detail settles equally."""
    if conn.in_transaction:
        raise ExecutionError("adjudication_materialization_requires_idle_connection")
    generation_row = conn.execute(
        """
        SELECT generation_id, material_json
        FROM audit_l2_adjudication_generations_v2 WHERE plan_sha=?
        """,
        (plan["plan_sha"],),
    ).fetchone()
    if generation_row is None:
        return {"state": "waiting_for_generation", "reduce_task_hashes": []}
    generation_id = generation_row["generation_id"]
    generation = _json(generation_row["material_json"])
    if not generation["exceptional_cards"]:
        return {
            "state": "not_required", "generation_id": generation_id,
            "reduce_task_hashes": [],
        }
    detail_rows = conn.execute(
        """
        SELECT task.task_hash, task.state, settlement.settlement_sha256,
               settlement.settlement_kind, settlement.normalized_result_json
        FROM audit_l2_derived_task_authority_v2 authority
        JOIN audit_logical_tasks task ON task.task_hash=authority.task_hash
        LEFT JOIN audit_task_settlements_v2 settlement
          ON settlement.task_hash=task.task_hash
        WHERE authority.generation_id=? AND authority.stage='detail'
        ORDER BY task.task_hash
        """,
        (generation_id,),
    ).fetchall()
    required_detail_count = sum(
        len(card["item_ids"]) for card in generation["exceptional_cards"]
    )
    if len(detail_rows) != required_detail_count:
        raise ExecutionError("adjudication_generation_conflict")
    if any(row["state"] not in TERMINAL_STATES for row in detail_rows):
        return {
            "state": "waiting_for_detail", "generation_id": generation_id,
            "reduce_task_hashes": [],
        }
    if not history_audit_store.validate_l2_terminal_graph(conn, plan["plan_sha"]):
        raise ExecutionError("malformed_l2_terminal_graph")
    recover_run(conn, plan["plan_sha"], cas_root=cas_root, now=now)
    if any(
        row["state"] != "settled" or row["settlement_kind"] != "equal"
        for row in detail_rows
    ):
        return {
            "state": "detail_incomplete", "generation_id": generation_id,
            "reduce_task_hashes": [],
        }
    entries = []
    for row in detail_rows:
        normalized = _json(row["normalized_result_json"])
        if (
            normalized.get("schema_version") != DETAIL_OUTPUT_SCHEMA
            or normalized.get("generation_id") != generation_id
            or normalized.get("task_hash") != row["task_hash"]
            or not isinstance(normalized.get("detail_card"), dict)
        ):
            raise ExecutionError("invalid_detail_settlement")
        entries.append(
            {
                "task_hash": row["task_hash"],
                "settlement_sha256": row["settlement_sha256"],
                "detail_card": normalized["detail_card"],
            }
        )
    specs = _reduce_task_specs(plan, generation_id, entries)
    if _stored_reduce_replay(conn, generation_id, specs):
        return {
            "state": "materialized", "generation_id": generation_id,
            "reduce_task_hashes": [spec["task_hash"] for spec in specs],
        }
    created_at = _now(now).isoformat()
    task_values = []
    binding_values = []
    input_values = []
    authority_values = []
    for spec in specs:
        task_values.append(
            (
                spec["task_hash"], plan["run_id"], "reduce",
                plan["candidate"]["candidate_id"], spec["input_id"],
            )
        )
        binding_values.append(
            (
                spec["task_hash"], plan["plan_sha"],
                plan["snapshot"]["snapshot_id"],
                plan["snapshot"]["snapshot_hash"], spec["request_sha"],
                _canonical(spec["assigned_item_ids"]), _canonical([]),
                _canonical(spec["provider_pool"]), None, 0, created_at,
            )
        )
        input_values.append(
            (
                spec["task_hash"], spec["input_id"], spec["request_sha"],
                spec["request_text"], _canonical(spec["assigned_item_ids"]),
                created_at,
            )
        )
        authority_values.append(
            (
                spec["task_hash"], generation_id, plan["plan_sha"], "reduce",
                _canonical(spec["authority"]), spec["authority_sha"], created_at,
            )
        )
    try:
        conn.execute("BEGIN IMMEDIATE")
        with history_audit_store.l2_adjudication_materialization_guard(
            conn, generation=None, tasks=task_values, bindings=binding_values,
            inputs=input_values, authorities=authority_values,
        ):
            for task, binding, task_input, authority in zip(
                task_values, binding_values, input_values, authority_values
            ):
                conn.execute(
                    """
                    INSERT INTO audit_logical_tasks(
                      task_hash, run_id, stage, staging_candidate_id, input_id,
                      state, fence, claim_token, lease_until, created_at
                    ) VALUES(?, ?, ?, ?, ?, 'planned', 0, NULL, NULL, ?)
                    """,
                    (*task, created_at),
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
                    binding,
                )
                conn.execute(
                    """
                    INSERT INTO audit_l2_task_inputs_v2(
                      task_hash, input_id, request_sha, request_text,
                      item_ids_json, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    task_input,
                )
                conn.execute(
                    """
                    INSERT INTO audit_l2_derived_task_authority_v2(
                      task_hash, generation_id, plan_sha, stage,
                      authority_json, authority_sha256, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    authority,
                )
            valid_count = conn.execute(
                """
                SELECT count(*)
                FROM audit_l2_valid_adjudication_task_authority_v2 valid
                JOIN audit_l2_derived_task_authority_v2 authority
                  ON authority.task_hash=valid.task_hash
                WHERE authority.generation_id=? AND authority.stage='reduce'
                """,
                (generation_id,),
            ).fetchone()[0]
            if valid_count != len(specs):
                raise ExecutionError("invalid_derived_task_authority")
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {
        "state": "materialized", "generation_id": generation_id,
        "reduce_task_hashes": [spec["task_hash"] for spec in specs],
    }


def claim_task(conn, task_key, worker_id, lease_seconds, expected_fence, *, now=None):
    """Acquire or renew one fenced logical task claim."""
    if not isinstance(worker_id, str) or not worker_id:
        raise ExecutionError("invalid_worker")
    if type(lease_seconds) is not int or lease_seconds < 0:
        raise ExecutionError("invalid_lease")
    current = _now(now)
    row = load_task(conn, task_key)
    if row["fence"] != expected_fence:
        raise history_audit_store.StaleFence("logical task fence is stale")
    if row["state"] in TERMINAL_STATES:
        raise ExecutionError("terminal_task")
    if row["state"] == "settling":
        raise ExecutionError("task_is_settling")
    if row["state"] == "claimed" and row["claim_token"] != worker_id:
        if _now(row["lease_until"]) > current:
            raise history_audit_store.StaleFence("logical task lease is live")
    lease_until = (current + datetime.timedelta(seconds=lease_seconds)).isoformat()
    try:
        conn.execute("BEGIN IMMEDIATE")
        history_audit_store.compare_and_set_logical_task(
            conn,
            task_key,
            expected_state=row["state"],
            expected_fence=expected_fence,
            new_state="claimed",
            new_fence=expected_fence + 1,
            claim_token=worker_id,
            lease_until=lease_until,
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {"task_hash": task_key, "fence": expected_fence + 1, "claim_token": worker_id, "lease_until": lease_until}


def _budget_event(conn, task, event_id, event_type, counters):
    intent = task["durable_plan"]["intent"]
    round_id = task["plan_sha"]
    material = {
        "run_id": task["run_id"],
        "intent": intent,
        "round_id": round_id,
        "event_id": event_id,
        "task_hash": task["task_hash"],
        "event_type": event_type,
        "counters": counters,
    }
    conn.execute(
        """
        INSERT OR IGNORE INTO audit_budget_events(
          event_id, run_id, intent, round_id, event_type,
          counters_json, event_sha256, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id, task["run_id"], intent, round_id, event_type,
            _canonical(counters), _sha("history-runtime-budget-event-v1", material),
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
        ),
    )


_BUDGET_RESOURCE_FIELDS = (
    "input_tokens", "output_tokens", "cache_tokens",
    "provider_usage_units", "currency_micros",
)
_ATTEMPT_KINDS = frozenset(
    {"initial", "retry", "failover", "split", "detail", "reduce", "cancel"}
)


def _effective_budget_totals(conn, task, *, candidate_only):
    """Read durable reservations, replacing them only with verified actual usage."""
    sql = """
        SELECT reservation.attempt_id, reservation.reserved_json,
               settlement.usage_verified, settlement.actual_json,
               settlement.created_at AS settlement_at
        FROM audit_runtime_budget_reservations_v2 reservation
        JOIN audit_l2_plans_v2 plan ON plan.plan_sha=reservation.plan_sha
        LEFT JOIN audit_runtime_budget_settlements_v2 settlement
          ON settlement.attempt_id=reservation.attempt_id
        WHERE plan.run_id=? AND reservation.intent=?
    """
    parameters = [task["run_id"], task["durable_plan"]["intent"]]
    if candidate_only:
        sql += " AND reservation.candidate_id=?"
        parameters.append(task["staging_candidate_id"])
    totals = {
        "started_attempts": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "provider_usage_units": 0,
    }
    for row in conn.execute(sql, parameters):
        totals["started_attempts"] += 1
        if row["usage_verified"] in (None, 0) and row["actual_json"] is None:
            usage = _json(row["reserved_json"])
        elif row["usage_verified"] == 1 and row["actual_json"] is not None:
            authority = (
                history_audit_store._verified_usage_authority_for_settlement(
                    conn,
                    attempt_id=row["attempt_id"],
                    actual_json=row["actual_json"],
                    terminal_at=row["settlement_at"],
                )
            )
            if authority is None:
                raise ExecutionError("usage_authority_unavailable")
            usage = _json(authority["actual_json"])
        else:
            raise ExecutionError("usage_authority_unavailable")
        for field in _BUDGET_RESOURCE_FIELDS:
            if field in usage:
                totals[field] = totals.get(field, 0) + usage[field]
    return totals


def _derived_reservation(task, request_bytes):
    capacity = task["durable_plan"]["capacity_profile"]
    maximum = capacity.get("max_output_tokens")
    if type(maximum) is not int or maximum < 0:
        raise ExecutionError("invalid_capacity_profile")
    input_tokens = len(request_bytes)
    return {
        "input_tokens": input_tokens,
        "output_tokens": maximum,
        "provider_usage_units": input_tokens + maximum,
    }


def _assert_budget_available(conn, task, reserved):
    try:
        policy = history_audit_plan._intent_policy(
            task["durable_plan"]["budget_policy"],
            task["durable_plan"]["intent"],
        )
    except history_audit_plan.AuditPlanError as exc:
        raise ExecutionError("invalid_budget_policy") from exc
    requested = {"started_attempts": 1, **reserved}
    for scope, candidate_only in (("round", False), ("candidate", True)):
        totals = _effective_budget_totals(
            conn, task, candidate_only=candidate_only
        )
        for field, amount in requested.items():
            if field not in policy[scope] or totals.get(field, 0) + amount > policy[scope][field]:
                raise ExecutionError("attempt_budget_exceeded")


def _settle_budget(conn, attempt_id, usage, *, completed_at):
    if usage is not None and not isinstance(usage, str):
        raise ExecutionError("usage_authority_unavailable")
    try:
        history_audit_store.insert_attempt_budget_settlement(
            conn, attempt_id, usage_authority_sha256=usage,
            created_at=completed_at,
        )
    except history_audit_store.AuditMigrationError as exc:
        if str(exc) == "verified_usage_authority_mismatch":
            raise ExecutionError("verified_usage_authority_mismatch") from exc
        raise ExecutionError("conflicting_budget_settlement") from exc
    stored = conn.execute(
        """
        SELECT usage_verified, actual_json, created_at
        FROM audit_runtime_budget_settlements_v2 WHERE attempt_id=?
        """,
        (attempt_id,),
    ).fetchone()
    if usage is None:
        expected = (0, None, completed_at)
    else:
        authority = conn.execute(
            "SELECT actual_json FROM audit_verified_usage_authorities_v2 "
            "WHERE usage_authority_sha256=? AND attempt_id=?",
            (usage, attempt_id),
        ).fetchone()
        if authority is None:
            raise ExecutionError("verified_usage_authority_mismatch")
        expected = (1, authority["actual_json"], completed_at)
    if stored is None or tuple(stored) != expected:
        raise ExecutionError("conflicting_budget_settlement")
    if usage is not None:
        return _json(expected[1])
    reservation = conn.execute(
        "SELECT reserved_json FROM audit_runtime_budget_reservations_v2 "
        "WHERE attempt_id=?", (attempt_id,),
    ).fetchone()
    return _json(reservation["reserved_json"])


def _has_route_dispatch_authority(conn, plan_sha):
    return conn.execute(
        """
        SELECT 1
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
        JOIN audit_candidate_route_observation_boundaries_v2 observation
          ON observation.run_id=dispatch.run_id
         AND observation.candidate_id=dispatch.candidate_id
         AND observation.route_fact_sha256=dispatch.route_fact_sha256
        WHERE dispatch.plan_sha=?
          AND route.router_facts_json=phase.router_facts_json
          AND route.risk_slices_json=phase.risk_slices_json
          AND route.matched_rule_ids_json=phase.matched_rule_ids_json
          AND route.route=phase.route
          AND route.call_l1_model=phase.call_l1_model
          AND route.dispatch_allowed=phase.dispatch_allowed
          AND route.rule_table_sha256=phase.rule_table_sha256
          AND route.risk_policy_version=phase.risk_policy_version
          AND observation.observation_scope='host_issued_shadow'
          AND observation.production_authority=0
          AND NOT EXISTS (
            SELECT 1 FROM audit_legacy_candidate_route_authorities_v2 legacy
            WHERE legacy.route_fact_sha256=route.fact_sha256
               OR (legacy.run_id=route.run_id
                   AND legacy.candidate_id=route.candidate_id)
          )
        """,
        (plan_sha,),
    ).fetchone() is not None


def record_attempt(
    conn, task_key, capability, usage_reservation, *, cas_root, request_bytes,
    ordinal=None, now=None,
):
    """Append a started attempt after its request CAS descriptor is durable."""
    task = load_task(conn, task_key)
    if not _has_route_dispatch_authority(conn, task["plan_sha"]):
        raise ExecutionError("missing_route_dispatch_authority")
    if task["state"] != "claimed":
        raise ExecutionError("task_not_claimed")
    if not isinstance(capability, dict) or not isinstance(capability.get("provider"), str):
        raise ExecutionError("capability_authority_mismatch")
    provider = capability["provider"]
    if provider not in task["provider_pool"]:
        raise ExecutionError("provider_outside_pool")
    bound_capability = task["durable_plan"]["provider_capabilities"].get(provider)
    if (
        bound_capability is None
        or history_contract_v2.canonical_bytes(capability)
        != history_contract_v2.canonical_bytes(bound_capability)
    ):
        raise ExecutionError("capability_authority_mismatch")
    if not isinstance(request_bytes, bytes):
        raise ExecutionError("invalid_attempt_request")
    if not isinstance(usage_reservation, dict):
        raise ExecutionError("invalid_usage_reservation")
    count = conn.execute(
        "SELECT count(*) FROM audit_task_attempts WHERE task_hash=?", (task_key,)
    ).fetchone()[0]
    ordinal = count if ordinal is None else ordinal
    if type(ordinal) is not int or ordinal != count or ordinal >= MAX_ATTEMPTS:
        raise ExecutionError("attempt_limit")
    if count == 0:
        if task["stage"] in {"detail", "reduce"}:
            attempt_kind = task["stage"]
        elif task["parent_task_hash"] is not None:
            attempt_kind = "split"
        else:
            attempt_kind = "initial"
    else:
        prior = conn.execute(
            """
            SELECT prior.provenance_json,
                   COALESCE(completion.outcome, cost.outcome) AS outcome
            FROM audit_task_attempts prior
            LEFT JOIN audit_attempt_completions_v2 completion USING(attempt_id)
            LEFT JOIN audit_attempt_cost_settlements_v2 cost USING(attempt_id)
            WHERE prior.task_hash=? AND prior.ordinal=?
            """,
            (task_key, count - 1),
        ).fetchone()
        if prior is None or prior["outcome"] is None:
            raise ExecutionError("prior_attempt_not_terminal")
        prior_provider = _json(prior["provenance_json"])["provider"]
        infrastructure_failure = prior["outcome"] in {"timeout", "429", "5xx"}
        retryable_failure = prior["outcome"] in {"syntax", "schema", "cancelled"}
        if not infrastructure_failure and not retryable_failure:
            raise ExecutionError("prior_attempt_not_retryable")
        attempt_kind = "failover" if infrastructure_failure else "retry"
        expected_transition_provider = (
            task["provider_pool"][min(ordinal, len(task["provider_pool"]) - 1)]
            if infrastructure_failure else prior_provider
        )
        if provider != expected_transition_provider:
            raise ExecutionError("capability_authority_mismatch")
    expected_provider = (
        task["provider_pool"][min(ordinal, len(task["provider_pool"]) - 1)]
        if attempt_kind == "failover"
        else task["provider_pool"][0]
    )
    if provider != expected_provider:
        raise ExecutionError("capability_authority_mismatch")
    request_sha = hashlib.sha256(request_bytes).hexdigest()
    if (
        request_sha != task["durable_request_sha"]
        or request_bytes.decode("utf-8", errors="strict") != task["durable_request_text"]
    ):
        raise ExecutionError("attempt_request_hash_mismatch")
    reserved = _derived_reservation(task, request_bytes)
    _assert_budget_available(conn, task, reserved)
    provenance = {
        **copy.deepcopy(bound_capability),
        "attempt_kind": attempt_kind,
        "ordinal": ordinal,
        "claim_token": task["claim_token"],
        "claim_fence": task["fence"],
    }
    attempt_id = history_contract_v2.attempt_id(task_key, ordinal, provenance)
    request = history_cas.put_object(
        conn,
        cas_root,
        request_bytes,
        "attempt-transient-7d",
        expires_at=_attempt_expiry(task),
    )
    if request["object_id"] != task["shard_input_sha"]:
        raise ExecutionError("attempt_request_hash_mismatch")
    try:
        conn.execute("BEGIN IMMEDIATE")
        _assert_budget_available(conn, task, reserved)
        created_at = _now(now).isoformat()
        conn.execute(
            """
            INSERT INTO audit_runtime_budget_reservations_v2(
              attempt_id, task_hash, plan_sha, candidate_id, intent,
              attempt_kind, reserved_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id, task_key, task["plan_sha"], task["staging_candidate_id"],
                task["durable_plan"]["intent"], attempt_kind,
                _canonical(reserved), created_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO audit_task_attempts(
              attempt_id, task_hash, ordinal, provenance_json,
              request_cas_object_id, output_cas_object_id, state, created_at
            ) VALUES(?, ?, ?, ?, ?, NULL, 'started', ?)
            """,
            (
                attempt_id, task_key, ordinal, _canonical(provenance),
                request["object_id"], created_at,
            ),
        )
        history_audit_store.record_attempt_launch_cost_fact(
            conn, attempt_id, created_at=created_at
        )
        _budget_event(conn, task, attempt_id + ":reserved", "reserved", reserved)
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {"attempt_id": attempt_id, "ordinal": ordinal, "request_cas_object_id": request["object_id"], "provenance": provenance}


def _decode_map_output(raw_output):
    if isinstance(raw_output, bytes):
        try:
            raw_output = raw_output.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MapValidationError("syntax") from exc
    if isinstance(raw_output, str):
        def closed_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise MapValidationError("syntax")
                result[key] = value
            return result
        try:
            raw_output = json.loads(raw_output, object_pairs_hook=closed_pairs)
        except (ValueError, TypeError) as exc:
            if isinstance(exc, MapValidationError):
                raise
            raise MapValidationError("syntax") from exc
    if not isinstance(raw_output, dict):
        raise MapValidationError("schema")
    return copy.deepcopy(raw_output)


def validate_map_output(task, raw_output, snapshot):
    """Require exact item IDs, relation schema, frozen anchors, and no truncation."""
    output = _decode_map_output(raw_output)
    if output.get("overflow") is True:
        raise MapValidationError("overflow")
    if set(output) != {"schema_version", "snapshot_id", "snapshot_hash", "truncated", "items"}:
        raise MapValidationError("schema")
    if output["schema_version"] != MAP_SCHEMA or type(output["truncated"]) is not bool:
        raise MapValidationError("schema")
    if output["truncated"]:
        raise MapValidationError("truncated")
    durable_records = task.get("durable_snapshot_records")
    if durable_records is not None:
        durable_plan_snapshot = task["durable_plan"]["snapshot"]
        durable_snapshot = copy.deepcopy(durable_plan_snapshot)
        durable_snapshot.pop("records_sha")
        durable_snapshot["records"] = copy.deepcopy(durable_records)
        if history_contract_v2.canonical_bytes(snapshot) != history_contract_v2.canonical_bytes(durable_snapshot):
            raise MapValidationError("snapshot_authority_mismatch")
        snapshot = durable_snapshot
    elif task.get("frozen_records") is not None:
        supplied = {
            item["item_id"]: item for item in snapshot.get("records", [])
        }
        if any(
            supplied.get(item["item_id"]) != item for item in task["frozen_records"]
        ):
            raise MapValidationError("snapshot_authority_mismatch")
        durable_records = task["frozen_records"]
    else:
        durable_records = snapshot.get("records", [])
    if output["snapshot_id"] != snapshot.get("snapshot_id") or output["snapshot_hash"] != snapshot.get("snapshot_hash"):
        raise MapValidationError("stale_snapshot")
    if task.get("snapshot_id") is not None and (
        task["snapshot_id"] != output["snapshot_id"]
        or task["snapshot_hash"] != output["snapshot_hash"]
    ):
        raise MapValidationError("stale_snapshot")
    assigned = task.get("assigned_item_ids")
    if not isinstance(assigned, list) or not assigned:
        raise MapValidationError("schema")
    if not isinstance(output["items"], list):
        raise MapValidationError("schema")
    ids = [item.get("item_id") if isinstance(item, dict) else None for item in output["items"]]
    if sorted(ids) != sorted(assigned) or len(set(ids)) != len(ids):
        raise MapValidationError("item_set_mismatch")
    records = {item["item_id"]: item for item in durable_records}
    normalized = []
    for item in output["items"]:
        if set(item) != {"item_id", "semantic_relation", "lineage_relation", "anchor"}:
            raise MapValidationError("schema")
        if item["semantic_relation"] not in SEMANTIC_RELATIONS or item["lineage_relation"] not in LINEAGE_RELATIONS:
            raise MapValidationError("schema")
        anchor = item["anchor"]
        if not isinstance(anchor, dict) or set(anchor) != {"asset_id", "artifact_sha", "start", "end", "quote"}:
            raise MapValidationError("invalid_anchor")
        source = records.get(item["item_id"])
        if source is None:
            raise MapValidationError("stale_snapshot")
        start, end = anchor["start"], anchor["end"]
        if (
            anchor["asset_id"] != item["item_id"]
            or anchor["artifact_sha"] != source["artifact_sha"]
            or type(start) is not int
            or type(end) is not int
            or start < 0
            or end <= start
            or end > len(source["content"])
            or anchor["quote"] != source["content"][start:end]
        ):
            raise MapValidationError("invalid_anchor")
        normalized.append(
            {
                "item_id": item["item_id"],
                "lineage_id": source["lineage_id"],
                "semantic_relation": item["semantic_relation"],
                "lineage_relation": item["lineage_relation"],
                "anchor": copy.deepcopy(anchor),
            }
        )
    normalized.sort(key=lambda item: item["item_id"])
    return {
        "schema_version": MAP_SCHEMA,
        "snapshot_id": output["snapshot_id"],
        "snapshot_hash": output["snapshot_hash"],
        "truncated": False,
        "items": normalized,
    }


def validate_detail_output(task, raw_output, snapshot=None):
    """Validate one exceptional lineage against only its frozen full records."""
    del snapshot
    output = _decode_map_output(raw_output)
    required = {
        "schema_version", "generation_id", "snapshot_id", "snapshot_hash",
        "task_hash", "truncated", "detail_card",
    }
    if output.get("overflow") is True:
        raise MapValidationError("overflow")
    if set(output) != required or output.get("schema_version") != DETAIL_OUTPUT_SCHEMA:
        raise MapValidationError("schema")
    if type(output["truncated"]) is not bool:
        raise MapValidationError("schema")
    if output["truncated"]:
        raise MapValidationError("truncated")
    if task.get("stage") != "detail":
        raise MapValidationError("schema")
    try:
        request = _json(task["durable_request_text"])
    except (TypeError, ValueError, KeyError) as exc:
        raise MapValidationError("schema") from exc
    if (
        request.get("schema_version") != DETAIL_REQUEST_SCHEMA
        or output["generation_id"] != request.get("generation_id")
        or output["snapshot_id"] != task.get("snapshot_id")
        or output["snapshot_hash"] != task.get("snapshot_hash")
        or output["task_hash"] != task.get("task_hash")
    ):
        raise MapValidationError("stale_snapshot")
    card = output["detail_card"]
    if not isinstance(card, dict) or set(card) != {
        "lineage_id", "semantic_relation", "item_ids", "evidence",
    }:
        raise MapValidationError("schema")
    source_card = request.get("exceptional_card")
    assigned = task.get("assigned_item_ids")
    severity = {
        "uncertain": 2,
        "substantive_overlap": 3,
        "blocking_duplicate": 4,
    }
    if (
        not isinstance(source_card, dict)
        or card["lineage_id"] != source_card.get("lineage_id")
        or card["semantic_relation"] not in severity
        or source_card.get("semantic_relation") not in severity
        or severity[card["semantic_relation"]]
        < severity[source_card["semantic_relation"]]
        or card["item_ids"] != assigned
        or card["item_ids"] != sorted(set(card["item_ids"]))
        or not isinstance(card["evidence"], list)
        or not card["evidence"]
    ):
        raise MapValidationError("schema")
    records = {
        item["item_id"]: item for item in task.get("frozen_records", [])
    }
    normalized_anchors = []
    for anchor in card["evidence"]:
        if not isinstance(anchor, dict) or set(anchor) != {
            "asset_id", "artifact_sha", "start", "end", "quote",
        }:
            raise MapValidationError("invalid_anchor")
        source = records.get(anchor["asset_id"])
        start, end = anchor["start"], anchor["end"]
        if (
            source is None
            or anchor["asset_id"] not in assigned
            or anchor["artifact_sha"] != source["artifact_sha"]
            or type(start) is not int
            or type(end) is not int
            or start < 0
            or end <= start
            or end > len(source["content"])
            or anchor["quote"] != source["content"][start:end]
        ):
            raise MapValidationError("invalid_anchor")
        normalized_anchors.append(copy.deepcopy(anchor))
    if sorted({anchor["asset_id"] for anchor in normalized_anchors}) != assigned:
        raise MapValidationError("invalid_anchor")
    unique = {_canonical(anchor): anchor for anchor in normalized_anchors}
    if len(unique) != len(normalized_anchors):
        raise MapValidationError("invalid_anchor")
    normalized_card = {
        "lineage_id": card["lineage_id"],
        "semantic_relation": card["semantic_relation"],
        "item_ids": copy.deepcopy(assigned),
        "evidence": [unique[key] for key in sorted(unique)],
    }
    return {
        "schema_version": DETAIL_OUTPUT_SCHEMA,
        "generation_id": output["generation_id"],
        "snapshot_id": output["snapshot_id"],
        "snapshot_hash": output["snapshot_hash"],
        "task_hash": output["task_hash"],
        "truncated": False,
        "detail_card": normalized_card,
    }


def validate_reduce_output(task, raw_output, snapshot=None):
    """Validate a reducer as an exact bounded summary of settled detail cards."""
    del snapshot
    output = _decode_map_output(raw_output)
    required = {
        "schema_version", "generation_id", "snapshot_id", "snapshot_hash",
        "task_hash", "truncated", "cards",
    }
    if output.get("overflow") is True:
        raise MapValidationError("overflow")
    if set(output) != required or output.get("schema_version") != REDUCE_OUTPUT_SCHEMA:
        raise MapValidationError("schema")
    if type(output["truncated"]) is not bool:
        raise MapValidationError("schema")
    if output["truncated"]:
        raise MapValidationError("truncated")
    if task.get("stage") != "reduce":
        raise MapValidationError("schema")
    try:
        request = _json(task["durable_request_text"])
    except (TypeError, ValueError, KeyError) as exc:
        raise MapValidationError("schema") from exc
    if (
        request.get("schema_version") != REDUCE_REQUEST_SCHEMA
        or output["generation_id"] != request.get("generation_id")
        or output["snapshot_id"] != task.get("snapshot_id")
        or output["snapshot_hash"] != task.get("snapshot_hash")
        or output["task_hash"] != task.get("task_hash")
        or not isinstance(output["cards"], list)
        or _canonical(output["cards"]) != _canonical(request.get("detail_cards"))
    ):
        raise MapValidationError("schema")
    return {
        "schema_version": REDUCE_OUTPUT_SCHEMA,
        "generation_id": output["generation_id"],
        "snapshot_id": output["snapshot_id"],
        "snapshot_hash": output["snapshot_hash"],
        "task_hash": output["task_hash"],
        "truncated": False,
        "cards": copy.deepcopy(output["cards"]),
    }


def _insert_completion(
    conn, task, attempt_id, output_id, outcome, normalized, usage, *, now=None
):
    if usage is not None and not isinstance(usage, str):
        raise ExecutionError("usage_authority_unavailable")
    encoded = _canonical(normalized) if normalized is not None else None
    completed_at = _now(now).isoformat()
    try:
        history_audit_store.insert_attempt_completion(
            conn, attempt_id, output_id, outcome, encoded,
            completed_at=completed_at,
        )
    except history_audit_store.AuditMigrationError as exc:
        raise ExecutionError("conflicting_attempt_completion") from exc
    effective = _settle_budget(
        conn, attempt_id, usage, completed_at=completed_at
    )
    try:
        history_audit_store.record_attempt_terminal_cost_fact(
            conn, attempt_id, completed_at=completed_at,
            usage_authority_sha256=usage,
        )
    except history_audit_store.AuditMigrationError as exc:
        if str(exc) == "verified_usage_authority_mismatch":
            raise ExecutionError("verified_usage_authority_mismatch") from exc
        raise ExecutionError("conflicting_attempt_cost") from exc
    _budget_event(conn, task, attempt_id + ":settled", "settled", effective)


def complete_attempt(
    conn, cas_root, task_key, attempt_id, raw_output, snapshot, *, usage=None,
    now=None,
):
    """CAS-write output before validation, then append one completion fact."""
    if usage is not None and not isinstance(usage, str):
        raise ExecutionError("usage_authority_unavailable")
    task = load_task(conn, task_key)
    attempt = conn.execute(
        "SELECT * FROM audit_task_attempts WHERE attempt_id=? AND task_hash=?",
        (attempt_id, task_key),
    ).fetchone()
    if attempt is None:
        raise ExecutionError("unknown_attempt")
    raw = (
        history_contract_v2.canonical_bytes(raw_output)
        if isinstance(raw_output, (dict, list))
        else (raw_output if isinstance(raw_output, bytes) else str(raw_output).encode("utf-8"))
    )
    output = history_cas.put_object(
        conn,
        cas_root,
        raw,
        "attempt-transient-7d",
        expires_at=_attempt_expiry(task),
    )
    current_task = load_task(conn, task_key)
    provenance = _json(attempt["provenance_json"])
    if (
        current_task["state"] != "claimed"
        or current_task["claim_token"] != provenance.get("claim_token")
        or current_task["fence"] != provenance.get("claim_fence")
    ):
        raise history_audit_store.StaleFence("attempt completion claim is stale")
    normalized = None
    outcome = "valid"
    error = None
    try:
        validator = {
            "map": validate_map_output,
            "detail": validate_detail_output,
            "reduce": validate_reduce_output,
        }.get(task["stage"])
        if validator is None:
            raise MapValidationError("schema")
        normalized = validator(task, raw_output, snapshot)
    except MapValidationError as exc:
        outcome = {
            "item_set_mismatch": "item_set",
            "stale_snapshot": "schema",
        }.get(exc.code, exc.code)
        error = exc
    completed_at = _now(now).isoformat()
    if usage is not None:
        try:
            history_audit_store._verified_usage_authority_for_terminal(
                conn, usage, attempt_id=attempt_id,
                output_cas_object_id=output["object_id"],
                terminal_outcome=outcome, terminal_at=completed_at,
            )
        except history_audit_store.AuditMigrationError as exc:
            raise ExecutionError(
                "verified_usage_authority_mismatch"
            ) from exc
    try:
        conn.execute("BEGIN IMMEDIATE")
        _insert_completion(
            conn, task, attempt_id, output["object_id"], outcome, normalized,
            usage, now=completed_at,
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    if error is not None:
        error.output_cas_object_id = output["object_id"]
        raise error
    return {"attempt_id": attempt_id, "output_cas_object_id": output["object_id"], "normalized": normalized}


def _failed_completion(
    conn, cas_root, task, attempt_id, outcome, raw, usage, *, now=None
):
    if usage is not None and not isinstance(usage, str):
        raise ExecutionError("usage_authority_unavailable")
    payload = raw if isinstance(raw, bytes) else str(raw).encode("utf-8")
    output = history_cas.put_object(
        conn,
        cas_root,
        payload,
        "attempt-transient-7d",
        expires_at=_attempt_expiry(task),
    )
    completed_at = _now(now).isoformat()
    if usage is not None:
        try:
            history_audit_store._verified_usage_authority_for_terminal(
                conn, usage, attempt_id=attempt_id,
                output_cas_object_id=output["object_id"],
                terminal_outcome=outcome, terminal_at=completed_at,
            )
        except history_audit_store.AuditMigrationError as exc:
            raise ExecutionError(
                "verified_usage_authority_mismatch"
            ) from exc
    try:
        conn.execute("BEGIN IMMEDIATE")
        _insert_completion(
            conn, task, attempt_id, output["object_id"], outcome, None, usage,
            now=completed_at,
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return output["object_id"]


def cancel_attempt(
    conn, attempt_id, *, billing_state="unknown", usage=None,
    error_class="cancelled", now=None,
):
    """Append an exact-once cancellation retaining reservation when usage is unknown."""
    row = conn.execute(
        "SELECT 1 FROM audit_task_attempts WHERE attempt_id=?", (attempt_id,)
    ).fetchone()
    if row is None:
        raise ExecutionError("unknown_attempt")
    if usage is not None and not isinstance(usage, str):
        raise ExecutionError("usage_authority_unavailable")
    completed_at = _now(now).isoformat()
    if usage is None:
        if billing_state != "unknown":
            raise ExecutionError("billing_authority_unavailable")
    else:
        try:
            authority = (
                history_audit_store._verified_usage_authority_for_terminal(
                    conn, usage, attempt_id=attempt_id,
                    output_cas_object_id=None,
                    terminal_outcome="cancelled", terminal_at=completed_at,
                )
            )
        except history_audit_store.AuditMigrationError as exc:
            raise ExecutionError(
                "verified_usage_authority_mismatch"
            ) from exc
        if (
            authority["billing_state"] != billing_state
            or error_class != "cancelled"
        ):
            raise ExecutionError("verified_usage_authority_mismatch")
    try:
        conn.execute("BEGIN IMMEDIATE")
        if usage is None:
            history_audit_store.record_attempt_terminal_cost_fact(
                conn, attempt_id, cancellation=True, error_class=error_class,
                completed_at=completed_at,
            )
            _settle_budget(conn, attempt_id, None, completed_at=completed_at)
        else:
            _settle_budget(
                conn, attempt_id, usage, completed_at=completed_at
            )
            history_audit_store.record_attempt_terminal_cost_fact(
                conn, attempt_id, cancellation=True, error_class=error_class,
                completed_at=completed_at,
                usage_authority_sha256=usage,
            )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {"attempt_id": attempt_id, "outcome": "cancelled"}


def settlement_decision(valid_attempts):
    """Canonicalize all valid outputs independent of completion arrival order."""
    if not isinstance(valid_attempts, list) or not valid_attempts:
        raise ExecutionError("no_valid_attempts")
    by_id = {}
    for attempt in valid_attempts:
        if not isinstance(attempt, dict) or not {"attempt_id", "normalized"}.issubset(attempt):
            raise ExecutionError("invalid_valid_attempt")
        _require_sha(attempt["attempt_id"], "attempt_id")
        if attempt["attempt_id"] in by_id:
            raise ExecutionError("duplicate_valid_attempt")
        by_id[attempt["attempt_id"]] = copy.deepcopy(attempt)
    ordered_ids = sorted(by_id)
    encodings = {_canonical(by_id[attempt_id]["normalized"]) for attempt_id in ordered_ids}
    equal = len(encodings) == 1
    return {
        "settlement_kind": "equal" if equal else "conflict",
        "normalized_result": copy.deepcopy(by_id[ordered_ids[0]]["normalized"]) if equal else None,
        "valid_attempt_ids": ordered_ids,
    }


def settle_task(conn, task_key, valid_attempts, *, cas_root, now=None):
    """Commit one equal result or deterministic conflict referencing all valid attempts."""
    task = load_task(conn, task_key)
    rows = conn.execute(
        """
        SELECT completion.*, attempt.request_cas_object_id
        FROM audit_attempt_completions_v2 completion
        JOIN audit_task_attempts attempt ON attempt.attempt_id=completion.attempt_id
        WHERE attempt.task_hash=? AND completion.outcome='valid'
        ORDER BY completion.attempt_id
        """,
        (task_key,),
    ).fetchall()
    durable = {}
    output_ids = {}
    for row in rows:
        request_state = history_cas.verify_object(conn, cas_root, row["request_cas_object_id"])
        output_state = history_cas.verify_object(conn, cas_root, row["output_cas_object_id"])
        if request_state["integrity_state"] == "expired" or output_state["integrity_state"] == "expired":
            raise history_cas.CASIntegrityError("terminal settlement payload is expired")
        durable[row["attempt_id"]] = _json(row["normalized_result_json"])
        output_ids[row["attempt_id"]] = row["output_cas_object_id"]
    supplied = {attempt["attempt_id"]: attempt["normalized"] for attempt in valid_attempts}
    if supplied != durable:
        raise ExecutionError("valid_attempt_set_mismatch")
    decision = settlement_decision(valid_attempts)
    decision["valid_output_cas_ids"] = [output_ids[attempt_id] for attempt_id in decision["valid_attempt_ids"]]
    material = {"task_hash": task_key, **decision}
    settlement_sha = _sha("history-task-settlement-v2", material)
    existing = conn.execute(
        "SELECT * FROM audit_task_settlements_v2 WHERE task_hash=?", (task_key,)
    ).fetchone()
    if existing is not None:
        if existing["settlement_sha256"] != settlement_sha:
            raise ExecutionError("conflicting_terminal_settlement")
        return {**decision, "settlement_sha256": settlement_sha}
    if task["state"] != "claimed":
        raise ExecutionError("task_not_claimed")
    if _now(task["lease_until"]) <= _now(now):
        raise history_audit_store.StaleFence("logical task settlement lease expired")
    try:
        conn.execute("BEGIN IMMEDIATE")
        history_audit_store.compare_and_set_logical_task(
            conn, task_key,
            expected_state="claimed", expected_fence=task["fence"],
            new_state="settling", new_fence=task["fence"] + 1,
            claim_token=task["claim_token"], lease_until=task["lease_until"],
        )
        conn.execute(
            """
            INSERT INTO audit_task_settlements_v2(
              task_hash, settlement_sha256, settlement_kind,
              normalized_result_json, valid_attempt_ids_json,
              valid_output_cas_ids_json, settled_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_key, settlement_sha, decision["settlement_kind"],
                _canonical(decision["normalized_result"]) if decision["normalized_result"] is not None else None,
                _canonical(decision["valid_attempt_ids"]),
                _canonical(decision["valid_output_cas_ids"]),
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
            ),
        )
        history_audit_store.compare_and_set_logical_task(
            conn, task_key,
            expected_state="settling", expected_fence=task["fence"] + 1,
            new_state="settled", new_fence=task["fence"] + 2,
            claim_token=None, lease_until=None,
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {**decision, "settlement_sha256": settlement_sha}


def exhaust_task(
    conn, task_key, reason, *, expected_fence=None, claim_token=None, now=None
):
    task = load_task(conn, task_key)
    if task["state"] in {"settled", "superseded"}:
        raise ExecutionError("terminal_task")
    if task["state"] != "exhausted" and (
        task["state"] != "claimed"
        or expected_fence is None
        or claim_token is None
    ):
        raise ExecutionError("exhaust_requires_live_claim")
    try:
        return history_audit_store.transition_l2_exhaust_task(
            conn,
            task_key,
            reason,
            expected_fence=expected_fence,
            claim_token=claim_token,
            now=(now or datetime.datetime.now(datetime.timezone.utc).isoformat()),
        )
    except history_audit_store.StaleFence:
        raise
    except history_audit_store.AuditMigrationError as exc:
        code = (
            "missing_overflow_evidence"
            if "overflow evidence" in str(exc)
            else "invalid_exhaust_authority"
        )
        raise ExecutionError(code) from exc


def split_task(
    conn, parent_key, *, expected_fence=None, claim_token=None, now=None
):
    """Supersede an invalid parent with stable, nonempty .0/.1 children."""
    parent = load_task(conn, parent_key)
    if parent["state"] == "superseded":
        try:
            return history_audit_store.transition_l2_split_task(
                conn, parent_key, expected_fence=expected_fence,
                claim_token=claim_token,
                now=(now or datetime.datetime.now(datetime.timezone.utc).isoformat()),
            )
        except history_audit_store.AuditMigrationError as exc:
            raise ExecutionError("invalid_split_authority") from exc
    if parent["state"] == "exhausted":
        try:
            return history_audit_store.transition_l2_exhaust_task(
                conn, parent_key, "single_item_overflow",
                expected_fence=expected_fence, claim_token=claim_token,
                now=(now or datetime.datetime.now(datetime.timezone.utc).isoformat()),
            )
        except history_audit_store.AuditMigrationError as exc:
            raise ExecutionError("invalid_exhaust_authority") from exc
    if parent["state"] == "settled":
        raise ExecutionError("terminal_task")
    if (
        parent["state"] != "claimed"
        or expected_fence is None
        or claim_token is None
    ):
        raise ExecutionError("split_requires_live_claim")
    item_ids = parent["assigned_item_ids"]
    if len(item_ids) == 1:
        return exhaust_task(
            conn, parent_key, "single_item_overflow",
            expected_fence=expected_fence, claim_token=claim_token, now=now,
        )
    try:
        return history_audit_store.transition_l2_split_task(
            conn,
            parent_key,
            expected_fence=expected_fence,
            claim_token=claim_token,
            now=(now or datetime.datetime.now(datetime.timezone.utc).isoformat()),
        )
    except history_audit_store.StaleFence:
        raise
    except history_audit_store.AuditMigrationError as exc:
        raise ExecutionError("invalid_split_authority") from exc


def _valid_completions(conn, task_key):
    return [
        {"attempt_id": row["attempt_id"], "normalized": _json(row["normalized_result_json"]), "output_cas_object_id": row["output_cas_object_id"]}
        for row in conn.execute(
            """
            SELECT completion.* FROM audit_attempt_completions_v2 completion
            JOIN audit_task_attempts attempt ON attempt.attempt_id=completion.attempt_id
            WHERE attempt.task_hash=? AND completion.outcome='valid'
            ORDER BY completion.attempt_id
            """,
            (task_key,),
        )
    ]


def run_task(
    conn, cas_root, plan, task_key, provider, *, now=None, lease_seconds=60,
    fault_after_cas=False
):
    """Run at most two fake-provider attempts using only declared pool order."""
    if not callable(provider):
        raise ExecutionError("provider_must_be_in_memory_callable")
    task = load_task(conn, task_key)
    claim_task(conn, task_key, "runtime-worker", lease_seconds, task["fence"], now=now)
    task = load_task(conn, task_key)
    terminal_transition = {
        "expected_fence": task["fence"],
        "claim_token": task["claim_token"],
        "now": (now or datetime.datetime.now(datetime.timezone.utc).isoformat()),
    }

    def finish_terminal(result):
        if task["stage"] == "map":
            materialize_adjudication_tasks(conn, cas_root, plan, now=now)
        elif task["stage"] == "detail":
            materialize_reduce_tasks(conn, cas_root, plan, now=now)
        return result

    existing_valid = _valid_completions(conn, task_key)
    if existing_valid:
        result = settle_task(
            conn, task_key, existing_valid, cas_root=cas_root, now=now
        )
        return finish_terminal(result)
    request_bytes = task["durable_request_text"].encode("utf-8")
    pool = task["provider_pool"]
    provider_index = 0
    prior_failure = None
    for ordinal in range(MAX_ATTEMPTS):
        provider_name = pool[min(provider_index, len(pool) - 1)]
        attempt_kind = "initial" if ordinal == 0 else ("failover" if prior_failure in {"timeout", "429", "5xx"} else "retry")
        reservation = {
            "attempt_kind": attempt_kind,
            "input_tokens": len(request_bytes),
            "output_tokens": 0,
            "provider_usage_units": len(request_bytes),
        }
        try:
            attempt = record_attempt(
                conn, task_key,
                copy.deepcopy(
                    task["durable_plan"]["provider_capabilities"][provider_name]
                ),
                reservation, cas_root=cas_root, request_bytes=request_bytes,
            )
        except ExecutionError as exc:
            if exc.code != "attempt_budget_exceeded":
                raise
            return finish_terminal(
                exhaust_task(
                    conn, task_key, "budget_exceeded", **terminal_transition
                )
            )
        response = provider(task_key, provider_name, ordinal, request_bytes)
        if not isinstance(response, dict) or response.get("kind") not in {
            "success", "timeout", "429", "5xx", "overflow", "syntax", "schema", "provider_error"
        }:
            response = {"kind": "provider_error", "raw": "invalid fake response"}
        kind = response["kind"]
        usage = response["usage"] if "usage" in response else None
        if kind == "success":
            try:
                valid = complete_attempt(
                    conn, cas_root, task_key, attempt["attempt_id"],
                    response.get("output"), plan["snapshot"], usage=usage,
                )
            except MapValidationError as exc:
                if (
                    task["stage"] == "map"
                    and exc.code in {"item_set_mismatch", "truncated", "overflow"}
                ):
                    return finish_terminal(
                        split_task(conn, task_key, **terminal_transition)
                    )
                if exc.code in {"syntax", "schema", "stale_snapshot"} and ordinal + 1 < MAX_ATTEMPTS:
                    prior_failure = exc.code
                    continue
                return finish_terminal(
                    exhaust_task(
                        conn, task_key, exc.code, **terminal_transition
                    )
                )
            if fault_after_cas:
                raise ExecutionCrash("fault injected after durable output CAS")
            result = settle_task(
                conn, task_key, [valid], cas_root=cas_root, now=now
            )
            return finish_terminal(result)
        _failed_completion(
            conn, cas_root, task, attempt["attempt_id"], kind,
            response.get("raw", kind), usage,
        )
        if kind == "overflow":
            if task["stage"] == "map":
                return finish_terminal(
                    split_task(conn, task_key, **terminal_transition)
                )
            return finish_terminal(
                exhaust_task(
                    conn, task_key, "overflow", **terminal_transition
                )
            )
        if kind in {"timeout", "429", "5xx"}:
            provider_index += 1
            prior_failure = kind
            if provider_index < len(pool) and ordinal + 1 < MAX_ATTEMPTS:
                continue
        elif kind in {"syntax", "schema"} and ordinal + 1 < MAX_ATTEMPTS:
            prior_failure = kind
            continue
        return finish_terminal(
            exhaust_task(
                conn, task_key, "provider_exhausted", **terminal_transition
            )
        )
    return finish_terminal(
        exhaust_task(conn, task_key, "attempt_limit", **terminal_transition)
    )


def run_map_task(
    conn, cas_root, plan, task_key, provider, *, now=None, lease_seconds=60,
    fault_after_cas=False
):
    """Compatibility entry point for one map task."""
    if load_task(conn, task_key)["stage"] != "map":
        raise ExecutionError("map_task_required")
    return run_task(
        conn, cas_root, plan, task_key, provider, now=now,
        lease_seconds=lease_seconds, fault_after_cas=fault_after_cas,
    )


def sha_provider(provider):
    return hashlib.sha256(provider.encode("utf-8")).hexdigest()


def load_terminal_states(conn, plan_sha):
    rows = conn.execute(
        """
        SELECT task.task_hash, task.stage, task.state,
               binding.assigned_item_ids_json,
               settlement.settlement_kind, settlement.normalized_result_json,
               settlement.settlement_sha256,
               settlement.valid_output_cas_ids_json,
               terminal.reason
        FROM audit_task_bindings_v2 binding
        JOIN audit_logical_tasks task ON task.task_hash=binding.task_hash
        LEFT JOIN audit_task_settlements_v2 settlement ON settlement.task_hash=task.task_hash
        LEFT JOIN audit_task_terminal_facts_v2 terminal ON terminal.task_hash=task.task_hash
        WHERE binding.plan_sha=? AND task.state IN ('settled','superseded','exhausted')
        ORDER BY task.task_hash
        """,
        (plan_sha,),
    ).fetchall()
    outcome_rows = conn.execute(
        """
        SELECT attempt.task_hash, completion.outcome
        FROM audit_task_bindings_v2 binding
        JOIN audit_task_attempts attempt ON attempt.task_hash=binding.task_hash
        JOIN audit_attempt_completions_v2 completion
          ON completion.attempt_id=attempt.attempt_id
        WHERE binding.plan_sha=?
        ORDER BY attempt.task_hash, attempt.ordinal
        """,
        (plan_sha,),
    ).fetchall()
    outcomes_by_task = {}
    for outcome in outcome_rows:
        outcomes_by_task.setdefault(outcome["task_hash"], []).append(
            outcome["outcome"]
        )
    return [
        {
            "task_hash": row["task_hash"],
            "stage": row["stage"],
            "state": row["state"],
            "item_ids": _json(row["assigned_item_ids_json"]),
            "settlement_kind": row["settlement_kind"],
            "settlement_sha256": row["settlement_sha256"],
            "valid_output_cas_ids": (
                _json(row["valid_output_cas_ids_json"])
                if row["valid_output_cas_ids_json"] else []
            ),
            "normalized_result": _json(row["normalized_result_json"]) if row["normalized_result_json"] else None,
            "reason": row["reason"],
            "attempt_outcomes": outcomes_by_task.get(row["task_hash"], []),
        }
        for row in rows
    ]


def load_adjudication_state(conn, plan_sha):
    """Load the required derived-task gate from immutable generation authority."""
    row = conn.execute(
        """
        SELECT generation.generation_id, generation.material_json,
               audit_l2_adjudication_generation_valid(
                 plan.plan_json, generation.material_json,
                 generation.generation_id
               ) AS identity_valid
        FROM audit_l2_adjudication_generations_v2 generation
        JOIN audit_l2_plans_v2 plan ON plan.plan_sha=generation.plan_sha
        WHERE generation.plan_sha=?
        """,
        (plan_sha,),
    ).fetchone()
    empty = {
        "generation_present": False,
        "generation_id": None,
        "required_detail_count": 0,
        "required_reduce_count": 0,
        "detail_complete": False,
        "reduce_complete": False,
        "detail_cards": [],
        "derived_conflict": False,
        "exhausted_reason": None,
    }
    if row is None:
        return empty
    if row["identity_valid"] != 1:
        raise ExecutionError("invalid_adjudication_generation")
    generation = _json(row["material_json"])
    required_detail = sum(
        len(card["item_ids"]) for card in generation["exceptional_cards"]
    )
    item_cap = conn.execute(
        """
        SELECT json_extract(plan_json, '$.capacity_profile.item_cap')
        FROM audit_l2_plans_v2 WHERE plan_sha=?
        """,
        (plan_sha,),
    ).fetchone()[0]
    if type(item_cap) is not int or item_cap <= 0:
        raise ExecutionError("invalid_capacity_profile")
    required_reduce = (
        (required_detail + item_cap - 1) // item_cap if required_detail else 0
    )
    rows = conn.execute(
        """
        SELECT authority.stage, task.task_hash, task.state,
               settlement.settlement_kind, settlement.normalized_result_json,
               terminal.reason,
               CASE WHEN valid.task_hash IS NULL THEN 0 ELSE 1 END AS valid
        FROM audit_l2_derived_task_authority_v2 authority
        JOIN audit_logical_tasks task ON task.task_hash=authority.task_hash
        LEFT JOIN audit_l2_valid_adjudication_task_authority_v2 valid
          ON valid.task_hash=authority.task_hash
        LEFT JOIN audit_task_settlements_v2 settlement
          ON settlement.task_hash=task.task_hash
        LEFT JOIN audit_task_terminal_facts_v2 terminal
          ON terminal.task_hash=task.task_hash
        WHERE authority.generation_id=?
        ORDER BY authority.stage, task.task_hash
        """,
        (row["generation_id"],),
    ).fetchall()
    if any(value["valid"] != 1 for value in rows):
        raise ExecutionError("invalid_derived_task_authority")
    by_stage = {
        stage: [value for value in rows if value["stage"] == stage]
        for stage in ("detail", "reduce")
    }
    conflicts = any(
        value["settlement_kind"] == "conflict" for value in rows
    )
    reasons = sorted(
        value["reason"] for value in rows
        if value["state"] == "exhausted" and value["reason"]
    )
    detail_cards = []
    detail_complete = len(by_stage["detail"]) == required_detail
    if detail_complete:
        for value in by_stage["detail"]:
            if value["state"] != "settled" or value["settlement_kind"] != "equal":
                detail_complete = False
                break
            normalized = _json(value["normalized_result_json"])
            if (
                normalized.get("schema_version") != DETAIL_OUTPUT_SCHEMA
                or normalized.get("generation_id") != row["generation_id"]
                or normalized.get("task_hash") != value["task_hash"]
                or not isinstance(normalized.get("detail_card"), dict)
            ):
                raise ExecutionError("invalid_detail_settlement")
            detail_cards.append(normalized["detail_card"])
    reduce_complete = len(by_stage["reduce"]) == required_reduce
    if reduce_complete:
        for value in by_stage["reduce"]:
            if value["state"] != "settled" or value["settlement_kind"] != "equal":
                reduce_complete = False
                break
            normalized = _json(value["normalized_result_json"])
            if (
                normalized.get("schema_version") != REDUCE_OUTPUT_SCHEMA
                or normalized.get("generation_id") != row["generation_id"]
                or normalized.get("task_hash") != value["task_hash"]
                or not isinstance(normalized.get("cards"), list)
            ):
                raise ExecutionError("invalid_reduce_settlement")
    return {
        "generation_present": True,
        "generation_id": row["generation_id"],
        "required_detail_count": required_detail,
        "required_reduce_count": required_reduce,
        "detail_complete": detail_complete,
        "reduce_complete": reduce_complete,
        "detail_cards": sorted(
            copy.deepcopy(detail_cards),
            key=lambda value: (value["lineage_id"], value["item_ids"]),
        ),
        "derived_conflict": conflicts,
        "exhausted_reason": reasons[0] if reasons else None,
    }


def build_coverage_receipt(
    plan, settlements, semantic_qualification, *, conn=None
):
    """Derive coverage, exceptional-card reduction, gates, status, and reason."""
    adjudication_state = (
        load_adjudication_state(conn, plan["plan_sha"])
        if conn is not None else None
    )
    return history_audit.summarize_l2_coverage(
        plan, settlements, semantic_qualification,
        adjudication_state=adjudication_state,
    )


def recover_run(conn, plan_sha, *, cas_root, now=None):
    """Reclaim only expired unsettled claims and verify terminal settlement payloads."""
    current = _now(now)
    stranded = conn.execute(
        """
        SELECT 1
        FROM audit_task_bindings_v2 binding
        LEFT JOIN audit_l2_plans_v2 plan ON plan.plan_sha=binding.plan_sha
        LEFT JOIN audit_l2_task_inputs_v2 input ON input.task_hash=binding.task_hash
        LEFT JOIN audit_l2_snapshot_records_v2 records
          ON records.snapshot_id=binding.snapshot_id
        WHERE binding.plan_sha=?
          AND (plan.plan_sha IS NULL OR input.task_hash IS NULL OR records.snapshot_id IS NULL)
        LIMIT 1
        """,
        (plan_sha,),
    ).fetchone()
    if stranded is not None:
        raise ExecutionError("stranded_l2_authority")
    if not history_audit_store.validate_l2_terminal_graph(conn, plan_sha):
        raise ExecutionError("malformed_l2_terminal_graph")
    rows = conn.execute(
        """
        SELECT task.* FROM audit_logical_tasks task
        JOIN audit_task_bindings_v2 binding ON binding.task_hash=task.task_hash
        WHERE binding.plan_sha=? ORDER BY task.task_hash
        """,
        (plan_sha,),
    ).fetchall()
    recovered = []
    for raw in rows:
        task = dict(raw)
        if task["state"] == "settled":
            settlement = conn.execute(
                "SELECT * FROM audit_task_settlements_v2 WHERE task_hash=?",
                (task["task_hash"],),
            ).fetchone()
            if settlement is None:
                raise ExecutionError("settled_task_missing_settlement")
            attempt_ids = _json(settlement["valid_attempt_ids_json"])
            output_ids = _json(settlement["valid_output_cas_ids_json"])
            valid_rows = conn.execute(
                """
                SELECT completion.attempt_id, completion.output_cas_object_id,
                       completion.normalized_result_json
                FROM audit_attempt_completions_v2 completion
                JOIN audit_task_attempts attempt
                  ON attempt.attempt_id=completion.attempt_id
                WHERE attempt.task_hash=? AND completion.outcome='valid'
                ORDER BY completion.attempt_id
                """,
                (task["task_hash"],),
            ).fetchall()
            if attempt_ids != [row["attempt_id"] for row in valid_rows]:
                raise ExecutionError("settlement_omits_valid_attempt")
            if output_ids != [row["output_cas_object_id"] for row in valid_rows]:
                raise ExecutionError("settlement_output_set_mismatch")
            valid_attempts = [
                {
                    "attempt_id": row["attempt_id"],
                    "normalized": _json(row["normalized_result_json"]),
                }
                for row in valid_rows
            ]
            decision = settlement_decision(valid_attempts)
            decision["valid_output_cas_ids"] = output_ids
            expected_sha = _sha(
                "history-task-settlement-v2",
                {"task_hash": task["task_hash"], **decision},
            )
            if expected_sha != settlement["settlement_sha256"]:
                raise ExecutionError("settlement_hash_mismatch")
            for attempt_id in attempt_ids:
                attempt = conn.execute(
                    """
                    SELECT attempt.request_cas_object_id, completion.output_cas_object_id
                    FROM audit_task_attempts attempt
                    JOIN audit_attempt_completions_v2 completion
                      ON completion.attempt_id=attempt.attempt_id
                    WHERE attempt.attempt_id=? AND attempt.task_hash=?
                    """,
                    (attempt_id, task["task_hash"]),
                ).fetchone()
                if attempt is None:
                    raise ExecutionError("settlement_attempt_missing")
                for object_id in attempt:
                    verified = history_cas.verify_object(conn, cas_root, object_id)
                    if verified["integrity_state"] == "expired":
                        raise history_cas.CASIntegrityError("settlement payload expired")
            continue
        if task["state"] in {"superseded", "exhausted", "planned"}:
            continue
        if task["state"] in {"claimed", "settling"} and _now(task["lease_until"]) <= current:
            if conn.execute(
                "SELECT 1 FROM audit_task_settlements_v2 WHERE task_hash=?",
                (task["task_hash"],),
            ).fetchone() is not None:
                raise ExecutionError("unsettled_state_has_terminal_settlement")
            try:
                conn.execute("BEGIN IMMEDIATE")
                history_audit_store.compare_and_set_logical_task(
                    conn, task["task_hash"],
                    expected_state=task["state"], expected_fence=task["fence"],
                    new_state="planned", new_fence=task["fence"] + 1,
                    claim_token=None, lease_until=None,
                )
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            recovered.append(task["task_hash"])
    return recovered
