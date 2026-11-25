#!/usr/bin/env python3
"""clivane - make ANY software agent-native, deterministically.

Six generation paths plus a proof system, all in pure Python stdlib.
No LLM, no API key, no network required for forge / mirror / verify.

Paths:
  forge   - static AST analysis of a Python module -> full agent CLI
  mirror  - wrap an existing CLI tool -> JSON-envelope wrapper CLI
  openapi - OpenAPI 3 JSON spec -> per-operation CLI
  game    - a Python game obeying the clivane game contract -> episode CLI;
            with --fullstack, a complete arena: HTTP session server +
            web UI + remote client CLI + replay-verified leaderboard
  serve   - a Python module -> HTTP JSON server + remote client CLI
  probe   - introspect a live server -> endpoint map + generic client CLI
  live    - a live game adapter (state/RNG on a remote server) -> live
            CLI with session journals, policy guardrails, a shadow
            simulator + divergence check, a guarded autopilot and a
            read-only observer. Live results are labelled
            "live-recorded" - never replay-verified (see SOP).

Proof:
  verify     - Trace-Seal: record golden traces, replay them, PASS/FAIL
  live-check - conformance suite for live adapters (offline, vs a mock)
  registry   - scan generated packages, assign Bronze/Silver/Gold tiers

See SOP.md for the full methodology.
"""

import argparse
import ast
import contextlib
import copy
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

SEAL_VERSION = 1
TIERS = ("Bronze", "Silver", "Gold")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def kebab(name):
    """snake_case / camelCase -> kebab-case."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name)
    s = s.replace("_", "-").lower()
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def write_file(path, content):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def pkg_manifest(pkg_dir):
    path = os.path.join(pkg_dir, "manifest.json")
    if not os.path.isfile(path):
        raise SystemExit("no manifest.json in %s (not a clivane package)" % pkg_dir)
    return read_json(path)


def run_cli(pkg_dir, entry, argv, timeout=30, env=None):
    """Run a generated CLI inside a package dir; return (code, out, err)."""
    full_env = None
    if env:
        full_env = dict(os.environ)
        full_env.update(env)
    proc = subprocess.run(
        [sys.executable, entry] + list(argv),
        cwd=pkg_dir, capture_output=True, text=True, timeout=timeout,
        env=full_env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def load_module_from_path(module_path, alias=None):
    """Import a Python module straight from a file path."""
    mod_name = os.path.splitext(os.path.basename(module_path))[0]
    spec = importlib.util.spec_from_file_location(alias or mod_name, module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def free_port():
    with contextlib.closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def http_json(method, url, payload=None, headers=None, timeout=5):
    """Tiny JSON HTTP helper; returns (status, parsed-or-text)."""
    data = None
    hdrs = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read().decode("utf-8", "replace")
    try:
        return status, json.loads(raw)
    except Exception:
        return status, raw


# --------------------------------------------------------------------------
# FORGE - static AST analysis of a Python module
# --------------------------------------------------------------------------

def _annotation_name(node):
    if node is None:
        return "str"
    try:
        text = ast.unparse(node)
    except Exception:
        return "str"
    text = text.strip()
    for base in ("int", "float", "bool", "str", "list", "dict"):
        if text == base or text.startswith(base + "["):
            return base
    return "str"


def _literal_default(node):
    if node is None:
        return False, None
    try:
        return True, ast.literal_eval(node)
    except Exception:
        return False, None


def analyse_module(module_path):
    """Return (module_name, [function specs]) for public functions."""
    with open(module_path, encoding="utf-8") as fh:
        source = fh.read()
    tree = ast.parse(source, filename=module_path)
    mod_name = os.path.splitext(os.path.basename(module_path))[0]
    specs = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("_"):
            continue
        args = []
        positional = list(node.args.args)
        defaults = [None] * (len(positional) - len(node.args.defaults)) + list(node.args.defaults)
        for arg, default_node in zip(positional, defaults):
            if arg.arg in ("self", "cls"):
                continue
            has_default, default_val = _literal_default(default_node)
            args.append({
                "name": arg.arg,
                "flag": "--" + kebab(arg.arg),
                "type": _annotation_name(arg.annotation),
                "required": not has_default,
                "default": default_val if has_default else None,
            })
        doc = (ast.get_docstring(node) or "").strip()
        specs.append({
            "func": node.name,
            "cmd": kebab(node.name),
            "doc": doc.splitlines()[0] if doc else node.name,
            "args": args,
        })
    specs.sort(key=lambda s: s["cmd"])
    return mod_name, specs


FORGE_TEMPLATE = '''#!/usr/bin/env python3
"""@NAME@ CLI - generated by clivane forge (deterministic, stdlib only).

Wraps the Python module `@MOD@`: one subcommand per public function,
--json on every command, plus a stateful REPL when run with no args.
"""
import argparse
import importlib
import json
import os
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
MODULE = importlib.import_module("@MOD@")
SPECS = json.loads(r"""@SPECS@""")


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
    parser = argparse.ArgumentParser(prog="@NAME@", description="Agent CLI for @MOD@")
    sub = parser.add_subparsers(dest="command")
    for spec in SPECS:
        p = sub.add_parser(spec["cmd"], help=spec["doc"])
        for arg in spec["args"]:
            p.add_argument(arg["flag"], required=arg["required"],
                           default=arg["default"], help=arg["type"])
        p.add_argument("--json", action="store_true", help="structured JSON output")
    return parser


def dispatch(argv):
    parser = build_parser()
    ns = parser.parse_args(argv)
    if not ns.command:
        parser.print_help()
        return 2
    spec = next(s for s in SPECS if s["cmd"] == ns.command)
    kwargs = {}
    for arg in spec["args"]:
        kwargs[arg["name"]] = coerce(getattr(ns, arg["name"]), arg["type"])
    result = getattr(MODULE, spec["func"])(**kwargs)
    if ns.json:
        print(json.dumps({"command": spec["cmd"], "result": result},
                         indent=2, sort_keys=True, default=str))
    elif result is not None:
        if isinstance(result, (dict, list)):
            print(json.dumps(result, indent=2, sort_keys=True, default=str))
        else:
            print(result)
    return 0


def repl():
    print("@NAME@ REPL - type a command, 'help', or 'exit'")
    while True:
        try:
            line = input("@NAME@> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in ("exit", "quit"):
            return 0
        if line == "help":
            build_parser().print_help()
            continue
        try:
            dispatch(shlex.split(line))
        except SystemExit as exc:
            if exc.code not in (0, None):
                print("error: exit %s" % exc.code)
        except Exception as exc:  # keep the session alive
            print("error: %s" % exc)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return repl()
    try:
        return dispatch(argv)
    except Exception as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
'''


def render_forge_cli(name, mod_name, specs):
    text = FORGE_TEMPLATE.replace("@NAME@", name)
    text = text.replace("@MOD@", mod_name)
    text = text.replace("@SPECS@", json.dumps(specs, indent=1, sort_keys=True))
    return text


def skill_md_forge(name, mod_name, specs, source_desc):
    lines = [
        "# %s" % name,
        "",
        "Agent skill for the `%s` CLI (forged by clivane from `%s`)." % (name, mod_name),
        source_desc,
        "",
        "## Commands",
        "",
    ]
    for spec in specs:
        flags = " ".join(
            "%s <%s>" % (a["flag"], a["type"]) if a["required"] else "[%s <%s>]" % (a["flag"], a["type"])
            for a in spec["args"]
        )
        lines.append("- `%s %s [--json]` - %s" % (spec["cmd"], flags, spec["doc"]))
    lines += [
        "",
        "## Conventions",
        "",
        "- Add `--json` to any command for a structured envelope:",
        '  `{"command": "<cmd>", "result": <value>}`.',
        "- Run with no arguments to enter the stateful REPL.",
        "- Exit code 0 means success; errors print to stderr with exit 1.",
        "",
    ]
    return "\n".join(lines)


def cmd_forge(module_path, out_dir):
    if not os.path.isfile(module_path):
        raise SystemExit("module not found: %s" % module_path)
    mod_name, specs = analyse_module(module_path)
    if not specs:
        raise SystemExit("no public functions found in %s" % module_path)
    name = mod_name.replace("_", "-") + "-cli"
    os.makedirs(out_dir, exist_ok=True)
    shutil.copyfile(module_path, os.path.join(out_dir, os.path.basename(module_path)))
    entry = name.replace("-", "_") + ".py"
    write_file(os.path.join(out_dir, entry),
               render_forge_cli(name, mod_name, specs))
    write_file(os.path.join(out_dir, "SKILL.md"),
               skill_md_forge(name, mod_name, specs,
                              "Deterministic path: static AST analysis, no LLM involved."))
    write_file(os.path.join(out_dir, "test_smoke.py"), SMOKE_TEMPLATE.replace("@ENTRY@", entry))
    manifest = {
        "name": name,
        "path": "forge",
        "entry": entry,
        "source": os.path.basename(module_path),
        "commands": [s["cmd"] for s in specs],
        "tier": "Bronze",
    }
    write_file(os.path.join(out_dir, "manifest.json"),
               json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


SMOKE_TEMPLATE = '''#!/usr/bin/env python3
"""Smoke test for this generated CLI: --help everywhere must exit 0."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRY = os.path.join(HERE, "@ENTRY@")


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
'''


# --------------------------------------------------------------------------
# MIRROR - wrap an existing CLI tool behind a JSON envelope
# --------------------------------------------------------------------------

def parse_help(help_text):
    """Extract likely subcommand names from a --help text.

    Heuristics: a 'Commands:'/'Available commands:' section wins; otherwise
    indented `name  description` lines in the first section are used.
    """
    commands = []
    in_section = False
    for raw in help_text.splitlines():
        line = raw.rstrip()
        low = line.strip().lower()
        if low in ("commands:", "available commands:", "available subcommands:"):
            in_section = True
            continue
        if in_section:
            if not line.strip():
                break
            m = re.match(r"^\s{2,}([a-zA-Z][a-zA-Z0-9_-]*)\s{2,}\S", line)
            if m and m.group(1) not in commands:
                commands.append(m.group(1))
            elif not line.startswith((" ", "\t")):
                break
    if not commands:
        for raw in help_text.splitlines():
            m = re.match(r"^\s{2,}([a-zA-Z][a-zA-Z0-9_-]*)\s{2,}\S", raw)
            if m and m.group(1) not in commands and not raw.strip().startswith("-"):
                commands.append(m.group(1))
    return commands


def probe_tool(tool):
    """Run `<tool> --help`; return (help_text, [subcommands])."""
    proc = subprocess.run([tool, "--help"], capture_output=True, text=True, timeout=30)
    help_text = (proc.stdout or "") + (proc.stderr or "")
    subs = parse_help(help_text)
    detailed = {}
    for sub in subs[:20]:
        try:
            p = subprocess.run([tool, sub, "--help"], capture_output=True, text=True, timeout=15)
            detailed[sub] = ((p.stdout or "") + (p.stderr or "")).strip()
        except Exception:
            detailed[sub] = ""
    return help_text, subs, detailed


MIRROR_TEMPLATE = '''#!/usr/bin/env python3
"""@NAME@ - clivane mirror wrapper around the `@TOOL@` CLI.

Forwards every argument to the real tool. Add `--json` anywhere to get a
structured envelope instead of raw output:
  {"tool": "@TOOL@", "argv": [...], "exit_code": int,
   "stdout": "...", "stderr": "..."}
"""
import json
import os
import subprocess
import sys

