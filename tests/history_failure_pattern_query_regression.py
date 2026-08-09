#!/usr/bin/env python3
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lib import history_retrieval, history_runtime


class FailurePatternQueryRegression(unittest.TestCase):
    def test_explicit_failure_facet_is_sufficient(self):
        query = {"facets": {"failure_pattern": "dynamic scene change"}}
        history_retrieval._validate_query_for_intent(
            query, "failure_pattern_search"
        )

    def test_generated_candidate_derives_failure_facet(self):
        candidate = {
            "candidate_id": "I1",
            "story": "Repair memory",
            "theme": "World Models",
            "candidate_markdown": (
                "## I1\n"
                "Target Failure: dynamic scene change\n"
                "Form: remove-load-bearing-assumption\n"
                "Minimal Falsification Experiment: compare stale and repaired memory\n"
            ),
        }
        candidate["content_sha256"] = history_runtime.candidate_content_sha256(
            candidate
        )
        query = history_runtime._retrieval_query(
            candidate, "failure_pattern_search"
        )
        self.assertIn("dynamic scene change", query["facets"]["failure_pattern"])
        history_retrieval._validate_query_for_intent(
            query, "failure_pattern_search"
        )


if __name__ == "__main__":
    unittest.main()
