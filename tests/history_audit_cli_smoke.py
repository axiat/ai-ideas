#!/usr/bin/env python3
"""Offline public-CLI contract for portable provider diagnostics."""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = ROOT / "lib/history_audit_cli.py"


class ProviderCommandCliSmoke(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.launch_log = self.root / "provider-launched"
        raw = (
            "#!/bin/sh\n"
            "printf '%s\\n' \"$0\" >> \"$PROVIDER_LAUNCH_LOG\"\n"
            "exit 91\n"
        )
        for provider in ("codex", "kimi", "grok", "opencode", "agy"):
            executable = self.bin / provider
            executable.write_text(raw, encoding="utf-8")
            executable.chmod(0o755)

    def tearDown(self):
        self.temporary.cleanup()

    def _run(self, surface, provider, *, model=None, reasoning=None):
        command = [
            sys.executable,
            str(CLI),
            "provider-command",
            "--surface",
            surface,
            "--provider",
            provider,
        ]
        if model is not None:
            command.extend(["--model", model])
        if reasoning is not None:
            command.extend(["--reasoning", reasoning])
        environment = os.environ.copy()
        environment["PATH"] = str(self.bin) + os.pathsep + environment.get("PATH", "")
        environment["PROVIDER_LAUNCH_LOG"] = str(self.launch_log)
        return subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )

    def _payload(self, surface, provider, *, model=None, reasoning=None):
        completed = self._run(
            surface, provider, model=model, reasoning=reasoning
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"provider-command failed: {completed.stderr}",
        )
        self.assertFalse(
            self.launch_log.exists(), "provider-command launched a provider"
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"provider-command did not print one JSON value: {exc}")
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ) + "\n"
        self.assertEqual(completed.stdout, canonical)
        return payload

    @staticmethod
    def _normalized_argv(payload):
        argv = list(payload["argv"])
        argv[0] = "<EXECUTABLE>"
        for flag in ("--cwd", "--dir", "--add-dir"):
            if flag in argv:
                argv[argv.index(flag) + 1] = "<MIRROR>"
        provider = payload["provider"]
        if provider in {"kimi", "grok"}:
            argv[argv.index("-p") + 1] = "<PROMPT>"
        elif provider == "agy":
            argv[argv.index("--print") + 1] = "<PROMPT>"
        else:
            argv[-1] = "<PROMPT>"
        return argv

    def test_provider_command_prints_canonical_json_without_launch(self):
        payload = self._payload(
            "hunt", "codex", model="gpt-5.6-sol", reasoning="xhigh"
        )
        for field, expected in {
            "schema_version": "provider-command-v1",
            "surface": "hunt",
            "provider": "codex",
            "model_override": "gpt-5.6-sol",
            "reasoning_override": "xhigh",
            "model_default": False,
            "reasoning_default": False,
            "hard_complete_eligible": False,
            "authority": "shadow-only",
            "execution_boundary": "portable-mirror-v1",
        }.items():
            self.assertEqual(payload.get(field), expected, field)
        self.assertEqual(payload.get("environment"), {})

    def test_provider_command_enforces_closed_surface_sets_before_launch(self):
        for provider in ("codex", "kimi", "grok"):
            with self.subTest(surface="hunt", provider=provider):
                self.assertEqual(self._payload("hunt", provider)["provider"], provider)
        for provider in ("codex", "kimi", "grok", "opencode", "agy"):
            with self.subTest(surface="awr", provider=provider):
                self.assertEqual(self._payload("awr", provider)["provider"], provider)
        for surface, provider in (
            ("hunt", "opencode"),
            ("hunt", "agy"),
            ("hunt", "unknown"),
            ("awr", "unknown"),
        ):
            with self.subTest(surface=surface, provider=provider):
                completed = self._run(surface, provider)
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(completed.stdout, "")
        self.assertFalse(self.launch_log.exists())

    def test_provider_command_emits_exact_override_argv_and_default_markers(self):
        cases = (
            (
                "hunt",
                "codex",
                "gpt-5.6-sol",
                "xhigh",
                [
                    "<EXECUTABLE>", "-m", "gpt-5.6-sol", "-c",
                    "model_reasoning_effort=xhigh", "-c", "approval_policy=never",
                    "exec", "-s", "workspace-write", "--skip-git-repo-check",
                    "--ephemeral", "<PROMPT>",
                ],
            ),
            (
                "hunt",
                "kimi",
                "kimi-code/k3",
                None,
                [
                    "<EXECUTABLE>", "--auto", "--output-format", "text", "-m",
                    "kimi-code/k3", "-p", "<PROMPT>",
                ],
            ),
            (
                "hunt",
                "grok",
                "grok-4.5",
                "high",
                [
                    "<EXECUTABLE>", "--always-approve", "--no-memory",
                    "--no-subagents", "--output-format", "plain", "--cwd",
                    "<MIRROR>", "-m", "grok-4.5", "--reasoning-effort", "high",
                    "-p", "<PROMPT>",
                ],
            ),
            (
                "awr",
                "opencode",
                "openai/gpt-5.6-sol",
                "high",
                [
                    "<EXECUTABLE>", "run", "--pure", "--auto", "--dir",
                    "<MIRROR>", "-m", "openai/gpt-5.6-sol", "--variant", "high",
                    "<PROMPT>",
                ],
            ),
            (
                "awr",
                "agy",
                "gemini-3.6-flash-high",
                "high",
                [
                    "<EXECUTABLE>", "--dangerously-skip-permissions",
                    "--disable-slash-commands", "--output-format", "text",
                    "--add-dir", "<MIRROR>", "--model", "gemini-3.6-flash-high",
                    "--effort", "high", "--print", "<PROMPT>",
                ],
            ),
        )
        for surface, provider, model, reasoning, expected in cases:
            with self.subTest(provider=provider):
                payload = self._payload(
                    surface, provider, model=model, reasoning=reasoning
                )
                self.assertEqual(self._normalized_argv(payload), expected)

        default = self._payload("hunt", "codex")
        self.assertTrue(default.get("model_default"))
        self.assertTrue(default.get("reasoning_default"))
        self.assertEqual(default.get("model_identity"), "provider-default")
        self.assertEqual(default.get("reasoning_identity"), "provider-default")
        self.assertNotIn("-m", default["argv"])
        self.assertFalse(
            any("model_reasoning_effort=" in item for item in default["argv"])
        )

    def test_kimi_reasoning_is_rejected_without_launch(self):
        completed = self._run(
            "hunt", "kimi", model="kimi-code/k3", reasoning="high"
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertIn("kimi", completed.stderr.lower())
        self.assertIn("reasoning", completed.stderr.lower())
        self.assertFalse(self.launch_log.exists())


if __name__ == "__main__":
    unittest.main()
