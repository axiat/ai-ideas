#!/usr/bin/env python3
"""Regression coverage for logical-task transition authority."""

import copy
import pathlib
import sqlite3
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_audit_store
from lib import history_execution
import history_audit_runtime_smoke as runtime_smoke


class HistoryTaskTransitionRegression(unittest.TestCase):
    def setUp(self):
        self.runtime = runtime_smoke.HistoryAuditRuntimeSmoke(methodName="runTest")
        self.runtime.setUp()

    def tearDown(self):
        self.runtime.tearDown()

    def test_public_cas_rejects_planned_to_settled_without_authority(self):
        plan = self.runtime._install()
        task_hash = plan["logical_task_keys"][0]

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "illegal logical task transition: planned->settled",
        ):
            history_audit_store.compare_and_set_logical_task(
                self.runtime.conn,
                task_hash,
                expected_state="planned",
                expected_fence=0,
                new_state="settled",
                new_fence=1,
            )

        task = history_execution.load_task(self.runtime.conn, task_hash)
        self.assertEqual((task["state"], task["fence"]), ("planned", 0))
        self.assertIsNone(
            self.runtime.conn.execute(
                "SELECT 1 FROM audit_task_settlements_v2 WHERE task_hash=?",
                (task_hash,),
            ).fetchone()
        )

    def test_existing_settlement_path_and_exact_replay_remain_valid(self):
        plan = self.runtime._install()
        task_hash = plan["logical_task_keys"][0]
        history_execution.claim_task(
            self.runtime.conn,
            task_hash,
            "worker-1",
            60,
            expected_fence=0,
            now=self.runtime._now(),
        )
        attempt = history_execution.record_attempt(
            self.runtime.conn,
            task_hash,
            copy.deepcopy(self.runtime.capabilities["codex"]),
            {
                "attempt_kind": "initial",
                "input_tokens": 10,
                "output_tokens": 10,
                "provider_usage_units": 20,
            },
            cas_root=self.runtime.cas_root,
            request_bytes=plan["shards"][0]["serialized_request"].encode(),
        )
        valid = history_execution.complete_attempt(
            self.runtime.conn,
            self.runtime.cas_root,
            task_hash,
            attempt["attempt_id"],
            self.runtime._output(plan),
            plan["snapshot"],
        )

        first = history_execution.settle_task(
            self.runtime.conn,
            task_hash,
            [valid],
            cas_root=self.runtime.cas_root,
            now=self.runtime._now(1),
        )
        task = history_execution.load_task(self.runtime.conn, task_hash)
        self.assertEqual((task["state"], task["fence"]), ("settled", 3))
        self.assertIsNotNone(
            self.runtime.conn.execute(
                "SELECT 1 FROM audit_task_settlements_v2 WHERE task_hash=?",
                (task_hash,),
            ).fetchone()
        )

        replay = history_execution.settle_task(
            self.runtime.conn,
            task_hash,
            [valid],
            cas_root=self.runtime.cas_root,
            now=self.runtime._now(2),
        )
        self.assertEqual(replay, first)
        replayed_task = history_execution.load_task(self.runtime.conn, task_hash)
        self.assertEqual((replayed_task["state"], replayed_task["fence"]), ("settled", 3))


if __name__ == "__main__":
    unittest.main()
