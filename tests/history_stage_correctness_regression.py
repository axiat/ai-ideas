#!/usr/bin/env python3
import os
import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_stage


class HistoryStageCorrectnessRegression(unittest.TestCase):
    def setUp(self):
        self.temporary = pathlib.Path(tempfile.mkdtemp(prefix="history-stage-regression-"))
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.temporary, ignore_errors=True)

    def test_codex_version_identity_does_not_execute_backend(self):
        executable = self.temporary / "codex"
        marker = self.temporary / "executed"
        executable.write_text(
            "#!/bin/sh\n"
            f"touch {marker}\n"
            'echo "codex-cli 0.146.7"\n',
            encoding="utf-8",
        )
        executable.chmod(0o755)
        captured = history_stage._capture_regular_path(executable, 4096)
        with mock.patch.object(
            history_stage.subprocess,
            "run",
            side_effect=AssertionError("backend executed outside containment"),
        ):
            observed = history_stage._detect_codex_cli_version(captured)
        self.assertEqual(observed, "0.146.7")
        self.assertFalse(marker.exists())

    def test_output_root_rejects_authority_overlaps(self):
        input_root = self.temporary / "input"
        output_root = self.temporary / "output"
        input_root.mkdir()
        output_root.mkdir()
        (input_root / "brief.json").write_text("{}", encoding="utf-8")
        manifest = self.temporary / "manifest.json"
        policy = self.temporary / "policy.json"
        database = self.temporary / "history.sqlite3"
        for path in (manifest, policy, database):
            path.write_text("x", encoding="utf-8")
        output = {
            "destination_guard": {"relative": "result.txt"},
        }
        preflight = {"relative": "preflight.json"}
        completion = {"relative": "completion.json"}
        input_descriptor = {
            "path": input_root,
            "resolved": input_root.resolve(),
            "identity": (input_root.stat().st_dev, input_root.stat().st_ino),
        }
        captured_inputs = {"brief": {"source": "brief.json"}}

        def validate(root, manifest_path=manifest, policy_path=policy, history_path=database):
            history_stage._validate_output_isolation(
                {"path": root},
                [output],
                preflight,
                completion,
                input_descriptor,
                captured_inputs,
                manifest_path,
                {"path": str(policy_path)},
                {"database_path": history_path},
            )

        validate(output_root)
        for label, protected in (
            ("input", input_root),
            ("manifest", manifest),
            ("policy", policy),
            ("history", pathlib.Path(str(database) + "-wal")),
        ):
            overlapping = protected.parent
            with self.subTest(label=label), self.assertRaises(
                history_stage.StageError
            ):
                validate(
                    overlapping,
                    manifest_path=manifest,
                    policy_path=policy,
                    history_path=database,
                )

    def test_early_pipe_eof_uses_overall_deadline_and_quiesces_group(self):
        marker = self.temporary / "late-child-write"
        program = (
            "import os,time\n"
            "pid=os.fork()\n"
            "os.close(1); os.close(2)\n"
            "if pid == 0:\n"
            " time.sleep(0.5)\n"
            f" open({str(marker)!r}, 'w').write('escaped')\n"
            "else:\n"
            " time.sleep(5)\n"
        )
        started = time.monotonic()
        with mock.patch.object(history_stage, "PROCESS_TIMEOUT_SECONDS", 0.1):
            with self.assertRaisesRegex(
                history_stage.StageError, "contained backend timed out"
            ):
                history_stage._run_contained(
                    [sys.executable, "-c", program],
                    self.temporary,
                    os.environ.copy(),
                )
        self.assertLess(time.monotonic() - started, 1.5)
        time.sleep(0.6)
        self.assertFalse(marker.exists())

    def test_wal_commit_invalidates_retained_snapshot(self):
        database = self.temporary / "history.sqlite3"
        writer = sqlite3.connect(database)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("CREATE TABLE items(value TEXT)")
        writer.commit()

        uri = database.resolve().as_uri() + "?mode=ro"
        observer = sqlite3.connect(uri, uri=True, isolation_level=None)
        snapshot = sqlite3.connect(uri, uri=True, isolation_level=None)
        snapshot.execute("BEGIN")
        snapshot.execute("SELECT count(*) FROM items").fetchone()
        root_stat = self.temporary.stat()
        db_stat = database.stat()
        authority = {
            "database_path": database,
            "root": {
                "path": self.temporary,
                "identity": (root_stat.st_dev, root_stat.st_ino),
            },
            "identity": (
                db_stat.st_dev,
                db_stat.st_ino,
                db_stat.st_size,
                db_stat.st_mtime_ns,
            ),
            "snapshot_data_version": observer.execute(
                "PRAGMA data_version"
            ).fetchone()[0],
            "_snapshot_connection": snapshot,
            "_observer_connection": observer,
        }
        self.addCleanup(history_stage._close_history_authority, authority)
        writer.execute("INSERT INTO items VALUES ('wal-only-change')")
        writer.commit()
        with mock.patch.object(
            history_stage, "_validate_history_snapshot", return_value=None
        ):
            with self.assertRaisesRegex(
                history_stage.StageError,
                "history store drifted during validation",
            ):
                history_stage._revalidate_history_authority(
                    authority, "generate", {}, {}
                )

    def test_complete_evidence_requires_two_url_bearing_rows(self):
        def markdown(cracks):
            evidence = "\n".join(f"Crack Evidence: {value}" for value in cracks)
            return (
                "Assumption-Removal Attempt: complete I1\n\n"
                "## I1\n"
                "One-Sentence Story: Story.\n"
                "Theme: Evaluation\n"
                "Form: remove-load-bearing-assumption\n"
                "Assumption to Remove: Assumption.\n"
                "Why It Can Be Removed Now: Evidence.\n"
                "Forcing Constraint: Constraint.\n"
                f"{evidence}\n"
                "Summary: Summary.\n"
                "Minimal Falsification Experiment: Experiment.\n"
                "Why It May Be Novel: Novelty.\n"
            )

        invalid_sets = (
            ("https://", "https:///missing-host"),
            ("https://example.com/a", "not-a-url"),
        )
        for cracks in invalid_sets:
            with self.subTest(cracks=cracks), self.assertRaisesRegex(
                history_stage.StageError,
                "assumption-removal crack evidence lacks URLs: I1",
            ):
                history_stage._build_generation_tsv_from_markdown(
                    markdown(cracks)
                )
        projected = history_stage._build_generation_tsv_from_markdown(
            markdown(("https://example.com/a", "https://EXAMPLE.COM/a"))
        )
        self.assertEqual(projected, "I1\tStory.\tEvaluation\n")


if __name__ == "__main__":
    unittest.main()
