#!/usr/bin/env bash
# Sub-analysis: the baseline search plus extra constraints (one output directory each):
#   large-apart    2-3 large frames (the 40x50 ones) that do not touch and sit >= 70 cm apart
#   entropy        reward for an irregular ("random") looking constellation
#   large-entropy  both
# Afterwards collage-report combines these with every other out/<name>/ (e.g. the
# configurations of run_experiments.sh) into out/report/report.tex, with its own chapter
# for the sub-analysis.
#
# Usage:  scripts/run_subanalysis.sh [TOTAL_MINUTES] [RUNS]      (defaults: 60 min, 4 x cores)
set -euo pipefail
cd "$(dirname "$0")/.."

TOTAL_MINUTES=${1:-60}
CORES=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)
RUNS=${2:-$((4 * CORES))}
N_CONFIGS=3
MINUTES=$(python3 -c "print(round($TOTAL_MINUTES / $N_CONFIGS, 2))")
COMMON=(--runs "$RUNS" --minutes "$MINUTES" --layouts 16)
echo "budget $TOTAL_MINUTES min total, $MINUTES min per configuration, $RUNS runs each on $CORES cores"

run() {
  local name=$1; shift
  echo "=== $name  ($(date))"
  uv run collage "${COMMON[@]}" --out "out/$name" "$@" 2>&1 | grep --line-buffered -v '^run '
}

run large-apart    --large-count 2-3 --large-apart
run entropy        --entropy 40
run large-entropy  --large-count 2-3 --large-apart --entropy 40

echo "=== report: one document for all configurations"
uv run collage-report --layouts 16 --out out/report 2>&1 | grep --line-buffered -v '^run '
echo "done $(date)"
