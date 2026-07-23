#!/usr/bin/env python3
"""Offline command-line interface for the canonical idea-history store."""

import argparse
import json
import pathlib

import history_store
import history_projection


def _targets(args):
    return {
        "ledger.tsv": pathlib.Path(args.ledger),
        "tmp/ledger.good": pathlib.Path(args.ledger_good),
    }


def _print(value):
    print(json.dumps(value, sort_keys=True, ensure_ascii=False))


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
            pathlib.Path(args.output).write_text(
                json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            value = dict(value, output=str(pathlib.Path(args.output)))
        else:
            raise AssertionError(args.command)
        _print(value)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
