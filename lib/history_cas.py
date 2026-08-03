#!/usr/bin/env python3
"""Durable content-addressed storage foundation for history audit v2."""

import datetime
import hashlib
import json
import os
import pathlib
import secrets
import sqlite3
import stat
import zlib

try:
    from lib import history_contract_v2
except ImportError:
    import history_contract_v2


CODEC = "zlib-v1"


class CASError(RuntimeError):
    pass


class CASIntegrityError(CASError):
    pass


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _require_sha(value, name):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CASError(f"{name} must be a lowercase SHA-256")


def _relative_path(object_id):
    _require_sha(object_id, "object_id")
    return pathlib.Path(object_id[:2]) / (object_id[2:] + ".zlib")


def _timestamp(value, name):
    if not isinstance(value, str) or not value:
        raise CASError(f"{name} must be a timezone-aware timestamp")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CASError(f"{name} must be a timezone-aware timestamp") from exc
    if parsed.tzinfo is None:
        raise CASError(f"{name} must be a timezone-aware timestamp")
    return parsed.astimezone(datetime.timezone.utc)


def _reject_symlink_ancestors(path):
    absolute = pathlib.Path(path).absolute()
    current = pathlib.Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.exists() or current.is_symlink():
            if current.is_symlink():
                raise CASError("CAS path has a symlink ancestor")


def _ensure_directory(path):
    _reject_symlink_ancestors(path.parent)
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise CASError("CAS directory is not a real directory")


def _bounded_target(root, object_id):
    configured_root = pathlib.Path(root).absolute()
    if configured_root.is_symlink():
        raise CASError("CAS root must not be a symlink")
    root = configured_root.resolve(strict=False)
    _ensure_directory(root)
    relative = _relative_path(object_id)
    target = root / relative
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise CASError("CAS object path escapes its root") from exc
    _reject_symlink_ancestors(target.parent)
    return root, relative, target


def _require_regular_object(file_stat):
    if not stat.S_ISREG(file_stat.st_mode):
        raise CASIntegrityError("CAS payload is not a regular file")
    if file_stat.st_nlink != 1:
        raise CASIntegrityError("CAS payload must have exactly one link")
    blocks = getattr(file_stat, "st_blocks", None)
    if file_stat.st_size and blocks is not None and blocks * 512 < file_stat.st_size:
        raise CASIntegrityError("CAS payload must not be sparse")


def _read_payload(path):
    try:
        visible_before = os.lstat(path)
    except FileNotFoundError:
        raise
    _require_regular_object(visible_before)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise CASIntegrityError("CAS payload cannot be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        _require_regular_object(opened)
        if (visible_before.st_dev, visible_before.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise CASIntegrityError("CAS payload path changed before verification")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        finished = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ) != (
            finished.st_dev,
            finished.st_ino,
            finished.st_size,
            finished.st_mtime_ns,
        ):
            raise CASIntegrityError("CAS payload changed during verification")
        visible = os.lstat(path)
        if (visible.st_dev, visible.st_ino) != (finished.st_dev, finished.st_ino):
            raise CASIntegrityError("CAS payload path changed during verification")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _fsync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor, raw):
    position = 0
    while position < len(raw):
        written = os.write(descriptor, raw[position:])
        if written <= 0:
            raise CASError("CAS write made no progress")
        position += written


def _publish(root, object_id, compressed):
    root, relative, target = _bounded_target(root, object_id)
    parent = root / relative.parent
    _ensure_directory(parent)
    if target.exists():
        if _read_payload(target) != compressed:
            raise CASIntegrityError("published CAS bytes conflict with object identity")
        return relative.as_posix()

    temporary = parent / (
        "." + object_id + "." + secrets.token_hex(12) + ".tmp"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(temporary), flags, 0o600)
    try:
        _write_all(descriptor, compressed)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        if target.exists():
            if _read_payload(target) != compressed:
                raise CASIntegrityError(
                    "concurrent CAS publish conflicts with object identity"
                )
        else:
            os.replace(str(temporary), str(target))
            _fsync_directory(parent)
            if _read_payload(target) != compressed:
                raise CASIntegrityError("published CAS bytes failed verification")
    finally:
        if temporary.exists():
            temporary.unlink()
    return relative.as_posix()


