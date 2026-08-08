#!/usr/bin/env python3
"""Offline contract tests for the bounded-history runtime cutover."""

import ast
import copy
import dataclasses
import hashlib
import inspect
import json
import pathlib
import shutil
import sys
import tempfile
import unittest
import os
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_projection
from lib import history_retrieval
from lib import history_runtime
from lib import history_store
from lib import history_archive
from lib import direction_contract
from lib import provider_adapters


POLICY_PATH = ROOT / "history" / "retrieval-policy-v1.json"
ROLE_PATH = ROOT / "roles" / "history-compare.md"
FAKE_STAGE_AGENT = ROOT / "tests" / "fake_stage_agent.py"
FAKE_PORTABLE_PROVIDER = (
    ROOT / "tests" / "fake_portable_stage_provider.py"
)
PROVIDER_REGISTRY = ROOT / "history" / "provider-adapters-v1.json"


def canonical(value):
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


class RuntimeFixture(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="history-runtime-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "tmp").mkdir()
        (self.root / ".ai-ideas").mkdir()
        (self.root / "ledger.instance-id").write_text(
            "runtime-smoke-instance\n",
            encoding="utf-8",
        )
        self.ledger = self.root / "ledger.tsv"
        self.ledger_good = self.root / "tmp" / "ledger.good"
        self.database = self.root / ".ai-ideas" / "history.sqlite3"
        self.policy_path = self.root / "retrieval-policy.json"
        self.policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        self.policy_path.write_bytes(canonical(self.policy))
        self.generation_policy_path = (
            self.root / "brainstorming_policy.md"
        )
        self.generation_policy_path.write_bytes(
            (ROOT / "brainstorming_policy.md").read_bytes()
        )
        self.review_contract_path = (
            self.root / "review-contract-v1.md"
        )
        self.review_contract_path.write_bytes(
            (
                ROOT / "history" / "review-contract-v1.md"
            ).read_bytes()
        )
        self.ledger.write_text(
            "date\tsource\ttheme\tidea\tverdict\treason\toverlap\tcategory\n"
            "2026-07-23\thunt\tEvaluation and Diagnostics\t"
            "Measure causal attribution under controlled interventions.\t"
            "accept-w-rev\tMissing a strong baseline.\tlow\tdesign-fixable\n",
            encoding="utf-8",
        )

    def connect_indexed(self):
        conn = history_store.connect(self.database)
        history_store.init_schema(conn)
        history_store.import_tsv_epoch(conn, self.ledger)
        history_projection.rebuild(conn, self.policy)
        return conn

    def shadow_test_authority(self):
        policy = history_projection.load_policy(self.policy_path)
        self.assertEqual(policy["mode"], "shadow")
        return history_runtime.validate_runtime_mode(policy)

    def startup(self, **overrides):
        values = {
            "db_path": self.database,
            "ledger_path": self.ledger,
            "ledger_good_path": self.ledger_good,
            "state_root": self.root / ".ai-ideas",
            "policy_path": self.policy_path,
            "brief_path": self.root / "tmp" / "round" / "generation_brief.json",
            "divergence_lens": "Name an unnamed estimand.",
        }
        values.update(overrides)
        return history_runtime.startup_runtime(**values)

    def frozen_candidate(self, candidate_id="I1", story=None):
        story = story or "A bounded candidate."
        candidate = {
            "candidate_id": candidate_id,
            "story": story,
            "theme": "Evaluation and Diagnostics",
            "candidate_markdown": (
                f"## {candidate_id}\n"
                f"One-Sentence Story: {story}\n"
                "Theme: Evaluation and Diagnostics\n"
            ),
        }
        candidate["content_sha256"] = (
            history_runtime.candidate_content_sha256(candidate)
        )
        return candidate


class StartupContract(RuntimeFixture):
    def test_absent_database_imports_once_and_publishes_both_targets(self):
        result = self.startup()
        self.assertTrue(result["imported"])
        self.assertEqual(result["policy_mode"], "shadow")
        self.assertTrue(result["brief_sha256"])
        self.assertEqual(self.ledger.read_bytes(), self.ledger_good.read_bytes())
        conn = history_store.connect(self.database)
        try:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM candidates").fetchone()[0],
                1,
            )
        finally:
            conn.close()
    def test_existing_database_ignores_conflicting_tsv_and_reconciles_now(self):
        first = self.startup()
        authoritative = self.ledger.read_bytes()
        self.ledger.write_text(
            "date\tsource\ttheme\tidea\tverdict\treason\toverlap\tcategory\n"
            "2099-01-01\thunt\tSafety and Robustness\tForged row.\treject\t"
            "conflict\thigh\tnovelty-dead\n",
            encoding="utf-8",
        )
        result = self.startup()
        self.assertFalse(result["imported"])
        self.assertEqual(result["source_watermark"], first["source_watermark"])
        self.assertEqual(self.ledger.read_bytes(), authoritative)
        self.assertEqual(self.ledger_good.read_bytes(), authoritative)

    def test_divergence_lens_is_bounded_and_bound_into_brief_hash(self):
        first = self.startup(divergence_lens="Lens one.")
        first_bytes = pathlib.Path(first["brief_path"]).read_bytes()
        second = self.startup(divergence_lens="Lens two.")
        second_bytes = pathlib.Path(second["brief_path"]).read_bytes()
        self.assertNotEqual(first["brief_sha256"], second["brief_sha256"])
        self.assertNotEqual(first_bytes, second_bytes)
        oversized = "x" * (history_runtime.DIVERGENCE_LENS_MAX_BYTES + 1)
        with self.assertRaises(history_runtime.RuntimeContractError):
            self.startup(divergence_lens=oversized)

    def test_startup_reclaims_live_projection_claim_under_instance_lock(self):
        startup = self.startup()
        conn = history_store.connect(self.database)
        try:
            history_store.append_rows(
                conn,
                [
                    "2026-07-24\thunt\tEvaluation and Diagnostics\t"
                    "Crash-recovery candidate.\treject\t"
                    "Bounded failure.\thigh\tnovelty-dead"
                ],
                {"run_id": "crashed-runtime"},
            )
            claim = history_store.claim_ledger_projection(conn)
            self.assertIsNotNone(claim)
            self.assertEqual(
                history_store.pending_ledger_projection_count(conn),
                1,
            )
        finally:
            conn.close()
        result = self.startup()
        self.assertFalse(result["imported"])
        conn = history_store.connect(self.database)
        try:
            self.assertEqual(
                history_store.pending_ledger_projection_count(conn),
                0,
            )
            self.assertEqual(
                self.ledger.read_bytes(),
                history_store.render_tsv(conn),
            )
            self.assertEqual(
                self.ledger_good.read_bytes(),
                history_store.render_tsv(conn),
            )
        finally:
            conn.close()

    def test_portable_stage_error_class_survives_runtime_boundary(self):
        from lib import portable_stage

        for code, expected_class in (
            ("final_output_missing", "contract"),
            ("timeout", "execution"),
        ):
            with self.subTest(code=code):
                with mock.patch.object(
                    portable_stage,
                    "public_descriptor",
                    side_effect=portable_stage.PortableStageError(code),
                ):
                    with self.assertRaises(
                        history_runtime.RuntimeContractError
                    ) as caught:
                        history_runtime._public_portable_stage({}, self.root)
                self.assertEqual(caught.exception.error_class, expected_class)
                self.assertIsNone(
                    history_runtime.RuntimeContractError("plain").error_class
                )

    def test_error_class_survives_pickle_round_trip(self):
        import pickle

        original = history_runtime.RuntimeContractError(
            "boom", error_class="contract"
        )
        restored = pickle.loads(pickle.dumps(original))
        self.assertEqual(restored.error_class, "contract")
        self.assertEqual(str(restored), "boom")
        calibration = pickle.loads(
            pickle.dumps(history_runtime.CalibrationError("cal"))
        )
        self.assertIsInstance(calibration, history_runtime.CalibrationError)
        self.assertIsNone(calibration.error_class)


