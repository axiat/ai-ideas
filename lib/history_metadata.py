"""Versioned metadata shadow retrieval for history audit v2."""

import contextlib
import datetime
import json
import math

try:
    from lib import history_audit_store
    from lib import history_contract_v2 as contract
except ImportError:
    import history_audit_store
    import history_contract_v2 as contract


PROFILE_FIELDS = frozenset(
    {
        "profile_id",
        "profile_key",
        "profile_version",
        "schema_version",
        "producer",
        "prompt_sha256",
        "synopsis_max_chars",
        "supersedes_profile_id",
    }
)
PRODUCER_FIELDS = frozenset({"kind", "id", "version"})
ANNOTATION_FAMILIES = frozenset(
    {"synopsis", "concept", "free_tag", "cluster", "direction"}
)
DIRECTION_FIELDS = frozenset(
    {
        "run_id",
        "batch_id",
        "direction_id",
        "contract_sha",
        "validator_version",
        "artifact_sha",
    }
)
RRF_K = 60


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_time(value, name):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is required")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(datetime.timezone.utc)


def _time_text(value):
    return value.astimezone(datetime.timezone.utc).isoformat()


def _sha(value, name):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def _text(value, name):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is required")
    return value


def _canonical_text(value):
    return contract.canonical_bytes(value).decode("utf-8")


def _framed(domain, value):
    return contract.framed_sha256(domain, contract.canonical_bytes(value))


@contextlib.contextmanager
def _transaction(conn, name):
    if conn.in_transaction:
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield
            conn.execute(f"RELEASE SAVEPOINT {name}")
        except Exception:
            conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            conn.execute(f"RELEASE SAVEPOINT {name}")
            raise
    else:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def _validate_profile(profile):
    if not isinstance(profile, dict) or set(profile) != PROFILE_FIELDS:
        raise ValueError("metadata profile schema is closed")
    producer = profile.get("producer")
    if not isinstance(producer, dict) or set(producer) != PRODUCER_FIELDS:
        raise ValueError("metadata producer identity schema is closed")
    for name in ("profile_id", "profile_key", "profile_version"):
        _text(profile[name], name)
    if profile["schema_version"] != "history-metadata-profile-v1":
        raise ValueError("unsupported metadata profile schema")
    for name in PRODUCER_FIELDS:
        _text(producer[name], f"producer.{name}")
    _sha(profile["prompt_sha256"], "prompt_sha256")
    if (
        type(profile["synopsis_max_chars"]) is not int
        or profile["synopsis_max_chars"] < 0
    ):
        raise ValueError("synopsis_max_chars must be a nonnegative integer")
    supersedes = profile["supersedes_profile_id"]
    if supersedes is not None:
        _text(supersedes, "supersedes_profile_id")
        if supersedes == profile["profile_id"]:
            raise ValueError("metadata profile cannot supersede itself")
    canonical = json.loads(_canonical_text(profile))
    return canonical, _framed("history-metadata-profile-v1", canonical)


def _latest_profile_state(conn, profile_id):
    row = conn.execute(
        """
        SELECT state FROM audit_metadata_profile_events_v2
        WHERE profile_id=? ORDER BY event_sequence DESC LIMIT 1
        """,
        (profile_id,),
    ).fetchone()
    return None if row is None else row["state"]


def _profile_event(conn, profile_id, state, reason, replaced_by, created_at):
    material = {
        "profile_id": profile_id,
        "state": state,
        "reason": reason,
        "replaced_by_profile_id": replaced_by,
    }
    conn.execute(
        """
        INSERT INTO audit_metadata_profile_events_v2(
          event_id, profile_id, state, reason, replaced_by_profile_id, created_at
        ) VALUES(?, ?, ?, ?, ?, ?)
        """,
        (
            _framed("history-metadata-profile-event-v1", material),
            profile_id,
            state,
            reason,
            replaced_by,
            created_at,
        ),
    )


