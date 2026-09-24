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


_NO_TENANT_SPEC = re.compile(r"조사된\s*임차\s*내역\s*없|임차\s*내역\s*없음|임차인\s*없음")
_TENANT_SPEC = re.compile(r"주택\s*임차|상가\s*임차|임차권자|현황조사\s|권리신고")


def opposability(occupants: list[dict[str, Any]], senior: dict[str, Any] | None,
                 spec_text: str = "") -> dict[str, Any]:
    """점유인마다 대항력 있음/없음/모름, 그리고 물건 전체 요약.

    현황조사서에 임차인이 없어도 매각물건명세서에만 있는 경우가 있다(법원이 '대항력 있는
    임차인 있음' 이라 쓴 73건 중 15건). 그땐 '임차인 없음' 이 아니라 '모름' 이다 —
    없다고 잘못 말하는 쪽이 더 나쁘다."""
    base = parse_date((senior or {}).get("date"))
    tenants = []
    for occ in occupants or []:
        role = str(occ.get("role") or "")
        if any(word in role for word in _NOT_TENANT):
            continue
        moved = parse_date(occ.get("전입일자"))
        if base is None or moved is None:
            verdict = "모름"
        else:
            verdict = "있음" if moved < base else "없음"
        tenants.append({"name": occ.get("name", ""), "role": role, "move_in": moved.isoformat() if moved else "",
                        "opposable": verdict})
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


# ── ③ 비고 문구 규칙 ─────────────────────────────────────────────────────

# 포기 문구를 걷어낸 뒤에도 '대항력' 이 남는데, 그게 **다른 임차인을 암시할 때만** 위험이
# 남는다. "대항력 있는 임차인 있음 … 단, 보증공사가 대항력 포기" 는 같은 임차인의 되풀이다.
# 정답지 90건: 이 규칙 90/90, '남으면 무조건 높음' 85/90(헛경고 5).
WAIVER_RE = re.compile(r"대항력\s*(?:은|을|의)?\s*포기")
_OTHER_TENANT_RE = re.compile(
    r"대항력[^.。]{0,20}(?:여지|있을\s*수|미상|주의)|임대차\s*관계\s*미상|미상의\s*(?:임차인|전입자)")


def waiver_leaves_other_tenant(text: str) -> bool:
    """포기와 별개로 대항력 있는(있을 수 있는) 다른 임차인이 적혀 있나."""
    return bool(_OTHER_TENANT_RE.search(WAIVER_RE.sub("", text)))


# 유치권이 **해소**됐나: 신고 취하·철회·포기서, 부존재 확인 승소·확정, 부존재확인서.
# 반대로 **남는** 것: "부존재확인 소송 … 원고 패소"·"일부 승소 … 성립 인정"·"유치권 존재확인
# 판결" — 법원이 유치권을 인정한 것이다. '부존재' 라는 글자만 보면 정반대로 틀린다.
# 신고인이 여럿이면 일부만 해소되기도 한다 — **마지막 해소 뒤에 신고·행사가 또 나오면 남음.**
# 얽힌 문장은 남음 쪽으로 틀린다(헛경고가 놓침보다 싸다). 그런 엇갈림은 Jev 가 찾아 정답지로.
_LIEN_RESOLVED = re.compile(
    r"유치권[^.。]{0,30}(?:취하|철회)|(?:취하|철회|포기)\s*\)?\s*서[^.。]{0,10}(?:제출|접수)|철회\s*신고서"
    r"|부존재[^.。]{0,80}(?:승소|확정)|부존재\s*확인서|존재하지\s*(?:아니|않)")
_LIEN_LOST = re.compile(
    r"부존재[^.。]{0,80}패소|원고\s*패소|일부\s*승소|성립[^.。]{0,6}인정|(?<!부)존재\s*확인\s*(?:판결|소)")
_LIEN_OPEN = re.compile(r"유치권\s*(?:권리)?신고|유치권[^.。]{0,10}(?:행사|주장)|성립\s*여부[^.。]{0,6}불분명")