TOOL = "@TOOL@"


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in args
    if json_mode:
        args.remove("--json")
    proc = subprocess.run([TOOL] + args, capture_output=True, text=True)
    if json_mode:
        print(json.dumps({
            "tool": os.path.basename(TOOL),
            "argv": args,
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }, indent=2, sort_keys=True))
    else:
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
'''


def skill_md_mirror(name, tool, help_text, subs, detailed):
    lines = [
        "# %s" % name,
        "",
        "Mirror wrapper for the existing `%s` CLI (wrapped by clivane)." % tool,
        "The wrapper forwards arguments to the real tool; `--json` returns a",
        "structured envelope `{tool, argv, exit_code, stdout, stderr}`.",
        "",
        "## Command map (from `%s --help`)" % tool,
        "",
    ]
    if subs:
        for sub in subs:
            first = ""
            if detailed.get(sub):
                first = detailed[sub].splitlines()[0].strip()
            lines.append("- `%s %s` - %s" % (tool, sub, first or "see --help"))
    else:
        lines.append("- (no subcommands detected; single-command tool)")
    lines += ["", "## Top-level help", "", "```", help_text.strip()[:2000], "```", ""]
    return "\n".join(lines)


def cmd_mirror(tool, out_dir):
    help_text, subs, detailed = probe_tool(tool)
    tool_ref = tool.rstrip("/")
    base = os.path.basename(tool_ref)
    name = "mirror-" + kebab(base)
    entry = name.replace("-", "_") + ".py"
    os.makedirs(out_dir, exist_ok=True)
    write_file(os.path.join(out_dir, entry),
               MIRROR_TEMPLATE.replace("@NAME@", name).replace("@TOOL@", tool_ref))
    write_file(os.path.join(out_dir, "SKILL.md"),
               skill_md_mirror(name, base, help_text, subs, detailed))
    write_file(os.path.join(out_dir, "test_smoke.py"), SMOKE_TEMPLATE.replace("@ENTRY@", entry))
    manifest = {
        "name": name,
        "path": "mirror",
        "entry": entry,
        "source": base,
        "commands": subs,
        "tier": "Bronze",
    }
    write_file(os.path.join(out_dir, "manifest.json"),
               json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


# --------------------------------------------------------------------------
# OPENAPI - generate a CLI from an OpenAPI 3 JSON spec
# --------------------------------------------------------------------------

def analyse_openapi(spec_path):
    spec = read_json(spec_path)
    title = (spec.get("info") or {}).get("title", "api")
    base_url = ""
    servers = spec.get("servers") or []
    if servers:
        base_url = servers[0].get("url", "")
    ops = []
    for path, item in sorted((spec.get("paths") or {}).items()):
        for method, op in sorted(item.items()):
            if method.lower() not in ("get", "post", "put", "patch", "delete"):
                continue
            op_id = op.get("operationId") or (method + path)
            params = []
            for prm in op.get("parameters", []):
                schema = prm.get("schema") or {}
                params.append({
                    "name": prm["name"],
                    "flag": "--" + kebab(prm["name"]),
                    "in": prm.get("in", "query"),
                    "required": bool(prm.get("required")),
                    "type": schema.get("type", "string"),
                })
            params.sort(key=lambda p: p["flag"])
            ops.append({
                "cmd": kebab(op_id),
                "operationId": op_id,
                "method": method.upper(),
                "path": path,
                "doc": (op.get("summary") or op_id).strip(),
                "params": params,
                "has_body": method.lower() in ("post", "put", "patch"),
            })
    ops.sort(key=lambda o: o["cmd"])
    return title, base_url, ops


OPENAPI_TEMPLATE = '''#!/usr/bin/env python3
"""@NAME@ CLI - generated by clivane openapi (deterministic, stdlib only).

One subcommand per operationId. Set the base URL with --base-url or the
@ENV@ environment variable. Responses print raw; --json wraps them in an
envelope {"status": int, "body": <parsed-or-text>}.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

OPS = json.loads(r"""@OPS@""")
DEFAULT_BASE = "@BASE@"


def build_parser():
    parser = argparse.ArgumentParser(prog="@NAME@", description="API CLI: @TITLE@")
    parser.add_argument("--base-url", default=os.environ.get("@ENV@", DEFAULT_BASE))
    sub = parser.add_subparsers(dest="command")
    for op in OPS:
        p = sub.add_parser(op["cmd"], help=op["doc"])
        for prm in op["params"]:
            p.add_argument(prm["flag"], required=prm["required"],
                           help="%s (%s)" % (prm["in"], prm["type"]))
        if op["has_body"]:
            p.add_argument("--body", default=None, help="JSON request body")
        p.add_argument("--json", action="store_true", help="structured envelope")
    return parser


def main(argv=None):
    parser = build_parser()
    ns = parser.parse_args(argv)
    if not ns.command:
        parser.print_help()
        return 2
    op = next(o for o in OPS if o["cmd"] == ns.command)
    path = op["path"]
    query = {}
    for prm in op["params"]:
        value = getattr(ns, prm["name"])
        if value is None:
            continue
        if prm["in"] == "path":
            path = path.replace("{" + prm["name"] + "}", urllib.parse.quote(str(value)))
        elif prm["in"] == "query":
            query[prm["name"]] = value
    url = ns.base_url.rstrip("/") + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = None
    headers = {"Accept": "application/json"}
    if op["has_body"] and getattr(ns, "body", None):
        data = ns.body.encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=op["method"])
    try:
        with urllib.request.urlopen(req) as resp:
            status, raw = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read().decode("utf-8", "replace")
    try:
        body = json.loads(raw)
    except Exception:
        body = raw
    if ns.json:
        print(json.dumps({"status": status, "body": body}, indent=2, sort_keys=True))
    elif isinstance(body, (dict, list)):
        print(json.dumps(body, indent=2, sort_keys=True))
    else:
        print(body)
    return 0 if status < 400 else 1


if __name__ == "__main__":
    sys.exit(main())
'''


def skill_md_openapi(name, title, ops):
    lines = [
        "# %s" % name,
        "",
        "Agent skill for the `%s` API CLI (generated by clivane from an" % name,
        "OpenAPI 3 spec for %s). One subcommand per operationId." % title,
        "",
        "## Commands",
        "",
    ]
    for op in ops:
        lines.append("- `%s` - %s %s - %s" % (op["cmd"], op["method"], op["path"], op["doc"]))
    lines += [
        "",
        "## Conventions",
        "",
        "- `--base-url` (or the environment variable) selects the API server.",
        "- `--json` wraps responses as `{\"status\": int, \"body\": ...}`.",
        "- Path and query parameters are flags; POST/PUT/PATCH take `--body '<json>'`.",
        "",
    ]
    return "\n".join(lines)


def cmd_openapi(spec_path, out_dir):
    title, base_url, ops = analyse_openapi(spec_path)
    if not ops:
        raise SystemExit("no operations found in %s" % spec_path)
    slug = kebab(title.replace(" API", "").replace("API", "")) or "api"
    name = slug + "-cli"
    entry = name.replace("-", "_") + ".py"
    env_name = slug.upper().replace("-", "_") + "_BASE_URL"
    os.makedirs(out_dir, exist_ok=True)
    text = OPENAPI_TEMPLATE.replace("@NAME@", name)
    text = text.replace("@TITLE@", title)
    text = text.replace("@BASE@", base_url)
    text = text.replace("@ENV@", env_name)
    text = text.replace("@OPS@", json.dumps(ops, indent=1, sort_keys=True))
    write_file(os.path.join(out_dir, entry), text)
    write_file(os.path.join(out_dir, "SKILL.md"), skill_md_openapi(name, title, ops))
    write_file(os.path.join(out_dir, "test_smoke.py"), SMOKE_TEMPLATE.replace("@ENTRY@", entry))
    manifest = {
        "name": name,
        "path": "openapi",
        "entry": entry,
        "source": os.path.basename(spec_path),
        "commands": [o["cmd"] for o in ops],
        "tier": "Bronze",
    }
    write_file(os.path.join(out_dir, "manifest.json"),
               json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


# --------------------------------------------------------------------------
# GAME - a Python game obeying the clivane game contract -> episode CLI
#
# Contract (validated at generation time):
#   new_game(seed) -> state            (dict, JSON-able)
#   legal_actions(state) -> list[str]
#   step(state, action) -> (state, reward, done, info)
#   observe(state) -> dict             (JSON-able)
# --------------------------------------------------------------------------

GAME_FUNCTIONS = ("new_game", "legal_actions", "step", "observe")


def validate_game_contract(module_path):
    """Load the game module and probe its contract; SystemExit if unfit."""
    try:
        module = load_module_from_path(module_path)
    except Exception as exc:
        raise SystemExit("game contract violation: cannot import %s (%s)"
                         % (module_path, exc))
    for fname in GAME_FUNCTIONS:
        fn = getattr(module, fname, None)
        if not callable(fn):
            raise SystemExit(
                "game contract violation: %s must define a callable %s()"
                % (os.path.basename(module_path), fname))

    def _jsonable(value, what):
        try:
            json.dumps(value)
        except Exception:
            raise SystemExit("game contract violation: %s is not JSON-able" % what)

    try:
        state = module.new_game(0)
    except Exception as exc:
        raise SystemExit("game contract violation: new_game(0) raised %s" % exc)
    if not isinstance(state, dict):
        raise SystemExit("game contract violation: new_game() must return a dict state")
    _jsonable(state, "the state from new_game()")
    try:
        actions = module.legal_actions(state)
    except Exception as exc:
        raise SystemExit("game contract violation: legal_actions() raised %s" % exc)
    if (not isinstance(actions, list) or not actions
            or not all(isinstance(a, str) for a in actions)):
        raise SystemExit(
            "game contract violation: legal_actions() must return a non-empty list[str]")
    try:
        obs = module.observe(state)
    except Exception as exc:
        raise SystemExit("game contract violation: observe() raised %s" % exc)
    if not isinstance(obs, dict):
        raise SystemExit("game contract violation: observe() must return a dict")
    _jsonable(obs, "the observation")
    try:
        result = module.step(state, actions[0])
    except Exception as exc:
        raise SystemExit("game contract violation: step() raised %s" % exc)
    if not isinstance(result, (tuple, list)) or len(result) != 4:
        raise SystemExit(
            "game contract violation: step() must return (state, reward, done, info)")
    new_state, reward, done, info = result
    if not isinstance(new_state, dict):
        raise SystemExit("game contract violation: step() must return a dict state")
    _jsonable(new_state, "the state from step()")
    if not isinstance(reward, (int, float)) or isinstance(reward, bool):
        raise SystemExit("game contract violation: step() reward must be a number")
    if not isinstance(done, bool):
        raise SystemExit("game contract violation: step() done must be a bool")
    if not isinstance(info, dict):
        raise SystemExit("game contract violation: step() info must be a dict")
    return module


GAME_TEMPLATE = '''#!/usr/bin/env python3
"""@NAME@ - episode CLI for the game module `@MOD@` (clivane game path).

