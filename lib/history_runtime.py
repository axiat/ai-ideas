#!/usr/bin/env python3
"""Host-owned orchestration for bounded historical-idea retrieval."""

import argparse
import contextlib
import contextvars
import copy
import ctypes
import datetime
import errno
import hashlib
import hmac
import json
import math
import os
import pathlib
import re
import secrets
import sqlite3
import stat
import tempfile
import weakref

try:
    from lib import direction_contract as direction_contract_lib
    from lib import history_archive
    from lib import history_budget
    from lib import history_eval
    from lib import history_projection
    from lib import history_retrieval
    from lib import history_store
    from lib import history_witness
except ImportError:
    import direction_contract as direction_contract_lib
    import history_archive
    import history_budget
    import history_eval
    import history_projection
    import history_retrieval
    import history_store
    import history_witness


DIVERGENCE_LENS_MAX_BYTES = (
    history_projection.DIVERGENCE_LENS_MAX_BYTES
)
ROOT = pathlib.Path(__file__).resolve().parents[1]
SYNTHETIC_SCOPE = "synthetic_contract_only"
PRODUCTION_SCOPE = "production"
PORTABLE_EXECUTION_BOUNDARY = "portable-mirror-v1"
CONTAINED_EXECUTOR = "contained-v1"
PORTABLE_EXECUTOR = "portable-v2"
PROVIDER_REGISTRY_PATH = (
    ROOT / "history" / "provider-adapters-v1.json"
)
RESUME_BINDING_FIELDS = (
    "mode",
    "policy_version",
    "policy_sha256",
    "source_watermark",
    "index_generation",
    "pack_sha256",
    "comparator_version",
    "candidate_content_sha256",
    "adapter_version",
    "preflight_sha256",
)
_INTENT_ORDER = (
    "duplicate_search",
    "evolution_search",
    "failure_pattern_search",
)
_STAGE_ROLES = {
    "generate": "roles/generate.md",
    "history-compare": "roles/history-compare.md",
    "review": "roles/review.md",
    "meta": "roles/meta.md",
}
_STAGE_MESSAGES = {
    "generate": "Generate bounded candidates.",
    "history-compare": "Compare the candidate.",
    "review": "Review the bounded candidate.",
    "meta": "Distill the bounded failure batch.",
}
_DIRECTION_UNSPECIFIED = object()
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
_STAGE_INPUTS = {
    "generate": (
        {"generation_brief.json", "generation_policy.md"},
        {"research_context.md", "direction_constraint.json"},
    ),
    "history-compare": ({"retrieval_pack.json"}, set()),
    "review": (
        {"candidate.json", "prior_work.md", "review_contract.md"},
        {"history_summary.json"},
    ),
    "meta": ({"failure_batch.json"}, set()),
}
_STAGE_OUTPUTS = {
    "generate": (
        (
            "output/ideas.tsv",
            "ideas.tsv",
            "generation-ideas-tsv",
            65536,
        ),
        (
            "output/ideas.md",
            "ideas.md",
            "generation-ideas-markdown",
            65536,
        ),
        (
            "output/prompt-attestation.json",
            "prompt-attestation.json",
            "prompt-attestation-json",
            4096,
        ),
    ),
    "history-compare": (
        (
            "output/history-comparison.json",
            "history-comparison.json",
            "history-comparison-json",
            65536,
        ),
        (
            "output/prompt-attestation.json",
            "prompt-attestation.json",
            "prompt-attestation-json",
            4096,
        ),
    ),
    "review": (
        (
            "output/review.md",
            "review.md",
            "review-markdown",
            65536,
        ),
        (
            "output/verdict.tsv",
            "verdict.tsv",
            "review-verdict-tsv",
            16384,
        ),
        (
            "output/prompt-attestation.json",
            "prompt-attestation.json",
            "prompt-attestation-json",
            4096,
        ),
    ),
    "meta": (
        (
            "output/failure-distillation.json",
            "failure-distillation.json",
            "failure-distillation-json",
            65536,
        ),
        (
            "output/prompt-attestation.json",
            "prompt-attestation.json",
            "prompt-attestation-json",
            4096,
        ),
    ),
}
_SHA_FIELDS = {
    "policy_sha256",
    "split_sha256",
    "calibration_query_ids_sha256",
    "heldout_query_ids_sha256",
    "policy_commitment_sha256",
    "trusted_runner_release_sha256",
    "preheldout_receipt_sha256",
    "benchmark_snapshot_sha256",
    "qrels_sha256",
    "adjudications_sha256",
    "heldout_output_sha256",
    "canonical_seal_sha256",
    "signature",
}


class RuntimeContractError(RuntimeError):
    def __init__(self, *args, error_class=None):
        super().__init__(*args)
        # None means the failure carried no portable-stage classification;
        # the portable stage's contract/execution tag otherwise passes through.
        self.error_class = error_class

    def __reduce__(self):
        # error_class is keyword-only and not in args, so default pickling
        # would drop it on reconstruction. Preserve it explicitly.
        return (
            _rebuild_runtime_contract_error,
            (type(self), self.args, self.error_class),
        )


class CalibrationError(RuntimeContractError):
    pass


def _rebuild_runtime_contract_error(error_type, args, error_class):
    # Pickle helper for RuntimeContractError.__reduce__; keeps the concrete
    # subclass and its portable-stage classification across reconstruction.
    return error_type(*args, error_class=error_class)


def _runtime_authority_capability():
    fields = (
        "mode",
        "policy_sha256",
        "capability_sha256",
        "policy_commitment_sha256",
        "preheldout_receipt_sha256",
        "trust_root_sha256",
        "scope",
    )
    registry = weakref.WeakKeyDictionary()

    class RuntimeAuthority:
        __slots__ = ("__weakref__",)

        def __new__(cls, *args, **kwargs):
            raise TypeError("runtime authorities are host-constructed")

        def __getitem__(self, key):
            value = registry.get(self)
            if value is None:
                raise TypeError("unsealed runtime authority")
            try:
                return value[fields.index(key)]
            except ValueError as exc:
                raise KeyError(key) from exc

        def get(self, key, default=None):
            try:
                return self[key]
            except KeyError:
                return default

    def issue(value):
        if not isinstance(value, dict) or set(value) != set(fields):
            raise TypeError("runtime authority fields are invalid")
        authority = object.__new__(RuntimeAuthority)
        registry[authority] = tuple(value[field] for field in fields)
        return authority

    def inspect(authority):
        if type(authority) is not RuntimeAuthority:
            return None
        value = registry.get(authority)
        if value is None:
            return None
        return dict(zip(fields, value))

    def validate_production(policy, capability=None, trust_root=None):
        material = _runtime_authority_material(
            policy,
            capability=capability,
            trust_root=trust_root,
            required_scope=PRODUCTION_SCOPE,
        )
        return issue(material)

    def validate_synthetic_for_test(
        policy, *, capability, trust_root
    ):
        material = _runtime_authority_material(
            policy,
            capability=capability,
            trust_root=trust_root,
            required_scope=SYNTHETIC_SCOPE,
        )
        return issue(material)

    return (
        RuntimeAuthority,
        inspect,
        validate_production,
        validate_synthetic_for_test,
    )


(
    RuntimeAuthority,
    _inspect_runtime_authority,
    _validate_production_authority,
    _validate_synthetic_authority_for_test,
) = _runtime_authority_capability()
_TEST_RUNTIME_CONTEXT = contextvars.ContextVar(
    "history_test_runtime_context",
    default=None,
)


def _test_state_root(state_root):
    root = pathlib.Path(state_root).resolve()
    temporary = pathlib.Path(tempfile.gettempdir()).resolve()
    try:
        root.relative_to(temporary)
    except ValueError as exc:
        raise RuntimeContractError(
            "synthetic runtime state must be temporary"
        ) from exc
    try:
        state = os.lstat(root)
    except OSError as exc:
        raise RuntimeContractError(
            "test runtime state is unavailable"
        ) from exc
    if (
        root == temporary
        or not stat.S_ISDIR(state.st_mode)
        or state.st_uid != os.getuid()
        or state.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    ):
        raise RuntimeContractError(
            "test runtime state must be a private test root"
        )
    return root


def _require_test_state_paths(state_root, paths):
    root = _test_state_root(state_root)
    for path in paths:
        try:
            pathlib.Path(path).resolve().relative_to(root)
        except (TypeError, ValueError) as exc:
            raise RuntimeContractError(
                "runtime fixture path escapes test state"
            ) from exc
    return root


@contextlib.contextmanager
def _runtime_for_test(
    policy,
    authority,
    state_root,
    *,
    state_paths=(),
):
    """Permit one exact-policy fixture run under ephemeral test state."""
    value = _inspect_runtime_authority(authority)
    root = _require_test_state_paths(state_root, state_paths)
    policy_sha = sha256(canonical_bytes(policy))
    if (
        value is None
        or value.get("mode") != policy.get("mode")
        or value.get("policy_sha256") != policy_sha
    ):
        raise RuntimeContractError(
            "test runtime authority does not match policy"
        )
    if policy.get("mode") == "shadow":
        valid_scope = (
            value.get("scope") is None
            and value.get("trust_root_sha256") is None
        )
    elif policy.get("mode") == "enforcement":
        valid_scope = (
            value.get("scope") == SYNTHETIC_SCOPE
            and value.get("trust_root_sha256") is None
        )
    else:
        valid_scope = False
    if not valid_scope:
        raise RuntimeContractError(
            "test runtime authority scope is invalid"
        )
    current = _TEST_RUNTIME_CONTEXT.get()
    expected = (authority, root, policy_sha)
    if current is not None and current != expected:
        raise RuntimeContractError(
            "nested test runtime context changed"
        )
    token = _TEST_RUNTIME_CONTEXT.set(expected)
    try:
        yield authority
    finally:
        _TEST_RUNTIME_CONTEXT.reset(token)


def _active_test_runtime(policy):
    context = _TEST_RUNTIME_CONTEXT.get()
    if (
        not isinstance(context, tuple)
        or len(context) != 3
        or context[2] != sha256(canonical_bytes(policy))
    ):
        return None
    value = _inspect_runtime_authority(context[0])
    if (
        value is None
        or value.get("mode") != policy.get("mode")
        or value.get("policy_sha256") != context[2]
    ):
        return None
    return {
        "authority": context[0],
        "state_root": context[1],
        "authority_value": value,
    }


def _require_context_test_paths(paths):
    context = _TEST_RUNTIME_CONTEXT.get()
    if context is None:
        return None
    if not isinstance(context, tuple) or len(context) != 3:
        raise RuntimeContractError(
            "test runtime context is invalid"
        )
    return _require_test_state_paths(context[1], paths)


def canonical_bytes(value):
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256(raw):
    if not isinstance(raw, bytes):
        raise TypeError("sha256 input must be bytes")
    return hashlib.sha256(raw).hexdigest()


def _valid_sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _open_safe_directory(path, *, create):
    absolute_text = os.path.abspath(os.fspath(path))
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
    absolute = pathlib.Path(absolute_text)
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        for component in absolute.parts[1:]:
            if component in {"", ".", ".."}:
                raise RuntimeContractError(
                    "artifact directory is not normalized"
                )
            if create:
                try:
                    os.mkdir(
                        component,
                        mode=0o700,
                        dir_fd=descriptor,
                    )
                except FileExistsError:
                    pass
            child = os.open(component, flags, dir_fd=descriptor)
            current = os.fstat(child)
            if not stat.S_ISDIR(current.st_mode):
                os.close(child)
                raise RuntimeContractError(
                    "artifact parent is not a directory"
                )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _resolved_path_text(path):
    return str(pathlib.Path(path).resolve())


def _artifact_name(path):
    name = pathlib.Path(path).name
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\x00" in name
    ):
        raise RuntimeContractError("artifact name is invalid")
    return name


def _write_descriptor(descriptor, raw):
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise RuntimeContractError("artifact write made no progress")
        view = view[written:]
    os.fsync(descriptor)


def _atomic_write(path, raw):
    destination = pathlib.Path(path)
    directory = _open_safe_directory(
        destination.parent, create=True
    )
    name = _artifact_name(destination)
    temporary = "." + name + "." + secrets.token_hex(12)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = None
    try:
        try:
            current = os.stat(
                name, dir_fd=directory, follow_symlinks=False
            )
        except FileNotFoundError:
            current = None
        if current is not None and (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
        ):
            raise RuntimeContractError(
                "artifact destination is not replaceable"
            )
        descriptor = os.open(
            temporary, flags, 0o600, dir_fd=directory
        )
        _write_descriptor(descriptor, raw)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary,
            name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        os.fsync(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def _publish_immutable(path, raw):
    destination = pathlib.Path(path)
    directory = _open_safe_directory(
        destination.parent, create=True
    )
    name = _artifact_name(destination)
    temporary = "." + name + "." + secrets.token_hex(12)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = None
    linked = False
    try:
        descriptor = os.open(
            temporary, flags, 0o600, dir_fd=directory
        )
        _write_descriptor(descriptor, raw)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=directory,
                dst_dir_fd=directory,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise RuntimeContractError(
                "immutable artifact already exists"
            ) from exc
        linked = True
        os.unlink(temporary, dir_fd=directory)
        os.fsync(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)
    if not linked:
        raise RuntimeContractError(
            "immutable artifact publication failed"
        )


def _mkdir_single_use(path):
    destination = pathlib.Path(path)
    directory = _open_safe_directory(
        destination.parent, create=True
    )
    name = _artifact_name(destination)
    try:
        try:
            os.mkdir(name, mode=0o700, dir_fd=directory)
        except FileExistsError as exc:
            raise RuntimeContractError(
                "immutable artifact root already exists"
            ) from exc
        os.fsync(directory)
    finally:
        os.close(directory)


def _directory_open_flags():
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _remove_tree_at(directory, name):
    try:
        child = os.open(name, _directory_open_flags(), dir_fd=directory)
    except FileNotFoundError:
        return
    try:
        for entry in os.listdir(child):
            state = os.stat(entry, dir_fd=child, follow_symlinks=False)
            if stat.S_ISDIR(state.st_mode):
                _remove_tree_at(child, entry)
            else:
                os.unlink(entry, dir_fd=child)
        os.fsync(child)
    finally:
        os.close(child)
    os.rmdir(name, dir_fd=directory)


def _remove_immutable_tree(path):
    destination = pathlib.Path(path)
    directory = _open_safe_directory(destination.parent, create=False)
    try:
        name = _artifact_name(destination)
        try:
            state = os.stat(
                name, dir_fd=directory, follow_symlinks=False
            )
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(state.st_mode):
            raise RuntimeContractError(
                "immutable artifact root cleanup target is invalid"
            )
        _remove_tree_at(directory, name)
        os.fsync(directory)
    finally:
        os.close(directory)


def _publish_immutable_at(directory, name, raw):
    name = _artifact_name(name)
    temporary = "." + name + "." + secrets.token_hex(12)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = None
    try:
        descriptor = os.open(
            temporary, flags, 0o600, dir_fd=directory
        )
        _write_descriptor(descriptor, raw)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=directory,
                dst_dir_fd=directory,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise RuntimeContractError(
                "immutable artifact already exists"
            ) from exc
        os.unlink(temporary, dir_fd=directory)
        os.fsync(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def _rename_no_replace(directory, source, destination):
    libc = ctypes.CDLL(None, use_errno=True)
    source_raw = os.fsencode(source)
    destination_raw = os.fsencode(destination)
    if hasattr(libc, "renameatx_np"):
        function = libc.renameatx_np
        function.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        function.restype = ctypes.c_int
        result = function(
            directory,
            source_raw,
            directory,
            destination_raw,
            0x00000004,
        )
    elif hasattr(libc, "renameat2"):
        function = libc.renameat2
        function.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        function.restype = ctypes.c_int
        result = function(
            directory,
            source_raw,
            directory,
            destination_raw,
            0x00000001,
        )
    else:
        raise RuntimeContractError(
            "atomic no-replace publication is unavailable"
        )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        raise RuntimeContractError(
            "immutable artifact root already exists"
        )
    raise OSError(error, os.strerror(error))


def _publish_immutable_tree(path, publications):
    destination = pathlib.Path(
        os.path.abspath(os.fspath(path))
    )
    if not isinstance(publications, dict):
        raise RuntimeContractError(
            "immutable artifact tree is invalid"
        )
    normalized = []
    for relative, raw in publications.items():
        if not isinstance(relative, (str, os.PathLike)):
            raise RuntimeContractError(
                "immutable artifact tree entry is invalid"
            )
        relative_path = pathlib.PurePath(relative)
        if (
            relative_path.is_absolute()
            or not relative_path.parts
            or any(
                component in {"", ".", ".."}
                for component in relative_path.parts
            )
            or not isinstance(raw, bytes)
        ):
            raise RuntimeContractError(
                "immutable artifact tree entry is invalid"
            )
        normalized.append((relative_path, raw))
    directory = _open_safe_directory(destination.parent, create=True)
    parent_state = os.fstat(directory)
    if (
        parent_state.st_uid != os.getuid()
        or parent_state.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        os.close(directory)
        raise RuntimeContractError(
            "immutable artifact parent is not private"
        )
    name = _artifact_name(destination)
    temporary_name = "." + name + "." + secrets.token_hex(12)
    temporary_descriptor = None
    published = False
    try:
        os.mkdir(temporary_name, mode=0o700, dir_fd=directory)
        temporary_descriptor = os.open(
            temporary_name,
            _directory_open_flags(),
            dir_fd=directory,
        )
        for relative, raw in normalized:
            parent = os.dup(temporary_descriptor)
            try:
                for component in relative.parts[:-1]:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=parent)
                    except FileExistsError:
                        pass
                    child = os.open(
                        component,
                        _directory_open_flags(),
                        dir_fd=parent,
                    )
                    os.close(parent)
                    parent = child
                _publish_immutable_at(parent, relative.parts[-1], raw)
            finally:
                os.close(parent)
        temporary_state = os.fstat(temporary_descriptor)
        named_state = os.stat(
            temporary_name,
            dir_fd=directory,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(named_state.st_mode)
            or (named_state.st_dev, named_state.st_ino)
            != (temporary_state.st_dev, temporary_state.st_ino)
        ):
            raise RuntimeContractError(
                "immutable artifact staging root changed"
            )
        os.fsync(temporary_descriptor)
        _rename_no_replace(directory, temporary_name, name)
        os.fsync(directory)
        published = True
    finally:
        try:
            if temporary_descriptor is not None:
                os.close(temporary_descriptor)
            if not published:
                _remove_tree_at(directory, temporary_name)
                os.fsync(directory)
        finally:
            os.close(directory)


def _verified_history_database(path, *, allow_missing=False):
    database = pathlib.Path(
        os.path.abspath(os.fspath(path))
    )
    try:
        directory = _open_safe_directory(
            database.parent, create=False
        )
    except FileNotFoundError:
        if allow_missing:
            return database
        raise RuntimeContractError(
            "history database is unavailable"
        )
    try:
        try:
            state = os.stat(
                _artifact_name(database),
                dir_fd=directory,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            if allow_missing:
                return database
            raise RuntimeContractError(
                "history database is unavailable"
            )
        if not stat.S_ISREG(state.st_mode):
            raise RuntimeContractError(
                "history database filename is invalid"
            )
        return database
    finally:
        os.close(directory)


def _connect_history_store(path, *, allow_missing=False):
    database = pathlib.Path(os.path.abspath(os.fspath(path)))
    try:
        directory = _open_safe_directory(
            database.parent, create=allow_missing
        )
    except (FileNotFoundError, NotADirectoryError, OSError) as exc:
        raise RuntimeContractError(
            "history database is unavailable"
        ) from exc
    name = _artifact_name(database)
    flags = os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if allow_missing:
        flags |= os.O_CREAT
    bound = None
    conn = None
    try:
        try:
            bound = os.open(name, flags, 0o600, dir_fd=directory)
        except OSError as exc:
            raise RuntimeContractError(
                "history database filename is invalid"
            ) from exc
        expected = os.fstat(bound)
        if not stat.S_ISREG(expected.st_mode):
            raise RuntimeContractError(
                "history database filename is invalid"
            )
        conn = sqlite3.connect(
            str(database), isolation_level=None
        )
        current = os.stat(
            name, dir_fd=directory, follow_symlinks=False
        )
        lexical = os.lstat(database)
        if (
            not stat.S_ISREG(current.st_mode)
            or not stat.S_ISREG(lexical.st_mode)
            or (current.st_dev, current.st_ino)
            != (expected.st_dev, expected.st_ino)
            or (lexical.st_dev, lexical.st_ino)
            != (expected.st_dev, expected.st_ino)
        ):
            raise RuntimeContractError(
                "history database binding changed during open"
            )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA recursive_triggers = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        return conn
    except Exception:
        if conn is not None:
            conn.close()
        raise
    finally:
        if bound is not None:
            os.close(bound)
        os.close(directory)


def _read_rooted_frozen_descriptor(
    root,
    descriptor,
    relative_path,
    label,
    *,
    maximum,
    fields,
):
    lexical_root = pathlib.Path(os.path.abspath(os.fspath(root)))
    root_descriptor = _open_safe_directory(lexical_root, create=False)
    os.close(root_descriptor)
    root = lexical_root.resolve(strict=True)
    relative = pathlib.PurePath(relative_path)
    expected = root.joinpath(*relative.parts)
    lexical_expected = lexical_root.joinpath(*relative.parts)
    descriptor_path = descriptor.get("path") if isinstance(descriptor, dict) else None
    normalized_descriptor_path = descriptor_path
    if isinstance(descriptor_path, str):
        for alias in ("/var", "/tmp", "/etc"):
            if descriptor_path == alias or descriptor_path.startswith(alias + os.sep):
                try:
                    alias_state = os.lstat(alias)
                except OSError:
                    break
                if stat.S_ISLNK(alias_state.st_mode) and alias_state.st_uid == 0:
                    normalized_descriptor_path = (
                        os.path.realpath(alias)
                        + descriptor_path[len(alias):]
                    )
                break
    if (
        not isinstance(descriptor, dict)
        or set(descriptor) != set(fields)
        or "path" not in fields
        or "sha256" not in fields
        or relative.is_absolute()
        or not relative.parts
        or any(
            component in {"", ".", ".."}
            for component in relative.parts
        )
        or normalized_descriptor_path
        not in {str(expected), str(lexical_expected)}
        or not _valid_sha256(descriptor.get("sha256"))
        or (
            "byte_count" in fields
            and (
                type(descriptor.get("byte_count")) is not int
                or descriptor["byte_count"] < 0
            )
        )
    ):
        raise RuntimeContractError(f"{label} descriptor is invalid")
    raw = _read_bound_regular(
        expected, label, maximum=maximum
    )
    if (
        sha256(raw) != descriptor["sha256"]
        or (
            "byte_count" in fields
            and len(raw) != descriptor["byte_count"]
        )
    ):
        raise RuntimeContractError(f"{label} changed")
    return raw


def _load_closed_json(value, label):
    if isinstance(value, (str, os.PathLike)):
        try:
            raw = _read_bound_regular(value, label)
        except RuntimeContractError as exc:
            raise CalibrationError(f"{label} is unavailable") from exc
        if len(raw) > 1024 * 1024:
            raise CalibrationError(f"{label} exceeds its byte bound")
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CalibrationError(f"{label} is invalid JSON") from exc
        if raw != canonical_bytes(parsed):
            raise CalibrationError(f"{label} is not canonical JSON")
        return parsed
    return value


def _require_fields(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise CalibrationError(f"{label} schema is not closed")


def _require_hashes(value, fields, label):
    for field in fields:
        if not _valid_sha256(value.get(field)):
            raise CalibrationError(f"{label} {field} is invalid")


def _parse_utc(value, label):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CalibrationError(f"{label} must be a UTC timestamp")
    try:
        return datetime.datetime.fromisoformat(
            value[:-1] + "+00:00"
        )
    except ValueError as exc:
        raise CalibrationError(f"{label} is invalid") from exc


def _test_root_key(root):
    _require_fields(
        root,
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
        type(root["schema_version"]) is not int
        or root["schema_version"] != 1
        or root["scope"] != SYNTHETIC_SCOPE
        or root["algorithm"] != "test-hmac-sha256"
        or not isinstance(root["trust_root_id"], str)
        or not root["trust_root_id"]
        or not _valid_sha256(root["hmac_sha256_key"])
    ):
        raise CalibrationError("test trust root is invalid")
    return bytes.fromhex(root["hmac_sha256_key"])


def _test_signature(domain, value, root):
    return hmac.new(
        _test_root_key(root),
        domain + canonical_bytes(value),
        hashlib.sha256,
    ).hexdigest()


def seal_test_preheldout_receipt(value, trust_root):
    """Seal one synthetic pre-held-out receipt for offline contract tests."""
    fields = {
        "schema_version",
        "scope",
        "trust_root_id",
        "policy_commitment_sha256",
        "split_sha256",
        "trusted_runner_release_sha256",
        "run_nonce",
        "witness_time",
    }
    _require_fields(value, fields, "pre-held-out receipt")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
    ):
        raise CalibrationError(
            "pre-held-out receipt schema is invalid"
        )
    result = dict(value)
    result["signature"] = _test_signature(
        b"history-preheldout-receipt-v1\0",
        result,
        trust_root,
    )
    return result


def _synthetic_relation_heldout_counts(*, advisory=False):
    positive = 10 if advisory else 30
    hard_negative = 10 if advisory else 30
    return {
        relation: {
            "positive": positive,
            "hard_negative": hard_negative,
            "advisory": advisory,
        }
        for relation in history_eval.POSITIVE_RELATIONS
    }


def _synthetic_evaluation_evidence(
    commitment, *, all_gates_passed=True
):
    def metric_gate(minimum=0.8):
        observed = 0.9 if all_gates_passed else 0.1
        lower = 0.85 if all_gates_passed else 0.05
        return {
            "arm": "end-to-end",
            "metric": "ndcg@10",
            "observed": observed,
            "ci95_lower": lower,
            "minimum": minimum,
            "passed": lower >= minimum,
        }

    def budget_gate(maximum=0.2):
        observed = 0.05 if all_gates_passed else 0.9
        upper = 0.1 if all_gates_passed else 0.95
        return {
            "arm": "end-to-end",
            "metric": "false_rate",
            "observed": observed,
            "ci95_upper": upper,
            "maximum": maximum,
            "passed": upper <= maximum,
        }

    def resource_gate(maximum):
        observed = maximum / 2 if all_gates_passed else maximum + 1
        return {
            "arm": "end-to-end",
            "metric": "resource",
            "observed": observed,
            "maximum": maximum,
            "passed": observed <= maximum,
        }

    evidence = {
        "schema_version": 1,
        "primary_metrics": {
            name: metric_gate(commitment["selected_thresholds"][name])
            for name in history_eval.RELATION_GAINS
        },
        "error_budgets": {
            "max_false_duplicate_rate": budget_gate(
                commitment["error_budgets"]["max_false_duplicate_rate"]
            ),
            "max_false_internal_no_match_rate": budget_gate(
                commitment["error_budgets"][
                    "max_false_internal_no_match_rate"
                ]
            ),
        },
        "resource_limits": {
            "latency_target_ms_p95": resource_gate(
                commitment["latency_target_ms_p95"]
            ),
            "token_budget": resource_gate(commitment["token_budget"]),
        },
        "selected_depths": {
            name: {
                "observed": (
                    max(0, commitment["selected_depths"][name] - 1)
                    if all_gates_passed
                    else commitment["selected_depths"][name] + 1
                ),
                "maximum": commitment["selected_depths"][name],
                "passed": all_gates_passed,
            }
            for name in (
                "per_channel_depth",
                "comparator_cutoff",
                "final_lineage_count",
            )
        },
        "confidence_intervals_sha256": "ab" * 32,
        "all_gates_passed": all_gates_passed,
    }
    # Keep all_gates_passed consistent with nested gates.
    evidence["all_gates_passed"] = all(
        item["passed"]
        for group in (
            evidence["primary_metrics"].values(),
            evidence["error_budgets"].values(),
            evidence["resource_limits"].values(),
            evidence["selected_depths"].values(),
        )
        for item in group
    )
    return evidence


def synthetic_policy_commitment(policy, *, digests=None):
    digests = digests or {}
    return {
        "schema_version": 1,
        "scope": SYNTHETIC_SCOPE,
        "policy_version": policy["retrieval_policy_version"],
        "policy_sha256": sha256(canonical_bytes(policy)),
        "split_sha256": digests.get("split", "12" * 32),
        "calibration_query_ids_sha256": digests.get(
            "calibration_queries", "13" * 32
        ),
        "heldout_query_ids_sha256": digests.get(
            "heldout_queries", "14" * 32
        ),
        "benchmark_input_sha256s": {
            name: digests.get(name, f"{index:02d}" * 32)
            for index, name in enumerate(
                history_eval.COMMITMENT_INPUTS, start=15
            )
        },
        "selected_thresholds": {
            name: 0.8 for name in history_eval.RELATION_GAINS
        },
        "error_budgets": {
            "max_false_duplicate_rate": 0.2,
            "max_false_internal_no_match_rate": 0.2,
        },
        "selected_depths": {
            "per_channel_depth": int(policy["per_channel_depth"]),
            "final_lineage_count": int(policy["final_lineage_count"]),
            "comparator_cutoff": int(policy["comparator_cutoff"]),
        },
        "latency_target_ms_p95": 1000,
        "token_budget": int(policy["max_retrieval_tokens"]),
        "sealed_at": "2026-07-23T23:59:59Z",
    }


def synthetic_calibration_capability_body(
    *,
    policy,
    trust_root_id,
    commitment,
    receipt,
    digests=None,
):
    digests = digests or {}
    return {
        "schema_version": history_eval.CAPABILITY_SCHEMA_VERSION,
        "scope": SYNTHETIC_SCOPE,
        "trust_root_id": trust_root_id,
        "policy_commitment_sha256": sha256(
            canonical_bytes(commitment)
        ),
        "preheldout_receipt_sha256": sha256(
            canonical_bytes(receipt)
        ),
        "policy_version": policy["retrieval_policy_version"],
        "policy_sha256": sha256(canonical_bytes(policy)),
        "benchmark_snapshot_sha256": digests.get(
            "snapshot", "22" * 32
        ),
        "qrels_sha256": digests.get("qrels", "23" * 32),
        "adjudications_sha256": digests.get(
            "adjudications", "24" * 32
        ),
        "relation_heldout_counts":
            _synthetic_relation_heldout_counts(),
        "unresolved_adjudications": 0,
        "heldout_output_sha256": digests.get(
            "heldout_output", "25" * 32
        ),
        "heldout_run_nonce": receipt["run_nonce"],
        "heldout_started_at": "2026-07-24T00:00:01Z",
        "heldout_completed_at": "2026-07-24T00:00:02Z",
        "criteria_sha256": history_eval._criteria_sha(commitment),
        "evaluation_evidence": _synthetic_evaluation_evidence(
            commitment
        ),
    }


def _capability_seal_material(value):
    result = dict(value)
    result.pop("canonical_seal_sha256", None)
    result.pop("signature", None)
    return result


def seal_test_calibration_capability(value, trust_root):
    """Seal one synthetic capability for offline contract tests."""
    version = value.get("schema_version") if isinstance(value, dict) else None
    if (
        type(version) is not int
        or version not in {
            history_eval.LEGACY_CAPABILITY_SCHEMA_VERSION,
            history_eval.CAPABILITY_SCHEMA_VERSION,
        }
    ):
        raise CalibrationError(
            "calibration capability schema is invalid"
        )
    result = dict(value)
    domain_version = (
        "v2"
        if version == history_eval.CAPABILITY_SCHEMA_VERSION
        else "v1"
    )
    result["canonical_seal_sha256"] = sha256(
        ("history-calibration-capability-%s\0" % domain_version).encode(
            "ascii"
        )
        + canonical_bytes(_capability_seal_material(result))
    )
    result["signature"] = _test_signature(
        (
            "history-calibration-capability-signature-%s\0"
            % domain_version
        ).encode("ascii"),
        result,
        trust_root,
    )
    return result


def _validate_commitment(commitment, policy):
    _require_fields(
        commitment,
        set(history_eval.COMMITMENT_FIELDS),
        "policy commitment",
    )
    if (
        type(commitment["schema_version"]) is not int
        or commitment["schema_version"] != 1
        or commitment["scope"] not in {
            SYNTHETIC_SCOPE,
            PRODUCTION_SCOPE,
        }
        or commitment["policy_version"]
        != policy["retrieval_policy_version"]
        or commitment["policy_sha256"]
        != sha256(canonical_bytes(policy))
    ):
        raise CalibrationError("policy commitment does not bind policy")
    _require_hashes(
        commitment,
        {
            "policy_sha256",
            "split_sha256",
            "calibration_query_ids_sha256",
            "heldout_query_ids_sha256",
        },
        "policy commitment",
    )
    inputs = commitment["benchmark_input_sha256s"]
    if (
        not isinstance(inputs, dict)
        or set(inputs) != set(history_eval.COMMITMENT_INPUTS)
        or any(not _valid_sha256(digest) for digest in inputs.values())
    ):
        raise CalibrationError("benchmark input commitment is invalid")
    thresholds = commitment["selected_thresholds"]
    if (
        not isinstance(thresholds, dict)
        or set(thresholds) != set(history_eval.RELATION_GAINS)
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 <= float(value) <= 1
            for value in thresholds.values()
        )
    ):
        raise CalibrationError("selected thresholds are invalid")
    budgets = commitment["error_budgets"]
    if (
        not isinstance(budgets, dict)
        or set(budgets)
        != {
            "max_false_duplicate_rate",
            "max_false_internal_no_match_rate",
        }
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 <= float(value) <= 1
            for value in budgets.values()
        )
    ):
        raise CalibrationError("error budgets are invalid")
    depths = commitment["selected_depths"]
    if (
        not isinstance(depths, dict)
        or set(depths)
        != {
            "per_channel_depth",
            "final_lineage_count",
            "comparator_cutoff",
        }
        or any(
            type(value) is not int
            or value < 1
            or value != policy.get(name)
            for name, value in depths.items()
        )
    ):
        raise CalibrationError("selected depths are invalid")
    latency_target = commitment["latency_target_ms_p95"]
    if (
        isinstance(latency_target, bool)
        or not isinstance(latency_target, (int, float))
        or not math.isfinite(float(latency_target))
        or float(latency_target) <= 0
        or type(commitment["token_budget"]) is not int
        or commitment["token_budget"]
        != policy.get("max_retrieval_tokens")
    ):
        raise CalibrationError("resource targets are invalid")
    _parse_utc(commitment["sealed_at"], "commitment seal time")


def _validate_preheldout_receipt(
    receipt,
    commitment,
    trust_root,
    required_scope,
    production_witness,
):
    _require_fields(
        receipt,
        {
            "schema_version",
            "scope",
            "trust_root_id",
            "policy_commitment_sha256",
            "split_sha256",
            "trusted_runner_release_sha256",
            "run_nonce",
            "witness_time",
            "signature",
        },
        "pre-held-out receipt",
    )
    if (
        type(receipt["schema_version"]) is not int
        or receipt["schema_version"] != 1
        or receipt["scope"] != required_scope
        or receipt["policy_commitment_sha256"]
        != sha256(canonical_bytes(commitment))
        or receipt["split_sha256"] != commitment["split_sha256"]
        or type(receipt["run_nonce"]) is not int
        or receipt["run_nonce"] < 1
    ):
        raise CalibrationError("pre-held-out receipt binding is invalid")
    _require_hashes(
        receipt,
        {
            "policy_commitment_sha256",
            "split_sha256",
            "trusted_runner_release_sha256",
            "signature",
        },
        "pre-held-out receipt",
    )
    _parse_utc(receipt["witness_time"], "witness time")
    if required_scope == SYNTHETIC_SCOPE:
        if (
            trust_root.get("scope") != SYNTHETIC_SCOPE
            or receipt["trust_root_id"]
            != trust_root.get("trust_root_id")
        ):
            raise CalibrationError("synthetic witness root mismatch")
        unsigned = dict(receipt)
        signature = unsigned.pop("signature")
        expected = _test_signature(
            b"history-preheldout-receipt-v1\0",
            unsigned,
            trust_root,
        )
        if not hmac.compare_digest(signature, expected):
            raise CalibrationError(
                "pre-held-out receipt signature is invalid"
            )
    else:
        if (
            trust_root.get("scope") != PRODUCTION_SCOPE
            or receipt["trust_root_id"]
            != trust_root.get("trust_root_id")
            or not isinstance(production_witness, dict)
            or production_witness.get("artifact_kind")
            != "preheldout_receipt"
            or production_witness.get("artifact_sha256")
            != sha256(canonical_bytes(receipt))
        ):
            raise CalibrationError(
                "production witness verifier is unavailable"
            )


def _validate_capability(
    capability,
    commitment,
    receipt,
    policy,
    trust_root,
    required_scope,
    production_witness,
):
    try:
        history_eval.validate_capability_artifact(
            capability, required_scope=required_scope
        )
    except history_eval.BenchmarkError as exc:
        raise CalibrationError(str(exc)) from exc
    _require_fields(
        capability,
        set(history_eval.CAPABILITY_FIELDS),
        "calibration capability",
    )
    if (
        capability["trust_root_id"] != receipt["trust_root_id"]
        or capability["policy_commitment_sha256"]
        != sha256(canonical_bytes(commitment))
        or capability["preheldout_receipt_sha256"]
        != sha256(canonical_bytes(receipt))
        or capability["policy_version"]
        != policy["retrieval_policy_version"]
        or capability["policy_sha256"]
        != sha256(canonical_bytes(policy))
        or capability["heldout_run_nonce"] != receipt["run_nonce"]
        or capability["unresolved_adjudications"] != 0
    ):
        raise CalibrationError("calibration capability binding is invalid")
    counts = capability["relation_heldout_counts"]
    if required_scope == PRODUCTION_SCOPE and any(
        item["advisory"] for item in counts.values()
    ):
        raise CalibrationError(
            "calibration held-out counts are insufficient"
        )
    capability_version = capability["schema_version"]
    if capability_version == history_eval.CAPABILITY_SCHEMA_VERSION:
        if capability["criteria_sha256"] != history_eval._criteria_sha(
            commitment
        ):
            raise CalibrationError(
                "calibration capability criteria are invalid"
            )
        evidence = capability["evaluation_evidence"]
        expected_primary = commitment["selected_thresholds"]
        expected_budgets = commitment["error_budgets"]
        expected_resources = {
            "latency_target_ms_p95": commitment[
                "latency_target_ms_p95"
            ],
            "token_budget": commitment["token_budget"],
        }
        expected_depths = commitment["selected_depths"]
        if (
            any(
                evidence["primary_metrics"][name]["minimum"]
                != expected_primary[name]
                for name in expected_primary
            )
            or any(
                evidence["error_budgets"][name]["maximum"]
                != expected_budgets[name]
                for name in expected_budgets
            )
            or any(
                evidence["resource_limits"][name]["maximum"]
                != expected_resources[name]
                for name in expected_resources
            )
            or any(
                evidence["selected_depths"][name]["maximum"]
                != expected_depths[name]
                for name in expected_depths
            )
        ):
            raise CalibrationError(
                "calibration capability criteria are invalid"
            )
    elif required_scope == PRODUCTION_SCOPE:
        raise CalibrationError(
            "legacy capability cannot enable production"
        )
    sealed_at = _parse_utc(
        commitment["sealed_at"], "commitment seal time"
    )
    witness_time = _parse_utc(
        receipt["witness_time"], "witness time"
    )
    heldout_start = _parse_utc(
        capability["heldout_started_at"],
        "held-out start time",
    )
    if capability_version == history_eval.CAPABILITY_SCHEMA_VERSION:
        heldout_completed = _parse_utc(
            capability["heldout_completed_at"],
            "held-out completion time",
        )
    else:
        heldout_completed = heldout_start
    if not (
        sealed_at < witness_time < heldout_start <= heldout_completed
    ):
        raise CalibrationError(
            "calibration capability chronology is invalid"
        )
    domain_version = (
        "v2"
        if capability_version == history_eval.CAPABILITY_SCHEMA_VERSION
        else "v1"
    )
    expected_seal = sha256(
        ("history-calibration-capability-%s\0" % domain_version).encode(
            "ascii"
        )
        + canonical_bytes(_capability_seal_material(capability))
    )
    if capability["canonical_seal_sha256"] != expected_seal:
        raise CalibrationError("calibration capability seal is invalid")
    if required_scope == SYNTHETIC_SCOPE:
        unsigned = dict(capability)
        signature = unsigned.pop("signature")
        expected_signature = _test_signature(
            (
                "history-calibration-capability-signature-%s\0"
                % domain_version
            ).encode("ascii"),
            unsigned,
            trust_root,
        )
        if not hmac.compare_digest(signature, expected_signature):
            raise CalibrationError(
                "calibration capability signature is invalid"
            )
    elif (
        not isinstance(production_witness, dict)
        or production_witness.get("artifact_kind")
        != "calibration_capability"
        or production_witness.get("artifact_sha256")
        != sha256(canonical_bytes(capability))
    ):
        raise CalibrationError(
            "production capability verifier is unavailable"
        )


def _runtime_authority_material(
    policy,
    capability=None,
    trust_root=None,
    required_scope=PRODUCTION_SCOPE,
):
    mode = policy.get("mode") if isinstance(policy, dict) else None
    if mode == "shadow":
        result = {
            "mode": "shadow",
            "policy_sha256": sha256(canonical_bytes(policy)),
            "capability_sha256": None,
            "policy_commitment_sha256": None,
            "preheldout_receipt_sha256": None,
            "trust_root_sha256": None,
            "scope": None,
        }
    elif mode == "enforcement":
        if required_scope not in {
            PRODUCTION_SCOPE,
            SYNTHETIC_SCOPE,
        }:
            raise CalibrationError("calibration scope is invalid")
        bundle = _load_closed_json(
            capability, "calibration capability bundle"
        )
        root = _load_closed_json(trust_root, "calibration trust root")
        _require_fields(
            bundle,
            {
                "schema_version",
                "policy_commitment",
                "preheldout_receipt",
                "calibration_capability",
            },
            "calibration capability bundle",
        )
        if (
            type(bundle["schema_version"]) is not int
            or bundle["schema_version"] != 1
        ):
            raise CalibrationError(
                "calibration capability bundle is invalid"
            )
        if not isinstance(root, dict):
            raise CalibrationError(
                "calibration trust root is unavailable"
            )
        commitment = bundle["policy_commitment"]
        receipt = bundle["preheldout_receipt"]
        sealed = bundle["calibration_capability"]
        _validate_commitment(commitment, policy)
        if commitment["scope"] != required_scope:
            raise CalibrationError(
                "calibration commitment scope is invalid"
            )
        if (
            required_scope == PRODUCTION_SCOPE
            and (
                root.get("scope") == SYNTHETIC_SCOPE
                or sealed.get("scope") == SYNTHETIC_SCOPE
            )
        ):
            raise CalibrationError(
                "synthetic_contract_only cannot enable production"
            )
        receipt_witness = None
        capability_witness = None
        trust_root_sha = None
        if required_scope == PRODUCTION_SCOPE:
            try:
                receipt_witness = (
                    history_witness.verify_production_artifact(
                        trust_root,
                        "preheldout_receipt",
                        receipt,
                    )
                )
                capability_witness = (
                    history_witness.verify_production_artifact(
                        trust_root,
                        "calibration_capability",
                        sealed,
                    )
                )
            except history_witness.WitnessError as exc:
                raise CalibrationError(
                    "production witness verification failed"
                ) from exc
            trust_root_sha = receipt_witness[
                "trust_root_sha256"
            ]
            if (
                capability_witness["trust_root_sha256"]
                != trust_root_sha
            ):
                raise CalibrationError(
                    "production witness trust root changed"
                )
            if (
                capability_witness[
                    "verifier_executable_sha256"
                ]
                != receipt_witness[
                    "verifier_executable_sha256"
                ]
            ):
                raise CalibrationError(
                    "production witness verifier changed"
                )
        _validate_preheldout_receipt(
            receipt,
            commitment,
            root,
            required_scope,
            receipt_witness,
        )
        _validate_capability(
            sealed,
            commitment,
            receipt,
            policy,
            root,
            required_scope,
            capability_witness,
        )
        result = {
            "mode": "enforcement",
            "policy_sha256": sha256(canonical_bytes(policy)),
            "capability_sha256": sha256(canonical_bytes(bundle)),
            "policy_commitment_sha256": sha256(
                canonical_bytes(commitment)
            ),
            "preheldout_receipt_sha256": sha256(
                canonical_bytes(receipt)
            ),
            "trust_root_sha256": trust_root_sha,
            "scope": required_scope,
        }
    else:
        raise CalibrationError("retrieval policy mode is invalid")
    return result


def validate_runtime_mode(
    policy,
    capability=None,
    trust_root=None,
):
    """Issue the production runtime authority for one policy."""
    return _validate_production_authority(
        policy,
        capability=capability,
        trust_root=trust_root,
    )


def _validate_runtime_mode_for_test(
    policy,
    *,
    capability,
    trust_root,
):
    """Issue a synthetic authority for isolated offline contract tests."""
    return _validate_synthetic_authority_for_test(
        policy,
        capability=capability,
        trust_root=trust_root,
    )


def startup_runtime(
    *,
    db_path,
    ledger_path,
    ledger_good_path,
    state_root,
    policy_path,
    brief_path,
    divergence_lens="",
    near_sa_path=None,
    calibration_capability_path=None,
    production_trust_root_path=None,
):
    database = _verified_history_database(
        db_path, allow_missing=True
    )
    ledger = pathlib.Path(ledger_path)
    ledger_good = pathlib.Path(ledger_good_path)
    state = pathlib.Path(state_root)
    brief_destination = pathlib.Path(brief_path)
    if database.parent.resolve() != state.resolve():
        raise RuntimeContractError(
            "history database must be inside the declared state root"
        )
    if (
        not isinstance(divergence_lens, str)
        or "\x00" in divergence_lens
        or len(divergence_lens.encode("utf-8"))
        > DIVERGENCE_LENS_MAX_BYTES
    ):
        raise RuntimeContractError("divergence lens exceeds its bound")
    policy = history_projection.load_policy(policy_path)
    mode_receipt = validate_runtime_mode(
        policy,
        capability=calibration_capability_path,
        trust_root=production_trust_root_path,
    )
    database_preexisting = database.exists()
    if not database_preexisting and not ledger.is_file():
        raise RuntimeContractError(
            "initial migration requires an operator ledger"
        )
    for directory_path in (
        state,
        ledger_good.parent,
        brief_destination.parent,
    ):
        directory = _open_safe_directory(
            directory_path, create=True
        )
        os.close(directory)
    conn = _connect_history_store(
        database, allow_missing=True
    )
    try:
        history_store.init_schema(conn)
        marker_row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?",
            (history_store.BOOTSTRAP_MARKER_KEY,),
        ).fetchone()
        bootstrap = None
        if marker_row is None:
            if not ledger.is_file():
                raise RuntimeContractError(
                    "incomplete bootstrap requires the operator ledger"
                )
            try:
                bootstrap = history_store.bootstrap_import_epoch(
                    conn,
                    ledger,
                    near_sa_path,
                    state_root=state,
                )
            except history_store.HistoryStoreError as exc:
                raise RuntimeContractError(str(exc)) from exc
            marker_row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = ?",
                (history_store.BOOTSTRAP_MARKER_KEY,),
            ).fetchone()
        try:
            bootstrap_marker = (
                history_store.validated_bootstrap_marker(conn)
            )
        except history_store.HistoryStoreError as exc:
            raise RuntimeContractError(str(exc)) from exc
        imported = (
            bootstrap is not None
            and not bootstrap["idempotent"]
        )
        targets = {
            "ledger.tsv": ledger,
            "tmp/ledger.good": ledger_good,
        }
        projection = history_store.reconcile_ledger_projection(
            conn, targets, state, reclaim_live=True
        )
        search = history_projection.recover(conn, policy)
        validation = history_projection.validate_published_generation(
            conn, policy
        )
        if not validation["valid"]:
            raise RuntimeContractError(
                "search generation is not current"
            )
        brief = history_projection.build_generation_brief(
            conn,
            policy,
            divergence_lens=divergence_lens,
        )
        brief_raw = history_projection.generation_brief_bytes(brief)
        _atomic_write(brief_destination, brief_raw)
        store_validation = history_store.validate_store(conn)
        if not store_validation["ok"]:
            raise RuntimeContractError(
                "canonical history validation failed"
            )
        return {
            "schema_version": 1,
            "imported": imported,
            "bootstrap_marker_sha256": sha256(
                canonical_bytes(bootstrap_marker)
            ),
            "policy_mode": mode_receipt["mode"],
            "policy_sha256": sha256(canonical_bytes(policy)),
            "capability_sha256": mode_receipt[
                "capability_sha256"
            ],
            "trust_root_sha256": mode_receipt[
                "trust_root_sha256"
            ],
            "source_watermark": brief["source_watermark"],
            "index_generation": brief["index_generation"],
            "brief_path": str(brief_destination),
            "brief_sha256": sha256(brief_raw),
            "ledger_projection": projection,
            "search_projection": search,
        }
    finally:
        conn.close()


