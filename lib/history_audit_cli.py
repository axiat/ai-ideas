#!/usr/bin/env python3
"""Closed public CLI for provider diagnostics and audit-v2 evidence."""

import argparse
import copy
import hashlib
import json
import os
import pathlib
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from lib import direction_contract
    from lib import history_contract_v2
    from lib import history_cas
    from lib import history_audit_eval_v2
    from lib import history_audit_plan
    from lib import history_audit_store
    from lib import history_execution
    from lib import history_runtime
    from lib import history_store
    from lib import provider_adapters
except ImportError:
    import direction_contract
    import history_contract_v2
    import history_cas
    import history_audit_eval_v2
    import history_audit_plan
    import history_audit_store
    import history_execution
    import history_runtime
    import history_store
    import provider_adapters

REGISTRY = ROOT / "history/provider-adapters-v1.json"
PLAN_SCHEMA = "history-audit-shadow-plan-v1"
PLAN_DOMAIN = b"history-audit-shadow-plan-v1\0"
OBSERVATION_DOMAIN = b"history-runtime-observation-v1\0"
TEST_INPUT_SCHEMA = "history-audit-cli-test-only-shadow-input-v1"
TEST_PLAN_SCHEMA = "history-audit-cli-test-only-plan-v1"
TEST_PROVIDER_PROTOCOL = "history-audit-test-provider-stdio-v1"
TEST_STATE_SCHEMA = "history-audit-cli-execution-state-v1"
TEST_PROVIDER_TIMEOUT_SECONDS = 10
TEST_PROVIDER_OUTPUT_LIMIT = 4 * 1024 * 1024
HOST_PREPARE_INPUT_SCHEMA = "history-router-host-cli-prepare-input-v1"
HOST_PREPARE_RECEIPT_SCHEMA = "history-router-host-cli-prepare-receipt-v1"
HOST_FINAL_RECEIPT_SCHEMA = "history-router-host-cli-final-receipt-v1"
_SHA_CHARS = frozenset("0123456789abcdef")
_PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "candidate",
        "intent",
        "status",
        "reason_code",
        "observation_scope",
        "l1_observation_sha256",
        "batch_sha256",
        "direction",
        "execution_request_profiles",
        "hard_complete_work_created",
        "production_no_match_authorized",
        "authority",
        "plan_sha256",
    }
)
_OBSERVATION_FIELDS = frozenset(
    {
        "schema_version",
        "candidate_id",
        "candidate_content_sha256",
        "observations",
        "observation_sha256",
    }
)
_OBSERVATION_ITEM_FIELDS = frozenset(
    {
        "intent",
        "retrieval_status",
        "status",
        "pack_path",
        "comparison_path",
        "receipt_path",
        "attempts",
    }
)
_OBSERVATION_ATTEMPT_FIELDS = frozenset(
    {"pack_path", "comparison_path", "receipt_path", "status"}
)
_TEST_INPUT_FIELDS = frozenset(
    {
        "schema_version",
        "authority_scope",
        "run_id",
        "batch_id",
        "intent",
        "candidate",
        "snapshot",
        "capacity_profile",
        "capacity_profile_sha256",
        "provider_pools_ordered",
        "provider_capabilities",
        "provider_capabilities_sha256",
        "fake_executable",
        "risk_policy",
        "risk_policy_sha256",
        "risk_slice_policy",
        "risk_slice_policy_sha256",
        "router_domain_sources",
        "semantic_policy_profile_id",
        "bundle_sha256",
    }
)
_TEST_PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "authority_scope",
        "production_authority",
        "test_only_shadow_input",
        "runtime_plan",
        "runtime_plan_sha256",
        "plan_envelope_sha256",
    }
)
_TEST_STATE_FIELDS = frozenset(
    {
        "schema_version",
        "authority_scope",
        "runtime_plan_sha256",
        "plan_envelope_sha256",
        "status",
        "receipt_sha256",
        "state_sha256",
    }
)
_HOST_PREPARE_INPUT_FIELDS = frozenset(
    {
        "schema_version", "authority_scope", "preplan", "observations",
        "input_sha256",
    }
)
_HOST_PREPLAN_FIELDS = frozenset(
    {
        "run_id", "batch_id", "intent", "history_as_of_watermark",
        "exclusion_policy_sha", "records", "candidates",
    }
)
_HOST_PREPLAN_CANDIDATE_FIELDS = frozenset(
    {"candidate_id", "raw_artifact_sha", "source_order"}
)
_HOST_OBSERVATION_FIELDS = frozenset(
    {"schema_version", "selected_candidate_id", "members"}
)
_HOST_OBSERVATION_MEMBER_FIELDS = frozenset(
    {
        "candidate_id", "selection_class", "channel_states",
        "assigned_slice_ids", "permanent_request_id",
    }
)
_HOST_PREPARE_RECEIPT_FIELDS = frozenset(
    {
        "schema_version", "authority_scope", "input_sha256",
        "preplan_sha256", "route_round_sha256",
        "observation_set_sha256", "host_round_authority_sha256",
        "pre_l1_source_set_sha256", "run_id", "batch_id", "intent",
        "snapshot_id", "snapshot_hash", "candidates", "receipt_sha256",
    }
)
_HOST_PREPARE_RECEIPT_CANDIDATE_FIELDS = frozenset(
    {
        "candidate_id", "candidate_hash", "raw_artifact_sha",
        "source_order", "pre_phase_fact_sha256", "call_l1_model",
    }
)
_HOST_L1_OBSERVATION_FIELDS = frozenset(
    {
        "schema_version", "route_round_sha256",
        "host_round_authority_sha256", "run_id", "batch_id", "intent",
        "snapshot_id", "snapshot_hash", "candidate_id", "candidate_hash",
        "candidate_raw_artifact_sha256", "source_order",
        "pre_phase_fact_sha256", "comparator_outcome", "coverage_state",
    }
)
_HOST_FINAL_RECEIPT_FIELDS = frozenset(
    {
        "schema_version", "authority_scope", "prepare_receipt_sha256",
        "route_round_sha256", "run_id", "batch_id", "intent",
        "l1_comparator_fact_sha256_by_candidate",
        "final_source_set_sha256", "candidate_routes", "receipt_sha256",
    }
)


class AuditCliError(ValueError):
    pass


def _canonical_bytes(value):
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AuditCliError("invalid JSON value") from exc


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _canonical_sha(domain, value):
    try:
        return history_contract_v2.framed_sha256(
            domain, history_contract_v2.canonical_bytes(value)
        )
    except history_contract_v2.ContractV2Error as exc:
        raise AuditCliError("invalid canonical authority material") from exc


def _is_sha(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and set(value).issubset(_SHA_CHARS)
    )


def _single_line(value, label, *, optional=False):
    if optional and value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or any(character in value for character in "\0\r\n")
    ):
        raise AuditCliError(f"invalid {label}")
    return value


def _pairs_without_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise AuditCliError("invalid JSON: duplicate object field")
        value[key] = item
    return value


def _reject_constant(_value):
    raise AuditCliError("invalid JSON number")


def _read_canonical_json(path, label, *, maximum=32 * 1024 * 1024):
    source = pathlib.Path(path)
    try:
        metadata = os.lstat(source)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise AuditCliError(f"invalid {label}")
        if metadata.st_size > maximum:
            raise AuditCliError(f"invalid {label}: byte bound exceeded")
        raw = source.read_bytes()
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=_reject_constant,
        )
    except AuditCliError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditCliError(f"invalid {label}") from exc
    if raw != _canonical_bytes(value):
        raise AuditCliError(f"invalid {label}: JSON is not canonical")
    return value, raw


def _atomic_write(path, raw):
    destination = pathlib.Path(path)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
    except OSError as exc:
        raise AuditCliError("invalid output destination") from exc
    temporary = pathlib.Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise AuditCliError("output publication failed") from exc


def _emit(value):
    sys.stdout.buffer.write(_canonical_bytes(value))
    return 0


def _reject_path_argument_aliases(arguments):
    paths = []
    for name in (
        "state",
        "receipt",
        "db",
        "plan",
        "input",
        "output",
        "prepare_receipt",
    ):
        value = getattr(arguments, name, None)
        if value is None:
            continue
        label = "--" + name.replace("_", "-")
        path = pathlib.Path(value)
        try:
            resolved = path.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise AuditCliError(f"invalid {label} path") from exc
        for other_label, other_path, other_resolved in paths:
            aliases = resolved == other_resolved
            if not aliases:
                try:
                    aliases = os.path.samefile(path, other_path)
                except OSError:
                    aliases = False
            if aliases:
                raise AuditCliError(
                    f"{other_label} and {label} paths must be distinct"
                )
        paths.append((label, path, resolved))


def _provider_command(arguments):
    registry = provider_adapters.load_registry(REGISTRY)
    intent = provider_adapters.resolve_command_intent(
        registry,
        arguments.surface,
        arguments.provider,
        model=arguments.model,
        reasoning=arguments.reasoning,
    )
    return _emit(provider_adapters.command_intent_record(intent))


def _owner_only_directory(path):
    directory = pathlib.Path(path)
    try:
        if directory.exists() or directory.is_symlink():
            metadata = os.lstat(directory)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(
                metadata.st_mode
            ):
                raise AuditCliError("invalid CAS root")
        else:
            directory.mkdir(parents=True, mode=0o700)
        os.chmod(directory, 0o700)
    except AuditCliError:
        raise
    except OSError as exc:
        raise AuditCliError("invalid CAS root") from exc


def _init(arguments):
    _owner_only_directory(arguments.cas_root)
    connection = None
    try:
        connection = history_store.connect(arguments.db)
        history_store.init_schema(connection)
        history_store.init_audit_schema_v2(connection)
        history_audit_store.quarantine_legacy_receipts(connection)
    except Exception as exc:
        raise AuditCliError("audit schema initialization failed") from exc
    finally:
        if connection is not None:
            connection.close()
    return _emit(
        {
            "cas_initialized": True,
            "database_initialized": True,
            "schema_version": "history-audit-init-v1",
            "status": "ready",
        }
    )


