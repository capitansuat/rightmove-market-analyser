"""UK Property Market Analyser — MCP Server.

Provides tools for Rightmove listings, Land Registry sales data,
UK House Price Index, flood risk, crime stats, and EPC certificates.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

mcp = FastMCP("uk_property_mcp")

# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}
SPARQL_ENDPOINT = "https://landregistry.data.gov.uk/landregistry/query"
POLICE_BASE = "https://data.police.uk/api"
EA_BASE = "https://environment.data.gov.uk/flood-monitoring"
EPC_BASE = "https://epc.opendatacommunities.org/api/v1"
PAGE_DELAY = 0.4



# ---------------------------------------------------------------------------
# Rightmove helpers
# ---------------------------------------------------------------------------

def _lookup_postcode(postcode: str) -> str:
    """Resolve a postcode to a Rightmove locationIdentifier."""
    url = f"https://los.rightmove.co.uk/typeahead?query={postcode.replace(' ', '+')}"
    r = httpx.get(url, headers=DEFAULT_HEADERS, timeout=10)
    r.raise_for_status()
    data = r.json()
    suggestions = data.get("typeAheadSuggestions", [])
    if not suggestions:
        raise ValueError(f"No Rightmove location found for '{postcode}'")
    norm = suggestions[0].get("normalisedSearchTerm", "")
    return norm


def _scrape_market(postcode: str, radius: float, max_price: int) -> dict:
    """Scrape Rightmove search results and return structured data."""
    location_id = _lookup_postcode(postcode)
    all_props: list[dict] = []
    today = datetime.now(tz=timezone.utc)
    index = 0

    while True:
        params = {
            "locationIdentifier": location_id,
            "maxPrice": max_price,
            "radius": radius,
            "sortType": 6,
            "propertyTypes": "",
            "includeSSTC": "true",
            "mustHave": "",
            "dontShow": "",
            "furnishTypes": "",
            "keywords": "",
            "index": index,
        }
        url = "https://www.rightmove.co.uk/property-for-sale/find.html"
        r = httpx.get(url, params=params, headers=DEFAULT_HEADERS, timeout=15)
        r.raise_for_status()
        html = r.text

        m = re.search(r'<script\s+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if not m:
            break
        page_data = json.loads(m.group(1))
        props = (
            page_data.get("props", {})
            .get("pageProps", {})
            .get("properties", [])
        )
        if not props:
            break

        for p in props:
            first_visible = p.get("firstVisibleDate", "")
            dom = 0
            if first_visible:
                try:
                    listed = datetime.fromisoformat(first_visible.replace("Z", "+00:00"))
                    dom = (today - listed).days
                except Exception:
                    pass

            display_status = p.get("displayStatus", "")
            is_stc = any(kw in display_status.lower() for kw in ("stc", "sold"))

            all_props.append({
                "id": p.get("id"),
                "address": p.get("displayAddress", ""),
                "price": p.get("price", {}).get("amount", 0),
                "bedrooms": p.get("bedrooms"),
                "type": p.get("propertySubType", ""),
                "dom": dom,
                "is_stc": is_stc,
                "added_or_reduced": p.get("addedOrReduced", ""),
                "url": f"https://www.rightmove.co.uk/properties/{p.get('id')}#/?channel=RES_BUY",
            })

        pagination = page_data.get("props", {}).get("pageProps", {}).get("pagination", {})
        if pagination.get("last", 0) <= index // 24:
            break
        index += 24
        time.sleep(PAGE_DELAY)

    # Dedup
    seen = set()
    unique = []
    for p in all_props:
        if p["id"] not in seen:
            seen.add(p["id"])
            unique.append(p)

    active = [p for p in unique if not p["is_stc"]]
    stc = [p for p in unique if p["is_stc"]]

    def median(vals):
        if not vals:
            return None
        s = sorted(vals)
        mid = len(s) // 2
        return s[mid]

    return {
        "postcode": postcode,
        "radius_miles": radius,
        "max_price": max_price,
        "total_listings": len(unique),
        "active_count": len(active),
        "stc_count": len(stc),
        "stc_rate_pct": round(len(stc) / max(len(unique), 1) * 100, 1),
        "active_dom_median": median([p["dom"] for p in active]),
        "active_dom_mean": round(sum(p["dom"] for p in active) / max(len(active), 1), 1),
        "stc_dom_median": median([p["dom"] for p in stc]),
        "stc_dom_mean": round(sum(p["dom"] for p in stc) / max(len(stc), 1), 1),
        "listings": unique,
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(
    name="uk_market_search",
    annotations={"readOnlyHint": True, "openWorldHint": True},
)
async def uk_market_search(
    postcode: str = Field(..., description="UK postcode, e.g. DE6 1DQ"),
    radius: float = Field(1.0, description="Search radius in miles"),
    max_price: int = Field(300000, description="Maximum price"),
) -> str:
    """Search Rightmove for active and STC listings near a postcode.

    Returns total counts, STC rate, median days on market, and full listing data.
    """
    try:
        data = _scrape_market(postcode, radius, max_price)
    except Exception as e:
        return json.dumps({"error": str(e)})
    # Strip full listing array for concise output, keep summary
    summary = {k: v for k, v in data.items() if k != "listings"}
    summary["listing_count"] = len(data["listings"])
    summary["sample_listings"] = data["listings"][:10]
    return json.dumps(summary, indent=2, default=str)


@mcp.tool(
    name="uk_land_registry",
    annotations={"readOnlyHint": True, "openWorldHint": True},
)
async def uk_land_registry(
    town: str = Field(..., description="Town name, e.g. ASHBOURNE"),
    postcode_prefix: str = Field(..., description="Postcode prefix, e.g. DE6"),
    months: int = Field(12, description="Months of history"),
) -> str:
    """Query HM Land Registry for completed property sales (Price Paid Data).

    Returns individual sales and monthly volume breakdown by property type.
    """
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=months * 31)).strftime("%Y-%m-%d")
    query = f"""
    PREFIX lrppi: <http://landregistry.data.gov.uk/def/ppi/>
    PREFIX lrcommon: <http://landregistry.data.gov.uk/def/common/>
    SELECT ?date ?price ?paon ?street ?postcode ?type WHERE {{
      ?txn lrppi:transactionDate ?date ;
           lrppi:pricePaid ?price ;
           lrppi:propertyAddress ?addr ;
           lrppi:propertyType ?typeUri .
      ?addr lrcommon:postcode ?postcode ;
            lrcommon:town "{town.upper()}"^^<http://www.w3.org/2001/XMLSchema#string> .
      OPTIONAL {{ ?addr lrcommon:paon ?paon }}
      OPTIONAL {{ ?addr lrcommon:street ?street }}
      ?typeUri <http://www.w3.org/2000/01/rdf-schema#label> ?type .
      FILTER(STRSTARTS(?postcode, "{postcode_prefix.upper()}"))
      FILTER(?date >= "{cutoff}"^^<http://www.w3.org/2001/XMLSchema#date>)
    }} ORDER BY DESC(?date) LIMIT 500
    """
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(SPARQL_ENDPOINT, params={"query": query, "output": "json"}, timeout=60)
            r.raise_for_status()
    except Exception as e:
        return json.dumps({"error": f"Land Registry query failed: {e}"})

    results = r.json().get("results", {}).get("bindings", [])
    sales = []
    monthly: dict[str, dict] = {}
    for row in results:
        date = row.get("date", {}).get("value", "")[:10]
        price = int(float(row.get("price", {}).get("value", 0)))
        paon = row.get("paon", {}).get("value", "")
        street = row.get("street", {}).get("value", "")
        postcode = row.get("postcode", {}).get("value", "")
        ptype = row.get("type", {}).get("value", "")
        sales.append({"date": date, "price": price, "address": f"{paon} {street}".strip(), "postcode": postcode, "type": ptype})
        mk = date[:7]
        if mk not in monthly:
            monthly[mk] = {"total": 0, "Terraced": 0, "Semi-detached": 0, "Detached": 0, "Flat/Maisonette": 0}
        monthly[mk]["total"] += 1
        if ptype in monthly[mk]:
            monthly[mk][ptype] += 1

    return json.dumps({"town": town.upper(), "total_sales": len(sales), "monthly": dict(sorted(monthly.items())), "sales": sales[:50]}, indent=2)


@mcp.tool(
    name="uk_hpi",
    annotations={"readOnlyHint": True, "openWorldHint": True},
)
async def uk_hpi(
    region: str = Field("derbyshire-dales", description="Region slug, e.g. derbyshire-dales, east-midlands, england"),
    months: int = Field(24, description="Months of history"),
) -> str:
    """UK House Price Index for a region. Returns average price, annual change %, and sales volume."""
    query = f"""
    PREFIX ukhpi: <http://landregistry.data.gov.uk/def/ukhpi/>
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
    SELECT ?date ?avgPrice ?hpi ?annualChange ?salesVolume ?regionLabel WHERE {{
      ?obs ukhpi:refRegion <http://landregistry.data.gov.uk/id/region/{region}> ;
           ukhpi:refMonth ?date ; ukhpi:averagePrice ?avgPrice .
      OPTIONAL {{ ?obs ukhpi:housePriceIndex ?hpi }}
      OPTIONAL {{ ?obs ukhpi:percentageChange ?annualChange }}
      OPTIONAL {{ ?obs ukhpi:salesVolume ?salesVolume }}
      <http://landregistry.data.gov.uk/id/region/{region}> rdfs:label ?regionLabel .
      FILTER(LANG(?regionLabel) = "" || LANG(?regionLabel) = "en")
    }} ORDER BY DESC(?date) LIMIT {months}
    """
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(SPARQL_ENDPOINT, params={"query": query, "output": "json"}, timeout=60)
            r.raise_for_status()
    except Exception as e:
        return json.dumps({"error": f"UKHPI query failed: {e}"})

    results = r.json().get("results", {}).get("bindings", [])
    if not results:
        return json.dumps({"error": f"No HPI data for region '{region}'"})

    data_points = []
    for row in sorted(results, key=lambda x: x["date"]["value"]):
        data_points.append({
            "date": row["date"]["value"],
            "average_price": round(float(row["avgPrice"]["value"])),
            "hpi": round(float(row["hpi"]["value"]), 1) if "hpi" in row else None,
            "annual_change_pct": round(float(row["annualChange"]["value"]), 1) if "annualChange" in row else None,
            "sales_volume": int(row["salesVolume"]["value"]) if "salesVolume" in row else None,
        })
    return json.dumps({"region": results[0].get("regionLabel", {}).get("value", region), "months": len(data_points), "data": data_points}, indent=2)


@mcp.tool(
    name="uk_flood_risk",
    annotations={"readOnlyHint": True, "openWorldHint": True},
)
async def uk_flood_risk(
    lat: float = Field(..., description="Latitude"),
    lng: float = Field(..., description="Longitude"),
    dist: int = Field(3, description="Search radius in km"),
) -> str:
    """Environment Agency flood data: monitoring stations, active warnings, and flood areas near a point."""
    result: dict = {"lat": lat, "lng": lng, "dist_km": dist}
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(f"{EA_BASE}/id/stations", params={"lat": lat, "long": lng, "dist": dist}, timeout=15)
            result["stations"] = [{"name": s.get("label",""), "river": s.get("riverName","")} for s in r.json().get("items",[])]
        except Exception:
            result["stations"] = []
        try:
            r2 = await client.get(f"{EA_BASE}/id/floods", params={"lat": lat, "long": lng, "dist": dist*2}, timeout=15)
            result["warnings"] = [{"description": f.get("description",""), "severity": f.get("severityLevel","")} for f in r2.json().get("items",[])]
        except Exception:
            result["warnings"] = []
        try:
            r3 = await client.get(f"{EA_BASE}/id/floodAreas", params={"lat": lat, "long": lng, "dist": dist}, timeout=15)
            result["flood_areas"] = [{"name": a.get("label",""), "river_or_sea": a.get("riverOrSea","")} for a in r3.json().get("items",[])]
        except Exception:
            result["flood_areas"] = []
    return json.dumps(result, indent=2)


@mcp.tool(
    name="uk_crime",
    annotations={"readOnlyHint": True, "openWorldHint": True},
)
async def uk_crime(
    lat: float = Field(..., description="Latitude"),
    lng: float = Field(..., description="Longitude"),
    date: str = Field("", description="Month as YYYY-MM, empty for latest"),
) -> str:
    """Police UK street-level crime data near a point, aggregated by category."""
    params: dict = {"lat": lat, "lng": lng}
    if date:
        params["date"] = date
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{POLICE_BASE}/crimes-street/all-crime", params=params, timeout=15)
            r.raise_for_status()
    except Exception as e:
        return json.dumps({"error": f"Police API failed: {e}"})

    crimes = r.json()
    by_cat: dict[str, int] = {}
    for c in crimes:
        cat = c.get("category", "unknown")
        by_cat[cat] = by_cat.get(cat, 0) + 1
    return json.dumps({"lat": lat, "lng": lng, "date": date or "latest", "total": len(crimes), "by_category": dict(sorted(by_cat.items(), key=lambda x: -x[1]))}, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
