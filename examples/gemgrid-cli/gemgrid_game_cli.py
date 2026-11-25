#!/usr/bin/env python3
"""gemgrid-game-cli - episode CLI for the game module `gemgrid_game` (clivane game path).

Session state lives in session.json next to this file, so separate
invocations continue the same episode. `replay` re-runs an action list
from a seed and checks the final state matches the recorded session
(or a second identical replay when no matching session exists).
"""
import argparse
import importlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
MODULE = importlib.import_module("gemgrid_game")
HERE = os.path.dirname(os.path.abspath(__file__))
SESSION = os.path.join(HERE, "session.json")


def save_session(sess):
    with open(SESSION, "w", encoding="utf-8") as fh:
        json.dump(sess, fh, indent=2, sort_keys=True)


def load_session():
    if not os.path.isfile(SESSION):
        raise SystemExit("no session yet - start one with: new --seed N")
    with open(SESSION, encoding="utf-8") as fh:
        return json.load(fh)


def emit(command, result, as_json):
    if as_json:
        print(json.dumps({"command": command, "result": result},
                         indent=2, sort_keys=True, default=str))
    elif isinstance(result, (dict, list)):
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        print(result)


def play(seed, actions):
    """Run a fresh episode; return (final_state, total_reward)."""
    state = MODULE.new_game(seed)
    total = 0
    for action in actions:
        legal = MODULE.legal_actions(state)
        if action not in legal:
            return state, total, False
        state, reward, done, _info = MODULE.step(state, action)
        total += reward
        if done:
            break
    return state, total, True


def build_parser():
    p = argparse.ArgumentParser(prog="gemgrid-game-cli", description="Episode CLI for gemgrid_game")
    sub = p.add_subparsers(dest="command")
    n = sub.add_parser("new", help="start a new episode")
    n.add_argument("--seed", type=int, default=0)
    n.add_argument("--json", action="store_true")
    o = sub.add_parser("observe", help="observe the current state")
    o.add_argument("--json", action="store_true")
    a = sub.add_parser("actions", help="list legal actions")
    a.add_argument("--json", action="store_true")
    t = sub.add_parser("act", help="apply one action")
    t.add_argument("action")
    t.add_argument("--json", action="store_true")
    s = sub.add_parser("status", help="session summary")
    s.add_argument("--json", action="store_true")
    r = sub.add_parser("replay", help="replay actions from a seed and verify")
    r.add_argument("--seed", type=int, required=True)
    r.add_argument("--actions", required=True, help="comma-separated actions")
    r.add_argument("--json", action="store_true")
    return p


def main(argv=None):
    ns = build_parser().parse_args(argv)
    if not ns.command:
        build_parser().print_help()
        return 2
    if ns.command == "new":
        state = MODULE.new_game(ns.seed)
        sess = {"seed": ns.seed, "state": state, "actions": [],
                "last_reward": None, "done": False}
        save_session(sess)
        emit("new", {"seed": ns.seed, "observe": MODULE.observe(state)}, ns.json)
        return 0
    if ns.command == "replay":
        actions = [a for a in ns.actions.split(",") if a != ""]
        final, total, ok = play(ns.seed, actions)
        verified = False
        if ok:
            sess = load_session() if os.path.isfile(SESSION) else None
            if sess is not None and sess["seed"] == ns.seed:
                # A transcript exists for this seed: the replay must
                # reproduce it exactly - same actions, same final state.
                # Any altered action is a verification failure.
                verified = (sess["actions"] == actions
                            and final == sess["state"])
            else:
                # No recorded transcript: determinism means a second
                # identical replay must land on the identical state.
                again, _, ok2 = play(ns.seed, actions)
                verified = bool(ok2 and again == final)
        emit("replay", {"seed": ns.seed, "steps": len(actions),
                        "total_reward": total,
                        "final_observe": MODULE.observe(final),
                        "verified": verified}, ns.json)
        return 0 if verified else 1
    sess = load_session()
    state = sess["state"]
    if ns.command == "observe":
        emit("observe", MODULE.observe(state), ns.json)
        return 0
    if ns.command == "actions":
        emit("actions", MODULE.legal_actions(state), ns.json)
        return 0
    if ns.command == "status":
        emit("status", {"seed": sess["seed"], "steps": len(sess["actions"]),
                        "done": sess["done"], "last_reward": sess["last_reward"],
                        "observe": MODULE.observe(state)}, ns.json)
        return 0
    if ns.command == "act":
        if sess["done"]:
            print("error: episode is done - start a new game", file=sys.stderr)
            return 1
        legal = MODULE.legal_actions(state)
        if ns.action not in legal:
            print("error: illegal action %r (legal: %s)"
                  % (ns.action, ", ".join(legal)), file=sys.stderr)
            return 1
        state, reward, done, info = MODULE.step(state, ns.action)
        sess.update({"state": state, "last_reward": reward, "done": done})
        sess["actions"].append(ns.action)
        save_session(sess)
        emit("act", {"observe": MODULE.observe(state), "reward": reward,
                     "done": done, "info": info}, ns.json)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
