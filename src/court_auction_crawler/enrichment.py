"""DB의 원시 물건 레코드를 꽁지맵이 쓰는 v1 공개 스키마로 변환하는 순수 함수들.

주소·가격·면적·지분 파싱, 스크리닝 점수, 물건유형/등기 힌트 추론이 여기 모인다.
전부 부작용 없는 순수 함수라 web(HTTP)과 cli(스냅샷 export)가 공유하고 단독 테스트된다.
web.py가 이 모듈을 re-export하므로 `from .web import public_auction_detail` 등 기존 경로는 유지된다."""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from . import __version__
from .common import TERMINAL_STATUS_KEYWORDS

BRACKET_RE = re.compile(r"\[([^\]]+)]")
PAREN_RE = re.compile(r"\(([^)]*)\)")
MONEY_RE = re.compile(r"[\d,]+")
PERCENT_RE = re.compile(r"\((\d+(?:\.\d+)?)%\)")
AREA_RE = re.compile(r"([\d,.]+)\s*㎡")
FAIL_COUNT_RE = re.compile(r"유찰\s*(\d+)")
LOT_NUMBER_RE = re.compile(r"(?:산\s*)?\d+(?:-\d+)?")


def safe_external_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return text


def parse_optional_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_optional_bool(value: str) -> bool | None:
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized in {"1", "true", "yes", "y", "on", "활성"}:
        return True
    if normalized in {"0", "false", "no", "n", "off", "비활성"}:
        return False
    return None


def public_auction_list(payload: dict[str, Any]) -> dict[str, Any]:
    items = [public_auction_summary(item) for item in payload.get("items", [])]
    total = int(payload.get("total", 0))
    offset = int(payload.get("offset", 0))
    limit = int(payload.get("limit", len(items)))
    return {
        "total": total,
        "count": len(items),
        "limit": limit,
        "offset": offset,
        "sort": payload.get("sort", "last_seen_desc"),
        "items": items,
        "has_more": offset + len(items) < total,
    }


def public_stats(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": __version__,
        "total": payload.get("total", 0),
        "active": payload.get("active", 0),
        "due": payload.get("due", 0),
        "by_detail_status": payload.get("by_detail_status", []),
        "by_document_status": payload.get("by_document_status", []),
        "asset_count": payload.get("asset_count", 0),
        "latest_sync": payload.get("latest_sync"),
        "by_source": payload.get("by_source", []),
        "by_status": payload.get("by_status", []),
    }


def public_auction_summary(item: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "id": item.get("item_key", ""),
        "source": item.get("source", ""),
        "case_no": item.get("case_no", ""),
        "item_no": item.get("item_no", ""),
        "court": item.get("court", ""),
        "address": item.get("address", ""),
        "category": item.get("category", ""),
        "appraisal": item.get("appraisal", ""),
        "minimum_bid": item.get("minimum_bid", ""),
        "sale_date": item.get("sale_date", ""),
        "status": item.get("status", ""),
        "detail_url": safe_external_url(item.get("detail_url", "")),
        "lat": parse_optional_float(item.get("lat")),
        "lng": parse_optional_float(item.get("lng")),
        "pnu": item.get("pnu", ""),
        "coordinate_source": item.get("coordinate_source", ""),
        "coordinate_quality": item.get("coordinate_quality", "missing"),
        "normalized_address": item.get("normalized_address", ""),
        "geocoded_at": item.get("geocoded_at", ""),
        "last_seen_at": item.get("last_seen_at", ""),
        "updated_at": item.get("updated_at", ""),
        "detail_status": item.get("detail_status", "pending"),
        "detail_collected_at": item.get("detail_collected_at", ""),
    }
    summary.update(public_auction_enrichment(item))
    return summary


def build_official_price(item: dict[str, Any]) -> dict[str, Any] | None:
    """사전 계산해 DB에 저장한 공시기준가를 공개 스키마로 내보낸다. 없으면 None."""
    value = parse_optional_float(item.get("official_price"))
    if not value or value <= 0:
        return None
    detail_raw = item.get("official_price_detail", "")
    try:
        detail = json.loads(detail_raw) if detail_raw else {}
    except (ValueError, TypeError):
        detail = {}
    return {
        "value": value,
        "type": item.get("official_price_type", ""),
        "year": item.get("official_price_year", ""),
        "detail": detail,
    }