def _candidate_material(candidate):
    value = dict(candidate)
    value.pop("content_sha256", None)
    return value


def candidate_content_sha256(candidate):
    if not isinstance(candidate, dict):
        raise RuntimeContractError("candidate must be an object")
    return sha256(
        b"history-runtime-candidate-v1\0"
        + canonical_bytes(_candidate_material(candidate))
    )


def _candidate_blocks(markdown):
    matches = list(
        re.finditer(r"(?m)^## (I[1-9][0-9]*)[ \t]*$", markdown)
    )
    candidate_ids = [match.group(1) for match in matches]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise RuntimeContractError(
            "generated markdown repeats a candidate heading"
        )
    result = {}
    for index, match in enumerate(matches):
        end = (
            len(markdown)
            if index + 1 == len(matches)
            else matches[index + 1].start()
        )
        result[match.group(1)] = markdown[match.start():end].rstrip() + "\n"
    return result


def _declared_parent(block):
    declaration_lines = re.findall(
        r"(?mi)^[ \t]*(?:Evolved[ \t]+from|Recheck(?:[ \t]+of)?)"
        r"[^\r\n]*$",
        block,
    )
    matches = re.findall(
        r"(?mi)^(?:Evolved from|Recheck(?: of)?):[ \t]*(\S+)[ \t]*$",
        block,
    )
    if len(declaration_lines) != len(matches):
        raise RuntimeContractError(
            "candidate evolution declaration is malformed"
        )
    if len(matches) > 1:
        raise RuntimeContractError(
            "candidate declares more than one parent"
        )
    return None if not matches else matches[0]


def freeze_candidate_batch(
    ideas_tsv,
    ideas_md,
    output_root,
    generation_brief=None,
    direction_contract=None,
    expected_direction=_DIRECTION_UNSPECIFIED,
):
    tsv_path = pathlib.Path(ideas_tsv)
    markdown_path = pathlib.Path(ideas_md)
    tsv_raw = _read_bound_regular(
        tsv_path, "generated candidate TSV"
    )
    markdown_raw = _read_bound_regular(
        markdown_path, "generated candidate markdown"
    )
    if len(tsv_raw) > 65536 or len(markdown_raw) > 65536:
        raise RuntimeContractError(
            "generated candidate source exceeds its bound"
        )
    try:
        markdown = markdown_raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeContractError(
            "generated candidate markdown is not UTF-8"
        ) from exc
    blocks = _candidate_blocks(markdown)
    rows = []
    seen = set()
    for raw in tsv_raw.splitlines():
        try:
            fields = raw.decode("utf-8").split("\t")
        except UnicodeDecodeError as exc:
            raise RuntimeContractError(
                "generated candidate TSV is not UTF-8"
            ) from exc
        if len(fields) != 3:
            raise RuntimeContractError(
                "generated candidate row must have three fields"
            )
        candidate_id, story, theme = fields
        if (
            not re.fullmatch(r"I[1-9][0-9]*", candidate_id)
            or candidate_id in seen
            or not story.strip()
            or not theme.strip()
            or candidate_id not in blocks
        ):
            raise RuntimeContractError(
                "generated candidate identity is invalid"
            )
        seen.add(candidate_id)
        block = blocks[candidate_id]
        markdown_stories = re.findall(
            r"(?m)^One-Sentence Story:[ \t]*(.*\S)[ \t]*$",
            block,
        )
        markdown_themes = re.findall(
            r"(?m)^Theme:[ \t]*(.*\S)[ \t]*$",
            block,
        )
        if len(markdown_stories) != 1 or len(markdown_themes) != 1:
            raise RuntimeContractError(
                "candidate markdown identity fields are invalid"
            )
        # Markdown is canonical. Contained generate already reprojects TSV
        # from markdown; freeze still reprojects so drifted hand-fed TSV
        # (or pre-reconcile artifacts) does not fail the batch.
        story = markdown_stories[0]
        theme = markdown_themes[0]
        if (
            len(story.encode("utf-8")) > 1024
            or len(theme.encode("utf-8")) > 1024
            or "\t" in story
            or "\t" in theme
        ):
            raise RuntimeContractError(
                "candidate identity exceeds TSV bounds"
            )
        parent = _declared_parent(block)
        if parent is not None:
            if generation_brief is None:
                raise RuntimeContractError(
                    "declared parent requires the generation brief"
                )
            brief_parent = generation_brief.get("parent")
            if (
                not isinstance(brief_parent, dict)
                or brief_parent.get("candidate_id") != parent
            ):
                raise RuntimeContractError(
                    "declared parent is not the validated brief parent"
                )
        row_raw = f"{candidate_id}\t{story}\t{theme}".encode("utf-8")
        candidate = {
            "candidate_id": candidate_id,
            "story": story,
            "theme": theme,
            "candidate_markdown": block,
            "tsv_row_sha256": sha256(row_raw),
            "markdown_sha256": sha256(block.encode("utf-8")),
            "declared_parent_candidate_id": parent,
        }
        candidate["content_sha256"] = candidate_content_sha256(
            candidate
        )
        rows.append(candidate)
    if set(blocks) != seen or not rows:
        raise RuntimeContractError(
            "generated candidate TSV and markdown differ"
        )
    if direction_contract is None:
        direction_identity = None
    else:
        try:
            _, _, direction_identity = (
                direction_contract_lib.parse_contract_bytes(
                    canonical_bytes(direction_contract)
                )
            )
        except direction_contract_lib.DirectionContractError as exc:
            raise RuntimeContractError(
                "direction contract is invalid"
            ) from exc
    if expected_direction is not _DIRECTION_UNSPECIFIED:
        try:
            expected_direction = (
                direction_contract_lib.validate_identity(
                    expected_direction
                )
            )
        except direction_contract_lib.DirectionContractError as exc:
            raise RuntimeContractError(
                "expected direction identity is invalid"
            ) from exc
        if direction_identity != expected_direction:
            raise RuntimeContractError(
                "direction identity changed before batch freeze"
            )
    root = pathlib.Path(
        os.path.abspath(os.fspath(output_root))
    )
    _mkdir_single_use(root)
    frozen_tsv = root / "sources" / "ideas.tsv"
    frozen_markdown = root / "sources" / "ideas.md"
    reconciled_tsv = (
        b"\n".join(
            f"{item['candidate_id']}\t{item['story']}\t{item['theme']}".encode(
                "utf-8"
            )
            for item in rows
        )
        + b"\n"
    )
    _publish_immutable(frozen_tsv, reconciled_tsv)
    _publish_immutable(frozen_markdown, markdown_raw)
    publications = []
    for candidate in rows:
        destination = root / (candidate["candidate_id"] + ".json")
        raw = canonical_bytes(candidate)
        _publish_immutable(destination, raw)
        publications.append(
            {
                "candidate_id": candidate["candidate_id"],
                "path": str(destination),
                "sha256": sha256(raw),
                "content_sha256": candidate["content_sha256"],
            }
        )
    manifest = {
        "schema_version": 2,
        "artifact_root": str(root),
        "generation_brief_sha256": (
            None
            if generation_brief is None
            else sha256(canonical_bytes(generation_brief))
        ),
        "direction": direction_identity,
        "ideas_tsv": {
            "path": str(frozen_tsv),
            "sha256": sha256(reconciled_tsv),
        },
        "ideas_markdown": {
            "path": str(frozen_markdown),
            "sha256": sha256(markdown_raw),
        },
        "candidate_count": len(rows),
        "candidates": publications,
    }
    manifest["batch_sha256"] = sha256(
        b"history-runtime-batch-v2\0" + canonical_bytes(manifest)
    )
    _publish_immutable(
        root / "batch.json", canonical_bytes(manifest)
    )
    return manifest


def frozen_batch_direction(manifest):
    """Return None for schema v1 or the validated schema-v2 direction identity."""
    if not isinstance(manifest, dict):
        raise RuntimeContractError("frozen batch manifest is invalid")
    schema_version = manifest.get("schema_version")
    if type(schema_version) is not int:
        raise RuntimeContractError("frozen batch manifest is invalid")
    if schema_version == 1:
        return None
    if schema_version != 2 or "direction" not in manifest:
        raise RuntimeContractError("frozen batch manifest is invalid")
    try:
        return direction_contract_lib.validate_identity(
            manifest["direction"]
        )
    except direction_contract_lib.DirectionContractError as exc:
        raise RuntimeContractError(
            "frozen batch direction identity is invalid"
        ) from exc


def verify_frozen_batch(manifest):
    v1_fields = {
        "schema_version",
        "artifact_root",
        "generation_brief_sha256",
        "ideas_tsv",
        "ideas_markdown",
        "candidate_count",
        "candidates",
        "batch_sha256",
    }
    v2_fields = v1_fields | {"direction"}
    schema_version = (
        manifest.get("schema_version")
        if isinstance(manifest, dict)
        else None
    )
    if (
        not isinstance(manifest, dict)
        or type(schema_version) is not int
        or schema_version not in {1, 2}
        or set(manifest)
        != (v1_fields if schema_version == 1 else v2_fields)
    ):
        raise RuntimeContractError("frozen batch manifest is invalid")
    if (
        manifest["generation_brief_sha256"] is not None
        and not _valid_sha256(
            manifest["generation_brief_sha256"]
        )
    ):
        raise RuntimeContractError(
            "frozen generation brief hash is invalid"
        )
    material = dict(manifest)
    batch_sha = material.pop("batch_sha256")
    hash_domain = (
        b"history-runtime-batch-v1\0"
        if schema_version == 1
        else b"history-runtime-batch-v2\0"
    )
    if batch_sha != sha256(
        hash_domain + canonical_bytes(material)
    ):
        raise RuntimeContractError("frozen batch hash is invalid")
    frozen_batch_direction(manifest)
    root = pathlib.Path(manifest["artifact_root"])
    try:
        root_state = root.lstat()
    except OSError as exc:
        raise RuntimeContractError(
            "frozen batch artifact root is unavailable"
        ) from exc
    if (
        not root.is_absolute()
        or root.is_symlink()
        or not stat.S_ISDIR(root_state.st_mode)
    ):
        raise RuntimeContractError(
            "frozen batch artifact root is invalid"
        )
    descriptor_fields = {"path", "sha256"}
    for source, relative_path in (
        ("ideas_tsv", "sources/ideas.tsv"),
        ("ideas_markdown", "sources/ideas.md"),
    ):
        raw = _read_rooted_frozen_descriptor(
            root,
            manifest[source],
            relative_path,
            f"frozen {source}",
            maximum=65536,
            fields=descriptor_fields,
        )
        if len(raw) > 65536:
            raise RuntimeContractError(
                "frozen generated source changed"
            )
    candidate_descriptors = manifest.get("candidates")
    if (
        type(manifest.get("candidate_count")) is not int
        or manifest["candidate_count"] < 1
        or not isinstance(candidate_descriptors, list)
        or len(candidate_descriptors)
        != manifest["candidate_count"]
    ):
        raise RuntimeContractError(
            "frozen candidate count is invalid"
        )
    seen = set()
    publication_fields = {
        "candidate_id",
        "path",
        "sha256",
        "content_sha256",
    }
    candidate_fields = {
        "candidate_id",
        "story",
        "theme",
        "candidate_markdown",
        "tsv_row_sha256",
        "markdown_sha256",
        "declared_parent_candidate_id",
        "content_sha256",
    }
    for descriptor in candidate_descriptors:
        candidate_id = (
            descriptor.get("candidate_id")
            if isinstance(descriptor, dict)
            else None
        )
        expected_path = root / f"{candidate_id}.json"
        if (
            not isinstance(descriptor, dict)
            or set(descriptor) != publication_fields
            or not re.fullmatch(
                r"I[1-9][0-9]*", candidate_id or ""
            )
            or candidate_id in seen
            or descriptor.get("path") != str(expected_path)
            or not _valid_sha256(descriptor.get("sha256"))
            or not _valid_sha256(
                descriptor.get("content_sha256")
            )
        ):
            raise RuntimeContractError(
                "frozen candidate descriptor is invalid"
            )
        seen.add(candidate_id)
        raw = _read_rooted_frozen_descriptor(
            root,
            descriptor,
            f"{candidate_id}.json",
            "frozen candidate",
            maximum=16384,
            fields=publication_fields,
        )
        try:
            candidate = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeContractError(
                "frozen candidate is invalid"
            ) from exc
        if (
            raw != canonical_bytes(candidate)
            or not isinstance(candidate, dict)
            or set(candidate) != candidate_fields
            or candidate.get("candidate_id") != candidate_id
            or not _valid_sha256(candidate.get("tsv_row_sha256"))
            or not _valid_sha256(candidate.get("markdown_sha256"))
            or
            candidate_content_sha256(candidate)
            != descriptor["content_sha256"]
            or candidate["content_sha256"]
            != descriptor["content_sha256"]
        ):
            raise RuntimeContractError(
                "frozen candidate content hash changed"
            )
    return True


def _source_descriptor(path, label, maximum=1024 * 1024):
    source = pathlib.Path(
        os.path.abspath(os.fspath(path))
    )
    raw = _read_bound_regular(
        source,
        label,
        maximum=maximum,
        allow_empty=True,
    )
    return {
        "path": str(source),
        "sha256": sha256(raw),
        "byte_count": len(raw),
    }, raw


def _validate_round_observation(
    batch, candidates, round_observation_path
):
    observation_path = pathlib.Path(
        os.path.abspath(os.fspath(round_observation_path))
    )
    observation = _load_canonical_json(
        observation_path, "round observation"
    )
    fields = {
        "schema_version",
        "batch_path",
        "candidates",
        "round_observation_sha256",
    }
    if (
        not isinstance(observation, dict)
        or set(observation) != fields
        or observation.get("schema_version") != 1
        or pathlib.Path(observation.get("batch_path", "")).resolve()
        != (pathlib.Path(batch["artifact_root"]) / "batch.json").resolve()
    ):
        raise RuntimeContractError(
            "round observation binding is invalid"
        )
    material = dict(observation)
    observation_sha = material.pop(
        "round_observation_sha256"
    )
    if observation_sha != sha256(
        b"history-runtime-round-observation-v1\0"
        + canonical_bytes(material)
    ):
        raise RuntimeContractError(
            "round observation ID is invalid"
        )
    expected = batch["candidates"]
    items = observation.get("candidates")
    item_fields = {
        "candidate_id",
        "candidate_content_sha256",
        "observation_path",
        "observation_sha256",
        "retrieval_statuses",
    }
    if (
        not isinstance(items, list)
        or len(items) != len(expected)
        or any(
            not isinstance(item, dict)
            or set(item) != item_fields
            for item in items
        )
        or [item["candidate_id"] for item in items]
        != [item["candidate_id"] for item in expected]
    ):
        raise RuntimeContractError(
            "round observation candidate coverage is invalid"
        )
    observation_root = observation_path.parent
    for item, descriptor in zip(items, expected):
        candidate_id = descriptor["candidate_id"]
        expected_path = (
            observation_root
            / candidate_id
            / "build-observation.json"
        )
        if (
            item["candidate_content_sha256"]
            != descriptor["content_sha256"]
            or item["observation_path"] != str(expected_path)
            or not _valid_sha256(item["observation_sha256"])
            or not isinstance(item["retrieval_statuses"], list)
        ):
            raise RuntimeContractError(
                "round observation candidate binding is invalid"
            )
        built = _load_canonical_json(
            expected_path, "candidate build observation"
        )
        _validated_build_observation(
            candidates[candidate_id],
            observation_root / candidate_id,
            built,
        )
        if (
            built.get("observation_sha256")
            != item["observation_sha256"]
            or built.get("candidate_id") != candidate_id
            or built.get("candidate_content_sha256")
            != descriptor["content_sha256"]
            or [
                value.get("retrieval_status")
                for value in built.get("observations", [])
            ]
            != item["retrieval_statuses"]
        ):
            raise RuntimeContractError(
                "candidate build observation changed"
            )
    return observation


def _selector_ranks(raw, candidates):
    ranks = {}
    try:
        lines = [
            line.decode("utf-8").split("\t")
            for line in raw.splitlines()
        ]
    except UnicodeDecodeError:
        return ranks
    for fields in lines:
        if len(fields) != 6:
            continue
        candidate_id, rank = fields[:2]
        if (
            candidate_id not in candidates
            or candidate_id in ranks
            or not rank.isdigit()
            or int(rank) < 1
        ):
            continue
        ranks[candidate_id] = int(rank)
    return ranks


def _prescreen_result(markdown, candidate_id):
    blocks = _candidate_blocks(markdown)
    block = blocks.get(candidate_id, "")
    decisions = re.findall(
        r"(?m)^Decision:[ \t]*(kill|keep)[ \t]*$",
        block,
    )
    if len(decisions) != 1 or decisions[0] != "kill":
        return {"decision": "keep", "evidence": None}
    query_pattern = re.compile(
        r"(?m)^- Query:[ \t]*https?://"
        r"(?:export\.arxiv\.org/api/query\?\S+|"
        r"api\.semanticscholar\.org/graph/v1/"
        r"[A-Za-z0-9._~%/:+-]+\?\S+)[ \t]*$"
    )
    occupant = re.findall(
        r"(?m)^Occupant:[ \t]*(https?://\S+)[ \t]*$",
        block,
    )
    if (
        not query_pattern.search(block)
        or len(occupant) != 1
        or "export.arxiv.org/api/query" in occupant[0]
        or "api.semanticscholar.org" in occupant[0]
    ):
        return {"decision": "keep", "evidence": None}
    return {"decision": "kill", "evidence": occupant[0]}


def _candidate_keep_rank(candidate):
    markdown = candidate["candidate_markdown"]
    if re.search(
        r"(?mi)^(?:Recheck(?: of)?|Evolved from):", markdown
    ):
        return 0
    if re.search(
        r"(?mi)^Form:[ \t]*"
        r"remove-load-bearing-assumption[ \t]*$",
        markdown,
    ):
        return 1
    return 2