def _require_initialized_database(path):
    database = pathlib.Path(path)
    try:
        metadata = os.lstat(database)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise AuditCliError("invalid audit database")
    except FileNotFoundError as exc:
        raise AuditCliError("audit database is not initialized") from exc
    except OSError as exc:
        raise AuditCliError("invalid audit database") from exc
    connection = None
    try:
        connection = history_store.connect(database)
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        required = {
            "audit_logical_tasks",
            "audit_task_attempts",
            "audit_semantic_release_authorizations_v2",
        }
        if not required.issubset(names):
            raise AuditCliError("audit database is not initialized")
    except AuditCliError:
        raise
    except Exception as exc:
        raise AuditCliError("invalid audit database") from exc
    finally:
        if connection is not None:
            connection.close()


def _connect_audit_database(path):
    connection = None
    try:
        connection = history_store.connect(path)
        history_store.init_audit_schema_v2(connection)
        return connection
    except Exception:
        if connection is not None:
            connection.close()
        raise


def _validated_candidate(value):
    try:
        expected = history_audit_plan.runtime_candidate_hash(value)
    except Exception as exc:
        raise AuditCliError("invalid runtime candidate") from exc
    if value.get("candidate_hash") != expected:
        raise AuditCliError("invalid runtime candidate hash")
    return dict(value)


def _validated_batch_binding(path, candidate):
    if path is None:
        return None, None
    batch, _ = _read_canonical_json(
        path, "frozen candidate batch", maximum=32 * 1024 * 1024
    )
    try:
        history_runtime.verify_frozen_batch(batch)
        direction = history_runtime.frozen_batch_direction(batch)
    except Exception as exc:
        raise AuditCliError("invalid frozen candidate batch") from exc
    source_order = candidate["source_order"]
    descriptors = batch.get("candidates")
    if (
        not isinstance(descriptors, list)
        or source_order >= len(descriptors)
    ):
        raise AuditCliError("runtime candidate is outside frozen batch")
    descriptor = descriptors[source_order]
    if (
        not isinstance(descriptor, dict)
        or descriptor.get("candidate_id") != candidate["candidate_id"]
        or descriptor.get("content_sha256")
        != candidate["raw_artifact_sha"]
    ):
        raise AuditCliError("runtime candidate is outside frozen batch")
    return batch["batch_sha256"], direction


def _profile_descriptor(intent):
    return {
        "surface": intent.surface,
        "provider": intent.provider,
        "requested_model": intent.requested_model,
        "requested_reasoning": intent.requested_reasoning,
        "effective_model": intent.effective_model,
        "effective_reasoning": intent.effective_reasoning,
        "default_probe_revision": intent.default_probe_revision,
        "model_catalog_probe_revision": intent.model_catalog_probe_revision,
        "model_catalog_sha256": intent.model_catalog_sha256,
        "execution_request_profile_hash": (
            intent.execution_request_profile_hash
        ),
    }


def _load_profile_descriptors(paths):
    registry = provider_adapters.load_registry(REGISTRY)
    descriptors = []
    seen = set()
    for path in paths:
        intent = provider_adapters.load_command_intent(path, registry)
        descriptor = _profile_descriptor(intent)
        profile_hash = descriptor["execution_request_profile_hash"]
        if profile_hash in seen:
            continue
        seen.add(profile_hash)
        descriptors.append(descriptor)
    return descriptors


def _validate_observation_item(item):
    if not isinstance(item, dict) or set(item) != _OBSERVATION_ITEM_FIELDS:
        raise AuditCliError("invalid L1 observation item")
    for field in ("intent", "retrieval_status", "status", "pack_path"):
        _single_line(item.get(field), f"L1 observation {field}")
    if item["status"] != item["retrieval_status"]:
        raise AuditCliError("invalid L1 observation status")
    if item["comparison_path"] is not None or item["receipt_path"] is not None:
        raise AuditCliError("invalid L1 observation completion")
    attempts = item["attempts"]
    if not isinstance(attempts, list) or len(attempts) != 1:
        raise AuditCliError("invalid L1 observation attempts")
    attempt = attempts[0]
    if not isinstance(attempt, dict) or set(attempt) != _OBSERVATION_ATTEMPT_FIELDS:
        raise AuditCliError("invalid L1 observation attempt")
    if attempt != {
        "pack_path": item["pack_path"],
        "comparison_path": None,
        "receipt_path": None,
        "status": item["retrieval_status"],
    }:
        raise AuditCliError("invalid L1 observation attempt binding")


def _validated_l1_observation(path, candidate, intent):
    value, raw = _read_canonical_json(
        path, "L1 observation", maximum=4 * 1024 * 1024
    )
    if not isinstance(value, dict) or set(value) != _OBSERVATION_FIELDS:
        raise AuditCliError("invalid L1 observation schema")
    material = dict(value)
    observation_sha = material.pop("observation_sha256", None)
    if (
        value.get("schema_version") != 1
        or value.get("candidate_id") != candidate["candidate_id"]
        or value.get("candidate_content_sha256")
        != candidate["raw_artifact_sha"]
        or not _is_sha(observation_sha)
        or observation_sha
        != _sha256(OBSERVATION_DOMAIN + _canonical_bytes(material))
    ):
        raise AuditCliError("invalid L1 observation binding")
    items = value.get("observations")
    if not isinstance(items, list) or not items:
        raise AuditCliError("invalid L1 observation coverage")
    observed_intents = []
    for item in items:
        _validate_observation_item(item)
        observed_intents.append(item["intent"])
    if len(set(observed_intents)) != len(observed_intents) or intent not in observed_intents:
        raise AuditCliError("invalid L1 observation intent coverage")
    return _sha256(raw)


def _plan_hash(material):
    return _sha256(PLAN_DOMAIN + _canonical_bytes(material))


def _canonical_equal(left, right):
    try:
        return history_contract_v2.canonical_bytes(
            left
        ) == history_contract_v2.canonical_bytes(right)
    except history_contract_v2.ContractV2Error:
        return False


def _validated_host_prepare_input(value):
    try:
        if not isinstance(value, dict) or set(value) != _HOST_PREPARE_INPUT_FIELDS:
            raise ValueError("prepare input fields")
        material = copy.deepcopy(value)
        input_sha = material.pop("input_sha256", None)
        if (
            value.get("schema_version") != HOST_PREPARE_INPUT_SCHEMA
            or value.get("authority_scope") != "host_production"
            or not _is_sha(input_sha)
            or input_sha != _canonical_sha(HOST_PREPARE_INPUT_SCHEMA, material)
        ):
            raise ValueError("prepare input authority")

        preplan = value.get("preplan")
        if not isinstance(preplan, dict) or set(preplan) != _HOST_PREPLAN_FIELDS:
            raise ValueError("preplan fields")
        for field in ("run_id", "batch_id", "intent"):
            _single_line(preplan.get(field), f"host preplan {field}")
        if (
            type(preplan.get("history_as_of_watermark")) is not int
            or preplan["history_as_of_watermark"] < 0
            or not _is_sha(preplan.get("exclusion_policy_sha"))
        ):
            raise ValueError("preplan authority")
        records = history_audit_plan.runtime_snapshot_records(
            preplan.get("records")
        )
        if records != preplan["records"]:
            raise ValueError("preplan record order")

        candidates = preplan.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ValueError("preplan candidates")
        candidate_ids = []
        source_orders = []
        for candidate in candidates:
            if (
                not isinstance(candidate, dict)
                or set(candidate) != _HOST_PREPLAN_CANDIDATE_FIELDS
            ):
                raise ValueError("preplan candidate fields")
            candidate_ids.append(
                _single_line(
                    candidate.get("candidate_id"),
                    "host preplan candidate_id",
                )
            )
            if (
                not _is_sha(candidate.get("raw_artifact_sha"))
                or type(candidate.get("source_order")) is not int
                or candidate["source_order"] < 0
            ):
                raise ValueError("preplan candidate authority")
            source_orders.append(candidate["source_order"])
        if (
            candidate_ids != sorted(candidate_ids)
            or len(set(candidate_ids)) != len(candidate_ids)
            or len(set(source_orders)) != len(source_orders)
        ):
            raise ValueError("preplan candidate cohort")

        observations = value.get("observations")
        if (
            not isinstance(observations, dict)
            or set(observations) != _HOST_OBSERVATION_FIELDS
            or observations.get("schema_version")
            != "history-router-host-observations-v1"
        ):
            raise ValueError("host observation fields")
        members = observations.get("members")
        if (
            not isinstance(members, list)
            or [
                member.get("candidate_id")
                if isinstance(member, dict)
                else None
                for member in members
            ]
            != candidate_ids
            or observations.get("selected_candidate_id") not in candidate_ids
        ):
            raise ValueError("host observation cohort")
        allowed_slices = set(
            history_audit_eval_v2.RISK_SLICE_POLICY_V1["allowed_slices"]
        )
        selected_class_ids = []
        for member in members:
            if set(member) != _HOST_OBSERVATION_MEMBER_FIELDS:
                raise ValueError("host observation member fields")
            channels = member.get("channel_states")
            slices = member.get("assigned_slice_ids")
            if (
                member.get("selection_class")
                not in {"finalist", "sa", "screened"}
                or not isinstance(channels, list)
                or [
                    channel.get("channel_id")
                    if isinstance(channel, dict)
                    else None
                    for channel in channels
                ]
                != ["dense_core", "exact_lineage", "fts"]
                or any(
                    not isinstance(channel, dict)
                    or set(channel) != {"channel_id", "state"}
                    or channel["state"] not in {"complete", "failed", "missing"}
                    for channel in channels
                )
                or not isinstance(slices, list)
                or slices != sorted(slices)
                or len(set(slices)) != len(slices)
                or set(slices).difference(allowed_slices)
                or (
                    member.get("permanent_request_id") is not None
                    and not _is_sha(member["permanent_request_id"])
                )
            ):
                raise ValueError("host observation member authority")
            if member["selection_class"] in {"finalist", "sa"}:
                selected_class_ids.append(member["candidate_id"])
        if selected_class_ids != [observations["selected_candidate_id"]]:
            raise ValueError("host observation selection")
        return copy.deepcopy(value)
    except Exception as exc:
        if isinstance(exc, AuditCliError) and str(exc) == (
            "invalid host router prepare input"
        ):
            raise
        raise AuditCliError("invalid host router prepare input") from exc


