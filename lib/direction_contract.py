#!/usr/bin/env python3
"""Closed versioned research-direction contracts and verdict receipts."""

import argparse
import hashlib
import json
import os
import pathlib
import re
import secrets
import stat
import sys
import unicodedata


MAX_CONTRACT_BYTES = 16384
MAX_TEXT_BYTES = 2048
_ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_CANDIDATE_ID_RE = re.compile(r"I[1-9][0-9]*\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_CONTRACT_FIELDS = {
    "schema_version",
    "direction_id",
    "statement",
    "all_candidates_must_match",
    "allowed_axes",
    "target_failures",
    "fixed_constraints",
    "excluded_scopes",
}
_VERDICT_HEADER = ("id", "direction-fit", "direction-evidence")
_VERDICT_FITS = {"in-scope", "out-of-scope"}


class DirectionContractError(ValueError):
    pass


def _error(message):
    raise DirectionContractError(message)


def _utf8_length(value):
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise DirectionContractError("value is not UTF-8 encodable") from exc


def _has_control(value):
    return any(unicodedata.category(character) == "Cc" for character in value)


def _validate_text(value, label, maximum=MAX_TEXT_BYTES):
    if not isinstance(value, str) or not value:
        _error("%s must be a nonempty string" % label)
    if _has_control(value):
        _error("%s contains a control character" % label)
    if _utf8_length(value) > maximum:
        _error("%s exceeds the byte limit" % label)


def _validate_id(value, label):
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        _error("%s is not a valid ID" % label)
    if _utf8_length(value) > 96:
        _error("%s exceeds the byte limit" % label)


def _validate_candidate_id(value, label="candidate ID"):
    if not isinstance(value, str) or not _CANDIDATE_ID_RE.fullmatch(value):
        _error("%s is not a valid candidate ID" % label)


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _error("duplicate JSON object key: %s" % key)
        result[key] = value
    return result


def canonical_bytes(value):
    """Return sorted compact UTF-8 JSON with one trailing LF."""
    try:
        text = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return (text + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise DirectionContractError("value cannot be canonicalized") from exc


def _validate_enum_entries(value, field):
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        _error("%s must contain 1 to 16 entries" % field)
    ids = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"id", "description"}:
            _error("%s entries must contain only id and description" % field)
        _validate_id(entry["id"], "%s ID" % field)
        _validate_text(entry["description"], "%s description" % field)
        if entry["id"] in ids:
            _error("%s IDs must be unique" % field)
        ids.add(entry["id"])


def _validate_text_list(value, field):
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        _error("%s must contain 1 to 32 entries" % field)
    entries = set()
    for entry in value:
        _validate_text(entry, field)
        if entry in entries:
            _error("%s entries must be unique" % field)
        entries.add(entry)


def _validate_contract(value):
    if not isinstance(value, dict) or set(value) != _CONTRACT_FIELDS:
        _error("contract fields do not match the closed schema")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        _error("schema_version must be integer 1")
    _validate_id(value["direction_id"], "direction_id")
    _validate_text(value["statement"], "statement")
    if value["all_candidates_must_match"] is not True:
        _error("all_candidates_must_match must be true")
    _validate_enum_entries(value["allowed_axes"], "allowed_axes")
    _validate_enum_entries(value["target_failures"], "target_failures")
    _validate_text_list(value["fixed_constraints"], "fixed_constraints")
    _validate_text_list(value["excluded_scopes"], "excluded_scopes")


def parse_contract_bytes(raw):
    """Return (contract, canonical_raw, identity)."""
    if not isinstance(raw, bytes):
        _error("contract input must be bytes")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DirectionContractError("contract is not valid UTF-8 JSON") from exc
    _validate_contract(value)
    canonical_raw = canonical_bytes(value)
    if len(canonical_raw) > MAX_CONTRACT_BYTES:
        _error("canonical contract exceeds the byte limit")
    return (
        value,
        canonical_raw,
        {
            "direction_id": value["direction_id"],
            "sha256": hashlib.sha256(canonical_raw).hexdigest(),
        },
    )


def _relative_components(source):
    if not isinstance(source, str) or not source:
        _error("contract source must be a nonempty relative path")
    raw_components = source.split(os.sep)
    if any(part in ("", ".", "..") for part in raw_components):
        _error("contract source has an unsafe path component")
    path = pathlib.PurePath(source)
    if path.is_absolute() or not path.parts:
        _error("contract source must be a relative path")
    return path.parts


