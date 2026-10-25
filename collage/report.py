"""One PDF for several search configurations ("paradigms"): ``collage-report``.

Reads the ``candidates.json`` of several search runs (by default every ``out/<name>/``),
ranks each run's candidates with the parameters that run was searched with and writes
``out/report/report.tex`` (+ ``.pdf``): title page, table of contents, a comparison of the
paradigms with their best layouts, a combined ranking over all paradigms and one section
per paradigm with its ranked layouts, one page each.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .inventory import Frame, FrameType, expand, fmt, load_inventory
from .layout import Params
from .rank import badness, layout_signature, params_from_args, rank_key, select_layouts
from .render import (Layout, _colors, _inventory_table, _layout_page, _preamble, _summary_rows, compile_pdf,
                     layout_dict, notch_text, tex_text, thumbnail, toc_entry)
from .search import Candidate

# order of the configurations in scripts/run_experiments.sh; unknown names follow alphabetically
KNOWN_ORDER = ["free-gap", "per-run-gap", "proportional", "gap-3", "linear-cost", "free-gap-s1",
               "large-apart", "entropy", "large-entropy"]
EXCLUDE_DIRS = {"combined", "report"}

DESCRIPTIONS = {
    "free-gap": (
        "Baseline. The nominal gap is an annealing variable between 1.5 and 6 cm (steps of 0.5 cm). "
        "Where a strip of frames is shorter than its neighbour, the slack is spread evenly over the gaps "
        "inside that strip. Gap deviations and border notches are penalised with a Huber shape: quadratic "
        "up to 2 cm, linear beyond, so a few large deviations are not punished out of proportion."
    ),
    "per-run-gap": (
        "Every annealing run keeps one fixed nominal gap from the grid 1.5, 2, \\dots, 6 cm (the runs are "
        "spread over the grid) instead of changing the gap during the search. Each run explores its gap "
        "value more thoroughly, which gives a more diverse set of results across gap values."
    ),
    "proportional": (
        "Unavoidable slack is distributed in proportion to the size of the neighbouring frames: the weight "
        "of a gap is the mean extent of the two frames (or groups) it separates, so gaps next to big frames "
        "widen more than gaps between small ones. The deviations are therefore systematic (bigger frame, "
        "bigger gap) rather than equal."
    ),
    "gap-3": (
        "The nominal gap is fixed at 3 cm; the search only changes the arrangement and the subset of frames. "
        "Actual gaps still deviate from 3 cm wherever slack has to be absorbed."
    ),
    "linear-cost": (
        "Gap deviations and notches are penalised linearly instead of with the Huber shape. The cost "
        "landscape is smoother and small deviations count relatively more, which favours layouts whose "
        "deviations are spread thinly instead of concentrated in one gap."
    ),
    "free-gap-s1": (
        "Identical to the baseline but with another random seed. Comparing it with the baseline shows how "
        "reproducible the results of a 20 minute search are."
    ),
    "large-apart": (
        "Sub-analysis. Like the baseline, but the layout must contain 2 to 3 large frames (outer area of at "
        "least 2000 cm$^2$, i.e.\\ the 52.5 $\\times$ 42.5 and 42.5 $\\times$ 52.5 frames) and they are kept apart: "
        "two large frames that are direct neighbours are penalised in proportion to the edge they share "
        "(barely touching costs little), and the closest pair of large frames is pulled towards a centre "
        "distance of at least 70 cm."
    ),
    "entropy": (
        "Sub-analysis. Like the baseline, but the cost also rewards the irregularity (``entropy'') of the "
        "constellation: a mix of many formats, different formats next to each other, frame edges that do "
        "not line up, and few straight gap lines running across the whole collage."
    ),
    "large-entropy": (
        "Sub-analysis. Both of the above: 2 to 3 large frames kept apart from each other, and the "
        "irregularity reward."
    ),
}

# parameters that may differ between paradigms, in the order they are listed
DIFF_KEYS = ["target", "tol", "gap", "gap_min", "gap_max", "gap_step", "gap_mode", "slack_rule", "cost_shape",
             "delta", "min_frames", "frame_bonus", "hard_margin", "large_area", "large_count", "large_apart",
             "large_min_dist", "entropy", "t_start", "t_end", "seed", "runs", "minutes", "iters"]


@dataclass
class Paradigm:
    name: str
    path: Path
    args: dict
    params: Params
    layouts: list[Layout]
    n_candidates: int

    @property
    def budget(self) -> str:
        a = self.args
        runs = a.get("runs", "?")
        if a.get("minutes") is not None:
            return f"{runs} runs, {a['minutes']:g} min wall-clock budget"
        return f"{runs} runs, {a.get('iters', '?')} iterations each"


def discover(out_dir: Path) -> list[Path]:
    dirs = [d for d in out_dir.iterdir()
            if d.is_dir() and (d / "candidates.json").exists() and d.name not in EXCLUDE_DIRS]

    def key(d: Path):
        return (KNOWN_ORDER.index(d.name) if d.name in KNOWN_ORDER else len(KNOWN_ORDER), d.name)

    return sorted(dirs, key=key)


def load_paradigm(path: Path, frames: list[Frame], n: int) -> Paradigm:
    data = json.loads((path / "candidates.json").read_text())
    args = data.get("args", {})
    p = params_from_args(args, n_types=len({(f.w, f.h) for f in frames}))
    cands = [Candidate(c["tree"], c["g"], c["cost"], c["run"]) for c in data["candidates"]]
    layouts = select_layouts([(c, p) for c in cands], frames, n, p)
    return Paradigm(path.name, path, args, p, layouts, len(cands))


def _val(v) -> str:
    if v is None:
        return "none"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def diff_args(args: dict, base: dict) -> list[str]:
    """``key = value`` for every search parameter that differs from the baseline's."""
    out = []
    for k in DIFF_KEYS:
        if k in args and args.get(k) != base.get(k):
            if k == "large_area" and not (args.get("large_count") or args.get("large_apart")):
                continue  # only meaningful with a large-frame constraint
            out.append(f"{k} = {_val(args[k])}")
    return out


