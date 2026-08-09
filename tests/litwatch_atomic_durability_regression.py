#!/usr/bin/env python3
"""Fault-injection regression for durable litwatch JSONL replacement."""
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
import litwatch  # noqa: E402


class LitwatchAtomicDurabilityRegression(unittest.TestCase):
    def test_fsync_failures_leave_only_previous_or_complete_output(self):
        records = [{"id": "arxiv:1"}, {"id": "arxiv:2"}]
        complete = b"".join(
            (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
            for record in records
        )

        for fault_stage in ("file", "parent"):
            with self.subTest(fault_stage=fault_stage), tempfile.TemporaryDirectory() as td:
                target = Path(td) / "index.jsonl"
                previous = b'{"old":true}\n'
                target.write_bytes(previous)
                events = []
                real_fsync = os.fsync
                real_replace = os.replace

                def faulting_fsync(descriptor):
                    kind = "parent" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file"
                    events.append(kind + "-fsync")
                    if kind == fault_stage:
                        raise OSError(kind + " fsync failed")
                    return real_fsync(descriptor)

                def tracking_replace(source, destination):
                    events.append("replace")
                    return real_replace(source, destination)

                with mock.patch.object(litwatch.os, "fsync", side_effect=faulting_fsync), \
                     mock.patch.object(litwatch.os, "replace", side_effect=tracking_replace):
                    with self.assertRaisesRegex(OSError, fault_stage + " fsync failed"):
                        litwatch._atomic_write_jsonl(records, target)

                if fault_stage == "file":
                    self.assertEqual(events, ["file-fsync"])
                    self.assertEqual(target.read_bytes(), previous)
                else:
                    self.assertEqual(events, ["file-fsync", "replace", "parent-fsync"])
                    self.assertEqual(target.read_bytes(), complete)
                self.assertEqual(list(target.parent.glob(".index.jsonl.*.tmp")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
