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

## Path 4 — game (new: episode CLI for contract games)

Use when the target is a game (or any episodic, seed-deterministic
environment) written as a Python module.

The contract — all four functions, validated by probing at generation
time; a module that violates any point is rejected with a named reason:

| Function | Signature | Rules |
|---|---|---|
| `new_game` | `(seed) -> state` | state is a `dict`, JSON-able |
| `legal_actions` | `(state) -> list[str]` | non-empty while not done |
| `step` | `(state, action) -> (state, reward, done, info)` | reward is a number, done a bool, info a dict |
| `observe` | `(state) -> dict` | JSON-able observation |

Generation emits a session CLI: `new --seed N` starts an episode and
persists it to `session.json` next to the CLI (so separate invocations
share one episode and form a transcript), `observe`, `actions`,
`act <action>` (validated against `legal_actions`, refused after done),
and `status`.

Determinism is the contract's load-bearing wall: the same seed must
produce the same episode, bit for bit. `replay --seed N --actions a,b,c`
re-runs from scratch and verifies — against the recorded session when a
transcript for that seed exists (same actions *and* identical final
state; an altered action FAILs), or against a second identical replay
when it does not. Because replay is an ordinary CLI command, it joins
`demo_calls.json` and the whole episode is Trace-Sealed like any other
package: a game package reaches Gold only if its winning episode
replays identically in a fresh copy.

### Fullstack mode — game arena (v1.2.0)

`clivane game <module> --fullstack` grows the same contract into a
complete arena package (the local episode CLI above remains available
and unchanged). Three layers, generated from the one contract:

1. **Backend — `game_server.py`** (stdlib `http.server` only). A
   session model replaces the single local `session.json`:
   `POST /sessions {seed, player}` creates an episode with a unique id;
   sessions live side by side in memory, each with its own state,
   transcript and done flag — acting on one can never touch another.
   `GET /sessions/<id>/observe`, `GET /sessions/<id>/actions`,
   `POST /sessions/<id>/act` (illegal actions rejected 400 before
   stepping), `GET /sessions`, `GET /health`.
2. **Frontend — `web/index.html`**, served by the same process at
   `GET /`. One static vanilla-JS page, zero dependencies, no CDN:
   renders `observe()` as a coloured grid when the observation carries
   a `grid`, builds the entity grid from `player`/`gems`/`obstacles`
   observations otherwise, and falls back to a JSON card + raw state
   for non-grid games. Legal-action buttons play manually; a
   new-session form takes seed + player name; a watch mode polls any
   session's `observe` once a second so a human can follow an agent
   playing; the leaderboard panel refreshes live.
3. **Agent access — a remote client CLI** with the local game verbs
   (`new / observe / actions / act`) plus `finish` and `leaderboard`,
   speaking HTTP via `--remote` / `$CLIVANE_REMOTE`, exactly like the
   serve path's client. Humans and agents play the *same* sessions
   through the *same* API.

**Score verification (the load-bearing rule).** There is deliberately
no endpoint that accepts a score. `POST /sessions/<id>/finish` takes
no score from the client: the server replays the session's recorded
action transcript from its seed and records a score only when the
replay ends `done` in a state *identical* to the session's live state.
The recorded score is read from the replayed final observation
(`score` field; summed step rewards as fallback), the entry
`{player, seed, actions, score, steps, verified: true}` is appended to
the persistent `scores.json` (survives restarts), and a session can be
finished exactly once. `GET /leaderboard` returns only those verified
entries, best first. A claimed score is therefore never trusted —
only a reproduced one is.

Security follows the server path's rules: bind localhost by default
with a loud warning otherwise, optional `X-Clivane-Token` on POSTs,
and Trace-Seal covers the arena end-to-end (the harness starts
`game_server.py` on an ephemeral port in a fresh copy and replays the
client calls, demo episode included, against it).

### Safe autopilot (v1.3.0)

Both game CLIs — the local episode CLI and the arena remote client —
carry an `autopilot` command that hands a whole episode to a strategy
under guardrails. This is the sanctioned way to let bots play: bounded,
observable, and unable to fake results.

**Strategy contract.** A strategy is
`choose(observe, legal_actions) -> action` — nothing more. It receives
a deep copy of the observation (it cannot mutate engine state) and the
current legal actions. Three deterministic built-ins:

- `greedy` — 1-step lookahead: every legal action is simulated on a
  *deep copy* of the current state (planning never touches the real
  state) and scored `(reward, done, -distance-to-nearest-target)`,
  where the distance term applies only when the observation exposes
  `player` + `gems`/`targets`; legal order is the final deterministic
  tie-break.
