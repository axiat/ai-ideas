#!/usr/bin/env python3
"""Defensive regressions for derived-task races and provider retries."""

import copy
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


class HistoryExecutionDefensiveConcurrencyRegression(unittest.TestCase):
    def setUp(self):
        self.fixture = runtime_smoke.HistoryAuditRuntimeSmoke(methodName="runTest")
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def _settle_exceptional_map(self):
        plan = self.fixture._install()
        task_key = plan["logical_task_keys"][0]
        history_execution.claim_task(
            self.fixture.conn, task_key, "map-worker", 60, 0,
            now=self.fixture._now(),
        )
        attempt = history_execution.record_attempt(
            self.fixture.conn, task_key,
            copy.deepcopy(plan["provider_capabilities"]["codex"]),
            {"attempt_kind": "initial"},
            cas_root=self.fixture.cas_root,
            request_bytes=plan["shards"][0]["serialized_request"].encode(),
        )
        valid = history_execution.complete_attempt(
            self.fixture.conn, self.fixture.cas_root, task_key,
            attempt["attempt_id"],
            self.fixture._output(
                plan,
                relations={
                    "asset-1": "blocking_duplicate",
                    "asset-2": "distinct",
                },
            ),
            plan["snapshot"],
        )
        history_execution.settle_task(
            self.fixture.conn, task_key, [valid],
            cas_root=self.fixture.cas_root, now=self.fixture._now(1),
        )
        return plan

    def _settle_detail(self, plan, detail_task_hash):
        task = history_execution.load_task(self.fixture.conn, detail_task_hash)
        history_execution.claim_task(
            self.fixture.conn, detail_task_hash, "detail-worker", 60, 0,
            now=self.fixture._now(2),
        )
        attempt = history_execution.record_attempt(
            self.fixture.conn, detail_task_hash,
            copy.deepcopy(plan["provider_capabilities"]["codex"]),
            {"attempt_kind": "detail"},
            cas_root=self.fixture.cas_root,
            request_bytes=task["durable_request_text"].encode(),
        )
        valid = history_execution.complete_attempt(
            self.fixture.conn, self.fixture.cas_root, detail_task_hash,
            attempt["attempt_id"], self.fixture._detail_output(task),
            plan["snapshot"],
        )
        history_execution.settle_task(
            self.fixture.conn, detail_task_hash, [valid],
            cas_root=self.fixture.cas_root, now=self.fixture._now(3),
        )

    def _race_after_absent_replay(self, replay_name, operation):
        connections = [
            sqlite3.connect(
                self.fixture.db_path, timeout=5, check_same_thread=False
            )
            for _ in range(2)
        ]
        for connection in connections:
            connection.row_factory = sqlite3.Row
            history_audit_store.init_schema(connection)
        barrier = threading.Barrier(2)
        first_checks = set()
        first_checks_lock = threading.Lock()
        original = getattr(history_execution, replay_name)
        results = [None, None]
        failures = []

        def synchronized_replay(connection, *args):
            result = original(connection, *args)
            with first_checks_lock:
                first = id(connection) not in first_checks
                first_checks.add(id(connection))
            if first:
                barrier.wait(timeout=5)
            return result

        def worker(index):
            try:
                results[index] = operation(connections[index])
            except BaseException as exc:
                failures.append(exc)

        with mock.patch.object(
            history_execution, replay_name, side_effect=synchronized_replay
        ):
            workers = [
                threading.Thread(target=worker, args=(index,))
                for index in range(2)
            ]
            for worker_thread in workers:
                worker_thread.start()
            for worker_thread in workers:
                worker_thread.join(10)
        for connection in connections:
            connection.close()
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(failures, [])
        self.assertEqual(results[0], results[1])
        return results[0]

    def test_concurrent_adjudication_materialization_exactly_replays_winner(self):
        plan = self._settle_exceptional_map()
        result = self._race_after_absent_replay(
            "_stored_adjudication_replay",
            lambda connection: history_execution.materialize_adjudication_tasks(
                connection, self.fixture.cas_root, plan, now=self.fixture._now(2)
            ),
        )
        self.assertEqual(result["state"], "materialized")
        self.assertEqual(len(result["detail_task_hashes"]), 1)
        counts = self.fixture.conn.execute(
            """
            SELECT
              (SELECT count(*) FROM audit_l2_adjudication_generations_v2),
              (SELECT count(*) FROM audit_l2_derived_task_authority_v2
               WHERE stage='detail'),
              (SELECT count(*) FROM audit_logical_tasks WHERE stage='detail')
            """
        ).fetchone()
        self.assertEqual(tuple(counts), (1, 1, 1))

    def test_concurrent_reduce_materialization_exactly_replays_winner(self):
        plan = self._settle_exceptional_map()
        materialized = history_execution.materialize_adjudication_tasks(
            self.fixture.conn, self.fixture.cas_root, plan,
            now=self.fixture._now(2),
        )
        self._settle_detail(plan, materialized["detail_task_hashes"][0])
        result = self._race_after_absent_replay(
            "_stored_reduce_replay",
            lambda connection: history_execution.materialize_reduce_tasks(
                connection, self.fixture.cas_root, plan, now=self.fixture._now(4)
            ),
        )
        self.assertEqual(result["state"], "materialized")
        self.assertEqual(len(result["reduce_task_hashes"]), 1)
        counts = self.fixture.conn.execute(
            """
            SELECT
              (SELECT count(*) FROM audit_l2_derived_task_authority_v2
               WHERE stage='reduce'),
              (SELECT count(*) FROM audit_logical_tasks WHERE stage='reduce')
            """
        ).fetchone()
        self.assertEqual(tuple(counts), (1, 1))

    def test_single_provider_retries_infrastructure_failure_consistently(self):
        plan = self.fixture._plan(self.fixture.records, map_providers=["codex"])
        self.fixture._install(plan)
        task_key = plan["logical_task_keys"][0]
        calls = []

        def provider(_task_key, provider_name, ordinal, _request):
            calls.append((provider_name, ordinal))
            if ordinal == 0:
                return {"kind": "timeout", "raw": "timed out"}
            return {"kind": "success", "output": self.fixture._output(plan)}

        result = history_execution.run_map_task(
            self.fixture.conn, self.fixture.cas_root, plan, task_key, provider,
            now=self.fixture._now(),
        )
        self.assertEqual(result["settlement_kind"], "equal")
        self.assertEqual(calls, [("codex", 0), ("codex", 1)])
        provenance = [
            history_execution._json(row[0])
            for row in self.fixture.conn.execute(
                "SELECT provenance_json FROM audit_task_attempts "
                "WHERE task_hash=? ORDER BY ordinal",
                (task_key,),
            )
        ]
        self.assertEqual(
            [(item["provider"], item["attempt_kind"]) for item in provenance],
            [("codex", "initial"), ("codex", "failover")],
        )

    def test_provider_exception_resume_uses_next_durable_ordinal(self):
        plan = self.fixture._install()
        task_key = plan["logical_task_keys"][0]

        with self.assertRaisesRegex(RuntimeError, "fixture provider failed"):
            history_execution.run_map_task(
                self.fixture.conn, self.fixture.cas_root, plan, task_key,
                lambda *_: (_ for _ in ()).throw(
                    RuntimeError("fixture provider failed")
                ),
                now=self.fixture._now(),
            )

        resumed_calls = []

        def resumed_provider(_task_key, provider_name, ordinal, _request):
            resumed_calls.append((provider_name, ordinal))
            return {"kind": "success", "output": self.fixture._output(plan)}

        result = history_execution.run_map_task(
            self.fixture.conn, self.fixture.cas_root, plan, task_key,
            resumed_provider, now=self.fixture._now(1),
        )
        self.assertEqual(result["settlement_kind"], "equal")
        self.assertEqual(resumed_calls, [("codex", 1)])
        attempts = self.fixture.conn.execute(
            """
            SELECT attempt.ordinal, json_extract(attempt.provenance_json,
                                                  '$.attempt_kind') AS kind,
                   COALESCE(completion.outcome, cost.outcome) AS outcome
            FROM audit_task_attempts attempt
            LEFT JOIN audit_attempt_completions_v2 completion USING(attempt_id)
            LEFT JOIN audit_attempt_cost_settlements_v2 cost USING(attempt_id)
            WHERE attempt.task_hash=? ORDER BY attempt.ordinal
            """,
            (task_key,),
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in attempts],
            [(0, "initial", "cancelled"), (1, "retry", "valid")],
        )


if __name__ == "__main__":
    unittest.main()
