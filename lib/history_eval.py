#!/usr/bin/env python3
"""Offline evaluation contract for bounded history retrieval."""

import argparse
import datetime
import hashlib
import hmac
import json
import math
import os
import pathlib
import random
import stat
import statistics
import struct
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "history/retrieval-policy-v1.json"
SYNTHETIC_SCOPE = "synthetic_contract_only"
PRODUCTION_SCOPE = "production"
LEGACY_OUTPUT_SCHEMA_VERSION = "history-eval-output-v1"
OUTPUT_SCHEMA_VERSION = "history-eval-output-v2"
LEGACY_CAPABILITY_SCHEMA_VERSION = 1
CAPABILITY_SCHEMA_VERSION = 2
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_JSONL_BYTES = 16 * 1024 * 1024
MAX_POLICY_BYTES = 256 * 1024
MAX_SCHEMA_BYTES = 512 * 1024
BOOTSTRAP_SEED = 20260723
BOOTSTRAP_SAMPLES = 2000
ARMS = (
    "retrieval-only",
    "comparator-only",
    "end-to-end",
    "closed-book",
)
RELATION_GAINS = {
    "duplicate": {
        "blocking": 2,
        "substantive": 1,
        "unrelated": 0,
    },
    "lineage": {
        "direct-parent": 3,
        "ancestor-or-descendant": 2,
        "sibling": 1,
        "unrelated": 0,
    },
    "failure": {
        "same-mechanism": 2,
        "related-defect": 1,
        "unrelated": 0,
    },
}
POSITIVE_RELATIONS = tuple(
    relation
    for relation_set in ("duplicate", "lineage", "failure")
    for relation, gain in RELATION_GAINS[relation_set].items()
    if gain > 0
)
RELATION_SET_BY_LABEL = {
    relation: relation_set
    for relation_set, gains in RELATION_GAINS.items()
    for relation, gain in gains.items()
    if gain > 0
}
BENCHMARK_FILES = (
    "queries.jsonl",
    "qrels.jsonl",
    "adjudications.jsonl",
    "folds.json",
    "corpus.jsonl",
    "oracle-packs.jsonl",
    "policy-commitment.json",
    "pre-heldout-receipt.json",
    "test-witness-key.json",
    "outputs/retrieval-only.jsonl",
    "outputs/comparator-only.jsonl",
    "outputs/end-to-end.jsonl",
    "outputs/closed-book.jsonl",
    "expected-metrics.json",
)
COMMITMENT_INPUTS = (
    "corpus.jsonl",
    "queries.jsonl",
    "folds.json",
)
SNAPSHOT_INPUTS = (
    "corpus.jsonl",
    "queries.jsonl",
    "folds.json",
    "oracle-packs.jsonl",
)
OUTPUT_FIELDS = {
    "schema_version",
    "arm",
    "query_id",
    "corpus_watermark",
    "ranked_record_ids",
    "ranked_scores",
    "retrieval_pack_id",
    "relation",
    "abstained",
    "evidence_ids",
    "status",
    "latency_ms",
    "input_tokens",
    "comparator_pairs",
    "pair_relations",
    "policy_commitment_sha256",
    "preheldout_receipt_sha256",
    "heldout_run_nonce",
    "heldout_started_at",
}
OUTPUT_V2_FIELDS = OUTPUT_FIELDS | {"channel_depths"}
CHANNEL_DEPTH_FIELDS = {"exact", "fts", "dense", "lineage"}
COMMITMENT_FIELDS = {
    "schema_version",
    "scope",
    "policy_version",
    "policy_sha256",
    "split_sha256",
    "calibration_query_ids_sha256",
    "heldout_query_ids_sha256",
    "benchmark_input_sha256s",
    "selected_thresholds",
    "error_budgets",
    "selected_depths",
    "latency_target_ms_p95",
    "token_budget",
    "sealed_at",
}
RECEIPT_FIELDS = {
    "schema_version",
    "scope",
    "trust_root_id",
    "policy_commitment_sha256",
    "split_sha256",
    "trusted_runner_release_sha256",
    "run_nonce",
    "witness_time",
    "signature",
}
LEGACY_CAPABILITY_FIELDS = {
    "schema_version",
    "scope",
    "trust_root_id",
    "policy_commitment_sha256",
    "preheldout_receipt_sha256",
    "policy_version",
    "policy_sha256",
    "benchmark_snapshot_sha256",
    "qrels_sha256",
    "adjudications_sha256",
    "relation_heldout_counts",
    "unresolved_adjudications",
    "heldout_output_sha256",
    "heldout_run_nonce",
    "heldout_started_at",
    "evaluation_evidence",
    "canonical_seal_sha256",
    "signature",
}
CAPABILITY_FIELDS = LEGACY_CAPABILITY_FIELDS | {
    "criteria_sha256",
    "heldout_completed_at",
}


class BenchmarkError(ValueError):
    """Raised when a benchmark artifact violates the closed contract."""


def canonical_bytes(value):
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def sha256(raw):
    if not isinstance(raw, bytes):
        raise TypeError("sha256 input must be bytes")
    return hashlib.sha256(raw).hexdigest()


def _valid_sha(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _require_closed(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise BenchmarkError(f"{label} schema is not closed")


def _require_sha(value, label):
    if not _valid_sha(value):
        raise BenchmarkError(f"{label} is not a SHA-256 digest")


def _parse_utc(value, label):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise BenchmarkError(f"{label} must be a UTC timestamp")
    try:
        parsed = datetime.datetime.fromisoformat(
            value[:-1] + "+00:00"
        )
    except ValueError as exc:
        raise BenchmarkError(f"{label} is invalid") from exc
    if parsed.utcoffset() != datetime.timedelta(0):
        raise BenchmarkError(f"{label} must be UTC")
    return parsed


def _read_bounded_bytes(
    path, label, maximum, *, boundary=None
):
    candidate = pathlib.Path(path)
    components = [candidate]
    if boundary is not None:
        boundary_path = pathlib.Path(boundary)
        try:
            candidate.relative_to(boundary_path)
        except ValueError as exc:
            raise BenchmarkError(
                f"{label} escapes its input root"
            ) from exc
        current = candidate.parent
        while current != boundary_path:
            components.append(current)
            if current == current.parent:
                raise BenchmarkError(
                    f"{label} escapes its input root"
                )
            current = current.parent
    for current in components:
        if current.is_symlink():
            raise BenchmarkError(f"{label} cannot use a symlink")
    try:
        path_before = candidate.lstat()
    except OSError as exc:
        raise BenchmarkError(f"{label} is unavailable") from exc
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(candidate), flags)
    except OSError as exc:
        raise BenchmarkError(f"{label} is unavailable") from exc
    try:
        metadata_before = os.fstat(descriptor)
        if not stat.S_ISREG(metadata_before.st_mode):
            raise BenchmarkError(f"{label} is not a regular file")
        if metadata_before.st_size > maximum:
            raise BenchmarkError(f"{label} exceeds its byte bound")
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > maximum:
            raise BenchmarkError(f"{label} exceeds its byte bound")
        if os.read(descriptor, 1):
            raise BenchmarkError(f"{label} exceeds its byte bound")
        metadata_after = os.fstat(descriptor)
        try:
            path_after = candidate.lstat()
        except OSError as exc:
            raise BenchmarkError(
                f"{label} changed while it was read"
            ) from exc
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            any(
                getattr(metadata_before, field)
                != getattr(metadata_after, field)
                for field in stable_fields
            )
            or any(
                getattr(path_before, field)
                != getattr(path_after, field)
                for field in stable_fields
            )
            or (
                path_after.st_dev,
                path_after.st_ino,
            )
            != (
                metadata_after.st_dev,
                metadata_after.st_ino,
            )
            or len(raw) != metadata_after.st_size
        ):
            raise BenchmarkError(
                f"{label} changed while it was read"
            )
        return raw
    finally:
        os.close(descriptor)


def _read_canonical_json(
    path,
    label,
    *,
    maximum=MAX_JSON_BYTES,
    boundary=None,
):
    raw = _read_bounded_bytes(
        path, label, maximum, boundary=boundary
    )
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BenchmarkError(f"{label} is invalid JSON") from exc
    if raw != canonical_bytes(value):
        raise BenchmarkError(f"{label} is not canonical JSON")
    return value, raw


def _read_canonical_jsonl(
    path,
    label,
    *,
    maximum=MAX_JSONL_BYTES,
    boundary=None,
):
    raw = _read_bounded_bytes(
        path, label, maximum, boundary=boundary
    )
    if not raw or not raw.endswith(b"\n") or b"\n\n" in raw:
        raise BenchmarkError(f"{label} is not canonical JSONL")
    rows = []
    for number, line in enumerate(raw.splitlines(), 1):
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BenchmarkError(
                f"{label}:{number} is invalid JSON"
            ) from exc
        if canonical_bytes(row).rstrip(b"\n") != line:
            raise BenchmarkError(
                f"{label}:{number} is not canonical JSON"
            )
        rows.append(row)
    return rows, raw


def _load_policy_snapshot(path=DEFAULT_POLICY):
    value, raw = _read_canonical_json(
        path,
        "retrieval policy",
        maximum=MAX_POLICY_BYTES,
    )
    if (
        not isinstance(value, dict)
        or value.get("retrieval_policy_version")
        != "retrieval-policy-v1"
    ):
        raise BenchmarkError("retrieval policy version is invalid")
    return value, raw


def load_policy(path=DEFAULT_POLICY):
    value, _ = _load_policy_snapshot(path)
    return value


def _load_inputs(benchmark):
    root = pathlib.Path(benchmark)
    if root.is_symlink() or not root.is_dir():
        raise BenchmarkError("benchmark root is unavailable")
    values = {}
    raw = {}
    for name in (
        "queries.jsonl",
        "qrels.jsonl",
        "adjudications.jsonl",
        "corpus.jsonl",
        "oracle-packs.jsonl",
    ):
        values[name], raw[name] = _read_canonical_jsonl(
            root / name, name, boundary=root
        )
    for arm in ARMS:
        name = f"outputs/{arm}.jsonl"
        values[name], raw[name] = _read_canonical_jsonl(
            root / name, name, boundary=root
        )
    for name in (
        "folds.json",
        "policy-commitment.json",
        "pre-heldout-receipt.json",
        "expected-metrics.json",
    ):
        values[name], raw[name] = _read_canonical_json(
            root / name, name, boundary=root
        )
    scope = values["policy-commitment.json"].get("scope")
    test_root_path = root / "test-witness-key.json"
    if scope == SYNTHETIC_SCOPE:
        (
            values["test-witness-key.json"],
            raw["test-witness-key.json"],
        ) = _read_canonical_json(
            test_root_path,
            "test-witness-key.json",
            boundary=root,
        )
    elif scope == PRODUCTION_SCOPE and (
        test_root_path.exists() or test_root_path.is_symlink()
    ):
        raise BenchmarkError(
            "production benchmark contains a synthetic test root"
        )
    return root, values, raw


def _index_unique(rows, key, label):
    result = {}
    for row in rows:
        value = row.get(key) if isinstance(row, dict) else None
        if not isinstance(value, str) or not value:
            raise BenchmarkError(f"{label} has an invalid {key}")
        if value in result:
            raise BenchmarkError(f"{label} has duplicate {key} {value}")
        result[value] = row
    return result


def _validate_corpus(rows):
    fields = {
        "schema_version",
        "record_id",
        "lineage_id",
        "committed_at",
        "text",
        "verdict",
        "reason",
        "citations",
    }
    corpus = _index_unique(rows, "record_id", "corpus")
    previous = None
    for row in rows:
        _require_closed(row, fields, "corpus record")
        if (
            row["schema_version"] != 1
            or not isinstance(row["lineage_id"], str)
            or not row["lineage_id"]
            or not isinstance(row["text"], str)
            or not row["text"].strip()
            or not isinstance(row["verdict"], str)
            or not isinstance(row["reason"], str)
            or not isinstance(row["citations"], list)
            or any(
                not isinstance(value, str) or not value
                for value in row["citations"]
            )
        ):
            raise BenchmarkError("corpus record is invalid")
        committed = _parse_utc(
            row["committed_at"],
            f"corpus record {row['record_id']} commit time",
        )
        order = (committed, row["record_id"])
        if previous is not None and order <= previous:
            raise BenchmarkError(
                "corpus records require stable chronological ordering"
            )
        previous = order
    return corpus


