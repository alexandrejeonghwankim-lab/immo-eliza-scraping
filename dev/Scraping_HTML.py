import os
import re
import json
import gzip
import pandas as pd
from datetime import datetime

from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

# =====================================================
# CONFIGURATION
# =====================================================

HTML_DIR = "pages"
OUTPUT_CSV = "sale_properties_final.csv"

MAX_WORKERS = 12
BATCH_SIZE = 1000

# =====================================================
# GLOBALS & CONSTANTS
# =====================================================

write_lock = Lock()

CSV_COLUMNS = [
    "property_id", "url", "source", "scrape_date", "posting_date", "html_path",
    "property_type", "category", "transaction_type", "price", "price_per_sqm", 
    "livable_surface", "land_surface", "bedrooms", "showers", "toilets", 
    "number_of_floors", "apartment_floor", "facades", "building_state",
    "build_year", "new_construction", "vat", "currently_leased", "furnished",
    "heating_type", "primary_energy_consumption", "epc", "solar_panels", "air_conditioning",
    "garden", "terrace", "balcony", "garage", "garages", "indoor_parking", "outdoor_parking",
    "cellar", "swimming_pool", "elevator", "flooding_area_type",
    "maintenance_cost", "co_ownership_charges",
    "locality", "postal_code", "province", "region", "latitude", "longitude"
]

# =====================================================
# HELPER UTILITIES
# =====================================================

def safe_float(value):
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def parse_jsonld(soup):
    jsonlds = []
    for tag in soup.select('script[type="application/ld+json"]'):
        txt = tag.get_text(strip=True)
        if not txt:
            continue
        try:
            obj = json.loads(txt)
            if isinstance(obj, list):
                jsonlds.extend(obj)
            else:
                jsonlds.append(obj)
        except json.JSONDecodeError:
            pass
    return jsonlds


def extract_postal_code(text):
    m = re.search(r'\b([1-9]\d{3})\b', text)
    return m.group(1) if m else None


def extract_number(text, label):
    pattern = rf'{label}\s*[:\-]?\s*([\d\.,]+)'
    m = re.search(pattern, text, flags=re.I)
    if not m:
        return None
    return m.group(1).replace(".", "").replace(",", ".")


def extract_binary_presence(text, label):
    """Returns 1 if the label is accompanied by a 'Yes', or explicitly exists in text, else 0."""
    pattern = rf'{label}\s*[:\-]?\s*(Yes|No|Oui|Non|Ja|Nee)'
    m = re.search(pattern, text, flags=re.I)
    if m:
        val = m.group(1).lower()
        if val in ['yes', 'oui', 'ja']:
            return 1
        return 0
    
    # Keyword fallback flag checking
    if re.search(rf'\b{label}\b', text, flags=re.I):
        return 1
    return 0


def get_belgian_geo(postal_code):
    """Maps Belgian postal codes to their respective Provinces and Regions."""
    if not postal_code:
        return None, None
    try:
        pc = int(postal_code)
    except (ValueError, TypeError):
        return None, None

    if 1000 <= pc <= 1299:
        return "Brussels Hoofdstedelijk Gewest", "Brussels"
    elif 1300 <= pc <= 1499:
        return "Waals-Brabant", "Wallonia"
    elif 1500 <= pc <= 1999 or 3000 <= pc <= 3499:
        return "Vlaams-Brabant", "Flanders"
    elif 2000 <= pc <= 2999:
        return "Antwerpen", "Flanders"
    elif 3500 <= pc <= 3999:
        return "Limburg", "Flanders"
    elif 4000 <= pc <= 4999:
        return "Luik", "Wallonia"
    elif 5000 <= pc <= 5999:
        return "Namen", "Wallonia"
    elif 6000 <= pc <= 6599 or 7000 <= pc <= 7999:
        return "Henegouwen", "Wallonia"
    elif 6600 <= pc <= 6999:
        return "Luxemburg", "Wallonia"
    elif 8000 <= pc <= 8999:
        return "West-Vlaanderen", "Flanders"
    elif 9000 <= pc <= 9999:
        return "Oost-Vlaanderen", "Flanders"
    return None, None


