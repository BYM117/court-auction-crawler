from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import ssl
from typing import Any
from urllib.parse import urlencode
from urllib.error import URLError
from urllib.request import Request, urlopen


BRACKET_RE = re.compile(r"\[[^\]]+]")
PAREN_RE = re.compile(r"\(([^)]*)\)")
UNIT_RE = re.compile(r"\b(?:제)?\d+\s*층\s*(?:제)?\d+[A-Za-z]?\s*호\b")
AREA_RE = re.compile(r"\b\d+(?:\.\d+)?\s*㎡")
ROAD_CORE_RE = re.compile(
    r"^(.+?(?:로|길|대로|번길|순환로|중앙로|해안로|산업로|테크노밸리로|국회단지길|국회단지\d+길)\s+\d+(?:-\d+)?)\b"
)
LOT_CORE_RE = re.compile(r"^(.+?(?:읍|면|동(?:\d+가)?|리)\s+산?\s*\d+(?:-\d+)?)\b")


@dataclass(slots=True)
class GeocodeResult:
    lat: float
    lng: float
    pnu: str = ""
    normalized_address: str = ""
    query: str = ""
    source: str = "address"
    quality: str = "verified"


def normalize_auction_address(address: str) -> str:
    text = str(address or "").strip()
    text = re.sub(r"^\s*(사용본거지|소재지|물건소재지)\s*:\s*", "", text)
    text = BRACKET_RE.sub(" ", text)
    text = UNIT_RE.sub(" ", text)
    text = AREA_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def geocode_address(address: str) -> GeocodeResult | None:
    key = env_value("VWORLD_API_KEY") or env_value("PUBLIC_DATA_SERVICE_KEY")
    if not key:
        return None

    max_queries = int(env_value("GEOCODER_MAX_QUERIES") or "4")
    queries = _candidate_queries(address)[:max_queries]
    for query in queries:
        for category in ("PARCEL", "ROAD"):
            try:
                item = _request_vworld_address(key, query, category)
            except (TimeoutError, OSError, URLError, json.JSONDecodeError):
                continue
            point = item.get("point") if isinstance(item, dict) else None
            if not point or not point.get("x") or not point.get("y"):
                continue
            if not _same_region(address, item):
                continue
            return GeocodeResult(
                lat=float(point["y"]),
                lng=float(point["x"]),
                pnu=str(item.get("id") or "") if re.fullmatch(r"\d{19}", str(item.get("id") or "")) else "",
                normalized_address=str(item.get("address", {}).get("parcel") or item.get("address", {}).get("road") or query),
                query=query,
                source="address",
                quality="verified",
            )
    return None


def _candidate_queries(address: str) -> list[str]:
    normalized = normalize_auction_address(address)
    candidates = [normalized]
    without_paren = PAREN_RE.sub(" ", normalized)
    without_paren = re.sub(r"\s+", " ", without_paren).strip()
    candidates.append(without_paren)

    for value in (normalized, without_paren):
        road_core = _extract_road_core(value)
        if road_core:
            candidates.append(road_core)
        lot_core = _extract_lot_core(value)
        if lot_core:
            candidates.append(lot_core)

    seen: set[str] = set()
    return [
        value
        for value in candidates
        if value and _is_specific_query(value) and not (value in seen or seen.add(value))
    ]


def _extract_road_core(address: str) -> str:
    match = ROAD_CORE_RE.search(address)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def _extract_lot_core(address: str) -> str:
    match = LOT_CORE_RE.search(address)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).replace("산 ", "산").strip()


def _is_specific_query(query: str) -> bool:
    return bool(_extract_road_core(query) or _extract_lot_core(query))


def _request_vworld_address(key: str, query: str, category: str) -> dict[str, Any]:
    params = {
        "service": "search",
        "request": "search",
        "version": "2.0",
        "crs": "EPSG:4326",
        "size": "1",
        "page": "1",
        "type": "ADDRESS",
        "category": category,
        "format": "json",
        "query": query,
        "key": key,
    }
    domain = env_value("VWORLD_API_DOMAIN")
    if domain:
        params["domain"] = domain
    url = f"https://api.vworld.kr/req/search?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "court-auction-crawler/0.1"})
    context = ssl._create_unverified_context() if env_value("GEOCODER_INSECURE_SSL") == "1" else None
    timeout = float(env_value("GEOCODER_TIMEOUT") or "3")
    with urlopen(request, timeout=timeout, context=context) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload.get("response", {}).get("result", {}).get("items", [{}])[0] or {}


def _same_region(address: str, item: dict[str, Any]) -> bool:
    source = normalize_auction_address(address)
    source_parts = source.split()
    if len(source_parts) < 2:
        return False

    address_payload = item.get("address") if isinstance(item.get("address"), dict) else {}
    returned_candidates = [
        str(item.get("title") or ""),
        str(address_payload.get("road") or ""),
        str(address_payload.get("parcel") or ""),
    ]
    for returned in returned_candidates:
        returned_parts = returned.split()
        if len(returned_parts) >= 2 and source_parts[0] == returned_parts[0] and source_parts[1] == returned_parts[1]:
            return True
    return False


def env_value(name: str) -> str:
    value = os.getenv(name)
    if value:
        return value

    for env_path in (Path.cwd() / ".env", Path.cwd().parent / ".env"):
        if not env_path.exists():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, raw = line.split("=", 1)
            if key.strip() == name:
                return raw.strip().strip("\"'")
    return ""
