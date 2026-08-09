#!/usr/bin/env python3
"""Correctness regressions for portable stage preflight and publication."""

import json
import os
import pathlib
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import portable_stage
from lib import provider_adapters


REGISTRY = ROOT / "history/provider-adapters-v1.json"
FAKE = ROOT / "tests/fake_portable_stage_provider.py"


class PortableStageCorrectnessRegression(unittest.TestCase):
    @staticmethod
    def _intent():
        return provider_adapters._resolve_command_intent_for_test(
            provider_adapters.load_registry(REGISTRY),
            "hunt",
            "claude",
            model="MODEL",
            reasoning="high",
            max_output_tokens=3072,
            executable_lookup=lambda _: str(FAKE),
        )

    def _prepare(self, root):
        inputs = root / "inputs"
        inputs.mkdir(parents=True)
        brief = inputs / "generation_brief.json"
        policy = inputs / "generation_policy.md"
        brief.write_text('{"brief":"bounded"}\n', encoding="utf-8")
        policy.write_text("bounded policy\n", encoding="utf-8")
        return portable_stage.prepare_stage(
            self._intent(),
            stage="generate",
            seat_id="generate-correctness-seat",
            serialized_prompt='{"schema_version":1,"stage":"generate"}\n',
            input_paths={
                "generation_brief.json": brief,
                "generation_policy.md": policy,
            },
            output_root=root / "published",
            state_root=root / "portable-state",
        )

    @staticmethod
    def _budget_with_render(argv, environment_delta=None):
        intent = types.SimpleNamespace(provider="grok")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            provider_adapters,
            "render_command",
            return_value=(argv, environment_delta or {}),
        ):
            return portable_stage._rendered_exec_budget(
                intent,
                "request",
                {"type": "object"},
                pathlib.Path(directory) / "state",
            )

    def test_exec_budget_rejects_individual_argument(self):
        argument = "x" * portable_stage._EXEC_SINGLE_STRING_MAX_BYTES
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(portable_stage.PortableStageError) as caught:
                self._budget_with_render([argument])
        self.assertEqual(caught.exception.code, "exec_argument_too_large")

    def test_exec_budget_rejects_individual_environment_entry(self):
        value = "x" * portable_stage._EXEC_SINGLE_STRING_MAX_BYTES
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(portable_stage.PortableStageError) as caught:
                self._budget_with_render(["provider"], {"OVERSIZED": value})
        self.assertEqual(caught.exception.code, "exec_environment_too_large")

    def test_exec_budget_rejects_aggregate_argv_and_environment(self):
        arguments = ["x" * 100_000, "y" * 100_000, "z" * 100_000]
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(portable_stage.PortableStageError) as caught:
                self._budget_with_render(arguments)
        self.assertEqual(caught.exception.code, "exec_aggregate_too_large")

    def test_preflight_binds_rendered_exec_budget_and_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            prepared = self._prepare(pathlib.Path(directory))
            preflight = json.loads(
                pathlib.Path(prepared["preflight_path"]).read_text(
                    encoding="utf-8"
                )
            )
            budget = preflight["exec_budget"]
            self.assertEqual(dict(prepared["exec_budget"]), budget)
            self.assertEqual(
                budget["conservative_total_bytes"],
                budget["argv_bytes"]
                + budget["environment_bytes"]
                + budget["pointer_bytes"]
                + budget["reserve_bytes"],
            )
            self.assertLessEqual(
                budget["conservative_total_bytes"],
                budget["aggregate_cap_bytes"],
            )
            for name in (
                "rendered_argv_sha256",
                "environment_delta_sha256",
            ):
                self.assertRegex(budget[name], r"^[0-9a-f]{64}$")

    def test_preflight_writer_and_reader_share_one_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(root / "reader")
            preflight_size = pathlib.Path(prepared["preflight_path"]).stat().st_size
            with mock.patch.object(
                portable_stage, "PREFLIGHT_MAX_BYTES", preflight_size - 1
            ):
                with self.assertRaises(portable_stage.PortableStageError) as caught:
                    portable_stage.run_stage(prepared, timeout_seconds=1)
            self.assertEqual(caught.exception.code, "oversize")

            writer_root = root / "writer"
            with mock.patch.object(portable_stage, "PREFLIGHT_MAX_BYTES", 1):
                with self.assertRaises(portable_stage.PortableStageError) as caught:
                    self._prepare(writer_root)
            self.assertEqual(caught.exception.code, "preflight_too_large")
            self.assertFalse((writer_root / "portable-state").exists())
            self.assertFalse((writer_root / "published").exists())

    def test_symlink_ancestor_is_rejected_before_root_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            real = root / "real"
            real.mkdir()
            alias = root / "alias"
            alias.symlink_to(real, target_is_directory=True)
            inputs = root / "inputs"
            inputs.mkdir()
            brief = inputs / "generation_brief.json"
            policy = inputs / "generation_policy.md"
            brief.write_text('{}\n', encoding="utf-8")
            policy.write_text("bounded\n", encoding="utf-8")
            with self.assertRaises(portable_stage.PortableStageError) as caught:
                portable_stage.prepare_stage(
                    self._intent(),
                    stage="generate",
                    seat_id="symlink-seat",
                    serialized_prompt='{"stage":"generate"}\n',
                    input_paths={
                        "generation_brief.json": brief,
                        "generation_policy.md": policy,
                    },
                    output_root=alias / "published",
                    state_root=root / "state",
                )
            self.assertEqual(caught.exception.code, "unsafe_output_root")
            self.assertFalse((real / "published").exists())
            self.assertFalse((root / "state").exists())

    def test_root_overlap_uses_canonical_temporary_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inputs = root / "inputs"
            inputs.mkdir()
            brief = inputs / "generation_brief.json"
            policy = inputs / "generation_policy.md"
            brief.write_text('{}\n', encoding="utf-8")
            policy.write_text("bounded\n", encoding="utf-8")
            output = root / "published"
            state = root.resolve() / "published" / "state"
            with self.assertRaises(portable_stage.PortableStageError) as caught:
                portable_stage.prepare_stage(
                    self._intent(),
                    stage="generate",
                    seat_id="overlap-seat",
                    serialized_prompt='{"stage":"generate"}\n',
                    input_paths={
                        "generation_brief.json": brief,
                        "generation_policy.md": policy,
                    },
                    output_root=output,
                    state_root=state,
                )
            self.assertEqual(caught.exception.code, "overlapping_state_roots")
            self.assertFalse(output.exists())

    def test_sibling_host_and_model_validation_codes_are_contract_errors(self):
        codes = (
            "invalid_history_comparison",
            "noncanonical_history_comparison",
            "invalid_retrieval_pack",
            "noncanonical_retrieval_pack",
            "invalid_review_candidate",
            "noncanonical_review_candidate",
            "invalid_failure_distillation",
            "noncanonical_failure_distillation",
            "invalid_failure_batch",
            "noncanonical_failure_batch",
        )
        for code in codes:
            with self.subTest(code=code):
                self.assertEqual(
                    portable_stage.PortableStageError(code).error_class,
                    "contract",
                )

    def test_exception_cleanup_preserves_concurrent_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            prepared = self._prepare(pathlib.Path(directory))
            output_root = pathlib.Path(prepared["output_root"])
            completion_path = pathlib.Path(prepared["completion_path"])
            outputs = {
                name: b"ours\n" for name in prepared["output_paths"]
            }
            attempt = {
                "provider": prepared["provider"],
                "execution_request_profile_hash": prepared[
                    "execution_request_profile_hash"
                ],
                "max_output_tokens": prepared["max_output_tokens"],
                "output_token_cap_binding": prepared[
                    "output_token_cap_binding"
                ],
                "output_token_cap_semantics": prepared[
                    "output_token_cap_semantics"
                ],
                "model_envelope_sha256": "a" * 64,
                "raw": b"unused\n",
            }
            fsync_calls = 0

            def replace_with_winner(_path):
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls != 3:
                    return
                shutil.rmtree(output_root)
                output_root.mkdir()
                (output_root / "winner.txt").write_text(
                    "winner\n", encoding="utf-8"
                )
                completion_path.unlink()
                completion_path.write_bytes(b"winner receipt\n")
                raise OSError("simulated post-publication failure")

            with mock.patch.object(
                portable_stage.portable_agent,
                "run_portable_stdout_attempt",
                return_value=attempt,
            ), mock.patch.object(
                portable_stage, "_project_outputs", return_value=outputs
            ), mock.patch.object(
                portable_stage,
                "_fsync_directory",
                side_effect=replace_with_winner,
            ):
                with self.assertRaises(OSError):
                    portable_stage.run_stage(prepared, timeout_seconds=1)

            self.assertEqual(
                (output_root / "winner.txt").read_text(encoding="utf-8"),
                "winner\n",
            )
            self.assertEqual(completion_path.read_bytes(), b"winner receipt\n")

    def test_completion_write_failure_cleans_receipt_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            prepared = self._prepare(pathlib.Path(directory))
            completion_path = pathlib.Path(prepared["completion_path"])
            outputs = {name: b"ours\n" for name in prepared["output_paths"]}
            attempt = {
                "provider": prepared["provider"],
                "execution_request_profile_hash": prepared[
                    "execution_request_profile_hash"
                ],
                "max_output_tokens": prepared["max_output_tokens"],
                "output_token_cap_binding": prepared[
                    "output_token_cap_binding"
                ],
                "output_token_cap_semantics": prepared[
                    "output_token_cap_semantics"
                ],
                "model_envelope_sha256": "a" * 64,
                "raw": b"unused\n",
            }
            real_identity = portable_stage._path_identity
            fsync_calls = 0
            completion_identity_failures = 0

            def reject_late_completion_identity(path):
                nonlocal completion_identity_failures
                if (
                    pathlib.Path(path) == completion_path
                    and completion_identity_failures == 0
                ):
                    completion_identity_failures += 1
                    raise OSError("late completion stat failed")
                return real_identity(path)

            def fail_after_completion_write(_path):
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 3:
                    raise OSError("simulated completion directory fsync failure")

            common = (
                mock.patch.object(
                    portable_stage.portable_agent,
                    "run_portable_stdout_attempt",
                    return_value=attempt,
                ),
                mock.patch.object(
                    portable_stage, "_project_outputs", return_value=outputs
                ),
            )
            with common[0], common[1], mock.patch.object(
                portable_stage,
                "_path_identity",
                side_effect=reject_late_completion_identity,
            ), mock.patch.object(
                portable_stage,
                "_fsync_directory",
                side_effect=fail_after_completion_write,
            ):
                with self.assertRaises(OSError):
                    portable_stage.run_stage(prepared, timeout_seconds=1)

            self.assertFalse(completion_path.exists())
            self.assertFalse(pathlib.Path(prepared["output_root"]).exists())
            with mock.patch.object(
                portable_stage.portable_agent,
                "run_portable_stdout_attempt",
                return_value=attempt,
            ), mock.patch.object(
                portable_stage, "_project_outputs", return_value=outputs
            ):
                completion = portable_stage.run_stage(prepared, timeout_seconds=1)
            self.assertEqual(completion["max_output_tokens"], 3072)

    def test_nonfinite_json_and_non_nfc_artifacts_are_rejected(self):
        for token in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(token=token):
                raw = b'{"value":' + token + b"}\n"
                with self.assertRaises(portable_stage.PortableStageError) as caught:
                    portable_stage._parse_json_artifact(raw, "fixture")
                self.assertEqual(caught.exception.code, "invalid_fixture")

        with self.assertRaises(portable_stage.PortableStageError):
            portable_stage._canonical_json_bytes({"value": float("nan")})

        decomposed = "é"
        composed = "é"
        self.assertEqual(
            portable_stage._canonical_json_bytes({"value": decomposed}),
            ('{"value":"' + composed + '"}\n').encode("utf-8"),
        )
        raw = ('{"value":"' + decomposed + '"}\n').encode("utf-8")
        with self.assertRaises(portable_stage.PortableStageError) as caught:
            portable_stage._parse_json_artifact(raw, "fixture")
        self.assertEqual(caught.exception.code, "invalid_fixture")

    def test_malformed_exec_budget_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            prepared = self._prepare(pathlib.Path(directory))
            preflight = json.loads(
                pathlib.Path(prepared["preflight_path"]).read_text(
                    encoding="utf-8"
                )
            )
            preflight["exec_budget"]["system_arg_max_bytes"] = "invalid"
            with self.assertRaises(portable_stage.PortableStageError) as caught:
                portable_stage._validate_public_preflight(preflight)
            self.assertEqual(caught.exception.code, "invalid_preflight")


if __name__ == "__main__":
    unittest.main()
