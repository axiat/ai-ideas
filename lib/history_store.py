#!/usr/bin/env python3
"""Canonical historical-idea storage and fenced TSV projection."""

import base64
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import secrets
import sqlite3
import stat
import tempfile
import time
import unicodedata


CANONICAL_VERSION = "canonical-story-v1"
SCHEMA_VERSION = 1
BOOTSTRAP_MARKER_KEY = "bootstrap_complete_v1"
BOOTSTRAP_MARKER_FIELDS = frozenset(
    {
        "import_epoch_id",
        "ledger_row_count",
        "ledger_sha256",
        "marker_sha256",
        "near_sa_sha256",
        "schema_version",
    }
)
BOOTSTRAP_PROVENANCE_FIELDS = frozenset(
    {
        "import_epoch",
        "ledger_sha256",
        "marker_key",
        "marker_sha256",
        "near_sa_observation_count",
        "near_sa_observations",
        "near_sa_observations_sha256",
        "near_sa_sha256",
        "schema_version",
    }
)
BOOTSTRAP_EPOCH_FIELDS = (
    "epoch_id",
    "input_manifest_sha256",
    "plan_sha256",
    "state",
    "row_count",
    "result_sha256",
    "projection_sequence",
    "committed_at",
)
BOOTSTRAP_OBSERVATION_FIELDS = (
    "observation_id",
    "candidate_id",
    "source_sequence",
    "sa_votes",
    "vote_vector",
    "overlap",
    "category",
    "reason",
    "observed_at",
    "candidate_theme",
    "candidate_source",
    "candidate_verdict",
)
HEADER = b"date\tsource\ttheme\tidea\tverdict\treason\toverlap\tcategory\n"
ALLOWED_RELATIONS = ("evolved_from", "recheck_of", "supersedes")
ALLOWED_EDGE_AUTHORITIES = ("explicit", "manual_mapping", "promotion")
TARGET_NAMES = ("ledger.tsv", "tmp/ledger.good")
LEASE_SECONDS = 60
IMPORT_PLAN_FIELDS = (
    "schema_version",
    "input_manifest_sha256",
    "ledger_instance_id",
    "ledger_instance_sha256",
    "ledger_sha256",
    "header_b64",
    "run_metadata",
    "rows",
    "edges",
)


class HistoryStoreError(RuntimeError):
    pass


class ImportConflict(HistoryStoreError):
    pass


class LineageCycle(HistoryStoreError):
    pass


class ProjectionConflict(HistoryStoreError):
    pass


class StaleClaim(HistoryStoreError):
    pass


