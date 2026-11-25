<p align="center">
  <img src="assets/logo.png" alt="clivane logo" width="160">
</p>

<h1 align="center">clivane</h1>

<p align="center">
  <strong>Make ANY software agent-native — deterministically.</strong><br>
  Today's software serves humans. The next users are agents.<br>
  clivane gets your software ready for them — <em>without an expensive agent in the loop</em>.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-stdlib%20only-blue" alt="stdlib only">
  <img src="https://img.shields.io/badge/dependencies-0-brightgreen" alt="zero dependencies">
  <img src="https://img.shields.io/badge/LLM-not%20required-brightgreen" alt="no LLM">
  <img src="https://img.shields.io/badge/API%20key-none-brightgreen" alt="no API key">
  <img src="https://img.shields.io/badge/license-Apache--2.0-blue" alt="Apache-2.0">
  <img src="https://img.shields.io/badge/version-2.0.0-orange" alt="v2.0.0">
  <img src="https://img.shields.io/badge/selftest-21%2F21%20passing-brightgreen" alt="selftest">
</p>

---

## What is clivane?

clivane is a single-file, pure-stdlib Python tool that converts software
into **agent-native CLIs** — command-line interfaces that AI agents
(Claude Code, Codex, Cursor, Gemini CLI, OpenCode, Copilot, …) can
discover via `--help`, drive with structured `--json` output, and trust
because every generated package carries **replayable proof** of how it
behaves.

Other approaches hand a software package to a large coding agent and let
it improvise a CLI over many expensive phases. clivane takes six paths
instead — five of them **deterministic** (the same input always produces
the byte-identical package, in about a second, for free), plus a sixth,
**live**, for games whose state lives on someone else's server:

| Path | Input | What you get |
|---|---|---|
| **forge** | a Python module | a full CLI: one subcommand per public function |
| **mirror** | an existing CLI tool | a wrapper that adds a structured JSON envelope |
| **openapi** | an OpenAPI 3 JSON spec | one subcommand per `operationId` |
| **game** *(new in v1.1.0)* | a game module obeying the game contract | a session episode CLI with replay verification — or, with `--fullstack` *(v1.2.0)*, a complete arena: session server + web UI + remote client + verified leaderboard — plus a **safe autopilot** *(v1.3.0)* that lets bot strategies play whole episodes under hard guardrails |
| **serve / probe** *(new in v1.1.0)* | a Python module / a live server | an HTTP JSON server + remote client / an endpoint map + generic client |
| **live** *(new in v2.0.0)* | a live game adapter (`fetch_state` / `list_actions` / `send_action`) | a live CLI with session journals, fail-closed policy guardrails, a shadow simulator + divergence check, a guarded autopilot and a read-only observer — results labelled **live-recorded**, never "verified" |

…and one proof system across all six: **Trace-Seal**, golden traces that
are recorded once and replayed to verify behaviour, feeding an explicit
**Bronze / Silver / Gold** quality registry — with an offline
**conformance suite** grading the live path, whose results cannot be
replay-verified by construction.

## Quickstart

No installation, no dependencies — just Python 3.10+:

```bash
git clone https://github.com/jawaadalah/clivane
cd clivane
python3 clivane.py selftest        # 21/21 checks, runs in seconds
```

### Path 1 — forge a Python module

```bash
python3 clivane.py forge examples/sample_app.py --out examples/notebox-cli
cd examples/notebox-cli
python3 sample_app_cli.py create-note --title "Hello" --body "first" --json
python3 sample_app_cli.py list-notes --json
python3 sample_app_cli.py            # no args -> stateful REPL
```

### Path 2 — mirror an existing tool

```bash
python3 clivane.py mirror ls --out examples/mirror-ls
python3 examples/mirror-ls/mirror_ls.py --json /tmp
# -> {"tool": "ls", "argv": ["/tmp"], "exit_code": 0, "stdout": "...", "stderr": ""}
```

### Path 3 — generate from OpenAPI

