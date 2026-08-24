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
# 명륜3가·종로1가처럼 '동' 없이 숫자+가로 끝나는 법정동도 지번 코어로 인정한다.
LOT_CORE_RE = re.compile(r"^(.+?(?:읍|면|동(?:\d+가)?|\d+가|리)\s+산?\s*\d+(?:-\d+)?)\b")

# 법원 주소는 구명칭(강원도·전라북도)·약칭(서울)이 흔한데 VWorld는 현행
# 공식 명칭을 반환하므로, 시도 비교는 정규화를 거쳐야 한다.
SIDO_ALIASES = {
    "서울": ("서울", "서울시", "서울특별시"),
    "부산": ("부산", "부산시", "부산광역시"),
    "대구": ("대구", "대구시", "대구광역시"),
    "인천": ("인천", "인천시", "인천광역시"),
    # 광주광역시와 전라남도는 전남광주통합특별시로 합쳐졌다. 지도 API가 통합명으로
    # 답하는데 우리가 옛 이름과 비교하면 멀쩡한 좌표를 지역 불일치로 버리게 된다.
    # 셋을 한 정규형으로 묶어 두 이름 모두 통과시킨다(시군구까지 비교하므로 안전).
    "전남광주": ("광주", "광주시", "광주광역시", "전남", "전라남도", "전남광주통합특별시"),
    "대전": ("대전", "대전시", "대전광역시"),
    "울산": ("울산", "울산시", "울산광역시"),
    "세종": ("세종", "세종시", "세종특별자치시"),
    "경기": ("경기", "경기도"),
    "강원": ("강원", "강원도", "강원특별자치도"),
    "충북": ("충북", "충청북도"),
    "충남": ("충남", "충청남도"),
    "전북": ("전북", "전라북도", "전북특별자치도"),
    "경북": ("경북", "경상북도"),
    "경남": ("경남", "경상남도"),
    "제주": ("제주", "제주도", "제주특별자치도"),
}
_SIDO_CANONICAL = {alias: canonical for canonical, aliases in SIDO_ALIASES.items() for alias in aliases}

# 자동차·중기 등 동산 경매의 주소는 물건 위치가 아니라 사용본거지라서
# 지도에 올리면 오해를 부른다. 지오코딩과 지도 노출 대상에서 제외한다.
MOVABLE_CATEGORY_KEYWORDS = (
    "자동차", "승용", "승합", "화물", "차량", "중기", "건설기계",
    "덤프", "지게차", "굴삭기", "선박", "항공기",
)


def normalize_sido(token: str) -> str:
    return _SIDO_CANONICAL.get(str(token or "").strip(), str(token or "").strip())


MOVABLE_ADDRESS_KEYWORDS = ("사용본거지", "선적항", "[선박", "동력선", "어선", "부선", "예인선")


def is_mappable_property(address: str, category: str = "") -> bool:
    address_text = str(address or "")
    if any(word in address_text for word in MOVABLE_ADDRESS_KEYWORDS):
        return False
    category_text = str(category or "")
    return not any(word in category_text for word in MOVABLE_CATEGORY_KEYWORDS)


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

    # 검색 API가 놓치는 주소를 좌표 변환(getcoord) API로 한 번 더 시도한다.
    # 매칭 엔진이 달라 회수율이 올라가고, 법정동코드로 PNU도 조립할 수 있다.
    for query in queries:
        for category in ("PARCEL", "ROAD"):
            result = _try_getcoord(key, query, category, address)
            if result:
                return result

    # 지번이 없는 블록·로트형 주소는 건물명 장소 검색으로 근사 좌표라도 확보한다.
    return _try_place_search(key, address)


def _try_getcoord(key: str, query: str, category: str, address: str) -> GeocodeResult | None:
    try:
        payload = _request_vworld_getcoord(key, query, category)
    except (TimeoutError, OSError, URLError, json.JSONDecodeError):
        return None
    result = payload.get("result") if isinstance(payload, dict) else None
    point = (result or {}).get("point") or {}
    if not point.get("x") or not point.get("y"):
        return None
    refined = payload.get("refined") or {}
    refined_text = str(refined.get("text") or "")
    if not _matches_region(address, [refined_text]):
        return None
    return GeocodeResult(
        lat=float(point["y"]),
        lng=float(point["x"]),
        pnu=_pnu_from_structure(refined.get("structure") or {}),
        normalized_address=refined_text or query,
        query=query,
        source="address",
        quality="verified",
    )


