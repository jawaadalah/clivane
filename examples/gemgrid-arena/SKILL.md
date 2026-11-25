# gemgrid-game-arena

Fullstack arena for the game `gemgrid_game` (clivane game path, `--fullstack`).
One game contract, three layers: an HTTP session server, a web UI,
and a remote client CLI - plus a leaderboard that only accepts
replay-verified scores.

## Run the arena

```bash
python3 game_server.py                    # 127.0.0.1:8800, UI at /
python3 game_server.py --token SECRET     # require X-Clivane-Token on POSTs
```

Open http://127.0.0.1:8800/ to play in the browser or watch any
session live. Verified scores persist in `scores.json` next to
the server and survive restarts.

## Endpoints

| Method | Path | Meaning |
|---|---|---|
| GET | `/` | the web UI (static, no dependencies) |
| GET | `/health` | `{"status": "ok"}` |
| POST | `/sessions` | `{"seed": N, "player": "name", "max_steps": M?}` -> new isolated session (server-enforced step budget) |
| GET | `/sessions` | list live sessions |
| GET | `/sessions/<id>/observe` | current observation |
| GET | `/sessions/<id>/actions` | legal actions now |
| POST | `/sessions/<id>/act` | `{"action": "..."}` -> step (illegal action = 400) |
| POST | `/sessions/<id>/finish` | replay-verify the episode; record the score |
| GET | `/leaderboard` | verified scores, best first |

## Play as an agent (remote client CLI)

```bash
export CLIVANE_REMOTE=http://127.0.0.1:8800
python3 gemgrid_game_arena_client.py new --seed 7 --player agent --json   # -> session id
python3 gemgrid_game_arena_client.py act down --session s1 --json
python3 gemgrid_game_arena_client.py finish --session s1 --json           # verified score
python3 gemgrid_game_arena_client.py leaderboard --json
```

## Score verification (anti fake-claim)

- There is NO endpoint that accepts a score. `finish` takes no
  score from the client - the server replays the session's whole
  action transcript from its seed and records a score only when
  the replay ends `done` in a state identical to the session's.
- The recorded score is read from the replayed final observation
  (`score` field) or, failing that, the summed step rewards.
- A session can be finished once; unfinished or non-done
  episodes are rejected (400).

## Safe autopilot (bots welcome, bounds enforced)

The client CLI has an `autopilot` command: a strategy
(`greedy` | `bfs` | `random`, or `--strategy-file` with a
`choose(observe, legal_actions)` function) plays a whole episode
under hard budgets (`--max-steps`, `--max-seconds`), stall
detection, illegal-action abort and exception containment.
Planning uses a local shadow copy of the bundled game module;
the server stays authoritative and any observation divergence
aborts the run. `--dry-run` simulates locally with zero server
contact: no session is created, nothing reaches the leaderboard.
When an episode finishes, autopilot calls `finish` - the score
still comes only from the server's replay, never from the bot.
Sessions are created as player `autopilot-<strategy>` unless
`--player` says otherwise.

Server-side, independent of the client: `POST /sessions`
accepts `max_steps`; an act past that budget aborts the session
(403, permanently for that session), and acts are rate-limited
to 20/second per session (429). A rogue bot cannot run unbounded
against this server.

## Security

- Default bind is localhost only; a non-local bind warns loudly.
- Set a token before exposing the arena beyond your machine.
- Sessions are isolated in memory per id; the only shared state
  is the append-only verified leaderboard.
