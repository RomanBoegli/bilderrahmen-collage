"""Simulated annealing over slicing trees (subset selection included)."""
from __future__ import annotations

import copy
import math
import random
from dataclasses import dataclass
from multiprocessing import Pool

from .inventory import Frame
from .layout import Params, Tree, is_leaf, leaves, size_violation, total_cost


@dataclass
class SearchConfig:
    iters: int = 1_000_000
    runs: int = 32
    seed: int = 0
    g_min: float = 1.5
    g_max: float = 6.0
    g_step: float = 0.5
    g_fixed: float | None = None
    min_frames: int = 10
    t_start: float = 10.0
    t_end: float = 0.02
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


def replace(t: Tree, path: tuple, value: Tree) -> Tree:
    """Replace the subtree at ``path`` (in place); returns the (possibly new) root."""
    if not path:
        return value
    node = t
    for i in path[:-1]:
        node = node[i]
    node[path[-1]] = value
    return t


def chain(items: list[Tree], op: str, rng: random.Random | None = None) -> Tree:
    t = items[0]
    for x in items[1:]:
        t = [op, t, x]
    return t


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
    while sum(len(r) for r in rows) < min_frames and row:
        rows.append(row)
        row = []
    return chain([chain(r, "V") for r in rows], "H")


def mutate(t: Tree, g: float, frames: list[Frame], cfg: SearchConfig, rng: random.Random) -> tuple[Tree, float]:
    t = copy.deepcopy(t)
    r = rng.random()
    paths = node_paths(t)
    leaf_paths = [q for q in paths if is_leaf(get(t, q))]
    inner_paths = [q for q in paths if not is_leaf(get(t, q))]
    used = set(leaves(t))
    unused = [i for i in range(len(frames)) if i not in used]
    nleaves = len(leaf_paths)

    if cfg.g_fixed is None and r < 0.05:
        step = cfg.g_step * rng.choice((-1, 1))
        g = min(cfg.g_max, max(cfg.g_min, round(g + step, 3)))
        return t, g
    if r < 0.22 and inner_paths:  # flip cut direction
        n = get(t, rng.choice(inner_paths))
        n[0] = "H" if n[0] == "V" else "V"
    elif r < 0.30 and inner_paths:  # swap children
        n = get(t, rng.choice(inner_paths))
        n[1], n[2] = n[2], n[1]
    elif r < 0.42 and nleaves >= 2:  # swap two leaves
        pa, pb = rng.sample(leaf_paths, 2)
        a, b = get(t, pa), get(t, pb)
        replace(t, pa, b)
        replace(t, pb, a)
    elif r < 0.50 and len(paths) >= 4:  # swap two disjoint subtrees
        pa, pb = rng.sample(paths[1:], 2)
        if pa[: len(pb)] != pb and pb[: len(pa)] != pa:
            a, b = get(t, pa), get(t, pb)
            replace(t, pa, b)
            replace(t, pb, a)
    elif r < 0.63 and nleaves >= 3:  # move a leaf somewhere else
        pa = rng.choice(leaf_paths)
        leaf = get(t, pa)
        sibling = get(t, pa[:-1] + (3 - pa[-1],))
        t = replace(t, pa[:-1], sibling)
        q = rng.choice(node_paths(t))
        sub = get(t, q)
        new = [rng.choice("HV"), sub, leaf] if rng.random() < 0.5 else [rng.choice("HV"), leaf, sub]
        t = replace(t, q, new)
    elif r < 0.78 and unused:  # replace a leaf by an unused frame of another type
        pa = rng.choice(leaf_paths)
        cur = get(t, pa)
        cand = [i for i in unused if frames[i].type != frames[cur].type]
        if cand:
            replace(t, pa, rng.choice(cand))
    elif r < 0.90 and unused:  # add a frame
        q = rng.choice(paths)
        sub = get(t, q)
        leaf = rng.choice(unused)
        new = [rng.choice("HV"), sub, leaf] if rng.random() < 0.5 else [rng.choice("HV"), leaf, sub]
        t = replace(t, q, new)
    elif nleaves > cfg.min_frames:  # remove a frame
        pa = rng.choice(leaf_paths)
        sibling = get(t, pa[:-1] + (3 - pa[-1],))
        t = replace(t, pa[:-1], sibling)
    return t, g


# ------------------------------------------------------------------------------ annealing


def anneal(frames: list[Frame], p: Params, cfg: SearchConfig, run: int) -> list[Candidate]:
    """One annealing run; returns the best state plus the best state per frame count."""
    rng = random.Random(cfg.seed * 1000 + run)
    gs = []
    x = cfg.g_min
    while x <= cfg.g_max + 1e-9:
        gs.append(round(x, 3))
        x += cfg.g_step
    g = cfg.g_fixed if cfg.g_fixed is not None else rng.choice(gs)
    t = greedy_init(frames, g, p, rng, cfg.min_frames)
    c, e = total_cost(t, frames, g, p)
    viol = size_violation(e.w, e.h, p)
    best: dict[int, Candidate] = {e.n: Candidate(copy.deepcopy(t), g, c, run)}
    best_all = Candidate(copy.deepcopy(t), g, c, run)
    limit = p.tol + p.hard_margin
    log_ratio = math.log(cfg.t_end / cfg.t_start)
    for k in range(cfg.iters):
        T = cfg.t_start * math.exp(log_ratio * k / cfg.iters)
        t2, g2 = mutate(t, g, frames, cfg, rng)
        c2, e2 = total_cost(t2, frames, g2, p)
        v2 = max(abs(e2.w - p.target_w) - limit, abs(e2.h - p.target_h) - limit, 0.0)
        if v2 > 0 and v2 >= viol:  # outside the hard window and not improving
            continue
        if c2 <= c or rng.random() < math.exp((c - c2) / T):
            t, g, c, viol = t2, g2, c2, v2
            if c < best_all.cost:
                best_all = Candidate(copy.deepcopy(t), g, c, run)
            b = best.get(e2.n)
            if b is None or c < b.cost:
                best[e2.n] = Candidate(copy.deepcopy(t), g, c, run)
    out = [best_all] + [b for b in best.values() if b.cost <= best_all.cost + 6.0]
    return out


def _worker(args):
    frames, p, cfg, run = args
    return anneal(frames, p, cfg, run)


def run_search(frames: list[Frame], p: Params, cfg: SearchConfig, progress=print) -> list[Candidate]:
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
