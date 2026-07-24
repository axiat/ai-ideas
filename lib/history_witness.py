#!/usr/bin/env python3
"""Closed subprocess boundary for production calibration witnesses."""

import hashlib
import json
import os
import pathlib
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import time


PROTOCOL = "history-calibration-witness-v1"
REQUEST_DOMAIN = b"history-calibration-witness-request-v1\0"
PRODUCTION_SCOPE = "production"
PROCESS_TIMEOUT_SECONDS = 5.0
TRUST_ROOT_MAX_BYTES = 65536
ARTIFACT_MAX_BYTES = 1024 * 1024
EXECUTABLE_MAX_BYTES = 64 * 1024 * 1024
STDOUT_MAX_BYTES = 4096
STDERR_MAX_BYTES = 16384
ARGV_ITEM_MAX_BYTES = 4096
ARGV_TOTAL_MAX_BYTES = 16384
ARTIFACT_KINDS = frozenset(
    {"preheldout_receipt", "calibration_capability"}
)
_TRUST_ROOT_FIELDS = {
    "schema_version",
    "scope",
    "trust_root_id",
    "verifier_protocol",
    "verifier_argv",
    "verifier_executable_sha256",
    "verifier_interpreter_sha256",
}
_RESPONSE_FIELDS = {
    "schema_version",
    "protocol",
    "request_sha256",
    "verified",
}
_MINIMAL_ENVIRONMENT = {
    "PATH": "/usr/bin:/bin",
    "LANG": "C",
    "LC_ALL": "C",
}
_NATIVE_EXECUTABLE_MAGICS = frozenset(
    {
        b"\x7fELF",
        b"\xfe\xed\xfa\xce",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xcf\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf",
        b"\xbf\xba\xfe\xca",
    }
)


class WitnessError(RuntimeError):
    pass


def canonical_bytes(value):
    try:
        text = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise WitnessError("witness value is not canonical JSON") from exc
    try:
        return (text + "\n").encode("utf-8")
    except UnicodeEncodeError as exc:
        raise WitnessError(
            "witness value is not canonical JSON"
        ) from exc


def sha256(raw):
    if not isinstance(raw, bytes):
        raise TypeError("witness hash input must be bytes")
    return hashlib.sha256(raw).hexdigest()


def _valid_sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _safe_absolute(path):
    try:
        absolute_text = os.path.abspath(os.fspath(path))
    except TypeError as exc:
        raise WitnessError(
            "production trust root must be a filesystem path"
        ) from exc
    for alias in ("/var", "/tmp", "/etc"):
        if (
            absolute_text == alias
            or absolute_text.startswith(alias + os.sep)
        ):
            try:
                alias_state = os.lstat(alias)
            except OSError:
                break
            if (
                stat.S_ISLNK(alias_state.st_mode)
                and alias_state.st_uid == 0
            ):
                absolute_text = (
                    os.path.realpath(alias)
                    + absolute_text[len(alias):]
                )
            break
    return pathlib.Path(absolute_text)


def _open_safe_directory(path):
    absolute = _safe_absolute(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open("/", flags)
    except OSError as exc:
        raise WitnessError(
            "witness filesystem root is unavailable"
        ) from exc
    try:
        for component in absolute.parts[1:]:
            if component in {"", ".", ".."}:
                raise WitnessError(
                    "witness path is not normalized"
                )
            child = os.open(component, flags, dir_fd=descriptor)
            current = os.fstat(child)
            if not stat.S_ISDIR(current.st_mode):
                os.close(child)
                raise WitnessError(
                    "witness parent is not a directory"
                )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        raise WitnessError(
            "witness parent is unavailable"
        ) from exc
    except Exception:
        os.close(descriptor)
        raise


def _artifact_name(path):
    name = pathlib.Path(path).name
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\x00" in name
    ):
        raise WitnessError("witness artifact name is invalid")
    return name


def _identity(state):
    return (
        state.st_dev,
        state.st_ino,
        state.st_size,
        state.st_mtime_ns,
        state.st_ctime_ns,
        state.st_mode,
        state.st_nlink,
    )


