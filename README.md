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
│   └── sale_properties.csv
└── src/
    ├── url_fetcher.py
    ├── url_to_html.py
    └── scraping_html.py
```

## 📓 Dataset output 🗒️ 

The scraper outputs a CSV file containing the consolidated real estate listings. 
Note that the included [`sale_properties.csv`](./data/sale_properties.csv) was generated on 2026-06-17 and future runs will yield different results.

- Format: CSV
- Rows: 15,140
- Columns: 49
- Data dictionary: [`data_dictionary.txt`](./data/data_dictionary.txt)

### Main column groups

- Identity & Provenance: property id, URL, source, ...
- Classification: property type, transaction type, ...
- Price: price, VAT, ...
- Location: latitude, longitude, ...
- Property details: bedrooms, bathrooms, ...
- Energy: heating type, EPC/PEB information, ...
- Outdoor & Extra : garden, garage, ...

For the full list of fields and their meaning, see [`data_dictionary.txt`](./data/data_dictionary.txt).  

## 💻 Installation & Usage 🔨

1. Clone the repository to your local machine.  

2. Check and install the required packages (_requirements.txt_)  

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


## 👥 Contributors 👥

- [Mahalakshmi Palanivel](https://github.com/mahalakshmip1604)
- [Alex Kim](https://github.com/alexandrejeonghwankim-lab)
- [Stephane van der Aa](https://github.com/stepvda)
- [Sooyoung Lee](https://github.com/patoobyte)


## 📆 Timeline 📆

The project was completed over 5 days.


## 🐈‍⬛ Personal Situation 🐈‍⬛

The project was done as part of the AI & Data Science bootcamp at [BeCode](https://becode.org)