def public_auction_enrichment(item: dict[str, Any]) -> dict[str, Any]:
    appraisal = parse_first_money(item.get("appraisal"))
    minimum_bid = parse_first_money(item.get("minimum_bid"))
    minimum_bid_percent = parse_bid_percent(item.get("minimum_bid"), appraisal, minimum_bid)
    address_info = parse_property_address(item.get("address", ""))
    fail_count = parse_fail_count(item.get("status", ""))
    active = parse_item_active(item)
    screening = build_screening(item, appraisal, minimum_bid, minimum_bid_percent, address_info, fail_count)

    return {
        "case": {
            "court": item.get("court", ""),
            "case_no": normalize_case_number(item.get("case_no", ""), item.get("court", "")),
            "display_case_no": item.get("case_no", ""),
            "item_no": item.get("item_no", ""),
            "source": item.get("source", ""),
        },
        "auction": {
            "sale_date": normalize_date_text(item.get("sale_date", "")),
            "sale_date_raw": item.get("sale_date", ""),
            "status": item.get("status", ""),
            "fail_count": fail_count,
            "is_active": active,
            "detail_url": safe_external_url(item.get("detail_url", "")),
        },
        "property": {
            "category": item.get("category", ""),
            "type_guess": infer_property_type(item.get("category", ""), item.get("address", "")),
            "address": address_info,
            "area": parse_area_info(address_info),
            "share": parse_share_info(address_info),
            "registry_search_hint": build_registry_search_hint(address_info, item.get("category", "")),
        },
        "price": {
            "appraisal": appraisal,
            "minimum_bid": minimum_bid,
            "minimum_bid_rate": round(minimum_bid_percent / 100, 4) if minimum_bid_percent is not None else None,
            "minimum_bid_percent": minimum_bid_percent,
            "official": build_official_price(item),
            "raw": {
                "appraisal": item.get("appraisal", ""),
                "minimum_bid": item.get("minimum_bid", ""),
            },
        },
        "map": {
            "lat": parse_optional_float(item.get("lat")),
            "lng": parse_optional_float(item.get("lng")),
            "pnu": item.get("pnu", ""),
            "coordinate_source": item.get("coordinate_source", "") if item.get("lat") and item.get("lng") else "none",
            "coordinate_quality": item.get("coordinate_quality", "") if item.get("lat") and item.get("lng") else "missing",
            "normalized_address": item.get("normalized_address", ""),
            "geocoded_at": item.get("geocoded_at", ""),
        },
        "screening": screening,
    }


def normalize_case_number(case_no: str, court: str) -> str:
    text = str(case_no or "").strip()
    court_text = str(court or "").strip()
    if court_text and text.startswith(court_text):
        return text.removeprefix(court_text).strip()
    return text


def parse_item_active(item: dict[str, Any]) -> bool:
    value = item.get("is_active")
    if value is not None:
        return bool(value)
    status = str(item.get("status", ""))
    return not any(keyword in status for keyword in TERMINAL_STATUS_KEYWORDS)


def parse_first_money(value: Any) -> int | None:
    match = MONEY_RE.search(str(value or ""))
    if not match:
        return None
    return int(match.group(0).replace(",", ""))


def parse_bid_percent(raw_value: Any, appraisal: int | None, minimum_bid: int | None) -> float | None:
    match = PERCENT_RE.search(str(raw_value or ""))
    if match:
        return round(float(match.group(1)), 2)
    if appraisal and minimum_bid:
        return round(minimum_bid / appraisal * 100, 2)
    return None


def parse_fail_count(status: str) -> int:
    match = FAIL_COUNT_RE.search(str(status or ""))
    return int(match.group(1)) if match else 0


def normalize_date_text(value: Any) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", text):
        return text.replace(".", "-")
    if re.fullmatch(r"\d{4}/\d{2}/\d{2}", text):
        return text.replace("/", "-")
    return text


