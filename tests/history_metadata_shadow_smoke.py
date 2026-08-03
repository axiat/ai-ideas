#!/usr/bin/env python3
import copy
import datetime
import hashlib
import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_audit
from lib import history_audit_store
from lib import history_metadata
from lib import history_store


HEADER = b"date\tsource\ttheme\tidea\tverdict\treason\toverlap\tcategory\n"


def ledger_row(story):
    return (
        "2026-08-03\thunt\tMetadata\t"
        + story
        + "\taccept-w-rev\treason\tlow\tdesign-fixable\n"
    ).encode("utf-8")


def profile(profile_id, version, prompt_digit, *, supersedes=None):
    return {
        "profile_id": profile_id,
        "profile_key": "metadata-shadow",
        "profile_version": version,
        "schema_version": "history-metadata-profile-v1",
        "producer": {
            "kind": "rule",
            "id": "metadata-fixture",
            "version": version,
        },
        "prompt_sha256": prompt_digit * 64,
        "synopsis_max_chars": 512,
        "supersedes_profile_id": supersedes,
    }


def annotation(family, value, confidence=1.0, *, direction_identity=None):
    result = {"family": family, "value": value, "confidence": confidence}
    if direction_identity is not None:
        result["direction_identity"] = direction_identity
    return result


class HistoryMetadataShadowSmoke(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        (self.root / "ledger.instance-id").write_text(
            "metadata-shadow\n", encoding="utf-8"
        )
        self.ledger = self.root / "ledger.tsv"
        self.ledger.write_bytes(
            HEADER + ledger_row("prior alpha") + ledger_row("prior beta")
        )
        self.conn = history_store.connect(self.root / "history.sqlite3")
        history_store.init_schema(self.conn)
        history_store.import_tsv_epoch(self.conn, self.ledger)
        history_audit_store.init_schema(self.conn)
        self.rows = list(
            self.conn.execute(
                "SELECT candidate_id, lineage_id, raw_sha256, source_sequence "
                "FROM candidates ORDER BY source_sequence"
            )
        )
        self.candidate_id = self.rows[0]["candidate_id"]
        self.lineage_id = self.rows[0]["lineage_id"]
        self.content_sha = self.rows[0]["raw_sha256"]
        self.conn.execute(
            """
            INSERT INTO audit_run_manifests(
              run_id, manifest_schema_version, plan_hash, manifest_json, created_at
            ) VALUES('metadata-run', 'history-audit-manifest-v2', ?, '{}',
                     '2026-08-03T00:00:00Z')
            """,
            ("a" * 64,),
        )
        self.snapshot = history_audit.freeze_snapshot(
            self.conn,
            run_id="metadata-run",
            batch_id="metadata-batch",
            current_batch_ids=["stg-v2-" + "b" * 64],
        )

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _register(self, profile_id="profile-v1", version="1", digit="1",
                  supersedes=None):
        return history_metadata.register_profile(
            self.conn,
            profile(
                profile_id,
                version,
                digit,
                supersedes=supersedes,
            ),
        )

    def _enqueue(self, profile_id="profile-v1", candidate_id=None,
                 content_sha=None):
        return history_metadata.enqueue_candidate(
            self.conn,
            self.candidate_id if candidate_id is None else candidate_id,
            self.content_sha if content_sha is None else content_sha,
            profile_id,
        )

    def _claim(self, work, token="worker-1", lease="2099-01-01T00:00:00Z",
               now="2026-08-03T00:00:00Z"):
        return history_metadata.claim_candidate(
            self.conn,
            work["outbox_id"],
            token,
            lease,
            now=now,
        )

    def _publish(self, annotations, *, profile_id="profile-v1",
                 candidate_id=None, content_sha=None, token="worker-1"):
        work = self._enqueue(
            profile_id,
            candidate_id=candidate_id,
            content_sha=content_sha,
        )
        claim = self._claim(work, token=token)
        return history_metadata.publish_annotations(
            self.conn, claim, annotations
        )

    def _insert_revision(self):
        source = self.conn.execute(
            "SELECT * FROM candidates WHERE candidate_id=?",
            (self.candidate_id,),
        ).fetchone()
        candidate_id = "revision-" + hashlib.sha256(b"revision").hexdigest()
        raw = ledger_row("prior alpha revised")
        content_sha = hashlib.sha256(raw.rstrip(b"\n")).hexdigest()
        self.conn.execute(
            """
            INSERT INTO candidates(
              candidate_id, origin_stable_id, lineage_id, row_number, raw_sha256,
              field_count, date, source, theme, story, verdict, reason, overlap,
              category, source_sequence, raw_row, row_terminator, provenance_json
            ) VALUES(?, NULL, ?, NULL, ?, 8, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                source["lineage_id"],
                content_sha,
                source["date"],
                source["source"],
                source["theme"],
                "prior alpha revised",
                source["verdict"],
                source["reason"],
                source["overlap"],
                source["category"],
                self.snapshot["history_as_of_watermark"] + 1,
                raw.rstrip(b"\n"),
                b"\n",
                "{}",
            ),
        )
        self.conn.execute(
            """
            INSERT INTO audit_run_manifests(
              run_id, manifest_schema_version, plan_hash, manifest_json, created_at
            ) VALUES('metadata-revision-run', 'history-audit-manifest-v2', ?, '{}',
                     '2026-08-03T00:00:00Z')
            """,
            ("c" * 64,),
        )
        snapshot = history_audit.freeze_snapshot(
            self.conn,
            run_id="metadata-revision-run",
            batch_id="metadata-revision-batch",
            current_batch_ids=["stg-v2-" + "d" * 64],
        )
        return candidate_id, content_sha, snapshot

    def test_annotations_are_append_only_and_bind_source_and_profile(self):
        first_profile = self._register()
        first = self._publish(
            [annotation("synopsis", "bounded synopsis")]
        )
        self._register("profile-v2", "2", "2", supersedes="profile-v1")
        second = self._publish(
            [annotation("synopsis", "new bounded synopsis")],
            profile_id="profile-v2",
            token="worker-2",
        )

        rows = list(
            self.conn.execute(
                "SELECT * FROM audit_annotation_versions_v2 "
                "ORDER BY created_at, annotation_id"
            )
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row["source_content_sha"] for row in rows}, {self.content_sha}
        )
        self.assertEqual(
            {row["profile_id"] for row in rows}, {"profile-v1", "profile-v2"}
        )
        self.assertEqual(rows[0]["producer_id"], "metadata-fixture")
        self.assertEqual(rows[0]["prompt_sha256"], "1" * 64)
        self.assertEqual(first["published_count"], 1)
        self.assertEqual(second["published_count"], 1)
        self.assertEqual(first_profile["profile_sha256"], rows[0]["profile_sha256"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE audit_annotation_versions_v2 SET stale_state='stale'"
            )
        with self.assertRaises(ValueError):
            history_metadata.enqueue_candidate(
                self.conn, self.candidate_id, "f" * 64, "profile-v2"
            )

    def test_unknown_and_missing_annotations_do_not_block_candidate(self):
        self._register()
        empty_result = self._publish([])
        self.assertEqual(empty_result["published_count"], 0)
        self._register("profile-empty", "2", "2")
        unknown_result = self._publish(
            [annotation("concept", None), annotation("free_tag", "")],
            profile_id="profile-empty",
            token="worker-empty",
        )
        self.assertEqual(unknown_result["published_count"], 2)

        flat = [{"candidate_id": self.candidate_id, "score": 0.75}]
        union = history_metadata.union_shadow(flat, [])
        self.assertEqual(union["flat_rankings"], flat)
        self.assertEqual(union["candidate_union"], flat)
        audit_union = history_audit.read_l1_shadow_union(
            self.conn, self.snapshot, flat, [], []
        )
        self.assertEqual(audit_union["flat_rankings"], flat)
        self.assertEqual(audit_union["candidate_union"], flat)

    def test_shadow_union_never_removes_or_reranks_flat_result(self):
        flat = [
            {
                "candidate_id": self.rows[1]["candidate_id"],
                "score": 0.125,
                "opaque": {"rank": 1, "bytes": "00ff"},
            },
            {
                "candidate_id": self.candidate_id,
                "score": 0.875,
                "opaque": {"rank": 2, "bytes": "ff00"},
            },
        ]
        frozen = copy.deepcopy(flat)
        metadata = [
            {
                "lineage_id": "metadata-only-lineage",
                "candidate_id": "metadata-only-candidate",
                "candidate_ids": ["metadata-only-candidate"],
                "score": 99.0,
                "family_scores": {"concept": 99.0},
            }
        ]

        union = history_metadata.union_shadow(flat, metadata)

        self.assertEqual(flat, frozen)
        self.assertEqual(union["flat_rankings"], frozen)
        self.assertEqual(union["candidate_union"][:2], frozen)
        self.assertEqual(
            [item["candidate_id"] for item in union["candidate_union"]],
            [
                self.rows[1]["candidate_id"],
                self.candidate_id,
                "metadata-only-candidate",
            ],
        )
        self.assertEqual(union["metadata_rankings"], metadata)

    def test_deleted_randomized_and_stale_tags_preserve_flat_recall(self):
        self._register()
        self._publish([annotation("free_tag", "correct-tag")])
        self._register("profile-v2", "2", "2", supersedes="profile-v1")
        stale_rank = history_metadata.shadow_rank(
            self.conn,
            [{"family": "free_tag", "value": "correct-tag", "rank": 1}],
            self.snapshot,
            ["profile-v1"],
        )
        randomized_rank = history_metadata.shadow_rank(
            self.conn,
            [{"family": "free_tag", "value": "random-tag", "rank": 1}],
            self.snapshot,
            ["profile-v2"],
        )
        flat = [{"candidate_id": self.candidate_id, "score": 0.5}]
        for metadata in ([], stale_rank, randomized_rank):
            with self.subTest(metadata=metadata):
                union = history_metadata.union_shadow(flat, metadata)
                self.assertEqual(union["flat_rankings"], flat)
                self.assertEqual(union["candidate_union"][:1], flat)

    def test_many_tags_versions_and_revisions_vote_once_per_lineage(self):
        revision_id, revision_sha, revision_snapshot = self._insert_revision()
        self._register()
        repeated = [
            annotation("free_tag", "robotics"),
            annotation("free_tag", "robotics"),
            annotation("free_tag", "robotics", 0.7),
        ]
        self._publish(repeated)
        self._publish(
            repeated,
            candidate_id=revision_id,
            content_sha=revision_sha,
            token="worker-revision",
        )

        old_snapshot_rankings = history_metadata.shadow_rank(
            self.conn,
            [{"family": "free_tag", "value": "robotics", "rank": 1}],
            self.snapshot,
            ["profile-v1"],
        )
        self.assertEqual(
            old_snapshot_rankings[0]["candidate_ids"], [self.candidate_id]
        )

        rankings = history_metadata.shadow_rank(
            self.conn,
            [{"family": "free_tag", "value": "robotics", "rank": 1}],
            revision_snapshot,
            ["profile-v1"],
        )

        self.assertEqual(len(rankings), 1)
        self.assertEqual(rankings[0]["lineage_id"], self.lineage_id)
        self.assertEqual(
            set(rankings[0]["candidate_ids"]),
            {self.candidate_id, revision_id},
        )
        self.assertEqual(rankings[0]["family_scores"], {"free_tag": 1 / 61})
        self.assertEqual(rankings[0]["score"], 1 / 61)

    def test_metadata_profile_change_stales_only_metadata_generation(self):
        self._register()
        self._publish([annotation("concept", "control-surface")])
        canonical_before = [
            tuple(row)
            for row in self.conn.execute(
                "SELECT candidate_id, raw_sha256, source_sequence, raw_row "
                "FROM candidates ORDER BY source_sequence"
            )
        ]
        projection_before = [
            tuple(row)
            for row in self.conn.execute(
                "SELECT * FROM search_projection_outbox ORDER BY record_id"
            )
        ]
        audit_before = [
            tuple(row)
            for row in self.conn.execute(
                "SELECT snapshot_id, snapshot_hash, expected_asset_ids_hash "
                "FROM audit_snapshots ORDER BY snapshot_id"
            )
        ]

        self._register("profile-v2", "2", "2", supersedes="profile-v1")

        self.assertEqual(
            [
                tuple(row)
                for row in self.conn.execute(
                    "SELECT candidate_id, raw_sha256, source_sequence, raw_row "
                    "FROM candidates ORDER BY source_sequence"
                )
            ],
            canonical_before,
        )
        self.assertEqual(
            [tuple(row) for row in self.conn.execute(
                "SELECT * FROM search_projection_outbox ORDER BY record_id"
            )],
            projection_before,
        )
        self.assertEqual(
            [tuple(row) for row in self.conn.execute(
                "SELECT snapshot_id, snapshot_hash, expected_asset_ids_hash "
                "FROM audit_snapshots ORDER BY snapshot_id"
            )],
            audit_before,
        )
        states = list(self.conn.execute(
            "SELECT profile_id, state FROM audit_metadata_profile_events_v2 "
            "ORDER BY event_sequence"
        ))
        self.assertEqual(
            [(row["profile_id"], row["state"]) for row in states],
            [("profile-v1", "current"), ("profile-v1", "stale"),
             ("profile-v2", "current")],
        )

    def test_direction_assignment_does_not_become_global_concept(self):
        self._register()
        direction = {
            "run_id": "direction-run-a",
            "batch_id": "direction-batch",
            "direction_id": "shared-direction-id",
            "contract_sha": "3" * 64,
            "validator_version": "direction-validator-v1",
            "artifact_sha": "4" * 64,
        }
        self._publish(
            [annotation("direction", "axis-a", direction_identity=direction)]
        )
        stored = self.conn.execute(
            "SELECT family, direction_identity_json "
            "FROM audit_annotation_versions_v2"
        ).fetchone()
        self.assertEqual(stored["family"], "direction")
        self.assertEqual(json.loads(stored["direction_identity_json"]), direction)

        no_scope = history_metadata.shadow_rank(
            self.conn,
            [{"family": "direction", "value": "axis-a", "rank": 1}],
            self.snapshot,
            ["profile-v1"],
        )
        foreign_direction = dict(direction, run_id="direction-run-b")
        wrong_run = history_metadata.shadow_rank(
            self.conn,
            [{
                "family": "direction",
                "value": "axis-a",
                "rank": 1,
                "direction_identity": foreign_direction,
            }],
            self.snapshot,
            ["profile-v1"],
        )
        exact_scope = history_metadata.shadow_rank(
            self.conn,
            [{
                "family": "direction",
                "value": "axis-a",
                "rank": 1,
                "direction_identity": direction,
            }],
            self.snapshot,
            ["profile-v1"],
        )
        self.assertEqual(no_scope, [])
        self.assertEqual(wrong_run, [])
        self.assertEqual(len(exact_scope), 1)
        with self.assertRaises(ValueError):
            self._register("profile-direction", "2", "2")
            self._publish(
                [annotation(
                    "concept", "axis-a", direction_identity=direction
                )],
                profile_id="profile-direction",
                token="worker-direction",
            )

    def test_outbox_claim_rejects_stale_fence_and_recovers_expired_claim(self):
        self._register()
        work = self._enqueue()
        first = self._claim(
            work,
            token="worker-old",
            lease="2026-08-03T00:00:01Z",
            now="2026-08-03T00:00:00Z",
        )
        with self.assertRaises(history_audit_store.StaleFence):
            history_metadata.claim_candidate(
                self.conn,
                work["outbox_id"],
                "worker-early",
                "2026-08-03T00:00:03Z",
                now="2026-08-03T00:00:00Z",
            )
        recovered = history_metadata.claim_candidate(
            self.conn,
            work["outbox_id"],
            "worker-new",
            "2099-01-01T00:00:00Z",
            now="2026-08-03T00:00:02Z",
        )
        self.assertEqual(recovered["fence"], first["fence"] + 1)
        with self.assertRaises(history_audit_store.StaleFence):
            history_metadata.publish_annotations(
                self.conn, first, [annotation("free_tag", "stale-worker")]
            )
        result = history_metadata.publish_annotations(
            self.conn, recovered, [annotation("free_tag", "current-worker")]
        )
        self.assertEqual(result["published_count"], 1)
        row = self.conn.execute(
            "SELECT state, fence, claim_token, lease_until "
            "FROM audit_metadata_outbox_v2 WHERE outbox_id=?",
            (work["outbox_id"],),
        ).fetchone()
        self.assertEqual(tuple(row), ("done", recovered["fence"] + 1, None, None))


if __name__ == "__main__":
    unittest.main()
