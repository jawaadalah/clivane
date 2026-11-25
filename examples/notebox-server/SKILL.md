# sample-app-server

HTTP server + remote client for the module `sample_app` (clivane serve path).

## Run the server

```bash
python3 server.py                 # 127.0.0.1:8799 (CLIVANE_PORT to change)
python3 server.py --token SECRET  # require X-Clivane-Token on /run
```

Endpoints: `GET /health`, `GET /commands`, `POST /run` with
`{"command": "<cmd>", "args": {...}}` -> the forge envelope
`{"command": ..., "result": ...}`. `/run` only dispatches commands
from the module manifest - there is no free-form evaluation.

## Run the client

```bash
export CLIVANE_REMOTE=http://127.0.0.1:8799
python3 sample_app_server_client.py <command> [--json]
```

## Commands (served)

- `count-notes` - Return how many notes exist.
- `create-note` - Create a note with a title and an optional body; returns the note.
- `delete-note` - Delete a note by id; returns True when a note was removed.
- `list-notes` - List all notes, in creation order.
- `search-notes` - Find notes whose title or body contains the query (case-insensitive).

## Security

- Default bind is localhost only; a non-local bind warns loudly.
- Set a token before exposing the server beyond your machine.
