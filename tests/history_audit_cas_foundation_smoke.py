#!/usr/bin/env python3
import hashlib
import pathlib
import sqlite3
import sys
import tempfile
import unittest
import zlib
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_audit_store
from lib import history_contract_v2
from lib import history_store
from history_contract_v2_smoke import valid_receipt


try:
    from lib import history_cas
except ImportError:
    class _CasAdapter:
        class CASError(RuntimeError):
            pass

        CASIntegrityError = CASError

        @staticmethod
        def put_object(conn, root, raw, retention_profile, *, pin_reason=None):
            object_id = hashlib.sha256(raw).hexdigest()
            path = pathlib.Path(root) / object_id[:2] / (object_id[2:] + ".zlib")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(zlib.compress(raw, level=9))
            return {"object_id": object_id}

        @staticmethod
        def verify_object(conn, root, object_id):
            raise _CasAdapter.CASIntegrityError("descriptor unavailable")

        @staticmethod
        def write_minimum_receipt(conn, receipt):
            return receipt["minimum_receipt_sha"]

    history_cas = _CasAdapter()


SHA = "0" * 64


class HistoryAuditCasFoundationSmoke(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.cas_root = self.root / "cas"
        self.db = self.root / "history.sqlite3"
        self.conn = history_store.connect(self.db)
        history_store.init_schema(self.conn)
        history_audit_store.init_schema(self.conn)
        self.conn.execute(
            """
            INSERT INTO audit_run_manifests(
              run_id, manifest_schema_version, plan_hash, manifest_json, created_at
            ) VALUES('run-1', 'history-audit-manifest-v2', ?, '{}',
                     '2026-08-03T00:00:00Z')
            """,
            (SHA,),
        )
        self.conn.execute(
            """
            INSERT INTO audit_snapshots(
              snapshot_id, snapshot_hash, history_as_of_watermark,
              current_batch_id_namespace, current_batch_ids_hash,
              exclusion_policy_sha, expected_asset_ids_hash, created_at,
              run_id, batch_id
            ) VALUES('snapshot-1', ?, 7, 'history-v2-staging-v1', ?, ?, ?,
                     '2026-08-03T00:00:00Z', 'run-1', 'batch-1')
            """,
            (SHA, SHA, SHA, SHA),
        )

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _receipt(self, object_ids):
        receipt = valid_receipt()
        receipt["raw_request_output_cas_hashes"] = list(object_ids)
        receipt["minimum_receipt_sha"] = "9" * 64
        return receipt

    def test_descriptor_binds_raw_and_compressed_hash_codec_and_lengths(self):
        raw = "请求与输出\n".encode("utf-8")
        descriptor = history_cas.put_object(
            self.conn, self.cas_root, raw, "transient-7d"
        )
        row = self.conn.execute(
            "SELECT * FROM audit_cas_objects WHERE object_id=?",
            (descriptor["object_id"],),
        ).fetchone()
        compressed = (
            self.cas_root / row["relative_path"]
        ).read_bytes()
        self.assertEqual(row["object_id"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(row["raw_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(
            row["compressed_sha256"], hashlib.sha256(compressed).hexdigest()
        )
        self.assertEqual(row["codec"], "zlib-v1")
        self.assertEqual(row["raw_length"], len(raw))
        self.assertEqual(row["compressed_length"], len(compressed))
        self.assertEqual(
            history_cas.verify_object(self.conn, self.cas_root, row["object_id"]),
            dict(row),
        )

    def test_equal_raw_payloads_deduplicate(self):
        first = history_cas.put_object(
            self.conn, self.cas_root, b"same payload", "transient-7d"
        )
        second = history_cas.put_object(
            self.conn, self.cas_root, b"same payload", "transient-7d"
        )
        self.assertEqual(first, second)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM audit_cas_objects").fetchone()[0],
            1,
        )

    def test_final_evidence_pin_is_durable(self):
        descriptor = history_cas.put_object(
            self.conn,
            self.cas_root,
            b"final evidence",
            "final-evidence",
            pin_reason="final-overlap-receipt:receipt-1",
        )
        self.conn.close()
        self.conn = history_store.connect(self.db)
        row = self.conn.execute(
            "SELECT pin_reason FROM audit_cas_pins WHERE object_id=?",
            (descriptor["object_id"],),
        ).fetchone()
        self.assertEqual(row[0], "final-overlap-receipt:receipt-1")

    def test_minimum_receipt_rejects_missing_cas_descriptor(self):
        receipt = self._receipt(["8" * 64])
        with self.assertRaises(history_cas.CASError):
            history_cas.write_minimum_receipt(self.conn, receipt)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM audit_receipts").fetchone()[0], 0
        )

    def test_complete_no_match_gates_are_enforced_by_storage(self):
        descriptor = history_cas.put_object(
            self.conn, self.cas_root, b"gate evidence", "permanent"
        )
        receipt = self._receipt([descriptor["object_id"]])
        receipt.update(
            final_status="complete_no_match",
            no_match_basis="l2_exhaustive",
            coverage_complete=False,
            adjudication_complete=False,
            semantic_policy_qualified=False,
        )
        with mock.patch.object(
            history_contract_v2, "validate_receipt", return_value=receipt
        ):
            with self.assertRaises(history_cas.CASError):
                history_cas.write_minimum_receipt(self.conn, receipt)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM audit_receipts").fetchone()[0], 0
        )

    def test_minimum_receipt_binds_run_plan_and_frozen_snapshot(self):
        descriptor = history_cas.put_object(
            self.conn, self.cas_root, b"identity evidence", "permanent"
        )
        for field, changed in (
            ("plan_hash", "1" * 64),
            ("snapshot_hash", "2" * 64),
            ("history_as_of_watermark", 8),
            ("current_batch_ids_hash", "3" * 64),
            ("exclusion_policy_sha", "4" * 64),
            ("expected_asset_ids_hash", "5" * 64),
        ):
            receipt = self._receipt([descriptor["object_id"]])
            receipt[field] = changed
            receipt["minimum_receipt_sha"] = hashlib.sha256(
                field.encode("utf-8")
            ).hexdigest()
            with self.subTest(field=field):
                with self.assertRaises(history_cas.CASError):
                    history_cas.write_minimum_receipt(self.conn, receipt)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM audit_receipts").fetchone()[0], 0
        )

    def test_standard_transaction_connection_commits_descriptor_and_pin(self):
        self.conn.close()
        standard = sqlite3.connect(str(self.db))
        standard.row_factory = sqlite3.Row
        standard.execute("PRAGMA foreign_keys = ON")
        history_audit_store.init_schema(standard)
        descriptor = history_cas.put_object(
            standard,
            self.cas_root,
            b"standard transaction mode",
            "permanent",
            pin_reason="final-overlap-receipt:standard",
        )
        standard.close()
        self.conn = history_store.connect(self.db)
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT 1 FROM audit_cas_objects WHERE object_id=?",
                (descriptor["object_id"],),
            ).fetchone()
        )
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT 1 FROM audit_cas_pins WHERE object_id=?",
                (descriptor["object_id"],),
            ).fetchone()
        )

    def test_integrity_guard_migration_rejects_preexisting_invalid_receipt(self):
        legacy_db = self.root / "pre-guard.sqlite3"
        legacy = history_store.connect(legacy_db)
        history_store.init_schema(legacy)
        pre_guard = tuple(
            migration
            for migration in history_audit_store.MIGRATIONS
            if migration.component != "integrity-guards"
        )
        with mock.patch.object(history_audit_store, "MIGRATIONS", pre_guard):
            history_audit_store.init_schema(legacy)
        legacy.execute(
            """
            INSERT INTO audit_run_manifests(
              run_id, manifest_schema_version, plan_hash, manifest_json, created_at
            ) VALUES('run-1', 'history-audit-manifest-v2', ?, '{}',
                     '2026-08-03T00:00:00Z')
            """,
            (SHA,),
        )
        legacy.execute(
            """
            INSERT INTO audit_snapshots(
              snapshot_id, snapshot_hash, history_as_of_watermark,
              current_batch_id_namespace, current_batch_ids_hash,
              exclusion_policy_sha, expected_asset_ids_hash, created_at,
              run_id, batch_id
            ) VALUES('snapshot-1', ?, 7, 'history-v2-staging-v1', ?, ?, ?,
                     '2026-08-03T00:00:00Z', 'run-1', 'batch-1')
            """,
            (SHA, SHA, SHA, SHA),
        )
        descriptor = history_cas.put_object(
            legacy, self.root / "pre-guard-cas", b"invalid legacy receipt", "permanent"
        )
        receipt = self._receipt([descriptor["object_id"]])
        receipt.update(
            final_status="complete_no_match",
            no_match_basis="l2_exhaustive",
            coverage_complete=False,
            adjudication_complete=False,
            semantic_policy_qualified=False,
        )
        with mock.patch.object(
            history_contract_v2, "validate_receipt", return_value=receipt
        ):
            history_cas.write_minimum_receipt(legacy, receipt)
        with self.assertRaises(history_audit_store.AuditMigrationError):
            history_audit_store.init_schema(legacy)
        legacy.close()

    def test_storage_rejects_fault_flags_with_complete_coverage(self):
        descriptor = history_cas.put_object(
            self.conn, self.cas_root, b"coverage fault evidence", "permanent"
        )
        cases = (
            ("complete_no_match", "invalid_schema"),
            ("complete_no_match", "invalid_anchor"),
            ("complete_no_match", "truncated"),
            ("uncertain", "invalid_schema"),
            ("uncertain", "invalid_anchor"),
            ("uncertain", "truncated"),
        )
        for status, fault in cases:
            receipt = self._receipt([descriptor["object_id"]])
            if status == "complete_no_match":
                receipt.update(
                    final_status=status,
                    no_match_basis="l2_exhaustive",
                    semantic_policy_qualified=True,
                )
            receipt[fault] = True
            receipt["minimum_receipt_sha"] = hashlib.sha256(
                f"{status}:{fault}".encode("utf-8")
            ).hexdigest()
            with self.subTest(status=status, fault=fault):
                with mock.patch.object(
                    history_contract_v2, "validate_receipt", return_value=receipt
                ):
                    with self.assertRaises(history_cas.CASError):
                        history_cas.write_minimum_receipt(self.conn, receipt)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM audit_receipts").fetchone()[0], 0
        )

    def test_coverage_guard_migration_rejects_preexisting_fault_receipt(self):
        legacy_db = self.root / "pre-coverage-guard.sqlite3"
        legacy = history_store.connect(legacy_db)
        history_store.init_schema(legacy)
        pre_guard = tuple(
            migration
            for migration in history_audit_store.MIGRATIONS
            if migration.component != "coverage-integrity-guards"
        )
        with mock.patch.object(history_audit_store, "MIGRATIONS", pre_guard):
            history_audit_store.init_schema(legacy)
        legacy.execute(
            """
            INSERT INTO audit_run_manifests(
              run_id, manifest_schema_version, plan_hash, manifest_json, created_at
            ) VALUES('run-1', 'history-audit-manifest-v2', ?, '{}',
                     '2026-08-03T00:00:00Z')
            """,
            (SHA,),
        )
        legacy.execute(
            """
            INSERT INTO audit_snapshots(
              snapshot_id, snapshot_hash, history_as_of_watermark,
              current_batch_id_namespace, current_batch_ids_hash,
              exclusion_policy_sha, expected_asset_ids_hash, created_at,
              run_id, batch_id
            ) VALUES('snapshot-1', ?, 7, 'history-v2-staging-v1', ?, ?, ?,
                     '2026-08-03T00:00:00Z', 'run-1', 'batch-1')
            """,
            (SHA, SHA, SHA, SHA),
        )
        descriptor = history_cas.put_object(
            legacy,
            self.root / "pre-coverage-guard-cas",
            b"preexisting coverage fault",
            "permanent",
        )
        receipt = self._receipt([descriptor["object_id"]])
        receipt.update(
            final_status="complete_no_match",
            no_match_basis="l2_exhaustive",
            semantic_policy_qualified=True,
            invalid_schema=True,
        )
        with mock.patch.object(
            history_contract_v2, "validate_receipt", return_value=receipt
        ):
            history_cas.write_minimum_receipt(legacy, receipt)
        with self.assertRaises(history_audit_store.AuditMigrationError):
            history_audit_store.init_schema(legacy)
        legacy.close()

    def test_crash_after_cas_publish_before_receipt_can_recover_descriptor(self):
        raw = b"published before descriptor transaction"
        compressed = zlib.compress(raw, level=9)
        object_id = hashlib.sha256(raw).hexdigest()
        relative = pathlib.Path(object_id[:2]) / (object_id[2:] + ".zlib")
        path = self.cas_root / relative
        path.parent.mkdir(parents=True)
        path.write_bytes(compressed)
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM audit_cas_objects WHERE object_id=?", (object_id,)
            ).fetchone()
        )
        descriptor = history_cas.put_object(
            self.conn, self.cas_root, raw, "transient-7d"
        )
        self.assertEqual(descriptor["object_id"], object_id)
        self.assertEqual(
            history_cas.verify_object(self.conn, self.cas_root, object_id),
            dict(
                self.conn.execute(
                    "SELECT * FROM audit_cas_objects WHERE object_id=?", (object_id,)
                ).fetchone()
            ),
        )
        self.assertEqual(
            history_cas.write_minimum_receipt(self.conn, self._receipt([object_id])),
            "9" * 64,
        )


if __name__ == "__main__":
    unittest.main()
