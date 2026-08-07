#!/usr/bin/env python3
import dataclasses
import copy
import hashlib
import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

from lib import portable_agent
from lib import provider_adapters


REGISTRY = ROOT / "history/provider-adapters-v1.json"
FAKE = ROOT / "tests/fake_portable_agent.py"
FORBIDDEN_PROVIDER = "cl" + "aude"
GROK_COMPATIBILITY_DISABLED = {
    "GROK_CLAUDE_SKILLS_ENABLED": "false",
    "GROK_CLAUDE_RULES_ENABLED": "false",
    "GROK_CLAUDE_MCPS_ENABLED": "false",
    "GROK_CLAUDE_HOOKS_ENABLED": "false",
    "GROK_CLAUDE_SESSIONS_ENABLED": "false",
}


def catalog(provider, *models):
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


def probe(
    provider,
    executable_path,
    model,
    reasoning,
    *,
    cli_revision="fake-cli-v1",
    effective_model=None,
    effective_reasoning=None,
    immutable_capacity_identity=None,
):
    return {
        "cli_revision": cli_revision,
        "serializer_revision": "portable-agent-command-v1",
        "effective_model": model if effective_model is None and model else effective_model,
        "effective_reasoning": (
            reasoning if effective_reasoning is None and reasoning else effective_reasoning
        ),
        "model_override_applied": model is None or effective_model in (None, model),
        "reasoning_override_applied": (
            reasoning is None or effective_reasoning in (None, reasoning)
        ),
        "immutable_capacity_identity": immutable_capacity_identity,
    }


