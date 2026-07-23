#!/usr/bin/env python3
import json
import pathlib
import tempfile
import unittest

import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_projection as projection
from lib import history_store
from lib import history_budget


HEADER = b"date\tsource\ttheme\tidea\tverdict\treason\toverlap\tcategory\n"


def row(story, theme="World Models", reason="missing strong baseline"):
    return (
        "2026-07-23\thunt\t" + theme + "\t" + story
        + "\taccept-w-rev\t" + reason + "\tlow\tdesign-fixable\n"
    ).encode("utf-8")


class HistoryProjectionSmoke(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        (self.root / "ledger.instance-id").write_text("projection-test\n", encoding="utf-8")
        self.ledger = self.root / "ledger.tsv"
        self.ledger.write_bytes(
            HEADER
            + row("confidence gated world model reduces unsafe rollout")
            + row("causal VLA probe isolates actuator ambiguity", "VLA", "weak prior work search")
        )
        self.conn = history_store.connect(self.root / "history.sqlite3")
        history_store.init_schema(self.conn)
        history_store.import_tsv_epoch(self.conn, self.ledger)
        self.policy = projection.load_policy(ROOT / "history/retrieval-policy-v1.json")
        self.candidate_id = self.conn.execute(
            "SELECT candidate_id FROM candidates ORDER BY source_sequence LIMIT 1"
        ).fetchone()[0]

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _rank(self, query):
        return projection.search(self.conn, query, self.policy)["candidate_ids"]

    def _all_searchable_ids(self):
        return projection.searchable_candidate_ids(self.conn)

    def _clean_rebuild_and_rank(self, query):
        projection.drop_rebuildable_projections(self.conn)
        projection.rebuild(self.conn, self.policy)
        return self._rank(query)

    def test_noop_rebuild_embeds_zero_facets(self):
        first = projection.rebuild(self.conn, self.policy)
        second = projection.rebuild(self.conn, self.policy)
        self.assertGreater(first["embedded_facets"], 0)
        self.assertEqual(second["embedded_facets"], 0)

    def test_incremental_matches_clean_rebuild(self):
        projection.rebuild(self.conn, self.policy)
        incremental = self._rank("confidence gated world model")
        clean = self._clean_rebuild_and_rank("confidence gated world model")
        self.assertEqual(incremental, clean)

    def test_change_and_delete_update_only_affected_facets(self):
        projection.rebuild(self.conn, self.policy)
        changed = projection.update_candidate_facets(
            self.conn, self.candidate_id, {"mechanism": "new mechanism"}
        )
        self.assertEqual(changed["queued_facets"], 1)
        result = projection.rebuild(self.conn, self.policy)
        self.assertEqual(result["embedded_facets"], 1)
        projection.remove_candidate_from_search(self.conn, self.candidate_id)
        self.assertNotIn(self.candidate_id, self._all_searchable_ids())
        self.assertIsNotNone(history_store.get_candidate(self.conn, self.candidate_id))

    def test_generation_brief_is_bounded_and_has_one_parent(self):
        projection.rebuild(self.conn, self.policy)
        brief = projection.build_generation_brief(self.conn, self.policy)
        self.assertLessEqual(len(brief.get("parents", [])), 1)
        self.assertNotIn("ledger_rows", brief)
        self.assertLessEqual(brief["estimated_tokens"], self.policy["max_retrieval_tokens"])
        self.assertEqual(brief["source_watermark"], 2)
        self.assertEqual(brief["retrieval_policy_version"], "retrieval-policy-v1")
        json.dumps(brief, sort_keys=True)

    def test_failure_codes_are_deterministic(self):
        self.assertEqual(projection.failure_code("reject", "novelty-dead", "direct hit"), "direct-hit")
        self.assertEqual(projection.failure_code("accept-w-rev", "design-fixable", "insufficient statistical power"), "statistical-power")
        self.assertEqual(projection.failure_code("accept-w-rev", "design-fixable", "unmapped text"), "other")

    def test_exact_lookup_uses_canonical_story(self):
        projection.rebuild(self.conn, self.policy)
        self.assertEqual(
            projection.exact_lookup(
                self.conn, "  confidence gated world model reduces unsafe rollout ", 50
            ),
            [self.candidate_id],
        )

    def test_round_artifact_maps_summary_and_falsification_fields(self):
        facets = projection.facets_from_round_artifact(
            "A bounded proposition", "World Models",
            "Summary: exposes a calibrated mechanism\n"
            "Minimal Falsification Experiment: compare against a strong baseline\n",
        )
        self.assertEqual(facets["mechanism"], "exposes a calibrated mechanism")
        self.assertEqual(
            facets["evaluation_expected_signal"], "compare against a strong baseline"
        )
        changed = projection.update_candidate_from_round_artifact(
            self.conn, self.candidate_id,
            "Summary: exposes a calibrated mechanism\n"
            "Minimal Falsification Experiment: compare against a strong baseline\n",
        )
        self.assertEqual(changed["queued_facets"], 2)

    def test_full_generation_brief_is_preflighted_as_mounted_bytes(self):
        projection.rebuild(self.conn, self.policy)
        brief = projection.build_generation_brief(self.conn, self.policy)
        brief_bytes = (json.dumps(brief, sort_keys=True, separators=(",", ":")) + "\n").encode()
        invocation = history_budget.serialize_stage_invocation(
            stage="generate", adapter_version="history-stage-v1",
            fixed_instructions="Generate candidates.",
            mounted_inputs={"generation_brief.json": brief_bytes}, candidate=None,
            retrieval_payload=None, receipts=[], tool_schemas=[],
            messages=[{"role": "user", "content": "Generate candidates."}],
        )
        receipt = history_budget.preflight_stage_invocation(invocation, self.policy)
        self.assertTrue(receipt["fits"])
        self.assertEqual(receipt["serialized_sha256"], __import__("hashlib").sha256(invocation).hexdigest())


if __name__ == "__main__":
    unittest.main()
