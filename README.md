# immo-eliza-scraper 🇧🇪

🧰 Built with 🧰  
  ![Python](https://img.shields.io/badge/Python-3.x-blue?logo=python&logoColor=white)

---


## 🇧🇪 Description 🇧🇪

A web scraping project that collects Belgian real estate property data from [Immovlan](https://immovlan.be/).

The project focuses on building a dataset of Belgian houses and apartments, for sale.

The scraper works in __3 distinct steps__ that are executed separately:

1. Collects property listing URLs and stores them in a CSV file.
   The collected URLs include:
   - Houses
   - Apartments
   - Individual units from project listings
3. Download raw HTML from the URLs.
4. Extract data from downloaded HTML files.


## 🗃️ Structure 🗃️

```
./immo-eliza-scraping/
├── .gitignore
├── README.md
├── requirements.txt
├── data/
│   ├── pages/
│   │   ├── rah80542.html.gz
│   │   ├── rai26618.html.gz
│   │   ├── ...
│   ├── data_dictionary.txt
│   ├── property_urls.csv
│   ├── sale_properties.csv
│   ├── sale_properties_mapdata.csv   # enriched: lat/lon + nearest city
│   └── property_map2.html            # interactive Leaflet map
├── dev/
│   └── map.py                        # geocode + nearest-city enrichment & map
└── src/
    ├── url_fetcher.py
    ├── url_to_html.py
    └── scraping_html.py
```


## 💻 Installation & Usage 🔨

1. Clone the repository to your local machine.  

2. Check and intall the required packages (_requirements.txt_)  

3. Run the __URL fetcher__ with  

	```
	python src/url_fetcher.py
	```

	or

	```
	python3 src/url_fetcher.py
	```

	The URL fetcher will:
    - Connects to ImmoVlan search pages
    - Searches for houses and apartments (both sale and rent)
	    - In order to get at least 10,000 data entries, search is split by price ranges.
	- Goes through each search result page and extracts individual property URLs
	- Detects project listings, opens them and collect individual units' URLs
	- Stores the final URLs list in a `.csv` file (`data/property_urls.csv`)

4. Run the __HTML downloader__ with

	```
	python src/url_to_html.py
	```
	or
	```
	python3 src/url_to_html.py
	```

	The URL to HTML script will:
    - Read the property URLs from the `.csv` file (`data/property_urls.csv`)
    - Connect to each individual ImmoVlan property page
    - Download the raw HTML content of each page
    - Save each property page as a separate compressed `.html.gz` file (`data/pages/propertyID.html.gz`)
        - The compressing saves up memory space

5. Run the __data scraper__ with

	```
	python src/scraping_html.py
	```
	or
	```
	python3 src/scraping_html.py
	```
	The HTML scraping script will:
    - Read the saved raw HTML files
    - Extract useful property information from each page
    - Collect details such as price, location, property type, surface, rooms, and other available features
    - Clean and organize the extracted information
        - This prepares the data for analysis and machine learning
    - Combine all scraped property data into one dataset
    - Store the final dataset in a `.csv` file (`data/sale_properties.csv`)


## 🗺️ Geospatial Enrichment & Mapping 🗺️

Beyond the three scraping steps, the __`dev/map.py`__ script turns the scraped
dataset into an interactive geographic map. It runs in two passes over the
scraper's CSV (e.g. `data/sale_properties.csv`):

### 1. Enrich the CSV (in place)

- __Geocoding__ — for every listing still missing `latitude` / `longitude`, the
  script queries the free [OpenStreetMap Nominatim](https://nominatim.openstreetmap.org/)
  API with the property's `postal_code` + `locality` (the town centroid, since
  the dataset carries no street). Calls are rate-limited to ≤ 1 request/second,
  cached per distinct postcode, and the CSV is checkpointed to disk every 20
  geocodes — so the step is fully __resumable__.
- __Nearest big city__ — two new columns, `nearest_city` and
  `nearest_city_distance_km`, are appended. For each property the script finds
  the closest city of __> 50,000 inhabitants__ (Belgian municipalities, plus the
  large cities just across the French, Dutch, German and Luxembourg borders) and
  records its name and the distance to it.

#### Distance formula — the haversine (great-circle) distance

The distance between a property and a candidate city centroid is the
great-circle distance on a spherical Earth, computed with the __haversine__
formula:

```text
a = sin²(Δφ / 2) + cos(φ₁) · cos(φ₂) · sin²(Δλ / 2)
d = 2 · R · arcsin(√a)
```

where

- `φ₁`, `φ₂` — the latitudes of the property and the city, in __radians__
- `Δφ = φ₂ − φ₁` — difference in latitude (radians)
- `Δλ = λ₂ − λ₁` — difference in longitude (radians)
- `R = 6371.0088 km` — the mean radius of the Earth
- `d` — the resulting distance, in kilometres

The property's `nearest_city` is the one that __minimises `d`__ across the
reference list of big cities, and `nearest_city_distance_km` is that minimum
distance (rounded to two decimals).

### 2. Render the map

The script writes an interactive [Leaflet](https://leafletjs.com/) map to
`data/property_map2.html` — one dot per property, __red for sale__ and
__yellow for rent__, each with a popup showing the price, locality, type and a
link to the listing. Coordinates outside Belgium are dropped and swapped
lat/lon pairs are auto-corrected, so the map stays fitted to the country.

### Usage

```bash
# work on a copy so the source CSV stays untouched, then enrich + map it
cp data/sale_properties.csv data/sale_properties_mapdata.csv
python dev/map.py --csv data/sale_properties_mapdata.csv --output data/property_map2.html
```

Flags: `--skip-map` (enrich only), `--skip-enrich` (map only), `--limit N`
(cap geocoding calls for a quick test). Requires `requests`.

Outputs: `data/sale_properties_mapdata.csv` (the input CSV extended with the
coordinate + nearest-city columns) and `data/property_map2.html` (the map).


## 👥 Contributors 👥

- [Mahalakshmi Palanivel](https://github.com/mahalakshmip1604)
- [Alex Kim](https://github.com/alexandrejeonghwankim-lab)
- [Stephane van der Aa](https://github.com/stepvda)
- [Sooyoung Lee](https://github.com/patoobyte)


## 📆 Timeline 📆

The project was completed over 5 days.


## 🐈‍⬛ Personal Situation 🐈‍⬛

The project was done as part of the AI & Data Science bootcamp at [BeCode](https://becode.org)
