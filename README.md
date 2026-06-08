# Rightmove Market Analyser

A local web tool for UK estate agents to analyse property market activity from Rightmove — days on market, STC rates, price distribution — for any postcode and radius.

## What it does

- Fetches all listings (active + STC) from Rightmove for a given postcode and radius
- Shows key market stats: total listings, STC rate, median days on market
- Full sortable/filterable table with DOM bar charts
- Links directly to each Rightmove listing
- No Rightmove account or API key needed

## Requirements

- Python 3.10+
- pip or uv

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/rightmove-market-analyser.git
cd rightmove-market-analyser
pip install -r requirements.txt
```

Or with uv:

```bash
git clone https://github.com/YOUR_USERNAME/rightmove-market-analyser.git
cd rightmove-market-analyser
uv pip install -r requirements.txt
```

## Usage

```bash
python -m uvicorn api.main:app --reload
```

Then open your browser at:

```
http://localhost:8000
```

Enter a postcode (e.g. `SW1A 2AA`), set radius and max price, click **Analyse**.

## Notes

- Data is fetched live from Rightmove each time you search — no caching
- Rightmove rate-limits aggressive scraping; the tool uses a polite 0.4s delay between pages
- DOM (Days on Market) is calculated from the `firstVisibleDate` field Rightmove embeds in each listing
- STC detection is based on `displayStatus` field containing "STC" or "SOLD"

## Project structure

```
rightmove-market-analyser/
├── api/
│   └── main.py          # FastAPI backend + Rightmove scraper
├── frontend/
│   └── index.html       # Single-page web UI
├── requirements.txt
└── README.md
```

## Licence

MIT