def register_profile(conn, profile):
    """Register immutable synopsis/concept/tag producer identity."""
    normalized, profile_sha = _validate_profile(profile)
    created_at = _time_text(_utc_now())
    with _transaction(conn, "metadata_register_profile"):
        existing = conn.execute(
            "SELECT * FROM audit_metadata_profiles_v2 WHERE profile_id=?",
            (normalized["profile_id"],),
        ).fetchone()
        if existing is not None:
            if existing["profile_sha256"] != profile_sha:
                raise ValueError("metadata profile identity is immutable")
            return dict(existing)
        version_owner = conn.execute(
            """
            SELECT profile_id FROM audit_metadata_profiles_v2
            WHERE profile_key=? AND profile_version=?
            """,
            (normalized["profile_key"], normalized["profile_version"]),
        ).fetchone()
        if version_owner is not None:
            raise ValueError("metadata profile key/version is already registered")
        supersedes = normalized["supersedes_profile_id"]
        prior = None
        if supersedes is not None:
            prior = conn.execute(
                "SELECT * FROM audit_metadata_profiles_v2 WHERE profile_id=?",
                (supersedes,),
            ).fetchone()
            if prior is None or prior["profile_key"] != normalized["profile_key"]:
                raise ValueError("superseded metadata profile is incompatible")
            if _latest_profile_state(conn, supersedes) != "current":
                raise ValueError("superseded metadata profile is not current")
        producer = normalized["producer"]
        conn.execute(
            """
            INSERT INTO audit_metadata_profiles_v2(
              profile_id, profile_key, profile_version, profile_sha256,
              profile_json, producer_kind, producer_id, producer_version,
              prompt_sha256, synopsis_max_chars, supersedes_profile_id,
              created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized["profile_id"],
                normalized["profile_key"],
                normalized["profile_version"],
                profile_sha,
                _canonical_text(normalized),
                producer["kind"],
                producer["id"],
                producer["version"],
                normalized["prompt_sha256"],
                normalized["synopsis_max_chars"],
                supersedes,
                created_at,
            ),
        )
        if prior is not None:
            _profile_event(
                conn,
                supersedes,
                "stale",
                "superseded_profile",
                normalized["profile_id"],
                created_at,
            )
        _profile_event(
            conn,
            normalized["profile_id"],
            "current",
            "registered",
            None,
            created_at,
        )
        return dict(
            conn.execute(
                "SELECT * FROM audit_metadata_profiles_v2 WHERE profile_id=?",
                (normalized["profile_id"],),
            ).fetchone()
        )


def enqueue_candidate(conn, candidate_id, content_sha, profile_id):
    """Append one metadata-generation outbox fact idempotently."""
    _text(candidate_id, "candidate_id")
    _sha(content_sha, "content_sha")
    _text(profile_id, "profile_id")
    created_at = _time_text(_utc_now())
    with _transaction(conn, "metadata_enqueue_candidate"):
        candidate = conn.execute(
            "SELECT raw_sha256, source_sequence FROM candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        if candidate is None or candidate["raw_sha256"] != content_sha:
            raise ValueError("metadata candidate source identity mismatch")
        profile = conn.execute(
            "SELECT * FROM audit_metadata_profiles_v2 WHERE profile_id=?",
            (profile_id,),
        ).fetchone()
        if profile is None or _latest_profile_state(conn, profile_id) != "current":
            raise ValueError("metadata profile is not eligible")
        material = {
            "candidate_id": candidate_id,
            "source_content_sha": content_sha,
            "source_sequence": candidate["source_sequence"],
            "profile_id": profile_id,
            "profile_sha256": profile["profile_sha256"],
            "producer_kind": profile["producer_kind"],
            "producer_id": profile["producer_id"],
            "producer_version": profile["producer_version"],
            "prompt_sha256": profile["prompt_sha256"],
        }
        outbox_id = _framed("history-metadata-outbox-v2", material)
        conn.execute(
            """
            INSERT OR IGNORE INTO audit_metadata_outbox_v2(
              outbox_id, profile_id, profile_sha256, candidate_id,
              source_content_sha, source_sequence, producer_kind, producer_id,
              producer_version, prompt_sha256, state, fence, claim_token,
              lease_until, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, NULL, NULL, ?)
            """,
            (
                outbox_id,
                profile_id,
                profile["profile_sha256"],
                candidate_id,
                content_sha,
                candidate["source_sequence"],
                profile["producer_kind"],
                profile["producer_id"],
                profile["producer_version"],
                profile["prompt_sha256"],
                created_at,
            ),
        )
        return dict(
            conn.execute(
                "SELECT * FROM audit_metadata_outbox_v2 WHERE outbox_id=?",
                (outbox_id,),
            ).fetchone()
        )


def claim_candidate(conn, outbox_id, claim_token, lease_until, *, now=None):
    """Claim pending metadata work or recover one expired fenced claim."""
    _sha(outbox_id, "outbox_id")
    _text(claim_token, "claim_token")
    now_value = _utc_now() if now is None else _parse_time(now, "now")
    lease_value = _parse_time(lease_until, "lease_until")
    if lease_value <= now_value:
        raise ValueError("metadata claim lease must be in the future")
    with _transaction(conn, "metadata_claim_candidate"):
        row = conn.execute(
            "SELECT * FROM audit_metadata_outbox_v2 WHERE outbox_id=?",
            (outbox_id,),
        ).fetchone()
        if row is None:
            raise ValueError("metadata outbox item is missing")
        if _latest_profile_state(conn, row["profile_id"]) != "current":
            raise history_audit_store.StaleFence("metadata profile is stale")
        if row["state"] == "claimed":
            if _parse_time(row["lease_until"], "stored lease") > now_value:
                raise history_audit_store.StaleFence(
                    "metadata outbox claim has not expired"
                )
            expected_state = "claimed"
        elif row["state"] == "pending":
            expected_state = row["state"]
        else:
            raise history_audit_store.StaleFence(
                "metadata outbox item is already settled"
            )
        history_audit_store.compare_and_set_metadata_shadow_outbox(
            conn,
            outbox_id,
            expected_state=expected_state,
            expected_fence=row["fence"],
            new_state="claimed",
            new_fence=row["fence"] + 1,
            claim_token=claim_token,
            lease_until=_time_text(lease_value),
            transition_now=_time_text(now_value),
        )
        return dict(
            conn.execute(
                "SELECT * FROM audit_metadata_outbox_v2 WHERE outbox_id=?",
                (outbox_id,),
            ).fetchone()
        )


def _validate_direction_identity(value):
    if not isinstance(value, dict) or set(value) != DIRECTION_FIELDS:
        raise ValueError("direction metadata identity schema is closed")
    normalized = dict(value)
    for name in ("run_id", "batch_id", "direction_id", "validator_version"):
        _text(normalized[name], f"direction_identity.{name}")
    _sha(normalized["contract_sha"], "direction_identity.contract_sha")
    _sha(normalized["artifact_sha"], "direction_identity.artifact_sha")
    return json.loads(_canonical_text(normalized))


def _normalize_annotation(value, synopsis_max_chars):
    if not isinstance(value, dict):
        raise ValueError("metadata annotation must be an object")
    family = value.get("family")
    direction = value.get("direction_identity")
    expected = {"family", "value", "confidence"}
    if direction is not None:
        expected.add("direction_identity")
    if set(value) != expected or family not in ANNOTATION_FAMILIES:
        raise ValueError("metadata annotation schema is closed")
    annotation_value = value["value"]
    if annotation_value is not None and not isinstance(annotation_value, str):
        raise ValueError("metadata annotation value must be text, empty, or unknown")
    if family == "synopsis" and isinstance(annotation_value, str) and len(
        annotation_value
    ) > synopsis_max_chars:
        raise ValueError("metadata synopsis exceeds its profile bound")
    confidence = value["confidence"]
    if (
        type(confidence) not in {int, float}
        or isinstance(confidence, bool)
        or not math.isfinite(confidence)
        or confidence < 0
        or confidence > 1
    ):
        raise ValueError("metadata confidence must be between zero and one")
    if family == "direction":
        direction = _validate_direction_identity(direction)
    elif direction is not None:
        raise ValueError("run-scoped direction evidence cannot mint a global concept")
    return {
        "family": family,
        "value": annotation_value,
        "confidence": float(confidence),
        "direction_identity": direction,
    }


def _claim_matches(row, claim):
    fields = (
        "outbox_id",
        "profile_id",
        "profile_sha256",
        "candidate_id",
        "source_content_sha",
        "source_sequence",
        "producer_kind",
        "producer_id",
        "producer_version",
        "prompt_sha256",
        "fence",
        "claim_token",
        "lease_until",
    )
    return isinstance(claim, dict) and all(
        claim.get(name) == row[name] for name in fields
    )


def publish_annotations(conn, claim, annotations):
    """Publish append-only versioned annotations and settle the outbox."""
    if not isinstance(annotations, list):
        raise ValueError("metadata annotations must be an array")
    now = _utc_now()
    with _transaction(conn, "metadata_publish_annotations"):
        outbox_id = claim.get("outbox_id") if isinstance(claim, dict) else None
        row = conn.execute(
            "SELECT * FROM audit_metadata_outbox_v2 WHERE outbox_id=?",
            (outbox_id,),
        ).fetchone()
        if (
            row is None
            or row["state"] != "claimed"
            or not _claim_matches(row, claim)
            or _parse_time(row["lease_until"], "stored lease") <= now
            or _latest_profile_state(conn, row["profile_id"]) != "current"
        ):
            raise history_audit_store.StaleFence(
                "metadata publish claim is stale"
            )
        candidate = conn.execute(
            "SELECT raw_sha256, source_sequence FROM candidates WHERE candidate_id=?",
            (row["candidate_id"],),
        ).fetchone()
        if (
            candidate is None
            or candidate["raw_sha256"] != row["source_content_sha"]
            or candidate["source_sequence"] != row["source_sequence"]
        ):
            raise history_audit_store.StaleFence(
                "metadata source identity is stale"
            )
        profile = conn.execute(
            "SELECT * FROM audit_metadata_profiles_v2 WHERE profile_id=?",
            (row["profile_id"],),
        ).fetchone()
        normalized = [
            _normalize_annotation(item, profile["synopsis_max_chars"])
            for item in annotations
        ]
        created_at = _time_text(now)
        annotation_ids = []
        with history_audit_store.metadata_shadow_publish_guard(
            conn,
            outbox_id=row["outbox_id"],
            claim_token=row["claim_token"],
            claim_fence=row["fence"],
            now=created_at,
        ):
            for ordinal, item in enumerate(normalized):
                value_json = _canonical_text(item["value"])
                value_sha = contract.framed_sha256(
                    "history-metadata-value-v1", value_json.encode("utf-8")
                )
                direction_json = (
                    None
                    if item["direction_identity"] is None
                    else _canonical_text(item["direction_identity"])
                )
                identity = {
                    "outbox_id": row["outbox_id"],
                    "ordinal": ordinal,
                    "family": item["family"],
                    "value_sha256": value_sha,
                    "confidence_millionths": round(
                        item["confidence"] * 1_000_000
                    ),
                    "direction_identity": item["direction_identity"],
                }
                annotation_id = _framed(
                    "history-metadata-annotation-version-v1", identity
                )
                conn.execute(
                    """
                    INSERT INTO audit_metadata_annotation_claims_v2(
                      annotation_id, outbox_id, claim_fence, claim_token,
                      created_at
                    ) VALUES(?, ?, ?, ?, ?)
                    """,
                    (
                        annotation_id,
                        row["outbox_id"],
                        row["fence"],
                        row["claim_token"],
                        created_at,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO audit_annotation_versions_v2(
                      annotation_id, outbox_id, profile_id, profile_sha256,
                      candidate_id, source_content_sha, source_sequence, family,
                      value_json, value_sha256, confidence,
                      direction_identity_json, producer_kind, producer_id,
                      producer_version, prompt_sha256, created_at, stale_state
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                             'current')
                    """,
                    (
                        annotation_id,
                        row["outbox_id"],
                        row["profile_id"],
                        row["profile_sha256"],
                        row["candidate_id"],
                        row["source_content_sha"],
                        row["source_sequence"],
                        item["family"],
                        value_json,
                        value_sha,
                        item["confidence"],
                        direction_json,
                        row["producer_kind"],
                        row["producer_id"],
                        row["producer_version"],
                        row["prompt_sha256"],
                        created_at,
                    ),
                )
                annotation_ids.append(annotation_id)
            history_audit_store.record_metadata_shadow_settlement(
                conn,
                outbox_id=row["outbox_id"],
                claim_fence=row["fence"],
                claim_token=row["claim_token"],
                annotation_ids=annotation_ids,
                created_at=created_at,
            )
            history_audit_store.compare_and_set_metadata_shadow_outbox(
                conn,
                row["outbox_id"],
                expected_state="claimed",
                expected_fence=row["fence"],
                new_state="done",
                new_fence=row["fence"] + 1,
            )
        return {
            "outbox_id": row["outbox_id"],
            "published_count": len(annotation_ids),
            "annotation_ids": annotation_ids,
            "settled_fence": row["fence"] + 1,
        }