def describe(P: Paradigm) -> str:
    if P.name in DESCRIPTIONS:
        return DESCRIPTIONS[P.name]
    return "No description on file for this configuration; see the parameters that differ from the baseline."


def combined_ranking(paradigms: list[Paradigm], frames: list[Frame], top: int, rank_p: Params):
    """Rank the layouts shown in the document across all paradigms; equal layouts are merged."""
    seen: dict[tuple, tuple[Layout, list[tuple[str, int]]]] = {}
    for P in paradigms:
        for L in P.layouts:
            sig = layout_signature(L, frames)
            if sig in seen:
                seen[sig][1].append((P.name, L.id))
            else:
                seen[sig] = (L, [(P.name, L.id)])
    rows = sorted(seen.values(), key=lambda t: rank_key(t[0], rank_p))
    return rows[:top], len(rows)


# ----------------------------------------------------------------------------- LaTeX pieces

def _link(name: str, lid: int, text: str | None = None) -> str:
    text = text if text is not None else f"{tex_text(name)} / {lid}"
    return rf"\hyperref[lay:{name}:{lid}]{{{text}}}"


def _title_page(paradigms: list[Paradigm], frames: list[Frame], types: list[FrameType], title: str,
                n_combined: int) -> str:
    base = paradigms[0]
    a = base.args
    colors = _colors(types)
    n_layouts = sum(len(P.layouts) for P in paradigms)
    names = ", ".join(tex_text(P.name) for P in paradigms)
    sub_txt = (", section~3 covers the sub-analysis with large-frame constraints and the entropy reward"
               if any(P.params.needs_placement for P in paradigms) else "")
    return rf"""\thispagestyle{{empty}}
\vspace*{{18mm}}
\begin{{center}}
{{\Huge\bfseries {tex_text(title)}}}\\[10pt]
{{\LARGE {len(paradigms)} search paradigms, {n_layouts} layouts}}\\[8pt]
{{\large {date.today():%d %B %Y}}}
\end{{center}}
\vspace{{14mm}}
\begin{{minipage}}[t]{{0.58\textwidth}}\vspace{{0pt}}
{{\bfseries What this document is}}\\[4pt]
The {len(frames)} picture frames listed on the right (measured outer dimensions, orientation as listed) are
to be hung as one collage of about {tex_text(a.get('target', '180x120')).replace('x', r' $\times$ ')} cm
($\pm$ {_val(a.get('tol', 10))} cm) with gaps as uniform as possible and, ideally, a sharp rectangular
outer border. Not all frames have to be used.

\vspace{{6pt}}
The same search was run with {len(paradigms)} different settings (``paradigms'': {names}).
Section~1 explains what distinguishes them and compares their best layouts, section~2 ranks all
{n_combined} distinct layouts of this document together{sub_txt}, and the remaining sections show every paradigm's
{len(base.layouts)} best layouts, one per page at scale 1:10, with frame labels, red annotations for gaps that
deviate from the nominal gap, and a coordinate table for hanging.

\vspace{{6pt}}
{{\bfseries Reading the numbers}}\\[4pt]
\emph{{Nominal gap}}: the spacing the layout is built on. \emph{{Max.\ / mean deviation}}: largest and
average difference between an actual gap and the nominal gap, in cm.
\emph{{Notches}}: total length of the outer border that is not covered by a frame or a regular gap.
\emph{{Corners}}: how many of the four corners of the bounding rectangle are filled by a frame.
\emph{{Score}} $=$ max.\ deviation $+ 0.5\cdot$ mean deviation $+ 0.15\cdot$ notch length $+ 2$ per
missing corner (lower is better). Layouts are ranked by score rounded to whole cm, then by number of frames
(more is better), then by exact score, then by distance from the target size.
\emph{{Entropy}}: irregularity of the constellation in $[0, 1]$ (mix of formats, different neighbours, staggered
edges, few through-lines; see the sub-analysis section). \emph{{Large frames}}: the 52.5 $\times$ 42.5 and
42.5 $\times$ 52.5 frames; a layout page says how many are used, whether two of them touch and how far apart the
closest two are.
\end{{minipage}}\hfill
\begin{{minipage}}[t]{{0.36\textwidth}}\vspace{{0pt}}
{{\bfseries Inventory}}\\[4pt]
{_inventory_table(types, {}, colors, show_used=False)}
\end{{minipage}}
\clearpage
\tableofcontents
\clearpage
"""


