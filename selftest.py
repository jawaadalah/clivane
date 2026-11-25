#!/usr/bin/env python3
"""clivane self-test suite. Pure stdlib; everything runs in temp dirs.

Coverage: forge (AST -> commands, run, JSON envelope, REPL), mirror (help
parsing fixture + envelope), openapi (operationId -> commands, --help),
verify (seal PASS + tampered trace FAIL), registry tiers, determinism,
game (contract valid/invalid, deterministic episodes, replay tamper),
serve (live server: health/commands/run, remote client, token, probe),
and the committed examples registry (5 packages, all Gold).
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
    assert manifest["commands"] == ["act", "actions", "new", "observe",
                                    "replay", "status"], manifest["commands"]
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


def _start_server(pkg_dir, port, extra_env=None):
    env = dict(os.environ)
    env["CLIVANE_PORT"] = str(port)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.Popen([sys.executable, "server.py"], cwd=pkg_dir, env=env,
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


def t_examples_registry(tmp):
    registry = clivane.read_json(os.path.join(HERE, "examples", "registry.json"))
    pkgs = registry["packages"]
    assert len(pkgs) == 5, [p["name"] for p in pkgs]
    assert all(p["tier"] == "Gold" for p in pkgs), pkgs
    paths = sorted(p["path"] for p in pkgs)
    assert paths == ["forge", "game", "mirror", "openapi", "serve"], paths
    # live recompute (never trust the file): both new packages replay PASS
    for child in ("gemgrid-cli", "notebox-server"):
        pkg_dir = os.path.join(HERE, "examples", child)
        status, detail = clivane.verify_package(pkg_dir)
        assert status == "PASS", (child, status, detail)
        assert clivane.tier_of(pkg_dir) == "Gold", child


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
        ("examples: registry holds 5 Gold packages", t_examples_registry),
    ]
    with tempfile.TemporaryDirectory(prefix="clivane-test-") as tmp:
        for name, fn in tests:
            check(name, lambda fn=fn: fn(tempfile.mkdtemp(dir=tmp)))
    failed = [r for r in RESULTS if not r[1]]
    print("selftest: %d/%d passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
