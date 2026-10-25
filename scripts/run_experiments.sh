#!/usr/bin/env bash
# Runs a set of search configurations (one output directory each), renders every
# result into its own out/<name>/layouts.tex (+ .pdf if latexmk is installed) and
# finally writes one combined document out/report/report.tex with a table of contents.
#
# Usage:  scripts/run_experiments.sh [TOTAL_MINUTES] [RUNS]
#   TOTAL_MINUTES  wall-clock budget for the whole script (default 120); it is split
#                  evenly over the 6 configurations below and the script ends after
#                  roughly that time regardless of the machine's speed or core count
#   RUNS           independent annealing runs per configuration (default 4 x cores)
#
# The search is CPU bound (one process per core, little memory). Interrupting is safe:
# finished configurations keep their out/<name>/candidates.json, which can be
# re-rendered with  uv run collage --candidates out/<name>/candidates.json --out out/<name>
set -euo pipefail
cd "$(dirname "$0")/.."

TOTAL_MINUTES=${1:-120}
CORES=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)
RUNS=${2:-$((4 * CORES))}
N_CONFIGS=6
MINUTES=$(python3 -c "print(round($TOTAL_MINUTES / $N_CONFIGS, 2))")
COMMON=(--runs "$RUNS" --minutes "$MINUTES" --layouts 16)
echo "budget $TOTAL_MINUTES min total, $MINUTES min per configuration, $RUNS runs each on $CORES cores"

run() {
  local name=$1; shift
  echo "=== $name  ($(date))"
  uv run collage "${COMMON[@]}" --out "out/$name" "$@" 2>&1 | grep --line-buffered -v '^run '
}

run free-gap                       # gap is an annealing variable in [1.5, 6]
run per-run-gap   --gap-mode per-run   # every run keeps one gap from the grid (diversity)
run proportional  --slack-rule proportional   # bigger frames get bigger gaps where slack is unavoidable
run gap-3         --gap 3
run linear-cost   --cost-shape linear
run free-gap-s1   --seed 1

echo "=== report: one document for all configurations"
uv run collage-report --layouts 16 --out out/report 2>&1 | grep --line-buffered -v '^run '
echo "done $(date)"
