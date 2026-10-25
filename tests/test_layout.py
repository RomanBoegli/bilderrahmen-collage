import math

from collage.inventory import Frame, FrameType, expand, parse_format
from collage.layout import Params, evaluate, metrics, neighbour_gaps, place, total_cost


def mk(*dims):
    return [Frame(i, i, w, h) for i, (w, h) in enumerate(dims)]


def test_parse_format():
    assert parse_format("20.5 x 15.5") == (20.5, 15.5)
    assert parse_format("32,5X42,5") == (32.5, 42.5)


def test_expand_and_labels():
    t = FrameType(0, 20.5, 15.5, 2)
    assert t.name == "15.5x20.5 L" and t.outer == "20.5x15.5"
    assert len(expand([t])) == 2


def test_exact_row_has_no_cost():
    # two 13x18 landscape stacked (15.5+1.5+15.5 = 32.5 high) next to a 30x40 landscape (42.5 x 32.5)
    frames = mk((20.5, 15.5), (20.5, 15.5), (42.5, 32.5))
    p = Params()
    t = ["V", ["H", 0, 1], 2]
    e = evaluate(t, frames, 1.5, p)
    assert e.cost == 0.0 and e.max_dev == 0.0 and e.max_notch == 0.0
    rects, W, H = place(t, frames, 1.5, p)
    m = metrics(rects, W, H, 1.5)
    assert m.max_dev == 0.0 and m.notch_len == 0.0 and m.corners == 4
    assert math.isclose(W, 20.5 + 1.5 + 42.5) and math.isclose(H, 32.5)


def test_slack_is_absorbed_by_inner_gaps_and_placed():
    # stack of three 10x10 next to a 10x36 frame with g=2: natural stack height 34 -> 2 cm slack over 2 gaps
    frames = mk((10, 10), (10, 10), (10, 10), (10, 36))
    p = Params()
    t = ["V", ["H", ["H", 0, 1], 2], 3]
    e = evaluate(t, frames, 2.0, p)
    assert math.isclose(e.max_dev, 1.0)
    rects, W, H = place(t, frames, 2.0, p)
    assert math.isclose(W, 22) and math.isclose(H, 36)
    m = metrics(rects, W, H, 2.0)
    assert math.isclose(m.max_dev, 1.0)  # each inner gap is 3 instead of 2
    assert m.notch_len == 0 and m.corners == 4


def test_dead_space_at_border_is_notch():
    frames = mk((10, 10), (10, 20))
    p = Params()
    e = evaluate(["V", 0, 1], frames, 2.0, p)
    assert e.max_notch == 10.0
    rects, W, H = place(["V", 0, 1], frames, 2.0, p)
    m = metrics(rects, W, H, 2.0)
    assert m.uncovered["bottom"] == 12.0 and m.corners == 3  # 10 cm frame + 2 cm gap missing


def test_placement_never_overlaps_random_trees():
    import random

    from collage.search import SearchConfig, greedy_init, mutate

    rng = random.Random(1)
    frames = mk(*[(rng.choice([15.5, 20.5, 25.5, 32.5, 42.5]), rng.choice([15.5, 23.5, 32.5, 42.5])) for _ in range(20)])
    p = Params()
    cfg = SearchConfig(min_frames=3)
    t = greedy_init(frames, 3.0, p, rng, 3)
    g = 3.0
    _, e = total_cost(t, frames, g, p)
    for _ in range(500):
        t, g = mutate(t, g, e, frames, cfg, rng)
        rects, W, H = place(t, frames, g, p)  # asserts no overlap / inside box
        assert len(rects) == len(set(r.frame for r in rects))
        neighbour_gaps(rects)
        _, e = total_cost(t, frames, g, p)
