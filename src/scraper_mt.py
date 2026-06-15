#!/usr/bin/env python3
"""scraper_mt.py — multi-threaded version of scraper.py.

Same behaviour as ``scraper.py`` (read URLs from ``data/property_urls.csv``,
download each page to ``data/html/<property_id>.html``, decide sale vs. rent
from the page, scrape the dictionary fields and append the row to
``data/forsale.csv`` / ``data/torent.csv``, resumable across restarts) — but it
fetches and parses many listings concurrently with a bounded thread pool.

The actual parsing, output schema and HTTP logic are imported unchanged from
``scraper.py``; this module only adds the concurrency layer:

  * a fixed pool of worker threads (``--workers``, default 8, capped at 16);
  * one HTTP session per thread (``requests.Session`` is not thread-safe);
  * a lock per output CSV so concurrent appends never interleave;
  * already-scraped URLs are filtered out up front, so no shared progress
    state has to be mutated while threads run.

Why 8 by default: scraping is I/O-bound (each thread spends almost all its time
waiting on the network), so the number of CPU cores is not the limit. The cap
is instead about being polite to a single host — 8 parallel connections with a
small per-request delay keeps the crawl fast (a handful of requests per second)
without hammering immovlan.be or getting throttled/blocked. Raise ``--workers``
at your own risk; it is clamped to MAX_WORKERS.

Usage:
    python scraper_mt.py                 # 8 threads, resuming
    python scraper_mt.py --workers 12    # more parallelism (<= 16)
    python scraper_mt.py --limit 100     # stop after 100 new listings
"""
from __future__ import annotations

import argparse
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

# Reuse everything from the single-threaded scraper (same folder -> importable
# regardless of the current working directory, since Python puts the script's
# directory on sys.path). Importing it also sets up the shared logging config.
from scraper import (
    COLUMNS,
    FORSALE_CSV,
    HTML_DIR,
    INPUT_CSV,
    TORENT_CSV,
    Fetcher,
    load_done_urls,
    parse_html,
)

# Default and hard-cap number of concurrent scraper threads.
DEFAULT_WORKERS = 8
MAX_WORKERS = 16

log = logging.getLogger("scraper_mt")

# --------------------------------------------------------------------------- #
# Per-thread HTTP session                                                     #
# --------------------------------------------------------------------------- #
_local = threading.local()


def get_fetcher() -> Fetcher:
    """Return this thread's own Fetcher (creating it on first use)."""
    fetcher = getattr(_local, "fetcher", None)
    if fetcher is None:
        fetcher = _local.fetcher = Fetcher()
    return fetcher


# --------------------------------------------------------------------------- #
# Thread-safe output                                                          #
# --------------------------------------------------------------------------- #
_write_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: str) -> threading.Lock:
    with _locks_guard:
        return _write_locks.setdefault(path, threading.Lock())


def append_row(path: str, row: dict) -> None:
    """Append one scraped row; serialised per output file across threads.

    Holding the lock around the existence check + write also closes the race
    where two threads would both decide to write the CSV header.
    """
    out = Path(path)
    with _lock_for(str(out)):
        out.parent.mkdir(parents=True, exist_ok=True)
        write_header = not out.exists() or out.stat().st_size == 0
        pd.DataFrame([row], columns=COLUMNS).to_csv(
            out, mode="a", header=write_header, index=False
        )


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="Multi-threaded Immovlan detail-page scraper.")
    ap.add_argument("--input", default=str(INPUT_CSV), help="input CSV with a 'url' column")
    ap.add_argument("--html-dir", default=str(HTML_DIR), help="where to store downloaded HTML")
    ap.add_argument("--forsale-csv", default=str(FORSALE_CSV), help="output CSV for sale listings")
    ap.add_argument("--torent-csv", default=str(TORENT_CSV), help="output CSV for rent listings")
    ap.add_argument("--limit", type=int, default=0, help="stop after N newly scraped listings (0 = all)")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help=f"parallel scraper threads (clamped to 1..{MAX_WORKERS})")
    args = ap.parse_args()

    workers = max(1, min(args.workers, MAX_WORKERS))

    urls = pd.read_csv(args.input)["url"].dropna().astype(str).tolist()
    done = load_done_urls(args.forsale_csv, args.torent_csv)
    todo = [u for u in urls if u not in done]
    if args.limit:
        todo = todo[: args.limit]

    total = len(todo)
    log.info("Input: %d URLs | already scraped: %d | to scrape: %d | workers: %d",
             len(urls), len(done), total, workers)

    html_dir = Path(args.html_dir)
    html_dir.mkdir(parents=True, exist_ok=True)

    # Shared progress/counters, guarded by a lock (also serialises stdout).
    state = {"done": 0, "scraped": 0, "failed": 0}
    state_lock = threading.Lock()

    def record(result: str, url: str, reason: str | None = None) -> None:
        with state_lock:
            state["done"] += 1
            state[result] += 1
            if reason:
                print()  # break off the live progress line before logging
                log.warning("%s: %s", reason, url)
            print(f"\rscraping {state['done']}/{total}", end="", flush=True)

    def worker(url: str) -> None:
        fetcher = get_fetcher()
        try:
            html = fetcher.get(url)
            if not html:
                record("failed", url, "Failed to download")
                return
            row = parse_html(html, url)
            if not row:
                record("failed", url, "Could not parse listing")
                return

            # Save the HTML snapshot named by the discovered property_id.
            html_path = html_dir / f"{row['property_id']}.html"
            html_path.write_text(html, encoding="utf-8")
            row["html_path"] = str(html_path)

            target = args.forsale_csv if row["transaction_type"] == "for-sale" else args.torent_csv
            append_row(target, row)
            record("scraped", url)
        finally:
            fetcher.sleep()

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scrape") as pool:
        futures = [pool.submit(worker, u) for u in todo]
        try:
            for _ in as_completed(futures):
                pass
        except KeyboardInterrupt:
            print()
            log.warning("Interrupted — cancelling pending tasks (scraped rows are saved)…")
            pool.shutdown(wait=False, cancel_futures=True)
            raise

    print()  # finish the progress line
    log.info("Done. %d scraped, %d failed (of %d).",
             state["scraped"], state["failed"], total)


if __name__ == "__main__":
    main()
