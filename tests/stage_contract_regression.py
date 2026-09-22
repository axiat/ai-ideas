#!/usr/bin/env python3
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import stage_contract


def assumption_markdown(marker, cracks):
    evidence = "\n".join(f"Crack Evidence: {value}" for value in cracks)
    return (
        f"Assumption-Removal Attempt: {marker}\n\n"
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


class StageContractRegression(unittest.TestCase):
    def test_generation_rejects_shared_content_before_first_candidate(self):
        markdown = assumption_markdown(
            "incomplete — I1; blocked by: Crack Evidence", ()
        )
        for shared in (
            "Shared experiment protocol: Use 200 paired trials per arm.\n",
            "# Shared protocol\nMatch memory bytes and action budgets.\n",
            "<!-- Shared budget: two A100-days. -->\n",
        ):
            for position in ("before-marker", "after-marker"):
                invalid = (
                    shared + markdown
                    if position == "before-marker"
                    else markdown.replace(
                        "\n\n## I1", "\n\n" + shared + "\n## I1", 1
                    )
                )
                with self.subTest(shared=shared, position=position):
                    with self.assertRaisesRegex(
                        stage_contract.StageError,
                        "generation markdown has content outside candidate sections",
                    ):
                        stage_contract.build_generation_tsv_from_markdown(invalid)

    def test_generation_accepts_blank_preamble_and_candidate_local_protocol(self):
        markdown = assumption_markdown(
            "incomplete — I1; blocked by: Crack Evidence", ()
        ).replace(
            "Minimal Falsification Experiment: Experiment.",
            "Minimal Falsification Experiment: Use 200 paired trials per arm.\n"
            "Match memory bytes and action budgets within this candidate.",
        )
        markdown = "\n \t\n" + markdown.replace(
            "\n\n## I1", "\n \t\n\n## I1", 1
        )
        self.assertEqual(
            stage_contract.build_generation_tsv_from_markdown(markdown),
            "I1\tStory.\tEvaluation\n",
        )

    def test_incomplete_attempt_accepts_zero_or_one_crack_row(self):
        for cracks in (
            (),
            ("Unavailable; requires downstream verification.",),
            ("https://example.com/crack",),
        ):
            with self.subTest(cracks=cracks):
                projected = stage_contract.build_generation_tsv_from_markdown(
                    assumption_markdown(
                        "incomplete — I1; blocked by: Crack Evidence", cracks
                    )
                )
                self.assertEqual(projected, "I1\tStory.\tEvaluation\n")

    def test_complete_attempt_rejects_zero_or_one_crack_row(self):
        for cracks in ((), ("Unavailable.",), ("https://example.com/crack",)):
            with self.subTest(cracks=cracks), self.assertRaisesRegex(
                stage_contract.StageError,
                "assumption-removal evidence is incomplete: I1",
            ):
                stage_contract.build_generation_tsv_from_markdown(
                    assumption_markdown("complete I1", cracks)
                )

    def test_incomplete_attempt_still_requires_assumption_fields(self):
        markdown = assumption_markdown(
            "incomplete — I1; blocked by: Crack Evidence", ("Unavailable.",)
        )
        for field in (
            "Assumption to Remove: Assumption.\n",
            "Why It Can Be Removed Now: Evidence.\n",
            "Forcing Constraint: Constraint.\n",
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                stage_contract.StageError,
                "assumption-removal evidence is incomplete: I1",
            ):
                stage_contract.build_generation_tsv_from_markdown(
                    markdown.replace(field, "")
                )

    def test_complete_evidence_requires_two_url_bearing_rows(self):
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
                    assumption_markdown("complete I1", cracks)
                )
        projected = stage_contract.build_generation_tsv_from_markdown(
            assumption_markdown(
                "complete I1", ("https://example.com/a", "https://EXAMPLE.COM/a")
            )
        )
        self.assertEqual(projected, "I1\tStory.\tEvaluation\n")


if __name__ == "__main__":
    unittest.main()
