"""DB의 원시 물건 레코드를 꽁지맵이 쓰는 v1 공개 스키마로 변환하는 순수 함수들.

주소·가격·면적·지분 파싱, 스크리닝 점수, 물건유형/등기 힌트 추론이 여기 모인다.
전부 부작용 없는 순수 함수라 web(HTTP)과 cli(스냅샷 export)가 공유하고 단독 테스트된다.
web.py가 이 모듈을 re-export하므로 `from .web import public_auction_detail` 등 기존 경로는 유지된다."""
from __future__ import annotations

import json
import re
from typing import Any, Iterator
from urllib.parse import urlparse

from . import __version__
from .common import TERMINAL_STATUS_KEYWORDS, asset_object_name

BRACKET_RE = re.compile(r"\[([^\]]+)]")
PAREN_RE = re.compile(r"\(([^)]*)\)")
MONEY_RE = re.compile(r"[\d,]+")
PERCENT_RE = re.compile(r"\((\d+(?:\.\d+)?)%\)")
AREA_RE = re.compile(r"([\d,.]+)\s*㎡")
FAIL_COUNT_RE = re.compile(r"유찰\s*(\d+)")
LOT_NUMBER_RE = re.compile(r"(?:산\s*)?\d+(?:-\d+)?")

# 1평 = 400/121 ㎡. 경매 정보는 평 단위로 읽는 사람이 많아 ㎡와 나란히 싣는다.
SQM_PER_PYEONG = 400 / 121

# 비고에 문장으로 섞여 오는 특수권리. 목록에서 태그로 걸러 볼 수 있게 뽑아낸다.
# 값이 태그 이름, 키가 비고에서 찾을 표현이다.
SPECIAL_RIGHT_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("유치권", "유치권"),
    ("법정지상권", "법정지상권"),
    ("분묘", "분묘기지권"),
    ("대항력", "대항력있는임차인"),
    ("선순위", "선순위임차인"),
    ("별도등기", "별도등기"),
    ("농지취득", "농지취득자격증명"),
    ("맹지", "맹지"),
    ("위반건축물", "위반건축물"),
    ("재매각", "재매각"),
    ("일괄매각", "일괄매각"),
    ("공유자우선매수", "공유자우선매수"),
    ("제시외", "제시외건물"),
)


def to_pyeong(sqm: float | None) -> float | None:
    if not sqm or sqm <= 0:
        return None
    return round(sqm / SQM_PER_PYEONG, 2)


def price_per_pyeong(amount: int | None, sqm: float | None) -> int | None:
    """평당 단가. 전용면적이 있으면 그 기준, 없으면 넘겨받은 면적 기준이다."""
    pyeong = to_pyeong(sqm)
    if not amount or not pyeong:
        return None
    return int(round(amount / pyeong))


# 물건상태에 이 말이 있으면 재매각이다. 낙찰됐다 깨진 물건이라 보증금이 20%로 오른다.
# 법원 사이트 오타를 그대로 받는다 — '대금납부'가 아니라 '대급납부'다. '대금'으로
# 찾으면 정상 납부 353건이 걸리고 정작 '대금미납'과 구별이 안 된다.
RESALE_MARKS: tuple[str, ...] = ("대금미납", "매각불허", "매각허가취소")

CASE_ITEM_TABLE_CAPTION = "물건내역"

# 형식적경매 4종. '공유물분할을위한경매'처럼 이름에 '형식적'이 없는 것이 있다.
FORMAL_AUCTION_MARKS: tuple[str, ...] = ("형식적경매", "공유물분할", "청산을위한")


def _case_tables(detail: Any, caption: str) -> Iterator[dict[str, Any]]:
    """사건 화면의 표를 caption으로 찾는다.

    탭을 옮겨 다니며 받은 스냅샷이라 같은 표가 세 리스트에 중복해 들어 있다.
    리스트 이름을 믿지 말고 셋 다 훑는다.
    """
    case = (detail or {}).get("case") if isinstance(detail, dict) else None
    for key in ("case_tables", "filing_and_service_tables", "schedule_tables"):
        for table in (case or {}).get(key) or []:
            if isinstance(table, dict) and caption in str(table.get("caption") or "").replace(" ", ""):
                yield table


