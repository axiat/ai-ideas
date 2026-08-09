#!/usr/bin/env python3
"""Adversarial contracts for the provider-neutral portable boundary."""

import copy
import dataclasses
import hashlib
import json
import os
import pathlib
import stat
import struct
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import direction_contract
from lib import history_contract_v2
from lib import history_stage_adapter
from lib import portable_agent
from lib import portable_stage
from lib import provider_adapters


REGISTRY = ROOT / "history/provider-adapters-v1.json"
FAKE_STAGE = ROOT / "tests/fake_portable_stage_provider.py"
FAKE_FILE = ROOT / "tests/fake_portable_agent.py"


class AlwaysEqualText(str):
    __hash__ = str.__hash__

    def __eq__(self, other):
        return True


def _probe(provider, executable_path, model, reasoning):
    return {
        "cli_revision": "fake-cli-v1",
        "serializer_revision": "portable-agent-command-v1",
        "effective_model": model or "fake-default-model",
        "effective_reasoning": reasoning or "fake-default-reasoning",
        "model_override_applied": True,
        "reasoning_override_applied": True,
        "immutable_capacity_identity": "fake-capacity-v1",
    }


class ProviderIdentityHardeningSmoke(unittest.TestCase):
    def setUp(self):
        self.registry = provider_adapters.load_registry(REGISTRY)

    def test_public_resolvers_reject_caller_owned_lookup_and_probe(self):
        lookup_calls = []
        probe_calls = []

        def forged_lookup(name):
            lookup_calls.append(name)
            return "/bin/echo"

        with self.assertRaises(provider_adapters.ProviderResolutionError):
            provider_adapters.resolve_command_intent(
                self.registry,
                "hunt",
                "codex",
                executable_lookup=forged_lookup,
            )

        def forged_probe(*args):
            probe_calls.append(args)
            return {
                "cli_revision": "forged-cli-v1",
                "serializer_revision": "portable-agent-command-v1",
                "effective_model": "forged-model",
                "effective_reasoning": "high",
                "model_override_applied": True,
                "reasoning_override_applied": True,
                "immutable_capacity_identity": "forged-capacity",
                "evidence_sha256": "a" * 64,
            }

        with self.assertRaises(provider_adapters.ProviderResolutionError):
            provider_adapters.resolve_provider(
                self.registry,
                "hunt",
                "codex",
                version_probe=forged_probe,
            )
        with self.assertRaises(provider_adapters.ProviderResolutionError):
            provider_adapters.resolve_provider(
                self.registry,
                "hunt",
                "codex",
                executable_lookup=forged_lookup,
            )
        with self.assertRaises(provider_adapters.ProviderResolutionError):
            provider_adapters.resolve_pool(
                self.registry,
                "hunt",
                ["codex"],
                version_probe=forged_probe,
            )
        with self.assertRaises(provider_adapters.ProviderResolutionError):
            provider_adapters.command_intent_from_record(
                self.registry,
                {},
                executable_lookup=forged_lookup,
            )
        with self.assertRaises(provider_adapters.ProviderResolutionError):
            provider_adapters.load_command_intent(
                ROOT / "missing-provider-command.json",
                self.registry,
                executable_lookup=forged_lookup,
            )
        self.assertEqual(lookup_calls, [])
        self.assertEqual(probe_calls, [])

    def test_private_fake_resolution_is_shadow_only_and_host_seals_evidence(self):
        resolve_intent = getattr(
            provider_adapters, "_resolve_command_intent_for_test", None
        )
        resolve_capability = getattr(
            provider_adapters, "_resolve_provider_for_test", None
        )
        self.assertTrue(callable(resolve_intent))
        self.assertTrue(callable(resolve_capability))

        intent = resolve_intent(
            self.registry,
            "hunt",
            "codex",
            executable_lookup=lambda _: str(FAKE_STAGE),
        )
        self.assertTrue(provider_adapters.command_intent_is_issued(intent))
        self.assertEqual(intent.authority, "shadow-only")
        self.assertFalse(intent.hard_complete_eligible)

        def fake_probe(*_, effective_model="fake-model-v1"):
            return {
                "cli_revision": "fake-cli-v1",
                "serializer_revision": "portable-agent-command-v1",
                "effective_model": effective_model,
                "effective_reasoning": "high",
                "model_override_applied": True,
                "reasoning_override_applied": True,
                "immutable_capacity_identity": "fake-capacity-v1",
            }

        first = resolve_capability(
            self.registry,
            "hunt",
            "codex",
            executable_lookup=lambda _: str(FAKE_FILE),
            version_probe=fake_probe,
        )
        changed = resolve_capability(
            self.registry,
            "hunt",
            "codex",
            executable_lookup=lambda _: str(FAKE_FILE),
            version_probe=lambda *args: fake_probe(
                *args, effective_model="fake-model-v2"
            ),
        )
        other_executable = resolve_capability(
            self.registry,
            "hunt",
            "codex",
            executable_lookup=lambda _: str(FAKE_STAGE),
            version_probe=fake_probe,
        )
        for capability in (first, changed, other_executable):
            self.assertTrue(provider_adapters.capability_is_issued(capability))
            self.assertEqual(capability.authority, "shadow-only")
            self.assertFalse(capability.hard_complete_eligible)
            self.assertEqual(len(capability.evidence_sha256), 64)
        executable_info = FAKE_FILE.resolve().lstat()
        expected_material = {
            "schema_version": "provider-capability-evidence-v1",
            "issuance_scope": "test-only-shadow",
            "provider": "codex",
            "surface": "hunt",
            "executable": "codex",
            "executable_path": str(FAKE_FILE.resolve()),
            "executable_identity": {
                "device": executable_info.st_dev,
                "inode": executable_info.st_ino,
                "mode": executable_info.st_mode,
                "size": executable_info.st_size,
                "mtime_ns": executable_info.st_mtime_ns,
                "ctime_ns": executable_info.st_ctime_ns,
            },
            "requested_model": None,
            "requested_reasoning": None,
            "effective_model": "fake-model-v1",
            "effective_reasoning": "high",
            "model_override_applied": True,
            "reasoning_override_applied": True,
            "immutable_capacity_identity": "fake-capacity-v1",
            "cli_revision": "fake-cli-v1",
            "serializer_revision": "portable-agent-command-v1",
            "grammar_revision": "codex-portable-v2",
        }
        self.assertEqual(
            first.evidence_sha256,
            history_contract_v2.framed_sha256(
                "provider-capability-evidence-v1",
                history_contract_v2.canonical_bytes(expected_material),
            ),
        )
        self.assertNotEqual(first.evidence_sha256, changed.evidence_sha256)
        self.assertNotEqual(
            first.evidence_sha256, other_executable.evidence_sha256
        )

    def test_non_nfc_model_and_reasoning_fail_before_executable_lookup(self):
        for field in ("model", "reasoning"):
            calls = []
            arguments = {field: "e\u0301"}
            with self.subTest(field=field):
                with self.assertRaises(provider_adapters.ProviderResolutionError):
                    provider_adapters._resolve_command_intent_for_test(
                        self.registry,
                        "hunt",
                        "codex",
                        executable_lookup=lambda name: calls.append(name),
                        **arguments,
                    )
                self.assertEqual(calls, [])

    def test_dataclass_relabeling_cannot_enter_command_rendering(self):
        intent = provider_adapters._resolve_command_intent_for_test(
            self.registry,
            "hunt",
            "codex",
            executable_lookup=lambda _: str(FAKE_STAGE),
        )
        forged_intent = dataclasses.replace(intent, provider="grok")
        with self.assertRaises(provider_adapters.ProviderResolutionError):
            provider_adapters.render_command(
                forged_intent, pathlib.Path("/mirror"), "PROMPT"
            )

        capability = provider_adapters._resolve_provider_for_test(
            self.registry,
            "hunt",
            "codex",
            executable_lookup=lambda _: str(FAKE_FILE),
            version_probe=_probe,
        )
        forged_capability = dataclasses.replace(
            capability,
            authority="shadow-only",
        )
        with self.assertRaises(provider_adapters.ProviderResolutionError):
            provider_adapters.render_command(
                forged_capability, pathlib.Path("/mirror"), "PROMPT"
            )

    def test_in_place_object_mutation_invalidates_resolver_issuance(self):
        intent = provider_adapters._resolve_command_intent_for_test(
            self.registry,
            "hunt",
            "codex",
            executable_lookup=lambda _: str(FAKE_FILE),
        )
        self.assertTrue(provider_adapters.command_intent_is_issued(intent))
        object.__setattr__(intent, "executable_path", str(FAKE_STAGE))
        self.assertFalse(provider_adapters.command_intent_is_issued(intent))
        with self.assertRaises(provider_adapters.ProviderResolutionError):
            provider_adapters.command_intent_record(intent)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            brief = root / "brief.json"
            policy = root / "policy.md"
            brief.write_text('{}\n', encoding="utf-8")
            policy.write_text('bounded\n', encoding="utf-8")
            with self.assertRaises(portable_stage.PortableStageError):
                portable_stage.prepare_stage(
                    intent,
                    stage="generate",
                    seat_id="mutated-intent",
                    serialized_prompt='{"stage":"generate"}\n',
                    input_paths={
                        "generation_brief.json": brief,
                        "generation_policy.md": policy,
                    },
                    output_root=root / "output",
                    state_root=root / "state",
                )
            self.assertFalse((root / "state").exists())

        capability = provider_adapters._resolve_provider_for_test(
            self.registry,
            "hunt",
            "codex",
            executable_lookup=lambda _: str(FAKE_FILE),
            version_probe=_probe,
        )
        self.assertTrue(provider_adapters.capability_is_issued(capability))
        object.__setattr__(capability, "_private_override", True)
        self.assertFalse(provider_adapters.capability_is_issued(capability))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(portable_agent.PortableAgentError):
                portable_agent.run_portable_attempt(
                    capability,
                    inputs=[],
                    output_contract={
                        "path": "output/result.json",
                        "max_bytes": 256,
                        "sha256": None,
                        "allowed_fields": ["request_id", "status"],
                        "required_fields": ["request_id", "status"],
                        "field_types": {
                            "request_id": "string",
                            "status": "string",
                        },
                        "forbid_extra_files": True,
                    },
                    prompt='{"mode":"success","request_id":"request-1"}',
                    state_root=pathlib.Path(directory) / "state",
                    timeout_seconds=2,
                )
            self.assertFalse((pathlib.Path(directory) / "state").exists())

    def test_text_subclass_cannot_spoof_resolver_or_issuance_snapshot(self):
        lookups = []
        with self.assertRaises(provider_adapters.ProviderResolutionError):
            provider_adapters._resolve_command_intent_for_test(
                self.registry,
                "hunt",
                "codex",
                model=AlwaysEqualText("MODEL-A"),
                executable_lookup=lambda name: lookups.append(name),
            )
        self.assertEqual(lookups, [])

        intent = provider_adapters._resolve_command_intent_for_test(
            self.registry,
            "hunt",
            "codex",
            model="MODEL-A",
            executable_lookup=lambda _: str(FAKE_STAGE),
        )
        object.__setattr__(
            intent, "requested_model", AlwaysEqualText("MODEL-B")
        )
        self.assertFalse(provider_adapters.command_intent_is_issued(intent))
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            brief = root / "brief.json"
            policy = root / "policy.md"
            brief.write_text('{}\n', encoding="utf-8")
            policy.write_text('bounded\n', encoding="utf-8")
            with self.assertRaises(portable_stage.PortableStageError):
                portable_stage.prepare_stage(
                    intent,
                    stage="generate",
                    seat_id="equality-spoof",
                    serialized_prompt='{"stage":"generate"}\n',
                    input_paths={
                        "generation_brief.json": brief,
                        "generation_policy.md": policy,
                    },
                    output_root=root / "output",
                    state_root=root / "state",
                )
            self.assertFalse((root / "state").exists())

    def test_probe_text_subclasses_and_caller_evidence_hash_are_rejected(self):
        for field in (
            "cli_revision",
            "effective_model",
        ):
            with self.subTest(field=field):
                def bad_probe(*args, field=field):
                    evidence = _probe(*args)
                    evidence[field] = AlwaysEqualText(evidence[field])
                    return evidence

                with self.assertRaises(provider_adapters.ProviderResolutionError):
                    provider_adapters._resolve_provider_for_test(
                        self.registry,
                        "hunt",
                        "codex",
                        executable_lookup=lambda _: str(FAKE_FILE),
                        version_probe=bad_probe,
                    )

        def probe_with_caller_hash(*args):
            return {**_probe(*args), "evidence_sha256": "a" * 64}

        with self.assertRaises(provider_adapters.ProviderResolutionError):
            provider_adapters._resolve_provider_for_test(
                self.registry,
                "hunt",
                "codex",
                executable_lookup=lambda _: str(FAKE_FILE),
                version_probe=probe_with_caller_hash,
            )

    def test_compact_profile_validation_is_pure_and_closed(self):
        intent = provider_adapters._resolve_command_intent_for_test(
            self.registry,
            "hunt",
            "codex",
            model="MODEL",
            reasoning="high",
            executable_lookup=lambda _: str(FAKE_STAGE),
        )
        descriptor = {
            "surface": "hunt",
            "provider": "codex",
            "requested_model": "MODEL",
            "requested_reasoning": "high",
            "effective_model": None,
            "effective_reasoning": None,
            "default_probe_revision": None,
            "model_catalog_probe_revision": None,
            "model_catalog_sha256": None,
            "max_output_tokens": None,
            "output_token_cap_binding": "unsupported",
            "output_token_cap_semantics": None,
            "execution_request_profile_hash": (
                intent.execution_request_profile_hash
            ),
        }
        validate = getattr(
            provider_adapters, "validate_command_profile_descriptor", None
        )
        self.assertTrue(callable(validate))
        self.assertEqual(validate(self.registry, descriptor), descriptor)
        with self.assertRaises(provider_adapters.ProviderResolutionError):
            validate(self.registry, {**descriptor, "_private": True})