def parse_property_address(address: str) -> dict[str, Any]:
    raw = str(address or "").strip()
    bracket_parts = [part.strip() for part in BRACKET_RE.findall(raw) if part.strip()]
    clean = BRACKET_RE.sub(" ", raw)
    building_hint = ""
    for part in PAREN_RE.findall(clean):
        if "," in part or any(word in part for word in ("동", "아파트", "빌라", "오피스텔")):
            building_hint = part.split(",")[-1].strip()
    clean = re.sub(r"\s+", " ", clean).strip()
    tokens = clean.split()
    sido = tokens[0] if tokens else ""
    sigungu = next((token for token in tokens[1:] if token.endswith(("시", "군", "구"))), "")
    eup_myeon_dong = next(
        (
            token
            for token in tokens[1:]
            if token.endswith(("읍", "면", "동", "리")) and not is_unit_dong_token(token)
        ),
        "",
    )
    lot_number = find_lot_number(clean) or find_lot_number(" ".join(bracket_parts))

    return {
        "raw": raw,
        "clean": clean,
        "detail": " ".join(bracket_parts),
        "sido": sido,
        "sigungu": sigungu,
        "eup_myeon_dong": eup_myeon_dong,
        "lot_number": lot_number,
        "building_name": building_hint,
        "dong": find_unit_part(raw, "동"),
        "ho": find_unit_part(raw, "호"),
    }


def find_lot_number(value: str) -> str:
    matches = re.findall(
        r"(?:[가-힣A-Za-z0-9]+(?:읍|면|동|리))\s+((?:산\s*)?\d+(?:-\d+)?)(?=$|\s|[,)\]])",
        str(value or ""),
    )
    return matches[-1].replace(" ", "") if matches else ""


def is_unit_dong_token(value: str) -> bool:
    token = str(value or "").strip(" ,./")
    return bool(re.fullmatch(r"(?:제)?(?:\d+[A-Za-z]?|[A-Za-z]|[가-힣])동", token))


def find_unit_part(value: str, suffix: str) -> str:
    for token in re.split(r"\s+", str(value or "")):
        token = token.strip(" ,./()[]")
        if suffix == "동" and is_unit_dong_token(token):
            return token
        if suffix == "호" and re.fullmatch(r"(?:제)?\d+[A-Za-z]?호", token):
            return token
    return ""


def parse_area_info(address_info: dict[str, Any]) -> dict[str, Any]:
    detail = str(address_info.get("detail") or "")
    raw = str(address_info.get("raw") or "")
    area_text = detail or raw
    values = [float(value.replace(",", "")) for value in AREA_RE.findall(area_text)]
    land_values = [float(value.replace(",", "")) for value in AREA_RE.findall(detail) if "토지" in detail[: detail.find(value) + len(value) + 2]]
    building_values = [
        float(value.replace(",", ""))
        for value in AREA_RE.findall(detail)
        if any(word in detail[: detail.find(value) + len(value) + 2] for word in ("건물", "집합건물", "층", "구조"))
    ]
    return {
        "raw": area_text,
        "total_sqm": round(sum(values), 2) if values else None,
        "land_sqm": round(sum(land_values), 2) if land_values else None,
        "building_sqm": round(sum(building_values), 2) if building_values else None,
        "values_sqm": values,
    }


def parse_share_info(address_info: dict[str, Any]) -> dict[str, Any]:
    text = f"{address_info.get('raw', '')} {address_info.get('detail', '')}"
    is_share = any(word in text for word in ("지분", "공유자", "분의"))
    fraction = ""
    match = re.search(r"(\d+)\s*분의\s*(\d+)", text)
    if match:
        fraction = f"{match.group(2)}/{match.group(1)}"
    return {
        "is_share_sale": is_share,
        "fraction": fraction,
        "raw": text if is_share else "",
    }


def build_registry_search_hint(address_info: dict[str, Any], category: str) -> dict[str, Any]:
    type_guess = infer_registry_realty_type(category, address_info.get("detail", ""))
    return {
        "realty_type_guess": type_guess,
        "address_for_search": address_info.get("clean", ""),
        "sido": address_info.get("sido", ""),
        "sigungu": address_info.get("sigungu", ""),
        "eup_myeon_dong": address_info.get("eup_myeon_dong", ""),
        "lot_number": address_info.get("lot_number", ""),
        "building_name": address_info.get("building_name", ""),
        "dong": address_info.get("dong", ""),
        "ho": address_info.get("ho", ""),
    }