class ArchiveContract(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(
            tempfile.mkdtemp(prefix="history-archive-")
        )
        self.addCleanup(shutil.rmtree, self.root, True)
        self.source = self.root / "round-source"
        (self.source / "history").mkdir(parents=True)
        (self.source / "history" / "batch.json").write_bytes(
            canonical({"schema_version": 1, "batch": "fixture"})
        )
        self.policy = self.root / "policy.json"
        policy_value = {"mode": "shadow", "schema_version": 1}
        self.policy.write_bytes(canonical(policy_value))
        self.startup = self.source / "history" / "startup.json"
        self.startup.write_bytes(
            canonical(
                {
                    "schema_version": 1,
                    "policy_sha256": hashlib.sha256(
                        canonical(policy_value)
                    ).hexdigest(),
                    "capability_sha256": None,
                    "trust_root_sha256": None,
                }
            )
        )
        self.projection = (
            self.source / "history" / "materialize-ledger.json"
        )
        self.projection.write_bytes(
            canonical(
                {
                    "schema_version": 1,
                    "sha256": "1" * 64,
                }
            )
        )
        self.state = self.root / "state"
        receipts = self.state / "ledger-target-receipts"
        receipts.mkdir(parents=True)
        for name in (
            "ledger.tsv.json",
            "tmp__ledger.good.json",
        ):
            (receipts / name).write_bytes(
                canonical(
                    {
                        "schema_version": 1,
                        "target": name,
                        "sha256": "2" * 64,
                    }
                )
            )
        self.destination = self.root / "archive"
        self.values = {
            "source_root": self.source,
            "destination": self.destination,
            "run_id": "run-1",
            "round_number": 1,
            "date": "2026-07-24",
            "policy_mode": "shadow",
            "reason": "decision",
            "policy_path": self.policy,
            "startup_path": self.startup,
            "state_root": self.state,
            "projection_path": self.projection,
        }

    def test_partial_copy_never_publishes_and_retry_is_complete(
        self,
    ):
        original = history_archive._copy_regular
        calls = {"count": 0}

        def interrupted(source, destination, label):
            calls["count"] += 1
            if calls["count"] == 2:
                raise history_archive.ArchiveError(
                    "injected partial copy"
                )
            return original(source, destination, label)

        with mock.patch.object(
            history_archive,
            "_copy_regular",
            side_effect=interrupted,
        ):
            with self.assertRaises(history_archive.ArchiveError):
                history_archive.archive_round(**self.values)
        self.assertFalse((self.destination / "round").exists())
        stale = self.destination / ".round.tmp.interrupted"
        stale.mkdir()
        (stale / "partial").write_bytes(b"partial")
        receipt = history_archive.archive_round(**self.values)
        self.assertEqual(receipt["run_id"], "run-1")
        self.assertTrue(
            history_archive.verify_archive(
                self.destination / "round",
                run_id="run-1",
                round_number=1,
            )
        )
        self.assertTrue(stale.is_dir())
        repeated = history_archive.archive_round(**self.values)
        self.assertEqual(repeated, receipt)

    def test_existing_partial_round_is_never_trusted(self):
        partial = self.destination / "round"
        partial.mkdir(parents=True)
        (partial / "batch.json").write_bytes(b"partial\n")
        with self.assertRaises(history_archive.ArchiveError):
            history_archive.archive_round(**self.values)
        self.assertFalse(
            (self.destination / "manifest.tsv").exists()
        )

    def test_direction_rejection_is_not_a_failure_or_decision_archive(self):
        receipt = history_archive.archive_round(
            **dict(self.values, reason="rejected:direction")
        )
        self.assertEqual(receipt["archive_class"], "rejection")
        verified = history_archive.verify_archive(
            self.destination / "round",
            run_id="run-1",
            round_number=1,
            reason="rejected:direction",
        )
        self.assertEqual(
            verified["created_reason"],
            "rejected:direction",
        )
        with self.assertRaises(history_archive.ArchiveError):
            history_archive.verified_failure_archive_binding(
                self.destination,
                expected_run_id="run-1",
            )

    def test_direction_rejection_receipt_cannot_change_reason(self):
        history_archive.archive_round(
            **dict(self.values, reason="rejected:direction")
        )
        receipt_path = (
            self.destination
            / "round/history/archive-receipt.json"
        )
        receipt = json.loads(
            receipt_path.read_text(encoding="utf-8")
        )
        receipt["created_reason"] = "rejected:other"
        receipt_path.chmod(0o600)
        receipt_path.write_bytes(canonical(receipt))
        with self.assertRaises(history_archive.ArchiveError):
            history_archive.verify_archive(
                self.destination / "round",
                run_id="run-1",
                round_number=1,
            )

    def test_archive_verifier_rejects_tree_and_attempt_drift(
        self,
    ):
        history_archive.archive_round(**self.values)
        archived = self.destination / "round"
        batch = archived / "history" / "batch.json"
        batch_raw = batch.read_bytes()
        batch.unlink()
        with self.assertRaises(history_archive.ArchiveError):
            history_archive.verify_archive(
                archived,
                run_id="run-1",
                round_number=1,
            )
        batch.write_bytes(batch_raw)
        extra = archived / "extra.json"
        extra.write_bytes(b"{}\n")
        with self.assertRaises(history_archive.ArchiveError):
            history_archive.verify_archive(
                archived,
                run_id="run-1",
                round_number=1,
            )
        extra.unlink()
        alias = archived / "alias.json"
        alias.symlink_to(batch)
        with self.assertRaises(history_archive.ArchiveError):
            history_archive.verify_archive(
                archived,
                run_id="run-1",
                round_number=1,
            )
        alias.unlink()
        source_batch = self.source / "history" / "batch.json"
        source_batch.write_bytes(
            canonical(
                {
                    "schema_version": 1,
                    "batch": "different-attempt",
                }
            )
        )
        with self.assertRaises(history_archive.ArchiveError):
            history_archive.archive_round(**self.values)

    def test_archive_stages_public_authority_without_trust_root(
        self,
    ):
        capability = self.root / "capability.json"
        capability_raw = canonical(
            {
                "schema_version": 1,
                "scope": "synthetic-fixture",
            }
        )
        capability.write_bytes(capability_raw)
        startup = json.loads(
            self.startup.read_text(encoding="utf-8")
        )
        startup["capability_sha256"] = hashlib.sha256(
            capability_raw
        ).hexdigest()
        startup["trust_root_sha256"] = "3" * 64
        self.startup.write_bytes(canonical(startup))
        private_root = self.root / "private-trust-root.json"
        sentinel = b"PRIVATE-TRUST-ROOT-SENTINEL\n"
        private_root.write_bytes(sentinel)
        values = dict(
            self.values,
            capability_path=capability,
        )
        history_archive.archive_round(**values)
        archived = self.destination / "round"
        authority = (
            archived
            / "history"
            / "archive-authority"
        )
        self.assertTrue(
            (authority / "retrieval-policy.json").is_file()
        )
        self.assertTrue(
            (authority / "calibration-capability.json").is_file()
        )
        self.assertFalse(
            (authority / "private-trust-root.json").exists()
        )
        for path in archived.rglob("*"):
            if path.is_file():
                self.assertNotIn(sentinel, path.read_bytes())
        capability.write_bytes(
            canonical(
                {
                    "schema_version": 1,
                    "scope": "changed-fixture",
                }
            )
        )
        with self.assertRaises(history_archive.ArchiveError):
            history_archive.archive_round(**values)

    def test_archive_reuse_rejects_lifecycle_mode_and_authority_drift(
        self,
    ):
        failure_values = dict(
            self.values,
            reason="failed:review",
            projection_path=None,
        )
        history_archive.archive_round(**failure_values)
        with self.assertRaises(history_archive.ArchiveError):
            history_archive.archive_round(**self.values)

        other = self.root / "decision-archive"
        decision_values = dict(self.values, destination=other)
        history_archive.archive_round(**decision_values)
        with self.assertRaises(history_archive.ArchiveError):
            history_archive.archive_round(
                **dict(
                    decision_values,
                    policy_mode="enforcement",
                )
            )
        policy_raw = self.policy.read_bytes()
        self.policy.write_bytes(
            canonical({"mode": "enforcement", "schema_version": 1})
        )
        with self.assertRaises(history_archive.ArchiveError):
            history_archive.archive_round(**decision_values)
        self.policy.write_bytes(policy_raw)
        startup_raw = self.startup.read_bytes()
        startup = json.loads(startup_raw.decode("utf-8"))
        startup["trust_root_sha256"] = "4" * 64
        self.startup.write_bytes(canonical(startup))
        with self.assertRaises(history_archive.ArchiveError):
            history_archive.archive_round(**decision_values)
        self.startup.write_bytes(startup_raw)
        target = (
            self.state
            / "ledger-target-receipts"
            / "ledger.tsv.json"
        )
        target_raw = target.read_bytes()
        changed = json.loads(target_raw.decode("utf-8"))
        changed["sha256"] = "5" * 64
        target.write_bytes(canonical(changed))
        with self.assertRaises(history_archive.ArchiveError):
            history_archive.archive_round(**decision_values)
        target.write_bytes(target_raw)
        history_archive.archive_round(
            **dict(decision_values, reason="published")
        )

    def test_archive_rejects_destination_and_lock_symlinks(self):
        real_destination = self.root / "real-destination"
        real_destination.mkdir()
        alias = self.root / "destination-alias"
        alias.symlink_to(
            real_destination, target_is_directory=True
        )
        with self.assertRaises(history_archive.ArchiveError):
            history_archive.archive_round(
                **dict(self.values, destination=alias)
            )
        destination = self.root / "lock-alias-archive"
        destination.mkdir()
        lock_target = self.root / "external-lock"
        lock_target.write_bytes(b"")
        (destination / "archive.lock").symlink_to(lock_target)
        with self.assertRaises(history_archive.ArchiveError):
            history_archive.archive_round(
                **dict(self.values, destination=destination)
            )

    def test_prior_failure_binding_verifies_receipt_and_tree(self):
        receipt = history_archive.archive_round(
            **dict(
                self.values,
                reason="failed:review",
                projection_path=None,
            )
        )
        receipt_path = (
            self.destination
            / "round/history/archive-receipt.json"
        )
        binding = (
            history_archive.verified_failure_archive_binding(
                self.destination,
                expected_run_id="run-1",
            )
        )
        self.assertEqual(
            binding,
            {
                "run_id": "run-1",
                "round": 1,
                "created_reason": "failed:review",
                "archive_receipt_sha256": hashlib.sha256(
                    receipt_path.read_bytes()
                ).hexdigest(),
                "archive_tree_sha256": receipt["tree_sha256"],
            },
        )
        archived_batch = (
            self.destination / "round/history/batch.json"
        )
        archived_batch.unlink()
        archived_batch.write_bytes(b"changed\n")
        with self.assertRaises(history_archive.ArchiveError):
            history_archive.verified_failure_archive_binding(
                self.destination,
                expected_run_id="run-1",
            )


class CapabilityContract(RuntimeFixture):
    def _signed_capability(self):
        policy = copy.deepcopy(self.policy)
        policy["mode"] = "enforcement"
        self.policy_path.write_bytes(canonical(policy))
        root = {
            "schema_version": 1,
            "scope": "synthetic_contract_only",
            "trust_root_id": "runtime-smoke-root",
            "algorithm": "test-hmac-sha256",
            "hmac_sha256_key": "11" * 32,
        }
        commitment = history_runtime.synthetic_policy_commitment(
            policy
        )
        receipt = history_runtime.seal_test_preheldout_receipt(
            {
                "schema_version": 1,
                "scope": "synthetic_contract_only",
                "trust_root_id": root["trust_root_id"],
                "policy_commitment_sha256": history_runtime.sha256(
                    canonical(commitment)
                ),
                "split_sha256": commitment["split_sha256"],
                "trusted_runner_release_sha256": "17" * 32,
                "run_nonce": 7,
                "witness_time": "2026-07-24T00:00:00Z",
            },
            root,
        )
        capability = history_runtime.seal_test_calibration_capability(
            history_runtime.synthetic_calibration_capability_body(
                policy=policy,
                trust_root_id=root["trust_root_id"],
                commitment=commitment,
                receipt=receipt,
            ),
            root,
        )
        bundle = {
            "schema_version": 1,
            "policy_commitment": commitment,
            "preheldout_receipt": receipt,
            "calibration_capability": capability,
        }
        return policy, root, bundle

    def test_enforcement_rejects_missing_capability_before_stage_launch(self):
        policy = copy.deepcopy(self.policy)
        policy["mode"] = "enforcement"
        self.policy_path.write_bytes(canonical(policy))
        with mock.patch.object(
            history_store,
            "connect",
            side_effect=AssertionError(
                "storage opened before authority validation"
            ),
        ) as connect:
            with self.assertRaises(history_runtime.CalibrationError):
                self.startup()
        connect.assert_not_called()

    def test_signed_test_capability_is_valid_only_in_test_scope(self):
        policy, root, capability = self._signed_capability()
        accepted = history_runtime._validate_runtime_mode_for_test(
            policy,
            capability=capability,
            trust_root=root,
        )
        self.assertEqual(accepted["capability_sha256"], history_runtime.sha256(
            canonical(capability)
        ))
        with self.assertRaises(history_runtime.CalibrationError):
            history_runtime.validate_runtime_mode(
                policy,
                capability=capability,
                trust_root=root,
            )

    def test_capability_signature_and_every_bound_dimension_are_checked(self):
        policy, root, capability = self._signed_capability()
        mutations = (
            ("capability", "signature", "0" * 64),
            ("capability", "policy_version", "other-policy"),
            ("capability", "policy_sha256", "0" * 64),
            ("capability", "benchmark_snapshot_sha256", "0" * 64),
            ("capability", "preheldout_receipt_sha256", "0" * 64),
            ("capability", "heldout_output_sha256", "0" * 64),
            ("capability", "heldout_run_nonce", 8),
            ("receipt", "run_nonce", 8),
            ("receipt", "split_sha256", "0" * 64),
            (
                "receipt",
                "policy_commitment_sha256",
                "0" * 64,
            ),
            (
                "receipt",
                "trusted_runner_release_sha256",
                "0" * 64,
            ),
            ("receipt", "witness_time", "2026-07-24T00:00:02Z"),
            ("commitment", "split_sha256", "0" * 64),
            (
                "commitment",
                "calibration_query_ids_sha256",
                "0" * 64,
            ),
            (
                "commitment",
                "heldout_query_ids_sha256",
                "0" * 64,
            ),
        )
        for owner, field, replacement in mutations:
            mutated = copy.deepcopy(capability)
            target = {
                "capability": mutated["calibration_capability"],
                "receipt": mutated["preheldout_receipt"],
                "commitment": mutated["policy_commitment"],
            }[owner]
            target[field] = replacement
            with self.subTest(owner=owner, field=field):
                with self.assertRaises(history_runtime.CalibrationError):
                    history_runtime._validate_runtime_mode_for_test(
                        policy,
                        capability=mutated,
                        trust_root=root,
                    )

    def test_schema_versions_reject_json_booleans(self):
        policy, root, bundle = self._signed_capability()
        commitment = bundle["policy_commitment"]
        receipt = bundle["preheldout_receipt"]

        invalid_root = dict(root, schema_version=True)
        unsigned_receipt = dict(receipt)
        unsigned_receipt.pop("signature")
        with self.assertRaises(history_runtime.CalibrationError):
            history_runtime.seal_test_preheldout_receipt(
                unsigned_receipt,
                invalid_root,
            )

        invalid_commitment = dict(
            commitment,
            schema_version=True,
        )
        with self.assertRaises(history_runtime.CalibrationError):
            history_runtime._validate_commitment(
                invalid_commitment,
                policy,
            )

        invalid_receipt = dict(receipt)
        invalid_receipt.pop("signature")
        invalid_receipt["schema_version"] = True
        with self.assertRaises(history_runtime.CalibrationError):
            history_runtime.seal_test_preheldout_receipt(
                invalid_receipt,
                root,
            )

        invalid_capability = dict(
            bundle["calibration_capability"]
        )
        invalid_capability.pop("canonical_seal_sha256")
        invalid_capability.pop("signature")
        invalid_capability["schema_version"] = True
        with self.assertRaises(history_runtime.CalibrationError):
            history_runtime.seal_test_calibration_capability(
                invalid_capability,
                root,
            )

        invalid_bundle = dict(bundle, schema_version=True)
        with self.assertRaises(history_runtime.CalibrationError):
            history_runtime._validate_runtime_mode_for_test(
                policy,
                capability=invalid_bundle,
                trust_root=root,
            )


class CandidateAndObservationContract(RuntimeFixture):
    def setUp(self):
        super().setUp()
        generated = self.root / "batch-v2-generated"
        generated.mkdir()
        self.ideas_tsv = generated / "ideas.all.tsv"
        self.ideas_md = generated / "ideas.all.md"
        self.ideas_tsv.write_text(
            "I1\tA bounded candidate.\tEvaluation and Diagnostics\n",
            encoding="utf-8",
        )
        self.ideas_md.write_text(
            "## I1\n"
            "One-Sentence Story: A bounded candidate.\n"
            "Theme: Evaluation and Diagnostics\n",
            encoding="utf-8",
        )
        self.direction_contract = json.loads(
            (
                ROOT
                / "directions"
                / "dynamic-spatial-memory-vla-v1.json"
            ).read_text(encoding="utf-8")
        )

    def directed_batch(self):
        return history_runtime.freeze_candidate_batch(
            self.ideas_tsv,
            self.ideas_md,
            self.root / "directed-batch-helper",
            direction_contract=self.direction_contract,
        )

    def schema_v1_batch_fixture(self):
        manifest = history_runtime.freeze_candidate_batch(
            self.ideas_tsv,
            self.ideas_md,
            self.root / "schema-v1-batch-fixture",
        )
        material = dict(manifest)
        material.pop("batch_sha256")
        material.pop("direction")
        material["schema_version"] = 1
        manifest = dict(material)
        manifest["batch_sha256"] = history_runtime.sha256(
            b"history-runtime-batch-v1\0"
            + history_runtime.canonical_bytes(material)
        )
        batch_path = (
            pathlib.Path(manifest["artifact_root"]) / "batch.json"
        )
        batch_path.chmod(0o600)
        batch_path.write_bytes(canonical(manifest))
        return manifest

    def test_directed_batch_v2_binds_canonical_direction_identity(self):
        result = history_runtime.freeze_candidate_batch(
            self.ideas_tsv,
            self.ideas_md,
            self.root / "directed-batch",
            direction_contract=self.direction_contract,
        )
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(
            result["direction"],
            {
                "direction_id": "dynamic-spatial-memory-vla-v1",
                "sha256": (
                    "50bbf68a8ee20f2635194abab2a41ee702d4ec227b5277"
                    "bf1bba9f463fee0d85"
                ),
            },
        )
        history_runtime.verify_frozen_batch(result)

    def test_freeze_rejects_direction_drift_before_publishing_batch(self):
        expected = direction_contract.parse_contract_bytes(
            direction_contract.canonical_bytes(self.direction_contract)
        )[2]
        drifted = copy.deepcopy(self.direction_contract)
        drifted["statement"] += " Drifted after startup."
        output = self.root / "drifted-direction-batch"
        with self.assertRaisesRegex(
            history_runtime.RuntimeContractError,
            "direction identity changed",
        ):
            history_runtime.freeze_candidate_batch(
                self.ideas_tsv,
                self.ideas_md,
                output,
                direction_contract=drifted,
                expected_direction=expected,
            )
        self.assertFalse(output.exists())
        self.assertFalse((self.root / "observations").exists())

    def test_selector_copy_binds_verified_round_bytes_to_startup_and_batch(self):
        identity = direction_contract.parse_contract_bytes(
            direction_contract.canonical_bytes(self.direction_contract)
        )[2]
        batch = history_runtime.freeze_candidate_batch(
            self.ideas_tsv,
            self.ideas_md,
            self.root / "selector-copy-batch",
            direction_contract=self.direction_contract,
            expected_direction=identity,
        )
        round_contract = self.root / "round-direction.json"
        round_contract.write_bytes(
            direction_contract.canonical_bytes(self.direction_contract)
        )
        round_identity = self.root / "round-direction-identity.json"
        round_identity.write_bytes(canonical(identity))
        delivered = self.root / "selector" / "direction-constraint.json"
        history_runtime.copy_verified_direction_contract(
            contract_path=round_contract,
            round_identity_path=round_identity,
            expected_direction=identity,
            batch_path=pathlib.Path(batch["artifact_root"]) / "batch.json",
            output_path=delivered,
        )
        self.assertEqual(delivered.read_bytes(), round_contract.read_bytes())

        drifted = copy.deepcopy(self.direction_contract)
        drifted["statement"] += " Selector drift."
        round_contract.write_bytes(direction_contract.canonical_bytes(drifted))
        rejected = self.root / "selector-drift" / "direction-constraint.json"
        with self.assertRaisesRegex(
            history_runtime.RuntimeContractError,
            "direction identity changed",
        ):
            history_runtime.copy_verified_direction_contract(
                contract_path=round_contract,
                round_identity_path=round_identity,
                expected_direction=identity,
                batch_path=pathlib.Path(batch["artifact_root"]) / "batch.json",
                output_path=rejected,
            )
        self.assertFalse(rejected.exists())

    def test_direction_gate_rejects_gate_contract_drift_before_receipt(self):
        identity = direction_contract.parse_contract_bytes(
            direction_contract.canonical_bytes(self.direction_contract)
        )[2]
        batch = history_runtime.freeze_candidate_batch(
            self.ideas_tsv,
            self.ideas_md,
            self.root / "gate-drift-batch",
            direction_contract=self.direction_contract,
            expected_direction=identity,
        )
        gate_contract = self.root / "gate-direction.json"
        drifted = copy.deepcopy(self.direction_contract)
        drifted["statement"] += " Gate drift."
        gate_contract.write_bytes(direction_contract.canonical_bytes(drifted))
        verdicts = self.root / "gate-direction.tsv"
        verdicts.write_text(
            "id\tdirection-fit\tdirection-evidence\n"
            "I1\tin-scope\tThe candidate tests corrected memory.\n",
            encoding="utf-8",
        )
        receipt = self.root / "direction-gate.json"
        with self.assertRaisesRegex(
            history_runtime.RuntimeContractError,
            "direction identity changed",
        ):
            history_runtime.validate_direction_gate(
                contract_path=gate_contract,
                expected_direction=identity,
                batch_path=pathlib.Path(batch["artifact_root"]) / "batch.json",
                verdicts_path=verdicts,
                output_path=receipt,
            )
        self.assertFalse(receipt.exists())
        self.assertFalse((self.root / "observations").exists())

    def test_new_undirected_batch_v2_records_null_direction(self):
        result = history_runtime.freeze_candidate_batch(
            self.ideas_tsv,
            self.ideas_md,
            self.root / "undirected-batch",
        )
        self.assertEqual(result["schema_version"], 2)
        self.assertIsNone(result["direction"])

    def test_schema_v1_batch_remains_verifiable_as_undirected(self):
        manifest = self.schema_v1_batch_fixture()
        self.assertTrue(history_runtime.verify_frozen_batch(manifest))
        self.assertIsNone(
            history_runtime.frozen_batch_direction(manifest)
        )

    def test_batch_direction_tamper_breaks_v2_hash(self):
        manifest = self.directed_batch()
        manifest["direction"]["direction_id"] = "changed"
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.verify_frozen_batch(manifest)

    def test_malformed_direction_identity_fails_after_rehash(self):
        manifest = self.directed_batch()
        manifest["direction"] = {
            "direction_id": "dynamic-spatial-memory-vla-v1",
            "sha256": "0" * 63,
        }
        material = dict(manifest)
        material.pop("batch_sha256")
        manifest["batch_sha256"] = history_runtime.sha256(
            b"history-runtime-batch-v2\0"
            + history_runtime.canonical_bytes(material)
        )
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.verify_frozen_batch(manifest)

    def test_candidate_batch_is_frozen_from_exact_generated_bytes(self):
        generated = self.root / "generated"
        generated.mkdir()
        ideas_tsv = generated / "ideas.all.tsv"
        ideas_md = generated / "ideas.all.md"
        ideas_tsv.write_text(
            "I1\tA bounded candidate.\tEvaluation and Diagnostics\n",
            encoding="utf-8",
        )
        ideas_md.write_text(
            "## I1\n"
            "One-Sentence Story: A bounded candidate.\n"
            "Theme: Evaluation and Diagnostics\n"
            "Summary: Measure one bounded estimand.\n",
            encoding="utf-8",
        )
        frozen = history_runtime.freeze_candidate_batch(
            ideas_tsv,
            ideas_md,
            self.root / "host-only" / "candidates",
        )
        self.assertEqual(frozen["candidate_count"], 1)
        candidate = json.loads(
            (self.root / "host-only" / "candidates" / "I1.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(candidate["candidate_id"], "I1")
        self.assertEqual(
            candidate["content_sha256"],
            history_runtime.candidate_content_sha256(candidate),
        )
        ideas_tsv.unlink()
        ideas_md.write_text("## I1\nforged\n", encoding="utf-8")
        history_runtime.verify_frozen_batch(frozen)
        pathlib.Path(
            frozen["ideas_markdown"]["path"]
        ).write_text("## I1\nforged\n", encoding="utf-8")
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.verify_frozen_batch(frozen)

    def test_candidate_freeze_rejects_aliases_and_republication(self):
        tsv = self.root / "alias-ideas.tsv"
        markdown = self.root / "alias-ideas.md"
        tsv.write_text(
            "I1\tA bounded candidate.\tEvaluation and Diagnostics\n",
            encoding="utf-8",
        )
        markdown.write_text(
            "## I1\n"
            "One-Sentence Story: A bounded candidate.\n"
            "Theme: Evaluation and Diagnostics\n",
            encoding="utf-8",
        )
        symlink = self.root / "ideas-link.tsv"
        symlink.symlink_to(tsv)
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.freeze_candidate_batch(
                symlink,
                markdown,
                self.root / "symlink-freeze",
            )
        hardlink = self.root / "ideas-hardlink.tsv"
        os.link(tsv, hardlink)
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.freeze_candidate_batch(
                hardlink,
                markdown,
                self.root / "hardlink-freeze",
            )
        hardlink.unlink()
        frozen_root = self.root / "single-use-freeze"
        history_runtime.freeze_candidate_batch(
            tsv, markdown, frozen_root
        )
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.freeze_candidate_batch(
                tsv, markdown, frozen_root
            )

    def test_candidate_freeze_rejects_duplicate_markdown_heading(self):
        tsv = self.root / "duplicate-heading.tsv"
        markdown = self.root / "duplicate-heading.md"
        tsv.write_text(
            "I1\tA bounded candidate.\tEvaluation and Diagnostics\n",
            encoding="utf-8",
        )
        markdown.write_text(
            "## I1\n"
            "One-Sentence Story: Discarded duplicate block.\n"
            "Theme: Evaluation and Diagnostics\n\n"
            "## I1\n"
            "One-Sentence Story: A bounded candidate.\n"
            "Theme: Evaluation and Diagnostics\n",
            encoding="utf-8",
        )
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.freeze_candidate_batch(
                tsv,
                markdown,
                self.root / "duplicate-heading-freeze",
            )

    def test_candidate_freeze_reprojects_tsv_identity_from_markdown(self):
        """Markdown is canonical; dual-write drift is projected away."""
        for field, markdown_value in (
            ("story", "A contradictory Markdown story."),
            ("theme", "Safety and Robustness"),
        ):
            with self.subTest(field=field):
                tsv = self.root / f"{field}-binding.tsv"
                markdown = self.root / f"{field}-binding.md"
                out = self.root / f"{field}-binding-freeze"
                tsv.write_text(
                    "I1\tA bounded candidate.\t"
                    "Evaluation and Diagnostics\n",
                    encoding="utf-8",
                )
                story = (
                    markdown_value
                    if field == "story"
                    else "A bounded candidate."
                )
                theme = (
                    markdown_value
                    if field == "theme"
                    else "Evaluation and Diagnostics"
                )
                markdown.write_text(
                    "## I1\n"
                    f"One-Sentence Story: {story}\n"
                    f"Theme: {theme}\n",
                    encoding="utf-8",
                )
                history_runtime.freeze_candidate_batch(
                    tsv, markdown, out
                )
                frozen = (out / "sources" / "ideas.tsv").read_text(
                    encoding="utf-8"
                )
                self.assertEqual(
                    frozen, f"I1\t{story}\t{theme}\n"
                )
                candidate = json.loads(
                    (out / "I1.json").read_text(encoding="utf-8")
                )
                self.assertEqual(candidate["story"], story)
                self.assertEqual(candidate["theme"], theme)

    def test_duplicate_and_failure_are_mandatory_and_evolution_is_declared(self):
        candidate = {
            "candidate_id": "I1",
            "story": "A bounded candidate.",
            "theme": "Evaluation and Diagnostics",
            "candidate_markdown": "## I1\nA bounded candidate.\n",
            "declared_parent_candidate_id": "parent-1",
        }
        self.assertEqual(
            history_runtime.required_intents(candidate),
            [
                "duplicate_search",
                "evolution_search",
                "failure_pattern_search",
            ],
        )
        candidate.pop("declared_parent_candidate_id")
        self.assertEqual(
            history_runtime.required_intents(candidate),
            ["duplicate_search", "failure_pattern_search"],
        )

    def test_declared_parent_must_equal_the_validated_brief_parent(self):
        generated = self.root / "parent-generated"
        generated.mkdir()
        tsv = generated / "ideas.all.tsv"
        markdown = generated / "ideas.all.md"
        tsv.write_text(
            "I1\tA bounded revision.\tEvaluation and Diagnostics\n",
            encoding="utf-8",
        )
        markdown.write_text(
            "## I1\n"
            "One-Sentence Story: A bounded revision.\n"
            "Theme: Evaluation and Diagnostics\n"
            "Evolved from: canonical-parent\n",
            encoding="utf-8",
        )
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.freeze_candidate_batch(
                tsv,
                markdown,
                self.root / "wrong-parent",
                generation_brief={
                    "parent": {"candidate_id": "other-parent"}
                },
            )
        result = history_runtime.freeze_candidate_batch(
            tsv,
            markdown,
            self.root / "right-parent",
            generation_brief={
                "parent": {"candidate_id": "canonical-parent"}
            },
        )
        candidate = json.loads(
            pathlib.Path(result["candidates"][0]["path"]).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            candidate["declared_parent_candidate_id"],
            "canonical-parent",
        )

    def test_build_only_observation_never_calls_comparator(self):
        def pack_builder(_conn, _query, intent, _policy, **_kwargs):
            return {
                "schema_version": 1,
                "intent": intent,
                "retrieval_status": "budget_exceeded",
                "query": {"candidate_id": "I1"},
                "pack_sha256": hashlib.sha256(intent.encode()).hexdigest(),
            }

        result = history_runtime.build_candidate_packs(
            conn=None,
            candidate=self.frozen_candidate(),
            policy=self.policy,
            artifact_root=self.root / "observations",
            pack_builder=pack_builder,
            comparator_role_bytes=ROLE_PATH.read_bytes(),
            comparator_role_identity="roles/history-compare.md",
        )
        self.assertEqual(
            [item["status"] for item in result["observations"]],
            ["budget_exceeded", "budget_exceeded"],
        )
        self.assertFalse(
            list((self.root / "observations").glob(
                "*/history-comparison*.json"
            ))
        )

    def test_comparator_runs_only_for_selected_candidate(self):
        conn = self.connect_indexed()
        try:
            candidates = [
                self.frozen_candidate(
                    candidate_id,
                    f"A distinct bounded candidate {candidate_id}.",
                )
                for candidate_id in ("I1", "I2")
            ]
            observations = {}
            for candidate in candidates:
                observations[candidate["candidate_id"]] = (
                    history_runtime.build_candidate_packs(
                        conn=conn,
                        candidate=candidate,
                        policy=self.policy,
                        artifact_root=(
                            self.root / "observations"
                            / candidate["candidate_id"]
                        ),
                        comparator_role_bytes=ROLE_PATH.read_bytes(),
                        comparator_role_identity=
                            "roles/history-compare.md",
                    )
                )
            calls = []

            def comparator(intent, pack, _intent_root):
                calls.append((pack["query"]["candidate_id"], intent))
                relations = []
                for lineage in pack["lineages"]:
                    match = lineage["matches"][0]
                    relations.append(
                        {
                            "relation": "distinct",
                            "candidate_id": match["candidate_id"],
                            "lineage_id": match["lineage_id"],
                            "facet": match["facet"],
                            "evidence_id": match["evidence_id"],
                            "material_difference":
                                "The bounded propositions differ.",
                            "confidence": 0.8,
                        }
                    )
                return {
                    "status": "complete_no_match",
                    "comparator_version":
                        history_retrieval.COMPARATOR_VERSION,
                    "relations": relations,
                    "expansion_request": None,
                }

            selected = history_runtime.compare_selected_candidate(
                conn=conn,
                candidate=candidates[1],
                policy=self.policy,
                artifact_root=self.root / "observations" / "I2",
                observation=observations["I2"],
                comparator_runner=comparator,
                comparator_role_bytes=ROLE_PATH.read_bytes(),
                comparator_role_identity="roles/history-compare.md",
            )
            self.assertEqual(
                calls,
                [
                    ("I2", "duplicate_search"),
                    ("I2", "failure_pattern_search"),
                ],
            )
            self.assertTrue(
                all(item["receipt_path"] for item in selected["observations"])
            )
            self.assertFalse(
                list((self.root / "observations" / "I1").glob(
                    "*/history-comparison*.json"
                ))
            )
        finally:
            conn.close()

    def test_record_expansion_exhausts_policy_bound_as_uncertain(self):
        conn = self.connect_indexed()
        try:
            candidate = self.frozen_candidate(
                story=(
                    "Measure causal attribution under controlled "
                    "interventions."
                )
            )
            root = self.root / "record-expansion" / "I1"
            built = history_runtime.build_candidate_packs(
                conn=conn,
                candidate=candidate,
                policy=self.policy,
                artifact_root=root,
                comparator_role_bytes=ROLE_PATH.read_bytes(),
                comparator_role_identity=
                    "roles/history-compare.md",
            )
            calls = []

            def comparator(intent, pack, _intent_root):
                calls.append(
                    (intent, pack["expansion_round"])
                )
                relations = []
                for lineage in pack["lineages"]:
                    match = lineage["matches"][0]
                    relations.append(
                        {
                            "relation": "uncertain",
                            "candidate_id":
                                match["candidate_id"],
                            "lineage_id":
                                match["lineage_id"],
                            "facet": match["facet"],
                            "evidence_id":
                                match["evidence_id"],
                            "material_difference":
                                "The bounded evidence is unresolved.",
                            "confidence": 0.5,
                        }
                    )
                request = None
                if (
                    pack["expansion_round"]
                    < self.policy["max_expansion_rounds"]
                ):
                    request = {
                        "record_ids": [
                            pack["lineages"][0]["matches"][0][
                                "candidate_id"
                            ]
                        ]
                    }
                return {
                    "status": "uncertain",
                    "comparator_version":
                        history_retrieval.COMPARATOR_VERSION,
                    "relations": relations,
                    "expansion_request": request,
                }

            compared = (
                history_runtime.compare_selected_candidate(
                    conn=conn,
                    candidate=candidate,
                    policy=self.policy,
                    artifact_root=root,
                    observation=built,
                    comparator_runner=comparator,
                    comparator_role_bytes=
                        ROLE_PATH.read_bytes(),
                    comparator_role_identity=
                        "roles/history-compare.md",
                )
            )
            self.assertEqual(
                [item["status"] for item in compared["observations"]],
                ["uncertain", "uncertain"],
            )
            self.assertTrue(
                all(
                    len(item["attempts"])
                    == 1
                    + self.policy["max_expansion_rounds"]
                    for item in compared["observations"]
                )
            )
            self.assertEqual(
                calls,
                [
                    ("duplicate_search", 0),
                    ("duplicate_search", 1),
                    ("failure_pattern_search", 0),
                    ("failure_pattern_search", 1),
                ],
            )
            self.assertTrue(
                (
                    root
                    / "duplicate_search"
                    / "retrieval-pack-expansion-1.json"
                ).is_file()
            )
            self.assertFalse(
                (
                    root
                    / "duplicate_search"
                    / "retrieval-pack-expansion-2.json"
                ).exists()
            )
        finally:
            conn.close()

    def test_retrieval_pack_binds_full_frozen_candidate_bytes(self):
        captured = []

        def pack_builder(_conn, query, intent, _policy, **_kwargs):
            captured.append((intent, query))
            return {
                "schema_version": 1,
                "intent": intent,
                "retrieval_status": "budget_exceeded",
                "query": query,
            }

        candidate = {
            "candidate_id": "I1",
            "story": "A bounded candidate.",
            "theme": "Evaluation and Diagnostics",
            "candidate_markdown": (
                "## I1\n"
                "One-Sentence Story: A bounded candidate.\n"
                "Mechanism: A byte-bound mechanism.\n"
            ),
            "tsv_row_sha256": "11" * 32,
            "markdown_sha256": "22" * 32,
            "declared_parent_candidate_id": None,
        }
        candidate["content_sha256"] = (
            history_runtime.candidate_content_sha256(candidate)
        )
        history_runtime.build_candidate_packs(
            conn=None,
            candidate=candidate,
            policy=self.policy,
            artifact_root=self.root / "bound-query",
            pack_builder=pack_builder,
            comparator_role_bytes=ROLE_PATH.read_bytes(),
            comparator_role_identity="roles/history-compare.md",
        )
        self.assertEqual(len(captured), 2)
        for _, query in captured:
            self.assertEqual(
                query["candidate_content_sha256"],
                candidate["content_sha256"],
            )
            self.assertEqual(
                query["candidate_markdown"],
                candidate["candidate_markdown"],
            )

    def test_evolution_pack_contains_exact_validated_parent(self):
        conn = self.connect_indexed()
        try:
            parent_id = conn.execute(
                "SELECT candidate_id FROM candidates"
            ).fetchone()[0]
            candidate = {
                "candidate_id": "I1",
                "story": "A bounded revision of the estimand.",
                "theme": "Evaluation and Diagnostics",
                "declared_parent_candidate_id": parent_id,
            }
            pack = history_retrieval.build_pack(
                conn,
                {
                    "candidate_id": candidate["candidate_id"],
                    "story": candidate["story"],
                    "theme": candidate["theme"],
                    "declared_parent_candidate_id": parent_id,
                },
                "evolution_search",
                self.policy,
                comparator_role_bytes=ROLE_PATH.read_bytes(),
                comparator_role_identity="roles/history-compare.md",
            )
            self.assertEqual(
                pack["query"]["declared_parent_candidate_id"],
                parent_id,
            )
            retained = {
                match["candidate_id"]
                for lineage in pack["lineages"]
                for match in lineage["matches"]
            }
            self.assertIn(parent_id, retained)
        finally:
            conn.close()

    def test_expected_infrastructure_failure_is_archived_but_bug_escapes(self):
        calls = []

        def projection_failure(*_args, **_kwargs):
            raise history_projection.ProjectionError(
                "fixture projection unavailable"
            )

        result = history_runtime.build_candidate_packs(
            conn=None,
            candidate=self.frozen_candidate(),
            policy=self.policy,
            artifact_root=self.root / "projection-failure",
            pack_builder=projection_failure,
            comparator_role_bytes=ROLE_PATH.read_bytes(),
            comparator_role_identity="roles/history-compare.md",
        )
        self.assertEqual(calls, [])
        self.assertEqual(
            [item["status"] for item in result["observations"]],
            ["backend_failed", "backend_failed"],
        )

        def programming_bug(*_args, **_kwargs):
            raise ValueError("fixture programming bug")

        with self.assertRaisesRegex(ValueError, "programming bug"):
            history_runtime.build_candidate_packs(
                conn=None,
                candidate=self.frozen_candidate(),
                policy=self.policy,
                artifact_root=self.root / "programming-bug",
                pack_builder=programming_bug,
                comparator_role_bytes=ROLE_PATH.read_bytes(),
                comparator_role_identity="roles/history-compare.md",
            )


class ContainedStageContract(RuntimeFixture):
    def _generate_stage(self):
        startup = self.startup()
        host = self.root / "host-stage"
        output = self.root / "stage-output"
        output.mkdir()
        command = canonical([str(FAKE_STAGE_AGENT)]).decode(
            "utf-8"
        ).strip()
        prepared = history_runtime._build_stage_manifest_for_test(
            test_authority=self.shadow_test_authority(),
            test_state_root=self.root,
            stage="generate",
            seat_id="generate",
            db_path=self.database,
            policy_path=self.policy_path,
            input_paths={
                "generation_brief.json": pathlib.Path(
                    startup["brief_path"]
                ),
                "generation_policy.md":
                    self.generation_policy_path,
            },
            output_root=output,
            manifest_path=host / "manifest.json",
            command_json=command,
        )
        return prepared, output

    def _run_test_stage(
        self,
        prepared,
        *,
        authority=None,
        backend_entry_fd=None,
    ):
        if authority is None:
            authority = self.shadow_test_authority()
        return history_runtime._run_contained_stage_for_test(
            test_authority=authority,
            test_state_root=self.root,
            prepared=prepared,
            backend_entry_fd=backend_entry_fd,
        )

    def test_production_stage_rejects_registered_fixture_backend(self):
        startup = self.startup()
        command = canonical([str(FAKE_STAGE_AGENT)]).decode(
            "utf-8"
        ).strip()
        host = self.root / "production-fixture-host"
        output = self.root / "production-fixture-output"
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.build_stage_manifest(
                stage="generate",
                seat_id="generate",
                db_path=self.database,
                policy_path=self.policy_path,
                input_paths={
                    "generation_brief.json": pathlib.Path(
                        startup["brief_path"]
                    ),
                    "generation_policy.md":
                        self.generation_policy_path,
                },
                output_root=output,
                manifest_path=host / "manifest.json",
                command_json=command,
            )
        self.assertFalse(host.exists())
        self.assertFalse(output.exists())

    def test_generate_direction_snapshot_is_mounted_and_canonical(self):
        startup = self.startup()
        direction_path = self.root / "direction_constraint.json"
        direction_path.write_bytes(
            direction_contract.canonical_bytes(
                json.loads(
                    (ROOT / "directions" / "dynamic-spatial-memory-vla-v1.json")
                    .read_text(encoding="utf-8")
                )
            )
        )
        command = canonical([str(FAKE_STAGE_AGENT)]).decode("utf-8").strip()
        prepared = history_runtime._build_stage_manifest_for_test(
            test_authority=self.shadow_test_authority(),
            test_state_root=self.root,
            stage="generate",
            seat_id="directed-generate",
            db_path=self.database,
            policy_path=self.policy_path,
            input_paths={
                "generation_brief.json": pathlib.Path(startup["brief_path"]),
                "generation_policy.md": self.generation_policy_path,
                "direction_constraint.json": direction_path,
            },
            output_root=self.root / "directed-output",
            manifest_path=self.root / "directed-host" / "manifest.json",
            command_json=command,
        )
        manifest = json.loads(
            pathlib.Path(prepared["manifest_path"]).read_text(encoding="utf-8")
        )
        mounted = {
            item["mirror_path"]: item for item in manifest["inputs"]
        }
        self.assertEqual(
            history_runtime._INPUT_CAPS["direction_constraint.json"],
            16384,
        )
        self.assertEqual(
            history_runtime._STAGE_INPUTS["generate"],
            (
                {"generation_brief.json", "generation_policy.md"},
                {"research_context.md", "direction_constraint.json"},
            ),
        )
        self.assertEqual(
            mounted["direction_constraint.json"]["sha256"],
            hashlib.sha256(direction_path.read_bytes()).hexdigest(),
        )
        serialized = history_runtime.history_budget.serialize_stage_invocation(
            stage="generate",
            adapter_version=manifest["adapter"]["version"],
            fixed_instructions=(ROOT / manifest["role"]["source"]).read_text(
                encoding="utf-8"
            ),
            mounted_inputs={
                item["mirror_path"]: (
                    pathlib.Path(prepared["manifest_path"]).parent
                    / ("inputs-" + manifest["seat_id"])
                    / item["mirror_path"]
                ).read_bytes()
                for item in manifest["inputs"]
            },
            **{
                key: manifest["invocation"][key]
                for key in (
                    "candidate", "retrieval_payload", "receipts", "tool_schemas",
                    "messages", "output_schema_instructions",
                )
            },
        )
        self.assertEqual(
            hashlib.sha256(serialized).hexdigest(),
            manifest["invocation"]["expected_serialized_sha256"],
        )
        completion = self._run_test_stage(prepared)
        self.assertEqual(completion["stage"], "generate")
        tampered = history_runtime._build_stage_manifest_for_test(
            test_authority=self.shadow_test_authority(),
            test_state_root=self.root,
            stage="generate",
            seat_id="tampered-directed-generate",
            db_path=self.database,
            policy_path=self.policy_path,
            input_paths={
                "generation_brief.json": pathlib.Path(startup["brief_path"]),
                "generation_policy.md": self.generation_policy_path,
                "direction_constraint.json": direction_path,
            },
            output_root=self.root / "tampered-directed-output",
            manifest_path=(
                self.root / "tampered-directed-host" / "manifest.json"
            ),
            command_json=command,
        )
        tampered_manifest = json.loads(
            pathlib.Path(tampered["manifest_path"]).read_text(encoding="utf-8")
        )
        snapshot = (
            pathlib.Path(tampered_manifest["input_roots"][0])
            / "direction_constraint.json"
        )
        snapshot.chmod(0o600)
        snapshot.write_bytes(direction_path.read_bytes().replace(b"\n", b""))
        with self.assertRaises(history_runtime.RuntimeContractError):
            self._run_test_stage(tampered)

    def test_public_manifest_accepts_canonical_direction_with_local_executable(
        self,
    ):
        startup = self.startup()
        direction_path = self.root / "public-direction.json"
        direction_path.write_bytes(
            direction_contract.parse_contract_bytes(
                (
                    ROOT
                    / "directions"
                    / "dynamic-spatial-memory-vla-v1.json"
                ).read_bytes()
            )[1]
        )
        executable = pathlib.Path("/usr/bin/true")
        self.assertTrue(executable.is_file())
        prepared = history_runtime.build_stage_manifest(
            stage="generate",
            seat_id="public-directed-generate",
            db_path=self.database,
            policy_path=self.policy_path,
            input_paths={
                "generation_brief.json": pathlib.Path(startup["brief_path"]),
                "generation_policy.md": self.generation_policy_path,
                "direction_constraint.json": direction_path,
            },
            output_root=self.root / "public-directed-output",
            manifest_path=(
                self.root / "public-directed-host" / "manifest.json"
            ),
            command_json=canonical([str(executable)]).decode().strip(),
        )
        manifest = json.loads(
            pathlib.Path(prepared["manifest_path"]).read_text(encoding="utf-8")
        )
        mounted = {
            item["mirror_path"]: item["sha256"]
            for item in manifest["inputs"]
        }
        self.assertEqual(
            mounted["direction_constraint.json"],
            hashlib.sha256(direction_path.read_bytes()).hexdigest(),
        )

    def test_hash_consistent_noncanonical_direction_reaches_stage_rejection(
        self,
    ):
        startup = self.startup()
        canonical_contract = direction_contract.parse_contract_bytes(
            (
                ROOT
                / "directions"
                / "dynamic-spatial-memory-vla-v1.json"
            ).read_bytes()
        )[0]
        direction_path = self.root / "public-noncanonical-direction.json"
        direction_path.write_text(
            json.dumps(canonical_contract, indent=2) + "\n",
            encoding="utf-8",
        )
        prepared = history_runtime.build_stage_manifest(
            stage="generate",
            seat_id="public-noncanonical-generate",
            db_path=self.database,
            policy_path=self.policy_path,
            input_paths={
                "generation_brief.json": pathlib.Path(startup["brief_path"]),
                "generation_policy.md": self.generation_policy_path,
                "direction_constraint.json": direction_path,
            },
            output_root=self.root / "public-noncanonical-output",
            manifest_path=(
                self.root / "public-noncanonical-host" / "manifest.json"
            ),
            command_json=canonical(["/usr/bin/true"]).decode().strip(),
        )
        with self.assertRaisesRegex(
            history_runtime.RuntimeContractError,
            "direction contract is not canonical",
        ):
            history_runtime.run_contained_stage(prepared)

    def test_private_stage_wrapper_confines_every_input_path(self):
        startup = self.startup()
        command = canonical([str(FAKE_STAGE_AGENT)]).decode(
            "utf-8"
        ).strip()
        host = self.root / "escaped-input-host"
        output = self.root / "escaped-input-output"
        with self.assertRaisesRegex(
            history_runtime.RuntimeContractError,
            "escapes test state",
        ):
            history_runtime._build_stage_manifest_for_test(
                test_authority=self.shadow_test_authority(),
                test_state_root=self.root,
                stage="generate",
                seat_id="generate",
                db_path=self.database,
                policy_path=self.policy_path,
                input_paths={
                    "generation_brief.json": pathlib.Path(
                        startup["brief_path"]
                    ),
                    "generation_policy.md":
                        ROOT / "brainstorming_policy.md",
                },
                output_root=output,
                manifest_path=host / "manifest.json",
                command_json=command,
            )
        self.assertFalse(host.exists())
        self.assertFalse(output.exists())

    def test_manifest_is_host_owned_and_binds_closed_command_grammar(self):
        prepared, _ = self._generate_stage()
        manifest = json.loads(
            pathlib.Path(prepared["manifest_path"]).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["role"]["source"], "roles/generate.md")
        self.assertEqual(
            manifest["history_store"],
            {
                "root": str(self.database.parent.resolve()),
                "source": "history.sqlite3",
            },
        )
        self.assertEqual(
            prepared["command_argv"], [str(FAKE_STAGE_AGENT)]
        )
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.build_stage_manifest(
                stage="generate",
                seat_id="generate",
                db_path=self.database,
                policy_path=self.policy_path,
                input_paths={
                    "generation_brief.json":
                        self.root / "tmp" / "round"
                        / "generation_brief.json",
                    "generation_policy.md":
                        ROOT / "brainstorming_policy.md",
                },
                output_root=self.root / "stage-output",
                manifest_path=self.root / "bad-manifest.json",
                command_json='["/bin/echo","--search"]',
            )

    def test_shadow_review_manifest_cannot_mount_history_summary(self):
        startup = self.startup()
        candidate = self.root / "candidate.json"
        candidate.write_bytes(
            canonical(
                {
                    "candidate_id": "I1",
                    "story": "A bounded candidate.",
                    "theme": "Evaluation and Diagnostics",
                }
            )
        )
        prior_work = self.root / "prior-work.md"
        prior_work.write_text(
            "## I1\nOverlap: low\nPapers Read: 5\n",
            encoding="utf-8",
        )
        summary = self.root / "history-summary.json"
        summary.write_bytes(
            canonical(
                {
                    "schema_version": 1,
                    "candidate_id": "I1",
                    "candidate_content_sha256": "11" * 32,
                    "adapter_version": "history-stage-v1",
                    "receipts": [{}, {}],
                    "aggregate_sha256": "22" * 32,
                }
            )
        )
        command = canonical([str(FAKE_STAGE_AGENT)]).decode(
            "utf-8"
        ).strip()
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime._build_stage_manifest_for_test(
                test_authority=self.shadow_test_authority(),
                test_state_root=self.root,
                stage="review",
                seat_id="review-1-I1",
                db_path=self.database,
                policy_path=self.policy_path,
                input_paths={
                    "candidate.json": candidate,
                    "prior_work.md": prior_work,
                    "review_contract.md":
                        self.review_contract_path,
                    "history_summary.json": summary,
                },
                output_root=self.root / "shadow-review-output",
                manifest_path=(
                    self.root / "shadow-review-host" / "manifest.json"
                ),
                command_json=command,
            )

    def test_enforcement_stage_requires_opaque_authority_at_build_and_run(self):
        policy, root, capability = (
            CapabilityContract._signed_capability(self)
        )
        self.policy = policy
        authority = history_runtime._validate_runtime_mode_for_test(
            policy,
            capability=capability,
            trust_root=root,
        )
        connection = self.connect_indexed()
        try:
            brief = history_projection.build_generation_brief(
                connection, policy
            )
        finally:
            connection.close()
        brief_path = self.root / "enforcement-brief.json"
        brief_path.write_bytes(canonical(brief))
        inputs = {
            "generation_brief.json": brief_path,
            "generation_policy.md":
                self.generation_policy_path,
        }
        command = canonical([str(FAKE_STAGE_AGENT)]).decode(
            "utf-8"
        ).strip()
        denied_host = self.root / "denied-stage"
        denied_output = self.root / "denied-output"
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.build_stage_manifest(
                stage="generate",
                seat_id="enforcement-generate",
                db_path=self.database,
                policy_path=self.policy_path,
                input_paths=inputs,
                output_root=denied_output,
                manifest_path=denied_host / "manifest.json",
                command_json=command,
            )
        self.assertFalse(denied_host.exists())
        self.assertFalse(denied_output.exists())
        prepared = history_runtime._build_stage_manifest_for_test(
            test_authority=authority,
            test_state_root=self.root,
            stage="generate",
            seat_id="enforcement-generate",
            db_path=self.database,
            policy_path=self.policy_path,
            input_paths=inputs,
            output_root=self.root / "enforcement-output",
            manifest_path=(
                self.root / "enforcement-host" / "manifest.json"
            ),
            command_json=command,
            authority=authority,
        )
        manifest = json.loads(
            pathlib.Path(prepared["manifest_path"]).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            manifest["policy"]["source"],
            "synthetic_contract_only",
        )
        entry_log = self.root / "backend-entry.log"
        descriptor = os.open(
            entry_log,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            with self.assertRaises(
                history_runtime.RuntimeContractError
            ):
                history_runtime.run_contained_stage(
                    prepared,
                    backend_entry_fd=descriptor,
                )
            self.assertEqual(entry_log.read_bytes(), b"")
            with self.assertRaises(
                history_runtime.RuntimeContractError
            ):
                history_runtime.run_contained_stage(
                    prepared,
                    authority={
                        "mode": "enforcement",
                        "policy_sha256": (
                            history_runtime.sha256(
                                canonical(policy)
                            )
                        ),
                    },
                    backend_entry_fd=descriptor,
                )
            self.assertEqual(entry_log.read_bytes(), b"")
            self._run_test_stage(
                prepared,
                authority=authority,
                backend_entry_fd=descriptor,
            )
        finally:
            os.close(descriptor)
        self.assertEqual(entry_log.read_bytes(), b"backend-entry\n")

    def test_contained_generate_requires_exact_completion_receipt(self):
        prepared, output = self._generate_stage()
        completion = self._run_test_stage(prepared)
        self.assertEqual(completion["stage"], "generate")
        self.assertTrue((output / "ideas.tsv").is_file())
        self.assertTrue((output / "ideas.md").is_file())
        self.assertTrue(
            history_runtime.verify_stage_completion(prepared)
        )
        pathlib.Path(prepared["completion_path"]).unlink()
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.verify_stage_completion(prepared)

    def test_completion_rejects_self_consistent_preflight_forgery(self):
        prepared, _ = self._generate_stage()
        self._run_test_stage(prepared)
        preflight_path = pathlib.Path(prepared["preflight_path"])
        completion_path = pathlib.Path(prepared["completion_path"])
        preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
        completion = json.loads(
            completion_path.read_text(encoding="utf-8")
        )
        preflight["manifest_sha256"] = "0" * 64
        preflight_path.write_bytes(canonical(preflight))
        completion["preflight_sha256"] = hashlib.sha256(
            canonical(preflight)
        ).hexdigest()
        completion.pop("completion_id")
        completion["completion_id"] = hashlib.sha256(
            b"history-stage-completion-v1\0"
            + canonical(completion)
        ).hexdigest()
        completion_path.write_bytes(canonical(completion))
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.verify_stage_completion(prepared)

    def test_completion_rejects_symlink_and_hardlink_output_swaps(self):
        prepared, output = self._generate_stage()
        self._run_test_stage(prepared)
        ideas = output / "ideas.tsv"
        external = self.root / "external-identical.tsv"
        external.write_bytes(ideas.read_bytes())
        ideas.unlink()
        ideas.symlink_to(external)
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.verify_stage_completion(prepared)
        ideas.unlink()
        os.link(external, ideas)
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.verify_stage_completion(prepared)

    def test_artifact_reader_rejects_symlinked_parent_directory(self):
        real = self.root / "real-parent"
        real.mkdir()
        source = real / "artifact.json"
        source.write_bytes(canonical({"schema_version": 1}))
        alias = self.root / "parent-alias"
        alias.symlink_to(real, target_is_directory=True)
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime._read_bound_regular(
                alias / source.name,
                "symlink-parent artifact",
            )


class ReceiptAndResumeContract(RuntimeFixture):
    def _receipt(self, conn, query, intent):
        pack = history_retrieval.build_pack(
            conn,
            history_runtime._retrieval_query(query, intent),
            intent,
            self.policy,
            comparator_role_bytes=ROLE_PATH.read_bytes(),
            comparator_role_identity="roles/history-compare.md",
        )
        relations = []
        for lineage in pack["lineages"]:
            match = lineage["matches"][0]
            relations.append(
                {
                    "relation": "distinct",
                    "candidate_id": match["candidate_id"],
                    "lineage_id": match["lineage_id"],
                    "facet": match["facet"],
                    "evidence_id": match["evidence_id"],
                    "material_difference": "The bounded propositions differ.",
                    "confidence": 0.8,
                }
            )
        response = {
            "status": "complete_no_match",
            "comparator_version": history_retrieval.COMPARATOR_VERSION,
            "relations": relations,
            "expansion_request": None,
        }
        receipt = history_retrieval.finalize_comparison(
            conn,
            pack,
            response,
            self.policy,
        )
        return pack, receipt

    def test_summary_binds_ordered_replayed_receipts_and_aggregate_hash(self):
        self.policy = copy.deepcopy(self.policy)
        self.policy["mode"] = "enforcement"
        self.policy_path.write_bytes(canonical(self.policy))
        conn = self.connect_indexed()
        try:
            candidate = self.frozen_candidate(
                "I1", "A distinct bounded candidate."
            )
            bindings = [
                self._receipt(conn, candidate, "duplicate_search"),
                self._receipt(conn, candidate, "failure_pattern_search"),
            ]
            summary = history_runtime.build_history_summary(
                conn,
                candidate,
                bindings,
                self.policy,
            )
            self.assertEqual(
                [item["intent"] for item in summary["receipts"]],
                ["duplicate_search", "failure_pattern_search"],
            )
            self.assertEqual(
                summary["candidate_content_sha256"],
                candidate["content_sha256"],
            )
            self.assertEqual(
                summary["aggregate_sha256"],
                history_runtime.history_summary_sha256(summary),
            )
            forged = copy.deepcopy(summary)
            forged["receipts"][0]["provenance"][
                "source_watermark"
            ] += 1
            with self.assertRaises(history_runtime.RuntimeContractError):
                history_runtime.verify_history_summary(
                    conn,
                    candidate,
                    forged,
                    self.policy,
                )
        finally:
            conn.close()

    def test_resume_requires_every_runtime_identity_dimension(self):
        binding = history_runtime.resume_binding(
            mode="shadow",
            policy_version="retrieval-policy-v1",
            policy_sha256="11" * 32,
            source_watermark=7,
            index_generation=3,
            pack_sha256="22" * 32,
            comparator_version="history-comparator-v1",
            candidate_content_sha256="33" * 32,
            adapter_version="history-stage-v1",
            preflight_sha256="44" * 32,
        )
        self.assertTrue(history_runtime.resume_matches(binding, dict(binding)))
        for field in history_runtime.RESUME_BINDING_FIELDS:
            changed = dict(binding)
            changed[field] = (
                changed[field] + 1
                if isinstance(changed[field], int)
                else str(changed[field]) + "-changed"
            )
            with self.subTest(field=field):
                self.assertFalse(history_runtime.resume_matches(binding, changed))


class RoundCoordinatorContract(CapabilityContract):
    @staticmethod
    def _portable_profile():
        return provider_adapters._resolve_command_intent_for_test(
            provider_adapters.load_registry(PROVIDER_REGISTRY),
            "hunt",
            "codex",
            model="MODEL",
            reasoning="high",
            executable_lookup=lambda _: str(FAKE_PORTABLE_PROVIDER),
        )

    def test_nonissued_portable_profiles_fail_before_state_or_launch(self):
        forged = dataclasses.replace(
            self._portable_profile(),
            requested_model="forged-model",
            execution_request_profile_hash="b" * 64,
        )
        self.assertFalse(
            provider_adapters.command_intent_is_issued(forged)
        )
        comparison_root = self.root / "forged-comparison"
        review_plan_path = self.root / "forged-review-plan.json"
        review_index_path = self.root / "forged-review-index.json"
        with mock.patch.object(
            history_runtime, "_run_portable_stage"
        ) as launch:
            with self.assertRaisesRegex(
                history_runtime.RuntimeContractError,
                "portable provider request profile is invalid",
            ):
                history_runtime.compare_frozen_targets(
                    db_path=self.database,
                    policy_path=self.policy_path,
                    batch_path=self.root / "missing-batch.json",
                    artifact_root=comparison_root,
                    selection_path=self.root / "missing-selection.json",
                    executor="portable-v2",
                    portable_request_profile=forged,
                )
            with self.assertRaisesRegex(
                history_runtime.RuntimeContractError,
                "portable provider request profile is invalid",
            ):
                history_runtime.seal_round_review_plan(
                    db_path=self.database,
                    policy_path=self.policy_path,
                    batch_path=self.root / "missing-batch.json",
                    selection_path=self.root / "missing-selection.json",
                    comparison_index_path=(
                        comparison_root / "comparison-index.json"
                    ),
                    artifact_root=comparison_root,
                    prior_work_path=self.root / "missing-prior-work.md",
                    review_contract_path=self.review_contract_path,
                    reviewer_commands={},
                    executor="portable-v2",
                    reviewer_request_profiles={"1": forged},
                    round_date="2026-07-24",
                    min_read=5,
                    axiom_min_cracks=2,
                    output_path=review_plan_path,
                )
            with self.assertRaisesRegex(
                history_runtime.RuntimeContractError,
                "portable provider request profile is invalid",
            ):
                history_runtime.run_review_matrix(
                    db_path=self.database,
                    policy_path=self.policy_path,
                    batch_path=self.root / "missing-batch.json",
                    review_plan_path=review_plan_path,
                    reviewer_commands={},
                    executor="portable-v2",
                    reviewer_request_profiles={"1": forged},
                    stage_root=self.root / "forged-review-stages",
                    output_path=review_index_path,
                )
            launch.assert_not_called()
        self.assertFalse(comparison_root.exists())
        self.assertFalse(review_plan_path.exists())
        self.assertFalse(review_index_path.exists())

    @staticmethod
    def _direction_contract():
        return json.loads(
            (
                ROOT
                / "directions"
                / "dynamic-spatial-memory-vla-v1.json"
            ).read_text(encoding="utf-8")
        )

    @staticmethod
    def _convert_batch_to_schema_v1(batch_path, manifest):
        material = dict(manifest)
        material.pop("batch_sha256")
        material.pop("direction")
        material["schema_version"] = 1
        legacy = dict(material)
        legacy["batch_sha256"] = history_runtime.sha256(
            b"history-runtime-batch-v1\0"
            + history_runtime.canonical_bytes(material)
        )
        batch_path.chmod(0o600)
        batch_path.write_bytes(canonical(legacy))
        return legacy

    def _sealed_round(
        self,
        selected=("I2",),
        killed=(),
        candidate_specs=None,
        direction_contract=None,
        legacy_v1=False,
        stem=None,
    ):
        startup = self.startup()
        generated = self.root / (
            "coordinator-generated"
            if stem is None
            else f"{stem}-generated"
        )
        generated.mkdir()
        ideas_tsv = generated / "ideas.all.tsv"
        ideas_md = generated / "ideas.all.md"
        if candidate_specs is None:
            candidate_specs = (
                {
                    "candidate_id": "I1",
                    "story": "First bounded candidate.",
                    "theme": "Evaluation and Diagnostics",
                    "falsification": (
                        "Reject if the bounded estimate misses its "
                        "held-out threshold."
                    ),
                },
                {
                    "candidate_id": "I2",
                    "story": "Second bounded candidate.",
                    "theme": "Evaluation and Diagnostics",
                    "falsification": (
                        "Reject if the bounded estimate misses its "
                        "held-out threshold."
                    ),
                },
            )
        candidate_ids = [
            item["candidate_id"] for item in candidate_specs
        ]
        ideas_tsv.write_text(
            "".join(
                f"{item['candidate_id']}\t{item['story']}\t"
                f"{item['theme']}\n"
                for item in candidate_specs
            ),
            encoding="utf-8",
        )
        ideas_md.write_text(
            "\n".join(
                (
                    f"## {item['candidate_id']}\n"
                    f"One-Sentence Story: {item['story']}\n"
                    f"Theme: {item['theme']}\n"
                    "Minimal Falsification Experiment: "
                    f"{item['falsification']}\n"
                )
                for item in candidate_specs
            ),
            encoding="utf-8",
        )
        batch_root = self.root / (
            "frozen-batch"
            if stem is None
            else f"{stem}-frozen-batch"
        )
        frozen = history_runtime.freeze_candidate_batch(
            ideas_tsv,
            ideas_md,
            batch_root,
            generation_brief=json.loads(
                pathlib.Path(startup["brief_path"]).read_text(
                    encoding="utf-8"
                )
            ),
            direction_contract=direction_contract,
        )
        batch_path = batch_root / "batch.json"
        if legacy_v1:
            frozen = self._convert_batch_to_schema_v1(
                batch_path, frozen
            )
        observation_root = self.root / (
            "history-observation"
            if stem is None
            else f"{stem}-history-observation"
        )
        history_runtime.observe_frozen_batch(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=batch_path,
            artifact_root=observation_root,
        )
        selector = self.root / (
            "select.tsv" if stem is None else f"{stem}-select.tsv"
        )
        ordered = list(selected) + [
            candidate_id
            for candidate_id in candidate_ids
            if candidate_id not in selected
        ]
        selector.write_text(
            "".join(
                f"{candidate_id}\t{rank}\t"
                "bounded\tbounded\tbounded\tbounded\n"
                for rank, candidate_id in enumerate(ordered, 1)
            ),
            encoding="utf-8",
        )
        prescreen = self.root / (
            "prescreen.md"
            if stem is None
            else f"{stem}-prescreen.md"
        )
        prescreen.write_text(
            "".join(
                (
                    f"## {candidate_id}\n"
                    "- Query: https://api.semanticscholar.org/"
                    "graph/v1/paper/search?query=bounded\n"
                    f"Occupant: https://example.com/{candidate_id}\n"
                    "Decision: kill\n"
                )
                if candidate_id in killed
                else f"## {candidate_id}\nDecision: keep\n"
                for candidate_id in candidate_ids
            ),
            encoding="utf-8",
        )
        selection_path = self.root / (
            "selection.json"
            if stem is None
            else f"{stem}-selection.json"
        )
        history_runtime.seal_round_selection(
            batch_path=batch_path,
            round_observation_path=(
                observation_root / "round-observation.json"
            ),
            generation_brief_path=startup["brief_path"],
            selector_path=selector,
            prescreen_path=prescreen,
            short_max=max(1, len(selected)),
            output_path=selection_path,
        )
        return {
            "batch": batch_path,
            "observation_root": observation_root,
            "selection": selection_path,
            "selector": selector,
            "direction_identity":
                history_runtime.frozen_batch_direction(frozen),
        }

    def _compared_round(
        self,
        comparison_status="complete_no_match",
        *,
        selected=("I2",),
        killed=(),
        candidate_specs=None,
        direction_contract=None,
        legacy_v1=False,
        stem=None,
    ):
        state = self._sealed_round(
            selected=selected,
            killed=killed,
            candidate_specs=candidate_specs,
            direction_contract=direction_contract,
            legacy_v1=legacy_v1,
            stem=stem,
        )
        command = canonical([str(FAKE_STAGE_AGENT)]).decode(
            "utf-8"
        ).strip()
        history_runtime._compare_frozen_targets_for_test(
            test_authority=self.shadow_test_authority(),
            test_state_root=self.root,
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            artifact_root=state["observation_root"],
            selection_path=state["selection"],
            command_json=command,
            test_comparator_status=comparison_status,
        )
        return state

    def test_portable_indices_persist_only_closed_public_stage_descriptors(self):
        profile = self._portable_profile()
        state = self._sealed_round(stem="portable-public")
        comparison_path = (
            state["observation_root"] / "comparison-index.json"
        )
        comparison = history_runtime.compare_frozen_targets(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            artifact_root=state["observation_root"],
            selection_path=state["selection"],
            executor="portable-v2",
            portable_request_profile=profile,
        )
        public_fields = {
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
        }
        private_fields = {
            "prepared",
            "executable_path",
            "serialized_prompt",
            "provider_request",
            "provider_command",
            "input_paths",
            "output_root",
            "output_paths",
            "state_root",
            "preflight_path",
            "completion_path",
        }

        def assert_public_stage(stage, reference_root):
            self.assertEqual(set(stage), public_fields)
            self.assertTrue(private_fields.isdisjoint(stage))
            self.assertNotIn(
                str(self.root), canonical(stage).decode("utf-8")
            )
            for descriptor in (
                stage["preflight"],
                stage["completion"],
                *stage["outputs"].values(),
            ):
                relative = pathlib.PurePosixPath(descriptor["path"])
                self.assertFalse(relative.is_absolute())
                self.assertNotIn("..", relative.parts)
                self.assertTrue((reference_root / relative).is_file())
            envelope = pathlib.PurePosixPath(
                stage["completion"]["model_envelope_path"]
            )
            self.assertFalse(envelope.is_absolute())
            self.assertNotIn("..", envelope.parts)
            self.assertTrue((reference_root / envelope).is_file())

        self.assertEqual(comparison["schema_version"], 2)
        for target in comparison["targets"]:
            candidate_root = (
                state["observation_root"] / target["candidate_id"]
            )
            self.assertTrue(target["portable_stages"])
            for stage in target["portable_stages"]:
                assert_public_stage(stage, candidate_root)

        research_root = self.root / "portable-public-research"
        research = history_runtime.materialize_research_views(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            selection_path=state["selection"],
            comparison_index_path=comparison_path,
            artifact_root=state["observation_root"],
            output_root=research_root,
            authority=self.shadow_test_authority(),
        )
        self.assertEqual(research["eligible_order"], ["I2"])

        original_comparison = comparison_path.read_bytes()
        changed = json.loads(original_comparison.decode("utf-8"))
        changed["targets"][0]["portable_stages"][0]["extra"] = "private"
        material = dict(changed)
        material.pop("comparison_index_sha256")
        changed["comparison_index_sha256"] = history_runtime.sha256(
            b"history-runtime-comparison-index-v2\0"
            + history_runtime.canonical_bytes(material)
        )
        comparison_path.chmod(0o600)
        comparison_path.write_bytes(canonical(changed))
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.materialize_research_views(
                db_path=self.database,
                policy_path=self.policy_path,
                batch_path=state["batch"],
                selection_path=state["selection"],
                comparison_index_path=comparison_path,
                artifact_root=state["observation_root"],
                output_root=self.root / "portable-public-tampered",
                authority=self.shadow_test_authority(),
            )
        comparison_path.write_bytes(original_comparison)

        prior_work = self.root / "portable-public-prior-work.md"
        prior_work.write_text(
            "## I2\nPapers Read: 5\nOverlap: low\n",
            encoding="utf-8",
        )
        profiles = {"1": profile}
        review_plan_path = self.root / "portable-public-review-plan.json"
        history_runtime.seal_round_review_plan(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            selection_path=state["selection"],
            comparison_index_path=comparison_path,
            artifact_root=state["observation_root"],
            prior_work_path=prior_work,
            review_contract_path=self.review_contract_path,
            reviewer_commands={},
            executor="portable-v2",
            reviewer_request_profiles=profiles,
            round_date="2026-07-24",
            min_read=5,
            axiom_min_cracks=2,
            output_path=review_plan_path,
        )
        review_index_path = self.root / "portable-public-review-index.json"
        review = history_runtime.run_review_matrix(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            review_plan_path=review_plan_path,
            reviewer_commands={},
            executor="portable-v2",
            reviewer_request_profiles=profiles,
            stage_root=self.root / "portable-public-review-stages",
            output_path=review_index_path,
        )
        self.assertEqual(review["schema_version"], 2)
        self.assertEqual(len(review["entries"]), 1)
        entry = review["entries"][0]
        self.assertEqual(set(entry), {"candidate_id", "seat_id", "stage"})
        assert_public_stage(entry["stage"], review_index_path.parent)
        self.assertTrue(
            history_runtime.verify_review_matrix(
                db_path=self.database,
                policy_path=self.policy_path,
                batch_path=state["batch"],
                review_plan_path=review_plan_path,
                review_index_path=review_index_path,
            )
        )
        aggregation = history_runtime.build_round_aggregation(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            review_plan_path=review_plan_path,
            review_index_path=review_index_path,
            output_path=self.root / "portable-public-aggregation.json",
        )
        self.assertEqual(len(aggregation["ledger_rows"]), 1)

        original_review = review_index_path.read_bytes()
        changed = json.loads(original_review.decode("utf-8"))
        changed["entries"][0]["stage"]["extra"] = "private"
        material = dict(changed)
        material.pop("review_index_sha256")
        changed["review_index_sha256"] = history_runtime.sha256(
            b"history-runtime-review-index-v2\0"
            + history_runtime.canonical_bytes(material)
        )
        review_index_path.chmod(0o600)
        review_index_path.write_bytes(canonical(changed))
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.verify_review_matrix(
                db_path=self.database,
                policy_path=self.policy_path,
                batch_path=state["batch"],
                review_plan_path=review_plan_path,
                review_index_path=review_index_path,
            )
        review_index_path.write_bytes(original_review)

    def _resume_round(
        self,
        *,
        stem,
        direction_contract=None,
        legacy_v1=False,
    ):
        state = self._compared_round(
            direction_contract=direction_contract,
            legacy_v1=legacy_v1,
            stem=stem,
        )
        prior_work = self.root / f"{stem}-prior-work.md"
        prior_work.write_text(
            "## I2\nPapers Read: 5\nOverlap: low\n",
            encoding="utf-8",
        )
        resume_path = self.root / f"{stem}-resume.json"
        history_runtime.seal_resume_state(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            selection_path=state["selection"],
            artifact_root=state["observation_root"],
            comparison_index_path=(
                state["observation_root"]
                / "comparison-index.json"
            ),
            prior_work_path=prior_work,
            output_path=resume_path,
        )
        return state, resume_path

    def directed_resume(self):
        return self._resume_round(
            stem="directed",
            direction_contract=self._direction_contract(),
        )

    def undirected_resume(self):
        return self._resume_round(stem="undirected")

    def _seal_review_plan(
        self,
        state,
        *,
        stem,
        prior_work_path=None,
        review_contract_path=None,
        reviewer_count=2,
        authority=None,
    ):
        artifact_root = state.get(
            "observation_root", state.get("artifact_root")
        )
        command = canonical([str(FAKE_STAGE_AGENT)]).decode(
            "utf-8"
        ).strip()
        commands = {
            str(index): command
            for index in range(1, reviewer_count + 1)
        }
        if prior_work_path is None:
            selection = json.loads(
                state["selection"].read_text(encoding="utf-8")
            )
            prior_work_path = self.root / f"{stem}-prior-work.md"
            prior_work_path.write_text(
                "".join(
                    f"## {item['candidate_id']}\n"
                    "Papers Read: 5\n"
                    "Overlap: low\n"
                    for item in selection["targets"]
                    if item["disposition"] == "shortlist"
                ),
                encoding="utf-8",
            )
        if review_contract_path is None:
            review_contract_path = self.review_contract_path
        plan_path = self.root / f"{stem}-review-plan.json"
        plan = history_runtime.seal_round_review_plan(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            selection_path=state["selection"],
            comparison_index_path=(
                artifact_root
                / "comparison-index.json"
            ),
            artifact_root=artifact_root,
            prior_work_path=prior_work_path,
            review_contract_path=review_contract_path,
            reviewer_commands=commands,
            round_date="2026-07-24",
            min_read=5,
            axiom_min_cracks=2,
            output_path=plan_path,
            authority=authority,
        )
        return {
            "commands": commands,
            "plan": plan,
            "plan_path": plan_path,
            "prior_work_path": pathlib.Path(prior_work_path),
            "review_contract_path":
                pathlib.Path(review_contract_path),
        }

    def _review_chain(
        self,
        state,
        *,
        stem,
        review_verdict="strong-accept",
        reviewer_count=2,
        authority=None,
    ):
        sealed = self._seal_review_plan(
            state,
            stem=stem,
            reviewer_count=reviewer_count,
            authority=authority,
        )
        index_path = self.root / f"{stem}-review-index.json"
        test_authority = (
            authority
            if authority is not None
            and authority.get("scope")
            == history_runtime.SYNTHETIC_SCOPE
            else self.shadow_test_authority()
        )
        index = history_runtime._run_review_matrix_for_test(
            test_authority=test_authority,
            test_state_root=self.root,
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            review_plan_path=sealed["plan_path"],
            reviewer_commands=sealed["commands"],
            stage_root=self.root / f"{stem}-review-stages",
            output_path=index_path,
            authority=authority,
            test_review_verdict=review_verdict,
        )
        aggregation_path = (
            self.root / f"{stem}-round-aggregation.json"
        )
        aggregation = history_runtime.build_round_aggregation(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            review_plan_path=sealed["plan_path"],
            review_index_path=index_path,
            output_path=aggregation_path,
            authority=authority,
        )
        return {
            **sealed,
            "index": index,
            "index_path": index_path,
            "aggregation": aggregation,
            "aggregation_path": aggregation_path,
        }

    def _enforcement_round(
        self,
        *,
        retrieval_status="complete",
        comparison_status="complete_no_match",
        contained=False,
        expansion_backend_failed=False,
    ):
        policy, root, bundle = self._signed_capability()
        self.policy = policy
        authority = history_runtime._validate_runtime_mode_for_test(
            policy,
            capability=bundle,
            trust_root=root,
        )
        scope = history_runtime._runtime_for_test(
            policy,
            authority,
            self.root,
        )
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        connection = self.connect_indexed()
        try:
            brief = history_projection.build_generation_brief(
                connection, policy
            )
            generated = self.root / "enforcement-generated"
            generated.mkdir()
            ideas_tsv = generated / "ideas.all.tsv"
            ideas_md = generated / "ideas.all.md"
            story = (
                "Measure causal attribution under controlled "
                "interventions."
            )
            ideas_tsv.write_text(
                "I1\t"
                + story
                + "\tEvaluation and Diagnostics\n",
                encoding="utf-8",
            )
            ideas_md.write_text(
                "## I1\n"
                f"One-Sentence Story: {story}\n"
                "Theme: Evaluation and Diagnostics\n",
                encoding="utf-8",
            )
            brief_path = self.root / "enforcement-brief.json"
            brief_path.write_bytes(canonical(brief))
            batch_root = self.root / "enforcement-batch"
            history_runtime.freeze_candidate_batch(
                ideas_tsv,
                ideas_md,
                batch_root,
                generation_brief=brief,
            )
            batch_path = batch_root / "batch.json"
            _, candidates = history_runtime._load_batch_candidates(
                batch_path
            )
            candidate = candidates["I1"]
            artifact_root = self.root / "enforcement-observation"
            candidate_root = artifact_root / "I1"

            if retrieval_status == "complete":
                pack_builder = history_retrieval.build_pack
            elif retrieval_status == "partial":
                def pack_builder(
                    conn, query, intent, sealed_policy, **kwargs
                ):
                    return history_retrieval.build_pack(
                        conn,
                        query,
                        intent,
                        sealed_policy,
                        disabled_channels={"dense"},
                        **kwargs,
                    )
            elif retrieval_status == "backend_failed":
                def pack_builder(*_args, **_kwargs):
                    raise history_projection.ProjectionError(
                        "synthetic projection failure"
                    )
            elif retrieval_status == "budget_exceeded":
                def pack_builder(
                    _conn, query, intent, _policy, **_kwargs
                ):
                    return {
                        "schema_version": 1,
                        "intent": intent,
                        "retrieval_status": "budget_exceeded",
                        "query": query,
                    }
            else:
                raise AssertionError(retrieval_status)
            built = history_runtime.build_candidate_packs(
                conn=connection,
                candidate=candidate,
                policy=policy,
                artifact_root=candidate_root,
                pack_builder=pack_builder,
                comparator_role_bytes=ROLE_PATH.read_bytes(),
                comparator_role_identity="roles/history-compare.md",
            )
            round_observation = {
                "schema_version": 1,
                "batch_path": str(batch_path),
                "candidates": [
                    {
                        "candidate_id": "I1",
                        "candidate_content_sha256":
                            candidate["content_sha256"],
                        "observation_path": str(
                            candidate_root
                            / "build-observation.json"
                        ),
                        "observation_sha256":
                            built["observation_sha256"],
                        "retrieval_statuses": [
                            item["retrieval_status"]
                            for item in built["observations"]
                        ],
                    }
                ],
            }
            round_observation["round_observation_sha256"] = (
                history_runtime.sha256(
                    b"history-runtime-round-observation-v1\0"
                    + canonical(round_observation)
                )
            )
            history_runtime._publish_immutable(
                artifact_root / "round-observation.json",
                canonical(round_observation),
            )
            selector = self.root / "enforcement-select.tsv"
            selector.write_text(
                "I1\t1\tbounded\tbounded\tbounded\tbounded\n",
                encoding="utf-8",
            )
            prescreen = self.root / "enforcement-prescreen.md"
            prescreen.write_text(
                "## I1\nDecision: keep\n",
                encoding="utf-8",
            )
            selection_path = self.root / "enforcement-selection.json"
            history_runtime.seal_round_selection(
                batch_path=batch_path,
                round_observation_path=(
                    artifact_root / "round-observation.json"
                ),
                generation_brief_path=brief_path,
                selector_path=selector,
                prescreen_path=prescreen,
                short_max=1,
                output_path=selection_path,
            )
            if retrieval_status == "complete" and not contained:
                def comparator(_intent, pack, _intent_root):
                    relations = []
                    for lineage in pack["lineages"]:
                        match = lineage["matches"][0]
                        relations.append(
                            {
                                "relation": (
                                    "uncertain"
                                    if comparison_status == "uncertain"
                                    else "distinct"
                                ),
                                "candidate_id": match["candidate_id"],
                                "lineage_id": match["lineage_id"],
                                "facet": match["facet"],
                                "evidence_id": match["evidence_id"],
                                "material_difference": (
                                    "The synthetic evidence remains "
                                    "bounded."
                                ),
                                "confidence": 0.5,
                            }
                        )
                    return {
                        "status": comparison_status,
                        "comparator_version": (
                            history_retrieval.COMPARATOR_VERSION
                        ),
                        "relations": relations,
                        "expansion_request": None,
                    }

                compared = history_runtime.compare_selected_candidate(
                    conn=connection,
                    candidate=candidate,
                    policy=policy,
                    artifact_root=candidate_root,
                    observation=built,
                    comparator_runner=comparator,
                    comparator_role_bytes=ROLE_PATH.read_bytes(),
                    comparator_role_identity=(
                        "roles/history-compare.md"
                    ),
                )
                comparison_index = {
                    "schema_version": 1,
                    "targets": [
                        {
                            "candidate_id": "I1",
                            "observation_path": str(
                                candidate_root
                                / "comparison-observation.json"
                            ),
                            "observation_sha256":
                                compared["observation_sha256"],
                            "statuses": [
                                item["status"]
                                for item in compared["observations"]
                            ],
                            "contained_stages": [],
                        }
                    ],
                }
                comparison_index[
                    "comparison_index_sha256"
                ] = history_runtime.sha256(
                    b"history-runtime-comparison-index-v1\0"
                    + canonical(comparison_index)
                )
                history_runtime._publish_immutable(
                    artifact_root / "comparison-index.json",
                    canonical(comparison_index),
                )
            else:
                connection.close()
                connection = None
                command = canonical(
                    [str(FAKE_STAGE_AGENT)]
                ).decode("utf-8").strip()
                comparison_index = (
                    history_runtime._compare_frozen_targets_for_test(
                        test_authority=authority,
                        test_state_root=self.root,
                        db_path=self.database,
                        policy_path=self.policy_path,
                        batch_path=batch_path,
                        artifact_root=artifact_root,
                        selection_path=selection_path,
                        command_json=command,
                        authority=authority,
                        test_comparator_status=comparison_status,
                        test_expansion_pack_builder=(
                            self._expansion_failure_builder
                            if expansion_backend_failed
                            else None
                        ),
                    )
                )
            return {
                "policy": policy,
                "authority": authority,
                "batch": batch_path,
                "artifact_root": artifact_root,
                "selection": selection_path,
                "candidate": candidate,
                "comparison_index": comparison_index,
            }
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _expansion_failure_builder(
        conn,
        query,
        intent,
        policy,
        *,
        expansion_request=None,
        **kwargs,
    ):
        if expansion_request is not None:
            with mock.patch.object(
                history_retrieval,
                "_validate_generation_snapshot",
                return_value={
                    "valid": False,
                    "code": "manifest_mismatch",
                },
            ):
                return history_retrieval.build_pack(
                    conn,
                    query,
                    intent,
                    policy,
                    expansion_request=expansion_request,
                    **kwargs,
                )
        return history_retrieval.build_pack(
            conn,
            query,
            intent,
            policy,
            expansion_request=expansion_request,
            **kwargs,
        )

    def _assert_nonpermanent_status_rolls_back(
        self, *, retrieval_status="complete", comparison_status
    ):
        state = self._enforcement_round(
            retrieval_status=retrieval_status,
            comparison_status=comparison_status,
            contained=(retrieval_status == "complete"),
        )
        statuses = state["comparison_index"]["targets"][0][
            "statuses"
        ]
        expected = (
            retrieval_status
            if retrieval_status != "complete"
            else comparison_status
        )
        self.assertEqual(statuses, [expected, expected])
        self.assertEqual(
            len(
                state["comparison_index"]["targets"][0][
                    "contained_stages"
                ]
            ),
            (
                0
                if retrieval_status != "complete"
                else (
                    4
                    if comparison_status == "uncertain"
                    else 2
                )
            ),
        )
        summary_path = (
            state["artifact_root"]
            / "I1"
            / "history-summary.json"
        )
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.publish_candidate_summary(
                db_path=self.database,
                policy_path=self.policy_path,
                batch_path=state["batch"],
                artifact_root=state["artifact_root"],
                candidate_id="I1",
                output_path=summary_path,
                authority=state["authority"],
            )
        self.assertFalse(summary_path.exists())
        connection = history_store.connect(self.database)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM candidates"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_enforcement_complete_receipts_commit_one_atomic_delta(self):
        state = self._enforcement_round(contained=True)
        target = state["comparison_index"]["targets"][0]
        self.assertEqual(
            target["statuses"],
            ["complete_no_match", "complete_no_match"],
        )
        self.assertEqual(len(target["contained_stages"]), 2)
        summary_path = (
            state["artifact_root"]
            / "I1"
            / "history-summary.json"
        )
        summary_result = history_runtime.publish_candidate_summary(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            artifact_root=state["artifact_root"],
            candidate_id="I1",
            output_path=summary_path,
            authority=state["authority"],
        )
        self.assertEqual(
            summary_result["overall_status"],
            "complete_no_match",
        )
        summary = json.loads(
            summary_path.read_text(encoding="utf-8")
        )
        self.assertEqual(
            [item["intent"] for item in summary["receipts"]],
            ["duplicate_search", "failure_pattern_search"],
        )
        connection = history_store.connect(self.database)
        try:
            before = connection.execute(
                "SELECT count(*) FROM candidates"
            ).fetchone()[0]
        finally:
            connection.close()
        connection = history_store.connect(self.database)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM candidates"
                ).fetchone()[0],
                before,
            )
        finally:
            connection.close()

    def test_complete_match_summary_does_not_choose_review_verdict(self):
        state = self._enforcement_round(
            contained=True,
            comparison_status="complete_match",
        )
        target = state["comparison_index"]["targets"][0]
        self.assertEqual(
            target["statuses"],
            ["complete_match", "complete_match"],
        )
        self.assertEqual(len(target["contained_stages"]), 2)
        summary_path = (
            state["artifact_root"]
            / "I1"
            / "history-summary.json"
        )
        summary_result = history_runtime.publish_candidate_summary(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            artifact_root=state["artifact_root"],
            candidate_id="I1",
            output_path=summary_path,
            authority=state["authority"],
        )
        self.assertEqual(
            summary_result["overall_status"],
            "complete_match",
        )
        summary = json.loads(
            summary_path.read_text(encoding="utf-8")
        )
        self.assertTrue(
            any(
                relation["relation"] != "distinct"
                for receipt in summary["receipts"]
                for relation in receipt["relations"]
            )
        )
        self.assertNotIn(
            "verdict",
            summary,
            "history evidence must not choose the review verdict",
        )

    def test_enforcement_partial_rolls_back(self):
        self._assert_nonpermanent_status_rolls_back(
            retrieval_status="partial",
            comparison_status="partial",
        )

    def test_enforcement_backend_failed_rolls_back(self):
        self._assert_nonpermanent_status_rolls_back(
            retrieval_status="backend_failed",
            comparison_status="backend_failed",
        )

    def test_enforcement_budget_exceeded_rolls_back_without_comparator(self):
        self._assert_nonpermanent_status_rolls_back(
            retrieval_status="budget_exceeded",
            comparison_status="budget_exceeded",
        )

    def test_enforcement_uncertain_rolls_back(self):
        self._assert_nonpermanent_status_rolls_back(
            comparison_status="uncertain",
        )

    def test_enforcement_expansion_backend_failure_seals_abstention(
        self,
    ):
        state = self._enforcement_round(
            comparison_status="uncertain",
            contained=True,
            expansion_backend_failed=True,
        )
        target = state["comparison_index"]["targets"][0]
        self.assertEqual(
            target["statuses"],
            ["backend_failed", "backend_failed"],
        )
        self.assertEqual(
            len(target["contained_stages"]),
            2,
            "the comparator must not rerun after expansion retrieval fails",
        )
        observation = json.loads(
            pathlib.Path(target["observation_path"]).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            [
                [attempt["status"] for attempt in item["attempts"]]
                for item in observation["observations"]
            ],
            [
                ["uncertain", "backend_failed"],
                ["uncertain", "backend_failed"],
            ],
        )
        history_runtime.publish_round_summaries(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            selection_path=state["selection"],
            artifact_root=state["artifact_root"],
            authority=state["authority"],
        )
        research = history_runtime.materialize_research_views(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            selection_path=state["selection"],
            comparison_index_path=(
                state["artifact_root"] / "comparison-index.json"
            ),
            artifact_root=state["artifact_root"],
            output_root=self.root / "expansion-failure-research",
            authority=state["authority"],
        )
        self.assertEqual(research["eligible_order"], [])
        self.assertEqual(research["summaries"], [])
        prior_work = self.root / "expansion-failure-priorwork.md"
        prior_work.write_bytes(b"")
        resume_path = self.root / "expansion-failure-resume.json"
        sealed = history_runtime.seal_resume_state(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            selection_path=state["selection"],
            artifact_root=state["artifact_root"],
            comparison_index_path=(
                state["artifact_root"] / "comparison-index.json"
            ),
            prior_work_path=prior_work,
            output_path=resume_path,
            authority=state["authority"],
        )
        self.assertEqual(
            sealed["nonpermanent_observations"][0]["statuses"],
            ["backend_failed", "backend_failed"],
        )
        self.assertTrue(
            history_runtime.validate_resume_state(
                resume_path, authority=state["authority"]
            )
        )
        connection = history_store.connect(self.database)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM candidates",
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_enforcement_conflicting_evidence_rolls_back(self):
        self._assert_nonpermanent_status_rolls_back(
            comparison_status="conflicting_evidence",
        )

    def test_selection_requires_complete_build_all_observation(self):
        state = self._sealed_round()
        pathlib.Path(
            state["observation_root"]
            / "I1"
            / "build-observation.json"
        ).unlink()
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.verify_round_selection(state["selection"])

    def test_comparator_consumes_only_immutable_selection_membership(self):
        state = self._sealed_round(selected=("I2",))
        command = canonical([str(FAKE_STAGE_AGENT)]).decode(
            "utf-8"
        ).strip()
        result = history_runtime._compare_frozen_targets_for_test(
            test_authority=self.shadow_test_authority(),
            test_state_root=self.root,
            test_comparator_status="complete_no_match",
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            artifact_root=state["observation_root"],
            selection_path=state["selection"],
            command_json=command,
        )
        self.assertEqual(
            [item["candidate_id"] for item in result["targets"]],
            ["I2"],
        )
        self.assertFalse(
            (
                state["observation_root"]
                / "I1"
                / "comparison-observation.json"
            ).exists()
        )
        state["selector"].write_text(
            "I1\t1\tforged\tforged\tforged\tforged\n",
            encoding="utf-8",
        )
        history_runtime.verify_round_selection(state["selection"])
        selection = json.loads(
            state["selection"].read_text(encoding="utf-8")
        )
        pathlib.Path(
            selection["sources"]["selector"]["path"]
        ).write_text(
            "I1\t1\tforged\tforged\tforged\tforged\n",
            encoding="utf-8",
        )
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.verify_round_selection(state["selection"])

    def test_shadow_cannot_publish_any_history_summary(self):
        state = self._compared_round()
        output = (
            state["observation_root"]
            / "I2"
            / "history-summary.json"
        )
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.publish_candidate_summary(
                db_path=self.database,
                policy_path=self.policy_path,
                batch_path=state["batch"],
                artifact_root=state["observation_root"],
                candidate_id="I2",
                output_path=output,
            )
        self.assertFalse(output.exists())

    def test_selection_views_preserve_sealed_shortlist_priority(self):
        state = self._sealed_round(selected=("I2", "I1"))
        source_tsv = (
            pathlib.Path(state["batch"]).parent.parent
            / "coordinator-generated"
            / "ideas.all.tsv"
        )
        source_before = source_tsv.read_bytes()
        output = self.root / "selection-views"
        result = history_runtime.materialize_round_views(
            batch_path=state["batch"],
            selection_path=state["selection"],
            output_root=output,
        )
        self.assertEqual(result["shortlist_order"], ["I2", "I1"])
        self.assertEqual(
            (output / "ideas.tsv").read_text(encoding="utf-8"),
            "I2\tSecond bounded candidate.\t"
            "Evaluation and Diagnostics\n"
            "I1\tFirst bounded candidate.\t"
            "Evaluation and Diagnostics\n",
        )
        markdown = (output / "ideas.md").read_text(encoding="utf-8")
        self.assertLess(markdown.index("## I2"), markdown.index("## I1"))
        self.assertEqual(source_tsv.read_bytes(), source_before)

    def test_shadow_research_views_are_byte_identical_without_history(self):
        state = self._compared_round("uncertain")
        selection_root = self.root / "shadow-selection-views"
        selection_views = history_runtime.materialize_round_views(
            batch_path=state["batch"],
            selection_path=state["selection"],
            output_root=selection_root,
        )
        fake_summary = (
            state["observation_root"]
            / "I2"
            / "history-summary.json"
        )
        fake_summary.write_bytes(
            canonical({"schema_version": 1, "forged": True})
        )
        output_root = self.root / "shadow-research-views"
        research = history_runtime.materialize_research_views(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            selection_path=state["selection"],
            comparison_index_path=(
                state["observation_root"]
                / "comparison-index.json"
            ),
            artifact_root=state["observation_root"],
            output_root=output_root,
            authority=self.shadow_test_authority(),
        )
        self.assertEqual(research["mode"], "shadow")
        self.assertEqual(research["abstentions"], [])
        self.assertEqual(research["summaries"], [])
        self.assertEqual(
            research["eligible_order"],
            selection_views["shortlist_order"],
        )
        for name in ("ideas.tsv", "ideas.md"):
            self.assertEqual(
                (output_root / name).read_bytes(),
                (selection_root / name).read_bytes(),
            )
        self.assertFalse(
            (output_root / "history-summaries").exists()
        )
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.materialize_research_views(
                db_path=self.database,
                policy_path=self.policy_path,
                batch_path=state["batch"],
                selection_path=state["selection"],
                comparison_index_path=(
                    state["observation_root"]
                    / "comparison-index.json"
                ),
                artifact_root=state["observation_root"],
                output_root=output_root,
                authority=self.shadow_test_authority(),
            )

    def test_enforcement_research_views_exclude_nonpermanent_targets(self):
        state = self._enforcement_round(
            comparison_status="uncertain",
            contained=True,
        )
        history_runtime.publish_round_summaries(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            selection_path=state["selection"],
            artifact_root=state["artifact_root"],
            authority=state["authority"],
        )
        output_root = self.root / "enforcement-research-views"
        research = history_runtime.materialize_research_views(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            selection_path=state["selection"],
            comparison_index_path=(
                state["artifact_root"] / "comparison-index.json"
            ),
            artifact_root=state["artifact_root"],
            output_root=output_root,
            authority=state["authority"],
        )
        self.assertEqual(research["eligible_order"], [])
        self.assertEqual(research["summaries"], [])
        self.assertEqual(
            research["abstentions"],
            [
                {
                    "candidate_id": "I1",
                    "statuses": ["uncertain", "uncertain"],
                }
            ],
        )
        self.assertEqual((output_root / "ideas.tsv").read_bytes(), b"")
        self.assertEqual((output_root / "ideas.md").read_bytes(), b"")

    def test_enforcement_research_views_publish_verified_summaries_only(self):
        state = self._enforcement_round(
            contained=True,
            comparison_status="complete_match",
        )
        history_runtime.publish_round_summaries(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            selection_path=state["selection"],
            artifact_root=state["artifact_root"],
            authority=state["authority"],
        )
        output_root = self.root / "verified-research-views"
        research = history_runtime.materialize_research_views(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            selection_path=state["selection"],
            comparison_index_path=(
                state["artifact_root"] / "comparison-index.json"
            ),
            artifact_root=state["artifact_root"],
            output_root=output_root,
            authority=state["authority"],
        )
        self.assertEqual(research["eligible_order"], ["I1"])
        self.assertEqual(
            [item["candidate_id"] for item in research["summaries"]],
            ["I1"],
        )
        source_summary = (
            state["artifact_root"]
            / "I1"
            / "history-summary.json"
        )
        published_summary = (
            output_root
            / "history-summaries"
            / "I1.json"
        )
        self.assertEqual(
            published_summary.read_bytes(),
            source_summary.read_bytes(),
        )
        source_summary.chmod(0o600)
        tampered = json.loads(
            source_summary.read_text(encoding="utf-8")
        )
        tampered["aggregate_sha256"] = "0" * 64
        source_summary.write_bytes(canonical(tampered))
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.materialize_research_views(
                db_path=self.database,
                policy_path=self.policy_path,
                batch_path=state["batch"],
                selection_path=state["selection"],
                comparison_index_path=(
                    state["artifact_root"]
                    / "comparison-index.json"
                ),
                artifact_root=state["artifact_root"],
                output_root=self.root / "tampered-summary-research",
                authority=state["authority"],
            )

    def test_complete_no_match_research_has_no_history_prompt(self):
        state = self._enforcement_round(contained=True)
        history_runtime.publish_round_summaries(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            selection_path=state["selection"],
            artifact_root=state["artifact_root"],
            authority=state["authority"],
        )
        output_root = self.root / "no-match-research-views"
        research = history_runtime.materialize_research_views(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            selection_path=state["selection"],
            comparison_index_path=(
                state["artifact_root"] / "comparison-index.json"
            ),
            artifact_root=state["artifact_root"],
            output_root=output_root,
            authority=state["authority"],
        )
        self.assertEqual(research["eligible_order"], ["I1"])
        self.assertEqual(research["summaries"], [])
        self.assertFalse(
            (output_root / "history-summaries").exists()
        )

    def test_research_views_reject_comparison_observation_tamper(self):
        state = self._compared_round()
        observation_path = (
            state["observation_root"]
            / "I2"
            / "comparison-observation.json"
        )
        observation = json.loads(
            observation_path.read_text(encoding="utf-8")
        )
        observation_path.chmod(0o600)
        observation["observations"][0]["status"] = "uncertain"
        observation_path.write_bytes(canonical(observation))
        output_root = self.root / "tampered-research-views"
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.materialize_research_views(
                db_path=self.database,
                policy_path=self.policy_path,
                batch_path=state["batch"],
                selection_path=state["selection"],
                comparison_index_path=(
                    state["observation_root"]
                    / "comparison-index.json"
                ),
                artifact_root=state["observation_root"],
                output_root=output_root,
                authority=self.shadow_test_authority(),
            )
        self.assertFalse(output_root.exists())

    def test_legacy_commit_api_is_absent(self):
        self.assertFalse(
            hasattr(history_runtime, "commit_round_delta")
        )

    def test_sealed_review_matrix_aggregation_and_commit_chain(self):
        state = self._compared_round()
        research_root = self.root / "decision-research-view"
        research = history_runtime.materialize_research_views(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            selection_path=state["selection"],
            comparison_index_path=(
                state["observation_root"]
                / "comparison-index.json"
            ),
            artifact_root=state["observation_root"],
            output_root=research_root,
            authority=self.shadow_test_authority(),
        )
        prior_work = self.root / "decision-priorwork.md"
        prior_work.write_text(
            "## I2\n"
            "Search Terms: bounded evaluation\n"
            "- Query: https://api.semanticscholar.org/graph/v1/"
            "paper/search?query=bounded-evaluation\n"
            "Nearest Work:\n"
            "- One | https://example.com/one | Adjacent | Distinct.\n"
            "- Two | https://example.com/two | Adjacent | Distinct.\n"
            "- Three | https://example.com/three | Adjacent | Distinct.\n"
            "- Four | https://example.com/four | Adjacent | Distinct.\n"
            "- Five | https://example.com/five | Adjacent | Distinct.\n"
            "Strongest Counterexample: The closest result is distinct.\n"
            "Overlap: low\n"
            "Papers Read: 5\n"
            "arXiv ID Check: yes\n",
            encoding="utf-8",
        )
        command = canonical([str(FAKE_STAGE_AGENT)]).decode(
            "utf-8"
        ).strip()
        plan_path = self.root / "review-plan.json"
        plan = history_runtime.seal_round_review_plan(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            selection_path=state["selection"],
            comparison_index_path=(
                state["observation_root"]
                / "comparison-index.json"
            ),
            artifact_root=state["observation_root"],
            prior_work_path=prior_work,
            review_contract_path=(
                ROOT / "history" / "review-contract-v1.md"
            ),
            reviewer_commands={"1": command, "2": command},
            round_date="2026-07-24",
            min_read=5,
            axiom_min_cracks=2,
            output_path=plan_path,
        )
        self.assertEqual(
            [item["seat_id"] for item in plan["reviewer_seats"]],
            ["1", "2"],
        )
        self.assertEqual(plan["targets"][0]["planned_outcome"], "review")
        self.assertIsNone(
            plan["targets"][0]["mounted_history_summary"]
        )
        index_path = self.root / "review-index.json"
        index = history_runtime._run_review_matrix_for_test(
            test_authority=self.shadow_test_authority(),
            test_state_root=self.root,
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            review_plan_path=plan_path,
            reviewer_commands={"1": command, "2": command},
            stage_root=self.root / "review-stages",
            output_path=index_path,
            test_review_verdict="strong-accept",
        )
        self.assertEqual(len(index["entries"]), 2)
        self.assertTrue(
            history_runtime.verify_review_matrix(
                db_path=self.database,
                policy_path=self.policy_path,
                batch_path=state["batch"],
                review_plan_path=plan_path,
                review_index_path=index_path,
            )
        )
        aggregation_path = self.root / "round-aggregation.json"
        aggregation = history_runtime.build_round_aggregation(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            review_plan_path=plan_path,
            review_index_path=index_path,
            output_path=aggregation_path,
        )
        self.assertEqual(len(aggregation["ledger_rows"]), 1)
        self.assertEqual(
            aggregation["ledger_rows"][0].split("\t")[4],
            "strong-accept",
        )
        report_root = self.root / "decision-report-view"
        report = history_runtime.materialize_report_views(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            research_view_path=(
                research_root / "research-view.json"
            ),
            review_plan_path=plan_path,
            review_index_path=index_path,
            aggregation_path=aggregation_path,
            output_root=report_root,
            round_number=3,
        )
        self.assertEqual(
            report["research_view_sha256"],
            research["research_view_sha256"],
        )
        self.assertEqual(report["accepted_count"], 1)
        self.assertEqual(report["rejected_count"], 0)
        self.assertEqual(
            (report_root / "accepted.tsv").read_text(
                encoding="utf-8"
            ),
            "I2\tSecond bounded candidate.\n",
        )
        self.assertEqual(
            (report_root / "rejects.tsv").read_bytes(), b""
        )
        self.assertIn(
            "## I2",
            (report_root / "ideas.md").read_text(
                encoding="utf-8"
            ),
        )
        self.assertTrue(
            (report_root / "rev" / "1" / "review.md")
            .read_text(encoding="utf-8")
            .startswith("## I2\n")
        )
        self.assertEqual(
            (report_root / "meta.txt").read_text(
                encoding="utf-8"
            ),
            "Rounds Attempted: 3\n"
            "Review Date: 2026-07-24\n"
            "Reviewers: 2\n",
        )
        with self.assertRaises(
            history_runtime.RuntimeContractError
        ):
            history_runtime.materialize_report_views(
                db_path=self.database,
                policy_path=self.policy_path,
                batch_path=state["batch"],
                research_view_path=(
                    research_root / "research-view.json"
                ),
                review_plan_path=plan_path,
                review_index_path=index_path,
                aggregation_path=aggregation_path,
                output_root=report_root,
                round_number=3,
            )
        authority = history_runtime.validate_runtime_mode(self.policy)
        commit_arguments = {
            "db_path": self.database,
            "policy_path": self.policy_path,
            "batch_path": state["batch"],
            "selection_path": state["selection"],
            "comparison_index_path": (
                state["observation_root"]
                / "comparison-index.json"
            ),
            "review_plan_path": plan_path,
            "review_index_path": index_path,
            "aggregation_path": aggregation_path,
            "authority": authority,
        }
        first = history_runtime.commit_round(**commit_arguments)
        second = history_runtime.commit_round(**commit_arguments)
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["candidate_ids"], second["candidate_ids"])

    def test_review_plan_uses_frozen_sources_and_rejects_snapshot_tamper(self):
        state = self._compared_round()
        prior_work = self.root / "original-prior-work.md"
        prior_work.write_text(
            "## I2\nPapers Read: 5\nOverlap: low\n",
            encoding="utf-8",
        )
        review_contract = self.root / "original-review-contract.md"
        review_contract.write_bytes(
            (
                ROOT / "history" / "review-contract-v1.md"
            ).read_bytes()
        )
        sealed = self._seal_review_plan(
            state,
            stem="frozen-source",
            prior_work_path=prior_work,
            review_contract_path=review_contract,
        )
        prior_work.unlink()
        review_contract.write_text(
            "mutated original contract\n",
            encoding="utf-8",
        )
        history_runtime.verify_round_review_plan(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            review_plan_path=sealed["plan_path"],
        )
        frozen_contract = pathlib.Path(
            sealed["plan"]["review_contract"]["path"]
        )
        frozen_contract.chmod(0o600)
        frozen_contract.write_bytes(
            frozen_contract.read_bytes() + b"x"
        )
        with self.assertRaisesRegex(
            history_runtime.RuntimeContractError,
            "source changed",
        ):
            history_runtime.verify_round_review_plan(
                db_path=self.database,
                policy_path=self.policy_path,
                batch_path=state["batch"],
                review_plan_path=sealed["plan_path"],
            )

    def test_selection_rejects_theme_outside_sealed_brief(self):
        unknown = (
            {
                "candidate_id": "I1",
                "story": "Candidate with an unsealed theme.",
                "theme": "Unsealed Theme",
                "falsification": (
                    "Reject if the bounded estimate misses its "
                    "held-out threshold."
                ),
            },
        )
        with self.assertRaisesRegex(
            history_runtime.RuntimeContractError,
            "outside the sealed inventory",
        ):
            self._sealed_round(
                selected=("I1",),
                candidate_specs=unknown,
            )

    def test_mixed_kills_and_reordered_shortlist_commit_in_sealed_order(self):
        specs = tuple(
            {
                "candidate_id": f"I{index}",
                "story": f"Ordered candidate {index}.",
                "theme": "Evaluation and Diagnostics",
                "falsification": (
                    "Reject if the bounded estimate misses its "
                    "held-out threshold."
                ),
            }
            for index in range(1, 5)
        )
        state = self._compared_round(
            selected=("I4", "I2"),
            killed=("I1", "I3"),
            candidate_specs=specs,
        )
        chain = self._review_chain(
            state,
            stem="mixed-order",
        )
        expected_ids = ["I1", "I3", "I4", "I2"]
        expected_stories = [
            "Ordered candidate 1.",
            "Ordered candidate 3.",
            "Ordered candidate 4.",
            "Ordered candidate 2.",
        ]
        self.assertEqual(
            chain["plan"]["commit_order"], expected_ids
        )
        self.assertEqual(
            [
                row.split("\t")[3]
                for row in chain["aggregation"]["ledger_rows"]
            ],
            expected_stories,
        )
        authority = history_runtime.validate_runtime_mode(
            self.policy
        )
        committed = history_runtime.commit_round(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            selection_path=state["selection"],
            comparison_index_path=(
                state["observation_root"]
                / "comparison-index.json"
            ),
            review_plan_path=chain["plan_path"],
            review_index_path=chain["index_path"],
            aggregation_path=chain["aggregation_path"],
            authority=authority,
        )
        self.assertEqual(len(committed["candidate_ids"]), 4)
        connection = history_store.connect(self.database)
        try:
            rows = connection.execute(
                "SELECT story FROM candidates "
                "ORDER BY source_sequence DESC LIMIT 4"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(
            [row["story"] for row in reversed(rows)],
            expected_stories,
        )

    def test_review_matrix_rejects_compact_review_and_ballot_tamper(self):
        state = self._compared_round()
        chain = self._review_chain(
            state,
            stem="review-tamper",
        )
        entry = chain["index"]["entries"][0]
        prepared = entry["prepared"]
        attacks = {
            "compact-review": (
                pathlib.Path(
                    prepared["output_paths"]["review.md"]
                ),
                lambda raw: raw.replace(
                    b"History: unavailable\n", b""
                ),
            ),
            "ballot": (
                pathlib.Path(
                    prepared["output_paths"]["verdict.tsv"]
                ),
                lambda raw: raw.replace(
                    b"strong-accept", b"reject", 1
                ),
            ),
        }
        for label, (path, mutate) in attacks.items():
            with self.subTest(label=label):
                original = path.read_bytes()
                path.chmod(0o600)
                path.write_bytes(mutate(original))
                with self.assertRaises(
                    history_runtime.RuntimeContractError
                ):
                    history_runtime.verify_review_matrix(
                        db_path=self.database,
                        policy_path=self.policy_path,
                        batch_path=state["batch"],
                        review_plan_path=chain["plan_path"],
                        review_index_path=chain["index_path"],
                    )
                path.write_bytes(original)
                self.assertTrue(
                    history_runtime.verify_review_matrix(
                        db_path=self.database,
                        policy_path=self.policy_path,
                        batch_path=state["batch"],
                        review_plan_path=chain["plan_path"],
                        review_index_path=chain["index_path"],
                    )
                )

    def test_one_byte_falsification_has_exact_evidence_incomplete_row(self):
        story = "Single-byte falsification candidate."
        state = self._compared_round(
            selected=("I1",),
            candidate_specs=(
                {
                    "candidate_id": "I1",
                    "story": story,
                    "theme": "Evaluation and Diagnostics",
                    "falsification": "x",
                },
            ),
        )
        chain = self._review_chain(
            state,
            stem="one-byte-falsification",
        )
        reason = (
            "Unanimous SA failed a mechanical gate: "
            "papers read < 5, missing research block, missing "
            "falsification experiment, incomplete review, or "
            "insufficient supported crack evidence"
        )
        self.assertEqual(
            chain["aggregation"]["ledger_rows"],
            [
                "2026-07-24\thunt\t"
                "Evaluation and Diagnostics\t"
                f"{story}\treject\t{reason}\tlow\t"
                "evidence-incomplete"
            ],
        )
        self.assertEqual(
            len(chain["aggregation"]["near_sa_observations"]),
            1,
        )

    def test_existing_same_story_suppresses_near_sa_observation(self):
        story = "Repeated evidence-incomplete candidate."
        state = self._compared_round(
            selected=("I1",),
            candidate_specs=(
                {
                    "candidate_id": "I1",
                    "story": story,
                    "theme": "Evaluation and Diagnostics",
                    "falsification": "x",
                },
            ),
        )
        chain = self._review_chain(
            state,
            stem="same-story",
            reviewer_count=1,
        )
        self.assertEqual(
            len(chain["aggregation"]["near_sa_observations"]),
            1,
        )
        connection = history_store.connect(self.database)
        try:
            history_store.append_rows(
                connection,
                [
                    "2026-07-24\thunt\t"
                    "Evaluation and Diagnostics\t"
                    f"{story}\treject\tPrior exact story.\thigh\t"
                    "novelty-dead"
                ],
                {"source": "same-story-fixture"},
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM candidates WHERE story = ?",
                    (story,),
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()
        aggregation = history_runtime.build_round_aggregation(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            review_plan_path=chain["plan_path"],
            review_index_path=chain["index_path"],
            output_path=(
                self.root
                / "same-story-after-prior-aggregation.json"
            ),
        )
        self.assertEqual(
            aggregation["near_sa_observations"], []
        )

    def test_enforcement_commit_replay_survives_projection_advance(self):
        state = self._enforcement_round(contained=True)
        summary_path = (
            state["artifact_root"]
            / "I1"
            / "history-summary.json"
        )
        history_runtime.publish_candidate_summary(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            artifact_root=state["artifact_root"],
            candidate_id="I1",
            output_path=summary_path,
            authority=state["authority"],
        )
        chain = self._review_chain(
            state,
            stem="enforcement-replay",
            authority=state["authority"],
        )
        arguments = {
            "db_path": self.database,
            "policy_path": self.policy_path,
            "batch_path": state["batch"],
            "selection_path": state["selection"],
            "comparison_index_path": (
                state["artifact_root"]
                / "comparison-index.json"
            ),
            "review_plan_path": chain["plan_path"],
            "review_index_path": chain["index_path"],
            "aggregation_path": chain["aggregation_path"],
            "authority": state["authority"],
        }
        connection = history_store.connect(self.database)
        try:
            before_watermark = connection.execute(
                "SELECT COALESCE(MAX(source_sequence), 0) "
                "FROM candidates"
            ).fetchone()[0]
            before_generation = (
                history_projection.current_index_generation(
                    connection
                )
            )
        finally:
            connection.close()
        first = history_runtime.commit_round(**arguments)
        self.assertFalse(first["replayed"])
        connection = history_store.connect(self.database)
        try:
            history_projection.rebuild(
                connection, state["policy"]
            )
            after_watermark = connection.execute(
                "SELECT COALESCE(MAX(source_sequence), 0) "
                "FROM candidates"
            ).fetchone()[0]
            after_generation = (
                history_projection.current_index_generation(
                    connection
                )
            )
        finally:
            connection.close()
        self.assertGreater(after_watermark, before_watermark)
        self.assertGreater(after_generation, before_generation)
        second = history_runtime.commit_round(**arguments)
        self.assertTrue(second["replayed"])
        self.assertEqual(
            second["candidate_ids"], first["candidate_ids"]
        )

    def test_resume_accepts_same_canonical_direction(self):
        state, resume_path = self.directed_resume()
        identity = state["direction_identity"]
        self.assertTrue(
            history_runtime.validate_resume_state(
                resume_path, expected_direction=identity
            )
        )

    def test_resume_rejects_changed_added_and_removed_direction(self):
        directed_state, directed_resume = self.directed_resume()
        _, undirected_resume = self.undirected_resume()
        cases = [
            (directed_resume, None),
            (
                undirected_resume,
                directed_state["direction_identity"],
            ),
            (
                directed_resume,
                {
                    "direction_id":
                        "dynamic-spatial-memory-vla-v1",
                    "sha256": "0" * 64,
                },
            ),
        ]
        for resume_path, expected in cases:
            with self.subTest(
                resume_path=resume_path, expected=expected
            ):
                with self.assertRaises(
                    history_runtime.RuntimeContractError
                ):
                    history_runtime.validate_resume_state(
                        resume_path,
                        expected_direction=expected,
                    )

    def test_resume_omitted_expected_direction_preserves_api_behavior(self):
        _, resume_path = self.directed_resume()
        self.assertTrue(
            history_runtime.validate_resume_state(resume_path)
        )

    def test_schema_v1_resume_chain_is_fenced_as_undirected(self):
        state, resume_path = self._resume_round(
            stem="legacy-v1",
            legacy_v1=True,
        )
        self.assertIsNone(state["direction_identity"])
        self.assertTrue(
            history_runtime.validate_resume_state(
                resume_path, expected_direction=None
            )
        )
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.validate_resume_state(
                resume_path,
                expected_direction={
                    "direction_id":
                        "dynamic-spatial-memory-vla-v1",
                    "sha256": (
                        "50bbf68a8ee20f2635194abab2a41ee702d4ec227"
                        "b5277bf1bba9f463fee0d85"
                    ),
                },
            )

    def test_resume_replays_durable_artifacts_not_only_tuple_fields(self):
        state = self._compared_round()
        prior_work = self.root / "priorwork.md"
        prior_work.write_text(
            "## I2\nPapers Read: 5\nOverlap: low\n",
            encoding="utf-8",
        )
        resume_path = self.root / "resume-state.json"
        history_runtime.seal_resume_state(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            selection_path=state["selection"],
            artifact_root=state["observation_root"],
            comparison_index_path=(
                state["observation_root"]
                / "comparison-index.json"
            ),
            prior_work_path=prior_work,
            output_path=resume_path,
        )
        self.assertTrue(
            history_runtime.validate_resume_state(resume_path)
        )
        prior_work.write_text(
            "## I2\nPapers Read: forged\n",
            encoding="utf-8",
        )
        sealed = history_runtime.validate_resume_state(
            resume_path
        )
        frozen_prior = pathlib.Path(
            sealed["prior_work"]["path"]
        )
        self.assertNotEqual(
            frozen_prior.resolve(), prior_work.resolve()
        )
        frozen_prior.chmod(0o600)
        frozen_prior.write_text(
            "## I2\nPapers Read: forged\n",
            encoding="utf-8",
        )
        with self.assertRaises(
            history_runtime.RuntimeContractError
        ):
            history_runtime.validate_resume_state(resume_path)

    def test_resume_attempt_receipt_binds_validated_resume(self):
        state = self._compared_round()
        prior_work = self.root / "resume-attempt-priorwork.md"
        prior_work.write_text(
            "## I2\nPapers Read: 5\nOverlap: low\n",
            encoding="utf-8",
        )
        resume_path = self.root / "resume-attempt-state.json"
        resume = history_runtime.seal_resume_state(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            selection_path=state["selection"],
            artifact_root=state["observation_root"],
            comparison_index_path=(
                state["observation_root"]
                / "comparison-index.json"
            ),
            prior_work_path=prior_work,
            output_path=resume_path,
        )
        output = self.root / "resume-attempt.json"
        receipt = history_runtime.seal_resume_attempt(
            resume_path=resume_path,
            run_id="run-2",
            resumed_from_run_id="run-1",
            prior_archive_path=None,
            output_path=output,
        )
        self.assertEqual(
            set(receipt),
            {
                "schema_version",
                "run_id",
                "resumed_from_run_id",
                "resume_state_sha256",
                "prior_failure_archive",
                "resume_attempt_sha256",
            },
        )
        self.assertEqual(receipt["run_id"], "run-2")
        self.assertEqual(
            receipt["resumed_from_run_id"], "run-1"
        )
        self.assertEqual(
            receipt["resume_state_sha256"],
            resume["resume_sha256"],
        )
        self.assertIsNone(receipt["prior_failure_archive"])
        self.assertEqual(output.read_bytes(), canonical(receipt))
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.seal_resume_attempt(
                resume_path=resume_path,
                run_id="run-1",
                resumed_from_run_id="run-1",
                prior_archive_path=None,
                output_path=self.root / "same-run-attempt.json",
            )

    def test_resume_requires_one_contained_stage_per_complete_attempt(self):
        state = self._compared_round()
        index_path = (
            state["observation_root"] / "comparison-index.json"
        )
        index = json.loads(index_path.read_text(encoding="utf-8"))
        self.assertEqual(
            len(index["targets"][0]["contained_stages"]),
            2,
        )
        index["targets"][0]["contained_stages"].pop()
        index.pop("comparison_index_sha256")
        index["comparison_index_sha256"] = history_runtime.sha256(
            b"history-runtime-comparison-index-v1\0"
            + canonical(index)
        )
        index_path.write_bytes(canonical(index))
        prior_work = self.root / "resume-coverage-priorwork.md"
        prior_work.write_text(
            "## I2\nPapers Read: 5\nOverlap: low\n",
            encoding="utf-8",
        )
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.seal_resume_state(
                db_path=self.database,
                policy_path=self.policy_path,
                batch_path=state["batch"],
                selection_path=state["selection"],
                artifact_root=state["observation_root"],
                comparison_index_path=index_path,
                prior_work_path=prior_work,
                output_path=self.root / "invalid-resume.json",
            )

    def test_shadow_resume_binds_nonpermanent_observation_to_generation(self):
        state = self._compared_round("uncertain")
        prior_work = self.root / "uncertain-resume-priorwork.md"
        prior_work.write_text(
            "## I2\nPapers Read: 5\nOverlap: low\n",
            encoding="utf-8",
        )
        resume_path = self.root / "uncertain-resume.json"
        sealed = history_runtime.seal_resume_state(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            selection_path=state["selection"],
            artifact_root=state["observation_root"],
            comparison_index_path=(
                state["observation_root"]
                / "comparison-index.json"
            ),
            prior_work_path=prior_work,
            output_path=resume_path,
        )
        self.assertEqual(
            [item["intent"] for item in sealed["bindings"]],
            ["duplicate_search", "failure_pattern_search"],
        )
        self.assertTrue(
            history_runtime.validate_resume_state(resume_path)
        )
        connection = history_store.connect(self.database)
        try:
            history_store.append_rows(
                connection,
                [
                    "2026-07-24\thunt\tEvaluation and Diagnostics\t"
                    "Advance the resume generation.\treject\t"
                    "Bounded stale-resume test.\thigh\tnovelty-dead"
                ],
                {"run_id": "resume-generation-advance"},
            )
            history_projection.rebuild(connection, self.policy)
        finally:
            connection.close()
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.validate_resume_state(resume_path)

    def test_enforcement_resume_requires_exact_authority_and_summary(self):
        state = self._enforcement_round(contained=True)
        summary_path = (
            state["artifact_root"]
            / "I1"
            / "history-summary.json"
        )
        history_runtime.publish_candidate_summary(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            artifact_root=state["artifact_root"],
            candidate_id="I1",
            output_path=summary_path,
            authority=state["authority"],
        )
        prior_work = self.root / "enforcement-priorwork.md"
        prior_work.write_text(
            "## I1\nPapers Read: 5\nOverlap: low\n",
            encoding="utf-8",
        )
        resume_path = self.root / "enforcement-resume.json"
        resume_values = {
            "db_path": self.database,
            "policy_path": self.policy_path,
            "batch_path": state["batch"],
            "selection_path": state["selection"],
            "artifact_root": state["artifact_root"],
            "comparison_index_path": (
                state["artifact_root"] / "comparison-index.json"
            ),
            "prior_work_path": prior_work,
        }
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.seal_resume_state(
                output_path=resume_path,
                **resume_values,
            )
        self.assertFalse(resume_path.exists())
        sealed = history_runtime.seal_resume_state(
            output_path=resume_path,
            authority=state["authority"],
            **resume_values,
        )
        self.assertEqual(
            sealed["runtime_authority"]["mode"],
            "enforcement",
        )
        self.assertIsNotNone(
            sealed["runtime_authority"]["capability_sha256"]
        )
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.validate_resume_state(resume_path)
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.validate_resume_state(
                resume_path,
                authority={
                    "mode": "enforcement",
                    "policy_sha256": (
                        sealed["runtime_authority"][
                            "policy_sha256"
                        ]
                    ),
                },
            )
        self.assertTrue(
            history_runtime.validate_resume_state(
                resume_path, authority=state["authority"]
            )
        )
        summary = json.loads(
            summary_path.read_text(encoding="utf-8")
        )
        summary["aggregate_sha256"] = "0" * 64
        summary_path.write_bytes(canonical(summary))
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.validate_resume_state(
                resume_path, authority=state["authority"]
            )

    def test_enforcement_nonpermanent_resume_binds_abstention_without_summary(
        self,
    ):
        state = self._enforcement_round(
            comparison_status="uncertain",
            contained=True,
        )
        prior_work = self.root / "abstention-priorwork.md"
        prior_work.write_bytes(b"")
        resume_path = self.root / "abstention-resume.json"
        sealed = history_runtime.seal_resume_state(
            db_path=self.database,
            policy_path=self.policy_path,
            batch_path=state["batch"],
            selection_path=state["selection"],
            artifact_root=state["artifact_root"],
            comparison_index_path=(
                state["artifact_root"] / "comparison-index.json"
            ),
            prior_work_path=prior_work,
            output_path=resume_path,
            authority=state["authority"],
        )
        self.assertEqual(sealed["summaries"], [])
        self.assertEqual(
            sealed["nonpermanent_observations"],
            [
                {
                    "candidate_id": "I1",
                    "observation_sha256": (
                        state["comparison_index"]["targets"][0][
                            "observation_sha256"
                        ]
                    ),
                    "statuses": ["uncertain", "uncertain"],
                }
            ],
        )
        self.assertFalse(
            (
                state["artifact_root"]
                / "I1"
                / "history-summary.json"
            ).exists()
        )
        self.assertTrue(
            history_runtime.validate_resume_state(
                resume_path, authority=state["authority"]
            )
        )
        first_pack = (
            state["artifact_root"]
            / "I1"
            / "duplicate_search"
            / "retrieval-pack.json"
        )
        first_pack.chmod(0o600)
        first_pack.write_bytes(b"{}\n")
        with self.assertRaises(
            history_runtime.RuntimeContractError
        ):
            history_runtime.validate_resume_state(
                resume_path, authority=state["authority"]
            )

    def test_plain_dict_cannot_forge_enforcement_authority(self):
        policy = copy.deepcopy(self.policy)
        policy["mode"] = "enforcement"
        self.assertFalse(
            hasattr(history_runtime, "_issue_runtime_authority")
        )
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime._validated_runtime_authority(
                policy,
                {
                    "mode": "enforcement",
                    "policy_sha256": history_runtime.sha256(
                        canonical(policy)
                    ),
                    "scope": "production",
                    "trust_root_sha256": "1" * 64,
                },
                state_paths=(self.database,),
            )


class AtomicCommitContract(RuntimeFixture):
    def test_rows_and_near_sa_observations_commit_in_one_transaction(self):
        conn = self.connect_indexed()
        try:
            row = (
                "2026-07-24\thunt\tEvaluation and Diagnostics\t"
                "A transactional runtime result.\taccept-w-rev\t"
                "One bounded repair remains.\tlow\tdesign-fixable"
            )
            result = history_store.append_rows(
                conn,
                [row],
                {"run_id": "runtime-smoke"},
                near_sa_observations=[
                    {
                        "row_index": 0,
                        "sa_votes": 1,
                        "vote_vector": "2,1,1",
                        "overlap": "low",
                        "category": "design-fixable",
                        "reason": "runtime-smoke/I1",
                        "observed_at": "2026-07-24",
                    }
                ],
            )
            self.assertEqual(result["appended"], 1)
            self.assertEqual(result["near_sa_observations"], 1)
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM near_sa_observations "
                    "WHERE candidate_id = ?",
                    (result["candidate_ids"][0],),
                ).fetchone()[0],
                1,
            )
        finally:
            conn.close()

    def test_invalid_observation_rolls_back_candidate_and_outboxes(self):
        conn = self.connect_indexed()
        try:
            before = {
                table: conn.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0]
                for table in (
                    "candidates",
                    "near_sa_observations",
                    "search_projection_outbox",
                    "ledger_projection_outbox",
                )
            }
            with self.assertRaises(ValueError):
                history_store.append_rows(
                    conn,
                    [
                        "2026-07-24\thunt\tEvaluation and Diagnostics\t"
                        "Rollback candidate.\treject\tReason.\tlow\t"
                        "design-fixable"
                    ],
                    {"run_id": "runtime-smoke"},
                    near_sa_observations=[
                        {
                            "row_index": 9,
                            "sa_votes": 1,
                            "vote_vector": "2",
                            "overlap": "low",
                            "category": "design-fixable",
                            "reason": "invalid",
                            "observed_at": "2026-07-24",
                        }
                    ],
                )
            after = {
                table: conn.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0]
                for table in before
            }
            self.assertEqual(after, before)
        finally:
            conn.close()

    def test_contradictory_near_sa_fact_rolls_back_whole_append(self):
        invalid_observations = (
            {
                "row_index": 0,
                "sa_votes": 2,
                "vote_vector": "2,1,1",
                "overlap": "low",
                "category": "design-fixable",
                "reason": "count mismatch",
                "observed_at": "2026-07-24",
            },
            {
                "row_index": 0,
                "sa_votes": 1,
                "vote_vector": "2,9,1",
                "overlap": "low",
                "category": "design-fixable",
                "reason": "vocabulary mismatch",
                "observed_at": "2026-07-24",
            },
            {
                "row_index": 0,
                "sa_votes": 1,
                "vote_vector": "2,1,1",
                "overlap": "low",
                "category": "novelty-dead",
                "reason": "ineligible category",
                "observed_at": "2026-07-24",
            },
        )
        for observation in invalid_observations:
            with self.subTest(reason=observation["reason"]):
                conn = self.connect_indexed()
                try:
                    before = conn.execute(
                        "SELECT count(*) FROM candidates"
                    ).fetchone()[0]
                    with self.assertRaises(ValueError):
                        history_store.append_rows(
                            conn,
                            [
                                "2026-07-24\thunt\t"
                                "Evaluation and Diagnostics\t"
                                "Contradictory fact candidate.\t"
                                "accept-w-rev\tReason.\tlow\t"
                                "design-fixable"
                            ],
                            {"run_id": "runtime-smoke"},
                            near_sa_observations=[observation],
                        )
                    self.assertEqual(
                        conn.execute(
                            "SELECT count(*) FROM candidates"
                        ).fetchone()[0],
                        before,
                    )
                finally:
                    conn.close()