Session state lives in session.json next to this file, so separate
invocations continue the same episode. `replay` re-runs an action list
from a seed and checks the final state matches the recorded session
(or a second identical replay when no matching session exists).
"""
import argparse
import importlib
import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
MODULE = importlib.import_module("@MOD@")
HERE = os.path.dirname(os.path.abspath(__file__))
SESSION = os.path.join(HERE, "session.json")


def save_session(sess):
    with open(SESSION, "w", encoding="utf-8") as fh:
        json.dump(sess, fh, indent=2, sort_keys=True)


def load_session():
    if not os.path.isfile(SESSION):
        raise SystemExit("no session yet - start one with: new --seed N")
    with open(SESSION, encoding="utf-8") as fh:
        return json.load(fh)


def emit(command, result, as_json):
    if as_json:
        print(json.dumps({"command": command, "result": result},
                         indent=2, sort_keys=True, default=str))
    elif isinstance(result, (dict, list)):
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        print(result)


def play(seed, actions):
    """Run a fresh episode; return (final_state, total_reward)."""
    state = MODULE.new_game(seed)
    total = 0
    for action in actions:
        legal = MODULE.legal_actions(state)
        if action not in legal:
            return state, total, False
        state, reward, done, _info = MODULE.step(state, action)
        total += reward
        if done:
            break
    return state, total, True


@AUTOPILOT@

def build_parser():
    p = argparse.ArgumentParser(prog="@NAME@", description="Episode CLI for @MOD@")
    sub = p.add_subparsers(dest="command")
    n = sub.add_parser("new", help="start a new episode")
    n.add_argument("--seed", type=int, default=0)
    n.add_argument("--json", action="store_true")
    o = sub.add_parser("observe", help="observe the current state")
    o.add_argument("--json", action="store_true")
    a = sub.add_parser("actions", help="list legal actions")
    a.add_argument("--json", action="store_true")
    t = sub.add_parser("act", help="apply one action")
    t.add_argument("action")
    t.add_argument("--json", action="store_true")
    s = sub.add_parser("status", help="session summary")
    s.add_argument("--json", action="store_true")
    r = sub.add_parser("replay", help="replay actions from a seed and verify")
    r.add_argument("--seed", type=int, required=True)
    r.add_argument("--actions", required=True, help="comma-separated actions")
    r.add_argument("--json", action="store_true")
    add_autopilot_args(sub)
    return p


def main(argv=None):
    ns = build_parser().parse_args(argv)
    if not ns.command:
        build_parser().print_help()
        return 2
    if ns.command == "new":
        state = MODULE.new_game(ns.seed)
        sess = {"seed": ns.seed, "state": state, "actions": [],
                "last_reward": None, "done": False}
        save_session(sess)
        emit("new", {"seed": ns.seed, "observe": MODULE.observe(state)}, ns.json)
        return 0
    if ns.command == "replay":
        actions = [a for a in ns.actions.split(",") if a != ""]
        final, total, ok = play(ns.seed, actions)
        verified = False
        if ok:
            sess = load_session() if os.path.isfile(SESSION) else None
            if sess is not None and sess["seed"] == ns.seed:
                # A transcript exists for this seed: the replay must
                # reproduce it exactly - same actions, same final state.
                # Any altered action is a verification failure.
                verified = (sess["actions"] == actions
                            and final == sess["state"])
            else:
                # No recorded transcript: determinism means a second
                # identical replay must land on the identical state.
                again, _, ok2 = play(ns.seed, actions)
                verified = bool(ok2 and again == final)
        emit("replay", {"seed": ns.seed, "steps": len(actions),
                        "total_reward": total,
                        "final_observe": MODULE.observe(final),
                        "verified": verified}, ns.json)
        return 0 if verified else 1
    if ns.command == "autopilot":
        return run_autopilot_command(ns)
    sess = load_session()
    state = sess["state"]
    if ns.command == "observe":
        emit("observe", MODULE.observe(state), ns.json)
        return 0
    if ns.command == "actions":
        emit("actions", MODULE.legal_actions(state), ns.json)
        return 0
    if ns.command == "status":
        emit("status", {"seed": sess["seed"], "steps": len(sess["actions"]),
                        "done": sess["done"], "last_reward": sess["last_reward"],
                        "observe": MODULE.observe(state)}, ns.json)
        return 0
    if ns.command == "act":
        if sess["done"]:
            print("error: episode is done - start a new game", file=sys.stderr)
            return 1
        legal = MODULE.legal_actions(state)
        if ns.action not in legal:
            print("error: illegal action %r (legal: %s)"
                  % (ns.action, ", ".join(legal)), file=sys.stderr)
            return 1
        state, reward, done, info = MODULE.step(state, ns.action)
        sess.update({"state": state, "last_reward": reward, "done": done})
        sess["actions"].append(ns.action)
        save_session(sess)
        emit("act", {"observe": MODULE.observe(state), "reward": reward,
                     "done": done, "info": info}, ns.json)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
'''


# --------------------------------------------------------------------------
# SAFE AUTOPILOT (v1.3.0) - shared engine text embedded into BOTH game
# CLIs (local episode CLI and arena remote client).
#
# A strategy is choose(observe, legal_actions) -> action. Built-ins:
#   greedy - 1-step lookahead on a deep copy of the state (the real
#            state is never touched by planning); key = (reward, done,
#            -distance-to-nearest-target) with legal order as the final
#            deterministic tie-break.
#   bfs    - shortest plan to a done/won state over a bounded node
#            budget, then executed step by step (greedy fallback).
#   random - seeded baseline.
# Custom strategies load from --strategy-file (same choose() contract).
#
# The engine enforces the guardrails around ANY strategy: hard step and
# time budgets, stall detection (observation unchanged K times in a
# row), illegal-action abort (the action is never sent to the game),
# exception containment (partial transcript still persisted, non-zero
# exit, no corrupt state), dry-run (pure simulation, nothing saved,
# nothing finished), and settle-by-replay - the ONLY way a score is
# ever verified. Autopilot can never write a score directly.
# --------------------------------------------------------------------------

AUTOPILOT_COMMON = '''

# ---------------------------------------------------------------------------
# Safe autopilot (clivane) - strategies drive an episode under hard
# guardrails: step/time budgets, stall detection, illegal-action abort,
# exception containment, dry-run simulation, settle-by-replay only.
# ---------------------------------------------------------------------------
import copy as _copy
import random as _random
import time as _time


def _bfs_key(state):
    # Volatile counters would defeat de-duplication; drop the usual ones.
    trimmed = {k: v for k, v in state.items()
               if k not in ("steps", "moves_left")}
    return json.dumps(trimmed, sort_keys=True, default=str)


def _bfs_plan(module, start, max_nodes):
    """Shortest action plan to a done (preferably won) state, or []."""
    from collections import deque
    start = _copy.deepcopy(start)
    seen = {_bfs_key(start)}
    queue = deque([(start, [])])
    fallback = None
    nodes = 0
    while queue:
        state, path = queue.popleft()
        nodes += 1
        if nodes > max_nodes:
            break
        for action in module.legal_actions(state):
            nxt, _r, done, _i = module.step(_copy.deepcopy(state), action)
            npath = path + [action]
            if done:
                if module.observe(nxt).get("won", True):
                    return npath
                if fallback is None:
                    fallback = npath
                continue
            key = _bfs_key(nxt)
            if key not in seen:
                seen.add(key)
                queue.append((nxt, npath))
    return fallback or []


def _target_distance(observe):
    """Manhattan distance player -> nearest gem/target, else None."""
    player = observe.get("player")
    targets = observe.get("gems") or observe.get("targets")
    if (isinstance(player, (list, tuple)) and len(player) == 2
            and isinstance(targets, list) and targets):
        try:
            return min(abs(player[0] - t[0]) + abs(player[1] - t[1])
                       for t in targets)
        except Exception:
            return None
    return None


def _make_choose(name, module, planning_state, seed, max_nodes):
    if name == "random":
        rng = _random.Random(seed)

        def choose_random(observe, legal):
            return rng.choice(list(legal))

        return choose_random
    if name == "greedy":

        def choose_greedy(observe, legal):
            state = planning_state()
            if module is None or state is None:
                raise RuntimeError("greedy needs the game module for lookahead")
            best = None
            best_key = None
            for action in legal:
                nxt, reward, done, _i = module.step(
                    _copy.deepcopy(state), action)
                dist = _target_distance(module.observe(nxt))
                key = (reward, 1 if done else 0,
                       -(dist if dist is not None else 0))
                if best_key is None or key > best_key:
                    best_key = key
                    best = action
            return best

        return choose_greedy
    if name == "bfs":
        plan = {"actions": None}

        def choose_bfs(observe, legal):
            if plan["actions"] is None:
                state = planning_state()
                if module is None or state is None:
                    raise RuntimeError("bfs needs the game module for planning")
                plan["actions"] = _bfs_plan(module, state, max_nodes)
            if plan["actions"]:
                return plan["actions"].pop(0)
            return _make_choose("greedy", module, planning_state,
                                seed, max_nodes)(observe, legal)

        return choose_bfs
    raise RuntimeError("unknown strategy: %s" % name)


def _load_strategy_file(path):
    spec = importlib.util.spec_from_file_location("clivane_user_strategy",
                                                  path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    choose = getattr(mod, "choose", None)
    if not callable(choose):
        raise RuntimeError(
            "strategy file %s must define choose(observe, legal_actions)"
            % path)
    return choose


def run_autopilot(driver, choose, strategy_name, seed, max_steps,
                  max_seconds, target_score, stall_limit, dry_run):
    """Drive one episode through `driver` under the guardrails.

    Returns (result_dict, exit_code). The transcript is persisted via
    driver.persist() on every non-dry-run exit path (aborts included);
    driver.settle() (replay verification) runs only for done episodes.
    Nothing here can write a score anywhere.
    """
    started = _time.monotonic()
    steps = 0
    total_reward = 0
    stop_reason = None
    error = None
    prev_key = json.dumps(driver.obs_now(), sort_keys=True, default=str)
    unchanged = 0
    while True:
        obs = driver.obs_now()
        if driver.done_now():
            stop_reason = "done"
            break
        cur_score = obs.get("score")
        if (target_score is not None
                and isinstance(cur_score, (int, float))
                and not isinstance(cur_score, bool)
                and cur_score >= target_score):
            stop_reason = "target_reached"
            break
        if steps >= max_steps:
            stop_reason = "step_budget"
            break
        if _time.monotonic() - started > max_seconds:
            stop_reason = "time_budget"
            break
        legal = driver.legal()
        if not legal:
            stop_reason = "error"
            error = "no legal actions but the episode is not done"
            break
        try:
            action = choose(_copy.deepcopy(obs), list(legal))
        except Exception as exc:
            stop_reason = "error"
            error = "strategy raised: %s" % exc
            break
        if action not in legal:
            stop_reason = "illegal_action"
            error = ("strategy returned an action outside "
                     "legal_actions: %r" % (action,))
            break
        try:
            obs, reward, done, _info = driver.act(action)
        except Exception as exc:
            stop_reason = "error"
            error = "step failed: %s" % exc
            break
        steps += 1
        if isinstance(reward, (int, float)) and not isinstance(reward, bool):
            total_reward += reward
        if done:
            stop_reason = "done"
            break
        obs_score = obs.get("score")
        if (target_score is not None
                and isinstance(obs_score, (int, float))
                and not isinstance(obs_score, bool)
                and obs_score >= target_score):
            stop_reason = "target_reached"
            break
        key = json.dumps(obs, sort_keys=True, default=str)
        if key == prev_key:
            unchanged += 1
        else:
            unchanged = 0
            prev_key = key
        if unchanged >= stall_limit:
            stop_reason = "stall"
            break
    final_obs = driver.obs_now()
    final_score = final_obs.get("score")
    if not (isinstance(final_score, (int, float))
            and not isinstance(final_score, bool)):
        final_score = total_reward
    verified = False
    if not dry_run:
        try:
            driver.persist()
        except Exception:
            pass
        if driver.done_now():
            try:
                verified = bool(driver.settle())
            except Exception:
                verified = False
    result = {"strategy": strategy_name, "seed": seed, "steps": steps,
              "score": final_score, "done": bool(driver.done_now()),
              "stop_reason": stop_reason, "verified": verified}
    if dry_run:
        result["dry_run"] = True
    if error:
        result["error"] = error
    code = 1 if stop_reason in ("error", "illegal_action") else 0
    return result, code


def add_autopilot_args(sub):
    ap = sub.add_parser("autopilot",
                        help="let a strategy play an episode, safely")
    ap.add_argument("--strategy", default="greedy",
                    choices=["greedy", "bfs", "random"])
    ap.add_argument("--strategy-file",
                    help="Python file defining choose(observe, legal_actions)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--max-steps", type=int, default=200)
    ap.add_argument("--max-seconds", type=float, default=60)
    ap.add_argument("--target-score", type=float, default=None)
    ap.add_argument("--stall-limit", type=int, default=5)
    ap.add_argument("--bfs-max-nodes", type=int, default=20000)
    ap.add_argument("--dry-run", action="store_true",
                    help="simulate only: no session saved, no finish")
    ap.add_argument("--json", action="store_true")
    return ap
'''


AUTOPILOT_LOCAL_GLUE = '''

class _LocalDriver:
    """Autopilot driver over the local module (no HTTP)."""

    def __init__(self, seed, persist_enabled):
        self.seed = seed
        self.persist_enabled = persist_enabled
        self.state = MODULE.new_game(seed)
        self.transcript = []
        self.last_reward = None
        self.done = False

    def obs_now(self):
        return MODULE.observe(self.state)

    def legal(self):
        return MODULE.legal_actions(self.state)

    def act(self, action):
        self.state, reward, done, info = MODULE.step(self.state, action)
        self.done = bool(done)
        self.last_reward = reward
        self.transcript.append(action)
        return MODULE.observe(self.state), reward, self.done, info

    def done_now(self):
        return self.done

    def planning_state(self):
        return self.state

    def persist(self):
        if self.persist_enabled:
            save_session({"seed": self.seed, "state": self.state,
                          "actions": list(self.transcript),
                          "last_reward": self.last_reward,
                          "done": self.done})

    def settle(self):
        final, _total, ok = play(self.seed, list(self.transcript))
        return bool(ok and final == self.state)


def run_autopilot_command(ns):
    driver = _LocalDriver(ns.seed, persist_enabled=not ns.dry_run)
    if ns.strategy_file:
        try:
            choose = _load_strategy_file(ns.strategy_file)
        except Exception as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 1
        strategy_name = "file:" + os.path.basename(ns.strategy_file)
    else:
        choose = _make_choose(ns.strategy, MODULE, driver.planning_state,
                              ns.seed, ns.bfs_max_nodes)
        strategy_name = ns.strategy
    result, code = run_autopilot(driver, choose, strategy_name, ns.seed,
                                 ns.max_steps, ns.max_seconds,
                                 ns.target_score, ns.stall_limit,
                                 ns.dry_run)
    print(json.dumps(result, indent=2, sort_keys=True))
    return code

'''


AUTOPILOT_REMOTE_GLUE = '''

def _call_safe(remote, method, path, payload=None):
    """call() that raises RuntimeError instead of SystemExit."""
    try:
        return call(remote, method, path, payload)
    except SystemExit:
        raise RuntimeError("arena request failed: %s %s" % (method, path))


class _RemoteDriver:
    """Autopilot driver over HTTP.

    Planning (greedy/bfs lookahead) uses a local shadow copy of the
    game module bundled in this package; the server stays
    authoritative: every act executes server-side and the returned
    observation must match the shadow, else the run aborts.
    """

    def __init__(self, remote, seed, player, max_steps):
        self.remote = remote
        created = _call_safe(remote, "POST", "/sessions",
                             {"seed": seed, "player": player,
                              "max_steps": max_steps})
        self.sid = created["id"]
        self.obs = created["observe"]
        self.done = bool(created["done"])
        self.transcript = []
        self.module = None
        self.shadow = None
        try:
            self.module = importlib.import_module("@MOD@")
            self.shadow = self.module.new_game(seed)
        except Exception:
            self.module = None
            self.shadow = None

    def obs_now(self):
        return self.obs

    def legal(self):
        res = _call_safe(self.remote, "GET",
                         "/sessions/%s/actions" % self.sid)
        return res["actions"]

    def act(self, action):
        res = _call_safe(self.remote, "POST",
                         "/sessions/%s/act" % self.sid, {"action": action})
        if self.module is not None:
            self.shadow, _r, _d, _i = self.module.step(self.shadow, action)
            if self.module.observe(self.shadow) != res["observe"]:
                raise RuntimeError(
                    "arena observation diverged from the local plan "
                    "(non-deterministic game?)")
        self.obs = res["observe"]
        self.done = bool(res["done"])
        self.transcript.append(action)
        return res["observe"], res["reward"], self.done, res.get("info", {})

    def done_now(self):
        return self.done

    def planning_state(self):
        return self.shadow

    def persist(self):
        pass  # the transcript lives server-side in the session

    def settle(self):
        try:
            res = _call_safe(self.remote, "POST",
                             "/sessions/%s/finish" % self.sid, {})
        except Exception:
            return False
        return bool(res.get("verified"))


class _SimDriver:
    """Dry-run driver: full local simulation, zero server contact."""

    def __init__(self, seed):
        self.module = importlib.import_module("@MOD@")
        self.state = self.module.new_game(seed)
        self.done = False

    def obs_now(self):
        return self.module.observe(self.state)

    def legal(self):
        return self.module.legal_actions(self.state)

    def act(self, action):
        self.state, reward, done, info = self.module.step(self.state,
                                                          action)
        self.done = bool(done)
        return self.module.observe(self.state), reward, self.done, info

    def done_now(self):
        return self.done

    def planning_state(self):
        return self.state

    def persist(self):
        pass

    def settle(self):
        return False


def run_autopilot_command(ns):
    if ns.dry_run:
        try:
            driver = _SimDriver(ns.seed)
        except Exception as exc:
            print("error: dry-run needs the bundled game module (%s)"
                  % exc, file=sys.stderr)
            return 1
        module = driver.module
    else:
        try:
            driver = _RemoteDriver(
                ns.remote, ns.seed,
                ns.player or ("autopilot-%s" % ns.strategy), ns.max_steps)
        except Exception as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 1
        module = driver.module
    if ns.strategy_file:
        try:
            choose = _load_strategy_file(ns.strategy_file)
        except Exception as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 1
        strategy_name = "file:" + os.path.basename(ns.strategy_file)
    else:
        choose = _make_choose(ns.strategy, module, driver.planning_state,
                              ns.seed, ns.bfs_max_nodes)
        strategy_name = ns.strategy
    result, code = run_autopilot(driver, choose, strategy_name, ns.seed,
                                 ns.max_steps, ns.max_seconds,
                                 ns.target_score, ns.stall_limit,
                                 ns.dry_run)
    print(json.dumps(result, indent=2, sort_keys=True))
    return code

'''


def skill_md_game(name, mod_name):
    return "\n".join([
        "# %s" % name,
        "",
        "Episode CLI for the game `%s` (generated by the clivane game path)." % mod_name,
        "The game module obeys the clivane game contract: `new_game(seed)`,",
        "`legal_actions(state)`, `step(state, action)`, `observe(state)`.",
        "",
        "## Commands",
        "",
        "- `new --seed N [--json]` - start an episode; state is saved to",
        "  `session.json` next to the CLI so invocations share the episode.",
        "- `observe [--json]` - the current observation.",
        "- `actions [--json]` - legal actions in the current state.",
        "- `act <action> [--json]` - apply one step; returns",
        "  `{observe, reward, done, info}` and appends to the transcript.",
        "- `status [--json]` - seed, steps taken, done flag, last reward.",
        "- `replay --seed N --actions a,b,c [--json]` - re-play from scratch",
        "  and verify the final state is identical (seed determinism).",
        "- `autopilot [--strategy greedy|bfs|random] [--strategy-file f.py]`",
        "  `[--seed N] [--max-steps N] [--max-seconds N] [--target-score N]`",
        "  `[--dry-run]` - let a strategy play a whole episode under hard",
        "  guardrails (see below); prints the result JSON.",
        "",
        "## Safe autopilot",
        "",
        "A strategy is `choose(observe, legal_actions) -> action`. Built-ins:",
        "`greedy` (1-step lookahead on a copy of the state), `bfs` (shortest",
        "plan to a won/done state, bounded node budget), `random` (seeded",
        "baseline). A custom file with `choose()` loads via `--strategy-file`.",
        "The engine enforces, for every strategy: hard step/time budgets,",
        "stall detection (observation unchanged `--stall-limit` times in a",
        "row -> stop), illegal-action abort (the action is never sent to",
        "the game), and exception containment (partial transcript saved,",
        "non-zero exit, no corrupt state). `--dry-run` simulates the whole",
        "episode without saving a session. A finished episode is settled",
        "by replay: the result's `verified` is true only when a fresh",
        "replay reproduces it. Result JSON: `{strategy, seed, steps,",
        "score, done, stop_reason, verified}` with `stop_reason` one of",
        "`done | target_reached | step_budget | time_budget | stall |`",
        "`illegal_action | error`.",
        "",
        "## Conventions",
        "",
        "- `--json` wraps results as `{\"command\": ..., \"result\": ...}`.",
        "- Illegal actions and acting after `done` exit 1 with an error.",
        "- A replay that diverges exits 1 and reports `\"verified\": false`.",
        "",
    ])


def cmd_game(module_path, out_dir, fullstack=False):
    if fullstack:
        return cmd_game_fullstack(module_path, out_dir)
    if not os.path.isfile(module_path):
        raise SystemExit("module not found: %s" % module_path)
    validate_game_contract(module_path)
    mod_name = os.path.splitext(os.path.basename(module_path))[0]
    name = mod_name.replace("_", "-") + "-cli"
    entry = name.replace("-", "_") + ".py"
    os.makedirs(out_dir, exist_ok=True)
    shutil.copyfile(module_path, os.path.join(out_dir, os.path.basename(module_path)))
    cli_text = GAME_TEMPLATE.replace(
        "@AUTOPILOT@", AUTOPILOT_COMMON + AUTOPILOT_LOCAL_GLUE)
    write_file(os.path.join(out_dir, entry),
               cli_text.replace("@NAME@", name).replace("@MOD@", mod_name))
    write_file(os.path.join(out_dir, "SKILL.md"), skill_md_game(name, mod_name))
    write_file(os.path.join(out_dir, "test_smoke.py"), SMOKE_TEMPLATE.replace("@ENTRY@", entry))
    manifest = {
        "name": name,
        "path": "game",
        "entry": entry,
        "source": os.path.basename(module_path),
        "commands": ["act", "actions", "autopilot", "new", "observe",
                     "replay", "status"],
        "tier": "Bronze",
    }
    write_file(os.path.join(out_dir, "manifest.json"),
               json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


# --------------------------------------------------------------------------
# GAME FULLSTACK - the game path as a complete arena (v1.2.0)
#
# `clivane game <module> --fullstack` emits three layers over the same
# game contract:
#   * game_server.py - stdlib HTTP server hosting many isolated episode
#     sessions, the web UI, and a persistent leaderboard (scores.json).
#     A score is recorded ONLY by POST /sessions/<id>/finish, which
#     replays the episode from its seed and requires an identical, done
#     final state - clients can never submit a score directly.
#   * web/index.html - a dependency-free page that renders observe()
#     (grid when the observation carries one, entity grid for
#     player/gems/obstacles observations, JSON card otherwise), plays
#     manually via the legal-action buttons, and can watch any session.
#   * a remote client CLI with the local game verbs (new / observe /
#     actions / act) plus finish and leaderboard.
# --------------------------------------------------------------------------

ARENA_SERVER_TEMPLATE = '''#!/usr/bin/env python3
"""@NAME@ - fullstack game arena server for `@MOD@` (clivane game path).

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

Bot guardrails (enforced server-side, whatever the client sends):
POST /sessions accepts max_steps; an act beyond that budget aborts the
session (403, and the session stays aborted), and acts are rate-limited
to 20/second per session (429) - loose for humans, tight for wild loops.
"""
import argparse
import importlib
import itertools
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
MODULE = importlib.import_module("@MOD@")
NAME = "@NAME@"
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
            "finished": sess["finished"], "aborted": sess["aborted"],
            "max_steps": sess["max_steps"],
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
            max_steps = payload.get("max_steps")
            if not isinstance(seed, int) or isinstance(seed, bool):
                self._send(400, {"error": "seed must be an integer"})
                return
            if not isinstance(player, str) or not player:
                self._send(400, {"error": "player must be a non-empty string"})
                return
            if max_steps is not None and (
                    not isinstance(max_steps, int)
                    or isinstance(max_steps, bool) or max_steps <= 0):
                self._send(400, {"error": "max_steps must be a positive integer"})
                return
            with LOCK:
                sid = "s%d" % next(IDS)
                sess = {"id": sid, "seed": seed, "player": player,
                        "state": MODULE.new_game(seed), "actions": [],
                        "total_reward": 0, "done": False, "finished": False,
                        "max_steps": max_steps, "aborted": False,
                        "act_times": []}
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
                # Server-side guardrails (v1.3.0): a rogue client cannot
                # run a session without bounds, whatever it sends.
                if sess["aborted"]:
                    self._send(403, {"error": "session aborted by the server "
                                     "(step budget exceeded)"})
                    return
                if (sess["max_steps"] is not None
                        and len(sess["actions"]) >= sess["max_steps"]):
                    sess["aborted"] = True
                    self._send(403, {"error": "server-side step budget "
                                     "exceeded; session aborted"})
                    return
                now = time.monotonic()
                sess["act_times"] = [t for t in sess["act_times"]
                                     if now - t < 1.0]
                if len(sess["act_times"]) >= 20:
                    self._send(429, {"error": "rate limit: over 20 "
                                     "actions/second on this session"})
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
                sess["act_times"].append(time.monotonic())
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
'''


ARENA_CLIENT_TEMPLATE = '''#!/usr/bin/env python3
"""@NAME@ client - remote CLI for a clivane fullstack game arena.

