#!/usr/bin/env python3
"""Host-issued authority and canonical identity for every v2 receipt."""

import copy
import datetime
import hashlib
import json
import pathlib
import sqlite3
import struct
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_audit_store
from lib import history_cas
from lib import history_contract_v2 as contract
from lib import history_execution
from lib import history_store
from history_audit_runtime_smoke import HistoryAuditRuntimeSmoke
from history_contract_v2_smoke import valid_receipt


SHA = "0" * 64
RECEIPT_VECTOR_SHA = (
    "9b95c892da3f4cf4cc7329298e17f74578951e10a3fd8a83252c47873e1918aa"
)


def literal_receipt_sha(receipt):
    """Independent test oracle for the committed self-excluding hash rule."""
    material = copy.deepcopy(receipt)
    material.pop("minimum_receipt_sha")
    encoded = (
        json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256()
    for part in (b"history-minimum-receipt-v2", encoded):
        digest.update(struct.pack(">Q", len(part)))
        digest.update(part)
    return digest.hexdigest()


class ReceiptAuthoritySmoke(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name).resolve()
        self.cas_root = self.root / "cas"
        self.db_path = self.root / "history.sqlite3"
        self.conn = history_store.connect(self.db_path)
        history_store.init_schema(self.conn)
        history_audit_store.init_schema(self.conn)
        self._seed_receipt_identity(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temporary.cleanup()

    @staticmethod
    def _seed_receipt_identity(conn):
        conn.execute(
            """
            INSERT INTO audit_run_manifests(
              run_id, manifest_schema_version, plan_hash, manifest_json, created_at
            ) VALUES('run-1', 'history-audit-manifest-v2', ?, '{}',
                     '2026-08-03T00:00:00Z')
            """,
            (SHA,),
        )
        conn.execute(
            """
            INSERT INTO audit_snapshots(
              snapshot_id, snapshot_hash, history_as_of_watermark,
              current_batch_id_namespace, current_batch_ids_hash,
              exclusion_policy_sha, expected_asset_ids_hash, created_at,
              run_id, batch_id
            ) VALUES('snapshot-1', ?, 7, 'history-v2-staging-v1', ?, ?, ?,
                     '2026-08-03T00:00:00Z', 'run-1', 'batch-1')
            """,
            (SHA, SHA, SHA, SHA),
        )
        conn.commit()

    @staticmethod
    def _receipt(**changes):
        receipt = valid_receipt()
        receipt.update(
            logical_task_hashes=[],
            attempt_manifest_hashes=[],
            raw_request_output_cas_hashes=[],
        )
        receipt.update(changes)
        receipt["minimum_receipt_sha"] = literal_receipt_sha(receipt)
        return receipt

    @staticmethod
    def _insert_receipt_sql(conn, receipt):
        encoded = history_cas._receipt_row(receipt)
        fields = tuple(receipt)
        conn.execute(
            "INSERT INTO audit_receipts(" + ",".join(fields) + ") VALUES(" +
            ",".join("?" for _ in fields) + ")",
            tuple(encoded[field] for field in fields),
        )

    def test_minimum_receipt_sha_has_committed_self_excluding_vector(self):
        calculate = getattr(contract, "minimum_receipt_sha", None)
        self.assertTrue(callable(calculate), "missing receipt identity function")
        self.assertEqual(calculate(valid_receipt()), RECEIPT_VECTOR_SHA)

    def test_receipt_validation_rejects_arbitrary_self_hash(self):
        receipt = self._receipt()
        receipt["minimum_receipt_sha"] = "f" * 64
        with self.assertRaises(contract.ContractV2Error):
            contract.validate_receipt(receipt)

    def test_every_final_status_requires_host_issuance_for_direct_sql(self):
        cases = {
            "overlap_found": {
                "stage_reason_code": "match_found",
                "evidence_anchors": [{"kind": "test-anchor"}],
            },
            "complete_no_match": {
                "stage_reason_code": "complete_no_match",
                "semantic_policy_qualified": True,
                "no_match_basis": "l2_exhaustive",
            },
            "uncertain": {},
            "partial": {
                "coverage_complete": False,
                "adjudication_complete": False,
                "stage_reason_code": "budget_exceeded",
            },
            "invalid": {
                "coverage_complete": False,
                "adjudication_complete": False,
                "invalid_schema": True,
                "stage_reason_code": "invalid_identity",
            },
        }
        for final_status, changes in cases.items():
            receipt = self._receipt(final_status=final_status, **changes)
            with self.subTest(final_status=final_status):
                self.conn.execute("SAVEPOINT receipt_case")
                try:
                    with self.assertRaises(sqlite3.IntegrityError):
                        self._insert_receipt_sql(self.conn, receipt)
                finally:
                    self.conn.execute("ROLLBACK TO SAVEPOINT receipt_case")
                    self.conn.execute("RELEASE SAVEPOINT receipt_case")

    def test_public_writer_rejects_canonical_but_unissued_receipt(self):
        receipt = self._receipt()
        with self.assertRaises(history_cas.CASError):
            history_cas.write_minimum_receipt(self.conn, receipt)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM audit_receipts").fetchone()[0],
            0,
        )

    def test_verify_rejects_receipt_from_pre_issuance_schema(self):
        old_db = self.root / "pre-issuance.sqlite3"
        old = history_store.connect(old_db)
        history_store.init_schema(old)
        migrations = history_audit_store.MIGRATIONS
        target = next(
            (
                index
                for index, migration in enumerate(migrations)
                if migration.component == "receipt-issuance-authority"
            ),
            len(migrations),
        )
        with mock.patch.object(
            history_audit_store, "MIGRATIONS", migrations[:target]
        ):
            history_audit_store.init_schema(old)
        self._seed_receipt_identity(old)
        receipt = self._receipt()
        self._insert_receipt_sql(old, receipt)
        old.commit()
        with self.assertRaises(history_cas.CASIntegrityError):
            history_cas.verify_minimum_receipt(
                old, self.cas_root, receipt["minimum_receipt_sha"]
            )
        old.close()

    def test_migration_rejects_preexisting_unissued_v2_receipt(self):
        old_db = self.root / "migration-pre-issuance.sqlite3"
        old = history_store.connect(old_db)
        history_store.init_schema(old)
        migrations = history_audit_store.MIGRATIONS
        target = next(
            (
                index
                for index, migration in enumerate(migrations)
                if migration.component == "receipt-issuance-authority"
            ),
            len(migrations),
        )
        with mock.patch.object(
            history_audit_store, "MIGRATIONS", migrations[:target]
        ):
            history_audit_store.init_schema(old)
        self._seed_receipt_identity(old)
        self._insert_receipt_sql(old, self._receipt())
        old.commit()
        with self.assertRaises(history_audit_store.AuditMigrationError):
            history_audit_store.init_schema(old)
        old.close()

    def test_cancel_receipt_authority_rejects_missing_conflicting_or_legacy_facts(self):
        base = {
            "usage_verified": 0,
            "actual_json": None,
            "launch_fact_sha256": "1" * 64,
            "cost_fact_sha256": "2" * 64,
            "usage_source": "reservation",
            "completion_outcome": None,
            "completion_usage_json": None,
            "output_cas_object_id": None,
            "cost_outcome": "cancelled",
            "billing_state": "unknown",
        }
        self.assertEqual(
            history_audit_store._receipt_attempt_outcome(base),
            "cancelled",
        )
        cases = {
            "missing_launch": {"launch_fact_sha256": None},
            "missing_cost": {"cost_fact_sha256": None},
            "legacy_verified_usage": {
                "usage_verified": 1,
                "actual_json": (
                    '{"input_tokens":0,"output_tokens":0,'
                    '"provider_usage_units":0}\n'
                ),
            },
            "completion_conflict": {
                "completion_outcome": "valid",
                "output_cas_object_id": "3" * 64,
            },
            "billing_conflict": {"billing_state": "billable"},
            "usage_source_conflict": {"usage_source": "verified_actual"},
            "cost_outcome_conflict": {"cost_outcome": "success"},
        }
        for name, changes in cases.items():
            with self.subTest(case=name):
                row = {**base, **changes}
                with self.assertRaises(ValueError):
                    history_audit_store._receipt_attempt_outcome(row)

        completed = {
            **base,
            "completion_outcome": "valid",
            "completion_usage_json": contract.canonical_bytes({}).decode(),
            "output_cas_object_id": "3" * 64,
            "cost_outcome": "success",
        }
        self.assertEqual(
            history_audit_store._receipt_attempt_outcome(completed),
            "valid",
        )
        for dirty_usage in (None, "{}", '{"caller":"usage"}\n'):
            with self.subTest(dirty_completion_usage=dirty_usage):
                with self.assertRaises(ValueError):
                    history_audit_store._receipt_attempt_outcome(
                        {**completed, "completion_usage_json": dirty_usage}
                    )


class L2ReceiptAuthoritySmoke(unittest.TestCase):
    def setUp(self):
        self.runtime = HistoryAuditRuntimeSmoke(methodName="runTest")
        self.runtime.setUp()

    def tearDown(self):
        self.runtime.tearDown()

    def _closed_receipt(self, provider=None):
        runtime = self.runtime
        plan = runtime._install()
        provider = provider or (
            lambda *_: {
                "kind": "success",
                "output": runtime._output(plan),
            }
        )
        runtime._api("run_map_task")(
            runtime.conn,
            runtime.cas_root,
            plan,
            plan["logical_task_keys"][0],
            provider,
            now=runtime._now(),
        )
        return self._receipt_for_plan(plan)

    def _receipt_for_plan(self, plan):
        runtime = self.runtime
        terminal = runtime._api("load_terminal_states")(
            runtime.conn, plan["plan_sha"]
        )
        summary = runtime._api("build_coverage_receipt")(
            plan,
            terminal,
            {"qualified": False, "profile_id": "semantic-test-v1"},
            conn=runtime.conn,
        )
        route = runtime.conn.execute(
            "SELECT * FROM audit_candidate_route_facts_v2 WHERE run_id=?",
            (plan["run_id"],),
        ).fetchone()
        tasks = [
            row[0]
            for row in runtime.conn.execute(
                """
                SELECT task.task_hash
                FROM audit_logical_tasks task
                JOIN audit_task_bindings_v2 binding USING(task_hash)
                WHERE binding.plan_sha=? ORDER BY task.task_hash
                """,
                (plan["plan_sha"],),
            )
        ]
        attempts = runtime.conn.execute(
            """
            SELECT attempt.attempt_id, attempt.request_cas_object_id,
                   completion.output_cas_object_id
            FROM audit_task_attempts attempt
            JOIN audit_logical_tasks task USING(task_hash)
            JOIN audit_task_bindings_v2 binding USING(task_hash)
            LEFT JOIN audit_attempt_completions_v2 completion USING(attempt_id)
            WHERE binding.plan_sha=? ORDER BY attempt.attempt_id
            """,
            (plan["plan_sha"],),
        ).fetchall()
        receipt = {
            "manifest_schema_version": "history-audit-manifest-v2",
            "canonical_codec_version": "history-canonical-json-v2",
            "run_id": plan["run_id"],
            "plan_hash": plan["plan_sha"],
            "candidate_hash": plan["candidate"]["candidate_hash"],
            "snapshot_id": plan["snapshot"]["snapshot_id"],
            "snapshot_hash": plan["snapshot"]["snapshot_hash"],
            "history_as_of_watermark": plan["snapshot"]["history_as_of_watermark"],
            "current_batch_id_namespace": plan["snapshot"]["current_batch_id_namespace"],
            "current_batch_ids_hash": plan["snapshot"]["current_batch_ids_hash"],
            "exclusion_policy_sha": plan["snapshot"]["exclusion_policy_sha"],
            "expected_asset_ids_hash": plan["snapshot"]["expected_asset_ids_hash"],
            "observed_asset_ids_hash": contract.ordered_set_sha256(
                "history-observed-assets-v2", summary["observed_ids"]
            ),
            "missing_ids": summary["missing_ids"],
            "duplicate_ids": summary["duplicate_ids"],
            "extra_ids": summary["extra_ids"],
            "invalid_schema": summary["invalid_schema"],
            "invalid_anchor": summary["invalid_anchor"],
            "truncated": summary["truncated"],
            "provider_pools_ordered": copy.deepcopy(plan["provider_pools_ordered"]),
            "provider_capability_profile_hashes": sorted(
                plan["provider_capability_profile_hashes"].values()
            ),
            "capacity_profile_id": plan["capacity_profile_id"],
            "semantic_policy_profile_id": summary["semantic_policy_profile_id"],
            "risk_policy_version": route["risk_policy_version"],
            "matched_router_rule_ids": json.loads(route["matched_rule_ids_json"]),
            "settlement_policy_sha": plan["settlement_policy_sha"],
            "shard_plan_sha": plan["shard_plan_sha"],
            "logical_task_hashes": tasks,
            "attempt_manifest_hashes": [row[0] for row in attempts],
            "raw_request_output_cas_hashes": sorted(
                {
                    object_id
                    for row in attempts
                    for object_id in (row[1], row[2])
                    if object_id is not None
                }
            ),
            "minimum_receipt_sha": SHA,
            "coverage_complete": summary["coverage_complete"],
            "adjudication_complete": summary["adjudication_complete"],
            "semantic_policy_qualified": summary["semantic_policy_qualified"],
            "no_match_basis": summary["no_match_basis"],
            "final_status": summary["final_status"],
            "stage_reason_code": summary["stage_reason_code"],
            "evidence_anchors": summary["evidence_anchors"],
        }
        receipt["minimum_receipt_sha"] = literal_receipt_sha(receipt)
        return plan, receipt

    def _cancel_then_retry(self):
        runtime = self.runtime
        plan = runtime._install()
        task_key = plan["logical_task_keys"][0]
        ready_at = runtime.conn.execute(
            "SELECT created_at FROM audit_logical_tasks WHERE task_hash=?",
            (task_key,),
        ).fetchone()[0]

        def at(offset):
            return (
                datetime.datetime.fromisoformat(ready_at)
                + datetime.timedelta(seconds=offset)
            ).isoformat()

        runtime._api("claim_task")(
            runtime.conn, task_key, "worker", 60, 0, now=ready_at
        )
        first = runtime._api("record_attempt")(
            runtime.conn, task_key, plan["provider_capabilities"]["codex"],
            {"attempt_kind": "initial"}, cas_root=runtime.cas_root,
            request_bytes=plan["shards"][0]["serialized_request"].encode(),
            now=ready_at,
        )
        runtime._api("cancel_attempt")(
            runtime.conn, first["attempt_id"], now=at(1)
        )
        second = runtime._api("record_attempt")(
            runtime.conn, task_key, plan["provider_capabilities"]["codex"],
            {"attempt_kind": "retry"}, cas_root=runtime.cas_root,
            request_bytes=plan["shards"][0]["serialized_request"].encode(),
            now=at(2),
        )
        valid = runtime._api("complete_attempt")(
            runtime.conn, runtime.cas_root, task_key, second["attempt_id"],
            runtime._output(plan), plan["snapshot"], now=at(3),
        )
        runtime._api("settle_task")(
            runtime.conn, task_key, [valid], cas_root=runtime.cas_root,
            now=at(3),
        )
        runtime._api("materialize_adjudication_tasks")(
            runtime.conn, runtime.cas_root, plan, now=at(3)
        )
        return plan, first, second, at

    def _verified_completion(self):
        runtime = self.runtime
        plan = runtime._install()
        task_key = plan["logical_task_keys"][0]
        ready_at = runtime.conn.execute(
            "SELECT created_at FROM audit_logical_tasks WHERE task_hash=?",
            (task_key,),
        ).fetchone()[0]
        terminal_at = (
            datetime.datetime.fromisoformat(ready_at)
            + datetime.timedelta(seconds=1)
        ).isoformat()
        runtime._api("claim_task")(
            runtime.conn, task_key, "verified-worker", 60, 0, now=ready_at
        )
        attempt = runtime._api("record_attempt")(
            runtime.conn, task_key, plan["provider_capabilities"]["codex"],
            {"attempt_kind": "initial"}, cas_root=runtime.cas_root,
            request_bytes=plan["shards"][0]["serialized_request"].encode(),
            now=ready_at,
        )
        output = runtime._output(plan)
        task = runtime._api("load_task")(runtime.conn, task_key)
        output_object = history_cas.put_object(
            runtime.conn,
            runtime.cas_root,
            contract.canonical_bytes(output),
            "attempt-transient-7d",
            expires_at=history_execution._attempt_expiry(task),
        )
        token = history_audit_store._issue_test_verified_usage_authority(
            runtime.conn,
            attempt_id=attempt["attempt_id"],
            output_cas_object_id=output_object["object_id"],
            terminal_outcome="valid",
            terminal_at=terminal_at,
            actual_usage={
                "input_tokens": 3,
                "output_tokens": 2,
                "provider_usage_units": 5,
            },
            billing_state="unknown",
            price_source=None,
            currency=None,
        )
        valid = runtime._api("complete_attempt")(
            runtime.conn, runtime.cas_root, task_key, attempt["attempt_id"],
            output, plan["snapshot"], usage=token, now=terminal_at,
        )
        runtime._api("settle_task")(
            runtime.conn, task_key, [valid], cas_root=runtime.cas_root,
            now=terminal_at,
        )
        runtime._api("materialize_adjudication_tasks")(
            runtime.conn, runtime.cas_root, plan, now=terminal_at
        )
        return plan, attempt, token, terminal_at

    def test_cancelled_attempt_then_retry_can_issue_and_replay_receipt(self):
        runtime = self.runtime
        plan, first, second, at = self._cancel_then_retry()
        _, receipt = self._receipt_for_plan(plan)
        receipt_id = history_cas.write_minimum_receipt(
            runtime.conn, receipt, now=at(4)
        )
        issuance = runtime.conn.execute(
            "SELECT provenance_json FROM audit_receipt_issuances_v2 "
            "WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
        provenance = contract.parse_json_bytes(
            issuance["provenance_json"].encode("utf-8")
        )
        attempts = {
            item["attempt_id"]: item for item in provenance["attempts"]
        }
        self.assertEqual(attempts[first["attempt_id"]]["outcome"], "cancelled")
        self.assertIsNone(
            attempts[first["attempt_id"]]["completion_usage_sha256"]
        )
        self.assertIsNone(
            attempts[first["attempt_id"]]["output_cas_object_id"]
        )
        self.assertEqual(attempts[second["attempt_id"]]["outcome"], "valid")
        self.assertEqual(
            attempts[second["attempt_id"]]["completion_usage_sha256"],
            history_audit_store._receipt_binding_sha(
                "history-receipt-completion-usage-v2",
                {"attempt_id": second["attempt_id"], "usage": {}},
            ),
        )

        runtime.conn.close()
        runtime.conn = sqlite3.connect(runtime.db_path)
        runtime.conn.row_factory = sqlite3.Row
        history_audit_store.init_schema(runtime.conn)
        verified = history_cas.verify_minimum_receipt(
            runtime.conn, runtime.cas_root, receipt_id
        )
        self.assertTrue(verified["execution_authorized"])

    def test_verified_usage_receipt_binds_exact_unpriced_sidecar(self):
        runtime = self.runtime
        plan, attempt, token, terminal_at = self._verified_completion()
        _, receipt = self._receipt_for_plan(plan)
        receipt_id = history_cas.write_minimum_receipt(
            runtime.conn, receipt, now=terminal_at
        )
        issuance = runtime.conn.execute(
            "SELECT provenance_json FROM audit_receipt_issuances_v2 "
            "WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
        provenance = contract.parse_json_bytes(
            issuance["provenance_json"].encode("utf-8")
        )
        bound = {
            item["attempt_id"]: item for item in provenance["attempts"]
        }[attempt["attempt_id"]]
        self.assertEqual(bound["usage_authority_sha256"], token)
        self.assertEqual(bound["billing_state"], "unknown")
        self.assertIsNone(bound["price_source"])
        self.assertIsNone(bound["currency"])

        runtime.conn.close()
        runtime.conn = sqlite3.connect(runtime.db_path)
        runtime.conn.row_factory = sqlite3.Row
        history_audit_store.init_schema(runtime.conn)
        self.assertTrue(
            history_cas.verify_minimum_receipt(
                runtime.conn, runtime.cas_root, receipt_id
            )["execution_authorized"]
        )

    def test_verified_usage_receipt_rejects_sidecar_actual_and_billing_tamper(self):
        mutations = (
            (
                "sidecar_sha",
                "UPDATE audit_verified_usage_authorities_v2 "
                "SET usage_authority_sha256=?",
                ("f" * 64,),
            ),
            (
                "actual",
                "UPDATE audit_verified_usage_authorities_v2 SET actual_json=?",
                (
                    contract.canonical_bytes({
                        "input_tokens": 30,
                        "output_tokens": 2,
                        "provider_usage_units": 32,
                    }).decode("utf-8"),
                ),
            ),
            (
                "billing",
                "UPDATE audit_verified_usage_authorities_v2 "
                "SET billing_state='billable',"
                "price_source='fake-price-v1',currency='USD'",
                (),
            ),
        )
        for index, (name, statement, parameters) in enumerate(mutations):
            if index:
                self.tearDown()
                self.setUp()
            plan, _, _, terminal_at = self._verified_completion()
            _, receipt = self._receipt_for_plan(plan)
            self.runtime.conn.execute(
                "DROP TRIGGER "
                "audit_verified_usage_authorities_v2_immutable_update"
            )
            self.runtime.conn.execute(statement, parameters)
            self.runtime.conn.commit()
            with self.subTest(tamper=name):
                with self.assertRaises(history_cas.CASError):
                    history_cas.write_minimum_receipt(
                        self.runtime.conn, receipt, now=terminal_at
                    )
                self.assertEqual(
                    self.runtime.conn.execute(
                        "SELECT count(*) FROM audit_receipt_issuances_v2"
                    ).fetchone()[0],
                    0,
                )

    def test_receipt_rejects_completion_conflicting_with_cancel_fact(self):
        runtime = self.runtime
        plan, first, _, at = self._cancel_then_retry()
        request_object_id = runtime.conn.execute(
            "SELECT request_cas_object_id FROM audit_task_attempts "
            "WHERE attempt_id=?",
            (first["attempt_id"],),
        ).fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            runtime.conn.execute(
                """
                INSERT INTO audit_attempt_completions_v2(
                  attempt_id, output_cas_object_id, outcome,
                  normalized_result_json, usage_json, completed_at
                ) VALUES(?, ?, 'valid', '{}', ?, ?)
                """,
                (
                    first["attempt_id"], request_object_id,
                    contract.canonical_bytes({}).decode(), at(4),
                ),
            )
        runtime.conn.rollback()
        _, receipt = self._receipt_for_plan(plan)
        receipt_id = history_cas.write_minimum_receipt(
            runtime.conn, receipt, now=at(5)
        )
        self.assertTrue(
            history_cas.verify_minimum_receipt(
                runtime.conn, runtime.cas_root, receipt_id
            )["execution_authorized"]
        )

    def test_l2_receipt_issues_from_exact_durable_execution_sets(self):
        _, receipt = self._closed_receipt()
        try:
            receipt_id = history_cas.write_minimum_receipt(
                self.runtime.conn, receipt
            )
        except history_cas.CASError as exc:
            self.fail(f"durable L2 receipt was not issued: {exc}")
        self.assertEqual(receipt_id, receipt["minimum_receipt_sha"])
        issuance = self.runtime.conn.execute(
            "SELECT * FROM audit_receipt_issuances_v2 WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
        self.assertIsNotNone(issuance)
        self.assertEqual(issuance["authority_kind"], "l2")
        verified = history_cas.verify_minimum_receipt(
            self.runtime.conn, self.runtime.cas_root, receipt_id
        )
        self.assertEqual(verified["minimum_receipt_sha"], receipt_id)
        self.assertTrue(verified["execution_authorized"])

    def test_l2_receipt_rejects_invented_or_omitted_durable_references(self):
        mutations = {
            "tasks": lambda value: value.update(logical_task_hashes=[]),
            "attempts": lambda value: value.update(attempt_manifest_hashes=[]),
            "cas": lambda value: value.update(raw_request_output_cas_hashes=[]),
            "router_rules": lambda value: value.update(matched_router_rule_ids=[]),
            "risk_policy": lambda value: value.update(risk_policy_version="forged-v1"),
            "settlement_policy": lambda value: value.update(
                settlement_policy_sha="f" * 64
            ),
            "evidence": lambda value: value.update(
                evidence_anchors=[{"kind": "caller-only"}]
            ),
        }
        for name, mutate in mutations.items():
            if name != "tasks":
                self.tearDown()
                self.setUp()
            _, receipt = self._closed_receipt()
            mutate(receipt)
            receipt["minimum_receipt_sha"] = literal_receipt_sha(receipt)
            with self.subTest(reference=name):
                with self.assertRaises(history_cas.CASError):
                    history_cas.write_minimum_receipt(
                        self.runtime.conn, receipt
                    )
                self.assertEqual(
                    self.runtime.conn.execute(
                        "SELECT count(*) FROM audit_receipts"
                    ).fetchone()[0],
                    0,
                )


if __name__ == "__main__":
    unittest.main()
