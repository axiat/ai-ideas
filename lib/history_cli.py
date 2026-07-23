#!/usr/bin/env python3
"""Offline command-line interface for the canonical idea-history store."""

import argparse
import json
import os
import pathlib
import tempfile

try:
    from lib import history_store
    from lib import history_projection
except ImportError:  # Direct execution through lib/history_cli.py.
    import history_store
    import history_projection


def _targets(args):
    return {
        "ledger.tsv": pathlib.Path(args.ledger),
        "tmp/ledger.good": pathlib.Path(args.ledger_good),
    }


def _print(value):
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
    protected = {
        (pathlib.Path.cwd() / "ledger.tsv").resolve(),
        (pathlib.Path.cwd() / "tmp" / "ledger.good").resolve(),
        (state_root.parent / "ledger.tsv").resolve(),
        (state_root.parent / "tmp" / "ledger.good").resolve(),
    }
    if destination.resolve() in protected:
        raise ValueError("generation brief cannot replace a ledger projection")
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


def parser():
    result = argparse.ArgumentParser()
    result.add_argument("--db", required=True)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    sync = commands.add_parser("sync-ledger")
    sync.add_argument("path")
    append = commands.add_parser("append-tsv")
    append.add_argument("path")
    near_sa = commands.add_parser("import-near-sa")
    near_sa.add_argument("path")
    for name in ("materialize-ledger", "reconcile-ledger"):
        projection = commands.add_parser(name)
        projection.add_argument("--ledger", default="ledger.tsv")
        projection.add_argument("--ledger-good", default="tmp/ledger.good")
        projection.add_argument("--state-root", default=".ai-ideas")
    export = commands.add_parser("export-tsv")
    export.add_argument("path")
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
    return result


def main():
    args = parser().parse_args()
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
            value = history_store.append_rows(
                conn, lines, {"source_path": str(pathlib.Path(args.path).resolve())}
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
                conn, history_projection.load_policy(args.policy), research_context
            )
            publication = write_generation_brief(conn, args.output, value)
            value = dict(value, output=publication["path"])
        else:
            raise AssertionError(args.command)
        _print(value)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
