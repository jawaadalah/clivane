"""starforge adapter - the clivane live-path bridge to Starforge.

Implements the live adapter contract:
    fetch_state() -> dict
    list_actions(state) -> list[str]
    send_action(action, **kw) -> dict
    meta() -> {"name", "game", "transport"}

Base URL resolution: $STARFORGE_URL, else $CLIVANE_REMOTE, else
http://127.0.0.1:8799 (the mock server's default port).
"""

import json
import os
import urllib.error
import urllib.request

BASE = (os.environ.get("STARFORGE_URL")
        or os.environ.get("CLIVANE_REMOTE")
        or "http://127.0.0.1:8799").rstrip("/")


def _call(method, path, payload=None):
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"error": raw}


def fetch_state():
    status, body = _call("GET", "/state")
    if status != 200:
        raise RuntimeError("starforge /state answered %d: %s" % (status, body))
    return body


def list_actions(state):
    status, body = _call("GET", "/actions")
    if status != 200:
        raise RuntimeError("starforge /actions answered %d: %s"
                           % (status, body))
    return body["actions"]


def send_action(action, **kw):
    status, body = _call("POST", "/act", {"action": action})
    if status >= 400:
        raise RuntimeError("starforge refused action %r (%d): %s"
                           % (action, status, body.get("error", body)))
    return body["result"]


def meta():
    return {"name": "starforge-live", "game": "Starforge",
            "transport": "REST (HTTP JSON)"}
