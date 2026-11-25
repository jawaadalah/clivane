#!/usr/bin/env python3
"""Smoke test for this generated CLI: --help everywhere must exit 0."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRY = os.path.join(HERE, "gemgrid_game_arena_client.py")


def main():
    import json
    manifest = json.load(open(os.path.join(HERE, "manifest.json")))
    calls = [["--help"]] + [[c, "--help"] for c in manifest["commands"]]
    for argv in calls:
        proc = subprocess.run([sys.executable, ENTRY] + argv,
                              capture_output=True, text=True)
        if proc.returncode != 0:
            print("FAIL", argv, proc.stderr)
            return 1
    print("smoke OK: %d help calls" % len(calls))
    return 0


if __name__ == "__main__":
    sys.exit(main())
