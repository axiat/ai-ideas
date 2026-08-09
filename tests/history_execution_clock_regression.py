#!/usr/bin/env python3
"""Regression tests for explicit history execution timestamps."""

import datetime
import hashlib
import pathlib
import sqlite3
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_execution


class HistoryExecutionClockRegression(unittest.TestCase):
    def test_now_rejects_explicit_invalid_values(self):
        invalid_values = (
            "",
            0,
            datetime.datetime(2001, 2, 3, 4, 5, 6),
        )
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(history_execution.ExecutionError) as caught:
                    history_execution._now(value)
                self.assertEqual(caught.exception.code, "invalid_timestamp")

    def test_now_accepts_aware_datetime_and_iso_string(self):
        aware = datetime.datetime(
            2001, 2, 3, 4, 5, 6,
            tzinfo=datetime.timezone(datetime.timedelta(hours=2)),
        )
        expected = datetime.datetime(
            2001, 2, 3, 2, 5, 6, tzinfo=datetime.timezone.utc
        )
        self.assertEqual(history_execution._now(aware), expected)
        self.assertEqual(history_execution._now("2001-02-03T02:05:06Z"), expected)

    def test_settle_task_uses_injected_timestamp_for_lease_and_settlement(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE audit_task_attempts(
              attempt_id TEXT PRIMARY KEY,
              task_hash TEXT NOT NULL,
              request_cas_object_id TEXT NOT NULL
            );
            CREATE TABLE audit_attempt_completions_v2(
              attempt_id TEXT PRIMARY KEY,
              output_cas_object_id TEXT NOT NULL,
              outcome TEXT NOT NULL,
              normalized_result_json TEXT
            );
            CREATE TABLE audit_attempt_cost_settlements_v2(
              attempt_id TEXT PRIMARY KEY
            );
            CREATE TABLE audit_runtime_budget_settlements_v2(
              attempt_id TEXT PRIMARY KEY
            );
            CREATE TABLE audit_task_settlements_v2(
              task_hash TEXT PRIMARY KEY,
              settlement_sha256 TEXT NOT NULL,
              settlement_kind TEXT NOT NULL,
              normalized_result_json TEXT,
              valid_attempt_ids_json TEXT NOT NULL,
              valid_output_cas_ids_json TEXT NOT NULL,
              settled_at TEXT NOT NULL
            );
            """
        )
        task_key = hashlib.sha256(b"task").hexdigest()
        attempt_id = hashlib.sha256(b"attempt").hexdigest()
        normalized = {"result": "valid"}
        conn.execute(
            "INSERT INTO audit_task_attempts VALUES(?, ?, ?)",
            (attempt_id, task_key, "request-object"),
        )
        conn.execute(
            "INSERT INTO audit_attempt_completions_v2 VALUES(?, ?, 'valid', ?)",
            (attempt_id, "output-object", history_execution._canonical(normalized)),
        )
        conn.execute(
            "INSERT INTO audit_attempt_cost_settlements_v2 VALUES(?)",
            (attempt_id,),
        )
        conn.execute(
            "INSERT INTO audit_runtime_budget_settlements_v2 VALUES(?)",
            (attempt_id,),
        )
        conn.commit()

        injected = datetime.datetime(
            2001, 2, 3, 4, 5, 6,
            tzinfo=datetime.timezone(datetime.timedelta(hours=2)),
        )
        expected = "2001-02-03T02:05:06+00:00"
        task = {
            "state": "claimed",
            "lease_until": "2001-02-03T02:05:07+00:00",
            "fence": 7,
            "claim_token": "worker",
        }
        with (
            mock.patch.object(history_execution, "load_task", return_value=task),
            mock.patch.object(
                history_execution.history_cas,
                "verify_object",
                return_value={"integrity_state": "present"},
            ),
            mock.patch.object(
                history_execution.history_audit_store,
                "compare_and_set_logical_task",
            ),
        ):
            history_execution.settle_task(
                conn,
                task_key,
                [{"attempt_id": attempt_id, "normalized": normalized}],
                cas_root=ROOT,
                now=injected,
            )

        settled_at = conn.execute(
            "SELECT settled_at FROM audit_task_settlements_v2 WHERE task_hash=?",
            (task_key,),
        ).fetchone()[0]
        self.assertEqual(settled_at, expected)
        conn.close()


if __name__ == "__main__":
    unittest.main()