def _selection_material(
    *,
    batch_path,
    round_observation_path,
    generation_brief_path,
    selector_path,
    prescreen_path,
    short_max,
    theme_min_low,
):
    if type(short_max) is not int or short_max < 1:
        raise RuntimeContractError(
            "round shortlist bound is invalid"
        )
    if type(theme_min_low) is not int or theme_min_low < 0:
        raise RuntimeContractError(
            "round low-theme bound is invalid"
        )
    batch = _load_canonical_json(
        batch_path, "frozen batch manifest"
    )
    verify_frozen_batch(batch)
    _, candidates = _load_batch_candidates(batch_path)
    round_observation = _validate_round_observation(
        batch, candidates, round_observation_path
    )
    sources = {}
    raw_sources = {}
    for name, path in (
        ("generation_brief", generation_brief_path),
        ("selector", selector_path),
        ("prescreen", prescreen_path),
    ):
        sources[name], raw_sources[name] = _source_descriptor(
            path, f"round selection {name}", maximum=65536
        )
    try:
        brief = json.loads(
            raw_sources["generation_brief"].decode("utf-8")
        )
        prescreen_markdown = raw_sources["prescreen"].decode(
            "utf-8"
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeContractError(
            "round selection source is invalid"
        ) from exc
    if (
        canonical_bytes(brief) != raw_sources["generation_brief"]
        or batch["generation_brief_sha256"]
        != sha256(raw_sources["generation_brief"])
        or not isinstance(brief.get("theme_counts"), dict)
        or brief.get("source_watermark") is None
        or brief.get("index_generation") is None
    ):
        raise RuntimeContractError(
            "round generation brief is invalid"
        )
    theme_counts = brief["theme_counts"]
    if (
        not theme_counts
        or any(
            not isinstance(theme, str)
            or not theme
            or type(count) is not int
            or count < 0
            for theme, count in theme_counts.items()
        )
    ):
        raise RuntimeContractError(
            "round theme inventory is invalid"
        )
    generated_themes = [
        candidates[item["candidate_id"]]["theme"]
        for item in batch["candidates"]
    ]
    if any(theme not in theme_counts for theme in generated_themes):
        raise RuntimeContractError(
            "candidate theme is outside the sealed inventory"
        )
    ordered_counts = sorted(theme_counts.values())
    low_threshold = ordered_counts[
        min(2, len(ordered_counts) - 1)
    ]
    low_hits = sum(
        theme_counts[theme] <= low_threshold
        for theme in generated_themes
    )
    if low_hits < theme_min_low:
        raise RuntimeContractError(
            "candidate batch lacks required low-inventory coverage"
        )
    ranks = _selector_ranks(
        raw_sources["selector"], candidates
    )
    kills = {}
    keeps = []
    for order, descriptor in enumerate(batch["candidates"]):
        candidate_id = descriptor["candidate_id"]
        candidate = candidates[candidate_id]
        prescreen = _prescreen_result(
            prescreen_markdown, candidate_id
        )
        if prescreen["decision"] == "kill":
            kills[candidate_id] = {
                "candidate_id": candidate_id,
                "candidate_content_sha256":
                    candidate["content_sha256"],
                "disposition": "prescreen_kill",
                "evidence": prescreen["evidence"],
            }
            continue
        theme_count = theme_counts[candidate["theme"]]
        keeps.append(
            (
                _candidate_keep_rank(candidate),
                ranks.get(candidate_id, 999),
                theme_count,
                order,
                candidate_id,
            )
        )
    shortlisted_ids = {
        item[4] for item in sorted(keeps)[:short_max]
    }
    targets = []
    for descriptor in batch["candidates"]:
        candidate_id = descriptor["candidate_id"]
        if candidate_id in kills:
            targets.append(kills[candidate_id])
        elif candidate_id in shortlisted_ids:
            targets.append(
                {
                    "candidate_id": candidate_id,
                    "candidate_content_sha256":
                        descriptor["content_sha256"],
                    "disposition": "shortlist",
                }
            )
    result = {
        "schema_version": 1,
        "batch_path": str(
            pathlib.Path(
                os.path.abspath(os.fspath(batch_path))
            )
        ),
        "batch_sha256": batch["batch_sha256"],
        "round_observation_path": str(
            pathlib.Path(
                os.path.abspath(
                    os.fspath(round_observation_path)
                )
            )
        ),
        "round_observation_sha256":
            round_observation["round_observation_sha256"],
        "sources": sources,
        "short_max": short_max,
        "theme_min_low": theme_min_low,
        "targets": targets,
    }
    return result


def seal_round_selection(
    *,
    batch_path,
    round_observation_path,
    generation_brief_path,
    selector_path,
    prescreen_path,
    short_max,
    theme_min_low=0,
    output_path,
):
    output = pathlib.Path(output_path)
    source_root = (
        output.parent / (output.stem + "-inputs")
    )
    _mkdir_single_use(source_root)
    frozen_sources = {}
    for name, source in (
        ("generation-brief.json", generation_brief_path),
        ("selector.tsv", selector_path),
        ("prescreen.md", prescreen_path),
    ):
        raw = _read_bound_regular(
            source,
            f"round selection source {name}",
            maximum=65536,
            allow_empty=True,
        )
        destination = source_root / name
        _publish_immutable(destination, raw)
        frozen_sources[name] = destination
    result = _selection_material(
        batch_path=batch_path,
        round_observation_path=round_observation_path,
        generation_brief_path=frozen_sources[
            "generation-brief.json"
        ],
        selector_path=frozen_sources["selector.tsv"],
        prescreen_path=frozen_sources["prescreen.md"],
        short_max=short_max,
        theme_min_low=theme_min_low,
    )
    result["selection_sha256"] = sha256(
        b"history-runtime-selection-v1\0"
        + canonical_bytes(result)
    )
    _publish_immutable(output, canonical_bytes(result))
    return result


def verify_round_selection(selection_path):
    selection = _load_canonical_json(
        selection_path, "round selection"
    )
    fields = {
        "schema_version",
        "batch_path",
        "batch_sha256",
        "round_observation_path",
        "round_observation_sha256",
        "sources",
        "short_max",
        "theme_min_low",
        "targets",
        "selection_sha256",
    }
    if (
        not isinstance(selection, dict)
        or set(selection) != fields
        or selection.get("schema_version") != 1
    ):
        raise RuntimeContractError(
            "round selection schema is invalid"
        )
    material = dict(selection)
    selection_sha = material.pop("selection_sha256")
    if selection_sha != sha256(
        b"history-runtime-selection-v1\0"
        + canonical_bytes(material)
    ):
        raise RuntimeContractError(
            "round selection ID is invalid"
        )
    sources = selection["sources"]
    if (
        not isinstance(sources, dict)
        or set(sources)
        != {"generation_brief", "selector", "prescreen"}
    ):
        raise RuntimeContractError(
            "round selection sources are invalid"
        )
    expected = _selection_material(
        batch_path=selection["batch_path"],
        round_observation_path=
            selection["round_observation_path"],
        generation_brief_path=
            sources["generation_brief"]["path"],
        selector_path=sources["selector"]["path"],
        prescreen_path=sources["prescreen"]["path"],
        short_max=selection["short_max"],
        theme_min_low=selection["theme_min_low"],
    )
    if expected != material:
        raise RuntimeContractError(
            "round selection source binding changed"
        )
    return selection


def _sealed_shortlist_order(batch, candidates, selection):
    brief = _load_canonical_json(
        selection["sources"]["generation_brief"]["path"],
        "selection generation brief",
    )
    selector_raw = _read_bound_regular(
        selection["sources"]["selector"]["path"],
        "selection selector",
        maximum=65536,
        allow_empty=True,
    )
    ranks = _selector_ranks(selector_raw, candidates)
    target_map = {
        item["candidate_id"]: item
        for item in selection["targets"]
    }
    killed = {
        candidate_id
        for candidate_id, target in target_map.items()
        if target["disposition"] == "prescreen_kill"
    }
    keeps = []
    for order, descriptor in enumerate(batch["candidates"]):
        candidate_id = descriptor["candidate_id"]
        if candidate_id in killed:
            continue
        candidate = candidates[candidate_id]
        if candidate["theme"] not in brief["theme_counts"]:
            raise RuntimeContractError(
                "candidate theme is outside the sealed inventory"
            )
        theme_count = brief["theme_counts"][
            candidate["theme"]
        ]
        if type(theme_count) is not int or theme_count < 0:
            raise RuntimeContractError(
                "selection theme inventory is invalid"
            )
        keeps.append(
            (
                _candidate_keep_rank(candidate),
                ranks.get(candidate_id, 999),
                theme_count,
                order,
                candidate_id,
            )
        )
    order = [
        item[4] for item in sorted(keeps)[:selection["short_max"]]
    ]
    selected = {
        item["candidate_id"]
        for item in selection["targets"]
        if item["disposition"] == "shortlist"
    }
    if set(order) != selected or len(order) != len(selected):
        raise RuntimeContractError(
            "selection shortlist order changed"
        )
    return order


def _frozen_candidate_source_views(batch, candidates):
    ideas_tsv_raw = _read_bound_regular(
        batch["ideas_tsv"]["path"],
        "frozen generated TSV",
        maximum=65536,
    )
    ideas_markdown_raw = _read_bound_regular(
        batch["ideas_markdown"]["path"],
        "frozen generated markdown",
        maximum=65536,
    )
    rows = {}
    for raw_line in ideas_tsv_raw.splitlines(keepends=True):
        candidate_id = raw_line.split(b"\t", 1)[0].decode(
            "utf-8"
        )
        rows[candidate_id] = (
            raw_line
            if raw_line.endswith(b"\n")
            else raw_line + b"\n"
        )
    try:
        source_markdown = ideas_markdown_raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeContractError(
            "frozen generated markdown is not UTF-8"
        ) from exc
    matches = list(
        re.finditer(
            r"(?m)^## (I[1-9][0-9]*)[ \t]*$",
            source_markdown,
        )
    )
    source_ids = [match.group(1) for match in matches]
    if len(source_ids) != len(set(source_ids)):
        raise RuntimeContractError(
            "frozen markdown repeats a candidate heading"
        )
    source_blocks = {}
    for index, match in enumerate(matches):
        end = (
            len(source_markdown)
            if index + 1 == len(matches)
            else matches[index + 1].start()
        )
        block = source_markdown[match.start():end]
        if not block.endswith("\n"):
            block += "\n"
        source_blocks[match.group(1)] = block
    if set(rows) != set(candidates) or set(source_blocks) != set(
        candidates
    ):
        raise RuntimeContractError(
            "frozen source views changed"
        )
    return {
        "ideas_tsv_raw": ideas_tsv_raw,
        "ideas_markdown_raw": ideas_markdown_raw,
        "rows": rows,
        "markdown_blocks": source_blocks,
    }


def materialize_round_views(
    *,
    batch_path,
    selection_path,
    output_root,
):
    selection = verify_round_selection(selection_path)
    batch, candidates = _load_batch_candidates(batch_path)
    if (
        selection["batch_sha256"] != batch["batch_sha256"]
        or pathlib.Path(selection["batch_path"]).resolve()
        != pathlib.Path(batch_path).resolve()
    ):
        raise RuntimeContractError(
            "selection views are outside the frozen batch"
        )
    brief = _load_canonical_json(
        selection["sources"]["generation_brief"]["path"],
        "selection generation brief",
    )
    selector_raw = _read_bound_regular(
        selection["sources"]["selector"]["path"],
        "selection selector",
        maximum=65536,
        allow_empty=True,
    )
    prescreen_raw = _read_bound_regular(
        selection["sources"]["prescreen"]["path"],
        "selection prescreen",
        maximum=65536,
        allow_empty=True,
    )
    try:
        prescreen_markdown = prescreen_raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeContractError(
            "selection prescreen is not UTF-8"
        ) from exc
    ranks = _selector_ranks(selector_raw, candidates)
    target_map = {
        item["candidate_id"]: item
        for item in selection["targets"]
    }
    keeps = []
    kills = []
    for order, descriptor in enumerate(batch["candidates"]):
        candidate_id = descriptor["candidate_id"]
        candidate = candidates[candidate_id]
        prescreen = _prescreen_result(
            prescreen_markdown, candidate_id
        )
        target = target_map.get(candidate_id)
        if prescreen["decision"] == "kill":
            if (
                target is None
                or target.get("disposition") != "prescreen_kill"
                or target.get("evidence") != prescreen["evidence"]
            ):
                raise RuntimeContractError(
                    "selection kill view changed"
                )
            kills.append(
                (
                    candidate_id,
                    candidate["story"],
                    candidate["theme"],
                    prescreen["evidence"],
                )
            )
            continue
        if candidate["theme"] not in brief["theme_counts"]:
            raise RuntimeContractError(
                "candidate theme is outside the sealed inventory"
            )
        theme_count = brief["theme_counts"][candidate["theme"]]
        if type(theme_count) is not int or theme_count < 0:
            raise RuntimeContractError(
                "selection theme inventory is invalid"
            )
        keeps.append(
            (
                _candidate_keep_rank(candidate),
                ranks.get(candidate_id, 999),
                theme_count,
                order,
                candidate_id,
                candidate["story"],
                candidate["theme"],
            )
        )
    keeps.sort()
    shortlist_order = [
        item[4]
        for item in keeps[:selection["short_max"]]
    ]
    selected_shortlist = [
        candidate_id
        for candidate_id in shortlist_order
        if candidate_id in target_map
        and target_map[candidate_id].get("disposition")
        == "shortlist"
    ]
    if (
        set(selected_shortlist)
        != {
            item["candidate_id"]
            for item in selection["targets"]
            if item["disposition"] == "shortlist"
        }
        or len(selected_shortlist) != len(shortlist_order)
    ):
        raise RuntimeContractError(
            "selection shortlist view changed"
        )
    source_views = _frozen_candidate_source_views(
        batch, candidates
    )
    ideas_tsv_raw = source_views["ideas_tsv_raw"]
    ideas_markdown_raw = source_views["ideas_markdown_raw"]
    rows = source_views["rows"]
    source_blocks = source_views["markdown_blocks"]
    output = pathlib.Path(
        os.path.abspath(os.fspath(output_root))
    )
    descriptor = _open_safe_directory(output, create=True)
    os.close(descriptor)
    raw_views = {
        "ideas.all.tsv": ideas_tsv_raw,
        "ideas.all.md": ideas_markdown_raw,
        "kills.tsv": "".join(
            "\t".join(item) + "\n" for item in kills
        ).encode("utf-8"),
        "keeps.tsv": "".join(
            "\t".join(
                (
                    str(item[0]),
                    str(item[1]),
                    str(item[2]),
                    str(item[3] + 1),
                    item[4],
                    item[5],
                    item[6],
                )
            )
            + "\n"
            for item in keeps
        ).encode("utf-8"),
        "ideas.tsv": b"".join(
            rows[candidate_id]
            for candidate_id in selected_shortlist
        ),
        "ideas.md": "".join(
            source_blocks[candidate_id] + "\n"
            for candidate_id in selected_shortlist
        ).encode("utf-8"),
    }
    publications = {}
    for name, raw in raw_views.items():
        path = output / name
        _publish_immutable(path, raw)
        publications[name] = {
            "path": str(path),
            "sha256": sha256(raw),
            "byte_count": len(raw),
        }
    return {
        "schema_version": 1,
        "selection_sha256": selection["selection_sha256"],
        "shortlist_order": selected_shortlist,
        "kill_order": [item[0] for item in kills],
        "views": publications,
    }


def materialize_research_views(
    *,
    db_path,
    policy_path,
    batch_path,
    selection_path,
    comparison_index_path,
    artifact_root,
    output_root,
    authority,
):
    """Seal research inputs after the history comparison gate."""
    state_paths = (
        db_path,
        policy_path,
        batch_path,
        selection_path,
        comparison_index_path,
        artifact_root,
        output_root,
    )
    _require_context_test_paths(state_paths)
    policy = history_projection.load_policy(policy_path)
    authority_value = _validated_runtime_authority(
        policy,
        authority,
        state_paths=state_paths,
    )
    batch, candidates = _load_batch_candidates(batch_path)
    selection = verify_round_selection(selection_path)
    root = pathlib.Path(
        os.path.abspath(os.fspath(artifact_root))
    )
    resolved_root = root.resolve()
    index_path = pathlib.Path(comparison_index_path).resolve()
    if (
        selection["batch_sha256"] != batch["batch_sha256"]
        or pathlib.Path(selection["batch_path"]).resolve()
        != pathlib.Path(batch_path).resolve()
        or pathlib.Path(
            selection["round_observation_path"]
        ).parent.resolve()
        != resolved_root
        or index_path
        != (root / "comparison-index.json").resolve()
    ):
        raise RuntimeContractError(
            "research views are outside the frozen round"
        )
    index = _comparison_index(index_path, selection)
    shortlist_order = _sealed_shortlist_order(
        batch, candidates, selection
    )
    source_views = _frozen_candidate_source_views(
        batch, candidates
    )
    target_map = {
        item["candidate_id"]: item
        for item in selection["targets"]
    }
    eligible_order = []
    abstentions = []
    summary_sources = []
    summary_index_descriptor = None
    matched_candidates = []
    allowed_statuses = (
        set(history_retrieval.PERMANENT_STATUSES)
        | {
            "partial",
            "backend_failed",
            "budget_exceeded",
            "uncertain",
            "conflicting_evidence",
        }
    )
    conn = _connect_history_store(db_path)
    history_store.init_schema(conn)
    try:
        current = _current_generation_binding(conn, policy)
        for indexed in index["targets"]:
            candidate_id = indexed["candidate_id"]
            candidate = candidates[candidate_id]
            observation_path = (
                root
                / candidate_id
                / "comparison-observation.json"
            )
            if (
                pathlib.Path(
                    indexed["observation_path"]
                ).resolve()
                != observation_path.resolve()
            ):
                raise RuntimeContractError(
                    "research comparison path changed"
                )
            observation = _load_canonical_json(
                observation_path,
                "research comparison observation",
            )
            material = dict(observation)
            observation_sha = material.pop(
                "observation_sha256", None
            )
            items = observation.get("observations")
            intents = required_intents(candidate)
            statuses = (
                [
                    item.get("status")
                    for item in items
                    if isinstance(item, dict)
                ]
                if isinstance(items, list)
                else []
            )
            if (
                not isinstance(observation, dict)
                or set(observation)
                != {
                    "schema_version",
                    "candidate_id",
                    "candidate_content_sha256",
                    "observations",
                    "observation_sha256",
                }
                or type(observation["schema_version"]) is not int
                or observation["schema_version"] != 1
                or observation_sha
                != indexed["observation_sha256"]
                or observation_sha
                != sha256(
                    b"history-runtime-observation-v1\0"
                    + canonical_bytes(material)
                )
                or observation["candidate_id"] != candidate_id
                or observation["candidate_content_sha256"]
                != candidate["content_sha256"]
                or not isinstance(items, list)
                or [
                    item.get("intent")
                    for item in items
                    if isinstance(item, dict)
                ]
                != intents
                or statuses != indexed["statuses"]
                or any(
                    status not in allowed_statuses
                    for status in statuses
                )
            ):
                raise RuntimeContractError(
                    "research comparison observation changed"
                )
            (
                comparison_executor,
                comparison_stage_records,
            ) = _comparison_stage_binding(index, indexed)
            pack_bindings = _validate_resume_comparator_stages(
                candidate=candidate,
                candidate_root=root / candidate_id,
                observation=observation,
                stage_records=comparison_stage_records,
                conn=conn,
                policy=policy,
                allow_unbindable=True,
                execution_boundary=comparison_executor,
            )
            for binding in pack_bindings:
                pack = binding["pack"]
                if (
                    pack["retrieval_policy_version"]
                    != policy["retrieval_policy_version"]
                    or pack["policy_sha256"]
                    != current["policy_sha256"]
                    or pack["source_watermark"]
                    != current["source_watermark"]
                    or pack["index_generation"]
                    != current["index_generation"]
                    or pack["generation_manifest_sha256"]
                    != current["generation_manifest_sha256"]
                ):
                    raise RuntimeContractError(
                        "research comparison generation changed"
                    )
            target = target_map[candidate_id]
            if target["disposition"] != "shortlist":
                continue
            if policy["mode"] == "shadow":
                eligible_order.append(candidate_id)
                continue
            permanent = (
                all(
                    status
                    in history_retrieval.PERMANENT_STATUSES
                    for status in statuses
                )
                and len(pack_bindings) == len(intents)
            )
            if not permanent:
                abstentions.append(
                    {
                        "candidate_id": candidate_id,
                        "statuses": statuses,
                    }
                )
                continue
            eligible_order.append(candidate_id)
            if any(
                status == "complete_match"
                for status in statuses
            ):
                matched_candidates.append(candidate_id)
        if policy["mode"] == "enforcement" and matched_candidates:
            (
                summary_index,
                verified_summaries,
            ) = _verified_summary_index(
                conn=conn,
                policy=policy,
                batch=batch,
                candidates=candidates,
                selection=selection,
                artifact_root=root,
            )
            summary_index_path = root / "summary-index.json"
            summary_index_descriptor = {
                "path": str(summary_index_path),
                "sha256": sha256(
                    canonical_bytes(summary_index)
                ),
                "summary_index_sha256": summary_index[
                    "summary_index_sha256"
                ],
            }
            for candidate_id in matched_candidates:
                verified_summary = verified_summaries.get(
                    candidate_id
                )
                if (
                    verified_summary is None
                    or verified_summary["overall_status"]
                    != "complete_match"
                ):
                    raise RuntimeContractError(
                        "research matched summary is unavailable"
                    )
                summary_path = (
                    root / candidate_id / "history-summary.json"
                )
                summary_sources.append(
                    (
                        candidate_id,
                        summary_path,
                        canonical_bytes(
                            verified_summary["summary"]
                        ),
                    )
                )
    finally:
        conn.close()
    if policy["mode"] == "shadow":
        eligible_order = list(shortlist_order)
        abstentions = []
        summary_sources = []
    elif any(
        candidate_id not in shortlist_order
        for candidate_id in eligible_order
    ):
        raise RuntimeContractError(
            "research eligibility escaped the shortlist"
        )
    eligible_set = set(eligible_order)
    eligible_order = [
        candidate_id
        for candidate_id in shortlist_order
        if candidate_id in eligible_set
    ]
    rows = source_views["rows"]
    blocks = source_views["markdown_blocks"]
    view_raw = {
        "ideas.tsv": b"".join(
            rows[candidate_id]
            for candidate_id in eligible_order
        ),
        "ideas.md": "".join(
            blocks[candidate_id] + "\n"
            for candidate_id in eligible_order
        ).encode("utf-8"),
    }
    output = pathlib.Path(
        os.path.abspath(os.fspath(output_root))
    )
    _mkdir_single_use(output)
    views = {}
    for name, raw in view_raw.items():
        path = output / name
        _publish_immutable(path, raw)
        views[name] = {
            "path": str(path.resolve()),
            "sha256": sha256(raw),
            "byte_count": len(raw),
        }
    summaries = []
    if summary_sources:
        summary_root = output / "history-summaries"
        _mkdir_single_use(summary_root)
        summary_source_map = {
            candidate_id: (source_path, raw)
            for candidate_id, source_path, raw in summary_sources
        }
        for candidate_id in eligible_order:
            if candidate_id not in summary_source_map:
                continue
            source_path, raw = summary_source_map[candidate_id]
            destination = summary_root / f"{candidate_id}.json"
            _publish_immutable(destination, raw)
            summaries.append(
                {
                    "candidate_id": candidate_id,
                    "source_path": str(source_path.resolve()),
                    "source_sha256": sha256(raw),
                    "path": str(destination.resolve()),
                    "sha256": sha256(raw),
                    "byte_count": len(raw),
                }
            )
    result = {
        "schema_version": 1,
        "mode": policy["mode"],
        "runtime_authority": {
            field: authority_value[field]
            for field in (
                "mode",
                "policy_sha256",
                "capability_sha256",
                "policy_commitment_sha256",
                "preheldout_receipt_sha256",
                "trust_root_sha256",
                "scope",
            )
        },
        "policy": {
            "path": str(pathlib.Path(policy_path).resolve()),
            "version": policy["retrieval_policy_version"],
            "sha256": current["policy_sha256"],
        },
        "history_store": {
            "path": str(pathlib.Path(db_path).resolve()),
            "source_watermark": current["source_watermark"],
            "index_generation": current["index_generation"],
            "generation_manifest_sha256":
                current["generation_manifest_sha256"],
        },
        "batch": {
            "path": str(pathlib.Path(batch_path).resolve()),
            "sha256": batch["batch_sha256"],
        },
        "selection": {
            "path": str(pathlib.Path(selection_path).resolve()),
            "sha256": selection["selection_sha256"],
        },
        "comparison_index": {
            "path": str(index_path),
            "sha256": index["comparison_index_sha256"],
        },
        "summary_index": summary_index_descriptor,
        "artifact_root": str(resolved_root),
        "output_root": str(output.resolve()),
        "shortlist_order": shortlist_order,
        "eligible_order": eligible_order,
        "abstentions": abstentions,
        "views": views,
        "summaries": summaries,
    }
    result["research_view_sha256"] = sha256(
        b"history-runtime-research-view-v1\0"
        + canonical_bytes(result)
    )
    _publish_immutable(
        output / "research-view.json",
        canonical_bytes(result),
    )
    return result


def required_intents(candidate):
    parent = candidate.get("declared_parent_candidate_id")
    intents = ["duplicate_search"]
    if parent:
        intents.append("evolution_search")
    intents.append("failure_pattern_search")
    return intents


def _retrieval_query(candidate, intent=None):
    content_sha = candidate_content_sha256(candidate)
    if candidate.get("content_sha256", content_sha) != content_sha:
        raise RuntimeContractError(
            "candidate content hash is invalid"
        )
    markdown = candidate.get("candidate_markdown")
    if not isinstance(markdown, str) or not markdown:
        raise RuntimeContractError(
            "frozen candidate markdown is required"
        )
    result = {
        field: candidate[field]
        for field in (
            "candidate_id",
            "story",
            "theme",
            "verdict",
            "reason",
            "category",
            "facets",
        )
        if field in candidate
    }
    result["candidate_content_sha256"] = content_sha
    result["candidate_markdown"] = markdown
    if intent == "failure_pattern_search":
        failure_lines = []
        for label in (
            "Target Failure",
            "Form",
            "Minimal Falsification Experiment",
        ):
            matches = re.findall(
                rf"(?mi)^{re.escape(label)}:[ \t]*(.*\S)[ \t]*$",
                markdown,
            )
            if len(matches) == 1:
                failure_lines.append(matches[0])
        if failure_lines:
            result.setdefault("facets", {})[
                "failure_pattern"
            ] = " ".join(failure_lines)
    if (
        intent == "evolution_search"
        and candidate.get("declared_parent_candidate_id")
    ):
        result["declared_parent_candidate_id"] = candidate[
            "declared_parent_candidate_id"
        ]
    return result


def _write_pack_trace(conn, pack, destination):
    trace = None
    if (
        conn is not None
        and isinstance(pack.get("pack_publication_id"), str)
    ):
        row = conn.execute(
            """
            SELECT rank_trace_json
            FROM history_pack_publications
            WHERE publication_id = ?
            """,
            (pack["pack_publication_id"],),
        ).fetchone()
        if row is not None:
            trace = json.loads(row["rank_trace_json"])
    if trace is None:
        trace = {
            "schema_version": 1,
            "intent": pack.get("intent"),
            "retrieval_status": pack.get("retrieval_status"),
            "infrastructure_only": True,
        }
    _publish_immutable(destination, canonical_bytes(trace))


def _write_candidate_observation(
    root, candidate, observations, filename
):
    result = {
        "schema_version": 1,
        "candidate_id": candidate["candidate_id"],
        "candidate_content_sha256":
            candidate_content_sha256(candidate),
        "observations": observations,
    }
    result["observation_sha256"] = sha256(
        b"history-runtime-observation-v1\0"
        + canonical_bytes(result)
    )
    _publish_immutable(root / filename, canonical_bytes(result))
    return result


def build_candidate_packs(
    *,
    conn,
    candidate,
    policy,
    artifact_root,
    pack_builder=history_retrieval.build_pack,
    comparator_role_bytes,
    comparator_role_identity,
):
    root = pathlib.Path(
        os.path.abspath(os.fspath(artifact_root))
    )
    root_descriptor = _open_safe_directory(root, create=True)
    os.close(root_descriptor)
    observations = []
    for intent in required_intents(candidate):
        intent_root = root / intent
        intent_descriptor = _open_safe_directory(
            intent_root, create=True
        )
        os.close(intent_descriptor)
        try:
            pack = pack_builder(
                conn,
                _retrieval_query(candidate, intent),
                intent,
                policy,
                comparator_role_bytes=comparator_role_bytes,
                comparator_role_identity=comparator_role_identity,
            )
        except (
            history_projection.ProjectionError,
            history_retrieval.RetrievalError,
        ) as exc:
            pack = {
                "schema_version": 1,
                "intent": intent,
                "retrieval_status": "backend_failed",
                "query": _retrieval_query(candidate, intent),
                "failure_code": type(exc).__name__,
            }
        _publish_immutable(
            intent_root / "retrieval-pack.json",
            canonical_bytes(pack),
        )
        _write_pack_trace(
            conn, pack, intent_root / "retrieval-trace.json"
        )
        retrieval_status = pack.get("retrieval_status")
        pack_path = _resolved_path_text(
            intent_root / "retrieval-pack.json"
        )
        item = {
            "intent": intent,
            "retrieval_status": retrieval_status,
            "status": retrieval_status,
            "pack_path": pack_path,
            "comparison_path": None,
            "receipt_path": None,
            "attempts": [
                {
                    "pack_path": pack_path,
                    "comparison_path": None,
                    "receipt_path": None,
                    "status": retrieval_status,
                }
            ],
        }
        observations.append(item)
    return _write_candidate_observation(
        root, candidate, observations, "build-observation.json"
    )


def _validated_build_observation(candidate, root, observation):
    expected_fields = {
        "schema_version",
        "candidate_id",
        "candidate_content_sha256",
        "observations",
        "observation_sha256",
    }
    if (
        not isinstance(observation, dict)
        or set(observation) != expected_fields
        or observation.get("schema_version") != 1
        or observation.get("candidate_id") != candidate["candidate_id"]
        or observation.get("candidate_content_sha256")
        != candidate_content_sha256(candidate)
    ):
        raise RuntimeContractError(
            "candidate build observation binding is invalid"
        )
    material = dict(observation)
    observation_sha = material.pop("observation_sha256")
    if observation_sha != sha256(
        b"history-runtime-observation-v1\0"
        + canonical_bytes(material)
    ):
        raise RuntimeContractError(
            "candidate build observation ID is invalid"
        )
    intents = required_intents(candidate)
    items = observation.get("observations")
    if (
        not isinstance(items, list)
        or [item.get("intent") for item in items] != intents
    ):
        raise RuntimeContractError(
            "candidate build observation coverage is invalid"
        )
    validated = []
    item_fields = {
        "intent",
        "retrieval_status",
        "status",
        "pack_path",
        "comparison_path",
        "receipt_path",
        "attempts",
    }
    attempt_fields = {
        "pack_path",
        "comparison_path",
        "receipt_path",
        "status",
    }
    for item, intent in zip(items, intents):
        pack_path = root / intent / "retrieval-pack.json"
        expected_path = _resolved_path_text(pack_path)
        expected_attempt = {
            "pack_path": expected_path,
            "comparison_path": None,
            "receipt_path": None,
            "status": item.get("retrieval_status"),
        }
        if (
            not isinstance(item, dict)
            or set(item) != item_fields
            or item.get("pack_path") != expected_path
            or item.get("status") != item.get("retrieval_status")
            or item.get("comparison_path") is not None
            or item.get("receipt_path") is not None
            or item.get("attempts") != [expected_attempt]
            or set(expected_attempt) != attempt_fields
        ):
            raise RuntimeContractError(
                "candidate build observation item is invalid"
            )
        raw = _read_bound_regular(
            pack_path, f"{intent} retrieval pack"
        )
        try:
            pack = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeContractError(
                f"{intent} retrieval pack is invalid"
            ) from exc
        if (
            raw != canonical_bytes(pack)
            or pack.get("intent") != intent
            or pack.get("retrieval_status")
            != item["retrieval_status"]
            or not _pack_matches_candidate(pack, candidate)
        ):
            raise RuntimeContractError(
                f"{intent} retrieval pack binding is invalid"
            )
        validated.append((item, pack))
    return validated


def _comparison_attempt_path(
    intent_root, stem, round_number
):
    if type(round_number) is not int or round_number < 0:
        raise RuntimeContractError(
            "comparison attempt round is invalid"
        )
    suffix = (
        ""
        if round_number == 0
        else f"-expansion-{round_number}"
    )
    return pathlib.Path(intent_root) / f"{stem}{suffix}.json"


def compare_selected_candidate(
    *,
    conn,
    candidate,
    policy,
    artifact_root,
    observation,
    comparator_runner,
    pack_builder=history_retrieval.build_pack,
    comparator_role_bytes,
    comparator_role_identity,
):
    root = pathlib.Path(
        os.path.abspath(os.fspath(artifact_root))
    )
    built = _validated_build_observation(
        candidate, root, observation
    )
    observations = []
    maximum_expansions = int(
        policy["max_expansion_rounds"]
    )
    for source_item, initial_pack in built:
        intent = source_item["intent"]
        intent_root = root / intent
        item = copy.deepcopy(source_item)
        if initial_pack.get("retrieval_status") != "complete":
            observations.append(item)
            continue
        item["attempts"] = []
        pack = initial_pack
        round_number = 0
        while True:
            pack_path = _comparison_attempt_path(
                intent_root, "retrieval-pack", round_number
            )
            comparison_path = _comparison_attempt_path(
                intent_root,
                "history-comparison",
                round_number,
            )
            receipt_path = _comparison_attempt_path(
                intent_root, "history-receipt", round_number
            )
            response = comparator_runner(
                intent, pack, intent_root
            )
            _publish_immutable(
                comparison_path, canonical_bytes(response)
            )
            receipt = history_retrieval.finalize_comparison(
                conn, pack, response, policy
            )
            _publish_immutable(
                receipt_path, canonical_bytes(receipt)
            )
            resolved_pack = _resolved_path_text(pack_path)
            resolved_comparison = _resolved_path_text(
                comparison_path
            )
            resolved_receipt = _resolved_path_text(
                receipt_path
            )
            attempt = {
                "pack_path": resolved_pack,
                "comparison_path": resolved_comparison,
                "receipt_path": resolved_receipt,
                "status": receipt["status"],
            }
            item["attempts"].append(attempt)
            item.update(
                {
                    "retrieval_status":
                        pack["retrieval_status"],
                    "status": receipt["status"],
                    "pack_path": resolved_pack,
                    "comparison_path": resolved_comparison,
                    "receipt_path": resolved_receipt,
                }
            )
            request = receipt.get("expansion_request")
            if (
                receipt["status"] != "uncertain"
                or request is None
                or round_number >= maximum_expansions
            ):
                break
            next_round = round_number + 1
            expanded_request = dict(request)
            expanded_request.update(
                {
                    "round": next_round,
                    "prior_pack_publication_id":
                        pack["pack_publication_id"],
                    "comparison_receipt_id":
                        receipt["receipt_id"],
                }
            )
            expanded_pack = pack_builder(
                conn,
                _retrieval_query(candidate, intent),
                intent,
                policy,
                expansion_request=expanded_request,
                comparator_role_bytes=
                    comparator_role_bytes,
                comparator_role_identity=
                    comparator_role_identity,
            )
            expanded_pack_path = _comparison_attempt_path(
                intent_root,
                "retrieval-pack",
                next_round,
            )
            expanded_trace_path = _comparison_attempt_path(
                intent_root,
                "retrieval-trace",
                next_round,
            )
            _publish_immutable(
                expanded_pack_path,
                canonical_bytes(expanded_pack),
            )
            _write_pack_trace(
                conn, expanded_pack, expanded_trace_path
            )
            retrieval_status = expanded_pack.get(
                "retrieval_status"
            )
            if retrieval_status != "complete":
                resolved_expanded = _resolved_path_text(
                    expanded_pack_path
                )
                # Expansion pack is still published on disk for audit.
                # budget_exceeded cannot fit more evidence: keep the prior
                # uncertain comparator observation and do not append a
                # divergent final attempt (resume binds item to attempts[-1]).
                if retrieval_status == "budget_exceeded":
                    break
                failed_attempt = {
                    "pack_path": resolved_expanded,
                    "comparison_path": None,
                    "receipt_path": None,
                    "status": retrieval_status,
                }
                item["attempts"].append(failed_attempt)
                item.update(
                    {
                        "retrieval_status":
                            retrieval_status,
                        "status": retrieval_status,
                        "pack_path": resolved_expanded,
                        "comparison_path": None,
                        "receipt_path": None,
                    }
                )
                break
            pack = expanded_pack
            round_number = next_round
        observations.append(item)
    return _write_candidate_observation(
        root,
        candidate,
        observations,
        "comparison-observation.json",
    )


def _receipt_summary_item(receipt, policy):
    provenance_fields = (
        "pack_publication_id",
        "pack_sha256",
        "retrieval_policy_version",
        "policy_sha256",
        "source_watermark",
        "index_generation",
        "generation_manifest_sha256",
        "rank_trace_sha256",
        "comparator_invocation_sha256",
        "comparator_preflight_sha256",
        "comparator_version",
        "comparison_sha256",
    )
    result = {
        "intent": receipt["intent"],
        "receipt_id": receipt["receipt_id"],
        "status": receipt["status"],
        "relations": receipt["relations"],
        "provenance": {
            field: receipt[field] for field in provenance_fields
        },
    }
    result["provenance"]["adapter_version"] = policy[
        "adapter_version"
    ]
    return result


def _pack_matches_candidate(pack, candidate):
    query = pack.get("query")
    if not isinstance(query, dict):
        return False
    if query.get("candidate_id") != candidate.get("candidate_id"):
        return False
    if (
        query.get("candidate_content_sha256")
        != candidate_content_sha256(candidate)
        or query.get("candidate_markdown")
        != candidate.get("candidate_markdown")
    ):
        return False
    for field in (
        "story",
        "theme",
        "verdict",
        "reason",
        "category",
        "declared_parent_candidate_id",
    ):
        if field in query and query[field] != candidate.get(field, ""):
            return False
    return True


def _current_generation_binding(conn, policy):
    row = conn.execute(
        """
        SELECT p.*
        FROM history_generation_provenance p
        JOIN schema_meta m
          ON m.key = 'history_index_generation'
         AND CAST(m.value AS INTEGER) = p.generation
        """
    ).fetchone()
    current_watermark = conn.execute(
        "SELECT COALESCE(MAX(source_sequence), 0) FROM candidates"
    ).fetchone()[0]
    pending = conn.execute(
        "SELECT count(*) FROM search_projection_outbox "
        "WHERE state != 'done'"
    ).fetchone()[0]
    try:
        manifest = None if row is None else json.loads(
            row["manifest_json"]
        )
    except (TypeError, ValueError):
        manifest = None
    expected_policy_sha = history_projection._policy_sha256(policy)
    if (
        row is None
        or manifest is None
        or pending
        or row["policy_sha256"] != expected_policy_sha
        or row["source_watermark"] != current_watermark
        or manifest.get("source_watermark") != current_watermark
        or row["manifest_sha256"]
        != sha256(history_retrieval.canonical_bytes(manifest))
    ):
        raise RuntimeContractError(
            "history summary requires a current generation"
        )
    return {
        "policy_sha256": expected_policy_sha,
        "source_watermark": current_watermark,
        "index_generation": row["generation"],
        "generation_manifest_sha256": row["manifest_sha256"],
    }


def history_summary_sha256(summary):
    material = dict(summary)
    material.pop("aggregate_sha256", None)
    return sha256(
        b"history-summary-v1\0" + canonical_bytes(material)
    )


def build_history_summary(
    conn,
    candidate,
    receipt_bindings,
    policy,
):
    if policy.get("mode") != "enforcement":
        raise RuntimeContractError(
            "history summaries require enforcement mode"
        )
    expected = required_intents(candidate)
    if not 2 <= len(receipt_bindings) <= 3:
        raise RuntimeContractError(
            "history summary requires two or three receipts"
        )
    items = []
    current = _current_generation_binding(conn, policy)
    for pack, receipt in receipt_bindings:
        verified = history_retrieval.replay_receipt(
            conn, pack, receipt, policy
        )
        if (
            verified["verified"] is not True
            or receipt["status"]
            not in history_retrieval.PERMANENT_STATUSES
            or not _pack_matches_candidate(pack, candidate)
            or receipt["policy_sha256"]
            != current["policy_sha256"]
            or receipt["source_watermark"]
            != current["source_watermark"]
            or receipt["index_generation"]
            != current["index_generation"]
            or receipt["generation_manifest_sha256"]
            != current["generation_manifest_sha256"]
        ):
            raise RuntimeContractError(
                "history receipt is not current and permanent"
            )
        items.append(_receipt_summary_item(receipt, policy))
    if [item["intent"] for item in items] != expected:
        raise RuntimeContractError(
            "history receipts are not complete and ordered"
        )
    content_sha = candidate_content_sha256(candidate)
    if candidate.get("content_sha256", content_sha) != content_sha:
        raise RuntimeContractError(
            "candidate content hash is invalid"
        )
    summary = {
        "schema_version": 1,
        "candidate_id": candidate["candidate_id"],
        "candidate_content_sha256": content_sha,
        "adapter_version": policy["adapter_version"],
        "receipts": items,
    }
    summary["aggregate_sha256"] = history_summary_sha256(summary)
    return summary


def verify_history_summary(conn, candidate, summary, policy):
    if (
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
        or summary["schema_version"] != 1
        or summary["candidate_id"] != candidate["candidate_id"]
        or summary["candidate_content_sha256"]
        != candidate_content_sha256(candidate)
        or summary["adapter_version"] != policy["adapter_version"]
        or summary["aggregate_sha256"]
        != history_summary_sha256(summary)
    ):
        raise RuntimeContractError("history summary binding is invalid")
    expected = required_intents(candidate)
    if (
        not isinstance(summary["receipts"], list)
        or len(summary["receipts"]) != len(expected)
        or [item.get("intent") for item in summary["receipts"]]
        != expected
    ):
        raise RuntimeContractError(
            "history summary receipt order is invalid"
        )
    current = _current_generation_binding(conn, policy)
    for item in summary["receipts"]:
        row = conn.execute(
            "SELECT receipt_json FROM history_receipts "
            "WHERE receipt_id = ?",
            (item.get("receipt_id"),),
        ).fetchone()
        if row is None:
            raise RuntimeContractError(
                "history summary receipt is not durable"
            )
        receipt = json.loads(row["receipt_json"])
        publication = conn.execute(
            """
            SELECT pack_bytes
            FROM history_pack_publications
            WHERE publication_id = ?
            """,
            (receipt.get("pack_publication_id"),),
        ).fetchone()
        if publication is None:
            raise RuntimeContractError(
                "history summary pack is not durable"
            )
        try:
            pack_raw = bytes(publication["pack_bytes"])
            pack = json.loads(pack_raw.decode("utf-8"))
        except (TypeError, UnicodeDecodeError, ValueError) as exc:
            raise RuntimeContractError(
                "history summary pack is corrupt"
            ) from exc
        if pack_raw != history_retrieval.canonical_bytes(pack):
            raise RuntimeContractError(
                "history summary pack is not canonical"
            )
        verified = history_retrieval.replay_receipt(
            conn, pack, receipt, policy
        )
        if (
            verified["verified"] is not True
            or receipt.get("status")
            not in history_retrieval.PERMANENT_STATUSES
            or not _pack_matches_candidate(pack, candidate)
            or receipt.get("policy_sha256")
            != current["policy_sha256"]
            or receipt.get("source_watermark")
            != current["source_watermark"]
            or receipt.get("index_generation")
            != current["index_generation"]
            or receipt.get("generation_manifest_sha256")
            != current["generation_manifest_sha256"]
            or _receipt_summary_item(receipt, policy) != item
        ):
            raise RuntimeContractError(
                "history summary receipt binding drifted"
            )
    return True


def resume_binding(**values):
    if set(values) != set(RESUME_BINDING_FIELDS):
        raise RuntimeContractError("resume binding schema is not closed")
    if values["mode"] not in {"shadow", "enforcement"}:
        raise RuntimeContractError("resume mode is invalid")
    for field in (
        "policy_sha256",
        "pack_sha256",
        "candidate_content_sha256",
        "preflight_sha256",
    ):
        if not _valid_sha256(values[field]):
            raise RuntimeContractError(
                f"resume {field} is invalid"
            )
    for field in ("source_watermark", "index_generation"):
        if type(values[field]) is not int or values[field] < 0:
            raise RuntimeContractError(
                f"resume {field} is invalid"
            )
    for field in (
        "policy_version",
        "comparator_version",
        "adapter_version",
    ):
        if not isinstance(values[field], str) or not values[field]:
            raise RuntimeContractError(
                f"resume {field} is invalid"
            )
    return dict(values)


def resume_matches(expected, observed):
    return (
        isinstance(expected, dict)
        and isinstance(observed, dict)
        and set(expected) == set(RESUME_BINDING_FIELDS)
        and set(observed) == set(RESUME_BINDING_FIELDS)
        and all(
            expected[field] == observed[field]
            for field in RESUME_BINDING_FIELDS
        )
    )


def _stage_modules():
    try:
        from lib import history_stage
    except ImportError:
        import history_stage
    return history_stage


def _portable_stage_module():
    try:
        from lib import portable_stage
    except ImportError:
        import portable_stage
    return portable_stage


def _portable_stage_contract_error(exc):
    return RuntimeContractError(str(exc), error_class=exc.error_class)


def _public_portable_stage(prepared, reference_root):
    portable_module = _portable_stage_module()
    try:
        return portable_module.public_descriptor(
            prepared, reference_root
        )
    except portable_module.PortableStageError as exc:
        raise _portable_stage_contract_error(exc) from exc


def _verified_public_portable_stage(descriptor, reference_root):
    portable_module = _portable_stage_module()
    try:
        return portable_module.verify_public_descriptor(
            descriptor, reference_root
        )
    except portable_module.PortableStageError as exc:
        raise _portable_stage_contract_error(exc) from exc


def _provider_adapters_module():
    try:
        from lib import provider_adapters
    except ImportError:
        import provider_adapters
    return provider_adapters


def _validated_portable_request_profile(profile, *, surface="hunt"):
    provider_module = _provider_adapters_module()
    if (
        not provider_module.command_intent_is_issued(profile)
        or profile.surface != surface
        or profile.provider_validation != "unverified"
        or profile.authority != "shadow-only"
        or profile.hard_complete_eligible is not False
        or not _valid_sha256(profile.execution_request_profile_hash)
    ):
        raise RuntimeContractError(
            "portable provider request profile is invalid"
        )
    return profile


def _load_portable_request_profile(path, *, surface="hunt"):
    provider_module = _provider_adapters_module()
    try:
        registry = provider_module.load_registry(
            PROVIDER_REGISTRY_PATH
        )
        profile = provider_module.load_command_intent(
            path, registry
        )
    except provider_module.ProviderResolutionError as exc:
        raise RuntimeContractError(str(exc)) from exc
    return _validated_portable_request_profile(
        profile, surface=surface
    )


def _portable_profile_descriptor(profile, seat_id):
    _validated_portable_request_profile(profile)
    return {
        "seat_id": seat_id,
        "execution_request_profile_hash":
            profile.execution_request_profile_hash,
    }


def _portable_serialized_prompt(stage, input_paths, policy):
    role_raw = _read_regular(
        ROOT / _STAGE_ROLES[stage],
        1024 * 1024,
        f"{stage} role",
    )
    mounted = {
        name: _read_regular(
            path, _INPUT_CAPS[name], f"portable stage input {name}"
        )
        for name, path in sorted(input_paths.items())
    }
    invocation = _stage_invocation(
        stage, mounted, role_raw, policy
    )
    serialized = history_budget.serialize_stage_invocation(
        stage=stage,
        adapter_version=policy["adapter_version"],
        fixed_instructions=role_raw.decode("utf-8"),
        mounted_inputs=mounted,
        candidate=invocation["candidate"],
        retrieval_payload=invocation["retrieval_payload"],
        receipts=invocation["receipts"],
        tool_schemas=invocation["tool_schemas"],
        messages=invocation["messages"],
        output_schema_instructions=invocation[
            "output_schema_instructions"
        ],
    )
    try:
        return serialized.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeContractError(
            "portable stage prompt is not UTF-8"
        ) from exc


def _run_portable_stage(
    *,
    request_profile,
    stage,
    seat_id,
    input_paths,
    invocation_root,
    policy,
):
    profile = _validated_portable_request_profile(request_profile)
    portable_module = _portable_stage_module()
    serialized_prompt = _portable_serialized_prompt(
        stage, input_paths, policy
    )
    try:
        prepared = portable_module.prepare_stage(
            profile,
            stage=stage,
            seat_id=seat_id,
            serialized_prompt=serialized_prompt,
            input_paths=input_paths,
            output_root=pathlib.Path(invocation_root) / "output",
            state_root=pathlib.Path(invocation_root) / "state",
        )
        portable_module.run_stage(prepared)
        portable_module.verify_completion(prepared)
    except portable_module.PortableStageError as exc:
        raise _portable_stage_contract_error(exc) from exc
    return prepared


def _read_regular(path, maximum, label):
    return _read_bound_regular(
        path, label, maximum=maximum
    )


def _stage_invocation(stage, mounted, role_raw, policy):
    candidate = None
    retrieval_payload = None
    receipts = []
    tool_schemas = []
    if stage == "history-compare":
        retrieval_payload = json.loads(
            mounted["retrieval_pack.json"].decode("utf-8")
        )
        candidate = retrieval_payload["query"]
        receipts = [
            {
                "pack_publication_id": retrieval_payload[
                    "pack_publication_id"
                ],
                "role_identity": _STAGE_ROLES[stage],
                "role_sha256": sha256(role_raw),
            }
        ]
        tool_schemas = [
            history_retrieval.comparator_output_schema(
                retrieval_payload, policy
            )
        ]
    elif stage == "review":
        candidate = json.loads(
            mounted["candidate.json"].decode("utf-8")
        )
    invocation = {
        "candidate": candidate,
        "retrieval_payload": retrieval_payload,
        "receipts": receipts,
        "tool_schemas": tool_schemas,
        "messages": [
            {
                "role": "user",
                "content": _STAGE_MESSAGES[stage],
            }
        ],
        "output_schema_instructions": None,
    }
    serialized = history_budget.serialize_stage_invocation(
        stage=stage,
        adapter_version=policy["adapter_version"],
        fixed_instructions=role_raw.decode("utf-8"),
        mounted_inputs=mounted,
        **invocation,
    )
    invocation["expected_serialized_sha256"] = sha256(serialized)
    return invocation


def _registered_test_backends():
    return {
        (ROOT / "tests" / "fake_stage_agent.py").resolve(),
        (ROOT / "tests" / "malicious_history_agent.py").resolve(),
    }


def _build_stage_manifest(
    *,
    stage,
    seat_id,
    db_path,
    policy_path,
    input_paths,
    output_root,
    manifest_path,
    command_json,
    authority=None,
    test_comparator_status=None,
    test_review_verdict=None,
):
    """Build one closed host-owned stage manifest and command binding."""
    if stage not in _STAGE_ROLES:
        raise RuntimeContractError("contained stage is unsupported")
    if (
        not isinstance(seat_id, str)
        or not seat_id
        or any(character in seat_id for character in "\r\n\x00")
    ):
        raise RuntimeContractError("stage seat ID is invalid")
    if not isinstance(input_paths, dict):
        raise RuntimeContractError(
            "contained stage inputs do not match its profile"
        )
    _require_context_test_paths(
        (
            db_path,
            policy_path,
            output_root,
            manifest_path,
            *input_paths.values(),
        )
    )
    stage_module = _stage_modules()
    try:
        command = stage_module.parse_command_json(command_json)
    except stage_module.StageError as exc:
        raise RuntimeContractError(str(exc)) from exc
    if not pathlib.Path(command[0]).is_absolute():
        raise RuntimeContractError(
            "contained command executable must be absolute"
        )
    try:
        resolved_command = pathlib.Path(command[0]).resolve(strict=True)
    except OSError as exc:
        raise RuntimeContractError(
            "contained command executable is unavailable"
        ) from exc
    test_backends = _registered_test_backends()
    policy = history_projection.load_policy(policy_path)
    test_runtime = _active_test_runtime(policy)
    if (
        resolved_command in test_backends
        and test_runtime is None
    ):
        raise RuntimeContractError(
            "registered fixture backend is unavailable in production"
        )
    if (
        test_runtime is None
        and (
            test_comparator_status is not None
            or test_review_verdict is not None
        )
    ):
        raise RuntimeContractError(
            "stage test controls are unavailable in production"
        )
    comparator_status = (
        "complete_no_match"
        if test_comparator_status is None
        else test_comparator_status
    )
    review_verdict = (
        "accept-w-rev"
        if test_review_verdict is None
        else test_review_verdict
    )
    if (
        comparator_status not in {
            "complete_match",
            "complete_no_match",
            "uncertain",
            "conflicting_evidence",
        }
        or (
            test_comparator_status is not None
            and resolved_command not in test_backends
        )
    ):
        raise RuntimeContractError(
            "test comparator status is unavailable"
        )
    if (
        review_verdict
        not in {"strong-accept", "accept-w-rev", "reject"}
        or (
            test_review_verdict is not None
            and resolved_command not in test_backends
        )
    ):
        raise RuntimeContractError(
            "test review verdict is unavailable"
        )
    required, optional = _STAGE_INPUTS[stage]
    if (
        not isinstance(input_paths, dict)
        or not required.issubset(input_paths)
        or set(input_paths) - required - optional
    ):
        raise RuntimeContractError(
            "contained stage inputs do not match its profile"
        )
    registered_policy = history_projection.load_policy(
        ROOT / "history" / "retrieval-policy-v1.json"
    )
    authority_value = None
    if policy["mode"] == "enforcement":
        authority_value = _validated_runtime_authority(
            policy,
            authority,
            state_paths=(manifest_path, output_root),
        )
    synthetic_policy = dict(registered_policy)
    synthetic_policy["mode"] = "enforcement"
    if policy == registered_policy:
        policy_raw = _read_regular(
            ROOT / "history" / "retrieval-policy-v1.json",
            1024 * 1024,
            "stage policy",
        )
        policy_descriptor = {
            "source": "history/retrieval-policy-v1.json",
            "sha256": sha256(policy_raw),
        }
    elif (
        policy == synthetic_policy
        and resolved_command in test_backends
        and test_runtime is not None
        and authority_value is not None
        and authority_value["scope"] == SYNTHETIC_SCOPE
    ):
        policy_raw = _read_regular(
            policy_path, 1024 * 1024, "synthetic stage policy"
        )
        policy_descriptor = {
            "source": SYNTHETIC_SCOPE,
            "host_path": str(pathlib.Path(policy_path).resolve()),
            "sha256": sha256(policy_raw),
            "authority_scope": SYNTHETIC_SCOPE,
        }
    else:
        raise RuntimeContractError(
            "stage policy is not the registered repository policy"
        )
    if (
        stage == "review"
        and "history_summary.json" in input_paths
        and policy["mode"] != "enforcement"
    ):
        raise RuntimeContractError(
            "shadow review cannot mount history evidence"
        )
    manifest_destination = pathlib.Path(
        os.path.abspath(os.fspath(manifest_path))
    )
    host_root = manifest_destination.parent
    input_root = host_root / (
        manifest_destination.name + "-inputs"
    )
    output = pathlib.Path(output_root)
    host_descriptor = _open_safe_directory(
        host_root, create=True
    )
    os.close(host_descriptor)
    output_descriptor = _open_safe_directory(
        output, create=True
    )
    try:
        if os.listdir(output_descriptor):
            raise RuntimeContractError(
                "stage output root must be empty"
            )
    finally:
        os.close(output_descriptor)
    role_relative = _STAGE_ROLES[stage]
    role_raw = _read_regular(
        ROOT / role_relative, 1024 * 1024, "stage role"
    )
    mounted = {}
    input_publications = {}
    descriptors = []
    for name, source in sorted(input_paths.items()):
        raw = _read_regular(
            source, _INPUT_CAPS[name], f"stage input {name}"
        )
        mounted[name] = raw
        input_publications[name] = raw
        descriptors.append(
            {
                "source": name,
                "mirror_path": name,
                "sha256": sha256(raw),
                "max_bytes": _INPUT_CAPS[name],
            }
        )
    invocation = _stage_invocation(
        stage, mounted, role_raw, policy
    )
    adapter = _read_regular(
        ROOT / "lib" / "history_stage_adapter.py",
        1024 * 1024,
        "stage adapter",
    )
    canonicalizer = _read_regular(
        ROOT / "lib" / "history_stage_proxy.py",
        1024 * 1024,
        "stage canonicalizer",
    )
    outputs = []
    output_paths = {}
    for mirror_path, destination, kind, maximum in _STAGE_OUTPUTS[
        stage
    ]:
        target = output / destination
        if target.exists() or target.is_symlink():
            raise RuntimeContractError(
                "stage output destination already exists"
            )
        outputs.append(
            {
                "mirror_path": mirror_path,
                "destination": destination,
                "artifact_kind": kind,
                "max_bytes": maximum,
                "required": True,
            }
        )
        output_paths[destination] = str(target)
    if resolved_command in test_backends:
        first_input = sorted(mounted)[0]
        registered_environment = {
            "HISTORY_STAGE_ATTACK_MODE": "none",
            "HISTORY_STAGE_COMPARATOR_STATUS":
                comparator_status,
            "HISTORY_STAGE_REVIEW_VERDICT":
                review_verdict,
            "HISTORY_STAGE_INPUT_PATH": "input/" + first_input,
            "HISTORY_STAGE_OUTSIDE_WRITE": str(
                (host_root / "outside-write").resolve()
            ),
            "HISTORY_STAGE_SEAT_ID": seat_id,
            "HISTORY_STAGE_SENTINELS_JSON": json.dumps(
                [
                    str((ROOT / "ledger.tsv").resolve()),
                    str(pathlib.Path(db_path).resolve()),
                    str((ROOT / ".git").resolve()),
                ],
                separators=(",", ":"),
            ),
            "HISTORY_STAGE_SIBLING": str(
                (host_root / "sibling-output").resolve()
            ),
        }
    else:
        registered_environment = {}
    needs_history = (
        stage in {"generate", "history-compare"}
        or (
            stage == "review"
            and "history_summary.json" in mounted
        )
    )
    database = (
        _verified_history_database(db_path)
        if needs_history
        else pathlib.Path(db_path)
    )
    if needs_history and database.name != "history.sqlite3":
        raise RuntimeContractError(
            "contained stage history authority is invalid"
        )
    manifest = {
        "schema_version": 1,
        "stage": stage,
        "seat_id": seat_id,
        "adapter": {
            "version": policy["adapter_version"],
            "fixed_wrapper": "history-stage-prompt-v1",
            "wrapper_allowance": policy[
                "adapter_wrapper_allowance"
            ],
            "executable_source": "lib/history_stage_adapter.py",
            "executable_sha256": sha256(adapter),
            "canonicalizer_source": "lib/history_stage_proxy.py",
            "canonicalizer_sha256": sha256(canonicalizer),
        },
        "policy": policy_descriptor,
        "role": {
            "source": role_relative,
            "sha256": sha256(role_raw),
        },
        "input_roots": [str(input_root.resolve())],
        "inputs": descriptors,
        "invocation": invocation,
        "output_roots": [str(output.resolve())],
        "outputs": outputs,
        "preflight_receipt_destination": "preflight.json",
        "completion_receipt_destination": "completion.json",
        "registered_runtime_reads": [],
        "registered_environment": registered_environment,
        "history_store": (
            {
                "root": str(database.parent.resolve()),
                "source": "history.sqlite3",
            }
            if needs_history
            else None
        ),
    }
    manifest_raw = canonical_bytes(manifest)
    _publish_immutable_tree(input_root, input_publications)
    try:
        _publish_immutable(manifest_destination, manifest_raw)
    except Exception:
        _remove_immutable_tree(input_root)
        raise
    prepared = {
        "schema_version": 1,
        "stage": stage,
        "seat_id": seat_id,
        "manifest_path": str(manifest_destination),
        "manifest_sha256": sha256(manifest_raw),
        "command_argv": command,
        "command_prefix_sha256": sha256(canonical_bytes(command)),
        "output_root": str(output.resolve()),
        "output_paths": output_paths,
        "preflight_path": str(
            (output / "preflight.json").resolve()
        ),
        "completion_path": str(
            (output / "completion.json").resolve()
        ),
    }
    return prepared


def build_stage_manifest(
    *,
    stage,
    seat_id,
    db_path,
    policy_path,
    input_paths,
    output_root,
    manifest_path,
    command_json,
    authority=None,
):
    """Build one production stage manifest without test controls."""
    return _build_stage_manifest(
        stage=stage,
        seat_id=seat_id,
        db_path=db_path,
        policy_path=policy_path,
        input_paths=input_paths,
        output_root=output_root,
        manifest_path=manifest_path,
        command_json=command_json,
        authority=authority,
    )


def _build_stage_manifest_for_test(
    *,
    test_authority,
    test_state_root,
    test_comparator_status=None,
    test_review_verdict=None,
    **values,
):
    paths = (
        values["db_path"],
        values["policy_path"],
        values["output_root"],
        values["manifest_path"],
        *values["input_paths"].values(),
    )
    _require_test_state_paths(test_state_root, paths)
    policy = history_projection.load_policy(values["policy_path"])
    call_values = dict(values)
    supplied_authority = call_values.get("authority")
    if (
        supplied_authority is not None
        and supplied_authority is not test_authority
    ):
        raise RuntimeContractError(
            "test stage authority changed"
        )
    if policy["mode"] == "enforcement":
        call_values["authority"] = test_authority
    with _runtime_for_test(
        policy,
        test_authority,
        test_state_root,
        state_paths=paths,
    ):
        return _build_stage_manifest(
            test_comparator_status=test_comparator_status,
            test_review_verdict=test_review_verdict,
            **call_values,
        )


def _validate_prepared_stage(prepared):
    fields = {
        "schema_version",
        "stage",
        "seat_id",
        "manifest_path",
        "manifest_sha256",
        "command_argv",
        "command_prefix_sha256",
        "output_root",
        "output_paths",
        "preflight_path",
        "completion_path",
    }
    if (
        not isinstance(prepared, dict)
        or set(prepared) != fields
        or prepared.get("schema_version") != 1
        or prepared.get("stage") not in _STAGE_ROLES
        or not _valid_sha256(prepared.get("manifest_sha256"))
        or not isinstance(prepared.get("command_argv"), list)
        or prepared.get("command_prefix_sha256")
        != sha256(canonical_bytes(prepared.get("command_argv")))
    ):
        raise RuntimeContractError(
            "prepared stage binding is invalid"
        )
    manifest_raw = _read_bound_regular(
        prepared["manifest_path"],
        "prepared stage manifest",
        maximum=1024 * 1024,
    )
    if sha256(manifest_raw) != prepared["manifest_sha256"]:
        raise RuntimeContractError("stage manifest changed")
    return manifest_raw


def _read_bound_regular(
    path, label, *, maximum=None, allow_empty=False
):
    target = pathlib.Path(path)
    directory = None
    descriptor = None
    try:
        directory = _open_safe_directory(
            target.parent, create=False
        )
        name = _artifact_name(target)
        before = os.stat(
            name,
            dir_fd=directory,
            follow_symlinks=False,
        )
    except (OSError, RuntimeContractError) as exc:
        if directory is not None:
            os.close(directory)
        raise RuntimeContractError(f"{label} is unavailable") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or (before.st_size < 1 and not allow_empty)
        or (
            maximum is not None
            and before.st_size > maximum
        )
    ):
        os.close(directory)
        raise RuntimeContractError(
            f"{label} is not a single-link regular file"
        )
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=directory)
    except OSError as exc:
        os.close(directory)
        raise RuntimeContractError(
            f"{label} cannot be opened safely"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        chunks = []
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
        os.close(directory)
    identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    if identity != (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
    ) or identity != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise RuntimeContractError(f"{label} changed during capture")
    raw = b"".join(chunks)
    if len(raw) != before.st_size:
        raise RuntimeContractError(f"{label} changed during capture")
    return raw


def verify_stage_completion(prepared):
    if (
        isinstance(prepared, dict)
        and prepared.get("execution_boundary")
        == PORTABLE_EXECUTION_BOUNDARY
    ):
        portable_module = _portable_stage_module()
        try:
            return portable_module.verify_completion(prepared)
        except portable_module.PortableStageError as exc:
            raise _portable_stage_contract_error(exc) from exc
    manifest_raw = _validate_prepared_stage(prepared)
    try:
        preflight_raw = _read_bound_regular(
            prepared["preflight_path"], "stage preflight"
        )
        completion_raw = _read_bound_regular(
            prepared["completion_path"], "stage completion"
        )
        preflight = json.loads(preflight_raw.decode("utf-8"))
        completion = json.loads(completion_raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise RuntimeContractError(
            "stage receipts are unavailable or corrupt"
        ) from exc
    manifest = json.loads(manifest_raw.decode("utf-8"))
    preflight_fields = {
        "schema_version",
        "stage",
        "seat_id",
        "manifest_sha256",
        "policy_sha256",
        "role_sha256",
        "adapter_version",
        "adapter_executable_sha256",
        "adapter_canonicalizer_sha256",
        "adapter_interpreter_sha256",
        "command_argv_sha256",
        "executable_sha256",
        "interpreter_sha256",
        "runtime_dependency_sha256s",
        "runtime_executable_sha256s",
        "backend_bootstrap_sha256s",
        "codex_capability_id",
        "codex_capability_profile_sha256",
        "codex_cli_version",
        "codex_auth_source",
        "canonical_request_sha256",
        "canonical_request_bytes",
        "response_schema_sha256",
        "history_pack_publication_id",
        "history_pack_sha256",
        "input_sha256s",
        "output_contract_sha256",
        "containment",
        "containment_executable_sha256",
        "serialized_byte_count",
        "serialized_sha256",
        "count_method",
        "input_upper_bound",
        "max_output_tokens",
        "safety_margin",
        "model_context_limit",
        "total_upper_bound",
        "mirror_path",
        "home_path",
        "tmp_path",
    }
    output_contract = [
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
            manifest["outputs"],
            key=lambda item: item["artifact_kind"],
        )
    ]
    expected_inputs = {
        item["mirror_path"]: item["sha256"]
        for item in sorted(
            manifest["inputs"],
            key=lambda item: item["mirror_path"],
        )
    }
    executable_raw = _read_bound_regular(
        pathlib.Path(prepared["command_argv"][0]).resolve(),
        "stage executable",
    )
    if (
        preflight_raw != canonical_bytes(preflight)
        or completion_raw != canonical_bytes(completion)
        or set(preflight) != preflight_fields
        or preflight.get("schema_version") != 1
        or preflight.get("stage") != prepared["stage"]
        or preflight.get("seat_id") != prepared["seat_id"]
        or preflight.get("manifest_sha256")
        != prepared["manifest_sha256"]
        or preflight.get("policy_sha256")
        != manifest["policy"]["sha256"]
        or preflight.get("role_sha256")
        != manifest["role"]["sha256"]
        or preflight.get("adapter_version")
        != manifest["adapter"]["version"]
        or preflight.get("adapter_executable_sha256")
        != manifest["adapter"]["executable_sha256"]
        or preflight.get("adapter_canonicalizer_sha256")
        != manifest["adapter"]["canonicalizer_sha256"]
        or preflight.get("executable_sha256")
        != sha256(executable_raw)
        or preflight.get("input_sha256s") != expected_inputs
        or preflight.get("output_contract_sha256")
        != sha256(canonical_bytes(output_contract))
        or preflight.get("serialized_sha256")
        != manifest["invocation"]["expected_serialized_sha256"]
        or type(preflight.get("serialized_byte_count")) is not int
        or preflight["serialized_byte_count"] < 1
        or preflight.get("total_upper_bound", 1)
        > preflight.get("model_context_limit", 0)
        or completion.get("stage") != prepared["stage"]
        or completion.get("seat_id") != prepared["seat_id"]
        or completion.get("preflight_sha256")
        != sha256(preflight_raw)
        or completion.get("serialized_sha256")
        != preflight["serialized_sha256"]
        or completion.get("command_argv_sha256")
        != preflight["command_argv_sha256"]
        or completion.get("containment") != preflight["containment"]
        or completion.get("mirror_path")
        != preflight["mirror_path"]
        or completion.get("home_path") != preflight["home_path"]
        or completion.get("tmp_path") != preflight["tmp_path"]
    ):
        raise RuntimeContractError(
            "stage receipt binding is invalid"
        )
    material = dict(completion)
    completion_id = material.pop("completion_id", None)
    if completion_id != sha256(
        b"history-stage-completion-v1\0"
        + canonical_bytes(material)
    ):
        raise RuntimeContractError(
            "stage completion ID is invalid"
        )
    outputs = completion.get("outputs")
    if (
        not isinstance(outputs, dict)
        or set(outputs) != set(prepared["output_paths"])
    ):
        raise RuntimeContractError(
            "stage completion output coverage is invalid"
        )
    for name, destination in prepared["output_paths"].items():
        raw = _read_bound_regular(
            destination, f"stage output {name}"
        )
        descriptor = outputs[name]
        expected_kind = next(
            item["artifact_kind"]
            for item in manifest["outputs"]
            if item["destination"] == name
        )
        if (
            not isinstance(descriptor, dict)
            or set(descriptor)
            != {"sha256", "byte_count", "artifact_kind"}
            or descriptor.get("sha256") != sha256(raw)
            or descriptor.get("byte_count") != len(raw)
            or descriptor.get("artifact_kind") != expected_kind
        ):
            raise RuntimeContractError(
                "stage output differs from completion receipt"
            )
    return True


def _prepared_stage_policy(prepared, manifest_raw):
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeContractError(
            "stage manifest policy is invalid"
        ) from exc
    descriptor = manifest.get("policy")
    if (
        isinstance(descriptor, dict)
        and set(descriptor) == {"source", "sha256"}
        and descriptor.get("source")
        == "history/retrieval-policy-v1.json"
    ):
        policy_path = (
            ROOT / "history" / "retrieval-policy-v1.json"
        )
    elif (
        isinstance(descriptor, dict)
        and set(descriptor)
        == {
            "source",
            "host_path",
            "sha256",
            "authority_scope",
        }
        and descriptor.get("source") == SYNTHETIC_SCOPE
        and descriptor.get("authority_scope") == SYNTHETIC_SCOPE
        and isinstance(descriptor.get("host_path"), str)
        and pathlib.Path(descriptor["host_path"]).is_absolute()
    ):
        try:
            backend = pathlib.Path(
                prepared["command_argv"][0]
            ).resolve(strict=True)
        except (IndexError, OSError) as exc:
            raise RuntimeContractError(
                "synthetic stage backend is unavailable"
            ) from exc
        allowed = {
            (ROOT / "tests" / "fake_stage_agent.py").resolve(),
            (
                ROOT / "tests" / "malicious_history_agent.py"
            ).resolve(),
        }
        if len(prepared["command_argv"]) != 1 or backend not in allowed:
            raise RuntimeContractError(
                "synthetic stage policy requires a local fixture"
            )
        policy_path = pathlib.Path(descriptor["host_path"])
    else:
        raise RuntimeContractError(
            "stage policy descriptor is invalid"
        )
    raw = _read_bound_regular(
        policy_path, "prepared stage policy", maximum=1024 * 1024
    )
    if (
        not _valid_sha256(descriptor.get("sha256"))
        or sha256(raw) != descriptor["sha256"]
    ):
        raise RuntimeContractError(
            "prepared stage policy changed"
        )
    policy = history_projection.load_policy(policy_path)
    if descriptor.get("source") == SYNTHETIC_SCOPE:
        registered = history_projection.load_policy(
            ROOT / "history" / "retrieval-policy-v1.json"
        )
        expected = dict(registered)
        expected["mode"] = "enforcement"
        if policy != expected:
            raise RuntimeContractError(
                "synthetic stage policy is not registered"
            )
    return policy


def run_contained_stage(
    prepared, authority=None, *, backend_entry_fd=None
):
    if _TEST_RUNTIME_CONTEXT.get() is not None:
        if (
            not isinstance(prepared, dict)
            or not isinstance(
                prepared.get("output_paths"), dict
            )
        ):
            raise RuntimeContractError(
                "prepared test stage is invalid"
            )
        _require_context_test_paths(
            (
                prepared.get("manifest_path"),
                prepared.get("output_root"),
                prepared.get("preflight_path"),
                prepared.get("completion_path"),
                *prepared["output_paths"].values(),
            )
        )
    manifest_raw = _validate_prepared_stage(prepared)
    policy = _prepared_stage_policy(prepared, manifest_raw)
    test_runtime = _active_test_runtime(policy)
    if test_runtime is not None:
        manifest = json.loads(manifest_raw.decode("utf-8"))
        manifest_paths = [
            *manifest.get("input_roots", ()),
            *manifest.get("output_roots", ()),
        ]
        history_descriptor = manifest.get("history_store")
        if isinstance(history_descriptor, dict):
            manifest_paths.append(history_descriptor.get("root"))
        policy_descriptor = manifest.get("policy")
        if (
            isinstance(policy_descriptor, dict)
            and "host_path" in policy_descriptor
        ):
            manifest_paths.append(policy_descriptor["host_path"])
        _require_context_test_paths(manifest_paths)
    try:
        command_path = pathlib.Path(
            prepared["command_argv"][0]
        ).resolve(strict=True)
    except (KeyError, IndexError, OSError, TypeError) as exc:
        raise RuntimeContractError(
            "prepared stage command is unavailable"
        ) from exc
    if (
        command_path in _registered_test_backends()
        and test_runtime is None
    ):
        raise RuntimeContractError(
            "registered fixture backend is unavailable in production"
        )
    if policy["mode"] == "enforcement":
        _validated_runtime_authority(
            policy,
            authority,
            state_paths=(
                prepared["output_root"],
                prepared["preflight_path"],
                prepared["completion_path"],
            ),
        )
    stage_module = _stage_modules()
    try:
        completion = stage_module.run_stage(
            prepared["stage"],
            pathlib.Path(prepared["manifest_path"]),
            prepared["command_argv"],
            backend_entry_fd=backend_entry_fd,
        )
    except stage_module.StageError as exc:
        raise RuntimeContractError(str(exc)) from exc
    verify_stage_completion(prepared)
    if completion != _load_canonical_json(
        prepared["completion_path"], "stage completion"
    ):
        raise RuntimeContractError(
            "returned completion differs from durable receipt"
        )
    return completion


def _run_contained_stage_for_test(
    *,
    test_authority,
    test_state_root,
    prepared,
    backend_entry_fd=None,
):
    if not isinstance(prepared, dict):
        raise RuntimeContractError(
            "prepared test stage is invalid"
        )
    paths = [
        prepared.get("manifest_path"),
        prepared.get("output_root"),
        prepared.get("preflight_path"),
        prepared.get("completion_path"),
    ]
    output_paths = prepared.get("output_paths")
    if not isinstance(output_paths, dict):
        raise RuntimeContractError(
            "prepared test stage outputs are invalid"
        )
    paths.extend(output_paths.values())
    _require_test_state_paths(test_state_root, paths)
    manifest_raw = _validate_prepared_stage(prepared)
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeContractError(
            "prepared test stage manifest is invalid"
        ) from exc
    paths.extend(manifest.get("input_roots", ()))
    paths.extend(manifest.get("output_roots", ()))
    history_store_descriptor = manifest.get("history_store")
    if isinstance(history_store_descriptor, dict):
        paths.append(history_store_descriptor.get("root"))
    policy_descriptor = manifest.get("policy")
    if (
        isinstance(policy_descriptor, dict)
        and "host_path" in policy_descriptor
    ):
        paths.append(policy_descriptor["host_path"])
    _require_test_state_paths(test_state_root, paths)
    policy = _prepared_stage_policy(prepared, manifest_raw)
    try:
        command_path = pathlib.Path(
            prepared["command_argv"][0]
        ).resolve(strict=True)
    except (KeyError, IndexError, OSError, TypeError) as exc:
        raise RuntimeContractError(
            "prepared test stage command is unavailable"
        ) from exc
    if command_path not in _registered_test_backends():
        raise RuntimeContractError(
            "test stage backend is not registered"
        )
    run_authority = (
        test_authority
        if policy["mode"] == "enforcement"
        else None
    )
    with _runtime_for_test(
        policy,
        test_authority,
        test_state_root,
        state_paths=paths,
    ):
        return run_contained_stage(
            prepared,
            authority=run_authority,
            backend_entry_fd=backend_entry_fd,
        )


def _load_canonical_json(path, label):
    raw = _read_bound_regular(path, label)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeContractError(f"{label} is invalid") from exc
    if raw != canonical_bytes(value):
        raise RuntimeContractError(f"{label} is not canonical")
    return value


def _load_batch_candidates(batch_path):
    manifest = _load_canonical_json(
        batch_path, "frozen batch manifest"
    )
    verify_frozen_batch(manifest)
    candidates = {}
    for descriptor in manifest["candidates"]:
        candidate = _load_canonical_json(
            descriptor["path"],
            "frozen candidate",
        )
        candidate_id = candidate["candidate_id"]
        if candidate_id in candidates:
            raise RuntimeContractError(
                "frozen candidate ID is duplicated"
            )
        candidates[candidate_id] = candidate
    return manifest, candidates


def _load_direction_identity(path, label):
    value = _load_canonical_json(path, label)
    try:
        return direction_contract_lib.validate_identity(value)
    except direction_contract_lib.DirectionContractError as exc:
        raise RuntimeContractError(f"{label} is invalid") from exc


def _load_canonical_direction_contract(path, label):
    raw = _read_bound_regular(
        path,
        label,
        maximum=direction_contract_lib.MAX_CONTRACT_BYTES,
    )
    try:
        contract, canonical_raw, identity = (
            direction_contract_lib.parse_contract_bytes(raw)
        )
    except direction_contract_lib.DirectionContractError as exc:
        raise RuntimeContractError(f"{label} is invalid") from exc
    if raw != canonical_raw:
        raise RuntimeContractError(f"{label} is not canonical")
    return contract, raw, identity


def copy_verified_direction_contract(
    *,
    contract_path,
    round_identity_path,
    expected_direction,
    batch_path,
    output_path,
):
    """Publish the exact round contract after one identity-bound read."""
    try:
        expected = direction_contract_lib.validate_identity(
            expected_direction
        )
    except direction_contract_lib.DirectionContractError as exc:
        raise RuntimeContractError(
            "expected direction identity is invalid"
        ) from exc
    round_identity = _load_direction_identity(
        round_identity_path, "round direction identity"
    )
    batch, _ = _load_batch_candidates(batch_path)
    _, raw, actual = _load_canonical_direction_contract(
        contract_path, "round direction contract"
    )
    if (
        expected is None
        or round_identity != expected
        or frozen_batch_direction(batch) != expected
        or actual != expected
    ):
        raise RuntimeContractError(
            "direction identity changed before selector copy"
        )
    _publish_immutable(output_path, raw)
    return actual


def validate_direction_gate(
    *,
    contract_path,
    expected_direction,
    batch_path,
    verdicts_path,
    output_path,
):
    """Validate selector verdicts against startup, batch, and gate identity."""
    try:
        expected = direction_contract_lib.validate_identity(
            expected_direction
        )
    except direction_contract_lib.DirectionContractError as exc:
        raise RuntimeContractError(
            "expected direction identity is invalid"
        ) from exc
    batch, _ = _load_batch_candidates(batch_path)
    _, _, actual = _load_canonical_direction_contract(
        contract_path, "direction gate contract"
    )
    if (
        expected is None
        or frozen_batch_direction(batch) != expected
        or actual != expected
    ):
        raise RuntimeContractError(
            "direction identity changed before verdict validation"
        )
    candidate_ids = [
        item["candidate_id"] for item in batch["candidates"]
    ]
    verdict_raw = _read_bound_regular(
        verdicts_path,
        "direction verdicts",
        maximum=65536,
    )
    try:
        verdicts = direction_contract_lib.require_all_in_scope(
            verdict_raw, candidate_ids
        )
    except direction_contract_lib.DirectionContractError as exc:
        raise RuntimeContractError(
            "direction verdicts are invalid"
        ) from exc
    receipt = {
        "schema_version": 1,
        "direction": actual,
        "candidate_count": len(candidate_ids),
        "verdicts": verdicts,
    }
    _publish_immutable(output_path, canonical_bytes(receipt))
    return receipt


def observe_frozen_batch(
    *,
    db_path,
    policy_path,
    batch_path,
    artifact_root,
    authority=None,
):
    _, candidates = _load_batch_candidates(batch_path)
    policy = history_projection.load_policy(policy_path)
    if policy["mode"] == "enforcement":
        _validated_runtime_authority(
            policy,
            authority,
            state_paths=(artifact_root,),
        )
    role_path = ROOT / "roles" / "history-compare.md"
    root = pathlib.Path(
        os.path.abspath(os.fspath(artifact_root))
    )
    _mkdir_single_use(root)
    conn = _connect_history_store(db_path)
    history_store.init_schema(conn)
    try:
        results = []
        for candidate_id, candidate in candidates.items():
            candidate_root = root / candidate_id
            observation = build_candidate_packs(
                conn=conn,
                candidate=candidate,
                policy=policy,
                artifact_root=candidate_root,
                comparator_role_bytes=_read_regular(
                    role_path,
                    1024 * 1024,
                    "history comparator role",
                ),
                comparator_role_identity="roles/history-compare.md",
            )
            results.append(
                {
                    "candidate_id": candidate_id,
                    "candidate_content_sha256":
                        candidate["content_sha256"],
                    "observation_path": str(
                        candidate_root / "build-observation.json"
                    ),
                    "observation_sha256":
                        observation["observation_sha256"],
                    "retrieval_statuses": [
                        item["retrieval_status"]
                        for item in observation["observations"]
                    ],
                }
            )
        result = {
            "schema_version": 1,
            "batch_path": str(
                pathlib.Path(batch_path).resolve()
            ),
            "candidates": results,
        }
        result["round_observation_sha256"] = sha256(
            b"history-runtime-round-observation-v1\0"
            + canonical_bytes(result)
        )
        _publish_immutable(
            root / "round-observation.json",
            canonical_bytes(result),
        )
        return result
    finally:
        conn.close()


def _contained_comparator_runner(
    *,
    db_path,
    policy_path,
    command_json,
    stage_root,
    authority=None,
    test_comparator_status=None,
):
    counter = {"value": 0}
    records = []

    def run(intent, pack, intent_root):
        counter["value"] += 1
        publication = pack.get("pack_publication_id")
        identity = (
            publication
            if isinstance(publication, str) and publication
            else sha256(canonical_bytes(pack))
        )
        invocation_root = (
            pathlib.Path(stage_root)
            / (
                f"{counter['value']:02d}-{intent}-"
                f"{identity[:16]}"
            )
        )
        pack_sha = sha256(canonical_bytes(pack))
        input_path = _comparison_attempt_path(
            intent_root,
            "retrieval-pack",
            pack.get("expansion_round"),
        )
        if (
            not input_path.is_file()
            or sha256(
                _read_bound_regular(
                    input_path,
                    "selected comparator pack",
                    maximum=65536,
                )
            )
            != pack_sha
        ):
            raise RuntimeContractError(
                "comparator pack file differs from selected pack"
            )
        prepared = _build_stage_manifest(
            stage="history-compare",
            seat_id=f"history-compare-{counter['value']}",
            db_path=db_path,
            policy_path=policy_path,
            input_paths={"retrieval_pack.json": input_path},
            output_root=invocation_root / "output",
            manifest_path=invocation_root / "manifest.json",
            command_json=command_json,
            authority=authority,
            test_comparator_status=test_comparator_status,
        )
        run_contained_stage(prepared, authority=authority)
        records.append(
            {
                "prepared": prepared,
                "completion_sha256": sha256(
                    _read_bound_regular(
                        prepared["completion_path"],
                        "contained comparator completion",
                        maximum=1024 * 1024,
                    )
                ),
            }
        )
        return _load_canonical_json(
            prepared["output_paths"]["history-comparison.json"],
            "contained comparator output",
        )

    run.stage_records = records
    return run


def _portable_comparator_runner(
    *,
    request_profile,
    policy,
    stage_root,
):
    profile = _validated_portable_request_profile(
        request_profile
    )
    reference_root = pathlib.Path(stage_root).parent
    counter = {"value": 0}
    records = []

    def run(intent, pack, intent_root):
        counter["value"] += 1
        publication = pack.get("pack_publication_id")
        identity = (
            publication
            if isinstance(publication, str) and publication
            else sha256(canonical_bytes(pack))
        )
        invocation_root = (
            pathlib.Path(stage_root)
            / (
                f"{counter['value']:02d}-{intent}-"
                f"{identity[:16]}"
            )
        )
        input_path = _comparison_attempt_path(
            intent_root,
            "retrieval-pack",
            pack.get("expansion_round"),
        )
        if (
            not input_path.is_file()
            or sha256(
                _read_bound_regular(
                    input_path,
                    "selected portable comparator pack",
                    maximum=65536,
                )
            )
            != sha256(canonical_bytes(pack))
        ):
            raise RuntimeContractError(
                "comparator pack file differs from selected pack"
            )
        prepared = _run_portable_stage(
            request_profile=profile,
            stage="history-compare",
            seat_id=f"history-compare-{counter['value']}",
            input_paths={"retrieval_pack.json": input_path},
            invocation_root=invocation_root,
            policy=policy,
        )
        records.append(
            _public_portable_stage(prepared, reference_root)
        )
        return _load_canonical_json(
            prepared["output_paths"]["history-comparison.json"],
            "portable comparator output",
        )

    run.stage_records = records
    return run


def _validate_comparison_executor(
    *, executor, command_json, portable_request_profile
):
    if executor == PORTABLE_EXECUTOR:
        if command_json is not None:
            raise RuntimeContractError(
                "portable-v2 cannot mix command_json"
            )
        if portable_request_profile is None:
            raise RuntimeContractError(
                "portable-v2 requires portable_request_profile"
            )
        _validated_portable_request_profile(
            portable_request_profile
        )
        return
    if executor == CONTAINED_EXECUTOR:
        if portable_request_profile is not None:
            raise RuntimeContractError(
                "contained-v1 cannot use portable_request_profile"
            )
        if command_json is None:
            raise RuntimeContractError(
                "contained-v1 requires command_json"
            )
        return
    raise RuntimeContractError("comparison executor is invalid")


def _compare_frozen_targets(
    *,
    db_path,
    policy_path,
    batch_path,
    artifact_root,
    selection_path,
    command_json=None,
    executor=CONTAINED_EXECUTOR,
    portable_request_profile=None,
    authority=None,
    test_comparator_status=None,
    pack_builder=history_retrieval.build_pack,
):
    _validate_comparison_executor(
        executor=executor,
        command_json=command_json,
        portable_request_profile=portable_request_profile,
    )
    _require_context_test_paths(
        (
            db_path,
            policy_path,
            batch_path,
            artifact_root,
            selection_path,
        )
    )
    batch, candidates = _load_batch_candidates(batch_path)
    selection = verify_round_selection(selection_path)
    if (
        selection["batch_sha256"] != batch["batch_sha256"]
        or pathlib.Path(selection["batch_path"]).resolve()
        != pathlib.Path(batch_path).resolve()
        or pathlib.Path(
            selection["round_observation_path"]
        ).parent.resolve()
        != pathlib.Path(artifact_root).resolve()
    ):
        raise RuntimeContractError(
            "comparison selection is outside the frozen round"
        )
    policy = history_projection.load_policy(policy_path)
    if (
        pack_builder is not history_retrieval.build_pack
        and _active_test_runtime(policy) is None
    ):
        raise RuntimeContractError(
            "test retrieval builder is unavailable in production"
        )
    if policy["mode"] == "enforcement":
        _validated_runtime_authority(
            policy,
            authority,
            state_paths=(artifact_root,),
        )
    role_path = ROOT / "roles" / "history-compare.md"
    root = pathlib.Path(
        os.path.abspath(os.fspath(artifact_root))
    )
    conn = _connect_history_store(db_path)
    history_store.init_schema(conn)
    try:
        results = []
        for target in selection["targets"]:
            candidate_id = target["candidate_id"]
            candidate = candidates.get(candidate_id)
            if candidate is None:
                raise RuntimeContractError(
                    "comparison target is outside the frozen batch"
                )
            candidate_root = root / candidate_id
            observation = _load_canonical_json(
                candidate_root / "build-observation.json",
                "candidate build observation",
            )
            if executor == CONTAINED_EXECUTOR:
                runner = _contained_comparator_runner(
                    db_path=db_path,
                    policy_path=policy_path,
                    command_json=command_json,
                    stage_root=(
                        candidate_root / "contained-comparisons"
                    ),
                    authority=authority,
                    test_comparator_status=test_comparator_status,
                )
            else:
                if test_comparator_status is not None:
                    raise RuntimeContractError(
                        "portable comparator cannot use test controls"
                    )
                runner = _portable_comparator_runner(
                    request_profile=portable_request_profile,
                    policy=policy,
                    stage_root=(
                        candidate_root / "portable-comparisons"
                    ),
                )
            compared = compare_selected_candidate(
                conn=conn,
                candidate=candidate,
                policy=policy,
                artifact_root=candidate_root,
                observation=observation,
                comparator_runner=runner,
                pack_builder=pack_builder,
                comparator_role_bytes=_read_regular(
                    role_path,
                    1024 * 1024,
                    "history comparator role",
                ),
                comparator_role_identity="roles/history-compare.md",
            )
            results.append(
                {
                    "candidate_id": candidate_id,
                    "observation_path": str(
                        candidate_root
                        / "comparison-observation.json"
                    ),
                    "observation_sha256":
                        compared["observation_sha256"],
                    "statuses": [
                        item["status"]
                        for item in compared["observations"]
                    ],
                    (
                        "contained_stages"
                        if executor == CONTAINED_EXECUTOR
                        else "portable_stages"
                    ): list(runner.stage_records),
                }
            )
        result = {"schema_version": 1, "targets": results}
        hash_domain = b"history-runtime-comparison-index-v1\0"
        if executor == PORTABLE_EXECUTOR:
            result["schema_version"] = 2
            result["execution_boundary"] = (
                PORTABLE_EXECUTION_BOUNDARY
            )
            hash_domain = b"history-runtime-comparison-index-v2\0"
        result["comparison_index_sha256"] = sha256(
            hash_domain + canonical_bytes(result)
        )
        _publish_immutable(
            root / "comparison-index.json",
            canonical_bytes(result),
        )
        return result
    finally:
        conn.close()


def compare_frozen_targets(
    *,
    db_path,
    policy_path,
    batch_path,
    artifact_root,
    selection_path,
    command_json=None,
    executor=CONTAINED_EXECUTOR,
    portable_request_profile=None,
    authority=None,
):
    """Compare selected candidates without exposing test controls."""
    return _compare_frozen_targets(
        db_path=db_path,
        policy_path=policy_path,
        batch_path=batch_path,
        artifact_root=artifact_root,
        selection_path=selection_path,
        command_json=command_json,
        executor=executor,
        portable_request_profile=portable_request_profile,
        authority=authority,
    )


def _compare_frozen_targets_for_test(
    *,
    test_authority,
    test_state_root,
    test_comparator_status,
    test_expansion_pack_builder=None,
    **values,
):
    paths = (
        values["db_path"],
        values["policy_path"],
        values["batch_path"],
        values["artifact_root"],
        values["selection_path"],
    )
    _require_test_state_paths(test_state_root, paths)
    policy = history_projection.load_policy(values["policy_path"])
    call_values = dict(values)
    supplied_authority = call_values.get("authority")
    if (
        supplied_authority is not None
        and supplied_authority is not test_authority
    ):
        raise RuntimeContractError(
            "test comparison authority changed"
        )
    if policy["mode"] == "enforcement":
        call_values["authority"] = test_authority
    with _runtime_for_test(
        policy,
        test_authority,
        test_state_root,
        state_paths=paths,
    ):
        if test_expansion_pack_builder is not None:
            call_values["pack_builder"] = (
                test_expansion_pack_builder
            )
        return _compare_frozen_targets(
            test_comparator_status=test_comparator_status,
            **call_values,
        )


def _recomputed_observation_items(candidate, observation, label):
    fields = {
        "schema_version",
        "candidate_id",
        "candidate_content_sha256",
        "observations",
        "observation_sha256",
    }
    if not isinstance(observation, dict) or set(observation) != fields:
        raise RuntimeContractError(f"{label} is invalid")
    items = observation["observations"]
    material = dict(observation)
    observation_sha = material.pop("observation_sha256")
    if (
        type(observation["schema_version"]) is not int
        or observation["schema_version"] != 1
        or observation["candidate_id"] != candidate["candidate_id"]
        or observation["candidate_content_sha256"]
        != candidate["content_sha256"]
        or not isinstance(items, list)
        or any(not isinstance(item, dict) for item in items)
        or [item.get("intent") for item in items]
        != required_intents(candidate)
        or not _valid_sha256(observation_sha)
        or observation_sha
        != sha256(
            b"history-runtime-observation-v1\0"
            + canonical_bytes(material)
        )
    ):
        raise RuntimeContractError(f"{label} is invalid")
    return items


def _receipt_bindings(
    candidate, root, observation, *, require_permanent
):
    expected = required_intents(candidate)
    items = _recomputed_observation_items(
        candidate, observation, "compared observation"
    )
    bindings = []
    for item in items:
        if item.get("status") not in history_retrieval.PERMANENT_STATUSES:
            if require_permanent:
                raise RuntimeContractError(
                    "history comparison is not permanent"
                )
            continue
        pack_path = item.get("pack_path")
        receipt_path = item.get("receipt_path")
        intent_root = root / item["intent"]
        attempts = item.get("attempts")
        if not isinstance(attempts, list) or not attempts:
            raise RuntimeContractError(
                "history comparison attempts are invalid"
            )
        final_round = len(attempts) - 1
        expected_pack_path = _comparison_attempt_path(
            intent_root, "retrieval-pack", final_round
        ).resolve()
        expected_receipt_path = _comparison_attempt_path(
            intent_root, "history-receipt", final_round
        ).resolve()
        if (
            not isinstance(pack_path, str)
            or pathlib.Path(pack_path).resolve()
            != expected_pack_path
            or not isinstance(receipt_path, str)
            or pathlib.Path(receipt_path).resolve()
            != expected_receipt_path
            or attempts[-1].get("pack_path") != pack_path
            or attempts[-1].get("receipt_path") != receipt_path
        ):
            raise RuntimeContractError(
                "history comparison artifact path is invalid"
            )
        pack = _load_canonical_json(
            pack_path, f"{item['intent']} summary pack"
        )
        receipt = _load_canonical_json(
            receipt_path, f"{item['intent']} summary receipt"
        )
        bindings.append((pack, receipt))
    return bindings


def _summary_bindings(candidate, root, observation):
    return _receipt_bindings(
        candidate,
        root,
        observation,
        require_permanent=True,
    )


def publish_candidate_summary(
    *,
    db_path,
    policy_path,
    batch_path,
    artifact_root,
    candidate_id,
    output_path,
    authority=None,
):
    _, candidates = _load_batch_candidates(batch_path)
    candidate = candidates.get(candidate_id)
    if candidate is None:
        raise RuntimeContractError(
            "summary candidate is outside the frozen batch"
        )
    root = pathlib.Path(artifact_root) / candidate_id
    observation = _load_canonical_json(
        root / "comparison-observation.json",
        "compared candidate observation",
    )
    policy = history_projection.load_policy(policy_path)
    if policy["mode"] != "enforcement":
        raise RuntimeContractError(
            "shadow mode cannot publish history summaries"
        )
    _validated_runtime_authority(
        policy, authority, state_paths=(output_path,)
    )
    conn = _connect_history_store(db_path)
    history_store.init_schema(conn)
    try:
        bindings = _summary_bindings(
            candidate, root, observation
        )
        summary = build_history_summary(
            conn, candidate, bindings, policy
        )
        verify_history_summary(conn, candidate, summary, policy)
        _publish_immutable(
            output_path, canonical_bytes(summary)
        )
        return {
            "schema_version": 1,
            "candidate_id": candidate_id,
            "summary_path": str(pathlib.Path(output_path)),
            "summary_sha256": sha256(canonical_bytes(summary)),
            "overall_status": (
                "complete_match"
                if any(
                    item["status"] == "complete_match"
                    for item in summary["receipts"]
                )
                else "complete_no_match"
            ),
        }
    finally:
        conn.close()


def publish_round_summaries(
    *,
    db_path,
    policy_path,
    batch_path,
    selection_path,
    artifact_root,
    authority=None,
):
    policy = history_projection.load_policy(policy_path)
    if policy["mode"] != "enforcement":
        raise RuntimeContractError(
            "shadow mode cannot publish history summaries"
        )
    _validated_runtime_authority(
        policy, authority, state_paths=(artifact_root,)
    )
    batch, candidates = _load_batch_candidates(batch_path)
    selection = verify_round_selection(selection_path)
    root = pathlib.Path(artifact_root)
    if (
        selection["batch_sha256"] != batch["batch_sha256"]
        or pathlib.Path(selection["batch_path"]).resolve()
        != pathlib.Path(batch_path).resolve()
        or pathlib.Path(
            selection["round_observation_path"]
        ).parent.resolve()
        != root.resolve()
    ):
        raise RuntimeContractError(
            "summary selection is outside the frozen round"
        )
    conn = _connect_history_store(db_path)
    history_store.init_schema(conn)
    prepared = []
    abstentions = []
    try:
        for target in selection["targets"]:
            candidate_id = target["candidate_id"]
            candidate = candidates.get(candidate_id)
            if candidate is None:
                raise RuntimeContractError(
                    "summary target is outside the frozen batch"
                )
            candidate_root = root / candidate_id
            observation = _load_canonical_json(
                candidate_root / "comparison-observation.json",
                "compared candidate observation",
            )
            observation_items = observation.get("observations", [])
            statuses = [
                item.get("status")
                for item in observation_items
                if isinstance(item, dict)
            ]
            if (
                observation.get("candidate_id") != candidate_id
                or observation.get(
                    "candidate_content_sha256"
                )
                != candidate["content_sha256"]
                or [
                    item.get("intent")
                    for item in observation_items
                    if isinstance(item, dict)
                ]
                != required_intents(candidate)
            ):
                raise RuntimeContractError(
                    "summary comparison observation is invalid"
                )
            if not all(
                status in history_retrieval.PERMANENT_STATUSES
                for status in statuses
            ):
                abstentions.append(
                    {
                        "candidate_id": candidate_id,
                        "statuses": statuses,
                    }
                )
                continue
            bindings = _summary_bindings(
                candidate, candidate_root, observation
            )
            summary = build_history_summary(
                conn, candidate, bindings, policy
            )
            verify_history_summary(
                conn, candidate, summary, policy
            )
            prepared.append(
                (
                    candidate_id,
                    candidate_root / "history-summary.json",
                    summary,
                )
            )
    finally:
        conn.close()
    results = []
    for candidate_id, output_path, summary in prepared:
        raw = canonical_bytes(summary)
        _publish_immutable(output_path, raw)
        results.append(
            {
                "candidate_id": candidate_id,
                "summary_path": str(output_path),
                "summary_sha256": sha256(raw),
                "overall_status": (
                    "complete_match"
                    if any(
                        item["status"] == "complete_match"
                        for item in summary["receipts"]
                    )
                    else "complete_no_match"
                ),
            }
        )
    result = {
        "schema_version": 1,
        "selection_sha256": selection["selection_sha256"],
        "summaries": results,
        "abstentions": abstentions,
    }
    result["summary_index_sha256"] = sha256(
        b"history-runtime-summary-index-v1\0"
        + canonical_bytes(result)
    )
    _publish_immutable(
        root / "summary-index.json",
        canonical_bytes(result),
    )
    return result


def _frozen_candidate_view_bytes(batch, candidates, candidate_ids):
    order = list(candidate_ids)
    if len(order) != len(set(order)) or any(
        candidate_id not in candidates for candidate_id in order
    ):
        raise RuntimeContractError(
            "candidate view membership is invalid"
        )
    tsv_raw = _read_bound_regular(
        batch["ideas_tsv"]["path"],
        "candidate view frozen TSV",
        maximum=65536,
    )
    markdown_raw = _read_bound_regular(
        batch["ideas_markdown"]["path"],
        "candidate view frozen markdown",
        maximum=65536,
    )
    rows = {}
    for line in tsv_raw.splitlines(keepends=True):
        try:
            candidate_id = line.split(b"\t", 1)[0].decode(
                "utf-8"
            )
        except UnicodeDecodeError as exc:
            raise RuntimeContractError(
                "candidate view TSV is not UTF-8"
            ) from exc
        rows[candidate_id] = (
            line if line.endswith(b"\n") else line + b"\n"
        )
    try:
        markdown = markdown_raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeContractError(
            "candidate view markdown is not UTF-8"
        ) from exc
    headings = list(
        re.finditer(
            r"(?m)^## (I[1-9][0-9]*)[ \t]*$", markdown
        )
    )
    blocks = {}
    for index, heading in enumerate(headings):
        end = (
            len(markdown)
            if index + 1 == len(headings)
            else headings[index + 1].start()
        )
        block = markdown[heading.start():end]
        if not block.endswith("\n"):
            block += "\n"
        blocks[heading.group(1)] = block
    if (
        set(rows) != set(candidates)
        or set(blocks) != set(candidates)
        or len(headings) != len(candidates)
    ):
        raise RuntimeContractError(
            "candidate view frozen sources changed"
        )
    return (
        b"".join(rows[candidate_id] for candidate_id in order),
        "".join(
            blocks[candidate_id] + "\n"
            for candidate_id in order
        ).encode("utf-8"),
    )


def _verified_summary_index(
    *,
    conn,
    policy,
    batch,
    candidates,
    selection,
    artifact_root,
):
    root = pathlib.Path(artifact_root)
    index = _load_canonical_json(
        root / "summary-index.json", "round summary index"
    )
    fields = {
        "schema_version",
        "selection_sha256",
        "summaries",
        "abstentions",
        "summary_index_sha256",
    }
    if (
        not isinstance(index, dict)
        or set(index) != fields
        or type(index.get("schema_version")) is not int
        or index["schema_version"] != 1
        or index.get("selection_sha256")
        != selection["selection_sha256"]
    ):
        raise RuntimeContractError(
            "round summary index schema is invalid"
        )
    material = dict(index)
    index_sha = material.pop("summary_index_sha256")
    if index_sha != sha256(
        b"history-runtime-summary-index-v1\0"
        + canonical_bytes(material)
    ):
        raise RuntimeContractError(
            "round summary index ID is invalid"
        )
    summaries = {
        item.get("candidate_id"): item
        for item in index["summaries"]
        if isinstance(item, dict)
    }
    abstentions = {
        item.get("candidate_id"): item
        for item in index["abstentions"]
        if isinstance(item, dict)
    }
    targets = {
        item["candidate_id"]: item
        for item in selection["targets"]
    }
    if (
        len(summaries) != len(index["summaries"])
        or len(abstentions) != len(index["abstentions"])
        or set(summaries) & set(abstentions)
        or set(summaries) | set(abstentions) != set(targets)
    ):
        raise RuntimeContractError(
            "round summary index coverage is invalid"
        )
    verified = {}
    for candidate_id, target in targets.items():
        candidate = candidates[candidate_id]
        observation = _load_canonical_json(
            root
            / candidate_id
            / "comparison-observation.json",
            "research-view comparison observation",
        )
        items = observation.get("observations")
        statuses = (
            [item.get("status") for item in items]
            if isinstance(items, list)
            else None
        )
        if (
            observation.get("candidate_id") != candidate_id
            or observation.get("candidate_content_sha256")
            != candidate["content_sha256"]
            or [
                item.get("intent") for item in items or ()
                if isinstance(item, dict)
            ]
            != required_intents(candidate)
        ):
            raise RuntimeContractError(
                "research-view comparison binding changed"
            )
        permanent = bool(statuses) and all(
            status in history_retrieval.PERMANENT_STATUSES
            for status in statuses
        )
        if permanent:
            descriptor = summaries.get(candidate_id)
            expected_path = (
                root / candidate_id / "history-summary.json"
            )
            summary = _load_canonical_json(
                expected_path, "research-view history summary"
            )
            expected_status = (
                "complete_match"
                if any(
                    item["status"] == "complete_match"
                    for item in summary["receipts"]
                )
                else "complete_no_match"
            )
            if (
                descriptor
                != {
                    "candidate_id": candidate_id,
                    "summary_path": str(expected_path),
                    "summary_sha256":
                        sha256(canonical_bytes(summary)),
                    "overall_status": expected_status,
                }
            ):
                raise RuntimeContractError(
                    "research-view summary descriptor changed"
                )
            verify_history_summary(
                conn, candidate, summary, policy
            )
            verified[candidate_id] = {
                "candidate_id": candidate_id,
                "overall_status": expected_status,
                "summary": summary,
            }
        elif abstentions.get(candidate_id) != {
            "candidate_id": candidate_id,
            "statuses": statuses,
        }:
            raise RuntimeContractError(
                "research-view abstention changed"
            )
    return index, verified


def _validated_runtime_authority(
    policy, authority, *, state_paths=()
):
    value = _inspect_runtime_authority(authority)
    policy_sha = sha256(canonical_bytes(policy))
    if (
        value is None
        or value["mode"] != policy["mode"]
        or value["policy_sha256"] != policy_sha
    ):
        raise RuntimeContractError(
            "round commit lacks matching runtime authority"
        )
    if policy["mode"] == "enforcement":
        production = (
            value["scope"] == PRODUCTION_SCOPE
            and _valid_sha256(value["trust_root_sha256"])
        )
        test_context = _TEST_RUNTIME_CONTEXT.get()
        synthetic_test = False
        if (
            value["scope"] == SYNTHETIC_SCOPE
            and value["trust_root_sha256"] is None
            and isinstance(test_context, tuple)
            and len(test_context) == 3
            and test_context[0] is authority
            and test_context[2] == policy_sha
            and state_paths
        ):
            test_root = test_context[1]
            try:
                for path in state_paths:
                    pathlib.Path(path).resolve().relative_to(
                        test_root
                    )
            except (TypeError, ValueError):
                synthetic_test = False
            else:
                synthetic_test = True
        if not (production or synthetic_test):
            raise RuntimeContractError(
                "enforcement runtime authority is invalid"
            )
    return value


def _comparison_index(path, selection):
    index = _load_canonical_json(path, "comparison index")
    if not isinstance(index, dict):
        raise RuntimeContractError(
            "comparison index schema is invalid"
        )
    schema_version = index.get("schema_version")
    if schema_version == 1:
        fields = {
            "schema_version",
            "targets",
            "comparison_index_sha256",
        }
        hash_domain = b"history-runtime-comparison-index-v1\0"
        stage_field = "contained_stages"
    elif schema_version == 2:
        fields = {
            "schema_version",
            "execution_boundary",
            "targets",
            "comparison_index_sha256",
        }
        if (
            index.get("execution_boundary")
            != PORTABLE_EXECUTION_BOUNDARY
        ):
            raise RuntimeContractError(
                "comparison index schema is invalid"
            )
        hash_domain = b"history-runtime-comparison-index-v2\0"
        stage_field = "portable_stages"
    else:
        raise RuntimeContractError(
            "comparison index schema is invalid"
        )
    if set(index) != fields:
        raise RuntimeContractError(
            "comparison index schema is invalid"
        )
    material = dict(index)
    index_sha = material.pop("comparison_index_sha256")
    if index_sha != sha256(
        hash_domain + canonical_bytes(material)
    ):
        raise RuntimeContractError(
            "comparison index ID is invalid"
        )
    expected_ids = [
        item["candidate_id"] for item in selection["targets"]
    ]
    items = index["targets"]
    target_fields = {
        "candidate_id",
        "observation_path",
        "observation_sha256",
        "statuses",
        stage_field,
    }
    if not isinstance(items, list):
        raise RuntimeContractError(
            "comparison index target coverage is invalid"
        )
    if any(
        not isinstance(item, dict)
        or set(item) != target_fields
        or not isinstance(item.get("candidate_id"), str)
        or not isinstance(item.get("observation_path"), str)
        or not _valid_sha256(item.get("observation_sha256"))
        or not isinstance(item.get("statuses"), list)
        or not isinstance(item.get(stage_field), list)
        for item in items
    ) or [
        item["candidate_id"] for item in items
    ] != expected_ids:
        raise RuntimeContractError(
            "comparison index target coverage is invalid"
        )
    if schema_version == 2:
        reference_root = pathlib.Path(path).resolve().parent
        for target in items:
            stages = target[stage_field]
            if not isinstance(stages, list):
                raise RuntimeContractError(
                    "comparison portable stage coverage is invalid"
                )
            for stage in stages:
                verified = _verified_public_portable_stage(
                    stage,
                    reference_root / target["candidate_id"],
                )
                if verified.get("stage") != "history-compare":
                    raise RuntimeContractError(
                        "comparison portable stage is invalid"
                    )
    return index


def _comparison_stage_binding(index, target):
    if index["schema_version"] == 1:
        return CONTAINED_EXECUTOR, target["contained_stages"]
    return PORTABLE_EXECUTION_BOUNDARY, target["portable_stages"]


def _validated_review_comparison(
    *, candidate, candidate_root, index, indexed, conn, policy
):
    expected_path = (
        pathlib.Path(candidate_root) / "comparison-observation.json"
    )
    if pathlib.Path(indexed["observation_path"]).resolve() != (
        expected_path.resolve()
    ):
        raise RuntimeContractError(
            "review-plan comparison path is invalid"
        )
    observation = _load_canonical_json(
        expected_path, "review-plan comparison observation"
    )
    fields = {
        "schema_version",
        "candidate_id",
        "candidate_content_sha256",
        "observations",
        "observation_sha256",
    }
    if not isinstance(observation, dict) or set(observation) != fields:
        raise RuntimeContractError(
            "review-plan comparison observation is invalid"
        )
    items = observation["observations"]
    intents = required_intents(candidate)
    if (
        type(observation["schema_version"]) is not int
        or observation["schema_version"] != 1
        or observation["candidate_id"] != candidate["candidate_id"]
        or observation["candidate_content_sha256"]
        != candidate["content_sha256"]
        or not isinstance(items, list)
        or any(not isinstance(item, dict) for item in items)
        or [item.get("intent") for item in items] != intents
    ):
        raise RuntimeContractError(
            "review-plan comparison observation is invalid"
        )
    material = dict(observation)
    observation_sha = material.pop("observation_sha256")
    statuses = [item.get("status") for item in items]
    if (
        not _valid_sha256(observation_sha)
        or observation_sha
        != sha256(
            b"history-runtime-observation-v1\0"
            + canonical_bytes(material)
        )
        or observation_sha != indexed["observation_sha256"]
        or statuses != indexed["statuses"]
    ):
        raise RuntimeContractError(
            "review-plan comparison binding is invalid"
        )
    comparison_executor, stage_records = _comparison_stage_binding(
        index, indexed
    )
    _validate_resume_comparator_stages(
        candidate=candidate,
        candidate_root=candidate_root,
        observation=observation,
        stage_records=stage_records,
        conn=conn,
        policy=policy,
        allow_unbindable=True,
        execution_boundary=comparison_executor,
    )
    return observation, statuses


def _validate_review_candidate_artifact(
    artifact, descriptor, candidate
):
    expected = {
        "path": descriptor["path"],
        "sha256": descriptor["sha256"],
    }
    if not isinstance(artifact, dict) or artifact != expected:
        raise RuntimeContractError(
            "review-plan candidate binding changed"
        )
    raw = _read_bound_regular(
        descriptor["path"], "review-plan candidate", maximum=16384
    )
    if (
        sha256(raw) != descriptor["sha256"]
        or raw != canonical_bytes(candidate)
        or candidate.get("content_sha256")
        != descriptor["content_sha256"]
        or candidate_content_sha256(candidate)
        != descriptor["content_sha256"]
    ):
        raise RuntimeContractError("review-plan candidate changed")
    return expected


def _reviewer_command_descriptors(reviewer_commands):
    if (
        not isinstance(reviewer_commands, dict)
        or not reviewer_commands
        or len(reviewer_commands) > 16
    ):
        raise RuntimeContractError(
            "reviewer command registry is invalid"
        )
    stage_module = _stage_modules()
    parsed = {}
    for seat_id, command_json in reviewer_commands.items():
        if (
            not isinstance(seat_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", seat_id)
        ):
            raise RuntimeContractError(
                "reviewer seat ID is invalid"
            )
        try:
            command = stage_module.parse_command_json(command_json)
        except stage_module.StageError as exc:
            raise RuntimeContractError(str(exc)) from exc
        if not pathlib.Path(command[0]).is_absolute():
            raise RuntimeContractError(
                "review command executable must be absolute"
            )
        try:
            resolved = pathlib.Path(command[0]).resolve(strict=True)
        except OSError as exc:
            raise RuntimeContractError(
                "review command executable is unavailable"
            ) from exc
        normalized = [str(resolved)] + command[1:]
        parsed[seat_id] = {
            "seat_id": seat_id,
            "command_argv": normalized,
            "command_prefix_sha256": sha256(
                canonical_bytes(normalized)
            ),
        }
    return [
        parsed[seat_id]
        for seat_id in sorted(
            parsed,
            key=lambda value: (
                0,
                int(value),
            )
            if value.isdigit()
            else (1, value),
        )
    ]


def _reviewer_profile_descriptors(reviewer_request_profiles):
    if (
        not isinstance(reviewer_request_profiles, dict)
        or not reviewer_request_profiles
        or len(reviewer_request_profiles) > 16
    ):
        raise RuntimeContractError(
            "reviewer request profile registry is invalid"
        )
    parsed = {}
    for seat_id, profile in reviewer_request_profiles.items():
        if (
            not isinstance(seat_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", seat_id)
        ):
            raise RuntimeContractError(
                "reviewer seat ID is invalid"
            )
        parsed[seat_id] = _portable_profile_descriptor(
            profile, seat_id
        )
    return [
        parsed[seat_id]
        for seat_id in sorted(
            parsed,
            key=lambda value: (0, int(value))
            if value.isdigit()
            else (1, value),
        )
    ]


def _artifact_descriptor(path, label, maximum=1024 * 1024):
    descriptor, _ = _source_descriptor(
        path, label, maximum=maximum
    )
    return descriptor


def _review_plan_hash(plan):
    material = dict(plan)
    material.pop("review_plan_sha256", None)
    domain = b"history-runtime-review-plan-v1\0"
    if (
        plan.get("schema_version") == 2
        and plan.get("execution_boundary")
        == PORTABLE_EXECUTION_BOUNDARY
    ):
        domain = b"history-runtime-review-plan-v2\0"
    return sha256(
        domain + canonical_bytes(material)
    )


def _validate_review_plan_executor(
    *, executor, reviewer_commands, reviewer_request_profiles
):
    if executor == PORTABLE_EXECUTOR:
        if reviewer_commands:
            raise RuntimeContractError(
                "portable-v2 cannot mix reviewer_commands"
            )
        if reviewer_request_profiles is None:
            raise RuntimeContractError(
                "portable-v2 requires reviewer_request_profiles"
            )
        _reviewer_profile_descriptors(
            reviewer_request_profiles
        )
        return
    if executor == CONTAINED_EXECUTOR:
        if reviewer_request_profiles is not None:
            raise RuntimeContractError(
                "contained-v1 cannot use reviewer_request_profiles"
            )
        if not reviewer_commands:
            raise RuntimeContractError(
                "contained-v1 requires reviewer_commands"
            )
        return
    raise RuntimeContractError("review executor is invalid")


def seal_round_review_plan(
    *,
    db_path,
    policy_path,
    batch_path,
    selection_path,
    comparison_index_path,
    artifact_root,
    prior_work_path,
    review_contract_path,
    reviewer_commands,
    executor=CONTAINED_EXECUTOR,
    reviewer_request_profiles=None,
    round_date,
    min_read,
    axiom_min_cracks,
    output_path,
    authority=None,
):
    _validate_review_plan_executor(
        executor=executor,
        reviewer_commands=reviewer_commands,
        reviewer_request_profiles=reviewer_request_profiles,
    )
    if (
        not isinstance(round_date, str)
        or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", round_date)
        or type(min_read) is not int
        or min_read < 0
        or type(axiom_min_cracks) is not int
        or axiom_min_cracks < 1
    ):
        raise RuntimeContractError(
            "review gate configuration is invalid"
        )
    policy = history_projection.load_policy(policy_path)
    authority_value = _validated_runtime_authority(
        policy, authority, state_paths=(output_path,)
    )
    batch, candidates = _load_batch_candidates(batch_path)
    selection = verify_round_selection(selection_path)
    root = pathlib.Path(artifact_root)
    if (
        selection["batch_sha256"] != batch["batch_sha256"]
        or pathlib.Path(selection["batch_path"]).resolve()
        != pathlib.Path(batch_path).resolve()
        or pathlib.Path(
            selection["round_observation_path"]
        ).parent.resolve()
        != root.resolve()
    ):
        raise RuntimeContractError(
            "review plan selection is outside the frozen round"
        )
    index = _comparison_index(
        comparison_index_path, selection
    )
    if (
        executor == CONTAINED_EXECUTOR
        and index["schema_version"] != 1
    ) or (
        executor == PORTABLE_EXECUTOR
        and (
            index["schema_version"] != 2
            or index.get("execution_boundary")
            != PORTABLE_EXECUTION_BOUNDARY
        )
    ):
        raise RuntimeContractError(
            "review plan execution boundary changed"
        )
    if executor == CONTAINED_EXECUTOR:
        seats = _reviewer_command_descriptors(
            reviewer_commands
        )
        public_seats = [
            {
                "seat_id": item["seat_id"],
                "command_prefix_sha256":
                    item["command_prefix_sha256"],
            }
            for item in seats
        ]
    else:
        public_seats = _reviewer_profile_descriptors(
            reviewer_request_profiles
        )
    review_contract_raw = _read_bound_regular(
        review_contract_path,
        "review contract",
        maximum=16384,
    )
    prior_source_raw = _read_bound_regular(
        prior_work_path,
        "round prior work",
        maximum=1024 * 1024,
    )
    try:
        prior_markdown = prior_source_raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeContractError(
            "round prior work is not UTF-8"
        ) from exc
    prior_blocks = _candidate_blocks(prior_markdown)
    output = pathlib.Path(
        os.path.abspath(os.fspath(output_path))
    )
    input_root = output.parent / (output.name + "-inputs")
    review_contract_path = (
        input_root / "review-contract.md"
    )
    prior_source_path = input_root / "prior-work-source.md"
    input_publications = {
        "review-contract.md": review_contract_raw,
        "prior-work-source.md": prior_source_raw,
    }
    review_contract = {
        "path": str(review_contract_path.resolve()),
        "sha256": sha256(review_contract_raw),
        "byte_count": len(review_contract_raw),
    }
    prior_source = {
        "path": str(prior_source_path.resolve()),
        "sha256": sha256(prior_source_raw),
        "byte_count": len(prior_source_raw),
    }
    candidate_descriptors = {
        item["candidate_id"]: item
        for item in batch["candidates"]
    }
    targets = []
    conn = _connect_history_store(db_path)
    history_store.init_schema(conn)
    try:
        for selected, indexed in zip(
            selection["targets"], index["targets"]
        ):
            candidate_id = selected["candidate_id"]
            candidate = candidates[candidate_id]
            candidate_descriptor = candidate_descriptors[
                candidate_id
            ]
            _, statuses = _validated_review_comparison(
                candidate=candidate,
                candidate_root=root / candidate_id,
                index=index,
                indexed=indexed,
                conn=conn,
                policy=policy,
            )
            permanent = all(
                status in history_retrieval.PERMANENT_STATUSES
                for status in statuses
            )
            if policy["mode"] == "enforcement" and not permanent:
                outcome = "history_abstain"
            elif selected["disposition"] == "prescreen_kill":
                outcome = "prescreen_kill"
            else:
                outcome = "review"
            gate_summary = None
            mounted_summary = None
            if policy["mode"] == "enforcement" and permanent:
                summary_path = (
                    root / candidate_id / "history-summary.json"
                )
                summary = _load_canonical_json(
                    summary_path,
                    "review-plan history summary",
                )
                verify_history_summary(
                    conn, candidate, summary, policy
                )
                gate_summary = _artifact_descriptor(
                    summary_path,
                    "review-plan history summary",
                    maximum=16384,
                )
                if (
                    outcome == "review"
                    and any(
                        item["status"] == "complete_match"
                        for item in summary["receipts"]
                    )
                ):
                    mounted_summary = dict(gate_summary)
            prior = None
            if outcome == "review":
                block = prior_blocks.get(candidate_id)
                if block is None:
                    raise RuntimeContractError(
                        "review target lacks exact prior work"
                    )
                prior_path = (
                    input_root / candidate_id / "prior-work.md"
                )
                prior_raw = block.encode("utf-8")
                input_publications[
                    f"{candidate_id}/prior-work.md"
                ] = prior_raw
                prior = {
                    "path": str(prior_path.resolve()),
                    "sha256": sha256(prior_raw),
                    "byte_count": len(prior_raw),
                }
            candidate_artifact = {
                "path": candidate_descriptor["path"],
                "sha256": candidate_descriptor["sha256"],
            }
            _validate_review_candidate_artifact(
                candidate_artifact, candidate_descriptor, candidate
            )
            targets.append(
                {
                    "candidate_id": candidate_id,
                    "candidate_content_sha256":
                        candidate["content_sha256"],
                    "candidate_artifact": candidate_artifact,
                    "selection_disposition":
                        selected["disposition"],
                    "prescreen_evidence": selected.get("evidence"),
                    "comparison_observation_sha256":
                        indexed["observation_sha256"],
                    "history_statuses": statuses,
                    "planned_outcome": outcome,
                    "prior_work": prior,
                    "gate_summary": gate_summary,
                    "mounted_history_summary": mounted_summary,
                }
            )
    finally:
        conn.close()
    outcome_map = {
        item["candidate_id"]: item["planned_outcome"]
        for item in targets
    }
    shortlist_order = _sealed_shortlist_order(
        batch, candidates, selection
    )
    commit_order = [
        item["candidate_id"]
        for item in targets
        if item["planned_outcome"] == "prescreen_kill"
    ] + [
        candidate_id
        for candidate_id in shortlist_order
        if outcome_map[candidate_id] == "review"
    ]
    result = {
        "schema_version": (
            1 if executor == CONTAINED_EXECUTOR else 2
        ),
        "batch_path": str(
            pathlib.Path(batch_path).resolve()
        ),
        "batch_sha256": batch["batch_sha256"],
        "selection_path": str(
            pathlib.Path(selection_path).resolve()
        ),
        "selection_sha256": selection["selection_sha256"],
        "comparison_index_path": str(
            pathlib.Path(comparison_index_path).resolve()
        ),
        "comparison_index_sha256":
            index["comparison_index_sha256"],
        "artifact_root": str(root.resolve()),
        "policy_mode": policy["mode"],
        "policy_sha256": sha256(canonical_bytes(policy)),
        "capability_sha256":
            authority_value["capability_sha256"],
        "trust_root_sha256":
            authority_value["trust_root_sha256"],
        "review_contract": review_contract,
        "prior_work_source": prior_source,
        "reviewer_seats": public_seats,
        "gate_config": {
            "round_date": round_date,
            "min_read": min_read,
            "axiom_min_cracks": axiom_min_cracks,
        },
        "commit_order": commit_order,
        "targets": targets,
    }
    if executor == PORTABLE_EXECUTOR:
        result["execution_boundary"] = (
            PORTABLE_EXECUTION_BOUNDARY
        )
    result["review_plan_sha256"] = _review_plan_hash(result)
    _publish_immutable_tree(input_root, input_publications)
    try:
        _publish_immutable(output, canonical_bytes(result))
    except Exception:
        _remove_immutable_tree(input_root)
        raise
    return result


def verify_round_review_plan(
    *,
    db_path,
    policy_path,
    batch_path,
    review_plan_path,
    authority=None,
    _connection=None,
):
    plan = _load_canonical_json(
        review_plan_path, "round review plan"
    )
    common_fields = {
        "schema_version",
        "batch_path",
        "batch_sha256",
        "selection_path",
        "selection_sha256",
        "comparison_index_path",
        "comparison_index_sha256",
        "artifact_root",
        "policy_mode",
        "policy_sha256",
        "capability_sha256",
        "trust_root_sha256",
        "review_contract",
        "prior_work_source",
        "reviewer_seats",
        "gate_config",
        "commit_order",
        "targets",
        "review_plan_sha256",
    }
    if not isinstance(plan, dict):
        raise RuntimeContractError(
            "round review plan schema is invalid"
        )
    if plan.get("schema_version") == 1:
        fields = common_fields
        execution_boundary = CONTAINED_EXECUTOR
        seat_identity_field = "command_prefix_sha256"
    elif (
        plan.get("schema_version") == 2
        and plan.get("execution_boundary")
        == PORTABLE_EXECUTION_BOUNDARY
    ):
        fields = common_fields | {"execution_boundary"}
        execution_boundary = PORTABLE_EXECUTION_BOUNDARY
        seat_identity_field = "execution_request_profile_hash"
    else:
        raise RuntimeContractError(
            "round review plan schema is invalid"
        )
    if (
        set(plan) != fields
        or plan.get("review_plan_sha256")
        != _review_plan_hash(plan)
    ):
        raise RuntimeContractError(
            "round review plan schema is invalid"
        )
    policy = history_projection.load_policy(policy_path)
    authority_value = _validated_runtime_authority(
        policy, authority, state_paths=(db_path,)
    )
    batch, candidates = _load_batch_candidates(batch_path)
    selection = verify_round_selection(plan["selection_path"])
    index = _comparison_index(
        plan["comparison_index_path"], selection
    )
    if (
        pathlib.Path(plan["batch_path"]).resolve()
        != pathlib.Path(batch_path).resolve()
        or plan["batch_sha256"] != batch["batch_sha256"]
        or plan["selection_sha256"]
        != selection["selection_sha256"]
        or plan["comparison_index_sha256"]
        != index["comparison_index_sha256"]
        or (
            execution_boundary == CONTAINED_EXECUTOR
            and index["schema_version"] != 1
        )
        or (
            execution_boundary == PORTABLE_EXECUTION_BOUNDARY
            and (
                index["schema_version"] != 2
                or index.get("execution_boundary")
                != PORTABLE_EXECUTION_BOUNDARY
            )
        )
        or plan["policy_mode"] != policy["mode"]
        or plan["policy_sha256"]
        != sha256(canonical_bytes(policy))
        or plan["capability_sha256"]
        != authority_value["capability_sha256"]
        or plan["trust_root_sha256"]
        != authority_value["trust_root_sha256"]
        or pathlib.Path(plan["artifact_root"]).resolve()
        != pathlib.Path(
            selection["round_observation_path"]
        ).parent.resolve()
    ):
        raise RuntimeContractError(
            "round review plan root binding changed"
        )
    review_plan = pathlib.Path(
        os.path.abspath(os.fspath(review_plan_path))
    )
    review_input_root = review_plan.parent / (
        review_plan.name + "-inputs"
    )
    source_raw = {}
    for name, relative_path, maximum in (
        ("review_contract", "review-contract.md", 16384),
        (
            "prior_work_source",
            "prior-work-source.md",
            1024 * 1024,
        ),
    ):
        source_raw[name] = _read_rooted_frozen_descriptor(
            review_input_root,
            plan[name],
            relative_path,
            f"round review plan {name}",
            maximum=maximum,
            fields={"path", "sha256", "byte_count"},
        )
    try:
        prior_markdown = source_raw["prior_work_source"].decode(
            "utf-8"
        )
    except UnicodeDecodeError as exc:
        raise RuntimeContractError(
            "round prior work is not UTF-8"
        ) from exc
    prior_blocks = _candidate_blocks(prior_markdown)
    seats = plan["reviewer_seats"]
    if (
        not isinstance(seats, list)
        or not seats
        or len(seats) > 16
        or len(
            {
                item.get("seat_id")
                for item in seats
                if isinstance(item, dict)
            }
        )
        != len(seats)
        or any(
            not isinstance(item, dict)
            or set(item)
            != {"seat_id", seat_identity_field}
            or not re.fullmatch(
                r"[A-Za-z0-9_.-]{1,32}",
                item.get("seat_id", ""),
            )
            or not _valid_sha256(
                item.get(seat_identity_field)
            )
            for item in seats
        )
    ):
        raise RuntimeContractError(
            "round review plan seats are invalid"
        )
    gate = plan["gate_config"]
    if (
        not isinstance(gate, dict)
        or set(gate)
        != {"round_date", "min_read", "axiom_min_cracks"}
        or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}",
            gate.get("round_date", ""),
        )
        or type(gate.get("min_read")) is not int
        or gate["min_read"] < 0
        or type(gate.get("axiom_min_cracks")) is not int
        or gate["axiom_min_cracks"] < 1
    ):
        raise RuntimeContractError(
            "round review plan gate is invalid"
        )
    targets = plan["targets"]
    if not isinstance(targets, list) or len(targets) != len(
        index["targets"]
    ):
        raise RuntimeContractError(
            "round review plan target coverage is invalid"
        )
    if any(not isinstance(item, dict) for item in targets) or [
        item.get("candidate_id") for item in targets
    ] != [
        item["candidate_id"] for item in selection["targets"]
    ]:
        raise RuntimeContractError(
            "round review plan target coverage is invalid"
        )
    candidate_descriptors = {
        item["candidate_id"]: item for item in batch["candidates"]
    }
    owns_connection = _connection is None
    conn = (
        _connect_history_store(db_path)
        if owns_connection
        else _connection
    )
    if owns_connection:
        history_store.init_schema(conn)
    try:
        for target, selected, indexed in zip(
            targets, selection["targets"], index["targets"]
        ):
            candidate = candidates.get(target["candidate_id"])
            expected_fields = {
                "candidate_id",
                "candidate_content_sha256",
                "candidate_artifact",
                "selection_disposition",
                "prescreen_evidence",
                "comparison_observation_sha256",
                "history_statuses",
                "planned_outcome",
                "prior_work",
                "gate_summary",
                "mounted_history_summary",
            }
            if (
                not isinstance(target, dict)
                or set(target) != expected_fields
                or candidate is None
                or target["candidate_content_sha256"]
                != candidate["content_sha256"]
                or target["selection_disposition"]
                != selected["disposition"]
                or target["prescreen_evidence"]
                != selected.get("evidence")
                or target["comparison_observation_sha256"]
                != indexed["observation_sha256"]
                or target["history_statuses"]
                != indexed["statuses"]
            ):
                raise RuntimeContractError(
                    "round review plan target changed"
                )
            _, statuses = _validated_review_comparison(
                candidate=candidate,
                candidate_root=(
                    pathlib.Path(plan["artifact_root"])
                    / target["candidate_id"]
                ),
                index=index,
                indexed=indexed,
                conn=conn,
                policy=policy,
            )
            permanent = all(
                status in history_retrieval.PERMANENT_STATUSES
                for status in statuses
            )
            expected_outcome = (
                "history_abstain"
                if policy["mode"] == "enforcement"
                and not permanent
                else "prescreen_kill"
                if selected["disposition"] == "prescreen_kill"
                else "review"
            )
            if target["planned_outcome"] != expected_outcome:
                raise RuntimeContractError(
                    "round review plan outcome changed"
                )
            candidate_artifact = target["candidate_artifact"]
            _validate_review_candidate_artifact(
                candidate_artifact,
                candidate_descriptors[target["candidate_id"]],
                candidate,
            )
            if expected_outcome == "review":
                prior = target["prior_work"]
                block = prior_blocks.get(target["candidate_id"])
                expected_prior_raw = (
                    None if block is None else block.encode("utf-8")
                )
                if expected_prior_raw is None:
                    raise RuntimeContractError(
                        "review-plan prior work binding changed"
                    )
                prior_raw = _read_rooted_frozen_descriptor(
                    review_input_root,
                    prior,
                    f"{target['candidate_id']}/prior-work.md",
                    "review-plan prior work",
                    maximum=16384,
                    fields={"path", "sha256", "byte_count"},
                )
                if prior_raw != expected_prior_raw:
                    raise RuntimeContractError(
                        "review-plan prior work changed"
                    )
            elif target["prior_work"] is not None:
                raise RuntimeContractError(
                    "non-review target has prior work"
                )
            if policy["mode"] == "shadow":
                if (
                    target["gate_summary"] is not None
                    or target["mounted_history_summary"] is not None
                ):
                    raise RuntimeContractError(
                        "shadow review plan contains history summary"
                    )
            elif permanent:
                summary_descriptor = target["gate_summary"]
                summary_raw = _read_rooted_frozen_descriptor(
                    plan["artifact_root"],
                    summary_descriptor,
                    f"{target['candidate_id']}/history-summary.json",
                    "review-plan gate summary",
                    maximum=16384,
                    fields={"path", "sha256", "byte_count"},
                )
                try:
                    summary = json.loads(summary_raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise RuntimeContractError(
                        "review-plan gate summary is invalid"
                    ) from exc
                if summary_raw != canonical_bytes(summary):
                    raise RuntimeContractError(
                        "review-plan gate summary is invalid"
                    )
                verify_history_summary(
                    conn, candidate, summary, policy
                )
                should_mount = (
                    expected_outcome == "review"
                    and any(
                        item["status"] == "complete_match"
                        for item in summary["receipts"]
                    )
                )
                if (
                    should_mount
                    and target["mounted_history_summary"]
                    != summary_descriptor
                ) or (
                    not should_mount
                    and target["mounted_history_summary"] is not None
                ):
                    raise RuntimeContractError(
                        "review-plan mounted summary changed"
                    )
            elif (
                target["gate_summary"] is not None
                or target["mounted_history_summary"] is not None
            ):
                raise RuntimeContractError(
                    "history abstention contains a summary"
                )
    finally:
        if owns_connection:
            conn.close()
    outcome_map = {
        item["candidate_id"]: item["planned_outcome"]
        for item in targets
    }
    shortlist_order = _sealed_shortlist_order(
        batch, candidates, selection
    )
    expected_commit_order = [
        item["candidate_id"]
        for item in targets
        if item["planned_outcome"] == "prescreen_kill"
    ] + [
        candidate_id
        for candidate_id in shortlist_order
        if outcome_map[candidate_id] == "review"
    ]
    if plan["commit_order"] != expected_commit_order:
        raise RuntimeContractError(
            "round review plan commit order changed"
        )
    return plan


def _review_index_hash(index):
    material = dict(index)
    material.pop("review_index_sha256", None)
    domain = b"history-runtime-review-index-v1\0"
    if (
        index.get("schema_version") == 2
        and index.get("execution_boundary")
        == PORTABLE_EXECUTION_BOUNDARY
    ):
        domain = b"history-runtime-review-index-v2\0"
    return sha256(
        domain + canonical_bytes(material)
    )


def _validate_review_matrix_executor(
    *,
    executor,
    reviewer_commands,
    reviewer_request_profiles,
    reviewer_stage_runner,
):
    if executor == CONTAINED_EXECUTOR:
        if reviewer_stage_runner is not None:
            raise RuntimeContractError(
                "contained-v1 cannot use reviewer_stage_runner"
            )
        if reviewer_request_profiles is not None:
            raise RuntimeContractError(
                "contained-v1 cannot use reviewer_request_profiles"
            )
        if not reviewer_commands:
            raise RuntimeContractError(
                "contained-v1 requires reviewer_commands"
            )
        return
    if executor == PORTABLE_EXECUTOR:
        if reviewer_commands:
            raise RuntimeContractError(
                "portable-v2 cannot mix reviewer_commands"
            )
        if reviewer_request_profiles is None:
            raise RuntimeContractError(
                "portable-v2 requires reviewer_request_profiles"
            )
        _reviewer_profile_descriptors(
            reviewer_request_profiles
        )
        if (
            reviewer_stage_runner is not None
            and not callable(reviewer_stage_runner)
        ):
            raise RuntimeContractError(
                "portable reviewer_stage_runner is invalid"
            )
        return
    raise RuntimeContractError("review executor is invalid")


def _run_review_matrix(
    *,
    db_path,
    policy_path,
    batch_path,
    review_plan_path,
    reviewer_commands,
    executor=CONTAINED_EXECUTOR,
    reviewer_request_profiles=None,
    reviewer_stage_runner=None,
    stage_root,
    output_path,
    authority=None,
    test_review_verdict=None,
):
    _validate_review_matrix_executor(
        executor=executor,
        reviewer_commands=reviewer_commands,
        reviewer_request_profiles=reviewer_request_profiles,
        reviewer_stage_runner=reviewer_stage_runner,
    )
    _require_context_test_paths(
        (
            db_path,
            policy_path,
            batch_path,
            review_plan_path,
            stage_root,
            output_path,
        )
    )
    public_reference_root = None
    if executor == PORTABLE_EXECUTOR:
        public_reference_root = pathlib.Path(output_path).resolve().parent
        try:
            pathlib.Path(stage_root).resolve().relative_to(
                public_reference_root
            )
        except ValueError as exc:
            raise RuntimeContractError(
                "portable review stages are outside the index root"
            ) from exc
    policy = history_projection.load_policy(policy_path)
    if policy["mode"] == "enforcement":
        _validated_runtime_authority(
            policy,
            authority,
            state_paths=(stage_root, output_path),
        )
    plan = verify_round_review_plan(
        db_path=db_path,
        policy_path=policy_path,
        batch_path=batch_path,
        review_plan_path=review_plan_path,
        authority=authority,
    )
    if executor == CONTAINED_EXECUTOR:
        if plan["schema_version"] != 1:
            raise RuntimeContractError(
                "review plan execution boundary changed"
            )
        commands = _reviewer_command_descriptors(
            reviewer_commands
        )
        if [
            {
                "seat_id": item["seat_id"],
                "command_prefix_sha256":
                    item["command_prefix_sha256"],
            }
            for item in commands
        ] != plan["reviewer_seats"]:
            raise RuntimeContractError(
                "review command registry changed after plan seal"
            )
        command_map = {
            item["seat_id"]: item for item in commands
        }
        profile_map = None
    else:
        if (
            plan["schema_version"] != 2
            or plan.get("execution_boundary")
            != PORTABLE_EXECUTION_BOUNDARY
        ):
            raise RuntimeContractError(
                "review plan execution boundary changed"
            )
        profiles = _reviewer_profile_descriptors(
            reviewer_request_profiles
        )
        if profiles != plan["reviewer_seats"]:
            raise RuntimeContractError(
                "review request profile registry changed after plan seal"
            )
        profile_map = dict(reviewer_request_profiles)
        command_map = None
        if reviewer_stage_runner is None:
            reviewer_stage_runner = _run_portable_stage
    root = pathlib.Path(stage_root)
    _mkdir_single_use(root)
    entries = []
    for target in plan["targets"]:
        if target["planned_outcome"] != "review":
            continue
        candidate_id = target["candidate_id"]
        for seat in plan["reviewer_seats"]:
            seat_id = seat["seat_id"]
            invocation_root = (
                root / candidate_id / ("seat-" + seat_id)
            )
            inputs = {
                "candidate.json":
                    target["candidate_artifact"]["path"],
                "prior_work.md": target["prior_work"]["path"],
                "review_contract.md":
                    plan["review_contract"]["path"],
            }
            mounted_summary = target[
                "mounted_history_summary"
            ]
            if mounted_summary is not None:
                inputs["history_summary.json"] = (
                    mounted_summary["path"]
                )
            stage_seat_id = (
                f"review-{candidate_id}-seat-{seat_id}"
            )
            if executor == CONTAINED_EXECUTOR:
                prepared = _build_stage_manifest(
                    stage="review",
                    seat_id=stage_seat_id,
                    db_path=db_path,
                    policy_path=policy_path,
                    input_paths=inputs,
                    output_root=invocation_root / "output",
                    manifest_path=(
                        invocation_root / "manifest.json"
                    ),
                    command_json=json.dumps(
                        command_map[seat_id]["command_argv"],
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ),
                    authority=authority,
                    test_review_verdict=test_review_verdict,
                )
                run_contained_stage(
                    prepared, authority=authority
                )
            else:
                if test_review_verdict is not None:
                    raise RuntimeContractError(
                        "portable review cannot use test controls"
                    )
                prepared = reviewer_stage_runner(
                    request_profile=profile_map[seat_id],
                    stage="review",
                    seat_id=stage_seat_id,
                    input_paths=inputs,
                    invocation_root=invocation_root,
                    policy=policy,
                )
                verify_stage_completion(prepared)
            if executor == PORTABLE_EXECUTOR:
                entries.append(
                    {
                        "candidate_id": candidate_id,
                        "seat_id": seat_id,
                        "stage": _public_portable_stage(
                            prepared, public_reference_root
                        ),
                    }
                )
            else:
                completion_raw = _read_bound_regular(
                    prepared["completion_path"],
                    "contained review completion",
                    maximum=1024 * 1024,
                )
                entries.append(
                    {
                        "candidate_id": candidate_id,
                        "seat_id": seat_id,
                        "prepared": prepared,
                        "completion_sha256": sha256(
                            completion_raw
                        ),
                    }
                )
    result = {
        "schema_version": (
            1 if executor == CONTAINED_EXECUTOR else 2
        ),
        "review_plan_sha256": plan["review_plan_sha256"],
        "entries": entries,
    }
    if executor == PORTABLE_EXECUTOR:
        result["execution_boundary"] = (
            PORTABLE_EXECUTION_BOUNDARY
        )
    result["review_index_sha256"] = _review_index_hash(
        result
    )
    _publish_immutable(output_path, canonical_bytes(result))
    return result


def run_review_matrix(
    *,
    db_path,
    policy_path,
    batch_path,
    review_plan_path,
    reviewer_commands,
    executor=CONTAINED_EXECUTOR,
    reviewer_request_profiles=None,
    stage_root,
    output_path,
    authority=None,
):
    return _run_review_matrix(
        db_path=db_path,
        policy_path=policy_path,
        batch_path=batch_path,
        review_plan_path=review_plan_path,
        reviewer_commands=reviewer_commands,
        executor=executor,
        reviewer_request_profiles=reviewer_request_profiles,
        stage_root=stage_root,
        output_path=output_path,
        authority=authority,
    )


def _run_review_matrix_for_test(
    *,
    test_authority,
    test_state_root,
    test_review_verdict,
    **values,
):
    commands = _reviewer_command_descriptors(
        values["reviewer_commands"]
    )
    paths = (
        values["db_path"],
        values["policy_path"],
        values["batch_path"],
        values["review_plan_path"],
        values["stage_root"],
        values["output_path"],
    )
    _require_test_state_paths(test_state_root, paths)
    allowed = {
        (ROOT / "tests" / "fake_stage_agent.py").resolve(),
        (ROOT / "tests" / "malicious_history_agent.py").resolve(),
    }
    if any(
        pathlib.Path(item["command_argv"][0]).resolve()
        not in allowed
        for item in commands
    ):
        raise RuntimeContractError(
            "test review matrix backend is not registered"
        )
    policy = history_projection.load_policy(values["policy_path"])
    call_values = dict(values)
    supplied_authority = call_values.get("authority")
    if (
        supplied_authority is not None
        and supplied_authority is not test_authority
    ):
        raise RuntimeContractError(
            "test review authority changed"
        )
    if policy["mode"] == "enforcement":
        call_values["authority"] = test_authority
    with _runtime_for_test(
        policy,
        test_authority,
        test_state_root,
        state_paths=paths,
    ):
        return _run_review_matrix(
            test_review_verdict=test_review_verdict,
            **call_values,
        )


def _parse_review_ballot(raw, candidate_id):
    if (
        b"\x00" in raw
        or b"\r" in raw
        or not raw.endswith(b"\n")
        or raw.count(b"\n") != 1
    ):
        raise RuntimeContractError(
            "review ballot must be one physical TSV row"
        )
    try:
        fields = raw[:-1].decode("utf-8").split("\t")
    except UnicodeDecodeError as exc:
        raise RuntimeContractError(
            "review ballot is not UTF-8"
        ) from exc
    if (
        len(fields) != 4
        or fields[0] != candidate_id
        or fields[1]
        not in {"strong-accept", "accept-w-rev", "reject"}
        or not re.fullmatch(r"[0-9]+", fields[2])
        or not fields[3].strip()
    ):
        raise RuntimeContractError(
            "review ballot schema is invalid"
        )
    return {
        "candidate_id": fields[0],
        "verdict": fields[1],
        "major_count": int(fields[2]),
        "reason": fields[3],
    }


def _validate_compact_review(raw, ballot):
    if b"\x00" in raw or b"\r" in raw:
        raise RuntimeContractError(
            "review artifact contains invalid bytes"
        )
    try:
        lines = [
            line
            for line in raw.decode("utf-8").splitlines()
            if line.strip()
        ]
    except UnicodeDecodeError as exc:
        raise RuntimeContractError(
            "review artifact is not UTF-8"
        ) from exc
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
        or lines[0] != f"# {ballot['candidate_id']}"
    ):
        raise RuntimeContractError(
            "review artifact schema is invalid"
        )
    values = {}
    for line, label in zip(lines[1:], labels):
        prefix = label + ":"
        if not line.startswith(prefix):
            raise RuntimeContractError(
                "review artifact field order is invalid"
            )
        value = line[len(prefix):].strip()
        if not value or len(value.encode("utf-8")) > 4096:
            raise RuntimeContractError(
                "review artifact field is invalid"
            )
        values[label] = value
    if (
        values["Verdict"] != ballot["verdict"]
        or not values["CRITICAL"].isdigit()
        or not values["MAJOR"].isdigit()
        or int(values["MAJOR"]) != ballot["major_count"]
        or values["Reason"] != ballot["reason"]
        or (
            int(values["CRITICAL"]) > 0
            and ballot["verdict"] != "reject"
        )
        or (
            int(values["MAJOR"]) >= 2
            and ballot["verdict"] == "strong-accept"
        )
    ):
        raise RuntimeContractError(
            "review artifact and ballot differ"
        )
    return values


def _validated_review_outputs(prepared, candidate_id):
    ballot_raw = _read_bound_regular(
        prepared["output_paths"]["verdict.tsv"],
        "review matrix ballot",
        maximum=16384,
    )
    ballot = _parse_review_ballot(
        ballot_raw, candidate_id
    )
    review_raw = _read_bound_regular(
        prepared["output_paths"]["review.md"],
        "review matrix artifact",
        maximum=65536,
    )
    _validate_compact_review(review_raw, ballot)
    return ballot, ballot_raw, review_raw


def _review_entry_stage(index, entry, review_index_path):
    if index.get("schema_version") == 2:
        return _verified_public_portable_stage(
            entry["stage"],
            pathlib.Path(review_index_path).resolve().parent,
        )
    return entry["prepared"]


def verify_review_matrix(
    *,
    db_path,
    policy_path,
    batch_path,
    review_plan_path,
    review_index_path,
    authority=None,
    _connection=None,
):
    plan = verify_round_review_plan(
        db_path=db_path,
        policy_path=policy_path,
        batch_path=batch_path,
        review_plan_path=review_plan_path,
        authority=authority,
        _connection=_connection,
    )
    index = _load_canonical_json(
        review_index_path, "round review index"
    )
    portable = plan["schema_version"] == 2
    index_fields = {
        "schema_version",
        "review_plan_sha256",
        "entries",
        "review_index_sha256",
    }
    expected_index_version = 1
    if portable:
        index_fields.add("execution_boundary")
        expected_index_version = 2
    if (
        not isinstance(index, dict)
        or set(index) != index_fields
        or index.get("schema_version")
        != expected_index_version
        or (
            portable
            and index.get("execution_boundary")
            != PORTABLE_EXECUTION_BOUNDARY
        )
        or index.get("review_plan_sha256")
        != plan["review_plan_sha256"]
        or index.get("review_index_sha256")
        != _review_index_hash(index)
    ):
        raise RuntimeContractError(
            "round review index schema is invalid"
        )
    expected = [
        (target["candidate_id"], seat["seat_id"])
        for target in plan["targets"]
        if target["planned_outcome"] == "review"
        for seat in plan["reviewer_seats"]
    ]
    entries = index["entries"]
    if (
        not isinstance(entries, list)
        or [
            (
                item.get("candidate_id"),
                item.get("seat_id"),
            )
            for item in entries
            if isinstance(item, dict)
        ]
        != expected
    ):
        raise RuntimeContractError(
            "review matrix does not match its exact product"
        )
    target_map = {
        item["candidate_id"]: item
        for item in plan["targets"]
    }
    seat_map = {
        item["seat_id"]: item
        for item in plan["reviewer_seats"]
    }
    entry_fields = (
        {"candidate_id", "seat_id", "stage"}
        if portable
        else {
            "candidate_id",
            "seat_id",
            "prepared",
            "completion_sha256",
        }
    )
    public_reference_root = pathlib.Path(
        review_index_path
    ).resolve().parent
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != entry_fields
            or (
                not portable
                and not _valid_sha256(
                    entry.get("completion_sha256")
                )
            )
        ):
            raise RuntimeContractError(
                "review matrix entry is invalid"
        )
        target = target_map[entry["candidate_id"]]
        seat = seat_map[entry["seat_id"]]
        expected_inputs = {
            "candidate.json":
                target["candidate_artifact"]["sha256"],
            "prior_work.md": target["prior_work"]["sha256"],
            "review_contract.md":
                plan["review_contract"]["sha256"],
        }
        summary = target["mounted_history_summary"]
        if summary is not None:
            expected_inputs["history_summary.json"] = (
                summary["sha256"]
            )
        if portable:
            stage = entry["stage"]
            prepared = _verified_public_portable_stage(
                stage, public_reference_root
            )
            completion_raw = _read_bound_regular(
                prepared["completion_path"],
                "portable review completion",
                maximum=1024 * 1024,
            )
            preflight_raw = _read_bound_regular(
                prepared["preflight_path"],
                "portable review preflight",
                maximum=1024 * 1024,
            )
            try:
                preflight = json.loads(
                    preflight_raw.decode("utf-8")
                )
            except (UnicodeDecodeError, ValueError) as exc:
                raise RuntimeContractError(
                    "portable review preflight is invalid"
                ) from exc
            if (
                preflight_raw != canonical_bytes(preflight)
                or stage.get("execution_boundary")
                != PORTABLE_EXECUTION_BOUNDARY
                or stage.get("stage") != "review"
                or stage.get("seat_id")
                != (
                    f"review-{entry['candidate_id']}-"
                    f"seat-{entry['seat_id']}"
                )
                or preflight.get("stage") != "review"
                or preflight.get("seat_id")
                != stage.get("seat_id")
                or stage.get(
                    "execution_request_profile_hash"
                )
                != seat["execution_request_profile_hash"]
                or preflight.get(
                    "execution_request_profile_hash"
                )
                != seat["execution_request_profile_hash"]
                or preflight.get("input_sha256s")
                != expected_inputs
                or stage.get("input_sha256s")
                != expected_inputs
                or sha256(completion_raw)
                != stage.get("completion", {}).get("sha256")
            ):
                raise RuntimeContractError(
                    "portable review stage binding changed"
                )
            _validated_review_outputs(
                prepared, entry["candidate_id"]
            )
            continue
        prepared = entry["prepared"]
        verify_stage_completion(prepared)
        completion_raw = _read_bound_regular(
            prepared["completion_path"],
            "review matrix completion",
            maximum=1024 * 1024,
        )
        manifest = _load_canonical_json(
            prepared["manifest_path"],
            "review matrix manifest",
        )
        actual_inputs = {
            item.get("source"): item.get("sha256")
            for item in manifest.get("inputs", [])
            if isinstance(item, dict)
        }
        if (
            prepared.get("stage") != "review"
            or prepared.get("seat_id")
            != (
                f"review-{entry['candidate_id']}-"
                f"seat-{entry['seat_id']}"
            )
            or manifest.get("stage") != "review"
            or manifest.get("seat_id") != prepared["seat_id"]
            or prepared.get("command_prefix_sha256")
            != seat["command_prefix_sha256"]
            or actual_inputs != expected_inputs
            or len(manifest.get("inputs", []))
            != len(expected_inputs)
            or sha256(completion_raw)
            != entry["completion_sha256"]
        ):
            raise RuntimeContractError(
                "review matrix stage binding changed"
            )
        _validated_review_outputs(
            prepared, entry["candidate_id"]
        )
    return index


