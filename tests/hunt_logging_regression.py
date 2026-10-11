#!/usr/bin/env python3
"""Hunt must advance even when its console stops accepting output."""
import fcntl
import os
import pathlib
import pty
import shlex
import signal
import subprocess
import tempfile
import termios
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
HUNT = (ROOT / "hunt.sh").read_text(encoding="utf-8")
LOG_FUNCTION = HUNT.split("log() {\n", 1)[1].split(
    "\ncopy_mutable_round_view() {", 1
)[0]
LOG_FUNCTION = "log() {\n" + LOG_FUNCTION
RUN_LOG_SETUP = HUNT.split('validate_config\nmkdir -p tmp "$HISTORY_STATE_ROOT"', 1)[
    1
].split("acquire_hunt_lock || exit 2", 1)[0]
RUN_LOG_SETUP = 'validate_config\nmkdir -p tmp "$HISTORY_STATE_ROOT"' + RUN_LOG_SETUP


def fill_output_queue(descriptor):
    flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
    fcntl.fcntl(descriptor, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    try:
        while True:
            os.write(descriptor, b"x" * 4096)
    except BlockingIOError:
        pass
    finally:
        fcntl.fcntl(descriptor, fcntl.F_SETFL, flags)


class HuntLoggingRegression(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = pathlib.Path(self.scratch.name)
        self.log = self.root / "hunt.log"
        self.marker = self.root / "next-stage"
        self.script = self.root / "logging.sh"
        self.script.write_text(
            "#!/usr/bin/env bash\nset -u\n"
            + f"LOG={shlex.quote(str(self.log))}\n"
            + LOG_FUNCTION
            + '\nlog "$1"\nstatus=$?\n'
            + f'printf "%s\\n" "$status" > {shlex.quote(str(self.marker))}\n'
            + 'exit "$status"\n',
            encoding="utf-8",
        )

    def run_log(self, message, stdout, stderr=subprocess.PIPE):
        process = subprocess.Popen(
            ["bash", str(self.script), message],
            cwd=ROOT,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            return process.communicate(timeout=3), process.returncode
        except subprocess.TimeoutExpired:
            raise self.failureException(
                "Hunt did not advance past logging within 3 seconds"
            ) from None
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def assert_logged(self, message):
        self.assertEqual(self.marker.read_text(), "0\n")
        raw = self.log.read_bytes()
        self.assertRegex(
            raw,
            rb"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ",
        )
        self.assertEqual(raw[22:], message.encode("utf-8") + b"\n")
        return raw

    def test_writable_console_receives_complete_timestamped_message(self):
        message = "Starting select: Memory VLA; 中文路径"
        (console, errors), status = self.run_log(message, subprocess.PIPE)
        self.assertEqual(status, 0)
        self.assertEqual(errors, b"")
        self.assertEqual(console, self.assert_logged(message))

    def test_paused_terminal_does_not_block_the_next_stage(self):
        master, slave = pty.openpty()
        try:
            termios.tcflow(slave, termios.TCOOFF)
            fill_output_queue(slave)
            (_, _), status = self.run_log("Starting select", slave, slave)
            self.assertEqual(status, 0)
            self.assert_logged("Starting select")
        finally:
            termios.tcflow(slave, termios.TCOON)
            os.close(slave)
            os.close(master)

    def test_unread_full_pipe_preserves_file_log_and_descriptor_flags(self):
        read_fd, write_fd = os.pipe()
        try:
            fill_output_queue(write_fd)
            flags = fcntl.fcntl(write_fd, fcntl.F_GETFL)
            message = "Starting research " + "evidence " * 8000
            (_, errors), status = self.run_log(message, write_fd)
            self.assertEqual(status, 0)
            self.assertEqual(errors, b"")
            self.assert_logged(message)
            self.assertEqual(fcntl.fcntl(write_fd, fcntl.F_GETFL), flags)
        finally:
            os.close(write_fd)
            os.close(read_fd)

    def test_closed_pipe_keeps_logging_successful(self):
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        try:
            (_, errors), status = self.run_log("Round complete", write_fd)
            self.assertEqual(status, 0)
            self.assertEqual(errors, b"")
            self.assert_logged("Round complete")
        finally:
            os.close(write_fd)

    def test_file_failure_returns_failure_without_writing_to_paused_terminal(self):
        self.log.mkdir()
        master, slave = pty.openpty()
        try:
            termios.tcflow(slave, termios.TCOOFF)
            fill_output_queue(slave)
            (_, _), status = self.run_log("File append must fail", slave, slave)
            self.assertNotEqual(status, 0)
            self.assertNotEqual(self.marker.read_text(), "0\n")
        finally:
            termios.tcflow(slave, termios.TCOON)
            os.close(slave)
            os.close(master)

    def test_runtime_traceback_reaches_failure_handling_with_paused_terminal(self):
        freeze = HUNT.split("history_freeze_batch() {", 1)[1].split(
            "history_publish_round_direction() {", 1
        )[0]
        self.script.write_text(
            "#!/usr/bin/env bash\nset -u\n"
            + f"LOG={shlex.quote(str(self.log))}\n"
            + f"HISTORY_STATE_ROOT={shlex.quote(str(self.root / 'state'))}\n"
            + "validate_config() { :; }\n"
            + RUN_LOG_SETUP
            + "history_freeze_batch() {" + freeze
            + '\nhistory_freeze_batch "$1" "$1" "$1" "$1" "" "$1" > /dev/null\n'
            + f'printf "%s\\n" "$?" > {shlex.quote(str(self.marker))}\n',
            encoding="utf-8",
        )
        master, slave = pty.openpty()
        try:
            termios.tcflow(slave, termios.TCOOFF)
            fill_output_queue(slave)
            (_, _), status = self.run_log(str(self.root / "missing"), slave, slave)
            self.assertEqual(status, 0)
            self.assertEqual(self.marker.read_text(), "1\n")
            self.assertIn("Traceback", self.log.read_text())
            self.assertIn(
                "RuntimeContractError: generation brief is unavailable",
                self.log.read_text(),
            )
        finally:
            termios.tcflow(slave, termios.TCOON)
            os.close(slave)
            os.close(master)

    def test_runtime_logging_setup_fails_without_blocking_on_stderr(self):
        self.log.mkdir()
        self.script.write_text(
            "#!/usr/bin/env bash\nset -u\n"
            + f"LOG={shlex.quote(str(self.log))}\n"
            + f"HISTORY_STATE_ROOT={shlex.quote(str(self.root / 'state'))}\n"
            + "validate_config() { :; }\n"
            + RUN_LOG_SETUP
            + f': > {shlex.quote(str(self.marker))}\n',
            encoding="utf-8",
        )
        master, slave = pty.openpty()
        try:
            termios.tcflow(slave, termios.TCOOFF)
            fill_output_queue(slave)
            (_, _), status = self.run_log("", slave, slave)
            self.assertEqual(status, 2)
            self.assertFalse(self.marker.exists())
        finally:
            termios.tcflow(slave, termios.TCOON)
            os.close(slave)
            os.close(master)


if __name__ == "__main__":
    unittest.main()
