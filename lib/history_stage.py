#!/usr/bin/env python3
"""Fail-closed OS containment for bounded history-aware agent stages."""

import argparse
import hashlib
import json
import os
import pathlib
import platform
import re
import secrets
import selectors
import shutil
import signal
import sqlite3
import stat
import subprocess
import tempfile
import time

try:
    from lib import history_budget
    from lib import direction_contract as direction_contract_lib
    from lib import history_projection
    from lib import history_retrieval
    from lib import history_runtime
    from lib import history_stage_adapter
    from lib import history_stage_proxy
except ImportError:
    import history_budget
    import direction_contract as direction_contract_lib
    import history_projection
    import history_retrieval
    import history_runtime
    import history_stage_adapter
    import history_stage_proxy


ROOT = pathlib.Path(__file__).resolve().parents[1]
POLICY_SOURCE = "history/retrieval-policy-v1.json"
GENERATION_POLICY_SOURCE = "brainstorming_policy.md"
ADAPTER_SOURCE = "lib/history_stage_adapter.py"
CANONICALIZER_SOURCE = "lib/history_stage_proxy.py"
CODEX_CAPABILITY_SOURCE = "history/codex-adapter-capabilities-v2.json"
CODEX_AUTH_PATH = pathlib.Path.home() / ".codex" / "auth.json"
# Fallback when the binary cannot report a version (offline / renamed).
CODEX_CLI_VERSION = "0.146.0"
CODEX_UPSTREAM = {
    "scheme": "https",
    "host": "chatgpt.com",
    "port": 443,
    "path": "/backend-api/codex/responses",
}
ADAPTER_VERSION = "history-stage-v1"
FIXED_WRAPPER = "history-stage-prompt-v1"
MANIFEST_MAX_BYTES = 1024 * 1024
# Contained generate/review with xhigh reasoning routinely exceeds 30s.
PROCESS_TIMEOUT_SECONDS = 600
DARWIN_SANDBOX_EXEC = pathlib.Path("/usr/bin/sandbox-exec")
LINUX_BWRAP = pathlib.Path("/usr/bin/bwrap")


class StageError(RuntimeError):
    pass


_INPUT_CAPS = {
    "generation_brief.json": 65536,
    "generation_policy.md": 16384,
    "research_context.md": 65536,
    "direction_constraint.json": 16384,
    "retrieval_pack.json": 65536,
    "candidate.json": 16384,
    "prior_work.md": 16384,
    "review_contract.md": 16384,
    "history_summary.json": 16384,
    "failure_batch.json": 65536,
}

_OUTPUT_PROFILES = {
    "generate": {
        "generation-ideas-tsv": ("output/ideas.tsv", 65536),
        "generation-ideas-markdown": ("output/ideas.md", 65536),
        "prompt-attestation-json": (
            "output/prompt-attestation.json",
            4096,
        ),
    },
    "history-compare": {
        "history-comparison-json": (
            "output/history-comparison.json",
            65536,
        ),
        "prompt-attestation-json": (
            "output/prompt-attestation.json",
            4096,
        ),
    },
    "review": {
        "review-markdown": ("output/review.md", 65536),
        "review-verdict-tsv": ("output/verdict.tsv", 16384),
        "prompt-attestation-json": (
            "output/prompt-attestation.json",
            4096,
        ),
    },
    "meta": {
        "failure-distillation-json": (
            "output/failure-distillation.json",
            65536,
        ),
        "prompt-attestation-json": (
            "output/prompt-attestation.json",
            4096,
        ),
    },
}

_STAGE_PROFILES = {
    "generate": {
        "role": "roles/generate.md",
        "required_inputs": {
            "generation_brief.json",
            "generation_policy.md",
        },
        "optional_inputs": {
            "research_context.md",
            "direction_constraint.json",
        },
        "message": "Generate bounded candidates.",
    },
    "history-compare": {
        "role": "roles/history-compare.md",
        "required_inputs": {"retrieval_pack.json"},
        "optional_inputs": set(),
        "message": "Compare the candidate.",
    },
    "review": {
        "role": "roles/review.md",
        "required_inputs": {
            "candidate.json",
            "prior_work.md",
            "review_contract.md",
        },
        "optional_inputs": {"history_summary.json"},
        "message": "Review the bounded candidate.",
    },
    "meta": {
        "role": "roles/meta.md",
        "required_inputs": {"failure_batch.json"},
        "optional_inputs": set(),
        "message": "Distill the bounded failure batch.",
    },
}

_MANIFEST_FIELDS = {
    "schema_version",
    "stage",
    "seat_id",
    "adapter",
    "policy",
    "role",
    "input_roots",
    "inputs",
    "invocation",
    "output_roots",
    "outputs",
    "preflight_receipt_destination",
    "completion_receipt_destination",
    "registered_runtime_reads",
    "registered_environment",
    "history_store",
}
_INVOCATION_FIELDS = {
    "candidate",
    "retrieval_payload",
    "receipts",
    "tool_schemas",
    "messages",
    "output_schema_instructions",
    "expected_serialized_sha256",
}
_TEST_ENVIRONMENT_FIELDS = {
    "HISTORY_STAGE_ATTACK_MODE",
    "HISTORY_STAGE_COMPARATOR_STATUS",
    "HISTORY_STAGE_REVIEW_VERDICT",
    "HISTORY_STAGE_INPUT_PATH",
    "HISTORY_STAGE_OUTSIDE_WRITE",
    "HISTORY_STAGE_SEAT_ID",
    "HISTORY_STAGE_SENTINELS_JSON",
    "HISTORY_STAGE_SIBLING",
}
_TEST_ATTACK_MODES = {
    "none",
    "delayed-child",
    "detached-child",
    "fork-probe",
    "rapid-double-fork",
    "extra-output",
    "wrong-attestation",
    "missing-output",
    "malformed-comparator",
    "invalid-utf8",
    "oversized-output",
    "symlink-output",
    "hardlink-output",
    "fifo-output",
    "nonzero",
    "timeout",
    "meta-missing",
    "meta-duplicate",
    "meta-extra",
    "meta-wrong-vocabulary",
    "meta-wrong-order",
    "generation-missing-field",
    "generation-id-mismatch",
    "generation-duplicate-id",
    "review-missing-field",
    "review-vote-mismatch",
    "review-major-mismatch",
    "review-gate-violation",
    "stdout-overflow",
    "stderr-overflow",
}
_FORBIDDEN_COMMAND_ARGUMENTS = {
    "--search",
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-skip-permissions",
}


def _canonical_bytes(value):
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


