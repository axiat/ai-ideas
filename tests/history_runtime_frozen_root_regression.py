#!/usr/bin/env python3
import copy
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import history_runtime_smoke as smoke  # noqa: E402

history_runtime = smoke.history_runtime
canonical = smoke.canonical


class FrozenRootIntegrityRegression(smoke.RuntimeFixture):
    def _batch(self):
        ideas_tsv = self.root / "ideas.tsv"
        ideas_md = self.root / "ideas.md"
        ideas_tsv.write_text(
            "I1\tA bounded candidate.\tEvaluation and Diagnostics\n",
            encoding="utf-8",
        )
        ideas_md.write_text(
            "## I1\n"
            "One-Sentence Story: A bounded candidate.\n"
            "Theme: Evaluation and Diagnostics\n",
            encoding="utf-8",
        )
        return history_runtime.freeze_candidate_batch(
            ideas_tsv, ideas_md, self.root / "frozen"
        )

    def test_database_filename_symlink_is_rejected(self):
        outside = self.root / "outside.sqlite3"
        outside.touch()
        self.database.symlink_to(outside)

        with self.assertRaises(history_runtime.RuntimeContractError):
            self.startup()

        self.assertEqual(outside.read_bytes(), b"")

    def test_top_level_frozen_source_cannot_escape_artifact_root(self):
        batch = self._batch()
        escaped = self.root / "escaped-ideas.tsv"
        escaped.write_bytes(
            pathlib.Path(batch["ideas_tsv"]["path"]).read_bytes()
        )
        forged = copy.deepcopy(batch)
        forged["ideas_tsv"]["path"] = str(escaped)
        material = dict(forged)
        material.pop("batch_sha256")
        forged["batch_sha256"] = history_runtime.sha256(
            b"history-runtime-batch-v2\0" + canonical(material)
        )

        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.verify_frozen_batch(forged)


class ReviewFrozenDescriptorRegression(smoke.RuntimeFixture):
    _sealed_round = smoke.RoundCoordinatorContract._sealed_round
    _compared_round = smoke.RoundCoordinatorContract._compared_round
    _seal_review_plan = smoke.RoundCoordinatorContract._seal_review_plan
    _signed_capability = smoke.CapabilityContract._signed_capability

    @staticmethod
    def _rewrite(path, value):
        path = pathlib.Path(path)
        path.chmod(0o600)
        path.write_bytes(canonical(value))

    def test_nested_review_input_descriptor_cannot_escape_input_root(self):
        state = self._compared_round(stem="nested-root")
        authority = self.shadow_test_authority()
        sealed = self._seal_review_plan(
            state, stem="nested-root", authority=authority
        )
        plan = copy.deepcopy(sealed["plan"])
        target = next(
            item for item in plan["targets"]
            if item["planned_outcome"] == "review"
        )
        prior = target["prior_work"]
        escaped = self.root / "escaped-prior.md"
        escaped.write_bytes(pathlib.Path(prior["path"]).read_bytes())
        prior["path"] = str(escaped)
        plan["review_plan_sha256"] = history_runtime._review_plan_hash(plan)
        self._rewrite(sealed["plan_path"], plan)

        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.verify_round_review_plan(
                db_path=self.database,
                policy_path=self.policy_path,
                batch_path=state["batch"],
                review_plan_path=sealed["plan_path"],
                authority=authority,
            )


if __name__ == "__main__":
    unittest.main()
