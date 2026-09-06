#!/usr/bin/env python3
"""Regression coverage for defensive AwR output capture and final publication."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
LEDGER_ROW = (
    "2026-08-09\thunt\tDefensive capture\tCapture hostile output safely"
    "\taccept-w-rev\tNeeds revision\tlow\tdesign-fixable\n"
)
PROVIDERS = ("codex", "kimi", "grok", "opencode", "agy", "claude")


class AwrSideOutputCaptureRegression(unittest.TestCase):
    def make_repo(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="awr-output-capture-"))
        self.addCleanup(shutil.rmtree, root, True)
        shutil.copytree(ROOT / "lib", root / "lib")
        shutil.copytree(ROOT / "roles", root / "roles")
        shutil.copytree(ROOT / "history", root / "history")
        for relative in (
            "awr-side.sh",
            "rubric.md",
            "brainstorming_policy.md",
        ):
            shutil.copy2(ROOT / relative, root / relative)
        test_bin = root / ".test-bin"
        test_bin.mkdir()
        for provider in PROVIDERS:
            destination = test_bin / provider
            shutil.copy2(
                ROOT / "tests" / "fake_portable_stage_provider.py",
                destination,
            )
            destination.chmod(0o755)
        (root / "ledger.tsv").write_text(LEDGER_ROW, encoding="utf-8")
        return root

    def run_awr(
        self,
        root: Path,
        *,
        max_rounds: int = 3,
        max_bad: int = 3,
    ) -> subprocess.CompletedProcess[str]:
        home = root / "provider-home"
        home.mkdir(exist_ok=True)
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / "codex-config"),
                "PATH": f"{root / '.test-bin'}:{os.environ['PATH']}",
                "HISTORY_RUNTIME_ABI": "v2",
                "AWR_PROVIDER": "claude",
                "AWR_RESEARCH_PROVIDER": "claude",
                "AWR_PRIORWORK_PROVIDER": "claude",
                "AWR_JUDGE_PROVIDER": "claude",
                "SIDE_GAP_MIN_SEC": "0",
                "SIDE_GAP_MAX_SEC": "0",
                "SIDE_POLL_SEC": "0",
                "SIDE_COOLDOWN_SEC": "0",
                "SIDE_MAX_BAD": str(max_bad),
                "SIDE_MAX_ROUNDS": str(max_rounds),
            }
        )
        return subprocess.run(
            ["bash", "./awr-side.sh"],
            cwd=root,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )

    def seed_ready_retry(
        self, root: Path, *, with_crack_verification: bool
    ) -> dict[str, Path]:
        outdir = root / "tmp/awr-side/awr"
        outdir.mkdir(parents=True)
        key = "r000001"
        paths = {
            "task": outdir / f"{key}.task.md",
            "draft": outdir / f"{key}.draft.md",
            "priorwork": outdir / f"{key}.priorwork.md",
            "judge": outdir / f"{key}.judge.md",
            "final": outdir / f"{key}.md",
        }
        paths["task"].write_text(
            "# AwR Task r000001\n"
            "Date: 2026-08-09\nTheme: Defensive capture\n"
            "Idea: Capture hostile output safely\nReason: Needs revision\n",
            encoding="utf-8",
        )
        paths["draft"].write_text(
            "## Revised Idea\nA complete bounded capture design.\n\n"
            "## Search Record\n"
            "- record https://example.com/a\n"
            "- record https://example.com/b\n"
            "- record https://example.com/c\n\n"
            "## Response\n"
            "Resolved Crack URL Verification: Replaced broken records with stable URLs.\n"
            "AGY-DONE\n",
            encoding="utf-8",
        )
        crack = ""
        if with_crack_verification:
            crack = (
                "## Crack Evidence Verification\n"
                "- https://example.com/crack | Verification: supports — "
                "The source supports bounded capture.\n"
            )
        paths["priorwork"].write_text(
            "## Independent Prior Work\n"
            "Search Terms: bounded output capture\n"
            "- Query: https://export.arxiv.org/api/query?search_query=all:capture\n"
            "Nearest Work:\n"
            "- paper https://example.com/1\n"
            "- paper https://example.com/2\n"
            "- paper https://example.com/3\n"
            "- paper https://example.com/4\n"
            "- paper https://example.com/5\n"
            "Strongest Counterexample: None found.\n"
            "Overlap: low\nPapers Read: 5\narXiv ID Check: complete\n"
            f"{crack}"
            "AGY-DONE\n",
            encoding="utf-8",
        )
        paths["judge"].write_text(
            "Decision: SA-possible\nAGY-DONE\n",
            encoding="utf-8",
        )
        static_inputs = (
            root / "roles/awr-judge.md",
            root / "rubric.md",
            root / "brainstorming_policy.md",
        )
        base = max(path.stat().st_mtime_ns for path in static_inputs) + 10_000_000_000
        for index, name in enumerate(("task", "draft", "priorwork", "judge")):
            timestamp = base + index * 1_000_000_000
            os.utime(paths[name], ns=(timestamp, timestamp))
        return paths

    def test_incidental_verification_prose_isolated(self) -> None:
        for with_crack_verification in (False, True):
            with self.subTest(with_crack_verification=with_crack_verification):
                root = self.make_repo()
                paths = self.seed_ready_retry(
                    root, with_crack_verification=with_crack_verification
                )

                result = self.run_awr(root)

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.count("Starting [review:r000001 round 1]"), 1)
                published = paths["final"].read_text(encoding="utf-8")
                self.assertIn("Status: ready\n", published)
                self.assertIn(
                    "Resolved Crack URL Verification: Replaced broken records",
                    published,
                )
                self.assertIn("## Independent Prior-Work Evidence\n", published)
                self.assertIn("## Final Reviewer Decision\n", published)
                outdir = paths["final"].parent
                self.assertEqual(list(outdir.glob(".final-check.*")), [])
                self.assertEqual(list(outdir.glob(".r000001.final.*")), [])

    def test_crlf_layout_and_inconsistent_not_ready_decision_are_rejected(self) -> None:
        for case in ("crlf", "bare-cr", "not-ready-sa-possible"):
            with self.subTest(case=case):
                root = self.make_repo()
                paths = self.seed_ready_retry(root, with_crack_verification=True)
                status = "not-ready" if case == "not-ready-sa-possible" else "ready"
                final_text = (
                    "# AwR Result r000001\n"
                    f"Status: {status}\n"
                    "Outcome: terminal fixture\n"
                    "Original Idea: Capture hostile output safely\n"
                    "Process Record: r000001.task.md\n\n"
                    f"{paths['draft'].read_text(encoding='utf-8')}"
                    "\n---\n## Independent Prior-Work Evidence\n"
                    f"{paths['priorwork'].read_text(encoding='utf-8')}"
                    "\n---\n## Final Reviewer Decision\n"
                    f"{paths['judge'].read_text(encoding='utf-8')}"
                )
                if case == "crlf":
                    final_text = final_text.replace(
                        "## Independent Prior-Work Evidence\n",
                        "## Independent Prior-Work Evidence\r\n",
                    )
                elif case == "bare-cr":
                    final_text = final_text.replace(
                        "## Independent Prior-Work Evidence\n",
                        "## Independent Prior-Work Evidence\r",
                    )
                paths["final"].write_bytes(final_text.encode("utf-8"))

                result = self.run_awr(root, max_bad=0)

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(paths["final"].exists())
                self.assertTrue(
                    (paths["final"].parent / "r000001.final.bad1").is_file()
                )

    def test_truncated_existing_final_is_quarantined_and_replaced(self) -> None:
        root = self.make_repo()
        outdir = root / "tmp/awr-side/awr"
        outdir.mkdir(parents=True)
        key = "r000001"
        task = outdir / f"{key}.task.md"
        draft = outdir / f"{key}.draft.md"
        priorwork = outdir / f"{key}.priorwork.md"
        judge = outdir / f"{key}.judge.md"
        final = outdir / f"{key}.md"

        task.write_text(
            "# AwR Task r000001\n"
            "Date: 2026-08-09\nTheme: Defensive capture\n"
            "Idea: Capture hostile output safely\nReason: Needs revision\n\n"
            "## Reviewer Feedback\nRound: 1\n- Defect: preserve stable bytes\n",
            encoding="utf-8",
        )
        draft.write_text(
            "## Revised Idea\nA complete bounded capture design.\n\n"
            "## Search Record\n"
            "- record https://example.com/a\n"
            "- record https://example.com/b\n"
            "- record https://example.com/c\n"
            "AGY-DONE\n",
            encoding="utf-8",
        )
        priorwork.write_text(
            "- Query: https://export.arxiv.org/api/query?search_query=all:capture\n"
            "Nearest Work:\n"
            "- paper https://example.com/1\n"
            "- paper https://example.com/2\n"
            "- paper https://example.com/3\n"
            "- paper https://example.com/4\n"
            "- paper https://example.com/5\n"
            "Strongest Counterexample: None found.\n"
            "Overlap: low\nPapers Read: 5\narXiv ID Check: complete\n"
            "AGY-DONE\n",
            encoding="utf-8",
        )
        judge.write_text(
            "Decision: not-ready\n- Defect: one issue remains\nAGY-DONE\n",
            encoding="utf-8",
        )
        final.write_text("# AwR Result r000001\nStatus: ready\n", encoding="utf-8")
        base = 1_700_000_000_000_000_000
        for index, path in enumerate((task, draft, priorwork, judge, final)):
            timestamp = base + index * 1_000_000_000
            os.utime(path, ns=(timestamp, timestamp))

        result = self.run_awr(root, max_rounds=1, max_bad=1)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        quarantined = outdir / f"{key}.final.bad1"
        self.assertEqual(
            quarantined.read_text(encoding="utf-8"),
            "# AwR Result r000001\nStatus: ready\n",
        )
        published = final.read_text(encoding="utf-8")
        self.assertIn("Status: not-ready\n", published)
        self.assertIn("## Independent Prior-Work Evidence\n", published)
        self.assertIn("## Final Reviewer Decision\n", published)
        self.assertTrue(published.rstrip().endswith("AGY-DONE"))
        state = final.stat()
        self.assertTrue(stat.S_ISREG(state.st_mode))
        self.assertEqual(state.st_nlink, 1)
        self.assertEqual(list(outdir.glob(f".{key}.final.*")), [])


if __name__ == "__main__":
    unittest.main()
