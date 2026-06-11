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
    url = "https://los.rightmove.co.uk/typeahead"
    params = {"query": postcode.upper().strip(), "limit": 10, "exclude": "STREET"}
    r = httpx.get(url, params=params, headers=DEFAULT_HEADERS, timeout=10)
    r.raise_for_status()
    matches = r.json().get("matches", [])
    if not matches:
        raise ValueError(f"No Rightmove location found for '{postcode}'")
    m = matches[0]
    return f"{m.get('type', 'OUTCODE')}^{m['id']}"


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
        search_results = (
            page_data.get("props", {})
            .get("pageProps", {})
            .get("searchResults", {})
        )
        props = search_results.get("properties", [])
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

        total = int(search_results.get("resultCount", "0").replace(",", "") or 0)
        index += 24
        if index >= total or index >= 1000:
            break
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
# Rightmove sold prices (turbo-stream)
# ---------------------------------------------------------------------------

def _parse_turbo_stream(raw_text: str) -> list:
    match = re.search(r'streamController\.enqueue\("(.+?)"\)', raw_text, re.DOTALL)
    if not match:
        return []
    raw = match.group(1).encode().decode("unicode_escape")
    return json.loads(raw)


def _extract_sold(parsed: list) -> list[dict]:
    props_list = None
    for i, item in enumerate(parsed):
        if item == "properties" and i + 1 < len(parsed) and isinstance(parsed[i + 1], list):
            props_list = parsed[i + 1]
            break
    if not props_list:
        return []

    def resolve_dict(d: dict) -> dict:
        result = {}
        for k, v in d.items():
            if not k.startswith("_") or not k[1:].isdigit():
                continue
            idx = int(k[1:])
            key_name = parsed[idx] if idx < len(parsed) else k
            if isinstance(v, int) and 0 <= v < len(parsed):
                result[key_name] = parsed[v]
            else:
                result[key_name] = v
        return result

    results = []
    for pi in props_list:
        if not isinstance(pi, int) or pi >= len(parsed):
            continue
        d = parsed[pi]
        if not isinstance(d, dict):
            continue

        prop = resolve_dict(d)
        address = prop.get("address", "")
        prop_type = prop.get("propertyType", "")
        bedrooms = prop.get("bedrooms")

        lt_raw = prop.get("latestTransaction")
        if not isinstance(lt_raw, dict):
            continue
        lt = resolve_dict(lt_raw)

        price = str(lt.get("displayPrice", "")).replace("\u00a3", "£").replace("Â£", "£")
        date_sold = str(lt.get("dateSold", ""))

        if not date_sold or not address:
            continue

        results.append({
            "address": str(address),
            "date_sold": date_sold,
            "price": price,
            "type": str(prop_type),
            "bedrooms": bedrooms if isinstance(bedrooms, int) else None,
        })
    return results


