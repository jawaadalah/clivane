#!/usr/bin/env python3
"""starforge-live - live CLI for the game adapter `starforge_adapter` (clivane live path).

The game's state and RNG live on a remote server, reached through the
adapter module bundled next to this file. Everything this CLI records
is labelled "live-recorded": a session journal is evidence of what
happened, never replay-verified proof - the seed belongs to the
server, not to us. The deterministic shadow simulator bundled with
this package (starforge_shadow.py) is where strategies are tested and live
transcripts are measured for divergence.
"""
import argparse
import html
import importlib
import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# clivane live runtime - adapter driver, session journal, policy.
#
# HONEST LABEL: everything recorded here is "live-recorded". A live
# game's state and RNG live on a remote server, so nothing on this
# path is - or can be - replay-verified; verified is always False.
# ---------------------------------------------------------------------------
import copy as _copy
import time as _time


class PolicyError(Exception):
    """An action refused by policy.json (fail-closed, no override)."""


LABEL = "live-recorded"
HERE = os.path.dirname(os.path.abspath(__file__))
SESSIONS_DIR = os.path.join(HERE, "sessions")
POLICY_PATH = os.path.join(HERE, "policy.json")
ADAPTER = importlib.import_module("starforge_adapter")
SHADOW = None
if os.path.isfile(os.path.join(HERE, "starforge_shadow.py")):
    SHADOW = importlib.import_module("starforge_shadow")


def load_policy(path=None):
    path = path or POLICY_PATH
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


POLICY = load_policy()


def fetch():
    state = ADAPTER.fetch_state()
    if not isinstance(state, dict):
        raise RuntimeError("adapter fetch_state() must return a dict")
    return state


def legal(state=None):
    state = fetch() if state is None else state
    return list(ADAPTER.list_actions(state))


def policy_blocked(action):
    return action in (POLICY.get("blocked_actions") or [])


def _summarize(state):
    try:
        json.dumps(state)
        return state
    except Exception:
        return {"unserializable": repr(state)[:200]}


def journal_path(sid):
    return os.path.join(SESSIONS_DIR, sid + ".jsonl")


