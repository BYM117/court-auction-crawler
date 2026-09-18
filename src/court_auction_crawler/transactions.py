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
from .geocoder import _extract_building_hint, env_value, ssl_context

BASE_URL = "https://apis.data.go.kr/1613000"

# category 키워드 → (매매 op, 전월세 op, 단지명 필드 후보)
API_SETS = {
    "apart": ("RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade", "RTMSDataSvcAptRent/getRTMSDataSvcAptRent", ("aptNm",)),
    "villa": ("RTMSDataSvcRHTrade/getRTMSDataSvcRHTrade", "RTMSDataSvcRHRent/getRTMSDataSvcRHRent", ("mhouseNm",)),
    "officetel": ("RTMSDataSvcOffiTrade/getRTMSDataSvcOffiTrade", "RTMSDataSvcOffiRent/getRTMSDataSvcOffiRent", ("offiNm",)),
    "land": ("RTMSDataSvcLandTrade/getRTMSDataSvcLandTrade", "", ()),
    # 단독·다가구와 상가·근린시설도 국토부가 준다. 안 부르고 있어서 활성 2,100건이
    # '대상외' 로 빠져 있었다 — 못 맞춘 게 아니라 **묻지도 않았다**.
    # 둘 다 `umdNm`·`jibun` 을 주므로 필지 대조가 그대로 걸린다.
    "house": ("RTMSDataSvcSHTrade/getRTMSDataSvcSHTrade",
              "RTMSDataSvcSHRent/getRTMSDataSvcSHRent", ()),
    "commercial": ("RTMSDataSvcNrgTrade/getRTMSDataSvcNrgTrade", "", ("bldgNm",)),
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
    """물건 종류를 실거래 API 갈래로 옮긴다. 빈 문자열이면 **실거래가 없는 물건**이다.

    예전에는 아파트·빌라·오피스텔·토지 넷뿐이라 근린시설 1,545건·상가 182건·
    단독다가구 309건이 통째로 '대상외' 였다. 못 맞춘 게 아니라 **묻지도 않았다.**
    자동차·중기(774건)는 진짜로 부동산 실거래가 없으므로 그대로 빈 문자열이다.
    """
    text = f"{category} {address}"
    if re.search(r"아파트", text):
        return "apart"
    if "오피스텔" in text:
        return "officetel"
    if re.search(r"다세대|연립|빌라", text):
        return "villa"
    # 자동차·중기·선박은 부동산이 아니다. 아래 갈래에 섞이기 전에 먼저 걸러낸다.
    if re.search(r"자동차|중기|선박|항공기|건설기계", text):
        return ""
    if re.search(r"단독주택|다가구", text):
        return "house"
    if re.search(r"근린시설|상가|업무시설|공장|창고|숙박|판매시설", text):
        return "commercial"
    if re.search(r"임야|대지|잡종지|과수원|목장용지|공장용지|도로|하천|구거|전\b|답\b|토지", text):
        return "land"
    return ""


def fetch_transactions(
    pnu: str,
    category: str,
    address: str = "",
    *,
    normalized_address: str = "",
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
    # 정규화 주소가 없으면(지오코딩 전) 원본에서라도 뽑아 본다. 원본은 브이월드가
    # 확인한 것이 아니라 법원이 적은 것이므로 두 번째 선택이다.
    parcel = parcel_key(normalized_address) or parcel_key(address)
    ymds = _recent_months(months)

    sales = _collect(key, sale_op, lawd, ymds, building, name_fields, cache, parcel)
    rents = (_collect(key, rent_op, lawd, ymds, building, name_fields, cache, parcel)
             if rent_op else None)

    if sales is None and rents is None:
        return None
    return {
        "type": kind,
        "building": building,
        "parcel": f"{parcel[0]} {parcel[1]}" if parcel else "",
        "sales": _summarize(sales, "sales", max_recent) if sales is not None else None,
        "rent": _summarize(rents, "rent", max_recent) if rents is not None else None,
    }


def _collect(
    key, operation, lawd, ymds, building, name_fields, cache=None, parcel=None
) -> tuple[list[dict[str, Any]], str] | None:
    """(거래목록, 얼마나 좁혔는지). 미등록/오류 API면 None을 돌려 스킵을 알린다.

    좁히는 순서가 곧 믿을 수 있는 순서다.

        parcel   같은 필지        — 이 물건이 있는 바로 그 땅의 거래
        name     같은 단지명      — 이름이 같으니 같은 건물일 것이다
        dong     같은 법정동      — 동네 시세
        sigungu  같은 시군구      — 그냥 그 구 전체다

    **마지막 둘은 이 물건의 값이 아니다.** 예전에는 넷을 `matched` 불리언 하나로
    뭉개 놓고 주석에 '인근(법정동) 전체'라 적어 뒀는데, 조회 단위가 `pnu[:5]`라
    실제로는 시군구였다(실측 중앙값 774건). 화면이 그 주석대로 '○○동 평균'이라
    쓰면 거짓말이 나간다.
    """
    got_any = False
    rows: list[dict[str, Any]] = []
    for ymd in ymds:
        page = _request_cached(key, operation, lawd, ymd, cache)
        if page is None:  # 미등록/오류 → 이 API 전체 스킵
            return (rows, "sigungu") if got_any else None
        got_any = True
        rows.extend(page)

    같은필지 = [r for r in rows if _parcel_matches(r, parcel)]
    if 같은필지:
        return 같은필지, "parcel"
    if building:
        같은이름 = [r for r in rows if _name_matches(r, name_fields, building)]
        if 같은이름:
            return 같은이름, "name"
    if parcel:
        # 필지도 이름도 못 맞췄으면 적어도 **같은 동**으로는 좁힌다. 응답에 이미
        # `umdNm`이 있어 추가 호출이 없다.
        같은동 = [r for r in rows if _text(r.get("umdNm")) == parcel[0]]
        if 같은동:
            return 같은동, "dong"
    return rows, "sigungu"


def _request_cached(key, operation, lawd, ymd, cache) -> list[dict[str, Any]] | None:
    if cache is None:
        return _request_rtms(key, operation, lawd, ymd)
    ck = (operation, lawd, ymd)
    if ck not in cache:
        cache[ck] = _request_rtms(key, operation, lawd, ymd)
    return cache[ck]


def _summarize(collected: tuple[list[dict[str, Any]], str], kind: str, max_recent: int) -> dict[str, Any]:
    rows, level = collected
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
        # `matched`는 예전 이름이라 그대로 둔다(뺄 때는 웹과 맞춰야 한다). 뜻은
        # '이 물건의 값이라 말해도 되는가'다. 어느 수준인지는 `match_level`이 말한다.
        "matched": level in ("parcel", "name"),
        "match_level": level,        # parcel | name | dong | sigungu
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


_PARCEL_RE = re.compile(r"([가-힣]+(?:동|리|가))\s+(산\s*)?(\d+)(?:-(\d+))?")


def parcel_key(normalized_address: str) -> tuple[str, str] | None:
    """브이월드가 확인해 준 정규화 주소에서 (법정동, 지번)을 뽑는다.

    **이름이 아니라 필지로 대조하려는 것이다.** 법원이 쓰는 단지명과 국토부가 쓰는
    단지명이 애초에 다르다 — `가산양우내안애애플` vs `가산양우내안에애플`(애/에),
    `달동엠타운` vs `달동 M타운`(엠/M). 정규화를 얹어도 못 붙는다.

    반면 실거래 응답에는 행마다 `umdNm`(법정동)과 `jibun`(지번)이 붙어 온다. 우리도
    지오코딩 때 같은 것을 받아 저장해 뒀다. 실측: 활성 물건의 **97%**가 `verified`
    등급의 동+지번을 갖고 있다(단지명이 뽑히는 것은 13%).
    """
    match = _PARCEL_RE.search(str(normalized_address or ""))
    if not match:
        return None
    동, 산, 본번, 부번 = match.groups()
    지번 = ("산 " if 산 else "") + 본번 + (f"-{부번}" if 부번 else "")
    return 동, 지번


def _parcel_matches(row: dict[str, Any], parcel: tuple[str, str] | None) -> bool:
    if not parcel:
        return False
    동, 지번 = parcel
    # 지번만 대면 안 된다. 같은 시군구 안에 같은 지번이 동마다 있다.
    return (_text(row.get("umdNm")) == 동
            and _text(row.get("jibun")).replace(" ", "") == 지번.replace(" ", ""))


def _building_name(address: str) -> str:
    """물건 주소에서 단지명을 추출한다. '(봉천동,샤롯캐슬)'의 마지막 항목이나
    괄호 밖 한글 건물명."""
    text = str(address or "")
    for group in re.findall(r"\(([^)]*)\)", text):
        candidate = group.split(",")[-1].strip()
        if len(candidate) >= 2 and not re.fullmatch(r"[가-힣]{1,3}동", candidate):
            return _normalize_name(candidate)
    # 괄호가 없는 지번 주소(`군산시 소룡동 1323-13 동아아파트 107동`)는 위에서
    # 아무것도 못 뽑는다. 법원 주소의 절반이 이 모양이라 단지명 확보가 13%에
    # 그쳤다. 지오코더가 같은 일을 이미 하므로 그것을 쓴다(실측 13% → 50%).
    return _normalize_name(_extract_building_hint(text))


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
