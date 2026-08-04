#!/usr/bin/env python3
"""CAS retention, integrity, and crash-recovery smoke tests."""

import datetime
import hashlib
import os
import pathlib
import sqlite3
import tempfile
import unittest
import zlib
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]

import sys

sys.path.insert(0, str(ROOT))

from lib import history_audit_store
from lib import history_cas
from lib import history_contract_v2


def sha(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class HistoryAuditStoreSmoke(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name).resolve()
        self.cas_root = self.root / "cas"
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        history_audit_store.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temporary.cleanup()

    def _past(self):
        return "2000-01-01T00:00:00+00:00"

    def _now(self):
        return "2026-08-03T00:00:00+00:00"

    def _put(self, raw=b"payload", *, pin_reason=None):
        return history_cas.put_object(
            self.conn,
            self.cas_root,
            raw,
            "transient-test",
            expires_at=self._past(),
            pin_reason=pin_reason,
        )

    def _path(self, descriptor):
        return self.cas_root / descriptor["relative_path"]

    def _install_receipt_owner(self, receipt):
        self.conn.execute(
            "INSERT INTO audit_run_manifests VALUES(?, ?, ?, ?, ?)",
            (
                receipt["run_id"],
                receipt["manifest_schema_version"],
                receipt["plan_hash"],
                "{}",
                self._now(),
            ),
        )
        self.conn.execute(
            """
            INSERT INTO audit_snapshots(
              snapshot_id, snapshot_hash, history_as_of_watermark,
              current_batch_id_namespace, current_batch_ids_hash,
              exclusion_policy_sha, expected_asset_ids_hash, created_at,
              run_id, batch_id
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt["snapshot_id"],
                receipt["snapshot_hash"],
                receipt["history_as_of_watermark"],
                receipt["current_batch_id_namespace"],
                receipt["current_batch_ids_hash"],
                receipt["exclusion_policy_sha"],
                receipt["expected_asset_ids_hash"],
                self._now(),
                receipt["run_id"],
                "batch-1",
            ),
        )
        self.conn.commit()

    def _store_fixture_provenance(self, receipt):
        return {
            "schema_version": "history-receipt-provenance-v2",
            "authority_kind": "l2",
            "run_id": receipt["run_id"],
            "plan_sha": receipt["plan_hash"],
            "fixture_scope": "store-smoke-only",
        }

    def _write_store_fixture_receipt(self, receipt):
        with mock.patch.object(
            history_audit_store,
            "derive_receipt_provenance",
            return_value=self._store_fixture_provenance(receipt),
        ):
            return history_cas.write_minimum_receipt(self.conn, receipt)

    def _receipt(self, object_ids):
        receipt = {
            "manifest_schema_version": "history-audit-manifest-v2",
            "canonical_codec_version": "history-canonical-json-v2",
            "run_id": "run-store-smoke",
            "plan_hash": sha("plan"),
            "candidate_hash": sha("candidate"),
            "snapshot_id": sha("snapshot-id"),
            "snapshot_hash": sha("snapshot"),
            "history_as_of_watermark": 10,
            "current_batch_id_namespace": "history-v2-staging-v1",
            "current_batch_ids_hash": sha("batch"),
            "exclusion_policy_sha": sha("exclusion"),
            "expected_asset_ids_hash": sha("expected"),
            "observed_asset_ids_hash": sha("observed"),
            "missing_ids": ["asset-2"],
            "duplicate_ids": [],
            "extra_ids": [],
            "invalid_schema": False,
            "invalid_anchor": False,
            "truncated": False,
            "provider_pools_ordered": {
                "comparator": ["codex"],
                "map": ["codex"],
                "detail": ["codex"],
                "reduce": ["codex"],
            },
            "provider_capability_profile_hashes": [sha("cap")],
            "capacity_profile_id": "test-capacity",
            "semantic_policy_profile_id": "test-policy",
            "risk_policy_version": "risk-v1",
            "matched_router_rule_ids": ["rule-1"],
            "settlement_policy_sha": sha("settlement"),
            "shard_plan_sha": sha("shards"),
            "logical_task_hashes": [sha("task")],
            "attempt_manifest_hashes": [sha("attempt")],
            "raw_request_output_cas_hashes": list(object_ids),
            "minimum_receipt_sha": sha("receipt"),
            "coverage_complete": False,
            "adjudication_complete": False,
            "semantic_policy_qualified": False,
            "no_match_basis": None,
            "final_status": "partial",
            "stage_reason_code": "incomplete_coverage",
            "evidence_anchors": [],
        }
        receipt["minimum_receipt_sha"] = (
            history_contract_v2.minimum_receipt_sha(receipt)
        )
        return receipt

    def test_equal_raw_objects_deduplicate_after_compression(self):
        first = self._put(b"same raw bytes")
        second = self._put(b"same raw bytes")
        self.assertEqual(first["object_id"], second["object_id"])
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM audit_cas_objects").fetchone()[0],
            1,
        )
        self.assertEqual(len(list(self.cas_root.rglob("*.zlib"))), 1)

    def test_final_evidence_pin_blocks_expiry(self):
        descriptor = self._put(b"final evidence", pin_reason="final-overlap")
        removed = history_cas.collect_garbage(
            self.conn, self.cas_root, self._now(), grace_seconds=0
        )
        self.assertEqual(removed, [])
        self.assertTrue(self._path(descriptor).is_file())
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM audit_cas_tombstones WHERE object_id=?",
                (descriptor["object_id"],),
            ).fetchone()
        )

    def test_terminal_receipt_pins_referenced_evidence(self):
        descriptor = self._put(b"terminal receipt evidence")
        receipt = self._receipt([descriptor["object_id"]])
        receipt.update(
            final_status="overlap_found",
            stage_reason_code="match_found_partial_coverage",
        )
        receipt["minimum_receipt_sha"] = (
            history_contract_v2.minimum_receipt_sha(receipt)
        )
        self._install_receipt_owner(receipt)
        self._write_store_fixture_receipt(receipt)
        pin = self.conn.execute(
            "SELECT pin_reason FROM audit_cas_pins WHERE object_id=?",
            (descriptor["object_id"],),
        ).fetchone()
        self.assertEqual(pin["pin_reason"], "terminal-receipt:" + receipt["minimum_receipt_sha"])
        self.assertEqual(
            history_cas.collect_garbage(
                self.conn, self.cas_root, self._now(), grace_seconds=0
            ),
            [],
        )

    def test_gc_writes_tombstone_before_payload_delete(self):
        descriptor = self._put(b"delete after durable tombstone")

        def crash_after_observing_tombstone(path):
            row = self.conn.execute(
                "SELECT reason FROM audit_cas_tombstones WHERE object_id=?",
                (descriptor["object_id"],),
            ).fetchone()
            self.assertEqual(row["reason"], "retention_expired")
            raise OSError("injected crash before unlink")

        with mock.patch.object(
            history_cas, "_delete_payload", side_effect=crash_after_observing_tombstone
        ):
            with self.assertRaises(OSError):
                history_cas.collect_garbage(
                    self.conn, self.cas_root, self._now(), grace_seconds=0
                )
        self.assertTrue(self._path(descriptor).exists())
        removed = history_cas.collect_garbage(
            self.conn, self.cas_root, self._now(), grace_seconds=0
        )
        self.assertEqual(removed, [descriptor["object_id"]])
        self.assertFalse(self._path(descriptor).exists())

    def test_missing_without_tombstone_is_integrity_fault(self):
        descriptor = self._put(b"unexpectedly missing")
        self._path(descriptor).unlink()
        with self.assertRaises(history_cas.CASIntegrityError):
            history_cas.verify_object(
                self.conn, self.cas_root, descriptor["object_id"]
            )

    def test_compressed_or_raw_hash_mismatch_is_integrity_fault(self):
        descriptor = self._put(b"compressed mismatch")
        self._path(descriptor).write_bytes(b"bad bytes")
        with self.assertRaises(history_cas.CASIntegrityError):
            history_cas.verify_object(
                self.conn, self.cas_root, descriptor["object_id"]
            )

        raw_id = sha("expected raw")
        wrong_raw = b"different raw"
        compressed = zlib.compress(wrong_raw, level=9)
        relative = pathlib.Path(raw_id[:2]) / (raw_id[2:] + ".zlib")
        target = self.cas_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(compressed)
        self.conn.execute(
            """
            INSERT INTO audit_cas_objects VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                raw_id,
                raw_id,
                hashlib.sha256(compressed).hexdigest(),
                "zlib-v1",
                len(wrong_raw),
                len(compressed),
                "transient-test",
                relative.as_posix(),
                self._now(),
                None,
                "verified",
            ),
        )
        self.conn.commit()
        with self.assertRaises(history_cas.CASIntegrityError):
            history_cas.verify_object(self.conn, self.cas_root, raw_id)

    def test_minimum_receipt_verifies_after_normal_raw_expiry(self):
        descriptor = self._put(b"receipt evidence")
        receipt = self._receipt([descriptor["object_id"]])
        self._install_receipt_owner(receipt)
        self._write_store_fixture_receipt(receipt)
        history_cas.collect_garbage(
            self.conn, self.cas_root, self._now(), grace_seconds=0
        )
        with mock.patch.object(
            history_audit_store,
            "derive_receipt_provenance",
            return_value=self._store_fixture_provenance(receipt),
        ):
            verified = history_cas.verify_minimum_receipt(
                self.conn, self.cas_root, receipt["minimum_receipt_sha"]
            )
        self.assertEqual(verified["minimum_receipt_sha"], receipt["minimum_receipt_sha"])
        self.assertEqual(verified["cas_states"], {descriptor["object_id"]: "expired"})

    def test_crash_after_object_publish_before_db_descriptor_recovers_safely(self):
        raw = b"published before descriptor"
        object_id = hashlib.sha256(raw).hexdigest()
        compressed = zlib.compress(raw, level=9)
        history_cas._publish(self.cas_root, object_id, compressed)
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM audit_cas_objects WHERE object_id=?", (object_id,)
            ).fetchone()
        )
        descriptor = self._put(raw)
        self.assertEqual(descriptor["object_id"], object_id)
        self.assertEqual(
            history_cas.verify_object(self.conn, self.cas_root, object_id)["object_id"],
            object_id,
        )

    def test_crash_after_tombstone_before_delete_resumes_idempotently(self):
        descriptor = self._put(b"resume delete")
        with mock.patch.object(
            history_cas, "_delete_payload", side_effect=OSError("injected crash")
        ):
            with self.assertRaises(OSError):
                history_cas.collect_garbage(
                    self.conn, self.cas_root, self._now(), grace_seconds=0
                )
        tombstone = dict(
            self.conn.execute(
                "SELECT * FROM audit_cas_tombstones WHERE object_id=?",
                (descriptor["object_id"],),
            ).fetchone()
        )
        history_cas.collect_garbage(
            self.conn, self.cas_root, self._now(), grace_seconds=0
        )
        resumed = dict(
            self.conn.execute(
                "SELECT * FROM audit_cas_tombstones WHERE object_id=?",
                (descriptor["object_id"],),
            ).fetchone()
        )
        self.assertEqual(resumed, tombstone)
        self.assertFalse(self._path(descriptor).exists())

    def test_linked_special_and_sparse_payloads_are_integrity_faults(self):
        descriptor = self._put(b"linked")
        os.link(self._path(descriptor), self.cas_root / "extra-link")
        with self.assertRaises(history_cas.CASIntegrityError):
            history_cas.verify_object(
                self.conn, self.cas_root, descriptor["object_id"]
            )

        special = self._put(b"special")
        self._path(special).unlink()
        os.mkfifo(self._path(special))
        with self.assertRaises(history_cas.CASIntegrityError):
            history_cas.verify_object(self.conn, self.cas_root, special["object_id"])

        sparse = self._put(b"sparse")
        self._path(sparse).unlink()
        self._path(sparse).touch()
        os.truncate(self._path(sparse), sparse["compressed_length"])
        with self.assertRaises(history_cas.CASIntegrityError):
            history_cas.verify_object(self.conn, self.cas_root, sparse["object_id"])

    def test_cas_rejects_root_reached_through_symlink_ancestor(self):
        real_parent = self.root / "real-parent"
        real_parent.mkdir()
        linked_parent = self.root / "linked-parent"
        os.symlink(real_parent, linked_parent)
        with self.assertRaises(history_cas.CASError):
            history_cas.put_object(
                self.conn, linked_parent / "cas", b"must not follow", "transient-test"
            )

    def test_cas_never_follows_root_swapped_after_safe_open(self):
        self.cas_root.mkdir()
        detached = self.root / "detached-cas"
        attacker = self.root / "attacker"
        attacker.mkdir()

        def swap_root(*_):
            self.cas_root.rename(detached)
            os.symlink(attacker, self.cas_root)

        with mock.patch.object(
            history_cas, "_after_root_open", side_effect=swap_root, create=True
        ):
            with self.assertRaises(history_cas.CASError):
                history_cas.put_object(
                    self.conn, self.cas_root, b"must stay out", "transient-test"
                )
        self.assertEqual(list(attacker.iterdir()), [])

    def test_cas_never_follows_object_directory_swapped_after_safe_open(self):
        raw = b"must stay inside held object directory"
        object_id = hashlib.sha256(raw).hexdigest()
        object_directory = self.cas_root / object_id[:2]
        object_directory.mkdir(parents=True)
        detached = self.root / "detached-object-directory"
        attacker = self.root / "object-attacker"
        attacker.mkdir()

        def swap_object_directory(*_):
            object_directory.rename(detached)
            os.symlink(attacker, object_directory)

        with mock.patch.object(
            history_cas,
            "_after_object_directory_open",
            side_effect=swap_object_directory,
            create=True,
        ):
            with self.assertRaises(history_cas.CASError):
                history_cas.put_object(
                    self.conn, self.cas_root, raw, "transient-test"
                )
        self.assertEqual(list(attacker.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