The local game verbs (new / observe / actions / act) plus finish and
leaderboard, but every call is HTTP to the arena server. Base URL:
--remote, else $CLIVANE_REMOTE, else http://127.0.0.1:8800.
$CLIVANE_TOKEN is sent as X-Clivane-Token.
"""
import argparse
import importlib
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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


@AUTOPILOT@

def build_parser():
    p = argparse.ArgumentParser(prog="@NAME@",
                                description="Remote client for the @MOD@ arena")
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
    ap = add_autopilot_args(sub)
    ap.add_argument("--player", default=None,
                    help="leaderboard name (default: autopilot-<strategy>)")
    return p


def main(argv=None):
    parser = build_parser()
    ns = parser.parse_args(argv)
    if not ns.command:
        parser.print_help()
        return 2
    if ns.command == "autopilot":
        return run_autopilot_command(ns)
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
'''


ARENA_WEB_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>@NAME@ - clivane game arena</title>
<style>
:root{--navy:#0A1931;--blue:#185ADB;--accent:#FF7A00;--bg:#eef1f7;--card:#ffffff;--ink:#16233c;--muted:#5b6b87;--line:#d7deeb}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink)}
header{background:var(--navy);color:#fff;padding:18px 22px}
header h1{margin:0;font-size:20px}
header p{margin:4px 0 0;color:#b9c6de;font-size:13px}
main{max-width:1020px;margin:0 auto;padding:18px;display:grid;grid-template-columns:1.2fr .8fr;gap:16px}
@media(max-width:760px){main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.card h2{margin:0 0 10px;font-size:15px;color:var(--navy)}
label{font-size:12px;color:var(--muted);display:block;margin:8px 0 4px}
input{width:100%;padding:8px;border:1px solid var(--line);border-radius:8px;font-size:14px}
button{background:var(--blue);color:#fff;border:0;border-radius:8px;padding:8px 14px;font-size:14px;cursor:pointer}
button.secondary{background:var(--navy)}
button.accent{background:var(--accent)}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.board{display:grid;gap:4px;margin:12px 0}
.cell{aspect-ratio:1;border-radius:6px;background:#e7ecf5;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:15px;color:var(--navy)}
.cell.player{background:var(--blue);color:#fff}
.cell.gem{background:#ffd166;color:#7a4d00}
.cell.obstacle{background:var(--navy);color:#fff}
.cell.other{background:#c9d6f2}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{border-bottom:1px solid var(--line);padding:6px 4px;text-align:left}
pre{background:#0A1931;color:#dbe6ff;border-radius:8px;padding:10px;overflow:auto;font-size:12px;max-height:260px}
.muted{color:var(--muted);font-size:13px}
.badge{display:inline-block;background:#e7ecf5;border-radius:20px;padding:2px 10px;font-size:12px;color:var(--navy);margin-right:4px}
</style>
</head>
<body>
<header>
<h1>@NAME@</h1>
<p>Fullstack game arena by clivane - play in the browser, or drive the same sessions with the agent CLI. Scores reach the leaderboard only after a deterministic replay verifies them.</p>
</header>
<main>
<section class="card">
<h2>Game</h2>
<div class="row">
<div style="flex:1;min-width:120px"><label>Player name</label><input id="player" value="human"></div>
<div style="width:110px"><label>Seed</label><input id="seed" type="number" value="7"></div>
<div style="padding-top:22px"><button id="newBtn">New session</button></div>
</div>
<label>Session id (play it, or type any id to watch it)</label>
<div class="row">
<input id="sessionId" style="flex:1" placeholder="create a session, or type an id to watch">
<button class="secondary" id="watchBtn">Watch</button>
<label class="row" style="margin:0"><input type="checkbox" id="autoWatch" style="width:auto"> auto-refresh (1s)</label>
</div>
<div id="board" class="board"></div>
<div id="stateCard"></div>
<div class="row" id="actions"></div>
<div class="row" style="margin-top:10px">
<button class="accent" id="finishBtn">Finish &amp; verify score</button>
<span id="status" class="muted"></span>
</div>
</section>
<section class="card">
<h2>Leaderboard - verified scores only</h2>
<table><thead><tr><th>#</th><th>Player</th><th>Seed</th><th>Score</th><th>Steps</th></tr></thead><tbody id="boardBody"></tbody></table>
<p class="muted">A score appears here only when the server replays the whole episode from its seed and the final state matches exactly. There is no way to submit a score directly.</p>
<h2 style="margin-top:18px">Live sessions</h2>
<div id="sessions" class="muted">none yet</div>
</section>
</main>
<script>
const $=id=>document.getElementById(id);
let timer=null;
async function api(method,path,body){
  const opt={method:method,headers:{}};
  if(body!==undefined){opt.headers["Content-Type"]="application/json";opt.body=JSON.stringify(body);}
  const r=await fetch(path,opt);
  const data=await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(data.error||("HTTP "+r.status));
  return data;
}
function cellClass(v){
  if(v==="P")return "cell player";
  if(v==="\u25c6"||v==="G")return "cell gem";
  if(v==="#"||v==="X")return "cell obstacle";
  if(v==="."||v===""||v===0||v===null)return "cell";
  return "cell other";
}
function gridFromObserve(obs){
  if(Array.isArray(obs.grid)) return obs.grid;
  if(obs.player&&obs.gems&&obs.obstacles){
    let n=1;
    const pts=[obs.player].concat(obs.gems,obs.obstacles);
    pts.forEach(p=>{n=Math.max(n,p[0]+1,p[1]+1);});
    if(typeof obs.size==="number") n=obs.size;
    const g=Array.from({length:n},()=>Array(n).fill("."));
    obs.obstacles.forEach(p=>{g[p[1]][p[0]]="#";});
    obs.gems.forEach(p=>{g[p[1]][p[0]]="\u25c6";});
    g[obs.player[1]][obs.player[0]]="P";
    return g;
  }
  return null;
}
function render(obs){
  const board=$("board");board.innerHTML="";
  const grid=gridFromObserve(obs);
  if(grid){
    board.style.gridTemplateColumns="repeat("+grid.length+",1fr)";
    grid.forEach(row=>row.forEach(v=>{
      const d=document.createElement("div");d.className=cellClass(v);
      d.textContent=(v==="."?"":v);board.appendChild(d);
    }));
  }
  const bits=[];
  if(typeof obs.score!=="undefined")bits.push("score "+obs.score);
  if(typeof obs.steps!=="undefined")bits.push("steps "+obs.steps);
  if(typeof obs.moves_left!=="undefined")bits.push("moves left "+obs.moves_left);
  bits.push(obs.done?(obs.won?"won":"done"):"playing");
  $("stateCard").innerHTML="<p>"+bits.map(b=>'<span class="badge">'+b+"</span>").join(" ")+"</p><pre>"+JSON.stringify(obs,null,2)+"</pre>";
}
async function refreshActions(){
  const id=$("sessionId").value.trim();if(!id)return;
  try{
    const data=await api("GET","/sessions/"+id+"/actions");
    const box=$("actions");box.innerHTML="";
    data.actions.forEach(a=>{
      const b=document.createElement("button");b.textContent=a;
      b.onclick=()=>act(a);box.appendChild(b);
    });
  }catch(e){$("status").textContent=e.message;}
}
async function show(id){
  const data=await api("GET","/sessions/"+id+"/observe");
  render(data.observe);
}
async function act(a){
  const id=$("sessionId").value.trim();if(!id)return;
  try{
    const data=await api("POST","/sessions/"+id+"/act",{action:a});
    render(data.observe);
    $("status").textContent="reward "+data.reward+(data.done?" - episode done":"");
    await refreshActions();await refreshSessions();
  }catch(e){$("status").textContent=e.message;}
}
async function newSession(){
  try{
    const data=await api("POST","/sessions",{player:$("player").value||"player",seed:parseInt($("seed").value||"0",10)});
    $("sessionId").value=data.id;
    render(data.observe);
    $("status").textContent="session "+data.id+" created";
    await refreshActions();await refreshSessions();
  }catch(e){$("status").textContent=e.message;}
}
async function finish(){
  const id=$("sessionId").value.trim();if(!id)return;
  try{
    const data=await api("POST","/sessions/"+id+"/finish",{});
    $("status").textContent="verified - score "+data.score+" recorded";
    await refreshBoard();
  }catch(e){$("status").textContent=e.message;}
}
async function refreshBoard(){
  try{
    const data=await api("GET","/leaderboard");
    $("boardBody").innerHTML=data.scores.map((s,i)=>"<tr><td>"+(i+1)+"</td><td>"+s.player+"</td><td>"+s.seed+"</td><td><strong>"+s.score+"</strong></td><td>"+s.steps+"</td></tr>").join("");
  }catch(e){}
}
async function refreshSessions(){
  try{
    const data=await api("GET","/sessions");
    $("sessions").innerHTML=data.sessions.length?data.sessions.map(s=>'<div><button class="secondary" data-sid="'+s.id+'">'+s.id+"</button> "+s.player+" - seed "+s.seed+" - "+s.steps+" steps"+(s.done?" - done":"")+"</div>").join(""):"none yet";
    document.querySelectorAll("[data-sid]").forEach(b=>{b.onclick=()=>{$("sessionId").value=b.getAttribute("data-sid");show(b.getAttribute("data-sid")).then(refreshActions);};});
  }catch(e){}
}
$("newBtn").onclick=newSession;
$("finishBtn").onclick=finish;
$("watchBtn").onclick=()=>{const id=$("sessionId").value.trim();if(id){show(id).then(refreshActions);}};
$("autoWatch").onchange=e=>{
  if(timer){clearInterval(timer);timer=null;}
  if(e.target.checked){timer=setInterval(()=>{const id=$("sessionId").value.trim();if(id)show(id);refreshBoard();refreshSessions();},1000);}
};
refreshBoard();refreshSessions();
setInterval(refreshBoard,5000);
</script>
</body>
</html>
"""


def skill_md_game_fullstack(name, mod_name):
    return "\n".join([
        "# %s" % name,
        "",
        "Fullstack arena for the game `%s` (clivane game path, `--fullstack`)." % mod_name,
        "One game contract, three layers: an HTTP session server, a web UI,",
        "and a remote client CLI - plus a leaderboard that only accepts",
        "replay-verified scores.",
        "",
        "## Run the arena",
        "",
        "```bash",
        "python3 game_server.py                    # 127.0.0.1:8800, UI at /",
        "python3 game_server.py --token SECRET     # require X-Clivane-Token on POSTs",
        "```",
        "",
        "Open http://127.0.0.1:8800/ to play in the browser or watch any",
        "session live. Verified scores persist in `scores.json` next to",
        "the server and survive restarts.",
        "",
        "## Endpoints",
        "",
        "| Method | Path | Meaning |",
        "|---|---|---|",
        "| GET | `/` | the web UI (static, no dependencies) |",
        "| GET | `/health` | `{\"status\": \"ok\"}` |",
        "| POST | `/sessions` | `{\"seed\": N, \"player\": \"name\", \"max_steps\": M?}` -> new isolated session (server-enforced step budget) |",
        "| GET | `/sessions` | list live sessions |",
        "| GET | `/sessions/<id>/observe` | current observation |",
        "| GET | `/sessions/<id>/actions` | legal actions now |",
        "| POST | `/sessions/<id>/act` | `{\"action\": \"...\"}` -> step (illegal action = 400) |",
        "| POST | `/sessions/<id>/finish` | replay-verify the episode; record the score |",
        "| GET | `/leaderboard` | verified scores, best first |",
        "",
        "## Play as an agent (remote client CLI)",
        "",
        "```bash",
        "export CLIVANE_REMOTE=http://127.0.0.1:8800",
        "python3 %s new --seed 7 --player agent --json   # -> session id" % (name.replace("-", "_") + "_client.py"),
        "python3 %s act down --session s1 --json" % (name.replace("-", "_") + "_client.py"),
        "python3 %s finish --session s1 --json           # verified score" % (name.replace("-", "_") + "_client.py"),
        "python3 %s leaderboard --json" % (name.replace("-", "_") + "_client.py"),
        "```",
        "",
        "## Score verification (anti fake-claim)",
        "",
        "- There is NO endpoint that accepts a score. `finish` takes no",
        "  score from the client - the server replays the session's whole",
        "  action transcript from its seed and records a score only when",
        "  the replay ends `done` in a state identical to the session's.",
        "- The recorded score is read from the replayed final observation",
        "  (`score` field) or, failing that, the summed step rewards.",
        "- A session can be finished once; unfinished or non-done",
        "  episodes are rejected (400).",
        "",
        "## Safe autopilot (bots welcome, bounds enforced)",
        "",
        "The client CLI has an `autopilot` command: a strategy",
        "(`greedy` | `bfs` | `random`, or `--strategy-file` with a",
        "`choose(observe, legal_actions)` function) plays a whole episode",
        "under hard budgets (`--max-steps`, `--max-seconds`), stall",
        "detection, illegal-action abort and exception containment.",
        "Planning uses a local shadow copy of the bundled game module;",
        "the server stays authoritative and any observation divergence",
        "aborts the run. `--dry-run` simulates locally with zero server",
        "contact: no session is created, nothing reaches the leaderboard.",
        "When an episode finishes, autopilot calls `finish` - the score",
        "still comes only from the server's replay, never from the bot.",
        "Sessions are created as player `autopilot-<strategy>` unless",
        "`--player` says otherwise.",
        "",
        "Server-side, independent of the client: `POST /sessions`",
        "accepts `max_steps`; an act past that budget aborts the session",
        "(403, permanently for that session), and acts are rate-limited",
        "to 20/second per session (429). A rogue bot cannot run unbounded",
        "against this server.",
        "",
        "## Security",
        "",
        "- Default bind is localhost only; a non-local bind warns loudly.",
        "- Set a token before exposing the arena beyond your machine.",
        "- Sessions are isolated in memory per id; the only shared state",
        "  is the append-only verified leaderboard.",
        "",
    ])


