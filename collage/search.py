"""Simulated annealing over slicing trees (subset selection included).

Besides the classic slicing-tree moves (flip a cut, swap leaves/subtrees, move a
leaf) the proposals are *fit aware*: when a frame is inserted or replaced, the
candidate is drawn with a preference for frames whose size matches the slot, and
a "repair" move picks the node with the largest slack and tries to fix exactly that.
"""
from __future__ import annotations

import copy
import math
import os
import random
import time
from dataclasses import dataclass
from multiprocessing import Pool

from .inventory import Frame
from .layout import Eval, Params, Tree, is_leaf, leaves, size_violation, total_cost


@dataclass
class SearchConfig:
    iters: int = 1_000_000  # iterations per run (ignored when a time budget is given)
    minutes: float | None = None  # total wall-clock budget for all runs of this search
    run_seconds: float | None = None  # derived from ``minutes``: wall-clock time of one run
    runs: int = 32
    seed: int = 0
    g_min: float = 1.5
    g_max: float = 6.0
    g_step: float = 0.5
    g_fixed: float | None = None
    g_mode: str = "free"  # "free": g is an annealing variable; "per-run": each run keeps one g from the grid
    min_frames: int = 10
    t_start: float | None = None  # None: calibrated so that a median uphill move is accepted with p=0.5
    t_end: float = 0.05
    fit_temp: float = 1.0  # softness (cm) of the fit-aware frame choice
    jobs: int | None = None


@dataclass
class Candidate:
    tree: Tree
    g: float
    cost: float
    run: int


# ------------------------------------------------------------------ tree helpers (paths)


def node_paths(t: Tree, path: tuple = (), acc: list | None = None) -> list[tuple]:
    if acc is None:
        acc = []
    acc.append(path)
    if not is_leaf(t):
        node_paths(t[1], path + (1,), acc)
        node_paths(t[2], path + (2,), acc)
    return acc


def get(t: Tree, path: tuple) -> Tree:
    for i in path:
        t = t[i]
    return t


def get_eval(e: Eval, path: tuple) -> Eval:
    for i in path:
        e = e.a if i == 1 else e.b
    return e


def replace(t: Tree, path: tuple, value: Tree) -> Tree:
    """Replace the subtree at ``path`` (in place); returns the (possibly new) root."""
    if not path:
        return value
    node = t
    for i in path[:-1]:
        node = node[i]
    node[path[-1]] = value
    return t


def chain(items: list[Tree], op: str) -> Tree:
    t = items[0]
    for x in items[1:]:
        t = [op, t, x]
    return t


def sibling_path(path: tuple) -> tuple:
    return path[:-1] + (3 - path[-1],)


def detach(t: Tree, path: tuple) -> Tree:
    """Remove the subtree at ``path`` (its sibling takes the parent's place)."""
    return replace(t, path[:-1], get(t, sibling_path(path)))


# --------------------------------------------------------------------------- init / moves


def greedy_init(frames: list[Frame], g: float, p: Params, rng: random.Random, min_frames: int) -> Tree:
    """Fill rows up to the target width and stack them up to the target height."""
    ids = list(range(len(frames)))
    rng.shuffle(ids)
    rows: list[list[int]] = []
    row: list[int] = []
    wsum = 0.0
    hsum = 0.0
    for i in ids:
        f = frames[i]
        add = f.w + (g if row else 0.0)
        if row and wsum + add > p.target_w:
            rows.append(row)
            hsum += max(frames[j].h for j in row) + (g if len(rows) > 1 else 0.0)
            row, wsum = [], 0.0
            add = f.w
            if hsum >= p.target_h - 25:
                break
        row.append(i)
        wsum += add
    if not rows:
        rows.append(row)
    return chain([chain(r, "V") for r in rows], "H")


def pick_by_fit(cands: list[int], score, rng: random.Random, temp: float) -> int | None:
    """Draw a candidate with probability ~ exp(-score/temp) (score in cm, lower = better fit)."""
    if not cands:
        return None
    scores = [score(i) for i in cands]
    m = min(scores)
    weights = [math.exp(-(s - m) / temp) for s in scores]
    return rng.choices(cands, weights)[0]


