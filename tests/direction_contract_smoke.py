#!/usr/bin/env python3
import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import direction_contract


class DirectionContractSmoke(unittest.TestCase):
    def setUp(self):
        self.contract = json.loads(
            (ROOT / "directions/dynamic-spatial-memory-vla-v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = pathlib.Path(self.temp.name)
        direction = self.repo / "direction.json"
        direction.write_bytes(direction_contract.canonical_bytes(self.contract))
        os.symlink("direction.json", self.repo / "direction-link.json")
        os.link(direction, self.repo / "direction-hardlink.json")
        self.dot_direction = self.repo / "dot-direction.json"
        self.dot_direction.write_bytes(
            direction_contract.canonical_bytes(self.contract)
        )
        self.nested = self.repo / "nested"
        self.nested.mkdir()
        (self.nested / "direction.json").write_bytes(
            direction_contract.canonical_bytes(self.contract)
        )
        self.outside = self.repo / "outside"
        self.outside.mkdir()
        (self.outside / "direction.json").write_bytes(
            direction_contract.canonical_bytes(self.contract)
        )

    def changed(self, **changes):
        value = copy.deepcopy(self.contract)
        value.update(changes)
        return value

    def with_duplicate_axis_id(self):
        value = copy.deepcopy(self.contract)
        value["allowed_axes"].append(copy.deepcopy(value["allowed_axes"][0]))
        return value

    def with_unknown_axis_field(self):
        value = copy.deepcopy(self.contract)
        value["allowed_axes"][0]["unexpected"] = True
        return value

    def test_initial_contract_has_stable_canonical_identity(self):
        value, raw, identity = direction_contract.load_contract(
            "directions/dynamic-spatial-memory-vla-v1.json",
            ROOT,
        )
        self.assertEqual(value["direction_id"], "dynamic-spatial-memory-vla-v1")
        self.assertEqual(len(raw), 2072)
        self.assertEqual(
            identity,
            {
                "direction_id": "dynamic-spatial-memory-vla-v1",
                "sha256": (
                    "50bbf68a8ee20f2635194abab2a41ee702d4ec227b5277bf1bba9f463fee0d85"
                ),
            },
        )
        self.assertTrue(raw.endswith(b"\n"))

    def test_source_formatting_does_not_change_identity(self):
        compact = json.dumps(self.contract, ensure_ascii=False).encode("utf-8")
        pretty = json.dumps(
            self.contract, indent=4, ensure_ascii=False
        ).encode("utf-8")
        padded = (b" " * 16385) + compact
        self.assertEqual(
            direction_contract.parse_contract_bytes(compact)[1:],
            direction_contract.parse_contract_bytes(pretty)[1:],
        )
        self.assertEqual(
            direction_contract.parse_contract_bytes(compact)[1:],
            direction_contract.parse_contract_bytes(padded)[1:],
        )

    def test_schema_is_closed_and_duplicate_json_keys_are_rejected(self):
        invalid = dict(self.contract, unexpected=True)
        with self.assertRaises(direction_contract.DirectionContractError):
            direction_contract.parse_contract_bytes(
                json.dumps(invalid).encode("utf-8")
            )
        with self.assertRaises(direction_contract.DirectionContractError):
            direction_contract.parse_contract_bytes(
                b'{"schema_version":1,"schema_version":1}'
            )

    def test_ids_controls_and_bounds_are_rejected(self):
        cases = [
            self.changed(direction_id="UPPER CASE"),
            self.changed(statement="contains\u0001control"),
            self.changed(statement="x" * 16385),
            self.with_duplicate_axis_id(),
            self.with_unknown_axis_field(),
            self.changed(all_candidates_must_match=False),
        ]
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(direction_contract.DirectionContractError):
                    direction_contract.parse_contract_bytes(
                        json.dumps(value).encode("utf-8")
                    )

    def test_loader_rejects_absolute_parent_symlink_and_hardlink_paths(self):
        for source in (
            str(self.repo / "direction.json"),
            "../direction.json",
            "direction-link.json",
            "direction-hardlink.json",
        ):
            with self.subTest(source=source):
                with self.assertRaises(direction_contract.DirectionContractError):
                    direction_contract.load_contract(source, self.repo)

    def test_loader_rejects_explicit_dot_components_before_normalizing(self):
        for source in ("./dot-direction.json", "nested/./direction.json"):
            with self.subTest(source=source):
                with self.assertRaises(direction_contract.DirectionContractError):
                    direction_contract.load_contract(source, self.repo)

    def test_loader_rejects_intermediate_symlink_replacement_during_open(self):
        original_open = direction_contract.os.open
        replaced = False

        def race_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal replaced
            if path == "nested" and dir_fd is not None and not replaced:
                os.unlink(self.nested / "direction.json")
                os.rmdir(self.nested)
                os.symlink(str(self.outside), str(self.nested))
                replaced = True
            if dir_fd is None:
                return original_open(path, flags, mode)
            return original_open(path, flags, mode, dir_fd=dir_fd)

        with mock.patch.object(direction_contract.os, "open", side_effect=race_open):
            with self.assertRaises(direction_contract.DirectionContractError):
                direction_contract.load_contract("nested/direction.json", self.repo)
        self.assertTrue(replaced)

    def test_snapshot_checks_both_destinations_before_writing(self):
        os.unlink(self.repo / "direction-hardlink.json")
        identity = self.repo / "direction.identity.json"
        identity.write_text("already exists\n", encoding="utf-8")
        output = self.repo / "direction.snapshot.json"
        with self.assertRaises(direction_contract.DirectionContractError):
            direction_contract.write_snapshot(
                "direction.json", self.repo, output, identity
            )
        self.assertFalse(output.exists())

    def test_snapshot_rejects_symlinked_destination_ancestor(self):
        real = self.repo / "real-parent"
        nested = real / "nested"
        nested.mkdir(parents=True)
        os.symlink(real, self.repo / "aliased-parent")
        destination = self.repo / "aliased-parent" / "nested" / "snapshot.json"
        with self.assertRaises(direction_contract.DirectionContractError):
            direction_contract._atomic_write_new(destination, b"sealed\n")
        self.assertFalse((nested / "snapshot.json").exists())

    def test_snapshot_retains_open_parent_across_path_swap(self):
        parent = self.repo / "publish-parent"
        parent.mkdir()
        moved = self.repo / "publish-parent-opened"
        destination = parent / "snapshot.json"
        original_open = direction_contract.os.open
        swapped = False

        def race_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            if (
                not swapped
                and flags & os.O_CREAT
                and flags & os.O_EXCL
                and ".snapshot.json." in os.fspath(path)
            ):
                parent.rename(moved)
                parent.mkdir()
                swapped = True
            if dir_fd is None:
                return original_open(path, flags, mode)
            return original_open(path, flags, mode, dir_fd=dir_fd)

        with mock.patch.object(
            direction_contract.os, "open", side_effect=race_open
        ):
            direction_contract._atomic_write_new(destination, b"sealed\n")
        self.assertTrue(swapped)
        self.assertEqual((moved / "snapshot.json").read_bytes(), b"sealed\n")
        self.assertFalse(destination.exists())

    def test_snapshot_destination_race_does_not_overwrite(self):
        destination = self.repo / "raced-snapshot.json"
        original_open = direction_contract.os.open
        raced = False

        def race_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal raced
            if (
                not raced
                and flags & os.O_CREAT
                and flags & os.O_EXCL
                and ".raced-snapshot.json." in os.fspath(path)
            ):
                destination.write_bytes(b"racer\n")
                raced = True
            if dir_fd is None:
                return original_open(path, flags, mode)
            return original_open(path, flags, mode, dir_fd=dir_fd)

        with mock.patch.object(
            direction_contract.os, "open", side_effect=race_open
        ):
            with self.assertRaises(direction_contract.DirectionContractError):
                direction_contract._atomic_write_new(destination, b"sealed\n")
        self.assertTrue(raced)
        self.assertEqual(destination.read_bytes(), b"racer\n")

    def test_round_snapshot_is_exclusive_and_resume_validates_existing_bytes(self):
        os.unlink(self.repo / "direction-hardlink.json")
        startup_contract = self.repo / "direction.json"
        _, startup_raw, identity = direction_contract.load_contract(
            "direction.json", self.repo
        )
        startup_identity = self.repo / "startup-identity.json"
        startup_identity.write_bytes(direction_contract.canonical_bytes(identity))
        round_root = self.repo / "round"
        round_root.mkdir()
        round_contract = round_root / "direction-constraint.json"
        round_identity = round_root / "direction-identity.json"

        direction_contract.publish_round_snapshot(
            startup_contract,
            startup_identity,
            round_contract,
            round_identity,
            resume=False,
        )
        self.assertEqual(round_contract.read_bytes(), startup_raw)
        self.assertEqual(
            round_identity.read_bytes(),
            direction_contract.canonical_bytes(identity),
        )

        direction_contract.publish_round_snapshot(
            startup_contract,
            startup_identity,
            round_contract,
            round_identity,
            resume=True,
        )
        round_contract.chmod(0o600)
        round_contract.write_bytes(startup_raw.replace(b"Research", b"Changed", 1))
        with self.assertRaises(direction_contract.DirectionContractError):
            direction_contract.publish_round_snapshot(
                startup_contract,
                startup_identity,
                round_contract,
                round_identity,
                resume=True,
            )
        drifted_identity = self.repo / "drifted-startup-identity.json"
        changed_identity = dict(identity, sha256="0" * 64)
        drifted_identity.write_bytes(
            direction_contract.canonical_bytes(changed_identity)
        )
        rejected_root = self.repo / "rejected-round"
        rejected_root.mkdir()
        rejected_contract = rejected_root / "direction-constraint.json"
        rejected_identity = rejected_root / "direction-identity.json"
        with self.assertRaises(direction_contract.DirectionContractError):
            direction_contract.publish_round_snapshot(
                startup_contract,
                drifted_identity,
                rejected_contract,
                rejected_identity,
                resume=False,
            )
        self.assertFalse(rejected_contract.exists())
        self.assertFalse(rejected_identity.exists())

    def test_validate_verdicts_cli_accepts_an_absolute_contract_path(self):
        ideas = self.repo / "ideas.tsv"
        ideas.write_text(
            "I1\tFirst candidate\tFirst summary\n"
            "I2\tSecond candidate\tSecond summary\n",
            encoding="utf-8",
        )
        verdicts = self.repo / "direction.tsv"
        verdicts.write_text(
            "id\tdirection-fit\tdirection-evidence\n"
            "I1\tin-scope\tThe proposition tests correctable 3D memory.\n"
            "I2\tin-scope\tThe experiment isolates memory injection.\n",
            encoding="utf-8",
        )
        output = self.repo / "receipt.json"
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "lib/direction_contract.py"),
                "validate-verdicts",
                "--contract",
                str(ROOT / "directions/dynamic-spatial-memory-vla-v1.json"),
                "--ideas",
                str(ideas),
                "--verdicts",
                str(verdicts),
                "--output",
                str(output),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(receipt["candidate_count"], 2)
        self.assertEqual(
            [item["candidate_id"] for item in receipt["verdicts"]],
            ["I1", "I2"],
        )

    def test_candidate_fields_use_exact_contract_enums(self):
        values = {
            "Direction Axis": "memory-representation-update",
            "Target Failure": "dynamic-scene-change",
            "Direction Evidence": (
                "The repair experiment attributes recovery to corrected 3D memory."
            ),
        }
        direction_contract.validate_candidate_fields(values, self.contract, "I1")
        direction_contract.validate_candidate_fields(
            dict(values, Title="Candidate title"), self.contract, "I1"
        )
        for field, invalid in (
            ("Direction Axis", "memory"),
            ("Target Failure", "navigation"),
            ("Direction Evidence", ""),
            ("Direction Evidence", "   "),
        ):
            changed = dict(values, **{field: invalid})
            with self.subTest(field=field):
                with self.assertRaises(direction_contract.DirectionContractError):
                    direction_contract.validate_candidate_fields(
                        changed, self.contract, "I1"
                    )

    def test_contract_prose_rejects_whitespace_only_values(self):
        values = [
            self.changed(statement="   "),
            self.changed(fixed_constraints=[" \t "]),
            self.changed(excluded_scopes=["  "]),
        ]
        axis = self.changed()
        axis["allowed_axes"][0]["description"] = "   "
        values.append(axis)
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(direction_contract.DirectionContractError):
                    direction_contract.parse_contract_bytes(
                        json.dumps(value).encode("utf-8")
                    )

    def test_candidate_axis_and_failure_types_raise_contract_error(self):
        valid = {
            "Direction Axis": "memory-representation-update",
            "Target Failure": "dynamic-scene-change",
            "Direction Evidence": "The repair arm isolates corrected memory.",
        }
        for field, invalid in (
            ("Direction Axis", ["memory-representation-update"]),
            ("Direction Axis", None),
            ("Target Failure", {"id": "dynamic-scene-change"}),
            ("Target Failure", 1),
        ):
            changed = dict(valid, **{field: invalid})
            with self.subTest(field=field, invalid=invalid):
                with self.assertRaises(direction_contract.DirectionContractError):
                    direction_contract.validate_candidate_fields(
                        changed, self.contract, "I1"
                    )

    def test_identity_schema_is_closed(self):
        valid = {
            "direction_id": "dynamic-spatial-memory-vla-v1",
            "sha256": "50bbf68a8ee20f2635194abab2a41ee702d4ec227b5277bf1bba9f463fee0d85",
        }
        self.assertEqual(direction_contract.validate_identity(valid), valid)
        self.assertIsNone(direction_contract.validate_identity(None))
        for value in (
            {},
            dict(valid, unexpected=True),
            dict(valid, sha256="0" * 63),
            dict(valid, direction_id="UPPER CASE"),
        ):
            with self.subTest(value=value):
                with self.assertRaises(direction_contract.DirectionContractError):
                    direction_contract.validate_identity(value)

    def test_direction_verdicts_require_header_order_coverage_and_scope(self):
        valid = (
            b"id\tdirection-fit\tdirection-evidence\n"
            b"I1\tin-scope\tThe proposition tests correctable 3D memory.\n"
            b"I2\tin-scope\tThe experiment isolates memory injection.\n"
        )
        parsed = direction_contract.require_all_in_scope(valid, ["I1", "I2"])
        self.assertEqual(
            [item["candidate_id"] for item in parsed],
            ["I1", "I2"],
        )
        with self.assertRaises(direction_contract.DirectionContractError):
            direction_contract.parse_direction_verdicts(valid, [["I1"]])
        invalid_values = [
            valid.replace(b"I2\tin-scope", b"I2\tout-of-scope"),
            valid.replace(b"I2\t", b"I1\t"),
            valid.replace(b"I1\t", b"I2\t", 1),
            valid.replace(
                b"\tThe experiment isolates memory injection.", b"\t"
            ),
            valid.replace(
                b"The experiment isolates memory injection.", b"   "
            ),
            valid.replace(b"id\tdirection-fit\tdirection-evidence\n", b""),
        ]
        for raw in invalid_values:
            with self.subTest(raw=raw):
                with self.assertRaises(direction_contract.DirectionContractError):
                    direction_contract.require_all_in_scope(raw, ["I1", "I2"])


if __name__ == "__main__":
    unittest.main()
