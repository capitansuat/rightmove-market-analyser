"""Rightmove Market Analyser — FastAPI backend."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Rightmove helpers
# ---------------------------------------------------------------------------

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}


def _lookup_postcode(postcode: str) -> str:
    url = "https://los.rightmove.co.uk/typeahead"
    params = {"query": postcode.upper().strip(), "limit": 10, "exclude": "STREET"}
    r = requests.get(url, params=params, timeout=10, headers=DEFAULT_HEADERS)
    r.raise_for_status()
    matches = r.json().get("matches", [])
    if not matches:
        raise ValueError(f"Postcode not found: {postcode}")
    m = matches[0]
    return f"{m.get('type', 'OUTCODE')}^{m['id']}"


def _build_url(location_id: str, *, max_price: int, radius: float, index: int = 0) -> str:
    base = "https://www.rightmove.co.uk/property-for-sale/find.html"
    return (
        f"{base}?locationIdentifier={location_id}"
        f"&maxPrice={max_price}&radius={radius}"
        f"&sortType=6&includeSSTC=true&index={index}"
    )


def _fetch_page(url: str) -> dict:
    r = requests.get(url, headers=DEFAULT_HEADERS, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        raise ValueError("Could not find search data on Rightmove page")
    data = json.loads(tag.string)
    return data["props"]["pageProps"]["searchResults"]


def _parse_listings(props: list, today: datetime) -> list[dict]:
    results = []
    for p in props:
        first_date = p.get("firstVisibleDate", "")
        dom = None
        if first_date:
            dt = datetime.fromisoformat(first_date.replace("Z", "+00:00"))
            dom = (today - dt).days

        status = p.get("displayStatus", "") or ""
        is_stc = "STC" in status.upper() or "SOLD" in status.upper()

        results.append({
            "id": p.get("id"),
            "address": p.get("displayAddress", ""),
            "price": p.get("price", {}).get("amount"),
            "bedrooms": p.get("bedrooms"),
            "type": p.get("propertySubType", ""),
            "dom": dom,
            "is_stc": is_stc,
            "added_or_reduced": p.get("addedOrReduced", ""),
            "url": f"https://www.rightmove.co.uk{p.get('propertyUrl', '')}",
        })
    return results


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Rightmove Market Analyser", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


class MarketSummary(BaseModel):
    postcode: str
    radius_miles: float
    max_price: int
    total_listings: int
    active_count: int
    stc_count: int
    stc_rate_pct: float
    active_dom_median: Optional[int]
    active_dom_mean: Optional[float]
    stc_dom_median: Optional[int]
    stc_dom_mean: Optional[float]
    listings: list[dict]
    fetched_at: str


@app.get("/api/market", response_model=MarketSummary)
def market(
    postcode: str = Query(..., description="UK postcode, e.g. DE6 1DQ"),
    radius: float = Query(1.0, description="Search radius in miles"),
    max_price: int = Query(300000, description="Maximum price"),
):
    try:
        location_id = _lookup_postcode(postcode)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Postcode lookup failed: {e}")

    all_props: list[dict] = []
    today = datetime.now(tz=timezone.utc)

    try:
        first_url = _build_url(location_id, max_price=max_price, radius=radius, index=0)
        sr = _fetch_page(first_url)
        total = int(str(sr.get("resultCount", "0")).replace(",", "") or 0)
        all_props.extend(_parse_listings(sr.get("properties", []), today))

        index = 24
        while index < total and index < 500:
            time.sleep(0.4)
            url = _build_url(location_id, max_price=max_price, radius=radius, index=index)
            sr = _fetch_page(url)
            batch = _parse_listings(sr.get("properties", []), today)
            if not batch:
                break
            all_props.extend(batch)
            index += 24

    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Rightmove fetch failed: {e}")

    seen: set[int] = set()
    unique: list[dict] = []
    for p in all_props:
        if p["id"] not in seen:
            seen.add(p["id"])
            unique.append(p)

    active = [p for p in unique if not p["is_stc"]]
    stc = [p for p in unique if p["is_stc"]]

    def median(vals: list[int]) -> Optional[int]:
        if not vals:
            return None
        s = sorted(vals)
        return s[len(s) // 2]

    def mean(vals: list[int]) -> Optional[float]:
        return round(sum(vals) / len(vals), 1) if vals else None

    active_doms = [p["dom"] for p in active if p["dom"] is not None]
    stc_doms = [p["dom"] for p in stc if p["dom"] is not None]
    stc_rate = round(len(stc) / len(unique) * 100, 1) if unique else 0.0

    return MarketSummary(
        postcode=postcode.upper(),
        radius_miles=radius,
        max_price=max_price,
        total_listings=len(unique),
        active_count=len(active),
        stc_count=len(stc),
        stc_rate_pct=stc_rate,
        active_dom_median=median(active_doms),
        active_dom_mean=mean(active_doms),
        stc_dom_median=median(stc_doms),
        stc_dom_mean=mean(stc_doms),
        listings=sorted(unique, key=lambda x: (x["dom"] or 0)),
        fetched_at=today.isoformat(),
    )


# ---------------------------------------------------------------------------
# Sold prices (Rightmove house-prices turbo-stream)
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
        """Resolve a turbo-stream dict: _N keys map to key name at parsed[N], value at parsed[V]."""
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


@app.get("/api/sold")
def sold(
    postcode: str = Query(..., description="UK postcode e.g. DE6 1DQ"),
):
    slug = postcode.strip().upper().replace(" ", "-").lower()
    url = f"https://www.rightmove.co.uk/house-prices/{slug}.html"

    try:
        r = requests.get(url, headers=DEFAULT_HEADERS, timeout=15)
        r.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Rightmove fetch failed: {e}")

    parsed = _parse_turbo_stream(r.text)
    if not parsed:
        raise HTTPException(status_code=404, detail="No sold data found")

    sales = _extract_sold(parsed)

    # Monthly summary
    from collections import Counter as C
    monthly: dict[str, int] = {}
    for s in sales:
        try:
            from datetime import datetime as DT
            dt = DT.strptime(s["date_sold"], "%d %b %Y")
            key = dt.strftime("%Y-%m")
            monthly[key] = monthly.get(key, 0) + 1
        except ValueError:
            pass

    return {
        "postcode": postcode.upper(),
        "total_sold": len(sales),
        "sales": sales,
        "monthly": dict(sorted(monthly.items())),
        "url": url,
    }


# Serve frontend
frontend_path = Path(__file__).parent.parent / "frontend"
if frontend_path.exists():
    app.mount("/", StaticFiles(directory=str(frontend_path), html=True), name="frontend")
