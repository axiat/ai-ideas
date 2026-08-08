#!/usr/bin/env python3
import hashlib
import pathlib
import tempfile
import unittest

import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_projection as projection
from lib import history_retrieval as retrieval
from lib import history_store


COMPARATOR_ROLE_IDENTITY = "tests/fixtures/history-compare-role.md"
COMPARATOR_ROLE_BYTES = b"Classify every retained lineage.\n"
HEADER = b"date\tsource\ttheme\tidea\tverdict\treason\toverlap\tcategory\n"


def row(story, reason="missing strong baseline", category="design-fixable"):
    return (
        "2026-08-09\thunt\tWorld Models\t"
        + story
        + "\taccept-w-rev\t"
        + reason
        + "\tlow\t"
        + category
        + "\n"
    ).encode("utf-8")


def build_pack(*args, **kwargs):
    kwargs.setdefault("comparator_role_bytes", COMPARATOR_ROLE_BYTES)
    kwargs.setdefault("comparator_role_identity", COMPARATOR_ROLE_IDENTITY)
    return retrieval.build_pack(*args, **kwargs)


class HistoryRetrievalCorrectnessRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        (self.root / "ledger.instance-id").write_text(
            "retrieval-correctness\n", encoding="utf-8"
        )
        self.alpha_story = "shared semantic marker lineage alpha"
        self.beta_story = "shared semantic marker lineage beta"
        ledger = self.root / "ledger.tsv"
        ledger.write_bytes(
            HEADER
            + row(self.alpha_story, "uniquealpha mechanism deficiency")
            + row(self.beta_story, "ordinary feasibility deficiency")
            + row("shared semantic marker candidate gamma")
            + row("shared semantic marker candidate delta")
        )
        self.conn = history_store.connect(self.root / "history.sqlite3")
        history_store.init_schema(self.conn)
        history_store.import_tsv_epoch(self.conn, ledger)
        self.policy = projection.load_policy(
            ROOT / "history/retrieval-policy-v1.json"
        )

        history_store.append_rows(
            self.conn,
            [row(self.beta_story, "ordinary feasibility deficiency")],
            {"run_id": "beta-revision"},
        )
        history_store.append_rows(
            self.conn,
            [
                row(self.alpha_story, "uniquealpha mechanism deficiency")
                for _ in range(3)
            ],
            {"run_id": "alpha-revisions"},
        )
        self._add_current_edge(self.alpha_story, "alpha-edge")
        self._add_current_edge(self.beta_story, "beta-edge")
        projection.rebuild(self.conn, self.policy)

        self.alpha = self._current(self.alpha_story)
        self.beta = self._current(self.beta_story)
        self.query = {
            "candidate_id": "query-candidate",
            "story": "shared semantic marker",
            "theme": "World Models",
            "verdict": "accept-w-rev",
            "reason": "missing strong baseline",
            "category": "design-fixable",
        }

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _current(self, story):
        return self.conn.execute(
            """
            SELECT * FROM candidates
            WHERE story = ?
            ORDER BY source_sequence DESC, candidate_id
            LIMIT 1
            """,
            (story,),
        ).fetchone()

    def _add_current_edge(self, story, artifact_id):
        candidates = self.conn.execute(
            """
            SELECT * FROM candidates
            WHERE story = ?
            ORDER BY source_sequence, candidate_id
            """,
            (story,),
        ).fetchall()
        digest = hashlib.sha256(artifact_id.encode("utf-8")).hexdigest()
        self.conn.execute(
            """
            INSERT INTO artifacts(
              artifact_id, kind, state, sha256, byte_count, source_path,
              source_sequence, provenance_json, idempotency_key
            ) VALUES(?, 'lineage-evidence', 'installed', ?, 1, ?, 1, '{}', ?)
            """,
            (artifact_id, digest, "evidence/" + artifact_id, artifact_id),
        )
        history_store.add_lineage_edge(
            self.conn,
            candidates[0]["candidate_id"],
            candidates[-1]["candidate_id"],
            "evolved_from",
            artifact_id,
            "explicit",
        )

    def test_supplied_failure_pattern_facet_is_used(self):
        query = dict(
            self.query,
            story="unrelated story wording",
            facets={"failure_pattern": "uniquealpha"},
        )
        normalized = retrieval._normalize_query(query)
        self.assertEqual(
            retrieval._query_facets(normalized, "failure_pattern_search"),
            {"failure_pattern": "uniquealpha"},
        )
        self.assertNotIn(
            "failure_pattern",
            retrieval._query_facets(normalized, "duplicate_search"),
        )
        hits = retrieval._fts_channel(
            self.conn, normalized, "failure_pattern_search", 10
        )
        self.assertTrue(hits)
        self.assertEqual(
            {hit["lineage_id"] for hit in hits},
            {self.alpha["lineage_id"]},
        )

    def test_failure_query_requires_all_structured_fields(self):
        for field in ("verdict", "reason", "category"):
            query = dict(self.query)
            query.pop(field)
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    retrieval.RetrievalError,
                    "requires verdict, reason, and category",
                ):
                    build_pack(
                        self.conn,
                        query,
                        "failure_pattern_search",
                        self.policy,
                    )

    def test_lineage_ranks_follow_fused_seed_order(self):
        lexical = sorted(
            (self.alpha, self.beta), key=lambda item: item["lineage_id"]
        )
        requested = list(reversed(lexical))
        results = retrieval._lineage_channel(
            self.conn,
            [item["candidate_id"] for item in requested],
            10,
        )
        self.assertEqual(
            [item["lineage_id"] for item in results],
            [item["lineage_id"] for item in requested],
        )
        self.assertEqual([item["rank"] for item in results], [1, 2])

    def test_lineage_expansion_is_fair_across_requested_lineages(self):
        results = retrieval._expansion_channel(
            self.conn,
            {
                "lineage_ids": [
                    self.alpha["lineage_id"],
                    self.beta["lineage_id"],
                ]
            },
            2,
        )
        self.assertEqual(
            [item["lineage_id"] for item in results],
            [self.alpha["lineage_id"], self.beta["lineage_id"]],
        )

    def test_semantic_facet_order_shares_global_channel_depth(self):
        query = dict(
            self.query,
            facets={
                "claimed_delta": "shared semantic marker",
                "problem_estimand": "shared semantic marker",
            },
        )
        expected_facets = [
            "problem_estimand",
            "claimed_delta",
            "problem_estimand",
        ]
        for channel in (retrieval._fts_channel, retrieval._dense_channel):
            with self.subTest(channel=channel.__name__):
                results = channel(
                    self.conn, query, "duplicate_search", 3
                )
                self.assertEqual(len(results), 3)
                self.assertEqual(
                    [item["facet"] for item in results], expected_facets
                )
                self.assertEqual(
                    [item["rank"] for item in results], [1, 1, 2]
                )


if __name__ == "__main__":
    unittest.main()
