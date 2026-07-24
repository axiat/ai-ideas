#!/usr/bin/env python3
import hashlib
import json
import os
import pathlib
import sqlite3
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_store


HEADER = b"date\tsource\ttheme\tidea\tverdict\treason\toverlap\tcategory\n"


def row(story, verdict="accept-w-rev", overlap="low", category="design-fixable"):
    return (
        "2026-07-23\thunt\tEvaluation and Diagnostics\t"
        + story
        + "\t"
        + verdict
        + "\treason\t"
        + overlap
        + "\t"
        + category
    ).encode("utf-8")


class HistoryStoreSmoke(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.state_root = self.root / ".ai-ideas"
        (self.root / "ledger.instance-id").write_text(
            "test-ledger-instance\n", encoding="utf-8"
        )
        legacy = (
            b"2026-07-20\thunt\tSafety and Robustness\tlegacy proposition"
            b"\taccept-w-rev\tlegacy reason\tunknown\n"
        )
        self.ledger = self.root / "ledger.tsv"
        self.ledger.write_bytes(
            HEADER
            + legacy
            + row("near sa proposition")
            + b"\n"
            + row("terminal proposition", verdict="reject", overlap="high",
                  category="novelty-capped")
            + b"\n"
        )
        self.db = self.root / "history.sqlite3"
        self.conn = history_store.connect(self.db)
        history_store.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _import(self):
        return history_store.import_tsv_epoch(self.conn, self.ledger)

    def _candidate_ids(self):
        return [
            item[0]
            for item in self.conn.execute(
                "SELECT candidate_id FROM candidates ORDER BY source_sequence"
            )
        ]

    def _candidate_for_story(self, story):
        return self.conn.execute(
            "SELECT * FROM candidates WHERE story = ? ORDER BY source_sequence",
            (story,),
        ).fetchall()

    def _canonical_counts(self):
        return tuple(
            self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("lineages", "candidates", "story_aliases", "lineage_edges")
        )

    def _near_sa_snapshot(
        self,
        name="bootstrap-near-sa.tsv",
        *,
        theme="Evaluation and Diagnostics",
        overlap="low",
        category="design-fixable",
    ):
        snapshot = self.root / name
        snapshot.write_text(
            "2026-07-23\trun/I1\tnear sa proposition\t"
            f"{theme}\t{overlap}\t2,1,1\t{category}\n",
            encoding="utf-8",
        )
        return snapshot

    def _bootstrap_counts(self, conn=None):
        active = self.conn if conn is None else conn
        return {
            table: active.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "bootstrap_provenance",
                "import_epochs",
                "lineages",
                "candidates",
                "story_aliases",
                "near_sa_observations",
                "search_projection_outbox",
                "ledger_projection_outbox",
            )
        }

    def _synthetic_bootstrap_marker(self):
        body = {
            "import_epoch_id": "0" * 64,
            "ledger_row_count": 0,
            "ledger_sha256": "1" * 64,
            "near_sa_sha256": None,
            "schema_version": 1,
        }
        marker = dict(body)
        marker["marker_sha256"] = hashlib.sha256(
            b"bootstrap-complete-v1\0"
            + (
                json.dumps(
                    body,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        ).hexdigest()
        return marker

    def _marker_json(self, marker):
        body = dict(marker)
        body.pop("marker_sha256", None)
        marker = dict(marker)
        marker["marker_sha256"] = hashlib.sha256(
            b"bootstrap-complete-v1\0"
            + (
                json.dumps(
                    body,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                + "\n"
            ).encode("utf-8")
        ).hexdigest()
        return (
            json.dumps(
                marker,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        )

    def _drop_bootstrap_marker_guards(self, conn):
        conn.execute(
            "DROP TRIGGER IF EXISTS bootstrap_complete_v1_immutable_insert"
        )
        conn.execute(
            "DROP TRIGGER IF EXISTS bootstrap_complete_v1_immutable_update"
        )
        conn.execute(
            "DROP TRIGGER IF EXISTS "
            "bootstrap_complete_v1_immutable_update_key"
        )
        conn.execute(
            "DROP TRIGGER IF EXISTS bootstrap_complete_v1_immutable_delete"
        )

    def test_import_export_preserves_legacy_and_current_rows(self):
        receipt = self._import()
        exported = self.root / "export.tsv"
        history_store.export_tsv(self.conn, exported)
        self.assertEqual(exported.read_bytes(), self.ledger.read_bytes())
        self.assertEqual(receipt["data_rows"], 3)

    def test_schema_excludes_deferred_awr_bridge(self):
        history_store.init_schema(self.conn)
        names = {
            item[0]
            for item in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        for name in (
            "reentry_grants",
            "reentry_requests",
            "round_slots",
            "materialization_outbox",
        ):
            self.assertNotIn(name, names)

    def test_bootstrap_import_epoch_recovers_schema_without_marker(self):
        snapshot = self._near_sa_snapshot()
        statements = []
        self.conn.set_trace_callback(statements.append)
        receipt = history_store.bootstrap_import_epoch(
            self.conn,
            self.ledger,
            snapshot,
            state_root=self.state_root,
        )
        self.conn.set_trace_callback(None)

        marker_row = self.conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'bootstrap_complete_v1'"
        ).fetchone()
        self.assertIsNotNone(marker_row)
        marker = json.loads(marker_row[0])
        self.assertEqual(
            set(marker),
            {
                "import_epoch_id",
                "ledger_row_count",
                "ledger_sha256",
                "marker_sha256",
                "near_sa_sha256",
                "schema_version",
            },
        )
        self.assertEqual(marker["schema_version"], 1)
        self.assertEqual(marker["import_epoch_id"], receipt["epoch_id"])
        self.assertEqual(marker["ledger_row_count"], 3)
        self.assertEqual(
            marker["ledger_sha256"],
            hashlib.sha256(self.ledger.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            marker["near_sa_sha256"],
            hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        )
        marker_body = dict(marker)
        marker_sha = marker_body.pop("marker_sha256")
        marker_material = (
            json.dumps(
                marker_body,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
        self.assertEqual(
            marker_sha,
            hashlib.sha256(
                b"bootstrap-complete-v1\0" + marker_material
            ).hexdigest(),
        )
        self.assertFalse(receipt["idempotent"])
        self.assertEqual(receipt["near_sa_observations"], 1)
        self.assertEqual(
            sum(
                statement.strip().upper() == "BEGIN IMMEDIATE"
                for statement in statements
            ),
            1,
        )
        self.assertEqual(
            sum(statement.strip().upper() == "COMMIT" for statement in statements),
            1,
        )
        self.assertEqual(
            self._bootstrap_counts(),
            {
                "bootstrap_provenance": 1,
                "import_epochs": 1,
                "lineages": 3,
                "candidates": 3,
                "story_aliases": 3,
                "near_sa_observations": 1,
                "search_projection_outbox": 3,
                "ledger_projection_outbox": 1,
            },
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE schema_meta SET value = '{}' "
                "WHERE key = 'bootstrap_complete_v1'"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "DELETE FROM schema_meta WHERE key = 'bootstrap_complete_v1'"
            )

    def test_bootstrap_marker_rejects_replace_and_rename_into_key(self):
        completed = history_store.bootstrap_import_epoch(
            self.conn,
            self.ledger,
            state_root=self.state_root,
        )
        marker_before = self.conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'bootstrap_complete_v1'"
        ).fetchone()[0]
        forged = dict(completed["bootstrap_marker"])
        forged["ledger_sha256"] = "f" * 64
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES(?, ?)",
                ("bootstrap_complete_v1", self._marker_json(forged)),
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT value FROM schema_meta "
                "WHERE key = 'bootstrap_complete_v1'"
            ).fetchone()[0],
            marker_before,
        )

        conn = history_store.connect(self.root / "marker-rename.sqlite3")
        history_store.init_schema(conn)
        try:
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('other', 'value')"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE schema_meta SET key = 'bootstrap_complete_v1' "
                    "WHERE key = 'other'"
                )
        finally:
            conn.close()

    def test_bootstrap_import_epoch_initializes_an_empty_database(self):
        conn = history_store.connect(self.root / "empty-history.sqlite3")
        try:
            receipt = history_store.bootstrap_import_epoch(
                conn,
                self.ledger,
                state_root=self.state_root,
            )
            self.assertFalse(receipt["idempotent"])
            self.assertEqual(receipt["near_sa_observations"], 0)
            self.assertEqual(
                self._bootstrap_counts(conn)["candidates"],
                3,
            )
            marker = json.loads(
                conn.execute(
                    "SELECT value FROM schema_meta "
                    "WHERE key = 'bootstrap_complete_v1'"
                ).fetchone()[0]
            )
            self.assertIsNone(marker["near_sa_sha256"])
        finally:
            conn.close()

    def test_validated_bootstrap_marker_reads_completed_marker_without_writes(self):
        completed = history_store.bootstrap_import_epoch(
            self.conn,
            self.ledger,
            state_root=self.state_root,
        )
        statements = []
        self.conn.set_trace_callback(statements.append)

        marker = history_store.validated_bootstrap_marker(self.conn)

        self.conn.set_trace_callback(None)
        self.assertEqual(marker, completed["bootstrap_marker"])
        self.assertTrue(statements)
        self.assertEqual(statements[0].strip().upper(), "BEGIN")
        self.assertEqual(statements[-1].strip().upper(), "COMMIT")
        self.assertTrue(
            all(
                statement.lstrip().upper().startswith(
                    ("BEGIN", "COMMIT", "SELECT", "PRAGMA")
                )
                for statement in statements
            )
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM bootstrap_provenance"
            ).fetchone()[0],
            1,
        )

    def test_validated_bootstrap_marker_rejects_missing_marker(self):
        with self.assertRaises(history_store.ImportConflict):
            history_store.validated_bootstrap_marker(self.conn)

    def test_validated_bootstrap_marker_rejects_extra_field(self):
        marker = self._synthetic_bootstrap_marker()
        marker["extra"] = "not closed"
        self.conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES(?, ?)",
            (
                "bootstrap_complete_v1",
                json.dumps(
                    marker,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
            ),
        )
        with self.assertRaises(history_store.ImportConflict):
            history_store.validated_bootstrap_marker(self.conn)
        report = history_store.validate_store(self.conn)
        self.assertFalse(report["ok"])
        self.assertIn("bootstrap_marker_invalid", report["issues"])

    def test_validated_bootstrap_marker_rejects_bad_hash_and_noncanonical_json(self):
        for case in ("bad_hash", "noncanonical"):
            with self.subTest(case=case):
                conn = history_store.connect(
                    self.root / f"invalid-marker-{case}.sqlite3"
                )
                history_store.init_schema(conn)
                try:
                    marker = self._synthetic_bootstrap_marker()
                    if case == "bad_hash":
                        marker["marker_sha256"] = "f" * 64
                        raw = (
                            json.dumps(
                                marker,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                            + "\n"
                        )
                    else:
                        raw = json.dumps(marker, indent=2) + "\n"
                    conn.execute(
                        "INSERT INTO schema_meta(key, value) VALUES(?, ?)",
                        ("bootstrap_complete_v1", raw),
                    )
                    with self.assertRaises(history_store.ImportConflict):
                        history_store.validated_bootstrap_marker(conn)
                finally:
                    conn.close()

    def test_validated_bootstrap_marker_binds_ledger_and_near_sa_digests(self):
        for field in ("ledger_sha256", "near_sa_sha256"):
            with self.subTest(field=field):
                conn = history_store.connect(
                    self.root / f"forged-{field}.sqlite3"
                )
                history_store.init_schema(conn)
                snapshot = self._near_sa_snapshot(
                    f"forged-{field}-near-sa.tsv"
                )
                try:
                    completed = history_store.bootstrap_import_epoch(
                        conn,
                        self.ledger,
                        snapshot,
                        state_root=self.state_root,
                    )
                    forged = dict(completed["bootstrap_marker"])
                    forged[field] = "f" * 64
                    self._drop_bootstrap_marker_guards(conn)
                    conn.execute(
                        "UPDATE schema_meta SET value = ? "
                        "WHERE key = 'bootstrap_complete_v1'",
                        (self._marker_json(forged),),
                    )
                    with self.assertRaises(history_store.ImportConflict):
                        history_store.validated_bootstrap_marker(conn)
                finally:
                    conn.close()

    def test_validated_bootstrap_marker_binds_full_import_epoch(self):
        history_store.bootstrap_import_epoch(
            self.conn,
            self.ledger,
            state_root=self.state_root,
        )
        self.conn.execute(
            "UPDATE import_epochs SET result_sha256 = ?",
            ("f" * 64,),
        )
        with self.assertRaises(history_store.ImportConflict):
            history_store.validated_bootstrap_marker(self.conn)

    def test_validated_bootstrap_marker_binds_near_sa_membership_content(self):
        snapshot = self._near_sa_snapshot()
        history_store.bootstrap_import_epoch(
            self.conn,
            self.ledger,
            snapshot,
            state_root=self.state_root,
        )
        self.conn.execute(
            "DROP TRIGGER IF EXISTS near_sa_observations_immutable_update"
        )
        self.conn.execute(
            "UPDATE near_sa_observations SET reason = 'tampered'"
        )
        with self.assertRaises(history_store.ImportConflict):
            history_store.validated_bootstrap_marker(self.conn)

    def test_bootstrap_exact_replay_is_idempotent_without_duplicate_work(self):
        snapshot = self._near_sa_snapshot()
        first = history_store.bootstrap_import_epoch(
            self.conn,
            self.ledger,
            snapshot,
            state_root=self.state_root,
        )
        before = self._bootstrap_counts()
        marker_before = self.conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'bootstrap_complete_v1'"
        ).fetchone()[0]

        replay = history_store.bootstrap_import_epoch(
            self.conn,
            self.ledger,
            snapshot,
            state_root=self.state_root,
        )

        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["epoch_id"], first["epoch_id"])
        self.assertEqual(replay["near_sa_observations"], 1)
        self.assertEqual(self._bootstrap_counts(), before)
        self.assertEqual(
            self.conn.execute(
                "SELECT value FROM schema_meta "
                "WHERE key = 'bootstrap_complete_v1'"
            ).fetchone()[0],
            marker_before,
        )

    def test_bootstrap_rejects_changed_ledger_or_near_sa_after_completion(self):
        snapshot = self._near_sa_snapshot()
        history_store.bootstrap_import_epoch(
            self.conn,
            self.ledger,
            snapshot,
            state_root=self.state_root,
        )
        before = self._bootstrap_counts()

        snapshot.write_text(
            snapshot.read_text(encoding="utf-8").replace("run/I1", "run/I2"),
            encoding="utf-8",
        )
        with self.assertRaises(history_store.ImportConflict):
            history_store.bootstrap_import_epoch(
                self.conn,
                self.ledger,
                snapshot,
                state_root=self.state_root,
            )
        self.assertEqual(self._bootstrap_counts(), before)

        snapshot = self._near_sa_snapshot()
        changed_ledger = self.root / "changed-ledger.tsv"
        changed_ledger.write_bytes(
            self.ledger.read_bytes().replace(
                b"terminal proposition", b"changed proposition"
            )
        )
        with self.assertRaises(history_store.ImportConflict):
            history_store.bootstrap_import_epoch(
                self.conn,
                changed_ledger,
                snapshot,
                state_root=self.state_root,
            )
        self.assertEqual(self._bootstrap_counts(), before)

    def test_bootstrap_completes_matching_preimport_without_duplicates(self):
        imported = history_store.import_tsv_epoch(self.conn, self.ledger)
        before = self._bootstrap_counts()
        self.assertIsNone(
            self.conn.execute(
                "SELECT value FROM schema_meta "
                "WHERE key = 'bootstrap_complete_v1'"
            ).fetchone()
        )
        snapshot = self._near_sa_snapshot()

        completed = history_store.bootstrap_import_epoch(
            self.conn,
            self.ledger,
            snapshot,
            state_root=self.state_root,
        )

        self.assertFalse(completed["idempotent"])
        self.assertTrue(completed["import_idempotent"])
        self.assertEqual(completed["epoch_id"], imported["epoch_id"])
        after = self._bootstrap_counts()
        self.assertEqual(after["candidates"], before["candidates"])
        self.assertEqual(
            after["search_projection_outbox"],
            before["search_projection_outbox"],
        )
        self.assertEqual(
            after["ledger_projection_outbox"],
            before["ledger_projection_outbox"],
        )
        self.assertEqual(after["near_sa_observations"], 1)

    def test_bootstrap_near_sa_must_match_canonical_row_and_eligibility(self):
        cases = (
            {
                "name": "theme",
                "source": "hunt",
                "verdict": "accept-w-rev",
                "theme": "Wrong Theme",
                "overlap": "low",
                "category": "design-fixable",
            },
            {
                "name": "overlap",
                "source": "hunt",
                "verdict": "accept-w-rev",
                "theme": "Evaluation and Diagnostics",
                "overlap": "high",
                "category": "design-fixable",
            },
            {
                "name": "category",
                "source": "hunt",
                "verdict": "accept-w-rev",
                "theme": "Evaluation and Diagnostics",
                "overlap": "low",
                "category": "evidence-incomplete",
            },
            {
                "name": "source",
                "source": "manual",
                "verdict": "accept-w-rev",
                "theme": "Evaluation and Diagnostics",
                "overlap": "low",
                "category": "design-fixable",
            },
            {
                "name": "verdict",
                "source": "hunt",
                "verdict": "pending",
                "theme": "Evaluation and Diagnostics",
                "overlap": "low",
                "category": "design-fixable",
            },
        )
        for case in cases:
            with self.subTest(case=case["name"]):
                root = self.root / f"near-sa-{case['name']}"
                root.mkdir()
                (root / "ledger.instance-id").write_text(
                    f"near-sa-{case['name']}\n",
                    encoding="utf-8",
                )
                ledger = root / "ledger.tsv"
                ledger.write_bytes(
                    HEADER
                    + (
                        "2026-07-23\t"
                        + case["source"]
                        + "\tEvaluation and Diagnostics\tnear sa proposition\t"
                        + case["verdict"]
                        + "\treason\tlow\tdesign-fixable\n"
                    ).encode("utf-8")
                )
                snapshot = root / "near-sa.tsv"
                snapshot.write_text(
                    "2026-07-23\trun/I1\tnear sa proposition\t"
                    + case["theme"]
                    + "\t"
                    + case["overlap"]
                    + "\t2,1,1\t"
                    + case["category"]
                    + "\n",
                    encoding="utf-8",
                )
                conn = history_store.connect(root / "history.sqlite3")
                history_store.init_schema(conn)
                try:
                    with self.assertRaises(history_store.ImportConflict):
                        history_store.bootstrap_import_epoch(
                            conn,
                            ledger,
                            snapshot,
                            state_root=root / ".ai-ideas",
                        )
                    self.assertEqual(
                        conn.execute(
                            "SELECT count(*) FROM candidates"
                        ).fetchone()[0],
                        0,
                    )
                finally:
                    conn.close()

    def test_near_sa_observations_are_immutable(self):
        for operation in ("update", "delete", "replace"):
            with self.subTest(operation=operation):
                conn = history_store.connect(
                    self.root / f"near-sa-{operation}.sqlite3"
                )
                history_store.init_schema(conn)
                snapshot = self._near_sa_snapshot(
                    f"near-sa-{operation}-snapshot.tsv"
                )
                try:
                    history_store.bootstrap_import_epoch(
                        conn,
                        self.ledger,
                        snapshot,
                        state_root=self.state_root,
                    )
                    existing = conn.execute(
                        "SELECT * FROM near_sa_observations"
                    ).fetchone()
                    with self.assertRaises(sqlite3.IntegrityError):
                        if operation == "update":
                            conn.execute(
                                "UPDATE near_sa_observations "
                                "SET reason = 'changed'"
                            )
                        elif operation == "delete":
                            conn.execute(
                                "DELETE FROM near_sa_observations"
                            )
                        else:
                            values = list(existing)
                            values[7] = "changed"
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO near_sa_observations(
                                  observation_id, candidate_id,
                                  source_sequence, sa_votes, vote_vector,
                                  overlap, category, reason, observed_at
                                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                values,
                            )
                finally:
                    conn.close()

    def test_bootstrap_rejects_missing_symlink_and_special_near_sa_inputs(self):
        snapshot = self._near_sa_snapshot("near-sa-regular.tsv")
        symlink = self.root / "near-sa-symlink.tsv"
        symlink.symlink_to(snapshot)
        cases = (
            ("missing", self.root / "near-sa-missing.tsv"),
            ("symlink", symlink),
            ("special", pathlib.Path(os.devnull)),
        )
        for name, path in cases:
            with self.subTest(case=name):
                conn = history_store.connect(
                    self.root / f"near-sa-input-{name}.sqlite3"
                )
                history_store.init_schema(conn)
                try:
                    with self.assertRaises(history_store.ImportConflict):
                        history_store.bootstrap_import_epoch(
                            conn,
                            self.ledger,
                            path,
                            state_root=self.state_root,
                        )
                    self.assertEqual(
                        conn.execute(
                            "SELECT count(*) FROM candidates"
                        ).fetchone()[0],
                        0,
                    )
                finally:
                    conn.close()

    def test_bootstrap_rejects_symlink_and_special_ledger_inputs(self):
        symlink = self.root / "ledger-symlink.tsv"
        symlink.symlink_to(self.ledger)
        for name, path in (
            ("symlink", symlink),
            ("special", pathlib.Path(os.devnull)),
        ):
            with self.subTest(case=name):
                conn = history_store.connect(
                    self.root / f"ledger-input-{name}.sqlite3"
                )
                history_store.init_schema(conn)
                try:
                    with self.assertRaises(history_store.ImportConflict):
                        history_store.bootstrap_import_epoch(
                            conn,
                            path,
                            state_root=self.state_root,
                        )
                    self.assertEqual(
                        conn.execute(
                            "SELECT count(*) FROM candidates"
                        ).fetchone()[0],
                        0,
                    )
                finally:
                    conn.close()

    def test_bootstrap_validation_and_faults_leave_no_business_state(self):
        invalid = self.root / "invalid-near-sa.tsv"
        invalid.write_text(
            "2026-07-23\trun/I1\tmissing proposition\t"
            "Evaluation and Diagnostics\tlow\t2,1,1\tdesign-fixable\n",
            encoding="utf-8",
        )
        with self.assertRaises(history_store.ImportConflict):
            history_store.bootstrap_import_epoch(
                self.conn,
                self.ledger,
                invalid,
                state_root=self.state_root,
            )
        self.assertEqual(
            self._bootstrap_counts(),
            dict.fromkeys(self._bootstrap_counts(), 0),
        )

        corrupt_marker_conn = history_store.connect(
            self.root / "corrupt-bootstrap-marker.sqlite3"
        )
        history_store.init_schema(corrupt_marker_conn)
        try:
            corrupt_marker = {
                "import_epoch_id": "0" * 64,
                "ledger_row_count": 3,
                "ledger_sha256": "0" * 64,
                "marker_sha256": "f" * 64,
                "near_sa_sha256": None,
                "schema_version": 1,
            }
            corrupt_marker_conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES(?, ?)",
                (
                    "bootstrap_complete_v1",
                    json.dumps(
                        corrupt_marker,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n",
                ),
            )
            with self.assertRaises(history_store.ImportConflict):
                history_store.bootstrap_import_epoch(
                    corrupt_marker_conn,
                    self.ledger,
                    state_root=self.state_root,
                )
            self.assertEqual(
                self._bootstrap_counts(corrupt_marker_conn),
                dict.fromkeys(self._bootstrap_counts(corrupt_marker_conn), 0),
            )
        finally:
            corrupt_marker_conn.close()

        snapshot = self._near_sa_snapshot()
        for fault_after in ("after_ledger", "after_near_sa"):
            with self.subTest(fault_after=fault_after):
                conn = history_store.connect(
                    self.root / f"bootstrap-{fault_after}.sqlite3"
                )
                history_store.init_schema(conn)
                try:
                    with self.assertRaises(history_store.InjectedCrash):
                        history_store.bootstrap_import_epoch(
                            conn,
                            self.ledger,
                            snapshot,
                            state_root=self.state_root,
                            _fault_after=fault_after,
                        )
                    counts = self._bootstrap_counts(conn)
                    self.assertEqual(counts, dict.fromkeys(counts, 0))
                    self.assertEqual(
                        conn.execute(
                            "SELECT value FROM schema_meta "
                            "WHERE key = 'projection_sequence'"
                        ).fetchone()[0],
                        "0",
                    )
                    self.assertIsNone(
                        conn.execute(
                            "SELECT value FROM schema_meta "
                            "WHERE key = 'bootstrap_complete_v1'"
                        ).fetchone()
                    )

                    recovered = history_store.bootstrap_import_epoch(
                        conn,
                        self.ledger,
                        snapshot,
                        state_root=self.state_root,
                    )
                    self.assertFalse(recovered["idempotent"])
                    self.assertEqual(
                        self._bootstrap_counts(conn)["candidates"],
                        3,
                    )
                finally:
                    conn.close()

    def test_append_keeps_existing_candidate_ids_stable(self):
        self._import()
        before = self._candidate_ids()
        history_store.append_rows(
            self.conn, [row("new proposition")], {"run_id": "r1"}
        )
        self.assertEqual(self._candidate_ids()[: len(before)], before)
        appended = self.conn.execute(
            "SELECT field_count, provenance_json FROM candidates "
            "ORDER BY source_sequence DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(appended["field_count"], 8)
        self.assertEqual(json.loads(appended["provenance_json"]), {"run_id": "r1"})

    def test_append_after_unterminated_import_preserves_sealed_row_framing(self):
        root = pathlib.Path(tempfile.mkdtemp(dir=self.root))
        (root / "ledger.instance-id").write_text(
            "unterminated-ledger-instance\n", encoding="utf-8"
        )
        legacy = (
            b"2026-07-20\thunt\tSafety and Robustness\tunterminated proposition"
            b"\taccept-w-rev\tlegacy reason\tunknown"
        )
        ledger = root / "ledger.tsv"
        ledger.write_bytes(HEADER + legacy)
        state_root = root / ".ai-ideas"
        conn = history_store.connect(root / "history.sqlite3")
        history_store.init_schema(conn)
        try:
            plan = history_store.build_import_plan({"ledger": ledger}, state_root)
            history_store.commit_import_plan(conn, plan)
            imported = conn.execute(
                """
                SELECT candidate_id, origin_stable_id, field_count, row_terminator
                FROM candidates WHERE source_sequence = 1
                """
            ).fetchone()
            self.assertEqual(imported["field_count"], 7)
            self.assertEqual(bytes(imported["row_terminator"]), b"")

            history_store.append_rows(
                conn, [row("appended proposition")], {"run_id": "r2"}
            )
            after_append = conn.execute(
                """
                SELECT candidate_id, origin_stable_id, field_count, row_terminator
                FROM candidates WHERE source_sequence = 1
                """
            ).fetchone()
            self.assertEqual(tuple(after_append), tuple(imported))

            retry = history_store.commit_import_plan(conn, plan)
            self.assertTrue(retry["idempotent"])
            self.assertTrue(history_store.validate_store(conn)["ok"])

            ledger_good = root / "tmp" / "ledger.good"
            publication = history_store.materialize_ledger_projection(
                conn,
                {"ledger.tsv": ledger, "tmp/ledger.good": ledger_good},
                state_root,
            )
            expected = HEADER + legacy + b"\n" + row("appended proposition") + b"\n"
            self.assertEqual(publication["row_count"], 2)
            self.assertEqual(ledger.read_bytes(), expected)
            self.assertEqual(ledger_good.read_bytes(), expected)
            self.assertTrue(history_store.validate_store(conn)["ok"])
        finally:
            conn.close()

    def test_eight_column_row_with_empty_category_remains_eight_column(self):
        ledger = self.root / "empty-category.tsv"
        ledger.write_bytes(
            HEADER
            + b"2026-07-23\thunt\tTheme\teight fields\taccept-w-rev"
            + b"\treason\tlow\t\n"
        )
        history_store.import_tsv_epoch(self.conn, ledger)
        field_count = self.conn.execute(
            "SELECT field_count FROM candidates"
        ).fetchone()[0]
        self.assertEqual(field_count, 8)
        self.assertEqual(history_store.render_tsv(self.conn), ledger.read_bytes())

    def test_blank_physical_row_fails_closed(self):
        ledger = self.root / "blank.tsv"
        ledger.write_bytes(
            HEADER + row("before blank") + b"\n\n" + row("after blank") + b"\n"
        )
        with self.assertRaises(history_store.ImportConflict):
            history_store.build_import_plan({"ledger": ledger}, self.state_root)

    def test_origin_row_v2_fixes_instance_ordinal_and_row_bytes(self):
        one = history_store.origin_stable_id("instance-a", 1, b"a\tb")
        expected = hashlib.sha256(
            b"tsv-row-v2\0instance-a\0" + b"1\0" + hashlib.sha256(b"a\tb").hexdigest().encode()
        ).hexdigest()
        self.assertEqual(one, expected)
        self.assertEqual(
            one,
            history_store.origin_stable_id("instance-a", 1, b"a\tb\r\n"),
        )
        self.assertNotEqual(
            one, history_store.origin_stable_id("instance-b", 1, b"a\tb")
        )
        self.assertNotEqual(
            one, history_store.origin_stable_id("instance-a", 2, b"a\tb")
        )

    def test_exact_duplicate_rows_are_distinct_candidates_in_one_lineage(self):
        duplicate = self.root / "duplicate.tsv"
        duplicate.write_bytes(
            HEADER + row("same proposition") + b"\n" + row("same proposition") + b"\n"
        )
        receipt = history_store.import_tsv_epoch(self.conn, duplicate)
        candidates = self._candidate_for_story("same proposition")
        self.assertEqual(len(candidates), 2)
        self.assertNotEqual(candidates[0]["candidate_id"], candidates[1]["candidate_id"])
        self.assertEqual(candidates[0]["lineage_id"], candidates[1]["lineage_id"])
        self.assertEqual(receipt["root_candidate_id"], candidates[0]["candidate_id"])

    def test_import_plan_is_sealed_before_commit(self):
        plan = history_store.build_import_plan(
            {"ledger": self.ledger}, self.state_root
        )
        self.assertTrue(pathlib.Path(plan["plan_path"]).is_file())
        self.assertTrue(pathlib.Path(plan["manifest_path"]).is_file())
        for sealed in plan["sealed_inputs"]:
            self.assertTrue(pathlib.Path(sealed["cas_path"]).is_file())

    def test_commit_rejects_changed_sealed_manifest(self):
        plan = history_store.build_import_plan(
            {"ledger": self.ledger}, self.state_root
        )
        pathlib.Path(plan["manifest_path"]).write_bytes(b"changed\n")
        with self.assertRaises(history_store.ImportConflict):
            history_store.commit_import_plan(self.conn, plan)
        self.assertEqual(self._canonical_counts(), (0, 0, 0, 0))

    def test_import_retry_verifies_existing_physical_rows(self):
        plan = history_store.build_import_plan(
            {"ledger": self.ledger}, self.state_root
        )
        history_store.commit_import_plan(self.conn, plan)
        self.conn.execute(
            "UPDATE candidates SET raw_row = ? WHERE source_sequence = 1",
            (b"tampered",),
        )
        with self.assertRaises(history_store.ImportConflict):
            history_store.commit_import_plan(self.conn, plan)

    def test_import_retry_repairs_every_missing_sealed_union_result(self):
        ledger = self.root / "retry-union.tsv"
        ledger.write_bytes(
            HEADER + row("retry parent") + b"\n" + row("retry child") + b"\n"
        )
        evidence = self.root / "retry-evidence.json"
        evidence.write_text('{"verified":true}\n', encoding="utf-8")
        mapping = self.root / "retry-mapping.json"
        mapping.write_text(
            json.dumps(
                {
                    "version": "lineage-mapping-v1",
                    "mappings": [
                        {
                            "parent_row": 1,
                            "child_row": 2,
                            "evidence_path": str(evidence),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        plan = history_store.build_import_plan(
            {"ledger": ledger, "mapping_manifest": mapping}, self.state_root
        )
        history_store.commit_import_plan(self.conn, plan)
        self.conn.execute("DELETE FROM lineage_edges")
        self.conn.execute("DELETE FROM artifacts")
        self.conn.execute(
            "DELETE FROM story_aliases WHERE canonical_story = 'retry child'"
        )
        self.conn.execute("DELETE FROM search_projection_outbox")
        self.conn.execute("DELETE FROM ledger_projection_outbox")
        self.assertFalse(history_store.validate_store(self.conn)["ok"])
        receipt = history_store.commit_import_plan(self.conn, plan)
        self.assertTrue(receipt["idempotent"])
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM lineage_edges").fetchone()[0], 1
        )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM artifacts").fetchone()[0], 1
        )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM story_aliases").fetchone()[0], 2
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM search_projection_outbox"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM ledger_projection_outbox"
            ).fetchone()[0],
            1,
        )
        self.assertTrue(history_store.validate_store(self.conn)["ok"])

    def test_import_retry_rejects_conflicting_sealed_lineage_root(self):
        duplicate = self.root / "retry-root.tsv"
        duplicate.write_bytes(
            HEADER + row("retry root") + b"\n" + row("retry root") + b"\n"
        )
        plan = history_store.build_import_plan(
            {"ledger": duplicate}, self.state_root
        )
        history_store.commit_import_plan(self.conn, plan)
        child = self.conn.execute(
            "SELECT candidate_id FROM candidates WHERE source_sequence = 2"
        ).fetchone()[0]
        self.conn.execute("UPDATE lineages SET root_candidate_id = ?", (child,))
        with self.assertRaises(history_store.ImportConflict):
            history_store.commit_import_plan(self.conn, plan)

    def test_conflicting_lineage_mapping_rolls_back_whole_epoch(self):
        self._import()
        other = self.root / "other.tsv"
        other.write_bytes(HEADER + row("new mapped story") + b"\n")
        plan = history_store.build_import_plan({"ledger": other}, self.state_root)
        plan["rows"][0]["lineage_id"] = self.conn.execute(
            "SELECT lineage_id FROM candidates ORDER BY source_sequence LIMIT 1"
        ).fetchone()[0]
        before = self._canonical_counts()
        with self.assertRaises(history_store.ImportConflict):
            history_store.commit_import_plan(self.conn, plan)
        self.assertEqual(self._canonical_counts(), before)

    def test_existing_anchor_conflict_rolls_back_inside_import_transaction(self):
        mapped = self.root / "transaction-conflict.tsv"
        mapped.write_bytes(
            HEADER + row("transaction root") + b"\n" + row("transaction child") + b"\n"
        )
        evidence = self.root / "transaction-evidence.json"
        evidence.write_text('{"verified":true}\n', encoding="utf-8")
        mapping = self.root / "transaction-mapping.json"
        mapping.write_text(
            json.dumps(
                {
                    "version": "lineage-mapping-v1",
                    "mappings": [
                        {
                            "parent_row": 1,
                            "child_row": 2,
                            "evidence_path": str(evidence),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        plan = history_store.build_import_plan(
            {"ledger": mapped, "mapping_manifest": mapping}, self.state_root
        )
        anchor = self.root / "anchor.tsv"
        anchor.write_bytes(HEADER + row("transaction child") + b"\n")
        history_store.import_tsv_epoch(self.conn, anchor)
        before = tuple(
            self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "import_epochs",
                "lineages",
                "candidates",
                "story_aliases",
                "lineage_edges",
                "artifacts",
                "search_projection_outbox",
                "ledger_projection_outbox",
            )
        )
        with self.assertRaises(history_store.ImportConflict):
            history_store.commit_import_plan(self.conn, plan)
        after = tuple(
            self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "import_epochs",
                "lineages",
                "candidates",
                "story_aliases",
                "lineage_edges",
                "artifacts",
                "search_projection_outbox",
                "ledger_projection_outbox",
            )
        )
        self.assertEqual(after, before)

    def test_explicit_mapping_unions_lineage_and_installs_typed_edge(self):
        ledger = self.root / "mapped.tsv"
        ledger.write_bytes(
            HEADER + row("mapped parent") + b"\n" + row("mapped child") + b"\n"
        )
        evidence = self.root / "parent-evidence.json"
        evidence.write_text('{"verified":true}\n', encoding="utf-8")
        mapping = self.root / "mapping.json"
        mapping.write_text(
            json.dumps(
                {
                    "version": "lineage-mapping-v1",
                    "mappings": [
                        {
                            "parent_row": 1,
                            "child_row": 2,
                            "relation_type": "evolved_from",
                            "authority": "manual_mapping",
                            "evidence_path": str(evidence),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        plan = history_store.build_import_plan(
            {"ledger": ledger, "mapping_manifest": mapping}, self.state_root
        )
        history_store.commit_import_plan(self.conn, plan)
        lineages = {
            item[0] for item in self.conn.execute("SELECT lineage_id FROM candidates")
        }
        self.assertEqual(len(lineages), 1)
        edge = self.conn.execute(
            "SELECT relation_type FROM lineage_edges"
        ).fetchone()
        self.assertEqual(edge[0], "evolved_from")

    def test_verified_parent_evidence_is_a_union_source(self):
        ledger = self.root / "parent-evidence.tsv"
        ledger.write_bytes(
            HEADER + row("archive parent") + b"\n" + row("archive child") + b"\n"
        )
        evidence = self.root / "parent-evidence.json"
        evidence.write_text(
            json.dumps(
                {
                    "version": "history-parent-evidence-v1",
                    "edges": [
                        {
                            "parent_row": 1,
                            "child_row": 2,
                            "relation_type": "evolved_from",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        plan = history_store.build_import_plan(
            {"ledger": ledger, "parent_evidence": [evidence]}, self.state_root
        )
        history_store.commit_import_plan(self.conn, plan)
        self.assertEqual(
            self.conn.execute(
                "SELECT count(DISTINCT lineage_id) FROM candidates"
            ).fetchone()[0],
            1,
        )

    def test_promotion_attestation_is_a_union_source(self):
        ledger = self.root / "promotion.tsv"
        ledger.write_bytes(
            HEADER + row("promotion origin") + b"\n" + row("promotion result") + b"\n"
        )
        attestation = self.root / "promotion.json"
        attestation.write_text(
            json.dumps(
                {
                    "version": "promotion-attestation-v1",
                    "origin_row_number": 1,
                    "committed_row_number": 2,
                }
            ),
            encoding="utf-8",
        )
        plan = history_store.build_import_plan(
            {"ledger": ledger, "promotion_receipts": [attestation]},
            self.state_root,
        )
        history_store.commit_import_plan(self.conn, plan)
        edge = self.conn.execute(
            "SELECT relation_type FROM lineage_edges"
        ).fetchone()
        self.assertEqual(edge[0], "supersedes")

    def test_mapping_cycle_is_rejected_before_plan_is_sealed(self):
        ledger = self.root / "cycle.tsv"
        ledger.write_bytes(
            HEADER + row("cycle a") + b"\n" + row("cycle b") + b"\n"
        )
        evidence = self.root / "cycle-evidence.json"
        evidence.write_text('{"verified":true}\n', encoding="utf-8")
        mapping = self.root / "cycle-mapping.json"
        mapping.write_text(
            json.dumps(
                {
                    "version": "lineage-mapping-v1",
                    "roots": [{"row": 1}],
                    "mappings": [
                        {
                            "parent_row": 1,
                            "child_row": 2,
                            "evidence_path": str(evidence),
                        },
                        {
                            "parent_row": 2,
                            "child_row": 1,
                            "evidence_path": str(evidence),
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(history_store.ImportConflict):
            history_store.build_import_plan(
                {"ledger": ledger, "mapping_manifest": mapping}, self.state_root
            )

    def test_explicit_root_must_be_the_parentless_ancestor(self):
        ledger = self.root / "wrong-root.tsv"
        ledger.write_bytes(
            HEADER + row("true root") + b"\n" + row("named child") + b"\n"
        )
        evidence = self.root / "wrong-root-evidence.json"
        evidence.write_text('{"verified":true}\n', encoding="utf-8")
        mapping = self.root / "wrong-root-mapping.json"
        mapping.write_text(
            json.dumps(
                {
                    "version": "lineage-mapping-v1",
                    "roots": [{"row": 2}],
                    "mappings": [
                        {
                            "parent_row": 1,
                            "child_row": 2,
                            "evidence_path": str(evidence),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(history_store.ImportConflict):
            history_store.build_import_plan(
                {"ledger": ledger, "mapping_manifest": mapping}, self.state_root
            )

    def test_ambiguous_explicit_parent_is_rejected_before_plan_is_sealed(self):
        ledger = self.root / "ambiguous.tsv"
        ledger.write_bytes(
            HEADER
            + row("parent a")
            + b"\n"
            + row("parent b")
            + b"\n"
            + row("shared child")
            + b"\n"
        )
        evidence = self.root / "ambiguous-evidence.json"
        evidence.write_text('{"verified":true}\n', encoding="utf-8")
        mapping = self.root / "ambiguous-mapping.json"
        mapping.write_text(
            json.dumps(
                {
                    "version": "lineage-mapping-v1",
                    "roots": [{"row": 1}],
                    "mappings": [
                        {
                            "parent_row": 1,
                            "child_row": 3,
                            "evidence_path": str(evidence),
                        },
                        {
                            "parent_row": 2,
                            "child_row": 3,
                            "evidence_path": str(evidence),
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(history_store.ImportConflict):
            history_store.build_import_plan(
                {"ledger": ledger, "mapping_manifest": mapping}, self.state_root
            )

    def _edge_fixture(self):
        ledger = self.root / "edges.tsv"
        ledger.write_bytes(
            HEADER
            + row("edge family")
            + b"\n"
            + row("edge family")
            + b"\n"
            + row("edge family")
            + b"\n"
        )
        history_store.import_tsv_epoch(self.conn, ledger)
        candidates = self.conn.execute(
            "SELECT candidate_id FROM candidates ORDER BY source_sequence"
        ).fetchall()
        ids = [item[0] for item in candidates]
        evidence = []
        for index in range(3):
            artifact_id = f"evidence-{index}"
            self.conn.execute(
                """
                INSERT INTO artifacts(
                  artifact_id, kind, state, sha256, byte_count, source_path,
                  source_sequence, provenance_json, idempotency_key
                ) VALUES (?, 'lineage-evidence', 'installed', ?, 1, ?, ?, '{}', ?)
                """,
                (artifact_id, f"{index:064x}", f"evidence/{index}", index, artifact_id),
            )
            evidence.append(artifact_id)
        self.conn.commit()
        return ids, evidence

    def test_similarity_cannot_write_lineage_edge(self):
        ids, _ = self._edge_fixture()
        with self.assertRaises(ValueError):
            history_store.add_lineage_edge(
                self.conn,
                ids[0],
                ids[1],
                "evolved_from",
                evidence_artifact_id=None,
                authority="similarity",
            )

    def test_explicit_lineage_edge_cannot_create_cycle(self):
        ids, evidence = self._edge_fixture()
        history_store.add_lineage_edge(
            self.conn, ids[0], ids[1], "evolved_from", evidence[0], "explicit"
        )
        history_store.add_lineage_edge(
            self.conn, ids[1], ids[2], "evolved_from", evidence[1], "explicit"
        )
        with self.assertRaises(history_store.LineageCycle):
            history_store.add_lineage_edge(
                self.conn, ids[2], ids[0], "evolved_from", evidence[2], "explicit"
            )

    def test_runtime_lineage_edge_cannot_cross_lineages(self):
        self._import()
        candidates = self.conn.execute(
            "SELECT candidate_id FROM candidates ORDER BY source_sequence LIMIT 2"
        ).fetchall()
        artifact_id = "cross-lineage-evidence"
        self.conn.execute(
            """
            INSERT INTO artifacts(
              artifact_id, kind, state, sha256, byte_count, source_path,
              source_sequence, provenance_json, idempotency_key
            ) VALUES (?, 'lineage-evidence', 'installed', ?, 1, ?, 1, '{}', ?)
            """,
            (artifact_id, "1" * 64, "evidence/cross", artifact_id),
        )
        with self.assertRaises(ValueError):
            history_store.add_lineage_edge(
                self.conn,
                candidates[0][0],
                candidates[1][0],
                "evolved_from",
                artifact_id,
                "explicit",
            )

    def test_runtime_lineage_edge_preserves_root_ancestry_and_one_parent(self):
        ids, evidence = self._edge_fixture()
        with self.assertRaises(ValueError):
            history_store.add_lineage_edge(
                self.conn, ids[1], ids[2], "evolved_from", evidence[0], "explicit"
            )
        history_store.add_lineage_edge(
            self.conn, ids[0], ids[1], "evolved_from", evidence[0], "explicit"
        )
        history_store.add_lineage_edge(
            self.conn, ids[0], ids[2], "evolved_from", evidence[1], "explicit"
        )
        with self.assertRaises(ValueError):
            history_store.add_lineage_edge(
                self.conn, ids[1], ids[2], "evolved_from", evidence[2], "explicit"
            )

    def test_validate_store_rejects_edge_outside_root_ancestry(self):
        ids, evidence = self._edge_fixture()
        self.conn.execute(
            """
            INSERT INTO lineage_edges(
              parent_candidate_id, child_candidate_id, relation_type,
              evidence_artifact_id
            ) VALUES(?, ?, 'evolved_from', ?)
            """,
            (ids[1], ids[2], evidence[0]),
        )
        report = history_store.validate_store(self.conn)
        self.assertFalse(report["ok"])
        self.assertIn("lineage_edge_outside_root_ancestry", report["issues"])

    def test_near_sa_priority_survives_queue_removal(self):
        self._import()
        fixture = self.root / "near-sa.tsv"
        fixture.write_text(
            "2026-07-23\trun/I1\tnear sa proposition\t"
            "Evaluation and Diagnostics\tlow\t2,1,1\tdesign-fixable\n",
            encoding="utf-8",
        )
        history_store.import_near_sa_observations(self.conn, fixture)
        expected = self._candidate_for_story("near sa proposition")[0]["candidate_id"]
        fixture.unlink()
        parent = history_store.select_generation_parent(self.conn)
        self.assertEqual(parent["candidate_id"], expected)

    def test_later_ledger_winner_invalidates_stale_near_sa_observation(self):
        self._import()
        fixture = self.root / "near-sa.tsv"
        fixture.write_text(
            "2026-07-23\trun/I1\tnear sa proposition\t"
            "Evaluation and Diagnostics\tlow\t2,1,1\tdesign-fixable\n",
            encoding="utf-8",
        )
        history_store.import_near_sa_observations(self.conn, fixture)
        history_store.append_rows(
            self.conn,
            [row("near sa proposition", verdict="reject", overlap="high",
                 category="novelty-capped")],
            {"run_id": "r2"},
        )
        self.assertIsNone(history_store.select_generation_parent(self.conn))

    def test_canonical_story_duplicate_invalidates_near_sa_observation(self):
        self._import()
        fixture = self.root / "near-sa.tsv"
        fixture.write_text(
            "2026-07-23\trun/I1\tnear sa proposition\t"
            "Evaluation and Diagnostics\tlow\t2,1,1\tdesign-fixable\n",
            encoding="utf-8",
        )
        history_store.import_near_sa_observations(self.conn, fixture)
        history_store.append_rows(
            self.conn,
            [row("near  sa proposition")],
            {"run_id": "r2"},
        )
        self.assertIsNone(history_store.select_generation_parent(self.conn))

    def test_earlier_canonical_duplicate_consumes_near_sa_story_once(self):
        ledger = self.root / "canonical-history.tsv"
        ledger.write_bytes(
            HEADER
            + row("near  sa proposition")
            + b"\n"
            + row("near sa proposition")
            + b"\n"
        )
        history_store.import_tsv_epoch(self.conn, ledger)
        fixture = self.root / "canonical-near-sa.tsv"
        fixture.write_text(
            "2026-07-23\trun/I2\tnear sa proposition\t"
            "Evaluation and Diagnostics\tlow\t2,1,1\tdesign-fixable\n",
            encoding="utf-8",
        )
        history_store.import_near_sa_observations(self.conn, fixture)
        self.assertIsNone(history_store.select_generation_parent(self.conn))

    def _fresh_projection_fixture(self):
        root = pathlib.Path(tempfile.mkdtemp(dir=self.root))
        (root / "ledger.instance-id").write_text("projection-instance\n", encoding="utf-8")
        ledger = root / "ledger.tsv"
        ledger.write_bytes(HEADER + row("projection proposition") + b"\n")
        conn = history_store.connect(root / "history.sqlite3")
        history_store.init_schema(conn)
        history_store.import_tsv_epoch(conn, ledger)
        ledger.write_bytes(b"stale\n")
        ledger_good = root / "tmp" / "ledger.good"
        targets = {"ledger.tsv": ledger, "tmp/ledger.good": ledger_good}
        return types.SimpleNamespace(
            root=root,
            state_root=root / ".ai-ideas",
            ledger=ledger,
            ledger_good=ledger_good,
            targets=targets,
            conn=conn,
            current_db_sequence=1,
            read_pointer=lambda: json.loads(
                (root / ".ai-ideas" / "ledger-current.json").read_text()
            ),
        )

    def test_reconcile_after_each_ledger_projection_crash(self):
        fault_points = (
            "db_commit",
            "snapshot_temp_fsync",
            "snapshot_rename",
            "snapshot_parent_fsync",
            "pointer_temp_fsync",
            "pointer_rename",
            "pointer_parent_fsync",
            "ledger_temp_fsync",
            "ledger_rename",
            "ledger_parent_fsync",
            "ledger_receipt_fsync",
            "ledger_good_temp_fsync",
            "ledger_good_rename",
            "ledger_good_parent_fsync",
            "ledger_good_receipt_fsync",
            "db_mark",
        )
        for crash_after in fault_points:
            with self.subTest(crash_after=crash_after):
                fixture = self._fresh_projection_fixture()
                try:
                    with self.assertRaises(history_store.InjectedCrash):
                        history_store.materialize_ledger_projection(
                            fixture.conn,
                            fixture.targets,
                            fixture.state_root,
                            fault_after=crash_after,
                        )
                    history_store.reconcile_ledger_projection(
                        fixture.conn,
                        fixture.targets,
                        fixture.state_root,
                        now=time.time() + history_store.LEASE_SECONDS + 1,
                    )
                    expected = history_store.render_tsv(fixture.conn)
                    self.assertEqual(fixture.ledger.read_bytes(), expected)
                    self.assertEqual(fixture.ledger_good.read_bytes(), expected)
                    self.assertEqual(
                        history_store.pending_ledger_projection_count(fixture.conn), 0
                    )
                finally:
                    fixture.conn.close()

    def test_newer_full_projection_satisfies_older_pending_rows(self):
        fixture = self._fresh_projection_fixture()
        try:
            history_store.append_rows(
                fixture.conn, [row("second projection")], {"run_id": "r2"}
            )
            fixture.current_db_sequence = 2
            history_store.reconcile_ledger_projection(
                fixture.conn, fixture.targets, fixture.state_root
            )
            pointer = fixture.read_pointer()
            self.assertEqual(pointer["sequence"], fixture.current_db_sequence)
            rows = fixture.conn.execute(
                """
                SELECT projection_sequence, satisfied_by_sequence
                FROM ledger_projection_outbox ORDER BY projection_sequence
                """
            ).fetchall()
            self.assertEqual([item[0] for item in rows], [1, 2])
            self.assertEqual([item[1] for item in rows], [2, 2])
        finally:
            fixture.conn.close()

    def test_reconcile_satisfies_repaired_older_projection_behind_current_done(self):
        root = pathlib.Path(tempfile.mkdtemp(dir=self.root))
        (root / "ledger.instance-id").write_text(
            "repaired-projection-instance\n", encoding="utf-8"
        )
        ledger = root / "ledger.tsv"
        ledger.write_bytes(HEADER + row("first projection") + b"\n")
        state_root = root / ".ai-ideas"
        conn = history_store.connect(root / "history.sqlite3")
        history_store.init_schema(conn)
        try:
            plan = history_store.build_import_plan({"ledger": ledger}, state_root)
            history_store.commit_import_plan(conn, plan)
            history_store.append_rows(
                conn, [row("second projection")], {"run_id": "r2"}
            )
            targets = {
                "ledger.tsv": ledger,
                "tmp/ledger.good": root / "tmp" / "ledger.good",
            }
            history_store.reconcile_ledger_projection(conn, targets, state_root)
            conn.execute(
                "DELETE FROM ledger_projection_outbox "
                "WHERE projection_sequence = 1"
            )
            history_store.commit_import_plan(conn, plan)

            conn.execute(
                """
                UPDATE ledger_projection_outbox
                SET state = 'processing', generation = generation + 1,
                    claim_token = 'live-older-claim', lease_until = ?,
                    satisfied_by_sequence = NULL, satisfied_by_sha256 = NULL,
                    completed_at = NULL
                WHERE projection_sequence = 1
                """,
                (str(time.time() + history_store.LEASE_SECONDS),),
            )
            self.assertTrue(history_store.validate_store(conn)["ok"])
            conn.execute(
                """
                UPDATE ledger_projection_outbox
                SET state = 'pending', claim_token = NULL, lease_until = NULL
                WHERE projection_sequence = 1
                """
            )
            invalid = history_store.validate_store(conn)
            self.assertFalse(invalid["ok"])
            self.assertIn(
                "older_pending_projection_behind_current_done",
                invalid["issues"],
            )

            publication = history_store.reconcile_ledger_projection(
                conn, targets, state_root
            )
            self.assertEqual(publication["sequence"], 2)
            rows = conn.execute(
                """
                SELECT projection_sequence, state, satisfied_by_sequence
                FROM ledger_projection_outbox ORDER BY projection_sequence
                """
            ).fetchall()
            self.assertEqual(
                [tuple(item) for item in rows],
                [(1, "done", 2), (2, "done", 2)],
            )
            self.assertEqual(
                history_store.pending_ledger_projection_count(conn), 0
            )
            self.assertTrue(history_store.validate_store(conn)["ok"])
        finally:
            conn.close()

    def test_pointer_cannot_regress_or_change_hash_at_equal_sequence(self):
        fixture = self._fresh_projection_fixture()
        try:
            fixture.state_root.mkdir(parents=True, exist_ok=True)
            (fixture.state_root / "ledger-current.json").write_text(
                json.dumps(
                    {
                        "sequence": 99,
                        "sha256": "higher",
                        "row_count": 1,
                        "immutable_object": "invalid",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(history_store.ProjectionConflict):
                history_store.reconcile_ledger_projection(
                    fixture.conn, fixture.targets, fixture.state_root
                )
        finally:
            fixture.conn.close()

    def test_two_consumers_fence_stale_projection_token(self):
        fixture = self._fresh_projection_fixture()
        try:
            claim_a = history_store.claim_ledger_projection(fixture.conn, now=10)
            sequence_before_renewal = fixture.conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'projection_sequence'"
            ).fetchone()[0]
            renewed = history_store.renew_ledger_projection_claim(
                fixture.conn, claim_a, now=11
            )
            self.assertGreater(renewed["lease_until"], claim_a["lease_until"])
            self.assertEqual(
                fixture.conn.execute(
                    "SELECT value FROM schema_meta WHERE key = 'projection_sequence'"
                ).fetchone()[0],
                sequence_before_renewal,
            )
            claim_a = renewed
            self.assertIsNone(history_store.claim_ledger_projection(fixture.conn, now=11))
            claim_b = history_store.claim_ledger_projection(fixture.conn, now=100)
            with self.assertRaises(history_store.StaleClaim):
                history_store.publish_claimed_ledger_projection(
                    fixture.conn, claim_a, fixture.targets, fixture.state_root
                )
            history_store.publish_claimed_ledger_projection(
                fixture.conn, claim_b, fixture.targets, fixture.state_root
            )
            self.assertEqual(
                history_store.pending_ledger_projection_count(fixture.conn), 0
            )
            pointer = fixture.read_pointer()
            pointer["sha256"] = "wrong"
            (fixture.state_root / "ledger-current.json").write_text(
                json.dumps(pointer), encoding="utf-8"
            )
            with self.assertRaises(history_store.ProjectionConflict):
                history_store.reconcile_ledger_projection(
                    fixture.conn, fixture.targets, fixture.state_root
                )
        finally:
            fixture.conn.close()

    def test_reconcile_does_not_steal_an_unexpired_live_claim(self):
        fixture = self._fresh_projection_fixture()
        second = history_store.connect(fixture.root / "history.sqlite3")
        try:
            claim = history_store.claim_ledger_projection(
                fixture.conn, now=time.time()
            )
            self.assertIsNone(
                history_store.reconcile_ledger_projection(
                    second, fixture.targets, fixture.state_root
                )
            )
            row = second.execute(
                "SELECT state, generation, claim_token FROM ledger_projection_outbox"
            ).fetchone()
            self.assertEqual(tuple(row), ("processing", claim["generation"],
                                          claim["claim_token"]))
        finally:
            second.close()
            fixture.conn.close()

    def test_export_lock_excludes_append_between_render_and_publication(self):
        fixture = self._fresh_projection_fixture()
        rendered = threading.Event()
        resume = threading.Event()
        append_done = threading.Event()
        publisher_result = []
        append_result = []
        original_atomic = history_store._atomic_replace

        def paused_atomic(path, data, *args, **kwargs):
            if (
                pathlib.Path(path).parent.name == "ledger-snapshots"
                and not rendered.is_set()
            ):
                rendered.set()
                resume.wait(5)
            return original_atomic(path, data, *args, **kwargs)

        def publish():
            conn = history_store.connect(fixture.root / "history.sqlite3")
            try:
                publisher_result.append(
                    history_store.materialize_ledger_projection(
                        conn, fixture.targets, fixture.state_root
                    )
                )
            finally:
                conn.close()

        def append():
            conn = history_store.connect(fixture.root / "history.sqlite3")
            try:
                append_result.append(
                    history_store.append_rows(
                        conn, [row("concurrent append")], {"run_id": "r2"}
                    )
                )
            finally:
                append_done.set()
                conn.close()

        with mock.patch.object(
            history_store, "_atomic_replace", side_effect=paused_atomic
        ):
            publisher = threading.Thread(target=publish)
            publisher.start()
            self.assertTrue(rendered.wait(5))
            appender = threading.Thread(target=append)
            appender.start()
            blocked = not append_done.wait(0.2)
            resume.set()
            publisher.join(5)
            appender.join(5)
        try:
            self.assertTrue(blocked)
            self.assertEqual(publisher_result[0]["sequence"], 1)
            self.assertEqual(append_result[0]["projection_sequence"], 2)
        finally:
            fixture.conn.close()

    def test_recovery_fsyncs_visible_snapshot_and_pointer_after_rename_crash(self):
        for fault_point, expected_name in (
            ("snapshot_rename", "ledger-snapshots"),
            ("pointer_rename", "ledger-current.json"),
        ):
            with self.subTest(fault_point=fault_point):
                fixture = self._fresh_projection_fixture()
                try:
                    with self.assertRaises(history_store.InjectedCrash):
                        history_store.materialize_ledger_projection(
                            fixture.conn,
                            fixture.targets,
                            fixture.state_root,
                            fault_after=fault_point,
                        )
                    repaired = []
                    with mock.patch.object(
                        history_store,
                        "_fsync_existing",
                        side_effect=lambda path: repaired.append(pathlib.Path(path)),
                        create=True,
                    ):
                        history_store.reconcile_ledger_projection(
                            fixture.conn,
                            fixture.targets,
                            fixture.state_root,
                            now=time.time() + history_store.LEASE_SECONDS + 1,
                        )
                    self.assertTrue(
                        any(
                            expected_name in str(path)
                            for path in repaired
                        )
                    )
                finally:
                    fixture.conn.close()

    def test_reusing_sealed_import_objects_repairs_file_and_parent_durability(self):
        first = history_store.build_import_plan(
            {"ledger": self.ledger}, self.state_root
        )
        repaired = []
        directories = []
        def record_existing(path):
            existing = pathlib.Path(path)
            repaired.append(existing)
            history_store._fsync_directory(existing.parent)

        with mock.patch.object(
            history_store,
            "_fsync_existing",
            side_effect=record_existing,
            create=True,
        ), mock.patch.object(
            history_store,
            "_fsync_directory",
            side_effect=lambda path: directories.append(pathlib.Path(path)),
        ):
            second = history_store.build_import_plan(
                {"ledger": self.ledger}, self.state_root
            )
        self.assertEqual(first["plan_sha256"], second["plan_sha256"])
        self.assertGreaterEqual(len(repaired), 3)
        self.assertIn(self.state_root / "import-cas", directories)

    def test_fresh_database_state_and_cas_directories_fsync_installed_parents(self):
        layouts = (
            ("database-under-state", True),
            ("separate-database", False),
        )
        for name, database_under_state in layouts:
            with self.subTest(name=name):
                layout = self.root / name
                layout.mkdir()
                state_root = layout / ".ai-ideas"
                database = (
                    state_root / "history.sqlite3"
                    if database_under_state
                    else layout / "database" / "history.sqlite3"
                )
                fsynced = []
                original_fsync_directory = history_store._fsync_directory

                def record_fsync(path):
                    fsynced.append(pathlib.Path(path))
                    original_fsync_directory(path)

                with mock.patch.object(
                    history_store,
                    "_fsync_directory",
                    side_effect=record_fsync,
                ):
                    conn = history_store.connect(database)
                    try:
                        history_store.init_schema(conn)
                        history_store.build_import_plan(
                            {"ledger": self.ledger}, state_root
                        )
                    finally:
                        conn.close()

                self.assertIn(layout, fsynced)
                self.assertIn(state_root, fsynced)
                if not database_under_state:
                    self.assertGreaterEqual(fsynced.count(layout), 2)

    def test_corrupt_filesystem_receipt_cannot_complete_projection(self):
        fixture = self._fresh_projection_fixture()
        original_atomic = history_store._atomic_replace

        def corrupt_receipt(path, data, *args, **kwargs):
            result = original_atomic(path, data, *args, **kwargs)
            if pathlib.Path(path).name == "tmp__ledger.good.json":
                pathlib.Path(path).write_bytes(b"corrupt\n")
            return result

        try:
            with mock.patch.object(
                history_store, "_atomic_replace", side_effect=corrupt_receipt
            ):
                with self.assertRaises(history_store.ProjectionConflict):
                    history_store.materialize_ledger_projection(
                        fixture.conn, fixture.targets, fixture.state_root
                    )
            self.assertEqual(
                history_store.pending_ledger_projection_count(fixture.conn), 1
            )
            history_store.reconcile_ledger_projection(
                fixture.conn,
                fixture.targets,
                fixture.state_root,
                now=time.time() + history_store.LEASE_SECONDS + 1,
            )
            self.assertEqual(
                history_store.pending_ledger_projection_count(fixture.conn), 0
            )
        finally:
            fixture.conn.close()

    def test_materialization_is_idempotent_and_receipt_tampering_is_repaired(self):
        fixture = self._fresh_projection_fixture()
        try:
            first = history_store.materialize_ledger_projection(
                fixture.conn, fixture.targets, fixture.state_root
            )
            receipt = fixture.state_root / "ledger-target-receipts" / "ledger.tsv.json"
            receipt.write_text('{"sha256":"wrong"}\n', encoding="utf-8")
            second = history_store.materialize_ledger_projection(
                fixture.conn, fixture.targets, fixture.state_root
            )
            self.assertEqual(first["sha256"], second["sha256"])
            repaired = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(repaired["sha256"], first["sha256"])
        finally:
            fixture.conn.close()

    def test_equal_sequence_pointer_metadata_conflict_fails_closed(self):
        fixture = self._fresh_projection_fixture()
        try:
            history_store.materialize_ledger_projection(
                fixture.conn, fixture.targets, fixture.state_root
            )
            pointer_path = fixture.state_root / "ledger-current.json"
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            pointer["row_count"] += 1
            pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
            with self.assertRaises(history_store.ProjectionConflict):
                history_store.reconcile_ledger_projection(
                    fixture.conn, fixture.targets, fixture.state_root
                )
        finally:
            fixture.conn.close()

    def test_projection_targets_reject_aliases_and_reserved_state_paths(self):
        fixture = self._fresh_projection_fixture()
        try:
            with self.assertRaises(ValueError):
                history_store.materialize_ledger_projection(
                    fixture.conn,
                    {
                        "ledger.tsv": fixture.ledger,
                        "tmp/ledger.good": fixture.ledger,
                    },
                    fixture.state_root,
                )
            with self.assertRaises(ValueError):
                history_store.materialize_ledger_projection(
                    fixture.conn,
                    {
                        "ledger.tsv": fixture.root / "history.sqlite3",
                        "tmp/ledger.good": fixture.ledger_good,
                    },
                    fixture.state_root,
                )
            with self.assertRaises(ValueError):
                history_store.materialize_ledger_projection(
                    fixture.conn,
                    {
                        "ledger.tsv": fixture.state_root / "ledger-current.json",
                        "tmp/ledger.good": fixture.ledger_good,
                    },
                    fixture.state_root,
                )
            with self.assertRaises(ValueError):
                history_store.export_tsv(
                    fixture.conn, fixture.root / "history.sqlite3"
                )
        finally:
            fixture.conn.close()

    def test_projection_targets_reject_hardlinks_to_database_and_state(self):
        fixture = self._fresh_projection_fixture()
        try:
            database_alias = fixture.root / "database-alias"
            os.link(fixture.root / "history.sqlite3", database_alias)
            with self.assertRaises(ValueError):
                history_store.materialize_ledger_projection(
                    fixture.conn,
                    {
                        "ledger.tsv": database_alias,
                        "tmp/ledger.good": fixture.ledger_good,
                    },
                    fixture.state_root,
                )
            history_store.materialize_ledger_projection(
                fixture.conn, fixture.targets, fixture.state_root
            )
            pointer_alias = fixture.root / "pointer-alias"
            os.link(fixture.state_root / "ledger-current.json", pointer_alias)
            with self.assertRaises(ValueError):
                history_store.materialize_ledger_projection(
                    fixture.conn,
                    {
                        "ledger.tsv": pointer_alias,
                        "tmp/ledger.good": fixture.ledger_good,
                    },
                    fixture.state_root,
                )
        finally:
            fixture.conn.close()

    def test_header_only_initialization_enqueues_and_materializes_projection(self):
        ledger = self.root / "header-only.tsv"
        ledger.write_bytes(HEADER.rstrip(b"\n") + b"\r\n")
        receipt = history_store.import_tsv_epoch(self.conn, ledger)
        self.assertEqual(receipt["data_rows"], 0)
        self.assertEqual(
            history_store.pending_ledger_projection_count(self.conn), 1
        )
        targets = {
            "ledger.tsv": self.root / "published-ledger.tsv",
            "tmp/ledger.good": self.root / "tmp" / "ledger.good",
        }
        history_store.materialize_ledger_projection(
            self.conn, targets, self.state_root
        )
        self.assertEqual(targets["ledger.tsv"].read_bytes(), ledger.read_bytes())
        self.assertEqual(targets["tmp/ledger.good"].read_bytes(), ledger.read_bytes())

    def test_append_after_unterminated_header_only_import_inserts_one_separator(self):
        header = HEADER.rstrip(b"\n")
        ledger = self.root / "unterminated-header-only.tsv"
        ledger.write_bytes(header)
        plan = history_store.build_import_plan({"ledger": ledger}, self.state_root)
        receipt = history_store.commit_import_plan(self.conn, plan)
        self.assertEqual(receipt["data_rows"], 0)
        self.assertEqual(history_store.render_tsv(self.conn), header)

        appended = row("first appended proposition")
        history_store.append_rows(
            self.conn, [appended], {"run_id": "header-first-row"}
        )
        expected = header + b"\n" + appended + b"\n"
        self.assertEqual(history_store.render_tsv(self.conn), expected)
        self.assertEqual(
            history_store._render_projection_prefix(self.conn, 0), header
        )

        retry = history_store.commit_import_plan(self.conn, plan)
        self.assertTrue(retry["idempotent"])
        self.assertTrue(history_store.validate_store(self.conn)["ok"])

        ledger_good = self.root / "tmp" / "unterminated-header.good"
        history_store.materialize_ledger_projection(
            self.conn,
            {"ledger.tsv": ledger, "tmp/ledger.good": ledger_good},
            self.state_root,
        )
        self.assertEqual(ledger.read_bytes(), expected)
        self.assertEqual(ledger_good.read_bytes(), expected)
        self.assertTrue(history_store.validate_store(self.conn)["ok"])

    def test_origin_stable_id_requires_exact_positive_integer_ordinal(self):
        for invalid in (True, False, 1.5, "1", 0, -1):
            with self.subTest(invalid=invalid):
                with self.assertRaises((TypeError, ValueError)):
                    history_store.origin_stable_id("instance", invalid, b"a\tb")

    def test_validate_store_reports_clean_state(self):
        self._import()
        report = history_store.validate_store(self.conn)
        self.assertTrue(report["ok"])
        self.assertEqual(report["candidates"], 3)


if __name__ == "__main__":
    unittest.main()