def _capture_regular(path, label, maximum):
    target = _safe_absolute(path)
    directory = None
    descriptor = None
    try:
        directory = _open_safe_directory(target.parent)
        name = _artifact_name(target)
        before = os.stat(
            name,
            dir_fd=directory,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size < 1
            or before.st_size > maximum
        ):
            raise WitnessError(
                f"{label} is not a bounded single-link regular file"
            )
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(name, flags, dir_fd=directory)
        opened = os.fstat(descriptor)
        chunks = []
        byte_count = 0
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            byte_count += len(chunk)
            if byte_count > maximum:
                raise WitnessError(f"{label} exceeds its byte bound")
            chunks.append(chunk)
        after = os.fstat(descriptor)
    except WitnessError:
        raise
    except OSError as exc:
        raise WitnessError(f"{label} is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)
    if (
        _identity(before) != _identity(opened)
        or _identity(before) != _identity(after)
        or byte_count != before.st_size
    ):
        raise WitnessError(f"{label} changed during capture")
    return {
        "path": str(target),
        "raw": b"".join(chunks),
        "identity": _identity(before),
        "mode": before.st_mode,
    }


def _load_production_trust_root(path):
    captured = _capture_regular(
        path, "production trust root", TRUST_ROOT_MAX_BYTES
    )
    raw = captured["raw"]
    try:
        root = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise WitnessError(
            "production trust root is invalid JSON"
        ) from exc
    if raw != canonical_bytes(root):
        raise WitnessError(
            "production trust root is not canonical JSON"
        )
    if (
        not isinstance(root, dict)
        or set(root) != _TRUST_ROOT_FIELDS
        or type(root.get("schema_version")) is not int
        or root["schema_version"] != 1
        or root.get("scope") != PRODUCTION_SCOPE
        or root.get("verifier_protocol") != PROTOCOL
        or not isinstance(root.get("trust_root_id"), str)
        or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}",
            root["trust_root_id"],
        )
        or not _valid_sha256(
            root.get("verifier_executable_sha256")
        )
        or not (
            root.get("verifier_interpreter_sha256") is None
            or _valid_sha256(
                root.get("verifier_interpreter_sha256")
            )
        )
    ):
        raise WitnessError(
            "production trust root schema is invalid"
        )
    argv = root.get("verifier_argv")
    if (
        not isinstance(argv, list)
        or len(argv) != 1
    ):
        raise WitnessError(
            "production verifier argv is invalid"
        )
    total = 0
    for item in argv:
        if (
            not isinstance(item, str)
            or not item
            or "\x00" in item
        ):
            raise WitnessError(
                "production verifier argv is invalid"
            )
        encoded = item.encode("utf-8")
        if len(encoded) > ARGV_ITEM_MAX_BYTES:
            raise WitnessError(
                "production verifier argv exceeds its byte bound"
            )
        total += len(encoded)
    if (
        total > ARGV_TOTAL_MAX_BYTES
        or not pathlib.Path(argv[0]).is_absolute()
    ):
        raise WitnessError(
            "production verifier argv is not absolute and bounded"
        )
    return root, raw


def _require_executable_capture(captured, label):
    if (
        not captured["mode"]
        & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    ):
        raise WitnessError(
            f"{label} is not executable"
        )
    if captured["mode"] & (stat.S_IWGRP | stat.S_IWOTH):
        raise WitnessError(
            f"{label} is externally writable"
        )


def _is_native_executable(captured):
    return (
        captured["raw"][:4] in _NATIVE_EXECUTABLE_MAGICS
    )


def _capture_pinned_executable(root):
    captured = _capture_regular(
        root["verifier_argv"][0],
        "production verifier executable",
        EXECUTABLE_MAX_BYTES,
    )
    if captured["path"] != root["verifier_argv"][0]:
        raise WitnessError(
            "production verifier executable path is not canonical"
        )
    _require_executable_capture(
        captured, "production verifier executable"
    )
    digest = sha256(captured["raw"])
    if digest != root["verifier_executable_sha256"]:
        raise WitnessError(
            "production verifier executable hash changed"
        )
    captured["sha256"] = digest
    return captured