def _validate_queries(rows, corpus):
    fields = {
        "schema_version",
        "query_id",
        "relation_set",
        "lineage_id",
        "fold",
        "as_of",
        "corpus_watermark",
        "text",
        "expected_abstain",
        "theme",
        "lexical_overlap_bucket",
        "history_age_bucket",
    }
    queries = _index_unique(rows, "query_id", "queries")
    if list(queries) != sorted(queries):
        raise BenchmarkError("queries require stable query_id ordering")
    corpus_times = {
        record_id: _parse_utc(
            record["committed_at"],
            f"corpus record {record_id} commit time",
        )
        for record_id, record in corpus.items()
    }
    protected_metadata = [
        value
        for record in corpus.values()
        for value in (
            [record["verdict"], record["reason"]]
            + list(record["citations"])
        )
    ]
    for row in rows:
        _require_closed(row, fields, "query")
        if (
            row["schema_version"] != 1
            or row["relation_set"] not in RELATION_GAINS
            or row["fold"] not in {"train", "calibration", "test"}
            or not isinstance(row["lineage_id"], str)
            or not row["lineage_id"]
            or not isinstance(row["text"], str)
            or not row["text"].strip()
            or type(row["expected_abstain"]) is not bool
            or any(
                not isinstance(row[field], str) or not row[field]
                for field in (
                    "theme",
                    "lexical_overlap_bucket",
                    "history_age_bucket",
                )
            )
        ):
            raise BenchmarkError("query is invalid")
        as_of = _parse_utc(
            row["as_of"], f"query {row['query_id']} as-of time"
        )
        query_watermark = _parse_utc(
            row["corpus_watermark"],
            f"query {row['query_id']} corpus watermark",
        )
        visible = [
            record
            for record_id, record in corpus.items()
            if corpus_times[record_id] < as_of
        ]
        if not visible:
            raise BenchmarkError("query has an empty as-of corpus")
        watermark = max(
            corpus_times[record["record_id"]] for record in visible
        )
        if query_watermark != watermark:
            raise BenchmarkError(
                f"query {row['query_id']} corpus watermark is invalid"
            )
        folded = row["text"].casefold()
        future_revision_text = [
            record["text"]
            for record_id, record in corpus.items()
            if corpus_times[record_id] >= as_of
        ]
        for leaked in protected_metadata + future_revision_text:
            if (
                isinstance(leaked, str)
                and len(leaked.strip()) >= 4
                and leaked.casefold() in folded
            ):
                raise BenchmarkError(
                    f"query text leaks verdict, reason, citation, "
                    f"or future revision for {row['query_id']}"
                )
    return queries


def _validate_folds(value, queries, corpus):
    _require_closed(
        value,
        {"schema_version", "split_version", "folds"},
        "fold split",
    )
    if (
        value["schema_version"] != 1
        or value["split_version"] != "lineage-temporal-v1"
        or not isinstance(value["folds"], dict)
        or set(value["folds"]) != {"train", "calibration", "test"}
    ):
        raise BenchmarkError("fold split is invalid")
    lineage_fold = {}
    query_fold = {}
    for fold in ("train", "calibration", "test"):
        entry = value["folds"][fold]
        _require_closed(
            entry, {"lineage_ids", "query_ids"}, f"{fold} fold"
        )
        for field in ("lineage_ids", "query_ids"):
            items = entry[field]
            if (
                not isinstance(items, list)
                or any(not isinstance(item, str) for item in items)
                or items != sorted(set(items))
            ):
                raise BenchmarkError(
                    f"{fold} {field} require stable unique ordering"
                )
        for lineage_id in entry["lineage_ids"]:
            if lineage_id in lineage_fold:
                raise BenchmarkError(
                    "lineage appears in multiple folds: "
                    + lineage_id
                )
            lineage_fold[lineage_id] = fold
        for query_id in entry["query_ids"]:
            if query_id in query_fold:
                raise BenchmarkError(
                    "query appears in multiple folds: " + query_id
                )
            query_fold[query_id] = fold
    if set(query_fold) != set(queries):
        raise BenchmarkError("fold split query coverage is invalid")
    referenced_lineages = {
        query["lineage_id"] for query in queries.values()
    } | {record["lineage_id"] for record in corpus.values()}
    if not referenced_lineages.issubset(lineage_fold):
        raise BenchmarkError("fold split omits a benchmark lineage")
    for query_id, query in queries.items():
        if (
            query_fold[query_id] != query["fold"]
            or lineage_fold[query["lineage_id"]] != query["fold"]
        ):
            raise BenchmarkError(
                f"query {query_id} crosses its lineage fold"
            )
    return lineage_fold, query_fold


def _visible(record, query):
    return _parse_utc(
        record["committed_at"], "corpus commit time"
    ) < _parse_utc(query["as_of"], "query as-of time")


def _validate_qrels(rows, queries, corpus, lineage_fold):
    fields = {
        "schema_version",
        "query_id",
        "record_id",
        "relation",
        "gain",
        "hard_negative",
    }
    qrels = {}
    for row in rows:
        _require_closed(row, fields, "qrel")
        query = queries.get(row.get("query_id"))
        record = corpus.get(row.get("record_id"))
        if query is None or record is None:
            raise BenchmarkError("qrel references an unknown object")
        key = (row["query_id"], row["record_id"])
        if key in qrels:
            raise BenchmarkError("qrel pair is duplicated")
        expected_gains = RELATION_GAINS[query["relation_set"]]
        if (
            row["schema_version"] != 1
            or row["relation"] not in expected_gains
            or type(row["gain"]) is not int
            or row["gain"] != expected_gains[row["relation"]]
            or type(row["hard_negative"]) is not bool
            or row["hard_negative"] != (
                row["relation"] == "unrelated"
            )
        ):
            raise BenchmarkError("qrel relation or gain is invalid")
        if not _visible(record, query):
            raise BenchmarkError("qrel exposes a future record")
        if (
            row["gain"] > 0
            and lineage_fold[record["lineage_id"]]
            != query["fold"]
        ):
            raise BenchmarkError(
                "adjudicated relationship crosses lineage folds"
            )
        qrels[key] = row
    for relation_set in RELATION_GAINS:
        candidates = [
            query_id
            for query_id, query in queries.items()
            if query["relation_set"] == relation_set
        ]
        if not candidates:
            raise BenchmarkError(
                f"missing queries for {relation_set}"
            )
        has_no_hit = any(
            not any(
                item["gain"] > 0 and key[0] == query_id
                for key, item in qrels.items()
            )
            for query_id in candidates
        )
        if not has_no_hit:
            raise BenchmarkError(
                f"missing no-hit query for {relation_set}"
            )
    return qrels


def _validate_adjudications(rows, queries, qrels):
    fields = {
        "schema_version",
        "query_id",
        "record_id",
        "judgments",
        "final_relation",
        "resolution",
    }
    judgment_fields = {"adjudicator_id", "relation"}
    adjudications = {}
    agreements = 0
    for row in rows:
        _require_closed(row, fields, "adjudication")
        key = (row.get("query_id"), row.get("record_id"))
        qrel = qrels.get(key)
        query = queries.get(row.get("query_id"))
        if qrel is None or query is None or key in adjudications:
            raise BenchmarkError(
                "adjudication pair is unknown or duplicated"
            )
        judgments = row["judgments"]
        if (
            row["schema_version"] != 1
            or not isinstance(judgments, list)
            or len(judgments) not in {2, 3}
        ):
            raise BenchmarkError(
                "adjudication requires two independent judgments"
            )
        identifiers = []
        for judgment in judgments:
            _require_closed(
                judgment, judgment_fields, "individual judgment"
            )
            if (
                not isinstance(judgment["adjudicator_id"], str)
                or not judgment["adjudicator_id"]
                or judgment["relation"]
                not in RELATION_GAINS[query["relation_set"]]
            ):
                raise BenchmarkError(
                    "individual adjudication is invalid"
                )
            identifiers.append(judgment["adjudicator_id"])
        if len(set(identifiers)) != len(identifiers):
            raise BenchmarkError(
                "adjudication requires two independent judgments"
            )
        first, second = (
            judgments[0]["relation"],
            judgments[1]["relation"],
        )
        if first == second:
            if (
                len(judgments) != 2
                or row["resolution"] != "agreement"
                or row["final_relation"] != first
            ):
                raise BenchmarkError(
                    "agreed adjudication resolution is invalid"
                )
            agreements += 1
        elif (
            len(judgments) != 3
            or row["resolution"] != "third-adjudicator"
            or row["final_relation"] != judgments[2]["relation"]
        ):
            raise BenchmarkError(
                "adjudication disagreement requires a third adjudication"
            )
        if row["final_relation"] != qrel["relation"]:
            raise BenchmarkError(
                "qrel does not match final adjudication"
            )
        adjudications[key] = row
    if set(adjudications) != set(qrels):
        raise BenchmarkError(
            "qrels and adjudications require exact pair coverage"
        )
    return adjudications, agreements / len(adjudications)


def _gold_ids(qrels, query_id):
    return {
        record_id
        for (candidate_query_id, record_id), qrel in qrels.items()
        if candidate_query_id == query_id and qrel["gain"] > 0
    }


def _validate_packs(rows, queries, corpus, qrels):
    fields = {
        "schema_version",
        "pack_id",
        "pack_kind",
        "arm",
        "query_id",
        "corpus_watermark",
        "record_ids",
    }
    packs = _index_unique(rows, "pack_id", "pack catalog")
    oracle_counts = {query_id: 0 for query_id in queries}
    for row in rows:
        _require_closed(row, fields, "retrieval pack")
        query = queries.get(row["query_id"])
        if (
            row["schema_version"] != 1
            or query is None
            or row["pack_kind"] not in {"oracle", "retrieved"}
            or row["arm"]
            not in {"comparator-only", "retrieval-only", "end-to-end"}
            or row["corpus_watermark"]
            != query["corpus_watermark"]
            or not isinstance(row["record_ids"], list)
            or row["record_ids"] != list(dict.fromkeys(row["record_ids"]))
        ):
            raise BenchmarkError("retrieval pack is invalid")
        if (
            row["pack_kind"] == "oracle"
            and row["arm"] != "comparator-only"
        ) or (
            row["pack_kind"] == "retrieved"
            and row["arm"] == "comparator-only"
        ):
            raise BenchmarkError("retrieval pack kind and arm disagree")
        for record_id in row["record_ids"]:
            record = corpus.get(record_id)
            if record is None:
                raise BenchmarkError(
                    "retrieval pack references an unknown record"
                )
            if not _visible(record, query):
                raise BenchmarkError(
                    "retrieval pack exposes a future record"
                )
        if row["pack_kind"] == "oracle":
            oracle_counts[row["query_id"]] += 1
            omitted = _gold_ids(qrels, row["query_id"]) - set(
                row["record_ids"]
            )
            if omitted:
                raise BenchmarkError(
                    "oracle pack omits gold IDs for "
                    + row["query_id"]
                )
    if any(count != 1 for count in oracle_counts.values()):
        raise BenchmarkError(
            "each query requires exactly one oracle pack"
        )
    return packs


