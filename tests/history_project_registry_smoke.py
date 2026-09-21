#!/usr/bin/env python3
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CLI = ROOT / "lib" / "history_cli.py"
DIRECTION_SOURCE = ROOT / "directions" / "dynamic-memory-vla-v1.json"

HEADER = b"date\tsource\ttheme\tidea\tverdict\treason\toverlap\tcategory\n"


def row(story, verdict="accept-w-rev", overlap="low", category="design-fixable"):
    return (
        "2026-09-21\thunt\tEvaluation and Diagnostics\t"
        + story
        + "\t"
        + verdict
        + "\treason\t"
        + overlap
        + "\t"
        + category
    ).encode("utf-8")


class HistoryProjectRegistrySmoke(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.checkout = pathlib.Path(self.temp.name) / "checkout"
        self.checkout.mkdir()
        self.external_root = pathlib.Path(self.temp.name) / "external"
        self.external_root.mkdir()
        self.project = self.external_root / "mytopic"
        self.project.mkdir()
        shutil.copyfile(DIRECTION_SOURCE, self.project / "direction.json")
        self.registry_path = self.checkout / ".ai-ideas" / "projects.json"

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(CLI), *[str(arg) for arg in args]],
            cwd=str(self.checkout),
            capture_output=True,
            text=True,
        )

    def _add(self, name="mytopic", path=None):
        return self.run_cli("project-add", name, path or self.project)

    def _read_registry(self):
        return json.loads(self.registry_path.read_text(encoding="utf-8"))

    def _make_project(self, name):
        path = self.external_root / name
        path.mkdir()
        shutil.copyfile(DIRECTION_SOURCE, path / "direction.json")
        return path

    def test_add_accepts_valid_project(self):
        result = self._add()
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["name"], "mytopic")
        self.assertEqual(value["path"], str(self.project.resolve()))
        self.assertEqual(value["direction_id"], "dynamic-memory-vla-v1")
        self.assertTrue(value["registered"])
        self.assertEqual(len(value["direction_sha256"]), 64)
        entry = self._read_registry()["projects"]["mytopic"]
        self.assertEqual(entry["path"], str(self.project.resolve()))
        self.assertNotIn("last_harvested_sequence", entry)

    def test_add_rejects_relative_path(self):
        result = self._add(path="relative/dir")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.registry_path.exists())

    def test_add_rejects_invalid_name(self):
        for bad in ("a/b", "a b"):
            result = self._add(name=bad)
            self.assertEqual(result.returncode, 2, bad)
        self.assertFalse(self.registry_path.exists())

    def test_add_rejects_nonexistent_dir(self):
        result = self._add(path=self.external_root / "missing")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.registry_path.exists())

    def test_add_rejects_symlinked_dir(self):
        link = self.external_root / "linked"
        os.symlink(str(self.project), link)
        result = self._add(path=link)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.registry_path.exists())

    def test_add_rejects_checkout_and_in_checkout_dirs(self):
        result = self._add(path=self.checkout)
        self.assertEqual(result.returncode, 2)
        inside = self.checkout / "inside"
        inside.mkdir()
        shutil.copyfile(DIRECTION_SOURCE, inside / "direction.json")
        result = self._add(path=inside)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.registry_path.exists())

    def test_add_rejects_missing_direction(self):
        empty = self.external_root / "empty"
        empty.mkdir()
        result = self._add(path=empty)
        self.assertEqual(result.returncode, 2)
        self.assertIn("direction.json is missing", result.stderr)
        self.assertFalse(self.registry_path.exists())

    def test_add_rejects_symlinked_direction(self):
        linked = self.external_root / "linkeddir"
        linked.mkdir()
        os.symlink(str(self.project / "direction.json"), linked / "direction.json")
        result = self._add(path=linked)
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot be a symlink", result.stderr)
        self.assertFalse(self.registry_path.exists())

    def test_add_rejects_contract_invalid_direction(self):
        broken = self.external_root / "broken"
        broken.mkdir()
        (broken / "direction.json").write_bytes(b"not json{")
        result = self._add(path=broken)
        self.assertEqual(result.returncode, 2)
        self.assertIn("contract is not valid UTF-8 JSON", result.stderr)
        self.assertFalse(self.registry_path.exists())

    def test_path_unknown_lists_registered(self):
        self.assertEqual(self._add().returncode, 0)
        result = self.run_cli("project-path", "other")
        self.assertEqual(result.returncode, 2)
        self.assertIn("mytopic", result.stderr)

    def test_path_prints_null_mark_before_first_harvest(self):
        self.assertEqual(self._add().returncode, 0)
        result = self.run_cli("project-path", "mytopic")
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["name"], "mytopic")
        self.assertEqual(value["path"], str(self.project.resolve()))
        self.assertIsNone(value["last_harvested_sequence"])

    def test_list_enumerates_sorted(self):
        for name in ("zeta", "alpha", "mid"):
            self.assertEqual(self._add(name=name, path=self._make_project(name)).returncode, 0)
        result = self.run_cli("project-list")
        self.assertEqual(result.returncode, 0, result.stderr)
        names = [entry["name"] for entry in json.loads(result.stdout)["projects"]]
        self.assertEqual(names, ["alpha", "mid", "zeta"])

    def test_unknown_version_fails_closed(self):
        self.assertEqual(self._add().returncode, 0)
        registry = self._read_registry()
        before = self.registry_path.read_bytes()
        registry["version"] = 2
        self.registry_path.write_text(json.dumps(registry), encoding="utf-8")
        self.assertEqual(self._add(name="two", path=self._make_project("two")).returncode, 2)
        self.assertEqual(self.run_cli("project-path", "mytopic").returncode, 2)
        self.assertEqual(self.run_cli("project-list").returncode, 2)
        registry["version"] = 1
        self.assertEqual(
            json.loads(self.registry_path.read_text(encoding="utf-8"))["projects"],
            json.loads(before.decode("utf-8"))["projects"],
        )

    def _init_db_with_rows(self):
        (self.checkout / "ledger.instance-id").write_text(
            "test-ledger-instance\n", encoding="utf-8"
        )
        ledger = self.checkout / "ledger.tsv"
        ledger.write_bytes(
            HEADER
            + row("registry alpha proposition")
            + b"\n"
            + row("registry beta proposition", verdict="accept")
            + b"\n"
        )
        db = ".ai-ideas/history.sqlite3"
        result = self.run_cli("--db", db, "init")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli("--db", db, "sync-ledger", "ledger.tsv")
        self.assertEqual(result.returncode, 0, result.stderr)
        return db

    def test_export_slice_project_advances_mark(self):
        db = self._init_db_with_rows()
        self.assertEqual(self._add(name="p1").returncode, 0)
        harvest = self.project / "harvest"
        first = harvest / "slice1.tsv"
        result = self.run_cli(
            "--db", db, "export-slice",
            "--after-sequence", 0, "--dest", first, "--project", "p1",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["row_count"], 2)
        self.assertEqual(value["max_sequence"], 2)
        self.assertEqual(value["project"], "p1")
        entry = self._read_registry()["projects"]["p1"]
        self.assertEqual(entry["last_harvested_sequence"], value["max_sequence"])
        shown = json.loads(self.run_cli("project-path", "p1").stdout)
        self.assertEqual(shown["last_harvested_sequence"], value["max_sequence"])
        second = harvest / "slice2.tsv"
        result = self.run_cli(
            "--db", db, "export-slice",
            "--after-sequence", value["max_sequence"],
            "--dest", second, "--project", "p1",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        empty = json.loads(result.stdout)
        self.assertEqual(empty["row_count"], 0)
        self.assertEqual(second.read_bytes(), HEADER)

    def test_export_slice_unknown_project_refused_before_write(self):
        db = self._init_db_with_rows()
        dest = self.external_root / "refused.tsv"
        result = self.run_cli(
            "--db", db, "export-slice",
            "--after-sequence", 0, "--dest", dest, "--project", "unknown",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown project", result.stderr)
        self.assertFalse(dest.exists())


if __name__ == "__main__":
    unittest.main()
