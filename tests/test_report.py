import json
from pathlib import Path

from collage import report

XLSX = Path(__file__).resolve().parents[1] / "Bilderwand.xlsx"

BASE_ARGS = {
    "target": "180x120", "tol": 10.0, "gap": None, "gap_min": 1.5, "gap_max": 6.0, "gap_step": 0.5,
    "delta": 0.5, "slack_rule": "even", "gap_mode": "free", "min_frames": 10, "frame_bonus": 1.5,
    "cost_shape": "huber", "hard_margin": 10.0, "runs": 2, "iters": 1000000, "minutes": 1.0,
    "t_start": None, "t_end": 0.05, "seed": 0,
}


def _write(d: Path, args: dict, cands: list[dict]) -> None:
    d.mkdir(parents=True)
    (d / "candidates.json").write_text(json.dumps({"args": args, "candidates": cands}))


def test_report_tex_and_json(tmp_path):
    # frame ids follow the sheet order: 0-8 = 20.5x15.5, 13 = 42.5x32.5, 25 = 42.5x52.5, 26/27 = 52.5x42.5
    a = tmp_path / "free-gap"
    b = tmp_path / "gap-3"
    _write(a, BASE_ARGS, [
        {"tree": ["V", ["H", 0, 1], 13], "g": 1.5, "cost": 0.0, "run": 0},
        {"tree": ["H", ["V", 26, 27], ["V", 25, 13]], "g": 3.0, "cost": 5.0, "run": 1},
    ])
    _write(b, {**BASE_ARGS, "gap": 3.0, "seed": 1}, [
        {"tree": ["V", ["H", 0, 1], 13], "g": 1.5, "cost": 0.0, "run": 0},  # same layout as in free-gap
    ])
    out = tmp_path / "report"
    rc = report.main([str(a), str(b), "--xlsx", str(XLSX), "--out", str(out), "--no-compile", "--layouts", "5"])
    assert rc == 0
    tex = (out / "report.tex").read_text()
    assert r"\tableofcontents" in tex
    assert r"\section{free-gap}" in tex and r"\section{gap-3}" in tex
    assert tex.count(r"\addcontentsline{toc}{subsection}") == 3  # one entry per layout page
    assert r"\label{lay:gap-3:1}" in tex and r"\hyperref[lay:gap-3:1]" in tex
    assert "gap = 3, seed = 1" in tex  # parameters that differ from the baseline
    data = json.loads((out / "report.json").read_text())
    assert [p["name"] for p in data["paradigms"]] == ["free-gap", "gap-3"]
    assert len(data["combined"]) == 2  # the shared layout is merged
    shared = next(r for r in data["combined"] if len(r["found_by"]) == 2)
    assert {f["paradigm"] for f in shared["found_by"]} == {"free-gap", "gap-3"}


def test_diff_args_lists_only_changes():
    assert report.diff_args(BASE_ARGS, BASE_ARGS) == []
    assert report.diff_args({**BASE_ARGS, "slack_rule": "proportional"}, BASE_ARGS) == ["slack_rule = proportional"]