- `bfs` — breadth-first search for the shortest plan to a done state
  (a `won` terminal is preferred over a merely-done one), bounded by
  `--bfs-max-nodes` (default 20.000); volatile counters (`steps`,
  `moves_left`) are dropped from the de-duplication key. When the plan
  runs out, greedy takes over.
- `random` — seeded baseline (`random.Random(seed)`), for comparison.

Custom strategies are plain Python files defining `choose()`, loaded
with `--strategy-file`. Built-in `greedy`/`bfs` plan against the game
module: locally that is the real state; in the arena client it is a
*shadow* copy bundled with the package — the server remains
authoritative, every act executes server-side, and if the server's
observation ever diverges from the shadow the run aborts (the game is
not deterministic, so its results cannot be trusted).

**The engine's guardrails** (identical for every strategy, both
drivers). Command shape:

```
autopilot [--strategy greedy|bfs|random] [--strategy-file f.py]
          [--seed N] [--max-steps 200] [--max-seconds 60]
          [--target-score N] [--stall-limit 5] [--dry-run]
```

- *Budgets are hard.* Steps and wall-clock seconds are checked before
  every action; hitting one stops the run with `stop_reason`
  `step_budget` / `time_budget`, the episode is closed cleanly and the
  transcript is still persisted.
- *Stall detection.* If `observe()` is unchanged `--stall-limit`
  times in a row (default 5), the run stops with `stall` — a bot
  spinning in place burns nothing.
- *Illegal actions are never forwarded.* A returned action outside
  `legal_actions` aborts the run immediately (`illegal_action`,
  exit 1); the game never sees it.
- *Exceptions are contained.* A raise in `choose()` or in `step()`
  aborts safely (`error`, exit 1, message in the result): the partial
  transcript is persisted and no half-stepped state is saved — the
  local driver only assigns the new state after `step()` returns.
- *Dry-run is pure.* `--dry-run` plays the full episode in simulation:
  locally it never writes `session.json`; in the arena it never even
  contacts the server (no session, no leaderboard change). The result
  is a projection and reports `verified: false`, `dry_run: true`.
- *Settle by replay only.* After a non-dry-run episode ends `done`,
  the result is verified — locally by replaying the transcript,
  in the arena by the server's `finish` (server replay). Autopilot
  has no path that writes a score: in the arena its session is
  created as player `autopilot-<strategy>`, and the leaderboard entry,
  if any, is produced exclusively by the server's replay.

Result JSON (always printed):
`{strategy, seed, steps, score, done, stop_reason, verified}` with
`stop_reason` ∈ `done | target_reached | step_budget | time_budget |
stall | illegal_action | error`.

**Server-side enforcement.** The arena does not trust autopilot — or
any client — to bound itself. `POST /sessions` accepts `max_steps`;
an act beyond that budget gets 403 and the session is marked
`aborted` permanently (further acts keep getting 403). Acts are also
rate-limited per session to 20/second (429) — loose enough for a
human clicking, tight enough to strangle a wild loop. The autopilot
client passes its own `--max-steps` as the server budget, so its
client-side stop normally fires first; the server cap is the backstop
for rogue clients.

**Ethics / scope.** Autopilot exists for games you own or run locally:
benchmarking agents, testing solvers, filling your own arena's
leaderboard with honestly-earned bot scores. It is not a tool for
flooding third-party services — the rate limit and budgets are
designed so a clivane arena cannot be abused that way either, and
pointing an unbounded bot at someone else's game is out of scope by
design (there is deliberately no unbounded mode).

## Path 5 — server (new: serve + probe, two directions)

### serve — module -> HTTP server + remote client

Use when agents should reach the software over HTTP instead of a local
process. The module is analysed exactly like forge (same specs), then:

1. `server.py` (stdlib `http.server` only) exposes:
   - `GET /health` -> `{"status": "ok", "name": ...}`,
   - `GET /commands` -> the command specs (the manifest, over HTTP),
   - `POST /run` with `{"command": ..., "args": {...}}` -> the forge
     envelope `{"command", "result"}`.
2. A client CLI with the same commands as forge; each call is a
   `POST /run`. Base URL: `--remote`, else `$CLIVANE_REMOTE`, else
   `http://127.0.0.1:8799`.

Security rules (non-negotiable):

- **Bind localhost by default** (`127.0.0.1`). Binding anything else
  prints a loud warning — never expose `0.0.0.0` without a token and a
  reason.
