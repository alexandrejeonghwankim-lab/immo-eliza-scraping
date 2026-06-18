#!/usr/bin/env python3
"""map.py — enrich the dev scraper's CSV and map it.

Takes the CSV produced by ``dev/Scraping_HTML.py`` (default
``sale_properties_final.csv``) and, in one pass, does what
``src/postprocessing.py`` and ``src/htmlmap.py`` do together:

  1. ENRICH (extends the CSV in place):
       * fills missing ``latitude`` / ``longitude`` by geocoding each listing's
         postcode + locality through OpenStreetMap Nominatim (the dev scraper
         does not capture a street, so the town centroid is used);
       * appends ``nearest_city`` and ``nearest_city_distance_km`` — the closest
         city of > 50 000 inhabitants and the great-circle distance to it.
     This step is resumable (the CSV is the state; only rows still missing the
     columns are processed) and reports progress on a single line.

  2. MAP:
       * writes an interactive Leaflet map (default ``data/property_map2.html``)
         with one dot per property — red for sale, yellow for rent.

Both steps run by default; use ``--skip-enrich`` / ``--skip-map`` for one only.

Requirements:  requests   (pip install requests)

Usage:
    python dev/map.py                                  # enrich + map the default CSV
    python dev/map.py --csv path/to/sale.csv           # a different input CSV
    python dev/map.py --output data/property_map2.html # map output path
    python dev/map.py --skip-map --limit 50            # enrich only (testing)
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #
# Default input = the CSV that dev/Scraping_HTML.py writes (its OUTPUT_CSV).
DEFAULT_CSV = "sale_properties_final.csv"
DEFAULT_OUTPUT = "data/property_map2.html"

# Geocoding (OpenStreetMap Nominatim — free, keyless, politeness-bound).
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "immo-eliza-devmap/1.0 (stephane@stepvda.com)"
GEOCODE_MIN_INTERVAL = 1.1   # seconds between geocoding requests (<= 1 req/s)
REQUEST_TIMEOUT = 30         # seconds
MAX_RETRIES = 3
CHECKPOINT_EVERY = 20        # flush the CSV to disk after this many geocodes

# Column names.
LAT_COL, LON_COL = "latitude", "longitude"
CITY_COL, DIST_COL = "nearest_city", "nearest_city_distance_km"
NEW_COLUMNS = [CITY_COL, DIST_COL]

# Map dot colours: (fill, stroke).
SALE_STYLE = {"fill": "#e03131", "stroke": "#8b1a1a"}   # red
RENT_STYLE = {"fill": "#f7d000", "stroke": "#9a7d00"}   # yellow

# Belgium bounding box (initial view when there are no points).
BELGIUM_BOUNDS = [[49.45, 2.50], [51.55, 6.45]]
# Plausible box for a Belgian property; outliers whose lat/lon are swapped are
# un-swapped, anything still outside is dropped, so the auto-fit stays on Belgium.
PLOT_BOUNDS = (49.3, 51.7, 2.3, 6.6)

# Cities of > 50 000 inhabitants with approximate centroid coordinates, used to
# find the closest big city to each property. Belgian municipalities above the
# threshold (the Brussels-Capital sub-municipalities collapsed into one
# "Brussels"), plus the large cities just across the border for frontier
# properties. Editing this list changes the distances.
BIG_CITIES: list[tuple[str, float, float]] = [
    ("Antwerp",         51.2194, 4.4025),
    ("Ghent",           51.0543, 3.7174),
    ("Charleroi",       50.4108, 4.4446),
    ("Liège",           50.6451, 5.5734),
    ("Brussels",        50.8467, 4.3499),
    ("Bruges",          51.2093, 3.2247),
    ("Namur",           50.4674, 4.8719),
    ("Leuven",          50.8798, 4.7005),
    ("Mons",            50.4542, 3.9563),
    ("Mechelen",        51.0281, 4.4801),
    ("Aalst",           50.9403, 4.0364),
    ("La Louvière",     50.4854, 4.1875),
    ("Kortrijk",        50.8281, 3.2649),
    ("Hasselt",         50.9307, 5.3378),
    ("Sint-Niklaas",    51.1652, 4.1437),
    ("Ostend",          51.2247, 2.9156),
    ("Tournai",         50.6071, 3.3892),
    ("Genk",            50.9650, 5.5006),
    ("Seraing",         50.5836, 5.5006),
    ("Roeselare",       50.9469, 3.1228),
    ("Mouscron",        50.7440, 3.2069),
    ("Verviers",        50.5911, 5.8625),
    # Large cities across the border (for frontier properties).
    ("Lille (FR)",      50.6292, 3.0573),
    ("Roubaix (FR)",    50.6942, 3.1746),
    ("Tourcoing (FR)",  50.7236, 3.1610),
    ("Dunkirk (FR)",    51.0344, 2.3768),
    ("Maastricht (NL)", 50.8514, 5.6910),
    ("Eindhoven (NL)",  51.4416, 5.4697),
    ("Aachen (DE)",     50.7753, 6.0839),
    ("Luxembourg (LU)", 49.6116, 6.1319),
]


# --------------------------------------------------------------------------- #
# Geometry                                                                     #
# --------------------------------------------------------------------------- #
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS84 points, in kilometres."""
    r = 6371.0088  # mean Earth radius (km)
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest_big_city(lat: float, lon: float) -> tuple[str, float]:
    """Return (city_name, distance_km) for the closest entry in BIG_CITIES."""
    best_name, best_dist = "", math.inf
    for name, clat, clon in BIG_CITIES:
        d = haversine_km(lat, lon, clat, clon)
        if d < best_dist:
            best_name, best_dist = name, d
    return best_name, round(best_dist, 2)


