"""브이월드 토지이용계획(getLandUseAttr)에서 용도지역·지구를 조회한다.

옥션원 리포트의 '토지이용계획'에 해당한다. 땅에 뭘 지을 수 있는지가 여기서 갈리므로
경매 판단에 크게 쓰인다.

지오코딩으로 확보한 PNU(19자리)를 그대로 넘기면 되고, 응답은 한 필지에 여러 줄이
온다(예: 계획관리지역 + 가축사육제한구역 + 성장관리계획구역). 그중 '용도지역'에
해당하는 항목을 대표값으로 뽑고 나머지는 지구·구역 목록으로 남긴다.
지오코더와 같은 VWORLD_API_KEY를 쓴다."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .geocoder import env_value, ssl_context

BASE_URL = "https://api.vworld.kr/ned/data/getLandUseAttr"

# 용도지역은 '무엇을 지을 수 있는가'를 정하므로 대표값으로 올리고, 나머지
# (구역·지구)는 제약 조건이라 목록으로만 남긴다.
ZONE_SUFFIXES = ("지역",)

# 응답에는 '도시지역'(상위 구분)과 '제2종일반주거지역'(실제 용도지역)이 함께 온다.
# 사람이 알고 싶은 건 뒤쪽이므로 국토계획법상 용도지역 21종에 해당하는 것을 먼저 고른다.
# '관리지역'을 그대로 두면 '생활소음진동관리지역'까지 잡히므로 실제 용도지역명만 적는다.
ZONE_CORE_WORDS = (
    "주거지역",
    "상업지역",
    "공업지역",
    "녹지지역",
    "계획관리지역",
    "생산관리지역",
    "보전관리지역",
    "농림지역",
    "자연환경보전지역",
)


@dataclass(slots=True)
class LandUse:
    zone: str = ""                 # 대표 용도지역 (예: 제1종일반주거지역)
    zones: list[str] = field(default_factory=list)      # 용도지역 전체
    districts: list[str] = field(default_factory=list)  # 지구·구역 등 그 밖의 제한
    legal_dong: str = ""           # 법정동명
    lot_number: str = ""           # 지번
    updated_at: str = ""           # 자료 기준일
    source: str = "vworld_land_use"
    detail: dict[str, Any] = field(default_factory=dict)


def fetch_land_use(pnu: str) -> LandUse | None:
    key = env_value("VWORLD_API_KEY")
    text = str(pnu or "").strip()
    if not key or len(text) != 19 or not text.isdigit():
        return None
    payload = _request(key, text)
    rows = _rows(payload)
    if not rows:
        return None

    zones: list[str] = []
    districts: list[str] = []
    for row in rows:
        name = str(row.get("prposAreaDstrcCodeNm") or "").strip()
        if not name:
            continue
        bucket = zones if name.endswith(ZONE_SUFFIXES) else districts
        if name not in bucket:
            bucket.append(name)

    first = rows[0]
    return LandUse(
        zone=pick_primary_zone(zones),
        zones=zones,
        districts=districts,
        legal_dong=str(first.get("ldCodeNm") or "").strip(),
        lot_number=str(first.get("mnnmSlno") or "").strip(),
        updated_at=str(first.get("lastUpdtDt") or "").strip(),
        detail={"count": len(rows)},
    )


def pick_primary_zone(zones: list[str]) -> str:
    """용도지역 목록에서 대표 하나를 고른다.

    '도시지역'처럼 상위 구분만 올리면 정작 알고 싶은 '제2종일반주거지역'이 묻힌다."""
    for zone in zones:
        if any(word in zone for word in ZONE_CORE_WORDS):
            return zone
    return zones[0] if zones else ""


def _rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """응답은 한 건일 때 dict, 여러 건일 때 list로 온다. 둘 다 리스트로 맞춘다."""
    field_value = ((payload or {}).get("landUses") or {}).get("field")
    if isinstance(field_value, dict):
        return [field_value]
    if isinstance(field_value, list):
        return [row for row in field_value if isinstance(row, dict)]
    return []


def _request(key: str, pnu: str) -> dict[str, Any]:
    params = {
        "pnu": pnu,
        "format": "json",
        "numOfRows": "100",
        "pageNo": "1",
        "key": key,
    }
    domain = env_value("VWORLD_API_DOMAIN")
    if domain:
        params["domain"] = domain
    request = Request(f"{BASE_URL}?{urlencode(params)}", headers={"User-Agent": "court-auction-crawler/0.1"})
    timeout = float(env_value("LAND_USE_TIMEOUT") or "10")
    with urlopen(request, timeout=timeout, context=ssl_context()) as response:
        return json.loads(response.read().decode("utf-8"))
