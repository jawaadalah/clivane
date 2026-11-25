#!/usr/bin/env python3
"""sample-app-server client - remote CLI for a clivane serve server (stdlib only).

Same commands as the forged CLI, but each call is a POST /run to the
server. The base URL comes from --remote, else $CLIVANE_REMOTE, else
http://127.0.0.1:8799. $CLIVANE_TOKEN is sent as X-Clivane-Token.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

SPECS = json.loads(r"""[
 {
  "args": [],
  "cmd": "count-notes",
  "doc": "Return how many notes exist.",
  "func": "count_notes"
 },
 {
  "args": [
   {
    "default": null,
    "flag": "--title",
    "name": "title",
    "required": true,
    "type": "str"
   },
   {
    "default": "",
    "flag": "--body",
    "name": "body",
    "required": false,
    "type": "str"
   }
  ],
  "cmd": "create-note",
  "doc": "Create a note with a title and an optional body; returns the note.",
  "func": "create_note"
 },
 {
  "args": [
   {
    "default": null,
    "flag": "--note-id",
    "name": "note_id",
    "required": true,
    "type": "int"
   }
  ],
  "cmd": "delete-note",
  "doc": "Delete a note by id; returns True when a note was removed.",
  "func": "delete_note"
 },
 {
  "args": [],
  "cmd": "list-notes",
  "doc": "List all notes, in creation order.",
  "func": "list_notes"
 },
 {
  "args": [
   {
    "default": null,
    "flag": "--query",
    "name": "query",
    "required": true,
    "type": "str"
   }
  ],
  "cmd": "search-notes",
  "doc": "Find notes whose title or body contains the query (case-insensitive).",
  "func": "search_notes"
 }
]""")
DEFAULT_REMOTE = "http://127.0.0.1:8799"


def coerce(value, type_name):
    if value is None:
        return None
    if type_name == "int":
        return int(value)
    if type_name == "float":
        return float(value)
    if type_name == "bool":
        return str(value).lower() in ("1", "true", "yes", "on")
    if type_name in ("list", "dict"):
        return json.loads(value)
    return value


def build_parser():
    parser = argparse.ArgumentParser(prog="sample-app-server",
                                     description="Remote client for sample_app")
    parser.add_argument("--remote",
                        default=os.environ.get("CLIVANE_REMOTE", DEFAULT_REMOTE),
                        help="base URL of the clivane serve server")
    sub = parser.add_subparsers(dest="command")
    for spec in SPECS:
        p = sub.add_parser(spec["cmd"], help=spec["doc"])
        for arg in spec["args"]:
            p.add_argument(arg["flag"], required=arg["required"],
                           default=arg["default"], help=arg["type"])
        p.add_argument("--json", action="store_true", help="structured JSON output")
    return parser


def main(argv=None):
    parser = build_parser()
    ns = parser.parse_args(argv)
    if not ns.command:
        parser.print_help()
        return 2
    spec = next(s for s in SPECS if s["cmd"] == ns.command)
    args = {}
    for arg in spec["args"]:
        args[arg["name"]] = coerce(getattr(ns, arg["name"]), arg["type"])
    body = json.dumps({"command": spec["cmd"], "args": args}).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    token = os.environ.get("CLIVANE_TOKEN")
    if token:
        headers["X-Clivane-Token"] = token
    req = urllib.request.Request(ns.remote.rstrip("/") + "/run", data=body,
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"error": raw}
        print("error: %s" % payload.get("error", exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print("error: cannot reach server at %s (%s)" % (ns.remote, exc),
              file=sys.stderr)
        return 1
    if ns.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        result = payload.get("result")
        if isinstance(result, (dict, list)):
            print(json.dumps(result, indent=2, sort_keys=True, default=str))
        elif result is not None:
            print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
