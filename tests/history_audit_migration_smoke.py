#!/usr/bin/env python3
import hashlib
import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_store


try:
    from lib import history_audit_store as audit_store
except ImportError:
    class _MigrationAdapter:
        MIGRATIONS = ()
        AuditMigrationError = RuntimeError
        StaleFence = RuntimeError

        @staticmethod
        def init_schema(conn):
            history_store.init_schema(conn)

        @staticmethod
        def quarantine_legacy_receipts(conn):
            return 0

        @staticmethod
        def compare_and_set_logical_task(*args, **kwargs):
            return False

    audit_store = _MigrationAdapter()


SHA = "0" * 64
HEADER = b"date\tsource\ttheme\tidea\tverdict\treason\toverlap\tcategory\n"


class HistoryAuditMigrationSmoke(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        (self.root / "ledger.instance-id").write_text(
            "audit-migration-test\n", encoding="utf-8"
        )
        self.db = self.root / "history.sqlite3"
        self.conn = history_store.connect(self.db)
        history_store.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _table_names(self):
        return {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

    def _insert_legacy_receipt(self, receipt_id, status, relations):
        manifest_json = '{"generation":1}\n'
        self.conn.execute(
            """
            INSERT OR IGNORE INTO history_generation_provenance(
              generation, manifest_sha256, manifest_json, source_watermark,
              policy_sha256, projection_schema_version, created_at
            ) VALUES(1, ?, ?, 0, ?, 'projection-v1', '2026-08-03T00:00:00Z')
            """,
            ("1" * 64, manifest_json, "2" * 64),
        )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO history_pack_publications(
              publication_id, pack_sha256, pack_bytes, policy_sha256,
              generation, generation_manifest_sha256, source_watermark,
              retrieval_status, rank_trace_json, rank_trace_sha256,
              comparator_invocation_json, comparator_invocation_sha256,
              comparator_preflight_json, comparator_preflight_sha256,
              created_at
            ) VALUES('pack-1', ?, ?, ?, 1, ?, 0, 'complete', ?, ?, ?, ?, ?, ?,
                     '2026-08-03T00:00:00Z')
            """,
            (
                "3" * 64,
                b"{}\n",
                "2" * 64,
                "1" * 64,
                '{}',
                "4" * 64,
                '{}',
                "5" * 64,
                '{}',
                "6" * 64,
            ),
        )
        payload = {
            "receipt_id": receipt_id,
            "status": status,
            "relations": relations,
        }
        receipt_json = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        self.conn.execute(
            "PRAGMA ignore_check_constraints = ON"
        )
        try:
            self.conn.execute(
                """
                INSERT INTO history_receipts(
                  receipt_id, query_candidate_id, intent, pack_sha256,
                  pack_publication_id, policy_sha256,
                  generation_manifest_sha256, rank_trace_sha256,
                  comparator_invocation_sha256, comparator_preflight_sha256,
                  retrieval_policy_version, source_watermark, index_generation,
                  comparator_version, status, receipt_json, created_at
                ) VALUES(?, 'query-1', 'duplicate_search', ?, 'pack-1', ?, ?, ?,
                         ?, ?, 'retrieval-v1', 0, 1, 'comparator-v1', ?, ?,
                         '2026-08-03T00:00:00Z')
                """,
                (
                    receipt_id,
                    "3" * 64,
                    "2" * 64,
                    "1" * 64,
                    "4" * 64,
                    "5" * 64,
                    "6" * 64,
                    status,
                    receipt_json,
                ),
            )
        finally:
            self.conn.execute("PRAGMA ignore_check_constraints = OFF")
        return receipt_json

    def _insert_run_and_task(self):
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
            INSERT INTO audit_logical_tasks(
              task_hash, run_id, stage, staging_candidate_id, input_id,
              state, fence, claim_token, lease_until, created_at
            ) VALUES(?, 'run-1', 'map', 'stg-1', 'shard-1', 'planned', 0,
                     NULL, NULL, '2026-08-03T00:00:00Z')
            """,
            ("7" * 64,),
        )

    def test_empty_database_applies_each_component_once(self):
        audit_store.init_schema(self.conn)
        rows = self.conn.execute(
            "SELECT component, version, migration_sha256 "
            "FROM audit_schema_migrations ORDER BY rowid"
        ).fetchall()
        self.assertEqual(len(rows), len(audit_store.MIGRATIONS))
        self.assertEqual(
            [(row[0], row[1]) for row in rows],
            [(item.component, item.version) for item in audit_store.MIGRATIONS],
        )
        expected_tables = {
            "audit_run_manifests", "audit_snapshots", "audit_batch_staging",
            "audit_batch_pairs", "audit_activation_maps",
            "audit_direction_contracts", "audit_direction_checks",
            "audit_legacy_receipts", "audit_provider_profiles",
            "audit_capacity_profiles", "audit_shard_plans",
            "audit_logical_tasks", "audit_task_attempts",
            "audit_task_settlements", "audit_budget_events", "audit_receipts",
            "audit_cas_objects", "audit_cas_pins", "audit_cas_tombstones",
            "audit_metadata_profiles", "audit_annotations",
            "audit_metadata_outbox", "audit_semantic_qualifications",
        }
        self.assertTrue(expected_tables.issubset(self._table_names()))

    def test_repeated_init_is_byte_idempotent(self):
        audit_store.init_schema(self.conn)
        before = "\n".join(self.conn.iterdump()).encode("utf-8")
        audit_store.init_schema(self.conn)
        after = "\n".join(self.conn.iterdump()).encode("utf-8")
        self.assertEqual(before, after)

    def test_migration_sha_drift_fails_closed(self):
        audit_store.init_schema(self.conn)
        first = audit_store.MIGRATIONS[0]
        self.conn.execute(
            "UPDATE audit_schema_migrations SET migration_sha256 = ? "
            "WHERE component = ? AND version = ?",
            ("f" * 64, first.component, first.version),
        )
        with self.assertRaises(audit_store.AuditMigrationError):
            audit_store.init_schema(self.conn)

    def test_interrupted_component_rolls_back_all_ddl(self):
        audit_store.init_schema(self.conn)
        migration_type = type(audit_store.MIGRATIONS[0])
        broken = migration_type(
            "fault-injection",
            1,
            "CREATE TABLE audit_should_rollback(value TEXT);\nCREATE TABLE broken(\n",
        )
        with mock.patch.object(
            audit_store, "MIGRATIONS", audit_store.MIGRATIONS + (broken,)
        ):
            with self.assertRaises(audit_store.AuditMigrationError):
                audit_store.init_schema(self.conn)
        self.assertNotIn("audit_should_rollback", self._table_names())
        count = self.conn.execute(
            "SELECT count(*) FROM audit_schema_migrations "
            "WHERE component='fault-injection'"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_current_populated_database_keeps_v1_schema_and_rows(self):
        ledger = self.root / "ledger.tsv"
        ledger.write_bytes(
            HEADER
            + b"2026-08-03\thunt\tAudit\tlegacy story\taccept-w-rev\treason\tlow\tdesign-fixable\n"
        )
        history_store.import_tsv_epoch(self.conn, ledger)
        before_schema = {
            row[0]: row[1]
            for row in self.conn.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'audit_%' AND sql IS NOT NULL"
            )
        }
        before_rows = [
            tuple(row)
            for row in self.conn.execute(
                "SELECT candidate_id, source_sequence, raw_row FROM candidates"
            )
        ]
        audit_store.init_schema(self.conn)
        after_schema = {
            row[0]: row[1]
            for row in self.conn.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'audit_%' AND sql IS NOT NULL"
            )
        }
        after_rows = [
            tuple(row)
            for row in self.conn.execute(
                "SELECT candidate_id, source_sequence, raw_row FROM candidates"
            )
        ]
        self.assertEqual(before_schema, after_schema)
        self.assertEqual(before_rows, after_rows)

    def test_legacy_complete_no_match_is_quarantined_not_promoted(self):
        raw = self._insert_legacy_receipt("legacy-no-match", "complete_no_match", [])
        audit_store.init_schema(self.conn)
        self.assertEqual(audit_store.quarantine_legacy_receipts(self.conn), 1)
        row = self.conn.execute(
            "SELECT * FROM audit_legacy_receipts WHERE legacy_receipt_id=?",
            ("legacy-no-match",),
        ).fetchone()
        self.assertEqual(row["legacy_json_sha256"], hashlib.sha256(raw.encode()).hexdigest())
        self.assertEqual(row["legacy_status_token"], "complete_no_match")
        self.assertEqual(row["pack_publication_id"], "pack-1")
        self.assertEqual(row["compatibility_state"], "unqualified")
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM audit_receipts").fetchone()[0], 0
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_semantic_qualifications"
            ).fetchone()[0],
            0,
        )

    def test_unknown_legacy_status_is_preserved_as_invalid_compatibility(self):
        self._insert_legacy_receipt(
            "legacy-unknown", "new_old_token", [{"relation": "maybe_same"}]
        )
        audit_store.init_schema(self.conn)
        audit_store.quarantine_legacy_receipts(self.conn)
        row = self.conn.execute(
            "SELECT legacy_status_token, legacy_relation_tokens_json, "
            "compatibility_state FROM audit_legacy_receipts "
            "WHERE legacy_receipt_id='legacy-unknown'"
        ).fetchone()
        self.assertEqual(row[0], "new_old_token")
        self.assertEqual(json.loads(row[1]), ["maybe_same"])
        self.assertEqual(row[2], "ambiguous")

    def test_claim_compare_and_set_rejects_stale_fence(self):
        audit_store.init_schema(self.conn)
        self._insert_run_and_task()
        with self.assertRaises(ValueError):
            audit_store.compare_and_set_logical_task(
                self.conn,
                "7" * 64,
                expected_state="planned",
                expected_fence=0,
                new_state="claimed",
                new_fence=0,
                claim_token="non-incrementing",
                lease_until="2026-08-03T00:01:00Z",
            )
        self.assertTrue(
            audit_store.compare_and_set_logical_task(
                self.conn,
                "7" * 64,
                expected_state="planned",
                expected_fence=0,
                new_state="claimed",
                new_fence=1,
                claim_token="claim-1",
                lease_until="2026-08-03T00:01:00Z",
            )
        )
        with self.assertRaises(audit_store.StaleFence):
            audit_store.compare_and_set_logical_task(
                self.conn,
                "7" * 64,
                expected_state="planned",
                expected_fence=0,
                new_state="claimed",
                new_fence=1,
                claim_token="stale",
                lease_until="2026-08-03T00:02:00Z",
            )

    def test_immutable_fact_tables_reject_update_and_delete(self):
        audit_store.init_schema(self.conn)
        self.conn.execute(
            """
            INSERT INTO audit_snapshots(
              snapshot_id, snapshot_hash, history_as_of_watermark,
              current_batch_id_namespace, current_batch_ids_hash,
              exclusion_policy_sha, expected_asset_ids_hash, created_at
            ) VALUES('snapshot-1', ?, 0, 'history-v2-staging-v1', ?, ?, ?,
                     '2026-08-03T00:00:00Z')
            """,
            (SHA, "1" * 64, "2" * 64, "3" * 64),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE audit_snapshots SET snapshot_hash=? "
                "WHERE snapshot_id='snapshot-1'",
                ("4" * 64,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "DELETE FROM audit_snapshots WHERE snapshot_id='snapshot-1'"
            )


if __name__ == "__main__":
    unittest.main()