def _node_slack(e: Eval) -> tuple[float, bool, int]:
    """(slack, vertical?, index of the smaller child 1|2) for an inner node."""
    if e.op == "V":
        s = abs(e.a.h - e.b.h)
        return s, True, (1 if e.a.h < e.b.h else 2)
    s = abs(e.a.w - e.b.w)
    return s, False, (1 if e.a.w < e.b.w else 2)


def _local_costs(e: Eval, path: tuple = (), acc: list | None = None) -> list[tuple[float, tuple]]:
    if acc is None:
        acc = []
    if e.op is not None:
        acc.append((e.cost - e.a.cost - e.b.cost, path))
        _local_costs(e.a, path + (1,), acc)
        _local_costs(e.b, path + (2,), acc)
    return acc


def mutate(t: Tree, g: float, e: Eval, frames: list[Frame], cfg: SearchConfig, rng: random.Random) -> tuple[Tree, float]:
    """Return a mutated copy of ``t`` (and possibly a new gap)."""
    t = copy.deepcopy(t)
    r = rng.random()
    paths = node_paths(t)
    leaf_paths = [q for q in paths if is_leaf(get(t, q))]
    inner_paths = [q for q in paths if not is_leaf(get(t, q))]
    used = set(leaves(t))
    unused = [i for i in range(len(frames)) if i not in used]
    nleaves = len(leaf_paths)
    ft = cfg.fit_temp

    def slot_score(path: tuple, vertical: bool):
        """Score function for a frame to be put at ``path``: distance to the sibling's size."""
        if not path:
            return lambda i: 0.0
        sib = get_eval(e, sibling_path(path))
        cur = get_eval(e, path)
        if vertical:
            return lambda i: abs(frames[i].h - max(sib.h, cur.h if cur.h < sib.h else 0.0))
        return lambda i: abs(frames[i].w - max(sib.w, cur.w if cur.w < sib.w else 0.0))

    if cfg.g_fixed is None and cfg.g_mode == "free" and r < 0.04:
        step = cfg.g_step * rng.choice((-1, 1))
        g = min(cfg.g_max, max(cfg.g_min, round(g + step, 3)))
        return t, g
    if r < 0.18 and inner_paths:  # flip cut direction
        n = get(t, rng.choice(inner_paths))
        n[0] = "H" if n[0] == "V" else "V"
    elif r < 0.24 and inner_paths:  # swap children
        n = get(t, rng.choice(inner_paths))
        n[1], n[2] = n[2], n[1]
    elif r < 0.34 and nleaves >= 2:  # swap two leaves
        pa, pb = rng.sample(leaf_paths, 2)
        a, b = get(t, pa), get(t, pb)
        replace(t, pa, b)
        replace(t, pb, a)
    elif r < 0.42 and len(paths) >= 4:  # swap two disjoint subtrees
        pa, pb = rng.sample(paths[1:], 2)
        if pa[: len(pb)] != pb and pb[: len(pa)] != pa:
            a, b = get(t, pa), get(t, pb)
            replace(t, pa, b)
            replace(t, pb, a)
    elif r < 0.54 and nleaves >= 3:  # move a leaf somewhere else
        pa = rng.choice(leaf_paths)
        leaf = get(t, pa)
        t = detach(t, pa)
        q = rng.choice(node_paths(t))
        sub = get(t, q)
        op = rng.choice("HV")
        new = [op, sub, leaf] if rng.random() < 0.5 else [op, leaf, sub]
        t = replace(t, q, new)
    elif r < 0.68 and unused:  # replace a leaf by an unused frame that fits its slot
        pa = rng.choice(leaf_paths)
        cur = get(t, pa)
        cand = [i for i in unused if frames[i].type != frames[cur].type]
        if cand and pa:
            parent_op = get(t, pa[:-1])[0]
            choice = pick_by_fit(cand, slot_score(pa, parent_op == "V"), rng, ft)
            replace(t, pa, choice)
    elif r < 0.78 and unused:  # insert an unused frame next to a node, preferring fitting sizes
        q = rng.choice(paths)
        sub = get(t, q)
        eq = get_eval(e, q)
        op = rng.choice("HV")
        score = (lambda i: abs(frames[i].h - eq.h)) if op == "V" else (lambda i: abs(frames[i].w - eq.w))
        leaf = pick_by_fit(unused, score, rng, ft)
        new = [op, sub, leaf] if rng.random() < 0.5 else [op, leaf, sub]
        t = replace(t, q, new)
    elif r < 0.83 and nleaves > cfg.min_frames:  # remove a frame
        t = detach(t, rng.choice(leaf_paths))
    elif inner_paths:  # repair: attack the node with the largest local cost
        t = repair(t, g, e, frames, cfg, rng, unused)
    return t, g


