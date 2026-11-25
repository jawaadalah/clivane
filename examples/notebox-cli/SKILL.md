# sample-app-cli

Agent skill for the `sample-app-cli` CLI (forged by clivane from `sample_app`).
Deterministic path: static AST analysis, no LLM involved.

## Commands

- `count-notes  [--json]` - Return how many notes exist.
- `create-note --title <str> [--body <str>] [--json]` - Create a note with a title and an optional body; returns the note.
- `delete-note --note-id <int> [--json]` - Delete a note by id; returns True when a note was removed.
- `list-notes  [--json]` - List all notes, in creation order.
- `search-notes --query <str> [--json]` - Find notes whose title or body contains the query (case-insensitive).

## Conventions

- Add `--json` to any command for a structured envelope:
  `{"command": "<cmd>", "result": <value>}`.
- Run with no arguments to enter the stateful REPL.
- Exit code 0 means success; errors print to stderr with exit 1.
