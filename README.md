# bilderrahmen-collage

Finds arrangements ("constellations") of our picture frames that fill a roughly
180 x 120 cm area on the wall such that

* the gaps between neighbouring frames are as uniform as possible, and
* the outer border of the whole collage is a sharp rectangle (straight edges,
  filled corners), so the collage reads as one big frame.

Not all frames have to be used. The inventory (outer frame sizes and counts) is
read from `Bilderwand.xlsx` (columns: `Format` as `W x H` in cm, count).

## Setup

```sh
uv sync                  # creates .venv and installs the package + dev deps
uv run pytest            # unit tests
```

LaTeX (`latexmk` + `pdflatex` with TikZ, e.g. MacTeX) is needed to produce the PDF;
without it use `--no-compile` and compile `out/layouts.tex` elsewhere.

## Usage

```sh
uv run collage                      # default: 32 annealing runs, 12 layouts -> out/layouts.pdf
uv run collage --gap 3              # fixed nominal gap of 3 cm
uv run collage --target 190x110 --tol 5
uv run collage --slack-rule proportional
uv run collage --help
```

Outputs in `out/`:

* `layouts.pdf` – overview page plus one page per layout (scale 1:10) with frame
  labels, dimensions, a stats box, the inventory usage and a coordinate table of
  all frame positions (top-left corners in cm). Gaps that deviate from the
  layout's nominal gap by more than 0.25 cm are printed in red.
* `layouts.json` – the same data machine readable (positions, gaps, metrics).
* `layouts.tex` – the LaTeX source.
* `candidates.json` – all candidate states of the search (re-renderable with
  `--candidates`, and the input of `collage-report`).

## Sub-analysis: large frames apart, entropy

Two optional constraint families can be added to any search:

```sh
uv run collage --large-count 2-3 --large-apart     # 2-3 large frames that do not touch, >= 70 cm apart
uv run collage --entropy 40                        # reward irregular ("random") constellations
uv run collage --large-count 2-3 --large-apart --entropy 40
scripts/run_subanalysis.sh 60                      # the three combinations above, 60 min in total
```

* *Large frames* are those with an outer area of at least `--large-area` (default
  2000 cm², i.e. the 52.5 x 42.5 and 42.5 x 52.5 frames). `--large-count MIN-MAX`
  requires that many of them; `--large-apart` penalises two large frames that face
  each other across a gap (in proportion to the edge they share, so barely touching
  is almost free) and pulls the closest pair towards a centre distance of at least
  `--large-min-dist` (default 70 cm).
* *Entropy* is a number in [0, 1] for how irregular a constellation looks: the mean
  of the format mix (Shannon entropy of the formats used), the share of neighbouring
  pairs with different formats, the share of distinct frame edge positions (grids
  score low) and a term for few straight gap lines crossing the whole collage.
  `--entropy W` adds `W * (1 - entropy)` to the cost (40 is a reasonable weight);
  layouts with equal rounded score are then ranked by entropy.

Both measures are computed for every layout and shown on its page and in
`layouts.json`; the report gets an extra chapter for the paradigms that use them.

## One report for several runs

`collage-report` combines the results of several search configurations
("paradigms", e.g. the ones `scripts/run_experiments.sh` produces) into one PDF
with a table of contents:

```sh
uv run collage-report                                   # every out/<name>/candidates.json -> out/report/report.pdf
uv run collage-report out/free-gap out/gap-3 --layouts 16 --top 20
```

The document has a title page with the inventory and a reading guide, the
table of contents (one entry per paradigm and per layout page), a section that
explains what distinguishes the paradigms (hand-written description plus the
parameters that differ from the baseline, i.e. the first directory), a table and
thumbnails of each paradigm's best layout, a combined ranking of all layouts in
the document with links to their pages, and then one section per paradigm with
its ranked layouts, one page each. Every paradigm's candidates are placed and
ranked with the parameters it was searched with (e.g. its slack rule).
`out/report/report.json` holds the same data machine readable.

## How it works

A layout is a *slicing tree*: frames are recursively placed side by side (`V`)
or on top of each other (`H`) with a nominal gap `g`. When two siblings are not
equally high (or wide) the smaller one has slack. Slack is absorbed by widening
the gaps inside the smaller sibling (spread evenly, or proportional to the
neighbouring frame sizes with `--slack-rule proportional`); if there are no
inner gaps it becomes dead space, which is pushed against the outer border when
the sibling touches it and centred otherwise.

A simulated-annealing search over trees *and* over the subset of frames used
minimises a cost made of: gap deviations (tolerance `--delta`, default 0.5 cm),
border notches (heavily penalised), distance from the target size (hard-ish
beyond `--tol`) and a small bonus per frame used. The nominal gap is part of
the search state (`--gap-min`/`--gap-max`) unless fixed with `--gap`.

Candidates from all runs are placed, measured exactly (actual gaps, border
coverage, filled corners), de-duplicated (mirror images count as equal) and
ranked by a combined score: maximum and mean gap deviation first, border
notches and missing corners second (a perfectly sharp border is a goal, not a
hard requirement), then more frames, then closeness to the target size.

Frame orientation is taken as listed in the sheet (landscape stays landscape).
All sizes are the measured outer dimensions of the frames including the wooden
border; frames are labelled with these outer sizes (width x height as hung).

## Running the search on a VM

The search is CPU bound and uses one process per core (memory use is tiny), so
more cores and more iterations are what help. On the VM:

```sh
git clone <this repo> && cd bilderrahmen-collage
curl -LsSf https://astral.sh/uv/install.sh | sh     # if uv is missing
uv sync
uv run pytest
scripts/run_experiments.sh 120                      # 6 configurations, ~2 h in total, then it stops
```

The script takes a total wall-clock budget in minutes (default 120) and splits
it over its six configurations; every annealing run gets an equal share and
its cooling schedule follows elapsed time, so the script ends after roughly the
budget on any machine. More cores simply mean more runs in the same time
(default 4 runs per core). Each configuration writes `out/<name>/candidates.json`
(all candidate states, resumable), `layouts.json` and `layouts.tex`; at the end
`collage-report` writes the combined document `out/report/report.tex`. If LaTeX
is not installed on the VM, commit `out/` and compile at home:

```sh
uv run collage-report --layouts 16 --out out/report     # -> out/report/report.pdf
# or just:  cd out/report && latexmk -pdf report.tex
```

Single configuration by hand, e.g. 128 runs within 30 minutes with a fixed 3 cm gap
(or `--iters N` instead of `--minutes` for a fixed number of iterations per run):

```sh
uv run collage --runs 128 --minutes 30 --gap 3 --layouts 16 --out out/gap3
```

Useful knobs: `--delta` (gap deviation that counts as harmless, default 0.5 cm),
`--frame-bonus` (push towards using more frames), `--min-frames`, `--tol`,
`--cost-shape linear` (smoother landscape), `--t-start` (default: calibrated per
run), `--gap-mode per-run` (each run keeps one gap value, more diverse results).
