
import pandas as pd 
import requests
import time
import os
from concurrent.futures import ThreadPoolExecutor
import gzip

#  load CSV
df = pd.read_csv("property_urls.csv")
urls = df["url"]

#  session
session = requests.Session()

headers = {
    "User-Agent": "Mozilla/5.0"
}

#  create folder
os.makedirs("for-sale", exist_ok=True)

url_list = urls[:] 

total = len(url_list)

def fetch_html(url):
    try:
        response = session.get(url, headers=headers)

        if response.status_code == 200:
            print(f" Success: {url}")

            property_id = url.split("/")[-1]
            file_path = os.path.join("for-sale", f"{property_id}.html.gz")

            with gzip.open(file_path, "wt", encoding="utf-8") as f:
                f.write(response.text)        
            
        else:
            print(f" Failed: {url} ({response.status_code})")

    except Exception as e:
        print(f" Error: {url} → {e}")

with ThreadPoolExecutor(max_workers=5) as executor:
    executor.map(fetch_html, url_list)