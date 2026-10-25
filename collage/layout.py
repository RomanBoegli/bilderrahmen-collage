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
    w: float
    h: float
    cost: float = 0.0
    # stretch bookkeeping per direction: cap = total gap weight that can absorb slack
    # (0 = cannot stretch); phi = fraction of absorbed slack that lands on the worst gap
    cap_w: float = 0.0
    phi_w: float = 0.0
    cap_h: float = 0.0
    phi_h: float = 0.0
    max_dev: float = 0.0  # largest single-gap deviation implied by slack absorption
    max_notch: float = 0.0  # largest unavoidable notch depth at the border
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
    if op == "V":
        ea = evaluate(a, frames, g, p, bt, bb, bl, False)
        eb = evaluate(b, frames, g, p, bt, bb, False, br)
        w = ea.w + g + eb.w
        h = max(ea.h, eb.h)
        s = abs(ea.h - eb.h)
        short = ea if ea.h < eb.h else eb
        cap_s, phi_s = short.cap_h, short.phi_h
        both, one = (bt and bb), (bt or bb)
        omega = gap_weight(ea.w, eb.w, p)
        cap_w = omega + ea.cap_w + eb.cap_w
        phi_w = max(omega, ea.phi_w * ea.cap_w, eb.phi_w * eb.cap_w) / cap_w
        if ea.cap_h > 0 and eb.cap_h > 0:
            cap_h, phi_h = min(ea.cap_h, eb.cap_h), max(ea.phi_h, eb.phi_h)
        else:
            cap_h, phi_h = 0.0, 0.0
    else:
        ea = evaluate(a, frames, g, p, bt, False, bl, br)
        eb = evaluate(b, frames, g, p, False, bb, bl, br)
        h = ea.h + g + eb.h
        w = max(ea.w, eb.w)
        s = abs(ea.w - eb.w)
        short = ea if ea.w < eb.w else eb
        cap_s, phi_s = short.cap_w, short.phi_w
        both, one = (bl and br), (bl or br)
        omega = gap_weight(ea.h, eb.h, p)
        cap_h = omega + ea.cap_h + eb.cap_h
        phi_h = max(omega, ea.phi_h * ea.cap_h, eb.phi_h * eb.cap_h) / cap_h
        if ea.cap_w > 0 and eb.cap_w > 0:
            cap_w, phi_w = min(ea.cap_w, eb.cap_w), max(ea.phi_w, eb.phi_w)
        else:
            cap_w, phi_w = 0.0, 0.0

    cost = ea.cost + eb.cost
    max_dev = max(ea.max_dev, eb.max_dev)
    max_notch = max(ea.max_notch, eb.max_notch)
    if s > EPS:
        if cap_s > 0:  # absorbed by inner gaps of the smaller child
            d = s * phi_s
            cost += p.w_dev * huber(d, p)
            max_dev = max(max_dev, d)
        elif both:  # touches both opposite borders -> notch unavoidable
            cost += p.w_notch * huber(s, p)
            max_notch = max(max_notch, s)
        elif one:  # flush with the border, inner gap grows by s
            cost += p.w_dev * huber(s, p)
            max_dev = max(max_dev, s)
        else:  # centred, both neighbouring gaps grow by s/2
            cost += 2 * p.w_dev * huber(s / 2, p)
            max_dev = max(max_dev, s / 2)
    return Eval(w, h, cost, cap_w, phi_w, cap_h, phi_h, max_dev, max_notch, ea.n + eb.n)


def size_violation(w: float, h: float, p: Params) -> float:
    return max(0.0, abs(w - p.target_w) - p.tol) + max(0.0, abs(h - p.target_h) - p.tol)


def total_cost(t: Tree, frames: list[Frame], g: float, p: Params) -> tuple[float, Eval]:
    e = evaluate(t, frames, g, p)
    dw, dh = abs(e.w - p.target_w), abs(e.h - p.target_h)
    c = e.cost + p.w_size * size_violation(e.w, e.h, p) + p.w_pull * (dw + dh) - p.frame_bonus * e.n
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
    """Return rectangles (origin top-left, y downwards) and the outer size."""
    root = evaluate(t, frames, g, p)
    rects: list[Rect] = []
    _place(t, frames, g, p, 0.0, 0.0, root.w, root.h, True, True, True, True, rects)
    rects.sort(key=lambda r: (r.y, r.x))
    check_placement(rects, root.w, root.h)
    return rects, root.w, root.h


def _place(t, frames, g, p, x, y, W, H, bt, bb, bl, br, out):
    if is_leaf(t):
        f = frames[t]
        ex, ey = W - f.w, H - f.h
        assert ex > -1e-6 and ey > -1e-6, "allotted box smaller than frame"
        dx = 0.0 if bl else (ex if br else ex / 2)
        dy = 0.0 if bt else (ey if bb else ey / 2)
        out.append(Rect(t, x + dx, y + dy, f.w, f.h))
        return
    op, a, b = t
    ea = evaluate(a, frames, g, p)
    eb = evaluate(b, frames, g, p)
    if op == "V":
        extra = W - (ea.w + g + eb.w)
        assert extra > -1e-6
        omega = gap_weight(ea.w, eb.w, p)
        cap = omega + ea.cap_w + eb.cap_w
        gap = g + extra * omega / cap
        wa = ea.w + extra * ea.cap_w / cap
        wb = eb.w + extra * eb.cap_w / cap
        _place(a, frames, g, p, x, y, wa, H, bt, bb, bl, False, out)
        _place(b, frames, g, p, x + wa + gap, y, wb, H, bt, bb, False, br, out)
    else:
        extra = H - (ea.h + g + eb.h)
        assert extra > -1e-6
        omega = gap_weight(ea.h, eb.h, p)
        cap = omega + ea.cap_h + eb.cap_h
        gap = g + extra * omega / cap
        ha = ea.h + extra * ea.cap_h / cap
        hb = eb.h + extra * eb.cap_h / cap
        _place(a, frames, g, p, x, y, W, ha, bt, False, bl, br, out)
        _place(b, frames, g, p, x, y + ha + gap, W, hb, False, bb, bl, br, out)


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

    @property
    def notch_len(self) -> float:
        return sum(self.uncovered.values())


def _overlap(a0, a1, b0, b1) -> float:
    return min(a1, b1) - max(a0, b0)


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
            if b.x >= a.x2 - EPS and _overlap(a.y, a.y2, b.y, b.y2) > EPS:
                y0, y1 = max(a.y, b.y), min(a.y2, b.y2)
                blocked = any(
                    k is not a and k is not b and k.x >= a.x2 - EPS and k.x2 <= b.x + EPS
                    and _overlap(y0, y1, k.y, k.y2) > EPS for k in rects
                )
                if not blocked:
                    gaps.append(Gap(i, j, True, b.x - a.x2, (a.x2 + b.x) / 2, (y0 + y1) / 2))
            # vertical neighbour: b below a
            if b.y >= a.y2 - EPS and _overlap(a.x, a.x2, b.x, b.x2) > EPS:
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


def metrics(rects: list[Rect], W: float, H: float, g: float) -> Metrics:
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
    )


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
