#!/usr/bin/env python3
"""RED contract for portable stage projection and runtime dispatch."""

import copy
import hashlib
import inspect
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_runtime
from lib import portable_agent
from lib import provider_adapters

try:
    from lib import portable_stage
except ImportError:
    portable_stage = None


REGISTRY = ROOT / "history/provider-adapters-v1.json"
FAKE = ROOT / "tests/fake_portable_stage_provider.py"
BOUNDARY = "portable-mirror-v1"


def _catalog(provider, *models):
    models = sorted(models)
    return {
        "schema_version": "provider-model-catalog-v1",
        "provider": provider,
        "models": models,
        "probe_revision": "fixture-model-catalog-v1",
        "catalog_sha256": provider_adapters._model_catalog_sha256(
            provider, models
        ),
    }


class PortableStageRuntimeSmoke(unittest.TestCase):
    def _api(self, module, name):
        self.assertIsNotNone(
            module,
            "missing behavior: lib.portable_stage",
        )
        value = getattr(module, name, None)
        self.assertTrue(
            callable(value),
            f"missing behavior: {module.__name__}.{name}",
        )
        return value

    def _error(self):
        self.assertIsNotNone(
            portable_stage,
            "missing behavior: lib.portable_stage",
        )
        value = getattr(portable_stage, "PortableStageError", None)
        self.assertTrue(
            isinstance(value, type) and issubclass(value, Exception),
            "missing behavior: portable_stage.PortableStageError",
        )
        return value

    @staticmethod
    def _intent(provider="codex"):
        resolve_intent = getattr(
            provider_adapters, "_resolve_command_intent_for_test", None
        )
        if not callable(resolve_intent):
            raise AssertionError(
                "missing behavior: provider_adapters._resolve_command_intent_for_test"
            )
        registry = provider_adapters.load_registry(REGISTRY)
        return resolve_intent(
            registry,
            "hunt",
            provider,
            model="MODEL",
            reasoning="high",
            executable_lookup=lambda _: str(FAKE),
        )

    def _prepare(
        self,
        root,
        *,
        stage="generate",
        intent=None,
        generation_policy="bounded policy\n",
    ):
        inputs = root / "inputs"
        inputs.mkdir(parents=True)
        if stage == "generate":
            serialized_prompt = json.dumps(
                {"schema_version": 1, "stage": "generate"},
                sort_keys=True,
                separators=(",", ":"),
            ) + "\n"
            (inputs / "generation_brief.json").write_text(
                '{"brief":"bounded"}\n', encoding="utf-8"
            )
            (inputs / "generation_policy.md").write_text(
                generation_policy, encoding="utf-8"
            )
            input_paths = {
                "generation_brief.json": inputs / "generation_brief.json",
                "generation_policy.md": inputs / "generation_policy.md",
            }
        elif stage == "review":
            serialized_prompt = json.dumps(
                {
                    "schema_version": 1,
                    "stage": "review",
                    "candidate": {"candidate_id": "I1"},
                },
                sort_keys=True,
                separators=(",", ":"),
            ) + "\n"
            (inputs / "candidate.json").write_text(
                '{"candidate_id":"I1"}\n', encoding="utf-8"
            )
            (inputs / "prior_work.md").write_text(
                "bounded prior work\n", encoding="utf-8"
            )
            (inputs / "review_contract.md").write_text(
                "bounded contract\n", encoding="utf-8"
            )
            input_paths = {
                "candidate.json": inputs / "candidate.json",
                "prior_work.md": inputs / "prior_work.md",
                "review_contract.md": inputs / "review_contract.md",
            }
        elif stage == "awr-research":
            serialized_prompt = json.dumps(
                {"schema_version": 1, "stage": "awr-research"},
                sort_keys=True,
                separators=(",", ":"),
            ) + "\n"
            (inputs / "idea.md").write_text(
                "bounded idea\n", encoding="utf-8"
            )
            input_paths = {"idea.md": inputs / "idea.md"}
        else:
            raise AssertionError(stage)
        prepared = self._api(portable_stage, "prepare_stage")(
            self._intent() if intent is None else intent,
            stage=stage,
            seat_id=f"{stage}-seat-1",
            serialized_prompt=serialized_prompt,
            input_paths=input_paths,
            output_root=root / "published",
            state_root=root / "portable-state",
        )
        return prepared, serialized_prompt, input_paths

    def test_grok_exact_fenced_transport_projects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared, _, _ = self._prepare(
                root,
                intent=self._intent("grok"),
            )
            completion = self._api(portable_stage, "run_stage")(
                prepared, timeout_seconds=2
            )
            self.assertTrue(
                pathlib.Path(prepared["completion_path"]).is_file()
            )
            self.assertEqual(
                completion["model_envelope_sha256"],
                hashlib.sha256(
                    (
                        pathlib.Path(prepared["state_root"])
                        / "imports"
                        / (
                            completion["model_envelope_sha256"]
                            + ".json"
                        )
                    ).read_bytes()
                ).hexdigest(),
            )
            self.assertTrue(
                all(
                    pathlib.Path(path).is_file()
                    for path in prepared["output_paths"].values()
                )
            )

    def test_request_binding_changes_with_declared_input_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            first, _, _ = self._prepare(
                root / "first", generation_policy="policy A\n"
            )
            second, _, _ = self._prepare(
                root / "second", generation_policy="policy B\n"
            )
            self.assertEqual(
                first["serialized_prompt_sha256"],
                second["serialized_prompt_sha256"],
            )
            self.assertNotEqual(
                first["provider_request_binding_sha256"],
                second["provider_request_binding_sha256"],
            )
            self.assertNotEqual(
                first["provider_request_sha256"],
                second["provider_request_sha256"],
            )

    def test_response_schema_drift_fails_before_provider_workload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared, _, _ = self._prepare(
                root,
                stage="awr-research",
                intent=provider_adapters._resolve_command_intent_for_test(
                    provider_adapters.load_registry(REGISTRY),
                    "awr",
                    "codex",
                    model="MODEL",
                    reasoning="high",
                    executable_lookup=lambda _: str(FAKE),
                ),
            )
            changed = copy.deepcopy(
                portable_stage._response_schema("awr-research")
            )
            changed["properties"]["artifacts"]["maxItems"] += 1
            with mock.patch.object(
                portable_stage,
                "_response_schema",
                return_value=changed,
            ), mock.patch.object(
                portable_agent,
                "run_portable_stdout_attempt",
            ) as workload:
                with self.assertRaises(self._error()) as caught:
                    self._api(portable_stage, "run_stage")(
                        prepared, timeout_seconds=2
                    )
            self.assertEqual(caught.exception.code, "response_schema_changed")
            workload.assert_not_called()

    def test_default_backend_drift_fails_before_provider_workload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            intent = provider_adapters._resolve_command_intent_for_test(
                provider_adapters.load_registry(REGISTRY),
                "awr",
                "opencode",
                executable_lookup=lambda _: str(FAKE),
                default_identity_probe=lambda *_: {
                    "schema_version": "provider-default-identity-v1",
                    "provider": "opencode",
                    "effective_model": "openai/safe-model",
                    "probe_revision": "fixture-default-probe-v1",
                },
                model_catalog_probe=lambda *_: _catalog(
                    "opencode", "openai/safe-model"
                ),
            )
            prepared, _, _ = self._prepare(
                root, stage="awr-research", intent=intent
            )
            workload = root / "provider-workload"
            with mock.patch.object(
                provider_adapters,
                "_host_default_identity_probe",
                return_value={
                    "schema_version": "provider-default-identity-v1",
                    "provider": "opencode",
                    "effective_model": "anthropic/claude-sonnet",
                    "probe_revision": "fixture-default-probe-v1",
                },
            ), mock.patch.object(
                provider_adapters,
                "_host_model_catalog_probe",
                return_value=_catalog("opencode", "openai/safe-model"),
            ), mock.patch.dict(
                os.environ,
                {"FAKE_PORTABLE_STAGE_LOG": str(workload)},
                clear=False,
            ):
                with self.assertRaises(self._error()):
                    self._api(portable_stage, "run_stage")(
                        prepared,
                        timeout_seconds=2,
                    )
            self.assertFalse(workload.exists())
            self.assertFalse(
                pathlib.Path(prepared["completion_path"]).exists()
            )

    @staticmethod
    def _load(path):
        raw = pathlib.Path(path).read_bytes()
        value = json.loads(raw.decode("utf-8"))
        if raw != (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8"):
            raise AssertionError(f"receipt is not canonical: {path}")
        return raw, value

    def test_preflight_and_completion_bind_dynamic_closed_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            launch_log = root / "launches.jsonl"
            prepared, prompt, input_paths = self._prepare(root)
            preflight_raw, preflight = self._load(
                prepared["preflight_path"]
            )
            self.assertEqual(prepared["execution_boundary"], BOUNDARY)
            for contained_only in (
                "manifest_path",
                "command_argv",
                "command_prefix_sha256",
            ):
                self.assertNotIn(
                    contained_only,
                    prepared,
                    "portable prepared value masquerades as contained-v1",
                )
            self.assertEqual(preflight["execution_boundary"], BOUNDARY)
            self.assertEqual(preflight["stage"], "generate")
            self.assertEqual(preflight["seat_id"], "generate-seat-1")
            self.assertEqual(
                preflight["execution_request_profile_hash"],
                self._intent().profile_hash,
            )
            self.assertEqual(
                preflight["provider_validation"], "unverified"
            )
            self.assertEqual(
                preflight["serialized_prompt_sha256"],
                hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(
                preflight["input_sha256s"],
                {
                    name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for name, path in input_paths.items()
                },
            )
            byte_budget = preflight["byte_budget"]
            self.assertEqual(
                byte_budget["role_bytes"],
                (ROOT / "roles/generate.md").stat().st_size,
            )
            self.assertEqual(
                byte_budget["declared_input_bytes"],
                {name: path.stat().st_size for name, path in input_paths.items()},
            )
            self.assertEqual(
                byte_budget["serialized_prompt_bytes"],
                len(prompt.encode("utf-8")),
            )
            self.assertLessEqual(
                byte_budget["conservative_total_bytes"],
                byte_budget["host_cap_bytes"],
            )
            self.assertEqual(
                byte_budget["host_cap_bytes"],
                portable_stage.HOST_INPUT_MAX_BYTES,
            )
            self.assertGreaterEqual(
                byte_budget["conservative_total_bytes"],
                byte_budget["role_bytes"]
                + sum(byte_budget["declared_input_bytes"].values())
                + byte_budget["provider_request_bytes"],
            )
            self.assertIsNone(prepared["output_contract"]["sha256"])
            self.assertEqual(
                prepared["output_contract"]["capture"], "stdout"
            )
            self.assertNotIn("path", prepared["output_contract"])
            self.assertNotIn("model_envelope_sha256", preflight)

            with mock.patch.dict(
                os.environ,
                {"FAKE_PORTABLE_STAGE_LOG": str(launch_log)},
                clear=False,
            ):
                completion = self._api(portable_stage, "run_stage")(
                    prepared, timeout_seconds=2
                )

            completion_raw, on_disk = self._load(
                prepared["completion_path"]
            )
            self.assertEqual(completion, on_disk)
            self.assertEqual(on_disk["execution_boundary"], BOUNDARY)
            self.assertEqual(
                on_disk["execution_request_profile_hash"],
                prepared["execution_request_profile_hash"],
            )
            self.assertEqual(
                on_disk["preflight_sha256"],
                hashlib.sha256(preflight_raw).hexdigest(),
            )
            self.assertRegex(
                on_disk["model_envelope_sha256"], r"^[0-9a-f]{64}$"
            )
            self.assertRegex(
                on_disk["completion_id"], r"^[0-9a-f]{64}$"
            )
            imported = (
                pathlib.Path(prepared["state_root"])
                / "imports"
                / f"{on_disk['model_envelope_sha256']}.json"
            )
            self.assertEqual(
                hashlib.sha256(imported.read_bytes()).hexdigest(),
                on_disk["model_envelope_sha256"],
            )
            self.assertEqual(
                set(on_disk["outputs"]),
                {"ideas.md", "ideas.tsv", "prompt-attestation.json"},
            )
            for name, descriptor in on_disk["outputs"].items():
                published = pathlib.Path(prepared["output_paths"][name])
                self.assertEqual(
                    hashlib.sha256(published.read_bytes()).hexdigest(),
                    descriptor["sha256"],
                )
                self.assertEqual(published.stat().st_size, descriptor["byte_count"])
            self.assertTrue(
                self._api(portable_stage, "verify_completion")(prepared)
            )
            self.assertTrue(history_runtime.verify_stage_completion(prepared))
            launch = json.loads(launch_log.read_text(encoding="utf-8"))
            self.assertEqual(launch["stage"], "generate")
            self.assertEqual(
                launch["prompt_sha256"],
                hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(
                hashlib.sha256(completion_raw).hexdigest(),
                hashlib.sha256(
                    pathlib.Path(prepared["completion_path"]).read_bytes()
                ).hexdigest(),
            )

    def test_fixed_host_input_cap_rejects_before_provider_launch(self):
        self.assertNotIn(
            "host_input_max_bytes",
            inspect.signature(
                self._api(portable_stage, "prepare_stage")
            ).parameters,
            "callers must not raise the host-owned input cap",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inputs = root / "inputs"
            inputs.mkdir()
            brief = inputs / "generation_brief.json"
            policy = inputs / "generation_policy.md"
            brief.write_text('{}\n', encoding="utf-8")
            policy.write_text('bounded\n', encoding="utf-8")
            launch_log = root / "provider-launched"
            with mock.patch.dict(
                os.environ,
                {"FAKE_PORTABLE_STAGE_LOG": str(launch_log)},
                clear=False,
            ):
                with self.assertRaises(self._error()) as caught:
                    self._api(portable_stage, "prepare_stage")(
                        self._intent(),
                        stage="generate",
                        seat_id="generate-seat-over-limit",
                        serialized_prompt=(
                            "x" * (portable_stage.HOST_INPUT_MAX_BYTES + 1)
                        ),
                        input_paths={
                            "generation_brief.json": brief,
                            "generation_policy.md": policy,
                        },
                        output_root=root / "published",
                        state_root=root / "state",
                    )
            self.assertEqual(caught.exception.code, "request_too_large")
            self.assertFalse(launch_log.exists())
            self.assertFalse(
                (root / "published").exists(),
                "over-limit input created the projection root",
            )
            self.assertFalse(
                (root / "state").exists(),
                "over-limit input created portable attempt state",
            )

    def test_provider_envelope_without_request_attestation_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared, _, _ = self._prepare(root)
            with mock.patch.dict(
                os.environ,
                {"FAKE_PORTABLE_STAGE_MODE": "missing-request-attestation"},
                clear=False,
            ):
                with self.assertRaises(self._error()):
                    self._api(portable_stage, "run_stage")(
                        prepared,
                        timeout_seconds=2,
                    )
            self.assertFalse(
                pathlib.Path(prepared["completion_path"]).exists()
            )
            self.assertTrue(
                all(
                    not pathlib.Path(path).exists()
                    for path in prepared["output_paths"].values()
                )
            )

    def test_wrong_provider_request_or_prompt_attestation_never_projects(self):
        for mode in (
            "wrong-request-attestation",
            "wrong-prompt-attestation",
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                prepared, _, _ = self._prepare(root)
                with mock.patch.dict(
                    os.environ,
                    {"FAKE_PORTABLE_STAGE_MODE": mode},
                    clear=False,
                ):
                    with self.assertRaises(self._error()):
                        self._api(portable_stage, "run_stage")(
                            prepared,
                            timeout_seconds=2,
                        )
                self.assertFalse(
                    pathlib.Path(prepared["completion_path"]).exists()
                )
                imports = pathlib.Path(prepared["state_root"]) / "imports"
                self.assertFalse(imports.exists() and any(imports.iterdir()))
                self.assertTrue(
                    all(
                        not pathlib.Path(path).exists()
                        for path in prepared["output_paths"].values()
                    )
                )

    def test_mirror_has_only_declared_inputs_and_scrubs_runtime_pointers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared, _, _ = self._prepare(root)
            provider_home = str(root / "provider-home")
            provider_codex_home = str(root / "provider-codex-home")
            scrubbed = {
                "PWD": str(ROOT),
                "OLDPWD": str(ROOT.parent),
                "GIT_DIR": str(ROOT / ".git"),
                "GIT_WORK_TREE": str(ROOT),
                "HISTORY_RUNTIME_ABI": "v1",
                "HISTORY_DB": str(ROOT / ".ai-ideas/history.sqlite3"),
                "AGENT_CMD": "legacy-global-agent",
                "FRONT_CMD": "legacy-front-agent",
                "BACK_CMD": "legacy-back-agent",
                "CONTAINED_AGENT_CMD_JSON": '["legacy-contained"]',
                "SIDE_CMD": "legacy-side-agent",
            }
            environment = {
                **scrubbed,
                "HOME": provider_home,
                "CODEX_HOME": provider_codex_home,
                "EXPECTED_PROVIDER_HOME": provider_home,
                "EXPECTED_PROVIDER_CODEX_HOME": provider_codex_home,
                "FAKE_PORTABLE_STAGE_MODE": "mirror-audit",
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                self._api(portable_stage, "run_stage")(
                    prepared, timeout_seconds=2
                )
            _, preflight = self._load(prepared["preflight_path"])
            self.assertEqual(
                preflight["environment_policy"],
                "provider-config-preserving-scrub-v1",
            )
            self.assertTrue(
                set(scrubbed).issubset(preflight["scrubbed_environment"])
            )
            self.assertTrue(
                {"HOME", "CODEX_HOME"}.issubset(
                    preflight["preserved_provider_config_environment"]
                )
            )
            preflight_text = pathlib.Path(
                prepared["preflight_path"]
            ).read_text(encoding="utf-8")
            for forbidden in (
                str(ROOT),
                str(ROOT / ".git"),
                str(ROOT / "ledger.tsv"),
                str(ROOT / ".ai-ideas/history.sqlite3"),
            ):
                self.assertNotIn(forbidden, preflight_text)

    def test_review_projection_stays_host_derived_and_replayable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared, _, _ = self._prepare(root, stage="review")
            self._api(portable_stage, "run_stage")(
                prepared, timeout_seconds=2
            )
            ballot = pathlib.Path(
                prepared["output_paths"]["verdict.tsv"]
            ).read_text(encoding="utf-8")
            self.assertEqual(
                ballot,
                "I1\tstrong-accept\t0\t"
                "The bounded fixture supports strong acceptance.\n",
            )
            self.assertTrue(history_runtime.verify_stage_completion(prepared))
            review = pathlib.Path(prepared["output_paths"]["review.md"])
            os.chmod(review, 0o600)
            review.write_text("tampered\n", encoding="utf-8")
            with self.assertRaises(self._error()):
                self._api(portable_stage, "verify_completion")(prepared)
            with self.assertRaises(history_runtime.RuntimeContractError):
                history_runtime.verify_stage_completion(prepared)

    def test_failed_or_unsafe_provider_output_never_projects(self):
        modes = (
            "nonzero",
            "malformed",
            "extra",
            "extra-envelope",
            "stdout-prefix",
            "second-envelope",
            "symlink",
            "oversize",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for mode in modes:
                with self.subTest(mode=mode):
                    case = root / mode
                    prepared, _, _ = self._prepare(case)
                    with mock.patch.dict(
                        os.environ,
                        {"FAKE_PORTABLE_STAGE_MODE": mode},
                        clear=False,
                    ):
                        with self.assertRaises(self._error()):
                            self._api(portable_stage, "run_stage")(
                                prepared, timeout_seconds=2
                            )
                    self.assertTrue(
                        pathlib.Path(prepared["preflight_path"]).is_file()
                    )
                    self.assertFalse(
                        pathlib.Path(prepared["completion_path"]).exists()
                    )
                    self.assertTrue(
                        all(
                            not pathlib.Path(path).exists()
                            for path in prepared["output_paths"].values()
                        )
                    )

    def test_timeout_kills_provider_process_group_before_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            delayed = root / "child-survived.txt"
            prepared, _, _ = self._prepare(root / "timeout")
            with mock.patch.dict(
                os.environ,
                {
                    "FAKE_PORTABLE_STAGE_MODE": "timeout",
                    "FAKE_PORTABLE_DELAYED_PATH": str(delayed),
                },
                clear=False,
            ):
                with self.assertRaises(self._error()) as caught:
                    self._api(portable_stage, "run_stage")(
                        prepared, timeout_seconds=0.2
                    )
            self.assertEqual(caught.exception.code, "timeout")
            time.sleep(1.0)
            self.assertFalse(delayed.exists())
            self.assertFalse(
                pathlib.Path(prepared["completion_path"]).exists()
            )
            self.assertTrue(
                all(
                    not pathlib.Path(path).exists()
                    for path in prepared["output_paths"].values()
                )
            )

    def test_runtime_has_narrow_explicit_comparator_and_reviewer_seams(self):
        self.assertTrue(
            callable(getattr(portable_agent, "run_portable_stdout_attempt", None)),
            "missing behavior: portable_agent.run_portable_stdout_attempt",
        )
        portable_runner = getattr(
            history_runtime, "_portable_comparator_runner", None
        )
        self.assertTrue(
            callable(portable_runner),
            "missing behavior: history_runtime._portable_comparator_runner",
        )
        compare_parameters = inspect.signature(
            history_runtime._compare_frozen_targets
        ).parameters
        review_parameters = inspect.signature(
            history_runtime._run_review_matrix
        ).parameters
        resume_parameters = inspect.signature(
            history_runtime._validate_resume_comparator_stages
        ).parameters
        self.assertIn("executor", compare_parameters)
        self.assertIn("portable_request_profile", compare_parameters)
        self.assertIn("executor", review_parameters)
        self.assertIn("reviewer_stage_runner", review_parameters)
        self.assertIn("execution_boundary", resume_parameters)

        launch_log = pathlib.Path(tempfile.gettempdir()) / (
            "portable-stage-mixing-" + os.urandom(8).hex()
        )
        try:
            with mock.patch.dict(
                os.environ,
                {"FAKE_PORTABLE_STAGE_LOG": str(launch_log)},
                clear=False,
            ):
                with mock.patch.object(
                    history_runtime,
                    "_read_bound_regular",
                    side_effect=AssertionError(
                        "executor mixing reached a path read"
                    ),
                ):
                    with self.assertRaisesRegex(
                        history_runtime.RuntimeContractError,
                        "^portable-v2 cannot mix command_json$",
                    ):
                        history_runtime._compare_frozen_targets(
                            db_path="missing.db",
                            policy_path="missing-policy.json",
                            batch_path="missing-batch.json",
                            artifact_root="missing-artifacts",
                            selection_path="missing-selection.json",
                            command_json=json.dumps([str(FAKE)]),
                            executor="portable-v2",
                            portable_request_profile=self._intent(),
                        )
                    with self.assertRaisesRegex(
                        history_runtime.RuntimeContractError,
                        "^contained-v1 cannot use reviewer_stage_runner$",
                    ):
                        history_runtime._run_review_matrix(
                            db_path="missing.db",
                            policy_path="missing-policy.json",
                            batch_path="missing-batch.json",
                            review_plan_path="missing-plan.json",
                            reviewer_commands={"1": json.dumps([str(FAKE)])},
                            reviewer_stage_runner=lambda **_: None,
                            executor="contained-v1",
                            stage_root="missing-stages",
                            output_path="missing-index.json",
                        )
            self.assertFalse(
                launch_log.exists(),
                "mixed executor configuration launched a provider",
            )
        finally:
            launch_log.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
