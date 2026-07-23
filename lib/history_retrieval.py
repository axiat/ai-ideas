#!/usr/bin/env python3
"""Deterministic bounded retrieval and host-owned history receipts."""

import hashlib
import json
import math

try:
    from lib import history_projection
    from lib import history_store
except ImportError:  # Direct execution through lib/history_cli.py.
    import history_projection
    import history_store


INTENTS = {
    "duplicate_search",
    "evolution_search",
    "failure_pattern_search",
}
RELATIONS = {
    "same_core_idea",
    "same_lineage_revision",
    "related_component",
    "same_failure_mechanism",
    "related_failure_pattern",
    "distinct",
    "uncertain",
}
FINAL_STATUSES = {
    "complete_match",
    "complete_no_match",
    "uncertain",
    "partial",
    "backend_failed",
    "budget_exceeded",
    "conflicting_evidence",
}
PERMANENT_STATUSES = {"complete_match", "complete_no_match"}
COMPARATOR_VERSION = "history-comparator-v1"
EVIDENCE_SPAN_LIMIT = 192


class RetrievalError(RuntimeError):
    pass


class ComparisonValidationError(RetrievalError):
    pass


class ReceiptReplayError(RetrievalError):
    pass


def canonical_bytes(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def pack_sha256(pack):
    value = dict(pack)
    value.pop("pack_sha256", None)
    value.pop("receipt_id", None)
    return _sha(canonical_bytes(value))


def _query_value(query, name, default=""):
    if isinstance(query, str):
        return query if name == "story" else default
    if not isinstance(query, dict):
        raise TypeError("query must be text or a mapping")
    value = query.get(name, default)
    if not isinstance(value, str):
        raise TypeError("query fields must be text")
    return value


def _query_facets(query, intent):
    story = _query_value(query, "story")
    theme = _query_value(query, "theme")
    supplied = {} if isinstance(query, str) else query.get("facets", {})
    if not isinstance(supplied, dict):
        raise TypeError("query facets must be a mapping")
    if intent == "failure_pattern_search":
        reason = _query_value(query, "reason")
        category = _query_value(query, "category")
        verdict = _query_value(query, "verdict")
        code = history_projection.failure_code(verdict, category, reason)
        return {"failure_pattern": " ".join((code, category, reason, verdict)).strip()}
    defaults = {
        "problem_estimand": story,
        "claimed_delta": story,
        "mechanism": "",
        "evaluation_expected_signal": "",
        "setting_task": (theme + " " + story).strip(),
        "entities_datasets_methods": " ".join(
            sorted(set(history_projection._tokens(theme + " " + story)))
        ),
    }
    for facet, text in supplied.items():
        if facet not in history_projection.FACETS or not isinstance(text, str):
            raise ValueError("query contains an unsupported facet")
        defaults[facet] = text
    return {facet: text for facet, text in defaults.items() if text}


def _candidate_row(conn, candidate_id):
    return conn.execute(
        """
        SELECT c.*, e.active
        FROM candidates c
        JOIN search_index_entries e ON e.candidate_id = c.candidate_id
        WHERE c.candidate_id = ? AND e.active = 1
        """,
        (candidate_id,),
    ).fetchone()


def _evidence(
    row,
    channel,
    facet,
    raw_score,
    rank,
    text,
    source_artifact_id=None,
):
    span = " ".join(str(text).split())[:EVIDENCE_SPAN_LIMIT]
    material = canonical_bytes(
        {
            "candidate_id": row["candidate_id"],
            "channel": channel,
            "facet": facet,
            "span": span,
            "source_sequence": row["source_sequence"],
        }
    )
    return {
        "candidate_id": row["candidate_id"],
        "lineage_id": row["lineage_id"],
        "facet": facet,
        "raw_score": round(float(raw_score), 12),
        "rank": int(rank),
        "source_artifact_id": source_artifact_id
        or "canonical-row:" + row["raw_sha256"],
        "source_location": "ledger.tsv#data-row=%d" % row["source_sequence"],
        "evidence_id": _sha(b"history-evidence-v1\0" + material),
        "evidence_span": span,
        "material_delta": span if facet == "claimed_delta" else "",
        "channel": channel,
    }


def _rank_best(values, depth):
    best = {}
    for value in values:
        prior = best.get(value["candidate_id"])
        key = (-value["_score"], value["candidate_id"], value["facet"])
        if prior is None or key < prior[0]:
            best[value["candidate_id"]] = (key, value)
    ranked = [
        item[1] for item in sorted(best.values(), key=lambda item: item[0])
    ][:depth]
    for rank, item in enumerate(ranked, 1):
        item["rank"] = rank
        item.pop("_score", None)
    return ranked


def _exact_channel(conn, query, intent, depth):
    rows = []
    if intent in ("duplicate_search", "evolution_search"):
        canonical = history_store.canonical_story_v1(
            _query_value(query, "story")
        )
        candidate_ids = [
            row[0]
            for row in conn.execute(
                """
                SELECT c.candidate_id FROM story_aliases a
                JOIN candidates c ON c.lineage_id = a.lineage_id
                JOIN search_index_entries e ON e.candidate_id = c.candidate_id
                WHERE a.canonical_version = ? AND a.canonical_story = ?
                  AND e.active = 1
                ORDER BY c.source_sequence DESC LIMIT ?
                """,
                (history_store.CANONICAL_VERSION, canonical, depth),
            )
        ]
        for rank, candidate_id in enumerate(candidate_ids, 1):
            row = _candidate_row(conn, candidate_id)
            if row is not None:
                rows.append(
                    _evidence(
                        row,
                        "exact",
                        "problem_estimand",
                        1.0,
                        rank,
                        row["story"],
                    )
                )
        return rows
    code = history_projection.failure_code(
        _query_value(query, "verdict"),
        _query_value(query, "category"),
        _query_value(query, "reason"),
    )
    for row in conn.execute(
        """
        SELECT c.*, e.active
        FROM candidates c
        JOIN search_index_entries e ON e.candidate_id = c.candidate_id
        WHERE e.active = 1 ORDER BY c.source_sequence DESC
        """
    ):
        candidate_code = history_projection.failure_code(
            row["verdict"], row["category"], row["reason"]
        )
        if candidate_code == code:
            rows.append(
                _evidence(row, "exact", "failure_code", 1.0, len(rows) + 1, code)
            )
            if len(rows) == depth:
                break
    return rows


def _fts_channel(conn, query, intent, depth):
    query_facets = _query_facets(query, intent)
    values = []
    if intent == "failure_pattern_search":
        terms = history_projection._tokens(query_facets["failure_pattern"])
        for row in conn.execute(
            """
            SELECT c.*, e.active
            FROM candidates c
            JOIN search_index_entries e ON e.candidate_id = c.candidate_id
            WHERE e.active = 1 ORDER BY c.candidate_id
            """
        ):
            text = " ".join((row["category"], row["reason"], row["verdict"]))
            candidate_terms = set(history_projection._tokens(text))
            score = sum(1 for term in terms if term in candidate_terms)
            if score:
                values.append(
                    dict(
                        _evidence(
                            row, "fts", "failure_pattern", score, 0, text
                        ),
                        _score=float(score),
                    )
                )
        return _rank_best(values, depth)
    for facet, text in sorted(query_facets.items()):
        terms = history_projection._tokens(text)
        if not terms:
            continue
        expression = " OR ".join(
            '"' + term.replace('"', "") + '"' for term in terms
        )
        for hit in conn.execute(
            """
            SELECT f.candidate_id, f.content, bm25(search_fts) AS bm25_score
            FROM search_fts f
            JOIN search_index_entries e ON e.candidate_id = f.candidate_id
            WHERE e.active = 1 AND f.facet = ? AND search_fts MATCH ?
            ORDER BY bm25_score, f.candidate_id LIMIT ?
            """,
            (facet, expression, depth),
        ):
            row = _candidate_row(conn, hit["candidate_id"])
            score = -float(hit["bm25_score"])
            values.append(
                dict(
                    _evidence(row, "fts", facet, score, 0, hit["content"]),
                    _score=score,
                )
            )
    return _rank_best(values, depth)


def _dense_channel(conn, query, intent, depth):
    query_facets = _query_facets(query, intent)
    values = []
    if intent == "failure_pattern_search":
        query_vector, query_norm = history_projection.embed(
            query_facets["failure_pattern"]
        )
        if not query_norm:
            return []
        for row in conn.execute(
            """
            SELECT c.*, e.active
            FROM candidates c
            JOIN search_index_entries e ON e.candidate_id = c.candidate_id
            WHERE e.active = 1 ORDER BY c.candidate_id
            """
        ):
            text = " ".join((row["category"], row["reason"], row["verdict"]))
            vector, norm = history_projection.embed(text)
            if not norm:
                continue
            score = history_projection._cosine(query_vector, vector)
            values.append(
                dict(
                    _evidence(
                        row, "dense", "failure_pattern", score, 0, text
                    ),
                    _score=score,
                )
            )
        return _rank_best(values, depth)
    for facet, text in sorted(query_facets.items()):
        query_vector, norm = history_projection.embed(text)
        if not norm:
            continue
        for hit in conn.execute(
            """
            SELECT v.candidate_id, v.vector, f.text, f.source_artifact_id
            FROM search_vectors v
            JOIN search_index_entries e ON e.candidate_id = v.candidate_id
            JOIN candidate_facets f
              ON f.candidate_id = v.candidate_id AND f.facet = v.facet
            WHERE e.active = 1 AND v.facet = ?
            """,
            (facet,),
        ):
            score = history_projection._cosine(
                query_vector, history_projection._unblob(hit["vector"])
            )
            row = _candidate_row(conn, hit["candidate_id"])
            values.append(
                dict(
                    _evidence(
                        row,
                        "dense",
                        facet,
                        score,
                        0,
                        hit["text"],
                        hit["source_artifact_id"],
                    ),
                    _score=score,
                )
            )
    return _rank_best(values, depth)


def _lineage_channel(conn, seed_results, depth):
    seed_ids = sorted({item["candidate_id"] for item in seed_results})
    if not seed_ids:
        return []
    placeholders = ",".join("?" for _ in seed_ids)
    rows = conn.execute(
        """
        SELECT DISTINCT related.*
        FROM candidates seed
        JOIN candidates related ON related.lineage_id = seed.lineage_id
        JOIN search_index_entries active ON active.candidate_id = related.candidate_id
        WHERE seed.candidate_id IN (%s) AND active.active = 1
        ORDER BY related.source_sequence DESC, related.candidate_id
        LIMIT ?
        """
        % placeholders,
        (*seed_ids, depth),
    ).fetchall()
    return [
        _evidence(row, "lineage", "lineage", 1.0, rank, row["story"])
        for rank, row in enumerate(rows, 1)
    ]


def _expansion_channel(conn, request, depth):
    if request is None:
        return []
    if not isinstance(request, dict) or set(request) != {"lineage_ids"}:
        raise ValueError("expansion request must name lineage IDs")
    lineage_ids = request["lineage_ids"]
    if not isinstance(lineage_ids, list) or not lineage_ids:
        raise ValueError("expansion request must name lineage IDs")
    if len(lineage_ids) > depth or any(not isinstance(item, str) for item in lineage_ids):
        raise ValueError("expansion request exceeds its bound")
    placeholders = ",".join("?" for _ in lineage_ids)
    rows = conn.execute(
        """
        SELECT c.*
        FROM candidates c
        JOIN search_index_entries e ON e.candidate_id = c.candidate_id
        WHERE c.lineage_id IN (%s) AND e.active = 1
        ORDER BY c.source_sequence DESC, c.candidate_id
        LIMIT ?
        """
        % placeholders,
        (*lineage_ids, depth),
    ).fetchall()
    return [
        _evidence(row, "expansion", "lineage", 1.0, rank, row["story"])
        for rank, row in enumerate(rows, 1)
    ]


def _empty_pack(
    query, intent, policy, status, generation=None, expansion_requested=False
):
    watermark = 0 if generation is None else generation["source_watermark"]
    generation_id = 0 if generation is None else generation["generation"]
    pack = {
        "schema_version": 1,
        "query": query,
        "intent": intent,
        "retrieval_policy_version": policy["retrieval_policy_version"],
        "source_watermark": watermark,
        "index_generation": generation_id,
        "projection": dict(policy["projection"]),
        "configured_depth": policy["per_channel_depth"],
        "comparator_cutoff": policy["comparator_cutoff"],
        "retrieval_status": status,
        "channels": {
            name: {
                "status": "failed",
                "failure_code": status,
                "result_count": 0,
                "omitted_result_count": 0,
                "results": [],
            }
            for name in ("exact", "fts", "dense", "lineage")
        },
        "lineages": [],
        "omitted_lineage_count": 0,
        "estimated_input_tokens": 0,
    }
    pack["channels"]["expansion"] = {
        "status": "failed" if expansion_requested else "not_applicable",
        "failure_code": status if expansion_requested else None,
        "result_count": 0,
        "omitted_result_count": 0,
        "results": [],
    }
    return _seal_pack(pack)


def _seal_pack(pack):
    value = dict(pack)
    value.pop("pack_sha256", None)
    value.pop("receipt_id", None)
    estimated = value.get("estimated_input_tokens", 0)
    while True:
        value["estimated_input_tokens"] = estimated
        digest = pack_sha256(value)
        sealed = dict(
            value,
            receipt_id=_sha(b"retrieval-pack-v1\0" + digest.encode("ascii")),
            pack_sha256=digest,
        )
        updated = int(math.ceil(len(canonical_bytes(sealed)) / 4.0))
        if updated == estimated:
            return sealed
        estimated = updated


def _fuse(channel_results, policy):
    scores = {}
    evidence = {}
    for channel in ("exact", "fts", "dense", "lineage", "expansion"):
        for item in channel_results.get(channel, []):
            candidate_id = item["candidate_id"]
            scores[candidate_id] = scores.get(candidate_id, 0.0) + 1.0 / (
                int(policy["rrf_k"]) + item["rank"]
            )
            evidence[(channel, candidate_id)] = item
    candidates = {}
    for item in evidence.values():
        candidates[item["candidate_id"]] = item["lineage_id"]
    lineages = {}
    for candidate_id, score in scores.items():
        lineage_id = candidates[candidate_id]
        lineage = lineages.setdefault(
            lineage_id, {"lineage_id": lineage_id, "rrf_score": 0.0, "matches": []}
        )
        lineage["rrf_score"] += score
    for item in evidence.values():
        lineages[item["lineage_id"]]["matches"].append(item)
    ranked = sorted(
        lineages.values(), key=lambda item: (-item["rrf_score"], item["lineage_id"])
    )
    for lineage_rank, lineage in enumerate(ranked, 1):
        lineage["rank"] = lineage_rank
        lineage["rrf_score"] = round(lineage["rrf_score"], 12)
        lineage["matches"].sort(
            key=lambda item: (
                item["channel"],
                item["rank"],
                item["candidate_id"],
                item["facet"],
            )
        )
    return ranked


def _bounded_channels(channels, lineages):
    retained = {item["lineage_id"] for item in lineages}
    result = {}
    for name, channel in channels.items():
        values = list(channel.get("results", []))
        bounded = [
            item for item in values if item.get("lineage_id") in retained
        ]
        result[name] = {
            key: value for key, value in channel.items() if key != "results"
        }
        result[name].update(
            {
                "result_count": len(values),
                "omitted_result_count": len(values) - len(bounded),
                "results": bounded,
            }
        )
    return result


def _validate_generation_snapshot(conn, policy):
    generation = conn.execute(
        "SELECT * FROM search_index_generations ORDER BY generation DESC LIMIT 1"
    ).fetchone()
    if generation is None:
        return {"valid": False, "code": "no_published_generation"}
    try:
        manifest = json.loads(generation["manifest_json"])
    except (TypeError, ValueError):
        return {"valid": False, "code": "invalid_manifest"}
    expected = history_projection._projection_manifest(
        conn, policy, generation["source_watermark"]
    )
    expected_bytes = history_projection._canonical_bytes(expected)
    projection = policy["projection"]
    valid = (
        generation["policy_sha256"] == history_projection._policy_sha256(policy)
        and generation["projection_schema_version"] == projection["schema_version"]
        and generation["fts_tokenizer"] == projection["fts_tokenizer"]
        and generation["vector_model"] == projection["vector_model"]
        and generation["vector_revision"] == projection["vector_revision"]
        and generation["preprocessing_version"]
        == projection["preprocessing_version"]
        and generation["dimensions"] == projection["dimensions"]
        and generation["metric"] == projection["metric"]
        and generation["manifest_sha256"] == _sha(expected_bytes)
        and manifest == expected
    )
    return {
        "valid": valid,
        "code": "ok" if valid else "manifest_mismatch",
        "generation": generation["generation"],
    }


def _build_pack_snapshot(
    conn,
    query,
    intent,
    policy,
    disabled_channels=None,
    expansion_request=None,
):
    if intent not in INTENTS:
        raise ValueError("unsupported retrieval intent")
    disabled = set(disabled_channels or ())
    supported = {"exact", "fts", "dense", "lineage", "expansion"}
    if disabled - supported:
        raise ValueError("unknown retrieval channel")
    validation = _validate_generation_snapshot(conn, policy)
    if (
        not validation["valid"]
        and policy.get("max_retrieval_tokens")
        != history_projection._POLICY_FIXED["max_retrieval_tokens"]
    ):
        projection_policy = dict(policy)
        projection_policy["max_retrieval_tokens"] = history_projection._POLICY_FIXED[
            "max_retrieval_tokens"
        ]
        validation = _validate_generation_snapshot(
            conn, projection_policy
        )
    generation = conn.execute(
        "SELECT * FROM search_index_generations ORDER BY generation DESC LIMIT 1"
    ).fetchone()
    if not validation["valid"] or generation is None:
        return _empty_pack(
            query,
            intent,
            policy,
            "backend_failed",
            generation,
            expansion_request is not None,
        )
    watermark = conn.execute(
        "SELECT COALESCE(MAX(source_sequence), 0) FROM candidates"
    ).fetchone()[0]
    if generation["source_watermark"] != watermark:
        return _empty_pack(
            query,
            intent,
            policy,
            "backend_failed",
            generation,
            expansion_request is not None,
        )
    depth = int(policy["per_channel_depth"])
    results = {}
    channels = {}
    mandatory_failed = False
    for channel in ("exact", "fts", "dense"):
        if channel in disabled:
            results[channel] = []
            channels[channel] = {"status": "failed", "results": []}
            mandatory_failed = True
            continue
        try:
            function = {
                "exact": _exact_channel,
                "fts": _fts_channel,
                "dense": _dense_channel,
            }[channel]
            results[channel] = function(conn, query, intent, depth)
            channels[channel] = {
                "status": "complete",
                "results": results[channel],
            }
        except Exception as exc:
            results[channel] = []
            channels[channel] = {
                "status": "failed",
                "failure_code": type(exc).__name__,
                "results": [],
            }
            mandatory_failed = True
    if "lineage" in disabled:
        results["lineage"] = []
        channels["lineage"] = {"status": "failed", "results": []}
        mandatory_failed = True
    else:
        try:
            seeds = results["exact"] + results["fts"] + results["dense"]
            results["lineage"] = _lineage_channel(conn, seeds, depth)
            channels["lineage"] = {
                "status": "complete",
                "results": results["lineage"],
            }
        except Exception as exc:
            results["lineage"] = []
            channels["lineage"] = {
                "status": "failed",
                "failure_code": type(exc).__name__,
                "results": [],
            }
            mandatory_failed = True
    if expansion_request is None:
        results["expansion"] = []
        channels["expansion"] = {"status": "not_applicable", "results": []}
    elif "expansion" in disabled:
        results["expansion"] = []
        channels["expansion"] = {"status": "failed", "results": []}
        mandatory_failed = True
    else:
        try:
            results["expansion"] = _expansion_channel(
                conn, expansion_request, depth
            )
            channels["expansion"] = {
                "status": "complete",
                "results": results["expansion"],
            }
        except Exception as exc:
            results["expansion"] = []
            channels["expansion"] = {
                "status": "failed",
                "failure_code": type(exc).__name__,
                "results": [],
            }
            mandatory_failed = True
    ranked = _fuse(results, policy)
    retained_count = int(policy["final_lineage_count"])
    retained = ranked[:retained_count]
    pack = {
        "schema_version": 1,
        "query": query,
        "intent": intent,
        "retrieval_policy_version": policy["retrieval_policy_version"],
        "source_watermark": generation["source_watermark"],
        "index_generation": generation["generation"],
        "projection": dict(policy["projection"]),
        "configured_depth": depth,
        "comparator_cutoff": int(policy["comparator_cutoff"]),
        "retrieval_status": "partial" if mandatory_failed else "complete",
        "channels": _bounded_channels(channels, retained),
        "lineages": retained,
        "omitted_lineage_count": max(0, len(ranked) - len(retained)),
        "estimated_input_tokens": 0,
    }
    sealed = _seal_pack(pack)
    if sealed["estimated_input_tokens"] <= int(policy["max_retrieval_tokens"]):
        return sealed
    cutoff = int(policy["comparator_cutoff"])
    reducible = [item for item in retained if item["rank"] > cutoff]
    while reducible:
        drop = reducible.pop()
        retained = [item for item in retained if item["lineage_id"] != drop["lineage_id"]]
        pack["lineages"] = retained
        pack["channels"] = _bounded_channels(channels, retained)
        pack["omitted_lineage_count"] += 1
        sealed = _seal_pack(pack)
        if sealed["estimated_input_tokens"] <= int(policy["max_retrieval_tokens"]):
            return sealed
    pack["retrieval_status"] = "budget_exceeded"
    pack["omitted_lineage_count"] = len(ranked)
    pack["lineages"] = []
    pack["channels"] = _bounded_channels(channels, [])
    return _seal_pack(pack)


def build_pack(
    conn,
    query,
    intent,
    policy,
    disabled_channels=None,
    expansion_request=None,
):
    started = not conn.in_transaction
    if started:
        history_projection._init(conn)
    if started:
        conn.execute("BEGIN")
    try:
        result = _build_pack_snapshot(
            conn,
            query,
            intent,
            policy,
            disabled_channels=disabled_channels,
            expansion_request=expansion_request,
        )
        if started:
            conn.execute("COMMIT")
        return result
    except Exception:
        if started and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _validate_pack(pack, policy):
    if not isinstance(pack, dict) or pack.get("schema_version") != 1:
        raise ComparisonValidationError("invalid retrieval pack")
    if pack.get("pack_sha256") != pack_sha256(pack):
        raise ComparisonValidationError("retrieval pack hash mismatch")
    if pack.get("retrieval_policy_version") != policy["retrieval_policy_version"]:
        raise ComparisonValidationError("retrieval policy mismatch")
    if pack.get("projection") != policy["projection"]:
        raise ComparisonValidationError("projection version mismatch")


def _evidence_index(pack):
    result = {}
    for lineage in pack.get("lineages", []):
        for match in lineage.get("matches", []):
            result[
                (
                    match.get("candidate_id"),
                    match.get("lineage_id"),
                    match.get("facet"),
                    match.get("evidence_id"),
                )
            ] = match
    return result


def _validate_relations(pack, relations):
    if not isinstance(relations, list):
        raise ComparisonValidationError("relations must be a list")
    evidence = _evidence_index(pack)
    required = {
        "relation",
        "candidate_id",
        "lineage_id",
        "facet",
        "evidence_id",
        "material_difference",
        "confidence",
    }
    semantic_relations = {
        "duplicate_search": {
            "same_core_idea",
            "same_lineage_revision",
            "related_component",
            "distinct",
            "uncertain",
        },
        "evolution_search": {
            "same_core_idea",
            "same_lineage_revision",
            "related_component",
            "distinct",
            "uncertain",
        },
        "failure_pattern_search": {
            "same_failure_mechanism",
            "related_failure_pattern",
            "distinct",
            "uncertain",
        },
    }
    allowed = semantic_relations.get(pack.get("intent"), set())
    for relation in relations:
        if not isinstance(relation, dict) or set(relation) != required:
            raise ComparisonValidationError("relation schema mismatch")
        if relation["relation"] not in RELATIONS or relation["relation"] not in allowed:
            raise ComparisonValidationError("unsupported relation")
        if (
            relation["candidate_id"],
            relation["lineage_id"],
            relation["facet"],
            relation["evidence_id"],
        ) not in evidence:
            raise ComparisonValidationError("relation references evidence outside pack")
        confidence = relation["confidence"]
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            raise ComparisonValidationError("confidence must be numeric")
        if not 0.0 <= float(confidence) <= 1.0:
            raise ComparisonValidationError("confidence is outside [0,1]")
        if not isinstance(relation["material_difference"], str):
            raise ComparisonValidationError("material difference must be text")


def _validate_response(pack, response):
    expected = {
        "status",
        "comparator_version",
        "relations",
        "expansion_request",
    }
    if not isinstance(response, dict) or set(response) != expected:
        raise ComparisonValidationError("comparison schema mismatch")
    if response["status"] not in FINAL_STATUSES:
        raise ComparisonValidationError("unsupported comparison status")
    if response["comparator_version"] != COMPARATOR_VERSION:
        raise ComparisonValidationError("unsupported comparator version")
    _validate_relations(pack, response["relations"])
    relations = {item["relation"] for item in response["relations"]}
    if response["status"] == "complete_match" and not (
        relations
        - {"distinct", "uncertain"}
    ):
        raise ComparisonValidationError("complete match lacks a material relation")
    if response["status"] == "complete_no_match" and (
        relations - {"distinct"}
    ):
        raise ComparisonValidationError("complete no-match has a material relation")
    if response["status"] == "uncertain" and "uncertain" not in relations:
        raise ComparisonValidationError("uncertain status lacks uncertain evidence")
    request = response["expansion_request"]
    if request is not None:
        if (
            not isinstance(request, dict)
            or set(request) != {"lineage_ids"}
            or not isinstance(request["lineage_ids"], list)
            or not request["lineage_ids"]
        ):
            raise ComparisonValidationError("invalid expansion request")
        pack_lineages = {item["lineage_id"] for item in pack["lineages"]}
        if not set(request["lineage_ids"]).issubset(pack_lineages):
            raise ComparisonValidationError("expansion request is outside pack")


def _receipt_material(receipt):
    value = dict(receipt)
    value.pop("receipt_id", None)
    return value


def finalize_comparison(conn, pack, response, policy):
    _validate_pack(pack, policy)
    if pack.get("retrieval_status") != "complete":
        raise ComparisonValidationError("only a complete pack may be compared")
    _validate_response(pack, response)
    response_hash = _sha(canonical_bytes(response))
    query = pack["query"]
    candidate_id = (
        query.get("candidate_id", "anonymous-query")
        if isinstance(query, dict)
        else "anonymous-query"
    )
    receipt = {
        "schema_version": 1,
        "query_candidate_id": candidate_id,
        "intent": pack["intent"],
        "pack_sha256": pack["pack_sha256"],
        "retrieval_policy_version": pack["retrieval_policy_version"],
        "source_watermark": pack["source_watermark"],
        "index_generation": pack["index_generation"],
        "comparator_version": response["comparator_version"],
        "comparison_sha256": response_hash,
        "status": response["status"],
        "relations": response["relations"],
        "expansion_request": response["expansion_request"],
    }
    receipt["receipt_id"] = _sha(
        b"history-receipt-v1\0" + canonical_bytes(receipt)
    )
    encoded = canonical_bytes(receipt).decode("utf-8").rstrip("\n")
    conn.execute("BEGIN IMMEDIATE")
    try:
        prior = conn.execute(
            "SELECT receipt_json FROM history_receipts WHERE receipt_id = ?",
            (receipt["receipt_id"],),
        ).fetchone()
        if prior is not None and prior[0] != encoded:
            raise ComparisonValidationError("receipt identity collision")
        conn.execute(
            """
            INSERT OR IGNORE INTO history_receipts(
              receipt_id, query_candidate_id, intent, pack_sha256,
              retrieval_policy_version, source_watermark, index_generation,
              comparator_version, status, receipt_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                receipt["receipt_id"],
                receipt["query_candidate_id"],
                receipt["intent"],
                receipt["pack_sha256"],
                receipt["retrieval_policy_version"],
                receipt["source_watermark"],
                receipt["index_generation"],
                receipt["comparator_version"],
                receipt["status"],
                encoded,
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return receipt


def replay_receipt(conn, pack, receipt, policy):
    try:
        _validate_pack(pack, policy)
        receipt_fields = {
            "schema_version",
            "query_candidate_id",
            "intent",
            "pack_sha256",
            "retrieval_policy_version",
            "source_watermark",
            "index_generation",
            "comparator_version",
            "comparison_sha256",
            "status",
            "relations",
            "expansion_request",
            "receipt_id",
        }
        if (
            not isinstance(receipt, dict)
            or set(receipt) != receipt_fields
            or receipt.get("schema_version") != 1
        ):
            raise ReceiptReplayError("invalid receipt")
        expected_id = _sha(
            b"history-receipt-v1\0" + canonical_bytes(_receipt_material(receipt))
        )
        if receipt.get("receipt_id") != expected_id:
            raise ReceiptReplayError("receipt hash mismatch")
        if receipt.get("pack_sha256") != pack["pack_sha256"]:
            raise ReceiptReplayError("pack hash mismatch")
        if receipt.get("retrieval_policy_version") != policy["retrieval_policy_version"]:
            raise ReceiptReplayError("retrieval policy mismatch")
        if receipt.get("source_watermark") != pack["source_watermark"]:
            raise ReceiptReplayError("source watermark mismatch")
        if receipt.get("index_generation") != pack["index_generation"]:
            raise ReceiptReplayError("index generation mismatch")
        if receipt.get("comparator_version") != COMPARATOR_VERSION:
            raise ReceiptReplayError("comparator version mismatch")
        if receipt.get("intent") != pack["intent"]:
            raise ReceiptReplayError("intent mismatch")
        _validate_relations(pack, receipt.get("relations"))
        reconstructed_response = {
            "status": receipt["status"],
            "comparator_version": receipt["comparator_version"],
            "relations": receipt["relations"],
            "expansion_request": receipt["expansion_request"],
        }
        if receipt.get("comparison_sha256") != _sha(
            canonical_bytes(reconstructed_response)
        ):
            raise ReceiptReplayError("comparison hash mismatch")
        generation = conn.execute(
            """
            SELECT * FROM search_index_generations WHERE generation = ?
            """,
            (receipt["index_generation"],),
        ).fetchone()
        try:
            manifest = None if generation is None else json.loads(
                generation["manifest_json"]
            )
        except (TypeError, ValueError):
            manifest = None
        projection = policy["projection"]
        generation_valid = (
            generation is not None
            and manifest is not None
            and generation["source_watermark"] == receipt["source_watermark"]
            and manifest.get("source_watermark") == receipt["source_watermark"]
            and generation["manifest_sha256"] == _sha(canonical_bytes(manifest))
            and generation["policy_sha256"]
            == history_projection._policy_sha256(policy)
            and generation["projection_schema_version"]
            == projection["schema_version"]
            and generation["fts_tokenizer"] == projection["fts_tokenizer"]
            and generation["vector_model"] == projection["vector_model"]
            and generation["vector_revision"] == projection["vector_revision"]
            and generation["preprocessing_version"]
            == projection["preprocessing_version"]
            and generation["dimensions"] == projection["dimensions"]
            and generation["metric"] == projection["metric"]
        )
        if not generation_valid:
            raise ReceiptReplayError("published generation drift")
        stored = conn.execute(
            "SELECT receipt_json FROM history_receipts WHERE receipt_id = ?",
            (receipt["receipt_id"],),
        ).fetchone()
        encoded = canonical_bytes(receipt).decode("utf-8").rstrip("\n")
        if stored is None or stored[0] != encoded:
            raise ReceiptReplayError("receipt is not durably recorded")
    except ComparisonValidationError as exc:
        raise ReceiptReplayError(str(exc)) from exc
    return {"valid": True, "receipt_id": receipt["receipt_id"]}


def permits_permanent_conclusion(receipt):
    return (
        isinstance(receipt, dict)
        and receipt.get("status") in PERMANENT_STATUSES
    )