```bash
python3 clivane.py openapi examples/todoline.openapi.json --out examples/todoline-cli
python3 examples/todoline-cli/todoline_cli.py --help
python3 examples/todoline-cli/todoline_cli.py list-todos --base-url http://localhost:8799 --json
```

### Path 4 — turn a game into an episode CLI

The game module implements four functions — `new_game(seed)`,
`legal_actions(state)`, `step(state, action) -> (state, reward, done,
info)`, `observe(state)` — and clivane validates that contract before
generating anything:

```bash
python3 clivane.py game examples/gemgrid_game.py --out examples/gemgrid-cli
cd examples/gemgrid-cli
python3 gemgrid_game_cli.py new --seed 7 --json
python3 gemgrid_game_cli.py act down --json     # {observe, reward, done, info}
python3 gemgrid_game_cli.py replay --seed 7 --actions down,down,down,right,up,right,right,right --json
# -> "verified": true — the replayed episode ends in the identical state
```

#### …or the full arena (v1.2.0)

```bash
python3 clivane.py game examples/gemgrid_game.py --fullstack --out examples/gemgrid-arena
cd examples/gemgrid-arena
python3 game_server.py                          # 127.0.0.1:8800 — API + web UI + leaderboard
# humans: open http://127.0.0.1:8800/ and play, or watch any session live
# agents: the generated remote client drives the same sessions over HTTP
CLIVANE_REMOTE=http://127.0.0.1:8800 \
  python3 gemgrid_game_arena_client.py new --seed 7 --player agent --json
CLIVANE_REMOTE=http://127.0.0.1:8800 \
  python3 gemgrid_game_arena_client.py finish --session s1 --json
# -> the server replays the episode from its seed; only a reproduced
#    score reaches the persistent leaderboard — claims are never trusted
```

#### …or let a bot play it — safe autopilot (v1.3.0)

Both game CLIs (local and arena client) carry an `autopilot` command.
A strategy is just `choose(observe, legal_actions) -> action`; three
deterministic ones are built in — `greedy` (1-step lookahead on a copy
of the state), `bfs` (shortest plan to a win, bounded search) and
`random` (seeded baseline) — and your own loads via `--strategy-file`:

```bash
python3 gemgrid_game_cli.py autopilot --strategy greedy --seed 7 --json
# -> {"strategy": "greedy", "seed": 7, "steps": 8, "score": 30,
#     "done": true, "stop_reason": "done", "verified": true}

CLIVANE_REMOTE=http://127.0.0.1:8800 \
  python3 gemgrid_game_arena_client.py autopilot --strategy bfs --seed 7
# -> plays on the server as "autopilot-bfs"; finish + server replay
#    put it on the leaderboard — the bot never submits a score itself
```

The guardrails are the feature. Around *any* strategy the engine
enforces hard step/time budgets (`--max-steps`, `--max-seconds`),
stall detection (observation unchanged 5 times in a row → stop),
illegal-action abort (the action is never sent to the game) and
exception containment (partial transcript saved, non-zero exit, no
corrupt state). `--dry-run` projects a full episode with zero side
effects — no session saved locally, no session created on the server.
And the arena server defends itself independently of the client:
sessions accept a server-side `max_steps` budget (breach → 403 and the
session is aborted for good) and acts are rate-limited to
20/second per session (429).

### Path 5 — serve a module over HTTP, probe a live server

```bash
python3 clivane.py serve examples/sample_app.py --out examples/notebox-server
cd examples/notebox-server
python3 server.py                              # 127.0.0.1:8799, localhost only
# in another shell — the generated client speaks to the server:
CLIVANE_REMOTE=http://127.0.0.1:8799 \
  python3 sample_app_server_client.py create-note --title "Remote" --json

# …and the reverse direction: introspect any live server
python3 clivane.py probe http://127.0.0.1:8799 --out /tmp/probed
```

### Path 6 — a game that lives on someone else's server (v2.0.0)

Some games keep their state and RNG on a remote server — a web game, a
Telegram game, a hosted world. There is no seed to replay, so the live
path makes a different promise: **everything is recorded, nothing is
over-claimed**. You write a small adapter (`fetch_state()`,
`list_actions(state)`, `send_action(action)`); generation validates it
*statically*, so it never touches the real server:

```bash
# the bundled mock game server (fictional "Starforge") for the demo:
CLIVANE_PORT=8799 python3 examples/starforge-live/mock_server.py &
python3 clivane.py live examples/starforge-live/starforge_adapter.py \
  --out examples/starforge-live \
  --shadow examples/starforge-live/starforge_shadow.py \
  --policy examples/starforge-live/policy.json
cd examples/starforge-live
CLIVANE_REMOTE=http://127.0.0.1:8799 \
  python3 starforge_live.py act mine --json     # journaled, live-recorded
CLIVANE_REMOTE=http://127.0.0.1:8799 \
  python3 starforge_live.py act buy-stars --json
# -> refused by policy.json (fail-closed; there is no override flag)
CLIVANE_REMOTE=http://127.0.0.1:8799 \
  python3 starforge_live.py shadow-check transcript.jsonl --json
# -> match_rate 1.0 on the bundled controlled transcript — a
#    measurement against the shadow model, reported as-is
printf 'def choose(observe, legal_actions):\n    return "mine"\n' > /tmp/always_mine.py
CLIVANE_REMOTE=http://127.0.0.1:8799 \
  python3 starforge_live.py live-autopilot --strategy-file /tmp/always_mine.py
# -> stops "budget_resource" after 4 mines: the policy caps total
#    energy spend per session, and the ship is never drained dry
python3 clivane.py live-check examples/starforge-live/starforge_adapter.py \
  --mock examples/starforge-live/mock_server.py   # conformance: Gold
```

### Prove it — Trace-Seal + registry

```bash
python3 clivane.py verify examples/notebox-cli   # first run: SEALED
python3 clivane.py verify examples/notebox-cli   # every later run: PASS / FAIL
python3 clivane.py registry --dir examples       # tiers for every package
```

## The six paths in detail

### forge — static analysis, not vibes

`forge` parses your module with Python's `ast` module. Public functions
become subcommands; type annotations become coercion rules (`int`,
`float`, `bool`, `str`, and JSON-parsed `list`/`dict`); literal defaults
become optional flags; docstrings become help text. The generated CLI
ships with:

- one subcommand per public function, kebab-cased,
- `--json` on **every** command: `{"command": ..., "result": ...}`,
- a **stateful REPL** when run with no arguments — module state persists
  across commands inside the session,
- the target module copied alongside, so the package is self-contained,
- a generated `SKILL.md` (command map for agents), a smoke test, and a
  `manifest.json`.

### mirror — zero-code adoption for existing CLIs

Most software worth automating already has a CLI; it just speaks
human. `mirror` probes `<tool> --help`, extracts the subcommand map into
a `SKILL.md`, and generates a thin wrapper: arguments forward untouched
to the real tool, the exit code is preserved, and a `--json` flag
(intercepted, never forwarded) switches the output to a structured
envelope — `{"tool", "argv", "exit_code", "stdout", "stderr"}` — that
any agent can consume reliably.

### openapi — every operation, one command

`openapi` reads a local OpenAPI 3 JSON document and emits one
subcommand per `operationId`. Path parameters are substituted and
URL-quoted, query parameters become flags, POST/PUT/PATCH accept
`--body '<json>'`, and requests run on `urllib` — still zero
dependencies. `--json` wraps responses as `{"status", "body"}`.

### game — episodes an agent can play, and prove

Games and simulation environments are software too, but their interface
is a *loop*, not a function call. The clivane game contract captures
that loop in four functions (above); the generated CLI keeps the
episode in `session.json`, so `new` / `observe` / `actions` / `act` /
`status` across separate invocations form one transcript. `replay`
re-runs an action list from its seed and verifies the final state is
identical — an altered action fails loudly. Seed determinism is what
lets a whole winning episode become a golden trace: the bundled
GemGrid package is Gold because its 8-move win replays bit-for-bit.

