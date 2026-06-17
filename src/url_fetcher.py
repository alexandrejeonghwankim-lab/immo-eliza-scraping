
"""urlfetcher.py — standalone Immovlan property-URL collector.

Collects the URLs of individual residential properties (houses & apartments)
from immovlan.be and writes them to a CSV file. It:

  * handles the website's pagination (``?page=N``);
  * partitions each search by price band so it reaches the *whole* inventory
    instead of the shallow ~50-page window a single broad query exposes;
  * detects "project" listings (``/en/projectdetail/...``) — new-construction
    developments that bundle several sub-units — opens each one and keeps only
    the URLs of the individual units inside, never the project page itself;
  * keeps only house/apartment property types and de-duplicates globally.

It is fully self-contained: run it directly. Settings come from the
``DEFAULT_CONFIG`` block below, optionally overridden by a JSON config file
(default ``urlfetcher_config.json``) sitting next to the script.

Requirements:  requests, beautifulsoup4, lxml
    pip install requests beautifulsoup4 lxml

Usage:
    python urlfetcher.py                       # use defaults / urlfetcher_config.json
    python urlfetcher.py --config my.json      # use a specific config file
    python urlfetcher.py --output urls.csv     # override the output path
    python urlfetcher.py --limit 500           # stop after N unique URLs (testing)
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse
import os

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Configuration (override any of these via a JSON config file)                #
# --------------------------------------------------------------------------- #
# 1. Get the directory of the current script
script_dir = os.path.dirname(os.path.abspath(__file__))

# 2. Go up one level and into the target folder
target_folder = os.path.join(script_dir, "..", "data")

# 3. Create the full file path
file_path = os.path.join(target_folder, "property_urls.csv") 

DEFAULT_CONFIG = {  

    # Where to write the collected URLs.
    "output_csv": file_path,

    # Which transactions and property types to search.
    "transactions": ["for-sale", "for-rent"],
    "property_types": ["house", "apartment"],

    # Price bands (euros) used to slice each search into deep, distinct windows.
    # `null` as the upper bound means open-ended. Bands differ per transaction
    # because rent prices are monthly.
    "price_bands": {
        "for-sale": [
            [0, 150000], [150000, 250000], [250000, 350000], [350000, 500000],
            [500000, 750000], [750000, 1500000], [1500000, None],
        ],
        "for-rent": [
            [0, 600], [600, 800], [800, 1000], [1000, 1250], [1250, 1500],
            [1500, 2000], [2000, 3000], [3000, None],
        ],
    },

    # Crawl politeness / robustness.
    "min_delay": 0.6,          # seconds between requests (lower bound)
    "max_delay": 1.6,          # seconds between requests (upper bound)
    "request_timeout": 30,     # seconds
    "max_retries": 3,
    "max_pages_per_band": 700,  # safety cap on pagination within one band
    "empty_page_streak": 3,    # stop a band after this many pages yield nothing new
    "limit": 0,                # stop after N unique URLs (0 = no limit)
}

BASE_URL = "https://immovlan.be"
SEARCH_ENDPOINT = "https://immovlan.be/en/real-estate"

# Property sub-types that count as houses / apartments. Project sub-units are
# classified against these; anything else (land, garage, office, …) is dropped.
HOUSE_SUBTYPES = {
    "house", "residence", "villa", "master-house", "mansion", "chalet",
    "bungalow", "cottage", "country-cottage", "manor-house", "town-house",
    "farmhouse", "castle",
}
APARTMENT_SUBTYPES = {
    "apartment", "flat", "studio", "duplex", "triplex", "penthouse",
    "ground-floor", "loft", "student-flat", "service-flat", "flat-studio",
}

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
]

# /en/detail/<type>/<transaction>/<postal>/<locality>/<id>
DETAIL_RE = re.compile(
    r"/en/detail/([a-z0-9\-]+)/([a-z0-9\-]+)/(\d+)/([a-z0-9\-]+)/([a-z0-9]+)", re.I
)
# /en/projectdetail/<id>-<id>
PROJECT_RE = re.compile(r"/en/projectdetail/[0-9]+-[0-9]+", re.I)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("urlfetcher")


# --------------------------------------------------------------------------- #
# Config loading                                                              #
# --------------------------------------------------------------------------- #
def load_config(path: str | None) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    candidate = Path(path) if path else Path(__file__).with_name("urlfetcher_config.json")
    if candidate.exists():
        log.info("Loading config overrides from %s", candidate)
        with candidate.open(encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    return cfg


# --------------------------------------------------------------------------- #
# HTTP                                                                         #
# --------------------------------------------------------------------------- #
class Fetcher:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": random.choice(USER_AGENTS),
            "Accept-Language": "en-US,en;q=0.9,nl;q=0.8,fr;q=0.7",
        })

    def get(self, url: str) -> str | None:
        for attempt in range(1, self.cfg["max_retries"] + 1):
            try:
                if random.random() < 0.2:
                    self.session.headers["User-Agent"] = random.choice(USER_AGENTS)
                resp = self.session.get(url, timeout=self.cfg["request_timeout"])
                if resp.status_code == 200 and len(resp.text) > 2000:
                    return resp.text
                if resp.status_code == 404:
                    return None
                log.warning("HTTP %s on %s (attempt %d)", resp.status_code, url, attempt)
            except requests.RequestException as exc:
                log.warning("Request error on %s: %s (attempt %d)", url, exc, attempt)
            time.sleep(1.5 * attempt)
        return None

    def sleep(self) -> None:
        time.sleep(random.uniform(self.cfg["min_delay"], self.cfg["max_delay"]))


# --------------------------------------------------------------------------- #
# Parsing helpers                                                             #
# --------------------------------------------------------------------------- #
def classify(property_type: str) -> str | None:
    """Return 'house' / 'apartment' for a detail-URL type segment, else None."""
    t = (property_type or "").lower()
    if t in HOUSE_SUBTYPES:
        return "house"
    if t in APARTMENT_SUBTYPES:
        return "apartment"
    return None


def extract_links(html: str) -> tuple[dict[str, str], set[str]]:
    """From a page, return ({detail_url: property_type}, {project_url}).

    Only house/apartment detail URLs are kept.
    """
    soup = BeautifulSoup(html, "lxml")
    details: dict[str, str] = {}
    projects: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = DETAIL_RE.search(href)
        if m:
            ptype = m.group(1)
            if classify(ptype):
                url = BASE_URL + m.group(0) if m.group(0).startswith("/") else m.group(0)
                details[url] = ptype
            continue
        pm = PROJECT_RE.search(href)
        if pm:
            url = BASE_URL + pm.group(0) if pm.group(0).startswith("/") else pm.group(0)
            projects.add(url)
    return details, projects


def page_url(base: str, page: int) -> str:
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}page={page}"


def build_partitions(cfg: dict) -> list[tuple[str, str]]:
    """Return (label, search_url) for every transaction x type x price band."""
    parts: list[tuple[str, str]] = []
    for transaction in cfg["transactions"]:
        bands = cfg["price_bands"].get(transaction, [[0, None]])
        for ptype in cfg["property_types"]:
            for lo, hi in bands:
                params = [
                    f"transactiontypes={transaction}",
                    f"propertytypes={ptype}",
                    f"minprice={lo}",
                ]
                if hi is not None:
                    params.append(f"maxprice={hi}")
                label = f"{transaction}/{ptype}/{lo}-{hi or 'max'}"
                parts.append((label, f"{SEARCH_ENDPOINT}?{'&'.join(params)}"))
    return parts


# --------------------------------------------------------------------------- #
# Collection                                                                  #
# --------------------------------------------------------------------------- #
def collect(cfg: dict, fetcher: Fetcher):
    """Walk all search partitions. Returns (property_map, project_set)."""
    properties: dict[str, str] = {}   # url -> property_type
    projects: set[str] = set()
    
    limit = cfg["limit"] or 0

    for label, base in build_partitions(cfg):
        if limit and len(properties) >= limit:
            break
        empty_streak = 0
        for page in range(1, cfg["max_pages_per_band"] + 1):
            if limit and len(properties) >= limit:
                break
            html = fetcher.get(page_url(base, page))
            if not html:
                empty_streak += 1
                if empty_streak >= cfg["empty_page_streak"]:
                    break
                continue
            details, page_projects = extract_links(html)
            new_props = {u: t for u, t in details.items() if u not in properties}
            new_projects = page_projects - projects
            if not new_props and not new_projects:
                empty_streak += 1
                if empty_streak >= cfg["empty_page_streak"]:
                    log.info("[%s] exhausted at page %d", label, page)
                    break
            else:
                empty_streak = 0
                properties.update(new_props)
                projects.update(new_projects)
                log.info("[%s] page %d: +%d properties, +%d projects (totals %d / %d)",
                         label, page, len(new_props), len(new_projects),
                         len(properties), len(projects))
            fetcher.sleep()
    return properties, projects


def expand_projects(cfg: dict, fetcher: Fetcher, projects: set[str],
                    known: set[str]):
    """Open each project page and return its child house/apartment unit URLs.

    `known` lets us skip child URLs already collected from search pages.
    Returns {url: property_type}.
    """
    found: dict[str, str] = {}
    total = len(projects)
    for i, purl in enumerate(sorted(projects), 1):
        html = fetcher.get(purl)
        if not html:
            continue
        details, _ = extract_links(html)
        added = 0
        for url, ptype in details.items():
            if url not in known and url not in found:
                found[url] = ptype
                added += 1
        if added:
            log.info("project %d/%d %s -> +%d units", i, total, purl.split("/")[-1], added)
        fetcher.sleep()
    return found


# --------------------------------------------------------------------------- #
# Output                                                                       #
# --------------------------------------------------------------------------- #
def write_csv(path: str, rows: list[dict]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["url", "property_type", "category", "origin"])
        writer.writeheader()
        writer.writerows(rows)


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="Collect Immovlan house/apartment property URLs to CSV.")
    ap.add_argument("--config", help="path to a JSON config file")
    ap.add_argument("--output", help="output CSV path (overrides config)")
    ap.add_argument("--limit", type=int, help="stop after N unique property URLs (testing)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.output:
        cfg["output_csv"] = args.output
    if args.limit is not None:
        cfg["limit"] = args.limit

    fetcher = Fetcher(cfg)

    log.info("Collecting property & project URLs from search pages…")
    properties, projects = collect(cfg, fetcher)
    log.info("Search done: %d individual properties, %d projects found",
             len(properties), len(projects))

    if projects:
        log.info("Expanding %d projects into their individual units…", len(projects))
        units = expand_projects(cfg, fetcher, projects, set(properties))
        log.info("Projects contributed %d additional unit URLs", len(units))
    else:
        units = {}

    rows = []
    for url, ptype in sorted(properties.items()):
        rows.append({"url": url, "property_type": ptype,
                     "category": classify(ptype), "origin": "search"})
    for url, ptype in sorted(units.items()):
        rows.append({"url": url, "property_type": ptype,
                     "category": classify(ptype), "origin": "project"})

    write_csv(cfg["output_csv"], rows)
    log.info("Wrote %d unique property URLs -> %s "
             "(%d from search, %d from projects)",
             len(rows), cfg["output_csv"], len(properties), len(units))


if __name__ == "__main__":
    main()