def journal(entry, sid=None):
    sid = sid or current_session_id(create=True)
    entry = dict(entry)
    entry.setdefault("ts", _time.time())
    entry.setdefault("session", sid)
    with open(journal_path(sid), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
    return sid


def current_session_id(create=False):
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    cur = os.path.join(SESSIONS_DIR, "current.json")
    if os.path.isfile(cur):
        with open(cur, encoding="utf-8") as fh:
            return json.load(fh)["id"]
    if not create:
        return None
    n = 1 + len([f for f in os.listdir(SESSIONS_DIR)
                 if f.startswith("live-") and f.endswith(".jsonl")])
    sid = "live-%d" % n
    with open(cur, "w", encoding="utf-8") as fh:
        json.dump({"id": sid}, fh)
    journal({"type": "session_start", "session": sid, "mode": "live",
             "label": LABEL}, sid=sid)
    return sid


def read_journal(sid):
    path = journal_path(sid)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def all_sessions():
    if not os.path.isdir(SESSIONS_DIR):
        return []
    return sorted(f[:-6] for f in os.listdir(SESSIONS_DIR)
                  if f.startswith("live-") and f.endswith(".jsonl"))


def journal_act_count(within_seconds=None):
    total = 0
    cutoff = _time.time() - within_seconds if within_seconds else None
    for sid in all_sessions():
        for line in read_journal(sid):
            if line.get("type") == "act":
                if cutoff is None or line.get("ts", 0) >= cutoff:
                    total += 1
    return total


def _reward_of(before, result, after):
    reward = result.get("reward")
    if isinstance(reward, (int, float)) and not isinstance(reward, bool):
        return reward
    b, a = before.get("score"), after.get("score")
    if (isinstance(b, (int, float)) and isinstance(a, (int, float))
            and not isinstance(b, bool) and not isinstance(a, bool)):
        return a - b
    return 0


def do_act(action):
    """One live action, journaled. Policy-blocked actions never leave
    this process - fail-closed, and there is deliberately no flag,
    env var or argument that overrides the block."""
    if policy_blocked(action):
        raise PolicyError(
            "policy: action %r is blocked (blocked_actions in policy.json)"
            % action)
    before = fetch()
    actions = legal(before)
    if action not in actions:
        raise RuntimeError("illegal action %r (legal: %s)"
                           % (action, ", ".join(actions)))
    result = ADAPTER.send_action(action)
    if not isinstance(result, dict):
        result = {"result": result}
    after = fetch()
    sid = journal({"type": "act", "state_before": _summarize(before),
                   "action": action, "result": _summarize(result),
                   "state_after": _summarize(after)})
    return {"session": sid, "action": action, "observe": after,
            "result": result, "label": LABEL}



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


def emit(command, result, as_json):
    if as_json:
        print(json.dumps({"command": command, "result": result},
                         indent=2, sort_keys=True, default=str))
    elif isinstance(result, (dict, list)):
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        print(result)


# ---------------------------------------------------------------------------
# Shadow simulator - divergence measurement for live transcripts
# ---------------------------------------------------------------------------

def _shadow_state_from(obs, seed):
    if SHADOW is None:
        return None
    if hasattr(SHADOW, "from_observe"):
        return SHADOW.from_observe(_copy.deepcopy(obs))
    return SHADOW.new_game(seed)


def run_shadow_check(transcript_path):
    if SHADOW is None:
        raise RuntimeError("this package has no shadow simulator bundled")
    with open(transcript_path, encoding="utf-8") as fh:
        lines = [json.loads(l) for l in fh if l.strip()]
    acts = [l for l in lines if l.get("type") == "act"]
    start = next((l for l in lines if l.get("type") == "session_start"), {})
    seed = start.get("seed", 0)
    if acts:
        state = _shadow_state_from(acts[0]["state_before"], seed)
    else:
        state = SHADOW.new_game(seed)
    matched = mismatched = tmatch = 0
    mismatches = []
    for i, line in enumerate(acts):
        state, reward, _done, _info = SHADOW.step(state, line["action"])
        obs = SHADOW.observe(state)
        rec = line.get("state_after") or {}
        fields = 0
        bad = []
        for key, val in rec.items():
            if key in obs:
                fields += 1
                if obs[key] == val:
                    matched += 1
                else:
                    mismatched += 1
                    bad.append(key)
        rec_reward = (line.get("result") or {}).get("reward")
        if (isinstance(rec_reward, (int, float))
                and not isinstance(rec_reward, bool)):
            fields += 1
            if reward == rec_reward:
                matched += 1
            else:
                mismatched += 1
                bad.append("reward")
        if fields and not bad:
            tmatch += 1
        elif bad:
            mismatches.append({"step": i + 1, "action": line["action"],
                               "fields": bad})
    total = matched + mismatched
    report = {"transcript": os.path.basename(transcript_path),
              "transitions": len(acts),
              "transitions_matched": tmatch,
              "fields_matched": matched,
              "fields_mismatched": mismatched,
              "match_rate": round(matched / total, 4) if total else 1.0,
              "mismatches": mismatches[:10],
              "label": LABEL, "verified": False}
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    with open(os.path.join(SESSIONS_DIR, "shadow_report.json"), "w",
              encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    return report


# ---------------------------------------------------------------------------
# Live autopilot - strategies under the policy guardrails.
#
# The strategy contract is the same choose(observe, legal) as the
# deterministic autopilot, and greedy/bfs plan against the SHADOW
# simulator (never against the live game). Around any strategy the
# policy file is enforced fail-closed:
#   blocked_actions   - never sent; the run stops `blocked`
#   approval_actions  - human-only; the run stops `needs_human`
#   budgets.max_actions_per_session / step & time budgets
#   budgets.max_actions_per_hour    - counted from the journal
#   budgets.resource_caps {field: max_spend} - spend read from state,
#                       next-step cost estimated on the shadow
#   allowed_hours [a, b]            - local-time window
#   sessions/STOP file              - kill-switch, checked every step
# --dry-run plays the whole episode in the shadow simulator only:
# send_action is never called and nothing is journaled.
# The result is labelled live-recorded and verified stays False -
# there is no replay on this path, by design and by honesty.
# ---------------------------------------------------------------------------

class _LiveDriver:
    """Autopilot driver over the real adapter (not dry-run)."""

    def __init__(self, seed):
        self.seed = seed
        self.state = fetch()
        self.done = bool(self.state.get("done"))
        self.shadow = _shadow_state_from(self.state, seed)

    def obs_now(self):
        return self.state

    def legal(self):
        return legal(self.state)

    def done_now(self):
        return self.done

    def planning_state(self):
        return self.shadow

    def act(self, action):
        before = self.state
        out = do_act(action)
        self.state = out["observe"]
        self.done = bool(self.state.get("done"))
        if self.shadow is not None:
            try:
                self.shadow, _r, _d, _i = SHADOW.step(self.shadow, action)
            except Exception:
                self.shadow = None
        return (self.state, _reward_of(before, out["result"], self.state),
                self.done, out["result"])


class _ShadowDriver:
    """Dry-run driver: shadow simulator only, zero live contact."""

    def __init__(self, seed):
        if SHADOW is None:
            raise RuntimeError(
                "dry-run needs the shadow simulator bundled in this package")
        self.seed = seed
        self.state = SHADOW.new_game(seed)
        self.done = bool(SHADOW.observe(self.state).get("done"))

    def obs_now(self):
        return SHADOW.observe(self.state)

    def legal(self):
        return SHADOW.legal_actions(self.state)

    def done_now(self):
        return self.done

    def planning_state(self):
        return self.state

    def act(self, action):
        self.state, reward, done, info = SHADOW.step(self.state, action)
        self.done = bool(done)
        return SHADOW.observe(self.state), reward, self.done, info


def _hour_allowed(hour, window):
    a, b = window
    if a <= b:
        return a <= hour <= b
    return hour >= a or hour <= b


def _num(value):
    return (isinstance(value, (int, float))
            and not isinstance(value, bool))


def run_live_autopilot(ns):
    budgets = POLICY.get("budgets") or {}
    caps = budgets.get("resource_caps") or {}
    blocked = set(POLICY.get("blocked_actions") or [])
    approval = set(POLICY.get("approval_actions") or [])
    max_actions_session = budgets.get("max_actions_per_session")
    max_actions_hour = budgets.get("max_actions_per_hour")
    allowed_hours = POLICY.get("allowed_hours")
    driver = _ShadowDriver(ns.seed) if ns.dry_run else _LiveDriver(ns.seed)
    if ns.strategy_file:
        choose = _load_strategy_file(ns.strategy_file)
        strategy_name = "file:" + os.path.basename(ns.strategy_file)
    else:
        choose = _make_choose(ns.strategy, SHADOW, driver.planning_state,
                              ns.seed, ns.bfs_max_nodes)
        strategy_name = ns.strategy
    started = _time.monotonic()
    initial = _copy.deepcopy(driver.obs_now())
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
        cur = obs.get("score")
        if ns.target_score is not None and _num(cur) \
                and cur >= ns.target_score:
            stop_reason = "target_reached"
            break
        if steps >= ns.max_steps:
            stop_reason = "step_budget"
            break
        if _time.monotonic() - started > ns.max_seconds:
            stop_reason = "time_budget"
            break
        if (max_actions_session is not None
                and steps >= max_actions_session):
            stop_reason = "budget_actions"
            break
        if not ns.dry_run:
            if allowed_hours and not _hour_allowed(
                    _time.localtime().tm_hour, allowed_hours):
                stop_reason = "rate_window"
                break
            if (max_actions_hour is not None
                    and journal_act_count(3600) >= max_actions_hour):
                stop_reason = "rate_window"
                break
            if os.path.isfile(os.path.join(SESSIONS_DIR, "STOP")):
                stop_reason = "kill_switch"
                break
        legal_actions = driver.legal()
        if not legal_actions:
            stop_reason = "error"
            error = "no legal actions but the episode is not done"
            break
        try:
            action = choose(_copy.deepcopy(obs), list(legal_actions))
        except Exception as exc:
            stop_reason = "error"
            error = "strategy raised: %s" % exc
            break
        if action in blocked:
            stop_reason = "blocked"
            error = "policy: action %r is blocked" % (action,)
            break
        if action in approval:
            stop_reason = "needs_human"
            error = ("policy: action %r needs human approval" % (action,))
            break
        if action not in legal_actions:
            stop_reason = "illegal_action"
            error = ("strategy returned an action outside "
                     "legal_actions: %r" % (action,))
            break
        if caps and SHADOW is not None and driver.planning_state() is not None:
            plan = driver.planning_state()
            try:
                nxt, _r, _d, _i = SHADOW.step(_copy.deepcopy(plan), action)
                plan_obs = SHADOW.observe(plan)
                next_obs = SHADOW.observe(nxt)
                over = None
                for field, cap in caps.items():
                    if _num(initial.get(field)) and _num(obs.get(field)) \
                            and _num(plan_obs.get(field)) \
                            and _num(next_obs.get(field)):
                        spend = max(0, initial[field] - obs[field])
                        est = max(0, plan_obs[field] - next_obs[field])
                        if spend + est > cap:
                            over = field
                            break
                if over is not None:
                    stop_reason = "budget_resource"
                    error = ("policy: resource cap for %r would be "
                             "exceeded" % over)
                    break
            except Exception:
                pass
        elif caps:
            over = None
            for field, cap in caps.items():
                if _num(initial.get(field)) and _num(obs.get(field)):
                    if max(0, initial[field] - obs[field]) >= cap:
                        over = field
                        break
            if over is not None:
                stop_reason = "budget_resource"
                error = "policy: resource cap for %r reached" % over
                break
        try:
            obs, reward, done, _info = driver.act(action)
        except PolicyError as exc:
            stop_reason = "blocked"
            error = str(exc)
            break
        except Exception as exc:
            stop_reason = "error"
            error = "step failed: %s" % exc
            break
        steps += 1
        if _num(reward):
            total_reward += reward
        if done:
            stop_reason = "done"
            break
        key = json.dumps(obs, sort_keys=True, default=str)
        if key == prev_key:
            unchanged += 1
        else:
            unchanged = 0
            prev_key = key
        if unchanged >= ns.stall_limit:
            stop_reason = "stall"
            break
    final_obs = driver.obs_now()
    final_score = final_obs.get("score")
    if not _num(final_score):
        final_score = total_reward
    result = {"strategy": strategy_name, "seed": ns.seed, "steps": steps,
              "score": final_score, "done": bool(driver.done_now()),
              "stop_reason": stop_reason, "verified": False,
              "label": LABEL}
    if ns.dry_run:
        result["dry_run"] = True
    if error:
        result["error"] = error
    if not ns.dry_run:
        journal({"type": "summary", "strategy": strategy_name,
                 "steps": steps, "score": final_score,
                 "stop_reason": stop_reason, "label": LABEL})
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if stop_reason in ("error", "illegal_action") else 0


# ---------------------------------------------------------------------------
# Live observer - a READ-ONLY dashboard over the session journals.
# It serves GET / only; every other method is refused 405. There is
# no action endpoint, no form post, nothing that can touch the game.
# ---------------------------------------------------------------------------

def _anomalies():
    out = []
    for sid in all_sessions():
        lines = read_journal(sid)
        acts = [l for l in lines if l.get("type") == "act"]
        if len(acts) >= 3:
            tail = [json.dumps(a.get("state_after"), sort_keys=True,
                               default=str) for a in acts[-3:]]
            if len(set(tail)) == 1:
                out.append("%s: stall suspected (state unchanged x3)" % sid)
        errs = 0
        for line in reversed(lines):
            if line.get("type") != "act":
                continue
            if "error" in (line.get("result") or {}):
                errs += 1
            else:
                break
        if errs >= 3:
            out.append("%s: %d consecutive error results" % (sid, errs))
    rep = os.path.join(SESSIONS_DIR, "shadow_report.json")
    if os.path.isfile(rep):
        try:
            with open(rep, encoding="utf-8") as fh:
                rate = json.load(fh).get("match_rate")
            if rate is not None and rate < 0.8:
                out.append("shadow match_rate low: %s" % rate)
        except Exception:
            pass
    return out


def _observer_html():
    esc = html.escape
    budgets = POLICY.get("budgets") or {}
    caps = budgets.get("resource_caps") or {}
    max_actions = budgets.get("max_actions_per_session")
    rows = []
    recent = []
    for sid in all_sessions():
        lines = read_journal(sid)
        acts = [l for l in lines if l.get("type") == "act"]
        summ = next((l for l in reversed(lines)
                     if l.get("type") == "summary"), None)
        spend_bits = []
        if acts:
            first = acts[0].get("state_before") or {}
            last = acts[-1].get("state_after") or {}
            for field, cap in caps.items():
                if _num(first.get(field)) and _num(last.get(field)):
                    spend_bits.append("%s spent %s / cap %s"
                                      % (field, first[field] - last[field],
                                         cap))
        budget = "%d acts" % len(acts)
        if max_actions is not None:
            budget += " / session budget %s" % max_actions
        if spend_bits:
            budget += "; " + "; ".join(spend_bits)
        rows.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                    % (esc(sid), esc(budget),
                       esc(acts[-1]["action"] if acts else "-"),
                       esc(summ.get("stop_reason", "-") if summ else "-")))
        for line in acts:
            recent.append((line.get("ts", 0), sid, line))
    recent.sort(key=lambda t: t[0], reverse=True)
    recent_rows = []
    for _ts, sid, line in recent[:10]:
        recent_rows.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (esc(sid), esc(str(line.get("action"))),
               esc(json.dumps(line.get("result") or {}, default=str)[:120])))
    anomalies = _anomalies()
    anomaly_html = "".join("<li>%s</li>" % esc(a) for a in anomalies) \
        or "<li>none</li>"
    rep_html = "no shadow-check run yet"
    rep = os.path.join(SESSIONS_DIR, "shadow_report.json")
    if os.path.isfile(rep):
        try:
            with open(rep, encoding="utf-8") as fh:
                r = json.load(fh)
            rep_html = ("match_rate %s over %s transitions (%s)"
                        % (r.get("match_rate"), r.get("transitions"),
                           esc(str(r.get("transcript")))))
        except Exception:
            rep_html = "shadow report unreadable"
    return ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<meta http-equiv='refresh' content='5'>"
            "<title>starforge-live - live observer</title>"
            "<style>body{font-family:system-ui,sans-serif;margin:2em;"
            "background:#0A1931;color:#e8eef7}"
            "table{border-collapse:collapse;margin:1em 0}"
            "td,th{border:1px solid #35507a;padding:.3em .7em}"
            ".banner{background:#7a2d0e;padding:.6em 1em;border-radius:6px}"
            "</style></head><body>"
            "<h1>starforge-live - live observer</h1>"
            "<p class='banner'>Label: <strong>live-recorded</strong> - "
            "these are journal records, NOT replay-verified results. "
            "This dashboard is read-only.</p>"
            "<h2>Sessions</h2><table><tr><th>session</th><th>budget use</th>"
            "<th>last action</th><th>stop_reason</th></tr>"
            + "".join(rows) + "</table>"
            "<h2>Anomalies</h2><ul>" + anomaly_html + "</ul>"
            "<h2>Shadow divergence</h2><p>" + rep_html + "</p>"
            "<h2>Recent actions</h2><table><tr><th>session</th>"
            "<th>action</th><th>result</th></tr>"
            + "".join(recent_rows) + "</table>"
            "</body></html>")


