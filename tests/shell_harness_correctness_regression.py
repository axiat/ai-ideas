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

    def test_awr_wrapper_shares_worker_launch_root_without_double_gate(self):
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            repo = root / "repo"
            repo.mkdir()
            for relative in ("awr-side.sh", "agy-worker.sh", "rubric.md", "brainstorming_policy.md"):
                target = repo / relative
                target.write_bytes((ROOT / relative).read_bytes())
            for relative in (
                "lib/resolve_cmd.sh",
                "roles/awr.md",
                "roles/awr-priorwork.md",
                "roles/awr-judge.md",
                "tests/fake_agent.sh",
            ):
                target = repo / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / relative).read_bytes())
            for relative in ("awr-side.sh", "agy-worker.sh", "tests/fake_agent.sh"):
                (repo / relative).chmod(0o755)
            (repo / "tmp").mkdir()

            ledger_lines = (ROOT / "ledger.tsv").read_text(encoding="utf-8").splitlines()
            selected = [ledger_lines[0]]
            for line in ledger_lines[1:]:
                fields = line.split("\t")
                if len(fields) > 4 and fields[1] == "hunt" and fields[4] == "accept-w-rev":
                    selected.append(line)
                    break
            self.assertEqual(len(selected), 2)
            (repo / "ledger.tsv").write_text("\n".join(selected) + "\n", encoding="utf-8")

            bindir = root / "bin"
            bindir.mkdir()
            launch_log = root / "launches.tsv"
            release = root / "release"
            fake_agy = bindir / "agy"
            fake_agy.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys, time\n"
                "prompt = sys.argv[-1]\n"
                "label = 'external' if 'external-probe' in prompt else 'wrapper'\n"
                "with open(os.environ['LAUNCH_LOG'], 'a') as stream:\n"
                "    stream.write(f'{label}\\t{time.time_ns()}\\n')\n"
                "    stream.flush()\n"
                "    os.fsync(stream.fileno())\n"
                "if label == 'external':\n"
                "    deadline = time.monotonic() + 15\n"
                "    while not os.path.exists(os.environ['RELEASE_FILE']):\n"
                "        if time.monotonic() >= deadline:\n"
                "            raise SystemExit(124)\n"
                "        time.sleep(0.02)\n"
                "    raise SystemExit(0)\n"
                "agent = os.environ['FAKE_AGENT_BIN']\n"
                "os.execv(agent, [agent, prompt])\n",
                encoding="utf-8",
            )
            fake_agy.chmod(0o755)
            shared_root = root / "shared-launch-root"
            shared_root.mkdir()
            env = os.environ.copy()
            env.update(
                {
                    "PATH": str(bindir) + os.pathsep + env.get("PATH", ""),
                    "AGY_LAUNCH_ROOT": str(shared_root),
                    "AGY_LAUNCH_GAP_SEC": "2",
                    "LAUNCH_LOG": str(launch_log),
                    "RELEASE_FILE": str(release),
                    "FAKE_AGENT_BIN": str(repo / "tests/fake_agent.sh"),
                    "FAKE_AGENT_MODE": "awr-ready",
                }
            )
            external = None
            awr = None
            try:
                external = subprocess.Popen(
                    [str(repo / "agy-worker.sh"), "external-probe"],
                    cwd=repo,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                deadline = time.monotonic() + 5
                while (
                    (not launch_log.exists() or not launch_log.read_text(encoding="utf-8").strip())
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.02)
                self.assertTrue(launch_log.exists(), "external worker never reached launch barrier")

                awr_env = env | {
                    "HISTORY_RUNTIME_ABI": "v1",
                    "SIDE_CMD": "./agy-worker.sh",
                    "SIDE_RESEARCH_CMD": "./agy-worker.sh",
                    "SIDE_PRIORWORK_CMD": "./agy-worker.sh",
                    "SIDE_JUDGE_CMD": "./agy-worker.sh",
                    "SIDE_POLL_SEC": "0",
                    "SIDE_MAX_ROUNDS": "1",
                    "SIDE_MAX_BAD": "1",
                    "SIDE_GAP_SEC": "2",
                    "SIDE_GAP_MIN_SEC": "0",
                    "SIDE_GAP_MAX_SEC": "0",
                    "SIDE_COOLDOWN_SEC": "0",
                }
                awr = subprocess.Popen(
                    ["bash", "./awr-side.sh"],
                    cwd=repo,
                    env=awr_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                deadline = time.monotonic() + 6
                while (
                    len(launch_log.read_text(encoding="utf-8").splitlines()) < 2
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.02)
                launches = launch_log.read_text(encoding="utf-8").splitlines()
                self.assertGreaterEqual(len(launches), 2, "AwR wrapper never reached launch barrier")
                first_label, first_ns = launches[0].split("\t")
                second_label, second_ns = launches[1].split("\t")
                self.assertEqual((first_label, second_label), ("external", "wrapper"))
                self.assertGreaterEqual((int(second_ns) - int(first_ns)) / 1e9, 0.75)

                release.touch()
                self.assertEqual(external.wait(timeout=5), 0)
                awr_output, _ = awr.communicate(timeout=15)
                self.assertEqual(awr.returncode, 0, awr_output)
                self.assertTrue((shared_root / "tmp/agy.last-launch").is_file())
                self.assertFalse((repo / "tmp/agy.last-launch").exists())
            finally:
                release.touch()
                for process in (external, awr):
                    if process is not None and process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=3)

    def test_harnesses_forward_agy_output_hints(self):
        self.assertIn('AGY_OUT_HINT="$(dirname "$rel")/" $cmd', AWR)
        self.assertIn("*agy-worker.sh) is_agy_wrapper=1", AWR)
        self.assertNotIn("*agy-worker.sh) gate;", AWR)
        self.assertIn('AGY_LAUNCH_GAP_SEC="$gap" $cmd', AWR)
        self.assertIn("AGY_OUT_HINT=tmp/out/ GROK_DISABLE_WEB=1", CALIB)


if __name__ == "__main__":
    unittest.main()