def _try_place_search(key: str, address: str) -> GeocodeResult | None:
    """신규 분할 필지처럼 주소 검색이 모르는 물건을 건물명 장소 검색으로 구제한다.

    시군구+법정동까지 붙인 쿼리를 먼저 시도하고, 같은 지역 결과 중에서
    주소의 동 표기(비동→B동 등)와 맞는 항목을 우선 고른다.
    """
    building = _extract_building_hint(address)
    if not building:
        return None
    normalized = normalize_auction_address(address)
    tokens = re.sub(r"\s+", " ", PAREN_RE.sub(" ", normalized)).strip().split()
    queries: list[str] = []
    for width in (3, 2):
        query = " ".join([*tokens[:width], building]).strip()
        if query and query not in queries:
            queries.append(query)

    dong_variants = _extract_building_dong_variants(address)
    for query in queries:
        try:
            items = _request_vworld_search_items(key, query, request_type="PLACE", size=5)
        except (TimeoutError, OSError, URLError, json.JSONDecodeError):
            continue
        candidates = [
            item
            for item in items
            if isinstance(item, dict)
            and (item.get("point") or {}).get("x")
            and (item.get("point") or {}).get("y")
            and _same_region(address, item)
        ]
        if not candidates:
            continue
        picked = next(
            (item for item in candidates if any(v in str(item.get("title") or "") for v in dong_variants)),
            candidates[0],
        )
        point = picked["point"]
        return GeocodeResult(
            lat=float(point["y"]),
            lng=float(point["x"]),
            pnu="",
            normalized_address=str(picked.get("address", {}).get("parcel") or picked.get("address", {}).get("road") or query),
            query=query,
            source="building",
            quality="approximate",
        )
    return None


# 건물 동 표기(비동, 에이동, 107동)를 장소검색 결과 제목과 대조하기 위한 변환표.
_DONG_LATIN = {
    "에이": "A", "비": "B", "씨": "C", "디": "D", "이": "E", "에프": "F",
    "지": "G", "에이치": "H", "아이": "I", "제이": "J", "케이": "K",
    "엘": "L", "엠": "M", "엔": "N",
}
BUILDING_DONG_RE = re.compile(r"(?:^|\s)(?:제)?([0-9]+|[가-힣]{1,2}|[A-Za-z])동(?=\s|$)")


def _extract_building_dong_variants(address: str) -> list[str]:
    normalized = normalize_auction_address(address)
    without_paren = PAREN_RE.sub(" ", normalized)
    matches = BUILDING_DONG_RE.findall(without_paren)
    if not matches:
        return []
    token = matches[-1]
    variants = [f"{token}동"]
    latin = _DONG_LATIN.get(token)
    if latin:
        variants.append(f"{latin}동")
    return variants


def _pnu_from_structure(structure: dict[str, Any]) -> str:
    """getcoord refined 구조에서 PNU(법정동코드10+산1+본번4+부번4)를 결정적으로 조립한다."""
    code = str(structure.get("level4LC") or "").strip()
    if not re.fullmatch(r"\d{10}", code):
        return ""
    parcel = str(structure.get("level5") or "").strip()
    match = re.fullmatch(r"(산)?\s*(\d{1,4})(?:-(\d{1,4}))?", parcel)
    if not match:
        return ""
    mountain = "2" if match.group(1) else "1"
    main = match.group(2).zfill(4)
    sub = (match.group(3) or "0").zfill(4)
    return f"{code}{mountain}{main}{sub}"


def _extract_building_hint(address: str) -> str:
    normalized = normalize_auction_address(address)
    # "(운서동,응답하라1976)" 같은 괄호의 마지막 항목이 건물명인 경우가 많다.
    paren_groups = PAREN_RE.findall(normalized)
    for group in reversed(paren_groups):
        candidate = group.split(",")[-1].strip()
        if len(candidate) >= 3 and not re.fullmatch(r"[가-힣]{1,3}동", candidate):
            return candidate
    # 블록·로트형/신규 필지 주소는 지번 대신 건물명이 위치 단서다.
    # 동 표기(107동·비동·에이동)와 법정동(당하동)은 건물명이 아니므로 건너뛴다.
    without_paren = re.sub(r"\s+", " ", PAREN_RE.sub(" ", normalized)).strip()
    for token in reversed(without_paren.split()):
        if re.search(r"(?:층|호)$", token) or re.fullmatch(r"(?:제)?(?:\d+[A-Za-z가-힣]?|[가-힣]{1,2}|[A-Za-z])동", token):
            continue
        if (
            len(token) >= 3
            and re.search(r"[가-힣]{3,}", token)
            and not re.search(r"(?:시|군|구|읍|면|동|리|로|길|대로|번길)$", token)
        ):
            return token
    return ""


