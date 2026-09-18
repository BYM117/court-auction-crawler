"""지금 무엇이 끝났고 무엇이 남았는지를 **측정해서** 말한다.

왜 이게 필요한가
----------------
세션이 여럿 붙어 있고 서로 대화할 수 없다. 지금까지는 상태를 사람이 문서에
**선언**했는데, 갱신을 잊으면 그대로 틀린 채로 남는다. 실제로 `DATA-GAPS.md`가
하루 만에 낡아 이미 끝난 일을 '미해결'이라고 말하고 있었다(2026-09-17 확인).

그래서 여기서는 **선언을 읽지 않는다.** DB 컬럼·코드·파일을 직접 재서
상태를 만든다. 문서를 갱신 안 해도 틀리지 않는다.

    python3 scripts/status.py           # 화면에 출력
    python3 scripts/status.py --write   # STATUS.md 로도 저장

읽기 전용이다. 아무것도 고치지 않는다.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "court_auction_crawler"
WEB = Path.home() / "Documents" / "court-auction-web"


def _git(*args: str) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except Exception:
        return ""


def find_db() -> Path | None:
    """DB를 찾는다. 격리 워크트리에는 data/ 가 안 따라오므로 본체 저장소를 본다.

    이 도구를 제일 필요로 하는 쪽이 워크트리 세션인데, 거기서 안 돌면 만든 뜻이 없다.
    `git rev-parse --git-common-dir` 는 워크트리에서도 **본체**의 .git 을 가리킨다.
    """
    here = ROOT / "data" / "auction.sqlite3"
    if here.exists():
        return here
    common = _git("rev-parse", "--git-common-dir")
    if not common:
        return None
    p = Path(common)
    if not p.is_absolute():
        p = (ROOT / p).resolve()
    main = p.parent / "data" / "auction.sqlite3"
    return main if main.exists() else None


DB = find_db()

DONE, WIP, TODO, FIXED_LIMIT, UNKNOWN = "해결됨", "진행 중", "미해결", "제약", "확인불가"


# ── 재는 도구 ────────────────────────────────────────────────────────────────
def q1(sql: str, default=0):
    """DB에서 숫자 하나. DB가 없거나 잠겨 있거나 컬럼이 없으면 None을 돌려준다."""
    if DB is None:
        return None
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute(sql).fetchone()
            return row[0] if row and row[0] is not None else default
        finally:
            con.close()
    except sqlite3.Error:
        return None


def src_has(filename: str, needle: str) -> bool | None:
    p = SRC / filename
    if not p.exists():
        return None
    return needle in p.read_text(encoding="utf-8", errors="ignore")


def web_uses(field: str) -> bool | None:
    if not WEB.exists():
        return None
    for sub in ("app", "components", "lib"):
        d = WEB / sub
        if not d.exists():
            continue
        for f in d.rglob("*.ts*"):
            try:
                if field in f.read_text(encoding="utf-8", errors="ignore"):
                    return True
            except OSError:
                continue
    return False


def pct(part, whole) -> str:
    if not whole or part is None:
        return "—"
    return f"{100 * part / whole:.0f}%"



# ── 결과를 재는 도구 (원인이 아니라 결과를 본다) ───────────────────────────
# 법정동 비교는 **수집기 본체와 같은 규칙을 써야 한다.** 여기서 따로 두면 두 곳이
# 어긋나고, 그러면 지표가 코드를 못 따라간다. geocoder 가 이 규칙으로 응답을 거르므로
# 판정도 같은 것을 쓴다.
sys.path.insert(0, str(ROOT / "src"))
try:
    from court_auction_crawler.geocoder import legal_dong, same_place  # noqa: E402
except Exception:  # 워크트리에 src 가 없거나 임포트가 깨져도 도구는 돌아야 한다
    def legal_dong(text):  # type: ignore[misc]
        return None

    def same_place(left, right):  # type: ignore[misc]
        return None


def wrong_place() -> int | None:
    """지오코더의 답이 **지오코더가 받은 질문**과 어긋나는 활성 물건 수.

    G12에서 '망가진 쿼리'는 **원인**이고 이것이 **결과**다. 원인만 보면 쿼리를
    고쳐도 이미 박힌 틀린 좌표가 안 잡힌다. 다만 이 측정 자체가 틀리면 멀쩡한
    것을 틀렸다고 하므로, 법정동을 못 고르는 주소는 **세지 않는다**.

    무엇과 대조하느냐가 핵심이다. `geocoder` 가 대조하는 것과 **같은 것**을
    대조해야 한다. 어긋나면 지표가 코드로 막을 수 없는 것을 계속 고발한다.

      주소 검색(source='address') 은 일부러 깎은 쿼리로 묻는다. 법원이
      `양천로 400-12 (가양동, 더리브골드타워)` 라 해도 `양천로 400-12` 로 묻고,
      브이월드는 `양천로 400-12 (등촌동)` 이라 답한다 — 도로명을 그대로 되받은
      **정확한 핀**인데 법원 괄호와 동 이름만 다르다. 원본과 대면 이것이 전부
      틀린 것이 된다(실측 179건 중 125건이 이 경우였다). 그래서 쿼리와 댄다.

      건물명 검색(source='building') 은 이름으로 찾으므로 다른 동의 동명이건물을
      집어온다(`서구 삼우빌라` → 가좌동이 아닌 석남동). 여기서는 코드도 원본과
      대조하므로 지표도 원본과 댄다.
    """
    if DB is None:
        return None
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        return None
    n = 0
    try:
        for addr, norm, query, source in con.execute(
            "SELECT address, normalized_address, geocode_query, coordinate_source "
            "FROM auction_items "
            "WHERE is_active=1 AND coordinate_quality IN ('verified','approximate')"):
            asked = query if source == "address" else addr
            if same_place(asked or "", norm) is False:
                n += 1
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return n


def sample_details(limit: int, where: str = "detail_status='collected'"):
    """detail_json 을 조금만 읽는다. 전수는 4GB라 몇 분 걸린다.

    **최근에 받은 것부터 본다.** 정렬 없이 `LIMIT`만 걸면 가장 오래된 것을 집어,
    수집기를 고쳐도 지표가 영영 안 움직인다(G08에서 실제로 그럴 뻔했다). 지표는
    고친 코드가 만든 데이터를 볼 수 있어야 한다.
    """
    if DB is None:
        return []
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
        rows = [r[0] for r in con.execute(
            f"SELECT detail_json FROM auction_items WHERE {where} "
            f"ORDER BY detail_collected_at DESC LIMIT {limit}")]
        con.close()
        return rows
    except sqlite3.Error:
        return []


def near_stats_filled(sample: int = 200) -> int | None:
    """인근매각통계에 데이터 행이 실제로 들어온 물건 수(표본)."""
    rows = sample_details(sample)
    if not rows:
        return None
    hit = 0
    for dj in rows:
        try:
            d = json.loads(dj)
        except Exception:
            continue
        for t in (d.get("sections") or []) + (d.get("tables") or []):
            if "인근매각통계" not in (t.get("caption") or ""):
                continue
            body = [r for r in (t.get("rows") or [])[1:] if any(str(c).strip() for c in r)]
            if body:
                hit += 1
                break
    return hit


def recent_coverage_warnings() -> tuple[int, int] | None:
    """(울린 경고 수, 판정을 돌린 횟수). **둘 다 세야 한다.**

    경고 0건은 '좋아졌다' 일 수도 '아예 안 쟀다' 일 수도 있다. 커버리지 기록은
    예정 창이 120일 이상인 full 사이클에서만 돌아가므로, quick 사이클만 돈 날은
    판정이 한 번도 실행되지 않는다(실측: 9/18 사이클 3회 중 기록 0회).
    **분모를 같이 내놓지 않으면 0이 초록불로 읽힌다** — 오늘 열 번째로 밟은 함정이다.
    """
    log = ROOT / "logs" / "collect-all.log"
    if not log.exists():
        return None
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return (text.count("커버리지 경고"), text.count("법원별 수집 기록 저장"))


def building_match_rate() -> tuple[int, int]:
    """(건물 표본, 이 물건의 값이라 말해도 되는 것). G13 의 B 몫이다.

    **토지를 빼고 센다.** 토지는 특정 지번이 6개월 안에 거래될 확률이 낮아 구조적으로
    0%다(실측 316건 중 parcel·name 0건). 섞어 세면 건물 37%가 전체 17%로 보여
    "안 고쳐졌다"고 읽힌다 — 분모가 다른 것이지 성적이 나쁜 게 아니다.
    """
    if DB is None:
        return (0, 0)
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
        rows = con.execute(
            "SELECT transactions_detail FROM auction_items "
            "WHERE is_active=1 AND transactions_detail LIKE '%match_level%'").fetchall()
        con.close()
    except sqlite3.Error:
        return (0, 0)
    건물 = 믿을만 = 0
    for (tj,) in rows:
        try:
            t = json.loads(tj)
        except Exception:
            continue
        if t.get("type") == "land":
            continue
        건물 += 1
        if ((t.get("sales") or {}).get("match_level")) in ("parcel", "name"):
            믿을만 += 1
    return (건물, 믿을만)


def screening_can_say_low() -> bool | None:
    """위험도가 '낮음'을 낼 수 있는지 **실제로 호출해서** 본다.

    grep 으로 '가점이 있나'를 보는 것은 원인이다. 최선 조건을 넣고 돌려서
    '낮음'이 나오는지가 결과다.
    """
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from court_auction_crawler.enrichment import build_screening  # noqa: PLC0415
    except Exception:
        return None
    try:
        best = build_screening(
            {"status": "신건"}, 100_000_000, 100_000_000, 100.0,
            {"clean": "서울특별시 중구 1-1"}, 0)
        return best.get("risk_level") == "낮음"
    except Exception:
        return None


def past_sales_shown() -> bool | None:
    """재매각 물건에 '직전에 얼마에 낙찰됐나'가 실려 나가는지 본다."""
    if DB is None:
        return None
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from court_auction_crawler.store import AuctionStore  # noqa: PLC0415
        from court_auction_crawler.enrichment import public_auction_summary  # noqa: PLC0415
        st = AuctionStore(str(DB))
        with st.connect() as c:
            # 기록이 남아 있는 물건으로 시험해야 '기능이 붙었나'를 본다.
            # 아무 재매각 물건이나 집으면 '데이터가 있나'를 재게 된다(다른 질문이다).
            keys = [r[0] for r in c.execute(
                "SELECT i.item_key FROM auction_items i "
                "WHERE i.is_active=1 AND i.resale_reason!='' AND EXISTS("
                "  SELECT 1 FROM auction_sale_results r"
                "   WHERE r.item_key=i.item_key AND r.sale_amount IS NOT NULL) LIMIT 5")]
        if not keys:
            return None
        for k in keys:
            a = (public_auction_summary(st.get_item(k)) or {}).get("auction") or {}
            if a.get("past_sales"):
                return True
        return False
    except Exception:
        return None


def backfill_running() -> int | None:
    """오래된 기일의 결과가 **최근에** 들어왔나 — 보충이 도는지의 결과다."""
    return q1(
        "SELECT COUNT(*) FROM auction_sale_results "
        "WHERE sale_amount IS NOT NULL AND collected_at >= date('now','-2 days') "
        "AND REPLACE(sale_date,'.','-') < date('now','-14 days')")


# ── 격차별 판정 ──────────────────────────────────────────────────────────────
def checks() -> list[dict]:
    active = q1("SELECT COUNT(*) FROM auction_items WHERE is_active=1")
    out: list[dict] = []

    def add(no, title, owner, state, measure, note="", by="측정"):
        """by: 무엇으로 판정했나. 측정=DB·결과물, 시험=함수 호출, 코드=소스에 있나"""
        out.append({"no": no, "title": title, "owner": owner, "state": state,
                    "measure": measure, "note": note, "by": by})

    # G01 — 사건 화면으로 낙찰가 회수
    sold = q1("SELECT COUNT(*) FROM auction_sale_results WHERE sale_amount IS NOT NULL")
    recovered = backfill_running()
    add("G01", "낙찰가 회수(사건 화면)", "A",
        DONE if (recovered or 0) > 0 else (WIP if src_has("detail_crawler.py", "기일내역에서 매각결과") else TODO),
        f"낙찰가 {sold:,}건 · 최근 2일 회수 {recovered or 0:,}건",
        "7일 창 밖의 기일 결과가 최근에 들어오면 보충이 도는 것")

    # G02 — 지분 딱지
    share = src_has("enrichment.py", "지분매각")
    add("G02", "지분매각 딱지", "B", DONE if share else TODO,
        "special_rights에 포함" if share else "없음", by="코드")

    # G03 — 종국결과
    # **훑기는 `detail_checked_at` 으로 판정한다.** 예전에는 `result_checked_at`
    # (매각결과 보충용)을 세어 '훑음 43건' 으로 찍혔다 — 실제로는 9,935건이었다.
    # 지표가 코드와 다른 열을 보면 일한 것이 안 한 것으로 보인다.
    sweep_done = """is_active = 0
          AND last_seen_at >= date('now', '-30 day')
          AND detail_checked_at IS NOT NULL AND detail_checked_at > last_seen_at"""
    sweep_left = """is_active = 0
          AND last_seen_at >= date('now', '-30 day')
          AND (detail_checked_at IS NULL OR detail_checked_at < last_seen_at)"""
    closed = q1("SELECT COUNT(*) FROM auction_items WHERE closing_result NOT IN ('','미종국')")
    scanned = q1(f"SELECT COUNT(*) FROM auction_items WHERE {sweep_done}")
    left = q1(f"SELECT COUNT(*) FROM auction_items WHERE {sweep_left}")
    has_code = src_has("store.py", "result_checked_at")
    add("G03", "취하·기각 파악(종국결과)", "B→A",
        DONE if (closed or 0) > 100 else (WIP if has_code else TODO),
        f"종국 확인 {closed:,}건 · 훑음 {scanned:,} / 남음 {left:,}",
        "코드는 있는데 값이 0이면 큐가 뒤에 밀린 것")

    # G04 — 재매각
    resale = q1("SELECT COUNT(*) FROM auction_items WHERE is_active=1 AND resale_reason!=''")
    flow = q1("SELECT COUNT(*) FROM auction_items WHERE is_active=1 AND item_status_flow!=''")
    add("G04", "재매각 구별·보증금", "B",
        DONE if (flow or 0) > (active or 1) * 0.9 else TODO,
        f"재매각 {resale:,}건 · 물건상태 {pct(flow, active)}")

    # G05 — 낙찰가가 되살아난 물건에 남는 문제 (0에 가까울수록 좋다)
    stale = q1("SELECT COUNT(*) FROM auction_items WHERE is_active=1 AND sold_amount IS NOT NULL")
    hist = past_sales_shown()
    cleared = (stale or 0) <= 5
    have = q1("SELECT COUNT(*) FROM auction_items i WHERE i.is_active=1 AND i.resale_reason!='' "
              "AND EXISTS(SELECT 1 FROM auction_sale_results r "
              "WHERE r.item_key=i.item_key AND r.sale_amount IS NOT NULL)")
    resale_n = q1("SELECT COUNT(*) FROM auction_items WHERE is_active=1 AND resale_reason!=''")
    add("G05", "되살아난 물건의 낙찰가", "B",
        DONE if (cleared and hist) else (WIP if cleared else TODO),
        f"낙찰가 박힘 {stale:,}건 · 이력 실림 {'예' if hist else '아니오'} "
        f"· 기록 보유 {have or 0}/{resale_n or 0}건",
        "법원은 깨진 낙찰의 금액을 지운다. 8/20부터 모았으므로 보유분은 시간이 채운다",
        by="시험")

    # G06 — 감정평가서 본문
    appraisal_body = q1(
        "SELECT COUNT(*) FROM auction_documents "
        "WHERE document_type='감정평가서' AND LENGTH(metadata_json)>2000")
    거짓 = q1("SELECT COUNT(*) FROM auction_documents "
              "WHERE document_type='감정평가서' AND status='collected'")
    add("G06", "감정평가서 본문", "A",
        FIXED_LIMIT if (거짓 or 0) < 100 else TODO,
        f"본문 있는 것 {appraisal_body:,}건 · 거짓 '수집됨' {거짓:,}건",
        "본문은 협회 뷰어라 원천 불가(함정 ⑧). 상태는 정직해졌다")

    # G07 — 사건명
    ctype = q1("SELECT COUNT(*) FROM auction_items WHERE is_active=1 AND case_type!=''")
    add("G07", "임의/강제·형식적경매", "B",
        DONE if (ctype or 0) > (active or 1) * 0.9 else TODO,
        f"사건명 {pct(ctype, active)}")

    # G08 — 인근매각통계
    near_n = near_stats_filled(200)
    add("G08", "인근매각통계 채우기", "A",
        DONE if (near_n or 0) > 10 else TODO,
        f"최근 200건 중 데이터 있음 {near_n if near_n is not None else '—'}건",
        "'인근매각물건사례'의 검색 버튼을 눌러야 채워진다. 소급은 안 된다")

    # G09 — 입찰구분
    bid = src_has("crawler.py", "BidLst")
    add("G09", "입찰구분을 '전체'로", "A", DONE if bid else TODO,
        "설정함" if bid else "기일입찰 기본값 그대로", "지금 손실 0, 재개 대비", by="코드")

    # G10 — 당사자
    parties = q1("SELECT COUNT(*) FROM auction_items WHERE is_active=1 AND parties_json NOT IN ('','{}')")
    add("G10", "당사자 내역 활용", "B",
        DONE if (parties or 0) > (active or 1) * 0.9 else TODO,
        f"당사자 {pct(parties, active)}")

    # G11 — 바깥 기준선
    tool = (ROOT / "scripts" / "baseline_compare.py").exists()
    saved = list((ROOT / "data").glob("baseline*")) if (ROOT / "data").exists() else []
    add("G11", "법원 공식 집계 대조", "C",
        DONE if saved else (WIP if tool else TODO),
        ("대조 자료 " + str(len(saved)) + "건") if saved else ("도구 있음" if tool else "없음"),
        "통계는 다음 달 1일 확정 — 10/1부터 대조 가능", by="파일")

    # G12 — 좌표
    broken = None
    try:
        if DB is None:
            raise sqlite3.Error("no db")
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
        SIDO = ("서울특별시", "부산광역시", "대구광역시", "인천광역시", "광주광역시",
                "대전광역시", "울산광역시", "세종특별자치시", "경기도", "강원도",
                "강원특별자치도", "충청북도", "충청남도", "전라북도", "전북특별자치도",
                "전라남도", "경상북도", "경상남도", "제주특별자치도", "전남광주통합특별시")
        broken = sum(
            1 for (qy,) in con.execute(
                "SELECT geocode_query FROM auction_items "
                "WHERE is_active=1 AND coordinate_source='building'")
            if qy and qy.split()[-1] in SIDO)
        con.close()
    except sqlite3.Error:
        pass
    approx = q1("SELECT COUNT(*) FROM auction_items WHERE is_active=1 AND coordinate_quality='approximate'")
    wrong = wrong_place()
    add("G12", "엉뚱한 좌표", "B",
        DONE if (wrong is not None and wrong < 50) else TODO,
        (f"다른 동네에 찍힘 {wrong}건 · 망가진 쿼리 {broken}건 · approximate {approx:,}건"
         if wrong is not None else "—"),
        "원인(쿼리)이 아니라 결과(다른 동네)로 판정한다. 일부는 읍면 승격이라 정상일 수 있다")

    # G13 — 웹이 안 받는 것
    unused = [f for f in ("transactions", "past_sales", "registry_search_hint") if web_uses(f) is False]
    건물, 믿을만 = building_match_rate()
    비율 = f"건물 매칭 {믿을만*100//건물}%({믿을만}/{건물})" if 건물 else "새 모양 없음"
    add("G13", "웹이 안 받는 payload", "B+D",
        DONE if not unused else (WIP if 건물 and 믿을만 * 3 > 건물 else TODO),
        f"{비율} · " + (("웹 미사용: " + ", ".join(unused)) if unused else "웹이 모두 사용"),
        "B 몫(매칭률)과 D 몫(화면 표시)이 따로다. 토지는 본질상 0%")

    # G14 — 위험도
    low_ok = screening_can_say_low()
    add("G14", "위험도 재설계", "E",
        DONE if low_ok else TODO,
        "최선 조건에서 '낮음' 나옴" if low_ok else "최선 조건에서도 '낮음'이 안 나온다",
        "SCREENING-REDESIGN.md 에서 기준부터", by="시험")

    # G15 — 배당요구종기공고
    notice = src_has("crawler.py", "142M01") or src_has("crawler.py", "배당요구종기공고")
    공고 = q1("SELECT COUNT(*) FROM auction_notices") or 0
    공고법원 = q1("SELECT COUNT(DISTINCT court) FROM auction_notices") or 0
    add("G15", "배당요구종기공고 수집", "A",
        DONE if 공고 > 1000 else (WIP if notice else TODO),
        f"공고로 받은 사건 {공고:,}건 · 법원 {공고법원}곳"
        if 공고 else ("수집 코드 있음" if notice else "화면 미수집"),
        "사건번호만 있다 — 기일·감정가는 아직 없다(감정평가 전)")

    # G16 — 제약
    add("G16", "문서 조회 창(제약)", "전체", FIXED_LIMIT,
        "명세서 기일 1주 전~ / 조사서·평가서 2주 전~", "고칠 것 없음. 알고만 있을 것", by="—")

    # G17 — 커버리지 경고
    # 문구를 엉뚱한 파일에서 찾고 있었다 — '같은 날' 은 store.py 에 있다.
    # 변수명을 한글로 쓴 코드라 영문 이름으로 grep 하면 영영 못 찾는다.
    same_day = src_has("store.py", "최근최대")
    cyc = (ROOT / "scripts" / "cycle_value.py").exists()
    자정 = src_has("cli.py", "_refresh_current_window")
    들어간수 = src_has("cli.py", "들어간수")
    고친것 = [이름 for 이름, 됨 in (("자정 넘김", 자정), ("들어간 수로 셈", 들어간수),
                                ("같은 날 비교", same_day)) if 됨]
    울림 = recent_coverage_warnings()
    잰값 = " · ".join(고친것) if 고친것 else "실측 도구만 있음"
    잰것 = False
    if 울림 is not None:
        경고수, 판정수 = 울림
        잰것 = 판정수 > 0
        잰값 += (f" · 오늘 경고 {경고수}건/판정 {판정수}회" if 잰것
                 else " · **오늘은 판정이 안 돌았다**(quick 사이클만)")
    add("G17", "커버리지 경고 오탐", "C+A",
        DONE if len(고친것) == 3 and 잰것 else (WIP if 고친것 or cyc else TODO),
        잰값, "남은 것: 망가진 사이클 즉시 재시도(2번)",
        by="측정" if 잰것 else "코드")

    return out


# ── 출력 ────────────────────────────────────────────────────────────────────
ICON = {DONE: "✅", WIP: "🔸", TODO: "⬜", FIXED_LIMIT: "📌", UNKNOWN: "❔"}
ORDER = [TODO, WIP, DONE, FIXED_LIMIT, UNKNOWN]


def render(rows: list[dict]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        head = subprocess.run(["git", "log", "-1", "--format=%h %s"], cwd=ROOT,
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        head = ""

    tally = {s: sum(1 for r in rows if r["state"] == s) for s in ORDER}
    L = []
    L.append(f"# 수집기 현황 — {now}")
    L.append("")
    L.append("> **이 파일은 손으로 쓰지 않는다.** `python3 scripts/status.py --write` 가 DB·코드·파일을")
    L.append("> 직접 재서 다시 만든다. 문서를 갱신 안 해도 틀리지 않게 하려는 것이다.")
    L.append("")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD") or "?"
    where = "본체" if (ROOT / "data").exists() else f"워크트리 — DB는 본체 것을 읽었다"
    L.append(f"읽은 곳 `{ROOT.name}` · 브랜치 `{branch}` · {where}")
    L.append("")
    if head:
        L.append(f"마지막 커밋 `{head}`")
        L.append("")
    L.append(f"**{DONE} {tally[DONE]} · {WIP} {tally[WIP]} · {TODO} {tally[TODO]} · {FIXED_LIMIT} {tally[FIXED_LIMIT]}**")
    L.append("")
    L.append("| | 번호 | 제목 | 담당 | 지금 잰 값 | 근거 |")
    L.append("|---|---|---|---|---|---|")
    docs = {p.name.split("-")[0]: p.name for p in sorted((ROOT / "gaps").glob("G*.md"))}
    for s in ORDER:
        for r in sorted((x for x in rows if x["state"] == s), key=lambda x: x["no"]):
            name = docs.get(r["no"])
            link = f"[{r['no']}](gaps/{name})" if name else r["no"]
            L.append(f"| {ICON[s]} | {link} | {r['title']} | {r['owner']} | {r['measure']} | {r.get('by','측정')} |")
    L.append("")

    L.append("## 세션별 다음 할 일")
    L.append("")
    owners = {"A": "수집기", "B": "가공·저장", "C": "운영·검증", "D": "웹", "E": "위험도"}
    for key, name in owners.items():
        mine = [r for r in rows if key in r["owner"] and r["state"] in (TODO, WIP)]
        mine.sort(key=lambda r: (r["state"] != WIP, r["no"]))
        L.append(f"**{key}. {name}** — `sessions/{key}-*.md`")
        if not mine:
            L.append("")
            L.append("  맡은 것이 다 끝났다. 새 몫은 사용자와 정한다.")
        else:
            L.append("")
            for r in mine[:4]:
                L.append(f"  - {ICON[r['state']]} **{r['no']}** {r['title']} — {r['measure']}")
                if r["note"]:
                    L.append(f"    · {r['note']}")
        L.append("")

    L.append("## 읽는 법")
    L.append("")
    L.append("| 표시 | 뜻 |")
    L.append("|---|---|")
    L.append(f"| {ICON[DONE]} | **측정값이 목표에 닿았다.** 선언이 아니라 숫자다 |")
    L.append(f"| {ICON[WIP]} | 도구·코드는 있는데 아직 값이 안 나온다 |")
    L.append(f"| {ICON[TODO]} | 아무도 안 잡았거나, 잡았는데 값이 안 움직인다 |")
    L.append(f"| {ICON[FIXED_LIMIT]} | 고칠 것이 아니라 알고 있어야 할 제약 |")
    L.append("")
    L.append("**근거** 열은 무엇으로 판정했는지다. 여기가 `코드`면 **결과가 아니라 원인을 본 것**이라")
    L.append("초록불이 실제보다 후할 수 있다. 결과를 잴 방법이 생기면 `측정`이나 `시험`으로 바꿀 것.")
    L.append("")
    L.append("| 값 | 뜻 |")
    L.append("|---|---|")
    L.append("| `측정` | DB 숫자를 셌다 |")
    L.append("| `시험` | 함수를 실제로 불러 결과를 봤다 |")
    L.append("| `코드` | 소스에 그 코드가 있는지만 봤다 — **결과는 확인 못 함** |")
    L.append("| `파일` | 산출물 파일이 있는지 봤다 |")
    L.append("")
    L.append("자세한 내용은 `gaps/G**.md`, 분담은 `sessions/_README.md`, 흐름은 `DEVLOG.md`.")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="수집기 현황을 재서 보여준다")
    ap.add_argument("--write", action="store_true", help="STATUS.md 로도 저장한다")
    args = ap.parse_args()

    if DB is None:
        print("DB를 못 찾았다. 본체 저장소의 data/auction.sqlite3 가 있어야 한다.\n"
              "(워크트리에서는 git rev-parse --git-common-dir 로 본체를 찾는다)",
              file=sys.stderr)
        return 1

    text = render(checks())
    print(text)
    if args.write:
        (ROOT / "STATUS.md").write_text(text + "\n", encoding="utf-8")
        print(f"\n→ {ROOT / 'STATUS.md'} 에 저장했다", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