def _validate_output_row(
    row,
    arm,
    queries,
    corpus,
    qrels,
    packs,
    scope,
):
    schema_version = row.get("schema_version") if isinstance(row, dict) else None
    if schema_version == OUTPUT_SCHEMA_VERSION:
        _require_closed(row, OUTPUT_V2_FIELDS, f"{arm} output")
    elif (
        schema_version == LEGACY_OUTPUT_SCHEMA_VERSION
        and scope == SYNTHETIC_SCOPE
    ):
        _require_closed(row, OUTPUT_FIELDS, f"{arm} output")
    else:
        raise BenchmarkError(
            f"{arm} output schema version is invalid for {scope}"
        )
    query = queries.get(row.get("query_id"))
    if (
        row["arm"] != arm
        or query is None
        or row["corpus_watermark"] != query["corpus_watermark"]
        or type(row["abstained"]) is not bool
        or not isinstance(row["ranked_record_ids"], list)
        or not isinstance(row["ranked_scores"], list)
        or not isinstance(row["retrieval_pack_id"], str)
        or not isinstance(row["relation"], str)
        or not isinstance(row["evidence_ids"], list)
        or not isinstance(row["status"], str)
        or isinstance(row["latency_ms"], bool)
        or not isinstance(row["latency_ms"], (int, float))
        or not math.isfinite(float(row["latency_ms"]))
        or row["latency_ms"] < 0
        or type(row["input_tokens"]) is not int
        or row["input_tokens"] < 0
        or type(row["comparator_pairs"]) is not int
        or row["comparator_pairs"] < 0
        or not isinstance(row["pair_relations"], list)
        or type(row["heldout_run_nonce"]) is not int
        or row["heldout_run_nonce"] < 1
    ):
        raise BenchmarkError(f"{arm} output row is invalid")
    _require_sha(
        row["policy_commitment_sha256"],
        "held-out output policy commitment SHA",
    )
    _require_sha(
        row["preheldout_receipt_sha256"],
        "held-out output pre-held-out receipt SHA",
    )
    _parse_utc(
        row["heldout_started_at"], "held-out output start time"
    )
    if schema_version == OUTPUT_SCHEMA_VERSION:
        channel_depths = row["channel_depths"]
        if (
            not isinstance(channel_depths, dict)
            or set(channel_depths) != CHANNEL_DEPTH_FIELDS
            or any(
                type(depth) is not int or depth < 0
                for depth in channel_depths.values()
            )
            or (
                arm in {"closed-book", "comparator-only"}
                and any(channel_depths.values())
            )
        ):
            raise BenchmarkError(
                f"{arm} channel depth evidence is invalid"
            )
    ranked = row["ranked_record_ids"]
    scores = row["ranked_scores"]
    if (
        len(ranked) != len(scores)
        or ranked != list(dict.fromkeys(ranked))
        or any(
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
            for score in scores
        )
    ):
        raise BenchmarkError(
            f"{arm} ranked records or scores are invalid"
        )
    for index, (record_id, score) in enumerate(
        zip(ranked, scores)
    ):
        record = corpus.get(record_id)
        if record is None:
            raise BenchmarkError(
                f"{arm} output references an unknown record"
            )
        if not _visible(record, query):
            raise BenchmarkError(
                f"{arm} output exposes a future record"
            )
        if index:
            previous_score = float(scores[index - 1])
            if float(score) > previous_score:
                raise BenchmarkError(
                    f"{arm} output scores are not descending"
                )
            if (
                float(score) == previous_score
                and record_id < ranked[index - 1]
            ):
                raise BenchmarkError(
                    f"{arm} tied ranks lack stable record ordering"
                )
    pack = None
    if row["retrieval_pack_id"]:
        pack = packs.get(row["retrieval_pack_id"])
        if pack is None or pack["query_id"] != row["query_id"]:
            raise BenchmarkError(
                f"{arm} output references an invalid pack"
            )
        if pack["arm"] != arm:
            raise BenchmarkError(
                f"{arm} output references a pack from another arm"
            )
    if arm == "closed-book":
        if (
            ranked
            or scores
            or pack is not None
            or row["evidence_ids"]
            or row["pair_relations"]
            or row["comparator_pairs"] != 0
        ):
            raise BenchmarkError(
                "closed-book output contains historical evidence"
            )
    elif arm == "comparator-only":
        if (
            ranked
            or scores
            or pack is None
            or pack["pack_kind"] != "oracle"
        ):
            raise BenchmarkError(
                "comparator-only output requires one oracle pack"
            )
    elif (
        pack is None
        or pack["pack_kind"] != "retrieved"
        or pack["record_ids"] != ranked
    ):
        raise BenchmarkError(
            f"{arm} output does not bind its retrieved pack"
        )
    allowed_relation = set(
        RELATION_GAINS[query["relation_set"]]
    ) | {"", "no_match"}
    if row["relation"] not in allowed_relation:
        raise BenchmarkError(f"{arm} output relation is invalid")
    if arm == "retrieval-only":
        if (
            row["relation"] != ""
            or row["abstained"]
            or row["evidence_ids"]
            or row["pair_relations"]
            or row["comparator_pairs"] != 0
            or row["status"] != "complete"
        ):
            raise BenchmarkError(
                "retrieval-only output unused fields must be empty"
            )
    elif row["abstained"]:
        if row["relation"] or row["status"] != "abstained":
            raise BenchmarkError(
                f"{arm} abstention fields are inconsistent"
            )
    elif row["relation"] == "no_match":
        if row["status"] != "complete_no_match":
            raise BenchmarkError(
                "no_match relation requires complete_no_match status"
            )
    elif row["status"] == "complete_no_match":
        raise BenchmarkError(
            "complete_no_match status requires no_match relation"
        )
    elif (
        not row["relation"]
        or row["status"] not in {"complete", "uncertain"}
    ):
        raise BenchmarkError(f"{arm} output status is invalid")
    if (
        len(row["evidence_ids"])
        != len(set(row["evidence_ids"]))
    ):
        raise BenchmarkError(f"{arm} evidence IDs are duplicated")
    allowed_evidence = (
        set(pack["record_ids"]) if pack is not None else set()
    )
    for record_id in row["evidence_ids"]:
        record = corpus.get(record_id)
        if record is None or not _visible(record, query):
            raise BenchmarkError(
                f"{arm} output references invalid evidence"
            )
        if record_id not in allowed_evidence:
            raise BenchmarkError(
                f"{arm} evidence is outside its retrieval pack"
            )
    pair_predictions = {}
    for prediction in row["pair_relations"]:
        _require_closed(
            prediction,
            {"record_id", "relation"},
            f"{arm} pair relation",
        )
        record_id = prediction["record_id"]
        if (
            record_id in pair_predictions
            or record_id not in allowed_evidence
        ):
            raise BenchmarkError(
                f"{arm} pair relation references invalid evidence"
            )
        qrel = qrels.get((row["query_id"], record_id))
        if qrel is None:
            if prediction["relation"] != "unjudged":
                raise BenchmarkError(
                    "unjudged pair cannot be labeled negative "
                    "or positive"
                )
        elif prediction["relation"] not in RELATION_GAINS[
            query["relation_set"]
        ]:
            raise BenchmarkError(
                f"{arm} pair relation label is invalid"
            )
        pair_predictions[record_id] = prediction["relation"]
    if row["comparator_pairs"] != len(pair_predictions):
        raise BenchmarkError(
            f"{arm} comparator pair count is inconsistent"
        )
    return pair_predictions


def _validate_outputs(
    values, queries, corpus, qrels, packs, scope
):
    outputs = {}
    schema_versions = set()
    starts = set()
    nonces = set()
    commitment_hashes = set()
    receipt_hashes = set()
    heldout_query_ids = sorted(
        query_id
        for query_id, query in queries.items()
        if query["fold"] == "test"
    )
    if not heldout_query_ids:
        raise BenchmarkError("benchmark has no held-out queries")
    for arm in ARMS:
        rows = values[f"outputs/{arm}.jsonl"]
        if [row.get("query_id") for row in rows] != heldout_query_ids:
            raise BenchmarkError(
                f"{arm} held-out output query coverage or ordering "
                "is invalid"
            )
        indexed = {}
        for row in rows:
            pair_predictions = _validate_output_row(
                row, arm, queries, corpus, qrels, packs, scope
            )
            schema_versions.add(row["schema_version"])
            indexed[row["query_id"]] = {
                "row": row,
                "pair_predictions": pair_predictions,
            }
            starts.add(row["heldout_started_at"])
            nonces.add(row["heldout_run_nonce"])
            commitment_hashes.add(
                row["policy_commitment_sha256"]
            )
            receipt_hashes.add(
                row["preheldout_receipt_sha256"]
            )
        outputs[arm] = indexed
    if len(commitment_hashes) != 1:
        raise BenchmarkError(
            "held-out output policy commitment SHA changed"
        )
    if len(receipt_hashes) != 1:
        raise BenchmarkError(
            "held-out output pre-held-out receipt SHA changed"
        )
    if len(starts) != 1 or len(nonces) != 1:
        raise BenchmarkError(
            "held-out outputs do not share one sealed run"
        )
    if len(schema_versions) != 1:
        raise BenchmarkError(
            "held-out outputs mix incompatible schema versions"
        )
    return {
        "arms": outputs,
        "schema_version": next(iter(schema_versions)),
        "heldout_started_at": next(iter(starts)),
        "heldout_run_nonce": next(iter(nonces)),
        "policy_commitment_sha256": next(
            iter(commitment_hashes)
        ),
        "preheldout_receipt_sha256": next(
            iter(receipt_hashes)
        ),
    }


def _query_ids_sha(query_ids):
    return sha256(canonical_bytes(sorted(query_ids)))


def _validate_number(value, label, *, minimum=0.0, maximum=None):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < minimum
        or (maximum is not None and float(value) > maximum)
    ):
        raise BenchmarkError(f"{label} is invalid")


def _validate_commitment(
    commitment,
    policy,
    raw,
    folds,
    query_fold,
):
    _require_closed(
        commitment, COMMITMENT_FIELDS, "policy commitment"
    )
    if (
        commitment["schema_version"] != 1
        or commitment["scope"]
        not in {SYNTHETIC_SCOPE, PRODUCTION_SCOPE}
        or commitment["policy_version"]
        != policy.get("retrieval_policy_version")
        or commitment["policy_sha256"]
        != sha256(canonical_bytes(policy))
    ):
        raise BenchmarkError(
            "policy commitment does not bind the retrieval policy"
        )
    for field in (
        "policy_sha256",
        "split_sha256",
        "calibration_query_ids_sha256",
        "heldout_query_ids_sha256",
    ):
        _require_sha(
            commitment[field],
            "policy commitment " + field.replace("_", " "),
        )
    split_sha = sha256(raw["folds.json"])
    if commitment["split_sha256"] != split_sha:
        raise BenchmarkError("policy commitment split SHA is invalid")
    calibration_ids = [
        query_id
        for query_id, fold in query_fold.items()
        if fold == "calibration"
    ]
    heldout_ids = [
        query_id
        for query_id, fold in query_fold.items()
        if fold == "test"
    ]
    if commitment["calibration_query_ids_sha256"] != _query_ids_sha(
        calibration_ids
    ):
        raise BenchmarkError(
            "policy commitment calibration-query set is invalid"
        )
    if commitment["heldout_query_ids_sha256"] != _query_ids_sha(
        heldout_ids
    ):
        raise BenchmarkError(
            "policy commitment held-out-query set is invalid"
        )
    input_hashes = commitment["benchmark_input_sha256s"]
    if (
        not isinstance(input_hashes, dict)
        or set(input_hashes) != set(COMMITMENT_INPUTS)
    ):
        raise BenchmarkError(
            "policy commitment benchmark input set is invalid"
        )
    for name in COMMITMENT_INPUTS:
        if input_hashes[name] != sha256(raw[name]):
            raise BenchmarkError(
                "policy commitment benchmark input hash is invalid"
            )
    thresholds = commitment["selected_thresholds"]
    if (
        not isinstance(thresholds, dict)
        or set(thresholds) != set(RELATION_GAINS)
    ):
        raise BenchmarkError(
            "policy commitment selected thresholds are invalid"
        )
    for relation, value in thresholds.items():
        _validate_number(
            value,
            f"{relation} selected threshold",
            minimum=0.0,
            maximum=1.0,
        )
    budgets = commitment["error_budgets"]
    if (
        not isinstance(budgets, dict)
        or set(budgets)
        != {
            "max_false_duplicate_rate",
            "max_false_internal_no_match_rate",
        }
    ):
        raise BenchmarkError(
            "policy commitment error budgets are invalid"
        )
    for name, value in budgets.items():
        _validate_number(
            value, name, minimum=0.0, maximum=1.0
        )
    depths = commitment["selected_depths"]
    if (
        not isinstance(depths, dict)
        or set(depths)
        != {
            "per_channel_depth",
            "comparator_cutoff",
            "final_lineage_count",
        }
        or any(
            type(value) is not int or value < 1
            for value in depths.values()
        )
    ):
        raise BenchmarkError(
            "policy commitment selected depths are invalid"
        )
    for name, value in depths.items():
        if value != policy.get(name):
            raise BenchmarkError(
                f"policy commitment selected {name} changed"
            )
    _validate_number(
        commitment["latency_target_ms_p95"],
        "latency target",
        minimum=0.000001,
    )
    if (
        type(commitment["token_budget"]) is not int
        or commitment["token_budget"] < 1
        or commitment["token_budget"]
        != policy.get("max_retrieval_tokens")
    ):
        raise BenchmarkError(
            "policy commitment token budget is invalid"
        )
    _parse_utc(
        commitment["sealed_at"], "policy commitment seal time"
    )
    return {
        "split_sha256": split_sha,
        "calibration_query_ids": sorted(calibration_ids),
        "heldout_query_ids": sorted(heldout_ids),
    }


def _test_root_key(trust_root):
    _require_closed(
        trust_root,
        {
            "schema_version",
            "scope",
            "trust_root_id",
            "algorithm",
            "hmac_sha256_key",
        },
        "test trust root",
    )
    if (
        trust_root["schema_version"] != 1
        or trust_root["scope"] != SYNTHETIC_SCOPE
        or trust_root["algorithm"] != "test-hmac-sha256"
        or not isinstance(trust_root["trust_root_id"], str)
        or not trust_root["trust_root_id"]
        or not _valid_sha(trust_root["hmac_sha256_key"])
    ):
        raise BenchmarkError("test trust root is invalid")
    return bytes.fromhex(trust_root["hmac_sha256_key"])


def _test_signature(domain, value, trust_root):
    return hmac.new(
        _test_root_key(trust_root),
        domain + canonical_bytes(value),
        hashlib.sha256,
    ).hexdigest()


def _validate_receipt(
    receipt,
    commitment,
    trust_root,
    required_scope,
    heldout_started_at,
    witness_verifier=None,
):
    _require_closed(
        receipt, RECEIPT_FIELDS, "pre-held-out receipt"
    )
    commitment_sha = sha256(canonical_bytes(commitment))
    if (
        receipt["schema_version"] != 1
        or receipt["scope"] != required_scope
        or receipt["policy_commitment_sha256"] != commitment_sha
        or receipt["split_sha256"] != commitment["split_sha256"]
        or type(receipt["run_nonce"]) is not int
        or receipt["run_nonce"] < 1
        or not isinstance(receipt["trust_root_id"], str)
        or not receipt["trust_root_id"]
    ):
        raise BenchmarkError(
            "pre-held-out receipt binding is invalid"
        )
    for field in (
        "policy_commitment_sha256",
        "split_sha256",
        "trusted_runner_release_sha256",
        "signature",
    ):
        _require_sha(
            receipt[field],
            "pre-held-out receipt " + field.replace("_", " "),
        )
    sealed_at = _parse_utc(
        commitment["sealed_at"], "policy commitment seal time"
    )
    witness_time = _parse_utc(
        receipt["witness_time"], "pre-held-out witness time"
    )
    heldout_start = _parse_utc(
        heldout_started_at, "held-out start time"
    )
    if not sealed_at < witness_time < heldout_start:
        raise BenchmarkError(
            "policy thresholds must be sealed before the "
            "pre-held-out receipt and held-out run"
        )
    if required_scope == SYNTHETIC_SCOPE:
        if (
            not isinstance(trust_root, dict)
            or trust_root.get("scope") != SYNTHETIC_SCOPE
            or receipt["trust_root_id"]
            != trust_root.get("trust_root_id")
        ):
            raise BenchmarkError(
                "synthetic pre-held-out trust root mismatch"
            )
        unsigned = dict(receipt)
        signature = unsigned.pop("signature")
        expected = _test_signature(
            b"history-preheldout-receipt-v1\0",
            unsigned,
            trust_root,
        )
        if not hmac.compare_digest(signature, expected):
            raise BenchmarkError(
                "pre-held-out receipt signature is invalid"
            )
    else:
        if (
            not isinstance(trust_root, dict)
            or trust_root.get("scope") != PRODUCTION_SCOPE
            or receipt["trust_root_id"]
            != trust_root.get("trust_root_id")
            or "hmac_sha256_key" in trust_root
            or trust_root.get("algorithm") == "test-hmac-sha256"
        ):
            raise BenchmarkError(
                "synthetic test root cannot verify production "
                "calibration"
            )
        if not callable(witness_verifier):
            raise BenchmarkError(
                "production trusted-runner witness verifier "
                "is unavailable"
            )
        try:
            verified = witness_verifier(
                "preheldout_receipt", receipt, trust_root
            )
        except Exception as exc:
            raise BenchmarkError(
                "production pre-held-out witness verification failed"
            ) from exc
        if verified is not True:
            raise BenchmarkError(
                "production pre-held-out witness verification failed"
            )
    return {
        "commitment_sha256": commitment_sha,
        "receipt_sha256": sha256(canonical_bytes(receipt)),
        "witness_time": receipt["witness_time"],
    }


