#!/usr/bin/env python3
"""Build data/tiles.json: the equal-size hexagon layout, one hexagon per electoral district.

Each district elects one MLA, so the hexagon view gives every district the same space.
The layout keeps rough geography:
  - the north, the interior, Vancouver Island and Greater Victoria are placed by hand;
  - the 51 districts of Metro Vancouver and the Fraser Valley are fitted into their block
    by an optimal assignment (scipy's linear_sum_assignment) from their real positions,
    after a square-root radial transform so the dense core around Vancouver spreads out.

Positions come from the district shapes in data/geometry.json. Hexagons are pointy-top,
in offset rows where odd rows sit half a hexagon to the right.

Needs numpy and scipy. Only needs rerunning if the boundaries change.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
from scipy.optimize import linear_sum_assignment

ROOT = Path(__file__).resolve().parent.parent

# (row, x): x counts hexagon widths from the left; odd rows sit half a hexagon to the right
FIXED = {
    "BLS": (0, 4), "PCN": (0, 5),
    "SKE": (1, 3.5), "NEC": (1, 4.5), "PRM": (1, 5.5), "PCS": (1, 6.5),
    "NOH": (2, 3), "CBC": (2, 4), "PRO": (2, 5), "PRV": (2, 6),
    "NOI": (3, 2.5), "POR": (3, 3.5), "WVS": (3, 4.5), "FRN": (3, 5.5), "KAN": (3, 6.5), "CLR": (3, 7.5),
    "CRC": (4, 2), "KAC": (4, 7), "SHU": (4, 8),
    "MPR": (5, 1.5), "LAO": (5, 2.5), "VRL": (5, 9.5), "KOM": (5, 10.5),
    "NAL": (6, 2), "KLC": (6, 10), "KOC": (6, 11),
    "NAI": (7, 2.5), "KEC": (7, 10.5), "KOR": (7, 11.5),
    "CWV": (8, 2), "SAN": (8, 3), "KLM": (8, 10), "WKP": (8, 11),
    "JFM": (9, 1.5), "LFH": (9, 2.5), "SAS": (9, 3.5), "PES": (9, 10.5), "BDS": (9, 11.5),
    "ESC": (10, 2), "VTS": (10, 3), "OBG": (10, 4),
    "VTB": (11, 3.5),
}
# Metro Vancouver and the Fraser Valley: the x positions available in each row
LM_ROWS = {
    4: [3, 4, 5, 6],
    5: [3.5, 4.5, 5.5, 6.5, 7.5, 8.5],
    6: [3, 4, 5, 6, 7, 8, 9],
    7: [3.5, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5],
    8: [4, 5, 6, 7, 8, 9],
    9: [4.5, 5.5, 6.5, 7.5, 8.5, 9.5],
    10: [5, 6, 7, 8, 9, 10],
    11: [4.5, 5.5, 6.5, 7.5, 8.5, 9.5],
    12: [5, 6, 7],
}
VAN_CENTRE = (948.0, 1262.0)  # km in the province view, near Burnaby


def rings(d: str) -> list[np.ndarray]:
    out = []
    for part in d.split("M")[1:]:
        head, rest = part.replace("z", "").split("l", 1)
        x, y = map(float, head.split())
        pts = [(x, y)]
        nums = list(map(float, re.findall(r"-?\d+", rest)))
        for i in range(0, len(nums) - 1, 2):
            x += nums[i]
            y += nums[i + 1]
            pts.append((x, y))
        out.append(np.array(pts))
    return out


def centroid(ring: np.ndarray) -> tuple[np.ndarray, float]:
    x, y = ring[:, 0], ring[:, 1]
    x1, y1 = np.roll(x, -1), np.roll(y, -1)
    c = x * y1 - x1 * y
    a = c.sum() / 2
    if abs(a) < 1e-9:
        return ring.mean(0), 0.0
    return np.array([((x + x1) * c).sum() / (6 * a), ((y + y1) * c).sum() / (6 * a)]), abs(a)


def district_centres(geo: dict) -> dict[str, np.ndarray]:
    """Centre of each district's largest piece, in km (x east, y south). Districts that sit
    wholly inside a close-up use the close-up's finer shape."""
    views, E = geo["views"], geo["views"]["pv"]["box"]
    out = {}
    for code, f in geo["f"].items():
        rs = rings(f["p"])
        best = max((centroid(r) for r in rs), key=lambda t: t[1])[0]
        pts = np.vstack(rs)
        mn, mx = pts.min(0), pts.max(0)
        for k in ("van", "vic"):
            if k not in f:
                continue
            b = views[k]["box"]
            fx0, fy0 = (b[0] - E[0]) / 2000, (E[3] - b[3]) / 2000
            fx1, fy1 = (b[2] - E[0]) / 2000, (E[3] - b[1]) / 2000
            if mn[0] >= fx0 - 0.5 and mx[0] <= fx1 + 0.5 and mn[1] >= fy0 - 0.5 and mx[1] <= fy1 + 0.5:
                ic = max((centroid(r) for r in rings(f[k])), key=lambda t: t[1])[0]
                best = np.array([(b[0] - E[0] + 100 * ic[0]) / 2000, (E[3] - b[3] + 100 * ic[1]) / 2000])
        out[code] = best * 2.0  # grid units are 2 km
    return out


def build(geo: dict) -> dict:
    centres = district_centres(geo)
    lm_cells = [(r, x) for r, xs in LM_ROWS.items() for x in xs]
    lm_codes = sorted(c for c in centres if c not in FIXED)
    assert len(lm_cells) >= len(lm_codes), "not enough cells in the Metro Vancouver block"
    P = np.array([centres[c] for c in lm_codes])
    d = P - np.array(VAN_CENTRE)
    r = np.hypot(d[:, 0], d[:, 1]) + 1e-9
    T = d / r[:, None] * np.sqrt(r)[:, None]
    Q = np.array([(x, row * 0.866) for row, x in lm_cells])
    Tn = (T - T.mean(0)) / T.std(0) * Q.std(0) + Q.mean(0)
    rows, cols = linear_sum_assignment(((Tn[:, None, :] - Q[None, :, :]) ** 2).sum(-1))
    layout = dict(FIXED)
    for i, j in zip(rows, cols):
        layout[lm_codes[i]] = lm_cells[j]
    assert len(layout) == len(geo["names"]) and len(set(layout.values())) == len(layout)
    cells = {c: [int(round(x - 0.5 * (row % 2))), row] for c, (row, x) in sorted(layout.items())}
    return {
        "cells": cells,
        "groups": {
            "van": {"label": "Metro Vancouver and the Fraser Valley", "codes": lm_codes},
            "vic": {"label": "Greater Victoria", "codes": sorted(c for c, f in geo["f"].items() if "vic" in f)},
        },
    }


if __name__ == "__main__":
    geo = json.loads((ROOT / "data" / "geometry.json").read_text(encoding="utf-8"))
    out = build(geo)
    path = ROOT / "data" / "tiles.json"
    path.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {path}: {len(out['cells'])} hexagons")
