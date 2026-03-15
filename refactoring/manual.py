from dsl import *


def _b0f4d537_find_separator(I):
    for c in range(width(I)):
        if all(I[r][c] == 5 for r in range(height(I))):
            return c
    return -1


def verify_b0f4d537(I: Grid) -> Grid:
    h_I, w_I = height(I), width(I)
    sep_c = _b0f4d537_find_separator(I)

    # Find active cells (not background 0, yellow 4, or grey 5)
    active = set()
    for r in range(h_I):
        for c in range(w_I):
            if I[r][c] not in (0, 4, 5):
                active.add((r, c))

    # Extract template (x15)
    t_top, t_left = uppermost(active), leftmost(active)
    t_h, t_w = lowermost(active) - t_top + 1, rightmost(active) - t_left + 1
    template = crop(I, (t_top, t_left), (t_h, t_w))

    # Identify mask area (x21)
    if t_left < sep_c:
        mask_range = range(sep_c + 1, w_I)
    else:
        mask_range = range(0, sep_c)
    mask = [tuple(I[r][c] for c in mask_range) for r in range(h_I)]

    # Column mapping logic
    t_cols = dmirror(template)
    base_col = mostcommon(t_cols)
    non_base_col_idxs = [i for i, col in enumerate(t_cols) if col != base_col]
    base_col_idx = t_cols.index(base_col)

    mask_col_yellows = [sum(1 for r in range(h_I) if mask[r][c] == 4) for c in range(len(mask[0]))]
    max_yellow = max(mask_col_yellows) if mask_col_yellows else 0
    special_mask_cols = [c for c, count in enumerate(mask_col_yellows) if count == max_yellow]

    col_map = []
    for c in range(len(mask[0])):
        if c in special_mask_cols:
            idx = special_mask_cols.index(c)
            col_map.append(non_base_col_idxs[idx] if idx < len(non_base_col_idxs) else base_col_idx)
        else:
            col_map.append(base_col_idx)

    # Row mapping logic
    t_rows = template
    base_row = mostcommon(t_rows)
    non_base_row_idxs = [i for i, row in enumerate(t_rows) if row != base_row]
    base_row_idx = t_rows.index(base_row)

    mask_row_yellows = [row.count(4) for row in mask]
    max_row_yellow = max(mask_row_yellows) if mask_row_yellows else 0
    special_mask_rows = [r for r, count in enumerate(mask_row_yellows) if count == max_row_yellow]

    row_map = []
    for r in range(h_I):
        if r in special_mask_rows:
            idx = special_mask_rows.index(r)
            row_map.append(non_base_row_idxs[idx] if idx < len(non_base_row_idxs) else base_row_idx)
        else:
            row_map.append(base_row_idx)

    # Final construction
    res = [[template[row_map[r]][col_map[c]] for c in range(len(mask[0]))] for r in range(h_I)]
    return tuple(tuple(r) for r in res)


def verify_0607ce86(I: Grid) -> Grid:
    h, w = height(I), width(I)

    # helper for Non-zero count
    def nz(element): return sum(1 for v in (element if isinstance(element, tuple) else [x[0] for x in element]) if v != 0)
    cols = dmirror(I)
    col_counts = [nz(cols[c]) for c in range(w)]
    row_counts = [nz(row) for row in I]

    def find_threshold(counts):
        s = sorted(counts, reverse=True)
        max_gap, thresh = -1, 0
        for i in range(len(s) - 1):
            gap = s[i] - s[i+1]
            if gap > max_gap:
                max_gap, thresh = gap, (s[i] + s[i+1]) / 2
        return thresh

    t_c = find_threshold(col_counts)
    t_r = find_threshold(row_counts)

    # 2. Extract tiles
    cols = [c for c in range(w) if col_counts[c] > t_c]
    rows = [r for r in range(h) if row_counts[r] > t_r]

    def group_consec(indices):
        if not indices: return []
        res, start = [], indices[0]
        for i in range(1, len(indices)):
            if indices[i] != indices[i-1] + 1:
                res.append((start, indices[i-1]+1)); start = indices[i]
        res.append((start, indices[-1]+1))
        return res

    g_c, g_r = group_consec(cols), group_consec(rows)
    tile_w = mostcommon([g[1]-g[0] for g in g_c])
    tile_h = mostcommon([g[1]-g[0] for g in g_r])

    f_c = [g for g in g_c if g[1]-g[0] == tile_w]
    f_r = [g for g in g_r if g[1]-g[0] == tile_h]

    # 3. Majority vote over tiles
    res = []
    for r in range(tile_h):
        row = []
        for c in range(tile_w):
            votes = [I[f_r[gi][0] + r][f_c[gj][0] + c] for gi in range(len(f_r)) for gj in range(len(f_c))]
            row.append(mostcommon(votes))
        res.append(tuple(row))
    tile = tuple(res)

    # 4. Reconstruct full grid
    full_res = [[0] * w for _ in range(h)]
    for gi in range(len(f_r)):
        for gj in range(len(f_c)):
            for r in range(tile_h):
                for c in range(tile_w):
                    full_res[f_r[gi][0] + r][f_c[gj][0] + c] = tile[r][c]
    return tuple(tuple(row) for row in full_res)
