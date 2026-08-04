#!/usr/bin/env python3
"""Candidate-budget admission and exact selected-route authority smoke tests."""

import copy
import json
import pathlib
import sqlite3
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_audit_eval_v2
from lib import history_audit_plan
from lib import history_audit_store
from lib import history_contract_v2
from lib import history_execution
from tests import history_audit_runtime_smoke as runtime_smoke


class CandidateBudgetAuthoritySmoke(unittest.TestCase):
    def setUp(self):
        self.runtime = runtime_smoke.HistoryAuditRuntimeSmoke()
        self.runtime.setUp()
        self.conn = self.runtime.conn

    def tearDown(self):
        self.runtime.tearDown()

    def _candidate(self, ordinal):
        candidate = {
            "candidate_id": "stg-v2-" + runtime_smoke.sha(
                f"candidate-budget-{ordinal}"
            ),
            "candidate_hash": "",
            "raw_artifact_sha": runtime_smoke.sha(
                f"candidate-budget-raw-{ordinal}"
            ),
            "source_order": ordinal,
        }
        candidate["candidate_hash"] = history_audit_plan.runtime_candidate_hash(
            candidate
        )
        return candidate

    def _canonical_plan(self, additional_candidates=None):
        plan = self.runtime._plan(
            self.runtime.records,
            additional_candidates=additional_candidates,
        )
        route = history_audit_eval_v2.route_candidate(
            {
                **self.runtime._router_facts(),
                "candidate_budget_available": True,
                "attempt_budget_available": True,
            },
            self.runtime._risk_policy(),
        )
        plan.update(
            history_audit_plan._issue_test_runtime_authority(
                provider_pools_ordered=plan["provider_pools_ordered"],
                provider_capabilities=plan["provider_capabilities"],
                intent=plan["intent"],
                started_attempt_limit=64,
                semantic_policy_profile_id="semantic-test-v1",
                matched_router_rule_ids=route["matched_rule_ids"],
                max_output_tokens=64,
            )
        )
        plan["plan_sha"] = history_audit_plan.runtime_plan_sha(plan)
        plan["logical_task_keys"] = [
            history_contract_v2.logical_task_key(
                plan["plan_sha"], "map", plan["candidate"]["candidate_id"],
                shard["request_sha256"],
            )
            for shard in plan["shards"]
        ]
        return plan

    def _route_authority(self, plan, *, facts_by_id=None, risk_policy=None):
        selected_id = plan["candidate"]["candidate_id"]
        candidates_by_id = {selected_id: plan["candidate"]}
        for candidate in getattr(self, "_additional_candidates", []):
            candidates_by_id[candidate["candidate_id"]] = candidate
        routes = []
        for candidate_id in sorted(plan["snapshot"]["current_batch_ids"]):
            facts = copy.deepcopy(
                (facts_by_id or {}).get(
                    candidate_id, self.runtime._router_facts()
                )
            )
            facts.pop("candidate_budget_available", None)
            facts.pop("attempt_budget_available", None)
            routes.append(
                {
                    "candidate": copy.deepcopy(candidates_by_id[candidate_id]),
                    "router_facts": facts,
                    "risk_slices": ["low_overlap"],
                }
            )
        authority = self.runtime._route_authority(
            plan, candidate_routes=routes
        )
        if risk_policy is not None:
            authority["risk_policy"] = risk_policy
        return authority

    def _plan_with(self, additional_candidates):
        self._additional_candidates = additional_candidates
        return self._canonical_plan(additional_candidates)

    def _prepare_route(self, plan, *, comparator="distinct"):
        additional = getattr(self, "_additional_candidates", [])
        route_round = history_audit_store.prepare_router_round(
            self.conn,
            self.runtime._router_round_material(
                plan, additional_candidates=additional
            ),
            created_at=self.runtime._now(5),
        )
        if route_round["candidate_budget_decision"] == "rejected":
            return route_round
        sources = self.runtime._router_domain_sources(
            plan,
            route_round,
            calibrated=False,
            comparator=comparator,
        )
        history_audit_store._issue_test_router_domain_sources(
            self.conn,
            route_round["route_round_sha256"],
            sources={
                kind: value
                for kind, value in sources.items()
                if kind != "l1_observation"
            },
            created_at=self.runtime._now(10),
        )
        history_audit_store.derive_candidate_route_facts(
            self.conn,
            plan["run_id"],
            plan["batch_id"],
            plan["intent"],
            phase="pre_l1",
            created_at=self.runtime._now(20),
        )
        history_audit_store._issue_test_router_domain_sources(
            self.conn,
            route_round["route_round_sha256"],
            sources={"l1_observation": sources["l1_observation"]},
            created_at=self.runtime._now(30),
        )
        return history_audit_store.derive_candidate_route_facts(
            self.conn,
            plan["run_id"],
            plan["batch_id"],
            plan["intent"],
            phase="final",
            created_at=self.runtime._now(40),
        )

    def _assert_no_lifecycle(self, *, allow_router=False):
        tables = [
            "audit_run_manifests",
            "audit_snapshots",
            "audit_snapshot_batch_sets",
            "audit_batch_staging",
            "audit_l2_plans_v2",
            "audit_logical_tasks",
            "audit_task_attempts",
            "audit_attempt_launch_facts_v2",
        ]
        if not allow_router:
            tables.extend(
                (
                    "audit_candidate_route_cohorts_v2",
                    "audit_candidate_route_facts_v2",
                )
            )
        for table in tables:
            with self.subTest(table=table):
                self.assertEqual(
                    self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0],
                    0,
                )

    def test_over_limit_saves_one_rejected_decision_and_no_lifecycle(self):
        candidates = [self._candidate(index) for index in range(1, 9)]
        plan = self._plan_with(candidates)
        self._prepare_route(plan)

        with self.assertRaises(history_execution.ExecutionError) as caught:
            history_execution.persist_plan(self.conn, plan)
        self.assertEqual(caught.exception.code, "candidate_budget_exceeded")
        row = self.conn.execute(
            "SELECT * FROM audit_candidate_budget_receipts_v2"
        ).fetchone()
        self.assertEqual(row["decision"], "rejected")
        self.assertEqual(row["requested_candidates"], 9)
        self.assertEqual(row["round_candidate_limit"], 8)
        first_identity = (row["decision_sha256"], row["decided_at"])
        self._assert_no_lifecycle()

        with self.assertRaises(history_execution.ExecutionError) as retry:
            history_execution.persist_plan(self.conn, plan)
        self.assertEqual(retry.exception.code, "candidate_budget_exceeded")
        rows = self.conn.execute(
            "SELECT decision_sha256, decided_at "
            "FROM audit_candidate_budget_receipts_v2"
        ).fetchall()
        self.assertEqual([tuple(item) for item in rows], [first_identity])

        self.conn.close()
        self.runtime.conn = sqlite3.connect(self.runtime.db_path)
        self.runtime.conn.row_factory = sqlite3.Row
        self.conn = self.runtime.conn
        history_audit_store.init_schema(self.conn)
        with self.assertRaises(history_execution.ExecutionError) as restarted:
            history_execution.persist_plan(self.conn, plan)
        self.assertEqual(restarted.exception.code, "candidate_budget_exceeded")
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT decision_sha256, decided_at "
                    "FROM audit_candidate_budget_receipts_v2"
                ).fetchone()
            ),
            first_identity,
        )
        self._assert_no_lifecycle()

    def test_zero_round_limit_is_a_durable_hard_rejection(self):
        plan = self._canonical_plan()
        authority = copy.deepcopy(
            history_audit_plan._TEST_RUNTIME_AUTHORITIES[plan["authority_id"]]
        )
        authority["budget_policy"]["intents"][plan["intent"]]["round"][
            "candidates"
        ] = 0
        plan["budget_policy"] = copy.deepcopy(authority["budget_policy"])
        with mock.patch.dict(
            history_audit_plan._TEST_RUNTIME_AUTHORITIES,
            {plan["authority_id"]: authority},
        ):
            plan["plan_sha"] = history_audit_plan.runtime_plan_sha(plan)
            plan["logical_task_keys"] = [
                history_contract_v2.logical_task_key(
                    plan["plan_sha"], "map",
                    plan["candidate"]["candidate_id"],
                    shard["request_sha256"],
                )
                for shard in plan["shards"]
            ]
            self._prepare_route(plan)
            with self.assertRaises(history_execution.ExecutionError) as caught:
                history_execution.persist_plan(self.conn, plan)
        self.assertEqual(caught.exception.code, "candidate_budget_exceeded")
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT requested_candidates, round_candidate_limit, decision "
                    "FROM audit_candidate_budget_receipts_v2"
                ).fetchone()
            ),
            (1, 0, "rejected"),
        )
        self._assert_no_lifecycle()

    def test_round_identity_allows_only_one_admission_across_reopen(self):
        first_plan = self._canonical_plan()
        second_candidate = self._candidate(1)
        second_plan = self._plan_with([second_candidate])
        self.assertEqual(first_plan["run_id"], second_plan["run_id"])
        self.assertEqual(first_plan["intent"], second_plan["intent"])
        self.assertNotEqual(first_plan["plan_sha"], second_plan["plan_sha"])
        self.assertNotEqual(
            first_plan["snapshot"]["current_batch_ids"],
            second_plan["snapshot"]["current_batch_ids"],
        )
        first_material = history_audit_plan.build_runtime_plan_material(
            first_plan
        )
        second_material = history_audit_plan.build_runtime_plan_material(
            second_plan
        )
        first = history_audit_store.issue_candidate_budget_receipt(
            self.conn, first_material, first_plan["plan_sha"],
            decided_at=self.runtime._now(1),
        )
        self.assertEqual(first["decision"], "accepted")

        serial_conflict = False
        try:
            history_audit_store.issue_candidate_budget_receipt(
                self.conn, second_material, second_plan["plan_sha"],
                decided_at=self.runtime._now(2),
            )
        except history_audit_store.AuditMigrationError:
            serial_conflict = True
        serial_count = self.conn.execute(
            "SELECT count(*) FROM audit_candidate_budget_receipts_v2"
        ).fetchone()[0]

        self.conn.close()
        self.runtime.conn = sqlite3.connect(self.runtime.db_path)
        self.runtime.conn.row_factory = sqlite3.Row
        self.conn = self.runtime.conn
        history_audit_store.init_schema(self.conn)
        reopened_conflict = False
        try:
            history_audit_store.issue_candidate_budget_receipt(
                self.conn, second_material, second_plan["plan_sha"],
                decided_at=self.runtime._now(3),
            )
        except history_audit_store.AuditMigrationError:
            reopened_conflict = True
        reopened_count = self.conn.execute(
            "SELECT count(*) FROM audit_candidate_budget_receipts_v2"
        ).fetchone()[0]

        self.assertEqual(
            (
                serial_conflict, serial_count,
                reopened_conflict, reopened_count,
            ),
            (True, 1, True, 1),
        )

    def test_accepted_decision_injects_budget_facts_for_exact_cohort(self):
        second = self._candidate(1)
        plan = self._plan_with([second])
        self._prepare_route(plan)
        history_execution.persist_plan(self.conn, plan)

        receipt = self.conn.execute(
            "SELECT * FROM audit_candidate_budget_receipts_v2"
        ).fetchone()
        self.assertEqual(receipt["decision"], "accepted")
        self.assertEqual(receipt["requested_candidates"], 2)
        rows = self.conn.execute(
            "SELECT candidate_id, router_facts_json, matched_rule_ids_json, "
            "risk_policy_version FROM audit_candidate_route_facts_v2 "
            "ORDER BY candidate_id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        for row in rows:
            facts = json.loads(row["router_facts_json"])
            self.assertIs(facts["candidate_budget_available"], True)
            self.assertEqual(
                facts["attempt_budget_available"],
                row["candidate_id"] == plan["candidate"]["candidate_id"],
            )
            if row["candidate_id"] == plan["candidate"]["candidate_id"]:
                self.assertEqual(
                    json.loads(row["matched_rule_ids_json"]),
                    plan["matched_router_rule_ids"],
                )
                self.assertEqual(
                    row["risk_policy_version"], plan["risk_policy_version"]
                )
        original = tuple(
            self.conn.execute(
                "SELECT decision_sha256, decided_at "
                "FROM audit_candidate_budget_receipts_v2"
            ).fetchone()
        )
        history_execution.persist_plan(self.conn, plan)
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT decision_sha256, decided_at "
                    "FROM audit_candidate_budget_receipts_v2"
                ).fetchone()
            ),
            original,
        )

    def test_caller_cannot_supply_either_budget_fact_even_when_false(self):
        for key, value in (
            ("candidate_budget_available", False),
            ("attempt_budget_available", True),
        ):
            with self.subTest(key=key):
                plan = self._canonical_plan()
                authority = self._route_authority(plan)
                authority["candidate_routes"][0]["router_facts"][key] = value
                with self.assertRaises(history_execution.ExecutionError) as caught:
                    history_execution.persist_plan(
                        self.conn, plan, route_authority=authority
                    )
                self.assertEqual(
                    caught.exception.code, "caller_route_authority_forbidden"
                )
                self.assertEqual(
                    self.conn.execute(
                        "SELECT count(*) FROM audit_run_manifests"
                    ).fetchone()[0],
                    0,
                )

    def test_selected_matched_rules_and_risk_version_are_exact(self):
        plan = self._canonical_plan()
        self._prepare_route(plan, comparator="uncertain")
        with self.assertRaises(history_execution.ExecutionError) as matched:
            history_execution.persist_plan(self.conn, plan)
        self.assertEqual(
            matched.exception.code, "selected_route_identity_mismatch"
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT decision FROM audit_candidate_budget_receipts_v2"
            ).fetchone()[0],
            "accepted",
        )
        self._assert_no_lifecycle(allow_router=True)

        risk_policy = copy.deepcopy(self.runtime._risk_policy())
        risk_policy["risk_policy_version"] = "wrong-risk-version"
        with self.assertRaises(history_execution.ExecutionError) as risk:
            history_execution.persist_plan(
                self.conn, plan,
                route_authority=self._route_authority(
                    plan, risk_policy=risk_policy
                ),
            )
        self.assertEqual(
            risk.exception.code, "caller_route_authority_forbidden"
        )
        self._assert_no_lifecycle(allow_router=True)

    def test_accepted_decision_survives_atomic_lifecycle_failure(self):
        plan = self._canonical_plan()
        self._prepare_route(plan)
        with mock.patch.object(
            history_audit_store,
            "_insert_new_l2_plan_with_dispatch",
            side_effect=history_audit_store.AuditMigrationError("injected"),
        ):
            with self.assertRaises(history_execution.ExecutionError) as caught:
                history_execution.persist_plan(
                    self.conn, plan,
                )
        self.assertEqual(caught.exception.code, "invalid_route_dispatch")
        self.assertEqual(
            self.conn.execute(
                "SELECT decision FROM audit_candidate_budget_receipts_v2"
            ).fetchone()[0],
            "accepted",
        )
        self._assert_no_lifecycle(allow_router=True)

    def test_migration_rejects_preexisting_plan_without_budget_receipt(self):
        path = self.runtime.root / "pre-budget-plan.sqlite3"
        legacy = sqlite3.connect(path)
        legacy.row_factory = sqlite3.Row
        migrations = tuple(
            migration for migration in history_audit_store.MIGRATIONS
            if migration.component != "candidate-budget-authority"
        )
        with mock.patch.object(
            history_audit_store, "MIGRATIONS", migrations
        ):
            history_audit_store.init_schema(legacy)
            self.runtime._persist_pre_route_plan(legacy, self.runtime.plan)
            legacy.commit()
        with self.assertRaises(history_audit_store.AuditMigrationError):
            history_audit_store.init_schema(legacy)
        self.assertIsNone(
            legacy.execute(
                "SELECT 1 FROM audit_schema_migrations "
                "WHERE component='candidate-budget-authority'"
            ).fetchone()
        )
        legacy.close()

    def test_budget_receipt_rejects_direct_sql_update_and_delete(self):
        values = (
            runtime_smoke.sha("forged-decision"), "forged-run", "batch-1",
            "duplicate_search", runtime_smoke.sha("forged-plan"),
            runtime_smoke.sha("forged-budget"), '["forged-candidate"]',
            1, 8, "accepted",
            self.runtime._now(),
        )
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute(
                "INSERT INTO audit_candidate_budget_receipts_v2 "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)", values
            )
        self.conn.rollback()

        plan = self._canonical_plan()
        self._prepare_route(plan)
        history_execution.persist_plan(self.conn, plan)
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute(
                "UPDATE audit_candidate_budget_receipts_v2 "
                "SET decision='rejected'"
            )
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("DELETE FROM audit_candidate_budget_receipts_v2")


if __name__ == "__main__":
    unittest.main()
