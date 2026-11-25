#!/usr/bin/env python3
"""starforge-shadow-cli - episode CLI for the game module `starforge_shadow` (clivane game path).

Session state lives in session.json next to this file, so separate
invocations continue the same episode. `replay` re-runs an action list
from a seed and checks the final state matches the recorded session
(or a second identical replay when no matching session exists).
"""
import argparse
import importlib
import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
MODULE = importlib.import_module("starforge_shadow")
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




# ---------------------------------------------------------------------------
# Safe autopilot (clivane) - strategies drive an episode under hard
# guardrails: step/time budgets, stall detection, illegal-action abort,
# exception containment, dry-run simulation, settle-by-replay only.
# ---------------------------------------------------------------------------
import copy as _copy
import random as _random
import time as _time


def _bfs_key(state):
    # Volatile counters would defeat de-duplication; drop the usual ones.
    trimmed = {k: v for k, v in state.items()
               if k not in ("steps", "moves_left")}
    return json.dumps(trimmed, sort_keys=True, default=str)


def _bfs_plan(module, start, max_nodes):
    """Shortest action plan to a done (preferably won) state, or []."""
    from collections import deque
    start = _copy.deepcopy(start)
    seen = {_bfs_key(start)}
    queue = deque([(start, [])])
    fallback = None
    nodes = 0
    while queue:
        state, path = queue.popleft()
        nodes += 1
        if nodes > max_nodes:
            break
        for action in module.legal_actions(state):
            nxt, _r, done, _i = module.step(_copy.deepcopy(state), action)
            npath = path + [action]
            if done:
                if module.observe(nxt).get("won", True):
                    return npath
                if fallback is None:
                    fallback = npath
                continue
            key = _bfs_key(nxt)
            if key not in seen:
                seen.add(key)
                queue.append((nxt, npath))
    return fallback or []


def _target_distance(observe):
    """Manhattan distance player -> nearest gem/target, else None."""
    player = observe.get("player")
    targets = observe.get("gems") or observe.get("targets")
    if (isinstance(player, (list, tuple)) and len(player) == 2
            and isinstance(targets, list) and targets):
        try:
            return min(abs(player[0] - t[0]) + abs(player[1] - t[1])
                       for t in targets)
        except Exception:
            return None
    return None


def _make_choose(name, module, planning_state, seed, max_nodes):
    if name == "random":
        rng = _random.Random(seed)

        def choose_random(observe, legal):
            return rng.choice(list(legal))

        return choose_random
    if name == "greedy":

        def choose_greedy(observe, legal):
            state = planning_state()
            if module is None or state is None:
                raise RuntimeError("greedy needs the game module for lookahead")
            best = None
            best_key = None
            for action in legal:
                nxt, reward, done, _i = module.step(
                    _copy.deepcopy(state), action)
                dist = _target_distance(module.observe(nxt))
                key = (reward, 1 if done else 0,
                       -(dist if dist is not None else 0))
                if best_key is None or key > best_key:
                    best_key = key
                    best = action
            return best

        return choose_greedy
    if name == "bfs":
        plan = {"actions": None}

        def choose_bfs(observe, legal):
            if plan["actions"] is None:
                state = planning_state()
                if module is None or state is None:
                    raise RuntimeError("bfs needs the game module for planning")
                plan["actions"] = _bfs_plan(module, state, max_nodes)
            if plan["actions"]:
                return plan["actions"].pop(0)
            return _make_choose("greedy", module, planning_state,
                                seed, max_nodes)(observe, legal)

        return choose_bfs
    raise RuntimeError("unknown strategy: %s" % name)