@mcp.tool(
    name="uk_sold_prices",
    annotations={"readOnlyHint": True, "openWorldHint": True},
)
async def uk_sold_prices(
    postcode: str = Field(..., description="UK postcode, e.g. SW1A 2AA"),
) -> str:
    """Rightmove sold prices for a postcode — completed sale transactions with address, price, date, and property type.

    Returns up to ~25 most recent sold properties from Rightmove's house-prices pages,
    plus a monthly count summary. Complements uk_land_registry (which is official but
    has a 1-3 month registration lag).
    """
    slug = postcode.strip().upper().replace(" ", "-").lower()
    url = f"https://www.rightmove.co.uk/house-prices/{slug}.html"

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=DEFAULT_HEADERS, timeout=15)
            r.raise_for_status()
    except Exception as e:
        return json.dumps({"error": f"Rightmove fetch failed: {e}"})

    parsed = _parse_turbo_stream(r.text)
    if not parsed:
        return json.dumps({"error": f"No sold data found for {postcode}"})

    sales = _extract_sold(parsed)

    monthly: dict[str, int] = {}
    for s in sales:
        try:
            dt = datetime.strptime(s["date_sold"], "%d %b %Y")
            key = dt.strftime("%Y-%m")
            monthly[key] = monthly.get(key, 0) + 1
        except ValueError:
            pass

    return json.dumps({
        "postcode": postcode.upper(),
        "total_sold": len(sales),
        "monthly": dict(sorted(monthly.items())),
        "sales": sales,
    }, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Listed Buildings — Historic England NHLE (free, no key)
# ---------------------------------------------------------------------------

NHLE_BASE = (
    "https://services-eu1.arcgis.com/ZOdPfBS3aqqDYPUQ/arcgis/rest/services/"
    "National_Heritage_List_for_England_NHLE_v02_VIEW/FeatureServer"
)


@mcp.tool(
    name="uk_listed_buildings",
    annotations={"readOnlyHint": True, "openWorldHint": True},
)
async def uk_listed_buildings(
    lat: float = Field(..., description="Latitude"),
    lng: float = Field(..., description="Longitude"),
    radius_m: int = Field(500, description="Search radius in metres"),
) -> str:
    """Listed buildings near a point from Historic England's National Heritage List (NHLE).

    Returns building name, listing grade (I, II*, II), list entry number, and link.
    Useful for checking if a property or its neighbours are listed (affects renovations,
    insurance, and conveyancing).
    """
    params = {
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "distance": str(radius_m),
        "units": "esriSRUnit_Meter",
        "outFields": "Name,Grade,ListEntry,hyperlink",
        "returnGeometry": "false",
        "f": "json",
        "resultRecordCount": "50",
    }
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{NHLE_BASE}/0/query", params=params, timeout=20)
            r.raise_for_status()
    except Exception as e:
        return json.dumps({"error": f"Historic England query failed: {e}"})

    data = r.json()
    if "error" in data:
        return json.dumps({"error": str(data["error"])})

    buildings = []
    grades: dict[str, int] = {}
    for f in data.get("features", []):
        a = f.get("attributes", {})
        grade = a.get("Grade", "?")
        grades[grade] = grades.get(grade, 0) + 1
        buildings.append({
            "name": a.get("Name", ""),
            "grade": grade,
            "list_entry": a.get("ListEntry"),
            "url": a.get("hyperlink", ""),
        })

    return json.dumps({
        "lat": lat, "lng": lng, "radius_m": radius_m,
        "total": len(buildings),
        "by_grade": grades,
        "buildings": buildings,
    }, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Air Quality — DEFRA UK-AIR SOS API (free, no key)
# ---------------------------------------------------------------------------

DEFRA_SOS = "https://uk-air.defra.gov.uk/sos-ukair/api/v1"


@mcp.tool(
    name="uk_air_quality",
    annotations={"readOnlyHint": True, "openWorldHint": True},
)
async def uk_air_quality(
    lat: float = Field(..., description="Latitude"),
    lng: float = Field(..., description="Longitude"),
    max_sites: int = Field(3, description="Number of nearest monitoring sites to include"),
) -> str:
    """Air quality from DEFRA's UK-AIR monitoring network.

    Finds the nearest monitoring sites and returns the latest pollutant measurements
    (NO2, PM10, PM2.5, ozone etc. in µg/m³). Note: rural areas may be 15-30km from
    the nearest monitor, so values are indicative of the wider area.
    """
    import math

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{DEFRA_SOS}/stations", timeout=30)
            r.raise_for_status()
            stations = r.json()

            def dist_km(slat: float, slng: float) -> float:
                return math.sqrt(
                    ((slat - lat) * 111) ** 2
                    + ((slng - lng) * 111 * math.cos(math.radians(lat))) ** 2
                )

            # Coordinates are [lat, lng, alt]; group series by site name
            sites: dict[str, dict] = {}
            for s in stations:
                coords = s.get("geometry", {}).get("coordinates", [])
                if len(coords) < 2:
                    continue
                label = s.get("properties", {}).get("label", "")
                site_name = label.split("-")[0]
                d = dist_km(coords[0], coords[1])
                if site_name not in sites or d < sites[site_name]["dist_km"]:
                    sites[site_name] = {"dist_km": d, "station_ids": []}
                sites[site_name]["station_ids"].append(s.get("properties", {}).get("id"))

            nearest = sorted(sites.items(), key=lambda x: x[1]["dist_km"])[:max_sites]

            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            results = []
            for site_name, info in nearest:
                site_result = {"site": site_name, "dist_km": round(info["dist_km"], 1), "measurements": []}
                for sid in info["station_ids"]:
                    sr = await client.get(f"{DEFRA_SOS}/stations/{sid}", timeout=20)
                    if sr.status_code != 200:
                        continue
                    st = sr.json()
                    ts_map = st.get("properties", {}).get("timeseries", {})
                    label = st.get("properties", {}).get("label", "")
                    pollutant = label.split("-", 1)[1] if "-" in label else label
                    for tid in ts_map:
                        dr = await client.get(
                            f"{DEFRA_SOS}/timeseries/{tid}/getData",
                            params={"timespan": f"PT48H/{now}"},
                            timeout=20,
                        )
                        if dr.status_code != 200:
                            continue
                        vals = dr.json().get("values", [])
                        if vals:
                            last = vals[-1]
                            ts_str = datetime.fromtimestamp(
                                last["timestamp"] / 1000, tz=timezone.utc
                            ).strftime("%Y-%m-%d %H:%M")
                            site_result["measurements"].append({
                                "pollutant": pollutant.replace(" (air)", "").replace(" (aerosol)", ""),
                                "value_ugm3": last["value"],
                                "measured_at": ts_str,
                            })
                results.append(site_result)
    except Exception as e:
        return json.dumps({"error": f"DEFRA air quality failed: {e}"})

    return json.dumps({"lat": lat, "lng": lng, "sites": results}, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Schools — OpenStreetMap Overpass API (free, no key)
# ---------------------------------------------------------------------------

OVERPASS_URL = "https://overpass-api.de/api/interpreter"


@mcp.tool(
    name="uk_schools",
    annotations={"readOnlyHint": True, "openWorldHint": True},
)
async def uk_schools(
    lat: float = Field(..., description="Latitude"),
    lng: float = Field(..., description="Longitude"),
    radius_m: int = Field(3000, description="Search radius in metres"),
) -> str:
    """Schools near a point from OpenStreetMap.

    Returns school names and types (primary/secondary) within the radius.
    Does not include Ofsted ratings — check reports.ofsted.gov.uk for those.
    """
    query = (
        f'[out:json][timeout:20];'
        f'(node["amenity"="school"](around:{radius_m},{lat},{lng});'
        f'way["amenity"="school"](around:{radius_m},{lat},{lng}););'
        f'out center tags;'
    )
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                OVERPASS_URL,
                content=query.encode(),
                headers={"Content-Type": "text/plain", "User-Agent": "uk-property-mcp/1.0"},
                timeout=30,
            )
            r.raise_for_status()
    except Exception as e:
        return json.dumps({"error": f"Overpass query failed: {e}"})

    schools = []
    seen = set()
    for e in r.json().get("elements", []):
        tags = e.get("tags", {})
        name = tags.get("name", "")
        if not name or name in seen:
            continue
        seen.add(name)
        schools.append({
            "name": name,
            "type": tags.get("school", ""),
            "religion": tags.get("religion", ""),
        })

    return json.dumps({
        "lat": lat, "lng": lng, "radius_m": radius_m,
        "total": len(schools),
        "schools": schools,
        "note": "For Ofsted ratings use the uk_ofsted tool",
    }, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Ofsted ratings — reports.ofsted.gov.uk (free, server-rendered HTML)
# ---------------------------------------------------------------------------

OFSTED_BASE = "https://reports.ofsted.gov.uk"


@mcp.tool(
    name="uk_ofsted",
    annotations={"readOnlyHint": True, "openWorldHint": True},
)
async def uk_ofsted(
    postcode: str = Field(..., description="UK postcode, e.g. SW1A 2AA"),
    radius_miles: int = Field(5, description="Search radius in miles (1-25)"),
    max_schools: int = Field(8, description="Max schools to fetch ratings for (each needs a page fetch)"),
) -> str:
    """Ofsted inspection ratings for schools near a postcode, from the official Ofsted reports site.

    Returns school name, category, latest inspection rating (Outstanding / Good /
    Requires improvement / Inadequate), inspection date, and report link.
    Note: schools inspected after Sept 2024 may not have a single overall rating
    (Ofsted replaced it with report cards); for those the latest available
    judgement is returned.
    """
    search_url = (
        f"{OFSTED_BASE}/search?q=&location={postcode.replace(' ', '+')}"
        f"&radius={radius_miles}&level_1_types=1&level_2_types%5B%5D=1"
    )
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(search_url, headers=DEFAULT_HEADERS, timeout=20)
            r.raise_for_status()

            blocks = re.findall(r'<li[^>]*search-result[^>]*>(.*?)</li>', r.text, re.DOTALL)
            schools = []
            for block in blocks[:max_schools]:
                name_m = re.search(r'<a[^>]*>([^<]+)</a>', block)
                link_m = re.search(r'href="([^"]+)"', block)
                cat_m = re.search(r'Category:.*?<[^>]*>([^<]+)<', block, re.DOTALL)
                if not name_m or not link_m:
                    continue

                school = {
                    "name": name_m.group(1).replace("&#039;", "'").strip(),
                    "category": cat_m.group(1).strip() if cat_m else "",
                    "rating": None,
                    "inspection_date": None,
                    "report_url": f"{OFSTED_BASE}{link_m.group(1)}",
                }

                # Fetch provider page for latest inspection rating
                try:
                    pr = await client.get(school["report_url"], headers=DEFAULT_HEADERS, timeout=15)
                    if pr.status_code == 200:
                        text = re.sub(r"<[^>]+>", " ", pr.text)
                        text = re.sub(r"\s+", " ", text)
                        m = re.search(
                            r"Full inspection: (Outstanding|Good|Requires improvement|Inadequate)"
                            r".{0,80}?Published (\d{1,2} \w+ \d{4})",
                            text,
                        )
                        if m:
                            school["rating"] = m.group(1)
                            school["inspection_date"] = m.group(2)
                except Exception:
                    pass

                schools.append(school)
                time.sleep(0.3)  # polite delay
    except Exception as e:
        return json.dumps({"error": f"Ofsted search failed: {e}"})

    return json.dumps({
        "postcode": postcode.upper(),
        "radius_miles": radius_miles,
        "total": len(schools),
        "schools": schools,
        "source": "reports.ofsted.gov.uk",
    }, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Daily tracker — snapshot + transition report (local SQLite)
# ---------------------------------------------------------------------------

@mcp.tool(
    name="uk_tracker_snapshot",
    annotations={"readOnlyHint": False, "openWorldHint": True},
)
async def uk_tracker_snapshot(
    postcode: str = Field(..., description="UK postcode to track, e.g. SW1A 2AA"),
    radius: float = Field(1.0, description="Search radius in miles"),
    max_price: int = Field(500000, description="Maximum price"),
) -> str:
    """Capture today's Rightmove listing states into the local tracker database.

    Run daily (manually or via cron) to build a history. Transitions between
    snapshots reveal true time-to-STC, fall-throughs, and price changes.
    """
    from tracker import take_snapshot
    try:
        result = take_snapshot(postcode, radius, max_price)
    except Exception as e:
        return json.dumps({"error": f"Snapshot failed: {e}"})
    return json.dumps(result, indent=2)


@mcp.tool(
    name="uk_tracker_report",
    annotations={"readOnlyHint": True, "openWorldHint": False},
)
async def uk_tracker_report(
    days: int = Field(30, description="How many days of snapshots to compare"),
) -> str:
    """Status transitions from the local tracker database.

    Compares consecutive daily snapshots and reports: Active -> STC transitions
    (with true time-to-STC), STC -> Active fall-throughs, price changes,
    new listings, and removals. A listing removed while STC has likely
    completed; removed while active was likely withdrawn.
    """
    from tracker import compute_transitions
    try:
        result = compute_transitions(days=days)
    except Exception as e:
        return json.dumps({"error": f"Report failed: {e}"})
    return json.dumps(result, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
