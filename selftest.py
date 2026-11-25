#!/usr/bin/env python3
"""clivane self-test suite. Pure stdlib; everything runs in temp dirs.

Coverage: forge (AST -> commands, run, JSON envelope, REPL), mirror (help
parsing fixture + envelope), openapi (operationId -> commands, --help),
verify (seal PASS + tampered trace FAIL), registry tiers, determinism,
game (contract valid/invalid, deterministic episodes, replay tamper),
serve (live server: health/commands/run, remote client, token, probe),
game fullstack (arena: UI 200, parallel session isolation, illegal act
400, finish replay-verification, persistent verified leaderboard,
remote client, token), safe autopilot (strategies greedy/bfs/random,
budgets, target, dry-run; abuse: stall, illegal action, strategy and
step exceptions; arena autopilot + server-side step budget abort and
per-session rate limit), live (adapter contract valid/invalid,
session journal, policy-blocked acts refused, shadow-check match +
tamper detection, live autopilot guardrails: resource cap, blocked,
needs-human, kill-switch, schedule + hourly rate windows, dry-run
purity; read-only observer 200/405; conformance suite tiers), and
the committed examples registry (8 packages, all Gold).
"""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
CLIVANE = os.path.join(HERE, "clivane.py")

spec = importlib.util.spec_from_file_location("clivane", CLIVANE)
clivane = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clivane)

RESULTS = []


def check(name, fn):
    try:
        fn()
        RESULTS.append((name, True, ""))
        print("ok   %s" % name)
    except Exception as exc:
        RESULTS.append((name, False, str(exc)))
        print("FAIL %s: %s" % (name, exc))


def run(args, cwd=None, inp=None):
    return subprocess.run([sys.executable] + args, cwd=cwd,
                          capture_output=True, text=True, input=inp, timeout=60)


def digest_dir(path):
    h = hashlib.sha256()
    for root, dirs, names in os.walk(path):
        dirs.sort()
        for n in sorted(names):
            rel = os.path.relpath(os.path.join(root, n), path)
            h.update(rel.encode())
            with open(os.path.join(root, n), "rb") as fh:
                h.update(fh.read())
    return h.hexdigest()


MINI_MODULE = '''
def add(a: int, b: int = 10) -> int:
    """Add two numbers."""
    return a + b


def greet(name: str) -> str:
    """Greet someone."""
    return "hello " + name


def _hidden() -> None:
    pass
'''

FAKE_TOOL = '''#!/usr/bin/env python3
import sys

HELP = """Usage: faketool [OPTIONS] COMMAND [ARGS]...

Commands:
  start   Start the thing
  stop    Stop the thing
  status  Show status
"""

if "--help" in sys.argv or len(sys.argv) == 1:
    print(HELP)
elif sys.argv[1] == "status":
    print("status: running")
else:
    print("did", sys.argv[1])
'''


def t_forge_ast_and_run(tmp):
    mod = os.path.join(tmp, "mini.py")
    with open(mod, "w") as fh:
        fh.write(MINI_MODULE)
    out = os.path.join(tmp, "pkg")
    r = run([CLIVANE, "forge", mod, "--out", out])
    assert r.returncode == 0, r.stderr
    manifest = clivane.read_json(os.path.join(out, "manifest.json"))
    assert manifest["commands"] == ["add", "greet"], manifest["commands"]
    assert "_hidden" not in json.dumps(manifest)
    entry = os.path.join(out, manifest["entry"])
    r = run([entry, "add", "--a", "2", "--b", "3", "--json"], cwd=out)
    env = json.loads(r.stdout)
    assert env == {"command": "add", "result": 5}, env
    r = run([entry, "greet", "--name", "vane"], cwd=out)
    assert r.stdout.strip() == "hello vane", r.stdout
    # REPL: stateful session in one process
    r = run([entry], cwd=out, inp="add --a 1 --b 2\nexit\n")
    assert "3" in r.stdout, r.stdout


def t_mirror(tmp):
    subs = clivane.parse_help(FAKE_TOOL.split('"""')[1])
    assert subs == ["start", "stop", "status"], subs
    tool = os.path.join(tmp, "faketool")
    with open(tool, "w") as fh:
        fh.write(FAKE_TOOL)
    os.chmod(tool, 0o755)
    out = os.path.join(tmp, "mpkg")
    r = run([CLIVANE, "mirror", tool, "--out", out])
    assert r.returncode == 0, r.stderr
    manifest = clivane.read_json(os.path.join(out, "manifest.json"))
    assert manifest["commands"] == ["start", "stop", "status"], manifest
    entry = os.path.join(out, manifest["entry"])
    r = run([entry, "--json", "status"], cwd=out)
    env = json.loads(r.stdout)
    assert env["tool"] == "faketool" and env["exit_code"] == 0, env
    assert "running" in env["stdout"], env
    r = run([entry, "status"], cwd=out)
    assert "status: running" in r.stdout and r.stdout.strip().startswith("status"), r.stdout


def t_openapi(tmp):
    spec_doc = {
        "openapi": "3.0.3",
        "info": {"title": "Petline API", "version": "1"},
        "servers": [{"url": "http://localhost:9"}],
        "paths": {
            "/pets": {"get": {"operationId": "listPets", "summary": "List pets"}},
            "/pets/{petId}": {"get": {"operationId": "getPet", "summary": "One pet",
                                       "parameters": [{"name": "petId", "in": "path",
                                                        "required": True,
                                                        "schema": {"type": "integer"}}]}},
        },
    }
    spec_path = os.path.join(tmp, "spec.json")
    with open(spec_path, "w") as fh:
        json.dump(spec_doc, fh)
    out = os.path.join(tmp, "opkg")
    r = run([CLIVANE, "openapi", spec_path, "--out", out])
    assert r.returncode == 0, r.stderr
    manifest = clivane.read_json(os.path.join(out, "manifest.json"))
    assert manifest["commands"] == ["get-pet", "list-pets"], manifest["commands"]
    entry = os.path.join(out, manifest["entry"])
    r = run([entry, "--help"], cwd=out)
    assert r.returncode == 0 and "list-pets" in r.stdout, r.stdout
    r = run([entry, "get-pet", "--help"], cwd=out)
    assert "--pet-id" in r.stdout, r.stdout
    skill = open(os.path.join(out, "SKILL.md")).read()
    assert "GET /pets/{petId}" in skill, skill


