#!/usr/bin/env python3
"""Deterministic, rebuildable search projections for canonical idea history."""

import hashlib
import json
import math
import re
import struct

try:
    from lib import history_store
except ImportError:  # Direct execution through lib/history_cli.py.
    import history_store


FACETS = (
    "problem_estimand",
    "claimed_delta",
    "mechanism",
    "evaluation_expected_signal",
    "setting_task",
    "entities_datasets_methods",
)
FAILURE_CODES = (
    "direct-hit", "strong-baseline", "statistical-power", "estimand",
    "attribution-control", "weak-prior-work", "novelty-cap", "feasibility",
    "evidence-incomplete", "other",
)
VECTOR_DIMENSIONS = 256
VECTOR_MODEL = "hash-ngram-v1"
VECTOR_REVISION = "1"
PREPROCESSING_VERSION = "search-text-v1"
PROJECTION_SCHEMA_VERSION = "history-projection-v2"
FTS_TOKENIZER = "unicode61"


class ProjectionError(RuntimeError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS search_index_entries(
  candidate_id TEXT PRIMARY KEY REFERENCES candidates(candidate_id),
  active INTEGER NOT NULL CHECK(active IN (0,1)),
  content_hash TEXT NOT NULL,
  indexed_generation INTEGER NOT NULL CHECK(indexed_generation >= 0)
);
CREATE TABLE IF NOT EXISTS search_vectors(
  candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  facet TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  vector BLOB NOT NULL,
  model TEXT NOT NULL,
  revision TEXT NOT NULL,
  preprocessing_version TEXT NOT NULL,
  dimensions INTEGER NOT NULL CHECK(dimensions = 256),
  metric TEXT NOT NULL CHECK(metric = 'cosine'),
  l2_norm REAL NOT NULL,
  PRIMARY KEY(candidate_id, facet)
);
CREATE TABLE IF NOT EXISTS search_index_generations(
  generation INTEGER PRIMARY KEY,
  source_watermark INTEGER NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  manifest_json TEXT NOT NULL DEFAULT '',
  policy_sha256 TEXT NOT NULL DEFAULT '',
  projection_schema_version TEXT NOT NULL DEFAULT '',
  fts_tokenizer TEXT NOT NULL DEFAULT '',
  vector_model TEXT NOT NULL DEFAULT '',
  vector_revision TEXT NOT NULL DEFAULT '',
  preprocessing_version TEXT NOT NULL DEFAULT '',
  dimensions INTEGER NOT NULL DEFAULT 0,
  metric TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
"""

_POLICY_FIXED = {
    "retrieval_policy_version": "retrieval-policy-v1",
    "mode": "shadow",
    "per_channel_depth": 50,
    "final_lineage_count": 10,
    "comparator_cutoff": 10,
    "max_matches": 10,
    "max_retrieval_tokens": 4096,
    "max_expansion_rounds": 1,
    "model_context_limit": 32768,
    "max_output_tokens": 2048,
    "safety_margin": 1024,
    "adapter_version": "history-stage-v1",
    "adapter_wrapper_allowance": 256,
    "tokenizer_identity": "history-stage-tokenizer-v1",
    "tokenizer_revision": "1",
    "rrf_k": 60,
}
_POLICY_CHANNELS = ["exact", "fts", "dense", "lineage"]
_POLICY_PROJECTION = {
    "schema_version": PROJECTION_SCHEMA_VERSION,
    "fts_tokenizer": FTS_TOKENIZER,
    "vector_model": VECTOR_MODEL,
    "vector_revision": VECTOR_REVISION,
    "preprocessing_version": PREPROCESSING_VERSION,
    "dimensions": VECTOR_DIMENSIONS,
    "metric": "cosine",
}
_POLICY_KEYS = set(_POLICY_FIXED) | {
    "mandatory_channels", "projection", "tested_adapter_allowances",
}


def load_policy(path):
    with open(path, "r", encoding="utf-8") as stream:
        policy = json.load(stream)
    if set(policy) != _POLICY_KEYS:
        raise ValueError("retrieval policy keys do not match v1")
    if any(policy.get(key) != value for key, value in _POLICY_FIXED.items()):
        raise ValueError("retrieval policy fixed values do not match v1")
    if policy.get("mandatory_channels") != _POLICY_CHANNELS:
        raise ValueError("retrieval policy channels do not match v1")
    if policy.get("projection") != _POLICY_PROJECTION:
        raise ValueError("retrieval policy projection versions do not match v1")
    if policy.get("tested_adapter_allowances") != {"history-stage-v1": 256}:
        raise ValueError("retrieval policy adapter allowance is not verified")
    return policy


def _init(conn):
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(search_index_generations)")}
    for name, definition in (
        ("manifest_json", "TEXT NOT NULL DEFAULT ''"),
        ("policy_sha256", "TEXT NOT NULL DEFAULT ''"),
        ("projection_schema_version", "TEXT NOT NULL DEFAULT ''"),
        ("fts_tokenizer", "TEXT NOT NULL DEFAULT ''"),
        ("vector_model", "TEXT NOT NULL DEFAULT ''"),
        ("vector_revision", "TEXT NOT NULL DEFAULT ''"),
        ("preprocessing_version", "TEXT NOT NULL DEFAULT ''"),
        ("dimensions", "INTEGER NOT NULL DEFAULT 0"),
        ("metric", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in columns:
            conn.execute("ALTER TABLE search_index_generations ADD COLUMN %s %s" % (name, definition))
    fts_columns = {row[1] for row in conn.execute("PRAGMA table_info(search_fts)")}
    if fts_columns and fts_columns != {"candidate_id", "facet", "content"}:
        conn.execute("DROP TABLE search_fts")
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(candidate_id UNINDEXED, facet UNINDEXED, content, tokenize='unicode61')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('history_index_generation', '0')"
    )


def _tokens(text):
    return re.findall(r"[\w]+", text.lower(), flags=re.UNICODE)


def _entities(text):
    return " ".join(sorted(set(_tokens(text))))


def _default_facets(candidate):
    story = candidate["story"]
    values = {
        "problem_estimand": story,
        "claimed_delta": story,
        "mechanism": "",
        "evaluation_expected_signal": "",
        "setting_task": candidate["theme"] + "\n" + story,
        "entities_datasets_methods": _entities(candidate["theme"] + " " + story),
    }
    return values


def facets_from_round_artifact(story, theme, artifact_text):
    """Extract the two generated-artifact-only facets without widening history."""
    if not all(isinstance(value, str) for value in (story, theme, artifact_text)):
        raise TypeError("round artifact fields must be text")
    facets = _default_facets({"story": story, "theme": theme})
    for field, facet in (
        ("Summary", "mechanism"),
        ("Minimal Falsification Experiment", "evaluation_expected_signal"),
    ):
        match = re.search(
            r"(?m)^" + re.escape(field) + r":\s*(.+)$", artifact_text
        )
        if match:
            facets[facet] = match.group(1).strip()
    return facets


def _content_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ensure_facets(conn, candidate_id):
    candidate = history_store.get_candidate(conn, candidate_id)
    if candidate is None:
        return
    for facet, text in _default_facets(candidate).items():
        if not text:
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO candidate_facets(candidate_id, facet, text, content_hash, source_artifact_id)
            VALUES(?, ?, ?, ?, NULL)
            """,
            (candidate_id, facet, text, _content_hash(text)),
        )


def _feature_sign(feature):
    digest = hashlib.sha256(feature.encode("utf-8")).digest()
    return int.from_bytes(digest[:2], "big") % VECTOR_DIMENSIONS, 1.0 if digest[2] & 1 else -1.0


def embed(text):
    tokens = _tokens(text)
    features = list(tokens)
    features.extend("w2:" + tokens[index] + "\0" + tokens[index + 1] for index in range(max(0, len(tokens) - 1)))
    compact = " ".join(tokens)
    features.extend("c3:" + compact[index:index + 3] for index in range(max(0, len(compact) - 2)))
    vector = [0.0] * VECTOR_DIMENSIONS
    for feature in features:
        index, sign = _feature_sign(feature)
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm:
        vector = [value / norm for value in vector]
    return vector, norm


def _blob(vector):
    return struct.pack("<%sf" % VECTOR_DIMENSIONS, *vector)


def _unblob(value):
    return struct.unpack("<%sf" % VECTOR_DIMENSIONS, bytes(value))


def _queue(conn, candidate_id, content_version):
    history_store.queue_search_projection(conn, candidate_id, content_version)


def update_candidate_facets(conn, candidate_id, updates):
    if not isinstance(updates, dict) or not updates:
        raise ValueError("facet updates are required")
    unknown = set(updates) - set(FACETS)
    if unknown:
        raise ValueError("unsupported facet")
    conn.execute("BEGIN IMMEDIATE")
    try:
        queued = 0
        for facet, text in sorted(updates.items()):
            if not isinstance(text, str):
                raise TypeError("facet text must be text")
            digest = _content_hash(text)
            prior = conn.execute(
                "SELECT content_hash FROM candidate_facets WHERE candidate_id = ? AND facet = ?",
                (candidate_id, facet),
            ).fetchone()
            if prior is not None and prior[0] == digest:
                continue
            conn.execute(
                """
                INSERT INTO candidate_facets(candidate_id, facet, text, content_hash, source_artifact_id)
                VALUES(?, ?, ?, ?, NULL)
                ON CONFLICT(candidate_id, facet) DO UPDATE SET text = excluded.text,
                  content_hash = excluded.content_hash, source_artifact_id = NULL
                """,
                (candidate_id, facet, text, digest),
            )
            _queue(conn, candidate_id, "facet-v1:" + facet + ":" + digest)
            queued += 1
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {"queued_facets": queued}


def update_candidate_from_round_artifact(conn, candidate_id, artifact_text):
    """Persist generated Summary and falsification fields as candidate facets."""
    candidate = history_store.get_candidate(conn, candidate_id)
    if candidate is None:
        raise ValueError("candidate is missing")
    extracted = facets_from_round_artifact(
        candidate["story"], candidate["theme"], artifact_text
    )
    updates = {
        facet: extracted[facet]
        for facet in ("mechanism", "evaluation_expected_signal")
    }
    return update_candidate_facets(conn, candidate_id, updates)


def _candidate_content(conn, candidate_id):
    rows = conn.execute(
        "SELECT facet, text FROM candidate_facets WHERE candidate_id = ? AND text != '' ORDER BY facet",
        (candidate_id,),
    ).fetchall()
    return "\n".join(row["facet"] + ": " + row["text"] for row in rows)


def _write_candidate(conn, candidate_id):
    excluded = conn.execute(
        "SELECT 1 FROM search_exclusions WHERE candidate_id = ?", (candidate_id,)
    ).fetchone()
    if excluded is not None:
        conn.execute("DELETE FROM search_fts WHERE candidate_id = ?", (candidate_id,))
        conn.execute("DELETE FROM search_vectors WHERE candidate_id = ?", (candidate_id,))
        candidate = history_store.get_candidate(conn, candidate_id)
        conn.execute(
            """INSERT INTO search_index_entries(candidate_id, active, content_hash, indexed_generation)
               VALUES(?, 0, ?, 0) ON CONFLICT(candidate_id) DO UPDATE SET
                 active = 0, content_hash = excluded.content_hash""",
            (candidate_id, _content_hash(candidate_id)),
        )
        return 0
    _ensure_facets(conn, candidate_id)
    facets = conn.execute(
        "SELECT facet, text, content_hash FROM candidate_facets WHERE candidate_id = ? ORDER BY facet",
        (candidate_id,),
    ).fetchall()
    embedded = 0
    active_facets = {row["facet"] for row in facets if row["text"]}
    for stale in conn.execute(
        "SELECT facet FROM search_vectors WHERE candidate_id = ?", (candidate_id,)
    ).fetchall():
        if stale["facet"] not in active_facets:
            conn.execute(
                "DELETE FROM search_vectors WHERE candidate_id = ? AND facet = ?",
                (candidate_id, stale["facet"]),
            )
    for row in facets:
        if not row["text"]:
            continue
        prior = conn.execute(
            "SELECT content_hash FROM search_vectors WHERE candidate_id = ? AND facet = ?",
            (candidate_id, row["facet"]),
        ).fetchone()
        if prior is not None and prior[0] == row["content_hash"]:
            continue
        vector, norm = embed(row["text"])
        conn.execute(
            """
            INSERT INTO search_vectors(candidate_id, facet, content_hash, vector, model,
              revision, preprocessing_version, dimensions, metric, l2_norm)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'cosine', ?)
            ON CONFLICT(candidate_id, facet) DO UPDATE SET content_hash = excluded.content_hash,
              vector = excluded.vector, model = excluded.model, revision = excluded.revision,
              preprocessing_version = excluded.preprocessing_version, dimensions = excluded.dimensions,
              metric = excluded.metric, l2_norm = excluded.l2_norm
            """,
            (candidate_id, row["facet"], row["content_hash"], _blob(vector), VECTOR_MODEL,
             VECTOR_REVISION, PREPROCESSING_VERSION, VECTOR_DIMENSIONS, norm),
        )
        embedded += 1
    content = _candidate_content(conn, candidate_id)
    digest = _content_hash(content)
    entry = conn.execute(
        "SELECT content_hash FROM search_index_entries WHERE candidate_id = ?", (candidate_id,)
    ).fetchone()
    if entry is None or entry[0] != digest:
        conn.execute("DELETE FROM search_fts WHERE candidate_id = ?", (candidate_id,))
        for facet, text in conn.execute(
            "SELECT facet, text FROM candidate_facets WHERE candidate_id = ? AND text != '' ORDER BY facet",
            (candidate_id,),
        ):
            conn.execute(
                "INSERT INTO search_fts(candidate_id, facet, content) VALUES(?, ?, ?)",
                (candidate_id, facet, text),
            )
    conn.execute(
        """
        INSERT INTO search_index_entries(candidate_id, active, content_hash, indexed_generation)
        VALUES(?, 1, ?, 0)
        ON CONFLICT(candidate_id) DO UPDATE SET active = 1, content_hash = excluded.content_hash
        """,
        (candidate_id, digest),
    )
    return embedded


def _canonical_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _policy_sha256(policy):
    return hashlib.sha256(_canonical_bytes(policy)).hexdigest()


def _projection_manifest(conn, policy, source_watermark):
    entries = [dict(row) for row in conn.execute(
        "SELECT candidate_id, active, content_hash FROM search_index_entries ORDER BY candidate_id"
    )]
    vectors = []
    for row in conn.execute(
        """SELECT candidate_id, facet, content_hash, vector, model, revision,
                  preprocessing_version, dimensions, metric, l2_norm
           FROM search_vectors ORDER BY candidate_id, facet"""
    ):
        value = dict(row)
        value["vector_sha256"] = hashlib.sha256(bytes(value.pop("vector"))).hexdigest()
        vectors.append(value)
    fts = [
        {"candidate_id": row[0], "facet": row[1], "content_sha256": _content_hash(row[2])}
        for row in conn.execute(
            "SELECT candidate_id, facet, content FROM search_fts ORDER BY candidate_id, facet"
        )
    ]
    return {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "source_watermark": source_watermark,
        "policy_sha256": _policy_sha256(policy),
        "fts_tokenizer": FTS_TOKENIZER,
        "vector": {
            "model": VECTOR_MODEL,
            "revision": VECTOR_REVISION,
            "preprocessing_version": PREPROCESSING_VERSION,
            "dimensions": VECTOR_DIMENSIONS,
            "metric": "cosine",
        },
        "entries": entries,
        "vectors": vectors,
        "fts": fts,
    }


def _latest_generation(conn):
    return conn.execute(
        "SELECT * FROM search_index_generations ORDER BY generation DESC LIMIT 1"
    ).fetchone()


def validate_published_generation(conn, policy):
    _init(conn)
    generation = _latest_generation(conn)
    if generation is None:
        return {"valid": False, "code": "no_published_generation"}
    try:
        manifest = json.loads(generation["manifest_json"])
    except (TypeError, ValueError):
        return {"valid": False, "code": "invalid_manifest"}
    expected = _projection_manifest(conn, policy, generation["source_watermark"])
    expected_bytes = _canonical_bytes(expected)
    compatible = (
        generation["policy_sha256"] == _policy_sha256(policy)
        and generation["projection_schema_version"] == PROJECTION_SCHEMA_VERSION
        and generation["fts_tokenizer"] == FTS_TOKENIZER
        and generation["vector_model"] == VECTOR_MODEL
        and generation["vector_revision"] == VECTOR_REVISION
        and generation["preprocessing_version"] == PREPROCESSING_VERSION
        and generation["dimensions"] == VECTOR_DIMENSIONS
        and generation["metric"] == "cosine"
    )
    valid = compatible and generation["manifest_sha256"] == hashlib.sha256(expected_bytes).hexdigest() and manifest == expected
    return {
        "valid": valid,
        "code": "ok" if valid else "manifest_mismatch",
        "generation": generation["generation"],
        "manifest": expected if valid else manifest,
    }


def _requeue_all(conn, content_version):
    for candidate_id, in conn.execute("SELECT candidate_id FROM candidates ORDER BY source_sequence"):
        _queue(conn, candidate_id, content_version)


def rebuild(conn, policy):
    _init(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        pending = conn.execute(
            "SELECT record_id, projection_kind, content_version FROM search_projection_outbox WHERE state = 'pending' ORDER BY source_sequence, content_version"
        ).fetchall()
        embedded = 0
        changed = set()
        for item in pending:
            embedded += _write_candidate(conn, item["record_id"])
            changed.add(item["record_id"])
            conn.execute(
                "UPDATE search_projection_outbox SET state = 'done', claim_token = NULL, lease_until = NULL WHERE record_id = ? AND projection_kind = ? AND content_version = ?",
                (item["record_id"], item["projection_kind"], item["content_version"]),
            )
        generation = int(conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'history_index_generation'"
        ).fetchone()[0])
        if changed:
            generation += 1
            watermark = conn.execute("SELECT COALESCE(MAX(source_sequence), 0) FROM candidates").fetchone()[0]
            manifest = _projection_manifest(conn, policy, watermark)
            manifest_bytes = _canonical_bytes(manifest)
            conn.execute(
                """INSERT INTO search_index_generations(
                   generation, source_watermark, manifest_sha256, manifest_json, policy_sha256,
                   projection_schema_version, fts_tokenizer, vector_model, vector_revision,
                   preprocessing_version, dimensions, metric, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                (generation, watermark, hashlib.sha256(manifest_bytes).hexdigest(),
                 manifest_bytes.decode("utf-8").rstrip("\n"), _policy_sha256(policy),
                 PROJECTION_SCHEMA_VERSION, FTS_TOKENIZER, VECTOR_MODEL, VECTOR_REVISION,
                 PREPROCESSING_VERSION, VECTOR_DIMENSIONS, "cosine"),
            )
            conn.execute(
                "UPDATE search_index_entries SET indexed_generation = ? WHERE candidate_id IN (%s)" % ",".join("?" * len(changed)),
                (generation, *sorted(changed)),
            )
            conn.execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'history_index_generation'", (str(generation),)
            )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {"embedded_facets": embedded, "index_generation": generation, "processed_records": len(pending)}


def recover(conn, policy):
    _init(conn)
    validation = validate_published_generation(conn, policy)
    pending = conn.execute(
        "SELECT count(*) FROM search_projection_outbox WHERE state = 'pending'"
    ).fetchone()[0]
    if not validation["valid"]:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _requeue_all(conn, "recovery-v2")
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    result = rebuild(conn, policy)
    if not validate_published_generation(conn, policy)["valid"]:
        raise ProjectionError("published projection recovery failed closed")
    result["recovered"] = not validation["valid"]
    result["pending_before_recovery"] = pending
    return result


def drop_rebuildable_projections(conn):
    _init(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM search_vectors")
        conn.execute("DELETE FROM search_index_entries")
        conn.execute("DELETE FROM search_fts")
        conn.execute("DELETE FROM search_index_generations")
        conn.execute("UPDATE schema_meta SET value = '0' WHERE key = 'history_index_generation'")
        _requeue_all(conn, "rebuild-v2")
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def remove_candidate_from_search(conn, candidate_id):
    _init(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if history_store.get_candidate(conn, candidate_id) is None:
            raise ValueError("candidate is missing")
        conn.execute(
            "INSERT OR REPLACE INTO search_exclusions(candidate_id, exclusion_reason, excluded_at) VALUES(?, 'operator', datetime('now'))",
            (candidate_id,),
        )
        _queue(conn, candidate_id, "exclusion-v1")
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {"candidate_id": candidate_id, "removed": True}


def searchable_candidate_ids(conn):
    _init(conn)
    return [row[0] for row in conn.execute(
        "SELECT candidate_id FROM search_index_entries WHERE active = 1 ORDER BY candidate_id"
    )]


def exact_lookup(conn, query, depth):
    """Return canonical exact-story matches from the active projection."""
    _init(conn)
    canonical = history_store.canonical_story_v1(query)
    return [row[0] for row in conn.execute(
        """SELECT c.candidate_id FROM story_aliases a
           JOIN candidates c ON c.lineage_id = a.lineage_id
           JOIN search_index_entries e ON e.candidate_id = c.candidate_id
           WHERE a.canonical_version = ? AND a.canonical_story = ? AND e.active = 1
           ORDER BY c.source_sequence DESC LIMIT ?""",
        (history_store.CANONICAL_VERSION, canonical, int(depth)),
    )]


def current_index_generation(conn):
    _init(conn)
    return int(conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'history_index_generation'"
    ).fetchone()[0])


def _cosine(left, right):
    return sum(a * b for a, b in zip(left, right))


def search(conn, query, policy):
    _init(conn)
    query = history_store.canonical_story_v1(query)
    depth = int(policy["per_channel_depth"])
    channels = {}
    exact = exact_lookup(conn, query, depth)
    channels["exact"] = exact
    terms = _tokens(query)
    fts_by_facet = {}
    if terms:
        expression = " OR ".join('"' + term.replace('"', '') + '"' for term in terms)
        for facet in FACETS:
            fts_by_facet[facet] = [row["candidate_id"] for row in conn.execute(
                """SELECT f.candidate_id, bm25(search_fts) AS rank FROM search_fts f
                   JOIN search_index_entries e ON e.candidate_id = f.candidate_id
                   WHERE e.active = 1 AND f.facet = ? AND search_fts MATCH ?
                   ORDER BY rank, f.candidate_id LIMIT ?""",
                (facet, expression, depth),
            )]
        fts = []
        for facet in sorted(fts_by_facet):
            for candidate_id in fts_by_facet[facet]:
                if candidate_id not in fts:
                    fts.append(candidate_id)
    else:
        fts = []
    channels["fts"] = fts
    channels["fts_by_facet"] = fts_by_facet
    vector, norm = embed(query)
    dense_by_facet = {}
    if norm:
        for facet in FACETS:
            values = []
            for row in conn.execute(
                """SELECT v.candidate_id, v.vector FROM search_vectors v
                   JOIN search_index_entries e ON e.candidate_id = v.candidate_id
                   WHERE e.active = 1 AND v.facet = ?""",
                (facet,),
            ):
                values.append((row["candidate_id"], _cosine(vector, _unblob(row["vector"]))))
            dense_by_facet[facet] = [candidate_id for candidate_id, _ in sorted(
                values, key=lambda item: (-item[1], item[0])
            )[:depth]]
    else:
        dense_by_facet = {facet: [] for facet in FACETS}
    dense = []
    for facet in FACETS:
        for candidate_id in dense_by_facet[facet]:
            if candidate_id not in dense:
                dense.append(candidate_id)
    channels["dense"] = dense
    channels["dense_by_facet"] = dense_by_facet
    channels["lineage"] = []
    scores = {}
    for channel, values in channels.items():
        if channel in ("fts_by_facet", "dense_by_facet"):
            continue
        for rank, candidate_id in enumerate(values, 1):
            scores[candidate_id] = scores.get(candidate_id, 0.0) + 1.0 / (int(policy["rrf_k"]) + rank)
    ranked = [candidate_id for candidate_id, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:int(policy["max_matches"])]]
    return {"candidate_ids": ranked, "channels": channels}


def failure_code(verdict, category, reason):
    text = (str(category) + " " + str(reason)).lower()
    rules = (
        ("direct-hit", ("direct hit", "occupied", "headline")),
        ("strong-baseline", ("strong baseline", "baseline")),
        ("statistical-power", ("statistical power", "sample size", "n<", "n≤")),
        ("estimand", ("estimand",)),
        ("attribution-control", ("attribution", "control")),
        ("weak-prior-work", ("weak prior", "prior work", "papers read")),
        ("novelty-cap", ("novelty", "ceiling", "capped")),
        ("feasibility", ("feasibility", "compute", "infeasible")),
        ("evidence-incomplete", ("evidence", "incomplete")),
    )
    for code, markers in rules:
        if any(marker in text for marker in markers):
            return code
    return "other"


def generation_brief_bytes(brief):
    return _canonical_bytes(brief)


def build_generation_brief(conn, policy, research_context=None):
    _init(conn)
    pending = conn.execute(
        "SELECT count(*) FROM search_projection_outbox WHERE state != 'done'"
    ).fetchone()[0]
    validation = validate_published_generation(conn, policy)
    current_watermark = conn.execute(
        "SELECT COALESCE(MAX(source_sequence), 0) FROM candidates"
    ).fetchone()[0]
    if pending or not validation["valid"]:
        raise ProjectionError("generation brief requires one validated current projection")
    generation = _latest_generation(conn)
    if generation["source_watermark"] != current_watermark:
        raise ProjectionError("generation brief cannot combine index and canonical snapshots")
    theme_counts = dict(conn.execute(
        "SELECT theme, count(*) FROM candidates GROUP BY theme ORDER BY theme"
    ).fetchall())
    failure_counts = {}
    for row in conn.execute("SELECT verdict, category, reason FROM candidates"):
        code = failure_code(row["verdict"], row["category"], row["reason"])
        failure_counts[code] = failure_counts.get(code, 0) + 1
    parent = history_store.select_generation_parent(conn)
    parent_value = None
    if parent is not None:
        parent_value = {
            "candidate_id": parent["candidate_id"],
            "category": parent["category"],
            "overlap": parent["overlap"],
            "reason": parent["reason"],
            "story": parent["story"],
            "verdict": parent["verdict"],
        }
    brief = {
        "schema_version": 1,
        "retrieval_policy_version": policy["retrieval_policy_version"],
        "source_watermark": generation["source_watermark"],
        "index_generation": generation["generation"],
        "theme_counts": theme_counts,
        "failure_code_counts": dict(sorted(failure_counts.items())),
        "parent": parent_value,
        "research_context": research_context,
    }
    brief["estimated_tokens"] = 0
    while True:
        final_size = len(generation_brief_bytes(brief))
        if brief["estimated_tokens"] == final_size:
            break
        brief["estimated_tokens"] = final_size
    if len(generation_brief_bytes(brief)) > int(policy["max_retrieval_tokens"]):
        raise ValueError("generation brief exceeds retrieval token budget")
    return brief