def _compact_json_bytes(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _valid_sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _codex_upstream_endpoint():
    return dict(CODEX_UPSTREAM)


def _normal_relative(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise StageError("path must be nonempty text")
    if "\\" in value:
        raise StageError("path must use POSIX separators")
    candidate = pathlib.PurePosixPath(value)
    if (
        candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or str(candidate) != value
    ):
        raise StageError("path is not a canonical relative path")
    return value


def _read_fd(fd, maximum):
    chunks = []
    total = 0
    while True:
        chunk = os.read(fd, min(65536, maximum + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum:
            raise StageError("file exceeds its byte limit")


def _regular_stat(
    fd, maximum, *, single_link=True, reject_sparse=True
):
    value = os.fstat(fd)
    if not stat.S_ISREG(value.st_mode):
        raise StageError("path is not a regular file")
    if single_link and value.st_nlink != 1:
        raise StageError("regular file must have exactly one link")
    if value.st_size < 0 or value.st_size > maximum:
        raise StageError("file exceeds its byte limit")
    if (
        reject_sparse
        and value.st_size
        and hasattr(value, "st_blocks")
        and value.st_blocks * 512 < value.st_size
    ):
        raise StageError("sparse files are not accepted")
    return value


def _capture_regular_path(path, maximum, *, reject_sparse=True):
    path = pathlib.Path(path)
    try:
        before = path.lstat()
    except OSError as exc:
        raise StageError("required file is unavailable") from exc
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise StageError("required file is not a single-link regular file")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise StageError("required file cannot be opened safely") from exc
    try:
        opened = _regular_stat(
            fd, maximum, reject_sparse=reject_sparse
        )
        raw = _read_fd(fd, maximum)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    identity = (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
    )
    if identity != (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) or identity != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise StageError("file identity changed during capture")
    return {
        "path": str(path),
        "raw": raw,
        "sha256": _sha256(raw),
        "identity": identity,
        "mode": opened.st_mode,
        "uid": opened.st_uid,
    }


def _capture_codex_auth(path=CODEX_AUTH_PATH):
    """Read only the broker fields from one canonical Codex auth file."""
    path = pathlib.Path(path)
    try:
        before = path.lstat()
    except OSError as exc:
        raise StageError("auth_refresh_required") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_uid != os.getuid()
        or before.st_mode & 0o077
        or before.st_size < 1
        or before.st_size > 256 * 1024
    ):
        raise StageError("auth_refresh_required")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise StageError("auth_refresh_required") from exc
    try:
        opened = os.fstat(descriptor)
        raw = _read_fd(descriptor, 256 * 1024)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity = (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
    )
    if (
        identity
        != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        or identity
        != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
    ):
        raise StageError("auth_refresh_required")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise StageError("auth_refresh_required") from exc
    # Require the ChatGPT session fields. Newer Codex CLI may write extra
    # top-level keys (e.g. OPENAI_API_KEY, often null after login); ignore them
    # and never return them to the broker.
    required_top = {"auth_mode", "tokens", "last_refresh"}
    tokens = value.get("tokens") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or not required_top.issubset(value)
        or value.get("auth_mode") != "chatgpt"
        or not isinstance(value.get("last_refresh"), str)
        or not value["last_refresh"]
        or not isinstance(tokens, dict)
        or set(tokens)
        != {
            "id_token",
            "access_token",
            "refresh_token",
            "account_id",
        }
        or any(
            not isinstance(tokens.get(name), str)
            or not tokens[name]
            or len(tokens[name].encode("utf-8")) > 128 * 1024
            for name in (
                "id_token",
                "access_token",
                "refresh_token",
                "account_id",
            )
        )
    ):
        raise StageError("auth_refresh_required")
    return {
        "access_token": tokens["access_token"],
        "account_id": tokens["account_id"],
        "source": {
            "path_kind": "canonical-codex-auth-v1",
            "identity": identity,
            "mode": stat.S_IMODE(opened.st_mode),
            "uid": opened.st_uid,
        },
        "_path": str(path),
    }


def _revalidate_codex_auth(auth):
    if auth is None:
        return
    try:
        current = pathlib.Path(auth["_path"]).lstat()
    except OSError as exc:
        raise StageError("auth_refresh_required") from exc
    identity = (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
    )
    source = auth["source"]
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or current.st_uid != source["uid"]
        or stat.S_IMODE(current.st_mode) != source["mode"]
        or identity != tuple(source["identity"])
    ):
        raise StageError("auth_refresh_required")


def capture_regular_input(root_fd, relative_path, declared_sha256, max_bytes):
    """Capture one no-follow, single-link, bounded regular input."""
    relative_path = _normal_relative(relative_path)
    if not _valid_sha256(declared_sha256):
        raise StageError("declared input hash is invalid")
    if type(max_bytes) is not int or max_bytes < 1:
        raise StageError("input byte limit is invalid")
    parts = pathlib.PurePosixPath(relative_path).parts
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            next_fd = os.open(part, flags, dir_fd=current)
            value = os.fstat(next_fd)
            if not stat.S_ISDIR(value.st_mode):
                os.close(next_fd)
                raise StageError("input path component is not a directory")
            os.close(current)
            current = next_fd
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK
        candidate_stat = os.stat(
            parts[-1],
            dir_fd=current,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(candidate_stat.st_mode)
            or candidate_stat.st_nlink != 1
        ):
            raise StageError(
                "input is not a single-link regular file"
            )
        fd = os.open(parts[-1], flags, dir_fd=current)
        try:
            before = _regular_stat(fd, max_bytes)
            raw = _read_fd(fd, max_bytes)
            after = os.fstat(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise StageError("input cannot be opened safely") from exc
    finally:
        os.close(current)
    identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    if identity != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise StageError("input changed during capture")
    if _sha256(raw) != declared_sha256:
        raise StageError("input hash mismatch")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StageError("mounted inputs must be UTF-8") from exc
    return {
        "relative_path": relative_path,
        "raw": raw,
        "sha256": declared_sha256,
        "identity": identity,
    }


def _load_json_bytes(raw, label, *, require_canonical=True):
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise StageError(f"{label} is not valid UTF-8 JSON") from exc
    if require_canonical and raw != _canonical_bytes(value):
        raise StageError(f"{label} is not canonical JSON")
    return value


def load_manifest(path, stage_profile):
    """Load a closed canonical manifest from a safe regular file."""
    captured = _capture_regular_path(path, MANIFEST_MAX_BYTES)
    manifest = _load_json_bytes(captured["raw"], "manifest")
    if (
        not isinstance(manifest, dict)
        or set(manifest) != _MANIFEST_FIELDS
        or manifest.get("schema_version") != 1
    ):
        raise StageError("closed manifest schema mismatch")
    if stage_profile not in _STAGE_PROFILES:
        raise StageError("unsupported stage")
    if manifest.get("stage") != stage_profile:
        raise StageError("CLI and manifest stage mismatch")
    seat_id = manifest.get("seat_id")
    if (
        not isinstance(seat_id, str)
        or not seat_id
        or len(seat_id.encode("utf-8")) > 128
        or any(character in seat_id for character in "\r\n\x00")
    ):
        raise StageError("seat ID is invalid")
    manifest["_manifest_capture"] = captured
    return manifest


def parse_command_json(value):
    try:
        command = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise StageError("command must be JSON") from exc
    if (
        not isinstance(command, list)
        or not command
        or any(
            not isinstance(item, str) or not item or "\x00" in item
            for item in command
        )
    ):
        raise StageError("command must be a nonempty JSON string array")
    if any(item in _FORBIDDEN_COMMAND_ARGUMENTS for item in command):
        raise StageError("command requests a forbidden capability")
    return command


def _root_identity(value, label):
    if not isinstance(value, str) or not pathlib.Path(value).is_absolute():
        raise StageError(f"{label} root must be absolute")
    path = pathlib.Path(value)
    try:
        current = path.lstat()
    except OSError as exc:
        raise StageError(f"{label} root is unavailable") from exc
    if not stat.S_ISDIR(current.st_mode):
        raise StageError(f"{label} root is not a directory")
    resolved = path.resolve(strict=True)
    broad = {
        pathlib.Path("/").resolve(),
        pathlib.Path.home().resolve(),
        ROOT.resolve(),
        ROOT.resolve().parent,
    }
    if resolved in broad:
        raise StageError(f"{label} root is too broad")
    return {
        "path": path,
        "resolved": resolved,
        "identity": (current.st_dev, current.st_ino),
    }


def _validate_root_list(value, label):
    if not isinstance(value, list) or len(value) != 1:
        raise StageError(f"exactly one {label} root is required")
    return _root_identity(value[0], label)


def _repo_artifact(descriptor, expected_source, label):
    if (
        not isinstance(descriptor, dict)
        or set(descriptor) != {"source", "sha256"}
        or descriptor.get("source") != expected_source
        or not _valid_sha256(descriptor.get("sha256"))
    ):
        raise StageError(f"{label} authority mismatch")
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        root_fd = os.open(ROOT, flags)
    except OSError as exc:
        raise StageError("repository root cannot be opened safely") from exc
    try:
        captured = capture_regular_input(
            root_fd,
            expected_source,
            descriptor["sha256"],
            MANIFEST_MAX_BYTES,
        )
    finally:
        os.close(root_fd)
    captured["path"] = str(ROOT / expected_source)
    captured["source"] = expected_source
    try:
        captured["text"] = captured["raw"].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StageError(f"{label} is not UTF-8") from exc
    return captured


def _stage_policy_artifact(descriptor, command_argv):
    if (
        isinstance(descriptor, dict)
        and set(descriptor) == {"source", "sha256"}
        and descriptor.get("source") == POLICY_SOURCE
    ):
        return _repo_artifact(
            descriptor, POLICY_SOURCE, "policy"
        )
    fields = {
        "source",
        "host_path",
        "sha256",
        "authority_scope",
    }
    test_backends = {
        (ROOT / "tests" / "fake_stage_agent.py").resolve(),
        (ROOT / "tests" / "malicious_history_agent.py").resolve(),
    }
    try:
        resolved_backend = pathlib.Path(command_argv[0]).resolve(
            strict=True
        )
    except (IndexError, OSError) as exc:
        raise StageError("synthetic policy backend is unavailable") from exc
    if (
        not isinstance(descriptor, dict)
        or set(descriptor) != fields
        or descriptor.get("source") != "synthetic_contract_only"
        or descriptor.get("authority_scope")
        != "synthetic_contract_only"
        or not isinstance(descriptor.get("host_path"), str)
        or not pathlib.Path(descriptor["host_path"]).is_absolute()
        or not _valid_sha256(descriptor.get("sha256"))
        or len(command_argv) != 1
        or resolved_backend not in test_backends
    ):
        raise StageError("policy authority mismatch")
    captured = _capture_regular_path(
        descriptor["host_path"], MANIFEST_MAX_BYTES
    )
    if captured["sha256"] != descriptor["sha256"]:
        raise StageError("policy input hash mismatch")
    registered = _load_json_bytes(
        _capture_regular_path(
            ROOT / POLICY_SOURCE, MANIFEST_MAX_BYTES
        )["raw"],
        "registered stage policy",
        require_canonical=False,
    )
    synthetic = _load_json_bytes(
        captured["raw"],
        "synthetic stage policy",
        require_canonical=False,
    )
    expected = dict(registered)
    expected["mode"] = "enforcement"
    if synthetic != expected:
        raise StageError(
            "synthetic policy differs beyond enforcement mode"
        )
    captured["source"] = descriptor["host_path"]
    captured["external_policy"] = True
    return captured


def _validate_adapter(manifest, policy):
    descriptor = manifest.get("adapter")
    if (
        not isinstance(descriptor, dict)
        or set(descriptor)
        != {
            "version",
            "fixed_wrapper",
            "wrapper_allowance",
            "executable_source",
            "executable_sha256",
            "canonicalizer_source",
            "canonicalizer_sha256",
        }
        or descriptor.get("version") != ADAPTER_VERSION
        or descriptor.get("fixed_wrapper") != FIXED_WRAPPER
        or descriptor.get("wrapper_allowance")
        != policy.get("adapter_wrapper_allowance")
        or descriptor.get("version") != policy.get("adapter_version")
        or policy.get("tested_adapter_allowances", {}).get(ADAPTER_VERSION)
        != descriptor.get("wrapper_allowance")
    ):
        raise StageError("adapter registry mismatch")
    return {
        "executable": _repo_artifact(
            {
                "source": descriptor["executable_source"],
                "sha256": descriptor["executable_sha256"],
            },
            ADAPTER_SOURCE,
            "adapter executable",
        ),
        "canonicalizer": _repo_artifact(
            {
                "source": descriptor["canonicalizer_source"],
                "sha256": descriptor["canonicalizer_sha256"],
            },
            CANONICALIZER_SOURCE,
            "adapter canonicalizer",
        ),
    }


def _validate_inputs(manifest, profile):
    root = _validate_root_list(manifest.get("input_roots"), "input")
    entries = manifest.get("inputs")
    if not isinstance(entries, list):
        raise StageError("inputs must be a list")
    allowed = profile["required_inputs"] | profile["optional_inputs"]
    seen_sources = set()
    seen_mirrors = set()
    captured = {}
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        root_fd = os.open(root["path"], flags)
    except OSError as exc:
        raise StageError("input root cannot be opened safely") from exc
    try:
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or set(entry)
                != {"source", "mirror_path", "sha256", "max_bytes"}
            ):
                raise StageError("input entry schema mismatch")
            source = _normal_relative(entry["source"])
            mirror = _normal_relative(entry["mirror_path"])
            if source in seen_sources or mirror in seen_mirrors:
                raise StageError("input paths must be unique")
            seen_sources.add(source)
            seen_mirrors.add(mirror)
            if mirror not in allowed:
                raise StageError("input is outside the stage registry")
            if (
                entry["max_bytes"] != _INPUT_CAPS[mirror]
                or not _valid_sha256(entry["sha256"])
            ):
                raise StageError("input limit or hash authority mismatch")
            value = capture_regular_input(
                root_fd,
                source,
                entry["sha256"],
                entry["max_bytes"],
            )
            value["source"] = source
            value["mirror_path"] = mirror
            value["max_bytes"] = entry["max_bytes"]
            captured[mirror] = value
    finally:
        os.close(root_fd)
    if (
        not profile["required_inputs"].issubset(captured)
        or not set(captured).issubset(allowed)
    ):
        raise StageError("stage input set mismatch")
    return root, captured


def _destination_parent(root, relative):
    relative = _normal_relative(relative)
    parts = pathlib.PurePosixPath(relative).parts
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    parent_fd = None
    try:
        parent_fd = os.open(root["path"], flags)
        root_stat = os.fstat(parent_fd)
        if (
            root_stat.st_dev,
            root_stat.st_ino,
        ) != root["identity"]:
            raise StageError("destination root identity changed")
        current_path = root["path"]
        for component in parts[:-1]:
            next_fd = os.open(component, flags, dir_fd=parent_fd)
            next_stat = os.fstat(next_fd)
            if not stat.S_ISDIR(next_stat.st_mode):
                os.close(next_fd)
                raise StageError(
                    "destination parent is not a directory"
                )
            os.close(parent_fd)
            parent_fd = next_fd
            current_path = current_path / component
        parent_stat = os.fstat(parent_fd)
        target_name = parts[-1]
        try:
            existing = os.stat(
                target_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode)
            or existing.st_nlink != 1
        ):
            raise StageError(
                "destination is not a replaceable regular file"
            )
        return {
            "relative": relative,
            "target_name": target_name,
            "target_existed": existing is not None,
            "parent_path": current_path,
            "parent_fd": parent_fd,
            "parent_identity": (
                parent_stat.st_dev,
                parent_stat.st_ino,
            ),
        }
    except OSError as exc:
        if parent_fd is not None:
            os.close(parent_fd)
        raise StageError("destination cannot be opened safely") from exc
    except Exception:
        if parent_fd is not None:
            os.close(parent_fd)
        raise


def _close_destination_guard(guard):
    descriptor = guard.get("parent_fd", -1)
    if descriptor >= 0:
        os.close(descriptor)
        guard["parent_fd"] = -1


def _validate_outputs(manifest, stage):
    root = _validate_root_list(manifest.get("output_roots"), "output")
    entries = manifest.get("outputs")
    expected = _OUTPUT_PROFILES[stage]
    if not isinstance(entries, list) or len(entries) != len(expected):
        raise StageError("output allowlist mismatch")
    seen_mirrors = set()
    seen_destinations = set()
    seen_kinds = set()
    guards = []
    try:
        result = []
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or set(entry)
                != {
                    "mirror_path",
                    "destination",
                    "artifact_kind",
                    "max_bytes",
                    "required",
                }
            ):
                raise StageError("output entry schema mismatch")
            mirror = _normal_relative(entry["mirror_path"])
            destination = _normal_relative(entry["destination"])
            kind = entry["artifact_kind"]
            if (
                kind not in expected
                or expected[kind] != (mirror, entry["max_bytes"])
                or entry["required"] is not True
                or mirror in seen_mirrors
                or destination in seen_destinations
                or kind in seen_kinds
            ):
                raise StageError("output authority mismatch")
            seen_mirrors.add(mirror)
            seen_destinations.add(destination)
            seen_kinds.add(kind)
            guard = _destination_parent(root, destination)
            guards.append(guard)
            result.append(dict(entry, destination_guard=guard))
        if seen_kinds != set(expected):
            raise StageError("required output kind is missing")
        preflight = _destination_parent(
            root, manifest.get("preflight_receipt_destination")
        )
        guards.append(preflight)
        completion = _destination_parent(
            root, manifest.get("completion_receipt_destination")
        )
        guards.append(completion)
        receipt_paths = {
            preflight["relative"],
            completion["relative"],
        }
        if (
            len(receipt_paths) != 2
            or receipt_paths.intersection(seen_destinations)
            or preflight["target_existed"]
            or completion["target_existed"]
        ):
            raise StageError("receipt destination collision")
        return root, result, preflight, completion
    except Exception:
        for guard in guards:
            _close_destination_guard(guard)
        raise


