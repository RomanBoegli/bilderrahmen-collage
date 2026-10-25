"""Command line interface: ``collage --help``."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .inventory import expand, load_inventory
from .rank import badness, params_from_args, parse_size, select_layouts
from .render import compile_pdf, write_json, write_tex
from .search import Candidate, SearchConfig, run_search


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
    ap.add_argument("--gap-mode", choices=["free", "per-run"], default="free",
                    help="free: the gap is an annealing variable; per-run: each run keeps one gap from the grid")
    ap.add_argument("--min-frames", type=int, default=10)
    ap.add_argument("--frame-bonus", type=float, default=1.5, help="cost bonus per frame used")
    ap.add_argument("--cost-shape", choices=["huber", "linear"], default="huber")
    ap.add_argument("--hard-margin", type=float, default=10.0,
                    help="states further than tol+margin from the target size are rejected")
    ap.add_argument("--runs", type=int, default=32, help="independent annealing runs")
    ap.add_argument("--iters", type=int, default=1_000_000, help="iterations per run (ignored with --minutes)")
    ap.add_argument("--minutes", type=float, default=None,
                    help="total wall-clock budget for the search; every run gets an equal share and the "
                         "annealing schedule follows elapsed time instead of iterations")
    ap.add_argument("--t-start", type=float, default=None, help="start temperature (default: calibrated per run)")
    ap.add_argument("--t-end", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--jobs", type=int, default=None, help="parallel processes (default: all cores)")
    ap.add_argument("--layouts", type=int, default=12, help="number of layouts to render")
    ap.add_argument("--out", default="out")
    ap.add_argument("--candidates", default=None,
                    help="skip the search and render from a candidates.json written by an earlier run "
                         "(several files may be given, comma separated)")
    ap.add_argument("--annotate-threshold", type=float, default=0.25)
    ap.add_argument("--no-compile", action="store_true", help="write .tex but do not run latexmk")
    args = ap.parse_args(argv)

    tw, th = parse_size(args.target)
    p = params_from_args(vars(args))
    cfg = SearchConfig(iters=args.iters, runs=args.runs, seed=args.seed, g_min=args.gap_min, g_max=args.gap_max,
                       g_step=args.gap_step, g_fixed=args.gap, g_mode=args.gap_mode, min_frames=args.min_frames,
                       t_start=args.t_start, t_end=args.t_end, jobs=args.jobs, minutes=args.minutes)
    if args.gap is not None:
        cfg.g_min = cfg.g_max = args.gap

    types = load_inventory(args.xlsx)
    frames = expand(types)
    print(f"{len(frames)} frames in {len(types)} formats; total area {sum(f.w * f.h for f in frames):.0f} cm^2",
          file=sys.stderr)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if args.candidates:
        # every file is placed/ranked with the parameters it was searched with (e.g. its slack rule)
        items: list = []
        for path in args.candidates.split(","):
            data = json.loads(Path(path).read_text())
            fp = params_from_args(data["args"]) if "args" in data else p
            items += [(Candidate(c["tree"], c["g"], c["cost"], c["run"]), fp) for c in data["candidates"]]
        print(f"loaded {len(items)} candidate states", file=sys.stderr)
    else:
        t0 = time.time()
        cands = run_search(frames, p, cfg, progress=lambda s: print(s, file=sys.stderr, flush=True))
        print(f"search finished in {time.time() - t0:.0f} s with {len(cands)} candidate states", file=sys.stderr)
        (out / "candidates.json").write_text(json.dumps({
            "args": vars(args),
            "candidates": [{"tree": c.tree, "g": c.g, "cost": c.cost, "run": c.run} for c in cands],
        }))
        items = [(c, p) for c in cands]

    layouts = select_layouts(items, frames, args.layouts, p)

    print(f"\n{'#':>2} {'size':>14} {'gap':>4} {'n':>3} {'maxdev':>6} {'meandev':>7} {'notch':>6} {'corners':>7} {'score':>6}")
    for L in layouts:
        m = L.metrics
        print(f"{L.id:>2} {L.W:6.1f} x {L.H:5.1f} {L.g:4.1f} {len(L.rects):>3} {m.max_dev:6.2f} {m.mean_dev:7.2f} "
              f"{m.notch_len:6.1f} {m.corners:>5}/4 {badness(L):6.2f}")

    params = {"target": f"{tw:g}x{th:g}", "tol": args.tol, "gap_min": cfg.g_min, "gap_max": cfg.g_max,
              "gap_mode": args.gap_mode, "delta": args.delta, "slack_rule": args.slack_rule,
              "cost_shape": args.cost_shape, "min_frames": args.min_frames, "frame_bonus": args.frame_bonus,
              "runs": args.runs, "iters": args.iters if args.minutes is None else f"{args.minutes:g} min",
              "seed": args.seed}
    write_json(out / "layouts.json", layouts, frames, types, params)
    tex = out / "layouts.tex"
    write_tex(tex, layouts, frames, types, params, args.annotate_threshold)
    print(f"wrote {out / 'layouts.json'} and {tex}", file=sys.stderr)
    pdf = None if args.no_compile else compile_pdf(tex)
    if pdf:
        print(f"wrote {pdf}", file=sys.stderr)
    else:
        print(f"PDF not compiled; run: cd {out} && latexmk -pdf layouts.tex", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