def _review_rank(verdict):
    return {
        "strong-accept": 2,
        "accept-w-rev": 1,
        "reject": 0,
    }[verdict]


def _round_aggregation_hash(aggregation):
    material = dict(aggregation)
    material.pop("aggregation_sha256", None)
    return sha256(
        b"history-runtime-round-aggregation-v1\0"
        + canonical_bytes(material)
    )


def _prior_work_facts(raw):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeContractError(
            "aggregation prior work is not UTF-8"
        ) from exc
    papers = re.findall(
        r"(?m)^Papers Read:[ \t]*([0-9]+)[ \t]*$", text
    )
    overlaps = re.findall(
        r"(?m)^Overlap:[ \t]*(high|medium|low)"
        r"(?:[ \t].*)?$",
        text,
    )
    if len(papers) != 1 or len(overlaps) != 1:
        raise RuntimeContractError(
            "aggregation prior-work facts are ambiguous"
        )
    return {
        "papers_read": int(papers[0]),
        "overlap": overlaps[0],
        "supported_cracks": len(
            re.findall(
                r"(?mi)Verification:[ \t]*supports\b",
                text,
            )
        ),
    }


def _aggregation_material(
    *,
    db_path,
    policy_path,
    batch_path,
    review_plan_path,
    review_index_path,
    authority=None,
    _connection=None,
):
    plan = verify_round_review_plan(
        db_path=db_path,
        policy_path=policy_path,
        batch_path=batch_path,
        review_plan_path=review_plan_path,
        authority=authority,
        _connection=_connection,
    )
    index = verify_review_matrix(
        db_path=db_path,
        policy_path=policy_path,
        batch_path=batch_path,
        review_plan_path=review_plan_path,
        review_index_path=review_index_path,
        authority=authority,
        _connection=_connection,
    )
    _, candidates = _load_batch_candidates(batch_path)
    entry_map = {}
    for entry in index["entries"]:
        entry_map[
            (entry["candidate_id"], entry["seat_id"])
        ] = entry
    rows = []
    near_sa = []
    target_results = {}
    plan_target_map = {
        item["candidate_id"]: item
        for item in plan["targets"]
    }
    processing_order = list(plan["commit_order"]) + [
        item["candidate_id"]
        for item in plan["targets"]
        if item["planned_outcome"] == "history_abstain"
    ]
    owns_connection = _connection is None
    conn = (
        _connect_history_store(db_path)
        if owns_connection
        else _connection
    )
    if owns_connection:
        history_store.init_schema(conn)
    try:
        for candidate_id in processing_order:
            target = plan_target_map[candidate_id]
            candidate = candidates[candidate_id]
            outcome = target["planned_outcome"]
            target_result = {
                "candidate_id": candidate_id,
                "planned_outcome": outcome,
                "review_completion_ids": [],
                "ballot_sha256s": [],
                "vote_vector": None,
                "row_index": None,
                "row_sha256": None,
                "near_sa_observation": None,
            }
            if outcome == "history_abstain":
                target_results[candidate_id] = target_result
                continue
            if outcome == "prescreen_kill":
                evidence = target["prescreen_evidence"]
                if not isinstance(evidence, str) or not evidence:
                    raise RuntimeContractError(
                        "prescreen row lacks sealed evidence"
                    )
                row = "\t".join(
                    (
                        plan["gate_config"]["round_date"],
                        "hunt",
                        candidate["theme"],
                        candidate["story"],
                        "reject",
                        "Prescreen direct hit: " + evidence,
                        "high",
                        "novelty-dead",
                    )
                )
                target_result["vote_vector"] = "-"
            else:
                ballots = []
                for seat in plan["reviewer_seats"]:
                    entry = entry_map[
                        (candidate_id, seat["seat_id"])
                    ]
                    prepared = _review_entry_stage(
                        index, entry, review_index_path
                    )
                    ballot, ballot_raw, _ = (
                        _validated_review_outputs(
                            prepared, candidate_id
                        )
                    )
                    rank = _review_rank(ballot["verdict"])
                    major = ballot["major_count"]
                    effective_rank = (
                        1
                        if rank == 2 and major >= 2
                        else rank
                    )
                    completion = _load_canonical_json(
                        prepared["completion_path"],
                        "aggregation review completion",
                    )
                    ballots.append(
                        {
                            "rank": effective_rank,
                            "reason": ballot["reason"],
                            "sha256": sha256(ballot_raw),
                            "completion_id":
                                completion["completion_id"],
                        }
                    )
                effective = [item["rank"] for item in ballots]
                raw_min = min(effective)
                sa_votes = sum(value == 2 for value in effective)
                reason = next(
                    item["reason"]
                    for item in ballots
                    if item["rank"] == raw_min
                )
                prior_raw = _read_bound_regular(
                    target["prior_work"]["path"],
                    "aggregation prior work",
                    maximum=16384,
                )
                facts = _prior_work_facts(prior_raw)
                markdown = candidate["candidate_markdown"]
                falsification = re.findall(
                    r"(?m)^Minimal Falsification Experiment:"
                    r"[ \t]*(\S.*)$",
                    markdown,
                )
                axiom = bool(
                    re.search(
                        r"(?mi)^Form:[ \t]*"
                        r"remove-load-bearing-assumption[ \t]*$",
                        markdown,
                    )
                )
                mechanical_gate = (
                    facts["papers_read"]
                    >= plan["gate_config"]["min_read"]
                    and len(falsification) == 1
                    and len(
                        falsification[0].encode("utf-8")
                    )
                    >= 30
                    and (
                        not axiom
                        or facts["supported_cracks"]
                        >= plan["gate_config"][
                            "axiom_min_cracks"
                        ]
                    )
                )
                downgraded = raw_min == 2 and not mechanical_gate
                final_rank = 0 if downgraded else raw_min
                if downgraded:
                    reason = (
                        "Unanimous SA failed a mechanical gate: "
                        "papers read < "
                        f"{plan['gate_config']['min_read']}, "
                        "missing research block, missing "
                        "falsification experiment, incomplete "
                        "review, or insufficient supported crack "
                        "evidence"
                    )
                verdict = {
                    2: "strong-accept",
                    1: "accept-w-rev",
                    0: "reject",
                }[final_rank]
                overlap = facts["overlap"]
                if final_rank == 2:
                    category = "-"
                elif downgraded:
                    category = "evidence-incomplete"
                elif overlap == "high":
                    category = "novelty-dead"
                elif raw_min == 1 and overlap == "low":
                    category = "design-fixable"
                elif raw_min == 1:
                    category = "ceiling-limited"
                else:
                    category = "novelty-dead"
                row = "\t".join(
                    (
                        plan["gate_config"]["round_date"],
                        "hunt",
                        candidate["theme"],
                        candidate["story"],
                        verdict,
                        reason,
                        overlap,
                        category,
                    )
                )
                target_result[
                    "review_completion_ids"
                ] = [
                    item["completion_id"] for item in ballots
                ]
                target_result["ballot_sha256s"] = [
                    item["sha256"] for item in ballots
                ]
                target_result["vote_vector"] = ",".join(
                    str(value) for value in effective
                )
                existing_count = conn.execute(
                    "SELECT count(*) FROM candidates WHERE story = ?",
                    (candidate["story"],),
                ).fetchone()[0]
                earlier_count = sum(
                    row.split("\t")[3] == candidate["story"]
                    for row in rows
                )
                story_count_after_append = (
                    existing_count + earlier_count + 1
                )
                if (
                    final_rank != 2
                    and category
                    in {"design-fixable", "evidence-incomplete"}
                    and sa_votes >= 1
                    and story_count_after_append < 2
                ):
                    observation = {
                        "row_index": len(rows),
                        "sa_votes": sa_votes,
                        "vote_vector":
                            target_result["vote_vector"],
                        "overlap": overlap,
                        "category": category,
                        "reason": (
                            plan["review_plan_sha256"]
                            + "/"
                            + candidate_id
                        ),
                        "observed_at":
                            plan["gate_config"]["round_date"],
                    }
                    target_result[
                        "near_sa_observation"
                    ] = observation
                    near_sa.append(observation)
            target_result["row_index"] = len(rows)
            target_result["row_sha256"] = sha256(
                row.encode("utf-8")
            )
            rows.append(row)
            target_results[candidate_id] = target_result
    finally:
        if owns_connection:
            conn.close()
    return {
        "schema_version": 1,
        "review_plan_sha256": plan["review_plan_sha256"],
        "review_index_sha256": index["review_index_sha256"],
        "targets": [
            target_results[item["candidate_id"]]
            for item in plan["targets"]
        ],
        "ledger_rows": rows,
        "near_sa_observations": near_sa,
    }