def run_observer(port):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/", "/index.html"):
                body = b"not found"
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = _observer_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _refuse(self):
            body = b"method not allowed: this observer is read-only"
            self.send_response(405)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = _refuse
        do_PUT = _refuse
        do_DELETE = _refuse
        do_PATCH = _refuse

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    print("live observer (read-only) on http://127.0.0.1:%d/" % port)
    server.serve_forever()
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="starforge-live", description="Live CLI for starforge_adapter (label: live-recorded)")
    sub = p.add_subparsers(dest="command")
    o = sub.add_parser("observe", help="fetch and print the live state")
    o.add_argument("--json", action="store_true")
    a = sub.add_parser("actions", help="list actions legal right now")
    a.add_argument("--json", action="store_true")
    t = sub.add_parser("act", help="perform one live action (journaled)")
    t.add_argument("action")
    t.add_argument("--policy", default=None,
                   help="policy file (default: policy.json next to the CLI)")
    t.add_argument("--json", action="store_true")
    s = sub.add_parser("status", help="current session summary")
    s.add_argument("--json", action="store_true")
    sc = sub.add_parser("shadow-check",
                        help="measure a transcript against the shadow sim")
    sc.add_argument("transcript")
    sc.add_argument("--json", action="store_true")
    ap = sub.add_parser("live-autopilot",
                        help="let a strategy play, under policy guardrails")
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
    ap.add_argument("--policy", default=None,
                    help="policy file (default: policy.json next to the CLI)")
    ap.add_argument("--dry-run", action="store_true",
                    help="shadow simulation only: no live action is sent")
    ap.add_argument("--json", action="store_true")
    ob = sub.add_parser("live-observe",
                        help="serve the read-only observer dashboard")
    ob.add_argument("--port", type=int, default=8899)
    return p


def main(argv=None):
    global POLICY
    ns = build_parser().parse_args(argv)
    if not ns.command:
        build_parser().print_help()
        return 2
    if getattr(ns, "policy", None):
        POLICY = load_policy(ns.policy)
    try:
        if ns.command == "observe":
            emit("observe", fetch(), ns.json)
            return 0
        if ns.command == "actions":
            emit("actions", legal(), ns.json)
            return 0
        if ns.command == "act":
            emit("act", do_act(ns.action), ns.json)
            return 0
        if ns.command == "status":
            sid = current_session_id()
            acts = 0
            if sid:
                acts = len([l for l in read_journal(sid)
                            if l.get("type") == "act"])
            emit("status", {"session": sid, "acts": acts, "label": LABEL,
                            "verified": False, "observe": fetch()}, ns.json)
            return 0
        if ns.command == "shadow-check":
            emit("shadow-check", run_shadow_check(ns.transcript), ns.json)
            return 0
        if ns.command == "live-autopilot":
            return run_live_autopilot(ns)
        if ns.command == "live-observe":
            return run_observer(ns.port)
    except PolicyError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
