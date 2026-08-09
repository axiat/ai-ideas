#!/usr/bin/env python3
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_store


HEADER = b"date\tsource\ttheme\tidea\tverdict\treason\toverlap\tcategory\n"


def ledger_row(index):
    return (
        f"2026-08-09\thunt\tLineage\tchain {index}"
        "\taccept-w-rev\treason\tlow\tdesign-fixable\n"
    ).encode("utf-8")


class HistoryStoreLineageBatchRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.state_root = self.root / ".ai-ideas"
        (self.root / "ledger.instance-id").write_text(
            "lineage-batch-regression\n", encoding="utf-8"
        )
        self.conn = history_store.connect(self.root / "history.sqlite3")
        history_store.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _chain_plan(self, count, name):
        ledger = self.root / f"{name}.tsv"
        ledger.write_bytes(
            HEADER + b"".join(ledger_row(index) for index in range(count))
        )
        evidence = self.root / f"{name}-evidence.json"
        evidence.write_text('{"verified":true}\n', encoding="utf-8")
        mapping = self.root / f"{name}-mapping.json"
        mapping.write_text(
            json.dumps(
                {
                    "version": "lineage-mapping-v1",
                    "mappings": [
                        {
                            "parent_row": index,
                            "child_row": index + 1,
                            "evidence_path": str(evidence),
                        }
                        for index in range(1, count)
                    ],
                }
            ),
            encoding="utf-8",
        )
        return history_store.build_import_plan(
            {"ledger": ledger, "mapping_manifest": mapping}, self.state_root
        )

    def test_commit_and_repair_use_one_linear_graph_validation(self):
        edge_count = 255
        plan = self._chain_plan(edge_count + 1, "long-chain")
        calls = {"complete": 0, "single": 0, "recursive": 0}
        original_complete = history_store._validate_complete_lineage_graph
        original_single = history_store._insert_lineage_edge

        def counted_complete(conn):
            calls["complete"] += 1
            return original_complete(conn)

        def rejected_single(*args, **kwargs):
            calls["single"] += 1
            raise AssertionError("batch import used the single-edge validator")

        def trace(statement):
            if "WITH RECURSIVE" in statement.upper():
                calls["recursive"] += 1

        history_store._validate_complete_lineage_graph = counted_complete
        history_store._insert_lineage_edge = rejected_single
        self.conn.set_trace_callback(trace)
        try:
            result = history_store.commit_import_plan(self.conn, plan)
            self.assertFalse(result["idempotent"])
            self.assertEqual(
                calls, {"complete": 1, "single": 0, "recursive": 0}
            )
            self.assertEqual(
                self.conn.execute("SELECT count(*) FROM lineage_edges").fetchone()[0],
                edge_count,
            )

            missing = plan["edges"][edge_count // 2]
            self.conn.execute(
                """
                DELETE FROM lineage_edges
                WHERE parent_candidate_id = ? AND child_candidate_id = ?
                  AND relation_type = ?
                """,
                (
                    missing["parent_candidate_id"],
                    missing["child_candidate_id"],
                    missing["relation_type"],
                ),
            )
            calls.update(complete=0, single=0, recursive=0)
            repaired = history_store.commit_import_plan(self.conn, plan)
            self.assertTrue(repaired["idempotent"])
            self.assertEqual(
                calls, {"complete": 1, "single": 0, "recursive": 0}
            )
            self.assertEqual(
                self.conn.execute("SELECT count(*) FROM lineage_edges").fetchone()[0],
                edge_count,
            )
        finally:
            self.conn.set_trace_callback(None)
            history_store._validate_complete_lineage_graph = original_complete
            history_store._insert_lineage_edge = original_single

    def test_failed_batch_validation_rolls_back_every_edge(self):
        plan = self._chain_plan(8, "rollback")
        original_complete = history_store._validate_complete_lineage_graph

        def reject_graph(conn):
            raise history_store.ImportConflict("injected graph validation failure")

        history_store._validate_complete_lineage_graph = reject_graph
        try:
            with self.assertRaisesRegex(
                history_store.ImportConflict, "injected graph validation failure"
            ):
                history_store.commit_import_plan(self.conn, plan)
        finally:
            history_store._validate_complete_lineage_graph = original_complete

        for table in (
            "lineage_edges",
            "candidates",
            "lineages",
            "story_aliases",
            "import_epochs",
        ):
            with self.subTest(table=table):
                self.assertEqual(
                    self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0],
                    0,
                )


if __name__ == "__main__":
    unittest.main()