def cmd_game_fullstack(module_path, out_dir):
    if not os.path.isfile(module_path):
        raise SystemExit("module not found: %s" % module_path)
    validate_game_contract(module_path)
    mod_name = os.path.splitext(os.path.basename(module_path))[0]
    name = mod_name.replace("_", "-") + "-arena"
    entry = name.replace("-", "_") + "_client.py"
    os.makedirs(out_dir, exist_ok=True)
    shutil.copyfile(module_path, os.path.join(out_dir, os.path.basename(module_path)))
    write_file(os.path.join(out_dir, "game_server.py"),
               ARENA_SERVER_TEMPLATE.replace("@NAME@", name).replace("@MOD@", mod_name))
    write_file(os.path.join(out_dir, "web", "index.html"),
               ARENA_WEB_HTML.replace("@NAME@", name))
    client_text = ARENA_CLIENT_TEMPLATE.replace(
        "@AUTOPILOT@", AUTOPILOT_COMMON + AUTOPILOT_REMOTE_GLUE)
    write_file(os.path.join(out_dir, entry),
               client_text.replace("@NAME@", name).replace("@MOD@", mod_name))
    write_file(os.path.join(out_dir, "SKILL.md"), skill_md_game_fullstack(name, mod_name))
    write_file(os.path.join(out_dir, "test_smoke.py"), SMOKE_TEMPLATE.replace("@ENTRY@", entry))
    manifest = {
        "name": name,
        "path": "game-fullstack",
        "mode": "fullstack",
        "entry": entry,
        "server": "game_server.py",
        "source": os.path.basename(module_path),
        "commands": ["act", "actions", "autopilot", "finish", "leaderboard",
                     "new", "observe"],
        "tier": "Bronze",
    }
    write_file(os.path.join(out_dir, "manifest.json"),
               json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


# --------------------------------------------------------------------------
# SERVE - a Python module -> HTTP JSON server + remote client CLI
#
# The generated server exposes the module's public functions (same spec
# analysis as forge) over stdlib http.server:
#   GET  /health   -> {"status": "ok", ...}
#   GET  /commands -> {"commands": [specs]}
#   POST /run      -> {"command": ..., "args": {...}} -> forge envelope
# Security: binds 127.0.0.1 by default, /run only dispatches commands in
# the module manifest (no free eval), optional X-Clivane-Token header.
# --------------------------------------------------------------------------

SERVER_TEMPLATE = '''#!/usr/bin/env python3
"""@NAME@ server - HTTP JSON front-end for `@MOD@` (clivane serve path).

Endpoints:
  GET  /health    -> {"status": "ok", "name": "@NAME@"}
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
MODULE = importlib.import_module("@MOD@")
SPECS = json.loads(r"""@SPECS@""")
BY_CMD = {s["cmd"]: s for s in SPECS}
NAME = "@NAME@"


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
'''


CLIENT_TEMPLATE = '''#!/usr/bin/env python3
"""@NAME@ client - remote CLI for a clivane serve server (stdlib only).

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

SPECS = json.loads(r"""@SPECS@""")
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
    parser = argparse.ArgumentParser(prog="@NAME@",
                                     description="Remote client for @MOD@")
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
'''


def skill_md_serve(name, mod_name, specs):
    lines = [
        "# %s" % name,
        "",
        "HTTP server + remote client for the module `%s` (clivane serve path)." % mod_name,
        "",
        "## Run the server",
        "",
        "```bash",
        "python3 server.py                 # 127.0.0.1:8799 (CLIVANE_PORT to change)",
        "python3 server.py --token SECRET  # require X-Clivane-Token on /run",
        "```",
        "",
        "Endpoints: `GET /health`, `GET /commands`, `POST /run` with",
        '`{"command": "<cmd>", "args": {...}}` -> the forge envelope',
        '`{"command": ..., "result": ...}`. `/run` only dispatches commands',
        "from the module manifest - there is no free-form evaluation.",
        "",
        "## Run the client",
        "",
        "```bash",
        "export CLIVANE_REMOTE=http://127.0.0.1:8799",
        "python3 %s <command> [--json]" % (name.replace("-", "_") + "_client.py"),
        "```",
        "",
        "## Commands (served)",
        "",
    ]
    for spec in specs:
        lines.append("- `%s` - %s" % (spec["cmd"], spec["doc"]))
    lines += [
        "",
        "## Security",
        "",
        "- Default bind is localhost only; a non-local bind warns loudly.",
        "- Set a token before exposing the server beyond your machine.",
        "",
    ]
    return "\n".join(lines)


def cmd_serve(module_path, out_dir):
    if not os.path.isfile(module_path):
        raise SystemExit("module not found: %s" % module_path)
    mod_name, specs = analyse_module(module_path)
    if not specs:
        raise SystemExit("no public functions found in %s" % module_path)
    name = mod_name.replace("_", "-") + "-server"
    entry = name.replace("-", "_") + "_client.py"
    specs_json = json.dumps(specs, indent=1, sort_keys=True)
    os.makedirs(out_dir, exist_ok=True)
    shutil.copyfile(module_path, os.path.join(out_dir, os.path.basename(module_path)))
    write_file(os.path.join(out_dir, "server.py"),
               SERVER_TEMPLATE.replace("@NAME@", name).replace("@MOD@", mod_name)
               .replace("@SPECS@", specs_json))
    write_file(os.path.join(out_dir, entry),
               CLIENT_TEMPLATE.replace("@NAME@", name).replace("@MOD@", mod_name)
               .replace("@SPECS@", specs_json))
    write_file(os.path.join(out_dir, "SKILL.md"), skill_md_serve(name, mod_name, specs))
    write_file(os.path.join(out_dir, "test_smoke.py"), SMOKE_TEMPLATE.replace("@ENTRY@", entry))
    manifest = {
        "name": name,
        "path": "serve",
        "entry": entry,
        "server": "server.py",
        "source": os.path.basename(module_path),
        "commands": [s["cmd"] for s in specs],
        "tier": "Bronze",
    }
    write_file(os.path.join(out_dir, "manifest.json"),
               json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


# --------------------------------------------------------------------------
# PROBE - introspect a live server -> endpoint map + generic client CLI
# --------------------------------------------------------------------------

def probe_server(base_url):
    """Return a map dict describing whatever the live server exposes."""
    base = base_url.rstrip("/")
    info = {"base_url": base, "health": None, "commands": [], "operations": [],
            "root": None}
    status, health = http_json("GET", base + "/health")
    if status == 200:
        info["health"] = health
    status, cmds = http_json("GET", base + "/commands")
    if status == 200 and isinstance(cmds, dict):
        info["commands"] = cmds.get("commands") or []
    status, spec = http_json("GET", base + "/openapi.json")
    if status == 200 and isinstance(spec, dict) and spec.get("paths"):
        title, _base, ops = analyse_openapi_doc(spec)
        info["operations"] = ops
        info["openapi_title"] = title
    status, root = http_json("GET", base + "/")
    if status == 200:
        info["root"] = root
    if not info["commands"] and not info["operations"] and info["health"] is None:
        raise SystemExit(
            "probe found nothing at %s (no /health, /commands or /openapi.json); "
            "is the server running?" % base)
    return info


def analyse_openapi_doc(spec):
    """analyse_openapi() on an already-parsed spec dict."""
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(spec, fh)
        return analyse_openapi(path)
    finally:
        os.unlink(path)


PROBE_CLIENT_TEMPLATE = '''#!/usr/bin/env python3
"""@NAME@ - generic client for a probed server (clivane probe path).

Base URL is baked in (override with --remote or $CLIVANE_REMOTE).
Run-commands (from the server's /commands) accept --args '<json>' and
POST /run; OpenAPI operations accept --params '<json>' and --body
'<json>'; health/root are plain GETs.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "@BASE@"
RUN_COMMANDS = json.loads(r"""@RUN@""")
OPERATIONS = json.loads(r"""@OPS@""")


def call(method, url, payload=None):
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    token = os.environ.get("CLIVANE_TOKEN")
    if token:
        headers["X-Clivane-Token"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status, raw = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        print("error: cannot reach %s (%s)" % (url, exc), file=sys.stderr)
        return None, 1
    try:
        return json.loads(raw), status
    except Exception:
        return raw, status


def build_parser():
    parser = argparse.ArgumentParser(prog="@NAME@",
                                     description="Client for probed server @BASE@")
    parser.add_argument("--remote",
                        default=os.environ.get("CLIVANE_REMOTE", BASE))
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("health", help="GET /health")
    sub.add_parser("root", help="GET /")
    for cmd in RUN_COMMANDS:
        p = sub.add_parser(cmd, help="POST /run %s" % cmd)
        p.add_argument("--args", default="{}", help="JSON args object")
        p.add_argument("--json", action="store_true")
    for op in OPERATIONS:
        p = sub.add_parser(op["cmd"], help="%s %s" % (op["method"], op["path"]))
        p.add_argument("--params", default="{}", help="JSON path/query params")
        if op["has_body"]:
            p.add_argument("--body", default=None, help="JSON request body")
        p.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    parser = build_parser()
    ns = parser.parse_args(argv)
    if not ns.command:
        parser.print_help()
        return 2
    base = ns.remote.rstrip("/")
    if ns.command in ("health", "root"):
        payload, status = call("GET", base + ("/health" if ns.command == "health" else "/"))
        if payload is None:
            return 1
        print(json.dumps(payload, indent=2, sort_keys=True, default=str)
              if isinstance(payload, (dict, list)) else payload)
        return 0 if status < 400 else 1
    if ns.command in RUN_COMMANDS:
        try:
            args = json.loads(ns.args)
        except Exception as exc:
            print("error: --args is not valid JSON (%s)" % exc, file=sys.stderr)
            return 1
        payload, status = call("POST", base + "/run",
                               {"command": ns.command, "args": args})
        if payload is None:
            return 1
        if status >= 400:
            print("error: %s" % (payload.get("error") if isinstance(payload, dict)
                                 else payload), file=sys.stderr)
            return 1
        if ns.json:
            print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        else:
            result = payload.get("result") if isinstance(payload, dict) else payload
            print(json.dumps(result, indent=2, sort_keys=True, default=str)
                  if isinstance(result, (dict, list)) else result)
        return 0
    op = next(o for o in OPERATIONS if o["cmd"] == ns.command)
    try:
        params = json.loads(ns.params)
    except Exception as exc:
        print("error: --params is not valid JSON (%s)" % exc, file=sys.stderr)
        return 1
    path = op["path"]
    query = {}
    for prm in op["params"]:
        if prm["name"] in params:
            if prm["in"] == "path":
                path = path.replace("{" + prm["name"] + "}",
                                    urllib.parse.quote(str(params.pop(prm["name"]))))
            elif prm["in"] == "query":
                query[prm["name"]] = params.pop(prm["name"])
    url = base + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    payload_body = None
    if op["has_body"] and getattr(ns, "body", None):
        try:
            payload_body = json.loads(ns.body)
        except Exception:
            payload_body = ns.body
    payload, status = call(op["method"], url, payload_body)
    if payload is None:
        return 1
    if ns.json:
        print(json.dumps({"status": status, "body": payload},
                         indent=2, sort_keys=True, default=str))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str)
              if isinstance(payload, (dict, list)) else payload)
    return 0 if status < 400 else 1


if __name__ == "__main__":
    sys.exit(main())
'''


def skill_md_probe(name, info):
    lines = [
        "# %s" % name,
        "",
        "Generic client for the live server at `%s` (clivane probe path)." % info["base_url"],
        "The endpoint map below was captured by introspection, not by hand.",
        "",
        "## Endpoint map",
        "",
        "- `GET /health` -> %s" % ("responded" if info["health"] is not None else "not found"),
        "- `GET /` -> %s" % ("responded" if info["root"] is not None else "not found"),
    ]
    if info["commands"]:
        lines.append("- `GET /commands` + `POST /run` (clivane serve style):")
        for spec in info["commands"]:
            lines.append("  - `%s` - %s" % (spec["cmd"], spec.get("doc", "")))
    if info["operations"]:
        lines.append("- OpenAPI operations (from `/openapi.json`):")
        for op in info["operations"]:
            lines.append("  - `%s` - %s %s - %s"
                         % (op["cmd"], op["method"], op["path"], op["doc"]))
    lines += [
        "",
        "## Usage",
        "",
        "- Run-commands take `--args '<json>'` and POST `/run`.",
        "- OpenAPI operations take `--params '<json>'` (path/query) and",
        "  `--body '<json>'` where applicable.",
        "- `--json` prints the full response envelope; `$CLIVANE_REMOTE`",
        "  or `--remote` overrides the baked-in base URL.",
        "",
    ]
    return "\n".join(lines)


def cmd_probe(base_url, out_dir):
    info = probe_server(base_url)
    netloc = re.sub(r"[^a-zA-Z0-9]+", "-", base_url.split("://", 1)[-1]).strip("-").lower()
    name = "probe-" + netloc
    entry = name.replace("-", "_") + ".py"
    run_cmds = sorted(s["cmd"] for s in info["commands"])
    commands = sorted(set(run_cmds + [o["cmd"] for o in info["operations"]]
                          + ["health", "root"]))
    os.makedirs(out_dir, exist_ok=True)
    text = PROBE_CLIENT_TEMPLATE.replace("@NAME@", name)
    text = text.replace("@BASE@", info["base_url"])
    text = text.replace("@RUN@", json.dumps(run_cmds))
    text = text.replace("@OPS@", json.dumps(info["operations"], indent=1, sort_keys=True))
    write_file(os.path.join(out_dir, entry), text)
    write_file(os.path.join(out_dir, "SKILL.md"), skill_md_probe(name, info))
    write_file(os.path.join(out_dir, "test_smoke.py"), SMOKE_TEMPLATE.replace("@ENTRY@", entry))
    manifest = {
        "name": name,
        "path": "probe",
        "entry": entry,
        "source": info["base_url"],
        "commands": commands,
        "tier": "Bronze",
    }
    write_file(os.path.join(out_dir, "manifest.json"),
               json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest, info


# --------------------------------------------------------------------------
# LIVE - the live game path (v2.0.0, phases 1-6)
#
# For games whose state and RNG live on someone else's server (a web
# game, a Telegram game, a hosted world). The deterministic game path
# cannot promise replay verification there - the seed is not ours - so
# this path is built on a different, honest contract:
#
#   HONEST LABEL: everything the live path records is labelled
#   "live-recorded". verified is always False here. Replay-verified
#   results exist only in the deterministic game path and in the
#   shadow simulator below.
#
# An ADAPTER is a small Python module bridging clivane to the game:
#   fetch_state() -> dict (JSON-able)          the current public state
#   list_actions(state) -> list[str]           actions legal right now
#   send_action(action, **kw) -> dict          perform one action
#   meta() -> {"name", "game", "transport"}    (optional)
#
# Generation validates the contract STATICALLY (AST signatures), so
# generating a package never touches the real server; --probe opts
# into a live probe (against a mock / staging URL) explicitly.
#
# The generated package carries: the live CLI (observe / actions /
# act / status), a per-session JSONL journal, a policy file with
# fail-closed guardrails, an optional shadow simulator (a plain
# deterministic game-contract module modelling the live game) with a
# divergence check, a guarded live autopilot, and a read-only
# observer dashboard. `clivane live-check` grades an adapter with an
# offline conformance suite against its mock server.
# --------------------------------------------------------------------------

LIVE_FUNCTIONS = ("fetch_state", "send_action", "list_actions")
LIVE_LABEL = "live-recorded"
LIVE_COMMANDS = ["actions", "act", "live-autopilot", "live-observe",
                 "observe", "shadow-check", "status"]


def _live_signature(tree, fname, want_args, adapter_base):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == fname:
            if len(node.args.args) != want_args:
                raise SystemExit(
                    "live contract violation: %s() in %s must take exactly "
                    "%d argument(s)" % (fname, adapter_base, want_args))
            return
    raise SystemExit("live contract violation: %s must define %s()"
                     % (adapter_base, fname))


def validate_live_adapter(adapter_path, probe=False, mock_url=None):
    """Static contract check (default) or live probe (--probe).

    Static mode parses the adapter's AST and checks the required
    function signatures WITHOUT importing or calling anything, so
    generation never touches the real game server. Probe mode loads
    the adapter and calls fetch_state()/list_actions() once - point
    CLIVANE_REMOTE / the adapter's own env at a mock or staging server,
    never at production, unless you mean it.
    """
    base = os.path.basename(adapter_path)
    try:
        with open(adapter_path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
    except Exception as exc:
        raise SystemExit("live contract violation: cannot parse %s (%s)"
                         % (base, exc))
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    for fname in LIVE_FUNCTIONS:
        if fname not in defined:
            raise SystemExit("live contract violation: %s must define %s()"
                             % (base, fname))
    _live_signature(tree, "fetch_state", 0, base)
    _live_signature(tree, "list_actions", 1, base)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "send_action":
            if len(node.args.args) < 1:
                raise SystemExit(
                    "live contract violation: send_action() in %s must take "
                    "at least the action argument" % base)
    info = {"functions": list(LIVE_FUNCTIONS), "meta": None, "probed": False}
    if not probe:
        return info
    if mock_url:
        os.environ["CLIVANE_REMOTE"] = mock_url.rstrip("/")
    try:
        module = load_module_from_path(adapter_path)
        state = module.fetch_state()
    except Exception as exc:
        raise SystemExit("live contract violation: probe fetch_state() "
                         "failed (%s)" % exc)
    if not isinstance(state, dict):
        raise SystemExit("live contract violation: fetch_state() must "
                         "return a dict")
    try:
        json.dumps(state)
    except Exception:
        raise SystemExit("live contract violation: the state from "
                         "fetch_state() is not JSON-able")
    try:
        actions = module.list_actions(state)
    except Exception as exc:
        raise SystemExit("live contract violation: probe list_actions() "
                         "failed (%s)" % exc)
    if (not isinstance(actions, list)
            or not all(isinstance(a, str) for a in actions)):
        raise SystemExit("live contract violation: list_actions() must "
                         "return a list[str]")
    meta = getattr(module, "meta", None)
    if callable(meta):
        try:
            m = meta()
        except Exception:
            m = None
        if isinstance(m, dict):
            info["meta"] = m
    info["probed"] = True
    return info


def shadow_check(sim_path, transcript_path):
    """Replay a live transcript's actions in the shadow simulator and
    measure divergence, field by field.

    The simulator starts from from_observe(first recorded state) when
    it offers one, else from new_game(seed) with the seed recorded at
    session start (default 0). A transition's compared fields are the
    recorded state_after keys the simulator's observe() also exposes,
    plus the step reward when the transcript recorded one. The report
    is a measurement, not a verdict: match_rate is reported as-is and
    `verified` stays False - this is the live path.
    """
    sim = load_module_from_path(sim_path)
    with open(transcript_path, encoding="utf-8") as fh:
        lines = [json.loads(l) for l in fh if l.strip()]
    acts = [l for l in lines if l.get("type") == "act"]
    start = next((l for l in lines if l.get("type") == "session_start"), {})
    seed = start.get("seed", 0)
    if acts and hasattr(sim, "from_observe"):
        state = sim.from_observe(copy.deepcopy(acts[0]["state_before"]))
    else:
        state = sim.new_game(seed)
    matched = mismatched = tmatch = 0
    mismatches = []
    for i, line in enumerate(acts):
        state, reward, _done, _info = sim.step(state, line["action"])
        obs = sim.observe(state)
        rec = line.get("state_after") or {}
        fields = 0
        bad = []
        for key, val in rec.items():
            if key in obs:
                fields += 1
                if obs[key] == val:
                    matched += 1
                else:
                    mismatched += 1
                    bad.append(key)
        rec_reward = (line.get("result") or {}).get("reward")
        if (isinstance(rec_reward, (int, float))
                and not isinstance(rec_reward, bool)):
            fields += 1
            if reward == rec_reward:
                matched += 1
            else:
                mismatched += 1
                bad.append("reward")
        if fields and not bad:
            tmatch += 1
        elif bad:
            mismatches.append({"step": i + 1, "action": line["action"],
                               "fields": bad})
    total = matched + mismatched
    return {"transcript": os.path.basename(transcript_path),
            "transitions": len(acts),
            "transitions_matched": tmatch,
            "fields_matched": matched,
            "fields_mismatched": mismatched,
            "match_rate": round(matched / total, 4) if total else 1.0,
            "mismatches": mismatches[:10],
            "label": LIVE_LABEL,
            "verified": False}


LIVE_HEADER = '''#!/usr/bin/env python3
"""@NAME@ - live CLI for the game adapter `@ADAPTER@` (clivane live path).

The game's state and RNG live on a remote server, reached through the
adapter module bundled next to this file. Everything this CLI records
is labelled "live-recorded": a session journal is evidence of what
happened, never replay-verified proof - the seed belongs to the
server, not to us. The deterministic shadow simulator bundled with
this package (@SHADOWFILE@) is where strategies are tested and live
transcripts are measured for divergence.
"""
import argparse
import html
import importlib
import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
'''


LIVE_COMMON = '''

# ---------------------------------------------------------------------------
# clivane live runtime - adapter driver, session journal, policy.
#
# HONEST LABEL: everything recorded here is "live-recorded". A live
# game's state and RNG live on a remote server, so nothing on this
# path is - or can be - replay-verified; verified is always False.
# ---------------------------------------------------------------------------
import copy as _copy
import time as _time


class PolicyError(Exception):
    """An action refused by policy.json (fail-closed, no override)."""


LABEL = "live-recorded"
HERE = os.path.dirname(os.path.abspath(__file__))
SESSIONS_DIR = os.path.join(HERE, "sessions")
POLICY_PATH = os.path.join(HERE, "policy.json")
ADAPTER = importlib.import_module("@ADAPTER@")
SHADOW = None
if os.path.isfile(os.path.join(HERE, "@SHADOWFILE@")):
    SHADOW = importlib.import_module("@SHADOWMOD@")


def load_policy(path=None):
    path = path or POLICY_PATH
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


POLICY = load_policy()


def fetch():
    state = ADAPTER.fetch_state()
    if not isinstance(state, dict):
        raise RuntimeError("adapter fetch_state() must return a dict")
    return state


def legal(state=None):
    state = fetch() if state is None else state
    return list(ADAPTER.list_actions(state))


def policy_blocked(action):
    return action in (POLICY.get("blocked_actions") or [])


def _summarize(state):
    try:
        json.dumps(state)
        return state
    except Exception:
        return {"unserializable": repr(state)[:200]}


def journal_path(sid):
    return os.path.join(SESSIONS_DIR, sid + ".jsonl")


def journal(entry, sid=None):
    sid = sid or current_session_id(create=True)
    entry = dict(entry)
    entry.setdefault("ts", _time.time())
    entry.setdefault("session", sid)
    with open(journal_path(sid), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True, default=str) + "\\n")
    return sid


def current_session_id(create=False):
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    cur = os.path.join(SESSIONS_DIR, "current.json")
    if os.path.isfile(cur):
        with open(cur, encoding="utf-8") as fh:
            return json.load(fh)["id"]
    if not create:
        return None
    n = 1 + len([f for f in os.listdir(SESSIONS_DIR)
                 if f.startswith("live-") and f.endswith(".jsonl")])
    sid = "live-%d" % n
    with open(cur, "w", encoding="utf-8") as fh:
        json.dump({"id": sid}, fh)
    journal({"type": "session_start", "session": sid, "mode": "live",
             "label": LABEL}, sid=sid)
    return sid


def read_journal(sid):
    path = journal_path(sid)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def all_sessions():
    if not os.path.isdir(SESSIONS_DIR):
        return []
    return sorted(f[:-6] for f in os.listdir(SESSIONS_DIR)
                  if f.startswith("live-") and f.endswith(".jsonl"))


def journal_act_count(within_seconds=None):
    total = 0
    cutoff = _time.time() - within_seconds if within_seconds else None
    for sid in all_sessions():
        for line in read_journal(sid):
            if line.get("type") == "act":
                if cutoff is None or line.get("ts", 0) >= cutoff:
                    total += 1
    return total


def _reward_of(before, result, after):
    reward = result.get("reward")
    if isinstance(reward, (int, float)) and not isinstance(reward, bool):
        return reward
    b, a = before.get("score"), after.get("score")
    if (isinstance(b, (int, float)) and isinstance(a, (int, float))
            and not isinstance(b, bool) and not isinstance(a, bool)):
        return a - b
    return 0


def do_act(action):
    """One live action, journaled. Policy-blocked actions never leave
    this process - fail-closed, and there is deliberately no flag,
    env var or argument that overrides the block."""
    if policy_blocked(action):
        raise PolicyError(
            "policy: action %r is blocked (blocked_actions in policy.json)"
            % action)
    before = fetch()
    actions = legal(before)
    if action not in actions:
        raise RuntimeError("illegal action %r (legal: %s)"
                           % (action, ", ".join(actions)))
    result = ADAPTER.send_action(action)
    if not isinstance(result, dict):
        result = {"result": result}
    after = fetch()
    sid = journal({"type": "act", "state_before": _summarize(before),
                   "action": action, "result": _summarize(result),
                   "state_after": _summarize(after)})
    return {"session": sid, "action": action, "observe": after,
            "result": result, "label": LABEL}

'''


LIVE_MAIN = '''

def emit(command, result, as_json):
    if as_json:
        print(json.dumps({"command": command, "result": result},
                         indent=2, sort_keys=True, default=str))
    elif isinstance(result, (dict, list)):
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        print(result)


# ---------------------------------------------------------------------------
# Shadow simulator - divergence measurement for live transcripts
# ---------------------------------------------------------------------------

def _shadow_state_from(obs, seed):
    if SHADOW is None:
        return None
    if hasattr(SHADOW, "from_observe"):
        return SHADOW.from_observe(_copy.deepcopy(obs))
    return SHADOW.new_game(seed)


def run_shadow_check(transcript_path):
    if SHADOW is None:
        raise RuntimeError("this package has no shadow simulator bundled")
    with open(transcript_path, encoding="utf-8") as fh:
        lines = [json.loads(l) for l in fh if l.strip()]
    acts = [l for l in lines if l.get("type") == "act"]
    start = next((l for l in lines if l.get("type") == "session_start"), {})
    seed = start.get("seed", 0)
    if acts:
        state = _shadow_state_from(acts[0]["state_before"], seed)
    else:
        state = SHADOW.new_game(seed)
    matched = mismatched = tmatch = 0
    mismatches = []
    for i, line in enumerate(acts):
        state, reward, _done, _info = SHADOW.step(state, line["action"])
        obs = SHADOW.observe(state)
        rec = line.get("state_after") or {}
        fields = 0
        bad = []
        for key, val in rec.items():
            if key in obs:
                fields += 1
                if obs[key] == val:
                    matched += 1
                else:
                    mismatched += 1
                    bad.append(key)
        rec_reward = (line.get("result") or {}).get("reward")
        if (isinstance(rec_reward, (int, float))
                and not isinstance(rec_reward, bool)):
            fields += 1
            if reward == rec_reward:
                matched += 1
            else:
                mismatched += 1
                bad.append("reward")
        if fields and not bad:
            tmatch += 1
        elif bad:
            mismatches.append({"step": i + 1, "action": line["action"],
                               "fields": bad})
    total = matched + mismatched
    report = {"transcript": os.path.basename(transcript_path),
              "transitions": len(acts),
              "transitions_matched": tmatch,
              "fields_matched": matched,
              "fields_mismatched": mismatched,
              "match_rate": round(matched / total, 4) if total else 1.0,
              "mismatches": mismatches[:10],
              "label": LABEL, "verified": False}
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    with open(os.path.join(SESSIONS_DIR, "shadow_report.json"), "w",
              encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    return report


# ---------------------------------------------------------------------------
# Live autopilot - strategies under the policy guardrails.
#
# The strategy contract is the same choose(observe, legal) as the
# deterministic autopilot, and greedy/bfs plan against the SHADOW
# simulator (never against the live game). Around any strategy the
# policy file is enforced fail-closed:
#   blocked_actions   - never sent; the run stops `blocked`
#   approval_actions  - human-only; the run stops `needs_human`
#   budgets.max_actions_per_session / step & time budgets
#   budgets.max_actions_per_hour    - counted from the journal
#   budgets.resource_caps {field: max_spend} - spend read from state,
#                       next-step cost estimated on the shadow
#   allowed_hours [a, b]            - local-time window
#   sessions/STOP file              - kill-switch, checked every step
# --dry-run plays the whole episode in the shadow simulator only:
# send_action is never called and nothing is journaled.
# The result is labelled live-recorded and verified stays False -
# there is no replay on this path, by design and by honesty.
# ---------------------------------------------------------------------------

class _LiveDriver:
    """Autopilot driver over the real adapter (not dry-run)."""

    def __init__(self, seed):
        self.seed = seed
        self.state = fetch()
        self.done = bool(self.state.get("done"))
        self.shadow = _shadow_state_from(self.state, seed)

    def obs_now(self):
        return self.state

    def legal(self):
        return legal(self.state)

    def done_now(self):
        return self.done

    def planning_state(self):
        return self.shadow

    def act(self, action):
        before = self.state
        out = do_act(action)
        self.state = out["observe"]
        self.done = bool(self.state.get("done"))
        if self.shadow is not None:
            try:
                self.shadow, _r, _d, _i = SHADOW.step(self.shadow, action)
            except Exception:
                self.shadow = None
        return (self.state, _reward_of(before, out["result"], self.state),
                self.done, out["result"])


class _ShadowDriver:
    """Dry-run driver: shadow simulator only, zero live contact."""

    def __init__(self, seed):
        if SHADOW is None:
            raise RuntimeError(
                "dry-run needs the shadow simulator bundled in this package")
        self.seed = seed
        self.state = SHADOW.new_game(seed)
        self.done = bool(SHADOW.observe(self.state).get("done"))

    def obs_now(self):
        return SHADOW.observe(self.state)

    def legal(self):
        return SHADOW.legal_actions(self.state)

    def done_now(self):
        return self.done

    def planning_state(self):
        return self.state

    def act(self, action):
        self.state, reward, done, info = SHADOW.step(self.state, action)
        self.done = bool(done)
        return SHADOW.observe(self.state), reward, self.done, info


def _hour_allowed(hour, window):
    a, b = window
    if a <= b:
        return a <= hour <= b
    return hour >= a or hour <= b


def _num(value):
    return (isinstance(value, (int, float))
            and not isinstance(value, bool))


def run_live_autopilot(ns):
    budgets = POLICY.get("budgets") or {}
    caps = budgets.get("resource_caps") or {}
    blocked = set(POLICY.get("blocked_actions") or [])
    approval = set(POLICY.get("approval_actions") or [])
    max_actions_session = budgets.get("max_actions_per_session")
    max_actions_hour = budgets.get("max_actions_per_hour")
    allowed_hours = POLICY.get("allowed_hours")
    driver = _ShadowDriver(ns.seed) if ns.dry_run else _LiveDriver(ns.seed)
    if ns.strategy_file:
        choose = _load_strategy_file(ns.strategy_file)
        strategy_name = "file:" + os.path.basename(ns.strategy_file)
    else:
        choose = _make_choose(ns.strategy, SHADOW, driver.planning_state,
                              ns.seed, ns.bfs_max_nodes)
        strategy_name = ns.strategy
    started = _time.monotonic()
    initial = _copy.deepcopy(driver.obs_now())
    steps = 0
    total_reward = 0
    stop_reason = None
    error = None
    prev_key = json.dumps(driver.obs_now(), sort_keys=True, default=str)
    unchanged = 0
    while True:
        obs = driver.obs_now()
        if driver.done_now():
            stop_reason = "done"
            break
        cur = obs.get("score")
        if ns.target_score is not None and _num(cur) \\
                and cur >= ns.target_score:
            stop_reason = "target_reached"
            break
        if steps >= ns.max_steps:
            stop_reason = "step_budget"
            break
        if _time.monotonic() - started > ns.max_seconds:
            stop_reason = "time_budget"
            break
        if (max_actions_session is not None
                and steps >= max_actions_session):
            stop_reason = "budget_actions"
            break
        if not ns.dry_run:
            if allowed_hours and not _hour_allowed(
                    _time.localtime().tm_hour, allowed_hours):
                stop_reason = "rate_window"
                break
            if (max_actions_hour is not None
                    and journal_act_count(3600) >= max_actions_hour):
                stop_reason = "rate_window"
                break
            if os.path.isfile(os.path.join(SESSIONS_DIR, "STOP")):
                stop_reason = "kill_switch"
                break
        legal_actions = driver.legal()
        if not legal_actions:
            stop_reason = "error"
            error = "no legal actions but the episode is not done"
            break
        try:
            action = choose(_copy.deepcopy(obs), list(legal_actions))
        except Exception as exc:
            stop_reason = "error"
            error = "strategy raised: %s" % exc
            break
        if action in blocked:
            stop_reason = "blocked"
            error = "policy: action %r is blocked" % (action,)
            break
        if action in approval:
            stop_reason = "needs_human"
            error = ("policy: action %r needs human approval" % (action,))
            break
        if action not in legal_actions:
            stop_reason = "illegal_action"
            error = ("strategy returned an action outside "
                     "legal_actions: %r" % (action,))
            break
        if caps and SHADOW is not None and driver.planning_state() is not None:
            plan = driver.planning_state()
            try:
                nxt, _r, _d, _i = SHADOW.step(_copy.deepcopy(plan), action)
                plan_obs = SHADOW.observe(plan)
                next_obs = SHADOW.observe(nxt)
                over = None
                for field, cap in caps.items():
                    if _num(initial.get(field)) and _num(obs.get(field)) \\
                            and _num(plan_obs.get(field)) \\
                            and _num(next_obs.get(field)):
                        spend = max(0, initial[field] - obs[field])
                        est = max(0, plan_obs[field] - next_obs[field])
                        if spend + est > cap:
                            over = field
                            break
                if over is not None:
                    stop_reason = "budget_resource"
                    error = ("policy: resource cap for %r would be "
                             "exceeded" % over)
                    break
            except Exception:
                pass
        elif caps:
            over = None
            for field, cap in caps.items():
                if _num(initial.get(field)) and _num(obs.get(field)):
                    if max(0, initial[field] - obs[field]) >= cap:
                        over = field
                        break
            if over is not None:
                stop_reason = "budget_resource"
                error = "policy: resource cap for %r reached" % over
                break
        try:
            obs, reward, done, _info = driver.act(action)
        except PolicyError as exc:
            stop_reason = "blocked"
            error = str(exc)
            break
        except Exception as exc:
            stop_reason = "error"
            error = "step failed: %s" % exc
            break
        steps += 1
        if _num(reward):
            total_reward += reward
        if done:
            stop_reason = "done"
            break
        key = json.dumps(obs, sort_keys=True, default=str)
        if key == prev_key:
            unchanged += 1
        else:
            unchanged = 0
            prev_key = key
        if unchanged >= ns.stall_limit:
            stop_reason = "stall"
            break
    final_obs = driver.obs_now()
    final_score = final_obs.get("score")
    if not _num(final_score):
        final_score = total_reward
    result = {"strategy": strategy_name, "seed": ns.seed, "steps": steps,
              "score": final_score, "done": bool(driver.done_now()),
              "stop_reason": stop_reason, "verified": False,
              "label": LABEL}
    if ns.dry_run:
        result["dry_run"] = True
    if error:
        result["error"] = error
    if not ns.dry_run:
        journal({"type": "summary", "strategy": strategy_name,
                 "steps": steps, "score": final_score,
                 "stop_reason": stop_reason, "label": LABEL})
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if stop_reason in ("error", "illegal_action") else 0


# ---------------------------------------------------------------------------
# Live observer - a READ-ONLY dashboard over the session journals.
# It serves GET / only; every other method is refused 405. There is
# no action endpoint, no form post, nothing that can touch the game.
# ---------------------------------------------------------------------------

def _anomalies():
    out = []
    for sid in all_sessions():
        lines = read_journal(sid)
        acts = [l for l in lines if l.get("type") == "act"]
        if len(acts) >= 3:
            tail = [json.dumps(a.get("state_after"), sort_keys=True,
                               default=str) for a in acts[-3:]]
            if len(set(tail)) == 1:
                out.append("%s: stall suspected (state unchanged x3)" % sid)
        errs = 0
        for line in reversed(lines):
            if line.get("type") != "act":
                continue
            if "error" in (line.get("result") or {}):
                errs += 1
            else:
                break
        if errs >= 3:
            out.append("%s: %d consecutive error results" % (sid, errs))
    rep = os.path.join(SESSIONS_DIR, "shadow_report.json")
    if os.path.isfile(rep):
        try:
            with open(rep, encoding="utf-8") as fh:
                rate = json.load(fh).get("match_rate")
            if rate is not None and rate < 0.8:
                out.append("shadow match_rate low: %s" % rate)
        except Exception:
            pass
    return out


def _observer_html():
    esc = html.escape
    budgets = POLICY.get("budgets") or {}
    caps = budgets.get("resource_caps") or {}
    max_actions = budgets.get("max_actions_per_session")
    rows = []
    recent = []
    for sid in all_sessions():
        lines = read_journal(sid)
        acts = [l for l in lines if l.get("type") == "act"]
        summ = next((l for l in reversed(lines)
                     if l.get("type") == "summary"), None)
        spend_bits = []
        if acts:
            first = acts[0].get("state_before") or {}
            last = acts[-1].get("state_after") or {}
            for field, cap in caps.items():
                if _num(first.get(field)) and _num(last.get(field)):
                    spend_bits.append("%s spent %s / cap %s"
                                      % (field, first[field] - last[field],
                                         cap))
        budget = "%d acts" % len(acts)
        if max_actions is not None:
            budget += " / session budget %s" % max_actions
        if spend_bits:
            budget += "; " + "; ".join(spend_bits)
        rows.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                    % (esc(sid), esc(budget),
                       esc(acts[-1]["action"] if acts else "-"),
                       esc(summ.get("stop_reason", "-") if summ else "-")))
        for line in acts:
            recent.append((line.get("ts", 0), sid, line))
    recent.sort(key=lambda t: t[0], reverse=True)
    recent_rows = []
    for _ts, sid, line in recent[:10]:
        recent_rows.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (esc(sid), esc(str(line.get("action"))),
               esc(json.dumps(line.get("result") or {}, default=str)[:120])))
    anomalies = _anomalies()
    anomaly_html = "".join("<li>%s</li>" % esc(a) for a in anomalies) \\
        or "<li>none</li>"
    rep_html = "no shadow-check run yet"
    rep = os.path.join(SESSIONS_DIR, "shadow_report.json")
    if os.path.isfile(rep):
        try:
            with open(rep, encoding="utf-8") as fh:
                r = json.load(fh)
            rep_html = ("match_rate %s over %s transitions (%s)"
                        % (r.get("match_rate"), r.get("transitions"),
                           esc(str(r.get("transcript")))))
        except Exception:
            rep_html = "shadow report unreadable"
    return ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<meta http-equiv='refresh' content='5'>"
            "<title>@NAME@ - live observer</title>"
            "<style>body{font-family:system-ui,sans-serif;margin:2em;"
            "background:#0A1931;color:#e8eef7}"
            "table{border-collapse:collapse;margin:1em 0}"
            "td,th{border:1px solid #35507a;padding:.3em .7em}"
            ".banner{background:#7a2d0e;padding:.6em 1em;border-radius:6px}"
            "</style></head><body>"
            "<h1>@NAME@ - live observer</h1>"
            "<p class='banner'>Label: <strong>live-recorded</strong> - "
            "these are journal records, NOT replay-verified results. "
            "This dashboard is read-only.</p>"
            "<h2>Sessions</h2><table><tr><th>session</th><th>budget use</th>"
            "<th>last action</th><th>stop_reason</th></tr>"
            + "".join(rows) + "</table>"
            "<h2>Anomalies</h2><ul>" + anomaly_html + "</ul>"
            "<h2>Shadow divergence</h2><p>" + rep_html + "</p>"
            "<h2>Recent actions</h2><table><tr><th>session</th>"
            "<th>action</th><th>result</th></tr>"
            + "".join(recent_rows) + "</table>"
            "</body></html>")


def run_observer(port):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/", "/index.html"):
                body = b"not found"
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = _observer_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _refuse(self):
            body = b"method not allowed: this observer is read-only"
            self.send_response(405)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = _refuse
        do_PUT = _refuse
        do_DELETE = _refuse
        do_PATCH = _refuse

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    print("live observer (read-only) on http://127.0.0.1:%d/" % port)
    server.serve_forever()
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="@NAME@", description="Live CLI for @ADAPTER@ (label: live-recorded)")
    sub = p.add_subparsers(dest="command")
    o = sub.add_parser("observe", help="fetch and print the live state")
    o.add_argument("--json", action="store_true")
    a = sub.add_parser("actions", help="list actions legal right now")
    a.add_argument("--json", action="store_true")
    t = sub.add_parser("act", help="perform one live action (journaled)")
    t.add_argument("action")
    t.add_argument("--policy", default=None,
                   help="policy file (default: policy.json next to the CLI)")
    t.add_argument("--json", action="store_true")
    s = sub.add_parser("status", help="current session summary")
    s.add_argument("--json", action="store_true")
    sc = sub.add_parser("shadow-check",
                        help="measure a transcript against the shadow sim")
    sc.add_argument("transcript")
    sc.add_argument("--json", action="store_true")
    ap = sub.add_parser("live-autopilot",
                        help="let a strategy play, under policy guardrails")
    ap.add_argument("--strategy", default="greedy",
                    choices=["greedy", "bfs", "random"])
    ap.add_argument("--strategy-file",
                    help="Python file defining choose(observe, legal_actions)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--max-steps", type=int, default=200)
    ap.add_argument("--max-seconds", type=float, default=60)
    ap.add_argument("--target-score", type=float, default=None)
    ap.add_argument("--stall-limit", type=int, default=5)
    ap.add_argument("--bfs-max-nodes", type=int, default=20000)
    ap.add_argument("--policy", default=None,
                    help="policy file (default: policy.json next to the CLI)")
    ap.add_argument("--dry-run", action="store_true",
                    help="shadow simulation only: no live action is sent")
    ap.add_argument("--json", action="store_true")
    ob = sub.add_parser("live-observe",
                        help="serve the read-only observer dashboard")
    ob.add_argument("--port", type=int, default=8899)
    return p


def main(argv=None):
    global POLICY
    ns = build_parser().parse_args(argv)
    if not ns.command:
        build_parser().print_help()
        return 2
    if getattr(ns, "policy", None):
        POLICY = load_policy(ns.policy)
    try:
        if ns.command == "observe":
            emit("observe", fetch(), ns.json)
            return 0
        if ns.command == "actions":
            emit("actions", legal(), ns.json)
            return 0
        if ns.command == "act":
            emit("act", do_act(ns.action), ns.json)
            return 0
        if ns.command == "status":
            sid = current_session_id()
            acts = 0
            if sid:
                acts = len([l for l in read_journal(sid)
                            if l.get("type") == "act"])
            emit("status", {"session": sid, "acts": acts, "label": LABEL,
                            "verified": False, "observe": fetch()}, ns.json)
            return 0
        if ns.command == "shadow-check":
            emit("shadow-check", run_shadow_check(ns.transcript), ns.json)
            return 0
        if ns.command == "live-autopilot":
            return run_live_autopilot(ns)
        if ns.command == "live-observe":
            return run_observer(ns.port)
    except PolicyError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
'''


def skill_md_live(name, adapter_name, meta, shadow_name):
    lines = [
        "# %s" % name,
        "",
        "Live CLI for the game adapter `%s` (clivane live path)." % adapter_name,
    ]
    if meta:
        lines.append("Game: **%s** - transport: %s."
                     % (meta.get("game", "?"), meta.get("transport", "?")))
    lines += [
        "",
        "## Honest label",
        "",
        "Everything this package records is **live-recorded**. The game's",
        "state and RNG live on a remote server, so no result here is - or",
        "can be - replay-verified; `verified` is always `false` on this",
        "path. Replay-verified results exist only in the deterministic",
        "game path and in this package's shadow simulator.",
        "",
        "## Commands",
        "",
        "- `observe [--json]` - fetch and print the current live state.",
        "- `actions [--json]` - actions legal in the current state.",
        "- `act <action> [--json]` - perform one live action. Every act",
        "  is appended to the session journal (see below). Policy-blocked",
        "  actions are refused before anything is sent.",
        "- `status [--json]` - current session id, journaled act count,",
        "  label and the live observation.",
        "- `shadow-check <transcript.jsonl> [--json]` - replay the",
        "  transcript's actions in the shadow simulator and report the",
        "  divergence (`match_rate`, mismatched fields) - a measurement,",
        "  reported as-is, never a verification.",
        "- `live-autopilot [--strategy greedy|bfs|random]",
        "  [--strategy-file f.py] [--seed N] [--max-steps N]",
        "  [--max-seconds N] [--target-score N] [--dry-run]` - let a",
        "  strategy play under the policy guardrails (see below).",
        "  `--dry-run` plays in the shadow simulator only: no live",
        "  action is sent, nothing is journaled.",
        "- `live-observe [--port N]` - serve the read-only observer",
        "  dashboard (sessions, budget use, anomalies, recent actions).",
        "  GET only; every other method is refused 405.",
        "",
        "## Session journal",
        "",
        "Sessions live in `sessions/` next to the CLI, one JSONL file",
        "per session (`live-1.jsonl`, ...). Lines: `session_start`,",
        "one `act` line per action (`ts`, `state_before`, `action`,",
        "`result`, `state_after`) and a `summary` line when an",
        "autopilot run ends. The journal is the audit trail for a game",
        "whose server you do not control.",
        "",
        "## Policy guardrails (policy.json, fail-closed)",
        "",
        "- `blocked_actions` - never sent, by the CLI or the autopilot.",
        "  There is no override flag.",
        "- `approval_actions` - human-only; the autopilot stops with",
        "  `needs_human` instead of sending them.",
        "- `budgets.max_actions_per_session`, `budgets.max_actions_per_hour`",
        "  (counted from the journal), `budgets.resource_caps`",
        "  (`{field: max_spend}`, spend read from the state, the next",
        "  step's cost estimated on the shadow simulator).",
        "- `allowed_hours` - optional `[start, end]` local-time window.",
        "- Kill-switch: a file named `STOP` in `sessions/` halts the",
        "  autopilot at the next step (`kill_switch`).",
        "",
        "Autopilot stop reasons: `done | target_reached | step_budget |`",
        "`time_budget | stall | illegal_action | error | budget_actions |`",
        "`budget_resource | blocked | needs_human | kill_switch |`",
        "`rate_window`.",
        "",
    ]
    if shadow_name:
        lines += [
            "## Shadow simulator",
            "",
            "`%s` is a deterministic model of the live game, written as" % shadow_name,
            "an ordinary clivane game-contract module. Strategies are",
            "tested against it (dry-run, greedy/bfs lookahead), and",
            "`shadow-check` measures how far the live game diverges",
            "from the model.",
            "",
        ]
    return "\n".join(lines)


def _copy_unless_same(src, dst):
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copyfile(src, dst)


def cmd_live(adapter_path, out_dir, shadow_path=None, policy_path=None,
             probe=False, mock_url=None):
    if not os.path.isfile(adapter_path):
        raise SystemExit("adapter not found: %s" % adapter_path)
    info = validate_live_adapter(adapter_path, probe=probe,
                                 mock_url=mock_url)
    adapter_name = os.path.splitext(os.path.basename(adapter_path))[0]
    meta = info.get("meta") or {}
    if meta.get("name"):
        name = kebab(str(meta["name"]))
    else:
        name = adapter_name.replace("_", "-") + "-live"
    entry = name.replace("-", "_") + ".py"
    shadow_mod = ""
    shadow_file = "absent.py"
    if shadow_path:
        if not os.path.isfile(shadow_path):
            raise SystemExit("shadow module not found: %s" % shadow_path)
        validate_game_contract(shadow_path)
        shadow_mod = os.path.splitext(os.path.basename(shadow_path))[0]
        shadow_file = os.path.basename(shadow_path)
    os.makedirs(out_dir, exist_ok=True)
    _copy_unless_same(adapter_path,
                      os.path.join(out_dir, os.path.basename(adapter_path)))
    if shadow_path:
        _copy_unless_same(shadow_path, os.path.join(out_dir, shadow_file))
    if policy_path:
        if not os.path.isfile(policy_path):
            raise SystemExit("policy not found: %s" % policy_path)
        _copy_unless_same(policy_path, os.path.join(out_dir, "policy.json"))
    elif not os.path.isfile(os.path.join(out_dir, "policy.json")):
        write_file(os.path.join(out_dir, "policy.json"),
                   json.dumps({"blocked_actions": [],
                               "approval_actions": [],
                               "budgets": {}}, indent=2) + "\n")
    cli_text = LIVE_HEADER + LIVE_COMMON + AUTOPILOT_COMMON + LIVE_MAIN
    cli_text = (cli_text.replace("@NAME@", name)
                .replace("@ADAPTER@", adapter_name)
                .replace("@SHADOWFILE@", shadow_file)
                .replace("@SHADOWMOD@", shadow_mod or "absent"))
    write_file(os.path.join(out_dir, entry), cli_text)
    write_file(os.path.join(out_dir, "SKILL.md"),
               skill_md_live(name, adapter_name, meta,
                             os.path.basename(shadow_path) if shadow_path
                             else None))
    write_file(os.path.join(out_dir, "test_smoke.py"),
               SMOKE_TEMPLATE.replace("@ENTRY@", entry))
    manifest = {
        "name": name,
        "path": "live",
        "kind": "live",
        "entry": entry,
        "source": os.path.basename(adapter_path),
        "commands": list(LIVE_COMMANDS),
        "label": LIVE_LABEL,
        "tier": "Bronze",
    }
    if shadow_path:
        manifest["shadow"] = shadow_file
    write_file(os.path.join(out_dir, "manifest.json"),
               json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


# --------------------------------------------------------------------------
# LIVE CONFORMANCE - `clivane live-check` (phase 5)
#
# An offline suite that grades a live adapter against its MOCK server
# (never the real game): contract, JSON-able state, consistent
# actions, a correctly written journal, and policy enforcement on a
# dry-run-style probe. The per-check evidence is what tiers are
# computed from - the tier string itself is never trusted.
#
#   Gold   - every check passes (incl. policy + meta)
#   Silver - contract + state + actions + journal
#   Bronze - contract + state
# --------------------------------------------------------------------------

def live_tier_from_checks(checks):
    if checks.get("contract") and checks.get("state_jsonable"):
        if (checks.get("actions_consistent") and checks.get("journal")
                and checks.get("policy") and checks.get("meta")):
            return "Gold"
        if checks.get("actions_consistent") and checks.get("journal"):
            return "Silver"
        return "Bronze"
    return "Bronze"


def live_tier(pkg_dir):
    """Tier for a live package, recomputed from conformance evidence."""
    path = os.path.join(pkg_dir, "conformance.json")
    if not os.path.isfile(path):
        return "Bronze"
    try:
        report = read_json(path)
    except Exception:
        return "Bronze"
    return live_tier_from_checks(report.get("checks") or {})


def cmd_live_check(adapter_path, mock_path, package_dir=None):
    """Run the conformance suite; return the report dict."""
    if not os.path.isfile(adapter_path):
        raise SystemExit("adapter not found: %s" % adapter_path)
    if not os.path.isfile(mock_path):
        raise SystemExit("mock server not found: %s" % mock_path)
    checks = {}
    tmp = tempfile.mkdtemp(prefix="clivane-livecheck-")
    port = free_port()
    base = "http://127.0.0.1:%d" % port
    env = dict(os.environ)
    env["CLIVANE_PORT"] = str(port)
    env["CLIVANE_REMOTE"] = base
    proc = subprocess.Popen([sys.executable, os.path.abspath(mock_path)],
                            env=env,
                            cwd=os.path.dirname(os.path.abspath(mock_path)),
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    old_remote = os.environ.get("CLIVANE_REMOTE")
    os.environ["CLIVANE_REMOTE"] = base
    try:
        deadline = time.time() + 10
        module = None
        while True:
            try:
                module = load_module_from_path(adapter_path)
                module.fetch_state()
                break
            except Exception:
                module = None
            if proc.poll() is not None:
                raise SystemExit("mock server exited during startup")
            if time.time() > deadline:
                raise SystemExit("mock server did not come up at %s" % base)
            time.sleep(0.05)
        validate_live_adapter(adapter_path)
        checks["contract"] = True
        state = module.fetch_state()
        try:
            json.dumps(state)
            checks["state_jsonable"] = isinstance(state, dict)
        except Exception:
            checks["state_jsonable"] = False
        first = module.list_actions(state)
        second = module.list_actions(module.fetch_state())
        checks["actions_consistent"] = (
            isinstance(first, list) and bool(first)
            and all(isinstance(a, str) for a in first)
            and set(first) == set(second))
        meta = getattr(module, "meta", None)
        m = meta() if callable(meta) else None
        checks["meta"] = (isinstance(m, dict) and "name" in m
                          and "game" in m and "transport" in m)
        # journal + policy, exercised through the generated CLI
        fake_blocked = "clivane-conformance-blocked"
        policy = {"blocked_actions": [fake_blocked],
                  "approval_actions": [], "budgets": {}}
        policy_path = os.path.join(tmp, "policy.json")
        write_file(policy_path, json.dumps(policy, indent=2) + "\n")
        pkg = os.path.join(tmp, "pkg")
        manifest = cmd_live(adapter_path, pkg, policy_path=policy_path)
        entry = manifest["entry"]
        cli_env = {"CLIVANE_REMOTE": base}
        before = module.fetch_state()
        code, _out, err = run_cli(pkg, entry, ["act", fake_blocked, "--json"],
                                  env=cli_env)
        checks["policy"] = (code == 1 and "policy" in err
                            and module.fetch_state() == before)
        legal_now = module.list_actions(module.fetch_state())
        code, _out, _err = run_cli(pkg, entry,
                                   ["act", legal_now[0], "--json"],
                                   env=cli_env)
        journal_ok = False
        sessions_dir = os.path.join(pkg, "sessions")
        if code == 0 and os.path.isdir(sessions_dir):
            for fname in os.listdir(sessions_dir):
                if fname.endswith(".jsonl"):
                    with open(os.path.join(sessions_dir, fname),
                              encoding="utf-8") as fh:
                        lines = [json.loads(l) for l in fh if l.strip()]
                    kinds = [l.get("type") for l in lines]
                    act_lines = [l for l in lines if l.get("type") == "act"]
                    if ("session_start" in kinds and act_lines
                            and "ts" in act_lines[0]
                            and "state_before" in act_lines[0]
                            and "state_after" in act_lines[0]):
                        journal_ok = True
        checks["journal"] = journal_ok
        # autopilot must stop `blocked` on the same fake action
        strat = os.path.join(tmp, "strat_blocked.py")
        write_file(strat, "def choose(observe, legal_actions):\n"
                          "    return %r\n" % fake_blocked)
        code, out, _err = run_cli(
            pkg, entry,
            ["live-autopilot", "--strategy-file", strat, "--json"],
            env=cli_env)
        try:
            res = json.loads(out)
            checks["policy"] = bool(checks.get("policy")) and \
                res.get("stop_reason") == "blocked" and res.get("steps") == 0
        except Exception:
            checks["policy"] = False
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        if old_remote is None:
            os.environ.pop("CLIVANE_REMOTE", None)
        else:
            os.environ["CLIVANE_REMOTE"] = old_remote
        shutil.rmtree(tmp, ignore_errors=True)
    report = {"adapter": os.path.basename(adapter_path),
              "checks": checks,
              "tier": live_tier_from_checks(checks),
              "label": LIVE_LABEL,
              "verified": False}
    if package_dir:
        write_file(os.path.join(package_dir, "conformance.json"),
                   json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


# --------------------------------------------------------------------------
# VERIFY - Trace-Seal: golden traces, replayed and compared
# --------------------------------------------------------------------------

def probe_calls(pkg_dir, manifest):
    calls = [["--help"]]
    for cmd in manifest.get("commands", []):
        calls.append([cmd, "--help"])
    demo_path = os.path.join(pkg_dir, "demo_calls.json")
    if os.path.isfile(demo_path):
        calls += [list(c) for c in read_json(demo_path)]
    return calls


def normalise(text, pkg_dir):
    out = []
    for line in (text or "").replace(pkg_dir, "<PKG>").splitlines():
        out.append(line.rstrip())
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


@contextlib.contextmanager
def pkg_runtime(work, manifest):
    """Runtime context for probing a package copy.

    Plain packages need nothing. Packages whose manifest names a
    `server` file (`serve` packages, fullstack game arenas) get that
    server started on an ephemeral localhost port, and the client calls
    receive CLIVANE_REMOTE / CLIVANE_PORT pointing at it - so golden
    traces cover real client<->server traffic while argv stays stable.
    Yields the extra environment dict for run_cli calls.
    """
    server_file = manifest.get("server")
    if not server_file:
        yield {}
        return
    port = free_port()
    env = {"CLIVANE_PORT": str(port),
           "CLIVANE_REMOTE": "http://127.0.0.1:%d" % port}
    full_env = dict(os.environ)
    full_env.update(env)
    proc = subprocess.Popen(
        [sys.executable, server_file],
        cwd=work, env=full_env, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 10
        while True:
            try:
                status, _ = http_json("GET", env["CLIVANE_REMOTE"] + "/health",
                                      timeout=1)
                if status == 200:
                    break
            except Exception:
                pass
            if proc.poll() is not None:
                raise RuntimeError("generated server exited during startup")
            if time.time() > deadline:
                raise RuntimeError("generated server did not become healthy")
            time.sleep(0.05)
        yield env
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def collect_trace(pkg_dir, manifest):
    """Run probe calls in a fresh temp copy; return the trace dict."""
    tmp = tempfile.mkdtemp(prefix="clivane-seal-")
    work = os.path.join(tmp, "pkg")
    shutil.copytree(pkg_dir, work)
    records = []
    try:
        with pkg_runtime(work, manifest) as env:
            for argv in probe_calls(work, manifest):
                code, out, err = run_cli(work, manifest["entry"], argv, env=env)
                records.append({
                    "argv": argv,
                    "exit_code": code,
                    "stdout": normalise(out, work),
                    "stderr_empty": not err.strip(),
                })
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"package": manifest["name"], "seal_version": SEAL_VERSION, "calls": records}


def verify_package(pkg_dir, record=False):
    """Return (status, detail). status in SEALED / PASS / FAIL."""
    manifest = pkg_manifest(pkg_dir)
    trace_path = os.path.join(pkg_dir, "traces.json")
    if record or not os.path.isfile(trace_path):
        trace = collect_trace(pkg_dir, manifest)
        write_file(trace_path, json.dumps(trace, indent=2, sort_keys=True) + "\n")
        bad = [r for r in trace["calls"] if r["exit_code"] != 0]
        if bad:
            return "FAIL", "seal recorded but %d probe call(s) failed" % len(bad)
        return "SEALED", "%d calls recorded to traces.json" % len(trace["calls"])
    expected = read_json(trace_path)
    actual = collect_trace(pkg_dir, manifest)
    if actual["calls"] == expected["calls"]:
        return "PASS", "%d calls match the golden trace" % len(actual["calls"])
    diffs = []
    exp = {tuple(r["argv"]): r for r in expected["calls"]}
    act = {tuple(r["argv"]): r for r in actual["calls"]}
    for key in sorted(set(exp) | set(act)):
        if exp.get(key) != act.get(key):
            diffs.append(" ".join(key))
    return "FAIL", "trace mismatch on: %s" % "; ".join(diffs[:5])


def smoke_ok(pkg_dir, manifest):
    tmp = tempfile.mkdtemp(prefix="clivane-smoke-")
    work = os.path.join(tmp, "pkg")
    try:
        shutil.copytree(pkg_dir, work)
        with pkg_runtime(work, manifest) as env:
            for argv in probe_calls(work, manifest):
                code, _, _ = run_cli(work, manifest["entry"], argv, env=env)
                if code != 0:
                    return False
        return True
    except Exception:
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def tier_of(pkg_dir):
    manifest = pkg_manifest(pkg_dir)
    if manifest.get("path") == "live":
        # Live packages are graded by the conformance suite's recorded
        # evidence (conformance.json), recomputed from its checks -
        # a live game cannot earn a tier from replay, by design.
        return live_tier(pkg_dir)
    if not os.path.isfile(os.path.join(pkg_dir, "traces.json")):
        return "Silver" if smoke_ok(pkg_dir, manifest) else "Bronze"
    status, _ = verify_package(pkg_dir)
    if status == "PASS":
        return "Gold"
    return "Silver" if smoke_ok(pkg_dir, manifest) else "Bronze"


# --------------------------------------------------------------------------
# REGISTRY - scan a parent dir, write registry.json + INDEX.md
# --------------------------------------------------------------------------

def _registry_entry(pkg_dir, dir_label):
    manifest = pkg_manifest(pkg_dir)
    return {
        "name": manifest["name"],
        "dir": dir_label,
        "path": manifest["path"],
        "kind": manifest.get("kind", "deterministic"),
        "commands": len(manifest.get("commands", [])),
        "command_list": manifest.get("commands", []),
        "tier": tier_of(pkg_dir),
    }


def cmd_registry(parent):
    entries = []
    for child in sorted(os.listdir(parent)):
        pkg_dir = os.path.join(parent, child)
        if not os.path.isdir(pkg_dir):
            continue
        if not os.path.isfile(os.path.join(pkg_dir, "manifest.json")):
            continue
        manifest = pkg_manifest(pkg_dir)
        entries.append(_registry_entry(pkg_dir, child))
        # Dual-mode packages (live path): the bundled shadow simulator
        # is a deterministic game package in its own right, registered
        # alongside - one game, two faces, one folder.
        shadow_rel = manifest.get("shadow_package")
        if shadow_rel:
            sub = os.path.join(pkg_dir, shadow_rel)
            if os.path.isfile(os.path.join(sub, "manifest.json")):
                entries.append(_registry_entry(sub, child + "/" + shadow_rel))
    entries.sort(key=lambda e: e["name"])
    write_file(os.path.join(parent, "registry.json"),
               json.dumps({"packages": entries}, indent=2, sort_keys=True) + "\n")
    lines = [
        "# clivane registry",
        "",
        "Generated by `clivane registry`. Tiers: Bronze = generated,",
        "Silver = smoke tests pass, Gold = Trace-Seal verified.",
        "",
        "| Package | Path | Commands | Tier |",
        "|---|---|---|---|",
    ]
    for e in entries:
        lines.append("| %s | %s | %d | %s |" % (e["name"], e["path"], e["commands"], e["tier"]))
    lines.append("")
    write_file(os.path.join(parent, "INDEX.md"), "\n".join(lines))
    return entries


# --------------------------------------------------------------------------
# LIST
# --------------------------------------------------------------------------

def cmd_list(pkg_dir):
    manifest = pkg_manifest(pkg_dir)
    files = []
    for root, dirs, names in os.walk(pkg_dir):
        dirs.sort()
        for n in sorted(names):
            files.append(os.path.relpath(os.path.join(root, n), pkg_dir))
    return manifest, files


# --------------------------------------------------------------------------
# main CLI
# --------------------------------------------------------------------------

def build_main_parser():
    p = argparse.ArgumentParser(prog="clivane",
                                description="Make ANY software agent-native - deterministically.")
    sub = p.add_subparsers(dest="command", required=True)

    f = sub.add_parser("forge", help="AST-analyse a Python module -> agent CLI")
    f.add_argument("module")
    f.add_argument("--out", required=True)

    m = sub.add_parser("mirror", help="wrap an existing CLI tool -> JSON envelope wrapper")
    m.add_argument("tool")
    m.add_argument("--out", required=True)

    o = sub.add_parser("openapi", help="OpenAPI 3 JSON spec -> per-operation CLI")
    o.add_argument("spec")
    o.add_argument("--out", required=True)

    g = sub.add_parser("game", help="game module (clivane game contract) -> episode CLI; --fullstack -> arena server + web UI + remote client")
    g.add_argument("module")
    g.add_argument("--out", required=True)
    g.add_argument("--fullstack", action="store_true",
                   help="generate the fullstack arena package instead of the local CLI")

    s = sub.add_parser("serve", help="Python module -> HTTP JSON server + remote client")
    s.add_argument("module")
    s.add_argument("--out", required=True)

    pb = sub.add_parser("probe", help="introspect a live server -> endpoint map + client")
    pb.add_argument("base_url")
    pb.add_argument("--out", required=True)

    lv = sub.add_parser("live", help="live game adapter -> live CLI (journal, policy guardrails, shadow sim, observer); results labelled live-recorded, never verified")
    lv.add_argument("adapter")
    lv.add_argument("--out", required=True)
    lv.add_argument("--shadow", default=None,
                    help="deterministic shadow simulator module (game contract)")
    lv.add_argument("--policy", default=None,
                    help="policy.json to bundle (default: empty policy)")
    lv.add_argument("--probe", action="store_true",
                    help="probe the adapter live once (fetch_state/list_actions) instead of static validation only - point it at a mock/staging server")
    lv.add_argument("--mock-url", default=None,
                    help="base URL used as CLIVANE_REMOTE for --probe")

    lc = sub.add_parser("live-check", help="conformance suite for a live adapter, offline against its mock server -> Bronze/Silver/Gold")
    lc.add_argument("adapter")
    lc.add_argument("--mock", required=True, help="mock game server script")
    lc.add_argument("--package", default=None,
                    help="also stamp conformance.json into this package dir")

    v = sub.add_parser("verify", help="Trace-Seal: record or replay golden traces")
    v.add_argument("dir")
    v.add_argument("--record", action="store_true", help="force re-recording the seal")

    r = sub.add_parser("registry", help="scan packages, write registry.json + INDEX.md")
    r.add_argument("--dir", required=True)

    li = sub.add_parser("list", help="show a package's manifest and files")
    li.add_argument("dir")

    sub.add_parser("selftest", help="run the clivane self-test suite")
    return p


def main(argv=None):
    ns = build_main_parser().parse_args(argv)
    if ns.command == "forge":
        manifest = cmd_forge(ns.module, ns.out)
        print("forged %s -> %s (%d commands)" % (manifest["name"], ns.out, len(manifest["commands"])))
        return 0
    if ns.command == "mirror":
        manifest = cmd_mirror(ns.tool, ns.out)
        print("mirrored %s -> %s (%d subcommands detected)" % (manifest["name"], ns.out, len(manifest["commands"])))
        return 0
    if ns.command == "openapi":
        manifest = cmd_openapi(ns.spec, ns.out)
        print("generated %s -> %s (%d operations)" % (manifest["name"], ns.out, len(manifest["commands"])))
        return 0
    if ns.command == "game":
        manifest = cmd_game(ns.module, ns.out, fullstack=ns.fullstack)
        if manifest.get("mode") == "fullstack":
            print("generated fullstack arena %s -> %s (contract OK, game_server.py + web UI + %s, %d commands)"
                  % (manifest["name"], ns.out, manifest["entry"], len(manifest["commands"])))
        else:
            print("generated game CLI %s -> %s (contract OK, %d commands)"
                  % (manifest["name"], ns.out, len(manifest["commands"])))
        return 0
    if ns.command == "serve":
        manifest = cmd_serve(ns.module, ns.out)
        print("generated server package %s -> %s (server.py + %s, %d commands)"
              % (manifest["name"], ns.out, manifest["entry"], len(manifest["commands"])))
        return 0
    if ns.command == "probe":
        manifest, info = cmd_probe(ns.base_url, ns.out)
        print("probed %s -> %s (%d run commands, %d openapi operations)"
              % (info["base_url"], ns.out,
                 len(info["commands"]), len(info["operations"])))
        return 0
    if ns.command == "live":
        manifest = cmd_live(ns.adapter, ns.out, shadow_path=ns.shadow,
                            policy_path=ns.policy, probe=ns.probe,
                            mock_url=ns.mock_url)
        print("generated live package %s -> %s (contract OK, %d commands, "
              "label: live-recorded)"
              % (manifest["name"], ns.out, len(manifest["commands"])))
        return 0
    if ns.command == "live-check":
        report = cmd_live_check(ns.adapter, ns.mock, package_dir=ns.package)
        for key in sorted(report["checks"]):
            print("  %-18s %s" % (key, "ok" if report["checks"][key] else "FAIL"))
        print("live-check %s: tier %s (label: live-recorded)"
              % (report["adapter"], report["tier"]))
        return 0 if report["checks"].get("contract") else 1
    if ns.command == "verify":
        status, detail = verify_package(ns.dir, record=ns.record)
        print("verify %s: %s - %s" % (ns.dir, status, detail))
        return 0 if status in ("PASS", "SEALED") else 1
    if ns.command == "registry":
        entries = cmd_registry(ns.dir)
        for e in entries:
            print("%-24s %-8s %3d commands  %s" % (e["name"], e["path"], e["commands"], e["tier"]))
        print("registry written: %d packages" % len(entries))
        return 0
    if ns.command == "list":
        manifest, files = cmd_list(ns.dir)
        print("%s (%s) entry=%s tier-recorded=%s" % (manifest["name"], manifest["path"],
                                                     manifest["entry"], manifest.get("tier")))
        print("commands: %s" % ", ".join(manifest.get("commands", [])))
        for f in files:
            print("  " + f)
        return 0
    if ns.command == "selftest":
        here = os.path.dirname(os.path.abspath(__file__))
        return subprocess.run([sys.executable, os.path.join(here, "selftest.py")]).returncode
    return 2


if __name__ == "__main__":
    sys.exit(main())
