"""Geometry utilities: Douglas-Peucker simplification + Oregon county builder.

Counties are built once (census/BLM source, slow) and cached to assets/counties.json.
Perimeter polygons are simplified at every bake inside collect.py using dp_simplify().
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def dp_simplify(ring, eps):
    """Iterative Douglas-Peucker on a closed ring [(lon, lat), ...].
    eps in degrees. Returns simplified ring (not closed)."""
    if len(ring) < 4:
        return list(ring)
    # treat ring as open polyline for simplification (drop closing point)
    pts = ring[:-1] if ring[0] == ring[-1] else list(ring)
    n = len(pts)
    if n < 3:
        return pts
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    eps2 = eps * eps
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ax, ay = pts[a]
        bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        maxd = -1.0
        maxi = -1
        for i in range(a + 1, b):
            x, y = pts[i]
            if length2 > 1e-12:
                t = ((x - ax) * dx + (y - ay) * dy) / length2
                t = max(0.0, min(1.0, t))
                px, py = ax + t * dx, ay + t * dy
            else:
                px, py = ax, ay
            ddx, ddy = x - px, y - py
            d2 = ddx * ddx + ddy * ddy
            if d2 > maxd:
                maxd = d2
                maxi = i
        if maxd > eps2:
            keep[maxi] = True
            stack.append((a, maxi))
            stack.append((maxi, b))
    out = [p for p, k in zip(pts, keep) if k]
    # re-close
    out.append(out[0])
    return out


def simplify_polygon(coords, eps):
    """coords: list of rings (each [(lon,lat),...]). Returns simplified rings, drops tiny ones."""
    out = []
    for ring in coords:
        if len(ring) < 4:
            continue
        s = dp_simplify(ring, eps)
        if len(s) >= 4:
            # drop degenerate rings with tiny bbox
            xs = [p[0] for p in s]
            ys = [p[1] for p in s]
            if (max(xs) - min(xs)) > eps * 0.5 and (max(ys) - min(ys)) > eps * 0.5:
                out.append(s)
    return out


def area_of_ring(ring):
    """Shoelace area in deg^2 (sign-agnostic)."""
    s = 0.0
    n = len(ring)
    for i in range(n - 1):
        s += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return abs(s) / 2.0


def rings_from_geom(geom):
    """Extract list of rings [(lon,lat),...] from a GeoJSON Polygon/MultiPolygon."""
    if not geom:
        return []
    t = geom.get('type')
    coords = geom.get('coordinates') or []
    if t == 'Polygon':
        return [r for r in coords if len(r) >= 4]
    if t == 'MultiPolygon':
        return [r for poly in coords for r in poly if len(r) >= 4]
    return []


def build_counties(src_geojson, out_json, eps=0.0032):
    """Merge BLM OR county polygons by COUNTY_NAME, simplify, write compact JSON."""
    g = json.load(open(src_geojson, encoding='utf-8'))
    by_name = {}
    for f in g['features']:
        p = f['properties']
        if not (p.get('COBCODE') or '').startswith('OR'):
            continue  # layer covers WA too — keep Oregon only
        name = (p.get('COUNTY_NAME') or '').strip().title()
        if not name:
            continue
        rings = rings_from_geom(f.get('geometry'))
        if rings:
            by_name.setdefault(name, []).extend(rings)

    counties = []
    total_raw = total_sim = 0
    for name in sorted(by_name):
        rings = by_name[name]
        total_raw += sum(len(r) for r in rings)
        simp = []
        for ring in rings:
            s = dp_simplify(ring, eps)
            if len(s) >= 4:
                ar = area_of_ring(s)
                if ar > (eps * 8) ** 2:  # drop specks
                    simp.append(s)
        total_sim += sum(len(r) for r in simp)
        counties.append({'name': name, 'polys': simp})
        print(f"  {name:12s} rings={len(simp):3d} pts={sum(len(r) for r in simp):5d} (raw {sum(len(r) for r in rings)})")

    os.makedirs(os.path.dirname(out_json), exist_ok=True)
    with open(out_json, 'w', encoding='utf-8') as f:
        json.dump({'counties': counties}, f, separators=(',', ':'))
    kb = os.path.getsize(out_json) / 1024
    print(f"-> {out_json}  {kb:.0f} KB  ({total_raw} raw pts -> {total_sim} sim pts)")


if __name__ == '__main__':
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'scratch', 'cnty_or.geojson')
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, 'assets', 'counties.json')
    eps = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0032
    build_counties(src, out, eps)
