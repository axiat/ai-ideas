#!/usr/bin/env python3
import json
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import history_runtime_smoke as smoke  # noqa: E402
from lib import history_stage  # noqa: E402

history_runtime = smoke.history_runtime
canonical = smoke.canonical


class RuntimeFinalIntegrityRegression(smoke.RuntimeFixture):
    _signed_capability = smoke.CapabilityContract._signed_capability
    _sealed_round = smoke.RoundCoordinatorContract._sealed_round
    _compared_round = smoke.RoundCoordinatorContract._compared_round
    _seal_review_plan = smoke.RoundCoordinatorContract._seal_review_plan
    _review_chain = smoke.RoundCoordinatorContract._review_chain
    _enforcement_round = smoke.RoundCoordinatorContract._enforcement_round

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(
            history_stage,
            "build_darwin_launch",
            side_effect=lambda _profile, _mirror, command, *args, **kwargs: (
                command,
                "(version 1)",
            ),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _report(self, state, stem, authority):
        artifact_root = state.get(
            "observation_root", state.get("artifact_root")
        )
        research_root = self.root / f"{stem}-research"
        history_runtime.materialize_research_views(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            selection_path=state["selection"],
            comparison_index_path=(
                artifact_root / "comparison-index.json"
            ),
            artifact_root=artifact_root,
            output_root=research_root,
            authority=authority,
        )
        chain = self._review_chain(
            state, stem=stem, authority=authority
        )
        report_root = self.root / f"{stem}-report"
        report = history_runtime.materialize_report_views(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            research_view_path=research_root / "research-view.json",
            review_plan_path=chain["plan_path"],
            review_index_path=chain["index_path"],
            aggregation_path=chain["aggregation_path"],
            output_root=report_root,
            round_number=1,
            authority=authority,
        )
        return report, report_root

    def test_report_includes_prescreen_kill(self):
        state = self._compared_round(
            selected=("I2",),
            killed=("I1",),
            stem="report-kill",
        )
        report, root = self._report(
            state, "report-kill", self.shadow_test_authority()
        )
        rejects = (root / "rejects.tsv").read_text(encoding="utf-8")
        self.assertIn("I1\t", rejects)
        self.assertIn("Prescreen direct hit:", rejects)
        self.assertEqual(report["rejected_count"], 1)

    def test_report_includes_history_abstention(self):
        state = self._enforcement_round(
            comparison_status="uncertain", contained=True
        )
        report, root = self._report(
            state, "report-abstain", state["authority"]
        )
        rejects = (root / "rejects.tsv").read_text(encoding="utf-8")
        self.assertIn("I1\t", rejects)
        self.assertIn(
            "History abstention: uncertain,uncertain", rejects
        )
        self.assertEqual(report["rejected_count"], 1)

    def test_malformed_evolution_declarations_fail_closed(self):
        ideas_tsv = self.root / "malformed-evolution.tsv"
        ideas_tsv.write_text(
            "I1\tA malformed evolution.\tEvaluation and Diagnostics\n",
            encoding="utf-8",
        )
        malformed = (
            "Evolved from parent-1",
            "Evolved from:",
            "  Recheck of: parent-1",
        )
        for index, declaration in enumerate(malformed):
            markdown = self.root / f"malformed-evolution-{index}.md"
            markdown.write_text(
                "## I1\n"
                "One-Sentence Story: A malformed evolution.\n"
                "Theme: Evaluation and Diagnostics\n"
                f"{declaration}\n",
                encoding="utf-8",
            )
            with self.subTest(declaration=declaration):
                with self.assertRaises(history_runtime.RuntimeContractError):
                    history_runtime.freeze_candidate_batch(
                        ideas_tsv,
                        markdown,
                        self.root / f"malformed-frozen-{index}",
                    )

    def test_summary_publication_recomputes_child_observation_hash(self):
        state = self._enforcement_round(contained=True)
        child_path = (
            state["artifact_root"]
            / "I1"
            / "comparison-observation.json"
        )
        child = json.loads(child_path.read_text(encoding="utf-8"))
        child["observations"][0]["status"] = "uncertain"
        child_path.chmod(0o600)
        child_path.write_bytes(canonical(child))

        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.publish_candidate_summary(
                db_path=self.database,
                policy_path=self.policy_path,
                batch_path=state["batch"],
                artifact_root=state["artifact_root"],
                candidate_id="I1",
                output_path=self.root / "tampered-summary.json",
                authority=state["authority"],
            )

    def test_round_selection_recomputes_child_observation_hash(self):
        state = self._sealed_round(stem="child-observation")
        round_path = (
            state["observation_root"] / "round-observation.json"
        )
        round_observation = json.loads(
            round_path.read_text(encoding="utf-8")
        )
        child_path = pathlib.Path(
            round_observation["candidates"][0]["observation_path"]
        )
        child = json.loads(child_path.read_text(encoding="utf-8"))
        child["observations"][0]["retrieval_status"] = "backend_failed"
        child["observations"][0]["status"] = "backend_failed"
        child["observations"][0]["attempts"][0]["status"] = (
            "backend_failed"
        )
        child_path.chmod(0o600)
        child_path.write_bytes(canonical(child))

        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.verify_round_selection(state["selection"])


if __name__ == "__main__":
    unittest.main()