def _capture_direct_interpreter(executable, expected_sha256):
    if _is_native_executable(executable):
        if expected_sha256 is not None:
            raise WitnessError(
                "native production verifier must use a null "
                "interpreter hash"
            )
        return None
    if expected_sha256 is None:
        raise WitnessError(
            "script production verifier requires an "
            "interpreter hash"
        )
    raw = executable["raw"]
    line_end = raw.find(b"\n")
    if (
        not raw.startswith(b"#!")
        or line_end < 3
    ):
        raise WitnessError(
            "production verifier is not a native executable "
            "or direct shebang script"
        )
    encoded_path = raw[2:line_end]
    if (
        len(encoded_path) > ARGV_ITEM_MAX_BYTES
        or b"\x00" in encoded_path
        or any(
            character in b" \t\r\v\f"
            for character in encoded_path
        )
    ):
        raise WitnessError(
            "production verifier shebang is invalid"
        )
    interpreter_text = os.fsdecode(encoded_path)
    interpreter_path = pathlib.Path(interpreter_text)
    if (
        not interpreter_path.is_absolute()
        or os.path.normpath(interpreter_text)
        != interpreter_text
        or interpreter_text == "/usr/bin/env"
    ):
        raise WitnessError(
            "production verifier shebang is not direct"
        )
    captured = _capture_regular(
        interpreter_path,
        "production verifier interpreter",
        EXECUTABLE_MAX_BYTES,
    )
    if captured["path"] != interpreter_text:
        raise WitnessError(
            "production verifier interpreter path is not canonical"
        )
    _require_executable_capture(
        captured, "production verifier interpreter"
    )
    if not _is_native_executable(captured):
        raise WitnessError(
            "production verifier interpreter is not native"
        )
    captured["sha256"] = sha256(captured["raw"])
    if captured["sha256"] != expected_sha256:
        raise WitnessError(
            "production verifier interpreter hash changed"
        )
    raise WitnessError(
        "script production verifier is unsupported without "
        "a pinned runtime"
    )


