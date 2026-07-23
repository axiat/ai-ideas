#!/usr/bin/env python3
"""Deterministic bounded retrieval and host-owned history receipts."""

import hashlib
import json
import weakref

try:
    from lib import history_budget
    from lib import history_projection
    from lib import history_store
except ImportError:  # Direct execution through lib/history_cli.py.
    import history_budget
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
COMPARATOR_STATUSES = {
    "complete_match",
    "complete_no_match",
    "uncertain",
    "conflicting_evidence",
}
PERMANENT_STATUSES = {"complete_match", "complete_no_match"}
COMPARATOR_VERSION = "history-comparator-v1"
EVIDENCE_SPAN_LIMIT = 48
QUERY_TEXT_LIMIT = 4096
QUERY_BYTES_LIMIT = 16384
QUERY_FIELDS = {
    "candidate_id", "story", "theme", "verdict", "reason", "category", "facets"
}
COMPARATOR_OUTPUT_SCHEMA = {
    "type": "object",
    "required": [
        "status", "comparator_version", "relations", "expansion_request"
    ],
}


class RetrievalError(RuntimeError):
    pass


class ComparisonValidationError(RetrievalError):
    pass


class ReceiptReplayError(RetrievalError):
    pass


def _verified_receipt_capability():
    fields = (
        "valid",
        "verified",
        "receipt_id",
        "status",
        "pack_publication_id",
    )
    registry = weakref.WeakKeyDictionary()

    class VerifiedReceipt:
        """Opaque host capability returned only after durable receipt replay."""

        __slots__ = ("__weakref__",)

        def __new__(cls, *args, **kwargs):
            raise TypeError("verified receipts are host-constructed")

        def __getitem__(self, key):
            decision = registry.get(self)
            if decision is None:
                raise TypeError("unsealed verified receipt")
            if isinstance(key, str):
                try:
                    key = fields.index(key)
                except ValueError as exc:
                    raise KeyError(key) from exc
            return decision[key]

        def get(self, key, default=None):
            try:
                return self[key]
            except KeyError:
                return default

        def keys(self):
            return fields

        def items(self):
            return tuple((field, self[field]) for field in fields)

        def values(self):
            return tuple(self[field] for field in fields)

    def issue(value):
        if not isinstance(value, dict) or set(value) != set(fields):
            raise TypeError("verified receipt fields are invalid")
        capability = object.__new__(VerifiedReceipt)
        registry[capability] = tuple(value[field] for field in fields)
        return capability

    def decision(capability):
        if type(capability) is not VerifiedReceipt:
            return None
        return registry.get(capability)

    return VerifiedReceipt, issue, decision


VerifiedReceipt, _issue_verified_receipt, _verified_receipt_decision = (
    _verified_receipt_capability()
)


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
    value.pop("pack_publication_id", None)
    return _sha(canonical_bytes(value))


def _normalize_query(query):
    if not isinstance(query, dict) or set(query) - QUERY_FIELDS:
        raise ValueError("query schema is not closed")
    if not isinstance(query.get("candidate_id"), str) or not query["candidate_id"]:
        raise ValueError("query candidate ID is required")
    if not isinstance(query.get("story"), str) or not query["story"].strip():
        raise ValueError("query story is required")
    normalized = {}
    for field in QUERY_FIELDS - {"facets"}:
        value = query.get(field, "")
        if not isinstance(value, str) or len(value.encode("utf-8")) > QUERY_TEXT_LIMIT:
            raise ValueError("query text field exceeds its bound")
        if value or field in {"candidate_id", "story"}:
            normalized[field] = value
    facets = query.get("facets", {})
    if not isinstance(facets, dict) or set(facets) - set(history_projection.FACETS):
        raise ValueError("query facets are not closed")
    normalized_facets = {}
    for facet, text in sorted(facets.items()):
        if not isinstance(text, str) or len(text.encode("utf-8")) > QUERY_TEXT_LIMIT:
            raise ValueError("query facet exceeds its bound")
        normalized_facets[facet] = text
    if normalized_facets:
        normalized["facets"] = normalized_facets
    if len(canonical_bytes(normalized)) > QUERY_BYTES_LIMIT:
        raise ValueError("query exceeds its aggregate bound")
    return normalized


def _query_value(query, name, default=""):
    value = query.get(name, default)
    if not isinstance(value, str):
        raise TypeError("query fields must be text")
    return value


def _query_facets(query, intent):
    story = _query_value(query, "story")
    theme = _query_value(query, "theme")
    supplied = query.get("facets", {})
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
    relevant = {
        "duplicate_search": {"problem_estimand", "claimed_delta"},
        "evolution_search": {"problem_estimand", "claimed_delta", "mechanism"},
    }[intent]
    return {
        facet: text
        for facet, text in defaults.items()
        if text and facet in relevant
    }


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
    resolved_artifact_id = (
        source_artifact_id or "canonical-row:" + row["raw_sha256"]
    )
    material = canonical_bytes(
        {
            "candidate_id": row["candidate_id"],
            "channel": channel,
            "facet": facet,
            "span": span,
            "source_sequence": row["source_sequence"],
            "source_artifact_id": resolved_artifact_id,
        }
    )
    return {
        "candidate_id": row["candidate_id"],
        "lineage_id": row["lineage_id"],
        "facet": facet,
        "raw_score": round(float(raw_score), 12),
        "rank": int(rank),
        "source_artifact_id": resolved_artifact_id,
        "source_location": "ledger.tsv#data-row=%d" % row["source_sequence"],
        "evidence_id": _sha(b"history-evidence-v1\0" + material),
        "evidence_span": span,
        "material_delta": "",
        "channel": channel,
    }