- **`/run` is a dispatcher, not an evaluator.** It only invokes
  functions named in the module manifest, validates the command name,
  rejects unknown args, and coerces values by the spec's types. There
  is no eval, no arbitrary attribute access, no shell.
- **Optional token.** `--token` / `$CLIVANE_TOKEN` makes `/run` require
  the `X-Clivane-Token` header (401 otherwise); clients send it from
  the same env var automatically.

Trace-Seal covers server packages end-to-end: the verify harness starts
the generated server on an ephemeral localhost port inside the fresh
package copy and runs the client probe calls against it, so the golden
trace is real client<->server traffic while argv stays stable.

### probe — live server -> endpoint map + generic client

The reverse direction. `clivane probe <base_url>` introspects a server
that is *already running*: it tries `GET /openapi.json` (operations are
mapped like Path 3), `GET /commands` + `POST /run` (serve-style command
specs), `GET /health` and `GET /`. Whatever answers becomes the
endpoint map in `SKILL.md`, plus a generic client CLI: one subcommand
per discovered run-command (`--args '<json>'`) or operation
(`--params` / `--body '<json>'`). A server that answers none of the
probes is rejected with a clear message.

## Path 6 — live (new in v2.0.0: games on someone else's server)

Use when the target is a game whose state and RNG live on a remote
server — a web game, a Telegram game, a hosted world. The game path's
load-bearing wall (the seed is ours, so replay can prove a result)
does not exist here. This path therefore runs on a different contract,
and its first rule is about language, not code:

**Honest label (non-negotiable).** Everything the live path records —
CLI output, journal lines, autopilot results, registry entries — is
labelled `live-recorded`, and `verified` is always `false`. A live
journal is evidence of what happened; it is never proof that the
result could be reproduced. Replay-verified claims are reserved for
the deterministic game path and for shadow simulators. Any wording,
in code or docs, that blurs this line is a bug.

### The adapter contract

An adapter is a small Python module bridging clivane to the game:

| Function | Signature | Rules |
|---|---|---|
| `fetch_state` | `() -> dict` | the current public state, JSON-able |
| `list_actions` | `(state) -> list[str]` | actions legal in that state |
| `send_action` | `(action, **kw) -> dict` | performs one action, returns its result dict |
| `meta` | `() -> dict` (optional) | `{"name", "game", "transport"}` |

Generation validates the contract **statically**: the adapter's AST
is parsed and its signatures checked; the module is never imported
and the game server is never contacted. `--probe` is the explicit
opt-in that loads the adapter and calls `fetch_state()` /
`list_actions()` once — point it at a mock or staging server, never
at production unless you mean it. A violating adapter is rejected
with a named `live contract violation` reason.

Generation emits: the adapter copied in, the live CLI
(`observe / actions / act / status / shadow-check / live-autopilot /
live-observe`), `SKILL.md`, `test_smoke.py`, `manifest.json`
(`path: live`, `kind: live`, `label: live-recorded`), a bundled
`policy.json`, and the shadow simulator when `--shadow` is given.

### Session journal

Every `act` — manual or autopilot — appends one JSONL line to
`sessions/live-N.jsonl` next to the CLI: `{type: "act", ts,
state_before, action, result, state_after}`. A session starts with a
`session_start` line; an autopilot run ends with a `summary` line
carrying its `stop_reason`. The journal is the audit trail for a
server you do not control: it cannot prove the server behaved, but
it fixes what you sent and what it answered, in order.

### Shadow simulator & divergence check

The shadow is a deterministic model of the live game, written as an
ordinary game-contract module (`new_game / legal_actions / step /
observe`, plus an optional `from_observe(obs) -> state` used to start
mid-game). It has three jobs:

1. **Planning.** Autopilot strategies (greedy/bfs) look ahead in the
   shadow, never in the live game.
2. **Dry-run.** `live-autopilot --dry-run` plays the whole episode in
   the shadow only: `send_action` is never called and nothing is
   journaled.
3. **Measurement.** `shadow-check <transcript.jsonl>` replays the
   transcript's actions in the shadow and compares, field by field,
   the modelled transitions against the recorded ones: the report
   carries `match_rate`, per-field matched/mismatched counts and the
   first mismatches. It is a measurement, reported as-is — a low rate
   means the model is wrong or the server is less deterministic than
   assumed, and either fact is useful. It is never a verification,
   and the report says so (`verified: false`).

### Policy guardrails (fail-closed)

`policy.json` is enforced by the generated CLI **and** the autopilot;
a refusal happens before anything reaches the adapter:

- `blocked_actions` — never sent, by hand or by bot. There is
  deliberately **no override**: no flag, env var or argument unblocks
  an action. Editing the policy file is the only way, and that is a
  human decision made in the open. (The bundled example blocks
  `buy-stars`, a fictional real-money action.)
- `approval_actions` — human-only. The manual CLI may send them (a
  human is present); the autopilot stops with `needs_human` instead.
- `budgets.max_actions_per_session` — per autopilot run.
- `budgets.max_actions_per_hour` — counted from the journal across
  sessions, so closing and reopening the CLI does not reset it.
- `budgets.resource_caps` — `{field: max_spend}`; spend is read from
  the state (`initial - current`), and the next step's cost is
  estimated on the shadow before the action is sent; a step that
  would cross the cap stops the run with `budget_resource`.
- `allowed_hours` — optional `[start, end]` local-time window
  (wrapping midnight allowed); outside it the autopilot stops with
  `rate_window`.
- **Kill-switch** — a file named `STOP` in `sessions/` halts the
  autopilot at the next step with `kill_switch`.

The autopilot's strategy contract and engine guardrails (step/time
budgets, stall detection, illegal-action abort, exception
containment) are the game path's, unchanged; its result adds the
live stop reasons `budget_actions | budget_resource | blocked |
needs_human | kill_switch | rate_window`, and its `verified` is
always `false` with `label: live-recorded`.

### Live observer (read-only)

`live-observe [--port N]` serves one dashboard page (stdlib
`http.server`, localhost) rendered from the journals: sessions and
their budget consumption, the shadow report's `match_rate`,
anomalies (a session whose last three states are identical — a
suspected stall; three or more consecutive error results; a shadow
`match_rate` below 0.8), and the ten most recent actions. It is
read-only by construction: `GET /` is the only route, and every
other method is refused **405**. There is no action endpoint, no
form, nothing that can reach the game.

### Conformance & tiers for live packages

A live adapter cannot earn Gold by replay, so it earns it by
conformance. `clivane live-check <adapter> --mock <mock-server>`
runs an offline suite against the adapter's **mock** server (the
mock binds an ephemeral localhost port via `$CLIVANE_PORT` and must
answer through the adapter):

| Check | What it proves |
|---|---|
| `contract` | the static contract validation passes |
| `state_jsonable` | `fetch_state()` returns a JSON-able dict |
| `actions_consistent` | `list_actions` is a non-empty `list[str]`, stable across fetches |
| `journal` | a generated CLI's real act writes a well-formed journal (session_start + shaped act line) |
| `policy` | a policy-blocked action is refused by the CLI, leaves the state untouched, and stops an autopilot with `blocked` at step 0 |
| `meta` | `meta()` returns name, game and transport |

Tiers are computed from the per-check evidence recorded in the
package's `conformance.json` (the stored tier string is never
trusted): **Gold** = every check; **Silver** = contract + state +
actions + journal; **Bronze** = contract + state. `clivane registry`
recomputes live tiers from that evidence, exactly as it recomputes
Trace-Seal tiers from traces. Live entries carry `kind: "live"` in
the registry; deterministic entries carry `kind: "deterministic"`.

**Dual-mode packages.** A package may bundle its shadow as a full
deterministic game package in a subfolder (`manifest.shadow_package`)
— one game, two faces in one folder: the live face graded by
conformance, the shadow face Trace-Sealed and replay-verified. The
registry lists both, each under its own proof system.

### Ethics / scope

The live path exists for games you own, operate, or are explicitly
allowed to automate. Its guardrails are designed for restraint —
budgets, windows, a kill-switch, fail-closed blocks on anything
resembling real-money actions — and there is deliberately no
unbounded mode and no policy override. Pointing an unguarded bot at
someone else's game is out of scope, the same line the autopilot
chapter draws for the deterministic path.

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

Live packages follow the conformance ladder instead (see Path 6):
Bronze = contract + state, Silver = + actions + journal, Gold =
every check including policy and meta, recomputed from the evidence
in `conformance.json`.

`clivane registry --dir <parent>` recomputes tiers live (it replays
seals; it never trusts the tier string stored in a manifest) and writes
`registry.json` plus a human-readable `INDEX.md` table.

## Adding a package by hand (checklist)

- [ ] Generate with one of the six paths.
- [ ] Add `demo_calls.json` exercising the main flows.
- [ ] `clivane verify <dir>` → SEALED, run again → PASS.
- [ ] `clivane registry --dir <parent>` shows the tier you expect.
- [ ] Commit the package *including* `traces.json` — the seal is the
      evidence; a package without it is Bronze by definition.