# --------------------------------------------------------------------------- #
# Geocoding                                                                    #
# --------------------------------------------------------------------------- #
class Geocoder:
    """Thin, rate-limited, cached client over the Nominatim search API."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self._last_request = 0.0
        self._cache: dict[tuple, tuple[float, float] | None] = {}
        self.api_calls = 0

    def _throttle(self) -> None:
        wait = GEOCODE_MIN_INTERVAL - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _query(self, params: dict) -> tuple[float, float] | None:
        params = {**params, "format": "jsonv2", "countrycodes": "be", "limit": 1}
        url = f"{NOMINATIM_URL}?{urlencode(params)}"
        for attempt in range(1, MAX_RETRIES + 1):
            self._throttle()
            try:
                self.api_calls += 1
                resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
                if resp.status_code == 200:
                    data = resp.json()
                    if data:
                        return float(data[0]["lat"]), float(data[0]["lon"])
                    return None  # valid empty result — no point retrying
                if resp.status_code == 429:  # rate-limited: back off and retry
                    time.sleep(2.0 * attempt)
                    continue
            except (requests.RequestException, ValueError, KeyError):
                time.sleep(1.5 * attempt)
        return None

    def geocode(self, row: dict) -> tuple[float, float] | None:
        """Geocode a property row from its (street if any) + postcode + locality."""
        street = (row.get("street") or "").strip()
        number = (row.get("house_number") or "").strip()
        postal = (row.get("postal_code") or "").strip()
        city = (row.get("locality") or "").strip()
        if not (street or city or postal):
            return None  # nothing to geocode

        # Coarsen on failure: exact street address (when present), then the
        # postcode / locality centroid. Results are cached per distinct query.
        attempts: list[dict] = []
        if street:
            attempts.append({"street": f"{number} {street}".strip(),
                             "postalcode": postal, "city": city})
        if city or postal:
            attempts.append({"postalcode": postal, "city": city})

        for params in attempts:
            params = {k: v for k, v in params.items() if v}
            key = tuple(sorted(params.items()))
            if key not in self._cache:
                self._cache[key] = self._query(params)
            if self._cache[key] is not None:
                return self._cache[key]
        return None


# --------------------------------------------------------------------------- #
# CSV I/O                                                                      #
# --------------------------------------------------------------------------- #
def read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        fields = list(reader.fieldnames or [])
    return fields, rows


def write_csv_atomic(path: Path, fields: list[str], rows: list[dict]) -> None:
    """Write rows to a temp file then atomically replace the target."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def has_coords(row: dict) -> bool:
    return bool((row.get(LAT_COL) or "").strip() and (row.get(LON_COL) or "").strip())


def has_city(row: dict) -> bool:
    return bool((row.get(CITY_COL) or "").strip())


