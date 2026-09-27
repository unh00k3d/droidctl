"""Pure geometry for the snapshot's spatial layer: rects, rows, grids, labels, maps.

Nothing here talks to a device. Rects are ``(left, top, right, bottom)`` tuples in
device px; a rect with ``right <= left`` or ``bottom <= top`` is empty (Android
reports nodes clipped away by their window as *inverted* bounds).

Used by snapshot.py (regions, rows, grids, inferred labels, --geo, --map) and by
`where` / the spatial locators (--right-of, --below, ...), which the resolver and
actions build on.
"""
import re


# --------------------------------------------------------------------------
# rects
# --------------------------------------------------------------------------
def rect(b):
    """A tuple rect from a [l, t, r, b] list, or None if missing, empty or inverted."""
    if not b or len(b) != 4:
        return None
    l, t, r, bt = b
    if r <= l or bt <= t:
        return None
    return (l, t, r, bt)


def area(r):
    return 0 if r is None else max(0, r[2] - r[0]) * max(0, r[3] - r[1])


def width(r):
    return r[2] - r[0]


def height(r):
    return r[3] - r[1]


def center(r):
    return ((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)


def inter(a, b):
    """Intersection of two rects, or None."""
    if a is None or b is None:
        return None
    r = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    return r if r[2] > r[0] and r[3] > r[1] else None


def contains(outer, point):
    x, y = point
    return outer[0] <= x < outer[2] and outer[1] <= y < outer[3]


def subtract(r, cut):
    """``r`` minus ``cut`` as a list of up to four disjoint rects."""
    c = inter(r, cut)
    if c is None:
        return [r]
    out = []
    if c[1] > r[1]:
        out.append((r[0], r[1], r[2], c[1]))            # band above the cut
    if c[3] < r[3]:
        out.append((r[0], c[3], r[2], r[3]))            # band below
    if c[0] > r[0]:
        out.append((r[0], c[1], c[0], c[3]))            # left of the cut
    if c[2] < r[2]:
        out.append((c[2], c[1], r[2], c[3]))            # right of the cut
    return out


def subtract_all(r, cuts):
    """The visible pieces of ``r`` once every rect in ``cuts`` is removed."""
    if r is None:
        return []
    pieces = [r]
    for c in cuts:
        if c is None:
            continue
        pieces = [p for piece in pieces for p in subtract(piece, c)]
        if not pieces:
            break
    return pieces


def bbox(pieces):
    """The bounding box of a list of rects, or None."""
    if not pieces:
        return None
    return (min(p[0] for p in pieces), min(p[1] for p in pieces),
            max(p[2] for p in pieces), max(p[3] for p in pieces))


def largest(pieces):
    return max(pieces, key=area) if pieces else None


def near(a, b, tol=8):
    """Every edge of ``a`` within ``tol`` px of ``b``'s."""
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def vspan_overlap(a, b):
    """Vertical overlap as a fraction of the shorter of the two heights."""
    ov = min(a[3], b[3]) - max(a[1], b[1])
    h = min(height(a), height(b))
    return ov / h if h > 0 and ov > 0 else 0.0


def hspan_overlap(a, b):
    ov = min(a[2], b[2]) - max(a[0], b[0])
    w = min(width(a), width(b))
    return ov / w if w > 0 and ov > 0 else 0.0


def overlap_ratio(a, b):
    """Intersection area over the smaller rect's area."""
    i = inter(a, b)
    small = min(area(a), area(b))
    return area(i) / small if i and small else 0.0


# --------------------------------------------------------------------------
# rows and grids
# --------------------------------------------------------------------------
def group_rows(items, key):
    """Group items into reading-order rows.

    Items whose vertical spans overlap by at least half of the shorter height
    share a row (compared against the row's first item, so rows don't chain
    down a staircase). Rows come top to bottom, items left to right.
    """
    rows = []
    for it in sorted(items, key=lambda i: (key(i)[1], key(i)[0])):
        r = key(it)
        for row in rows:
            if vspan_overlap(key(row[0]), r) >= 0.5:
                row.append(it)
                break
        else:
            rows.append([it])
    for row in rows:
        row.sort(key=lambda i: key(i)[0])
    rows.sort(key=lambda row: min(key(i)[1] for i in row))
    return rows


def grid_columns(rows, key, tol_frac=0.25):
    """If ``rows`` form a grid (>= 2 rows, >= 2 columns, cells of similar size and
    aligned columns), return the sorted column centres; else None."""
    if len(rows) < 2 or max(len(r) for r in rows) < 2:
        return None
    cells = [key(i) for row in rows for i in row]
    if len(cells) < 4:
        return None
    ws = sorted(width(c) for c in cells)
    hs = sorted(height(c) for c in cells)
    mw, mh = ws[len(ws) // 2], hs[len(hs) // 2]
    if any(abs(width(c) - mw) > tol_frac * mw or abs(height(c) - mh) > tol_frac * mh for c in cells):
        return None
    cols = []
    for c in sorted(center(c)[0] for c in cells):
        if cols and c - cols[-1][-1] <= mw * 0.4:
            cols[-1].append(c)
        else:
            cols.append([c])
    if len(cols) < 2 or len(cols) > max(len(r) for r in rows) + 1:
        return None
    return [sum(c) // len(c) for c in cols]


def column_of(r, cols):
    x = center(r)[0]
    return min(range(len(cols)), key=lambda i: abs(cols[i] - x))


# --------------------------------------------------------------------------
# inferred labels
# --------------------------------------------------------------------------
_ID_PREFIX = re.compile(r"^(ic|icon|btn|button|iv|img|image|imgbtn|action|menu|nav|tv|txt|bt|ib)(?=[_A-Z0-9])_?")
_ID_SUFFIX = re.compile(r"_?(button|btn|icon|ic|iv|img|image|view|layout|container)$", re.I)


def short_id(res_id):
    """``com.pkg:id/ic_share`` -> ``ic_share``; Compose test tags pass through."""
    if not res_id:
        return ""
    return res_id.split(":id/", 1)[-1]


def label_from_id(res_id):
    """A guessed label from a resource-id name (``ic_share`` -> ``share``), or None."""
    name = short_id(res_id)
    if not name:
        return None
    name = _ID_PREFIX.sub("", name)
    name = _ID_SUFFIX.sub("", name) or name
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)   # camelCase -> words
    name = re.sub(r"[_\-.]+", " ", name).strip().lower()
    return name or None


# --------------------------------------------------------------------------
# neighbours and relative locators
# --------------------------------------------------------------------------
# 16 px of slack: Compose siblings often overlap their neighbour by a few px
def right_of(r, anchor):
    return r[0] >= anchor[2] - 16 and vspan_overlap(r, anchor) > 0


def left_of(r, anchor):
    return r[2] <= anchor[0] + 16 and vspan_overlap(r, anchor) > 0


def below(r, anchor):
    return r[1] >= anchor[3] - 16 and hspan_overlap(r, anchor) > 0


def above(r, anchor):
    return r[3] <= anchor[1] + 16 and hspan_overlap(r, anchor) > 0


def gap(r, anchor, direction):
    """Distance in px from ``anchor`` to ``r`` along ``direction``."""
    return {"right": r[0] - anchor[2], "left": anchor[0] - r[2],
            "below": r[1] - anchor[3], "above": anchor[1] - r[3]}[direction]


RELATIONS = {"right": right_of, "left": left_of, "below": below, "above": above}


def nearest(items, anchor, direction, key):
    """The item nearest to ``anchor`` in ``direction`` (sharing a row for left/right,
    a column for above/below), or None."""
    test = RELATIONS[direction]
    cands = [i for i in items if key(i) != anchor and test(key(i), anchor)]
    return min(cands, key=lambda i: (max(0, gap(key(i), anchor, direction)),
                                     abs(center(key(i))[1] - center(anchor)[1])
                                     + abs(center(key(i))[0] - center(anchor)[0]))) if cands else None


def near_to(items, anchor, key):
    """Items sorted by centre distance to ``anchor`` (for --near)."""
    cx, cy = center(anchor)
    return sorted((i for i in items if key(i) != anchor),
                  key=lambda i: (center(key(i))[0] - cx) ** 2 + (center(key(i))[1] - cy) ** 2)


# --------------------------------------------------------------------------
# --geo and --map
# --------------------------------------------------------------------------
def geo(r, w, h):
    """``@x,y wxh`` in whole screen percentages (top-left corner, size)."""
    return (f"@{round(100 * r[0] / w)},{round(100 * r[1] / h)} "
            f"{max(1, round(100 * width(r) / w))}x{max(1, round(100 * height(r) / h))}")


def render_map(boxes, w, h, cols=48, rows=24):
    """A coarse ASCII wireframe of the screen.

    ``boxes`` is a list of ``(label, rect)`` drawn in order (later on top). A box
    big enough gets a ``+--+`` frame with the label in its top-left corner; a
    small one is just its label at the box's centre.
    """
    grid = [[" "] * cols for _ in range(rows)]

    def cx(x):
        return min(cols - 1, max(0, int(x * cols / w)))

    def cy(y):
        return min(rows - 1, max(0, int(y * rows / h)))

    def put(x, y, s):
        for i, ch in enumerate(s):
            if 0 <= x + i < cols:
                grid[y][x + i] = ch

    for label, r in boxes:
        x0, y0, x1, y1 = cx(r[0]), cy(r[1]), cx(r[2] - 1), cy(r[3] - 1)
        if x1 - x0 >= len(label) + 1 and y1 - y0 >= 1:
            for x in range(x0 + 1, x1):
                grid[y0][x] = grid[y1][x] = "-"
            for y in range(y0 + 1, y1):
                grid[y][x0] = grid[y][x1] = "|"
                for x in range(x0 + 1, x1):
                    grid[y][x] = " "
            for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
                grid[y][x] = "+"
            put(x0 + 1, y0 + (1 if y1 - y0 >= 2 else 0), label)
        else:
            mx, my = (x0 + x1) // 2, (y0 + y1) // 2
            put(max(0, min(cols - len(label), mx - len(label) // 2)), my, label)
    border = "+" + "-" * cols + "+"
    return [border] + ["|" + "".join(row) + "|" for row in grid] + [border]
