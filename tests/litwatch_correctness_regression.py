#!/usr/bin/env python3
"""Focused regressions for litwatch annotation admission and output durability."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
import litwatch  # noqa: E402


class LitwatchCorrectnessRegression(unittest.TestCase):
    def test_invalid_annotation_does_not_suppress_later_valid_one(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            staging = root / "staging.jsonl"
            annotations = root / "annotations.jsonl"
            drops_path = root / "drops.jsonl"
            staging.write_text(
                json.dumps({"id": "arxiv:1", "source": "arxiv", "title": "one"}) + "\n",
                encoding="utf-8",
            )
            annotations.write_text(
                json.dumps({"id": "arxiv:1", "note": 42}) + "\n"
                + json.dumps({"id": "arxiv:1", "theme": "robotics", "note": "valid neighbor"}) + "\n",
                encoding="utf-8",
            )

            records, drops = litwatch.ingest(staging, annotations, drops_path)

            self.assertEqual(records[0]["agy_note"], "valid neighbor")
            self.assertEqual(records[0]["theme"], "robotics")
            self.assertEqual([drop["reason"] for drop in drops], ["malformed"])
            self.assertEqual(
                [json.loads(line) for line in drops_path.read_text(encoding="utf-8").splitlines()],
                drops,
            )

    def test_failed_index_emit_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "index.jsonl"
            previous = b'{"old":true}\n'
            target.write_bytes(previous)

            with self.assertRaises(TypeError):
                litwatch._emit([{"id": "ok"}, {"bad": object()}], target)

            self.assertEqual(target.read_bytes(), previous)
            self.assertEqual(list(target.parent.glob(".index.jsonl.*.tmp")), [])

    def test_failed_drop_emit_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            staging = root / "staging.jsonl"
            annotations = root / "annotations.jsonl"
            drops_path = root / "drops.jsonl"
            staging.write_text('{"id":"arxiv:1","title":"one"}\n', encoding="utf-8")
            annotations.write_text("not-json\n", encoding="utf-8")
            previous = b'{"old":"drop"}\n'
            drops_path.write_bytes(previous)

            with mock.patch.object(litwatch.json, "dumps", side_effect=RuntimeError("emit failed")):
                with self.assertRaisesRegex(RuntimeError, "emit failed"):
                    litwatch.ingest(staging, annotations, drops_path)

            self.assertEqual(drops_path.read_bytes(), previous)
            self.assertEqual(list(root.glob(".drops.jsonl.*.tmp")), [])

    def test_successful_emit_fsyncs_and_replaces_sibling_temp(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "index.jsonl"
            target.write_text("old\n", encoding="utf-8")
            real_replace = os.replace
            real_fsync = os.fsync
            with mock.patch.object(litwatch.os, "replace", wraps=real_replace) as replace, \
                 mock.patch.object(litwatch.os, "fsync", wraps=real_fsync) as fsync:
                litwatch._emit([{"id": "arxiv:1"}], target)

            temp_arg, target_arg = replace.call_args.args
            self.assertEqual(Path(temp_arg).parent, target.parent)
            self.assertEqual(Path(target_arg), target)
            self.assertGreaterEqual(fsync.call_count, 1)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"id": "arxiv:1"})

    def test_custom_litwatch_dir_is_injected_into_backend_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output_dir = root / "custom-output"
            staging = root / "prebuilt.jsonl"
            capture = root / "prompt.txt"
            backend = root / "backend.sh"
            staging.write_text(
                '{"id":"arxiv:1","source":"arxiv","title":"one","abstract":"","url":"u","date":""}\n',
                encoding="utf-8",
            )
            backend.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$1\" > \"$PROMPT_CAPTURE\"\n"
                "printf '%s\\n' '{\"id\":\"arxiv:1\",\"note\":\"neighbor\"}' > \"$AGY_OUT_HINT/annotations.jsonl\"\n",
                encoding="utf-8",
            )
            backend.chmod(0o755)
            env = os.environ.copy()
            env.update({
                "LITWATCH_DIR": str(output_dir),
                "LITWATCH_PREBUILT_STAGING": str(staging),
                "LITWATCH_CMD": str(backend),
                "LITWATCH_FETCH_GAP": "0",
                "PROMPT_CAPTURE": str(capture),
            })

            completed = subprocess.run(
                [str(REPO / "litwatch.sh")], cwd=REPO, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            prompt = capture.read_text(encoding="utf-8")
            self.assertIn(str(output_dir / "agy" / "staging.jsonl"), prompt)
            self.assertIn(str(output_dir / "agy" / "annotations.jsonl"), prompt)
            self.assertNotIn("tmp/litwatch", prompt)
            role = (REPO / "roles" / "litwatch.md").read_text(encoding="utf-8")
            self.assertNotIn("tmp/litwatch", role)
            record = json.loads((output_dir / "index.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(record["agy_note"], "neighbor")


if __name__ == "__main__":
    unittest.main(verbosity=2)