def infer_property_type(category: str, address: str) -> str:
    text = f"{category} {address}"
    if "오피스텔" in text:
        return "오피스텔"
    if "아파트" in text:
        return "아파트"
    if any(word in text for word in ("다세대", "연립", "빌라")):
        return "빌라"
    if any(word in text for word in ("상가", "근린", "점포")):
        return "상가"
    if any(word in text for word in ("임야", "전 ", "답 ", "도로", "대지", "토지")):
        return "토지"
    if any(word in text for word in ("단독주택", "주택")):
        return "단독주택"
    return str(category or "").strip() or "기타"


def infer_registry_realty_type(category: str, detail: str) -> str:
    text = f"{category} {detail}"
    has_land = any(word in text for word in ("토지", "임야", "대지", "전 ", "답 ", "도로"))
    has_building = any(word in text for word in ("건물", "집합건물", "아파트", "오피스텔", "주택", "상가"))
    if "집합건물" in text or any(word in text for word in ("아파트", "오피스텔", "다세대", "연립")):
        return "집합건물"
    if has_land and has_building:
        return "토지+건물"
    if has_land:
        return "토지"
    if has_building:
        return "건물"
    return "확인 필요"


def build_screening(
    item: dict[str, Any],
    appraisal: int | None,
    minimum_bid: int | None,
    minimum_bid_percent: float | None,
    address_info: dict[str, Any],
    fail_count: int,
) -> dict[str, Any]:
    flags: list[str] = []
    score = 50
    if fail_count:
        flags.append(f"유찰 {fail_count}회")
        score -= min(fail_count * 4, 24)
    if minimum_bid_percent is not None:
        flags.append(f"최저가율 {minimum_bid_percent:g}%")
        if minimum_bid_percent <= 30:
            score -= 12
        elif minimum_bid_percent <= 50:
            score -= 6
    share = parse_share_info(address_info)
    if share["is_share_sale"]:
        flags.append("지분 매각 의심")
        score -= 15
    if not address_info.get("clean"):
        flags.append("주소 확인 필요")
        score -= 10
    if not appraisal or not minimum_bid:
        flags.append("가격 정보 확인 필요")
        score -= 8
    if any(keyword in str(item.get("status", "")) for keyword in TERMINAL_STATUS_KEYWORDS):
        flags.append("종료성 상태")
        score -= 20
    flags.append("권리확인 필요")
    risk_level = "낮음" if score >= 65 else "보통" if score >= 40 else "높음"
    return {
        "score": max(0, min(100, score)),
        "risk_level": risk_level,
        "flags": flags,
    }


def public_auction_detail(item: dict[str, Any]) -> dict[str, Any]:
    summary = public_auction_summary(item)
    summary.update(
        {
            "first_seen_at": item.get("first_seen_at", ""),
            "last_changed_at": item.get("last_changed_at", ""),
            "next_check_at": item.get("next_check_at", ""),
            "is_active": bool(item.get("is_active")),
            "crawl_priority": item.get("crawl_priority", 0),
            "raw": item.get("raw", {}),
            "detail": item.get("detail", {}),
            "events": item.get("events", []),
            "detail_collection": {
                "status": item.get("detail_status", "pending"),
                "collected_at": item.get("detail_collected_at", ""),
                "checked_at": item.get("detail_checked_at", ""),
                "next_retry_at": item.get("detail_next_retry_at", ""),
                "fail_count": item.get("detail_fail_count", 0),
                "error": item.get("detail_error", ""),
            },
            "documents": [
                {
                    **document,
                    "url": (
                        f"/api/v1/documents/{document.get('id')}"
                        if document.get("status") == "collected" and document.get("file_size")
                        else ""
                    ),
                }
                for document in item.get("documents", [])
            ],
            "assets": [
                {**asset, "url": f"/api/v1/assets/{asset.get('id')}"}
                for asset in item.get("assets", [])
            ],
        }
    )
    return summary