def _load_host_prepare_input(path):
    try:
        value, _ = _read_canonical_json(
            path, "host router prepare input", maximum=32 * 1024 * 1024
        )
        return _validated_host_prepare_input(value)
    except Exception as exc:
        if isinstance(exc, AuditCliError) and str(exc) == (
            "invalid host router prepare input"
        ):
            raise
        raise AuditCliError("invalid host router prepare input") from exc


def _sealed_host_prepare_receipt(material):
    receipt = copy.deepcopy(material)
    receipt["receipt_sha256"] = _canonical_sha(
        HOST_PREPARE_RECEIPT_SCHEMA, material
    )
    return receipt


def _validated_host_prepare_receipt(value):
    try:
        if (
            not isinstance(value, dict)
            or set(value) != _HOST_PREPARE_RECEIPT_FIELDS
        ):
            raise ValueError("prepare receipt fields")
        material = copy.deepcopy(value)
        receipt_sha = material.pop("receipt_sha256", None)
        if (
            value.get("schema_version") != HOST_PREPARE_RECEIPT_SCHEMA
            or value.get("authority_scope") != "host_production"
            or not _is_sha(receipt_sha)
            or receipt_sha
            != _canonical_sha(HOST_PREPARE_RECEIPT_SCHEMA, material)
        ):
            raise ValueError("prepare receipt authority")
        for field in (
            "input_sha256",
            "preplan_sha256",
            "route_round_sha256",
            "observation_set_sha256",
            "host_round_authority_sha256",
            "pre_l1_source_set_sha256",
            "snapshot_id",
            "snapshot_hash",
        ):
            if not _is_sha(value.get(field)):
                raise ValueError("prepare receipt digest")
        for field in ("run_id", "batch_id", "intent"):
            _single_line(value.get(field), f"host prepare receipt {field}")

        candidates = value.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ValueError("prepare receipt candidates")
        candidate_ids = []
        source_orders = []
        for candidate in candidates:
            if (
                not isinstance(candidate, dict)
                or set(candidate) != _HOST_PREPARE_RECEIPT_CANDIDATE_FIELDS
                or not _is_sha(candidate.get("candidate_hash"))
                or not _is_sha(candidate.get("raw_artifact_sha"))
                or not _is_sha(candidate.get("pre_phase_fact_sha256"))
                or type(candidate.get("source_order")) is not int
                or candidate["source_order"] < 0
                or type(candidate.get("call_l1_model")) is not bool
            ):
                raise ValueError("prepare receipt candidate fields")
            candidate_id = _single_line(
                candidate.get("candidate_id"),
                "host prepare receipt candidate_id",
            )
            expected_hash = history_audit_plan.runtime_candidate_hash(
                {
                    "candidate_id": candidate_id,
                    "candidate_hash": candidate["candidate_hash"],
                    "raw_artifact_sha": candidate["raw_artifact_sha"],
                    "source_order": candidate["source_order"],
                }
            )
            if expected_hash != candidate["candidate_hash"]:
                raise ValueError("prepare receipt candidate binding")
            candidate_ids.append(candidate_id)
            source_orders.append(candidate["source_order"])
        if (
            candidate_ids != sorted(candidate_ids)
            or len(set(candidate_ids)) != len(candidate_ids)
            or len(set(source_orders)) != len(source_orders)
        ):
            raise ValueError("prepare receipt candidate cohort")
        return copy.deepcopy(value)
    except Exception as exc:
        if isinstance(exc, AuditCliError) and str(exc) == (
            "invalid host router prepare receipt"
        ):
            raise
        raise AuditCliError("invalid host router prepare receipt") from exc


def _load_host_prepare_receipt(path):
    try:
        value, _ = _read_canonical_json(
            path, "host router prepare receipt", maximum=4 * 1024 * 1024
        )
        return _validated_host_prepare_receipt(value)
    except Exception as exc:
        if isinstance(exc, AuditCliError) and str(exc) == (
            "invalid host router prepare receipt"
        ):
            raise
        raise AuditCliError("invalid host router prepare receipt") from exc


def _validated_host_l1_observations(paths, prepare_receipt):
    try:
        required_candidates = [
            candidate
            for candidate in prepare_receipt["candidates"]
            if candidate["call_l1_model"]
        ]
        required_ids = [
            candidate["candidate_id"] for candidate in required_candidates
        ]
        observations_by_candidate = {}
        for path in paths:
            observation, raw = _read_canonical_json(
                path,
                "host router L1 observation",
                maximum=64 * 1024,
            )
            if (
                not isinstance(observation, dict)
                or set(observation) != _HOST_L1_OBSERVATION_FIELDS
                or observation.get("schema_version")
                != "history-router-host-l1-observation-v2"
            ):
                raise ValueError("L1 observation fields")
            candidate_id = observation.get("candidate_id")
            if (
                candidate_id not in required_ids
                or candidate_id in observations_by_candidate
            ):
                raise ValueError("L1 observation cohort")
            candidate = next(
                item
                for item in required_candidates
                if item["candidate_id"] == candidate_id
            )
            expected = {
                "route_round_sha256": prepare_receipt[
                    "route_round_sha256"
                ],
                "host_round_authority_sha256": prepare_receipt[
                    "host_round_authority_sha256"
                ],
                "run_id": prepare_receipt["run_id"],
                "batch_id": prepare_receipt["batch_id"],
                "intent": prepare_receipt["intent"],
                "snapshot_id": prepare_receipt["snapshot_id"],
                "snapshot_hash": prepare_receipt["snapshot_hash"],
                "candidate_id": candidate_id,
                "candidate_hash": candidate["candidate_hash"],
                "candidate_raw_artifact_sha256": candidate[
                    "raw_artifact_sha"
                ],
                "source_order": candidate["source_order"],
                "pre_phase_fact_sha256": candidate[
                    "pre_phase_fact_sha256"
                ],
            }
            if (
                any(observation.get(name) != value for name, value in expected.items())
                or observation.get("comparator_outcome")
                not in {"match", "no_match", "uncertain"}
                or observation.get("coverage_state") != "complete"
            ):
                raise ValueError("L1 observation binding")
            observations_by_candidate[candidate_id] = raw
        if set(observations_by_candidate) != set(required_ids):
            raise ValueError("L1 observation coverage")
        return [
            {
                "candidate_id": candidate_id,
                "raw_observation_bytes": observations_by_candidate[
                    candidate_id
                ],
            }
            for candidate_id in required_ids
        ]
    except Exception as exc:
        if isinstance(exc, AuditCliError) and str(exc) == (
            "invalid host router L1 observations"
        ):
            raise
        raise AuditCliError("invalid host router L1 observations") from exc


def _sealed_host_final_receipt(material):
    receipt = copy.deepcopy(material)
    receipt["receipt_sha256"] = _canonical_sha(
        HOST_FINAL_RECEIPT_SCHEMA, material
    )
    return receipt


def _host_route_prepare(arguments):
    _require_initialized_database(arguments.db)
    prepare_input = _load_host_prepare_input(arguments.input)
    preplan_input = prepare_input["preplan"]
    connection = None
    try:
        connection = _connect_audit_database(arguments.db)
        preplan = history_audit_store.record_host_router_preplan(
            connection,
            **preplan_input,
        )
        router_round = history_audit_store.prepare_host_router_round(
            connection,
            run_id=preplan_input["run_id"],
            batch_id=preplan_input["batch_id"],
            intent=preplan_input["intent"],
            raw_observations=prepare_input["observations"],
        )
        history_audit_store.issue_host_router_domain_sources(
            connection,
            router_round["route_round_sha256"],
            phase="pre_l1",
        )
        derivation = history_audit_store.derive_candidate_route_facts(
            connection,
            preplan_input["run_id"],
            preplan_input["batch_id"],
            preplan_input["intent"],
            phase="pre_l1",
        )
        routes = derivation["candidate_routes"]
        if (
            derivation.get("phase") != "pre_l1"
            or derivation.get("route_round_sha256")
            != router_round["route_round_sha256"]
            or [route.get("candidate_id") for route in routes]
            != [candidate["candidate_id"] for candidate in preplan["candidates"]]
        ):
            raise AuditCliError("host router prepare receipt mismatch")
        candidate_receipts = []
        for candidate, route in zip(preplan["candidates"], routes):
            candidate_receipts.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "candidate_hash": candidate["candidate_hash"],
                    "raw_artifact_sha": candidate["raw_artifact_sha"],
                    "source_order": candidate["source_order"],
                    "pre_phase_fact_sha256": route[
                        "phase_fact_sha256"
                    ],
                    "call_l1_model": bool(route["call_l1_model"]),
                }
            )
        material = {
            "schema_version": HOST_PREPARE_RECEIPT_SCHEMA,
            "authority_scope": "host_production",
            "input_sha256": prepare_input["input_sha256"],
            "preplan_sha256": preplan["preplan_sha256"],
            "route_round_sha256": router_round["route_round_sha256"],
            "observation_set_sha256": router_round[
                "observation_set_sha256"
            ],
            "host_round_authority_sha256": router_round[
                "host_round_authority_sha256"
            ],
            "pre_l1_source_set_sha256": derivation[
                "source_set_sha256"
            ],
            "run_id": preplan_input["run_id"],
            "batch_id": preplan_input["batch_id"],
            "intent": preplan_input["intent"],
            "snapshot_id": preplan["snapshot"]["snapshot_id"],
            "snapshot_hash": preplan["snapshot"]["snapshot_hash"],
            "candidates": candidate_receipts,
        }
        receipt = _validated_host_prepare_receipt(
            _sealed_host_prepare_receipt(material)
        )
        raw = _canonical_bytes(receipt)
        _atomic_write(arguments.output, raw)
        sys.stdout.buffer.write(raw)
        return 0
    except AuditCliError:
        raise
    except Exception as exc:
        raise AuditCliError("host router prepare failed") from exc
    finally:
        if connection is not None:
            connection.close()


