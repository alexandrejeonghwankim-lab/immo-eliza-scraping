# Geographic enrichment & mapping

Two scripts turn the raw scraped listings into a geographically enriched
dataset and an interactive map:

| Script | Role |
| --- | --- |
| [`postprocessing.py`](postprocessing.py) | Fill in coordinates and tag each listing with its nearest major city. |
| [`htmlmap.py`](htmlmap.py) | Render every listing as a coloured dot on an interactive map of Belgium. |

They run as a pipeline on the two scraper outputs, `data/forsale.csv` and
`data/torent.csv`:

```
scraper → forsale.csv / torent.csv → postprocessing.py → htmlmap.py → property_map.html
```

---

## 1. `postprocessing.py` — coordinates & nearest city

### What it does

For every property record in the two CSV files it:

1. **Fills in `latitude` / `longitude`** for any row still missing them, by
   geocoding the street address through an external API.
2. **Adds two columns** describing the closest major city:
   - `nearest_city` — the name of the closest city of **more than 50 000
     inhabitants**;
   - `nearest_city_distance_km` — the straight-line distance to it, in
     kilometres.

The `latitude` / `longitude` columns already exist in the scraper output, so
they are populated *in place* rather than recreated. The two `nearest_city*`
columns are appended to the header if they are not present yet.

### Geocoding (external API)

| Aspect | Value |
| --- | --- |
| Service | OpenStreetMap **Nominatim** (`https://nominatim.openstreetmap.org/search`) — free, no API key |
| Query | Structured search built from `house_number` + `street`, `postal_code`, `locality`, restricted to Belgium (`countrycodes=be`) |
| Fallback | If the full street address returns nothing, it retries with only the postcode / locality (town centroid) |
| Rate limit | One request every `1.1 s` (Nominatim policy is ≤ 1 req/s) |
| Politeness | Identifying `User-Agent`, `30 s` timeout, up to `3` retries with back-off on errors / HTTP 429 |
| Caching | Identical address queries are answered from an in-memory cache, so duplicate addresses cost a single API call |

### Nearest-city tagging

