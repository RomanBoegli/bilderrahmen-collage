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
ranked: no notches first, then smallest maximum gap deviation, then more
frames, then closeness to the target size.

Frame orientation is taken as listed in the sheet (landscape stays landscape).
The picture-size labels (13x18, 21x30, ...) assume IKEA RIBBA frames, whose
outer size is the picture size + 2.5 cm; the layout itself only uses the outer
sizes from the sheet.
