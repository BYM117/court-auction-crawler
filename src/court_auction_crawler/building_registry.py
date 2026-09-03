"""공공데이터포털 건축물대장(BldRgstHubService)에서 건축물 개요를 조회한다.

옥션원 리포트의 '건축물정보'에 해당한다. 아파트 같은 집합건물은 대지면적·건폐율·
용적률·총세대수가 동별 표제부가 아니라 총괄표제부에 있으므로 둘을 조합한다.
- 총괄표제부(getBrRecapTitleInfo): 대지면적/건폐율/용적률/총세대수/동수 (단지 전체)
- 표제부(getBrTitleInfo): 구조/주용도/사용승인/지상층수 (주건물)
단독주택·토지 등 총괄표제부가 없는 물건은 표제부만 사용한다.

지오코딩으로 확보한 PNU(19자리)에서 시군구·법정동·번지를 분해해 조회한다.
공공데이터포털 키(PUBLIC_DATA_SERVICE_KEY)를 쓴다."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .common import RateLimitError, is_rate_limited
from .geocoder import env_value, ssl_context

BASE_URL = "https://apis.data.go.kr/1613000/BldRgstHubService"


@dataclass(slots=True)
class BuildingRegistry:
    bld_nm: str = ""
    plat_area: float = 0.0        # 대지면적 ㎡
    arch_area: float = 0.0        # 건축면적 ㎡
    tot_area: float = 0.0         # 연면적 ㎡
    bc_rat: float = 0.0           # 건폐율 %
    vl_rat: float = 0.0           # 용적률 %
    hhld_cnt: int = 0             # 총세대수
    main_bld_cnt: int = 0         # 동수
    grnd_flr_cnt: int = 0         # 지상층수
    use_apr_day: str = ""         # 사용승인일 YYYYMMDD
    structure: str = ""           # 구조
    main_purpose: str = ""        # 주용도
    source: str = "building_registry"
    detail: dict[str, Any] = field(default_factory=dict)


def pnu_parts(pnu: str) -> tuple[str, str, str, str] | None:
    """PNU 19자리 → (sigunguCd5, bjdongCd5, bun4, ji4). 실패 시 None."""
    text = str(pnu or "").strip()
    if len(text) != 19 or not text.isdigit():
        return None
    return text[0:5], text[5:10], text[11:15], text[15:19]


def fetch_building_registry(pnu: str, category: str = "") -> BuildingRegistry | None:
    key = env_value("PUBLIC_DATA_SERVICE_KEY")
    parts = pnu_parts(pnu)
    if not key or parts is None:
        return None
    sigungu, bjdong, bun, ji = parts

    recap = _pick_recap(_request_bld(key, "getBrRecapTitleInfo", sigungu, bjdong, bun, ji))
    title = _pick_title(_request_bld(key, "getBrTitleInfo", sigungu, bjdong, bun, ji))
    if recap is None and title is None:
        return None

    reg = BuildingRegistry()
    # 단지 전체 지표는 총괄표제부 우선, 없으면 표제부로 대체.
    primary = recap or title or {}
    reg.bld_nm = _text(primary.get("bldNm"))
    reg.plat_area = _num(primary.get("platArea"))
    reg.arch_area = _num(primary.get("archArea"))
    reg.tot_area = _num(primary.get("totArea"))
    reg.bc_rat = _num(primary.get("bcRat"))
    reg.vl_rat = _num(primary.get("vlRat"))
    reg.hhld_cnt = int(_num(primary.get("hhldCnt")))
    reg.main_bld_cnt = int(_num(primary.get("mainBldCnt")))
    reg.use_apr_day = _text(primary.get("useAprDay"))
    # 구조·용도·층수는 표제부(동별)가 더 정확하다.
    detail_src = title or recap or {}
    reg.structure = _text(detail_src.get("strctCdNm")) or _text(detail_src.get("etcStrct"))
    reg.main_purpose = _text(detail_src.get("mainPurpsCdNm")) or _text(detail_src.get("etcPurps"))
    reg.grnd_flr_cnt = int(_num(detail_src.get("grndFlrCnt")))
    if not reg.use_apr_day:
        reg.use_apr_day = _text(detail_src.get("useAprDay"))
    reg.detail = {
        "recap": bool(recap),
        "title": bool(title),
    }
    # 아무 값도 못 채웠으면 의미 없는 레코드다.
    if not (reg.plat_area or reg.tot_area or reg.structure or reg.bld_nm):
        return None
    return reg


def _pick_recap(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """총괄표제부는 보통 1건. 대지면적이 있는 것을 고른다."""
    if not rows:
        return None
    scored = [r for r in rows if _num(r.get("platArea")) > 0]
    return (scored or rows)[0]


def _pick_title(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """표제부는 동·부속건물별로 여러 건. 연면적이 가장 큰 주건물을 고른다.
    지하주차장·노인정 등 부속은 연면적이 작아 자연히 밀린다."""
    if not rows:
        return None
    main = [r for r in rows if "주차장" not in _text(r.get("etcPurps")) and "부속" not in _text(r.get("regstrKindCdNm"))]
    candidates = main or rows
    return max(candidates, key=lambda r: _num(r.get("totArea")))


def _request_bld(
    key: str,
    operation: str,
    sigungu: str,
    bjdong: str,
    bun: str,
    ji: str,
    num_rows: int = 30,
) -> list[dict[str, Any]]:
    params = {
        "serviceKey": key,
        "sigunguCd": sigungu,
        "bjdongCd": bjdong,
        "bun": bun,
        "ji": ji,
        "numOfRows": str(num_rows),
        "pageNo": "1",
        "_type": "json",
    }
    url = f"{BASE_URL}/{operation}?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "court-auction-crawler/0.1"})
    timeout = float(env_value("PUBLIC_DATA_TIMEOUT") or "8")
    try:
        with urlopen(request, timeout=timeout, context=ssl_context()) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        if exc.code == 429:  # 일일 트래픽 한도 초과는 429로 온다
            raise RateLimitError("건축물대장 일일 한도 초과(HTTP 429)") from exc
        return []
    except (TimeoutError, OSError, URLError):
        return []
    if is_rate_limited(raw):
        raise RateLimitError("건축물대장 일일 한도 초과")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    body = (payload.get("response") or {}).get("body") or {}
    items = body.get("items") or {}
    if not isinstance(items, dict):
        return []
    item = items.get("item")
    if item is None:
        return []
    return item if isinstance(item, list) else [item]


def _num(value: Any) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _text(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() == "none" else text
