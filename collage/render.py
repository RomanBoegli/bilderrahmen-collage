"""Render layouts as a LaTeX/TikZ document (one layout per page) and as JSON."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .inventory import Frame, FrameType, fmt
from .layout import Metrics, Rect, Tree

PALETTE = ["CFE4F2", "FBE2B4", "D4E9CC", "E5D3EA", "F5CACA", "F9F1B5", "CDE9E6", "E9D9C7", "DADADA"]
TIMES = r"\texorpdfstring{$\times$}{x}"  # for moving arguments (table of contents, bookmarks)


@dataclass
class Layout:
    id: int
    tree: Tree
    g: float
    W: float
    H: float
    rects: list[Rect]
    metrics: Metrics
    cost: float


def _esc(s: str) -> str:
    return s.replace("x", r"$\times$")


def tex_text(s: str) -> str:
    """Escape a plain string (names, parameter values) for LaTeX text mode."""
    for a, b in (("\\", r"\textbackslash{}"), ("_", r"\_"), ("&", r"\&"), ("%", r"\%"), ("#", r"\#"),
                 ("$", r"\$"), ("{", r"\{"), ("}", r"\}")):
        s = s.replace(a, b)
    return s


def _colors(types: list[FrameType]) -> dict[str, str]:
    keys = sorted({t.size_key for t in types}, key=lambda k: tuple(float(v) for v in k.split("x")))
    return {k: f"ft{i}" for i, k in enumerate(keys)}


def _preamble(types: list[FrameType], report: bool = False) -> str:
    colors = _colors(types)
    defs = "\n".join(
        f"\\definecolor{{{name}}}{{HTML}}{{{PALETTE[i % len(PALETTE)]}}}" for i, name in enumerate(colors.values())
    )
    if report:
        geometry = "margin=10mm, top=18mm, bottom=12mm, headheight=14pt, headsep=4mm, footskip=6mm"
        extra = r"""\usepackage{fancyhdr}
