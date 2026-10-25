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
scripts/run_experiments.sh 64 3000000               # ~6 configurations, see the script
```

Rough runtime: one run of 1M iterations takes about a minute on one core, so
64 runs x 3M iterations take ~3 h on one core, ~12 min on 16 cores, per
configuration. Each configuration writes `out/<name>/candidates.json` (all
candidate states, resumable), `layouts.json` and `layouts.tex`. If LaTeX is not
installed on the VM, commit `out/` and compile at home:

```sh
uv run collage --candidates out/combined/candidates.json --layouts 20 --out out/combined
# or just:  cd out/combined && latexmk -pdf layouts.tex
```

Single configuration by hand, e.g. 128 runs of 5M iterations with a fixed 3 cm gap:

```sh
uv run collage --runs 128 --iters 5000000 --gap 3 --layouts 16 --out out/gap3
```

Useful knobs: `--delta` (gap deviation that counts as harmless, default 0.5 cm),
`--frame-bonus` (push towards using more frames), `--min-frames`, `--tol`,
`--cost-shape linear` (smoother landscape), `--t-start` (default: calibrated per
run), `--gap-mode per-run` (each run keeps one gap value, more diverse results).
