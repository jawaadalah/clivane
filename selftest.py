#!/usr/bin/env python3
"""clivane self-test suite. Pure stdlib; everything runs in temp dirs.

Coverage: forge (AST -> commands, run, JSON envelope, REPL), mirror (help
parsing fixture + envelope), openapi (operationId -> commands, --help),
verify (seal PASS + tampered trace FAIL), registry tiers, determinism.
"""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

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


def main():
    tests = [
        ("forge: AST->commands, run, --json, REPL", t_forge_ast_and_run),
        ("mirror: help parsing + JSON envelope", t_mirror),
        ("openapi: operationId->commands, --help", t_openapi),
        ("verify: seal PASS + tampered trace FAIL", t_verify_pass_and_tamper),
        ("registry: Silver -> Gold tiers", t_registry_tiers),
        ("determinism: forge digest stable x2", t_determinism),
    ]
    with tempfile.TemporaryDirectory(prefix="clivane-test-") as tmp:
        for name, fn in tests:
            check(name, lambda fn=fn: fn(tempfile.mkdtemp(dir=tmp)))
    failed = [r for r in RESULTS if not r[1]]
    print("selftest: %d/%d passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
