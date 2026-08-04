#!/usr/bin/env python3
"""Deterministic stdio provider used only by offline audit-v2 tests."""

import hashlib
import json
import os
import pathlib
import signal
import subprocess
import sys
import time


PROTOCOL_REVISION = "history-audit-test-provider-stdio-v1"


def _pairs_without_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate field")
        value[key] = item
    return value


def _canonical_bytes(value):
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def main():
    try:
        mode = os.environ.get("HISTORY_AUDIT_TEST_FIXTURE_MODE")
        if mode == "fail":
            return 70
        if mode == "interrupt-spawn-child":
            marker = os.environ.get("HISTORY_AUDIT_TEST_FIXTURE_CHILD_MARKER")
            if not marker:
                return 71
            pathlib.Path(marker + ".pid").write_text(
                str(os.getpgrp()), encoding="utf-8"
            )
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import pathlib,time; time.sleep(1); "
                        "pathlib.Path(%r).write_text('orphan', encoding='utf-8')"
                    )
                    % marker,
                ]
            )
            time.sleep(0.2)
            os.kill(os.getppid(), signal.SIGINT)
            time.sleep(30)
            return 0
        if mode == "overflow-spawn-child":
            marker = os.environ.get("HISTORY_AUDIT_TEST_FIXTURE_CHILD_MARKER")
            if not marker:
                return 71
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import pathlib,time; time.sleep(0.5); "
                        "pathlib.Path(%r).write_text('orphan', encoding='utf-8')"
                    )
                    % marker,
                ]
            )
            sys.stdout.buffer.write(b"x" * (4 * 1024 * 1024 + 1))
            sys.stdout.buffer.flush()
            return 0
        if mode == "timeout-spawn-child":
            marker = os.environ.get("HISTORY_AUDIT_TEST_FIXTURE_CHILD_MARKER")
            if not marker:
                return 71
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import pathlib,time; time.sleep(12); "
                        "pathlib.Path(%r).write_text('orphan', encoding='utf-8')"
                    )
                    % marker,
                ]
            )
            time.sleep(30)
            return 0
        raw = sys.stdin.buffer.read()
        request = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=lambda _: (_ for _ in ()).throw(
                ValueError("non-finite number")
            ),
        )
        if raw != _canonical_bytes(request)[:-1]:
            return 64
        if request.get("schema_version") != "history-audit-map-request-v1":
            return 65
        snapshot = request["snapshot"]
        output_items = []
        for wrapper in request["items"]:
            item_id = wrapper["item_id"]
            record = wrapper["record"]
            content = record["content"]
            quote = content[:5]
            output_items.append(
                {
                    "item_id": item_id,
                    "semantic_relation": "distinct",
                    "lineage_relation": "none",
                    "anchor": {
                        "asset_id": item_id,
                        "artifact_sha": record["artifact_sha"],
                        "start": 0,
                        "end": len(quote),
                        "quote": quote,
                    },
                }
            )
        output = {
            "schema_version": "history-map-output-v1",
            "snapshot_id": snapshot["snapshot_id"],
            "snapshot_hash": snapshot["snapshot_hash"],
            "truncated": False,
            "items": output_items,
        }
        log_path = os.environ.get("HISTORY_AUDIT_FAKE_PROVIDER_LOG")
        if log_path:
            record = {
                "protocol_revision": PROTOCOL_REVISION,
                "request_sha256": hashlib.sha256(raw).hexdigest(),
            }
            with open(log_path, "ab") as stream:
                stream.write(_canonical_bytes(record))
        sys.stdout.buffer.write(_canonical_bytes(output))
        return 0
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return 66


if __name__ == "__main__":
    raise SystemExit(main())
