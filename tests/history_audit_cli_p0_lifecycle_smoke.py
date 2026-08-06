#!/usr/bin/env python3
"""P0 offline acceptance contract for the durable audit-v2 CLI lifecycle.

The positive path is explicitly test-only-shadow.  Its executable is a local
deterministic fixture whose regular-file bytes are sealed by the planning
bundle.  No registered provider executable is allowed to run.
"""

import copy
import hashlib
import json
import os
import pathlib
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib import history_audit_plan  # noqa: E402
from lib import history_audit_store  # noqa: E402

CLI = ROOT / "lib/history_audit_cli.py"
TEST_INPUT_SCHEMA = "history-audit-cli-test-only-shadow-input-v1"
TEST_PLAN_SCHEMA = "history-audit-cli-test-only-plan-v1"
TEST_PROVIDER_PROTOCOL = "history-audit-test-provider-stdio-v1"
RECEIPT_FIELDS = {
    "manifest_schema_version",
    "canonical_codec_version",
    "run_id",
    "plan_hash",
    "candidate_hash",
    "snapshot_id",
    "snapshot_hash",
    "history_as_of_watermark",
    "current_batch_id_namespace",
    "current_batch_ids_hash",
    "exclusion_policy_sha",
    "expected_asset_ids_hash",
    "observed_asset_ids_hash",
    "missing_ids",
    "duplicate_ids",
    "extra_ids",
    "invalid_schema",
    "invalid_anchor",
    "truncated",
    "provider_pools_ordered",
    "provider_capability_profile_hashes",
    "capacity_profile_id",
    "semantic_policy_profile_id",
    "risk_policy_version",
    "matched_router_rule_ids",
    "settlement_policy_sha",
    "shard_plan_sha",
    "logical_task_hashes",
    "attempt_manifest_hashes",
    "raw_request_output_cas_hashes",
    "minimum_receipt_sha",
    "coverage_complete",
    "adjudication_complete",
    "semantic_policy_qualified",
    "no_match_basis",
    "final_status",
    "stage_reason_code",
    "evidence_anchors",
}


def canonical_bytes(value):
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def framed_sha256(domain, *parts):
    digest = hashlib.sha256()
    for part in (domain.encode("utf-8"), *parts):
        digest.update(struct.pack(">Q", len(part)))
        digest.update(part)
    return digest.hexdigest()


def canonical_sha(domain, value):
    return framed_sha256(domain, canonical_bytes(value))


def ordered_set_sha(domain, values):
    return canonical_sha(domain, sorted(values))


def self_hashed(domain, material, field):
    value = dict(material)
    value[field] = canonical_sha(domain, material)
    return value


