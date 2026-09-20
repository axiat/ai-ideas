#!/usr/bin/env python3
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import stage_contract


class StageContractRegression(unittest.TestCase):
    def test_complete_evidence_requires_two_url_bearing_rows(self):
        def markdown(cracks):
            evidence = "\n".join(f"Crack Evidence: {value}" for value in cracks)
            return (
                "Assumption-Removal Attempt: complete I1\n\n"
                "## I1\n"
                "One-Sentence Story: Story.\n"
                "Theme: Evaluation\n"
                "Form: remove-load-bearing-assumption\n"
                "Assumption to Remove: Assumption.\n"
                "Why It Can Be Removed Now: Evidence.\n"
                "Forcing Constraint: Constraint.\n"
                f"{evidence}\n"
                "Summary: Summary.\n"
                "Minimal Falsification Experiment: Experiment.\n"
                "Why It May Be Novel: Novelty.\n"
            )

        invalid_sets = (
            ("https://", "https:///missing-host"),
            ("https://example.com/a", "not-a-url"),
        )
        for cracks in invalid_sets:
            with self.subTest(cracks=cracks), self.assertRaisesRegex(
                stage_contract.StageError,
                "assumption-removal crack evidence lacks URLs: I1",
            ):
                stage_contract.build_generation_tsv_from_markdown(
                    markdown(cracks)
                )
        projected = stage_contract.build_generation_tsv_from_markdown(
            markdown(("https://example.com/a", "https://EXAMPLE.COM/a"))
        )
        self.assertEqual(projected, "I1\tStory.\tEvaluation\n")


if __name__ == "__main__":
    unittest.main()
