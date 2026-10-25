import math
import random

from collage.inventory import Frame
from collage.layout import (Params, evaluate, extras, extras_cost, metrics, place, place_from_eval,
                            regular_neighbours, through_lines, total_cost)


def mk(*dims):
    return [Frame(i, i, w, h) for i, (w, h) in enumerate(dims)]


def test_place_from_eval_matches_place():
    from collage.search import SearchConfig, greedy_init, mutate

    rng = random.Random(3)
    frames = mk(*[(rng.choice([15.5, 20.5, 25.5, 32.5, 42.5]), rng.choice([15.5, 23.5, 32.5, 42.5])) for _ in range(16)])
    p = Params(slack_rule="proportional")
    cfg = SearchConfig(min_frames=3)
    t, g = greedy_init(frames, 3.0, p, rng, 3), 3.0
    _, e = total_cost(t, frames, g, p)
    for _ in range(200):
        t, g = mutate(t, g, e, frames, cfg, rng)
        _, e = total_cost(t, frames, g, p)
        fast = sorted(place_from_eval(t, e, frames, g, p), key=lambda r: (r.y, r.x))
        slow, _, _ = place(t, frames, g, p)
        assert [(r.frame, round(r.x, 6), round(r.y, 6)) for r in fast] == [(r.frame, round(r.x, 6), round(r.y, 6)) for r in slow]


def test_grid_measures():
    # 2 x 2 grid of equal frames with g = 2: two through-lines, no variety, low stagger
    frames = mk((10, 10), (10, 10), (10, 10), (10, 10))
    t = ["H", ["V", 0, 1], ["V", 2, 3]]
    rects, W, H = place(t, frames, 2.0, Params())
    assert through_lines(rects) == 2
    pairs = regular_neighbours(rects, 2.0)
    assert len(pairs) == 4
    x = extras(rects, 2.0, Params(n_types=9))
    assert x.variety == 0.0 and x.type_mix == 0.0
    assert math.isclose(x.stagger, 8 / 16)  # x: 0,10,12,22 and y: 0,10,12,22 -> 8 distinct of 16 edges
    assert math.isclose(x.lines, 0.75)


def test_large_frames_touching_and_cost():
    # two large frames side by side (touching over 42.5 cm), a third far away below
    frames = mk((52.5, 42.5), (52.5, 42.5), (42.5, 52.5), (52.5, 52.5))
    p = Params(large_min=2, large_max=3, large_apart=True)
    t = ["H", ["V", 0, 1], ["V", 2, 3]]
    rects, W, H = place(t, frames, 2.0, p)
    x = extras(rects, 2.0, p)
    assert x.n_large == 4 and x.large_touch_pairs >= 1
    assert math.isclose(sum(ov for i, j, ov in regular_neighbours(rects, 2.0) if {i, j} <= {0, 1}), 42.5)
    c = extras_cost(x, p)
    assert c > p.w_large  # one large frame too many plus touching
    m = metrics(rects, W, H, 2.0, p)
    assert m.extras.n_large == 4
    # without constraints the extras cost nothing
    assert extras_cost(x, Params()) == 0.0


def test_total_cost_adds_entropy_term():
    frames = mk((20.5, 15.5), (20.5, 15.5), (42.5, 32.5))
    t = ["V", ["H", 0, 1], 2]
    c0, _ = total_cost(t, frames, 1.5, Params())
    c1, e = total_cost(t, frames, 1.5, Params(w_entropy=10.0))
    rects = place_from_eval(t, e, frames, 1.5, Params())
    ent = extras(rects, 1.5, Params(w_entropy=10.0)).entropy
    assert 0.0 < ent < 1.0
    assert math.isclose(c1 - c0, 10.0 * (1.0 - ent))
