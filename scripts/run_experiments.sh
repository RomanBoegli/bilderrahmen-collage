#!/usr/bin/env bash
# Runs a set of search configurations (one output directory each) and renders every
# result into its own out/<name>/layouts.tex (+ .pdf if latexmk is installed).
#
# Usage:  scripts/run_experiments.sh [RUNS] [ITERS]
#   RUNS   independent annealing runs per configuration (default 64)
#   ITERS  iterations per run (default 3000000; ~1 min per 1M iterations and run)
#
# The search is CPU bound (one process per core, little memory). Interrupting is safe:
# finished configurations keep their out/<name>/candidates.json, which can be
# re-rendered with  uv run collage --candidates out/<name>/candidates.json --out out/<name>
set -euo pipefail
cd "$(dirname "$0")/.."

RUNS=${1:-64}
ITERS=${2:-3000000}
COMMON=(--runs "$RUNS" --iters "$ITERS" --layouts 16)

run() {
  local name=$1; shift
  echo "=== $name  ($(date))"
  uv run collage "${COMMON[@]}" --out "out/$name" "$@" 2>&1 | grep -v '^run '
}

run free-gap                       # gap is an annealing variable in [1.5, 6]
run per-run-gap   --gap-mode per-run   # every run keeps one gap from the grid (diversity)
run proportional  --slack-rule proportional   # bigger frames get bigger gaps where slack is unavoidable
run gap-3         --gap 3
run linear-cost   --cost-shape linear
run free-gap-s1   --seed 1

echo "=== combined ranking of all configurations"
uv run collage --candidates "$(ls out/*/candidates.json | paste -sd, -)" --layouts 20 --out out/combined 2>&1 | grep -v '^run '
echo "done $(date)"