def _gold_relation(query_id, query, qrels):
    candidates = [
        item
        for (candidate_query_id, _), item in qrels.items()
        if candidate_query_id == query_id and item["gain"] > 0
    ]
    if not candidates:
        return "no_match"
    return max(
        candidates,
        key=lambda item: (
            item["gain"],
            item["relation"],
            item["record_id"],
        ),
    )["relation"]


def _rounded(value):
    if value is None:
        return None
    return round(float(value), 12)


def _safe_ratio(numerator, denominator):
    if not denominator:
        return None
    return _rounded(numerator / denominator)


def _rank_metrics(rows, query_ids, queries, qrels):
    positive_query_ids = [
        query_id
        for query_id in query_ids
        if _gold_ids(qrels, query_id)
    ]
    if not positive_query_ids:
        return {
            "evaluated_positive_queries": 0,
            "hit_at": {str(k): None for k in (1, 3, 5, 10)},
            "mrr_at_10": None,
            "ndcg_at_10": None,
            "recall_at": {
                str(k): None for k in (1, 3, 5, 10)
            },
            "judged_ranked_pairs": 0,
            "unjudged_ranked_pairs": 0,
            "incomplete_judgment_queries": 0,
        }
    hits = {k: [] for k in (1, 3, 5, 10)}
    recalls = {k: [] for k in (1, 3, 5, 10)}
    reciprocal_ranks = []
    ndcgs = []
    unjudged = 0
    judged = 0
    incomplete = 0
    for query_id in positive_query_ids:
        raw_ranked = rows[query_id]["row"]["ranked_record_ids"]
        ranked = [
            record_id
            for record_id in raw_ranked
            if (query_id, record_id) in qrels
        ]
        gold = _gold_ids(qrels, query_id)
        unjudged_for_query = len(raw_ranked) - len(ranked)
        unjudged += unjudged_for_query
        judged += len(ranked)
        if unjudged_for_query:
            incomplete += 1
        for cutoff in (1, 3, 5, 10):
            selected = set(ranked[:cutoff])
            found = len(gold & selected)
            hits[cutoff].append(1.0 if found else 0.0)
            recalls[cutoff].append(found / len(gold))
        first = next(
            (
                index
                for index, record_id in enumerate(
                    ranked[:10], 1
                )
                if record_id in gold
            ),
            None,
        )
        reciprocal_ranks.append(0.0 if first is None else 1 / first)
        gains = [
            qrels[(query_id, record_id)]["gain"]
            for record_id in ranked[:10]
        ]
        dcg = sum(
            (2 ** gain - 1) / math.log2(index + 1)
            for index, gain in enumerate(gains, 1)
        )
        ideal_gains = sorted(
            (
                item["gain"]
                for (candidate_query_id, _), item in qrels.items()
                if candidate_query_id == query_id and item["gain"] > 0
            ),
            reverse=True,
        )[:10]
        ideal = sum(
            (2 ** gain - 1) / math.log2(index + 1)
            for index, gain in enumerate(ideal_gains, 1)
        )
        ndcgs.append(0.0 if ideal == 0 else dcg / ideal)
    return {
        "evaluated_positive_queries": len(positive_query_ids),
        "hit_at": {
            str(cutoff): _rounded(statistics.mean(values))
            for cutoff, values in hits.items()
        },
        "mrr_at_10": _rounded(statistics.mean(reciprocal_ranks)),
        "ndcg_at_10": _rounded(statistics.mean(ndcgs)),
        "recall_at": {
            str(cutoff): _rounded(statistics.mean(values))
            for cutoff, values in recalls.items()
        },
        "judged_ranked_pairs": judged,
        "unjudged_ranked_pairs": unjudged,
        "incomplete_judgment_queries": incomplete,
    }


def _judged_ranked_ids(output, query_id, qrels, cutoff=None):
    ranked = [
        record_id
        for record_id in output[query_id]["row"][
            "ranked_record_ids"
        ]
        if (query_id, record_id) in qrels
    ]
    return ranked if cutoff is None else ranked[:cutoff]


def _retrieval_alert_metrics(
    output, query_ids, queries, qrels, *, classified
):
    duplicate_ids = [
        query_id
        for query_id in query_ids
        if queries[query_id]["relation_set"] == "duplicate"
    ]
    alerts = []
    actual = []
    for query_id in duplicate_ids:
        if classified:
            predicted = output[query_id]["row"]["relation"] in {
                "blocking",
                "substantive",
            }
        else:
            predicted = bool(
                _judged_ranked_ids(
                    output, query_id, qrels, cutoff=10
                )
            )
        alerts.append(predicted)
        actual.append(bool(_gold_ids(qrels, query_id)))
    true_alerts = sum(
        1
        for predicted, positive in zip(alerts, actual)
        if predicted and positive
    )
    alert_count = sum(alerts)
    no_hit_count = sum(not positive for positive in actual)
    no_hit_alerts = sum(
        1
        for predicted, positive in zip(alerts, actual)
        if predicted and not positive
    )
    return {
        "alert_precision": _safe_ratio(
            true_alerts, alert_count
        ),
        "no_hit_false_positive_rate": _safe_ratio(
            no_hit_alerts, no_hit_count
        ),
    }


def _lineage_required_metrics(output, query_ids, queries, qrels):
    lineage_ids = [
        query_id
        for query_id in query_ids
        if queries[query_id]["relation_set"] == "lineage"
    ]
    ranking = _rank_metrics(
        output, lineage_ids, queries, qrels
    )
    direct_parent_queries = [
        query_id
        for query_id in lineage_ids
        if any(
            qrel["relation"] == "direct-parent"
            for (candidate_query_id, _), qrel in qrels.items()
            if candidate_query_id == query_id
        )
    ]
    direct_parent_correct = 0
    for query_id in direct_parent_queries:
        ranked = _judged_ranked_ids(
            output, query_id, qrels, cutoff=1
        )
        if (
            ranked
            and qrels[(query_id, ranked[0])]["relation"]
            == "direct-parent"
        ):
            direct_parent_correct += 1
    ancestor_queries = [
        query_id
        for query_id in lineage_ids
        if any(
            qrel["relation"]
            in {"direct-parent", "ancestor-or-descendant"}
            for (candidate_query_id, _), qrel in qrels.items()
            if candidate_query_id == query_id
        )
    ]
    ancestor_recall = {}
    for cutoff in (5, 10):
        values = []
        for query_id in ancestor_queries:
            gold = {
                record_id
                for (candidate_query_id, record_id), qrel
                in qrels.items()
                if candidate_query_id == query_id
                and qrel["relation"]
                in {"direct-parent", "ancestor-or-descendant"}
            }
            selected = set(
                _judged_ranked_ids(
                    output, query_id, qrels, cutoff=cutoff
                )
            )
            values.append(len(gold & selected) / len(gold))
        ancestor_recall[str(cutoff)] = (
            _rounded(statistics.mean(values)) if values else None
        )
    return {
        "ndcg_at_10": ranking["ndcg_at_10"],
        "direct_parent_accuracy_at_1": _safe_ratio(
            direct_parent_correct, len(direct_parent_queries)
        ),
        "ancestor_recall_at": ancestor_recall,
    }


def _retrieval_failure_relations(
    output, query_ids, queries, qrels
):
    failure_ids = {
        query_id
        for query_id in query_ids
        if queries[query_id]["relation_set"] == "failure"
    }
    selected = {
        (query_id, record_id)
        for query_id in failure_ids
        for record_id in _judged_ranked_ids(
            output, query_id, qrels, cutoff=10
        )
    }
    precision = {}
    recall = {}
    for relation in ("same-mechanism", "related-defect"):
        gold = {
            key
            for key, qrel in qrels.items()
            if key[0] in failure_ids
            and qrel["relation"] == relation
        }
        true_positive = len(gold & selected)
        precision[relation] = _safe_ratio(
            true_positive, len(selected)
        )
        recall[relation] = _safe_ratio(
            true_positive, len(gold)
        )
    return precision, recall


def _required_metrics(
    arm, output, query_ids, queries, qrels, classification
):
    ranked = arm in {"retrieval-only", "end-to-end"}
    classified = arm != "retrieval-only"
    duplicate_ids = [
        query_id
        for query_id in query_ids
        if queries[query_id]["relation_set"] == "duplicate"
    ]
    failure_ids = [
        query_id
        for query_id in query_ids
        if queries[query_id]["relation_set"] == "failure"
    ]
    duplicate_ranking = (
        _rank_metrics(
            output, duplicate_ids, queries, qrels
        )
        if ranked
        else None
    )
    failure_ranking = (
        _rank_metrics(output, failure_ids, queries, qrels)
        if ranked
        else None
    )
    alerts = _retrieval_alert_metrics(
        output,
        query_ids,
        queries,
        qrels,
        classified=classified,
    )
    if classification is not None:
        failure_precision = {
            relation: classification["relation_precision"][
                relation
            ]
            for relation in (
                "same-mechanism",
                "related-defect",
            )
        }
        failure_recall = {
            relation: classification["relation_recall"][
                relation
            ]
            for relation in (
                "same-mechanism",
                "related-defect",
            )
        }
    elif ranked:
        failure_precision, failure_recall = (
            _retrieval_failure_relations(
                output, query_ids, queries, qrels
            )
        )
    else:
        failure_precision = {
            "same-mechanism": None,
            "related-defect": None,
        }
        failure_recall = dict(failure_precision)
    return {
        "duplicate": {
            "hit_at": duplicate_ranking["hit_at"]
            if duplicate_ranking is not None
            else {str(k): None for k in (1, 3, 5, 10)},
            "mrr_at_10": duplicate_ranking["mrr_at_10"]
            if duplicate_ranking is not None
            else None,
            **alerts,
        },
        "lineage": (
            _lineage_required_metrics(
                output, query_ids, queries, qrels
            )
            if ranked
            else {
                "ndcg_at_10": None,
                "direct_parent_accuracy_at_1": None,
                "ancestor_recall_at": {"5": None, "10": None},
            }
        ),
        "failure": {
            "recall_at": failure_ranking["recall_at"]
            if failure_ranking is not None
            else {str(k): None for k in (1, 3, 5, 10)},
            "relation_precision": failure_precision,
            "relation_recall": failure_recall,
        },
    }


def _query_relation_correct(query_id, output, queries, qrels):
    query = queries[query_id]
    row = output[query_id]["row"]
    if query["expected_abstain"]:
        return 1.0 if row["abstained"] else 0.0
    return (
        1.0
        if not row["abstained"]
        and row["relation"]
        == _gold_relation(query_id, query, qrels)
        else 0.0
    )


