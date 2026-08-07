#!/usr/bin/env python3
"""RED contracts for host-owned, no-workload provider capability evidence."""

import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import provider_adapters


REGISTRY = ROOT / "history/provider-adapters-v1.json"
FAKE_EXECUTABLE = ROOT / "tests/fake_portable_agent.py"
PROVIDERS = (
    ("hunt", "codex"),
    ("hunt", "kimi"),
    ("hunt", "grok"),
    ("hunt", "claude"),
    ("awr", "opencode"),
    ("awr", "agy"),
    ("awr", "claude"),
)


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


def _fixture_config(
    provider,
    *,
    model,
    reasoning,
    model_source="default",
    reasoning_source="default",
    capacity_identity="fixture-capacity-17",
):
    """Raw fake config bytes; the host parser must derive final capability facts."""
    return (
        "fixture-capability-config-v1\n"
        f"provider={provider}\n"
        f"configured_model={model}\n"
        f"configured_reasoning={reasoning}\n"
        f"model_source={model_source}\n"
        f"reasoning_source={reasoning_source}\n"
        f"capacity_identity={capacity_identity}\n"
    ).encode("utf-8")


def _observation(
    provider,
    *,
    model,
    reasoning,
    cli_revision=None,
    model_source="default",
    reasoning_source="default",
    capacity_identity="fixture-capacity-17",
    exit_code=0,
    stderr=b"",
    config_bytes=None,
    timed_out=False,
    truncated=False,
):
    revision = cli_revision or f"fixture-cli-revision::{provider}"
    if config_bytes is None:
        config_bytes = _fixture_config(
            provider,
            model=model,
            reasoning=reasoning,
            model_source=model_source,
            reasoning_source=reasoning_source,
            capacity_identity=capacity_identity,
        )
    return {
        "schema_version": "provider-host-probe-observation-v1",
        "exit_code": exit_code,
        "stdout": f"fixture-version-output:{revision}\n".encode("utf-8"),
        "stderr": stderr,
        "config_bytes": config_bytes,
        "timed_out": timed_out,
        "truncated": truncated,
    }