# --------------------------------------------------------------------------- #
# Step 1 — enrich the CSV (geocode + nearest city)                            #
# --------------------------------------------------------------------------- #
def progress(label: str, done: int, total: int, geocoded: int,
             cities: int, status: str = "") -> None:
    """Render running progress on a single, overwritten terminal line."""
    pct = (done / total * 100) if total else 100.0
    line = (f"\r[{label}] {done}/{total} ({pct:5.1f}%)  "
            f"geocoded={geocoded}  cities_tagged={cities}  {status}")
    sys.stderr.write(line.ljust(100)[:100])
    sys.stderr.flush()


def enrich_csv(path: Path, geocoder: Geocoder, *, limit: int = 0,
               force: bool = False) -> dict:
    label = path.name
    fields, rows = read_csv(path)

    # Ensure the new columns exist in the header (appended, order preserved).
    for col in NEW_COLUMNS:
        if col not in fields:
            fields.append(col)
    if force:
        for row in rows:
            for col in NEW_COLUMNS:
                row[col] = ""

    total = len(rows)
    geocoded = cities = skipped = 0
    pending_geocodes = 0

    def checkpoint() -> None:
        nonlocal pending_geocodes
        write_csv_atomic(path, fields, rows)
        pending_geocodes = 0

    try:
        for i, row in enumerate(rows, 1):
            need_geo = not has_coords(row)
            need_city = not has_city(row)

            if not need_geo and not need_city:
                skipped += 1
            else:
                # 1) Fill missing coordinates via the geocoding API.
                if need_geo and not (limit and geocoder.api_calls >= limit):
                    progress(label, i, total, geocoded, cities, status="geocoding…")
                    result = geocoder.geocode(row)
                    if result:
                        row[LAT_COL] = f"{result[0]:.7f}"
                        row[LON_COL] = f"{result[1]:.7f}"
                        geocoded += 1
                        pending_geocodes += 1

                # 2) Tag the nearest big city from whatever coordinates we have.
                if not has_city(row) and has_coords(row):
                    try:
                        lat = float(row[LAT_COL])
                        lon = float(row[LON_COL])
                    except (TypeError, ValueError):
                        lat = lon = None
                    if lat is not None:
                        name, dist = nearest_big_city(lat, lon)
                        row[CITY_COL] = name
                        row[DIST_COL] = f"{dist}"
                        cities += 1

                if pending_geocodes >= CHECKPOINT_EVERY:
                    checkpoint()

            if i % 50 == 0 or i == total:
                progress(label, i, total, geocoded, cities)
    finally:
        checkpoint()
        sys.stderr.write("\n")
        sys.stderr.flush()

    return {"total": total, "geocoded": geocoded, "cities": cities,
            "skipped": skipped, "api_calls": geocoder.api_calls}


# --------------------------------------------------------------------------- #
# Step 2 — render the interactive map                                         #
# --------------------------------------------------------------------------- #
def classify(row: dict, filename: str) -> str:
    """Return 'rent' or 'sale' for a property row."""
    t = (row.get("transaction_type") or "").lower()
    if "rent" in t:
        return "rent"
    if "sale" in t:
        return "sale"
    return "rent" if "rent" in filename.lower() else "sale"


def parse_price(value: str | None) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _within_belgium(lat: float, lon: float) -> bool:
    la0, la1, lo0, lo1 = PLOT_BOUNDS
    return la0 <= lat <= la1 and lo0 <= lon <= lo1


def sanitize_coords(lat: float, lon: float) -> tuple[float, float, str]:
    """Return (lat, lon, status) — 'ok', 'swapped' or 'drop'."""
    if _within_belgium(lat, lon):
        return lat, lon, "ok"
    if _within_belgium(lon, lat):
        return lon, lat, "swapped"
    return lat, lon, "drop"