def parse_case_item(detail: Any, item_no: Any) -> dict[str, Any]:
    """사건 화면 '물건내역'에서 **이 물건 번호의** 상태·비고·보증금을 뽑는다.

    물건내역은 물건번호마다 표가 따로라 형제 물건이 안 섞인다(기일내역과 다르다).

        ['물건번호', '1', '물건용도', '상가', '감정평가액 (최저매각가격) (매수신청보증금)',
         '304,000,000원 (8,557,000원) (1,711,400원)']
        ['물건상태', '매각준비 -> 매각공고 -> 매각 -> 매각허가결정 -> 대금미납']
        ['물건비고', '특별매각조건: 매수신청보증금 최저매각가격의 20%']
    """
    wanted = re.sub(r"\D", "", str(item_no or ""))
    empty = {"status_flow": "", "note": "", "resale_reason": "", "deposit_amount": None, "deposit_rate": None}
    for table in _case_tables(detail, CASE_ITEM_TABLE_CAPTION):
        rows = [row for row in table.get("rows") or [] if isinstance(row, list)]
        head = next((row for row in rows if row and str(row[0]).strip() == "물건번호"), None)
        if head is None or len(head) < 2:
            continue
        if wanted and re.sub(r"\D", "", str(head[1])) != wanted:
            continue
        found = dict(empty)
        for row in rows:
            label = str(row[0]).strip() if row else ""
            if label == "물건상태" and len(row) > 1:
                found["status_flow"] = str(row[1]).strip()
            elif label == "물건비고" and len(row) > 1:
                found["note"] = str(row[1]).strip()
        money = [parse_money_text(part) for part in re.findall(r"[\d,]+원", " ".join(str(v) for v in head))]
        # 감정평가액 (최저매각가격) (매수신청보증금) 순서다. 보증금만 쓰고 비율은 최저가 대비다.
        if len(money) >= 3 and money[2]:
            found["deposit_amount"] = money[2]
            if money[1]:
                found["deposit_rate"] = round(money[2] / money[1], 3)
        found["resale_reason"] = next((mark for mark in RESALE_MARKS if mark in found["status_flow"]), "")
        return found
    return empty


def parse_case_type(detail: Any) -> str:
    """사건 기본내역에서 사건명을 뽑는다 — 부동산임의경매 / 부동산강제경매 / 형식적경매 등.

    임의경매는 담보권 실행(근저당 등)이고 강제경매는 집행권원(판결 등)이다. 근거가
    다르면 취소 가능성과 권리 관계가 달라져 권리 분석이 갈린다. 공유물분할·청산을
    위한 형식적경매는 성격이 아예 다르다.

        ['사건번호', '2024타경178', '사건명', '부동산강제경매']
    """
    for table in _case_tables(detail, "사건기본내역"):
        for row in table.get("rows") or []:
            if not isinstance(row, list):
                continue
            for index, cell in enumerate(row[:-1]):
                if str(cell).strip() == "사건명":
                    return str(row[index + 1]).strip()
    return ""


def parse_case_closing(detail: Any) -> dict[str, str]:
    """사건 기본내역에서 종국결과·종국일자를 뽑는다.

    목록 화면의 진행상태는 '신건'과 '유찰 N회' 둘뿐이라, **취하된 물건은 상태 변화
    없이 그냥 사라진다.** 왜 사라졌는지는 사건 화면만 안다.

        ['종국결과', '미종국', '종국일자']
        ['종국결과', '취하', '종국일자', '2026.09.09']
    """
    found = {"result": "", "date": ""}
    for table in _case_tables(detail, "사건기본내역"):
        for row in table.get("rows") or []:
            if not isinstance(row, list):
                continue
            for index, cell in enumerate(row[:-1]):
                label = str(cell).strip()
                if label == "종국결과" and not found["result"]:
                    found["result"] = str(row[index + 1]).strip()
                elif label == "종국일자" and not found["date"]:
                    found["date"] = str(row[index + 1]).strip()
        if found["result"]:
            return found
    return found


# 법원은 화면의 당사자명을 첫 글자만 남기고 가린다(광OOOOOOO). 은행/캐피탈/공공을
# 이름으로 가르는 것은 **불가능하다**. 다만 이름의 '모양'으로 개인과 기관은 갈린다 —
# 개인은 성 한 자 + 가림 두 자(이OO)이고, 기관은 길거나 괄호·공백이 붙는다.
INDIVIDUAL_NAME_RE = re.compile(r"[가-힣]O{1,2}")


