#!/usr/bin/env python3
"""Atomic, content-addressed decision archive publication."""

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import secrets
import shutil
import stat
import tempfile


class ArchiveError(RuntimeError):
    pass


RECEIPT_PATH = pathlib.PurePosixPath(
    "history/archive-receipt.json"
)
AUTHORITY_ROOT = pathlib.PurePosixPath(
    "history/archive-authority"
)


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


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _read_regular(path, label, maximum=128 * 1024 * 1024):
    source = pathlib.Path(path)
    try:
        before = source.lstat()
    except OSError as exc:
        raise ArchiveError(f"{label} is unavailable") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_size > maximum
    ):
        raise ArchiveError(f"{label} is not a bounded regular file")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise ArchiveError(f"{label} cannot be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        raw = bytearray()
        while len(raw) <= maximum:
            chunk = os.read(
                descriptor,
                min(65536, maximum + 1 - len(raw)),
            )
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    opened_identity = (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
        opened.st_ctime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if (
        identity != opened_identity
        or opened_identity != after_identity
        or len(raw) != before.st_size
        or len(raw) > maximum
    ):
        raise ArchiveError(f"{label} changed during capture")
    return bytes(raw)


def _write_regular(path, raw):
    destination = pathlib.Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o444)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ArchiveError("archive write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_regular(source, destination, label):
    _write_regular(destination, _read_regular(source, label))


def _copy_tree(source, destination):
    root = pathlib.Path(source)
    if not root.is_dir() or root.is_symlink():
        raise ArchiveError("round source is not a safe directory")
    for current, directories, files in os.walk(
        root, topdown=True, followlinks=False
    ):
        current_path = pathlib.Path(current)
        relative = current_path.relative_to(root)
        target_directory = pathlib.Path(destination) / relative
        target_directory.mkdir(parents=True, exist_ok=True)
        for name in list(directories):
            source_directory = current_path / name
            state = source_directory.lstat()
            if not stat.S_ISDIR(state.st_mode):
                raise ArchiveError(
                    "round source contains a special directory"
                )
        for name in files:
            source_file = current_path / name
            relative_file = source_file.relative_to(root)
            if pathlib.PurePosixPath(relative_file) == RECEIPT_PATH:
                raise ArchiveError(
                    "round source collides with archive receipt"
                )
            _copy_regular(
                source_file,
                pathlib.Path(destination) / relative_file,
                f"round artifact {relative_file}",
            )


def _canonical_json_file(
    path, label, *, require_canonical=True
):
    raw = _read_regular(path, label)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ArchiveError(f"{label} is not JSON") from exc
    if require_canonical and canonical_bytes(value) != raw:
        raise ArchiveError(f"{label} is not canonical")
    return value, raw


def _capture_authority(
    *,
    policy_path,
    startup_path,
    projection_path,
    state_root,
    capability_path,
    require_projection,
):
    artifacts = {}
    try:
        policy = json.loads(
            _read_regular(
                policy_path, "retrieval policy"
            ).decode("utf-8")
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise ArchiveError("retrieval policy is not JSON") from exc
    policy_raw = canonical_bytes(policy)
    artifacts[
        str(AUTHORITY_ROOT / "retrieval-policy.json")
    ] = policy_raw
    startup, startup_raw = _canonical_json_file(
        startup_path, "runtime startup receipt"
    )
    artifacts[str(AUTHORITY_ROOT / "startup.json")] = (
        startup_raw
    )
    if startup.get("policy_sha256") != _sha256(policy_raw):
        raise ArchiveError(
            "startup receipt and archived policy differ"
        )
    projection_descriptor = None
    if projection_path:
        _, projection_raw = _canonical_json_file(
            projection_path,
            "ledger projection receipt",
            require_canonical=False,
        )
        artifacts[
            str(AUTHORITY_ROOT / "materialize-ledger.json")
        ] = projection_raw
        projection_descriptor = {
            "archive_path": (
                str(AUTHORITY_ROOT / "materialize-ledger.json")
            ),
            "sha256": _sha256(projection_raw),
        }
    elif require_projection:
        raise ArchiveError(
            "decision archive lacks the projection receipt"
        )
    targets = []
    receipt_root = (
        pathlib.Path(state_root) / "ledger-target-receipts"
    )
    for name in (
        "ledger.tsv.json",
        "tmp__ledger.good.json",
    ):
        _, raw = _canonical_json_file(
            receipt_root / name,
            f"ledger target receipt {name}",
        )
        archive_path = str(
            AUTHORITY_ROOT
            / "ledger-target-receipts"
            / name
        )
        artifacts[archive_path] = raw
        targets.append(
            {
                "archive_path": archive_path,
                "sha256": _sha256(raw),
            }
        )
    capability_descriptor = None
    if capability_path:
        capability, _ = _canonical_json_file(
            capability_path,
            "calibration capability",
        )
        capability_raw = canonical_bytes(capability)
        artifacts[
            str(
                AUTHORITY_ROOT
                / "calibration-capability.json"
            )
        ] = capability_raw
        capability_sha = _sha256(capability_raw)
        if startup.get("capability_sha256") != capability_sha:
            raise ArchiveError(
                "startup receipt and calibration capability differ"
            )
        capability_descriptor = {
            "archive_path": str(
                AUTHORITY_ROOT / "calibration-capability.json"
            ),
            "sha256": capability_sha,
        }
    elif startup.get("capability_sha256") is not None:
        raise ArchiveError(
            "startup receipt names an unavailable capability"
        )
    reference = {
        "schema_version": 1,
        "policy": {
            "archive_path": str(
                AUTHORITY_ROOT / "retrieval-policy.json"
            ),
            "sha256": _sha256(policy_raw),
        },
        "startup": {
            "archive_path": str(
                AUTHORITY_ROOT / "startup.json"
            ),
            "sha256": _sha256(startup_raw),
        },
        "projection": projection_descriptor,
        "target_receipts": targets,
        "capability": capability_descriptor,
        "trust_root_sha256": startup.get(
            "trust_root_sha256"
        ),
    }
    reference_raw = canonical_bytes(reference)
    artifacts[
        str(AUTHORITY_ROOT / "authority-reference.json")
    ] = reference_raw
    return reference, artifacts


def _stage_authority(root, **values):
    reference, artifacts = _capture_authority(**values)
    archive_root = pathlib.Path(root)
    for relative, raw in sorted(artifacts.items()):
        _write_regular(archive_root / relative, raw)
    return _sha256(canonical_bytes(reference))


def _verify_current_authority(root, **values):
    reference, artifacts = _capture_authority(**values)
    archive_root = pathlib.Path(root)
    prefix = str(AUTHORITY_ROOT) + "/"
    actual = {
        item["path"]
        for item in _tree_files(archive_root)
        if item["path"].startswith(prefix)
    }
    if actual != set(artifacts):
        raise ArchiveError(
            "archived authority artifact set changed"
        )
    for relative, expected in artifacts.items():
        if _read_regular(
            archive_root / relative,
            f"archived authority artifact {relative}",
        ) != expected:
            raise ArchiveError(
                "archived authority differs from current inputs"
            )
    return _sha256(canonical_bytes(reference))


def _tree_files(root):
    base = pathlib.Path(root)
    files = []
    for current, directories, filenames in os.walk(
        base, topdown=True, followlinks=False
    ):
        current_path = pathlib.Path(current)
        for name in list(directories):
            state = (current_path / name).lstat()
            if not stat.S_ISDIR(state.st_mode):
                raise ArchiveError(
                    "archive contains a special directory"
                )
        for name in filenames:
            path = current_path / name
            relative = pathlib.PurePosixPath(
                path.relative_to(base).as_posix()
            )
            if relative == RECEIPT_PATH:
                continue
            raw = _read_regular(
                path, f"archived artifact {relative}"
            )
            files.append(
                {
                    "path": str(relative),
                    "sha256": _sha256(raw),
                    "byte_count": len(raw),
                }
            )
    files.sort(key=lambda item: item["path"])
    return files


def _receipt(
    root,
    *,
    run_id,
    round_number,
    policy_mode,
    reason,
    authority_reference_sha256,
    source_tree_sha256,
):
    files = _tree_files(root)
    return {
        "schema_version": 1,
        "run_id": run_id,
        "round": round_number,
        "policy_mode": policy_mode,
        "archive_class": _reason_class(reason),
        "created_reason": reason,
        "authority_reference_sha256":
            authority_reference_sha256,
        "source_tree_sha256": source_tree_sha256,
        "files": files,
        "tree_sha256": _sha256(canonical_bytes(files)),
    }


def verify_archive(
    round_root,
    *,
    run_id,
    round_number,
    source_root=None,
    policy_mode=None,
    reason=None,
):
    root = pathlib.Path(round_root)
    receipt_file = root.joinpath(*RECEIPT_PATH.parts)
    receipt, _ = _canonical_json_file(
        receipt_file, "archive receipt"
    )
    fields = {
        "schema_version",
        "run_id",
        "round",
        "policy_mode",
        "archive_class",
        "created_reason",
        "authority_reference_sha256",
        "source_tree_sha256",
        "files",
        "tree_sha256",
    }
    files = _tree_files(root)
    if (
        not isinstance(receipt, dict)
        or set(receipt) != fields
        or receipt.get("schema_version") != 1
        or receipt.get("run_id") != run_id
        or receipt.get("round") != round_number
        or (
            policy_mode is not None
            and receipt.get("policy_mode") != policy_mode
        )
        or receipt.get("files") != files
        or receipt.get("tree_sha256")
        != _sha256(canonical_bytes(files))
    ):
        raise ArchiveError("archive receipt does not match its tree")
    if (
        _reason_class(receipt.get("created_reason"))
        != receipt.get("archive_class")
    ):
        raise ArchiveError("archive lifecycle transition is invalid")
    if reason is not None:
        requested_class = _reason_class(reason)
        if (
            receipt.get("archive_class") != requested_class
            or (
                requested_class == "failure"
                and receipt.get("created_reason") != reason
            )
            or (
                requested_class == "decision"
                and receipt.get("created_reason")
                not in {"decision", "published"}
            )
            or (
                requested_class == "rejection"
                and receipt.get("created_reason")
                != "rejected:direction"
            )
        ):
            raise ArchiveError(
                "archive lifecycle transition is invalid"
            )
    if source_root is not None:
        source_files = _tree_files(source_root)
        if receipt.get("source_tree_sha256") != _sha256(
            canonical_bytes(source_files)
        ):
            raise ArchiveError(
                "archive receipt belongs to a different source attempt"
            )
    reference_path = root.joinpath(
        *AUTHORITY_ROOT.parts,
        "authority-reference.json",
    )
    reference_raw = _read_regular(
        reference_path, "archive authority reference"
    )
    if (
        receipt.get("authority_reference_sha256")
        != _sha256(reference_raw)
    ):
        raise ArchiveError(
            "archive authority reference is not sealed"
        )
    return receipt


def verified_failure_archive_binding(
    destination,
    *,
    expected_run_id,
):
    archive = pathlib.Path(destination)
    try:
        archive_state = archive.lstat()
        round_state = (archive / "round").lstat()
    except OSError as exc:
        raise ArchiveError(
            "prior failure archive is unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(archive_state.st_mode)
        or stat.S_ISLNK(archive_state.st_mode)
        or not stat.S_ISDIR(round_state.st_mode)
        or stat.S_ISLNK(round_state.st_mode)
    ):
        raise ArchiveError(
            "prior failure archive is not a safe directory"
        )
    round_root = archive / "round"
    receipt_path = round_root.joinpath(*RECEIPT_PATH.parts)
    receipt, raw = _canonical_json_file(
        receipt_path, "prior failure archive receipt"
    )
    round_number = receipt.get("round")
    reason = receipt.get("created_reason")
    if (
        receipt.get("run_id") != expected_run_id
        or receipt.get("archive_class") != "failure"
        or type(round_number) is not int
        or round_number < 1
        or not isinstance(reason, str)
        or not reason.startswith("failed:")
    ):
        raise ArchiveError(
            "prior archive is not the named failed attempt"
        )
    verified = verify_archive(
        round_root,
        run_id=expected_run_id,
        round_number=round_number,
        reason=reason,
    )
    return {
        "run_id": expected_run_id,
        "round": round_number,
        "created_reason": reason,
        "archive_receipt_sha256": _sha256(raw),
        "archive_tree_sha256": verified["tree_sha256"],
    }


def _fsync_directory(path):
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _reason_class(reason):
    if reason in {"decision", "published"}:
        return "decision"
    if reason == "rejected:direction":
        return "rejection"
    if (
        isinstance(reason, str)
        and reason.startswith("failed:")
        and len(reason) > len("failed:")
    ):
        return "failure"
    raise ArchiveError("archive reason is invalid")


def _write_manifest(
    destination,
    *,
    run_id,
    round_number,
    date,
    policy_mode,
    reason,
):
    raw = (
        f"run_id\t{run_id}\n"
        f"round\t{round_number}\n"
        f"date\t{date}\n"
        f"policy_mode\t{policy_mode}\n"
        f"reason\t{reason}\n"
        "archived_at\t"
        + datetime.datetime.now().astimezone().strftime(
            "%Y-%m-%d %H:%M:%S%z"
        )
        + "\n"
    ).encode("utf-8")
    temporary = pathlib.Path(destination) / (
        ".manifest.tsv." + secrets.token_hex(12)
    )
    try:
        _write_regular(temporary, raw)
        os.replace(temporary, pathlib.Path(destination) / "manifest.tsv")
        _fsync_directory(destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def archive_round(
    *,
    source_root,
    destination,
    run_id,
    round_number,
    date,
    policy_mode,
    reason,
    policy_path,
    startup_path,
    state_root,
    projection_path=None,
    capability_path=None,
):
    if (
        not isinstance(run_id, str)
        or not run_id
        or any(
            character
            not in (
                "abcdefghijklmnopqrstuvwxyz"
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                "0123456789._-"
            )
            for character in run_id
        )
        or type(round_number) is not int
        or round_number < 1
    ):
        raise ArchiveError("archive identity is invalid")
    destination = pathlib.Path(destination)
    if destination.exists() or destination.is_symlink():
        state = destination.lstat()
        if (
            not stat.S_ISDIR(state.st_mode)
            or stat.S_ISLNK(state.st_mode)
        ):
            raise ArchiveError(
                "archive destination is not a safe directory"
            )
    else:
        destination.mkdir(parents=True)
    _reason_class(reason)
    lock_path = destination / "archive.lock"
    lock_flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        lock_flags |= os.O_NOFOLLOW
    try:
        lock_descriptor = os.open(
            lock_path, lock_flags, 0o600
        )
    except OSError as exc:
        raise ArchiveError(
            "archive lock cannot be opened safely"
        ) from exc
    try:
        lock_state = os.fstat(lock_descriptor)
        if (
            not stat.S_ISREG(lock_state.st_mode)
            or lock_state.st_nlink != 1
        ):
            raise ArchiveError("archive lock is not a regular file")
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        round_root = destination / "round"
        if round_root.exists() or round_root.is_symlink():
            if not round_root.is_dir() or round_root.is_symlink():
                raise ArchiveError(
                    "archive destination is not a safe directory"
                )
            receipt = verify_archive(
                round_root,
                run_id=run_id,
                round_number=round_number,
                source_root=source_root,
                policy_mode=policy_mode,
                reason=reason,
            )
            authority_sha = _verify_current_authority(
                round_root,
                policy_path=policy_path,
                startup_path=startup_path,
                projection_path=projection_path,
                state_root=state_root,
                capability_path=capability_path,
                require_projection=(
                    _reason_class(reason) == "decision"
                ),
            )
            if (
                receipt["authority_reference_sha256"]
                != authority_sha
            ):
                raise ArchiveError(
                    "archive authority identity changed"
                )
        else:
            temporary = pathlib.Path(
                tempfile.mkdtemp(
                    prefix=".round.tmp.", dir=destination
                )
            )
            try:
                _copy_tree(source_root, temporary)
                source_tree_sha = _sha256(
                    canonical_bytes(_tree_files(temporary))
                )
                authority_sha = _stage_authority(
                    temporary,
                    policy_path=policy_path,
                    startup_path=startup_path,
                    projection_path=projection_path,
                    state_root=state_root,
                    capability_path=capability_path,
                    require_projection=(
                        _reason_class(reason) == "decision"
                    ),
                )
                receipt = _receipt(
                    temporary,
                    run_id=run_id,
                    round_number=round_number,
                    policy_mode=policy_mode,
                    reason=reason,
                    authority_reference_sha256=authority_sha,
                    source_tree_sha256=source_tree_sha,
                )
                receipt_path = temporary.joinpath(
                    *RECEIPT_PATH.parts
                )
                _write_regular(
                    receipt_path, canonical_bytes(receipt)
                )
                verify_archive(
                    temporary,
                    run_id=run_id,
                    round_number=round_number,
                    source_root=source_root,
                    policy_mode=policy_mode,
                    reason=reason,
                )
                for current, _, _ in os.walk(
                    temporary, topdown=False
                ):
                    _fsync_directory(current)
                _fsync_directory(destination)
                os.rename(temporary, round_root)
                _fsync_directory(destination)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        _write_manifest(
            destination,
            run_id=run_id,
            round_number=round_number,
            date=date,
            policy_mode=policy_mode,
            reason=reason,
        )
        return receipt
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)


def _main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--round", required=True, type=int)
    parser.add_argument("--date", required=True)
    parser.add_argument("--policy-mode", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--startup", required=True)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--projection")
    parser.add_argument("--capability")
    args = parser.parse_args(argv)
    archive_round(
        source_root=args.source_root,
        destination=args.destination,
        run_id=args.run_id,
        round_number=args.round,
        date=args.date,
        policy_mode=args.policy_mode,
        reason=args.reason,
        policy_path=args.policy,
        startup_path=args.startup,
        state_root=args.state_root,
        projection_path=args.projection,
        capability_path=args.capability,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