def _bounded_material_delta(parent_story, child_story):
    parent = str(parent_story).split()
    child = str(child_story).split()
    prefix = 0
    while (
        prefix < len(parent)
        and prefix < len(child)
        and parent[prefix] == child[prefix]
    ):
        prefix += 1
    suffix = 0
    while (
        suffix < len(parent) - prefix
        and suffix < len(child) - prefix
        and parent[-suffix - 1] == child[-suffix - 1]
    ):
        suffix += 1
    parent_end = len(parent) - suffix if suffix else len(parent)
    child_end = len(child) - suffix if suffix else len(child)
    parent_change = " ".join(parent[prefix:parent_end]) or "<none>"
    child_change = " ".join(child[prefix:child_end]) or "<none>"
    labels = "parent=", ";child="
    available = EVIDENCE_SPAN_LIMIT - len(labels[0]) - len(labels[1])
    parent_budget = min(len(parent_change), max(1, available // 2))
    child_budget = min(len(child_change), max(1, available - parent_budget))
    remaining = available - parent_budget - child_budget
    if remaining:
        parent_extra = min(remaining, len(parent_change) - parent_budget)
        parent_budget += parent_extra
        child_budget += min(
            remaining - parent_extra, len(child_change) - child_budget
        )
    return (
        labels[0]
        + parent_change[:parent_budget]
        + labels[1]
        + child_change[:child_budget]
    )


def _rank_facet(values, depth):
    ranked = sorted(
        values,
        key=lambda item: (-item["_score"], item["candidate_id"]),
    )[:depth]
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
    for facet, text in sorted(query_facets.items()):
        facet_values = []
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
            facet_values.append(
                dict(
                    _evidence(row, "fts", facet, score, 0, hit["content"]),
                    _score=score,
                )
            )
        values.extend(_rank_facet(facet_values, depth))
    return values


def _dense_channel(conn, query, intent, depth):
    query_facets = _query_facets(query, intent)
    values = []
    for facet, text in sorted(query_facets.items()):
        facet_values = []
        query_vector, norm = history_projection.embed(text)
        if not norm:
            continue
        for hit in conn.execute(
            """
            SELECT v.candidate_id, v.vector, v.content,
                   v.source_artifact_id
            FROM search_vectors v
            JOIN search_index_entries e ON e.candidate_id = v.candidate_id
            WHERE e.active = 1 AND v.facet = ?
            """,
            (facet,),
        ):
            score = history_projection._cosine(
                query_vector, history_projection._unblob(hit["vector"])
            )
            row = _candidate_row(conn, hit["candidate_id"])
            facet_values.append(
                dict(
                    _evidence(
                        row,
                        "dense",
                        facet,
                        score,
                        0,
                        hit["content"],
                        hit["source_artifact_id"],
                    ),
                    _score=score,
                )
            )
        values.extend(_rank_facet(facet_values, depth))
    return values


def _lineage_channel(conn, seed_ids, depth):
    seed_ids = list(dict.fromkeys(seed_ids))
    if not seed_ids:
        return []
    placeholders = ",".join("?" for _ in seed_ids)
    lineages = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT lineage_id FROM candidates "
            "WHERE candidate_id IN (%s) ORDER BY lineage_id" % placeholders,
            tuple(seed_ids),
        )
    ]
    results = []
    for lineage_id in lineages:
        current = conn.execute(
            """
            SELECT c.* FROM candidates c
            JOIN search_index_entries e ON e.candidate_id = c.candidate_id
            WHERE c.lineage_id = ? AND e.active = 1
            ORDER BY c.source_sequence DESC, c.candidate_id LIMIT 1
            """,
            (lineage_id,),
        ).fetchone()
        highest = next(
            (
                row
                for candidate_id in seed_ids
                for row in [_candidate_row(conn, candidate_id)]
                if row is not None and row["lineage_id"] == lineage_id
            ),
            None,
        )
        edges = conn.execute(
            """
            SELECT edge.*, parent.story AS parent_story, child.story AS child_story
            FROM lineage_edges edge
            JOIN candidates parent ON parent.candidate_id = edge.parent_candidate_id
            JOIN candidates child ON child.candidate_id = edge.child_candidate_id
            WHERE parent.lineage_id = ?
            ORDER BY parent.source_sequence, child.source_sequence,
                     edge.relation_type
            """,
            (lineage_id,),
        ).fetchall()
        if highest is None or current is None or not edges:
            continue
        highest_id = highest["candidate_id"]
        current_id = current["candidate_id"]
        if highest_id == current_id:
            incoming = [
                edge for edge in edges
                if edge["child_candidate_id"] == current_id
            ]
            incident = incoming or [
                edge for edge in edges
                if edge["parent_candidate_id"] == current_id
            ]
            descriptors = (
                []
                if not incident
                else [(incident[-1], current_id, "highest_match+current")]
            )
        else:
            adjacency = {}
            for edge in edges:
                adjacency.setdefault(edge["parent_candidate_id"], []).append(
                    (edge["child_candidate_id"], edge)
                )
                adjacency.setdefault(edge["child_candidate_id"], []).append(
                    (edge["parent_candidate_id"], edge)
                )
            queue = [(highest_id, [])]
            visited = {highest_id}
            path = None
            while queue:
                node, traversed = queue.pop(0)
                if node == current_id:
                    path = traversed
                    break
                for neighbor, edge in adjacency.get(node, []):
                    if neighbor in visited:
                        continue
                    visited.add(neighbor)
                    queue.append(
                        (neighbor, traversed + [(edge, node, neighbor)])
                    )
            if path is None:
                descriptors = []
            elif len(path) == 1:
                edge, _, _ = path[0]
                descriptors = [
                    (edge, highest_id, "highest_match"),
                    (edge, current_id, "current"),
                ]
            else:
                descriptors = []
                for index, (edge, _, destination_id) in enumerate(path):
                    if index == 0:
                        descriptors.append(
                            (edge, highest_id, "highest_match")
                        )
                    elif index == len(path) - 1:
                        descriptors.append((edge, current_id, "current"))
                    else:
                        descriptors.append((edge, destination_id, "path"))
        if len(results) + len(descriptors) > depth:
            raise RetrievalError("lineage path exceeds channel depth")
        for edge, candidate_id, role in descriptors:
            row = _candidate_row(conn, candidate_id)
            material_delta = _bounded_material_delta(
                edge["parent_story"], edge["child_story"]
            )
            # material_delta is the bounded extractive span for typed
            # edges; avoid duplicating it in the generic evidence field.
            evidence = _evidence(
                row,
                "lineage",
                "lineage",
                1.0,
                len(results) + 1,
                "",
                edge["evidence_artifact_id"],
            )
            evidence.update(
                {
                    "relation_type": edge["relation_type"],
                    "edge_evidence_artifact_id": edge["evidence_artifact_id"],
                    "version_role": role,
                    "parent_candidate_id": edge["parent_candidate_id"],
                    "child_candidate_id": edge["child_candidate_id"],
                    "edge_direction": (
                        "parent"
                        if candidate_id == edge["parent_candidate_id"]
                        else "child"
                    ),
                    "material_delta": material_delta,
                }
            )
            results.append(evidence)
    return results


def _expansion_channel(conn, request, depth):
    if request is None:
        return []
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
    manifest_sha256 = "0" * 64 if generation is None else generation["manifest_sha256"]
    pack = {
        "schema_version": 1,
        "query": query,
        "intent": intent,
        "retrieval_policy_version": policy["retrieval_policy_version"],
        "policy_sha256": history_projection._policy_sha256(policy),
        "source_watermark": watermark,
        "index_generation": generation_id,
        "generation_manifest_sha256": manifest_sha256,
        "projection": dict(policy["projection"]),
        "configured_depth": policy["per_channel_depth"],
        "comparator_cutoff": policy["comparator_cutoff"],
        "hard_limits": {
            key: policy[key]
            for key in (
                "max_matches",
                "max_retrieval_tokens",
                "max_expansion_rounds",
                "model_context_limit",
                "max_output_tokens",
                "safety_margin",
                "adapter_wrapper_allowance",
            )
        },
        "channel_matrix": {
            "mandatory": list(policy["mandatory_channels"]),
            "expansion": "conditional",
        },
        "expansion_round": 0,
        "prior_pack_publication_id": None,
        "prior_comparison_receipt_id": None,
        "retrieval_status": status,
        "channels": {
            name: {
                "status": "failed",
                "failure_code": status,
                "result_count": 0,
                "retained_result_count": 0,
            }
            for name in ("exact", "fts", "dense", "lineage")
        },
        "lineages": [],
        "rank_contributions": [],
        "omitted_lineage_count": 0,
        "estimated_input_tokens": 0,
    }
    pack["channels"]["expansion"] = {
        "status": "failed" if expansion_requested else "not_applicable",
        "failure_code": status if expansion_requested else None,
        "result_count": 0,
        "retained_result_count": 0,
    }
    return _seal_pack(pack)


def _budget_exceeded_audit_pack(
    query, intent, policy, generation, channels, ranked, expansion_request
):
    pack = _empty_pack(
        query,
        intent,
        policy,
        "budget_exceeded",
        generation,
        expansion_request is not None,
    )
    pack["channels"] = _bounded_channels(channels, [])
    pack["omitted_lineage_count"] = len(ranked)
    if expansion_request is not None:
        pack["expansion_round"] = expansion_request["round"]
        pack["prior_pack_publication_id"] = expansion_request[
            "prior_pack_publication_id"
        ]
        pack["prior_comparison_receipt_id"] = expansion_request[
            "comparison_receipt_id"
        ]
    return _seal_pack(pack)


def _seal_pack(pack):
    value = dict(pack)
    value.pop("pack_sha256", None)
    value.pop("receipt_id", None)
    value.pop("pack_publication_id", None)
    estimated = value.get("estimated_input_tokens", 0)
    while True:
        value["estimated_input_tokens"] = estimated
        digest = pack_sha256(value)
        sealed = dict(
            value,
            receipt_id=_sha(
                b"retrieval-pack-v1\0" + digest.encode("ascii")
            ),
            pack_publication_id=_sha(
                b"history-pack-publication-v1\0"
                + digest.encode("ascii")
                + value["policy_sha256"].encode("ascii")
                + value["generation_manifest_sha256"].encode("ascii")
            ),
            pack_sha256=digest,
        )
        updated = (
            len(canonical_bytes(sealed))
            + int(value["hard_limits"]["adapter_wrapper_allowance"])
        )
        if updated == estimated:
            return sealed
        estimated = updated


