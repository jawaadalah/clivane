"""starforge shadow - a deterministic model of the live Starforge game.

Written as an ordinary clivane game-contract module
(new_game / legal_actions / step / observe), so it doubles as a
regular game package (registered as this folder's shadow package,
Trace-Seal verified) AND as the live package's shadow simulator:
strategies are planned and dry-run against it, and `shadow-check`
measures how far the live server diverges from the model.

The model uses the server's modal outcomes: mining always yields
2 ore (the server rolls 1-3), a raid always costs 10 hp and loots
5 stars (the server rolls 5-15 / 3-8). Everything else - energy
costs, trade, repair, the done rule - is modelled exactly, which is
why a live transcript scores a high but not perfect match_rate.
That gap is the honest price of a server-side RNG, and shadow-check
reports it as-is.

`from_observe(obs)` rebuilds a simulator state from a live
observation, so a transcript can be shadow-replayed from wherever
the live session happened to start.
"""

import copy

ACTIONS = ["mine", "trade", "repair", "finish-raid", "buy-stars"]
FIELDS = ("energy", "stars", "ore", "ship_hp", "raids_done", "done")


def new_game(seed: int) -> dict:
    """The model's fresh state (the seed does not drive this game)."""
    return {"energy": 20, "stars": 0, "ore": 0, "ship_hp": 100,
            "raids_done": 0, "done": False}


def from_observe(obs: dict) -> dict:
    """Rebuild a simulator state from a live observation."""
    return {k: obs[k] for k in FIELDS}


def legal_actions(state: dict) -> list:
    """All actions while the ship has energy, none when done."""
    return [] if state["done"] else list(ACTIONS)


def step(state: dict, action: str):
    """Apply one modelled action; returns (state, reward, done, info)."""
    state = copy.deepcopy(state)
    if state["done"]:
        return state, 0, True, {"event": "already-done"}
    if action == "mine":
        state["energy"] = max(0, state["energy"] - 3)
        state["ore"] += 2
        reward, event = 2, "mine"
    elif action == "trade":
        if state["ore"] >= 2:
            state["ore"] -= 2
            state["stars"] += 5
            state["energy"] = max(0, state["energy"] - 1)
            reward, event = 5, "trade"
        else:
            reward, event = 0, "not-enough-ore"
    elif action == "repair":
        state["ship_hp"] = min(100, state["ship_hp"] + 20)
        state["energy"] = max(0, state["energy"] - 2)
        reward, event = 0, "repair"
    elif action == "finish-raid":
        state["ship_hp"] -= 10
        state["stars"] += 5
        state["energy"] = max(0, state["energy"] - 4)
        state["raids_done"] += 1
        reward, event = 5, "finish-raid"
    elif action == "buy-stars":
        state["stars"] += 10
        reward, event = 0, "buy-stars"
    else:
        return state, 0, state["done"], {"event": "blocked"}
    if state["energy"] <= 0:
        state["done"] = True
    return state, reward, state["done"], {"event": event}


def observe(state: dict) -> dict:
    """Return the public observation of a state."""
    return {k: state[k] for k in FIELDS}
