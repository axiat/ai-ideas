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
        self.cas_root = pathlib.Path(self.temporary.name) / "cas"
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        history_audit_store.init_schema(self.conn)
        self.records = [
            record("asset-1", "alpha evidence", "lineage-a"),
            record("asset-2", "beta evidence", "lineage-b"),
        ]
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

    def _plan(self, records, *, shards=None):
        snapshot = {
            "snapshot_id": sha("snapshot-id"),
            "snapshot_hash": sha("snapshot"),
            "history_as_of_watermark": 550,
            "current_batch_id_namespace": "history-v2-staging-v1",
            "current_batch_ids_hash": sha("batch"),
            "exclusion_policy_sha": sha("exclusion"),
            "expected_asset_ids_hash": sha("expected"),
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
        task_keys = [
            history_contract_v2.logical_task_key(
                sha("runtime-plan" + str(len(shards))),
                "map",
                "staging-1",
                shard["request_sha256"],
            )
            for shard in shards
        ]
        return {
            "schema_version": "history-audit-plan-v1",
            "run_id": "run-runtime-smoke",
            "batch_id": "batch-1",
            "plan_sha": sha("runtime-plan" + str(len(shards))),
            "candidate": {"candidate_id": "staging-1", "candidate_hash": sha("candidate")},
            "snapshot": snapshot,
            "provider_pools_ordered": {
                "comparator": ["codex"],
                "map": ["codex", "grok"],
                "detail": ["codex"],
                "reduce": ["codex"],
            },
            "provider_capability_profile_hashes": [sha("codex"), sha("grok")],
            "capacity_profile_id": "fake-safe-24k-v1",
            "semantic_policy_profile_id": "semantic-test-v1",
            "risk_policy_version": "risk-v1",
            "matched_router_rule_ids": ["rule-l2"],
            "settlement_policy_sha": sha("settlement"),
            "shard_plan_sha": sha("shard-plan" + str(len(shards))),
            "shards": shards,
            "logical_task_keys": task_keys,
            "intent": "duplicate_search",
        }

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
            {"provider": "codex", "profile_hash": sha("codex")},
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
            {"provider": "codex", "profile_hash": sha("codex")},
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