def _descriptor_material(
    raw, compressed, retention_profile, relative_path, expires_at
):
    object_id = _sha256(raw)
    return {
        "object_id": object_id,
        "raw_sha256": object_id,
        "compressed_sha256": _sha256(compressed),
        "codec": CODEC,
        "raw_length": len(raw),
        "compressed_length": len(compressed),
        "retention_profile": retention_profile,
        "relative_path": relative_path,
        "created_at": _utc_now(),
        "expires_at": expires_at,
        "integrity_state": "verified",
    }


def _assert_descriptor_matches(row, expected):
    for field in (
        "object_id",
        "raw_sha256",
        "compressed_sha256",
        "codec",
        "raw_length",
        "compressed_length",
        "retention_profile",
        "relative_path",
        "expires_at",
        "integrity_state",
    ):
        if row[field] != expected[field]:
            raise CASIntegrityError(f"CAS descriptor mismatch: {field}")


def put_object(
    conn, root, raw, retention_profile, *, expires_at=None, pin_reason=None
):
    """Compress, fsync, publish, describe, and optionally pin one CAS object."""
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3 connection")
    if conn.in_transaction:
        raise CASError("CAS publication requires an idle connection")
    if not isinstance(raw, bytes):
        raise CASError("CAS payload must be bytes")
    if not isinstance(retention_profile, str) or not retention_profile:
        raise CASError("retention_profile must be a non-empty string")
    if expires_at is not None:
        _timestamp(expires_at, "expires_at")
    if pin_reason is not None and (
        not isinstance(pin_reason, str) or not pin_reason
    ):
        raise CASError("pin_reason must be a non-empty string")

    compressed = zlib.compress(raw, level=9)
    object_id = _sha256(raw)
    relative_path = _publish(root, object_id, compressed)
    expected = _descriptor_material(
        raw, compressed, retention_profile, relative_path, expires_at
    )
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM audit_cas_objects WHERE object_id=?", (object_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO audit_cas_objects(
                  object_id, raw_sha256, compressed_sha256, codec,
                  raw_length, compressed_length, retention_profile,
                  relative_path, created_at, expires_at, integrity_state
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(expected[field] for field in expected),
            )
            row = conn.execute(
                "SELECT * FROM audit_cas_objects WHERE object_id=?", (object_id,)
            ).fetchone()
        _assert_descriptor_matches(row, expected)
        if pin_reason is not None:
            conn.execute(
                "INSERT OR IGNORE INTO audit_cas_pins("
                "object_id, pin_reason, pinned_at) VALUES(?, ?, ?)",
                (object_id, pin_reason, _utc_now()),
            )
        conn.execute("COMMIT")
    except Exception as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if isinstance(exc, CASError):
            raise
        raise CASError("CAS descriptor persistence failed") from exc
    return dict(row)


def verify_object(conn, root, object_id):
    """Verify raw/compressed hashes, codec, lengths, descriptor, and payload."""
    _require_sha(object_id, "object_id")
    row = conn.execute(
        "SELECT * FROM audit_cas_objects WHERE object_id=?", (object_id,)
    ).fetchone()
    if row is None:
        raise CASIntegrityError("CAS descriptor is missing")
    descriptor = dict(row)
    if descriptor["integrity_state"] != "verified":
        raise CASIntegrityError("CAS descriptor is not verified")
    if descriptor["codec"] != CODEC:
        raise CASIntegrityError("CAS codec is unsupported")
    root, relative, path = _bounded_target(root, object_id)
    expected_relative = relative.as_posix()
    if descriptor["relative_path"] != expected_relative:
        raise CASIntegrityError("CAS descriptor path is invalid")
    tombstone = conn.execute(
        "SELECT * FROM audit_cas_tombstones WHERE object_id=?", (object_id,)
    ).fetchone()
    try:
        compressed = _read_payload(path)
    except FileNotFoundError as exc:
        if tombstone is None:
            raise CASIntegrityError("CAS payload is missing without tombstone") from exc
        _validate_tombstone(dict(tombstone), descriptor)
        expired = dict(descriptor)
        expired["integrity_state"] = "expired"
        expired["tombstone_sha256"] = tombstone["tombstone_sha256"]
        return expired
    if len(compressed) != descriptor["compressed_length"]:
        raise CASIntegrityError("CAS compressed length mismatch")
    if _sha256(compressed) != descriptor["compressed_sha256"]:
        raise CASIntegrityError("CAS compressed hash mismatch")
    try:
        raw = zlib.decompress(compressed)
    except zlib.error as exc:
        raise CASIntegrityError("CAS payload cannot be decompressed") from exc
    if len(raw) != descriptor["raw_length"]:
        raise CASIntegrityError("CAS raw length mismatch")
    raw_sha = _sha256(raw)
    if raw_sha != object_id or raw_sha != descriptor["raw_sha256"]:
        raise CASIntegrityError("CAS raw hash mismatch")
    if tombstone is not None:
        _validate_tombstone(dict(tombstone), descriptor)
        descriptor["integrity_state"] = "tombstoned"
        descriptor["tombstone_sha256"] = tombstone["tombstone_sha256"]
    return descriptor