def _load_strategy_file(path):
    spec = importlib.util.spec_from_file_location("clivane_user_strategy",
                                                  path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    choose = getattr(mod, "choose", None)
    if not callable(choose):
        raise RuntimeError(
            "strategy file %s must define choose(observe, legal_actions)"
            % path)
    return choose


def run_autopilot(driver, choose, strategy_name, seed, max_steps,
                  max_seconds, target_score, stall_limit, dry_run):
    """Drive one episode through `driver` under the guardrails.

    Returns (result_dict, exit_code). The transcript is persisted via
    driver.persist() on every non-dry-run exit path (aborts included);
    driver.settle() (replay verification) runs only for done episodes.
    Nothing here can write a score anywhere.
    """
    started = _time.monotonic()
    steps = 0
    total_reward = 0
    stop_reason = None
    error = None
    prev_key = json.dumps(driver.obs_now(), sort_keys=True, default=str)
    unchanged = 0
    while True:
        obs = driver.obs_now()
        if driver.done_now():
            stop_reason = "done"
            break
        cur_score = obs.get("score")
        if (target_score is not None
                and isinstance(cur_score, (int, float))
                and not isinstance(cur_score, bool)
                and cur_score >= target_score):
            stop_reason = "target_reached"
            break
        if steps >= max_steps:
            stop_reason = "step_budget"
            break
        if _time.monotonic() - started > max_seconds:
            stop_reason = "time_budget"
            break
        legal = driver.legal()
        if not legal:
            stop_reason = "error"
            error = "no legal actions but the episode is not done"
            break
        try:
            action = choose(_copy.deepcopy(obs), list(legal))
        except Exception as exc:
            stop_reason = "error"
            error = "strategy raised: %s" % exc
            break
        if action not in legal:
            stop_reason = "illegal_action"
            error = ("strategy returned an action outside "
                     "legal_actions: %r" % (action,))
            break
        try:
            obs, reward, done, _info = driver.act(action)
        except Exception as exc:
            stop_reason = "error"
            error = "step failed: %s" % exc
            break
        steps += 1
        if isinstance(reward, (int, float)) and not isinstance(reward, bool):
            total_reward += reward
        if done:
            stop_reason = "done"
            break
        obs_score = obs.get("score")
        if (target_score is not None
                and isinstance(obs_score, (int, float))
                and not isinstance(obs_score, bool)
                and obs_score >= target_score):
            stop_reason = "target_reached"
            break
        key = json.dumps(obs, sort_keys=True, default=str)
        if key == prev_key:
            unchanged += 1
        else:
            unchanged = 0
            prev_key = key
        if unchanged >= stall_limit:
            stop_reason = "stall"
            break
    final_obs = driver.obs_now()
    final_score = final_obs.get("score")
    if not (isinstance(final_score, (int, float))
            and not isinstance(final_score, bool)):
        final_score = total_reward
    verified = False
    if not dry_run:
        try:
            driver.persist()
        except Exception:
            pass
        if driver.done_now():
            try:
                verified = bool(driver.settle())
            except Exception:
                verified = False
    result = {"strategy": strategy_name, "seed": seed, "steps": steps,
              "score": final_score, "done": bool(driver.done_now()),
              "stop_reason": stop_reason, "verified": verified}
    if dry_run:
        result["dry_run"] = True
    if error:
        result["error"] = error
    code = 1 if stop_reason in ("error", "illegal_action") else 0
    return result, code


def add_autopilot_args(sub):
    ap = sub.add_parser("autopilot",
                        help="let a strategy play an episode, safely")
    ap.add_argument("--strategy", default="greedy",
                    choices=["greedy", "bfs", "random"])
    ap.add_argument("--strategy-file",
                    help="Python file defining choose(observe, legal_actions)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--max-steps", type=int, default=200)
    ap.add_argument("--max-seconds", type=float, default=60)
    ap.add_argument("--target-score", type=float, default=None)
    ap.add_argument("--stall-limit", type=int, default=5)
    ap.add_argument("--bfs-max-nodes", type=int, default=20000)
    ap.add_argument("--dry-run", action="store_true",
                    help="simulate only: no session saved, no finish")
    ap.add_argument("--json", action="store_true")
    return ap


class _LocalDriver:
    """Autopilot driver over the local module (no HTTP)."""

    def __init__(self, seed, persist_enabled):
        self.seed = seed
        self.persist_enabled = persist_enabled
        self.state = MODULE.new_game(seed)
        self.transcript = []
        self.last_reward = None
        self.done = False

    def obs_now(self):
        return MODULE.observe(self.state)

    def legal(self):
        return MODULE.legal_actions(self.state)

    def act(self, action):
        self.state, reward, done, info = MODULE.step(self.state, action)
        self.done = bool(done)
        self.last_reward = reward
        self.transcript.append(action)
        return MODULE.observe(self.state), reward, self.done, info

    def done_now(self):
        return self.done

    def planning_state(self):
        return self.state

    def persist(self):
        if self.persist_enabled:
            save_session({"seed": self.seed, "state": self.state,
                          "actions": list(self.transcript),
                          "last_reward": self.last_reward,
                          "done": self.done})

    def settle(self):
        final, _total, ok = play(self.seed, list(self.transcript))
        return bool(ok and final == self.state)


def run_autopilot_command(ns):
    driver = _LocalDriver(ns.seed, persist_enabled=not ns.dry_run)
    if ns.strategy_file:
        try:
            choose = _load_strategy_file(ns.strategy_file)
        except Exception as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 1
        strategy_name = "file:" + os.path.basename(ns.strategy_file)
    else:
        choose = _make_choose(ns.strategy, MODULE, driver.planning_state,
                              ns.seed, ns.bfs_max_nodes)
        strategy_name = ns.strategy
    result, code = run_autopilot(driver, choose, strategy_name, ns.seed,
                                 ns.max_steps, ns.max_seconds,
                                 ns.target_score, ns.stall_limit,
                                 ns.dry_run)
    print(json.dumps(result, indent=2, sort_keys=True))
    return code



def build_parser():
    p = argparse.ArgumentParser(prog="starforge-shadow-cli", description="Episode CLI for starforge_shadow")
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
    add_autopilot_args(sub)
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
    if ns.command == "autopilot":
        return run_autopilot_command(ns)
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
