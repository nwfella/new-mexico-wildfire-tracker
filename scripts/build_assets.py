"""Build New Mexico map assets from TIGER county GeoJSON (run once, cache results).

Reads a raw NM county GeoJSON (TIGER-derived, e.g. the NMWRRI tlgdb_2023_NM_County
FeatureServer) and produces:
  assets/counties.json   — simplified county polygons for the choropleth
  assets/nm_outline.json — New Mexico outer boundary via edge-union of county rings

Usage:  python scripts/build_assets.py [src.geojson] [eps]
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, HERE)
from geo import area_of_ring, dp_simplify, rings_from_geom  # noqa: E402


def county_name(prop):
    """NAMELSAD like 'De Baca County' → 'De Baca'. Diacritics preserved here;
    burn-heat matching normalizes on both sides at bake time."""
    name = (prop.get("NAMELSAD") or "").strip()
    for suffix in (" County", " city", " City", " Borough", " Parish"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name.strip()


def build_counties(src_geojson, out_json, eps):
    g = json.load(open(src_geojson, encoding="utf-8"))
    by_name = {}
    for f in g.get("features", []):
        p = f.get("properties") or {}
        name = county_name(p)
        if not name:
            continue
        rings = rings_from_geom(f.get("geometry"))
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
        counties.append({"name": name, "polys": simp})
        print(f"  {name:14s} rings={len(simp):3d} pts={sum(len(r) for r in simp):6d} (raw {sum(len(r) for r in rings)})")

    os.makedirs(os.path.dirname(out_json), exist_ok=True)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"counties": counties}, f, separators=(",", ":"))
    kb = os.path.getsize(out_json) / 1024
    print(f"-> {out_json}  {kb:.0f} KB  ({total_raw} raw pts -> {total_sim} sim pts)")


def edge_union_outline(src_geojson, out_json, eps=0.0035):
    """Derive the state outer boundary from county rings (topologically exact
    shared edges). Segments seen once = boundary; chain via endpoint index."""
    g = json.load(open(src_geojson, encoding="utf-8"))
    seg_count = {}
    raw_rings = []
    for f in g.get("features", []):
        for r in rings_from_geom(f.get("geometry")):
            raw_rings.append(r)

    # 1) emit normalized segments (coords rounded to 6 dp for exact matching)
    for r in raw_rings:
        for i in range(len(r) - 1):
            a = (round(r[i][0], 6), round(r[i][1], 6))
            b = (round(r[i + 1][0], 6), round(r[i + 1][1], 6))
            key = (a, b) if a <= b else (b, a)
            seg_count[key] = seg_count.get(key, 0) + 1

    boundary = [k for k, n in seg_count.items() if n == 1]
    print(f"  segments: {len(seg_count)} total, {len(boundary)} boundary")

    # 2) endpoint index → chain rings
    from collections import defaultdict
    idx = defaultdict(list)
    for a, b in boundary:
        idx[a].append((a, b))
        idx[b].append((a, b))

    used = set()
    rings = []
    for a, b in boundary:
        if (a, b) in used or (b, a) in used:
            continue
        ring = [a, b]
        used.add((a, b))
        cur = b
        while cur != a:
            nxt = None
            for s in idx[cur]:
                if s not in used and (s[1], s[0]) not in used:
                    nxt = s
                    break
            if nxt is None:
                break  # open chain (shouldn't happen for a closed state)
            used.add(nxt)
            nxt_pt = nxt[1] if nxt[0] == cur else nxt[0]
            ring.append(nxt_pt)
            cur = nxt_pt
            if len(ring) > 200000:
                break
        rings.append(ring[:-1])  # drop closing dup

    rings.sort(key=lambda r: -len(r))
    print(f"  chained rings: {len(rings)}, biggest {len(rings[0])} pts")

    # 3) simplify each ring
    simp = []
    for r in rings:
        s = dp_simplify(r, eps)
        if len(s) >= 8:
            simp.append(s)
        else:
            print(f"  dropped ring ({len(s)} pts after simplify)")
    print(f"  simplified rings: {len(simp)}")

    # 4) verify closure + bbox
    for r in simp:
        assert r[0] == r[-1], "ring not closed!"
    xs = [p[0] for r in simp for p in r]
    ys = [p[1] for r in simp for p in r]
    print(f"  outline bbox: lon {min(xs):.4f}..{max(xs):.4f}  lat {min(ys):.4f}..{max(ys):.4f}")
    # New Mexico spans roughly -109.05..-103.0, 31.33..37.0
    assert min(xs) < -108.5 and max(xs) > -103.5, "outline bbox out of range!"
    assert min(ys) < 31.6 and max(ys) > 36.6, "outline bbox out of range!"

    os.makedirs(os.path.dirname(out_json), exist_ok=True)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"rings": simp}, f, separators=(",", ":"))
    kb = os.path.getsize(out_json) / 1024
    print(f"-> {out_json}  {kb:.0f} KB")


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "scratch", "nm_counties.geojson")
    eps = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0032
    build_counties(src, os.path.join(ROOT, "assets", "counties.json"), eps)
    edge_union_outline(src, os.path.join(ROOT, "assets", "nm_outline.json"))