def _tombstone_material(descriptor, reason, marked_at, delete_after):
    return {
        "object_id": descriptor["object_id"],
        "raw_sha256": descriptor["raw_sha256"],
        "compressed_sha256": descriptor["compressed_sha256"],
        "codec": descriptor["codec"],
        "raw_length": descriptor["raw_length"],
        "compressed_length": descriptor["compressed_length"],
        "reason": reason,
        "marked_at": marked_at,
        "delete_after": delete_after,
    }


def _validate_tombstone(tombstone, descriptor):
    material = _tombstone_material(
        descriptor,
        tombstone["reason"],
        tombstone["marked_at"],
        tombstone["delete_after"],
    )
    expected = _sha256(history_contract_v2.canonical_bytes(material))
    if expected != tombstone["tombstone_sha256"]:
        raise CASIntegrityError("CAS tombstone hash mismatch")
    if tombstone["reason"] != "retention_expired":
        raise CASIntegrityError("CAS tombstone reason is invalid")


def _delete_payload(path):
    try:
        _read_payload(path)
    except FileNotFoundError:
        return False
    path.unlink()
    _fsync_directory(path.parent)
    return True


def collect_garbage(conn, root, now, grace_seconds):
    """Tombstone then delete eligible unpinned objects idempotently."""
    if not isinstance(conn, sqlite3.Connection) or conn.in_transaction:
        raise CASError("CAS garbage collection requires an idle connection")
    current = _timestamp(now, "now")
    if type(grace_seconds) is not int or grace_seconds < 0:
        raise CASError("grace_seconds must be a nonnegative integer")
    cutoff = current - datetime.timedelta(seconds=grace_seconds)
    removed = []
    rows = conn.execute(
        """
        SELECT object.*
        FROM audit_cas_objects object
        WHERE object.expires_at IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM audit_cas_pins pin WHERE pin.object_id=object.object_id
          )
        ORDER BY object.object_id
        """
    ).fetchall()
    for row in rows:
        descriptor = dict(row)
        if _timestamp(descriptor["expires_at"], "expires_at") > cutoff:
            continue
        object_id = descriptor["object_id"]
        _, relative, path = _bounded_target(root, object_id)
        if descriptor["relative_path"] != relative.as_posix():
            raise CASIntegrityError("CAS descriptor path is invalid")
        tombstone = conn.execute(
            "SELECT * FROM audit_cas_tombstones WHERE object_id=?", (object_id,)
        ).fetchone()
        if tombstone is None:
            verified = verify_object(conn, root, object_id)
            if verified["integrity_state"] != "verified":
                raise CASIntegrityError("CAS object is unexpectedly tombstoned")
            material = _tombstone_material(
                descriptor, "retention_expired", now, now
            )
            tombstone_sha = _sha256(history_contract_v2.canonical_bytes(material))
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT OR IGNORE INTO audit_cas_tombstones(
                      object_id, tombstone_sha256, reason, marked_at, delete_after
                    ) VALUES(?, ?, ?, ?, ?)
                    """,
                    (object_id, tombstone_sha, "retention_expired", now, now),
                )
                stored = conn.execute(
                    "SELECT * FROM audit_cas_tombstones WHERE object_id=?",
                    (object_id,),
                ).fetchone()
                _validate_tombstone(dict(stored), descriptor)
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        else:
            _validate_tombstone(dict(tombstone), descriptor)
        if _delete_payload(path):
            removed.append(object_id)
    return removed


_JSON_RECEIPT_FIELDS = frozenset(
    {
        "missing_ids",
        "duplicate_ids",
        "extra_ids",
        "provider_pools_ordered",
        "provider_capability_profile_hashes",
        "matched_router_rule_ids",
        "logical_task_hashes",
        "attempt_manifest_hashes",
        "raw_request_output_cas_hashes",
        "evidence_anchors",
    }
)
_BOOLEAN_RECEIPT_FIELDS = frozenset(
    {
        "invalid_schema",
        "invalid_anchor",
        "truncated",
        "coverage_complete",
        "adjudication_complete",
        "semantic_policy_qualified",
    }
)


def _receipt_row(receipt):
    row = {}
    for field, value in receipt.items():
        if field in _JSON_RECEIPT_FIELDS:
            row[field] = history_contract_v2.canonical_bytes(value).decode("utf-8")
        elif field in _BOOLEAN_RECEIPT_FIELDS:
            row[field] = int(value)
        else:
            row[field] = value
    return row


def write_minimum_receipt(conn, receipt):
    """Persist a closed receipt after every referenced CAS descriptor exists."""
    if conn.in_transaction:
        raise CASError("minimum receipt persistence requires an idle connection")
    try:
        normalized = history_contract_v2.validate_receipt(receipt)
    except history_contract_v2.ContractV2Error as exc:
        raise CASError("minimum receipt is invalid") from exc
    fields = tuple(normalized)
    encoded = _receipt_row(normalized)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for object_id in normalized["raw_request_output_cas_hashes"]:
            descriptor = conn.execute(
                "SELECT integrity_state FROM audit_cas_objects WHERE object_id=?",
                (object_id,),
            ).fetchone()
            if descriptor is None or descriptor[0] != "verified":
                raise CASError(
                    f"receipt CAS descriptor is missing or invalid: {object_id}"
                )
        placeholders = ",".join("?" for _ in fields)
        conn.execute(
            "INSERT INTO audit_receipts(" + ",".join(fields) + ") VALUES(" 
            + placeholders + ")",
            tuple(encoded[field] for field in fields),
        )
        if normalized["final_status"] in {"overlap_found", "complete_no_match"}:
            pin_reason = "terminal-receipt:" + normalized["minimum_receipt_sha"]
            for object_id in normalized["raw_request_output_cas_hashes"]:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO audit_cas_pins(
                      object_id, pin_reason, pinned_at
                    ) VALUES(?, ?, ?)
                    """,
                    (object_id, pin_reason, _utc_now()),
                )
        conn.execute("COMMIT")
    except Exception as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if isinstance(exc, CASError):
            raise
        raise CASError("minimum receipt persistence failed") from exc
    return normalized["minimum_receipt_sha"]


def verify_minimum_receipt(conn, root, minimum_receipt_sha):
    """Verify one retained receipt and every live or normally expired CAS link."""
    _require_sha(minimum_receipt_sha, "minimum_receipt_sha")
    row = conn.execute(
        "SELECT * FROM audit_receipts WHERE minimum_receipt_sha=?",
        (minimum_receipt_sha,),
    ).fetchone()
    if row is None:
        raise CASIntegrityError("minimum receipt is missing")
    try:
        object_ids = json.loads(row["raw_request_output_cas_hashes"])
    except (TypeError, ValueError) as exc:
        raise CASIntegrityError("minimum receipt CAS set is invalid") from exc
    if (
        not isinstance(object_ids, list)
        or len(set(object_ids)) != len(object_ids)
    ):
        raise CASIntegrityError("minimum receipt CAS set is invalid")
    states = {}
    for object_id in object_ids:
        _require_sha(object_id, "receipt object_id")
        descriptor = verify_object(conn, root, object_id)
        states[object_id] = descriptor["integrity_state"]
    return {
        "minimum_receipt_sha": minimum_receipt_sha,
        "cas_states": states,
    }