def repair(t: Tree, g: float, e: Eval, frames: list[Frame], cfg: SearchConfig, rng: random.Random,
           unused: list[int]) -> Tree:
    costs = _local_costs(e)
    costs.sort(reverse=True)
    # pick among the worst few nodes
    _, path = rng.choice(costs[: min(3, len(costs))])
    en = get_eval(e, path)
    s, vertical, short_idx = _node_slack(en)
    if s <= 1e-9:
        return t
    short_path = path + (short_idx,)
    long_path = path + (3 - short_idx,)
    short_e = get_eval(e, short_path)
    short_t = get(t, short_path)
    choice = rng.random()
    if choice < 0.35 and unused:
        # extend the smaller child by a frame that fills the slack
        op = "H" if vertical else "V"
        need = s - g
        if vertical:
            score = lambda i: abs(frames[i].h - need) + 0.5 * abs(frames[i].w - short_e.w)
        else:
            score = lambda i: abs(frames[i].w - need) + 0.5 * abs(frames[i].h - short_e.h)
        leaf = pick_by_fit(unused, score, rng, cfg.fit_temp)
        new = [op, short_t, leaf] if rng.random() < 0.5 else [op, leaf, short_t]
        t = replace(t, short_path, new)
    elif choice < 0.65 and unused:
        # replace a leaf of the smaller child by a larger one (or of the larger child by a smaller one)
        grow = rng.random() < 0.6
        base = short_path if grow else long_path
        lp = [q for q in node_paths(get(t, base)) if is_leaf(get(get(t, base), q))]
        q = base + rng.choice(lp)
        cur = get(t, q)
        delta = s if grow else -s
        cand = [i for i in unused if frames[i].type != frames[cur].type]
        if vertical:
            score = lambda i: abs(frames[i].h - (frames[cur].h + delta)) + 0.5 * abs(frames[i].w - frames[cur].w)
        else:
            score = lambda i: abs(frames[i].w - (frames[cur].w + delta)) + 0.5 * abs(frames[i].h - frames[cur].h)
        leaf = pick_by_fit(cand, score, rng, cfg.fit_temp)
        if leaf is not None:
            replace(t, q, leaf)
    elif choice < 0.8:
        # remove a leaf from the larger child
        long_t = get(t, long_path)
        lp = [q for q in node_paths(long_t) if is_leaf(get(long_t, q))]
        if len(lp) >= 2 and len(leaves(t)) > cfg.min_frames:
            t = detach(t, long_path + rng.choice(lp))
    else:
        # flip the cut of the smaller child (if it is an inner node) or of the node itself
        n = get(t, short_path) if not is_leaf(short_t) else get(t, path)
        n[0] = "H" if n[0] == "V" else "V"
    return t


# ------------------------------------------------------------------------------ annealing


def gap_grid(cfg: SearchConfig) -> list[float]:
    gs = []
    x = cfg.g_min
    while x <= cfg.g_max + 1e-9:
        gs.append(round(x, 3))
        x += cfg.g_step
    return gs


