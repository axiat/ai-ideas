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
import tempfile
import time
import unicodedata


CANONICAL_VERSION = "canonical-story-v1"
HEADER = b"date\tsource\ttheme\tidea\tverdict\treason\toverlap\tcategory\n"
ALLOWED_RELATIONS = ("evolved_from", "recheck_of", "supersedes")
ALLOWED_EDGE_AUTHORITIES = ("explicit", "manual_mapping", "promotion")
TARGET_NAMES = ("ledger.tsv", "tmp/ledger.good")
LEASE_SECONDS = 60


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
CREATE TABLE IF NOT EXISTS import_epochs(
  epoch_id TEXT PRIMARY KEY,
  input_manifest_sha256 TEXT NOT NULL UNIQUE,
  plan_sha256 TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL CHECK(state = 'done'),
  row_count INTEGER NOT NULL CHECK(row_count >= 0),
  result_sha256 TEXT NOT NULL,
  committed_at TEXT NOT NULL
);
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
  UNIQUE(candidate_id, lineage_id)
);
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
  UNIQUE(candidate_id, source_sequence)
);
CREATE TABLE IF NOT EXISTS history_receipts(
  receipt_id TEXT PRIMARY KEY,
  query_candidate_id TEXT NOT NULL,
  intent TEXT NOT NULL,
  pack_sha256 TEXT NOT NULL,
  retrieval_policy_version TEXT NOT NULL,
  source_watermark INTEGER NOT NULL,
  index_generation INTEGER NOT NULL,
  comparator_version TEXT NOT NULL,
  status TEXT NOT NULL,
  receipt_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS search_projection_outbox(
  record_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  projection_kind TEXT NOT NULL,
  content_version TEXT NOT NULL,
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
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def init_schema(conn):
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('schema_version', '1')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('projection_sequence', '0')"
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
    if isinstance(value, pathlib.Path):
        raw = value.read_text(encoding="utf-8")
    elif isinstance(value, os.PathLike):
        raw = pathlib.Path(value).read_text(encoding="utf-8")
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


def _seal_object(path, cas_root):
    source = pathlib.Path(path)
    data = source.read_bytes()
    digest = _sha(data)
    cas_root.mkdir(parents=True, exist_ok=True)
    destination = cas_root / digest
    if destination.exists():
        if destination.read_bytes() != data:
            raise ImportConflict(f"CAS collision for {source}")
    else:
        _write_immutable(destination, data)
    return {
        "source_path": str(source.resolve()),
        "sha256": digest,
        "byte_count": len(data),
        "cas_path": str(destination.resolve()),
    }


def _fsync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_immutable(path, data):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ImportConflict(f"immutable object conflicts at {path}")
        return
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
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


def _mapping_data(path):
    if not path:
        return None
    try:
        value = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ImportConflict("lineage mapping manifest is not valid JSON") from exc
    if value.get("version") != "lineage-mapping-v1":
        raise ImportConflict("unsupported lineage mapping version")
    return value


def _union_source_data(sealed_object, source_kind):
    try:
        value = json.loads(
            pathlib.Path(sealed_object["cas_path"]).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
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
    instance_value = inputs.get("ledger_instance_id")
    if instance_value is None:
        instance_value = inputs.get(
            "ledger_instance_id_path", ledger.parent / "ledger.instance-id"
        )
    ledger_instance_id = _read_instance_id(instance_value)
    ledger_object = _seal_object(ledger, cas_root)
    ledger_object["role"] = "ledger"
    sealed = [ledger_object]
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
    data = pathlib.Path(sealed[0]["cas_path"]).read_bytes()
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
    explicit_mapping = _mapping_data(
        None if mapping_object is None else mapping_object["cas_path"]
    )
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
    _write_immutable(manifest_path, manifest_bytes)
    plan_body = {
        "schema_version": 1,
        "input_manifest_sha256": manifest_sha,
        "ledger_instance_id": ledger_instance_id,
        "header_b64": base64.b64encode(header_raw + header_terminator).decode("ascii"),
        "run_metadata": inputs.get("run_metadata"),
        "rows": rows,
        "edges": edges,
    }
    plan_bytes = _json_bytes(plan_body)
    plan_sha = _sha(plan_bytes)
    plan_path = state / "import-plans" / f"{plan_sha}.json"
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
    return {
        key: plan[key]
        for key in (
            "schema_version",
            "input_manifest_sha256",
            "ledger_instance_id",
            "header_b64",
            "run_metadata",
            "rows",
            "edges",
        )
    }


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


def _render_tsv_in_transaction(conn):
    header_b64 = _meta(conn, "ledger_header_b64")
    header = HEADER if header_b64 is None else base64.b64decode(header_b64)
    chunks = [header]
    for item in conn.execute(
        "SELECT raw_row, row_terminator FROM candidates ORDER BY source_sequence"
    ):
        chunks.extend((bytes(item[0]), bytes(item[1])))
    return b"".join(chunks)


def render_tsv(conn):
    return _render_tsv_in_transaction(conn)


def _queue_search_projection(conn, candidate):
    conn.execute(
        """
        INSERT INTO search_projection_outbox(
          record_id, projection_kind, content_version, source_sequence,
          state, generation, claim_token, lease_until
        ) VALUES(?, 'candidate', 'candidate-v1', ?, 'pending', 0, NULL, NULL)
        ON CONFLICT(record_id, projection_kind, content_version)
        DO UPDATE SET source_sequence = excluded.source_sequence,
                      state = 'pending', generation = generation + 1,
                      claim_token = NULL, lease_until = NULL
        """,
        (candidate["candidate_id"], candidate["row_number"]),
    )


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


def _verify_existing_candidate(existing, item):
    expected = (
        item["origin_stable_id"],
        item["lineage_id"],
        item["row_number"],
        item["raw_sha256"],
        item["candidate_id"],
    )
    actual = (
        existing["origin_stable_id"],
        existing["lineage_id"],
        existing["row_number"],
        existing["raw_sha256"],
        existing["candidate_id"],
    )
    if (
        actual != expected
        or bytes(existing["raw_row"]) != base64.b64decode(item["raw_row_b64"])
        or bytes(existing["row_terminator"])
        != base64.b64decode(item["row_terminator_b64"])
        or existing["field_count"] != item["field_count"]
    ):
        raise ImportConflict(
            f"append-only row location {item['row_number']} changed"
        )


def _insert_artifact_from_evidence(conn, evidence, source_sequence, cas_root):
    cas_path = pathlib.Path(cas_root) / evidence["sha256"]
    if not cas_path.is_file() or _sha(cas_path.read_bytes()) != evidence["sha256"]:
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
    return artifact_id


def commit_import_plan(conn, plan):
    plan_body = _plan_body(plan)
    calculated_sha = _sha(_json_bytes(plan_body))
    if calculated_sha != plan.get("plan_sha256"):
        raise ImportConflict("import plan content does not match its sealed hash")
    plan_path = pathlib.Path(plan["plan_path"])
    if not plan_path.is_file() or _sha(plan_path.read_bytes()) != calculated_sha:
        raise ImportConflict("sealed import plan is missing or changed")
    manifest_path = pathlib.Path(plan["manifest_path"])
    if (
        not manifest_path.is_file()
        or _sha(manifest_path.read_bytes()) != plan["input_manifest_sha256"]
    ):
        raise ImportConflict("sealed input manifest is missing or changed")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ImportConflict("sealed input manifest is malformed") from exc
    cas_root = plan_path.parent.parent / "import-cas"
    for artifact in manifest.get("artifacts", []):
        expected_artifact_id = _sha(
            b"import-artifact-v1\0" + artifact["sha256"].encode("ascii")
        )
        cas_path = cas_root / artifact["sha256"]
        if (
            artifact.get("artifact_id") != expected_artifact_id
            or not cas_path.is_file()
            or _sha(cas_path.read_bytes()) != artifact["sha256"]
        ):
            raise ImportConflict("sealed import input is missing or changed")
    epoch_id = _sha(
        b"import-epoch-v1\0" + plan["input_manifest_sha256"].encode("ascii")
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        previous = conn.execute(
            "SELECT * FROM import_epochs WHERE input_manifest_sha256 = ?",
            (plan["input_manifest_sha256"],),
        ).fetchone()
        if previous is not None:
            if previous["plan_sha256"] != calculated_sha:
                raise ImportConflict("input manifest is already bound to another plan")
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
                _verify_existing_candidate(existing, item)
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
                _verify_existing_candidate(existing, item)
                continue
            raw_row = base64.b64decode(item["raw_row_b64"])
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
                    base64.b64decode(item["row_terminator_b64"]),
                    json.dumps(
                        {
                            "import_epoch": epoch_id,
                            "run_metadata": plan.get("run_metadata"),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
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
        if inserted:
            _enqueue_ledger_projection(conn)
        result = _render_tsv_in_transaction(conn)
        result_sha = _sha(result)
        conn.execute(
            """
            INSERT INTO import_epochs(
              epoch_id, input_manifest_sha256, plan_sha256, state, row_count,
              result_sha256, committed_at
            ) VALUES(?, ?, ?, 'done', ?, ?, ?)
            """,
            (
                epoch_id,
                plan["input_manifest_sha256"],
                calculated_sha,
                len(plan["rows"]),
                result_sha,
                _utc_now(),
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
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


def import_tsv_epoch(conn, path):
    ledger = pathlib.Path(path)
    state_root = ledger.parent / ".ai-ideas"
    plan = build_import_plan({"ledger": ledger}, state_root)
    return commit_import_plan(conn, plan)


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


def append_rows(conn, rows, provenance):
    raw_rows = [_normalize_append_row(item) for item in rows]
    if not raw_rows:
        return {"appended": 0, "projection_sequence": None}
    conn.execute("BEGIN IMMEDIATE")
    try:
        instance = _meta(conn, "ledger_instance_id")
        if not instance:
            raise HistoryStoreError("store is not bound to ledger.instance-id")
        next_sequence = (
            conn.execute(
                "SELECT COALESCE(MAX(source_sequence), 0) + 1 FROM candidates"
            ).fetchone()[0]
        )
        last = conn.execute(
            """
            SELECT candidate_id, row_terminator FROM candidates
            ORDER BY source_sequence DESC LIMIT 1
            """
        ).fetchone()
        if last is not None and bytes(last["row_terminator"]) == b"":
            conn.execute(
                "UPDATE candidates SET row_terminator = ? WHERE candidate_id = ?",
                (b"\n", last["candidate_id"]),
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
        projection_sequence = _enqueue_ledger_projection(conn)
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {
        "appended": len(candidate_ids),
        "candidate_ids": candidate_ids,
        "projection_sequence": projection_sequence,
        "provenance": dict(provenance),
    }


def get_candidate(conn, candidate_id):
    row = conn.execute(
        "SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,)
    ).fetchone()
    return None if row is None else dict(row)


def export_tsv(conn, path):
    destination = pathlib.Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = render_tsv(conn)
    _atomic_replace(destination, data, None, None, None)
    return {"path": str(destination), "sha256": _sha(data), "byte_count": len(data)}


def _insert_lineage_edge(conn, parent, child, relation, evidence):
    if relation not in ALLOWED_RELATIONS:
        raise ValueError("unsupported lineage relation")
    if parent == child:
        raise LineageCycle("self edges are cycles")
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
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def import_near_sa_observations(conn, path):
    source = pathlib.Path(path)
    rows = []
    for number, raw_line in enumerate(source.read_bytes().splitlines(), 1):
        if not raw_line:
            continue
        try:
            fields = raw_line.decode("utf-8").split("\t")
        except UnicodeDecodeError as exc:
            raise ImportConflict(f"near-SA row {number} is not UTF-8") from exc
        if len(fields) != 7:
            raise ImportConflict(f"near-SA row {number} must have seven fields")
        date, origin, story, _theme, overlap, votes, category = fields
        canonical = canonical_story_v1(story)
        candidates = conn.execute(
            """
            SELECT candidate_id, source_sequence FROM candidates c
            JOIN story_aliases a ON a.lineage_id = c.lineage_id
            WHERE a.canonical_version = ? AND a.canonical_story = ?
              AND c.story = ?
            ORDER BY c.source_sequence
            """,
            (CANONICAL_VERSION, canonical, story),
        ).fetchall()
        unique = {(item["candidate_id"], item["source_sequence"]) for item in candidates}
        if len(unique) != 1:
            raise ImportConflict(
                f"near-SA row {number} resolves to {len(unique)} candidates"
            )
        candidate_id, source_sequence = next(iter(unique))
        observation_id = _sha(b"near-sa-v1\0" + raw_line)
        rows.append(
            (
                observation_id,
                candidate_id,
                source_sequence,
                sum(part == "2" for part in votes.split(",")),
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
        row = conn.execute(
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
            JOIN candidates c ON c.candidate_id = o.candidate_id
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
              AND (
                SELECT count(*) FROM candidates same_story
                WHERE same_story.lineage_id = c.lineage_id
                  AND same_story.story = c.story
              ) < 2
            ORDER BY c.source_sequence DESC
            LIMIT 1
            """
        ).fetchone()
        result = None if row is None else dict(row)
        if started:
            conn.execute("COMMIT")
        return result
    except Exception:
        if started and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _normalize_targets(targets):
    normalized = {key: pathlib.Path(value) for key, value in dict(targets).items()}
    if set(normalized) != set(TARGET_NAMES):
        raise ValueError(f"targets must be exactly {TARGET_NAMES}")
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
    return _claim_projection(conn, time.time() if now is None else now)


def renew_ledger_projection_claim(conn, claim, now=None):
    current_time = time.time() if now is None else float(now)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _verify_claim(conn, claim)
        lease_until = current_time + LEASE_SECONDS
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
    path.parent.mkdir(parents=True, exist_ok=True)
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
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "ledger-export.lock"
    with lock_path.open("a+b") as stream:
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


def _publish_effects(conn, claim, targets, state_root, fault_after=None, verify_claim=True):
    targets = _normalize_targets(targets)
    state = pathlib.Path(state_root)
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
    if snapshot.exists():
        if snapshot.read_bytes() != data:
            raise ProjectionConflict("immutable snapshot content conflicts")
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
        receipt_path = _receipt_path(state, target_name)
        if not receipt_path.exists():
            raise ProjectionConflict(f"{target_name} receipt is missing")
    return {"sequence": sequence, "sha256": digest, "row_count": row_count}


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
    with _export_lock(state_root):
        return _publish_claimed_locked(conn, claim, targets, state_root)


def materialize_ledger_projection(
    conn, targets, state_root, fault_after=None
):
    with _export_lock(state_root):
        claim = _claim_projection(conn, time.time())
        if claim is None:
            return _reconcile_ledger_projection_locked(
                conn, targets, pathlib.Path(state_root)
            )
        _fault("db_commit", fault_after)
        return _publish_claimed_locked(
            conn, claim, targets, state_root, fault_after=fault_after
        )


def _reconcile_ledger_projection_locked(conn, targets, state):
    targets = _normalize_targets(targets)
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
        return _publish_effects(
            conn,
            None,
            targets,
            state,
            verify_claim=False,
        )
    claim = _claim_projection(conn, time.time(), allow_live_reclaim=True)
    if claim is None:
        raise ProjectionConflict("pending projection could not be claimed")
    return _publish_claimed_locked(conn, claim, targets, state)


def reconcile_ledger_projection(conn, targets, state_root):
    state = pathlib.Path(state_root)
    with _export_lock(state):
        return _reconcile_ledger_projection_locked(conn, targets, state)


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
    if deferred:
        issues.append("deferred_awr_tables_present=" + ",".join(sorted(deferred)))
    return {
        "ok": not issues,
        "issues": issues,
        "candidates": conn.execute("SELECT count(*) FROM candidates").fetchone()[0],
        "lineages": conn.execute("SELECT count(*) FROM lineages").fetchone()[0],
        "projection_sequence": int(_meta(conn, "projection_sequence", "0")),
        "pending_ledger_projections": pending_ledger_projection_count(conn),
    }
