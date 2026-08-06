#!/usr/bin/env python3
"""P0 authority tests for multi-backend model routing and registry ABI."""

import copy
import json
import pathlib
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

from lib import history_contract_v2
from lib import portable_agent
from lib import portable_stage
from lib import provider_adapters


REGISTRY = ROOT / "history/provider-adapters-v1.json"
FAKE = ROOT / "tests/fake_portable_stage_provider.py"


def catalog(provider, *models, revision="fixture-model-catalog-v1"):
    normalized = sorted(models)
    material = {
        "schema_version": "provider-model-catalog-v1",
        "provider": provider,
        "models": normalized,
    }
    return {
        **material,
        "probe_revision": revision,
        "catalog_sha256": history_contract_v2.framed_sha256(
            "provider-model-catalog-v1",
            history_contract_v2.canonical_bytes(material),
        ),
    }


def default_identity(model):
    return {
        "schema_version": "provider-default-identity-v1",
        "provider": "opencode",
        "effective_model": model,
        "probe_revision": "fixture-default-identity-v1",
    }


class RegistryAbiAuthoritySmoke(unittest.TestCase):
    def test_registry_is_the_exact_tracked_v1_abi(self):
        base = json.loads(REGISTRY.read_text(encoding="utf-8"))
        mutations = []
        for path, value in (
            (("registry_revision",), "forged-revision"),
            (("providers", "kimi", "grammar_revision"), "forged-grammar"),
            (("providers", "kimi", "reasoning_values"), ["high"]),
            (("providers", "codex", "reasoning_values"), ["low", "high"]),
        ):
            changed = copy.deepcopy(base)
            target = changed
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            mutations.append(changed)

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for index, changed in enumerate(mutations):
                path = root / f"forged-{index}.json"
                path.write_text(json.dumps(changed), encoding="utf-8")
                with self.subTest(index=index), self.assertRaises(
                    provider_adapters.ProviderResolutionError
                ):
                    provider_adapters.load_registry(path)

            reformatted = root / "reformatted.json"
            reformatted.write_text(
                json.dumps(base, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(provider_adapters.ProviderResolutionError):
                provider_adapters.load_registry(reformatted)

            duplicate = root / "duplicate.json"
            duplicate.write_text(
                REGISTRY.read_text(encoding="utf-8").replace(
                    '"registry_revision": "2026-08-05",',
                    '"registry_revision": "forged",\n'
                    '  "registry_revision": "2026-08-05",',
                ),
                encoding="utf-8",
            )
            with self.assertRaises(provider_adapters.ProviderResolutionError):
                provider_adapters.load_registry(duplicate)

    def test_kimi_render_guard_rejects_reasoning_without_registry_lookup(self):
        with self.assertRaises(provider_adapters.ProviderResolutionError):
            provider_adapters._render_command_fields(
                "kimi", str(FAKE), "model", "high", "/mirror", "PROMPT"
            )


class ModelCatalogAuthoritySmoke(unittest.TestCase):
    def setUp(self):
        self.registry = provider_adapters.load_registry(REGISTRY)

    def resolve(self, provider, model, *, models, default=None):
        return provider_adapters._resolve_command_intent_for_test(
            self.registry,
            "awr",
            provider,
            model=model,
            executable_lookup=lambda _: str(FAKE),
            default_identity_probe=(
                None if default is None else lambda *_: default_identity(default)
            ),
            model_catalog_probe=lambda *_: catalog(provider, *models),
            version_probe=lambda *_: b"1.1.10\n",
        )

    def test_dynamic_routes_fail_even_when_the_catalog_lists_them(self):
        routes = ("default", "auto", "openrouter/auto", "configured/current")
        for provider in ("opencode", "agy"):
            for route in routes:
                with self.subTest(provider=provider, route=route), self.assertRaises(
                    provider_adapters.ProviderResolutionError
                ):
                    self.resolve(provider, route, models=(route,))

    def test_exact_catalog_membership_and_opencode_provider_model_shape(self):
        safe = self.resolve(
            "opencode",
            "openai/gpt-safe",
            models=("openai/gpt-safe", "openrouter/auto", "anthropic/claude"),
        )
        self.assertEqual(safe.effective_model, "openai/gpt-safe")
        self.assertEqual(safe.model_catalog_probe_revision, "fixture-model-catalog-v1")
        self.assertEqual(len(safe.model_catalog_sha256), 64)

        for provider, model, models in (
            ("opencode", "openai/not-listed", ("openai/gpt-safe",)),
            ("opencode", "gpt-safe", ("gpt-safe",)),
            ("agy", "bogus-model", ("gemini-safe",)),
            ("opencode", "anthropic/claude", ("anthropic/claude",)),
            ("agy", "claude-sonnet", ("claude-sonnet",)),
        ):
            with self.subTest(provider=provider, model=model), self.assertRaises(
                provider_adapters.ProviderResolutionError
            ):
                self.resolve(provider, model, models=models)

    def test_opencode_omitted_default_must_be_safe_and_in_the_same_catalog(self):
        safe = self.resolve(
            "opencode",
            None,
            default="openai/gpt-safe",
            models=("openai/gpt-safe",),
        )
        self.assertEqual(safe.effective_model, "openai/gpt-safe")

        for default, models in (
            ("openrouter/auto", ("openrouter/auto",)),
            ("openai/not-listed", ("openai/gpt-safe",)),
            ("anthropic/claude", ("anthropic/claude",)),
        ):
            with self.subTest(default=default), self.assertRaises(
                provider_adapters.ProviderResolutionError
            ):
                self.resolve("opencode", None, default=default, models=models)

    def test_launch_reprobe_rejects_catalog_or_default_drift(self):
        explicit = self.resolve(
            "agy", "gemini-safe", models=("gemini-safe",)
        )
        with mock.patch.object(
            provider_adapters,
            "_host_model_catalog_probe",
            return_value=catalog("agy", "gemini-safe", "new-model"),
        ):
            with self.assertRaises(provider_adapters.ProviderResolutionError):
                provider_adapters.revalidate_command_intent_for_launch(explicit)

        omitted = self.resolve(
            "opencode",
            None,
            default="openai/gpt-safe",
            models=("openai/gpt-safe",),
        )
        with mock.patch.object(
            provider_adapters,
            "_host_model_catalog_probe",
            return_value=catalog("opencode", "openai/gpt-safe"),
        ), mock.patch.object(
            provider_adapters,
            "_host_default_identity_probe",
            return_value=default_identity("openai/gpt-other"),
        ):
            with self.assertRaises(provider_adapters.ProviderResolutionError):
                provider_adapters.revalidate_command_intent_for_launch(omitted)

    def test_direct_stdout_runner_revalidates_before_render_or_workload(self):
        intent = self.resolve(
            "agy", "gemini-safe", models=("gemini-safe",)
        )
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            provider_adapters,
            "_host_model_catalog_probe",
            return_value=catalog("agy", "gemini-safe", "new-model"),
        ), mock.patch.object(
            provider_adapters,
            "render_command",
            side_effect=AssertionError("render must not be reached"),
        ) as render:
            with self.assertRaises(portable_agent.PortableAgentError) as caught:
                portable_agent.run_portable_stdout_attempt(
                    intent,
                    inputs=[],
                    prompt="PROMPT",
                    response_schema=portable_stage._response_schema(
                        "awr-research"
                    ),
                    expected_response_attestation={
                        "schema_version": "portable-stage-response-attestation-v1",
                        "provider_request_binding_sha256": "0" * 64,
                        "serialized_prompt_sha256": "0" * 64,
                    },
                    state_root=pathlib.Path(directory) / "state",
                    timeout_seconds=1,
                )
            self.assertEqual(
                caught.exception.code,
                "provider_model_authority_changed",
            )
            render.assert_not_called()

    def test_unsupported_agy_revalidation_stops_before_catalog_render_or_popen(self):
        intent = self.resolve(
            "agy", "gemini-safe", models=("gemini-safe",)
        )
        catalog_probe = mock.Mock(
            side_effect=AssertionError("catalog must not be reached")
        )
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            provider_adapters,
            "_host_agy_version_probe",
            return_value=b"1.1.7\n",
        ), mock.patch.object(
            provider_adapters,
            "_host_model_catalog_probe",
            catalog_probe,
        ), mock.patch.object(
            provider_adapters,
            "render_command",
            side_effect=AssertionError("render must not be reached"),
        ) as render, mock.patch.object(
            portable_agent.subprocess,
            "Popen",
            side_effect=AssertionError("Popen must not be reached"),
        ) as popen:
            with self.assertRaises(portable_agent.PortableAgentError) as caught:
                portable_agent.run_portable_stdout_attempt(
                    intent,
                    inputs=[],
                    prompt="PROMPT",
                    response_schema=portable_stage._response_schema(
                        "awr-research"
                    ),
                    expected_response_attestation={
                        "schema_version": "portable-stage-response-attestation-v1",
                        "provider_request_binding_sha256": "0" * 64,
                        "serialized_prompt_sha256": "0" * 64,
                    },
                    state_root=pathlib.Path(directory) / "state",
                    timeout_seconds=1,
                )
            self.assertEqual(
                caught.exception.code,
                "provider_model_authority_changed",
            )
            catalog_probe.assert_not_called()
            render.assert_not_called()
            popen.assert_not_called()

    def test_stdout_runner_revalidates_again_immediately_before_popen(self):
        intent = self.resolve(
            "agy", "gemini-safe", models=("gemini-safe",)
        )
        current = {"catalog": catalog("agy", "gemini-safe")}

        def drift_during_input_copy(*_args, **_kwargs):
            current["catalog"] = catalog(
                "agy", "gemini-safe", "new-model"
            )
            return set()

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            provider_adapters,
            "_host_model_catalog_probe",
            side_effect=lambda *_: current["catalog"],
        ), mock.patch.object(
            provider_adapters,
            "_host_agy_version_probe",
            return_value=b"1.1.10\n",
        ), mock.patch.object(
            portable_agent,
            "_copy_inputs",
            side_effect=drift_during_input_copy,
        ), mock.patch.object(
            provider_adapters,
            "render_command",
            return_value=([str(FAKE), "PROMPT"], {}),
        ), mock.patch.object(
            portable_agent.subprocess,
            "Popen",
            side_effect=AssertionError(
                "Popen must not be reached after catalog drift"
            ),
        ) as popen:
            with self.assertRaises(portable_agent.PortableAgentError) as caught:
                portable_agent.run_portable_stdout_attempt(
                    intent,
                    inputs=[],
                    prompt="PROMPT",
                    response_schema=portable_stage._response_schema(
                        "awr-research"
                    ),
                    expected_response_attestation={
                        "schema_version": "portable-stage-response-attestation-v1",
                        "provider_request_binding_sha256": "0" * 64,
                        "serialized_prompt_sha256": "0" * 64,
                    },
                    state_root=pathlib.Path(directory) / "state",
                    timeout_seconds=1,
                )
            self.assertEqual(
                caught.exception.code,
                "provider_model_authority_changed",
            )
            popen.assert_not_called()

    def test_host_catalog_probe_is_bounded_pure_line_introspection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            audit = root / "audit.json"
            executable = root / "opencode"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                f"pathlib.Path({str(audit)!r}).write_text(json.dumps({{"
                "'argv': sys.argv[1:], 'cwd': os.getcwd(), "
                "'launch': os.environ.get('PROVIDER_LAUNCH_LOG')}))\n"
                "if sys.argv[1:] != ['models', '--pure']:\n"
                "    raise SystemExit(91)\n"
                "print('openai/gpt-safe')\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            with mock.patch.dict(
                "os.environ", {"PROVIDER_LAUNCH_LOG": "must-not-propagate"}, clear=False
            ):
                evidence = provider_adapters._host_model_catalog_probe(
                    "opencode", str(executable)
                )
            self.assertEqual(evidence["models"], ["openai/gpt-safe"])
            observed = json.loads(audit.read_text(encoding="utf-8"))
            self.assertEqual(observed["argv"], ["models", "--pure"])
            self.assertTrue(pathlib.Path(observed["cwd"]).name.startswith("provider-model-catalog-"))
            self.assertIsNone(observed["launch"])

    def test_agy_catalog_probe_allows_a_thirty_second_observation(self):
        def delayed_catalog_probe(*_args, timeout_seconds, **_kwargs):
            if timeout_seconds < 30:
                return None
            return 0, b"gemini-safe\n", b""

        with mock.patch.object(
            provider_adapters,
            "_run_bounded_default_probe",
            side_effect=delayed_catalog_probe,
        ) as probe:
            evidence = provider_adapters._host_model_catalog_probe(
                "agy", "/fake/agy"
            )

        probe.assert_called_once_with(
            ["/fake/agy", "models"],
            cwd=mock.ANY,
            env=mock.ANY,
            timeout_seconds=30,
        )
        self.assertEqual(
            evidence,
            {
                "schema_version": "provider-model-catalog-v1",
                "provider": "agy",
                "models": ["gemini-safe"],
                "probe_revision": "agy-models-v1",
                "catalog_sha256": (
                    "5bd869404013cd885e890dc5041c9c0f0ab7f65f358319c68f1937157d460903"
                ),
            },
        )