def _forge_notebox(tmp):
    out = os.path.join(tmp, "nb")
    r = run([CLIVANE, "forge", os.path.join(HERE, "examples", "sample_app.py"),
             "--out", out])
    assert r.returncode == 0, r.stderr
    with open(os.path.join(out, "demo_calls.json"), "w") as fh:
        json.dump([["create-note", "--title", "Alpha", "--json"],
                   ["list-notes", "--json"]], fh)
    return out


def t_verify_pass_and_tamper(tmp):
    out = _forge_notebox(tmp)
    status, _ = clivane.verify_package(out)
    assert status == "SEALED", status
    status, detail = clivane.verify_package(out)
    assert status == "PASS", (status, detail)
    assert clivane.tier_of(out) == "Gold"
    # tamper: change one expected stdout in the golden trace
    trace_path = os.path.join(out, "traces.json")
    trace = clivane.read_json(trace_path)
    trace["calls"][0]["stdout"] = "tampered expectation"
    with open(trace_path, "w") as fh:
        json.dump(trace, fh)
    status, detail = clivane.verify_package(out)
    assert status == "FAIL", (status, detail)


def t_registry_tiers(tmp):
    parent = os.path.join(tmp, "reg")
    os.makedirs(parent)
    out = os.path.join(parent, "nb")
    r = run([CLIVANE, "forge", os.path.join(HERE, "examples", "sample_app.py"),
             "--out", out])
    assert r.returncode == 0, r.stderr
    entries = clivane.cmd_registry(parent)
    assert len(entries) == 1, entries
    assert entries[0]["tier"] == "Silver", entries  # smoke passes, no seal yet
    status, _ = clivane.verify_package(out)
    assert status == "SEALED", status
    entries = clivane.cmd_registry(parent)
    assert entries[0]["tier"] == "Gold", entries
    index = open(os.path.join(parent, "INDEX.md")).read()
    assert "Gold" in index and "sample-app-cli" in index, index


def t_determinism(tmp):
    mod = os.path.join(tmp, "mini.py")
    with open(mod, "w") as fh:
        fh.write(MINI_MODULE)
    digests = []
    for i in (1, 2):
        out = os.path.join(tmp, "pkg%d" % i)
        r = run([CLIVANE, "forge", mod, "--out", out])
        assert r.returncode == 0, r.stderr
        digests.append(digest_dir(out))
    assert digests[0] == digests[1], digests


BAD_GAME = '''
def new_game(seed):
    return {"x": 0}


def legal_actions(state):
    return ["go"]


def observe(state):
    return {"x": state["x"]}
'''

WRONG_GAME = '''
def new_game(seed):
    return {"x": 0}


def legal_actions(state):
    return ["go"]


def step(state, action):
    return {"x": 1}, "not-a-number", False, {}


def observe(state):
    return {"x": state["x"]}
'''


def _solve_gemgrid(seed):
    game = clivane.load_module_from_path(
        os.path.join(HERE, "examples", "gemgrid_game.py"))

    def key(s):
        return (tuple(s["player"]), frozenset(tuple(x) for x in s["gems"]))

    start = game.new_game(seed)
    seen = {key(start)}
    queue = deque([(start, [])])
    while queue:
        state, acts = queue.popleft()
        if state["won"]:
            return acts
        for action in game.legal_actions(state):
            nxt, _r, _d, _i = game.step(state, action)
            if key(nxt) not in seen:
                seen.add(key(nxt))
                queue.append((nxt, acts + [action]))
    raise AssertionError("no winning episode for seed %d" % seed)


def t_game_contract(tmp):
    good = os.path.join(HERE, "examples", "gemgrid_game.py")
    out = os.path.join(tmp, "g")
    r = run([CLIVANE, "game", good, "--out", out])
    assert r.returncode == 0, r.stderr
    manifest = clivane.read_json(os.path.join(out, "manifest.json"))
    assert manifest["path"] == "game", manifest
    assert manifest["commands"] == ["act", "actions", "autopilot", "new",
                                    "observe", "replay", "status"], manifest["commands"]
    for label, source in (("missing-step", BAD_GAME), ("wrong-reward", WRONG_GAME)):
        bad = os.path.join(tmp, label + ".py")
        with open(bad, "w") as fh:
            fh.write(source)
        r = run([CLIVANE, "game", bad, "--out", os.path.join(tmp, label)])
        assert r.returncode != 0, label
        assert "game contract violation" in (r.stderr + r.stdout), (label, r.stdout, r.stderr)


def t_game_episode(tmp):
    actions = _solve_gemgrid(7)
    assert actions, "empty solution"
    transcripts = []
    pkg = None
    for i in (1, 2):
        out = os.path.join(tmp, "g%d" % i)
        r = run([CLIVANE, "game", os.path.join(HERE, "examples", "gemgrid_game.py"),
                 "--out", out])
        assert r.returncode == 0, r.stderr
        manifest = clivane.read_json(os.path.join(out, "manifest.json"))
        entry = os.path.join(out, manifest["entry"])
        transcript = []
        calls = [["new", "--seed", "7", "--json"]]
        calls += [["act", a, "--json"] for a in actions]
        calls.append(["status", "--json"])
        for argv in calls:
            r = run([entry] + argv, cwd=out)
            assert r.returncode == 0, (argv, r.stderr)
            transcript.append(r.stdout)
        last = json.loads(transcript[-2])  # final act
        assert last["result"]["done"] is True and last["result"]["info"]["event"] == "win", last
        transcripts.append(transcript)
        pkg = (out, entry)
    # determinism: same seed -> byte-identical episode transcript
    assert transcripts[0] == transcripts[1], "episode transcripts diverged"
    out, entry = pkg
    # replay of the recorded transcript verifies
    r = run([entry, "replay", "--seed", "7", "--actions", ",".join(actions), "--json"],
            cwd=out)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert json.loads(r.stdout)["result"]["verified"] is True, r.stdout
    # replay with one action altered must FAIL against the recorded session
    altered = list(actions)
    altered[0] = "right" if actions[0] != "right" else "left"
    r = run([entry, "replay", "--seed", "7", "--actions", ",".join(altered), "--json"],
            cwd=out)
    assert r.returncode != 0, r.stdout
    assert json.loads(r.stdout)["result"]["verified"] is False, r.stdout


