#!/usr/bin/env python3
"""postprocessing.py — geocode the scraped listings and tag the nearest big city.

Enriches the property CSVs produced by the scraper (by default
``data/forsale.csv`` and ``data/torent.csv``) with geographic information:

  * ``latitude`` / ``longitude`` — filled in for any record that is still
    missing coordinates, by geocoding its street address through the free
    OpenStreetMap **Nominatim** API (no API key required). These columns
    already exist in the scraper output, so they are populated in place rather
    than re-created; rows that already have coordinates are left untouched.
  * ``nearest_city`` / ``nearest_city_distance_km`` — the name of, and the
    great-circle distance (km) to, the closest city of more than 50 000
    inhabitants, computed from the coordinates against the embedded
    ``BIG_CITIES`` table below.

The run is **resumable**: the CSV itself is the state. Each record is only
processed for the columns it is still missing, progress is check-pointed to
disk every ``CHECKPOINT_EVERY`` geocodes (and on Ctrl-C), so an interrupted
run simply picks up where it left off on the next invocation — already
enriched rows are skipped and incur no API calls.

Progress is reported on a single, continuously updated terminal line.

Requirements:  requests
    pip install requests

Usage:
    python postprocessing.py                         # process the two default CSVs
    python postprocessing.py --files data/a.csv      # process specific file(s)
    python postprocessing.py --limit 50              # cap geocodes per file (testing)
    python postprocessing.py --force                 # recompute every row from scratch
"""
from __future__ import annotations

import argparse
import csv
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
DEFAULT_FILES = ["data/forsale.csv", "data/torent.csv"]

# Geocoding (OpenStreetMap Nominatim — free, keyless, but politeness-bound).
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim's usage policy *requires* an identifying User-Agent and at most one
# request per second. Change the contact below if you fork this script.
USER_AGENT = "immo-eliza-postprocessing/1.0 (stephane@stepvda.com)"
GEOCODE_MIN_INTERVAL = 1.1   # seconds between geocoding requests (<= 1 req/s)
REQUEST_TIMEOUT = 30         # seconds
MAX_RETRIES = 3
CHECKPOINT_EVERY = 20        # flush the CSV to disk after this many geocodes

# Column names.
LAT_COL, LON_COL = "latitude", "longitude"
CITY_COL, DIST_COL = "nearest_city", "nearest_city_distance_km"
NEW_COLUMNS = [CITY_COL, DIST_COL]