def _parse_stage_inputs(stage, captured, policy):
    parsed = {}
    for name in (
        "generation_brief.json",
        "retrieval_pack.json",
        "candidate.json",
        "history_summary.json",
        "failure_batch.json",
    ):
        if name in captured:
            parsed[name] = _load_json_bytes(captured[name]["raw"], name)
    if stage == "generate":
        brief = parsed["generation_brief.json"]
        if (
            not isinstance(brief, dict)
            or set(brief)
            != {
                "schema_version",
                "retrieval_policy_version",
                "source_watermark",
                "index_generation",
                "theme_counts",
                "failure_code_counts",
                "divergence_lens",
                "parent",
                "research_context",
                "estimated_tokens",
            }
            or brief.get("schema_version") != 1
        ):
            raise StageError("generation brief schema mismatch")
        registered_policy = _capture_regular_path(
            ROOT / GENERATION_POLICY_SOURCE,
            _INPUT_CAPS["generation_policy.md"],
        )
        if (
            captured["generation_policy.md"]["raw"]
            != registered_policy["raw"]
        ):
            raise StageError(
                "generation policy is not the registered version"
            )
        if (
            "research_context.md" in captured
            and brief.get("research_context") not in (None, "", [])
        ):
            raise StageError("research context has two representations")
        if "direction_constraint.json" in captured:
            try:
                contract, canonical_raw, _ = (
                    direction_contract_lib.parse_contract_bytes(
                        captured["direction_constraint.json"]["raw"]
                    )
                )
            except direction_contract_lib.DirectionContractError as exc:
                raise StageError("direction contract is invalid") from exc
            if canonical_raw != captured["direction_constraint.json"]["raw"]:
                raise StageError("direction contract is not canonical")
            parsed["direction_constraint.json"] = contract
    elif stage == "history-compare":
        if not isinstance(parsed["retrieval_pack.json"], dict):
            raise StageError("retrieval pack must be an object")
    elif stage == "review":
        candidate = parsed["candidate.json"]
        if (
            not isinstance(candidate, dict)
            or not isinstance(candidate.get("candidate_id"), str)
            or not candidate["candidate_id"]
        ):
            raise StageError("review candidate is invalid")
        contract = _capture_regular_path(
            ROOT / "history" / "review-contract-v1.md",
            _INPUT_CAPS["review_contract.md"],
        )
        if captured["review_contract.md"]["raw"] != contract["raw"]:
            raise StageError("review contract is not the registered version")
        summary = parsed.get("history_summary.json")
        if (
            summary is not None
            and policy.get("mode") != "enforcement"
        ):
            raise StageError(
                "shadow review cannot mount history evidence"
            )
        if summary is not None and (
            not isinstance(summary, dict)
            or set(summary)
            != {
                "schema_version",
                "candidate_id",
                "candidate_content_sha256",
                "adapter_version",
                "receipts",
                "aggregate_sha256",
            }
            or summary.get("schema_version") != 1
            or summary.get("candidate_id")
            != candidate["candidate_id"]
            or not _valid_sha256(
                summary.get("candidate_content_sha256")
            )
            or not isinstance(summary.get("adapter_version"), str)
            or not isinstance(summary.get("receipts"), list)
            or not 2 <= len(summary["receipts"]) <= 3
            or not _valid_sha256(summary.get("aggregate_sha256"))
        ):
            raise StageError("history summary is not receipt-bound")
    elif stage == "meta":
        batch = parsed["failure_batch.json"]
        if (
            not isinstance(batch, dict)
            or set(batch)
            != {
                "schema_version",
                "failure_codes",
                "themes",
                "items",
            }
            or batch.get("schema_version") != 1
            or not isinstance(batch.get("items"), list)
            or not isinstance(batch.get("failure_codes"), list)
            or not isinstance(batch.get("themes"), list)
        ):
            raise StageError("failure batch schema mismatch")
        for vocabulary in ("failure_codes", "themes"):
            values = batch[vocabulary]
            if (
                not values
                or len(values) > 128
                or "unmapped" not in values
                or len(values) != len(set(values))
                or any(
                    not isinstance(value, str)
                    or not value
                    or len(value.encode("utf-8")) > 128
                    for value in values
                )
            ):
                raise StageError("failure batch vocabulary mismatch")
        if len(batch["items"]) > 128:
            raise StageError("failure batch item bound exceeded")
        source_ids = []
        for item in batch["items"]:
            if (
                not isinstance(item, dict)
                or set(item) != {"source_id", "reason"}
                or not isinstance(item["source_id"], str)
                or not item["source_id"]
                or len(item["source_id"].encode("utf-8")) > 128
                or not isinstance(item["reason"], str)
                or not item["reason"]
                or len(item["reason"].encode("utf-8")) > 2048
            ):
                raise StageError("failure batch item schema mismatch")
            source_ids.append(item["source_id"])
        if len(source_ids) != len(set(source_ids)):
            raise StageError("failure batch source IDs are duplicated")
    return parsed