def _host_route_finalize(arguments):
    _require_initialized_database(arguments.db)
    prepare_receipt = _load_host_prepare_receipt(
        arguments.prepare_receipt
    )
    observations = _validated_host_l1_observations(
        arguments.l1_observation,
        prepare_receipt,
    )
    connection = None
    try:
        connection = _connect_audit_database(arguments.db)
        verified_prepare_receipt = (
            history_audit_store.verify_host_router_prepare_receipt(
                connection,
                prepare_receipt,
            )
        )
        if not _canonical_equal(
            verified_prepare_receipt,
            prepare_receipt,
        ):
            raise AuditCliError("host router prepare receipt mismatch")
        comparator_batch = (
            history_audit_store.record_host_router_l1_observations(
                connection,
                route_round_sha256=prepare_receipt[
                    "route_round_sha256"
                ],
                observations=observations,
            )
        )
        history_audit_store.issue_host_router_domain_sources(
            connection,
            prepare_receipt["route_round_sha256"],
            phase="final",
        )
        derivation = history_audit_store.derive_candidate_route_facts(
            connection,
            prepare_receipt["run_id"],
            prepare_receipt["batch_id"],
            prepare_receipt["intent"],
            phase="final",
        )
        candidate_ids = [
            candidate["candidate_id"]
            for candidate in prepare_receipt["candidates"]
        ]
        routes = derivation["candidate_routes"]
        comparator_receipts = comparator_batch.get("receipts")
        if (
            comparator_batch.get("authority_scope") != "host_production"
            or comparator_batch.get("route_round_sha256")
            != prepare_receipt["route_round_sha256"]
            or not isinstance(comparator_receipts, list)
            or [
                receipt.get("candidate_id")
                for receipt in comparator_receipts
            ]
            != [
                candidate["candidate_id"]
                for candidate in prepare_receipt["candidates"]
                if candidate["call_l1_model"]
            ]
            or derivation.get("phase") != "final"
            or derivation.get("route_round_sha256")
            != prepare_receipt["route_round_sha256"]
            or [route.get("candidate_id") for route in routes]
            != candidate_ids
        ):
            raise AuditCliError("host router final receipt mismatch")
        comparator_map = {
            receipt["candidate_id"]: receipt["comparator_fact_sha256"]
            for receipt in comparator_receipts
        }
        if any(not _is_sha(value) for value in comparator_map.values()):
            raise AuditCliError("host router final receipt mismatch")
        material = {
            "schema_version": HOST_FINAL_RECEIPT_SCHEMA,
            "authority_scope": "host_production",
            "prepare_receipt_sha256": prepare_receipt[
                "receipt_sha256"
            ],
            "route_round_sha256": prepare_receipt[
                "route_round_sha256"
            ],
            "run_id": prepare_receipt["run_id"],
            "batch_id": prepare_receipt["batch_id"],
            "intent": prepare_receipt["intent"],
            "l1_comparator_fact_sha256_by_candidate": comparator_map,
            "final_source_set_sha256": derivation[
                "source_set_sha256"
            ],
            "candidate_routes": copy.deepcopy(routes),
        }
        receipt = _sealed_host_final_receipt(material)
        if set(receipt) != _HOST_FINAL_RECEIPT_FIELDS:
            raise AuditCliError("host router final receipt mismatch")
        raw = _canonical_bytes(receipt)
        _atomic_write(arguments.output, raw)
        sys.stdout.buffer.write(raw)
        return 0
    except AuditCliError:
        raise
    except Exception as exc:
        raise AuditCliError("host router finalize failed") from exc
    finally:
        if connection is not None:
            connection.close()


def _trusted_test_fixture_bytes():
    path = ROOT / "history/test-only-provider-v1.py"
    try:
        metadata = os.lstat(path)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise AuditCliError("invalid repository test-only fixture")
        raw = path.read_bytes()
    except OSError as exc:
        raise AuditCliError("invalid repository test-only fixture") from exc
    if len(raw) > 1024 * 1024:
        raise AuditCliError("invalid repository test-only fixture")
    return raw


def _validated_test_only_input(value):
    if not isinstance(value, dict) or set(value) != _TEST_INPUT_FIELDS:
        raise AuditCliError("invalid test-only shadow input schema")
    material = copy.deepcopy(value)
    bundle_sha = material.pop("bundle_sha256", None)
    if (
        value.get("schema_version") != TEST_INPUT_SCHEMA
        or value.get("authority_scope") != "test-only-shadow"
        or not _is_sha(bundle_sha)
        or bundle_sha
        != _canonical_sha(
            "history-audit-cli-test-only-shadow-input-v1", material
        )
    ):
        raise AuditCliError("invalid test-only shadow input authority")
    for field in ("run_id", "batch_id", "intent"):
        _single_line(value.get(field), f"test-only input {field}")

    candidate = _validated_candidate(value.get("candidate"))
    snapshot = value.get("snapshot")
    snapshot_fields = {
        "snapshot_id",
        "snapshot_hash",
        "history_as_of_watermark",
        "current_batch_id_namespace",
        "current_batch_ids_hash",
        "current_batch_ids",
        "exclusion_policy_sha",
        "expected_asset_ids_hash",
        "expected_asset_ids",
        "records",
    }
    if not isinstance(snapshot, dict) or set(snapshot) != snapshot_fields:
        raise AuditCliError("invalid test-only runtime snapshot")
    try:
        records = history_audit_plan.runtime_snapshot_records(
            snapshot["records"]
        )
    except history_audit_plan.AuditPlanError as exc:
        raise AuditCliError("invalid test-only runtime snapshot") from exc
    if (
        snapshot.get("current_batch_id_namespace")
        != "history-v2-staging-v1"
        or snapshot.get("current_batch_ids") != [candidate["candidate_id"]]
        or snapshot.get("expected_asset_ids")
        != sorted(record["item_id"] for record in records)
        or history_contract_v2.ordered_set_sha256(
            "history-current-batch-ids-v2", snapshot["current_batch_ids"]
        )
        != snapshot.get("current_batch_ids_hash")
        or history_contract_v2.ordered_set_sha256(
            "history-snapshot-assets-v2", snapshot["expected_asset_ids"]
        )
        != snapshot.get("expected_asset_ids_hash")
    ):
        raise AuditCliError("invalid test-only runtime snapshot binding")
    snapshot_material = {
        "run_id": value["run_id"],
        "batch_id": value["batch_id"],
        "history_as_of_watermark": snapshot["history_as_of_watermark"],
        "current_batch_id_namespace": snapshot[
            "current_batch_id_namespace"
        ],
        "current_batch_ids_hash": snapshot["current_batch_ids_hash"],
        "exclusion_policy_sha": snapshot["exclusion_policy_sha"],
        "expected_asset_ids_hash": snapshot["expected_asset_ids_hash"],
    }
    expected_snapshot_hash = _canonical_sha(
        "history-snapshot-v2", snapshot_material
    )
    expected_snapshot_id = _canonical_sha(
        "history-snapshot-id-v2",
        {
            "run_id": value["run_id"],
            "batch_id": value["batch_id"],
            "snapshot_hash": expected_snapshot_hash,
        },
    )
    if (
        snapshot.get("snapshot_hash") != expected_snapshot_hash
        or snapshot.get("snapshot_id") != expected_snapshot_id
    ):
        raise AuditCliError("invalid test-only runtime snapshot identity")

    try:
        host = history_audit_plan._host_runtime_authority()
    except history_audit_plan.AuditPlanError as exc:
        raise AuditCliError("invalid host runtime policy") from exc
    capacity = value.get("capacity_profile")
    if (
        not _canonical_equal(
            capacity, host["capacity_profiles"].get("fake-safe-24k-v1")
        )
        or value.get("capacity_profile_sha256")
        != _canonical_sha("history-audit-cli-test-capacity-v1", capacity)
    ):
        raise AuditCliError("invalid test-only capacity authority")
    pools = value.get("provider_pools_ordered")
    try:
        history_audit_plan._validate_pools(pools)
    except history_audit_plan.AuditPlanError as exc:
        raise AuditCliError("invalid test-only provider pools") from exc
    providers = {
        provider for pool in pools.values() for provider in pool
    }
    capabilities = value.get("provider_capabilities")
    if (
        not isinstance(capabilities, dict)
        or set(capabilities) != providers
        or value.get("provider_capabilities_sha256")
        != _canonical_sha(
            "history-audit-cli-test-provider-capabilities-v1", capabilities
        )
    ):
        raise AuditCliError("invalid test-only provider capabilities")
    capability_fields = {
        "provider",
        "capability_profile_hash",
        "model_identity",
        "reasoning_identity",
        "model_default",
        "reasoning_default",
        "executable",
        "cli_revision",
    }
    binding_fields = capability_fields.difference({"provider"})
    for provider in sorted(providers):
        capability = capabilities.get(provider)
        binding = capacity["provider_bindings"].get(provider)
        if (
            not isinstance(capability, dict)
            or set(capability) != capability_fields
            or capability.get("provider") != provider
            or not isinstance(binding, dict)
            or binding.get("state") != "hard-complete"
            or any(
                capability.get(field) != binding.get(field)
                for field in binding_fields
            )
        ):
            raise AuditCliError("invalid test-only provider capability binding")

    executable = value.get("fake_executable")
    if (
        not isinstance(executable, dict)
        or set(executable) != {"path", "sha256", "protocol_revision"}
        or not isinstance(executable.get("path"), str)
        or not pathlib.Path(executable["path"]).is_absolute()
        or not _is_sha(executable.get("sha256"))
        or executable.get("protocol_revision") != TEST_PROVIDER_PROTOCOL
    ):
        raise AuditCliError("invalid test-only executable descriptor")
    fixture = _trusted_test_fixture_bytes()
    if executable["sha256"] != _sha256(fixture):
        raise AuditCliError("invalid test-only executable descriptor")
    if (
        not _canonical_equal(value.get("risk_policy"), host["risk_policy"])
        or value.get("risk_policy_sha256") != host["risk_policy_sha"]
        or not _canonical_equal(
            value.get("risk_slice_policy"),
            history_audit_eval_v2.RISK_SLICE_POLICY_V1,
        )
        or value.get("risk_slice_policy_sha256")
        != _canonical_sha(
            "history-risk-slice-policy-v1",
            history_audit_eval_v2.RISK_SLICE_POLICY_V1,
        )
        or value.get("semantic_policy_profile_id")
        != host["semantic_policy_profile_id"]
    ):
        raise AuditCliError("invalid test-only policy authority")
    sources = value.get("router_domain_sources")
    if (
        not isinstance(sources, dict)
        or set(sources) != set(history_audit_store._ROUTER_SOURCE_KINDS)
        or any(not isinstance(source, dict) for source in sources.values())
    ):
        raise AuditCliError("invalid router domain source set")
    return copy.deepcopy(value)


