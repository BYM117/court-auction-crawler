# G13. 수집기가 보내는데 웹이 안 받는다 + **실거래가 매칭률 3.5%**

**상태**: 미해결 · **담당**: 세션 B(정확도) + 세션 D(표시) · **우선순위**: 높음

## 한 줄
`CLAUDE.md`가 *"경계는 R2 객체다"* 라고 적은 그 경계에서, **이쪽은 올리는데 저쪽은 안 내린다.**

## 원인
payload 필드를 전수로 뽑아(`public_auction_summary` 103개 / `public_auction_detail` 240개)
웹 저장소에서 하나씩 찾아봤다. **네 덩어리가 아무 데서도 안 쓰인다.**

| payload 필드 | 확보량 | 웹 사용 |
|---|---|---|
| `property.transactions` (국토부 실거래가) | 활성 19,241건 | **없음** |
| `property.share` (지분 매각·지분율) | 지분 물건 전부 | 없음 (screening.flags로 간접만) |
| `property.registry_search_hint` | 전량 | 없음 |
| `map.coordinate_quality` | 전량 | 없음 (→ `G12`) |

`screening`·`type_guess`·`special_rights`·`land_use`·`official`·`building`·`popularity`·`sold`는
잘 쓰이고 있다. 문제는 위 넷이다.

---

## ⚠ 실거래가는 지금 상태로 그냥 붙이면 안 된다 — 이게 더 중요하다

`transactions`의 건물 매칭률을 실측했다.

| | 그 건물 매칭 | 동네 평균만 |
|---|---|---|
| 매매 | **664 (3.5%)** | 18,577 |
| 전월세 | **1,001 (5.2%)** | 18,240 |

**96.5%가 그 건물 실거래가 아니라 동네 평균이다.**
그런데 필드 이름은 `transactions`이고 `building`에 건물명이 들어 있다.

```json
{"type":"villa","building":"에스아이팰리스센트럴성내",
 "sales":{"matched":false,"count":880,"min":11000,"avg":47006,"max":360000,
          "recent":[{"name":"금성빌리지2차","area":62.09,"floor":"3","amount":40000,"date":"2026-07-31"}...]}}
```

`matched: false`인데 `building`에 건물명이 붙어 있어서, 화면에 무심코 얹으면
**"이 건물 실거래가"로 읽힌다.** 경매 입찰가 판단에 직결되는 숫자라 오해 비용이 크다.

**웹이 안 쓰고 있던 게 오히려 다행이었다.**

---

## 고치는 법

**세션 B (우리 쪽 숙제)**
1. **매칭률 3.5%를 끌어올린다.** 건물명 정규화 · 면적 대조 · PNU 기반 조회를 검토한다.
   **이게 안 고쳐지면 꽁지맵이 뭘 만들어도 틀린 숫자를 예쁘게 보여주는 것뿐이다.**
2. `property.share`를 `special_rights`에도 넣는다.
3. `G07`의 `case_type`도 함께 싣는다.

**세션 D (꽁지맵)**
4. `sales.matched` / `rent.matched`를 **화면에 반드시 드러낸다.**
   매칭이면 "이 건물", 아니면 "○○동 평균(880건)"처럼 범위를 명시한다.
   **이 구분 없이 붙이는 것은 하지 말 것.**
5. 목표는 네이버 부동산처럼 **"언제, 얼마에 팔렸다"**를 보여주는 것이다.
6. `coordinate_quality`를 써서 `approximate`는 "대략 위치"임을 드러낸다(`G12`).
7. `property.share`를 직접 쓴다.

> 2026-09-16 사용자 방침: *"웹이 이 값을 잘 쓸 수 있게 해야지, 옥션원처럼."*
> **payload에 실어 보내는 것으로 끝이 아니다. 꽁지맵 쪽 작업이 짝으로 필요하다.**

## 확인
```python
from court_auction_crawler.enrichment import public_auction_summary, public_auction_detail
# → 각각 103 / 240개 필드. 웹 저장소에서 grep으로 대조한다.
```

## 관련
`G02`(share) · `G07`(case_type) · `G12`(coordinate_quality) · `CLAUDE.md` 경계 규칙