def normalize_category(prop_type):
    """Normalizes ImmoVlan sub-types into 'house' or 'apartment' categories."""
    if not prop_type:
        return None
    pt_lower = prop_type.lower()
    
    house_types = {"house", "villa", "residence", "bproperty", "chalet", "castle", "countryhouse"}
    apt_types = {"apartment", "duplex", "penthouse", "studio", "flat", "loft"}
    
    if any(ht in pt_lower for ht in house_types):
        return "house"
    if any(at in pt_lower for at in apt_types):
        return "apartment"
    return None


# =====================================================
# PROPERTY DATA EXTRACTOR
# =====================================================

def extract_property(filepath):
    property_id = os.path.basename(filepath).replace(".html.gz", "")

    try:
        with gzip.open(filepath, "rt", encoding="utf-8", errors="ignore") as f:
            html = f.read()
    except Exception as e:
        print(f"Failed to read file {filepath}: {e}")
        return None

    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)
    jsonlds = parse_jsonld(soup)

    # Base structural dictionary mapped perfectly to target schema
    data = {
        "property_id": property_id,
        "url": None,
        "source": "ImmoVlan",
        "scrape_date": datetime.today().strftime('%Y-%m-%d'),
        "posting_date": None,
        "html_path": os.path.relpath(filepath),
        
        "property_type": None,
        "category": None,
        "transaction_type": "for-sale",
        
        "price": None,
        "price_per_sqm": None,
        "livable_surface": None,
        "land_surface": None,
        
        "bedrooms": None,
        "showers": None,
        "toilets": None,
        "number_of_floors": None,
        "apartment_floor": None,
        "facades": None,
        "building_state": None,
        
        "build_year": None,
        "new_construction": None,
        "vat": None,
        "currently_leased": None,
        "furnished": None,
        
        "heating_type": None,
        "primary_energy_consumption": None,
        "epc": None,
        "solar_panels": 0,
        "air_conditioning": 0,
        
        "garden": 0,
        "terrace": 0,
        "balcony": 0,
        "garage": None,
        "garages": None,
        "indoor_parking": None,
        "outdoor_parking": None,
        "cellar": 0,
        "swimming_pool": 0,
        "elevator": 0,
        "flooding_area_type": None,
        
        "maintenance_cost": None,
        "co_ownership_charges": None,
        
        "locality": None,
        "postal_code": None,
        "province": None,
        "region": None,
        
        "latitude": None,
        "longitude": None
    }

    # 1. Metadata Parsing (URL & Posting Date)
    canonical = soup.find("link", rel="canonical")
    og_url = soup.find("meta", property="og:url")
    if canonical and canonical.get("href"):
        data["url"] = canonical.get("href")
    elif og_url and og_url.get("content"):
        data["url"] = og_url.get("content")

    meta_date_selectors = [
        "article:published_time", "publication_date", "og:updated_time", 
        "date_posted", "datePublished", "releaseDate"
    ]
    for meta_name in meta_date_selectors:
        meta_tag = soup.find("meta", property=meta_name) or soup.find("meta", attrs={"name": meta_name})
        if meta_tag and meta_tag.get("content"):
            date_match = re.search(r'(\d{4}-\d{2}-\d{2})', meta_tag.get("content"))
            if date_match:
                data["posting_date"] = date_match.group(1)
                break

    # 2. JSON-LD Structured Data Parsing
    for js in jsonlds:
        if not isinstance(js, dict):
            continue
        obj_type = js.get("@type")

        if obj_type in ("Apartment", "House", "SingleFamilyResidence", "RealEstateAgent"):
            if obj_type in ("Apartment", "House"):
                data["property_type"] = data["property_type"] or obj_type

            data["bedrooms"] = data["bedrooms"] or js.get("numberOfRooms")
            data["showers"] = data["showers"] or js.get("numberOfBathroomsTotal")
            
            floor_size = js.get("floorSize")
            if isinstance(floor_size, dict):
                data["livable_surface"] = data["livable_surface"] or floor_size.get("value")
            elif isinstance(floor_size, (int, float)):
                data["livable_surface"] = data["livable_surface"] or floor_size

            address = js.get("address")
            if isinstance(address, dict):
                data["postal_code"] = data["postal_code"] or address.get("postalCode")
                data["locality"] = data["locality"] or address.get("addressLocality")

        elif obj_type == "SellAction":
            data["price"] = data["price"] or js.get("price")

        elif obj_type == "GeoCoordinates":
            data["latitude"] = data["latitude"] or js.get("latitude")
            data["longitude"] = data["longitude"] or js.get("longitude")

    # 3. Text & URL Fallbacks 
    if data["url"]:
        url_segments = data["url"].lower().split('/')
        for seg in url_segments:
            if seg in ["house", "villa", "apartment", "duplex", "penthouse", "studio", "loft", "residence","student-flat"]:
                data["property_type"] = seg
                break
                
    data["category"] = normalize_category(data["property_type"])

    if not data["livable_surface"]:
        surf = extract_number(text, "Livable surface") or extract_number(text, "Living area")
        if surf:
            data["livable_surface"] = safe_float(surf)

    if not data["locality"] and soup.title:
        title_text = soup.title.get_text()
        loc_match = re.search(r'(?:in|à|te)\s+([A-Z][a-zA-Z\s\-]+)(?:\s*\(|\d{4})', title_text)
        if loc_match:
            data["locality"] = loc_match.group(1).strip()

    data["new_construction"] = "new construction project" in text.lower()
    # -----------------------------------------------------
    # PROPERTY EXTRACTION SPECIFICATIONS
    # -----------------------------------------------------
    # Structural features
    data["land_surface"] = (
        extract_number(text, "Land surface") 
        or extract_number(text, "Surface area of plot")
    )
    data["number_of_floors"] = (
        extract_number(text, "Number of floors") 
        or extract_number(text, "Amount of floors")
    )
    data["apartment_floor"] = extract_number(text, "Floor")
    data["facades"] = (
        extract_number(text, "Number of facades") 
        or extract_number(text, "Facades")
    )

    # Building state mapping selector logic
    state_pattern = r'Building condition|State.*? (Normal|To renovate|New|Good|As new|Just renovated)'
    state_match = re.search(state_pattern, text, flags=re.I)
    if state_match and state_match.group(1):
        data["building_state"] = state_match.group(1).strip()

    # Financial & tenancy variables
    vat_match = re.search(r'VAT applied\??\s*(Yes|No)', text, flags=re.I)
    if vat_match:
        data["vat"] = vat_match.group(1)

    data["cadastral_income"] = extract_number(text, "Cadastral income")
    data["toilets"] = extract_number(text, "Toilets")

    build_year = re.search(r'Build year.*?(\d{4})', text, flags=re.I)
    if build_year:
        data["build_year"] = build_year.group(1)

    leased = re.search(r'Currently leased.*?(Yes|No)', text, flags=re.I)
    if leased:
        data["currently_leased"] = leased.group(1)

    furnished_match = re.search(r'Furnished.*?(Yes|No)', text, flags=re.I)
    if furnished_match:
        data["furnished"] = furnished_match.group(1)

    # Energy features
    heat_pattern = r'Heating system|Heating type.*? (Gas|Electric|Fuel oil|Pellet|Wood)'
    heat_match = re.search(heat_pattern, text, flags=re.I)
    if heat_match and heat_match.group(1):
        data["heating_type"] = heat_match.group(1).strip()

    data["primary_energy_consumption"] = (
        extract_number(text, "Primary energy consumption") 
        or extract_number(text, "Specific energy consumption")
    )

    epc_match = re.search(r'EPC class|Energy class.*? ([A-G\+\-]+)', text, flags=re.I)
    if epc_match and epc_match.group(1):
        data["epc"] = epc_match.group(1).strip()

    # Binary flags logic transformation metrics (Strictly mapping to 1 or 0)
    data["solar_panels"] = extract_binary_presence(text, "Solar panels")
    data["air_conditioning"] = extract_binary_presence(text, "Air conditioning")
    data["garden"] = extract_binary_presence(text, "Garden")
    data["terrace"] = extract_binary_presence(text, "Terrace")
    data["balcony"] = extract_binary_presence(text, "Balcony")
    data["cellar"] = extract_binary_presence(text, "Cellar")
    data["swimming_pool"] = extract_binary_presence(text, "Swimming pool")
    data["elevator"] = extract_binary_presence(text, "Elevator")

    # Parking and storage attributes
    data["garage"] = extract_binary_presence(text, "Garage")
    data["garages"] = extract_number(text, "Number of garages")
    data["indoor_parking"] = (
        extract_number(text, "Indoor parking") 
        or extract_number(text, "Indoor parking spaces")
    )
    data["outdoor_parking"] = (
        extract_number(text, "Outdoor parking") 
        or extract_number(text, "Outdoor parking spaces")
    )

    # Environmental risk classifications
    flood_pattern = r'Flooding area type|Flood zone.*? (no flooding area|possible flood zone|effectively flood zone|sensitive area)'
    flood_match = re.search(flood_pattern, text, flags=re.I)
    if flood_match and flood_match.group(1):
        data["flooding_area_type"] = flood_match.group(1).strip()

    # Asset fees configurations
    data["maintenance_cost"] = extract_number(text, "Maintenance cost")
    data["co_ownership_charges"] = (
        extract_number(text, "Co-ownership charges") 
        or extract_number(text, "Common charges")
    )

    # Postal fallback validation check
    if not data["postal_code"]:
        data["postal_code"] = extract_postal_code(text)

    # -----------------------------------------------------
    # GEOGRAPHIC MAPS & MATH COMPUTATIONS
    # -----------------------------------------------------
    data["province"], data["region"] = get_belgian_geo(data["postal_code"])

    if data["price"] and data["livable_surface"]:
        try:
            data["price_per_sqm"] = round(
                float(data["price"]) / float(data["livable_surface"]), 
                2
            )
        except (ValueError, TypeError):
            pass

    return data


