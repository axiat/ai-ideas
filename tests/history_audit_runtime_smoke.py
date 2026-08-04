#!/usr/bin/env python3
"""Deterministic L2 execution, settlement, coverage, and recovery smoke tests."""

import copy
import contextlib
import datetime
import hashlib
import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_audit_plan
from lib import history_audit_store
from lib import history_audit_eval_v2
from lib import history_contract_v2

try:
    from lib import history_execution
except ImportError:
    history_execution = None


def sha(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def record(item_id, content, lineage_id):
    return {
        "item_id": item_id,
        "artifact_sha": sha(content),
        "content": content,
        "lineage_id": lineage_id,
    }


class HistoryAuditRuntimeSmoke(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name).resolve()
        self.cas_root = self.root / "cas"
        self.db_path = self.root / "runtime.sqlite3"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        history_audit_store.init_schema(self.conn)
        self.records = [
            record("asset-1", "alpha evidence", "lineage-a"),
            record("asset-2", "beta evidence", "lineage-b"),
        ]
        self.capabilities = {
            provider: {
                "provider": provider,
                "capability_profile_hash": sha("capability-" + provider),
                "model_identity": "fake-model-" + provider,
                "reasoning_identity": "high",
                "model_default": False,
                "reasoning_default": False,
                "executable": provider,
                "cli_revision": "fake-cli-v1",
            }
            for provider in ("codex", "grok", "reviewer")
        }
        self.plan = self._plan(self.records)

    def tearDown(self):
        self.conn.close()
        self.temporary.cleanup()

    def _api(self, name):
        value = getattr(history_execution, name, None) if history_execution else None
        self.assertTrue(callable(value), f"missing behavior: history_execution.{name}")
        return value

    def _without_candidate_budget_migrations(self):
        return tuple(
            migration for migration in history_audit_store.MIGRATIONS
            if migration.component != "candidate-budget-authority"
        )

    def _now(self, seconds=0):
        base = datetime.datetime(2026, 8, 3, tzinfo=datetime.timezone.utc)
        return (base + datetime.timedelta(seconds=seconds)).isoformat()

    def _risk_policy(self):
        return json.loads(
            (ROOT / "history/risk-policy-v1.json").read_text(encoding="utf-8")
        )

    def _router_facts(self, **overrides):
        facts = {
            "retriever_calibrated": False,
            "finalist_or_sa": True,
            "mandatory_channel_failed": False,
            "comparator_uncertain": False,
            "bad_slice_membership": True,
            "index_profile_recently_changed": False,
            "permanent_no_match_requested": False,
            "release_qualified": False,
        }
        facts.update(overrides)
        return facts

    def _route_authority(
        self, plan=None, *, risk_slices=None, facts=None, candidate_routes=None
    ):
        plan = plan or self.plan
        return {
            "risk_policy": self._risk_policy(),
            "risk_slice_policy": {
                "schema_version": "history-risk-slice-policy-v1",
                "policy_version": "critical-semantic-slices-v1",
                "allowed_slices": [
                    "cross_language", "lineage_revision", "low_overlap",
                ],
            },
            "candidate_routes": candidate_routes or [{
                "candidate": copy.deepcopy(plan["candidate"]),
                "router_facts": facts or self._router_facts(),
                "risk_slices": sorted(
                    ["low_overlap"] if risk_slices is None else risk_slices
                ),
            }],
        }

    def _plan(
        self, records, *, shards=None, started_attempt_limit=100,
        additional_candidates=None, intent="duplicate_search",
        map_providers=None, comparator_providers=None, router_facts=None,
    ):
        candidate_id = "stg-v2-" + sha("runtime-candidate-id")
        candidate = {
            "candidate_id": candidate_id,
            "candidate_hash": "",
            "raw_artifact_sha": sha("runtime-candidate-raw"),
            "source_order": 0,
        }
        candidate["candidate_hash"] = history_contract_v2.framed_sha256(
            "history-runtime-candidate-v2",
            history_contract_v2.canonical_bytes(
                {
                    "candidate_id": candidate_id,
                    "raw_artifact_sha": candidate["raw_artifact_sha"],
                    "source_order": 0,
                }
            ),
        )
        expected_ids = sorted(item["item_id"] for item in records)
        current_ids = sorted([
            candidate_id,
            *[
                item["candidate_id"]
                for item in (additional_candidates or [])
            ],
        ])
        expected_hash = history_contract_v2.ordered_set_sha256(
            "history-snapshot-assets-v2", expected_ids
        )
        current_hash = history_contract_v2.ordered_set_sha256(
            "history-current-batch-ids-v2", current_ids
        )
        snapshot_material = {
            "run_id": "run-runtime-smoke",
            "batch_id": "batch-1",
            "history_as_of_watermark": 550,
            "current_batch_id_namespace": "history-v2-staging-v1",
            "current_batch_ids_hash": current_hash,
            "exclusion_policy_sha": sha("exclusion"),
            "expected_asset_ids_hash": expected_hash,
        }
        snapshot_hash = history_contract_v2.framed_sha256(
            "history-snapshot-v2",
            history_contract_v2.canonical_bytes(snapshot_material),
        )
        snapshot = {
            "snapshot_id": history_contract_v2.framed_sha256(
                "history-snapshot-id-v2",
                history_contract_v2.canonical_bytes(
                    {
                        "run_id": "run-runtime-smoke",
                        "batch_id": "batch-1",
                        "snapshot_hash": snapshot_hash,
                    }
                ),
            ),
            "snapshot_hash": snapshot_hash,
            "history_as_of_watermark": 550,
            "current_batch_id_namespace": "history-v2-staging-v1",
            "current_batch_ids_hash": current_hash,
            "current_batch_ids": current_ids,
            "exclusion_policy_sha": sha("exclusion"),
            "expected_asset_ids_hash": expected_hash,
            "expected_asset_ids": expected_ids,
            "records": copy.deepcopy(records),
        }
        if shards is None:
            shards = [{"shard_id": "map-0000", "item_ids": [r["item_id"] for r in records]}]
        for shard in shards:
            shard["serialized_request"] = json.dumps(
                {"item_ids": shard["item_ids"]}, sort_keys=True
            )
            shard["request_sha256"] = hashlib.sha256(
                shard["serialized_request"].encode("utf-8")
            ).hexdigest()
            shard["final_request_tokens"] = len(
                shard["serialized_request"].encode("utf-8")
            )
        provider_pools = {
            "comparator": list(comparator_providers or ["reviewer"]),
            "map": list(map_providers or ["codex", "grok"]),
            "detail": ["codex"],
            "reduce": ["codex"],
        }
        active_providers = {
            provider
            for pool in provider_pools.values()
            for provider in pool
        }
        provider_capabilities = {
            provider: copy.deepcopy(capability)
            for provider, capability in self.capabilities.items()
            if provider in active_providers
        }
        plan = {
            "schema_version": "history-audit-plan-v2",
            "run_id": "run-runtime-smoke",
            "batch_id": "batch-1",
            "candidate": candidate,
            "snapshot": snapshot,
            "provider_pools_ordered": provider_pools,
            "provider_capability_profile_hashes": {
                provider: capability["capability_profile_hash"]
                for provider, capability in provider_capabilities.items()
            },
            "provider_capabilities": provider_capabilities,
            "shards": shards,
            "intent": intent,
        }
        selected_route = history_audit_eval_v2.route_candidate(
            {
                **(router_facts or self._router_facts()),
                "candidate_budget_available": True,
                "attempt_budget_available": True,
            },
            self._risk_policy(),
        )
        plan.update(
            history_audit_plan._issue_test_runtime_authority(
                provider_pools_ordered=plan["provider_pools_ordered"],
                provider_capabilities=plan["provider_capabilities"],
                intent=plan["intent"],
                started_attempt_limit=min(started_attempt_limit, 64),
                semantic_policy_profile_id="semantic-test-v1",
                matched_router_rule_ids=selected_route["matched_rule_ids"],
                max_output_tokens=64,
            )
        )
        plan["shard_plan_sha"] = history_contract_v2.framed_sha256(
            "history-shard-plan-v2",
            history_contract_v2.canonical_bytes(
                sorted(copy.deepcopy(shards), key=lambda shard: shard["shard_id"])
            ),
        )
        plan["plan_sha"] = history_audit_plan.runtime_plan_sha(plan)
        plan["logical_task_keys"] = [
            history_contract_v2.logical_task_key(
                plan["plan_sha"], "map", candidate_id, shard["request_sha256"]
            )
            for shard in shards
        ]
        return plan

    def _router_candidate_cohort(self, plan, additional_candidates=None):
        candidates = {plan["candidate"]["candidate_id"]: plan["candidate"]}
        candidates.update({
            candidate["candidate_id"]: candidate
            for candidate in (additional_candidates or [])
        })
        self.assertEqual(
            sorted(candidates), plan["snapshot"]["current_batch_ids"]
        )
        return [copy.deepcopy(candidates[value]) for value in sorted(candidates)]

    def _router_round_material(self, plan, additional_candidates=None):
        snapshot_fields = {
            "snapshot_id", "snapshot_hash", "history_as_of_watermark",
            "current_batch_id_namespace", "current_batch_ids_hash",
            "current_batch_ids", "exclusion_policy_sha",
            "expected_asset_ids_hash", "expected_asset_ids",
        }
        return {
            "schema_version": "history-router-round-v1",
            "run_id": plan["run_id"],
            "batch_id": plan["batch_id"],
            "intent": plan["intent"],
            "snapshot": {
                name: copy.deepcopy(plan["snapshot"][name])
                for name in snapshot_fields
            },
            "candidates": self._router_candidate_cohort(
                plan, additional_candidates=additional_candidates
            ),
            "semantic_policy_profile_id": plan["semantic_policy_profile_id"],
            "risk_policy_sha": plan["risk_policy_sha"],
            "risk_slice_policy_sha": history_contract_v2.framed_sha256(
                "history-risk-slice-policy-v1",
                history_contract_v2.canonical_bytes(
                    history_audit_eval_v2.RISK_SLICE_POLICY_V1
                ),
            ),
            "budget_policy_sha": history_audit_plan.runtime_budget_policy_sha(
                plan["budget_policy"]
            ),
            "authority_scope": "test_fake",
        }

    def _router_dependencies(self, route_round):
        return {
            "semantic_policy": sha("runtime-router-semantic-policy"),
            "plan": route_round["route_round_sha256"],
            "prompt": sha("runtime-router-prompt"),
            "schema": sha("runtime-router-schema"),
            "ordered_provider_pools": sha("runtime-router-provider-pools"),
            "capacity": sha("runtime-router-capacity"),
            "provider": sha("runtime-router-provider"),
            "fault": sha("runtime-router-fault"),
            "replay": sha("runtime-router-replay"),
            "fts": sha("runtime-router-fts"),
            "metadata": sha("runtime-router-metadata"),
        }

    def _router_domain_sources(
        self, plan, route_round, *, calibrated=False,
        comparator="pre_l1_skip", risk_slices_by_candidate=None,
    ):
        snapshot = plan["snapshot"]
        selected_id = plan["candidate"]["candidate_id"]
        candidate_ids = list(snapshot["current_batch_ids"])
        identity = {
            "run_id": plan["run_id"],
            "batch_id": plan["batch_id"],
            "snapshot_id": snapshot["snapshot_id"],
            "snapshot_hash": snapshot["snapshot_hash"],
            "route_round_sha256": route_round["route_round_sha256"],
        }
        selection_members = []
        l1_members = []
        risk_members = []
        request_members = []
        risk_slices_by_candidate = risk_slices_by_candidate or {}
        for candidate_id in candidate_ids:
            selection_members.append({
                "candidate_id": candidate_id,
                "selection_class": (
                    "finalist" if candidate_id == selected_id else "screened"
                ),
                "channel_states": [
                    {"channel_id": "dense_core", "state": "complete"},
                    {"channel_id": "exact_lineage", "state": "complete"},
                    {"channel_id": "fts", "state": "complete"},
                ],
            })
            if comparator == "pre_l1_skip":
                l1_members.append({
                    "candidate_id": candidate_id,
                    "observation_kind": "pre_l1_skip",
                    "skip_reason": "retriever_uncalibrated",
                    "coverage_state": "not_run",
                    "pre_phase_fact_sha256": None,
                })
            else:
                l1_members.append({
                    "candidate_id": candidate_id,
                    "observation_kind": "comparator",
                    "comparator_outcome": comparator,
                    "coverage_state": "complete",
                    "comparator_receipt_sha256": sha(
                        "runtime-router-comparator-receipt-" + candidate_id
                    ),
                })
            risk_members.append({
                "candidate_id": candidate_id,
                "assigned_slice_ids": sorted(
                    risk_slices_by_candidate.get(candidate_id, ["low_overlap"])
                ),
            })
            request_members.append({
                "candidate_id": candidate_id,
                "request_state": "not_requested",
                "request_id": None,
            })
        qrels_hash = sha("runtime-router-qrels")
        dependencies = self._router_dependencies(route_round)
        return {
            "selection": {
                "schema_version": "history-router-selection-source-v1",
                **identity,
                "selected_candidate_id": selected_id,
                "candidate_ids": candidate_ids,
                "members": selection_members,
            },
            "l1_observation": {
                "schema_version": "history-router-l1-source-v1",
                **identity,
                "candidate_ids": candidate_ids,
                "members": l1_members,
            },
            "calibration": {
                "schema_version": "history-router-calibration-source-v1",
                **identity,
                "semantic_policy_profile_id": plan[
                    "semantic_policy_profile_id"
                ],
                "qrels_hash": qrels_hash,
                "calibration_state": (
                    "shadow_ready" if calibrated else "unqualified"
                ),
            },
            "qualification": {
                "schema_version": "history-router-qualification-source-v1",
                **identity,
                "semantic_policy_profile_id": plan[
                    "semantic_policy_profile_id"
                ],
                "qrels_hash": qrels_hash,
                "qualification_id": None,
                "lookup_state": "unavailable",
                "dependency_heads": dependencies,
            },
            "risk_assignment": {
                "schema_version": "history-router-risk-assignment-source-v1",
                **identity,
                "candidate_ids": candidate_ids,
                "members": risk_members,
            },
            "dependency_heads": {
                "schema_version": "history-router-dependency-heads-source-v1",
                **identity,
                "heads": dependencies,
                "observed_index_profile_sha256": dependencies["fts"],
            },
            "permanent_request": {
                "schema_version": "history-router-permanent-request-source-v1",
                **identity,
                "candidate_ids": candidate_ids,
                "members": request_members,
            },
        }

    def _install(
        self, plan=None, *, additional_candidates=None, calibrated=False,
        comparator="pre_l1_skip", risk_slices_by_candidate=None,
    ):
        plan = plan or self.plan
        try:
            material = history_audit_plan.build_runtime_plan_material(plan)
            computed_plan_sha = history_audit_plan.runtime_plan_sha_from_material(
                material
            )
            records = history_audit_plan.runtime_snapshot_records(
                plan["snapshot"]["records"]
            )
        except history_audit_plan.AuditPlanError as exc:
            raise self._api("ExecutionError")(
                "frozen_identity_mismatch", exc.code
            ) from exc
        if (
            plan.get("plan_sha") != computed_plan_sha
            or plan.get("shard_plan_sha") != material["shard_plan_sha"]
            or sorted(item["item_id"] for item in records)
            != material["snapshot"]["expected_asset_ids"]
            or plan["candidate"]["candidate_id"]
            not in material["snapshot"]["current_batch_ids"]
        ):
            raise self._api("ExecutionError")("frozen_identity_mismatch")
        route_round = history_audit_store.prepare_router_round(
            self.conn,
            self._router_round_material(
                plan, additional_candidates=additional_candidates
            ),
            created_at=self._now(5),
        )
        sources = self._router_domain_sources(
            plan,
            route_round,
            calibrated=calibrated,
            comparator=comparator,
            risk_slices_by_candidate=risk_slices_by_candidate,
        )
        pre_sources = {
            name: value
            for name, value in sources.items()
            if name != "l1_observation"
        }
        history_audit_store._issue_test_router_domain_sources(
            self.conn,
            route_round["route_round_sha256"],
            sources=copy.deepcopy(pre_sources),
            created_at=self._now(10),
        )
        pre = history_audit_store.derive_candidate_route_facts(
            self.conn,
            plan["run_id"],
            plan["batch_id"],
            plan["intent"],
            phase="pre_l1",
            created_at=self._now(20),
        )
        l1_source = copy.deepcopy(sources["l1_observation"])
        pre_by_candidate = {
            item["candidate_id"]: item["phase_fact_sha256"]
            for item in pre["candidate_routes"]
        }
        for member in l1_source["members"]:
            if member["observation_kind"] == "pre_l1_skip":
                member["pre_phase_fact_sha256"] = pre_by_candidate[
                    member["candidate_id"]
                ]
        history_audit_store._issue_test_router_domain_sources(
            self.conn,
            route_round["route_round_sha256"],
            sources={"l1_observation": l1_source},
            created_at=self._now(30),
        )
        history_audit_store.derive_candidate_route_facts(
            self.conn,
            plan["run_id"],
            plan["batch_id"],
            plan["intent"],
            phase="final",
            created_at=self._now(40),
        )
        self._api("persist_plan")(self.conn, plan)
        return plan

    def _persist_pre_route_plan(self, conn, plan):
        """Build an old-schema fixture without weakening current runtime gates."""
        has_budget_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='audit_candidate_budget_receipts_v2'"
        ).fetchone() is not None
        has_route = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='audit_candidate_route_facts_v2'"
        ).fetchone() is not None and conn.execute(
            "SELECT 1 FROM audit_candidate_route_facts_v2 WHERE run_id=?",
            (plan["run_id"],),
        ).fetchone() is not None
        if has_budget_table and not has_route:
            conn.execute(
                "DROP TRIGGER IF EXISTS "
                "audit_l2_plans_v2_candidate_budget_guard"
            )
        admission = (
            contextlib.nullcontext()
            if has_budget_table
            else mock.patch.object(
                history_audit_store,
                "issue_candidate_budget_receipt",
                return_value={
                    "decision": "accepted", "decided_at": self._now()
                },
            )
        )
        def insert_plan_without_dispatch(target_conn, plan_values):
            target_conn.execute(
                "INSERT INTO audit_l2_plans_v2 "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                plan_values,
            )

        with mock.patch.object(
            history_audit_store,
            "_router_candidate_cohort_for_plan_persistence",
            return_value=[copy.deepcopy(plan["candidate"])],
        ), mock.patch.object(
            history_audit_store,
            "_materialize_final_candidate_routes_for_plan",
        ), mock.patch.object(
            history_audit_store,
            "_insert_new_l2_plan_with_dispatch",
            side_effect=insert_plan_without_dispatch,
        ), admission:
            history_execution.persist_plan(conn, plan)

    def _record_prefix_legacy_route(
        self, conn, plan, *, created_at
    ):
        """Seed the caller-route projection used by one prefix migration fixture."""
        authority = self._route_authority(plan)
        routes = authority["candidate_routes"]
        candidate_ids = [item["candidate"]["candidate_id"] for item in routes]
        risk_policy = authority["risk_policy"]
        slice_policy = authority["risk_slice_policy"]
        risk_policy_sha = history_audit_store._semantic_sha(
            "history-risk-policy-v1", risk_policy
        )
        slice_policy_sha = history_audit_store._semantic_sha(
            "history-risk-slice-policy-v1", slice_policy
        )
        cohort_material = {
            "run_id": plan["run_id"],
            "batch_id": plan["batch_id"],
            "intent": plan["intent"],
            "candidate_ids": candidate_ids,
            "risk_policy_sha256": risk_policy_sha,
            "risk_slice_policy_sha256": slice_policy_sha,
            "created_at": created_at,
        }
        cohort_sha = history_audit_store._semantic_sha(
            "history-candidate-route-cohort-v2", cohort_material
        )
        cohort_values = (
            plan["run_id"], plan["batch_id"], plan["intent"],
            history_audit_store._semantic_canonical(candidate_ids),
            history_audit_store._semantic_canonical(risk_policy),
            risk_policy_sha,
            history_audit_store._semantic_canonical(slice_policy),
            slice_policy_sha, cohort_sha, created_at,
        )
        guard = history_audit_store._COST_FACT_GUARDS[id(conn)]
        guard["cohort"] = cohort_values
        try:
            conn.execute(
                "INSERT INTO audit_candidate_route_cohorts_v2 "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                cohort_values,
            )
        finally:
            guard["cohort"] = None
        item = routes[0]
        facts = {
            **item["router_facts"],
            "candidate_budget_available": True,
            "attempt_budget_available": True,
        }
        derived = history_audit_eval_v2.route_candidate(facts, risk_policy)
        route_material = {
            "run_id": plan["run_id"],
            "candidate_id": plan["candidate"]["candidate_id"],
            "intent": plan["intent"],
            "cohort_sha256": cohort_sha,
            "router_facts": facts,
            "risk_slices": item["risk_slices"],
            "matched_rule_ids": derived["matched_rule_ids"],
            "route": derived["route"],
            "call_l1_model": derived["call_l1_model"],
            "dispatch_allowed": derived["dispatch_allowed"],
            "rule_table_sha256": derived["rule_table_sha256"],
            "risk_policy_version": derived["receipt_risk_policy_version"],
            "created_at": created_at,
        }
        fact_sha = history_audit_store._semantic_sha(
            "history-candidate-route-fact-v2", route_material
        )
        route_values = (
            plan["run_id"], plan["candidate"]["candidate_id"],
            plan["intent"], cohort_sha,
            history_audit_store._semantic_canonical(facts),
            history_audit_store._semantic_canonical(item["risk_slices"]),
            history_audit_store._semantic_canonical(
                derived["matched_rule_ids"]
            ),
            derived["route"], int(derived["call_l1_model"]),
            int(derived["dispatch_allowed"]), derived["rule_table_sha256"],
            derived["receipt_risk_policy_version"], fact_sha, created_at,
        )
        guard["route"] = route_values
        try:
            conn.execute(
                "INSERT INTO audit_candidate_route_facts_v2 "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                route_values,
            )
        finally:
            guard["route"] = None
        observation_material = {
            "run_id": plan["run_id"],
            "candidate_id": plan["candidate"]["candidate_id"],
            "route_fact_sha256": fact_sha,
            "observation_scope": "host_issued_shadow",
            "production_authority": False,
            "created_at": created_at,
        }
        observation_values = (
            plan["run_id"], plan["candidate"]["candidate_id"], fact_sha,
            "host_issued_shadow", 0,
            history_audit_store._semantic_sha(
                "history-candidate-route-observation-boundary-v1",
                observation_material,
            ),
            created_at,
        )
        guard["route_observation"] = observation_values
        try:
            conn.execute(
                "INSERT INTO audit_candidate_route_observation_boundaries_v2 "
                "VALUES(?,?,?,?,?,?,?)",
                observation_values,
            )
        finally:
            guard["route_observation"] = None
        return fact_sha

    def _persist_prefix_legacy_route_plan(self, conn, plan):
        self._persist_pre_route_plan(conn, plan)
        created_at = conn.execute(
            "SELECT created_at FROM audit_l2_plans_v2 WHERE plan_sha=?",
            (plan["plan_sha"],),
        ).fetchone()[0]
        conn.execute("BEGIN IMMEDIATE")
        try:
            fact_sha = self._record_prefix_legacy_route(
                conn, plan, created_at=created_at
            )
            dispatch_material = {
                "plan_sha": plan["plan_sha"],
                "run_id": plan["run_id"],
                "candidate_id": plan["candidate"]["candidate_id"],
                "route_fact_sha256": fact_sha,
                "created_at": created_at,
            }
            dispatch_values = (
                plan["plan_sha"], plan["run_id"],
                plan["candidate"]["candidate_id"], fact_sha,
                history_audit_store._semantic_sha(
                    "history-candidate-l2-dispatch-v2", dispatch_material
                ),
                created_at,
            )
            guard = history_audit_store._COST_FACT_GUARDS[id(conn)]
            guard["dispatch"] = dispatch_values
            try:
                conn.execute(
                    "INSERT INTO audit_candidate_l2_dispatch_facts_v2 "
                    "VALUES(?,?,?,?,?,?)",
                    dispatch_values,
                )
            finally:
                guard["dispatch"] = None
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def _seed_route_prerequisites(
        self, conn, plan, candidates, *, run_created_at=None,
        staging_created_at=None,
    ):
        material = history_audit_plan.build_runtime_plan_material(plan)
        history_audit_store.issue_candidate_budget_receipt(
            conn, material, plan["plan_sha"], decided_at=run_created_at
        )
        snapshot = plan["snapshot"]
        run_created_at = run_created_at or self._now(10)
        staging_created_at = staging_created_at or self._now()
        conn.execute(
            "INSERT INTO audit_run_manifests VALUES(?,?,?,?,?)",
            (
                plan["run_id"], "history-audit-manifest-v2",
                plan["plan_sha"], history_execution._canonical(material),
                run_created_at,
            ),
        )
        conn.execute(
            "INSERT INTO audit_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                snapshot["snapshot_id"], snapshot["snapshot_hash"],
                snapshot["history_as_of_watermark"],
                snapshot["current_batch_id_namespace"],
                snapshot["current_batch_ids_hash"],
                snapshot["exclusion_policy_sha"],
                snapshot["expected_asset_ids_hash"], staging_created_at,
                plan["run_id"], plan["batch_id"],
            ),
        )
        conn.execute(
            "INSERT INTO audit_snapshot_batch_sets VALUES(?,?,?,?,?,?,?)",
            (
                snapshot["snapshot_id"], plan["run_id"], plan["batch_id"],
                snapshot["current_batch_ids_hash"],
                json.dumps(
                    snapshot["current_batch_ids"], sort_keys=True,
                    separators=(",", ":"),
                ),
                len(snapshot["current_batch_ids"]), staging_created_at,
            ),
        )
        for candidate in candidates:
            history_audit_store.insert_authorized_batch_staging(
                conn,
                staging_candidate_id=candidate["candidate_id"],
                run_id=plan["run_id"],
                batch_id=plan["batch_id"],
                candidate_hash=candidate["candidate_hash"],
                raw_artifact_sha=candidate["raw_artifact_sha"],
                source_order=candidate["source_order"],
                created_at=staging_created_at,
            )
        conn.commit()
        return run_created_at

    def test_persist_plan_requires_bound_route_authority(self):
        with self.assertRaises(self._api("ExecutionError")) as caught:
            self._api("persist_plan")(self.conn, self.plan)
        self.assertEqual(caught.exception.code, "route_authority_required")
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM audit_l2_plans_v2").fetchone()[0],
            0,
        )

    def test_cost_summary_handles_zero_candidate_denominator(self):
        self.assertEqual(
            history_audit_eval_v2.summarize_realized_cost(
                self.conn, "run-without-candidates"
            ),
            {"run_id": "run-without-candidates", "intents": {}},
        )

    def test_route_authority_rejects_unselected_intent_and_unknown_slice(self):
        authority = self._route_authority()
        authority["intent"] = "arbitrary_intent"
        with self.assertRaises(self._api("ExecutionError")) as caught:
            self._api("persist_plan")(
                self.conn, self.plan, route_authority=authority
            )
        self.assertEqual(caught.exception.code, "caller_route_authority_forbidden")
        authority = self._route_authority(risk_slices=["invented_slice"])
        with self.assertRaises(self._api("ExecutionError")) as caught:
            self._api("persist_plan")(
                self.conn, self.plan, route_authority=authority
            )
        self.assertEqual(caught.exception.code, "caller_route_authority_forbidden")
        authority["risk_slice_policy"]["allowed_slices"].append(
            "invented_slice"
        )
        authority["risk_slice_policy"]["allowed_slices"].sort()
        with self.assertRaises(self._api("ExecutionError")) as caught:
            self._api("persist_plan")(
                self.conn, self.plan, route_authority=authority
            )
        self.assertEqual(caught.exception.code, "caller_route_authority_forbidden")

    def test_route_cohort_cannot_omit_a_frozen_batch_candidate(self):
        second = {
            "candidate_id": "stg-v2-" + sha("omitted-frozen-candidate"),
            "candidate_hash": "",
            "raw_artifact_sha": sha("omitted-frozen-candidate-raw"),
            "source_order": 1,
        }
        second["candidate_hash"] = history_audit_plan.runtime_candidate_hash(
            second
        )
        plan = self._plan(self.records, additional_candidates=[second])
        with self.assertRaises(self._api("ExecutionError")) as caught:
            self._api("persist_plan")(
                self.conn, plan,
                route_authority=self._route_authority(plan),
            )
        self.assertEqual(caught.exception.code, "caller_route_authority_forbidden")
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_candidate_route_facts_v2"
            ).fetchone()[0],
            0,
        )

    def test_route_rejects_caller_claimed_release_qualification(self):
        authority = self._route_authority(
            facts=self._router_facts(release_qualified=True)
        )
        with self.assertRaises(self._api("ExecutionError")) as caught:
            self._api("persist_plan")(
                self.conn, self.plan, route_authority=authority
            )
        self.assertEqual(caught.exception.code, "caller_route_authority_forbidden")
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_candidate_route_facts_v2"
            ).fetchone()[0],
            0,
        )

    def test_legacy_plan_cannot_receive_retroactive_route_facts(self):
        plan = self.plan
        self._persist_pre_route_plan(self.conn, plan)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(history_audit_store.AuditMigrationError):
                history_audit_store.record_candidate_route_facts(
                    self.conn, plan["run_id"], plan["batch_id"],
                    plan["intent"], self._route_authority(plan),
                    created_at=self._now(),
                )
        finally:
            self.conn.execute("ROLLBACK")
        summary = history_audit_eval_v2.summarize_realized_cost(
            self.conn, plan["run_id"]
        )["intents"][plan["intent"]]
        self.assertFalse(summary["route_facts_complete"])
        self.assertEqual(
            summary["expected_unavailable_reason"],
            "candidate_route_facts_unavailable",
        )

    def test_public_dispatch_cannot_retrofit_plan_or_open_attempt_gate(self):
        plan = self.plan
        created_at = self._seed_route_prerequisites(
            self.conn, plan, [plan["candidate"]]
        )
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaisesRegex(
                history_audit_store.AuditMigrationError,
                "caller_route_authority_forbidden",
            ):
                history_audit_store.record_candidate_route_facts(
                    self.conn, plan["run_id"], plan["batch_id"],
                    plan["intent"], self._route_authority(plan),
                    created_at=created_at,
                )
        finally:
            self.conn.execute("ROLLBACK")
        self._persist_pre_route_plan(self.conn, plan)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(history_audit_store.AuditMigrationError):
                history_audit_store.record_candidate_l2_dispatch_fact(
                    self.conn, plan["plan_sha"], created_at=created_at
                )
        finally:
            self.conn.execute("ROLLBACK")
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_candidate_l2_dispatch_facts_v2"
            ).fetchone()[0],
            0,
        )
        task_key = plan["logical_task_keys"][0]
        self._api("claim_task")(
            self.conn, task_key, "worker", 60, 0, now=self._now()
        )
        with self.assertRaises(self._api("ExecutionError")) as caught:
            self._api("record_attempt")(
                self.conn, task_key, plan["provider_capabilities"]["codex"],
                {"attempt_kind": "initial"}, cas_root=self.cas_root,
                request_bytes=plan["shards"][0]["serialized_request"].encode(),
            )
        self.assertEqual(caught.exception.code, "missing_route_dispatch_authority")

    def test_route_summary_marks_inputs_as_host_issued_shadow(self):
        plan = self._install()
        summary = history_audit_eval_v2.summarize_realized_cost(
            self.conn, plan["run_id"]
        )["intents"][plan["intent"]]
        self.assertEqual(
            summary["route_observation_scope"], "host_issued_shadow"
        )
        self.assertFalse(summary["route_observations_authorize_production"])

    def _output(self, plan=None, *, relations=None, item_ids=None, truncated=False):
        plan = plan or self.plan
        records = {item["item_id"]: item for item in plan["snapshot"]["records"]}
        item_ids = item_ids or list(plan["shards"][0]["item_ids"])
        relations = relations or {}
        items = []
        for item_id in item_ids:
            source = records.get(item_id, self.records[0])
            items.append(
                {
                    "item_id": item_id,
                    "semantic_relation": relations.get(item_id, "distinct"),
                    "lineage_relation": "none",
                    "anchor": {
                        "asset_id": item_id,
                        "artifact_sha": source["artifact_sha"],
                        "start": 0,
                        "end": 5,
                        "quote": source["content"][:5],
                    },
                }
            )
        return {
            "schema_version": "history-map-output-v1",
            "snapshot_id": plan["snapshot"]["snapshot_id"],
            "snapshot_hash": plan["snapshot"]["snapshot_hash"],
            "truncated": truncated,
            "items": items,
        }

    def _detail_output(self, task, *, anchor_item_id=None):
        request = json.loads(task["durable_request_text"])
        records = {
            item["item_id"]: item for item in self.plan["snapshot"]["records"]
        }
        records.update({
            item["item_id"]: item for item in task["frozen_records"]
        })
        item_ids = task["assigned_item_ids"]
        anchor_item_id = anchor_item_id or item_ids[0]
        source = records[anchor_item_id]
        return {
            "schema_version": "history-detail-output-v1",
            "generation_id": request["generation_id"],
            "snapshot_id": task["snapshot_id"],
            "snapshot_hash": task["snapshot_hash"],
            "task_hash": task["task_hash"],
            "truncated": False,
            "detail_card": {
                "lineage_id": request["exceptional_card"]["lineage_id"],
                "semantic_relation": request["exceptional_card"][
                    "semantic_relation"
                ],
                "item_ids": item_ids,
                "evidence": [{
                    "asset_id": anchor_item_id,
                    "artifact_sha": source["artifact_sha"],
                    "start": 0,
                    "end": 5,
                    "quote": source["content"][:5],
                }],
            },
        }

    def _reduce_output(self, task, *, cards=None):
        request = json.loads(task["durable_request_text"])
        return {
            "schema_version": "history-reduce-output-v1",
            "generation_id": request["generation_id"],
            "snapshot_id": task["snapshot_id"],
            "snapshot_hash": task["snapshot_hash"],
            "task_hash": task["task_hash"],
            "truncated": False,
            "cards": copy.deepcopy(
                request["detail_cards"] if cards is None else cards
            ),
        }

    def _attempt(self, plan, task_key, provider, output):
        claim = self._api("claim_task")(
            self.conn, task_key, "worker-1", 60, expected_fence=0, now=self._now()
        )
        attempt = self._api("record_attempt")(
            self.conn,
            task_key,
            {"provider": provider, "profile_hash": sha(provider)},
            {
                "attempt_kind": "initial",
                "input_tokens": 10,
                "output_tokens": 10,
                "provider_usage_units": 20,
            },
            cas_root=self.cas_root,
            request_bytes=plan["shards"][0]["serialized_request"].encode(),
        )
        valid = self._api("complete_attempt")(
            self.conn,
            self.cas_root,
            task_key,
            attempt["attempt_id"],
            output,
            plan["snapshot"],
        )
        return claim, valid

    def test_map_requires_exact_manifest_ids_and_frozen_anchors(self):
        plan = self._install()
        task = self._api("load_task")(self.conn, plan["logical_task_keys"][0])
        valid = self._api("validate_map_output")(task, self._output(), plan["snapshot"])
        self.assertEqual([item["item_id"] for item in valid["items"]], ["asset-1", "asset-2"])
        reversed_output = self._output()
        reversed_output["items"].reverse()
        reordered = self._api("validate_map_output")(
            task, reversed_output, plan["snapshot"]
        )
        self.assertEqual(
            [item["item_id"] for item in reordered["items"]],
            ["asset-1", "asset-2"],
        )

        stale = self._output()
        stale["items"][0]["anchor"]["quote"] = "wrong"
        with self.assertRaises(self._api("MapValidationError")) as caught:
            self._api("validate_map_output")(task, stale, plan["snapshot"])
        self.assertEqual(caught.exception.code, "invalid_anchor")

        extra = self._output(item_ids=["asset-1", "asset-2", "asset-x"])
        with self.assertRaises(self._api("MapValidationError")) as caught:
            self._api("validate_map_output")(task, extra, plan["snapshot"])
        self.assertEqual(caught.exception.code, "item_set_mismatch")

    def test_consistent_looking_hash_labels_cannot_forge_frozen_identity(self):
        forged = copy.deepcopy(self.plan)
        forged["plan_sha"] = sha("forged-plan-label")
        forged["candidate"]["candidate_hash"] = sha("forged-candidate-label")
        forged["snapshot"]["snapshot_hash"] = sha("forged-snapshot-label")
        forged["snapshot"]["expected_asset_ids_hash"] = sha("forged-assets-label")
        forged["logical_task_keys"] = [
            history_contract_v2.logical_task_key(
                forged["plan_sha"],
                "map",
                forged["candidate"]["candidate_id"],
                shard["request_sha256"],
            )
            for shard in forged["shards"]
        ]
        with self.assertRaises(self._api("ExecutionError")) as caught:
            self._install(forged)
        self.assertEqual(caught.exception.code, "frozen_identity_mismatch")

    def test_canonical_plan_persists_provider_keyed_capability_material(self):
        plan = self._install()
        task = self._api("load_task")(self.conn, plan["logical_task_keys"][0])
        self.assertIn("provider_capabilities", task["durable_plan"])
        self.assertEqual(
            task["durable_plan"]["provider_capabilities"], self.capabilities
        )

    def test_budget_event_identity_binds_durable_plan_intent_and_round(self):
        plan = self._plan(self.records, intent="evolution_search")
        self._install(plan)
        self._api("run_map_task")(
            self.conn, self.cas_root, plan, plan["logical_task_keys"][0],
            lambda *_: {"kind": "success", "output": self._output(plan)},
            now=self._now(),
        )
        rows = self.conn.execute(
            """
            SELECT * FROM audit_budget_events
            ORDER BY event_id
            """
        ).fetchall()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["run_id"], plan["run_id"])
            self.assertEqual(row["intent"], "evolution_search")
            self.assertEqual(row["round_id"], plan["plan_sha"])
            material = {
                "run_id": plan["run_id"],
                "intent": "evolution_search",
                "round_id": plan["plan_sha"],
                "event_id": row["event_id"],
                "task_hash": plan["logical_task_keys"][0],
                "event_type": row["event_type"],
                "counters": json.loads(row["counters_json"]),
            }
            expected_sha = history_contract_v2.framed_sha256(
                "history-runtime-budget-event-v1",
                history_contract_v2.canonical_bytes(material),
            )
            self.assertEqual(row["event_sha256"], expected_sha)

    def test_record_attempt_rejects_arbitrary_capability_hash(self):
        plan = self._install()
        task_key = plan["logical_task_keys"][0]
        self._api("claim_task")(
            self.conn, task_key, "worker-a", 60, expected_fence=0, now=self._now()
        )
        forged = copy.deepcopy(self.capabilities["codex"])
        forged["capability_profile_hash"] = sha("arbitrary-profile")
        with self.assertRaises(self._api("ExecutionError")) as caught:
            self._api("record_attempt")(
                self.conn, task_key, forged, {"attempt_kind": "initial"},
                cas_root=self.cas_root,
                request_bytes=plan["shards"][0]["serialized_request"].encode(),
            )
        self.assertEqual(caught.exception.code, "capability_authority_mismatch")

    def test_database_rejects_swapped_provider_profile_provenance(self):
        plan = self._install()
        task_key = plan["logical_task_keys"][0]
        claim = self._api("claim_task")(
            self.conn, task_key, "worker-a", 60, expected_fence=0, now=self._now()
        )
        task = self._api("load_task")(self.conn, task_key)
        request_bytes = task["durable_request_text"].encode()
        request = history_execution.history_cas.put_object(
            self.conn, self.cas_root, request_bytes, "attempt-transient-7d",
            expires_at=(datetime.datetime.fromisoformat(task["created_at"])
                        + datetime.timedelta(days=7)).isoformat(),
        )
        forged = copy.deepcopy(self.capabilities["codex"])
        forged["capability_profile_hash"] = self.capabilities["grok"]["capability_profile_hash"]
        provenance = {
            **forged,
            "attempt_kind": "initial",
            "ordinal": 0,
            "claim_token": claim["claim_token"],
            "claim_fence": claim["fence"],
        }
        attempt_id = history_contract_v2.attempt_id(task_key, 0, provenance)
        maximum = plan["capacity_profile"]["max_output_tokens"]
        reserved = {
            "input_tokens": len(request_bytes),
            "output_tokens": maximum,
            "provider_usage_units": len(request_bytes) + maximum,
        }
        self.conn.execute(
            """
            INSERT INTO audit_runtime_budget_reservations_v2(
              attempt_id, task_hash, plan_sha, candidate_id, intent,
              attempt_kind, reserved_json, created_at
            ) VALUES(?, ?, ?, ?, ?, 'initial', ?, ?)
            """,
            (
                attempt_id, task_key, plan["plan_sha"],
                plan["candidate"]["candidate_id"], plan["intent"],
                history_contract_v2.canonical_bytes(reserved).decode(), self._now(),
            ),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO audit_task_attempts(
                  attempt_id, task_hash, ordinal, provenance_json,
                  request_cas_object_id, output_cas_object_id, state, created_at
                ) VALUES(?, ?, 0, ?, ?, NULL, 'started', ?)
                """,
                (
                    attempt_id, task_key,
                    history_contract_v2.canonical_bytes(provenance).decode(),
                    request["object_id"], self._now(),
                ),
            )

    def _insert_forged_task_binding_chain(self, conn, plan, cas_root):
        shard = plan["shards"][0]
        forged_task = sha("forged-upgrade-task")
        request_bytes = shard["serialized_request"].encode()
        request = history_execution.history_cas.put_object(
            conn, cas_root, request_bytes, "attempt-transient-7d",
            expires_at="2026-08-10T00:00:00+00:00",
        )
        provenance = {
            **copy.deepcopy(self.capabilities["reviewer"]),
            "attempt_kind": "initial",
            "ordinal": 0,
            "claim_token": "forged-worker",
            "claim_fence": 0,
        }
        attempt_id = history_contract_v2.attempt_id(forged_task, 0, provenance)
        maximum = plan["capacity_profile"]["max_output_tokens"]
        reserved = {
            "input_tokens": len(request_bytes),
            "output_tokens": maximum,
            "provider_usage_units": len(request_bytes) + maximum,
        }
        conn.execute(
            """
            INSERT INTO audit_logical_tasks(
              task_hash, run_id, stage, staging_candidate_id, input_id,
              state, fence, claim_token, lease_until, created_at
            ) VALUES(?, ?, 'map', 'forged-candidate', ?,
                     'planned', 0, NULL, NULL, ?)
            """,
            (forged_task, plan["run_id"], shard["shard_id"], self._now()),
        )
        conn.execute(
            """
            INSERT INTO audit_task_bindings_v2(
              task_hash, plan_sha, snapshot_id, snapshot_hash,
              shard_input_sha, assigned_item_ids_json, frozen_records_json,
              provider_pool_json, parent_task_hash, split_depth, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?)
            """,
            (
                forged_task, plan["plan_sha"], plan["snapshot"]["snapshot_id"],
                plan["snapshot"]["snapshot_hash"], shard["request_sha256"],
                history_contract_v2.canonical_bytes(shard["item_ids"]).decode(),
                history_contract_v2.canonical_bytes(plan["snapshot"]["records"]).decode(),
                history_contract_v2.canonical_bytes(["reviewer"]).decode(),
                self._now(),
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
                forged_task, shard["shard_id"], shard["request_sha256"],
                shard["serialized_request"],
                history_contract_v2.canonical_bytes(shard["item_ids"]).decode(),
                self._now(),
            ),
        )
        conn.execute(
            """
            INSERT INTO audit_runtime_budget_reservations_v2(
              attempt_id, task_hash, plan_sha, candidate_id, intent,
              attempt_kind, reserved_json, created_at
            ) VALUES(?, ?, ?, ?, ?, 'initial', ?, ?)
            """,
            (
                attempt_id, forged_task, plan["plan_sha"],
                plan["candidate"]["candidate_id"], plan["intent"],
                history_contract_v2.canonical_bytes(reserved).decode(), self._now(),
            ),
        )
        conn.execute(
            """
            INSERT INTO audit_task_attempts(
              attempt_id, task_hash, ordinal, provenance_json,
              request_cas_object_id, output_cas_object_id, state, created_at
            ) VALUES(?, ?, 0, ?, ?, NULL, 'started', ?)
            """,
            (
                attempt_id, forged_task,
                history_contract_v2.canonical_bytes(provenance).decode(),
                request["object_id"], self._now(),
            ),
        )

    def test_database_rejects_forged_task_binding_before_attempt_authority(self):
        plan = self._install()
        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "forged task authority"
        ):
            self._insert_forged_task_binding_chain(
                self.conn, plan, self.cas_root
            )

    def test_database_rejects_direct_unissued_detail_task(self):
        plan = self._install()
        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "forged task authority"
        ):
            self.conn.execute(
                """
                INSERT INTO audit_logical_tasks(
                  task_hash, run_id, stage, staging_candidate_id, input_id,
                  state, fence, claim_token, lease_until, created_at
                ) VALUES(?, ?, 'detail', ?, 'detail-forged',
                         'planned', 0, NULL, NULL, ?)
                """,
                (
                    sha("direct-forged-detail"), plan["run_id"],
                    plan["candidate"]["candidate_id"], self._now(),
                ),
            )
        self.conn.rollback()

    def test_adjudication_upgrade_rejects_preexisting_unissued_derived_task(self):
        path = self.root / "forged-adjudication-upgrade.sqlite3"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        migrations = history_audit_store.MIGRATIONS
        target = next(
            index for index, migration in enumerate(migrations)
            if migration.component == "l2-adjudication-authority"
        )
        with mock.patch.object(
            history_audit_store, "MIGRATIONS", migrations[:target]
        ):
            history_audit_store.init_schema(conn)
            self._persist_pre_route_plan(conn, self.plan)
            conn.execute(
                "DROP TRIGGER audit_logical_tasks_l2_insert_authority_guard_v2"
            )
            conn.execute(
                """
                INSERT INTO audit_logical_tasks(
                  task_hash, run_id, stage, staging_candidate_id, input_id,
                  state, fence, claim_token, lease_until, created_at
                ) VALUES(?, ?, 'detail', ?, 'detail-preexisting',
                         'planned', 0, NULL, NULL, ?)
                """,
                (
                    sha("preexisting-unissued-detail"), self.plan["run_id"],
                    self.plan["candidate"]["candidate_id"], self._now(),
                ),
            )
            conn.commit()
        try:
            with self.assertRaises(history_audit_store.AuditMigrationError):
                history_audit_store.init_schema(conn)
        finally:
            conn.close()

    def test_upgrade_probe_rejects_existing_forged_task_binding_chain(self):
        conn = sqlite3.connect(self.root / "forged-upgrade.sqlite3")
        conn.row_factory = sqlite3.Row
        before_task_authority = tuple(
            migration for migration in history_audit_store.MIGRATIONS
            if history_audit_store.MIGRATIONS.index(migration)
            < next(
                index for index, item in enumerate(history_audit_store.MIGRATIONS)
                if item.component == "l2-runtime-task-authority"
            )
        )
        with mock.patch.object(
            history_audit_store, "MIGRATIONS", before_task_authority
        ):
            history_audit_store.init_schema(conn)
            self._persist_pre_route_plan(conn, self.plan)
            self._insert_forged_task_binding_chain(
                conn, self.plan, self.root / "forged-upgrade-cas"
            )
            conn.commit()
        try:
            with self.assertRaises(history_audit_store.AuditMigrationError):
                history_audit_store.init_schema(conn)
        finally:
            conn.close()

    def test_valid_root_and_split_facts_upgrade_into_task_authority(self):
        conn = sqlite3.connect(self.root / "valid-task-authority-upgrade.sqlite3")
        conn.row_factory = sqlite3.Row
        before_task_authority = tuple(
            migration for migration in history_audit_store.MIGRATIONS
            if history_audit_store.MIGRATIONS.index(migration)
            < next(
                index for index, item in enumerate(history_audit_store.MIGRATIONS)
                if item.component == "l2-runtime-task-authority"
            )
        )
        with mock.patch.object(
            history_audit_store, "MIGRATIONS", before_task_authority
        ):
            history_audit_store.init_schema(conn)
            self._persist_pre_route_plan(conn, self.plan)
            parent_key = self.plan["logical_task_keys"][0]
            conn.execute("BEGIN IMMEDIATE")
            children = [
                self._insert_canonical_split_child_without_transition(
                    conn, self.plan, parent_key, position,
                    preupgrade_direct=True,
                )
                for position in (0, 1)
            ]
            history_audit_store.compare_and_set_logical_task(
                conn, parent_key,
                expected_state="planned", expected_fence=0,
                new_state="superseded", new_fence=1,
            )
            terminal = {
                "task_hash": parent_key,
                "terminal_state": "superseded",
                "reason": "invalid_parent_split",
            }
            conn.execute(
                "INSERT INTO audit_task_terminal_facts_v2 VALUES(?, ?, ?, ?, ?)",
                (
                    parent_key, "superseded", "invalid_parent_split",
                    history_execution._sha(
                        "history-task-terminal-v2", terminal
                    ),
                    self._now(),
                ),
            )
            for position, child in enumerate(children):
                edge = {
                    "parent_task_hash": parent_key,
                    "child_task_hash": child["task_hash"],
                    "position": position,
                }
                conn.execute(
                    "INSERT INTO audit_task_edges_v2 VALUES(?, ?, ?, ?, ?)",
                    (
                        parent_key, child["task_hash"], position,
                        history_execution._sha("history-task-edge-v2", edge),
                        self._now(),
                    ),
                )
            conn.execute("COMMIT")
        with mock.patch.object(
            history_audit_store, "MIGRATIONS",
            self._without_candidate_budget_migrations(),
        ):
            history_audit_store.init_schema(conn)
        applied = conn.execute(
            """
            SELECT 1 FROM audit_schema_migrations
            WHERE component='l2-runtime-task-authority' AND version=1
            """
        ).fetchone()
        conn.close()
        self.assertIsNotNone(applied)

    def _canonical_split_child(self, conn, plan, parent_key, position):
        parent = history_execution.load_task(conn, parent_key)
        midpoint = len(parent["assigned_item_ids"]) // 2
        groups = (
            parent["assigned_item_ids"][:midpoint],
            parent["assigned_item_ids"][midpoint:],
        )
        child_ids = groups[position]
        request_material = {
            "parent_task_hash": parent_key,
            "position": position,
            "item_ids": child_ids,
        }
        request_text = history_contract_v2.canonical_bytes(
            request_material
        ).decode("utf-8")
        request_sha = hashlib.sha256(request_text.encode("utf-8")).hexdigest()
        child_key = history_contract_v2.logical_task_key(
            parent["plan_sha"], parent["stage"],
            parent["staging_candidate_id"], request_sha,
        )
        records = {
            item["item_id"]: item for item in parent["frozen_records"]
        }
        return {
            "task_hash": child_key,
            "run_id": parent["run_id"],
            "stage": parent["stage"],
            "candidate_id": parent["staging_candidate_id"],
            "input_id": parent["input_id"] + f".{position}",
            "plan_sha": parent["plan_sha"],
            "snapshot_id": parent["snapshot_id"],
            "snapshot_hash": parent["snapshot_hash"],
            "request_sha": request_sha,
            "request_text": request_text,
            "item_ids": child_ids,
            "frozen_records": [records[item_id] for item_id in child_ids],
            "provider_pool": parent["provider_pool"],
            "split_depth": parent["split_depth"] + 1,
        }

    def _insert_canonical_split_child_without_transition(
        self, conn, plan, parent_key, position=0, *, preupgrade_direct=False,
        stored_input_id=None,
    ):
        child = self._canonical_split_child(
            conn, plan, parent_key, position
        )
        created_at = self._now()
        def insert_task():
            conn.execute(
                """
                INSERT INTO audit_logical_tasks(
                  task_hash, run_id, stage, staging_candidate_id, input_id,
                  state, fence, claim_token, lease_until, created_at
                ) VALUES(?, ?, ?, ?, ?, 'planned', 0, NULL, NULL, ?)
                """,
                (
                    child["task_hash"], child["run_id"], child["stage"],
                    child["candidate_id"], child["input_id"], created_at,
                ),
            )
        if preupgrade_direct:
            expected = (
                child["task_hash"], child["run_id"], child["stage"],
                child["candidate_id"], child["input_id"],
            )
            conn.create_function(
                "audit_l2_split_task_insert_allowed", 5,
                lambda *values: 1 if tuple(values) == expected else 0,
            )
            insert_task()
            conn.create_function(
                "audit_l2_split_task_insert_allowed", 5, lambda *_: 0
            )
        else:
            with history_audit_store.l2_split_task_insert_guard(
                conn,
                task_hash=child["task_hash"],
                run_id=child["run_id"],
                stage=child["stage"],
                candidate_id=child["candidate_id"],
                input_id=child["input_id"],
            ):
                insert_task()
        conn.execute(
            """
            INSERT INTO audit_task_bindings_v2(
              task_hash, plan_sha, snapshot_id, snapshot_hash,
              shard_input_sha, assigned_item_ids_json, frozen_records_json,
              provider_pool_json, parent_task_hash, split_depth, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                child["task_hash"], child["plan_sha"], child["snapshot_id"],
                child["snapshot_hash"], child["request_sha"],
                history_contract_v2.canonical_bytes(child["item_ids"]).decode(),
                history_contract_v2.canonical_bytes(
                    child["frozen_records"]
                ).decode(),
                history_contract_v2.canonical_bytes(
                    child["provider_pool"]
                ).decode(),
                parent_key, child["split_depth"], created_at,
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
                child["task_hash"], stored_input_id or child["input_id"],
                child["request_sha"],
                child["request_text"],
                history_contract_v2.canonical_bytes(child["item_ids"]).decode(),
                created_at,
            ),
        )
        return child

    def test_public_split_guard_cannot_insert_one_canonical_child_without_transition(self):
        plan = self._install()
        parent_key = plan["logical_task_keys"][0]
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(history_audit_store.AuditMigrationError):
                self._insert_canonical_split_child_without_transition(
                    self.conn, plan, parent_key
                )
        finally:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
        parent = self.conn.execute(
            "SELECT state FROM audit_logical_tasks WHERE task_hash=?",
            (parent_key,),
        ).fetchone()
        self.assertEqual(parent["state"], "planned")
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_task_bindings_v2 "
                "WHERE parent_task_hash=?", (parent_key,),
            ).fetchone()[0],
            0,
        )

    def test_upgrade_rejects_canonical_child_without_parent_terminal_or_edge(self):
        conn = sqlite3.connect(self.root / "forged-split-upgrade.sqlite3")
        conn.row_factory = sqlite3.Row
        split_authority_index = next(
            index for index, migration in enumerate(
                history_audit_store.MIGRATIONS
            )
            if migration.component == "l2-runtime-split-authority"
        )
        with mock.patch.object(
            history_audit_store, "MIGRATIONS",
            history_audit_store.MIGRATIONS[:split_authority_index],
        ):
            history_audit_store.init_schema(conn)
            self._persist_pre_route_plan(conn, self.plan)
            conn.execute("BEGIN IMMEDIATE")
            self._insert_canonical_split_child_without_transition(
                conn, self.plan, self.plan["logical_task_keys"][0],
                preupgrade_direct=True,
            )
            conn.execute("COMMIT")
        try:
            with self.assertRaises(history_audit_store.AuditMigrationError):
                history_audit_store.init_schema(conn)
        finally:
            conn.close()

    def test_upgrade_rejects_split_child_with_mismatched_stored_input_id(self):
        conn = sqlite3.connect(self.root / "forged-child-input-upgrade.sqlite3")
        conn.row_factory = sqlite3.Row
        split_authority_index = next(
            index for index, migration in enumerate(
                history_audit_store.MIGRATIONS
            )
            if migration.component == "l2-runtime-split-authority"
        )
        with mock.patch.object(
            history_audit_store, "MIGRATIONS",
            history_audit_store.MIGRATIONS[:split_authority_index],
        ):
            history_audit_store.init_schema(conn)
            self._persist_pre_route_plan(conn, self.plan)
            parent_key = self.plan["logical_task_keys"][0]
            conn.execute("DROP TRIGGER audit_l2_task_inputs_v2_guard")
            conn.execute(
                "DROP TRIGGER audit_l2_task_inputs_v2_full_authority_guard"
            )
            conn.execute("BEGIN IMMEDIATE")
            children = [
                self._insert_canonical_split_child_without_transition(
                    conn, self.plan, parent_key, position,
                    preupgrade_direct=True,
                    stored_input_id=(
                        "forged-child-input.0" if position == 0 else None
                    ),
                )
                for position in (0, 1)
            ]
            history_audit_store.compare_and_set_logical_task(
                conn, parent_key,
                expected_state="planned", expected_fence=0,
                new_state="superseded", new_fence=1,
            )
            terminal = {
                "task_hash": parent_key,
                "terminal_state": "superseded",
                "reason": "invalid_parent_split",
            }
            conn.execute(
                "INSERT INTO audit_task_terminal_facts_v2 VALUES(?, ?, ?, ?, ?)",
                (
                    parent_key, "superseded", "invalid_parent_split",
                    history_execution._sha(
                        "history-task-terminal-v2", terminal
                    ),
                    self._now(),
                ),
            )
            for position, child in enumerate(children):
                edge = {
                    "parent_task_hash": parent_key,
                    "child_task_hash": child["task_hash"],
                    "position": position,
                }
                conn.execute(
                    "INSERT INTO audit_task_edges_v2 VALUES(?, ?, ?, ?, ?)",
                    (
                        parent_key, child["task_hash"], position,
                        history_execution._sha("history-task-edge-v2", edge),
                        self._now(),
                    ),
                )
            conn.execute("COMMIT")
        try:
            with self.assertRaises(history_audit_store.AuditMigrationError):
                history_audit_store.init_schema(conn)
        finally:
            conn.close()

    def test_database_rejects_split_child_with_mismatched_stored_input_id(self):
        plan = self._install()
        parent_key = plan["logical_task_keys"][0]
        self.conn.execute("DROP TRIGGER audit_l2_task_inputs_v2_guard")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_canonical_split_child_without_transition(
                    self.conn, plan, parent_key,
                    preupgrade_direct=True,
                    stored_input_id="forged-child-input.0",
                )
        finally:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")

    def test_split_requires_live_claim_fence_and_token(self):
        plan = self._install()
        parent_key = plan["logical_task_keys"][0]
        with self.assertRaises(self._api("ExecutionError")) as caught:
            self._api("split_task")(self.conn, parent_key)
        self.assertEqual(caught.exception.code, "split_requires_live_claim")
        task = self._api("load_task")(self.conn, parent_key)
        self.assertEqual(task["state"], "planned")

    def test_generic_fenced_cas_cannot_forge_terminal_task_state(self):
        plan = self._install()
        parent_key = plan["logical_task_keys"][0]
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                history_audit_store.compare_and_set_logical_task(
                    self.conn,
                    parent_key,
                    expected_state="planned",
                    expected_fence=0,
                    new_state="superseded",
                    new_fence=1,
                )
        finally:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")

    def test_terminal_facts_and_split_edges_require_atomic_transition_authority(self):
        plan = self._install()
        parent_key = plan["logical_task_keys"][0]
        terminal = {
            "task_hash": parent_key,
            "terminal_state": "superseded",
            "reason": "invalid_parent_split",
        }
        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "terminal fact lacks transition authority"
        ):
            self.conn.execute(
                "INSERT INTO audit_task_terminal_facts_v2 VALUES(?, ?, ?, ?, ?)",
                (
                    parent_key, "superseded", "invalid_parent_split",
                    history_execution._sha(
                        "history-task-terminal-v2", terminal
                    ),
                    self._now(),
                ),
            )
        edge = {
            "parent_task_hash": parent_key,
            "child_task_hash": parent_key,
            "position": 0,
        }
        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "split edge lacks transition authority"
        ):
            self.conn.execute(
                "INSERT INTO audit_task_edges_v2 VALUES(?, ?, ?, ?, ?)",
                (
                    parent_key, parent_key, 0,
                    history_execution._sha("history-task-edge-v2", edge),
                    self._now(),
                ),
            )

    def test_split_rejects_expired_claim_even_with_exact_fence_and_token(self):
        plan = self._install()
        parent_key = plan["logical_task_keys"][0]
        claim = self._api("claim_task")(
            self.conn, parent_key, "worker-a", 1,
            expected_fence=0, now=self._now(),
        )
        with self.assertRaises(history_audit_store.StaleFence):
            self._api("split_task")(
                self.conn, parent_key,
                expected_fence=claim["fence"],
                claim_token=claim["claim_token"],
                now=self._now(2),
            )
        self.assertEqual(
            self._api("load_task")(self.conn, parent_key)["state"], "claimed"
        )

    def test_single_item_split_requires_verified_overflow_completion(self):
        one_item_plan = self._plan([self.records[0]])
        self._install(one_item_plan)
        parent_key = one_item_plan["logical_task_keys"][0]
        claim = self._api("claim_task")(
            self.conn, parent_key, "worker-a", 60,
            expected_fence=0, now=self._now(),
        )
        with self.assertRaises(self._api("ExecutionError")) as caught:
            self._api("split_task")(
                self.conn,
                parent_key,
                expected_fence=claim["fence"],
                claim_token=claim["claim_token"],
                now=self._now(),
            )
        self.assertEqual(caught.exception.code, "missing_overflow_evidence")
        self.assertEqual(
            self._api("load_task")(self.conn, parent_key)["state"], "claimed"
        )

    def test_recovery_rejects_orphan_canonical_split_child_graph(self):
        plan = self._install()
        parent_key = plan["logical_task_keys"][0]
        self.conn.execute(
            "DROP TRIGGER audit_logical_tasks_l2_insert_authority_guard_v2"
        )
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            child = self._canonical_split_child(
                self.conn, plan, parent_key, 0
            )
            created_at = self._now()
            self.conn.execute(
                """
                INSERT INTO audit_logical_tasks(
                  task_hash, run_id, stage, staging_candidate_id, input_id,
                  state, fence, claim_token, lease_until, created_at
                ) VALUES(?, ?, ?, ?, ?, 'planned', 0, NULL, NULL, ?)
                """,
                (
                    child["task_hash"], child["run_id"], child["stage"],
                    child["candidate_id"], child["input_id"], created_at,
                ),
            )
            self.conn.execute(
                """
                INSERT INTO audit_task_bindings_v2(
                  task_hash, plan_sha, snapshot_id, snapshot_hash,
                  shard_input_sha, assigned_item_ids_json, frozen_records_json,
                  provider_pool_json, parent_task_hash, split_depth, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    child["task_hash"], child["plan_sha"], child["snapshot_id"],
                    child["snapshot_hash"], child["request_sha"],
                    history_contract_v2.canonical_bytes(
                        child["item_ids"]
                    ).decode(),
                    history_contract_v2.canonical_bytes(
                        child["frozen_records"]
                    ).decode(),
                    history_contract_v2.canonical_bytes(
                        child["provider_pool"]
                    ).decode(),
                    parent_key, child["split_depth"], created_at,
                ),
            )
            self.conn.execute(
                """
                INSERT INTO audit_l2_task_inputs_v2(
                  task_hash, input_id, request_sha, request_text,
                  item_ids_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    child["task_hash"], child["input_id"], child["request_sha"],
                    child["request_text"],
                    history_contract_v2.canonical_bytes(
                        child["item_ids"]
                    ).decode(),
                    created_at,
                ),
            )
            self.conn.execute("COMMIT")
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        with self.assertRaises(self._api("ExecutionError")) as caught:
            self._api("recover_run")(
                self.conn, plan["plan_sha"],
                cas_root=self.cas_root, now=self._now(),
            )
        self.assertEqual(caught.exception.code, "malformed_l2_terminal_graph")

    def test_recovery_rejects_orphan_terminal_fact_on_planned_task(self):
        plan = self._install()
        parent_key = plan["logical_task_keys"][0]
        self.conn.execute(
            "DROP TRIGGER audit_task_terminal_facts_v2_authority_guard"
        )
        terminal = {
            "task_hash": parent_key,
            "terminal_state": "superseded",
            "reason": "invalid_parent_split",
        }
        self.conn.execute(
            "INSERT INTO audit_task_terminal_facts_v2 VALUES(?, ?, ?, ?, ?)",
            (
                parent_key, "superseded", "invalid_parent_split",
                history_execution._sha("history-task-terminal-v2", terminal),
                self._now(),
            ),
        )
        with self.assertRaises(self._api("ExecutionError")) as caught:
            self._api("recover_run")(
                self.conn, plan["plan_sha"],
                cas_root=self.cas_root, now=self._now(),
            )
        self.assertEqual(caught.exception.code, "malformed_l2_terminal_graph")

    def _legacy_l2_connection(
        self, *, through_authority=False, fact_kind="planned"
    ):
        path = self.root / f"legacy-{fact_kind}-{'authority' if through_authority else 'runtime'}.sqlite3"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        prefix = []
        for migration in history_audit_store.MIGRATIONS:
            prefix.append(migration)
            if migration.component == (
                "l2-runtime-authority" if through_authority else "l2-runtime"
            ):
                break
        with mock.patch.object(history_audit_store, "MIGRATIONS", tuple(prefix)):
            history_audit_store.init_schema(conn)
        run_id = "legacy-l2-run"
        plan_sha = sha("legacy-l2-plan")
        snapshot_id = sha("legacy-l2-snapshot-id")
        snapshot_hash = sha("legacy-l2-snapshot")
        task_hash = sha("legacy-l2-task")
        state = "settling" if fact_kind == "settlement" else (
            "claimed" if fact_kind in {"active", "attempt"} else "planned"
        )
        claim_token = "legacy-worker" if state in {"claimed", "settling"} else None
        lease_until = self._now(60) if claim_token else None
        conn.execute(
            "INSERT INTO audit_run_manifests VALUES(?, 'history-audit-manifest-v2', ?, '{}', ?)",
            (run_id, plan_sha, self._now()),
        )
        conn.execute(
            """
            INSERT INTO audit_snapshots(
              snapshot_id, snapshot_hash, history_as_of_watermark,
              current_batch_id_namespace, current_batch_ids_hash,
              exclusion_policy_sha, expected_asset_ids_hash, created_at,
              run_id, batch_id
            ) VALUES(?, ?, 0, 'history-v2-staging-v1', ?, ?, ?, ?, ?, 'legacy-batch')
            """,
            (snapshot_id, snapshot_hash, sha("batch-set"), sha("exclude"),
             sha("assets"), self._now(), run_id),
        )
        conn.execute(
            """
            INSERT INTO audit_logical_tasks(
              task_hash, run_id, stage, staging_candidate_id, input_id,
              state, fence, claim_token, lease_until, created_at
            ) VALUES(?, ?, 'map', 'legacy-candidate', 'legacy-input',
                     ?, 0, ?, ?, ?)
            """,
            (task_hash, run_id, state, claim_token, lease_until, self._now()),
        )
        conn.execute(
            """
            INSERT INTO audit_task_bindings_v2(
              task_hash, plan_sha, snapshot_id, snapshot_hash, shard_input_sha,
              assigned_item_ids_json, frozen_records_json, provider_pool_json,
              parent_task_hash, split_depth, created_at
            ) VALUES(?, ?, ?, ?, ?, '["asset-1"]', '[]', '["codex"]',
                     NULL, 0, ?)
            """,
            (task_hash, plan_sha, snapshot_id, snapshot_hash, sha("legacy-input"), self._now()),
        )
        conn.commit()
        if fact_kind in {"attempt", "settlement"}:
            request = history_execution.history_cas.put_object(
                conn, self.cas_root, b"legacy request", "attempt-transient-7d"
            )
            output = history_execution.history_cas.put_object(
                conn, self.cas_root, b"legacy output", "attempt-transient-7d"
            )
            attempt_id = sha("legacy-" + fact_kind + "-attempt")
            conn.execute(
                """
                INSERT INTO audit_task_attempts(
                  attempt_id, task_hash, ordinal, provenance_json,
                  request_cas_object_id, output_cas_object_id, state, created_at
                ) VALUES(?, ?, 0, '{}', ?, NULL, 'started', ?)
                """,
                (attempt_id, task_hash, request["object_id"], self._now()),
            )
            if fact_kind == "settlement":
                conn.execute(
                    """
                    INSERT INTO audit_attempt_completions_v2(
                      attempt_id, output_cas_object_id, outcome,
                      normalized_result_json, usage_json, completed_at
                    ) VALUES(?, ?, 'valid', '{}', '{}', ?)
                    """,
                    (attempt_id, output["object_id"], self._now()),
                )
                conn.execute(
                    """
                    INSERT INTO audit_task_settlements_v2(
                      task_hash, settlement_sha256, settlement_kind,
                      normalized_result_json, valid_attempt_ids_json,
                      valid_output_cas_ids_json, settled_at
                    ) VALUES(?, ?, 'equal', '{}', ?, ?, ?)
                    """,
                    (
                        task_hash, sha("legacy-settlement"),
                        json.dumps([attempt_id]), json.dumps([output["object_id"]]),
                        self._now(),
                    ),
                )
            conn.commit()
        return conn, task_hash, plan_sha

    def test_pre_authority_l2_facts_make_upgrade_fail_closed(self):
        for fact_kind in ("planned", "active", "attempt", "settlement"):
            with self.subTest(fact_kind=fact_kind):
                conn, _, _ = self._legacy_l2_connection(fact_kind=fact_kind)
                try:
                    with self.assertRaises(history_audit_store.AuditMigrationError):
                        history_audit_store.init_schema(conn)
                finally:
                    conn.close()

    def test_empty_pre_authority_l2_upgrade_applies_integrity_migration(self):
        conn = sqlite3.connect(self.root / "legacy-empty-runtime.sqlite3")
        conn.row_factory = sqlite3.Row
        prefix = []
        for migration in history_audit_store.MIGRATIONS:
            prefix.append(migration)
            if migration.component == "l2-runtime":
                break
        with mock.patch.object(history_audit_store, "MIGRATIONS", tuple(prefix)):
            history_audit_store.init_schema(conn)
        history_audit_store.init_schema(conn)
        applied = conn.execute(
            """
            SELECT 1 FROM audit_schema_migrations
            WHERE component='l2-runtime-integrity' AND version=1
            """
        ).fetchone()
        conn.close()
        self.assertIsNotNone(applied)

    def test_stranded_authority_is_integrity_error_for_load_and_recovery(self):
        conn, task_hash, plan_sha = self._legacy_l2_connection(through_authority=True)
        try:
            with self.assertRaises(self._api("ExecutionError")) as loaded:
                self._api("load_task")(conn, task_hash)
            self.assertEqual(loaded.exception.code, "stranded_l2_authority")
            with self.assertRaises(self._api("ExecutionError")) as recovered:
                self._api("recover_run")(
                    conn, plan_sha, cas_root=self.cas_root, now=self._now()
                )
            self.assertEqual(recovered.exception.code, "stranded_l2_authority")
        finally:
            conn.close()

    def test_anchor_authority_comes_only_from_durable_frozen_records(self):
        plan = self._install()
        task = self._api("load_task")(self.conn, plan["logical_task_keys"][0])
        replacement = copy.deepcopy(plan["snapshot"])
        replacement["records"][0]["content"] = "omega replacement"
        replacement["records"][0]["artifact_sha"] = sha("omega replacement")
        forged = self._output()
        forged["items"][0]["anchor"].update(
            artifact_sha=sha("omega replacement"),
            quote="omega",
        )
        with self.assertRaises(self._api("MapValidationError")) as caught:
            self._api("validate_map_output")(task, forged, replacement)
        self.assertEqual(caught.exception.code, "snapshot_authority_mismatch")

    def test_timeout_then_success_commits_one_logical_result(self):
        plan = self._install()

        def provider(task_key, provider_name, ordinal, request):
            if ordinal == 0:
                return {"kind": "timeout", "raw": "timeout"}
            return {"kind": "success", "output": self._output()}

        result = self._api("run_map_task")(
            self.conn, self.cas_root, plan, plan["logical_task_keys"][0], provider,
            now=self._now(),
        )
        self.assertEqual(result["settlement_kind"], "equal")
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM audit_task_attempts").fetchone()[0], 2
        )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM audit_task_settlements_v2").fetchone()[0], 1
        )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM audit_cas_objects").fetchone()[0], 3
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_cas_objects WHERE expires_at IS NOT NULL"
            ).fetchone()[0],
            3,
        )

    def test_durable_cost_facts_survive_reopen_without_double_counting(self):
        plan = self._install()

        def provider(*_):
            return {"kind": "success", "output": self._output()}

        self._api("run_map_task")(
            self.conn, self.cas_root, plan, plan["logical_task_keys"][0],
            provider, now=self._now(),
        )
        first = history_audit_eval_v2.summarize_realized_cost(
            self.conn, plan["run_id"]
        )
        realized = first["intents"]["duplicate_search"]["realized"]
        reservation = json.loads(
            self.conn.execute(
                "SELECT reserved_json FROM audit_runtime_budget_reservations_v2"
            ).fetchone()[0]
        )
        expected_reservation = {
            "input_tokens": len(
                plan["shards"][0]["serialized_request"].encode("utf-8")
            ),
            "output_tokens": plan["capacity_profile"]["max_output_tokens"],
        }
        expected_reservation["provider_usage_units"] = (
            expected_reservation["input_tokens"]
            + expected_reservation["output_tokens"]
        )
        self.assertEqual(reservation, expected_reservation)
        self.assertEqual(realized["calls"], 1)
        self.assertEqual(realized["input_tokens"], reservation["input_tokens"])
        self.assertEqual(realized["output_tokens"], reservation["output_tokens"])
        self.assertEqual(realized["cache_tokens"], 0)
        self.assertEqual(
            realized["provider_usage_units"],
            reservation["provider_usage_units"],
        )
        self.assertNotIn("currency_micros", realized)
        self.assertGreaterEqual(realized["queue_latency_ms"], 0)
        self.assertGreaterEqual(realized["run_latency_ms"], 0)
        self.assertTrue(first["intents"]["duplicate_search"]["latency_complete"])
        self.assertFalse(
            first["intents"]["duplicate_search"]["accounting_complete"]
        )
        settlement = self.conn.execute(
            """
            SELECT usage_verified, actual_json
            FROM audit_runtime_budget_settlements_v2
            """
        ).fetchone()
        self.assertEqual(tuple(settlement), (0, None))
        self.assertEqual(
            first["intents"]["duplicate_search"]["expected_per_candidate"]["calls"],
            1.0,
        )
        self.assertEqual(
            first["intents"]["duplicate_search"]["risk_slices"]
                ["low_overlap"]["candidate_count"],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_attempt_launch_facts_v2"
            ).fetchone()[0], 1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_attempt_cost_settlements_v2"
            ).fetchone()[0], 1,
        )
        self.conn.close()
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        history_audit_store.init_schema(self.conn)
        self._api("recover_run")(
            self.conn, plan["plan_sha"], cas_root=self.cas_root,
            now=self._now(120),
        )
        second = history_audit_eval_v2.summarize_realized_cost(
            self.conn, plan["run_id"]
        )
        self.assertEqual(second, first)
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_attempt_launch_facts_v2"
            ).fetchone()[0], 1,
        )

    def test_route_facts_include_zero_attempt_candidates_and_overlapping_slices(self):
        second = {
            "candidate_id": "stg-v2-" + sha("zero-attempt-candidate"),
            "candidate_hash": "",
            "raw_artifact_sha": sha("zero-attempt-candidate-raw"),
            "source_order": 1,
        }
        second["candidate_hash"] = history_audit_plan.runtime_candidate_hash(
            second
        )
        plan = self._plan(self.records, additional_candidates=[second])
        plan = self._install(
            plan,
            additional_candidates=[second],
            risk_slices_by_candidate={
                plan["candidate"]["candidate_id"]: [
                    "cross_language", "low_overlap",
                ],
                second["candidate_id"]: ["low_overlap"],
            },
        )
        before = history_audit_eval_v2.summarize_realized_cost(
            self.conn, plan["run_id"]
        )["intents"][plan["intent"]]
        self.assertEqual(before["candidate_count"], 2)
        self.assertEqual(before["realized"]["calls"], 0)
        self.assertEqual(before["escalated_candidate_count"], 1)
        self.assertEqual(before["escalation_rate"], 0.5)
        self._api("run_map_task")(
            self.conn, self.cas_root, plan, plan["logical_task_keys"][0],
            lambda *_: {
                "kind": "success", "output": self._output(plan),
            },
            now=self._now(2),
        )

        summary = history_audit_eval_v2.summarize_realized_cost(
            self.conn, plan["run_id"]
        )["intents"][plan["intent"]]
        attempt_rows = [
            tuple(row) for row in self.conn.execute(
                "SELECT attempt.ordinal, completion.outcome "
                "FROM audit_task_attempts attempt "
                "LEFT JOIN audit_attempt_completions_v2 completion "
                "USING(attempt_id) ORDER BY attempt.ordinal"
            )
        ]
        self.assertEqual(attempt_rows, [(0, "valid")])
        self.assertEqual(summary["candidate_count"], 2)
        self.assertEqual(summary["escalated_candidate_count"], 1)
        self.assertEqual(summary["escalation_rate"], 0.5)
        self.assertEqual(summary["realized"]["calls"], 1)
        self.assertEqual(summary["expected_per_candidate"]["calls"], 0.5)
        self.assertEqual(
            summary["expected_per_candidate"]["input_tokens"],
            summary["realized"]["input_tokens"] / 2,
        )
        self.assertEqual(
            summary["risk_slices"]["low_overlap"]["candidate_count"], 2
        )
        self.assertEqual(
            summary["risk_slices"]["cross_language"]["candidate_count"], 1
        )
        self.assertEqual(summary["providers"]["codex"]["realized"]["calls"], 1)

    def test_call_l1_without_durable_l1_attempt_makes_expected_unavailable(self):
        facts = self._router_facts(
            retriever_calibrated=True,
            finalist_or_sa=True,
            bad_slice_membership=False,
        )
        plan = self._plan(self.records, router_facts=facts)
        plan = self._install(
            plan,
            calibrated=True,
            comparator="certain",
            risk_slices_by_candidate={plan["candidate"]["candidate_id"]: []},
        )
        summary = history_audit_eval_v2.summarize_realized_cost(
            self.conn, plan["run_id"]
        )["intents"][plan["intent"]]
        self.assertIsNone(summary["expected_per_candidate"])
        self.assertEqual(
            summary["expected_unavailable_reason"],
            "durable_l1_attempt_facts_unavailable",
        )

    def test_route_facts_reject_direct_sql_mutation_and_conflicting_reissue(self):
        plan = self._install()
        self._api("persist_plan")(self.conn, plan)
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_candidate_route_facts_v2"
            ).fetchone()[0],
            1,
        )
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute(
                "UPDATE audit_candidate_route_facts_v2 SET route='routine'"
            )
        self.conn.rollback()
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("DELETE FROM audit_candidate_l2_dispatch_facts_v2")
        self.conn.rollback()
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute(
                "INSERT INTO audit_candidate_route_cohorts_v2 "
                "SELECT * FROM audit_candidate_route_cohorts_v2"
            )
        self.conn.rollback()
        with self.assertRaises(self._api("ExecutionError")) as caught:
            self._api("persist_plan")(
                self.conn, plan,
                route_authority=self._route_authority(
                    risk_slices=["cross_language", "low_overlap"]
                ),
            )
        self.assertEqual(caught.exception.code, "caller_route_authority_forbidden")
        self.conn.close()
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        history_audit_store.init_schema(self.conn)
        row = self.conn.execute(
            "SELECT route.call_l1_model, route.risk_slices_json, "
            "dispatch.plan_sha IS NOT NULL AS actual_l2_dispatch "
            "FROM audit_candidate_route_facts_v2 route "
            "LEFT JOIN audit_candidate_l2_dispatch_facts_v2 dispatch "
            "ON dispatch.route_fact_sha256=route.fact_sha256"
        ).fetchone()
        self.assertEqual(tuple(row), (0, '["low_overlap"]', 1))

    def test_route_facts_accept_frozen_candidate_staged_at_earlier_time(self):
        plan = self.plan
        material = history_audit_plan.build_runtime_plan_material(plan)
        snapshot = plan["snapshot"]
        self.conn.execute(
            "INSERT INTO audit_run_manifests VALUES(?,?,?,?,?)",
            (
                plan["run_id"], "history-audit-manifest-v2",
                plan["plan_sha"], history_execution._canonical(material),
                self._now(10),
            ),
        )
        self.conn.execute(
            "INSERT INTO audit_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                snapshot["snapshot_id"], snapshot["snapshot_hash"],
                snapshot["history_as_of_watermark"],
                snapshot["current_batch_id_namespace"],
                snapshot["current_batch_ids_hash"],
                snapshot["exclusion_policy_sha"],
                snapshot["expected_asset_ids_hash"], self._now(),
                plan["run_id"], plan["batch_id"],
            ),
        )
        self.conn.execute(
            "INSERT INTO audit_snapshot_batch_sets VALUES(?,?,?,?,?,?,?)",
            (
                snapshot["snapshot_id"], plan["run_id"], plan["batch_id"],
                snapshot["current_batch_ids_hash"],
                json.dumps(
                    snapshot["current_batch_ids"], sort_keys=True,
                    separators=(",", ":"),
                ),
                len(snapshot["current_batch_ids"]), self._now(),
            ),
        )
        candidate = plan["candidate"]
        history_audit_store.insert_authorized_batch_staging(
            self.conn,
            staging_candidate_id=candidate["candidate_id"],
            run_id=plan["run_id"],
            batch_id=plan["batch_id"],
            candidate_hash=candidate["candidate_hash"],
            raw_artifact_sha=candidate["raw_artifact_sha"],
            source_order=candidate["source_order"],
            created_at=self._now(),
        )
        self.conn.commit()
        self._install(plan)
        row = self.conn.execute(
            "SELECT staging.created_at, route.created_at "
            "FROM audit_batch_staging staging "
            "JOIN audit_candidate_route_facts_v2 route "
            "ON route.candidate_id=staging.staging_candidate_id"
        ).fetchone()
        self.assertEqual(tuple(row), (self._now(), self._now(10)))

    def test_queue_and_run_latency_are_derived_from_injected_timestamps(self):
        plan = self._install()
        task_key = plan["logical_task_keys"][0]
        ready_at = self.conn.execute(
            "SELECT created_at FROM audit_logical_tasks WHERE task_hash=?",
            (task_key,),
        ).fetchone()[0]
        ready = datetime.datetime.fromisoformat(ready_at)
        started_at = (ready + datetime.timedelta(seconds=10)).isoformat()
        completed_at = (ready + datetime.timedelta(seconds=14)).isoformat()
        self._api("claim_task")(
            self.conn, task_key, "worker", 60, 0, now=ready_at
        )
        attempt = self._api("record_attempt")(
            self.conn, task_key, plan["provider_capabilities"]["codex"],
            {"attempt_kind": "initial"}, cas_root=self.cas_root,
            request_bytes=plan["shards"][0]["serialized_request"].encode(),
            now=started_at,
        )
        self._api("complete_attempt")(
            self.conn, self.cas_root, task_key, attempt["attempt_id"],
            self._output(), plan["snapshot"],
            now=completed_at,
        )
        realized = history_audit_eval_v2.summarize_realized_cost(
            self.conn, plan["run_id"]
        )["intents"][plan["intent"]]["realized"]
        self.assertEqual(realized["queue_latency_ms"], 10000)
        self.assertEqual(realized["run_latency_ms"], 4000)

    def test_completion_rejects_empty_and_zero_caller_usage_authority(self):
        plan = self._install()
        task_key = plan["logical_task_keys"][0]
        self._api("claim_task")(
            self.conn, task_key, "worker", 60, 0, now=self._now()
        )
        attempt = self._api("record_attempt")(
            self.conn, task_key, plan["provider_capabilities"]["codex"],
            {"attempt_kind": "initial"}, cas_root=self.cas_root,
            request_bytes=plan["shards"][0]["serialized_request"].encode(),
        )
        for usage in (
            {},
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "provider_usage_units": 0,
            },
        ):
            with self.subTest(usage=usage):
                with self.assertRaises(self._api("ExecutionError")) as caught:
                    self._api("complete_attempt")(
                        self.conn, self.cas_root, task_key,
                        attempt["attempt_id"], self._output(),
                        plan["snapshot"], usage=usage,
                    )
                self.assertEqual(
                    caught.exception.code, "usage_authority_unavailable"
                )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_attempt_completions_v2"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_runtime_budget_settlements_v2"
            ).fetchone()[0],
            0,
        )
        started_at = self.conn.execute(
            "SELECT created_at FROM audit_task_attempts WHERE attempt_id=?",
            (attempt["attempt_id"],),
        ).fetchone()[0]
        completed_at = (
            datetime.datetime.fromisoformat(started_at)
            + datetime.timedelta(seconds=1)
        ).isoformat()
        self._api("complete_attempt")(
            self.conn, self.cas_root, task_key, attempt["attempt_id"],
            self._output(), plan["snapshot"],
            now=completed_at,
        )
        settlement = self.conn.execute(
            """
            SELECT budget.usage_verified, budget.actual_json,
                   budget.created_at, completion.completed_at,
                   completion.usage_json
            FROM audit_runtime_budget_settlements_v2 budget
            JOIN audit_attempt_completions_v2 completion USING(attempt_id)
            WHERE budget.attempt_id=?
            """,
            (attempt["attempt_id"],),
        ).fetchone()
        self.assertEqual(
            tuple(settlement),
            (
                0,
                None,
                completed_at,
                completed_at,
                history_contract_v2.canonical_bytes({}).decode(),
            ),
        )

    def test_database_rejects_unreceipted_verified_usage_settlements(self):
        plan = self._install()
        task_key = plan["logical_task_keys"][0]
        self._api("claim_task")(
            self.conn, task_key, "worker", 60, 0, now=self._now()
        )
        attempt = self._api("record_attempt")(
            self.conn, task_key, plan["provider_capabilities"]["codex"],
            {"attempt_kind": "initial"}, cas_root=self.cas_root,
            request_bytes=plan["shards"][0]["serialized_request"].encode(),
        )
        cases = (
            {},
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "provider_usage_units": 0,
            },
            {
                "input_tokens": 10,
                "output_tokens": 5,
                "provider_usage_units": 15,
            },
        )
        for actual in cases:
            with self.subTest(actual=actual):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.conn.execute(
                        """
                        INSERT INTO audit_runtime_budget_settlements_v2(
                          attempt_id, usage_verified, actual_json, created_at
                        ) VALUES(?, 1, ?, ?)
                        """,
                        (
                            attempt["attempt_id"],
                            history_contract_v2.canonical_bytes(actual).decode(),
                            self._now(),
                        ),
                    )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_runtime_budget_settlements_v2"
            ).fetchone()[0],
            0,
        )

    def test_direct_terminal_rows_require_host_one_shot_authority(self):
        plan = self._install()
        task_key = plan["logical_task_keys"][0]
        self._api("claim_task")(
            self.conn, task_key, "worker", 60, 0, now=self._now()
        )
        attempt = self._api("record_attempt")(
            self.conn, task_key, plan["provider_capabilities"]["codex"],
            {"attempt_kind": "initial"}, cas_root=self.cas_root,
            request_bytes=plan["shards"][0]["serialized_request"].encode(),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO audit_runtime_budget_settlements_v2(
                  attempt_id, usage_verified, actual_json, created_at
                ) VALUES(?, 0, NULL, ?)
                """,
                (attempt["attempt_id"], self._now()),
            )
        self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO audit_attempt_completions_v2(
                  attempt_id, output_cas_object_id, outcome,
                  normalized_result_json, usage_json, completed_at
                ) VALUES(?, ?, 'valid', '{}', ?, ?)
                """,
                (
                    attempt["attempt_id"], attempt["request_cas_object_id"],
                    history_contract_v2.canonical_bytes({}).decode(),
                    self._now(),
                ),
            )
        self.conn.rollback()
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_attempt_completions_v2"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_runtime_budget_settlements_v2"
            ).fetchone()[0],
            0,
        )

    def test_terminal_host_guards_clear_on_rollback_and_survive_reopen(self):
        plan = self._install()
        task_key = plan["logical_task_keys"][0]
        ready_at = self.conn.execute(
            "SELECT created_at FROM audit_logical_tasks WHERE task_hash=?",
            (task_key,),
        ).fetchone()[0]
        self._api("claim_task")(
            self.conn, task_key, "worker", 60, 0, now=ready_at
        )
        attempt = self._api("record_attempt")(
            self.conn, task_key, plan["provider_capabilities"]["codex"],
            {"attempt_kind": "initial"}, cas_root=self.cas_root,
            request_bytes=plan["shards"][0]["serialized_request"].encode(),
            now=ready_at,
        )
        completed_at = (
            datetime.datetime.fromisoformat(ready_at)
            + datetime.timedelta(seconds=1)
        ).isoformat()
        self.conn.execute("BEGIN IMMEDIATE")
        history_audit_store.insert_attempt_completion(
            self.conn, attempt["attempt_id"], attempt["request_cas_object_id"],
            "valid", history_contract_v2.canonical_bytes({}).decode(),
            completed_at=completed_at,
        )
        self.conn.rollback()
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM audit_attempt_completions_v2 WHERE attempt_id=?",
            (attempt["attempt_id"],),
        ).fetchone())

        self.conn.close()
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        history_audit_store.init_schema(self.conn)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO audit_attempt_completions_v2(
                  attempt_id, output_cas_object_id, outcome,
                  normalized_result_json, usage_json, completed_at
                ) VALUES(?, ?, 'valid', ?, ?, ?)
                """,
                (
                    attempt["attempt_id"], attempt["request_cas_object_id"],
                    history_contract_v2.canonical_bytes({}).decode(),
                    history_contract_v2.canonical_bytes({}).decode(),
                    completed_at,
                ),
            )
        self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO audit_runtime_budget_settlements_v2(
                  attempt_id, usage_verified, actual_json, created_at
                ) VALUES(?, 0, NULL, ?)
                """,
                (attempt["attempt_id"], completed_at),
            )
        self.conn.rollback()
        self._api("complete_attempt")(
            self.conn, self.cas_root, task_key, attempt["attempt_id"],
            self._output(), plan["snapshot"], now=completed_at,
        )

    def test_completion_and_cancellation_are_mutually_exclusive_both_orders(self):
        plan = self._install()
        task_key = plan["logical_task_keys"][0]
        ready_at = self.conn.execute(
            "SELECT created_at FROM audit_logical_tasks WHERE task_hash=?",
            (task_key,),
        ).fetchone()[0]
        self._api("claim_task")(
            self.conn, task_key, "worker", 60, 0, now=ready_at
        )
        attempt = self._api("record_attempt")(
            self.conn, task_key, plan["provider_capabilities"]["codex"],
            {"attempt_kind": "initial"}, cas_root=self.cas_root,
            request_bytes=plan["shards"][0]["serialized_request"].encode(),
            now=ready_at,
        )
        completed_at = (
            datetime.datetime.fromisoformat(ready_at)
            + datetime.timedelta(seconds=1)
        ).isoformat()
        normalized = history_contract_v2.canonical_bytes({}).decode()

        self.conn.execute("BEGIN IMMEDIATE")
        history_audit_store.insert_attempt_completion(
            self.conn, attempt["attempt_id"], attempt["request_cas_object_id"],
            "valid", normalized, completed_at=completed_at,
        )
        with self.assertRaises(history_audit_store.AuditMigrationError):
            history_audit_store.record_attempt_terminal_cost_fact(
                self.conn, attempt["attempt_id"], cancellation=True,
                completed_at=completed_at,
            )
        self.conn.rollback()

        self.conn.execute("BEGIN IMMEDIATE")
        history_audit_store.record_attempt_terminal_cost_fact(
            self.conn, attempt["attempt_id"], cancellation=True,
            completed_at=completed_at,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            history_audit_store.insert_attempt_completion(
                self.conn, attempt["attempt_id"],
                attempt["request_cas_object_id"], "valid", normalized,
                completed_at=completed_at,
            )
        self.conn.rollback()
        self.assertEqual(
            tuple(self.conn.execute(
                """
                SELECT
                  (SELECT count(*) FROM audit_attempt_completions_v2),
                  (SELECT count(*) FROM audit_attempt_cost_settlements_v2),
                  (SELECT count(*) FROM audit_runtime_budget_settlements_v2)
                """
            ).fetchone()),
            (0, 0, 0),
        )

    def test_committed_completion_rejects_cancel_without_mutating_terminal_rows(self):
        plan = self._install()
        task_key = plan["logical_task_keys"][0]
        ready_at = self.conn.execute(
            "SELECT created_at FROM audit_logical_tasks WHERE task_hash=?",
            (task_key,),
        ).fetchone()[0]
        completed_at = (
            datetime.datetime.fromisoformat(ready_at)
            + datetime.timedelta(seconds=1)
        ).isoformat()
        cancel_at = (
            datetime.datetime.fromisoformat(ready_at)
            + datetime.timedelta(seconds=2)
        ).isoformat()
        self._api("claim_task")(
            self.conn, task_key, "worker", 60, 0, now=ready_at
        )
        attempt = self._api("record_attempt")(
            self.conn, task_key, plan["provider_capabilities"]["codex"],
            {"attempt_kind": "initial"}, cas_root=self.cas_root,
            request_bytes=plan["shards"][0]["serialized_request"].encode(),
            now=ready_at,
        )
        self._api("complete_attempt")(
            self.conn, self.cas_root, task_key, attempt["attempt_id"],
            self._output(), plan["snapshot"], now=completed_at,
        )
        query = """
            SELECT completion.outcome, completion.completed_at,
                   budget.usage_verified, budget.actual_json,
                   budget.created_at, cost.outcome, cost.completed_at,
                   cost.fact_sha256
            FROM audit_attempt_completions_v2 completion
            JOIN audit_runtime_budget_settlements_v2 budget USING(attempt_id)
            JOIN audit_attempt_cost_settlements_v2 cost USING(attempt_id)
            WHERE completion.attempt_id=?
        """
        before = tuple(self.conn.execute(
            query, (attempt["attempt_id"],)
        ).fetchone())
        with self.assertRaises(history_audit_store.AuditMigrationError):
            self._api("cancel_attempt")(
                self.conn, attempt["attempt_id"], now=cancel_at
            )
        after = tuple(self.conn.execute(
            query, (attempt["attempt_id"],)
        ).fetchone())
        self.assertEqual(after, before)
        self.assertEqual(
            (after[0], after[2], after[3], after[5]),
            ("valid", 0, None, "success"),
        )

    def test_terminal_authority_upgrade_rejects_dirty_conflicting_and_orphan_rows(self):
        component = "attempt-terminal-authority"
        self.assertTrue(any(
            migration.component == component
            for migration in history_audit_store.MIGRATIONS
        ))
        for case in (
            "dirty_completion_usage",
            "completion_cancel_conflict",
            "orphan_budget_settlement",
        ):
            with self.subTest(case=case):
                legacy = sqlite3.connect(self.root / f"legacy-{case}.sqlite3")
                legacy.row_factory = sqlite3.Row
                target = next(
                    index for index, migration in enumerate(
                        history_audit_store.MIGRATIONS
                    )
                    if migration.component == component
                )
                old_migrations = history_audit_store.MIGRATIONS[:target]
                with mock.patch.object(
                    history_audit_store, "MIGRATIONS", old_migrations
                ):
                    history_audit_store.init_schema(legacy)
                primary_conn, primary_cas = self.conn, self.cas_root
                self.conn = legacy
                self.cas_root = self.root / f"legacy-{case}-cas"
                try:
                    plan = copy.deepcopy(self.plan)
                    self._persist_prefix_legacy_route_plan(legacy, plan)
                    task_key = plan["logical_task_keys"][0]
                    ready_at = legacy.execute(
                        "SELECT created_at FROM audit_logical_tasks "
                        "WHERE task_hash=?",
                        (task_key,),
                    ).fetchone()[0]
                    self._api("claim_task")(
                        legacy, task_key, "legacy-worker", 60, 0,
                        now=ready_at,
                    )
                    with mock.patch.object(
                        history_execution,
                        "_has_route_dispatch_authority",
                        return_value=True,
                    ):
                        attempt = self._api("record_attempt")(
                            legacy, task_key,
                            plan["provider_capabilities"]["codex"],
                            {"attempt_kind": "initial"},
                            cas_root=self.cas_root,
                            request_bytes=(
                                plan["shards"][0]["serialized_request"].encode()
                            ),
                            now=ready_at,
                        )
                    terminal_at = (
                        datetime.datetime.fromisoformat(ready_at)
                        + datetime.timedelta(seconds=1)
                    ).isoformat()
                    if case == "dirty_completion_usage":
                        legacy.execute(
                            """
                            INSERT INTO audit_attempt_completions_v2(
                              attempt_id, output_cas_object_id, outcome,
                              normalized_result_json, usage_json, completed_at
                            ) VALUES(?, ?, 'valid', '{}', ?, ?)
                            """,
                            (
                                attempt["attempt_id"],
                                attempt["request_cas_object_id"],
                                history_contract_v2.canonical_bytes(
                                    {"caller": "usage"}
                                ).decode(),
                                terminal_at,
                            ),
                        )
                    else:
                        legacy.execute(
                            """
                            INSERT INTO audit_runtime_budget_settlements_v2(
                              attempt_id, usage_verified, actual_json, created_at
                            ) VALUES(?, 0, NULL, ?)
                            """,
                            (attempt["attempt_id"], terminal_at),
                        )
                        if case == "completion_cancel_conflict":
                            legacy.execute(
                                "CREATE TEMP TABLE "
                                "audit_verified_usage_authorities_v2("
                                "attempt_id TEXT)"
                            )
                            try:
                                history_audit_store.record_attempt_terminal_cost_fact(
                                    legacy, attempt["attempt_id"],
                                    completed_at=terminal_at,
                                    cancellation=True,
                                )
                            finally:
                                legacy.execute(
                                    "DROP TABLE temp."
                                    "audit_verified_usage_authorities_v2"
                                )
                            legacy.execute(
                                """
                                INSERT INTO audit_attempt_completions_v2(
                                  attempt_id, output_cas_object_id, outcome,
                                  normalized_result_json, usage_json,
                                  completed_at
                                ) VALUES(?, ?, 'valid', '{}', ?, ?)
                                """,
                                (
                                    attempt["attempt_id"],
                                    attempt["request_cas_object_id"],
                                    history_contract_v2.canonical_bytes(
                                        {}
                                    ).decode(),
                                    terminal_at,
                                ),
                            )
                    legacy.commit()
                    with self.assertRaises(
                        history_audit_store.AuditMigrationError
                    ):
                        history_audit_store.init_schema(legacy)
                    self.assertIsNone(legacy.execute(
                        """
                        SELECT 1 FROM audit_schema_migrations
                        WHERE component=?
                        """,
                        (component,),
                    ).fetchone())
                finally:
                    self.conn, self.cas_root = primary_conn, primary_cas
                    legacy.close()

    def test_usage_authority_upgrade_rejects_legacy_verified_actual_atomically(self):
        component = "runtime-usage-authority"
        self.assertTrue(any(
            migration.component == component
            for migration in history_audit_store.MIGRATIONS
        ))
        legacy_path = self.root / "legacy-verified-usage.sqlite3"
        legacy = sqlite3.connect(legacy_path)
        legacy.row_factory = sqlite3.Row
        target = next(
            index for index, migration in enumerate(
                history_audit_store.MIGRATIONS
            )
            if migration.component == component
        )
        old_migrations = history_audit_store.MIGRATIONS[:target]
        def legacy_usage_valid(usage_verified, actual_json):
            return int(
                (usage_verified == 0 and actual_json is None)
                or (usage_verified == 1 and actual_json is not None)
            )

        with mock.patch.object(
            history_audit_store, "MIGRATIONS", old_migrations
        ), mock.patch.object(
            history_audit_store,
            "_l2_budget_settlement_valid",
            legacy_usage_valid,
        ):
            history_audit_store.init_schema(legacy)
        primary_conn, primary_cas = self.conn, self.cas_root
        self.conn = legacy
        self.cas_root = self.root / "legacy-verified-usage-cas"
        try:
            plan = copy.deepcopy(self.plan)
            self._persist_prefix_legacy_route_plan(legacy, plan)
            task_key = plan["logical_task_keys"][0]
            self._api("claim_task")(
                legacy, task_key, "legacy-worker", 60, 0, now=self._now()
            )
            with mock.patch.object(
                history_execution,
                "_has_route_dispatch_authority",
                return_value=True,
            ):
                attempt = self._api("record_attempt")(
                    legacy, task_key,
                    plan["provider_capabilities"]["codex"],
                    {"attempt_kind": "initial"}, cas_root=self.cas_root,
                    request_bytes=(
                        plan["shards"][0]["serialized_request"].encode()
                    ),
                )
            actual = {
                "input_tokens": 0,
                "output_tokens": 0,
                "provider_usage_units": 0,
            }
            legacy.execute(
                """
                INSERT INTO audit_runtime_budget_settlements_v2(
                  attempt_id, usage_verified, actual_json, created_at
                ) VALUES(?, 1, ?, ?)
                """,
                (
                    attempt["attempt_id"],
                    history_contract_v2.canonical_bytes(actual).decode(),
                    self._now(),
                ),
            )
            legacy.commit()
            task = self._api("load_task")(legacy, task_key)
            with self.assertRaises(self._api("ExecutionError")) as budget:
                history_execution._effective_budget_totals(
                    legacy, task, candidate_only=False
                )
            self.assertEqual(
                budget.exception.code, "usage_authority_unavailable"
            )
            with self.assertRaises(history_audit_store.AuditMigrationError):
                history_audit_store.init_schema(legacy)
            self.assertIsNone(legacy.execute(
                """
                SELECT 1 FROM audit_schema_migrations
                WHERE component=?
                """,
                (component,),
            ).fetchone())
            stored = legacy.execute(
                """
                SELECT usage_verified, actual_json
                FROM audit_runtime_budget_settlements_v2
                WHERE attempt_id=?
                """,
                (attempt["attempt_id"],),
            ).fetchone()
            self.assertEqual(stored["usage_verified"], 1)
            self.assertIsNotNone(stored["actual_json"])
        finally:
            self.conn, self.cas_root = primary_conn, primary_cas
            legacy.close()

    def test_run_task_rejects_provider_shaped_usage_before_completion(self):
        plan = self._install()
        with self.assertRaises(self._api("ExecutionError")) as caught:
            self._api("run_map_task")(
                self.conn, self.cas_root, plan,
                plan["logical_task_keys"][0],
                lambda *_: {
                    "kind": "success",
                    "output": self._output(),
                    "usage": {},
                },
                now=self._now(),
            )
        self.assertEqual(caught.exception.code, "usage_authority_unavailable")
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_attempt_completions_v2"
            ).fetchone()[0],
            0,
        )

    def test_cost_fact_tables_reject_direct_writes_and_mutation(self):
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute(
                "INSERT INTO audit_attempt_launch_facts_v2 VALUES(?,?,?,?,?)",
                ("f" * 64, self._now(), None, "e" * 64, self._now()),
            )
        self.conn.rollback()
        plan = self._install()
        self._api("claim_task")(
            self.conn, plan["logical_task_keys"][0], "worker", 60, 0,
            now=self._now(),
        )
        attempt = self._api("record_attempt")(
            self.conn, plan["logical_task_keys"][0],
            plan["provider_capabilities"]["codex"],
            {"attempt_kind": "initial"}, cas_root=self.cas_root,
            request_bytes=plan["shards"][0]["serialized_request"].encode(),
            now=self.conn.execute(
                "SELECT created_at FROM audit_logical_tasks WHERE task_hash=?",
                (plan["logical_task_keys"][0],),
            ).fetchone()[0],
        )
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute(
                """
                INSERT INTO audit_attempt_cost_settlements_v2(
                  attempt_id, outcome, error_class, billing_state,
                  usage_source, price_source, currency, run_latency_ms,
                  fact_sha256, completed_at
                ) VALUES(?, 'cancelled', 'cancelled', 'unknown',
                         'reservation', NULL, NULL, 0, ?, ?)
                """,
                (attempt["attempt_id"], "e" * 64, self._now(1)),
            )
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute(
                "UPDATE audit_attempt_launch_facts_v2 SET queued_at=?",
                (self._now(1),),
            )
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("DELETE FROM audit_attempt_launch_facts_v2")

    def test_planned_task_arbitrary_attempt_cannot_gain_cost_authority(self):
        plan = self._install()
        task_key = plan["logical_task_keys"][0]
        task = self._api("load_task")(self.conn, task_key)
        request = history_execution.history_cas.put_object(
            self.conn, self.cas_root,
            plan["shards"][0]["serialized_request"].encode(),
            "attempt-transient-7d",
        )
        provenance = {
            **copy.deepcopy(plan["provider_capabilities"]["codex"]),
            "attempt_kind": "initial", "ordinal": 0,
            "claim_token": "forged", "claim_fence": 999,
        }
        attempt_id = "f" * 64
        reserved = history_execution._derived_reservation(
            task, plan["shards"][0]["serialized_request"].encode()
        )
        self.conn.execute(
            "INSERT INTO audit_runtime_budget_reservations_v2 VALUES(?,?,?,?,?,?,?,?)",
            (
                attempt_id, task_key, plan["plan_sha"],
                plan["candidate"]["candidate_id"], plan["intent"], "initial",
                history_execution._canonical(reserved), self._now(),
            ),
        )
        self.conn.execute(
            "INSERT INTO audit_task_attempts VALUES(?,?,?,?,?,NULL,'started',?)",
            (
                attempt_id, task_key, 0,
                history_execution._canonical(provenance),
                request["object_id"], self._now(),
            ),
        )
        self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(history_audit_store.AuditMigrationError):
                history_audit_store.record_attempt_launch_cost_fact(
                    self.conn, attempt_id, queued_at=self._now()
                )
        finally:
            self.conn.execute("ROLLBACK")
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_attempt_launch_facts_v2"
            ).fetchone()[0], 0,
        )
        with self.assertRaisesRegex(ValueError, "launch parity"):
            history_audit_eval_v2.summarize_realized_cost(
                self.conn, plan["run_id"]
            )

    def test_nonempty_legacy_attempt_upgrade_is_quarantined_not_fabricated(self):
        legacy_path = self.root / "legacy-cost.sqlite3"
        legacy = sqlite3.connect(legacy_path)
        legacy.row_factory = sqlite3.Row
        migrations = history_audit_store.MIGRATIONS
        target = next(
            index for index, migration in enumerate(migrations)
            if migration.component == "durable-cost-facts"
        )
        with mock.patch.object(
            history_audit_store, "MIGRATIONS", migrations[:target]
        ):
            history_audit_store.init_schema(legacy)
        plan = self._plan(self.records)
        self._persist_pre_route_plan(legacy, plan)
        history_execution.claim_task(
            legacy, plan["logical_task_keys"][0], "legacy-worker", 60, 0,
            now=self._now(),
        )
        with mock.patch.object(
            history_execution, "_has_route_dispatch_authority",
            return_value=True,
        ), mock.patch.object(
            history_audit_store, "record_attempt_launch_cost_fact",
            return_value="legacy-unaccounted",
        ):
            attempt = history_execution.record_attempt(
                legacy, plan["logical_task_keys"][0],
                plan["provider_capabilities"]["codex"],
                {"attempt_kind": "initial"},
                cas_root=self.root / "legacy-cas",
                request_bytes=plan["shards"][0]["serialized_request"].encode(),
            )
        legacy.close()
        legacy = sqlite3.connect(legacy_path)
        legacy.row_factory = sqlite3.Row
        with mock.patch.object(
            history_audit_store, "MIGRATIONS",
            self._without_candidate_budget_migrations(),
        ):
            history_audit_store.init_schema(legacy)
        quarantined = legacy.execute(
            "SELECT reason FROM audit_legacy_unaccounted_attempts_v2 "
            "WHERE attempt_id=?",
            (attempt["attempt_id"],),
        ).fetchone()
        self.assertEqual(quarantined[0], "pre_durable_cost_facts")
        with self.assertRaisesRegex(ValueError, "launch parity"):
            history_audit_eval_v2.summarize_realized_cost(
                legacy, plan["run_id"]
            )
        legacy.close()

    def test_legacy_plan_without_route_facts_is_explicitly_unavailable(self):
        legacy_path = self.root / "legacy-route.sqlite3"
        legacy = sqlite3.connect(legacy_path)
        legacy.row_factory = sqlite3.Row
        migrations = history_audit_store.MIGRATIONS
        target = next(
            index for index, migration in enumerate(migrations)
            if migration.component == "candidate-route-facts"
        )
        with mock.patch.object(
            history_audit_store, "MIGRATIONS", migrations[:target]
        ):
            history_audit_store.init_schema(legacy)
            plan = self._plan(self.records)
            self._persist_pre_route_plan(legacy, plan)
        legacy.close()
        legacy = sqlite3.connect(legacy_path)
        legacy.row_factory = sqlite3.Row
        with mock.patch.object(
            history_audit_store, "MIGRATIONS",
            self._without_candidate_budget_migrations(),
        ):
            history_audit_store.init_schema(legacy)
        summary = history_audit_eval_v2.summarize_realized_cost(
            legacy, plan["run_id"]
        )["intents"][plan["intent"]]
        self.assertFalse(summary["route_facts_complete"])
        self.assertIsNone(summary["expected_per_candidate"])
        self.assertEqual(
            summary["expected_unavailable_reason"],
            "candidate_route_facts_unavailable",
        )
        self.assertIsNone(summary["risk_slices"])
        self.assertEqual(
            summary["risk_slices_unavailable_reason"],
            "candidate_route_facts_unavailable",
        )
        legacy.close()

    def test_pre_observation_dispatch_cannot_launch_new_attempt_after_upgrade(self):
        legacy_path = self.root / "legacy-route-observation.sqlite3"
        legacy = sqlite3.connect(legacy_path)
        legacy.row_factory = sqlite3.Row
        migrations = history_audit_store.MIGRATIONS
        target = next(
            index for index, migration in enumerate(migrations)
            if migration.component == "candidate-route-observation-boundary"
        )
        with mock.patch.object(
            history_audit_store, "MIGRATIONS", migrations[:target]
        ):
            history_audit_store.init_schema(legacy)
            legacy.execute(
                """
                CREATE TABLE audit_candidate_route_observation_boundaries_v2(
                  run_id TEXT, candidate_id TEXT, route_fact_sha256 TEXT,
                  observation_scope TEXT, production_authority INTEGER,
                  boundary_sha256 TEXT, created_at TEXT
                )
                """
            )
            plan = self._plan(self.records)
            with mock.patch.object(
                history_audit_store,
                "issue_candidate_budget_receipt",
                return_value={
                    "decision": "accepted", "decided_at": self._now()
                },
            ), mock.patch.object(
                history_audit_store,
                "_accepted_candidate_budget_receipt_matches",
                return_value=True,
            ):
                self._persist_prefix_legacy_route_plan(legacy, plan)
            legacy.execute(
                "DROP TABLE audit_candidate_route_observation_boundaries_v2"
            )
            legacy.commit()
        legacy.close()
        legacy = sqlite3.connect(legacy_path)
        legacy.row_factory = sqlite3.Row
        with mock.patch.object(
            history_audit_store, "MIGRATIONS",
            self._without_candidate_budget_migrations(),
        ):
            history_audit_store.init_schema(legacy)
        summary = history_audit_eval_v2.summarize_realized_cost(
            legacy, plan["run_id"]
        )["intents"][plan["intent"]]
        self.assertFalse(summary["route_facts_complete"])
        self.assertEqual(
            summary["route_observation_unavailable_reason"],
            "candidate_route_observation_boundary_unavailable",
        )
        task_key = plan["logical_task_keys"][0]
        history_execution.claim_task(
            legacy, task_key, "legacy-worker", 60, 0, now=self._now()
        )
        with self.assertRaises(history_execution.ExecutionError) as caught:
            history_execution.record_attempt(
                legacy, task_key, plan["provider_capabilities"]["codex"],
                {"attempt_kind": "initial"},
                cas_root=self.root / "legacy-route-observation-cas",
                request_bytes=plan["shards"][0]["serialized_request"].encode(),
            )
        self.assertEqual(caught.exception.code, "missing_route_dispatch_authority")
        legacy.close()

    def test_cancel_attempt_is_exact_once_and_keeps_unknown_currency_out(self):
        plan = self._install()
        task_key = plan["logical_task_keys"][0]
        self._api("claim_task")(
            self.conn, task_key, "worker", 60, 0, now=self._now()
        )
        ready_at = self.conn.execute(
            "SELECT created_at FROM audit_logical_tasks WHERE task_hash=?",
            (task_key,),
        ).fetchone()[0]
        cancel_at = (
            datetime.datetime.fromisoformat(ready_at)
            + datetime.timedelta(seconds=1)
        ).isoformat()
        attempt = self._api("record_attempt")(
            self.conn, task_key, plan["provider_capabilities"]["codex"],
            {"attempt_kind": "reduce"}, cas_root=self.cas_root,
            request_bytes=plan["shards"][0]["serialized_request"].encode(),
            now=ready_at,
        )
        with self.assertRaises(self._api("ExecutionError")):
            self._api("cancel_attempt")(
                self.conn, attempt["attempt_id"], billing_state="billable",
                now=cancel_at,
            )
        for usage in (
            {},
            {
                "input_tokens": 0, "output_tokens": 0,
                "provider_usage_units": 0,
            },
        ):
            with self.subTest(usage=usage):
                with self.assertRaises(self._api("ExecutionError")) as caught:
                    self._api("cancel_attempt")(
                        self.conn, attempt["attempt_id"],
                        billing_state="unknown", usage=usage,
                        now=cancel_at,
                    )
                self.assertEqual(
                    caught.exception.code, "usage_authority_unavailable"
                )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_runtime_budget_settlements_v2"
            ).fetchone()[0],
            0,
        )
        for _ in range(2):
            self._api("cancel_attempt")(
                self.conn, attempt["attempt_id"], billing_state="unknown",
                now=cancel_at,
            )
        settlement = self.conn.execute(
            """
            SELECT budget.usage_verified, budget.actual_json,
                   budget.created_at, cost.completed_at,
                   completion.attempt_id AS completion_attempt_id
            FROM audit_runtime_budget_settlements_v2 budget
            JOIN audit_attempt_cost_settlements_v2 cost USING(attempt_id)
            LEFT JOIN audit_attempt_completions_v2 completion USING(attempt_id)
            WHERE budget.attempt_id=?
            """,
            (attempt["attempt_id"],),
        ).fetchone()
        self.assertEqual(
            tuple(settlement),
            (0, None, cancel_at, cancel_at, None),
        )
        summary = history_audit_eval_v2.summarize_realized_cost(
            self.conn, plan["run_id"]
        )["intents"]["duplicate_search"]
        self.assertEqual(summary["realized"]["calls"], 1)
        self.assertEqual(summary["realized"]["billable_cancelled_calls"], 0)
        self.assertNotIn("currency_micros", summary["realized"])
        self.assertFalse(summary["accounting_complete"])
        provenance = json.loads(
            self.conn.execute(
                "SELECT provenance_json FROM audit_task_attempts WHERE attempt_id=?",
                (attempt["attempt_id"],),
            ).fetchone()[0]
        )
        self.assertEqual(provenance["attempt_kind"], "initial")

    def test_429_or_5xx_fails_over_in_declared_pool_order(self):
        for failure in ("429", "5xx"):
            with self.subTest(failure=failure):
                self.tearDown()
                self.setUp()
                plan = self._install()

                def provider(task_key, provider_name, ordinal, request):
                    if ordinal == 0:
                        return {"kind": failure, "raw": failure}
                    return {"kind": "success", "output": self._output()}

                self._api("run_map_task")(
                    self.conn, self.cas_root, plan, plan["logical_task_keys"][0], provider,
                    now=self._now(),
                )
                providers = [
                    json.loads(row[0])["provider"]
                    for row in self.conn.execute(
                        "SELECT provenance_json FROM audit_task_attempts ORDER BY ordinal"
                    )
                ]
                self.assertEqual(providers, ["codex", "grok"])
                cost = history_audit_eval_v2.summarize_realized_cost(
                    self.conn, plan["run_id"]
                )["intents"][plan["intent"]]
                self.assertEqual(cost["realized"]["calls"], 2)
                self.assertEqual(cost["realized"]["failover_calls"], 1)
                self.assertEqual(cost["providers"]["codex"]["realized"]["calls"], 1)
                self.assertEqual(cost["providers"]["grok"]["realized"]["calls"], 1)

    def test_syntax_retry_cost_stays_on_initial_provider(self):
        plan = self._install()

        def provider(_task_key, _provider_name, ordinal, _request):
            if ordinal == 0:
                return {"kind": "syntax", "raw": "syntax"}
            return {"kind": "success", "output": self._output()}

        self._api("run_map_task")(
            self.conn, self.cas_root, plan, plan["logical_task_keys"][0],
            provider, now=self._now(),
        )
        cost = history_audit_eval_v2.summarize_realized_cost(
            self.conn, plan["run_id"]
        )["intents"][plan["intent"]]
        self.assertEqual(cost["realized"]["calls"], 2)
        self.assertEqual(cost["realized"]["retry_calls"], 1)
        self.assertEqual(cost["realized"]["failover_calls"], 0)
        self.assertEqual(cost["providers"]["codex"]["realized"]["calls"], 2)

    def test_equal_duplicate_completions_are_arrival_order_independent(self):
        output = self._output()
        first = self._api("settlement_decision")(
            [{"attempt_id": sha("a"), "normalized": output}, {"attempt_id": sha("b"), "normalized": copy.deepcopy(output)}]
        )
        second = self._api("settlement_decision")(
            [{"attempt_id": sha("b"), "normalized": copy.deepcopy(output)}, {"attempt_id": sha("a"), "normalized": output}]
        )
        self.assertEqual(first, second)
        self.assertEqual(first["settlement_kind"], "equal")
        self.assertEqual(first["valid_attempt_ids"], sorted([sha("a"), sha("b")]))

    def test_conflicting_valid_completions_are_arrival_order_independent(self):
        left = self._output(relations={"asset-1": "distinct"})
        right = self._output(relations={"asset-1": "blocking_duplicate"})
        forward = self._api("settlement_decision")(
            [{"attempt_id": sha("a"), "normalized": left}, {"attempt_id": sha("b"), "normalized": right}]
        )
        reverse = self._api("settlement_decision")(
            [{"attempt_id": sha("b"), "normalized": right}, {"attempt_id": sha("a"), "normalized": left}]
        )
        self.assertEqual(forward, reverse)
        self.assertEqual(forward["settlement_kind"], "conflict")
        self.assertIsNone(forward["normalized_result"])

    def test_overflow_supersedes_parent_and_splits_not_retries(self):
        plan = self._install()

        def provider(*_):
            return {"kind": "overflow", "raw": "overflow"}

        result = self._api("run_map_task")(
            self.conn, self.cas_root, plan, plan["logical_task_keys"][0], provider,
            now=self._now(),
        )
        self.assertEqual(result["state"], "superseded")
        self.assertEqual([child["position"] for child in result["children"]], [0, 1])
        self.assertTrue(all(child["item_ids"] for child in result["children"]))
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM audit_task_attempts").fetchone()[0], 1
        )
        replay = self._api("split_task")(self.conn, plan["logical_task_keys"][0])
        self.assertEqual(replay, result)
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_l2_terminal_transition_authority_v2"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self._api("recover_run")(
                self.conn, plan["plan_sha"],
                cas_root=self.cas_root, now=self._now(),
            ),
            [],
        )
        child = result["children"][0]
        self._api("run_map_task")(
            self.conn, self.cas_root, plan, child["task_hash"],
            lambda *_: {
                "kind": "success",
                "output": self._output(plan, item_ids=child["item_ids"]),
            },
            now=self._now(1),
        )
        cost = history_audit_eval_v2.summarize_realized_cost(
            self.conn, plan["run_id"]
        )["intents"][plan["intent"]]
        self.assertEqual(cost["realized"]["calls"], 2)
        self.assertEqual(cost["realized"]["split_calls"], 1)
        self.assertEqual(cost["attempt_kind_availability"]["detail"], "producer_unavailable")
        self.assertEqual(cost["attempt_kind_availability"]["reduce"], "producer_unavailable")

    def test_single_item_overflow_exhausts_without_empty_children(self):
        plan = self._plan([self.records[0]])
        self._install(plan)

        def provider(*_):
            return {"kind": "overflow", "raw": "overflow"}

        result = self._api("run_map_task")(
            self.conn, self.cas_root, plan, plan["logical_task_keys"][0], provider,
            now=self._now(),
        )
        self.assertEqual(result["state"], "exhausted")
        self.assertEqual(result["children"], [])
        self.assertEqual(
            self._api("split_task")(
                self.conn, plan["logical_task_keys"][0]
            ),
            result,
        )
        state = self._api("load_adjudication_state")(
            self.conn, plan["plan_sha"]
        )
        self.assertTrue(state["generation_present"])
        self.assertEqual(state["required_detail_count"], 0)

    def test_invalid_parent_faults_remain_visible_until_split_recovers(self):
        invalid_schema = self._output()
        invalid_schema["items"][0]["unexpected"] = True
        invalid_anchor = self._output()
        invalid_anchor["items"][0]["anchor"]["quote"] = "wrong"
        cases = {
            # Durable item-set evidence intentionally does not distinguish these
            # three source shapes; the invalid parent contributes no observed IDs.
            "missing": (
                self._output(item_ids=["asset-1"]),
                "superseded", ["item_set"], {},
            ),
            "duplicate": (
                self._output(item_ids=["asset-1", "asset-1"]),
                "superseded", ["item_set"], {},
            ),
            "extra": (
                self._output(item_ids=["asset-1", "asset-2", "asset-x"]),
                "superseded", ["item_set"], {},
            ),
            "truncated": (
                self._output(truncated=True),
                "superseded", ["truncated"], {"truncated": True},
            ),
            "invalid_schema": (
                invalid_schema,
                "exhausted", ["schema", "schema"], {"invalid_schema": True},
            ),
            "invalid_anchor": (
                invalid_anchor,
                "exhausted", ["invalid_anchor"], {"invalid_anchor": True},
            ),
        }
        for name, (output, state, outcomes, expected_faults) in cases.items():
            with self.subTest(name=name):
                self.tearDown()
                self.setUp()
                plan = self._install()

                def provider(*_):
                    return {"kind": "success", "output": output}

                result = self._api("run_map_task")(
                    self.conn, self.cas_root, plan, plan["logical_task_keys"][0], provider,
                    now=self._now(),
                )
                self.assertEqual(result["state"], state)
                terminal = self._api("load_terminal_states")(
                    self.conn, plan["plan_sha"]
                )
                parent = next(
                    item for item in terminal
                    if item["task_hash"] == plan["logical_task_keys"][0]
                )
                self.assertEqual(parent["attempt_outcomes"], outcomes)
                coverage = self._api("build_coverage_receipt")(
                    plan, terminal,
                    {"qualified": False, "profile_id": "semantic-test-v1"},
                )
                self.assertEqual(coverage["observed_ids"], [])
                self.assertEqual(coverage["missing_ids"], ["asset-1", "asset-2"])
                self.assertEqual(coverage["duplicate_ids"], [])
                self.assertEqual(coverage["extra_ids"], [])
                self.assertFalse(coverage["coverage_complete"])
                for field in ("invalid_schema", "invalid_anchor", "truncated"):
                    self.assertEqual(
                        coverage[field], expected_faults.get(field, False), field
                    )

    def test_valid_split_children_replace_invalid_parent_coverage_and_faults(self):
        cases = {
            "missing": self._output(item_ids=["asset-1"]),
            "duplicate": self._output(item_ids=["asset-1", "asset-1"]),
            "extra": self._output(item_ids=["asset-1", "asset-2", "asset-x"]),
            "truncated": self._output(truncated=True),
        }
        for name, output in cases.items():
            with self.subTest(name=name):
                self.tearDown()
                self.setUp()
                plan = self._install()
                result = self._api("run_map_task")(
                    self.conn, self.cas_root, plan,
                    plan["logical_task_keys"][0],
                    lambda *_: {"kind": "success", "output": output},
                    now=self._now(),
                )
                self.assertEqual(result["state"], "superseded")
                for index, child in enumerate(result["children"], 1):
                    self._api("run_map_task")(
                        self.conn, self.cas_root, plan, child["task_hash"],
                        lambda *_, child=child: {
                            "kind": "success",
                            "output": self._output(
                                plan,
                                item_ids=child["item_ids"],
                                relations={
                                    item_id: "related_only"
                                    for item_id in child["item_ids"]
                                },
                            ),
                        },
                        now=self._now(index),
                    )
                coverage = self._api("build_coverage_receipt")(
                    plan,
                    self._api("load_terminal_states")(
                        self.conn, plan["plan_sha"]
                    ),
                    {"qualified": False, "profile_id": "semantic-test-v1"},
                    conn=self.conn,
                )
                self.assertEqual(coverage["observed_ids"], ["asset-1", "asset-2"])
                self.assertEqual(coverage["missing_ids"], [])
                self.assertEqual(coverage["duplicate_ids"], [])
                self.assertEqual(coverage["extra_ids"], [])
                self.assertFalse(coverage["invalid_schema"])
                self.assertFalse(coverage["invalid_anchor"])
                self.assertFalse(coverage["truncated"])
                self.assertTrue(coverage["coverage_complete"])

    def test_reducer_receives_only_hit_and_uncertain_cards(self):
        plan = self.plan
        settlements = [{
            "state": "settled",
            "settlement_kind": "equal",
            "normalized_result": {
                "items": self._api("validate_map_output")(
                    {"assigned_item_ids": ["asset-1", "asset-2"]},
                    self._output(relations={"asset-1": "uncertain", "asset-2": "distinct"}),
                    plan["snapshot"],
                )["items"]
            },
        }]
        receipt = self._api("build_coverage_receipt")(
            plan, settlements, {"qualified": False, "profile_id": "semantic-test-v1"}
        )
        self.assertEqual([card["lineage_id"] for card in receipt["reducer_input"]], ["lineage-a"])
        self.assertEqual(receipt["observed_ids"], ["asset-1", "asset-2"])

    def test_exceptional_map_requires_detail_and_reduce_before_adjudication(self):
        normalized = self._api("validate_map_output")(
            {"assigned_item_ids": ["asset-1", "asset-2"]},
            self._output(
                relations={
                    "asset-1": "blocking_duplicate",
                    "asset-2": "distinct",
                }
            ),
            self.plan["snapshot"],
        )
        receipt = self._api("build_coverage_receipt")(
            self.plan,
            [{
                "state": "settled",
                "settlement_kind": "equal",
                "normalized_result": normalized,
            }],
            {"qualified": False, "profile_id": "semantic-test-v1"},
        )
        self.assertTrue(receipt["coverage_complete"])
        self.assertFalse(receipt["adjudication_complete"])
        self.assertEqual(
            (receipt["final_status"], receipt["stage_reason_code"]),
            ("overlap_found", "match_found_partial_coverage"),
        )

    def test_nonexceptional_map_needs_no_detail_or_reduce(self):
        plan = self._install()
        self._api("run_map_task")(
            self.conn, self.cas_root, plan, plan["logical_task_keys"][0],
            lambda *_: {
                "kind": "success", "output": self._output(),
            },
            now=self._now(),
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_l2_adjudication_generations_v2"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_l2_derived_task_authority_v2"
            ).fetchone()[0],
            0,
        )
        state = self._api("load_adjudication_state")(
            self.conn, plan["plan_sha"]
        )
        self.assertTrue(state["generation_present"])
        self.assertTrue(state["detail_complete"])
        self.assertTrue(state["reduce_complete"])
        receipt = self._api("build_coverage_receipt")(
            plan, self._api("load_terminal_states")(self.conn, plan["plan_sha"]),
            {"qualified": False, "profile_id": "semantic-test-v1"},
            conn=self.conn,
        )
        self.assertTrue(receipt["coverage_complete"])
        self.assertTrue(receipt["adjudication_complete"])
        self.assertEqual(
            (receipt["final_status"], receipt["stage_reason_code"]),
            ("uncertain", "semantic_policy_unqualified"),
        )
        availability = history_audit_eval_v2.summarize_realized_cost(
            self.conn, plan["run_id"]
        )["intents"][plan["intent"]]["attempt_kind_availability"]
        self.assertEqual(availability["detail"], "not_required")
        self.assertEqual(availability["reduce"], "not_required")

    def test_exceptional_map_materializes_detail_with_only_its_full_record(self):
        plan = self._install()

        def provider(*_):
            return {
                "kind": "success",
                "output": self._output(
                    relations={
                        "asset-1": "blocking_duplicate",
                        "asset-2": "distinct",
                    }
                ),
            }

        self._api("run_map_task")(
            self.conn,
            self.cas_root,
            plan,
            plan["logical_task_keys"][0],
            provider,
            now=self._now(),
        )
        tasks = self.conn.execute(
            "SELECT task_hash, stage FROM audit_logical_tasks ORDER BY stage, task_hash"
        ).fetchall()
        self.assertEqual([row["stage"] for row in tasks], ["detail", "map"])
        detail = self._api("load_task")(self.conn, tasks[0]["task_hash"])
        request = json.loads(detail["durable_request_text"])
        self.assertEqual(request["schema_version"], "history-detail-request-v1")
        self.assertEqual(
            [item["item_id"] for item in request["full_records"]],
            ["asset-1"],
        )
        self.assertNotIn("beta evidence", detail["durable_request_text"])
        self.assertEqual(detail["provider_pool"], ["codex"])

    def test_adjudication_materialization_replays_after_database_reopen(self):
        plan = self._install()
        self._api("run_map_task")(
            self.conn, self.cas_root, plan, plan["logical_task_keys"][0],
            lambda *_: {
                "kind": "success",
                "output": self._output(
                    relations={
                        "asset-1": "blocking_duplicate",
                        "asset-2": "distinct",
                    }
                ),
            },
            now=self._now(),
        )
        first = self.conn.execute(
            """
            SELECT task_hash FROM audit_l2_derived_task_authority_v2
            WHERE stage='detail' ORDER BY task_hash
            """
        ).fetchall()
        first_hashes = [row["task_hash"] for row in first]
        self.assertEqual(len(first_hashes), 1)

        self.conn.close()
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        history_audit_store.init_schema(self.conn)

        self.assertEqual(
            self._api("recover_run")(
                self.conn, plan["plan_sha"], cas_root=self.cas_root,
                now=self._now(1),
            ),
            [],
        )
        replay = self._api("materialize_adjudication_tasks")(
            self.conn, self.cas_root, plan, now=self._now(1)
        )
        self.assertEqual(replay["detail_task_hashes"], first_hashes)
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_l2_adjudication_generations_v2"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_l2_derived_task_authority_v2"
            ).fetchone()[0],
            1,
        )

    def test_adjudication_replay_rejects_changed_frozen_plan(self):
        plan = self._install()
        self._api("run_map_task")(
            self.conn, self.cas_root, plan, plan["logical_task_keys"][0],
            lambda *_: {
                "kind": "success",
                "output": self._output(
                    relations={
                        "asset-1": "blocking_duplicate",
                        "asset-2": "distinct",
                    }
                ),
            },
            now=self._now(),
        )
        changed = copy.deepcopy(plan)
        changed["provider_pools_ordered"]["detail"] = ["grok"]
        with self.assertRaises(self._api("ExecutionError")) as caught:
            self._api("materialize_adjudication_tasks")(
                self.conn, self.cas_root, changed, now=self._now(1)
            )
        self.assertEqual(caught.exception.code, "frozen_identity_mismatch")

    def test_detail_uses_independent_schema_and_only_bound_full_record_anchors(self):
        plan = self._install()
        self._api("run_map_task")(
            self.conn, self.cas_root, plan, plan["logical_task_keys"][0],
            lambda *_: {
                "kind": "success",
                "output": self._output(
                    relations={
                        "asset-1": "blocking_duplicate",
                        "asset-2": "distinct",
                    }
                ),
            },
            now=self._now(),
        )
        pending = history_audit_eval_v2.summarize_realized_cost(
            self.conn, plan["run_id"]
        )["intents"][plan["intent"]]["attempt_kind_availability"]
        self.assertEqual(pending["detail"], "pending")
        self.assertEqual(pending["reduce"], "pending")
        detail_hash = self.conn.execute(
            "SELECT task_hash FROM audit_logical_tasks WHERE stage='detail'"
        ).fetchone()[0]
        detail = self._api("load_task")(self.conn, detail_hash)
        normalized = self._api("validate_detail_output")(
            detail, self._detail_output(detail)
        )
        self.assertEqual(normalized["schema_version"], "history-detail-output-v1")
        with self.assertRaises(self._api("MapValidationError")) as disguised:
            self._api("validate_detail_output")(detail, self._output())
        self.assertEqual(disguised.exception.code, "schema")
        with self.assertRaises(self._api("MapValidationError")) as foreign:
            self._api("validate_detail_output")(
                detail, self._detail_output(detail, anchor_item_id="asset-2")
            )
        self.assertEqual(foreign.exception.code, "invalid_anchor")

    def test_settled_detail_materializes_reduce_with_only_detail_cards(self):
        plan = self._install()
        self._api("run_map_task")(
            self.conn, self.cas_root, plan, plan["logical_task_keys"][0],
            lambda *_: {
                "kind": "success",
                "output": self._output(
                    relations={
                        "asset-1": "blocking_duplicate",
                        "asset-2": "distinct",
                    }
                ),
            },
            now=self._now(),
        )
        detail_hash = self.conn.execute(
            "SELECT task_hash FROM audit_logical_tasks WHERE stage='detail'"
        ).fetchone()[0]
        detail = self._api("load_task")(self.conn, detail_hash)
        expected_card = self._detail_output(detail)["detail_card"]
        self._api("run_task")(
            self.conn, self.cas_root, plan, detail_hash,
            lambda *_: {
                "kind": "success",
                "output": self._detail_output(detail),
            },
            now=self._now(1),
        )
        detail_durable = history_audit_eval_v2.summarize_realized_cost(
            self.conn, plan["run_id"]
        )["intents"][plan["intent"]]["attempt_kind_availability"]
        self.assertEqual(detail_durable["detail"], "durable")
        self.assertEqual(detail_durable["reduce"], "pending")
        reduce_hash = self.conn.execute(
            "SELECT task_hash FROM audit_logical_tasks WHERE stage='reduce'"
        ).fetchone()[0]
        reduce_task = self._api("load_task")(self.conn, reduce_hash)
        request = json.loads(reduce_task["durable_request_text"])
        self.assertEqual(request["schema_version"], "history-reduce-request-v1")
        self.assertEqual(request["detail_cards"], [expected_card])
        self.assertEqual(request["source_task_hashes"], [detail_hash])
        self.assertNotIn("full_records", request)
        self.assertNotIn("alpha evidence", reduce_task["durable_request_text"])
        self.assertEqual(reduce_task["frozen_records"], [])
        self.assertEqual(reduce_task["provider_pool"], ["codex"])
        detail_reservation = self.conn.execute(
            """
            SELECT reservation.attempt_kind
            FROM audit_runtime_budget_reservations_v2 reservation
            JOIN audit_task_attempts attempt
              ON attempt.attempt_id=reservation.attempt_id
            WHERE attempt.task_hash=?
            """,
            (detail_hash,),
        ).fetchone()
        self.assertEqual(detail_reservation["attempt_kind"], "detail")

    def test_each_exceptional_record_gets_one_detail_before_reduce(self):
        records = [
            self.records[0],
            record("asset-3", "gamma evidence", "lineage-a"),
        ]
        plan = self._plan(records)
        self._install(plan)
        self._api("run_map_task")(
            self.conn, self.cas_root, plan, plan["logical_task_keys"][0],
            lambda *_: {
                "kind": "success",
                "output": self._output(
                    plan,
                    relations={
                        "asset-1": "blocking_duplicate",
                        "asset-3": "substantive_overlap",
                    },
                ),
            },
            now=self._now(),
        )
        detail_hashes = [
            row["task_hash"] for row in self.conn.execute(
                """
                SELECT task_hash FROM audit_logical_tasks
                WHERE stage='detail' ORDER BY task_hash
                """
            )
        ]
        self.assertEqual(len(detail_hashes), 2)
        for offset, detail_hash in enumerate(detail_hashes, start=1):
            detail = self._api("load_task")(self.conn, detail_hash)
            self.assertEqual(len(detail["assigned_item_ids"]), 1)
            self._api("run_task")(
                self.conn, self.cas_root, plan, detail_hash,
                lambda *_args, detail=detail: {
                    "kind": "success",
                    "output": self._detail_output(detail),
                },
                now=self._now(offset),
            )
        reduce_tasks = self.conn.execute(
            "SELECT task_hash FROM audit_logical_tasks WHERE stage='reduce'"
        ).fetchall()
        self.assertEqual(len(reduce_tasks), 1)
        reduce_task = self._api("load_task")(
            self.conn, reduce_tasks[0]["task_hash"]
        )
        request = json.loads(reduce_task["durable_request_text"])
        self.assertEqual(len(request["detail_cards"]), 2)
        self.assertNotIn("full_records", request)

    def test_reduce_is_independent_and_only_durable_completion_opens_adjudication(self):
        plan = self._install()
        self._api("run_map_task")(
            self.conn, self.cas_root, plan, plan["logical_task_keys"][0],
            lambda *_: {
                "kind": "success",
                "output": self._output(
                    relations={
                        "asset-1": "blocking_duplicate",
                        "asset-2": "distinct",
                    }
                ),
            },
            now=self._now(),
        )
        detail_hash = self.conn.execute(
            "SELECT task_hash FROM audit_logical_tasks WHERE stage='detail'"
        ).fetchone()[0]
        detail = self._api("load_task")(self.conn, detail_hash)
        self._api("run_task")(
            self.conn, self.cas_root, plan, detail_hash,
            lambda *_: {
                "kind": "success", "output": self._detail_output(detail),
            },
            now=self._now(1),
        )
        before = self._api("build_coverage_receipt")(
            plan, self._api("load_terminal_states")(self.conn, plan["plan_sha"]),
            {"qualified": False, "profile_id": "semantic-test-v1"},
            conn=self.conn,
        )
        self.assertFalse(before["adjudication_complete"])
        reduce_hash = self.conn.execute(
            "SELECT task_hash FROM audit_logical_tasks WHERE stage='reduce'"
        ).fetchone()[0]
        reduce_task = self._api("load_task")(self.conn, reduce_hash)
        normalized = self._api("validate_reduce_output")(
            reduce_task, self._reduce_output(reduce_task)
        )
        self.assertEqual(normalized["schema_version"], "history-reduce-output-v1")
        with self.assertRaises(self._api("MapValidationError")) as override:
            forged = self._reduce_output(reduce_task, cards=[])
            forged["final_status"] = "complete_no_match"
            self._api("validate_reduce_output")(reduce_task, forged)
        self.assertEqual(override.exception.code, "schema")
        self._api("run_task")(
            self.conn, self.cas_root, plan, reduce_hash,
            lambda *_: {
                "kind": "success", "output": self._reduce_output(reduce_task),
            },
            now=self._now(2),
        )
        durable = history_audit_eval_v2.summarize_realized_cost(
            self.conn, plan["run_id"]
        )["intents"][plan["intent"]]["attempt_kind_availability"]
        self.assertEqual(durable["detail"], "durable")
        self.assertEqual(durable["reduce"], "durable")
        after = self._api("build_coverage_receipt")(
            plan, self._api("load_terminal_states")(self.conn, plan["plan_sha"]),
            {"qualified": False, "profile_id": "semantic-test-v1"},
            conn=self.conn,
        )
        self.assertTrue(after["coverage_complete"])
        self.assertTrue(after["adjudication_complete"])
        self.assertEqual(
            (after["final_status"], after["stage_reason_code"]),
            ("overlap_found", "match_found"),
        )
        reduce_kind = self.conn.execute(
            """
            SELECT reservation.attempt_kind
            FROM audit_runtime_budget_reservations_v2 reservation
            JOIN audit_task_attempts attempt
              ON attempt.attempt_id=reservation.attempt_id
            WHERE attempt.task_hash=?
            """,
            (reduce_hash,),
        ).fetchone()[0]
        self.assertEqual(reduce_kind, "reduce")

    def test_exhausted_detail_cannot_materialize_reduce_or_open_adjudication(self):
        plan = self._install()
        self._api("run_map_task")(
            self.conn, self.cas_root, plan, plan["logical_task_keys"][0],
            lambda *_: {
                "kind": "success",
                "output": self._output(
                    relations={
                        "asset-1": "blocking_duplicate",
                        "asset-2": "distinct",
                    }
                ),
            },
            now=self._now(),
        )
        detail_hash = self.conn.execute(
            "SELECT task_hash FROM audit_logical_tasks WHERE stage='detail'"
        ).fetchone()[0]
        exhausted = self._api("run_task")(
            self.conn, self.cas_root, plan, detail_hash,
            lambda *_: {"kind": "schema", "raw": "invalid"},
            now=self._now(1),
        )
        self.assertEqual(exhausted["state"], "exhausted")
        self.assertTrue(
            history_audit_store.validate_l2_terminal_graph(
                self.conn, plan["plan_sha"]
            )
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_logical_tasks WHERE stage='reduce'"
            ).fetchone()[0],
            0,
        )
        state = self._api("load_adjudication_state")(
            self.conn, plan["plan_sha"]
        )
        self.assertFalse(state["detail_complete"])
        self.assertFalse(state["reduce_complete"])
        self.assertEqual(state["exhausted_reason"], "provider_exhausted")
        receipt = self._api("build_coverage_receipt")(
            plan, self._api("load_terminal_states")(self.conn, plan["plan_sha"]),
            {"qualified": False, "profile_id": "semantic-test-v1"},
            conn=self.conn,
        )
        self.assertFalse(receipt["adjudication_complete"])
        self.assertEqual(
            (receipt["final_status"], receipt["stage_reason_code"]),
            ("overlap_found", "match_found_partial_coverage"),
        )

    def test_lineage_uses_maximum_relation_severity_without_extra_votes(self):
        records = [self.records[0], record("asset-3", "gamma evidence", "lineage-a")]
        plan = self._plan(records)
        output = self._output(
            plan,
            relations={"asset-1": "uncertain", "asset-3": "blocking_duplicate"},
        )
        normalized = self._api("validate_map_output")(
            {"assigned_item_ids": ["asset-1", "asset-3"]}, output, plan["snapshot"]
        )
        receipt = self._api("build_coverage_receipt")(
            plan,
            [{"state": "settled", "settlement_kind": "equal", "normalized_result": normalized}],
            {"qualified": False, "profile_id": "semantic-test-v1"},
        )
        self.assertEqual(len(receipt["reducer_input"]), 1)
        self.assertEqual(receipt["reducer_input"][0]["semantic_relation"], "blocking_duplicate")
        self.assertEqual(len(receipt["reducer_input"][0]["evidence"]), 2)
        self.assertEqual(receipt["lineage_vote_count"], 1)

    def test_exhausted_leaf_is_partial_unless_verified_hit_exists(self):
        exhausted = [{"state": "exhausted", "item_ids": ["asset-2"], "reason": "budget_exceeded"}]
        partial = self._api("build_coverage_receipt")(
            self.plan, exhausted, {"qualified": True, "profile_id": "semantic-test-v1"}
        )
        self.assertEqual((partial["final_status"], partial["stage_reason_code"]), ("partial", "budget_exceeded"))

        hit_output = self._output(item_ids=["asset-1"], relations={"asset-1": "blocking_duplicate"})
        hit = self._api("validate_map_output")(
            {"assigned_item_ids": ["asset-1"]}, hit_output, self.plan["snapshot"]
        )
        positive = self._api("build_coverage_receipt")(
            self.plan,
            [{"state": "settled", "settlement_kind": "equal", "normalized_result": hit}] + exhausted,
            {"qualified": True, "profile_id": "semantic-test-v1"},
        )
        self.assertEqual((positive["final_status"], positive["stage_reason_code"]), ("overlap_found", "match_found_partial_coverage"))

    def test_crash_after_cas_before_settlement_resumes_only_unsettled_task(self):
        shards = [
            {"shard_id": "map-0000", "item_ids": ["asset-1"]},
            {"shard_id": "map-0001", "item_ids": ["asset-2"]},
        ]
        plan = self._plan(self.records, shards=shards)
        self._install(plan)

        def provider(task_key, *_):
            item_id = "asset-1" if task_key == plan["logical_task_keys"][0] else "asset-2"
            return {
                "kind": "success",
                "output": self._output(plan, item_ids=[item_id]),
            }

        self._api("run_map_task")(
            self.conn, self.cas_root, plan, plan["logical_task_keys"][0], provider,
            now=self._now(), lease_seconds=1,
        )
        with self.assertRaises(self._api("ExecutionCrash")):
            self._api("run_map_task")(
                self.conn, self.cas_root, plan, plan["logical_task_keys"][1], provider,
                now=self._now(), lease_seconds=1, fault_after_cas=True,
            )
        recovered = self._api("recover_run")(
            self.conn, plan["plan_sha"], cas_root=self.cas_root, now=self._now(2)
        )
        self.assertEqual(recovered, [plan["logical_task_keys"][1]])
        states = dict(self.conn.execute("SELECT task_hash, state FROM audit_logical_tasks"))
        self.assertEqual(states[plan["logical_task_keys"][0]], "settled")
        self.assertEqual(states[plan["logical_task_keys"][1]], "planned")

    def test_budget_covers_retry_failover_split_detail_and_reduce(self):
        events = []
        append = getattr(history_audit_plan, "append_runtime_budget_event", None)
        totals = getattr(history_audit_plan, "realized_budget_totals", None)
        self.assertTrue(callable(append), "missing runtime budget event behavior")
        self.assertTrue(callable(totals), "missing realized budget behavior")
        for index, kind in enumerate(("retry", "failover", "split", "detail", "reduce")):
            append(
                events,
                work_id=f"work-{index}",
                attempt_kind=kind,
                usage={"input_tokens": 10, "output_tokens": 2, "provider_usage_units": 12},
            )
        append(
            events,
            work_id="work-0",
            attempt_kind="retry",
            usage={"input_tokens": 10, "output_tokens": 2, "provider_usage_units": 12},
        )
        self.assertEqual([event["attempt_kind"] for event in events], ["retry", "failover", "split", "detail", "reduce"])
        self.assertEqual(
            totals(events),
            {"started_attempts": 5, "input_tokens": 50, "output_tokens": 10, "provider_usage_units": 60},
        )

    def test_durable_budget_rejects_split_child_after_reopen_without_partial_event(self):
        plan = self._plan(
            self.records,
            started_attempt_limit=1,
            map_providers=["codex"],
        )
        self._install(plan)
        parent = plan["logical_task_keys"][0]
        claim = self._api("claim_task")(
            self.conn, parent, "worker-a", 60, expected_fence=0, now=self._now()
        )
        self._api("record_attempt")(
            self.conn,
            parent,
            copy.deepcopy(self.capabilities["codex"]),
            {
                "attempt_kind": "initial",
                "input_tokens": 1,
                "output_tokens": 1,
                "provider_usage_units": 2,
            },
            cas_root=self.cas_root,
            request_bytes=plan["shards"][0]["serialized_request"].encode(),
        )
        children = self._api("split_task")(
            self.conn, parent,
            expected_fence=claim["fence"],
            claim_token=claim["claim_token"],
            now=self._now(),
        )["children"]
        child = children[0]["task_hash"]
        self.conn.close()
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        history_audit_store.init_schema(self.conn)
        self._api("claim_task")(
            self.conn, child, "worker-b", 60, expected_fence=0, now=self._now()
        )
        before = {
            "attempts": self.conn.execute("SELECT count(*) FROM audit_task_attempts").fetchone()[0],
            "events": self.conn.execute("SELECT count(*) FROM audit_budget_events").fetchone()[0],
            "objects": self.conn.execute("SELECT count(*) FROM audit_cas_objects").fetchone()[0],
        }
        task = self._api("load_task")(self.conn, child)
        request_bytes = history_contract_v2.canonical_bytes(
            {
                "parent_task_hash": task["parent_task_hash"],
                "position": 0,
                "item_ids": task["assigned_item_ids"],
            }
        )
        with self.assertRaises(self._api("ExecutionError")) as caught:
            self._api("record_attempt")(
                self.conn,
                child,
                copy.deepcopy(self.capabilities["codex"]),
                {
                    "attempt_kind": "split",
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "provider_usage_units": 2,
                },
                cas_root=self.cas_root,
                request_bytes=request_bytes,
            )
        self.assertEqual(caught.exception.code, "attempt_budget_exceeded")
        after = {
            "attempts": self.conn.execute("SELECT count(*) FROM audit_task_attempts").fetchone()[0],
            "events": self.conn.execute("SELECT count(*) FROM audit_budget_events").fetchone()[0],
            "objects": self.conn.execute("SELECT count(*) FROM audit_cas_objects").fetchone()[0],
        }
        self.assertEqual(after, before)

    def test_run_task_turns_budget_rejection_into_durable_partial_terminal(self):
        shards = [
            {"shard_id": "map-0000", "item_ids": ["asset-1"]},
            {"shard_id": "map-0001", "item_ids": ["asset-2"]},
        ]
        plan = self._plan(
            self.records,
            shards=shards,
            started_attempt_limit=2,
            map_providers=["codex"],
        )
        self._install(plan)

        def consume_retry_budget(_task_key, _provider, ordinal, _request):
            if ordinal == 0:
                return {"kind": "syntax", "raw": "syntax"}
            return {
                "kind": "success",
                "output": self._output(
                    plan, item_ids=["asset-1"],
                    relations={"asset-1": "blocking_duplicate"},
                ),
            }

        self._api("run_map_task")(
            self.conn, self.cas_root, plan, plan["logical_task_keys"][0],
            consume_retry_budget,
            now=self._now(),
        )
        provider_calls = []

        def should_not_run(*args):
            provider_calls.append(args)
            return {
                "kind": "success",
                "output": self._output(plan, item_ids=["asset-2"]),
            }

        result = self._api("run_map_task")(
            self.conn, self.cas_root, plan, plan["logical_task_keys"][1],
            should_not_run, now=self._now(1),
        )
        self.assertEqual(provider_calls, [])
        self.assertEqual(result["state"], "exhausted")
        terminal = self.conn.execute(
            """
            SELECT task.state, fact.reason
            FROM audit_logical_tasks task
            JOIN audit_task_terminal_facts_v2 fact
              ON fact.task_hash=task.task_hash
            WHERE task.task_hash=?
            """,
            (plan["logical_task_keys"][1],),
        ).fetchone()
        self.assertEqual(
            (terminal["state"], terminal["reason"]),
            ("exhausted", "budget_exceeded"),
        )
        receipt = self._api("build_coverage_receipt")(
            plan, self._api("load_terminal_states")(self.conn, plan["plan_sha"]),
            {"qualified": False, "profile_id": "semantic-test-v1"},
            conn=self.conn,
        )
        self.assertEqual(
            (receipt["final_status"], receipt["stage_reason_code"]),
            ("overlap_found", "match_found_partial_coverage"),
        )

    def test_database_rejects_direct_budget_reservation_past_candidate_limit(self):
        plan = self._plan(
            self.records,
            started_attempt_limit=1,
            map_providers=["codex"],
        )
        self._install(plan)
        parent = plan["logical_task_keys"][0]
        claim = self._api("claim_task")(
            self.conn, parent, "worker-a", 60, expected_fence=0, now=self._now()
        )
        self._api("record_attempt")(
            self.conn,
            parent,
            copy.deepcopy(self.capabilities["codex"]),
            {"attempt_kind": "initial"},
            cas_root=self.cas_root,
            request_bytes=plan["shards"][0]["serialized_request"].encode(),
        )
        child_key = self._api("split_task")(
            self.conn, parent,
            expected_fence=claim["fence"],
            claim_token=claim["claim_token"],
            now=self._now(),
        )["children"][0]["task_hash"]
        child = self._api("load_task")(self.conn, child_key)
        forged_attempt = sha("direct-budget-forgery")
        reserved = {
            "input_tokens": len(child["durable_request_text"].encode()),
            "output_tokens": plan["capacity_profile"]["max_output_tokens"],
            "provider_usage_units": len(child["durable_request_text"].encode())
            + plan["capacity_profile"]["max_output_tokens"],
        }
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO audit_runtime_budget_reservations_v2(
                  attempt_id, task_hash, plan_sha, candidate_id, intent,
                  attempt_kind, reserved_json, created_at
                ) VALUES(?, ?, ?, ?, ?, 'split', ?, ?)
                """,
                (
                    forged_attempt, child_key, plan["plan_sha"],
                    plan["candidate"]["candidate_id"], plan["intent"],
                    json.dumps(reserved, sort_keys=True, separators=(",", ":")) + "\n",
                    self._now(),
                ),
            )

    def test_claims_are_fenced_and_terminal_states_do_not_reopen(self):
        plan = self._install()
        task_key = plan["logical_task_keys"][0]
        first = self._api("claim_task")(
            self.conn, task_key, "worker-a", 10, expected_fence=0, now=self._now()
        )
        self.assertEqual(first["fence"], 1)
        with self.assertRaises(history_audit_store.StaleFence):
            self._api("claim_task")(
                self.conn, task_key, "worker-b", 10, expected_fence=0, now=self._now()
            )
        self._api("exhaust_task")(
            self.conn, task_key, "budget_exceeded",
            expected_fence=first["fence"],
            claim_token=first["claim_token"],
            now=self._now(),
        )
        with self.assertRaises(self._api("ExecutionError")):
            self._api("claim_task")(
                self.conn, task_key, "worker-c", 10, expected_fence=2, now=self._now(20)
            )

    def test_expired_claim_cannot_publish_terminal_settlement(self):
        plan = self._install()
        task_key = plan["logical_task_keys"][0]
        self._api("claim_task")(
            self.conn, task_key, "worker-a", 1, expected_fence=0, now=self._now()
        )
        attempt = self._api("record_attempt")(
            self.conn,
            task_key,
            copy.deepcopy(self.capabilities["codex"]),
            {
                "attempt_kind": "initial",
                "input_tokens": 1,
                "output_tokens": 1,
                "provider_usage_units": 2,
            },
            cas_root=self.cas_root,
            request_bytes=plan["shards"][0]["serialized_request"].encode(),
        )
        valid = self._api("complete_attempt")(
            self.conn, self.cas_root, task_key, attempt["attempt_id"],
            self._output(), plan["snapshot"],
        )
        with self.assertRaises(history_audit_store.StaleFence):
            self._api("settle_task")(
                self.conn, task_key, [valid], cas_root=self.cas_root, now=self._now(2)
            )

    def test_attempt_completion_fact_rejects_conflicting_replay(self):
        plan = self._install()
        task_key = plan["logical_task_keys"][0]
        self._api("claim_task")(
            self.conn, task_key, "worker-a", 60, expected_fence=0, now=self._now()
        )
        attempt = self._api("record_attempt")(
            self.conn,
            task_key,
            copy.deepcopy(self.capabilities["codex"]),
            {
                "attempt_kind": "initial",
                "input_tokens": 1,
                "output_tokens": 1,
                "provider_usage_units": 2,
            },
            cas_root=self.cas_root,
            request_bytes=plan["shards"][0]["serialized_request"].encode(),
        )
        self._api("complete_attempt")(
            self.conn, self.cas_root, task_key, attempt["attempt_id"],
            self._output(), plan["snapshot"],
        )
        conflicting = self._output(relations={"asset-1": "blocking_duplicate"})
        with self.assertRaises(self._api("ExecutionError")) as caught:
            self._api("complete_attempt")(
                self.conn, self.cas_root, task_key, attempt["attempt_id"],
                conflicting, plan["snapshot"],
            )
        self.assertEqual(caught.exception.code, "conflicting_attempt_completion")


if __name__ == "__main__":
    unittest.main()