def _paradigm_overview(paradigms: list[Paradigm], frames: list[Frame], types: list[FrameType]) -> str:
    base = paradigms[0]
    rows = []
    for P in paradigms:
        diffs = diff_args(P.args, base.args)
        dtxt = "baseline" if P is base else (", ".join(tex_text(d) for d in diffs) or "same parameters")
        rows.append(rf"\hyperref[sec:{P.name}]{{\bfseries {tex_text(P.name)}}} & {describe(P)} & {{\small {dtxt}}} \\"
                    + "\n" + r"\addlinespace[4pt]")
    best_rows = []
    for P in paradigms:
        if not P.layouts:
            best_rows.append(rf"\hyperref[sec:{P.name}]{{{tex_text(P.name)}}} & \multicolumn{{10}}{{l}}{{no layouts}} \\")
            continue
        L = P.layouts[0]
        m = L.metrics
        best_rows.append(
            rf"\hyperref[sec:{P.name}]{{{tex_text(P.name)}}} & {_link(P.name, L.id, str(L.id))} & "
            f"{L.W:.1f} $\\times$ {L.H:.1f} & {len(L.rects)} & {fmt(L.g)} & {m.mean_dev:.2f} & {m.max_dev:.1f} & "
            f"{notch_text(m)} & {m.corners}/4 & {m.extras.entropy:.2f} & {badness(L, P.params):.2f} \\\\"
        )
    gallery = _gallery(paradigms, frames, types)
    return rf"""\section{{Search paradigms}}\label{{sec:paradigms}}
All paradigms share the same model and search. A layout is a \emph{{slicing tree}} over a subset of the
frames: frames are recursively placed side by side or on top of each other with a nominal gap. Where two
siblings differ in height (or width) the smaller one has slack, which is absorbed by widening the gaps inside
it; if it has no inner gaps the slack becomes dead space, which counts as a notch when it touches the outer
border. Simulated annealing over the tree, the subset of frames used and (unless fixed) the nominal gap
minimises a cost made of gap deviations, border notches, the distance from the target size and a small bonus
per frame used. Every paradigm ran {base.budget.replace('runs', 'independent annealing runs')} on all cores;
its candidate states were then placed, measured exactly, de-duplicated (mirror images count as equal) and
ranked by the score described on the title page.

\vspace{{8pt}}
\begin{{tabular}}{{@{{}}l p{{0.50\textwidth}} p{{0.30\textwidth}}@{{}}}}
\toprule
paradigm & what differs & parameters that differ from the baseline \\
\midrule
{chr(10).join(rows)}
\bottomrule
\end{{tabular}}

\vspace{{12pt}}
{{\bfseries Best-ranked layout of each paradigm}} (all measurements in cm)\\[4pt]
\begin{{tabular}}{{@{{}}l r l r r r r l c r r@{{}}}}
\toprule
paradigm & layout & size & frames & nominal gap & mean dev & max dev & notches & corners & entropy & score \\
\midrule
{chr(10).join(best_rows)}
\bottomrule
\end{{tabular}}
\clearpage
{{\large\bfseries Best-ranked layout of each paradigm at a glance}} {{\small (scale 1:25; details on the linked pages)}}\\[10pt]
{gallery}
\clearpage
"""