def _write_staged_capture(
    directory, name, captured, label
):
    destination = directory / name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = None
    try:
        descriptor = os.open(destination, flags, 0o700)
        remaining = memoryview(captured["raw"])
        while remaining:
            written = os.write(descriptor, remaining[:65536])
            if written <= 0:
                raise WitnessError(
                    f"{label} staging made no progress"
                )
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o500)
        staged = os.fstat(descriptor)
        if (
            not stat.S_ISREG(staged.st_mode)
            or staged.st_nlink != 1
            or staged.st_size != len(captured["raw"])
        ):
            raise WitnessError(
                f"{label} staging is invalid"
            )
    except (OSError, WitnessError) as exc:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        try:
            os.unlink(destination)
        except OSError:
            pass
        if isinstance(exc, WitnessError):
            raise
        raise WitnessError(
            f"{label} could not be staged"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        verified = _capture_regular(
            destination,
            f"staged {label}",
            EXECUTABLE_MAX_BYTES,
        )
        if sha256(verified["raw"]) != captured["sha256"]:
            raise WitnessError(
                f"staged {label} hash changed"
            )
    except WitnessError:
        try:
            os.unlink(destination)
        except OSError:
            pass
        raise
    return destination


def _stage_captured_executables(executable, interpreter):
    directory = pathlib.Path(
        tempfile.mkdtemp(prefix="history-witness-exec-")
    )
    os.chmod(directory, 0o700)
    destinations = []
    try:
        staged_executable = _write_staged_capture(
            directory,
            "verifier",
            executable,
            "production verifier",
        )
        destinations.append(staged_executable)
        staged_interpreter = None
        if interpreter is not None:
            staged_interpreter = _write_staged_capture(
                directory,
                "interpreter",
                interpreter,
                "production verifier interpreter",
            )
            destinations.append(staged_interpreter)
    except WitnessError:
        _remove_staged_executables(
            directory, *destinations
        )
        raise
    return (
        directory,
        staged_executable,
        staged_interpreter,
    )


def _remove_staged_executables(directory, *destinations):
    for destination in destinations:
        if destination is None:
            continue
        try:
            os.unlink(destination)
        except OSError:
            pass
    try:
        os.rmdir(directory)
    except OSError:
        pass


def _kill_process_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _close_registered(selector, pipe):
    try:
        selector.unregister(pipe)
    except (KeyError, OSError, ValueError):
        pass
    try:
        pipe.close()
    except OSError:
        pass


def _bounded_exchange(argv, request_raw, executable_path):
    try:
        process = subprocess.Popen(
            list(argv),
            executable=os.fspath(executable_path),
            cwd="/",
            env=dict(_MINIMAL_ENVIRONMENT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            start_new_session=True,
            bufsize=0,
        )
    except OSError as exc:
        raise WitnessError(
            "production witness verifier could not start"
        ) from exc
    selector = selectors.DefaultSelector()
    pipes = (process.stdin, process.stdout, process.stderr)
    output = {"stdout": bytearray(), "stderr": bytearray()}
    pending = memoryview(request_raw)
    failure = None
    try:
        for pipe in pipes:
            os.set_blocking(pipe.fileno(), False)
        selector.register(
            process.stdin, selectors.EVENT_WRITE, "stdin"
        )
        selector.register(
            process.stdout, selectors.EVENT_READ, "stdout"
        )
        selector.register(
            process.stderr, selectors.EVENT_READ, "stderr"
        )
        deadline = time.monotonic() + PROCESS_TIMEOUT_SECONDS
        while selector.get_map():
            now = time.monotonic()
            if now >= deadline:
                failure = WitnessError(
                    "production witness verifier timed out"
                )
                break
            events = selector.select(
                timeout=min(0.02, deadline - now)
            )
            for key, _ in events:
                name = key.data
                pipe = key.fileobj
                if name == "stdin":
                    try:
                        written = os.write(
                            pipe.fileno(), pending[:65536]
                        )
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        failure = WitnessError(
                            "production witness verifier "
                            "closed its request input"
                        )
                        break
                    if written <= 0:
                        failure = WitnessError(
                            "production witness verifier "
                            "request made no progress"
                        )
                        break
                    pending = pending[written:]
                    if not pending:
                        _close_registered(selector, pipe)
                    continue
                maximum = (
                    STDOUT_MAX_BYTES
                    if name == "stdout"
                    else STDERR_MAX_BYTES
                )
                try:
                    chunk = os.read(
                        pipe.fileno(),
                        min(
                            65536,
                            maximum - len(output[name]) + 1,
                        ),
                    )
                except BlockingIOError:
                    continue
                if not chunk:
                    _close_registered(selector, pipe)
                    continue
                output[name].extend(chunk)
                if len(output[name]) > maximum:
                    failure = WitnessError(
                        "production witness verifier "
                        f"{name} exceeds its byte bound"
                    )
                    break
            if failure is not None:
                break
        if failure is not None:
            _kill_process_group(process)
            raise failure
        remaining = max(0.0, deadline - time.monotonic())
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            _kill_process_group(process)
            raise WitnessError(
                "production witness verifier timed out"
            ) from exc
        if return_code != 0:
            raise WitnessError(
                "production witness verifier failed"
            )
        return bytes(output["stdout"]), bytes(output["stderr"])
    except WitnessError:
        raise
    except OSError as exc:
        _kill_process_group(process)
        raise WitnessError(
            "production witness verifier I/O failed"
        ) from exc
    finally:
        for pipe in pipes:
            _close_registered(selector, pipe)
        selector.close()
        _kill_process_group(process)


def _request(root, artifact_kind, artifact):
    if artifact_kind not in ARTIFACT_KINDS:
        raise WitnessError(
            "production witness artifact kind is invalid"
        )
    if (
        not isinstance(artifact, dict)
        or artifact.get("scope") != PRODUCTION_SCOPE
        or artifact.get("trust_root_id")
        != root["trust_root_id"]
    ):
        raise WitnessError(
            "production witness artifact root binding is invalid"
        )
    artifact_raw = canonical_bytes(artifact)
    if len(artifact_raw) > ARTIFACT_MAX_BYTES:
        raise WitnessError(
            "production witness artifact exceeds its byte bound"
        )
    artifact_sha = sha256(artifact_raw)
    request = {
        "schema_version": 1,
        "protocol": PROTOCOL,
        "trust_root_id": root["trust_root_id"],
        "artifact_kind": artifact_kind,
        "artifact_sha256": artifact_sha,
        "artifact": artifact,
    }
    raw = canonical_bytes(request)
    if len(raw) > ARTIFACT_MAX_BYTES + 4096:
        raise WitnessError(
            "production witness request exceeds its byte bound"
        )
    return raw, artifact_sha


def _validate_response(raw, request_sha):
    try:
        response = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise WitnessError(
            "production witness response is invalid JSON"
        ) from exc
    if raw != canonical_bytes(response):
        raise WitnessError(
            "production witness response is not canonical JSON"
        )
    if (
        not isinstance(response, dict)
        or set(response) != _RESPONSE_FIELDS
        or type(response.get("schema_version")) is not int
        or response["schema_version"] != 1
        or response.get("protocol") != PROTOCOL
        or response.get("request_sha256") != request_sha
        or response.get("verified") is not True
    ):
        raise WitnessError(
            "production witness response binding is invalid"
        )


def verify_production_artifact(
    trust_root_path, artifact_kind, artifact
):
    """Verify with staged copies of all captured executable bytes.

    The operating-system loader and dynamically linked platform
    libraries remain part of the trusted computing base.
    """
    root, root_raw = _load_production_trust_root(
        trust_root_path
    )
    request_raw, artifact_sha = _request(
        root, artifact_kind, artifact
    )
    request_sha = sha256(REQUEST_DOMAIN + request_raw)
    before = _capture_pinned_executable(root)
    interpreter_before = _capture_direct_interpreter(
        before, root["verifier_interpreter_sha256"]
    )
    (
        stage_directory,
        staged_executable,
        staged_interpreter,
    ) = _stage_captured_executables(
        before, interpreter_before
    )
    if staged_interpreter is None:
        launch_argv = root["verifier_argv"]
        launch_executable = staged_executable
    else:
        launch_argv = [
            os.fspath(staged_interpreter),
            os.fspath(staged_executable),
        ]
        launch_executable = staged_interpreter
    exchange_error = None
    stdout = None
    try:
        try:
            stdout, _ = _bounded_exchange(
                launch_argv,
                request_raw,
                launch_executable,
            )
        except WitnessError as exc:
            exchange_error = exc
    finally:
        _remove_staged_executables(
            stage_directory,
            staged_executable,
            staged_interpreter,
        )
    try:
        after = _capture_pinned_executable(root)
    except WitnessError as exc:
        raise WitnessError(
            "production verifier executable changed after launch"
        ) from exc
    if (
        after["identity"] != before["identity"]
        or after["sha256"] != before["sha256"]
    ):
        raise WitnessError(
            "production verifier executable changed after launch"
        )
    try:
        interpreter_after = _capture_direct_interpreter(
            after, root["verifier_interpreter_sha256"]
        )
    except WitnessError as exc:
        raise WitnessError(
            "production verifier interpreter changed after launch"
        ) from exc
    if (
        (interpreter_before is None)
        != (interpreter_after is None)
        or (
            interpreter_before is not None
            and (
                interpreter_after["identity"]
                != interpreter_before["identity"]
                or interpreter_after["sha256"]
                != interpreter_before["sha256"]
            )
        )
    ):
        raise WitnessError(
            "production verifier interpreter changed after launch"
        )
    if exchange_error is not None:
        raise exchange_error
    _validate_response(stdout, request_sha)
    return {
        "schema_version": 1,
        "trust_root_id": root["trust_root_id"],
        "trust_root_sha256": sha256(root_raw),
        "verifier_executable_sha256": before["sha256"],
        "verifier_interpreter_sha256":
            root["verifier_interpreter_sha256"],
        "artifact_kind": artifact_kind,
        "artifact_sha256": artifact_sha,
        "request_sha256": request_sha,
    }