def build_round_aggregation(
    *,
    output_path,
    **values,
):
    policy = history_projection.load_policy(
        values["policy_path"]
    )
    if policy["mode"] == "enforcement":
        _validated_runtime_authority(
            policy,
            values.get("authority"),
            state_paths=(output_path,),
        )
    result = _aggregation_material(**values)
    result["aggregation_sha256"] = (
        _round_aggregation_hash(result)
    )
    _publish_immutable(output_path, canonical_bytes(result))
    return result


def verify_round_aggregation(
    *,
    aggregation_path,
    **values,
):
    aggregation = _load_canonical_json(
        aggregation_path, "round aggregation"
    )
    if (
        not isinstance(aggregation, dict)
        or set(aggregation)
        != {
            "schema_version",
            "review_plan_sha256",
            "review_index_sha256",
            "targets",
            "ledger_rows",
            "near_sa_observations",
            "aggregation_sha256",
        }
        or aggregation.get("schema_version") != 1
        or aggregation.get("aggregation_sha256")
        != _round_aggregation_hash(aggregation)
    ):
        raise RuntimeContractError(
            "round aggregation schema is invalid"
        )
    expected = _aggregation_material(**values)
    material = dict(aggregation)
    material.pop("aggregation_sha256")
    if material != expected:
        raise RuntimeContractError(
            "round aggregation inputs changed"
        )
    return aggregation