class ProviderHostCapabilityEvidenceRed(unittest.TestCase):
    def setUp(self):
        self.registry = provider_adapters.load_registry(REGISTRY)

    def _producer(self):
        producer = getattr(
            provider_adapters,
            "_resolve_provider_with_test_host_probe_runner",
            None,
        )
        self.assertTrue(
            callable(producer),
            "missing host evidence producer: "
            "provider_adapters._resolve_provider_with_test_host_probe_runner",
        )
        return producer

    def _resolve(
        self,
        surface,
        provider,
        *,
        observation,
        model=None,
        reasoning=None,
        calls=None,
        executable_path=FAKE_EXECUTABLE,
    ):
        calls = [] if calls is None else calls

        def runner(invocation):
            calls.append(invocation)
            return observation

        return self._producer()(
            self.registry,
            surface,
            provider,
            model=model,
            reasoning=reasoning,
            executable_lookup=lambda _: str(executable_path),
            probe_runner=runner,
        )

    def test_grammar_only_intent_remains_usable_and_omitted_overrides_add_no_flags(self):
        absent = {
            "codex": ("-m", "model_reasoning_effort="),
            "kimi": ("-m",),
            "grok": ("-m", "--reasoning-effort"),
        }
        for surface, provider in PROVIDERS[:3]:
            with self.subTest(provider=provider):
                intent = provider_adapters._resolve_command_intent_for_test(
                    self.registry,
                    surface,
                    provider,
                    executable_lookup=lambda _: str(FAKE_EXECUTABLE),
                )
                argv, environment = provider_adapters.render_command(
                    intent,
                    pathlib.Path("/fixture/mirror"),
                    "FIXTURE_PROMPT",
                )
                self.assertTrue(provider_adapters.command_intent_is_issued(intent))
                self.assertEqual(intent.authority, "shadow-only")
                self.assertFalse(intent.hard_complete_eligible)
                self.assertIsNone(intent.effective_model)
                self.assertIsNone(intent.effective_reasoning)
                self.assertEqual(
                    environment,
                    (
                        {
                            "GROK_CLAUDE_SKILLS_ENABLED": "false",
                            "GROK_CLAUDE_RULES_ENABLED": "false",
                            "GROK_CLAUDE_MCPS_ENABLED": "false",
                            "GROK_CLAUDE_HOOKS_ENABLED": "false",
                            "GROK_CLAUDE_SESSIONS_ENABLED": "false",
                        }
                        if provider == "grok"
                        else {}
                    ),
                )
                for spelling in absent[provider]:
                    self.assertFalse(
                        any(item == spelling or item.startswith(spelling) for item in argv),
                        (provider, spelling, argv),
                    )

        opencode = provider_adapters._resolve_command_intent_for_test(
            self.registry,
            "awr",
            "opencode",
            executable_lookup=lambda _: str(FAKE_EXECUTABLE),
            default_identity_probe=lambda *_: {
                "schema_version": "provider-default-identity-v1",
                "provider": "opencode",
                "effective_model": "openai/fixture-model",
                "probe_revision": "fixture-default-probe-v1",
            },
            model_catalog_probe=lambda *_: _catalog(
                "opencode", "openai/fixture-model"
            ),
        )
        argv, _ = provider_adapters.render_command(
            opencode, pathlib.Path("/fixture/mirror"), "FIXTURE_PROMPT"
        )
        self.assertIsNone(opencode.requested_model)
        self.assertEqual(opencode.effective_model, "openai/fixture-model")
        self.assertIn("openai/fixture-model", argv)
        self.assertNotIn("--variant", argv)

        with self.assertRaises(provider_adapters.ProviderResolutionError):
            provider_adapters._resolve_command_intent_for_test(
                self.registry,
                "awr",
                "agy",
                executable_lookup=lambda _: str(FAKE_EXECUTABLE),
                version_probe=lambda *_: b"1.1.10\n",
            )

    def test_raw_observations_derive_effective_defaults_for_all_registered_providers(self):
        for surface, provider in PROVIDERS:
            with self.subTest(provider=provider):
                model = f"fixture-default-model::{provider}"
                reasoning = f"fixture-default-reasoning::{provider}"
                calls = []
                capability = self._resolve(
                    surface,
                    provider,
                    observation=_observation(
                        provider,
                        model=model,
                        reasoning=reasoning,
                    ),
                    calls=calls,
                )
                self.assertEqual(capability.model_identity, model)
                self.assertEqual(capability.reasoning_identity, reasoning)
                self.assertEqual(
                    capability.cli_revision,
                    f"fixture-cli-revision::{provider}",
                )
                self.assertTrue(provider_adapters.capability_is_issued(capability))
                self.assertEqual(capability.authority, "shadow-only")
                self.assertFalse(capability.hard_complete_eligible)
                self.assertEqual(len(capability.evidence_sha256), 64)
                self.assertEqual(len(calls), 1)

    def test_raw_observations_verify_explicit_model_and_supported_reasoning_overrides(self):
        expected_spelling = {
            "codex": ("-m", "model_reasoning_effort="),
            "kimi": ("-m",),
            "grok": ("-m", "--reasoning-effort"),
            "opencode": ("-m", "--variant"),
            "agy": ("--model", "--effort"),
            "claude": ("--model", "--effort"),
        }
        for surface, provider in PROVIDERS:
            with self.subTest(provider=provider):
                model = (
                    "fixture/requested-model"
                    if provider == "opencode"
                    else f"fixture-requested-model::{provider}"
                )
                reasoning = None if provider == "kimi" else "high"
                effective_reasoning = reasoning or f"fixture-default-reasoning::{provider}"
                capability = self._resolve(
                    surface,
                    provider,
                    model=model,
                    reasoning=reasoning,
                    observation=_observation(
                        provider,
                        model=model,
                        reasoning=effective_reasoning,
                        model_source="argv-model-override",
                        reasoning_source=(
                            "default" if reasoning is None else "argv-reasoning-override"
                        ),
                    ),
                )
                self.assertEqual(capability.model_identity, model)
                self.assertEqual(capability.reasoning_identity, effective_reasoning)
                self.assertFalse(capability.hard_complete_eligible)
                if provider in ("opencode", "agy"):
                    with self.assertRaises(
                        provider_adapters.ProviderResolutionError
                    ):
                        provider_adapters.render_command(
                            capability,
                            pathlib.Path("/fixture/mirror"),
                            "FIXTURE_PROMPT",
                        )
                    continue
                render_kwargs = {}
                if provider == "claude":
                    render_kwargs["response_schema"] = {"type": "object"}
                argv, _ = provider_adapters.render_command(
                    capability,
                    pathlib.Path("/fixture/mirror"),
                    "FIXTURE_PROMPT",
                    **render_kwargs,
                )
                for spelling in expected_spelling[provider]:
                    self.assertTrue(
                        any(item == spelling or item.startswith(spelling) for item in argv),
                        (provider, spelling, argv),
                    )

    def test_unsupported_reasoning_and_ignored_overrides_fail_before_workload_launch(self):
        calls = []
        with self.assertRaises(provider_adapters.ProviderResolutionError):
            self._resolve(
                "hunt",
                "kimi",
                model="fixture-model",
                reasoning="fixture-effort",
                observation=_observation(
                    "kimi",
                    model="fixture-model",
                    reasoning="fixture-effort",
                ),
                calls=calls,
            )
        self.assertEqual(calls, [])

        mismatches = (
            {
                "model": "fixture-requested-model",
                "reasoning": None,
                "observed_model": "fixture-default-model",
                "observed_reasoning": "fixture-default-reasoning",
                "model_source": "default",
                "reasoning_source": "default",
            },
            {
                "model": None,
                "reasoning": "high",
                "observed_model": "fixture-default-model",
                "observed_reasoning": "low",
                "model_source": "default",
                "reasoning_source": "default",
            },
        )
        for case in mismatches:
            with self.subTest(case=case):
                calls = []
                with self.assertRaises(provider_adapters.ProviderResolutionError):
                    self._resolve(
                        "hunt",
                        "codex",
                        model=case["model"],
                        reasoning=case["reasoning"],
                        observation=_observation(
                            "codex",
                            model=case["observed_model"],
                            reasoning=case["observed_reasoning"],
                            model_source=case["model_source"],
                            reasoning_source=case["reasoning_source"],
                        ),
                        calls=calls,
                    )
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]["purpose"], "capability-introspection")
                self.assertNotIn("prompt", calls[0])

    def test_missing_ambiguous_or_unknown_default_evidence_stays_unbudgetable(self):
        ambiguous = (
            _observation(
                "codex",
                model="fixture-model-a",
                reasoning="fixture-effort",
                config_bytes=(
                    b"fixture-capability-config-v1\n"
                    b"provider=codex\n"
                    b"configured_model=fixture-model-a\n"
                    b"configured_model=fixture-model-b\n"
                    b"configured_reasoning=fixture-effort\n"
                    b"model_source=default\n"
                    b"reasoning_source=default\n"
                    b"capacity_identity=fixture-capacity-17\n"
                ),
            ),
            _observation(
                "codex",
                model="fixture-model",
                reasoning="fixture-effort",
                exit_code=7,
            ),
            _observation(
                "codex",
                model="fixture-model",
                reasoning="fixture-effort",
                config_bytes=b"unknown-fixture-format\n",
            ),
            _observation(
                "codex",
                model="fixture-model",
                reasoning="fixture-effort",
                timed_out=True,
            ),
            {
                **_observation(
                    "codex",
                    model="fixture-model",
                    reasoning="fixture-effort",
                ),
                "caller_capability_claim": True,
            },
        )
        for observation in ambiguous:
            with self.subTest(observation=observation):
                capability = self._resolve(
                    "hunt",
                    "codex",
                    observation=observation,
                )
                self.assertEqual(capability.model_identity, "provider-default")
                self.assertEqual(capability.reasoning_identity, "provider-default")
                self.assertEqual(capability.authority, "shadow-only")
                self.assertFalse(capability.hard_complete_eligible)

    def test_ambiguous_evidence_cannot_satisfy_a_strict_explicit_override(self):
        observation = _observation(
            "codex",
            model="fixture-requested-model",
            reasoning="fixture-effort",
            config_bytes=b"unknown-fixture-format\n",
        )
        with self.assertRaises(provider_adapters.ProviderResolutionError):
            self._resolve(
                "hunt",
                "codex",
                model="fixture-requested-model",
                observation=observation,
            )

    def test_public_resolution_rejects_caller_runner_probe_and_observation(self):
        calls = []

        def injected(*args, **kwargs):
            calls.append((args, kwargs))
            return _observation(
                "codex",
                model="caller-model",
                reasoning="caller-reasoning",
            )

        with self.assertRaises(provider_adapters.ProviderResolutionError):
            provider_adapters.resolve_provider(
                self.registry,
                "hunt",
                "codex",
                version_probe=injected,
            )
        for keyword in ("probe_runner", "probe_observation"):
            with self.subTest(keyword=keyword):
                with self.assertRaises(
                    (TypeError, provider_adapters.ProviderResolutionError)
                ):
                    provider_adapters.resolve_provider(
                        self.registry,
                        "hunt",
                        "codex",
                        **{keyword: injected},
                    )
        self.assertEqual(calls, [])

    def test_probe_invocation_is_no_workload_bounded_and_environment_sanitized(self):
        calls = []

        def runner(invocation):
            calls.append(invocation)
            self.assertEqual(invocation["purpose"], "capability-introspection")
            self.assertEqual(
                invocation["schema_version"],
                "provider-host-probe-invocation-v1",
            )
            self.assertNotIn("prompt", invocation)
            self.assertNotIn("AI_IDEAS_PROBE_SECRET_SENTINEL", invocation["environment"])
            self.assertGreater(invocation["timeout_seconds"], 0)
            self.assertLessEqual(invocation["timeout_seconds"], 10)
            for field in (
                "stdout_limit_bytes",
                "stderr_limit_bytes",
                "config_limit_bytes",
            ):
                self.assertGreater(invocation[field], 0)
                self.assertLessEqual(invocation[field], 65536)
            self.assertEqual(
                pathlib.Path(invocation["argv"][0]).resolve(),
                FAKE_EXECUTABLE.resolve(),
            )
            return _observation(
                "codex",
                model="fixture-default-model",
                reasoning="fixture-default-reasoning",
            )

        with mock.patch.dict(
            os.environ,
            {"AI_IDEAS_PROBE_SECRET_SENTINEL": "must-not-cross-probe-boundary"},
        ):
            capability = self._producer()(
                self.registry,
                "hunt",
                "codex",
                executable_lookup=lambda _: str(FAKE_EXECUTABLE),
                probe_runner=runner,
            )
        self.assertEqual(len(calls), 1)
        self.assertFalse(capability.hard_complete_eligible)

    def test_evidence_binds_raw_observation_cli_revision_and_executable_identity(self):
        first = self._resolve(
            "hunt",
            "codex",
            observation=_observation(
                "codex",
                model="fixture-model",
                reasoning="fixture-effort",
                cli_revision="fixture-cli-r1",
                stderr=b"fixture-note-a\n",
            ),
        )
        raw_drift = self._resolve(
            "hunt",
            "codex",
            observation=_observation(
                "codex",
                model="fixture-model",
                reasoning="fixture-effort",
                cli_revision="fixture-cli-r1",
                stderr=b"fixture-note-b\n",
            ),
        )
        revision_drift = self._resolve(
            "hunt",
            "codex",
            observation=_observation(
                "codex",
                model="fixture-model",
                reasoning="fixture-effort",
                cli_revision="fixture-cli-r2",
                stderr=b"fixture-note-a\n",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            alternate = pathlib.Path(directory) / "fake-provider"
            alternate.write_bytes(FAKE_EXECUTABLE.read_bytes())
            alternate.chmod(0o700)
            executable_drift = self._resolve(
                "hunt",
                "codex",
                observation=_observation(
                    "codex",
                    model="fixture-model",
                    reasoning="fixture-effort",
                    cli_revision="fixture-cli-r1",
                    stderr=b"fixture-note-a\n",
                ),
                executable_path=alternate,
            )
        self.assertNotEqual(first.evidence_sha256, raw_drift.evidence_sha256)
        self.assertNotEqual(first.profile_hash, raw_drift.profile_hash)
        self.assertNotEqual(first.profile_hash, revision_drift.profile_hash)
        self.assertNotEqual(first.evidence_sha256, executable_drift.evidence_sha256)
        self.assertNotEqual(first.profile_hash, executable_drift.profile_hash)
        self.assertFalse(provider_adapters.capability_is_current(first, raw_drift))
        self.assertFalse(provider_adapters.capability_is_current(first, revision_drift))
        self.assertFalse(
            provider_adapters.capability_is_current(first, executable_drift)
        )


if __name__ == "__main__":
    unittest.main()
