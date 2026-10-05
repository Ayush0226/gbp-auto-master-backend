from __future__ import annotations

import os
import re
import base64
import hashlib
import hmac
import json
import time
import zlib
from typing import Any, Callable

from http_client import requests


RANK_REPORT_COST = 10.0
RANK_PDF_TTL_SECONDS = 3600


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


def create_rank_pdf_token(
    report: dict[str, Any], secret: str, ttl_seconds: int = RANK_PDF_TTL_SECONDS
) -> tuple[str, int]:
    if not secret:
        raise RuntimeError("Rank report signing is not configured")
    expires_at = int(time.time()) + ttl_seconds
    payload = json.dumps(
        {"exp": expires_at, "report": report}, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(zlib.compress(payload, 9)).rstrip(b"=")
    signature = hmac.new(secret.encode("utf-8"), encoded, hashlib.sha256).digest()
    token = encoded + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")
    return token.decode("ascii"), expires_at


def read_rank_pdf_token(token: str, secret: str) -> dict[str, Any]:
    try:
        encoded_text, signature_text = token.split(".", 1)
        encoded = encoded_text.encode("ascii")
        signature = base64.urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
        expected = hmac.new(secret.encode("utf-8"), encoded, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        compressed = base64.urlsafe_b64decode(encoded_text + "=" * (-len(encoded_text) % 4))
        payload = json.loads(zlib.decompress(compressed))
        if int(payload["exp"]) < int(time.time()):
            raise TimeoutError("Rank report download link has expired")
        report = payload["report"]
        if not isinstance(report, dict):
            raise ValueError
        return report
    except TimeoutError:
        raise
    except Exception as error:
        raise ValueError("Invalid rank report download link") from error


def _pdf_escape(value: Any) -> str:
    text = str(value if value is not None else "N/A")
    text = text.encode("latin-1", "replace").decode("latin-1")
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def render_rank_report_pdf(report: dict[str, Any]) -> bytes:
    rank = f"#{report.get('actual_rank')}" if report.get("found_in_top_11") else "Not found in top 11"
    lines = [
        "GBP Auto Master - Google Local Rank Report",
        f"Keyword: {report.get('keyword')}",
        f"Business: {report.get('target_business')}",
        f"Search area: {report.get('search_area')}",
        f"Measured rank: {rank}",
        "",
        "Top 11 Google local results",
    ]
    for item in report.get("results") or []:
        marker = " [YOUR BUSINESS]" if item.get("is_target") else ""
        lines.append(
            f"#{item.get('position')} {item.get('business_name')}{marker} | "
            f"Rating: {item.get('rating') if item.get('rating') is not None else 'N/A'} | "
            f"Reviews: {item.get('reviews') if item.get('reviews') is not None else 'N/A'}"
        )
        if item.get("address"):
            lines.append(f"    {item['address']}")
    lines += ["", "The ranking reflects the Google local results returned for this keyword and area at scan time."]

    pages = [lines[index:index + 38] for index in range(0, len(lines), 38)] or [[]]
    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{4 + page * 2} 0 R" for page in range(len(pages)))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for page_number, page_lines in enumerate(pages):
        page_object = 4 + page_number * 2
        content_object = page_object + 1
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_object} 0 R >>".encode()
        )
        commands = ["BT", "/F1 11 Tf", "50 750 Td", "14 TL"]
        for line in page_lines:
            commands.append(f"({_pdf_escape(line)}) Tj")
            commands.append("T*")
        commands.append("ET")
        stream = "\n".join(commands).encode("latin-1")
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")

    document = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(document))
        document.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(document)
    document.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        document.extend(f"{offset:010d} 00000 n \n".encode())
    document.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(document)
