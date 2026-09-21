#!/usr/bin/env python3
"""Offline command-line interface for the canonical idea-history store."""

import argparse
import datetime
import json
import os
import pathlib
import re
import stat
import tempfile

try:
    from lib import history_store
    from lib import history_projection
    from lib import history_retrieval
    from lib import history_eval
    from lib import direction_contract
except ImportError:  # Direct execution through lib/history_cli.py.
    import history_store
    import history_projection
    import history_retrieval
    import history_eval
    import direction_contract


def _targets(args):
    return {
        "ledger.tsv": pathlib.Path(args.ledger),
        "tmp/ledger.good": pathlib.Path(args.ledger_good),
    }


def _print(value):
    if type(value) is history_retrieval.VerifiedReceipt:
        value = dict(value.items())
    print(json.dumps(value, sort_keys=True, ensure_ascii=False))


def _fsync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_generation_brief(conn, output, brief):
    """Durably publish a brief without permitting canonical-state overwrite."""
    destination = pathlib.Path(output)
    state_root = history_store._store_state_root(conn)
    history_store._validate_destination(conn, destination, state_root)
    parent = destination.parent
    if parent.is_symlink() or not parent.exists() or not parent.is_dir():
        raise ValueError("generation brief parent must be an existing directory")
    data = history_projection.generation_brief_bytes(brief)
    descriptor, temporary = tempfile.mkstemp(prefix=".%s." % destination.name, dir=str(parent))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        _fsync_directory(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"path": str(destination), "sha256": history_store._sha(data), "byte_count": len(data)}


def write_json_artifact(conn, output, value):
    destination = pathlib.Path(output)
    state_root = history_store._store_state_root(conn)
    history_store._validate_destination(conn, destination, state_root)
    if destination.is_symlink():
        raise ValueError("JSON artifact destination cannot be a symlink")
    parent = destination.parent
    if parent.is_symlink() or not parent.exists() or not parent.is_dir():
        raise ValueError("JSON artifact parent must be an existing directory")
    data = history_retrieval.canonical_bytes(value)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".%s." % destination.name, dir=str(parent)
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        _fsync_directory(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {
        "path": str(destination),
        "sha256": history_store._sha(data),
        "byte_count": len(data),
    }


_REGISTRY_PATH = pathlib.Path(".ai-ideas") / "projects.json"


def _has_control_chars(value):
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)


def _validate_project_entry(name, entry):
    if not isinstance(entry, dict):
        raise ValueError(
            f"project registry is corrupt: entry {name!r} is not an object"
        )
    path = entry.get("path")
    if not isinstance(path, str) or _has_control_chars(path):
        raise ValueError(
            f"project registry is corrupt: entry {name!r} has an invalid path"
        )
    mark = entry.get("last_harvested_sequence")
    if mark is None:
        entry.pop("last_harvested_sequence", None)
    elif type(mark) is not int or mark < 0:
        raise ValueError(
            f"project registry is corrupt: entry {name!r} has an invalid "
            "last_harvested_sequence"
        )


def _load_project_registry():
    if not _REGISTRY_PATH.exists():
        return {"version": 1, "projects": {}}
    try:
        registry = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"project registry is corrupt: {exc}") from None
    if not isinstance(registry, dict):
        raise ValueError("project registry is corrupt: top level is not an object")
    version = registry.get("version")
    if type(version) is not int or version != 1:
        raise ValueError(f"unsupported project registry version: {version!r}")
    if not isinstance(registry.get("projects"), dict):
        raise ValueError("project registry is corrupt: projects is not an object")
    for name, entry in registry["projects"].items():
        _validate_project_entry(name, entry)
    return registry


