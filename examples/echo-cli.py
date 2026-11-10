#!/usr/bin/env python3
"""Echo CLI — example declarative custom backend for swrm (M3 slice 1, #101).

A stand-in for any real CLI agent: it takes the task prompt, writes
``echo-output.txt`` into the worktree, and prints a JSON result object.
Wire it up with the sibling ``custom-backends.echo.json``:

    openswarm run --backend echo --backends-file examples/custom-backends.echo.json \
        --prompt "add a file echo-output.txt with a greeting" \
        --verify "test -f echo-output.txt"

Stdlib only.
"""
import argparse
import json
import os
import sys


def main():
    parser = argparse.ArgumentParser(description="Echo CLI example backend")
    parser.add_argument("--prompt", required=True, help="task text from openswarm")
    parser.add_argument("--workdir", default=".", help="worktree to work in")
    parser.add_argument("--model", default="", help="model name from the backend config")
    args = parser.parse_args()
    with open(os.path.join(args.workdir, "echo-output.txt"), "w") as fh:
        fh.write("echo: %s\nmodel: %s\n" % (args.prompt, args.model))
    print(json.dumps({"status": "done", "summary": "echoed the prompt",
                      "files_changed": ["echo-output.txt"], "tests": "no tests claimed"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
