"""Disposable manifest-only agent mirrors for history audit v2."""

import hashlib
import json
import math
import os
import pathlib
import secrets
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unicodedata

try:
    from lib import history_contract_v2
    from lib import provider_adapters
except ImportError:
    import history_contract_v2
    import provider_adapters


class PortableAgentError(RuntimeError):
    def __init__(self, code, detail=None):
        self.code = code
        self.detail = detail
        super().__init__(code)


ENVIRONMENT_POLICY = "provider-config-preserving-scrub-v1"
SCRUBBED_ENVIRONMENT = (
    "PWD",
    "OLDPWD",
    "INIT_CWD",
    "GIT_*",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "PYTHONPATH",
    "PYTHONHOME",
    "VIRTUAL_ENV",
    "HISTORY_*",
    "HISTORY_RUNTIME_ABI",
    "HISTORY_DB",
    "HUNT_*",
    "AWR_*",
    "SIDE_*",
    "SIDE_CMD",
    "CONTAINED_*",
    "CONTAINED_AGENT_CMD_JSON",
    "AGENT_CMD",
    "FRONT_CMD",
    "BACK_CMD",
    "RESEARCH_DIRECTION_FILE",
    "ENV",
    "BASH_ENV",
    "ZDOTDIR",
)
PRESERVED_PROVIDER_CONFIG_ENVIRONMENT = ("HOME", "CODEX_HOME")


def _safe_relative(value, name):
    if not isinstance(value, str):
        raise PortableAgentError("invalid_manifest", name)
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or str(path) in ("", ".") or ".." in path.parts:
        raise PortableAgentError("invalid_manifest", name)
    return path


def _reserved(path):
    lowered = [part.lower() for part in path.parts]
    name = lowered[-1]
    return (
        any(
            part in {
                ".git",
                ".ai-ideas",
                ".claude",
                ".codex",
                "durable-state",
            }
            for part in lowered
        )
        or "ledger" in name
        or "durable-state" in name
        or "candidate-state" in name
        or name.endswith((".db", ".sqlite", ".sqlite3"))
    )


def _regular_single_link(path, code):
    try:
        info = path.lstat()
    except OSError as exc:
        raise PortableAgentError(code) from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PortableAgentError(code)
    return info


def _open_read_stable(path, maximum, code, *, require_owner_only=False):
    before = _regular_single_link(path, code)
    if require_owner_only and before.st_mode & 0o077:
        raise PortableAgentError(code)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        raise PortableAgentError(code) from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise PortableAgentError(code)
        raw = os.read(descriptor, maximum + 1)
        if len(raw) > maximum or os.read(descriptor, 1):
            raise PortableAgentError("oversize")
    finally:
        os.close(descriptor)
    after = _regular_single_link(path, code)
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise PortableAgentError("unstable_output")
    return raw


