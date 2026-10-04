from __future__ import annotations

import os
import re
from typing import Any, Callable

from http_client import requests


RANK_REPORT_COST = 10.0


def _normalized_name(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


def _search_area(address: dict[str, Any]) -> str:
    parts = [
        address.get("locality"),
        address.get("administrativeArea"),
        address.get("regionCode"),
    ]
    return ", ".join(str(part).strip() for part in parts if part)


def run_local_rank_scan(
    *,
    location_id: str,
    keyword: str,
    google_access_token: str,
    serpapi_key: str | None = None,
    http_get: Callable[..., Any] = requests.get,
) -> dict[str, Any]:
    """Measure one business's position in the first 11 Google local results."""
    keyword = keyword.strip()
    if not keyword:
        raise ValueError("Keyword is required")

    key = serpapi_key or os.getenv("SERPAPI_KEY")
    if not key:
        raise RuntimeError("SERPAPI_KEY is not configured")

    business_response = http_get(
        f"https://mybusinessbusinessinformation.googleapis.com/v1/{location_id}",
        headers={"Authorization": f"Bearer {google_access_token}"},
        params={"readMask": "title,storefrontAddress,metadata"},
        timeout=30,
    )
    if not business_response.ok:
        raise RuntimeError("Could not load the connected business details from Google")
    business = business_response.json()
    business_title = (business.get("title") or "").strip()
    address = _search_area(business.get("storefrontAddress") or {})
    if not business_title or not address:
        raise ValueError("The connected business needs a name and storefront address")

    country = str((business.get("storefrontAddress") or {}).get("regionCode") or "").lower()
    params = {
        "engine": "google_local",
        "q": keyword,
        "location": address,
        "hl": "en",
        "api_key": key,
    }
    if re.fullmatch(r"[a-z]{2}", country):
        params["gl"] = country
    serp_response = http_get("https://serpapi.com/search.json", params=params, timeout=45)
    if not serp_response.ok:
        raise RuntimeError("Google local rank data is temporarily unavailable")
    payload = serp_response.json()
    if payload.get("error"):
        raise RuntimeError(f"Rank provider error: {str(payload['error'])[:300]}")

    raw_results = payload.get("local_results") or []
    if not isinstance(raw_results, list) or not raw_results:
        raise RuntimeError("No Google local results were returned for this keyword and area")

    target_place_id = str((business.get("metadata") or {}).get("placeId") or "")
    target_name = _normalized_name(business_title)
    results: list[dict[str, Any]] = []
    actual_rank: int | None = None
    for index, item in enumerate(raw_results[:11], start=1):
        position = item.get("position")
        position = int(position) if isinstance(position, (int, float)) else index
        place_id = str(item.get("place_id") or item.get("data_id") or "")
        is_target = bool(
            (target_place_id and place_id == target_place_id)
            or (target_name and _normalized_name(item.get("title")) == target_name)
        )
        if is_target and actual_rank is None:
            actual_rank = position
        results.append(
            {
                "position": position,
                "business_name": str(item.get("title") or "Unknown business"),
                "rating": float(item["rating"]) if item.get("rating") is not None else None,
                "reviews": int(item["reviews"]) if item.get("reviews") is not None else None,
                "address": str(item.get("address") or "") or None,
                "place_id": place_id or None,
                "is_target": is_target,
            }
        )

    return {
        "keyword": keyword,
        "location_id": location_id,
        "search_area": address,
        "target_business": business_title,
        "actual_rank": actual_rank,
        "found_in_top_11": actual_rank is not None and actual_rank <= 11,
        "results": results,
    }
