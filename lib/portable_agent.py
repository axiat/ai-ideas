"""Disposable manifest-only agent mirrors for history audit v2."""

import hashlib
import json
import os
import pathlib
import secrets
import shutil
import signal
import stat
import subprocess
import tempfile

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
            part in {".git", ".ai-ideas", ".codex", "durable-state"}
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
    process.communicate()


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
    if not isinstance(capability, provider_adapters.ProviderCapability):
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
        environment = os.environ.copy()
        environment.update(environment_delta)
        process = subprocess.Popen(
            argv,
            cwd=mirror,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            _kill_group(process)
            process = None
            raise PortableAgentError("timeout") from exc
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
        raw = _open_read_stable(
            output_path, output_contract["max_bytes"], "unsafe_output",
            require_owner_only=True,
        )
        output_parent = output_path.parent.lstat()
        if not stat.S_ISDIR(output_parent.st_mode) or output_parent.st_mode & 0o077:
            raise PortableAgentError("unsafe_output")
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
