#!/usr/bin/env python3
"""RED contract for host-derived, durable router source authority."""

import contextlib
import copy
import datetime
import hashlib
import importlib.util
import json
import pathlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_audit_plan
from lib import history_audit_eval_v2
from lib import history_audit_store
from lib import history_contract_v2


def sha(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _load_runtime_fixture_module():
    path = ROOT / "tests/history_audit_runtime_smoke.py"
    spec = importlib.util.spec_from_file_location(
        "_history_router_runtime_fixture", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNTIME_FIXTURE = _load_runtime_fixture_module()


def _load_eval_fixture_module():
    path = ROOT / "tests/history_audit_eval_smoke.py"
    spec = importlib.util.spec_from_file_location(
        "_history_router_eval_fixture", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EVAL_FIXTURE = _load_eval_fixture_module()


class HistoryRouterSourceAuthoritySmoke(unittest.TestCase):
    """The router consumes durable domain artifacts, never caller booleans."""

    def setUp(self):
        self.runtime = self._new_runtime()

    def tearDown(self):
        self.runtime.tearDown()

    def _new_runtime(self):
        runtime = RUNTIME_FIXTURE.HistoryAuditRuntimeSmoke(
            "test_persist_plan_requires_bound_route_authority"
        )
        runtime.setUp()
        return runtime

    @contextlib.contextmanager
    def _fresh_runtime(self):
        runtime = self._new_runtime()
        try:
            yield runtime
        finally:
            runtime.tearDown()

    def _source_api(self):
        value = getattr(
            history_audit_store, "_issue_test_router_domain_sources", None
        )
        self.assertTrue(
            callable(value),
            "missing behavior: "
            "history_audit_store._issue_test_router_domain_sources",
        )
        return value

    def _prepare_api(self):
        value = getattr(history_audit_store, "prepare_router_round", None)
        self.assertTrue(
            callable(value),
            "missing behavior: history_audit_store.prepare_router_round",
        )
        return value

    def _derive_api(self):
        value = getattr(
            history_audit_store, "derive_candidate_route_facts", None
        )
        self.assertTrue(
            callable(value),
            "missing behavior: history_audit_store.derive_candidate_route_facts",
        )
        return value

    def _host_api(self, name):
        value = getattr(history_audit_store, name, None)
        self.assertTrue(
            callable(value),
            "missing behavior: history_audit_store." + name,
        )
        return value

    def _now(self, seconds=0):
        base = datetime.datetime(2026, 8, 3, tzinfo=datetime.timezone.utc)
        return (base + datetime.timedelta(seconds=seconds)).isoformat()

    def _additional_candidate(self, label="router-second", source_order=1):
        candidate = {
            "candidate_id": "stg-v2-" + sha(label),
            "candidate_hash": "",
            "raw_artifact_sha": sha(label + "-raw"),
            "source_order": source_order,
        }
        candidate["candidate_hash"] = history_audit_plan.runtime_candidate_hash(
            candidate
        )
        return candidate

    def _cohort_plan(self, runtime=None, *, started_attempt_limit=100):
        runtime = runtime or self.runtime
        return runtime._plan(
            runtime.records,
            additional_candidates=[self._additional_candidate()],
            started_attempt_limit=started_attempt_limit,
        )

    def _foreign_plan(self, plan):
        changed = copy.deepcopy(plan)
        changed["run_id"] = "run-router-foreign"
        changed["batch_id"] = "batch-router-foreign"
        candidate = {
            "candidate_id": "stg-v2-" + sha("router-foreign-candidate"),
            "candidate_hash": "",
            "raw_artifact_sha": sha("router-foreign-candidate-raw"),
            "source_order": 0,
        }
        candidate["candidate_hash"] = history_audit_plan.runtime_candidate_hash(
            candidate
        )
        changed["candidate"] = candidate
        snapshot = changed["snapshot"]
        snapshot["current_batch_ids"] = [candidate["candidate_id"]]
        snapshot["current_batch_ids_hash"] = (
            history_contract_v2.ordered_set_sha256(
                "history-current-batch-ids-v2", snapshot["current_batch_ids"]
            )
        )
        snapshot_material = {
            "run_id": changed["run_id"],
            "batch_id": changed["batch_id"],
            "history_as_of_watermark": snapshot["history_as_of_watermark"],
            "current_batch_id_namespace": snapshot[
                "current_batch_id_namespace"
            ],
            "current_batch_ids_hash": snapshot["current_batch_ids_hash"],
            "exclusion_policy_sha": snapshot["exclusion_policy_sha"],
            "expected_asset_ids_hash": snapshot["expected_asset_ids_hash"],
        }
        snapshot["snapshot_hash"] = history_contract_v2.framed_sha256(
            "history-snapshot-v2",
            history_contract_v2.canonical_bytes(snapshot_material),
        )
        snapshot["snapshot_id"] = history_contract_v2.framed_sha256(
            "history-snapshot-id-v2",
            history_contract_v2.canonical_bytes(
                {
                    "run_id": changed["run_id"],
                    "batch_id": changed["batch_id"],
                    "snapshot_hash": snapshot["snapshot_hash"],
                }
            ),
        )
        changed["plan_sha"] = history_audit_plan.runtime_plan_sha(changed)
        changed["logical_task_keys"] = [
            history_contract_v2.logical_task_key(
                changed["plan_sha"], "map", candidate["candidate_id"],
                shard["request_sha256"],
            )
            for shard in changed["shards"]
        ]
        return changed

    def _candidate_cohort(self, plan, additional_candidates=None):
        candidates = {plan["candidate"]["candidate_id"]: plan["candidate"]}
        candidates.update(
            {
                candidate["candidate_id"]: candidate
                for candidate in (additional_candidates or [])
            }
        )
        for candidate_id in plan["snapshot"]["current_batch_ids"]:
            if candidate_id not in candidates:
                candidate = self._additional_candidate()
                self.assertEqual(candidate["candidate_id"], candidate_id)
                candidates[candidate_id] = candidate
        return [copy.deepcopy(candidates[value]) for value in sorted(candidates)]

    def _round_material(self, plan, additional_candidates=None):
        snapshot_fields = {
            "snapshot_id", "snapshot_hash", "history_as_of_watermark",
            "current_batch_id_namespace", "current_batch_ids_hash",
            "current_batch_ids", "exclusion_policy_sha",
            "expected_asset_ids_hash", "expected_asset_ids",
        }
        snapshot = {
            name: copy.deepcopy(plan["snapshot"][name])
            for name in snapshot_fields
        }
        return {
            "schema_version": "history-router-round-v1",
            "run_id": plan["run_id"],
            "batch_id": plan["batch_id"],
            "intent": plan["intent"],
            "snapshot": snapshot,
            "candidates": self._candidate_cohort(
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

    def _host_observations(self, plan, additional_candidates=None):
        candidates = self._candidate_cohort(
            plan, additional_candidates=additional_candidates
        )
        selected_id = plan["candidate"]["candidate_id"]
        return {
            "schema_version": "history-router-host-observations-v1",
            "selected_candidate_id": selected_id,
            "members": [
                {
                    "candidate_id": candidate["candidate_id"],
                    "selection_class": (
                        "finalist"
                        if candidate["candidate_id"] == selected_id
                        else "screened"
                    ),
                    "channel_states": [
                        {"channel_id": "dense_core", "state": "complete"},
                        {"channel_id": "exact_lineage", "state": "complete"},
                        {"channel_id": "fts", "state": "complete"},
                    ],
                    "assigned_slice_ids": ["low_overlap"],
                    "permanent_request_id": None,
                }
                for candidate in candidates
            ],
        }

    def _install_host_shadow_calibration(self, runtime, route_round_sha256):
        policy = history_audit_plan._load_host_policy(
            "semantic-release-policy-v1.json"
        )
        authority = history_audit_plan._host_runtime_authority()
        dependencies = history_audit_store._router_host_default_dependency_heads(
            route_round_sha256, authority
        )
        dependencies["semantic_policy"] = (
            history_audit_eval_v2.semantic_policy_sha256(policy)
        )
        rows = EVAL_FIXTURE.qrels(30, 20, slice_count=5)
        validated = history_audit_eval_v2.validate_qrels(
            rows, EVAL_FIXTURE.partitions(rows), scope="real"
        )
        evidence = EVAL_FIXTURE.evidence(
            dependency_overrides=dependencies
        )
        history_audit_store.publish_semantic_dependency_heads(
            runtime.conn, dependencies, now=self._now(6)
        )
        stored = history_audit_store.persist_semantic_qualification(
            runtime.conn, validated, EVAL_FIXTURE.outputs(rows), policy,
            evidence, now=self._now(7),
        )
        self.assertFalse(stored["production_qualified"])
        return stored

    def _host_l1_raw_bytes(
        self, plan, route_round, candidate, pre_route, *, outcome="no_match"
    ):
        return history_contract_v2.canonical_bytes({
            "schema_version": "history-router-host-l1-observation-v2",
            "route_round_sha256": route_round["route_round_sha256"],
            "host_round_authority_sha256": route_round[
                "host_round_authority_sha256"
            ],
            "run_id": plan["run_id"],
            "batch_id": plan["batch_id"],
            "intent": plan["intent"],
            "snapshot_id": plan["snapshot"]["snapshot_id"],
            "snapshot_hash": plan["snapshot"]["snapshot_hash"],
            "candidate_id": candidate["candidate_id"],
            "candidate_hash": candidate["candidate_hash"],
            "candidate_raw_artifact_sha256": candidate["raw_artifact_sha"],
            "source_order": candidate["source_order"],
            "pre_phase_fact_sha256": pre_route["phase_fact_sha256"],
            "comparator_outcome": outcome,
            "coverage_state": "complete",
        })

    def _seed_host_round_inputs(
        self, runtime, plan, *, additional_candidates=None
    ):
        records = history_audit_plan.runtime_snapshot_records(
            plan["snapshot"]["records"]
        )
        candidates = self._candidate_cohort(
            plan, additional_candidates=additional_candidates
        )
        receipt = history_audit_store.record_host_router_preplan(
            runtime.conn,
            run_id=plan["run_id"], batch_id=plan["batch_id"],
            intent=plan["intent"],
            history_as_of_watermark=plan["snapshot"][
                "history_as_of_watermark"
            ],
            exclusion_policy_sha=plan["snapshot"]["exclusion_policy_sha"],
            records=records,
            candidates=[
                {
                    "candidate_id": candidate["candidate_id"],
                    "raw_artifact_sha": candidate["raw_artifact_sha"],
                    "source_order": candidate["source_order"],
                }
                for candidate in candidates
            ],
            created_at=self._now(1),
        )
        self.assertEqual(receipt["snapshot"], self._round_material(
            plan, additional_candidates=additional_candidates
        )["snapshot"])
        self.assertEqual(receipt["candidates"], candidates)
        return receipt

    def _seed_true_host_route_for_production_plan(
        self, conn, plan, *, selected_class="finalist"
    ):
        self.assertEqual(
            plan["snapshot"]["current_batch_ids"],
            [plan["candidate"]["candidate_id"]],
        )
        history_audit_store.record_host_router_preplan(
            conn,
            run_id=plan["run_id"], batch_id=plan["batch_id"],
            intent=plan["intent"],
            history_as_of_watermark=plan["snapshot"][
                "history_as_of_watermark"
            ],
            exclusion_policy_sha=plan["snapshot"]["exclusion_policy_sha"],
            records=plan["snapshot"]["records"],
            candidates=[{
                "candidate_id": plan["candidate"]["candidate_id"],
                "raw_artifact_sha": plan["candidate"]["raw_artifact_sha"],
                "source_order": plan["candidate"]["source_order"],
            }],
            created_at=self._now(1),
        )
        route_round = history_audit_store.prepare_host_router_round(
            conn,
            run_id=plan["run_id"], batch_id=plan["batch_id"],
            intent=plan["intent"],
            raw_observations={
                "schema_version": "history-router-host-observations-v1",
                "selected_candidate_id": plan["candidate"]["candidate_id"],
                "members": [{
                    "candidate_id": plan["candidate"]["candidate_id"],
                    "selection_class": selected_class,
                    "channel_states": [
                        {"channel_id": "dense_core", "state": "complete"},
                        {"channel_id": "exact_lineage", "state": "complete"},
                        {"channel_id": "fts", "state": "complete"},
                    ],
                    "assigned_slice_ids": ["low_overlap"],
                    "permanent_request_id": None,
                }],
            },
            created_at=self._now(5),
        )
        history_audit_store.issue_host_router_domain_sources(
            conn, route_round["route_round_sha256"], phase="pre_l1",
            created_at=self._now(10),
        )
        pre = history_audit_store.derive_candidate_route_facts(
            conn, plan["run_id"], plan["batch_id"], plan["intent"],
            phase="pre_l1", created_at=self._now(20),
        )
        history_audit_store.issue_host_router_domain_sources(
            conn, route_round["route_round_sha256"], phase="final",
            created_at=self._now(30),
        )
        final = history_audit_store.derive_candidate_route_facts(
            conn, plan["run_id"], plan["batch_id"], plan["intent"],
            phase="final", created_at=self._now(40),
        )
        self.assertEqual(
            final["candidate_routes"][0]["matched_rule_ids"],
            plan["matched_router_rule_ids"],
        )
        self.assertEqual(
            final["candidate_routes"][0]["risk_policy_version"],
            plan["risk_policy_version"],
        )
        return route_round, pre, final

    def test_host_production_path_derives_sources_without_final_payload(self):
        plan = self._cohort_plan()
        additional = [self._additional_candidate()]
        self._seed_host_round_inputs(
            self.runtime, plan, additional_candidates=additional
        )
        prepare = getattr(
            history_audit_store, "prepare_host_router_round", None
        )
        issue = getattr(
            history_audit_store, "issue_host_router_domain_sources", None
        )
        self.assertTrue(callable(prepare))
        self.assertTrue(callable(issue))
        round_receipt = prepare(
            self.runtime.conn,
            run_id=plan["run_id"], batch_id=plan["batch_id"],
            intent=plan["intent"],
            raw_observations=self._host_observations(plan, additional),
            created_at=self._now(5),
        )
        dependencies = self._dependencies(round_receipt)
        history_audit_store.publish_semantic_dependency_heads(
            self.runtime.conn, dependencies, now=self._now(8)
        )
        issue(
            self.runtime.conn, round_receipt["route_round_sha256"],
            phase="pre_l1", created_at=self._now(10),
        )
        pre = self._derive(self.runtime, plan, "pre_l1", seconds=20)
        issue(
            self.runtime.conn, round_receipt["route_round_sha256"],
            phase="final", created_at=self._now(30),
        )
        final = self._derive(self.runtime, plan, "final", seconds=40)
        self.assertTrue(pre["candidate_routes"])
        self.assertTrue(final["candidate_routes"])
        self.assertEqual(
            self.runtime.conn.execute(
                "SELECT authority_scope FROM audit_router_rounds_v2"
            ).fetchone()[0],
            "test_fake",
        )
        self.assertEqual(
            self.runtime.conn.execute(
                "SELECT authority_scope FROM "
                "audit_router_host_round_authorities_v2"
            ).fetchone()[0],
            "host_production",
        )
        final_refs = json.loads(
            self.runtime.conn.execute(
                "SELECT source_refs_json FROM audit_router_source_sets_v2 "
                "WHERE phase='final'"
            ).fetchone()[0]
        )
        self.assertEqual(
            self.runtime.conn.execute(
                "SELECT count(*) FROM audit_router_host_source_authorities_v2 "
                "WHERE source_kind IN ("
                + ",".join("?" for _ in final_refs)
                + ")",
                tuple(final_refs),
            ).fetchone()[0],
            len(final_refs),
        )
        forged = self._host_observations(plan, additional)
        forged["matched_rule_ids"] = ["retriever_uncalibrated"]
        with self.assertRaises(history_audit_store.AuditMigrationError):
            prepare(
                self.runtime.conn,
                run_id="other-run", batch_id=plan["batch_id"],
                intent=plan["intent"], raw_observations=forged,
            )

    def test_host_l1_source_joins_exact_raw_sha_and_durable_comparator_fact(self):
        plan = self._cohort_plan()
        additional = [self._additional_candidate()]
        self._seed_host_round_inputs(
            self.runtime, plan, additional_candidates=additional
        )
        prepare = self._host_api("prepare_host_router_round")
        issue = self._host_api("issue_host_router_domain_sources")
        record_l1 = self._host_api("record_host_router_l1_observation")
        route_round = prepare(
            self.runtime.conn,
            run_id=plan["run_id"], batch_id=plan["batch_id"],
            intent=plan["intent"],
            raw_observations=self._host_observations(plan, additional),
            created_at=self._now(5),
        )
        self._install_host_shadow_calibration(
            self.runtime, route_round["route_round_sha256"]
        )
        issue(
            self.runtime.conn, route_round["route_round_sha256"],
            phase="pre_l1", created_at=self._now(10),
        )
        pre = self._derive(self.runtime, plan, "pre_l1", seconds=20)
        self.assertTrue(all(
            route["call_l1_model"] for route in pre["candidate_routes"]
        ))
        pre_by_candidate = {
            route["candidate_id"]: route
            for route in pre["candidate_routes"]
        }
        candidates = self._candidate_cohort(plan, additional)
        receipts = {}
        raw_by_candidate = {}
        for index, candidate in enumerate(candidates):
            raw = self._host_l1_raw_bytes(
                plan, route_round, candidate,
                pre_by_candidate[candidate["candidate_id"]],
                outcome="no_match" if index == 0 else "match",
            )
            raw_by_candidate[candidate["candidate_id"]] = raw
            with mock.patch.object(
                history_audit_store, "_utc_now",
                return_value=self._now(25 + index),
            ):
                receipts[candidate["candidate_id"]] = record_l1(
                    self.runtime.conn,
                    route_round_sha256=route_round["route_round_sha256"],
                    candidate_id=candidate["candidate_id"],
                    raw_observation_bytes=raw,
                )
        issue(
            self.runtime.conn, route_round["route_round_sha256"],
            phase="final", created_at=self._now(30),
        )
        final = self._derive(self.runtime, plan, "final", seconds=40)
        source_row = self.runtime.conn.execute(
            "SELECT source_json FROM audit_router_domain_sources_v2 "
            "WHERE route_round_sha256=? AND source_kind='l1_observation'",
            (route_round["route_round_sha256"],),
        ).fetchone()
        source = json.loads(source_row[0])
        members = {
            member["candidate_id"]: member for member in source["members"]
        }
        for candidate in candidates:
            candidate_id = candidate["candidate_id"]
            receipt = receipts[candidate_id]
            fact = self.runtime.conn.execute(
                "SELECT * FROM audit_router_host_l1_comparator_facts_v2 "
                "WHERE candidate_id=?", (candidate_id,),
            ).fetchone()
            self.assertEqual(
                fact["raw_comparator_artifact_sha256"],
                hashlib.sha256(raw_by_candidate[candidate_id]).hexdigest(),
            )
            self.assertEqual(
                bytes(fact["raw_comparator_artifact"]),
                raw_by_candidate[candidate_id],
            )
            self.assertEqual(
                members[candidate_id]["comparator_receipt_sha256"],
                fact["comparator_fact_sha256"],
            )
            self.assertEqual(
                receipt["comparator_fact_sha256"],
                fact["comparator_fact_sha256"],
            )
        self.assertEqual(
            [route["candidate_id"] for route in final["candidate_routes"]],
            plan["snapshot"]["current_batch_ids"],
        )
        script = textwrap.dedent(
            f"""
            import json
            import sqlite3
            import sys

            sys.path.insert(0, {str(ROOT)!r})
            from lib import history_audit_plan
            from lib import history_audit_store

            class Bomb(dict):
                def _explode(self, *args, **kwargs):
                    raise AssertionError("test authority map was accessed")
                __getitem__ = _explode
                get = _explode
                __contains__ = _explode
                __iter__ = _explode
                __len__ = _explode
                keys = _explode
                items = _explode
                values = _explode

            history_audit_plan._TEST_RUNTIME_AUTHORITIES = Bomb()
            history_audit_store._TEST_ROUTER_ROUND_AUTHORITIES = Bomb()
            conn = sqlite3.connect({str(self.runtime.db_path)!r})
            conn.row_factory = sqlite3.Row
            history_audit_store.init_schema(conn)
            facts = conn.execute(
                "SELECT * FROM audit_router_host_l1_comparator_facts_v2 "
                "ORDER BY candidate_id"
            ).fetchall()
            assert facts
            assert all(
                history_audit_store._router_host_l1_comparator_row_valid(
                    *tuple(row)
                ) == 1
                for row in facts
            )
            result = history_audit_store.derive_candidate_route_facts(
                conn, {plan['run_id']!r}, {plan['batch_id']!r},
                {plan['intent']!r}, phase="final",
                created_at={self._now(50)!r},
            )
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT, text=True,
            capture_output=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        replayed = json.loads(completed.stdout)
        self.assertEqual(
            replayed["source_set_sha256"], final["source_set_sha256"]
        )
        self.assertEqual(
            replayed["candidate_routes"], final["candidate_routes"]
        )

    def test_host_l1_batch_is_full_cohort_ordered_and_atomic(self):
        plan = self._cohort_plan()
        additional = [self._additional_candidate()]
        self._seed_host_round_inputs(
            self.runtime, plan, additional_candidates=additional
        )
        route_round = self._host_api("prepare_host_router_round")(
            self.runtime.conn,
            run_id=plan["run_id"], batch_id=plan["batch_id"],
            intent=plan["intent"],
            raw_observations=self._host_observations(plan, additional),
            created_at=self._now(5),
        )
        self._install_host_shadow_calibration(
            self.runtime, route_round["route_round_sha256"]
        )
        self._host_api("issue_host_router_domain_sources")(
            self.runtime.conn, route_round["route_round_sha256"],
            phase="pre_l1", created_at=self._now(10),
        )
        pre = self._derive(self.runtime, plan, "pre_l1", seconds=20)
        pre_by_candidate = {
            route["candidate_id"]: route
            for route in pre["candidate_routes"]
        }
        candidates = self._candidate_cohort(plan, additional)
        observations = [
            {
                "candidate_id": candidate["candidate_id"],
                "raw_observation_bytes": self._host_l1_raw_bytes(
                    plan, route_round, candidate,
                    pre_by_candidate[candidate["candidate_id"]],
                    outcome="no_match" if index == 0 else "match",
                ),
            }
            for index, candidate in enumerate(candidates)
        ]
        record_batch = self._host_api(
            "record_host_router_l1_observations"
        )
        mutated = copy.deepcopy(observations)
        raw = bytearray(mutated[1]["raw_observation_bytes"])
        marker = candidates[1]["candidate_hash"].encode("ascii")
        offset = raw.index(marker)
        raw[offset] = ord("0") if raw[offset] != ord("0") else ord("1")
        mutated[1]["raw_observation_bytes"] = bytes(raw)
        with self.assertRaisesRegex(
            history_audit_store.AuditMigrationError,
            "router_host_l1_identity_mismatch",
        ):
            record_batch(
                self.runtime.conn,
                route_round_sha256=route_round["route_round_sha256"],
                observations=mutated,
            )
        self.assertEqual(
            self.runtime.conn.execute(
                "SELECT count(*) FROM "
                "audit_router_host_l1_comparator_facts_v2"
            ).fetchone()[0],
            0,
        )
        for incomplete in (
            observations[:1], list(reversed(observations)),
        ):
            with self.assertRaisesRegex(
                history_audit_store.AuditMigrationError,
                "router_host_l1_cohort_mismatch",
            ):
                record_batch(
                    self.runtime.conn,
                    route_round_sha256=route_round["route_round_sha256"],
                    observations=incomplete,
                )
        insert_fact = (
            history_audit_store._insert_host_router_l1_comparator_fact
        )
        insertion_count = 0

        def fail_after_second_insert(conn, values):
            nonlocal insertion_count
            insert_fact(conn, values)
            insertion_count += 1
            if insertion_count == 2:
                raise RuntimeError("injected host L1 batch fault")

        with mock.patch.object(
            history_audit_store,
            "_insert_host_router_l1_comparator_fact",
            side_effect=fail_after_second_insert,
        ), self.assertRaisesRegex(
            RuntimeError, "injected host L1 batch fault"
        ):
            record_batch(
                self.runtime.conn,
                route_round_sha256=route_round["route_round_sha256"],
                observations=observations,
            )
        self.assertEqual(
            self.runtime.conn.execute(
                "SELECT count(*) FROM "
                "audit_router_host_l1_comparator_facts_v2"
            ).fetchone()[0],
            0,
        )
        with mock.patch.object(
            history_audit_store, "_utc_now", return_value=self._now(25)
        ):
            receipt = record_batch(
                self.runtime.conn,
                route_round_sha256=route_round["route_round_sha256"],
                observations=observations,
            )
        self.assertEqual(
            receipt["schema_version"],
            "history-router-host-l1-comparator-batch-receipt-v2",
        )
        self.assertEqual(
            [item["candidate_id"] for item in receipt["receipts"]],
            [candidate["candidate_id"] for candidate in candidates],
        )
        self.assertEqual(
            self.runtime.conn.execute(
                "SELECT count(*) FROM "
                "audit_router_host_l1_comparator_facts_v2"
            ).fetchone()[0],
            len(candidates),
        )

    def test_host_l1_empty_batch_is_valid_for_full_pre_l1_skip_cohort(self):
        plan = self._cohort_plan()
        additional = [self._additional_candidate()]
        self._seed_host_round_inputs(
            self.runtime, plan, additional_candidates=additional
        )
        route_round = self._host_api("prepare_host_router_round")(
            self.runtime.conn,
            run_id=plan["run_id"], batch_id=plan["batch_id"],
            intent=plan["intent"],
            raw_observations=self._host_observations(plan, additional),
            created_at=self._now(5),
        )
        issue = self._host_api("issue_host_router_domain_sources")
        issue(
            self.runtime.conn, route_round["route_round_sha256"],
            phase="pre_l1", created_at=self._now(10),
        )
        pre = self._derive(self.runtime, plan, "pre_l1", seconds=20)
        self.assertFalse(any(
            route["call_l1_model"] for route in pre["candidate_routes"]
        ))
        record_batch = self._host_api(
            "record_host_router_l1_observations"
        )
        for _ in range(2):
            receipt = record_batch(
                self.runtime.conn,
                route_round_sha256=route_round["route_round_sha256"],
                observations=[],
            )
            self.assertEqual(receipt["receipts"], [])
        issue(
            self.runtime.conn, route_round["route_round_sha256"],
            phase="final", created_at=self._now(30),
        )
        final = self._derive(self.runtime, plan, "final", seconds=40)
        self.assertFalse(any(
            route["router_facts"]["comparator_uncertain"]
            for route in final["candidate_routes"]
        ))
        source = json.loads(self.runtime.conn.execute(
            "SELECT source_json FROM audit_router_domain_sources_v2 "
            "WHERE route_round_sha256=? AND source_kind='l1_observation'",
            (route_round["route_round_sha256"],),
        ).fetchone()[0])
        self.assertTrue(all(
            member["observation_kind"] == "pre_l1_skip"
            for member in source["members"]
        ))
        self.assertEqual(
            self.runtime.conn.execute(
                "SELECT count(*) FROM "
                "audit_router_host_l1_comparator_facts_v2"
            ).fetchone()[0],
            0,
        )

    def test_host_prepare_receipt_is_verified_against_exact_durable_chain(self):
        plan = self._cohort_plan()
        additional = [self._additional_candidate()]
        preplan = self._seed_host_round_inputs(
            self.runtime, plan, additional_candidates=additional
        )
        raw_observations = self._host_observations(plan, additional)
        route_round = self._host_api("prepare_host_router_round")(
            self.runtime.conn,
            run_id=plan["run_id"], batch_id=plan["batch_id"],
            intent=plan["intent"],
            raw_observations=raw_observations,
            created_at=self._now(5),
        )
        self._host_api("issue_host_router_domain_sources")(
            self.runtime.conn, route_round["route_round_sha256"],
            phase="pre_l1", created_at=self._now(10),
        )
        pre = self._derive(self.runtime, plan, "pre_l1", seconds=20)
        routes = {
            route["candidate_id"]: route
            for route in pre["candidate_routes"]
        }
        schema = "history-router-host-cli-prepare-receipt-v1"
        input_schema = "history-router-host-cli-prepare-input-v1"
        prepare_input = {
            "schema_version": input_schema,
            "authority_scope": "host_production",
            "preplan": {
                "run_id": plan["run_id"],
                "batch_id": plan["batch_id"],
                "intent": plan["intent"],
                "history_as_of_watermark": plan["snapshot"][
                    "history_as_of_watermark"
                ],
                "exclusion_policy_sha": plan["snapshot"][
                    "exclusion_policy_sha"
                ],
                "records": history_audit_plan.runtime_snapshot_records(
                    plan["snapshot"]["records"]
                ),
                "candidates": [
                    {
                        "candidate_id": candidate["candidate_id"],
                        "raw_artifact_sha": candidate[
                            "raw_artifact_sha"
                        ],
                        "source_order": candidate["source_order"],
                    }
                    for candidate in preplan["candidates"]
                ],
            },
            "observations": raw_observations,
        }
        material = {
            "schema_version": schema,
            "authority_scope": "host_production",
            "input_sha256": history_contract_v2.framed_sha256(
                input_schema,
                history_contract_v2.canonical_bytes(prepare_input),
            ),
            "preplan_sha256": preplan["preplan_sha256"],
            "route_round_sha256": route_round["route_round_sha256"],
            "observation_set_sha256": route_round[
                "observation_set_sha256"
            ],
            "host_round_authority_sha256": route_round[
                "host_round_authority_sha256"
            ],
            "pre_l1_source_set_sha256": pre["source_set_sha256"],
            "run_id": plan["run_id"],
            "batch_id": plan["batch_id"],
            "intent": plan["intent"],
            "snapshot_id": plan["snapshot"]["snapshot_id"],
            "snapshot_hash": plan["snapshot"]["snapshot_hash"],
            "candidates": [
                {
                    "candidate_id": candidate["candidate_id"],
                    "candidate_hash": candidate["candidate_hash"],
                    "raw_artifact_sha": candidate["raw_artifact_sha"],
                    "source_order": candidate["source_order"],
                    "pre_phase_fact_sha256": routes[
                        candidate["candidate_id"]
                    ]["phase_fact_sha256"],
                    "call_l1_model": routes[
                        candidate["candidate_id"]
                    ]["call_l1_model"],
                }
                for candidate in preplan["candidates"]
            ],
        }
        receipt = dict(material)
        receipt["receipt_sha256"] = history_contract_v2.framed_sha256(
            schema, history_contract_v2.canonical_bytes(material)
        )
        verify = self._host_api("verify_host_router_prepare_receipt")
        verified = verify(self.runtime.conn, receipt)
        self.assertEqual(verified, receipt)

        tampered = copy.deepcopy(receipt)
        tampered["run_id"] = "forged-run"
        unsigned = copy.deepcopy(tampered)
        unsigned.pop("receipt_sha256")
        tampered["receipt_sha256"] = history_contract_v2.framed_sha256(
            schema, history_contract_v2.canonical_bytes(unsigned)
        )
        with self.assertRaisesRegex(
            history_audit_store.AuditMigrationError,
            "router_host_prepare_receipt_mismatch",
        ):
            verify(self.runtime.conn, tampered)
        tampered_input = copy.deepcopy(receipt)
        tampered_input["input_sha256"] = sha("forged-prepare-input")
        unsigned = copy.deepcopy(tampered_input)
        unsigned.pop("receipt_sha256")
        tampered_input["receipt_sha256"] = (
            history_contract_v2.framed_sha256(
                schema, history_contract_v2.canonical_bytes(unsigned)
            )
        )
        with self.assertRaisesRegex(
            history_audit_store.AuditMigrationError,
            "router_host_prepare_receipt_mismatch",
        ):
            verify(self.runtime.conn, tampered_input)
        self.assertEqual(
            self.runtime.conn.execute(
                "SELECT count(*) FROM "
                "audit_router_host_l1_comparator_facts_v2"
            ).fetchone()[0],
            0,
        )
        self.assertIsNone(self.runtime.conn.execute(
            "SELECT 1 FROM audit_router_domain_sources_v2 "
            "WHERE route_round_sha256=? "
            "AND source_kind='l1_observation'",
            (route_round["route_round_sha256"],),
        ).fetchone())

    def test_host_sources_drive_uncertain_and_permanent_request_rules(self):
        with self._fresh_runtime() as runtime:
            plan = self._cohort_plan(runtime)
            additional = [self._additional_candidate()]
            self._seed_host_round_inputs(
                runtime, plan, additional_candidates=additional
            )
            route_round = self._host_api("prepare_host_router_round")(
                runtime.conn,
                run_id=plan["run_id"], batch_id=plan["batch_id"],
                intent=plan["intent"],
                raw_observations=self._host_observations(plan, additional),
                created_at=self._now(5),
            )
            self._install_host_shadow_calibration(
                runtime, route_round["route_round_sha256"]
            )
            issue = self._host_api("issue_host_router_domain_sources")
            issue(
                runtime.conn, route_round["route_round_sha256"],
                phase="pre_l1", created_at=self._now(10),
            )
            pre = self._derive(runtime, plan, "pre_l1", seconds=20)
            pre_by_candidate = {
                route["candidate_id"]: route
                for route in pre["candidate_routes"]
            }
            candidates = self._candidate_cohort(plan, additional)
            self._host_api("record_host_router_l1_observations")(
                runtime.conn,
                route_round_sha256=route_round["route_round_sha256"],
                observations=[
                    {
                        "candidate_id": candidate["candidate_id"],
                        "raw_observation_bytes": self._host_l1_raw_bytes(
                            plan, route_round, candidate,
                            pre_by_candidate[candidate["candidate_id"]],
                            outcome=(
                                "uncertain"
                                if candidate["candidate_id"]
                                == plan["candidate"]["candidate_id"]
                                else "no_match"
                            ),
                        ),
                    }
                    for candidate in candidates
                ],
            )
            issue(
                runtime.conn, route_round["route_round_sha256"],
                phase="final", created_at=self._now(30),
            )
            final = self._derive(runtime, plan, "final", seconds=40)
            selected = next(
                route for route in final["candidate_routes"]
                if route["candidate_id"]
                == plan["candidate"]["candidate_id"]
            )
            self.assertTrue(
                selected["router_facts"]["comparator_uncertain"]
            )
            self.assertIn(
                "comparator_uncertain", selected["matched_rule_ids"]
            )
            self.assertFalse(selected["release_authorized"])

        with self._fresh_runtime() as runtime:
            plan = self._cohort_plan(runtime)
            additional = [self._additional_candidate()]
            self._seed_host_round_inputs(
                runtime, plan, additional_candidates=additional
            )
            raw_observations = self._host_observations(plan, additional)
            request_id = sha("host-permanent-request")
            next(
                member for member in raw_observations["members"]
                if member["candidate_id"]
                == plan["candidate"]["candidate_id"]
            )["permanent_request_id"] = request_id
            route_round = self._host_api("prepare_host_router_round")(
                runtime.conn,
                run_id=plan["run_id"], batch_id=plan["batch_id"],
                intent=plan["intent"], raw_observations=raw_observations,
                created_at=self._now(5),
            )
            issue = self._host_api("issue_host_router_domain_sources")
            issue(
                runtime.conn, route_round["route_round_sha256"],
                phase="pre_l1", created_at=self._now(10),
            )
            pre = self._derive(runtime, plan, "pre_l1", seconds=20)
            self.assertFalse(any(
                route["call_l1_model"]
                for route in pre["candidate_routes"]
            ))
            self._host_api("record_host_router_l1_observations")(
                runtime.conn,
                route_round_sha256=route_round["route_round_sha256"],
                observations=[],
            )
            issue(
                runtime.conn, route_round["route_round_sha256"],
                phase="final", created_at=self._now(30),
            )
            final = self._derive(runtime, plan, "final", seconds=40)
            selected = next(
                route for route in final["candidate_routes"]
                if route["candidate_id"]
                == plan["candidate"]["candidate_id"]
            )
            self.assertTrue(
                selected["router_facts"]["permanent_no_match_requested"]
            )
            self.assertIn(
                "permanent_no_match_without_release_gate",
                selected["matched_rule_ids"],
            )
            self.assertFalse(selected["release_authorized"])
            request_source = json.loads(runtime.conn.execute(
                "SELECT source_json FROM audit_router_domain_sources_v2 "
                "WHERE route_round_sha256=? "
                "AND source_kind='permanent_request'",
                (route_round["route_round_sha256"],),
            ).fetchone()[0])
            member = next(
                item for item in request_source["members"]
                if item["candidate_id"]
                == plan["candidate"]["candidate_id"]
            )
            self.assertEqual(member, {
                "candidate_id": plan["candidate"]["candidate_id"],
                "request_state": "requested",
                "request_id": request_id,
            })

    def test_host_raw_observations_cannot_supply_router_rule_budget_or_release_outputs(
        self,
    ):
        forbidden_observations = {
            "matched_rule_ids": ["retriever_uncalibrated"],
            "route": "exhaustive",
            "call_l1_model": False,
            "dispatch_allowed": True,
            "release_authorized": True,
            "candidate_budget_available": True,
            "attempt_budget_available": True,
            "budget_policy_sha": sha("caller-budget-policy"),
            "member.release_authorized": True,
        }
        for field, value in forbidden_observations.items():
            with self.subTest(field=field), self._fresh_runtime() as runtime:
                plan = self._cohort_plan(runtime)
                additional = [self._additional_candidate()]
                self._seed_host_round_inputs(
                    runtime, plan, additional_candidates=additional
                )
                raw = self._host_observations(plan, additional)
                if field.startswith("member."):
                    raw["members"][0][field.split(".", 1)[1]] = value
                else:
                    raw[field] = value
                prepare = self._host_api("prepare_host_router_round")
                with self.assertRaisesRegex(
                    history_audit_store.AuditMigrationError,
                    "router_host_observation_schema_mismatch",
                ):
                    prepare(
                        runtime.conn,
                        run_id=plan["run_id"],
                        batch_id=plan["batch_id"],
                        intent=plan["intent"],
                        raw_observations=raw,
                        created_at=self._now(5),
                    )
                for table in (
                    "audit_router_host_observation_sets_v2",
                    "audit_router_host_round_authorities_v2",
                    "audit_router_rounds_v2",
                    "audit_router_budget_facts_v2",
                ):
                    self.assertEqual(
                        runtime.conn.execute(
                            f"SELECT count(*) FROM {table}"
                        ).fetchone()[0],
                        0,
                    )

    def test_host_l1_rejects_mutation_cross_stitch_naked_sha_and_retrofit(self):
        plan = self._cohort_plan()
        additional = [self._additional_candidate()]
        self._seed_host_round_inputs(
            self.runtime, plan, additional_candidates=additional
        )
        route_round = self._host_api("prepare_host_router_round")(
            self.runtime.conn,
            run_id=plan["run_id"], batch_id=plan["batch_id"],
            intent=plan["intent"],
            raw_observations=self._host_observations(plan, additional),
            created_at=self._now(5),
        )
        self._install_host_shadow_calibration(
            self.runtime, route_round["route_round_sha256"]
        )
        issue = self._host_api("issue_host_router_domain_sources")
        issue(
            self.runtime.conn, route_round["route_round_sha256"],
            phase="pre_l1", created_at=self._now(10),
        )
        pre = self._derive(self.runtime, plan, "pre_l1", seconds=20)
        pre_by_candidate = {
            route["candidate_id"]: route
            for route in pre["candidate_routes"]
        }
        candidates = self._candidate_cohort(plan, additional)
        first, second = candidates
        raw = self._host_l1_raw_bytes(
            plan, route_round, first, pre_by_candidate[first["candidate_id"]]
        )
        record_l1 = self._host_api("record_host_router_l1_observation")

        mutated = bytearray(raw)
        marker = first["candidate_hash"].encode("ascii")
        offset = raw.index(marker)
        mutated[offset] = ord("0") if mutated[offset] != ord("0") else ord("1")
        with self.assertRaisesRegex(
            history_audit_store.AuditMigrationError,
            "router_host_l1_identity_mismatch",
        ):
            record_l1(
                self.runtime.conn,
                route_round_sha256=route_round["route_round_sha256"],
                candidate_id=first["candidate_id"],
                raw_observation_bytes=bytes(mutated),
            )
        with self.assertRaisesRegex(
            history_audit_store.AuditMigrationError,
            "router_host_l1_identity_mismatch",
        ):
            record_l1(
                self.runtime.conn,
                route_round_sha256=route_round["route_round_sha256"],
                candidate_id=second["candidate_id"],
                raw_observation_bytes=raw,
            )
        with self.assertRaisesRegex(
            history_audit_store.AuditMigrationError,
            "router_host_l1_observation_invalid",
        ):
            record_l1(
                self.runtime.conn,
                route_round_sha256=route_round["route_round_sha256"],
                candidate_id=first["candidate_id"],
                raw_observation_bytes=hashlib.sha256(raw).hexdigest().encode(
                    "ascii"
                ),
            )
        self.assertEqual(
            self.runtime.conn.execute(
                "SELECT count(*) FROM "
                "audit_router_host_l1_comparator_facts_v2"
            ).fetchone()[0],
            0,
        )
        with mock.patch.object(
            history_audit_store, "_utc_now", return_value=self._now(25)
        ):
            record_l1(
                self.runtime.conn,
                route_round_sha256=route_round["route_round_sha256"],
                candidate_id=first["candidate_id"],
                raw_observation_bytes=raw,
            )
        conflicting = self._host_l1_raw_bytes(
            plan, route_round, first, pre_by_candidate[first["candidate_id"]],
            outcome="uncertain",
        )
        with self.assertRaisesRegex(
            history_audit_store.AuditMigrationError,
            "router_host_l1_observation_conflict",
        ):
            record_l1(
                self.runtime.conn,
                route_round_sha256=route_round["route_round_sha256"],
                candidate_id=first["candidate_id"],
                raw_observation_bytes=conflicting,
            )

        issue(
            self.runtime.conn, route_round["route_round_sha256"],
            phase="final", created_at=self._now(30),
        )
        final = self._derive(self.runtime, plan, "final", seconds=40)
        l1_source = json.loads(self.runtime.conn.execute(
            "SELECT source_json FROM audit_router_domain_sources_v2 "
            "WHERE route_round_sha256=? AND source_kind='l1_observation'",
            (route_round["route_round_sha256"],),
        ).fetchone()[0])
        unavailable = next(
            member for member in l1_source["members"]
            if member["candidate_id"] == second["candidate_id"]
        )
        self.assertEqual(unavailable, {
            "candidate_id": second["candidate_id"],
            "observation_kind": "unavailable",
            "unavailable_reason": "comparator_fact_missing",
            "coverage_state": "unavailable",
            "pre_phase_fact_sha256": pre_by_candidate[
                second["candidate_id"]
            ]["phase_fact_sha256"],
        })
        late_raw = self._host_l1_raw_bytes(
            plan, route_round, second,
            pre_by_candidate[second["candidate_id"]],
        )
        with self.assertRaisesRegex(
            history_audit_store.AuditMigrationError,
            "router_host_l1_source_already_final",
        ):
            record_l1(
                self.runtime.conn,
                route_round_sha256=route_round["route_round_sha256"],
                candidate_id=second["candidate_id"],
                raw_observation_bytes=late_raw,
            )
        self.assertTrue(
            next(
                route for route in final["candidate_routes"]
                if route["candidate_id"] == second["candidate_id"]
            )["router_facts"]["comparator_uncertain"]
        )

        with self._fresh_runtime() as fake_runtime:
            fake_plan = self._foreign_plan(self._cohort_plan(fake_runtime))
            fake_round = self._prepare(fake_runtime, fake_plan)
            with self.assertRaisesRegex(
                history_audit_store.AuditMigrationError,
                "router_host_l1_identity_mismatch",
            ):
                record_l1(
                    fake_runtime.conn,
                    route_round_sha256=fake_round["route_round_sha256"],
                    candidate_id=first["candidate_id"],
                    raw_observation_bytes=raw,
                )
            fake_sources = self._domain_sources(
                fake_plan, fake_round, calibrated=True
            )
            self._issue(
                fake_runtime, fake_round,
                {
                    kind: source for kind, source in fake_sources.items()
                    if kind != "l1_observation"
                },
                seconds=10,
            )
            fake_pre = self._derive(
                fake_runtime, fake_plan, "pre_l1", seconds=20
            )
            fake_candidate = self._candidate_cohort(
                fake_plan
            )[0]
            fake_route = dict(fake_round)
            fake_route["host_round_authority_sha256"] = sha(
                "forged-host-round-authority"
            )
            fake_raw = self._host_l1_raw_bytes(
                fake_plan, fake_route, fake_candidate,
                next(
                    route for route in fake_pre["candidate_routes"]
                    if route["candidate_id"] == fake_candidate["candidate_id"]
                ),
            )
            with self.assertRaisesRegex(
                history_audit_store.AuditMigrationError,
                "router_host_l1_authority_unavailable",
            ):
                record_l1(
                    fake_runtime.conn,
                    route_round_sha256=fake_round["route_round_sha256"],
                    candidate_id=fake_candidate["candidate_id"],
                    raw_observation_bytes=fake_raw,
                )
            self.assertEqual(
                fake_runtime.conn.execute(
                    "SELECT count(*) FROM "
                    "audit_router_host_l1_comparator_facts_v2"
                ).fetchone()[0],
                0,
            )

    def test_host_final_source_set_requires_exact_seven_kinds_and_full_cohort(
        self,
    ):
        with self._fresh_runtime() as runtime:
            plan = self._cohort_plan(runtime)
            additional = [self._additional_candidate()]
            self._seed_host_round_inputs(
                runtime, plan, additional_candidates=additional
            )
            raw = self._host_observations(plan, additional)
            raw["members"].pop()
            prepare = self._host_api("prepare_host_router_round")
            with self.assertRaisesRegex(
                history_audit_store.AuditMigrationError,
                "router_host_observation_cohort_mismatch",
            ):
                prepare(
                    runtime.conn,
                    run_id=plan["run_id"],
                    batch_id=plan["batch_id"],
                    intent=plan["intent"],
                    raw_observations=raw,
                    created_at=self._now(5),
                )
            self.assertEqual(
                runtime.conn.execute(
                    "SELECT count(*) FROM audit_router_rounds_v2"
                ).fetchone()[0],
                0,
            )

        plan = self._cohort_plan()
        additional = [self._additional_candidate()]
        self._seed_host_round_inputs(
            self.runtime, plan, additional_candidates=additional
        )
        prepare = self._host_api("prepare_host_router_round")
        issue = self._host_api("issue_host_router_domain_sources")
        route_round = prepare(
            self.runtime.conn,
            run_id=plan["run_id"],
            batch_id=plan["batch_id"],
            intent=plan["intent"],
            raw_observations=self._host_observations(plan, additional),
            created_at=self._now(5),
        )
        issue(
            self.runtime.conn,
            route_round["route_round_sha256"],
            phase="pre_l1",
            created_at=self._now(10),
        )
        self._derive(self.runtime, plan, "pre_l1", seconds=20)
        issue(
            self.runtime.conn,
            route_round["route_round_sha256"],
            phase="final",
            created_at=self._now(30),
        )
        final = self._derive(self.runtime, plan, "final", seconds=40)
        expected_kinds = set(history_audit_store._ROUTER_SOURCE_KINDS)
        self.assertEqual(len(expected_kinds), 7)
        source_set = self.runtime.conn.execute(
            "SELECT source_refs_json FROM audit_router_source_sets_v2 "
            "WHERE route_round_sha256=? AND phase='final'",
            (route_round["route_round_sha256"],),
        ).fetchone()
        self.assertIsNotNone(source_set)
        self.assertEqual(set(json.loads(source_set[0])), expected_kinds)
        self.assertEqual(
            {
                row[0]
                for row in self.runtime.conn.execute(
                    "SELECT source_kind FROM audit_router_domain_sources_v2 "
                    "WHERE route_round_sha256=?",
                    (route_round["route_round_sha256"],),
                )
            },
            expected_kinds,
        )
        self.assertEqual(
            {
                row[0]
                for row in self.runtime.conn.execute(
                    "SELECT source_kind FROM "
                    "audit_router_host_source_authorities_v2 "
                    "WHERE route_round_sha256=?",
                    (route_round["route_round_sha256"],),
                )
            },
            expected_kinds,
        )
        cohort = plan["snapshot"]["current_batch_ids"]
        self.assertEqual(
            [route["candidate_id"] for route in final["candidate_routes"]],
            cohort,
        )
        for kind in (
            "selection", "l1_observation", "risk_assignment",
            "permanent_request",
        ):
            source_json = self.runtime.conn.execute(
                "SELECT source_json FROM audit_router_domain_sources_v2 "
                "WHERE route_round_sha256=? AND source_kind=?",
                (route_round["route_round_sha256"], kind),
            ).fetchone()[0]
            self.assertEqual(json.loads(source_json)["candidate_ids"], cohort)

    def test_host_sources_derive_exact_ordered_rules_and_local_invalidation(self):
        def derive_with(runtime, *, mutate_second=False):
            plan = self._cohort_plan(runtime)
            additional = [self._additional_candidate()]
            self._seed_host_round_inputs(
                runtime, plan, additional_candidates=additional
            )
            raw = self._host_observations(plan, additional)
            for member in raw["members"]:
                member["assigned_slice_ids"] = []
            if mutate_second:
                second = next(
                    member for member in raw["members"]
                    if member["candidate_id"]
                        != plan["candidate"]["candidate_id"]
                )
                second["channel_states"][2]["state"] = "failed"
                second["assigned_slice_ids"] = ["low_overlap"]
            route_round = self._host_api("prepare_host_router_round")(
                runtime.conn,
                run_id=plan["run_id"], batch_id=plan["batch_id"],
                intent=plan["intent"], raw_observations=raw,
                created_at=self._now(5),
            )
            issue = self._host_api("issue_host_router_domain_sources")
            issue(
                runtime.conn, route_round["route_round_sha256"],
                phase="pre_l1", created_at=self._now(10),
            )
            self._derive(runtime, plan, "pre_l1", seconds=20)
            issue(
                runtime.conn, route_round["route_round_sha256"],
                phase="final", created_at=self._now(30),
            )
            final = self._derive(runtime, plan, "final", seconds=40)
            return plan, {
                route["candidate_id"]: route
                for route in final["candidate_routes"]
            }

        with self._fresh_runtime() as baseline_runtime:
            baseline_plan, baseline = derive_with(baseline_runtime)
        with self._fresh_runtime() as changed_runtime:
            changed_plan, changed = derive_with(
                changed_runtime, mutate_second=True
            )
        self.assertEqual(
            baseline_plan["snapshot"]["current_batch_ids"],
            changed_plan["snapshot"]["current_batch_ids"],
        )
        selected_id = baseline_plan["candidate"]["candidate_id"]
        second_id = next(
            candidate_id
            for candidate_id in baseline_plan["snapshot"]["current_batch_ids"]
            if candidate_id != selected_id
        )
        semantic_route_fields = (
            "router_facts", "risk_slices", "matched_rule_ids", "route",
            "call_l1_model", "dispatch_allowed", "release_authorized",
            "rule_table_sha256", "risk_policy_version",
        )
        self.assertEqual(
            {
                name: baseline[selected_id][name]
                for name in semantic_route_fields
            },
            {
                name: changed[selected_id][name]
                for name in semantic_route_fields
            },
        )
        self.assertNotEqual(
            baseline[selected_id]["source_set_sha256"],
            changed[selected_id]["source_set_sha256"],
        )
        differing_facts = {
            name for name in baseline[second_id]["router_facts"]
            if baseline[second_id]["router_facts"][name]
                != changed[second_id]["router_facts"][name]
        }
        self.assertEqual(
            differing_facts,
            {"mandatory_channel_failed", "bad_slice_membership"},
        )
        policy = history_audit_plan._host_runtime_authority()["risk_policy"]
        for routes in (baseline, changed):
            for route in routes.values():
                replay = history_audit_eval_v2.route_candidate(
                    route["router_facts"], policy
                )
                self.assertEqual(
                    route["matched_rule_ids"], replay["matched_rule_ids"]
                )
                positions = [
                    next(
                        index for index, rule in enumerate(policy["rules"])
                        if rule["rule_id"] == rule_id
                    )
                    for rule_id in route["matched_rule_ids"]
                ]
                self.assertEqual(positions, sorted(positions))
                self.assertFalse(route["release_authorized"])
        self.assertEqual(
            changed[second_id]["matched_rule_ids"],
            [
                "retriever_uncalibrated", "mandatory_channel_failed",
                "bad_slice_membership",
            ],
        )

    def test_host_final_route_plan_binding_dispatch_are_exact_and_cross_stitch_proof(
        self,
    ):
        with EVAL_FIXTURE.future_production_plan(
            self.runtime.conn, install_host_router=False,
        ) as (_, plan):
            _, _, final = self._seed_true_host_route_for_production_plan(
                self.runtime.conn, plan
            )
            persisted = RUNTIME_FIXTURE.history_execution.persist_plan(
                self.runtime.conn, plan
            )
            self.assertTrue(persisted)
            selected = final["candidate_routes"][0]
            joined = self.runtime.conn.execute(
                """
                SELECT phase.phase_fact_sha256,
                       binding.final_phase_fact_sha256,
                       binding.source_set_sha256,
                       route.fact_sha256 AS route_fact_sha256,
                       dispatch.route_fact_sha256 AS dispatch_route_sha,
                       dispatch.plan_sha,plan.plan_sha AS stored_plan_sha
                FROM audit_router_phase_facts_v2 phase
                JOIN audit_candidate_route_source_bindings_v2 binding
                  ON binding.final_phase_fact_sha256=phase.phase_fact_sha256
                JOIN audit_candidate_route_facts_v2 route
                  ON route.run_id=binding.run_id
                 AND route.candidate_id=binding.candidate_id
                 AND route.fact_sha256=binding.route_fact_sha256
                JOIN audit_candidate_l2_dispatch_facts_v2 dispatch
                  ON dispatch.run_id=route.run_id
                 AND dispatch.candidate_id=route.candidate_id
                 AND dispatch.route_fact_sha256=route.fact_sha256
                JOIN audit_l2_plans_v2 plan
                  ON plan.plan_sha=dispatch.plan_sha
                WHERE phase.route_round_sha256=?
                  AND phase.phase='final'
                  AND phase.candidate_id=?
                """,
                (
                    final["route_round_sha256"],
                    plan["candidate"]["candidate_id"],
                ),
            ).fetchone()
            self.assertIsNotNone(joined)
            self.assertEqual(
                joined["phase_fact_sha256"], selected["phase_fact_sha256"]
            )
            self.assertEqual(
                joined["phase_fact_sha256"],
                joined["final_phase_fact_sha256"],
            )
            self.assertEqual(
                joined["source_set_sha256"], final["source_set_sha256"]
            )
            self.assertEqual(
                joined["route_fact_sha256"], joined["dispatch_route_sha"]
            )
            self.assertEqual(
                joined["plan_sha"], joined["stored_plan_sha"]
            )

        with self._fresh_runtime() as mismatch_runtime:
            with EVAL_FIXTURE.future_production_plan(
                mismatch_runtime.conn, install_host_router=False,
            ) as (_, plan):
                self._seed_true_host_route_for_production_plan(
                    mismatch_runtime.conn, plan
                )
                mismatched = copy.deepcopy(plan)
                mismatched["matched_router_rule_ids"] = [
                    "retriever_uncalibrated"
                ]
                mismatched["plan_sha"] = (
                    history_audit_plan.runtime_plan_sha(mismatched)
                )
                mismatched["logical_task_keys"] = [
                    history_contract_v2.logical_task_key(
                        mismatched["plan_sha"], "map",
                        mismatched["candidate"]["candidate_id"],
                        shard["request_sha256"],
                    )
                    for shard in mismatched["shards"]
                ]
                with self.assertRaises(
                    RUNTIME_FIXTURE.history_execution.ExecutionError
                ) as caught:
                    RUNTIME_FIXTURE.history_execution.persist_plan(
                        mismatch_runtime.conn, mismatched
                    )
                self.assertEqual(
                    caught.exception.code, "host_route_authority_required"
                )
                for table in (
                    "audit_candidate_budget_receipts_v2",
                    "audit_run_manifests", "audit_l2_plans_v2",
                    "audit_candidate_route_source_bindings_v2",
                    "audit_candidate_l2_dispatch_facts_v2",
                    "audit_logical_tasks",
                    "audit_runtime_budget_reservations_v2",
                ):
                    self.assertEqual(
                        mismatch_runtime.conn.execute(
                            f"SELECT count(*) FROM {table}"
                        ).fetchone()[0],
                        0,
                        table,
                    )

    def test_two_true_host_rounds_cannot_cross_stitch_final_chain(self):
        with EVAL_FIXTURE.future_production_plan(
            self.runtime.conn, approve_production_evidence=False,
            install_host_router=False,
        ) as (_, plan_a):
            self._seed_true_host_route_for_production_plan(
                self.runtime.conn, plan_a
            )
            RUNTIME_FIXTURE.history_execution.persist_plan(
                self.runtime.conn, plan_a
            )
            plan_b = self._foreign_plan(plan_a)
            round_b, _, final_b = (
                self._seed_true_host_route_for_production_plan(
                    self.runtime.conn, plan_b
                )
            )
            RUNTIME_FIXTURE.history_execution.persist_plan(
                self.runtime.conn, plan_b
            )
            a_candidate_id = plan_a["candidate"]["candidate_id"]
            b_candidate_id = plan_b["candidate"]["candidate_id"]
            chain_rows = {}
            for label, plan, candidate_id in (
                ("a", plan_a, a_candidate_id),
                ("b", plan_b, b_candidate_id),
            ):
                joined = self.runtime.conn.execute(
                    """
                    SELECT source_set.*,phase.*,route.*,binding.*,
                           plan.*,dispatch.*
                    FROM audit_l2_plans_v2 plan
                    JOIN audit_candidate_l2_dispatch_facts_v2 dispatch
                      ON dispatch.plan_sha=plan.plan_sha
                    JOIN audit_candidate_route_facts_v2 route
                      ON route.run_id=dispatch.run_id
                     AND route.candidate_id=dispatch.candidate_id
                     AND route.fact_sha256=dispatch.route_fact_sha256
                    JOIN audit_candidate_route_source_bindings_v2 binding
                      ON binding.run_id=route.run_id
                     AND binding.candidate_id=route.candidate_id
                     AND binding.route_fact_sha256=route.fact_sha256
                    JOIN audit_router_phase_facts_v2 phase
                      ON phase.phase_fact_sha256=
                         binding.final_phase_fact_sha256
                     AND phase.candidate_id=route.candidate_id
                     AND phase.source_set_sha256=binding.source_set_sha256
                    JOIN audit_router_source_sets_v2 source_set
                      ON source_set.source_set_sha256=
                         binding.source_set_sha256
                     AND source_set.route_round_sha256=
                         phase.route_round_sha256
                    WHERE plan.plan_sha=? AND plan.candidate_id=?
                    """,
                    (plan["plan_sha"], candidate_id),
                ).fetchone()
                self.assertIsNotNone(joined, label)
                self.assertTrue(
                    RUNTIME_FIXTURE.history_execution
                    ._has_route_dispatch_authority(
                        self.runtime.conn, plan["plan_sha"]
                    ),
                    label,
                )
                chain_rows[label] = joined

            b_phase_sha = final_b["candidate_routes"][0][
                "phase_fact_sha256"
            ]
            b_source_set_sha = final_b["source_set_sha256"]
            b_route = tuple(self.runtime.conn.execute(
                "SELECT * FROM audit_candidate_route_facts_v2 "
                "WHERE run_id=? AND candidate_id=?",
                (plan_b["run_id"], b_candidate_id),
            ).fetchone())
            b_binding = tuple(self.runtime.conn.execute(
                "SELECT * FROM audit_candidate_route_source_bindings_v2 "
                "WHERE run_id=? AND candidate_id=?",
                (plan_b["run_id"], b_candidate_id),
            ).fetchone())
            b_plan = tuple(self.runtime.conn.execute(
                "SELECT * FROM audit_l2_plans_v2 WHERE plan_sha=?",
                (plan_b["plan_sha"],),
            ).fetchone())
            b_dispatch = tuple(self.runtime.conn.execute(
                "SELECT * FROM audit_candidate_l2_dispatch_facts_v2 "
                "WHERE plan_sha=?",
                (plan_b["plan_sha"],),
            ).fetchone())
            self.assertEqual(b_binding[2], b_route[12])
            self.assertEqual(b_binding[3], b_phase_sha)
            self.assertEqual(b_binding[4], b_source_set_sha)
            self.assertEqual(b_dispatch[0], b_plan[0])
            self.assertEqual(b_dispatch[3], b_route[12])

            durable_before_attacks = "\n".join(
                self.runtime.conn.iterdump()
            ).encode("utf-8")
            public_attacks = []
            foreign_candidate = copy.deepcopy(plan_a)
            foreign_candidate["candidate"] = copy.deepcopy(
                plan_b["candidate"]
            )
            public_attacks.append(("candidate", foreign_candidate, None))
            foreign_plan = copy.deepcopy(plan_a)
            foreign_plan["plan_sha"] = plan_b["plan_sha"]
            foreign_plan["logical_task_keys"] = copy.deepcopy(
                plan_b["logical_task_keys"]
            )
            public_attacks.append(("plan", foreign_plan, None))
            public_attacks.append((
                "caller_chain",
                plan_a,
                {
                    "candidate": copy.deepcopy(plan_b["candidate"]),
                    "source_set_sha256": b_source_set_sha,
                    "final_phase_fact_sha256": b_phase_sha,
                    "route_fact": b_route,
                    "binding": b_binding,
                    "plan": b_plan,
                    "dispatch": b_dispatch,
                },
            ))
            for label, attack_plan, route_authority in public_attacks:
                with self.subTest(public_entry=label), self.assertRaises(
                    RUNTIME_FIXTURE.history_execution.ExecutionError
                ):
                    RUNTIME_FIXTURE.history_execution.persist_plan(
                        self.runtime.conn,
                        attack_plan,
                        route_authority=route_authority,
                    )
                if self.runtime.conn.in_transaction:
                    self.runtime.conn.rollback()
                self.assertEqual(
                    "\n".join(self.runtime.conn.iterdump()).encode("utf-8"),
                    durable_before_attacks,
                    label,
                )

            direct_attacks = (
                (
                    "source_set",
                    "UPDATE audit_candidate_route_source_bindings_v2 "
                    "SET source_set_sha256=? WHERE run_id=? AND candidate_id=?",
                    (b_source_set_sha, plan_a["run_id"], a_candidate_id),
                ),
                (
                    "final_phase",
                    "UPDATE audit_candidate_route_source_bindings_v2 "
                    "SET final_phase_fact_sha256=? "
                    "WHERE run_id=? AND candidate_id=?",
                    (b_phase_sha, plan_a["run_id"], a_candidate_id),
                ),
                (
                    "route_fact",
                    "UPDATE audit_candidate_route_facts_v2 SET fact_sha256=? "
                    "WHERE run_id=? AND candidate_id=?",
                    (b_route[12], plan_a["run_id"], a_candidate_id),
                ),
                (
                    "binding",
                    "UPDATE audit_candidate_route_source_bindings_v2 "
                    "SET route_fact_sha256=?,final_phase_fact_sha256=?,"
                    "source_set_sha256=?,bound_at=? "
                    "WHERE run_id=? AND candidate_id=?",
                    (
                        b_binding[2], b_binding[3], b_binding[4], b_binding[5],
                        plan_a["run_id"], a_candidate_id,
                    ),
                ),
                (
                    "plan",
                    "UPDATE audit_l2_plans_v2 SET plan_sha=? WHERE plan_sha=?",
                    (b_plan[0], plan_a["plan_sha"]),
                ),
                (
                    "dispatch",
                    "UPDATE audit_candidate_l2_dispatch_facts_v2 "
                    "SET plan_sha=?,run_id=?,candidate_id=?,"
                    "route_fact_sha256=?,dispatch_sha256=?,created_at=? "
                    "WHERE plan_sha=?",
                    (*b_dispatch, plan_a["plan_sha"]),
                ),
            )
            for label, statement, values in direct_attacks:
                with self.subTest(durable_field=label), self.assertRaises(
                    sqlite3.DatabaseError
                ):
                    self.runtime.conn.execute(statement, values)
                self.runtime.conn.rollback()
                self.assertEqual(
                    "\n".join(self.runtime.conn.iterdump()).encode("utf-8"),
                    durable_before_attacks,
                    label,
                )
            self.assertEqual(conn := self.runtime.conn.execute(
                "PRAGMA foreign_key_check"
            ).fetchall(), [], conn)

    def test_snapshot_records_migration_is_fault_atomic_and_restart_safe(self):
        component = "l2-snapshot-records-per-snapshot"
        target = next(
            index for index, migration in enumerate(
                history_audit_store.MIGRATIONS
            )
            if migration.component == component
        )
        migration = history_audit_store.MIGRATIONS[target]
        prefix = history_audit_store.MIGRATIONS[:target]
        path = self.runtime.root / "snapshot-records-upgrade.sqlite3"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            with mock.patch.object(
                history_audit_store, "MIGRATIONS", prefix
            ):
                history_audit_store.init_schema(conn)
            migrated = prefix + (migration,)
            with EVAL_FIXTURE.future_production_plan(
                conn, approve_production_evidence=False,
                install_host_router=False,
            ) as (_, plan_a):
                self._seed_true_host_route_for_production_plan(conn, plan_a)
                RUNTIME_FIXTURE.history_execution.persist_plan(conn, plan_a)
                row_a = tuple(conn.execute(
                    "SELECT * FROM audit_l2_snapshot_records_v2 "
                    "WHERE snapshot_id=?",
                    (plan_a["snapshot"]["snapshot_id"],),
                ).fetchone())
                before_fault = "\n".join(conn.iterdump()).encode("utf-8")
                broken = type(migration)(
                    component,
                    migration.version,
                    migration.sql
                    + "\nCREATE TABLE audit_snapshot_records_fault(\n",
                )
                with mock.patch.object(
                    history_audit_store,
                    "MIGRATIONS",
                    prefix + (broken,),
                ), self.assertRaises(
                    history_audit_store.AuditMigrationError
                ):
                    history_audit_store.init_schema(conn)
                self.assertEqual(
                    "\n".join(conn.iterdump()).encode("utf-8"),
                    before_fault,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) FROM audit_schema_migrations "
                        "WHERE component=?",
                        (component,),
                    ).fetchone()[0],
                    0,
                )

                with mock.patch.object(
                    history_audit_store, "MIGRATIONS", migrated
                ):
                    history_audit_store.init_schema(conn)
                self.assertEqual(
                    tuple(conn.execute(
                        "SELECT * FROM audit_l2_snapshot_records_v2 "
                        "WHERE snapshot_id=?",
                        (plan_a["snapshot"]["snapshot_id"],),
                    ).fetchone()),
                    row_a,
                )
                after_upgrade = "\n".join(conn.iterdump()).encode("utf-8")
                with mock.patch.object(
                    history_audit_store, "MIGRATIONS", migrated
                ):
                    history_audit_store.init_schema(conn)
                self.assertEqual(
                    "\n".join(conn.iterdump()).encode("utf-8"),
                    after_upgrade,
                )

                plan_b = self._foreign_plan(plan_a)
                plan_b["shards"][0]["shard_id"] = "map-records-b-0000"
                plan_b["shard_plan_sha"] = (
                    history_audit_plan.runtime_shard_plan_sha(
                        plan_b["shards"]
                    )
                )
                plan_b["plan_sha"] = history_audit_plan.runtime_plan_sha(
                    plan_b
                )
                plan_b["logical_task_keys"] = [
                    history_contract_v2.logical_task_key(
                        plan_b["plan_sha"], "map",
                        plan_b["candidate"]["candidate_id"],
                        shard["request_sha256"],
                    )
                    for shard in plan_b["shards"]
                ]
                self._seed_true_host_route_for_production_plan(conn, plan_b)
                RUNTIME_FIXTURE.history_execution.persist_plan(conn, plan_b)
                records = conn.execute(
                    "SELECT snapshot_id,records_sha FROM "
                    "audit_l2_snapshot_records_v2 ORDER BY snapshot_id"
                ).fetchall()
                self.assertEqual(len(records), 2)
                self.assertEqual(records[0][1], records[1][1])
                self.assertNotEqual(records[0][0], records[1][0])
                with self.assertRaises(sqlite3.DatabaseError):
                    conn.execute(
                        "UPDATE audit_l2_snapshot_records_v2 "
                        "SET records_json='{}' WHERE snapshot_id=?",
                        (plan_a["snapshot"]["snapshot_id"],),
                    )
                conn.rollback()
        finally:
            conn.close()

    def test_l2_plans_per_run_migration_is_fault_atomic_and_restart_safe(self):
        component = "l2-plans-per-run"
        target = next(
            index for index, migration in enumerate(
                history_audit_store.MIGRATIONS
            )
            if migration.component == component
        )
        migration = history_audit_store.MIGRATIONS[target]
        prefix = history_audit_store.MIGRATIONS[:target]
        migrated = prefix + (migration,)
        path = self.runtime.root / "plans-per-run-upgrade.sqlite3"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            with mock.patch.object(
                history_audit_store, "MIGRATIONS", prefix
            ):
                history_audit_store.init_schema(conn)
            with EVAL_FIXTURE.future_production_plan(
                conn, approve_production_evidence=False,
                install_host_router=False,
            ) as (_, plan_a):
                self._seed_true_host_route_for_production_plan(conn, plan_a)
                RUNTIME_FIXTURE.history_execution.persist_plan(conn, plan_a)
                plan_row_a = tuple(conn.execute(
                    "SELECT * FROM audit_l2_plans_v2 WHERE plan_sha=?",
                    (plan_a["plan_sha"],),
                ).fetchone())
                shard_row_a = tuple(conn.execute(
                    "SELECT * FROM audit_shard_plans WHERE run_id=?",
                    (plan_a["run_id"],),
                ).fetchone())
                self.assertGreater(
                    conn.execute(
                        "SELECT count(*) FROM audit_task_bindings_v2 "
                        "WHERE plan_sha=?",
                        (plan_a["plan_sha"],),
                    ).fetchone()[0],
                    0,
                )
                before_fault = "\n".join(conn.iterdump()).encode("utf-8")
                broken = type(migration)(
                    component,
                    migration.version,
                    migration.sql + "\nCREATE TABLE audit_plans_fault(\n",
                )
                with mock.patch.object(
                    history_audit_store,
                    "MIGRATIONS",
                    prefix + (broken,),
                ), self.assertRaises(
                    history_audit_store.AuditMigrationError
                ):
                    history_audit_store.init_schema(conn)
                self.assertEqual(
                    "\n".join(conn.iterdump()).encode("utf-8"),
                    before_fault,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) FROM audit_schema_migrations "
                        "WHERE component=?",
                        (component,),
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute("PRAGMA legacy_alter_table").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute("PRAGMA defer_foreign_keys").fetchone()[0],
                    0,
                )

                with mock.patch.object(
                    history_audit_store, "MIGRATIONS", migrated
                ):
                    history_audit_store.init_schema(conn)
                self.assertEqual(
                    tuple(conn.execute(
                        "SELECT * FROM audit_l2_plans_v2 WHERE plan_sha=?",
                        (plan_a["plan_sha"],),
                    ).fetchone()),
                    plan_row_a,
                )
                self.assertEqual(
                    tuple(conn.execute(
                        "SELECT * FROM audit_shard_plans WHERE run_id=?",
                        (plan_a["run_id"],),
                    ).fetchone()),
                    shard_row_a,
                )
                self.assertEqual(conn.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall(), [])
                after_upgrade = "\n".join(conn.iterdump()).encode("utf-8")
                with mock.patch.object(
                    history_audit_store, "MIGRATIONS", migrated
                ):
                    history_audit_store.init_schema(conn)
                self.assertEqual(
                    "\n".join(conn.iterdump()).encode("utf-8"),
                    after_upgrade,
                )

                plan_b = self._foreign_plan(plan_a)
                self._seed_true_host_route_for_production_plan(conn, plan_b)
                RUNTIME_FIXTURE.history_execution.persist_plan(conn, plan_b)
                plans = conn.execute(
                    "SELECT run_id,shard_plan_sha FROM audit_l2_plans_v2 "
                    "ORDER BY run_id"
                ).fetchall()
                shards = conn.execute(
                    "SELECT run_id,shard_plan_sha FROM audit_shard_plans "
                    "ORDER BY run_id"
                ).fetchall()
                self.assertEqual(len(plans), 2)
                self.assertEqual(len(shards), 2)
                self.assertEqual(plans[0][1], plans[1][1])
                self.assertEqual(shards[0][1], shards[1][1])
                self.assertNotEqual(plans[0][0], plans[1][0])
                self.assertEqual(conn.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall(), [])
                for table, predicate, value in (
                    ("audit_l2_plans_v2", "plan_sha", plan_a["plan_sha"]),
                    ("audit_shard_plans", "run_id", plan_a["run_id"]),
                ):
                    with self.subTest(table=table):
                        with self.assertRaises(sqlite3.DatabaseError):
                            conn.execute(
                                f"UPDATE {table} SET created_at='forged' "
                                f"WHERE {predicate}=?",
                                (value,),
                            )
                        conn.rollback()
        finally:
            conn.close()

    def test_host_selected_sa_routes_with_finalist_or_sa_rule(self):
        with EVAL_FIXTURE.future_production_plan(
            self.runtime.conn, install_host_router=False,
        ) as (_, plan):
            _, _, final = self._seed_true_host_route_for_production_plan(
                self.runtime.conn, plan, selected_class="sa"
            )
            selected = final["candidate_routes"][0]
            self.assertTrue(selected["router_facts"]["finalist_or_sa"])
            self.assertIn("finalist_or_sa", selected["matched_rule_ids"])
            RUNTIME_FIXTURE.history_execution.persist_plan(
                self.runtime.conn, plan
            )
            stitched = self.runtime.conn.execute(
                """
                SELECT route.matched_rule_ids_json,
                       route.fact_sha256,dispatch.route_fact_sha256,
                       binding.final_phase_fact_sha256
                FROM audit_candidate_route_facts_v2 route
                JOIN audit_candidate_l2_dispatch_facts_v2 dispatch
                  ON dispatch.run_id=route.run_id
                 AND dispatch.candidate_id=route.candidate_id
                JOIN audit_candidate_route_source_bindings_v2 binding
                  ON binding.run_id=route.run_id
                 AND binding.candidate_id=route.candidate_id
                 AND binding.route_fact_sha256=route.fact_sha256
                WHERE route.run_id=? AND route.candidate_id=?
                """,
                (plan["run_id"], plan["candidate"]["candidate_id"]),
            ).fetchone()
            self.assertIsNotNone(stitched)
            self.assertEqual(
                json.loads(stitched["matched_rule_ids_json"]),
                selected["matched_rule_ids"],
            )
            self.assertEqual(
                stitched["fact_sha256"], stitched["route_fact_sha256"]
            )
            self.assertEqual(
                stitched["final_phase_fact_sha256"],
                selected["phase_fact_sha256"],
            )

    def test_test_fake_route_and_plan_cannot_satisfy_host_production_authority(
        self,
    ):
        persist_plan = RUNTIME_FIXTURE.history_execution.persist_plan
        with mock.patch.object(
            RUNTIME_FIXTURE.history_execution,
            "persist_plan",
            return_value=[],
        ):
            with EVAL_FIXTURE.future_production_plan(
                self.runtime.conn, install_host_router=False,
            ) as (helper, plan):
                helper._install(plan)
                self.assertEqual(plan["authority_scope"], "production")
                self.assertEqual(
                    self.runtime.conn.execute(
                        "SELECT authority_scope FROM audit_router_rounds_v2 "
                        "WHERE run_id=?",
                        (plan["run_id"],),
                    ).fetchone()[0],
                    "test_fake",
                )
                with self.assertRaises(
                    RUNTIME_FIXTURE.history_execution.ExecutionError
                ) as caught:
                    persist_plan(self.runtime.conn, plan)
                self.assertEqual(
                    caught.exception.code, "host_route_authority_required"
                )
                for table in (
                    "audit_candidate_budget_receipts_v2",
                    "audit_run_manifests",
                    "audit_l2_plans_v2",
                    "audit_candidate_route_source_bindings_v2",
                    "audit_candidate_l2_dispatch_facts_v2",
                    "audit_logical_tasks",
                    "audit_runtime_budget_reservations_v2",
                ):
                    self.assertEqual(
                        self.runtime.conn.execute(
                            f"SELECT count(*) FROM {table}"
                        ).fetchone()[0],
                        0,
                        table,
                    )

    def test_host_round_cannot_be_reopened_by_test_prepare_or_test_source_issuer(
        self,
    ):
        plan = self._cohort_plan()
        additional = [self._additional_candidate()]
        self._seed_host_round_inputs(
            self.runtime, plan, additional_candidates=additional
        )
        route_round = self._host_api("prepare_host_router_round")(
            self.runtime.conn,
            run_id=plan["run_id"], batch_id=plan["batch_id"],
            intent=plan["intent"],
            raw_observations=self._host_observations(plan, additional),
            created_at=self._now(5),
        )
        route_sha = route_round["route_round_sha256"]
        round_json = self.runtime.conn.execute(
            "SELECT round_json FROM audit_router_rounds_v2 "
            "WHERE route_round_sha256=?", (route_sha,),
        ).fetchone()[0]
        material = json.loads(round_json)
        history_audit_store._TEST_ROUTER_ROUND_AUTHORITIES.pop(
            route_sha, None
        )
        with self.assertRaisesRegex(
            history_audit_store.AuditMigrationError,
            "router_source_test_authority_forbidden",
        ):
            history_audit_store.prepare_router_round(
                self.runtime.conn, material, created_at=self._now(6)
            )
        self.assertNotIn(
            route_sha, history_audit_store._TEST_ROUTER_ROUND_AUTHORITIES
        )
        forged_sources = self._domain_sources(plan, route_round)
        with mock.patch.dict(
            history_audit_store._TEST_ROUTER_ROUND_AUTHORITIES,
            {route_sha: round_json}, clear=False,
        ):
            with self.assertRaisesRegex(
                history_audit_store.AuditMigrationError,
                "router_source_test_authority_forbidden",
            ):
                self._source_api()(
                    self.runtime.conn, route_sha,
                    sources={"selection": forged_sources["selection"]},
                    created_at=self._now(7),
                )
        self.assertEqual(
            self.runtime.conn.execute(
                "SELECT count(*) FROM audit_router_domain_sources_v2"
            ).fetchone()[0],
            0,
        )

    def test_compat_projection_cannot_authorize_without_canonical_host_chain(self):
        plan = self._cohort_plan()
        self.runtime._install(
            plan, additional_candidates=[self._additional_candidate()]
        )
        self.assertEqual(
            self.runtime.conn.execute(
                "SELECT count(*) FROM audit_candidate_route_facts_v2 "
                "WHERE run_id=?", (plan["run_id"],),
            ).fetchone()[0],
            len(plan["snapshot"]["current_batch_ids"]),
        )
        self.assertEqual(
            self.runtime.conn.execute(
                "SELECT count(*) FROM audit_candidate_l2_dispatch_facts_v2 "
                "WHERE run_id=?", (plan["run_id"],),
            ).fetchone()[0],
            1,
        )
        material = history_audit_plan.build_runtime_plan_material(plan)
        with self.assertRaisesRegex(
            history_audit_store.AuditMigrationError,
            "production host router authority is unavailable",
        ):
            history_audit_store._require_host_router_preplan_chain(
                self.runtime.conn, material
            )
        for table in (
            "audit_router_host_preplan_batches_v2",
            "audit_router_host_observation_sets_v2",
            "audit_router_host_round_authorities_v2",
            "audit_router_host_l1_comparator_facts_v2",
            "audit_router_host_source_authorities_v2",
        ):
            self.assertEqual(
                self.runtime.conn.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0],
                0,
                table,
            )
        self.assertEqual(
            self.runtime.conn.execute(
                "SELECT count(*) FROM "
                "audit_router_host_source_authorities_v2"
            ).fetchone()[0],
            0,
        )

    def test_host_authority_replays_in_fresh_process_with_test_maps_bombed(self):
        plan = self._cohort_plan()
        additional = [self._additional_candidate()]
        self._seed_host_round_inputs(
            self.runtime, plan, additional_candidates=additional
        )
        prepare = self._host_api("prepare_host_router_round")
        issue = self._host_api("issue_host_router_domain_sources")
        route_round = prepare(
            self.runtime.conn,
            run_id=plan["run_id"],
            batch_id=plan["batch_id"],
            intent=plan["intent"],
            raw_observations=self._host_observations(plan, additional),
            created_at=self._now(5),
        )
        issue(
            self.runtime.conn,
            route_round["route_round_sha256"],
            phase="pre_l1",
            created_at=self._now(10),
        )
        self._derive(self.runtime, plan, "pre_l1", seconds=20)
        issue(
            self.runtime.conn,
            route_round["route_round_sha256"],
            phase="final",
            created_at=self._now(30),
        )
        expected = self._derive(self.runtime, plan, "final", seconds=40)
        script = textwrap.dedent(
            f"""
            import json
            import pathlib
            import sqlite3
            import sys

            sys.path.insert(0, {str(ROOT)!r})
            from lib import history_audit_plan
            from lib import history_audit_store

            class Bomb(dict):
                def _explode(self, *args, **kwargs):
                    raise AssertionError("test authority map was accessed")
                __getitem__ = _explode
                get = _explode
                __contains__ = _explode
                __iter__ = _explode
                __len__ = _explode
                keys = _explode
                items = _explode
                values = _explode

            history_audit_plan._TEST_RUNTIME_AUTHORITIES = Bomb()
            history_audit_store._TEST_ROUTER_ROUND_AUTHORITIES = Bomb()
            conn = sqlite3.connect({str(self.runtime.db_path)!r})
            conn.row_factory = sqlite3.Row
            history_audit_store.init_schema(conn)
            result = history_audit_store.derive_candidate_route_facts(
                conn,
                {plan['run_id']!r},
                {plan['batch_id']!r},
                {plan['intent']!r},
                phase="final",
                created_at={self._now(50)!r},
            )
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        replayed = json.loads(completed.stdout)
        self.assertEqual(
            replayed["source_set_sha256"], expected["source_set_sha256"]
        )
        self.assertEqual(
            replayed["candidate_routes"], expected["candidate_routes"]
        )

    def _prepare(
        self, runtime, plan, *, additional_candidates=None, seconds=5
    ):
        return self._prepare_api()(
            runtime.conn,
            self._round_material(
                plan, additional_candidates=additional_candidates
            ),
            created_at=self._now(seconds),
        )

    def _dependencies(self, route_round):
        return {
            "semantic_policy": sha("router-semantic-policy"),
            "plan": route_round["route_round_sha256"],
            "prompt": sha("router-prompt"),
            "schema": sha("router-schema"),
            "ordered_provider_pools": sha("router-provider-pools"),
            "capacity": sha("router-capacity"),
            "provider": sha("router-provider"),
            "fault": sha("router-fault"),
            "replay": sha("router-replay"),
            "fts": sha("router-fts"),
            "metadata": sha("router-metadata"),
        }

    def _domain_sources(
        self, plan, route_round, *, calibrated=False,
        comparator="pre_l1_skip"
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
        members = []
        l1_members = []
        risk_members = []
        request_members = []
        for candidate_id in candidate_ids:
            members.append(
                {
                    "candidate_id": candidate_id,
                    "selection_class": (
                        "finalist" if candidate_id == selected_id else "screened"
                    ),
                    "channel_states": [
                        {"channel_id": "dense_core", "state": "complete"},
                        {"channel_id": "exact_lineage", "state": "complete"},
                        {"channel_id": "fts", "state": "complete"},
                    ],
                }
            )
            if comparator == "pre_l1_skip":
                l1_members.append(
                    {
                        "candidate_id": candidate_id,
                        "observation_kind": "pre_l1_skip",
                        "skip_reason": "retriever_uncalibrated",
                        "coverage_state": "not_run",
                        "pre_phase_fact_sha256": None,
                    }
                )
            else:
                l1_members.append(
                    {
                        "candidate_id": candidate_id,
                        "observation_kind": "comparator",
                        "comparator_outcome": comparator,
                        "coverage_state": "complete",
                        "comparator_receipt_sha256": sha(
                            "router-comparator-receipt-" + candidate_id
                        ),
                    }
                )
            risk_members.append(
                {
                    "candidate_id": candidate_id,
                    "assigned_slice_ids": ["low_overlap"],
                }
            )
            request_members.append(
                {
                    "candidate_id": candidate_id,
                    "request_state": "not_requested",
                    "request_id": None,
                }
            )
        qrels_hash = sha("router-qrels")
        dependencies = self._dependencies(route_round)
        return {
            "selection": {
                "schema_version": "history-router-selection-source-v1",
                **identity,
                "selected_candidate_id": selected_id,
                "candidate_ids": candidate_ids,
                "members": members,
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

    def _issue(self, runtime, route_round, sources, *, seconds=10):
        return self._source_api()(
            runtime.conn,
            route_round["route_round_sha256"],
            sources=copy.deepcopy(sources),
            created_at=self._now(seconds),
        )

    def _derive(self, runtime, plan, phase, *, seconds=20):
        return self._derive_api()(
            runtime.conn,
            plan["run_id"],
            plan["batch_id"],
            plan["intent"],
            phase=phase,
            created_at=self._now(seconds),
        )

    def _issue_and_derive_final(
        self, runtime, plan, route_round, sources=None
    ):
        sources = sources or self._domain_sources(plan, route_round)
        pre_sources = {
            name: value
            for name, value in sources.items()
            if name != "l1_observation"
        }
        self._issue(runtime, route_round, pre_sources, seconds=10)
        pre = self._derive(runtime, plan, "pre_l1", seconds=20)
        if "l1_observation" in sources:
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
            self._issue(
                runtime,
                route_round,
                {"l1_observation": l1_source},
                seconds=30,
            )
        final = self._derive(runtime, plan, "final", seconds=40)
        return pre, final

    def _assert_derivation_identity(self, result, plan, phase):
        self.assertEqual(result["schema_version"], "history-router-derivation-v1")
        self.assertEqual(result["run_id"], plan["run_id"])
        self.assertEqual(result["batch_id"], plan["batch_id"])
        self.assertEqual(result["intent"], plan["intent"])
        self.assertEqual(result["phase"], phase)
        self.assertRegex(result["source_set_sha256"], r"^[0-9a-f]{64}$")
        routes = result["candidate_routes"]
        self.assertEqual(
            [route["candidate_id"] for route in routes],
            plan["snapshot"]["current_batch_ids"],
        )
        self.assertTrue(
            all(
                route["source_set_sha256"] == result["source_set_sha256"]
                for route in routes
            )
        )
        return {route["candidate_id"]: route for route in routes}

    def _record_prefix_legacy_route(
        self, conn, run_id, batch_id, intent, route_authority, *, created_at
    ):
        """Seed one valid pre-migration caller route in an isolated fixture."""
        routes = route_authority["candidate_routes"]
        candidate_ids = [item["candidate"]["candidate_id"] for item in routes]
        risk_policy = route_authority["risk_policy"]
        slice_policy = route_authority["risk_slice_policy"]
        risk_policy_sha = history_audit_store._semantic_sha(
            "history-risk-policy-v1", risk_policy
        )
        slice_policy_sha = history_audit_store._semantic_sha(
            "history-risk-slice-policy-v1", slice_policy
        )
        cohort_material = {
            "run_id": run_id,
            "batch_id": batch_id,
            "intent": intent,
            "candidate_ids": candidate_ids,
            "risk_policy_sha256": risk_policy_sha,
            "risk_slice_policy_sha256": slice_policy_sha,
            "created_at": created_at,
        }
        cohort_sha = history_audit_store._semantic_sha(
            "history-candidate-route-cohort-v2", cohort_material
        )
        cohort_values = (
            run_id,
            batch_id,
            intent,
            history_audit_store._semantic_canonical(candidate_ids),
            history_audit_store._semantic_canonical(risk_policy),
            risk_policy_sha,
            history_audit_store._semantic_canonical(slice_policy),
            slice_policy_sha,
            cohort_sha,
            created_at,
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
        fact_shas = []
        selected_id = routes[0]["candidate"]["candidate_id"]
        for item in routes:
            candidate_id = item["candidate"]["candidate_id"]
            facts = {
                **item["router_facts"],
                "candidate_budget_available": True,
                "attempt_budget_available": candidate_id == selected_id,
            }
            derived = history_audit_eval_v2.route_candidate(facts, risk_policy)
            material = {
                "run_id": run_id,
                "candidate_id": candidate_id,
                "intent": intent,
                "cohort_sha256": cohort_sha,
                "router_facts": facts,
                "risk_slices": item["risk_slices"],
                "matched_rule_ids": derived["matched_rule_ids"],
                "route": derived["route"],
                "call_l1_model": derived["call_l1_model"],
                "dispatch_allowed": derived["dispatch_allowed"],
                "rule_table_sha256": derived["rule_table_sha256"],
                "risk_policy_version": derived[
                    "receipt_risk_policy_version"
                ],
                "created_at": created_at,
            }
            fact_sha = history_audit_store._semantic_sha(
                "history-candidate-route-fact-v2", material
            )
            fact_values = (
                run_id,
                candidate_id,
                intent,
                cohort_sha,
                history_audit_store._semantic_canonical(facts),
                history_audit_store._semantic_canonical(item["risk_slices"]),
                history_audit_store._semantic_canonical(
                    derived["matched_rule_ids"]
                ),
                derived["route"],
                int(derived["call_l1_model"]),
                int(derived["dispatch_allowed"]),
                derived["rule_table_sha256"],
                derived["receipt_risk_policy_version"],
                fact_sha,
                created_at,
            )
            guard["route"] = fact_values
            try:
                conn.execute(
                    "INSERT INTO audit_candidate_route_facts_v2 "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    fact_values,
                )
            finally:
                guard["route"] = None
            observation_material = {
                "run_id": run_id,
                "candidate_id": candidate_id,
                "route_fact_sha256": fact_sha,
                "observation_scope": "host_issued_shadow",
                "production_authority": False,
                "created_at": created_at,
            }
            observation_values = (
                run_id,
                candidate_id,
                fact_sha,
                "host_issued_shadow",
                0,
                history_audit_store._semantic_sha(
                    "history-candidate-route-observation-boundary-v1",
                    observation_material,
                ),
                created_at,
            )
            guard["route_observation"] = observation_values
            try:
                conn.execute(
                    "INSERT INTO "
                    "audit_candidate_route_observation_boundaries_v2 "
                    "VALUES(?,?,?,?,?,?,?)",
                    observation_values,
                )
            finally:
                guard["route_observation"] = None
            fact_shas.append(fact_sha)
        return {
            "cohort_sha256": cohort_sha,
            "route_fact_sha256": fact_shas[0],
        }

    def _persist_prefix_legacy_plan(self, conn, plan, authority):
        """Build an exact prefix-38 route/dispatch fixture without current APIs."""
        created_at = self.runtime._seed_route_prerequisites(
            conn, plan, [plan["candidate"]]
        )
        material = history_audit_plan.build_runtime_plan_material(plan)
        snapshot = plan["snapshot"]
        conn.execute("BEGIN IMMEDIATE")
        try:
            route = self._record_prefix_legacy_route(
                conn, plan["run_id"], plan["batch_id"], plan["intent"],
                authority, created_at=created_at,
            )
            records = history_audit_plan.runtime_snapshot_records(
                snapshot["records"]
            )
            conn.execute(
                "INSERT INTO audit_l2_snapshot_records_v2 VALUES(?,?,?,?)",
                (
                    snapshot["snapshot_id"],
                    material["snapshot"]["records_sha"],
                    RUNTIME_FIXTURE.history_execution._canonical(records),
                    created_at,
                ),
            )
            plan_values = (
                plan["plan_sha"], plan["run_id"],
                plan["candidate"]["candidate_id"],
                plan["candidate"]["candidate_hash"],
                snapshot["snapshot_id"], snapshot["snapshot_hash"],
                plan["shard_plan_sha"], material["budget_policy_sha"],
                plan["intent"],
                RUNTIME_FIXTURE.history_execution._canonical(material),
                created_at,
            )
            conn.execute(
                "INSERT INTO audit_l2_plans_v2 VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                plan_values,
            )
            dispatch_material = {
                "plan_sha": plan["plan_sha"], "run_id": plan["run_id"],
                "candidate_id": plan["candidate"]["candidate_id"],
                "route_fact_sha256": route["route_fact_sha256"],
                "created_at": created_at,
            }
            dispatch_values = (
                plan["plan_sha"], plan["run_id"],
                plan["candidate"]["candidate_id"],
                route["route_fact_sha256"],
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
                    "VALUES(?,?,?,?,?,?)", dispatch_values,
                )
            finally:
                guard["dispatch"] = None
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def test_prepare_router_round_is_preplan_additive_and_reopens_exactly(self):
        plan = self.runtime.plan
        prepared = self._prepare(self.runtime, plan)
        self.assertEqual(
            prepared["schema_version"], "history-router-round-receipt-v1"
        )
        self.assertRegex(prepared["route_round_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(prepared["budget_fact_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(prepared["candidate_budget_decision"], "accepted")
        expected_tables = {
            "audit_router_rounds_v2",
            "audit_router_domain_sources_v2",
            "audit_router_budget_facts_v2",
            "audit_router_source_sets_v2",
            "audit_router_phase_facts_v2",
            "audit_candidate_route_source_bindings_v2",
            "audit_legacy_candidate_route_authorities_v2",
        }
        observed = {
            row[0]
            for row in self.runtime.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertTrue(expected_tables.issubset(observed))
        for table in (
            "audit_run_manifests", "audit_snapshots", "audit_batch_staging"
        ):
            self.assertEqual(
                self.runtime.conn.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0],
                0,
            )
        self.assertEqual(self._prepare(self.runtime, plan), prepared)
        self.runtime.conn.close()
        reopened = sqlite3.connect(self.runtime.db_path)
        reopened.row_factory = sqlite3.Row
        history_audit_store.init_schema(reopened)
        self.runtime.conn = reopened
        self.assertEqual(self._prepare(self.runtime, plan), prepared)
        with self.assertRaises(sqlite3.DatabaseError):
            reopened.execute(
                "UPDATE audit_router_rounds_v2 SET intent='forged' "
                "WHERE route_round_sha256=?",
                (prepared["route_round_sha256"],),
            )
        reopened.rollback()

    def test_fake_domain_source_issuer_is_closed_guarded_and_exactly_replayable(self):
        plan = self.runtime.plan
        route_round = self._prepare(self.runtime, plan)
        sources = self._domain_sources(plan, route_round)
        sources.pop("l1_observation")
        issued = self._issue(self.runtime, route_round, sources)
        self.assertEqual(
            issued["schema_version"], "history-router-domain-source-set-v1"
        )
        self.assertEqual(
            sorted(issued["source_sha256_by_kind"]), sorted(sources)
        )
        self.assertTrue(
            all(
                re.fullmatch(r"[0-9a-f]{64}", value)
                for value in issued["source_sha256_by_kind"].values()
            )
        )
        self.assertEqual(self._issue(self.runtime, route_round, sources), issued)
        self.assertEqual(
            self.runtime.conn.execute(
                "SELECT count(*) FROM audit_router_domain_sources_v2 "
                "WHERE route_round_sha256=?",
                (route_round["route_round_sha256"],),
            ).fetchone()[0],
            len(sources),
        )
        with self.assertRaises(sqlite3.DatabaseError):
            self.runtime.conn.execute(
                "UPDATE audit_router_domain_sources_v2 SET source_json='{}' "
                "WHERE route_round_sha256=?",
                (route_round["route_round_sha256"],),
            )
        self.runtime.conn.rollback()
        unbound_l1 = self._domain_sources(plan, route_round)["l1_observation"]
        for member in unbound_l1["members"]:
            member["pre_phase_fact_sha256"] = sha(
                "unbound-pre-phase-" + member["candidate_id"]
            )
        with self.assertRaisesRegex(
            history_audit_store.AuditMigrationError,
            "router_source_identity_mismatch",
        ):
            self._issue(
                self.runtime,
                route_round,
                {"l1_observation": unbound_l1},
                seconds=30,
            )
        self.assertEqual(
            self.runtime.conn.execute(
                "SELECT count(*) FROM audit_router_domain_sources_v2 "
                "WHERE route_round_sha256=? AND source_kind='l1_observation'",
                (route_round["route_round_sha256"],),
            ).fetchone()[0],
            0,
        )

    def test_two_valid_rounds_cannot_cross_stitch_persisted_sources(self):
        plan_a = self.runtime.plan
        plan_b = self._foreign_plan(plan_a)
        round_a = self._prepare(self.runtime, plan_a)
        round_b = self._prepare(self.runtime, plan_b)
        source_a = {
            "selection": self._domain_sources(plan_a, round_a)["selection"]
        }
        source_b = {
            "selection": self._domain_sources(plan_b, round_b)["selection"]
        }
        issued_b = self._issue(self.runtime, round_b, source_b)
        self.assertIn("selection", issued_b["source_sha256_by_kind"])
        with self.assertRaisesRegex(
            history_audit_store.AuditMigrationError,
            "router_source_identity_mismatch",
        ):
            self._issue(self.runtime, round_a, source_b)
        issued_a = self._issue(self.runtime, round_a, source_a)
        self.assertNotEqual(
            issued_a["source_sha256_by_kind"]["selection"],
            issued_b["source_sha256_by_kind"]["selection"],
        )

    def test_prepare_uses_exact_host_policy_and_records_over_limit_cohort(self):
        plan = self.runtime.plan
        caller_policy = copy.deepcopy(plan["budget_policy"])
        caller_policy["intents"][plan["intent"]]["round"][
            "candidates"
        ] += 1000
        forged = self._round_material(plan)
        forged["budget_policy"] = caller_policy
        forged["budget_policy_sha"] = (
            history_audit_plan.runtime_budget_policy_sha(caller_policy)
        )
        with self.assertRaisesRegex(
            history_audit_store.AuditMigrationError,
            "router_round_(schema|policy)_mismatch",
        ):
            self._prepare_api()(
                self.runtime.conn, forged, created_at=self._now(5)
            )
        self.assertEqual(
            self.runtime.conn.execute(
                "SELECT count(*) FROM audit_router_rounds_v2"
            ).fetchone()[0],
            0,
        )

        round_limit = plan["budget_policy"]["intents"][plan["intent"]][
            "round"
        ]["candidates"]
        additional = [
            self._additional_candidate(
                f"router-over-limit-{index}", source_order=index + 1
            )
            for index in range(round_limit)
        ]
        rejected_plan = self.runtime._plan(
            self.runtime.records, additional_candidates=additional
        )
        rejected = self._prepare(
            self.runtime,
            rejected_plan,
            additional_candidates=additional,
        )
        self.assertEqual(rejected["candidate_budget_decision"], "rejected")
        stored = self.runtime.conn.execute(
            "SELECT requested_candidates,round_candidate_limit,"
            "candidate_budget_decision FROM audit_router_budget_facts_v2 "
            "WHERE route_round_sha256=?",
            (rejected["route_round_sha256"],),
        ).fetchone()
        self.assertEqual(
            tuple(stored), (round_limit + 1, round_limit, "rejected")
        )

    def test_all_router_authority_tables_reject_direct_insert_update_delete(self):
        plan = self.runtime.plan
        route_round = self._prepare(self.runtime, plan)
        selection = {
            "selection": self._domain_sources(plan, route_round)["selection"]
        }
        issued = self._issue(self.runtime, route_round, selection)
        round_sha = route_round["route_round_sha256"]
        budget_sha = route_round["budget_fact_sha256"]
        source_sha = issued["source_sha256_by_kind"]["selection"]
        forged_sha = sha("router-direct-forgery")
        now = self._now(70)
        direct_inserts = {
            "audit_router_rounds_v2": (
                "INSERT INTO audit_router_rounds_v2 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    forged_sha, "forged-run", "forged-batch", "duplicate_search",
                    sha("forged-snapshot-id"), sha("forged-snapshot"),
                    sha("forged-current"), "[]", "{}", sha("forged-risk"),
                    sha("forged-slice"), sha("forged-budget"), "test_fake", now,
                ),
            ),
            "audit_router_budget_facts_v2": (
                "INSERT INTO audit_router_budget_facts_v2 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    forged_sha, round_sha, "[]", 1, 8, "accepted", 0,
                    64, 64, 1, sha("forged-usage"), "{}", now,
                ),
            ),
            "audit_router_domain_sources_v2": (
                "INSERT INTO audit_router_domain_sources_v2 VALUES(?,?,?,?,?)",
                (forged_sha, round_sha, "selection", "{}", now),
            ),
            "audit_router_source_sets_v2": (
                "INSERT INTO audit_router_source_sets_v2 VALUES(?,?,?,?,?,?,?)",
                (forged_sha, round_sha, "pre_l1", "{}", budget_sha, "[]", now),
            ),
            "audit_router_phase_facts_v2": (
                "INSERT INTO audit_router_phase_facts_v2 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    forged_sha, round_sha, "pre_l1",
                    plan["candidate"]["candidate_id"], forged_sha, "{}", "[]",
                    "[]", "routine", 0, 0, 0, sha("forged-rules"),
                    "risk-v1", now,
                ),
            ),
            "audit_candidate_route_source_bindings_v2": (
                "INSERT INTO audit_candidate_route_source_bindings_v2 VALUES(?,?,?,?,?,?)",
                (
                    plan["run_id"], plan["candidate"]["candidate_id"],
                    forged_sha, forged_sha, forged_sha, now,
                ),
            ),
            "audit_legacy_candidate_route_authorities_v2": (
                "INSERT INTO audit_legacy_candidate_route_authorities_v2 "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    forged_sha, plan["run_id"],
                    plan["candidate"]["candidate_id"], forged_sha, None,
                    None, None, "pre_source_authority", now,
                ),
            ),
        }
        for table, (statement, values) in direct_inserts.items():
            with self.subTest(table=table, operation="insert"):
                with self.assertRaises(sqlite3.DatabaseError):
                    self.runtime.conn.execute(statement, values)
                self.runtime.conn.rollback()

        source_set_sha = sha("authorized-test-source-set")
        phase_fact_sha = sha("authorized-test-phase-fact")
        route_fact_sha = sha("authorized-test-route-fact")
        source_set_values = (
            source_set_sha, round_sha, "pre_l1", "{}", budget_sha, "[]", now,
        )
        phase_fact_values = (
            phase_fact_sha, round_sha, "pre_l1",
            plan["candidate"]["candidate_id"], source_set_sha, "{}", "[]",
            "[]", "routine", 0, 0, 0, sha("test-rule-table"),
            "risk-v1", now,
        )
        binding_values = (
            plan["run_id"], plan["candidate"]["candidate_id"],
            route_fact_sha, phase_fact_sha, source_set_sha, now,
        )
        legacy_values = (
            route_fact_sha, plan["run_id"],
            plan["candidate"]["candidate_id"], sha("legacy-cohort"), None,
            None, None, "pre_source_authority", now,
        )
        guard = history_audit_store._ROUTER_SOURCE_GUARDS[
            id(self.runtime.conn)
        ]
        self.runtime.conn.execute(
            "DROP TRIGGER audit_candidate_route_source_bindings_v2_guard"
        )
        self.runtime.conn.commit()
        self.runtime.conn.execute("BEGIN IMMEDIATE")
        self.runtime.conn.execute("PRAGMA defer_foreign_keys=ON")
        try:
            for key, statement, values in (
                (
                    "source_set",
                    "INSERT INTO audit_router_source_sets_v2 VALUES(?,?,?,?,?,?,?)",
                    source_set_values,
                ),
                (
                    "phase_fact",
                    "INSERT INTO audit_router_phase_facts_v2 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    phase_fact_values,
                ),
                (
                    "binding",
                    "INSERT INTO audit_candidate_route_source_bindings_v2 VALUES(?,?,?,?,?,?)",
                    binding_values,
                ),
                (
                    "legacy",
                    "INSERT INTO audit_legacy_candidate_route_authorities_v2 "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    legacy_values,
                ),
            ):
                guard[key] = values
                try:
                    self.runtime.conn.execute(statement, values)
                finally:
                    guard[key] = None
            populated = (
                "audit_router_rounds_v2",
                "audit_router_budget_facts_v2",
                "audit_router_domain_sources_v2",
                "audit_router_source_sets_v2",
                "audit_router_phase_facts_v2",
                "audit_candidate_route_source_bindings_v2",
                "audit_legacy_candidate_route_authorities_v2",
            )
            for table in populated:
                with self.subTest(table=table, operation="update"):
                    with self.assertRaises(sqlite3.DatabaseError):
                        self.runtime.conn.execute(
                            f"UPDATE {table} SET rowid=rowid "
                            "WHERE rowid=(SELECT min(rowid) FROM " + table + ")"
                        )
                with self.subTest(table=table, operation="delete"):
                    with self.assertRaises(sqlite3.DatabaseError):
                        self.runtime.conn.execute(
                            f"DELETE FROM {table} WHERE rowid=(SELECT min(rowid) "
                            f"FROM {table})"
                        )
        finally:
            self.runtime.conn.execute("ROLLBACK")
            for key in ("source_set", "phase_fact", "binding", "legacy"):
                guard[key] = None

    def test_host_router_authority_tables_reject_direct_sql_mutation(self):
        plan = self._cohort_plan()
        additional = [self._additional_candidate()]
        self._seed_host_round_inputs(
            self.runtime, plan, additional_candidates=additional
        )
        route_round = self._host_api("prepare_host_router_round")(
            self.runtime.conn,
            run_id=plan["run_id"], batch_id=plan["batch_id"],
            intent=plan["intent"],
            raw_observations=self._host_observations(plan, additional),
            created_at=self._now(5),
        )
        self._install_host_shadow_calibration(
            self.runtime, route_round["route_round_sha256"]
        )
        self._host_api("issue_host_router_domain_sources")(
            self.runtime.conn, route_round["route_round_sha256"],
            phase="pre_l1", created_at=self._now(10),
        )
        pre = self._derive(self.runtime, plan, "pre_l1", seconds=20)
        pre_by_candidate = {
            route["candidate_id"]: route
            for route in pre["candidate_routes"]
        }
        candidates = self._candidate_cohort(plan, additional)
        with mock.patch.object(
            history_audit_store, "_utc_now", return_value=self._now(25)
        ):
            self._host_api("record_host_router_l1_observations")(
                self.runtime.conn,
                route_round_sha256=route_round["route_round_sha256"],
                observations=[
                    {
                        "candidate_id": candidate["candidate_id"],
                        "raw_observation_bytes": self._host_l1_raw_bytes(
                            plan, route_round, candidate,
                            pre_by_candidate[candidate["candidate_id"]],
                        ),
                    }
                    for candidate in candidates
                ],
            )
        tables = (
            "audit_router_host_preplan_batches_v2",
            "audit_router_host_observation_sets_v2",
            "audit_router_host_round_authorities_v2",
            "audit_router_host_l1_comparator_facts_v2",
            "audit_router_host_source_authorities_v2",
        )
        for table in tables:
            self.assertGreater(
                self.runtime.conn.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0],
                0,
                table,
            )
            with self.subTest(table=table, operation="insert"):
                with self.assertRaisesRegex(
                    sqlite3.DatabaseError, "requires host authority"
                ):
                    self.runtime.conn.execute(
                        f"INSERT INTO {table} SELECT * FROM {table} LIMIT 1"
                    )
                self.runtime.conn.rollback()
            with self.subTest(table=table, operation="update"):
                with self.assertRaises(sqlite3.DatabaseError):
                    self.runtime.conn.execute(
                        f"UPDATE {table} SET rowid=rowid "
                        f"WHERE rowid=(SELECT min(rowid) FROM {table})"
                    )
                self.runtime.conn.rollback()
            with self.subTest(table=table, operation="delete"):
                with self.assertRaises(sqlite3.DatabaseError):
                    self.runtime.conn.execute(
                        f"DELETE FROM {table} "
                        f"WHERE rowid=(SELECT min(rowid) FROM {table})"
                    )
                self.runtime.conn.rollback()

    def test_host_prepare_source_and_derivation_faults_roll_back_atomically(
        self,
    ):
        def deny_insert(table):
            def authorize(action, name, _column, _database, _trigger):
                if action == sqlite3.SQLITE_INSERT and name == table:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            return authorize

        def allow_all(*_arguments):
            return sqlite3.SQLITE_OK

        with self._fresh_runtime() as runtime:
            plan = self._cohort_plan(runtime)
            additional = [self._additional_candidate()]
            self._seed_host_round_inputs(
                runtime, plan, additional_candidates=additional
            )
            runtime.conn.set_authorizer(deny_insert(
                "audit_router_host_observation_sets_v2"
            ))
            try:
                with self.assertRaises(sqlite3.DatabaseError):
                    self._host_api("prepare_host_router_round")(
                        runtime.conn,
                        run_id=plan["run_id"], batch_id=plan["batch_id"],
                        intent=plan["intent"],
                        raw_observations=self._host_observations(
                            plan, additional
                        ),
                        created_at=self._now(5),
                    )
            finally:
                runtime.conn.set_authorizer(allow_all)
            for table in (
                "audit_router_rounds_v2",
                "audit_router_budget_facts_v2",
                "audit_router_host_observation_sets_v2",
                "audit_router_host_round_authorities_v2",
            ):
                self.assertEqual(
                    runtime.conn.execute(
                        f"SELECT count(*) FROM {table}"
                    ).fetchone()[0],
                    0,
                    table,
                )

        with self._fresh_runtime() as runtime:
            plan = self._cohort_plan(runtime)
            additional = [self._additional_candidate()]
            self._seed_host_round_inputs(
                runtime, plan, additional_candidates=additional
            )
            route_round = self._host_api("prepare_host_router_round")(
                runtime.conn,
                run_id=plan["run_id"], batch_id=plan["batch_id"],
                intent=plan["intent"],
                raw_observations=self._host_observations(plan, additional),
                created_at=self._now(5),
            )
            heads_before = (
                history_audit_store.current_semantic_dependency_heads(
                    runtime.conn
                )
            )
            runtime.conn.set_authorizer(deny_insert(
                "audit_router_host_source_authorities_v2"
            ))
            try:
                with self.assertRaises(sqlite3.DatabaseError):
                    self._host_api("issue_host_router_domain_sources")(
                        runtime.conn, route_round["route_round_sha256"],
                        phase="pre_l1", created_at=self._now(10),
                    )
            finally:
                runtime.conn.set_authorizer(allow_all)
            self.assertEqual(
                history_audit_store.current_semantic_dependency_heads(
                    runtime.conn
                ),
                heads_before,
            )
            for table in (
                "audit_router_domain_sources_v2",
                "audit_router_host_source_authorities_v2",
            ):
                self.assertEqual(
                    runtime.conn.execute(
                        f"SELECT count(*) FROM {table}"
                    ).fetchone()[0],
                    0,
                    table,
                )

        with self._fresh_runtime() as runtime:
            plan = self._cohort_plan(runtime)
            additional = [self._additional_candidate()]
            self._seed_host_round_inputs(
                runtime, plan, additional_candidates=additional
            )
            route_round = self._host_api("prepare_host_router_round")(
                runtime.conn,
                run_id=plan["run_id"], batch_id=plan["batch_id"],
                intent=plan["intent"],
                raw_observations=self._host_observations(plan, additional),
                created_at=self._now(5),
            )
            self._host_api("issue_host_router_domain_sources")(
                runtime.conn, route_round["route_round_sha256"],
                phase="pre_l1", created_at=self._now(10),
            )
            runtime.conn.set_authorizer(deny_insert(
                "audit_router_phase_facts_v2"
            ))
            try:
                with self.assertRaises(sqlite3.DatabaseError):
                    self._derive(runtime, plan, "pre_l1", seconds=20)
            finally:
                runtime.conn.set_authorizer(allow_all)
            for table in (
                "audit_router_source_sets_v2",
                "audit_router_phase_facts_v2",
            ):
                self.assertEqual(
                    runtime.conn.execute(
                        f"SELECT count(*) FROM {table}"
                    ).fetchone()[0],
                    0,
                    table,
                )

    def test_upgrade_quarantines_legacy_route_atomically_without_promotion(self):
        migrations = history_audit_store.MIGRATIONS
        router_index = next(
            index for index, migration in enumerate(migrations)
            if migration.component == "router-source-authority"
        )
        verified_index = next(
            index for index, migration in enumerate(migrations)
            if migration.component == "verified-usage-authority"
        )
        l1_index = next(
            index for index, migration in enumerate(migrations)
            if migration.component == "l1-cost-authority"
        )
        semantic_index = next(
            index for index, migration in enumerate(migrations)
            if migration.component == "semantic-production-evidence-authority"
        )
        self.assertEqual(
            [router_index, verified_index, l1_index, semantic_index],
            sorted([router_index, verified_index, l1_index, semantic_index]),
        )
        with tempfile.TemporaryDirectory() as temporary:
            db_path = pathlib.Path(temporary) / "legacy-router.sqlite3"
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            with mock.patch.object(
                history_audit_store, "MIGRATIONS", migrations[:router_index]
            ):
                history_audit_store.init_schema(conn)
            ledger_before = [
                tuple(row)
                for row in conn.execute(
                    "SELECT component,version,migration_sha256,applied_at "
                    "FROM audit_schema_migrations ORDER BY rowid"
                )
            ]
            plan = self.runtime.plan
            authority = self.runtime._route_authority(plan)
            self._persist_prefix_legacy_plan(conn, plan, authority)
            old_route = tuple(
                conn.execute(
                    "SELECT * FROM audit_candidate_route_facts_v2"
                ).fetchone()
            )
            old_observation = tuple(
                conn.execute(
                    "SELECT * FROM audit_candidate_route_observation_boundaries_v2"
                ).fetchone()
            )
            old_dispatch = tuple(
                conn.execute(
                    "SELECT * FROM audit_candidate_l2_dispatch_facts_v2"
                ).fetchone()
            )
            conn.close()

            reopened = sqlite3.connect(db_path)
            reopened.row_factory = sqlite3.Row
            history_audit_store.init_schema(reopened)
            try:
                self.assertEqual(
                    [
                        tuple(row)
                        for row in reopened.execute(
                            "SELECT component,version,migration_sha256,applied_at "
                            "FROM audit_schema_migrations ORDER BY rowid LIMIT ?",
                            (router_index,),
                        )
                    ],
                    ledger_before,
                )
                self.assertEqual(
                    tuple(
                        reopened.execute(
                            "SELECT * FROM audit_candidate_route_facts_v2"
                        ).fetchone()
                    ),
                    old_route,
                )
                self.assertEqual(
                    tuple(
                        reopened.execute(
                            "SELECT * FROM audit_candidate_route_observation_boundaries_v2"
                        ).fetchone()
                    ),
                    old_observation,
                )
                self.assertEqual(
                    tuple(
                        reopened.execute(
                            "SELECT * FROM audit_candidate_l2_dispatch_facts_v2"
                        ).fetchone()
                    ),
                    old_dispatch,
                )
                legacy = reopened.execute(
                    "SELECT route_fact_sha256,run_id,candidate_id,cohort_sha256,"
                    "observation_boundary_sha256,dispatch_sha256,plan_sha,reason "
                    "FROM audit_legacy_candidate_route_authorities_v2"
                ).fetchone()
                self.assertEqual(
                    tuple(legacy),
                    (
                        old_route[12], old_route[0], old_route[1], old_route[3],
                        old_observation[5], old_dispatch[4], old_dispatch[0],
                        "pre_source_authority",
                    ),
                )
                self.assertEqual(
                    reopened.execute(
                        "SELECT count(*) FROM audit_candidate_route_source_bindings_v2"
                    ).fetchone()[0],
                    0,
                )
                with self.assertRaisesRegex(
                    sqlite3.DatabaseError,
                    "legacy candidate route cannot receive source authority",
                ):
                    reopened.execute(
                        "INSERT INTO audit_candidate_route_source_bindings_v2 "
                        "VALUES(?,?,?,?,?,?)",
                        (
                            old_route[0], old_route[1], old_route[12],
                            sha("forged-final-phase"),
                            sha("forged-final-source-set"), self._now(80),
                        ),
                    )
                reopened.rollback()
                self.assertFalse(
                    history_audit_store.candidate_route_authority_replay_matches(
                        reopened, plan["run_id"], plan["batch_id"],
                        plan["intent"], authority,
                    )
                )
                self.assertFalse(
                    history_audit_store.candidate_l2_dispatch_replay_matches(
                        reopened,
                        plan["plan_sha"],
                        created_at=old_dispatch[-1],
                    )
                )
                with self.assertRaises(sqlite3.DatabaseError):
                    reopened.execute(
                        "UPDATE audit_legacy_candidate_route_authorities_v2 "
                        "SET reason=reason"
                    )
                reopened.rollback()
                with self.assertRaises(sqlite3.DatabaseError):
                    reopened.execute(
                        "DELETE FROM audit_legacy_candidate_route_authorities_v2"
                    )
                reopened.rollback()
            finally:
                reopened.close()

    def test_every_caller_boolean_flip_is_forbidden_for_nonselected_candidate(self):
        plan = self._cohort_plan()
        second = self._additional_candidate()
        self.runtime._seed_route_prerequisites(
            self.runtime.conn, plan, [plan["candidate"], second]
        )
        fields = tuple(self.runtime._router_facts())
        for field in fields:
            with self.subTest(field=field):
                routes = []
                for candidate in sorted(
                    [plan["candidate"], second],
                    key=lambda value: value["candidate_id"],
                ):
                    facts = self.runtime._router_facts()
                    slices = ["low_overlap"]
                    if candidate["candidate_id"] != plan["candidate"]["candidate_id"]:
                        facts[field] = not facts[field]
                        if field == "bad_slice_membership" and not facts[field]:
                            slices = []
                    routes.append(
                        {
                            "candidate": copy.deepcopy(candidate),
                            "router_facts": facts,
                            "risk_slices": slices,
                        }
                    )
                authority = self.runtime._route_authority(
                    plan, candidate_routes=routes
                )
                self.runtime.conn.execute("BEGIN IMMEDIATE")
                try:
                    with self.assertRaisesRegex(
                        history_audit_store.AuditMigrationError,
                        "^caller_route_authority_forbidden$",
                    ):
                        history_audit_store.record_candidate_route_facts(
                            self.runtime.conn,
                            plan["run_id"],
                            plan["batch_id"],
                            plan["intent"],
                            authority,
                            created_at=self._now(),
                        )
                finally:
                    self.runtime.conn.execute("ROLLBACK")

    def test_pre_l1_and_final_routes_derive_whole_frozen_cohort(self):
        plan = self._cohort_plan()
        route_round = self._prepare(self.runtime, plan)
        pre, final = self._issue_and_derive_final(
            self.runtime, plan, route_round
        )
        pre_routes = self._assert_derivation_identity(pre, plan, "pre_l1")
        final_routes = self._assert_derivation_identity(final, plan, "final")
        self.assertNotEqual(pre["source_set_sha256"], final["source_set_sha256"])
        selected = final_routes[plan["candidate"]["candidate_id"]]
        expected_facts = {
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
        self.assertEqual(selected["router_facts"], expected_facts)
        self.assertEqual(selected["risk_slices"], ["low_overlap"])
        self.assertEqual(
            selected["matched_rule_ids"],
            [
                "retriever_uncalibrated",
                "finalist_or_sa",
                "bad_slice_membership",
            ],
        )
        self.assertEqual(selected["route"], "exhaustive")
        self.assertFalse(selected["call_l1_model"])
        self.assertTrue(selected["dispatch_allowed"])
        self.assertFalse(
            final_routes[
                next(
                    candidate_id
                    for candidate_id in plan["snapshot"]["current_batch_ids"]
                    if candidate_id != plan["candidate"]["candidate_id"]
                )
            ]["router_facts"]["attempt_budget_available"]
        )
        self.assertTrue(
            pre_routes[plan["candidate"]["candidate_id"]]["router_facts"][
                "comparator_uncertain"
            ]
        )
        self.assertEqual(
            self.runtime.conn.execute(
                "SELECT count(*) FROM audit_candidate_route_source_bindings_v2 "
                "WHERE run_id=?", (plan["run_id"],)
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            [
                tuple(row)
                for row in self.runtime.conn.execute(
                    "SELECT phase,count(*) FROM audit_router_phase_facts_v2 "
                    "WHERE route_round_sha256=? GROUP BY phase ORDER BY phase",
                    (route_round["route_round_sha256"],),
                )
            ],
            [("final", len(final_routes)), ("pre_l1", len(pre_routes))],
        )

    def test_persist_plan_reuses_exact_final_route_without_route_payload(self):
        plan = self._cohort_plan()
        try:
            RUNTIME_FIXTURE.history_execution.persist_plan(
                self.runtime.conn,
                plan,
                route_authority=self.runtime._route_authority(),
            )
        except TypeError:
            pass
        except RUNTIME_FIXTURE.history_execution.ExecutionError as exc:
            self.assertEqual(exc.code, "caller_route_authority_forbidden")
        else:
            self.fail("persist_plan accepted caller-shaped route authority")
        route_round = self._prepare(self.runtime, plan)
        _, final = self._issue_and_derive_final(
            self.runtime, plan, route_round
        )
        first = RUNTIME_FIXTURE.history_execution.persist_plan(
            self.runtime.conn, plan
        )
        history_audit_store.publish_semantic_dependency_heads(
            self.runtime.conn,
            {"provider": sha("post-persist-provider-head")},
            now=self._now(50),
        )
        second = RUNTIME_FIXTURE.history_execution.persist_plan(
            self.runtime.conn, plan
        )
        self.assertEqual(first, plan["logical_task_keys"])
        self.assertEqual(second, first)
        self.assertEqual(
            self.runtime.conn.execute(
                "SELECT count(*) FROM audit_candidate_l2_dispatch_facts_v2 "
                "WHERE plan_sha=?",
                (plan["plan_sha"],),
            ).fetchone()[0],
            1,
        )
        candidate_count = len(plan["snapshot"]["current_batch_ids"])
        self.assertEqual(
            self.runtime.conn.execute(
                "SELECT count(*) FROM audit_batch_staging WHERE run_id=?",
                (plan["run_id"],),
            ).fetchone()[0],
            candidate_count,
        )
        self.assertEqual(
            self.runtime.conn.execute(
                """
                SELECT count(*)
                FROM audit_candidate_route_facts_v2 route
                JOIN audit_candidate_route_source_bindings_v2 binding
                  ON binding.run_id=route.run_id
                 AND binding.candidate_id=route.candidate_id
                 AND binding.route_fact_sha256=route.fact_sha256
                JOIN audit_router_phase_facts_v2 phase
                  ON phase.phase_fact_sha256=binding.final_phase_fact_sha256
                 AND phase.phase='final'
                 AND phase.source_set_sha256=binding.source_set_sha256
                 AND phase.candidate_id=route.candidate_id
                WHERE route.run_id=?
                  AND route.router_facts_json=phase.router_facts_json
                  AND route.risk_slices_json=phase.risk_slices_json
                  AND route.matched_rule_ids_json=phase.matched_rule_ids_json
                  AND route.route=phase.route
                  AND route.call_l1_model=phase.call_l1_model
                  AND route.dispatch_allowed=phase.dispatch_allowed
                  AND route.rule_table_sha256=phase.rule_table_sha256
                  AND route.risk_policy_version=phase.risk_policy_version
                """,
                (plan["run_id"],),
            ).fetchone()[0],
            candidate_count,
        )
        self.assertEqual(
            self.runtime.conn.execute(
                "SELECT source_set_sha256 "
                "FROM audit_router_phase_facts_v2 "
                "WHERE route_round_sha256=? AND phase='final' "
                "AND candidate_id=?",
                (
                    route_round["route_round_sha256"],
                    plan["candidate"]["candidate_id"],
                ),
            ).fetchone()[0],
            final["source_set_sha256"],
        )

    def test_missing_identity_or_candidate_budget_refuses_route_issuance(self):
        with self._fresh_runtime() as runtime:
            with self.assertRaises(history_audit_store.AuditMigrationError):
                self._derive(runtime, runtime.plan, "pre_l1")
            self.assertEqual(
                runtime.conn.execute(
                    "SELECT count(*) FROM audit_candidate_route_facts_v2"
                ).fetchone()[0],
                0,
            )
        with self._fresh_runtime() as runtime:
            plan = runtime.plan
            round_limit = plan["budget_policy"]["intents"][plan["intent"]][
                "round"
            ]["candidates"]
            additional = [
                self._additional_candidate(
                    f"router-over-limit-{index}", source_order=index + 1
                )
                for index in range(round_limit)
            ]
            rejected_plan = runtime._plan(
                runtime.records, additional_candidates=additional
            )
            route_round = self._prepare(
                runtime,
                rejected_plan,
                additional_candidates=additional,
            )
            self.assertEqual(
                route_round["candidate_budget_decision"], "rejected"
            )
            sources = self._domain_sources(rejected_plan, route_round)
            self._issue(
                runtime,
                route_round,
                {name: value for name, value in sources.items() if name != "l1_observation"},
            )
            with self.assertRaisesRegex(
                history_audit_store.AuditMigrationError,
                "candidate_budget_(authority_unavailable|exceeded)",
            ):
                self._derive(runtime, rejected_plan, "pre_l1")
            self.assertEqual(
                runtime.conn.execute(
                    "SELECT count(*) FROM audit_candidate_route_facts_v2"
                ).fetchone()[0],
                0,
            )

    def test_missing_domain_sources_take_conservative_closed_defaults(self):
        expected = {
            "l1_observation": {"comparator_uncertain": True},
            "calibration": {"retriever_calibrated": False},
            "qualification": {"release_qualified": False},
            "risk_assignment": {"bad_slice_membership": True},
            "dependency_heads": {"index_profile_recently_changed": True},
            "permanent_request": {"permanent_no_match_requested": True},
        }
        for missing, facts in expected.items():
            with self.subTest(missing=missing), self._fresh_runtime() as runtime:
                plan = runtime.plan
                route_round = self._prepare(runtime, plan)
                sources = self._domain_sources(plan, route_round)
                pre_sources = {
                    name: value
                    for name, value in sources.items()
                    if name not in {"l1_observation", missing}
                }
                self._issue(runtime, route_round, pre_sources)
                pre = self._derive(runtime, plan, "pre_l1")
                if missing != "l1_observation":
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
                    self._issue(
                        runtime,
                        route_round,
                        {"l1_observation": l1_source},
                        seconds=30,
                    )
                final = self._derive(runtime, plan, "final", seconds=40)
                route = self._assert_derivation_identity(
                    final, plan, "final"
                )[plan["candidate"]["candidate_id"]]
                for name, value in facts.items():
                    self.assertIs(route["router_facts"][name], value)
                if missing == "risk_assignment":
                    self.assertEqual(
                        route["risk_slices"],
                        sorted(history_audit_eval_v2.RISK_SLICE_POLICY_V1[
                            "allowed_slices"
                        ]),
                    )
                self.assertEqual(route["route"], "exhaustive")
                self.assertFalse(route["release_authorized"])

    def test_raw_source_helper_rejects_router_outputs_and_cross_identity_stitching(self):
        mutations = {
            "router_facts": lambda sources: sources["selection"].update(
                {"router_facts": self.runtime._router_facts()}
            ),
            "risk_slices": lambda sources: sources["risk_assignment"].update(
                {"risk_slices": ["low_overlap"]}
            ),
            "cross_run": lambda sources: sources["selection"].update(
                {"run_id": "run-foreign"}
            ),
            "cross_snapshot": lambda sources: sources["calibration"].update(
                {"snapshot_id": sha("foreign-snapshot")}
            ),
            "cross_candidate": lambda sources: sources["risk_assignment"][
                "members"
            ][0].update({"candidate_id": "stg-v2-" + sha("foreign-candidate")}),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), self._fresh_runtime() as runtime:
                plan = runtime.plan
                route_round = self._prepare(runtime, plan)
                sources = self._domain_sources(plan, route_round)
                sources.pop("l1_observation")
                mutate(sources)
                with self.assertRaisesRegex(
                    history_audit_store.AuditMigrationError,
                    "router_source_(schema|identity)_mismatch",
                ):
                    self._issue(runtime, route_round, sources)
                self.assertEqual(
                    runtime.conn.execute(
                        "SELECT count(*) FROM audit_candidate_route_facts_v2"
                    ).fetchone()[0],
                    0,
                )

    def test_dependency_head_drift_invalidates_exact_route_replay(self):
        plan = self.runtime.plan
        route_round = self._prepare(self.runtime, plan)
        _, final = self._issue_and_derive_final(
            self.runtime, plan, route_round
        )
        history_audit_store.publish_semantic_dependency_heads(
            self.runtime.conn,
            {"provider": sha("router-provider-drift")},
            now=self._now(50),
        )
        with self.assertRaisesRegex(
            history_audit_store.AuditMigrationError,
            "router_source_dependency_drift",
        ):
            self._derive(self.runtime, plan, "final", seconds=60)
        self.assertEqual(
            self.runtime.conn.execute(
                "SELECT source_set_sha256 "
                "FROM audit_router_phase_facts_v2 "
                "WHERE route_round_sha256=? AND phase='final' "
                "AND candidate_id=?",
                (
                    route_round["route_round_sha256"],
                    plan["candidate"]["candidate_id"],
                ),
            ).fetchone()[0],
            final["source_set_sha256"],
        )

    def test_selected_route_rules_are_exact_plan_authority(self):
        plan = self.runtime._plan(
            self.runtime.records,
            router_facts=self.runtime._router_facts(comparator_uncertain=True),
        )
        route_round = self._prepare(self.runtime, plan)
        _, final = self._issue_and_derive_final(
            self.runtime, plan, route_round
        )
        selected = self._assert_derivation_identity(final, plan, "final")[
            plan["candidate"]["candidate_id"]
        ]
        self.assertNotEqual(
            selected["matched_rule_ids"], plan["matched_router_rule_ids"]
        )
        with self.assertRaises(
            RUNTIME_FIXTURE.history_execution.ExecutionError
        ) as caught:
            RUNTIME_FIXTURE.history_execution.persist_plan(
                self.runtime.conn, plan
            )
        self.assertEqual(caught.exception.code, "selected_route_identity_mismatch")
        self.assertEqual(
            self.runtime.conn.execute(
                "SELECT count(*) FROM audit_l2_plans_v2"
            ).fetchone()[0],
            0,
        )

    def test_direct_sql_cannot_flip_delete_or_forge_derived_route(self):
        plan = self.runtime.plan
        route_round = self._prepare(self.runtime, plan)
        _, final = self._issue_and_derive_final(
            self.runtime, plan, route_round
        )
        row = self.runtime.conn.execute(
            "SELECT phase_fact_sha256,router_facts_json,source_set_sha256 "
            "FROM audit_router_phase_facts_v2 "
            "WHERE route_round_sha256=? AND phase='final' AND candidate_id=?",
            (
                route_round["route_round_sha256"],
                plan["candidate"]["candidate_id"],
            ),
        ).fetchone()
        original = json.loads(row["router_facts_json"])
        for field in sorted(original):
            with self.subTest(field=field):
                forged = dict(original)
                forged[field] = not forged[field]
                with self.assertRaises(sqlite3.DatabaseError):
                    self.runtime.conn.execute(
                        "UPDATE audit_router_phase_facts_v2 "
                        "SET router_facts_json=? WHERE phase_fact_sha256=?",
                        (
                            history_contract_v2.canonical_bytes(forged).decode(
                                "utf-8"
                            ),
                            row["phase_fact_sha256"],
                        ),
                    )
                self.runtime.conn.rollback()
        with self.assertRaises(sqlite3.DatabaseError):
            self.runtime.conn.execute(
                "DELETE FROM audit_router_phase_facts_v2 "
                "WHERE phase_fact_sha256=?",
                (row["phase_fact_sha256"],),
            )
        self.runtime.conn.rollback()
        replay = self.runtime.conn.execute(
            "SELECT router_facts_json,source_set_sha256 "
            "FROM audit_router_phase_facts_v2 "
            "WHERE phase_fact_sha256=?",
            (row["phase_fact_sha256"],),
        ).fetchone()
        self.assertEqual(replay["router_facts_json"], row["router_facts_json"])
        self.assertEqual(replay["source_set_sha256"], final["source_set_sha256"])

    def test_reopen_replays_exact_source_set_and_route(self):
        plan = self.runtime.plan
        route_round = self._prepare(self.runtime, plan)
        pre, final = self._issue_and_derive_final(
            self.runtime, plan, route_round
        )
        self.runtime.conn.close()
        reopened = sqlite3.connect(self.runtime.db_path)
        reopened.row_factory = sqlite3.Row
        history_audit_store.init_schema(reopened)
        self.runtime.conn = reopened
        replay_pre = self._derive(self.runtime, plan, "pre_l1", seconds=20)
        replay_final = self._derive(self.runtime, plan, "final", seconds=40)
        self.assertEqual(replay_pre, pre)
        self.assertEqual(replay_final, final)
        self.assertEqual(
            tuple(
                reopened.execute(
                    "SELECT count(DISTINCT source_set_sha256), count(*) "
                    "FROM audit_router_phase_facts_v2 "
                    "WHERE route_round_sha256=?",
                    (route_round["route_round_sha256"],),
                ).fetchone()
            ),
            (2, 2),
        )
        self.assertEqual(
            reopened.execute(
                "SELECT count(*) FROM audit_candidate_route_source_bindings_v2 "
                "WHERE run_id=?", (plan["run_id"],)
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