class CliContract(RuntimeFixture):
    @staticmethod
    def _direction_identity():
        return {
            "direction_id": "dynamic-spatial-memory-vla-v1",
            "sha256": (
                "50bbf68a8ee20f2635194abab2a41ee702d4ec227b5277"
                "bf1bba9f463fee0d85"
            ),
        }

    def test_freeze_cli_loads_canonical_direction_contract(self):
        brief_path = self.root / "cli-brief.json"
        brief_path.write_bytes(canonical({"schema_version": 1}))
        direction_path = self.root / "cli-direction.json"
        contract = json.loads(
            (
                ROOT
                / "directions"
                / "dynamic-spatial-memory-vla-v1.json"
            ).read_text(encoding="utf-8")
        )
        direction_path.write_bytes(canonical(contract))
        result = {
            "schema_version": 2,
            "direction": self._direction_identity(),
        }
        with (
            mock.patch.object(
                history_runtime,
                "freeze_candidate_batch",
                return_value=result,
            ) as freeze,
            mock.patch("builtins.print"),
        ):
            self.assertEqual(
                history_runtime._main(
                    [
                        "freeze-batch",
                        "--tsv",
                        str(self.root / "ideas.tsv"),
                        "--markdown",
                        str(self.root / "ideas.md"),
                        "--output-root",
                        str(self.root / "batch"),
                        "--brief",
                        str(brief_path),
                        "--direction",
                        str(direction_path),
                    ]
                ),
                0,
            )
        self.assertEqual(
            freeze.call_args.kwargs["direction_contract"],
            contract,
        )

    def test_validate_resume_cli_loads_expected_direction_identity(self):
        expected_path = self.root / "expected-direction.json"
        identity = self._direction_identity()
        expected_path.write_bytes(canonical(identity))
        with (
            mock.patch.object(
                history_runtime,
                "_cli_runtime_authority",
                return_value=None,
            ),
            mock.patch.object(
                history_runtime,
                "validate_resume_state",
                return_value={"schema_version": 1},
            ) as validate,
            mock.patch("builtins.print"),
        ):
            self.assertEqual(
                history_runtime._main(
                    [
                        "validate-resume",
                        "--policy",
                        str(self.policy_path),
                        "--resume",
                        str(self.root / "resume.json"),
                        "--expected-direction",
                        str(expected_path),
                    ]
                ),
                0,
            )
        self.assertEqual(
            validate.call_args.kwargs["expected_direction"],
            identity,
        )

    def test_validate_resume_cli_rejects_noncanonical_or_malformed_identity(
        self,
    ):
        identity = self._direction_identity()
        noncanonical = self.root / "noncanonical-identity.json"
        noncanonical.write_text(
            json.dumps(identity, indent=2),
            encoding="utf-8",
        )
        malformed = self.root / "malformed-identity.json"
        malformed.write_bytes(
            canonical(
                {
                    "direction_id":
                        "dynamic-spatial-memory-vla-v1",
                    "sha256": "0" * 63,
                }
            )
        )
        for identity_path in (noncanonical, malformed):
            with self.subTest(identity_path=identity_path), (
                mock.patch.object(
                    history_runtime,
                    "_cli_runtime_authority",
                    return_value=None,
                )
            ), mock.patch.object(
                history_runtime,
                "validate_resume_state",
            ) as validate, mock.patch("builtins.print"):
                with self.assertRaises(
                    history_runtime.RuntimeContractError
                ):
                    history_runtime._main(
                        [
                            "validate-resume",
                            "--policy",
                            str(self.policy_path),
                            "--resume",
                            str(self.root / "resume.json"),
                            "--expected-direction",
                            str(identity_path),
                        ]
                    )
                validate.assert_not_called()

    def test_runtime_has_no_duplicate_top_level_functions(self):
        tree = ast.parse(
            (ROOT / "lib" / "history_runtime.py").read_text(
                encoding="utf-8"
            )
        )
        names = [
            node.name
            for node in tree.body
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            )
        ]
        duplicates = sorted(
            {
                name
                for name in names
                if names.count(name) > 1
            }
        )
        self.assertEqual(duplicates, [])

    def test_command_json_option_does_not_replace_subcommand(self):
        command_json = canonical(
            [str(FAKE_STAGE_AGENT)]
        ).decode("utf-8").strip()
        authority = self.shadow_test_authority()
        prepared = {
            "schema_version": 1,
            "stage": "generate",
        }
        with (
            mock.patch.object(
                history_runtime,
                "_cli_runtime_authority",
                return_value=authority,
            ),
            mock.patch.object(
                history_runtime,
                "build_stage_manifest",
                return_value=prepared,
            ) as build,
            mock.patch.object(
                history_runtime,
                "run_contained_stage",
                return_value={"schema_version": 1},
            ),
            mock.patch("builtins.print"),
        ):
            self.assertEqual(
                history_runtime._main(
                    [
                        "run-stage",
                        "--stage",
                        "generate",
                        "--seat",
                        "generate",
                        "--db",
                        str(self.database),
                        "--policy",
                        str(self.policy_path),
                        "--output-root",
                        str(self.root / "cli-stage-output"),
                        "--manifest",
                        str(self.root / "cli-stage-manifest.json"),
                        "--command",
                        command_json,
                    ]
                ),
                0,
            )
        self.assertEqual(
            build.call_args.kwargs["command_json"],
            command_json,
        )
        with (
            mock.patch.object(
                history_runtime,
                "_cli_runtime_authority",
                return_value=authority,
            ),
            mock.patch.object(
                history_runtime,
                "compare_frozen_targets",
                return_value={"schema_version": 1},
            ) as compare,
            mock.patch("builtins.print"),
        ):
            self.assertEqual(
                history_runtime._main(
                    [
                        "compare-targets",
                        "--db",
                        str(self.database),
                        "--policy",
                        str(self.policy_path),
                        "--batch",
                        str(self.root / "batch.json"),
                        "--artifact-root",
                        str(self.root / "artifacts"),
                        "--selection",
                        str(self.root / "selection.json"),
                        "--command",
                        command_json,
                    ]
                ),
                0,
            )
        self.assertEqual(
            compare.call_args.kwargs["command_json"],
            command_json,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