# =====================================================
# BATCH WRITER WORKER
# =====================================================

def write_batch(records):
    """
    Appends a block of processed records to the destination CSV file.
    Uses a thread lock to securely format output without overlapping operations.
    """
    if not records:
        return
        
    with write_lock:
        df_batch = pd.DataFrame(records)
        df_batch.to_csv(
            OUTPUT_CSV, 
            mode="a", 
            index=False, 
            header=False, 
            columns=CSV_COLUMNS
        )
# =====================================================
# RUNTIME EXECUTOR (ROOT LEVEL)
# =====================================================

# 1. Environment Verification
if not os.path.exists(HTML_DIR):
    print(f"CRITICAL ERROR: Target directory '{HTML_DIR}' does not exist.")
    exit(1)


# -----------------------------------------------------
# STEP 2: Collect Target Compressed Snapshots
# -----------------------------------------------------
files = [
    os.path.join(HTML_DIR, f)
    for f in os.listdir(HTML_DIR)
    if f.endswith(".html.gz")
]

total_files = len(files)
print(f"\n[INFO] Discovery Success: Located {total_files:,} target snapshot files.")

if total_files == 0:
    print("[WARN] No processable files found. Terminating execution.")
    exit(0)


# -----------------------------------------------------
# STEP 3: Secure File Layout Initialization
# -----------------------------------------------------
df_empty = pd.DataFrame(columns=CSV_COLUMNS)
df_empty.to_csv(OUTPUT_CSV, index=False)
print(f"[INFO] Schema Initialized: Empty sheet structured at '{OUTPUT_CSV}'")