With `--fullstack`, the same contract grows into a complete **arena**:
a stdlib HTTP server hosting many isolated sessions in parallel, a
dependency-free web UI (play manually, or watch an agent's session
live, board and leaderboard on one navy page), and a remote client CLI
so agents play over HTTP with the same verbs. Scores are
anti-fake by construction: there is no submit-a-score endpoint —
`finish` makes the *server* replay the episode from its seed, and
only a reproduced score is written to the persistent leaderboard
(`scores.json`). The bundled `gemgrid-arena` package is Gold: its
Trace-Seal replays the full client↔server episode, finish included.

### serve & probe — software over HTTP, both directions

`serve` wraps a module in a stdlib HTTP server: `GET /health`,
`GET /commands`, and `POST /run` returning the forge envelope. `/run`
is a dispatcher, not an evaluator — it only invokes commands from the
module manifest, validates names and args, and can require an
`X-Clivane-Token` header. It binds **localhost by default** and warns
loudly on any other bind. The generated client CLI has the same
commands as a forged CLI, so an agent can drive the software remotely.

`probe` goes the other way: point it at a server that is already
running and it introspects `/openapi.json`, `/commands`, `/health` and
`/` — then emits an endpoint map (`SKILL.md`) and a generic client CLI
for whatever answered.

For Trace-Seal, server packages are verified end-to-end: the harness
starts the generated server on an ephemeral port inside a fresh copy
and replays the client calls against it — real HTTP traffic, stable
traces.

### live — games on someone else's server, honestly labelled

The deterministic game path rests on one wall: the seed is ours, so a
replay can *prove* a result. Live games knock that wall down — state
and RNG belong to a remote server — so the live path is built on a
different contract, and it says so everywhere:

- **Honest label.** Every output, journal line and registry entry on
  this path is labelled `live-recorded`, and `verified` is always
  `false`. A journal is evidence of what happened, never proof it
  could be reproduced.
- **Adapter core.** An adapter is three functions —
  `fetch_state()`, `list_actions(state)`, `send_action(action)` —
  plus an optional `meta()`. Generation validates the contract
  *statically* (AST signatures, nothing imported, nothing called), so
  generating a package never touches the real game; `--probe` opts
  into one live probe against a mock or staging server.
- **Session journal.** Every `act` appends a JSONL line
  (`ts`, `state_before`, `action`, `result`, `state_after`) to
  `sessions/live-N.jsonl` — the audit trail for a server you do not
  control.
