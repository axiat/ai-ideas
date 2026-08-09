#!/usr/bin/env python3
"""Regressions for provider-native portable output-token caps."""

import copy
import json
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import portable_agent
from lib import portable_stage
from lib import provider_adapters


REGISTRY = ROOT / "history/provider-adapters-v1.json"
FAKE = ROOT / "tests/fake_portable_stage_provider.py"


class PortableOutputTokenContractRegression(unittest.TestCase):
    def setUp(self):
        self.registry = provider_adapters.load_registry(REGISTRY)

    def _claude_intent(self, maximum=3072, executable=FAKE):
        return provider_adapters._resolve_command_intent_for_test(
            self.registry,
            "hunt",
            "claude",
            model="fixture-model",
            reasoning="high",
            max_output_tokens=maximum,
            executable_lookup=lambda _: str(executable),
        )

    def _prepare(self, root, intent):
        inputs = root / "inputs"
        inputs.mkdir(parents=True)
        brief = inputs / "generation_brief.json"
        policy = inputs / "generation_policy.md"
        brief.write_text('{"brief":"bounded"}\n', encoding="utf-8")
        policy.write_text("bounded policy\n", encoding="utf-8")
        return portable_stage.prepare_stage(
            intent,
            stage="generate",
            seat_id="output-token-contract-seat",
            serialized_prompt='{"schema_version":1,"stage":"generate"}\n',
            input_paths={
                "generation_brief.json": brief,
                "generation_policy.md": policy,
            },
            output_root=root / "published",
            state_root=root / "state",
        )

    def test_cap_is_bound_end_to_end_and_value_drift_is_rejected(self):
        intent = self._claude_intent()
        changed = self._claude_intent(2048)
        self.assertNotEqual(intent.profile_hash, changed.profile_hash)
        argv, environment = provider_adapters.render_command(
            intent,
            pathlib.Path("/portable-mirror"),
            "PROMPT",
            response_schema={"type": "object"},
        )
        self.assertEqual(environment, {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "3072"})
        self.assertNotIn("3072", argv)

        record = provider_adapters.command_intent_record(intent)
        drifted = copy.deepcopy(record)
        drifted["max_output_tokens"] = 2048
        with self.assertRaises(provider_adapters.ProviderResolutionError):
            provider_adapters.validate_command_intent_record(
                self.registry, drifted
            )

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            "os.environ", {"FAKE_PORTABLE_STAGE_MODE": "output-token-cap-audit"},
            clear=False,
        ):
            root = pathlib.Path(directory)
            provider = root / "claude"
            shutil.copy2(FAKE, provider)
            provider.chmod(0o700)
            e2e_intent = self._claude_intent(executable=provider)
            role = root / "role.md"
            role.write_bytes(portable_stage._ROLES["generate"].read_bytes())
            with mock.patch.object(
                portable_stage, "ROOT", root
            ), mock.patch.dict(
                portable_stage._ROLES, {"generate": role}
            ):
                prepared = self._prepare(root, e2e_intent)
                preflight = json.loads(
                    pathlib.Path(prepared["preflight_path"]).read_text(encoding="utf-8")
                )
                self.assertEqual(preflight["max_output_tokens"], 3072)
                self.assertEqual(
                    preflight["provider_command"]["environment"],
                    {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "3072"},
                )
                completion = portable_stage.run_stage(prepared, timeout_seconds=5)
                self.assertEqual(completion["max_output_tokens"], 3072)
                self.assertEqual(
                    completion["output_token_cap_semantics"],
                    "reasoning-and-visible-output",
                )
                self.assertTrue(portable_stage.verify_completion(prepared))

            with self.assertRaises(portable_agent.PortableAgentError) as caught:
                portable_agent.run_portable_stdout_attempt(
                    intent,
                    inputs=[],
                    prompt="{}",
                    response_schema={"type": "object"},
                    expected_response_attestation={
                        "schema_version": "portable-stage-response-attestation-v1",
                        "provider_request_binding_sha256": "a" * 64,
                        "serialized_prompt_sha256": "b" * 64,
                    },
                    state_root=pathlib.Path(directory) / "drift-state",
                    timeout_seconds=1,
                    max_output_tokens=2048,
                )
            self.assertEqual(caught.exception.code, "output_token_cap_unsupported")
            self.assertFalse((pathlib.Path(directory) / "drift-state").exists())

    def test_claude_provider_ceiling_is_enforced_before_issuance(self):
        intent = self._claude_intent(128_000)
        self.assertEqual(
            provider_adapters.require_native_output_token_cap(intent),
            128_000,
        )
        argv, environment = provider_adapters.render_command(
            intent,
            pathlib.Path("/portable-mirror"),
            "PROMPT",
            response_schema={"type": "object"},
        )
        self.assertEqual(
            argv,
            [
                str(FAKE),
                "--bare",
                "--dangerously-skip-permissions",
                "--output-format",
                "json",
                "--add-dir",
                "/portable-mirror",
                "--model",
                "fixture-model",
                "--effort",
                "high",
                "--json-schema",
                '{"type":"object"}',
                "-p",
                "PROMPT",
            ],
        )
        self.assertEqual(
            environment,
            {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "128000"},
        )

        with self.assertRaises(
            provider_adapters.ProviderResolutionError
        ) as caught:
            self._claude_intent(128_001)
        self.assertIn("128000", str(caught.exception))

        oversized_record = provider_adapters.command_intent_record(intent)
        oversized_record["max_output_tokens"] = 128_001
        with self.assertRaises(provider_adapters.ProviderResolutionError):
            provider_adapters.validate_command_intent_record(
                self.registry,
                oversized_record,
            )
        with self.assertRaises(provider_adapters.ProviderResolutionError):
            provider_adapters._render_command_fields(
                "claude",
                str(FAKE),
                "fixture-model",
                "high",
                pathlib.Path("/portable-mirror"),
                "PROMPT",
                '{"type":"object"}',
                max_output_tokens=128_001,
                output_token_cap_binding="provider-native-exact",
                output_token_cap_semantics="reasoning-and-visible-output",
            )

        probe_calls = []
        with self.assertRaises(provider_adapters.ProviderResolutionError):
            provider_adapters._resolve_provider_for_test(
                self.registry,
                "hunt",
                "claude",
                model="fixture-model",
                reasoning="high",
                max_output_tokens=128_001,
                executable_lookup=lambda _: str(FAKE),
                version_probe=lambda *args: probe_calls.append(args),
            )
        self.assertEqual(probe_calls, [])

    def test_unsupported_cap_can_retain_hard_complete_authority(self):
        evidence = {
            "cli_revision": "fixture-cli-v1",
            "serializer_revision": "portable-agent-command-v1",
            "effective_model": "fixture-model",
            "effective_reasoning": "high",
            "model_override_applied": True,
            "reasoning_override_applied": True,
            "immutable_capacity_identity": "fixture-capacity-v1",
        }
        capability = provider_adapters._resolve_provider(
            self.registry,
            "hunt",
            "codex",
            "fixture-model",
            "high",
            3072,
            executable_lookup=lambda _: str(FAKE),
            version_probe=lambda *args: evidence,
            issuance_scope="fixture-host",
            allow_hard_complete=True,
        )
        self.assertEqual(capability.output_token_cap_binding, "unsupported")
        self.assertTrue(capability.hard_complete_eligible)
        self.assertEqual(capability.authority, "hard-complete")

    def test_empty_content_violates_declared_min_length(self):
        schema = portable_stage._response_schema("generate")
        contract = portable_agent._validate_response_schema_contract(schema)
        value = {
            "schema_version": 1,
            "stage": "generate",
            "request_attestation": {
                "schema_version": "portable-stage-response-attestation-v1",
                "provider_request_binding_sha256": "a" * 64,
                "serialized_prompt_sha256": "b" * 64,
            },
            "artifacts": [
                {"artifact_kind": "generation-ideas-markdown", "content": ""}
            ],
        }
        with self.assertRaises(portable_agent.PortableAgentError) as caught:
            portable_agent._validate_response_value(value, contract)
        self.assertEqual(caught.exception.code, "schema_mismatch")

    def test_omission_fails_but_provider_default_budgets_are_recorded(self):
        omitted = provider_adapters._resolve_command_intent_for_test(
            self.registry,
            "hunt",
            "claude",
            model="fixture-model",
            reasoning="high",
            executable_lookup=lambda _: str(FAKE),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with self.assertRaises(portable_stage.PortableStageError) as caught:
                self._prepare(root, omitted)
            self.assertEqual(caught.exception.code, "output_token_cap_unsupported")
            self.assertFalse((root / "state").exists())

        for surface, provider in (
            ("hunt", "codex"),
            ("hunt", "kimi"),
            ("hunt", "grok"),
            ("awr", "opencode"),
            ("awr", "agy"),
        ):
            with self.subTest(provider=provider):
                kwargs = {}
                if provider in {"opencode", "agy"}:
                    kwargs["model_catalog_probe"] = lambda *_, p=provider: {
                        "schema_version": "provider-model-catalog-v1",
                        "provider": p,
                        "models": ["vendor/fixture"],
                        "probe_revision": "fixture-v1",
                        "catalog_sha256": provider_adapters._model_catalog_sha256(
                            p, ["vendor/fixture"]
                        ),
                    }
                    kwargs["version_probe"] = lambda *_: b"1.1.10\n"
                intent = provider_adapters._resolve_command_intent(
                    self.registry,
                    surface,
                    provider,
                    "vendor/fixture",
                    None,
                    3072,
                    executable_lookup=lambda _: str(FAKE),
                    **kwargs,
                )
                self.assertEqual(intent.max_output_tokens, 3072)
                self.assertFalse(intent.hard_complete_eligible)
                self.assertEqual(intent.authority, "shadow-only")
                self.assertEqual(
                    intent.output_token_cap_binding, "unsupported"
                )
                self.assertIsNone(intent.output_token_cap_semantics)
                self.assertEqual(
                    provider_adapters.require_portable_output_token_budget(
                        intent, 3072
                    ),
                    3072,
                )
                argv, environment = provider_adapters.render_command(
                    intent,
                    pathlib.Path("/portable-mirror"),
                    "PROMPT",
                    response_schema=(
                        {"type": "object"} if provider == "agy" else None
                    ),
                )
                self.assertTrue(argv)
                self.assertNotIn("CLAUDE_CODE_MAX_OUTPUT_TOKENS", environment)
                self.assertNotIn("FAKE_PROVIDER_MAX_OUTPUT_TOKENS", environment)


if __name__ == "__main__":
    unittest.main()