def _fuse(channel_results, policy):
    scores = {}
    evidence = {}
    contributions = []
    for channel in ("exact", "fts", "dense", "lineage", "expansion"):
        unique = {}
        for item in channel_results.get(channel, []):
            key = (item["facet"], item["candidate_id"])
            prior = unique.get(key)
            if prior is None or (
                item["rank"],
                item.get("evidence_id", ""),
            ) < (
                prior["rank"],
                prior.get("evidence_id", ""),
            ):
                unique[key] = item
        for item in sorted(
            unique.values(),
            key=lambda value: (
                value["facet"],
                value["rank"],
                value["candidate_id"],
                value.get("evidence_id", ""),
            ),
        ):
            candidate_id = item["candidate_id"]
            contribution = 1.0 / (
                int(policy["rrf_k"]) + item["rank"]
            )
            scores[candidate_id] = scores.get(candidate_id, 0.0) + contribution
            evidence[(channel, item["facet"], candidate_id)] = item
            contributions.append(
                {
                    "channel": channel,
                    "facet": item["facet"],
                    "candidate_id": candidate_id,
                    "rank": item["rank"],
                    "rrf_score": round(contribution, 12),
                }
            )
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
        channel_priority = {
            "exact": 0,
            "fts": 1,
            "dense": 2,
            "lineage": 3,
            "expansion": 4,
        }
        lineage["matches"].sort(
            key=lambda item: (
                channel_priority[item["channel"]],
                item["rank"],
                item["candidate_id"],
                item["facet"],
            )
        )
    contributions.sort(
        key=lambda item: (
            item["channel"], item["facet"], item["rank"], item["candidate_id"]
        )
    )
    grouped = []
    for candidate_id in sorted({item["candidate_id"] for item in contributions}):
        grouped.append(
            {
                "candidate_id": candidate_id,
                "ranks": [
                    {
                        key: item[key]
                        for key in ("channel", "facet", "rank", "rrf_score")
                    }
                    for item in contributions
                    if item["candidate_id"] == candidate_id
                ],
            }
        )
    return ranked, grouped


def _fusion_summary(ranked, contributions):
    candidate_lineages = {
        match["candidate_id"]: lineage["lineage_id"]
        for lineage in ranked
        for match in lineage["matches"]
    }
    candidates = []
    for contribution in contributions:
        candidates.append(
            {
                "candidate_id": contribution["candidate_id"],
                "lineage_id": candidate_lineages[contribution["candidate_id"]],
                "rrf_score": round(
                    sum(item["rrf_score"] for item in contribution["ranks"]),
                    12,
                ),
            }
        )
    candidates.sort(
        key=lambda item: (-item["rrf_score"], item["candidate_id"])
    )
    return {
        "candidate_order": candidates,
        "lineage_order": [
            {
                "lineage_id": lineage["lineage_id"],
                "rank": lineage["rank"],
                "rrf_score": lineage["rrf_score"],
                "candidate_ids": [
                    item["candidate_id"]
                    for item in candidates
                    if item["lineage_id"] == lineage["lineage_id"]
                ],
            }
            for lineage in ranked
        ],
    }


def _bounded_channels(channels, lineages):
    retained = {item["lineage_id"] for item in lineages}
    retained_evidence = {
        match["evidence_id"]
        for lineage in lineages
        for match in lineage["matches"]
    }
    result = {}
    for name, channel in channels.items():
        values = list(channel.get("results", []))
        bounded = [
            item
            for item in values
            if item.get("lineage_id") in retained
            and item.get("evidence_id") in retained_evidence
        ]
        result[name] = {
            key: value for key, value in channel.items() if key != "results"
        }
        result[name].update(
            {
                "result_count": len(values),
                "retained_result_count": len(bounded),
            }
        )
    return result


def _evolution_unit(lineage):
    lineage_matches = sorted(
        [
            match
            for match in lineage["matches"]
            if match.get("channel") == "lineage"
        ],
        key=lambda item: (
            item["rank"],
            item["candidate_id"],
            item.get("evidence_id", ""),
        ),
    )
    if not lineage_matches:
        facet_priority = {
            "mechanism": 0,
            "claimed_delta": 1,
            "problem_estimand": 2,
        }
        return sorted(
            lineage["matches"],
            key=lambda item: (
                facet_priority.get(item.get("facet"), 3),
                item["rank"],
                item["channel"],
                item["candidate_id"],
            ),
        )[:1]
    highest_index = next(
        (
            index
            for index, item in enumerate(lineage_matches)
            if "highest_match" in item.get("version_role", "").split("+")
        ),
        None,
    )
    current_index = next(
        (
            index
            for index, item in enumerate(lineage_matches)
            if "current" in item.get("version_role", "").split("+")
        ),
        None,
    )
    if (
        highest_index is None
        or current_index is None
        or highest_index > current_index
    ):
        raise RetrievalError(
            "evolution lineage lacks a complete highest/current unit"
        )
    return lineage_matches[highest_index:current_index + 1]


def _cap_matches(lineages, limit, intent="duplicate_search"):
    if intent == "evolution_search":
        result = [
            dict(lineage, matches=_evolution_unit(lineage))
            for lineage in lineages
            if lineage["matches"]
        ]
        if sum(len(lineage["matches"]) for lineage in result) > limit:
            raise RetrievalError(
                "complete lineage evidence units exceed match bound"
            )
        return result
    result = [
        dict(lineage, matches=[])
        for lineage in lineages
    ]
    remaining = [list(lineage["matches"]) for lineage in lineages]
    selected = 0
    while selected < limit and any(remaining):
        for index, values in enumerate(remaining):
            if selected >= limit:
                break
            if values:
                result[index]["matches"].append(values.pop(0))
                selected += 1
    return [lineage for lineage in result if lineage["matches"]]


def _validate_runtime_policy(policy):
    if (
        not isinstance(policy, dict)
        or set(policy) != history_projection._POLICY_KEYS
        or any(
            policy.get(key) != value
            for key, value in history_projection._POLICY_FIXED.items()
        )
        or policy.get("mandatory_channels")
        != history_projection._POLICY_CHANNELS
        or policy.get("projection") != history_projection._POLICY_PROJECTION
        or policy.get("tested_adapter_allowances")
        != {"history-stage-v1": 256}
    ):
        raise RetrievalError("retrieval policy is not the sealed v1 policy")


def _validate_expansion_request(conn, request, query, intent, policy):
    if request is None:
        return None
    fields = {
        "lineage_ids",
        "round",
        "prior_pack_publication_id",
        "comparison_receipt_id",
    }
    if not isinstance(request, dict) or set(request) != fields:
        raise RetrievalError("expansion provenance is incomplete")
    round_number = request["round"]
    if (
        type(round_number) is not int
        or round_number < 1
        or round_number > int(policy["max_expansion_rounds"])
    ):
        raise RetrievalError("expansion round exceeds the sealed policy")
    publication = conn.execute(
        "SELECT * FROM history_pack_publications WHERE publication_id = ?",
        (request["prior_pack_publication_id"],),
    ).fetchone()
    receipt_row = conn.execute(
        "SELECT receipt_json FROM history_receipts WHERE receipt_id = ?",
        (request["comparison_receipt_id"],),
    ).fetchone()
    if publication is None or receipt_row is None:
        raise RetrievalError("expansion provenance is not host-published")
    try:
        prior_pack = json.loads(bytes(publication["pack_bytes"]).decode("utf-8"))
        receipt = json.loads(receipt_row[0])
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise RetrievalError("expansion provenance is corrupt") from exc
    try:
        verified = replay_receipt(conn, prior_pack, receipt, policy)
    except ReceiptReplayError as exc:
        raise RetrievalError("expansion receipt replay failed") from exc
    lineage_ids = request["lineage_ids"]
    allowed = {item["lineage_id"] for item in prior_pack["lineages"]}
    if (
        not isinstance(lineage_ids, list)
        or not lineage_ids
        or any(not isinstance(item, str) for item in lineage_ids)
        or not set(lineage_ids).issubset(allowed)
        or verified.get("verified") is not True
        or receipt.get("receipt_id") != request["comparison_receipt_id"]
        or receipt.get("status") != "uncertain"
        or receipt.get("pack_publication_id")
        != request["prior_pack_publication_id"]
        or receipt.get("expansion_request") != {"lineage_ids": lineage_ids}
        or prior_pack.get("query") != query
        or prior_pack.get("intent") != intent
        or round_number != prior_pack.get("expansion_round", -1) + 1
    ):
        raise RetrievalError("expansion is not bound to a validated outcome")
    return dict(request)


