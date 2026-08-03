#!/usr/bin/env python3
"""Record Hunt's audit-v2 CLI routing, then delegate to the real CLI."""

import os
import pathlib
import runpy
import sys


log = os.environ.get("HISTORY_AUDIT_CLI_CALL_LOG")
if log and len(sys.argv) > 1:
    with open(log, "a", encoding="utf-8") as stream:
        stream.write(sys.argv[1] + "\n")

real = pathlib.Path(__file__).with_name("history_audit_cli_real.py")
if not real.is_file():
    raise SystemExit("history audit CLI recorder has no delegate")
runpy.run_path(str(real), run_name="__main__")
