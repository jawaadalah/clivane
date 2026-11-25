"""notebox - a tiny note-taking application used as the clivane forge demo.

State lives in a JSON file: $NOTEBOX_DB, or ./notebox.json in the current
working directory. Output is fully deterministic (no timestamps), which is
what makes Trace-Seal golden traces stable for this package.
"""

import json
import os

_DB = os.environ.get("NOTEBOX_DB", "notebox.json")


def _load():
    if os.path.isfile(_DB):
        with open(_DB, encoding="utf-8") as fh:
            return json.load(fh)
    return []


def _save(notes):
    with open(_DB, "w", encoding="utf-8") as fh:
        json.dump(notes, fh, indent=2, sort_keys=True)


def create_note(title: str, body: str = "") -> dict:
    """Create a note with a title and an optional body; returns the note."""
    notes = _load()
    note = {"id": (max((n["id"] for n in notes), default=0) + 1),
            "title": title, "body": body}
    notes.append(note)
    _save(notes)
    return note


def list_notes() -> list:
    """List all notes, in creation order."""
    return _load()


def search_notes(query: str) -> list:
    """Find notes whose title or body contains the query (case-insensitive)."""
    q = query.lower()
    return [n for n in _load()
            if q in n["title"].lower() or q in n["body"].lower()]


def count_notes() -> int:
    """Return how many notes exist."""
    return len(_load())


def delete_note(note_id: int) -> bool:
    """Delete a note by id; returns True when a note was removed."""
    notes = _load()
    kept = [n for n in notes if n["id"] != note_id]
    if len(kept) == len(notes):
        return False
    _save(kept)
    return True
