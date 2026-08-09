#!/usr/bin/env python3
"""Defensive regressions for derived-task races and provider retries."""

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

    def _partial_valid_completion(self):
        plan = self.fixture._install()
        task_key = plan["logical_task_keys"][0]
        history_execution.claim_task(
            self.fixture.conn, task_key, "partial-worker", 60, 0,
            now=self.fixture._now(),
        )
        task = history_execution.load_task(self.fixture.conn, task_key)
        attempt = history_execution.record_attempt(
            self.fixture.conn, task_key,
            copy.deepcopy(plan["provider_capabilities"]["codex"]),
            {"attempt_kind": "initial"},
            cas_root=self.fixture.cas_root,
            request_bytes=task["durable_request_text"].encode(),
        )
        normalized = history_execution.validate_map_output(
            task, self.fixture._output(plan), plan["snapshot"]
        )
        raw = history_execution.history_contract_v2.canonical_bytes(
            self.fixture._output(plan)
        )
        output = history_execution.history_cas.put_object(
            self.fixture.conn, self.fixture.cas_root, raw,
            "attempt-transient-7d",
            expires_at=history_execution._attempt_expiry(task),
        )
        completed_at = self.fixture.conn.execute(
            "SELECT created_at FROM audit_task_attempts WHERE attempt_id=?",
            (attempt["attempt_id"],),
        ).fetchone()[0]
        self.fixture.conn.execute("BEGIN IMMEDIATE")
        history_audit_store.insert_attempt_completion(
            self.fixture.conn, attempt["attempt_id"], output["object_id"],
            "valid", history_execution._canonical(normalized),
            completed_at=completed_at,
        )
        self.fixture.conn.execute("COMMIT")
        valid = {
            "attempt_id": attempt["attempt_id"],
            "output_cas_object_id": output["object_id"],
            "normalized": normalized,
        }
        return plan, task, attempt, output, completed_at, valid

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

    def test_terminal_prior_retry_window_refuses_concurrent_claim(self):
        plan = self.fixture._install()
        task_key = plan["logical_task_keys"][0]
        connections = [
            sqlite3.connect(
                self.fixture.db_path, timeout=5, check_same_thread=False
            )
            for _ in range(2)
        ]
        for connection in connections:
            connection.row_factory = sqlite3.Row
            history_audit_store.init_schema(connection)
        retry_window = threading.Barrier(2)
        release_retry = threading.Event()
        original_record = history_execution.record_attempt
        first_calls = [0]
        first_results = []
        first_errors = []

        def blocking_record(connection, *args, **kwargs):
            if connection is connections[0]:
                first_calls[0] += 1
                if first_calls[0] == 2:
                    retry_window.wait(timeout=5)
                    if not release_retry.wait(5):
                        raise AssertionError("retry release timed out")
            return original_record(connection, *args, **kwargs)

        def provider(_task, _provider, ordinal, _request):
            if ordinal == 0:
                return {"kind": "syntax", "raw": "syntax"}
            return {"kind": "success", "output": self.fixture._output(plan)}

        def first_runner():
            try:
                first_results.append(history_execution.run_map_task(
                    connections[0], self.fixture.cas_root, plan, task_key,
                    provider, now=self.fixture._now(),
                ))
            except BaseException as exc:
                first_errors.append(exc)

        with mock.patch.object(
            history_execution, "record_attempt", side_effect=blocking_record
        ):
            worker = threading.Thread(target=first_runner)
            worker.start()
            retry_window.wait(timeout=5)
            second_calls = []
            with self.assertRaises(history_audit_store.StaleFence):
                history_execution.run_map_task(
                    connections[1], self.fixture.cas_root, plan, task_key,
                    lambda *_: second_calls.append(True),
                    now=self.fixture._now(),
                )
            self.assertEqual(second_calls, [])
            release_retry.set()
            worker.join(10)
        for connection in connections:
            connection.close()
        self.assertFalse(worker.is_alive())
        self.assertEqual(first_errors, [])
        self.assertEqual(first_results[0]["settlement_kind"], "equal")
        self.assertEqual(first_calls, [2])

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
    def test_live_provider_attempt_cannot_be_cancelled_by_concurrent_runner(self):
        plan = self.fixture._install()
        task_key = plan["logical_task_keys"][0]
        connections = [
            sqlite3.connect(
                self.fixture.db_path, timeout=5, check_same_thread=False
            )
            for _ in range(2)
        ]
        for connection in connections:
            connection.row_factory = sqlite3.Row
            history_audit_store.init_schema(connection)
        provider_started = threading.Barrier(2)
        release_provider = threading.Event()
        first_result = []
        first_errors = []

        def blocked_provider(*_):
            provider_started.wait(timeout=5)
            if not release_provider.wait(5):
                raise AssertionError("provider release timed out")
            return {"kind": "success", "output": self.fixture._output(plan)}

        def first_runner():
            try:
                first_result.append(history_execution.run_map_task(
                    connections[0], self.fixture.cas_root, plan, task_key,
                    blocked_provider, now=self.fixture._now(),
                ))
            except BaseException as exc:
                first_errors.append(exc)

        worker = threading.Thread(target=first_runner)
        worker.start()
        provider_started.wait(timeout=5)
        second_calls = []
        try:
            with self.assertRaises(history_audit_store.StaleFence):
                history_execution.run_map_task(
                    connections[1], self.fixture.cas_root, plan, task_key,
                    lambda *_: second_calls.append(True),
                    now=self.fixture._now(),
                )
            active = connections[1].execute(
                """
                SELECT completion.attempt_id, cost.attempt_id
                FROM audit_task_attempts attempt
                LEFT JOIN audit_attempt_completions_v2 completion USING(attempt_id)
                LEFT JOIN audit_attempt_cost_settlements_v2 cost USING(attempt_id)
                WHERE attempt.task_hash=? AND attempt.ordinal=0
                """,
                (task_key,),
            ).fetchone()
            self.assertEqual(tuple(active), (None, None))
            self.assertEqual(second_calls, [])
        finally:
            release_provider.set()
            worker.join(10)
            for connection in connections:
                connection.close()
        self.assertFalse(worker.is_alive())
        self.assertEqual(first_errors, [])
        self.assertEqual(first_result[0]["settlement_kind"], "equal")
        self.assertEqual(
            self.fixture.conn.execute(
                "SELECT count(*) FROM audit_task_attempts WHERE task_hash=?",
                (task_key,),
            ).fetchone()[0],
            1,
        )

    def test_resume_replays_committed_overflow_split_without_provider_call(self):
        plan = self.fixture._install()
        task_key = plan["logical_task_keys"][0]
        with mock.patch.object(
            history_execution, "split_task",
            side_effect=history_execution.ExecutionCrash(
                "crash after overflow completion"
            ),
        ):
            with self.assertRaises(history_execution.ExecutionCrash):
                history_execution.run_map_task(
                    self.fixture.conn, self.fixture.cas_root, plan, task_key,
                    lambda *_: {"kind": "overflow", "raw": "overflow"},
                    now=self.fixture._now(),
                )

        resumed_calls = []
        result = history_execution.run_map_task(
            self.fixture.conn, self.fixture.cas_root, plan, task_key,
            lambda *_: resumed_calls.append(True), now=self.fixture._now(1),
        )
        self.assertEqual(result["state"], "superseded")
        self.assertEqual(resumed_calls, [])
        parent = history_execution.load_task(self.fixture.conn, task_key)
        self.assertEqual(parent["state"], "superseded")
        self.assertEqual(
            self.fixture.conn.execute(
                "SELECT count(*) FROM audit_task_edges_v2 "
                "WHERE parent_task_hash=?",
                (task_key,),
            ).fetchone()[0],
            2,
        )

    def test_expired_overflow_claim_transfers_and_replays_after_recovery_crash(self):
        plan = self.fixture._install()
        task_key = plan["logical_task_keys"][0]
        with mock.patch.object(
            history_execution, "split_task",
            side_effect=history_execution.ExecutionCrash(
                "crash before overflow split"
            ),
        ):
            with self.assertRaises(history_execution.ExecutionCrash):
                history_execution.run_map_task(
                    self.fixture.conn, self.fixture.cas_root, plan, task_key,
                    lambda *_: {"kind": "overflow", "raw": "overflow"},
                    now=self.fixture._now(), lease_seconds=60,
                )
        with self.assertRaises(history_execution.ExecutionError) as expired:
            history_execution.run_map_task(
                self.fixture.conn, self.fixture.cas_root, plan, task_key,
                lambda *_: self.fail("expired failure called provider"),
                now=self.fixture._now(61),
            )
        self.assertEqual(expired.exception.code, "expired_split_failure_claim")
        self.assertEqual(
            history_execution.recover_run(
                self.fixture.conn, plan["plan_sha"],
                cas_root=self.fixture.cas_root, now=self.fixture._now(61),
            ),
            [task_key],
        )
        with mock.patch.object(
            history_execution, "split_task",
            side_effect=history_execution.ExecutionCrash(
                "crash after recovery claim transfer"
            ),
        ):
            with self.assertRaises(history_execution.ExecutionCrash):
                history_execution.run_map_task(
                    self.fixture.conn, self.fixture.cas_root, plan, task_key,
                    lambda *_: self.fail("recovered failure called provider"),
                    now=self.fixture._now(62), lease_seconds=60,
                )
        transferred = history_execution.load_task(self.fixture.conn, task_key)
        self.assertEqual(transferred["state"], "claimed")
        self.assertEqual(
            self.fixture.conn.execute(
                "SELECT count(*) FROM audit_l2_failure_claim_transfers_v3 "
                "WHERE task_hash=?", (task_key,),
            ).fetchone()[0],
            1,
        )
        resumed_calls = []
        result = history_execution.run_map_task(
            self.fixture.conn, self.fixture.cas_root, plan, task_key,
            lambda *_: resumed_calls.append(True), now=self.fixture._now(63),
        )
        self.assertEqual(result["state"], "superseded")
        self.assertEqual(resumed_calls, [])
        self.assertEqual(
            self.fixture.conn.execute(
                "SELECT count(*) FROM audit_task_edges_v2 "
                "WHERE parent_task_hash=?", (task_key,),
            ).fetchone()[0],
            2,
        )

    def test_resume_replays_final_syntax_exhaustion_without_third_attempt(self):
        plan = self.fixture._install()
        task_key = plan["logical_task_keys"][0]
        with mock.patch.object(
            history_execution, "exhaust_task",
            side_effect=history_execution.ExecutionCrash(
                "crash after final syntax completion"
            ),
        ):
            with self.assertRaises(history_execution.ExecutionCrash):
                history_execution.run_map_task(
                    self.fixture.conn, self.fixture.cas_root, plan, task_key,
                    lambda *_: {"kind": "syntax", "raw": "syntax"},
                    now=self.fixture._now(),
                )
        resumed_calls = []
        result = history_execution.run_map_task(
            self.fixture.conn, self.fixture.cas_root, plan, task_key,
            lambda *_: resumed_calls.append(True), now=self.fixture._now(1),
        )
        self.assertEqual(result["state"], "exhausted")
        self.assertEqual(resumed_calls, [])
        self.assertEqual(
            self.fixture.conn.execute(
                "SELECT reason FROM audit_task_terminal_facts_v2 "
                "WHERE task_hash=?",
                (task_key,),
            ).fetchone()[0],
            "provider_exhausted",
        )
        self.assertEqual(
            self.fixture.conn.execute(
                "SELECT count(*) FROM audit_task_attempts WHERE task_hash=?",
                (task_key,),
            ).fetchone()[0],
            2,
        )

    def test_resume_replays_final_timeout_exhaustion_without_third_attempt(self):
        plan = self.fixture._install()
        task_key = plan["logical_task_keys"][0]
        with mock.patch.object(
            history_execution, "exhaust_task",
            side_effect=history_execution.ExecutionCrash(
                "crash after final timeout completion"
            ),
        ):
            with self.assertRaises(history_execution.ExecutionCrash):
                history_execution.run_map_task(
                    self.fixture.conn, self.fixture.cas_root, plan, task_key,
                    lambda *_: {"kind": "timeout", "raw": "timed out"},
                    now=self.fixture._now(),
                )

        resumed_calls = []
        result = history_execution.run_map_task(
            self.fixture.conn, self.fixture.cas_root, plan, task_key,
            lambda *_: resumed_calls.append(True), now=self.fixture._now(1),
        )
        self.assertEqual(result["state"], "exhausted")
        self.assertEqual(resumed_calls, [])
        self.assertEqual(
            self.fixture.conn.execute(
                "SELECT reason FROM audit_task_terminal_facts_v2 "
                "WHERE task_hash=?",
                (task_key,),
            ).fetchone()[0],
            "provider_exhausted",
        )
        self.assertEqual(
            self.fixture.conn.execute(
                "SELECT count(*) FROM audit_task_attempts WHERE task_hash=?",
                (task_key,),
            ).fetchone()[0],
            2,
        )
    def test_production_attempt_requires_explicit_claim_authority(self):
        plan = self.fixture._install()
        task_key = plan["logical_task_keys"][0]
        history_execution.claim_task(
            self.fixture.conn, task_key, "worker", 60, 0,
            now=history_execution.load_task(
                self.fixture.conn, task_key
            )["created_at"],
        )
        production_task = history_execution.load_task(
            self.fixture.conn, task_key
        )
        production_task["durable_plan"]["authority_scope"] = "production"
        with (
            mock.patch.object(
                history_execution, "load_task", return_value=production_task
            ),
            mock.patch.object(
                history_execution, "_has_route_dispatch_authority",
                return_value=True,
            ),
        ):
            with self.assertRaises(history_execution.ExecutionError) as caught:
                history_execution.record_attempt(
                    self.fixture.conn, task_key,
                    copy.deepcopy(plan["provider_capabilities"]["codex"]),
                    {"attempt_kind": "initial"},
                    cas_root=self.fixture.cas_root,
                    request_bytes=plan["shards"][0][
                        "serialized_request"
                    ].encode(),
                )
        self.assertEqual(
            caught.exception.code, "attempt_claim_authority_required"
        )

    def test_attempt_cannot_start_at_expired_claim_timestamp(self):
        plan = self.fixture._install()
        task_key = plan["logical_task_keys"][0]
        task = history_execution.load_task(self.fixture.conn, task_key)
        claim = history_execution.claim_task(
            self.fixture.conn, task_key, "worker", 0, 0,
            now=task["created_at"],
        )
        with self.assertRaises(history_audit_store.StaleFence):
            history_execution.record_attempt(
                self.fixture.conn, task_key,
                copy.deepcopy(plan["provider_capabilities"]["codex"]),
                {"attempt_kind": "initial"},
                cas_root=self.fixture.cas_root,
                request_bytes=task["durable_request_text"].encode(),
                claim_fence=claim["fence"],
                claim_token=claim["claim_token"],
                claim_now=task["created_at"], now=task["created_at"],
            )
        self.assertEqual(
            self.fixture.conn.execute(
                "SELECT count(*) FROM audit_task_attempts WHERE task_hash=?",
                (task_key,),
            ).fetchone()[0],
            0,
        )

    def test_failed_completion_rechecks_reclaimed_fence_under_write_lock(self):
        plan = self.fixture._install()
        task_key = plan["logical_task_keys"][0]
        connections = [
            sqlite3.connect(
                self.fixture.db_path, timeout=5, check_same_thread=False
            )
            for _ in range(2)
        ]
        for connection in connections:
            connection.row_factory = sqlite3.Row
            history_audit_store.init_schema(connection)
        task = history_execution.load_task(connections[0], task_key)
        claim = history_execution.claim_task(
            connections[0], task_key, "worker-a", 1, 0,
            now=task["created_at"],
        )
        task = history_execution.load_task(connections[0], task_key)
        attempt = history_execution.record_attempt(
            connections[0], task_key,
            copy.deepcopy(plan["provider_capabilities"]["codex"]),
            {"attempt_kind": "initial"},
            cas_root=self.fixture.cas_root,
            request_bytes=task["durable_request_text"].encode(),
            claim_fence=claim["fence"], claim_token=claim["claim_token"],
            claim_now=task["created_at"], now=task["created_at"],
        )
        output_written = threading.Barrier(2)
        release_failure = threading.Event()
        original_put = history_execution.history_cas.put_object
        failures = []

        def blocked_put(*args, **kwargs):
            result = original_put(*args, **kwargs)
            output_written.wait(timeout=5)
            if not release_failure.wait(5):
                raise AssertionError("failed completion release timed out")
            return result

        def stale_failure():
            try:
                history_execution._failed_completion(
                    connections[0], self.fixture.cas_root, task,
                    attempt["attempt_id"], "timeout", "timed out", None,
                    claim_fence=claim["fence"],
                    claim_token=claim["claim_token"],
                    authority_now=claim["lease_until"],
                    now=claim["lease_until"],
                )
            except BaseException as exc:
                failures.append(exc)

        with mock.patch.object(
            history_execution.history_cas, "put_object",
            side_effect=blocked_put,
        ):
            worker = threading.Thread(target=stale_failure)
            worker.start()
            output_written.wait(timeout=5)
            recovered = history_execution.recover_run(
                connections[1], plan["plan_sha"],
                cas_root=self.fixture.cas_root,
                now=claim["lease_until"],
            )
            self.assertEqual(recovered, [task_key])
            release_failure.set()
            worker.join(10)
        for connection in connections:
            connection.close()
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], history_audit_store.StaleFence)
        counts = self.fixture.conn.execute(
            """
            SELECT
              (SELECT count(*) FROM audit_attempt_completions_v2
               WHERE attempt_id=?),
              (SELECT count(*) FROM audit_runtime_budget_settlements_v2
               WHERE attempt_id=?),
              (SELECT count(*) FROM audit_attempt_cost_settlements_v2
               WHERE attempt_id=?)
            """,
            (attempt["attempt_id"],) * 3,
        ).fetchone()
        self.assertEqual(tuple(counts), (0, 0, 0))

    def test_run_task_refreshes_production_completion_timestamp(self):
        plan = self.fixture._install()
        task_key = plan["logical_task_keys"][0]
        task_ready_at = history_execution.load_task(
            self.fixture.conn, task_key
        )["created_at"]
        original_load = history_execution.load_task
        original_now = history_execution._now
        completion_at = (
            original_now(task_ready_at) + datetime.timedelta(seconds=1)
        ).isoformat()
        clock = [original_now(task_ready_at)]

        def production_load(connection, key):
            task = original_load(connection, key)
            task["durable_plan"]["authority_scope"] = "production"
            return task

        def controlled_now(value=None):
            return original_now(value) if value is not None else clock[0]

        def provider(*_):
            clock[0] = original_now(completion_at)
            return {"kind": "success", "output": self.fixture._output(plan)}

        with (
            mock.patch.object(
                history_execution, "load_task", side_effect=production_load
            ),
            mock.patch.object(
                history_execution, "_now", side_effect=controlled_now
            ),
        ):
            history_execution.run_map_task(
                self.fixture.conn, self.fixture.cas_root, plan, task_key,
                provider, now=task_ready_at,
            )
        timestamps = self.fixture.conn.execute(
            """
            SELECT attempt.created_at, completion.completed_at,
                   budget.created_at, cost.completed_at, settlement.settled_at
            FROM audit_task_attempts attempt
            JOIN audit_attempt_completions_v2 completion USING(attempt_id)
            JOIN audit_runtime_budget_settlements_v2 budget USING(attempt_id)
            JOIN audit_attempt_cost_settlements_v2 cost USING(attempt_id)
            JOIN audit_task_settlements_v2 settlement USING(task_hash)
            WHERE attempt.task_hash=?
            """,
            (task_key,),
        ).fetchone()
        self.assertEqual(
            tuple(timestamps),
            (task_ready_at, completion_at, completion_at, completion_at,
             completion_at),
        )
    def test_blocking_production_provider_cannot_complete_after_lease(self):
        plan = self.fixture._install()
        task_key = plan["logical_task_keys"][0]
        connection = sqlite3.connect(
            self.fixture.db_path, timeout=5, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        history_audit_store.init_schema(connection)
        task_ready_at = history_execution.load_task(
            connection, task_key
        )["created_at"]
        original_load = history_execution.load_task
        original_now = history_execution._now
        completion_at = (
            original_now(task_ready_at) + datetime.timedelta(seconds=2)
        ).isoformat()
        clock = [original_now(task_ready_at)]
        provider_entered = threading.Barrier(2)
        release_provider = threading.Event()
        failures = []

        def production_load(target, key):
            task = original_load(target, key)
            task["durable_plan"]["authority_scope"] = "production"
            return task

        def controlled_now(value=None):
            return original_now(value) if value is not None else clock[0]

        def blocking_provider(*_):
            provider_entered.wait(timeout=5)
            if not release_provider.wait(5):
                raise AssertionError("provider release timed out")
            return {"kind": "timeout", "raw": "timed out"}

        def runner():
            try:
                history_execution.run_map_task(
                    connection, self.fixture.cas_root, plan, task_key,
                    blocking_provider, now=task_ready_at, lease_seconds=1,
                )
            except BaseException as exc:
                failures.append(exc)

        with (
            mock.patch.object(
                history_execution, "load_task", side_effect=production_load
            ),
            mock.patch.object(
                history_execution, "_now", side_effect=controlled_now
            ),
        ):
            worker = threading.Thread(target=runner)
            worker.start()
            provider_entered.wait(timeout=5)
            clock[0] = original_now(completion_at)
            release_provider.set()
            worker.join(10)
        connection.close()
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], history_audit_store.StaleFence)
        attempt_id = self.fixture.conn.execute(
            "SELECT attempt_id FROM audit_task_attempts WHERE task_hash=?",
            (task_key,),
        ).fetchone()[0]
        counts = self.fixture.conn.execute(
            """
            SELECT
              (SELECT count(*) FROM audit_attempt_completions_v2
               WHERE attempt_id=?),
              (SELECT count(*) FROM audit_runtime_budget_settlements_v2
               WHERE attempt_id=?),
              (SELECT count(*) FROM audit_attempt_cost_settlements_v2
               WHERE attempt_id=?)
            """,
            (attempt_id,) * 3,
        ).fetchone()
        self.assertEqual(tuple(counts), (0, 0, 0))
    def test_settlement_rejects_partially_terminal_attempt_then_replays_exactly(self):
        plan, task, attempt, output, completed_at, valid = (
            self._partial_valid_completion()
        )
        task_key = plan["logical_task_keys"][0]
        with self.assertRaises(history_execution.ExecutionError) as caught:
            history_execution.settle_task(
                self.fixture.conn, task_key, [valid],
                cas_root=self.fixture.cas_root, now=self.fixture._now(1),
            )
        self.assertEqual(caught.exception.code, "outstanding_task_attempt")
        self.assertIsNone(self.fixture.conn.execute(
            "SELECT 1 FROM audit_task_settlements_v2 WHERE task_hash=?",
            (task_key,),
        ).fetchone())

        self.fixture.conn.execute("BEGIN IMMEDIATE")
        history_execution._insert_completion(
            self.fixture.conn, task, attempt["attempt_id"],
            output["object_id"], "valid", valid["normalized"], None,
            now=completed_at,
        )
        self.fixture.conn.execute("COMMIT")
        first = history_execution.settle_task(
            self.fixture.conn, task_key, [valid],
            cas_root=self.fixture.cas_root, now=self.fixture._now(1),
        )
        replay = history_execution.settle_task(
            self.fixture.conn, task_key, [valid],
            cas_root=self.fixture.cas_root, now=self.fixture._now(1),
        )
        self.assertEqual(replay, first)

    def test_settle_vs_completion_race_freezes_terminal_attempt_set(self):
        connections = [
            sqlite3.connect(
                self.fixture.db_path, timeout=5, check_same_thread=False
            )
            for _ in range(2)
        ]
        for connection in connections:
            connection.row_factory = sqlite3.Row
            history_audit_store.init_schema(connection)
        plan, task, attempt, output, completed_at, valid = (
            self._partial_valid_completion()
        )
        task_key = plan["logical_task_keys"][0]
        settlement_read = threading.Barrier(2)
        release_settlement = threading.Event()
        original_verify = history_execution.history_cas.verify_object
        results = []
        failures = []
        blocked = [False]

        def blocking_verify(connection, *args, **kwargs):
            result = original_verify(connection, *args, **kwargs)
            if connection is connections[0] and not blocked[0]:
                blocked[0] = True
                settlement_read.wait(timeout=5)
                if not release_settlement.wait(5):
                    raise AssertionError("settlement release timed out")
            return result

        def settle_worker():
            try:
                results.append(history_execution.settle_task(
                    connections[0], task_key, [valid],
                    cas_root=self.fixture.cas_root, now=self.fixture._now(1),
                ))
            except BaseException as exc:
                failures.append(exc)

        with mock.patch.object(
            history_execution.history_cas, "verify_object",
            side_effect=blocking_verify,
        ):
            worker = threading.Thread(target=settle_worker)
            worker.start()
            settlement_read.wait(timeout=5)
            connections[1].execute("BEGIN IMMEDIATE")
            current_task = history_execution.load_task(
                connections[1], task_key
            )
            history_execution._insert_completion(
                connections[1], current_task, attempt["attempt_id"],
                output["object_id"], "valid", valid["normalized"], None,
                now=completed_at,
            )
            connections[1].execute("COMMIT")
            release_settlement.set()
            worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        replay = history_execution.settle_task(
            connections[0], task_key, [valid],
            cas_root=self.fixture.cas_root, now=self.fixture._now(1),
        )
        self.assertEqual(replay, results[0])
        for connection in connections:
            connection.close()


if __name__ == "__main__":
    unittest.main()