def _gallery(paradigms: list[Paradigm], frames: list[Frame], types: list[FrameType]) -> str:
    """Thumbnails of the best layout of each paradigm, three per row."""
    thumbs = []
    for P in paradigms:
        if not P.layouts:
            continue
        L = P.layouts[0]
        m = L.metrics
        thumbs.append(rf"""\begin{{minipage}}[t]{{0.32\textwidth}}\centering\vspace{{0pt}}
{thumbnail(L, frames, types)}\\[2pt]
{{\small\bfseries {tex_text(P.name)}}} -- {_link(P.name, L.id, f"layout {L.id}")}\\
{{\scriptsize {L.W:.1f} $\times$ {L.H:.1f} cm, {len(L.rects)} frames, gap {fmt(L.g)} cm,
max.\ dev.\ {m.max_dev:.1f} cm, notches {notch_text(m)}, entropy {m.extras.entropy:.2f}}}
\end{{minipage}}""")
    rows = []
    for i in range(0, len(thumbs), 3):
        rows.append("\\hfill\n".join(thumbs[i:i + 3]) + "\n\\par\\vspace{10pt}")
    return "\n".join(rows)


def _large_text(L: Layout) -> str:
    x = L.metrics.extras
    if x.n_large < 2:
        return str(x.n_large)
    touch = "apart" if x.large_touch_pairs == 0 else f"{x.large_touch_pairs} touching ({x.large_touch:.0f} cm)"
    return f"{x.n_large}, {touch}, {x.large_min_dist:.0f} cm"


def _subanalysis_section(cps: list[Paradigm], frames: list[Frame], types: list[FrameType]) -> str:
    """Chapter for the paradigms with large-frame constraints and/or the entropy reward."""
    rows = []
    for P in cps:
        if not P.layouts:
            continue
        L = P.layouts[0]
        m, x = L.metrics, L.metrics.extras
        rows.append(
            rf"\hyperref[sec:{P.name}]{{{tex_text(P.name)}}} & {_link(P.name, L.id, str(L.id))} & "
            f"{L.W:.1f} $\\times$ {L.H:.1f} & {len(L.rects)} & {fmt(L.g)} & {m.max_dev:.1f} & {notch_text(m)} & "
            f"{_large_text(L)} & {x.entropy:.2f} & {x.type_mix:.2f} / {x.variety:.2f} / {x.stagger:.2f} / {x.lines:.2f} & "
            f"{badness(L, P.params):.2f} \\\\"
        )
    any_large = any(P.params.large_min > 0 or P.params.large_apart for P in cps)
    any_ent = any(P.params.w_entropy > 0 for P in cps)
    d = cps[0].params
    large_txt = rf"""{{\bfseries Large frames apart.}} Frames with an outer area of at least {d.large_area:g} cm$^2$ count
as large (here the 52.5 $\times$ 42.5 and 42.5 $\times$ 52.5 frames). The layout must contain between
{d.large_min} and {d.large_max} of them (each one missing or too many costs as much as a 1.5 cm notch across the whole
width). Two large frames that face each other across a regular gap are ``touching''; the penalty grows with the
edge they share, so barely touching is almost free. In addition the closest pair of large frames is pulled towards a
centre distance of at least {d.large_min_dist:g} cm. The tables report the number of large frames, how many pairs touch
(with the total shared edge) and the smallest centre distance.""" if any_large else ""
    ent_txt = r"""{\bfseries Entropy.} A number in $[0, 1]$ for how irregular (``random'') a constellation looks, the
mean of four shares: \emph{mix} -- the Shannon entropy of the formats used, relative to using all formats equally;
\emph{variety} -- the share of neighbouring pairs (across a regular gap) that have different formats;
\emph{stagger} -- the share of distinct frame edge positions (in a grid most edges line up, so it scores low);
\emph{lines} -- 1 for a single straight gap line running across the whole collage, 0 for five or more.
The search adds $w \cdot (1 - \mathrm{entropy})$ to the cost, so it trades a little gap uniformity for irregularity;
layouts with equal rounded score are ranked by entropy.""" if any_ent else ""
    return rf"""\section{{Sub-analysis: large frames apart, entropy}}\label{{sec:sub}}
The following paradigms add constraints on top of the baseline search:
{", ".join(rf"\hyperref[sec:{P.name}]{{{tex_text(P.name)}}}" for P in cps)}. The gap and border measures stay the
same; the extra measures are shown on every layout page and in the tables below.

\vspace{{4pt}}
{large_txt}

\vspace{{4pt}}
{ent_txt}

\vspace{{10pt}}
{{\bfseries Best-ranked layout of each sub-analysis paradigm}} (cm; large = count, touching pairs, min.\ centre distance;
entropy components = mix / variety / stagger / lines)\\[4pt]
\begin{{tabular}}{{@{{}}l r l r r r l l r l r@{{}}}}
\toprule
paradigm & layout & size & frames & gap & max dev & notches & large frames & entropy & components & score \\
\midrule
{chr(10).join(rows)}
\bottomrule
\end{{tabular}}
\clearpage
{{\large\bfseries Best-ranked layout of each sub-analysis paradigm at a glance}} {{\small (scale 1:25)}}\\[10pt]
{_gallery(cps, frames, types)}
\clearpage
"""