def load_points(path: Path, *, with_popups: bool):
    """Return (sale_points, rent_points, stats) from the enriched CSV.

    Point layout: [lat, lon] or, with popups, [lat, lon, price, locality, type, url].
    """
    sale: list[list] = []
    rent: list[list] = []
    stats = {"no_coords": 0, "swapped": 0, "dropped": 0}

    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            lat_s = (row.get("latitude") or "").strip()
            lon_s = (row.get("longitude") or "").strip()
            try:
                lat = round(float(lat_s), 5)
                lon = round(float(lon_s), 5)
            except ValueError:
                stats["no_coords"] += 1
                continue

            lat, lon, status = sanitize_coords(lat, lon)
            if status == "drop":
                stats["dropped"] += 1
                continue
            if status == "swapped":
                stats["swapped"] += 1

            point: list = [lat, lon]
            if with_popups:
                point += [
                    parse_price(row.get("price")),
                    html.escape((row.get("locality") or "").strip()),
                    html.escape((row.get("property_type") or "").strip()),
                    (row.get("url") or "").strip(),
                ]
            (rent if classify(row, path.name) == "rent" else sale).append(point)

    return sale, rent, stats


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>__TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" crossorigin="" />
<style>
  html, body { margin: 0; height: 100%; }
  #map { width: 100%; height: 100%; background: #eef1f4; }
  .legend {
    background: rgba(255,255,255,0.92); padding: 8px 10px; border-radius: 6px;
    font: 13px/1.4 system-ui, sans-serif; box-shadow: 0 1px 4px rgba(0,0,0,0.3);
  }
  .legend b { display: block; margin-bottom: 4px; }
  .legend .dot {
    display: inline-block; width: 11px; height: 11px; border-radius: 50%;
    margin-right: 6px; vertical-align: -1px; border: 1px solid rgba(0,0,0,0.35);
  }
  .leaflet-popup-content { font: 13px/1.45 system-ui, sans-serif; }
</style>
</head>
<body>
<div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" crossorigin=""></script>
<script>
  var SALE = __SALE_JSON__;
  var RENT = __RENT_JSON__;
  var RADIUS = __RADIUS__;
  var WITH_POPUPS = __WITH_POPUPS__;
  var SALE_STYLE = __SALE_STYLE__;
  var RENT_STYLE = __RENT_STYLE__;

  var map = L.map('map', { preferCanvas: true });
  L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
    maxZoom: 19, subdomains: 'abcd',
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> ' +
                 'contributors &copy; <a href="https://carto.com/attributions">CARTO</a>'
  }).addTo(map);

  function fmtPrice(p, kind) {
    if (p === null || p === undefined || p === '') return 'Price on request';
    var s = '€' + p.toLocaleString('en-US');
    return kind === 'For rent' ? s + '/mo' : s;
  }

  function makeLayer(data, style, kind) {
    var markers = [];
    for (var i = 0; i < data.length; i++) {
      var d = data[i];
      var m = L.circleMarker([d[0], d[1]], {
        radius: RADIUS, weight: 0.6, color: style.stroke,
        fillColor: style.fill, fillOpacity: 0.8
      });
      if (WITH_POPUPS) {
        var parts = ['<b>' + kind + '</b>', fmtPrice(d[2], kind)];
        var loc = (d[3] || '') + (d[4] ? ' · ' + d[4] : '');
        if (loc) parts.push(loc);
        if (d[5]) parts.push('<a href="' + d[5] + '" target="_blank" rel="noopener">View listing ↗</a>');
        m.bindPopup(parts.join('<br>'));
      }
      markers.push(m);
    }
    return L.layerGroup(markers);
  }

  // Sale first, rent on top so the rarer yellow dots stay visible.
  var saleLayer = makeLayer(SALE, SALE_STYLE, 'For sale').addTo(map);
  var rentLayer = makeLayer(RENT, RENT_STYLE, 'For rent').addTo(map);

  var overlays = {};
  overlays['<span style="color:#c0392b">&#9679;</span> For sale (' + SALE.length + ')'] = saleLayer;
  overlays['<span style="color:#caa300">&#9679;</span> For rent (' + RENT.length + ')'] = rentLayer;
  L.control.layers(null, overlays, { collapsed: false }).addTo(map);

  var legend = L.control({ position: 'bottomright' });
  legend.onAdd = function () {
    var div = L.DomUtil.create('div', 'legend');
    div.innerHTML =
      '<b>Properties (' + (SALE.length + RENT.length).toLocaleString('en-US') + ')</b>' +
      '<span class="dot" style="background:' + SALE_STYLE.fill + '"></span>For sale (' +
        SALE.length.toLocaleString('en-US') + ')<br>' +
      '<span class="dot" style="background:' + RENT_STYLE.fill + '"></span>For rent (' +
        RENT.length.toLocaleString('en-US') + ')';
    return div;
  };
  legend.addTo(map);

  // Fit to the data, or fall back to Belgium's bounding box.
  var all = SALE.concat(RENT);
  if (all.length) {
    map.fitBounds(all.map(function (d) { return [d[0], d[1]]; }), { padding: [20, 20] });
  } else {
    map.fitBounds(__BELGIUM_BOUNDS__);
  }
