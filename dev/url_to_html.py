
import pandas as pd 
import requests
import time
import os
from concurrent.futures import ThreadPoolExecutor

#  load CSV
df = pd.read_csv("projects/03-immoeliza-scraping/property_urls.csv")
urls = df["url"]

#  session
session = requests.Session()

headers = {
    "User-Agent": "Mozilla/5.0"
}

#  create folder
os.makedirs("for-sale", exist_ok=True)

url_list = urls[:200] # test batch

total = len(url_list)
saved_count = 0 # global counter

def fetch_html(url):
    try:
        response = session.get(url, headers=headers)

        if response.status_code == 200:
            print(f" Success: {url}")

            property_id = url.split("/")[-1]
            file_path = os.path.join("for-sale", f"{property_id}.html")

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(response.text)

            saved_count +=1

            if saved_count % 50 == 0:
                remaining = total - saved_count
                print(f"Saved: {saved_count} | Reamaining: {remaining}") 

            

        else:
            print(f" Failed: {url} ({response.status_code})")

    except Exception as e:
        print(f" Error: {url} → {e}")

#  test with 20


with ThreadPoolExecutor(max_workers=5) as executor:
    executor.map(fetch_html, url_list)
