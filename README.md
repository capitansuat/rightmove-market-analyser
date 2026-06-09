# UK Property Market Analyser

Open-source MCP server and REST API for UK property market analysis. Pulls live data from Rightmove, HM Land Registry, Environment Agency, and Police UK — all from free public sources, no paid subscriptions needed.

## What it does

- **Rightmove listings** — active and STC properties with days on market, price, bedrooms, and property type
- **Rightmove sold prices** — completed sales from house-prices pages
- **Land Registry sales** — official Price Paid Data via SPARQL, with monthly volume breakdown by property type
- **UK House Price Index** — regional price trends, annual change %, and sales volume over time
- **Flood risk** — Environment Agency monitoring stations, active warnings, and flood areas near any point
- **Crime data** — Police UK street-level crime aggregated by category
- **EPC certificates** — energy rating, floor area, heating, walls, roof (free API key required)

## MCP Server (recommended)

The MCP server lets AI tools like Claude and ChatGPT call these data sources directly as tools.

### Setup

```bash
git clone https://github.com/capitansuat/rightmove-market-analyser.git
cd rightmove-market-analyser
pip install -r requirements.txt "mcp[cli]" httpx
```

### Claude Desktop config

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "uk-property": {
      "command": "python3",
      "args": ["/path/to/rightmove-market-analyser/server.py"]
    }
  }
}
```

### Available tools

| Tool | Source | Data |
|---|---|---|
| `uk_market_search` | Rightmove | Active + STC listings, DOM, price |
| `uk_sold_prices` | Rightmove | Recent completed sales (house-prices) |
| `uk_land_registry` | HM Land Registry | PPD sales, monthly volume by type |
| `uk_hpi` | HM Land Registry | Price index, annual change, sales volume |
| `uk_flood_risk` | Environment Agency | Monitoring stations, warnings, flood areas |
| `uk_crime` | Police UK | Crime counts by category near a point |
| `uk_listed_buildings` | Historic England | Listed buildings, grades (NHLE) |
| `uk_air_quality` | DEFRA UK-AIR | Latest NO2/PM10/PM2.5/ozone readings |
| `uk_schools` | OpenStreetMap | Schools and types near a point |
| `uk_ofsted` | Ofsted | School inspection ratings and dates |

## REST API (alternative)

For direct HTTP access without MCP:

```bash
python -m uvicorn api.main:app --reload
```

| Endpoint | Source | Key | Data |
|---|---|---|---|
| `/api/market` | Rightmove | No | Active + STC listings, DOM, price |
| `/api/sold` | Rightmove | No | Completed sales (house-prices) |
| `/api/land-registry` | HM Land Registry | No | PPD sales, monthly volume by type |
| `/api/hpi` | HM Land Registry | No | Price index, annual change, sales volume |
| `/api/flood` | Environment Agency | No | Monitoring stations, warnings, flood areas |
| `/api/crime` | Police UK | No | Crime counts by category near a point |
| `/api/epc` | EPC Register | Free | Energy rating, floor area, construction |


## Data sources

All data comes from free, public sources:

| Source | What | Auth |
|---|---|---|
| [Rightmove](https://www.rightmove.co.uk) | Listings, sold prices | None |
| [HM Land Registry](https://landregistry.data.gov.uk) | Price Paid Data, House Price Index | None |
| [Environment Agency](https://environment.data.gov.uk) | Flood monitoring, warnings | None |
| [Police UK](https://data.police.uk) | Street-level crime | None |
| [Historic England](https://opendata-historicengland.hub.arcgis.com) | Listed buildings (NHLE) | None |
| [DEFRA UK-AIR](https://uk-air.defra.gov.uk) | Air quality monitoring | None |
| [OpenStreetMap](https://overpass-api.de) | Schools | None |
| [Ofsted](https://reports.ofsted.gov.uk) | School inspection ratings | None |
| [EPC Register](https://epc.opendatacommunities.org) | Energy certificates | Free key |

## Notes

- Data is fetched live each time — no caching or database
- Rightmove scraping uses a polite 0.4s delay between pages
- Days on Market is calculated from Rightmove's `firstVisibleDate` field
- Land Registry SPARQL queries use `ukhpi:refMonth` for HPI and `lrppi:` prefix for PPD

## Project structure

```
rightmove-market-analyser/
├── server.py            # MCP server (recommended)
├── api/
│   └── main.py          # FastAPI REST API (alternative)
├── frontend/
│   └── index.html       # Web UI (optional)
├── requirements.txt
├── .env.example
└── README.md
```

## Licence

MIT
