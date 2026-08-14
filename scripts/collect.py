#!/usr/bin/env python3
"""New Mexico Wildfire Tracker — data collector & static baker.

Fetches live wildfire data (keyless public APIs), normalizes it, and bakes a
fully static snapshot into index.html (zero runtime network calls — the IT-safe
pattern: the page must render even where fetch/XHR is blocked).

Sources:
  - Incidents:  Esri Live Feeds USA_Wildfires_v1 (NIFC/WFIGS mirror)
  - Perimeters: Esri Wildfire_aggregated_v1 layer 1 (daily fire perimeters)
  - Air quality: Esri OpenAQ mirror (PM2.5 latest readings)
  - Alerts:     NWS api.weather.gov (area=NM), fire-relevant events only
  - Fire news:  New Mexico Fire Information blog RSS (nmfireinfo.com)
  - Counties:   cached simplified GeoJSON (assets/counties.json, see build_assets.py)

Usage:  python scripts/collect.py
"""
import html as html_mod
import json
import os
import re
import sys
import time
import datetime
import unicodedata
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from geo import dp_simplify, rings_from_geom  # noqa: E402

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
NM_BBOX = "-109.2,31.2,-102.9,37.1"
NM_BBOX_ENC = "-109.2%2C31.2%2C-102.9%2C37.1"
FIRES_URL = ("https://services9.arcgis.com/RHVPKKiFTONKtxq3/arcgis/rest/services/"
             "USA_Wildfires_v1/FeatureServer/0/query?where=1%3D1&outFields=*&f=geojson"
             "&geometry=" + NM_BBOX_ENC + "&geometryType=esriGeometryEnvelope&inSR=4326&outSR=4326"
             "&resultRecordCount=600")
PERIM_URL = ("https://services9.arcgis.com/RHVPKKiFTONKtxq3/arcgis/rest/services/"
             "Wildfire_aggregated_v1/FeatureServer/1/query?where=1%3D1&outFields=*&f=geojson"
             "&geometry=" + NM_BBOX_ENC + "&geometryType=esriGeometryEnvelope&inSR=4326&outSR=4326"
             "&resultRecordCount=600")
AQI_URL = ("https://services9.arcgis.com/RHVPKKiFTONKtxq3/arcgis/rest/services/"
           "Air_Quality_PM25_Latest_Results/FeatureServer/0/query?where=1%3D1&outFields=*&f=geojson"
           "&geometry=" + NM_BBOX_ENC + "&geometryType=esriGeometryEnvelope&inSR=4326&outSR=4326"
           "&resultRecordCount=400")
NWS_URL = "https://api.weather.gov/alerts/active?area=NM"
SMOKE_URL = "https://nmfireinfo.com/feed/"

ALERT_EVENTS = {
    "Red Flag Warning": 1, "Fire Weather Watch": 2, "Evacuation": 3, "Evacuation Order": 3,
    "Evacuation Warning": 3, "Air Quality Alert": 4, "Excessive Heat Warning": 5,
    "Heat Advisory": 6, "High Wind Warning": 7, "Wind Advisory": 8, "Fire Warning": 1,
    "Flash Flood Warning": 7, "Flood Warning": 8, "Flood Watch": 9, "Severe Thunderstorm Warning": 9,
}
ALERT_CATEGORIES = {
    "Fire Warning": "Fire Warning", "Red Flag Warning": "Red Flag Warning", "Fire Weather Watch": "Fire Weather Watch",
    "Evacuation Order": "Evacuation", "Evacuation Warning": "Evacuation", "Evacuation": "Evacuation",
    "Air Quality Alert": "Air Quality Alert",
}
KEEP_UNKNOWN = True  # keep unexpected events but rank them lowest


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_json(url, timeout=30):
    return json.loads(fetch(url, timeout).decode("utf-8", "replace"))


