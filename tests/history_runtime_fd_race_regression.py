#!/usr/bin/env python3
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import history_runtime_smoke as smoke  # noqa: E402

history_runtime = smoke.history_runtime
history_store = smoke.history_store


class DescriptorRaceRegression(smoke.RuntimeFixture):
    def test_tree_publication_retains_parent_fd_across_rename(self):
        parent = self.root / "publication-parent"
        parent.mkdir()
        moved = self.root / "publication-parent-moved"
        destination = parent / "tree"
        original = history_runtime._publish_immutable_at
        renamed = False

        def rename_parent(directory, name, raw):
            nonlocal renamed
            if not renamed:
                parent.rename(moved)
                parent.mkdir()
                renamed = True
            return original(directory, name, raw)

        with mock.patch.object(
            history_runtime,
            "_publish_immutable_at",
            side_effect=rename_parent,
        ):
            history_runtime._publish_immutable_tree(
                destination,
                {"nested/first": b"one", "nested/second": b"two"},
            )

        self.assertEqual((moved / "tree/nested/first").read_bytes(), b"one")
        self.assertEqual((moved / "tree/nested/second").read_bytes(), b"two")
        self.assertFalse((parent / "tree").exists())

    def test_tree_publication_never_replaces_concurrent_empty_root(self):
        parent = self.root / "publication-race"
        parent.mkdir()
        destination = parent / "tree"
        original = history_runtime._rename_no_replace

        def claim_destination(directory, source, name):
            destination.mkdir()
            return original(directory, source, name)

        with mock.patch.object(
            history_runtime,
            "_rename_no_replace",
            side_effect=claim_destination,
        ):
            with self.assertRaises(history_runtime.RuntimeContractError):
                history_runtime._publish_immutable_tree(
                    destination, {"nested/value": b"value"}
                )

        self.assertTrue(destination.is_dir())
        self.assertEqual(list(destination.iterdir()), [])
        self.assertEqual(list(parent.glob(".tree.*")), [])


class DatabaseRaceRegression(smoke.RuntimeFixture):
    def _initialized_database(self, path):
        conn = history_store.connect(path)
        history_store.init_schema(conn)
        conn.close()

    def test_database_final_symlink_swap_is_rejected(self):
        self._initialized_database(self.database)
        alternate = self.root / "alternate.sqlite3"
        self._initialized_database(alternate)
        displaced = self.database.with_suffix(".displaced")
        original = history_runtime.sqlite3.connect
        swapped = False

        def swap_filename(path, **kwargs):
            nonlocal swapped
            if not swapped:
                self.database.rename(displaced)
                self.database.symlink_to(alternate)
                swapped = True
            return original(path, **kwargs)

        with mock.patch.object(
            history_runtime.sqlite3, "connect", side_effect=swap_filename
        ):
            with self.assertRaises(history_runtime.RuntimeContractError):
                history_runtime._connect_history_store(self.database)

    def test_database_ancestor_rename_is_rejected(self):
        self._initialized_database(self.database)
        state = self.database.parent
        moved = self.root / "state-moved"
        original = history_runtime.sqlite3.connect
        swapped = False

        def swap_parent(path, **kwargs):
            nonlocal swapped
            if not swapped:
                state.rename(moved)
                state.mkdir()
                conn = original(state / "history.sqlite3")
                history_store.init_schema(conn)
                conn.commit()
                conn.close()
                swapped = True
            return original(path, **kwargs)

        with mock.patch.object(
            history_runtime.sqlite3, "connect", side_effect=swap_parent
        ):
            with self.assertRaises(history_runtime.RuntimeContractError):
                history_runtime._connect_history_store(self.database)


if __name__ == "__main__":
    unittest.main()