def _verify_materialized_research_view(
    *,
    research_view_path,
    batch,
    candidates,
    plan,
):
    research = _load_canonical_json(
        research_view_path, "materialized research view"
    )
    fields = {
        "schema_version",
        "mode",
        "runtime_authority",
        "policy",
        "history_store",
        "batch",
        "selection",
        "comparison_index",
        "summary_index",
        "artifact_root",
        "output_root",
        "shortlist_order",
        "eligible_order",
        "abstentions",
        "views",
        "summaries",
        "research_view_sha256",
    }
    material = dict(research)
    research_sha = material.pop(
        "research_view_sha256", None
    )
    if (
        not isinstance(research, dict)
        or set(research) != fields
        or type(research.get("schema_version")) is not int
        or research["schema_version"] != 1
        or research_sha
        != sha256(
            b"history-runtime-research-view-v1\0"
            + canonical_bytes(material)
        )
        or research["mode"] != plan["policy_mode"]
        or research["batch"].get("sha256")
        != batch["batch_sha256"]
        or research["selection"].get("sha256")
        != plan["selection_sha256"]
        or research["comparison_index"].get("sha256")
        != plan["comparison_index_sha256"]
        or pathlib.Path(
            research["artifact_root"]
        ).resolve()
        != pathlib.Path(plan["artifact_root"]).resolve()
    ):
        raise RuntimeContractError(
            "report research view binding changed"
        )
    outcome_map = {
        item["candidate_id"]: item["planned_outcome"]
        for item in plan["targets"]
    }
    expected_eligible = [
        candidate_id
        for candidate_id in plan["commit_order"]
        if outcome_map[candidate_id] == "review"
    ]
    expected_abstentions = [
        {
            "candidate_id": item["candidate_id"],
            "statuses": item["history_statuses"],
        }
        for item in plan["targets"]
        if item["planned_outcome"] == "history_abstain"
    ]
    expected_summaries = [
        item
        for item in plan["targets"]
        if item["mounted_history_summary"] is not None
    ]
    if (
        research["eligible_order"] != expected_eligible
        or research["abstentions"] != expected_abstentions
        or [
            item.get("candidate_id")
            for item in research["summaries"]
        ]
        != [
            item["candidate_id"]
            for item in expected_summaries
        ]
    ):
        raise RuntimeContractError(
            "report research eligibility changed"
        )
    output_root = pathlib.Path(
        research["output_root"]
    ).resolve()
    if (
        pathlib.Path(research_view_path).resolve()
        != output_root / "research-view.json"
    ):
        raise RuntimeContractError(
            "report research view path changed"
        )
    expected_tsv, expected_markdown = (
        _frozen_candidate_view_bytes(
            batch, candidates, expected_eligible
        )
    )
    views = research["views"]
    expected_views = {
        "ideas.tsv": expected_tsv,
        "ideas.md": expected_markdown,
    }
    if not isinstance(views, dict) or set(views) != set(
        expected_views
    ):
        raise RuntimeContractError(
            "report research source views are invalid"
        )
    for name, expected_raw in expected_views.items():
        descriptor = views[name]
        expected_path = output_root / name
        raw = _read_bound_regular(
            expected_path,
            "report research source view",
            maximum=1024 * 1024,
            allow_empty=True,
        )
        if (
            not isinstance(descriptor, dict)
            or set(descriptor)
            != {"path", "sha256", "byte_count"}
            or pathlib.Path(
                descriptor["path"]
            ).resolve()
            != expected_path
            or raw != expected_raw
            or descriptor["sha256"] != sha256(raw)
            or descriptor["byte_count"] != len(raw)
        ):
            raise RuntimeContractError(
                "report research source view changed"
            )
    for descriptor, target in zip(
        research["summaries"], expected_summaries
    ):
        expected_source = target["mounted_history_summary"]
        destination = (
            output_root
            / "history-summaries"
            / (target["candidate_id"] + ".json")
        )
        raw = _read_bound_regular(
            destination,
            "report mounted history summary",
            maximum=16384,
        )
        if (
            not isinstance(descriptor, dict)
            or set(descriptor)
            != {
                "candidate_id",
                "source_path",
                "source_sha256",
                "path",
                "sha256",
                "byte_count",
            }
            or pathlib.Path(
                descriptor["source_path"]
            ).resolve()
            != pathlib.Path(
                expected_source["path"]
            ).resolve()
            or descriptor["source_sha256"]
            != expected_source["sha256"]
            or pathlib.Path(descriptor["path"]).resolve()
            != destination
            or descriptor["sha256"] != sha256(raw)
            or descriptor["sha256"]
            != expected_source["sha256"]
            or descriptor["byte_count"] != len(raw)
        ):
            raise RuntimeContractError(
                "report mounted history summary changed"
            )
    if (
        expected_summaries
        and not isinstance(research["summary_index"], dict)
    ) or (
        not expected_summaries
        and research["summary_index"] is not None
    ):
        raise RuntimeContractError(
            "report research summary index changed"
        )
    return research


