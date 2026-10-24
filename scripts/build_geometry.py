#!/usr/bin/env python3
"""Rebuild data/geometry.json: BC's 93 electoral districts as compact SVG paths.

Sources:
  - Districts: Elections BC, Provincial Electoral Districts, Electoral Boundaries Redistribution 2023
    (the districts used in the 2024 and 2026 elections), from the BC Geographic Warehouse
    WFS layer WHSE_ADMIN_BOUNDARIES.EBC_ELECTORAL_DISTS_BS11_SVW, in BC Albers (EPSG:3005).
  - Land: Province of British Columbia - Boundary Terrestrial (GeoBC). Legal district boundaries
    run out into the ocean, so each district is clipped to land; otherwise the coast disappears.
    If that download fails, Natural Earth's 1:10m land polygons are used instead.

What it does:
  1. Clips each district to land and measures its land area.
  2. Province view: simplifies to about 1 km and snaps to a 2 km grid.
  3. Close-ups of Metro Vancouver and Greater Victoria: every district that reaches into the
     frame, clipped to it, simplified to about 40 m and snapped to a 100 m grid.
  4. Writes each shape as an SVG path ("M x y l dx dy ... z"), so the page needs no map library.

District boundaries only change after a redistribution, so this runs by hand
(Actions -> Build geometry -> Run workflow).

Needs: shapely, pyproj (pip install shapely pyproj).
"""
from __future__ import annotations

import io
import json
import math
import sqlite3
import struct
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from shapely import make_valid, wkb
from shapely.geometry import MultiPolygon, Polygon, box, shape
from shapely.ops import transform, unary_union

ROOT = Path(__file__).resolve().parent.parent
UA = {"User-Agent": "bc-election-2026-maps/1.0 (+https://github.com/fahadBgias/bc-election-2026-maps)"}
WFS = ("https://openmaps.gov.bc.ca/geo/pub/WHSE_ADMIN_BOUNDARIES.EBC_ELECTORAL_DISTS_BS11_SVW/ows"
       "?service=WFS&version=2.0.0&request=GetFeature"
       "&typeName=pub:WHSE_ADMIN_BOUNDARIES.EBC_ELECTORAL_DISTS_BS11_SVW"
       "&outputFormat=application/json&srsName=EPSG:3005"
       "&propertyName=ED_NAME,ED_ABBREVIATION,ELECTORAL_DISTRICT_ID,SHAPE")
LAND_GPKG = "ftp://ftp.geobc.gov.bc.ca/sections/outgoing/bmgs/BC_Boundary_Terrestrial/BC_Boundary_Terrestrial.gpkg.zip"
LAND_NE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_land.geojson"

PROVINCE = {"grid": 2000, "simplify": 1000, "min_part_km2": 12}
# Close-up frames in BC Albers metres: [x0, y0, x1, y1]
FRAMES = {
    "van": {"box": [1194000, 444000, 1290000, 500000], "grid": 100, "simplify": 40, "min_part_km2": 0.05},
    "vic": {"box": [1174000, 368000, 1206000, 404000], "grid": 100, "simplify": 40, "min_part_km2": 0.05},
}


def get(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=300) as r:
        return r.read()


def js_round(v: float) -> int:
    return math.floor(v + 0.5)


def parts(g) -> list[Polygon]:
    if g.is_empty:
        return []
    if isinstance(g, Polygon):
        return [g]
    if isinstance(g, MultiPolygon):
        return list(g.geoms)
    return [p for geom in getattr(g, "geoms", []) for p in parts(geom)]


def gpkg_geometry(blob: bytes):
    """GeoPackage geometry blob -> shapely geometry (skip the GP header and envelope)."""
    flags = blob[3]
    env = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}[(flags >> 1) & 0b111]
    return wkb.loads(blob[8 + env:])


def land_from_gpkg() -> object:
    data = get(LAND_GPKG)
    with zipfile.ZipFile(io.BytesIO(data)) as z, tempfile.TemporaryDirectory() as tmp:
        name = next(n for n in z.namelist() if n.lower().endswith(".gpkg"))
        path = z.extract(name, tmp)
        con = sqlite3.connect(path)
        rows = con.execute("SELECT table_name, column_name, geometry_type_name, srs_id FROM gpkg_geometry_columns").fetchall()
        best = None
        for table, col, gtype, srs in rows:
            if "POLYGON" not in gtype.upper():
                continue
            geoms = [gpkg_geometry(b) for (b,) in con.execute(f'SELECT "{col}" FROM "{table}"') if b]
            u = unary_union(geoms)
            print(f"  land layer {table}: {gtype}, EPSG:{srs}, {len(geoms)} features, {u.area / 1e6:,.0f} km2")
            if srs == 3005 and (best is None or u.area > best.area):
                best = u
        con.close()
    if best is None:
        raise RuntimeError("no polygon layer in EPSG:3005 found in the land GeoPackage")
    return best


