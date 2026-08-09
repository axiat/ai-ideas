#!/usr/bin/env python3
import copy
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import history_runtime_smoke as smoke  # noqa: E402
from lib import history_stage  # noqa: E402
from lib import portable_agent  # noqa: E402

history_runtime = smoke.history_runtime


class ResumeBindingRegression(smoke.RuntimeFixture):
    _signed_capability = smoke.CapabilityContract._signed_capability
    _sealed_round = smoke.RoundCoordinatorContract._sealed_round
    _compared_round = smoke.RoundCoordinatorContract._compared_round
    _portable_profile = staticmethod(
        smoke.RoundCoordinatorContract._portable_profile
    )

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
        reserved = portable_agent._reserved
        reserved_patcher = mock.patch.object(
            portable_agent,
            "_reserved",
            side_effect=lambda path: (
                False
                if pathlib.Path(path).name == "history-compare.md"
                else reserved(path)
            ),
        )
        reserved_patcher.start()
        self.addCleanup(reserved_patcher.stop)

    def _seal_resume(self, state, stem, authority):
        prior = self.root / f"{stem}-prior.md"
        prior.write_text(
            "## I2\nPapers Read: 5\nOverlap: low\n",
            encoding="utf-8",
        )
        path = self.root / f"{stem}-resume.json"
        sealed = history_runtime.seal_resume_state(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            selection_path=state["selection"],
            artifact_root=state["observation_root"],
            comparison_index_path=(
                state["observation_root"] / "comparison-index.json"
            ),
            prior_work_path=prior,
            output_path=path,
            authority=authority,
        )
        return path, sealed

    def test_shadow_resume_and_attempt_require_policy_authority(self):
        state = self._compared_round(stem="shadow-authority")
        prior = self.root / "shadow-authority-prior.md"
        prior.write_text("## I2\nPapers Read: 5\n", encoding="utf-8")
        resume_path = self.root / "shadow-authority-resume.json"
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.seal_resume_state(
                db_path=self.database,
                policy_path=self.policy_path,
                batch_path=state["batch"],
                selection_path=state["selection"],
                artifact_root=state["observation_root"],
                comparison_index_path=(
                    state["observation_root"] / "comparison-index.json"
                ),
                prior_work_path=prior,
                output_path=resume_path,
            )
        self.assertFalse(resume_path.exists())

        authority = self.shadow_test_authority()
        resume_path, _ = self._seal_resume(
            state, "shadow-authority-valid", authority
        )
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.validate_resume_state(resume_path)
        attempt = self.root / "shadow-attempt.json"
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.seal_resume_attempt(
                resume_path=resume_path,
                run_id="run-2",
                resumed_from_run_id="run-1",
                output_path=attempt,
            )
        self.assertFalse(attempt.exists())
        history_runtime.seal_resume_attempt(
            resume_path=resume_path,
            run_id="run-2",
            resumed_from_run_id="run-1",
            output_path=attempt,
            authority=authority,
        )
        self.assertTrue(attempt.is_file())

    def test_resume_rejects_copied_batch_manifest_path(self):
        state = self._compared_round(stem="copied-batch")
        copied = self.root / "copied-batch.json"
        copied.write_bytes(pathlib.Path(state["batch"]).read_bytes())
        prior = self.root / "copied-batch-prior.md"
        prior.write_text("## I2\nPapers Read: 5\n", encoding="utf-8")
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.seal_resume_state(
                db_path=self.database,
                policy_path=self.policy_path,
                batch_path=copied,
                selection_path=state["selection"],
                artifact_root=state["observation_root"],
                comparison_index_path=(
                    state["observation_root"] / "comparison-index.json"
                ),
                prior_work_path=prior,
                output_path=self.root / "copied-resume.json",
                authority=self.shadow_test_authority(),
            )

    def test_resume_reconstructs_current_prompt_and_generation_manifest(self):
        state = self._compared_round(stem="current-prompt")
        authority = self.shadow_test_authority()
        resume_path, _ = self._seal_resume(
            state, "current-prompt", authority
        )
        self.assertTrue(
            history_runtime.validate_resume_state(
                resume_path, authority=authority
            )
        )
        with mock.patch.object(
            history_runtime,
            "_portable_serialized_prompt",
            return_value="changed current serializer output",
        ):
            with self.assertRaises(history_runtime.RuntimeContractError):
                history_runtime.validate_resume_state(
                    resume_path, authority=authority
                )

        original = history_runtime._current_generation_binding

        def drifted_generation(conn, policy):
            value = copy.deepcopy(original(conn, policy))
            value["generation_manifest_sha256"] = "0" * 64
            return value

        with mock.patch.object(
            history_runtime,
            "_current_generation_binding",
            side_effect=drifted_generation,
        ):
            with self.assertRaises(history_runtime.RuntimeContractError):
                history_runtime.validate_resume_state(
                    resume_path, authority=authority
                )

    def test_portable_resume_binds_outputs_and_receipts(self):
        state = self._sealed_round(stem="portable-resume")
        history_runtime.compare_frozen_targets(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            artifact_root=state["observation_root"],
            selection_path=state["selection"],
            executor="portable-v2",
            portable_request_profile=self._portable_profile(),
        )
        authority = self.shadow_test_authority()
        resume_path, sealed = self._seal_resume(
            state, "portable-resume", authority
        )
        self.assertEqual(sealed["schema_version"], 2)
        self.assertTrue(sealed["bindings"])
        for binding in sealed["bindings"]:
            self.assertRegex(
                binding["comparison_sha256"], r"^[0-9a-f]{64}$"
            )
            self.assertRegex(
                binding["comparison_receipt_sha256"],
                r"^[0-9a-f]{64}$",
            )
        self.assertTrue(
            history_runtime.validate_resume_state(
                resume_path, authority=authority
            )
        )


if __name__ == "__main__":
    unittest.main()
