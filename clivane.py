#!/usr/bin/env python3
"""clivane - make ANY software agent-native, deterministically.

Three generation paths plus a proof system, all in pure Python stdlib.
No LLM, no API key, no network required for forge / mirror / verify.

Paths:
  forge   - static AST analysis of a Python module -> full agent CLI
  mirror  - wrap an existing CLI tool -> JSON-envelope wrapper CLI
  openapi - OpenAPI 3 JSON spec -> per-operation CLI
  game    - a Python game obeying the clivane game contract -> episode CLI
  serve   - a Python module -> HTTP JSON server + remote client CLI
  probe   - introspect a live server -> endpoint map + generic client CLI

Proof:
  verify  - Trace-Seal: record golden traces, replay them, PASS/FAIL
  registry- scan generated packages, assign Bronze/Silver/Gold tiers

See SOP.md for the full methodology.
"""

import argparse
import ast
import contextlib
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
        "",
        "## Conventions",
        "",
        "- `--json` wraps results as `{\"command\": ..., \"result\": ...}`.",
        "- Illegal actions and acting after `done` exit 1 with an error.",
        "- A replay that diverges exits 1 and reports `\"verified\": false`.",
        "",
    ])


def cmd_game(module_path, out_dir):
    if not os.path.isfile(module_path):
        raise SystemExit("module not found: %s" % module_path)
    validate_game_contract(module_path)
    mod_name = os.path.splitext(os.path.basename(module_path))[0]
    name = mod_name.replace("_", "-") + "-cli"
    entry = name.replace("-", "_") + ".py"
    os.makedirs(out_dir, exist_ok=True)
    shutil.copyfile(module_path, os.path.join(out_dir, os.path.basename(module_path)))
    write_file(os.path.join(out_dir, entry),
               GAME_TEMPLATE.replace("@NAME@", name).replace("@MOD@", mod_name))
    write_file(os.path.join(out_dir, "SKILL.md"), skill_md_game(name, mod_name))
    write_file(os.path.join(out_dir, "test_smoke.py"), SMOKE_TEMPLATE.replace("@ENTRY@", entry))
    manifest = {
        "name": name,
        "path": "game",
        "entry": entry,
        "source": os.path.basename(module_path),
        "commands": ["act", "actions", "new", "observe", "replay", "status"],
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

    Plain packages need nothing. `serve` packages get their generated
    server started on an ephemeral localhost port, and the client calls
    receive CLIVANE_REMOTE / CLIVANE_PORT pointing at it - so golden
    traces cover real client<->server traffic while argv stays stable.
    Yields the extra environment dict for run_cli calls.
    """
    if manifest.get("path") != "serve":
        yield {}
        return
    port = free_port()
    env = {"CLIVANE_PORT": str(port),
           "CLIVANE_REMOTE": "http://127.0.0.1:%d" % port}
    full_env = dict(os.environ)
    full_env.update(env)
    proc = subprocess.Popen(
        [sys.executable, manifest.get("server", "server.py")],
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
    if not os.path.isfile(os.path.join(pkg_dir, "traces.json")):
        return "Silver" if smoke_ok(pkg_dir, manifest) else "Bronze"
    status, _ = verify_package(pkg_dir)
    if status == "PASS":
        return "Gold"
    return "Silver" if smoke_ok(pkg_dir, manifest) else "Bronze"


# --------------------------------------------------------------------------
# REGISTRY - scan a parent dir, write registry.json + INDEX.md
# --------------------------------------------------------------------------

def cmd_registry(parent):
    entries = []
    for child in sorted(os.listdir(parent)):
        pkg_dir = os.path.join(parent, child)
        if not os.path.isdir(pkg_dir):
            continue
        if not os.path.isfile(os.path.join(pkg_dir, "manifest.json")):
            continue
        manifest = pkg_manifest(pkg_dir)
        entries.append({
            "name": manifest["name"],
            "dir": child,
            "path": manifest["path"],
            "commands": len(manifest.get("commands", [])),
            "command_list": manifest.get("commands", []),
            "tier": tier_of(pkg_dir),
        })
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

    g = sub.add_parser("game", help="game module (clivane game contract) -> episode CLI")
    g.add_argument("module")
    g.add_argument("--out", required=True)

    s = sub.add_parser("serve", help="Python module -> HTTP JSON server + remote client")
    s.add_argument("module")
    s.add_argument("--out", required=True)

    pb = sub.add_parser("probe", help="introspect a live server -> endpoint map + client")
    pb.add_argument("base_url")
    pb.add_argument("--out", required=True)

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
        manifest = cmd_game(ns.module, ns.out)
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
