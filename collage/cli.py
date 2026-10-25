"""Command line interface: ``collage --help``."""
from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

from .inventory import expand, load_inventory
from .layout import Params, metrics, place, signature
from .render import Layout, compile_pdf, write_json, write_tex
from .search import SearchConfig, run_search


def parse_size(s: str) -> tuple[float, float]:
    w, h = s.lower().replace("×", "x").split("x")
    return float(w), float(h)


def rank_key(L: Layout, p: Params):
    m = L.metrics
    notch_bucket = math.ceil(m.notch_len / 5.0)
    dev_bucket = math.ceil(max(0.0, m.max_dev - 0.05) / 0.5)
    size_dist = abs(L.W - p.target_w) + abs(L.H - p.target_h)
    return (notch_bucket, dev_bucket, -len(L.rects), round(m.mean_dev, 1), size_dist, L.cost)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="collage", description="Find collage layouts with uniform gaps and a sharp border.")
    ap.add_argument("--xlsx", default="Bilderwand.xlsx", help="inventory sheet (Format 'W x H', count)")
    ap.add_argument("--target", default="180x120", help="target outer size WxH in cm")
    ap.add_argument("--tol", type=float, default=10.0, help="allowed deviation from the target size (cm)")
    ap.add_argument("--gap", type=float, default=None, help="fixed nominal gap (cm); default: free")
    ap.add_argument("--gap-min", type=float, default=1.5)
    ap.add_argument("--gap-max", type=float, default=6.0)
    ap.add_argument("--gap-step", type=float, default=0.5)
    ap.add_argument("--delta", type=float, default=0.5, help="harmless gap deviation (cm)")
    ap.add_argument("--slack-rule", choices=["even", "proportional"], default="even",
                    help="how unavoidable slack is spread over gaps: evenly, or proportional to neighbour size")
    ap.add_argument("--min-frames", type=int, default=10)
    ap.add_argument("--frame-bonus", type=float, default=1.5, help="cost bonus per frame used")
    ap.add_argument("--runs", type=int, default=32, help="independent annealing runs")
    ap.add_argument("--iters", type=int, default=1_000_000, help="iterations per run")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--jobs", type=int, default=None, help="parallel processes (default: all cores)")
    ap.add_argument("--layouts", type=int, default=12, help="number of layouts to render")
    ap.add_argument("--out", default="out")
    ap.add_argument("--annotate-threshold", type=float, default=0.25)
    ap.add_argument("--no-compile", action="store_true", help="write .tex but do not run latexmk")
    args = ap.parse_args(argv)

    tw, th = parse_size(args.target)
    p = Params(target_w=tw, target_h=th, tol=args.tol, delta=args.delta, frame_bonus=args.frame_bonus,
               slack_rule=args.slack_rule)
    cfg = SearchConfig(iters=args.iters, runs=args.runs, seed=args.seed, g_min=args.gap_min, g_max=args.gap_max,
                       g_step=args.gap_step, g_fixed=args.gap, min_frames=args.min_frames, jobs=args.jobs)
    if args.gap is not None:
        cfg.g_min = cfg.g_max = args.gap

    types = load_inventory(args.xlsx)
    frames = expand(types)
    print(f"{len(frames)} frames in {len(types)} formats; total area {sum(f.w * f.h for f in frames):.0f} cm^2",
          file=sys.stderr)

    t0 = time.time()
    cands = run_search(frames, p, cfg, progress=lambda s: print(s, file=sys.stderr))
    print(f"search finished in {time.time() - t0:.0f} s with {len(cands)} candidate states", file=sys.stderr)

    best: dict[tuple, Layout] = {}
    for c in cands:
        rects, W, H = place(c.tree, frames, c.g, p)
        sig = signature(rects, frames, W, H)
        L = Layout(0, c.tree, c.g, W, H, rects, metrics(rects, W, H, c.g), c.cost)
        if sig not in best or rank_key(L, p) < rank_key(best[sig], p):
            best[sig] = L
    layouts = sorted(best.values(), key=lambda L: rank_key(L, p))[: args.layouts]
    for i, L in enumerate(layouts, 1):
        L.id = i

    print(f"\n{'#':>2} {'size':>14} {'gap':>4} {'n':>3} {'maxdev':>6} {'meandev':>7} {'notch':>6} {'corners':>7}")
    for L in layouts:
        m = L.metrics
        print(f"{L.id:>2} {L.W:6.1f} x {L.H:5.1f} {L.g:4.1f} {len(L.rects):>3} {m.max_dev:6.2f} {m.mean_dev:7.2f} "
              f"{m.notch_len:6.1f} {m.corners:>5}/4")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    params = {"target": f"{tw:g}x{th:g}", "tol": args.tol, "gap_min": cfg.g_min, "gap_max": cfg.g_max,
              "delta": args.delta, "slack_rule": args.slack_rule, "min_frames": args.min_frames,
              "frame_bonus": args.frame_bonus, "runs": args.runs, "iters": args.iters, "seed": args.seed}
    write_json(out / "layouts.json", layouts, frames, types, params)
    tex = out / "layouts.tex"
    write_tex(tex, layouts, frames, types, params, args.annotate_threshold)
    print(f"wrote {out / 'layouts.json'} and {tex}", file=sys.stderr)
    if not args.no_compile:
        pdf = compile_pdf(tex)
        if pdf:
            print(f"wrote {pdf}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