def _classification_metrics(output, query_ids, queries, qrels):
    relation_labels = list(POSITIVE_RELATIONS) + ["unrelated"]
    counts = {
        relation: {"tp": 0, "fp": 0, "fn": 0}
        for relation in relation_labels
    }
    false_duplicate = 0
    duplicate_negatives = 0
    for (query_id, record_id), qrel in qrels.items():
        if query_id not in query_ids:
            continue
        predicted = output[query_id]["pair_predictions"].get(
            record_id
        )
        for relation in relation_labels:
            if predicted == relation and qrel["relation"] == relation:
                counts[relation]["tp"] += 1
            elif predicted == relation:
                counts[relation]["fp"] += 1
            elif qrel["relation"] == relation:
                counts[relation]["fn"] += 1
        if (
            queries[query_id]["relation_set"] == "duplicate"
            and qrel["relation"] == "unrelated"
        ):
            duplicate_negatives += 1
            if predicted in {"blocking", "substantive"}:
                false_duplicate += 1
    precision = {
        relation: _safe_ratio(
            value["tp"], value["tp"] + value["fp"]
        )
        for relation, value in counts.items()
    }
    recall = {
        relation: _safe_ratio(
            value["tp"], value["tp"] + value["fn"]
        )
        for relation, value in counts.items()
    }
    positive_precision = [
        precision[relation]
        for relation in POSITIVE_RELATIONS
        if precision[relation] is not None
    ]
    positive_recall = [
        recall[relation]
        for relation in POSITIVE_RELATIONS
        if recall[relation] is not None
    ]
    positive_queries = [
        query_id
        for query_id in query_ids
        if _gold_ids(qrels, query_id)
    ]
    false_no_match = sum(
        1
        for query_id in positive_queries
        if (
            output[query_id]["row"]["relation"] == "no_match"
            or output[query_id]["row"]["status"]
            == "complete_no_match"
        )
    )
    no_hit_queries = [
        query_id
        for query_id in query_ids
        if not _gold_ids(qrels, query_id)
    ]
    no_hit_false_positives = sum(
        1
        for query_id in no_hit_queries
        if output[query_id]["row"]["relation"]
        not in {"", "no_match"}
    )
    abstention_correct = sum(
        1
        for query_id in query_ids
        if output[query_id]["row"]["abstained"]
        == queries[query_id]["expected_abstain"]
    )
    relation_correct = sum(
        _query_relation_correct(
            query_id, output, queries, qrels
        )
        for query_id in query_ids
    )
    return {
        "relation_precision": precision,
        "relation_recall": recall,
        "macro_positive_relation_precision": _rounded(
            statistics.mean(positive_precision)
        )
        if positive_precision
        else None,
        "macro_positive_relation_recall": _rounded(
            statistics.mean(positive_recall)
        )
        if positive_recall
        else None,
        "relation_accuracy": _safe_ratio(
            relation_correct, len(query_ids)
        ),
        "false_duplicate_rate": _safe_ratio(
            false_duplicate, duplicate_negatives
        ),
        "false_internal_no_match_rate": _safe_ratio(
            false_no_match, len(positive_queries)
        ),
        "no_hit_false_positive_rate": _safe_ratio(
            no_hit_false_positives, len(no_hit_queries)
        ),
        "abstention_accuracy": _safe_ratio(
            abstention_correct, len(query_ids)
        ),
    }


def _evidence_metrics(output, query_ids, queries, qrels):
    supported = 0
    unsupported = 0
    unjudged = 0
    supported_pairs = set()
    positive_pairs = {
        key
        for key, qrel in qrels.items()
        if key[0] in query_ids and qrel["gain"] > 0
    }
    positive_claims = 0
    unsupported_claims = 0
    for query_id in query_ids:
        row = output[query_id]["row"]
        evidence = row["evidence_ids"]
        for record_id in evidence:
            qrel = qrels.get((query_id, record_id))
            if qrel is None:
                unjudged += 1
            elif qrel["gain"] > 0:
                supported += 1
                supported_pairs.add((query_id, record_id))
            else:
                unsupported += 1
        if row["relation"] not in {"", "no_match", "unrelated"}:
            positive_claims += 1
            exact_support = any(
                (
                    qrels.get((query_id, record_id)) or {}
                ).get("relation")
                == row["relation"]
                for record_id in evidence
            )
            if not exact_support:
                unsupported_claims += 1
    return {
        "evidence_precision": _safe_ratio(
            supported, supported + unsupported
        ),
        "evidence_recall": _safe_ratio(
            len(supported_pairs), len(positive_pairs)
        ),
        "unsupported_claim_rate": _safe_ratio(
            unsupported_claims, positive_claims
        ),
        "supported_evidence_count": supported,
        "unsupported_evidence_count": unsupported,
        "unjudged_evidence_count": unjudged,
    }


def _percentile(values, proportion):
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    rank = max(1, math.ceil(proportion * len(ordered)))
    return _rounded(ordered[rank - 1])


def _operations_metrics(output, query_ids):
    latencies = [
        output[query_id]["row"]["latency_ms"]
        for query_id in query_ids
    ]
    tokens = [
        output[query_id]["row"]["input_tokens"]
        for query_id in query_ids
    ]
    pairs = [
        output[query_id]["row"]["comparator_pairs"]
        for query_id in query_ids
    ]
    return {
        "latency_ms_p50": _percentile(latencies, 0.50),
        "latency_ms_p95": _percentile(latencies, 0.95),
        "input_tokens_p50": _percentile(tokens, 0.50),
        "input_tokens_p95": _percentile(tokens, 0.95),
        "max_input_tokens": max(tokens) if tokens else None,
        "tokens_per_query": _rounded(statistics.mean(tokens)),
        "comparator_pairs_per_query": _rounded(
            statistics.mean(pairs)
        ),
        "max_comparator_pairs": max(pairs) if pairs else None,
        "cost_per_query": None,
    }


def _slice_metrics(output, query_ids, queries, qrels, arm):
    ranked = arm in {"retrieval-only", "end-to-end"}
    classified = arm != "retrieval-only"
    result = {}
    dimensions = {
        "theme": "theme",
        "lexical_overlap_bucket": "lexical_overlap_bucket",
        "relation_type": "relation_set",
        "history_age_bucket": "history_age_bucket",
    }
    for output_name, query_field in dimensions.items():
        buckets = {}
        values = sorted(
            {queries[query_id][query_field] for query_id in query_ids}
        )
        for value in values:
            selected = [
                query_id
                for query_id in query_ids
                if queries[query_id][query_field] == value
            ]
            ranking = (
                _rank_metrics(
                    output, selected, queries, qrels
                )
                if ranked
                else None
            )
            classification = _classification_metrics(
                output, selected, queries, qrels
            ) if classified else None
            buckets[value] = {
                "query_count": len(selected),
                "hit_at_10": (
                    ranking["hit_at"]["10"]
                    if ranking is not None
                    else None
                ),
                "relation_accuracy": (
                    classification["relation_accuracy"]
                    if classification is not None
                    else None
                ),
                "required_metrics": _required_metrics(
                    arm,
                    output,
                    selected,
                    queries,
                    qrels,
                    classification,
                ),
            }
        result[output_name] = buckets
    return result


def _arm_metrics(arm, output, queries, qrels):
    query_ids = sorted(output)
    ranked = arm in {"retrieval-only", "end-to-end"}
    classified = arm != "retrieval-only"
    classification = (
        _classification_metrics(
            output, query_ids, queries, qrels
        )
        if classified
        else None
    )
    return {
        "query_count": len(query_ids),
        "ranking": (
            _rank_metrics(output, query_ids, queries, qrels)
            if ranked
            else None
        ),
        "classification": classification,
        "evidence": (
            _evidence_metrics(
                output, query_ids, queries, qrels
            )
            if classified
            else None
        ),
        "operations": _operations_metrics(output, query_ids),
        "required_metrics": _required_metrics(
            arm,
            output,
            query_ids,
            queries,
            qrels,
            classification,
        ),
        "slices": _slice_metrics(
            output,
            query_ids,
            queries,
            qrels,
            arm,
        ),
    }


PRIMARY_METRICS = (
    "duplicate_hit_at_10",
    "duplicate_alert_precision",
    "lineage_ndcg_at_10",
    "failure_recall_at_10",
    "relation_accuracy",
    "false_duplicate_rate",
    "false_internal_no_match_rate",
)


def _metric_estimate(
    metric, output, query_ids, queries, qrels
):
    arm = next(
        (
            value["row"]["arm"]
            for value in output.values()
        ),
        None,
    )
    ranked = arm in {"retrieval-only", "end-to-end"}
    classified = arm != "retrieval-only"
    if metric == "duplicate_hit_at_10":
        if not ranked:
            return None
        values = []
        for query_id in query_ids:
            if (
                queries[query_id]["relation_set"] != "duplicate"
                or not _gold_ids(qrels, query_id)
            ):
                continue
            selected = set(
                _judged_ranked_ids(
                    output, query_id, qrels, cutoff=10
                )
            )
            values.append(
                1.0
                if selected & _gold_ids(qrels, query_id)
                else 0.0
            )
        return (
            _rounded(statistics.mean(values)) if values else None
        )
    if metric == "duplicate_alert_precision":
        duplicate_ids = [
            query_id
            for query_id in query_ids
            if queries[query_id]["relation_set"] == "duplicate"
        ]
        true_alerts = 0
        alerts = 0
        for query_id in duplicate_ids:
            if classified:
                predicted = output[query_id]["row"]["relation"] in {
                    "blocking",
                    "substantive",
                }
            else:
                predicted = bool(
                    _judged_ranked_ids(
                        output, query_id, qrels, cutoff=10
                    )
                )
            if predicted:
                alerts += 1
                if _gold_ids(qrels, query_id):
                    true_alerts += 1
        return _safe_ratio(true_alerts, alerts)
    if metric == "lineage_ndcg_at_10":
        if not ranked:
            return None
        selected = [
            query_id
            for query_id in query_ids
            if queries[query_id]["relation_set"] == "lineage"
            and _gold_ids(qrels, query_id)
        ]
        return _rank_metrics(
            output, selected, queries, qrels
        )["ndcg_at_10"]
    if metric == "failure_recall_at_10":
        if not ranked:
            return None
        selected = [
            query_id
            for query_id in query_ids
            if queries[query_id]["relation_set"] == "failure"
            and _gold_ids(qrels, query_id)
        ]
        return _rank_metrics(
            output, selected, queries, qrels
        )["recall_at"]["10"]
    if metric == "relation_accuracy":
        if not classified:
            return None
        values = [
            _query_relation_correct(
                query_id, output, queries, qrels
            )
            for query_id in query_ids
        ]
        return (
            _rounded(statistics.mean(values)) if values else None
        )
    if metric == "false_duplicate_rate":
        if not classified:
            return None
        false = 0
        negatives = 0
        for query_id in query_ids:
            if queries[query_id]["relation_set"] != "duplicate":
                continue
            for (candidate_query_id, record_id), qrel in qrels.items():
                if (
                    candidate_query_id != query_id
                    or qrel["relation"] != "unrelated"
                ):
                    continue
                negatives += 1
                predicted = output[query_id][
                    "pair_predictions"
                ].get(record_id)
                if predicted in {"blocking", "substantive"}:
                    false += 1
        return _safe_ratio(false, negatives)
    if metric == "false_internal_no_match_rate":
        if not classified:
            return None
        positives = [
            query_id
            for query_id in query_ids
            if _gold_ids(qrels, query_id)
        ]
        false = sum(
            1
            for query_id in positives
            if (
                output[query_id]["row"]["relation"] == "no_match"
                or output[query_id]["row"]["status"]
                == "complete_no_match"
            )
        )
        return _safe_ratio(false, len(positives))
    raise AssertionError(metric)


def _bootstrap_interval(values):
    if not values:
        return {"lower": None, "upper": None}
    ordered = sorted(values)
    lower_index = math.floor(0.025 * (len(ordered) - 1))
    upper_index = math.ceil(0.975 * (len(ordered) - 1))
    return {
        "lower": _rounded(ordered[lower_index]),
        "upper": _rounded(ordered[upper_index]),
    }


def _confidence_intervals(outputs, queries, qrels):
    query_ids = sorted(outputs["end-to-end"])
    generator = random.Random(BOOTSTRAP_SEED)
    samples = [
        [
            query_ids[generator.randrange(len(query_ids))]
            for _ in query_ids
        ]
        for _ in range(BOOTSTRAP_SAMPLES)
    ]
    arm_values = {}
    for arm in ARMS:
        metrics = {}
        for metric in PRIMARY_METRICS:
            estimate = _metric_estimate(
                metric,
                outputs[arm],
                query_ids,
                queries,
                qrels,
            )
            if estimate is None:
                continue
            bootstrapped = [
                value
                for selected in samples
                for value in [
                    _metric_estimate(
                        metric,
                        outputs[arm],
                        selected,
                        queries,
                        qrels,
                    )
                ]
                if value is not None
            ]
            metrics[metric] = {
                "estimate": estimate,
                "ci95": _bootstrap_interval(bootstrapped),
            }
        arm_values[arm] = metrics
    comparisons = (
        ("end-to-end", "retrieval-only"),
        ("end-to-end", "comparator-only"),
        ("end-to-end", "closed-book"),
    )
    differences = {}
    for candidate, baseline in comparisons:
        metrics = {}
        for metric in PRIMARY_METRICS:
            candidate_value = _metric_estimate(
                metric,
                outputs[candidate],
                query_ids,
                queries,
                qrels,
            )
            baseline_value = _metric_estimate(
                metric,
                outputs[baseline],
                query_ids,
                queries,
                qrels,
            )
            if candidate_value is None or baseline_value is None:
                continue
            bootstrapped = []
            for selected in samples:
                candidate_sample = _metric_estimate(
                    metric,
                    outputs[candidate],
                    selected,
                    queries,
                    qrels,
                )
                baseline_sample = _metric_estimate(
                    metric,
                    outputs[baseline],
                    selected,
                    queries,
                    qrels,
                )
                if (
                    candidate_sample is not None
                    and baseline_sample is not None
                ):
                    bootstrapped.append(
                        candidate_sample - baseline_sample
                    )
            metrics[metric] = {
                "estimate": _rounded(
                    candidate_value - baseline_value
                ),
                "ci95": _bootstrap_interval(bootstrapped),
            }
        differences[f"{candidate}_minus_{baseline}"] = metrics
    return {
        "schema_version": 1,
        "seed": BOOTSTRAP_SEED,
        "samples": BOOTSTRAP_SAMPLES,
        "arms": arm_values,
        "system_differences": differences,
    }


