#!/usr/bin/env python3
"""starforge mock server - a fictional LIVE game server for demos/tests.

"Starforge" is a tiny space-mining game whose state and RNG live here,
on the server - exactly the situation the clivane live path exists
for. The RNG is seeded once at startup, so a fixed call sequence is
reproducible for demos and Trace-Seal, but that seed is the server's
private business: clients are NOT promised determinism, and the live
path never labels anything from this server "verified".

Endpoints (stdlib http.server only, binds 127.0.0.1):
  GET  /health  -> {"status": "ok", "name": "starforge-mock"}
  GET  /state   -> the current public state
  GET  /actions -> {"actions": [...]} legal right now
  POST /act     -> {"action": ...} performs one action

Port: $CLIVANE_PORT (default 8799).

Fixture note: the seed below (16) is chosen so the demo transcript's
first mine rolls land on 2 - the shadow model's modal value - which
makes the bundled transcript a *controlled* example with a high
shadow match_rate. Real live games are under no such control; that
is precisely what shadow-check measures and reports as-is.
"""

import json
import os
import random
from http.server import BaseHTTPRequestHandler, HTTPServer

RNG = random.Random(16)

STATE = {
    "energy": 20,
    "stars": 0,
    "ore": 0,
    "ship_hp": 100,
    "raids_done": 0,
    "done": False,
}

ACTIONS = ["mine", "trade", "repair", "finish-raid", "buy-stars"]


def list_actions(state):
    return [] if state["done"] else list(ACTIONS)


def apply_action(action):
    """Mutate STATE; return the action result dict."""
    if action == "mine":
        STATE["energy"] = max(0, STATE["energy"] - 3)
        gained = RNG.randint(1, 3)
        STATE["ore"] += gained
        result = {"ore_gained": gained, "reward": gained}
    elif action == "trade":
        if STATE["ore"] >= 2:
            STATE["ore"] -= 2
            STATE["stars"] += 5
            STATE["energy"] = max(0, STATE["energy"] - 1)
            result = {"stars_gained": 5, "reward": 5}
        else:
            result = {"error": "not enough ore", "reward": 0}
    elif action == "repair":
        STATE["ship_hp"] = min(100, STATE["ship_hp"] + 20)
        STATE["energy"] = max(0, STATE["energy"] - 2)
        result = {"repaired": 20, "reward": 0}
    elif action == "finish-raid":
        damage = RNG.randint(5, 15)
        loot = RNG.randint(3, 8)
        STATE["ship_hp"] -= damage
        STATE["stars"] += loot
        STATE["energy"] = max(0, STATE["energy"] - 4)
        STATE["raids_done"] += 1
        result = {"damage": damage, "stars_gained": loot, "reward": loot}
    elif action == "buy-stars":
        # A fictional real-money action. The game offers it; the
        # clivane live policy in this package blocks it fail-closed.
        STATE["stars"] += 10
        result = {"stars_bought": 10, "reward": 0}
    else:
        return None
    if STATE["energy"] <= 0:
        STATE["done"] = True
    return result


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok", "name": "starforge-mock"})
        elif self.path == "/state":
            self._send(200, dict(STATE))
        elif self.path == "/actions":
            self._send(200, {"actions": list_actions(STATE)})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/act":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._send(400, {"error": "invalid JSON body"})
            return
        action = payload.get("action")
        if action not in list_actions(STATE):
            self._send(400, {"error": "illegal action: %r" % (action,)})
            return
        result = apply_action(action)
        self._send(200, {"result": result, "state": dict(STATE)})

    def log_message(self, *args):
        pass


def main():
    port = int(os.environ.get("CLIVANE_PORT", "8799"))
    server = HTTPServer(("127.0.0.1", port), Handler)
    print("starforge mock on http://127.0.0.1:%d/" % port)
    server.serve_forever()


if __name__ == "__main__":
    main()