def _save_project_registry(registry):
    history_store._durable_mkdir(_REGISTRY_PATH.parent)
    data = (
        json.dumps(registry, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    parent = _REGISTRY_PATH.parent
    descriptor, temporary = tempfile.mkstemp(
        prefix=".%s." % _REGISTRY_PATH.name, dir=str(parent)
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, _REGISTRY_PATH)
        _fsync_directory(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _validate_project_name(name):
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or name in (".", ".."):
        raise ValueError(f"invalid project name: {name!r}")


def _unknown_project(name, projects):
    return ValueError(
        f"unknown project {name!r}; registered: "
        f"{', '.join(sorted(projects)) or 'none'}"
    )


def _project_add(name, raw_path):
    _validate_project_name(name)
    if not os.path.isabs(raw_path):
        raise ValueError("project path must be absolute")
    if _has_control_chars(raw_path):
        raise ValueError("project path contains control characters")
    candidate = pathlib.Path(raw_path)
    if candidate.is_symlink():
        raise ValueError("project directory cannot be a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError):
        raise ValueError(f"project directory does not exist: {raw_path}") from None
    if not resolved.is_dir():
        raise ValueError(f"project directory does not exist: {raw_path}")
    checkout = pathlib.Path.cwd().resolve()
    if resolved == checkout or checkout in resolved.parents:
        raise ValueError("project directory cannot be the checkout or inside it")
    direction = resolved / "direction.json"
    try:
        direction_stat = os.lstat(direction)
    except FileNotFoundError:
        raise ValueError("project direction.json is missing") from None
    if stat.S_ISLNK(direction_stat.st_mode):
        raise ValueError("project direction.json cannot be a symlink")
    if not stat.S_ISREG(direction_stat.st_mode):
        raise ValueError("project direction.json is not a regular file")
    _, _, identity = direction_contract.parse_contract_bytes(
        direction.read_bytes()
    )
    registry = _load_project_registry()
    entry = registry["projects"].get(name)
    replacement = {
        "path": str(resolved),
        "added": datetime.date.today().isoformat(),
    }
    if entry is not None and "last_harvested_sequence" in entry:
        replacement["last_harvested_sequence"] = entry["last_harvested_sequence"]
    registry["projects"][name] = replacement
    _save_project_registry(registry)
    return {
        "name": name,
        "path": str(resolved),
        "direction_id": identity["direction_id"],
        "direction_sha256": identity["sha256"],
        "registered": True,
    }


def _project_path(name):
    registry = _load_project_registry()
    projects = registry["projects"]
    entry = projects.get(name)
    if entry is None:
        raise _unknown_project(name, projects)
    return {
        "name": name,
        "path": entry["path"],
        "last_harvested_sequence": entry.get("last_harvested_sequence"),
    }


def _project_list():
    registry = _load_project_registry()
    projects = registry["projects"]
    return {
        "projects": [
            {
                "name": name,
                "path": projects[name]["path"],
                "last_harvested_sequence": projects[name].get(
                    "last_harvested_sequence"
                ),
            }
            for name in sorted(projects)
        ]
    }


def _advance_project_mark(name, sequence):
    registry = _load_project_registry()
    projects = registry["projects"]
    entry = projects.get(name)
    if entry is None:
        raise _unknown_project(name, projects)
    entry["last_harvested_sequence"] = max(
        int(sequence), entry.get("last_harvested_sequence", 0)
    )
    _save_project_registry(registry)
    return entry


def parser():
    result = argparse.ArgumentParser()
    result.add_argument("--db")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    sync = commands.add_parser("sync-ledger")
    sync.add_argument("path")
    append = commands.add_parser("append-tsv")
    append.add_argument("path")
    append.add_argument("--near-sa-json")
    near_sa = commands.add_parser("import-near-sa")
    near_sa.add_argument("path")
    for name in ("materialize-ledger", "reconcile-ledger"):
        projection = commands.add_parser(name)
        projection.add_argument("--ledger", default="ledger.tsv")
        projection.add_argument("--ledger-good", default="tmp/ledger.good")
        projection.add_argument("--state-root", default=".ai-ideas")
    export = commands.add_parser("export-tsv")
    export.add_argument("path")
    slice_export = commands.add_parser(
        "export-slice",
        help=(
            "export ledger rows above a sequence mark as a byte-exact TSV "
            "slice; the slice is a read-only snapshot and re-import is "
            "unsupported"
        ),
        description=(
            "Export ledger rows above a sequence mark as a byte-exact TSV "
            "slice. The slice is a read-only snapshot; re-import is "
            "unsupported."
        ),
    )
    slice_export.add_argument("--after-sequence", required=True, type=int)
    slice_export.add_argument("--dest", required=True)
    slice_export.add_argument("--project")
    commands.add_parser("max-sequence")
    project_add = commands.add_parser("project-add")
    project_add.add_argument("name")
    project_add.add_argument("path")
    project_path = commands.add_parser("project-path")
    project_path.add_argument("name")
    commands.add_parser("project-list")
    project_mark = commands.add_parser(
        "project-mark-advance",
        help=(
            "advance a registered project's harvest mark monotonically; "
            "used after a harvest slice and manifest are durably written"
        ),
    )
    project_mark.add_argument("name")
    project_mark.add_argument("sequence", type=int)
    commands.add_parser("validate")
    for name in ("rebuild-projections", "recover-projections"):
        projection = commands.add_parser(name)
        projection.add_argument(
            "--policy", default="history/retrieval-policy-v1.json"
        )
    brief = commands.add_parser("build-brief")
    brief.add_argument("--policy", default="history/retrieval-policy-v1.json")
    brief.add_argument("--output", default="generation_brief.json")
    brief.add_argument("--research-context")
    brief.add_argument("--divergence-lens", default="")
    retrieve = commands.add_parser("retrieve")
    retrieve.add_argument(
        "--policy", default="history/retrieval-policy-v1.json"
    )
    retrieve.add_argument("--query", required=True)
    retrieve.add_argument("--intent", choices=sorted(history_retrieval.INTENTS), required=True)
    retrieve.add_argument("--output", default="retrieval_pack.json")
    retrieve.add_argument("--expansion-request")
    retrieve.add_argument("--comparator-role", required=True)
    retrieve.add_argument("--comparator-role-identity", required=True)
    finalize = commands.add_parser("finalize-comparison")
    finalize.add_argument(
        "--policy", default="history/retrieval-policy-v1.json"
    )
    finalize.add_argument("--pack", required=True)
    finalize.add_argument("--comparison", required=True)
    finalize.add_argument("--output", default="history_receipt.json")
    replay = commands.add_parser("replay-receipt")
    replay.add_argument(
        "--policy", default="history/retrieval-policy-v1.json"
    )
    replay.add_argument("--pack", required=True)
    replay.add_argument("--receipt", required=True)
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--benchmark", required=True)
    evaluate.add_argument("--output", required=True)
    evaluate.add_argument(
        "--policy", default="history/retrieval-policy-v1.json"
    )
    return result


def main():
    argument_parser = parser()
    args = argument_parser.parse_args()
    if args.command == "evaluate":
        result = history_eval.write_evaluation(
            args.benchmark,
            args.output,
            policy_path=args.policy,
        )
        _print(
            {
                "output": str(pathlib.Path(args.output)),
                "scope": result["scope"],
                "policy_commitment_sha256": result[
                    "policy_commitment_sha256"
                ],
            }
        )
        return
    if args.command in (
        "project-add", "project-path", "project-list", "project-mark-advance"
    ):
        try:
            if args.command == "project-add":
                value = _project_add(args.name, args.path)
            elif args.command == "project-path":
                value = _project_path(args.name)
            elif args.command == "project-mark-advance":
                if args.sequence < 0:
                    raise ValueError("sequence must be nonnegative")
                _advance_project_mark(args.name, args.sequence)
                value = _project_path(args.name)
            else:
                value = _project_list()
        except (ValueError, OSError) as exc:
            argument_parser.error(str(exc))
        _print(value)
        return
    if not args.db:
        argument_parser.error(
            "--db is required for store and retrieval commands"
        )
    conn = history_store.connect(args.db)
    history_store.init_schema(conn)
    try:
        if args.command == "init":
            value = {"initialized": str(pathlib.Path(args.db))}
        elif args.command == "sync-ledger":
            value = history_store.import_tsv_epoch(conn, args.path)
        elif args.command == "append-tsv":
            lines = pathlib.Path(args.path).read_bytes().splitlines()
            if lines and lines[0].split(b"\t") == history_store.HEADER.rstrip(b"\n").split(
                b"\t"
            ):
                lines = lines[1:]
            observations = None
            if args.near_sa_json:
                observations = json.loads(
                    pathlib.Path(args.near_sa_json).read_text(
                        encoding="utf-8"
                    )
                )
                if not isinstance(observations, list):
                    raise ValueError(
                        "near-SA observation input must be a list"
                    )
            value = history_store.append_rows(
                conn,
                lines,
                {"source_path": str(pathlib.Path(args.path).resolve())},
                near_sa_observations=observations,
            )
        elif args.command == "import-near-sa":
            value = history_store.import_near_sa_observations(conn, args.path)
        elif args.command == "materialize-ledger":
            value = history_store.materialize_ledger_projection(
                conn, _targets(args), pathlib.Path(args.state_root)
            )
        elif args.command == "reconcile-ledger":
            value = history_store.reconcile_ledger_projection(
                conn, _targets(args), pathlib.Path(args.state_root)
            )
        elif args.command == "export-tsv":
            value = history_store.export_tsv(conn, args.path)
        elif args.command == "export-slice":
            if args.after_sequence < 0:
                argument_parser.error("--after-sequence must be nonnegative")
            if args.project is not None:
                try:
                    _project_path(args.project)
                except ValueError as exc:
                    argument_parser.error(str(exc))
            value = history_store.export_slice(
                conn, args.after_sequence, args.dest
            )
            if args.project is not None:
                try:
                    _advance_project_mark(args.project, value["max_sequence"])
                except ValueError as exc:
                    argument_parser.error(str(exc))
                value = dict(value, project=args.project)
        elif args.command == "max-sequence":
            value = {"max_sequence": history_store.max_source_sequence(conn)}
        elif args.command == "validate":
            value = history_store.validate_store(conn)
            if not value["ok"]:
                _print(value)
                raise SystemExit(1)
        elif args.command == "rebuild-projections":
            value = history_projection.rebuild(
                conn, history_projection.load_policy(args.policy)
            )
        elif args.command == "recover-projections":
            value = history_projection.recover(
                conn, history_projection.load_policy(args.policy)
            )
        elif args.command == "build-brief":
            research_context = None
            if args.research_context:
                research_context = pathlib.Path(args.research_context).read_text(
                    encoding="utf-8"
                )
            value = history_projection.build_generation_brief(
                conn,
                history_projection.load_policy(args.policy),
                research_context,
                args.divergence_lens,
            )
            publication = write_generation_brief(conn, args.output, value)
            value = dict(value, output=publication["path"])
        elif args.command == "retrieve":
            query = json.loads(pathlib.Path(args.query).read_text(encoding="utf-8"))
            expansion_request = None
            if args.expansion_request:
                expansion_request = json.loads(
                    pathlib.Path(args.expansion_request).read_text(encoding="utf-8")
                )
            value = history_retrieval.build_pack(
                conn,
                query,
                args.intent,
                history_projection.load_policy(args.policy),
                expansion_request=expansion_request,
                comparator_role_bytes=pathlib.Path(
                    args.comparator_role
                ).read_bytes(),
                comparator_role_identity=args.comparator_role_identity,
            )
            publication = write_json_artifact(conn, args.output, value)
            value = dict(value, output=publication["path"])
        elif args.command == "finalize-comparison":
            pack = json.loads(pathlib.Path(args.pack).read_text(encoding="utf-8"))
            comparison = json.loads(
                pathlib.Path(args.comparison).read_text(encoding="utf-8")
            )
            value = history_retrieval.finalize_comparison(
                conn,
                pack,
                comparison,
                history_projection.load_policy(args.policy),
            )
            publication = write_json_artifact(conn, args.output, value)
            value = dict(value, output=publication["path"])
        elif args.command == "replay-receipt":
            pack = json.loads(pathlib.Path(args.pack).read_text(encoding="utf-8"))
            receipt = json.loads(
                pathlib.Path(args.receipt).read_text(encoding="utf-8")
            )
            value = history_retrieval.replay_receipt(
                conn,
                pack,
                receipt,
                history_projection.load_policy(args.policy),
            )
        else:
            raise AssertionError(args.command)
        _print(value)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
