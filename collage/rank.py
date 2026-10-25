"""Ranking of candidate states and selection of the layouts to render.

Shared by the single-run CLI (``collage``) and the multi-run report (``collage-report``).
"""
from __future__ import annotations

from collections.abc import Iterable

from .inventory import Frame
from .layout import Params, metrics, place, signature
from .render import Layout
from .search import Candidate


def parse_size(s: str) -> tuple[float, float]:
    w, h = s.lower().replace("×", "x").split("x")
    return float(w), float(h)


def parse_count_range(s: str | None) -> tuple[int, int]:
    """'2-3' -> (2, 3); '3' -> (3, 3); None -> (0, 99) = unconstrained."""
    if not s:
        return 0, 99
    lo, _, hi = str(s).partition("-")
    return int(lo), int(hi or lo)


def params_from_args(a: dict, n_types: int | None = None) -> Params:
    """Rebuild the cost/placement parameters from the ``args`` stored in a candidates.json."""
    tw, th = parse_size(a.get("target", "180x120"))
    d = Params()
    lmin, lmax = parse_count_range(a.get("large_count"))
    return Params(
        target_w=tw,
        target_h=th,
        tol=a.get("tol", d.tol),
        delta=a.get("delta", d.delta),
        frame_bonus=a.get("frame_bonus", d.frame_bonus),
        slack_rule=a.get("slack_rule", d.slack_rule),
        cost_shape=a.get("cost_shape", d.cost_shape),
        hard_margin=a.get("hard_margin", d.hard_margin),
        n_types=n_types if n_types is not None else d.n_types,
        large_area=a.get("large_area", d.large_area),
        large_min=lmin,
        large_max=lmax,
        large_apart=bool(a.get("large_apart", False)),
        large_min_dist=a.get("large_min_dist", d.large_min_dist),
        w_entropy=a.get("entropy", 0.0) or 0.0,
    )


def badness(L: Layout, p: Params | None = None) -> float:
    """Visible imperfection in 'cm of gap deviation' units: uniformity, then border sharpness.

    With constraint parameters, violations of the large-frame constraints are added.
    """
    m = L.metrics
    b = m.max_dev + 0.5 * m.mean_dev + 0.15 * m.notch_len + 2.0 * (4 - m.corners)
    if p is not None:
        x = m.extras
        if p.large_min > 0 or p.large_max < 99:
            b += 3.0 * (max(0, p.large_min - x.n_large) + max(0, x.n_large - p.large_max))
        if p.large_apart and x.n_large >= 2:
            b += 3.0 * x.large_touch_pairs + max(0.0, p.large_min_dist - x.large_min_dist) / 10.0
    return b


def rank_key(L: Layout, p: Params):
    size_dist = abs(L.W - p.target_w) + abs(L.H - p.target_h)
    b = badness(L, p)
    ent = -round(L.metrics.extras.entropy, 1) if p.w_entropy > 0 else 0.0
    return (round(b, 0), -len(L.rects), ent, round(b, 2), size_dist, L.cost)


def build_layout(c: Candidate, frames: list[Frame], p: Params) -> Layout:
    """Place a candidate state with the parameters it was searched with and measure it."""
    rects, W, H = place(c.tree, frames, c.g, p)
    return Layout(0, c.tree, c.g, W, H, rects, metrics(rects, W, H, c.g, p), c.cost)


def layout_signature(L: Layout, frames: list[Frame]) -> tuple:
    return signature(L.rects, frames, L.W, L.H)


def select_layouts(items: Iterable[tuple[Candidate, Params]], frames: list[Frame], n: int,
                   rank_p: Params) -> list[Layout]:
    """Place, de-duplicate (mirror images count as equal) and rank; return the best ``n`` numbered 1..n."""
    best: dict[tuple, Layout] = {}
    for c, p in items:
        L = build_layout(c, frames, p)
        sig = layout_signature(L, frames)
        if sig not in best or rank_key(L, rank_p) < rank_key(best[sig], rank_p):
            best[sig] = L
    layouts = sorted(best.values(), key=lambda L: rank_key(L, rank_p))[:n]
    for i, L in enumerate(layouts, 1):
        L.id = i
    return layouts
