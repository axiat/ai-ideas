#!/usr/bin/env python3
"""Crash-recovery regressions for hunt.sh decision publication."""

import base64
import hashlib
import json
import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
HUNT = (ROOT / "hunt.sh").read_text(encoding="utf-8")


def section(start: str, end: str) -> str:
    begin = HUNT.index(start)
    finish = HUNT.index(end, begin)
    return HUNT[begin:finish]


def run_shell(script: str, cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", "set -u\n" + script],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


class HuntRecoveryAuthorityRegression(unittest.TestCase):
    def test_verified_pending_archive_carries_its_archived_date(self):
        verifier = section(
            "verify_pending_recovery_archive() {",
            "find_pending_archive_report_view() {",
        )
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            archive = root / "runs" / "run-old"
            (archive / "round/history").mkdir(parents=True)
            (archive / "manifest.tsv").write_text(
                "run_id\trun-old\n"
                "round\t4\n"
                "date\t2026-08-08\n"
                "policy_mode\tenforcement\n"
                "reason\tdecision\n"
                "archived_at\t2026-08-08 23:59:59+0000\n",
                encoding="utf-8",
            )
            (archive / "round/history/decision-outcome.tsv").write_text(
                "strong-accept\t1\n", encoding="ascii"
            )
            library = root / "lib"
            library.mkdir()
            (library / "history_archive.py").write_text(
                "class ArchiveError(Exception):\n    pass\n"
                "def verify_archive(*args, **kwargs):\n"
                "    return {'created_reason': 'decision'}\n",
                encoding="utf-8",
            )
            result = run_shell(
                "LOG=/dev/null\n"
                + verifier
                + f"verify_pending_recovery_archive {str(archive)!r}\n",
                root,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout.strip(),
                "run-old\t4\t2026-08-08\tdecision\tstrong-accept",
            )

    def test_no_accept_decision_is_not_pending_recovery(self):
        finder = section(
            "find_pending_archive_report_view() {",
            "refresh_published_archive() {",
        )
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            runs = root / "runs"
            (runs / "ordinary").mkdir(parents=True)
            (root / "history").mkdir()
            (root / "history/run-id").write_text("ordinary\n", encoding="ascii")
            result = run_shell(
                f"RUNS_DIR={str(runs)!r}\nLOG=/dev/null\nRD=.\n"
                "log() { :; }\n"
                "verify_pending_recovery_archive() {\n"
                "  printf 'ordinary\\t2\\t2026-08-09\\tdecision\\tno-strong-accept\\n'\n"
                "}\n"
                + finder
                + "if find_pending_archive_report_view; then exit 9; "
                "else rc=$?; [ \"$rc\" -eq 1 ] "
                "&& [ \"$RECOVERY_CURRENT_DECISION_COMPLETE\" -eq 1 ]; fi\n",
                root,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_pending_strong_accept_uses_archived_date(self):
        finder = section(
            "find_pending_archive_report_view() {",
            "refresh_published_archive() {",
        )
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            runs = root / "runs"
            report_view = runs / "old-run/round/history/review-attempts/001/report-view"
            report_view.mkdir(parents=True)
            (report_view / "accepted.tsv").write_text("I1\tstory\n", encoding="utf-8")
            result = run_shell(
                f"RUNS_DIR={str(runs)!r}\nLOG=/dev/null\nRD=.\n"
                "log() { :; }\n"
                "verify_pending_recovery_archive() {\n"
                "  printf 'old-run\\t7\\t2026-08-08\\tdecision\\tstrong-accept\\n'\n"
                "}\n"
                + finder
                + "find_pending_archive_report_view || exit $?\n"
                + "printf '%s\\t%s\\t%s\\n' \"$RECOVERY_RUN_ID\" "
                "\"$RECOVERY_ROUND\" \"$RECOVERY_DATE\"\n",
                root,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "old-run\t7\t2026-08-08")

    def test_unrelated_report_cannot_authorize_publication(self):
        verifier = section(
            "verify_pending_report_binding() {",
            "snapshot_archive_source() {",
        )
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            archive = root / "runs/run-old"
            archive.mkdir(parents=True)
            ideas = root / "ideas"
            ideas.mkdir()
            unrelated = ideas / "2026-08-08_hunt.md"
            unrelated.write_text("unrelated\n", encoding="utf-8")
            prefix = (
                f"ARCHIVE={str(archive)!r}\n"
                + verifier
            )
            missing = run_shell(
                prefix
                + "verify_pending_report_binding \"$ARCHIVE\" run-old 2026-08-08\n",
                root,
            )
            self.assertEqual(missing.returncode, 3, missing.stderr)

            report = ideas / "2026-08-08_hunt-2.md"
            report.write_text("bound report\n", encoding="utf-8")
            binding = {
                "report_content_base64": base64.b64encode(report.read_bytes()).decode(
                    "ascii"
                ),
                "report_path": "ideas/2026-08-08_hunt-2.md",
                "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                "run_id": "run-old",
                "schema_version": 1,
            }
            binding_raw = json.dumps(
                binding, sort_keys=True, separators=(",", ":")
            ) + "\n"
            (archive / "report-binding.json").write_text(
                binding_raw, encoding="utf-8"
            )
            verified = run_shell(
                prefix
                + "verify_pending_report_binding \"$ARCHIVE\" run-old 2026-08-08\n",
                root,
            )
            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertEqual(verified.stdout.strip(), binding["report_path"])

            report.unlink()
            restored = run_shell(
                prefix
                + "verify_pending_report_binding \"$ARCHIVE\" run-old 2026-08-08\n",
                root,
            )
            self.assertEqual(restored.returncode, 0, restored.stderr)
            self.assertEqual(report.read_text(encoding="utf-8"), "bound report\n")

            unrelated.write_text("changed unrelated report\n", encoding="utf-8")
            still_verified = run_shell(
                prefix
                + "verify_pending_report_binding \"$ARCHIVE\" run-old 2026-08-08\n",
                root,
            )
            self.assertEqual(still_verified.returncode, 0, still_verified.stderr)
            report.write_text("tampered bound report\n", encoding="utf-8")
            tampered = run_shell(
                prefix
                + "verify_pending_report_binding \"$ARCHIVE\" run-old 2026-08-08\n",
                root,
            )
            self.assertNotEqual(tampered.returncode, 0)

    def test_report_copy_seals_binding_without_replacement(self):
        copier = section("copy_external_output() {", "external_stage_prompt() {")
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            mirror = root / "mirror"
            (mirror / "ideas").mkdir(parents=True)
            (mirror / "ideas/report.md").write_text("report body\n", encoding="utf-8")
            round_root = root / "tmp/round"
            (round_root / "history").mkdir(parents=True)
            archive = root / "runs/run-bound"
            archive.mkdir(parents=True)
            script = (
                f"RD={str(round_root)!r}\n"
                "today=2026-08-08\nrun_id=run-bound\n"
                f"RUNS_DIR={str(root / 'runs')!r}\n"
                + copier
                + f"copy_external_output report {str(mirror)!r}\n"
            )
            first = run_shell(script, root)
            self.assertEqual(first.returncode, 0, first.stderr)
            binding_path = archive / "report-binding.json"
            original_binding = binding_path.read_bytes()
            binding = json.loads(original_binding)
            self.assertEqual(binding["run_id"], "run-bound")
            self.assertEqual(binding["report_path"], "ideas/2026-08-08_hunt.md")
            self.assertEqual(
                base64.b64decode(binding["report_content_base64"], validate=True),
                (root / binding["report_path"]).read_bytes(),
            )
            self.assertEqual(
                binding["report_sha256"],
                hashlib.sha256((root / binding["report_path"]).read_bytes()).hexdigest(),
            )
            second = run_shell(script, root)
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(binding_path.read_bytes(), original_binding)
            self.assertFalse((root / "ideas/2026-08-08_hunt-2.md").exists())

    def test_recovery_publication_uses_archived_date(self):
        publisher = section(
            "publish_hunt_for_date() {",
            "publish_existing_strong_accept_report() {",
        )
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            (root / "publish.sh").write_text(
                (ROOT / "publish.sh").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (root / "publish.sh").chmod(0o755)
            bindir = root / "bin"
            bindir.mkdir()
            capture = root / "publish-commands"
            fake_git = bindir / "git"
            fake_git.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'git' >> \"$CAPTURE\"\n"
                "printf '\\t%s' \"$@\" >> \"$CAPTURE\"\n"
                "printf '\\n' >> \"$CAPTURE\"\n"
                "if [ \"${1:-}\" = diff ] && [ \"${3:-}\" = --quiet ]; then exit 1; fi\n"
                "if [ \"${1:-}\" = diff ] && [ \"${3:-}\" = --name-only ]; then "
                "printf 'ideas/report.md\\n'; fi\n"
                "if [ \"${1:-}\" = rev-parse ] && [ \"${2:-}\" = --abbrev-ref ]; then "
                "printf 'main\\n'; fi\n",
                encoding="utf-8",
            )
            fake_git.chmod(0o755)
            fake_gh = bindir / "gh"
            fake_gh.write_text(
                "#!/usr/bin/env bash\nprintf 'OPEN\\n'\n", encoding="utf-8"
            )
            fake_gh.chmod(0o755)
            result = run_shell(
                f"LOG=/dev/null\nTMPDIR={str(root)!r}\n"
                f"export CAPTURE={str(capture)!r}\n"
                f"PATH={str(bindir)!r}:$PATH\n"
                + publisher
                + "publish_hunt_for_date 2026-08-08\n",
                root,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            commands = capture.read_text(encoding="utf-8")
            self.assertIn("git\tcheckout\t-b\thunt/2026-08-08", commands)
            self.assertIn("hunt: publish 2026-08-08 report and ledger", commands)
            self.assertIn("git\tpush\t-u\torigin\thunt/2026-08-08", commands)

    def test_recovery_does_not_use_date_report_counts_as_authority(self):
        recovery = section(
            "while :; do\n  if find_pending_archive_report_view",
            "today_sa=$(sa_today)",
        )
        self.assertIn("verify_pending_report_binding", recovery)
        self.assertNotIn("reports_today", recovery)
        self.assertNotIn("report-count-before", recovery)
        self.assertIn("today=$RECOVERY_DATE", recovery)
        self.assertIn('seal_decision_outcome "$sa_count"', HUNT)
        self.assertIn('[ "$RECOVERY_CURRENT_DECISION_COMPLETE" -eq 0 ]', HUNT)
        self.assertIn('"report-binding.json"', HUNT)
        self.assertIn("os.link(temporary, destination", HUNT)
        self.assertEqual(HUNT.count("./publish.sh"), 1)
        self.assertIn('publish_hunt_for_date "$today"', HUNT)


if __name__ == "__main__":
    unittest.main()
