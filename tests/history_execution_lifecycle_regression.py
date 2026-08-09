#!/usr/bin/env python3
"""Regression tests for attempt claim and exception lifecycle correctness."""

import contextlib
import copy
import datetime
import pathlib
import sqlite3
import sys
import threading
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import history_audit_runtime_smoke as runtime_smoke
from lib import history_audit_store
from lib import history_execution


class BrokenResponse(dict):
    def get(self, key, default=None):
        raise RuntimeError("fixture response parser failed")


class HistoryExecutionLifecycleRegression(unittest.TestCase):
    def setUp(self):
        self.fixture = runtime_smoke.HistoryAuditRuntimeSmoke(methodName="runTest")
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def _start_attempt(self, *, lease_seconds=60):
        plan = self.fixture._install()
        task_key = plan["logical_task_keys"][0]
        started_at = self.fixture.conn.execute(
            "SELECT created_at FROM audit_logical_tasks WHERE task_hash=?",
            (task_key,),
        ).fetchone()[0]
        self.fixture._api("claim_task")(
            self.fixture.conn, task_key, "worker-a", lease_seconds, 0,
            now=started_at,
        )
        attempt = self.fixture._api("record_attempt")(
            self.fixture.conn, task_key,
            copy.deepcopy(self.fixture.capabilities["codex"]),
            {"attempt_kind": "initial"},
            cas_root=self.fixture.cas_root,
            request_bytes=plan["shards"][0]["serialized_request"].encode(),
            now=started_at,
        )
        return plan, task_key, attempt, started_at

    @staticmethod
    def _later(value, seconds):
        return (
            datetime.datetime.fromisoformat(value)
            + datetime.timedelta(seconds=seconds)
        ).isoformat()

    def _assert_cancelled_then_resumable(self, provider, *, patcher=None):
        plan = self.fixture._install()
        task_key = plan["logical_task_keys"][0]
        context = patcher if patcher is not None else contextlib.nullcontext()
        with context:
            with self.assertRaises(RuntimeError):
                history_execution.run_map_task(
                    self.fixture.conn, self.fixture.cas_root, plan, task_key,
                    provider, now=self.fixture._now(),
                )
        terminal = self.fixture.conn.execute(
            """
            SELECT cost.outcome, cost.error_class,
                   budget.usage_verified, budget.actual_json
            FROM audit_task_attempts attempt
            JOIN audit_attempt_cost_settlements_v2 cost USING(attempt_id)
            JOIN audit_runtime_budget_settlements_v2 budget USING(attempt_id)
            WHERE attempt.task_hash=? AND attempt.ordinal=0
            """,
            (task_key,),
        ).fetchone()
        self.assertEqual(tuple(terminal), ("cancelled", "runtime_exception", 0, None))
        self.assertIsNone(self.fixture.conn.execute(
            """
            SELECT 1 FROM audit_attempt_completions_v2 completion
            JOIN audit_task_attempts attempt USING(attempt_id)
            WHERE attempt.task_hash=? AND attempt.ordinal=0
            """,
            (task_key,),
        ).fetchone())

        result = history_execution.run_map_task(
            self.fixture.conn, self.fixture.cas_root, plan, task_key,
            lambda *_: {
                "kind": "success", "output": self.fixture._output(plan)
            },
            now=self.fixture._now(1),
        )
        self.assertEqual(result["settlement_kind"], "equal")
        self.assertEqual(
            history_execution.load_task(self.fixture.conn, task_key)["state"],
            "settled",
        )
        self.assertEqual(
            self.fixture.conn.execute(
                "SELECT count(*) FROM audit_task_attempts WHERE task_hash=?",
                (task_key,),
            ).fetchone()[0],
            2,
        )

    def test_completion_rechecks_claim_under_write_lock_after_reclaim(self):
        plan, task_key, attempt, started_at = self._start_attempt(lease_seconds=1)
        second = sqlite3.connect(
            self.fixture.db_path, check_same_thread=False
        )
        second.row_factory = sqlite3.Row
        history_audit_store.init_schema(second)
        stale_read = threading.Event()
        reclaimed = threading.Event()
        thread_errors = []
        original_load_task = history_execution.load_task
        calls = 0

        def barrier_load_task(conn, key):
            nonlocal calls
            row = original_load_task(conn, key)
            if conn is self.fixture.conn and key == task_key:
                calls += 1
                if calls == 2:
                    stale_read.set()
                    if not reclaimed.wait(5):
                        raise AssertionError("reclaim barrier timed out")
            return row

        def reclaim_expired_claim():
            try:
                if not stale_read.wait(5):
                    raise AssertionError("stale-read barrier timed out")
                recovered = history_execution.recover_run(
                    second, plan["plan_sha"], cas_root=self.fixture.cas_root,
                    now=self._later(started_at, 2),
                )
                self.assertEqual(recovered, [task_key])
            except BaseException as exc:
                thread_errors.append(exc)
            finally:
                reclaimed.set()

        worker = threading.Thread(target=reclaim_expired_claim)
        worker.start()
        try:
            with mock.patch.object(
                history_execution, "load_task", side_effect=barrier_load_task
            ):
                with self.assertRaises(history_audit_store.StaleFence):
                    history_execution.complete_attempt(
                        self.fixture.conn, self.fixture.cas_root, task_key,
                        attempt["attempt_id"], self.fixture._output(plan),
                        plan["snapshot"], now=self._later(started_at, 2),
                    )
        finally:
            reclaimed.set()
            worker.join(5)
            second.close()
        self.assertFalse(worker.is_alive())
        self.assertEqual(thread_errors, [])
        self.assertEqual(calls, 3)
        counts = self.fixture.conn.execute(
            """
            SELECT
              (SELECT count(*) FROM audit_attempt_completions_v2),
              (SELECT count(*) FROM audit_runtime_budget_settlements_v2),
              (SELECT count(*) FROM audit_attempt_cost_settlements_v2)
            """
        ).fetchone()
        self.assertEqual(tuple(counts), (0, 0, 0))
        task = history_execution.load_task(self.fixture.conn, task_key)
        self.assertEqual((task["state"], task["fence"]), ("planned", 2))

    def test_completion_rejects_expired_unreclaimed_lease(self):
        plan, task_key, attempt, started_at = self._start_attempt(lease_seconds=1)
        with self.assertRaises(history_audit_store.StaleFence):
            history_execution.complete_attempt(
                self.fixture.conn, self.fixture.cas_root, task_key,
                attempt["attempt_id"], self.fixture._output(plan),
                plan["snapshot"], now=self._later(started_at, 2),
            )
        self.assertEqual(
            self.fixture.conn.execute(
                "SELECT count(*) FROM audit_attempt_completions_v2"
            ).fetchone()[0],
            0,
        )

    def test_exact_completion_replay_remains_idempotent(self):
        plan, task_key, attempt, started_at = self._start_attempt()
        arguments = (
            self.fixture.conn, self.fixture.cas_root, task_key,
            attempt["attempt_id"], self.fixture._output(plan), plan["snapshot"],
        )
        first = history_execution.complete_attempt(
            *arguments, now=self._later(started_at, 1)
        )
        second = history_execution.complete_attempt(
            *arguments, now=self._later(started_at, 1)
        )
        self.assertEqual(second, first)
        counts = self.fixture.conn.execute(
            """
            SELECT
              (SELECT count(*) FROM audit_attempt_completions_v2),
              (SELECT count(*) FROM audit_runtime_budget_settlements_v2),
              (SELECT count(*) FROM audit_attempt_cost_settlements_v2)
            """
        ).fetchone()
        self.assertEqual(tuple(counts), (1, 1, 1))

    def test_failing_provider_fixture_is_cancelled_and_resumable(self):
        def failing_fixture(*_):
            raise RuntimeError("provider fixture failed")

        self._assert_cancelled_then_resumable(failing_fixture)

    def test_response_parser_exception_is_cancelled_and_resumable(self):
        self._assert_cancelled_then_resumable(lambda *_: BrokenResponse())

    def test_validation_exception_is_cancelled_and_resumable(self):
        self._assert_cancelled_then_resumable(
            lambda *_: {
                "kind": "success", "output": self.fixture._output()
            },
            patcher=mock.patch.object(
                history_execution, "validate_map_output",
                side_effect=RuntimeError("validator fixture failed"),
            ),
        )

    def test_completion_exception_is_cancelled_and_resumable(self):
        self._assert_cancelled_then_resumable(
            lambda *_: {
                "kind": "success", "output": self.fixture._output()
            },
            patcher=mock.patch.object(
                history_execution, "_insert_completion",
                side_effect=RuntimeError("completion fixture failed"),
            ),
        )


if __name__ == "__main__":
    unittest.main()
