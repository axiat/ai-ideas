#!/usr/bin/env python3
import pathlib
import sqlite3
import sys
import tempfile
import threading
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_audit_store
from lib import history_metadata
from lib import history_store


HEADER = b"date\tsource\ttheme\tidea\tverdict\treason\toverlap\tcategory\n"
ROW = (
    b"2026-08-03\thunt\tMetadata\tlease regression\taccept-w-rev\t"
    b"reason\tlow\tdesign-fixable\n"
)


def profile():
    return {
        "profile_id": "metadata-correctness-v1",
        "profile_key": "metadata-correctness",
        "profile_version": "1",
        "schema_version": "history-metadata-profile-v1",
        "producer": {
            "kind": "rule",
            "id": "correctness-fixture",
            "version": "1",
        },
        "prompt_sha256": "1" * 64,
        "synopsis_max_chars": 512,
        "supersedes_profile_id": None,
    }


def annotation(value):
    return {"family": "free_tag", "value": value, "confidence": 1.0}


class HistoryMetadataCorrectnessRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        (self.root / "ledger.instance-id").write_text(
            "metadata-correctness\n", encoding="utf-8"
        )
        self.ledger = self.root / "ledger.tsv"
        self.ledger.write_bytes(HEADER + ROW)
        self.database = self.root / "history.sqlite3"
        self.conn = history_store.connect(self.database)
        history_store.init_schema(self.conn)
        history_store.import_tsv_epoch(self.conn, self.ledger)
        history_audit_store.init_schema(self.conn)
        candidate = self.conn.execute(
            "SELECT candidate_id, raw_sha256 FROM candidates"
        ).fetchone()
        history_metadata.register_profile(self.conn, profile())
        self.work = history_metadata.enqueue_candidate(
            self.conn,
            candidate["candidate_id"],
            candidate["raw_sha256"],
            "metadata-correctness-v1",
        )

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _claim(self, *, lease_until, now):
        return history_metadata.claim_candidate(
            self.conn,
            self.work["outbox_id"],
            "correctness-worker",
            lease_until,
            now=now,
        )

    def _worker_connection(self):
        conn = sqlite3.connect(
            str(self.database), isolation_level=None, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA recursive_triggers = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        history_store.init_schema(conn)
        history_audit_store.init_schema(conn)
        return conn

    def test_lock_wait_is_included_in_publication_lease_check(self):
        claim = self._claim(
            lease_until="2026-08-03T00:00:01Z",
            now="2026-08-03T00:00:00Z",
        )
        worker = self._worker_connection()
        begin_attempted = threading.Event()
        clock_called = threading.Event()
        lock_released = threading.Event()
        outcome = []

        def trace(statement):
            if statement.strip().upper() == "BEGIN IMMEDIATE":
                begin_attempted.set()

        def clock():
            clock_called.set()
            if lock_released.is_set():
                return "2026-08-03T00:00:02Z"
            return "2026-08-03T00:00:00.500000Z"

        def publish():
            try:
                history_metadata.publish_annotations(
                    worker, claim, [annotation("too-late")], clock=clock
                )
            except BaseException as exc:
                outcome.append(exc)

        worker.set_trace_callback(trace)
        self.conn.execute("BEGIN IMMEDIATE")
        thread = threading.Thread(target=publish)
        try:
            thread.start()
            self.assertTrue(begin_attempted.wait(2), "publisher did not request lock")
            self.assertFalse(
                clock_called.wait(0.1),
                "publication clock was read before acquiring the writer lock",
            )
            lock_released.set()
            self.conn.execute("COMMIT")
            thread.join(5)
        finally:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            thread.join(5)
            worker.close()

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], history_audit_store.StaleFence)
        stored = self.conn.execute(
            "SELECT state, fence, claim_token FROM audit_metadata_outbox_v2 "
            "WHERE outbox_id=?",
            (claim["outbox_id"],),
        ).fetchone()
        self.assertEqual(tuple(stored), ("claimed", claim["fence"], claim["claim_token"]))
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_annotation_versions_v2"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_metadata_settlements_v2"
            ).fetchone()[0],
            0,
        )

    def test_exact_publication_replays_without_duplicate_writes(self):
        claim = self._claim(
            lease_until="2026-08-03T00:01:00Z",
            now="2026-08-03T00:00:00Z",
        )
        annotations = [annotation("stable")]
        first = history_metadata.publish_annotations(
            self.conn,
            claim,
            annotations,
            clock=lambda: "2026-08-03T00:00:01Z",
        )

        def clock_must_not_run():
            raise AssertionError("exact replay consulted the lease clock")

        replay = history_metadata.publish_annotations(
            self.conn, claim, annotations, clock=clock_must_not_run
        )
        self.assertEqual(replay, first)
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_annotation_versions_v2"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_metadata_settlements_v2"
            ).fetchone()[0],
            1,
        )
        with self.assertRaises(history_audit_store.StaleFence):
            history_metadata.publish_annotations(
                self.conn, claim, [annotation("changed")], clock=clock_must_not_run
            )


if __name__ == "__main__":
    unittest.main()
