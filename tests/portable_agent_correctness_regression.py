#!/usr/bin/env python3
"""Correctness regressions for portable CAS publication and root walking."""

import concurrent.futures
import errno
import hashlib
import os
import pathlib
import stat
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import portable_agent


class PortableImportPublicationRegression(unittest.TestCase):
    def _state(self, directory):
        return portable_agent._open_or_create_absolute_directory_no_follow(
            pathlib.Path(directory).resolve() / "state",
            "unsafe_state_root",
        )

    def test_short_write_enospc_never_exposes_partial_final(self):
        raw = b'{"status":"complete"}\n'
        output_sha = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root, root_descriptor = self._state(directory)
            real_write = portable_agent.os.write
            writes = 0

            def fail_after_partial_write(descriptor, content):
                nonlocal writes
                writes += 1
                if writes == 1:
                    return real_write(descriptor, content[:5])
                raise OSError(errno.ENOSPC, "injected full filesystem")

            try:
                with mock.patch.object(
                    portable_agent.os,
                    "write",
                    side_effect=fail_after_partial_write,
                ):
                    with self.assertRaises(OSError) as caught:
                        portable_agent._publish_import(
                            root_descriptor,
                            output_sha,
                            raw,
                            len(raw),
                        )
                self.assertEqual(caught.exception.errno, errno.ENOSPC)
                imports = root / "imports"
                self.assertFalse((imports / f"{output_sha}.json").exists())
                self.assertEqual(list(imports.iterdir()), [])
            finally:
                os.close(root_descriptor)

    def test_publication_fsyncs_temp_then_atomically_links_and_verifies_winner(self):
        raw = b'{"status":"complete"}\n'
        output_sha = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root, root_descriptor = self._state(directory)
            events = []
            real_fsync = portable_agent.os.fsync
            real_link = portable_agent.os.link

            def record_fsync(descriptor):
                info = os.fstat(descriptor)
                events.append("fsync-dir" if stat.S_ISDIR(info.st_mode) else "fsync-file")
                return real_fsync(descriptor)

            def record_link(*args, **kwargs):
                events.append("link")
                return real_link(*args, **kwargs)

            try:
                with mock.patch.object(
                    portable_agent.os, "fsync", side_effect=record_fsync
                ), mock.patch.object(
                    portable_agent.os, "link", side_effect=record_link
                ):
                    name = portable_agent._publish_import(
                        root_descriptor,
                        output_sha,
                        raw,
                        len(raw),
                    )
                self.assertLess(events.index("fsync-file"), events.index("link"))
                self.assertLess(events.index("link"), events.index("fsync-dir"))
                final = root / "imports" / name
                self.assertEqual(final.read_bytes(), raw)
                self.assertEqual(final.stat().st_nlink, 1)
                with mock.patch.object(
                    portable_agent.os,
                    "write",
                    side_effect=AssertionError("existing winner was rewritten"),
                ):
                    repeated = portable_agent._publish_import(
                        root_descriptor,
                        output_sha,
                        raw,
                        len(raw),
                    )
                self.assertEqual(repeated, name)
                self.assertEqual(
                    [path.name for path in (root / "imports").iterdir()],
                    [name],
                )
            finally:
                os.close(root_descriptor)

    def test_crash_residue_is_repaired_and_same_winner_is_idempotent(self):
        raw = b'{"status":"complete"}\n'
        output_sha = hashlib.sha256(raw).hexdigest()
        final_name = f"{output_sha}.json"
        residue_name = f".{final_name}.{'a' * 32}.tmp"
        with tempfile.TemporaryDirectory() as directory:
            root, root_descriptor = self._state(directory)
            imports_descriptor = portable_agent._open_or_create_directory_at(
                root_descriptor,
                "imports",
                "unsafe_state_path",
            )
            try:
                descriptor = os.open(
                    residue_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=imports_descriptor,
                )
                try:
                    os.write(descriptor, raw)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.link(
                    residue_name,
                    final_name,
                    src_dir_fd=imports_descriptor,
                    dst_dir_fd=imports_descriptor,
                    follow_symlinks=False,
                )
            finally:
                os.close(imports_descriptor)
            try:
                self.assertEqual(
                    portable_agent._publish_import(
                        root_descriptor,
                        output_sha,
                        raw,
                        len(raw),
                    ),
                    final_name,
                )
                final = root / "imports" / final_name
                self.assertEqual(final.read_bytes(), raw)
                self.assertEqual(final.stat().st_nlink, 1)
                self.assertFalse((root / "imports" / residue_name).exists())
            finally:
                os.close(root_descriptor)

    def test_concurrent_publishers_converge_on_one_complete_winner(self):
        raw = b'{"status":"complete"}\n'
        output_sha = hashlib.sha256(raw).hexdigest()
        final_name = f"{output_sha}.json"
        with tempfile.TemporaryDirectory() as directory:
            root, root_descriptor = self._state(directory)
            try:
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=8
                ) as executor:
                    names = list(
                        executor.map(
                            lambda _: portable_agent._publish_import(
                                root_descriptor,
                                output_sha,
                                raw,
                                len(raw),
                            ),
                            range(32),
                        )
                    )
                self.assertEqual(names, [final_name] * 32)
                final = root / "imports" / final_name
                self.assertEqual(final.read_bytes(), raw)
                self.assertEqual(final.stat().st_nlink, 1)
                self.assertEqual(
                    [path.name for path in (root / "imports").iterdir()],
                    [final_name],
                )
            finally:
                os.close(root_descriptor)

    def test_existing_different_winner_preserves_import_conflict_contract(self):
        expected = b'{"status":"expected"}\n'
        existing = b'{"status":"different"}\n'
        output_sha = hashlib.sha256(expected).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root, root_descriptor = self._state(directory)
            imports = root / "imports"
            imports.mkdir(mode=0o700)
            final = imports / f"{output_sha}.json"
            final.write_bytes(existing)
            final.chmod(0o600)
            try:
                with self.assertRaises(portable_agent.PortableAgentError) as caught:
                    portable_agent._publish_import(
                        root_descriptor,
                        output_sha,
                        expected,
                        max(len(expected), len(existing)),
                    )
                self.assertEqual(caught.exception.code, "import_conflict")
                self.assertEqual(final.read_bytes(), existing)
                self.assertEqual([path.name for path in imports.iterdir()], [final.name])
            finally:
                os.close(root_descriptor)