class HistoryAuditCliP0LifecycleSmoke(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name).resolve()
        self.database = self.root / "history.sqlite3"
        self.cas_root = self.root / "cas"
        self.plan_path = self.root / "plan.json"
        self.state_path = self.root / "state.json"
        self.receipt_path = self.root / "receipt.json"
        self.candidate_path = self.root / "candidate.json"
        self.input_path = self.root / "test-only-shadow-input.json"
        self.fake_log = self.root / "fake-provider.jsonl"
        self.real_launch_log = self.root / "real-provider-launched"
        self.bin = self.root / "bin"
        self.bin.mkdir()

        trap = (
            "#!/bin/sh\n"
            "printf '%s\\n' \"$0\" >> \"$REAL_PROVIDER_LAUNCH_LOG\"\n"
            "exit 97\n"
        )
        for provider in ("codex", "kimi", "grok", "opencode", "agy", "claude"):
            executable = self.bin / provider
            executable.write_text(trap, encoding="utf-8")
            executable.chmod(0o700)

        wrapper = (ROOT / "history/test-only-provider-v1.py").read_bytes()
        self.fake_executable = self.root / "offline-fake-provider"
        self.fake_executable.write_bytes(wrapper)
        self.fake_executable.chmod(0o700)
        self.fake_executable_bytes = wrapper
        self.fixture_mode = None
        self.fixture_child_marker = self.root / "fixture-child-marker"
        self.fixture_pid_file = pathlib.Path(
            str(self.fixture_child_marker) + ".pid"
        )

        raw_candidate = b"bounded p0 offline candidate\n"
        candidate_id = "stg-v2-" + sha256(b"p0-cli-candidate-id")
        candidate_material = {
            "candidate_id": candidate_id,
            "raw_artifact_sha": sha256(raw_candidate),
            "source_order": 0,
        }
        self.candidate = {
            **candidate_material,
            "candidate_hash": canonical_sha(
                "history-runtime-candidate-v2", candidate_material
            ),
        }
        self.candidate_path.write_bytes(canonical_bytes(self.candidate))

        records = [
            {
                "item_id": "asset-1",
                "artifact_sha": sha256(b"alpha evidence"),
                "content": "alpha evidence",
                "lineage_id": "lineage-a",
            },
            {
                "item_id": "asset-2",
                "artifact_sha": sha256(b"beta evidence"),
                "content": "beta evidence",
                "lineage_id": "lineage-b",
            },
        ]
        run_id = "run-cli-p0"
        batch_id = "batch-cli-p0"
        current_ids = [candidate_id]
        expected_ids = [record["item_id"] for record in records]
        snapshot_material = {
            "run_id": run_id,
            "batch_id": batch_id,
            "history_as_of_watermark": 17,
            "current_batch_id_namespace": "history-v2-staging-v1",
            "current_batch_ids_hash": ordered_set_sha(
                "history-current-batch-ids-v2", current_ids
            ),
            "exclusion_policy_sha": sha256(b"p0-exclusion-policy"),
            "expected_asset_ids_hash": ordered_set_sha(
                "history-snapshot-assets-v2", expected_ids
            ),
        }
        snapshot_hash = canonical_sha(
            "history-snapshot-v2", snapshot_material
        )
        snapshot = {
            "snapshot_id": canonical_sha(
                "history-snapshot-id-v2",
                {
                    "run_id": run_id,
                    "batch_id": batch_id,
                    "snapshot_hash": snapshot_hash,
                },
            ),
            "snapshot_hash": snapshot_hash,
            "history_as_of_watermark": 17,
            "current_batch_id_namespace": "history-v2-staging-v1",
            "current_batch_ids_hash": snapshot_material[
                "current_batch_ids_hash"
            ],
            "current_batch_ids": current_ids,
            "exclusion_policy_sha": snapshot_material[
                "exclusion_policy_sha"
            ],
            "expected_asset_ids_hash": snapshot_material[
                "expected_asset_ids_hash"
            ],
            "expected_asset_ids": expected_ids,
            "records": records,
        }

        capacity_registry = json.loads(
            (ROOT / "history/capacity-profiles-v1.json").read_text(
                encoding="utf-8"
            )
        )
        capacity = capacity_registry["profiles"]["fake-safe-24k-v1"]
        binding = capacity["provider_bindings"]["codex"]
        provider_capabilities = {
            "codex": {
                "provider": "codex",
                "capability_profile_hash": binding[
                    "capability_profile_hash"
                ],
                "model_identity": binding["model_identity"],
                "reasoning_identity": binding["reasoning_identity"],
                "model_default": binding["model_default"],
                "reasoning_default": binding["reasoning_default"],
                "executable": binding["executable"],
                "cli_revision": binding["cli_revision"],
            }
        }
        risk_policy = json.loads(
            (ROOT / "history/risk-policy-v1.json").read_text(
                encoding="utf-8"
            )
        )
        risk_slice_policy = {
            "schema_version": "history-risk-slice-policy-v1",
            "policy_version": "critical-semantic-slices-v1",
            "allowed_slices": [
                "cross_language",
                "lineage_revision",
                "low_overlap",
            ],
        }
        provider_pools = {
            stage: ["codex"]
            for stage in ("comparator", "map", "detail", "reduce")
        }
        preliminary = history_audit_plan._issue_test_runtime_authority(
            provider_pools_ordered=provider_pools,
            provider_capabilities=provider_capabilities,
            intent="duplicate_search",
            semantic_policy_profile_id="semantic-release-v1",
            matched_router_rule_ids=(),
            max_output_tokens=capacity["max_output_tokens"],
        )
        router_round = {
            "schema_version": "history-router-round-v1",
            "run_id": run_id,
            "batch_id": batch_id,
            "intent": "duplicate_search",
            "snapshot": {
                name: snapshot[name]
                for name in (
                    "snapshot_id",
                    "snapshot_hash",
                    "history_as_of_watermark",
                    "current_batch_id_namespace",
                    "current_batch_ids_hash",
                    "current_batch_ids",
                    "exclusion_policy_sha",
                    "expected_asset_ids_hash",
                    "expected_asset_ids",
                )
            },
            "candidates": [self.candidate],
            "semantic_policy_profile_id": "semantic-release-v1",
            "risk_policy_sha": canonical_sha(
                "history-risk-policy-v1", risk_policy
            ),
            "risk_slice_policy_sha": canonical_sha(
                "history-risk-slice-policy-v1", risk_slice_policy
            ),
            "budget_policy_sha": history_audit_plan.runtime_budget_policy_sha(
                preliminary["budget_policy"]
            ),
            "authority_scope": "test_fake",
        }
        normalized_round, _ = history_audit_store._router_validate_round_material(
            router_round
        )
        route_round_sha = history_audit_store._router_round_sha(
            normalized_round
        )
        router_sources = self._router_sources(
            candidate_id,
            run_id=run_id,
            batch_id=batch_id,
            snapshot=snapshot,
            route_round_sha=route_round_sha,
        )
        input_material = {
            "schema_version": TEST_INPUT_SCHEMA,
            "authority_scope": "test-only-shadow",
            "run_id": run_id,
            "batch_id": batch_id,
            "intent": "duplicate_search",
            "candidate": self.candidate,
            "snapshot": snapshot,
            "capacity_profile": capacity,
            "capacity_profile_sha256": canonical_sha(
                "history-audit-cli-test-capacity-v1", capacity
            ),
            "provider_pools_ordered": provider_pools,
            "provider_capabilities": provider_capabilities,
            "provider_capabilities_sha256": canonical_sha(
                "history-audit-cli-test-provider-capabilities-v1",
                provider_capabilities,
            ),
            "fake_executable": {
                "path": str(self.fake_executable),
                "sha256": sha256(wrapper),
                "protocol_revision": TEST_PROVIDER_PROTOCOL,
            },
            "risk_policy": risk_policy,
            "risk_policy_sha256": canonical_sha(
                "history-risk-policy-v1", risk_policy
            ),
            "risk_slice_policy": risk_slice_policy,
            "risk_slice_policy_sha256": canonical_sha(
                "history-risk-slice-policy-v1", risk_slice_policy
            ),
            "router_domain_sources": router_sources,
            "semantic_policy_profile_id": "semantic-release-v1",
        }
        self.input_bundle = self_hashed(
            "history-audit-cli-test-only-shadow-input-v1",
            input_material,
            "bundle_sha256",
        )
        self.input_path.write_bytes(canonical_bytes(self.input_bundle))

    def tearDown(self):
        self.temporary.cleanup()

    def _router_sources(
        self, candidate_id, *, run_id, batch_id, snapshot, route_round_sha
    ):
        identity = {
            "route_round_sha256": route_round_sha,
            "run_id": run_id,
            "batch_id": batch_id,
            "snapshot_id": snapshot["snapshot_id"],
            "snapshot_hash": snapshot["snapshot_hash"],
        }
        dependencies = {
            name: sha256(f"cli-p0-{name}".encode("utf-8"))
            for name in (
                "semantic_policy",
                "plan",
                "prompt",
                "schema",
                "ordered_provider_pools",
                "capacity",
                "provider",
                "fault",
                "replay",
                "fts",
                "metadata",
            )
        }
        qrels_hash = sha256(b"cli-p0-qrels")
        return {
            "selection": {
                "schema_version": "history-router-selection-source-v1",
                **identity,
                "selected_candidate_id": candidate_id,
                "candidate_ids": [candidate_id],
                "members": [
                    {
                        "candidate_id": candidate_id,
                        "selection_class": "finalist",
                        "channel_states": [
                            {"channel_id": "dense_core", "state": "complete"},
                            {"channel_id": "exact_lineage", "state": "complete"},
                            {"channel_id": "fts", "state": "complete"},
                        ],
                    }
                ],
            },
            "l1_observation": {
                "schema_version": "history-router-l1-source-v1",
                **identity,
                "candidate_ids": [candidate_id],
                "members": [
                    {
                        "candidate_id": candidate_id,
                        "observation_kind": "comparator",
                        "comparator_outcome": "distinct",
                        "coverage_state": "complete",
                        "comparator_receipt_sha256": sha256(
                            b"cli-p0-l1-comparator-receipt"
                        ),
                    }
                ],
            },
            "calibration": {
                "schema_version": "history-router-calibration-source-v1",
                **identity,
                "semantic_policy_profile_id": "semantic-release-v1",
                "qrels_hash": qrels_hash,
                "calibration_state": "unqualified",
            },
            "qualification": {
                "schema_version": "history-router-qualification-source-v1",
                **identity,
                "semantic_policy_profile_id": "semantic-release-v1",
                "qrels_hash": qrels_hash,
                "qualification_id": None,
                "lookup_state": "unavailable",
                "dependency_heads": dependencies,
            },
            "risk_assignment": {
                "schema_version": "history-router-risk-assignment-source-v1",
                **identity,
                "candidate_ids": [candidate_id],
                "members": [
                    {
                        "candidate_id": candidate_id,
                        "assigned_slice_ids": ["low_overlap"],
                    }
                ],
            },
            "dependency_heads": {
                "schema_version": "history-router-dependency-heads-source-v1",
                **identity,
                "heads": dependencies,
                "observed_index_profile_sha256": dependencies["fts"],
            },
            "permanent_request": {
                "schema_version": "history-router-permanent-request-source-v1",
                **identity,
                "candidate_ids": [candidate_id],
                "members": [
                    {
                        "candidate_id": candidate_id,
                        "request_state": "not_requested",
                        "request_id": None,
                    }
                ],
            },
        }

    def _environment(self):
        environment = os.environ.copy()
        environment["PATH"] = (
            str(self.bin) + os.pathsep + environment.get("PATH", "")
        )
        environment["REAL_PROVIDER_LAUNCH_LOG"] = str(self.real_launch_log)
        environment["HISTORY_AUDIT_FAKE_PROVIDER_LOG"] = str(self.fake_log)
        if self.fixture_mode is not None:
            environment["HISTORY_AUDIT_TEST_FIXTURE_MODE"] = self.fixture_mode
            environment["HISTORY_AUDIT_TEST_FIXTURE_CHILD_MARKER"] = str(
                self.fixture_child_marker
            )
        return environment

    def _run(self, *arguments):
        return subprocess.run(
            [sys.executable, str(CLI), *map(str, arguments)],
            cwd=ROOT,
            env=self._environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=16,
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
        self.assertEqual(self._canonical_stdout(completed)["status"], "ready")
        self.assertFalse(self.real_launch_log.exists())

    def _full_plan(self):
        completed = self._run(
            "plan",
            "--db",
            self.database,
            "--candidate",
            self.candidate_path,
            "--intent",
            "duplicate_search",
            "--output",
            self.plan_path,
            "--test-only-shadow-input",
            self.input_path,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        envelope = self._canonical_stdout(completed)
        self.assertEqual(self.plan_path.read_bytes(), completed.stdout)
        self.assertEqual(
            set(envelope),
            {
                "schema_version",
                "authority_scope",
                "production_authority",
                "test_only_shadow_input",
                "runtime_plan",
                "runtime_plan_sha256",
                "plan_envelope_sha256",
            },
        )
        self.assertEqual(envelope["schema_version"], TEST_PLAN_SCHEMA)
        self.assertEqual(envelope["authority_scope"], "test-only-shadow")
        self.assertIs(envelope["production_authority"], False)
        self.assertEqual(
            envelope["test_only_shadow_input"], self.input_bundle
        )
        runtime_plan = envelope["runtime_plan"]
        self.assertEqual(runtime_plan["schema_version"], "history-audit-plan-v2")
        self.assertEqual(runtime_plan["authority_scope"], "test-only-shadow")
        self.assertEqual(runtime_plan["run_id"], "run-cli-p0")
        self.assertEqual(runtime_plan["candidate"], self.candidate)
        self.assertEqual(runtime_plan["plan_sha"], envelope["runtime_plan_sha256"])
        material = dict(envelope)
        envelope_sha = material.pop("plan_envelope_sha256")
        self.assertEqual(
            envelope_sha,
            canonical_sha("history-audit-cli-test-only-plan-v1", material),
        )
        self.assertFalse(self.real_launch_log.exists())
        self.assertFalse(self.fake_log.exists())
        return envelope

    def _execute(
        self,
        command,
        *,
        fault_after_cas=False,
        plan_path=None,
        state_path=None,
        receipt_path=None,
        provider_path=None,
    ):
        arguments = [
            command,
            "--db",
            self.database,
            "--cas-root",
            self.cas_root,
            "--plan",
            plan_path or self.plan_path,
            "--state",
            state_path or self.state_path,
            "--receipt",
            receipt_path or self.receipt_path,
            "--test-only-provider-executable",
            provider_path or self.fake_executable,
        ]
        if fault_after_cas:
            arguments.append("--test-fault-after-cas")
        return self._run(*arguments)

    def _verify(self, *, plan_path=None, receipt_path=None):
        return self._run(
            "verify",
            "--db",
            self.database,
            "--cas-root",
            self.cas_root,
            "--plan",
            plan_path or self.plan_path,
            "--receipt",
            receipt_path or self.receipt_path,
        )

    def _successful_lifecycle(self):
        self._init()
        envelope = self._full_plan()
        completed = self._execute("run")
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        status = self._canonical_stdout(completed)
        self.assertEqual(status["schema_version"], "history-audit-execution-status-v2")
        self.assertEqual(status["command"], "run")
        self.assertEqual(status["status"], "closed")
        self.assertEqual(
            status["runtime_plan_sha256"], envelope["runtime_plan_sha256"]
        )
        self.assertEqual(
            status["plan_envelope_sha256"], envelope["plan_envelope_sha256"]
        )
        self.assertTrue(self.receipt_path.is_file())
        self.assertFalse(self.real_launch_log.exists())
        return envelope, status

    def _load_receipt(self):
        raw = self.receipt_path.read_bytes()
        receipt = json.loads(raw.decode("utf-8"))
        self.assertEqual(raw, canonical_bytes(receipt))
        self.assertEqual(set(receipt), RECEIPT_FIELDS)
        material = dict(receipt)
        receipt_sha = material.pop("minimum_receipt_sha")
        self.assertEqual(
            receipt_sha,
            canonical_sha("history-minimum-receipt-v2", material),
        )
        return receipt

    def _counts(self):
        tables = (
            "audit_l2_plans_v2",
            "audit_logical_tasks",
            "audit_task_attempts",
            "audit_task_settlements_v2",
            "audit_cas_objects",
            "audit_runtime_budget_reservations_v2",
            "audit_runtime_budget_settlements_v2",
            "audit_attempt_launch_facts_v2",
            "audit_attempt_cost_settlements_v2",
            "audit_candidate_route_facts_v2",
            "audit_receipts",
            "audit_receipt_issuances_v2",
            "audit_semantic_release_authorizations_v2",
        )
        with sqlite3.connect(self.database) as connection:
            return {
                table: connection.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0]
                for table in tables
            }

    def _assert_no_real_provider(self):
        self.assertFalse(
            self.real_launch_log.exists(),
            "a registered provider executable was launched",
        )

    def _replace_with_alternate_candidate_bundle(self):
        candidate_material = {
            "candidate_id": "stg-v2-" + sha256(b"p0-cli-candidate-alternate"),
            "raw_artifact_sha": sha256(b"p0-cli-candidate-alternate-raw"),
            "source_order": 1,
        }
        candidate = {
            **candidate_material,
            "candidate_hash": canonical_sha(
                "history-runtime-candidate-v2", candidate_material
            ),
        }
        self.candidate = candidate
        self.candidate_path.write_bytes(canonical_bytes(candidate))
        bundle = copy.deepcopy(self.input_bundle)
        bundle.pop("bundle_sha256")
        bundle["candidate"] = candidate
        snapshot = bundle["snapshot"]
        snapshot["current_batch_ids"] = [candidate["candidate_id"]]
        snapshot["current_batch_ids_hash"] = ordered_set_sha(
            "history-current-batch-ids-v2", snapshot["current_batch_ids"]
        )
        snapshot_material = {
            "run_id": bundle["run_id"],
            "batch_id": bundle["batch_id"],
            "history_as_of_watermark": snapshot["history_as_of_watermark"],
            "current_batch_id_namespace": snapshot[
                "current_batch_id_namespace"
            ],
            "current_batch_ids_hash": snapshot["current_batch_ids_hash"],
            "exclusion_policy_sha": snapshot["exclusion_policy_sha"],
            "expected_asset_ids_hash": snapshot["expected_asset_ids_hash"],
        }
        snapshot["snapshot_hash"] = canonical_sha(
            "history-snapshot-v2", snapshot_material
        )
        snapshot["snapshot_id"] = canonical_sha(
            "history-snapshot-id-v2",
            {
                "run_id": bundle["run_id"],
                "batch_id": bundle["batch_id"],
                "snapshot_hash": snapshot["snapshot_hash"],
            },
        )
        preliminary = history_audit_plan._issue_test_runtime_authority(
            provider_pools_ordered=bundle["provider_pools_ordered"],
            provider_capabilities=bundle["provider_capabilities"],
            intent=bundle["intent"],
            semantic_policy_profile_id=bundle[
                "semantic_policy_profile_id"
            ],
            matched_router_rule_ids=(),
            max_output_tokens=bundle["capacity_profile"]["max_output_tokens"],
        )
        router_round = {
            "schema_version": "history-router-round-v1",
            "run_id": bundle["run_id"],
            "batch_id": bundle["batch_id"],
            "intent": bundle["intent"],
            "snapshot": {
                name: snapshot[name]
                for name in (
                    "snapshot_id",
                    "snapshot_hash",
                    "history_as_of_watermark",
                    "current_batch_id_namespace",
                    "current_batch_ids_hash",
                    "current_batch_ids",
                    "exclusion_policy_sha",
                    "expected_asset_ids_hash",
                    "expected_asset_ids",
                )
            },
            "candidates": [candidate],
            "semantic_policy_profile_id": bundle[
                "semantic_policy_profile_id"
            ],
            "risk_policy_sha": bundle["risk_policy_sha256"],
            "risk_slice_policy_sha": bundle["risk_slice_policy_sha256"],
            "budget_policy_sha": history_audit_plan.runtime_budget_policy_sha(
                preliminary["budget_policy"]
            ),
            "authority_scope": "test_fake",
        }
        normalized, _ = history_audit_store._router_validate_round_material(
            router_round
        )
        bundle["router_domain_sources"] = self._router_sources(
            candidate["candidate_id"],
            run_id=bundle["run_id"],
            batch_id=bundle["batch_id"],
            snapshot=snapshot,
            route_round_sha=history_audit_store._router_round_sha(normalized),
        )
        self.input_bundle = self_hashed(
            "history-audit-cli-test-only-shadow-input-v1",
            bundle,
            "bundle_sha256",
        )
        self.input_path.write_bytes(canonical_bytes(self.input_bundle))

    def test_cli_argv_contract_exposes_explicit_test_only_lifecycle(self):
        expected = {
            "plan": {"--test-only-shadow-input"},
            "run": {
                "--db",
                "--cas-root",
                "--plan",
                "--state",
                "--receipt",
                "--test-only-provider-executable",
                "--test-fault-after-cas",
            },
            "resume": {
                "--db",
                "--cas-root",
                "--plan",
                "--state",
                "--receipt",
                "--test-only-provider-executable",
            },
            "verify": {"--db", "--cas-root", "--plan", "--receipt"},
        }
        for command, flags in expected.items():
            with self.subTest(command=command):
                completed = self._run(command, "--help")
                self.assertEqual(completed.returncode, 0, completed.stderr.decode())
                help_text = completed.stdout.decode("utf-8")
                for flag in flags:
                    self.assertIn(flag, help_text)
        self._assert_no_real_provider()

    def test_independent_processes_create_full_closed_shadow_lifecycle(self):
        envelope, status = self._successful_lifecycle()
        receipt = self._load_receipt()
        self.assertEqual(receipt["plan_hash"], envelope["runtime_plan_sha256"])
        self.assertEqual(receipt["final_status"], "uncertain")
        self.assertEqual(
            receipt["stage_reason_code"], "semantic_policy_unqualified"
        )
        self.assertTrue(receipt["coverage_complete"])
        self.assertTrue(receipt["adjudication_complete"])
        self.assertFalse(receipt["semantic_policy_qualified"])
        self.assertIsNone(receipt["no_match_basis"])
        self.assertEqual(status["receipt_sha256"], receipt["minimum_receipt_sha"])

        counts = self._counts()
        for table in (
            "audit_l2_plans_v2",
            "audit_logical_tasks",
            "audit_task_attempts",
            "audit_task_settlements_v2",
            "audit_runtime_budget_reservations_v2",
            "audit_runtime_budget_settlements_v2",
            "audit_attempt_launch_facts_v2",
            "audit_attempt_cost_settlements_v2",
            "audit_candidate_route_facts_v2",
            "audit_receipts",
            "audit_receipt_issuances_v2",
        ):
            self.assertGreater(counts[table], 0, table)
        self.assertGreaterEqual(counts["audit_cas_objects"], 2)
        self.assertEqual(
            counts["audit_semantic_release_authorizations_v2"], 0
        )

        verified = self._verify()
        self.assertEqual(verified.returncode, 0, verified.stderr.decode())
        result = self._canonical_stdout(verified)
        self.assertEqual(result["status"], "verified")
        self.assertTrue(result["execution_authorized"])
        self.assertFalse(result["production_authority"])
        self.assertFalse(result["current_release_authority"])
        self.assertEqual(
            result["receipt_sha256"], receipt["minimum_receipt_sha"]
        )
        self._assert_no_real_provider()

    def test_run_then_resume_recovers_across_processes_and_keeps_identity(self):
        self._init()
        envelope = self._full_plan()
        interrupted = self._execute("run", fault_after_cas=True)
        self.assertNotEqual(interrupted.returncode, 0)
        interrupted_status = self._canonical_stdout(interrupted)
        self.assertEqual(interrupted_status["status"], "interrupted")
        self.assertEqual(
            interrupted_status["reason_code"], "fault_after_cas"
        )
        state_before = self.state_path.read_bytes()
        state = json.loads(state_before.decode("utf-8"))
        self.assertEqual(
            state["runtime_plan_sha256"], envelope["runtime_plan_sha256"]
        )
        self.assertEqual(
            state["plan_envelope_sha256"], envelope["plan_envelope_sha256"]
        )
        self.assertFalse(self.receipt_path.exists())
        self.assertEqual(len(self.fake_log.read_text(encoding="utf-8").splitlines()), 1)

        tampered = json.loads(self.plan_path.read_text(encoding="utf-8"))
        tampered["runtime_plan"]["intent"] = "evolution_search"
        tampered_plan = self.root / "tampered-resume-plan.json"
        tampered_plan.write_bytes(canonical_bytes(tampered))
        rejected = self._execute("resume", plan_path=tampered_plan)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(self.state_path.read_bytes(), state_before)
        self.assertEqual(len(self.fake_log.read_text(encoding="utf-8").splitlines()), 1)

        resumed = self._execute("resume")
        self.assertEqual(resumed.returncode, 0, resumed.stderr.decode())
        resumed_status = self._canonical_stdout(resumed)
        self.assertEqual(resumed_status["status"], "closed")
        self.assertEqual(resumed_status["command"], "resume")
        self.assertEqual(
            resumed_status["runtime_plan_sha256"], envelope["runtime_plan_sha256"]
        )
        self.assertEqual(len(self.fake_log.read_text(encoding="utf-8").splitlines()), 1)
        receipt = self._load_receipt()

        replay = self._execute("resume")
        self.assertEqual(replay.returncode, 0, replay.stderr.decode())
        replay_status = self._canonical_stdout(replay)
        self.assertEqual(
            replay_status["receipt_sha256"], receipt["minimum_receipt_sha"]
        )
        self.assertEqual(len(self.fake_log.read_text(encoding="utf-8").splitlines()), 1)
        self._assert_no_real_provider()

    def test_verify_rejects_tampered_receipt_plan_and_cas(self):
        self._successful_lifecycle()
        receipt = self._load_receipt()

        tampered_receipt = dict(receipt)
        tampered_receipt["final_status"] = "complete_no_match"
        tampered_receipt_path = self.root / "tampered-receipt.json"
        tampered_receipt_path.write_bytes(canonical_bytes(tampered_receipt))
        rejected_receipt = self._verify(receipt_path=tampered_receipt_path)
        self.assertNotEqual(rejected_receipt.returncode, 0)

        tampered_plan = json.loads(self.plan_path.read_text(encoding="utf-8"))
        tampered_plan["runtime_plan"]["candidate"]["source_order"] = 1
        tampered_plan_path = self.root / "tampered-plan.json"
        tampered_plan_path.write_bytes(canonical_bytes(tampered_plan))
        rejected_plan = self._verify(plan_path=tampered_plan_path)
        self.assertNotEqual(rejected_plan.returncode, 0)

        with sqlite3.connect(self.database) as connection:
            relative_path = connection.execute(
                "SELECT relative_path FROM audit_cas_objects ORDER BY object_id LIMIT 1"
            ).fetchone()[0]
        (self.cas_root / relative_path).write_bytes(b"tampered compressed CAS")
        rejected_cas = self._verify()
        self.assertNotEqual(rejected_cas.returncode, 0)
        self._assert_no_real_provider()

    def test_real_unbudgetable_shadow_plan_starts_zero_hard_work(self):
        self._init()
        shadow_plan = self.root / "unbudgetable-shadow-plan.json"
        planned = self._run(
            "plan",
            "--db",
            self.database,
            "--candidate",
            self.candidate_path,
            "--intent",
            "duplicate_search",
            "--output",
            shadow_plan,
        )
        self.assertEqual(planned.returncode, 0, planned.stderr.decode())
        plan = self._canonical_stdout(planned)
        self.assertEqual(plan["status"], "producer_unavailable")
        self.assertEqual(plan["reason_code"], "unbudgetable_provider")
        self.assertFalse(plan["hard_complete_work_created"])

        attempted = self._run(
            "run", "--plan", shadow_plan, "--state", self.state_path
        )
        self.assertNotEqual(attempted.returncode, 0)
        result = self._canonical_stdout(attempted)
        self.assertEqual(result["status"], "plan_not_executable")
        self.assertFalse(result.get("production_no_match_authorized", False))
        counts = self._counts()
        for table in (
            "audit_l2_plans_v2",
            "audit_logical_tasks",
            "audit_task_attempts",
            "audit_cas_objects",
            "audit_runtime_budget_reservations_v2",
            "audit_attempt_launch_facts_v2",
            "audit_receipts",
            "audit_semantic_release_authorizations_v2",
        ):
            self.assertEqual(counts[table], 0, table)
        self.assertFalse(self.fake_log.exists())
        self._assert_no_real_provider()

    def test_test_only_input_is_closed_and_rejects_authority_outputs(self):
        self._init()
        self._full_plan()
        baseline = self._counts()
        attacks = {
            "router_facts": {"release_qualified": True},
            "final_status": "complete_no_match",
            "receipt": {"production_authority": True},
        }
        for field, value in attacks.items():
            with self.subTest(field=field):
                material = dict(self.input_bundle)
                material.pop("bundle_sha256")
                material[field] = value
                attack = self_hashed(
                    "history-audit-cli-test-only-shadow-input-v1",
                    material,
                    "bundle_sha256",
                )
                path = self.root / f"attack-{field}.json"
                output = self.root / f"attack-{field}-plan.json"
                path.write_bytes(canonical_bytes(attack))
                rejected = self._run(
                    "plan",
                    "--db",
                    self.database,
                    "--candidate",
                    self.candidate_path,
                    "--intent",
                    "duplicate_search",
                    "--output",
                    output,
                    "--test-only-shadow-input",
                    path,
                )
                self.assertNotEqual(rejected.returncode, 0)
                self.assertFalse(output.exists())
        counts = self._counts()
        for table in (
            "audit_l2_plans_v2",
            "audit_logical_tasks",
            "audit_task_attempts",
            "audit_cas_objects",
            "audit_receipts",
        ):
            self.assertEqual(counts[table], baseline[table], table)
        self.assertFalse(self.fake_log.exists())
        self._assert_no_real_provider()

    def test_test_provider_must_match_bound_regular_file_sha(self):
        self._init()
        self._full_plan()
        real_file = self.root / "offline-fake-provider.real"
        self.fake_executable.rename(real_file)
        self.fake_executable.symlink_to(real_file)
        symlinked = self._execute("run")
        self.assertNotEqual(symlinked.returncode, 0)
        self.assertFalse(self.fake_log.exists())
        self.assertEqual(self._counts()["audit_task_attempts"], 0)

        self.fake_executable.unlink()
        real_file.rename(self.fake_executable)
        self.fake_executable.write_bytes(
            self.fake_executable_bytes + b"# sha drift\n"
        )
        self.fake_executable.chmod(0o700)
        drifted = self._execute("run")
        self.assertNotEqual(drifted.returncode, 0)
        self.assertFalse(self.fake_log.exists())
        self.assertEqual(self._counts()["audit_task_attempts"], 0)

        self.fake_executable.write_bytes(self.fake_executable_bytes)
        self.fake_executable.chmod(0o700)
        completed = self._execute("run")
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(self._canonical_stdout(completed)["status"], "closed")
        self.assertEqual(len(self.fake_log.read_text(encoding="utf-8").splitlines()), 1)
        self._assert_no_real_provider()

    def test_test_provider_requires_the_repository_owned_fixture_bytes(self):
        self._init()
        attacker = self.root / "attacker-wrapper"
        attacker.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
        attacker.chmod(0o700)
        bundle = copy.deepcopy(self.input_bundle)
        bundle.pop("bundle_sha256")
        bundle["fake_executable"] = {
            "path": str(attacker),
            "sha256": sha256(attacker.read_bytes()),
            "protocol_revision": TEST_PROVIDER_PROTOCOL,
        }
        attack = self_hashed(
            "history-audit-cli-test-only-shadow-input-v1",
            bundle,
            "bundle_sha256",
        )
        attack_path = self.root / "attacker-input.json"
        attack_plan = self.root / "attacker-plan.json"
        attack_path.write_bytes(canonical_bytes(attack))
        rejected = self._run(
            "plan",
            "--db",
            self.database,
            "--candidate",
            self.candidate_path,
            "--intent",
            "duplicate_search",
            "--output",
            attack_plan,
            "--test-only-shadow-input",
            attack_path,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertFalse(attack_plan.exists())
        self._assert_no_real_provider()

    def test_test_provider_runs_an_exact_nonexecutable_fixture_copy_with_host_python(self):
        self._init()
        self._full_plan()
        self.fake_executable.chmod(0o600)
        completed = self._execute("run")
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(self._canonical_stdout(completed)["status"], "closed")
        self.assertTrue(self.fake_log.is_file())
        self._assert_no_real_provider()

    def test_rehashed_serialized_request_substitution_is_rejected_before_launch(self):
        self._init()
        envelope = self._full_plan()
        tampered = copy.deepcopy(envelope)
        shard = tampered["runtime_plan"]["shards"][0]
        shard["serialized_request"] = "{}"
        shard["request_sha256"] = sha256(b"{}")
        shard["final_request_tokens"] = 2
        tampered["runtime_plan"]["shard_plan_sha"] = (
            history_audit_plan.runtime_shard_plan_sha(
                tampered["runtime_plan"]["shards"]
            )
        )
        runtime_material = copy.deepcopy(tampered["runtime_plan"])
        runtime_material.pop("plan_sha")
        runtime_sha = history_audit_plan.runtime_plan_sha_from_material(
            runtime_material
        )
        tampered["runtime_plan"]["plan_sha"] = runtime_sha
        tampered["runtime_plan_sha256"] = runtime_sha
        tampered.pop("plan_envelope_sha256")
        tampered = self_hashed(
            "history-audit-cli-test-only-plan-v1",
            tampered,
            "plan_envelope_sha256",
        )
        tampered_path = self.root / "rehashed-substitution-plan.json"
        tampered_path.write_bytes(canonical_bytes(tampered))
        rejected = self._execute("run", plan_path=tampered_path)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertFalse(self.fake_log.exists())
        self.assertEqual(self._counts()["audit_task_attempts"], 0)
        self._assert_no_real_provider()

    def test_closed_resume_and_verify_reject_a_foreign_durable_receipt(self):
        self._init()
        envelope_a = self._full_plan()
        foreign = HistoryAuditCliP0LifecycleSmoke(
            "test_independent_processes_create_full_closed_shadow_lifecycle"
        )
        foreign.setUp()
        try:
            foreign._replace_with_alternate_candidate_bundle()
            envelope_b, _ = foreign._successful_lifecycle()
            self.assertNotEqual(
                envelope_a["runtime_plan_sha256"],
                envelope_b["runtime_plan_sha256"],
            )
            foreign_receipt = json.loads(
                foreign.receipt_path.read_text(encoding="utf-8")
            )
            forged_state = self_hashed(
                "history-audit-cli-execution-state-v1",
                {
                    "schema_version": "history-audit-cli-execution-state-v1",
                    "authority_scope": "test-only-shadow",
                    "runtime_plan_sha256": envelope_a["runtime_plan_sha256"],
                    "plan_envelope_sha256": envelope_a["plan_envelope_sha256"],
                    "status": "closed",
                    "receipt_sha256": foreign_receipt["minimum_receipt_sha"],
                },
                "state_sha256",
            )
            foreign.state_path.write_bytes(canonical_bytes(forged_state))
            resumed = foreign._execute(
                "resume",
                plan_path=self.plan_path,
                provider_path=self.fake_executable,
            )
            self.assertNotEqual(resumed.returncode, 0)
            verified = foreign._verify(plan_path=self.plan_path)
            self.assertNotEqual(verified.returncode, 0)
        finally:
            foreign.tearDown()
        self._assert_no_real_provider()

    def test_fixture_failure_is_nonzero_and_cannot_issue_a_receipt(self):
        self._init()
        self._full_plan()
        self.fixture_mode = "fail"
        failed = self._execute("run")
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse(self.receipt_path.exists())
        self._assert_no_real_provider()

    def test_output_bound_kills_the_fixture_process_group(self):
        self._init()
        self._full_plan()
        self.fixture_mode = "overflow-spawn-child"
        failed = self._execute("run")
        self.assertNotEqual(failed.returncode, 0)
        time.sleep(0.8)
        self.assertFalse(self.fixture_child_marker.exists())
        self.assertFalse(self.receipt_path.exists())
        self._assert_no_real_provider()

    def test_timeout_kills_the_fixture_process_group(self):
        self._init()
        self._full_plan()
        self.fixture_mode = "timeout-spawn-child"
        failed = self._execute("run")
        self.assertNotEqual(failed.returncode, 0)
        time.sleep(2.2)
        self.assertFalse(self.fixture_child_marker.exists())
        self.assertFalse(self.receipt_path.exists())
        self._assert_no_real_provider()

    def test_keyboard_interrupt_kills_the_fixture_process_group(self):
        self._init()
        self._full_plan()
        self.fixture_mode = "interrupt-spawn-child"
        try:
            failed = self._execute("run")
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn(b"KeyboardInterrupt", failed.stderr)
            self.assertNotIn(b"trusted test fixture I/O failed", failed.stderr)
            time.sleep(1.2)
            self.assertFalse(self.fixture_child_marker.exists())
            self.assertFalse(self.receipt_path.exists())
            self._assert_no_real_provider()
        finally:
            if self.fixture_pid_file.is_file():
                try:
                    os.killpg(
                        int(self.fixture_pid_file.read_text(encoding="utf-8")),
                        signal.SIGKILL,
                    )
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    unittest.main()
