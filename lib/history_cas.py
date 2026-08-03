#!/usr/bin/env python3
"""Durable content-addressed storage foundation for history audit v2."""

import datetime
import hashlib
import os
import pathlib
import secrets
import sqlite3
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
    return pathlib.Path(object_id[:2]) / (object_id[2:] + ".zlib")


def _ensure_directory(path):
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise CASError("CAS directory is not a real directory")


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
    root = pathlib.Path(root)
    _ensure_directory(root)
    relative = _relative_path(object_id)
    parent = root / relative.parent
    _ensure_directory(parent)
    target = root / relative
    if target.exists():
        if target.is_symlink() or not target.is_file():
            raise CASIntegrityError("CAS target is not a regular file")
        if target.read_bytes() != compressed:
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
            if target.read_bytes() != compressed:
                raise CASIntegrityError(
                    "concurrent CAS publish conflicts with object identity"
                )
        else:
            os.replace(str(temporary), str(target))
            _fsync_directory(parent)
    finally:
        if temporary.exists():
            temporary.unlink()
    return relative.as_posix()


def _descriptor_material(raw, compressed, retention_profile, relative_path):
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
        "expires_at": None,
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
        "integrity_state",
    ):
        if row[field] != expected[field]:
            raise CASIntegrityError(f"CAS descriptor mismatch: {field}")


def put_object(conn, root, raw, retention_profile, *, pin_reason=None):
    """Compress, fsync, publish, describe, and optionally pin one CAS object."""
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3 connection")
    if conn.in_transaction:
        raise CASError("CAS publication requires an idle connection")
    if not isinstance(raw, bytes):
        raise CASError("CAS payload must be bytes")
    if not isinstance(retention_profile, str) or not retention_profile:
        raise CASError("retention_profile must be a non-empty string")
    if pin_reason is not None and (
        not isinstance(pin_reason, str) or not pin_reason
    ):
        raise CASError("pin_reason must be a non-empty string")

    compressed = zlib.compress(raw, level=9)
    object_id = _sha256(raw)
    relative_path = _publish(root, object_id, compressed)
    expected = _descriptor_material(
        raw, compressed, retention_profile, relative_path
    )
    row = conn.execute(
        "SELECT * FROM audit_cas_objects WHERE object_id=?", (object_id,)
    ).fetchone()
    if row is None:
        try:
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
        except sqlite3.DatabaseError as exc:
            raise CASError("CAS descriptor persistence failed") from exc
        row = conn.execute(
            "SELECT * FROM audit_cas_objects WHERE object_id=?", (object_id,)
        ).fetchone()
    _assert_descriptor_matches(row, expected)

    if pin_reason is not None:
        conn.execute(
            "INSERT OR IGNORE INTO audit_cas_pins(object_id, pin_reason, pinned_at) "
            "VALUES(?, ?, ?)",
            (object_id, pin_reason, _utc_now()),
        )
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
    expected_relative = _relative_path(object_id).as_posix()
    if descriptor["relative_path"] != expected_relative:
        raise CASIntegrityError("CAS descriptor path is invalid")
    path = pathlib.Path(root) / expected_relative
    if not path.exists() or path.is_symlink() or not path.is_file():
        raise CASIntegrityError("CAS payload is missing")
    compressed = path.read_bytes()
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
    return descriptor


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
        conn.execute("COMMIT")
    except Exception as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if isinstance(exc, CASError):
            raise
        raise CASError("minimum receipt persistence failed") from exc
    return normalized["minimum_receipt_sha"]
