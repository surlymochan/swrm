#!/usr/bin/env python3
"""Patch CLI — second example declarative custom backend for swrm (M3 slice 2, #103).

A stand-in for any real CLI agent, deliberately different from the echo
example in all three config dimensions: a different command template
(``run --task/--dir/--engine`` instead of ``--prompt/--workdir/--model``),
different env (``PATCHER_PREFIX`` / ``PATCHER_ENGINE={model}``), and a
``file`` quota source instead of ``none``. It takes the task text, writes
``patch-note.md`` into the worktree (prefix from env, engine from env or
flag), self-checks the file, and prints a JSON result object.
Wire it up with the sibling ``custom-backends.patch.json``:

    openswarm run --backend patcher --backends-file examples/custom-backends.patch.json \
        --prompt "add a file patch-note.md with a note" \
        --verify "test -f patch-note.md && grep -q patcher-note patch-note.md"

Stdlib only.
"""
import argparse
import json
import os
import sys


def main():
    parser = argparse.ArgumentParser(description="Patch CLI example backend")
    parser.add_argument("command", choices=["run"], help="subcommand")
    parser.add_argument("--task", required=True, help="task text from openswarm")
    parser.add_argument("--dir", default=".", help="worktree to work in")
    parser.add_argument("--engine", default="", help="engine name from the backend config")
    args = parser.parse_args()
    prefix = os.environ.get("PATCHER_PREFIX", "patcher-note")
    engine = os.environ.get("PATCHER_ENGINE") or args.engine
    note = "# patch note\n\nprefix: %s\nengine: %s\ntask: %s\n" % (prefix, engine, args.task)
    path = os.path.join(args.dir, "patch-note.md")
    with open(path, "w") as fh:
        fh.write(note)
    # Self-check: the file we claim must exist and carry the prefix + task.
    with open(path) as fh:
        body = fh.read()
    assert prefix in body and args.task in body, "patch-note.md self-check failed"
    print(json.dumps({"status": "done", "summary": "wrote patch-note.md",
                      "files_changed": ["patch-note.md"],
                      "tests": "self-check passed: patch-note.md present with "
                               "PATCHER_PREFIX prefix and task text"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
