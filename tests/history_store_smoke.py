#!/usr/bin/env python3
import hashlib
import json
import pathlib
import sqlite3
import tempfile
import types
import unittest

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
            + row("edge a")
            + b"\n"
            + row("edge b")
            + b"\n"
            + row("edge c")
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
                        fixture.conn, fixture.targets, fixture.state_root
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

    def test_validate_store_reports_clean_state(self):
        self._import()
        report = history_store.validate_store(self.conn)
        self.assertTrue(report["ok"])
        self.assertEqual(report["candidates"], 3)


if __name__ == "__main__":
    unittest.main()