def _combined_section(rows, n_total: int, paradigms: list[Paradigm]) -> str:
    lines = []
    for rank, (L, found) in enumerate(rows, 1):
        m = L.metrics
        first = found[0]
        also = ", ".join(_link(n, i) for n, i in found[1:]) or "--"
        lines.append(
            f"{rank} & {_link(*first)} & {{\\small {also}}} & {L.W:.1f} $\\times$ {L.H:.1f} & {fmt(L.g)} & "
            f"{len(L.rects)} & {m.max_dev:.1f} & {m.mean_dev:.2f} & {notch_text(m)} & {m.corners}/4 & "
            f"{m.extras.entropy:.2f} & {badness(L):.2f} \\\\"
        )
    n_shown = sum(len(P.layouts) for P in paradigms)
    return rf"""\section{{Combined ranking}}\label{{sec:combined}}
The {n_shown} layouts shown in this document ({n_total} distinct ones; a layout found by several paradigms is
listed once, with the other paradigms in the third column) ranked together by the same score (gap uniformity and
border only; the sub-analysis constraints are not part of this score). Click a layout to jump to its page.\\[8pt]
\begin{{tabular}}{{@{{}}r l l l r r r r l c r r@{{}}}}
\toprule
\# & paradigm / layout & also found by & size (cm) & gap & frames & max dev & mean dev & notches & corners & entropy & score \\
\midrule
{chr(10).join(lines)}
\bottomrule
\end{{tabular}}
\clearpage
"""


def _paradigm_section(P: Paradigm, base: Paradigm, frames: list[Frame], types: list[FrameType],
                      annotate_threshold: float) -> str:
    diffs = diff_args(P.args, base.args)
    if P is base:
        dtxt = "This is the baseline the other paradigms are compared with."
    elif diffs:
        dtxt = "Parameters that differ from the baseline: " + ", ".join(tex_text(d) for d in diffs) + "."
    else:
        dtxt = "Same parameters as the baseline."
    head = rf"""\section{{{tex_text(P.name)}}}\label{{sec:{P.name}}}
{describe(P)}\\[4pt]
{{\small {dtxt}}}\\
{{\small {P.budget}, {P.n_candidates} candidate states, the {len(P.layouts)} best distinct layouts are shown.}}\\[10pt]
\begin{{tabular}}{{@{{}}r l r r r r l c r@{{}}}}
\toprule
\# & size (cm) & gap & frames & max dev & mean dev & notches & corners & entropy \\
\midrule
{_summary_rows(P.layouts, frames, types, link_prefix=P.name)}
\bottomrule
\end{{tabular}}
\clearpage
"""
    pages = [_layout_page(L, frames, types, annotate_threshold, toc=toc_entry(L), label=f"lay:{P.name}:{L.id}")
             for L in P.layouts]
    return head + "\n".join(pages)


