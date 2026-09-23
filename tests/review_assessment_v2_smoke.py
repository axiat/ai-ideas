#!/usr/bin/env python3
"""Versioned review causes, frozen evidence, and non-expanding reentry."""
import copy
import inspect
import itertools
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lib import stage_contract, history_runtime
try:
    from lib import review_assessment
except ImportError:
    review_assessment = None

CANDIDATE = "The contribution is controlled memory intervention."
PRIOR = "Nearest Work: https://example.org/paper — Passive memory observation only."

def assessment(coverage="not-covered", code="design-invalid"):
    return {
        "coverage": coverage,
        "coverage_evidence": [
            {"source": "candidate", "quote": CANDIDATE},
            {"source": "prior-work", "quote": PRIOR},
        ],
        "causes": [] if code is None else [{
            "code": code, "evidence": [{"source": "candidate", "quote": CANDIDATE}]
        }],
    }

def review(value, verdict="reject", major=1):
    return (
        "# I1\n" + f"Verdict: {verdict}\nCRITICAL: 0\nMAJOR: {major}\n"
        "Headline: Bounded claim.\nOccupation: Closest work is supplied.\n"
        "Experiment: Current design misses the comparison.\nEstimand: Aligned.\n"
        "Payoff: Uncertain.\nFeasibility: Bounded.\nHistory: unavailable\n"
        + ("Assessment: " + json.dumps(value, separators=(",", ":")) + "\n" if value is not None else "")
        + "Reason: Current design cannot identify the claimed effect.\n"
    )

