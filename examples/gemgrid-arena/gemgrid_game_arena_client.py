#!/usr/bin/env python3
"""gemgrid-game-arena client - remote CLI for a clivane fullstack game arena.

The local game verbs (new / observe / actions / act) plus finish and
leaderboard, but every call is HTTP to the arena server. Base URL:
--remote, else $CLIVANE_REMOTE, else http://127.0.0.1:8800.
$CLIVANE_TOKEN is sent as X-Clivane-Token.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_REMOTE = "http://127.0.0.1:8800"


def call(remote, method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    token = os.environ.get("CLIVANE_TOKEN")
    if token:
        headers["X-Clivane-Token"] = token
    req = urllib.request.Request(remote.rstrip("/") + path, data=data,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"error": raw}
        print("error: %s" % payload.get("error", exc), file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print("error: cannot reach arena at %s (%s)" % (remote, exc),
              file=sys.stderr)
        raise SystemExit(1)


def emit(command, result, as_json):
    if as_json:
        print(json.dumps({"command": command, "result": result},
                         indent=2, sort_keys=True, default=str))
    else:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))


def build_parser():
    p = argparse.ArgumentParser(prog="gemgrid-game-arena",
                                description="Remote client for the gemgrid_game arena")
    p.add_argument("--remote",
                   default=os.environ.get("CLIVANE_REMOTE", DEFAULT_REMOTE),
                   help="base URL of the arena server")
    sub = p.add_subparsers(dest="command")
    n = sub.add_parser("new", help="create a session on the server")
    n.add_argument("--seed", type=int, default=0)
    n.add_argument("--player", default="player")
    n.add_argument("--json", action="store_true")
    for verb, helptext in (("observe", "observe a session"),
                           ("actions", "legal actions of a session"),
                           ("finish", "verify by replay and record the score")):
        q = sub.add_parser(verb, help=helptext)
        q.add_argument("--session", required=True)
        q.add_argument("--json", action="store_true")
    t = sub.add_parser("act", help="apply one action to a session")
    t.add_argument("action")
    t.add_argument("--session", required=True)
    t.add_argument("--json", action="store_true")
    lb = sub.add_parser("leaderboard", help="verified scores, best first")
    lb.add_argument("--json", action="store_true")
    return p


def main(argv=None):
    parser = build_parser()
    ns = parser.parse_args(argv)
    if not ns.command:
        parser.print_help()
        return 2
    if ns.command == "new":
        result = call(ns.remote, "POST", "/sessions",
                      {"seed": ns.seed, "player": ns.player})
    elif ns.command == "observe":
        result = call(ns.remote, "GET", "/sessions/%s/observe" % ns.session)
    elif ns.command == "actions":
        result = call(ns.remote, "GET", "/sessions/%s/actions" % ns.session)
    elif ns.command == "act":
        result = call(ns.remote, "POST", "/sessions/%s/act" % ns.session,
                      {"action": ns.action})
    elif ns.command == "finish":
        result = call(ns.remote, "POST", "/sessions/%s/finish" % ns.session, {})
    elif ns.command == "leaderboard":
        result = call(ns.remote, "GET", "/leaderboard")
    else:
        return 2
    emit(ns.command, result, ns.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