# Cities of > 50 000 inhabitants, with approximate centroid coordinates, used to
# find the closest big city to each property. The list is the set of Belgian
# municipalities above the threshold (the 18 Brussels-Capital sub-municipalities
# are collapsed into a single "Brussels" entry to avoid nonsensical answers such
# as "nearest big city: Forest"), plus the large foreign cities just across the
# border so that frontier properties get a correct nearest-city answer.
# Edit freely — distances are recomputed from whatever is listed here.
BIG_CITIES: list[tuple[str, float, float]] = [
    # name,                 latitude,  longitude
    ("Antwerp",             51.2194,   4.4025),
    ("Ghent",               51.0543,   3.7174),
    ("Charleroi",           50.4108,   4.4446),
    ("Liège",               50.6451,   5.5734),
    ("Brussels",            50.8467,   4.3499),
    ("Bruges",              51.2093,   3.2247),
    ("Namur",               50.4674,   4.8719),
    ("Leuven",              50.8798,   4.7005),
    ("Mons",                50.4542,   3.9563),
    ("Mechelen",            51.0281,   4.4801),
    ("Aalst",               50.9403,   4.0364),
    ("La Louvière",         50.4854,   4.1875),
    ("Kortrijk",            50.8281,   3.2649),
    ("Hasselt",             50.9307,   5.3378),
    ("Sint-Niklaas",        51.1652,   4.1437),
    ("Ostend",              51.2247,   2.9156),
    ("Tournai",             50.6071,   3.3892),
    ("Genk",                50.9650,   5.5006),
    ("Seraing",             50.5836,   5.5006),
    ("Roeselare",           50.9469,   3.1228),
    ("Mouscron",            50.7440,   3.2069),
    ("Verviers",            50.5911,   5.8625),
    # Large cities across the border (for frontier properties).
    ("Lille (FR)",          50.6292,   3.0573),
    ("Roubaix (FR)",        50.6942,   3.1746),
    ("Tourcoing (FR)",      50.7236,   3.1610),
    ("Dunkirk (FR)",        51.0344,   2.3768),
    ("Maastricht (NL)",     50.8514,   5.6910),
    ("Eindhoven (NL)",      51.4416,   5.4697),
    ("Aachen (DE)",         50.7753,   6.0839),
    ("Luxembourg (LU)",     49.6116,   6.1319),
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
        self.api_calls = 0  # number of network requests actually issued

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
        """Geocode a property row, falling back from full address to locality."""
        street = (row.get("street") or "").strip()
        number = (row.get("house_number") or "").strip()
        postal = (row.get("postal_code") or "").strip()
        city = (row.get("locality") or "").strip()
        if not (street or city or postal):
            return None  # nothing to geocode

        # Two attempts, coarsening on failure: exact street address, then the
        # locality / postcode centroid. Results are cached per distinct query.
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
# Processing                                                                   #
# --------------------------------------------------------------------------- #
def progress(file_label: str, done: int, total: int, geocoded: int,
             cities: int, status: str = "") -> None:
    """Render the running progress on a single, overwritten terminal line."""
    pct = (done / total * 100) if total else 100.0
    line = (f"\r[{file_label}] {done}/{total} ({pct:5.1f}%)  "
            f"geocoded={geocoded}  cities_tagged={cities}  {status}")
    sys.stderr.write(line.ljust(100)[:100])
    sys.stderr.flush()


def process_file(path: Path, geocoder: Geocoder, *, limit: int = 0,
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
    pending_geocodes = 0  # geocodes since last checkpoint

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
                        lat = lon = None  # malformed coords — leave city blank
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
        # Always persist whatever progress was made (covers Ctrl-C / errors).
        checkpoint()
        sys.stderr.write("\n")
        sys.stderr.flush()

    return {"total": total, "geocoded": geocoded, "cities": cities,
            "skipped": skipped, "api_calls": geocoder.api_calls}


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Geocode scraped listings and tag the nearest big city "
                    "(resumable; only fills records still missing the columns).")
    ap.add_argument("--files", nargs="+", default=DEFAULT_FILES,
                    help="CSV file(s) to process (default: the two scraper outputs)")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap the number of geocoding API calls per file (testing)")
    ap.add_argument("--force", action="store_true",
                    help="recompute the nearest-city columns for every row")
    args = ap.parse_args()

    geocoder = Geocoder()
    grand_total = {"total": 0, "geocoded": 0, "cities": 0, "skipped": 0}

    for f in args.files:
        path = Path(f)
        if not path.exists():
            print(f"skip: {path} not found", file=sys.stderr)
            continue
        print(f"Processing {path} …", file=sys.stderr)
        # Fresh geocoder counter per file so --limit is per-file.
        geocoder.api_calls = 0
        stats = process_file(path, geocoder, limit=args.limit, force=args.force)
        print(f"  {path.name}: {stats['total']} rows | "
              f"{stats['geocoded']} newly geocoded | "
              f"{stats['cities']} cities tagged | "
              f"{stats['skipped']} already complete | "
              f"{stats['api_calls']} API calls", file=sys.stderr)
        for k in grand_total:
            grand_total[k] += stats[k]

    print(f"Done. {grand_total['total']} rows across {len(args.files)} file(s): "
          f"{grand_total['geocoded']} geocoded, "
          f"{grand_total['cities']} nearest-city tags added, "
          f"{grand_total['skipped']} already complete.", file=sys.stderr)


if __name__ == "__main__":
    main()