def _derive_relation_heldout_counts(queries, qrels):
    heldout = {
        query_id
        for query_id, query in queries.items()
        if query["fold"] == "test"
    }
    result = {}
    for relation in POSITIVE_RELATIONS:
        relation_set = RELATION_SET_BY_LABEL[relation]
        positive_queries = {
            query_id
            for (query_id, _), qrel in qrels.items()
            if query_id in heldout
            and queries[query_id]["relation_set"] == relation_set
            and qrel["relation"] == relation
        }
        hard_negative_queries = {
            query_id
            for (query_id, _), qrel in qrels.items()
            if query_id in heldout
            and queries[query_id]["relation_set"] == relation_set
            and qrel["hard_negative"]
        }
        positive = len(positive_queries)
        hard_negative = len(hard_negative_queries)
        result[relation] = {
            "positive": positive,
            "hard_negative": hard_negative,
            "advisory": positive < 30 or hard_negative < 30,
        }
    return result


def _evaluation_evidence_sha(confidence_intervals, channel_depths):
    if channel_depths is None:
        # Legacy v1 synthetic capabilities retain their historical digest.
        return sha256(canonical_bytes(confidence_intervals))
    return _framed_sha256(
        "history-calibration-evaluation-v2",
        canonical_bytes(
            {
                "confidence_intervals": confidence_intervals,
                "channel_depths": channel_depths,
            }
        ),
    )


def _gate_evidence(context, metrics, confidence_intervals):
    commitment = context["commitment"]
    end_to_end = metrics["end-to-end"]
    primary_sources = {
        "duplicate": (
            "duplicate_alert_precision",
            end_to_end["required_metrics"]["duplicate"][
                "alert_precision"
            ],
        ),
        "lineage": (
            "lineage_ndcg_at_10",
            end_to_end["required_metrics"]["lineage"][
                "ndcg_at_10"
            ],
        ),
        "failure": (
            "failure_recall_at_10",
            end_to_end["required_metrics"]["failure"][
                "recall_at"
            ]["10"],
        ),
    }
    primary = {}
    for relation_set, (metric, observed) in primary_sources.items():
        interval = confidence_intervals["arms"]["end-to-end"].get(
            metric
        )
        lower = (
            interval["ci95"]["lower"]
            if interval is not None
            else None
        )
        minimum = commitment["selected_thresholds"][
            relation_set
        ]
        primary[relation_set] = {
            "arm": "end-to-end",
            "metric": metric,
            "observed": observed,
            "ci95_lower": lower,
            "minimum": minimum,
            "passed": (
                observed is not None
                and lower is not None
                and lower >= minimum
            ),
        }
    error_metrics = {
        "max_false_duplicate_rate": "false_duplicate_rate",
        "max_false_internal_no_match_rate":
            "false_internal_no_match_rate",
    }
    errors = {}
    for budget_name, metric in error_metrics.items():
        observed = end_to_end["classification"][metric]
        interval = confidence_intervals["arms"]["end-to-end"].get(
            metric
        )
        upper = (
            interval["ci95"]["upper"]
            if interval is not None
            else None
        )
        maximum = commitment["error_budgets"][budget_name]
        errors[budget_name] = {
            "arm": "end-to-end",
            "metric": metric,
            "observed": observed,
            "ci95_upper": upper,
            "maximum": maximum,
            "passed": (
                observed is not None
                and upper is not None
                and upper <= maximum
            ),
        }
    operations = end_to_end["operations"]
    resources = {
        "latency_target_ms_p95": {
            "arm": "end-to-end",
            "metric": "latency_ms_p95",
            "observed": operations["latency_ms_p95"],
            "maximum": commitment["latency_target_ms_p95"],
            "passed": (
                operations["latency_ms_p95"] is not None
                and operations["latency_ms_p95"]
                <= commitment["latency_target_ms_p95"]
            ),
        },
        "token_budget": {
            "arm": "end-to-end",
            "metric": "max_input_tokens",
            "observed": operations["max_input_tokens"],
            "maximum": commitment["token_budget"],
            "passed": (
                operations["max_input_tokens"] is not None
                and operations["max_input_tokens"]
                <= commitment["token_budget"]
            ),
        },
    }
    output = context["outputs"]["end-to-end"]
    corpus = context["corpus"]
    channel_depths = None
    if context["output_schema_version"] == OUTPUT_SCHEMA_VERSION:
        channel_depths = {
            channel: max(
                value["row"]["channel_depths"][channel]
                for value in output.values()
            )
            for channel in sorted(CHANNEL_DEPTH_FIELDS)
        }
        per_channel_depth = max(channel_depths.values())
    else:
        # v1 is retained only for synthetic compatibility and has no
        # execution-level channel evidence. It cannot authorize production.
        per_channel_depth = max(
            len(value["row"]["ranked_record_ids"])
            for value in output.values()
        )
    observed_depths = {
        "per_channel_depth": per_channel_depth,
        "comparator_cutoff": max(
            value["row"]["comparator_pairs"]
            for value in output.values()
        ),
        "final_lineage_count": max(
            len(
                {
                    corpus[record_id]["lineage_id"]
                    for record_id in value["row"][
                        "ranked_record_ids"
                    ]
                }
            )
            for value in output.values()
        ),
    }
    depths = {}
    for name, observed in observed_depths.items():
        maximum = commitment["selected_depths"][name]
        depths[name] = {
            "observed": observed,
            "maximum": maximum,
            "passed": observed <= maximum,
        }
    coverage = _derive_relation_heldout_counts(
        context["queries"], context["qrels"]
    )
    all_gates = (
        all(not value["advisory"] for value in coverage.values())
        and context["agreement_rate"] is not None
        and all(value["passed"] for value in primary.values())
        and all(value["passed"] for value in errors.values())
        and all(value["passed"] for value in resources.values())
        and all(value["passed"] for value in depths.values())
    )
    return {
        "schema_version": 1,
        "primary_metrics": primary,
        "error_budgets": errors,
        "resource_limits": resources,
        "selected_depths": depths,
        "confidence_intervals_sha256": _evaluation_evidence_sha(
            confidence_intervals, channel_depths
        ),
        "all_gates_passed": all_gates,
    }


def _paired_bootstrap(confidence_intervals):
    return {
        "schema_version": 1,
        "seed": BOOTSTRAP_SEED,
        "samples": BOOTSTRAP_SAMPLES,
        "confidence_intervals_sha256": sha256(
            canonical_bytes(confidence_intervals)
        ),
    }


def _schema_root(benchmark_root):
    candidate = benchmark_root.parent
    required = (
        "calibration-policy-commitment.schema.json",
        "calibration-pre-heldout-receipt.schema.json",
        "calibration-capability.schema.json",
    )
    if all((candidate / name).is_file() for name in required):
        return candidate
    fallback = ROOT / "calib/history-retrieval"
    if all((fallback / name).is_file() for name in required):
        return fallback
    raise BenchmarkError("calibration JSON schemas are unavailable")


def _validate_schema_contracts(benchmark_root):
    schema_root = _schema_root(benchmark_root)
    contracts = (
        (
            "calibration-policy-commitment.schema.json",
            COMMITMENT_FIELDS,
        ),
        (
            "calibration-pre-heldout-receipt.schema.json",
            RECEIPT_FIELDS,
        ),
        (
            "calibration-capability.schema.json",
            CAPABILITY_FIELDS,
        ),
    )
    hashes = {}
    for name, fields in contracts:
        value, raw = _read_canonical_json(
            schema_root / name,
            name,
            maximum=MAX_SCHEMA_BYTES,
            boundary=schema_root,
        )
        if (
            not isinstance(value, dict)
            or value.get("$schema")
            != "https://json-schema.org/draft/2020-12/schema"
            or value.get("type") != "object"
            or value.get("additionalProperties") is not False
            or set(value.get("required", [])) != set(fields)
            or set(value.get("properties", {})) != set(fields)
        ):
            raise BenchmarkError(
                f"{name} is not the closed runtime contract"
            )
        hashes["schemas/" + name] = sha256(raw)
    return hashes


def _snapshot_sha(raw):
    return sha256(
        b"history-benchmark-snapshot-v1\0"
        + canonical_bytes(
            {name: sha256(raw[name]) for name in SNAPSHOT_INPUTS}
        )
    )


def _heldout_output_sha(raw):
    return sha256(
        b"history-heldout-outputs-v1\0"
        + canonical_bytes(
            {
                arm: sha256(raw[f"outputs/{arm}.jsonl"])
                for arm in ARMS
            }
        )
    )


def _numeric_equal(actual, expected, tolerance):
    if (
        isinstance(actual, bool)
        or isinstance(expected, bool)
        or not isinstance(actual, (int, float))
        or not isinstance(expected, (int, float))
    ):
        return actual == expected
    return math.isclose(
        float(actual),
        float(expected),
        rel_tol=0.0,
        abs_tol=tolerance,
    )


def _compare_expected(actual, expected, tolerance, path="metrics"):
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or set(actual) != set(expected):
            raise BenchmarkError(
                f"expected metrics mismatch at {path}"
            )
        for key in sorted(expected):
            _compare_expected(
                actual[key],
                expected[key],
                tolerance,
                f"{path}.{key}",
            )
        return
    if isinstance(expected, list):
        if (
            not isinstance(actual, list)
            or len(actual) != len(expected)
        ):
            raise BenchmarkError(
                f"expected metrics mismatch at {path}"
            )
        for index, value in enumerate(expected):
            _compare_expected(
                actual[index],
                value,
                tolerance,
                f"{path}[{index}]",
            )
        return
    if not _numeric_equal(actual, expected, tolerance):
        raise BenchmarkError(f"expected metrics mismatch at {path}")


def _prepare_benchmark(
    benchmark,
    *,
    policy_path=DEFAULT_POLICY,
    policy_override=None,
    trust_root_override=None,
    required_scope=None,
    witness_verifier=None,
):
    root, values, raw = _load_inputs(benchmark)
    schema_hashes = _validate_schema_contracts(root)
    if policy_override is None:
        policy, policy_raw = _load_policy_snapshot(policy_path)
    else:
        if not isinstance(policy_override, dict):
            raise BenchmarkError(
                "retrieval policy override is invalid"
            )
        policy = policy_override
        policy_raw = canonical_bytes(policy)
    corpus = _validate_corpus(values["corpus.jsonl"])
    queries = _validate_queries(values["queries.jsonl"], corpus)
    lineage_fold, query_fold = _validate_folds(
        values["folds.json"], queries, corpus
    )
    qrels = _validate_qrels(
        values["qrels.jsonl"],
        queries,
        corpus,
        lineage_fold,
    )
    adjudications, agreement_rate = _validate_adjudications(
        values["adjudications.jsonl"], queries, qrels
    )
    packs = _validate_packs(
        values["oracle-packs.jsonl"], queries, corpus, qrels
    )
    scope = values["policy-commitment.json"].get("scope")
    outputs = _validate_outputs(
        values, queries, corpus, qrels, packs, scope
    )
    commitment = values["policy-commitment.json"]
    commitment_meta = _validate_commitment(
        commitment,
        policy,
        raw,
        values["folds.json"],
        query_fold,
    )
    if _parse_utc(
        commitment["sealed_at"], "policy commitment seal time"
    ) >= _parse_utc(
        outputs["heldout_started_at"], "held-out start time"
    ):
        raise BenchmarkError(
            "policy thresholds must be sealed before the "
            "held-out run"
        )
    trust_root = (
        values.get("test-witness-key.json")
        if trust_root_override is None
        else trust_root_override
    )
    scope = commitment["scope"]
    if required_scope is not None and scope != required_scope:
        raise BenchmarkError(
            "benchmark calibration scope is invalid"
        )
    receipt = values["pre-heldout-receipt.json"]
    receipt_meta = _validate_receipt(
        receipt,
        commitment,
        trust_root,
        scope,
        outputs["heldout_started_at"],
        witness_verifier=witness_verifier,
    )
    if (
        outputs["policy_commitment_sha256"]
        != receipt_meta["commitment_sha256"]
    ):
        raise BenchmarkError(
            "held-out output policy commitment SHA is invalid"
        )
    if (
        outputs["preheldout_receipt_sha256"]
        != receipt_meta["receipt_sha256"]
    ):
        raise BenchmarkError(
            "held-out output pre-held-out receipt SHA is invalid"
        )
    if outputs["heldout_run_nonce"] != receipt["run_nonce"]:
        raise BenchmarkError(
            "held-out output run nonce is invalid"
        )
    input_hashes = {
        name: sha256(content)
        for name, content in raw.items()
    }
    input_hashes["history/retrieval-policy-v1.json"] = sha256(
        policy_raw
    )
    input_hashes.update(schema_hashes)
    return {
        "root": root,
        "values": values,
        "raw": raw,
        "policy": policy,
        "corpus": corpus,
        "queries": queries,
        "qrels": qrels,
        "adjudications": adjudications,
        "packs": packs,
        "outputs": outputs["arms"],
        "output_schema_version": outputs["schema_version"],
        "scope": scope,
        "heldout_started_at": outputs["heldout_started_at"],
        "agreement_rate": _rounded(agreement_rate),
        "commitment": commitment,
        "receipt": receipt,
        "commitment_meta": commitment_meta,
        "receipt_meta": receipt_meta,
        "benchmark_snapshot_sha256": _snapshot_sha(raw),
        "heldout_output_sha256": _heldout_output_sha(raw),
        "input_sha256s": dict(sorted(input_hashes.items())),
    }


