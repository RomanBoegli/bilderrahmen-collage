"""Render layouts as a LaTeX/TikZ document (one layout per page) and as JSON."""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from .inventory import Frame, FrameType, fmt
from .layout import Metrics, Rect, Tree

PALETTE = ["CFE4F2", "FBE2B4", "D4E9CC", "E5D3EA", "F5CACA", "F9F1B5", "CDE9E6", "E9D9C7", "DADADA"]


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


def _colors(types: list[FrameType]) -> dict[str, str]:
    keys = sorted({t.picture_key for t in types}, key=lambda k: float(k.split("x")[0]))
    return {k: f"ft{i}" for i, k in enumerate(keys)}


def _preamble(types: list[FrameType]) -> str:
    colors = _colors(types)
    defs = "\n".join(
        f"\\definecolor{{{name}}}{{HTML}}{{{PALETTE[i % len(PALETTE)]}}}" for i, name in enumerate(colors.values())
    )
    return rf"""\documentclass[a4paper,landscape,10pt]{{article}}
\usepackage[margin=10mm]{{geometry}}
\usepackage[T1]{{fontenc}}
\usepackage{{lmodern}}
\usepackage{{helvet}}
\renewcommand{{\familydefault}}{{\sfdefault}}
\usepackage{{tikz}}
\usetikzlibrary{{arrows.meta}}
\usepackage{{xcolor}}
\usepackage{{booktabs}}
\pagestyle{{empty}}
\setlength{{\parindent}}{{0pt}}
{defs}
\begin{{document}}
"""


def _inventory_table(types: list[FrameType], used: dict[int, int], colors: dict[str, str]) -> str:
    rows = []
    for t in types:
        swatch = rf"\tikz[baseline=-0.6ex]\fill[{colors[t.picture_key]},draw=black!60] (0,0) rectangle (1.2ex,1.2ex);"
        rows.append(
            f"{swatch} & {_esc(t.picture_key)} {t.orientation} & {_esc(t.outer)} & {used.get(t.idx, 0)} & {t.count} \\\\"
        )
    body = "\n".join(rows)
    return rf"""\begin{{tabular}}{{@{{}}c l l r r@{{}}}}
\toprule
 & picture & outer (cm) & used & have \\
\midrule
{body}
\bottomrule
\end{{tabular}}"""


def _summary_rows(layouts: list[Layout], frames: list[Frame], types: list[FrameType]) -> str:
    rows = []
    for L in layouts:
        m = L.metrics
        notch = "none" if m.notch_len < 1e-6 else f"{m.notch_len:.0f} cm"
        rows.append(
            f"{L.id} & {L.W:.1f} $\\times$ {L.H:.1f} & {fmt(L.g)} & {len(L.rects)} & "
            f"{m.max_dev:.1f} & {m.mean_dev:.2f} & {notch} & {m.corners}/4 \\\\"
        )
    return "\n".join(rows)


def _overview_page(layouts: list[Layout], frames: list[Frame], types: list[FrameType], params: dict) -> str:
    colors = _colors(types)
    have = {t.idx: 0 for t in types}
    inv = _inventory_table(types, have, colors).replace(" & used & have", " & & have").replace(
        "& 0 &", "& &"
    )
    plist = ", ".join(f"{k} = {v}" for k, v in params.items()).replace("_", r"\_")
    return rf"""{{\LARGE\bfseries Bilderwand -- layout candidates}}\\[4pt]
{{\small Target {params['target']} cm $\pm$ {params['tol']} cm, nominal gap free in
[{params['gap_min']}, {params['gap_max']}] cm, orientation fixed. Max.\ gap deviation = largest
difference between an actual gap and the layout's nominal gap; notches = parts of the outer border not
covered by a frame or a regular gap.}}\\[10pt]
\begin{{minipage}}[t]{{0.6\textwidth}}\vspace{{0pt}}
\begin{{tabular}}{{@{{}}r l r r r r l c@{{}}}}
\toprule
\# & size (cm) & gap & frames & max dev & mean dev & notches & corners \\
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


def _layout_page(L: Layout, frames: list[Frame], types: list[FrameType], annotate_threshold: float) -> str:
    colors = _colors(types)
    m = L.metrics
    tik = []
    tik.append(rf"\draw[gray!70, dashed, line width=0.4pt] (0,0) rectangle ({L.W:.2f},{L.H:.2f});")
    for i, r in enumerate(L.rects, 1):
        t = types[frames[r.frame].type]
        col = colors[t.picture_key]
        tik.append(
            rf"\filldraw[fill={col}, draw=black!80, line width=0.5pt] ({r.x:.2f},{r.y:.2f}) rectangle ({r.x2:.2f},{r.y2:.2f});"
        )
        cx, cy = (r.x + r.x2) / 2, (r.y + r.y2) / 2
        tik.append(
            rf"\node[align=center, inner sep=0pt] at ({cx:.2f},{cy:.2f}) "
            rf"{{\scriptsize\bfseries {_esc(t.picture_key)}\\[-2pt]\tiny {_esc(t.outer)}}};"
        )
        tik.append(rf"\node[anchor=north west, inner sep=1.2pt, font=\tiny, text=black!60] at ({r.x:.2f},{r.y:.2f}) {{{i}}};")
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
    body = "\n".join(tik)

    used: dict[int, int] = {}
    for r in L.rects:
        used[frames[r.frame].type] = used.get(frames[r.frame].type, 0) + 1
    gap_sizes = [gp.size for gp in m.gaps]
    gap_range = f"{min(gap_sizes):.1f} -- {max(gap_sizes):.1f} cm" if gap_sizes else "--"
    notch = "none" if m.notch_len < 1e-6 else ", ".join(f"{k} {v:.0f} cm" for k, v in m.uncovered.items() if v > 0)
    coords = "\n".join(
        f"{i} & {_esc(types[frames[r.frame].type].picture_key)} {types[frames[r.frame].type].orientation}"
        f" & {r.x:.1f} & {r.y:.1f} & {fmt(r.w)} $\\times$ {fmt(r.h)} \\\\"
        for i, r in enumerate(L.rects, 1)
    )
    return rf"""\begin{{minipage}}[t]{{0.74\textwidth}}\vspace{{0pt}}
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
\bottomrule
\end{{tabular}}

\vspace{{8pt}}
\scriptsize
{_inventory_table(types, used, colors)}

\vspace{{8pt}}
{{\bfseries Positions}} (top-left corner, cm)\\[2pt]
\tiny
\begin{{tabular}}{{@{{}}r l r r l@{{}}}}
\toprule
\# & frame & x & y & w $\times$ h \\
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


def write_json(path: Path, layouts: list[Layout], frames: list[Frame], types: list[FrameType], params: dict) -> None:
    data = {"params": params, "layouts": []}
    for L in layouts:
        data["layouts"].append(
            {
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
                },
            }
        )
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")


def compile_pdf(tex: Path) -> Path | None:
    exe = shutil.which("latexmk") or shutil.which("/Library/TeX/texbin/latexmk")
    if exe is None:
        print("latexmk not found; skipping PDF compilation")
        return None
    cmd = [exe, "-pdf", "-interaction=nonstopmode", "-halt-on-error", "-quiet", tex.name]
    res = subprocess.run(cmd, cwd=tex.parent, capture_output=True, text=True)
    if res.returncode != 0:
        print(res.stdout[-3000:])
        print(res.stderr[-1000:])
        raise SystemExit("LaTeX compilation failed")
    subprocess.run([exe, "-c", "-quiet", tex.name], cwd=tex.parent, capture_output=True)
    return tex.with_suffix(".pdf")
