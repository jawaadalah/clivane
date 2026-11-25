#!/usr/bin/env python3
"""gemgrid-game-arena - fullstack game arena server for `gemgrid_game` (clivane game path).

Three layers, one process (stdlib http.server only):
  * API    - session endpoints to play the game over HTTP
  * Web UI - GET / serves web/index.html (vanilla JS, no dependencies)
  * Proof  - POST /sessions/<id>/finish replays the episode from its
             seed; a score reaches the persistent leaderboard
             (scores.json) ONLY when the replay is identical and done.
             Clients can never submit a score directly.

Binds 127.0.0.1 unless --host/CLIVANE_HOST says otherwise; a non-local
bind prints a warning. Set --token or CLIVANE_TOKEN to require the
X-Clivane-Token header on POST endpoints.
"""
import argparse
import importlib
import itertools
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
MODULE = importlib.import_module("gemgrid_game")
NAME = "gemgrid-game-arena"
HERE = os.path.dirname(os.path.abspath(__file__))
SCORES_PATH = os.path.join(HERE, "scores.json")
WEB_INDEX = os.path.join(HERE, "web", "index.html")

LOCK = threading.Lock()
SESSIONS = {}
IDS = itertools.count(1)


def load_scores():
    if os.path.isfile(SCORES_PATH):
        try:
            with open(SCORES_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


SCORES = load_scores()


def save_scores():
    tmp = SCORES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(SCORES, fh, indent=2, sort_keys=True)
    os.replace(tmp, SCORES_PATH)


def replay(seed, actions):
    """Re-run an episode from scratch; (final_state, total, done, ok)."""
    state = MODULE.new_game(seed)
    total = 0
    for action in actions:
        if action not in MODULE.legal_actions(state):
            return state, total, False, False
        state, reward, done, _info = MODULE.step(state, action)
        total += reward
        if done:
            break
    return state, total, bool(done), True


def public_session(sess):
    return {"id": sess["id"], "seed": sess["seed"], "player": sess["player"],
            "steps": len(sess["actions"]), "done": sess["done"],
            "finished": sess["finished"],
            "observe": MODULE.observe(sess["state"]),
            "actions": MODULE.legal_actions(sess["state"])}


class Handler(BaseHTTPRequestHandler):
    token = None

    def log_message(self, *args):
        pass

    def _send(self, status, payload, content_type="application/json"):
        if isinstance(payload, str):
            body = payload.encode()
        elif isinstance(payload, bytes):
            body = payload
        else:
            body = json.dumps(payload, indent=2, sort_keys=True,
                              default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def _session(self, sid):
        sess = SESSIONS.get(sid)
        if sess is None:
            self._send(404, {"error": "unknown session: %s" % sid})
        return sess

    def do_GET(self):
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        if not parts:
            if os.path.isfile(WEB_INDEX):
                with open(WEB_INDEX, encoding="utf-8") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            else:
                self._send(500, {"error": "web/index.html missing"})
            return
        if parts == ["health"]:
            self._send(200, {"status": "ok", "name": NAME})
            return
        if parts == ["leaderboard"]:
            board = sorted(SCORES, key=lambda e: e["score"], reverse=True)
            self._send(200, {"scores": board})
            return
        if parts == ["sessions"]:
            with LOCK:
                listing = [public_session(s) for s in SESSIONS.values()]
            self._send(200, {"sessions": listing})
            return
        if len(parts) in (2, 3) and parts[0] == "sessions":
            sess = self._session(parts[1])
            if sess is None:
                return
            if len(parts) == 2:
                self._send(200, public_session(sess))
            elif parts[2] == "observe":
                self._send(200, {"session": sess["id"],
                                 "observe": MODULE.observe(sess["state"])})
            elif parts[2] == "actions":
                self._send(200, {"session": sess["id"],
                                 "actions": MODULE.legal_actions(sess["state"])})
            else:
                self._send(404, {"error": "not found: %s" % self.path})
            return
        self._send(404, {"error": "not found: %s" % self.path})

    def do_POST(self):
        if self.token and self.headers.get("X-Clivane-Token") != self.token:
            self._send(401, {"error": "missing or wrong X-Clivane-Token"})
            return
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        try:
            payload = self._body()
        except Exception as exc:
            self._send(400, {"error": "bad JSON body: %s" % exc})
            return
        if not isinstance(payload, dict):
            self._send(400, {"error": "JSON body must be an object"})
            return
        if parts == ["sessions"]:
            seed = payload.get("seed", 0)
            player = payload.get("player", "player")
            if not isinstance(seed, int) or isinstance(seed, bool):
                self._send(400, {"error": "seed must be an integer"})
                return
            if not isinstance(player, str) or not player:
                self._send(400, {"error": "player must be a non-empty string"})
                return
            with LOCK:
                sid = "s%d" % next(IDS)
                sess = {"id": sid, "seed": seed, "player": player,
                        "state": MODULE.new_game(seed), "actions": [],
                        "total_reward": 0, "done": False, "finished": False}
                SESSIONS[sid] = sess
                out = public_session(sess)
            self._send(201, out)
            return
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "act":
            sess = self._session(parts[1])
            if sess is None:
                return
            action = payload.get("action")
            with LOCK:
                if sess["finished"]:
                    self._send(400, {"error": "session already finished"})
                    return
                if sess["done"]:
                    self._send(400, {"error": "episode is done"})
                    return
                legal = MODULE.legal_actions(sess["state"])
                if action not in legal:
                    self._send(400, {"error": "illegal action: %r (legal: %s)"
                                     % (action, ", ".join(legal))})
                    return
                state, reward, done, info = MODULE.step(sess["state"], action)
                sess["state"] = state
                sess["actions"].append(action)
                sess["total_reward"] += reward
                sess["done"] = bool(done)
            self._send(200, {"session": sess["id"],
                             "observe": MODULE.observe(state),
                             "reward": reward, "done": bool(done),
                             "info": info})
            return
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "finish":
            sess = self._session(parts[1])
            if sess is None:
                return
            with LOCK:
                if sess["finished"]:
                    self._send(400, {"error": "session already finished and recorded"})
                    return
                if not sess["done"]:
                    self._send(400, {"error": "episode is not done yet"})
                    return
                final, total, done, ok = replay(sess["seed"],
                                                list(sess["actions"]))
                if not ok or not done or final != sess["state"]:
                    self._send(400, {"error": "replay does not reproduce this episode",
                                     "verified": False})
                    return
                obs = MODULE.observe(final)
                score = obs.get("score")
                if not isinstance(score, (int, float)) or isinstance(score, bool):
                    score = total
                entry = {"player": sess["player"], "seed": sess["seed"],
                         "actions": list(sess["actions"]), "score": score,
                         "steps": len(sess["actions"]), "verified": True}
                SCORES.append(entry)
                save_scores()
                sess["finished"] = True
            self._send(200, {"session": sess["id"], "verified": True,
                             "score": score, "entry": entry})
            return
        self._send(404, {"error": "not found: %s" % self.path})


def main(argv=None):
    parser = argparse.ArgumentParser(prog=NAME + " arena server")
    parser.add_argument("--host", default=os.environ.get("CLIVANE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("CLIVANE_PORT", "8800")))
    parser.add_argument("--token", default=os.environ.get("CLIVANE_TOKEN"))
    ns = parser.parse_args(argv)
    if ns.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: binding %s exposes this arena beyond localhost; "
              "set a --token before doing this." % ns.host, file=sys.stderr)
    Handler.token = ns.token
    server = ThreadingHTTPServer((ns.host, ns.port), Handler)
    print("%s arena on http://%s:%d" % (NAME, ns.host, ns.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