def land_from_natural_earth(bounds) -> object:
    from pyproj import Transformer
    to_albers = Transformer.from_crs("EPSG:4326", "EPSG:3005", always_xy=True).transform
    fc = json.loads(get(LAND_NE))
    lon_box = box(-140, 47, -113, 61)
    geoms = []
    for f in fc["features"]:
        g = shape(f["geometry"])
        if g.intersects(lon_box):
            geoms.append(make_valid(transform(to_albers, make_valid(g).intersection(lon_box))).buffer(0))
    return unary_union(geoms).intersection(box(*bounds).buffer(50000))


def to_path(g, x0: float, y1: float, grid: float, min_part_m2: float) -> str:
    """SVG path in grid units, origin at the frame's top-left corner; drops tiny islands."""
    polys = sorted(parts(g), key=lambda p: -p.area)
    keep = [p for i, p in enumerate(polys) if i == 0 or p.area >= min_part_m2]
    out = []
    for p in keep:
        for ring in [p.exterior, *p.interiors]:
            pts, last = [], None
            for x, y in ring.coords:
                q = (js_round((x - x0) / grid), js_round((y1 - y) / grid))
                if q != last:
                    pts.append(q)
                    last = q
            if len(pts) > 1 and pts[0] == pts[-1]:
                pts.pop()
            if len(set(pts)) < 3:
                continue
            s = f"M{pts[0][0]} {pts[0][1]}l" + " ".join(
                f"{b[0] - a[0]} {b[1] - a[1]}" for a, b in zip(pts, pts[1:]))
            out.append(s + "z")
    return "".join(out)


def build() -> dict:
    print("Downloading districts...")
    fc = json.loads(get(WFS))
    feats = sorted(fc["features"], key=lambda f: f["properties"]["ED_ABBREVIATION"])
    districts = {f["properties"]["ED_ABBREVIATION"]: (f["properties"]["ED_NAME"], shape(f["geometry"]).buffer(0)) for f in feats}
    print(f"  {len(districts)} districts")
    bounds = unary_union([g for _, g in districts.values()]).bounds
    print("Downloading land...")
    try:
        land, land_source = land_from_gpkg(), "GeoBC, Province of British Columbia - Boundary Terrestrial"
    except Exception as e:  # FTP can be unreachable from some networks
        print(f"  GeoBC land boundary unavailable ({e}); using Natural Earth 1:10m land")
        land, land_source = land_from_natural_earth(bounds), "Natural Earth 1:10m land"
    land = land.buffer(0)

    clipped, area = {}, {}
    for code, (name, g) in districts.items():
        c = g.intersection(land)
        if c.is_empty:
            c = g
        clipped[code] = c
        area[code] = round(c.area / 1e6, 1)

    allg = unary_union(list(clipped.values()))
    x0, y0, x1, y1 = allg.bounds
    pv = PROVINCE
    E = [js_round(x0), js_round(y0), js_round(x1), js_round(y1)]
    shapes = {}
    for code, g in clipped.items():
        s = g.simplify(pv["simplify"], preserve_topology=True)
        shapes[code] = {"p": to_path(s, E[0], E[3], pv["grid"], pv["min_part_km2"] * 1e6)}
    for key, fr in FRAMES.items():
        bx = box(*fr["box"])
        for code, g in clipped.items():
            if not g.intersects(bx):
                continue
            c = g.intersection(bx).simplify(fr["simplify"], preserve_topology=True)
            path = to_path(c, fr["box"][0], fr["box"][3], fr["grid"], fr["min_part_km2"] * 1e6)
            if path:
                shapes[code][key] = path
    view = lambda b, grid: [math.ceil((b[2] - b[0]) / grid), math.ceil((b[3] - b[1]) / grid)]
    return {
        "source": {"districts": "Elections BC, Provincial Electoral Districts (2023 redistribution), BC Geographic Warehouse",
                   "land": land_source},
        "views": {
            "pv": {"box": E, "grid": pv["grid"], "size": view(E, pv["grid"])},
            **{k: {"box": fr["box"], "grid": fr["grid"], "size": view(fr["box"], fr["grid"])} for k, fr in FRAMES.items()},
        },
        "names": {code: name for code, (name, _) in districts.items()},
        "area_km2": area,
        "f": shapes,
    }


if __name__ == "__main__":
    out = build()
    path = ROOT / "data" / "geometry.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {path} ({path.stat().st_size / 1024:.0f} KB), land from {out['source']['land']}")