def evaluate_benchmark(
    benchmark,
    *,
    policy_path=DEFAULT_POLICY,
    trust_root=None,
    witness_verifier=None,
    verify_expected=True,
):
    """Validate one temporal benchmark and compute its four arms."""
    context = _prepare_benchmark(
        benchmark,
        policy_path=policy_path,
        trust_root_override=trust_root,
        witness_verifier=witness_verifier,
    )
    metrics = {
        arm: _arm_metrics(
            arm,
            context["outputs"][arm],
            context["queries"],
            context["qrels"],
        )
        for arm in ARMS
    }
    confidence_intervals = _confidence_intervals(
        context["outputs"],
        context["queries"],
        context["qrels"],
    )
    paired = _paired_bootstrap(confidence_intervals)
    expected = context["values"]["expected-metrics.json"]
    if verify_expected:
        _require_closed(
            expected,
            {
                "schema_version",
                "scope",
                "tolerance",
                "metrics",
                "confidence_intervals",
                "paired_bootstrap",
            },
            "expected metrics",
        )
        if (
            expected["schema_version"] != 1
            or expected["scope"] != context["scope"]
        ):
            raise BenchmarkError("expected metrics scope is invalid")
        _validate_number(
            expected["tolerance"],
            "expected metric tolerance",
            minimum=0.0,
        )
        _compare_expected(
            metrics,
            expected["metrics"],
            float(expected["tolerance"]),
        )
        _compare_expected(
            confidence_intervals,
            expected["confidence_intervals"],
            float(expected["tolerance"]),
            "confidence_intervals",
        )
        _compare_expected(
            paired,
            expected["paired_bootstrap"],
            float(expected["tolerance"]),
            "paired_bootstrap",
        )
    return {
        "schema_version": "history-retrieval-evaluation-v1",
        "scope": context["scope"],
        "enforcement_eligible": False
        if context["scope"] == SYNTHETIC_SCOPE
        else None,
        "policy_version": context["policy"][
            "retrieval_policy_version"
        ],
        "policy_commitment_sha256": context["receipt_meta"][
            "commitment_sha256"
        ],
        "preheldout_receipt_sha256": context["receipt_meta"][
            "receipt_sha256"
        ],
        "benchmark_snapshot_sha256": context[
            "benchmark_snapshot_sha256"
        ],
        "heldout_output_sha256": context[
            "heldout_output_sha256"
        ],
        "adjudicator_initial_agreement": context[
            "agreement_rate"
        ],
        "input_sha256s": context["input_sha256s"],
        "arms": list(ARMS),
        "metrics": metrics,
        "confidence_intervals": confidence_intervals,
        "paired_bootstrap": paired,
    }


def _framed_sha256(domain, raw):
    domain_bytes = domain.encode("utf-8")
    digest = hashlib.sha256()
    for part in (domain_bytes, raw):
        digest.update(struct.pack(">Q", len(part)))
        digest.update(part)
    return digest.hexdigest()


def _criteria_sha(commitment):
    material = {
        name: commitment[name]
        for name in (
            "selected_thresholds",
            "error_budgets",
            "selected_depths",
            "latency_target_ms_p95",
            "token_budget",
        )
    }
    material["per_channel_depths"] = {
        channel: commitment["selected_depths"]["per_channel_depth"]
        for channel in sorted(CHANNEL_DEPTH_FIELDS)
    }
    return _framed_sha256(
        "history-calibration-criteria-v2",
        canonical_bytes(material),
    )


def _capability_seal_material(capability):
    value = dict(capability)
    value.pop("canonical_seal_sha256", None)
    value.pop("signature", None)
    return value


def _capability_seal_sha(capability):
    version = capability.get("schema_version")
    domain_version = (
        "v2" if version == CAPABILITY_SCHEMA_VERSION else "v1"
    )
    return sha256(
        ("history-calibration-capability-%s\0" % domain_version).encode(
            "ascii"
        )
        + canonical_bytes(_capability_seal_material(capability))
    )


def _capability_signature_domain(version):
    domain_version = (
        "v2" if version == CAPABILITY_SCHEMA_VERSION else "v1"
    )
    return (
        "history-calibration-capability-signature-%s\0"
        % domain_version
    ).encode("ascii")


def build_synthetic_capability_for_test(
    benchmark,
    *,
    policy_path=DEFAULT_POLICY,
):
    """Build a test-key capability that production always rejects."""
    context = _prepare_benchmark(
        benchmark, policy_path=policy_path
    )
    if context["scope"] != SYNTHETIC_SCOPE:
        raise BenchmarkError(
            "synthetic capability builder requires synthetic scope"
        )
    trust_root = context["values"]["test-witness-key.json"]
    metrics = {
        arm: _arm_metrics(
            arm,
            context["outputs"][arm],
            context["queries"],
            context["qrels"],
        )
        for arm in ARMS
    }
    confidence_intervals = _confidence_intervals(
        context["outputs"],
        context["queries"],
        context["qrels"],
    )
    capability_version = (
        CAPABILITY_SCHEMA_VERSION
        if context["output_schema_version"] == OUTPUT_SCHEMA_VERSION
        else LEGACY_CAPABILITY_SCHEMA_VERSION
    )
    capability = {
        "schema_version": capability_version,
        "scope": SYNTHETIC_SCOPE,
        "trust_root_id": trust_root["trust_root_id"],
        "policy_commitment_sha256": context["receipt_meta"][
            "commitment_sha256"
        ],
        "preheldout_receipt_sha256": context["receipt_meta"][
            "receipt_sha256"
        ],
        "policy_version": context["policy"][
            "retrieval_policy_version"
        ],
        "policy_sha256": sha256(
            canonical_bytes(context["policy"])
        ),
        "benchmark_snapshot_sha256": context[
            "benchmark_snapshot_sha256"
        ],
        "qrels_sha256": sha256(context["raw"]["qrels.jsonl"]),
        "adjudications_sha256": sha256(
            context["raw"]["adjudications.jsonl"]
        ),
        "relation_heldout_counts": {
            **_derive_relation_heldout_counts(
                context["queries"], context["qrels"]
            )
        },
        "unresolved_adjudications": 0,
        "heldout_output_sha256": context[
            "heldout_output_sha256"
        ],
        "heldout_run_nonce": context["receipt"][
            "run_nonce"
        ],
        "heldout_started_at": context["heldout_started_at"],
        "evaluation_evidence": _gate_evidence(
            context, metrics, confidence_intervals
        ),
    }
    if capability_version == CAPABILITY_SCHEMA_VERSION:
        capability["criteria_sha256"] = _criteria_sha(
            context["commitment"]
        )
        # Synthetic fixtures have no production witness for completion.
        # Equality is the deterministic, least-authoritative test interval.
        capability["heldout_completed_at"] = context[
            "heldout_started_at"
        ]
    capability["canonical_seal_sha256"] = _capability_seal_sha(
        capability
    )
    capability["signature"] = _test_signature(
        _capability_signature_domain(capability_version),
        capability,
        trust_root,
    )
    return {
        "schema_version": 1,
        "policy_commitment": context["commitment"],
        "preheldout_receipt": context["receipt"],
        "calibration_capability": capability,
    }


def _load_value(value, label):
    if isinstance(value, (str, pathlib.Path)):
        parsed, _ = _read_canonical_json(value, label)
        return parsed
    if isinstance(value, bytes):
        if len(value) > MAX_JSON_BYTES:
            raise BenchmarkError(f"{label} exceeds its byte bound")
        try:
            parsed = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BenchmarkError(f"{label} is invalid JSON") from exc
        if value != canonical_bytes(parsed):
            raise BenchmarkError(f"{label} is not canonical JSON")
        return parsed
    return value


def _optional_number(value, label):
    if value is None:
        return
    _validate_number(value, label)


def _require_finite_numbers(value, label):
    if isinstance(value, float) and not math.isfinite(value):
        raise BenchmarkError(f"{label} contains a non-finite number")
    if isinstance(value, dict):
        for item in value.values():
            _require_finite_numbers(item, label)
    elif isinstance(value, list):
        for item in value:
            _require_finite_numbers(item, label)


def _validate_evaluation_evidence(evidence, counts):
    _require_finite_numbers(
        evidence, "calibration evaluation evidence"
    )
    _require_closed(
        evidence,
        {
            "schema_version",
            "primary_metrics",
            "error_budgets",
            "resource_limits",
            "selected_depths",
            "confidence_intervals_sha256",
            "all_gates_passed",
        },
        "calibration evaluation evidence",
    )
    if (
        evidence["schema_version"] != 1
        or type(evidence["all_gates_passed"]) is not bool
    ):
        raise BenchmarkError(
            "calibration evaluation evidence is invalid"
        )
    _require_sha(
        evidence["confidence_intervals_sha256"],
        "calibration confidence intervals SHA",
    )
    primary = evidence["primary_metrics"]
    if (
        not isinstance(primary, dict)
        or set(primary) != set(RELATION_GAINS)
    ):
        raise BenchmarkError(
            "calibration primary metric evidence is invalid"
        )
    primary_passed = []
    for relation_set, value in primary.items():
        _require_closed(
            value,
            {
                "arm",
                "metric",
                "observed",
                "ci95_lower",
                "minimum",
                "passed",
            },
            f"{relation_set} primary metric evidence",
        )
        if (
            value["arm"] != "end-to-end"
            or not isinstance(value["metric"], str)
            or not value["metric"]
            or type(value["passed"]) is not bool
        ):
            raise BenchmarkError(
                "calibration primary metric evidence is invalid"
            )
        _optional_number(
            value["observed"],
            f"{relation_set} primary metric observation",
        )
        _optional_number(
            value["ci95_lower"],
            f"{relation_set} primary metric lower bound",
        )
        _validate_number(
            value["minimum"],
            f"{relation_set} primary metric minimum",
            maximum=1.0,
        )
        expected = (
            value["observed"] is not None
            and value["ci95_lower"] is not None
            and value["ci95_lower"] >= value["minimum"]
        )
        if value["passed"] != expected:
            raise BenchmarkError(
                "calibration primary metric gate is inconsistent"
            )
        primary_passed.append(expected)
    budgets = evidence["error_budgets"]
    if (
        not isinstance(budgets, dict)
        or set(budgets)
        != {
            "max_false_duplicate_rate",
            "max_false_internal_no_match_rate",
        }
    ):
        raise BenchmarkError(
            "calibration error-budget evidence is invalid"
        )
    budget_passed = []
    for name, value in budgets.items():
        _require_closed(
            value,
            {
                "arm",
                "metric",
                "observed",
                "ci95_upper",
                "maximum",
                "passed",
            },
            f"{name} evidence",
        )
        if (
            value["arm"] != "end-to-end"
            or not isinstance(value["metric"], str)
            or not value["metric"]
            or type(value["passed"]) is not bool
        ):
            raise BenchmarkError(
                "calibration error-budget evidence is invalid"
            )
        for field in ("observed", "ci95_upper", "maximum"):
            _optional_number(
                value[field],
                f"{name} {field.replace('_', ' ')}",
            )
        expected = (
            value["observed"] is not None
            and value["ci95_upper"] is not None
            and value["ci95_upper"] <= value["maximum"]
        )
        if value["passed"] != expected:
            raise BenchmarkError(
                "calibration error-budget gate is inconsistent"
            )
        budget_passed.append(expected)
    resources = evidence["resource_limits"]
    if (
        not isinstance(resources, dict)
        or set(resources)
        != {"latency_target_ms_p95", "token_budget"}
    ):
        raise BenchmarkError(
            "calibration resource evidence is invalid"
        )
    resource_passed = []
    for name, value in resources.items():
        _require_closed(
            value,
            {
                "arm",
                "metric",
                "observed",
                "maximum",
                "passed",
            },
            f"{name} evidence",
        )
        if (
            value["arm"] != "end-to-end"
            or not isinstance(value["metric"], str)
            or not value["metric"]
            or type(value["passed"]) is not bool
        ):
            raise BenchmarkError(
                "calibration resource evidence is invalid"
            )
        _optional_number(value["observed"], f"{name} observed")
        _validate_number(
            value["maximum"], f"{name} maximum", minimum=0.000001
        )
        expected = (
            value["observed"] is not None
            and value["observed"] <= value["maximum"]
        )
        if value["passed"] != expected:
            raise BenchmarkError(
                "calibration resource gate is inconsistent"
            )
        resource_passed.append(expected)
    depths = evidence["selected_depths"]
    if (
        not isinstance(depths, dict)
        or set(depths)
        != {
            "per_channel_depth",
            "comparator_cutoff",
            "final_lineage_count",
        }
    ):
        raise BenchmarkError(
            "calibration depth evidence is invalid"
        )
    depth_passed = []
    for name, value in depths.items():
        _require_closed(
            value,
            {"observed", "maximum", "passed"},
            f"{name} depth evidence",
        )
        if (
            type(value["observed"]) is not int
            or value["observed"] < 0
            or type(value["maximum"]) is not int
            or value["maximum"] < 1
            or type(value["passed"]) is not bool
        ):
            raise BenchmarkError(
                "calibration depth evidence is invalid"
            )
        expected = value["observed"] <= value["maximum"]
        if value["passed"] != expected:
            raise BenchmarkError(
                "calibration depth gate is inconsistent"
            )
        depth_passed.append(expected)
    expected_all = (
        all(not value["advisory"] for value in counts.values())
        and all(primary_passed)
        and all(budget_passed)
        and all(resource_passed)
        and all(depth_passed)
    )
    if evidence["all_gates_passed"] != expected_all:
        raise BenchmarkError(
            "calibration all-gates evidence is inconsistent"
        )


