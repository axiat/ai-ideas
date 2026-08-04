#!/usr/bin/env python3
"""Durable L1 attempt authority and additive per-intent cost accounting."""

import copy
import hashlib
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
TESTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

from lib import history_audit_eval_v2
from lib import history_audit_plan
from lib import history_audit_store
from lib import history_contract_v2
import history_audit_runtime_smoke as runtime_smoke


def sha(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class HistoryL1CostAuthoritySmoke(unittest.TestCase):
    def setUp(self):
        self.runtime = runtime_smoke.HistoryAuditRuntimeSmoke(
            methodName="runTest"
        )
        self.runtime.setUp()

    def tearDown(self):
        self.runtime.tearDown()

    def _l1_api(self):
        api = getattr(history_audit_store, "record_l1_attempt_fact", None)
        self.assertTrue(
            callable(api),
            "missing behavior: history_audit_store.record_l1_attempt_fact",
        )
        return api

    def _mixed_cohort(self, count=2, *, comparator_providers=None):
        if count < 2:
            raise ValueError("mixed cohort requires at least two candidates")
        facts = self.runtime._router_facts(
            retriever_calibrated=True,
            finalist_or_sa=True,
            bad_slice_membership=False,
        )
        additional = []
        for index in range(1, count):
            candidate = {
                "candidate_id": "stg-v2-" + sha(f"l1-candidate-{index}"),
                "candidate_hash": "",
                "raw_artifact_sha": sha(f"l1-candidate-raw-{index}"),
                "source_order": index,
            }
            candidate["candidate_hash"] = (
                history_audit_plan.runtime_candidate_hash(candidate)
            )
            additional.append(candidate)
        plan = self.runtime._plan(
            self.runtime.records,
            additional_candidates=additional,
            comparator_providers=comparator_providers,
            router_facts=facts,
        )
        candidates = sorted(
            [plan["candidate"], *additional],
            key=lambda candidate: candidate["candidate_id"],
        )
        self.runtime._install(
            plan,
            additional_candidates=additional,
            calibrated=True,
            comparator="distinct",
            risk_slices_by_candidate={
                candidate["candidate_id"]: [] for candidate in candidates
            },
        )
        return plan, candidates

    def _receipt(self, candidate):
        return sha(
            "runtime-router-comparator-receipt-" + candidate["candidate_id"]
        )

    def _fact(self, plan, candidate, ordinal=0, **changes):
        provider = changes.get("provider", "reviewer")
        outcome = changes.get("outcome", "success")
        material = {
            "schema_version": "history-l1-attempt-fact-v2",
            "attempt_id": None,
            "ordinal": ordinal,
            "previous_attempt_id": None,
            "run_id": plan["run_id"],
            "candidate_id": candidate["candidate_id"],
            "intent": plan["intent"],
            "provider": provider,
            "capability_profile_hash": plan["provider_capabilities"][provider][
                "capability_profile_hash"
            ],
            "request_evidence_sha256": sha(
                f"l1-request:{candidate['candidate_id']}:{ordinal}"
            ),
            "result_evidence_sha256": (
                self._receipt(candidate) if outcome == "success" else None
            ),
            "usage_source": "reservation",
            "reserved": {
                "input_tokens": 10,
                "output_tokens": 5,
                "provider_usage_units": 15,
            },
            "usage_authority_sha256": None,
            "queue_latency_ms": 2 + ordinal,
            "run_latency_ms": 10 + ordinal,
            "outcome": outcome,
            "terminal_at": self.runtime._now(21 + ordinal),
        }
        material.update(copy.deepcopy(changes))
        if material["attempt_id"] is None:
            material["attempt_id"] = (
                history_audit_store._l1_attempt_id_for_material(
                    self.runtime.conn, material
                )
            )
        return material

    def _verified(self, material, actual, *, billing_state="billable"):
        issuer = getattr(
            history_audit_store,
            "_issue_test_l1_verified_usage_authority",
            None,
        )
        self.assertTrue(callable(issuer), "missing private L1 usage issuer")
        authority = issuer(
            self.runtime.conn,
            copy.deepcopy(material),
            actual_usage=copy.deepcopy(actual),
            billing_state=billing_state,
            price_source=(
                "test-price-v1" if billing_state == "billable" else None
            ),
            currency="USD" if billing_state == "billable" else None,
        )
        result = copy.deepcopy(material)
        result["usage_source"] = "verified_actual"
        result["usage_authority_sha256"] = authority
        return result

    def _record(self, material, *, conn=None):
        conn = conn or self.runtime.conn
        api = self._l1_api()
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = api(conn, copy.deepcopy(material))
            conn.execute("COMMIT")
            return result
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def _run_l2(self, plan):
        self.runtime._api("run_map_task")(
            self.runtime.conn,
            self.runtime.cas_root,
            plan,
            plan["logical_task_keys"][0],
            lambda *_: {
                "kind": "success",
                "output": self.runtime._output(plan),
            },
            now=self.runtime._now(2),
        )

    def _intent_cost(self, plan):
        return history_audit_eval_v2.summarize_realized_cost(
            self.runtime.conn, plan["run_id"]
        )["intents"][plan["intent"]]

    def test_l1_fact_table_is_append_only_and_direct_sql_requires_host(self):
        plan, candidates = self._mixed_cohort()
        columns = {
            row[1] for row in self.runtime.conn.execute(
                "PRAGMA table_info(audit_l1_attempt_facts_v2)"
            )
        }
        required = {
            "attempt_id", "ordinal", "previous_attempt_id", "run_id",
            "candidate_id", "intent", "provider",
            "capability_profile_hash", "request_evidence_sha256",
            "result_evidence_sha256", "usage_source", "reserved_json",
            "usage_authority_sha256", "actual_json", "queue_latency_ms",
            "run_latency_ms", "outcome", "billing_state", "price_source",
            "currency", "fact_sha256", "terminal_at",
            "route_fact_sha256", "final_phase_fact_sha256",
            "source_set_sha256",
        }
        self.assertTrue(required.issubset(columns), "missing durable L1 schema")
        fact = self._fact(plan, candidates[0])
        with self.assertRaises(sqlite3.IntegrityError):
            self.runtime.conn.execute(
                """
                INSERT INTO audit_l1_attempt_facts_v2(
                  attempt_id, ordinal, previous_attempt_id,
                  run_id, candidate_id, intent, provider,
                  capability_profile_hash, request_evidence_sha256,
                  result_evidence_sha256, usage_source, reserved_json,
                  usage_authority_sha256, actual_json,
                  queue_latency_ms, run_latency_ms, outcome, billing_state,
                  price_source, currency, fact_sha256, terminal_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    fact["attempt_id"], fact["ordinal"], None,
                    fact["run_id"], fact["candidate_id"],
                    fact["intent"], fact["provider"],
                    fact["capability_profile_hash"],
                    fact["request_evidence_sha256"],
                    fact["result_evidence_sha256"], fact["usage_source"],
                    history_contract_v2.canonical_bytes(
                        fact["reserved"]
                    ).decode(),
                    None, None, fact["queue_latency_ms"],
                    fact["run_latency_ms"], fact["outcome"], "unknown",
                    None, None,
                    sha("forged-l1-fact"), fact["terminal_at"],
                ),
            )
        self.runtime.conn.rollback()

    def test_host_fact_exactly_replays_after_reopen_and_rejects_mutation(self):
        plan, candidates = self._mixed_cohort()
        fact = self._fact(plan, candidates[0])
        first = self._record(fact)
        before = dict(self.runtime.conn.execute(
            "SELECT * FROM audit_l1_attempt_facts_v2 WHERE attempt_id=?",
            (fact["attempt_id"],),
        ).fetchone())

        self.runtime.conn.close()
        self.runtime.conn = sqlite3.connect(self.runtime.db_path)
        self.runtime.conn.row_factory = sqlite3.Row
        history_audit_store.init_schema(self.runtime.conn)
        second = self._record(fact)
        after = dict(self.runtime.conn.execute(
            "SELECT * FROM audit_l1_attempt_facts_v2 WHERE attempt_id=?",
            (fact["attempt_id"],),
        ).fetchone())
        self.assertEqual(second, first)
        self.assertEqual(after, before)
        self.assertEqual(
            self.runtime.conn.execute(
                "SELECT count(*) FROM audit_l1_attempt_facts_v2"
            ).fetchone()[0],
            1,
        )

        conflicting = {**fact, "result_evidence_sha256": sha("conflict")}
        with self.assertRaises(history_audit_store.AuditMigrationError):
            self._record(conflicting)
        with self.assertRaises(sqlite3.IntegrityError):
            self.runtime.conn.execute(
                "UPDATE audit_l1_attempt_facts_v2 SET outcome='failed'"
            )
        self.runtime.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.runtime.conn.execute("DELETE FROM audit_l1_attempt_facts_v2")
        self.runtime.conn.rollback()

    def test_reopen_probe_recomputes_l1_fact_and_source_bindings(self):
        plan, candidates = self._mixed_cohort()
        fact = self._fact(plan, candidates[0])
        self._record(fact)
        trigger = self.runtime.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='audit_l1_attempt_facts_v2_immutable_update'"
        ).fetchone()[0]
        self.runtime.conn.execute(
            "DROP TRIGGER audit_l1_attempt_facts_v2_immutable_update"
        )
        self.runtime.conn.execute(
            "UPDATE audit_l1_attempt_facts_v2 SET fact_sha256=? "
            "WHERE attempt_id=?",
            (sha("tampered-l1-fact"), fact["attempt_id"]),
        )
        self.runtime.conn.execute(trigger)
        self.runtime.conn.commit()
        self.runtime.conn.close()
        self.runtime.conn = sqlite3.connect(self.runtime.db_path)
        self.runtime.conn.row_factory = sqlite3.Row
        with self.assertRaises(history_audit_store.AuditMigrationError):
            history_audit_store.init_schema(self.runtime.conn)

    def test_l1_cost_migration_is_additive_and_preserves_prior_hashes(self):
        migrations = history_audit_store.MIGRATIONS
        l1_index = next(
            index for index, migration in enumerate(migrations)
            if migration.component == "l1-cost-authority"
        )
        verified_index = next(
            index for index, migration in enumerate(migrations)
            if migration.component == "verified-usage-authority"
        )
        semantic_index = next(
            index for index, migration in enumerate(migrations)
            if migration.component == "semantic-production-evidence-authority"
        )
        self.assertLess(verified_index, l1_index)
        self.assertLess(l1_index, semantic_index)
        prefix_rows = [
            (migration.component, migration.version, migration.sha256)
            for migration in migrations[:l1_index]
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "l1-additive.sqlite3"
            prefix = sqlite3.connect(path)
            prefix.row_factory = sqlite3.Row
            with mock.patch.object(
                history_audit_store, "MIGRATIONS", migrations[:l1_index]
            ):
                history_audit_store.init_schema(prefix)
            before = [
                tuple(row) for row in prefix.execute(
                    "SELECT component,version,migration_sha256 "
                    "FROM audit_schema_migrations ORDER BY rowid"
                )
            ]
            self.assertEqual(before, prefix_rows)
            prefix.close()

            reopened = sqlite3.connect(path)
            reopened.row_factory = sqlite3.Row
            history_audit_store.init_schema(reopened)
            after = [
                tuple(row) for row in reopened.execute(
                    "SELECT component,version,migration_sha256 "
                    "FROM audit_schema_migrations ORDER BY rowid LIMIT ?",
                    (l1_index,),
                )
            ]
            self.assertEqual(after, before)
            self.assertEqual(
                reopened.execute(
                    "SELECT component FROM audit_schema_migrations "
                    "WHERE component='l1-cost-authority'"
                ).fetchone()[0],
                "l1-cost-authority",
            )
            reopened.close()

    def test_host_fact_rejects_route_identity_and_capability_drift(self):
        plan, candidates = self._mixed_cohort()
        self._l1_api()
        base = self._fact(plan, candidates[0])
        cases = {
            "caller_chosen_attempt_id": {},
            "candidate": {"candidate_id": "stg-v2-" + sha("foreign")},
            "intent": {"intent": "foreign_intent"},
            "provider": {"provider": "forged-provider"},
            "capability": {"capability_profile_hash": "f" * 64},
            "request_evidence": {"request_evidence_sha256": "not-a-sha"},
            "result_evidence": {"result_evidence_sha256": "not-a-sha"},
            "result_binding": {
                "result_evidence_sha256": sha("foreign-result")
            },
        }
        for index, (name, changes) in enumerate(cases.items(), start=1):
            with self.subTest(case=name):
                material = {
                    **base,
                    **changes,
                    "attempt_id": sha(f"invalid-l1-attempt-{index}"),
                }
                with self.assertRaises(history_audit_store.AuditMigrationError):
                    self._record(material)
        self.assertEqual(
            self.runtime.conn.execute(
                "SELECT count(*) FROM audit_l1_attempt_facts_v2"
            ).fetchone()[0],
            0,
        )

    def test_pre_l1_skip_cannot_mint_a_comparator_result_fact(self):
        plan = self.runtime._install(comparator="pre_l1_skip")
        fact = self._fact(plan, plan["candidate"])
        with self.assertRaises(history_audit_store.AuditMigrationError):
            self._record(fact)

    def test_verified_usage_requires_private_immutable_authority_token(self):
        plan, candidates = self._mixed_cohort()
        fact = self._fact(plan, candidates[0])
        forged = {
            **fact,
            "usage_source": "verified_actual",
            "usage_authority_sha256": sha("forged-l1-usage-authority"),
        }
        with self.assertRaises(history_audit_store.AuditMigrationError):
            self._record(forged)
        with self.assertRaises(history_audit_store.AuditMigrationError):
            self._record({**fact, "actual": {"input_tokens": 0}})

        verified = self._verified(
            fact,
            {
                "input_tokens": 4,
                "output_tokens": 2,
                "provider_usage_units": 6,
                "currency_micros": 600,
            },
        )
        self._record(verified)
        stored = self.runtime.conn.execute(
            "SELECT usage_authority_sha256,actual_json,billing_state "
            "FROM audit_l1_attempt_facts_v2 WHERE attempt_id=?",
            (fact["attempt_id"],),
        ).fetchone()
        self.assertEqual(
            stored["usage_authority_sha256"],
            verified["usage_authority_sha256"],
        )
        self.assertEqual(stored["billing_state"], "billable")

    def test_attempt_chain_requires_contiguous_previous_terminal_and_success_tail(self):
        plan, candidates = self._mixed_cohort()
        first = self._fact(plan, candidates[0], outcome="failed")
        dangling = self._fact(
            plan,
            candidates[0],
            1,
            previous_attempt_id=sha("missing-previous"),
            attempt_id=sha("dangling-l1-attempt"),
        )
        with self.assertRaises(history_audit_store.AuditMigrationError):
            self._record(dangling)

        self._record(first)
        self._record(self._fact(plan, candidates[1]))
        self._run_l2(plan)
        partial = self._intent_cost(plan)
        self.assertIsNone(partial["expected_per_candidate"])
        self.assertEqual(
            partial["expected_unavailable_reason"],
            "durable_l1_attempt_facts_incomplete",
        )

        tail = self._fact(
            plan,
            candidates[0],
            1,
            previous_attempt_id=first["attempt_id"],
        )
        self._record(tail)
        retry_model = self._intent_cost(plan)["expected_per_candidate"]
        self.assertIsNotNone(retry_model)
        self.assertEqual(retry_model["L1_per_candidate"]["retry_calls"], 0.5)
        self.assertEqual(retry_model["L1_per_candidate"]["failover_calls"], 0.0)
        after_success = self._fact(
            plan,
            candidates[0],
            2,
            previous_attempt_id=tail["attempt_id"],
            outcome="cancelled",
        )
        with self.assertRaises(history_audit_store.AuditMigrationError):
            self._record(after_success)

    def test_retry_chain_allows_only_ordered_comparator_pool_failover(self):
        plan, candidates = self._mixed_cohort(
            comparator_providers=["reviewer", "codex"]
        )
        failed = self._fact(plan, candidates[0], outcome="failed")
        self._record(failed)
        failover = self._fact(
            plan,
            candidates[0],
            1,
            previous_attempt_id=failed["attempt_id"],
            provider="codex",
        )
        self._record(failover)
        self._record(self._fact(plan, candidates[1]))
        self._run_l2(plan)
        l1 = self._intent_cost(plan)["expected_per_candidate"][
            "L1_per_candidate"
        ]
        self.assertEqual(l1["failover_calls"], 0.5)
        self.assertEqual(l1["retry_calls"], 0.0)

    def test_failover_rejects_nonfirst_root_and_backwards_provider(self):
        plan, candidates = self._mixed_cohort(
            comparator_providers=["reviewer", "codex"]
        )
        nonfirst = self._fact(
            plan, candidates[0], provider="codex", outcome="failed"
        )
        with self.assertRaises(history_audit_store.AuditMigrationError):
            self._record(nonfirst)

        first = self._fact(plan, candidates[1], outcome="failed")
        self._record(first)
        forward = self._fact(
            plan, candidates[1], 1,
            previous_attempt_id=first["attempt_id"],
            provider="codex", outcome="failed",
        )
        self._record(forward)
        backwards = self._fact(
            plan, candidates[1], 2,
            previous_attempt_id=forward["attempt_id"],
            provider="reviewer",
        )
        with self.assertRaises(history_audit_store.AuditMigrationError):
            self._record(backwards)

    def test_route_authority_completeness_is_isolated_per_intent(self):
        cohorts = [
            {"intent": "intent-a", "complete": True},
            {"intent": "intent-b", "complete": False},
        ]
        routes = [
            {"intent": "intent-a", "candidate_id": "a"},
            {"intent": "intent-b", "candidate_id": "b"},
        ]
        with mock.patch.object(
            history_audit_eval_v2,
            "_validate_intent_route_authority",
            side_effect=lambda _run, cohort, _rows: cohort["complete"],
        ):
            complete = (
                history_audit_eval_v2
                ._route_authority_completeness_by_intent(
                    "run", cohorts, routes
                )
            )
        self.assertEqual(
            complete, {"intent-a": True, "intent-b": False}
        )

    def test_mixed_cohort_exposes_complete_additive_cost_formula(self):
        plan, candidates = self._mixed_cohort()
        for candidate in candidates:
            self._record(self._fact(plan, candidate))
        self._run_l2(plan)

        summary = self._intent_cost(plan)
        model = summary["expected_per_candidate"]
        self.assertEqual(summary["candidate_count"], 2)
        self.assertEqual(summary["escalated_candidate_count"], 1)
        self.assertEqual(summary["escalation_rate"], 0.5)
        self.assertEqual(
            model["formula"],
            "L1_per_candidate + escalation_rate * L2_per_escalation",
        )
        self.assertEqual(model["escalation_rate"], 0.5)
        l1 = model["L1_per_candidate"]
        l2 = model["L2_per_escalation"]
        self.assertEqual(l1["calls"], 1.0)
        self.assertEqual(l1["input_tokens"], 10.0)
        self.assertEqual(l1["output_tokens"], 5.0)
        self.assertEqual(l1["provider_usage_units"], 15.0)
        self.assertEqual(l2["calls"], 1.0)
        for field in (
            "calls", "input_tokens", "output_tokens", "cache_tokens",
            "provider_usage_units", "queue_latency_ms", "run_latency_ms",
        ):
            self.assertEqual(
                model[field], l1[field] + 0.5 * l2[field], field
            )
        self.assertIsNone(summary["expected_unavailable_reason"])

    def test_missing_or_partial_l1_facts_make_formula_unavailable(self):
        plan, candidates = self._mixed_cohort()
        self._run_l2(plan)
        missing = self._intent_cost(plan)
        self.assertIsNone(missing["expected_per_candidate"])
        self.assertEqual(
            missing["expected_unavailable_reason"],
            "durable_l1_attempt_facts_unavailable",
        )

        self._record(self._fact(plan, candidates[0]))
        partial = self._intent_cost(plan)
        self.assertIsNone(partial["expected_per_candidate"])
        self.assertEqual(
            partial["expected_unavailable_reason"],
            "durable_l1_attempt_facts_incomplete",
        )

    def test_failed_and_cancelled_l1_attempts_still_contribute_cost(self):
        plan, candidates = self._mixed_cohort(count=3)
        failed = self._fact(
            plan, candidates[1], outcome="failed",
            reserved={
                "input_tokens": 20,
                "output_tokens": 5,
                "provider_usage_units": 25,
            },
        )
        cancelled = self._fact(
            plan, candidates[2], outcome="cancelled",
            reserved={
                "input_tokens": 30,
                "output_tokens": 5,
                "provider_usage_units": 35,
            },
        )
        self._record(self._fact(plan, candidates[0]))
        self._record(failed)
        self._record(self._fact(
            plan, candidates[1], 1,
            previous_attempt_id=failed["attempt_id"],
        ))
        self._record(cancelled)
        self._record(self._fact(
            plan, candidates[2], 1,
            previous_attempt_id=cancelled["attempt_id"],
        ))
        self._run_l2(plan)

        l1 = self._intent_cost(plan)["expected_per_candidate"][
            "L1_per_candidate"
        ]
        self.assertEqual(l1["calls"], 5.0 / 3.0)
        self.assertEqual(l1["failed_calls"], 1.0 / 3.0)
        self.assertEqual(l1["input_tokens"], 80.0 / 3.0)
        self.assertEqual(l1["output_tokens"], 25.0 / 3.0)
        self.assertEqual(l1["provider_usage_units"], 105.0 / 3.0)

    def test_unknown_currency_is_not_fabricated_as_zero_in_total_formula(self):
        plan, candidates = self._mixed_cohort()
        for candidate in candidates:
            self._record(self._fact(plan, candidate))
        self._run_l2(plan)

        summary = self._intent_cost(plan)
        model = summary["expected_per_candidate"]
        self.assertFalse(summary["currency_complete"])
        self.assertNotIn("currency_micros", model["L1_per_candidate"])
        self.assertNotIn("currency_micros", model["L2_per_escalation"])
        self.assertNotIn("currency_micros", model)

    def test_verified_actual_l1_usage_overrides_reservation(self):
        plan, candidates = self._mixed_cohort()
        self._record(self._verified(
            self._fact(
                plan,
                candidates[0],
                reserved={
                    "input_tokens": 100,
                    "output_tokens": 100,
                    "provider_usage_units": 200,
                },
            ),
            {
                "input_tokens": 4,
                "output_tokens": 2,
                "provider_usage_units": 6,
                "currency_micros": 600,
            },
        ))
        self._record(self._verified(
            self._fact(plan, candidates[1]),
            {
                "input_tokens": 10,
                "output_tokens": 5,
                "provider_usage_units": 15,
            },
            billing_state="nonbillable",
        ))
        self._run_l2(plan)

        summary = self._intent_cost(plan)
        l1 = summary["expected_per_candidate"]["L1_per_candidate"]
        self.assertEqual(l1["input_tokens"], 7.0)
        self.assertEqual(l1["output_tokens"], 3.5)
        self.assertEqual(l1["provider_usage_units"], 10.5)
        self.assertEqual(l1["currency_micros"], 300.0)
        self.assertFalse(summary["currency_complete"])
        self.assertNotIn("currency_micros", summary["expected_per_candidate"])


if __name__ == "__main__":
    unittest.main()
