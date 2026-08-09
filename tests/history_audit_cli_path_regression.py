#!/usr/bin/env python3
"""Regression tests for audit CLI path identity checks."""

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = ROOT / "lib/history_audit_cli.py"


class HistoryAuditCliPathRegression(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.plan = self.root / "plan.json"
        self.plan.write_bytes(b"{}\n")

    def tearDown(self):
        self.temporary.cleanup()

    def _run(self, *arguments):
        return subprocess.run(
            [sys.executable, str(CLI), *map(str, arguments)],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=8,
            check=False,
        )

    def _assert_alias_rejected(self, completed, message):
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(
            completed.stderr,
            f"history-audit: run: invalid: {message}\n".encode("utf-8"),
        )

    def test_state_and_receipt_resolved_alias_are_rejected(self):
        destination = self.root / "execution.json"
        state = self.root / "missing-parent" / ".." / destination.name
        completed = self._run(
            "run",
            "--plan",
            self.plan,
            "--state",
            state,
            "--receipt",
            destination,
        )

        self._assert_alias_rejected(
            completed,
            "--state and --receipt paths must be distinct",
        )
        self.assertFalse(destination.exists())
        self.assertEqual(self.plan.read_bytes(), b"{}\n")

    def test_state_and_database_samefile_alias_are_rejected(self):
        database = self.root / "history.sqlite3"
        database.write_bytes(b"database sentinel")
        state = self.root / "state.json"
        os.link(database, state)

        completed = self._run(
            "run",
            "--plan",
            self.plan,
            "--state",
            state,
            "--db",
            database,
        )

        self._assert_alias_rejected(
            completed,
            "--state and --db paths must be distinct",
        )
        self.assertEqual(database.read_bytes(), b"database sentinel")
        self.assertEqual(state.read_bytes(), b"database sentinel")


if __name__ == "__main__":
    unittest.main()
