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


ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

from lib import portable_agent
from lib import provider_adapters


REGISTRY = ROOT / "history/provider-adapters-v1.json"
FAKE = ROOT / "tests/fake_portable_agent.py"
FORBIDDEN_PROVIDER = "cl" + "aude"


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
        "evidence_sha256": hashlib.sha256(
            f"{provider}|{executable_path}|{cli_revision}|{effective_model}|"
            f"{effective_reasoning}|{immutable_capacity_identity}".encode()
        ).hexdigest(),
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
        return self._api(provider_adapters, "resolve_provider")(
            self._registry(),
            surface,
            provider,
            model=model,
            reasoning=reasoning,
            executable_lookup=lambda _: str(FAKE),
            version_probe=probe_fn,
        )

    def test_hunt_accepts_exactly_codex_kimi_grok(self):
        accepted = []
        for name in ("codex", "kimi", "grok"):
            accepted.append(self._resolve("hunt", name).provider)
        self.assertEqual(accepted, ["codex", "kimi", "grok"])
        for name in ("opencode", "agy", "unknown"):
            with self.assertRaises(self._error()):
                self._resolve("hunt", name)

    def test_awr_adds_opencode_and_agy(self):
        providers = ["codex", "kimi", "grok", "opencode", "agy"]
        self.assertEqual(
            [self._resolve("awr", name).provider for name in providers], providers
        )

    def test_omitted_model_and_reasoning_emit_no_override_flags(self):
        render = self._api(provider_adapters, "render_command")
        for name in ("codex", "kimi", "grok", "opencode", "agy"):
            surface = "hunt" if name in ("codex", "kimi", "grok") else "awr"
            capability = self._resolve(surface, name)
            argv, environment = render(
                capability, pathlib.Path("/tmp/disposable-mirror"), "PROMPT"
            )
            self.assertNotIn("MODEL", argv)
            self.assertFalse(any("reasoning" in item or "variant" in item or "effort" in item for item in argv))
            self.assertEqual(environment, {})

    def test_default_marker_is_shadow_only_without_effective_capacity_identity(self):
        capability = self._resolve("hunt", "codex")
        self.assertEqual(capability.model_identity, "provider-default")
        self.assertEqual(capability.reasoning_identity, "provider-default")
        self.assertFalse(capability.hard_complete_eligible)
        self.assertEqual(capability.authority, "shadow-only")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            capability.provider = "grok"

    def test_default_model_reasoning_or_cli_drift_stales_capability(self):
        resolve = self._api(provider_adapters, "resolve_provider")
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
            "opencode": ["-m", "MODEL", "--variant", "high"],
            "agy": ["--model", "MODEL", "--effort", "high"],
        }
        render = self._api(provider_adapters, "render_command")
        for name, spelling in cases.items():
            surface = "hunt" if name in ("codex", "kimi", "grok") else "awr"
            reasoning = None if name == "kimi" else "high"
            capability = self._resolve(surface, name, "MODEL", reasoning)
            argv, _ = render(capability, pathlib.Path("/mirror"), "PROMPT")
            joined = "\0".join(argv)
            self.assertIn("\0".join(spelling), joined)

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

    def test_registry_and_resolved_commands_have_no_claude_path(self):
        registry = self._registry()
        self.assertNotIn(FORBIDDEN_PROVIDER, json.dumps(registry, default=list).lower())
        render = self._api(provider_adapters, "render_command")
        for name in ("codex", "kimi", "grok", "opencode", "agy"):
            surface = "hunt" if name in ("codex", "kimi", "grok") else "awr"
            argv, _ = render(self._resolve(surface, name), pathlib.Path("/mirror"), "P")
            self.assertNotIn(FORBIDDEN_PROVIDER, "\0".join(argv).lower())
        with self.assertRaises(self._error()):
            provider_adapters.resolve_provider(
                registry, "hunt", "codex",
                executable_lookup=lambda _: "/tmp/" + FORBIDDEN_PROVIDER,
                version_probe=probe,
            )

    def test_pool_failover_cannot_escape_declared_order(self):
        resolve_pool = self._api(provider_adapters, "resolve_pool")
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
            provider_adapters.resolve_provider(
                forged, "hunt", "codex",
                executable_lookup=lookup, version_probe=version_probe,
            )
        exact_copy = copy.deepcopy(json.loads(REGISTRY.read_text()))
        with self.assertRaises(self._error()):
            provider_adapters.resolve_provider(
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
        resolve_provider = getattr(provider_adapters, "resolve_provider", None)
        self.assertTrue(callable(load_registry), "missing behavior: provider_adapters.load_registry")
        self.assertTrue(callable(resolve_provider), "missing behavior: provider_adapters.resolve_provider")
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