def parse_case_parties(detail: Any) -> dict[str, Any]:
    """사건 화면 '당사자 내역'을 구조화한다.

    **이름은 가려져 있어도 당사자 '구분'은 안 가려진다.** 거기에 권리 분석 신호가 있다 —
    교부권자·압류권자는 조세 체납, 임차인·임차권자는 대항력, 공유자는 우선매수다.

        ['채권자', '광OOOOOOO', '채무자겸소유자', '이OO']
    """
    counts: dict[str, int] = {}
    creditor_individual: bool | None = None
    for table in _case_tables(detail, "당사자내역"):
        for row in table.get("rows") or []:
            if not isinstance(row, list):
                continue
            for index in range(0, len(row) - 1, 2):
                kind = str(row[index]).strip()
                name = str(row[index + 1]).strip()
                if not kind or not name or kind == "당사자구분":
                    continue
                counts[kind] = counts.get(kind, 0) + 1
                if kind == "채권자" and creditor_individual is None:
                    creditor_individual = bool(INDIVIDUAL_NAME_RE.fullmatch(name))
        if counts:
            break
    return {"counts": counts, "creditor_individual": creditor_individual}


def parse_money_text(value: Any) -> int | None:
    digits = re.sub(r"[^\d]", "", str(value or ""))
    return int(digits) if digits else None


def parse_special_rights(*texts: Any) -> list[str]:
    haystack = " ".join(str(text or "") for text in texts)
    if not haystack.strip():
        return []
    found: list[str] = []
    for keyword, label in SPECIAL_RIGHT_KEYWORDS:
        if keyword in haystack and label not in found:
            found.append(label)
    return found


def days_until(date_text: str) -> int | None:
    """매각기일까지 남은 날. 지난 기일은 음수."""
    from datetime import date as _date

    match = re.search(r"(\d{4})[.\-](\d{2})[.\-](\d{2})", str(date_text or ""))
    if not match:
        return None
    try:
        target = _date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None
    return (target - _date.today()).days


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
        # 목록 카드용 대표 사진. 웹에는 DB가 없으므로 스토리지 객체 이름을 실어 보낸다.
        "thumbnail": (
            asset_object_name(item.get("thumb_sha256", ""), item.get("thumb_content_type", ""))
            if item.get("thumb_sha256")
            else ""
        ),
    }
    summary.update(public_auction_enrichment(item))
    return summary


BUILDING_SUMMARY_FIELDS = ("main_purpose", "use_apr_day", "hhld_cnt", "grnd_flr_cnt")


def build_registry_summary(item: dict[str, Any]) -> dict[str, Any]:
    """목록에도 싣는 공공 부가정보 최소 필드(건축물대장 주용도, 용도지역).

    법원 '용도'가 그룹 라벨로만 오는 물건은 주용도가 유일한 단서고, 토지는 용도지역이
    그렇다. 상세만 갖고 있으면 지도 한 화면에 수천 건을 그리는 쪽이 물건마다 상세를
    부를 수밖에 없다. 목록 조회는 SQL에서 뽑은 평평한 별칭으로, 상세 조회는 이미
    풀어둔 딕셔너리로 값이 들어온다. 어느 쪽이든 같은 모양으로 낸다."""
    building = item.get("building") if isinstance(item.get("building"), dict) else {}
    land_use = item.get("land_use") if isinstance(item.get("land_use"), dict) else {}
    return {
        "building": {
            field: building.get(field) or item.get(f"building_{field}") or None
            for field in BUILDING_SUMMARY_FIELDS
        },
        "land_use": {"zone": land_use.get("zone") or item.get("land_use_zone") or None},
    }


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


def build_sold(item: dict[str, Any], appraisal: int | None) -> dict[str, Any] | None:
    """낙찰 결과. 낙찰가율은 감정가 대비다. 아직 안 팔렸으면 None."""
    amount = item.get("sold_amount")
    if not amount:
        return None
    return {
        "amount": int(amount),
        "date": normalize_date_text(item.get("sold_date", "")),
        "rate": round(int(amount) / appraisal, 4) if appraisal else None,
    }


def build_past_sales(item: dict[str, Any], appraisal: int | None) -> list[dict[str, Any]]:
    """전에 낙찰됐다 깨진 기록. "1억 3천에 낙찰됐다가 대금미납으로 다시 나왔다"를 만든다.

    **법원은 이걸 안 준다.** 매각이 실효되면 사건 기일내역에서 금액을 지우기 때문이다
    (실측: 매각행 뒤가 '납부'면 금액이 남고 5,604건, '미납'이면 사라진다 3,526건).
    그래서 우리가 낙찰 시점에 받아둔 `auction_sale_results`가 유일한 기록이다.
    `retire_sold_amount`가 물건이 되살아날 때 낙찰가를 그리로 옮겨 두고 있다.

    지금 기일보다 **이전**의 매각만 고른다. 이번 회차 낙찰은 `sold`가 따로 싣는다.
    """
    current = str(item.get("sale_date") or "")
    out: list[dict[str, Any]] = []
    for row in item.get("sale_results") or []:
        amount = row.get("sale_amount")
        when = str(row.get("sale_date") or "")
        if not amount or not when:
            continue
        if current and when >= current:
            continue
        out.append({
            "date": normalize_date_text(when),
            "amount": int(amount),
            "rate": round(int(amount) / appraisal, 4) if appraisal else None,
            # 왜 깨졌는지. 물건상태에서 읽는다 — 매각 기록 자체에는 사유가 없다.
            "broken_by": item.get("resale_reason") or "",
        })
    out.sort(key=lambda r: r["date"], reverse=True)
    return out


