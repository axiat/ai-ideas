#!/usr/bin/env python3
import copy
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

canonical = smoke.canonical
history_runtime = smoke.history_runtime


class ReviewPlanBindingRegression(smoke.RuntimeFixture):
    _sealed_round = smoke.RoundCoordinatorContract._sealed_round
    _compared_round = smoke.RoundCoordinatorContract._compared_round
    _seal_review_plan = smoke.RoundCoordinatorContract._seal_review_plan
    _enforcement_round = smoke.RoundCoordinatorContract._enforcement_round
    _signed_capability = smoke.CapabilityContract._signed_capability

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

    @staticmethod
    def _rewrite(path, value):
        path = pathlib.Path(path)
        path.chmod(0o600)
        path.write_bytes(canonical(value))

    @staticmethod
    def _rehash_comparison(observation):
        material = dict(observation)
        material.pop("observation_sha256", None)
        observation["observation_sha256"] = history_runtime.sha256(
            b"history-runtime-observation-v1\0" + canonical(material)
        )

    @staticmethod
    def _rehash_comparison_index(index):
        material = dict(index)
        material.pop("comparison_index_sha256", None)
        domain = (
            b"history-runtime-comparison-index-v2\0"
            if index.get("schema_version") == 2
            else b"history-runtime-comparison-index-v1\0"
        )
        index["comparison_index_sha256"] = history_runtime.sha256(
            domain + canonical(material)
        )

    @staticmethod
    def _rehash_review_plan(plan):
        plan["review_plan_sha256"] = history_runtime._review_plan_hash(plan)

    def test_tampered_observation_cannot_change_review_to_abstention(self):
        state = self._enforcement_round(contained=True)
        index_path = state["artifact_root"] / "comparison-index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        indexed = index["targets"][0]
        observation_path = pathlib.Path(indexed["observation_path"])
        observation = json.loads(observation_path.read_text(encoding="utf-8"))
        for item in observation["observations"]:
            item["status"] = "partial"
            item["attempts"][-1]["status"] = "partial"
        self._rehash_comparison(observation)
        self._rewrite(observation_path, observation)
        indexed["observation_sha256"] = observation["observation_sha256"]
        indexed["statuses"] = ["partial", "partial"]
        self._rehash_comparison_index(index)
        self._rewrite(index_path, index)

        with self.assertRaises(history_runtime.RuntimeContractError):
            self._seal_review_plan(
                state,
                stem="tampered-observation",
                authority=state["authority"],
            )

    def test_forged_candidate_subject_is_rejected(self):
        state = self._compared_round(stem="forged-candidate")
        authority = self.shadow_test_authority()
        sealed = self._seal_review_plan(
            state,
            stem="forged-candidate",
            authority=authority,
        )
        plan = copy.deepcopy(sealed["plan"])
        forged = dict(
            json.loads(
                pathlib.Path(
                    plan["targets"][0]["candidate_artifact"]["path"]
                ).read_text(encoding="utf-8")
            )
        )
        forged["story"] = "A forged review subject."
        forged["content_sha256"] = history_runtime.candidate_content_sha256(
            forged
        )
        forged_path = self.root / "forged-candidate.json"
        forged_path.write_bytes(canonical(forged))
        plan["targets"][0]["candidate_artifact"] = {
            "path": str(forged_path),
            "sha256": history_runtime.sha256(forged_path.read_bytes()),
        }
        self._rehash_review_plan(plan)
        self._rewrite(sealed["plan_path"], plan)

        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.verify_round_review_plan(
                db_path=self.database,
                policy_path=self.policy_path,
                batch_path=state["batch"],
                review_plan_path=sealed["plan_path"],
                authority=authority,
            )

    def test_forged_prior_work_metrics_are_rejected(self):
        state = self._compared_round(stem="forged-prior")
        authority = self.shadow_test_authority()
        sealed = self._seal_review_plan(
            state,
            stem="forged-prior",
            authority=authority,
        )
        plan = copy.deepcopy(sealed["plan"])
        prior = plan["targets"][0]["prior_work"]
        forged_raw = b"## I2\nPapers Read: 500\nOverlap: none\n"
        prior_path = pathlib.Path(prior["path"])
        prior_path.chmod(0o600)
        prior_path.write_bytes(forged_raw)
        prior["sha256"] = history_runtime.sha256(forged_raw)
        prior["byte_count"] = len(forged_raw)
        self._rehash_review_plan(plan)
        self._rewrite(sealed["plan_path"], plan)

        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.verify_round_review_plan(
                db_path=self.database,
                policy_path=self.policy_path,
                batch_path=state["batch"],
                review_plan_path=sealed["plan_path"],
                authority=authority,
            )

    def test_malformed_comparison_items_raise_contract_error(self):
        state = self._compared_round(stem="malformed-observation")
        index_path = state["observation_root"] / "comparison-index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        indexed = index["targets"][0]
        observation_path = pathlib.Path(indexed["observation_path"])
        observation = json.loads(observation_path.read_text(encoding="utf-8"))
        observation["observations"] = [None]
        self._rehash_comparison(observation)
        self._rewrite(observation_path, observation)
        indexed["observation_sha256"] = observation["observation_sha256"]
        indexed["statuses"] = [None]
        self._rehash_comparison_index(index)
        self._rewrite(index_path, index)

        with self.assertRaises(history_runtime.RuntimeContractError):
            self._seal_review_plan(
                state,
                stem="malformed-observation",
                authority=self.shadow_test_authority(),
            )

    def test_shadow_review_plan_requires_matching_authority(self):
        state = self._compared_round(stem="shadow-authority")
        with self.assertRaises(history_runtime.RuntimeContractError):
            self._seal_review_plan(state, stem="shadow-authority-missing")
        forged_policy = copy.deepcopy(self.policy)
        forged_policy["retrieval_policy_version"] += "-forged"
        forged = history_runtime.validate_runtime_mode(forged_policy)
        with self.assertRaises(history_runtime.RuntimeContractError):
            self._seal_review_plan(
                state,
                stem="shadow-authority-forged",
                authority=forged,
            )


if __name__ == "__main__":
    unittest.main()
