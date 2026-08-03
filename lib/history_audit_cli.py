#!/usr/bin/env python3
"""Closed public CLI for provider diagnostics and audit-v2 shadow evidence."""

import argparse
import hashlib
import json
import os
import pathlib
import stat
import sys
import tempfile

try:
    from lib import direction_contract
    from lib import history_audit_eval_v2
    from lib import history_audit_plan
    from lib import history_audit_store
    from lib import history_runtime
    from lib import history_store
    from lib import provider_adapters
except ImportError:
    import direction_contract
    import history_audit_eval_v2
    import history_audit_plan
    import history_audit_store
    import history_runtime
    import history_store
    import provider_adapters


ROOT = pathlib.Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "history/provider-adapters-v1.json"
PLAN_SCHEMA = "history-audit-shadow-plan-v1"
PLAN_DOMAIN = b"history-audit-shadow-plan-v1\0"
OBSERVATION_DOMAIN = b"history-runtime-observation-v1\0"
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


def _plan(arguments):
    _require_initialized_database(arguments.db)
    candidate_value, _ = _read_canonical_json(
        arguments.candidate, "runtime candidate", maximum=64 * 1024
    )
    candidate = _validated_candidate(candidate_value)
    batch_sha, direction = _validated_batch_binding(
        arguments.batch, candidate
    )
    intent = _single_line(arguments.intent, "intent")
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


def _execute_shadow(arguments):
    plan, _ = _load_shadow_plan(arguments.plan)
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
    plan.set_defaults(handler=_plan)

    for name in ("run", "resume"):
        execute = commands.add_parser(
            name, help=f"{name} a durable audit plan"
        )
        execute.add_argument("--plan", required=True)
        execute.add_argument("--state", required=True)
        execute.set_defaults(handler=_execute_shadow)

    verify = commands.add_parser("verify", help="verify a closed receipt")
    verify.add_argument("--receipt", required=True)
    verify.set_defaults(handler=_verify)

    evaluate = commands.add_parser(
        "evaluate", help="evaluate diagnostic shadow readiness"
    )
    evaluate.add_argument("--qrels", required=True)
    evaluate.add_argument("--outputs", required=True)
    evaluate.set_defaults(handler=_evaluate)
    return parser


def main(argv=None):
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
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