\usepackage[hidelinks, bookmarksnumbered, bookmarksopen, bookmarksopenlevel=1]{hyperref}
\setcounter{tocdepth}{2}
\setcounter{secnumdepth}{1}
\renewcommand{\sectionmark}[1]{\markboth{#1}{}}
\pagestyle{fancy}
\fancyhf{}
\renewcommand{\headrulewidth}{0.3pt}
\fancyhead[L]{\small Bilderwand -- layout candidates}
\fancyhead[R]{\small\nouppercase{\leftmark}}
\fancyfoot[C]{\small\thepage}"""
    else:
        geometry = "margin=10mm"
        extra = r"\pagestyle{empty}"
    return rf"""\documentclass[a4paper,landscape,10pt]{{article}}
\usepackage[{geometry}]{{geometry}}
\usepackage[T1]{{fontenc}}
\usepackage{{lmodern}}
\usepackage{{helvet}}
\renewcommand{{\familydefault}}{{\sfdefault}}
\usepackage{{tikz}}
\usetikzlibrary{{arrows.meta}}
\usepackage{{xcolor}}
\usepackage{{booktabs}}
\setlength{{\parindent}}{{0pt}}
{defs}
{extra}
\begin{{document}}
"""


def _inventory_table(types: list[FrameType], used: dict[int, int], colors: dict[str, str],
                     show_used: bool = True) -> str:
    rows = []
    for t in types:
        swatch = rf"\tikz[baseline=-0.6ex]\fill[{colors[t.size_key]},draw=black!60] (0,0) rectangle (1.2ex,1.2ex);"
        u = f"{used.get(t.idx, 0)} & " if show_used else ""
        rows.append(f"{swatch} & {_esc(t.outer)} & {t.orientation} & {u}{t.count} \\\\")
    body = "\n".join(rows)
    head = " & used & have" if show_used else " & have"
    cols = "c l l r r" if show_used else "c l l r"
    return rf"""\begin{{tabular}}{{@{{}}{cols}@{{}}}}
\toprule
 & outer w $\times$ h (cm) & {head} \\
\midrule
{body}
\bottomrule
\end{{tabular}}"""


def notch_text(m: Metrics) -> str:
    return "none" if m.notch_len < 1e-6 else f"{m.notch_len:.0f} cm"


def _summary_rows(layouts: list[Layout], frames: list[Frame], types: list[FrameType],
                  link_prefix: str | None = None) -> str:
    rows = []
    for L in layouts:
        m = L.metrics
        idc = rf"\hyperref[lay:{link_prefix}:{L.id}]{{{L.id}}}" if link_prefix else str(L.id)
        rows.append(
            f"{idc} & {L.W:.1f} $\\times$ {L.H:.1f} & {fmt(L.g)} & {len(L.rects)} & "
            f"{m.max_dev:.1f} & {m.mean_dev:.2f} & {notch_text(m)} & {m.corners}/4 & {m.extras.entropy:.2f} \\\\"
        )
    return "\n".join(rows)


def _overview_page(layouts: list[Layout], frames: list[Frame], types: list[FrameType], params: dict) -> str:
    colors = _colors(types)
    inv = _inventory_table(types, {}, colors, show_used=False)
    plist = ", ".join(f"{k} = {v}" for k, v in params.items()).replace("_", r"\_")
    return rf"""{{\LARGE\bfseries Bilderwand -- layout candidates}}\\[4pt]
{{\small Target {params['target']} cm $\pm$ {params['tol']} cm, nominal gap free in
[{params['gap_min']}, {params['gap_max']}] cm, orientation fixed, sizes are measured outer dimensions.
Max.\ gap deviation = largest difference between an actual gap and the layout's nominal gap;
notches = parts of the outer border not covered by a frame or a regular gap. Ranked by a combined
score (gap uniformity first, border sharpness second, then number of frames).}}\\[10pt]
\begin{{minipage}}[t]{{0.6\textwidth}}\vspace{{0pt}}
\begin{{tabular}}{{@{{}}r l r r r r l c r@{{}}}}
\toprule
\# & size (cm) & gap & frames & max dev & mean dev & notches & corners & entropy \\
\midrule
{_summary_rows(layouts, frames, types)}
\bottomrule
\end{{tabular}}
\end{{minipage}}\hfill
\begin{{minipage}}[t]{{0.36\textwidth}}\vspace{{0pt}}
{{\bfseries Inventory}}\\[4pt]
{inv}
\\[8pt]
{{\scriptsize Search parameters: {plist}}}
\end{{minipage}}
\clearpage
"""


def _tikz_body(L: Layout, frames: list[Frame], types: list[FrameType], annotate_threshold: float,
               detail: bool = True) -> str:
    """TikZ commands drawing the layout; ``detail=False`` gives a bare thumbnail (no labels)."""
    colors = _colors(types)
    m = L.metrics
    lw = "0.5pt" if detail else "0.3pt"
    tik = [rf"\draw[gray!70, dashed, line width=0.4pt] (0,0) rectangle ({L.W:.2f},{L.H:.2f});"]
    for i, r in enumerate(L.rects, 1):
        t = types[frames[r.frame].type]
        col = colors[t.size_key]
        tik.append(
            rf"\filldraw[fill={col}, draw=black!80, line width={lw}] ({r.x:.2f},{r.y:.2f}) rectangle ({r.x2:.2f},{r.y2:.2f});"
        )
        if not detail:
            continue
        cx, cy = (r.x + r.x2) / 2, (r.y + r.y2) / 2
        tik.append(
            rf"\node[align=center, inner sep=0pt] at ({cx:.2f},{cy:.2f}) {{\scriptsize {_esc(t.outer)}}};"
        )
        tik.append(rf"\node[anchor=north west, inner sep=1.2pt, font=\tiny, text=black!60] at ({r.x:.2f},{r.y:.2f}) {{{i}}};")
    if not detail:
        return "\n".join(tik)
    for gp in m.gaps:
        if abs(gp.size - L.g) > annotate_threshold:
            rot = ", rotate=90" if gp.horizontal else ""
            tik.append(
                rf"\node[font=\tiny, text=red, fill=white, inner sep=0.3pt{rot}] at ({gp.mx:.2f},{gp.my:.2f}) {{{gp.size:.1f}}};"
            )
    tik.append(
        rf"\draw[Bar-Bar, gray] (0,{L.H + 6:.2f}) -- ({L.W:.2f},{L.H + 6:.2f}) node[midway, below=1pt, font=\small, text=black] {{{L.W:.1f} cm}};"
    )
    tik.append(
        rf"\draw[Bar-Bar, gray] ({L.W + 6:.2f},0) -- ({L.W + 6:.2f},{L.H:.2f}) node[midway, right=1pt, font=\small, text=black, rotate=-90, anchor=north] {{{L.H:.1f} cm}};"
    )
    return "\n".join(tik)


def thumbnail(L: Layout, frames: list[Frame], types: list[FrameType], mm_per_cm: float = 0.4) -> str:
    """A small unlabelled picture of the layout (default scale 1:25)."""
    return (rf"\begin{{tikzpicture}}[x={mm_per_cm}mm, y=-{mm_per_cm}mm]" + "\n"
            + _tikz_body(L, frames, types, 0.0, detail=False) + "\n" + r"\end{tikzpicture}")


def toc_entry(L: Layout) -> str:
    """Table-of-contents text for a layout page (needs hyperref)."""
    return (f"Layout {L.id}: {L.W:.1f} {TIMES} {L.H:.1f} cm, {len(L.rects)} frames, gap {fmt(L.g)} cm, "
            f"max.\\ deviation {L.metrics.max_dev:.1f} cm")


def _layout_page(L: Layout, frames: list[Frame], types: list[FrameType], annotate_threshold: float,
                 toc: str | None = None, label: str | None = None) -> str:
    colors = _colors(types)
    m = L.metrics
    body = _tikz_body(L, frames, types, annotate_threshold)
    head = ""
    if toc is not None:
        head = rf"\phantomsection\addcontentsline{{toc}}{{subsection}}{{{toc}}}" + "\n"
    if label is not None:
        head += rf"\label{{{label}}}" + "\n"

    used: dict[int, int] = {}
    for r in L.rects:
        used[frames[r.frame].type] = used.get(frames[r.frame].type, 0) + 1
    gap_sizes = [gp.size for gp in m.gaps]
    gap_range = f"{min(gap_sizes):.1f} -- {max(gap_sizes):.1f} cm" if gap_sizes else "--"
    notch = "none" if m.notch_len < 1e-6 else ", ".join(f"{k} {v:.0f} cm" for k, v in m.uncovered.items() if v > 0)
    coords = "\n".join(
        f"{i} & {fmt(r.w)} $\\times$ {fmt(r.h)} & {r.x:.1f} & {r.y:.1f} \\\\"
        for i, r in enumerate(L.rects, 1)
    )
    x = m.extras
    if x.n_large >= 2:
        touch = "not touching" if x.large_touch_pairs == 0 else f"touching {x.large_touch_pairs}$\\times$ ({x.large_touch:.0f} cm)"
        large = f"{x.n_large}, {touch}, min.\\ dist.\\ {x.large_min_dist:.0f} cm"
    else:
        large = str(x.n_large)
    return rf"""{head}\begin{{minipage}}[t]{{0.74\textwidth}}\vspace{{0pt}}
{{\LARGE\bfseries Layout {L.id}}}\hspace{{1.5em}}{{\large {L.W:.1f} $\times$ {L.H:.1f} cm \quad gap {fmt(L.g)} cm \quad {len(L.rects)} frames}}\\[8pt]
\begin{{tikzpicture}}[x=1mm, y=-1mm]
{body}
\end{{tikzpicture}}\\[4pt]
{{\scriptsize Scale 1:10 (1 mm = 1 cm). Red numbers mark gaps that deviate from the nominal gap by more than {annotate_threshold} cm. Small grey numbers index the coordinate table.}}
\end{{minipage}}\hfill
\begin{{minipage}}[t]{{0.25\textwidth}}\vspace{{0pt}}
\small
\begin{{tabular}}{{@{{}}l l@{{}}}}
\toprule
Outer size & {L.W:.1f} $\times$ {L.H:.1f} cm \\
Nominal gap & {fmt(L.g)} cm \\
Actual gaps & {gap_range} \\
Max.\ deviation & {m.max_dev:.1f} cm \\
Mean deviation & {m.mean_dev:.2f} cm \\
Border notches & {notch} \\
Corners filled & {m.corners} / 4 \\
Frames used & {len(L.rects)} of {len(frames)} \\
Large frames & {{\scriptsize {large}}} \\
Entropy & {x.entropy:.2f} \\
\quad {{\scriptsize mix / variety}} & {{\scriptsize {x.type_mix:.2f} / {x.variety:.2f}}} \\
\quad {{\scriptsize stagger / lines}} & {{\scriptsize {x.stagger:.2f} / {x.lines:.2f} ({x.through_lines} through)}} \\
\bottomrule
\end{{tabular}}

\vspace{{8pt}}
\scriptsize
{_inventory_table(types, used, colors)}

\vspace{{8pt}}
{{\bfseries Positions}} (top-left corner, cm)\\[2pt]
\tiny
\begin{{tabular}}{{@{{}}r l r r@{{}}}}
\toprule
\# & frame w $\times$ h & x & y \\
\midrule
{coords}
\bottomrule
\end{{tabular}}
\end{{minipage}}
\clearpage
"""


def write_tex(path: Path, layouts: list[Layout], frames: list[Frame], types: list[FrameType], params: dict,
              annotate_threshold: float = 0.25) -> None:
    parts = [_preamble(types), _overview_page(layouts, frames, types, params)]
    parts += [_layout_page(L, frames, types, annotate_threshold) for L in layouts]
    parts.append("\\end{document}\n")
    path.write_text("\n".join(parts), encoding="utf-8")


def layout_dict(L: Layout, frames: list[Frame], types: list[FrameType]) -> dict:
    return {
        "id": L.id,
        "width": L.W,
        "height": L.H,
        "gap": L.g,
        "cost": L.cost,
        "tree": L.tree,
        "frames": [
            {
                "index": i,
                "frame_id": r.frame,
                "type": types[frames[r.frame].type].name,
                "outer": types[frames[r.frame].type].outer,
                "x": round(r.x, 3),
                "y": round(r.y, 3),
                "w": r.w,
                "h": r.h,
            }
            for i, r in enumerate(L.rects, 1)
        ],
        "metrics": {
            "max_gap_deviation": L.metrics.max_dev,
            "mean_gap_deviation": L.metrics.mean_dev,
            "notches": L.metrics.uncovered,
            "corners_filled": L.metrics.corners,
            "dead_fraction": L.metrics.dead_frac,
            "gaps": [asdict(gp) for gp in L.metrics.gaps],
            "large_frames": {
                "count": L.metrics.extras.n_large,
                "touching_pairs": L.metrics.extras.large_touch_pairs,
                "touching_edge_cm": L.metrics.extras.large_touch,
                "min_centre_distance": None if L.metrics.extras.large_min_dist == float("inf")
                else L.metrics.extras.large_min_dist,
            },
            "entropy": {
                "total": L.metrics.extras.entropy,
                "type_mix": L.metrics.extras.type_mix,
                "variety": L.metrics.extras.variety,
                "stagger": L.metrics.extras.stagger,
                "lines": L.metrics.extras.lines,
                "through_lines": L.metrics.extras.through_lines,
            },
        },
    }


def write_json(path: Path, layouts: list[Layout], frames: list[Frame], types: list[FrameType], params: dict) -> None:
    data = {"params": params, "layouts": [layout_dict(L, frames, types) for L in layouts]}
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")


def compile_pdf(tex: Path) -> Path | None:
    """Run latexmk; returns the PDF path, or None (with a message) if latexmk is missing or fails.

    A failed compilation is not fatal: the .tex is kept and can be compiled elsewhere.
    """
    exe = shutil.which("latexmk") or shutil.which("/Library/TeX/texbin/latexmk")
    if exe is None:
        print("latexmk not found; skipping PDF compilation", file=sys.stderr)
        return None
    cmd = [exe, "-pdf", "-interaction=nonstopmode", "-halt-on-error", "-quiet", tex.name]
    res = subprocess.run(cmd, cwd=tex.parent, capture_output=True, text=True)
    if res.returncode != 0:
        print(res.stdout[-3000:], file=sys.stderr)
        print(res.stderr[-1000:], file=sys.stderr)
        print(f"LaTeX compilation of {tex} failed (missing packages?); the .tex file is kept", file=sys.stderr)
        return None
    subprocess.run([exe, "-c", "-quiet", tex.name], cwd=tex.parent, capture_output=True)
    return tex.with_suffix(".pdf")