Once a row has coordinates, the script computes the distance from the property
to **every** city in the embedded [major-cities table](#major-cities-reference-table)
and keeps the closest one. The result is written to `nearest_city` and
`nearest_city_distance_km`.

#### Distance formula — Haversine

Distances use the **Haversine** great-circle formula, which gives the
shortest distance over the Earth's surface between two points defined by
latitude/longitude.

For a property at $(\varphi_1, \lambda_1)$ and a city at $(\varphi_2, \lambda_2)$,
with all angles converted to **radians** and

$$\Delta\varphi = \varphi_2 - \varphi_1, \qquad \Delta\lambda = \lambda_2 - \lambda_1$$

the distance $d$ is:

$$a = \sin^2\!\left(\frac{\Delta\varphi}{2}\right) + \cos\varphi_1 \cdot \cos\varphi_2 \cdot \sin^2\!\left(\frac{\Delta\lambda}{2}\right)$$

$$d = 2R \cdot \arcsin\!\left(\sqrt{a}\right)$$

where **$R = 6371.0088$ km** is the mean radius of the Earth.
(The `2R\arcsin(\sqrt{a})` form is equivalent to the more commonly written
`2R\,\operatorname{atan2}(\sqrt{a}, \sqrt{1-a})`.)

In code this is:

```python
def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0088  # mean Earth radius (km)
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
```

The Haversine distance is a great-circle approximation on a perfect sphere; for
distances within Belgium the error versus the true ellipsoidal distance is well
under 0.5 %, which is far below the precision needed here.

### Resumability

The run is **resumable** — the CSV file itself is the state:

- Each record is only processed for the columns it is still missing. A row that
  already has coordinates is never re-geocoded; a row that already has a
  `nearest_city` is not recomputed.
- Progress is check-pointed to disk every `20` geocodes (constant
  `CHECKPOINT_EVERY`) and again on `Ctrl-C` or any error, using an atomic
  temp-file-then-replace write so the CSV is never left half-written.
- An interrupted run therefore simply continues where it stopped on the next
  invocation, and already-enriched rows incur **no** API calls.
- Rows with neither coordinates nor a usable address can never be completed;
  they are skipped each run and cost nothing.

### Progress output

While running it prints a **single, continuously overwritten** terminal line
(carriage-return based), e.g.:

```
[forsale.csv] 8421/14951 ( 56.3%)  geocoded=2  cities_tagged=8419  geocoding…
```

A per-file and grand-total summary is printed when it finishes.

### Command-line usage

```bash
python postprocessing.py                    # process the two default CSVs
python postprocessing.py --files data/a.csv # process specific file(s)
python postprocessing.py --limit 50         # cap geocoding API calls per file (testing)
python postprocessing.py --force            # recompute the nearest-city columns for every row
```

---

## Major cities reference table

These are the cities used as targets for the nearest-city calculation: the
Belgian municipalities above ~50 000 inhabitants (the 18 Brussels-Capital
sub-municipalities collapsed into a single **Brussels** entry to avoid answers
like "nearest big city: Forest"), plus the large cities **just across the
border** so that frontier properties get a correct answer. Coordinates are
approximate town-centre centroids in decimal degrees (WGS84). Editing this list
in `postprocessing.py` automatically changes the distances.

### Belgian cities (> ~50 000 inhabitants)

| City | Latitude | Longitude |
| --- | --- | --- |
| Antwerp | 51.2194 | 4.4025 |
| Ghent | 51.0543 | 3.7174 |
| Charleroi | 50.4108 | 4.4446 |
| Liège | 50.6451 | 5.5734 |
| Brussels | 50.8467 | 4.3499 |
| Bruges | 51.2093 | 3.2247 |
| Namur | 50.4674 | 4.8719 |
| Leuven | 50.8798 | 4.7005 |
| Mons | 50.4542 | 3.9563 |
| Mechelen | 51.0281 | 4.4801 |
| Aalst | 50.9403 | 4.0364 |
| La Louvière | 50.4854 | 4.1875 |
| Kortrijk | 50.8281 | 3.2649 |
| Hasselt | 50.9307 | 5.3378 |
| Sint-Niklaas | 51.1652 | 4.1437 |
| Ostend | 51.2247 | 2.9156 |
| Tournai | 50.6071 | 3.3892 |
| Genk | 50.9650 | 5.5006 |
| Seraing | 50.5836 | 5.5006 |
| Roeselare | 50.9469 | 3.1228 |
| Mouscron | 50.7440 | 3.2069 |
| Verviers | 50.5911 | 5.8625 |

### Large border cities (for frontier properties)

| City | Country | Latitude | Longitude |
| --- | --- | --- | --- |
| Lille | France | 50.6292 | 3.0573 |
| Roubaix | France | 50.6942 | 3.1746 |
| Tourcoing | France | 50.7236 | 3.1610 |
| Dunkirk | France | 51.0344 | 2.3768 |
| Maastricht | Netherlands | 50.8514 | 5.6910 |
| Eindhoven | Netherlands | 51.4416 | 5.4697 |
| Aachen | Germany | 50.7753 | 6.0839 |
| Luxembourg | Luxembourg | 49.6116 | 6.1319 |

---

## 2. `htmlmap.py` — interactive map

### What it does

Reads the enriched CSVs and writes a single, self-contained HTML file
(`data/property_map.html` by default) with an interactive map of Belgium
showing **one small dot per property** at its `latitude` / `longitude`:

- **Red dots** — properties **for sale**;
- **Yellow dots** — properties **for rent**.

The for-sale / for-rent class is taken from each row's `transaction_type`
column, falling back to the file name (e.g. `torent.csv`) when absent.

### Map features

- Built on **Leaflet** (library and map tiles loaded from public CDNs), using a
  **canvas renderer** so it comfortably draws the ~20 000 dots.
- A light **CARTO Positron** basemap so the coloured dots stand out; rent dots
  are drawn on top of sale dots so the rarer yellow ones stay visible.
- Each dot has a **popup** with its price, locality, type and a link to the
  listing (can be disabled to shrink the file).
- A **layer control** to toggle for-sale / for-rent on and off, and a **legend**
  showing the per-class counts.
- The view **auto-fits** to the plotted points (falling back to Belgium's
  bounding box if there are none).

### Coordinate sanitization

Some source rows have their latitude and longitude **swapped**, which would
otherwise stretch the auto-fit out to the whole globe and make the map look
empty. On load, each coordinate pair is checked against a plausible Belgian
bounding box (`PLOT_BOUNDS = (lat 49.3–51.7, lon 2.3–6.6)`):

- inside the box → kept as-is;
- outside, but the **swap** is inside → silently un-swapped and kept;
- otherwise → dropped.

The script reports how many rows it recovered, dropped, or skipped (no
coordinates) when it finishes.

### Command-line usage

```bash
python htmlmap.py                          # -> data/property_map.html
python htmlmap.py --output map.html        # custom output path
python htmlmap.py --files data/forsale.csv # plot specific file(s)
python htmlmap.py --radius 4               # bigger dots
python htmlmap.py --no-popups              # omit popups (smaller, faster file)
python htmlmap.py --title "My map"         # custom page title
```

Opening the resulting HTML needs an internet connection (for the Leaflet
library and the map tiles); the property data itself is embedded in the file.

---

## Typical workflow

```bash
# 1. enrich the scraped CSVs with coordinates + nearest city
python src/postprocessing.py

# 2. render the interactive map
python src/htmlmap.py

# 3. open it
open data/property_map.html
```

### Dependencies

- `postprocessing.py` requires **`requests`** (for the geocoding API).
- `htmlmap.py` uses only the Python **standard library**.
