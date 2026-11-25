#!/usr/bin/env python3
"""gemgrid-game-arena client - remote CLI for a clivane fullstack game arena.

The local game verbs (new / observe / actions / act) plus finish and
leaderboard, but every call is HTTP to the arena server. Base URL:
--remote, else $CLIVANE_REMOTE, else http://127.0.0.1:8800.
$CLIVANE_TOKEN is sent as X-Clivane-Token.
"""
import argparse
import importlib
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
DEFAULT_REMOTE = "http://127.0.0.1:8800"


def call(remote, method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    token = os.environ.get("CLIVANE_TOKEN")
    if token:
        headers["X-Clivane-Token"] = token
    req = urllib.request.Request(remote.rstrip("/") + path, data=data,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"error": raw}
        print("error: %s" % payload.get("error", exc), file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print("error: cannot reach arena at %s (%s)" % (remote, exc),
              file=sys.stderr)
        raise SystemExit(1)


def emit(command, result, as_json):
    if as_json:
        print(json.dumps({"command": command, "result": result},
                         indent=2, sort_keys=True, default=str))
    else:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))




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


def _call_safe(remote, method, path, payload=None):
    """call() that raises RuntimeError instead of SystemExit."""
    try:
        return call(remote, method, path, payload)
    except SystemExit:
        raise RuntimeError("arena request failed: %s %s" % (method, path))


class _RemoteDriver:
    """Autopilot driver over HTTP.

    Planning (greedy/bfs lookahead) uses a local shadow copy of the
    game module bundled in this package; the server stays
    authoritative: every act executes server-side and the returned
    observation must match the shadow, else the run aborts.
    """

    def __init__(self, remote, seed, player, max_steps):
        self.remote = remote
        created = _call_safe(remote, "POST", "/sessions",
                             {"seed": seed, "player": player,
                              "max_steps": max_steps})
        self.sid = created["id"]
        self.obs = created["observe"]
        self.done = bool(created["done"])
        self.transcript = []
        self.module = None
        self.shadow = None
        try:
            self.module = importlib.import_module("gemgrid_game")
            self.shadow = self.module.new_game(seed)
        except Exception:
            self.module = None
            self.shadow = None

    def obs_now(self):
        return self.obs

    def legal(self):
        res = _call_safe(self.remote, "GET",
                         "/sessions/%s/actions" % self.sid)
        return res["actions"]

    def act(self, action):
        res = _call_safe(self.remote, "POST",
                         "/sessions/%s/act" % self.sid, {"action": action})
        if self.module is not None:
            self.shadow, _r, _d, _i = self.module.step(self.shadow, action)
            if self.module.observe(self.shadow) != res["observe"]:
                raise RuntimeError(
                    "arena observation diverged from the local plan "
                    "(non-deterministic game?)")
        self.obs = res["observe"]
        self.done = bool(res["done"])
        self.transcript.append(action)
        return res["observe"], res["reward"], self.done, res.get("info", {})

    def done_now(self):
        return self.done

    def planning_state(self):
        return self.shadow

    def persist(self):
        pass  # the transcript lives server-side in the session

    def settle(self):
        try:
            res = _call_safe(self.remote, "POST",
                             "/sessions/%s/finish" % self.sid, {})
        except Exception:
            return False
        return bool(res.get("verified"))


class _SimDriver:
    """Dry-run driver: full local simulation, zero server contact."""

    def __init__(self, seed):
        self.module = importlib.import_module("gemgrid_game")
        self.state = self.module.new_game(seed)
        self.done = False

    def obs_now(self):
        return self.module.observe(self.state)

    def legal(self):
        return self.module.legal_actions(self.state)

    def act(self, action):
        self.state, reward, done, info = self.module.step(self.state,
                                                          action)
        self.done = bool(done)
        return self.module.observe(self.state), reward, self.done, info

    def done_now(self):
        return self.done

    def planning_state(self):
        return self.state

    def persist(self):
        pass

    def settle(self):
        return False


def run_autopilot_command(ns):
    if ns.dry_run:
        try:
            driver = _SimDriver(ns.seed)
        except Exception as exc:
            print("error: dry-run needs the bundled game module (%s)"
                  % exc, file=sys.stderr)
            return 1
        module = driver.module
    else:
        try:
            driver = _RemoteDriver(
                ns.remote, ns.seed,
                ns.player or ("autopilot-%s" % ns.strategy), ns.max_steps)
        except Exception as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 1
        module = driver.module
    if ns.strategy_file:
        try:
            choose = _load_strategy_file(ns.strategy_file)
        except Exception as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 1
        strategy_name = "file:" + os.path.basename(ns.strategy_file)
    else:
        choose = _make_choose(ns.strategy, module, driver.planning_state,
                              ns.seed, ns.bfs_max_nodes)
        strategy_name = ns.strategy
    result, code = run_autopilot(driver, choose, strategy_name, ns.seed,
                                 ns.max_steps, ns.max_seconds,
                                 ns.target_score, ns.stall_limit,
                                 ns.dry_run)
    print(json.dumps(result, indent=2, sort_keys=True))
    return code



def build_parser():
    p = argparse.ArgumentParser(prog="gemgrid-game-arena",
                                description="Remote client for the gemgrid_game arena")
    p.add_argument("--remote",
                   default=os.environ.get("CLIVANE_REMOTE", DEFAULT_REMOTE),
                   help="base URL of the arena server")
    sub = p.add_subparsers(dest="command")
    n = sub.add_parser("new", help="create a session on the server")
    n.add_argument("--seed", type=int, default=0)
    n.add_argument("--player", default="player")
    n.add_argument("--json", action="store_true")
    for verb, helptext in (("observe", "observe a session"),
                           ("actions", "legal actions of a session"),
                           ("finish", "verify by replay and record the score")):
        q = sub.add_parser(verb, help=helptext)
        q.add_argument("--session", required=True)
        q.add_argument("--json", action="store_true")
    t = sub.add_parser("act", help="apply one action to a session")
    t.add_argument("action")
    t.add_argument("--session", required=True)
    t.add_argument("--json", action="store_true")
    lb = sub.add_parser("leaderboard", help="verified scores, best first")
    lb.add_argument("--json", action="store_true")
    ap = add_autopilot_args(sub)
    ap.add_argument("--player", default=None,
                    help="leaderboard name (default: autopilot-<strategy>)")
    return p


def main(argv=None):
    parser = build_parser()
    ns = parser.parse_args(argv)
    if not ns.command:
        parser.print_help()
        return 2
    if ns.command == "autopilot":
        return run_autopilot_command(ns)
    if ns.command == "new":
        result = call(ns.remote, "POST", "/sessions",
                      {"seed": ns.seed, "player": ns.player})
    elif ns.command == "observe":
        result = call(ns.remote, "GET", "/sessions/%s/observe" % ns.session)
    elif ns.command == "actions":
        result = call(ns.remote, "GET", "/sessions/%s/actions" % ns.session)
    elif ns.command == "act":
        result = call(ns.remote, "POST", "/sessions/%s/act" % ns.session,
                      {"action": ns.action})
    elif ns.command == "finish":
        result = call(ns.remote, "POST", "/sessions/%s/finish" % ns.session, {})
    elif ns.command == "leaderboard":
        result = call(ns.remote, "GET", "/leaderboard")
    else:
        return 2
    emit(ns.command, result, ns.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
