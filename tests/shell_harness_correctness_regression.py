#!/usr/bin/env python3
import os
import pathlib
import subprocess
import tempfile
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
HUNT = (ROOT / "hunt.sh").read_text(encoding="utf-8")
AWR = (ROOT / "awr-side.sh").read_text(encoding="utf-8")
CALIB = (ROOT / "calib/run_panel.sh").read_text(encoding="utf-8")


def section(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    finish = source.index(end, begin)
    return source[begin:finish]


class ShellHarnessCorrectnessRegression(unittest.TestCase):
    def test_hunt_lock_is_exclusive_and_reacquirable(self):
        lock_function = section(HUNT, "acquire_hunt_lock() {", "pick_lens() {")
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            script = root / "lock-test.sh"
            marker = root / "held"
            script.write_text(
                "#!/usr/bin/env bash\nset -u\n"
                + f"LOCK={str(root / 'hunt.lock')!r}\n"
                + "LOCK_STATUS=\nLOCK_HOLDER_PID=\n"
                + "log() { printf '%s\\n' \"$*\" >&2; }\n"
                + lock_function
                + "cleanup() {\n"
                + "  rm -f \"$LOCK_STATUS\"\n"
                + "  if [ -n \"$LOCK_HOLDER_PID\" ]; then\n"
                + "    kill \"$LOCK_HOLDER_PID\" 2>/dev/null || true\n"
                + "    wait \"$LOCK_HOLDER_PID\" 2>/dev/null || true\n"
                + "  fi\n}\ntrap cleanup EXIT\n"
                + "acquire_hunt_lock || exit 2\n"
                + f"[ \"${{1:-}}\" != hold ] || {{ : > {str(marker)!r}; sleep 1; }}\n",
                encoding="utf-8",
            )
            first = subprocess.Popen(["bash", str(script), "hold"])
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), "first lock holder never became ready")
            busy = subprocess.run(["bash", str(script)], check=False)
            self.assertEqual(busy.returncode, 2)
            self.assertEqual(first.wait(timeout=5), 0)
            reacquired = subprocess.run(["bash", str(script)], check=False)
            self.assertEqual(reacquired.returncode, 0)
            self.assertTrue((root / "hunt.lock").is_file())

    def test_hunt_lock_rejects_hardlinked_lock_file(self):
        lock_function = section(HUNT, "acquire_hunt_lock() {", "pick_lens() {")
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            external = root / "external.txt"
            external.write_text("must-not-change\n", encoding="utf-8")
            lock = root / "hunt.lock"
            os.link(external, lock)
            script = root / "hardlink-test.sh"
            script.write_text(
                "#!/usr/bin/env bash\nset -u\n"
                + f"LOCK={str(lock)!r}\n"
                + "LOCK_STATUS=\nLOCK_HOLDER_PID=\n"
                + "log() { :; }\n"
                + lock_function
                + "cleanup() {\n"
                + "  rm -f \"$LOCK_STATUS\"\n"
                + "  if [ -n \"$LOCK_HOLDER_PID\" ]; then\n"
                + "    kill \"$LOCK_HOLDER_PID\" 2>/dev/null || true\n"
                + "    wait \"$LOCK_HOLDER_PID\" 2>/dev/null || true\n"
                + "  fi\n}\ntrap cleanup EXIT\n"
                + "acquire_hunt_lock\n",
                encoding="utf-8",
            )
            result = subprocess.run(["bash", str(script)], check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(external.read_text(encoding="utf-8"), "must-not-change\n")

    def test_report_failure_is_bounded_without_round_reset(self):
        finalize = section(HUNT, "finalize_strong_accept() {", "fail_round() {")
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            attempts = root / "attempts"
            script = root / "report-test.sh"
            script.write_text(
                "#!/usr/bin/env bash\nset -u\n"
                + "fails=0\nMAX_FAILS=3\nFAIL_SLEEP_MIN=0\nBACK_CMD=false\nLOG=/dev/null\n"
                + f"ATTEMPTS={str(attempts)!r}\n"
                + "reports_today() { printf '0\\n'; }\n"
                + "run_external_stage() { n=$(cat \"$ATTEMPTS\" 2>/dev/null || printf 0); printf '%s\\n' $((n + 1)) > \"$ATTEMPTS\"; return 1; }\n"
                + "sleep_minutes() { :; }\nlog() { :; }\nrefresh_published_archive() { return 0; }\n"
                + finalize
                + "finalize_strong_accept\n",
                encoding="utf-8",
            )
            result = subprocess.run(["bash", str(script)], check=False)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(attempts.read_text(encoding="utf-8").strip(), "3")

    def test_startup_recovers_verified_pending_archives_before_target_gate(self):
        pending = HUNT.index("while :; do\n  if find_pending_archive_report_view")
        target = HUNT.index('if [ "$SA_TARGET" -gt 0 ]', pending)
        self.assertLess(pending, target)
        self.assertIn('reason="decision"', HUNT)
        self.assertIn('archive.name != fields["run_id"]', HUNT)
        self.assertIn('log "Recovering missing report for pending Strong Accept archive $run_id"', HUNT)
        self.assertIn('log "Recovering publication for pending Strong Accept archive $run_id"', HUNT)
        self.assertIn("publish_existing_strong_accept_report || exit $?", HUNT)
        self.assertIn("finalize_strong_accept || exit $?", HUNT)
        self.assertNotIn("archive_round published || true", HUNT)
        self.assertIn('archive_round published "$source"', HUNT)
        self.assertIn('--startup "$source_root/history/startup.json"', HUNT)
        self.assertIn('--projection "$source_root/history/materialize-ledger.json"', HUNT)
        self.assertIn('reports_today > "$ARCHIVE_SOURCE/history/report-count-before"', HUNT)

    def test_theme_gate_uses_exact_closed_vocabulary(self):
        theme_functions = section(HUNT, "theme_in_vocabulary() {", "axiom_ok() {")
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            (root / "brainstorming_policy.md").write_text(
                "## Theme Vocabulary\n\n"
                "World Models - Architecture / Data Engines / Safety and Robustness\n\n"
                "A later sentence mentions Architecture and data engines.\n"
                "## Divergence Lenses\n",
                encoding="utf-8",
            )
            ideas = root / "ideas.tsv"
            script = root / "theme-test.sh"
            script.write_text(
                "#!/usr/bin/env bash\nset -u\nRD=.\nlog() { :; }\n"
                + theme_functions
                + "themes_ok ideas.tsv\n",
                encoding="utf-8",
            )
            ideas.write_text("I1\tstory\tData Engines\n", encoding="utf-8")
            valid = subprocess.run(["bash", str(script)], cwd=root, check=False)
            self.assertEqual(valid.returncode, 0)
            ideas.write_text("I1\tstory\tArchitecture\n", encoding="utf-8")
            substring = subprocess.run(["bash", str(script)], cwd=root, check=False)
            self.assertNotEqual(substring.returncode, 0)
            ideas.write_text("I1\tstory\tdata engines\n", encoding="utf-8")
            wrong_case = subprocess.run(["bash", str(script)], cwd=root, check=False)
            self.assertNotEqual(wrong_case.returncode, 0)

    def test_agy_gap_is_shared_across_repo_mirrors(self):
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            worker = root / "agy-worker.sh"
            worker.write_text((ROOT / "agy-worker.sh").read_text(encoding="utf-8"), encoding="utf-8")
            worker.chmod(0o755)
            bindir = root / "bin"
            bindir.mkdir()
            capture = root / "prompt.txt"
            fake = bindir / "agy"
            fake.write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" > \"$CAPTURE\"\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            mirrors = [root / "seat-1", root / "seat-2"]
            for mirror in mirrors:
                mirror.mkdir()
            env = os.environ.copy()
            env.update(
                {
                    "PATH": str(bindir) + os.pathsep + env.get("PATH", ""),
                    "AGY_LAUNCH_GAP_SEC": "2",
                    "AGY_OUT_HINT": "tmp/out/",
                    "CAPTURE": str(capture),
                }
            )
            first_env = env | {"AGY_REPO": str(mirrors[0])}
            second_env = env | {"AGY_REPO": str(mirrors[1])}
            subprocess.run([str(worker), "first"], env=first_env, check=True)
            started = time.monotonic()
            subprocess.run([str(worker), "second"], env=second_env, check=True)
            self.assertGreaterEqual(time.monotonic() - started, 0.8)
            self.assertTrue((root / "tmp/agy.last-launch").is_file())
            self.assertFalse((mirrors[0] / "tmp/agy.last-launch").exists())
            self.assertFalse((mirrors[1] / "tmp/agy.last-launch").exists())
            prompt = capture.read_text(encoding="utf-8")
            self.assertIn(f"Write artifacts only under {mirrors[1]}/tmp/out/", prompt)

    def test_harnesses_forward_agy_output_hints(self):
        self.assertIn('AGY_OUT_HINT="$(dirname "$rel")/" $cmd', AWR)
        self.assertIn("*agy-worker.sh) gate; is_agy_wrapper=1", AWR)
        self.assertIn("AGY_LAUNCH_GAP_SEC=0 $cmd", AWR)
        self.assertIn("AGY_OUT_HINT=tmp/out/ GROK_DISABLE_WEB=1", CALIB)


if __name__ == "__main__":
    unittest.main()