def write_report_tex(path: Path, paradigms: list[Paradigm], frames: list[Frame], types: list[FrameType],
                     top: int, annotate_threshold: float, title: str) -> None:
    base = paradigms[0]
    rows, n_total = combined_ranking(paradigms, frames, top, base.params)
    parts = [_preamble(types, report=True), _title_page(paradigms, frames, types, title, n_total),
             _paradigm_overview(paradigms, frames, types), _combined_section(rows, n_total, paradigms)]
    cps = [P for P in paradigms if P.params.needs_placement]
    if cps:
        parts.append(_subanalysis_section(cps, frames, types))
    parts += [_paradigm_section(P, base, frames, types, annotate_threshold) for P in paradigms]
    parts.append("\\end{document}\n")
    path.write_text("\n".join(parts), encoding="utf-8")


def write_report_json(path: Path, paradigms: list[Paradigm], frames: list[Frame], types: list[FrameType],
                      top: int) -> None:
    base = paradigms[0]
    rows, _ = combined_ranking(paradigms, frames, top, base.params)
    data = {
        "paradigms": [
            {"name": P.name, "args": P.args, "candidates": P.n_candidates,
             "layouts": [layout_dict(L, frames, types) for L in P.layouts]}
            for P in paradigms
        ],
        "combined": [
            {"rank": i, "found_by": [{"paradigm": n, "layout": lid} for n, lid in found],
             "width": L.W, "height": L.H, "gap": L.g, "frames": len(L.rects),
             "max_gap_deviation": L.metrics.max_dev, "mean_gap_deviation": L.metrics.mean_dev,
             "notch_length": L.metrics.notch_len, "corners_filled": L.metrics.corners, "score": badness(L)}
            for i, (L, found) in enumerate(rows, 1)
        ],
    }
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="collage-report",
                                 description="One PDF with a table of contents for several search runs.")
    ap.add_argument("dirs", nargs="*",
                    help="result directories containing candidates.json (default: every out/<name>/ "
                         "except out/combined and out/report)")
    ap.add_argument("--xlsx", default="Bilderwand.xlsx", help="inventory sheet the runs were made with")
    ap.add_argument("--layouts", type=int, default=16, help="layouts per paradigm")
    ap.add_argument("--top", type=int, default=20, help="rows in the combined ranking")
    ap.add_argument("--out", default="out/report")
    ap.add_argument("--title", default="Bilderwand -- layout candidates")
    ap.add_argument("--annotate-threshold", type=float, default=0.25)
    ap.add_argument("--no-compile", action="store_true", help="write .tex but do not run latexmk")
    args = ap.parse_args(argv)

    dirs = [Path(d) for d in args.dirs] or discover(Path("out"))
    if not dirs:
        print("no result directories with candidates.json found", file=sys.stderr)
        return 1
    types = load_inventory(args.xlsx)
    frames = expand(types)
    paradigms = [load_paradigm(d, frames, args.layouts) for d in dirs]
    for P in paradigms:
        print(f"{P.name}: {P.n_candidates} candidate states -> {len(P.layouts)} layouts", file=sys.stderr)

    print(f"\n{'paradigm':<14} {'size':>14} {'gap':>4} {'n':>3} {'maxdev':>6} {'meandev':>7} {'notch':>6} {'corners':>7} {'score':>6}")
    for P in paradigms:
        if not P.layouts:
            continue
        L, m = P.layouts[0], P.layouts[0].metrics
        print(f"{P.name:<14} {L.W:6.1f} x {L.H:5.1f} {L.g:4.1f} {len(L.rects):>3} {m.max_dev:6.2f} {m.mean_dev:7.2f} "
              f"{m.notch_len:6.1f} {m.corners:>5}/4 {badness(L, P.params):6.2f}  entropy {m.extras.entropy:.2f}"
              + (f"  large {_large_text(L)}" if P.params.needs_placement else ""))

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tex = out / "report.tex"
    write_report_tex(tex, paradigms, frames, types, args.top, args.annotate_threshold, args.title)
    write_report_json(out / "report.json", paradigms, frames, types, args.top)
    print(f"wrote {out / 'report.json'} and {tex}", file=sys.stderr)
    pdf = None if args.no_compile else compile_pdf(tex)
    if pdf:
        print(f"wrote {pdf}", file=sys.stderr)
    else:
        print(f"PDF not compiled; run: cd {out} && latexmk -pdf report.tex", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