class ProviderAdaptersSmoke(unittest.TestCase):
    def _api(self, module, name):
        value = getattr(module, name, None)
        self.assertTrue(callable(value), f"missing behavior: {module.__name__}.{name}")
        return value

    def _registry(self):
        return self._api(provider_adapters, "load_registry")(REGISTRY)

    def _error(self):
        value = getattr(provider_adapters, "ProviderResolutionError", None)
        self.assertTrue(
            isinstance(value, type) and issubclass(value, Exception),
            "missing behavior: provider_adapters.ProviderResolutionError",
        )
        return value

    def _resolve(self, surface, provider, model=None, reasoning=None, probe_fn=probe):
        return self._api(provider_adapters, "_resolve_provider_for_test")(
            self._registry(),
            surface,
            provider,
            model=model,
            reasoning=reasoning,
            executable_lookup=lambda _: str(FAKE),
            version_probe=probe_fn,
        )

    def test_hunt_accepts_exactly_codex_kimi_grok_claude(self):
        accepted = []
        for name in ("codex", "kimi", "grok", "claude"):
            accepted.append(self._resolve("hunt", name).provider)
        self.assertEqual(accepted, ["codex", "kimi", "grok", "claude"])
        for name in ("opencode", "agy", "unknown"):
            with self.assertRaises(self._error()):
                self._resolve("hunt", name)

    def test_awr_adds_opencode_agy_and_claude(self):
        providers = ["codex", "kimi", "grok", "opencode", "agy", "claude"]
        self.assertEqual(
            [self._resolve("awr", name).provider for name in providers], providers
        )

    def test_omitted_model_and_reasoning_emit_no_override_flags(self):
        render = self._api(provider_adapters, "render_command")
        for name in ("codex", "kimi", "grok"):
            capability = self._resolve("hunt", name)
            argv, environment = render(
                capability, pathlib.Path("/tmp/disposable-mirror"), "PROMPT"
            )
            self.assertNotIn("MODEL", argv)
            self.assertFalse(any("reasoning" in item or "variant" in item or "effort" in item for item in argv))
            self.assertEqual(
                environment,
                GROK_COMPATIBILITY_DISABLED if name == "grok" else {},
            )
        claude = self._resolve("hunt", "claude")
        argv, environment = render(
            claude,
            pathlib.Path("/tmp/disposable-mirror"),
            "PROMPT",
            response_schema={"type": "object"},
        )
        self.assertNotIn("--model", argv)
        self.assertNotIn("--effort", argv)
        self.assertEqual(environment, {})
        for name in ("opencode", "agy"):
            with self.subTest(provider=name), self.assertRaises(self._error()):
                render(
                    self._resolve("awr", name),
                    pathlib.Path("/tmp/disposable-mirror"),
                    "PROMPT",
                )

    def test_default_marker_is_shadow_only_without_effective_capacity_identity(self):
        capability = self._resolve("hunt", "codex")
        self.assertEqual(capability.model_identity, "provider-default")
        self.assertEqual(capability.reasoning_identity, "provider-default")
        self.assertFalse(capability.hard_complete_eligible)
        self.assertEqual(capability.authority, "shadow-only")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            capability.provider = "grok"

    def test_command_intent_is_distinct_from_probed_shadow_capability(self):
        strict = self._resolve(
            "hunt",
            "codex",
            probe_fn=lambda *args: probe(
                *args,
                effective_model="observed-model",
                effective_reasoning="high",
                immutable_capacity_identity="observed-capacity",
            ),
        )
        self.assertIsInstance(strict, provider_adapters.ProviderCapability)
        self.assertFalse(strict.hard_complete_eligible)
        self.assertEqual(strict.authority, "shadow-only")
        self.assertEqual(strict.model_identity, "observed-model")
        self.assertEqual(strict.reasoning_identity, "high")

        resolve_intent = self._api(
            provider_adapters, "_resolve_command_intent_for_test"
        )
        intent = resolve_intent(
            self._registry(),
            "hunt",
            "codex",
            model="requested-model",
            reasoning="high",
            executable_lookup=lambda _: str(FAKE),
        )
        intent_type = getattr(
            provider_adapters, "ProviderCommandIntent", None
        )
        self.assertIs(type(intent), intent_type)
        self.assertNotIsInstance(intent, provider_adapters.ProviderCapability)
        self.assertEqual(intent.requested_model, "requested-model")
        self.assertEqual(intent.requested_reasoning, "high")
        self.assertIsNone(intent.effective_model)
        self.assertIsNone(intent.effective_reasoning)
        self.assertIsNone(intent.model_override_applied)
        self.assertIsNone(intent.reasoning_override_applied)
        self.assertEqual(intent.provider_validation, "unverified")
        self.assertFalse(intent.hard_complete_eligible)

    def test_default_model_reasoning_or_cli_drift_stales_capability(self):
        resolve = self._api(provider_adapters, "_resolve_provider_for_test")
        current = self._resolve(
            "hunt",
            "codex",
            probe_fn=lambda *args: probe(
                *args,
                effective_model="effective-a",
                effective_reasoning="high",
                immutable_capacity_identity="capacity-a",
            ),
        )
        drifts = (
            dict(effective_model="effective-b", effective_reasoning="high", immutable_capacity_identity="capacity-a"),
            dict(effective_model="effective-a", effective_reasoning="low", immutable_capacity_identity="capacity-a"),
            dict(effective_model="effective-a", effective_reasoning="high", immutable_capacity_identity="capacity-b"),
            dict(effective_model="effective-a", effective_reasoning="high", immutable_capacity_identity="capacity-a", cli_revision="fake-cli-v2"),
        )
        for change in drifts:
            replacement = resolve(
                self._registry(), "hunt", "codex",
                executable_lookup=lambda _: str(FAKE),
                version_probe=lambda *args, change=change: probe(*args, **change),
            )
            self.assertNotEqual(current.profile_hash, replacement.profile_hash)
            self.assertFalse(
                self._api(provider_adapters, "capability_is_current")(
                    current, replacement
                )
            )

    def test_ignored_override_fails_before_launch(self):
        def ignored(provider, executable_path, model, reasoning):
            result = probe(provider, executable_path, model, reasoning)
            result["effective_model"] = "different-model"
            result["model_override_applied"] = False
            return result

        with self.assertRaises(self._error()):
            self._resolve("hunt", "codex", model="MODEL", probe_fn=ignored)

    def test_each_explicit_override_uses_verified_cli_spelling(self):
        cases = {
            "codex": ["-m", "MODEL", "-c", "model_reasoning_effort=high"],
            "kimi": ["-m", "MODEL"],
            "grok": ["-m", "MODEL", "--reasoning-effort", "high"],
            "opencode": ["-m", "safe/MODEL", "--variant", "high"],
            "agy": ["--model", "MODEL", "--effort", "high"],
            "claude": ["--model", "MODEL", "--effort", "high"],
        }
        render = self._api(provider_adapters, "render_command")
        for name, spelling in cases.items():
            surface = (
                "hunt"
                if name in ("codex", "kimi", "grok", "claude")
                else "awr"
            )
            reasoning = None if name == "kimi" else "high"
            model = "safe/MODEL" if name == "opencode" else "MODEL"
            if name in ("opencode", "agy"):
                capability = provider_adapters._resolve_command_intent_for_test(
                    self._registry(),
                    surface,
                    name,
                    model=model,
                    reasoning=reasoning,
                    executable_lookup=lambda _: str(FAKE),
                    model_catalog_probe=lambda *_: catalog(name, model),
                    version_probe=lambda *_: b"1.1.10\n",
                )
            else:
                capability = self._resolve(surface, name, model, reasoning)
            if name in ("agy", "claude"):
                argv, _ = render(
                    capability,
                    pathlib.Path("/mirror"),
                    "PROMPT",
                    response_schema={"type": "object"},
                )
            else:
                argv, _ = render(
                    capability, pathlib.Path("/mirror"), "PROMPT"
                )
            joined = "\0".join(argv)
            self.assertIn("\0".join(spelling), joined)

    def test_explicit_overrides_render_byte_exact_argv(self):
        response_schema = {
            "additionalProperties": False,
            "properties": {"answer": {"type": "string"}},
            "type": "object",
        }
        cases = {
            "codex": [
                str(FAKE), "-m", "MODEL", "-c",
                "model_reasoning_effort=high", "-c", "approval_policy=never",
                "exec", "-s", "workspace-write", "--skip-git-repo-check",
                "--ephemeral", "PROMPT",
            ],
            "kimi": [
                str(FAKE), "--auto", "--output-format", "text", "-m",
                "MODEL", "-p", "PROMPT",
            ],
            "grok": [
                str(FAKE), "--always-approve", "--no-memory",
                "--output-format", "json", "--cwd",
                "/mirror", "-m", "MODEL", "--reasoning-effort", "high",
                "-p", "PROMPT",
            ],
            "opencode": [
                str(FAKE), "run", "--pure", "--auto", "--dir", "/mirror",
                "-m", "safe/MODEL", "--variant", "high", "PROMPT",
            ],
            "agy": [
                str(FAKE), "--dangerously-skip-permissions",
                "--disable-slash-commands", "--output-format", "json",
                "--add-dir", "/mirror", "--model", "MODEL", "--effort",
                "high", "--json-schema",
                '{"additionalProperties":false,"properties":{"answer":'
                '{"type":"string"}},"type":"object"}',
                "--print", "PROMPT",
            ],
            "claude": [
                str(FAKE), "--bare", "--dangerously-skip-permissions",
                "--output-format", "json", "--add-dir",
                "/mirror", "--model", "MODEL", "--effort", "high",
                "--json-schema",
                '{"additionalProperties":false,"properties":{"answer":'
                '{"type":"string"}},"type":"object"}',
                "-p", "PROMPT",
            ],
        }
        render = self._api(provider_adapters, "render_command")
        for provider, expected in cases.items():
            with self.subTest(provider=provider):
                surface = (
                    "hunt"
                    if provider in {"codex", "kimi", "grok", "claude"}
                    else "awr"
                )
                model = "safe/MODEL" if provider == "opencode" else "MODEL"
                reasoning = None if provider == "kimi" else "high"
                if provider in {"opencode", "agy"}:
                    intent = provider_adapters._resolve_command_intent_for_test(
                        self._registry(),
                        surface,
                        provider,
                        model=model,
                        reasoning=reasoning,
                        executable_lookup=lambda _: str(FAKE),
                        model_catalog_probe=lambda *_: catalog(provider, model),
                        version_probe=lambda *_: b"1.1.10\n",
                    )
                else:
                    intent = self._resolve(surface, provider, model, reasoning)
                if provider in {"agy", "claude"}:
                    argv, environment = render(
                        intent,
                        pathlib.Path("/mirror"),
                        "PROMPT",
                        response_schema=response_schema,
                    )
                else:
                    argv, environment = render(
                        intent, pathlib.Path("/mirror"), "PROMPT"
                    )
                self.assertEqual(argv, expected)
                self.assertEqual(
                    environment,
                    (
                        GROK_COMPATIBILITY_DISABLED
                        if provider == "grok"
                        else {}
                    ),
                )

    def test_agy_runtime_render_requires_and_canonicalizes_inline_schema(self):
        schema = {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "additionalProperties": False,
        }
        intent = provider_adapters._resolve_command_intent_for_test(
            self._registry(),
            "awr",
            "agy",
            model="MODEL",
            reasoning="high",
            executable_lookup=lambda _: str(FAKE),
            model_catalog_probe=lambda *_: catalog("agy", "MODEL"),
            version_probe=lambda *_: b"1.1.10\n",
        )
        with self.assertRaises(self._error()):
            provider_adapters.render_command(
                intent, pathlib.Path("/mirror"), "PROMPT"
            )
        argv, _ = provider_adapters.render_command(
            intent,
            pathlib.Path("/mirror"),
            "PROMPT",
            response_schema=schema,
        )
        argument = argv[argv.index("--json-schema") + 1]
        self.assertEqual(
            argument,
            '{"additionalProperties":false,"properties":{"answer":'
            '{"type":"string"}},"type":"object"}',
        )
        self.assertFalse(argument.endswith("\n"))
        with self.assertRaises(self._error()):
            provider_adapters.render_command(
                intent,
                pathlib.Path("/mirror"),
                "PROMPT",
                "/legacy/schema.json",
                response_schema=schema,
            )

    def test_claude_runtime_render_requires_and_canonicalizes_inline_schema(self):
        schema = {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "additionalProperties": False,
        }
        intent = self._resolve("hunt", "claude", "MODEL", "high")
        with self.assertRaises(self._error()):
            provider_adapters.render_command(
                intent, pathlib.Path("/mirror"), "PROMPT"
            )
        argv, _ = provider_adapters.render_command(
            intent,
            pathlib.Path("/mirror"),
            "PROMPT",
            response_schema=schema,
        )
        argument = argv[argv.index("--json-schema") + 1]
        self.assertEqual(
            argument,
            '{"additionalProperties":false,"properties":{"answer":'
            '{"type":"string"}},"type":"object"}',
        )
        self.assertFalse(argument.endswith("\n"))
        self.assertIn("--bare", argv)
        self.assertNotIn("--tools", argv)

    def test_kimi_reasoning_and_unknown_provider_fail_before_launch(self):
        calls = []
        def no_launch_probe(*args):
            calls.append(args)
            return probe(*args)
        with self.assertRaises(self._error()):
            self._resolve("hunt", "kimi", reasoning="high", probe_fn=no_launch_probe)
        with self.assertRaises(self._error()):
            self._resolve("awr", "unknown", probe_fn=no_launch_probe)
        self.assertEqual(calls, [])

    def test_unsupported_reasoning_fails_before_executable_lookup(self):
        for surface, provider in (
            ("hunt", "codex"),
            ("hunt", "grok"),
            ("hunt", "claude"),
            ("awr", "opencode"),
            ("awr", "agy"),
            ("awr", "claude"),
        ):
            calls = []

            def lookup(executable):
                calls.append(executable)
                return str(FAKE)

            with self.subTest(provider=provider):
                with self.assertRaises(self._error()):
                    provider_adapters._resolve_command_intent_for_test(
                        self._registry(),
                        surface,
                        provider,
                        reasoning="definitely-invalid",
                        executable_lookup=lookup,
                    )
                self.assertEqual(calls, [])

    def test_safe_default_identity_changes_execution_profile(self):
        registry = self._registry()

        def resolve(effective_model):
            return provider_adapters._resolve_command_intent_for_test(
                registry,
                "awr",
                "opencode",
                executable_lookup=lambda _: str(FAKE),
                default_identity_probe=lambda *_: {
                    "schema_version": "provider-default-identity-v1",
                    "provider": "opencode",
                    "effective_model": effective_model,
                    "probe_revision": "fixture-default-probe-v1",
                },
                model_catalog_probe=lambda *_: catalog(
                    "opencode", effective_model
                ),
            )

        first = resolve("openai/model-a")
        second = resolve("openai/model-b")
        self.assertEqual(first.effective_model, "openai/model-a")
        argv, _ = provider_adapters.render_command(
            first, pathlib.Path("/mirror"), "PROMPT"
        )
        self.assertIn("\0-m\0openai/model-a\0", "\0" + "\0".join(argv) + "\0")
        self.assertIn(
            "\0-m\0openai/model-a\0",
            "\0"
            + "\0".join(provider_adapters.command_intent_record(first)["argv"])
            + "\0",
        )
        self.assertNotEqual(
            first.execution_request_profile_hash,
            second.execution_request_profile_hash,
        )

    def test_omitted_multibackend_default_requires_host_owned_probe(self):
        with mock.patch.object(
            provider_adapters,
            "_host_executable_lookup",
            return_value=str(FAKE),
        ), mock.patch.object(
            provider_adapters,
            "_host_default_identity_probe",
            return_value=None,
            create=True,
        ):
            for provider in ("opencode", "agy"):
                with self.subTest(provider=provider):
                    with self.assertRaises(self._error()):
                        provider_adapters.resolve_command_intent(
                            self._registry(),
                            "awr",
                            provider,
                        )

    def test_agy_omitted_model_rejects_injected_and_persisted_default_evidence(self):
        evidence = {
            "schema_version": "provider-default-identity-v1",
            "provider": "agy",
            "effective_model": "gemini/fixture-default",
            "probe_revision": "untrusted-agy-probe-v1",
        }
        with self.assertRaises(self._error()):
            provider_adapters._resolve_command_intent_for_test(
                self._registry(),
                "awr",
                "agy",
                executable_lookup=lambda _: str(FAKE),
                default_identity_probe=lambda *_: evidence,
                version_probe=lambda *_: b"1.1.10\n",
            )

        descriptor = {
            "surface": "awr",
            "provider": "agy",
            "requested_model": None,
            "requested_reasoning": None,
            "effective_model": evidence["effective_model"],
            "effective_reasoning": None,
            "default_probe_revision": evidence["probe_revision"],
            "execution_request_profile_hash": provider_adapters._command_profile_hash(
                self._registry(),
                "awr",
                "agy",
                None,
                None,
                evidence["effective_model"],
                None,
                evidence["probe_revision"],
            ),
        }
        with self.assertRaises(self._error()):
            provider_adapters.validate_command_profile_descriptor(
                self._registry(), descriptor
            )

    def test_default_identity_probe_kills_on_output_overflow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            marker = root / "continued-after-overflow"
            executable = root / "opencode"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys, time\n"
                "sys.stdout.write('x' * 65536)\n"
                "sys.stdout.flush()\n"
                "time.sleep(0.2)\n"
                f"pathlib.Path({str(marker)!r}).write_text('unsafe')\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            self.assertIsNone(
                provider_adapters._host_default_identity_probe(
                    "opencode", str(executable)
                )
            )
            self.assertFalse(marker.exists())

    def test_default_identity_probe_is_pure_config_introspection_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            audit = root / "probe.json"
            workload = root / "provider-workload"
            executable = root / "opencode"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                f"audit = pathlib.Path({str(audit)!r})\n"
                f"workload = pathlib.Path({str(workload)!r})\n"
                "if sys.argv[1:] != ['--pure', 'debug', 'config']:\n"
                "    workload.write_text('unsafe')\n"
                "    raise SystemExit(91)\n"
                "audit.write_text(json.dumps({\n"
                "    'argv': sys.argv[1:],\n"
                "    'cwd': os.getcwd(),\n"
                "    'provider_launch_log': os.environ.get('PROVIDER_LAUNCH_LOG'),\n"
                "}, sort_keys=True))\n"
                "print(json.dumps({'model': 'openai/fixture-default'}))\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            with mock.patch.dict(
                os.environ,
                {"PROVIDER_LAUNCH_LOG": str(workload)},
                clear=False,
            ):
                evidence = provider_adapters._host_default_identity_probe(
                    "opencode", str(executable)
                )
            self.assertEqual(
                evidence,
                {
                    "schema_version": "provider-default-identity-v1",
                    "provider": "opencode",
                    "effective_model": "openai/fixture-default",
                    "probe_revision": "opencode-pure-debug-config-v1",
                },
            )
            observed = json.loads(audit.read_text(encoding="utf-8"))
            self.assertEqual(observed["argv"], ["--pure", "debug", "config"])
            self.assertTrue(
                pathlib.Path(observed["cwd"]).name.startswith(
                    "provider-default-probe-"
                )
            )
            self.assertIsNone(observed["provider_launch_log"])
            self.assertFalse(workload.exists())

    def test_default_identity_probe_rejects_duplicate_model_key(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = pathlib.Path(directory) / "opencode"
            executable.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' "
                "'{\"model\":\"anthropic/claude\","
                "\"model\":\"openai/apparently-safe\"}'\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            self.assertIsNone(
                provider_adapters._host_default_identity_probe(
                    "opencode", str(executable)
                )
            )

    def test_registry_includes_claude_and_keeps_other_providers_clean(self):
        registry = self._registry()
        providers = list(registry["providers"])
        self.assertEqual(
            providers,
            ["codex", "kimi", "grok", "opencode", "agy", "claude"],
        )
        self.assertEqual(
            list(registry["surfaces"]["hunt"]),
            ["codex", "kimi", "grok", "claude"],
        )
        render = self._api(provider_adapters, "render_command")
        for name in ("codex", "kimi", "grok", "opencode", "agy", "claude"):
            surface = (
                "hunt"
                if name in ("codex", "kimi", "grok", "claude")
                else "awr"
            )
            model = None if surface == "hunt" else "safe-provider/model"
            if name in ("opencode", "agy"):
                resolved = provider_adapters._resolve_command_intent_for_test(
                    registry,
                    surface,
                    name,
                    model=model,
                    executable_lookup=lambda _: str(FAKE),
                    model_catalog_probe=lambda *_, name=name, model=model: catalog(
                        name, model
                    ),
                    version_probe=lambda *_: b"1.1.10\n",
                )
            else:
                resolved = self._resolve(surface, name, model=model)
            argv, _ = render(
                resolved,
                pathlib.Path("/mirror"),
                "P",
                response_schema=(
                    {"type": "object"} if name in ("agy", "claude") else None
                ),
            )
            if name == "claude":
                self.assertIn("--output-format", argv)
                self.assertIn("json", argv)
                self.assertIn("--json-schema", argv)
                self.assertIn("--bare", argv)
            else:
                # Ignore argv[0]; disposable paths may contain unrelated substrings.
                self.assertNotIn(
                    FORBIDDEN_PROVIDER,
                    "\0".join(argv[1:]).lower(),
                )

    def test_pool_failover_cannot_escape_declared_order(self):
        resolve_pool = self._api(provider_adapters, "_resolve_pool_for_test")
        provider_for_attempt = self._api(provider_adapters, "provider_for_attempt")
        pool = resolve_pool(
            self._registry(), "hunt", ["grok", "codex"],
            executable_lookup=lambda _: str(FAKE), version_probe=probe,
        )
        self.assertEqual([item.provider for item in pool], ["grok", "codex"])
        self.assertEqual(provider_for_attempt(pool, 0).provider, "grok")
        self.assertEqual(provider_for_attempt(pool, 1).provider, "codex")
        with self.assertRaises(self._error()):
            provider_for_attempt(pool, 2)

    def test_direct_resolution_rejects_unvalidated_registry_before_lookup(self):
        forged = json.loads(REGISTRY.read_text())
        forged["providers"]["codex"]["executable"] = "portable-wrapper"
        calls = []

        def lookup(executable):
            calls.append(("lookup", executable))
            return str(FAKE)

        def version_probe(*args):
            calls.append(("probe", args))
            return probe(*args)

        with self.assertRaises(self._error()):
            provider_adapters._resolve_provider_for_test(
                forged, "hunt", "codex",
                executable_lookup=lookup, version_probe=version_probe,
            )
        exact_copy = copy.deepcopy(json.loads(REGISTRY.read_text()))
        with self.assertRaises(self._error()):
            provider_adapters._resolve_provider_for_test(
                exact_copy, "hunt", "codex",
                executable_lookup=lookup, version_probe=version_probe,
            )
        self.assertEqual(calls, [])


