#!/usr/bin/env python3
"""RED contract for host-issued verified actual usage authority."""

import datetime
import hashlib
import importlib.util
import json
import pathlib
import re
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_audit_eval_v2
from lib import history_audit_store
from lib import history_cas
from lib import history_contract_v2
from lib import history_execution


AUTHORITY_TABLE = "audit_verified_usage_authorities_v2"


def sha(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _load_runtime_fixture_module():
    path = ROOT / "tests/history_audit_runtime_smoke.py"
    spec = importlib.util.spec_from_file_location(
        "_history_verified_usage_runtime_fixture", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNTIME_FIXTURE = _load_runtime_fixture_module()


class HistoryVerifiedUsageAuthoritySmoke(unittest.TestCase):
    """Verified actual usage is opaque, durable, exact, and additive."""

    def setUp(self):
        self.runtime = RUNTIME_FIXTURE.HistoryAuditRuntimeSmoke(
            "test_completion_rejects_empty_and_zero_caller_usage_authority"
        )
        self.runtime.setUp()

    def tearDown(self):
        self.runtime.tearDown()

    def _api(self, name):
        value = getattr(history_execution, name, None)
        self.assertTrue(
            callable(value), f"missing behavior: history_execution.{name}"
        )
        return value

    def _issuer_api(self):
        value = getattr(
            history_audit_store,
            "_issue_test_verified_usage_authority",
            None,
        )
        self.assertTrue(
            callable(value),
            "missing behavior: "
            "history_audit_store._issue_test_verified_usage_authority",
        )
        return value

    def _authority_columns(self):
        exists = self.runtime.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (AUTHORITY_TABLE,),
        ).fetchone()
        self.assertIsNotNone(
            exists, f"missing behavior: {AUTHORITY_TABLE}"
        )
        return [
            row["name"]
            for row in self.runtime.conn.execute(
                f"PRAGMA table_info({AUTHORITY_TABLE})"
            )
        ]

    def _two_shard_plan(self):
        return self.runtime._plan(
            self.runtime.records,
            shards=[
                {"shard_id": "map-0000", "item_ids": ["asset-1"]},
                {"shard_id": "map-0001", "item_ids": ["asset-2"]},
            ],
        )

    def _start(self, *, plan=None, task_index=0, install=True):
        plan = plan or self.runtime.plan
        if install:
            self.runtime._install(plan)
        task_key = plan["logical_task_keys"][task_index]
        ready_at = self.runtime.conn.execute(
            "SELECT created_at FROM audit_logical_tasks WHERE task_hash=?",
            (task_key,),
        ).fetchone()[0]
        self._api("claim_task")(
            self.runtime.conn,
            task_key,
            f"worker-{task_index}",
            60,
            0,
            now=ready_at,
        )
        attempt = self._api("record_attempt")(
            self.runtime.conn,
            task_key,
            plan["provider_capabilities"]["codex"],
            {"attempt_kind": "initial"},
            cas_root=self.runtime.cas_root,
            request_bytes=plan["shards"][task_index][
                "serialized_request"
            ].encode("utf-8"),
            now=ready_at,
        )
        terminal_at = (
            datetime.datetime.fromisoformat(ready_at)
            + datetime.timedelta(seconds=1)
        ).isoformat()
        return {
            "plan": plan,
            "task_index": task_index,
            "task_key": task_key,
            "attempt": attempt,
            "terminal_at": terminal_at,
        }

    def _raw_bytes(self, output):
        if isinstance(output, (dict, list)):
            return history_contract_v2.canonical_bytes(output)
        if isinstance(output, bytes):
            return output
        return str(output).encode("utf-8")

    def _publish_output(self, started, output):
        task = history_execution.load_task(
            self.runtime.conn, started["task_key"]
        )
        descriptor = history_cas.put_object(
            self.runtime.conn,
            self.runtime.cas_root,
            self._raw_bytes(output),
            "attempt-transient-7d",
            expires_at=history_execution._attempt_expiry(task),
        )
        return descriptor["object_id"]

    def _issue(
        self,
        started,
        *,
        output_cas_object_id,
        terminal_outcome="valid",
        actual_usage=None,
        billing_state="unknown",
        price_source=None,
        currency=None,
        terminal_at=None,
    ):
        return self._issuer_api()(
            self.runtime.conn,
            attempt_id=started["attempt"]["attempt_id"],
            output_cas_object_id=output_cas_object_id,
            terminal_outcome=terminal_outcome,
            terminal_at=terminal_at or started["terminal_at"],
            actual_usage=(
                {
                    "input_tokens": 3,
                    "output_tokens": 2,
                    "provider_usage_units": 5,
                }
                if actual_usage is None
                else actual_usage
            ),
            billing_state=billing_state,
            price_source=price_source,
            currency=currency,
        )

    def _complete(self, started, output, token, *, now=None):
        return self._api("complete_attempt")(
            self.runtime.conn,
            self.runtime.cas_root,
            started["task_key"],
            started["attempt"]["attempt_id"],
            output,
            started["plan"]["snapshot"],
            usage=token,
            now=now or started["terminal_at"],
        )

    def _summary(self, started):
        return history_audit_eval_v2.summarize_realized_cost(
            self.runtime.conn, started["plan"]["run_id"]
        )["intents"][started["plan"]["intent"]]

    def test_verified_usage_is_an_additive_private_sidecar(self):
        migrations = history_audit_store.MIGRATIONS
        verified_index = next(
            index for index, migration in enumerate(migrations)
            if migration.component == "verified-usage-authority"
        )
        router_index = next(
            index for index, migration in enumerate(migrations)
            if migration.component == "router-source-authority"
        )
        l1_index = next(
            index for index, migration in enumerate(migrations)
            if migration.component == "l1-cost-authority"
        )
        self.assertLess(router_index, verified_index)
        self.assertLess(verified_index, l1_index)
        prefix_rows = [
            (migration.component, migration.version, migration.sha256)
            for migration in migrations[:verified_index]
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "additive.sqlite3"
            prefix = sqlite3.connect(path)
            prefix.row_factory = sqlite3.Row
            with mock.patch.object(
                history_audit_store, "MIGRATIONS", migrations[:verified_index]
            ):
                history_audit_store.init_schema(prefix)
            before = [
                tuple(row)
                for row in prefix.execute(
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
                tuple(row)
                for row in reopened.execute(
                    "SELECT component,version,migration_sha256 "
                    "FROM audit_schema_migrations ORDER BY rowid LIMIT ?",
                    (verified_index,),
                )
            ]
            self.assertEqual(after, before)
            reopened.close()
        completion_columns = [
            row["name"]
            for row in self.runtime.conn.execute(
                "PRAGMA table_info(audit_attempt_completions_v2)"
            )
        ]
        self.assertEqual(
            completion_columns,
            [
                "attempt_id",
                "output_cas_object_id",
                "outcome",
                "normalized_result_json",
                "usage_json",
                "completed_at",
            ],
        )
        required = {
            "usage_authority_sha256",
            "attempt_id",
            "run_id",
            "intent",
            "candidate_id",
            "provider",
            "capability_profile_hash",
            "request_cas_object_id",
            "output_cas_object_id",
            "terminal_outcome",
            "actual_json",
            "billing_state",
            "price_source",
            "currency",
            "terminal_at",
            "authority_scope",
        }
        self.assertTrue(required.issubset(self._authority_columns()))
        self._issuer_api()
        self.assertFalse(
            callable(
                getattr(
                    history_audit_store,
                    "issue_verified_usage_authority",
                    None,
                )
            ),
            "fake authority issuer must remain private and test-only",
        )

    def test_public_usage_dict_remains_rejected_without_minting_authority(self):
        started = self._start()
        with self.assertRaises(self._api("ExecutionError")) as caught:
            self._complete(
                started,
                self.runtime._output(),
                {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "provider_usage_units": 2,
                },
            )
        self.assertEqual(caught.exception.code, "usage_authority_unavailable")
        self._authority_columns()
        self.assertEqual(
            self.runtime.conn.execute(
                f"SELECT count(*) FROM {AUTHORITY_TABLE}"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.runtime.conn.execute(
                "SELECT count(*) FROM audit_attempt_completions_v2"
            ).fetchone()[0],
            0,
        )

    def test_private_issuer_returns_opaque_exact_attempt_authority(self):
        started = self._start()
        output = self.runtime._output()
        output_id = self._publish_output(started, output)
        actual = {
            "input_tokens": 3,
            "output_tokens": 2,
            "provider_usage_units": 5,
        }
        token = self._issue(
            started,
            output_cas_object_id=output_id,
            actual_usage=actual,
        )
        self.assertIsInstance(token, str)
        self.assertRegex(token, re.compile(r"^[0-9a-f]{64}$"))
        row = self.runtime.conn.execute(
            f"SELECT * FROM {AUTHORITY_TABLE} "
            "WHERE usage_authority_sha256=?",
            (token,),
        ).fetchone()
        self.assertIsNotNone(row)
        provenance = started["attempt"]["provenance"]
        plan = started["plan"]
        self.assertEqual(row["attempt_id"], started["attempt"]["attempt_id"])
        self.assertEqual(row["run_id"], plan["run_id"])
        self.assertEqual(row["intent"], plan["intent"])
        self.assertEqual(row["candidate_id"], plan["candidate"]["candidate_id"])
        self.assertEqual(row["provider"], provenance["provider"])
        self.assertEqual(
            row["capability_profile_hash"],
            provenance["capability_profile_hash"],
        )
        self.assertEqual(
            row["request_cas_object_id"],
            started["attempt"]["request_cas_object_id"],
        )
        self.assertEqual(row["output_cas_object_id"], output_id)
        self.assertEqual(row["terminal_outcome"], "valid")
        self.assertEqual(row["terminal_at"], started["terminal_at"])
        self.assertEqual(
            row["actual_json"],
            history_contract_v2.canonical_bytes(actual).decode("utf-8"),
        )
        self.assertEqual(row["billing_state"], "unknown")
        self.assertIsNone(row["price_source"])
        self.assertIsNone(row["currency"])
        self.assertEqual(row["authority_scope"], "test_fake")

    def test_budget_settlement_currency_presence_matches_reservation(self):
        required = {
            "input_tokens": 3,
            "output_tokens": 2,
            "provider_usage_units": 5,
        }
        cases = (
            (required, required, 1),
            ({**required, "currency_micros": 9},
             {**required, "currency_micros": 7}, 1),
            ({**required, "currency_micros": 9}, required, 0),
            (required, {**required, "currency_micros": 7}, 0),
        )
        for reserved, actual, expected in cases:
            with self.subTest(
                reserved_currency="currency_micros" in reserved,
                actual_currency="currency_micros" in actual,
            ):
                valid = self.runtime.conn.execute(
                    "SELECT audit_l2_budget_settlement_valid(1, ?, ?)",
                    (
                        history_contract_v2.canonical_bytes(actual).decode(),
                        history_contract_v2.canonical_bytes(reserved).decode(),
                    ),
                ).fetchone()[0]
                self.assertEqual(valid, expected)

    def test_verified_actual_overrides_reservation_but_completion_stays_empty(self):
        started = self._start()
        output = self.runtime._output()
        output_id = self._publish_output(started, output)
        actual = {
            "input_tokens": 3,
            "output_tokens": 2,
            "provider_usage_units": 5,
        }
        token = self._issue(
            started,
            output_cas_object_id=output_id,
            actual_usage=actual,
        )
        self._complete(started, output, token)
        row = self.runtime.conn.execute(
            """
            SELECT reservation.reserved_json,
                   budget.usage_verified, budget.actual_json,
                   completion.usage_json AS completion_usage_json,
                   cost.usage_source
            FROM audit_runtime_budget_reservations_v2 reservation
            JOIN audit_runtime_budget_settlements_v2 budget USING(attempt_id)
            JOIN audit_attempt_completions_v2 completion USING(attempt_id)
            JOIN audit_attempt_cost_settlements_v2 cost USING(attempt_id)
            WHERE reservation.attempt_id=?
            """,
            (started["attempt"]["attempt_id"],),
        ).fetchone()
        self.assertNotEqual(json.loads(row["reserved_json"]), actual)
        self.assertEqual(row["usage_verified"], 1)
        self.assertEqual(json.loads(row["actual_json"]), actual)
        self.assertEqual(
            row["completion_usage_json"],
            history_contract_v2.canonical_bytes({}).decode("utf-8"),
        )
        self.assertEqual(row["usage_source"], "verified_actual")
        task = history_execution.load_task(
            self.runtime.conn, started["task_key"]
        )
        totals = history_execution._effective_budget_totals(
            self.runtime.conn, task, candidate_only=False
        )
        self.assertEqual(
            totals,
            {"started_attempts": 1, **actual},
        )

    def test_token_rejects_attempt_output_and_terminal_mismatch_then_replays(self):
        plan = self._two_shard_plan()
        first = self._start(plan=plan, task_index=0)
        second = self._start(plan=plan, task_index=1, install=False)
        first_output = self.runtime._output(
            plan, item_ids=plan["shards"][0]["item_ids"]
        )
        second_output = self.runtime._output(
            plan, item_ids=plan["shards"][1]["item_ids"]
        )
        first_output_id = self._publish_output(first, first_output)
        token = self._issue(
            first, output_cas_object_id=first_output_id
        )
        with self.assertRaises(self._api("ExecutionError")) as caught:
            self._complete(second, second_output, token)
        self.assertEqual(
            caught.exception.code, "verified_usage_authority_mismatch"
        )
        late = (
            datetime.datetime.fromisoformat(first["terminal_at"])
            + datetime.timedelta(seconds=1)
        ).isoformat()
        with self.assertRaises(self._api("ExecutionError")) as caught:
            self._complete(first, first_output, token, now=late)
        self.assertEqual(
            caught.exception.code, "verified_usage_authority_mismatch"
        )
        changed_output = self.runtime._output(
            plan,
            item_ids=plan["shards"][0]["item_ids"],
            relations={"asset-1": "related_only"},
        )
        with self.assertRaises(self._api("ExecutionError")) as caught:
            self._complete(first, changed_output, token)
        self.assertEqual(
            caught.exception.code, "verified_usage_authority_mismatch"
        )
        self.assertEqual(
            tuple(
                self.runtime.conn.execute(
                    """
                    SELECT
                      (SELECT count(*) FROM audit_attempt_completions_v2),
                      (SELECT count(*) FROM audit_runtime_budget_settlements_v2),
                      (SELECT count(*) FROM audit_attempt_cost_settlements_v2)
                    """
                ).fetchone()
            ),
            (0, 0, 0),
        )
        self._complete(first, first_output, token)
        self.assertEqual(
            self.runtime.conn.execute(
                "SELECT count(*) FROM audit_attempt_completions_v2"
            ).fetchone()[0],
            1,
        )

    def test_actual_over_reservation_is_recorded_and_blocks_retry_budget(self):
        started = self._start()
        raw = b"not-json"
        output_id = self._publish_output(started, raw)
        actual = {
            "input_tokens": 300000,
            "output_tokens": 1,
            "provider_usage_units": 300001,
        }
        token = self._issue(
            started,
            output_cas_object_id=output_id,
            terminal_outcome="syntax",
            actual_usage=actual,
        )
        with self.assertRaises(history_execution.MapValidationError) as caught:
            self._complete(started, raw, token)
        self.assertEqual(caught.exception.code, "syntax")
        row = self.runtime.conn.execute(
            """
            SELECT reservation.reserved_json,
                   budget.usage_verified, budget.actual_json,
                   completion.usage_json, cost.usage_source, cost.outcome
            FROM audit_runtime_budget_reservations_v2 reservation
            JOIN audit_runtime_budget_settlements_v2 budget USING(attempt_id)
            JOIN audit_attempt_completions_v2 completion USING(attempt_id)
            JOIN audit_attempt_cost_settlements_v2 cost USING(attempt_id)
            WHERE reservation.attempt_id=?
            """,
            (started["attempt"]["attempt_id"],),
        ).fetchone()
        self.assertLess(
            json.loads(row["reserved_json"])["provider_usage_units"],
            actual["provider_usage_units"],
        )
        self.assertEqual(row["usage_verified"], 1)
        self.assertEqual(json.loads(row["actual_json"]), actual)
        self.assertEqual(
            row["usage_json"],
            history_contract_v2.canonical_bytes({}).decode("utf-8"),
        )
        self.assertEqual(
            (row["usage_source"], row["outcome"]),
            ("verified_actual", "failed"),
        )
        realized = self._summary(started)["realized"]
        self.assertEqual(realized["failed_calls"], 1)
        self.assertEqual(realized["input_tokens"], actual["input_tokens"])
        self.assertEqual(
            realized["provider_usage_units"],
            actual["provider_usage_units"],
        )
        with self.assertRaises(self._api("ExecutionError")) as budget:
            self._api("record_attempt")(
                self.runtime.conn,
                started["task_key"],
                started["plan"]["provider_capabilities"]["codex"],
                {"attempt_kind": "retry"},
                cas_root=self.runtime.cas_root,
                request_bytes=started["plan"]["shards"][0][
                    "serialized_request"
                ].encode("utf-8"),
                now=started["terminal_at"],
            )
        self.assertEqual(budget.exception.code, "attempt_budget_exceeded")

    def test_unknown_currency_is_not_fabricated_as_zero(self):
        started = self._start()
        output = self.runtime._output()
        output_id = self._publish_output(started, output)
        token = self._issue(
            started,
            output_cas_object_id=output_id,
            billing_state="unknown",
            price_source=None,
            currency=None,
        )
        self._complete(started, output, token)
        summary = self._summary(started)
        self.assertFalse(summary["currency_complete"])
        self.assertNotIn("currency_micros", summary["realized"])
        self.assertEqual(summary["realized"]["unverified_usage_calls"], 0)

    def test_actual_currency_requires_currency_reservation(self):
        started = self._start()
        actual = {
            "input_tokens": 4,
            "output_tokens": 1,
            "provider_usage_units": 5,
            "currency_micros": 7,
        }
        token = self._issue(
            started,
            output_cas_object_id=None,
            terminal_outcome="cancelled",
            actual_usage=actual,
            billing_state="billable",
            price_source="fake-price-v1",
            currency="USD",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self._api("cancel_attempt")(
                self.runtime.conn,
                started["attempt"]["attempt_id"],
                billing_state="billable",
                usage=token,
                now=started["terminal_at"],
            )
        counts = self.runtime.conn.execute(
            """
            SELECT
              (SELECT count(*) FROM audit_runtime_budget_settlements_v2),
              (SELECT count(*) FROM audit_attempt_cost_settlements_v2),
              (SELECT count(*) FROM audit_attempt_completions_v2)
            """
        ).fetchone()
        self.assertEqual(tuple(counts), (0, 0, 0))

    def test_direct_sql_cannot_forge_update_or_delete_authority(self):
        plan = self._two_shard_plan()
        first = self._start(plan=plan, task_index=0)
        second = self._start(plan=plan, task_index=1, install=False)
        first_output = self.runtime._output(
            plan, item_ids=plan["shards"][0]["item_ids"]
        )
        second_output = self.runtime._output(
            plan, item_ids=plan["shards"][1]["item_ids"]
        )
        first_output_id = self._publish_output(first, first_output)
        second_output_id = self._publish_output(second, second_output)
        token = self._issue(first, output_cas_object_id=first_output_id)
        self.runtime.conn.commit()
        columns = self._authority_columns()
        valid = dict(
            self.runtime.conn.execute(
                f"SELECT * FROM {AUTHORITY_TABLE} "
                "WHERE usage_authority_sha256=?",
                (token,),
            ).fetchone()
        )
        forged = dict(valid)
        forged.update(
            {
                "usage_authority_sha256": sha("forged-usage-authority"),
                "attempt_id": second["attempt"]["attempt_id"],
                "request_cas_object_id": second["attempt"][
                    "request_cas_object_id"
                ],
                "output_cas_object_id": second_output_id,
            }
        )
        names = ", ".join(f'"{column}"' for column in columns)
        placeholders = ", ".join("?" for _ in columns)
        with self.assertRaises(sqlite3.DatabaseError):
            self.runtime.conn.execute(
                f"INSERT INTO {AUTHORITY_TABLE} ({names}) "
                f"VALUES ({placeholders})",
                tuple(forged[column] for column in columns),
            )
        self.runtime.conn.rollback()
        with self.assertRaises(sqlite3.DatabaseError):
            self.runtime.conn.execute(
                f"UPDATE {AUTHORITY_TABLE} SET terminal_at=? "
                "WHERE usage_authority_sha256=?",
                (second["terminal_at"], token),
            )
        self.runtime.conn.rollback()
        with self.assertRaises(sqlite3.DatabaseError):
            self.runtime.conn.execute(
                f"DELETE FROM {AUTHORITY_TABLE} "
                "WHERE usage_authority_sha256=?",
                (token,),
            )
        self.runtime.conn.rollback()
        self.assertEqual(
            self.runtime.conn.execute(
                f"SELECT count(*) FROM {AUTHORITY_TABLE} "
                "WHERE usage_authority_sha256=?",
                (token,),
            ).fetchone()[0],
            1,
        )

    def test_reopen_replays_same_token_and_settles_exactly_once(self):
        started = self._start()
        output = self.runtime._output()
        output_id = self._publish_output(started, output)
        token = self._issue(started, output_cas_object_id=output_id)
        self.runtime.conn.close()
        reopened = sqlite3.connect(self.runtime.db_path)
        reopened.row_factory = sqlite3.Row
        history_audit_store.init_schema(reopened)
        self.runtime.conn = reopened
        replayed = self._issue(started, output_cas_object_id=output_id)
        self.assertEqual(replayed, token)
        for _ in range(2):
            result = self._complete(started, output, token)
            self.assertEqual(result["output_cas_object_id"], output_id)
        self.assertEqual(
            tuple(
                self.runtime.conn.execute(
                    f"""
                    SELECT
                      (SELECT count(*) FROM {AUTHORITY_TABLE}),
                      (SELECT count(*) FROM audit_attempt_completions_v2),
                      (SELECT count(*) FROM audit_runtime_budget_settlements_v2),
                      (SELECT count(*) FROM audit_attempt_cost_settlements_v2)
                    """
                ).fetchone()
            ),
            (1, 1, 1, 1),
        )


if __name__ == "__main__":
    unittest.main()
