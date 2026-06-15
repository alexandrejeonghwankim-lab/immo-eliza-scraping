#!/usr/bin/env python3
"""scraper.py — Immovlan property detail scraper.

Reads property URLs from ``data/property_urls.csv`` one by one and, for each
listing, it:

  * downloads the raw HTML page to ``data/html/<property_id>.html``;
  * decides from the page itself whether the listing is FOR SALE or FOR RENT
    (the ``transaction_type`` in the page's GTM data layer / the ``A vendre`` vs
    ``A louer`` meta tag) and therefore which data dictionary applies
    (``data_dictionary.txt`` for sale, ``data_dictionary_rent.txt`` for rent);
  * scrapes every field documented in those dictionaries that the page exposes;
  * appends the scraped row to ``data/forsale.csv`` or ``data/torent.csv``.
    Both files carry the full dictionary column set plus an ``html_path`` column
    holding the path of the downloaded HTML snapshot.

The ``property_id`` used to name the HTML file is the Immovlan listing reference
discovered while parsing the page (e.g. ``rbv47406``).

The run is resumable: on start-up it reads back the two output CSVs, collects
the URLs already scraped and skips them, so an interrupted run can simply be
restarted. Each scraped row is flushed to disk immediately, so at most the
in-flight listing is lost on a hard interruption.

Requirements:  requests, beautifulsoup4, lxml, pandas
    pip install requests beautifulsoup4 lxml pandas

Usage:
    python scraper.py                          # scrape everything, resuming
    python scraper.py --limit 50               # stop after 50 new listings
    python scraper.py --input data/urls.csv    # use a different URL list
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Paths & runtime configuration                                               #
# --------------------------------------------------------------------------- #
# Anchor default paths to the project root (the parent of this script's folder)
# so the scraper works regardless of the current working directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
INPUT_CSV = DATA_DIR / "property_urls.csv"
HTML_DIR = DATA_DIR / "html"
FORSALE_CSV = DATA_DIR / "forsale.csv"
TORENT_CSV = DATA_DIR / "torent.csv"

MIN_DELAY = 0.6          # seconds between requests (lower bound)
MAX_DELAY = 1.6          # seconds between requests (upper bound)
REQUEST_TIMEOUT = 30     # seconds
MAX_RETRIES = 3

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("scraper")

# --------------------------------------------------------------------------- #
# Output schema — column order follows the data dictionaries.                 #
# `html_path` is the path to the downloaded HTML snapshot (the "html" field).  #
# --------------------------------------------------------------------------- #
COLUMNS = [
    # Identity & provenance
    "property_id", "url", "source", "scrape_date", "posting_date", "html_path",
    # Classification
    "property_type", "category", "transaction_type", "new_construction",
    # Price
    "price", "price_per_sqm", "vat", "cadastral_income",
    # Location
    "street", "house_number", "postal_code", "locality", "province", "region",
    "latitude", "longitude",
    # Interior
    "bedrooms", "bedroom_surfaces", "livable_surface", "living_room_surface",
    "kitchen_surface", "kitchen_equipment", "bathrooms", "showers", "toilets",
    "build_year", "currently_leased", "furnished",
    # Technical / energy
    "heating_type", "glazing_type", "heat_pump", "solar_panels",
    "air_conditioning", "primary_energy_consumption", "epc",
    "electrical_certificate", "facades", "facade_orientation",
    # Outdoor & extras
    "garden", "garden_surface", "garden_orientation", "terrace",
    "terrace_surface", "balcony", "garage", "garages", "indoor_parking",
    "outdoor_parking", "cellar", "swimming_pool", "elevator", "running_water",
    # Risk / availability
    "flooding_area_type", "demarcated_flooding_area", "flood_g_score",
    "flood_p_score", "availability", "building_state",
    # House-specific
    "land_surface", "ground_depth", "terrain_width", "number_of_floors",
    # Apartment-specific
    "apartment_floor", "maintenance_cost", "co_ownership_charges",
]

BOOL_FIELDS = {
    "new_construction", "vat", "currently_leased", "furnished", "heat_pump",
    "solar_panels", "air_conditioning", "electrical_certificate", "garden",
    "terrace", "balcony", "garage", "cellar", "swimming_pool", "elevator",
    "running_water", "demarcated_flooding_area",
}
INT_FIELDS = {
    "price", "cadastral_income", "postal_code", "bedrooms", "livable_surface",
    "living_room_surface", "kitchen_surface", "bathrooms", "showers", "toilets",
    "build_year", "primary_energy_consumption", "facades", "garden_surface",
    "terrace_surface", "garages", "indoor_parking", "outdoor_parking",
    "land_surface", "ground_depth", "terrain_width", "number_of_floors",
    "apartment_floor", "maintenance_cost", "co_ownership_charges",
}
FLOAT_FIELDS = {"price_per_sqm", "latitude", "longitude"}

# --------------------------------------------------------------------------- #
# Mapping from the page's characteristic labels (the <h4> text in each         #
# data-row) to dictionary fields. Keys are normalised (lower-case, no trailing #
# punctuation). Several wordings are included so the parser is robust across   #
# listings; only labels actually present on a page are used.                   #
# --------------------------------------------------------------------------- #
LABEL_MAP = {
    # Interior
    "number of bedrooms": "bedrooms",
    "livable surface": "livable_surface",
    "surface of living-room": "living_room_surface",
    "surface living-room": "living_room_surface",      # alias
    "furnished": "furnished",
    "currently leased": "currently_leased",
    "cellar": "cellar",
    # Kitchen & bathrooms
    "kitchen equipment": "kitchen_equipment",
    "surface kitchen": "kitchen_surface",
    "surface of the kitchen": "kitchen_surface",        # alias
    "number of bathrooms": "bathrooms",
    "number of showers": "showers",
    "number of toilets": "toilets",
    # Heating & energy
    "type of heating": "heating_type",
    "type of glazing": "glazing_type",
    "heat pump": "heat_pump",
    "solar panels": "solar_panels",
    "photovoltaic solar panels": "solar_panels",        # alias
    "air conditioning": "air_conditioning",
    "specific primary energy consumption": "primary_energy_consumption",
    "energy class": "epc",                              # alias (epc usually from <meta>)
    "certification - electrical installation": "electrical_certificate",
    # Construction / structure
    "build year": "build_year",
    "construction year": "build_year",                  # alias
    "year of construction": "build_year",               # alias
    "number of facades": "facades",
    "number of frontages": "facades",                   # alias
    "orientation of the front facade": "facade_orientation",
    "state of the property": "building_state",
    "building state": "building_state",                 # alias
    "number of floors": "number_of_floors",
    "floor of appartment": "apartment_floor",
    "floor of apartment": "apartment_floor",            # alias
    # Outdoor & extras
    "garden": "garden",
    "surface garden": "garden_surface",
    "garden surface": "garden_surface",                 # alias
    "garden orientation": "garden_orientation",
    "terrace": "terrace",
    "surface terrace": "terrace_surface",
    "terrace surface": "terrace_surface",               # alias
    "balcony": "balcony",
    "garage": "garage",
    "number of garages": "garages",
    "number of parking spaces (indoor)": "indoor_parking",
    "number of indoor parkings": "indoor_parking",      # alias
    "number of parking places (outdoor)": "outdoor_parking",
    "number of outdoor parkings": "outdoor_parking",    # alias
    "swimming pool": "swimming_pool",
    "elevator": "elevator",
    "running water": "running_water",
    # Plot (house-specific)
    "total land surface": "land_surface",
    "surface of the plot": "land_surface",              # alias
    "land surface": "land_surface",                     # alias
    "ground depth": "ground_depth",
    "terrain width at the roadside": "terrain_width",
    # Risk / availability
    "flooding area type": "flooding_area_type",
    "availability": "availability",
    # note: "demarcated flooding area" and the flood G-/P-scores are handled
    # separately in parse_html (descriptive text / nested grade markup).
}

# The "Financial details" block is a plain <ul> of "<strong>Label</strong>: value"
# items (separate from the characteristic data-rows above).
FINANCIAL_MAP = {
    "price": "price",
    "rent": "price",
    "cadastral income": "cadastral_income",
    "maintenance cost": "maintenance_cost",
    "vat": "vat",
    "vat applied?": "vat",
}

# --------------------------------------------------------------------------- #
# Belgian postal-code -> (province, region) enrichment.                       #
# Ranges follow the official Belgian postal-code layout.                       #
# --------------------------------------------------------------------------- #
FLANDERS, WALLONIA, BRUSSELS = "Flanders", "Wallonia", "Brussels"
POSTAL_RANGES = [
    (1000, 1299, "Brussels", BRUSSELS),
    (1300, 1499, "Walloon Brabant", WALLONIA),
    (1500, 1999, "Flemish Brabant", FLANDERS),
    (2000, 2999, "Antwerp", FLANDERS),
    (3000, 3499, "Flemish Brabant", FLANDERS),
    (3500, 3999, "Limburg", FLANDERS),
    (4000, 4999, "Liège", WALLONIA),
    (5000, 5999, "Namur", WALLONIA),
    (6000, 6599, "Hainaut", WALLONIA),
    (6600, 6999, "Luxembourg", WALLONIA),
    (7000, 7999, "Hainaut", WALLONIA),
    (8000, 8999, "West Flanders", FLANDERS),
    (9000, 9999, "East Flanders", FLANDERS),
]


def province_region(postal_code: int | None) -> tuple[str | None, str | None]:
    if postal_code is None:
        return None, None
    for lo, hi, prov, reg in POSTAL_RANGES:
        if lo <= postal_code <= hi:
            return prov, reg
    return None, None


# --------------------------------------------------------------------------- #
# Value parsing helpers                                                       #
# --------------------------------------------------------------------------- #
_MISSING = {
    "", "n/a", "na", "unknown", "inconnu", "not specified", "not communicated",
    "(information not available)", "information not available", "-",
}


def normalize_label(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower()).rstrip(":").strip()


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    v = re.sub(r"\s+", " ", value.strip())
    if v.lower() in _MISSING:
        return None
    return v or None


def to_int(value: str | None) -> int | None:
    """First integer in `value`, ignoring thousand separators (space/dot)."""
    v = clean_text(value)
    if v is None:
        return None
    m = re.search(r"-?\d[\d.\s  ]*", v)
    if not m:
        return None
    num = re.sub(r"[\s  ]", "", m.group(0))
    num = re.split(r"[.,]", num)[0]          # drop any decimal part
    try:
        return int(num)
    except ValueError:
        return None


def to_float(value: str | None) -> float | None:
    v = clean_text(value)
    if v is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", v.replace(",", "."))
    return float(m.group(0)) if m else None


def to_bool(value: str | None) -> int | None:
    """Map yes/no-ish text to 1/0 (None if unknown/absent)."""
    v = clean_text(value)
    if v is None:
        return None
    low = v.lower()
    if low.startswith(("yes", "oui", "ja")) or "in accordance" in low:
        return 1
    if low.startswith(("no", "non", "nee")) or "not in accordance" in low:
        return 0
    return None


def coerce(field: str, value: str | None):
    if field in BOOL_FIELDS:
        return to_bool(value)
    if field in INT_FIELDS:
        return to_int(value)
    if field in FLOAT_FIELDS:
        return to_float(value)
    return clean_text(value)


# --------------------------------------------------------------------------- #
# Page-level extraction                                                       #
# --------------------------------------------------------------------------- #
def _js_var(html: str, name: str) -> str | None:
    m = re.search(rf"window\.{name}\s*=\s*'([^']*)'", html)
    return m.group(1) if m else None


def _data_layer(html: str) -> dict:
    """The object pushed to GTM's dataLayer holds clean core attributes.

    The call looks like ``dataLayer.push({...} || {})``; extract the first
    balanced ``{...}`` object (a plain regex would swallow the ``|| {}`` tail).
    """
    idx = html.find("dataLayer.push(")
    start = html.find("{", idx) if idx != -1 else -1
    if start == -1:
        return {}
    depth = 0
    for i in range(start, len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start:i + 1])
                except json.JSONDecodeError:
                    return {}
    return {}


def _og(soup: BeautifulSoup, prop: str) -> str | None:
    tag = soup.find("meta", attrs={"property": prop})
    return tag.get("content") if tag and tag.get("content") else None


def detect_transaction(html: str, dl: dict, url: str) -> str | None:
    """Return 'for-sale' / 'for-rent' from the page, else None."""
    tx = (dl.get("transaction_type") or "").lower()
    if tx in ("sale", "for-sale"):
        return "for-sale"
    if tx in ("rent", "for-rent", "let"):
        return "for-rent"
    m = re.search(r'rbf-immovlan-section"\s+content="([^"]+)"', html)
    if m:
        section = m.group(1).lower()
        if "vendre" in section or "koop" in section or "sale" in section:
            return "for-sale"
        if "louer" in section or "huur" in section or "rent" in section:
            return "for-rent"
    if "/for-sale/" in url:
        return "for-sale"
    if "/for-rent/" in url:
        return "for-rent"
    return None


def detect_property_id(html: str, soup: BeautifulSoup, dl: dict, url: str) -> str | None:
    ref = _js_var(html, "IMMOVLAN_REFERENCE")
    if ref:
        return ref.lower()
    if dl.get("vlan_code"):
        return str(dl["vlan_code"]).lower()
    og_url = _og(soup, "og:url") or ""
    m = re.search(r"/([a-z0-9]+)/?$", og_url, re.I)
    if m:
        return m.group(1).lower()
    return url.rstrip("/").rsplit("/", 1)[-1].lower() or None


def extract_characteristics(soup: BeautifulSoup) -> dict[str, str]:
    """Collect every <h4>label</h4><p>value</p> pair in the detail data-rows."""
    pairs: dict[str, str] = {}
    for row in soup.select("div.data-row"):
        for item in row.select(".data-row-wrapper > div"):
            h4 = item.find("h4")
            p = item.find("p")
            if h4 and p:
                label = normalize_label(h4.get_text(" ", strip=True))
                value = p.get_text(" ", strip=True)
                # Skip document/attachment rows (h4 = filename, p = "Download").
                if label and label not in pairs and value.strip().lower() != "download":
                    pairs[label] = value
    return pairs


def extract_financials(soup: BeautifulSoup) -> dict[str, str]:
    """Collect the "Financial details" list items (price, cadastral income,
    VAT, maintenance cost, ...). Each item is "<strong>Label</strong> : value"."""
    out: dict[str, str] = {}
    fin = soup.select_one(".financial")
    if not fin:
        return out
    for li in fin.select("li"):
        strong = li.find("strong")
        if not strong:
            continue
        label = normalize_label(strong.get_text(" ", strip=True))
        full = li.get_text(" ", strip=True)
        value = full.split(":", 1)[1].strip() if ":" in full else ""
        if label and label not in out:
            out[label] = value
    return out


def extract_flood_scores(soup: BeautifulSoup) -> dict[str, str]:
    """Flemish "watertoets" flood scores. The <h4> starts with 'G-score' /
    'P-score' (followed by a tooltip) and the grade letter sits in the <p>."""
    out: dict[str, str] = {}
    for item in soup.select("div.data-row .data-row-wrapper > div"):
        h4 = item.find("h4")
        if not h4:
            continue
        label = h4.get_text(" ", strip=True).lower()
        key = "flood_g_score" if label.startswith(("g-score", "g score")) else \
              "flood_p_score" if label.startswith(("p-score", "p score")) else None
        if not key or key in out:
            continue
        # The grade letter is in the <p> sibling of the <h4>, not the (now empty)
        # <p> the parser leaves inside the <h4>'s tooltip markup.
        grade_p = next((p for p in item.find_all("p") if p.find_parent("h4") is None), None)
        if grade_p:
            m = re.search(r"[A-G]", grade_p.get_text(" ", strip=True))
            if m:
                out[key] = m.group(0)
    return out


def parse_address(soup: BeautifulSoup, postal_code: int | None) -> dict:
    """Split the header address into street / house_number / locality."""
    out = {"street": None, "house_number": None, "locality": None}
    el = soup.select_one(".detail__header_address")
    if not el:
        return out
    text = clean_text(el.get_text(" ", strip=True))
    if not text:
        return out
    # The address reads "<street and number> <postal> <locality>".
    if postal_code is not None:
        m = re.search(rf"\b{postal_code}\b\s*(.*)$", text)
        if m:
            out["locality"] = clean_text(m.group(1)) or None
            text = text[: m.start()].strip()
    # Whatever remains is the street line; trailing numbers are the house no.
    hm = re.search(r"^(.*?)(\d+[\w\-/]*(?:\s+\d+\w*)?)\s*$", text)
    if hm and hm.group(1).strip():
        out["street"] = clean_text(hm.group(1))
        out["house_number"] = clean_text(hm.group(2))
    else:
        out["street"] = clean_text(text) or None
    return out


def parse_html(html: str, url: str) -> dict | None:
    """Parse one detail page into a dictionary row (None if unusable)."""
    soup = BeautifulSoup(html, "lxml")
    dl = _data_layer(html)

    transaction = detect_transaction(html, dl, url)
    property_id = detect_property_id(html, soup, dl, url)
    if not property_id or not transaction:
        return None

    row: dict[str, object] = {col: None for col in COLUMNS}
    row["property_id"] = property_id
    row["url"] = url
    row["source"] = "ImmoVlan"
    row["scrape_date"] = date.today().isoformat()
    row["transaction_type"] = transaction

    # Posting date from the JobPosting / RealEstateListing structured data.
    m = re.search(r'"datePosted"\s*:\s*"([0-9]{4}-[0-9]{2}-[0-9]{2})', html)
    if m:
        row["posting_date"] = m.group(1)

    # Classification (GTM data layer is cleanest; fall back to the URL).
    sub = clean_text(dl.get("property_sub_type"))
    ptype = clean_text(dl.get("property_type"))
    row["property_type"] = sub or ptype
    blob = f"{ptype or ''} {sub or ''} {url}".lower()
    if "appart" in blob or "flat" in blob or "studio" in blob:
        row["category"] = "apartment"
    elif "house" in blob or "villa" in blob or "residence" in blob:
        row["category"] = "house"
    if isinstance(dl.get("is_new_construction_project"), bool):
        row["new_construction"] = 1 if dl["is_new_construction_project"] else 0

    # Price (data layer carries the numeric value reliably).
    row["price"] = to_int(str(dl.get("price"))) if dl.get("price") else None

    # Location.
    postal = to_int(str(dl.get("zip_code"))) if dl.get("zip_code") else None
    row["postal_code"] = postal
    row.update(parse_address(soup, postal))
    row["province"], row["region"] = province_region(postal)
    row["latitude"] = to_float(_js_var(html, "AD_LATITUDE"))
    row["longitude"] = to_float(_js_var(html, "AD_LONGITUDE"))

    # EPC label from the social description ("... | EPC B | ...").
    desc = _og(soup, "og:description") or ""
    em = re.search(r"EPC\s+([A-G][+\-]*\d*)", desc, re.I)
    if em:
        row["epc"] = em.group(1).upper()

    # Detailed characteristics from the data-row tables.
    chars = extract_characteristics(soup)
    for label, value in chars.items():
        field = LABEL_MAP.get(label)
        if field and row.get(field) in (None, ""):
            row[field] = coerce(field, value)

    # Financial details block (cadastral income, VAT, maintenance cost, and a
    # price/rent fallback if the data layer didn't carry it).
    for label, value in extract_financials(soup).items():
        field = FINANCIAL_MAP.get(label)
        if field and row.get(field) in (None, ""):
            row[field] = coerce(field, value)

    # Flood G-/P-scores: structured watertoets block, with a free-text fallback
    # ("P-score: D / G-score: A") for the listings that only mention them in prose.
    flood = extract_flood_scores(soup)
    row["flood_g_score"] = flood.get("flood_g_score")
    row["flood_p_score"] = flood.get("flood_p_score")
    if row["flood_g_score"] is None:
        gm = re.search(r"G-score:\s*([A-G])", html)
        if gm:
            row["flood_g_score"] = gm.group(1)
    if row["flood_p_score"] is None:
        pm = re.search(r"P-score:\s*([A-G])", html)
        if pm:
            row["flood_p_score"] = pm.group(1)

    # Demarcated flooding area is published as descriptive text ("a demarcated
    # flooding area"); treat any real value as present (1), "n/a"/absence as NULL.
    dfa = clean_text(chars.get("demarcated flooding area"))
    if dfa is not None:
        row["demarcated_flooding_area"] = 0 if dfa.lower().startswith(("no", "not ", "non")) else 1

    # Livable surface fallback from the data layer.
    if row.get("livable_surface") is None and dl.get("livable_surface"):
        row["livable_surface"] = to_int(str(dl["livable_surface"]))

    # Per-bedroom surfaces -> ';'-joined string (e.g. "15;10").
    bedroom_surfaces = [
        to_int(chars[k])
        for k in sorted(chars)
        if re.fullmatch(r"surface bedroom \d+", k)
    ]
    bedroom_surfaces = [str(s) for s in bedroom_surfaces if s is not None]
    if bedroom_surfaces:
        row["bedroom_surfaces"] = ";".join(bedroom_surfaces)

    # Calculated field: price per square metre.
    price, surface = row.get("price"), row.get("livable_surface")
    if isinstance(price, int) and isinstance(surface, int) and surface:
        row["price_per_sqm"] = round(price / surface, 2)

    return row


# --------------------------------------------------------------------------- #
# HTTP                                                                         #
# --------------------------------------------------------------------------- #
class Fetcher:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": random.choice(USER_AGENTS),
            "Accept-Language": "en-US,en;q=0.9,nl;q=0.8,fr;q=0.7",
        })

    def get(self, url: str) -> str | None:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if random.random() < 0.2:
                    self.session.headers["User-Agent"] = random.choice(USER_AGENTS)
                resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
                if resp.status_code == 200 and len(resp.text) > 2000:
                    return resp.text
                if resp.status_code == 404:
                    log.warning("404 Not Found: %s", url)
                    return None
                log.warning("HTTP %s on %s (attempt %d)", resp.status_code, url, attempt)
            except requests.RequestException as exc:
                log.warning("Request error on %s: %s (attempt %d)", url, exc, attempt)
            time.sleep(1.5 * attempt)
        return None

    def sleep(self) -> None:
        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


# --------------------------------------------------------------------------- #
# Resume / output                                                             #
# --------------------------------------------------------------------------- #
def load_done_urls(*paths: str) -> set[str]:
    """URLs already present in the output CSVs (so they can be skipped)."""
    done: set[str] = set()
    for path in paths:
        p = Path(path)
        if p.exists() and p.stat().st_size > 0:
            try:
                df = pd.read_csv(p, usecols=["url"])
                done.update(df["url"].dropna().astype(str))
            except (ValueError, pd.errors.EmptyDataError):
                log.warning("Could not read existing output %s for resume", path)
    return done


def append_row(path: str, row: dict) -> None:
    """Append one scraped row, writing the header only when the file is new."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_header = not out.exists() or out.stat().st_size == 0
    frame = pd.DataFrame([row], columns=COLUMNS)
    frame.to_csv(out, mode="a", header=write_header, index=False)


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="Scrape Immovlan property detail pages to CSV.")
    ap.add_argument("--input", default=INPUT_CSV, help="input CSV with a 'url' column")
    ap.add_argument("--html-dir", default=HTML_DIR, help="where to store downloaded HTML")
    ap.add_argument("--forsale-csv", default=FORSALE_CSV, help="output CSV for sale listings")
    ap.add_argument("--torent-csv", default=TORENT_CSV, help="output CSV for rent listings")
    ap.add_argument("--limit", type=int, default=0, help="stop after N newly scraped listings (0 = all)")
    args = ap.parse_args()

    urls = pd.read_csv(args.input)["url"].dropna().astype(str).tolist()
    done = load_done_urls(args.forsale_csv, args.torent_csv)
    log.info("Input: %d URLs | already scraped: %d", len(urls), len(done))

    html_dir = Path(args.html_dir)
    html_dir.mkdir(parents=True, exist_ok=True)
    fetcher = Fetcher()

    total = len(urls)
    scraped = failed = skipped = 0
    for i, url in enumerate(urls, 1):
        # Live single-line progress: <input csv line> / <total urls>.
        print(f"\rscraping {i}/{total}", end="", flush=True)

        if url in done:
            skipped += 1
            continue

        html = fetcher.get(url)
        if not html:
            print()  # break off the progress line before logging
            log.warning("Failed to download: %s", url)
            failed += 1
            fetcher.sleep()
            continue

        row = parse_html(html, url)
        if not row:
            print()  # break off the progress line before logging
            log.warning("Could not parse listing: %s", url)
            failed += 1
            fetcher.sleep()
            continue

        # Save the HTML snapshot named by the discovered property_id.
        html_path = html_dir / f"{row['property_id']}.html"
        html_path.write_text(html, encoding="utf-8")
        row["html_path"] = str(html_path)

        target = args.forsale_csv if row["transaction_type"] == "for-sale" else args.torent_csv
        append_row(target, row)
        done.add(url)
        scraped += 1

        if args.limit and scraped >= args.limit:
            break
        fetcher.sleep()

    print()  # finish the progress line
    log.info("Done. %d scraped, %d skipped, %d failed.", scraped, skipped, failed)


if __name__ == "__main__":
    main()