def _load_test_only_input(path):
    value, _ = _read_canonical_json(
        path, "test-only shadow input", maximum=32 * 1024 * 1024
    )
    return _validated_test_only_input(value)


def _test_router_round_material(bundle):
    preliminary = history_audit_plan._issue_test_runtime_authority(
        provider_pools_ordered=bundle["provider_pools_ordered"],
        provider_capabilities=bundle["provider_capabilities"],
        intent=bundle["intent"],
        semantic_policy_profile_id=bundle["semantic_policy_profile_id"],
        matched_router_rule_ids=(),
        max_output_tokens=bundle["capacity_profile"]["max_output_tokens"],
    )
    snapshot_fields = {
        "snapshot_id",
        "snapshot_hash",
        "history_as_of_watermark",
        "current_batch_id_namespace",
        "current_batch_ids_hash",
        "current_batch_ids",
        "exclusion_policy_sha",
        "expected_asset_ids_hash",
        "expected_asset_ids",
    }
    material = {
        "schema_version": "history-router-round-v1",
        "run_id": bundle["run_id"],
        "batch_id": bundle["batch_id"],
        "intent": bundle["intent"],
        "snapshot": {
            name: copy.deepcopy(bundle["snapshot"][name])
            for name in snapshot_fields
        },
        "candidates": [copy.deepcopy(bundle["candidate"])],
        "semantic_policy_profile_id": bundle["semantic_policy_profile_id"],
        "risk_policy_sha": bundle["risk_policy_sha256"],
        "risk_slice_policy_sha": bundle["risk_slice_policy_sha256"],
        "budget_policy_sha": history_audit_plan.runtime_budget_policy_sha(
            preliminary["budget_policy"]
        ),
        "authority_scope": "test_fake",
    }
    try:
        normalized, _ = history_audit_store._router_validate_round_material(
            material
        )
    except history_audit_store.AuditMigrationError as exc:
        raise AuditCliError("invalid router round material") from exc
    return normalized, history_audit_store._router_round_sha(normalized)


def _persist_test_only_plan(connection, bundle):
    round_material, route_round_sha = _test_router_round_material(bundle)
    sources = copy.deepcopy(bundle["router_domain_sources"])
    try:
        for kind in sorted(sources):
            history_audit_store._router_validate_domain_source(
                round_material, route_round_sha, kind, sources[kind]
            )
        if (
            sources["selection"]["selected_candidate_id"]
            != bundle["candidate"]["candidate_id"]
        ):
            raise AuditCliError("test-only selected candidate mismatch")
        route_round = history_audit_store.prepare_router_round(
            connection, round_material
        )
        if route_round["route_round_sha256"] != route_round_sha:
            raise AuditCliError("router round replay mismatch")
        history_audit_store._issue_test_router_domain_sources(
            connection,
            route_round_sha,
            sources={
                kind: source
                for kind, source in sources.items()
                if kind != "l1_observation"
            },
        )
        history_audit_store.derive_candidate_route_facts(
            connection,
            bundle["run_id"],
            bundle["batch_id"],
            bundle["intent"],
            phase="pre_l1",
        )
        history_audit_store._issue_test_router_domain_sources(
            connection,
            route_round_sha,
            sources={"l1_observation": sources["l1_observation"]},
        )
        final = history_audit_store.derive_candidate_route_facts(
            connection,
            bundle["run_id"],
            bundle["batch_id"],
            bundle["intent"],
            phase="final",
        )
    except (history_audit_store.AuditMigrationError, KeyError) as exc:
        raise AuditCliError("router source derivation failed") from exc
    selected = next(
        (
            route
            for route in final["candidate_routes"]
            if route["candidate_id"] == bundle["candidate"]["candidate_id"]
        ),
        None,
    )
    if (
        selected is None
        or selected["dispatch_allowed"] is not True
        or selected["release_authorized"] is not False
    ):
        raise AuditCliError("test-only route is not executable shadow work")
    try:
        plan = history_audit_plan.build_test_only_runtime_plan(
            run_id=bundle["run_id"],
            batch_id=bundle["batch_id"],
            snapshot=bundle["snapshot"],
            candidate=bundle["candidate"],
            provider_pools_ordered=bundle["provider_pools_ordered"],
            provider_capabilities=bundle["provider_capabilities"],
            intent=bundle["intent"],
            matched_router_rule_ids=selected["matched_rule_ids"],
            semantic_policy_profile_id=bundle[
                "semantic_policy_profile_id"
            ],
            test_execution_binding={
                "schema_version": "history-test-execution-binding-v1",
                "fake_executable_sha256": bundle["fake_executable"][
                    "sha256"
                ],
                "protocol_revision": bundle["fake_executable"][
                    "protocol_revision"
                ],
            },
            max_output_tokens=bundle["capacity_profile"][
                "max_output_tokens"
            ],
        )
        history_execution.persist_plan(connection, plan)
        material = history_audit_plan.build_runtime_plan_material(plan)
    except (
        history_audit_plan.AuditPlanError,
        history_execution.ExecutionError,
    ) as exc:
        raise AuditCliError("test-only runtime plan persistence failed") from exc
    return plan, material


def _test_plan_envelope(bundle, runtime_material, runtime_plan_sha):
    public_runtime_plan = {
        **copy.deepcopy(runtime_material),
        "plan_sha": runtime_plan_sha,
    }
    material = {
        "schema_version": TEST_PLAN_SCHEMA,
        "authority_scope": "test-only-shadow",
        "production_authority": False,
        "test_only_shadow_input": copy.deepcopy(bundle),
        "runtime_plan": public_runtime_plan,
        "runtime_plan_sha256": runtime_plan_sha,
    }
    return {
        **material,
        "plan_envelope_sha256": _canonical_sha(
            "history-audit-cli-test-only-plan-v1", material
        ),
    }


def _plan(arguments):
    _require_initialized_database(arguments.db)
    candidate_value, _ = _read_canonical_json(
        arguments.candidate, "runtime candidate", maximum=64 * 1024
    )
    candidate = _validated_candidate(candidate_value)
    intent = _single_line(arguments.intent, "intent")
    if arguments.test_only_shadow_input is not None:
        if (
            arguments.batch is not None
            or arguments.l1_observation is not None
            or arguments.execution_request_profile
        ):
            raise AuditCliError(
                "test-only shadow input cannot be mixed with legacy plan inputs"
            )
        bundle = _load_test_only_input(arguments.test_only_shadow_input)
        if (
            not _canonical_equal(bundle["candidate"], candidate)
            or bundle["intent"] != intent
        ):
            raise AuditCliError("test-only plan input binding mismatch")
        connection = None
        try:
            connection = _connect_audit_database(arguments.db)
            plan, runtime_material = _persist_test_only_plan(
                connection, bundle
            )
        except AuditCliError:
            raise
        except Exception as exc:
            raise AuditCliError("test-only plan construction failed") from exc
        finally:
            if connection is not None:
                connection.close()
        envelope = _test_plan_envelope(
            bundle, runtime_material, plan["plan_sha"]
        )
        raw = _canonical_bytes(envelope)
        _atomic_write(arguments.output, raw)
        sys.stdout.buffer.write(raw)
        return 0
    batch_sha, direction = _validated_batch_binding(
        arguments.batch, candidate
    )
    profiles = _load_profile_descriptors(
        arguments.execution_request_profile or []
    )
    observation_scope = "configuration_shadow"
    observation_sha = None
    if arguments.l1_observation is not None:
        observation_sha = _validated_l1_observation(
            arguments.l1_observation, candidate, intent
        )
        observation_scope = "l1_shadow"
    material = {
        "schema_version": PLAN_SCHEMA,
        "candidate": candidate,
        "intent": intent,
        "status": "producer_unavailable",
        "reason_code": "unbudgetable_provider",
        "observation_scope": observation_scope,
        "l1_observation_sha256": observation_sha,
        "batch_sha256": batch_sha,
        "direction": direction,
        "execution_request_profiles": profiles,
        "hard_complete_work_created": False,
        "production_no_match_authorized": False,
        "authority": "shadow-only",
    }
    plan = dict(material)
    plan["plan_sha256"] = _plan_hash(material)
    raw = _canonical_bytes(plan)
    _atomic_write(arguments.output, raw)
    sys.stdout.buffer.write(raw)
    return 0


def _validate_profile_descriptor(value):
    try:
        registry = provider_adapters.load_registry(REGISTRY)
        provider_adapters.validate_command_profile_descriptor(
            registry, value
        )
    except provider_adapters.ProviderResolutionError as exc:
        raise AuditCliError(
            "invalid execution request profile descriptor"
        ) from exc


