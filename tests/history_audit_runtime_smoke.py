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
            for provider in ("codex", "grok")
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

    def _plan(self, records, *, shards=None, started_attempt_limit=100):
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
        current_ids = [candidate_id]
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
                "comparator": ["codex"],
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
            "risk_policy_sha": sha("risk-policy"),
            "capacity_profile": {
                "profile_id": "fake-safe-24k-v1",
                "item_cap": 12,
                "max_output_tokens": 64,
            },
            "budget_policy": {
                "schema_version": "l2-budget-v1",
                "settlement_policy_sha": sha("settlement"),
                "risk_policy_sha": sha("risk-policy"),
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

    def _install(self, plan=None):
        plan = plan or self.plan
        self._api("persist_plan")(self.conn, plan)
        return plan

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
        self._api("claim_task")(
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
        children = self._api("split_task")(self.conn, parent)["children"]
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
        self._api("claim_task")(
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
        child_key = self._api("split_task")(self.conn, parent)["children"][0]["task_hash"]
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
        self._api("exhaust_task")(self.conn, task_key, "budget_exceeded", expected_fence=1)
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
