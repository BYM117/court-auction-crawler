"""PNU로 공시가격(공시기준가)을 조회한다.

지오코딩으로 확보한 PNU를 이용해 유형별 공시가격을 VWorld에서 읽어 온다.
- 토지: 개별공시지가(원/㎡) × 토지면적
- 아파트/빌라/다세대: 공동주택가격(세대 총액, 주소의 동/호로 세대 특정)
- 단독/다가구: 개별주택가격(총액)

오피스텔·상가는 VWorld가 아니라 국세청 기준시가(로컬 파일)라 여기서 다루지 않는다.
꽁지맵이 로컬 인덱스로 처리한다.

지오코딩과 동일한 VWORLD_API_KEY를 쓴다(추가 승인 불필요).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .geocoder import env_value, ssl_context


HO_RE = re.compile(r"제?\s*(\d+)호")
DIGIT_DONG_RE = re.compile(r"(?:^|[\s(])제?(\d+[A-Za-z]?)동(?=[^가-힣0-9]|$)")
LETTER_DONG_RE = re.compile(r"(?:^|\s)([가나다라마바사아자차카타파하])동(?=[^가-힣0-9]|$)")
BRACKET_RE = re.compile(r"\[[^\]]*]")


@dataclass(slots=True)
class OfficialPrice:
    value: float                      # 공시기준가 총액(원)
    price_type: str                   # 개별공시지가 / 공동주택가격 / 개별주택가격
    year: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


def classify_official_kind(category: str, address: str = "") -> str:
    text = f"{category} {address}"
    if re.search(r"아파트|다세대|연립|공동주택|빌라", text):
        return "commonHousing"
    if "오피스텔" in text:
        return "officetel"
    if re.search(r"단독|다가구|주택", text):
        return "detachedHousing"
    if re.search(r"임야|대지|잡종지|과수원|목장용지|공장용지|도로|하천|구거|체육용지", text) or re.search(r"\b[전답]\b", text):
        if not re.search(r"건물|아파트|다세대|연립|빌라|주택|오피스텔|상가|공장|창고|근린", text):
            return "land"
    return "mixed"


def fetch_official_price(
    *,
    pnu: str,
    kind: str,
    address: str = "",
    land_area: float = 0.0,
    year: int | None = None,
) -> OfficialPrice | None:
    """PNU와 유형으로 공시기준가를 조회한다. 실패하면 None."""
    key = env_value("VWORLD_API_KEY") or env_value("PUBLIC_DATA_SERVICE_KEY")
    if not key or not re.fullmatch(r"\d{19}", str(pnu or "")):
        return None

    from datetime import datetime

    base_year = year or datetime.now().year
    years = [str(y) for y in (base_year, base_year - 1, base_year - 2) if y > 2000]

    if kind == "land":
        return _fetch_land(key, pnu, land_area, years)
    if kind == "commonHousing":
        return _fetch_apart(key, pnu, address, years)
    if kind == "detachedHousing":
        return _fetch_indvd_housing(key, pnu, years)
    return None


def _fetch_land(key: str, pnu: str, land_area: float, years: list[str]) -> OfficialPrice | None:
    for year in years:
        rows = _request_ned(key, "getIndvdLandPriceAttr", "indvdLandPrices", pnu, year, num_rows=10)
        first = next((r for r in rows if _num(r.get("pblntfPclnd")) > 0), None)
        if first:
            per_sqm = _num(first.get("pblntfPclnd"))
            area = land_area if land_area > 0 else 0
            if area <= 0:
                return None
            return OfficialPrice(
                value=round(per_sqm * area),
                price_type="개별공시지가",
                year=year,
                detail={"perSqm": per_sqm, "landArea": area},
            )
    return None


def _fetch_apart(key: str, pnu: str, address: str, years: list[str]) -> OfficialPrice | None:
    dong, ho = _parse_unit_from_address(address)
    for year in years:
        rows = _request_ned(key, "getApartHousingPriceAttr", "apartHousingPrices", pnu, year, num_rows=1000, max_pages=5)
        if not rows:
            continue
        match = _pick_apart_unit(rows, dong, ho)
        if match:
            return OfficialPrice(
                value=_num(match.get("pblntfPc")),
                price_type="공동주택가격",
                year=year,
                detail={
                    "name": match.get("aphusNm", ""),
                    "dong": match.get("dongNm", ""),
                    "ho": match.get("hoNm", ""),
                    "area": _num(match.get("prvuseAr")) or None,
                },
            )
        return None  # 세대 목록은 있는데 동/호가 안 맞으면 연도를 바꿔도 동일하다
    return None


def _fetch_indvd_housing(key: str, pnu: str, years: list[str]) -> OfficialPrice | None:
    for year in years:
        rows = _request_ned(key, "getIndvdHousingPriceAttr", "indvdHousingPrices", pnu, year, num_rows=10)
        first = next((r for r in rows if _num(r.get("housePc")) > 0), None)
        if first:
            return OfficialPrice(
                value=_num(first.get("housePc")),
                price_type="개별주택가격",
                year=year,
                detail={
                    "landArea": _num(first.get("ladRegstrAr")) or None,
                    "buildingArea": _num(first.get("buldCalcTotAr")) or None,
                },
            )
    return None


def _pick_apart_unit(rows: list[dict[str, Any]], dong: str, ho: str) -> dict[str, Any] | None:
    if not ho:
        return None
    ho_matches = [r for r in rows if _digits(r.get("hoNm")) == _digits(ho)]
    if not ho_matches:
        return None
    if dong:
        dong_matches = [r for r in ho_matches if _digits(r.get("dongNm")) == _digits(dong)]
        if dong_matches:
            return dong_matches[0]
        return ho_matches[0] if len(ho_matches) == 1 else None
    if len(ho_matches) == 1:
        return ho_matches[0]
    prices = {str(r.get("pblntfPc")) for r in ho_matches}
    return ho_matches[0] if len(prices) == 1 else None


def _parse_unit_from_address(address: str) -> tuple[str, str]:
    text = BRACKET_RE.sub(" ", str(address or ""))
    ho = ""
    ho_matches = HO_RE.findall(text)
    if ho_matches:
        ho = ho_matches[-1]
    dong = ""
    digit_dong = DIGIT_DONG_RE.findall(text)
    if digit_dong:
        dong = digit_dong[-1]
    else:
        letter_dong = LETTER_DONG_RE.findall(text)
        if letter_dong:
            dong = letter_dong[-1]
    return _norm(dong), _norm(ho)


def _request_ned(
    key: str,
    endpoint: str,
    bucket: str,
    pnu: str,
    year: str,
    *,
    num_rows: int = 10,
    max_pages: int = 1,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        params = {
            "key": key,
            "pnu": pnu,
            "stdrYear": year,
            "format": "json",
            "numOfRows": str(num_rows),
            "pageNo": str(page),
        }
        domain = env_value("VWORLD_API_DOMAIN")
        if domain:
            params["domain"] = domain
        try:
            payload = _request(f"https://api.vworld.kr/ned/data/{endpoint}", params)
        except (TimeoutError, OSError, URLError, json.JSONDecodeError):
            break
        page_rows = (((payload or {}).get(bucket) or {}).get("field")) or []
        if isinstance(page_rows, dict):
            page_rows = [page_rows]
        rows.extend(page_rows)
        if len(page_rows) < num_rows:
            break
    return rows


def _request(base_url: str, params: dict[str, str]) -> dict[str, Any]:
    url = f"{base_url}?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "court-auction-crawler/0.1"})
    timeout = float(env_value("GEOCODER_TIMEOUT") or "5")
    with urlopen(request, timeout=timeout, context=ssl_context()) as response:
        return json.loads(response.read().decode("utf-8"))


def _num(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(re.sub(r"[^0-9.\-]", "", str(value)) or 0)
    except ValueError:
        return 0.0


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _norm(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^제", "", text)
    text = re.sub(r"(동|층|호)$", "", text)
    return re.sub(r"\s+", "", text).upper()