def _validate_history_authority(stage, reference, parsed_inputs, policy):
    summary = parsed_inputs.get("history_summary.json")
    needs_store = (
        stage in {"generate", "history-compare"}
        or (stage == "review" and summary is not None)
    )
    if not needs_store:
        if reference is not None:
            raise StageError(
                "history store is not allowed for this stage"
            )
        return None
    if (
        not isinstance(reference, dict)
        or set(reference) != {"root", "source"}
        or reference.get("source") != "history.sqlite3"
    ):
        raise StageError("history store authority mismatch")
    root = _root_identity(reference["root"], "history store")
    source = _normal_relative(reference["source"])
    database = root["path"] / source
    try:
        before = database.lstat()
    except OSError as exc:
        raise StageError("history store is unavailable") from exc
    identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size < 1
        or before.st_size > 4 * 1024 * 1024 * 1024
    ):
        raise StageError("history store is not a bounded regular file")
    connection = None
    try:
        connection = sqlite3.connect(
            database.resolve(strict=True).as_uri() + "?mode=ro",
            uri=True,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN")
        pack = None
        if stage == "generate":
            expected = (
                history_projection._build_generation_brief_snapshot(
                    connection,
                    policy,
                    divergence_lens=parsed_inputs[
                        "generation_brief.json"
                    ]["divergence_lens"],
                )
            )
            if parsed_inputs["generation_brief.json"] != expected:
                raise StageError(
                    "generation brief is not the current host projection"
                )
        elif stage == "history-compare":
            pack = parsed_inputs["retrieval_pack.json"]
            history_retrieval._validate_pack(
                connection,
                pack,
                policy,
                require_complete=True,
            )
        else:
            history_runtime.verify_history_summary(
                connection,
                parsed_inputs["candidate.json"],
                summary,
                policy,
            )
        connection.execute("ROLLBACK")
    except (
        OSError,
        sqlite3.Error,
        history_projection.ProjectionError,
        history_retrieval.RetrievalError,
        history_runtime.RuntimeContractError,
    ) as exc:
        raise StageError("history authority validation failed") from exc
    finally:
        if connection is not None:
            connection.close()
    try:
        after = database.lstat()
        root_after = root["path"].lstat()
    except OSError as exc:
        raise StageError("history store drifted during validation") from exc
    if identity != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or root["identity"] != (
        root_after.st_dev,
        root_after.st_ino,
    ):
        raise StageError("history store drifted during validation")
    return {
        "reference": dict(reference),
        "root": root,
        "identity": identity,
        "pack_publication_id": (
            None
            if pack is None
            else pack["pack_publication_id"]
        ),
        "pack_sha256": (
            None if pack is None else pack["pack_sha256"]
        ),
    }


def build_stage_invocation(profile, manifest, captured_inputs, role, policy):
    """Build and validate the one canonical prompt byte string."""
    invocation = manifest.get("invocation")
    if not isinstance(invocation, dict) or set(invocation) != _INVOCATION_FIELDS:
        raise StageError("closed invocation manifest mismatch")
    stage = manifest["stage"]
    parsed = _parse_stage_inputs(stage, captured_inputs, policy)
    mounted = {
        name: value["raw"] for name, value in captured_inputs.items()
    }
    candidate = None
    retrieval_payload = None
    receipts = []
    tool_schemas = []
    if stage == "history-compare":
        retrieval_payload = parsed["retrieval_pack.json"]
        try:
            candidate = retrieval_payload["query"]
            receipts = [
                {
                    "pack_publication_id": retrieval_payload[
                        "pack_publication_id"
                    ],
                    "role_identity": profile["role"],
                    "role_sha256": role["sha256"],
                }
            ]
            tool_schemas = [
                history_retrieval.comparator_output_schema(
                    retrieval_payload, policy
                )
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise StageError("comparator pack invocation is invalid") from exc
    elif stage == "review":
        candidate = parsed["candidate.json"]
    expected = {
        "candidate": candidate,
        "retrieval_payload": retrieval_payload,
        "receipts": receipts,
        "tool_schemas": tool_schemas,
        "messages": [
            {"role": "user", "content": profile["message"]}
        ],
        "output_schema_instructions": None,
    }
    if any(invocation.get(key) != value for key, value in expected.items()):
        raise StageError("invocation fields are not host-derived")
    try:
        if stage == "history-compare":
            serialized = history_retrieval.comparator_invocation_bytes(
                retrieval_payload,
                policy,
                role_bytes=role["raw"],
                role_identity=profile["role"],
            )
        else:
            serialized = history_budget.serialize_stage_invocation(
                stage=stage,
                adapter_version=ADAPTER_VERSION,
                fixed_instructions=role["text"],
                mounted_inputs=mounted,
                **expected,
            )
    except (KeyError, TypeError, ValueError, history_retrieval.RetrievalError) as exc:
        raise StageError("stage invocation cannot be serialized") from exc
    if (
        not _valid_sha256(invocation["expected_serialized_sha256"])
        or invocation["expected_serialized_sha256"] != _sha256(serialized)
    ):
        raise StageError("serialized invocation attestation mismatch")
    return serialized, mounted, parsed


def preflight_stage_invocation(serialized, policy, captured_inputs):
    try:
        return history_budget.preflight_stage_invocation(
            serialized,
            policy,
            expected_mounted_inputs={
                name: value["raw"]
                for name, value in captured_inputs.items()
            },
        )
    except history_budget.PreflightError as exc:
        raise StageError(exc.code) from exc


def _validate_test_environment(value, seat_id):
    if not isinstance(value, dict) or set(value) != _TEST_ENVIRONMENT_FIELDS:
        raise StageError("test adapter environment registry mismatch")
    if value.get("HISTORY_STAGE_SEAT_ID") != seat_id:
        raise StageError("test adapter seat mismatch")
    if value.get("HISTORY_STAGE_ATTACK_MODE") not in _TEST_ATTACK_MODES:
        raise StageError("unknown test adapter mode")
    if value.get("HISTORY_STAGE_COMPARATOR_STATUS") not in {
        "complete_match",
        "complete_no_match",
        "uncertain",
        "conflicting_evidence",
    }:
        raise StageError("unknown test comparator status")
    if value.get("HISTORY_STAGE_REVIEW_VERDICT") not in {
        "strong-accept",
        "accept-w-rev",
        "reject",
    }:
        raise StageError("unknown test review verdict")
    try:
        sentinels = json.loads(value["HISTORY_STAGE_SENTINELS_JSON"])
    except (TypeError, ValueError) as exc:
        raise StageError("test sentinel registry is invalid") from exc
    if (
        not isinstance(sentinels, list)
        or any(
            not isinstance(item, str)
            or not pathlib.Path(item).is_absolute()
            for item in sentinels
        )
    ):
        raise StageError("test sentinels must be absolute paths")
    for name in ("HISTORY_STAGE_OUTSIDE_WRITE", "HISTORY_STAGE_SIBLING"):
        if (
            not isinstance(value[name], str)
            or not pathlib.Path(value[name]).is_absolute()
        ):
            raise StageError("test adapter path is invalid")
    input_path = value["HISTORY_STAGE_INPUT_PATH"]
    _normal_relative(input_path)
    if not input_path.startswith("input/"):
        raise StageError("test input attack path is outside the mirror input")
    return dict(value)


def _parse_codex_prefix(command):
    if len(command) != 5 or command[1] != "-m" or command[3] != "-c":
        raise StageError("Codex prefix is outside the registered grammar")
    model = command[2]
    reasoning = command[4]
    allowed_reasoning = {
        "model_reasoning_effort=low",
        "model_reasoning_effort=medium",
        "model_reasoning_effort=high",
        "model_reasoning_effort=xhigh",
    }
    if (
        not model
        or model.startswith("-")
        or any(character in model for character in "\r\n\x00")
        or reasoning not in allowed_reasoning
    ):
        raise StageError("Codex prefix identity is invalid")
    return {
        "model": model,
        "reasoning_setting": reasoning,
    }


def codex_loopback_argv(
    executable,
    *,
    model,
    reasoning_effort,
    mirror,
    proxy_port,
    output_schema_path,
    output_last_message_path,
):
    """Build the pinned Codex 0.146.0 fake-provider integration argv."""
    executable = pathlib.Path(executable)
    mirror = pathlib.Path(mirror)
    schema = pathlib.Path(output_schema_path)
    final = pathlib.Path(output_last_message_path)
    try:
        schema.relative_to(mirror)
        final.relative_to(mirror)
    except ValueError as exc:
        raise StageError("Codex output paths must stay in the mirror") from exc
    if (
        not executable.is_absolute()
        or not isinstance(model, str)
        or not model
        or reasoning_effort not in {"low", "medium", "high", "xhigh"}
        or type(proxy_port) is not int
        or not 1 <= proxy_port <= 65535
        or not schema.is_file()
        or final.exists()
    ):
        raise StageError("Codex loopback configuration is invalid")
    provider = (
        'model_providers.history_loopback={name="history-loopback",'
        f'base_url="http://127.0.0.1:{proxy_port}/v1",'
        'wire_api="responses",requires_openai_auth=false,'
        "request_max_retries=0,stream_max_retries=0}"
    )
    return [
        str(executable),
        "-c",
        'model_provider="history_loopback"',
        "-c",
        provider,
        "-c",
        f'model_reasoning_effort="{reasoning_effort}"',
        "-m",
        model,
        "-c",
        "sandbox_workspace_write.network_access=false",
        "-c",
        "tools.web_search=false",
        "-c",
        'shell_environment_policy.inherit="none"',
        "-c",
        "project_doc_max_bytes=0",
        "-a",
        "never",
        "-s",
        "workspace-write",
        "exec",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--strict-config",
        "-C",
        str(mirror),
        "--output-schema",
        str(schema),
        "--output-last-message",
        str(final),
    ]


def _codex_loopback_template(identity):
    return [
        "<codex>",
        "-c",
        'model_provider="history_loopback"',
        "-c",
        (
            "model_providers.history_loopback={"
            'name="history-loopback",'
            'base_url="http://127.0.0.1:<proxy-port>/v1",'
            'wire_api="responses",requires_openai_auth=false,'
            "request_max_retries=0,stream_max_retries=0}"
        ),
        "-c",
        (
            'model_reasoning_effort="'
            + identity["reasoning_setting"].split("=", 1)[1]
            + '"'
        ),
        "-m",
        identity["model"],
        "-c",
        "sandbox_workspace_write.network_access=false",
        "-c",
        "tools.web_search=false",
        "-c",
        'shell_environment_policy.inherit="none"',
        "-c",
        "project_doc_max_bytes=0",
        "-a",
        "never",
        "-s",
        "workspace-write",
        "exec",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--strict-config",
        "-C",
        "<mirror>",
        "--output-schema",
        "<response-schema>",
        "--output-last-message",
        "<last-message>",
    ]


def _detect_codex_cli_version(executable_path):
    """Read `codex --version`; return None when it cannot be read."""
    path = pathlib.Path(executable_path)
    try:
        completed = subprocess.run(
            [str(path), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (completed.stdout or completed.stderr or "").strip()
    # Formats seen: "codex-cli 0.146.0", "0.146.0"
    for token in text.replace(",", " ").split():
        if token[0:1].isdigit() and token.count(".") >= 1:
            return token
    return None


def _codex_cli_version_family(version):
    """Normalize to major.minor so patch upgrades share one profile."""
    if not isinstance(version, str) or not version:
        return CODEX_CLI_VERSION
    if version in {"*", "any"}:
        return version
    parts = version.split(".")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        return f"{parts[0]}.{parts[1]}"
    return version


def _codex_cli_version_compatible(registered, observed):
    """Accept exact match, major.minor family match, or wildcard."""
    if not isinstance(registered, str) or not isinstance(observed, str):
        return False
    if registered in {"*", "any"}:
        return True
    if registered == observed:
        return True
    return _codex_cli_version_family(registered) == _codex_cli_version_family(
        observed
    )


def _codex_profile_bytes(
    binary_sha256,
    identity,
    policy,
    adapter_artifacts,
    *,
    codex_cli_version=None,
):
    """Build the audited capability profile.

    Binary SHA is recorded in preflight separately; the capability match
    intentionally omits it so Homebrew codex patch upgrades do not force a
    registry rewrite. CLI version is stored as major.minor family only.
    ``binary_sha256`` remains accepted for call-site compatibility but ignored.
    """
    del binary_sha256  # not part of the match profile (version-tolerant)
    schema_hashes = {
        stage: _sha256(
            _compact_json_bytes(
                history_stage_adapter.stage_response_schema(stage)
            )
        )
        for stage in sorted(_STAGE_PROFILES)
    }
    version_family = _codex_cli_version_family(
        codex_cli_version or CODEX_CLI_VERSION
    )
    profile = {
        "adapter_executable_sha256": adapter_artifacts[
            "executable"
        ]["sha256"],
        "canonical_request_version": getattr(
            history_stage_proxy,
            "CANONICAL_REQUEST_VERSION",
            "history-canonical-request-v1",
        ),
        "canonicalizer_sha256": adapter_artifacts[
            "canonicalizer"
        ]["sha256"],
        "codex_cli_version_family": version_family,
        "command_template": _codex_loopback_template(identity),
        "fixed_bounds": {
            "max_output_tokens": policy["max_output_tokens"],
            "model_context_limit": policy["model_context_limit"],
            "safety_margin": policy["safety_margin"],
        },
        "model": identity["model"],
        "platform": {
            "machine": platform.machine(),
            "system": "Darwin",
        },
        "reasoning_setting": identity["reasoning_setting"],
        "response_schema_sha256s": schema_hashes,
        "schema_version": 3,
        "auth_source_kind": "canonical-codex-auth-v1",
        "upstream": {
            **CODEX_UPSTREAM,
            "kind": "chatgpt-codex-responses-v1",
        },
    }
    return _canonical_bytes(profile)


def _validated_codex_capability(
    captured,
    identity,
    policy,
    adapter_artifacts,
):
    registry_capture = _capture_regular_path(
        ROOT / CODEX_CAPABILITY_SOURCE,
        1024 * 1024,
    )
    registry = _load_json_bytes(
        registry_capture["raw"],
        "Codex adapter capability registry",
    )
    if (
        not isinstance(registry, dict)
        or set(registry) != {"schema_version", "capabilities"}
        or registry.get("schema_version") not in (2, 3)
        or not isinstance(registry.get("capabilities"), list)
    ):
        raise StageError("Codex capability registry is invalid")
    captured_path = captured.get("path")
    observed_version = (
        _detect_codex_cli_version(captured_path) if captured_path else None
    )
    # Fail closed on version drift: when the binary reports a version, only
    # that version may match. The static pin is the offline fallback for
    # binaries that cannot report one (renamed / no --version output).
    effective_version = (
        observed_version if observed_version is not None else CODEX_CLI_VERSION
    )
    matches = []
    profile_sha256 = _sha256(
        _codex_profile_bytes(
            captured["sha256"],
            identity,
            policy,
            adapter_artifacts,
            codex_cli_version=effective_version,
        )
    )
    for item in registry["capabilities"]:
        if not isinstance(item, dict):
            continue
        keys = set(item)
        if not {
            "capability_id",
            "codex_cli_version",
            "profile_sha256",
        }.issubset(keys):
            continue
        if item.get("profile_sha256") != profile_sha256:
            continue
        if not _codex_cli_version_compatible(
            item.get("codex_cli_version"),
            effective_version,
        ):
            continue
        expected_id = _sha256(
            b"history-codex-capability-v3\0"
            + profile_sha256.encode("ascii")
        )
        # Accept v2 id formula for registries written before the
        # binary-agnostic profile (migration window).
        expected_id_v2 = _sha256(
            b"history-codex-capability-v2\0"
            + profile_sha256.encode("ascii")
        )
        if item.get("capability_id") not in (
            expected_id,
            expected_id_v2,
        ):
            continue
        matches.append(item)
    unique = []
    seen = set()
    for item in matches:
        key = item.get("capability_id")
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    if len(unique) != 1:
        raise StageError(
            "audited Codex adapter capability is unavailable"
        )
    return {
        **unique[0],
        "registry": registry_capture,
        "identity": identity,
        "observed_codex_cli_version": observed_version,
        "matched_profile_sha256": profile_sha256,
        "matched_codex_cli_version": effective_version,
    }


def _capture_python_runtime():
    candidates = (
        pathlib.Path(
            "/Applications/Xcode.app/Contents/Developer/usr/bin/python3"
        ),
        pathlib.Path("/usr/bin/python3"),
    )
    source = next((path for path in candidates if path.exists()), None)
    if source is None:
        raise StageError("registered Python runtime is unavailable")
    resolved = source.resolve(strict=True)
    interpreter = _capture_regular_path(
        resolved,
        32 * 1024 * 1024,
        reject_sparse=False,
    )
    executables = {"python3": interpreter}
    python_library = resolved.parent.parent / "Python3"
    if python_library.is_file():
        executables["python3-framework"] = _capture_regular_path(
            python_library,
            32 * 1024 * 1024,
            reject_sparse=False,
        )
    return interpreter, executables


def _capture_containment_executable(system):
    if system == "Darwin":
        fixed = DARWIN_SANDBOX_EXEC
    elif system == "Linux":
        fixed = LINUX_BWRAP
    else:
        raise StageError("no registered containment implementation")
    try:
        resolved = fixed.resolve(strict=True)
    except OSError as exc:
        raise StageError("containment executable is unavailable") from exc
    captured = _capture_regular_path(
        resolved,
        32 * 1024 * 1024,
        reject_sparse=False,
    )
    if (
        captured["uid"] != 0
        or not captured["mode"] & 0o111
        or captured["mode"] & 0o022
    ):
        raise StageError("containment executable ownership is unsafe")
    return captured


def _revalidate_captured_executable(captured, label):
    current = _capture_regular_path(
        captured["path"],
        32 * 1024 * 1024,
        reject_sparse=False,
    )
    if (
        current["identity"] != captured["identity"]
        or current["raw"] != captured["raw"]
    ):
        raise StageError(f"{label} drifted before launch")


def _is_codex_backend_basename(name):
    """True for Codex CLI basenames, including Homebrew realpath targets.

    Hunt normalizes contained argv[0] with realpath, so a `codex` symlink may
    become `codex-aarch64-apple-darwin` (or similar `codex-*`) before resolve.
    Exact `codex` and `codex-*` are the registered family; other names are not.
    """
    return isinstance(name, str) and (
        name == "codex" or name.startswith("codex-")
    )


def _resolve_backend(command, manifest, policy, adapter_artifacts):
    if not isinstance(command, list) or not command:
        raise StageError("resolved command is required")
    if any(
        not isinstance(item, str) or not item or "\x00" in item
        for item in command
    ):
        raise StageError("resolved command contains an invalid argument")
    if any(item in _FORBIDDEN_COMMAND_ARGUMENTS for item in command):
        raise StageError("backend command requests a forbidden capability")
    executable = pathlib.Path(command[0])
    if not executable.is_absolute():
        raise StageError("backend executable must be absolute")
    try:
        resolved = executable.resolve(strict=True)
    except OSError as exc:
        raise StageError("backend executable cannot be resolved") from exc
    captured = _capture_regular_path(
        resolved,
        512 * 1024 * 1024,
        reject_sparse=False,
    )
    backend_type = None
    wrapper_interpreter, runtime_executables = _capture_python_runtime()
    test_paths = {
        (ROOT / "tests" / "malicious_history_agent.py").resolve(),
        (ROOT / "tests" / "fake_stage_agent.py").resolve(),
    }
    if resolved in test_paths:
        if len(command) != 1:
            raise StageError("local fixture accepts no fixed argv")
        backend_type = "local-fixture"
        interpreter = wrapper_interpreter
        dependencies = {}
        environment = _validate_test_environment(
            manifest.get("registered_environment"),
            manifest["seat_id"],
        )
        if manifest.get("registered_runtime_reads") != []:
            raise StageError("local fixture accepts no runtime reads")
        bootstrap_files = {}
        codex_identity = None
        codex_capability = None
    elif (
        _is_codex_backend_basename(executable.name)
        and _is_codex_backend_basename(resolved.name)
    ):
        if platform.system() != "Darwin":
            raise StageError(
                "Codex containment is unavailable on this platform"
            )
        codex_identity = _parse_codex_prefix(
            [str(resolved)] + command[1:]
        )
        backend_type = "codex"
        if manifest.get("registered_environment") != {}:
            raise StageError("Codex inherits no manifest environment")
        if manifest.get("registered_runtime_reads") != []:
            raise StageError("Codex accepts no host file reads")
        codex_capability = _validated_codex_capability(
            captured,
            codex_identity,
            policy,
            adapter_artifacts,
        )
        codex_auth = _capture_codex_auth(CODEX_AUTH_PATH)
        bootstrap_files = {}
        environment = {}
        interpreter = None
        dependencies = {}
    else:
        raise StageError("backend is not registered")
    return {
        "type": backend_type,
        "original_argv": [str(resolved)] + command[1:],
        "source": captured,
        "environment": environment,
        "runtime_reads": [],
        "registered_runtime_reads": list(
            manifest["registered_runtime_reads"]
        ),
        "bootstrap_files": bootstrap_files,
        "interpreter": interpreter,
        "dependencies": dependencies,
        "runtime_executables": runtime_executables,
        "wrapper_interpreter": wrapper_interpreter,
        "codex_identity": codex_identity,
        "codex_capability": codex_capability,
        "codex_auth": (
            codex_auth if backend_type == "codex" else None
        ),
    }


def _mkdir_mode(path, mode):
    path.mkdir(parents=True, exist_ok=False)
    path.chmod(mode)


def _remove_mirror(path):
    for current, directories, files in os.walk(
        path, topdown=True, followlinks=False
    ):
        try:
            os.chmod(current, 0o700)
        except OSError:
            pass
        for name in directories:
            target = pathlib.Path(current) / name
            if not target.is_symlink():
                try:
                    target.chmod(0o700)
                except OSError:
                    pass
        for name in files:
            target = pathlib.Path(current) / name
            if not target.is_symlink():
                try:
                    target.chmod(0o600)
                except OSError:
                    pass

    def make_writable(function, target, _error):
        try:
            os.chmod(target, 0o700)
            function(target)
        except OSError:
            pass

    shutil.rmtree(path, onerror=make_writable)


def _write_mirror_file(root, relative, raw, mode):
    relative = _normal_relative(relative)
    path = root.joinpath(*pathlib.PurePosixPath(relative).parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise StageError("mirror path collision")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, mode)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    path.chmod(mode)
    return path


def _prepare_mirror(
    captured_inputs,
    backend,
    adapter,
    stage,
    seat_id,
    *,
    codex_proxy_port=None,
    response_schema=None,
):
    system = platform.system()
    parent = pathlib.Path("/private/tmp" if system == "Darwin" else "/tmp")
    if not parent.is_dir():
        raise StageError("explicit system temporary root is unavailable")
    mirror = pathlib.Path(
        tempfile.mkdtemp(prefix="ai-ideas-history-stage-", dir=parent)
    )
    mirror.chmod(0o700)
    try:
        for name, mode in (
            ("input", 0o755),
            ("output", 0o755),
            ("home", 0o700),
            ("tmp", 0o700),
            ("runtime", 0o755),
        ):
            _mkdir_mode(mirror / name, mode)
        for name, value in captured_inputs.items():
            _write_mirror_file(
                mirror / "input",
                name,
                value["raw"],
                0o444,
            )
        for directory in sorted(
            (mirror / "input").rglob("*"),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            if directory.is_dir():
                directory.chmod(0o555)
        (mirror / "input").chmod(0o555)
        adapter_executable = _write_mirror_file(
            mirror / "runtime",
            "history_stage_adapter.py",
            adapter["raw"],
            0o444,
        )
        if backend["type"] == "local-fixture":
            runtime_executable = _write_mirror_file(
                mirror / "runtime",
                "backend.py",
                backend["source"]["raw"],
                0o444,
            )
            for name, dependency in backend["dependencies"].items():
                _write_mirror_file(
                    mirror / "runtime",
                    name,
                    dependency["raw"],
                    0o444,
                )
            backend_command = [
                backend["wrapper_interpreter"]["path"],
                str(runtime_executable),
            ]
            execution = {
                "backend_command": backend_command,
                "response_schema": None,
                "model_output": None,
            }
        else:
            if (
                type(codex_proxy_port) is not int
                or not 1 <= codex_proxy_port <= 65535
                or not isinstance(response_schema, dict)
            ):
                raise StageError("Codex loopback is not provisioned")
            _mkdir_mode(mirror / "home" / ".codex", 0o700)
            schema_path = _write_mirror_file(
                mirror / "runtime",
                "output-schema.json",
                _compact_json_bytes(response_schema),
                0o444,
            )
            final_path = mirror / "tmp" / "model-final.json"
            backend_command = codex_loopback_argv(
                backend["source"]["path"],
                model=backend["codex_identity"]["model"],
                reasoning_effort=backend["codex_identity"][
                    "reasoning_setting"
                ].split("=", 1)[1],
                mirror=mirror,
                proxy_port=codex_proxy_port,
                output_schema_path=schema_path,
                output_last_message_path=final_path,
            )
            execution = {
                "backend_command": backend_command,
                "response_schema": schema_path,
                "model_output": final_path,
            }
        launch_prefix = [
            backend["wrapper_interpreter"]["path"],
            str(adapter_executable),
            stage,
            seat_id,
            "output/prompt-attestation.json",
            json.dumps(
                backend_command,
                separators=(",", ":"),
                ensure_ascii=False,
            ),
        ]
        for xdg in ("xdg-config", "xdg-cache", "xdg-data"):
            _mkdir_mode(mirror / xdg, 0o700)
        return mirror, launch_prefix, execution
    except Exception:
        _remove_mirror(mirror)
        raise


def _sandbox_quote(value):
    value = str(value)
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_darwin_launch(
    profile_path,
    mirror,
    command,
    runtime_reads,
    network,
    *,
    runtime_executables=(),
):
    """Return a literal sandbox-exec argv and its default-deny profile."""
    read_subpaths = [
        "/System",
        "/usr/bin",
        "/usr/lib",
        "/bin",
        "/sbin",
        "/etc",
        "/private/etc",
        "/dev",
        str(mirror),
    ]
    for optional in (
        "/Library/Apple",
        "/Library/Developer/CommandLineTools",
        "/Applications/Xcode.app",
    ):
        if pathlib.Path(optional).exists():
            read_subpaths.append(optional)
    metadata_literals = {
        "/Applications",
        "/Library",
        "/private",
        "/private/tmp",
        "/usr",
    }
    for registered_path in [*runtime_reads, *runtime_executables]:
        current = pathlib.Path(registered_path).parent
        while current != current.parent:
            metadata_literals.add(str(current))
            current = current.parent
    lines = [
        "(version 3)",
        "(deny default)",
        '(import "dyld-support.sb")',
        "(allow syscall*)",
        "(allow system-mac-syscall)",
        "(allow system-fcntl)",
        "(allow process-exec*)",
        "(deny process-fork)",
        "(allow process-info* (target self))",
        "(allow signal (target self))",
        "(allow sysctl-read)",
    ]
    for operation in (
        "file-test-existence",
        "file-read-metadata",
        "file-read*",
    ):
        lines.append(f"(allow {operation}")
        lines.extend(
            f"  (subpath {_sandbox_quote(path)})"
            for path in read_subpaths
        )
        if operation != "file-read*":
            lines.extend(
                f"  (literal {_sandbox_quote(path)})"
                for path in sorted(metadata_literals)
            )
        lines.extend(
            f"  (literal {_sandbox_quote(path)})"
            for path in [*runtime_reads, *runtime_executables]
        )
        lines.append(")")
    lines.extend(
        [
            "(allow file-map-executable",
            '  (subpath "/System")',
            '  (subpath "/bin")',
            '  (subpath "/usr/bin")',
            '  (subpath "/usr/lib")',
            '  (subpath "/Library/Apple")',
            '  (subpath "/Library/Developer/CommandLineTools")',
            '  (subpath "/Applications/Xcode.app")',
            f"  (subpath {_sandbox_quote(str(mirror / 'runtime'))})",
        ]
    )
    lines.extend(
        f"  (literal {_sandbox_quote(path)})"
        for path in runtime_executables
    )
    lines.append(")")
    lines.extend(
        [
            "(allow file-write*",
            f"  (subpath {_sandbox_quote(str(mirror / 'output'))})",
            f"  (subpath {_sandbox_quote(str(mirror / 'home'))})",
            f"  (subpath {_sandbox_quote(str(mirror / 'tmp'))})",
            f"  (subpath {_sandbox_quote(str(mirror / 'xdg-config'))})",
            f"  (subpath {_sandbox_quote(str(mirror / 'xdg-cache'))})",
            f"  (subpath {_sandbox_quote(str(mirror / 'xdg-data'))})",
            ")",
        ]
    )
    if network is not False and network is not None:
        if (
            type(network) is not int
            or not 1 <= network <= 65535
        ):
            raise StageError("Darwin network endpoint is invalid")
        lines.append(
            "(allow socket-option-get (socket-option-name SO_ERROR))"
        )
        lines.append(
            "(allow socket-option-set (socket-option-name SO_NOSIGPIPE))"
        )
        lines.append(
            "(allow network-outbound "
            f'(remote ip "localhost:{network}"))'
        )
        lines.append(
            "(allow network-outbound "
            f'(remote tcp "localhost:{network}"))'
        )
    denied_paths = [
        ROOT,
        ROOT / ".git",
        pathlib.Path.home() / ".ai-ideas-runs",
    ]
    if not runtime_reads:
        denied_paths.append(pathlib.Path.home())
    for denied in denied_paths:
        lines.append(
            f"(deny file-read* (subpath {_sandbox_quote(str(denied))}))"
        )
        lines.append(
            f"(deny file-write* (subpath {_sandbox_quote(str(denied))}))"
        )
    profile = "\n".join(lines) + "\n"
    return [
        "/usr/bin/sandbox-exec",
        "-f",
        str(profile_path),
        *command,
    ], profile


def build_linux_launch(
    bwrap_path,
    mirror,
    command,
    *,
    network=False,
    registered_reads=(),
):
    """Return the fixed bwrap argv; no repository or host-home bind exists."""
    argv = [
        str(bwrap_path),
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--unshare-pid",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
    ]
    if network:
        argv.append("--share-net")
    for system_path in (
        "/usr",
        "/bin",
        "/lib",
        "/lib64",
        "/etc/ld.so.cache",
    ):
        if pathlib.Path(system_path).exists():
            argv.extend(["--ro-bind", system_path, system_path])
    for registered in registered_reads:
        argv.extend(["--ro-bind", str(registered), str(registered)])
    for name in ("input", "runtime"):
        path = mirror / name
        argv.extend(["--ro-bind", str(path), str(path)])
    for name in ("output", "home", "tmp", "xdg-config", "xdg-cache", "xdg-data"):
        path = mirror / name
        argv.extend(["--bind", str(path), str(path)])
    argv.extend(["--chdir", str(mirror), "--", *command])
    return argv


def _minimal_environment(mirror, registered, backend_type=None):
    environment = {
        "HOME": str(mirror / "home"),
        "TMPDIR": str(mirror / "tmp"),
        "XDG_CONFIG_HOME": str(mirror / "xdg-config"),
        "XDG_CACHE_HOME": str(mirror / "xdg-cache"),
        "XDG_DATA_HOME": str(mirror / "xdg-data"),
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
    }
    if backend_type == "codex":
        environment["CODEX_HOME"] = str(
            mirror / "home" / ".codex"
        )
    environment.update(registered)
    return environment


def _atomic_publish(guard, raw, mode=0o644):
    parent_fd = guard["parent_fd"]
    if parent_fd < 0:
        raise StageError("destination guard is closed")
    current = os.fstat(parent_fd)
    if (
        current.st_dev,
        current.st_ino,
    ) != guard["parent_identity"]:
        raise StageError("destination parent identity changed")
    try:
        path_current = guard["parent_path"].lstat()
    except OSError as exc:
        raise StageError(
            "destination parent path is unavailable"
        ) from exc
    if (
        path_current.st_dev,
        path_current.st_ino,
    ) != guard["parent_identity"]:
        raise StageError("destination parent path changed")
    target = guard["target_name"]
    try:
        existing = os.stat(
            target,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        existing = None
    if existing is not None and (
        not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1
    ):
        raise StageError("destination changed to a non-regular file")
    temporary = None
    fd = -1
    try:
        for _ in range(128):
            candidate = ".history-stage-" + secrets.token_hex(16)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(
                    candidate,
                    flags,
                    mode,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                continue
            temporary = candidate
            break
        if temporary is None:
            raise StageError(
                "destination temporary name space is exhausted"
            )
        os.fchmod(fd, mode)
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.rename(
            temporary,
            target,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temporary = None
        published = os.stat(
            target,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(published.st_mode)
            or published.st_nlink != 1
            or published.st_size != len(raw)
        ):
            raise StageError("published destination is invalid")
        os.fsync(parent_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass


def _rehash_mirror_inputs(mirror, captured_inputs):
    root_fd = os.open(mirror / "input", os.O_RDONLY)
    try:
        for name, expected in captured_inputs.items():
            actual = capture_regular_input(
                root_fd,
                name,
                expected["sha256"],
                expected["max_bytes"],
            )
            if actual["raw"] != expected["raw"]:
                raise StageError("mirror input drifted before launch")
    finally:
        os.close(root_fd)


def _revalidate_sources(
    manifest_path,
    manifest_capture,
    role,
    policy,
    adapter_artifacts,
    input_root,
    inputs,
    backend,
):
    current_manifest = _capture_regular_path(
        manifest_path, MANIFEST_MAX_BYTES
    )
    if (
        current_manifest["raw"] != manifest_capture["raw"]
        or current_manifest["identity"] != manifest_capture["identity"]
    ):
        raise StageError("manifest drifted before launch")
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    repo_fd = os.open(ROOT, flags)
    try:
        for label, value in (
            ("role", role),
            ("adapter", adapter_artifacts["executable"]),
            (
                "canonicalizer",
                adapter_artifacts["canonicalizer"],
            ),
        ):
            current = capture_regular_input(
                repo_fd,
                value["source"],
                value["sha256"],
                MANIFEST_MAX_BYTES,
            )
            if (
                current["raw"] != value["raw"]
                or current["identity"] != value["identity"]
            ):
                raise StageError(f"{label} drifted before launch")
    finally:
        os.close(repo_fd)
    if policy.get("external_policy") is True:
        current_policy = _capture_regular_path(
            policy["path"], MANIFEST_MAX_BYTES
        )
        if (
            current_policy["raw"] != policy["raw"]
            or current_policy["identity"] != policy["identity"]
        ):
            raise StageError("policy drifted before launch")
    else:
        repo_fd = os.open(ROOT, flags)
        try:
            current_policy = capture_regular_input(
                repo_fd,
                policy["source"],
                policy["sha256"],
                MANIFEST_MAX_BYTES,
            )
            if (
                current_policy["raw"] != policy["raw"]
                or current_policy["identity"] != policy["identity"]
            ):
                raise StageError("policy drifted before launch")
        finally:
            os.close(repo_fd)
    root_fd = os.open(input_root["path"], flags)
    try:
        for value in inputs.values():
            current = capture_regular_input(
                root_fd,
                value["source"],
                value["sha256"],
                value["max_bytes"],
            )
            if (
                current["raw"] != value["raw"]
                or current["identity"] != value["identity"]
            ):
                raise StageError("input drifted before launch")
    finally:
        os.close(root_fd)
    current_backend = _capture_regular_path(
        backend["source"]["path"],
        512 * 1024 * 1024,
        reject_sparse=False,
    )
    if (
        current_backend["raw"] != backend["source"]["raw"]
        or current_backend["identity"] != backend["source"]["identity"]
    ):
        raise StageError("backend executable drifted before launch")
    if backend["interpreter"] is not None:
        current_interpreter = _capture_regular_path(
            backend["interpreter"]["path"],
            16 * 1024 * 1024,
            reject_sparse=False,
        )
        if (
            current_interpreter["raw"] != backend["interpreter"]["raw"]
            or current_interpreter["identity"]
            != backend["interpreter"]["identity"]
        ):
            raise StageError("backend interpreter drifted before launch")
    for dependency in backend["dependencies"].values():
        current_dependency = _capture_regular_path(
            dependency["path"],
            1024 * 1024,
        )
        if (
            current_dependency["raw"] != dependency["raw"]
            or current_dependency["identity"] != dependency["identity"]
        ):
            raise StageError("backend runtime dependency drifted before launch")
    for executable in backend["runtime_executables"].values():
        current_executable = _capture_regular_path(
            executable["path"],
            32 * 1024 * 1024,
            reject_sparse=False,
        )
        if (
            current_executable["raw"] != executable["raw"]
            or current_executable["identity"] != executable["identity"]
        ):
            raise StageError("registered runtime executable drifted")
    for bootstrap in backend["bootstrap_files"].values():
        current_bootstrap = _capture_regular_path(
            bootstrap["path"],
            max(16 * 1024, len(bootstrap["raw"])),
        )
        if (
            current_bootstrap["raw"] != bootstrap["raw"]
            or current_bootstrap["identity"] != bootstrap["identity"]
        ):
            raise StageError("registered backend bootstrap drifted")
    if backend["codex_capability"] is not None:
        registry = backend["codex_capability"]["registry"]
        current_registry = _capture_regular_path(
            registry["path"],
            1024 * 1024,
        )
        if (
            current_registry["raw"] != registry["raw"]
            or current_registry["identity"] != registry["identity"]
        ):
            raise StageError("Codex capability registry drifted")
        _revalidate_codex_auth(backend["codex_auth"])


def _kill_contained_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.kill()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _run_contained(
    launch,
    mirror,
    environment,
    *,
    backend_entry_fd=None,
):
    if backend_entry_fd is not None:
        try:
            os.write(backend_entry_fd, b"backend-entry\n")
            os.fsync(backend_entry_fd)
        except OSError as exc:
            raise StageError(
                "backend entry could not be recorded"
            ) from exc
    try:
        process = subprocess.Popen(
            launch,
            cwd=mirror,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise StageError("contained backend could not start") from exc
    output = {
        "stdout": bytearray(),
        "stderr": bytearray(),
    }
    selector = selectors.DefaultSelector()
    for name, pipe in (
        ("stdout", process.stdout),
        ("stderr", process.stderr),
    ):
        os.set_blocking(pipe.fileno(), False)
        selector.register(pipe, selectors.EVENT_READ, name)
    deadline = time.monotonic() + PROCESS_TIMEOUT_SECONDS
    failure = None
    try:
        while selector.get_map():
            now = time.monotonic()
            if now >= deadline:
                failure = StageError("contained backend timed out")
                break
            for key, _ in selector.select(
                timeout=min(0.02, deadline - now)
            ):
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                target = output[key.data]
                target.extend(chunk)
                if len(target) > 1024 * 1024:
                    failure = StageError(
                        "contained backend log exceeded its bound"
                    )
                    break
            if failure is not None:
                break
        if failure is None:
            process.wait(timeout=1)
    finally:
        selector.close()
        _kill_contained_group(process)
        for pipe in (process.stdout, process.stderr):
            if pipe is not None and not pipe.closed:
                pipe.close()
    if failure is not None:
        raise failure
    if process.returncode != 0:
        detail = bytes(output["stderr"][-2048:]).decode(
            "utf-8",
            errors="replace",
        )
        raise StageError(
            f"contained backend exited {process.returncode}: {detail}"
        )
    return bytes(output["stdout"]), bytes(output["stderr"])


def _capture_output_file(path, maximum):
    captured = _capture_regular_path(path, maximum)
    try:
        captured["text"] = captured["raw"].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StageError("stage output is not UTF-8") from exc
    return captured


def _validate_output_tree(mirror, outputs):
    output_root = mirror / "output"
    expected = {
        str(pathlib.PurePosixPath(item["mirror_path"]).relative_to("output"))
        for item in outputs
    }
    actual = set()
    for current, directories, files in os.walk(
        output_root, topdown=True, followlinks=False
    ):
        current_path = pathlib.Path(current)
        for directory in list(directories):
            path = current_path / directory
            value = path.lstat()
            if not stat.S_ISDIR(value.st_mode):
                raise StageError("stage output contains a special path")
        for filename in files:
            path = current_path / filename
            relative = str(path.relative_to(output_root))
            actual.add(relative)
    if actual != expected:
        raise StageError("stage output allowlist mismatch")


def _validate_verdict(text, candidate_id):
    lines = text.splitlines()
    if len(lines) != 1:
        raise StageError("review verdict must contain one row")
    fields = lines[0].split("\t")
    if (
        len(fields) != 4
        or fields[0] != candidate_id
        or fields[1] not in {"strong-accept", "accept-w-rev", "reject"}
        or not fields[2].isdigit()
        or not fields[3].strip()
    ):
        raise StageError("review verdict schema mismatch")
    return {
        "candidate_id": fields[0],
        "verdict": fields[1],
        "major_count": int(fields[2]),
        "reason": fields[3].strip(),
    }


def _build_generation_tsv_from_markdown(markdown, direction_contract=None):
    """Validate generate markdown and return host-projected ideas.tsv text.

    Single source of truth: model writes markdown only. The TSV index is
    derived from each section's One-Sentence Story and Theme.
    """
    lines = markdown.splitlines()
    markers = [
        index
        for index, line in enumerate(lines)
        if line.startswith("Assumption-Removal Attempt:")
    ]
    headings = [
        (index, line[3:].strip())
        for index, line in enumerate(lines)
        if line.startswith("## ")
    ]
    if (
        len(markers) != 1
        or not headings
        or len(headings) > 20
        or markers[0] >= headings[0][0]
        or [identifier for _, identifier in headings]
        != [f"I{index}" for index in range(1, len(headings) + 1)]
    ):
        raise StageError("generation markdown section mismatch")
    required = (
        "One-Sentence Story",
        "Theme",
        "Form",
        "Summary",
        "Minimal Falsification Experiment",
        "Why It May Be Novel",
    )
    assumption_required = (
        "Assumption to Remove",
        "Why It Can Be Removed Now",
        "Forcing Constraint",
    )
    direction_required = ()
    if direction_contract is not None:
        direction_required = (
            "Direction Axis",
            "Target Failure",
            "Direction Evidence",
        )
    assumption_ids = set()
    # id -> count of Crack Evidence rows that carry a real http(s) URL
    assumption_url_cracks = {}
    rows = []
    for position, (start, identifier) in enumerate(headings):
        end = (
            headings[position + 1][0]
            if position + 1 < len(headings)
            else len(lines)
        )
        values = {}
        crack_evidence = []
        for line in lines[start + 1:end]:
            stripped = line.strip()
            for label in (
                *required,
                *assumption_required,
                *direction_required,
            ):
                prefix = label + ":"
                if stripped.startswith(prefix):
                    if label in values:
                        raise StageError(
                            f"generation field is duplicated: "
                            f"{identifier} {label}"
                        )
                    value = stripped[len(prefix):].strip()
                    if not value:
                        raise StageError(
                            f"generation field is empty: "
                            f"{identifier} {label}"
                        )
                    if len(value.encode("utf-8")) > 4096:
                        raise StageError(
                            f"generation field exceeds bound: "
                            f"{identifier} {label}"
                        )
                    values[label] = value
            if stripped.startswith("Crack Evidence:"):
                evidence = stripped[len("Crack Evidence:"):].strip()
                if (
                    not evidence
                    or len(evidence.encode("utf-8")) > 2048
                ):
                    raise StageError(
                        f"generation crack evidence is invalid: "
                        f"{identifier}"
                    )
                crack_evidence.append(evidence)
        missing = [
            label
            for label in (*required, *direction_required)
            if label not in values
        ]
        if missing:
            raise StageError(
                f"generation field is missing: {identifier} "
                f"{', '.join(missing)}"
            )
        if direction_contract is not None:
            try:
                direction_contract_lib.validate_candidate_fields(
                    {
                        label: values[label]
                        for label in direction_required
                    },
                    direction_contract,
                    identifier,
                )
            except direction_contract_lib.DirectionContractError as exc:
                raise StageError(
                    f"generation direction fields are invalid: {identifier}"
                ) from exc
        story = values["One-Sentence Story"]
        theme = values["Theme"]
        if (
            len(story.encode("utf-8")) > 1024
            or len(theme.encode("utf-8")) > 1024
            or "\t" in story
            or "\t" in theme
            or "\n" in story
            or "\n" in theme
        ):
            raise StageError(
                f"generation field is invalid: {identifier}"
            )
        rows.append(f"{identifier}\t{story}\t{theme}")
        if values["Form"] == "remove-load-bearing-assumption":
            assumption_ids.add(identifier)
            url_cracks = sum(
                1
                for evidence in crack_evidence
                if re.search(r"https?://", evidence)
            )
            assumption_url_cracks[identifier] = url_cracks
            if (
                any(
                    label not in values
                    for label in assumption_required
                )
                or len(crack_evidence) < 2
            ):
                raise StageError(
                    f"assumption-removal evidence is incomplete: "
                    f"{identifier}"
                )
    marker = lines[markers[0]]
    complete = re.fullmatch(
        r"Assumption-Removal Attempt: complete (I[1-9][0-9]?)",
        marker,
    )
    incomplete = marker.startswith(
        "Assumption-Removal Attempt: incomplete "
    )
    if (
        complete is None
        and not incomplete
    ) or (
        complete is not None
        and complete.group(1) not in assumption_ids
    ):
        raise StageError("assumption-removal marker is invalid")
    # Complete attempts must carry real http(s) Crack Evidence URLs.
    # Incomplete markers satisfy the attempt quota; placeholder cracks OK.
    if complete is not None:
        complete_id = complete.group(1)
        if assumption_url_cracks.get(complete_id, 0) < 2:
            raise StageError(
                f"assumption-removal crack evidence lacks URLs: "
                f"{complete_id}"
            )
    return "\n".join(rows) + "\n"


def _project_generation_tsv(mirror, direction_contract=None):
    """Write output/ideas.tsv from output/ideas.md (host-owned index)."""
    md_path = mirror / "output" / "ideas.md"
    # Use regular-file capture so FIFO/symlink attacks cannot block open().
    captured = _capture_regular_path(md_path, 65536)
    if not captured["raw"]:
        raise StageError("generation markdown bound is invalid")
    try:
        markdown = captured["raw"].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StageError("generation markdown is not UTF-8") from exc
    tsv_text = _build_generation_tsv_from_markdown(
        markdown, direction_contract=direction_contract
    )
    tsv_raw = tsv_text.encode("utf-8")
    tsv_path = mirror / "output" / "ideas.tsv"
    # Replace any agent-written TSV; the host owns this file.
    try:
        if tsv_path.exists() or tsv_path.is_symlink():
            tsv_path.unlink()
    except OSError as exc:
        raise StageError("generation TSV cannot be replaced") from exc
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(tsv_path, flags, 0o444)
    except OSError as exc:
        raise StageError("generation TSV cannot be written") from exc
    try:
        view = memoryview(tsv_raw)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)

def _build_review_verdict_from_markdown(markdown, candidate_id):
    """Validate review markdown and return host-projected verdict.tsv text.

    Markdown is authoritative; dual-write TSV drift is projected away.
    """
    lines = [line for line in markdown.splitlines() if line.strip()]
    labels = (
        "Verdict",
        "CRITICAL",
        "MAJOR",
        "Headline",
        "Occupation",
        "Experiment",
        "Estimand",
        "Payoff",
        "Feasibility",
        "History",
        "Reason",
    )
    if (
        len(lines) != len(labels) + 1
        or lines[0] != f"# {candidate_id}"
    ):
        raise StageError("review markdown schema mismatch")
    values = {}
    for line, label in zip(lines[1:], labels):
        prefix = label + ":"
        if not line.startswith(prefix):
            raise StageError("review markdown field order mismatch")
        value = line[len(prefix):].strip()
        if not value or len(value.encode("utf-8")) > 4096:
            raise StageError("review markdown field is invalid")
        values[label] = value
    if values["Verdict"] not in {
        "strong-accept",
        "accept-w-rev",
        "reject",
    }:
        raise StageError("review markdown verdict is invalid")
    if not values["CRITICAL"].isdigit() or not values["MAJOR"].isdigit():
        raise StageError("review markdown count fields are invalid")
    critical = int(values["CRITICAL"])
    major = int(values["MAJOR"])
    if (
        critical > 0
        and values["Verdict"] != "reject"
    ) or (
        major >= 2
        and values["Verdict"] == "strong-accept"
    ):
        raise StageError("review verdict violates a hard gate")
    reason = values["Reason"]
    if "\t" in reason or "\n" in reason:
        raise StageError("review markdown reason is invalid")
    return f"{candidate_id}\t{values['Verdict']}\t{major}\t{reason}\n"


def _project_review_verdict(mirror, candidate_id):
    """Write output/verdict.tsv from output/review.md (host-owned)."""
    md_path = mirror / "output" / "review.md"
    captured = _capture_regular_path(md_path, 65536)
    if not captured["raw"]:
        raise StageError("review markdown bound is invalid")
    try:
        markdown = captured["raw"].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StageError("review markdown is not UTF-8") from exc
    tsv_text = _build_review_verdict_from_markdown(markdown, candidate_id)
    tsv_raw = tsv_text.encode("utf-8")
    tsv_path = mirror / "output" / "verdict.tsv"
    try:
        if tsv_path.exists() or tsv_path.is_symlink():
            tsv_path.unlink()
    except OSError as exc:
        raise StageError("review verdict cannot be replaced") from exc
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(tsv_path, flags, 0o444)
    except OSError as exc:
        raise StageError("review verdict cannot be written") from exc
    try:
        view = memoryview(tsv_raw)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _validate_review_output(markdown, verdict_row):
    expected = _build_review_verdict_from_markdown(
        markdown, verdict_row["candidate_id"]
    )
    actual = (
        f"{verdict_row['candidate_id']}\t{verdict_row['verdict']}\t"
        f"{verdict_row['major_count']}\t{verdict_row['reason']}\n"
    )
    if expected != actual:
        raise StageError("review vote and markdown disagree")


def _validate_failure_distillation(value, batch):
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "mappings"}
        or value.get("schema_version") != 1
        or not isinstance(value.get("mappings"), list)
    ):
        raise StageError("failure distillation schema mismatch")
    required = {"source_id", "failure_code", "theme"}
    source_ids = [item["source_id"] for item in batch["items"]]
    if (
        len(value["mappings"]) != len(source_ids)
        or [
            item.get("source_id")
            for item in value["mappings"]
            if isinstance(item, dict)
        ]
        != source_ids
    ):
        raise StageError("failure mapping coverage mismatch")
    for item in value["mappings"]:
        if (
            not isinstance(item, dict)
            or set(item) != required
            or any(
                not isinstance(item[key], str)
                or not item[key]
                or len(item[key].encode("utf-8")) > 128
                for key in required
            )
            or item["failure_code"]
            not in batch["failure_codes"]
            or item["theme"] not in batch["themes"]
        ):
            raise StageError("failure mapping schema mismatch")


def validate_stage_outputs(mirror, outputs, stage, parsed_inputs, preflight, seat_id):
    """Capture and parse all outputs before any host copy-back."""
    _validate_output_tree(mirror, outputs)
    captured = {}
    by_kind = {}
    for output in outputs:
        path = mirror.joinpath(
            *pathlib.PurePosixPath(output["mirror_path"]).parts
        )
        value = _capture_output_file(path, output["max_bytes"])
        captured[output["mirror_path"]] = value
        by_kind[output["artifact_kind"]] = value
    attestation = _load_json_bytes(
        by_kind["prompt-attestation-json"]["raw"],
        "prompt attestation",
    )
    if (
        not isinstance(attestation, dict)
        or set(attestation)
        != {"schema_version", "stage", "seat_id", "prompt_sha256"}
        or attestation.get("schema_version") != 1
        or attestation.get("stage") != stage
        or attestation.get("seat_id") != seat_id
        or attestation.get("prompt_sha256")
        != preflight["serialized_sha256"]
    ):
        raise StageError("backend prompt attestation mismatch")
    if stage == "generate":
        # Host already projected TSV from markdown; re-check equality.
        expected_tsv = _build_generation_tsv_from_markdown(
            by_kind["generation-ideas-markdown"]["text"],
            direction_contract=parsed_inputs.get(
                "direction_constraint.json"
            ),
        )
        if by_kind["generation-ideas-tsv"]["text"] != expected_tsv:
            raise StageError("generation TSV projection mismatch")
    elif stage == "history-compare":
        response = _load_json_bytes(
            by_kind["history-comparison-json"]["raw"],
            "history comparison",
        )
        try:
            history_retrieval._validate_response(
                parsed_inputs["retrieval_pack.json"], response
            )
        except history_retrieval.ComparisonValidationError as exc:
            raise StageError("history comparison schema mismatch") from exc
    elif stage == "review":
        candidate_id = parsed_inputs["candidate.json"]["candidate_id"]
        expected_tsv = _build_review_verdict_from_markdown(
            by_kind["review-markdown"]["text"],
            candidate_id,
        )
        if by_kind["review-verdict-tsv"]["text"] != expected_tsv:
            raise StageError("review TSV projection mismatch")
        verdict_row = _validate_verdict(expected_tsv, candidate_id)
        _validate_review_output(
            by_kind["review-markdown"]["text"],
            verdict_row,
        )
    elif stage == "meta":
        _validate_failure_distillation(
            _load_json_bytes(
                by_kind["failure-distillation-json"]["raw"],
                "failure distillation",
            ),
            parsed_inputs["failure_batch.json"],
        )
    return captured


def publish_stage_outputs(validated_bytes, outputs):
    """Atomically publish previously captured buffers."""
    published = {}
    for output in sorted(outputs, key=lambda item: item["destination"]):
        value = validated_bytes[output["mirror_path"]]
        _atomic_publish(output["destination_guard"], value["raw"])
        published[output["destination"]] = {
            "sha256": value["sha256"],
            "byte_count": len(value["raw"]),
            "artifact_kind": output["artifact_kind"],
        }
    return published


def _output_contract_hash(outputs):
    return _sha256(
        _canonical_bytes(
            [
                {
                    key: output[key]
                    for key in (
                        "mirror_path",
                        "destination",
                        "artifact_kind",
                        "max_bytes",
                        "required",
                    )
                }
                for output in sorted(
                    outputs, key=lambda item: item["artifact_kind"]
                )
            ]
        )
    )


def _materialize_codex_result(
    mirror,
    execution,
    proxy,
    stage,
    seat_id,
    prompt_sha256,
):
    try:
        exchange_receipt = proxy.receipt()
    except history_stage_proxy.ProxyError as exc:
        failure_code = getattr(proxy, "failure_code", None)
        if failure_code is None:
            failure_code = getattr(
                getattr(proxy, "server", None),
                "failure_code",
                None,
            )
        if failure_code == "auth_refresh_required":
            raise StageError("auth_refresh_required") from exc
        raise StageError("canonical exchange was incomplete") from exc
    model_output = _capture_regular_path(
        execution["model_output"],
        history_stage_proxy.MODEL_OUTPUT_MAX_BYTES,
    )
    if (
        model_output["sha256"]
        != exchange_receipt["model_output_sha256"]
        or len(model_output["raw"])
        != exchange_receipt["model_output_bytes"]
    ):
        raise StageError("Codex final message differs from canonical exchange")
    try:
        history_stage_adapter.materialize_model_output(
            mirror,
            stage,
            seat_id,
            prompt_sha256,
            model_output["raw"],
        )
    except (OSError, TypeError, ValueError) as exc:
        raise StageError("Codex final message is invalid") from exc
    return exchange_receipt


def run_stage(
    stage,
    manifest_path,
    command_argv,
    *,
    backend_entry_fd=None,
):
    """Run one stage and return the host-owned completion receipt."""
    mirror = None
    proxy = None
    destination_guards = []
    if backend_entry_fd is not None:
        if type(backend_entry_fd) is not int or backend_entry_fd < 0:
            raise StageError("backend entry descriptor is invalid")
        try:
            entry_stat = os.fstat(backend_entry_fd)
        except OSError as exc:
            raise StageError(
                "backend entry descriptor is invalid"
            ) from exc
        if (
            not stat.S_ISREG(entry_stat.st_mode)
            or entry_stat.st_nlink != 1
            or entry_stat.st_size != 0
        ):
            raise StageError("backend entry descriptor is unsafe")
    manifest_path = pathlib.Path(manifest_path)
    try:
        manifest = load_manifest(manifest_path, stage)
        profile = _STAGE_PROFILES[stage]
        role = _repo_artifact(manifest["role"], profile["role"], "role")
        policy_capture = _stage_policy_artifact(
            manifest["policy"], command_argv
        )
        policy = _load_json_bytes(
            policy_capture["raw"],
            "stage policy",
            require_canonical=False,
        )
        if not isinstance(policy, dict):
            raise StageError("stage policy must be an object")
        adapter_artifacts = _validate_adapter(manifest, policy)
        adapter_capture = adapter_artifacts["executable"]
        input_root, captured_inputs = _validate_inputs(manifest, profile)
        (
            output_root,
            outputs,
            preflight_destination,
            completion_destination,
        ) = _validate_outputs(manifest, stage)
        destination_guards = [
            *[
                output["destination_guard"]
                for output in outputs
            ],
            preflight_destination,
            completion_destination,
        ]
        serialized, _, parsed_inputs = build_stage_invocation(
            profile,
            manifest,
            captured_inputs,
            role,
            policy,
        )
        history_authority = _validate_history_authority(
            stage,
            manifest.get("history_store"),
            parsed_inputs,
            policy,
        )
        backend = _resolve_backend(
            command_argv,
            manifest,
            policy,
            adapter_artifacts,
        )
        response_schema = None
        canonical_request = None
        if backend["type"] == "codex":
            response_schema = history_stage_adapter.stage_response_schema(
                stage
            )
            canonical_request = history_stage_proxy.canonical_request(
                prompt=serialized.decode("utf-8"),
                schema=response_schema,
                model=backend["codex_identity"]["model"],
                reasoning_effort=backend["codex_identity"][
                    "reasoning_setting"
                ].split("=", 1)[1],
                max_output_tokens=policy["max_output_tokens"],
            )
            budget_receipt = history_budget.preflight_canonical_request(
                serialized,
                canonical_request,
                policy,
            )
            auth = backend["codex_auth"]
            proxy = history_stage_proxy.CanonicalProxyServer(
                prompt=serialized.decode("utf-8"),
                canonical_request=canonical_request,
                upstream_endpoint=_codex_upstream_endpoint(),
                authorization=auth["access_token"],
                account_id=auth["account_id"],
                output_validator=lambda raw: (
                    history_stage_adapter.parse_model_output(stage, raw)
                ),
                max_output_tokens=policy["max_output_tokens"],
            )
            proxy.__enter__()
        else:
            budget_receipt = preflight_stage_invocation(
                serialized, policy, captured_inputs
            )
        for runtime_read in backend["runtime_reads"]:
            _capture_regular_path(runtime_read, 16 * 1024 * 1024)
        mirror, launch_prefix, execution = _prepare_mirror(
            captured_inputs,
            backend,
            adapter_capture,
            stage,
            manifest["seat_id"],
            codex_proxy_port=(
                None if proxy is None else proxy.port
            ),
            response_schema=response_schema,
        )
        prompt = serialized.decode("utf-8")
        command = [*launch_prefix, prompt]
        system = platform.system()
        containment_executable = _capture_containment_executable(system)
        if system == "Darwin":
            profile_path = mirror / "runtime" / "stage.sb"
            launch, sandbox_profile = build_darwin_launch(
                profile_path,
                mirror,
                command,
                backend["runtime_reads"],
                (
                    proxy.port
                    if backend["type"] == "codex"
                    else False
                ),
                runtime_executables=(
                    [
                        *(
                            [backend["source"]["path"]]
                            if backend["type"] == "codex"
                            else []
                        ),
                        *[
                            value["path"]
                            for value in backend[
                                "runtime_executables"
                            ].values()
                        ],
                    ]
                ),
            )
            _write_mirror_file(
                mirror / "runtime",
                "stage.sb",
                sandbox_profile.encode("utf-8"),
                0o444,
            )
            containment = "darwin-sandbox-exec-v2"
        elif system == "Linux":
            launch = build_linux_launch(
                pathlib.Path(containment_executable["path"]),
                mirror,
                command,
                network=False,
                registered_reads=(
                    []
                ),
            )
            containment = "linux-bwrap-v1"
        else:
            raise StageError("no registered containment implementation")
        command_hash = _sha256(
            _canonical_bytes(
                {
                    "adapter_sha256": adapter_capture["sha256"],
                    "canonicalizer_sha256": adapter_artifacts[
                        "canonicalizer"
                    ]["sha256"],
                    "backend_argv": execution["backend_command"],
                    "wrapper_interpreter_sha256": backend[
                        "wrapper_interpreter"
                    ]["sha256"],
                }
            )
        )
        preflight = {
            "schema_version": 1,
            "stage": stage,
            "seat_id": manifest["seat_id"],
            "manifest_sha256": manifest["_manifest_capture"]["sha256"],
            "policy_sha256": policy_capture["sha256"],
            "role_sha256": role["sha256"],
            "adapter_version": ADAPTER_VERSION,
            "adapter_executable_sha256": adapter_capture["sha256"],
            "adapter_canonicalizer_sha256": adapter_artifacts[
                "canonicalizer"
            ]["sha256"],
            "adapter_interpreter_sha256": backend[
                "wrapper_interpreter"
            ]["sha256"],
            "command_argv_sha256": command_hash,
            "executable_sha256": backend["source"]["sha256"],
            "interpreter_sha256": (
                None
                if backend["interpreter"] is None
                else backend["interpreter"]["sha256"]
            ),
            "runtime_dependency_sha256s": {
                name: value["sha256"]
                for name, value in sorted(
                    backend["dependencies"].items()
                )
            },
            "runtime_executable_sha256s": {
                name: value["sha256"]
                for name, value in sorted(
                    backend["runtime_executables"].items()
                )
            },
            "backend_bootstrap_sha256s": {
                name: value["sha256"]
                for name, value in sorted(
                    backend["bootstrap_files"].items()
                )
            },
            "codex_capability_id": (
                None
                if backend["codex_capability"] is None
                else backend["codex_capability"][
                    "capability_id"
                ]
            ),
            "codex_capability_profile_sha256": (
                None
                if backend["codex_capability"] is None
                else backend["codex_capability"][
                    "profile_sha256"
                ]
            ),
            "codex_cli_version": (
                None
                if backend["codex_capability"] is None
                else backend["codex_capability"][
                    "codex_cli_version"
                ]
            ),
            "codex_auth_source": (
                None
                if backend["codex_auth"] is None
                else backend["codex_auth"]["source"]
            ),
            "canonical_request_sha256": (
                None
                if canonical_request is None
                else budget_receipt["canonical_request_sha256"]
            ),
            "canonical_request_bytes": (
                None
                if canonical_request is None
                else budget_receipt["canonical_request_bytes"]
            ),
            "response_schema_sha256": (
                None
                if response_schema is None
                else _sha256(_compact_json_bytes(response_schema))
            ),
            "history_pack_publication_id": (
                None
                if history_authority is None
                else history_authority["pack_publication_id"]
            ),
            "history_pack_sha256": (
                None
                if history_authority is None
                else history_authority["pack_sha256"]
            ),
            "input_sha256s": {
                name: value["sha256"]
                for name, value in sorted(captured_inputs.items())
            },
            "output_contract_sha256": _output_contract_hash(outputs),
            "containment": containment,
            "containment_executable_sha256": containment_executable[
                "sha256"
            ],
            "serialized_byte_count": len(serialized),
            "serialized_sha256": _sha256(serialized),
            "count_method": budget_receipt["count_method"],
            "input_upper_bound": budget_receipt["input_upper_bound"],
            "max_output_tokens": budget_receipt["output_tokens"],
            "safety_margin": budget_receipt["safety_margin"],
            "model_context_limit": budget_receipt["model_context_limit"],
            "total_upper_bound": budget_receipt["total_upper_bound"],
            "mirror_path": str(mirror),
            "home_path": str(mirror / "home"),
            "tmp_path": str(mirror / "tmp"),
        }
        preflight_raw = _canonical_bytes(preflight)
        _atomic_publish(preflight_destination, preflight_raw)
        _revalidate_sources(
            manifest_path,
            manifest["_manifest_capture"],
            role,
            policy_capture,
            adapter_artifacts,
            input_root,
            captured_inputs,
            backend,
        )
        _rehash_mirror_inputs(mirror, captured_inputs)
        _revalidate_captured_executable(
            containment_executable,
            "containment executable",
        )
        if history_authority is not None:
            _validate_history_authority(
                stage,
                history_authority["reference"],
                parsed_inputs,
                policy,
            )
        environment = _minimal_environment(
            mirror,
            backend["environment"],
            backend["type"],
        )
        try:
            _run_contained(
                launch,
                mirror,
                environment,
                backend_entry_fd=backend_entry_fd,
            )
        except StageError as exc:
            failure_code = (
                None
                if proxy is None
                else getattr(proxy, "failure_code", None)
            )
            last_error = (
                None
                if proxy is None
                else getattr(proxy, "last_error", None)
            )
            if failure_code is None and proxy is not None:
                failure_code = getattr(
                    proxy.server,
                    "failure_code",
                    None,
                )
            if last_error is None and proxy is not None:
                last_error = getattr(
                    proxy.server,
                    "last_error",
                    None,
                )
            if failure_code == "auth_refresh_required":
                raise StageError("auth_refresh_required") from exc
            if failure_code is not None and last_error:
                raise StageError(
                    f"{failure_code}: {last_error}"
                ) from exc
            raise
        canonical_exchange = None
        if proxy is not None:
            canonical_exchange = _materialize_codex_result(
                mirror,
                execution,
                proxy,
                stage,
                manifest["seat_id"],
                preflight["serialized_sha256"],
            )
        if stage == "generate":
            _project_generation_tsv(
                mirror,
                direction_contract=parsed_inputs.get(
                    "direction_constraint.json"
                ),
            )
        if stage == "review":
            _project_review_verdict(
                mirror,
                parsed_inputs["candidate.json"]["candidate_id"],
            )
        validated = validate_stage_outputs(
            mirror,
            outputs,
            stage,
            parsed_inputs,
            preflight,
            manifest["seat_id"],
        )
        published = publish_stage_outputs(validated, outputs)
        completion = {
            "schema_version": 1,
            "stage": stage,
            "seat_id": manifest["seat_id"],
            "preflight_sha256": _sha256(preflight_raw),
            "serialized_sha256": preflight["serialized_sha256"],
            "command_argv_sha256": command_hash,
            "containment": containment,
            "canonical_exchange": canonical_exchange,
            "outputs": published,
            "mirror_path": str(mirror),
            "home_path": str(mirror / "home"),
            "tmp_path": str(mirror / "tmp"),
        }
        completion["completion_id"] = _sha256(
            b"history-stage-completion-v1\0"
            + _canonical_bytes(completion)
        )
        _atomic_publish(
            completion_destination, _canonical_bytes(completion)
        )
        return completion
    except StageError:
        raise
    except (OSError, TypeError, ValueError, KeyError) as exc:
        raise StageError("stage execution failed closed") from exc
    finally:
        if proxy is not None:
            proxy.__exit__(None, None, None)
        if mirror is not None:
            _remove_mirror(mirror)
        for guard in destination_guards:
            _close_destination_guard(guard)


def _main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run a bounded agent stage under OS containment."
    )
    subparsers = parser.add_subparsers(dest="command_name", required=True)
    run = subparsers.add_parser("run")
    run.add_argument(
        "--stage",
        required=True,
        choices=sorted(_STAGE_PROFILES),
    )
    run.add_argument("--manifest", required=True)
    run.add_argument("--command", required=True)
    args = parser.parse_args(argv)
    try:
        command = parse_command_json(args.command)
        run_stage(args.stage, pathlib.Path(args.manifest), command)
    except StageError as exc:
        parser.exit(1, f"history_stage: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
