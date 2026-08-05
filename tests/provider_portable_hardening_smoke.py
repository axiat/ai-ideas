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
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import direction_contract
from lib import history_contract_v2
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
            "grammar_revision": "codex-portable-v1",
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
    def _intent(self, surface="hunt", provider="codex"):
        return provider_adapters._resolve_command_intent_for_test(
            provider_adapters.load_registry(REGISTRY),
            surface,
            provider,
            model="MODEL",
            reasoning=None if provider == "kimi" else "high",
            executable_lookup=lambda _: str(FAKE_STAGE),
        )

    def _capability(self):
        return provider_adapters._resolve_provider_for_test(
            provider_adapters.load_registry(REGISTRY),
            "hunt",
            "codex",
            model="MODEL",
            reasoning="high",
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
        prepared = portable_stage.prepare_stage(
            self._intent(surface, provider),
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
            "fence-non-line-start",
            "fence-crlf",
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

    def test_codex_noncanonical_raw_stdout_still_rejects_before_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            prepared = self._prepare(root, provider="codex")
            with mock.patch.dict(
                os.environ,
                {"FAKE_PORTABLE_STAGE_MODE": "noncanonical-raw"},
                clear=False,
            ):
                with self.assertRaises(portable_stage.PortableStageError) as caught:
                    portable_stage.run_stage(prepared, timeout_seconds=2)
            self.assertEqual(caught.exception.code, "noncanonical_output")
            imports = root / "state/imports"
            self.assertFalse(imports.exists() and any(imports.iterdir()))
            self.assertFalse((root / "state/completion.json").exists())
            self.assertTrue(
                all(
                    not pathlib.Path(path).exists()
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


class LegacyPortableFileOutputHardeningSmoke(unittest.TestCase):
    def _capability(self):
        return provider_adapters._resolve_provider_for_test(
            provider_adapters.load_registry(REGISTRY),
            "hunt",
            "codex",
            model="MODEL",
            reasoning="high",
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


if __name__ == "__main__":
    unittest.main()
