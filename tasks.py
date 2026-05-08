#!/usr/bin/env python3

from pathlib import Path

import invoke

ROOT = Path(__file__).parent
SHELL = "/bin/sh"


@invoke.task
def format(c, check=False):
    dirs = ["src", "tests"]

    dirs = " ".join([str(ROOT / d) for d in dirs])

    format_command = f"ruff format {dirs}"
    lint_command = f"ruff check {dirs}"

    if check:
        format_command += " --check"
    else:
        lint_command += " --fix"

    print("Formatting")
    c.run(format_command, shell=SHELL)
    print("Linting")
    c.run(lint_command, shell=SHELL)