def build_sale_results(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """매각결과검색에서 받은 기일별 결과. 낙찰가율은 감정가 대비다."""
    results = []
    for row in rows:
        amount = row.get("sale_amount")
        appraisal = row.get("appraisal")
        rate = None
        if amount and appraisal:
            rate = round(amount / appraisal, 4)
        results.append(
            {
                "sale_date": normalize_date_text(row.get("sale_date", "")),
                "result": row.get("result", ""),
                "sold": amount is not None,
                "sale_amount": amount,
                "minimum_bid": row.get("minimum_bid"),
                "appraisal": appraisal,
                "sale_rate": rate,
                "collected_at": row.get("collected_at", ""),
            }
        )
    return results


def public_auction_enrichment(item: dict[str, Any]) -> dict[str, Any]:
    appraisal = parse_first_money(item.get("appraisal"))
    minimum_bid = parse_first_money(item.get("minimum_bid"))
    minimum_bid_percent = parse_bid_percent(item.get("minimum_bid"), appraisal, minimum_bid)
    address_info = parse_property_address(item.get("address", ""))
    요항표 = str((item.get("detail") or {}).get("appraisal_summary") or "")
    fail_count = parse_fail_count(item.get("status", ""))
    active = parse_item_active(item)
    screening = build_screening(item, appraisal, minimum_bid, minimum_bid_percent, address_info, fail_count)
    area = parse_area_info(address_info)
    # 평당가는 전용(건물)면적 기준이 관례다. 토지만 있는 물건은 토지면적으로 잡는다.
    unit_sqm = area.get("building_sqm") or area.get("land_sqm") or area.get("total_sqm")
    raw = item.get("raw")
    if not isinstance(raw, dict):
        try:
            raw = json.loads(item.get("raw_json") or "{}")
        except (TypeError, ValueError):
            raw = {}
    # 사건 화면 물건내역은 여태 받아만 놓고 안 읽었다. 재매각 여부와 보증금이 여기 있다.
    # 값은 상세를 저장할 때 컬럼으로 뽑아 둔다 — 목록 조회는 detail_json을 안 읽는다.
    # 아직 안 뽑힌 행(예전 수집분)은 상세가 손에 있으면 그 자리에서 읽는다.
    case_item = {
        "resale_reason": item.get("resale_reason") or "",
        "status_flow": item.get("item_status_flow") or "",
        "deposit_amount": item.get("deposit_amount"),
        "deposit_rate": item.get("deposit_rate"),
        "note": item.get("item_note") or "",
    }
    if not case_item["status_flow"] and item.get("detail"):
        case_item = parse_case_item(item.get("detail"), item.get("item_no"))
    case_type = item.get("case_type") or parse_case_type(item.get("detail"))
    parties = item.get("parties")
    if not isinstance(parties, dict):
        try:
            parties = json.loads(item.get("parties_json") or "{}")
        except (TypeError, ValueError):
            parties = {}
    if not parties.get("counts") and item.get("detail"):
        parties = parse_case_parties(item.get("detail"))
    closing = {
        "result": item.get("closing_result") or "",
        "date": normalize_date_text(item.get("closing_date") or ""),
    }
    if not closing["result"] and item.get("detail"):
        raw_closing = parse_case_closing(item.get("detail"))
        closing = {"result": raw_closing["result"], "date": normalize_date_text(raw_closing["date"])}

    # 웹이 딱지로 쓰는 배열은 이것 하나다. 다른 곳에서 이미 아는 사실도 여기 없으면
    # 화면에 안 보인다(G02에서 지분매각이 그랬다).
    flags = parse_special_rights(
        raw.get("비고"), item.get("address"), address_info.get("detail"), case_item["note"]
    )
    if case_item["resale_reason"] and "재매각" not in flags:
        flags.append("재매각")
    if parse_share_info(address_info)["is_share_sale"] and "지분매각" not in flags:
        flags.append("지분매각")
    # 형식적경매는 성격이 아예 다르다. '공유물분할을위한경매'는 이름에 '형식적'이 없지만
    # 형식적경매다 — 이름만 보면 놓친다(실측 83건).
    if any(mark in case_type for mark in FORMAL_AUCTION_MARKS) and "형식적경매" not in flags:
        flags.append("형식적경매")

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
            # 임의(담보권 실행) / 강제(집행권원) / 형식적경매. 권리 분석이 갈리는 구분이다.
            "case_type": case_type,
            # 취하·기각·취소는 목록 상태로는 절대 알 수 없다. 물건이 왜 사라졌는지는
            # 사건 화면의 종국결과만 말해 준다.
            "closing": closing,
            # 이름은 가려져 있어도 당사자 '구분'은 안 가려진다. 교부권자·압류권자는
            # 조세 체납, 임차인은 대항력, 공유자는 우선매수 신호다.
            "parties": {
                "counts": parties.get("counts") or {},
                "creditor_individual": parties.get("creditor_individual"),
            },
            "fail_count": fail_count,
            "is_active": active,
            "days_until_sale": days_until(item.get("sale_date", "")),
            "special_rights": flags,
            # 재매각이면 매수신청보증금이 최저가의 20%다(보통 10%). 모르고 가면
            # 보증금 부족으로 입찰이 그 자리에서 무효가 된다. 목록에서 구별돼야 한다.
            "resale": {
                "is_resale": bool(case_item["resale_reason"]),
                "reason": case_item["resale_reason"],
                "status_flow": case_item["status_flow"],
            },
            "deposit": {
                "amount": case_item["deposit_amount"],
                "rate": case_item["deposit_rate"],
            },
            # 낙찰되면 목록에서 조용히 사라질 뿐 status는 '유찰 N회'에 머문다.
            # 낙찰가를 여기 실어야 목록에서 바로 '얼마에 팔렸는지'가 보인다.
            "sold": build_sold(item, appraisal),
            # 전에 낙찰됐다 깨진 기록. 재매각이면 보증금이 오르고(위 deposit),
            # 직전 낙찰가는 시세의 강한 단서다. 법원은 실효된 낙찰가를 지우므로
            # 우리가 그때 받아둔 것이 유일한 기록이다.
            "past_sales": build_past_sales(item, appraisal),
            # 받아 두고 안 꺼내던 것들(G18) — 청구금액·배당요구종기·개시일·
            # 임차인·가압류·지상권 유무. 새로 긁지 않고 상세 표에서 뽑는다.
            **build_case_basics(item.get("detail") or {}, parties),
            # 다음 기일·최저가 **추정**(G18). 법원별 실측 체감률을 받아서 쓴다 —
            # 일률적으로 30%를 깎으면 전체의 24%(서울남부·광주 등 80% 법원)에서
            # 틀린 금액을 사실처럼 보여준다. 각 줄에 `estimated`·`basis` 가 붙는다.
            "projected_sales": project_future_sales(
                minimum_bid, item.get("sale_date", ""),
                item.get("court_reduction_rate")) if active else [],
            # 법원 사이트의 다수조회·다수관심 화면에서 받은 인기도. 상위 물건에만 값이 있다.
            "popularity": {
                "view_count": item.get("view_count") or (item.get("popularity") or {}).get("view_count"),
                "interest_count": item.get("interest_count")
                or (item.get("popularity") or {}).get("interest_count"),
            },
            "detail_url": safe_external_url(item.get("detail_url", "")),
        },
        "property": {
            "category": item.get("category", ""),
            "type_guess": infer_property_type(item.get("category", ""), item.get("address", "")),
            "address": address_info,
            "area": {
                **area,
                "total_pyeong": to_pyeong(area.get("total_sqm")),
                "land_pyeong": to_pyeong(area.get("land_sqm")),
                "building_pyeong": to_pyeong(area.get("building_sqm")),
            },
            "share": parse_share_info(address_info),
            "registry_search_hint": build_registry_search_hint(address_info, item.get("category", "")),
            # 감정평가서 PDF 는 협회 서버라 못 받지만(G06), 법원이 화면에 주는 요항표
            # 요약은 받는다 — 위치·주위환경·교통·건물 구조·이용상태·설비내역이
            # 평가사가 쓴 문장 그대로다. 실측 94~1,458자.
            "appraisal_summary": 요항표,
            # **문장이 아니라 사실을 싣는다.** 화면은 이쪽을 쓴다 — 검색·필터·비교가
            # 되고, 평가사가 쓴 문장을 그대로 옮기지 않아도 된다.
            "appraisal_facts": appraisal_facts(요항표),
            **build_registry_summary(item),
        },
        "price": {
            "appraisal": appraisal,
            "minimum_bid": minimum_bid,
            "minimum_bid_rate": round(minimum_bid_percent / 100, 4) if minimum_bid_percent is not None else None,
            "minimum_bid_percent": minimum_bid_percent,
            "appraisal_per_pyeong": price_per_pyeong(appraisal, unit_sqm),
            "minimum_bid_per_pyeong": price_per_pyeong(minimum_bid, unit_sqm),
            "per_pyeong_basis": (
                "building" if area.get("building_sqm") else ("land" if area.get("land_sqm") else "total")
            ),
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
    category = str(category or "").strip()
    # 법원 '용도'에는 개별 용도('아파트')와 검색 그룹 라벨('상가,오피스텔,근린시설')이
    # 섞여 온다. 쉼표가 있으면 '이 셋 중 하나'라는 뜻이지 특정 용도가 아니다. 통째로
    # 부분 문자열로 훑으면 활성 4천 건이 전부 '오피스텔'이 된다. 라벨은 단정하지 않는다.
    text = address if "," in category else f"{category} {address}"
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
    return category or "기타"


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


# 감정평가서 원본은 한국감정평가사협회 서버에 있고, 그 뷰어 페이지가
# **"무단 복제 및 링크하여 사용하는 경우 위반사항에 따라 형사처벌"** 을 명시한다.
# 그래서 받아 둔 파일이 있어도 내려받기 주소를 내보내지 않는다 — 재배포가 된다.
# 화면에 보여줄 내용은 법원이 직접 공개하는 `property.appraisal_summary` 로 간다.
RESTRICTED_DOCUMENTS = ("감정평가서",)

# 요항표는 `N) 제목 내용` 이 이어 붙은 글이다. 제목 어휘 15개로 실측 94%를 맞춘다.
# 양식이 둘이라 둘 다 담는다 — 구분건물(건물의 구조·설비내역)과 토지(형태·제시목록 외).
_APPRAISAL_HEADINGS = (
    "위치 및 주위환경", "교통상황", "건물의 구조", "이용상태", "설비내역",
    "토지의 형상 및 이용상태", "형태 및 이용상태", "인접 도로상태등", "인접 도로상태",
    "토지이용계획 및 제한상태", "제시목록 외의 물건", "공부와의 차이",
    "부합물 및 종물", "기타참고사항", "임대관계",
)
_APPRAISAL_SPLIT = re.compile(r"(?:(?<=\s)|^)\d{1,2}\)\s*")
# '비었다' 는 말이 여러 모양이다. **띄어쓰기까지 지우고** 봐야 한다 —
# 실측에서 `없 음.` 이 120건으로 1위였고, 그걸 못 걸러 '공부와의 차이 있음' 이
# 64%로 부풀었다. 실제로 차이가 있는 물건은 훨씬 적다.
_BLANK_RE = re.compile(
    r"^(?:[ㅡ\-–—~.·:：]*|없음|없슴|없습니다|해당사항없음|해당사항없습니다|"
    r"해당없음|해당없습니다|미상|불명|알수없음)[.]?$")


def split_appraisal_summary(text: str) -> dict[str, str]:
    """요항표 글을 항목별로 가른다. 제목은 알려진 어휘로만 맞춘다.

    번호로만 자르면 본문 속 `1) 2)` 나열까지 항목으로 오인한다. 어휘를 두면
    못 맞춘 조각은 조용히 버려지고, 그 비율(실측 6%)이 곧 어휘를 넓힐 신호가 된다.
    """
    out: dict[str, str] = {}
    for 조각 in _APPRAISAL_SPLIT.split(str(text or "")):
        조각 = 조각.strip()
        for 제목 in _APPRAISAL_HEADINGS:
            if 조각.startswith(제목):
                본문 = 조각[len(제목):].strip(" :：")
                if 본문 and 제목 not in out:
                    out[제목] = 본문
                break
    return out


def _has_content(value: str) -> bool:
    """'없 음.', '해당사항 없음', '-' 처럼 **비었다는 뜻의 표기**를 내용으로 세지 않는다."""
    깎음 = re.sub(r"\s+", "", str(value or ""))
    return bool(깎음) and not _BLANK_RE.match(깎음)


def detail_key_values(detail: dict[str, Any]) -> dict[str, str]:
    """상세 표의 `라벨 | 값` 쌍을 한 사전으로 모은다.

    법원 표는 `라벨,값,라벨,값` 이 한 줄에 들어가는 모양이라 두 칸씩 짚어야 한다.
    **이미 받아 두고 안 꺼내던 것들이다** — 실측 200건에서 배당요구종기·청구금액·
    사건접수·경매개시일·입찰방법이 전부 100% 들어 있었다(G18).
    """
    out: dict[str, str] = {}
    for table in (detail or {}).get("tables") or []:
        for row in table.get("rows") or []:
            cells = [str(c).strip() for c in row]
            for i in range(len(cells) - 1):
                label, value = cells[i], cells[i + 1]
                if 2 <= len(label) <= 12 and value and label not in out:
                    out[label] = value
    return out


def build_case_basics(detail: dict[str, Any], parties: Any = None) -> dict[str, Any]:
    """옥션원이 한 장에 싣는 것 중 **우리가 받아 두고 안 꺼내던 것**(G18).

    청구금액은 그 자체로 신호다 — 감정가보다 크면 남는 게 없을 수 있다.
    배당요구종기는 지났는지 여부가 임차인 대항력 판단에 걸린다.
    """
    kv = detail_key_values(detail)
    out: dict[str, Any] = {
        "filed_at": kv.get("사건접수", ""),
        "opened_at": kv.get("경매개시일", ""),
        "dividend_deadline": kv.get("배당요구종기", ""),
        "bid_method": kv.get("입찰방법", ""),
        "claim_amount": parse_first_money(kv.get("청구금액", "")),
    }
    # 임차인이 있는지는 당사자 내역이 말한다. **이름은 싣지 않는다** — 있고 없고만이
    # 판단에 필요하고, 실명은 마스킹 정책의 대상이다(PRIVACY-MASKING.md).
    # `parties` 는 리스트가 아니라 `{"counts": {"임차인": 1, ...}}` 모양이다.
    종류 = (parties or {}).get("counts") if isinstance(parties, dict) else None
    종류 = 종류 if isinstance(종류, dict) else {}
    def 있나(*말들: str) -> bool:
        return any(any(말 in 이름 for 말 in 말들) and 수 for 이름, 수 in 종류.items())
    out["has_tenant"] = 있나("임차인")
    out["has_seizure"] = 있나("가압류", "압류")
    # 법정지상권은 건물만 낙찰받고 땅을 못 쓰는 대표적 함정이다.
    out["has_surface_right"] = 있나("지상권")
    return out


# 기일 간격 중앙 35일(실측 44,524쌍, 25~75%가 35~42일).
NEXT_SALE_GAP_DAYS = 35
DEFAULT_REDUCTION_RATE = 70   # 전국 최빈. 법원별 실측값이 있으면 그것을 쓴다.


def project_future_sales(
    minimum_bid: int | None,
    sale_date: str,
    reduction_rate: int | None = None,
    rounds: int = 3,
) -> list[dict[str, Any]]:
    """다음 기일과 최저가를 **추정**한다. 사실이 아니라 계산이다.

    옥션원은 아직 안 잡힌 3차·4차를 계산해서 보여준다. 입찰자가 '얼마까지
    기다릴까' 를 판단하는 정보라 값은 크다. 다만 **틀리면 비용도 크다.**

    그래서 일률적으로 30%를 깎지 않는다. 실측 44,524쌍에서 70%가 75%·80%가 24%인데
    **법원마다 갈린다**(인천·수원·부산 70% · 서울남부·광주 80%). 0.7 을 일괄
    적용하면 전체의 24%에서 틀린 금액을 사실처럼 보여준다.
    `store.court_reduction_rates()` 가 법원별 실측값을 준다.

    돌려주는 각 줄에 `estimated: True` 와 `basis` 를 붙인다. **화면은 이것을
    반드시 '예상' 으로 표시해야 한다** — 법원이 정한 값이 아니다.
    """
    from datetime import date as _date, timedelta as _td  # noqa: PLC0415

    if not minimum_bid or minimum_bid <= 0:
        return []
    m = re.search(r"(\d{4})[.\-](\d{1,2})[.\-](\d{1,2})", str(sale_date or ""))
    if not m:
        return []
    try:
        시작 = _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return []
    # **정수로 곱하고 나눈다.** 실수로 하면 254,100,000 × 0.7 이 177,869,999 가
    # 되어 법원 금액과 1원씩 어긋난다(옥션원 표시값과 대조해 확인).
    비율 = int(reduction_rate or DEFAULT_REDUCTION_RATE)
    out: list[dict[str, Any]] = []
    값 = int(minimum_bid)
    날 = 시작
    for _ in range(max(rounds, 0)):
        값 = 값 * 비율 // 100
        날 = 날 + _td(days=NEXT_SALE_GAP_DAYS)
        out.append({
            "sale_date": 날.strftime("%Y.%m.%d"),
            "minimum_bid": 값,
            "estimated": True,
            "basis": f"유찰 시 {비율}% · 기일 간격 {NEXT_SALE_GAP_DAYS}일(실측 중앙)",
        })
    return out


def appraisal_facts(text: str) -> dict[str, Any]:
    """요항표에서 **사실만** 뽑는다. 평가사가 쓴 문장은 그대로 옮기지 않는다.

    사실(사용승인일·구조·형상·도로·설비)은 저작권 대상이 아니고, 무엇보다
    **검색·필터·비교가 된다.** 1,500자 문단은 사람이 안 읽는다.

    경매의 대표적 함정 셋이 이 글에 그대로 들어 있다.
      · 제시목록 외의 물건 — 매각에서 빠지는 수목·구조물
      · 공부와의 차이     — 장부의 지목·면적과 현황이 다름
      · 맹지             — 도로에 안 붙은 땅
    지금 위험도가 모든 물건에 붙이는 '권리확인 필요' 보다 훨씬 쓸모 있다(G14).
    """
    항목 = split_appraisal_summary(text)
    전체 = str(text or "")
    facts: dict[str, Any] = {}

    도로 = 항목.get("인접 도로상태등") or 항목.get("인접 도로상태") or ""
    if 도로:
        facts["도로"] = {
            "맹지": ("맹지" in 도로) or ("접하지" in 도로 and "않" in 도로),
            "포장": "포장도로" in 도로,
        }
    facts["제시외물건"] = _has_content(항목.get("제시목록 외의 물건", "")) or _has_content(
        항목.get("부합물 및 종물", ""))
    facts["공부와_차이"] = _has_content(항목.get("공부와의 차이", ""))

    m = re.search(r"사용승인일\s*[:：]?\s*(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", 전체)
    if m:
        facts["사용승인일"] = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"((?:철근|철골|경량철골|벽돌|블록|목|연와|석)[가-힣]*(?:\s*[가-힣]+)?조)", 전체)
    if m:
        facts["구조"] = re.sub(r"\s+", " ", m.group(1)).strip()
    m = re.search(r"((?:부정|정방|장방|세장|사다리|자루|삼각|가로장방|세로장방)형)", 전체)
    if m:
        facts["토지형상"] = m.group(1)
    if "도시가스" in 전체:
        facts["난방"] = "도시가스"
    elif "개별난방" in 전체:
        facts["난방"] = "개별난방"
    elif "중앙난방" in 전체 or "지역난방" in 전체:
        facts["난방"] = "중앙·지역난방"
    facts["승강기"] = "승강기" in 전체
    return facts


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
            "building": item.get("building") or {},
            "land_use": item.get("land_use") or {},
            "transactions": item.get("transactions") or {},
            "events": item.get("events", []),
            "sale_results": build_sale_results(item.get("sale_results", [])),
            "detail_collection": {
                "status": item.get("detail_status", "pending"),
                "collected_at": item.get("detail_collected_at", ""),
                "checked_at": item.get("detail_checked_at", ""),
                "next_retry_at": item.get("detail_next_retry_at", ""),
                "fail_count": item.get("detail_fail_count", 0),
                "error": item.get("detail_error", ""),
            },
            # `metadata_json` 은 내보내지 않는다. 수집 당시의 원본 부스러기라 화면이
            # 쓸 일이 없는데, 감정평가서 것에는 **한국감정평가사협회 뷰어·PDF 주소**가
            # 들어 있다. 그 페이지가 "무단 복제 및 **링크하여** 사용하는 경우 형사처벌"
            # 을 명시하므로, 우리 payload 에 실어 내보내는 것 자체가 그 링크 사용이다.
            # 진단이 필요하면 DB 원본(`auction_documents.metadata_json`)을 본다.
            "documents": [
                {
                    **{k: v for k, v in document.items()
                       if k not in ("metadata_json", "file_path")},
                    "url": (
                        f"/api/v1/documents/{document.get('id')}"
                        if document.get("status") == "collected"
                        and document.get("file_size")
                        and document.get("document_type") not in RESTRICTED_DOCUMENTS
                        else ""
                    ),
                }
                for document in item.get("documents", [])
            ],
            "assets": [
                {
                    **asset,
                    "url": f"/api/v1/assets/{asset.get('id')}",
                    # 웹(Vercel)에는 DB가 없어서 위의 숫자 id로는 사진을 찾을 수 없다.
                    # 객체 스토리지에 올라간 이름을 같이 실어 보내야 서명 URL을 만든다.
                    "object_key": asset_object_name(asset.get("sha256", ""), asset.get("content_type", "")),
                }
                for asset in item.get("assets", [])
            ],
        }
    )
    return summary
