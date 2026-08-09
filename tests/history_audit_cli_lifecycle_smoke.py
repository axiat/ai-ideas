#!/usr/bin/env python3
"""RED contract for the public audit-v2 shadow CLI lifecycle."""

import hashlib
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_audit_eval_v2
from lib import history_audit_plan
from tests import history_audit_eval_smoke as evaluation_fixture


CLI = ROOT / "lib/history_audit_cli.py"
PLAN_DOMAIN = b"history-audit-shadow-plan-v1\0"
OBSERVATION_DOMAIN = b"history-runtime-observation-v1\0"


def canonical_bytes(value):
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


class HistoryAuditCliLifecycleSmoke(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.database = self.root / "history.sqlite3"
        self.cas_root = self.root / "cas"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.launch_log = self.root / "provider-launched"
        fake = (
            "#!/bin/sh\n"
            "printf '%s\\n' \"$0\" >> \"$PROVIDER_LAUNCH_LOG\"\n"
            "exit 91\n"
        )
        for provider in ("codex", "kimi", "grok", "opencode", "agy", "claude"):
            executable = self.bin / provider
            executable.write_text(fake, encoding="utf-8")
            executable.chmod(0o755)
        raw_artifact_sha = sha256(b"bounded candidate artifact\n")
        self.candidate = {
            "candidate_id": "stg-v2-" + sha256(b"bounded candidate"),
            "candidate_hash": "",
            "raw_artifact_sha": raw_artifact_sha,
            "source_order": 0,
        }
        self.candidate["candidate_hash"] = (
            history_audit_plan.runtime_candidate_hash(self.candidate)
        )
        self.candidate_path = self.root / "candidate.json"
        self.candidate_path.write_bytes(canonical_bytes(self.candidate))

    def tearDown(self):
        self.temporary.cleanup()

    def _run(self, *arguments):
        environment = os.environ.copy()
        environment["PATH"] = (
            str(self.bin) + os.pathsep + environment.get("PATH", "")
        )
        environment["PROVIDER_LAUNCH_LOG"] = str(self.launch_log)
        return subprocess.run(
            [sys.executable, str(CLI), *map(str, arguments)],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=8,
            check=False,
        )

    def _canonical_stdout(self, completed):
        try:
            value = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.fail(f"CLI stdout is not one JSON value: {exc}")
        self.assertEqual(completed.stdout, canonical_bytes(value))
        return value

    def _init(self):
        completed = self._run(
            "init",
            "--db",
            self.database,
            "--cas-root",
            self.cas_root,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        value = self._canonical_stdout(completed)
        self.assertEqual(
            value,
            {
                "cas_initialized": True,
                "database_initialized": True,
                "schema_version": "history-audit-init-v1",
                "status": "ready",
            },
        )
        self.assertTrue(self.database.is_file())
        self.assertTrue(self.cas_root.is_dir())
        self.assertFalse(self.launch_log.exists())

    def _plan(self, output, *extra):
        return self._run(
            "plan",
            "--db",
            self.database,
            "--candidate",
            self.candidate_path,
            "--intent",
            "duplicate_search",
            "--output",
            output,
            *extra,
        )

    def _assert_shadow_plan(
        self,
        value,
        *,
        scope,
        l1_sha=None,
        profiles=(),
    ):
        self.assertEqual(
            set(value),
            {
                "schema_version",
                "candidate",
                "intent",
                "status",
                "reason_code",
                "observation_scope",
                "l1_observation_sha256",
                "batch_sha256",
                "direction",
                "execution_request_profiles",
                "hard_complete_work_created",
                "production_no_match_authorized",
                "authority",
                "plan_sha256",
            },
        )
        self.assertEqual(value["schema_version"], "history-audit-shadow-plan-v1")
        self.assertEqual(value["candidate"], self.candidate)
        self.assertEqual(value["intent"], "duplicate_search")
        self.assertEqual(value["status"], "producer_unavailable")
        self.assertEqual(value["reason_code"], "unbudgetable_provider")
        self.assertEqual(value["observation_scope"], scope)
        self.assertEqual(value["l1_observation_sha256"], l1_sha)
        self.assertIsNone(value["batch_sha256"])
        self.assertIsNone(value["direction"])
        self.assertEqual(value["execution_request_profiles"], list(profiles))
        self.assertFalse(value["hard_complete_work_created"])
        self.assertFalse(value["production_no_match_authorized"])
        self.assertEqual(value["authority"], "shadow-only")
        material = dict(value)
        plan_sha = material.pop("plan_sha256")
        self.assertEqual(plan_sha, sha256(PLAN_DOMAIN + canonical_bytes(material)))

    def _assert_zero_hard_work(self):
        with sqlite3.connect(self.database) as connection:
            for table in (
                "audit_logical_tasks",
                "audit_task_attempts",
                "audit_semantic_release_authorizations_v2",
            ):
                count = connection.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0]
                self.assertEqual(count, 0, table)

    def _provider_profile(self, name, *, model=None, reasoning=None):
        arguments = [
            "provider-command",
            "--surface",
            "hunt",
            "--provider",
            name,
        ]
        if model is not None:
            arguments.extend(("--model", model))
        if reasoning is not None:
            arguments.extend(("--reasoning", reasoning))
        completed = self._run(*arguments)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        profile = self._canonical_stdout(completed)
        for field, expected in {
            "schema_version": "provider-command-v1",
            "surface": "hunt",
            "provider": name,
            "requested_model": model,
            "requested_reasoning": reasoning,
            "effective_model": None,
            "effective_reasoning": None,
            "model_override_applied": None,
            "reasoning_override_applied": None,
            "hard_complete_eligible": False,
            "authority": "shadow-only",
            "execution_boundary": "portable-mirror-v1",
            "provider_validation": "unverified",
        }.items():
            self.assertEqual(profile.get(field), expected, field)
        self.assertRegex(
            profile.get("execution_request_profile_hash", ""),
            r"^[0-9a-f]{64}$",
        )
        self.assertFalse(self.launch_log.exists())
        path = self.root / f"{name}-provider-command.json"
        path.write_bytes(completed.stdout)
        descriptor = {
            "surface": profile["surface"],
            "provider": profile["provider"],
            "requested_model": profile["requested_model"],
            "requested_reasoning": profile["requested_reasoning"],
            "effective_model": profile["effective_model"],
            "effective_reasoning": profile["effective_reasoning"],
            "default_probe_revision": profile["default_probe_revision"],
            "model_catalog_probe_revision": profile[
                "model_catalog_probe_revision"
            ],
            "model_catalog_sha256": profile["model_catalog_sha256"],
            "max_output_tokens": profile["max_output_tokens"],
            "output_token_cap_binding": profile[
                "output_token_cap_binding"
            ],
            "output_token_cap_semantics": profile[
                "output_token_cap_semantics"
            ],
            "execution_request_profile_hash": profile[
                "execution_request_profile_hash"
            ],
        }
        return path, descriptor

    def _l1_observation(
        self,
        *,
        intent="duplicate_search",
        candidate_id=None,
        candidate_content_sha256=None,
        empty=False,
    ):
        pack_path = "shadow-input/retrieval-pack.json"
        observations = [] if empty else [
            {
                "intent": intent,
                "retrieval_status": "complete",
                "status": "complete",
                "pack_path": pack_path,
                "comparison_path": None,
                "receipt_path": None,
                "attempts": [
                    {
                        "pack_path": pack_path,
                        "comparison_path": None,
                        "receipt_path": None,
                        "status": "complete",
                    }
                ],
            }
        ]
        material = {
            "schema_version": 1,
            "candidate_id": candidate_id or self.candidate["candidate_id"],
            "candidate_content_sha256": (
                candidate_content_sha256
                or self.candidate["raw_artifact_sha"]
            ),
            "observations": observations,
        }
        value = dict(material)
        value["observation_sha256"] = sha256(
            OBSERVATION_DOMAIN + canonical_bytes(material)
        )
        path = self.root / "build-observation.json"
        raw = canonical_bytes(value)
        path.write_bytes(raw)
        return path, sha256(raw)

    def test_unbudgetable_configuration_shadow_lifecycle_is_closed(self):
        self._init()
        plan_path = self.root / "shadow-plan.json"
        completed = self._plan(plan_path)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        plan = self._canonical_stdout(completed)
        self.assertEqual(plan_path.read_bytes(), completed.stdout)
        self._assert_shadow_plan(plan, scope="configuration_shadow")
        self._assert_zero_hard_work()

        state = self.root / "execution-state"
        for command in ("run", "resume"):
            with self.subTest(command=command):
                attempted = self._run(
                    command,
                    "--plan",
                    plan_path,
                    "--state",
                    state,
                )
                self.assertNotEqual(attempted.returncode, 0)
                result = self._canonical_stdout(attempted)
                self.assertEqual(
                    result,
                    {
                        "command": command,
                        "plan_sha256": plan["plan_sha256"],
                        "reason_code": "producer_unavailable",
                        "schema_version": "history-audit-execution-status-v1",
                        "status": "plan_not_executable",
                    },
                )
                self.assertFalse(state.exists())
                self.assertFalse(self.launch_log.exists())
                self._assert_zero_hard_work()

        verified = self._run("verify", "--receipt", plan_path)
        self.assertEqual(verified.returncode, 0, verified.stderr.decode())
        verification = self._canonical_stdout(verified)
        self.assertEqual(
            verification,
            {
                "authority": "shadow-only",
                "plan_sha256": plan["plan_sha256"],
                "production_no_match_authorized": False,
                "receipt_sha256": sha256(plan_path.read_bytes()),
                "schema_version": "history-audit-verification-v1",
                "status": "verified",
            },
        )
        self.assertFalse(self.launch_log.exists())

        tampered = dict(plan)
        tampered["candidate"] = dict(tampered["candidate"])
        tampered["candidate"]["source_order"] = 1
        tampered_path = self.root / "tampered-shadow-plan.json"
        tampered_path.write_bytes(canonical_bytes(tampered))
        rejected = self._run("verify", "--receipt", tampered_path)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(rejected.stdout, b"")
        self.assertIn(b"invalid", rejected.stderr.lower())

    def test_plan_binds_repeated_profiles_and_only_sealed_l1_is_l1_shadow(self):
        self._init()
        base_path, base_descriptor = self._provider_profile("kimi")
        review_path, review_descriptor = self._provider_profile(
            "grok", model="grok-4.5", reasoning="high"
        )
        profiles = (base_descriptor, review_descriptor)

        configuration_path = self.root / "profile-only-plan.json"
        profile_only = self._plan(
            configuration_path,
            "--execution-request-profile",
            base_path,
            "--execution-request-profile",
            review_path,
        )
        self.assertEqual(profile_only.returncode, 0, profile_only.stderr.decode())
        configuration = self._canonical_stdout(profile_only)
        self._assert_shadow_plan(
            configuration,
            scope="configuration_shadow",
            profiles=profiles,
        )

        observation_path, observation_file_sha = self._l1_observation()
        l1_path = self.root / "l1-shadow-plan.json"
        planned = self._plan(
            l1_path,
            "--execution-request-profile",
            base_path,
            "--execution-request-profile",
            review_path,
            "--l1-observation",
            observation_path,
        )
        self.assertEqual(planned.returncode, 0, planned.stderr.decode())
        l1_plan = self._canonical_stdout(planned)
        self.assertEqual(l1_path.read_bytes(), planned.stdout)
        self._assert_shadow_plan(
            l1_plan,
            scope="l1_shadow",
            l1_sha=observation_file_sha,
            profiles=profiles,
        )
        self.assertNotEqual(
            configuration["plan_sha256"], l1_plan["plan_sha256"]
        )
        self._assert_zero_hard_work()
        self.assertFalse(self.launch_log.exists())

        invalid_observations = []
        tampered = json.loads(observation_path.read_text(encoding="utf-8"))
        tampered["candidate_content_sha256"] = "f" * 64
        tampered_path = self.root / "tampered-build-observation.json"
        tampered_path.write_bytes(canonical_bytes(tampered))
        invalid_observations.append(("broken-self-hash", tampered_path))
        wrong_id_path, _ = self._l1_observation(candidate_id="stg-v2-wrong")
        wrong_id_copy = self.root / "wrong-id-build-observation.json"
        wrong_id_copy.write_bytes(wrong_id_path.read_bytes())
        wrong_sha_path, _ = self._l1_observation(
            candidate_content_sha256="f" * 64
        )
        wrong_sha_copy = self.root / "wrong-sha-build-observation.json"
        wrong_sha_copy.write_bytes(wrong_sha_path.read_bytes())
        empty_path, _ = self._l1_observation(empty=True)
        empty_copy = self.root / "empty-build-observation.json"
        empty_copy.write_bytes(empty_path.read_bytes())
        wrong_path, _ = self._l1_observation(intent="novelty_search")
        wrong_copy = self.root / "wrong-intent-build-observation.json"
        wrong_copy.write_bytes(wrong_path.read_bytes())
        invalid_observations.extend(
            (
                ("wrong-candidate-id", wrong_id_copy),
                ("wrong-raw-artifact-sha", wrong_sha_copy),
                ("empty", empty_copy),
                ("wrong-intent", wrong_copy),
            )
        )
        for label, invalid_observation in invalid_observations:
            with self.subTest(invalid_l1=label):
                rejected_path = self.root / f"{label}-l1-plan.json"
                rejected = self._plan(
                    rejected_path,
                    "--execution-request-profile",
                    base_path,
                    "--l1-observation",
                    invalid_observation,
                )
                self.assertNotEqual(rejected.returncode, 0)
                self.assertEqual(rejected.stdout, b"")
                self.assertFalse(rejected_path.exists())
                self._assert_zero_hard_work()

    def test_plan_rejects_nonclosed_or_mishashed_runtime_candidate(self):
        self._init()
        for label, mutate in (
            ("extra", lambda value: value.update(extra="forbidden")),
            (
                "mishashed",
                lambda value: value.update(raw_artifact_sha="f" * 64),
            ),
        ):
            with self.subTest(label=label):
                invalid = dict(self.candidate)
                mutate(invalid)
                self.candidate_path.write_bytes(canonical_bytes(invalid))
                output = self.root / f"{label}-plan.json"
                rejected = self._plan(output)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertEqual(rejected.stdout, b"")
                self.assertFalse(output.exists())
                self.assertFalse(self.launch_log.exists())
                self._assert_zero_hard_work()

    def test_verify_reconstructs_compact_provider_profile_descriptors(self):
        self._init()
        profile_path, _ = self._provider_profile("kimi")
        plan_path = self.root / "provider-bound-plan.json"
        planned = self._plan(
            plan_path,
            "--execution-request-profile",
            profile_path,
        )
        self.assertEqual(planned.returncode, 0, planned.stderr.decode())
        plan = self._canonical_stdout(planned)

        verified = self._run("verify", "--receipt", plan_path)
        self.assertEqual(verified.returncode, 0, verified.stderr.decode())
        verification = self._canonical_stdout(verified)
        self.assertEqual(verification["status"], "verified")
        self.assertEqual(verification["plan_sha256"], plan["plan_sha256"])
        self.assertFalse(self.launch_log.exists())

        attacks = {
            "unsupported-kimi-reasoning": {
                "requested_reasoning": "unsupported",
            },
            "self-consistent-wrong-profile-hash": {
                "execution_request_profile_hash": "b" * 64,
            },
        }
        for label, updates in attacks.items():
            with self.subTest(label=label):
                tampered = json.loads(json.dumps(plan))
                tampered["execution_request_profiles"][0].update(updates)
                material = dict(tampered)
                material.pop("plan_sha256")
                tampered["plan_sha256"] = sha256(
                    PLAN_DOMAIN + canonical_bytes(material)
                )
                receipt = self.root / f"{label}.json"
                receipt.write_bytes(canonical_bytes(tampered))

                verified = self._run("verify", "--receipt", receipt)
                self.assertNotEqual(verified.returncode, 0)
                self.assertEqual(verified.stdout, b"")
                self.assertIn(b"profile", verified.stderr.lower())
                self.assertFalse(self.launch_log.exists())

    def test_evaluate_is_closed_shadow_only_and_never_publishes_authority(self):
        rows = evaluation_fixture.qrels(
            30,
            20,
            scope="diagnostic_synthetic",
        )
        bundle = {
            "schema_version": "history-audit-qrels-bundle-v1",
            "scope": "diagnostic_synthetic",
            "qrels": rows,
            "partitions": evaluation_fixture.partitions(rows),
            "policy": evaluation_fixture.policy(),
        }
        predicted = evaluation_fixture.outputs(rows)
        qrels_path = self.root / "qrels-bundle.json"
        outputs_path = self.root / "outputs.json"
        qrels_path.write_bytes(canonical_bytes(bundle))
        outputs_path.write_bytes(canonical_bytes(predicted))

        completed = self._run(
            "evaluate",
            "--qrels",
            qrels_path,
            "--outputs",
            outputs_path,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        result = self._canonical_stdout(completed)
        validated = history_audit_eval_v2.validate_qrels(
            rows,
            bundle["partitions"],
            scope=bundle["scope"],
        )
        expected = history_audit_eval_v2.evaluate_shadow_readiness(
            validated,
            predicted,
            bundle["policy"],
        )
        self.assertEqual(result, expected)
        self.assertEqual(result["readiness_state"], "shadow_ready")
        self.assertFalse(result["production_qualified"])
        self.assertFalse(self.launch_log.exists())


if __name__ == "__main__":
    unittest.main()
