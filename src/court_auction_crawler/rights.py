"""권리 판정 — 문서 본문을 **읽어서** 낙찰자에게 무엇이 남는지 가른다.

`enrichment` 처럼 순수 함수만 둔다(DB·네트워크 없음). Jev 가 필요한 꼬리는
`jev.py` 가 따로 풀어 `item["jev"]` 로 넣어 주고, 여기서는 그 답을 읽기만 한다.

규칙이 먼저다. 법원 문구는 틀에 박혀 있어 규칙이 90% 넘게 맞힌다(2026-09-23 실측,
DEVLOG). Jev 는 규칙이 못 뽑는 모양(칸이 찢어진 PDF 날짜 등)과, 규칙과 엇갈리는 것을
찾아 정답지를 키우는 데 쓴다 — 규칙이 틀리는 새 유형은 거기서 발견된다.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

# ── 날짜 ────────────────────────────────────────────────────────────────

_DATE = re.compile(r"(?<!\d)(\d{4}|\d{2})\s?[.\-]\s?(\d{1,2})\s?[.\-]\s?(\d{1,2})(?!\d)")


def parse_date(text: Any) -> date | None:
    """'2024.09.26.' · '22.07.18' · '2021. 2. 8' 를 날짜로. 두 자리 연도는 2000년대."""
    m = _DATE.search(str(text or ""))
    if not m:
        return None
    y, mo, d = (int(g) for g in m.groups())
    if y < 100:
        y += 2000
    try:
        return date(y, mo, d)
    except ValueError:
        return None


# ── ① 최선순위 설정일 (말소기준) ─────────────────────────────────────────
# 매각물건명세서 PDF 를 글로 뽑은 것이라 칸이 뒤섞인다. 실측 36,653건에서 옛 규칙 88.2%,
# 아래 규칙 95.3%. 나머지는 날짜와 권리 종류가 다른 줄로 찢어진 것·목록별로 갈린 것이라
# 규칙으로 쫓지 않고 Jev 가 날짜 후보 중에서 고른다(`senior_candidates`).

_SENIOR = re.compile(
    r"최선순위[\s\S]{0,160}?(?<!\d)(\d{4}|\d{2})\s?\.\s?(\d{1,2})\s?\.\s?(\d{1,2})\s?\.?\s*"
    r"(?:자\s*)?[-(\[\s]*(?:신청)?"
    r"(근저?당권?|근정당권?|전세권|가압류|압류|담보가등기|가등기|강제경매개시?|임의경매개시?|"
    r"경매개시|개시결정|저당권|가처분)"
)
_KIND_NORMAL = (("근정당", "근저당권"), ("근저당", "근저당권"), ("근당", "근저당권"),
                ("강제경매", "강제경매개시결정"), ("임의경매", "임의경매개시결정"),
                ("경매개시", "경매개시결정"), ("개시결정", "경매개시결정"))


def senior_right(text: Any) -> dict[str, str] | None:
    """매각물건명세서 본문에서 최선순위 설정일과 권리 종류. 못 뽑으면 None."""
    m = _SENIOR.search(str(text or ""))
    if not m:
        return None
    d = parse_date(".".join(m.groups()[:3]))
    if d is None:
        return None
    kind = m.group(4)
    for needle, name in _KIND_NORMAL:
        if needle in kind:
            kind = name
            break
    return {"date": d.isoformat(), "kind": kind, "source": "rule"}


def senior_candidates(text: Any) -> tuple[str, list[str]]:
    """규칙이 못 뽑았을 때 Jev 에게 줄 (문맥, 날짜 후보). '최선순위' 가 없으면 ('', [])."""
    t = str(text or "")
    i = t.find("최선순위")
    if i < 0:
        return "", []
    window = t[max(0, i - 200): i + 350]
    return window, list(dict.fromkeys(m.group(0).strip() for m in _DATE.finditer(window)))


# ── ② 대항력 판정 ────────────────────────────────────────────────────────
# 대항력은 전입(상가는 사업자등록)이 최선순위 설정일보다 **빠를** 때 생긴다. 같은 날이면
# 없다 — 대항력은 전입 다음 날 0시에 생기고 근저당은 그날 효력이 나서다.
# 이 판정은 '가능성' 이다. 배당요구·확정일자로 보증금을 다 받으면 낙찰자가 안 떠안는다.

_NOT_TENANT = ("소유자", "채무자")
# 현황조사서 전입일 칸에 날짜 대신 글자가 오는 일이 있다(10-07: 진행 '모름' 중 미전입 368·확정일자 1,121·미상 422).
# '미전입·미등재·미등록·해당없음' 은 전입을 안 했다는 뜻 — **주거 세입자만** 대항력 없음으로 본다. 점포·사무실은
# 대항력이 사업자등록으로 생기고 농지·창고는 법이 달라, 이 칸만으로 '없음' 이라 하면 위험한 쪽으로 틀린다.
_NO_MOVE_IN = re.compile(r"^(?:미전입|미등재|미등록|해당\s*없음)\.?$")
_KO_DATE = re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")


def _move_in(value: Any) -> date | None:
    """전입일 칸 — 점 날짜와 '2021년10월7일' 꼴."""
    m = _KO_DATE.search(str(value or ""))
    return parse_date(value) or (parse_date(f"{m[1]}.{m[2]}.{m[3]}") if m else None)


_NO_TENANT_SPEC = re.compile(r"조사된\s*임차\s*내역\s*없|임차\s*내역\s*없음|임차인\s*없음")
_TENANT_SPEC = re.compile(r"주택\s*임차|상가\s*임차|임차권자|현황조사\s|권리신고")


def same_property(a: str, b: str) -> bool:
    """두 주소가 같은 물건인가. 쉼표·공백·앞번호만 다른 표기를 같게 본다(웹 sameProperty 와 같다)."""
    key = lambda v: re.sub(r"[^가-힣0-9a-zA-Z]", "", re.sub(r"^\d+\.\s*", "", v or ""))  # noqa: E731
    left, right = key(a), key(b)
    return bool(left and right) and (left == right or left in right or right in left)


# 매각물건명세서의 점유자 표 — PDF 글이라 칸이 뒤섞이지만 법원 양식의 칸 순서는 정해져 있다:
# 보증금 → 차임 → **전입신고(사업자등록)일** → 확정일 → 배당요구일. 그래서 같은 줄에서 보증금(과 차임)
# 바로 뒤 첫 날짜, 보증금이 없는 줄은 '현황조사' 바로 뒤 날짜가 전입일이다(손 정답 16건 중 15).
# Jev 로 날짜 후보마다 물었더니 16건 중 3건 — 뒤섞인 표에서 칸을 못 이었다. 여긴 규칙이다.
# '입원'(요양원 환자) 줄은 임차인이 아니라 뺀다.
_MONEY = r"\d{1,3}(?:,\d{3})+\s*원?"
_FILL = r"(?:\s+(?:" + _MONEY + r"|미상|없음|0))"
_D = r"(\d{4})\s?\.\s?(\d{1,2})\s?\.\s?(\d{1,2})\.?"
_SPEC_AFTER_MONEY = re.compile(_MONEY + _FILL + r"?\s+" + _D)
_SPEC_AFTER_SURVEY = re.compile(r"현황조사(?:\s+(?:차|미상|없음|0|" + _MONEY + r")){0,4}\s+" + _D)


def spec_moveins(spec_text: str) -> list[date]:
    """매각물건명세서 점유자 표에서 전입신고(사업자등록)일들."""
    t = str(spec_text or "")
    a = t.find("록 신청일자")
    a = a if a >= 0 else t.find("점유자")
    if a < 0:
        return []
    body = t[a:].split("<비고>")[0]
    found: list[date] = []
    for line in body.split("\n"):
        for m in (*_SPEC_AFTER_MONEY.finditer(line), *_SPEC_AFTER_SURVEY.finditer(line)):
            d = parse_date(".".join(m.groups()[-3:]))
            if d and d not in found:
                found.append(d)
    return found


def opposability(occupants: list[dict[str, Any]], senior: dict[str, Any] | None,
                 spec_text: str = "", address: str = "", bulk: bool = False) -> dict[str, Any]:
    """점유인마다 대항력 있음/없음/모름, 그리고 물건 전체 요약.

    현황조사서에 임차인이 없어도 매각물건명세서에만 있는 경우가 있다(법원이 '대항력 있는
    임차인 있음' 이라 쓴 73건 중 15건). 그땐 '임차인 없음' 이 아니라 '모름' 이다 —
    없다고 잘못 말하는 쪽이 더 나쁘다."""
    base = parse_date((senior or {}).get("date"))
    # 남의 호실 임차인은 뺀다. 주소 표기가 너무 달라 하나도 안 걸리면 다 쓴다 — 웹과 같은 선택
    # (가려내려다 다 숨기는 것보다 낫다). 목록과 상세가 같은 답을 내야 한다.
    # 일괄매각은 여러 목록을 한 번에 판다 — 주소 칸엔 첫 목록만 있어도 다른 목록 임차인이 이 물건
    # 임차인이다(법원이 '대항력 있음' 이라 쓴 일괄매각 2건을 거르다 틀렸다). 그땐 거르지 않는다.
    mine = [] if bulk else [o for o in occupants or []
                            if address and same_property(str(o.get("소재지") or ""), address)]
    tenants = []
    for occ in mine or (occupants or []):
        role = str(occ.get("role") or "")
        if any(word in role for word in _NOT_TENANT):
            continue
        moved = _move_in(occ.get("전입일자"))
        if base is not None and moved is None and "주거" in str(occ.get("용도") or "") \
                and _NO_MOVE_IN.match(str(occ.get("전입일자") or "").strip()):
            verdict = "없음"
        elif base is None or moved is None:
            verdict = "모름"
        else:
            verdict = "있음" if moved < base else "없음"
        tenants.append({"name": occ.get("name", ""), "role": role, "move_in": moved.isoformat() if moved else "",
                        "opposable": verdict})
    # 명세서의 임차인도 더한다. 현황조사서 표엔 **지금 사는 사람**만 있고, 이사 나가며 임차권등기를 해 둔
    # 세입자(보증공사가 넘겨받은 전세사기 유형)는 명세서에만 있다 — 표만 보면 '없음' 으로 틀린다(09-30
    # 대조에서 표 '없음'·명세서 '있음' 720건, 열어 본 것 전부 명세서가 맞음). 명세서는 물건번호마다
    # 따로라 옆 호실이 섞이지 않는다. 표에 같은 날짜가 있으면 같은 사람으로 본다.
    seen = {t["move_in"] for t in tenants}
    for moved in spec_moveins(spec_text):
        if moved.isoformat() in seen:
            continue
        verdict = "모름" if base is None else ("있음" if moved < base else "없음")
        tenants.append({"name": "", "role": "명세서", "move_in": moved.isoformat(), "opposable": verdict})
    verdicts = {t["opposable"] for t in tenants}
    if not tenants:
        spec_has_tenant = bool(_TENANT_SPEC.search(spec_text)) and not _NO_TENANT_SPEC.search(spec_text)
        summary = "모름" if spec_has_tenant else "임차인 없음"
    elif "있음" in verdicts:
        summary = "있음"
    elif verdicts == {"없음"}:
        summary = "없음"
    else:
        summary = "모름"
    return {"senior": senior, "tenants": tenants, "summary": summary}


# ── ②-1 낙찰자가 떠안는 권리 ────────────────────────────────────────────────
# 매각물건명세서의 '등기된 부동산에 관한 권리 또는 가처분으로 매각으로 그 효력이 소멸되지 아니하는 것' 칸 —
# 법원이 **낙찰자가 떠안는 권리** 를 직접 적는다. 진행 4,005건에 내용이 있는데 2026-10-04 까지 안 썼다.
# 권리 종류마다 따로 본다: 그 종류를 말한 문장에 '인수함·인수됨·말소되지 않' 이 있으면 떠안음, 없고 '말소 동의·
# 확약서·포기' 가 있으면 해소, 둘 다 없으면 떠안음(모르면 위험 쪽). 손 정답 30건으로 세웠다.
_INHERIT_KINDS = (("가처분", "가처분"), ("가등기", "가등기"), ("전세권", "전세권"), ("지역권", "지역권"),
                  ("지상권", "지상권"), ("임차권", "임차권"), ("임차인", "임차권"))
def _loose(word: str) -> str:
    """PDF 가 줄을 바꾸며 낱말 가운데 빈칸을 넣는다('말소동 의서', '인 수됨') — 글자 사이 빈칸을 허용한다."""
    return r"\s?".join(re.escape(ch) for ch in word)


_INHERIT_YES = re.compile("|".join([_loose("인수") + r"\s?(?:함|됨|될|하)", r"매수인(?:이|에게)\s*" + _loose("인수"),
                                    _loose("말소되지") + r"\s*(?:않|아니)", _loose("소멸하지") + r"\s*않", _loose("상실")]))
_INHERIT_NO = re.compile("|".join([_loose("말소") + r"\s*" + _loose("동의"), _loose("확약서"),
                                   r"대항력\S{0,3}\s*" + _loose("포기"), r"반환\s*(?:청구)?\s*권을?\s*" + _loose("포기")]))
# 구분 기호 없이 권리를 잇달아 적는 법원도 있다 — "을구 1번 임차권등기(다만 …확약서) 별도등기(…지상권설정등기)".
# 새 등기가 시작되는 자리에서도 끊는다. 안 끊으면 임차권의 확약서가 지상권까지 '해소' 로 만든다(10-04 Jev 엇갈림 2건).
_CLAUSE = re.compile(r"(?<=[음됨함다])\.\s+|\s(?=\d+\.\s)|\s-\s|\s-(?=\S)|\s(?=별도등기|(?:토지\s*)?[갑을]구\s*(?:순위\s*)?\d)")


def inherited_section(spec_text: str) -> str:
    t = str(spec_text or "")
    a = t.find("효력이 소멸되지 아니하는 것")
    if a < 0:
        return ""
    b = t.find("매각에 따라 설정된", a)
    sec = re.sub(r"\s+", " ", t[a + 16: b if b > a else a + 600]).strip(" /")
    return "" if re.fullmatch(r"(해당\s*사항\s*없음|없음|-)?", sec) else sec


def inherited_rights(spec_text: str) -> dict[str, str]:
    """{권리 종류: '떠안음' | '해소'}. 칸이 비었거나 '해당사항없음' 이면 {}."""
    sec = inherited_section(spec_text)
    if not sec:
        return {}
    clauses = [c for c in _CLAUSE.split(sec) if c.strip()]
    out: dict[str, str] = {}
    for needle, kind in _INHERIT_KINDS:
        mine = [c for c in clauses if needle in c]
        if not mine:
            continue
        if any(_INHERIT_YES.search(c) for c in mine):
            out[kind] = "떠안음"
        elif any(_INHERIT_NO.search(c) for c in mine):
            out.setdefault(kind, "해소")
        else:
            out[kind] = "떠안음"
    return out


# 명세서의 다른 두 칸. 목록 비고에는 없고 여기에만 적힌 것이 있다(10-04: 진행 물건 중 법정지상권 473·
# 분묘 97, 비고란의 위반건축물 145·맹지 118 등이 딱지에 없었다).
# '매각에 따라 설정된 것으로 보는 지상권의 개요' 는 법정지상권 칸이다 — 분묘 얘기면 분묘기지권, 그 밖의
# 내용은 전부 법정지상권. "성립 여부 불분명" 도 딱지를 단다(법원이 주의를 준 것이고, 목록 비고에 같은 말이
# 있을 때도 그렇게 해 왔다). 성립하지 않는다고 못 박은 것만 뺀다.
_SPEC_NEG = re.compile(r"(?:성립하지|성립되지)\s*(?:않|아니)|성립\s*(?:할\s*)?여지\s*(?:가\s*)?없|해당\s*(?:사항\s*)?없|^\W*없음")
# 비고란은 법원 메모다. 뜻이 분명한 낱말만 딱지로 — 대항력·유치권은 위의 규칙이 따로 본다.
_SPEC_REMARK = ((r"위반\s*건축물", "위반건축물"), ("맹지", "맹지"), (r"대지권\s*미등기", "대지권미등기"),
                (r"별도\s*등기", "별도등기"), (r"지분\s*매각", "지분매각"), (r"농지\s*취득", "농지취득자격증명"),
                ("제시외", "제시외건물"), ("분묘", "분묘기지권"), (r"법정\s*지상권", "법정지상권"))
# "농지취득자격증명 없이 취득 가능"·"발급받지 않고" · "대지권 미등기이나, 이후 대지권등기가 완료"
_TENANT_STATED = re.compile(r"대항할\s*수\s*있는\s*(?:주택\s*|상가\s*)?(?:임차인|임차권|전세권)(?!\S{0,3}\s*없)")
_REMARK_NEG = r"[^.。]{0,16}?(?:아님|없음|아니|해당\s*없|불요|불필요|없이|않고|완료)"


def spec_sections(spec_text: str) -> tuple[str, str]:
    """(지상권 개요 칸, 비고란) — 서식 안내문('1: 매각목적물에서 제외되는…')은 뺀다."""
    t = str(spec_text or "")
    a, b = t.find("지상권의 개요"), t.find("비고란")
    if b < 0:
        return "", ""
    c = t.find("매각목적물에서 제외되는 미등기건물", b)
    clean = lambda x: re.sub(r"\s+", " ", x).strip(" -:·※1")
    sup = clean(t[a + 7: b]) if 0 <= a < b else ""
    return ("" if re.fullmatch(r"[\s\-:·.]*(?:해당\s*사항\s*)?(?:없음|무)?[\s.]*", sup) else sup), clean(t[b + 3: c if c > b else b + 600])


def spec_flags(spec_text: str) -> list[str]:
    """명세서 지상권 개요 칸 + 비고란에서 뽑은 딱지."""
    sup, remark = spec_sections(spec_text)
    out: list[str] = []
    for clause in re.split(r"(?<=[음함다])[.,]\s*|\s(?=목록\s*\d)", sup):
        if clause.strip(" .") and not _SPEC_NEG.search(clause):
            # 낱말 없는 토막("(성립여부는 불분명)")은 앞 문장의 되풀이다 — 딱지를 새로 만들지 않는다
            label = "분묘기지권" if "분묘" in clause else "법정지상권" if re.search(
                r"건물|지상권|구축물|공작물|컨테이너|창고|주택", clause) else None
            if label and label not in out:
                out.append(label)
    # 법원이 '대항력' 대신 "매수인에게 대항할 수 있는 임차인 있음" 이라고 쓰는 일이 많다(10-05: 진행 34건이
    # 이 문장만 있어 높음이 아니었다). 포기 확약이 같이 적혔으면 enrichment 의 대항력포기 몫이다.
    if _TENANT_STATED.search(remark) and not _WAIVER_DOC_RE.search(remark):
        out.append("대항력있는임차인")
    for pattern, label in _SPEC_REMARK:
        hits = [m for m in re.finditer(pattern, remark) if not re.match(_REMARK_NEG, remark[m.end():])]
        if hits and label not in out:
            out.append(label)
    return out


# ── ③ 비고 문구 규칙 ─────────────────────────────────────────────────────

# 포기 문구를 걷어낸 뒤에도 '대항력' 이 남는데, 그게 **다른 임차인을 암시할 때만** 위험이
# 남는다. "대항력 있는 임차인 있음 … 단, 보증공사가 대항력 포기" 는 같은 임차인의 되풀이다.
# 정답지 90건: 이 규칙 90/90, '남으면 무조건 높음' 85/90(헛경고 5).
WAIVER_RE = re.compile(r"대항력\s*(?:은|을|의)?\s*포기")
# 포기 확약은 '대항력' 이라는 말 없이도 쓰인다 — "잔존 보증금반환채권을 포기하고 주택임차권등기 말소에 동의".
# 명세서 비고에만 적힌 일도 많다(09-30: 계산상 '대항력 있음' 3,363건 중 1,873건이 이랬다). '말소 동의' 만으로는
# 안 본다 — 지상권 말소동의서 같은 다른 권리와 섞인다. 임차권·보증금반환이 붙어야 한다.
_WAIVER_DOC_RE = re.compile(
    r"대항력\s*(?:은|을|의)?\s*포기|보증금\s*반환\s*(?:청구)?\s*(?:채)?권을?\s*포기|임차권\s*등기[^.。]{0,12}말소[^.。]{0,12}동의")


def tenant_waived(note: str, spec_text: str) -> bool:
    """보증기관·임차인이 대항력(잔존 보증금 청구)을 포기했다는 확약 — 비고나 명세서 비고에."""
    remark = spec_text.split("<비고>", 1)[1] if "<비고>" in spec_text else ""
    return bool(_WAIVER_DOC_RE.search(note) or _WAIVER_DOC_RE.search(remark))
_OTHER_TENANT_RE = re.compile(
    r"대항력[^.。]{0,20}(?:여지|있을\s*수|미상|주의)|임대차\s*관계\s*(?:미상|불분명)|미상의\s*(?:임차인|전입자)")


def waiver_leaves_other_tenant(text: str) -> bool:
    """포기와 별개로 대항력 있는(있을 수 있는) 다른 임차인이 적혀 있나."""
    return bool(_OTHER_TENANT_RE.search(WAIVER_RE.sub("", text)))


# 유치권이 **해소**됐나: 신고 취하·철회·포기서, 부존재 확인 승소·확정, 부존재확인서.
# 반대로 **남는** 것: "부존재확인 소송 … 원고 패소·원고 청구기각"·"일부 승소 … 성립 인정"·"유치권 존재확인
# 판결" — 법원이 유치권을 인정한 것이다. '부존재' 라는 글자만 보면 정반대로 틀린다.
# 신고인이 여럿이면 일부만 해소되기도 한다 — **마지막 해소 뒤에 신고·행사가 또 나오면 남음.**
# 얽힌 문장은 남음 쪽으로 틀린다(헛경고가 놓침보다 싸다). 그런 엇갈림은 Jev 가 찾아 정답지로.
# 끝난 것만 해소다 — '1심 승소'·'승소했으나 미확정'·'승소판결 뒤 항소장 접수' 는 아직이다(2026-09-28,
# Jev 엇갈림 19건 중 규칙이 틀린 9건이 전부 이 무리였고 전부 '남음' 을 '해소' 로 틀린 위험한 쪽이었다).
_FINAL = r"(?<!미)확정(?!\s*되지)|상고\s*기각"
_LIEN_RESOLVED = re.compile(
    r"유치권[^.。]{0,30}(?:취하|철회)|(?:취하|철회|포기)\s*\)?\s*서[^.。]{0,10}(?:제출|접수)|철회\s*신고서"
    r"|부존재\s*확인서|(?:부존재|존재하지\s*(?:아니|않))[^。]{0,160}?(?:" + _FINAL + ")")
_LIEN_LOST = re.compile(
    r"부존재[^.。]{0,80}(?:패소|청구\s*기각)|원고\s*패소|일부\s*승소|성립[^.。]{0,6}인정|(?<!부)존재\s*확인\s*(?:판결|소)"
    r"|유치권이\s*존재한다|(?<!부)존재한다는|(?:타|다른)\s*채권자의?\s*유치권")
# 해소 뒤에 이런 말이 또 나오면 남음 — 다른 신고인, 아직 진행 중인 소송, 항소, 미확정.
_LIEN_OPEN = re.compile(
    r"유치권\s*(?:권리)?신고|유치권[^.。]{0,10}(?:행사|주장)|성립\s*여부[^.。]{0,6}불분명"
    r"|진행\s*중|항소장|항소\s*중|미확정|확정되지")


def lien_status(text: str) -> str | None:
    """'해소' · '남음' · None(유치권 언급 없음)."""
    if "유치권" not in text:
        return None
    if _LIEN_LOST.search(text):
        return "남음"
    resolved = [m.end() for m in _LIEN_RESOLVED.finditer(text)]   # 해소 문장이 끝난 자리부터 본다
    if not resolved:
        return "남음"
    opened = [m.start() for m in _LIEN_OPEN.finditer(text)]
    return "남음" if opened and max(opened) >= max(resolved) else "해소"


# ── ④ 현황조사 — 조사관이 점유자를 직접 만났나 ─────────────────────────────
# 표 칸의 '임차인(별지)점유' 는 전입세대 서류만 보고 적는 일이 흔하다 — 본문은 폐문부재·
# 공실인데 표만 보면 '세입자가 산다' 로 읽힌다(표본 22건 중 3건). 정답지 22건: 21/22.

_MET = re.compile(r"면담|만나(?!지\s*못|지\s*않)|만났|문의한\s*바|통화|유선|안내로|(?<!없)다고\s*함")
_NOT_MET = re.compile(r"폐문|만나지\s*못|만날\s*수\s*없|알\s*수\s*없|탐문하였으나|확인되지\s*않|조사\s*불가|연락이\s*없")
_SENTENCE = re.compile(r"[.。]\s*(?=[가-힣])|\s-\s|[①-⑳]")
_HEARSAY = re.compile(r"관리사무소|관리인|관리실|인근|이웃|주민|탐문")
_OCC_LABEL = re.compile(r"점유관계\s+((?:채무자|소유자|임차인|기타|제3자|점유자)\S{0,8}?점유|미상)")


def survey_memo(text: Any) -> str:
    """현황조사서 본문에서 점유관계·현황 부분만 떼어 표 흔적을 걷어 낸다."""
    t = str(text or "")
    i = t.find("부동산의 점유관계")
    if i < 0:
        return ""
    end = t.find("임대차관계 조사서", i)
    body = t[i: end if end > 0 else len(t)]
    body = re.sub(r"[^\n]*displayed in the table", " ", body)
    return re.sub(r"\s+", " ", body).strip()


def occupancy_check(memo: str) -> dict[str, Any]:
    """confirmed: True(직접 만남·통화) · False(못 만남) · None(말이 없음)."""
    if not memo:
        return {"confirmed": None, "label": "", "label_unverified": False}
    labels = sorted(set(_OCC_LABEL.findall(memo)))
    # 관리사무소 직원·이웃에게 물은 것은 점유자를 만난 게 아니다 — 그 문장은 빼고 본다.
    own = " ".join(part for part in _SENTENCE.split(memo) if not _HEARSAY.search(part))
    confirmed = True if _MET.search(own) else (False if _NOT_MET.search(memo) else None)
    says_occupied = any(label != "미상" for label in labels)
    return {"confirmed": confirmed, "label": ", ".join(labels),
            # 표엔 누가 점유한다고 적혔는데 조사관은 아무도 못 만났다 — 서류로만 적은 것
            "label_unverified": says_occupied and confirmed is False}


# ── ⑤ 실명 — 진행 중엔 보이고, 끝나면 가린다(옥션원 방식) ─────────────────
# 법원 사건 화면의 당사자 표는 법원이 '이OO' 로 가려서 준다. 실명은 현황조사서·
# 매각물건명세서 **본문**에 있다(G10: 1,200건 훑어 가려진 것 0). 문서가 "여기가 이름
# 칸" 이라고 밝히는 자리에서만 거둔다 — 두세 글자 한글을 다 이름으로 보면 본문이 깨진다.
# 문장 속에 섞인 이름("허범이 유치권신고")은 규칙이 못 잡아 Jev 가 거둔다(item["jev"]).

_ROLE_WORDS = ("채무자겸소유자", "채무자 겸 소유자", "소유자", "채무자", "채권자", "임차인",
               "세대주", "점유자", "점유인", "유치권자", "신고인", "임차권자")
_ROLE_BEFORE = re.compile(
    r"(채무자\s*겸\s*소유자|소유자|채무자|채권자|임차인|세대주|점유자|점유인|유치권자|신고인|임차권자)"
    r"\s*[:：]?\s*([가-힣]{2,4})(?=[\s,.()\[(]|$|은|는|이|가|의|로부터|으로부터)")
_ROLE_AFTER = re.compile(r"([가-힣]{2,4})\s*\((채무자\s*겸\s*소유자|채무자|소유자|임차인|세대주)\)")
_CORP = ("주식회사", "(주)", "㈜", "유한회사", "법인", "은행", "조합", "공사", "보험", "농협", "수협",
         "신협", "금고", "캐피탈", "대부", "자산관리", "관리단", "재단", "센터", "공단")
_NOT_NAME = frozenset(
    "없음 있음 미상 불명 성명불상 불상 전부 일부 등으로 등이 겸 및 또는 부부 가족 세대 본인 배우자 "
    "권리신고 배당요구 현황조사 조사서 전입 확정 거주 점유 임차 임대 대표 대표자 직원 관계자 측 "
    "주택도시 한국토지 서울보증 신청채권 승계인 양수인 대리인 소유 명의 부분 전입세대 외국인 "
    "안내문 면담 문의 진술 확인 여부 해당 기재 사항 동인 동인은 위 해당없음 모두 각 다수 "
    # 성씨 글자로 시작하는 역할어·법원 상용어 — Jev 가 '임차인' 을 이름이라 답한 적이 있다(2026-09-23)
    "임차인 임차권 임차권자 임대인 임대차 유치권 유치권자 채무자 채권자 소유자 세입자 세대원 점유인 점유자 "
    "신고인 신청인 관계인 이해관계인 공유자 공유 전입자 전세 전세권 전세권자 차임 차량 최선순위 최초 최종 "
    "선순위 설정 이상 이하 이후 이전 이건 이곳 이용 이동 이사 조사 조사관 현황 현장 방문 안내 기타 성명 "
    "민원 정확 정도 장소 장비 강제 경매 정리 한편 우선 우측 좌측 배당 배우자 소재 소재지 구조 지목 도로 "
    "목록 명의 명도 계약 표시 표지 반환 변제 변경 연락 연락처 제시 제출 제외 채권 위반 유선 유무 인근 인접 "
    "지하 지상 오피스텔 원룸 상가 주택 주식 한국 공사 문서 서류 송달 방문시 전화 사장 직원 대표 대표이사 "
    "전용 전용허가 허가 신축 준공 사용승인 "
    # 실데이터에서 이름으로 잘못 거둔 것(2026-09-24): 조사·어미가 붙은 낱말, 용도어
    "소유 소유로 소유의 소유임 이고 이며 이자 이외 이외에 이상의 이하의 이내 주거 점포 사무실 공장 창고 "
    "영업 영업장 공실 한명 전원 정도 각자 각각 공동 단독 유일 동일 최고 최저 이미 이번 이사 임의로 "
    "조사불가 조사불능 현재 현재까지 주택도 주택도시 소유라고 명의의 지분을 진술에".split())
# 이름 뒤에 붙은 조사를 뗀다. 네 글자면 끝 한 글자 조사, 두 글자 조사는 남는 게 두 글자 이상일 때.
_NAME_TAIL_1 = tuple("은는이가의과와외에도")
_NAME_TAIL_2 = ("으로", "에게", "로부터", "으로부터", "에서", "께서")
_SURNAMES = set("김이박최정강조윤장임한오서신권황안송류유전홍고문양손배백허남심노하곽성차주우구민진지엄채원천방"
                "공현함변염여추도소석선설마길연위표명기반왕금옥육인맹제모탁국어은편용")


def _looks_like_name(word: str) -> bool:
    return (2 <= len(word) <= 4 and word[0] in _SURNAMES and word not in _NOT_NAME
            and not any(c in word for c in _CORP))


def _clean_name(name: str) -> str:
    """조사를 떼고 이름꼴이면 돌려준다, 아니면 ''. 떼기 전 모양도 제외어로 본다('조사불가' → '조사불')."""
    if name in _NOT_NAME:
        return ""
    for tail in _NAME_TAIL_2:
        if name.endswith(tail) and len(name) - len(tail) >= 2:
            name = name[: -len(tail)]
            break
    else:
        if len(name) == 4 and name.endswith(_NAME_TAIL_1):
            name = name[:-1]
    return name if _looks_like_name(name) else ""


def harvest_names(documents: list[dict[str, Any]], occupants: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """이름을 두 갈래로 거둔다.

    - `table`: 현황조사서 표의 '점유인' 칸 — 법원이 이름 칸이라고 정한 자리라 그대로 쓴다.
    - `prose`: 문장 속 "소유자 ○○○"·"○○○(채무자)" — **후보일 뿐**이다. 실데이터에서 '소유로'·'이며'·
      '현재'·'조사불(가)' 를 이름으로 집었다(2026-09-24). Jev 가 사람 이름이라 확인한 것만 쓴다.
    - `windows`: prose 후보 앞뒤 글 — Jev 에게 문맥으로 준다."""
    table: dict[str, list[str]] = {}
    prose: dict[str, list[str]] = {}
    windows: list[str] = []

    def add(bucket: dict[str, list[str]], role: str, raw: str) -> str:
        role = "채무자겸소유자" if "겸" in role else role.replace(" ", "")
        name = _clean_name(raw)
        if name and name not in bucket.setdefault(role, []):
            bucket[role].append(name)
        return name

    for occ in occupants or []:
        add(table, str(occ.get("role") or "점유인"), str(occ.get("name") or ""))
    for doc in documents or []:
        if str(doc.get("document_type") or "") not in ("현황조사서", "매각물건명세서"):
            continue
        text = str((doc.get("metadata") or {}).get("text") or "")
        for m in _ROLE_BEFORE.finditer(text):
            if add(prose, m.group(1), m.group(2)):
                windows.append(text[max(0, m.start() - 40): m.end() + 40])
        for m in _ROLE_AFTER.finditer(text):
            if add(prose, m.group(2), m.group(1)):
                windows.append(text[max(0, m.start() - 40): m.end() + 40])
    return {"table": {r: n for r, n in table.items() if n}, "prose": {r: n for r, n in prose.items() if n},
            "windows": windows[:30]}


def mask_person_name(name: str) -> str:
    """가운데를 가린다(김○○). 법원이 목록에서 쓰는 방식과 같다."""
    return name[0] + "○" * (len(name) - 1) if len(name) >= 2 else name


# 두 글자 이름은 다른 낱말 속에 들어 있을 수 있다('정원' → '정원수'). 뒤에 한글이 아닌 것이나
# 조사가 올 때만 가린다. 세 글자 이상은 우연히 겹칠 일이 드물어 그대로 가린다.
_SHORT_TAIL = r"(?=$|[^가-힣]|(?:이|가|은|는|의|을|를|과|와|에게|에게서|로부터|으로부터|씨|님)(?:$|[^가-힣]))"


def mask_text(text: str, names: list[str]) -> str:
    """거둔 이름만 가린다. 긴 이름부터 — 짧은 이름이 긴 이름의 앞을 먼저 먹으면 안 된다."""
    for name in sorted(set(names), key=len, reverse=True):
        if len(name) >= 3:
            text = text.replace(name, mask_person_name(name))
        else:
            text = re.sub(re.escape(name) + _SHORT_TAIL, mask_person_name(name), text)
    return text


# 문장 속 이름 후보. **넉넉히** 뽑는다(놓친 이름은 그대로 노출된다) — 이름이냐 아니냐는
# Jev 가 가른다. 한글 덩어리에서 조사를 뗀 2~4자, '(' 앞, 역할어 뒤.
_PARTICLES = ("으로부터", "로부터", "에게서", "에게", "이", "가", "의", "은", "는", "씨")


def name_candidates(text: str, limit: int = 60) -> list[str]:
    out: list[str] = []
    for m in re.finditer(r"[가-힣]+", text):
        w = m.group(0)
        # 가장 긴 조사 하나만 뗀다 — '한익성으로부터' 에서 '로부터' 까지 떼면 '한익성으' 가 남는다.
        forms = next(([w[: -len(p)]] for p in _PARTICLES if w.endswith(p) and 2 <= len(w) - len(p) <= 4), [])
        if not forms and 2 <= len(w) <= 4 and (text[m.end(): m.end() + 1] == "(" or
                                 re.search(r"(?:임차인|유치권자|채무자|소유자|세대주|점유자|신고인|세입자)\s$",
                                           text[max(0, m.start() - 8): m.start()])):
            forms.append(w)
        for f in forms:
            if f[0] in _SURNAMES and f not in _NOT_NAME and not any(c in f for c in _CORP) and f not in out:
                out.append(f)
    return out[:limit]


def mask_payload(node: Any, names: list[str]) -> Any:
    """payload 안의 모든 글에서 이름을 가린다. 끝난 물건에만 쓴다."""
    if not names:
        return node
    if isinstance(node, str):
        return mask_text(node, names)
    if isinstance(node, list):
        return [mask_payload(v, names) for v in node]
    if isinstance(node, dict):
        return {k: mask_payload(v, names) for k, v in node.items()}
    return node


RIGHTS_VERSION = 11   # 11: 주거 '미전입' 은 대항력 없음 · 한글 날짜 · 10: '대항할 수 있는 임차인' 문장 · 9: 명세서 지상권 개요 칸·비고란 딱지 · 8: 부존재확인 청구기각은 남음 · 새 등기 자리에서 끊는다 · 7: 낙찰자가 떠안는 권리 칸 · 6: 명세서 임차인을 합친다 · 5: 확정 안 된 부존재 승소는 남음 · 임대차관계 불분명 · 4: 문장 속 이름은 Jev 확인분만 · 2: 남의 호실 임차인을 뺀다 · 3: 문장에서 거둔 이름의 조사·낱말을 걸렀다


def compute_rights(*, spec_text: str, survey_text: str, note: str,
                   occupants: list[dict[str, Any]], jev: dict[str, Any] | None = None,
                   address: str = "") -> dict[str, Any]:
    """규칙 판정 전부 + (있으면) Jev 가 채운 꼬리. DB 에 `rights_json` 으로 그대로 들어간다.

    이름은 **실명 그대로** 담는다(DB 는 우리 것). 가리는 것은 payload 를 만들 때 한다 —
    진행 중이면 보이고 끝나면 가린다."""
    jev = jev or {}
    senior = senior_right(spec_text)
    if senior is None and (jev.get("senior") or {}).get("date"):
        senior = {**jev["senior"], "source": "jev"}
    memo = survey_memo(survey_text)
    harvested = harvest_names(
        [{"document_type": "현황조사서", "metadata": {"text": survey_text}},
         {"document_type": "매각물건명세서", "metadata": {"text": spec_text}}], occupants)
    confirmed = set(jev.get("names") or [])
    names = {role: list(group) for role, group in harvested["table"].items()}
    for role, group in harvested["prose"].items():
        for name in group:   # 문장에서 거둔 것은 Jev 가 사람 이름이라 한 것만
            if name in confirmed and name not in names.setdefault(role, []):
                names[role].append(name)
    return {
        "v": RIGHTS_VERSION,
        "senior": senior,
        "opposability": opposability(occupants, senior, spec_text, address, bulk="일괄매각" in note),
        "lien": lien_status(note),
        "waiver_other_tenant": waiver_leaves_other_tenant(note) if WAIVER_RE.search(note) else None,
        "waived": tenant_waived(note, spec_text),
        "inherited": inherited_rights(spec_text),
        "spec_flags": spec_flags(spec_text),
        "survey": {**occupancy_check(memo), "memo": memo},
        "names": {role: group for role, group in names.items() if group},
        "name_candidates": sorted({n for group in harvested["prose"].values() for n in group}),
        "name_windows": harvested["windows"],
        "jev_names": sorted(confirmed),
    }


def all_names(rights: dict[str, Any], occupants: list[dict[str, Any]] | None = None) -> list[str]:
    """가릴 이름 전부 — 규칙이 거둔 것 + Jev 가 문장 속에서 찾은 것 + 점유인."""
    names = [n for group in (rights.get("names") or {}).values() for n in group]
    names += list(rights.get("jev_names") or [])
    names += [str(o.get("name") or "") for o in occupants or [] if _looks_like_name(str(o.get("name") or ""))]
    return sorted({n for n in names if n}, key=len, reverse=True)
