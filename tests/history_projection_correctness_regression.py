#!/usr/bin/env python3
import pathlib
import tempfile
import threading
import unittest
from unittest import mock

import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_projection as projection
from lib import history_store


HEADER = b"date\tsource\ttheme\tidea\tverdict\treason\toverlap\tcategory\n"


def row(story, theme="Snapshot Theme"):
    return (
        "2026-08-09\thunt\t"
        + theme
        + "\t"
        + story
        + "\taccept-w-rev\tmissing strong baseline\tlow\tdesign-fixable\n"
    ).encode("utf-8")


class HistoryProjectionCorrectnessRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        (self.root / "ledger.instance-id").write_text(
            "projection-correctness-test\n", encoding="utf-8"
        )
        self.db = self.root / "history.sqlite3"
        self.policy = projection.load_policy(
            ROOT / "history/retrieval-policy-v1.json"
        )

    def tearDown(self):
        self.temp.cleanup()

    def _initialize(self, rows):
        ledger = self.root / "ledger.tsv"
        ledger.write_bytes(HEADER + b"".join(rows))
        conn = history_store.connect(self.db)
        history_store.init_schema(conn)
        if rows:
            history_store.import_tsv_epoch(conn, ledger)
        return conn

    def _append_and_rebuild(self, story, theme="Snapshot Theme"):
        conn = history_store.connect(self.db)
        try:
            history_store.append_rows(
                conn, [row(story, theme)], {"run_id": story}
            )
            candidate = conn.execute(
                "SELECT candidate_id, source_sequence FROM candidates "
                "ORDER BY source_sequence DESC LIMIT 1"
            ).fetchone()
            projection.rebuild(conn, self.policy)
            return candidate["candidate_id"], candidate["source_sequence"]
        finally:
            conn.close()

    def _rebuild_while_tokens_start(self, call, story):
        reader_thread = threading.current_thread()
        start = threading.Event()
        done = threading.Event()
        errors = []
        appended = []

        def writer():
            start.wait(5)
            try:
                appended.append(self._append_and_rebuild(story))
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()

        worker = threading.Thread(target=writer)
        worker.start()
        original_tokens = projection._tokens
        triggered = False

        def tokens(value):
            nonlocal triggered
            if threading.current_thread() is reader_thread and not triggered:
                triggered = True
                start.set()
                if not done.wait(5):
                    raise AssertionError("concurrent rebuild did not finish")
            return original_tokens(value)

        try:
            with mock.patch.object(projection, "_tokens", side_effect=tokens):
                result = call()
        finally:
            start.set()
            worker.join(5)
        if worker.is_alive():
            self.fail("concurrent rebuild thread did not stop")
        if errors:
            raise errors[0]
        self.assertTrue(triggered)
        return result, appended[0][0]

    def test_invalid_policy_reads_do_not_initialize_or_mutate(self):
        conn = self._initialize([])
        try:
            invalid = dict(self.policy)
            invalid["rrf_k"] += 1
            before_tables = conn.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall()
            before_changes = conn.total_changes
            for read in (
                lambda: projection.validate_published_generation(conn, invalid),
                lambda: projection.search(conn, "query", invalid),
                lambda: projection.build_generation_brief(conn, invalid),
            ):
                with self.assertRaises(ValueError):
                    read()
            self.assertFalse(
                projection.validate_published_generation(conn, self.policy)[
                    "valid"
                ]
            )
            self.assertEqual(
                projection.search(conn, "query", self.policy)["candidate_ids"],
                [],
            )
            self.assertEqual(
                projection.l1_rankings_as_of(conn, "query", 10, 0),
                {"exact": [], "fts": [], "hash_dense": []},
            )
            with self.assertRaises(projection.ProjectionError):
                projection.build_generation_brief(conn, self.policy)
            after_tables = conn.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall()
            self.assertEqual(
                [tuple(item) for item in before_tables],
                [tuple(item) for item in after_tables],
            )
            self.assertEqual(conn.total_changes, before_changes)
            self.assertFalse(projection._projection_initialized(conn))
        finally:
            conn.close()

    def test_read_apis_are_observational(self):
        conn = self._initialize([row("observational read")])
        try:
            projection.rebuild(conn, self.policy)
            before_changes = conn.total_changes
            before_generation = [
                tuple(item)
                for item in conn.execute(
                    "SELECT * FROM search_index_generations ORDER BY generation"
                )
            ]
            projection.validate_published_generation(conn, self.policy)
            projection.search(conn, "observational read", self.policy)
            projection.l1_rankings_as_of(
                conn, "observational read", 10, 1000
            )
            projection.build_generation_brief(conn, self.policy)
            projection.searchable_candidate_ids(conn)
            projection.exact_lookup(conn, "observational read", 10)
            projection.current_index_generation(conn)
            after_generation = [
                tuple(item)
                for item in conn.execute(
                    "SELECT * FROM search_index_generations ORDER BY generation"
                )
            ]
            self.assertEqual(conn.total_changes, before_changes)
            self.assertEqual(after_generation, before_generation)
        finally:
            conn.close()

    def test_empty_history_publishes_generation_zero_concurrently(self):
        conn = self._initialize([])
        conn.execute(
            "UPDATE schema_meta SET value = '7' "
            "WHERE key = 'history_index_generation'"
        )
        conn.close()
        barrier = threading.Barrier(3)
        errors = []

        def recover_empty():
            connection = history_store.connect(self.db)
            try:
                barrier.wait(5)
                projection.recover(connection, self.policy)
            except Exception as exc:
                errors.append(exc)
            finally:
                connection.close()

        workers = [threading.Thread(target=recover_empty) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait(5)
        for worker in workers:
            worker.join(5)
        self.assertFalse(errors)

        conn = history_store.connect(self.db)
        try:
            generations = conn.execute(
                "SELECT generation FROM search_index_generations"
            ).fetchall()
            self.assertEqual([item[0] for item in generations], [0])
            self.assertEqual(projection.current_index_generation(conn), 0)
            self.assertTrue(
                projection.validate_published_generation(conn, self.policy)[
                    "valid"
                ]
            )
            brief = projection.build_generation_brief(conn, self.policy)
            self.assertEqual(brief["index_generation"], 0)
            self.assertEqual(brief["source_watermark"], 0)
            self.assertEqual(brief["theme_counts"], {})
            self.assertIsNone(brief["parent"])
            conn.execute(
                "UPDATE search_index_generations SET manifest_sha256 = 'broken' "
                "WHERE generation = 0"
            )
            recovered = projection.recover(conn, self.policy)
            self.assertTrue(recovered["recovered"])
            self.assertEqual(recovered["index_generation"], 0)
            self.assertTrue(
                projection.validate_published_generation(conn, self.policy)[
                    "valid"
                ]
            )
        finally:
            conn.close()

    def test_recovery_requeue_survives_rebuild_failure(self):
        conn = self._initialize([row("durable recovery intent")])
        try:
            projection.rebuild(conn, self.policy)
            conn.execute("DELETE FROM search_vectors")
            self.assertFalse(
                projection.validate_published_generation(conn, self.policy)[
                    "valid"
                ]
            )
            with mock.patch.object(
                projection,
                "_write_candidate",
                side_effect=RuntimeError("injected rebuild failure"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "injected rebuild failure"
                ):
                    projection.recover(conn, self.policy)
            pending = conn.execute(
                "SELECT content_version, state FROM search_projection_outbox "
                "WHERE state = 'pending'"
            ).fetchall()
            self.assertEqual(
                [tuple(item) for item in pending],
                [("recovery-v2", "pending")],
            )
            projection.recover(conn, self.policy)
            self.assertTrue(
                projection.validate_published_generation(conn, self.policy)[
                    "valid"
                ]
            )
        finally:
            conn.close()

    def test_public_mutators_reject_nested_transactions(self):
        conn = self._initialize([row("nested transaction")])
        try:
            conn.execute("BEGIN")
            with self.assertRaises(projection.ProjectionError):
                projection.rebuild(conn, self.policy)
            with self.assertRaises(projection.ProjectionError):
                projection.recover(conn, self.policy)
            self.assertTrue(conn.in_transaction)
            conn.execute("ROLLBACK")
        finally:
            conn.close()

    def test_search_uses_one_projection_snapshot(self):
        conn = self._initialize([row("snapshot marker")])
        try:
            projection.rebuild(conn, self.policy)
            result, appended_id = self._rebuild_while_tokens_start(
                lambda: projection.search(conn, "snapshot marker", self.policy),
                "snapshot marker concurrent search",
            )
            visible = set(result["candidate_ids"])
            for name, values in result["channels"].items():
                if name in ("fts_by_facet", "dense_by_facet"):
                    for facet_values in values.values():
                        visible.update(facet_values)
                else:
                    visible.update(values)
            self.assertNotIn(appended_id, visible)
        finally:
            conn.close()

    def test_l1_rankings_use_one_projection_snapshot(self):
        conn = self._initialize([row("snapshot marker")])
        try:
            projection.rebuild(conn, self.policy)
            result, appended_id = self._rebuild_while_tokens_start(
                lambda: projection.l1_rankings_as_of(
                    conn, "snapshot marker", 50, 1000
                ),
                "snapshot marker concurrent l1",
            )
            visible = {
                item["candidate_id"]
                for ranking in result.values()
                for item in ranking
            }
            self.assertNotIn(appended_id, visible)
        finally:
            conn.close()

    def test_generation_brief_uses_one_canonical_snapshot(self):
        conn = self._initialize([row("initial candidate", "Initial Theme")])
        try:
            projection.rebuild(conn, self.policy)
            initial_generation = projection.current_index_generation(conn)
            reader_thread = threading.current_thread()
            start = threading.Event()
            done = threading.Event()
            errors = []

            def writer():
                start.wait(5)
                connection = history_store.connect(self.db)
                try:
                    history_store.append_rows(
                        connection,
                        [row("new eligible parent", "Concurrent Theme")],
                        {"run_id": "brief-concurrent"},
                    )
                    candidate = connection.execute(
                        "SELECT candidate_id, source_sequence FROM candidates "
                        "ORDER BY source_sequence DESC LIMIT 1"
                    ).fetchone()
                    connection.execute(
                        """INSERT INTO near_sa_observations(
                           observation_id, candidate_id, source_sequence,
                           sa_votes, vote_vector, overlap, category, reason,
                           observed_at
                        ) VALUES(?, ?, ?, 1, '[1]', 'low', 'design-fixable',
                                 'missing strong baseline', datetime('now'))""",
                        (
                            "brief-concurrent-observation",
                            candidate["candidate_id"],
                            candidate["source_sequence"],
                        ),
                    )
                    projection.rebuild(connection, self.policy)
                except Exception as exc:
                    errors.append(exc)
                finally:
                    connection.close()
                    done.set()

            worker = threading.Thread(target=writer)
            worker.start()
            original_validate = projection._validate_published_generation_snapshot
            triggered = False

            def validate(connection, policy):
                nonlocal triggered
                result = original_validate(connection, policy)
                if threading.current_thread() is reader_thread and not triggered:
                    triggered = True
                    start.set()
                    if not done.wait(5):
                        raise AssertionError("concurrent brief rebuild did not finish")
                return result

            try:
                with mock.patch.object(
                    projection,
                    "_validate_published_generation_snapshot",
                    side_effect=validate,
                ):
                    brief = projection.build_generation_brief(
                        conn, self.policy
                    )
            finally:
                start.set()
                worker.join(5)
            self.assertFalse(errors)
            self.assertTrue(triggered)
            self.assertEqual(brief["index_generation"], initial_generation)
            self.assertEqual(brief["source_watermark"], 1)
            self.assertEqual(brief["theme_counts"], {"Initial Theme": 1})
            self.assertIsNone(brief["parent"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