def _open_directory_at(parent_descriptor, component, code):
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise PortableAgentError("no_follow_traversal_unavailable")
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(component, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        raise PortableAgentError(code) from exc
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise PortableAgentError(code)
    return descriptor


def _open_absolute_directory_no_follow(path, code):
    path = pathlib.Path(path)
    if not path.is_absolute():
        raise PortableAgentError(code)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(os.path.sep, flags)
    try:
        for component in path.parts[1:]:
            child = _open_directory_at(descriptor, component, code)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _read_declared_source(resolved_root, root_info, source_relative, maximum):
    directory_descriptor = _open_absolute_directory_no_follow(
        resolved_root, "unsafe_source_root"
    )
    try:
        opened_root = os.fstat(directory_descriptor)
        if (opened_root.st_dev, opened_root.st_ino) != (
            root_info.st_dev,
            root_info.st_ino,
        ):
            raise PortableAgentError("unstable_source_root")
        for component in source_relative.parts[:-1]:
            child = _open_directory_at(
                directory_descriptor, component, "source_boundary_violation"
            )
            os.close(directory_descriptor)
            directory_descriptor = child
        flags = os.O_RDONLY | os.O_NOFOLLOW
        try:
            source_descriptor = os.open(
                source_relative.parts[-1], flags, dir_fd=directory_descriptor
            )
        except OSError as exc:
            raise PortableAgentError("unsafe_input") from exc
        try:
            before = os.fstat(source_descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise PortableAgentError("unsafe_input")
            chunks = []
            total = 0
            while True:
                chunk = os.read(
                    source_descriptor,
                    min(65536, maximum + 1 - total),
                )
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > maximum:
                    raise PortableAgentError("oversize")
            after = os.fstat(source_descriptor)
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise PortableAgentError("unstable_input")
            return b"".join(chunks)
        finally:
            os.close(source_descriptor)
    finally:
        os.close(directory_descriptor)


def _ensure_owner_tree(root, directory):
    root = pathlib.Path(root)
    directory = pathlib.Path(directory)
    try:
        relative = directory.relative_to(root)
    except ValueError as exc:
        raise PortableAgentError("unsafe_state_path") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            try:
                info = current.lstat()
            except OSError as exc:
                raise PortableAgentError("unsafe_state_path") from exc
            if not stat.S_ISDIR(info.st_mode):
                raise PortableAgentError("unsafe_state_path")
        else:
            current.mkdir(mode=0o700)
        os.chmod(current, 0o700)


def _write_owner_only(path, raw, owner_root):
    _ensure_owner_tree(owner_root, path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(path), flags, 0o600)
    try:
        position = 0
        while position < len(raw):
            position += os.write(descriptor, raw[position:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(str(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_inputs(inputs, mirror):
    if not isinstance(inputs, (list, tuple)):
        raise PortableAgentError("invalid_manifest")
    copied = set()
    for item in inputs:
        if not isinstance(item, dict) or set(item) != {
            "source_root", "source_path", "provenance", "path", "sha256",
            "max_bytes",
        }:
            raise PortableAgentError("invalid_manifest")
        if item["provenance"] != "declared-input-v1":
            raise PortableAgentError("invalid_provenance")
        source_relative = _safe_relative(item["source_path"], "source path")
        relative = _safe_relative(item["path"], "input path")
        if (
            _reserved(source_relative)
            or _reserved(relative)
            or relative.as_posix().startswith("output/")
        ):
            raise PortableAgentError("reserved_input")
        if relative.as_posix() in copied:
            raise PortableAgentError("duplicate_input")
        maximum = item["max_bytes"]
        if type(maximum) is not int or maximum < 0:
            raise PortableAgentError("invalid_manifest")
        source_root = pathlib.Path(item["source_root"])
        try:
            root_info = source_root.lstat()
        except OSError as exc:
            raise PortableAgentError("unsafe_source_root") from exc
        if not source_root.is_absolute() or not stat.S_ISDIR(root_info.st_mode):
            raise PortableAgentError("unsafe_source_root")
        try:
            resolved_root = source_root.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise PortableAgentError("source_boundary_violation") from exc
        resolved_parts = pathlib.PurePosixPath(resolved_root.as_posix()).joinpath(
            source_relative
        )
        if _reserved(resolved_parts):
            raise PortableAgentError("reserved_input")
        raw = _read_declared_source(
            resolved_root, root_info, source_relative, maximum
        )
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise PortableAgentError("input_sha_mismatch")
        target = mirror.joinpath(*relative.parts)
        _write_owner_only(target, raw, mirror)
        copied.add(relative.as_posix())
    return copied


def _validate_schema(raw, contract):
    allowed = contract["allowed_fields"]
    required = contract["required_fields"]
    field_types = contract["field_types"]
    if not all(isinstance(item, str) for item in allowed + required):
        raise PortableAgentError("invalid_output_contract")
    try:
        value = history_contract_v2.parse_json_bytes(raw, allowed_fields=allowed)
    except (history_contract_v2.ContractV2Error, TypeError) as exc:
        raise PortableAgentError("malformed_output") from exc
    if not isinstance(value, dict) or not set(required).issubset(value):
        raise PortableAgentError("schema_mismatch")
    types = {"string": str, "integer": int, "boolean": bool, "object": dict, "array": list}
    for field, type_name in field_types.items():
        expected = types.get(type_name)
        if field not in allowed or expected is None or field not in value or type(value[field]) is not expected:
            raise PortableAgentError("schema_mismatch")
    return value


def _validate_response_schema_contract(schema):
    if (
        type(schema) is not dict
        or set(schema)
        != {"additionalProperties", "properties", "required", "type"}
        or schema.get("additionalProperties") is not False
        or schema.get("type") != "object"
        or schema.get("required")
        != [
            "schema_version",
            "stage",
            "request_attestation",
            "artifacts",
        ]
        or type(schema.get("properties")) is not dict
        or set(schema["properties"])
        != {"schema_version", "stage", "request_attestation", "artifacts"}
    ):
        raise PortableAgentError("invalid_response_schema")
    properties = schema["properties"]
    version = properties["schema_version"]
    stage = properties["stage"]
    attestation = properties["request_attestation"]
    artifacts = properties["artifacts"]
    if (
        type(version) is not dict
        or set(version) != {"enum", "type"}
        or version.get("type") != "integer"
        or type(version.get("enum")) is not list
        or len(version["enum"]) != 1
        or type(version["enum"][0]) is not int
        or type(stage) is not dict
        or set(stage) != {"enum", "type"}
        or stage.get("type") != "string"
        or type(stage.get("enum")) is not list
        or len(stage["enum"]) != 1
        or type(stage["enum"][0]) is not str
        or type(attestation) is not dict
        or set(attestation)
        != {"additionalProperties", "properties", "required", "type"}
        or attestation.get("additionalProperties") is not False
        or attestation.get("type") != "object"
        or attestation.get("required")
        != [
            "schema_version",
            "provider_request_binding_sha256",
            "serialized_prompt_sha256",
        ]
        or type(attestation.get("properties")) is not dict
        or set(attestation["properties"])
        != {
            "schema_version",
            "provider_request_binding_sha256",
            "serialized_prompt_sha256",
        }
        or type(artifacts) is not dict
        or set(artifacts) != {"items", "maxItems", "minItems", "type"}
        or artifacts.get("type") != "array"
        or type(artifacts.get("minItems")) is not int
        or type(artifacts.get("maxItems")) is not int
        or artifacts["minItems"] <= 0
        or artifacts["minItems"] != artifacts["maxItems"]
    ):
        raise PortableAgentError("invalid_response_schema")
    attestation_properties = attestation["properties"]
    attestation_version = attestation_properties["schema_version"]
    if (
        type(attestation_version) is not dict
        or set(attestation_version) != {"enum", "type"}
        or attestation_version.get("type") != "string"
        or attestation_version.get("enum")
        != ["portable-stage-response-attestation-v1"]
        or any(
            value != {"type": "string"}
            for value in (
                attestation_properties[
                    "provider_request_binding_sha256"
                ],
                attestation_properties["serialized_prompt_sha256"],
            )
        )
    ):
        raise PortableAgentError("invalid_response_schema")
    item = artifacts["items"]
    if (
        type(item) is not dict
        or set(item)
        != {"additionalProperties", "properties", "required", "type"}
        or item.get("additionalProperties") is not False
        or item.get("type") != "object"
        or item.get("required") != ["artifact_kind", "content"]
        or type(item.get("properties")) is not dict
        or set(item["properties"]) != {"artifact_kind", "content"}
    ):
        raise PortableAgentError("invalid_response_schema")
    kind = item["properties"]["artifact_kind"]
    content = item["properties"]["content"]
    if (
        type(kind) is not dict
        or set(kind) != {"enum", "type"}
        or kind.get("type") != "string"
        or type(kind.get("enum")) is not list
        or len(kind["enum"]) != artifacts["minItems"]
        or any(type(value) is not str or not value for value in kind["enum"])
        or len(set(kind["enum"])) != len(kind["enum"])
        or type(content) is not dict
        or set(content) != {"maxLength", "type"}
        or content.get("type") != "string"
        or type(content.get("maxLength")) is not int
        or content["maxLength"] <= 0
    ):
        raise PortableAgentError("invalid_response_schema")
    return {
        "schema_version": version["enum"][0],
        "stage": stage["enum"][0],
        "artifact_kinds": tuple(kind["enum"]),
        "content_max_length": content["maxLength"],
    }


def _validate_response_value(value, contract):
    if (
        type(value) is not dict
        or set(value)
        != {"schema_version", "stage", "request_attestation", "artifacts"}
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != contract["schema_version"]
        or type(value.get("stage")) is not str
        or value["stage"] != contract["stage"]
        or type(value.get("artifacts")) is not list
        or len(value["artifacts"]) != len(contract["artifact_kinds"])
    ):
        raise PortableAgentError("schema_mismatch")
    attestation = value.get("request_attestation")
    if (
        type(attestation) is not dict
        or set(attestation)
        != {
            "schema_version",
            "provider_request_binding_sha256",
            "serialized_prompt_sha256",
        }
        or attestation.get("schema_version")
        != "portable-stage-response-attestation-v1"
        or any(
            type(attestation.get(name)) is not str
            or len(attestation[name]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in attestation[name]
            )
            for name in (
                "provider_request_binding_sha256",
                "serialized_prompt_sha256",
            )
        )
    ):
        raise PortableAgentError("schema_mismatch")
    for item, expected_kind in zip(
        value["artifacts"], contract["artifact_kinds"]
    ):
        if (
            type(item) is not dict
            or set(item) != {"artifact_kind", "content"}
            or type(item.get("artifact_kind")) is not str
            or item["artifact_kind"] != expected_kind
            or type(item.get("content")) is not str
            or len(item["content"]) > contract["content_max_length"]
        ):
            raise PortableAgentError("schema_mismatch")


def _validate_expected_response_attestation(value):
    fields = {
        "schema_version",
        "provider_request_binding_sha256",
        "serialized_prompt_sha256",
    }
    if (
        type(value) is not dict
        or set(value) != fields
        or value.get("schema_version")
        != "portable-stage-response-attestation-v1"
        or any(
            type(value.get(name)) is not str
            or len(value[name]) != 64
            or any(character not in "0123456789abcdef" for character in value[name])
            for name in (
                "provider_request_binding_sha256",
                "serialized_prompt_sha256",
            )
        )
    ):
        raise PortableAgentError("invalid_response_schema")
    return value


def _read_mirror_output(mirror, relative, maximum):
    root = pathlib.Path(mirror).resolve(strict=True)
    directory_descriptor = _open_absolute_directory_no_follow(
        root, "unsafe_output"
    )
    try:
        for component in relative.parts[:-1]:
            child = _open_directory_at(
                directory_descriptor, component, "unsafe_output"
            )
            if os.fstat(child).st_mode & 0o077:
                os.close(child)
                raise PortableAgentError("unsafe_output")
            os.close(directory_descriptor)
            directory_descriptor = child
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(
                relative.parts[-1], flags, dir_fd=directory_descriptor
            )
        except OSError as exc:
            raise PortableAgentError("unsafe_output") from exc
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_mode & 0o077
            ):
                raise PortableAgentError("unsafe_output")
            raw = os.read(descriptor, maximum + 1)
            if len(raw) > maximum or os.read(descriptor, 1):
                raise PortableAgentError("oversize")
            after = os.fstat(descriptor)
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise PortableAgentError("unstable_output")
            return raw
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_descriptor)


def _canonical_json_bytes(value):
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


def _pairs_without_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise PortableAgentError("malformed_output")
        value[key] = item
    return value


def _require_nfc_json(value):
    if isinstance(value, str):
        if unicodedata.normalize("NFC", value) != value:
            raise PortableAgentError("malformed_output")
        return
    if isinstance(value, list):
        for item in value:
            _require_nfc_json(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _require_nfc_json(key)
            _require_nfc_json(item)


def _parse_strict_json(raw, *, reject_floats, require_nfc):
    def parse_float(text):
        if reject_floats:
            raise PortableAgentError("malformed_output")
        value = float(text)
        if not math.isfinite(value):
            raise PortableAgentError("malformed_output")
        return value

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs_without_duplicates,
            parse_float=parse_float,
            parse_constant=lambda _: (_ for _ in ()).throw(
                PortableAgentError("malformed_output")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortableAgentError("malformed_output") from exc
    if require_nfc:
        _require_nfc_json(value)
    return value


def _parse_strict_model_json(raw):
    return _parse_strict_json(raw, reject_floats=True, require_nfc=True)


def _parse_canonical_stdout(raw):
    value = _parse_strict_model_json(raw)
    if raw != _canonical_json_bytes(value):
        raise PortableAgentError("noncanonical_output")
    return value


def _parse_grok_transport(raw):
    outer = _parse_strict_json(raw, reject_floats=False, require_nfc=False)
    if type(outer) is not dict:
        raise PortableAgentError("malformed_output")
    if type(outer.get("text")) is not str:
        raise PortableAgentError("malformed_output")
    if outer.get("stopReason") != "end_turn":
        raise PortableAgentError("malformed_output")
    return outer


def _grok_model_text_bytes(text):
    try:
        raw = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PortableAgentError("malformed_output") from exc
    opening = b"```json\n"
    line_starts = [0]
    line_starts.extend(
        index + 1 for index, byte in enumerate(raw) if byte == 0x0A
    )
    line_start_set = set(line_starts)
    fence_starts = [
        index for index in line_starts if raw.startswith(b"```", index)
    ]
    if not fence_starts and opening not in raw:
        return raw
    opening_starts = [
        index for index in line_starts if raw.startswith(opening, index)
    ]
    closing_start = len(raw) - len(b"```")
    has_terminal_closing = (
        closing_start in line_start_set and raw.endswith(b"```")
    )
    if (
        len(fence_starts) != 2
        or len(opening_starts) != 1
        or raw.count(opening) != 1
        or not has_terminal_closing
    ):
        raise PortableAgentError("malformed_output")
    opening_start = opening_starts[0]
    if closing_start <= opening_start or raw[closing_start - 1] != 0x0A:
        raise PortableAgentError("malformed_output")
    return raw[opening_start + len(opening) : closing_start - 1]


def _parse_provider_stdout(provider, raw):
    if provider != "grok":
        value = _parse_canonical_stdout(raw)
        return value, raw
    outer = _parse_grok_transport(raw)
    inner_raw = _grok_model_text_bytes(outer["text"])
    value = _parse_strict_model_json(inner_raw)
    return value, _canonical_json_bytes(value)


def _validate_contract(contract):
    fields = {
        "path", "max_bytes", "sha256", "allowed_fields", "required_fields",
        "field_types", "forbid_extra_files",
    }
    if not isinstance(contract, dict) or set(contract) != fields:
        raise PortableAgentError("invalid_output_contract")
    relative = _safe_relative(contract["path"], "output path")
    if not relative.as_posix().startswith("output/"):
        raise PortableAgentError("invalid_output_contract")
    if type(contract["max_bytes"]) is not int or contract["max_bytes"] < 0:
        raise PortableAgentError("invalid_output_contract")
    expected_sha = contract["sha256"]
    if (
        (
            expected_sha is not None
            and (
                not isinstance(expected_sha, str)
                or len(expected_sha) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in expected_sha
                )
            )
        )
        or type(contract["forbid_extra_files"]) is not bool
        or not isinstance(contract["allowed_fields"], list)
        or not isinstance(contract["required_fields"], list)
        or not isinstance(contract["field_types"], dict)
    ):
        raise PortableAgentError("invalid_output_contract")
    return relative


def _kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def _environment_is_scrubbed(name):
    if name in {
        "PWD",
        "OLDPWD",
        "INIT_CWD",
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "AGENT_CMD",
        "FRONT_CMD",
        "BACK_CMD",
        "RESEARCH_DIRECTION_FILE",
        "ENV",
        "BASH_ENV",
        "ZDOTDIR",
    }:
        return True
    return name.startswith(
        ("GIT_", "HISTORY_", "HUNT_", "AWR_", "SIDE_", "CONTAINED_")
    )


def _provider_environment(mirror, environment_delta):
    environment = {
        name: value
        for name, value in os.environ.items()
        if not _environment_is_scrubbed(name)
    }
    for name, value in environment_delta.items():
        if _environment_is_scrubbed(name):
            raise PortableAgentError("unsafe_environment")
        environment[name] = value
    temporary = pathlib.Path(mirror) / ".tmp"
    temporary.mkdir(mode=0o700)
    environment["TMPDIR"] = str(temporary)
    return environment


def _communicate_bounded(
    process,
    *,
    timeout_seconds,
    stdout_max_bytes,
    stderr_max_bytes=4096,
):
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    streams = {
        process.stdout.fileno(): (process.stdout, stdout, stdout_max_bytes, True),
        process.stderr.fileno(): (process.stderr, stderr, stderr_max_bytes, False),
    }
    for descriptor, (stream, _, _, _) in streams.items():
        os.set_blocking(descriptor, False)
        selector.register(stream, selectors.EVENT_READ, descriptor)
    deadline = time.monotonic() + timeout_seconds
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_group(process)
                raise PortableAgentError("timeout")
            events = selector.select(min(remaining, 0.1))
            if not events:
                if process.poll() is not None:
                    # Pipes may still contain buffered bytes; keep polling them.
                    continue
                continue
            for key, _ in events:
                descriptor = key.data
                stream, capture, maximum, enforce = streams[descriptor]
                try:
                    chunk = os.read(descriptor, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                if enforce and len(capture) + len(chunk) > maximum:
                    _kill_group(process)
                    raise PortableAgentError("oversize")
                if len(capture) < maximum:
                    capture.extend(chunk[: maximum - len(capture)])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _kill_group(process)
            raise PortableAgentError("timeout")
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            _kill_group(process)
            raise PortableAgentError("timeout") from exc
        return bytes(stdout), bytes(stderr)
    finally:
        selector.close()


def _validate_stdout_mirror(mirror, expected_files):
    observed = set()
    for current, directories, files in os.walk(
        mirror,
        topdown=True,
        followlinks=False,
    ):
        current_path = pathlib.Path(current)
        for name in list(directories):
            path = current_path / name
            try:
                info = path.lstat()
            except OSError as exc:
                raise PortableAgentError("unexpected_artifact") from exc
            if not stat.S_ISDIR(info.st_mode):
                raise PortableAgentError("unexpected_artifact")
        for name in files:
            path = current_path / name
            relative = path.relative_to(mirror).as_posix()
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise PortableAgentError("unexpected_artifact")
            observed.add(relative)
    if observed != set(expected_files):
        raise PortableAgentError("unexpected_artifact")


def _revalidate_provider_model_authority(capability):
    try:
        provider_adapters.revalidate_command_intent_for_launch(capability)
    except provider_adapters.ProviderResolutionError as exc:
        raise PortableAgentError(
            "provider_model_authority_changed"
        ) from exc


def run_portable_stdout_attempt(
    capability,
    *,
    inputs,
    prompt,
    response_schema,
    expected_response_attestation,
    state_root,
    timeout_seconds,
    max_stdout_bytes=128 * 1024,
):
    """Run one disposable mirror and import one canonical stdout envelope."""
    if not provider_adapters.command_intent_is_issued(capability):
        raise PortableAgentError("invalid_capability")
    _revalidate_provider_model_authority(capability)
    if not isinstance(prompt, str) or not prompt:
        raise PortableAgentError("invalid_prompt")
    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise PortableAgentError("invalid_timeout")
    if type(max_stdout_bytes) is not int or not 1 <= max_stdout_bytes <= 128 * 1024:
        raise PortableAgentError("invalid_output_contract")
    response_contract = _validate_response_schema_contract(response_schema)
    expected_attestation = _validate_expected_response_attestation(
        expected_response_attestation
    )
    root = pathlib.Path(state_root)
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise PortableAgentError("unsafe_state_root")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    attempt = pathlib.Path(tempfile.mkdtemp(prefix="attempt-", dir=root))
    os.chmod(attempt, 0o700)
    mirror = attempt / "mirror"
    mirror.mkdir(mode=0o700)
    process = None
    try:
        copied = _copy_inputs(inputs, mirror)
        argv, environment_delta = provider_adapters.render_command(
            capability,
            mirror,
            prompt,
        )
        environment = _provider_environment(mirror, environment_delta)
        _revalidate_provider_model_authority(capability)
        process = subprocess.Popen(
            argv,
            cwd=mirror,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        stdout, stderr = _communicate_bounded(
            process,
            timeout_seconds=timeout_seconds,
            stdout_max_bytes=max_stdout_bytes,
        )
        if process.returncode != 0:
            raise PortableAgentError(
                "nonzero_exit",
                {"returncode": process.returncode, "stderr": stderr},
            )
        _validate_stdout_mirror(mirror, copied)
        value, model_bytes = _parse_provider_stdout(capability.provider, stdout)
        _validate_response_value(value, response_contract)
        if _canonical_json_bytes(value["request_attestation"]) != _canonical_json_bytes(
            expected_attestation
        ):
            raise PortableAgentError("provider_request_attestation_mismatch")
        output_sha = hashlib.sha256(model_bytes).hexdigest()
        imports = root / "imports"
        _ensure_owner_tree(root, imports)
        imported = imports / (output_sha + ".json")
        if imported.exists():
            existing = _open_read_stable(
                imported,
                max_stdout_bytes,
                "unsafe_import",
                require_owner_only=True,
            )
            if existing != model_bytes:
                raise PortableAgentError("import_conflict")
        else:
            _write_owner_only(imported, model_bytes, root)
            _fsync_directory(imports)
        return {
            "provider": capability.provider,
            "execution_request_profile_hash": capability.profile_hash,
            "model_envelope_sha256": output_sha,
            "output_path": str(imported),
            "value": value,
            "raw": model_bytes,
            "stderr": stderr,
        }
    except OSError as exc:
        raise PortableAgentError("process_error") from exc
    finally:
        if process is not None and process.poll() is None:
            _kill_group(process)
        shutil.rmtree(attempt, ignore_errors=True)


def run_portable_attempt(
    capability,
    *,
    inputs,
    output_contract,
    prompt,
    state_root,
    timeout_seconds,
):
    """Run one disposable mirror and import one validated output."""
    if not provider_adapters.capability_is_issued(capability):
        raise PortableAgentError("invalid_capability")
    if not isinstance(prompt, str) or not prompt:
        raise PortableAgentError("invalid_prompt")
    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise PortableAgentError("invalid_timeout")
    relative_output = _validate_contract(output_contract)
    root = pathlib.Path(state_root)
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise PortableAgentError("unsafe_state_root")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    attempt = pathlib.Path(tempfile.mkdtemp(prefix="attempt-", dir=root))
    os.chmod(attempt, 0o700)
    mirror = attempt / "mirror"
    mirror.mkdir(mode=0o700)
    process = None
    try:
        copied = _copy_inputs(inputs, mirror)
        argv, environment_delta = provider_adapters.render_command(
            capability, mirror, prompt
        )
        environment = _provider_environment(mirror, environment_delta)
        process = subprocess.Popen(
            argv,
            cwd=mirror,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        stdout, stderr = _communicate_bounded(
            process,
            timeout_seconds=timeout_seconds,
            stdout_max_bytes=4096,
        )
        if process.returncode != 0:
            raise PortableAgentError(
                "nonzero_exit",
                {"returncode": process.returncode, "stderr": stderr[:4096]},
            )
        output_path = mirror.joinpath(*relative_output.parts)
        if output_contract["forbid_extra_files"]:
            observed = {
                path.relative_to(mirror).as_posix()
                for path in mirror.rglob("*")
                if path.is_symlink() or not path.is_dir()
            }
            expected = copied | {relative_output.as_posix()}
            if observed != expected:
                raise PortableAgentError("unexpected_artifact")
        raw = _read_mirror_output(
            mirror, relative_output, output_contract["max_bytes"]
        )
        output_sha = hashlib.sha256(raw).hexdigest()
        expected_sha = output_contract["sha256"]
        if expected_sha is not None and output_sha != expected_sha:
            raise PortableAgentError("output_sha_mismatch")
        value = _validate_schema(raw, output_contract)
        imports = root / "imports"
        _ensure_owner_tree(root, imports)
        imported = imports / (output_sha + ".json")
        if imported.exists():
            existing = _open_read_stable(
                imported, output_contract["max_bytes"], "unsafe_import",
                require_owner_only=True,
            )
            if existing != raw:
                raise PortableAgentError("import_conflict")
        else:
            _write_owner_only(imported, raw, root)
            _fsync_directory(imports)
        return {
            "provider": capability.provider,
            "capability_profile_hash": capability.profile_hash,
            "output_sha256": output_sha,
            "output_path": str(imported),
            "value": value,
            "stdout": stdout[:4096],
        }
    except OSError as exc:
        raise PortableAgentError("process_error") from exc
    finally:
        if process is not None and process.poll() is None:
            _kill_group(process)
        shutil.rmtree(attempt, ignore_errors=True)
