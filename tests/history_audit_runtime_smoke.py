#!/usr/bin/env python3
"""Deterministic L2 execution, settlement, coverage, and recovery smoke tests."""

import copy
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
            "candidate_budget_available": True,
            "attempt_budget_available": True,
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
        additional_candidates=None,
    ):
        risk_policy_sha = history_contract_v2.framed_sha256(
            "history-risk-policy-v1",
            history_contract_v2.canonical_bytes(self._risk_policy()),
        )
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
        plan = {
            "schema_version": "history-audit-plan-v2",
            "run_id": "run-runtime-smoke",
            "batch_id": "batch-1",
            "candidate": candidate,
            "snapshot": snapshot,
            "provider_pools_ordered": {
                "comparator": ["reviewer"],
                "map": ["codex", "grok"],
                "detail": ["codex"],
                "reduce": ["codex"],
            },
            "provider_capability_profile_hashes": {
                provider: capability["capability_profile_hash"]
                for provider, capability in self.capabilities.items()
            },
            "provider_capabilities": copy.deepcopy(self.capabilities),
            "capacity_profile_id": "fake-safe-24k-v1",
            "semantic_policy_profile_id": "semantic-test-v1",
            "risk_policy_version": "risk-v1",
            "matched_router_rule_ids": ["rule-l2"],
            "settlement_policy_sha": sha("settlement"),
            "risk_policy_sha": risk_policy_sha,
            "capacity_profile": {
                "profile_id": "fake-safe-24k-v1",
                "item_cap": 12,
                "max_output_tokens": 64,
            },
            "budget_policy": {
                "schema_version": "l2-budget-v1",
                "settlement_policy_sha": sha("settlement"),
                "risk_policy_sha": risk_policy_sha,
                "intents": {
                    "duplicate_search": {
                        "round": {
                            "candidates": 8,
                            "started_attempts": started_attempt_limit,
                            "input_tokens": 100000,
                            "output_tokens": 100000,
                            "provider_usage_units": 200000,
                        },
                        "candidate": {
                            "started_attempts": started_attempt_limit,
                            "input_tokens": 100000,
                            "output_tokens": 100000,
                            "provider_usage_units": 200000,
                        },
                    }
                },
            },
            "shards": shards,
            "intent": "duplicate_search",
        }
        plan["shard_plan_sha"] = history_contract_v2.framed_sha256(
            "history-shard-plan-v2",
            history_contract_v2.canonical_bytes(
                sorted(copy.deepcopy(shards), key=lambda shard: shard["shard_id"])
            ),
        )
        snapshot_for_plan = copy.deepcopy(snapshot)
        snapshot_for_plan.pop("records")
        snapshot_for_plan["records_sha"] = history_contract_v2.framed_sha256(
            "history-l2-snapshot-records-v2",
            history_contract_v2.canonical_bytes(
                sorted(copy.deepcopy(records), key=lambda item: item["item_id"])
            ),
        )
        budget_sha = history_contract_v2.framed_sha256(
            "history-budget-policy-v1",
            history_contract_v2.canonical_bytes(plan["budget_policy"]),
        )
        plan_material = {
            "schema_version": "history-audit-plan-v2",
            "run_id": plan["run_id"],
            "batch_id": plan["batch_id"],
            "candidate": copy.deepcopy(candidate),
            "snapshot": snapshot_for_plan,
            "provider_pools_ordered": copy.deepcopy(plan["provider_pools_ordered"]),
            "provider_capability_profile_hashes": copy.deepcopy(
                plan["provider_capability_profile_hashes"]
            ),
            "provider_capabilities": copy.deepcopy(plan["provider_capabilities"]),
            "capacity_profile": copy.deepcopy(plan["capacity_profile"]),
            "budget_policy": copy.deepcopy(plan["budget_policy"]),
            "budget_policy_sha": budget_sha,
            "intent": plan["intent"],
            "risk_policy_sha": plan["risk_policy_sha"],
            "settlement_policy_sha": plan["settlement_policy_sha"],
            "shard_plan_sha": plan["shard_plan_sha"],
            "shards": sorted(copy.deepcopy(shards), key=lambda shard: shard["shard_id"]),
        }
        plan["plan_sha"] = history_contract_v2.framed_sha256(
            "history-audit-plan-v2",
            history_contract_v2.canonical_bytes(plan_material),
        )
        plan["logical_task_keys"] = [
            history_contract_v2.logical_task_key(
                plan["plan_sha"], "map", candidate_id, shard["request_sha256"]
            )
            for shard in shards
        ]
        return plan

    def _install(self, plan=None, *, route_authority=None):
        plan = plan or self.plan
        self._api("persist_plan")(
            self.conn, plan,
            route_authority=(route_authority or self._route_authority(plan)),
        )
        return plan

    def _persist_pre_route_plan(self, conn, plan):
        """Build an old-schema fixture without weakening current runtime gates."""
        with mock.patch.object(
            history_audit_store, "record_candidate_route_facts"
        ), mock.patch.object(
            history_audit_store, "record_candidate_l2_dispatch_fact"
        ):
            history_execution.persist_plan(conn, plan, route_authority={})

    def _seed_route_prerequisites(
        self, conn, plan, candidates, *, run_created_at=None,
        staging_created_at=None,
    ):
        material = history_audit_plan.build_runtime_plan_material(plan)
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
            conn.execute(
                "INSERT INTO audit_batch_staging VALUES(?,?,?,?,?,?,?)",
                (
                    candidate["candidate_id"], plan["run_id"],
                    plan["batch_id"], candidate["candidate_hash"],
                    candidate["raw_artifact_sha"], candidate["source_order"],
                    staging_created_at,
                ),
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
        with self.assertRaises(self._api("ExecutionError")):
            self._api("persist_plan")(
                self.conn, self.plan, route_authority=authority
            )
        authority = self._route_authority(risk_slices=["invented_slice"])
        with self.assertRaises(self._api("ExecutionError")):
            self._api("persist_plan")(
                self.conn, self.plan, route_authority=authority
            )
        authority["risk_slice_policy"]["allowed_slices"].append(
            "invented_slice"
        )
        authority["risk_slice_policy"]["allowed_slices"].sort()
        with self.assertRaises(self._api("ExecutionError")):
            self._api("persist_plan")(
                self.conn, self.plan, route_authority=authority
            )

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
        self.assertEqual(caught.exception.code, "invalid_route_authority")
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
        self.assertEqual(caught.exception.code, "invalid_route_authority")
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
        history_audit_store.record_candidate_route_facts(
            self.conn, plan["run_id"], plan["batch_id"], plan["intent"],
            self._route_authority(plan), created_at=created_at,
        )
        self.conn.execute("COMMIT")
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
                return {"kind": "timeout", "raw": "timeout", "usage": {"input_tokens": 10}}
            return {"kind": "success", "output": self._output(), "usage": {"input_tokens": 10, "output_tokens": 5}}

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
            return {
                "kind": "success", "output": self._output(),
                "usage": {
                    "input_tokens": 10, "output_tokens": 5,
                    "cache_tokens": 3, "provider_usage_units": 15,
                },
            }

        self._api("run_map_task")(
            self.conn, self.cas_root, plan, plan["logical_task_keys"][0],
            provider, now=self._now(),
        )
        first = history_audit_eval_v2.summarize_realized_cost(
            self.conn, plan["run_id"]
        )
        realized = first["intents"]["duplicate_search"]["realized"]
        self.assertEqual(realized["calls"], 1)
        self.assertEqual(realized["input_tokens"], 10)
        self.assertEqual(realized["output_tokens"], 5)
        self.assertEqual(realized["cache_tokens"], 3)
        self.assertNotIn("currency_micros", realized)
        self.assertGreaterEqual(realized["queue_latency_ms"], 0)
        self.assertGreaterEqual(realized["run_latency_ms"], 0)
        self.assertTrue(first["intents"]["duplicate_search"]["latency_complete"])
        self.assertFalse(
            first["intents"]["duplicate_search"]["accounting_complete"]
        )
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
        candidate_routes = sorted([
            {
                "candidate": copy.deepcopy(plan["candidate"]),
                "router_facts": self._router_facts(),
                "risk_slices": ["cross_language", "low_overlap"],
            },
            {
                "candidate": copy.deepcopy(second),
                "router_facts": self._router_facts(
                    candidate_budget_available=False,
                    attempt_budget_available=False,
                ),
                "risk_slices": ["low_overlap"],
            },
        ], key=lambda item: item["candidate"]["candidate_id"])
        authority = self._route_authority(
            plan, candidate_routes=candidate_routes
        )
        plan = self._install(plan, route_authority=authority)
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
                "usage": {
                    "input_tokens": 10, "output_tokens": 6,
                    "cache_tokens": 2, "provider_usage_units": 16,
                },
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
        self.assertEqual(summary["expected_per_candidate"]["input_tokens"], 5)
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
            finalist_or_sa=False,
            bad_slice_membership=False,
        )
        plan = self._install(
            route_authority=self._route_authority(
                facts=facts, risk_slices=[]
            )
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
        self._api("persist_plan")(
            self.conn, plan, route_authority=self._route_authority(plan)
        )
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
        with self.assertRaises(self._api("ExecutionError")):
            self._api("persist_plan")(
                self.conn, plan,
                route_authority=self._route_authority(
                    risk_slices=["cross_language", "low_overlap"]
                ),
            )
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
        self.conn.execute(
            "INSERT INTO audit_batch_staging VALUES(?,?,?,?,?,?,?)",
            (
                candidate["candidate_id"], plan["run_id"], plan["batch_id"],
                candidate["candidate_hash"], candidate["raw_artifact_sha"],
                candidate["source_order"], self._now(),
            ),
        )
        self.conn.commit()
        self._api("persist_plan")(
            self.conn, plan, route_authority=self._route_authority(plan)
        )
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
            usage={
                "input_tokens": 10, "output_tokens": 5,
                "provider_usage_units": 15,
            },
            now=completed_at,
        )
        realized = history_audit_eval_v2.summarize_realized_cost(
            self.conn, plan["run_id"]
        )["intents"][plan["intent"]]["realized"]
        self.assertEqual(realized["queue_latency_ms"], 10000)
        self.assertEqual(realized["run_latency_ms"], 4000)

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
        self._api("record_attempt")(
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
            history_execution.persist_plan(
                legacy, plan, route_authority=self._route_authority(plan)
            )
            legacy.execute(
                "DROP TABLE audit_candidate_route_observation_boundaries_v2"
            )
            legacy.commit()
        legacy.close()
        legacy = sqlite3.connect(legacy_path)
        legacy.row_factory = sqlite3.Row
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
        for _ in range(2):
            self._api("cancel_attempt")(
                self.conn, attempt["attempt_id"], billing_state="unknown",
                usage={
                    "input_tokens": 2, "output_tokens": 0,
                    "cache_tokens": 1, "provider_usage_units": 2,
                },
                now=cancel_at,
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
                        return {"kind": failure, "raw": failure, "usage": {}}
                    return {"kind": "success", "output": self._output(), "usage": {}}

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
                return {"kind": "syntax", "raw": "syntax", "usage": {}}
            return {"kind": "success", "output": self._output(), "usage": {}}

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
            return {"kind": "overflow", "raw": "overflow", "usage": {}}

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
                "usage": {},
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
            return {"kind": "overflow", "raw": "overflow", "usage": {}}

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

    def test_missing_duplicate_extra_and_truncated_outputs_never_cover_parent(self):
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

                def provider(*_):
                    return {"kind": "success", "output": output, "usage": {}}

                result = self._api("run_map_task")(
                    self.conn, self.cas_root, plan, plan["logical_task_keys"][0], provider,
                    now=self._now(),
                )
                self.assertEqual(result["state"], "superseded")
                coverage = self._api("build_coverage_receipt")(
                    plan, self._api("load_terminal_states")(self.conn, plan["plan_sha"]),
                    {"qualified": False, "profile_id": "semantic-test-v1"},
                )
                self.assertEqual(coverage["observed_ids"], [])

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
            return {"kind": "success", "output": self._output(plan, item_ids=[item_id]), "usage": {}}

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
        plan = self._plan(self.records, started_attempt_limit=1)
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

    def test_database_rejects_direct_budget_reservation_past_candidate_limit(self):
        plan = self._plan(self.records, started_attempt_limit=1)
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