def calibrate_t_start(t: Tree, g: float, e: Eval, c: float, frames: list[Frame], p: Params, cfg: SearchConfig,
                      rng: random.Random, trials: int = 400) -> float:
    """Start temperature at which the median uphill move is accepted with probability 0.5."""
    ups = []
    for _ in range(trials):
        t2, g2 = mutate(t, g, e, frames, cfg, rng)
        c2, _ = total_cost(t2, frames, g2, p)
        if c2 > c:
            ups.append(c2 - c)
    if not ups:
        return 10.0
    ups.sort()
    return max(1.0, ups[len(ups) // 2] / math.log(2))


def anneal(frames: list[Frame], p: Params, cfg: SearchConfig, run: int) -> list[Candidate]:
    """One annealing run; returns the best state plus the best state per frame count."""
    rng = random.Random(cfg.seed * 1000 + run)
    gs = gap_grid(cfg)
    if cfg.g_fixed is not None:
        g = cfg.g_fixed
    elif cfg.g_mode == "per-run":
        g = gs[run % len(gs)]
    else:
        g = rng.choice(gs)
    t = greedy_init(frames, g, p, rng, cfg.min_frames)
    c, e = total_cost(t, frames, g, p)
    viol = size_violation(e.w, e.h, p)
    best: dict[int, Candidate] = {e.n: Candidate(copy.deepcopy(t), g, c, run)}
    best_all = Candidate(copy.deepcopy(t), g, c, run)
    limit = p.tol + p.hard_margin
    t_start = cfg.t_start if cfg.t_start is not None else calibrate_t_start(t, g, e, c, frames, p, cfg, rng)
    log_ratio = math.log(cfg.t_end / t_start)
    timed = cfg.run_seconds is not None
    t_begin = time.monotonic()
    k = 0
    frac = 0.0
    while True:
        if timed:
            if k % 500 == 0:
                frac = (time.monotonic() - t_begin) / cfg.run_seconds
                if frac >= 1.0:
                    break
        else:
            if k >= cfg.iters:
                break
            frac = k / cfg.iters
        k += 1
        T = t_start * math.exp(log_ratio * frac)
        t2, g2 = mutate(t, g, e, frames, cfg, rng)
        c2, e2 = total_cost(t2, frames, g2, p)
        v2 = max(abs(e2.w - p.target_w) - limit, abs(e2.h - p.target_h) - limit, 0.0)
        if v2 > 0 and v2 >= viol:  # outside the hard window and not improving
            continue
        if c2 <= c or rng.random() < math.exp((c - c2) / T):
            t, g, c, e, viol = t2, g2, c2, e2, v2
            if c < best_all.cost:
                best_all = Candidate(copy.deepcopy(t), g, c, run)
            b = best.get(e.n)
            if b is None or c < b.cost:
                best[e.n] = Candidate(copy.deepcopy(t), g, c, run)
    out = [best_all] + [b for b in best.values() if b.cost <= best_all.cost + 6.0]
    return out


def _worker(args):
    frames, p, cfg, run = args
    return anneal(frames, p, cfg, run)


def run_search(frames: list[Frame], p: Params, cfg: SearchConfig, progress=print) -> list[Candidate]:
    """Run ``cfg.runs`` annealing runs on ``cfg.jobs`` processes.

    With ``cfg.minutes`` set, the runs are executed in waves of ``jobs`` and each run gets
    an equal share of the budget, so the whole search ends after about ``minutes``.
    """
    workers = cfg.jobs or os.cpu_count() or 1
    if cfg.minutes is not None:
        waves = math.ceil(cfg.runs / workers)
        cfg.run_seconds = cfg.minutes * 60.0 / waves
        progress(f"time budget {cfg.minutes:g} min: {cfg.runs} runs in {waves} wave(s) of {workers} processes, "
                 f"{cfg.run_seconds / 60:.1f} min per run")
    jobs = [(frames, p, cfg, run) for run in range(cfg.runs)]
    results: list[Candidate] = []
    if cfg.jobs == 1:
        for job in jobs:
            results.extend(_worker(job))
            progress(f"run {job[3] + 1}/{cfg.runs} done")
    else:
        with Pool(cfg.jobs) as pool:
            for i, res in enumerate(pool.imap_unordered(_worker, jobs)):
                results.extend(res)
                progress(f"run {i + 1}/{cfg.runs} done (best cost so far {min(r.cost for r in results):.2f})")
    return results