def materialize_report_views(
    *,
    db_path,
    policy_path,
    batch_path,
    research_view_path,
    review_plan_path,
    review_index_path,
    aggregation_path,
    output_root,
    round_number,
    authority=None,
):
    if type(round_number) is not int or round_number < 1:
        raise RuntimeContractError(
            "report view round number is invalid"
        )
    policy = history_projection.load_policy(policy_path)
    if policy["mode"] == "enforcement":
        _validated_runtime_authority(
            policy, authority, state_paths=(output_root,)
        )
    plan = verify_round_review_plan(
        db_path=db_path,
        policy_path=policy_path,
        batch_path=batch_path,
        review_plan_path=review_plan_path,
        authority=authority,
    )
    batch, candidates = _load_batch_candidates(batch_path)
    research = _verify_materialized_research_view(
        research_view_path=research_view_path,
        batch=batch,
        candidates=candidates,
        plan=plan,
    )
    index = verify_review_matrix(
        db_path=db_path,
        policy_path=policy_path,
        batch_path=batch_path,
        review_plan_path=review_plan_path,
        review_index_path=review_index_path,
        authority=authority,
    )
    aggregation = verify_round_aggregation(
        db_path=db_path,
        policy_path=policy_path,
        batch_path=batch_path,
        review_plan_path=review_plan_path,
        review_index_path=review_index_path,
        aggregation_path=aggregation_path,
        authority=authority,
    )
    plan_targets = {
        item["candidate_id"]: item
        for item in plan["targets"]
    }
    accepted = []
    rejected = []
    accepted_ids = []
    for result in aggregation["targets"]:
        target = plan_targets[result["candidate_id"]]
        candidate = candidates[result["candidate_id"]]
        outcome = target["planned_outcome"]
        if outcome == "history_abstain":
            if result["row_index"] is not None:
                raise RuntimeContractError(
                    "history abstention unexpectedly has a ledger row"
                )
            statuses = target["history_statuses"]
            if (
                not isinstance(statuses, list)
                or not statuses
                or any(not isinstance(status, str) for status in statuses)
            ):
                raise RuntimeContractError(
                    "history abstention lacks sealed statuses"
                )
            rejected.append(
                result["candidate_id"]
                + "\t"
                + candidate["story"]
                + "\tHistory abstention: "
                + ",".join(statuses)
                + "\n"
            )
            continue
        if (
            outcome not in {"review", "prescreen_kill"}
            or result["row_index"] is None
        ):
            raise RuntimeContractError(
                "report view target lacks its sealed outcome"
            )
        fields = aggregation["ledger_rows"][
            result["row_index"]
        ].split("\t")
        if len(fields) != 8:
            raise RuntimeContractError(
                "report view ledger row is invalid"
            )
        candidate = candidates[result["candidate_id"]]
        if (
            fields[2] != candidate["theme"]
            or fields[3] != candidate["story"]
        ):
            raise RuntimeContractError(
                "report view row changed candidate identity"
            )
        if fields[4] == "strong-accept":
            accepted_ids.append(result["candidate_id"])
            accepted.append(
                result["candidate_id"]
                + "\t"
                + candidate["story"]
                + "\n"
            )
        else:
            rejected.append(
                result["candidate_id"]
                + "\t"
                + candidate["story"]
                + "\t"
                + fields[5]
                + "\n"
            )
    ideas_tsv, ideas_markdown = _frozen_candidate_view_bytes(
        batch, candidates, accepted_ids
    )
    del ideas_tsv
    prior_descriptor = plan["prior_work_source"]
    prior_raw = _read_bound_regular(
        prior_descriptor["path"],
        "report view prior work",
        maximum=1024 * 1024,
        allow_empty=True,
    )
    if (
        sha256(prior_raw) != prior_descriptor["sha256"]
        or len(prior_raw) != prior_descriptor["byte_count"]
    ):
        raise RuntimeContractError(
            "report view prior work changed"
        )
    entry_map = {
        (entry["candidate_id"], entry["seat_id"]): entry
        for entry in index["entries"]
    }
    reviewer_one = []
    for candidate_id in accepted_ids:
        entry = entry_map.get((candidate_id, "1"))
        if entry is None:
            raise RuntimeContractError(
                "report view lacks reviewer seat 1"
            )
        prepared = _review_entry_stage(
            index, entry, review_index_path
        )
        _, _, review_raw = _validated_review_outputs(
            prepared, candidate_id
        )
        try:
            review = review_raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeContractError(
                "report view review is not UTF-8"
            ) from exc
        lines = review.splitlines()
        if not lines or lines[0] != "# " + candidate_id:
            raise RuntimeContractError(
                "report view review heading is invalid"
            )
        lines[0] = "## " + candidate_id
        reviewer_one.append(
            "\n".join(lines).rstrip() + "\n"
        )
    output = pathlib.Path(output_root)
    _mkdir_single_use(output)
    reviewer_root = output / "rev" / "1"
    reviewer_descriptor = _open_safe_directory(
        reviewer_root, create=True
    )
    os.close(reviewer_descriptor)
    raw_views = {
        "accepted.tsv": "".join(accepted).encode("utf-8"),
        "rejects.tsv": "".join(rejected).encode("utf-8"),
        "ideas.md": ideas_markdown,
        "priorwork.md": prior_raw,
        "meta.txt": (
            f"Rounds Attempted: {round_number}\n"
            f"Review Date: "
            f"{plan['gate_config']['round_date']}\n"
            f"Reviewers: {len(plan['reviewer_seats'])}\n"
        ).encode("utf-8"),
        "rev/1/review.md": "\n".join(
            reviewer_one
        ).encode("utf-8"),
    }
    publications = {}
    for relative, raw in raw_views.items():
        destination = output / relative
        _publish_immutable(destination, raw)
        publications[relative] = {
            "path": str(destination),
            "sha256": sha256(raw),
            "byte_count": len(raw),
        }
    result = {
        "schema_version": 1,
        "research_view_sha256":
            research["research_view_sha256"],
        "review_plan_sha256": plan["review_plan_sha256"],
        "review_index_sha256": index["review_index_sha256"],
        "aggregation_sha256":
            aggregation["aggregation_sha256"],
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "views": publications,
    }
    result["report_view_sha256"] = sha256(
        b"history-runtime-report-view-v1\0"
        + canonical_bytes(result)
    )
    _publish_immutable(
        output / "report-view.json",
        canonical_bytes(result),
    )
    return result


def commit_round(
    *,
    db_path,
    policy_path,
    batch_path,
    selection_path,
    comparison_index_path,
    review_plan_path,
    review_index_path,
    aggregation_path,
    authority,
):
    policy = history_projection.load_policy(policy_path)
    authority_value = _validated_runtime_authority(
        policy, authority, state_paths=(db_path,)
    )
    batch, candidates = _load_batch_candidates(batch_path)
    selection = verify_round_selection(selection_path)
    comparison = _comparison_index(
        comparison_index_path, selection
    )
    plan = _load_canonical_json(
        review_plan_path, "round review plan"
    )
    review_index = _load_canonical_json(
        review_index_path, "round review index"
    )
    aggregation = _load_canonical_json(
        aggregation_path, "round aggregation"
    )
    portable_round = (
        plan.get("schema_version") == 2
        and plan.get("execution_boundary")
        == PORTABLE_EXECUTION_BOUNDARY
    )
    if (
        plan.get("review_plan_sha256")
        != _review_plan_hash(plan)
        or review_index.get("review_index_sha256")
        != _review_index_hash(review_index)
        or aggregation.get("aggregation_sha256")
        != _round_aggregation_hash(aggregation)
        or plan.get("batch_sha256") != batch["batch_sha256"]
        or plan.get("selection_sha256")
        != selection["selection_sha256"]
        or plan.get("comparison_index_sha256")
        != comparison["comparison_index_sha256"]
        or review_index.get("review_plan_sha256")
        != plan["review_plan_sha256"]
        or aggregation.get("review_plan_sha256")
        != plan["review_plan_sha256"]
        or aggregation.get("review_index_sha256")
        != review_index["review_index_sha256"]
        or [
            item.get("candidate_id")
            for item in plan.get("targets", [])
        ]
        != [
            item["candidate_id"]
            for item in selection["targets"]
        ]
        or [
            item.get("candidate_id")
            for item in aggregation.get("targets", [])
        ]
        != [
            item["candidate_id"]
            for item in selection["targets"]
        ]
    ):
        raise RuntimeContractError(
            "round commit sealed roots differ"
        )
    request_targets = []
    for planned, aggregated in zip(
        plan["targets"], aggregation["targets"]
    ):
        candidate = candidates[planned["candidate_id"]]
        if (
            planned["planned_outcome"]
            != aggregated["planned_outcome"]
        ):
            raise RuntimeContractError(
                "round commit target outcome differs"
            )
        request_targets.append(
            {
                "candidate_id": planned["candidate_id"],
                "candidate_content_sha256":
                    candidate["content_sha256"],
                "planned_outcome":
                    planned["planned_outcome"],
                "row_sha256": aggregated["row_sha256"],
                "review_completion_ids":
                    aggregated["review_completion_ids"],
            }
        )
    request = {
        "schema_version": 2 if portable_round else 1,
        "batch_sha256": batch["batch_sha256"],
        "selection_sha256": selection["selection_sha256"],
        "comparison_index_sha256":
            comparison["comparison_index_sha256"],
        "review_plan_sha256": plan["review_plan_sha256"],
        "review_index_sha256":
            review_index["review_index_sha256"],
        "aggregation_sha256":
            aggregation["aggregation_sha256"],
        "policy_mode": policy["mode"],
        "policy_sha256": authority_value["policy_sha256"],
        "capability_sha256":
            authority_value["capability_sha256"],
        "trust_root_sha256":
            authority_value["trust_root_sha256"],
        "targets": request_targets,
    }
    if portable_round:
        request["execution_boundary"] = (
            PORTABLE_EXECUTION_BOUNDARY
        )
    request_json = canonical_bytes(request).decode(
        "utf-8"
    ).rstrip("\n")
    request_sha = sha256(
        (
            b"history-round-commit-request-v2\0"
            if portable_round
            else b"history-round-commit-request-v1\0"
        )
        + canonical_bytes(request)
    )
    commit_key = (
        ("hunt-round-v2:" if portable_round else "hunt-round-v1:")
        + selection["selection_sha256"]
    )
    conn = _connect_history_store(db_path)
    history_store.init_schema(conn)
    try:
        try:
            prior = history_store.lookup_round_commit(
                conn,
                commit_key=commit_key,
                request_sha256=request_sha,
                request_json=request_json,
            )
        except history_store.HistoryStoreError as exc:
            raise RuntimeContractError(str(exc)) from exc
        if prior is not None:
            return prior
        def validate_precommit(locked_connection):
            verified = verify_round_aggregation(
                db_path=db_path,
                policy_path=policy_path,
                batch_path=batch_path,
                review_plan_path=review_plan_path,
                review_index_path=review_index_path,
                aggregation_path=aggregation_path,
                authority=authority,
                _connection=locked_connection,
            )
            if verified != aggregation:
                raise RuntimeContractError(
                    "round aggregation verification changed"
                )
        try:
            commit_metadata = {
                "source": "hunt-runtime-v2",
                "selection_sha256":
                    selection["selection_sha256"],
                "review_plan_sha256":
                    plan["review_plan_sha256"],
                "review_index_sha256":
                    review_index["review_index_sha256"],
                "aggregation_sha256":
                    aggregation["aggregation_sha256"],
                "policy_mode": policy["mode"],
                "policy_sha256":
                    authority_value["policy_sha256"],
                "capability_sha256":
                    authority_value["capability_sha256"],
                "trust_root_sha256":
                    authority_value["trust_root_sha256"],
            }
            if portable_round:
                commit_metadata["execution_boundary"] = (
                    PORTABLE_EXECUTION_BOUNDARY
                )
            return history_store.append_rows_idempotent(
                conn,
                aggregation["ledger_rows"],
                commit_metadata,
                commit_key=commit_key,
                request_sha256=request_sha,
                request_json=request_json,
                near_sa_observations=aggregation[
                    "near_sa_observations"
                ],
                precommit_validator=validate_precommit,
            )
        except history_store.HistoryStoreError as exc:
            raise RuntimeContractError(str(exc)) from exc
    finally:
        conn.close()


def _expected_comparator_invocation_root(
    candidate_root, stage_number, intent, pack, execution_boundary
):
    publication = pack.get("pack_publication_id")
    identity = (
        publication
        if isinstance(publication, str) and publication
        else sha256(canonical_bytes(pack))
    )
    stage_directory = (
        "portable-comparisons"
        if execution_boundary == PORTABLE_EXECUTION_BOUNDARY
        else "contained-comparisons"
    )
    return (
        pathlib.Path(candidate_root)
        / stage_directory
        / f"{stage_number:02d}-{intent}-{identity[:16]}"
    )


