# New Mexico Wildfire Tracker 🔥

Live wildfire tracker for the state of New Mexico — active fires, daily perimeters, containment,
air quality, red-flag warnings, and fire news. **Fully static**: every data point is baked
into `index.html` server-side, so the page works even where corporate IT blocks `fetch`/XHR.

**Live:** https://nwfella.github.io/new-mexico-wildfire-tracker/

## Features

- **Hero map, top and center** — full-width canvas map of New Mexico on page load; **⛶ maximizes it to full screen** (native Fullscreen API + CSS fallback, Esc/✕ to exit)
- **5 color themes** — Ember (default), Forest, Ocean, Magma, Daybreak; theme picker in the header, saved to localStorage, the canvas map recolorizes to match
- **Legend = layer filters** — tap any legend row (active fires, perimeters, county burn heat, AQI monitors) to hide/show that layer on the map; multi-select; "show all" reset; choices remembered
- **Collapsible legend** — the **−** button minimizes it to a small "Legend" pill (tap to bring it back; auto-collapsed on phones)
- **Mobile-first** — 60vh touch map, pinch-zoom + drag pan, 40px tap targets, swipeable alert strip, responsive grid lists
- **County burn-heat choropleth** — all 33 NM counties shaded by active fire acreage
- **Active fires** — incidents live from the NIFC/WFIGS feed: size, containment, cause,
  personnel, structures lost, complexity, discovery & last-report times
- **Anchored popup details** — tap a fire on the map and a popup bubble appears right next to the marker (arrow pointing at it, flips below near the top edge, follows while panning/zooming); select a fire or AQI monitor from the lists and the same detail card appears inline in the rail
- **Daily perimeters** — orange fire boundaries, pulse-highlighted when a fire is selected
- **Air quality** — PM2.5 readings from New Mexico-area monitors (OpenAQ mirror), EPA AQI +
  category colors, worst-first ranking
- **NWS alerts** — red flag warnings, fire weather watches, evacuations, air quality alerts
  (color-coded swipe strip + list)
- **NM Fire Info news** — latest posts from the interagency New Mexico Fire Information blog
- **No-JS fallback** — static table of the top fires renders with JavaScript disabled
- **Zero runtime network calls** — a cron refreshes the snapshot 3× a day (00:00 / 08:00 / 16:00 MT)

## Data sources (all public, no API keys)

| Data | Source |
|---|---|
| Incidents | Esri Live Feeds `USA_Wildfires_v1` (NIFC/WFIGS mirror) |
| Perimeters | Esri `Wildfire_aggregated_v1` (daily fire perimeters) |
| Air quality | Esri OpenAQ mirror (PM2.5 latest readings) |
| Alerts | NWS `api.weather.gov` (area=NM) |
| Fire news | New Mexico Fire Information blog RSS (`nmfireinfo.com/feed`) |
| Counties | Census TIGER 2023 county boundaries (NMWRRI mirror) |

## How it works

```
scripts/publish.py (Hermes cron, 3× daily)
  └─ scripts/collect.py
      ├─ fetch incidents / perimeters / AQI / alerts / fire news (parallel, keyless)
      ├─ normalize + simplify geometry (Douglas-Peucker: 55.8K raw county pts → 1.1K)
      ├─ compute stats + county burn heat + EPA AQI
      └─ bake inline JSON into index.html via template.html markers
            → git commit + push → GitHub Pages serves the static snapshot
```

- `template.html` — editable source with `/*__NM_FIRE_START__*/`…`/*__NM_FIRE_END__*/` markers
- `index.html` — generated, fully self-contained (~135 KB), committed
- `scripts/build_assets.py` — one-shot NM county + state-outline builder (edge-union of
  county rings, no shapely); outputs `assets/counties.json` + `assets/nm_outline.json`
- `scripts/publish.py` — cron wrapper: bake → commit only if changed → push (silent when nothing new)
- `assets/counties.json` — cached simplified New Mexico counties (33, TIGER 2023)
- `assets/nm_outline.json` — New Mexico state boundary (edge-union derived)

### Local refresh

```bash
python scripts/collect.py    # fetches + bakes index.html
```

## Stats snapshot (Aug 2026 fire season)

18 active fires, ~5.6K acres burning, 557 personnel assigned, 0 red-flag warnings at last bake,
worst AQI 51 (Moderate).

## License

MIT — see [LICENSE](LICENSE).