# 행정구역 개편으로 광주광역시와 전라남도가 전남광주통합특별시로 합쳐졌다.
# 법원경매정보는 옛 이름을 그대로 주고 지도 API는 새 이름만 받아, 이 두 지역
# 지오코딩이 통째로 실패했다(광주 44%, 전남 25.5%가 좌표 없음).
# 옛 이름도 남겨 두 이름을 모두 시도한다.
MERGED_SIDO = (
    ("광주광역시", "전남광주통합특별시"),
    ("전라남도", "전남광주통합특별시"),
    ("전남 ", "전남광주통합특별시 "),
)


def apply_merged_sido(address: str) -> str:
    """옛 시도명으로 시작하는 주소를 통합 시도명으로 바꾼다. 해당 없으면 그대로."""
    text = str(address or "")
    for old_name, new_name in MERGED_SIDO:
        if text.startswith(old_name):
            return new_name + text[len(old_name):]
    return text


def _candidate_queries(address: str) -> list[str]:
    normalized = normalize_auction_address(address)
    candidates = [normalized]
    # 통합 시도명 후보를 옛 이름보다 앞에 둔다. 후보 수에 상한이 있어 뒤에 두면
    # 정작 유일하게 통하는 이름이 잘려나간다.
    merged = apply_merged_sido(normalized)
    if merged != normalized:
        candidates.insert(0, merged)
    without_paren = PAREN_RE.sub(" ", normalized)
    without_paren = re.sub(r"\s+", " ", without_paren).strip()
    candidates.append(without_paren)
    merged_wp = apply_merged_sido(without_paren)
    if merged_wp != without_paren:
        candidates.append(merged_wp)

    for value in (merged, merged_wp, normalized, without_paren):
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
    return _request_vworld_search(key, query, request_type="ADDRESS", category=category)


def _request_vworld_search(
    key: str,
    query: str,
    request_type: str = "ADDRESS",
    category: str | None = None,
) -> dict[str, Any]:
    items = _request_vworld_search_items(key, query, request_type=request_type, category=category, size=1)
    return items[0] if items else {}


def _request_vworld_search_items(
    key: str,
    query: str,
    request_type: str = "ADDRESS",
    category: str | None = None,
    size: int = 1,
) -> list[dict[str, Any]]:
    params = {
        "service": "search",
        "request": "search",
        "version": "2.0",
        "crs": "EPSG:4326",
        "size": str(size),
        "page": "1",
        "type": request_type,
        "format": "json",
        "query": query,
        "key": key,
    }
    if category:
        params["category"] = category
    payload = _request_vworld("https://api.vworld.kr/req/search", params)
    items = payload.get("response", {}).get("result", {}).get("items", []) or []
    return [item for item in items if isinstance(item, dict)]


def _request_vworld_getcoord(key: str, address: str, category: str) -> dict[str, Any]:
    params = {
        "service": "address",
        "request": "getcoord",
        "version": "2.0",
        "crs": "EPSG:4326",
        "type": category,
        "address": address,
        "refine": "true",
        "simple": "false",
        "format": "json",
        "key": key,
    }
    payload = _request_vworld("https://api.vworld.kr/req/address", params)
    return payload.get("response", {}) or {}


def _request_vworld(base_url: str, params: dict[str, str]) -> dict[str, Any]:
    domain = env_value("VWORLD_API_DOMAIN")
    if domain:
        params = {**params, "domain": domain}
    url = f"{base_url}?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "court-auction-crawler/0.1"})
    timeout = float(env_value("GEOCODER_TIMEOUT") or "3")
    with urlopen(request, timeout=timeout, context=_ssl_context()) as response:
        return json.loads(response.read().decode("utf-8"))


def _ssl_context() -> ssl.SSLContext | None:
    if env_value("GEOCODER_INSECURE_SSL") == "1":
        return ssl._create_unverified_context()
    try:
        # macOS 기본 파이썬은 CA 번들이 비어 있어 검증에 실패하는 경우가 있다.
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return None


def _same_region(address: str, item: dict[str, Any]) -> bool:
    address_payload = item.get("address") if isinstance(item.get("address"), dict) else {}
    return _matches_region(
        address,
        [
            str(item.get("title") or ""),
            str(address_payload.get("road") or ""),
            str(address_payload.get("parcel") or ""),
        ],
    )


def _matches_region(address: str, returned_texts: list[str]) -> bool:
    source_parts = normalize_auction_address(address).split()
    if len(source_parts) < 2:
        return False
    for returned in returned_texts:
        returned_parts = str(returned or "").split()
        if (
            len(returned_parts) >= 2
            and normalize_sido(source_parts[0]) == normalize_sido(returned_parts[0])
            and source_parts[1] == returned_parts[1]
        ):
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