class PortableNoFollowRootRegression(unittest.TestCase):
    def test_state_root_rejects_symlink_ancestor_without_creating_target(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory).resolve()
            target = base / "target"
            target.mkdir()
            (base / "alias").symlink_to(target, target_is_directory=True)
            with self.assertRaises(portable_agent.PortableAgentError) as caught:
                portable_agent._open_or_create_absolute_directory_no_follow(
                    base / "alias" / "state",
                    "unsafe_state_root",
                )
            self.assertEqual(caught.exception.code, "unsafe_state_root")
            self.assertFalse((target / "state").exists())

    def test_import_root_rejects_symlink_without_writing_target(self):
        raw = b'{"status":"complete"}\n'
        output_sha = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory).resolve()
            root, root_descriptor = portable_agent._open_or_create_absolute_directory_no_follow(
                base / "state",
                "unsafe_state_root",
            )
            outside = base / "outside"
            outside.mkdir()
            (root / "imports").symlink_to(outside, target_is_directory=True)
            try:
                with self.assertRaises(portable_agent.PortableAgentError) as caught:
                    portable_agent._publish_import(
                        root_descriptor,
                        output_sha,
                        raw,
                        len(raw),
                    )
                self.assertEqual(caught.exception.code, "unsafe_state_path")
                self.assertEqual(list(outside.iterdir()), [])
            finally:
                os.close(root_descriptor)

    def test_output_root_rejects_symlink_ancestor(self):
        raw = b'{"status":"complete"}\n'
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory).resolve()
            real = base / "real"
            output = real / "mirror" / "output"
            output.mkdir(parents=True)
            result = output / "result.json"
            result.write_bytes(raw)
            result.chmod(0o600)
            (base / "alias").symlink_to(real, target_is_directory=True)
            with self.assertRaises(portable_agent.PortableAgentError) as caught:
                portable_agent._read_mirror_output(
                    base / "alias" / "mirror",
                    pathlib.PurePosixPath("output/result.json"),
                    len(raw),
                )
            self.assertEqual(caught.exception.code, "unsafe_output")

    def test_stable_import_read_rejects_symlink_ancestor(self):
        raw = b'{"status":"complete"}\n'
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory).resolve()
            real = base / "real"
            real.mkdir()
            imported = real / "winner.json"
            imported.write_bytes(raw)
            imported.chmod(0o600)
            (base / "alias").symlink_to(real, target_is_directory=True)
            with self.assertRaises(portable_agent.PortableAgentError) as caught:
                portable_agent._open_read_stable(
                    base / "alias" / "winner.json",
                    len(raw),
                    "unsafe_import",
                    require_owner_only=True,
                )
            self.assertEqual(caught.exception.code, "unsafe_import")
    def test_response_contract_accepts_required_nonempty_content(self):
        schema = {
            "additionalProperties": False,
            "properties": {
                "schema_version": {
                    "minimum": 1,
                    "maximum": 1,
                    "type": "integer",
                },
                "stage": {"enum": ["generate"], "type": "string"},
                "request_attestation": {
                    "additionalProperties": False,
                    "properties": {
                        "schema_version": {
                            "enum": ["portable-stage-response-attestation-v1"],
                            "type": "string",
                        },
                        "provider_request_binding_sha256": {"type": "string"},
                        "serialized_prompt_sha256": {"type": "string"},
                    },
                    "required": [
                        "schema_version",
                        "provider_request_binding_sha256",
                        "serialized_prompt_sha256",
                    ],
                    "type": "object",
                },
                "artifacts": {
                    "items": {
                        "additionalProperties": False,
                        "properties": {
                            "artifact_kind": {
                                "enum": ["generation-tsv"],
                                "type": "string",
                            },
                            "content": {
                                "maxLength": 65536,
                                "minLength": 1,
                                "type": "string",
                            },
                        },
                        "required": ["artifact_kind", "content"],
                        "type": "object",
                    },
                    "minItems": 1,
                    "maxItems": 1,
                    "type": "array",
                },
            },
            "required": [
                "schema_version",
                "stage",
                "request_attestation",
                "artifacts",
            ],
            "type": "object",
        }
        contract = portable_agent._validate_response_schema_contract(schema)
        self.assertEqual(contract["content_max_length"], 65536)


if __name__ == "__main__":
    unittest.main()