class AssessmentContract(unittest.TestCase):
    def parse(self, value, **kwargs):
        self.assertIn("review_output_version", inspect.signature(
            stage_contract.build_review_verdict_from_markdown).parameters,
            "v2 review parser has not been implemented")
        return stage_contract.build_review_verdict_from_markdown(
            review(value, **kwargs), "I1", review_output_version=2,
            candidate_markdown=CANDIDATE, prior_work=PRIOR)

    def test_v2_valid_reject_and_old_output_require_host_selected_version(self):
        self.assertEqual(self.parse(assessment()),
            "I1\treject\t1\tCurrent design cannot identify the claimed effect.\n")
        with self.assertRaises(stage_contract.StageError):
            self.parse(None)
        with self.assertRaises(stage_contract.StageError):
            stage_contract.build_review_verdict_from_markdown(review(assessment()), "I1")
        self.assertIn("\treject\t1\t", stage_contract.build_review_verdict_from_markdown(review(None), "I1"))

    def test_bad_refs_schema_and_contradictions_are_invalid_not_unknown(self):
        cases = []
        for key, value in (("coverage", "maybe"), ("extra", True)):
            item = assessment(); item[key] = value; cases.append(item)
        item = assessment(); del item["causes"]; cases.append(item)
        item = assessment(); item["coverage_evidence"][0]["quote"] = "invented"; cases.append(item)
        item = assessment(); item["causes"][0]["evidence"][0]["source"] = "other"; cases.append(item)
        item = assessment("covered", "contribution-covered")
        item["coverage_evidence"][1]["quote"] = "Passive memory observation only."; cases.append(item)
        cases.append(assessment("covered", "design-invalid"))
        item = assessment(); item["causes"].append(copy.deepcopy(item["causes"][0])); cases.append(item)
        for item in cases:
            with self.subTest(item=item), self.assertRaises(stage_contract.StageError):
                self.parse(item)
        with self.assertRaises(stage_contract.StageError):
            self.parse(assessment("covered", "contribution-covered"), verdict="accept-w-rev")
        with self.assertRaises(stage_contract.StageError):
            self.parse(assessment(), verdict="strong-accept", major=0)
        self.assertIn("\treject\t", self.parse({"coverage":"unknown", "coverage_evidence":[],
            "causes":[{"code":"unknown","evidence":[]}]}))
        self.assertIn("\tstrong-accept\t", self.parse(assessment(code=None), verdict="strong-accept", major=0))

    def test_runtime_and_projection_accept_the_same_nonempty_lines(self):
        text = "\n" + review(assessment())
        ballot = {"candidate_id": "I1", "verdict": "reject", "major_count": 1,
                  "reason": "Current design cannot identify the claimed effect."}
        self.assertEqual(history_runtime._validate_compact_review(text.encode(), ballot,
            review_output_version=2, candidate_markdown=CANDIDATE, prior_work=PRIOR)["Assessment"], assessment())

    def test_duplicate_json_keys_and_major_cap_fail(self):
        self.parse(assessment())
        text = review(assessment()).replace('"coverage":"not-covered"',
            '"coverage":"unknown","coverage":"not-covered"')
        with self.assertRaises(stage_contract.StageError):
            stage_contract.build_review_verdict_from_markdown(text, "I1", review_output_version=2,
                candidate_markdown=CANDIDATE, prior_work=PRIOR)
        with self.assertRaises(stage_contract.StageError):
            self.parse(assessment(code=None), verdict="strong-accept", major=2)

    def api(self):
        self.assertIsNotNone(review_assessment, "versioned assessment aggregation is missing")
        return review_assessment

    def test_coverage_truth_table_and_complementary_causes(self):
        api = self.api()
        cases = [(["unknown"] * 3,"unknown"),(["covered","unknown"],"covered"),
            (["covered","not-covered"],"disputed"),(["not-covered","unknown"],"not-covered"),
            (["covered","disputed"],"disputed")]
        for values, expected in cases:
            self.assertEqual(api.aggregate_coverage([{"coverage": x} for x in values]), expected)
        self.assertEqual(api.aggregate_coverage([assessment(code="design-invalid"),
            assessment(code="evidence-insufficient")]), "not-covered")
        for overlap in ("low","medium","high"):
            self.assertEqual(api.classify(final_rank=0, raw_min=0, downgraded=False,
                overlap=overlap, assessments=[assessment()]), "review-unresolved")
        self.assertEqual(api.classify(final_rank=0, raw_min=0, downgraded=False, overlap="low",
            assessments=[assessment("covered","contribution-covered")]), "novelty-dead")
        self.assertEqual(api.classify(final_rank=0, raw_min=0, downgraded=False, overlap="high",
            assessments=[assessment("covered","contribution-covered"),assessment(code=None)]), "review-unresolved")
        self.assertEqual(api.classify(final_rank=1, raw_min=1, downgraded=False, overlap="high",
            assessments=[assessment()]), "ceiling-limited")

    def test_reentry_is_subset_for_all_three_seat_votes_and_story_counts(self):
        api = self.api()
        for votes, overlap, gate, count in itertools.product(itertools.product(range(3),repeat=3),
                ("low","medium","high"),(True,False),(1,2,3)):
            raw = min(votes); downgraded = raw == 2 and not gate; final = 0 if downgraded else raw
            args = dict(final_rank=final,raw_min=raw,downgraded=downgraded,overlap=overlap)
            old = api.legacy_category(**args)
            new = api.classify(**args,assessments=[assessment()])
            eligible = api.eligible_for_reentry(**args, category=new, sa_votes=votes.count(2),
                story_count_after_append=count)
            old_eligible = final != 2 and old in {"design-fixable","evidence-incomplete"} and 2 in votes and count < 2
            self.assertFalse(eligible and not old_eligible, (votes,overlap,gate,count))
        self.assertTrue(api.eligible_for_reentry(final_rank=1,raw_min=1,downgraded=False,
            overlap="low",category="design-fixable",sa_votes=1,story_count_after_append=1))
        self.assertFalse(api.eligible_for_reentry(final_rank=0,raw_min=0,downgraded=False,
            overlap="low",category="evidence-incomplete",sa_votes=1,story_count_after_append=1))

if __name__ == "__main__":
    unittest.main()