def epoch_to_iso(ms):
    if not ms:
        return None
    try:
        return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def norm_name(s):
    """Robust county-name key: strip diacritics ('Doña'→'Dona'), lowercase,
    drop non-alphanumerics ('De Baca'→'debaca')."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def pm25_to_aqi(pm):
    """EPA PM2.5 breakpoints → AQI + category + color."""
    bp = [(0.0, 12.0, 0, 50), (12.1, 35.4, 51, 100), (35.5, 55.4, 101, 150),
          (55.5, 150.4, 151, 200), (150.5, 250.4, 201, 300), (250.5, 500.4, 301, 500)]
    for lo, hi, alo, ahi in bp:
        if lo <= pm <= hi:
            aqi = int(round((ahi - alo) / (hi - lo) * (pm - lo) + alo))
            break
    else:
        aqi = min(999, int(pm * 2))
    if aqi <= 50:
        return aqi, "Good", "#00e400"
    if aqi <= 100:
        return aqi, "Moderate", "#ffff00"
    if aqi <= 150:
        return aqi, "USG", "#ff7e00"
    if aqi <= 200:
        return aqi, "Unhealthy", "#ff0000"
    if aqi <= 300:
        return aqi, "Very Unhealthy", "#8f3f97"
    return aqi, "Hazardous", "#7e0023"


# ---------------------------------------------------------------- incidents
def get_incidents():
    g = fetch_json(FIRES_URL)
    feats = g.get("features", [])
    out = []
    seen = set()
    for f in feats:
        a = f.get("properties") or f.get("attributes") or {}
        if a.get("POOState") != "US-NM":
            continue
        uid = a.get("UniqueFireIdentifier") or a.get("IrwinID") or str(a.get("OBJECTID"))
        if uid in seen:
            continue
        seen.add(uid)
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates")
        if not coords or len(coords) < 2:
            continue
        lon, lat = coords[0], coords[1]
        acres = a.get("CalculatedAcres") or a.get("DailyAcres") or 0
        struc = (a.get("ResidencesDestroyed") or 0) + (a.get("OtherStructuresDestroyed") or 0)
        out.append({
            "id": uid,
            "n": a.get("IncidentName") or "Unnamed fire",
            "cnty": (a.get("POOCounty") or "").strip() or None,
            "acres": round(acres, 1),
            "cont": a.get("PercentContained"),
            "cause": a.get("FireCause") or None,
            "kind": a.get("IncidentTypeKind") or a.get("IncidentTypeCategory") or None,
            "per": a.get("TotalIncidentPersonnel"),
            "struc": struc or None,
            "inj": a.get("Injuries") or None,
            "fat": a.get("Fatalities") or None,
            "comp": a.get("FireMgmtComplexity") or None,
            "mgmt": a.get("IncidentManagementOrganization") or None,
            "fuel": a.get("PredominantFuelGroup") or None,
            "disp": epoch_to_iso(a.get("FireDiscoveryDateTime")),
            "rep": epoch_to_iso(a.get("ICS209ReportDateTime")),
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "irwin": a.get("IrwinID"),
        })
    out.sort(key=lambda i: -(i["acres"] or 0))
    return out


# ---------------------------------------------------------------- perimeters
def get_perimeters(incidents):
    irwin_ids = set(i.get("irwin") for i in incidents if i.get("irwin"))
    g = fetch_json(PERIM_URL)
    now = datetime.datetime.now(datetime.timezone.utc)
    out = []
    used_names = set()
    for f in g.get("features", []):
        a = f.get("properties") or {}
        dcur = a.get("DateCurrent")
        keep = False
        if a.get("IRWINID") in irwin_ids:
            keep = True
        elif dcur:
            try:
                dt = datetime.datetime.fromtimestamp(dcur / 1000, tz=datetime.timezone.utc)
                if (now - dt).days <= 30:
                    keep = True
            except (ValueError, OSError, OverflowError):
                keep = False
        if not keep:
            continue
        rings = rings_from_geom(f.get("geometry"))
        simp = []
        for r in rings:
            s = dp_simplify(r, 0.004)
            if len(s) >= 8:
                # cap per-ring points
                if len(s) > 2500:
                    step = len(s) // 2500
                    s = s[::step] + [s[-1]]
                simp.append(s)
        if not simp:
            continue
        name = (a.get("IncidentName") or "").strip()
        if not name:
            continue
        # dedupe by name: keep the most recent
        date_s = epoch_to_iso(dcur)
        if name in used_names:
            continue
        used_names.add(name)
        out.append({"n": name, "d": date_s, "acres": round(a.get("GISAcres") or 0, 0), "p": simp})
    # cap total points to keep the HTML lean
    total = sum(len(r) for o in out for r in o["p"])
    if total > 40000:
        factor = 40000 / total
        for o in out:
            o["p"] = [r[::max(1, int(1 / factor))] + [r[-1]] for r in o["p"]]
    out.sort(key=lambda o: -o["acres"])
    return out


# ---------------------------------------------------------------- air quality
def get_aqi():
    g = fetch_json(AQI_URL)
    out = []
    for f in g.get("features", []):
        a = f.get("properties") or {}
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates")
        if not coords or len(coords) < 2:
            continue
        lon, lat = coords[0], coords[1]
        if not (-109.1 <= lon <= -102.9 and 31.25 <= lat <= 37.05):
            continue  # strict New Mexico bounds (bbox pulls AZ/TX/CO too)
        v = a.get("value")
        if v is None or a.get("parameter") != "pm25":
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v < 0 or v > 2000:
            continue
        aqi, cat, col = pm25_to_aqi(v)
        out.append({
            "c": (a.get("city") or "Unknown").strip(),
            "l": (a.get("location") or a.get("city") or "").strip(),
            "v": round(v, 1),
            "aqi": aqi,
            "cat": cat,
            "col": col,
            "t": a.get("lastUpdated"),
            "lat": round(lat, 4),
            "lon": round(lon, 4),
        })
    # dedupe by location name, keep newest
    by_name = {}
    for o in out:
        key = o["l"] or o["c"]
        if key not in by_name or (o.get("t") or "") > (by_name[key].get("t") or ""):
            by_name[key] = o
    return sorted(by_name.values(), key=lambda o: -o["v"])


# ---------------------------------------------------------------- alerts
def get_alerts():
    d = fetch_json(NWS_URL)
    out = []
    for f in d.get("features", []):
        p = f.get("properties") or {}
        event = p.get("event") or ""
        rank = ALERT_EVENTS.get(event)
        if rank is None and not KEEP_UNKNOWN:
            continue
        if rank is None:
            rank = 99
        out.append({
            "e": ALERT_CATEGORIES.get(event, event),
            "raw": event,
            "h": p.get("headline") or event,
            "a": p.get("areaDesc") or "",
            "sev": p.get("severity") or "",
            "on": p.get("onset"),
            "ex": p.get("expires"),
            "d": (p.get("description") or "")[:400],
            "url": "https://api.weather.gov/alerts/" + (f.get("id", "").rsplit("/", 1)[-1]),
            "rank": rank,
        })
    out.sort(key=lambda o: o["rank"])
    return out


# ---------------------------------------------------------------- fire news feed
def get_smoke():
    data = fetch(SMOKE_URL)
    root = ET.fromstring(data)
    out = []

    # RSS 2.0 shape: <rss><channel><item><title><link><pubDate><description>
    items = root.findall(".//item")
    if items:
        for item in items[:3]:
            t = html_mod.unescape((item.findtext("title") or "").strip())
            link = (item.findtext("link") or "").strip()
            pub = item.findtext("pubDate") or ""
            desc = re.sub(r"<[^>]+>", " ", item.findtext("description") or "")
            desc = re.sub(r"\s+", " ", html_mod.unescape(desc)).strip()
            out.append({"t": t, "p": pub, "s": desc[:400], "u": link})
        return out

    # Atom shape: <feed><entry><title><link><published><summary>
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for entry in root.findall("a:entry", ns)[:3]:
        title = html_mod.unescape((entry.findtext("a:title", default="", namespaces=ns) or "").strip())
        link = ""
        for l in entry.findall("a:link", ns):
            if l.get("rel") == "alternate" or l.get("rel") is None:
                link = l.get("href", "")
                break
        pub = entry.findtext("a:published", default="", namespaces=ns)
        summary = entry.findtext("a:summary", default="", namespaces=ns) or ""
        summary = re.sub(r"<[^>]+>", " ", summary)
        summary = re.sub(r"\s+", " ", html_mod.unescape(summary)).strip()
        out.append({"t": title, "p": pub, "s": summary[:400], "u": link})
    return out


# ---------------------------------------------------------------- cities
CITIES = [
    ("Albuquerque", 35.084, -106.651, 1), ("Santa Fe", 35.687, -105.938, 1),
    ("Las Cruces", 32.319, -106.763, 1), ("Rio Rancho", 35.232, -106.664, 1),
    ("Roswell", 33.394, -104.523, 1), ("Farmington", 36.728, -108.219, 1),
    ("Clovis", 34.405, -103.205, 1), ("Hobbs", 32.702, -103.136, 1),
    ("Alamogordo", 32.900, -105.960, 1), ("Carlsbad", 32.421, -104.229, 1),
    ("Gallup", 35.528, -108.743, 1), ("Taos", 36.407, -105.573, 1),
    ("Ruidoso", 33.331, -105.673, 1), ("Las Vegas", 35.594, -105.223, 1),
    ("Silver City", 32.770, -108.280, 0), ("Los Alamos", 35.888, -106.307, 0),
    ("Grants", 35.147, -107.851, 0), ("Deming", 32.269, -107.759, 0),
    ("Artesia", 32.842, -104.403, 0), ("Portales", 34.186, -103.334, 0),
    ("Tucumcari", 35.172, -103.725, 0), ("Raton", 36.903, -104.439, 0),
    ("Truth or Consequences", 33.128, -107.253, 0), ("Espanola", 35.991, -106.081, 0),
    ("Aztec", 36.822, -107.993, 0), ("Lovington", 32.944, -103.348, 0),
    ("Socorro", 34.058, -106.891, 0), ("Clayton", 36.452, -103.184, 0),
    ("Lordsburg", 32.350, -108.708, 0), ("Ruidoso Downs", 33.330, -105.589, 0),
]


def build_counties(incidents):
    with open(os.path.join(ROOT, "assets", "counties.json"), encoding="utf-8") as f:
        data = json.load(f)
    burn = {}
    for i in incidents:
        if i.get("cnty"):
            key = norm_name(i["cnty"])
            if key:
                burn[key] = burn.get(key, 0) + (i["acres"] or 0)
    out = []
    for c in data["counties"]:
        out.append({"n": c["name"], "b": round(burn.get(norm_name(c["name"]), 0), 0), "p": c["polys"]})
    return out


def build_static_fires_table(incidents):
    rows = []
    for i in incidents[:12]:
        cont = i["cont"] if i["cont"] is not None else "—"
        rows.append(
            f'<tr><td style="padding:6px;border:1px solid #242a38;">{i["n"]}</td>'
            f'<td style="padding:6px;border:1px solid #242a38;">{i["cnty"] or "—"}</td>'
            f'<td style="padding:6px;border:1px solid #242a38;">{int(i["acres"]):,}</td>'
            f'<td style="padding:6px;border:1px solid #242a38;">{cont}%</td>'
            f'<td style="padding:6px;border:1px solid #242a38;">{i["cause"] or "—"}</td></tr>')
    return "\n".join(rows)


def main():
    t0 = time.time()
    print("== New Mexico Wildfire Tracker bake ==")
    warnings = []

    try:
        incidents = get_incidents()
        print(f"  incidents: {len(incidents)}")
        if not incidents:
            print("FATAL: zero New Mexico incidents — refusing to bake; keeping previous index.html")
            sys.exit(1)
    except Exception as e:
        print(f"FATAL: incidents fetch failed: {e}")
        sys.exit(1)

    try:
        perimeters = get_perimeters(incidents)
        print(f"  perimeters: {len(perimeters)}")
    except Exception as e:
        warnings.append(f"perimeters: {e}")
        perimeters = []

    try:
        aqi = get_aqi()
        print(f"  aqi monitors: {len(aqi)}")
    except Exception as e:
        warnings.append(f"aqi: {e}")
        aqi = []

    try:
        alerts = get_alerts()
        red_flags = sum(1 for a in alerts if "Red Flag" in a["raw"] or "Fire Warning" in a["raw"])
        print(f"  alerts: {len(alerts)} ({red_flags} red flag/fire warnings)")
    except Exception as e:
        warnings.append(f"alerts: {e}")
        alerts = []
        red_flags = 0

    try:
        smoke = get_smoke()
        print(f"  fire-news posts: {len(smoke)}")
    except Exception as e:
        warnings.append(f"fire news: {e}")
        smoke = []

    counties = build_counties(incidents)
    print(f"  counties: {len(counties)}")

    with open(os.path.join(ROOT, "assets", "nm_outline.json"), encoding="utf-8") as f:
        outline = json.load(f)["rings"]
    print(f"  state outline rings: {len(outline)}")

    # ---- stats
    total_acres = sum(i["acres"] or 0 for i in incidents)
    conts = [i["cont"] for i in incidents if i["cont"] is not None]
    avg_cont = round(sum(conts) / len(conts)) if conts else None
    total_per = sum(i["per"] or 0 for i in incidents)
    now = datetime.datetime.now(datetime.timezone.utc)
    new24 = sum(1 for i in incidents if i["disp"] and
                (now - datetime.datetime.fromisoformat(i["disp"])).total_seconds() < 86400)
    worst = aqi[0] if aqi else None
    stats = {
        "fires": len(incidents), "acres": round(total_acres), "avgCont": avg_cont,
        "per": total_per, "new24": new24, "redFlags": red_flags,
        "worstAqi": {"city": worst["c"], "aqi": worst["aqi"], "cat": worst["cat"]} if worst else None,
    }

    data = {
        "updated": now_iso(),
        "stats": stats,
        "incidents": incidents,
        "perimeters": perimeters,
        "aqi": aqi,
        "alerts": [{"e": a["e"], "h": a["h"], "a": a["a"], "sev": a["sev"],
                    "on": a["on"], "ex": a["ex"], "d": a["d"], "url": a["url"]} for a in alerts],
        "smoke": smoke,
        "counties": counties,
        "outline": outline,
        "cities": [{"n": n, "lat": la, "lon": lo, "major": m} for n, la, lo, m in CITIES],
    }

    # ---- bake
    with open(os.path.join(ROOT, "template.html"), "rb") as f:
        html = f.read()

    payload = ("\nconst NM_FIRE_DATA = " +
               json.dumps(data, separators=(",", ":")).replace("<", "\\u003c") + ";\n").encode("utf-8")

    START = b"/*__NM_FIRE_START__*/"
    END = b"/*__NM_FIRE_END__*/"
    s = html.find(START)
    e = html.find(END)
    if s == -1 or e == -1 or e <= s:
        print("FATAL: bake markers not found in template.html")
        sys.exit(1)
    baked = html[:s + len(START)] + payload + html[e:]

    static_rows = build_static_fires_table(incidents).encode("utf-8")
    m = b"<!--__STATIC_FIRES__-->"
    mi = baked.find(m)
    if mi != -1:
        baked = baked[:mi] + static_rows + baked[mi + len(m):]

    out_path = os.path.join(ROOT, "index.html")
    with open(out_path, "wb") as f:
        f.write(baked)

    # ---- data artifacts
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    with open(os.path.join(ROOT, "data", "timestamp.json"), "w") as f:
        json.dump({"updated": data["updated"]}, f, separators=(",", ":"))
    with open(os.path.join(ROOT, "data", "snapshot.json"), "w") as f:
        json.dump(data, f, separators=(",", ":"))

    kb = os.path.getsize(out_path) / 1024
    print(f"  baked index.html: {kb:.0f} KB in {time.time() - t0:.1f}s")
    if warnings:
        print("  WARN:", "; ".join(warnings))
    print(f"  top fires: {', '.join(i['n'] for i in incidents[:5])}")
    if worst:
        print(f"  worst AQI: {worst['c']} — {worst['aqi']} ({worst['cat']})")
    print("OK")


if __name__ == "__main__":
    main()
