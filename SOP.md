# clivane SOP — the methodology

This document is the single source of truth for how clivane turns software
into agent-native CLIs. Every command in `clivane.py` implements a section
of this SOP; if code and SOP disagree, the SOP wins and the code is a bug.

## Principles

1. **Deterministic first.** Generation is a pure function of the input:
   same module / tool / spec in, byte-identical package out. No LLM, no
   API key, no network, no timestamps in generated files.
2. **Interface to software, not a replacement.** A generated CLI calls the
   real function, forwards to the real tool, or speaks to the real API.
   clivane never re-implements the target's behaviour.
3. **Proof over promises.** A package is only as good as its evidence:
   smoke tests for Silver, a replayed golden trace (Trace-Seal) for Gold.
4. **Agent-native output.** Every command supports `--json`; `--help` is
   the discoverable documentation; exit codes are meaningful (0 = ok).
5. **Small and inspectable.** One generator file, one file per generated
   CLI, plain JSON manifests. Anything generated can be read in minutes.

## Path 1 — forge (new: deterministic, no agent required)

Use when the target is a Python module.

1. Parse the module with `ast`. Collect public top-level functions
   (names not starting with `_`).
2. For each function record: name, docstring summary, parameters with
   type annotations (`int`, `float`, `bool`, `str`, `list`, `dict`) and
   literal defaults. Non-literal defaults become optional `None`.
3. Emit one subcommand per function, kebab-cased. Parameters become
   `--flags`; list/dict values are parsed as JSON.
4. Emit three run modes in the generated CLI:
   - subcommand mode for scripts and pipelines,
   - `--json` envelope mode: `{"command": ..., "result": ...}`,
   - a stateful REPL when invoked with no arguments (module state and
     open resources persist across commands in the session).
5. Copy the target module next to the generated CLI so the package is
   self-contained, and emit `SKILL.md`, `test_smoke.py`, `manifest.json`.

Limits (by design): classes, decorators with side effects, and functions
requiring live objects are out of scope — wrap them in plain functions.

## Path 2 — mirror (new: wrap what already exists)

Use when the target already ships a CLI.

1. Probe `<tool> --help`; extract subcommands from a `Commands:` section
   (fallback: indented `name  description` lines). Probe each detected
   subcommand's `--help` for the skill map (best effort, capped).
2. Generate a wrapper that forwards argv untouched to the real tool.
3. `--json` anywhere in argv is intercepted by the wrapper (never
   forwarded) and switches output to the envelope:
   `{"tool", "argv", "exit_code", "stdout", "stderr"}`.
4. Exit code is always the wrapped tool's exit code.
5. `SKILL.md` carries the probed command map so an agent can plan calls
   without probing the tool itself.

## Path 3 — openapi (new: spec in, CLI out)

Use when the target is an HTTP API with an OpenAPI 3 JSON document.

1. Walk `paths` × methods; one subcommand per `operationId` (kebab-cased).
2. `path` parameters are substituted (URL-quoted); `query` parameters
   become the query string; POST/PUT/PATCH accept `--body '<json>'`.
3. Base URL: `--base-url`, else `<NAME>_BASE_URL` env var, else the
   spec's first `servers[].url`. Requests use `urllib` only.
4. `--json` wraps responses as `{"status": int, "body": ...}`; HTTP
   status >= 400 yields exit code 1.

## Trace-Seal — the proof system

1. Probe set = `--help`, every command's `--help`, plus the argv arrays
   in an optional `demo_calls.json` shipped with the package.
2. **Seal:** run the probe set in a *fresh temporary copy* of the package
   (state starts empty, so traces are reproducible), normalise output
   (package path -> `<PKG>`, trailing whitespace stripped), and record
   `{argv, exit_code, stdout, stderr_empty}` per call into `traces.json`.
3. **Verify:** rebuild the trace the same way and compare exactly.
   Any drift in exit code or output is a FAIL with the differing calls
   named. Editing a golden trace by hand is detectable the same way —
   the seal only passes against real behaviour.
4. Re-seal deliberately with `clivane verify <dir> --record` after an
   intentional behaviour change, and review the trace diff like code.

## Quality tiers (registry)

| Tier   | Meaning                                             |
|--------|-----------------------------------------------------|
| Bronze | Package generated; manifest present.                |
| Silver | Bronze + every probe call exits 0 (smoke level).    |
| Gold   | Silver + a Trace-Seal on file that replays PASS.   |

`clivane registry --dir <parent>` recomputes tiers live (it replays
seals; it never trusts the tier string stored in a manifest) and writes
`registry.json` plus a human-readable `INDEX.md` table.

## Adding a package by hand (checklist)

- [ ] Generate with one of the three paths.
- [ ] Add `demo_calls.json` exercising the main flows.
- [ ] `clivane verify <dir>` → SEALED, run again → PASS.
- [ ] `clivane registry --dir <parent>` shows the tier you expect.
- [ ] Commit the package *including* `traces.json` — the seal is the
      evidence; a package without it is Bronze by definition.