def _validated_shadow_plan(value):
    if not isinstance(value, dict) or set(value) != _PLAN_FIELDS:
        raise AuditCliError("invalid shadow plan schema")
    if value.get("schema_version") != PLAN_SCHEMA:
        raise AuditCliError("invalid shadow plan version")
    _validated_candidate(value.get("candidate"))
    _single_line(value.get("intent"), "intent")
    if (
        value.get("status") != "producer_unavailable"
        or value.get("reason_code") != "unbudgetable_provider"
        or value.get("authority") != "shadow-only"
        or value.get("hard_complete_work_created") is not False
        or value.get("production_no_match_authorized") is not False
    ):
        raise AuditCliError("invalid shadow plan authority")
    scope = value.get("observation_scope")
    l1_sha = value.get("l1_observation_sha256")
    if not (
        (scope == "configuration_shadow" and l1_sha is None)
        or (scope == "l1_shadow" and _is_sha(l1_sha))
    ):
        raise AuditCliError("invalid shadow plan observation scope")
    batch_sha = value.get("batch_sha256")
    direction = value.get("direction")
    if batch_sha is None:
        if direction is not None:
            raise AuditCliError("invalid shadow plan batch binding")
    elif not _is_sha(batch_sha):
        raise AuditCliError("invalid shadow plan batch binding")
    try:
        direction_contract.validate_identity(direction)
    except direction_contract.DirectionContractError as exc:
        raise AuditCliError("invalid shadow plan direction binding") from exc
    profiles = value.get("execution_request_profiles")
    if not isinstance(profiles, list):
        raise AuditCliError("invalid execution request profiles")
    profile_hashes = []
    for descriptor in profiles:
        _validate_profile_descriptor(descriptor)
        profile_hashes.append(descriptor["execution_request_profile_hash"])
    if len(profile_hashes) != len(set(profile_hashes)):
        raise AuditCliError("duplicate execution request profile")
    material = dict(value)
    plan_sha = material.pop("plan_sha256", None)
    if not _is_sha(plan_sha) or plan_sha != _plan_hash(material):
        raise AuditCliError("invalid shadow plan hash")
    return value


def _load_shadow_plan(path):
    value, raw = _read_canonical_json(
        path, "shadow plan", maximum=4 * 1024 * 1024
    )
    return _validated_shadow_plan(value), raw


def _validated_test_plan(value):
    if not isinstance(value, dict) or set(value) != _TEST_PLAN_FIELDS:
        raise AuditCliError("invalid test-only plan envelope schema")
    material = copy.deepcopy(value)
    envelope_sha = material.pop("plan_envelope_sha256", None)
    if (
        value.get("schema_version") != TEST_PLAN_SCHEMA
        or value.get("authority_scope") != "test-only-shadow"
        or value.get("production_authority") is not False
        or not _is_sha(envelope_sha)
        or envelope_sha
        != _canonical_sha("history-audit-cli-test-only-plan-v1", material)
    ):
        raise AuditCliError("invalid test-only plan envelope authority")
    bundle = _validated_test_only_input(value.get("test_only_shadow_input"))
    public_plan = value.get("runtime_plan")
    if not isinstance(public_plan, dict):
        raise AuditCliError("invalid test-only runtime plan")
    runtime_plan_sha = public_plan.get("plan_sha")
    runtime_material = copy.deepcopy(public_plan)
    runtime_material.pop("plan_sha", None)
    try:
        normalized = history_audit_plan.validate_runtime_plan_material(
            runtime_material
        )
        computed = history_audit_plan.runtime_plan_sha_from_material(
            normalized
        )
    except history_audit_plan.AuditPlanError as exc:
        raise AuditCliError("invalid test-only runtime plan") from exc
    if (
        not _is_sha(runtime_plan_sha)
        or runtime_plan_sha != computed
        or value.get("runtime_plan_sha256") != computed
        or runtime_material.get("authority_scope") != "test-only-shadow"
        or runtime_material.get("run_id") != bundle["run_id"]
        or runtime_material.get("batch_id") != bundle["batch_id"]
        or runtime_material.get("intent") != bundle["intent"]
        or not _canonical_equal(
            runtime_material.get("candidate"), bundle["candidate"]
        )
        or runtime_material.get("snapshot", {}).get("snapshot_id")
        != bundle["snapshot"]["snapshot_id"]
        or runtime_material.get("snapshot", {}).get("records_sha")
        != history_audit_plan.runtime_snapshot_records_sha(
            bundle["snapshot"]["records"]
        )
        or runtime_material.get("test_execution_binding")
        != {
            "schema_version": "history-test-execution-binding-v1",
            "fake_executable_sha256": bundle["fake_executable"]["sha256"],
            "protocol_revision": bundle["fake_executable"][
                "protocol_revision"
            ],
        }
    ):
        raise AuditCliError("invalid test-only runtime plan binding")
    rebuilt = _build_test_runtime_plan(
        bundle, normalized["matched_router_rule_ids"]
    )
    rebuilt_material = history_audit_plan.build_runtime_plan_material(rebuilt)
    if (
        not _canonical_equal(rebuilt_material, normalized)
        or rebuilt["plan_sha"] != computed
    ):
        raise AuditCliError("test-only runtime plan reconstruction mismatch")
    return copy.deepcopy(value)


def _load_test_plan(path):
    value, raw = _read_canonical_json(
        path, "test-only plan", maximum=32 * 1024 * 1024
    )
    return _validated_test_plan(value), raw


def _build_test_runtime_plan(bundle, matched_router_rule_ids):
    try:
        return history_audit_plan.build_test_only_runtime_plan(
            run_id=bundle["run_id"],
            batch_id=bundle["batch_id"],
            snapshot=bundle["snapshot"],
            candidate=bundle["candidate"],
            provider_pools_ordered=bundle["provider_pools_ordered"],
            provider_capabilities=bundle["provider_capabilities"],
            intent=bundle["intent"],
            matched_router_rule_ids=matched_router_rule_ids,
            semantic_policy_profile_id=bundle["semantic_policy_profile_id"],
            test_execution_binding={
                "schema_version": "history-test-execution-binding-v1",
                "fake_executable_sha256": bundle["fake_executable"]["sha256"],
                "protocol_revision": bundle["fake_executable"][
                    "protocol_revision"
                ],
            },
            max_output_tokens=bundle["capacity_profile"]["max_output_tokens"],
        )
    except history_audit_plan.AuditPlanError as exc:
        raise AuditCliError("invalid reconstructed test-only plan") from exc


def _reconstruct_test_runtime_plan(envelope):
    runtime = envelope["runtime_plan"]
    return _build_test_runtime_plan(
        envelope["test_only_shadow_input"], runtime["matched_router_rule_ids"]
    )


def _require_existing_cas_root(path):
    root = pathlib.Path(path)
    try:
        metadata = os.lstat(root)
    except FileNotFoundError as exc:
        raise AuditCliError("CAS root is not initialized") from exc
    except OSError as exc:
        raise AuditCliError("invalid CAS root") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise AuditCliError("invalid CAS root")
    return root


def _verified_test_executable_bytes(descriptor, supplied_path):
    if supplied_path is None or str(supplied_path) != descriptor["path"]:
        raise AuditCliError("test-only executable path binding mismatch")
    path = pathlib.Path(supplied_path)
    try:
        path_before = os.lstat(path)
        if stat.S_ISLNK(path_before.st_mode) or not stat.S_ISREG(
            path_before.st_mode
        ):
            raise AuditCliError("invalid test-only executable")
        if path_before.st_size > 1024 * 1024:
            raise AuditCliError("test-only executable byte bound exceeded")
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor_fd = os.open(path, flags)
        try:
            file_before = os.fstat(descriptor_fd)
            if (
                not stat.S_ISREG(file_before.st_mode)
                or file_before.st_size != path_before.st_size
                or (file_before.st_dev, file_before.st_ino)
                != (path_before.st_dev, path_before.st_ino)
            ):
                raise AuditCliError("invalid test-only executable")
            chunks = []
            remaining = file_before.st_size
            while remaining:
                chunk = os.read(descriptor_fd, min(65536, remaining))
                if not chunk:
                    raise AuditCliError("test-only executable changed while read")
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            file_after = os.fstat(descriptor_fd)
        finally:
            os.close(descriptor_fd)
        path_after = os.lstat(path)
    except AuditCliError:
        raise
    except OSError as exc:
        raise AuditCliError("invalid test-only executable") from exc
    identity_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if (
        any(
            getattr(file_before, field) != getattr(file_after, field)
            or getattr(file_after, field) != getattr(path_after, field)
            for field in identity_fields
        )
        or _sha256(raw) != descriptor["sha256"]
        or raw != _trusted_test_fixture_bytes()
        or descriptor["protocol_revision"] != TEST_PROVIDER_PROTOCOL
    ):
        raise AuditCliError("test-only executable identity mismatch")
    return raw


def _state_value(envelope, status, receipt_sha=None):
    material = {
        "schema_version": TEST_STATE_SCHEMA,
        "authority_scope": "test-only-shadow",
        "runtime_plan_sha256": envelope["runtime_plan_sha256"],
        "plan_envelope_sha256": envelope["plan_envelope_sha256"],
        "status": status,
        "receipt_sha256": receipt_sha,
    }
    return {
        **material,
        "state_sha256": _canonical_sha(
            "history-audit-cli-execution-state-v1", material
        ),
    }


def _load_execution_state(path, envelope, *, required):
    source = pathlib.Path(path)
    if not source.exists():
        if required:
            raise AuditCliError("durable execution state is missing")
        return None
    value, _ = _read_canonical_json(
        source, "durable execution state", maximum=64 * 1024
    )
    if not isinstance(value, dict) or set(value) != _TEST_STATE_FIELDS:
        raise AuditCliError("invalid durable execution state")
    material = copy.deepcopy(value)
    state_sha = material.pop("state_sha256", None)
    if (
        value.get("schema_version") != TEST_STATE_SCHEMA
        or value.get("authority_scope") != "test-only-shadow"
        or value.get("status") not in {"running", "interrupted", "closed"}
        or value.get("runtime_plan_sha256")
        != envelope["runtime_plan_sha256"]
        or value.get("plan_envelope_sha256")
        != envelope["plan_envelope_sha256"]
        or not _is_sha(state_sha)
        or state_sha
        != _canonical_sha("history-audit-cli-execution-state-v1", material)
        or (
            value["status"] == "closed"
            and not _is_sha(value.get("receipt_sha256"))
        )
        or (
            value["status"] != "closed"
            and value.get("receipt_sha256") is not None
        )
    ):
        raise AuditCliError("invalid durable execution state binding")
    return value


