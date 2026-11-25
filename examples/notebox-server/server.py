#!/usr/bin/env python3
"""sample-app-server server - HTTP JSON front-end for `sample_app` (clivane serve path).

Endpoints:
  GET  /health    -> {"status": "ok", "name": "sample-app-server"}
  GET  /commands  -> {"name": ..., "commands": [specs]}
  POST /run       -> {"command": "<cmd>", "args": {...}}
                     returns the forge envelope {"command", "result"}

Binds 127.0.0.1 unless --host/CLIVANE_HOST says otherwise; a non-local
bind prints a warning. Set --token or CLIVANE_TOKEN to require the
X-Clivane-Token header on /run.
"""
import argparse
import importlib
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
MODULE = importlib.import_module("sample_app")
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
BY_CMD = {s["cmd"]: s for s in SPECS}
NAME = "sample-app-server"


def coerce(value, type_name):
    if value is None or not isinstance(value, str):
        if type_name == "bool" and isinstance(value, bool):
            return value
        return value
    if type_name == "int":
        return int(value)
    if type_name == "float":
        return float(value)
    if type_name == "bool":
        return value.lower() in ("1", "true", "yes", "on")
    if type_name in ("list", "dict"):
        return json.loads(value)
    return value


class Handler(BaseHTTPRequestHandler):
    token = None

    def log_message(self, *args):
        pass

    def _send(self, status, payload):
        body = json.dumps(payload, indent=2, sort_keys=True, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok", "name": NAME})
        elif self.path == "/commands":
            self._send(200, {"name": NAME, "commands": SPECS})
        elif self.path == "/":
            self._send(200, {"name": NAME,
                             "endpoints": ["/health", "/commands", "/run"]})
        else:
            self._send(404, {"error": "not found: %s" % self.path})

    def do_POST(self):
        if self.path != "/run":
            self._send(404, {"error": "not found: %s" % self.path})
            return
        if self.token and self.headers.get("X-Clivane-Token") != self.token:
            self._send(401, {"error": "missing or wrong X-Clivane-Token"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception as exc:
            self._send(400, {"error": "bad JSON body: %s" % exc})
            return
        cmd = payload.get("command")
        spec = BY_CMD.get(cmd)
        if spec is None:
            self._send(400, {"error": "unknown command: %r (known: %s)"
                             % (cmd, ", ".join(sorted(BY_CMD)))})
            return
        args = payload.get("args") or {}
        if not isinstance(args, dict):
            self._send(400, {"error": "args must be an object"})
            return
        known = {a["name"] for a in spec["args"]}
        extra = sorted(set(args) - known)
        if extra:
            self._send(400, {"error": "unknown args for %s: %s"
                             % (cmd, ", ".join(extra))})
            return
        kwargs = {}
        for arg in spec["args"]:
            if arg["name"] in args:
                try:
                    kwargs[arg["name"]] = coerce(args[arg["name"]], arg["type"])
                except Exception as exc:
                    self._send(400, {"error": "bad value for %s: %s"
                                     % (arg["name"], exc)})
                    return
            elif arg["required"]:
                self._send(400, {"error": "missing required arg: %s" % arg["name"]})
                return
            else:
                kwargs[arg["name"]] = arg["default"]
        try:
            result = getattr(MODULE, spec["func"])(**kwargs)
            json.dumps(result)
        except Exception as exc:
            self._send(500, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self._send(200, {"command": spec["cmd"], "result": result})


def main(argv=None):
    parser = argparse.ArgumentParser(prog=NAME + " server")
    parser.add_argument("--host", default=os.environ.get("CLIVANE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("CLIVANE_PORT", "8799")))
    parser.add_argument("--token", default=os.environ.get("CLIVANE_TOKEN"))
    ns = parser.parse_args(argv)
    if ns.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: binding %s exposes this server beyond localhost; "
              "set a --token before doing this." % ns.host, file=sys.stderr)
    Handler.token = ns.token
    server = ThreadingHTTPServer((ns.host, ns.port), Handler)
    print("%s server on http://%s:%d" % (NAME, ns.host, ns.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