class InjectedCrash(HistoryStoreError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS bootstrap_complete_v1_immutable_insert
BEFORE INSERT ON schema_meta
WHEN NEW.key = 'bootstrap_complete_v1'
  AND EXISTS(
    SELECT 1 FROM schema_meta WHERE key = 'bootstrap_complete_v1'
  )
BEGIN
  SELECT RAISE(ABORT, 'bootstrap marker is insert-once');
END;
CREATE TRIGGER IF NOT EXISTS bootstrap_complete_v1_immutable_update
BEFORE UPDATE ON schema_meta
WHEN OLD.key = 'bootstrap_complete_v1'
BEGIN
  SELECT RAISE(ABORT, 'bootstrap marker is immutable');
END;
CREATE TRIGGER IF NOT EXISTS bootstrap_complete_v1_immutable_update_key
BEFORE UPDATE ON schema_meta
WHEN NEW.key = 'bootstrap_complete_v1'
BEGIN
  SELECT RAISE(ABORT, 'bootstrap marker key is reserved');
END;
CREATE TRIGGER IF NOT EXISTS bootstrap_complete_v1_immutable_delete
BEFORE DELETE ON schema_meta
WHEN OLD.key = 'bootstrap_complete_v1'
BEGIN
  SELECT RAISE(ABORT, 'bootstrap marker is immutable');
END;
CREATE TABLE IF NOT EXISTS import_epochs(
  epoch_id TEXT PRIMARY KEY,
  input_manifest_sha256 TEXT NOT NULL UNIQUE,
  plan_sha256 TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL CHECK(state = 'done'),
  row_count INTEGER NOT NULL CHECK(row_count >= 0),
  result_sha256 TEXT NOT NULL,
  projection_sequence INTEGER NOT NULL CHECK(projection_sequence >= 1),
  committed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bootstrap_provenance(
  marker_key TEXT PRIMARY KEY
    CHECK(marker_key = 'bootstrap_complete_v1'),
  provenance_sha256 TEXT NOT NULL UNIQUE,
  provenance_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS bootstrap_provenance_insert_once
BEFORE INSERT ON bootstrap_provenance
WHEN EXISTS(
  SELECT 1 FROM bootstrap_provenance
  WHERE marker_key = 'bootstrap_complete_v1'
)
BEGIN
  SELECT RAISE(ABORT, 'bootstrap provenance is insert-once');
END;
CREATE TRIGGER IF NOT EXISTS bootstrap_provenance_immutable_update
BEFORE UPDATE ON bootstrap_provenance
BEGIN
  SELECT RAISE(ABORT, 'bootstrap provenance is immutable');
END;
CREATE TRIGGER IF NOT EXISTS bootstrap_provenance_immutable_delete
BEFORE DELETE ON bootstrap_provenance
BEGIN
  SELECT RAISE(ABORT, 'bootstrap provenance is immutable');
END;
CREATE TABLE IF NOT EXISTS lineages(
  lineage_id TEXT PRIMARY KEY,
  root_candidate_id TEXT NOT NULL UNIQUE,
  UNIQUE(lineage_id, root_candidate_id),
  FOREIGN KEY(root_candidate_id, lineage_id)
    REFERENCES candidates(candidate_id, lineage_id)
    DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE IF NOT EXISTS candidates(
  candidate_id TEXT PRIMARY KEY,
  origin_stable_id TEXT UNIQUE,
  lineage_id TEXT NOT NULL REFERENCES lineages(lineage_id),
  row_number INTEGER,
  raw_sha256 TEXT NOT NULL,
  field_count INTEGER NOT NULL CHECK(field_count IN (7, 8)),
  date TEXT NOT NULL,
  source TEXT NOT NULL,
  theme TEXT NOT NULL,
  story TEXT NOT NULL,
  verdict TEXT NOT NULL,
  reason TEXT NOT NULL,
  overlap TEXT NOT NULL,
  category TEXT NOT NULL,
  source_sequence INTEGER NOT NULL UNIQUE,
  raw_row BLOB NOT NULL,
  row_terminator BLOB NOT NULL,
  provenance_json TEXT NOT NULL,
  UNIQUE(candidate_id, lineage_id),
  UNIQUE(candidate_id, source_sequence)
);
CREATE TRIGGER IF NOT EXISTS candidates_insert_once
BEFORE INSERT ON candidates
WHEN EXISTS(
  SELECT 1 FROM candidates
  WHERE candidate_id = NEW.candidate_id
     OR source_sequence = NEW.source_sequence
     OR (
       NEW.origin_stable_id IS NOT NULL
       AND origin_stable_id = NEW.origin_stable_id
     )
)
BEGIN
  SELECT CASE
    WHEN EXISTS(
      SELECT 1 FROM candidates
      WHERE candidate_id = NEW.candidate_id
        AND origin_stable_id IS NEW.origin_stable_id
        AND lineage_id = NEW.lineage_id
        AND row_number IS NEW.row_number
        AND raw_sha256 = NEW.raw_sha256
        AND field_count = NEW.field_count
        AND date = NEW.date
        AND source = NEW.source
        AND theme = NEW.theme
        AND story = NEW.story
        AND verdict = NEW.verdict
        AND reason = NEW.reason
        AND overlap = NEW.overlap
        AND category = NEW.category
        AND source_sequence = NEW.source_sequence
        AND raw_row = NEW.raw_row
        AND row_terminator = NEW.row_terminator
        AND provenance_json = NEW.provenance_json
    )
    THEN RAISE(IGNORE)
    ELSE RAISE(ABORT, 'candidate identity is immutable')
  END;
END;
CREATE TRIGGER IF NOT EXISTS candidates_immutable_update
BEFORE UPDATE ON candidates
BEGIN
  SELECT RAISE(ABORT, 'candidate is immutable');
END;
CREATE TRIGGER IF NOT EXISTS candidates_immutable_delete
BEFORE DELETE ON candidates
BEGIN
  SELECT RAISE(ABORT, 'candidate is immutable');
END;
CREATE TABLE IF NOT EXISTS story_aliases(
  canonical_version TEXT NOT NULL,
  canonical_hash TEXT NOT NULL,
  canonical_story TEXT NOT NULL,
  lineage_id TEXT NOT NULL REFERENCES lineages(lineage_id),
  PRIMARY KEY(canonical_version, canonical_hash),
  UNIQUE(canonical_version, canonical_story)
);
CREATE TABLE IF NOT EXISTS invocations(
  invocation_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  role TEXT NOT NULL,
  backend TEXT NOT NULL,
  state TEXT NOT NULL
    CHECK(state IN ('prepared','started','completed','installed','failed')),
  process_instance_id TEXT NOT NULL,
  context_id TEXT NOT NULL,
  session_lineage_id TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  input_manifest_sha256 TEXT NOT NULL,
  output_artifact_id TEXT REFERENCES artifacts(artifact_id),
  idempotency_key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS artifacts(
  artifact_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('prepared','installed','archived')),
  sha256 TEXT NOT NULL,
  byte_count INTEGER NOT NULL CHECK(byte_count >= 0),
  source_path TEXT NOT NULL,
  source_sequence INTEGER NOT NULL,
  producer_invocation_id TEXT REFERENCES invocations(invocation_id),
  provenance_json TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS candidate_facets(
  candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  facet TEXT NOT NULL,
  text TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  source_artifact_id TEXT REFERENCES artifacts(artifact_id),
  PRIMARY KEY(candidate_id, facet)
);
CREATE TABLE IF NOT EXISTS search_exclusions(
  candidate_id TEXT PRIMARY KEY REFERENCES candidates(candidate_id),
  exclusion_reason TEXT NOT NULL,
  excluded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lineage_edges(
  parent_candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  child_candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  relation_type TEXT NOT NULL
    CHECK(relation_type IN ('evolved_from','recheck_of','supersedes')),
  evidence_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
  PRIMARY KEY(parent_candidate_id, child_candidate_id, relation_type),
  CHECK(parent_candidate_id <> child_candidate_id)
);
CREATE TABLE IF NOT EXISTS reviews(
  candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  seat INTEGER NOT NULL CHECK(seat >= 1),
  vote INTEGER NOT NULL CHECK(vote BETWEEN 0 AND 2),
  reason TEXT NOT NULL,
  artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
  producer_invocation_id TEXT NOT NULL REFERENCES invocations(invocation_id),
  policy_version TEXT NOT NULL,
  PRIMARY KEY(candidate_id, seat),
  UNIQUE(candidate_id, producer_invocation_id)
);
CREATE TABLE IF NOT EXISTS near_sa_observations(
  observation_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  source_sequence INTEGER NOT NULL,
  sa_votes INTEGER NOT NULL CHECK(sa_votes >= 0),
  vote_vector TEXT NOT NULL,
  overlap TEXT NOT NULL,
  category TEXT NOT NULL,
  reason TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  UNIQUE(candidate_id, source_sequence),
  FOREIGN KEY(candidate_id, source_sequence)
    REFERENCES candidates(candidate_id, source_sequence)
);
CREATE TRIGGER IF NOT EXISTS near_sa_candidate_identity_guard
BEFORE INSERT ON near_sa_observations
WHEN NOT EXISTS(
  SELECT 1 FROM candidates
  WHERE candidate_id = NEW.candidate_id
    AND source_sequence = NEW.source_sequence
)
BEGIN
  SELECT RAISE(ABORT, 'near-SA candidate identity mismatch');
END;
CREATE TRIGGER IF NOT EXISTS near_sa_observations_insert_once
BEFORE INSERT ON near_sa_observations
WHEN EXISTS(
  SELECT 1 FROM near_sa_observations
  WHERE observation_id = NEW.observation_id
     OR (
       candidate_id = NEW.candidate_id
       AND source_sequence = NEW.source_sequence
     )
)
BEGIN
  SELECT CASE
    WHEN EXISTS(
      SELECT 1 FROM near_sa_observations
      WHERE observation_id = NEW.observation_id
        AND candidate_id = NEW.candidate_id
        AND source_sequence = NEW.source_sequence
        AND sa_votes = NEW.sa_votes
        AND vote_vector = NEW.vote_vector
        AND overlap = NEW.overlap
        AND category = NEW.category
        AND reason = NEW.reason
        AND observed_at = NEW.observed_at
    )
    THEN RAISE(IGNORE)
    ELSE RAISE(ABORT, 'near-SA observation identity is immutable')
  END;
END;
CREATE TRIGGER IF NOT EXISTS near_sa_observations_immutable_update
BEFORE UPDATE ON near_sa_observations
BEGIN
  SELECT RAISE(ABORT, 'near-SA observation is immutable');
END;
CREATE TRIGGER IF NOT EXISTS near_sa_observations_immutable_delete
BEFORE DELETE ON near_sa_observations
BEGIN
  SELECT RAISE(ABORT, 'near-SA observation is immutable');
END;
CREATE TABLE IF NOT EXISTS history_receipts(
  receipt_id TEXT PRIMARY KEY,
  query_candidate_id TEXT NOT NULL,
  intent TEXT NOT NULL CHECK(intent IN (
    'duplicate_search','evolution_search','failure_pattern_search'
  )),
  pack_sha256 TEXT NOT NULL,
  pack_publication_id TEXT NOT NULL REFERENCES history_pack_publications(publication_id),
  policy_sha256 TEXT NOT NULL,
  generation_manifest_sha256 TEXT NOT NULL,
  rank_trace_sha256 TEXT NOT NULL,
  comparator_invocation_sha256 TEXT NOT NULL,
  comparator_preflight_sha256 TEXT NOT NULL,
  retrieval_policy_version TEXT NOT NULL,
  source_watermark INTEGER NOT NULL,
  index_generation INTEGER NOT NULL,
  comparator_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN (
    'complete_match','complete_no_match','uncertain','partial',
    'backend_failed','budget_exceeded','conflicting_evidence'
  )),
  receipt_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_round_commits(
  commit_key TEXT PRIMARY KEY,
  request_sha256 TEXT NOT NULL,
  request_json TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS runtime_round_commits_immutable_update
BEFORE UPDATE ON runtime_round_commits
BEGIN
  SELECT RAISE(ABORT, 'runtime round commits are immutable');
END;
CREATE TRIGGER IF NOT EXISTS runtime_round_commits_immutable_delete
BEFORE DELETE ON runtime_round_commits
BEGIN
  SELECT RAISE(ABORT, 'runtime round commits are immutable');
END;
CREATE TABLE IF NOT EXISTS history_generation_provenance(
  generation INTEGER PRIMARY KEY CHECK(generation >= 1),
  manifest_sha256 TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  source_watermark INTEGER NOT NULL CHECK(source_watermark >= 0),
  policy_sha256 TEXT NOT NULL,
  projection_schema_version TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history_pack_publications(
  publication_id TEXT PRIMARY KEY,
  pack_sha256 TEXT NOT NULL UNIQUE,
  pack_bytes BLOB NOT NULL,
  policy_sha256 TEXT NOT NULL,
  generation INTEGER NOT NULL REFERENCES history_generation_provenance(generation),
  generation_manifest_sha256 TEXT NOT NULL,
  source_watermark INTEGER NOT NULL CHECK(source_watermark >= 0),
  retrieval_status TEXT NOT NULL CHECK(retrieval_status IN (
    'complete','partial','backend_failed','budget_exceeded'
  )),
  rank_trace_json TEXT NOT NULL,
  rank_trace_sha256 TEXT NOT NULL,
  comparator_invocation_json TEXT NOT NULL,
  comparator_invocation_sha256 TEXT NOT NULL,
  comparator_preflight_json TEXT NOT NULL,
  comparator_preflight_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS history_pack_provenance_guard
BEFORE INSERT ON history_pack_publications
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM history_generation_provenance p
    WHERE p.generation = NEW.generation
      AND p.manifest_sha256 = NEW.generation_manifest_sha256
      AND p.source_watermark = NEW.source_watermark
      AND p.policy_sha256 = NEW.policy_sha256
  ) THEN RAISE(ABORT, 'pack provenance mismatch') END;
END;
CREATE TRIGGER IF NOT EXISTS history_receipt_pack_guard
BEFORE INSERT ON history_receipts
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM history_pack_publications p
    WHERE p.publication_id = NEW.pack_publication_id
      AND p.pack_sha256 = NEW.pack_sha256
      AND p.policy_sha256 = NEW.policy_sha256
      AND p.generation_manifest_sha256 = NEW.generation_manifest_sha256
      AND p.rank_trace_sha256 = NEW.rank_trace_sha256
      AND p.comparator_invocation_sha256 = NEW.comparator_invocation_sha256
      AND p.comparator_preflight_sha256 = NEW.comparator_preflight_sha256
      AND p.source_watermark = NEW.source_watermark
      AND p.generation = NEW.index_generation
  ) THEN RAISE(ABORT, 'receipt pack mismatch') END;
END;
CREATE TABLE IF NOT EXISTS search_projection_outbox(
  record_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  projection_kind TEXT NOT NULL,
  content_version TEXT NOT NULL,
  canonical_revision INTEGER NOT NULL CHECK(canonical_revision >= 0),
  source_sequence INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','processing','done')),
  generation INTEGER NOT NULL CHECK(generation >= 0),
  claim_token TEXT,
  lease_until TEXT,
  CHECK(
    (state = 'processing' AND claim_token IS NOT NULL AND lease_until IS NOT NULL)
    OR
    (state IN ('pending','done') AND claim_token IS NULL AND lease_until IS NULL)
  ),
  PRIMARY KEY(record_id, projection_kind, content_version)
);
CREATE TABLE IF NOT EXISTS ledger_projection_outbox(
  projection_sequence INTEGER PRIMARY KEY,
  snapshot_sha256 TEXT NOT NULL,
  row_count INTEGER NOT NULL CHECK(row_count >= 0),
  state TEXT NOT NULL CHECK(state IN ('pending','processing','done')),
  generation INTEGER NOT NULL CHECK(generation >= 0),
  claim_token TEXT,
  lease_until TEXT,
  satisfied_by_sequence INTEGER,
  satisfied_by_sha256 TEXT,
  completed_at TEXT,
  CHECK(
    (state = 'processing' AND claim_token IS NOT NULL AND lease_until IS NOT NULL)
    OR
    (state IN ('pending','done') AND claim_token IS NULL AND lease_until IS NULL)
  ),
  CHECK(
    (state = 'done' AND satisfied_by_sequence IS NOT NULL
      AND satisfied_by_sha256 IS NOT NULL AND completed_at IS NOT NULL)
    OR
    (state IN ('pending','processing') AND satisfied_by_sequence IS NULL
      AND satisfied_by_sha256 IS NULL AND completed_at IS NULL)
  ),
  CHECK(satisfied_by_sequence IS NULL
        OR satisfied_by_sequence >= projection_sequence),
  UNIQUE(projection_sequence, snapshot_sha256),
  FOREIGN KEY(satisfied_by_sequence, satisfied_by_sha256)
    REFERENCES ledger_projection_outbox(projection_sequence, snapshot_sha256)
);
CREATE TABLE IF NOT EXISTS ledger_projection_receipts(
  projection_sequence INTEGER NOT NULL
    REFERENCES ledger_projection_outbox(projection_sequence),
  target TEXT NOT NULL CHECK(target IN ('ledger.tsv','tmp/ledger.good')),
  published_sequence INTEGER NOT NULL,
  snapshot_sha256 TEXT NOT NULL,
  receipt_sha256 TEXT NOT NULL,
  installed_at TEXT NOT NULL,
  PRIMARY KEY(projection_sequence, target),
  FOREIGN KEY(published_sequence, snapshot_sha256)
    REFERENCES ledger_projection_outbox(projection_sequence, snapshot_sha256)
);
"""


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _json_bytes(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _db_path(conn):
    for _, name, path in conn.execute("PRAGMA database_list"):
        if name == "main":
            return pathlib.Path(path)
    raise HistoryStoreError("main database path is unavailable")


def connect(path):
    db = pathlib.Path(path)
    _durable_mkdir(db.parent)
    conn = sqlite3.connect(str(db), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA recursive_triggers = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def init_audit_schema_v2(conn):
    """Explicitly apply the sibling v2 audit schema to an idle connection."""
    try:
        from lib import history_audit_store
    except ImportError:  # Direct imports with lib/ on sys.path.
        import history_audit_store
    history_audit_store.init_schema(conn)


def _rename_unverified_table(conn, table):
    target = table + "_legacy_unverified"
    existing = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (target,),
    ).fetchone()
    if existing is not None:
        target += "_" + _sha(table.encode("utf-8"))[:8]
    conn.execute("ALTER TABLE %s RENAME TO %s" % (table, target))


def _migrate_unverified_history_audit(conn):
    publication = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name = 'history_pack_publications'"
    ).fetchone()
    if publication is not None:
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(history_pack_publications)"
            )
        }
        required = {
            "rank_trace_sha256",
            "comparator_invocation_json",
            "comparator_invocation_sha256",
            "comparator_preflight_sha256",
        }
        if not required.issubset(columns):
            conn.execute("DROP TRIGGER IF EXISTS history_receipt_pack_guard")
            conn.execute(
                "DROP TRIGGER IF EXISTS history_receipt_update_guard"
            )
            conn.execute(
                "DROP TRIGGER IF EXISTS history_receipt_delete_guard"
            )
            conn.execute(
                "DROP TRIGGER IF EXISTS history_pack_provenance_guard"
            )
            conn.execute(
                "DROP TRIGGER IF EXISTS history_pack_publication_update_guard"
            )
            conn.execute(
                "DROP TRIGGER IF EXISTS history_pack_publication_delete_guard"
            )
            receipt = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'history_receipts'"
            ).fetchone()
            if receipt is not None:
                _rename_unverified_table(conn, "history_receipts")
            _rename_unverified_table(conn, "history_pack_publications")


def _migrate_unverified_history_receipts(conn):
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'history_receipts'"
    ).fetchone()
    if exists is None:
        return
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(history_receipts)")
    }
    required = {
        "pack_publication_id",
        "policy_sha256",
        "generation_manifest_sha256",
        "rank_trace_sha256",
        "comparator_invocation_sha256",
        "comparator_preflight_sha256",
    }
    if not required.issubset(columns):
        conn.execute("DROP TRIGGER IF EXISTS history_receipt_pack_guard")
        _rename_unverified_table(conn, "history_receipts")


def init_schema(conn):
    _migrate_unverified_history_audit(conn)
    _migrate_unverified_history_receipts(conn)
    conn.executescript(SCHEMA)
    conn.execute(
        "DROP TRIGGER IF EXISTS history_pack_publication_update_guard"
    )
    pack_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(history_pack_publications)")
    }
    if "rank_trace_json" not in pack_columns:
        conn.execute(
            "ALTER TABLE history_pack_publications "
            "ADD COLUMN rank_trace_json TEXT NOT NULL DEFAULT "
            """'{"channels":{},"contributions":[],"fusion":"""
            """{"candidate_order":[],"lineage_order":[]}}'"""
        )
    if "rank_trace_sha256" not in pack_columns:
        conn.execute(
            "ALTER TABLE history_pack_publications "
            "ADD COLUMN rank_trace_sha256 TEXT NOT NULL DEFAULT ''"
        )
    if "comparator_preflight_sha256" not in pack_columns:
        conn.execute(
            "ALTER TABLE history_pack_publications "
            "ADD COLUMN comparator_preflight_sha256 TEXT NOT NULL DEFAULT ''"
        )
    for row in conn.execute(
        """
        SELECT publication_id, rank_trace_json, comparator_preflight_json
        FROM history_pack_publications
        WHERE rank_trace_sha256 = '' OR comparator_preflight_sha256 = ''
        """
    ):
        try:
            trace = json.loads(row["rank_trace_json"])
            preflight = json.loads(row["comparator_preflight_json"])
        except (TypeError, ValueError) as exc:
            raise HistoryStoreError("published retrieval audit is invalid") from exc
        trace_bytes = _json_bytes(trace)
        preflight_bytes = _json_bytes(preflight)
        conn.execute(
            """
            UPDATE history_pack_publications
            SET rank_trace_json = ?, rank_trace_sha256 = ?,
                comparator_preflight_json = ?,
                comparator_preflight_sha256 = ?
            WHERE publication_id = ?
            """,
            (
                trace_bytes.decode("utf-8"),
                _sha(trace_bytes),
                preflight_bytes.decode("utf-8"),
                _sha(preflight_bytes),
                row["publication_id"],
            ),
        )
    conn.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS history_pack_publication_update_guard
        BEFORE UPDATE ON history_pack_publications
        BEGIN
          SELECT RAISE(ABORT, 'history pack publication is immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS history_pack_publication_delete_guard
        BEFORE DELETE ON history_pack_publications
        BEGIN
          SELECT RAISE(ABORT, 'history pack publication is immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS history_receipt_update_guard
        BEFORE UPDATE ON history_receipts
        BEGIN
          SELECT RAISE(ABORT, 'history receipt is immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS history_receipt_delete_guard
        BEFORE DELETE ON history_receipts
        BEGIN
          SELECT RAISE(ABORT, 'history receipt is immutable');
        END;
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('schema_version', '1')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('projection_sequence', '0')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) "
        "VALUES('history_index_generation_sequence', '0')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) "
        "VALUES('history_search_content_revision', '0')"
    )
    outbox_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(search_projection_outbox)")
    }
    if "canonical_revision" not in outbox_columns:
        conn.execute(
            "ALTER TABLE search_projection_outbox "
            "ADD COLUMN canonical_revision INTEGER NOT NULL DEFAULT 0"
        )
    generation_values = []
    for key in (
        "history_index_generation",
        "history_index_generation_sequence",
    ):
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?", (key,)
        ).fetchone()
        if row is not None:
            generation_values.append(int(row[0]))
    generation_values.append(
        int(
            conn.execute(
                "SELECT COALESCE(MAX(generation), 0) "
                "FROM history_generation_provenance"
            ).fetchone()[0]
        )
    )
    if conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'search_index_generations'
        """
    ).fetchone():
        generation_values.append(
            int(
                conn.execute(
                    "SELECT COALESCE(MAX(generation), 0) "
                    "FROM search_index_generations"
                ).fetchone()[0]
            )
        )
    conn.execute(
        """
        UPDATE schema_meta
        SET value = ?
        WHERE key = 'history_index_generation_sequence'
        """,
        (str(max(generation_values)),),
    )


def canonical_story_v1(text):
    if not isinstance(text, str):
        raise TypeError("story must be text")
    return " ".join(unicodedata.normalize("NFC", text).split())


def _strip_one_terminator(raw_row):
    if raw_row.endswith(b"\r\n"):
        return raw_row[:-2]
    if raw_row.endswith(b"\n"):
        return raw_row[:-1]
    return raw_row


def origin_stable_id(ledger_instance_id, row_number, raw_row):
    instance = ledger_instance_id.strip()
    if not instance or "\n" in instance or "\r" in instance:
        raise ValueError("ledger instance ID must be one nonempty normalized line")
    if type(row_number) is not int:
        raise TypeError("data row ordinal must be an integer")
    if row_number < 1:
        raise ValueError("data row ordinal must be positive")
    raw_sha = _sha(_strip_one_terminator(bytes(raw_row)))
    material = (
        b"tsv-row-v2\0"
        + instance.encode("utf-8")
        + b"\0"
        + str(row_number).encode("ascii")
        + b"\0"
        + raw_sha.encode("ascii")
    )
    return _sha(material)


def _candidate_id(origin_id):
    return _sha(b"candidate-import-v1\0" + origin_id.encode("ascii"))


def _lineage_id(canonical_story):
    return _sha(b"tsv-v1\0" + canonical_story.encode("utf-8"))


def _canonical_hash(canonical_story):
    return _sha(canonical_story.encode("utf-8"))


def _read_instance_id(value):
    if value is None:
        raise ImportConflict("ledger.instance-id is required")
    if isinstance(value, (pathlib.Path, os.PathLike)):
        data = _read_regular_file_nofollow(
            pathlib.Path(value), "ledger.instance-id"
        )
        try:
            raw = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ImportConflict(
                "ledger.instance-id must be UTF-8"
            ) from exc
    elif isinstance(value, bytes):
        try:
            raw = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ImportConflict(
                "ledger.instance-id must be UTF-8"
            ) from exc
    else:
        raw = str(value)
    normalized = raw.strip()
    if not normalized or "\n" in normalized or "\r" in normalized:
        raise ImportConflict("ledger.instance-id must contain one nonempty line")
    return normalized


def _split_physical_lines(data):
    lines = data.splitlines(keepends=True)
    if not lines:
        raise ImportConflict("ledger is empty")
    result = []
    for line in lines:
        if line.endswith(b"\r\n"):
            result.append((line[:-2], b"\r\n"))
        elif line.endswith(b"\n"):
            result.append((line[:-1], b"\n"))
        else:
            result.append((line, b""))
    return result


def _parse_row(raw, row_number, terminator=b"\n"):
    try:
        fields = raw.decode("utf-8").split("\t")
    except UnicodeDecodeError as exc:
        raise ImportConflict(f"row {row_number} is not UTF-8") from exc
    field_count = len(fields)
    if field_count not in (7, 8):
        raise ImportConflict(
            f"row {row_number} has {len(fields)} fields; expected 7 or 8"
        )
    if len(fields) == 7:
        fields.append("")
    canonical = canonical_story_v1(fields[3])
    if not canonical:
        raise ImportConflict(f"row {row_number} has an empty canonical story")
    return {
        "row_number": row_number,
        "raw_row_b64": base64.b64encode(raw).decode("ascii"),
        "row_terminator_b64": base64.b64encode(terminator).decode("ascii"),
        "raw_sha256": _sha(raw),
        "field_count": field_count,
        "date": fields[0],
        "source": fields[1],
        "theme": fields[2],
        "story": fields[3],
        "verdict": fields[4],
        "reason": fields[5],
        "overlap": fields[6],
        "category": fields[7],
        "canonical_story": canonical,
        "canonical_hash": _canonical_hash(canonical),
    }


def _read_regular_file_nofollow(path, description):
    source = pathlib.Path(path)
    try:
        before = os.lstat(str(source))
    except OSError as exc:
        raise ImportConflict(f"{description} is unavailable") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ImportConflict(f"{description} is not a regular file")
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(str(source), flags)
    except OSError as exc:
        raise ImportConflict(f"{description} cannot be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        current = os.lstat(str(source))
        snapshots = {
            (
                item.st_dev,
                item.st_ino,
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
            )
            for item in (before, opened, current)
        }
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or len(snapshots) != 1
        ):
            raise ImportConflict(f"{description} changed during safe open")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        finished = os.fstat(descriptor)
        after = os.lstat(str(source))
        snapshots.update(
            (
                item.st_dev,
                item.st_ino,
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
            )
            for item in (finished, after)
        )
        if (
            not stat.S_ISREG(finished.st_mode)
            or stat.S_ISLNK(after.st_mode)
            or not stat.S_ISREG(after.st_mode)
            or len(snapshots) != 1
        ):
            raise ImportConflict(f"{description} changed during safe read")
        return b"".join(chunks)
    except OSError as exc:
        raise ImportConflict(f"{description} cannot be read safely") from exc
    finally:
        os.close(descriptor)


def _read_content_addressed_file(
    path, expected_sha256, description, expected_bytes=None
):
    data = _read_regular_file_nofollow(path, description)
    if _sha(data) != expected_sha256 or (
        expected_bytes is not None and data != expected_bytes
    ):
        raise ImportConflict(f"{description} digest or content changed")
    return data


def _seal_object(path, cas_root):
    source = pathlib.Path(path)
    data = _read_regular_file_nofollow(source, "import input")
    return _seal_bytes(data, cas_root, str(source.absolute()))


def _seal_bytes(data, cas_root, source_path):
    data = bytes(data)
    digest = _sha(data)
    _durable_mkdir(cas_root)
    destination = cas_root / digest
    _write_immutable(destination, data)
    _read_content_addressed_file(
        destination,
        digest,
        "sealed CAS object",
        expected_bytes=data,
    )
    return {
        "source_path": str(source_path),
        "sha256": digest,
        "byte_count": len(data),
        "cas_path": str(destination.absolute()),
    }


def _fsync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_existing(path):
    existing = pathlib.Path(path)
    try:
        before = os.lstat(str(existing))
    except OSError as exc:
        raise HistoryStoreError(
            f"durable file is unavailable: {existing}"
        ) from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise HistoryStoreError(f"durable file is unsafe: {existing}")
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(str(existing), flags)
    except OSError as exc:
        raise HistoryStoreError(
            f"durable file cannot be opened safely: {existing}"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        current = os.lstat(str(existing))
        identity = {
            (item.st_dev, item.st_ino)
            for item in (before, opened, current)
        }
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or len(identity) != 1
        ):
            raise HistoryStoreError(
                f"durable file changed during safe open: {existing}"
            )
        os.fsync(descriptor)
        finished = os.fstat(descriptor)
        after = os.lstat(str(existing))
        identity.update(
            (item.st_dev, item.st_ino)
            for item in (finished, after)
        )
        if (
            not stat.S_ISREG(finished.st_mode)
            or stat.S_ISLNK(after.st_mode)
            or not stat.S_ISREG(after.st_mode)
            or len(identity) != 1
        ):
            raise HistoryStoreError(
                f"durable file changed during fsync: {existing}"
            )
    except OSError as exc:
        raise HistoryStoreError(
            f"durable file cannot be fsynced safely: {existing}"
        ) from exc
    finally:
        os.close(descriptor)
    _fsync_directory(existing.parent)


def _durable_mkdir(path):
    directory = pathlib.Path(path)
    if directory.exists():
        if directory.is_symlink() or not directory.is_dir():
            raise HistoryStoreError(f"durable directory is unsafe: {directory}")
        return
    if directory == directory.parent:
        raise HistoryStoreError(f"cannot create filesystem root {directory}")
    _durable_mkdir(directory.parent)
    try:
        os.mkdir(str(directory))
    except FileExistsError:
        if directory.is_symlink() or not directory.is_dir():
            raise HistoryStoreError(f"durable directory is unsafe: {directory}")
    _fsync_directory(directory.parent)


def _write_immutable(path, data):
    path = pathlib.Path(path)
    data = bytes(data)
    _durable_mkdir(path.parent)
    try:
        existing = os.lstat(str(path))
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise ImportConflict(
            f"immutable object cannot be inspected at {path}"
        ) from exc
    if existing is not None:
        if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(
            existing.st_mode
        ):
            raise ImportConflict(f"immutable object is unsafe at {path}")
        if _read_regular_file_nofollow(
            path, "immutable object"
        ) != data:
            raise ImportConflict(f"immutable object conflicts at {path}")
        _fsync_existing(path)
        if _read_regular_file_nofollow(
            path, "immutable object"
        ) != data:
            raise ImportConflict(f"immutable object changed at {path}")
        return
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            if _read_regular_file_nofollow(
                path, "immutable object"
            ) != data:
                raise ImportConflict(
                    f"immutable object conflicts at {path}"
                )
            _fsync_existing(path)
        _fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class _UnionFind:
    def __init__(self, values):
        self.parent = {value: value for value in values}

    def find(self, value):
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left, right):
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)


def _mapping_data(sealed_object):
    if not sealed_object:
        return None
    try:
        value = json.loads(
            _read_content_addressed_file(
                pathlib.Path(sealed_object["cas_path"]),
                sealed_object["sha256"],
                "sealed lineage mapping manifest",
            )
        )
    except (TypeError, ValueError) as exc:
        raise ImportConflict("lineage mapping manifest is not valid JSON") from exc
    if value.get("version") != "lineage-mapping-v1":
        raise ImportConflict("unsupported lineage mapping version")
    return value


def _union_source_data(sealed_object, source_kind):
    try:
        value = json.loads(
            _read_content_addressed_file(
                pathlib.Path(sealed_object["cas_path"]),
                sealed_object["sha256"],
                f"sealed {source_kind}",
            )
        )
    except (TypeError, ValueError) as exc:
        raise ImportConflict(f"{source_kind} is not valid JSON") from exc
    if source_kind == "parent_evidence":
        if value.get("version") != "history-parent-evidence-v1":
            raise ImportConflict("unsupported historical parent-evidence version")
        entries = value.get("edges", value.get("mappings", []))
        roots = value.get("roots", [])
        default_relation = "evolved_from"
        authority = "explicit"
    else:
        if value.get("version") != "promotion-attestation-v1":
            raise ImportConflict("unsupported promotion-attestation version")
        entries = value.get("edges", value.get("mappings"))
        if entries is None:
            entries = [
                {
                    "parent_row": value.get("origin_row_number"),
                    "child_row": value.get("committed_row_number"),
                }
            ]
        roots = value.get("roots", [])
        default_relation = "supersedes"
        authority = "promotion"
    normalized = []
    for entry in entries:
        if entry.get("parent_row") is None or entry.get("child_row") is None:
            raise ImportConflict(f"{source_kind} has an incomplete row relation")
        normalized.append(
            {
                "parent_row": entry["parent_row"],
                "child_row": entry["child_row"],
                "relation_type": entry.get("relation_type", default_relation),
                "authority": authority,
                "evidence_path": sealed_object["cas_path"],
                "root_row": entry.get("root_row"),
            }
        )
    return {"mappings": normalized, "roots": roots}


def _build_components(rows, mapping):
    by_row = {item["row_number"]: item for item in rows}
    stories = sorted({item["canonical_story"] for item in rows})
    union = _UnionFind(stories)
    explicit_edges = []
    indegree = {story: 0 for story in stories}
    parents = {story: set() for story in stories}
    children = {story: set() for story in stories}
    explicit_roots = {}
    if mapping:
        for entry in mapping.get("mappings", mapping.get("edges", [])):
            parent_number = int(entry.get("parent_row", entry.get("parent_ordinal", 0)))
            child_number = int(entry.get("child_row", entry.get("child_ordinal", 0)))
            if parent_number not in by_row or child_number not in by_row:
                raise ImportConflict("mapping references a missing data row")
            parent_story = by_row[parent_number]["canonical_story"]
            child_story = by_row[child_number]["canonical_story"]
            if parent_story == child_story:
                raise ImportConflict("explicit mapping duplicates an exact alias edge")
            parents[child_story].add(parent_story)
            if len(parents[child_story]) > 1:
                raise ImportConflict("mapping gives one story multiple explicit parents")
            children[parent_story].add(child_story)
            union.union(parent_story, child_story)
            indegree[child_story] = len(parents[child_story])
            explicit_edges.append(
                {
                    "parent_row": parent_number,
                    "child_row": child_number,
                    "relation_type": entry.get("relation_type", "evolved_from"),
                    "evidence_path": entry.get("evidence_path"),
                    "authority": entry.get("authority", "manual_mapping"),
                    "parent_story": parent_story,
                    "child_story": child_story,
                }
            )
            root_number = entry.get("root_row")
            if root_number is not None:
                root_number = int(root_number)
                if root_number not in by_row:
                    raise ImportConflict("mapping root references a missing data row")
                explicit_roots[parent_story] = by_row[root_number]["canonical_story"]
        for root in mapping.get("roots", []):
            root_number = int(root["row"])
            if root_number not in by_row:
                raise ImportConflict("mapping root references a missing data row")
            explicit_roots[by_row[root_number]["canonical_story"]] = by_row[root_number][
                "canonical_story"
            ]
    remaining = dict(indegree)
    frontier = sorted(story for story, degree in remaining.items() if degree == 0)
    visited = []
    while frontier:
        story = frontier.pop(0)
        visited.append(story)
        for child in sorted(children[story]):
            remaining[child] -= 1
            if remaining[child] == 0:
                frontier.append(child)
                frontier.sort()
    if len(visited) != len(stories):
        raise ImportConflict("mapping parent graph contains a cycle")
    topological_rank = {story: index for index, story in enumerate(visited)}
    explicit_edges.sort(
        key=lambda item: (
            topological_rank[item["parent_story"]],
            topological_rank[item["child_story"]],
            item["parent_row"],
            item["child_row"],
        )
    )
    for edge in explicit_edges:
        edge.pop("parent_story")
        edge.pop("child_story")
    components = {}
    for story in stories:
        components.setdefault(union.find(story), []).append(story)
    root_by_story = {}
    for component in components.values():
        named = {explicit_roots[item] for item in component if item in explicit_roots}
        parentless = [item for item in component if indegree[item] == 0]
        if len(named) > 1:
            raise ImportConflict("component has conflicting explicit roots")
        if named:
            root = next(iter(named))
            if root not in component:
                raise ImportConflict("explicit root is outside its component")
            if root not in parentless:
                raise ImportConflict("explicit root is not a parentless ancestor")
        elif len(parentless) == 1:
            root = parentless[0]
        elif len(component) == 1:
            root = component[0]
        else:
            raise ImportConflict(
                "mapping with distinct parentless stories requires an explicit root"
            )
        for story in component:
            root_by_story[story] = root
    return root_by_story, explicit_edges


def build_import_plan(inputs, state_root):
    if isinstance(inputs, (str, os.PathLike)):
        inputs = {"ledger": pathlib.Path(inputs)}
    else:
        inputs = dict(inputs)
    ledger = pathlib.Path(inputs["ledger"])
    state = pathlib.Path(state_root)
    cas_root = state / "import-cas"
    inline_instance = inputs.get("ledger_instance_id")
    if inline_instance is not None and not isinstance(
        inline_instance, (pathlib.Path, os.PathLike)
    ):
        if isinstance(inline_instance, bytes):
            instance_data = inline_instance
        else:
            instance_data = str(inline_instance).encode("utf-8")
        ledger_instance_id = _read_instance_id(instance_data)
        instance_object = _seal_bytes(
            instance_data,
            cas_root,
            "<inline:ledger-instance-id>",
        )
    else:
        instance_path = pathlib.Path(
            inline_instance
            if inline_instance is not None
            else inputs.get(
                "ledger_instance_id_path",
                ledger.parent / "ledger.instance-id",
            )
        )
        instance_object = _seal_object(instance_path, cas_root)
        instance_data = _read_content_addressed_file(
            instance_object["cas_path"],
            instance_object["sha256"],
            "sealed ledger.instance-id",
        )
        ledger_instance_id = _read_instance_id(instance_data)
    instance_object["role"] = "ledger-instance-id"
    ledger_object = _seal_object(ledger, cas_root)
    ledger_object["role"] = "ledger"
    sealed = [ledger_object, instance_object]
    mapping_object = None
    parent_objects = []
    promotion_objects = []
    if inputs.get("mapping_manifest"):
        mapping_object = _seal_object(inputs["mapping_manifest"], cas_root)
        mapping_object["role"] = "lineage-mapping"
        sealed.append(mapping_object)
    for key in ("parent_evidence", "promotion_receipts"):
        for index, source in enumerate(inputs.get(key, []), 1):
            item = _seal_object(source, cas_root)
            item["role"] = f"{key}:{index}"
            sealed.append(item)
            if key == "parent_evidence":
                parent_objects.append(item)
            else:
                promotion_objects.append(item)
    data = _read_content_addressed_file(
        ledger_object["cas_path"],
        ledger_object["sha256"],
        "sealed ledger",
    )
    physical = _split_physical_lines(data)
    header_raw, header_terminator = physical[0]
    if header_raw.split(b"\t") != HEADER.rstrip(b"\n").split(b"\t"):
        raise ImportConflict("ledger header does not match the eight-column contract")
    rows = [
        _parse_row(raw, index, terminator)
        for index, (raw, terminator) in enumerate(physical[1:], 1)
    ]
    if any(not raw for raw, _ in physical[1:]):
        raise ImportConflict("ledger contains a blank physical data row")
    explicit_mapping = _mapping_data(mapping_object)
    combined_mapping = {"version": "lineage-mapping-v1", "mappings": [], "roots": []}
    if explicit_mapping:
        combined_mapping["mappings"].extend(
            explicit_mapping.get(
                "mappings", explicit_mapping.get("edges", [])
            )
        )
        combined_mapping["roots"].extend(explicit_mapping.get("roots", []))
    for item in parent_objects:
        source_data = _union_source_data(item, "parent_evidence")
        combined_mapping["mappings"].extend(source_data["mappings"])
        combined_mapping["roots"].extend(source_data["roots"])
    for item in promotion_objects:
        source_data = _union_source_data(item, "promotion_receipt")
        combined_mapping["mappings"].extend(source_data["mappings"])
        combined_mapping["roots"].extend(source_data["roots"])
    mapping = (
        combined_mapping
        if combined_mapping["mappings"] or combined_mapping["roots"]
        else None
    )
    root_by_story, explicit_edges = _build_components(rows, mapping)
    for item in rows:
        origin = origin_stable_id(
            ledger_instance_id,
            item["row_number"],
            base64.b64decode(item["raw_row_b64"]),
        )
        item["origin_stable_id"] = origin
        item["candidate_id"] = _candidate_id(origin)
        item["root_story"] = root_by_story[item["canonical_story"]]
        item["lineage_id"] = _lineage_id(item["root_story"])
    first_by_story = {}
    for item in rows:
        first_by_story.setdefault(item["canonical_story"], item["candidate_id"])
    root_candidate_by_lineage = {}
    for item in rows:
        if item["canonical_story"] == item["root_story"]:
            root_candidate_by_lineage.setdefault(item["lineage_id"], item["candidate_id"])
    for item in rows:
        item["root_candidate_id"] = root_candidate_by_lineage[item["lineage_id"]]
    by_row = {item["row_number"]: item for item in rows}
    edges = []
    for entry in explicit_edges:
        if entry["relation_type"] not in ALLOWED_RELATIONS:
            raise ImportConflict("mapping contains an unsupported relation")
        if entry["authority"] not in ALLOWED_EDGE_AUTHORITIES:
            raise ImportConflict("mapping contains an unsupported authority")
        if not entry["evidence_path"]:
            raise ImportConflict("explicit mapping requires an evidence artifact")
        evidence = _seal_object(entry["evidence_path"], cas_root)
        evidence["role"] = (
            f"lineage-evidence:{entry['parent_row']}:{entry['child_row']}"
        )
        sealed.append(evidence)
        edges.append(
            {
                "parent_candidate_id": by_row[entry["parent_row"]]["candidate_id"],
                "child_candidate_id": by_row[entry["child_row"]]["candidate_id"],
                "relation_type": entry["relation_type"],
                "authority": entry["authority"],
                "evidence": {
                    "artifact_id": _sha(
                        b"import-artifact-v1\0"
                        + evidence["sha256"].encode("ascii")
                    ),
                    "byte_count": evidence["byte_count"],
                    "sha256": evidence["sha256"],
                },
            }
        )
    manifest = {
        "schema_version": 1,
        "ledger_instance_id": ledger_instance_id,
        "artifacts": sorted(
            (
                {
                    "artifact_id": _sha(
                        b"import-artifact-v1\0" + item["sha256"].encode("ascii")
                    ),
                    "role": item["role"],
                    "sha256": item["sha256"],
                }
                for item in sealed
            ),
            key=lambda item: (item["role"], item["sha256"]),
        ),
        "run_metadata": inputs.get("run_metadata"),
    }
    manifest_bytes = _json_bytes(manifest)
    manifest_sha = _sha(manifest_bytes)
    manifest_path = state / "import-manifests" / f"{manifest_sha}.json"
    plan_body = {
        "schema_version": 1,
        "input_manifest_sha256": manifest_sha,
        "ledger_instance_id": ledger_instance_id,
        "ledger_instance_sha256": instance_object["sha256"],
        "ledger_sha256": ledger_object["sha256"],
        "header_b64": base64.b64encode(header_raw + header_terminator).decode("ascii"),
        "run_metadata": inputs.get("run_metadata"),
        "rows": rows,
        "edges": edges,
    }
    if _render_import_plan(plan_body) != data:
        raise ImportConflict(
            "ledger bytes are not projection-equivalent to the sealed import plan"
        )
    plan_bytes = _json_bytes(plan_body)
    plan_sha = _sha(plan_bytes)
    plan_path = state / "import-plans" / f"{plan_sha}.json"
    _write_immutable(manifest_path, manifest_bytes)
    _write_immutable(plan_path, plan_bytes)
    plan = dict(plan_body)
    plan.update(
        {
            "plan_sha256": plan_sha,
            "plan_path": str(plan_path.resolve()),
            "manifest_path": str(manifest_path.resolve()),
            "sealed_inputs": sealed,
        }
    )
    return plan


def _plan_body(plan):
    return {key: plan[key] for key in IMPORT_PLAN_FIELDS}


def _meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM schema_meta WHERE key = ?", (key,)).fetchone()
    return default if row is None else row[0]


def _set_meta(conn, key, value):
    conn.execute(
        """
        INSERT INTO schema_meta(key, value) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, str(value)),
    )


def _store_state_root(conn):
    value = _meta(conn, "state_root")
    if not value:
        raise HistoryStoreError("store is not bound to a durable state root")
    return pathlib.Path(value)


def _render_projection_rows(header, rows):
    chunks = [header]
    previous_terminator = b"\n" if header.endswith(b"\n") else b""
    for row_value in rows:
        if previous_terminator == b"":
            chunks.append(b"\n")
        raw_row = bytes(row_value[0])
        row_terminator = bytes(row_value[1])
        chunks.extend((raw_row, row_terminator))
        previous_terminator = row_terminator
    return b"".join(chunks)


def _render_tsv_in_transaction(conn):
    header_b64 = _meta(conn, "ledger_header_b64")
    header = HEADER if header_b64 is None else base64.b64decode(header_b64)
    rows = conn.execute(
        "SELECT raw_row, row_terminator FROM candidates ORDER BY source_sequence"
    ).fetchall()
    return _render_projection_rows(header, rows)


def render_tsv(conn):
    return _render_tsv_in_transaction(conn)


def _next_search_content_revision(conn):
    revision = int(_meta(conn, "history_search_content_revision", "0")) + 1
    _set_meta(conn, "history_search_content_revision", revision)
    return revision


def _insert_search_projection(
    conn, candidate_id, content_version, source_sequence, canonical_revision
):
    conn.execute(
        """
        INSERT INTO search_projection_outbox(
          record_id, projection_kind, content_version, canonical_revision,
          source_sequence, state, generation, claim_token, lease_until
        ) VALUES(?, 'candidate', ?, ?, ?, 'pending', 0, NULL, NULL)
        ON CONFLICT(record_id, projection_kind, content_version)
        DO UPDATE SET canonical_revision = excluded.canonical_revision,
                      source_sequence = excluded.source_sequence,
                      state = 'pending', generation = generation + 1,
                      claim_token = NULL, lease_until = NULL
        """,
        (
            candidate_id,
            content_version,
            canonical_revision,
            source_sequence,
        ),
    )


def queue_search_projections(conn, candidate_ids, content_version):
    """Advance canonical search content once and atomically queue its records."""
    candidate_ids = tuple(dict.fromkeys(candidate_ids))
    if not candidate_ids:
        raise ValueError("projection candidates are required")
    if not isinstance(content_version, str) or not content_version:
        raise ValueError("projection content version is required")
    started = not conn.in_transaction
    if started:
        conn.execute("BEGIN IMMEDIATE")
    try:
        rows = {
            row["candidate_id"]: row["source_sequence"]
            for row in conn.execute(
                "SELECT candidate_id, source_sequence FROM candidates "
                "WHERE candidate_id IN (%s)"
                % ",".join("?" for _ in candidate_ids),
                candidate_ids,
            )
        }
        if set(rows) != set(candidate_ids):
            raise ValueError("candidate is missing")
        revision = _next_search_content_revision(conn)
        for candidate_id in candidate_ids:
            _insert_search_projection(
                conn,
                candidate_id,
                content_version,
                rows[candidate_id],
                revision,
            )
        if started:
            conn.execute("COMMIT")
        return revision
    except Exception:
        if started and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def queue_search_projection(conn, candidate_id, content_version):
    """Advance canonical search content and queue one affected candidate."""
    return queue_search_projections(conn, (candidate_id,), content_version)


def requeue_search_projection(conn, candidate_id, content_version):
    """Queue a projection repair without changing canonical search content."""
    started = not conn.in_transaction
    if started:
        conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT source_sequence FROM candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise ValueError("candidate is missing")
        revision = int(_meta(conn, "history_search_content_revision", "0"))
        _insert_search_projection(
            conn, candidate_id, content_version, row[0], revision
        )
        if started:
            conn.execute("COMMIT")
        return revision
    except Exception:
        if started and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _queue_search_projection(conn, candidate):
    queue_search_projection(conn, candidate["candidate_id"], "candidate-v1")


def _enqueue_ledger_projection(conn):
    sequence = int(_meta(conn, "projection_sequence", "0")) + 1
    _set_meta(conn, "projection_sequence", sequence)
    snapshot = _render_tsv_in_transaction(conn)
    row_count = conn.execute("SELECT count(*) FROM candidates").fetchone()[0]
    conn.execute(
        """
        INSERT INTO ledger_projection_outbox(
          projection_sequence, snapshot_sha256, row_count, state, generation,
          claim_token, lease_until, satisfied_by_sequence, satisfied_by_sha256,
          completed_at
        ) VALUES(?, ?, ?, 'pending', 0, NULL, NULL, NULL, NULL, NULL)
        """,
        (sequence, _sha(snapshot), row_count),
    )
    return sequence


def _decode_plan_bytes(value, description):
    if not isinstance(value, str):
        raise ImportConflict(f"{description} is not base64 text")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ImportConflict(f"{description} is not valid base64") from exc
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ImportConflict(f"{description} is not canonical base64")
    return decoded


def _validate_import_plan_rows(plan):
    expected_fields = {
        "row_number",
        "raw_row_b64",
        "row_terminator_b64",
        "raw_sha256",
        "field_count",
        "date",
        "source",
        "theme",
        "story",
        "verdict",
        "reason",
        "overlap",
        "category",
        "canonical_story",
        "canonical_hash",
        "origin_stable_id",
        "candidate_id",
        "root_story",
        "lineage_id",
        "root_candidate_id",
    }
    ledger_instance_id = _read_instance_id(
        plan.get("ledger_instance_id", "")
    )
    rows = plan.get("rows")
    edges = plan.get("edges")
    header = _decode_plan_bytes(
        plan.get("header_b64"), "import plan header"
    )
    if header.endswith(b"\r\n"):
        header_row = header[:-2]
    elif header.endswith(b"\n"):
        header_row = header[:-1]
    else:
        header_row = header
    if (
        type(plan.get("schema_version")) is not int
        or plan["schema_version"] != 1
        or ledger_instance_id != plan.get("ledger_instance_id")
        or not isinstance(rows, list)
        or not isinstance(edges, list)
        or header_row.split(b"\t")
        != HEADER.rstrip(b"\n").split(b"\t")
    ):
        raise ImportConflict("import plan ledger instance ID is not normalized")
    roots = {}
    for expected_number, item in enumerate(rows, 1):
        if (
            not isinstance(item, dict)
            or set(item) != expected_fields
            or type(item["row_number"]) is not int
            or item["row_number"] != expected_number
        ):
            raise ImportConflict("import plan row schema or order is invalid")
        raw = _decode_plan_bytes(
            item["raw_row_b64"],
            f"import row {expected_number}",
        )
        terminator = _decode_plan_bytes(
            item["row_terminator_b64"],
            f"import row {expected_number} terminator",
        )
        if terminator not in {b"", b"\n", b"\r\n"}:
            raise ImportConflict(
                f"import row {expected_number} has an invalid terminator"
            )
        parsed = _parse_row(raw, expected_number, terminator)
        for field in (
            "row_number",
            "raw_row_b64",
            "row_terminator_b64",
            "raw_sha256",
            "field_count",
            "date",
            "source",
            "theme",
            "story",
            "verdict",
            "reason",
            "overlap",
            "category",
            "canonical_story",
            "canonical_hash",
        ):
            if item[field] != parsed[field]:
                raise ImportConflict(
                    f"import row {expected_number} parsed fields conflict"
                )
        expected_origin = origin_stable_id(
            ledger_instance_id, expected_number, raw
        )
        if (
            item["origin_stable_id"] != expected_origin
            or item["candidate_id"] != _candidate_id(expected_origin)
            or not isinstance(item["root_story"], str)
            or not item["root_story"]
            or canonical_story_v1(item["root_story"])
            != item["root_story"]
            or item["lineage_id"] != _lineage_id(item["root_story"])
        ):
            raise ImportConflict(
                f"import row {expected_number} identity conflicts"
            )
        if item["canonical_story"] == item["root_story"]:
            roots.setdefault(
                item["lineage_id"], item["candidate_id"]
            )
    for item in rows:
        if (
            item["lineage_id"] not in roots
            or item["root_candidate_id"]
            != roots[item["lineage_id"]]
        ):
            raise ImportConflict("import plan lineage root conflicts")
    candidate_ids = {item["candidate_id"] for item in rows}
    for edge in edges:
        evidence = edge.get("evidence") if isinstance(edge, dict) else None
        if (
            not isinstance(edge, dict)
            or set(edge)
            != {
                "parent_candidate_id",
                "child_candidate_id",
                "relation_type",
                "authority",
                "evidence",
            }
            or edge["parent_candidate_id"] not in candidate_ids
            or edge["child_candidate_id"] not in candidate_ids
            or edge["parent_candidate_id"]
            == edge["child_candidate_id"]
            or edge["relation_type"] not in ALLOWED_RELATIONS
            or edge["authority"] not in ALLOWED_EDGE_AUTHORITIES
            or not isinstance(evidence, dict)
            or set(evidence)
            != {"artifact_id", "byte_count", "sha256"}
            or not _is_sha256_text(evidence["sha256"])
            or evidence["artifact_id"]
            != _sha(
                b"import-artifact-v1\0"
                + evidence["sha256"].encode("ascii")
            )
            or type(evidence["byte_count"]) is not int
            or evidence["byte_count"] < 0
        ):
            raise ImportConflict("import plan lineage edge is invalid")


def _import_candidate_provenance(plan):
    return json.dumps(
        {
            "import_epoch": _bootstrap_epoch_id(plan),
            "run_metadata": plan.get("run_metadata"),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _verify_existing_candidate(existing, item, plan):
    raw = _decode_plan_bytes(
        item["raw_row_b64"], f"import row {item['row_number']}"
    )
    terminator = _decode_plan_bytes(
        item["row_terminator_b64"],
        f"import row {item['row_number']} terminator",
    )
    expected = {
        "candidate_id": item["candidate_id"],
        "origin_stable_id": item["origin_stable_id"],
        "lineage_id": item["lineage_id"],
        "row_number": item["row_number"],
        "raw_sha256": item["raw_sha256"],
        "field_count": item["field_count"],
        "date": item["date"],
        "source": item["source"],
        "theme": item["theme"],
        "story": item["story"],
        "verdict": item["verdict"],
        "reason": item["reason"],
        "overlap": item["overlap"],
        "category": item["category"],
        "source_sequence": item["row_number"],
        "provenance_json": _import_candidate_provenance(plan),
    }
    if (
        any(existing[field] != value for field, value in expected.items())
        or bytes(existing["raw_row"]) != raw
        or bytes(existing["row_terminator"]) != terminator
    ):
        raise ImportConflict(
            f"append-only row location {item['row_number']} changed"
        )


def _validate_candidate_record(conn, candidate):
    try:
        row_number = candidate["row_number"]
        source_sequence = candidate["source_sequence"]
        raw = bytes(candidate["raw_row"])
        terminator = bytes(candidate["row_terminator"])
        if (
            type(row_number) is not int
            or row_number < 1
            or source_sequence != row_number
            or terminator not in {b"", b"\n", b"\r\n"}
        ):
            raise ImportConflict("candidate row location is invalid")
        parsed = _parse_row(raw, row_number, terminator)
        for field in (
            "raw_sha256",
            "field_count",
            "date",
            "source",
            "theme",
            "story",
            "verdict",
            "reason",
            "overlap",
            "category",
        ):
            if candidate[field] != parsed[field]:
                raise ImportConflict("candidate parsed fields changed")
        instance_id = _meta(conn, "ledger_instance_id")
        expected_origin = origin_stable_id(
            instance_id, row_number, raw
        )
        if (
            candidate["origin_stable_id"] != expected_origin
            or candidate["candidate_id"]
            != _candidate_id(expected_origin)
        ):
            raise ImportConflict("candidate origin identity changed")
        alias = conn.execute(
            """
            SELECT canonical_story, lineage_id
            FROM story_aliases
            WHERE canonical_version = ? AND canonical_hash = ?
            """,
            (CANONICAL_VERSION, parsed["canonical_hash"]),
        ).fetchone()
        if (
            alias is None
            or tuple(alias)
            != (
                parsed["canonical_story"],
                candidate["lineage_id"],
            )
        ):
            raise ImportConflict("candidate canonical alias changed")
        provenance = json.loads(candidate["provenance_json"])
        canonical_provenance = {
            json.dumps(
                provenance,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=ensure_ascii,
            )
            for ensure_ascii in (True, False)
        }
        if (
            not isinstance(provenance, dict)
            or candidate["provenance_json"]
            not in canonical_provenance
        ):
            raise ImportConflict("candidate provenance is invalid")
    except (
        TypeError,
        ValueError,
        KeyError,
        AttributeError,
        UnicodeError,
        ImportConflict,
    ) as exc:
        if isinstance(exc, ImportConflict):
            raise
        raise ImportConflict("candidate record is invalid") from exc


def _insert_artifact_from_evidence(conn, evidence, source_sequence, cas_root):
    cas_path = pathlib.Path(cas_root) / evidence["sha256"]
    evidence_data = _read_content_addressed_file(
        cas_path,
        evidence["sha256"],
        "sealed lineage evidence",
    )
    if len(evidence_data) != evidence["byte_count"]:
        raise ImportConflict("sealed lineage evidence is missing or changed")
    artifact_id = _sha(
        b"lineage-evidence-v1\0" + evidence["sha256"].encode("ascii")
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO artifacts(
          artifact_id, kind, state, sha256, byte_count, source_path,
          source_sequence, producer_invocation_id, provenance_json,
          idempotency_key
        ) VALUES(?, 'lineage-evidence', 'installed', ?, ?, ?, ?, NULL, ?, ?)
        """,
        (
            artifact_id,
            evidence["sha256"],
            evidence["byte_count"],
            str(cas_path),
            source_sequence,
            json.dumps(
                {"import_artifact_id": evidence["artifact_id"]}, sort_keys=True
            ),
            f"lineage-evidence:{evidence['sha256']}",
        ),
    )
    installed = conn.execute(
        """
        SELECT state, sha256, byte_count, source_path
        FROM artifacts WHERE artifact_id = ?
        """,
        (artifact_id,),
    ).fetchone()
    if installed is None or tuple(installed) != (
        "installed",
        evidence["sha256"],
        evidence["byte_count"],
        str(cas_path),
    ):
        raise ImportConflict("lineage evidence artifact conflicts with sealed plan")
    return artifact_id


def _render_import_plan(plan):
    return _render_projection_rows(
        _decode_plan_bytes(
            plan["header_b64"], "import plan header"
        ),
        (
            (
                _decode_plan_bytes(
                    item["raw_row_b64"],
                    f"import row {item['row_number']}",
                ),
                _decode_plan_bytes(
                    item["row_terminator_b64"],
                    f"import row {item['row_number']} terminator",
                ),
            )
            for item in plan["rows"]
        ),
    )


def _validate_sealed_import_manifest(plan, manifest_path, cas_root):
    manifest_bytes = _read_content_addressed_file(
        manifest_path,
        plan["input_manifest_sha256"],
        "sealed input manifest",
    )
    try:
        manifest = json.loads(manifest_bytes)
    except (TypeError, ValueError) as exc:
        raise ImportConflict("sealed input manifest is malformed") from exc
    if (
        not isinstance(manifest, dict)
        or _json_bytes(manifest) != manifest_bytes
        or set(manifest)
        != {
            "schema_version",
            "ledger_instance_id",
            "artifacts",
            "run_metadata",
        }
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or manifest["ledger_instance_id"] != plan["ledger_instance_id"]
        or _json_bytes(manifest["run_metadata"])
        != _json_bytes(plan.get("run_metadata"))
        or not isinstance(manifest["artifacts"], list)
    ):
        raise ImportConflict("sealed input manifest schema is invalid")
    for artifact in manifest["artifacts"]:
        if (
            not isinstance(artifact, dict)
            or set(artifact) != {"artifact_id", "role", "sha256"}
            or not isinstance(artifact["role"], str)
            or not artifact["role"]
            or not _is_sha256_text(artifact["sha256"])
        ):
            raise ImportConflict("sealed import artifact schema is invalid")
        expected_artifact_id = _sha(
            b"import-artifact-v1\0"
            + artifact["sha256"].encode("ascii")
        )
        if artifact["artifact_id"] != expected_artifact_id:
            raise ImportConflict(
                "sealed import artifact identity is invalid"
            )
        _read_content_addressed_file(
            pathlib.Path(cas_root) / artifact["sha256"],
            artifact["sha256"],
            "sealed import input",
        )
    if manifest["artifacts"] != sorted(
        manifest["artifacts"],
        key=lambda item: (item["role"], item["sha256"]),
    ):
        raise ImportConflict("sealed import artifact order is invalid")
    row_number_by_candidate = {
        item["candidate_id"]: item["row_number"]
        for item in plan["rows"]
    }
    expected_evidence_members = sorted(
        (
            "lineage-evidence:"
            f"{row_number_by_candidate[edge['parent_candidate_id']]}:"
            f"{row_number_by_candidate[edge['child_candidate_id']]}",
            edge["evidence"]["artifact_id"],
            edge["evidence"]["sha256"],
        )
        for edge in plan["edges"]
    )
    actual_evidence_members = sorted(
        (
            artifact["role"],
            artifact["artifact_id"],
            artifact["sha256"],
        )
        for artifact in manifest["artifacts"]
        if artifact["role"].startswith("lineage-evidence:")
    )
    if actual_evidence_members != expected_evidence_members:
        raise ImportConflict(
            "sealed lineage evidence manifest membership is invalid"
        )
    ledger_artifacts = [
        item
        for item in manifest["artifacts"]
        if item["role"] == "ledger"
    ]
    instance_artifacts = [
        item
        for item in manifest["artifacts"]
        if item["role"] == "ledger-instance-id"
    ]
    rendered_plan = _render_import_plan(plan)
    if (
        len(ledger_artifacts) != 1
        or ledger_artifacts[0]["sha256"] != plan["ledger_sha256"]
        or len(instance_artifacts) != 1
        or instance_artifacts[0]["sha256"]
        != plan["ledger_instance_sha256"]
    ):
        raise ImportConflict(
            "sealed manifest conflicts with rendered ledger artifacts"
        )
    _read_content_addressed_file(
        pathlib.Path(cas_root) / plan["ledger_sha256"],
        plan["ledger_sha256"],
        "sealed manifest ledger",
        expected_bytes=rendered_plan,
    )
    instance_data = _read_content_addressed_file(
        pathlib.Path(cas_root) / plan["ledger_instance_sha256"],
        plan["ledger_instance_sha256"],
        "sealed manifest ledger.instance-id",
    )
    if _read_instance_id(instance_data) != plan["ledger_instance_id"]:
        raise ImportConflict(
            "sealed manifest conflicts with ledger.instance-id"
        )
    return manifest


def _verify_epoch_results(conn, plan, epoch, cas_root, repair):
    _validate_import_plan_rows(plan)
    expected_result = _render_import_plan(plan)
    expected_sha = _sha(expected_result)
    if (
        plan.get("ledger_sha256") != expected_sha
        or not _is_sha256_text(
            plan.get("ledger_instance_sha256")
        )
        or epoch["plan_sha256"] != plan["plan_sha256"]
        or epoch["row_count"] != len(plan["rows"])
        or epoch["result_sha256"] != expected_sha
    ):
        raise ImportConflict("import epoch result conflicts with sealed plan")
    _read_content_addressed_file(
        pathlib.Path(cas_root) / plan["ledger_sha256"],
        plan["ledger_sha256"],
        "sealed epoch ledger",
        expected_bytes=expected_result,
    )
    instance_data = _read_content_addressed_file(
        pathlib.Path(cas_root) / plan["ledger_instance_sha256"],
        plan["ledger_instance_sha256"],
        "sealed epoch ledger.instance-id",
    )
    if (
        _read_instance_id(instance_data) != plan["ledger_instance_id"]
    ):
        raise ImportConflict(
            "sealed epoch ledger.instance-id conflicts with rendered plan"
        )
    roots = {}
    for item in plan["rows"]:
        roots[item["lineage_id"]] = item["root_candidate_id"]
    for lineage_id, root_candidate_id in roots.items():
        lineage = conn.execute(
            "SELECT root_candidate_id FROM lineages WHERE lineage_id = ?",
            (lineage_id,),
        ).fetchone()
        if lineage is None and repair:
            conn.execute(
                "INSERT INTO lineages(lineage_id, root_candidate_id) VALUES(?, ?)",
                (lineage_id, root_candidate_id),
            )
            lineage = (root_candidate_id,)
        if lineage is None or lineage[0] != root_candidate_id:
            raise ImportConflict("sealed lineage root is missing or conflicting")
    for item in plan["rows"]:
        existing = conn.execute(
            """
            SELECT * FROM candidates
            WHERE source_sequence = ? AND origin_stable_id = ?
            """,
            (item["row_number"], item["origin_stable_id"]),
        ).fetchone()
        if existing is None:
            raise ImportConflict("committed import row is missing")
        _verify_existing_candidate(existing, item, plan)
        alias = conn.execute(
            """
            SELECT canonical_story, lineage_id FROM story_aliases
            WHERE canonical_version = ? AND canonical_hash = ?
            """,
            (CANONICAL_VERSION, item["canonical_hash"]),
        ).fetchone()
        expected_alias = (item["canonical_story"], item["lineage_id"])
        if alias is None and repair:
            conn.execute(
                """
                INSERT INTO story_aliases(
                  canonical_version, canonical_hash, canonical_story, lineage_id
                ) VALUES(?, ?, ?, ?)
                """,
                (
                    CANONICAL_VERSION,
                    item["canonical_hash"],
                    item["canonical_story"],
                    item["lineage_id"],
                ),
            )
            alias = expected_alias
        if alias is None or tuple(alias) != expected_alias:
            raise ImportConflict("sealed story alias is missing or conflicting")
        search = conn.execute(
            """
            SELECT source_sequence FROM search_projection_outbox
            WHERE record_id = ? AND projection_kind = 'candidate'
              AND content_version = 'candidate-v1'
            """,
            (item["candidate_id"],),
        ).fetchone()
        if search is None and repair:
            _queue_search_projection(conn, item)
            search = (item["row_number"],)
        if search is None or search[0] != item["row_number"]:
            raise ImportConflict("sealed search projection is missing or conflicting")
    for index, edge in enumerate(plan["edges"], 1):
        evidence_record = edge.get("evidence")
        if (
            not isinstance(evidence_record, dict)
            or set(evidence_record)
            != {"artifact_id", "byte_count", "sha256"}
            or not _is_sha256_text(evidence_record["sha256"])
            or evidence_record["artifact_id"]
            != _sha(
                b"import-artifact-v1\0"
                + evidence_record["sha256"].encode("ascii")
            )
            or type(evidence_record["byte_count"]) is not int
            or evidence_record["byte_count"] < 0
        ):
            raise ImportConflict(
                "sealed lineage evidence identity is invalid"
            )
        evidence_data = _read_content_addressed_file(
            pathlib.Path(cas_root) / evidence_record["sha256"],
            evidence_record["sha256"],
            "sealed epoch lineage evidence",
        )
        if len(evidence_data) != evidence_record["byte_count"]:
            raise ImportConflict(
                "sealed lineage evidence byte count changed"
            )
        evidence_id = _insert_artifact_from_evidence(
            conn, edge["evidence"], index, cas_root
        ) if repair else _sha(
            b"lineage-evidence-v1\0"
            + edge["evidence"]["sha256"].encode("ascii")
        )
        evidence = conn.execute(
            "SELECT sha256, state FROM artifacts WHERE artifact_id = ?",
            (evidence_id,),
        ).fetchone()
        if evidence is None or tuple(evidence) != (
            edge["evidence"]["sha256"],
            "installed",
        ):
            raise ImportConflict("sealed lineage evidence is missing or conflicting")
        typed_edge = conn.execute(
            """
            SELECT evidence_artifact_id FROM lineage_edges
            WHERE parent_candidate_id = ? AND child_candidate_id = ?
              AND relation_type = ?
            """,
            (
                edge["parent_candidate_id"],
                edge["child_candidate_id"],
                edge["relation_type"],
            ),
        ).fetchone()
        if typed_edge is None and repair:
            _insert_lineage_edge(
                conn,
                edge["parent_candidate_id"],
                edge["child_candidate_id"],
                edge["relation_type"],
                evidence_id,
            )
            typed_edge = (evidence_id,)
        if typed_edge is None or typed_edge[0] != evidence_id:
            raise ImportConflict("sealed typed lineage edge is missing or conflicting")
    projection = conn.execute(
        """
        SELECT snapshot_sha256, row_count FROM ledger_projection_outbox
        WHERE projection_sequence = ?
        """,
        (epoch["projection_sequence"],),
    ).fetchone()
    expected_projection = (expected_sha, len(plan["rows"]))
    if projection is None and repair:
        conn.execute(
            """
            INSERT INTO ledger_projection_outbox(
              projection_sequence, snapshot_sha256, row_count, state, generation,
              claim_token, lease_until, satisfied_by_sequence,
              satisfied_by_sha256, completed_at
            ) VALUES(?, ?, ?, 'pending', 0, NULL, NULL, NULL, NULL, NULL)
            """,
            (
                epoch["projection_sequence"],
                expected_sha,
                len(plan["rows"]),
            ),
        )
        projection = expected_projection
    if projection is None or tuple(projection) != expected_projection:
        raise ImportConflict("sealed ledger projection is missing or conflicting")


def _commit_import_plan_locked(conn, plan, *, _manage_transaction=True):
    plan_body = _plan_body(plan)
    plan_bytes = _json_bytes(plan_body)
    calculated_sha = _sha(plan_bytes)
    if calculated_sha != plan.get("plan_sha256"):
        raise ImportConflict("import plan content does not match its sealed hash")
    _validate_import_plan_rows(plan)
    rendered_plan = _render_import_plan(plan)
    if (
        not _is_sha256_text(plan.get("ledger_sha256"))
        or _sha(rendered_plan) != plan["ledger_sha256"]
        or not _is_sha256_text(
            plan.get("ledger_instance_sha256")
        )
    ):
        raise ImportConflict(
            "import plan does not bind its rendered ledger artifacts"
        )
    plan_path = pathlib.Path(plan["plan_path"])
    _read_content_addressed_file(
        plan_path,
        calculated_sha,
        "sealed import plan",
        expected_bytes=plan_bytes,
    )
    manifest_path = pathlib.Path(plan["manifest_path"])
    cas_root = plan_path.parent.parent / "import-cas"
    _validate_sealed_import_manifest(
        plan, manifest_path, cas_root
    )
    epoch_id = _sha(
        b"import-epoch-v1\0" + plan["input_manifest_sha256"].encode("ascii")
    )
    state_root = plan_path.parent.parent
    started = _manage_transaction
    if started:
        conn.execute("BEGIN IMMEDIATE")
    elif not conn.in_transaction:
        raise HistoryStoreError(
            "bootstrap import requires an active business transaction"
        )
    try:
        existing_state_root = _meta(conn, "state_root")
        resolved_state_root = str(state_root.resolve())
        if (
            existing_state_root is not None
            and pathlib.Path(existing_state_root).resolve() != state_root.resolve()
        ):
            raise ImportConflict("database is bound to another state root")
        _set_meta(conn, "state_root", resolved_state_root)
        previous = conn.execute(
            "SELECT * FROM import_epochs WHERE input_manifest_sha256 = ?",
            (plan["input_manifest_sha256"],),
        ).fetchone()
        if previous is not None:
            if previous["plan_sha256"] != calculated_sha:
                raise ImportConflict("input manifest is already bound to another plan")
            _verify_epoch_results(conn, plan, previous, cas_root, repair=True)
            if started:
                conn.execute("COMMIT")
            roots = {
                item["root_candidate_id"] for item in plan["rows"]
            }
            return {
                "epoch_id": previous["epoch_id"],
                "data_rows": previous["row_count"],
                "result_sha256": previous["result_sha256"],
                "root_candidate_id": next(iter(roots)) if len(roots) == 1 else None,
                "idempotent": True,
            }
        instance = _meta(conn, "ledger_instance_id")
        if instance is not None and instance != plan["ledger_instance_id"]:
            raise ImportConflict("database is bound to another ledger instance")
        _set_meta(conn, "ledger_instance_id", plan["ledger_instance_id"])
        header_b64 = _meta(conn, "ledger_header_b64")
        initializes_projection = header_b64 is None
        if header_b64 is not None and header_b64 != plan["header_b64"]:
            raise ImportConflict("ledger header bytes changed")
        _set_meta(conn, "ledger_header_b64", plan["header_b64"])
        for item in plan["rows"]:
            existing_alias = conn.execute(
                """
                SELECT lineage_id FROM story_aliases
                WHERE canonical_version = ? AND canonical_hash = ?
                """,
                (CANONICAL_VERSION, item["canonical_hash"]),
            ).fetchone()
            if existing_alias is not None and existing_alias[0] != item["lineage_id"]:
                raise ImportConflict("canonical alias is anchored to another lineage")
        roots = {}
        for item in plan["rows"]:
            roots[item["lineage_id"]] = item["root_candidate_id"]
        for lineage_id, root_candidate_id in roots.items():
            conn.execute(
                """
                INSERT OR IGNORE INTO lineages(lineage_id, root_candidate_id)
                VALUES(?, ?)
                """,
                (lineage_id, root_candidate_id),
            )
            actual = conn.execute(
                "SELECT root_candidate_id FROM lineages WHERE lineage_id = ?",
                (lineage_id,),
            ).fetchone()[0]
            if actual != root_candidate_id:
                raise ImportConflict("lineage has another existing root")
        inserted = 0
        for item in plan["rows"]:
            existing = conn.execute(
                """
                SELECT * FROM candidates
                WHERE source_sequence = ? OR origin_stable_id = ?
                """,
                (item["row_number"], item["origin_stable_id"]),
            ).fetchone()
            if existing is not None:
                _verify_existing_candidate(existing, item, plan)
                continue
            raw_row = _decode_plan_bytes(
                item["raw_row_b64"],
                f"import row {item['row_number']}",
            )
            conn.execute(
                """
                INSERT INTO candidates(
                  candidate_id, origin_stable_id, lineage_id, row_number,
                  raw_sha256, field_count, date, source, theme, story, verdict,
                  reason, overlap, category, source_sequence, raw_row,
                  row_terminator, provenance_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["candidate_id"],
                    item["origin_stable_id"],
                    item["lineage_id"],
                    item["row_number"],
                    item["raw_sha256"],
                    item["field_count"],
                    item["date"],
                    item["source"],
                    item["theme"],
                    item["story"],
                    item["verdict"],
                    item["reason"],
                    item["overlap"],
                    item["category"],
                    item["row_number"],
                    raw_row,
                    _decode_plan_bytes(
                        item["row_terminator_b64"],
                        f"import row {item['row_number']} terminator",
                    ),
                    _import_candidate_provenance(plan),
                ),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO story_aliases(
                  canonical_version, canonical_hash, canonical_story, lineage_id
                ) VALUES(?, ?, ?, ?)
                """,
                (
                    CANONICAL_VERSION,
                    item["canonical_hash"],
                    item["canonical_story"],
                    item["lineage_id"],
                ),
            )
            _queue_search_projection(conn, item)
            inserted += 1
        for index, edge in enumerate(plan["edges"], 1):
            evidence_id = _insert_artifact_from_evidence(
                conn, edge["evidence"], index, cas_root
            )
            _insert_lineage_edge(
                conn,
                edge["parent_candidate_id"],
                edge["child_candidate_id"],
                edge["relation_type"],
                evidence_id,
            )
        projection_sequence = None
        if inserted or initializes_projection:
            projection_sequence = _enqueue_ledger_projection(conn)
        if projection_sequence is None:
            current_projection = _current_projection(conn)
            if current_projection is None:
                raise ImportConflict("import has no ledger projection anchor")
            projection_sequence = current_projection["projection_sequence"]
        result = _render_tsv_in_transaction(conn)
        result_sha = _sha(result)
        if result_sha != plan["ledger_sha256"]:
            raise ImportConflict(
                "committed ledger differs from the sealed import artifact"
            )
        conn.execute(
            """
            INSERT INTO import_epochs(
              epoch_id, input_manifest_sha256, plan_sha256, state, row_count,
              result_sha256, projection_sequence, committed_at
            ) VALUES(?, ?, ?, 'done', ?, ?, ?, ?)
            """,
            (
                epoch_id,
                plan["input_manifest_sha256"],
                calculated_sha,
                len(plan["rows"]),
                result_sha,
                projection_sequence,
                _utc_now(),
            ),
        )
        if started:
            conn.execute("COMMIT")
    except Exception:
        if started and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    root_ids = {item["root_candidate_id"] for item in plan["rows"]}
    return {
        "epoch_id": epoch_id,
        "data_rows": len(plan["rows"]),
        "result_sha256": result_sha,
        "root_candidate_id": next(iter(root_ids)) if len(root_ids) == 1 else None,
        "idempotent": False,
    }


def commit_import_plan(conn, plan):
    plan_path = pathlib.Path(plan["plan_path"])
    state_root = plan_path.parent.parent
    with _export_lock(state_root):
        return _commit_import_plan_locked(conn, plan)


def import_tsv_epoch(conn, path):
    ledger = pathlib.Path(path)
    state_root = ledger.parent / ".ai-ideas"
    plan = build_import_plan({"ledger": ledger}, state_root)
    return commit_import_plan(conn, plan)


def _bootstrap_epoch_id(plan):
    return _sha(
        b"import-epoch-v1\0"
        + plan["input_manifest_sha256"].encode("ascii")
    )


def _bootstrap_marker(plan, near_sa_sha256):
    ledger_inputs = [
        item
        for item in plan.get("sealed_inputs", ())
        if item.get("role") == "ledger"
    ]
    if (
        len(ledger_inputs) != 1
        or ledger_inputs[0].get("sha256") != plan.get("ledger_sha256")
        or _sha(_render_import_plan(plan)) != plan.get("ledger_sha256")
    ):
        raise ImportConflict("bootstrap plan has no unique sealed ledger")
    body = {
        "import_epoch_id": _bootstrap_epoch_id(plan),
        "ledger_row_count": len(plan["rows"]),
        "ledger_sha256": plan["ledger_sha256"],
        "near_sa_sha256": near_sa_sha256,
        "schema_version": SCHEMA_VERSION,
    }
    marker = dict(body)
    marker["marker_sha256"] = _sha(
        b"bootstrap-complete-v1\0" + _json_bytes(body)
    )
    return marker


def _is_sha256_text(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_bootstrap_marker(stored, expected=None):
    try:
        marker = json.loads(stored)
    except (TypeError, ValueError) as exc:
        raise ImportConflict("bootstrap marker is malformed") from exc
    if (
        not isinstance(marker, dict)
        or set(marker) != BOOTSTRAP_MARKER_FIELDS
    ):
        raise ImportConflict("bootstrap marker schema is not closed")
    if (
        type(marker["schema_version"]) is not int
        or marker["schema_version"] != SCHEMA_VERSION
        or type(marker["ledger_row_count"]) is not int
        or marker["ledger_row_count"] < 0
        or not _is_sha256_text(marker["import_epoch_id"])
        or not _is_sha256_text(marker["ledger_sha256"])
        or not _is_sha256_text(marker["marker_sha256"])
        or (
            marker["near_sa_sha256"] is not None
            and not _is_sha256_text(marker["near_sa_sha256"])
        )
    ):
        raise ImportConflict("bootstrap marker field types are invalid")
    body = dict(marker)
    marker_sha = body.pop("marker_sha256")
    if marker_sha != _sha(
        b"bootstrap-complete-v1\0" + _json_bytes(body)
    ):
        raise ImportConflict("bootstrap marker hash is invalid")
    canonical = _json_bytes(marker).decode("utf-8")
    if stored != canonical:
        raise ImportConflict("bootstrap marker is not canonical JSON")
    if (
        expected is not None
        and canonical != _json_bytes(expected).decode("utf-8")
    ):
        raise ImportConflict("bootstrap inputs differ from completed migration")
    return marker


def _bootstrap_epoch_record(epoch):
    return {field: epoch[field] for field in BOOTSTRAP_EPOCH_FIELDS}


def _bootstrap_observation_record(item):
    stored = {
        key: item[key]
        for key in BOOTSTRAP_OBSERVATION_FIELDS[:9]
    }
    stored.update(
        {
            "candidate_theme": item["expected_theme"],
            "candidate_source": item["expected_source"],
            "candidate_verdict": item["expected_verdict"],
        }
    )
    return stored


def _bootstrap_provenance_payload(epoch, marker, observations):
    observation_records = sorted(
        (
            _bootstrap_observation_record(item)
            for item in observations
        ),
        key=lambda item: item["observation_id"],
    )
    observation_bytes = _json_bytes(observation_records)
    return {
        "import_epoch": _bootstrap_epoch_record(epoch),
        "ledger_sha256": marker["ledger_sha256"],
        "marker_key": BOOTSTRAP_MARKER_KEY,
        "marker_sha256": marker["marker_sha256"],
        "near_sa_observation_count": len(observation_records),
        "near_sa_observations": observation_records,
        "near_sa_observations_sha256": _sha(
            b"bootstrap-near-sa-membership-v1\0" + observation_bytes
        ),
        "near_sa_sha256": marker["near_sa_sha256"],
        "schema_version": SCHEMA_VERSION,
    }


def _install_bootstrap_provenance(conn, epoch, marker, observations):
    payload = _bootstrap_provenance_payload(
        epoch, marker, observations
    )
    raw = _json_bytes(payload).decode("utf-8")
    digest = _sha(
        b"bootstrap-provenance-v1\0" + raw.encode("utf-8")
    )
    existing = conn.execute(
        """
        SELECT provenance_sha256, provenance_json
        FROM bootstrap_provenance WHERE marker_key = ?
        """,
        (BOOTSTRAP_MARKER_KEY,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) != (digest, raw):
            raise ImportConflict(
                "bootstrap provenance conflicts with existing state"
            )
        return payload
    conn.execute(
        """
        INSERT INTO bootstrap_provenance(
          marker_key, provenance_sha256, provenance_json, created_at
        ) VALUES(?, ?, ?, ?)
        """,
        (BOOTSTRAP_MARKER_KEY, digest, raw, _utc_now()),
    )
    return payload


def _validated_bootstrap_provenance(conn, marker):
    row = conn.execute(
        """
        SELECT provenance_sha256, provenance_json
        FROM bootstrap_provenance WHERE marker_key = ?
        """,
        (BOOTSTRAP_MARKER_KEY,),
    ).fetchone()
    if row is None:
        raise ImportConflict("bootstrap provenance is missing")
    try:
        provenance = json.loads(row["provenance_json"])
    except (TypeError, ValueError) as exc:
        raise ImportConflict("bootstrap provenance is malformed") from exc
    if (
        not isinstance(provenance, dict)
        or set(provenance) != BOOTSTRAP_PROVENANCE_FIELDS
    ):
        raise ImportConflict("bootstrap provenance schema is not closed")
    canonical = _json_bytes(provenance).decode("utf-8")
    if canonical != row["provenance_json"]:
        raise ImportConflict("bootstrap provenance is not canonical JSON")
    if (
        not _is_sha256_text(row["provenance_sha256"])
        or row["provenance_sha256"]
        != _sha(
            b"bootstrap-provenance-v1\0"
            + canonical.encode("utf-8")
        )
    ):
        raise ImportConflict("bootstrap provenance hash is invalid")
    if (
        provenance["marker_key"] != BOOTSTRAP_MARKER_KEY
        or type(provenance["schema_version"]) is not int
        or provenance["schema_version"] != SCHEMA_VERSION
        or provenance["marker_sha256"] != marker["marker_sha256"]
        or provenance["ledger_sha256"] != marker["ledger_sha256"]
        or provenance["near_sa_sha256"] != marker["near_sa_sha256"]
    ):
        raise ImportConflict(
            "bootstrap marker conflicts with durable provenance"
        )
    epoch_record = provenance["import_epoch"]
    if (
        not isinstance(epoch_record, dict)
        or set(epoch_record) != set(BOOTSTRAP_EPOCH_FIELDS)
        or epoch_record["state"] != "done"
        or type(epoch_record["row_count"]) is not int
        or epoch_record["row_count"] < 0
        or type(epoch_record["projection_sequence"]) is not int
        or epoch_record["projection_sequence"] < 1
        or not _is_sha256_text(epoch_record["epoch_id"])
        or not _is_sha256_text(
            epoch_record["input_manifest_sha256"]
        )
        or not _is_sha256_text(epoch_record["plan_sha256"])
        or not _is_sha256_text(epoch_record["result_sha256"])
        or not isinstance(epoch_record["committed_at"], str)
        or not epoch_record["committed_at"]
    ):
        raise ImportConflict("bootstrap import epoch provenance is invalid")
    if (
        epoch_record["epoch_id"] != marker["import_epoch_id"]
        or epoch_record["row_count"] != marker["ledger_row_count"]
        or epoch_record["result_sha256"] != marker["ledger_sha256"]
        or epoch_record["epoch_id"]
        != _sha(
            b"import-epoch-v1\0"
            + epoch_record["input_manifest_sha256"].encode("ascii")
        )
    ):
        raise ImportConflict(
            "bootstrap marker import epoch binding is invalid"
        )
    epoch = conn.execute(
        "SELECT * FROM import_epochs WHERE epoch_id = ?",
        (marker["import_epoch_id"],),
    ).fetchone()
    if (
        epoch is None
        or _bootstrap_epoch_record(epoch) != epoch_record
    ):
        raise ImportConflict(
            "bootstrap import epoch conflicts with durable provenance"
        )
    projection = conn.execute(
        """
        SELECT snapshot_sha256, row_count
        FROM ledger_projection_outbox
        WHERE projection_sequence = ?
        """,
        (epoch_record["projection_sequence"],),
    ).fetchone()
    if (
        projection is None
        or tuple(projection)
        != (epoch_record["result_sha256"], epoch_record["row_count"])
    ):
        raise ImportConflict(
            "bootstrap ledger projection conflicts with import epoch"
        )
    observations = provenance["near_sa_observations"]
    count = provenance["near_sa_observation_count"]
    if (
        not isinstance(observations, list)
        or type(count) is not int
        or count < 0
        or count != len(observations)
        or (
            provenance["near_sa_sha256"] is None
            and count != 0
        )
        or not _is_sha256_text(
            provenance["near_sa_observations_sha256"]
        )
        or provenance["near_sa_observations_sha256"]
        != _sha(
            b"bootstrap-near-sa-membership-v1\0"
            + _json_bytes(observations)
        )
    ):
        raise ImportConflict(
            "bootstrap near-SA provenance digest is invalid"
        )
    if observations != sorted(
        observations, key=lambda item: item.get("observation_id", "")
    ):
        raise ImportConflict(
            "bootstrap near-SA provenance order is invalid"
        )
    seen = set()
    for observation in observations:
        if (
            not isinstance(observation, dict)
            or set(observation) != set(BOOTSTRAP_OBSERVATION_FIELDS)
            or not _is_sha256_text(observation["observation_id"])
            or not _is_sha256_text(observation["candidate_id"])
            or type(observation["source_sequence"]) is not int
            or observation["source_sequence"] < 1
            or type(observation["sa_votes"]) is not int
            or observation["sa_votes"] < 0
            or any(
                not isinstance(observation[field], str)
                for field in BOOTSTRAP_OBSERVATION_FIELDS[4:]
            )
            or observation["observation_id"] in seen
        ):
            raise ImportConflict(
                "bootstrap near-SA provenance row is invalid"
            )
        vote_parts = observation["vote_vector"].split(",")
        if (
            not vote_parts
            or any(vote not in {"0", "1", "2"} for vote in vote_parts)
            or observation["sa_votes"]
            != sum(vote == "2" for vote in vote_parts)
            or observation["category"]
            not in {"design-fixable", "evidence-incomplete"}
        ):
            raise ImportConflict(
                "bootstrap near-SA provenance eligibility is invalid"
            )
        seen.add(observation["observation_id"])
        stored = conn.execute(
            """
            SELECT observation_id, candidate_id, source_sequence, sa_votes,
                   vote_vector, overlap, category, reason, observed_at
            FROM near_sa_observations WHERE observation_id = ?
            """,
            (observation["observation_id"],),
        ).fetchone()
        if (
            stored is None
            or tuple(stored)
            != tuple(
                observation[field]
                for field in BOOTSTRAP_OBSERVATION_FIELDS[:9]
            )
        ):
            raise ImportConflict(
                "bootstrap near-SA membership content changed"
            )
        candidate = conn.execute(
            """
            SELECT source_sequence, theme, source, verdict, overlap, category
            FROM candidates WHERE candidate_id = ?
            """,
            (observation["candidate_id"],),
        ).fetchone()
        if (
            candidate is None
            or tuple(candidate)
            != (
                observation["source_sequence"],
                observation["candidate_theme"],
                observation["candidate_source"],
                observation["candidate_verdict"],
                observation["overlap"],
                observation["category"],
            )
            or observation["candidate_source"] != "hunt"
            or observation["candidate_verdict"]
            not in {"accept-w-rev", "reject"}
        ):
            raise ImportConflict(
                "bootstrap near-SA canonical membership changed"
            )
    return provenance


def validated_bootstrap_marker(conn):
    """Return the durable closed bootstrap marker after read-only validation."""
    started = not conn.in_transaction
    if started:
        conn.execute("BEGIN")
    try:
        stored = _meta(conn, BOOTSTRAP_MARKER_KEY)
        if stored is None:
            raise ImportConflict("bootstrap completion marker is missing")
        marker = _validate_bootstrap_marker(stored)
        if _meta(conn, "schema_version") != str(
            marker["schema_version"]
        ):
            raise ImportConflict(
                "bootstrap marker schema version is not installed"
            )
        _validated_bootstrap_provenance(conn, marker)
        if started:
            conn.execute("COMMIT")
        return dict(marker)
    except sqlite3.OperationalError as exc:
        if started and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise ImportConflict(
            "bootstrap completion provenance is unavailable"
        ) from exc
    except Exception:
        if started and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _prepare_bootstrap_near_sa(path, plan):
    if path is None:
        return None, []
    source = pathlib.Path(path)
    data = _read_regular_file_nofollow(
        source, "near-SA bootstrap snapshot"
    )
    rows = []
    seen_observations = set()
    seen_candidates = set()
    for number, raw_line in enumerate(data.splitlines(), 1):
        if not raw_line:
            continue
        try:
            fields = raw_line.decode("utf-8").split("\t")
        except UnicodeDecodeError as exc:
            raise ImportConflict(
                f"near-SA row {number} is not UTF-8"
            ) from exc
        if len(fields) != 7:
            raise ImportConflict(
                f"near-SA row {number} must have seven fields"
            )
        date, origin, story, _theme, overlap, votes, category = fields
        if not date or not origin or not story or not votes:
            raise ImportConflict(
                f"near-SA row {number} has an empty required field"
            )
        vote_parts = votes.split(",")
        if any(vote not in {"0", "1", "2"} for vote in vote_parts):
            raise ImportConflict(
                f"near-SA row {number} has an invalid vote vector"
            )
        canonical = canonical_story_v1(story)
        matches = [
            item
            for item in plan["rows"]
            if item["canonical_story"] == canonical
            and item["story"] == story
        ]
        if len(matches) != 1:
            raise ImportConflict(
                f"near-SA row {number} resolves to {len(matches)} candidates"
            )
        candidate = matches[0]
        if (
            _theme != candidate["theme"]
            or overlap != candidate["overlap"]
            or category != candidate["category"]
            or category
            not in {"design-fixable", "evidence-incomplete"}
            or candidate["source"] != "hunt"
            or candidate["verdict"] not in {"accept-w-rev", "reject"}
        ):
            raise ImportConflict(
                f"near-SA row {number} contradicts its canonical row"
            )
        candidate_id = candidate["candidate_id"]
        source_sequence = candidate["row_number"]
        observation_id = _sha(b"near-sa-v1\0" + raw_line)
        candidate_key = (candidate_id, source_sequence)
        if (
            observation_id in seen_observations
            or candidate_key in seen_candidates
        ):
            raise ImportConflict("near-SA bootstrap snapshot contains a duplicate")
        seen_observations.add(observation_id)
        seen_candidates.add(candidate_key)
        rows.append(
            {
                "observation_id": observation_id,
                "candidate_id": candidate_id,
                "source_sequence": source_sequence,
                "sa_votes": sum(part == "2" for part in vote_parts),
                "vote_vector": votes,
                "overlap": overlap,
                "category": category,
                "reason": origin,
                "observed_at": date,
                "expected_theme": _theme,
                "expected_source": candidate["source"],
                "expected_verdict": candidate["verdict"],
            }
        )
    return _sha(data), rows


def _bootstrap_observation_values(item):
    return tuple(
        item[key]
        for key in (
            "observation_id",
            "candidate_id",
            "source_sequence",
            "sa_votes",
            "vote_vector",
            "overlap",
            "category",
            "reason",
            "observed_at",
        )
    )


def _verify_bootstrap_observations(conn, observations, *, exact):
    for item in observations:
        stored = conn.execute(
            """
            SELECT observation_id, candidate_id, source_sequence, sa_votes,
                   vote_vector, overlap, category, reason, observed_at
            FROM near_sa_observations
            WHERE observation_id = ?
            """,
            (item["observation_id"],),
        ).fetchone()
        if stored is None or tuple(stored) != _bootstrap_observation_values(
            item
        ):
            raise ImportConflict(
                "near-SA bootstrap observation is missing or conflicting"
            )
    if exact:
        count = conn.execute(
            "SELECT count(*) FROM near_sa_observations"
        ).fetchone()[0]
        if count != len(observations):
            raise ImportConflict(
                "database has near-SA observations outside the bootstrap snapshot"
            )


def _install_bootstrap_observations(conn, observations):
    for item in observations:
        candidate = conn.execute(
            """
            SELECT source_sequence, source, theme, verdict, overlap, category
            FROM candidates
            WHERE candidate_id = ?
            """,
            (item["candidate_id"],),
        ).fetchone()
        if (
            candidate is None
            or candidate["source_sequence"] != item["source_sequence"]
            or candidate["source"] != item["expected_source"]
            or candidate["theme"] != item["expected_theme"]
            or candidate["verdict"] != item["expected_verdict"]
            or candidate["overlap"] != item["overlap"]
            or candidate["category"] != item["category"]
        ):
            raise ImportConflict(
                "near-SA bootstrap candidate is missing or conflicting"
            )
        existing = conn.execute(
            """
            SELECT observation_id, candidate_id, source_sequence, sa_votes,
                   vote_vector, overlap, category, reason, observed_at
            FROM near_sa_observations
            WHERE observation_id = ?
               OR (candidate_id = ? AND source_sequence = ?)
            """,
            (
                item["observation_id"],
                item["candidate_id"],
                item["source_sequence"],
            ),
        ).fetchall()
        expected = _bootstrap_observation_values(item)
        if existing:
            if len(existing) != 1 or tuple(existing[0]) != expected:
                raise ImportConflict(
                    "near-SA bootstrap observation conflicts with existing state"
                )
            continue
        conn.execute(
            """
            INSERT INTO near_sa_observations(
              observation_id, candidate_id, source_sequence, sa_votes,
              vote_vector, overlap, category, reason, observed_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            expected,
        )


def _bootstrap_result(epoch, marker, observations, *, idempotent, import_idempotent):
    return {
        "epoch_id": epoch["epoch_id"],
        "data_rows": epoch["row_count"],
        "result_sha256": epoch["result_sha256"],
        "near_sa_observations": len(observations),
        "bootstrap_marker": dict(marker),
        "idempotent": idempotent,
        "import_idempotent": import_idempotent,
    }


def bootstrap_import_epoch(
    conn,
    ledger_path,
    near_sa_path=None,
    *,
    state_root=None,
    _fault_after=None,
):
    """Install the sealed first migration and its closed completion marker."""
    if _fault_after not in {None, "after_ledger", "after_near_sa"}:
        raise ValueError("unknown bootstrap fault point")
    if conn.in_transaction:
        raise HistoryStoreError(
            "bootstrap import requires a connection outside a transaction"
        )
    ledger = pathlib.Path(ledger_path)
    _read_regular_file_nofollow(ledger, "bootstrap ledger")
    state = (
        ledger.parent / ".ai-ideas"
        if state_root is None
        else pathlib.Path(state_root)
    )
    plan = build_import_plan({"ledger": ledger}, state)
    near_sa_sha, observations = _prepare_bootstrap_near_sa(
        near_sa_path, plan
    )
    expected_marker = _bootstrap_marker(plan, near_sa_sha)
    expected_marker_json = _json_bytes(expected_marker).decode("utf-8")
    epoch_id = expected_marker["import_epoch_id"]

    with _export_lock(state):
        init_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            schema_version = _meta(conn, "schema_version")
            if schema_version != str(SCHEMA_VERSION):
                raise ImportConflict(
                    "database schema version cannot run bootstrap migration"
                )
            stored_marker = _meta(conn, BOOTSTRAP_MARKER_KEY)
            if stored_marker is not None:
                marker = validated_bootstrap_marker(conn)
                _validate_bootstrap_marker(
                    stored_marker, expected_marker
                )
                epoch = conn.execute(
                    "SELECT * FROM import_epochs WHERE epoch_id = ?",
                    (epoch_id,),
                ).fetchone()
                if epoch is None:
                    raise ImportConflict(
                        "bootstrap marker references a missing import epoch"
                    )
                _verify_epoch_results(
                    conn,
                    plan,
                    epoch,
                    state / "import-cas",
                    repair=False,
                )
                _verify_bootstrap_observations(
                    conn, observations, exact=False
                )
                if conn.execute("PRAGMA foreign_key_check").fetchone():
                    raise ImportConflict(
                        "bootstrap store has foreign-key violations"
                    )
                conn.execute("ROLLBACK")
                return _bootstrap_result(
                    epoch,
                    marker,
                    observations,
                    idempotent=True,
                    import_idempotent=True,
                )

            imported = _commit_import_plan_locked(
                conn, plan, _manage_transaction=False
            )
            _fault("after_ledger", _fault_after)
            _install_bootstrap_observations(conn, observations)
            _fault("after_near_sa", _fault_after)

            epoch = conn.execute(
                "SELECT * FROM import_epochs WHERE epoch_id = ?",
                (epoch_id,),
            ).fetchone()
            if epoch is None:
                raise ImportConflict(
                    "bootstrap import epoch was not installed"
                )
            if _render_tsv_in_transaction(conn) != _render_import_plan(plan):
                raise ImportConflict(
                    "database contains canonical rows outside the bootstrap ledger"
                )
            _verify_epoch_results(
                conn,
                plan,
                epoch,
                state / "import-cas",
                repair=False,
            )
            _verify_bootstrap_observations(
                conn, observations, exact=True
            )
            if conn.execute("PRAGMA foreign_key_check").fetchone():
                raise ImportConflict(
                    "bootstrap store has foreign-key violations"
                )
            _install_bootstrap_provenance(
                conn, epoch, expected_marker, observations
            )
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES(?, ?)",
                (BOOTSTRAP_MARKER_KEY, expected_marker_json),
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return _bootstrap_result(
        epoch,
        expected_marker,
        observations,
        idempotent=False,
        import_idempotent=imported["idempotent"],
    )


def _normalize_append_row(value):
    if isinstance(value, bytes):
        raw = _strip_one_terminator(value)
    elif isinstance(value, str):
        raw = _strip_one_terminator(value.encode("utf-8"))
    elif isinstance(value, dict):
        raw = "\t".join(
            str(value.get(key, ""))
            for key in (
                "date",
                "source",
                "theme",
                "story",
                "verdict",
                "reason",
                "overlap",
                "category",
            )
        ).encode("utf-8")
    else:
        raw = "\t".join(str(item) for item in value).encode("utf-8")
    return raw


def _normalize_near_sa_observations(values, row_count):
    normalized = []
    expected = {
        "row_index",
        "sa_votes",
        "vote_vector",
        "overlap",
        "category",
        "reason",
        "observed_at",
    }
    for value in values or ():
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("near-SA observation schema is not closed")
        row_index = value["row_index"]
        sa_votes = value["sa_votes"]
        if (
            type(row_index) is not int
            or row_index < 0
            or row_index >= row_count
        ):
            raise ValueError("near-SA observation row index is invalid")
        if type(sa_votes) is not int or sa_votes < 0:
            raise ValueError("near-SA vote count is invalid")
        for field in (
            "vote_vector",
            "overlap",
            "category",
            "reason",
            "observed_at",
        ):
            if not isinstance(value[field], str) or not value[field]:
                raise ValueError(
                    "near-SA observation fields must be nonempty text"
                )
        votes = value["vote_vector"].split(",")
        if (
            not votes
            or any(vote not in {"0", "1", "2"} for vote in votes)
            or sa_votes != sum(vote == "2" for vote in votes)
        ):
            raise ValueError(
                "near-SA vote vector does not match its SA count"
            )
        if value["category"] not in {
            "design-fixable",
            "evidence-incomplete",
        }:
            raise ValueError("near-SA category is not eligible")
        normalized.append(dict(value))
    if len({item["row_index"] for item in normalized}) != len(normalized):
        raise ValueError("near-SA observation row index is duplicated")
    return normalized


def _append_rows_locked(
    conn,
    rows,
    provenance,
    near_sa_observations=None,
    *,
    commit_key=None,
    request_sha256=None,
    request_json=None,
    precommit_validator=None,
):
    raw_rows = [_normalize_append_row(item) for item in rows]
    observations = _normalize_near_sa_observations(
        near_sa_observations, len(raw_rows)
    )
    if (
        len(
            {
                value is None
                for value in (
                    commit_key,
                    request_sha256,
                    request_json,
                )
            }
        )
        != 1
    ):
        raise ValueError(
            "round commit identity must be supplied together"
        )
    if commit_key is not None and (
        not isinstance(commit_key, str)
        or not commit_key
        or not isinstance(request_sha256, str)
        or len(request_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in request_sha256
        )
    ):
        raise ValueError("round commit identity is invalid")
    if commit_key is not None:
        if not isinstance(request_json, str) or not request_json:
            raise ValueError("round commit request JSON is invalid")
        try:
            parsed_request = json.loads(request_json)
        except ValueError as exc:
            raise ValueError(
                "round commit request JSON is invalid"
            ) from exc
        if (
            json.dumps(
                parsed_request,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            != request_json
        ):
            raise ValueError(
                "round commit request JSON is not canonical"
            )
    if (
        precommit_validator is not None
        and not callable(precommit_validator)
    ):
        raise ValueError("round precommit validator is invalid")
    if not raw_rows and commit_key is None:
        return {
            "appended": 0,
            "near_sa_observations": 0,
            "projection_sequence": None,
        }
    conn.execute("BEGIN IMMEDIATE")
    try:
        if commit_key is not None:
            prior = conn.execute(
                """
                SELECT request_sha256, request_json, result_json
                FROM runtime_round_commits
                WHERE commit_key = ?
                """,
                (commit_key,),
            ).fetchone()
            if prior is not None:
                if (
                    prior["request_sha256"] != request_sha256
                    or prior["request_json"] != request_json
                ):
                    raise HistoryStoreError(
                        "round commit replay conflicts with durable receipt"
                    )
                result = json.loads(prior["result_json"])
                conn.execute("ROLLBACK")
                result["replayed"] = True
                result["commit_key"] = commit_key
                result["request_sha256"] = request_sha256
                return result
        if precommit_validator is not None:
            if not conn.in_transaction:
                raise HistoryStoreError(
                    "round precommit validation lost its transaction"
                )
            precommit_validator(conn)
            if not conn.in_transaction:
                raise HistoryStoreError(
                    "round precommit validation ended its transaction"
                )
        instance = _meta(conn, "ledger_instance_id")
        if not instance:
            raise HistoryStoreError("store is not bound to ledger.instance-id")
        next_sequence = (
            conn.execute(
                "SELECT COALESCE(MAX(source_sequence), 0) + 1 FROM candidates"
            ).fetchone()[0]
        )
        candidate_ids = []
        for offset, raw in enumerate(raw_rows):
            sequence = next_sequence + offset
            item = _parse_row(raw, sequence, b"\n")
            origin = origin_stable_id(instance, sequence, raw)
            candidate_id = _candidate_id(origin)
            lineage = conn.execute(
                """
                SELECT lineage_id FROM story_aliases
                WHERE canonical_version = ? AND canonical_hash = ?
                """,
                (CANONICAL_VERSION, item["canonical_hash"]),
            ).fetchone()
            if lineage is None:
                lineage_id = _lineage_id(item["canonical_story"])
                conn.execute(
                    "INSERT INTO lineages(lineage_id, root_candidate_id) VALUES(?, ?)",
                    (lineage_id, candidate_id),
                )
                conn.execute(
                    """
                    INSERT INTO story_aliases(
                      canonical_version, canonical_hash, canonical_story, lineage_id
                    ) VALUES(?, ?, ?, ?)
                    """,
                    (
                        CANONICAL_VERSION,
                        item["canonical_hash"],
                        item["canonical_story"],
                        lineage_id,
                    ),
                )
            else:
                lineage_id = lineage[0]
            conn.execute(
                """
                INSERT INTO candidates(
                  candidate_id, origin_stable_id, lineage_id, row_number,
                  raw_sha256, field_count, date, source, theme, story, verdict,
                  reason, overlap, category, source_sequence, raw_row,
                  row_terminator, provenance_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    origin,
                    lineage_id,
                    sequence,
                    item["raw_sha256"],
                    item["field_count"],
                    item["date"],
                    item["source"],
                    item["theme"],
                    item["story"],
                    item["verdict"],
                    item["reason"],
                    item["overlap"],
                    item["category"],
                    sequence,
                    raw,
                    b"\n",
                    json.dumps(
                        dict(provenance),
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ),
                ),
            )
            queued = dict(item, candidate_id=candidate_id, row_number=sequence)
            _queue_search_projection(conn, queued)
            candidate_ids.append(candidate_id)
        for observation in observations:
            candidate_id = candidate_ids[observation["row_index"]]
            candidate = conn.execute(
                """
                SELECT source_sequence, source, verdict, overlap, category
                FROM candidates WHERE candidate_id = ?
                """,
                (candidate_id,),
            ).fetchone()
            if (
                candidate is None
                or candidate["source"] != "hunt"
                or candidate["verdict"]
                not in {"accept-w-rev", "reject"}
                or observation["overlap"] != candidate["overlap"]
                or observation["category"] != candidate["category"]
            ):
                raise ValueError(
                    "near-SA observation must match its canonical row"
                )
            material = {
                "candidate_id": candidate_id,
                "source_sequence": candidate["source_sequence"],
                "sa_votes": observation["sa_votes"],
                "vote_vector": observation["vote_vector"],
                "overlap": observation["overlap"],
                "category": observation["category"],
                "reason": observation["reason"],
                "observed_at": observation["observed_at"],
            }
            observation_id = _sha(
                b"near-sa-runtime-v1\0" + _json_bytes(material)
            )
            conn.execute(
                """
                INSERT INTO near_sa_observations(
                  observation_id, candidate_id, source_sequence, sa_votes,
                  vote_vector, overlap, category, reason, observed_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation_id,
                    candidate_id,
                    candidate["source_sequence"],
                    observation["sa_votes"],
                    observation["vote_vector"],
                    observation["overlap"],
                    observation["category"],
                    observation["reason"],
                    observation["observed_at"],
                ),
            )
        projection_sequence = (
            _enqueue_ledger_projection(conn)
            if raw_rows
            else None
        )
        result = {
            "appended": len(candidate_ids),
            "candidate_ids": candidate_ids,
            "near_sa_observations": len(observations),
            "projection_sequence": projection_sequence,
            "provenance": dict(provenance),
        }
        if commit_key is not None:
            conn.execute(
                """
                INSERT INTO runtime_round_commits(
                  commit_key, request_sha256, request_json,
                  result_json, created_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    commit_key,
                    request_sha256,
                    request_json,
                    json.dumps(
                        result,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ),
                    _utc_now(),
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    if commit_key is not None:
        result["replayed"] = False
        result["commit_key"] = commit_key
        result["request_sha256"] = request_sha256
    return result


def append_rows(
    conn, rows, provenance, near_sa_observations=None
):
    state_root = _store_state_root(conn)
    with _export_lock(state_root):
        return _append_rows_locked(
            conn,
            rows,
            provenance,
            near_sa_observations=near_sa_observations,
        )


def append_rows_idempotent(
    conn,
    rows,
    provenance,
    *,
    commit_key,
    request_sha256,
    request_json,
    near_sa_observations=None,
    precommit_validator=None,
):
    state_root = _store_state_root(conn)
    with _export_lock(state_root):
        return _append_rows_locked(
            conn,
            rows,
            provenance,
            near_sa_observations=near_sa_observations,
            commit_key=commit_key,
            request_sha256=request_sha256,
            request_json=request_json,
            precommit_validator=precommit_validator,
        )


def lookup_round_commit(
    conn, *, commit_key, request_sha256, request_json
):
    row = conn.execute(
        """
        SELECT request_sha256, request_json, result_json
        FROM runtime_round_commits
        WHERE commit_key = ?
        """,
        (commit_key,),
    ).fetchone()
    if row is None:
        return None
    if (
        row["request_sha256"] != request_sha256
        or row["request_json"] != request_json
    ):
        raise HistoryStoreError(
            "round commit replay conflicts with durable receipt"
        )
    result = json.loads(row["result_json"])
    result["replayed"] = True
    result["commit_key"] = commit_key
    result["request_sha256"] = request_sha256
    return result


def get_candidate(conn, candidate_id):
    row = conn.execute(
        "SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,)
    ).fetchone()
    return None if row is None else dict(row)


def _is_within(path, directory):
    try:
        pathlib.Path(path).relative_to(pathlib.Path(directory))
        return True
    except ValueError:
        return False


def _validate_destination(
    conn, path, state_root=None, allow_ledger_projection=False
):
    destination = pathlib.Path(path)
    if destination.is_symlink():
        raise ValueError(f"destination cannot be a symlink: {destination}")
    resolved = destination.resolve()
    database = _db_path(conn).resolve()
    protected = {
        pathlib.Path(str(database) + suffix).resolve()
        for suffix in ("", "-wal", "-shm", "-journal")
    }
    if not allow_ledger_projection:
        protected.update(
            {
                (pathlib.Path.cwd() / "ledger.tsv").resolve(),
                (pathlib.Path.cwd() / "tmp" / "ledger.good").resolve(),
            }
        )
    if state_root is not None:
        resolved_state = pathlib.Path(state_root).resolve()
        if not allow_ledger_projection:
            protected.update(
                {
                    (resolved_state.parent / "ledger.tsv").resolve(),
                    (resolved_state.parent / "tmp" / "ledger.good").resolve(),
                }
            )
    if resolved in protected:
        raise ValueError("destination cannot overlap reserved canonical state")
    if destination.exists() and not destination.is_file():
        raise ValueError(f"destination is not a regular file: {destination}")
    if destination.exists():
        for reserved in protected:
            if reserved.exists() and os.path.samefile(
                str(destination), str(reserved)
            ):
                raise ValueError("destination cannot alias reserved canonical state")
    if state_root is not None:
        resolved_state = pathlib.Path(state_root).resolve()
        if _is_within(resolved, resolved_state):
            raise ValueError("destination cannot overlap reserved history state")
        if destination.exists() and resolved_state.exists():
            for reserved in resolved_state.rglob("*"):
                if (
                    reserved.is_file()
                    and not reserved.is_symlink()
                    and os.path.samefile(str(destination), str(reserved))
                ):
                    raise ValueError(
                        "destination cannot alias reserved history state"
                    )
    return resolved


def export_tsv(conn, path):
    state_value = _meta(conn, "state_root")
    state_root = None if state_value is None else pathlib.Path(state_value)
    destination = _validate_destination(conn, path, state_root)
    data = render_tsv(conn)
    _atomic_replace(destination, data, None, None, None)
    return {"path": str(destination), "sha256": _sha(data), "byte_count": len(data)}


def _insert_lineage_edge(conn, parent, child, relation, evidence):
    if relation not in ALLOWED_RELATIONS:
        raise ValueError("unsupported lineage relation")
    if parent == child:
        raise LineageCycle("self edges are cycles")
    endpoints = conn.execute(
        """
        SELECT candidate_id, lineage_id FROM candidates
        WHERE candidate_id IN (?, ?)
        """,
        (parent, child),
    ).fetchall()
    endpoint_lineages = {item["candidate_id"]: item["lineage_id"] for item in endpoints}
    if set(endpoint_lineages) != {parent, child}:
        raise ValueError("lineage edge endpoint is missing")
    if endpoint_lineages[parent] != endpoint_lineages[child]:
        raise ValueError("lineage edge cannot cross lineages")
    reachable = conn.execute(
        """
        WITH RECURSIVE reachable(candidate_id) AS (
          SELECT child_candidate_id FROM lineage_edges
          WHERE parent_candidate_id = ?
          UNION
          SELECT edge.child_candidate_id
          FROM lineage_edges edge
          JOIN reachable ON edge.parent_candidate_id = reachable.candidate_id
        )
        SELECT 1 FROM reachable WHERE candidate_id = ? LIMIT 1
        """,
        (child, parent),
    ).fetchone()
    if reachable is not None:
        raise LineageCycle("lineage edge would create a cycle")
    lineage_id = endpoint_lineages[parent]
    root = conn.execute(
        "SELECT root_candidate_id FROM lineages WHERE lineage_id = ?",
        (lineage_id,),
    ).fetchone()[0]
    if child == root:
        raise ValueError("lineage root cannot have an explicit parent")
    existing_parent = conn.execute(
        """
        SELECT DISTINCT parent_candidate_id FROM lineage_edges
        WHERE child_candidate_id = ?
        """,
        (child,),
    ).fetchall()
    if existing_parent and any(item[0] != parent for item in existing_parent):
        raise ValueError("lineage child already has another explicit parent")
    if parent != root:
        root_reaches_parent = conn.execute(
            """
            WITH RECURSIVE reachable(candidate_id) AS (
              SELECT child_candidate_id FROM lineage_edges
              WHERE parent_candidate_id = ?
              UNION
              SELECT edge.child_candidate_id
              FROM lineage_edges edge
              JOIN reachable ON edge.parent_candidate_id = reachable.candidate_id
            )
            SELECT 1 FROM reachable WHERE candidate_id = ? LIMIT 1
            """,
            (root, parent),
        ).fetchone()
        if root_reaches_parent is None:
            raise ValueError("lineage parent is not descended from its root")
    conn.execute(
        """
        INSERT INTO lineage_edges(
          parent_candidate_id, child_candidate_id, relation_type,
          evidence_artifact_id
        ) VALUES(?, ?, ?, ?)
        """,
        (parent, child, relation, evidence),
    )


def add_lineage_edge(
    conn,
    parent_candidate_id,
    child_candidate_id,
    relation_type,
    evidence_artifact_id,
    authority,
):
    if authority not in ALLOWED_EDGE_AUTHORITIES:
        raise ValueError("lineage edges require explicit non-similarity authority")
    if not evidence_artifact_id:
        raise ValueError("lineage edges require a durable evidence artifact")
    conn.execute("BEGIN IMMEDIATE")
    try:
        evidence = conn.execute(
            "SELECT state FROM artifacts WHERE artifact_id = ?",
            (evidence_artifact_id,),
        ).fetchone()
        if evidence is None or evidence[0] not in ("installed", "archived"):
            raise ValueError("lineage evidence is not durable")
        _insert_lineage_edge(
            conn,
            parent_candidate_id,
            child_candidate_id,
            relation_type,
            evidence_artifact_id,
        )
        edge_version = "lineage-edge-v1:" + _sha(
            _json_bytes(
                {
                    "parent_candidate_id": parent_candidate_id,
                    "child_candidate_id": child_candidate_id,
                    "relation_type": relation_type,
                    "evidence_artifact_id": evidence_artifact_id,
                }
            )
        )
        queue_search_projections(
            conn,
            (parent_candidate_id, child_candidate_id),
            edge_version,
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def import_near_sa_observations(conn, path):
    source = pathlib.Path(path)
    data = _read_regular_file_nofollow(
        source, "near-SA observation snapshot"
    )
    rows = []
    for number, raw_line in enumerate(data.splitlines(), 1):
        if not raw_line:
            continue
        try:
            fields = raw_line.decode("utf-8").split("\t")
        except UnicodeDecodeError as exc:
            raise ImportConflict(f"near-SA row {number} is not UTF-8") from exc
        if len(fields) != 7:
            raise ImportConflict(f"near-SA row {number} must have seven fields")
        date, origin, story, _theme, overlap, votes, category = fields
        if any(
            not value
            for value in (
                date,
                origin,
                story,
                _theme,
                overlap,
                votes,
                category,
            )
        ):
            raise ImportConflict(
                f"near-SA row {number} has an empty required field"
            )
        vote_parts = votes.split(",")
        if (
            not vote_parts
            or any(
                vote not in {"0", "1", "2"}
                for vote in vote_parts
            )
        ):
            raise ImportConflict(
                f"near-SA row {number} has an invalid vote vector"
            )
        canonical = canonical_story_v1(story)
        candidates = conn.execute(
            """
            SELECT c.candidate_id, c.source_sequence, c.source, c.theme,
                   c.verdict, c.overlap, c.category
            FROM candidates c
            JOIN story_aliases a ON a.lineage_id = c.lineage_id
            WHERE a.canonical_version = ? AND a.canonical_story = ?
              AND c.story = ?
            ORDER BY c.source_sequence
            """,
            (CANONICAL_VERSION, canonical, story),
        ).fetchall()
        unique = {
            (item["candidate_id"], item["source_sequence"]): item
            for item in candidates
        }
        if len(unique) != 1:
            raise ImportConflict(
                f"near-SA row {number} resolves to {len(unique)} candidates"
            )
        (candidate_id, source_sequence), candidate = next(
            iter(unique.items())
        )
        if (
            _theme != candidate["theme"]
            or overlap != candidate["overlap"]
            or category != candidate["category"]
            or category
            not in {"design-fixable", "evidence-incomplete"}
            or candidate["source"] != "hunt"
            or candidate["verdict"]
            not in {"accept-w-rev", "reject"}
        ):
            raise ImportConflict(
                f"near-SA row {number} contradicts its canonical row"
            )
        observation_id = _sha(b"near-sa-v1\0" + raw_line)
        rows.append(
            (
                observation_id,
                candidate_id,
                source_sequence,
                sum(part == "2" for part in vote_parts),
                votes,
                overlap,
                category,
                origin,
                date,
            )
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        for item in rows:
            conn.execute(
                """
                INSERT OR IGNORE INTO near_sa_observations(
                  observation_id, candidate_id, source_sequence, sa_votes,
                  vote_vector, overlap, category, reason, observed_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                item,
            )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {"observations": len(rows)}


def select_generation_parent(conn):
    started = not conn.in_transaction
    if started:
        conn.execute("BEGIN")
    try:
        eligible = conn.execute(
            """
            WITH latest_observation AS (
              SELECT o.*
              FROM near_sa_observations o
              WHERE NOT EXISTS (
                SELECT 1 FROM near_sa_observations newer
                WHERE newer.candidate_id = o.candidate_id
                  AND (newer.source_sequence > o.source_sequence
                       OR (newer.source_sequence = o.source_sequence
                           AND newer.observation_id > o.observation_id))
              )
            )
            SELECT c.*
            FROM latest_observation o
            JOIN candidates c
              ON c.candidate_id = o.candidate_id
             AND c.source_sequence = o.source_sequence
            WHERE o.sa_votes >= 1
              AND o.category IN ('design-fixable', 'evidence-incomplete')
              AND c.category = o.category
              AND c.overlap = o.overlap
              AND c.source = 'hunt'
              AND c.verdict IN ('accept-w-rev', 'reject')
              AND NOT EXISTS (
                SELECT 1 FROM candidates newer
                WHERE newer.lineage_id = c.lineage_id
                  AND newer.source_sequence > c.source_sequence
              )
            ORDER BY c.source_sequence DESC
            """
        ).fetchall()
        result = None
        for row in eligible:
            canonical = canonical_story_v1(row["story"])
            lineage_stories = conn.execute(
                "SELECT story FROM candidates WHERE lineage_id = ?",
                (row["lineage_id"],),
            ).fetchall()
            canonical_count = sum(
                canonical_story_v1(item[0]) == canonical
                for item in lineage_stories
            )
            if canonical_count < 2:
                result = dict(row)
                break
        if started:
            conn.execute("COMMIT")
        return result
    except Exception:
        if started and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _normalize_targets(conn, targets, state_root):
    expected_state = _store_state_root(conn).resolve()
    provided_state = pathlib.Path(state_root).resolve()
    if expected_state != provided_state:
        raise ValueError("projection state root does not match the canonical store")
    normalized = {
        key: _validate_destination(
            conn, value, provided_state, allow_ledger_projection=True
        )
        for key, value in dict(targets).items()
    }
    if set(normalized) != set(TARGET_NAMES):
        raise ValueError(f"targets must be exactly {TARGET_NAMES}")
    if normalized[TARGET_NAMES[0]] == normalized[TARGET_NAMES[1]]:
        raise ValueError("ledger projection targets must be distinct")
    left = normalized[TARGET_NAMES[0]]
    right = normalized[TARGET_NAMES[1]]
    if left.exists() and right.exists() and os.path.samefile(str(left), str(right)):
        raise ValueError("ledger projection targets cannot alias one inode")
    return normalized


def pending_ledger_projection_count(conn):
    return conn.execute(
        "SELECT count(*) FROM ledger_projection_outbox WHERE state != 'done'"
    ).fetchone()[0]


def _claim_projection(conn, now, allow_live_reclaim=False):
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """
            SELECT * FROM ledger_projection_outbox
            WHERE state != 'done'
            ORDER BY projection_sequence DESC LIMIT 1
            """
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        live = (
            row["state"] == "processing"
            and float(row["lease_until"]) > float(now)
        )
        if live and not allow_live_reclaim:
            conn.execute("COMMIT")
            return None
        token = secrets.token_hex(24)
        generation = row["generation"] + 1
        lease_until = float(now) + LEASE_SECONDS
        conn.execute(
            """
            UPDATE ledger_projection_outbox
            SET state = 'processing', generation = ?, claim_token = ?,
                lease_until = ?
            WHERE projection_sequence = ?
            """,
            (generation, token, str(lease_until), row["projection_sequence"]),
        )
        conn.execute("COMMIT")
        return {
            "projection_sequence": row["projection_sequence"],
            "snapshot_sha256": row["snapshot_sha256"],
            "row_count": row["row_count"],
            "generation": generation,
            "claim_token": token,
            "lease_until": lease_until,
        }
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def claim_ledger_projection(conn, now=None):
    state_root = _store_state_root(conn)
    with _export_lock(state_root):
        return _claim_projection(conn, time.time() if now is None else now)


def renew_ledger_projection_claim(conn, claim, now=None):
    current_time = time.time() if now is None else float(now)
    state_root = _store_state_root(conn)
    with _export_lock(state_root):
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = _verify_claim(conn, claim)
            lease_until = max(
                float(row["lease_until"]), current_time + LEASE_SECONDS
            )
            conn.execute(
                """
                UPDATE ledger_projection_outbox
                SET lease_until = ?
                WHERE projection_sequence = ? AND generation = ?
                  AND claim_token = ?
                """,
                (
                    str(lease_until),
                    claim["projection_sequence"],
                    claim["generation"],
                    claim["claim_token"],
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    renewed = dict(claim)
    renewed["lease_until"] = lease_until
    return renewed


def _fault(name, requested):
    if requested == name:
        raise InjectedCrash(name)


def _atomic_replace(path, data, temp_fault, rename_fault, parent_fault, requested=None):
    path = pathlib.Path(path)
    _durable_mkdir(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if temp_fault:
            _fault(temp_fault, requested)
        os.replace(temporary, path)
        if rename_fault:
            _fault(rename_fault, requested)
        _fsync_directory(path.parent)
        if parent_fault:
            _fault(parent_fault, requested)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def _export_lock(state_root):
    root = pathlib.Path(state_root)
    _durable_mkdir(root)
    lock_path = root / "ledger-export.lock"
    created = not lock_path.exists()
    with lock_path.open("a+b") as stream:
        if created:
            stream.flush()
            os.fsync(stream.fileno())
            _fsync_directory(root)
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _current_projection(conn):
    sequence = int(_meta(conn, "projection_sequence", "0"))
    if sequence == 0:
        return None
    row = conn.execute(
        "SELECT * FROM ledger_projection_outbox WHERE projection_sequence = ?",
        (sequence,),
    ).fetchone()
    if row is None:
        raise ProjectionConflict("projection sequence has no outbox row")
    return row


def _validate_pointer(
    pointer_path, sequence, digest, row_count=None, immutable_object=None
):
    if not pointer_path.exists():
        return None
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProjectionConflict("ledger pointer is malformed") from exc
    pointer_sequence = int(pointer["sequence"])
    if pointer_sequence > sequence:
        raise ProjectionConflict("ledger pointer is ahead of the database")
    if pointer_sequence == sequence and pointer.get("sha256") != digest:
        raise ProjectionConflict("ledger pointer hash conflicts at equal sequence")
    if pointer_sequence == sequence and row_count is not None:
        if int(pointer.get("row_count", -1)) != int(row_count):
            raise ProjectionConflict("ledger pointer row count conflicts")
        if pointer.get("immutable_object") != immutable_object:
            raise ProjectionConflict("ledger pointer immutable object conflicts")
    return pointer


def _verify_claim(conn, claim):
    current = _current_projection(conn)
    if current is None or current["projection_sequence"] != claim["projection_sequence"]:
        raise StaleClaim("claim is not for the current projection")
    row = conn.execute(
        """
        SELECT * FROM ledger_projection_outbox
        WHERE projection_sequence = ?
        """,
        (claim["projection_sequence"],),
    ).fetchone()
    if (
        row["state"] != "processing"
        or row["generation"] != claim["generation"]
        or row["claim_token"] != claim["claim_token"]
    ):
        raise StaleClaim("projection claim token was fenced")
    return row


def _receipt_path(state_root, target):
    filename = target.replace("/", "__") + ".json"
    return pathlib.Path(state_root) / "ledger-target-receipts" / filename


def _verify_target_receipt(
    conn, state, target_name, sequence, digest, byte_count
):
    receipt_path = _receipt_path(state, target_name)
    try:
        receipt_bytes = receipt_path.read_bytes()
        receipt = json.loads(receipt_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ProjectionConflict(f"{target_name} receipt is malformed") from exc
    if _json_bytes(receipt) != receipt_bytes:
        raise ProjectionConflict(f"{target_name} receipt is not canonical")
    if (
        receipt.get("target") != target_name
        or receipt.get("published_sequence") != sequence
        or receipt.get("sha256") != digest
        or receipt.get("byte_count") != byte_count
        or not receipt.get("installed_at")
    ):
        raise ProjectionConflict(f"{target_name} receipt binding conflicts")
    database_receipt = conn.execute(
        """
        SELECT published_sequence, snapshot_sha256, receipt_sha256, installed_at
        FROM ledger_projection_receipts
        WHERE projection_sequence = ? AND target = ?
        """,
        (sequence, target_name),
    ).fetchone()
    expected = (
        sequence,
        digest,
        _sha(receipt_bytes),
        receipt["installed_at"],
    )
    if database_receipt is None or tuple(database_receipt) != expected:
        raise ProjectionConflict(f"{target_name} SQLite receipt conflicts")
    _fsync_existing(receipt_path)


def _publish_effects(conn, claim, targets, state_root, fault_after=None, verify_claim=True):
    state = pathlib.Path(state_root)
    targets = _normalize_targets(conn, targets, state)
    row = _verify_claim(conn, claim) if verify_claim else _current_projection(conn)
    if row is None:
        return None
    sequence = row["projection_sequence"]
    data = render_tsv(conn)
    digest = _sha(data)
    row_count = conn.execute("SELECT count(*) FROM candidates").fetchone()[0]
    if digest != row["snapshot_sha256"] or row_count != row["row_count"]:
        raise ProjectionConflict("current DB projection differs from its outbox row")
    snapshots = state / "ledger-snapshots"
    snapshot = snapshots / f"{sequence}-{digest}.tsv"
    immutable_object = str(snapshot.relative_to(state))
    pointer_path = state / "ledger-current.json"
    pointer = _validate_pointer(
        pointer_path, sequence, digest, row_count, immutable_object
    )
    if verify_claim:
        final_row = _verify_claim(conn, claim)
        if (
            final_row["snapshot_sha256"] != digest
            or final_row["row_count"] != row_count
        ):
            raise StaleClaim("projection changed before filesystem publication")
    if snapshot.exists():
        if snapshot.read_bytes() != data:
            raise ProjectionConflict("immutable snapshot content conflicts")
        _fsync_existing(snapshot)
    else:
        _atomic_replace(
            snapshot,
            data,
            "snapshot_temp_fsync",
            "snapshot_rename",
            "snapshot_parent_fsync",
            fault_after,
        )
    if pointer is None or int(pointer["sequence"]) < sequence:
        pointer_value = {
            "immutable_object": immutable_object,
            "row_count": row_count,
            "sequence": sequence,
            "sha256": digest,
        }
        _atomic_replace(
            pointer_path,
            _json_bytes(pointer_value),
            "pointer_temp_fsync",
            "pointer_rename",
            "pointer_parent_fsync",
            fault_after,
        )
    _fsync_existing(pointer_path)
    installed_at = _utc_now()
    for target_name, prefix in (
        ("ledger.tsv", "ledger"),
        ("tmp/ledger.good", "ledger_good"),
    ):
        destination = targets[target_name]
        _atomic_replace(
            destination,
            data,
            f"{prefix}_temp_fsync",
            f"{prefix}_rename",
            f"{prefix}_parent_fsync",
            fault_after,
        )
        if _sha(destination.read_bytes()) != digest:
            raise ProjectionConflict(f"{target_name} failed post-write verification")
        receipt_value = {
            "byte_count": len(data),
            "installed_at": installed_at,
            "published_sequence": sequence,
            "sha256": digest,
            "target": target_name,
        }
        receipt_bytes = _json_bytes(receipt_value)
        receipt_path = _receipt_path(state, target_name)
        _atomic_replace(receipt_path, receipt_bytes, None, None, None)
        _fault(f"{prefix}_receipt_fsync", fault_after)
        conn.execute("BEGIN IMMEDIATE")
        try:
            if verify_claim:
                _verify_claim(conn, claim)
            conn.execute(
                """
                INSERT INTO ledger_projection_receipts(
                  projection_sequence, target, published_sequence,
                  snapshot_sha256, receipt_sha256, installed_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(projection_sequence, target) DO UPDATE SET
                  published_sequence = excluded.published_sequence,
                  snapshot_sha256 = excluded.snapshot_sha256,
                  receipt_sha256 = excluded.receipt_sha256,
                  installed_at = excluded.installed_at
                """,
                (
                    sequence,
                    target_name,
                    sequence,
                    digest,
                    _sha(receipt_bytes),
                    installed_at,
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    pointer = _validate_pointer(
        pointer_path, sequence, digest, row_count, immutable_object
    )
    if pointer is None or int(pointer["sequence"]) != sequence:
        raise ProjectionConflict("durable pointer did not advance")
    for target_name, destination in targets.items():
        if _sha(destination.read_bytes()) != digest:
            raise ProjectionConflict(f"{target_name} changed before completion")
        _verify_target_receipt(
            conn, state, target_name, sequence, digest, len(data)
        )
    return {"sequence": sequence, "sha256": digest, "row_count": row_count}


def _render_projection_prefix(conn, row_count):
    rows = conn.execute(
        """
        SELECT raw_row, row_terminator
        FROM candidates ORDER BY source_sequence LIMIT ?
        """,
        (row_count,),
    ).fetchall()
    if len(rows) != row_count:
        raise ProjectionConflict("projection row count exceeds canonical history")
    header_b64 = _meta(conn, "ledger_header_b64")
    header = HEADER if header_b64 is None else base64.b64decode(header_b64)
    return _render_projection_rows(header, rows)


def _satisfy_older_projections_from_current_done(conn, publication, now):
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = _current_projection(conn)
        if (
            current is None
            or current["state"] != "done"
            or current["projection_sequence"] != publication["sequence"]
            or current["snapshot_sha256"] != publication["sha256"]
            or current["row_count"] != publication["row_count"]
        ):
            raise ProjectionConflict(
                "current completed projection changed during reconciliation"
            )
        completed_at = _utc_now()
        older = conn.execute(
            """
            SELECT * FROM ledger_projection_outbox
            WHERE projection_sequence < ? AND state != 'done'
            ORDER BY projection_sequence
            """,
            (publication["sequence"],),
        ).fetchall()
        for item in older:
            if item["state"] == "processing":
                try:
                    live_claim = (
                        item["lease_until"] is not None
                        and float(item["lease_until"]) > now
                    )
                except (TypeError, ValueError):
                    live_claim = False
                if live_claim:
                    continue
            if item["state"] not in ("pending", "processing"):
                continue
            if item["row_count"] > publication["row_count"]:
                continue
            prefix = _render_projection_prefix(conn, item["row_count"])
            if _sha(prefix) != item["snapshot_sha256"]:
                continue
            conn.execute(
                """
                UPDATE ledger_projection_outbox
                SET state = 'done', claim_token = NULL, lease_until = NULL,
                    satisfied_by_sequence = ?, satisfied_by_sha256 = ?,
                    completed_at = ?
                WHERE projection_sequence = ? AND state != 'done'
                """,
                (
                    publication["sequence"],
                    publication["sha256"],
                    completed_at,
                    item["projection_sequence"],
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _mark_projection_complete(conn, claim, publication, verify_claim=True):
    conn.execute("BEGIN IMMEDIATE")
    try:
        if verify_claim:
            _verify_claim(conn, claim)
        now = _utc_now()
        conn.execute(
            """
            UPDATE ledger_projection_outbox
            SET state = 'done', claim_token = NULL, lease_until = NULL,
                satisfied_by_sequence = ?, satisfied_by_sha256 = ?,
                completed_at = ?
            WHERE projection_sequence <= ? AND state != 'done'
            """,
            (
                publication["sequence"],
                publication["sha256"],
                now,
                publication["sequence"],
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _publish_claimed_locked(
    conn, claim, targets, state_root, fault_after=None
):
    publication = _publish_effects(
        conn, claim, targets, state_root, fault_after, verify_claim=True
    )
    _mark_projection_complete(conn, claim, publication, verify_claim=True)
    _fault("db_mark", fault_after)
    return publication


def publish_claimed_ledger_projection(conn, claim, targets, state_root):
    state = pathlib.Path(state_root)
    normalized = _normalize_targets(conn, targets, state)
    with _export_lock(state):
        return _publish_claimed_locked(conn, claim, normalized, state)


def materialize_ledger_projection(
    conn, targets, state_root, fault_after=None
):
    state = pathlib.Path(state_root)
    normalized = _normalize_targets(conn, targets, state)
    with _export_lock(state):
        claim = _claim_projection(conn, time.time())
        if claim is None:
            return _reconcile_ledger_projection_locked(
                conn, normalized, state, time.time()
            )
        _fault("db_commit", fault_after)
        return _publish_claimed_locked(
            conn, claim, normalized, state, fault_after=fault_after
        )


def _reconcile_ledger_projection_locked(
    conn, targets, state, now, reclaim_live=False
):
    targets = _normalize_targets(conn, targets, state)
    current = _current_projection(conn)
    if current is None:
        return None
    data = render_tsv(conn)
    digest = _sha(data)
    if digest != current["snapshot_sha256"]:
        raise ProjectionConflict("current database projection hash is inconsistent")
    immutable_object = (
        f"ledger-snapshots/{current['projection_sequence']}-{digest}.tsv"
    )
    _validate_pointer(
        state / "ledger-current.json",
        current["projection_sequence"],
        digest,
        current["row_count"],
        immutable_object,
    )
    if current["state"] == "done":
        publication = _publish_effects(
            conn,
            None,
            targets,
            state,
            verify_claim=False,
        )
        _satisfy_older_projections_from_current_done(
            conn, publication, float(now)
        )
        return publication
    claim = _claim_projection(
        conn, now, allow_live_reclaim=reclaim_live
    )
    if claim is None:
        return None
    return _publish_claimed_locked(conn, claim, targets, state)


def reconcile_ledger_projection(
    conn,
    targets,
    state_root,
    now=None,
    reclaim_live=False,
):
    state = pathlib.Path(state_root)
    normalized = _normalize_targets(conn, targets, state)
    with _export_lock(state):
        return _reconcile_ledger_projection_locked(
            conn,
            normalized,
            state,
            time.time() if now is None else float(now),
            reclaim_live=reclaim_live,
        )


def validate_store(conn):
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    cycle = conn.execute(
        """
        WITH RECURSIVE walk(origin, candidate_id) AS (
          SELECT parent_candidate_id, child_candidate_id FROM lineage_edges
          UNION
          SELECT walk.origin, edge.child_candidate_id
          FROM walk JOIN lineage_edges edge
            ON edge.parent_candidate_id = walk.candidate_id
        )
        SELECT 1 FROM walk WHERE origin = candidate_id LIMIT 1
        """
    ).fetchone()
    deferred = {
        item[0]
        for item in conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table'
              AND name IN (
                'reentry_grants', 'reentry_requests', 'round_slots',
                'materialization_outbox'
              )
            """
        )
    }
    issues = []
    if integrity != "ok":
        issues.append(f"integrity_check={integrity}")
    if foreign_keys:
        issues.append(f"foreign_key_violations={len(foreign_keys)}")
    if cycle is not None:
        issues.append("lineage_cycle")
    for candidate in conn.execute(
        "SELECT * FROM candidates ORDER BY source_sequence"
    ):
        try:
            _validate_candidate_record(conn, candidate)
        except ImportConflict:
            issues.append(
                f"candidate_record_invalid:{candidate['candidate_id']}"
            )
    near_sa_identity_mismatch = conn.execute(
        """
        SELECT 1
        FROM near_sa_observations observation
        LEFT JOIN candidates candidate
          ON candidate.candidate_id = observation.candidate_id
         AND candidate.source_sequence = observation.source_sequence
        WHERE candidate.candidate_id IS NULL
        LIMIT 1
        """
    ).fetchone()
    if near_sa_identity_mismatch is not None:
        issues.append("near_sa_candidate_identity_mismatch")
    if _meta(conn, BOOTSTRAP_MARKER_KEY) is not None:
        try:
            validated_bootstrap_marker(conn)
        except ImportConflict:
            issues.append("bootstrap_marker_invalid")
    cross_lineage = conn.execute(
        """
        SELECT 1 FROM lineage_edges edge
        JOIN candidates parent ON parent.candidate_id = edge.parent_candidate_id
        JOIN candidates child ON child.candidate_id = edge.child_candidate_id
        WHERE parent.lineage_id != child.lineage_id
        LIMIT 1
        """
    ).fetchone()
    if cross_lineage is not None:
        issues.append("cross_lineage_edge")
    multiple_parent = conn.execute(
        """
        SELECT child_candidate_id FROM lineage_edges
        GROUP BY child_candidate_id
        HAVING count(DISTINCT parent_candidate_id) > 1
        LIMIT 1
        """
    ).fetchone()
    if multiple_parent is not None:
        issues.append("multiple_explicit_parents")
    rooted_child = conn.execute(
        """
        SELECT 1 FROM lineage_edges edge
        JOIN lineages lineage
          ON lineage.root_candidate_id = edge.child_candidate_id
        LIMIT 1
        """
    ).fetchone()
    if rooted_child is not None:
        issues.append("lineage_root_has_parent")
    outside_root_ancestry = conn.execute(
        """
        WITH RECURSIVE reached(lineage_id, candidate_id) AS (
          SELECT lineage_id, root_candidate_id FROM lineages
          UNION
          SELECT reached.lineage_id, edge.child_candidate_id
          FROM reached
          JOIN lineage_edges edge
            ON edge.parent_candidate_id = reached.candidate_id
          JOIN candidates child
            ON child.candidate_id = edge.child_candidate_id
           AND child.lineage_id = reached.lineage_id
        )
        SELECT 1
        FROM lineage_edges edge
        JOIN candidates parent
          ON parent.candidate_id = edge.parent_candidate_id
        WHERE NOT EXISTS (
          SELECT 1 FROM reached
          WHERE reached.lineage_id = parent.lineage_id
            AND reached.candidate_id = parent.candidate_id
        )
        LIMIT 1
        """
    ).fetchone()
    if outside_root_ancestry is not None:
        issues.append("lineage_edge_outside_root_ancestry")
    state_value = _meta(conn, "state_root")
    if state_value:
        state_root = pathlib.Path(state_value)
        for epoch in conn.execute(
            "SELECT * FROM import_epochs ORDER BY committed_at, epoch_id"
        ).fetchall():
            plan_path = state_root / "import-plans" / f"{epoch['plan_sha256']}.json"
            try:
                plan_bytes = _read_content_addressed_file(
                    plan_path,
                    epoch["plan_sha256"],
                    "sealed validation import plan",
                )
                plan_body = json.loads(plan_bytes)
                if (
                    not isinstance(plan_body, dict)
                    or set(plan_body) != set(IMPORT_PLAN_FIELDS)
                    or _json_bytes(plan_body) != plan_bytes
                ):
                    raise ImportConflict("sealed import plan hash conflicts")
                manifest_path = (
                    state_root
                    / "import-manifests"
                    / f"{epoch['input_manifest_sha256']}.json"
                )
                plan = dict(plan_body)
                plan.update(
                    {
                        "plan_sha256": epoch["plan_sha256"],
                        "plan_path": str(plan_path),
                        "manifest_path": str(manifest_path),
                    }
                )
                if (
                    plan["input_manifest_sha256"]
                    != epoch["input_manifest_sha256"]
                ):
                    raise ImportConflict(
                        "sealed plan input manifest conflicts with epoch"
                    )
                _validate_sealed_import_manifest(
                    plan,
                    manifest_path,
                    state_root / "import-cas",
                )
                _verify_epoch_results(
                    conn,
                    plan,
                    epoch,
                    state_root / "import-cas",
                    repair=False,
                )
            except (OSError, ValueError, HistoryStoreError) as exc:
                issues.append(
                    f"import_epoch_inconsistent:{epoch['epoch_id']}:{type(exc).__name__}"
                )
    if deferred:
        issues.append("deferred_awr_tables_present=" + ",".join(sorted(deferred)))
    current_sequence = int(_meta(conn, "projection_sequence", "0"))
    current_projection = conn.execute(
        """
        SELECT state FROM ledger_projection_outbox
        WHERE projection_sequence = ?
        """,
        (current_sequence,),
    ).fetchone()
    if current_projection is not None and current_projection["state"] == "done":
        now = time.time()
        for projection in conn.execute(
            """
            SELECT state, lease_until
            FROM ledger_projection_outbox
            WHERE projection_sequence < ? AND state != 'done'
            """,
            (current_sequence,),
        ):
            live_claim = False
            if projection["state"] == "processing":
                try:
                    live_claim = (
                        projection["lease_until"] is not None
                        and float(projection["lease_until"]) > now
                    )
                except (TypeError, ValueError):
                    live_claim = False
            if not live_claim:
                issues.append("older_pending_projection_behind_current_done")
                break
    return {
        "ok": not issues,
        "issues": issues,
        "candidates": conn.execute("SELECT count(*) FROM candidates").fetchone()[0],
        "lineages": conn.execute("SELECT count(*) FROM lineages").fetchone()[0],
        "projection_sequence": int(_meta(conn, "projection_sequence", "0")),
        "pending_ledger_projections": pending_ledger_projection_count(conn),
    }