def _receipt_for_plan(connection, plan):
    terminal = history_execution.load_terminal_states(
        connection, plan["plan_sha"]
    )
    summary = history_execution.build_coverage_receipt(
        plan,
        terminal,
        {
            "qualified": False,
            "profile_id": plan["semantic_policy_profile_id"],
        },
        conn=connection,
    )
    route = connection.execute(
        "SELECT * FROM audit_candidate_route_facts_v2 "
        "WHERE run_id=? AND candidate_id=?",
        (plan["run_id"], plan["candidate"]["candidate_id"]),
    ).fetchone()
    if route is None:
        raise AuditCliError("durable route fact is missing")
    tasks = [
        row[0]
        for row in connection.execute(
            "SELECT task.task_hash FROM audit_logical_tasks task "
            "JOIN audit_task_bindings_v2 binding USING(task_hash) "
            "WHERE binding.plan_sha=? ORDER BY task.task_hash",
            (plan["plan_sha"],),
        )
    ]
    attempts = connection.execute(
        "SELECT attempt.attempt_id,attempt.request_cas_object_id,"
        "completion.output_cas_object_id "
        "FROM audit_task_attempts attempt "
        "JOIN audit_logical_tasks task USING(task_hash) "
        "JOIN audit_task_bindings_v2 binding USING(task_hash) "
        "LEFT JOIN audit_attempt_completions_v2 completion USING(attempt_id) "
        "WHERE binding.plan_sha=? ORDER BY attempt.attempt_id",
        (plan["plan_sha"],),
    ).fetchall()
    try:
        matched_rule_ids = history_contract_v2.parse_json_bytes(
            route["matched_rule_ids_json"].encode("utf-8")
        )
    except history_contract_v2.ContractV2Error as exc:
        raise AuditCliError("invalid durable route fact") from exc
    receipt = {
        "manifest_schema_version": "history-audit-manifest-v2",
        "canonical_codec_version": "history-canonical-json-v2",
        "run_id": plan["run_id"],
        "plan_hash": plan["plan_sha"],
        "candidate_hash": plan["candidate"]["candidate_hash"],
        "snapshot_id": plan["snapshot"]["snapshot_id"],
        "snapshot_hash": plan["snapshot"]["snapshot_hash"],
        "history_as_of_watermark": plan["snapshot"][
            "history_as_of_watermark"
        ],
        "current_batch_id_namespace": plan["snapshot"][
            "current_batch_id_namespace"
        ],
        "current_batch_ids_hash": plan["snapshot"][
            "current_batch_ids_hash"
        ],
        "exclusion_policy_sha": plan["snapshot"]["exclusion_policy_sha"],
        "expected_asset_ids_hash": plan["snapshot"][
            "expected_asset_ids_hash"
        ],
        "observed_asset_ids_hash": history_contract_v2.ordered_set_sha256(
            "history-observed-assets-v2", summary["observed_ids"]
        ),
        "missing_ids": summary["missing_ids"],
        "duplicate_ids": summary["duplicate_ids"],
        "extra_ids": summary["extra_ids"],
        "invalid_schema": summary["invalid_schema"],
        "invalid_anchor": summary["invalid_anchor"],
        "truncated": summary["truncated"],
        "provider_pools_ordered": copy.deepcopy(
            plan["provider_pools_ordered"]
        ),
        "provider_capability_profile_hashes": sorted(
            plan["provider_capability_profile_hashes"].values()
        ),
        "capacity_profile_id": plan["capacity_profile_id"],
        "semantic_policy_profile_id": summary[
            "semantic_policy_profile_id"
        ],
        "risk_policy_version": route["risk_policy_version"],
        "matched_router_rule_ids": matched_rule_ids,
        "settlement_policy_sha": plan["settlement_policy_sha"],
        "shard_plan_sha": plan["shard_plan_sha"],
        "logical_task_hashes": tasks,
        "attempt_manifest_hashes": [row[0] for row in attempts],
        "raw_request_output_cas_hashes": sorted(
            {
                object_id
                for row in attempts
                for object_id in (row[1], row[2])
                if object_id is not None
            }
        ),
        "minimum_receipt_sha": "0" * 64,
        "coverage_complete": summary["coverage_complete"],
        "adjudication_complete": summary["adjudication_complete"],
        "semantic_policy_qualified": summary[
            "semantic_policy_qualified"
        ],
        "no_match_basis": summary["no_match_basis"],
        "final_status": summary["final_status"],
        "stage_reason_code": summary["stage_reason_code"],
        "evidence_anchors": summary["evidence_anchors"],
    }
    receipt["minimum_receipt_sha"] = history_contract_v2.minimum_receipt_sha(
        receipt
    )
    try:
        return history_contract_v2.validate_receipt(receipt)
    except history_contract_v2.ContractV2Error as exc:
        raise AuditCliError("invalid derived minimum receipt") from exc


def _load_minimum_receipt(path):
    value, raw = _read_canonical_json(
        path, "minimum receipt", maximum=4 * 1024 * 1024
    )
    try:
        return history_contract_v2.validate_receipt(value), raw
    except history_contract_v2.ContractV2Error as exc:
        raise AuditCliError("invalid minimum receipt") from exc


def _require_durable_test_plan(connection, plan):
    material = history_audit_plan.build_runtime_plan_material(plan)
    plan_json = _canonical_bytes(material).decode("utf-8")
    stored_plan = connection.execute(
        "SELECT run_id,plan_json FROM audit_l2_plans_v2 WHERE plan_sha=?",
        (plan["plan_sha"],),
    ).fetchone()
    manifest = connection.execute(
        "SELECT plan_hash,manifest_json FROM audit_run_manifests WHERE run_id=?",
        (plan["run_id"],),
    ).fetchone()
    if (
        stored_plan is None
        or tuple(stored_plan) != (plan["run_id"], plan_json)
        or manifest is None
        or tuple(manifest) != (plan["plan_sha"], plan_json)
    ):
        raise AuditCliError("durable test-only plan binding is missing")


def _verify_closed_test_receipt(connection, cas_root, plan, receipt):
    if receipt["plan_hash"] != plan["plan_sha"]:
        raise AuditCliError("receipt plan binding mismatch")
    _require_durable_test_plan(connection, plan)
    if not _canonical_equal(_receipt_for_plan(connection, plan), receipt):
        raise AuditCliError("receipt durable material mismatch")
    try:
        return history_cas.verify_minimum_receipt(
            connection,
            cas_root,
            receipt["minimum_receipt_sha"],
        )
    except Exception as exc:
        raise AuditCliError("closed execution receipt is invalid") from exc


def _execution_status(command, envelope, status, receipt_sha=None, reason=None):
    return {
        "schema_version": "history-audit-execution-status-v2",
        "command": command,
        "status": status,
        "reason_code": reason,
        "runtime_plan_sha256": envelope["runtime_plan_sha256"],
        "plan_envelope_sha256": envelope["plan_envelope_sha256"],
        "receipt_sha256": receipt_sha,
    }


def _kill_and_reap_process_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as exc:
        raise AuditCliError("trusted test fixture group cleanup failed") from exc
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired as exc:
        raise AuditCliError("trusted test fixture group reap timed out") from exc


