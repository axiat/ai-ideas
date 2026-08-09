#!/usr/bin/env python3
"""Regression tests for split and exhaustion terminal authority."""

import copy
import datetime
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from lib import history_audit_store
from lib import history_execution
import history_audit_runtime_smoke as runtime_smoke


class HistoryAuditStoreTerminalEvidenceRegression(unittest.TestCase):
    def setUp(self):
        self.fixture = runtime_smoke.HistoryAuditRuntimeSmoke(
            "test_timeout_then_success_commits_one_logical_result"
        )
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def _install_and_claim(self, records):
        plan = self.fixture._plan(records)
        self.fixture._install(plan)
        task_hash = plan["logical_task_keys"][0]
        self.transition_now = history_audit_store._utc_now()
        claim = history_execution.claim_task(
            self.fixture.conn,
            task_hash,
            "terminal-evidence-worker",
            60,
            expected_fence=0,
            now=self.transition_now,
        )
        return plan, task_hash, claim

    def _record_failure(self, plan, task_hash, outcome):
        task = history_execution.load_task(self.fixture.conn, task_hash)
        attempt = history_execution.record_attempt(
            self.fixture.conn,
            task_hash,
            copy.deepcopy(self.fixture.capabilities["codex"]),
            {
                "attempt_kind": "initial",
                "input_tokens": 1,
                "output_tokens": 1,
                "provider_usage_units": 2,
            },
            cas_root=self.fixture.cas_root,
            request_bytes=plan["shards"][0]["serialized_request"].encode("utf-8"),
            now=self.transition_now,
            claim_fence=task["fence"],
            claim_token=task["claim_token"],
            claim_now=self.transition_now,
        )
        history_execution._failed_completion(
            self.fixture.conn,
            self.fixture.cas_root,
            task,
            attempt["attempt_id"],
            outcome,
            outcome,
            None,
            claim_fence=task["fence"],
            claim_token=task["claim_token"],
            authority_now=self.transition_now,
            now=self.transition_now,
        )
        return attempt

    def test_multi_item_split_requires_current_claim_failure(self):
        plan, task_hash, claim = self._install_and_claim(self.fixture.records)
        with self.assertRaises(history_audit_store.AuditMigrationError):
            history_audit_store.transition_l2_split_task(
                self.fixture.conn,
                task_hash,
                expected_fence=claim["fence"],
                claim_token=claim["claim_token"],
                now=self.transition_now,
            )
        self._record_failure(plan, task_hash, "overflow")
        result = history_audit_store.transition_l2_split_task(
            self.fixture.conn,
            task_hash,
            expected_fence=claim["fence"],
            claim_token=claim["claim_token"],
            now=self.transition_now,
        )
        self.assertEqual(result["state"], "superseded")
        self.assertEqual(len(result["children"]), 2)
        self.assertIsNotNone(
            self.fixture.conn.execute(
                "SELECT 1 FROM audit_l2_valid_split_families_v3 "
                "WHERE parent_task_hash=?", (task_hash,),
            ).fetchone()
        )

    def _recover_expired_claim(self, plan, task_hash, claim):
        recovery_now = datetime.datetime.fromisoformat(
            claim["lease_until"]
        ).isoformat()
        self.assertEqual(
            history_execution.recover_run(
                self.fixture.conn, plan["plan_sha"],
                cas_root=self.fixture.cas_root, now=recovery_now,
            ),
            [task_hash],
        )
        return (
            datetime.datetime.fromisoformat(recovery_now)
            + datetime.timedelta(seconds=1)
        ).isoformat()

    def test_recovery_transfer_rejects_mismatched_terminal_evidence(self):
        plan, task_hash, claim = self._install_and_claim(self.fixture.records)
        attempt = self._record_failure(plan, task_hash, "overflow")
        transfer_now = self._recover_expired_claim(
            plan, task_hash, claim
        )
        for attempt_id, outcome in (
            (attempt["attempt_id"], "truncated"),
            ("0" * 64, "overflow"),
        ):
            with self.subTest(attempt_id=attempt_id, outcome=outcome):
                with self.assertRaisesRegex(
                    history_audit_store.AuditMigrationError,
                    "lacks exact terminal evidence",
                ):
                    history_audit_store.claim_l2_failure_recovery(
                        self.fixture.conn, task_hash, attempt_id, outcome,
                        "recovery-worker", 60,
                        expected_fence=2, now=transfer_now,
                    )
        task = history_execution.load_task(self.fixture.conn, task_hash)
        self.assertEqual((task["state"], task["fence"]), ("planned", 2))
        self.assertEqual(
            self.fixture.conn.execute(
                "SELECT count(*) FROM audit_l2_failure_claim_transfers_v3"
            ).fetchone()[0],
            0,
        )

    def test_recovery_transfer_authorizes_only_new_claim(self):
        plan, task_hash, old_claim = self._install_and_claim(
            self.fixture.records
        )
        attempt = self._record_failure(plan, task_hash, "overflow")
        transfer_now = self._recover_expired_claim(
            plan, task_hash, old_claim
        )
        new_claim = history_audit_store.claim_l2_failure_recovery(
            self.fixture.conn, task_hash, attempt["attempt_id"], "overflow",
            "recovery-worker", 60,
            expected_fence=2, now=transfer_now,
        )
        with self.assertRaises(history_audit_store.StaleFence):
            history_audit_store.transition_l2_split_task(
                self.fixture.conn, task_hash,
                expected_fence=old_claim["fence"],
                claim_token=old_claim["claim_token"], now=transfer_now,
            )
        result = history_audit_store.transition_l2_split_task(
            self.fixture.conn, task_hash,
            expected_fence=new_claim["fence"],
            claim_token=new_claim["claim_token"], now=transfer_now,
        )
        self.assertEqual(result["state"], "superseded")
        self.assertIsNotNone(
            self.fixture.conn.execute(
                "SELECT 1 FROM audit_l2_valid_split_families_v3 "
                "WHERE parent_task_hash=?", (task_hash,),
            ).fetchone()
        )

    def test_exhaustion_replay_requires_exact_reason(self):
        plan, task_hash, claim = self._install_and_claim(self.fixture.records[:1])
        self._record_failure(plan, task_hash, "overflow")
        first = history_audit_store.transition_l2_exhaust_task(
            self.fixture.conn,
            task_hash,
            "single_item_overflow",
            expected_fence=claim["fence"],
            claim_token=claim["claim_token"],
            now=self.transition_now,
        )
        self.assertEqual(first["state"], "exhausted")
        replay = history_audit_store.transition_l2_exhaust_task(
            self.fixture.conn,
            task_hash,
            "single_item_overflow",
            expected_fence=claim["fence"],
            claim_token=claim["claim_token"],
            now=self.transition_now,
        )
        self.assertEqual(replay, first)
        with self.assertRaisesRegex(
            history_audit_store.AuditMigrationError,
            "replay reason conflicts",
        ):
            history_audit_store.transition_l2_exhaust_task(
                self.fixture.conn,
                task_hash,
                "provider_exhausted",
                expected_fence=claim["fence"],
                claim_token=claim["claim_token"],
                now=self.transition_now,
            )

    def test_exhaustion_rechecks_lease_under_write_lock(self):
        _, task_hash, claim = self._install_and_claim(self.fixture.records)
        with mock.patch.object(
            history_audit_store,
            "_metadata_lease_live",
            side_effect=[1, 0],
        ):
            with self.assertRaisesRegex(
                history_audit_store.StaleFence,
                "expired before transition",
            ):
                history_audit_store.transition_l2_exhaust_task(
                    self.fixture.conn,
                    task_hash,
                    "provider_exhausted",
                    expected_fence=claim["fence"],
                    claim_token=claim["claim_token"],
                    now=self.transition_now,
                )
        task = history_execution.load_task(self.fixture.conn, task_hash)
        self.assertEqual(task["state"], "claimed")
        self.assertEqual(task["fence"], claim["fence"])


if __name__ == "__main__":
    unittest.main()
