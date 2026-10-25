"""Slicing-tree layout model.

A layout is a binary *slicing tree* over a subset of frames:

* leaf  -> frame id (int)
* inner -> ``[op, a, b]`` with ``op == "V"`` (a left of b) or ``op == "H"`` (a above b)

Siblings are separated by the nominal gap ``g``.  When the two children of a node
do not have the same height (``V``) / width (``H``) the smaller child has *slack*.
Slack is absorbed by widening the gaps inside that child ("stretch"); if the child
has no inner gaps in that direction it becomes dead space, which is pushed against
the outer border (if the child touches it) or centred.

``evaluate`` computes sizes, a cost that penalises non-uniform gaps and border
notches, and bookkeeping about how slack can be absorbed.  ``place`` turns a tree
into rectangles using exactly the same absorption rule, and ``metrics`` measures
the *actual* gaps and border coverage of the placed rectangles.
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

from .inventory import Frame

EPS = 1e-6
Tree = int | list  # leaf id or [op, a, b]


@dataclass
class Params:
    target_w: float = 180.0
    target_h: float = 120.0
    tol: float = 10.0  # allowed deviation from the target size (cm)
    delta: float = 0.5  # gap deviation that is considered harmless (cm)
    huber_d0: float = 2.0  # beyond this deviation the penalty grows linearly
    w_dev: float = 1.0  # weight of gap deviations
    w_notch: float = 3.0  # weight of notches in the outer border
    w_size: float = 20.0  # per cm outside target +- tol
    w_pull: float = 0.1  # per cm away from the target size
    frame_bonus: float = 1.5  # bonus per frame used
    slack_rule: str = "even"  # "even" | "proportional"
    hard_margin: float = 5.0  # states further than tol+margin from the target are rejected
    cost_shape: str = "huber"  # "huber" | "linear"
    # optional constraints (sub-analysis); all off by default
    n_types: int = 9  # distinct frame formats in the inventory (normalises the type-mix entropy)
    large_area: float = 2000.0  # frames with at least this outer area (cm^2) count as "large"
    large_min: int = 0  # required number of large frames (0 = no constraint)
    large_max: int = 99
    w_large: float = 30.0  # per large frame missing or too many
    large_apart: bool = False  # keep large frames away from each other
    w_touch: float = 1.0  # per cm of shared edge between two large frames that are direct neighbours
    large_min_dist: float = 70.0  # wanted minimum centre distance between large frames (cm)
    w_dist: float = 0.5  # per cm the closest pair of large frames falls short of large_min_dist
    w_entropy: float = 0.0  # weight of (1 - entropy); 0 = off

    @property
    def needs_placement(self) -> bool:
        """True if the cost has terms that need the placed rectangles (not just the tree)."""
        return self.large_min > 0 or self.large_max < 99 or self.large_apart or self.w_entropy > 0


def huber(d: float, p: Params) -> float:
    """Quadratic penalty in units of ``delta`` up to ``huber_d0``, linear beyond."""
    if d <= 0:
        return 0.0
    if p.cost_shape == "linear":
        return d / p.delta
    if d <= p.huber_d0:
        return (d / p.delta) ** 2
    return (p.huber_d0 / p.delta) ** 2 + 2 * p.huber_d0 / p.delta**2 * (d - p.huber_d0)


@dataclass
class Eval:
    """Annotated node: natural size plus how slack can be absorbed below it."""

    w: float
    h: float
    op: str | None = None  # None for a leaf
    a: "Eval | None" = None
    b: "Eval | None" = None
    omega: float = 0.0  # weight of the gap between a and b (slack distribution)
    # stretch capacity per direction: total gap weight that can absorb extra size (0 = rigid)
    cap_w: float = 0.0
    cap_h: float = 0.0
    cost: float = 0.0
    max_dev: float = 0.0  # largest single-gap deviation implied by slack absorption
    max_notch: float = 0.0  # largest notch depth at the border
    n: int = 1


def is_leaf(t: Tree) -> bool:
    return isinstance(t, int)


def leaves(t: Tree, acc: list[int] | None = None) -> list[int]:
    if acc is None:
        acc = []
    if is_leaf(t):
        acc.append(t)
    else:
        leaves(t[1], acc)
        leaves(t[2], acc)
    return acc


def gap_weight(ext_a: float, ext_b: float, p: Params) -> float:
    """Weight of the gap between two siblings for slack distribution."""
    if p.slack_rule == "proportional":
        return (ext_a + ext_b) / 2.0
    return 1.0


def child_flags(op: str, bt: bool, bb: bool, bl: bool, br: bool):
    """Border flags of the two children of a node with the given flags."""
    if op == "V":
        return (bt, bb, bl, False), (bt, bb, False, br)
    return (bt, False, bl, br), (False, bb, bl, br)


def stretch_cost(e: Eval, s: float, vertical: bool, flags: tuple, p: Params) -> tuple[float, float, float]:
    """Cost of making node ``e`` larger by ``s`` in height (vertical) or width.

    Mirrors ``_place``: the extra size is spread over the node's gaps (weights ``omega``)
    and its stretchable children; a rigid node (leaf) turns it into dead space.
    Returns (cost, max gap deviation, max notch depth).
    """
    if s <= EPS:
        return 0.0, 0.0, 0.0
    bt, bb, bl, br = flags
    if e.op is None:  # leaf: dead space
        if vertical:
            notch = (bt and bb) or bl or br
            flush = bt or bb
        else:
            notch = (bl and br) or bt or bb
            flush = bl or br
        if notch:  # hole visible in the outer border
            return p.w_notch * huber(s, p), 0.0, s
        if flush:  # flush with the border, the inner gap grows by s
            return p.w_dev * huber(s, p), s, 0.0
        return 2 * p.w_dev * huber(s / 2, p), s / 2, 0.0  # centred: both gaps grow by s/2
    fa, fb = child_flags(e.op, bt, bb, bl, br)
    along = (e.op == "H") == vertical  # stretch direction follows the node's own gap
    if along:
        cap = e.omega + (e.a.cap_h + e.b.cap_h if vertical else e.a.cap_w + e.b.cap_w)
        d_gap = s * e.omega / cap
        sa = s * (e.a.cap_h if vertical else e.a.cap_w) / cap
        sb = s * (e.b.cap_h if vertical else e.b.cap_w) / cap
        ca, da, na = stretch_cost(e.a, sa, vertical, fa, p)
        cb, db, nb = stretch_cost(e.b, sb, vertical, fb, p)
        return p.w_dev * huber(d_gap, p) + ca + cb, max(d_gap, da, db), max(na, nb)
    # across: both children must grow by s
    ca, da, na = stretch_cost(e.a, s, vertical, fa, p)
    cb, db, nb = stretch_cost(e.b, s, vertical, fb, p)
    return ca + cb, max(da, db), max(na, nb)


def evaluate(
    t: Tree,
    frames: list[Frame],
    g: float,
    p: Params,
    bt: bool = True,
    bb: bool = True,
    bl: bool = True,
    br: bool = True,
) -> Eval:
    """Bottom-up evaluation; border flags say which outer edges the node touches."""
    if is_leaf(t):
        f = frames[t]
        return Eval(f.w, f.h)
    op, a, b = t
    fa, fb = child_flags(op, bt, bb, bl, br)
    ea = evaluate(a, frames, g, p, *fa)
    eb = evaluate(b, frames, g, p, *fb)
    if op == "V":
        w, h = ea.w + g + eb.w, max(ea.h, eb.h)
        omega = gap_weight(ea.w, eb.w, p)
        cap_w = omega + ea.cap_w + eb.cap_w
        cap_h = min(ea.cap_h, eb.cap_h) if (ea.cap_h > 0 and eb.cap_h > 0) else 0.0
        short, flags, s = (ea, fa, eb.h - ea.h) if ea.h < eb.h else (eb, fb, ea.h - eb.h)
        c, d, nt = stretch_cost(short, s, True, flags, p)
    else:
        w, h = max(ea.w, eb.w), ea.h + g + eb.h
        omega = gap_weight(ea.h, eb.h, p)
        cap_h = omega + ea.cap_h + eb.cap_h
        cap_w = min(ea.cap_w, eb.cap_w) if (ea.cap_w > 0 and eb.cap_w > 0) else 0.0
        short, flags, s = (ea, fa, eb.w - ea.w) if ea.w < eb.w else (eb, fb, ea.w - eb.w)
        c, d, nt = stretch_cost(short, s, False, flags, p)
    return Eval(
        w, h, op, ea, eb, omega, cap_w, cap_h,
        cost=ea.cost + eb.cost + c,
        max_dev=max(ea.max_dev, eb.max_dev, d),
        max_notch=max(ea.max_notch, eb.max_notch, nt),
        n=ea.n + eb.n,
    )


def size_violation(w: float, h: float, p: Params) -> float:
    return max(0.0, abs(w - p.target_w) - p.tol) + max(0.0, abs(h - p.target_h) - p.tol)


def total_cost(t: Tree, frames: list[Frame], g: float, p: Params) -> tuple[float, Eval]:
    e = evaluate(t, frames, g, p)
    dw, dh = abs(e.w - p.target_w), abs(e.h - p.target_h)
    c = e.cost + p.w_size * size_violation(e.w, e.h, p) + p.w_pull * (dw + dh) - p.frame_bonus * e.n
    if p.needs_placement:
        c += extras_cost(extras(place_from_eval(t, e, frames, g, p), g, p), p)
    return c, e


# ----------------------------------------------------------------------------- placement


@dataclass
class Rect:
    frame: int
    x: float
    y: float
    w: float
    h: float

    @property
    def x2(self) -> float:
        return self.x + self.w

    @property
    def y2(self) -> float:
        return self.y + self.h


def place(t: Tree, frames: list[Frame], g: float, p: Params) -> tuple[list[Rect], float, float]:
    """Return rectangles (origin top-left, y downwards, sorted by row) and the outer size."""
    root = evaluate(t, frames, g, p)
    rects = place_from_eval(t, root, frames, g, p)
    rects.sort(key=lambda r: (r.y, r.x))
    check_placement(rects, root.w, root.h)
    return rects, root.w, root.h


def place_from_eval(t: Tree, e: Eval, frames: list[Frame], g: float, p: Params) -> list[Rect]:
    """Rectangles for an already evaluated tree (no re-evaluation, no checks; tree order)."""
    rects: list[Rect] = []
    _place(t, e, frames, g, p, 0.0, 0.0, e.w, e.h, True, True, True, True, rects)
    return rects


def _place(t, e, frames, g, p, x, y, W, H, bt, bb, bl, br, out):
    if is_leaf(t):
        f = frames[t]
        ex, ey = W - f.w, H - f.h
        assert ex > -1e-6 and ey > -1e-6, "allotted box smaller than frame"
        dx = 0.0 if bl else (ex if br else ex / 2)
        dy = 0.0 if bt else (ey if bb else ey / 2)
        out.append(Rect(t, x + dx, y + dy, f.w, f.h))
        return
    op, a, b = t
    ea, eb = e.a, e.b
    if op == "V":
        extra = W - (ea.w + g + eb.w)
        assert extra > -1e-6
        omega = gap_weight(ea.w, eb.w, p)
        cap = omega + ea.cap_w + eb.cap_w
        gap = g + extra * omega / cap
        wa = ea.w + extra * ea.cap_w / cap
        wb = eb.w + extra * eb.cap_w / cap
        _place(a, ea, frames, g, p, x, y, wa, H, bt, bb, bl, False, out)
        _place(b, eb, frames, g, p, x + wa + gap, y, wb, H, bt, bb, False, br, out)
    else:
        extra = H - (ea.h + g + eb.h)
        assert extra > -1e-6
        omega = gap_weight(ea.h, eb.h, p)
        cap = omega + ea.cap_h + eb.cap_h
        gap = g + extra * omega / cap
        ha = ea.h + extra * ea.cap_h / cap
        hb = eb.h + extra * eb.cap_h / cap
        _place(a, ea, frames, g, p, x, y, W, ha, bt, False, bl, br, out)
        _place(b, eb, frames, g, p, x, y + ha + gap, W, hb, False, bb, bl, br, out)


def check_placement(rects: list[Rect], W: float, H: float) -> None:
    for r in rects:
        assert r.x > -EPS and r.y > -EPS and r.x2 < W + EPS and r.y2 < H + EPS, "frame outside box"
    for i, a in enumerate(rects):
        for b in rects[i + 1 :]:
            if a.x < b.x2 - EPS and b.x < a.x2 - EPS and a.y < b.y2 - EPS and b.y < a.y2 - EPS:
                raise AssertionError(f"frames overlap: {a} {b}")


# ------------------------------------------------------------------------------- metrics


@dataclass
class Gap:
    a: int  # index into rects
    b: int
    horizontal: bool  # True: b is right of a; False: b is below a
    size: float
    mx: float  # midpoint of the gap (for annotations)
    my: float


@dataclass
class Metrics:
    gaps: list[Gap]
    max_dev: float
    mean_dev: float
    uncovered: dict[str, float]  # per border side: notch length (holes that are not regular gaps)
    corners: int  # number of filled corners (0..4)
    dead_frac: float  # share of the bounding box that is neither frame nor nominal gap
    extras: "Extras" = field(default_factory=lambda: Extras())

    @property
    def notch_len(self) -> float:
        return sum(self.uncovered.values())


def _overlap(a0, a1, b0, b1) -> float:
    return min(a1, b1) - max(a0, b0)


MIN_SHARED_EDGE = 5.0  # two frames are neighbours only if they share at least this much edge (cm)


def neighbour_gaps(rects: list[Rect]) -> list[Gap]:
    gaps: list[Gap] = []
    n = len(rects)
    for i in range(n):
        a = rects[i]
        for j in range(n):
            if i == j:
                continue
            b = rects[j]
            # horizontal neighbour: b right of a
            if b.x >= a.x2 - EPS and _overlap(a.y, a.y2, b.y, b.y2) >= MIN_SHARED_EDGE:
                y0, y1 = max(a.y, b.y), min(a.y2, b.y2)
                blocked = any(
                    k is not a and k is not b and k.x >= a.x2 - EPS and k.x2 <= b.x + EPS
                    and _overlap(y0, y1, k.y, k.y2) > EPS for k in rects
                )
                if not blocked:
                    gaps.append(Gap(i, j, True, b.x - a.x2, (a.x2 + b.x) / 2, (y0 + y1) / 2))
            # vertical neighbour: b below a
            if b.y >= a.y2 - EPS and _overlap(a.x, a.x2, b.x, b.x2) >= MIN_SHARED_EDGE:
                x0, x1 = max(a.x, b.x), min(a.x2, b.x2)
                blocked = any(
                    k is not a and k is not b and k.y >= a.y2 - EPS and k.y2 <= b.y + EPS
                    and _overlap(x0, x1, k.x, k.x2) > EPS for k in rects
                )
                if not blocked:
                    gaps.append(Gap(i, j, False, b.y - a.y2, (x0 + x1) / 2, (a.y2 + b.y) / 2))
    return gaps


def _notch_length(intervals: list[tuple[float, float]], length: float, g: float) -> float:
    """Length of a border side not accounted for by frames or regular gaps.

    ``intervals`` are the extents of frames touching that side.  Holes between two
    consecutive frames that are at most ``g + 1`` wide are ordinary gaps; wider holes and
    holes at the ends of the side are notches.
    """
    if not intervals:
        return length
    merged: list[list[float]] = []
    for lo, hi in sorted(intervals):
        if merged and lo <= merged[-1][1] + EPS:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    notch = merged[0][0] + (length - merged[-1][1])
    for (_, hi), (lo, _) in zip(merged, merged[1:]):
        hole = lo - hi
        if hole > g + 1.0:
            notch += hole
    return max(0.0, notch)


def metrics(rects: list[Rect], W: float, H: float, g: float, p: Params | None = None) -> Metrics:
    gaps = neighbour_gaps(rects)
    devs = [abs(x.size - g) for x in gaps]
    uncovered = {
        "top": _notch_length([(r.x, r.x2) for r in rects if r.y < EPS], W, g),
        "bottom": _notch_length([(r.x, r.x2) for r in rects if r.y2 > H - EPS], W, g),
        "left": _notch_length([(r.y, r.y2) for r in rects if r.x < EPS], H, g),
        "right": _notch_length([(r.y, r.y2) for r in rects if r.x2 > W - EPS], H, g),
    }
    corners = 0
    for left in (True, False):
        for top in (True, False):
            corners += any(
                (abs(r.x) < EPS if left else abs(r.x2 - W) < EPS)
                and (abs(r.y) < EPS if top else abs(r.y2 - H) < EPS)
                for r in rects
            )
    frame_area = sum(r.w * r.h for r in rects)
    # nominal gap area: every neighbour gap counted with its nominal size times its shared edge
    nominal = 0.0
    for x in gaps:
        a, b = rects[x.a], rects[x.b]
        edge = _overlap(a.y, a.y2, b.y, b.y2) if x.horizontal else _overlap(a.x, a.x2, b.x, b.x2)
        nominal += g * edge
    dead = max(0.0, W * H - frame_area - nominal) / (W * H)
    return Metrics(
        gaps=gaps,
        max_dev=max(devs) if devs else 0.0,
        mean_dev=sum(devs) / len(devs) if devs else 0.0,
        uncovered={k: max(0.0, round(v, 3)) for k, v in uncovered.items()},
        corners=corners,
        dead_frac=dead,
        extras=extras(rects, g, p or Params()),
    )


# ------------------------------------------------- optional constraints: large frames, entropy


@dataclass
class Extras:
    """Measures for the optional constraints: large-frame placement and 'entropy' (irregularity)."""

    n_large: int = 0
    large_touch: float = 0.0  # total shared edge (cm) of large-frame pairs that are direct neighbours
    large_touch_pairs: int = 0
    large_min_dist: float = math.inf  # smallest centre distance between two large frames
    type_mix: float = 0.0  # normalised Shannon entropy of the frame formats used
    variety: float = 0.0  # share of neighbouring pairs (across a regular gap) with different formats
    stagger: float = 0.0  # share of distinct frame edge positions (grids score low)
    through_lines: int = 0  # straight gap lines running across the whole collage
    lines: float = 0.0  # 1 for a single through-line, 0 for five or more

    @property
    def entropy(self) -> float:
        """Irregularity of the constellation in [0, 1]: mean of the four components."""
        return (self.type_mix + self.variety + self.stagger + self.lines) / 4.0


def regular_neighbours(rects: list[Rect], g: float) -> list[tuple[int, int, float]]:
    """Pairs (i, j, shared edge) of frames facing each other across a gap of at most g + 1 cm.

    Nothing can lie in such a narrow gap, so no blocking test is needed (unlike ``neighbour_gaps``).
    """
    n = len(rects)
    lim = g + 1.0 + EPS
    xs = sorted(range(n), key=lambda i: rects[i].x)
    xk = [rects[i].x for i in xs]
    ys = sorted(range(n), key=lambda i: rects[i].y)
    yk = [rects[i].y for i in ys]
    pairs: list[tuple[int, int, float]] = []
    for i, a in enumerate(rects):
        for k in range(bisect_left(xk, a.x2 - EPS), bisect_right(xk, a.x2 + lim)):
            b = rects[xs[k]]
            ov = min(a.y2, b.y2) - max(a.y, b.y)
            if ov >= MIN_SHARED_EDGE:
                pairs.append((i, xs[k], ov))
        for k in range(bisect_left(yk, a.y2 - EPS), bisect_right(yk, a.y2 + lim)):
            b = rects[ys[k]]
            ov = min(a.x2, b.x2) - max(a.x, b.x)
            if ov >= MIN_SHARED_EDGE:
                pairs.append((i, ys[k], ov))
    return pairs


def _breaks(intervals: list[tuple[float, float]]) -> int:
    """Number of holes in the union of the intervals."""
    intervals.sort()
    breaks = 0
    end = intervals[0][1]
    for lo, hi in intervals[1:]:
        if lo > end + EPS:
            breaks += 1
        end = max(end, hi)
    return breaks


def through_lines(rects: list[Rect]) -> int:
    """Straight gap lines that cross the whole collage (vertical + horizontal)."""
    if not rects:
        return 0
    return _breaks([(r.x, r.x2) for r in rects]) + _breaks([(r.y, r.y2) for r in rects])


def extras(rects: list[Rect], g: float, p: Params) -> Extras:
    x = Extras()
    n = len(rects)
    if n == 0:
        return x
    large = [i for i, r in enumerate(rects) if r.w * r.h >= p.large_area - EPS]
    x.n_large = len(large)
    pairs = regular_neighbours(rects, g)
    if len(large) >= 2:
        ls = set(large)
        for i, j, ov in pairs:
            if i in ls and j in ls:
                x.large_touch += ov
                x.large_touch_pairs += 1
        for a in range(len(large)):
            ra = rects[large[a]]
            for b in range(a + 1, len(large)):
                rb = rects[large[b]]
                d = math.hypot((ra.x + ra.x2 - rb.x - rb.x2) / 2, (ra.y + ra.y2 - rb.y - rb.y2) / 2)
                x.large_min_dist = min(x.large_min_dist, d)
    counts: dict[tuple[float, float], int] = {}
    for r in rects:
        counts[(r.w, r.h)] = counts.get((r.w, r.h), 0) + 1
    ent = -sum(c / n * math.log(c / n) for c in counts.values())
    x.type_mix = ent / math.log(p.n_types) if p.n_types > 1 else 0.0
    if pairs:
        diff = sum(1 for i, j, _ in pairs if (rects[i].w, rects[i].h) != (rects[j].w, rects[j].h))
        x.variety = diff / len(pairs)
    ex = {round(v, 1) for r in rects for v in (r.x, r.x2)}
    ey = {round(v, 1) for r in rects for v in (r.y, r.y2)}
    x.stagger = (len(ex) + len(ey)) / (4.0 * n)
    x.through_lines = through_lines(rects)
    x.lines = max(0.0, 1.0 - (x.through_lines - 1) / 4.0)
    return x


def extras_cost(x: Extras, p: Params) -> float:
    c = 0.0
    if p.large_min > 0 or p.large_max < 99:
        c += p.w_large * (max(0, p.large_min - x.n_large) + max(0, x.n_large - p.large_max))
    if p.large_apart and x.n_large >= 2:
        c += p.w_touch * x.large_touch + p.w_dist * max(0.0, p.large_min_dist - x.large_min_dist)
    if p.w_entropy > 0:
        c += p.w_entropy * (1.0 - x.entropy)
    return c


def signature(rects: list[Rect], frames: list[Frame], W: float, H: float) -> tuple:
    """Canonical signature of a placement, identical for mirror images."""
    variants = []
    for fx in (False, True):
        for fy in (False, True):
            sig = []
            for r in rects:
                x = W - r.x2 if fx else r.x
                y = H - r.y2 if fy else r.y
                sig.append((frames[r.frame].type, round(x, 1), round(y, 1)))
            variants.append(tuple(sorted(sig)))
    return min(variants)
