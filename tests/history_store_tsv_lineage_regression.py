#!/usr/bin/env python3
import json
import pathlib
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_store


HEADER = b"date\tsource\ttheme\tidea\tverdict\treason\toverlap\tcategory\n"
FIELD_NAMES = (
    "date",
    "source",
    "theme",
    "story",
    "verdict",
    "reason",
    "overlap",
    "category",
)


def row(story):
    return (
        "2026-08-09\thunt\tLineage\t"
        + story
        + "\taccept-w-rev\treason\tlow\tdesign-fixable"
    ).encode("utf-8")


class HistoryStoreTsvLineageRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.state_root = self.root / ".ai-ideas"
        (self.root / "ledger.instance-id").write_text(
            "tsv-lineage-regression\n", encoding="utf-8"
        )
        self.conn = history_store.connect(self.root / "history.sqlite3")
        history_store.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _mapping(self, entries, name="mapping"):
        evidence = self.root / f"{name}-evidence.json"
        evidence.write_text('{"verified":true}\n', encoding="utf-8")
        normalized = []
        for entry in entries:
            item = dict(entry)
            item.setdefault("evidence_path", str(evidence))
            normalized.append(item)
        mapping = self.root / f"{name}.json"
        mapping.write_text(
            json.dumps(
                {"version": "lineage-mapping-v1", "mappings": normalized}
            ),
            encoding="utf-8",
        )
        return mapping

    def _plan(self, stories, entries, name="mapped"):
        ledger = self.root / f"{name}.tsv"
        ledger.write_bytes(
            HEADER + b"".join(row(story) + b"\n" for story in stories)
        )
        mapping = self._mapping(entries, name)
        return history_store.build_import_plan(
            {"ledger": ledger, "mapping_manifest": mapping}, self.state_root
        )

    def test_append_normalization_rejects_cr_or_lf_in_every_structured_field(self):
        clean = {
            "date": "2026-08-09",
            "source": "hunt",
            "theme": "Lineage",
            "story": "story",
            "verdict": "accept-w-rev",
            "reason": "reason",
            "overlap": "low",
            "category": "design-fixable",
        }
        for field_name in FIELD_NAMES:
            for separator in ("\r", "\n", "\r\n"):
                value = dict(clean)
                value[field_name] += separator + "injected"
                with self.subTest(field=field_name, separator=repr(separator)):
                    with self.assertRaises(ValueError):
                        history_store._normalize_append_row(value)

        sequence = [clean[field] for field in FIELD_NAMES]
        for index in range(len(sequence)):
            value = list(sequence)
            value[index] += "\nextra"
            with self.subTest(sequence_field=index):
                with self.assertRaises(ValueError):
                    history_store._normalize_append_row(value)

    def test_append_normalization_allows_one_row_terminator_only(self):
        raw = row("terminal")
        self.assertEqual(history_store._normalize_append_row(raw + b"\n"), raw)
        self.assertEqual(history_store._normalize_append_row(raw + b"\r\n"), raw)
        for value in (raw + b"\r", raw + b"\n\n", raw + b"\r\n\n"):
            with self.subTest(value=value[-4:]):
                with self.assertRaises(ValueError):
                    history_store._normalize_append_row(value)

    def test_physical_line_split_accepts_only_lf_crlf_and_final_unterminated_row(self):
        self.assertEqual(
            history_store._split_physical_lines(b"one\ntwo\r\nthree"),
            [(b"one", b"\n"), (b"two", b"\r\n"), (b"three", b"")],
        )
        self.assertEqual(
            history_store._split_physical_lines(b"one\r\n"),
            [(b"one", b"\r\n")],
        )
        for value in (b"one\rtwo\n", b"one\r", b"\rone\n"):
            with self.subTest(value=value):
                with self.assertRaises(history_store.ImportConflict):
                    history_store._split_physical_lines(value)

    def test_alias_used_as_parent_becomes_the_candidate_root(self):
        plan = self._plan(
            ["same parent", "same parent", "mapped child"],
            [{"parent_row": 2, "child_row": 3}],
            "alias-parent",
        )
        self.assertEqual(
            {item["root_candidate_id"] for item in plan["rows"]},
            {plan["rows"][1]["candidate_id"]},
        )

        history_store.commit_import_plan(self.conn, plan)

        edge = self.conn.execute(
            "SELECT parent_candidate_id, child_candidate_id FROM lineage_edges"
        ).fetchone()
        self.assertEqual(
            tuple(edge),
            (
                plan["rows"][1]["candidate_id"],
                plan["rows"][2]["candidate_id"],
            ),
        )
        self.assertTrue(history_store.validate_store(self.conn)["ok"])

    def test_aliases_cannot_splice_disconnected_candidate_chains(self):
        with self.assertRaises(history_store.ImportConflict):
            self._plan(
                ["root", "alias bridge", "alias bridge", "child"],
                [
                    {"parent_row": 1, "child_row": 2},
                    {"parent_row": 3, "child_row": 4},
                ],
                "disconnected-alias",
            )

    def test_plan_validation_rejects_an_unreachable_alias_root(self):
        plan = self._plan(
            ["same parent", "same parent", "mapped child"],
            [{"parent_row": 2, "child_row": 3}],
            "forged-root",
        )
        unreachable = plan["rows"][0]["candidate_id"]
        for item in plan["rows"]:
            item["root_candidate_id"] = unreachable

        with self.assertRaises(history_store.ImportConflict):
            history_store._validate_import_plan_rows(plan)

    def test_identical_lineage_edges_are_deduplicated_before_plan_seal(self):
        duplicate = {"parent_row": 1, "child_row": 2}
        plan = self._plan(
            ["dedup parent", "dedup child"],
            [duplicate, duplicate],
            "deduplicated",
        )
        self.assertEqual(len(plan["edges"]), 1)
        evidence_roles = [
            item["role"]
            for item in plan["sealed_inputs"]
            if item["role"].startswith("lineage-evidence:")
        ]
        self.assertEqual(evidence_roles, ["lineage-evidence:1:2"])

        history_store.commit_import_plan(self.conn, plan)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM lineage_edges").fetchone()[0],
            1,
        )
    def test_duplicate_validation_is_order_independent(self):
        valid = {"parent_row": 1, "child_row": 2}
        invalid_cases = (
            {"parent_row": 1, "child_row": 2, "authority": "similarity"},
            {"parent_row": 1, "child_row": 2, "evidence_path": ""},
        )
        for case_index, invalid in enumerate(invalid_cases):
            for order_index, entries in enumerate(
                ((valid, invalid), (invalid, valid))
            ):
                with self.subTest(case=case_index, order=order_index):
                    with self.assertRaises(history_store.ImportConflict):
                        self._plan(
                            ["parent", "child"],
                            list(entries),
                            f"bad-duplicate-{case_index}-{order_index}",
                        )

    def test_same_stored_edge_with_different_authority_or_evidence_conflicts(self):
        other_evidence = self.root / "other-evidence.json"
        other_evidence.write_text('{"verified":true}\n', encoding="utf-8")
        conflicts = (
            (
                {"parent_row": 1, "child_row": 2},
                {
                    "parent_row": 1,
                    "child_row": 2,
                    "authority": "explicit",
                },
            ),
            (
                {"parent_row": 1, "child_row": 2},
                {
                    "parent_row": 1,
                    "child_row": 2,
                    "evidence_path": str(other_evidence),
                },
            ),
        )
        for case_index, entries in enumerate(conflicts):
            for order_index, ordered in enumerate((entries, tuple(reversed(entries)))):
                with self.subTest(case=case_index, order=order_index):
                    with self.assertRaises(history_store.ImportConflict):
                        self._plan(
                            ["parent", "child"],
                            list(ordered),
                            f"conflicting-duplicate-{case_index}-{order_index}",
                        )

    def test_reversed_chain_edges_validate_and_commit_by_graph_order(self):
        plan = self._plan(
            ["chain a", "chain b", "chain c"],
            [
                {"parent_row": 1, "child_row": 2},
                {"parent_row": 2, "child_row": 3},
            ],
            "reversed-chain",
        )
        expected_order = [
            (edge["parent_candidate_id"], edge["child_candidate_id"])
            for edge in plan["edges"]
        ]
        plan["edges"].reverse()
        ordered = history_store._validate_import_plan_rows(plan)
        self.assertEqual(
            [
                (edge["parent_candidate_id"], edge["child_candidate_id"])
                for edge in ordered
            ],
            expected_order,
        )
        plan_bytes = history_store._json_bytes(history_store._plan_body(plan))
        plan_sha256 = history_store._sha(plan_bytes)
        plan_path = self.state_root / "import-plans" / f"{plan_sha256}.json"
        plan_path.write_bytes(plan_bytes)
        plan["plan_sha256"] = plan_sha256
        plan["plan_path"] = str(plan_path.resolve())

        history_store.commit_import_plan(self.conn, plan)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM lineage_edges").fetchone()[0],
            2,
        )

    def test_large_alias_and_component_sets_stay_linear_enough(self):
        alias_count = 5000
        alias_rows = [
            {"row_number": index, "canonical_story": "shared alias"}
            for index in range(1, alias_count + 1)
        ]
        started = time.monotonic()
        alias_roots, alias_edges = history_store._build_components(
            alias_rows, None
        )

        component_count = 2000
        rows = []
        mappings = []
        for index in range(component_count):
            parent = index * 2 + 1
            child = parent + 1
            rows.extend(
                (
                    {"row_number": parent, "canonical_story": f"parent {index}"},
                    {"row_number": child, "canonical_story": f"child {index}"},
                )
            )
            mappings.append(
                {
                    "parent_row": parent,
                    "child_row": child,
                    "evidence_path": "evidence.json",
                }
            )
        roots, edges = history_store._build_components(
            rows, {"mappings": mappings}
        )
        elapsed = time.monotonic() - started

        self.assertEqual(set(alias_roots.values()), {1})
        self.assertEqual(alias_edges, [])
        self.assertEqual(len(roots), component_count * 2)
        self.assertEqual(len(edges), component_count)
        self.assertLess(elapsed, 2.0)


if __name__ == "__main__":
    unittest.main()