# -----------------------------------------------------
# STEP 4: Memory Allocations & Telemetry Tracking States
# -----------------------------------------------------
batch = []
processed_count = 0

print(f"[INFO] Initializing worker pool allocation map...")
print(f"-> Thread Configuration: Running with {MAX_WORKERS} parallel processing workers.")
print(f"-> Active RAM Management: Committing datasets in chunks of {BATCH_SIZE:,} elements.")
print("-" * 70)


# -----------------------------------------------------
# STEP 5: Concurrent Extraction Execution Engine
# -----------------------------------------------------
with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    
    # Map tasks across the concurrent scheduler infrastructure
    futures = {
        executor.submit(extract_property, file): file 
        for file in files
    }

    # Stream out datasets dynamically as workers conclude their updates
    for future in as_completed(futures):
        processed_count += 1
        file_path = futures[future]
        
        try:
            result = future.result()
            if result:
                batch.append(result)
        except Exception as e:
            print(f"\n[ERROR] Thread failure encountered on workspace file '{file_path}': {e}")

        # Flush block arrays directly to the CSV to protect physical RAM ceilings
        if len(batch) >= BATCH_SIZE:
            write_batch(batch)
            batch = []

        # Display real-time progress status bars
        if processed_count % 500 == 0 or processed_count == total_files:
            print(f" [*] Processing Tracker: Finished {processed_count:,} / {total_files:,} file snapshots...")


# -----------------------------------------------------
# STEP 6: Final Residual Cache Flush
# -----------------------------------------------------
if batch:
    write_batch(batch)

print("-" * 70)
print(f" SUCCESS: Multi-Threaded Scraping Pipeline Complete!")
print(f" Target dataset destination generated -> {OUTPUT_CSV}\n")

