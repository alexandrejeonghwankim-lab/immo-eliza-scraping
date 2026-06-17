#!/usr/bin/env python3
"""htmlmap.py — render an interactive Belgium map of the scraped properties.

Reads the enriched property CSVs (by default ``data/forsale.csv`` and
``data/torent.csv``) and writes a single, self-contained HTML file showing one
small dot per property at its ``latitude`` / ``longitude``:

  * **red** dots   — properties for sale,
  * **yellow** dots — properties for rent.

The map is a Leaflet map (tiles + library pulled from public CDNs) using a
canvas renderer, which comfortably draws the ~20 000 points. Each dot carries a
popup with its price, locality, type and a link to the listing. Sale / rent can
be toggled from the layer control, and a legend shows the per-class counts.

Run ``postprocessing.py`` first so the coordinate columns are populated; rows
without coordinates are simply skipped (and reported).

Requirements:  none beyond the Python standard library.

Usage:
    python htmlmap.py                              # -> data/property_map.html
    python htmlmap.py --output map.html            # custom output path
    python htmlmap.py --files data/forsale.csv     # specific file(s)
    python htmlmap.py --radius 4 --no-popups       # bigger dots, smaller file
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #
DEFAULT_FILES = ["data/forsale.csv", "data/torent.csv"]
DEFAULT_OUTPUT = "data/property_map.html"

# Dot colours: (fill, stroke). The stroke darkens each fill so the yellow rent
# dots stay visible against the light basemap.
SALE_STYLE = {"fill": "#e03131", "stroke": "#8b1a1a"}   # red
RENT_STYLE = {"fill": "#f7d000", "stroke": "#9a7d00"}   # yellow

# Belgium bounding box, used as the initial view when there are no points.
BELGIUM_BOUNDS = [[49.45, 2.50], [51.55, 6.45]]

# Plausible bounding box for a Belgian property (Belgium + a small margin), as
# (lat_min, lat_max, lon_min, lon_max). Some source rows have latitude and
# longitude swapped; a point outside this box whose swap falls inside it is
# un-swapped, and anything still outside is dropped. Without this guard a single
# stray row zooms the auto-fit out to the whole globe and the map looks empty.
PLOT_BOUNDS = (49.3, 51.7, 2.3, 6.6)


# --------------------------------------------------------------------------- #
# Data loading                                                                #
# --------------------------------------------------------------------------- #
def classify(row: dict, filename: str) -> str:
    """Return 'rent' or 'sale' for a property row."""
    t = (row.get("transaction_type") or "").lower()
    if "rent" in t:
        return "rent"
    if "sale" in t:
        return "sale"
    # Fall back to the file name (e.g. torent.csv / forsale.csv).
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
    """Return (lat, lon, status) — status is 'ok', 'swapped' or 'drop'.

    Recovers rows whose latitude / longitude are stored the wrong way round.
    """
    if _within_belgium(lat, lon):
        return lat, lon, "ok"
    if _within_belgium(lon, lat):
        return lon, lat, "swapped"
    return lat, lon, "drop"


def load_points(files: list[str], *, with_popups: bool):
    """Return (sale_points, rent_points, stats). Each point is a compact list.

    Point layout: [lat, lon] or, with popups, [lat, lon, price, locality, type, url].
    stats counts rows missing coordinates, recovered swaps and dropped outliers.
    """
    sale: list[list] = []
    rent: list[list] = []
    stats = {"no_coords": 0, "swapped": 0, "dropped": 0}

    for f in files:
        path = Path(f)
        if not path.exists():
            print(f"skip: {path} not found", file=sys.stderr)
            continue
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


# --------------------------------------------------------------------------- #
# HTML rendering                                                              #
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Render an interactive Belgium map of the scraped properties "
                    "(red = for sale, yellow = for rent).")
    ap.add_argument("--files", nargs="+", default=DEFAULT_FILES,
                    help="CSV file(s) to plot (default: the two scraper outputs)")
    ap.add_argument("--output", default=DEFAULT_OUTPUT,
                    help=f"output HTML path (default: {DEFAULT_OUTPUT})")
    ap.add_argument("--radius", type=float, default=3.0,
                    help="dot radius in pixels (default: 3)")
    ap.add_argument("--no-popups", action="store_true",
                    help="omit per-dot popups (smaller file, faster load)")
    ap.add_argument("--title", default="Belgium property map",
                    help="HTML page title")
    args = ap.parse_args()

    with_popups = not args.no_popups
    sale, rent, stats = load_points(args.files, with_popups=with_popups)
    total = len(sale) + len(rent)
    if total == 0:
        print("No geocoded properties found — run postprocessing.py first.",
              file=sys.stderr)
        sys.exit(1)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        build_html(sale, rent, radius=args.radius, with_popups=with_popups,
                   title=args.title),
        encoding="utf-8")

    size_mb = out_path.stat().st_size / (1024 * 1024)
    notes = []
    if stats["swapped"]:
        notes.append(f"recovered {stats['swapped']} rows with swapped lat/lon")
    if stats["dropped"]:
        notes.append(f"dropped {stats['dropped']} rows outside Belgium")
    if stats["no_coords"]:
        notes.append(f"skipped {stats['no_coords']} rows without coordinates")
    print(f"Wrote {total} dots ({len(sale)} sale / red, {len(rent)} rent / yellow) "
          f"to {out_path}  [{size_mb:.1f} MB]"
          + ("; " + ", ".join(notes) if notes else ""),
          file=sys.stderr)


if __name__ == "__main__":
    main()