class PortableAgentSmoke(unittest.TestCase):
    def _api(self, name):
        value = getattr(portable_agent, name, None)
        self.assertTrue(callable(value), f"missing behavior: portable_agent.{name}")
        return value

    def _capability(self):
        load_registry = getattr(provider_adapters, "load_registry", None)
        resolve_provider = getattr(
            provider_adapters, "_resolve_provider_for_test", None
        )
        self.assertTrue(callable(load_registry), "missing behavior: provider_adapters.load_registry")
        self.assertTrue(
            callable(resolve_provider),
            "missing behavior: provider_adapters._resolve_provider_for_test",
        )
        registry = load_registry(REGISTRY)
        return resolve_provider(
            registry, "hunt", "codex", model="MODEL", reasoning="high",
            executable_lookup=lambda _: str(FAKE), version_probe=probe,
        )

    def _error(self):
        value = getattr(portable_agent, "PortableAgentError", None)
        self.assertTrue(
            isinstance(value, type) and issubclass(value, Exception),
            "missing behavior: portable_agent.PortableAgentError",
        )
        return value

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

    def _run(self, root, mode, *, inputs=(), timeout=2, prompt_extra=None):
        request = {"mode": mode, "request_id": "request-1"}
        request.update(prompt_extra or {})
        return self._api("run_portable_attempt")(
            self._capability(), inputs=list(inputs), output_contract=self._contract(),
            prompt=json.dumps(request),
            state_root=root, timeout_seconds=timeout,
        )

    def test_manifest_only_mirror_and_validated_single_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "declared.txt"
            source.write_text("declared\n")
            manifest = [{
                "source_root": str(root), "source_path": "declared.txt",
                "provenance": "declared-input-v1",
                "path": "input/deep/declared.txt",
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "max_bytes": 64,
            }]
            result = self._run(root / "state", "undeclared-read", inputs=manifest)
            self.assertEqual(result["output_sha256"], self._contract()["sha256"])
            self.assertEqual(result["value"]["status"], "ok")
            state_mode = (root / "state").stat().st_mode & 0o777
            self.assertEqual(state_mode, 0o700)
            self.assertEqual(pathlib.Path(result["output_path"]).stat().st_mode & 0o777, 0o600)

    def test_owner_only_state_rejects_preexisting_import_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            state = root / "state"
            state.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (state / "imports").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(self._error()):
                self._run(state, "success")
            self.assertEqual(list(outside.iterdir()), [])

    def test_input_symlink_hardlink_and_reserved_paths_fail_before_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            real = root / "real.txt"
            real.write_text("x")
            symlink = root / "symlink.txt"
            symlink.symlink_to(real)
            hardlink = root / "hardlink.txt"
            os.link(real, hardlink)
            for source, target in (
                (symlink, "input/a.txt"),
                (hardlink, "input/b.txt"),
                (real, "ledger.tsv"),
                (real, ".git/config"),
                (real, ".claude/settings.json"),
                (real, "history.sqlite3"),
            ):
                with self.assertRaises(self._error()):
                    self._run(root / "state", "success", inputs=[{
                        "source_root": str(root), "source_path": source.name,
                        "provenance": "declared-input-v1", "path": target,
                        "sha256": hashlib.sha256(real.read_bytes()).hexdigest(),
                        "max_bytes": 64,
                    }])

    def test_reserved_source_cannot_hide_behind_benign_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            reserved_sources = (
                root / "ledger.tsv",
                root / "history.sqlite3",
                root / ".git" / "config",
                root / ".claude" / "settings.json",
                root / ".ai-ideas" / "durable-state.json",
            )
            for source in reserved_sources:
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text("durable\n")
                with self.subTest(source=source):
                    with self.assertRaises(self._error()):
                        self._run(root / "state", "success", inputs=[{
                            "source_root": str(root),
                            "source_path": source.relative_to(root).as_posix(),
                            "provenance": "declared-input-v1",
                            "path": "input/renamed-records.json",
                            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                            "max_bytes": 64,
                        }])

    def test_declared_regular_source_within_explicit_boundary_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source_root = root / "declared"
            source_root.mkdir()
            source = source_root / "records.json"
            source.write_text("declared\n")
            try:
                result = self._run(root / "state", "success", inputs=[{
                    "source_root": str(source_root),
                    "source_path": "records.json",
                    "provenance": "declared-input-v1",
                    "path": "input/records.json",
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "max_bytes": 64,
                }])
            except self._error() as exc:
                self.fail(f"declared regular source was rejected: {exc.code}")
            self.assertEqual(result["value"]["status"], "ok")

            outside = root / "outside.json"
            outside.write_text("outside\n")
            with self.assertRaises(self._error()):
                self._run(root / "outside-state", "success", inputs=[{
                    "source_root": str(source_root),
                    "source_path": "../outside.json",
                    "provenance": "declared-input-v1",
                    "path": "input/records.json",
                    "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
                    "max_bytes": 64,
                }])

    def test_source_path_rejects_symlink_ancestor_and_allows_normal_nested_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source_root = root / "declared"
            nested = source_root / "real" / "nested"
            nested.mkdir(parents=True)
            source = nested / "records.json"
            source.write_text("declared\n")
            common = {
                "source_root": str(source_root),
                "provenance": "declared-input-v1",
                "path": "input/records.json",
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "max_bytes": 64,
            }

            allowed = dict(common, source_path="real/nested/records.json")
            result = self._run(root / "normal-state", "success", inputs=[allowed])
            self.assertEqual(result["value"]["status"], "ok")

            (source_root / "link").symlink_to(source_root / "real", target_is_directory=True)
            bypass = dict(common, source_path="link/nested/records.json")
            with self.assertRaises(self._error()):
                self._run(root / "symlink-state", "success", inputs=[bypass])

    def test_invalid_outputs_are_never_imported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for mode in (
                "extra-file", "symlink", "hardlink", "oversize",
                "malformed-json", "nonzero",
            ):
                with self.subTest(mode=mode):
                    with self.assertRaises(self._error()):
                        self._run(root / mode, mode)

    def test_timeout_kills_the_entire_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            delayed = root / "child-survived.txt"
            with self.assertRaises(self._error()) as caught:
                self._run(
                    root / "state", "timeout", timeout=0.2,
                    prompt_extra={"delayed_path": str(delayed)},
                )
            self.assertEqual(caught.exception.code, "timeout")
            time.sleep(1.0)
            self.assertFalse(delayed.exists())


if __name__ == "__main__":
    unittest.main()