class PortableStageHardeningSmoke(unittest.TestCase):
    def _intent(
        self,
        surface="hunt",
        provider="codex",
        executable_path=FAKE_STAGE,
    ):
        model = {
            "agy": "gemini/fixture-model",
            "opencode": "openai/fixture-model",
        }.get(provider, "MODEL")
        return provider_adapters._resolve_command_intent_for_test(
            provider_adapters.load_registry(REGISTRY),
            surface,
            provider,
            model=model if provider in {"agy", "opencode"} else (
                None if provider == "kimi" else "MODEL"
            ),
            reasoning=None if provider == "kimi" else "high",
            max_output_tokens=3072,
            executable_lookup=lambda _: str(executable_path),
            model_catalog_probe=(
                provider_adapters._host_model_catalog_probe
                if provider in {"agy", "opencode"}
                else None
            ),
            default_identity_probe=(
                provider_adapters._host_default_identity_probe
                if provider == "opencode"
                else None
            ),
            version_probe=(
                (lambda *_: b"1.1.10\n") if provider == "agy" else None
            ),
        )

    def _capability(self):
        return provider_adapters._resolve_provider_for_test(
            provider_adapters.load_registry(REGISTRY),
            "hunt",
            "codex",
            model="MODEL",
            reasoning="high",
            max_output_tokens=3072,
            executable_lookup=lambda _: str(FAKE_STAGE),
            version_probe=_probe,
        )

    def _prepare(
        self, root, *, stage="generate", directed=False, provider="codex"
    ):
        inputs = root / "inputs"
        inputs.mkdir(parents=True)
        if stage == "generate":
            (inputs / "generation_brief.json").write_text(
                '{"brief":"bounded"}\n', encoding="utf-8"
            )
            (inputs / "generation_policy.md").write_text(
                "bounded policy\n", encoding="utf-8"
            )
            input_paths = {
                "generation_brief.json": inputs / "generation_brief.json",
                "generation_policy.md": inputs / "generation_policy.md",
            }
            if directed:
                direction = inputs / "direction_constraint.json"
                direction.write_bytes(
                    direction_contract.canonical_bytes(
                        json.loads(
                            (
                                ROOT
                                / "directions/dynamic-spatial-memory-vla-v1.json"
                            ).read_text(encoding="utf-8")
                        )
                    )
                )
                input_paths["direction_constraint.json"] = direction
            serialized = '{"schema_version":1,"stage":"generate"}\n'
            surface = "hunt"
        elif stage == "awr-research":
            (inputs / "idea.md").write_text("bounded idea\n", encoding="utf-8")
            input_paths = {"idea.md": inputs / "idea.md"}
            serialized = '{"schema_version":1,"stage":"awr-research"}\n'
            surface = "awr"
        else:
            raise AssertionError(stage)
        executable_path = FAKE_STAGE
        if provider in {"agy", "grok", "claude", "opencode", "kimi", "codex"}:
            # Bind argv[0] basename to the provider under test for transport
            # selection inside the fake executable.
            if provider != "codex":
                executable_path = root / provider
                executable_path.write_bytes(FAKE_STAGE.read_bytes())
                executable_path.chmod(0o755)
        prepared = portable_stage.prepare_stage(
            self._intent(surface, provider, executable_path),
            stage=stage,
            seat_id=stage + "-seat-1",
            serialized_prompt=serialized,
            input_paths=input_paths,
            output_root=root / "published",
            state_root=root / "state",
        )
        return prepared

    @staticmethod
    def _canonical(value):
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    @staticmethod
    def _framed_sha256(domain, *parts):
        digest = hashlib.sha256()
        for part in (domain.encode("utf-8"),) + parts:
            digest.update(struct.pack(">Q", len(part)))
            digest.update(part)
        return digest.hexdigest()

    def _rewrite_public_receipts(self, root, descriptor, mutate_preflight):
        changed = copy.deepcopy(descriptor)
        preflight_path = root / changed["preflight"]["path"]
        completion_path = root / changed["completion"]["path"]
        preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
        mutate_preflight(preflight)
        preflight_raw = self._canonical(preflight)
        preflight_path.write_bytes(preflight_raw)
        preflight_sha = hashlib.sha256(preflight_raw).hexdigest()

        completion = json.loads(completion_path.read_text(encoding="utf-8"))
        completion["preflight_sha256"] = preflight_sha
        completion.pop("completion_id")
        completion["completion_id"] = self._framed_sha256(
            "portable-stage-completion-id-v1",
            self._canonical(completion),
        )
        completion_raw = self._canonical(completion)
        completion_path.write_bytes(completion_raw)
        changed["preflight"]["sha256"] = preflight_sha
        changed["completion"]["sha256"] = hashlib.sha256(
            completion_raw
        ).hexdigest()
        changed["completion"]["completion_id"] = completion["completion_id"]
        return changed

    def test_non_grok_transport_instructions_are_closed_and_binding_covered(self):
        expected_transport = {
            "schema_version": "portable-stage-transport-instructions-v1",
            "precedence": (
                "role_text, declared_input_texts, host_output_contract, "
                "response_schema, and these transport instructions are the "
                "authoritative stage contract. They override any conflicting "
                "output-location or file-writing wording inside role_text."
            ),
            "role": (
                "Follow role_text and host_output_contract exactly when "
                "building artifact content. Disk paths role.md and input/* "
                "are byte-identical audit copies; do not require file tools."
            ),
            "mirror": (
                "Do not create, modify, or delete any file in the mirror."
            ),
            "stdout": (
                "Return exactly one JSON object matching response_schema as the "
                "structured final result. The Codex CLI writes that final result "
                "through its host-configured output-last-message path; stdout is "
                "diagnostic transport and is never an output fallback. The harness "
                "parses and canonicalizes the final JSON. Do not put Markdown fences "
                "or narration inside the structured value."
            ),
            "request_attestation": (
                "Copy request_binding.provider_request_binding_sha256 and "
                "request_binding.serialized_prompt_sha256 exactly into "
                "request_attestation."
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(root)
            request = json.loads(prepared["provider_request"])

        self.assertIn("transport_instructions", request)
        self.assertEqual(
            request["transport_instructions"], expected_transport
        )
        self.assertEqual(
            set(request),
            {
                "schema_version",
                "stage",
                "seat_id",
                "max_output_tokens",
                "output_token_cap_semantics",
                "serialized_prompt",
                "role_path",
                "role_text",
                "declared_inputs",
                "declared_input_sha256s",
                "declared_input_texts",
                "role_sha256",
                "response_schema",
                "host_output_contract",
                "transport_instructions",
                "request_binding",
            },
        )
        self.assertEqual(
            request["role_text"],
            (ROOT / "roles/generate.md").read_text(encoding="utf-8"),
        )
        self.assertIn("generation_brief.json", request["declared_input_texts"])
        self.assertEqual(
            request["host_output_contract"]["artifact_kind"],
            "generation-ideas-markdown",
        )
        binding = request["request_binding"]
        base = {
            key: value
            for key, value in request.items()
            if key != "request_binding"
        }
        expected_binding = history_contract_v2.framed_sha256(
            "portable-stage-request-base-v1",
            self._canonical(base),
        )
        self.assertEqual(
            binding["provider_request_binding_sha256"], expected_binding
        )
        self.assertEqual(
            prepared["provider_request_binding_sha256"], expected_binding
        )
        self.assertEqual(
            prepared["provider_request_sha256"],
            hashlib.sha256(self._canonical(request)).hexdigest(),
        )

        changed_base = copy.deepcopy(base)
        changed_base["transport_instructions"]["mirror"] = (
            "The provider may modify the mirror."
        )
        changed_binding = history_contract_v2.framed_sha256(
            "portable-stage-request-base-v1",
            self._canonical(changed_base),
        )
        changed_request = copy.deepcopy(changed_base)
        changed_request["request_binding"] = {
            **binding,
            "provider_request_binding_sha256": changed_binding,
        }
        changed_wire_sha256 = hashlib.sha256(
            self._canonical(changed_request)
        ).hexdigest()
        self.assertNotEqual(changed_binding, expected_binding)
        self.assertNotEqual(
            changed_wire_sha256, prepared["provider_request_sha256"]
        )

    def test_grok_transport_instructions_bind_terminal_response_fence(self):
        expected_transport = {
            "schema_version": "portable-stage-transport-instructions-v1",
            "precedence": (
                "role_text, declared_input_texts, host_output_contract, "
                "response_schema, and these transport instructions are the "
                "authoritative stage contract. They override any conflicting "
                "output-location or file-writing wording inside role_text."
            ),
            "role": (
                "Follow role_text and host_output_contract exactly when "
                "building artifact content. Disk paths role.md and input/* "
                "are byte-identical audit copies; do not require file tools."
            ),
            "mirror": (
                "Do not create, modify, or delete any file in the mirror."
            ),
            "stdout": (
                "Make the FINAL ASSISTANT RESPONSE exactly one UTF-8 NFC "
                "canonical JSON object inside exactly one Markdown fence. "
                "The opening fence must be the exact lowercase bytes "
                "```json followed by LF. The JSON object must use "
                "lexicographically sorted object keys, compact separators, "
                "and exactly one trailing LF; that LF must be followed "
                "immediately by the terminal closing bytes ```. The object "
                "must match response_schema. Do not emit any bytes before "
                "the opening fence or after the closing fence in the FINAL "
                "ASSISTANT RESPONSE, and do not emit triple-backtick bytes "
                "in any earlier assistant response."
            ),
            "request_attestation": (
                "Copy request_binding.provider_request_binding_sha256 and "
                "request_binding.serialized_prompt_sha256 exactly into "
                "request_attestation."
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(root, provider="grok")
            request = json.loads(prepared["provider_request"])

        self.assertEqual(
            request["transport_instructions"], expected_transport
        )
        base = {
            key: value
            for key, value in request.items()
            if key != "request_binding"
        }
        expected_binding = history_contract_v2.framed_sha256(
            "portable-stage-request-base-v1",
            self._canonical(base),
        )
        self.assertEqual(
            request["request_binding"][
                "provider_request_binding_sha256"
            ],
            expected_binding,
        )
        self.assertEqual(
            prepared["provider_request_binding_sha256"], expected_binding
        )

    def test_agy_transport_instructions_bind_structured_result(self):
        expected_stdout = (
            "Return exactly one JSON object matching response_schema as the "
            "structured final result. The Agy CLI owns the outer stdout JSON; "
            "only a status=SUCCESS structured_output member is eligible for "
            "import. Do not put Markdown fences or narration inside the "
            "structured value."
        )
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(
                root, stage="awr-research", provider="agy"
            )
            request = json.loads(prepared["provider_request"])
        self.assertEqual(
            request["transport_instructions"]["stdout"], expected_stdout
        )
        base = {
            key: value
            for key, value in request.items()
            if key != "request_binding"
        }
        self.assertEqual(
            request["request_binding"][
                "provider_request_binding_sha256"
            ],
            history_contract_v2.framed_sha256(
                "portable-stage-request-base-v1",
                self._canonical(base),
            ),
        )

    def test_claude_transport_instructions_bind_structured_result(self):
        expected_stdout = (
            "Return exactly one JSON object matching response_schema as the "
            "structured final result. The Claude CLI owns the outer stdout JSON; "
            "only subtype=success structured_output is eligible for import. "
            "Do not put Markdown fences or narration inside the structured value."
        )
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(
                root, stage="awr-research", provider="claude"
            )
            request = json.loads(prepared["provider_request"])
        self.assertEqual(
            request["transport_instructions"]["stdout"], expected_stdout
        )
        base = {
            key: value
            for key, value in request.items()
            if key != "request_binding"
        }
        self.assertEqual(
            request["request_binding"][
                "provider_request_binding_sha256"
            ],
            history_contract_v2.framed_sha256(
                "portable-stage-request-base-v1",
                self._canonical(base),
            ),
        )

    def test_all_portable_stage_schemas_pin_integer_version_with_bounds(self):
        self.assertEqual(len(portable_stage._ROLES), 7)
        for stage in portable_stage._ROLES:
            with self.subTest(stage=stage):
                self.assertEqual(
                    portable_stage._response_schema(stage)["properties"][
                        "schema_version"
                    ],
                    {"maximum": 1, "minimum": 1, "type": "integer"},
                )

        for stage in ("generate", "history-compare", "review", "meta"):
            with self.subTest(legacy_stage=stage):
                self.assertEqual(
                    history_stage_adapter.stage_response_schema(stage)[
                        "properties"
                    ]["schema_version"],
                    {"enum": [1], "type": "integer"},
                )

    def test_closed_response_schema_accepts_equal_type_exact_integer_bounds(self):
        schema = portable_stage._response_schema("awr-research")
        contract = portable_agent._validate_response_schema_contract(schema)
        self.assertEqual(contract["schema_version"], 1)

    def test_closed_response_schema_rejects_malformed_version_bounds(self):
        schema = portable_stage._response_schema("awr-research")
        malformed_versions = {
            "missing-minimum": {"maximum": 1, "type": "integer"},
            "missing-maximum": {"minimum": 1, "type": "integer"},
            "extra-enum": {
                "enum": [1],
                "maximum": 1,
                "minimum": 1,
                "type": "integer",
            },
            "extra-const": {
                "const": 1,
                "maximum": 1,
                "minimum": 1,
                "type": "integer",
            },
            "unequal-bounds": {
                "maximum": 2,
                "minimum": 1,
                "type": "integer",
            },
            "equal-zero-bounds": {
                "maximum": 0,
                "minimum": 0,
                "type": "integer",
            },
            "equal-two-bounds": {
                "maximum": 2,
                "minimum": 2,
                "type": "integer",
            },
            "boolean-bound": {
                "maximum": 1,
                "minimum": True,
                "type": "integer",
            },
            "float-bound": {
                "maximum": 1.0,
                "minimum": 1,
                "type": "integer",
            },
        }
        for name, version in malformed_versions.items():
            with self.subTest(name=name):
                changed = copy.deepcopy(schema)
                changed["properties"]["schema_version"] = version
                with self.assertRaises(
                    portable_agent.PortableAgentError
                ) as caught:
                    portable_agent._validate_response_schema_contract(
                        changed
                    )
                self.assertEqual(
                    caught.exception.code, "invalid_response_schema"
                )

    def test_inner_schema_version_requires_exact_bounded_integer(self):
        contract = portable_agent._validate_response_schema_contract(
            portable_stage._response_schema("awr-research")
        )
        value = {
            "artifacts": [
                {
                    "artifact_kind": "awr-draft-markdown",
                    "content": "bounded\n",
                }
            ],
            "request_attestation": {
                "provider_request_binding_sha256": "a" * 64,
                "schema_version": (
                    "portable-stage-response-attestation-v1"
                ),
                "serialized_prompt_sha256": "b" * 64,
            },
            "schema_version": 1,
            "stage": "awr-research",
        }
        portable_agent._validate_response_value(value, contract)
        for version in (True, 0, 2, 1.0):
            with self.subTest(version=version):
                changed = copy.deepcopy(value)
                changed["schema_version"] = version
                with self.assertRaises(
                    portable_agent.PortableAgentError
                ) as caught:
                    portable_agent._validate_response_value(
                        changed, contract
                    )
                self.assertEqual(caught.exception.code, "schema_mismatch")

    def test_agy_numeric_enum_workaround_preserves_schema_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            marker = root / "schema-before-prompt.json"
            log = root / "provider.jsonl"
            prepared = self._prepare(
                root, stage="awr-research", provider="agy"
            )
            request_schema = json.loads(prepared["provider_request"])[
                "response_schema"
            ]
            captured = {}
            real_frozen_schema = portable_stage._frozen_response_schema
            real_workload = portable_agent.run_portable_stdout_attempt
            real_parse = portable_agent._parse_agy_transport

            def capture_frozen_schema(*args, **kwargs):
                value = real_frozen_schema(*args, **kwargs)
                captured["frozen"] = value
                return value

            def capture_workload(*args, **kwargs):
                captured["launch"] = kwargs["response_schema"]
                return real_workload(*args, **kwargs)

            def capture_outer(raw, response_schema):
                captured["outer"] = json.loads(raw.decode("utf-8"))[
                    "json_schema"
                ]
                captured["decoder"] = response_schema
                return real_parse(raw, response_schema)

            with mock.patch.dict(
                os.environ,
                {
                    "FAKE_PORTABLE_PREPROMPT_MARKER": str(marker),
                    "FAKE_PORTABLE_STAGE_LOG": str(log),
                    "FAKE_PORTABLE_STAGE_MODE": (
                        "agy-reject-numeric-enum"
                    ),
                },
                clear=False,
            ), mock.patch.object(
                portable_stage,
                "_frozen_response_schema",
                side_effect=capture_frozen_schema,
            ), mock.patch.object(
                portable_agent,
                "run_portable_stdout_attempt",
                side_effect=capture_workload,
            ), mock.patch.object(
                portable_agent,
                "_parse_agy_transport",
                side_effect=capture_outer,
            ):
                completion = portable_stage.run_stage(
                    prepared, timeout_seconds=2
                )

            expected_raw = self._canonical(request_schema)
            self.assertEqual(marker.read_bytes(), expected_raw[:-1])
            self.assertIs(captured["launch"], captured["frozen"])
            self.assertIs(captured["decoder"], captured["launch"])
            for observed in (
                captured["frozen"],
                captured["launch"],
                captured["outer"],
            ):
                self.assertEqual(self._canonical(observed), expected_raw)
            record = json.loads(log.read_text(encoding="utf-8"))
            self.assertTrue(record["structured_transport_valid"])
            imported = pathlib.Path(prepared["state_root"]) / "imports" / (
                completion["model_envelope_sha256"] + ".json"
            )
            self.assertTrue(imported.is_file())
            self.assertTrue((root / "published/draft.md").is_file())
            self.assertTrue(
                pathlib.Path(prepared["completion_path"]).is_file()
            )

    def test_agy_pre_prompt_schema_rejection_keeps_only_preflight_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            marker = root / "provider-rejected"
            prepared = self._prepare(
                root, stage="awr-research", provider="agy"
            )
            with mock.patch.dict(
                os.environ,
                {
                    "FAKE_PORTABLE_PREPROMPT_MARKER": str(marker),
                    "FAKE_PORTABLE_STAGE_MODE": (
                        "agy-reject-schema-before-prompt"
                    ),
                },
                clear=False,
            ):
                with self.assertRaises(
                    portable_stage.PortableStageError
                ) as caught:
                    portable_stage.run_stage(
                        prepared, timeout_seconds=2
                    )
            self.assertEqual(caught.exception.code, "nonzero_exit")
            self.assertEqual(marker.read_bytes(), b"rejected-before-prompt\n")
            state = pathlib.Path(prepared["state_root"])
            self.assertTrue(pathlib.Path(prepared["preflight_path"]).is_file())
            self.assertFalse(
                pathlib.Path(prepared["completion_path"]).exists()
            )
            self.assertFalse((state / "imports").exists())
            self.assertFalse(any(state.glob("attempt-*")))
            self.assertFalse(pathlib.Path(prepared["output_root"]).exists())

    def test_stage_requires_exact_builtin_text_before_input_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            state = root / "state"
            with self.assertRaises(portable_stage.PortableStageError) as caught:
                portable_stage.prepare_stage(
                    self._intent(),
                    stage=AlwaysEqualText("generate"),
                    seat_id="generate-seat-stage-subclass",
                    serialized_prompt='{"stage":"generate"}\n',
                    input_paths={
                        "generation_brief.json": root / "must-not-be-read.json",
                    },
                    output_root=root / "published",
                    state_root=state,
                )
            self.assertEqual(caught.exception.code, "unsupported_stage")
            self.assertFalse(state.exists())

    def test_verified_capability_cannot_be_relabelled_as_portable_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inputs = root / "inputs"
            inputs.mkdir()
            brief = inputs / "generation_brief.json"
            policy = inputs / "generation_policy.md"
            brief.write_text('{}\n', encoding="utf-8")
            policy.write_text('bounded\n', encoding="utf-8")
            with self.assertRaises(portable_stage.PortableStageError) as caught:
                portable_stage.prepare_stage(
                    self._capability(),
                    stage="generate",
                    seat_id="generate-seat-capability",
                    serialized_prompt='{"stage":"generate"}\n',
                    input_paths={
                        "generation_brief.json": brief,
                        "generation_policy.md": policy,
                    },
                    output_root=root / "published",
                    state_root=root / "state",
                )
            self.assertEqual(caught.exception.code, "invalid_provider_request")
            self.assertFalse((root / "state").exists())

    def test_prepared_stage_is_opaque_and_cannot_drop_direction_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(root, directed=True)
            with self.assertRaises(TypeError):
                prepared["input_paths"].pop("direction_constraint.json")

            mutated = dict(prepared)
            for field in (
                "input_paths",
                "input_sha256s",
                "declared_input_bytes",
            ):
                mutated[field] = dict(mutated[field])
                mutated[field].pop("direction_constraint.json")
            with self.assertRaises(portable_stage.PortableStageError) as caught:
                portable_stage.run_stage(mutated, timeout_seconds=2)
            self.assertEqual(caught.exception.code, "invalid_prepared_stage")
            self.assertFalse((root / "published").exists())

    def test_provider_request_and_launch_share_frozen_response_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(root, stage="awr-research")
            request = json.loads(prepared["provider_request"])
            real_workload = portable_agent.run_portable_stdout_attempt
            real_frozen_schema = portable_stage._frozen_response_schema
            frozen = {}

            def capture_frozen_schema(*args, **kwargs):
                value = real_frozen_schema(*args, **kwargs)
                frozen["value"] = value
                return value

            with mock.patch.object(
                portable_stage,
                "_frozen_response_schema",
                side_effect=capture_frozen_schema,
            ), mock.patch.object(
                portable_agent,
                "run_portable_stdout_attempt",
                wraps=real_workload,
            ) as workload:
                portable_stage.run_stage(prepared, timeout_seconds=2)
            launched_schema = workload.call_args.kwargs["response_schema"]
            self.assertIs(launched_schema, frozen["value"])
            self.assertEqual(launched_schema, request["response_schema"])
            self.assertEqual(
                portable_stage._canonical_bytes(launched_schema),
                portable_stage._canonical_bytes(request["response_schema"]),
            )

    def test_executable_substitution_cannot_launch_from_copied_prepared_value(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(root)
            mutated = dict(prepared)
            mutated["executable_path"] = "/bin/echo"
            with self.assertRaises(portable_stage.PortableStageError) as caught:
                portable_stage.run_stage(mutated, timeout_seconds=2)
            self.assertEqual(caught.exception.code, "invalid_prepared_stage")
            self.assertFalse((root / "state/imports").exists())
            self.assertFalse((root / "published").exists())

    def test_stdout_schema_and_canonical_codec_reject_before_import(self):
        for mode in ("boolean-schema-version", "non-nfc-envelope", "duplicate-key"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                prepared = self._prepare(root, stage="awr-research")
                with mock.patch.dict(
                    os.environ,
                    {"FAKE_PORTABLE_STAGE_MODE": mode},
                    clear=False,
                ):
                    with self.assertRaises(portable_stage.PortableStageError):
                        portable_stage.run_stage(prepared, timeout_seconds=2)
                imports = root / "state/imports"
                self.assertFalse(imports.exists() and any(imports.iterdir()))
                self.assertFalse((root / "state/completion.json").exists())
                self.assertFalse((root / "published").exists())

    def test_agy_mirror_write_rejects_before_import_projection_or_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(
                root,
                stage="awr-research",
                provider="agy",
            )
            with mock.patch.dict(
                os.environ,
                {"FAKE_PORTABLE_STAGE_MODE": "agy-mirror-write"},
                clear=False,
            ):
                with self.assertRaises(
                    portable_stage.PortableStageError
                ) as caught:
                    portable_stage.run_stage(prepared, timeout_seconds=2)
            self.assertEqual(caught.exception.code, "unexpected_artifact")
            imports = root / "state/imports"
            self.assertFalse(imports.exists() and any(imports.iterdir()))
            self.assertFalse((root / "state/completion.json").exists())
            self.assertFalse((root / "published").exists())

    def test_grok_forces_claude_compatibility_sources_off(self):
        names = (
            "GROK_CLAUDE_SKILLS_ENABLED",
            "GROK_CLAUDE_RULES_ENABLED",
            "GROK_CLAUDE_MCPS_ENABLED",
            "GROK_CLAUDE_HOOKS_ENABLED",
            "GROK_CLAUDE_SESSIONS_ENABLED",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(
                root,
                stage="awr-research",
                provider="grok",
            )
            hostile = {name: "true" for name in names}
            hostile["FAKE_PORTABLE_STAGE_MODE"] = (
                "grok-compatibility-audit"
            )
            with mock.patch.dict(os.environ, hostile, clear=False):
                portable_stage.run_stage(prepared, timeout_seconds=2)
            preflight = json.loads(
                pathlib.Path(prepared["preflight_path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                preflight["provider_command"]["environment"],
                {
                    **{name: "false" for name in names},
                    "FAKE_PROVIDER_MAX_OUTPUT_TOKENS": "3072",
                },
            )

    def test_stdout_hidden_file_in_unreadable_directory_rejects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(
                root,
                stage="awr-research",
                provider="agy",
            )
            with mock.patch.dict(
                os.environ,
                {"FAKE_PORTABLE_STAGE_MODE": "agy-hidden-extra-mode-zero"},
                clear=False,
            ):
                with self.assertRaises(
                    portable_stage.PortableStageError
                ) as caught:
                    portable_stage.run_stage(prepared, timeout_seconds=2)
            self.assertEqual(caught.exception.code, "unexpected_artifact")
            imports = root / "state/imports"
            self.assertFalse(imports.exists() and any(imports.iterdir()))
            self.assertFalse((root / "state/completion.json").exists())
            self.assertFalse((root / "published").exists())
            self.assertFalse(any((root / "state").glob("attempt-*")))

    def test_stdout_expected_input_requires_regular_single_link(self):
        modes = (
            "agy-input-symlink",
            "agy-input-hardlink",
            "agy-input-special",
        )
        for mode in modes:
            with (
                self.subTest(mode=mode),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = pathlib.Path(directory)
                prepared = self._prepare(
                    root,
                    stage="awr-research",
                    provider="agy",
                )
                target = root / "inputs/idea.md"
                with mock.patch.dict(
                    os.environ,
                    {
                        "FAKE_PORTABLE_STAGE_MODE": mode,
                        "FAKE_PORTABLE_LINK_TARGET": str(target),
                    },
                    clear=False,
                ):
                    with self.assertRaises(
                        portable_stage.PortableStageError
                    ) as caught:
                        portable_stage.run_stage(prepared, timeout_seconds=2)
                self.assertEqual(caught.exception.code, "unexpected_artifact")
                imports = root / "state/imports"
                self.assertFalse(imports.exists() and any(imports.iterdir()))
                self.assertFalse((root / "state/completion.json").exists())
                self.assertFalse((root / "published").exists())
                self.assertFalse(any((root / "state").glob("attempt-*")))

    def test_stdout_directory_swap_out_during_walk_rejects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(
                root,
                stage="awr-research",
                provider="agy",
            )
            real_open_directory = portable_agent._open_directory_at
            raced = False

            def swap_after_open(parent_descriptor, component, code):
                nonlocal raced
                child = real_open_directory(
                    parent_descriptor,
                    component,
                    code,
                )
                if component == "input" and not raced:
                    raced = True
                    source = root / "inputs/idea.md"
                    mirror_input = next(
                        (root / "state").glob("attempt-*/mirror/input")
                    )
                    mirror_input.rename(root / "swapped-input")
                    mirror_input.mkdir(mode=0o700)
                    os.chmod(mirror_input, 0o700)
                    replacement = mirror_input / "idea.md"
                    replacement.write_bytes(source.read_bytes())
                    os.chmod(replacement, 0o600)
                    hidden = mirror_input / "hidden.txt"
                    hidden.write_text("hidden\n", encoding="utf-8")
                    os.chmod(hidden, 0o600)
                return child

            with mock.patch.object(
                portable_agent,
                "_open_directory_at",
                swap_after_open,
            ):
                with self.assertRaises(
                    portable_stage.PortableStageError
                ) as caught:
                    portable_stage.run_stage(prepared, timeout_seconds=2)
            self.assertTrue(raced)
            self.assertEqual(caught.exception.code, "unexpected_artifact")
            imports = root / "state/imports"
            self.assertFalse(imports.exists() and any(imports.iterdir()))
            self.assertFalse((root / "state/completion.json").exists())
            self.assertFalse((root / "published").exists())
            self.assertFalse(any((root / "state").glob("attempt-*")))

    def test_stdout_mirror_root_swap_during_walk_rejects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(
                root,
                stage="awr-research",
                provider="agy",
            )
            real_walk = portable_agent._walk_mirror_directory
            raced = False

            def swap_after_root_walk(
                directory_descriptor,
                prefix,
                visit_non_directory,
                skipped_root,
                seen_skipped_root,
            ):
                nonlocal raced
                result = real_walk(
                    directory_descriptor,
                    prefix,
                    visit_non_directory,
                    skipped_root,
                    seen_skipped_root,
                )
                if not prefix and not raced:
                    raced = True
                    mirror = next(
                        (root / "state").glob("attempt-*/mirror")
                    )
                    displaced = mirror.with_name("mirror-old")
                    mirror.rename(displaced)
                    mirror.symlink_to(
                        displaced.name,
                        target_is_directory=True,
                    )
                return result

            with mock.patch.object(
                portable_agent,
                "_walk_mirror_directory",
                swap_after_root_walk,
            ):
                with self.assertRaises(
                    portable_stage.PortableStageError
                ) as caught:
                    portable_stage.run_stage(prepared, timeout_seconds=2)
            self.assertTrue(raced)
            self.assertEqual(caught.exception.code, "unexpected_artifact")
            imports = root / "state/imports"
            self.assertFalse(imports.exists() and any(imports.iterdir()))
            self.assertFalse((root / "state/completion.json").exists())
            self.assertFalse((root / "published").exists())
            self.assertFalse(any((root / "state").glob("attempt-*")))

    def test_agy_bounded_tmp_schema_is_ignored_and_never_imported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(
                root,
                stage="awr-research",
                provider="agy",
            )
            with mock.patch.dict(
                os.environ,
                {"FAKE_PORTABLE_STAGE_MODE": "agy-tmp-schema"},
                clear=False,
            ):
                completion = portable_stage.run_stage(
                    prepared, timeout_seconds=2
                )
            imported = pathlib.Path(prepared["state_root"]) / "imports" / (
                completion["model_envelope_sha256"] + ".json"
            )
            self.assertEqual(
                {
                    path.relative_to(root).as_posix()
                    for path in root.rglob("*")
                    if path.is_file()
                },
                {
                    "agy",
                    "inputs/idea.md",
                    "published/draft.md",
                    "state/completion.json",
                    "state/imports/" + imported.name,
                    "state/preflight.json",
                },
            )
            self.assertEqual(
                imported.read_bytes(),
                portable_agent._canonical_json_bytes(
                    json.loads(imported.read_text(encoding="utf-8"))
                ),
            )
            self.assertFalse(any((root / "state").glob("attempt-*")))

    def test_agy_tmp_exact_file_entry_and_byte_limits_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(
                root,
                stage="awr-research",
                provider="agy",
            )
            with mock.patch.dict(
                os.environ,
                {"FAKE_PORTABLE_STAGE_MODE": "agy-tmp-exact-limits"},
                clear=False,
            ):
                completion = portable_stage.run_stage(
                    prepared, timeout_seconds=2
                )
            imported = pathlib.Path(prepared["state_root"]) / "imports" / (
                completion["model_envelope_sha256"] + ".json"
            )
            self.assertTrue(imported.is_file())
            self.assertTrue((root / "published/draft.md").is_file())
            self.assertTrue((root / "state/completion.json").is_file())
            self.assertFalse(any((root / "state").glob("attempt-*")))

    def test_agy_tmp_aggregate_bytes_over_limit_rejects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(
                root,
                stage="awr-research",
                provider="agy",
            )
            with mock.patch.dict(
                os.environ,
                {
                    "FAKE_PORTABLE_STAGE_MODE": (
                        "agy-tmp-aggregate-oversize"
                    )
                },
                clear=False,
            ):
                with self.assertRaises(
                    portable_stage.PortableStageError
                ) as caught:
                    portable_stage.run_stage(prepared, timeout_seconds=2)
            self.assertEqual(caught.exception.code, "unexpected_artifact")
            imports = root / "state/imports"
            self.assertFalse(imports.exists() and any(imports.iterdir()))
            self.assertFalse((root / "state/completion.json").exists())
            self.assertFalse((root / "published").exists())
            self.assertFalse(any((root / "state").glob("attempt-*")))

    def test_agy_unsafe_or_unbounded_tmp_rejects_before_import_or_projection(self):
        modes = (
            "agy-tmp-missing",
            "agy-tmp-root-symlink",
            "agy-tmp-file-symlink",
            "agy-tmp-hardlink",
            "agy-tmp-special",
            "agy-tmp-oversize",
            "agy-tmp-too-many",
            "agy-tmp-too-many-directories",
            "agy-tmp-mode-zero",
        )
        for mode in modes:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                prepared = self._prepare(
                    root,
                    stage="awr-research",
                    provider="agy",
                )
                with mock.patch.dict(
                    os.environ,
                    {"FAKE_PORTABLE_STAGE_MODE": mode},
                    clear=False,
                ):
                    with self.assertRaises(
                        portable_stage.PortableStageError
                    ) as caught:
                        portable_stage.run_stage(prepared, timeout_seconds=2)
                self.assertEqual(caught.exception.code, "unexpected_artifact")
                imports = root / "state/imports"
                self.assertFalse(imports.exists() and any(imports.iterdir()))
                self.assertFalse((root / "state/completion.json").exists())
                self.assertFalse((root / "published").exists())
                self.assertFalse(any((root / "state").glob("attempt-*")))

    def test_successful_provider_exit_quiesces_late_tmp_growth(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            trigger = root / "grow-trigger"
            done = root / "grow-done"
            prepared = self._prepare(
                root,
                stage="awr-research",
                provider="agy",
            )
            real_read = portable_agent._read_tmp_file_stable

            def trigger_after_read(directory_descriptor, name, maximum):
                raw = real_read(directory_descriptor, name, maximum)
                if name == "cache":
                    trigger.write_text("go\n", encoding="utf-8")
                    deadline = time.monotonic() + 0.5
                    while not done.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                return raw

            with mock.patch.dict(
                os.environ,
                {
                    "FAKE_PORTABLE_STAGE_MODE": "agy-tmp-late-grow",
                    "FAKE_PORTABLE_TRIGGER_PATH": str(trigger),
                    "FAKE_PORTABLE_DONE_PATH": str(done),
                },
                clear=False,
            ), mock.patch.object(
                portable_agent,
                "_read_tmp_file_stable",
                trigger_after_read,
            ):
                completion = portable_stage.run_stage(
                    prepared, timeout_seconds=2
                )
            self.assertTrue(
                (
                    pathlib.Path(prepared["state_root"])
                    / "imports"
                    / (completion["model_envelope_sha256"] + ".json")
                ).is_file()
            )
            self.assertFalse(done.exists())
            self.assertFalse(any((root / "state").glob("attempt-*")))

    def test_successful_provider_exit_quiesces_late_root_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            trigger = root / "add-trigger"
            done = root / "add-done"
            prepared = self._prepare(
                root,
                stage="awr-research",
                provider="agy",
            )
            real_walk = portable_agent._walk_mirror_directory
            triggered = False

            def trigger_before_root(
                directory_descriptor,
                prefix,
                visit_non_directory,
                skipped_root,
                seen_skipped_root,
            ):
                nonlocal triggered
                if not prefix and not triggered:
                    triggered = True
                    trigger.write_text("go\n", encoding="utf-8")
                    deadline = time.monotonic() + 0.5
                    while not done.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                return real_walk(
                    directory_descriptor,
                    prefix,
                    visit_non_directory,
                    skipped_root,
                    seen_skipped_root,
                )

            with mock.patch.dict(
                os.environ,
                {
                    "FAKE_PORTABLE_STAGE_MODE": "agy-late-root-file",
                    "FAKE_PORTABLE_TRIGGER_PATH": str(trigger),
                    "FAKE_PORTABLE_DONE_PATH": str(done),
                },
                clear=False,
            ), mock.patch.object(
                portable_agent,
                "_walk_mirror_directory",
                trigger_before_root,
            ):
                completion = portable_stage.run_stage(
                    prepared, timeout_seconds=2
                )
            self.assertTrue(triggered)
            self.assertTrue(
                (
                    pathlib.Path(prepared["state_root"])
                    / "imports"
                    / (completion["model_envelope_sha256"] + ".json")
                ).is_file()
            )
            self.assertFalse(done.exists())
            self.assertFalse(any((root / "state").glob("attempt-*")))

    def test_cleanup_failure_rejects_before_import_or_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(
                root,
                stage="awr-research",
                provider="agy",
            )
            real_rmtree = portable_agent.shutil.rmtree

            def deny_attempt_removal(path, *args, **kwargs):
                if pathlib.Path(path).name.startswith("attempt-"):
                    raise PermissionError("injected cleanup failure")
                return real_rmtree(path, *args, **kwargs)

            with mock.patch.object(
                portable_agent.shutil,
                "rmtree",
                deny_attempt_removal,
            ):
                with self.assertRaises(
                    portable_stage.PortableStageError
                ) as caught:
                    portable_stage.run_stage(prepared, timeout_seconds=2)
            self.assertEqual(caught.exception.code, "attempt_cleanup_failed")
            imports = root / "state/imports"
            self.assertFalse(imports.exists() and any(imports.iterdir()))
            self.assertFalse((root / "state/completion.json").exists())
            self.assertFalse((root / "published").exists())
            attempts = list((root / "state").glob("attempt-*"))
            self.assertEqual(len(attempts), 1)
            portable_agent._repair_attempt_directories(attempts[0])
            real_rmtree(attempts[0])

    def test_existing_mirror_file_drift_rejects_before_import_or_projection(self):
        modes = (
            "overwrite-role",
            "overwrite-declared-input",
            "chmod-declared-input",
        )
        for mode in modes:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                prepared = self._prepare(root, stage="awr-research")
                with mock.patch.dict(
                    os.environ,
                    {"FAKE_PORTABLE_STAGE_MODE": mode},
                    clear=False,
                ):
                    with self.assertRaises(
                        portable_stage.PortableStageError
                    ) as caught:
                        portable_stage.run_stage(prepared, timeout_seconds=2)
                self.assertEqual(caught.exception.code, "unexpected_artifact")
                imports = root / "state/imports"
                self.assertFalse(imports.exists() and any(imports.iterdir()))
                self.assertFalse((root / "state/completion.json").exists())
                self.assertFalse((root / "published").exists())

    def test_grok_transport_imports_the_canonical_inner_model_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(root, provider="grok")
            completion = portable_stage.run_stage(prepared, timeout_seconds=2)
            imported = pathlib.Path(prepared["state_root"]) / "imports" / (
                completion["model_envelope_sha256"] + ".json"
            )
            self.assertEqual(
                imported.read_bytes(),
                portable_agent._canonical_json_bytes(
                    json.loads(imported.read_text(encoding="utf-8"))
                ),
            )

    def _assert_agy_transport_rejects(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(
                root, stage="awr-research", provider="agy"
            )
            with mock.patch.dict(
                os.environ,
                {"FAKE_PORTABLE_STAGE_MODE": mode},
                clear=False,
            ):
                with self.assertRaises(
                    portable_stage.PortableStageError
                ) as caught:
                    portable_stage.run_stage(prepared, timeout_seconds=2)
            imports = root / "state/imports"
            self.assertFalse(imports.exists() and any(imports.iterdir()))
            self.assertFalse(
                pathlib.Path(prepared["completion_path"]).exists()
            )
            self.assertFalse(pathlib.Path(prepared["output_root"]).exists())
            self.assertTrue(
                all(
                    not pathlib.Path(path).exists()
                    for path in prepared["output_paths"].values()
                )
            )
            return caught.exception

    def test_agy_structured_transport_imports_only_structured_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(
                root, stage="awr-research", provider="agy"
            )
            completion = portable_stage.run_stage(
                prepared, timeout_seconds=2
            )
            imported = pathlib.Path(prepared["state_root"]) / "imports" / (
                completion["model_envelope_sha256"] + ".json"
            )
            raw = imported.read_bytes()
            self.assertEqual(
                raw,
                portable_agent._canonical_json_bytes(
                    json.loads(raw.decode("utf-8"))
                ),
            )
            self.assertEqual(
                hashlib.sha256(raw).hexdigest(),
                completion["model_envelope_sha256"],
            )
            self.assertNotIn(b"noisy-response", raw)
            self.assertTrue((root / "published/draft.md").is_file())
            self.assertTrue(
                pathlib.Path(prepared["completion_path"]).is_file()
            )

    def test_agy_structured_transport_rejects_invalid_outer(self):
        for mode in (
            "malformed-outer-json",
            "invalid-outer-utf8",
            "outer-array",
            "duplicate-outer-status",
        ):
            with self.subTest(mode=mode):
                caught = self._assert_agy_transport_rejects(mode)
                self.assertEqual(caught.code, "malformed_output")

    def test_agy_structured_transport_normalizes_outer_json_recursion_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(
                root, stage="awr-research", provider="agy"
            )
            parser = mock.Mock(wraps=json)
            parser.JSONDecodeError = json.JSONDecodeError
            parser.loads.side_effect = RecursionError
            caught = None
            with mock.patch.object(portable_agent, "json", parser):
                try:
                    portable_stage.run_stage(prepared, timeout_seconds=2)
                except (
                    portable_stage.PortableStageError,
                    RecursionError,
                ) as exc:
                    caught = exc
            imports = root / "state/imports"
            self.assertFalse(imports.exists() and any(imports.iterdir()))
            self.assertFalse(
                pathlib.Path(prepared["completion_path"]).exists()
            )
            self.assertFalse(pathlib.Path(prepared["output_root"]).exists())
            self.assertTrue(
                all(
                    not pathlib.Path(path).exists()
                    for path in prepared["output_paths"].values()
                )
            )
            self.assertIs(type(caught), portable_stage.PortableStageError)
            self.assertEqual(caught.code, "malformed_output")

    def test_agy_structured_transport_requires_success_status(self):
        for mode in ("agy-missing-status", "agy-failure-status"):
            with self.subTest(mode=mode):
                self._assert_agy_transport_rejects(mode)

    def test_agy_structured_transport_requires_object_payload(self):
        for mode in (
            "agy-missing-payload",
            "agy-null-payload",
            "agy-array-payload",
        ):
            with self.subTest(mode=mode):
                caught = self._assert_agy_transport_rejects(mode)
                self.assertEqual(caught.code, "malformed_output")

    def test_agy_structured_transport_requires_type_exact_schema_echo(self):
        for mode in (
            "agy-missing-schema",
            "agy-mutated-schema",
            "agy-schema-int-float",
            "agy-schema-int-bool",
        ):
            with self.subTest(mode=mode):
                caught = self._assert_agy_transport_rejects(mode)
                self.assertEqual(caught.code, "malformed_output")

    def test_agy_structured_transport_rejects_non_strict_inner_values(self):
        for mode in (
            "duplicate-key",
            "float-inner-value",
            "non-nfc-envelope",
            "surrogate-inner-value",
        ):
            with self.subTest(mode=mode):
                caught = self._assert_agy_transport_rejects(mode)
                self.assertEqual(caught.code, "malformed_output")

    def test_agy_structured_transport_preserves_closed_schema_validation(self):
        expected_codes = {
            "extra-envelope": "schema_mismatch",
            "boolean-schema-version": "schema_mismatch",
            "missing-request-attestation": "schema_mismatch",
            "wrong-request-attestation": (
                "provider_request_attestation_mismatch"
            ),
        }
        for mode, expected_code in expected_codes.items():
            with self.subTest(mode=mode):
                caught = self._assert_agy_transport_rejects(mode)
                self.assertEqual(caught.code, expected_code)

    def _assert_claude_transport_rejects(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(
                root, stage="awr-research", provider="claude"
            )
            with mock.patch.dict(
                os.environ,
                {"FAKE_PORTABLE_STAGE_MODE": mode},
                clear=False,
            ):
                with self.assertRaises(
                    portable_stage.PortableStageError
                ) as caught:
                    portable_stage.run_stage(prepared, timeout_seconds=2)
            imports = root / "state/imports"
            self.assertFalse(imports.exists() and any(imports.iterdir()))
            self.assertFalse(
                pathlib.Path(prepared["completion_path"]).exists()
            )
            self.assertFalse(pathlib.Path(prepared["output_root"]).exists())
            self.assertTrue(
                all(
                    not pathlib.Path(path).exists()
                    for path in prepared["output_paths"].values()
                )
            )
            return caught.exception

    def test_claude_structured_transport_imports_only_structured_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(
                root, stage="awr-research", provider="claude"
            )
            completion = portable_stage.run_stage(
                prepared, timeout_seconds=2
            )
            imported = pathlib.Path(prepared["state_root"]) / "imports" / (
                completion["model_envelope_sha256"] + ".json"
            )
            raw = imported.read_bytes()
            self.assertEqual(
                raw,
                portable_agent._canonical_json_bytes(
                    json.loads(raw.decode("utf-8"))
                ),
            )
            self.assertEqual(
                hashlib.sha256(raw).hexdigest(),
                completion["model_envelope_sha256"],
            )
            self.assertNotIn(b"ignored", raw)
            self.assertTrue((root / "published/draft.md").is_file())
            self.assertTrue(
                pathlib.Path(prepared["completion_path"]).is_file()
            )

    def test_claude_structured_transport_rejects_invalid_outer(self):
        for mode in (
            "malformed-outer-json",
            "invalid-outer-utf8",
            "outer-array",
            "duplicate-outer-subtype",
            "claude-missing-subtype",
            "claude-error-subtype",
            "claude-is-error",
            "claude-missing-is-error",
            "claude-missing-payload",
            "claude-null-payload",
            "claude-array-payload",
            "claude-text-only",
        ):
            with self.subTest(mode=mode):
                caught = self._assert_claude_transport_rejects(mode)
                self.assertEqual(caught.code, "malformed_output")

    def test_claude_structured_transport_preserves_closed_schema_validation(self):
        expected_codes = {
            "extra-envelope": "schema_mismatch",
            "boolean-schema-version": "schema_mismatch",
            "missing-request-attestation": "schema_mismatch",
            "wrong-request-attestation": (
                "provider_request_attestation_mismatch"
            ),
        }
        for mode, expected_code in expected_codes.items():
            with self.subTest(mode=mode):
                caught = self._assert_claude_transport_rejects(mode)
                self.assertEqual(caught.code, expected_code)

    def test_grok_transport_accepts_bare_inner_model_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(root, provider="grok")
            with mock.patch.dict(
                os.environ,
                {"FAKE_PORTABLE_STAGE_MODE": "bare-inner-success"},
                clear=False,
            ):
                completion = portable_stage.run_stage(
                    prepared, timeout_seconds=2
                )
            imported = pathlib.Path(prepared["state_root"]) / "imports" / (
                completion["model_envelope_sha256"] + ".json"
            )
            self.assertEqual(
                imported.read_bytes(),
                portable_agent._canonical_json_bytes(
                    json.loads(imported.read_text(encoding="utf-8"))
                ),
            )

    def test_grok_transport_accepts_one_terminal_fence_after_narration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(root, provider="grok")
            with mock.patch.dict(
                os.environ,
                {"FAKE_PORTABLE_STAGE_MODE": "narrated-terminal-fence"},
                clear=False,
            ):
                completion = portable_stage.run_stage(
                    prepared, timeout_seconds=2
                )
            imported = pathlib.Path(prepared["state_root"]) / "imports" / (
                completion["model_envelope_sha256"] + ".json"
            )
            self.assertEqual(
                imported.read_bytes(),
                portable_agent._canonical_json_bytes(
                    json.loads(imported.read_text(encoding="utf-8"))
                ),
            )
            self.assertTrue(
                pathlib.Path(prepared["completion_path"]).is_file()
            )
            self.assertTrue(
                all(
                    pathlib.Path(path).is_file()
                    for path in prepared["output_paths"].values()
                )
            )

    def test_grok_transport_accepts_reducer_joined_terminal_fence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(root, provider="grok")
            with mock.patch.dict(
                os.environ,
                {"FAKE_PORTABLE_STAGE_MODE": "fence-non-line-start"},
                clear=False,
            ):
                completion = portable_stage.run_stage(
                    prepared, timeout_seconds=2
                )
            imported = pathlib.Path(prepared["state_root"]) / "imports" / (
                completion["model_envelope_sha256"] + ".json"
            )
            self.assertEqual(
                imported.read_bytes(),
                portable_agent._canonical_json_bytes(
                    json.loads(imported.read_text(encoding="utf-8"))
                ),
            )
            self.assertTrue(
                pathlib.Path(prepared["completion_path"]).is_file()
            )
            self.assertTrue(pathlib.Path(prepared["output_root"]).is_dir())

    def test_grok_transport_rejects_narrated_terminal_bare_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(root, provider="grok")
            with mock.patch.dict(
                os.environ,
                {"FAKE_PORTABLE_STAGE_MODE": "narrated-terminal-bare"},
                clear=False,
            ):
                with self.assertRaises(
                    portable_stage.PortableStageError
                ) as caught:
                    portable_stage.run_stage(prepared, timeout_seconds=2)
            self.assertEqual(caught.exception.code, "malformed_output")
            imports = root / "state/imports"
            self.assertFalse(imports.exists() and any(imports.iterdir()))
            self.assertFalse((root / "state/completion.json").exists())
            self.assertFalse((root / "published").exists())
            self.assertTrue(
                all(
                    not pathlib.Path(path).exists()
                    for path in prepared["output_paths"].values()
                )
            )

    def test_grok_transport_rejects_overlapping_prefix_fence_runs(self):
        for mode in (
            "fence-four-backtick-prefix",
            "fence-five-backtick-prefix",
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                prepared = self._prepare(root, provider="grok")
                with mock.patch.dict(
                    os.environ,
                    {"FAKE_PORTABLE_STAGE_MODE": mode},
                    clear=False,
                ):
                    with self.assertRaises(
                        portable_stage.PortableStageError
                    ) as caught:
                        portable_stage.run_stage(prepared, timeout_seconds=2)
                self.assertEqual(caught.exception.code, "malformed_output")
                imports = root / "state/imports"
                self.assertFalse(
                    imports.exists() and any(imports.iterdir())
                )
                self.assertFalse(
                    pathlib.Path(prepared["completion_path"]).exists()
                )
                self.assertFalse(
                    pathlib.Path(prepared["output_root"]).exists()
                )
                self.assertTrue(
                    all(
                        not pathlib.Path(path).exists()
                        for path in prepared["output_paths"].values()
                    )
                )

    def test_grok_transport_rejects_invalid_outer_and_inner_responses(self):
        modes = (
            "malformed-outer-json",
            "duplicate-outer-text",
            "invalid-outer-utf8",
            "outer-array",
            "nonstring-text",
            "surrogate-text",
            "missing-text",
            "max-tokens",
            "malformed",
            "duplicate-key",
            "non-nfc-envelope",
            "float-inner-value",
            "wrong-request-attestation",
            "fence-duplicate-delimiter",
            "fence-crlf",
            "fence-cr-before-close",
            "fence-prefix-inline-delimiter",
            "fence-prefix-indented-delimiter",
            "fence-prefix-inline-wrong-language",
            "fence-wrong-language",
            "fence-wrong-case",
            "fence-missing-close",
            "fence-trailing-newline",
            "fence-trailing-space",
            "fence-trailing-text",
            "fence-trailing-nul",
        )
        for mode in modes:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                prepared = self._prepare(root, provider="grok")
                with mock.patch.dict(
                    os.environ,
                    {"FAKE_PORTABLE_STAGE_MODE": mode},
                    clear=False,
                ):
                    with self.assertRaises(portable_stage.PortableStageError):
                        portable_stage.run_stage(prepared, timeout_seconds=2)
                imports = root / "state/imports"
                self.assertFalse(imports.exists() and any(imports.iterdir()))
                self.assertFalse((root / "state/completion.json").exists())
                self.assertTrue(
                    all(
                        not pathlib.Path(path).exists()
                        for path in prepared["output_paths"].values()
                    )
                )

    def test_codex_noncanonical_final_json_is_canonicalized_before_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(root, provider="codex")
            with mock.patch.dict(
                os.environ,
                {"FAKE_PORTABLE_STAGE_MODE": "noncanonical-raw"},
                clear=False,
            ):
                completion = portable_stage.run_stage(
                    prepared, timeout_seconds=2
                )
            imported = (
                root / "state/imports"
                / f"{completion['model_envelope_sha256']}.json"
            )
            raw = imported.read_bytes()
            self.assertEqual(
                raw,
                self._canonical(json.loads(raw.decode("utf-8"))),
            )
            self.assertTrue((root / "state/completion.json").is_file())
            self.assertTrue(
                all(
                    pathlib.Path(path).is_file()
                    for path in prepared["output_paths"].values()
                )
            )

    def test_new_import_directory_entry_is_fsynced_before_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(root, stage="awr-research")
            fsynced_directories = []
            real_fsync = portable_agent.os.fsync

            def recording_fsync(descriptor):
                info = os.fstat(descriptor)
                if stat.S_ISDIR(info.st_mode):
                    fsynced_directories.append((info.st_dev, info.st_ino))
                return real_fsync(descriptor)

            with mock.patch.object(portable_agent.os, "fsync", recording_fsync):
                portable_stage.run_stage(prepared, timeout_seconds=2)
            imports_info = (root / "state/imports").stat()
            self.assertIn(
                (imports_info.st_dev, imports_info.st_ino),
                fsynced_directories,
            )

    def test_public_descriptor_is_closed_path_free_and_independently_verifiable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(root, stage="awr-research")
            portable_stage.run_stage(prepared, timeout_seconds=2)
            descriptor = portable_stage.public_descriptor(prepared, root)
            encoded = json.dumps(
                descriptor,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            self.assertNotIn(str(root), encoded)
            self.assertNotIn(str(FAKE_STAGE), encoded)
            self.assertNotIn('"serialized_prompt":', encoded)
            self.assertNotIn("portable-stage-request-v1", encoded)
            self.assertEqual(
                set(descriptor),
                {
                    "schema_version",
                    "execution_boundary",
                    "stage",
                    "seat_id",
                    "provider",
                    "provider_validation",
                    "authority",
                    "execution_request_profile_hash",
                    "max_output_tokens",
                    "output_token_cap_binding",
                    "output_token_cap_semantics",
                    "serialized_prompt_sha256",
                    "role_sha256",
                    "input_sha256s",
                    "provider_request_sha256",
                    "provider_request_binding_sha256",
                    "response_schema_sha256",
                    "preflight",
                    "completion",
                    "outputs",
                },
            )
            view = portable_stage.verify_public_descriptor(descriptor, root)
            self.assertEqual(view["stage"], "awr-research")
            self.assertEqual(
                pathlib.Path(view["output_paths"]["draft.md"]).read_text(
                    encoding="utf-8"
                ),
                pathlib.Path(prepared["output_paths"]["draft.md"]).read_text(
                    encoding="utf-8"
                ),
            )

            escaped = copy.deepcopy(descriptor)
            escaped["outputs"]["draft.md"]["path"] = "../outside.md"
            with self.assertRaises(portable_stage.PortableStageError):
                portable_stage.verify_public_descriptor(escaped, root)

    def test_public_verifier_rejects_self_rehashed_split_receipt_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(root, stage="awr-research")
            portable_stage.run_stage(prepared, timeout_seconds=2)
            descriptor = portable_stage.public_descriptor(prepared, root)
            attacked = self._rewrite_public_receipts(
                root,
                descriptor,
                lambda preflight: preflight.__setitem__(
                    "seat_id", "different-seat"
                ),
            )
            with self.assertRaises(portable_stage.PortableStageError):
                portable_stage.verify_public_descriptor(attacked, root)

    def test_public_verifier_reconstructs_closed_provider_command_grammar(self):
        attacks = (
            (
                "private-extra-field",
                "codex",
                lambda command: command.__setitem__("_private", True),
            ),
            (
                "unsupported-kimi-reasoning",
                "kimi",
                lambda command: (
                    command.__setitem__("requested_reasoning", "high"),
                    command.__setitem__("reasoning_default", False),
                ),
            ),
        )
        for name, provider, mutate_command in attacks:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                prepared = self._prepare(
                    root, stage="awr-research", provider=provider
                )
                portable_stage.run_stage(prepared, timeout_seconds=2)
                descriptor = portable_stage.public_descriptor(prepared, root)
                attacked = self._rewrite_public_receipts(
                    root,
                    descriptor,
                    lambda preflight: mutate_command(
                        preflight["provider_command"]
                    ),
                )
                with self.assertRaises(portable_stage.PortableStageError):
                    portable_stage.verify_public_descriptor(attacked, root)

    def test_public_verifier_rejects_boolean_fields_encoded_as_integers(self):
        attacks = (
            ("hard_complete_eligible", "codex", 0),
            ("model_default", "codex", 0),
            ("reasoning_default", "kimi", 1),
        )
        for field, provider, replacement in attacks:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                prepared = self._prepare(
                    root, stage="awr-research", provider=provider
                )
                portable_stage.run_stage(prepared, timeout_seconds=2)
                descriptor = portable_stage.public_descriptor(prepared, root)
                attacked = self._rewrite_public_receipts(
                    root,
                    descriptor,
                    lambda preflight, field=field, replacement=replacement: (
                        preflight["provider_command"].__setitem__(
                            field, replacement
                        )
                    ),
                )
                with self.assertRaises(portable_stage.PortableStageError):
                    portable_stage.verify_public_descriptor(attacked, root)



    def test_all_providers_inline_stage_contract_without_file_tools(self):
        """Every Hunt/AwR portable provider must receive role+inputs in -p."""
        providers = (
            ("hunt", "codex"),
            ("hunt", "kimi"),
            ("hunt", "grok"),
            ("hunt", "claude"),
            ("awr", "opencode"),
            ("awr", "agy"),
            ("awr", "claude"),
        )
        for surface, provider in providers:
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                stage = "generate" if surface == "hunt" else "awr-research"
                prepared = self._prepare(root, stage=stage, provider=provider)
                request = json.loads(prepared["provider_request"])
                self.assertIn("role_text", request)
                self.assertTrue(request["role_text"].strip())
                self.assertEqual(
                    hashlib.sha256(request["role_text"].encode("utf-8")).hexdigest(),
                    request["role_sha256"],
                )
                self.assertIn("declared_input_texts", request)
                self.assertEqual(
                    set(request["declared_input_texts"]),
                    set(request["declared_input_sha256s"]),
                )
                for name, body in request["declared_input_texts"].items():
                    self.assertEqual(
                        hashlib.sha256(body.encode("utf-8")).hexdigest(),
                        request["declared_input_sha256s"][name],
                    )
                self.assertIn("host_output_contract", request)
                # Providers must not depend on reading role.md from disk.
                self.assertIn("do not require file tools", request["transport_instructions"]["role"])
                argv = prepared["provider_command"]["argv"]
                if provider == "claude":
                    self.assertNotIn("--tools", argv)

    def test_contract_error_class_covers_transport_and_assembly_codes(self):
        contract_codes = (
            "invalid_generation_output",
            "malformed_output",
            "noncanonical_output",
            "final_output_missing",
            "final_output_unreadable",
            "final_output_oversize",
            "schema_mismatch",
            "provider_request_attestation_mismatch",
            "invalid_contract_text",
            "contract_text_hash_mismatch",
            "request_too_large",
        )
        for code in contract_codes:
            with self.subTest(code=code):
                err = portable_stage.PortableStageError(code, "detail")
                self.assertEqual(err.error_class, "contract")
                self.assertIn(code, str(err))
        execution = portable_stage.PortableStageError("timeout")
        self.assertEqual(execution.error_class, "execution")

    def test_provider_request_rejects_partial_declared_input_texts(self):
        role = (ROOT / "roles/generate.md").read_text(encoding="utf-8")
        role_sha = hashlib.sha256(role.encode("utf-8")).hexdigest()
        body = "brief\n"
        body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        with self.assertRaises(portable_stage.PortableStageError) as caught:
            portable_stage._provider_request(
                "claude",
                "generate",
                "seat",
                '{"schema_version":1,"stage":"generate"}\n',
                {"generation_brief.json": body_sha, "generation_policy.md": body_sha},
                role_sha,
                portable_stage._response_schema("generate"),
                3072,
                "reasoning-and-visible-output",
                role_text=role,
                declared_input_texts={"generation_brief.json": body},
            )
        self.assertEqual(caught.exception.code, "invalid_contract_text")
        self.assertEqual(caught.exception.error_class, "contract")



class LegacyPortableFileOutputHardeningSmoke(unittest.TestCase):
    def _capability(self):
        return provider_adapters._resolve_provider_for_test(
            provider_adapters.load_registry(REGISTRY),
            "hunt",
            "codex",
            model="MODEL",
            reasoning="high",
            max_output_tokens=3072,
            executable_lookup=lambda _: str(FAKE_FILE),
            version_probe=_probe,
        )

    @staticmethod
    def _contract():
        raw = b'{"request_id":"request-1","status":"ok"}\n'
        return {
            "path": "output/result.json",
            "max_bytes": 256,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "allowed_fields": ["request_id", "status"],
            "required_fields": ["request_id", "status"],
            "field_types": {"request_id": "string", "status": "string"},
            "forbid_extra_files": True,
        }

    def _run(self, root, mode, extra=None):
        request = {"mode": mode, "request_id": "request-1"}
        request.update(extra or {})
        return portable_agent.run_portable_attempt(
            self._capability(),
            inputs=[],
            output_contract=self._contract(),
            prompt=json.dumps(request),
            state_root=root,
            timeout_seconds=2,
            max_output_tokens=3072,
        )

    def test_legacy_file_output_uses_scrubbed_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            environment = {
                "GIT_DIR": str(ROOT / ".git"),
                "HISTORY_DB": str(ROOT / ".ai-ideas/history.sqlite3"),
                "HUNT_RUNTIME_ABI": "v2",
                "AWR_RUNTIME_ABI": "v2",
                "CONTAINED_AGENT_CMD_JSON": '["unsafe"]',
                "AGENT_CMD": "unsafe",
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                result = self._run(
                    root / "state",
                    "success",
                    {"audit_environment": True},
                )
            self.assertEqual(result["value"]["status"], "ok")

    def test_legacy_file_output_rejects_stdout_flood(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with self.assertRaises(portable_agent.PortableAgentError) as caught:
                self._run(root / "state", "stdout-flood")
            self.assertEqual(caught.exception.code, "oversize")
            self.assertFalse((root / "state/imports").exists())

    def test_legacy_success_quiesces_remaining_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            delayed = root / "background-child-survived"
            result = self._run(
                root / "state",
                "success-background-child",
                {"delayed_path": str(delayed)},
            )
            self.assertEqual(result["value"]["status"], "ok")
            time.sleep(1.0)
            self.assertFalse(delayed.exists())
            self.assertFalse(any((root / "state").glob("attempt-*")))

    def test_legacy_unreadable_hidden_file_rejects_and_cleans_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with self.assertRaises(portable_agent.PortableAgentError) as caught:
                self._run(root / "state", "mode-zero")
            self.assertEqual(caught.exception.code, "unexpected_artifact")
            self.assertFalse((root / "state/imports").exists())
            self.assertFalse(any((root / "state").glob("attempt-*")))

    def test_legacy_directory_replacement_during_walk_rejects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            state = root / "state"
            real_open_directory = portable_agent._open_directory_at
            raced = False

            def swap_after_open(parent_descriptor, component, code):
                nonlocal raced
                child = real_open_directory(
                    parent_descriptor,
                    component,
                    code,
                )
                if component == "output" and not raced:
                    raced = True
                    output = next(state.glob("attempt-*/mirror/output"))
                    raw = (output / "result.json").read_bytes()
                    output.rename(root / "swapped-output")
                    output.mkdir(mode=0o700)
                    os.chmod(output, 0o700)
                    replacement = output / "result.json"
                    replacement.write_bytes(raw)
                    os.chmod(replacement, 0o600)
                    hidden = output / "hidden.txt"
                    hidden.write_text("hidden\n", encoding="utf-8")
                    os.chmod(hidden, 0o600)
                return child

            with mock.patch.object(
                portable_agent,
                "_open_directory_at",
                swap_after_open,
            ):
                with self.assertRaises(
                    portable_agent.PortableAgentError
                ) as caught:
                    self._run(state, "success")
            self.assertTrue(raced)
            self.assertEqual(caught.exception.code, "unexpected_artifact")
            self.assertFalse((state / "imports").exists())
            self.assertFalse(any(state.glob("attempt-*")))

    def test_legacy_mirror_root_swap_during_walk_rejects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            state = root / "state"
            real_walk = portable_agent._walk_mirror_directory
            raced = False

            def swap_after_root_walk(
                directory_descriptor,
                prefix,
                visit_non_directory,
                skipped_root,
                seen_skipped_root,
            ):
                nonlocal raced
                result = real_walk(
                    directory_descriptor,
                    prefix,
                    visit_non_directory,
                    skipped_root,
                    seen_skipped_root,
                )
                if not prefix and not raced:
                    raced = True
                    mirror = next(state.glob("attempt-*/mirror"))
                    displaced = mirror.with_name("mirror-old")
                    mirror.rename(displaced)
                    mirror.symlink_to(
                        displaced.name,
                        target_is_directory=True,
                    )
                return result

            with mock.patch.object(
                portable_agent,
                "_walk_mirror_directory",
                swap_after_root_walk,
            ):
                with self.assertRaises(
                    portable_agent.PortableAgentError
                ) as caught:
                    self._run(state, "success")
            self.assertTrue(raced)
            self.assertEqual(caught.exception.code, "unexpected_artifact")
            self.assertFalse((state / "imports").exists())
            self.assertFalse(any(state.glob("attempt-*")))

    def test_legacy_expected_input_symlink_rejects_before_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source.txt"
            source.write_text("declared\n", encoding="utf-8")
            raw = source.read_bytes()
            state = root / "state"
            with self.assertRaises(portable_agent.PortableAgentError) as caught:
                portable_agent.run_portable_attempt(
                    self._capability(),
                    inputs=[
                        {
                            "source_root": str(root),
                            "source_path": source.name,
                            "provenance": "declared-input-v1",
                            "path": "input/declared.txt",
                            "sha256": hashlib.sha256(raw).hexdigest(),
                            "max_bytes": len(raw),
                        }
                    ],
                    output_contract=self._contract(),
                    prompt=json.dumps(
                        {
                            "mode": "replace-input-symlink",
                            "request_id": "request-1",
                            "symlink_target": str(source),
                        }
                    ),
                    state_root=state,
                    timeout_seconds=2,
                    max_output_tokens=3072,
                )
            self.assertEqual(caught.exception.code, "unexpected_artifact")
            self.assertFalse((state / "imports").exists())
            self.assertFalse(any(state.glob("attempt-*")))

    def test_legacy_cleanup_failure_rejects_before_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            real_rmtree = portable_agent.shutil.rmtree

            def deny_attempt_removal(path, *args, **kwargs):
                if pathlib.Path(path).name.startswith("attempt-"):
                    raise PermissionError("injected cleanup failure")
                return real_rmtree(path, *args, **kwargs)

            with mock.patch.object(
                portable_agent.shutil,
                "rmtree",
                deny_attempt_removal,
            ):
                with self.assertRaises(
                    portable_agent.PortableAgentError
                ) as caught:
                    self._run(root / "state", "success")
            self.assertEqual(caught.exception.code, "attempt_cleanup_failed")
            self.assertFalse((root / "state/imports").exists())
            attempts = list((root / "state").glob("attempt-*"))
            self.assertEqual(len(attempts), 1)
            portable_agent._repair_attempt_directories(attempts[0])
            real_rmtree(attempts[0])





if __name__ == "__main__":
    unittest.main()