def _validate_resume_comparator_stages(
    *,
    candidate,
    candidate_root,
    observation,
    stage_records,
    conn,
    policy,
    allow_unbindable=False,
    execution_boundary=CONTAINED_EXECUTOR,
):
    if execution_boundary not in {
        CONTAINED_EXECUTOR,
        PORTABLE_EXECUTION_BOUNDARY,
    }:
        raise RuntimeContractError(
            "resume comparison execution boundary is invalid"
        )
    expected = []
    resume_bindings = []
    build_items = {}
    if allow_unbindable:
        build_observation = _load_canonical_json(
            pathlib.Path(candidate_root)
            / "build-observation.json",
            "research build observation",
        )
        build_items = {
            item["intent"]: (item, pack)
            for item, pack in _validated_build_observation(
                candidate,
                pathlib.Path(candidate_root),
                build_observation,
            )
        }
    item_fields = {
        "intent",
        "retrieval_status",
        "status",
        "pack_path",
        "comparison_path",
        "receipt_path",
        "attempts",
    }
    attempt_fields = {
        "pack_path",
        "comparison_path",
        "receipt_path",
        "status",
    }
    items = observation.get("observations")
    intents = required_intents(candidate)
    if (
        not isinstance(items, list)
        or [item.get("intent") for item in items] != intents
    ):
        raise RuntimeContractError(
            "resume comparison intent coverage is invalid"
        )
    for item, intent in zip(items, intents):
        attempts = item.get("attempts")
        final_binding = None
        if (
            not isinstance(item, dict)
            or set(item) != item_fields
            or not isinstance(attempts, list)
            or not attempts
            or len(attempts)
            > 1 + int(policy["max_expansion_rounds"])
        ):
            raise RuntimeContractError(
                "resume comparison attempts are invalid"
            )
        final_retrieval_status = None
        prior_pack_publication_id = None
        prior_comparison_receipt_id = None
        for attempt_index, attempt in enumerate(attempts):
            intent_root = pathlib.Path(candidate_root) / intent
            expected_pack_path = _comparison_attempt_path(
                intent_root,
                "retrieval-pack",
                attempt_index,
            )
            if (
                not isinstance(attempt, dict)
                or set(attempt) != attempt_fields
                or not isinstance(attempt.get("pack_path"), str)
                or pathlib.Path(
                    attempt["pack_path"]
                ).resolve()
                != expected_pack_path.resolve()
            ):
                raise RuntimeContractError(
                    "resume comparison attempt path is invalid"
                )
            pack_raw = _read_bound_regular(
                expected_pack_path,
                "resume comparison pack",
                maximum=65536,
            )
            try:
                pack = json.loads(pack_raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise RuntimeContractError(
                    "resume comparison pack is invalid"
                ) from exc
            if (
                pack_raw != canonical_bytes(pack)
                or pack.get("intent") != intent
                or not _pack_matches_candidate(pack, candidate)
                or (
                    "expansion_round" in pack
                    and (
                        pack.get("expansion_round")
                        != attempt_index
                        or pack.get(
                            "prior_pack_publication_id"
                        )
                        != prior_pack_publication_id
                        or pack.get(
                            "prior_comparison_receipt_id"
                        )
                        != prior_comparison_receipt_id
                    )
                )
                or (
                    "expansion_round" not in pack
                    and attempt_index != 0
                )
            ):
                raise RuntimeContractError(
                    "resume comparison pack binding changed"
                )
            final_retrieval_status = pack.get(
                "retrieval_status"
            )
            comparison_path = attempt["comparison_path"]
            receipt_path = attempt["receipt_path"]
            both_strings = (
                isinstance(comparison_path, str)
                and isinstance(receipt_path, str)
            )
            both_none = (
                comparison_path is None
                and receipt_path is None
            )
            if not (both_strings or both_none):
                raise RuntimeContractError(
                    "resume comparison attempt pair is invalid"
                )
            host_validated = True
            try:
                history_retrieval._validate_pack(
                    conn,
                    pack,
                    policy,
                    require_complete=both_strings,
                )
            except history_retrieval.RetrievalError as exc:
                built = build_items.get(intent)
                initial_unbindable = (
                    attempt_index == 0
                    and len(attempts) == 1
                    and built is not None
                    and item == built[0]
                    and pack == built[1]
                )
                if (
                    not allow_unbindable
                    or both_strings
                    or not initial_unbindable
                ):
                    raise RuntimeContractError(
                        "resume pack is not host-validated"
                    ) from exc
                host_validated = False
            publication = None
            if host_validated:
                publication = conn.execute(
                    """
                    SELECT comparator_preflight_sha256
                    FROM history_pack_publications
                    WHERE publication_id = ?
                    """,
                    (pack["pack_publication_id"],),
                ).fetchone()
                if (
                    publication is None
                    or not _valid_sha256(
                        publication[
                            "comparator_preflight_sha256"
                        ]
                    )
                ):
                    raise RuntimeContractError(
                        "resume comparator preflight is unavailable"
                    )
            receipt = None
            if both_strings:
                expected_comparison = _comparison_attempt_path(
                    intent_root,
                    "history-comparison",
                    attempt_index,
                )
                expected_receipt = _comparison_attempt_path(
                    intent_root,
                    "history-receipt",
                    attempt_index,
                )
                if (
                    pathlib.Path(comparison_path).resolve()
                    != expected_comparison.resolve()
                    or pathlib.Path(receipt_path).resolve()
                    != expected_receipt.resolve()
                    or pack.get("retrieval_status") != "complete"
                ):
                    raise RuntimeContractError(
                        "resume compared attempt path is invalid"
                    )
                comparison = _load_canonical_json(
                    comparison_path,
                    "resume history comparison",
                )
                receipt = _load_canonical_json(
                    receipt_path,
                    "resume comparison receipt",
                )
                replayed = history_retrieval.replay_receipt(
                    conn, pack, receipt, policy
                )
                current_prompt_sha256 = sha256(
                    _portable_serialized_prompt(
                        "history-compare",
                        {"retrieval_pack.json": expected_pack_path},
                        policy,
                    ).encode("utf-8")
                )
                comparison_sha256 = sha256(
                    canonical_bytes(comparison)
                )
                receipt_sha256 = sha256(canonical_bytes(receipt))
                if (
                    replayed["verified"] is not True
                    or receipt.get("pack_sha256")
                    != pack.get("pack_sha256")
                    or receipt.get("comparison_sha256")
                    != comparison_sha256
                    or receipt.get("comparator_invocation_sha256")
                    != current_prompt_sha256
                    or receipt.get("status") != attempt["status"]
                ):
                    raise RuntimeContractError(
                        "resume comparison receipt changed"
                    )
                expected.append(
                    {
                        "intent": intent,
                        "pack": pack,
                        "input_sha256": sha256(pack_raw),
                        "pack_sha256": pack.get("pack_sha256"),
                        "comparison_sha256": comparison_sha256,
                        "receipt_sha256": receipt_sha256,
                        "serialized_prompt_sha256":
                            current_prompt_sha256,
                        "receipt": receipt,
                    }
                )
                prior_pack_publication_id = pack.get(
                    "pack_publication_id"
                )
                prior_comparison_receipt_id = receipt.get(
                    "receipt_id"
                )
            elif (
                pack.get("retrieval_status") == "complete"
                or attempt.get("status")
                != pack.get("retrieval_status")
            ):
                raise RuntimeContractError(
                    "resume noncompared attempt is invalid"
                )
            if host_validated:
                final_binding = {
                    "intent": intent,
                    "status": attempt["status"],
                    "pack": pack,
                    "comparator_version": (
                        receipt["comparator_version"]
                        if receipt is not None
                        else history_retrieval.COMPARATOR_VERSION
                    ),
                    "preflight_sha256": (
                        receipt[
                            "comparator_preflight_sha256"
                        ]
                        if receipt is not None
                        else publication[
                            "comparator_preflight_sha256"
                        ]
                    ),
                    "comparison_sha256": (
                        None
                        if receipt is None
                        else receipt["comparison_sha256"]
                    ),
                    "receipt_sha256": (
                        None
                        if receipt is None
                        else sha256(canonical_bytes(receipt))
                    ),
                }
        last = attempts[-1]
        if (
            item["pack_path"] != last["pack_path"]
            or item["comparison_path"]
            != last["comparison_path"]
            or item["receipt_path"] != last["receipt_path"]
            or item["status"] != last["status"]
            or item["retrieval_status"]
            != final_retrieval_status
        ):
            raise RuntimeContractError(
                "resume comparison final attempt changed"
            )
        if final_binding is None and not allow_unbindable:
            raise RuntimeContractError(
                "resume final pack binding is unavailable"
            )
        if final_binding is not None:
            resume_bindings.append(final_binding)
    if (
        not isinstance(stage_records, list)
        or len(stage_records) != len(expected)
    ):
        label = (
            "contained-stage"
            if execution_boundary == CONTAINED_EXECUTOR
            else "portable-stage"
        )
        raise RuntimeContractError(
            f"resume {label} coverage is invalid"
        )
    for stage_number, (expected_attempt, record) in enumerate(
        zip(expected, stage_records), start=1
    ):
        receipt = expected_attempt["receipt"]
        invocation_root = _expected_comparator_invocation_root(
            candidate_root,
            stage_number,
            expected_attempt["intent"],
            expected_attempt["pack"],
            execution_boundary,
        )
        if execution_boundary == PORTABLE_EXECUTION_BOUNDARY:
            verified = _verified_public_portable_stage(
                record, pathlib.Path(candidate_root)
            )
            completion_raw = _read_bound_regular(
                verified["completion_path"],
                "resume portable-stage completion",
                maximum=1024 * 1024,
            )
            preflight_raw = _read_bound_regular(
                verified["preflight_path"],
                "resume portable-stage preflight",
                maximum=1024 * 1024,
            )
            try:
                preflight = json.loads(
                    preflight_raw.decode("utf-8")
                )
            except (UnicodeDecodeError, ValueError) as exc:
                raise RuntimeContractError(
                    "resume portable-stage preflight is invalid"
                ) from exc
            if (
                preflight_raw != canonical_bytes(preflight)
                or record.get("execution_boundary")
                != PORTABLE_EXECUTION_BOUNDARY
                or record.get("stage") != "history-compare"
                or record.get("seat_id")
                != f"history-compare-{stage_number}"
                or preflight.get("stage") != "history-compare"
                or preflight.get("input_sha256s")
                != {
                    "retrieval_pack.json":
                        expected_attempt["input_sha256"]
                }
                or record.get("input_sha256s")
                != preflight.get("input_sha256s")
                or not _valid_sha256(
                    record.get(
                        "execution_request_profile_hash"
                    )
                )
                or preflight.get(
                    "execution_request_profile_hash"
                )
                != record[
                    "execution_request_profile_hash"
                ]
                or receipt.get("pack_sha256")
                != expected_attempt["pack_sha256"]
                or record.get("serialized_prompt_sha256")
                != expected_attempt["serialized_prompt_sha256"]
                or receipt.get("comparator_invocation_sha256")
                != expected_attempt["serialized_prompt_sha256"]
                or record.get("outputs", {}).get(
                    "history-comparison.json", {}
                ).get("sha256")
                != expected_attempt["comparison_sha256"]
                or pathlib.Path(
                    verified["output_paths"][
                        "history-comparison.json"
                    ]
                ).resolve()
                != (
                    invocation_root
                    / "output"
                    / "history-comparison.json"
                ).resolve()
                or pathlib.Path(verified["preflight_path"]).resolve()
                != (invocation_root / "state" / "preflight.json").resolve()
                or pathlib.Path(verified["completion_path"]).resolve()
                != (invocation_root / "state" / "completion.json").resolve()
                or sha256(completion_raw)
                != record.get("completion", {}).get("sha256")
            ):
                raise RuntimeContractError(
                    "resume portable-stage binding changed"
                )
            continue
        if (
            not isinstance(record, dict)
            or set(record) != {"prepared", "completion_sha256"}
            or not _valid_sha256(record["completion_sha256"])
        ):
            raise RuntimeContractError(
                "resume contained-stage record is invalid"
            )
        prepared = record["prepared"]
        verify_stage_completion(prepared)
        completion_raw = _read_bound_regular(
            prepared["completion_path"],
            "resume contained-stage completion",
            maximum=1024 * 1024,
        )
        manifest = _load_canonical_json(
            prepared["manifest_path"],
            "resume contained-stage manifest",
        )
        contained_output_raw = _read_bound_regular(
            prepared["output_paths"]["history-comparison.json"],
            "resume contained comparator output",
            maximum=65536,
        )
        inputs = manifest.get("inputs")
        if (
            prepared.get("stage") != "history-compare"
            or manifest.get("stage") != "history-compare"
            or not isinstance(inputs, list)
            or len(inputs) != 1
            or inputs[0].get("source") != "retrieval_pack.json"
            or inputs[0].get("sha256")
            != expected_attempt["input_sha256"]
            or receipt.get("pack_sha256")
            != expected_attempt["pack_sha256"]
            or receipt.get("comparator_invocation_sha256")
            != expected_attempt["serialized_prompt_sha256"]
            or manifest.get("invocation", {}).get(
                "expected_serialized_sha256"
            )
            != expected_attempt["serialized_prompt_sha256"]
            or pathlib.Path(prepared.get("manifest_path", "")).resolve()
            != (invocation_root / "manifest.json").resolve()
            or pathlib.Path(prepared.get("output_root", "")).resolve()
            != (invocation_root / "output").resolve()
            or pathlib.Path(
                prepared.get("completion_path", "")
            ).resolve()
            != (invocation_root / "output" / "completion.json").resolve()
            or pathlib.Path(
                prepared.get("output_paths", {}).get(
                    "history-comparison.json", ""
                )
            ).resolve()
            != (
                invocation_root
                / "output"
                / "history-comparison.json"
            ).resolve()
            or sha256(contained_output_raw)
            != expected_attempt["comparison_sha256"]
            or sha256(completion_raw)
            != record["completion_sha256"]
        ):
            raise RuntimeContractError(
                "resume contained-stage binding changed"
            )
    return resume_bindings


def _resume_material(
    *,
    db_path,
    policy_path,
    batch_path,
    selection_path,
    artifact_root,
    comparison_index_path,
    prior_work_path,
    authority=None,
):
    policy = history_projection.load_policy(policy_path)
    authority_value = _validated_runtime_authority(
        policy, authority, state_paths=(db_path,)
    )
    batch, candidates = _load_batch_candidates(batch_path)
    selection = verify_round_selection(selection_path)
    root = pathlib.Path(artifact_root)
    if (
        selection["batch_sha256"] != batch["batch_sha256"]
        or pathlib.Path(batch_path).resolve()
        != (
            pathlib.Path(batch["artifact_root"]) / "batch.json"
        ).resolve()
        or pathlib.Path(selection["batch_path"]).resolve()
        != pathlib.Path(batch_path).resolve()
        or pathlib.Path(
            selection["round_observation_path"]
        ).parent.resolve()
        != root.resolve()
    ):
        raise RuntimeContractError(
            "resume selection is outside the frozen round"
        )
    index = _comparison_index(
        comparison_index_path, selection
    )
    if pathlib.Path(comparison_index_path).resolve() != (
        root / "comparison-index.json"
    ).resolve():
        raise RuntimeContractError(
            "resume comparison index path is invalid"
        )
    portable_comparison = index["schema_version"] == 2
    prior_descriptor, _ = _source_descriptor(
        prior_work_path, "resume prior work", maximum=1024 * 1024
    )
    conn = _connect_history_store(db_path)
    history_store.init_schema(conn)
    try:
        current = _current_generation_binding(conn, policy)
        bindings = []
        summaries = []
        nonpermanent_observations = []
        for indexed in index["targets"]:
            candidate_id = indexed["candidate_id"]
            candidate = candidates[candidate_id]
            expected_observation_path = (
                root
                / candidate_id
                / "comparison-observation.json"
            )
            if (
                pathlib.Path(
                    indexed["observation_path"]
                ).resolve()
                != expected_observation_path.resolve()
            ):
                raise RuntimeContractError(
                    "resume comparison observation path is invalid"
                )
            observation = _load_canonical_json(
                expected_observation_path,
                "resume comparison observation",
            )
            material = dict(observation)
            observation_sha = material.pop(
                "observation_sha256", None
            )
            if (
                observation_sha
                != indexed["observation_sha256"]
                or observation_sha
                != sha256(
                    b"history-runtime-observation-v1\0"
                    + canonical_bytes(material)
                )
                or observation.get("candidate_id")
                != candidate_id
                or observation.get(
                    "candidate_content_sha256"
                )
                != candidate["content_sha256"]
                or [
                    item.get("status")
                    for item in observation.get(
                        "observations", []
                    )
                ]
                != indexed["statuses"]
            ):
                raise RuntimeContractError(
                    "resume comparison observation changed"
                )
            statuses = indexed["statuses"]
            permanent = (
                len(statuses) == len(required_intents(candidate))
                and all(
                    status
                    in history_retrieval.PERMANENT_STATUSES
                    for status in statuses
                )
            )
            if not permanent:
                nonpermanent_observations.append(
                    {
                        "candidate_id": candidate_id,
                        "observation_sha256":
                            indexed["observation_sha256"],
                        "statuses": statuses,
                    }
                )
            (
                comparison_executor,
                comparison_stage_records,
            ) = _comparison_stage_binding(index, indexed)
            stage_bindings = _validate_resume_comparator_stages(
                candidate=candidate,
                candidate_root=root / candidate_id,
                observation=observation,
                stage_records=comparison_stage_records,
                conn=conn,
                policy=policy,
                allow_unbindable=True,
                execution_boundary=comparison_executor,
            )
            for stage_binding in stage_bindings:
                pack = stage_binding["pack"]
                if (
                    not _pack_matches_candidate(pack, candidate)
                    or pack["retrieval_policy_version"]
                    != policy["retrieval_policy_version"]
                    or pack["policy_sha256"]
                    != current["policy_sha256"]
                    or pack["source_watermark"]
                    != current["source_watermark"]
                    or pack["index_generation"]
                    != current["index_generation"]
                    or pack["generation_manifest_sha256"]
                    != current["generation_manifest_sha256"]
                ):
                    raise RuntimeContractError(
                        "resume pack is not current"
                    )
                bindings.append(
                    dict(
                        candidate_id=candidate_id,
                        intent=stage_binding["intent"],
                        comparison_sha256=stage_binding[
                            "comparison_sha256"
                        ],
                        comparison_receipt_sha256=stage_binding[
                            "receipt_sha256"
                        ],
                        **resume_binding(
                            mode=policy["mode"],
                            policy_version=policy[
                                "retrieval_policy_version"
                            ],
                            policy_sha256=current[
                                "policy_sha256"
                            ],
                            source_watermark=current[
                                "source_watermark"
                            ],
                            index_generation=current[
                                "index_generation"
                            ],
                            pack_sha256=pack["pack_sha256"],
                            comparator_version=stage_binding[
                                "comparator_version"
                            ],
                            candidate_content_sha256=
                                candidate["content_sha256"],
                            adapter_version=policy[
                                "adapter_version"
                            ],
                            preflight_sha256=stage_binding[
                                "preflight_sha256"
                            ],
                        ),
                    )
                )
            if policy["mode"] == "enforcement" and permanent:
                summary_path = (
                    root
                    / candidate_id
                    / "history-summary.json"
                )
                summary = _load_canonical_json(
                    summary_path, "resume history summary"
                )
                verify_history_summary(
                    conn, candidate, summary, policy
                )
                summaries.append(
                    {
                        "candidate_id": candidate_id,
                        "path": str(summary_path.resolve()),
                        "sha256": sha256(
                            canonical_bytes(summary)
                        ),
                    }
                )
        result = {
            "schema_version": (
                2 if portable_comparison else 1
            ),
            "runtime_authority": {
                field: authority_value[field]
                for field in (
                    "mode",
                    "policy_sha256",
                    "capability_sha256",
                    "trust_root_sha256",
                    "scope",
                )
            },
            "policy_path": str(
                pathlib.Path(
                    os.path.abspath(os.fspath(policy_path))
                )
            ),
            "db_path": str(
                pathlib.Path(
                    os.path.abspath(os.fspath(db_path))
                )
            ),
            "batch_path": str(
                pathlib.Path(
                    os.path.abspath(os.fspath(batch_path))
                )
            ),
            "batch_sha256": batch["batch_sha256"],
            "selection_path": str(
                pathlib.Path(
                    os.path.abspath(os.fspath(selection_path))
                )
            ),
            "selection_sha256": selection["selection_sha256"],
            "artifact_root": str(
                pathlib.Path(
                    os.path.abspath(os.fspath(artifact_root))
                )
            ),
            "comparison_index_path": str(
                pathlib.Path(
                    os.path.abspath(
                        os.fspath(comparison_index_path)
                    )
                )
            ),
            "comparison_index_sha256":
                index["comparison_index_sha256"],
            "prior_work": prior_descriptor,
            "summaries": summaries,
            "nonpermanent_observations":
                nonpermanent_observations,
            "bindings": bindings,
        }
        if portable_comparison:
            result["execution_boundary"] = (
                PORTABLE_EXECUTION_BOUNDARY
            )
        return result
    finally:
        conn.close()


def _valid_run_id(value):
    if not isinstance(value, str) or not value:
        return False
    allowed = (
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789._-"
    )
    return all(character in allowed for character in value)


def seal_resume_state(*, output_path, authority=None, **values):
    policy = history_projection.load_policy(
        values["policy_path"]
    )
    _validated_runtime_authority(
        policy,
        authority,
        state_paths=(output_path,),
    )
    _, prior_raw = _source_descriptor(
        values["prior_work_path"],
        "resume prior-work source",
        maximum=1024 * 1024,
    )
    output = pathlib.Path(
        os.path.abspath(os.fspath(output_path))
    )
    input_root = output.parent / (output.name + "-inputs")
    frozen_prior = input_root / "prior-work.md"
    _publish_immutable_tree(
        input_root, {"prior-work.md": prior_raw}
    )
    try:
        material_values = dict(values)
        material_values["prior_work_path"] = frozen_prior
        result = _resume_material(
            authority=authority, **material_values
        )
        resume_domain = b"history-runtime-resume-v1\0"
        if result["schema_version"] == 2:
            resume_domain = b"history-runtime-resume-v2\0"
        result["resume_sha256"] = sha256(
            resume_domain + canonical_bytes(result)
        )
        _publish_immutable(output, canonical_bytes(result))
    except Exception:
        _remove_immutable_tree(input_root)
        raise
    return result


def seal_resume_attempt(
    *,
    resume_path,
    run_id,
    resumed_from_run_id,
    output_path,
    prior_archive_path=None,
    authority=None,
):
    if (
        not _valid_run_id(run_id)
        or not _valid_run_id(resumed_from_run_id)
        or run_id == resumed_from_run_id
    ):
        raise RuntimeContractError(
            "resume attempt run IDs are invalid"
        )
    resume = validate_resume_state(
        resume_path, authority=authority
    )
    policy = history_projection.load_policy(resume["policy_path"])
    _validated_runtime_authority(
        policy, authority, state_paths=(output_path,)
    )
    prior_binding = None
    if prior_archive_path is not None:
        try:
            prior_binding = (
                history_archive.verified_failure_archive_binding(
                    prior_archive_path,
                    expected_run_id=resumed_from_run_id,
                )
            )
        except history_archive.ArchiveError as exc:
            raise RuntimeContractError(
                "prior failure archive is invalid"
            ) from exc
    receipt = {
        "schema_version": 1,
        "run_id": run_id,
        "resumed_from_run_id": resumed_from_run_id,
        "resume_state_sha256": resume["resume_sha256"],
        "prior_failure_archive": prior_binding,
    }
    receipt["resume_attempt_sha256"] = sha256(
        b"history-runtime-resume-attempt-v1\0"
        + canonical_bytes(receipt)
    )
    _publish_immutable(output_path, canonical_bytes(receipt))
    return receipt


def validate_resume_state(
    resume_path,
    authority=None,
    expected_direction=_DIRECTION_UNSPECIFIED,
):
    if expected_direction is not _DIRECTION_UNSPECIFIED:
        try:
            expected_direction = (
                direction_contract_lib.validate_identity(
                    expected_direction
                )
            )
        except direction_contract_lib.DirectionContractError as exc:
            raise RuntimeContractError(
                "expected direction identity is invalid"
            ) from exc
    resume = _load_canonical_json(
        resume_path, "resume state"
    )
    fields = {
        "schema_version",
        "runtime_authority",
        "policy_path",
        "db_path",
        "batch_path",
        "batch_sha256",
        "selection_path",
        "selection_sha256",
        "artifact_root",
        "comparison_index_path",
        "comparison_index_sha256",
        "prior_work",
        "summaries",
        "nonpermanent_observations",
        "bindings",
        "resume_sha256",
    }
    if not isinstance(resume, dict):
        raise RuntimeContractError(
            "resume state schema is invalid"
        )
    if resume.get("schema_version") == 1:
        resume_domain = b"history-runtime-resume-v1\0"
    elif (
        resume.get("schema_version") == 2
        and resume.get("execution_boundary")
        == PORTABLE_EXECUTION_BOUNDARY
    ):
        fields.add("execution_boundary")
        resume_domain = b"history-runtime-resume-v2\0"
    else:
        raise RuntimeContractError(
            "resume state schema is invalid"
        )
    if set(resume) != fields:
        raise RuntimeContractError(
            "resume state schema is invalid"
        )
    material = dict(resume)
    resume_sha = material.pop("resume_sha256")
    if resume_sha != sha256(
        resume_domain + canonical_bytes(material)
    ):
        raise RuntimeContractError(
            "resume state ID is invalid"
        )
    expected = _resume_material(
        db_path=resume["db_path"],
        policy_path=resume["policy_path"],
        batch_path=resume["batch_path"],
        selection_path=resume["selection_path"],
        artifact_root=resume["artifact_root"],
        comparison_index_path=
            resume["comparison_index_path"],
        prior_work_path=resume["prior_work"]["path"],
        authority=authority,
    )
    if expected != material:
        changed = sorted(
            key
            for key in set(expected) | set(material)
            if expected.get(key) != material.get(key)
        )
        raise RuntimeContractError(
            "resume state binding changed: "
            + ",".join(changed)
        )
    if expected_direction is not _DIRECTION_UNSPECIFIED:
        batch, _ = _load_batch_candidates(resume["batch_path"])
        if frozen_batch_direction(batch) != expected_direction:
            raise RuntimeContractError(
                "resume direction identity changed"
            )
    return resume


def _parse_input_bindings(values):
    result = {}
    for value in values:
        if "=" not in value:
            raise RuntimeContractError(
                "stage input must use name=path"
            )
        name, path = value.split("=", 1)
        if not name or not path or name in result:
            raise RuntimeContractError(
                "stage input binding is invalid"
            )
        result[name] = pathlib.Path(path)
    return result


def _parse_reviewer_commands(values):
    result = {}
    for value in values:
        if "=" not in value:
            raise RuntimeContractError(
                "reviewer command must use seat=argv-json"
            )
        seat, command_json = value.split("=", 1)
        if not seat or not command_json or seat in result:
            raise RuntimeContractError(
                "reviewer command binding is invalid"
            )
        result[seat] = command_json
    return result


def _parse_reviewer_request_profiles(values):
    result = {}
    for value in values:
        if "=" not in value:
            raise RuntimeContractError(
                "reviewer request profile must use seat=path"
            )
        seat, path = value.split("=", 1)
        if not seat or not path or seat in result:
            raise RuntimeContractError(
                "reviewer request profile binding is invalid"
            )
        result[seat] = _load_portable_request_profile(path)
    return result


def _add_cli_authority_arguments(parser):
    parser.add_argument("--calibration-capability")
    parser.add_argument("--production-trust-root")


def _cli_runtime_authority(args):
    policy = history_projection.load_policy(args.policy)
    return validate_runtime_mode(
        policy,
        capability=args.calibration_capability,
        trust_root=args.production_trust_root,
    )


def _main(argv=None):
    parser = argparse.ArgumentParser(
        description="Bounded history runtime host."
    )
    subparsers = parser.add_subparsers(
        dest="operation", required=True
    )
    startup = subparsers.add_parser("startup")
    startup.add_argument("--db", required=True)
    startup.add_argument("--ledger", required=True)
    startup.add_argument("--ledger-good", required=True)
    startup.add_argument("--state-root", required=True)
    startup.add_argument("--policy", required=True)
    startup.add_argument("--brief", required=True)
    startup.add_argument("--divergence-lens", default="")
    startup.add_argument("--near-sa")
    startup.add_argument("--calibration-capability")
    startup.add_argument("--production-trust-root")
    stage = subparsers.add_parser("run-stage")
    stage.add_argument("--stage", required=True)
    stage.add_argument("--seat", required=True)
    stage.add_argument("--db", required=True)
    stage.add_argument("--policy", required=True)
    stage.add_argument("--input", action="append", default=[])
    stage.add_argument("--output-root", required=True)
    stage.add_argument("--manifest", required=True)
    stage.add_argument(
        "--command", dest="command_json", required=True
    )
    _add_cli_authority_arguments(stage)
    freeze = subparsers.add_parser("freeze-batch")
    freeze.add_argument("--tsv", required=True)
    freeze.add_argument("--markdown", required=True)
    freeze.add_argument("--output-root", required=True)
    freeze.add_argument("--brief", required=True)
    freeze.add_argument("--direction")
    freeze.add_argument("--expected-direction")
    copy_direction = subparsers.add_parser("copy-direction")
    copy_direction.add_argument("--contract", required=True)
    copy_direction.add_argument("--round-identity", required=True)
    copy_direction.add_argument("--expected-direction", required=True)
    copy_direction.add_argument("--batch", required=True)
    copy_direction.add_argument("--output", required=True)
    direction_gate = subparsers.add_parser("validate-direction-gate")
    direction_gate.add_argument("--contract", required=True)
    direction_gate.add_argument("--expected-direction", required=True)
    direction_gate.add_argument("--batch", required=True)
    direction_gate.add_argument("--verdicts", required=True)
    direction_gate.add_argument("--output", required=True)
    observe = subparsers.add_parser("observe-round")
    observe.add_argument("--db", required=True)
    observe.add_argument("--policy", required=True)
    observe.add_argument("--batch", required=True)
    observe.add_argument("--artifact-root", required=True)
    _add_cli_authority_arguments(observe)
    compare = subparsers.add_parser("compare-targets")
    compare.add_argument("--db", required=True)
    compare.add_argument("--policy", required=True)
    compare.add_argument("--batch", required=True)
    compare.add_argument("--artifact-root", required=True)
    compare.add_argument("--selection", required=True)
    compare.add_argument(
        "--command", dest="command_json"
    )
    compare.add_argument(
        "--executor",
        choices=(CONTAINED_EXECUTOR, PORTABLE_EXECUTOR),
        default=CONTAINED_EXECUTOR,
    )
    compare.add_argument("--provider-request-profile")
    _add_cli_authority_arguments(compare)
    seal_selection = subparsers.add_parser("seal-selection")
    seal_selection.add_argument("--batch", required=True)
    seal_selection.add_argument(
        "--round-observation", required=True
    )
    seal_selection.add_argument("--brief", required=True)
    seal_selection.add_argument("--selector", required=True)
    seal_selection.add_argument("--prescreen", required=True)
    seal_selection.add_argument(
        "--short-max", required=True, type=int
    )
    seal_selection.add_argument(
        "--theme-min-low", required=True, type=int
    )
    seal_selection.add_argument("--output", required=True)
    selection_views = subparsers.add_parser(
        "materialize-selection"
    )
    selection_views.add_argument("--batch", required=True)
    selection_views.add_argument("--selection", required=True)
    selection_views.add_argument("--output-root", required=True)
    research_views = subparsers.add_parser(
        "materialize-research"
    )
    research_views.add_argument("--db", required=True)
    research_views.add_argument("--policy", required=True)
    research_views.add_argument("--batch", required=True)
    research_views.add_argument("--selection", required=True)
    research_views.add_argument(
        "--comparison-index", required=True
    )
    research_views.add_argument(
        "--artifact-root", required=True
    )
    research_views.add_argument("--output-root", required=True)
    _add_cli_authority_arguments(research_views)
    summary = subparsers.add_parser("publish-summary")
    summary.add_argument("--db", required=True)
    summary.add_argument("--policy", required=True)
    summary.add_argument("--batch", required=True)
    summary.add_argument("--artifact-root", required=True)
    summary.add_argument("--candidate-id", required=True)
    summary.add_argument("--output", required=True)
    _add_cli_authority_arguments(summary)
    summaries = subparsers.add_parser("publish-summaries")
    summaries.add_argument("--db", required=True)
    summaries.add_argument("--policy", required=True)
    summaries.add_argument("--batch", required=True)
    summaries.add_argument("--selection", required=True)
    summaries.add_argument("--artifact-root", required=True)
    _add_cli_authority_arguments(summaries)
    seal_resume = subparsers.add_parser("seal-resume")
    seal_resume.add_argument("--db", required=True)
    seal_resume.add_argument("--policy", required=True)
    seal_resume.add_argument("--batch", required=True)
    seal_resume.add_argument("--selection", required=True)
    seal_resume.add_argument("--artifact-root", required=True)
    seal_resume.add_argument(
        "--comparison-index", required=True
    )
    seal_resume.add_argument("--prior-work", required=True)
    seal_resume.add_argument("--output", required=True)
    _add_cli_authority_arguments(seal_resume)
    validate_resume = subparsers.add_parser("validate-resume")
    validate_resume.add_argument("--policy", required=True)
    validate_resume.add_argument("--resume", required=True)
    validate_resume.add_argument("--expected-direction")
    _add_cli_authority_arguments(validate_resume)
    seal_resume_attempt_parser = subparsers.add_parser(
        "seal-resume-attempt"
    )
    seal_resume_attempt_parser.add_argument(
        "--policy", required=True
    )
    seal_resume_attempt_parser.add_argument(
        "--resume", required=True
    )
    seal_resume_attempt_parser.add_argument(
        "--run-id", required=True
    )
    seal_resume_attempt_parser.add_argument(
        "--resumed-from", required=True
    )
    seal_resume_attempt_parser.add_argument(
        "--prior-archive", default=None
    )
    seal_resume_attempt_parser.add_argument(
        "--output", required=True
    )
    _add_cli_authority_arguments(seal_resume_attempt_parser)
    review_plan = subparsers.add_parser("seal-review-plan")
    review_plan.add_argument("--db", required=True)
    review_plan.add_argument("--policy", required=True)
    review_plan.add_argument("--batch", required=True)
    review_plan.add_argument("--selection", required=True)
    review_plan.add_argument(
        "--comparison-index", required=True
    )
    review_plan.add_argument("--artifact-root", required=True)
    review_plan.add_argument("--prior-work", required=True)
    review_plan.add_argument("--review-contract", required=True)
    review_plan.add_argument(
        "--reviewer-command", action="append", default=[]
    )
    review_plan.add_argument(
        "--reviewer-request-profile", action="append", default=[]
    )
    review_plan.add_argument(
        "--executor",
        choices=(CONTAINED_EXECUTOR, PORTABLE_EXECUTOR),
        default=CONTAINED_EXECUTOR,
    )
    review_plan.add_argument("--round-date", required=True)
    review_plan.add_argument(
        "--min-read", required=True, type=int
    )
    review_plan.add_argument(
        "--axiom-min-cracks", required=True, type=int
    )
    review_plan.add_argument("--output", required=True)
    _add_cli_authority_arguments(review_plan)
    review_matrix = subparsers.add_parser(
        "run-review-matrix"
    )
    review_matrix.add_argument("--db", required=True)
    review_matrix.add_argument("--policy", required=True)
    review_matrix.add_argument("--batch", required=True)
    review_matrix.add_argument(
        "--review-plan", required=True
    )
    review_matrix.add_argument(
        "--reviewer-command", action="append", default=[]
    )
    review_matrix.add_argument(
        "--reviewer-request-profile", action="append", default=[]
    )
    review_matrix.add_argument(
        "--executor",
        choices=(CONTAINED_EXECUTOR, PORTABLE_EXECUTOR),
        default=CONTAINED_EXECUTOR,
    )
    review_matrix.add_argument("--stage-root", required=True)
    review_matrix.add_argument("--output", required=True)
    _add_cli_authority_arguments(review_matrix)
    aggregation = subparsers.add_parser(
        "build-aggregation"
    )
    aggregation.add_argument("--db", required=True)
    aggregation.add_argument("--policy", required=True)
    aggregation.add_argument("--batch", required=True)
    aggregation.add_argument("--review-plan", required=True)
    aggregation.add_argument("--review-index", required=True)
    aggregation.add_argument("--output", required=True)
    _add_cli_authority_arguments(aggregation)
    report_views = subparsers.add_parser(
        "materialize-report"
    )
    report_views.add_argument("--db", required=True)
    report_views.add_argument("--policy", required=True)
    report_views.add_argument("--batch", required=True)
    report_views.add_argument(
        "--research-view", required=True
    )
    report_views.add_argument(
        "--review-plan", required=True
    )
    report_views.add_argument(
        "--review-index", required=True
    )
    report_views.add_argument(
        "--aggregation", required=True
    )
    report_views.add_argument(
        "--round-number", required=True, type=int
    )
    report_views.add_argument(
        "--output-root", required=True
    )
    _add_cli_authority_arguments(report_views)
    commit = subparsers.add_parser("commit-round")
    commit.add_argument("--db", required=True)
    commit.add_argument("--policy", required=True)
    commit.add_argument("--batch", required=True)
    commit.add_argument("--selection", required=True)
    commit.add_argument("--comparison-index", required=True)
    commit.add_argument("--review-plan", required=True)
    commit.add_argument("--review-index", required=True)
    commit.add_argument("--aggregation", required=True)
    _add_cli_authority_arguments(commit)
    args = parser.parse_args(argv)
    if args.operation == "startup":
        result = startup_runtime(
            db_path=args.db,
            ledger_path=args.ledger,
            ledger_good_path=args.ledger_good,
            state_root=args.state_root,
            policy_path=args.policy,
            brief_path=args.brief,
            divergence_lens=args.divergence_lens,
            near_sa_path=args.near_sa,
            calibration_capability_path=
                args.calibration_capability,
            production_trust_root_path=
                args.production_trust_root,
        )
    elif args.operation == "run-stage":
        authority = _cli_runtime_authority(args)
        prepared = build_stage_manifest(
            stage=args.stage,
            seat_id=args.seat,
            db_path=args.db,
            policy_path=args.policy,
            input_paths=_parse_input_bindings(args.input),
            output_root=args.output_root,
            manifest_path=args.manifest,
            command_json=args.command_json,
            authority=authority,
        )
        completion = run_contained_stage(
            prepared, authority=authority
        )
        result = {
            "schema_version": 1,
            "prepared": prepared,
            "completion": completion,
        }
    elif args.operation == "freeze-batch":
        brief = _load_canonical_json(
            args.brief, "generation brief"
        )
        direction = None
        if args.direction is not None:
            direction_value = _load_canonical_json(
                args.direction, "direction contract"
            )
            try:
                direction, _, _ = (
                    direction_contract_lib.parse_contract_bytes(
                        canonical_bytes(direction_value)
                    )
                )
            except (
                direction_contract_lib.DirectionContractError
            ) as exc:
                raise RuntimeContractError(
                    "direction contract is invalid"
                ) from exc
        expected_direction = _DIRECTION_UNSPECIFIED
        if args.expected_direction is not None:
            expected_direction = _load_direction_identity(
                args.expected_direction,
                "expected direction identity",
            )
        result = freeze_candidate_batch(
            args.tsv,
            args.markdown,
            args.output_root,
            generation_brief=brief,
            direction_contract=direction,
            expected_direction=expected_direction,
        )
    elif args.operation == "copy-direction":
        result = copy_verified_direction_contract(
            contract_path=args.contract,
            round_identity_path=args.round_identity,
            expected_direction=_load_direction_identity(
                args.expected_direction,
                "expected direction identity",
            ),
            batch_path=args.batch,
            output_path=args.output,
        )
    elif args.operation == "validate-direction-gate":
        result = validate_direction_gate(
            contract_path=args.contract,
            expected_direction=_load_direction_identity(
                args.expected_direction,
                "expected direction identity",
            ),
            batch_path=args.batch,
            verdicts_path=args.verdicts,
            output_path=args.output,
        )
    elif args.operation == "observe-round":
        authority = _cli_runtime_authority(args)
        result = observe_frozen_batch(
            db_path=args.db,
            policy_path=args.policy,
            batch_path=args.batch,
            artifact_root=args.artifact_root,
            authority=authority,
        )
    elif args.operation == "compare-targets":
        if (
            args.executor == PORTABLE_EXECUTOR
            and args.command_json is not None
        ):
            raise RuntimeContractError(
                "portable-v2 cannot mix command_json"
            )
        if (
            args.executor == CONTAINED_EXECUTOR
            and args.provider_request_profile is not None
        ):
            raise RuntimeContractError(
                "contained-v1 cannot use portable_request_profile"
            )
        portable_request_profile = (
            _load_portable_request_profile(
                args.provider_request_profile
            )
            if args.executor == PORTABLE_EXECUTOR
            and args.provider_request_profile is not None
            else None
        )
        authority = _cli_runtime_authority(args)
        result = compare_frozen_targets(
            db_path=args.db,
            policy_path=args.policy,
            batch_path=args.batch,
            artifact_root=args.artifact_root,
            selection_path=args.selection,
            command_json=args.command_json,
            executor=args.executor,
            portable_request_profile=portable_request_profile,
            authority=authority,
        )
    elif args.operation == "seal-selection":
        result = seal_round_selection(
            batch_path=args.batch,
            round_observation_path=args.round_observation,
            generation_brief_path=args.brief,
            selector_path=args.selector,
            prescreen_path=args.prescreen,
            short_max=args.short_max,
            theme_min_low=args.theme_min_low,
            output_path=args.output,
        )
    elif args.operation == "materialize-selection":
        result = materialize_round_views(
            batch_path=args.batch,
            selection_path=args.selection,
            output_root=args.output_root,
        )
    elif args.operation == "materialize-research":
        authority = _cli_runtime_authority(args)
        result = materialize_research_views(
            db_path=args.db,
            policy_path=args.policy,
            batch_path=args.batch,
            selection_path=args.selection,
            comparison_index_path=args.comparison_index,
            artifact_root=args.artifact_root,
            output_root=args.output_root,
            authority=authority,
        )
    elif args.operation == "publish-summary":
        authority = _cli_runtime_authority(args)
        result = publish_candidate_summary(
            db_path=args.db,
            policy_path=args.policy,
            batch_path=args.batch,
            artifact_root=args.artifact_root,
            candidate_id=args.candidate_id,
            output_path=args.output,
            authority=authority,
        )
    elif args.operation == "publish-summaries":
        authority = _cli_runtime_authority(args)
        result = publish_round_summaries(
            db_path=args.db,
            policy_path=args.policy,
            batch_path=args.batch,
            selection_path=args.selection,
            artifact_root=args.artifact_root,
            authority=authority,
        )
    elif args.operation == "seal-resume":
        authority = _cli_runtime_authority(args)
        result = seal_resume_state(
            db_path=args.db,
            policy_path=args.policy,
            batch_path=args.batch,
            selection_path=args.selection,
            artifact_root=args.artifact_root,
            comparison_index_path=args.comparison_index,
            prior_work_path=args.prior_work,
            output_path=args.output,
            authority=authority,
        )
    elif args.operation == "validate-resume":
        authority = _cli_runtime_authority(args)
        expected_direction = _DIRECTION_UNSPECIFIED
        if args.expected_direction is not None:
            expected_direction = _load_canonical_json(
                args.expected_direction,
                "expected direction identity",
            )
            try:
                expected_direction = (
                    direction_contract_lib.validate_identity(
                        expected_direction
                    )
                )
            except (
                direction_contract_lib.DirectionContractError
            ) as exc:
                raise RuntimeContractError(
                    "expected direction identity is invalid"
                ) from exc
        result = validate_resume_state(
            args.resume,
            authority=authority,
            expected_direction=expected_direction,
        )
    elif args.operation == "seal-resume-attempt":
        authority = _cli_runtime_authority(args)
        result = seal_resume_attempt(
            resume_path=args.resume,
            run_id=args.run_id,
            resumed_from_run_id=args.resumed_from,
            prior_archive_path=args.prior_archive,
            output_path=args.output,
            authority=authority,
        )
    elif args.operation == "seal-review-plan":
        if (
            args.executor == PORTABLE_EXECUTOR
            and args.reviewer_command
        ):
            raise RuntimeContractError(
                "portable-v2 cannot mix reviewer_commands"
            )
        if (
            args.executor == CONTAINED_EXECUTOR
            and args.reviewer_request_profile
        ):
            raise RuntimeContractError(
                "contained-v1 cannot use reviewer_request_profiles"
            )
        reviewer_profiles = (
            _parse_reviewer_request_profiles(
                args.reviewer_request_profile
            )
            if args.executor == PORTABLE_EXECUTOR
            else None
        )
        authority = _cli_runtime_authority(args)
        result = seal_round_review_plan(
            db_path=args.db,
            policy_path=args.policy,
            batch_path=args.batch,
            selection_path=args.selection,
            comparison_index_path=args.comparison_index,
            artifact_root=args.artifact_root,
            prior_work_path=args.prior_work,
            review_contract_path=args.review_contract,
            reviewer_commands=_parse_reviewer_commands(
                args.reviewer_command
            ),
            executor=args.executor,
            reviewer_request_profiles=reviewer_profiles,
            round_date=args.round_date,
            min_read=args.min_read,
            axiom_min_cracks=args.axiom_min_cracks,
            output_path=args.output,
            authority=authority,
        )
    elif args.operation == "run-review-matrix":
        if (
            args.executor == PORTABLE_EXECUTOR
            and args.reviewer_command
        ):
            raise RuntimeContractError(
                "portable-v2 cannot mix reviewer_commands"
            )
        if (
            args.executor == CONTAINED_EXECUTOR
            and args.reviewer_request_profile
        ):
            raise RuntimeContractError(
                "contained-v1 cannot use reviewer_request_profiles"
            )
        reviewer_profiles = (
            _parse_reviewer_request_profiles(
                args.reviewer_request_profile
            )
            if args.executor == PORTABLE_EXECUTOR
            else None
        )
        authority = _cli_runtime_authority(args)
        result = run_review_matrix(
            db_path=args.db,
            policy_path=args.policy,
            batch_path=args.batch,
            review_plan_path=args.review_plan,
            reviewer_commands=_parse_reviewer_commands(
                args.reviewer_command
            ),
            executor=args.executor,
            reviewer_request_profiles=reviewer_profiles,
            stage_root=args.stage_root,
            output_path=args.output,
            authority=authority,
        )
    elif args.operation == "build-aggregation":
        authority = _cli_runtime_authority(args)
        result = build_round_aggregation(
            db_path=args.db,
            policy_path=args.policy,
            batch_path=args.batch,
            review_plan_path=args.review_plan,
            review_index_path=args.review_index,
            output_path=args.output,
            authority=authority,
        )
    elif args.operation == "materialize-report":
        authority = _cli_runtime_authority(args)
        result = materialize_report_views(
            db_path=args.db,
            policy_path=args.policy,
            batch_path=args.batch,
            research_view_path=args.research_view,
            review_plan_path=args.review_plan,
            review_index_path=args.review_index,
            aggregation_path=args.aggregation,
            output_root=args.output_root,
            round_number=args.round_number,
            authority=authority,
        )
    elif args.operation == "commit-round":
        authority = _cli_runtime_authority(args)
        result = commit_round(
            db_path=args.db,
            policy_path=args.policy,
            batch_path=args.batch,
            selection_path=args.selection,
            comparison_index_path=args.comparison_index,
            review_plan_path=args.review_plan,
            review_index_path=args.review_index,
            aggregation_path=args.aggregation,
            authority=authority,
        )
    else:
        raise AssertionError(args.operation)
    print(
        json.dumps(
            result,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
