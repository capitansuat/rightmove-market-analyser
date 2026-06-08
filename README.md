# Rightmove Market Analyser

A local web tool for UK estate agents to analyse property market activity from Rightmove — days on market, STC rates, price distribution — for any postcode and radius.

## What it does

- Fetches all active and STC listings from Rightmove for a given postcode and radius
- Shows completed sales from Rightmove house-prices pages
- Pulls official sold data from HM Land Registry (SPARQL)
- Displays UK House Price Index trends by region (average price, annual change, sales volume)
- Looks up Energy Performance Certificates (EPC rating, floor area, heating, walls, roof)
- Key market stats: total listings, STC rate, median days on market
- Full sortable/filterable table with DOM bar charts
- Links directly to each Rightmove listing
- No Rightmove account needed (EPC requires free API key)

## API Endpoints

| Endpoint | Source | Data |
|---|---|---|
| `/api/market` | Rightmove | Active + STC listings, DOM, price |
| `/api/sold` | Rightmove | Completed sales (house-prices) |
| `/api/land-registry` | HM Land Registry | PPD sales, monthly volume by type |
| `/api/hpi` | HM Land Registry | Price index, annual change, sales volume |
| `/api/epc` | EPC Register | Energy rating, floor area, construction |

## Requirements

- Python 3.10+
- pip or uv

## Installation

```bash
git clone https://github.com/capitansuat/rightmove-market-analyser.git
cd rightmove-market-analyser
pip install -r requirements.txt
```

Or with uv:

```bash
git clone https://github.com/capitansuat/rightmove-market-analyser.git
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

### EPC Setup (optional)

To use the `/api/epc` endpoint, register for a free API key at https://epc.opendatacommunities.org/ and set environment variables:

```bash
export EPC_API_EMAIL=your@email.com
export EPC_API_KEY=your-api-key
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