def _run_trusted_test_fixture(executable, request_bytes, environment):
    try:
        process = subprocess.Popen(
            [sys.executable, str(executable)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=environment,
        )
    except OSError as exc:
        raise AuditCliError("trusted test fixture launch failed") from exc
    selector = selectors.DefaultSelector()
    output = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + TEST_PROVIDER_TIMEOUT_SECONDS
    written = 0
    try:
        for stream in (process.stdin, process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
        if request_bytes:
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        else:
            process.stdin.close()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AuditCliError("trusted test fixture timeout")
            for key, _ in selector.select(remaining):
                if key.data == "stdin":
                    written += os.write(
                        key.fileobj.fileno(), request_bytes[written:]
                    )
                    if written == len(request_bytes):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                stream = output[key.data]
                stream.extend(chunk)
                if len(stream) > TEST_PROVIDER_OUTPUT_LIMIT:
                    raise AuditCliError(
                        "trusted test fixture output byte bound exceeded"
                    )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AuditCliError("trusted test fixture timeout")
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise AuditCliError("trusted test fixture timeout") from exc
        if process.returncode != 0:
            raise AuditCliError("trusted test fixture exited nonzero")
        return bytes(output["stdout"]), bytes(output["stderr"])
    except BaseException as exc:
        cleanup_error = None
        try:
            _kill_and_reap_process_group(process)
        except Exception as cleanup_exc:
            cleanup_error = cleanup_exc
        if not isinstance(exc, Exception):
            raise
        if cleanup_error is not None:
            raise cleanup_error from exc
        if isinstance(exc, AuditCliError):
            raise
        raise AuditCliError("trusted test fixture I/O failed") from exc
    finally:
        selector.close()


def _execute_test_plan(arguments, envelope, executable_bytes):
    required = {
        "db": arguments.db,
        "cas_root": arguments.cas_root,
        "receipt": arguments.receipt,
    }
    if any(value is None for value in required.values()):
        raise AuditCliError("test-only execution arguments are incomplete")
    _require_initialized_database(arguments.db)
    cas_root = _require_existing_cas_root(arguments.cas_root)
    state = _load_execution_state(
        arguments.state,
        envelope,
        required=arguments.command == "resume",
    )
    plan = _reconstruct_test_runtime_plan(envelope)
    if state is not None and state["status"] == "closed":
        receipt, _ = _load_minimum_receipt(arguments.receipt)
        if receipt["minimum_receipt_sha"] != state["receipt_sha256"]:
            raise AuditCliError("closed execution receipt mismatch")
        connection = _connect_audit_database(arguments.db)
        try:
            _verify_closed_test_receipt(connection, cas_root, plan, receipt)
        finally:
            connection.close()
        return _emit(
            _execution_status(
                arguments.command,
                envelope,
                "closed",
                receipt["minimum_receipt_sha"],
            )
        )

    connection = None
    try:
        connection = _connect_audit_database(arguments.db)
        history_execution.persist_plan(connection, plan)
        _atomic_write(
            arguments.state,
            _canonical_bytes(_state_value(envelope, "running")),
        )
        with tempfile.TemporaryDirectory(
            prefix="history-audit-fake-exec-"
        ) as temporary:
            executable = pathlib.Path(temporary) / "provider"
            executable.write_bytes(executable_bytes)
            executable.chmod(0o700)

            def provider(_task_key, _provider_name, _ordinal, request_bytes):
                environment = {}
                for name in (
                    "HISTORY_AUDIT_FAKE_PROVIDER_LOG",
                    "HISTORY_AUDIT_TEST_FIXTURE_MODE",
                    "HISTORY_AUDIT_TEST_FIXTURE_CHILD_MARKER",
                ):
                    if os.environ.get(name):
                        environment[name] = os.environ[name]
                stdout, _ = _run_trusted_test_fixture(
                    executable, request_bytes, environment
                )
                return {"kind": "success", "output": stdout}

            fault = bool(
                getattr(arguments, "test_fault_after_cas", False)
            )
            for index, task_key in enumerate(plan["logical_task_keys"]):
                task = history_execution.load_task(connection, task_key)
                if task["state"] in history_execution.TERMINAL_STATES:
                    continue
                history_execution.run_map_task(
                    connection,
                    cas_root,
                    plan,
                    task_key,
                    provider,
                    lease_seconds=60,
                    fault_after_cas=fault and index == 0,
                )
        receipt = _receipt_for_plan(connection, plan)
        receipt_sha = history_cas.write_minimum_receipt(
            connection, receipt
        )
        receipt_raw = _canonical_bytes(receipt)
        _atomic_write(arguments.receipt, receipt_raw)
        _atomic_write(
            arguments.state,
            _canonical_bytes(
                _state_value(envelope, "closed", receipt_sha)
            ),
        )
        return _emit(
            _execution_status(
                arguments.command, envelope, "closed", receipt_sha
            )
        )
    except history_execution.ExecutionCrash:
        _atomic_write(
            arguments.state,
            _canonical_bytes(_state_value(envelope, "interrupted")),
        )
        _emit(
            _execution_status(
                arguments.command,
                envelope,
                "interrupted",
                reason="fault_after_cas",
            )
        )
        return 4
    except AuditCliError:
        raise
    except Exception as exc:
        raise AuditCliError("test-only execution failed") from exc
    finally:
        if connection is not None:
            connection.close()


def _execute_shadow(arguments):
    value, _ = _read_canonical_json(
        arguments.plan, "audit plan", maximum=32 * 1024 * 1024
    )
    if isinstance(value, dict) and value.get("schema_version") == TEST_PLAN_SCHEMA:
        envelope = _validated_test_plan(value)
        executable_bytes = _verified_test_executable_bytes(
            envelope["test_only_shadow_input"]["fake_executable"],
            arguments.test_only_provider_executable,
        )
        return _execute_test_plan(arguments, envelope, executable_bytes)
    plan = _validated_shadow_plan(value)
    _emit(
        {
            "command": arguments.command,
            "plan_sha256": plan["plan_sha256"],
            "reason_code": "producer_unavailable",
            "schema_version": "history-audit-execution-status-v1",
            "status": "plan_not_executable",
        }
    )
    return 3


def _verify(arguments):
    if arguments.plan is not None:
        if arguments.db is None or arguments.cas_root is None:
            raise AuditCliError("test-only verification arguments are incomplete")
        envelope, _ = _load_test_plan(arguments.plan)
        receipt, _ = _load_minimum_receipt(arguments.receipt)
        plan = _reconstruct_test_runtime_plan(envelope)
        _require_initialized_database(arguments.db)
        cas_root = _require_existing_cas_root(arguments.cas_root)
        connection = None
        try:
            connection = _connect_audit_database(arguments.db)
            verified = _verify_closed_test_receipt(
                connection, cas_root, plan, receipt
            )
        finally:
            if connection is not None:
                connection.close()
        return _emit(
            {
                "schema_version": "history-audit-verification-v2",
                "status": "verified",
                "authority": "test-only-shadow",
                "execution_authorized": verified["execution_authorized"],
                "production_authority": False,
                "current_release_authority": verified[
                    "current_release_authority"
                ],
                "runtime_plan_sha256": envelope[
                    "runtime_plan_sha256"
                ],
                "plan_envelope_sha256": envelope[
                    "plan_envelope_sha256"
                ],
                "receipt_sha256": receipt["minimum_receipt_sha"],
            }
        )
    plan, raw = _load_shadow_plan(arguments.receipt)
    return _emit(
        {
            "authority": "shadow-only",
            "plan_sha256": plan["plan_sha256"],
            "production_no_match_authorized": False,
            "receipt_sha256": _sha256(raw),
            "schema_version": "history-audit-verification-v1",
            "status": "verified",
        }
    )


def _evaluate(arguments):
    bundle, _ = _read_canonical_json(
        arguments.qrels, "qrels bundle", maximum=32 * 1024 * 1024
    )
    outputs, _ = _read_canonical_json(
        arguments.outputs, "shadow outputs", maximum=32 * 1024 * 1024
    )
    if not isinstance(bundle, dict) or set(bundle) != {
        "schema_version",
        "scope",
        "qrels",
        "partitions",
        "policy",
    }:
        raise AuditCliError("invalid qrels bundle schema")
    if bundle.get("schema_version") != "history-audit-qrels-bundle-v1":
        raise AuditCliError("invalid qrels bundle version")
    try:
        validated = history_audit_eval_v2.validate_qrels(
            bundle["qrels"],
            bundle["partitions"],
            scope=bundle["scope"],
        )
        result = history_audit_eval_v2.evaluate_shadow_readiness(
            validated,
            outputs,
            bundle["policy"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AuditCliError("invalid shadow evaluation input") from exc
    return _emit(result)


def _parser():
    parser = argparse.ArgumentParser(prog="history_audit_cli.py")
    commands = parser.add_subparsers(dest="command", required=True)

    provider = commands.add_parser(
        "provider-command",
        help="print a canonical no-launch provider command diagnostic",
    )
    provider.add_argument("--surface", choices=("hunt", "awr"), required=True)
    provider.add_argument("--provider", required=True)
    provider.add_argument("--model")
    provider.add_argument("--reasoning")
    provider.set_defaults(handler=_provider_command)

    initialize = commands.add_parser("init", help="initialize audit-v2 storage")
    initialize.add_argument("--db", required=True)
    initialize.add_argument("--cas-root", required=True)
    initialize.set_defaults(handler=_init)

    plan = commands.add_parser("plan", help="seal a closed shadow plan")
    plan.add_argument("--db", required=True)
    plan.add_argument("--candidate", required=True)
    plan.add_argument("--batch")
    plan.add_argument("--intent", required=True)
    plan.add_argument("--output", required=True)
    plan.add_argument(
        "--execution-request-profile",
        action="append",
        default=[],
    )
    plan.add_argument("--l1-observation")
    plan.add_argument("--test-only-shadow-input")
    plan.set_defaults(handler=_plan)

    for name in ("run", "resume"):
        execute = commands.add_parser(
            name, help=f"{name} a durable audit plan"
        )
        execute.add_argument("--plan", required=True)
        execute.add_argument("--state", required=True)
        execute.add_argument("--db")
        execute.add_argument("--cas-root")
        execute.add_argument("--receipt")
        execute.add_argument("--test-only-provider-executable")
        if name == "run":
            execute.add_argument(
                "--test-fault-after-cas", action="store_true"
            )
        execute.set_defaults(handler=_execute_shadow)

    verify = commands.add_parser("verify", help="verify a closed receipt")
    verify.add_argument("--receipt", required=True)
    verify.add_argument("--db")
    verify.add_argument("--cas-root")
    verify.add_argument("--plan")
    verify.set_defaults(handler=_verify)

    evaluate = commands.add_parser(
        "evaluate", help="evaluate diagnostic shadow readiness"
    )
    evaluate.add_argument("--qrels", required=True)
    evaluate.add_argument("--outputs", required=True)
    evaluate.set_defaults(handler=_evaluate)

    host_prepare = commands.add_parser(
        "host-route-prepare",
        help="derive a host-authoritative pre-L1 router receipt",
    )
    host_prepare.add_argument("--db", required=True)
    host_prepare.add_argument("--input", required=True)
    host_prepare.add_argument("--output", required=True)
    host_prepare.set_defaults(handler=_host_route_prepare)

    host_finalize = commands.add_parser(
        "host-route-finalize",
        help="seal host L1 observations and derive final router facts",
    )
    host_finalize.add_argument("--db", required=True)
    host_finalize.add_argument("--prepare-receipt", required=True)
    host_finalize.add_argument(
        "--l1-observation",
        action="append",
        default=[],
    )
    host_finalize.add_argument("--output", required=True)
    host_finalize.set_defaults(handler=_host_route_finalize)
    return parser


def main(argv=None):
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        _reject_path_argument_aliases(arguments)
        return arguments.handler(arguments)
    except provider_adapters.ProviderResolutionError as exc:
        print(f"history-audit: {arguments.command}: {exc}", file=sys.stderr)
        return 2
    except AuditCliError as exc:
        print(
            f"history-audit: {arguments.command}: invalid: {exc}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