def _validate_generation_snapshot(conn, policy):
    generation = conn.execute(
        "SELECT * FROM search_index_generations ORDER BY generation DESC LIMIT 1"
    ).fetchone()
    if generation is None:
        return {"valid": False, "code": "no_published_generation"}
    current_revision = int(
        conn.execute(
            "SELECT value FROM schema_meta "
            "WHERE key = 'history_search_content_revision'"
        ).fetchone()[0]
    )
    if generation["canonical_revision"] < current_revision:
        return {
            "valid": False,
            "code": "projection_revision_pending",
            "generation": generation["generation"],
        }
    try:
        manifest = json.loads(generation["manifest_json"])
    except (TypeError, ValueError):
        return {"valid": False, "code": "invalid_manifest"}
    expected = history_projection._projection_manifest(
        conn,
        policy,
        generation["source_watermark"],
        generation["canonical_revision"],
    )
    expected_bytes = history_projection._canonical_bytes(expected)
    projection = policy["projection"]
    valid = (
        generation["canonical_revision"] == current_revision
        and generation["policy_sha256"] == history_projection._policy_sha256(policy)
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
    trace_sink=None,
):
    if trace_sink is None:
        trace_sink = {}
    if intent not in INTENTS:
        raise ValueError("unsupported retrieval intent")
    disabled = set(disabled_channels or ())
    supported = {"exact", "fts", "dense", "lineage", "expansion"}
    if disabled - supported:
        raise ValueError("unknown retrieval channel")
    validation = _validate_generation_snapshot(conn, policy)
    generation = conn.execute(
        "SELECT * FROM search_index_generations ORDER BY generation DESC LIMIT 1"
    ).fetchone()
    if not validation["valid"] or generation is None:
        status = (
            "partial"
            if validation["code"] == "projection_revision_pending"
            else "backend_failed"
        )
        return _empty_pack(
            query,
            intent,
            policy,
            status,
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
    if "lineage" in disabled:
        results["lineage"] = []
        channels["lineage"] = {"status": "failed", "results": []}
        mandatory_failed = True
    else:
        try:
            seed_results = dict(results, lineage=[])
            seed_ranked, seed_contributions = _fuse(
                seed_results, policy
            )
            seed_order = [
                item["candidate_id"]
                for item in _fusion_summary(
                    seed_ranked, seed_contributions
                )["candidate_order"]
            ]
            results["lineage"] = _lineage_channel(
                conn, seed_order, depth
            )
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
    ranked, contributions = _fuse(results, policy)
    trace_sink.update(
        {
            "contributions": [
                dict(rank, candidate_id=contribution["candidate_id"])
                for contribution in contributions
                for rank in contribution["ranks"]
            ],
            "channels": {
                name: list(values) for name, values in results.items()
            },
            "fusion": _fusion_summary(ranked, contributions),
        }
    )
    if intent == "evolution_search":
        for lineage in ranked:
            lineage["matches"].sort(
                key=lambda item: (
                    0 if item["channel"] == "lineage" else 1,
                    item["rank"],
                    item["channel"],
                    item["candidate_id"],
                )
            )
    retained_count = int(policy["final_lineage_count"])
    try:
        retained = _cap_matches(
            ranked[:retained_count],
            int(policy["max_matches"]),
            intent=intent,
        )
    except RetrievalError:
        return _budget_exceeded_audit_pack(
            query,
            intent,
            policy,
            generation,
            channels,
            ranked,
            expansion_request,
        )
    retained_candidates = {
        match["candidate_id"]
        for lineage in retained
        for match in lineage["matches"]
    }
    highest_candidates = (
        {match["candidate_id"] for match in retained[0]["matches"]}
        if retained
        else set()
    )
    retained_contributions = [
        item for item in contributions
        if item["candidate_id"] in highest_candidates
    ]
    if intent == "evolution_search":
        retained_contributions = []
    pack = {
        "schema_version": 1,
        "query": query,
        "intent": intent,
        "retrieval_policy_version": policy["retrieval_policy_version"],
        "policy_sha256": history_projection._policy_sha256(policy),
        "source_watermark": generation["source_watermark"],
        "index_generation": generation["generation"],
        "generation_manifest_sha256": generation["manifest_sha256"],
        "projection": dict(policy["projection"]),
        "configured_depth": depth,
        "comparator_cutoff": int(policy["comparator_cutoff"]),
        "hard_limits": {
            key: policy[key]
            for key in (
                "max_matches",
                "max_retrieval_tokens",
                "max_expansion_rounds",
                "model_context_limit",
                "max_output_tokens",
                "safety_margin",
                "adapter_wrapper_allowance",
            )
        },
        "channel_matrix": {
            "mandatory": list(policy["mandatory_channels"]),
            "expansion": "conditional",
        },
        "expansion_round": (
            0 if expansion_request is None else expansion_request["round"]
        ),
        "prior_pack_publication_id": (
            None
            if expansion_request is None
            else expansion_request["prior_pack_publication_id"]
        ),
        "prior_comparison_receipt_id": (
            None
            if expansion_request is None
            else expansion_request["comparison_receipt_id"]
        ),
        "retrieval_status": "partial" if mandatory_failed else "complete",
        "channels": _bounded_channels(channels, retained),
        "lineages": retained,
        "rank_contributions": retained_contributions,
        "omitted_lineage_count": max(0, len(ranked) - len(retained)),
        "estimated_input_tokens": 0,
    }
    sealed = _seal_pack(pack)
    while sealed["estimated_input_tokens"] > int(policy["max_retrieval_tokens"]):
        removable = next(
            (
                lineage
                for lineage in reversed(retained)
                if len(lineage["matches"])
                > max(
                    1,
                    sum(
                        match["channel"] == "lineage"
                        for match in lineage["matches"]
                    ),
                )
            ),
            None,
        )
        if removable is None:
            break
        removable["matches"].pop()
        pack["lineages"] = retained
        pack["channels"] = _bounded_channels(channels, retained)
        highest_candidates = (
            {
                match["candidate_id"]
                for match in retained[0]["matches"]
            }
            if retained
            else set()
        )
        pack["rank_contributions"] = (
            []
            if intent == "evolution_search"
            else [
                item
                for item in contributions
                if item["candidate_id"] in highest_candidates
            ]
        )
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
        highest_candidates = (
            {match["candidate_id"] for match in retained[0]["matches"]}
            if retained
            else set()
        )
        pack["rank_contributions"] = (
            []
            if intent == "evolution_search"
            else [
                item for item in contributions
                if item["candidate_id"] in highest_candidates
            ]
        )
        pack["omitted_lineage_count"] += 1
        sealed = _seal_pack(pack)
        if sealed["estimated_input_tokens"] <= int(policy["max_retrieval_tokens"]):
            return sealed
    pack["retrieval_status"] = "budget_exceeded"
    pack["omitted_lineage_count"] = len(ranked)
    pack["lineages"] = []
    pack["channels"] = _bounded_channels(channels, [])
    pack["rank_contributions"] = []
    return _seal_pack(pack)


def _validated_comparator_role(role_bytes, role_identity):
    if not isinstance(role_bytes, bytes) or not role_bytes:
        raise RetrievalError("comparator role bytes are required")
    try:
        role_text = role_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RetrievalError("comparator role is not UTF-8") from exc
    if (
        not isinstance(role_identity, str)
        or not role_identity
        or "\n" in role_identity
        or "\r" in role_identity
    ):
        raise RetrievalError("comparator role identity is invalid")
    return role_text


def comparator_invocation_bytes(
    pack, policy, *, role_bytes, role_identity
):
    """Return the canonical serialized invocation consumed by the comparator."""
    role_text = _validated_comparator_role(role_bytes, role_identity)
    pack_bytes = canonical_bytes(pack)
    return history_budget.serialize_stage_invocation(
        stage="history-compare",
        adapter_version=policy["adapter_version"],
        fixed_instructions=role_text,
        mounted_inputs={"retrieval_pack.json": pack_bytes},
        candidate=pack["query"],
        retrieval_payload=pack,
        receipts=[{
            "pack_publication_id": pack["pack_publication_id"],
            "role_identity": role_identity,
            "role_sha256": _sha(role_bytes),
        }],
        tool_schemas=[COMPARATOR_OUTPUT_SCHEMA],
        messages=[{"role": "user", "content": "Compare the candidate."}],
    )


def _comparator_preflight(
    pack, policy, *, role_bytes, role_identity
):
    pack_bytes = canonical_bytes(pack)
    invocation = comparator_invocation_bytes(
        pack,
        policy,
        role_bytes=role_bytes,
        role_identity=role_identity,
    )
    return history_budget.preflight_stage_invocation(
        invocation,
        policy,
        expected_mounted_inputs={"retrieval_pack.json": pack_bytes},
    )

def _publish_pack(
    conn, pack, policy, preflight, rank_trace, invocation_bytes
):
    if pack["index_generation"] < 1:
        return
    encoded = canonical_bytes(pack)
    if not rank_trace:
        rank_trace = {
            "contributions": [],
            "channels": {name: [] for name in pack["channels"]},
            "fusion": {"candidate_order": [], "lineage_order": []},
        }
    rank_trace_bytes = canonical_bytes(rank_trace)
    preflight_bytes = canonical_bytes(preflight)
    conn.execute(
        """
        INSERT INTO history_pack_publications(
          publication_id, pack_sha256, pack_bytes, policy_sha256, generation,
          generation_manifest_sha256, source_watermark, retrieval_status,
          rank_trace_json, rank_trace_sha256, comparator_invocation_json,
          comparator_invocation_sha256, comparator_preflight_json,
          comparator_preflight_sha256, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                 datetime('now'))
        ON CONFLICT(publication_id) DO NOTHING
        """,
        (
            pack["pack_publication_id"],
            pack["pack_sha256"],
            encoded,
            history_projection._policy_sha256(policy),
            pack["index_generation"],
            pack["generation_manifest_sha256"],
            pack["source_watermark"],
            pack["retrieval_status"],
            rank_trace_bytes.decode("utf-8"),
            _sha(rank_trace_bytes),
            invocation_bytes.decode("utf-8"),
            _sha(invocation_bytes),
            preflight_bytes.decode("utf-8"),
            _sha(preflight_bytes),
        ),
    )
    stored = conn.execute(
        """
        SELECT pack_bytes, rank_trace_json, rank_trace_sha256,
               comparator_invocation_json, comparator_invocation_sha256,
               comparator_preflight_json, comparator_preflight_sha256
        FROM history_pack_publications
        WHERE publication_id = ?
        """,
        (pack["pack_publication_id"],),
    ).fetchone()
    if (
        stored is None
        or bytes(stored["pack_bytes"]) != encoded
        or stored["rank_trace_json"].encode("utf-8") != rank_trace_bytes
        or stored["rank_trace_sha256"] != _sha(rank_trace_bytes)
        or stored["comparator_invocation_json"].encode("utf-8")
        != invocation_bytes
        or stored["comparator_invocation_sha256"] != _sha(invocation_bytes)
        or stored["comparator_preflight_json"].encode("utf-8")
        != preflight_bytes
        or stored["comparator_preflight_sha256"] != _sha(preflight_bytes)
    ):
        raise RetrievalError("pack publication identity collision")


def build_pack(
    conn,
    query,
    intent,
    policy,
    disabled_channels=None,
    expansion_request=None,
    *,
    comparator_role_bytes=None,
    comparator_role_identity=None,
):
    _validate_runtime_policy(policy)
    _validated_comparator_role(
        comparator_role_bytes, comparator_role_identity
    )
    normalized_query = _normalize_query(query)
    normalized_expansion = _validate_expansion_request(
        conn, expansion_request, normalized_query, intent, policy
    )
    started = not conn.in_transaction
    if started:
        history_projection._init(conn)
    if started:
        conn.execute("BEGIN")
    try:
        rank_trace = {}
        result = _build_pack_snapshot(
            conn,
            normalized_query,
            intent,
            policy,
            disabled_channels=disabled_channels,
            expansion_request=normalized_expansion,
            trace_sink=rank_trace,
        )
        if started:
            conn.execute("COMMIT")
        if result["retrieval_status"] == "complete":
            try:
                preflight = _comparator_preflight(
                    result,
                    policy,
                    role_bytes=comparator_role_bytes,
                    role_identity=comparator_role_identity,
                )
            except history_budget.PreflightError as exc:
                result = dict(
                    result,
                    retrieval_status="budget_exceeded",
                    lineages=[],
                    rank_contributions=[],
                    channels=_bounded_channels(
                        {
                            key: value
                            for key, value in result["channels"].items()
                        },
                        [],
                    ),
                )
                result = _seal_pack(result)
                preflight = exc.receipt
        else:
            preflight = {
                "fits": False,
                "code": result["retrieval_status"],
            }
        invocation_bytes = comparator_invocation_bytes(
            result,
            policy,
            role_bytes=comparator_role_bytes,
            role_identity=comparator_role_identity,
        )
        _publish_pack(
            conn,
            result,
            policy,
            preflight,
            rank_trace,
            invocation_bytes,
        )
        return result
    except Exception:
        if started and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _validate_trace_evidence(conn, channel, item):
    base_fields = {
        "candidate_id",
        "lineage_id",
        "facet",
        "raw_score",
        "rank",
        "source_artifact_id",
        "source_location",
        "evidence_id",
        "evidence_span",
        "material_delta",
        "channel",
    }
    expected_fields = (
        base_fields
        | {
            "relation_type",
            "edge_evidence_artifact_id",
            "version_role",
            "parent_candidate_id",
            "child_candidate_id",
            "edge_direction",
        }
        if channel == "lineage"
        else base_fields
    )
    allowed_facets = {
        "exact": {"problem_estimand", "failure_code"},
        "fts": set(history_projection.SEARCH_FACETS),
        "dense": set(history_projection.SEARCH_FACETS),
        "lineage": {"lineage"},
        "expansion": {"lineage"},
    }
    if (
        not isinstance(item, dict)
        or set(item) != expected_fields
        or item.get("channel") != channel
        or item.get("facet") not in allowed_facets[channel]
        or any(
            not isinstance(item.get(field), str) or not item[field]
            for field in (
                "candidate_id",
                "lineage_id",
                "source_artifact_id",
                "source_location",
                "evidence_id",
            )
        )
        or type(item.get("rank")) is not int
        or item["rank"] < 1
        or not isinstance(item.get("raw_score"), (int, float))
        or isinstance(item.get("raw_score"), bool)
        or not isinstance(item.get("evidence_span"), str)
        or len(item["evidence_span"]) > EVIDENCE_SPAN_LIMIT
        or not isinstance(item.get("material_delta"), str)
    ):
        raise ComparisonValidationError("published channel-result schema mismatch")
    candidate = conn.execute(
        "SELECT * FROM candidates WHERE candidate_id = ?",
        (item["candidate_id"],),
    ).fetchone()
    if (
        candidate is None
        or candidate["lineage_id"] != item["lineage_id"]
        or item["source_location"]
        != "ledger.tsv#data-row=%d" % candidate["source_sequence"]
    ):
        raise ComparisonValidationError("published channel-result identity mismatch")
    expected_evidence = _sha(
        b"history-evidence-v1\0"
        + canonical_bytes(
            {
                "candidate_id": candidate["candidate_id"],
                "channel": channel,
                "facet": item["facet"],
                "span": item["evidence_span"],
                "source_sequence": candidate["source_sequence"],
                "source_artifact_id": item["source_artifact_id"],
            }
        )
    )
    if item["evidence_id"] != expected_evidence:
        raise ComparisonValidationError("published evidence identity mismatch")
    if channel != "lineage":
        return
    if (
        item["source_artifact_id"] != item["edge_evidence_artifact_id"]
        or not item["version_role"]
        or item["edge_direction"] not in {"parent", "child"}
        or item["candidate_id"]
        != item[item["edge_direction"] + "_candidate_id"]
    ):
        raise ComparisonValidationError("published typed-edge identity mismatch")
    edges = conn.execute(
        """
        SELECT edge.*, parent.story AS parent_story, child.story AS child_story
        FROM lineage_edges edge
        JOIN candidates parent
          ON parent.candidate_id = edge.parent_candidate_id
        JOIN candidates child
          ON child.candidate_id = edge.child_candidate_id
        WHERE edge.evidence_artifact_id = ? AND edge.relation_type = ?
          AND edge.parent_candidate_id = ?
          AND edge.child_candidate_id = ?
        """,
        (
            item["edge_evidence_artifact_id"],
            item["relation_type"],
            item["parent_candidate_id"],
            item["child_candidate_id"],
        ),
    ).fetchall()
    if not any(
        item["material_delta"]
        == _bounded_material_delta(edge["parent_story"], edge["child_story"])
        and item["evidence_span"] == ""
        for edge in edges
    ):
        raise ComparisonValidationError("published typed-edge delta mismatch")


def _validate_published_rank_trace(conn, publication, pack, policy):
    try:
        trace_bytes = publication["rank_trace_json"].encode("utf-8")
        trace = json.loads(trace_bytes)
    except (AttributeError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ComparisonValidationError("published rank trace is corrupt") from exc
    if (
        trace_bytes != canonical_bytes(trace)
        or publication["rank_trace_sha256"] != _sha(trace_bytes)
        or not isinstance(trace, dict)
        or set(trace) != {"channels", "contributions", "fusion"}
        or not isinstance(trace["channels"], dict)
        or set(trace["channels"]) != set(pack["channels"])
        or not isinstance(trace["contributions"], list)
    ):
        raise ComparisonValidationError("published rank trace hash mismatch")
    for name, values in trace["channels"].items():
        if (
            not isinstance(values, list)
            or len(values) != pack["channels"][name]["result_count"]
        ):
            raise ComparisonValidationError("published rank trace count mismatch")
        for item in values:
            _validate_trace_evidence(conn, name, item)
    for contribution in trace["contributions"]:
        if (
            not isinstance(contribution, dict)
            or set(contribution)
            != {"candidate_id", "channel", "facet", "rank", "rrf_score"}
        ):
            raise ComparisonValidationError("published rank trace schema mismatch")
    ranked, contributions = _fuse(trace["channels"], policy)
    expected_contributions = [
        dict(rank, candidate_id=contribution["candidate_id"])
        for contribution in contributions
        for rank in contribution["ranks"]
    ]
    if trace["contributions"] != expected_contributions:
        raise ComparisonValidationError("published fusion contributions mismatch")
    if trace["fusion"] != _fusion_summary(ranked, contributions):
        raise ComparisonValidationError("published fusion ordering mismatch")
    expected_lineage_ids = [
        lineage["lineage_id"] for lineage in ranked[:len(pack["lineages"])]
    ]
    if [lineage["lineage_id"] for lineage in pack["lineages"]] != expected_lineage_ids:
        raise ComparisonValidationError("published fused lineage order mismatch")
    ranked_by_lineage = {
        lineage["lineage_id"]: lineage for lineage in ranked
    }
    for lineage in pack["lineages"]:
        expected = ranked_by_lineage[lineage["lineage_id"]]
        if (
            lineage["rank"] != expected["rank"]
            or lineage["rrf_score"] != expected["rrf_score"]
        ):
            raise ComparisonValidationError("published fused lineage score mismatch")
        expected_matches = (
            _evolution_unit(expected)
            if pack["intent"] == "evolution_search"
            else list(expected["matches"])
        )
        actual_keys = [
            (item["channel"], item["facet"], item["candidate_id"], item["evidence_id"])
            for item in lineage["matches"]
        ]
        expected_keys = [
            (item["channel"], item["facet"], item["candidate_id"], item["evidence_id"])
            for item in expected_matches[:len(actual_keys)]
        ]
        if actual_keys != expected_keys:
            raise ComparisonValidationError("published fused candidate order mismatch")
    highest_candidates = (
        {
            match["candidate_id"]
            for match in pack["lineages"][0]["matches"]
        }
        if pack["lineages"]
        else set()
    )
    expected_bounded = (
        []
        if pack["intent"] == "evolution_search"
        else [
            contribution
            for contribution in contributions
            if contribution["candidate_id"] in highest_candidates
        ]
    )
    if pack["rank_contributions"] != expected_bounded:
        raise ComparisonValidationError("published bounded fusion mismatch")
    bounded = [
        dict(rank, candidate_id=contribution["candidate_id"])
        for contribution in pack["rank_contributions"]
        for rank in contribution["ranks"]
    ]
    if any(item not in trace["contributions"] for item in bounded):
        raise ComparisonValidationError("bounded rank trace is not reproducible")


def _validate_pack(conn, pack, policy, require_complete=False):
    _validate_runtime_policy(policy)
    fields = {
        "schema_version",
        "query",
        "intent",
        "retrieval_policy_version",
        "policy_sha256",
        "source_watermark",
        "index_generation",
        "generation_manifest_sha256",
        "projection",
        "configured_depth",
        "comparator_cutoff",
        "hard_limits",
        "channel_matrix",
        "expansion_round",
        "prior_pack_publication_id",
        "prior_comparison_receipt_id",
        "retrieval_status",
        "channels",
        "lineages",
        "rank_contributions",
        "omitted_lineage_count",
        "estimated_input_tokens",
        "receipt_id",
        "pack_publication_id",
        "pack_sha256",
    }
    if (
        not isinstance(pack, dict)
        or set(pack) != fields
        or pack.get("schema_version") != 1
    ):
        raise ComparisonValidationError("invalid retrieval pack")
    try:
        if _normalize_query(pack["query"]) != pack["query"]:
            raise ComparisonValidationError("query is not canonical")
    except (TypeError, ValueError) as exc:
        raise ComparisonValidationError("query schema mismatch") from exc
    if pack.get("intent") not in INTENTS:
        raise ComparisonValidationError("retrieval intent mismatch")
    if pack.get("pack_sha256") != pack_sha256(pack):
        raise ComparisonValidationError("retrieval pack hash mismatch")
    expected_receipt_id = _sha(
        b"retrieval-pack-v1\0" + pack["pack_sha256"].encode("ascii")
    )
    expected_publication_id = _sha(
        b"history-pack-publication-v1\0"
        + pack["pack_sha256"].encode("ascii")
        + pack["policy_sha256"].encode("ascii")
        + pack["generation_manifest_sha256"].encode("ascii")
    )
    if (
        pack.get("receipt_id") != expected_receipt_id
        or pack.get("pack_publication_id") != expected_publication_id
    ):
        raise ComparisonValidationError("derived pack identity mismatch")
    policy_sha256 = history_projection._policy_sha256(policy)
    if (
        pack.get("retrieval_policy_version") != policy["retrieval_policy_version"]
        or pack.get("policy_sha256") != policy_sha256
    ):
        raise ComparisonValidationError("retrieval policy mismatch")
    if pack.get("projection") != policy["projection"]:
        raise ComparisonValidationError("projection version mismatch")
    hard_limits = {
        key: policy[key]
        for key in (
            "max_matches",
            "max_retrieval_tokens",
            "max_expansion_rounds",
            "model_context_limit",
            "max_output_tokens",
            "safety_margin",
            "adapter_wrapper_allowance",
        )
    }
    if (
        pack.get("hard_limits") != hard_limits
        or pack.get("configured_depth") != policy["per_channel_depth"]
        or pack.get("comparator_cutoff") != policy["comparator_cutoff"]
        or pack.get("channel_matrix")
        != {
            "mandatory": list(policy["mandatory_channels"]),
            "expansion": "conditional",
        }
    ):
        raise ComparisonValidationError("hard retrieval contract mismatch")
    expected_estimate = (
        len(canonical_bytes(pack)) + policy["adapter_wrapper_allowance"]
    )
    if (
        pack.get("estimated_input_tokens") != expected_estimate
        or expected_estimate > policy["max_retrieval_tokens"]
    ):
        raise ComparisonValidationError("pack byte bound mismatch")
    if set(pack.get("channels", {})) != {
        "exact", "fts", "dense", "lineage", "expansion"
    }:
        raise ComparisonValidationError("channel matrix mismatch")
    for channel in pack["channels"].values():
        allowed_channel_fields = {
            "status", "failure_code", "result_count", "retained_result_count"
        }
        if (
            not isinstance(channel, dict)
            or set(channel) - allowed_channel_fields
            or channel.get("status")
            not in {"complete", "failed", "not_applicable"}
            or type(channel.get("result_count")) is not int
            or type(channel.get("retained_result_count")) is not int
        ):
            raise ComparisonValidationError("channel schema mismatch")
    if require_complete and pack.get("retrieval_status") != "complete":
        raise ComparisonValidationError("only a complete pack may be compared")
    if pack.get("retrieval_status") == "complete":
        for channel in policy["mandatory_channels"]:
            if pack["channels"][channel].get("status") != "complete":
                raise ComparisonValidationError("mandatory channel is incomplete")
        expected_expansion = (
            "not_applicable" if pack["expansion_round"] == 0 else "complete"
        )
        if pack["channels"]["expansion"].get("status") != expected_expansion:
            raise ComparisonValidationError("expansion channel state mismatch")
    matches = [
        match for lineage in pack.get("lineages", [])
        for match in lineage.get("matches", [])
    ]
    if len(matches) > policy["max_matches"]:
        raise ComparisonValidationError("match bound exceeded")
    lineage_fields = {"lineage_id", "rrf_score", "matches", "rank"}
    match_fields = {
        "candidate_id",
        "lineage_id",
        "facet",
        "raw_score",
        "rank",
        "source_artifact_id",
        "source_location",
        "evidence_id",
        "evidence_span",
        "material_delta",
        "channel",
    }
    lineage_match_fields = match_fields | {
        "relation_type",
        "edge_evidence_artifact_id",
        "version_role",
        "parent_candidate_id",
        "child_candidate_id",
        "edge_direction",
    }
    for lineage in pack.get("lineages", []):
        if not isinstance(lineage, dict) or set(lineage) != lineage_fields:
            raise ComparisonValidationError("lineage schema mismatch")
        for match in lineage["matches"]:
            expected = (
                lineage_match_fields
                if match.get("channel") == "lineage"
                else match_fields
            )
            if (
                not isinstance(match, dict)
                or set(match) != expected
                or match.get("lineage_id") != lineage["lineage_id"]
            ):
                raise ComparisonValidationError("evidence schema mismatch")
    for contribution in pack.get("rank_contributions", []):
        if (
            not isinstance(contribution, dict)
            or set(contribution) != {"candidate_id", "ranks"}
            or not isinstance(contribution["ranks"], list)
        ):
            raise ComparisonValidationError("rank trace schema mismatch")
        for rank in contribution["ranks"]:
            if set(rank) != {"channel", "facet", "rank", "rrf_score"}:
                raise ComparisonValidationError("rank contribution schema mismatch")
    publication = conn.execute(
        "SELECT * FROM history_pack_publications WHERE publication_id = ?",
        (pack["pack_publication_id"],),
    ).fetchone()
    encoded = canonical_bytes(pack)
    if (
        publication is None
        or bytes(publication["pack_bytes"]) != encoded
        or publication["pack_sha256"] != pack["pack_sha256"]
        or publication["policy_sha256"] != policy_sha256
        or publication["generation"] != pack["index_generation"]
        or publication["generation_manifest_sha256"]
        != pack["generation_manifest_sha256"]
        or publication["source_watermark"] != pack["source_watermark"]
        or publication["retrieval_status"] != pack["retrieval_status"]
    ):
        raise ComparisonValidationError("pack is not host-published")
    _validate_published_rank_trace(conn, publication, pack, policy)
    provenance = conn.execute(
        "SELECT * FROM history_generation_provenance WHERE generation = ?",
        (pack["index_generation"],),
    ).fetchone()
    if (
        provenance is None
        or provenance["manifest_sha256"] != pack["generation_manifest_sha256"]
        or provenance["source_watermark"] != pack["source_watermark"]
        or provenance["policy_sha256"] != policy_sha256
        or provenance["projection_schema_version"]
        != policy["projection"]["schema_version"]
    ):
        raise ComparisonValidationError("generation provenance mismatch")
    try:
        manifest = json.loads(provenance["manifest_json"])
    except (TypeError, ValueError) as exc:
        raise ComparisonValidationError("generation manifest is corrupt") from exc
    manifest_candidates = {
        item["candidate_id"] for item in manifest.get("entries", [])
        if item.get("active") == 1
    }
    manifest_fts = {
        (item["candidate_id"], item["facet"])
        for item in manifest.get("fts", [])
    }
    manifest_vectors = {
        (item["candidate_id"], item["facet"])
        for item in manifest.get("vectors", [])
    }
    manifest_edges = {
        (
            item["parent_candidate_id"],
            item["child_candidate_id"],
            item["relation_type"],
            item["evidence_artifact_id"],
        )
        for item in manifest.get("lineage_edges", [])
    }
    for match in matches:
        if match.get("candidate_id") not in manifest_candidates:
            raise ComparisonValidationError("evidence is outside generation")
        candidate = conn.execute(
            "SELECT * FROM candidates WHERE candidate_id = ?",
            (match["candidate_id"],),
        ).fetchone()
        if candidate is None or candidate["lineage_id"] != match.get("lineage_id"):
            raise ComparisonValidationError("evidence candidate binding mismatch")
        expected_evidence = _sha(
            b"history-evidence-v1\0"
            + canonical_bytes(
                {
                    "candidate_id": candidate["candidate_id"],
                    "channel": match["channel"],
                    "facet": match["facet"],
                    "span": match["evidence_span"],
                    "source_sequence": candidate["source_sequence"],
                    "source_artifact_id": match["source_artifact_id"],
                }
            )
        )
        if match["evidence_id"] != expected_evidence:
            raise ComparisonValidationError("evidence identity mismatch")
        if (
            match["channel"] == "fts"
            and (match["candidate_id"], match["facet"]) not in manifest_fts
        ):
            raise ComparisonValidationError("FTS evidence is outside generation")
        if (
            match["channel"] == "dense"
            and (match["candidate_id"], match["facet"]) not in manifest_vectors
        ):
            raise ComparisonValidationError("dense evidence is outside generation")
        if match.get("channel") == "lineage":
            edge_identity = (
                match["parent_candidate_id"],
                match["child_candidate_id"],
                match["relation_type"],
                match["edge_evidence_artifact_id"],
            )
            if edge_identity not in manifest_edges:
                raise ComparisonValidationError(
                    "typed lineage evidence is outside generation"
                )
            edge = conn.execute(
                """
                SELECT 1 FROM lineage_edges
                WHERE evidence_artifact_id = ? AND relation_type = ?
                  AND parent_candidate_id = ?
                  AND child_candidate_id = ?
                """,
                (
                    match.get("edge_evidence_artifact_id"),
                    match.get("relation_type"),
                    match["parent_candidate_id"],
                    match["child_candidate_id"],
                ),
            ).fetchone()
            if edge is None:
                raise ComparisonValidationError("typed lineage evidence mismatch")
    try:
        invocation_bytes = publication[
            "comparator_invocation_json"
        ].encode("utf-8")
        invocation = json.loads(invocation_bytes)
        invocation_receipts = invocation["receipts"]
        role_identity = invocation_receipts[0]["role_identity"]
        role_bytes = invocation["fixed_instructions"].encode("utf-8")
    except (
        AttributeError,
        IndexError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise ComparisonValidationError(
            "comparator invocation is corrupt"
        ) from exc
    if (
        invocation_bytes != canonical_bytes(invocation)
        or publication["comparator_invocation_sha256"]
        != _sha(invocation_bytes)
        or len(invocation_receipts) != 1
        or invocation_bytes
        != comparator_invocation_bytes(
            pack,
            policy,
            role_bytes=role_bytes,
            role_identity=role_identity,
        )
    ):
        raise ComparisonValidationError(
            "comparator invocation identity mismatch"
        )
    try:
        preflight_bytes = publication["comparator_preflight_json"].encode("utf-8")
        preflight = json.loads(preflight_bytes)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ComparisonValidationError("comparator preflight is corrupt") from exc
    expected_preflight_fields = {
        "adapter_version",
        "code",
        "count_method",
        "fits",
        "input_upper_bound",
        "model_context_limit",
        "output_tokens",
        "safety_margin",
        "serialized_byte_count",
        "serialized_sha256",
        "input_sha256s",
        "total_upper_bound",
    }
    if (
        preflight_bytes != canonical_bytes(preflight)
        or publication["comparator_preflight_sha256"] != _sha(preflight_bytes)
        or set(preflight) != expected_preflight_fields
        or preflight["adapter_version"] != policy["adapter_version"]
        or preflight["code"] != "ok"
        or preflight["fits"] is not True
        or preflight["count_method"] != "utf8_byte_upper_bound"
        or preflight["input_upper_bound"]
        != preflight["serialized_byte_count"]
        + policy["adapter_wrapper_allowance"]
        or preflight["model_context_limit"] != policy["model_context_limit"]
        or preflight["output_tokens"] != policy["max_output_tokens"]
        or preflight["safety_margin"] != policy["safety_margin"]
        or preflight["total_upper_bound"]
        != preflight["input_upper_bound"]
        + preflight["output_tokens"]
        + preflight["safety_margin"]
        or preflight["total_upper_bound"] > policy["model_context_limit"]
        or not isinstance(preflight["serialized_sha256"], str)
        or len(preflight["serialized_sha256"]) != 64
        or pack["pack_sha256"] not in preflight["input_sha256s"]
        or _sha(canonical_bytes(pack)) not in preflight["input_sha256s"]
        or preflight["serialized_sha256"] != _sha(invocation_bytes)
    ):
        raise ComparisonValidationError("comparator preflight identity mismatch")
    if pack["retrieval_status"] == "complete" and not preflight.get("fits"):
        raise ComparisonValidationError("comparator preflight did not pass")


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
    classified_lineages = []
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
        classified_lineages.append(relation["lineage_id"])
    retained_lineages = [
        lineage["lineage_id"] for lineage in pack.get("lineages", [])
    ]
    if (
        len(classified_lineages) != len(retained_lineages)
        or len(set(classified_lineages)) != len(classified_lineages)
        or set(classified_lineages) != set(retained_lineages)
    ):
        raise ComparisonValidationError(
            "lineage classification coverage mismatch"
        )


def _validate_response(pack, response):
    expected = {
        "status",
        "comparator_version",
        "relations",
        "expansion_request",
    }
    if not isinstance(response, dict) or set(response) != expected:
        raise ComparisonValidationError("comparison schema mismatch")
    if response["status"] not in COMPARATOR_STATUSES:
        raise ComparisonValidationError("unsupported comparison status")
    if response["comparator_version"] != COMPARATOR_VERSION:
        raise ComparisonValidationError("unsupported comparator version")
    _validate_relations(pack, response["relations"])
    relations = {item["relation"] for item in response["relations"]}
    if response["status"] == "complete_match" and "uncertain" in relations:
        raise ComparisonValidationError(
            "complete match contains uncertain classification"
        )
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
        if response["status"] != "uncertain":
            raise ComparisonValidationError(
                "expansion requires uncertain status"
            )
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
    _validate_pack(conn, pack, policy, require_complete=True)
    _validate_response(pack, response)
    publication = conn.execute(
        """
        SELECT rank_trace_sha256, comparator_invocation_sha256,
               comparator_preflight_sha256
        FROM history_pack_publications
        WHERE publication_id = ?
        """,
        (pack["pack_publication_id"],),
    ).fetchone()
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
        "pack_publication_id": pack["pack_publication_id"],
        "policy_sha256": pack["policy_sha256"],
        "generation_manifest_sha256": pack["generation_manifest_sha256"],
        "rank_trace_sha256": publication["rank_trace_sha256"],
        "comparator_invocation_sha256":
            publication["comparator_invocation_sha256"],
        "comparator_preflight_sha256":
            publication["comparator_preflight_sha256"],
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
              pack_publication_id, policy_sha256, generation_manifest_sha256,
              rank_trace_sha256, comparator_invocation_sha256,
              comparator_preflight_sha256,
              retrieval_policy_version, source_watermark, index_generation,
              comparator_version, status, receipt_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                     datetime('now'))
            """,
            (
                receipt["receipt_id"],
                receipt["query_candidate_id"],
                receipt["intent"],
                receipt["pack_sha256"],
                receipt["pack_publication_id"],
                receipt["policy_sha256"],
                receipt["generation_manifest_sha256"],
                receipt["rank_trace_sha256"],
                receipt["comparator_invocation_sha256"],
                receipt["comparator_preflight_sha256"],
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
        _validate_pack(conn, pack, policy, require_complete=True)
        receipt_fields = {
            "schema_version",
            "query_candidate_id",
            "intent",
            "pack_sha256",
            "pack_publication_id",
            "policy_sha256",
            "generation_manifest_sha256",
            "rank_trace_sha256",
            "comparator_invocation_sha256",
            "comparator_preflight_sha256",
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
        if (
            receipt.get("pack_publication_id") != pack["pack_publication_id"]
            or receipt.get("policy_sha256") != pack["policy_sha256"]
            or receipt.get("generation_manifest_sha256")
            != pack["generation_manifest_sha256"]
        ):
            raise ReceiptReplayError("pack provenance mismatch")
        publication = conn.execute(
            """
            SELECT rank_trace_sha256, comparator_invocation_sha256,
                   comparator_preflight_sha256
            FROM history_pack_publications
            WHERE publication_id = ?
            """,
            (pack["pack_publication_id"],),
        ).fetchone()
        if (
            publication is None
            or receipt.get("rank_trace_sha256")
            != publication["rank_trace_sha256"]
            or receipt.get("comparator_invocation_sha256")
            != publication["comparator_invocation_sha256"]
            or receipt.get("comparator_preflight_sha256")
            != publication["comparator_preflight_sha256"]
        ):
            raise ReceiptReplayError("retrieval audit provenance mismatch")
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
        if receipt.get("query_candidate_id") != pack["query"]["candidate_id"]:
            raise ReceiptReplayError("query candidate mismatch")
        _validate_relations(pack, receipt.get("relations"))
        reconstructed_response = {
            "status": receipt["status"],
            "comparator_version": receipt["comparator_version"],
            "relations": receipt["relations"],
            "expansion_request": receipt["expansion_request"],
        }
        _validate_response(pack, reconstructed_response)
        if receipt.get("comparison_sha256") != _sha(
            canonical_bytes(reconstructed_response)
        ):
            raise ReceiptReplayError("comparison hash mismatch")
        generation = conn.execute(
            "SELECT * FROM history_generation_provenance WHERE generation = ?",
            (receipt["index_generation"],),
        ).fetchone()
        try:
            manifest = None if generation is None else json.loads(generation["manifest_json"])
        except (TypeError, ValueError):
            manifest = None
        generation_valid = (
            generation is not None
            and manifest is not None
            and generation["source_watermark"] == receipt["source_watermark"]
            and manifest.get("source_watermark") == receipt["source_watermark"]
            and generation["manifest_sha256"] == _sha(canonical_bytes(manifest))
            and generation["policy_sha256"]
            == history_projection._policy_sha256(policy)
            and generation["projection_schema_version"]
            == policy["projection"]["schema_version"]
            and generation["manifest_sha256"]
            == receipt["generation_manifest_sha256"]
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
    return _issue_verified_receipt(
        {
            "valid": True,
            "verified": True,
            "receipt_id": receipt["receipt_id"],
            "status": receipt["status"],
            "pack_publication_id": receipt["pack_publication_id"],
        }
    )


def permits_permanent_conclusion(conn, receipt):
    decision = _verified_receipt_decision(receipt)
    if decision is None:
        return False
    valid, verified, receipt_id, status, publication_id = decision
    if not (
        verified is True
        and valid is True
        and isinstance(receipt_id, str)
        and isinstance(publication_id, str)
        and status in PERMANENT_STATUSES
    ):
        return False
    stored = conn.execute(
        """
        SELECT r.receipt_json, r.status, r.pack_publication_id
        FROM history_receipts r
        JOIN history_pack_publications p
         ON p.publication_id = r.pack_publication_id
         AND p.pack_sha256 = r.pack_sha256
         AND p.rank_trace_sha256 = r.rank_trace_sha256
         AND p.comparator_invocation_sha256 =
             r.comparator_invocation_sha256
         AND p.comparator_preflight_sha256 =
             r.comparator_preflight_sha256
        WHERE r.receipt_id = ?
        """,
        (receipt_id,),
    ).fetchone()
    if (
        stored is None
        or stored["status"] != status
        or stored["pack_publication_id"] != publication_id
    ):
        return False
    try:
        durable_receipt = json.loads(stored["receipt_json"])
    except (TypeError, ValueError):
        return False
    return (
        isinstance(durable_receipt, dict)
        and durable_receipt.get("receipt_id") == receipt_id
        and durable_receipt.get("status") == status
        and durable_receipt.get("pack_publication_id") == publication_id
        and receipt_id
        == _sha(
            b"history-receipt-v1\0"
            + canonical_bytes(_receipt_material(durable_receipt))
        )
    )