class AgyStructuredOutputVersionSmoke(unittest.TestCase):
    def setUp(self):
        self.registry = provider_adapters.load_registry(REGISTRY)

    def test_agy_version_requires_canonical_supported_semver(self):
        accepted = (b"1.1.8\n", b"1.1.10\n", b"2.0.0\n")
        rejected = (
            b"1.1.7\n", b"01.1.8\n", b"1.01.8\n", b"1.1.08\n",
            b"1.1.8\r\n", b"v1.1.8\n", b"1.1.8-beta\n",
            b"1.1.8+build\n", b"1.1.8\nextra\n", b"\xff\n",
        )
        for raw in accepted:
            with self.subTest(raw=raw):
                self.assertEqual(
                    provider_adapters._parse_agy_cli_revision(raw),
                    raw[:-1].decode("ascii"),
                )
        for raw in rejected:
            with self.subTest(raw=raw), self.assertRaises(
                provider_adapters.ProviderResolutionError
            ):
                provider_adapters._parse_agy_cli_revision(raw)

    def test_host_agy_version_probe_is_bounded_and_exact(self):
        cases = (
            (None, None),
            ((1, b"1.1.8\n", b""), None),
            ((0, b"1.1.8\n", b"diagnostic\n"), None),
            ((0, b"x" * 32769, b""), None),
            ((0, b"1.1.8\n", b""), b"1.1.8\n"),
        )
        for observation, expected in cases:
            with self.subTest(observation=observation), mock.patch.object(
                provider_adapters,
                "_run_bounded_default_probe",
                return_value=observation,
            ) as runner:
                self.assertEqual(
                    provider_adapters._host_agy_version_probe("agy", "/fake/agy"),
                    expected,
                )
                runner.assert_called_once_with(
                    ["/fake/agy", "--version"],
                    cwd=mock.ANY,
                    env=mock.ANY,
                )

    def test_unsupported_agy_short_circuits_catalog_render_and_workload(self):
        catalog_probe = mock.Mock(
            side_effect=AssertionError("catalog must not be reached")
        )
        with self.assertRaises(provider_adapters.ProviderResolutionError):
            provider_adapters._resolve_command_intent_for_test(
                self.registry,
                "awr",
                "agy",
                model="gemini-safe",
                executable_lookup=lambda _: str(FAKE),
                version_probe=lambda *_: b"1.1.7\n",
                model_catalog_probe=catalog_probe,
            )
        catalog_probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
