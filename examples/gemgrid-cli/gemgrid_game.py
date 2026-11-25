"""gemgrid - an original mini game used as the clivane game-path demo.

A 5x5 grid. Move in 4 directions, collect every gem, avoid obstacles.
Placement is a pure function of the seed (random.Random(seed) plus a
deterministic reachability retry), scores are integers, and there are
no timestamps anywhere - so episodes replay bit-for-bit and Trace-Seal
golden traces stay stable.

Implements the clivane game contract:
    new_game(seed) -> state (dict, JSON-able)
    legal_actions(state) -> list[str]
    step(state, action) -> (state, reward, done, info)
    observe(state) -> dict
"""

import copy
import random
from collections import deque

SIZE = 5
MOVES = 40
GEM_COUNT = 3
OBSTACLE_COUNT = 4
GEM_SCORE = 10
DELTAS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}


def _reachable(start, targets, blocked):
    """BFS: can every target cell be reached from start avoiding blocked?"""
    seen = {start}
    queue = deque([start])
    while queue:
        x, y = queue.popleft()
        for dx, dy in DELTAS.values():
            cell = (x + dx, y + dy)
            if (0 <= cell[0] < SIZE and 0 <= cell[1] < SIZE
                    and cell not in blocked and cell not in seen):
                seen.add(cell)
                queue.append(cell)
    return all(t in seen for t in targets)


def new_game(seed: int) -> dict:
    """Start a new GemGrid episode from a seed; returns the state."""
    rng = random.Random(seed)
    start = (0, 0)
    cells = [(x, y) for x in range(SIZE) for y in range(SIZE) if (x, y) != start]
    while True:
        obstacles = rng.sample(cells, OBSTACLE_COUNT)
        rest = [c for c in cells if c not in obstacles]
        gems = rng.sample(rest, GEM_COUNT)
        if _reachable(start, gems, set(obstacles)):
            break
    return {
        "player": [0, 0],
        "gems": sorted([list(g) for g in gems]),
        "obstacles": sorted([list(o) for o in obstacles]),
        "score": 0,
        "steps": 0,
        "moves_left": MOVES,
        "done": False,
        "won": False,
    }


def legal_actions(state: dict) -> list:
    """Moves that stay on the grid and out of obstacles ([] when done)."""
    if state["done"]:
        return []
    x, y = state["player"]
    blocked = {tuple(o) for o in state["obstacles"]}
    out = []
    for name, (dx, dy) in DELTAS.items():
        cell = (x + dx, y + dy)
        if 0 <= cell[0] < SIZE and 0 <= cell[1] < SIZE and cell not in blocked:
            out.append(name)
    return sorted(out)


def step(state: dict, action: str):
    """Apply one move; returns (state, reward, done, info)."""
    state = copy.deepcopy(state)
    if state["done"]:
        return state, 0, True, {"event": "already-done", "score": state["score"]}
    if action not in legal_actions(state):
        return state, -1, state["done"], {"event": "blocked", "score": state["score"]}
    dx, dy = DELTAS[action]
    state["player"] = [state["player"][0] + dx, state["player"][1] + dy]
    state["steps"] += 1
    state["moves_left"] -= 1
    reward = 0
    event = "move"
    if state["player"] in state["gems"]:
        state["gems"].remove(state["player"])
        state["score"] += GEM_SCORE
        reward = GEM_SCORE
        event = "gem"
    if not state["gems"]:
        state["done"] = True
        state["won"] = True
        event = "win"
    elif state["moves_left"] <= 0:
        state["done"] = True
        event = "out-of-moves"
    return state, reward, state["done"], {"event": event, "score": state["score"]}


def observe(state: dict) -> dict:
    """Return the public observation of a state."""
    return {
        "player": list(state["player"]),
        "gems": [list(g) for g in state["gems"]],
        "obstacles": [list(o) for o in state["obstacles"]],
        "score": state["score"],
        "steps": state["steps"],
        "moves_left": state["moves_left"],
        "done": state["done"],
        "won": state["won"],
    }
