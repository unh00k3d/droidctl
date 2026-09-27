"""The pure geometry under the snapshot's spatial layer (no UI trees involved)."""
from droidctl import spatial as sp


def test_rect_reads_inverted_and_empty_bounds_as_none():
    assert sp.rect([0, 10, 100, 60]) == (0, 10, 100, 60)
    assert sp.rect([1118, 470, 1080, 899]) is None      # clipped away by its window
    assert sp.rect([540, 1923, 540, 1923]) is None      # zero area
    assert sp.rect(None) is None


def test_subtract_splits_into_disjoint_pieces_with_the_right_area():
    r = (0, 0, 100, 100)
    pieces = sp.subtract(r, (25, 25, 75, 75))
    assert len(pieces) == 4
    assert sum(sp.area(p) for p in pieces) == 100 * 100 - 50 * 50
    for i, a in enumerate(pieces):                       # disjoint
        for b in pieces[i + 1:]:
            assert sp.inter(a, b) is None
    assert sp.subtract(r, (200, 200, 300, 300)) == [r]
    assert sp.subtract(r, (0, 0, 100, 100)) == []


def test_subtract_all_models_a_keyboard_over_a_list():
    lst = (0, 300, 1080, 2094)
    ime = (0, 1217, 1080, 2094)
    pieces = sp.subtract_all(lst, [ime])
    assert pieces == [(0, 300, 1080, 1217)]
    assert sp.bbox(pieces) == (0, 300, 1080, 1217)


def test_bbox_keeps_a_box_split_by_a_small_edge_handle():
    box = (0, 242, 1080, 2094)
    pieces = sp.subtract_all(box, [(1038, 470, 1080, 899)])
    assert sp.bbox(pieces) == box
    assert sp.largest(pieces) != box


def test_group_rows_joins_overlapping_spans_left_to_right():
    a, b, c = (500, 100, 600, 200), (0, 110, 100, 190), (0, 300, 100, 400)
    rows = sp.group_rows([a, b, c], key=lambda r: r)
    assert rows == [[b, a], [c]]


def test_group_rows_does_not_chain_a_staircase():
    steps = [(i * 100, i * 60, i * 100 + 90, i * 60 + 100) for i in range(4)]
    rows = sp.group_rows(steps, key=lambda r: r)
    assert all(len(r) <= 2 for r in rows)


def test_grid_columns_finds_an_aligned_table_and_rejects_ragged_rows():
    cells = [[(x, y, x + 100, y + 100) for x in (0, 150, 300)] for y in (0, 150)]
    cols = sp.grid_columns(cells, key=lambda r: r)
    assert cols == [50, 200, 350]
    assert sp.column_of((150, 0, 250, 100), cols) == 1
    ragged = [[(0, 0, 100, 100), (150, 0, 700, 100)], [(0, 150, 100, 250), (150, 150, 250, 250)]]
    assert sp.grid_columns(ragged, key=lambda r: r) is None
    assert sp.grid_columns([cells[0]], key=lambda r: r) is None


def test_label_from_id():
    assert sp.label_from_id("com.x:id/ic_share") == "share"
    assert sp.label_from_id("com.x:id/btn_add_to_cart") == "add to cart"
    assert sp.label_from_id("buttonDrawer") == "drawer"
    assert sp.label_from_id("com.x:id/remove_icon") == "remove"
    assert sp.label_from_id("com.android.systemui:id/recent_apps") == "recent apps"
    assert sp.label_from_id(None) is None


def test_relations_and_nearest():
    anchor = (100, 100, 200, 200)
    right = (250, 120, 300, 180)
    far_right = (600, 120, 650, 180)
    below = (110, 300, 190, 350)
    items = [right, far_right, below]
    assert sp.right_of(right, anchor) and not sp.right_of(below, anchor)
    assert sp.below(below, anchor)
    assert sp.nearest(items, anchor, "right", key=lambda r: r) == right
    assert sp.nearest(items, anchor, "below", key=lambda r: r) == below
    assert sp.nearest(items, anchor, "left", key=lambda r: r) is None
    assert sp.near_to(items, anchor, key=lambda r: r)[0] == right


def test_geo_is_in_screen_percent():
    assert sp.geo((0, 63, 147, 210), 1080, 2220) == "@0,3 14x7"


def test_render_map_has_the_asked_size_and_draws_labels():
    lines = sp.render_map([("1", (0, 0, 540, 1110)), ("22", (900, 2000, 1000, 2100))], 1080, 2220)
    assert len(lines) == 24 + 2
    assert all(len(line) == 48 + 2 for line in lines)
    body = "\n".join(lines)
    assert "1" in body and "22" in body
