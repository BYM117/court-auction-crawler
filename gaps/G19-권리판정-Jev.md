# G19. 받아둔 문서를 **읽어서** 권리를 판정한다 — 대항력·유치권 해소·점유 확인·실명

**진행 중** (Jev 세션, 2026-09-23~) · **담당**: Jev 세션 (규칙 `rights.py` · Jev `jev.py`)

## 한 줄
매각물건명세서·현황조사서·비고를 3만 건 넘게 받아두고, **글자 포함 여부**로만 봤다.
"대항력을 포기" 가 '대항력 있는 임차인' 으로, "유치권 신고 취하" 가 '유치권' 으로 찍혔다.
대항력 판정에 필요한 최선순위 설정일은 뽑지도 않았다.

## 무엇을 하나

| 판정 | 방식 | 정답지 점수(2026-09-23) |
|---|---|---|
| 최선순위 설정일 | 규칙(95.3%) + 칸이 찢어진 나머지는 Jev 가 날짜 후보 중 고름 | 규칙이 놓친 28건 Jev 28/28 |
| 대항력 있음/없음 | 전입일 < 최선순위 (코드가 비교) | 법원이 직접 쓴 결론과 94% 일치 |
| 대항력 포기 | 포기 뒤 **다른** 임차인을 암시할 때만 위험 유지 | 90/90 |
| 유치권 해소 | 취하·철회·부존재 확정 = 해소, 원고 패소·일부 승소·존재확인 = 남음 | 66/67 |
| 점유자 직접 확인 | 만남·통화 = 확인, 관리사무소·이웃은 아님 | 40/40 |
| 실명 | 진행 중엔 그대로, **끝나면 가림**(옥션원 방식). 문장 속 이름은 Jev | 12/12 |

## 어디에 있나
- `rights_json` 칸(물건 행) — 목록 스냅샷엔 요약만(`rights_opposable` 등), 상세엔 전부.
- payload `rights` — 목록: `opposable`·`lien`·`occupant_met`·`label_unverified`,
  상세: + `senior`·`tenants`·`survey.memo`·`parties`(끝나면 가림).
- 딱지: `유치권해소`(보통) 추가, `대항력포기` 규칙 정밀화.

## 진화 — 정답지가 기준이다
`jev_golden/README.md`. 사이클이 규칙↔Jev 엇갈림을 모으고(`scripts/jev_review.py`),
정답을 매겨 보태고, `scripts/jev_eval.py` 가 **최고점보다 떨어지면 막는다.**

## 확인 방법
```bash
python3 scripts/status.py             # G19 줄: 판정 비율·대항력 있음/없음·Jev 미룸·오류·엇갈림
python3 scripts/jev_eval.py --no-jev  # 규칙만 정답지로
```

## 남은 것
- 웹 표시(세션 D 몫): `rights` 필드, `대항력포기`·`유치권해소` 안내문(지금은 `/대항력/`·`/유치권/`
  조각에 걸려 빨간 안내가 뜬다), 당사자 실명.
- 계산된 '대항력 있음' 을 위험도 등급에 반영할지 — 사용자 결정(G14).

## ⏸ status.py 탐침 — 보류 중
2026-09-24 에 원본 폴더의 `scripts/status.py` 에 다른 세션(B, 사이트 점검 대응)의 커밋 안 된 변경이
있어, 이 파일을 건드리면 main 에 합칠 수 없었다. **B 가 status.py 를 커밋하면 아래 블록을
`checks()` 의 `return out` 바로 앞에 넣는다**(`test_status_metric` 이 G19 문서를 요구하니 이 문서는 그대로).

```python
    # 권리 판정(rights.py + Jev 꼬리) — 결과를 센다. Jev 실패는 사이클을 안 멈추므로
    # 여기서 안 세면 조용히 빈다(함정 ④). 엇갈림은 정답지 후보(scripts/jev_review.py).
    판정 = q1("SELECT COUNT(*) FROM auction_items WHERE is_active=1 AND rights_json <> ''")
    if 판정 is None:
        add("G19", "권리 판정(대항력·유치권·점유·실명)", "Jev", TODO, "rights_json 칸 없음 — 판정 단계 전")
    else:
        있음 = q1("SELECT COUNT(*) FROM auction_items WHERE is_active=1 "
                  "AND json_extract(NULLIF(rights_json,''),'$.opposability.summary')='있음'")
        없음 = q1("SELECT COUNT(*) FROM auction_items WHERE is_active=1 "
                  "AND json_extract(NULLIF(rights_json,''),'$.opposability.summary')='없음'")
        미룸 = q1("SELECT COUNT(*) FROM auction_items WHERE json_extract(NULLIF(rights_json,''),'$.jev_pending')=1")
        오류 = q1("SELECT COUNT(*) FROM auction_items WHERE json_extract(NULLIF(rights_json,''),'$.jev.error') IS NOT NULL")
        엇갈림 = q1("SELECT COUNT(*) FROM auction_items "
                    "WHERE json_array_length(json_extract(NULLIF(rights_json,''),'$.review')) > 0")
        add("G19", "권리 판정(대항력·유치권·점유·실명)", "Jev",
            DONE if active and 판정 >= active * 0.9 and (오류 or 0) <= 판정 * 0.05 else (WIP if 판정 else TODO),
            f"진행 {pct(판정, active)} 판정 · 대항력 있음 {있음:,}/없음 {없음:,} · Jev 미룸 {미룸:,} · 오류 {오류:,} · 엇갈림 {엇갈림:,}",
            "엇갈림은 python3 scripts/jev_review.py 로 정답을 매겨 정답지에 보탠다")
```
