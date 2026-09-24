"""Jev(TypeSafe AI) — 규칙이 못 하는 꼬리만 맡긴다.

Jev 는 글을 쓰지 않고 **고르기만** 한다(참/거짓 확률, 보기 중 하나). 그래서 값이 필요하면
정규식이 후보를 넉넉히 뽑고 Jev 가 고른다 — 없는 값을 지어낼 수 없다.

**질문 문구는 아래 `QUESTIONS` 한 곳에만 둔다.** 운영(`rights` 단계)과 평가
(`scripts/jev_eval.py`)가 같은 것을 쓴다. 문구를 고치면 버전을 올린다 — 버전이 바뀌면
저장된 답을 버리고 다시 묻는다. 고칠 때는 정답지로 먼저 재서 떨어지지 않을 때만 올린다.

2026-09-23 실측(DEVLOG): 한 질문에 몰아 묻거나 "원하나?" 처럼 의도를 물으면 참패했다
(14/21, 0/14). **원자 질문 · 글자 그대로 · 값은 후보 중 고르기** 로 바꾸니 회복했다.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from typing import Any

from .geocoder import env_value

URL = "https://api.typesafe.ai/v1/systemone"   # docs.typesafe.ai/api.md 에 적힌 주소만 쓴다
MODEL = "jev-latest"

QUESTIONS: dict[str, dict[str, Any]] = {
    # 매각물건명세서 '최선순위 설정' 날짜 — 정규식이 못 뽑은 것(칸이 찢어진 PDF)만.
    "senior": {"version": "senior-pick/1", "type": "choice",
               "instructions": "매각물건명세서의 '최선순위 설정' 칸에 적힌 날짜, 즉 최선순위 근저당·압류·가압류·"
                               "전세권·경매개시결정이 설정된 날짜는 어느 것인가? 작성일자와 배당요구종기 날짜는 아니다."},
    # 문장 속 실명 — 끝난 물건에서 가릴 이름. 후보마다 참/거짓.
    # names/2: '임차인' 을 이름이라 답했다(2026-09-23) → 역할·직함은 거짓이라고 못 박았다.
    "name": {"version": "names/2", "type": "noul",
             "instructions": "이 글에서 '{c}'는 특정한 개인(사람)의 실명인가? 임차인·채무자·유치권자 같은 역할이나 "
                             "직함, 회사·기관·지명, 일반 낱말이나 낱말의 일부면 거짓이다.",
             "criteria": {"true": "사람 이름", "false": "사람 이름이 아님"}},
    # 규칙이 '포기로 안전' 이라 했을 때 두 번째 의견 — 엇갈리면 정답지 후보로 모은다.
    "waiver": {"version": "waiver-check/1", "type": "noul",
               "instructions": "임차인이나 그 승계인(주택도시보증공사, 서울보증보험 등)이 대항력을 포기하거나, "
                               "임차권등기 말소에 동의한다는 확약서·동의서·포기조건이 적혀 있는가?",
               "criteria": {"true": "포기·말소동의·포기조건 매각이 적혀 있음", "false": "그런 내용이 없음"}},
    "other_tenant": {"version": "waiver-check/1", "type": "noul",
                     "instructions": "대항력을 포기했거나 말소에 동의한 그 임차인 말고, 대항력이 있거나 있을 수 있는 "
                                     "임차인이 따로 더 적혀 있는가? 포기한 임차인 한 명에 대한 설명만 있으면 거짓이다.",
                     "criteria": {"true": "포기와 별개로 다른 임차인의 대항력 가능성이 적혀 있음",
                                  "false": "포기한 임차인 이야기뿐이거나, 포기가 아예 없음"}},
    # 규칙이 '유치권 해소' 라 했을 때 두 번째 의견.
    "lien_resolved": {"version": "lien-check/1", "type": "noul",
                      "instructions": "유치권 신고가 취하·철회·포기되었거나, 유치권이 없다는 판결이 확정되었거나, "
                                      "유치권이 없다는 확인서가 제출되어, 유치권 문제가 해소되었다고 적혀 있는가?",
                      "criteria": {"true": "유치권이 해소되었다고 적혀 있음", "false": "해소되었다는 내용이 없음"}},
    "lien_remaining": {"version": "lien-check/1", "type": "noul",
                       "instructions": "아직 해소되지 않은 유치권 신고·주장·행사가 하나라도 남아 있는가?",
                       "criteria": {"true": "해소되지 않은 유치권이 남아 있음", "false": "남은 유치권이 없음"}},
}

_DATE = re.compile(r"(?<!\d)(\d{4}|\d{2})\s?\.\s?(\d{1,2})\s?\.\s?(\d{1,2})\s?\.?")


class JevError(RuntimeError):
    pass


def available() -> bool:
    return bool(env_value("TYPESAFE_API_KEY"))


def ask(state: str, questions: dict[str, dict[str, Any]], retries: int = 4) -> dict[str, Any]:
    """한 번 호출. 429·529 는 물러났다 다시, 나머지 오류는 JevError."""
    key = env_value("TYPESAFE_API_KEY")
    if not key:
        raise JevError("TYPESAFE_API_KEY 가 없습니다 (.env)")
    body = json.dumps({"model": MODEL, "state": state, "questions": questions}, ensure_ascii=False).encode()
    for attempt in range(retries):
        request = urllib.request.Request(URL, data=body, method="POST", headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())["answers"]
        except urllib.error.HTTPError as error:
            if error.code in (429, 529) and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise JevError(f"HTTP {error.code}: {error.read()[:200].decode(errors='replace')}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise JevError(str(error)) from error
    raise JevError("재시도 초과")


def _q(name: str, **fmt: str) -> dict[str, Any]:
    spec = QUESTIONS[name]
    q = {"type": spec["type"], "instructions": spec["instructions"].format(**fmt) if fmt else spec["instructions"]}
    if "criteria" in spec:
        q["criteria"] = spec["criteria"]
    return q


def pick_senior(window: str, candidates: list[str]) -> dict[str, Any] | None:
    if not candidates:
        return None
    q = _q("senior")
    q["criteria"] = {c: f"날짜 {c}" for c in candidates} | {"없음": "최선순위 설정일이 보이지 않음"}
    answer = ask(window, {"senior": q})["senior"]
    m = _DATE.search(answer.get("choice", ""))
    if not m:
        return None
    y, mo, d = (int(g) for g in m.groups())
    y = y + 2000 if y < 100 else y
    return {"date": f"{y:04d}-{mo:02d}-{d:02d}", "kind": "", "confidence": answer.get("confidence")}


def find_names(text: str, candidates: list[str], chunk: int = 40) -> list[str]:
    found: list[str] = []
    for start in range(0, len(candidates), chunk):
        part = candidates[start:start + chunk]
        answers = ask(text, {f"c{n}": _q("name", c=c) for n, c in enumerate(part)})
        found += [c for n, c in enumerate(part) if answers[f"c{n}"]["noul"] >= 0.5]
    return found


def second_opinion(note: str, kind: str) -> dict[str, float]:
    names = ("waiver", "other_tenant") if kind == "waiver" else ("lien_resolved", "lien_remaining")
    answers = ask(note, {n: _q(n) for n in names})
    return {n: answers[n]["noul"] for n in names}


def fingerprint(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
