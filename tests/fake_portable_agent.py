#!/usr/bin/env python3
"""Offline portable-agent fixture; accepts every supported CLI argv shape."""

import json
import os
import pathlib
import subprocess
import sys
import time


def _prompt(argv):
    for flag in ("-p", "--print"):
        if flag in argv:
            return argv[argv.index(flag) + 1]
    return argv[-1]


def _write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    path.write_bytes(raw)
    os.chmod(path, 0o600)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--delayed-child":
        time.sleep(0.8)
        _write(pathlib.Path(sys.argv[2]), b"child survived\n")
        return 0

    request = json.loads(_prompt(sys.argv[1:]))
    mode = request["mode"]
    output = pathlib.Path("output/result.json")
    valid = b'{"request_id":"request-1","status":"ok"}\n'
    if mode in {"success", "undeclared-read"}:
        if request.get("audit_environment"):
            forbidden = {
                "PWD",
                "OLDPWD",
                "GIT_DIR",
                "HISTORY_DB",
                "HUNT_RUNTIME_ABI",
                "AWR_RUNTIME_ABI",
                "CONTAINED_AGENT_CMD_JSON",
                "AGENT_CMD",
            }
            if forbidden.intersection(os.environ):
                return 25
        if mode == "undeclared-read":
            forbidden = ["ledger.tsv", "history.sqlite3", ".git/config"]
            if any(pathlib.Path(path).exists() for path in forbidden):
                return 19
            for path in pathlib.Path(".").rglob("*"):
                expected = 0o700 if path.is_dir() else 0o600
                if path.lstat().st_mode & 0o777 != expected:
                    return 20
        _write(output, valid)
        return 0
    if mode == "success-background-child":
        _write(output, valid)
        subprocess.Popen(
            [
                sys.executable,
                __file__,
                "--delayed-child",
                request["delayed_path"],
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return 0
    if mode == "mode-zero":
        _write(output, valid)
        locked = pathlib.Path(".tmp/locked")
        _write(locked / "cache", b"locked\n")
        os.chmod(locked, 0)
        return 0
    if mode == "stdout-flood":
        _write(output, valid)
        sys.stdout.buffer.write(b"x" * (1024 * 1024))
        return 0
    if mode == "extra-file":
        _write(output, valid)
        _write(pathlib.Path("output/extra.txt"), b"extra\n")
        return 0
    if mode == "symlink":
        target = pathlib.Path("output/real.json")
        _write(target, valid)
        output.symlink_to("real.json")
        return 0
    if mode == "hardlink":
        target = pathlib.Path("output/real.json")
        _write(target, valid)
        os.link(target, output)
        return 0
    if mode == "oversize":
        _write(output, b"x" * 8192)
        return 0
    if mode == "malformed-json":
        _write(output, b"{bad json\n")
        return 0
    if mode == "nonzero":
        return 23
    if mode == "timeout":
        delayed_path = request.get("delayed_path", "output/late.txt")
        subprocess.Popen(
            [sys.executable, __file__, "--delayed-child", delayed_path]
        )
        time.sleep(60)
        return 0
    return 24


if __name__ == "__main__":
    raise SystemExit(main())
