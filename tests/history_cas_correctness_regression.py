#!/usr/bin/env python3
"""Correctness regressions for CAS publication, retention, and collection."""

import hashlib
import os
import pathlib
import sqlite3
import sys
import tempfile
import threading
import unittest
import zlib
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_audit_store
from lib import history_cas


class HistoryCasCorrectnessRegression(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.cas_root = self.root / "cas"
        self.conn = sqlite3.connect(self.root / "history.sqlite3")
        self.conn.row_factory = sqlite3.Row
        history_audit_store.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temporary.cleanup()

    def _put(self, raw, profile, expires_at):
        return history_cas.put_object(
            self.conn,
            self.cas_root,
            raw,
            profile,
            expires_at=expires_at,
        )

    @unittest.skipUnless(sys.platform == "darwin", "macOS system aliases")
    def test_macos_tmp_and_var_aliases_are_fixed_without_general_resolution(self):
        self.assertEqual(
            history_cas._safe_absolute_path("/tmp/cas"),
            pathlib.Path("/private/tmp/cas"),
        )
        self.assertEqual(
            history_cas._safe_absolute_path("/var/folders/cas"),
            pathlib.Path("/private/var/folders/cas"),
        )
        real = self.root / "real"
        real.mkdir()
        alias = self.root / "caller-link"
        alias.symlink_to(real, target_is_directory=True)
        with self.assertRaises(history_cas.CASError):
            history_cas.put_object(
                self.conn, alias / "cas", b"reject caller symlink", "permanent"
            )

    def test_exclusive_publication_keeps_winner_inode_and_verifies_winner(self):
        directory = self.root / "exclusive"
        directory.mkdir()
        parent = os.open(directory, history_cas._directory_flags())
        first = ".first.tmp"
        second = ".second.tmp"
        name = "object.zlib"
        payload = zlib.compress(b"same object", level=9)
        try:
            for temporary in (first, second):
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=parent,
                )
                try:
                    history_cas._write_all(descriptor, payload)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            self.assertTrue(history_cas._publish_exclusive(parent, first, name))
            winner = os.stat(name, dir_fd=parent, follow_symlinks=False)
            self.assertFalse(history_cas._publish_exclusive(parent, second, name))
            observed = os.stat(name, dir_fd=parent, follow_symlinks=False)
            self.assertEqual(
                (observed.st_dev, observed.st_ino),
                (winner.st_dev, winner.st_ino),
            )
            self.assertEqual(history_cas._read_payload(parent, name), payload)
        finally:
            for temporary in (first, second):
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
            os.close(parent)

    def test_equal_content_retention_only_extends_and_none_is_permanent(self):
        raw = b"one object, several retention requests"
        object_id = hashlib.sha256(raw).hexdigest()
        first = self._put(raw, "short", "2030-01-01T00:00:00+00:00")
        self.assertEqual(first["object_id"], object_id)

        extended = self._put(raw, "long", "2040-01-01T00:00:00+00:00")
        self.assertEqual(extended["expires_at"], first["expires_at"])
        extension = self.conn.execute(
            "SELECT pin_reason FROM audit_cas_pins WHERE object_id=?",
            (object_id,),
        ).fetchone()["pin_reason"]
        self.assertEqual(
            extension,
            "cas-retention-until:2040-01-01T00:00:00+00:00",
        )

        self._put(raw, "stale", "2020-01-01T00:00:00+00:00")
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_cas_pins WHERE object_id=?",
                (object_id,),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            history_cas.collect_garbage(
                self.conn,
                self.cas_root,
                "2035-01-01T00:00:00+00:00",
                grace_seconds=0,
            ),
            [],
        )

        self._put(raw, "permanent", None)
        permanent_pin = self.conn.execute(
            "SELECT 1 FROM audit_cas_pins "
            "WHERE object_id=? AND pin_reason='cas-retention-permanent'",
            (object_id,),
        ).fetchone()
        self.assertIsNotNone(permanent_pin)
        self._put(raw, "finite-again", "2099-01-01T00:00:00+00:00")
        self.assertEqual(
            history_cas.collect_garbage(
                self.conn,
                self.cas_root,
                "2100-01-01T00:00:00+00:00",
                grace_seconds=0,
            ),
            [],
        )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM audit_cas_objects").fetchone()[0],
            1,
        )
        self.assertEqual(len(list(self.cas_root.rglob("*.zlib"))), 1)

    def test_gc_final_transaction_rechecks_new_pin_before_unlink(self):
        descriptor = self._put(
            b"pin during collection",
            "transient",
            "2000-01-01T00:00:00+00:00",
        )
        original = history_cas._gc_eligible_descriptor
        checks = 0

        def pin_on_final_check(conn, object_id, cutoff):
            nonlocal checks
            checks += 1
            if checks == 3:
                conn.execute(
                    "INSERT INTO audit_cas_pins(object_id, pin_reason, pinned_at) "
                    "VALUES(?, ?, ?)",
                    (object_id, "racing-pin", "2026-08-09T00:00:00+00:00"),
                )
            return original(conn, object_id, cutoff)

        with mock.patch.object(
            history_cas,
            "_gc_eligible_descriptor",
            side_effect=pin_on_final_check,
        ), mock.patch.object(
            history_cas, "_delete_payload", wraps=history_cas._delete_payload
        ) as delete:
            removed = history_cas.collect_garbage(
                self.conn,
                self.cas_root,
                "2026-08-09T00:00:00+00:00",
                grace_seconds=0,
            )
        self.assertEqual(removed, [])
        delete.assert_not_called()
        self.assertTrue((self.cas_root / descriptor["relative_path"]).is_file())
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT 1 FROM audit_cas_pins WHERE object_id=?",
                (descriptor["object_id"],),
            ).fetchone()
        )

    def test_gc_pin_after_tombstone_retires_it_until_retention_expires(self):
        descriptor = self._put(
            b"pin committed after tombstone",
            "transient",
            "2000-01-01T00:00:00+00:00",
        )
        object_id = descriptor["object_id"]
        database = self.root / "history.sqlite3"
        barrier = threading.Barrier(2)

        class TombstoneBarrierConnection(sqlite3.Connection):
            armed = False

            def execute(self, sql, parameters=()):
                cursor = super().execute(sql, parameters)
                if (
                    self.armed
                    and sql.strip().upper() == "COMMIT"
                    and super().execute(
                        "SELECT 1 FROM audit_cas_tombstones WHERE object_id=?",
                        (object_id,),
                    ).fetchone()
                    is not None
                ):
                    self.armed = False
                    barrier.wait(timeout=10)
                    barrier.wait(timeout=10)
                return cursor

        gc_conn = sqlite3.connect(
            database,
            factory=TombstoneBarrierConnection,
            check_same_thread=False,
        )
        gc_conn.row_factory = sqlite3.Row
        pin_conn = sqlite3.connect(database)
        pin_conn.row_factory = sqlite3.Row
        outcome = {}

        def collect():
            try:
                outcome["removed"] = history_cas.collect_garbage(
                    gc_conn,
                    self.cas_root,
                    "2026-08-09T00:00:00+00:00",
                    grace_seconds=0,
                )
            except BaseException as exc:
                outcome["error"] = exc

        try:
            gc_conn.armed = True
            worker = threading.Thread(target=collect)
            worker.start()
            barrier.wait(timeout=10)
            self.assertIsNotNone(
                pin_conn.execute(
                    "SELECT 1 FROM audit_cas_tombstones WHERE object_id=?",
                    (object_id,),
                ).fetchone()
            )
            pin_conn.execute(
                "INSERT INTO audit_cas_pins(object_id, pin_reason, pinned_at) "
                "VALUES(?, ?, ?)",
                (
                    object_id,
                    "cas-retention-until:2099-01-01T00:00:00+00:00",
                    "2026-08-09T00:00:00+00:00",
                ),
            )
            pin_conn.commit()
            barrier.wait(timeout=10)
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
            if "error" in outcome:
                raise outcome["error"]

            self.assertEqual(outcome["removed"], [])
            self.assertTrue((self.cas_root / descriptor["relative_path"]).is_file())
            self.assertEqual(
                history_cas.verify_object(self.conn, self.cas_root, object_id)[
                    "integrity_state"
                ],
                "verified",
            )
            self.assertEqual(
                history_cas.collect_garbage(
                    self.conn,
                    self.cas_root,
                    "2098-01-01T00:00:00+00:00",
                    grace_seconds=0,
                ),
                [],
            )
            self.assertEqual(
                history_cas.collect_garbage(
                    self.conn,
                    self.cas_root,
                    "2100-01-01T00:00:00+00:00",
                    grace_seconds=0,
                ),
                [object_id],
            )
            self.assertEqual(
                history_cas.verify_object(self.conn, self.cas_root, object_id)[
                    "integrity_state"
                ],
                "expired",
            )
        finally:
            pin_conn.close()
            gc_conn.close()

    def test_gc_replay_retires_tombstone_when_pin_wins_initial_recheck(self):
        descriptor = self._put(
            b"pin committed during tombstone replay",
            "transient",
            "2000-01-01T00:00:00+00:00",
        )
        object_id = descriptor["object_id"]
        with mock.patch.object(
            history_cas,
            "_delete_payload",
            side_effect=RuntimeError("interrupt after tombstone commit"),
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupt after tombstone"):
                history_cas.collect_garbage(
                    self.conn,
                    self.cas_root,
                    "2026-08-09T00:00:00+00:00",
                    grace_seconds=0,
                )
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT 1 FROM audit_cas_tombstones WHERE object_id=?", (object_id,)
            ).fetchone()
        )

        barrier = threading.Barrier(2)

        class InitialRecheckBarrierConnection(sqlite3.Connection):
            armed = False

            def execute(self, sql, parameters=()):
                if self.armed and sql.strip().upper() == "BEGIN IMMEDIATE":
                    self.armed = False
                    barrier.wait(timeout=10)
                    barrier.wait(timeout=10)
                return super().execute(sql, parameters)

        database = self.root / "history.sqlite3"
        gc_conn = sqlite3.connect(
            database,
            factory=InitialRecheckBarrierConnection,
            check_same_thread=False,
        )
        gc_conn.row_factory = sqlite3.Row
        pin_conn = sqlite3.connect(database)
        pin_conn.row_factory = sqlite3.Row
        outcome = {}

        def replay():
            try:
                outcome["removed"] = history_cas.collect_garbage(
                    gc_conn,
                    self.cas_root,
                    "2026-08-09T00:00:00+00:00",
                    grace_seconds=0,
                )
            except BaseException as exc:
                outcome["error"] = exc

        try:
            gc_conn.armed = True
            worker = threading.Thread(target=replay)
            worker.start()
            barrier.wait(timeout=10)
            pin_conn.execute(
                "INSERT INTO audit_cas_pins(object_id, pin_reason, pinned_at) "
                "VALUES(?, ?, ?)",
                (object_id, "replay-racing-pin", "2026-08-09T00:00:00+00:00"),
            )
            pin_conn.commit()
            barrier.wait(timeout=10)
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
            if "error" in outcome:
                raise outcome["error"]

            self.assertEqual(outcome["removed"], [])
            self.assertTrue((self.cas_root / descriptor["relative_path"]).is_file())
            self.assertEqual(
                history_cas.verify_object(self.conn, self.cas_root, object_id)[
                    "integrity_state"
                ],
                "verified",
            )
        finally:
            pin_conn.close()
            gc_conn.close()

    def test_put_rejects_tombstone_retirement_pin_namespace(self):
        with self.assertRaisesRegex(history_cas.CASError, "reserved CAS prefix"):
            history_cas.put_object(
                self.conn,
                self.cas_root,
                b"caller cannot forge tombstone retirement",
                "transient",
                expires_at="2000-01-01T00:00:00+00:00",
                pin_reason="cas-tombstone-retired:" + "0" * 64,
            )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM audit_cas_objects").fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