def _normalize_query_annotation(value):
    if not isinstance(value, dict):
        raise ValueError("metadata query annotation must be an object")
    family = value.get("family")
    direction = value.get("direction_identity")
    expected = {"family", "value", "rank"}
    if direction is not None:
        expected.add("direction_identity")
    if set(value) != expected or family not in ANNOTATION_FAMILIES:
        raise ValueError("metadata query annotation schema is closed")
    if value["value"] is not None and not isinstance(value["value"], str):
        raise ValueError("metadata query value must be text, empty, or unknown")
    if type(value["rank"]) is not int or value["rank"] < 1:
        raise ValueError("metadata query rank must be a positive integer")
    if family == "direction":
        if direction is None:
            return None
        direction = _validate_direction_identity(direction)
    elif direction is not None:
        raise ValueError("direction scope is valid only for direction metadata")
    if value["value"] in {None, ""}:
        return None
    return {
        "family": family,
        "value": value["value"],
        "rank": value["rank"],
        "direction_identity": direction,
    }


def shadow_rank(conn, query_annotations, snapshot, profile_ids):
    """Return one best metadata rank per visible lineage."""
    if not isinstance(query_annotations, list):
        raise ValueError("metadata query annotations must be an array")
    if (
        not isinstance(profile_ids, (list, tuple))
        or any(not isinstance(item, str) or not item for item in profile_ids)
        or len(set(profile_ids)) != len(profile_ids)
    ):
        raise ValueError("metadata profile IDs must be unique strings")
    try:
        from lib import history_audit
    except ImportError:
        import history_audit
    frozen = history_audit._stored_snapshot(conn, snapshot)
    expected_ids = set(frozen["expected_asset_ids"])
    queries = [
        normalized
        for normalized in (
            _normalize_query_annotation(item) for item in query_annotations
        )
        if normalized is not None
    ]
    eligible_profiles = {
        profile_id
        for profile_id in profile_ids
        if _latest_profile_state(conn, profile_id) == "current"
    }
    if not queries or not eligible_profiles:
        return []
    stored = conn.execute(
        """
        SELECT annotation.*, candidate.lineage_id, candidate.raw_sha256,
               candidate.source_sequence AS current_source_sequence,
               profile.profile_sha256 AS current_profile_sha256,
               profile.producer_kind AS current_producer_kind,
               profile.producer_id AS current_producer_id,
               profile.producer_version AS current_producer_version,
               profile.prompt_sha256 AS current_prompt_sha256
        FROM audit_annotation_versions_v2 annotation
        JOIN candidates candidate ON candidate.candidate_id=annotation.candidate_id
        JOIN audit_metadata_profiles_v2 profile
          ON profile.profile_id=annotation.profile_id
        WHERE annotation.stale_state='current'
        ORDER BY annotation.annotation_id
        """
    )
    best = {}
    candidates = {}
    representatives = {}
    for row in stored:
        if (
            row["profile_id"] not in eligible_profiles
            or row["candidate_id"] not in expected_ids
            or row["source_sequence"] > frozen["history_as_of_watermark"]
            or row["source_sequence"] != row["current_source_sequence"]
            or row["source_content_sha"] != row["raw_sha256"]
            or row["profile_sha256"] != row["current_profile_sha256"]
            or row["producer_kind"] != row["current_producer_kind"]
            or row["producer_id"] != row["current_producer_id"]
            or row["producer_version"] != row["current_producer_version"]
            or row["prompt_sha256"] != row["current_prompt_sha256"]
        ):
            continue
        try:
            stored_value = json.loads(row["value_json"])
            stored_direction = (
                None
                if row["direction_identity_json"] is None
                else json.loads(row["direction_identity_json"])
            )
        except (TypeError, ValueError):
            continue
        for query in queries:
            if (
                query["family"] != row["family"]
                or query["value"] != stored_value
                or query["direction_identity"] != stored_direction
            ):
                continue
            contribution = row["confidence"] / (RRF_K + query["rank"])
            key = (row["lineage_id"], row["family"])
            prior = best.get(key, 0.0)
            if contribution > prior:
                best[key] = contribution
                representatives[key] = row["candidate_id"]
            candidates.setdefault(row["lineage_id"], set()).add(
                row["candidate_id"]
            )
    results = []
    for lineage_id in sorted(candidates):
        family_scores = {
            family: best[(lineage_id, family)]
            for family in sorted(ANNOTATION_FAMILIES)
            if (lineage_id, family) in best
        }
        if not family_scores:
            continue
        strongest_family = min(
            family_scores,
            key=lambda family: (-family_scores[family], family),
        )
        results.append(
            {
                "lineage_id": lineage_id,
                "candidate_id": representatives[(lineage_id, strongest_family)],
                "candidate_ids": sorted(candidates[lineage_id]),
                "family_scores": family_scores,
                "score": sum(family_scores.values()),
            }
        )
    results.sort(key=lambda item: (-item["score"], item["lineage_id"]))
    return results


def union_shadow(flat_rankings, metadata_rankings):
    """Add metadata candidates without removing or reranking flat reachability."""
    if not isinstance(flat_rankings, list) or not isinstance(
        metadata_rankings, list
    ):
        raise ValueError("flat and metadata rankings must be arrays")
    flat_ids = []
    for item in flat_rankings:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("candidate_id"), str)
            or not item["candidate_id"]
        ):
            raise ValueError("flat ranking requires candidate_id")
        flat_ids.append(item["candidate_id"])
    reached = set(flat_ids)
    metadata_tail = []
    for item in metadata_rankings:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("candidate_id"), str)
            or not item["candidate_id"]
        ):
            raise ValueError("metadata ranking requires candidate_id")
        if item["candidate_id"] in reached:
            continue
        metadata_tail.append(item)
        reached.add(item["candidate_id"])
    return {
        "flat_rankings": flat_rankings,
        "metadata_rankings": metadata_rankings,
        "metadata_tail": metadata_tail,
        "candidate_union": list(flat_rankings) + metadata_tail,
        "flat_reachability": flat_ids,
    }