def _open_contract(source, repo_root):
    components = _relative_components(source)
    root = pathlib.Path(repo_root)
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        _error("safe directory descriptors are unavailable")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW
    directory_fd = None
    descriptor = None
    try:
        directory_fd = os.open(str(root), directory_flags)
        for component in components[:-1]:
            next_directory_fd = os.open(
                component, directory_flags, dir_fd=directory_fd
            )
            previous_directory_fd = directory_fd
            directory_fd = next_directory_fd
            os.close(previous_directory_fd)
        descriptor = os.open(
            components[-1], file_flags, dir_fd=directory_fd
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            _error("opened contract source is invalid")
        if opened.st_size > MAX_CONTRACT_BYTES:
            _error("opened contract source exceeds the byte limit")
        chunks = []
        remaining = MAX_CONTRACT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(raw) > MAX_CONTRACT_BYTES or after.st_size != opened.st_size:
            _error("contract source changed during read")
        return raw
    except OSError as exc:
        raise DirectionContractError("contract source could not be opened") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory_fd is not None:
            os.close(directory_fd)


def load_contract(source, repo_root):
    """Safely read a repository-relative contract and return the same tuple."""
    return parse_contract_bytes(_open_contract(source, repo_root))


def _check_new_snapshot_destination(destination):
    path = pathlib.Path(destination)
    parent = path.parent
    try:
        parent_stat = os.stat(str(parent), follow_symlinks=False)
    except OSError as exc:
        raise DirectionContractError("snapshot parent is unavailable") from exc
    if not stat.S_ISDIR(parent_stat.st_mode) or stat.S_ISLNK(parent_stat.st_mode):
        _error("snapshot parent must be a real directory")
    try:
        os.lstat(str(path))
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise DirectionContractError("snapshot destination cannot be checked") from exc
    else:
        _error("snapshot destination already exists")
    return path, parent


def _atomic_write_new(destination, raw):
    path, parent = _check_new_snapshot_destination(destination)

    temporary = parent / (".%s.%s.tmp" % (path.name, secrets.token_hex(16)))
    try:
        descriptor = os.open(
            str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        try:
            total = 0
            while total < len(raw):
                total += os.write(descriptor, raw[total:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(str(temporary), str(path))
        directory = os.open(str(parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        try:
            os.unlink(str(temporary))
        except OSError:
            pass
        raise DirectionContractError("atomic snapshot write failed") from exc


def write_snapshot(source, repo_root, output_path, identity_path):
    """Atomically write a canonical contract and identity; source=None writes null identity."""
    if source is None:
        if output_path is not None:
            _error("snapshot output requires a contract source")
        _atomic_write_new(identity_path, b"null\n")
        return None
    contract, raw, identity = load_contract(source, repo_root)
    if pathlib.Path(output_path) == pathlib.Path(identity_path):
        _error("snapshot contract and identity destinations must differ")
    _check_new_snapshot_destination(output_path)
    _check_new_snapshot_destination(identity_path)
    _atomic_write_new(output_path, raw)
    _atomic_write_new(identity_path, canonical_bytes(identity))
    return contract, raw, identity


def validate_identity(value):
    """Return None or an exact direction_id/SHA-256 identity object."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"direction_id", "sha256"}:
        _error("identity fields do not match the closed schema")
    _validate_id(value["direction_id"], "identity direction_id")
    if not isinstance(value["sha256"], str) or not _SHA256_RE.fullmatch(value["sha256"]):
        _error("identity sha256 is invalid")
    return value


def _validate_evidence(value, label):
    _validate_text(value, label)
    if "\t" in value:
        _error("%s contains a tab" % label)


def validate_candidate_fields(values, contract, candidate_id):
    """Validate Direction Axis, Target Failure, and Direction Evidence."""
    _validate_candidate_id(candidate_id)
    _validate_contract(contract)
    if not isinstance(values, dict):
        _error("candidate fields must be an object")
    expected = {"Direction Axis", "Target Failure", "Direction Evidence"}
    if not expected.issubset(values):
        _error("candidate direction fields are incomplete")
    axes = {item["id"] for item in contract["allowed_axes"]}
    failures = {item["id"] for item in contract["target_failures"]}
    if values["Direction Axis"] not in axes:
        _error("candidate %s has an invalid Direction Axis" % candidate_id)
    if values["Target Failure"] not in failures:
        _error("candidate %s has an invalid Target Failure" % candidate_id)
    _validate_evidence(values["Direction Evidence"], "Direction Evidence")


def _validate_candidate_ids(candidate_ids):
    if not isinstance(candidate_ids, (list, tuple)) or not candidate_ids:
        _error("candidate IDs must be a nonempty ordered list")
    result = list(candidate_ids)
    for candidate_id in result:
        _validate_candidate_id(candidate_id)
    if len(set(result)) != len(result):
        _error("candidate IDs must be unique")
    return result


def parse_direction_verdicts(raw, candidate_ids):
    """Validate the exact header, ordered ID coverage, enums, and evidence."""
    expected_ids = _validate_candidate_ids(candidate_ids)
    if not isinstance(raw, bytes):
        _error("direction verdict input must be bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DirectionContractError("direction verdicts are not UTF-8") from exc
    if not text.endswith("\n") or "\r" in text:
        _error("direction verdicts must use LF-terminated rows")
    rows = text[:-1].split("\n")
    if not rows or tuple(rows[0].split("\t")) != _VERDICT_HEADER:
        _error("direction verdict header is invalid")
    if len(rows) - 1 != len(expected_ids):
        _error("direction verdict coverage is incomplete")
    verdicts = []
    for index, row in enumerate(rows[1:]):
        columns = row.split("\t")
        if len(columns) != 3:
            _error("direction verdict rows must have exactly three columns")
        candidate_id, direction_fit, evidence = columns
        if candidate_id != expected_ids[index]:
            _error("direction verdict IDs are not in candidate order")
        if direction_fit not in _VERDICT_FITS:
            _error("direction verdict fit is invalid")
        _validate_evidence(evidence, "direction verdict evidence")
        verdicts.append(
            {
                "candidate_id": candidate_id,
                "direction_fit": direction_fit,
                "evidence": evidence,
            }
        )
    return verdicts


def require_all_in_scope(raw, candidate_ids):
    """Return parsed verdicts or raise when any verdict is out-of-scope."""
    verdicts = parse_direction_verdicts(raw, candidate_ids)
    if any(item["direction_fit"] != "in-scope" for item in verdicts):
        _error("every candidate must be in scope")
    return verdicts


def _parse_idea_ids(raw):
    if not isinstance(raw, bytes):
        _error("ideas input must be bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DirectionContractError("ideas input is not UTF-8") from exc
    if not text.endswith("\n") or "\r" in text:
        _error("ideas input must use LF-terminated rows")
    rows = text[:-1].split("\n")
    if not rows or any(not row for row in rows):
        _error("ideas input must not contain blank rows")
    candidate_ids = []
    for row in rows:
        columns = row.split("\t")
        if len(columns) != 3:
            _error("ideas rows must have exactly three columns")
        _validate_candidate_id(columns[0], "ideas candidate ID")
        candidate_ids.append(columns[0])
    return _validate_candidate_ids(candidate_ids)


def _command_snapshot(arguments):
    if arguments.source is None:
        if arguments.output is not None:
            _error("snapshot output requires --source")
        write_snapshot(None, arguments.repo_root, None, arguments.identity_output)
        return
    if arguments.output is None:
        _error("snapshot --source requires --output")
    write_snapshot(
        arguments.source,
        arguments.repo_root,
        arguments.output,
        arguments.identity_output,
    )


def _command_validate_verdicts(arguments):
    contract_path = pathlib.Path(arguments.contract)
    if contract_path.is_absolute():
        if not contract_path.name:
            _error("contract path must name a regular file")
        contract, _, identity = load_contract(
            contract_path.name, contract_path.parent
        )
    else:
        contract, _, identity = load_contract(
            contract_path.as_posix(), pathlib.Path.cwd()
        )
    candidate_ids = _parse_idea_ids(pathlib.Path(arguments.ideas).read_bytes())
    verdicts = require_all_in_scope(
        pathlib.Path(arguments.verdicts).read_bytes(), candidate_ids
    )
    receipt = {
        "schema_version": 1,
        "direction": identity,
        "candidate_count": len(candidate_ids),
        "verdicts": verdicts,
    }
    _atomic_write_new(arguments.output, canonical_bytes(receipt))


def main(argv=None):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--repo-root", required=True)
    snapshot.add_argument("--identity-output", required=True)
    snapshot.add_argument("--source")
    snapshot.add_argument("--output")
    validate = commands.add_parser("validate-verdicts")
    validate.add_argument("--contract", required=True)
    validate.add_argument("--ideas", required=True)
    validate.add_argument("--verdicts", required=True)
    validate.add_argument("--output", required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "snapshot":
            _command_snapshot(arguments)
        else:
            _command_validate_verdicts(arguments)
    except (DirectionContractError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
