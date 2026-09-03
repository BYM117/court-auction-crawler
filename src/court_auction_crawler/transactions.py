"""국토교통부 실거래가(RTMSDataSvc)에서 인근 매매·전월세 시세를 조회한다.

옥션원 리포트의 '국토부 실거래가' 블록에 해당한다. 실거래가 API는 법정동코드(앞5자리)
+ 계약년월 단위로 그 동네 전체 거래를 XML로 돌려주므로, 받아온 뒤 물건의 단지명·
전용면적으로 필터링해 '같은 단지/같은 평형' 시세로 정리한다.

물건 유형별로 API가 다르다(아파트/연립다세대/오피스텔 매매·전월세, 토지 매매).
아직 활용신청이 반영되지 않은 API는 조용히 건너뛴다(HTTP 403/미등록).
공공데이터포털 키(PUBLIC_DATA_SERVICE_KEY)를 쓴다."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import re
from typing import Any
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from .common import RateLimitError, is_rate_limited
from .geocoder import env_value, ssl_context

BASE_URL = "https://apis.data.go.kr/1613000"

# category 키워드 → (매매 op, 전월세 op, 단지명 필드 후보)
API_SETS = {
    "apart": ("RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade", "RTMSDataSvcAptRent/getRTMSDataSvcAptRent", ("aptNm",)),
    "villa": ("RTMSDataSvcRHTrade/getRTMSDataSvcRHTrade", "RTMSDataSvcRHRent/getRTMSDataSvcRHRent", ("mhouseNm",)),
    "officetel": ("RTMSDataSvcOffiTrade/getRTMSDataSvcOffiTrade", "RTMSDataSvcOffiRent/getRTMSDataSvcOffiRent", ("offiNm",)),
    "land": ("RTMSDataSvcLandTrade/getRTMSDataSvcLandTrade", "", ()),
}
NAME_FIELDS = ("aptNm", "offiNm", "mhouseNm", "bldgNm")
AMOUNT_FIELDS = ("dealAmount",)
AREA_FIELDS = ("excluUseAr", "dealArea", "plottageAr")


@dataclass(slots=True)
class TransactionSummary:
    kind: str                       # sales / rent
    count: int = 0
    min_amount: int = 0             # 매매: 만원 / 전월세: 보증금 만원
    avg_amount: int = 0
    max_amount: int = 0
    recent: list[dict[str, Any]] = field(default_factory=list)


def classify_transaction_kind(category: str, address: str = "") -> str:
    text = f"{category} {address}"
    if re.search(r"아파트", text):
        return "apart"
    if "오피스텔" in text:
        return "officetel"
    if re.search(r"다세대|연립|빌라", text):
        return "villa"
    if re.search(r"임야|대지|잡종지|과수원|목장용지|공장용지|도로|하천|구거|전\b|답\b|토지", text):
        return "land"
    return ""


def fetch_transactions(
    pnu: str,
    category: str,
    address: str = "",
    *,
    months: int = 6,
    max_recent: int = 12,
    cache: dict[tuple[str, str, str], Any] | None = None,
) -> dict[str, Any] | None:
    """물건 인근 실거래를 조회해 매매·전월세 요약과 최근 거래 목록을 돌려준다.

    cache를 주면 (operation, 법정동, 계약년월) 단위로 응답을 재사용한다. 같은 동네
    물건이 몰려 있어(평균 100건/법정동) 대량 백필 시 API 호출을 20배 이상 줄인다."""
    key = env_value("PUBLIC_DATA_SERVICE_KEY")
    lawd = str(pnu or "")[:5]
    kind = classify_transaction_kind(category, address)
    if not key or not lawd.isdigit() or len(lawd) != 5 or not kind:
        return None

    sale_op, rent_op, name_fields = API_SETS[kind]
    building = _building_name(address)
    ymds = _recent_months(months)

    sales = _collect(key, sale_op, lawd, ymds, building, name_fields, cache)
    rents = _collect(key, rent_op, lawd, ymds, building, name_fields, cache) if rent_op else None

    if sales is None and rents is None:
        return None
    return {
        "type": kind,
        "building": building,
        "sales": _summarize(sales, "sales", max_recent) if sales is not None else None,
        "rent": _summarize(rents, "rent", max_recent) if rents is not None else None,
    }


def _collect(key, operation, lawd, ymds, building, name_fields, cache=None) -> tuple[list[dict[str, Any]], bool] | None:
    """(거래목록, 단지매칭여부). 미등록/오류 API면 None을 돌려 스킵을 알린다."""
    got_any = False
    rows: list[dict[str, Any]] = []
    for ymd in ymds:
        page = _request_cached(key, operation, lawd, ymd, cache)
        if page is None:  # 미등록/오류 → 이 API 전체 스킵
            return (rows, False) if got_any else None
        got_any = True
        rows.extend(page)
    if building:
        matched = [r for r in rows if _name_matches(r, name_fields, building)]
        if matched:
            return matched, True
    return rows, False  # 단지 매칭 실패 → 인근(법정동) 전체


def _request_cached(key, operation, lawd, ymd, cache) -> list[dict[str, Any]] | None:
    if cache is None:
        return _request_rtms(key, operation, lawd, ymd)
    ck = (operation, lawd, ymd)
    if ck not in cache:
        cache[ck] = _request_rtms(key, operation, lawd, ymd)
    return cache[ck]


def _summarize(collected: tuple[list[dict[str, Any]], bool], kind: str, max_recent: int) -> dict[str, Any]:
    rows, matched = collected
    amounts: list[int] = []
    recent: list[dict[str, Any]] = []
    for r in rows:
        if kind == "sales":
            amount = _num(_first(r, AMOUNT_FIELDS))
        else:
            amount = _num(r.get("deposit"))
        if amount <= 0:
            continue
        amounts.append(amount)
        recent.append({
            "name": _first(r, NAME_FIELDS),
            "area": _numf(_first(r, AREA_FIELDS)),
            "floor": _text(r.get("floor")),
            "amount": amount,
            "monthly": _num(r.get("monthlyRent")) if kind == "rent" else 0,
            "date": _deal_date(r),
            "build_year": _text(r.get("buildYear")),
        })
    recent.sort(key=lambda x: x["date"], reverse=True)
    return {
        "matched": matched,          # True=같은 단지, False=인근(법정동) 전체
        "count": len(amounts),
        "min": min(amounts) if amounts else 0,
        "avg": round(sum(amounts) / len(amounts)) if amounts else 0,
        "max": max(amounts) if amounts else 0,
        "recent": recent[:max_recent],
    }


def _request_rtms(key, operation, lawd, ymd) -> list[dict[str, Any]] | None:
    """실거래 한 달치. 미등록/오류면 None(스킵 신호), 정상이면 거래 리스트(빈 리스트 가능)."""
    params = {"serviceKey": key, "LAWD_CD": lawd, "DEAL_YMD": ymd, "numOfRows": "1000", "pageNo": "1"}
    url = f"{BASE_URL}/{operation}?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "court-auction-crawler/0.1"})
    timeout = float(env_value("PUBLIC_DATA_TIMEOUT") or "8")
    try:
        with urlopen(request, timeout=timeout, context=ssl_context()) as response:
            body = response.read().decode("utf-8", "replace")
    except HTTPError as exc:
        if exc.code == 429:  # 일일 트래픽 한도 초과는 429로 온다
            raise RateLimitError("실거래가 일일 한도 초과(HTTP 429)") from exc
        return None if exc.code in (401, 403) else []
    except (TimeoutError, OSError, URLError):
        return []
    if is_rate_limited(body):
        raise RateLimitError("실거래가 일일 한도 초과")
    if "NORMAL SERVICE" not in body and "<resultCode>00" not in body and "<items>" not in body:
        return None  # 미등록 서비스키 등
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    return [{child.tag: (child.text or "").strip() for child in item} for item in root.iter("item")]


def _recent_months(months: int) -> list[str]:
    today = date.today()
    result = []
    y, m = today.year, today.month
    for _ in range(months):
        result.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return result


def _building_name(address: str) -> str:
    """물건 주소에서 단지명을 추출한다. '(봉천동,샤롯캐슬)'의 마지막 항목이나
    괄호 밖 한글 건물명."""
    text = str(address or "")
    for group in re.findall(r"\(([^)]*)\)", text):
        candidate = group.split(",")[-1].strip()
        if len(candidate) >= 2 and not re.fullmatch(r"[가-힣]{1,3}동", candidate):
            return _normalize_name(candidate)
    return ""


def _name_matches(row: dict[str, Any], name_fields: tuple[str, ...], building: str) -> bool:
    fields = name_fields or NAME_FIELDS
    for f in fields:
        name = _normalize_name(row.get(f, ""))
        if name and (name in building or building in name):
            return True
    return False


def _normalize_name(value: Any) -> str:
    return re.sub(r"[\s\-_()]+", "", str(value or ""))


def _deal_date(r: dict[str, Any]) -> str:
    y = _text(r.get("dealYear")); m = _text(r.get("dealMonth")); d = _text(r.get("dealDay"))
    if y and m:
        return f"{y}-{int(m):02d}-{int(d):02d}" if d else f"{y}-{int(m):02d}"
    return ""


def _first(r: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if r.get(k) not in (None, ""):
            return r[k]
    return ""


def _num(value: Any) -> int:
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return 0


def _numf(value: Any) -> float:
    try:
        return round(float(str(value).replace(",", "").strip()), 2)
    except (TypeError, ValueError):
        return 0.0


def _text(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() == "none" else text