def validate_capability_artifact(
    capability, *, required_scope=PRODUCTION_SCOPE
):
    """Validate one sealed capability's public, trust-agnostic contract."""
    if required_scope not in {SYNTHETIC_SCOPE, PRODUCTION_SCOPE}:
        raise BenchmarkError("calibration scope is invalid")
    value = _load_value(capability, "calibration capability")
    if not isinstance(value, dict):
        raise BenchmarkError("calibration capability schema is invalid")
    capability_version = value.get("schema_version")
    if capability_version == CAPABILITY_SCHEMA_VERSION:
        fields = CAPABILITY_FIELDS
    elif capability_version == LEGACY_CAPABILITY_SCHEMA_VERSION:
        fields = LEGACY_CAPABILITY_FIELDS
    else:
        raise BenchmarkError(
            "calibration capability version is invalid for scope"
        )
    _require_closed(
        value, fields, "calibration capability"
    )
    if (
        value["schema_version"] != capability_version
        or value["scope"] not in {
            SYNTHETIC_SCOPE,
            PRODUCTION_SCOPE,
        }
    ):
        raise BenchmarkError(
            "calibration capability schema is invalid"
        )
    if (
        value["scope"] == SYNTHETIC_SCOPE
        and required_scope == PRODUCTION_SCOPE
    ):
        raise BenchmarkError(
            "synthetic_contract_only capability cannot enable "
            "production"
        )
    if value["scope"] != required_scope:
        raise BenchmarkError("calibration capability scope is invalid")
    if (
        not isinstance(value["trust_root_id"], str)
        or not value["trust_root_id"]
        or not isinstance(value["policy_version"], str)
        or not value["policy_version"]
        or type(value["heldout_run_nonce"]) is not int
        or value["heldout_run_nonce"] < 1
        or type(value["unresolved_adjudications"]) is not int
        or value["unresolved_adjudications"] != 0
    ):
        raise BenchmarkError(
            "calibration capability binding is invalid"
        )
    heldout_started = _parse_utc(
        value["heldout_started_at"],
        "calibration capability held-out start",
    )
    if capability_version == CAPABILITY_SCHEMA_VERSION:
        _require_sha(
            value["criteria_sha256"],
            "calibration capability criteria SHA",
        )
        heldout_completed = _parse_utc(
            value["heldout_completed_at"],
            "calibration capability held-out completion",
        )
        if heldout_completed < heldout_started:
            raise BenchmarkError(
                "calibration capability completion precedes start"
            )
    for field in (
        "policy_commitment_sha256",
        "preheldout_receipt_sha256",
        "policy_sha256",
        "benchmark_snapshot_sha256",
        "qrels_sha256",
        "adjudications_sha256",
        "heldout_output_sha256",
        "canonical_seal_sha256",
        "signature",
    ):
        _require_sha(
            value[field],
            "calibration capability " + field.replace("_", " "),
        )
    counts = value["relation_heldout_counts"]
    if (
        not isinstance(counts, dict)
        or set(counts) != set(POSITIVE_RELATIONS)
    ):
        raise BenchmarkError(
            "calibration relation held-out counts are invalid"
        )
    for relation, relation_counts in counts.items():
        _require_closed(
            relation_counts,
            {"positive", "hard_negative", "advisory"},
            f"{relation} held-out counts",
        )
        if (
            type(relation_counts["positive"]) is not int
            or relation_counts["positive"] < 0
            or type(relation_counts["hard_negative"]) is not int
            or relation_counts["hard_negative"] < 0
            or type(relation_counts["advisory"]) is not bool
            or relation_counts["advisory"]
            != (
                relation_counts["positive"] < 30
                or relation_counts["hard_negative"] < 30
            )
        ):
            raise BenchmarkError(
                "calibration relation held-out counts are invalid"
            )
    _validate_evaluation_evidence(
        value["evaluation_evidence"], counts
    )
    if required_scope == PRODUCTION_SCOPE and (
        any(item["advisory"] for item in counts.values())
        or not value["evaluation_evidence"]["all_gates_passed"]
    ):
        raise BenchmarkError(
            "production calibration evidence is advisory or failed"
        )
    expected_seal = _capability_seal_sha(value)
    if value["canonical_seal_sha256"] != expected_seal:
        raise BenchmarkError(
            "calibration capability canonical seal is invalid"
        )
    normalized = json.loads(canonical_bytes(value))
    return {
        "scope": required_scope,
        "enforcement_eligible": (
            required_scope == PRODUCTION_SCOPE
        ),
        "relation_heldout_counts": normalized[
            "relation_heldout_counts"
        ],
        "evaluation_evidence": normalized[
            "evaluation_evidence"
        ],
        "canonical_seal_sha256": normalized[
            "canonical_seal_sha256"
        ],
    }


def validate_calibration_capability(
    bundle,
    *,
    policy,
    trust_root,
    required_scope=PRODUCTION_SCOPE,
    benchmark,
    witness_verifier=None,
):
    """Validate a sealed calibration capability against exact inputs."""
    if required_scope not in {SYNTHETIC_SCOPE, PRODUCTION_SCOPE}:
        raise BenchmarkError("calibration scope is invalid")
    value = _load_value(bundle, "calibration capability bundle")
    _require_closed(
        value,
        {
            "schema_version",
            "policy_commitment",
            "preheldout_receipt",
            "calibration_capability",
        },
        "calibration capability bundle",
    )
    if value["schema_version"] != 1:
        raise BenchmarkError(
            "calibration capability bundle version is invalid"
        )
    commitment = value["policy_commitment"]
    receipt = value["preheldout_receipt"]
    capability = value["calibration_capability"]
    if (
        isinstance(capability, dict)
        and capability.get("scope") == SYNTHETIC_SCOPE
        and required_scope == PRODUCTION_SCOPE
    ):
        raise BenchmarkError(
            "synthetic_contract_only capability cannot enable "
            "production"
        )
    policy_value = _load_value(policy, "retrieval policy")
    policy_path = DEFAULT_POLICY
    if isinstance(policy, (str, pathlib.Path)):
        policy_path = pathlib.Path(policy)
    trust_root_value = _load_value(
        trust_root, "calibration trust root"
    )
    context = _prepare_benchmark(
        benchmark,
        policy_path=policy_path,
        policy_override=policy_value,
        trust_root_override=trust_root_value,
        required_scope=required_scope,
        witness_verifier=witness_verifier,
    )
    if policy_value != context["policy"]:
        raise BenchmarkError(
            "calibration policy object does not match benchmark policy"
        )
    if commitment != context["commitment"]:
        raise BenchmarkError(
            "calibration capability policy commitment changed"
        )
    if receipt != context["receipt"]:
        raise BenchmarkError(
            "calibration capability pre-held-out receipt changed"
        )
    if commitment["scope"] != required_scope:
        raise BenchmarkError(
            "calibration commitment scope is invalid"
        )
    receipt_meta = _validate_receipt(
        receipt,
        commitment,
        trust_root_value,
        required_scope,
        context["heldout_started_at"],
        witness_verifier=witness_verifier,
    )
    if not isinstance(capability, dict):
        raise BenchmarkError("calibration capability schema is invalid")
    capability_fields = (
        CAPABILITY_FIELDS
        if capability.get("schema_version") == CAPABILITY_SCHEMA_VERSION
        else LEGACY_CAPABILITY_FIELDS
    )
    _require_closed(
        capability,
        capability_fields,
        "calibration capability",
    )
    artifact_evidence = validate_capability_artifact(
        capability, required_scope=required_scope
    )
    if (
        capability["schema_version"]
        not in {
            LEGACY_CAPABILITY_SCHEMA_VERSION,
            CAPABILITY_SCHEMA_VERSION,
        }
        or capability["scope"] != required_scope
        or capability["trust_root_id"]
        != receipt["trust_root_id"]
        or capability["policy_commitment_sha256"]
        != receipt_meta["commitment_sha256"]
        or capability["preheldout_receipt_sha256"]
        != receipt_meta["receipt_sha256"]
        or capability["policy_version"]
        != policy_value.get("retrieval_policy_version")
        or capability["policy_sha256"]
        != sha256(canonical_bytes(policy_value))
        or capability["benchmark_snapshot_sha256"]
        != context["benchmark_snapshot_sha256"]
        or capability["qrels_sha256"]
        != sha256(context["raw"]["qrels.jsonl"])
        or capability["adjudications_sha256"]
        != sha256(context["raw"]["adjudications.jsonl"])
        or capability["heldout_output_sha256"]
        != context["heldout_output_sha256"]
        or capability["heldout_run_nonce"]
        != receipt["run_nonce"]
        or capability["heldout_started_at"]
        != context["heldout_started_at"]
        or capability["unresolved_adjudications"] != 0
    ):
        raise BenchmarkError(
            "calibration capability binding is invalid"
        )
    if capability["schema_version"] == CAPABILITY_SCHEMA_VERSION:
        if (
            context["output_schema_version"] != OUTPUT_SCHEMA_VERSION
            or capability["criteria_sha256"]
            != _criteria_sha(commitment)
        ):
            raise BenchmarkError(
                "calibration capability criteria or execution evidence "
                "binding is invalid"
            )
    derived_counts = _derive_relation_heldout_counts(
        context["queries"], context["qrels"]
    )
    metrics = {
        arm: _arm_metrics(
            arm,
            context["outputs"][arm],
            context["queries"],
            context["qrels"],
        )
        for arm in ARMS
    }
    confidence_intervals = _confidence_intervals(
        context["outputs"],
        context["queries"],
        context["qrels"],
    )
    derived_evidence = _gate_evidence(
        context, metrics, confidence_intervals
    )
    if capability["relation_heldout_counts"] != derived_counts:
        raise BenchmarkError(
            "calibration capability derived held-out coverage "
            "is invalid"
        )
    if capability["evaluation_evidence"] != derived_evidence:
        raise BenchmarkError(
            "calibration capability derived evaluation evidence "
            "is invalid"
        )
    if required_scope == SYNTHETIC_SCOPE:
        if (
            not isinstance(trust_root_value, dict)
            or trust_root_value.get("scope") != SYNTHETIC_SCOPE
            or capability["trust_root_id"]
            != trust_root_value.get("trust_root_id")
        ):
            raise BenchmarkError(
                "synthetic capability trust root mismatch"
            )
        unsigned = dict(capability)
        signature = unsigned.pop("signature")
        expected_signature = _test_signature(
            _capability_signature_domain(
                capability["schema_version"]
            ),
            unsigned,
            trust_root_value,
        )
        if not hmac.compare_digest(
            signature, expected_signature
        ):
            raise BenchmarkError(
                "calibration capability signature is invalid"
            )
    else:
        if not callable(witness_verifier):
            raise BenchmarkError(
                "production capability witness verifier "
                "is unavailable"
            )
        try:
            verified = witness_verifier(
                "calibration_capability",
                capability,
                trust_root_value,
            )
        except Exception as exc:
            raise BenchmarkError(
                "production capability witness verification failed"
            ) from exc
        if verified is not True:
            raise BenchmarkError(
                "production capability witness verification failed"
            )
    return {
        "scope": required_scope,
        "enforcement_eligible": (
            required_scope == PRODUCTION_SCOPE
        ),
        "policy_commitment_sha256": receipt_meta[
            "commitment_sha256"
        ],
        "preheldout_receipt_sha256": receipt_meta[
            "receipt_sha256"
        ],
        "calibration_capability_sha256": sha256(
            canonical_bytes(capability)
        ),
        "relation_heldout_counts": artifact_evidence[
            "relation_heldout_counts"
        ],
        "evaluation_evidence": artifact_evidence[
            "evaluation_evidence"
        ],
    }


def write_evaluation(
    benchmark,
    output,
    *,
    policy_path=DEFAULT_POLICY,
):
    """Atomically write one reproducible evaluation artifact."""
    result = evaluate_benchmark(
        benchmark, policy_path=policy_path
    )
    destination = pathlib.Path(output)
    parent = destination.parent
    if parent.is_symlink():
        raise BenchmarkError(
            "evaluation output parent cannot be a symlink"
        )
    parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix="." + destination.name + ".",
        dir=str(parent),
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_bytes(result))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory = os.open(str(parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return result


def parser():
    result = argparse.ArgumentParser(
        description="Evaluate a sealed history-retrieval benchmark"
    )
    result.add_argument("--benchmark", required=True)
    result.add_argument("--output", required=True)
    result.add_argument("--policy", default=str(DEFAULT_POLICY))
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    result = write_evaluation(
        args.benchmark,
        args.output,
        policy_path=args.policy,
    )
    print(
        json.dumps(
            {
                "output": str(pathlib.Path(args.output)),
                "scope": result["scope"],
                "policy_commitment_sha256": result[
                    "policy_commitment_sha256"
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
