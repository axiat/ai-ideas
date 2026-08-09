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

    def test_omission_and_unsupported_providers_fail_before_launch(self):
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
                with self.assertRaises(provider_adapters.ProviderResolutionError):
                    provider_adapters._resolve_command_intent_for_test(
                        self.registry,
                        surface,
                        provider,
                        model="vendor/fixture",
                        max_output_tokens=3072,
                        executable_lookup=lambda _: str(FAKE),
                        **kwargs,
                    )


if __name__ == "__main__":
    unittest.main()
