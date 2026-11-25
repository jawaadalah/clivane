#!/usr/bin/env python3
"""mirror-ls - clivane mirror wrapper around the `ls` CLI.

Forwards every argument to the real tool. Add `--json` anywhere to get a
structured envelope instead of raw output:
  {"tool": "ls", "argv": [...], "exit_code": int,
   "stdout": "...", "stderr": "..."}
"""
import json
import os
import subprocess
import sys

TOOL = "ls"


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in args
    if json_mode:
        args.remove("--json")
    proc = subprocess.run([TOOL] + args, capture_output=True, text=True)
    if json_mode:
        print(json.dumps({
            "tool": os.path.basename(TOOL),
            "argv": args,
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }, indent=2, sort_keys=True))
    else:
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
