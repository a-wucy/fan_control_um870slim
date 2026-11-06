#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Run userspace and shared C-core tests, with no hardware or root access."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile


root = Path(__file__).resolve().parents[1]
subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(root / "tests"), "-v"], check=True)
with tempfile.TemporaryDirectory(prefix="system-fan-tests-") as directory:
    executable = Path(directory) / "test_core"
    subprocess.run(shlex.split(os.environ.get("CC", "cc")) + [
        "-std=c11", "-Wall", "-Wextra", "-Werror", "-g",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
        str(root / "tests" / "test_core.c"), "-o", str(executable),
    ], check=True)
    subprocess.run([str(executable)], check=True)