def lien_status(text: str) -> str | None:
    """'해소' · '남음' · None(유치권 언급 없음)."""
    if "유치권" not in text:
        return None
    if _LIEN_LOST.search(text):
        return "남음"
    resolved = [m.start() for m in _LIEN_RESOLVED.finditer(text)]
    if not resolved:
        return "남음"
    opened = [m.start() for m in _LIEN_OPEN.finditer(text)]
    return "남음" if opened and max(opened) > max(resolved) else "해소"


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
    "전용 전용허가 허가 신축 준공 사용승인".split())
_SURNAMES = set("김이박최정강조윤장임한오서신권황안송류유전홍고문양손배백허남심노하곽성차주우구민진지엄채원천방"
                "공현함변염여추도소석선설마길연위표명기반왕금옥육인맹제모탁국어은편용")


def _looks_like_name(word: str) -> bool:
    return (2 <= len(word) <= 4 and word[0] in _SURNAMES and word not in _NOT_NAME
            and not any(c in word for c in _CORP))


def harvest_names(documents: list[dict[str, Any]], occupants: list[dict[str, Any]] | None = None) -> dict[str, list[str]]:
    """역할별 실명. {'소유자': [...], '채무자': [...], '임차인': [...], ...}"""
    found: dict[str, list[str]] = {}

    def add(role: str, name: str) -> None:
        role = "채무자겸소유자" if "겸" in role else role.replace(" ", "")
        if _looks_like_name(name) and name not in found.setdefault(role, []):
            found[role].append(name)

    for occ in occupants or []:
        add(str(occ.get("role") or "점유인"), str(occ.get("name") or ""))
    for doc in documents or []:
        if str(doc.get("document_type") or "") not in ("현황조사서", "매각물건명세서"):
            continue
        text = str((doc.get("metadata") or {}).get("text") or "")
        for role, name in _ROLE_BEFORE.findall(text):
            add(role, name)
        for name, role in _ROLE_AFTER.findall(text):
            add(role, name)
    return {role: names for role, names in found.items() if names}


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


RIGHTS_VERSION = 1


def compute_rights(*, spec_text: str, survey_text: str, note: str,
                   occupants: list[dict[str, Any]], jev: dict[str, Any] | None = None) -> dict[str, Any]:
    """규칙 판정 전부 + (있으면) Jev 가 채운 꼬리. DB 에 `rights_json` 으로 그대로 들어간다.

    이름은 **실명 그대로** 담는다(DB 는 우리 것). 가리는 것은 payload 를 만들 때 한다 —
    진행 중이면 보이고 끝나면 가린다."""
    jev = jev or {}
    senior = senior_right(spec_text)
    if senior is None and (jev.get("senior") or {}).get("date"):
        senior = {**jev["senior"], "source": "jev"}
    memo = survey_memo(survey_text)
    rule_names = harvest_names(
        [{"document_type": "현황조사서", "metadata": {"text": survey_text}},
         {"document_type": "매각물건명세서", "metadata": {"text": spec_text}}], occupants)
    return {
        "v": RIGHTS_VERSION,
        "senior": senior,
        "opposability": opposability(occupants, senior, spec_text),
        "lien": lien_status(note),
        "waiver_other_tenant": waiver_leaves_other_tenant(note) if WAIVER_RE.search(note) else None,
        "survey": {**occupancy_check(memo), "memo": memo},
        "names": rule_names,
        "jev_names": list(jev.get("names") or []),
    }


def all_names(rights: dict[str, Any], occupants: list[dict[str, Any]] | None = None) -> list[str]:
    """가릴 이름 전부 — 규칙이 거둔 것 + Jev 가 문장 속에서 찾은 것 + 점유인."""
    names = [n for group in (rights.get("names") or {}).values() for n in group]
    names += list(rights.get("jev_names") or [])
    names += [str(o.get("name") or "") for o in occupants or [] if _looks_like_name(str(o.get("name") or ""))]
    return sorted({n for n in names if n}, key=len, reverse=True)
