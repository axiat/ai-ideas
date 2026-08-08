#!/usr/bin/env python3
import pathlib
import tempfile
import unittest

import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_audit
from lib import history_audit_store
from lib import history_store


def raw_row(story):
    return (
        "2026-08-09\thunt\tAudit\t"
        + story
        + "\taccept-w-rev\treason\tlow\tdesign-fixable\n"
    ).encode("utf-8")


class HistoryAuditCorrectnessRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.conn = history_store.connect(self.root / "history.sqlite3")
        history_store.init_schema(self.conn)
        history_audit_store.init_schema(self.conn)
        self.run_id = "run-correctness"
        self.batch_id = "batch-correctness"
        self.staging_id = "stg-v2-" + "1" * 64
        self.conn.execute(
            """
            INSERT INTO audit_run_manifests(
              run_id, manifest_schema_version, plan_hash, manifest_json, created_at
            ) VALUES(?, 'history-audit-manifest-v2', ?, '{}',
                     '2026-08-09T00:00:00Z')
            """,
            (self.run_id, "2" * 64),
        )
        self.snapshot = history_audit.freeze_snapshot(
            self.conn,
            run_id=self.run_id,
            batch_id=self.batch_id,
            current_batch_ids=[self.staging_id],
        )
        self.raw_candidates = [{
            "staging_candidate_id": self.staging_id,
            "raw_candidate": raw_row("candidate alpha"),
        }]
        self.direction = {
            "direction_id": "direction-correctness",
            "contract_sha": "3" * 64,
            "validator_version": "direction-validator-v1",
            "artifact_sha": "4" * 64,
        }

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_stage_raw_batch_rejects_conflicting_persisted_direction(self):
        history_audit.stage_raw_batch(
            self.conn,
            snapshot=self.snapshot,
            raw_candidates=self.raw_candidates,
            direction_receipt=self.direction,
        )
        conflicting = dict(self.direction, artifact_sha="5" * 64)

        with self.assertRaisesRegex(
            ValueError, "direction contract identity conflicts with durable state"
        ):
            history_audit.stage_raw_batch(
                self.conn,
                snapshot=self.snapshot,
                raw_candidates=self.raw_candidates,
                direction_receipt=conflicting,
            )

        rows = self.conn.execute(
            "SELECT direction_id, contract_sha, validator_version, artifact_sha "
            "FROM audit_direction_contracts WHERE run_id=? AND batch_id=?",
            (self.run_id, self.batch_id),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            tuple(rows[0]),
            tuple(
                self.direction[name]
                for name in (
                    "direction_id", "contract_sha", "validator_version", "artifact_sha"
                )
            ),
        )

    def test_staged_snapshot_rejects_non_mapping_candidate_with_value_error(self):
        staged_batch = {
            "run_id": self.run_id,
            "batch_id": self.batch_id,
            "snapshot_id": self.snapshot["snapshot_id"],
            "candidates": [None],
        }

        with self.assertRaisesRegex(ValueError, "staged batch candidates are invalid"):
            history_audit.record_batch_pair_results(
                self.conn, staged_batch, {"not": "used"}, []
            )

    def test_exceptional_card_merge_validates_shapes_before_extending(self):
        base = {
            "lineage_id": "lineage-1",
            "semantic_relation": "uncertain",
            "item_ids": ["asset-1"],
            "evidence": [{"quote": "evidence"}],
        }
        invalid_cards = {
            "lineage mapping": dict(base, lineage_id={"bad": "lineage"}),
            "item_ids string": dict(base, item_ids="asset-1"),
            "item_ids member": dict(base, item_ids=[{"bad": "asset"}]),
            "evidence mapping": dict(base, evidence={"quote": "evidence"}),
            "evidence null": dict(base, evidence=None),
            "open card": {**base, "unexpected": True},
        }

        for name, card in invalid_cards.items():
            with self.subTest(name=name):
                with self.assertRaisesRegex(
                    ValueError, "derived exceptional card is invalid"
                ):
                    history_audit._merge_l2_exceptional_cards([card])

        self.assertEqual(
            history_audit._merge_l2_exceptional_cards([base]),
            [base],
        )


if __name__ == "__main__":
    unittest.main()