def _wait_health(url, proc, seconds=10):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            status, _ = clivane.http_json("GET", url + "/health", timeout=1)
            if status == 200:
                return
        except Exception:
            pass
        assert proc.poll() is None, "server exited during startup"
        time.sleep(0.05)
    raise AssertionError("server at %s never became healthy" % url)


def _start_server(pkg_dir, port, extra_env=None, server_file="server.py"):
    env = dict(os.environ)
    env["CLIVANE_PORT"] = str(port)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.Popen([sys.executable, server_file], cwd=pkg_dir, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _wait_health("http://127.0.0.1:%d" % port, proc)
    return proc


def t_serve_live(tmp):
    out = os.path.join(tmp, "srv")
    r = run([CLIVANE, "serve", os.path.join(HERE, "examples", "sample_app.py"),
             "--out", out])
    assert r.returncode == 0, r.stderr
    manifest = clivane.read_json(os.path.join(out, "manifest.json"))
    assert manifest["path"] == "serve" and manifest["server"] == "server.py", manifest
    entry = os.path.join(out, manifest["entry"])
    port = clivane.free_port()
    base = "http://127.0.0.1:%d" % port
    proc = _start_server(out, port)
    try:
        status, health = clivane.http_json("GET", base + "/health")
        assert status == 200 and health["status"] == "ok", health
        status, cmds = clivane.http_json("GET", base + "/commands")
        names = [c["cmd"] for c in cmds["commands"]]
        assert "create-note" in names and "list-notes" in names, names
        # /run via raw HTTP
        status, env_resp = clivane.http_json(
            "POST", base + "/run", {"command": "create-note",
                                    "args": {"title": "ViaHTTP", "body": "x"}})
        assert status == 200 and env_resp["result"]["title"] == "ViaHTTP", env_resp
        # unknown command is rejected, never evaluated
        status, err = clivane.http_json(
            "POST", base + "/run", {"command": "os-system", "args": {}})
        assert status == 400 and "unknown command" in err["error"], err
        # remote client end-to-end
        env = dict(os.environ)
        env["CLIVANE_REMOTE"] = base
        r = subprocess.run([sys.executable, entry, "create-note", "--title",
                            "ViaClient", "--json"], cwd=out, env=env,
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["result"]["title"] == "ViaClient", r.stdout
        r = subprocess.run([sys.executable, entry, "list-notes", "--json"],
                           cwd=out, env=env, capture_output=True, text=True, timeout=30)
        titles = [n["title"] for n in json.loads(r.stdout)["result"]]
        assert titles == ["ViaHTTP", "ViaClient"], titles
        # probe the live server, then use the probed client
        probe_out = os.path.join(tmp, "probe")
        r = run([CLIVANE, "probe", base, "--out", probe_out])
        assert r.returncode == 0, r.stderr
        p_manifest = clivane.read_json(os.path.join(probe_out, "manifest.json"))
        assert "create-note" in p_manifest["commands"], p_manifest
        skill = open(os.path.join(probe_out, "SKILL.md")).read()
        assert "/run" in skill and "create-note" in skill, skill
        p_entry = os.path.join(probe_out, p_manifest["entry"])
        r = run([p_entry, "list-notes", "--json"], cwd=probe_out)
        assert r.returncode == 0, r.stderr
        assert "ViaClient" in r.stdout, r.stdout
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    # token gate: /run without the header -> 401; with it -> 200
    port2 = clivane.free_port()
    base2 = "http://127.0.0.1:%d" % port2
    proc = _start_server(out, port2, {"CLIVANE_TOKEN": "s3cret"})
    try:
        status, err = clivane.http_json(
            "POST", base2 + "/run", {"command": "list-notes", "args": {}})
        assert status == 401, (status, err)
        status, ok = clivane.http_json(
            "POST", base2 + "/run", {"command": "list-notes", "args": {}},
            headers={"X-Clivane-Token": "s3cret"})
        # same package dir -> the notes written above are still the state
        assert status == 200, (status, ok)
        assert [n["title"] for n in ok["result"]] == ["ViaHTTP", "ViaClient"], ok
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def t_game_fullstack_generate(tmp):
    out = os.path.join(tmp, "arena")
    r = run([CLIVANE, "game", os.path.join(HERE, "examples", "gemgrid_game.py"),
             "--fullstack", "--out", out])
    assert r.returncode == 0, r.stderr
    manifest = clivane.read_json(os.path.join(out, "manifest.json"))
    assert manifest["path"] == "game-fullstack", manifest
    assert manifest["mode"] == "fullstack", manifest
    assert manifest["server"] == "game_server.py", manifest
    assert manifest["commands"] == ["act", "actions", "autopilot", "finish",
                                    "leaderboard", "new", "observe"], manifest["commands"]
    for rel in ("game_server.py", "web/index.html", "SKILL.md",
                "gemgrid_game.py", manifest["entry"], "test_smoke.py"):
        assert os.path.isfile(os.path.join(out, rel)), rel
    html = open(os.path.join(out, "web", "index.html")).read()
    assert "cdn" not in html.lower(), "web UI must be dependency-free"
    assert 'src="http' not in html and "href=\"http" not in html, html[:200]
    assert "leaderboard" in html.lower() and "0A1931" in html, "navy arena UI"
    skill = open(os.path.join(out, "SKILL.md")).read()
    assert "/sessions" in skill and "finish" in skill, skill
    # generation is deterministic, like every other path
    out2 = os.path.join(tmp, "arena2")
    r = run([CLIVANE, "game", os.path.join(HERE, "examples", "gemgrid_game.py"),
             "--fullstack", "--out", out2])
    assert r.returncode == 0, r.stderr
    assert digest_dir(out) == digest_dir(out2), "arena generation diverged"
    # the plain local game CLI is untouched by the new mode
    plain = os.path.join(tmp, "plain")
    r = run([CLIVANE, "game", os.path.join(HERE, "examples", "gemgrid_game.py"),
             "--out", plain])
    assert r.returncode == 0, r.stderr
    p_manifest = clivane.read_json(os.path.join(plain, "manifest.json"))
    assert p_manifest["path"] == "game" and "mode" not in p_manifest, p_manifest


def t_game_fullstack_live(tmp):
    out = os.path.join(tmp, "arena")
    r = run([CLIVANE, "game", os.path.join(HERE, "examples", "gemgrid_game.py"),
             "--fullstack", "--out", out])
    assert r.returncode == 0, r.stderr
    manifest = clivane.read_json(os.path.join(out, "manifest.json"))
    entry = os.path.join(out, manifest["entry"])
    actions = _solve_gemgrid(7)
    port = clivane.free_port()
    base = "http://127.0.0.1:%d" % port
    proc = _start_server(out, port, server_file="game_server.py")
    try:
        # web UI is served at /
        req = urllib.request.Request(base + "/")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200, resp.status
            assert resp.headers.get_content_type() == "text/html", resp.headers
            html = resp.read().decode("utf-8", "replace")
        assert "leaderboard" in html.lower(), "UI leaderboard panel missing"
        # two parallel sessions, isolated state
        status, a = clivane.http_json("POST", base + "/sessions",
                                      {"seed": 7, "player": "agent"})
        assert status == 201 and a["id"] == "s1", (status, a)
        status, h = clivane.http_json("POST", base + "/sessions",
                                      {"seed": 3, "player": "human-sim"})
        assert status == 201 and h["id"] == "s2", (status, h)
        # agent wins its episode over HTTP
        last = None
        for action in actions:
            status, last = clivane.http_json(
                "POST", base + "/sessions/s1/act", {"action": action})
            assert status == 200, (action, status, last)
        assert last["done"] is True, last
        # isolation: the other session never moved
        status, obs = clivane.http_json("GET", base + "/sessions/s2/observe")
        assert obs["observe"]["steps"] == 0, obs
        assert obs["observe"]["player"] == [0, 0], obs
        # illegal action -> 400
        status, err = clivane.http_json("POST", base + "/sessions/s2/act",
                                        {"action": "jump"})
        assert status == 400 and "illegal action" in err["error"], (status, err)
        # finish refuses an episode that is not done
        status, err = clivane.http_json("POST", base + "/sessions/s2/finish", {})
        assert status == 400 and "not done" in err["error"], (status, err)
        # there is no manual score path: extra fields change nothing
        status, fin = clivane.http_json("POST", base + "/sessions/s1/finish",
                                        {"score": 99999})
        assert status == 200 and fin["verified"] is True, (status, fin)
        assert fin["score"] == 30, fin  # replayed truth, not the claim
        # a finished session cannot be recorded twice
        status, err = clivane.http_json("POST", base + "/sessions/s1/finish", {})
        assert status == 400, (status, err)
        # leaderboard holds exactly the one verified score
        status, board = clivane.http_json("GET", base + "/leaderboard")
        scores = board["scores"]
        assert len(scores) == 1, scores
        entry0 = scores[0]
        assert entry0["player"] == "agent" and entry0["seed"] == 7, entry0
        assert entry0["score"] == 30 and entry0["verified"] is True, entry0
        assert entry0["actions"] == actions, entry0
        # remote client CLI plays against the same server
        env = dict(os.environ)
        env["CLIVANE_REMOTE"] = base
        r = subprocess.run([sys.executable, entry, "leaderboard", "--json"],
                           cwd=out, env=env, capture_output=True, text=True,
                           timeout=30)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["result"]["scores"][0]["player"] == "agent", r.stdout
        r = subprocess.run([sys.executable, entry, "new", "--seed", "7",
                            "--player", "bot", "--json"],
                           cwd=out, env=env, capture_output=True, text=True,
                           timeout=30)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["result"]["id"] == "s3", r.stdout
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    # persistence: scores.json survives a server restart
    scores_path = os.path.join(out, "scores.json")
    saved = clivane.read_json(scores_path)
    assert len(saved) == 1 and saved[0]["verified"] is True, saved
    port2 = clivane.free_port()
    base2 = "http://127.0.0.1:%d" % port2
    proc = _start_server(out, port2, server_file="game_server.py")
    try:
        status, board = clivane.http_json("GET", base2 + "/leaderboard")
        assert len(board["scores"]) == 1, board
        assert board["scores"][0]["score"] == 30, board
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    # token gate: POSTs need the header when a token is set
    port3 = clivane.free_port()
    base3 = "http://127.0.0.1:%d" % port3
    proc = _start_server(out, port3, {"CLIVANE_TOKEN": "s3cret"},
                         server_file="game_server.py")
    try:
        status, err = clivane.http_json("POST", base3 + "/sessions",
                                        {"seed": 1, "player": "x"})
        assert status == 401, (status, err)
        status, ok = clivane.http_json("POST", base3 + "/sessions",
                                       {"seed": 1, "player": "x"},
                                       headers={"X-Clivane-Token": "s3cret"})
        assert status == 201, (status, ok)
    finally:
        proc.terminate()
        proc.wait(timeout=5)


WAIT_GAME = '''
def new_game(seed):
    return {"phase": "waiting", "ticks": 0, "done": False, "score": 0}


def legal_actions(state):
    if state["done"]:
        return []
    return ["wait", "finish-it", "explode"]


def step(state, action):
    state = dict(state)
    if action == "wait":
        state["ticks"] += 1
        return state, 0, False, {"event": "wait"}
    if action == "explode":
        raise RuntimeError("kaboom")
    if action == "finish-it":
        state["done"] = True
        state["phase"] = "finished"
        state["score"] = 5
        return state, 5, True, {"event": "finished"}
    return state, -1, False, {"event": "blocked"}


def observe(state):
    # ticks is deliberately NOT observed: "wait" leaves observe() unchanged
    return {"phase": state["phase"], "done": state["done"],
            "score": state["score"]}
'''

STRAT_WAIT = 'def choose(observe, legal_actions):\n    return "wait"\n'
STRAT_ILLEGAL = 'def choose(observe, legal_actions):\n    return "teleport"\n'
STRAT_BOOM = ('def choose(observe, legal_actions):\n'
              '    raise RuntimeError("strategy boom")\n')
STRAT_EXPLODE = 'def choose(observe, legal_actions):\n    return "explode"\n'


def _write(tmp, name, text):
    path = os.path.join(tmp, name)
    with open(path, "w") as fh:
        fh.write(text)
    return path


def _autopilot(entry, cwd, args, env=None):
    full = dict(os.environ)
    if env:
        full.update(env)
    return subprocess.run([sys.executable, entry, "autopilot"] + args,
                          cwd=cwd, env=full, capture_output=True, text=True,
                          timeout=60)


def t_autopilot_local(tmp):
    out = os.path.join(tmp, "g")
    r = run([CLIVANE, "game", os.path.join(HERE, "examples", "gemgrid_game.py"),
             "--out", out])
    assert r.returncode == 0, r.stderr
    manifest = clivane.read_json(os.path.join(out, "manifest.json"))
    entry = os.path.join(out, manifest["entry"])
    solution = _solve_gemgrid(7)
    # greedy and bfs both solve seed 7 optimally and settle by replay
    for strat in ("greedy", "bfs"):
        r = _autopilot(entry, out, ["--strategy", strat, "--seed", "7", "--json"])
        assert r.returncode == 0, (strat, r.stdout, r.stderr)
        res = json.loads(r.stdout)
        assert res["done"] is True and res["score"] == 30, res
        assert res["steps"] == len(solution), res
        assert res["stop_reason"] == "done" and res["verified"] is True, res
        # the transcript was persisted and replays clean
        sess = clivane.read_json(os.path.join(out, "session.json"))
        assert sess["actions"] == solution, sess
        r = run([entry, "replay", "--seed", "7",
                 "--actions", ",".join(solution), "--json"], cwd=out)
        assert r.returncode == 0, r.stdout
    # random baseline: seeded -> two runs byte-identical, sane stop
    runs = []
    for _ in (1, 2):
        r = _autopilot(entry, out, ["--strategy", "random", "--seed", "7"])
        assert r.returncode == 0, r.stderr
        runs.append(r.stdout)
    assert runs[0] == runs[1], "seeded random autopilot diverged"
    res = json.loads(runs[0])
    assert res["stop_reason"] == "done" and res["steps"] <= 40, res
    assert res["verified"] is True, res  # replay settles even a loss
    # target score stops early, nothing settled
    r = _autopilot(entry, out, ["--strategy", "greedy", "--seed", "7",
                                "--target-score", "10"])
    res = json.loads(r.stdout)
    assert res["stop_reason"] == "target_reached", res
    assert res["done"] is False and res["score"] >= 10, res
    assert res["verified"] is False and res["steps"] < len(solution), res
    # hard step budget: stops exactly at the budget, transcript saved
    r = _autopilot(entry, out, ["--strategy", "greedy", "--seed", "7",
                                "--max-steps", "3"])
    res = json.loads(r.stdout)
    assert res["stop_reason"] == "step_budget" and res["steps"] == 3, res
    sess = clivane.read_json(os.path.join(out, "session.json"))
    assert len(sess["actions"]) == 3 and sess["done"] is False, sess
    # hard time budget (negative budget trips on the first check)
    r = _autopilot(entry, out, ["--strategy", "greedy", "--seed", "7",
                                "--max-seconds", "-1"])
    res = json.loads(r.stdout)
    assert res["stop_reason"] == "time_budget" and res["steps"] == 0, res
    # dry-run: full projection, zero persistence
    os.remove(os.path.join(out, "session.json"))
    r = _autopilot(entry, out, ["--strategy", "greedy", "--seed", "7",
                                "--dry-run"])
    res = json.loads(r.stdout)
    assert res["done"] is True and res["score"] == 30, res
    assert res["dry_run"] is True and res["verified"] is False, res
    assert not os.path.exists(os.path.join(out, "session.json"))


def t_autopilot_abuse(tmp):
    mod = _write(tmp, "wait_game.py", WAIT_GAME)
    out = os.path.join(tmp, "w")
    r = run([CLIVANE, "game", mod, "--out", out])
    assert r.returncode == 0, r.stderr
    manifest = clivane.read_json(os.path.join(out, "manifest.json"))
    entry = os.path.join(out, manifest["entry"])
    # stall: "wait" never changes observe() -> stopped after 5 repeats
    sf = _write(tmp, "strat_wait.py", STRAT_WAIT)
    r = _autopilot(entry, out, ["--strategy-file", sf, "--seed", "1"])
    res = json.loads(r.stdout)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert res["stop_reason"] == "stall" and res["steps"] == 5, res
    # illegal action: never forwarded to the game, aborts at step 0
    sf = _write(tmp, "strat_illegal.py", STRAT_ILLEGAL)
    r = _autopilot(entry, out, ["--strategy-file", sf, "--seed", "1"])
    res = json.loads(r.stdout)
    assert r.returncode == 1, (r.returncode, r.stdout)
    assert res["stop_reason"] == "illegal_action" and res["steps"] == 0, res
    sess = clivane.read_json(os.path.join(out, "session.json"))
    assert sess["actions"] == [] and sess["state"]["ticks"] == 0, sess
    # strategy exception: safe abort, partial transcript persisted
    sf = _write(tmp, "strat_boom.py", STRAT_BOOM)
    r = _autopilot(entry, out, ["--strategy-file", sf, "--seed", "1"])
    res = json.loads(r.stdout)
    assert r.returncode == 1, (r.returncode, r.stdout)
    assert res["stop_reason"] == "error" and "strategy boom" in res["error"], res
    sess = clivane.read_json(os.path.join(out, "session.json"))
    assert sess["actions"] == [], sess
    # step() exception: contained the same way, state not corrupted
    sf = _write(tmp, "strat_explode.py", STRAT_EXPLODE)
    r = _autopilot(entry, out, ["--strategy-file", sf, "--seed", "1"])
    res = json.loads(r.stdout)
    assert r.returncode == 1, (r.returncode, r.stdout)
    assert res["stop_reason"] == "error" and "kaboom" in res["error"], res
    sess = clivane.read_json(os.path.join(out, "session.json"))
    assert sess["actions"] == [] and sess["done"] is False, sess
    # the package is still fully usable after all that abuse
    r = run([entry, "new", "--seed", "1", "--json"], cwd=out)
    assert r.returncode == 0, r.stderr


def t_autopilot_arena(tmp):
    out = os.path.join(tmp, "arena")
    r = run([CLIVANE, "game", os.path.join(HERE, "examples", "gemgrid_game.py"),
             "--fullstack", "--out", out])
    assert r.returncode == 0, r.stderr
    manifest = clivane.read_json(os.path.join(out, "manifest.json"))
    entry = os.path.join(out, manifest["entry"])
    port = clivane.free_port()
    base = "http://127.0.0.1:%d" % port
    env = {"CLIVANE_REMOTE": base}
    proc = _start_server(out, port, server_file="game_server.py")
    try:
        # greedy + bfs bots finish seed 7 and are settled by server replay
        for strat in ("greedy", "bfs"):
            r = _autopilot(entry, out, ["--strategy", strat, "--seed", "7"],
                           env=env)
            assert r.returncode == 0, (strat, r.stdout, r.stderr)
            res = json.loads(r.stdout)
            assert res["done"] is True and res["score"] == 30, res
            assert res["stop_reason"] == "done" and res["verified"] is True, res
        status, board = clivane.http_json("GET", base + "/leaderboard")
        players = {s["player"]: s for s in board["scores"]}
        assert set(players) == {"autopilot-greedy", "autopilot-bfs"}, players
        for s in players.values():
            assert s["score"] == 30 and s["verified"] is True, s
        # dry-run: no session created, leaderboard untouched
        status, before = clivane.http_json("GET", base + "/sessions")
        r = _autopilot(entry, out, ["--strategy", "greedy", "--seed", "7",
                                    "--dry-run"], env=env)
        res = json.loads(r.stdout)
        assert res["done"] is True and res["dry_run"] is True, res
        assert res["verified"] is False, res
        status, after = clivane.http_json("GET", base + "/sessions")
        assert len(after["sessions"]) == len(before["sessions"]), after
        status, board2 = clivane.http_json("GET", base + "/leaderboard")
        assert len(board2["scores"]) == 2, board2
        # server-side step budget: a rogue client is aborted at the cap
        status, rogue = clivane.http_json(
            "POST", base + "/sessions",
            {"seed": 7, "player": "rogue", "max_steps": 2})
        assert status == 201 and rogue["max_steps"] == 2, (status, rogue)
        sid = rogue["id"]
        for _ in (1, 2):
            status, _resp = clivane.http_json(
                "POST", base + "/sessions/%s/act" % sid, {"action": "down"})
            assert status == 200, status
        status, err = clivane.http_json(
            "POST", base + "/sessions/%s/act" % sid, {"action": "down"})
        assert status == 403 and "budget" in err["error"], (status, err)
        status, view = clivane.http_json("GET", base + "/sessions/" + sid)
        assert view["aborted"] is True, view
        status, err = clivane.http_json(
            "POST", base + "/sessions/%s/act" % sid, {"action": "down"})
        assert status == 403, (status, err)  # stays aborted
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    # rate limit: 20 acts/second per session, the 21st burst act is 429
    mod = _write(tmp, "wait_game.py", WAIT_GAME)
    wout = os.path.join(tmp, "warena")
    r = run([CLIVANE, "game", mod, "--fullstack", "--out", wout])
    assert r.returncode == 0, r.stderr
    port = clivane.free_port()
    base = "http://127.0.0.1:%d" % port
    proc = _start_server(wout, port, server_file="game_server.py")
    try:
        status, sess = clivane.http_json("POST", base + "/sessions",
                                         {"seed": 1, "player": "bursty"})
        assert status == 201, (status, sess)
        statuses = []
        for _ in range(25):
            status, _resp = clivane.http_json(
                "POST", base + "/sessions/%s/act" % sess["id"],
                {"action": "wait"})
            statuses.append(status)
        assert statuses[:20] == [200] * 20, statuses
        assert 429 in statuses[20:], statuses
    finally:
        proc.terminate()
        proc.wait(timeout=5)


SF_DIR = os.path.join(HERE, "examples", "starforge-live")
SF_ADAPTER = os.path.join(SF_DIR, "starforge_adapter.py")
SF_SHADOW = os.path.join(SF_DIR, "starforge_shadow.py")
SF_POLICY = os.path.join(SF_DIR, "policy.json")
SF_MOCK = os.path.join(SF_DIR, "mock_server.py")
SF_TRANSCRIPT = os.path.join(SF_DIR, "transcript.jsonl")

LIVE_BAD_MISSING = '''
def fetch_state():
    return {"x": 0}


def list_actions(state):
    return ["go"]
'''

LIVE_BAD_SIGNATURE = '''
def fetch_state(unexpected):
    return {"x": 0}


def list_actions(state):
    return ["go"]


def send_action(action):
    return {}
'''


def _live_pkg(tmp, policy=SF_POLICY, name="lp"):
    out = os.path.join(tmp, name)
    r = run([CLIVANE, "live", SF_ADAPTER, "--out", out,
             "--shadow", SF_SHADOW, "--policy", policy])
    assert r.returncode == 0, r.stderr
    manifest = clivane.read_json(os.path.join(out, "manifest.json"))
    return out, os.path.join(out, manifest["entry"])


def _live_run(entry, cwd, args, base):
    env = dict(os.environ)
    env["CLIVANE_REMOTE"] = base
    return subprocess.run([sys.executable, entry] + args, cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=60)


def t_live_contract(tmp):
    out, _entry = _live_pkg(tmp)
    manifest = clivane.read_json(os.path.join(out, "manifest.json"))
    assert manifest["path"] == "live" and manifest["kind"] == "live", manifest
    assert manifest["label"] == "live-recorded", manifest
    assert manifest["commands"] == ["actions", "act", "live-autopilot",
                                    "live-observe", "observe",
                                    "shadow-check", "status"], manifest
    for rel in (manifest["entry"], "SKILL.md", "test_smoke.py",
                    "starforge_adapter.py", "starforge_shadow.py",
                    "policy.json"):
        assert os.path.isfile(os.path.join(out, rel)), rel
    skill = open(os.path.join(out, "SKILL.md")).read()
    assert "live-recorded" in skill and "never" in skill, skill
    # static validation never imports the adapter: bad ones are rejected
    for label, source in (("missing", LIVE_BAD_MISSING),
                          ("signature", LIVE_BAD_SIGNATURE)):
        bad = _write(tmp, "bad_%s.py" % label, source)
        r = run([CLIVANE, "live", bad, "--out", os.path.join(tmp, label)])
        assert r.returncode != 0, label
        assert "live contract violation" in (r.stderr + r.stdout), \
            (label, r.stdout, r.stderr)


def t_live_journal_policy(tmp):
    out, entry = _live_pkg(tmp)
    port = clivane.free_port()
    base = "http://127.0.0.1:%d" % port
    proc = _start_server(SF_DIR, port, server_file="mock_server.py")
    try:
        r = _live_run(entry, out, ["observe", "--json"], base)
        assert json.loads(r.stdout)["result"]["energy"] == 20, r.stdout
        r = _live_run(entry, out, ["act", "mine", "--json"], base)
        assert r.returncode == 0, r.stderr
        res = json.loads(r.stdout)["result"]
        assert res["label"] == "live-recorded" and res["observe"]["ore"] == 2, res
        # policy-blocked action: refused locally, server never sees it
        r = _live_run(entry, out, ["act", "buy-stars", "--json"], base)
        assert r.returncode == 1 and "policy" in r.stderr, (r.returncode, r.stderr)
        r = _live_run(entry, out, ["observe", "--json"], base)
        assert json.loads(r.stdout)["result"]["stars"] == 0, r.stdout
        # status: honest label, never verified, journal counted
        r = _live_run(entry, out, ["status", "--json"], base)
        st = json.loads(r.stdout)["result"]
        assert st["session"] == "live-1" and st["acts"] == 1, st
        assert st["label"] == "live-recorded" and st["verified"] is False, st
        # the journal itself: session_start + one act, fully shaped
        path = os.path.join(out, "sessions", "live-1.jsonl")
        lines = [json.loads(l) for l in open(path) if l.strip()]
        assert [l["type"] for l in lines] == ["session_start", "act"], lines
        act = lines[1]
        for key in ("ts", "state_before", "action", "result", "state_after"):
            assert key in act, (key, act)
        assert act["action"] == "mine" and act["state_after"]["ore"] == 2, act
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def t_live_shadow_check(tmp):
    # generator-side measurement on the committed controlled transcript
    report = clivane.shadow_check(SF_SHADOW, SF_TRANSCRIPT)
    assert report["match_rate"] >= 0.9, report
    assert report["verified"] is False and report["label"] == "live-recorded", report
    # tampering with one recorded value is detected as divergence
    lines = [json.loads(l) for l in open(SF_TRANSCRIPT) if l.strip()]
    for line in lines:
        if line.get("type") == "act":
            line["state_after"]["energy"] -= 1
            break
    tampered = os.path.join(tmp, "tampered.jsonl")
    with open(tampered, "w") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")
    bad = clivane.shadow_check(SF_SHADOW, tampered)
    assert bad["fields_mismatched"] > 0 and bad["match_rate"] < report["match_rate"], bad
    # the generated CLI reports the same measurement
    out, entry = _live_pkg(tmp)
    r = _live_run(entry, out, ["shadow-check", SF_TRANSCRIPT, "--json"],
                  "http://127.0.0.1:9")
    assert r.returncode == 0, r.stderr
    cli_report = json.loads(r.stdout)["result"]
    assert cli_report["match_rate"] == report["match_rate"], cli_report


def t_live_autopilot_guards(tmp):
    out, entry = _live_pkg(tmp)
    port = clivane.free_port()
    base = "http://127.0.0.1:%d" % port
    proc = _start_server(SF_DIR, port, server_file="mock_server.py")
    try:
        def autopilot(args):
            r = _live_run(entry, out, ["live-autopilot"] + args, base)
            return json.loads(r.stdout)

        mine = _write(tmp, "mine.py",
                      'def choose(observe, legal_actions):\n    return "mine"\n')
        buy = _write(tmp, "buy.py",
                     'def choose(observe, legal_actions):\n    return "buy-stars"\n')
        raid = _write(tmp, "raid.py",
                      'def choose(observe, legal_actions):\n    return "finish-raid"\n')
        # resource cap: 4 mines spend exactly the 12-energy cap; the
        # 5th would exceed it -> budget_resource, ship not drained
        res = autopilot(["--strategy-file", mine, "--seed", "0"])
        assert res["stop_reason"] == "budget_resource" and res["steps"] == 4, res
        assert res["verified"] is False and res["label"] == "live-recorded", res
        # blocked action: never sent, stops at step 0
        res = autopilot(["--strategy-file", buy, "--seed", "0"])
        assert res["stop_reason"] == "blocked" and res["steps"] == 0, res
        # approval action: human-only, autopilot defers
        res = autopilot(["--strategy-file", raid, "--seed", "0"])
        assert res["stop_reason"] == "needs_human" and res["steps"] == 0, res
        # kill-switch: a STOP file in sessions/ halts before any action
        stop_path = os.path.join(out, "sessions", "STOP")
        with open(stop_path, "w") as fh:
            fh.write("stop\n")
        res = autopilot(["--strategy-file", mine, "--seed", "0"])
        assert res["stop_reason"] == "kill_switch" and res["steps"] == 0, res
        os.remove(stop_path)
        # dry-run: shadow only - live state and journal untouched
        r = _live_run(entry, out, ["observe", "--json"], base)
        energy_before = json.loads(r.stdout)["result"]["energy"]
        journal_files = sorted(os.listdir(os.path.join(out, "sessions")))
        res = autopilot(["--strategy-file", mine, "--seed", "0", "--dry-run"])
        assert res["dry_run"] is True and res["steps"] >= 1, res
        r = _live_run(entry, out, ["observe", "--json"], base)
        assert json.loads(r.stdout)["result"]["energy"] == energy_before, r.stdout
        assert sorted(os.listdir(os.path.join(out, "sessions"))) == journal_files
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    # schedule window: a one-hour window that is not now -> rate_window
    import time as _time
    hour = _time.localtime().tm_hour
    window = [(hour + 2) % 24, (hour + 2) % 24]
    pol = clivane.read_json(SF_POLICY)
    pol["allowed_hours"] = window
    pol_path = _write(tmp, "window_policy.json", json.dumps(pol))
    out2, entry2 = _live_pkg(tmp, policy=pol_path, name="lp2")
    port = clivane.free_port()
    base = "http://127.0.0.1:%d" % port
    proc = _start_server(SF_DIR, port, server_file="mock_server.py")
    try:
        mine = _write(tmp, "mine2.py",
                      'def choose(observe, legal_actions):\n    return "mine"\n')
        r = _live_run(entry2, out2,
                      ["live-autopilot", "--strategy-file", mine], base)
        res = json.loads(r.stdout)
        assert res["stop_reason"] == "rate_window" and res["steps"] == 0, res
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    # hourly budget, counted from the journal: 2 manual acts fill a cap of 2
    pol = clivane.read_json(SF_POLICY)
    pol["budgets"]["max_actions_per_hour"] = 2
    pol_path = _write(tmp, "hourly_policy.json", json.dumps(pol))
    out3, entry3 = _live_pkg(tmp, policy=pol_path, name="lp3")
    port = clivane.free_port()
    base = "http://127.0.0.1:%d" % port
    proc = _start_server(SF_DIR, port, server_file="mock_server.py")
    try:
        for _ in (1, 2):
            r = _live_run(entry3, out3, ["act", "mine", "--json"], base)
            assert r.returncode == 0, r.stderr
        mine = _write(tmp, "mine3.py",
                      'def choose(observe, legal_actions):\n    return "mine"\n')
        r = _live_run(entry3, out3,
                      ["live-autopilot", "--strategy-file", mine], base)
        res = json.loads(r.stdout)
        assert res["stop_reason"] == "rate_window" and res["steps"] == 0, res
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def t_live_observer(tmp):
    out, entry = _live_pkg(tmp)
    port = clivane.free_port()
    base = "http://127.0.0.1:%d" % port
    proc = _start_server(SF_DIR, port, server_file="mock_server.py")
    try:
        r = _live_run(entry, out, ["act", "mine", "--json"], base)
        assert r.returncode == 0, r.stderr
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    oport = clivane.free_port()
    env = dict(os.environ)
    oproc = subprocess.Popen([sys.executable, entry, "live-observe",
                              "--port", str(oport)], cwd=out, env=env,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 10
        body = None
        while time.time() < deadline:
            try:
                status, body = clivane.http_json(
                    "GET", "http://127.0.0.1:%d/" % oport, timeout=1)
                if status == 200:
                    break
            except Exception:
                pass
            assert oproc.poll() is None, "observer exited during startup"
            time.sleep(0.05)
        assert status == 200, status
        assert "live-recorded" in body and "live-1" in body, body[:400]
        assert "mine" in body, body[:400]
        # read-only means read-only: writes are refused 405
        status, _ = clivane.http_json(
            "POST", "http://127.0.0.1:%d/" % oport, {"action": "mine"})
        assert status == 405, status
        status, _ = clivane.http_json(
            "PUT", "http://127.0.0.1:%d/" % oport, {"action": "mine"})
        assert status == 405, status
    finally:
        oproc.terminate()
        oproc.wait(timeout=5)


def t_live_conformance(tmp):
    pkg, _entry = _live_pkg(tmp)
    r = run([CLIVANE, "live-check", SF_ADAPTER, "--mock", SF_MOCK,
             "--package", pkg])
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "tier Gold" in r.stdout, r.stdout
    conf = clivane.read_json(os.path.join(pkg, "conformance.json"))
    assert all(conf["checks"].values()), conf
    assert conf["tier"] == "Gold" and conf["verified"] is False, conf
    assert clivane.tier_of(pkg) == "Gold"
    # the tier ladder itself: policy/meta missing drops Gold to Silver
    checks = dict(conf["checks"])
    checks["policy"] = False
    assert clivane.live_tier_from_checks(checks) == "Silver"
    checks["journal"] = False
    assert clivane.live_tier_from_checks(checks) == "Bronze"


def t_examples_registry(tmp):
    registry = clivane.read_json(os.path.join(HERE, "examples", "registry.json"))
    pkgs = registry["packages"]
    assert len(pkgs) == 8, [p["name"] for p in pkgs]
    assert all(p["tier"] == "Gold" for p in pkgs), pkgs
    paths = sorted(p["path"] for p in pkgs)
    assert paths == ["forge", "game", "game", "game-fullstack", "live",
                     "mirror", "openapi", "serve"], paths
    kinds = {p["name"]: p["kind"] for p in pkgs}
    assert kinds["starforge-live"] == "live", kinds
    assert kinds["starforge-shadow-cli"] == "deterministic", kinds
    # live recompute (never trust the file): the newer packages replay PASS
    for child in ("gemgrid-cli", "notebox-server", "gemgrid-arena"):
        pkg_dir = os.path.join(HERE, "examples", child)
        status, detail = clivane.verify_package(pkg_dir)
        assert status == "PASS", (child, status, detail)
        assert clivane.tier_of(pkg_dir) == "Gold", child
    # the dual-mode pair: shadow verified by replay, live graded by
    # conformance evidence - both Gold, by different proof systems
    shadow_dir = os.path.join(SF_DIR, "shadow")
    status, detail = clivane.verify_package(shadow_dir)
    assert status == "PASS", (status, detail)
    assert clivane.tier_of(shadow_dir) == "Gold"
    assert clivane.tier_of(SF_DIR) == "Gold"
    status, detail = clivane.verify_package(SF_DIR)
    assert status == "PASS", (status, detail)


def main():
    tests = [
        ("forge: AST->commands, run, --json, REPL", t_forge_ast_and_run),
        ("mirror: help parsing + JSON envelope", t_mirror),
        ("openapi: operationId->commands, --help", t_openapi),
        ("verify: seal PASS + tampered trace FAIL", t_verify_pass_and_tamper),
        ("registry: Silver -> Gold tiers", t_registry_tiers),
        ("determinism: forge digest stable x2", t_determinism),
        ("game: contract valid + invalid rejected", t_game_contract),
        ("game: deterministic episode + replay tamper FAIL", t_game_episode),
        ("serve: live server health/run/client/probe/token", t_serve_live),
        ("game fullstack: arena package shape + determinism", t_game_fullstack_generate),
        ("game fullstack: live arena UI/sessions/verify/leaderboard", t_game_fullstack_live),
        ("autopilot: local strategies, budgets, target, dry-run", t_autopilot_local),
        ("autopilot: abuse - stall/illegal/strategy+step exceptions", t_autopilot_abuse),
        ("autopilot: arena bots + server budget abort + rate limit", t_autopilot_arena),
        ("live: adapter contract valid + invalid rejected", t_live_contract),
        ("live: journal written + policy-blocked act refused", t_live_journal_policy),
        ("live: shadow-check match + tamper detected", t_live_shadow_check),
        ("live autopilot: budget_resource/blocked/needs_human/kill-switch/rate windows/dry-run", t_live_autopilot_guards),
        ("live observer: dashboard 200 + writes refused 405", t_live_observer),
        ("live-check: conformance Gold + tier ladder", t_live_conformance),
        ("examples: registry holds 8 Gold packages", t_examples_registry),
    ]
    with tempfile.TemporaryDirectory(prefix="clivane-test-") as tmp:
        for name, fn in tests:
            check(name, lambda fn=fn: fn(tempfile.mkdtemp(dir=tmp)))
    failed = [r for r in RESULTS if not r[1]]
    print("selftest: %d/%d passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