</script>
</body>
</html>
"""


def build_html(sale: list, rent: list, *, radius: float, with_popups: bool,
               title: str) -> str:
    compact = (",", ":")
    replacements = {
        "__TITLE__": html.escape(title),
        "__SALE_JSON__": json.dumps(sale, separators=compact),
        "__RENT_JSON__": json.dumps(rent, separators=compact),
        "__RADIUS__": json.dumps(radius),
        "__WITH_POPUPS__": "true" if with_popups else "false",
        "__SALE_STYLE__": json.dumps(SALE_STYLE, separators=compact),
        "__RENT_STYLE__": json.dumps(RENT_STYLE, separators=compact),
        "__BELGIUM_BOUNDS__": json.dumps(BELGIUM_BOUNDS, separators=compact),
    }
    out = HTML_TEMPLATE
    for key, value in replacements.items():
        out = out.replace(key, value)
    return out


def render_map(csv_path: Path, out_path: Path, *, radius: float,
               with_popups: bool, title: str) -> dict:
    sale, rent, stats = load_points(csv_path, with_popups=with_popups)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        build_html(sale, rent, radius=radius, with_popups=with_popups, title=title),
        encoding="utf-8")
    stats["sale"] = len(sale)
    stats["rent"] = len(rent)
    stats["size_mb"] = out_path.stat().st_size / (1024 * 1024)
    return stats


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Enrich the dev scraper's CSV (geocode + nearest big city) "
                    "and render an interactive map (red = sale, yellow = rent).")
    ap.add_argument("--csv", default=DEFAULT_CSV,
                    help=f"input CSV from dev/Scraping_HTML.py (default: {DEFAULT_CSV})")
    ap.add_argument("--output", default=DEFAULT_OUTPUT,
                    help=f"output HTML map path (default: {DEFAULT_OUTPUT})")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap geocoding API calls (testing)")
    ap.add_argument("--force", action="store_true",
                    help="recompute the nearest-city columns for every row")
    ap.add_argument("--radius", type=float, default=3.0,
                    help="map dot radius in pixels (default: 3)")
    ap.add_argument("--no-popups", action="store_true",
                    help="omit per-dot popups (smaller, faster map file)")
    ap.add_argument("--title", default="Belgium property map (dev)",
                    help="HTML page title")
    ap.add_argument("--skip-enrich", action="store_true",
                    help="do not modify the CSV; only render the map")
    ap.add_argument("--skip-map", action="store_true",
                    help="only enrich the CSV; do not render the map")
    args = ap.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"error: input CSV {csv_path} not found — run dev/Scraping_HTML.py "
              f"first (it writes {DEFAULT_CSV}).", file=sys.stderr)
        sys.exit(1)

    # Step 1 — enrich the CSV in place.
    if not args.skip_enrich:
        print(f"Enriching {csv_path} …", file=sys.stderr)
        stats = enrich_csv(csv_path, Geocoder(), limit=args.limit, force=args.force)
        print(f"  {csv_path.name}: {stats['total']} rows | "
              f"{stats['geocoded']} newly geocoded | "
              f"{stats['cities']} cities tagged | "
              f"{stats['skipped']} already complete | "
              f"{stats['api_calls']} API calls", file=sys.stderr)

    # Step 2 — render the map.
    if not args.skip_map:
        out_path = Path(args.output)
        stats = render_map(csv_path, out_path, radius=args.radius,
                           with_popups=not args.no_popups, title=args.title)
        total = stats["sale"] + stats["rent"]
        if total == 0:
            print("No geocoded properties to map yet.", file=sys.stderr)
        notes = []
        if stats["swapped"]:
            notes.append(f"recovered {stats['swapped']} swapped lat/lon")
        if stats["dropped"]:
            notes.append(f"dropped {stats['dropped']} out-of-range")
        if stats["no_coords"]:
            notes.append(f"skipped {stats['no_coords']} without coordinates")
        print(f"Wrote {total} dots ({stats['sale']} sale / red, "
              f"{stats['rent']} rent / yellow) to {out_path}  "
              f"[{stats['size_mb']:.1f} MB]"
              + ("; " + ", ".join(notes) if notes else ""), file=sys.stderr)


if __name__ == "__main__":
    main()