- **Shadow simulator.** A deterministic model of the live game,
  written as an ordinary game-contract module, bundled with the
  package. Strategies are planned and dry-run against it, and
  `shadow-check <transcript>` replays a live transcript in the model
  and reports the divergence — `match_rate`, mismatched fields —
  exactly as measured. The bundled Starforge transcript scores 1.0
  (a controlled fixture: the mock's seed is chosen so its early rolls
  land on the model's modal values); a real game's score will be
  lower, and that number *is* the information.
- **Policy guardrails.** `policy.json` is enforced fail-closed by the
  CLI and the autopilot alike: `blocked_actions` are never sent
  (there is no override flag), `approval_actions` stop the autopilot
  with `needs_human`, budgets cap actions per session and per hour
  (counted from the journal) and resource spend read from the state
  (`budget_resource`), an `allowed_hours` window and a `STOP` file
  in `sessions/` act as schedule gate and kill-switch.
- **Read-only observer.** `live-observe` serves one dashboard page —
  sessions, budget consumption, anomalies (stalls, error streaks, a
  low shadow match rate), recent actions. GET only; every other
  method is refused 405. There is no action endpoint at all.
- **Conformance, not replay.** `clivane live-check <adapter> --mock
  <mock-server>` grades an adapter offline: contract, JSON-able
  state, consistent actions, a correctly written journal, policy
  enforcement. Gold requires every check; the tier is recomputed
  from the recorded per-check evidence, never from a stored string.

The bundled example is dual-mode: `examples/starforge-live/` holds
the live package (Gold by conformance) and, in its `shadow/`
subfolder, the same game as a deterministic game package (Gold by
Trace-Seal) — one game, two faces, two proof systems that never
pretend to be each other.

## Trace-Seal & quality tiers

Generated code is easy; *trusted* generated code is the hard part.
Trace-Seal records a **golden trace**: the probe set (`--help`, every
command's `--help`, plus the package's `demo_calls.json`) executed in a
fresh copy of the package, with output normalised and stored in
`traces.json`. `verify` replays the same set and compares exactly —
drift in behaviour, or a hand-edited trace, fails loudly and names the
differing calls.

The registry turns that evidence into an explicit quality tier:

| Tier | Requirement |
|---|---|
| **Bronze** | package generated, manifest present |
| **Silver** | Bronze + all probe calls exit 0 (smoke level) |
| **Gold** | Silver + a Trace-Seal on file that replays **PASS** |

Live packages earn tiers from the **conformance suite** instead
(contract + state + journal for Silver, every check including policy
and meta for Gold), recomputed from the recorded evidence in
`conformance.json`. Tiers are **recomputed live** by
`clivane registry` — the tier string in a manifest is never trusted.
See `examples/INDEX.md` for a live table: all eight bundled examples
are Gold.

## Included examples

| Package | Path | Source | Commands | Tier |
|---|---|---|---|---|
| `sample-app-cli` | forge | `examples/sample_app.py` ("notebox") | create-note, list-notes, search-notes, count-notes, delete-note | Gold |
| `mirror-ls` | mirror | the system `ls` tool | (single-command tool) | Gold |
| `todoline-cli` | openapi | `examples/todoline.openapi.json` | list-todos, create-todo, get-todo, delete-todo | Gold |
| `gemgrid-game-cli` | game | `examples/gemgrid_game.py` ("GemGrid") | new, observe, actions, act, status, replay | Gold |
| `gemgrid-game-arena` | game-fullstack | `examples/gemgrid_game.py` ("GemGrid") | arena server + web UI + remote client: new, observe, actions, act, finish, leaderboard | Gold |
| `sample-app-server` | serve | `examples/sample_app.py` ("notebox") | the 5 notebox commands, over HTTP | Gold |
| `starforge-live` | live | `examples/starforge-live/starforge_adapter.py` ("Starforge", mock server included) | observe, actions, act, status, shadow-check, live-autopilot, live-observe | Gold (conformance) |
| `starforge-shadow-cli` | game | `examples/starforge-live/starforge_shadow.py` (the shadow model, as a game package) | new, observe, actions, act, status, replay, autopilot | Gold |

`notebox` keeps its state in a JSON file and is deliberately
timestamp-free, which is what makes its golden trace stable — a good
pattern to copy for your own forge targets.

## How clivane compares

An honest comparison with the agent-harness approach (a coding agent
builds your CLI phase by phase, e.g. the 7-phase methodology popularised
by CLI-Anything):

| | clivane | agent-harness approach |
|---|---|---|
| Cost per CLI | **0** (local, deterministic) | many LLM/agent calls |
| Time per CLI | **~1 second** | minutes to hours |
| Needs an API key / agent | **no** | yes |
| Reproducibility | **byte-identical output** | varies per run |
| Backend required | only what the target itself needs | the real app must be installed |
| GUI desktop apps | via mirror (if they ship any CLI) or forge (Python cores) | **strongest case — deep GUI mapping** |
| Games / episodic environments | **game contract -> episode CLI + replay; `--fullstack` arena with web UI + verified leaderboard** | not addressed |
| Live-server games (state/RNG not yours) | **live path: adapter + journal + shadow divergence + policy guardrails, honestly labelled live-recorded** | not addressed |
| Remote / HTTP access | **serve (server+client) and probe (introspect live servers)** | local processes only |
| Proof of behaviour | **Trace-Seal golden traces + tiers** | test suites written per project |
| Package size | 1 generated file + SKILL.md | full package tree per app |

Use the harness approach when a GUI application needs deep, semantic
mapping of its internals. Use clivane when you want agent access to
Python modules, existing CLIs, and HTTP APIs **today**, with proof.

## Repository layout

```
clivane/
├── clivane.py          # the whole tool (stdlib only)
├── selftest.py         # 21-check self-test suite
├── SOP.md              # the methodology — source of truth
├── examples/
│   ├── sample_app.py           # "notebox", the forge + serve demo target
│   ├── notebox-cli/            # forged package (Gold, traces committed)
│   ├── todoline.openapi.json   # the openapi demo spec
│   ├── todoline-cli/           # generated package (Gold)
│   ├── mirror-ls/              # mirrored package (Gold)
│   ├── gemgrid_game.py         # "GemGrid", the game demo target
│   ├── gemgrid-cli/            # game package (Gold, episode sealed)
│   ├── gemgrid-arena/          # fullstack arena: server + web UI + client (Gold)
│   ├── notebox-server/         # server + client package (Gold)
│   ├── starforge-live/         # live package (Gold by conformance):
│   │   │                       #   mock server + adapter + policy + journal demo
│   │   └── shadow/             #   the same game, deterministic (Gold by Trace-Seal)
│   ├── registry.json           # machine-readable registry
│   └── INDEX.md                # human-readable tier table
└── assets/logo.png
```

## Commands

```
clivane forge   <module.py> --out <dir>    # AST -> agent CLI
clivane mirror  <tool>      --out <dir>    # existing CLI -> JSON wrapper
clivane openapi <spec.json> --out <dir>    # OpenAPI -> per-operation CLI
clivane game    <game.py>   --out <dir>    # game contract -> episode CLI
clivane game    <game.py>   --out <dir> --fullstack
                                           # game contract -> arena (server + web UI + client)
clivane serve   <module.py> --out <dir>    # module -> HTTP server + client
clivane probe   <base_url>  --out <dir>    # live server -> map + client
clivane live    <adapter.py> --out <dir> [--shadow <sim.py>] [--policy <policy.json>]
                                           # live game adapter -> live CLI (journal + guardrails)
clivane live-check <adapter.py> --mock <mock.py> [--package <dir>]
                                           # conformance suite -> Bronze/Silver/Gold
clivane verify  <dir> [--record]           # Trace-Seal record / replay
clivane registry --dir <parent>            # tiers -> registry.json + INDEX.md
clivane list    <dir>                      # inspect one package
clivane selftest                           # run the self-test suite
```

## Methodology

Everything above follows one written standard: **[SOP.md](SOP.md)** —
the six paths, the Trace-Seal protocol, and the tier rules. If the
code and the SOP ever disagree, the SOP wins.

## Bahasa Indonesia (singkat)

clivane mengubah software apa pun menjadi CLI yang siap dipakai AI
agent, **tanpa LLM dan tanpa API key**. Enam jalur: **forge** (modul
Python dianalisis statis via AST menjadi CLI lengkap), **mirror** (CLI
yang sudah ada dibungkus envelope JSON terstruktur), **openapi**
(spesifikasi OpenAPI 3 menjadi CLI per operasi), **game** (modul game
berkontrak menjadi CLI episode dengan replay terverifikasi — atau mode
`--fullstack`: arena lengkap berisi server sesi, UI web, client
remote, dan papan skor yang hanya menerima skor hasil replay server),
**server** dua arah — **serve** (modul menjadi server HTTP + client
jarak jauh) serta **probe** (introspeksi server yang sedang jalan
menjadi peta endpoint + client) — dan **live** *(v2.0.0)* untuk game
yang state & RNG-nya di server orang lain: adapter kecil menjadi CLI
live dengan jurnal sesi, pagar policy fail-closed, simulator bayangan
+ ukur divergensi, autopilot terjaga, dan observer baca-saja; semua
hasilnya berlabel jujur **live-recorded**, tidak pernah diklaim
terverifikasi. Setiap paket hasil dibuktikan dengan **Trace-Seal** —
rekaman golden trace yang diputar ulang untuk verifikasi — atau, khusus
jalur live, uji **conformance** offline melawan mock server; keduanya
diberi tier kualitas Bronze/Silver/Gold di registry. Satu file Python,
nol dependensi.

## License

Apache-2.0 — see [LICENSE](LICENSE).
