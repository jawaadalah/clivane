# starforge-live

Live CLI for the game adapter `starforge_adapter` (clivane live path).
Game: **Starforge** - transport: REST (HTTP JSON).

## Honest label

Everything this package records is **live-recorded**. The game's
state and RNG live on a remote server, so no result here is - or
can be - replay-verified; `verified` is always `false` on this
path. Replay-verified results exist only in the deterministic
game path and in this package's shadow simulator.

## Commands

- `observe [--json]` - fetch and print the current live state.
- `actions [--json]` - actions legal in the current state.
- `act <action> [--json]` - perform one live action. Every act
  is appended to the session journal (see below). Policy-blocked
  actions are refused before anything is sent.
- `status [--json]` - current session id, journaled act count,
  label and the live observation.
- `shadow-check <transcript.jsonl> [--json]` - replay the
  transcript's actions in the shadow simulator and report the
  divergence (`match_rate`, mismatched fields) - a measurement,
  reported as-is, never a verification.
- `live-autopilot [--strategy greedy|bfs|random]
  [--strategy-file f.py] [--seed N] [--max-steps N]
  [--max-seconds N] [--target-score N] [--dry-run]` - let a
  strategy play under the policy guardrails (see below).
  `--dry-run` plays in the shadow simulator only: no live
  action is sent, nothing is journaled.
- `live-observe [--port N]` - serve the read-only observer
  dashboard (sessions, budget use, anomalies, recent actions).
  GET only; every other method is refused 405.

## Session journal

Sessions live in `sessions/` next to the CLI, one JSONL file
per session (`live-1.jsonl`, ...). Lines: `session_start`,
one `act` line per action (`ts`, `state_before`, `action`,
`result`, `state_after`) and a `summary` line when an
autopilot run ends. The journal is the audit trail for a game
whose server you do not control.

## Policy guardrails (policy.json, fail-closed)

- `blocked_actions` - never sent, by the CLI or the autopilot.
  There is no override flag.
- `approval_actions` - human-only; the autopilot stops with
  `needs_human` instead of sending them.
- `budgets.max_actions_per_session`, `budgets.max_actions_per_hour`
  (counted from the journal), `budgets.resource_caps`
  (`{field: max_spend}`, spend read from the state, the next
  step's cost estimated on the shadow simulator).
- `allowed_hours` - optional `[start, end]` local-time window.
- Kill-switch: a file named `STOP` in `sessions/` halts the
  autopilot at the next step (`kill_switch`).

Autopilot stop reasons: `done | target_reached | step_budget |`
`time_budget | stall | illegal_action | error | budget_actions |`
`budget_resource | blocked | needs_human | kill_switch |`
`rate_window`.

## Shadow simulator

`starforge_shadow.py` is a deterministic model of the live game, written as
an ordinary clivane game-contract module. Strategies are
tested against it (dry-run, greedy/bfs lookahead), and
`shadow-check` measures how far the live game diverges
from the model.
