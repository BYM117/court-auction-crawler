from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator

from .enrichment import (
    parse_case_closing,
    parse_case_item,
    parse_case_parties,
    parse_case_type,
)
from .common import CASE_NO_RE, TERMINAL_STATUS_KEYWORDS, utc_now
from .models import AuctionItem, SyncSummary
from .utils import clean_text, parse_date, parse_money, parse_sale_result


SCHEMA_VERSION = 4


# 목록에서 사라진 뒤로 상세를 한 번도 안 받은 물건. 받고 나면
# detail_collected_at > last_seen_at이 되어 스스로 큐에서 빠진다.
# collected_at이 아니라 **checked_at**이다. 조회불가·실패로 끝난 물건은 collected_at이
# 안 올라가서, 그걸로 걸면 같은 물건이 영영 큐 앞에 남는다(매각결과 보충에서 이미
# 한 번 겪은 정체다). checked_at은 성공·실패·조회불가 모두에서 올라간다.
CLOSING_SWEEP_SQL = (
    "is_active = 0"
    " AND COALESCE(last_seen_at, '') >= ?"
    " AND (detail_checked_at IS NULL OR detail_checked_at < last_seen_at)"
)


def closing_sweep_cutoff(days: int) -> str:
    """이보다 오래 전에 사라진 물건은 훑지 않는다. 사건이 종국되면 기일 정보가 끊기고,
    30일이 더 지나면 기본정보만 남는다. 뒤늦게 파도 얻을 게 줄어든다."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


ITEM_LIST_SELECT = """
                SELECT item_key, source, case_no, item_no, court, address, category, appraisal,
                       minimum_bid, sale_date, status, detail_url, lat, lng, pnu,
                       coordinate_source, coordinate_quality, normalized_address,
                       geocode_query, geocoded_at,
                       resale_reason, item_status_flow, deposit_amount, deposit_rate, item_note,
                       case_type, closing_result, closing_date, parties_json,
                       official_price, official_price_type, official_price_year,
                       official_price_detail, official_price_status, official_price_at,
                       first_seen_at,
                       last_seen_at, last_changed_at, next_check_at, is_active,
                       crawl_priority, detail_status, detail_collected_at,
                       detail_checked_at, detail_next_retry_at, detail_fail_count,
                       updated_at,
                       -- 비고에 특수권리(유치권·법정지상권·지분매각 등)가 문장으로 들어
                       -- 있어 목록에서도 태그로 뽑아 쓴다. 평균 400바이트라 부담이 없다.
                       raw_json,
                       -- 목록 썸네일. 사진 전체를 실으면 스냅샷이 몇 배로 불어나므로
                       -- 대표 한 장의 해시·타입만 싣고 웹이 객체 이름을 만들게 한다.
                       (SELECT a.sha256 FROM auction_assets a
                         WHERE a.item_key = auction_items.item_key AND a.kind = 'photo'
                         ORDER BY a.id LIMIT 1) AS thumb_sha256,
                       (SELECT a.content_type FROM auction_assets a
                         WHERE a.item_key = auction_items.item_key AND a.kind = 'photo'
                         ORDER BY a.id LIMIT 1) AS thumb_content_type,
                       (SELECT p.view_count FROM auction_popularity p
                         WHERE p.item_key = auction_items.item_key) AS view_count,
                       (SELECT p.interest_count FROM auction_popularity p
                         WHERE p.item_key = auction_items.item_key) AS interest_count,
                       -- 법원 '용도'는 '상가,오피스텔,근린시설' 같은 검색 그룹 라벨로만
                       -- 오는 물건이 활성 4천 건이라, 목록에서 용도를 가릴 단서는 건축물대장
                       -- 주용도뿐이다. 대장·토지이용계획 전체를 실으면 스냅샷이 14MB 불어나므로
                       -- 쓰는 값만 뽑는다. 조회 전(''), 조회했으나 없음('{}') 모두 NULL이 된다.
                       json_extract(NULLIF(building_detail, ''), '$.main_purpose') AS building_main_purpose,
                       json_extract(NULLIF(building_detail, ''), '$.use_apr_day') AS building_use_apr_day,
                       json_extract(NULLIF(building_detail, ''), '$.hhld_cnt') AS building_hhld_cnt,
                       json_extract(NULLIF(building_detail, ''), '$.grnd_flr_cnt') AS building_grnd_flr_cnt,
                       json_extract(NULLIF(land_use_detail, ''), '$.zone') AS land_use_zone,
                       sold_amount, sold_date
                  FROM auction_items
                """


# 건축물대장이 있을 수 없는 지목. 법원 '용도' 값 그대로다. '대지'는 성공률 21%라 뺀다.
LAND_ONLY_CATEGORIES = ("전답", "임야", "대지,임야,전답")
LAND_ONLY_RETRY_DAYS = 365


CASE_KEYS = ("사건번호", "사건", "case_no")
ITEM_KEYS = ("물건번호", "물건", "item_no")
ADDRESS_KEYS = ("소재지", "주소", "address")
CATEGORY_KEYS = ("용도", "종류", "category")
APPRAISAL_KEYS = ("감정평가액", "감정가", "appraisal")
MINIMUM_KEYS = ("최저매각가격", "최저가", "최저매각가", "minimum_bid")
SALE_DATE_KEYS = ("매각기일", "입찰기일", "sale_date")
STATUS_KEYS = ("진행상태", "상태", "status")
COURT_KEYS = ("법원", "담당법원", "court")
DETAIL_URL_KEYS = ("상세URL", "detail_url")
SOURCE_KEYS = ("수집구분", "source")
# 예정/진행 검색이 같은 물건을 번갈아 목격할 때 해시가 출렁이지 않도록
# 수집 경로에 따라 달라지는 키는 해시에서 제외한다.
VOLATILE_HASH_KEYS = frozenset({"수집구분", "source", "상세URL", "detail_url"})
LIST_HASH_KEYS = (
    "법원",
    "사건번호",
    "물건번호",
    "진행상태",
    "매각기일",
    "최저매각가격",
    "감정평가액",
    "소재지",
    "용도",
)
PARCEL_LIST_KEY = "소재지목록"
REGION_ALIASES = {
    "서울": ("서울", "서울특별시"),
    "경기": ("경기", "경기도", "수원", "성남", "안산", "안양", "평택", "고양", "의정부"),
    "인천": ("인천", "인천광역시"),
    "부산": ("부산", "부산광역시"),
    "대구": ("대구", "대구광역시"),
    "광주": ("광주", "광주광역시"),
    "대전": ("대전", "대전광역시"),
    "울산": ("울산", "울산광역시"),
    "세종": ("세종", "세종특별자치시"),
    "강원": ("강원", "강원도", "강원특별자치도", "춘천", "강릉", "원주", "속초", "영월"),
    "충북": ("충북", "충청북도", "청주", "충주", "제천", "영동"),
    "충남": ("충남", "충청남도", "천안", "공주", "논산", "서산", "홍성"),
    "전북": ("전북", "전라북도", "전북특별자치도", "전주", "군산", "정읍", "남원"),
    "전남": ("전남", "전라남도", "목포", "순천", "해남", "장흥"),
    "경북": ("경북", "경상북도", "대구지방법원", "안동", "경주", "김천", "상주", "의성", "영덕", "포항"),
    "경남": ("경남", "경상남도", "창원", "마산", "진주", "통영", "밀양", "거창"),
    "제주": ("제주", "제주특별자치도"),
}


@dataclass(slots=True)
class UpsertResult:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


class AuctionStore:
    def __init__(self, db_path: str | Path = "auction_data.sqlite3") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        # 웹서버·수집 서브프로세스·watch 러너가 같은 파일을 동시에 쓰므로
        # WAL이 없으면 database is locked가 난다.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS auction_items (
                    item_key TEXT PRIMARY KEY,
                    source TEXT NOT NULL DEFAULT '',
                    case_no TEXT,
                    item_no TEXT,
                    court TEXT,
                    address TEXT,
                    category TEXT,
                    appraisal TEXT,
                    minimum_bid TEXT,
                    sale_date TEXT,
                    status TEXT,
                    detail_url TEXT,
                    lat REAL,
                    lng REAL,
                    pnu TEXT NOT NULL DEFAULT '',
                    coordinate_source TEXT NOT NULL DEFAULT '',
                    coordinate_quality TEXT NOT NULL DEFAULT 'missing',
                    normalized_address TEXT NOT NULL DEFAULT '',
                    geocode_query TEXT NOT NULL DEFAULT '',
                    geocoded_at TEXT,
                    raw_json TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    content_hash TEXT NOT NULL,
                    list_hash TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_changed_at TEXT,
                    next_check_at TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    crawl_priority INTEGER NOT NULL DEFAULT 0,
                    crawl_fail_count INTEGER NOT NULL DEFAULT 0,
                    detail_status TEXT NOT NULL DEFAULT 'pending',
                    detail_collected_at TEXT,
                    detail_checked_at TEXT,
                    detail_next_retry_at TEXT,
                    detail_fail_count INTEGER NOT NULL DEFAULT 0,
                    detail_error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_auction_items_last_seen
                    ON auction_items(last_seen_at DESC);
                CREATE INDEX IF NOT EXISTS idx_auction_items_sale_date
                    ON auction_items(sale_date);
                CREATE INDEX IF NOT EXISTS idx_auction_items_status
                    ON auction_items(status);
                CREATE INDEX IF NOT EXISTS idx_auction_items_source
                    ON auction_items(source);
                CREATE INDEX IF NOT EXISTS idx_auction_items_coordinates
                    ON auction_items(lat, lng);
                CREATE TABLE IF NOT EXISTS auction_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_key TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    old_json TEXT,
                    new_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sync_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    summary_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS court_run_stats (
                    finished_at TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    court TEXT NOT NULL,
                    item_count INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS auction_sale_results (
                    court TEXT NOT NULL,
                    case_no TEXT NOT NULL,
                    item_no TEXT NOT NULL,
                    sale_date TEXT NOT NULL,
                    result TEXT NOT NULL DEFAULT '',
                    sale_amount INTEGER,
                    minimum_bid INTEGER,
                    appraisal INTEGER,
                    item_key TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    collected_at TEXT NOT NULL,
                    PRIMARY KEY (court, case_no, item_no, sale_date)
                );

                CREATE INDEX IF NOT EXISTS idx_sale_results_item
                    ON auction_sale_results(item_key, sale_date DESC);

                CREATE TABLE IF NOT EXISTS auction_popularity (
                    item_key TEXT PRIMARY KEY,
                    view_count INTEGER,
                    interest_count INTEGER,
                    checked_at TEXT NOT NULL
                );

                -- 배당요구종기공고(G15). 경매개시결정된 사건이 매각공고보다 몇 달 먼저
                -- 여기 뜬다. **기일도 감정가도 없는 사건 단위**라 auction_items 에 섞으면
                -- 웹 목록이 깨진다. 사건번호를 먼저 확보하는 것이 목적이라 따로 쌓는다.
                CREATE TABLE IF NOT EXISTS auction_notices (
                    court TEXT NOT NULL,
                    case_no TEXT NOT NULL,
                    address TEXT NOT NULL DEFAULT '',
                    owner TEXT NOT NULL DEFAULT '',
                    debtor TEXT NOT NULL DEFAULT '',
                    notice_date TEXT NOT NULL DEFAULT '',
                    dept TEXT NOT NULL DEFAULT '',
                    opened_at TEXT NOT NULL DEFAULT '',
                    dividend_deadline TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (court, case_no)
                );

                CREATE TABLE IF NOT EXISTS auction_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_key TEXT NOT NULL,
                    document_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    title TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    file_path TEXT NOT NULL DEFAULT '',
                    content_type TEXT NOT NULL DEFAULT '',
                    file_size INTEGER NOT NULL DEFAULT 0,
                    sha256 TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    checked_at TEXT NOT NULL,
                    collected_at TEXT,
                    next_retry_at TEXT,
                    fail_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    UNIQUE(item_key, document_type)
                );

                CREATE TABLE IF NOT EXISTS auction_assets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    file_path TEXT NOT NULL,
                    content_type TEXT NOT NULL DEFAULT '',
                    file_size INTEGER NOT NULL DEFAULT 0,
                    sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_key, kind, label, sha256)
                );
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(auction_items)").fetchall()}
            if "source" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN source TEXT NOT NULL DEFAULT ''")
            if "list_hash" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN list_hash TEXT NOT NULL DEFAULT ''")
            if "last_changed_at" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN last_changed_at TEXT")
            if "next_check_at" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN next_check_at TEXT")
            for sold_col, sold_ddl in (
                ("sold_amount", "INTEGER"),
                ("sold_date", "TEXT NOT NULL DEFAULT ''"),
                # 사건 화면 '물건내역'에서 뽑아 둔다. 목록 조회는 detail_json을 안 읽으므로
                # (763MB라 스냅샷이 감당 못 한다) 여기 적어야 목록에서도 보인다.
                ("resale_reason", "TEXT NOT NULL DEFAULT ''"),
                ("item_status_flow", "TEXT NOT NULL DEFAULT ''"),
                ("deposit_amount", "INTEGER"),
                ("deposit_rate", "REAL"),
                # 물건비고('특별매각조건: …', '미납관리비 …원 있음')는 목록 비고에 없는
                # 특수권리 단서다. 목록에서도 태그로 쓰려면 여기 있어야 한다.
                ("item_note", "TEXT NOT NULL DEFAULT ''"),
                # 사건명(부동산임의경매/강제경매/형식적경매). 사건 단위라 물건마다 같다.
                ("case_type", "TEXT NOT NULL DEFAULT ''"),
                # 취하·기각·취소. 목록 화면은 '신건'과 '유찰 N회'밖에 안 줘서, 물건이
                # 왜 사라졌는지는 사건 화면의 종국결과로만 알 수 있다.
                ("closing_result", "TEXT NOT NULL DEFAULT ''"),
                ("closing_date", "TEXT NOT NULL DEFAULT ''"),
                # 당사자 구성(임차인·교부권자·가압류권자 수 등). 이름은 가려져 있어도
                # 구분은 안 가려진다. 목록에서도 쓰려면 여기 있어야 한다.
                ("parties_json", "TEXT NOT NULL DEFAULT ''"),
            ):
                if sold_col not in columns:
                    conn.execute(f"ALTER TABLE auction_items ADD COLUMN {sold_col} {sold_ddl}")
            # 매각결과 보충이 어디까지 훑었는지. 이게 없으면 대기열 맨 앞이 영영
            # 안 비켜서(그 기일에 결과가 아예 안 올라오는 물건이 많다) 같은 사건을
            # 패스마다 다시 걷는다 — 실측: 15번째 패스는 870사건에서 160행뿐이었다.
            if "result_checked_at" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN result_checked_at TEXT")
            for land_col, land_ddl in (
                ("land_use_detail", "TEXT NOT NULL DEFAULT ''"),
                ("land_use_status", "TEXT NOT NULL DEFAULT ''"),
                ("land_use_at", "TEXT"),
            ):
                if land_col not in columns:
                    conn.execute(f"ALTER TABLE auction_items ADD COLUMN {land_col} {land_ddl}")
            if "is_active" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
            if "crawl_priority" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN crawl_priority INTEGER NOT NULL DEFAULT 0")
            if "crawl_fail_count" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN crawl_fail_count INTEGER NOT NULL DEFAULT 0")
            if "lat" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN lat REAL")
            if "lng" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN lng REAL")
            if "pnu" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN pnu TEXT NOT NULL DEFAULT ''")
            if "coordinate_source" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN coordinate_source TEXT NOT NULL DEFAULT ''")
            if "coordinate_quality" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN coordinate_quality TEXT NOT NULL DEFAULT 'missing'")
            if "normalized_address" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN normalized_address TEXT NOT NULL DEFAULT ''")
            if "geocode_query" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN geocode_query TEXT NOT NULL DEFAULT ''")
            if "geocoded_at" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN geocoded_at TEXT")
            if "official_price" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN official_price REAL")
            if "official_price_type" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN official_price_type TEXT NOT NULL DEFAULT ''")
            if "official_price_year" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN official_price_year TEXT NOT NULL DEFAULT ''")
            if "official_price_detail" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN official_price_detail TEXT NOT NULL DEFAULT ''")
            if "official_price_status" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN official_price_status TEXT NOT NULL DEFAULT ''")
            if "official_price_at" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN official_price_at TEXT")
            if "building_detail" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN building_detail TEXT NOT NULL DEFAULT ''")
            if "building_status" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN building_status TEXT NOT NULL DEFAULT ''")
            if "building_at" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN building_at TEXT")
            if "transactions_detail" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN transactions_detail TEXT NOT NULL DEFAULT ''")
            if "transactions_status" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN transactions_status TEXT NOT NULL DEFAULT ''")
            if "transactions_at" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN transactions_at TEXT")
            if "detail_status" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN detail_status TEXT NOT NULL DEFAULT 'pending'")
            if "detail_collected_at" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN detail_collected_at TEXT")
            if "detail_checked_at" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN detail_checked_at TEXT")
            if "detail_next_retry_at" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN detail_next_retry_at TEXT")
            if "detail_fail_count" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN detail_fail_count INTEGER NOT NULL DEFAULT 0")
            if "detail_error" not in columns:
                conn.execute("ALTER TABLE auction_items ADD COLUMN detail_error TEXT NOT NULL DEFAULT ''")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_auction_items_next_check
                    ON auction_items(is_active, next_check_at, crawl_priority)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_auction_items_coordinates
                    ON auction_items(lat, lng)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_auction_items_detail_due
                    ON auction_items(is_active, detail_status, detail_next_retry_at, sale_date)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_auction_documents_item
                    ON auction_documents(item_key, document_type)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_auction_assets_item
                    ON auction_assets(item_key, kind)
                """
            )
            # 웹으로 무엇을 어떤 내용으로 올렸는지. 이게 없으면 매일 사진 12G를 다시 올린다.
            # hash는 종류별로 다른 걸 본다 - 물건은 content_hash, 사진은 sha256.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS web_sync (
                    kind TEXT NOT NULL,
                    ref TEXT NOT NULL,
                    hash TEXT NOT NULL DEFAULT '',
                    remote_key TEXT NOT NULL DEFAULT '',
                    size INTEGER NOT NULL DEFAULT 0,
                    pushed_at TEXT NOT NULL,
                    PRIMARY KEY (kind, ref)
                )
                """
            )
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < SCHEMA_VERSION:
                if version < 1:
                    conn.execute("UPDATE auction_items SET source = '진행' WHERE source = ''")
                    conn.execute("UPDATE auction_items SET list_hash = content_hash WHERE list_hash = ''")
                    conn.execute("UPDATE auction_items SET last_changed_at = updated_at WHERE last_changed_at IS NULL")
                    conn.execute("UPDATE auction_items SET next_check_at = updated_at WHERE next_check_at IS NULL")
                    self._backfill_schedule(conn)
                if version < 2:
                    self._migrate_v2_rekey_and_merge(conn)
                if version < 3:
                    conn.execute(
                        "UPDATE auction_items SET detail_status = 'pending' WHERE detail_collected_at IS NULL"
                    )
                if version < 4:
                    # collected였다가 갱신 재수집 실패로 failed가 된 오염 복구.
                    # 상세 데이터(detail_collected_at)가 있으면 collected가 맞다.
                    conn.execute(
                        """
                        UPDATE auction_items
                           SET detail_status = 'collected'
                         WHERE detail_status = 'failed'
                           AND detail_collected_at IS NOT NULL
                        """
                    )
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _migrate_v2_rekey_and_merge(self, conn: sqlite3.Connection) -> None:
        """구 키(auction:사건번호:물건 / auction:scheduled:...)를 법원 포함 키로 통일하고,
        같은 물건으로 판명된 행(예정/진행 쌍둥이)을 병합한다."""
        rows = conn.execute("SELECT * FROM auction_items").fetchall()
        if not rows:
            return

        groups: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            try:
                values = json.loads(row["raw_json"] or "{}")
            except json.JSONDecodeError:
                values = {}
            new_key = build_item_key(values) if values else row["item_key"]
            groups.setdefault(new_key, []).append(row)

        now = utc_now()
        for new_key, group in groups.items():
            primary = max(group, key=_migration_row_rank)
            merged = dict(primary)
            merged["item_key"] = new_key
            merged["first_seen_at"] = min(row["first_seen_at"] for row in group)
            merged["last_seen_at"] = max(row["last_seen_at"] for row in group)
            if any(row["source"] == "진행" for row in group):
                merged["source"] = "진행"
            if not (merged["lat"] and merged["lng"]):
                located = next((row for row in group if row["lat"] and row["lng"]), None)
                if located is not None:
                    for column in (
                        "lat", "lng", "pnu", "coordinate_source", "coordinate_quality",
                        "normalized_address", "geocode_query", "geocoded_at",
                    ):
                        merged[column] = located[column]

            try:
                values = json.loads(primary["raw_json"] or "{}")
            except json.JSONDecodeError:
                values = {}
            if values:
                merged["content_hash"] = stable_hash(values)
                merged["list_hash"] = stable_list_hash(values)
                merged["is_active"] = 1 if is_active_status(merged["status"] or "") else 0
                merged["next_check_at"] = calculate_next_check_at(
                    merged["status"] or "", merged["sale_date"] or "", changed=False, now=now
                )
                merged["crawl_priority"] = calculate_crawl_priority(merged["status"] or "", merged["sale_date"] or "")

            old_keys = [row["item_key"] for row in group]
            conn.executemany("DELETE FROM auction_items WHERE item_key = ?", [(key,) for key in old_keys])
            columns = list(merged.keys())
            conn.execute(
                f"INSERT INTO auction_items({', '.join(columns)}) VALUES({', '.join('?' for _ in columns)})",
                [merged[column] for column in columns],
            )
            for old_key in old_keys:
                if old_key != new_key:
                    conn.execute(
                        "UPDATE auction_events SET item_key = ? WHERE item_key = ?",
                        (new_key, old_key),
                    )

    def _backfill_schedule(self, conn: sqlite3.Connection) -> None:
        now = utc_now()
        rows = conn.execute(
            """
            SELECT item_key, raw_json, content_hash, list_hash, status, sale_date, updated_at
              FROM auction_items
             WHERE list_hash = content_hash
                OR next_check_at IS NULL
                OR next_check_at = updated_at
            """
        ).fetchall()
        for row in rows:
            try:
                values = json.loads(row["raw_json"] or "{}")
            except json.JSONDecodeError:
                values = {}
            status = row["status"] or first_value(values, STATUS_KEYS)
            sale_date = row["sale_date"] or first_value(values, SALE_DATE_KEYS)
            list_hash = stable_list_hash(values) if values else row["list_hash"]
            active = is_active_status(status)
            conn.execute(
                """
                UPDATE auction_items
                   SET list_hash = ?,
                       is_active = ?,
                       next_check_at = ?,
                       crawl_priority = ?
                 WHERE item_key = ?
                """,
                (
                    list_hash,
                    1 if active else 0,
                    calculate_next_check_at(status, sale_date, changed=False, now=now),
                    calculate_crawl_priority(status, sale_date),
                    row["item_key"],
                ),
            )

    def start_sync(self) -> int:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO sync_runs(started_at, status) VALUES(?, ?)",
                (now, "running"),
            )
            return int(cursor.lastrowid)

    def finish_sync(self, run_id: int, status: str, summary: SyncSummary, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE sync_runs
                   SET finished_at = ?, status = ?, summary_json = ?, error = ?
                 WHERE id = ?
                """,
                (utc_now(), status, json_dumps(asdict(summary)), error, run_id),
            )

    def upsert_items(self, items: list[AuctionItem]) -> UpsertResult:
        result = UpsertResult()
        now = utc_now()
        with self.connect() as conn:
            for item_key, values in aggregate_items_by_key(items):
                raw_json = json_dumps(values)
                content_hash = stable_hash(values)
                list_hash = stable_list_hash(values)
                existing = conn.execute(
                    "SELECT raw_json, detail_json, content_hash, list_hash, sale_date, "
                    "sold_amount, sold_date FROM auction_items WHERE item_key = ?",
                    (item_key,),
                ).fetchone()

                extracted = extract_common_fields(values)

                # 과거 기일 구간 검색은 같은 물건의 지난 회차를 다시 보여준다.
                # 이미 더 새 기일을 아는 행을 과거 목격으로 되돌리지 않는다.
                if existing is not None:
                    incoming_date = sale_date_of(extracted["sale_date"])
                    existing_date = sale_date_of(existing["sale_date"] or "")
                    if incoming_date and existing_date and incoming_date < existing_date:
                        conn.execute(
                            "UPDATE auction_items SET last_seen_at = ? WHERE item_key = ?",
                            (now, item_key),
                        )
                        result.unchanged += 1
                        continue

                changed = existing is None or existing["list_hash"] != list_hash
                active = is_active_status(extracted["status"])
                # 낙찰됐다 되돌아온 물건(대금미납·매각불허)에 옛 낙찰가가 그대로 남아
                # "지금 입찰 가능한데 낙찰 ○○원"이 화면에 떴다. 지우기 전에 이력으로
                # 옮긴다 — 직전 낙찰가는 입찰자에게 시세의 강한 단서다.
                if active and existing is not None and existing["sold_amount"]:
                    retire_sale(conn, item_key, extracted, existing, now)
                next_check_at = calculate_next_check_at(
                    extracted["status"],
                    extracted["sale_date"],
                    changed=changed,
                    now=now,
                )
                crawl_priority = calculate_crawl_priority(extracted["status"], extracted["sale_date"])
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO auction_items(
                            item_key, source, case_no, item_no, court, address, category,
                            appraisal, minimum_bid, sale_date, status, detail_url,
                            raw_json, detail_json, content_hash, list_hash, first_seen_at,
                            last_seen_at, last_changed_at, next_check_at, is_active,
                            crawl_priority, crawl_fail_count, updated_at
                        )
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item_key,
                            extracted["source"],
                            extracted["case_no"],
                            extracted["item_no"],
                            extracted["court"],
                            extracted["address"],
                            extracted["category"],
                            extracted["appraisal"],
                            extracted["minimum_bid"],
                            extracted["sale_date"],
                            extracted["status"],
                            extracted["detail_url"],
                            raw_json,
                            json_dumps(extract_detail_fields(values)),
                            content_hash,
                            list_hash,
                            now,
                            now,
                            now,
                            next_check_at,
                            1 if active else 0,
                            crawl_priority,
                            0,
                            now,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO auction_events(item_key, event_type, new_json, created_at)
                        VALUES(?, 'created', ?, ?)
                        """,
                        (item_key, raw_json, now),
                    )
                    result.inserted += 1
                    continue

                if existing["content_hash"] != content_hash:
                    conn.execute(
                        """
                        UPDATE auction_items
                           SET source = ?, case_no = ?, item_no = ?, court = ?, address = ?,
                               category = ?, appraisal = ?, minimum_bid = ?,
                               sale_date = ?, status = ?, detail_url = ?,
                               raw_json = ?, detail_json = ?, content_hash = ?, list_hash = ?,
                               last_seen_at = ?, last_changed_at = ?, next_check_at = ?,
                               is_active = ?, crawl_priority = ?, crawl_fail_count = 0,
                               updated_at = ?
                         WHERE item_key = ?
                        """,
                        (
                            extracted["source"],
                            extracted["case_no"],
                            extracted["item_no"],
                            extracted["court"],
                            extracted["address"],
                            extracted["category"],
                            extracted["appraisal"],
                            extracted["minimum_bid"],
                            extracted["sale_date"],
                            extracted["status"],
                            extracted["detail_url"],
                            raw_json,
                            merge_detail_json(existing["detail_json"], extract_detail_fields(values)),
                            content_hash,
                            list_hash,
                            now,
                            now,
                            next_check_at,
                            1 if active else 0,
                            crawl_priority,
                            now,
                            item_key,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO auction_events(item_key, event_type, old_json, new_json, created_at)
                        VALUES(?, 'updated', ?, ?, ?)
                        """,
                        (item_key, existing["raw_json"], raw_json, now),
                    )
                    result.updated += 1
                else:
                    conn.execute(
                        """
                        UPDATE auction_items
                           SET last_seen_at = ?, next_check_at = ?, is_active = ?,
                               crawl_priority = ?, crawl_fail_count = 0
                         WHERE item_key = ?
                        """,
                        (now, next_check_at, 1 if active else 0, crawl_priority, item_key),
                    )
                    result.unchanged += 1
        return result

    def list_items(
        self,
        query: str = "",
        status: str = "",
        source: str = "",
        region: str = "",
        sale_date_from: str = "",
        sale_date_to: str = "",
        active: bool | None = None,
        sold_since: str = "",
        require_coordinates: bool = False,
        sw_lat: float | None = None,
        sw_lng: float | None = None,
        ne_lat: float | None = None,
        ne_lng: float | None = None,
        sort: str = "last_seen_desc",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit = min(max(limit, 1), 500)
        offset = max(offset, 0)
        clauses: list[str] = []
        params: list[Any] = []

        if query:
            # raw_json(물건당 수 KB, 총 1.9GB) LIKE는 전건 풀스캔이라 제외한다.
            # 표시·검색에 쓰는 정규화 컬럼만으로 검색한다.
            clauses.append(
                "(case_no LIKE ? OR item_no LIKE ? OR court LIKE ? OR address LIKE ? OR category LIKE ?)"
            )
            like = f"%{query}%"
            params.extend([like, like, like, like, like])
        if status:
            clauses.append("status = ?")
            params.append(status)
        if source:
            clauses.append("source = ?")
            params.append(source)
        if sale_date_from:
            clauses.append("sale_date >= ?")
            params.append(normalize_sale_date_filter(sale_date_from))
        if sale_date_to:
            clauses.append("sale_date <= ?")
            params.append(normalize_sale_date_filter(sale_date_to))
        if active is not None:
            clauses.append("is_active = ?")
            params.append(1 if active else 0)
        if sold_since:
            # 낙찰된 물건은 목록에서 사라져 비활성이 되지만, 얼마에 팔렸는지가
            # 시세 판단에 가장 쓸모 있는 정보다. 최근 낙찰분은 따로 뽑아 쓴다.
            clauses.append("sold_amount IS NOT NULL AND COALESCE(sold_date, '') >= ?")
            params.append(sold_since)
        if require_coordinates:
            clauses.append("lat IS NOT NULL AND lng IS NOT NULL")
        if None not in (sw_lat, sw_lng, ne_lat, ne_lng):
            clauses.append("lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?")
            params.extend(
                [
                    min(float(sw_lat), float(ne_lat)),
                    max(float(sw_lat), float(ne_lat)),
                    min(float(sw_lng), float(ne_lng)),
                    max(float(sw_lng), float(ne_lng)),
                ]
            )
        if region:
            aliases = REGION_ALIASES.get(region, (region,))
            region_clauses: list[str] = []
            for alias in aliases:
                region_clauses.append("(address LIKE ? OR court LIKE ?)")
                like = f"%{alias}%"
                params.extend([like, like])
            clauses.append(f"({' OR '.join(region_clauses)})")

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order_by = list_order_by(sort)
        with self.connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) AS count FROM auction_items {where}", params).fetchone()["count"]
            rows = conn.execute(
                f"""{ITEM_LIST_SELECT}
                  {where}
                 ORDER BY {order_by}
                 LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "sort": sort,
            "items": [dict(row) for row in rows],
        }

    def iter_public_rows(
        self, *, active: bool | None = None, sold_since: str = ""
    ) -> Iterator[dict[str, Any]]:
        """스냅샷용 전량 조회. 페이징 없이 커서로 흘려보낸다.

        list_items로 500개씩 끊어 읽으면 페이지마다 COUNT(*)를 다시 돌아 3만 건에
        13분이 걸렸다(COUNT 한 번이 11초 × 62페이지). 스냅샷은 전체를 한 번에
        쓰므로 페이지 수도 총 건수도 필요 없다."""
        clauses: list[str] = []
        params: list[Any] = []
        if active is not None:
            clauses.append("is_active = ?")
            params.append(1 if active else 0)
        if sold_since:
            clauses.append("sold_amount IS NOT NULL AND COALESCE(sold_date, '') >= ?")
            params.append(sold_since)
        clauses.append("lat IS NOT NULL AND lng IS NOT NULL")
        where = "WHERE " + " AND ".join(f"({clause})" for clause in clauses)
        with self.connect() as conn:
            for row in conn.execute(f"""{ITEM_LIST_SELECT}
                  {where}""", params):
                yield dict(row)

    def list_missing_coordinates(
        self,
        limit: int = 200,
        active: bool | None = True,
        retry_failed_after_days: int = 7,
    ) -> list[dict[str, Any]]:
        clauses = ["lat IS NULL OR lng IS NULL", "coordinate_quality != 'not_applicable'"]
        params: list[Any] = []
        if active is not None:
            clauses.append("is_active = ?")
            params.append(1 if active else 0)
        if retry_failed_after_days is not None:
            # 실패했던 주소를 매 실행 재시도하면 API 쿼터만 태운다. 유예 기간이 지난 뒤 다시 시도.
            cutoff = (
                datetime.now(timezone.utc) - timedelta(days=retry_failed_after_days)
            ).isoformat(timespec="seconds")
            clauses.append("geocoded_at IS NULL OR geocoded_at < ?")
            params.append(cutoff)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT item_key, address, category, coordinate_quality, geocoded_at
                  FROM auction_items
                 WHERE {' AND '.join(f'({clause})' for clause in clauses)}
                 ORDER BY (geocoded_at IS NULL) DESC, crawl_priority DESC, last_seen_at DESC
                 LIMIT ?
                """,
                [*params, min(max(limit, 1), 5000)],
            ).fetchall()
        return [dict(row) for row in rows]

    def list_detail_targets(
        self,
        *,
        limit: int | None = None,
        include_inactive: bool = False,
        force: bool = False,
        item_key: str = "",
        closing_sweep_days: int = 30,
    ) -> list[dict[str, Any]]:
        clauses = ["court != ''", "case_no != ''", "item_no != ''"]
        params: list[Any] = []
        if not include_inactive:
            # 물건이 왜 사라졌는지는 사건 화면의 종국결과(취하·기각·취소)만 말해 준다.
            # 목록 상태는 '신건'과 '유찰 N회'뿐이라 취하는 흔적 없이 사라진다(G03).
            # 사라진 뒤로 한 번도 안 들여다본 물건을 딱 한 번 더 본다 —
            # 보고 나면 detail_checked_at > last_seen_at이 되어 스스로 빠진다.
            # 오래 전에 사라진 것까지 파헤치지 않는다(사건이 종국되면 창이 닫힌다).
            if closing_sweep_days > 0:
                clauses.append(f"is_active = 1 OR ({CLOSING_SWEEP_SQL})")
                params.append(closing_sweep_cutoff(closing_sweep_days))
            else:
                clauses.append("is_active = 1")
        if item_key:
            clauses.append("item_key = ?")
            params.append(item_key)
        if not force:
            clauses.append(
                """
                (
                    (detail_status = 'pending' AND detail_checked_at IS NULL)
                    OR (
                        -- 목록이 갱신된 물건은 상세를 다시 받되, 재수집이 실패해
                        -- 백오프가 걸려 있으면 예약 시각 전까지는 다시 올리지 않는다.
                        -- unavailable은 여기서 빼야 한다. 그 상태는 백오프가 아니라
                        -- next_retry=NULL로 표시되는데, NULL이 '지금 대상'으로 읽혀
                        -- 시도->조회불가->NULL->즉시 대상으로 영원히 돈다(실측 41회).
                        -- 재공고로 다시 볼 필요가 생기면 아래 unavailable 전용
                        -- 조건이 detail_checked_at 기준으로 잡아준다.
                        detail_status != 'unavailable'
                        AND last_changed_at IS NOT NULL
                        AND last_changed_at > detail_collected_at
                        AND (detail_next_retry_at IS NULL OR detail_next_retry_at <= ?)
                    )
                    OR (
                        detail_status IN ('failed', 'metadata_only')
                        AND (detail_next_retry_at IS NULL OR detail_next_retry_at <= ?)
                    )
                    OR (
                        -- 조회 불가였던 물건도 확인 이후에 목록 변경이 잡히면(재공고 등) 다시 시도한다.
                        detail_status = 'unavailable'
                        AND last_changed_at IS NOT NULL
                        AND last_changed_at > detail_checked_at
                    )
                    OR (
                        detail_status != 'unavailable'
                        AND EXISTS (
                            SELECT 1
                              FROM auction_documents AS document
                             WHERE document.item_key = auction_items.item_key
                               AND document.status != 'collected'
                               AND (document.next_retry_at IS NULL OR document.next_retry_at <= ?)
                        )
                    )
                    -- 사라진 물건을 딱 한 번 더 본다. 위에서 문을 열어도 이 조건이
                    -- 막고 있으면 영영 안 들어온다(실측: 큐에 0건이었다).
                    OR ({closing_sweep})
                )
                """.format(closing_sweep=CLOSING_SWEEP_SQL if closing_sweep_days > 0 else "0")
            )
            now = utc_now()
            params.extend([now, now, now])
            if closing_sweep_days > 0:
                params.append(closing_sweep_cutoff(closing_sweep_days))
        row_limit = 1_000_000 if limit is None or limit <= 0 else min(limit, 1_000_000)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT item_key, court, case_no, item_no, sale_date, status,
                       detail_status, detail_collected_at, detail_next_retry_at,
                       detail_fail_count, last_changed_at
                  FROM auction_items
                 WHERE {' AND '.join(f'({clause})' for clause in clauses)}
                 -- **시한이 있는 것이 먼저다.** 사라진 물건은 종국 후 30일이 지나면
                 -- 법원이 기일정보를 안 준다(함정 ⑥) — 그때 놓치면 취하·기각을
                 -- 영영 모른다. 산 물건은 내일도 거기 있다. 만료가 임박한 훑기만
                 -- 앞세우고 나머지는 종전대로 산 물건을 먼저 본다.
                 --
                 -- 2026-09-18 실측: 감정평가서 상태 교정으로 활성 32,875건이 큐에
                 -- 들어오자 훑기 4,194건이 통째로 뒤로 밀려 종국 확인이 0건이었다.
                 ORDER BY (is_active = 0 AND COALESCE(last_seen_at, '') < ?) DESC,
                          is_active DESC,
                          (detail_collected_at IS NULL) DESC,
                          (REPLACE(SUBSTR(sale_date, 1, 10), '.', '-') > ?) DESC,
                          (sale_date IS NULL OR sale_date = '') ASC,
                          sale_date ASC,
                          detail_fail_count ASC,
                          crawl_priority DESC,
                          last_seen_at DESC
                 LIMIT ?
                """,
                # `?` 는 WHERE → ORDER BY → LIMIT 순서로 소비된다. ORDER BY 것을
                # 앞에 두면 WHERE 자리로 흘러 들어가 조용히 다른 비교가 된다.
                # 만료 7일 전부터 급한 것으로 본다(훑기 창 30일 → 23일 이상).
                [*params,
                 closing_sweep_cutoff(max(closing_sweep_days - 7, 1)),
                 date.today().isoformat(), row_limit],
            ).fetchall()
        return [dict(row) for row in rows]

    def save_item_detail(self, item_key: str, detail: dict[str, Any]) -> None:
        now = utc_now()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT detail_json, item_no FROM auction_items WHERE item_key = ?",
                (item_key,),
            ).fetchone()
            if row is None:
                raise KeyError(item_key)
            try:
                merged = json.loads(row["detail_json"] or "{}")
            except (TypeError, ValueError):
                merged = {}
            merged.update(detail)
            case_item = parse_case_item(merged, row["item_no"])
            closing = parse_case_closing(merged)
            conn.execute(
                """
                UPDATE auction_items
                   SET detail_json = ?, detail_status = 'collected',
                       resale_reason = ?, item_status_flow = ?,
                       deposit_amount = ?, deposit_rate = ?, item_note = ?, case_type = ?,
                       closing_result = ?, closing_date = ?, parties_json = ?,
                       detail_collected_at = ?, detail_checked_at = ?,
                       detail_next_retry_at = NULL, detail_fail_count = 0,
                       detail_error = '', updated_at = ?
                 WHERE item_key = ?
                """,
                (
                    json_dumps(merged),
                    case_item["resale_reason"],
                    case_item["status_flow"],
                    case_item["deposit_amount"],
                    case_item["deposit_rate"],
                    case_item["note"],
                    parse_case_type(merged),
                    closing["result"],
                    closing["date"],
                    json_dumps(parse_case_parties(merged)),
                    now,
                    now,
                    now,
                    item_key,
                ),
            )

    def mark_detail_failure(self, item_key: str, error: str) -> None:
        now = datetime.now(timezone.utc)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT detail_fail_count, detail_collected_at, sale_date"
                "  FROM auction_items WHERE item_key = ?",
                (item_key,),
            ).fetchone()
            if row is None:
                return
            fail_count = int(row["detail_fail_count"] or 0) + 1
            retry_hours = min(24 * 7, 2 ** min(fail_count - 1, 7))
            next_retry = detail_retry_at(now, retry_hours, row["sale_date"] or "")
            # 이미 상세를 받아둔 물건(재수집=갱신 시도)이 실패하면 status를 failed로
            # 덮지 않는다. 기존 상세 데이터는 여전히 유효하므로 collected를 유지하고
            # 재시도만 백오프 예약한다. 상세를 한 번도 못 받은 것만 failed로 표시한다.
            keep_collected = row["detail_collected_at"] is not None
            status = "collected" if keep_collected else "failed"
            conn.execute(
                """
                UPDATE auction_items
                   SET detail_status = ?, detail_checked_at = ?,
                       detail_next_retry_at = ?, detail_fail_count = ?,
                       detail_error = ?, updated_at = ?
                 WHERE item_key = ?
                """,
                (
                    status,
                    now.isoformat(timespec="seconds"),
                    next_retry,
                    fail_count,
                    str(error)[:500],
                    now.isoformat(timespec="seconds"),
                    item_key,
                ),
            )

    def mark_detail_unavailable(self, item_key: str, reason: str) -> None:
        """종결·취하 등으로 상세조회 버튼이 비활성인 물건. 재시도해도 소용없으므로
        failed와 달리 재시도 큐에서 제외한다. 물건이 다시 변경되면(재공고 등)
        list_detail_targets가 다시 대상으로 올린다."""
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE auction_items
                   SET detail_status = 'unavailable', detail_checked_at = ?,
                       detail_next_retry_at = NULL, detail_error = ?, updated_at = ?
                 WHERE item_key = ?
                """,
                (now, str(reason)[:500], now, item_key),
            )

    def document_statuses(self, item_key: str) -> dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT document_type, status FROM auction_documents WHERE item_key = ?",
                (item_key,),
            ).fetchall()
        return {row["document_type"]: row["status"] for row in rows}

    def save_document_status(
        self,
        item_key: str,
        document_type: str,
        *,
        status: str,
        title: str = "",
        source_url: str = "",
        file_path: str = "",
        content_type: str = "",
        file_size: int = 0,
        sha256: str = "",
        metadata: dict[str, Any] | None = None,
        next_retry_at: str = "",
        error: str = "",
    ) -> None:
        now = utc_now()
        collected_at = now if status == "collected" else None
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT status, fail_count FROM auction_documents WHERE item_key = ? AND document_type = ?",
                (item_key, document_type),
            ).fetchone()
            if existing is not None and existing["status"] == "collected" and status != "collected":
                return
            fail_count = 0
            if status != "collected" and existing is not None:
                fail_count = int(existing["fail_count"] or 0) + 1
            conn.execute(
                """
                INSERT INTO auction_documents(
                    item_key, document_type, status, title, source_url, file_path,
                    content_type, file_size, sha256, metadata_json, checked_at,
                    collected_at, next_retry_at, fail_count, error
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_key, document_type) DO UPDATE SET
                    status = excluded.status,
                    title = CASE WHEN excluded.title != '' THEN excluded.title ELSE auction_documents.title END,
                    source_url = CASE WHEN excluded.source_url != '' THEN excluded.source_url ELSE auction_documents.source_url END,
                    file_path = CASE WHEN excluded.file_path != '' THEN excluded.file_path ELSE auction_documents.file_path END,
                    content_type = CASE WHEN excluded.content_type != '' THEN excluded.content_type ELSE auction_documents.content_type END,
                    file_size = CASE WHEN excluded.file_size > 0 THEN excluded.file_size ELSE auction_documents.file_size END,
                    sha256 = CASE WHEN excluded.sha256 != '' THEN excluded.sha256 ELSE auction_documents.sha256 END,
                    metadata_json = CASE WHEN excluded.metadata_json != '{}' THEN excluded.metadata_json ELSE auction_documents.metadata_json END,
                    checked_at = excluded.checked_at,
                    collected_at = COALESCE(excluded.collected_at, auction_documents.collected_at),
                    next_retry_at = excluded.next_retry_at,
                    fail_count = excluded.fail_count,
                    error = excluded.error
                """,
                (
                    item_key,
                    document_type,
                    status,
                    title,
                    source_url,
                    file_path,
                    content_type,
                    int(file_size or 0),
                    sha256,
                    json_dumps(metadata or {}),
                    now,
                    collected_at,
                    next_retry_at or None,
                    fail_count,
                    str(error)[:500],
                ),
            )

    def save_asset(
        self,
        item_key: str,
        *,
        kind: str,
        label: str,
        file_path: str,
        content_type: str,
        sha256: str,
        file_size: int,
    ) -> int:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO auction_assets(
                    item_key, kind, label, file_path, content_type, file_size, sha256, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (item_key, kind, label, file_path, content_type, file_size, sha256, utc_now()),
            )
            row = conn.execute(
                """
                SELECT id FROM auction_assets
                 WHERE item_key = ? AND kind = ? AND label = ? AND sha256 = ?
                """,
                (item_key, kind, label, sha256),
            ).fetchone()
        return int(row["id"])

    def get_asset(self, asset_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM auction_assets WHERE id = ?", (asset_id,)).fetchone()
        return dict(row) if row is not None else None

    def get_document(self, document_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM auction_documents WHERE id = ? AND status = 'collected'",
                (document_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_item(self, item_key: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM auction_items WHERE item_key = ?", (item_key,)).fetchone()
            if row is None:
                return None
            events = conn.execute(
                """
                SELECT event_type, old_json, new_json, created_at
                  FROM auction_events
                 WHERE item_key = ?
                 ORDER BY created_at DESC
                 LIMIT 30
                """,
                (item_key,),
            ).fetchall()
            documents = conn.execute(
                """
                SELECT id, document_type, status, title, source_url, content_type,
                       file_size, sha256, checked_at, collected_at, next_retry_at,
                       fail_count, error, metadata_json
                  FROM auction_documents
                 WHERE item_key = ?
                 ORDER BY document_type
                """,
                (item_key,),
            ).fetchall()
            assets = conn.execute(
                """
                SELECT id, kind, label, content_type, file_size, sha256, created_at
                  FROM auction_assets
                 WHERE item_key = ?
                 ORDER BY kind, id
                """,
                (item_key,),
            ).fetchall()
            sale_results = conn.execute(
                """
                SELECT sale_date, result, sale_amount, minimum_bid, appraisal, collected_at
                  FROM auction_sale_results
                 WHERE item_key = ?
                 ORDER BY sale_date DESC
                """,
                (item_key,),
            ).fetchall()
            popularity = conn.execute(
                "SELECT view_count, interest_count, checked_at FROM auction_popularity WHERE item_key = ?",
                (item_key,),
            ).fetchone()

        item = dict(row)
        item["raw"] = json.loads(item.pop("raw_json") or "{}")
        item["detail"] = json.loads(item.pop("detail_json") or "{}")
        item["building"] = json.loads(item.get("building_detail") or "{}")
        item["transactions"] = json.loads(item.get("transactions_detail") or "{}")
        item["land_use"] = json.loads(item.get("land_use_detail") or "{}")
        item["events"] = [dict(event) for event in events]
        item["documents"] = []
        for document in documents:
            document_payload = dict(document)
            document_payload["metadata"] = json.loads(document_payload.pop("metadata_json") or "{}")
            item["documents"].append(document_payload)
        item["assets"] = [dict(asset) for asset in assets]
        item["sale_results"] = [dict(result) for result in sale_results]
        item["popularity"] = dict(popularity) if popularity else {}
        return item

    def update_coordinates(
        self,
        item_key: str,
        *,
        lat: float,
        lng: float,
        pnu: str = "",
        coordinate_source: str = "address",
        coordinate_quality: str = "verified",
        normalized_address: str = "",
        geocode_query: str = "",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE auction_items
                   SET lat = ?,
                       lng = ?,
                       pnu = ?,
                       coordinate_source = ?,
                       coordinate_quality = ?,
                       normalized_address = ?,
                       geocode_query = ?,
                       geocoded_at = ?,
                       updated_at = ?
                 WHERE item_key = ?
                """,
                (
                    lat,
                    lng,
                    pnu,
                    coordinate_source,
                    coordinate_quality,
                    normalized_address,
                    geocode_query,
                    utc_now(),
                    utc_now(),
                    item_key,
                ),
            )

    def update_official_price(
        self,
        item_key: str,
        *,
        value: float | None,
        price_type: str = "",
        year: str = "",
        detail: dict[str, Any] | None = None,
        status: str,
    ) -> None:
        """공시기준가 조회 결과를 저장한다. 조회 실패/대상외는 value=None, status로만 기록한다."""
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE auction_items
                   SET official_price = ?,
                       official_price_type = ?,
                       official_price_year = ?,
                       official_price_detail = ?,
                       official_price_status = ?,
                       official_price_at = ?,
                       updated_at = ?
                 WHERE item_key = ?
                """,
                (
                    value,
                    price_type,
                    year,
                    json.dumps(detail or {}, ensure_ascii=False),
                    status,
                    utc_now(),
                    utc_now(),
                    item_key,
                ),
            )

    def list_missing_official_price(
        self,
        *,
        limit: int = 200,
        active: bool | None = True,
        retry_failed_after_days: int = 14,
    ) -> list[dict[str, Any]]:
        """PNU는 있으나 공시기준가를 아직 못 채운(또는 재시도 대상) 물건을 고른다."""
        clauses = ["pnu != ''"]
        params: list[Any] = []
        if active is not None:
            clauses.append("is_active = ?")
            params.append(1 if active else 0)
        # 미시도이거나, 실패한 지 오래된 것만 재시도
        clauses.append(
            "(official_price_status = ''"
            " OR (official_price_status IN ('geocode_miss','price_miss','error')"
            "     AND (official_price_at IS NULL OR official_price_at <= ?)))"
        )
        params.append(
            (datetime.now(timezone.utc) - timedelta(days=retry_failed_after_days)).isoformat()
        )
        where = " AND ".join(clauses)
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT item_key, address, category, pnu, official_price_status
                  FROM auction_items
                 WHERE {where}
                 ORDER BY crawl_priority DESC, sale_date ASC
                 LIMIT ?
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def update_building(
        self, item_key: str, *, detail: dict[str, Any] | None, status: str, pnu: str = ""
    ) -> int:
        """건축물대장 조회 결과를 저장한다(없으면 detail=None, status로만). 적은 물건 수를 돌려준다.

        대장은 필지 단위라 같은 PNU의 물건은 답이 똑같다. pnu를 주면 그 필지의 활성
        물건 전부에 한 번에 적는다 — 일괄매각 아파트는 한 필지에 물건이 106개까지
        붙어 있어, 물건마다 물어보면 같은 답을 106번 받아온다(실측: 확보한 21,646건이
        실제로는 13,350필지)."""
        # 이미 채운 물건은 건드리지 않는다. 덮어써 봐야 값은 같은데 updated_at만 밀려
        # 한 필지에 물건이 106개면 106건이 통째로 R2 재업로드 후보가 된다.
        # 활성 여부는 보지 않는다. 물어본 필지의 답은 그 필지 물건 전부에 적어야
        # 다음 실행에서 다시 묻지 않는다(낙찰된 물건도 같은 건물이라 답이 같다).
        target, values = (
            ("pnu = ? AND building_status != 'ok'", (pnu,))
            if pnu
            else ("item_key = ?", (item_key,))
        )
        with self.connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE auction_items
                   SET building_detail = ?, building_status = ?, building_at = ?, updated_at = ?
                 WHERE {target}
                """,
                (json.dumps(detail or {}, ensure_ascii=False), status, utc_now(), utc_now(), *values),
            )
            return cursor.rowcount

    def update_transactions(
        self, item_key: str, *, detail: dict[str, Any] | None, status: str
    ) -> None:
        """국토부 실거래가 조회 결과를 저장한다."""
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE auction_items
                   SET transactions_detail = ?, transactions_status = ?, transactions_at = ?, updated_at = ?
                 WHERE item_key = ?
                """,
                (json.dumps(detail or {}, ensure_ascii=False), status, utc_now(), utc_now(), item_key),
            )

    def update_land_use(
        self, item_key: str, *, detail: dict[str, Any] | None, status: str
    ) -> None:
        """토지이용계획 조회 결과를 저장한다."""
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE auction_items
                   SET land_use_detail = ?, land_use_status = ?, land_use_at = ?, updated_at = ?
                 WHERE item_key = ?
                """,
                (json.dumps(detail or {}, ensure_ascii=False), status, utc_now(), utc_now(), item_key),
            )

    def upsert_notices(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        """배당요구종기공고 행을 쌓는다. 이미 본 사건은 last_seen_at 만 민다.

        **공고는 사라진다.** 배당요구종기일이 지나면 목록에서 빠지는 것으로 보인다.
        그래서 지운 적이 없어도 '한 번 본 것'을 남겨 두는 것 자체가 값이다.
        """
        now = utc_now()
        새로 = 갱신 = 0
        with self.connect() as conn:
            for row in rows:
                court, case_no = str(row.get("court") or ""), str(row.get("case_no") or "")
                if not court or not case_no:
                    continue
                cur = conn.execute(
                    """
                    UPDATE auction_notices
                       SET address = ?, owner = ?, debtor = ?, notice_date = ?, dept = ?,
                           opened_at = ?, dividend_deadline = ?, last_seen_at = ?
                     WHERE court = ? AND case_no = ?
                    """,
                    (row.get("address", ""), row.get("owner", ""), row.get("debtor", ""),
                     row.get("notice_date", ""), row.get("dept", ""), row.get("opened_at", ""),
                     row.get("dividend_deadline", ""), now, court, case_no),
                )
                if cur.rowcount:
                    갱신 += 1
                    continue
                conn.execute(
                    """
                    INSERT INTO auction_notices
                        (court, case_no, address, owner, debtor, notice_date, dept,
                         opened_at, dividend_deadline, first_seen_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (court, case_no, row.get("address", ""), row.get("owner", ""),
                     row.get("debtor", ""), row.get("notice_date", ""), row.get("dept", ""),
                     row.get("opened_at", ""), row.get("dividend_deadline", ""), now, now),
                )
                새로 += 1
        return {"inserted": 새로, "updated": 갱신}

    def count_notices(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM auction_notices").fetchone()[0])

    def court_reduction_rates(self, min_samples: int = 200) -> dict[str, int]:
        """법원별 **실측** 유찰 체감률(%). 표본이 적으면 빼고 전국 최빈으로 받는다.

        일률적으로 30%를 깎으면 틀린다. 실측 44,524쌍에서 70%가 75%·80%가 24%인데,
        **법원마다 갈린다** — 인천·수원·부산은 70%, 서울남부·광주는 80%다.
        24%를 틀리게 보여주면서 입찰가 판단에 쓰게 할 수는 없다.
        """
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT court, sale_date, minimum_bid, item_key
                  FROM auction_sale_results
                 WHERE minimum_bid > 0 AND sale_date != ''
                 ORDER BY item_key, sale_date
                """
            ).fetchall()
        from collections import Counter, defaultdict  # noqa: PLC0415

        직전: dict[str, tuple[str, int]] = {}
        표: defaultdict[str, Counter] = defaultdict(Counter)
        for row in rows:
            key = row["item_key"]
            앞 = 직전.get(key)
            직전[key] = (row["court"], row["minimum_bid"])
            if not 앞 or 앞[1] <= 0 or row["minimum_bid"] <= 0:
                continue
            if row["minimum_bid"] >= 앞[1]:
                continue
            표[앞[0]][round(row["minimum_bid"] / 앞[1] * 100)] += 1
        out = {court: cnt.most_common(1)[0][0]
               for court, cnt in 표.items() if sum(cnt.values()) >= min_samples}
        return out

    def notice_funnel(self) -> dict[str, Any]:
        """공고로 먼저 안 사건이 실제 물건으로 넘어오는 흐름.

        **이 숫자가 G15 의 값어치다.** 공고는 기일이 잡히기 몇 달 전에 뜨고,
        기일이 잡히면 기존 매각예정 파이프라인이 알아서 받아 간다. 그 사이의
        시간이 우리가 남보다 먼저 아는 만큼이다.
        """
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN EXISTS (
                             SELECT 1 FROM auction_items i WHERE i.case_no = n.case_no)
                           THEN 1 ELSE 0 END) AS arrived
                  FROM auction_notices AS n
                """
            ).fetchone()
            앞선날 = conn.execute(
                """
                SELECT AVG(julianday(replace(i.sale_date, '.', '-'))
                           - julianday(replace(n.opened_at, '.', '-'))) AS days
                  FROM auction_notices AS n
                  JOIN auction_items AS i ON i.case_no = n.case_no
                 WHERE n.opened_at != '' AND i.sale_date != ''
                """
            ).fetchone()
        total = int(row["total"] or 0)
        arrived = int(row["arrived"] or 0)
        return {
            "total": total,
            "arrived": arrived,
            "waiting": total - arrived,
            "opened_to_sale_days": round(앞선날["days"], 1) if 앞선날 and 앞선날["days"] else None,
        }

    def notices_not_in_items(self, limit: int = 0) -> list[dict[str, Any]]:
        """공고에는 있는데 물건 목록에는 아직 없는 사건. G15 가 재는 격차 그 자체다."""
        sql = """
            SELECT n.court, n.case_no, n.opened_at, n.dividend_deadline, n.address
              FROM auction_notices AS n
             WHERE NOT EXISTS (
                   SELECT 1 FROM auction_items AS i WHERE i.case_no = n.case_no)
             ORDER BY n.opened_at DESC
        """
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql)]

    def list_missing_enrichment(
        self,
        field: str,
        *,
        limit: int = 200,
        active: bool | None = True,
        retry_failed_after_days: int = 14,
    ) -> list[dict[str, Any]]:
        """PNU는 있으나 부가정보(building/transactions)를 아직 못 채운(또는 재시도 대상) 물건.
        field는 'building' 또는 'transactions'."""
        if field not in ("building", "transactions", "land_use"):
            raise ValueError(f"unknown enrichment field: {field}")
        status_col, at_col = f"{field}_status", f"{field}_at"
        clauses = ["pnu != ''"]
        params: list[Any] = []
        if active is not None:
            clauses.append("is_active = ?")
            params.append(1 if active else 0)
        clauses.append(
            f"({status_col} = ''"
            f" OR ({status_col} IN ('miss','error','unregistered')"
            f"     AND ({at_col} IS NULL OR {at_col} <= ?)))"
        )
        now = datetime.now(timezone.utc)
        params.append((now - timedelta(days=retry_failed_after_days)).isoformat())
        if field == "building":
            # 밭과 산에는 건축물대장이 있을 수 없다. 실측 성공률이 전답 1.7%·임야 4.8%인데
            # miss 7,159건을 30일마다 되물으면 하루 한도만 태운다. 첫 조회(status='')는
            # 그대로 하고, 없다고 확인된 뒤의 재시도만 늦춘다. 0%는 아니라서 막지는 않는다.
            placeholders = ",".join("?" * len(LAND_ONLY_CATEGORIES))
            clauses.append(
                f"(category NOT IN ({placeholders})"
                f" OR {status_col} = '' OR {at_col} IS NULL OR {at_col} <= ?)"
            )
            params.extend(LAND_ONLY_CATEGORIES)
            params.append((now - timedelta(days=LAND_ONLY_RETRY_DAYS)).isoformat())
        where = " AND ".join(clauses)
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT item_key, address, category, pnu, normalized_address
                  FROM auction_items
                 WHERE {where}
                 ORDER BY crawl_priority DESC, sale_date ASC
                 LIMIT ?
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def mark_coordinate_missing(
        self,
        item_key: str,
        *,
        normalized_address: str = "",
        geocode_query: str = "",
        quality: str = "missing",
        clear_point: bool = False,
    ) -> None:
        """좌표를 못 얻었다고 기록한다.

        기본값은 점을 건드리지 않는다 — 원래 호출자(좌표가 처음부터 없는 물건)에는
        지울 점이 없다. **이미 박힌 좌표가 틀린 것으로 밝혀진 경우**에는 clear_point로
        점까지 지워야 한다. 안 그러면 quality만 바뀌고 틀린 핀이 지도에 그대로 남는다.
        """
        cleared = ", lat = NULL, lng = NULL, pnu = ''" if clear_point else ""
        with self.connect() as conn:
            conn.execute(
                f"""
                UPDATE auction_items
                   SET coordinate_quality = ?{cleared},
                       normalized_address = ?,
                       geocode_query = ?,
                       geocoded_at = ?,
                       updated_at = ?
                 WHERE item_key = ?
                """,
                (
                    quality,
                    normalized_address,
                    geocode_query,
                    utc_now(),
                    utc_now(),
                    item_key,
                ),
            )

    def healthcheck(self) -> None:
        """DB를 실제로 열어 가벼운 쿼리를 던진다. 좀비(핸들 깨짐) 상태면 여기서
        sqlite3.OperationalError가 난다. 워치독이 이걸로 서버 상태를 판정한다."""
        with self.connect() as conn:
            conn.execute("SELECT 1").fetchone()

    # 웹 푸시가 마지막으로 올린 뒤에 물건이 달라졌는지 판정할 때 보는 시각들.
    # content_hash는 목록 필드만 덮어서(상세·좌표·건축물대장·실거래가는 빠진다)
    # 이것만 믿으면 상세가 채워져도 푸시가 안 일어난다.
    _PUSH_FRESHNESS_SQL = (
        "MAX(COALESCE(i.updated_at,''), COALESCE(i.detail_collected_at,''), "
        "COALESCE(i.geocoded_at,''), COALESCE(i.official_price_at,''), "
        "COALESCE(i.building_at,''), COALESCE(i.transactions_at,''))"
    )

    @staticmethod
    def _push_limit(limit: int) -> int:
        """0 이하는 '전부'로 읽는다. list_detail_targets와 같은 약속인데 여기만 달라서
        --item-limit 0이 LIMIT 0으로 나가 한 건도 안 올라갔다."""
        return 1_000_000 if limit is None or limit <= 0 else min(limit, 1_000_000)

    def pending_item_pushes(self, *, limit: int = 500, include_inactive: bool = True) -> list[dict[str, Any]]:
        """웹에 올릴 후보 물건. 한 번도 안 올렸거나, 올린 뒤 무언가 갱신된 것만 고른다.

        여기서는 시각만 보고 싼값에 후보를 좁힌다. 실제 변경 여부는 페이로드를 만들어
        해시를 비교해 확정한다(상세 763MB를 매번 다 읽지 않기 위한 2단 판정)."""
        # <= 인 이유: 시각이 초 단위라 푸시와 갱신이 같은 초에 걸리면 변경을 놓친다.
        # 같은 초를 다시 후보로 잡아도 페이로드 해시 비교에서 걸러지므로 손해가 없다.
        clauses = ["(w.ref IS NULL OR w.pushed_at <= " + self._PUSH_FRESHNESS_SQL + ")"]
        if not include_inactive:
            clauses.append("i.is_active = 1")
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                -- i.* 로 받으면 detail_json까지 딸려와 전량 모드에서 수백 MB를 메모리에
                -- 올린다(실측: 4.6만 행에 RSS가 계속 불어나 진행이 멈췄다). 페이로드는
                -- 어차피 get_item으로 다시 읽으므로 여기서는 판정에 쓰는 두 컬럼만 본다.
                SELECT i.item_key, COALESCE(w.hash, '') AS pushed_hash
                  FROM auction_items i
                  LEFT JOIN web_sync w ON w.kind = 'item' AND w.ref = i.item_key
                 WHERE {' AND '.join(clauses)}
                 ORDER BY i.is_active DESC, i.updated_at DESC
                 LIMIT ?
                """,
                (self._push_limit(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_asset_pushes(self, *, limit: int = 500, include_inactive: bool = True) -> list[dict[str, Any]]:
        """웹에 올릴 후보 사진. sha256이 파일 내용 그대로라 시각을 볼 필요 없이 정확하다."""
        clauses = ["(w.ref IS NULL OR w.hash <> a.sha256)"]
        if not include_inactive:
            clauses.append("i.is_active = 1")
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT a.id, a.item_key, a.file_path, a.content_type, a.sha256
                  FROM auction_assets a
                  JOIN auction_items i ON i.item_key = a.item_key
                  LEFT JOIN web_sync w ON w.kind = 'asset' AND w.ref = CAST(a.id AS TEXT)
                 WHERE {' AND '.join(clauses)}
                 ORDER BY i.is_active DESC, a.id
                 LIMIT ?
                """,
                (self._push_limit(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_pushed_many(self, records: list[tuple[str, str, str, str, int]]) -> None:
        """푸시 기록을 한 트랜잭션에 모아 쓴다.

        객체마다 쓰기 트랜잭션을 열면 상세수집기와 락을 다투느라 사진 50장에 수 분이
        걸린다(실측). 초기 전량이 20만 객체라 여기서 갈린다."""
        if not records:
            return
        now = utc_now()
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO web_sync (kind, ref, hash, remote_key, size, pushed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(kind, ref) DO UPDATE SET
                    hash = excluded.hash,
                    remote_key = excluded.remote_key,
                    size = excluded.size,
                    pushed_at = excluded.pushed_at
                """,
                [(*record, now) for record in records],
            )

    def mark_pushed(self, kind: str, ref: str, *, hash_value: str, remote_key: str, size: int) -> None:
        self.mark_pushed_many([(kind, ref, hash_value, remote_key, size)])

    def web_sync_stats(self) -> dict[str, Any]:
        with self.connect() as conn:
            pushed = {
                row["kind"]: {"count": row["count"], "bytes": row["bytes"] or 0}
                for row in conn.execute(
                    "SELECT kind, COUNT(*) AS count, SUM(size) AS bytes FROM web_sync GROUP BY kind"
                ).fetchall()
            }
            items_total = conn.execute("SELECT COUNT(*) AS c FROM auction_items").fetchone()["c"]
            assets_total = conn.execute("SELECT COUNT(*) AS c FROM auction_assets").fetchone()["c"]
            last = conn.execute("SELECT MAX(pushed_at) AS at FROM web_sync").fetchone()["at"]
        return {
            "pushed": pushed,
            "items_total": items_total,
            "assets_total": assets_total,
            "last_pushed_at": last,
        }

    def integrity_check(self) -> list[str]:
        """DB 손상을 점검하고 문제 목록을 돌려준다(정상이면 빈 리스트).

        quick_check가 아니라 integrity_check를 쓴다. 실측: 인덱스 항목이 빠진
        손상을 quick_check는 ok로 통과시켰고, 그 상태로 수집기가
        'database disk image is malformed'로 죽었다. 1.9G DB에서 1~2초라 아끼는
        의미가 없다."""
        with self.connect() as conn:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
        problems = [str(row[0]) for row in rows]
        if problems == ["ok"]:
            return []
        return problems

    def repair_indexes(self, index_names: Iterator[str] | list[str] | None = None) -> None:
        """인덱스를 테이블 데이터로부터 재생성한다. 이름을 안 주면 DB 전체를 다시 만든다.
        테이블 본문은 건드리지 않으므로 인덱스 한정 손상에서는 데이터 손실이 없다."""
        names = list(index_names or [])
        with self.connect() as conn:
            if not names:
                conn.execute("REINDEX")
                return
            for name in names:
                # 인덱스 이름은 integrity_check 출력에서만 오고, 실재하는 것만 재생성한다.
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)
                ).fetchone()
                if exists:
                    conn.execute(f'REINDEX "{name}"')

    def stats(self, include_detail_breakdown: bool = False) -> dict[str, Any]:
        # 대시보드가 5초마다 폴링하는 경로다. auction_documents GROUP BY(수만 건,
        # 상세수집기 동시 쓰기 경합)는 42초까지 걸려 서버 전체를 마비시키므로
        # 기본 통계에서 제외하고, 필요할 때만 include_detail_breakdown으로 계산한다.
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS count FROM auction_items").fetchone()["count"]
            latest = conn.execute(
                """
                SELECT id, started_at, finished_at, status, summary_json, error
                  FROM sync_runs
                 ORDER BY id DESC
                 LIMIT 1
                """
            ).fetchone()
            by_status = conn.execute(
                """
                SELECT COALESCE(NULLIF(status, ''), '미분류') AS name, COUNT(*) AS count
                  FROM auction_items
                 GROUP BY COALESCE(NULLIF(status, ''), '미분류')
                 ORDER BY count DESC
                 LIMIT 8
                """
            ).fetchall()
            by_source = conn.execute(
                """
                SELECT COALESCE(NULLIF(source, ''), '진행') AS name, COUNT(*) AS count
                  FROM auction_items
                 GROUP BY COALESCE(NULLIF(source, ''), '진행')
                 ORDER BY name
                """
            ).fetchall()
            active = conn.execute(
                "SELECT COUNT(*) AS count FROM auction_items WHERE is_active = 1"
            ).fetchone()["count"]
            due = conn.execute(
                """
                SELECT COUNT(*) AS count
                  FROM auction_items
                 WHERE is_active = 1
                   AND next_check_at <= ?
                """,
                (utc_now(),),
            ).fetchone()["count"]
            detail_breakdown: dict[str, Any] = {}
            if include_detail_breakdown:
                by_detail_status = conn.execute(
                    """
                    SELECT COALESCE(NULLIF(detail_status, ''), 'pending') AS name, COUNT(*) AS count
                      FROM auction_items
                     GROUP BY COALESCE(NULLIF(detail_status, ''), 'pending')
                     ORDER BY count DESC
                    """
                ).fetchall()
                by_document_status = conn.execute(
                    """
                    SELECT status AS name, COUNT(*) AS count
                      FROM auction_documents
                     GROUP BY status
                     ORDER BY count DESC
                    """
                ).fetchall()
                asset_count = conn.execute("SELECT COUNT(*) AS count FROM auction_assets").fetchone()["count"]
                detail_breakdown = {
                    "by_detail_status": [dict(row) for row in by_detail_status],
                    "by_document_status": [dict(row) for row in by_document_status],
                    "asset_count": asset_count,
                }

        latest_dict = dict(latest) if latest else None
        if latest_dict:
            latest_dict["summary"] = json.loads(latest_dict.pop("summary_json") or "{}")
        return {
            "total": total,
            "latest_sync": latest_dict,
            "by_status": [dict(row) for row in by_status],
            "by_source": [dict(row) for row in by_source],
            "active": active,
            "due": due,
            **detail_breakdown,
        }

    def apply_lifecycle(
        self,
        *,
        past_grace_days: int = 3,
        unseen_no_date_days: int = 21,
        unseen_future_days: int = 7,
        now: str | None = None,
    ) -> dict[str, int]:
        """낙찰·취하된 물건은 상태 변경 없이 검색결과에서 사라지므로 상태 텍스트로는
        종결을 알 수 없다. 매각기일이 유예기간 이상 지났는데 새 기일이 잡히지 않은
        물건을 비활성 처리한다. 유찰 후 새 기일이 잡히면 upsert가 다시 활성화한다."""
        now_text = now or utc_now()
        now_dt = parse_iso_datetime(now_text)
        # sale_date는 'YYYY.MM.DD' 텍스트라 '.'→'-'로 바꾸면 ISO 문자열 비교=날짜 비교가 된다.
        grace_cutoff = (now_dt.date() - timedelta(days=past_grace_days)).isoformat()
        unseen_cutoff = (now_dt - timedelta(days=unseen_no_date_days)).isoformat(timespec="seconds")
        # 취하·기일변경된 물건은 기일이 오기도 전에 목록에서 사라진다. 기일이 지나야
        # 정리하는 규칙만으로는 없는 물건을 최대 2~3주 들고 있게 된다(실측: 수원 08.25
        # 미목격 59건이 법원 사이트에 전부 없었다). 수집이 하루 4~5회 도니 7일이면
        # 30회 가까이 연속 미목격이라 일시적 수집 실패와 헷갈릴 여지가 없다.
        unseen_future_cutoff = (now_dt - timedelta(days=unseen_future_days)).isoformat(timespec="seconds")
        next_check = (now_dt + timedelta(days=7)).isoformat(timespec="seconds")
        # 기일이 유예기간 넘게 지났거나, 기일이 없거나 아직 남았는데 오래 미목격이면 종결.
        expired_clause = (
            "is_active = 1 AND ("
            "  (COALESCE(sale_date, '') != '' "
            "   AND REPLACE(SUBSTR(sale_date, 1, 10), '.', '-') < ?)"
            "  OR (COALESCE(sale_date, '') = '' AND COALESCE(last_seen_at, '') < ?)"
            "  OR (COALESCE(sale_date, '') != '' AND COALESCE(last_seen_at, '') < ?)"
            ")"
        )
        with self.connect() as conn:
            checked = conn.execute(
                "SELECT COUNT(*) AS c FROM auction_items WHERE is_active = 1"
            ).fetchone()["c"]
            # 이벤트를 먼저 일괄 기록한 뒤(같은 조건) 일괄 UPDATE. 3.6만 건 파이썬
            # 루프+개별 UPDATE 대신 2개 SQL로 락 점유 시간을 크게 줄인다.
            conn.execute(
                f"""
                INSERT INTO auction_events(item_key, event_type, new_json, created_at)
                SELECT item_key, 'deactivated', raw_json, ?
                  FROM auction_items
                 WHERE {expired_clause}
                """,
                (now_text, grace_cutoff, unseen_cutoff, unseen_future_cutoff),
            )
            cursor = conn.execute(
                f"""
                UPDATE auction_items
                   SET is_active = 0, crawl_priority = -100, next_check_at = ?
                 WHERE {expired_clause}
                """,
                (next_check, grace_cutoff, unseen_cutoff, unseen_future_cutoff),
            )
            deactivated = cursor.rowcount
        return {"checked": checked, "deactivated": deactivated}

    def record_sale_results(self, rows: list[dict[str, str]]) -> dict[str, int]:
        """매각결과 화면에서 받은 행을 저장한다.

        사이트가 직전 기일들의 결과만 짧게 보여주므로 같은 기일을 여러 번 받게 된다.
        (법원, 사건, 물건, 기일)로 덮어써서 중복을 막는다."""
        now = utc_now()
        prepared: list[tuple[Any, ...]] = []
        for values in rows:
            common = extract_common_fields(values)
            case_no = representative_case_no(common["case_no"])
            sale_date = common["sale_date"]
            if not (case_no and common["item_no"] and sale_date):
                continue
            result, amount = parse_sale_result(values.get("매각결과", ""))
            prepared.append(
                (
                    common["court"],
                    case_no,
                    common["item_no"],
                    sale_date,
                    result,
                    amount,
                    parse_money(common["minimum_bid"]),
                    parse_money(common["appraisal"]),
                    build_item_key(values),
                    json_dumps(values),
                    now,
                )
            )
        if not prepared:
            return {"received": len(rows), "saved": 0, "sold": 0}
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO auction_sale_results(
                    court, case_no, item_no, sale_date, result,
                    sale_amount, minimum_bid, appraisal, item_key, raw_json, collected_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(court, case_no, item_no, sale_date) DO UPDATE SET
                    result = excluded.result,
                    sale_amount = excluded.sale_amount,
                    minimum_bid = excluded.minimum_bid,
                    appraisal = excluded.appraisal,
                    item_key = excluded.item_key,
                    raw_json = excluded.raw_json,
                    collected_at = excluded.collected_at
                """,
                prepared,
            )
            # 낙찰은 목록에서 조용히 사라지는 것으로만 드러나 status가 '유찰 N회'에
            # 머문다. 낙찰가를 받은 김에 물건 자체에도 새겨 둬야 목록에서 바로 보인다.
            conn.execute(
                """
                UPDATE auction_items
                   SET sold_amount = (
                           SELECT r.sale_amount FROM auction_sale_results r
                            WHERE r.item_key = auction_items.item_key
                              AND r.sale_amount IS NOT NULL
                            ORDER BY r.sale_date DESC LIMIT 1),
                       sold_date = COALESCE((
                           SELECT r.sale_date FROM auction_sale_results r
                            WHERE r.item_key = auction_items.item_key
                              AND r.sale_amount IS NOT NULL
                            ORDER BY r.sale_date DESC LIMIT 1), ''),
                       -- 낙찰이 확인되면 그 자리에서 진행 목록에서 내린다. 기일이
                       -- 지나기를 기다리면 이미 팔린 물건이 입찰 가능한 것처럼 남는다.
                       -- 대금미납·매각불허가로 재매각되면 목록에 다시 나타나 되살아난다.
                       is_active = 0,
                       crawl_priority = -100,
                       updated_at = ?
                 WHERE item_key IN (
                       SELECT r.item_key FROM auction_sale_results r
                        WHERE r.sale_amount IS NOT NULL AND r.collected_at = ?)
                """,
                (now, now),
            )
        return {
            "received": len(rows),
            "saved": len(prepared),
            "sold": sum(1 for row in prepared if row[5] is not None),
        }

    def backfill_sale_results(self, rows: list[dict[str, str]]) -> dict[str, int]:
        """사건 화면 기일내역에서 뽑은 결과를 '없는 것만' 채운다.

        매각결과 화면은 기일 다음날부터 이레만 보여주지만 사건 화면의 기일내역은
        금액까지 영구히 남는다. 그 사이에 놓친 기일을 뒤늦게 메우는 길이다.

        이미 있는 행은 절대 건드리지 않는다. 기일내역은 '매각'인데 금액이 빠져
        있을 때가 있어서(실측 15%), 덮어쓰면 결과 화면에서 받아둔 낙찰가를 잃는다."""
        now = utc_now()
        prepared: list[tuple[Any, ...]] = []
        for values in rows:
            common = extract_common_fields(values)
            case_no = representative_case_no(common["case_no"])
            sale_date = common["sale_date"]
            if not (case_no and common["item_no"] and sale_date):
                continue
            result, amount = parse_sale_result(values.get("매각결과", ""))
            if not result:
                continue
            prepared.append(
                (
                    common["court"],
                    case_no,
                    common["item_no"],
                    sale_date,
                    result,
                    amount,
                    parse_money(common["minimum_bid"]),
                    parse_money(common["appraisal"]),
                    build_item_key(values),
                    json_dumps(values),
                    now,
                )
            )
        if not prepared:
            return {"received": len(rows), "inserted": 0, "sold": 0}
        with self.connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT INTO auction_sale_results(
                    court, case_no, item_no, sale_date, result,
                    sale_amount, minimum_bid, appraisal, item_key, raw_json, collected_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(court, case_no, item_no, sale_date) DO NOTHING
                """,
                prepared,
            )
            inserted = conn.total_changes - before
            sold = 0
            for item_key in {row[8] for row in prepared}:
                latest = conn.execute(
                    """
                    SELECT sale_date, sale_amount FROM auction_sale_results
                     WHERE item_key = ? ORDER BY sale_date DESC LIMIT 1
                    """,
                    (item_key,),
                ).fetchone()
                if latest is None or latest["sale_amount"] is None:
                    continue
                item = conn.execute(
                    "SELECT sale_date, sold_amount FROM auction_items WHERE item_key = ?",
                    (item_key,),
                ).fetchone()
                if item is None or item["sold_amount"] is not None:
                    continue
                # 대금미납 재매각처럼 새 기일을 받아 되살아난 물건은 아직 팔린 게
                # 아니다. 옛 낙찰로 목록에서 내리면 입찰 가능한 물건이 사라진다.
                if (item["sale_date"] or "") > latest["sale_date"]:
                    continue
                conn.execute(
                    """
                    UPDATE auction_items
                       SET sold_amount = ?, sold_date = ?, is_active = 0,
                           crawl_priority = -100, updated_at = ?
                     WHERE item_key = ?
                    """,
                    (latest["sale_amount"], latest["sale_date"], now, item_key),
                )
                sold += 1
        return {"received": len(rows), "inserted": inserted, "sold": sold}

    # 매각결과 화면은 기일 다음날부터 이레만 보여준다. 그 창이 열려 있는 동안은
    # 정상 경로(수집 사이클)가 가져가므로 사건 화면까지 들출 이유가 없다.
    RESULT_WINDOW_DAYS = 8

    def mark_result_checked(self, item_keys: list[str]) -> None:
        """매각결과 보충이 이 물건의 사건 화면을 훑었다고 적어 둔다.

        결과를 못 채웠어도 적는다 — 법원이 그 기일 결과를 아예 안 올리는 경우가
        많아서, 못 채운 것을 다시 맨 앞에 두면 나머지를 영영 못 본다. 다음 차례는
        돌아오되 한 바퀴 뒤다."""
        if not item_keys:
            return
        now = utc_now()
        with self.connect() as conn:
            conn.executemany(
                "UPDATE auction_items SET result_checked_at = ? WHERE item_key = ?",
                [(now, key) for key in item_keys],
            )

    def list_missing_result_targets(self, limit: int | None = None) -> list[dict[str, Any]]:
        """기일은 지났는데 그 기일의 결과행이 없는 물건. 사건 화면으로 메울 대상이다.

        결과 화면의 이레 창이 아직 안 닫힌 기일은 뺀다 — 곧 정상 경로가 채운다.
        활성·비활성을 가리지 않는다. 놓친 낙찰은 이미 비활성이 되어 있고, 상세
        수집 대기열은 활성만 보기 때문에 영원히 다시 들르지 않는다."""
        row_limit = 1_000_000 if limit is None or limit <= 0 else min(limit, 1_000_000)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT item_key, court, case_no, item_no, sale_date, status,
                       detail_status, detail_collected_at, detail_next_retry_at,
                       detail_fail_count, last_changed_at
                  FROM auction_items AS item
                 WHERE court != '' AND case_no != '' AND item_no != ''
                   AND sale_date != '' AND REPLACE(sale_date, '.', '-') < ?
                   AND NOT EXISTS (
                       SELECT 1 FROM auction_sale_results AS result
                        WHERE result.item_key = item.item_key
                          AND result.sale_date = item.sale_date)
                 -- 한 번도 안 본 것부터. 그 기일에 결과가 끝내 안 올라오는 물건이
                 -- 많아서, 안 비켜주면 대기열 맨 앞이 고정된다.
                 -- 그 다음은 오래된 기일 순. 법원이 기일내역에 결과를 늦게 채우기
                 -- 때문에 최근 기일은 '매각'만 뜨고 금액이 아직 없다(실측: 최근분
                 -- 49% vs 전체 85%). 오래된 쪽이 회수율이 높고, 정상 경로로는
                 -- 영영 못 얻는 것도 그쪽이다.
                 ORDER BY (result_checked_at IS NULL) DESC,
                          result_checked_at ASC,
                          sale_date ASC
                 LIMIT ?
                """,
                (
                    (date.today() - timedelta(days=self.RESULT_WINDOW_DAYS)).isoformat(),
                    row_limit,
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_popularity(self, rows: list[dict[str, str]], field: str) -> dict[str, int]:
        """다수조회·다수관심 화면에서 받은 인기도 지표를 저장한다.

        두 화면이 각각 조회수와 관심등록수를 주므로 한 번에 한 칸만 채운다.
        상위 물건만 나오는 화면이라 값이 없는 물건은 그대로 비워 둔다."""
        column = {"조회수": "view_count", "관심등록수": "interest_count"}.get(field)
        if not column:
            return {"received": len(rows), "saved": 0}
        now = utc_now()
        prepared: list[tuple[Any, ...]] = []
        for values in rows:
            count = parse_money(values.get(field, ""))
            if count is None:
                continue
            item_key = build_item_key(values)
            if not item_key.startswith("auction:"):
                continue
            prepared.append((item_key, count, now))
        if not prepared:
            return {"received": len(rows), "saved": 0}
        with self.connect() as conn:
            conn.executemany(
                f"""
                INSERT INTO auction_popularity(item_key, {column}, checked_at)
                VALUES(?, ?, ?)
                ON CONFLICT(item_key) DO UPDATE SET
                    {column} = excluded.{column},
                    checked_at = excluded.checked_at
                """,
                prepared,
            )
        return {"received": len(rows), "saved": len(prepared)}

    def sale_result_summary(self) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN sale_amount IS NOT NULL THEN 1 ELSE 0 END) AS sold,
                       MIN(sale_date) AS first_date,
                       MAX(sale_date) AS last_date
                  FROM auction_sale_results
                """
            ).fetchone()
        return {
            "total": row["total"] or 0,
            "sold": row["sold"] or 0,
            "first_date": row["first_date"] or "",
            "last_date": row["last_date"] or "",
        }

    def record_court_stats(
        self,
        counts: dict[tuple[str, str], int],
        *,
        drop_ratio: float = 0.3,
        min_baseline: int = 20,
    ) -> list[str]:
        """전체 수집의 법원별 건수를 기록하고, **진짜** 급감·누락만 경고로 돌려준다.

        예전에는 **직전 기록**과 댔다. 그래서 정상적인 기일 소진까지 경고가 됐다 —
        진행 화면은 '오늘~13일' 창이라 그 창에 그 법원 기일이 없으면 **0건이 정답**이다.
        통영이 9/14 기일을 소진하면 그 뒤로 계속 0인데, 매 사이클 "246건 -> 0건" 이
        울렸다. 6일에 54번. 그래서 아무도 안 보게 됐고 **진짜 신호가 묻혔다**(G17).

        이제 **같은 날 다른 사이클**과 댄다(최근 24시간 최대치). 하루 안에서 0↔비0 이
        갈리는 것만 창이 망가진 것이다 — 실측 58일에서 12.1%가 여기 해당하고, 한 번에
        광주 478건·목포 264건씩 통째로 빠진다.

        그리고 **상태가 아니라 전환**에서만 운다. 직전 사이클도 낮았으면 이미 아는
        일이라 다시 울리지 않는다. 소진된 법원이 하루 종일 짖는 것을 막는다.
        """
        if not counts:
            return []
        now = utc_now()
        warnings: list[str] = []
        하루전 = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        with self.connect() as conn:
            previous: dict[tuple[str, str], int] = {}
            for row in conn.execute(
                """
                SELECT mode, court, item_count FROM court_run_stats
                 WHERE finished_at = (SELECT MAX(finished_at) FROM court_run_stats)
                """
            ):
                previous[(row["mode"], row["court"])] = row["item_count"]

            # 최근 24시간(≈5사이클)의 최대치. 같은 창을 본 다른 사이클이 무엇을 봤는가.
            최근최대: dict[tuple[str, str], int] = {}
            for row in conn.execute(
                "SELECT mode, court, MAX(item_count) AS top FROM court_run_stats "
                " WHERE finished_at >= ? GROUP BY mode, court", (하루전,)
            ):
                최근최대[(row["mode"], row["court"])] = row["top"]

            for (mode, court), count in sorted(counts.items()):
                conn.execute(
                    "INSERT INTO court_run_stats(finished_at, mode, court, item_count) VALUES(?, ?, ?, ?)",
                    (now, mode, court, count),
                )
                기준 = 최근최대.get((mode, court))
                직전 = previous.get((mode, court))
                if 기준 is None or 기준 < min_baseline:
                    continue
                if count >= 기준 * drop_ratio:
                    continue
                # 직전도 이미 낮았으면 전환이 아니다 — 소진된 법원이 하루 종일 짖는다.
                if 직전 is not None and 직전 < 기준 * drop_ratio:
                    continue
                warnings.append(
                    f"{court} {mode} 수집 급감: 오늘 최대 {기준}건 -> {count}건")
            for (mode, court), baseline in sorted(previous.items()):
                if (mode, court) not in counts and baseline >= min_baseline:
                    warnings.append(f"{court} {mode} 이번 수집에서 누락 (이전 {baseline}건)")
        return warnings

    def regions(self) -> list[dict[str, Any]]:
        regions: list[dict[str, Any]] = []
        with self.connect() as conn:
            for name, aliases in REGION_ALIASES.items():
                clauses: list[str] = []
                params: list[Any] = []
                for alias in aliases:
                    clauses.append("(address LIKE ? OR court LIKE ?)")
                    like = f"%{alias}%"
                    params.extend([like, like])
                row = conn.execute(
                    f"SELECT COUNT(*) AS count FROM auction_items WHERE {' OR '.join(clauses)}",
                    params,
                ).fetchone()
                regions.append({"name": name, "count": row["count"]})
        return regions


def extract_common_fields(values: dict[str, str]) -> dict[str, str]:
    case_no = first_value(values, CASE_KEYS)
    court = first_value(values, COURT_KEYS) or infer_court_from_case(case_no)
    return {
        "source": first_value(values, SOURCE_KEYS) or "진행",
        "case_no": case_no,
        "item_no": first_value(values, ITEM_KEYS),
        "court": court,
        "address": strip_address_label(first_value(values, ADDRESS_KEYS)),
        "category": first_value(values, CATEGORY_KEYS),
        "appraisal": first_value(values, APPRAISAL_KEYS),
        "minimum_bid": first_value(values, MINIMUM_KEYS),
        "sale_date": first_value(values, SALE_DATE_KEYS),
        "status": first_value(values, STATUS_KEYS),
        "detail_url": first_value(values, DETAIL_URL_KEYS),
    }


# 주소 맨 앞에 붙어 오는 항목 라벨. 부동산이 아닌 물건(자동차·선박·어장)은 목록 표
# 구조가 달라서 '사용본거지 : 부산광역시…'처럼 라벨이 값에 섞여 들어온다(실측 1,368건).
ADDRESS_LABEL_RE = re.compile(r"^(소재지|사용본거지|선적항|어장의위치|소재지목록)\s*:\s*")


def strip_address_label(address: str) -> str:
    """주소 맨 앞의 항목 라벨만 벗긴다.

    '(현장표시 : …)'나 '[집합건물 건물의번호 : …]'처럼 주소 안에 콜론이 들어간 정상
    표기가 있어서, 통째로 자르면 멀쩡한 주소가 잘린다. 시작 위치의 알려진 라벨만 본다."""
    return ADDRESS_LABEL_RE.sub("", clean_text(address), count=1)


def merge_detail_json(existing_text: str | None, list_fields: dict[str, str]) -> str:
    """목록에서 뽑은 값을 기존 상세 위에 얹는다. 통째로 갈아끼우면 안 된다.

    상세 크롤러가 채운 본문(기일내역·감정평가·사진 목록 등 평균 22KB)을 목록 갱신이
    덮어써서, 물건이 한 번 바뀔 때마다 상세가 68바이트짜리 껍데기로 되돌아가고 있었다
    (실측 6,871건). detail_status는 collected로 남아 있어 겉으로는 정상으로 보인다.
    재수집 대기열이 결국 다시 채우지만, 그 사이 API와 웹에는 빈 상세가 나간다."""
    try:
        merged = json.loads(existing_text) if existing_text else {}
    except (TypeError, ValueError):
        merged = {}
    if not isinstance(merged, dict):
        merged = {}
    # 목록이 더 최신인 값(담당계·비고 등)만 갱신하고 크롤링 본문은 남긴다.
    merged.update(list_fields)
    return json_dumps(merged)


def extract_detail_fields(values: dict[str, str]) -> dict[str, str]:
    common = set().union(
        CASE_KEYS,
        ITEM_KEYS,
        ADDRESS_KEYS,
        CATEGORY_KEYS,
        APPRAISAL_KEYS,
        MINIMUM_KEYS,
        SALE_DATE_KEYS,
        STATUS_KEYS,
        COURT_KEYS,
        DETAIL_URL_KEYS,
        SOURCE_KEYS,
    )
    return {key: value for key, value in values.items() if key not in common and value}


def first_value(values: dict[str, str], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = clean_text(values.get(key))
        if value:
            return value
    return ""


def infer_court_from_case(case_no: str) -> str:
    """사건번호 앞에 붙은 법원명을 뽑는다.

    사이트는 '안양지원 2025타경101127'로 줄 때도 있고 '안양지원2025타경101127'처럼
    붙여서 줄 때도 있다. 공백만 보고 자르면 붙은 형태에서 법원을 통째로 놓치는데,
    그러면 item_key가 'auction:-:...'가 되고 상세 크롤러가 법원 드롭다운을 고르지
    못해 그 물건은 상세 수집 대상에서 아예 빠진다(실측: 활성 4,065건이 시도조차
    되지 않은 채 pending에 머물러 있었다).

    그래서 공백이 아니라 사건번호가 시작되는 위치를 기준으로 자른다."""
    text = clean_text(case_no)
    match = CASE_NO_RE.search(text)
    if match:
        prefix = text[: match.start()].strip()
        if prefix.endswith(("법원", "지원")):
            return prefix
    return ""


def _migration_row_rank(row: sqlite3.Row) -> tuple[str, str]:
    """병합 대상 중 대표 행 선택: 가장 새 매각기일(=갱신된 회차)이 우선, 다음은 최근 목격."""
    sale_date = sale_date_of(row["sale_date"] or "")
    return (sale_date.isoformat() if sale_date else "", row["last_seen_at"] or "")


def representative_case_no(case_no: str) -> str:
    """병합·중복사건은 사건번호가 여러 개 붙는다. 첫 번째(선행) 사건번호로 물건을 식별한다."""
    text = clean_text(case_no)
    match = CASE_NO_RE.search(text)
    if match:
        return match.group(0)
    return text


def build_item_key(values: dict[str, str]) -> str:
    common = extract_common_fields(values)
    case_no = representative_case_no(common["case_no"])
    if case_no and common["item_no"]:
        # 사건번호는 법원별로 중복될 수 있으므로 법원이 키에 반드시 포함되어야 한다.
        # 예정/진행은 같은 물건이므로 수집구분은 키에 넣지 않는다.
        parts = [common["court"], case_no, common["item_no"]]
        return "auction:" + ":".join(part or "-" for part in parts)
    digest_source = json_dumps(values)
    return "hash:" + hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:24]


def is_valid_auction_item(values: dict[str, str]) -> bool:
    common = extract_common_fields(values)
    return bool(common["case_no"] and common["item_no"])


def normalize_sale_date_filter(value: str) -> str:
    cleaned = clean_text(value).replace("-", ".").replace("/", ".")
    try:
        return parse_date(cleaned).strftime("%Y.%m.%d")
    except ValueError:
        return cleaned


def list_order_by(sort: str) -> str:
    return {
        "last_seen_desc": "last_seen_at DESC, updated_at DESC",
        "last_seen_asc": "last_seen_at ASC, updated_at ASC",
        "sale_date_asc": "sale_date ASC, last_seen_at DESC",
        "sale_date_desc": "sale_date DESC, last_seen_at DESC",
        "priority_desc": "crawl_priority DESC, next_check_at ASC, last_seen_at DESC",
    }.get(sort, "last_seen_at DESC, updated_at DESC")


def stable_hash(values: dict[str, str]) -> str:
    filtered = {key: value for key, value in values.items() if key not in VOLATILE_HASH_KEYS}
    return hashlib.sha256(json_dumps(filtered).encode("utf-8")).hexdigest()


def stable_list_hash(values: dict[str, str]) -> str:
    normalized = {key: clean_text(values.get(key)) for key in LIST_HASH_KEYS if clean_text(values.get(key))}
    return hashlib.sha256(json_dumps(normalized).encode("utf-8")).hexdigest()


def sale_date_of(values_or_text: dict[str, str] | str) -> date | None:
    if isinstance(values_or_text, dict):
        text = first_value(values_or_text, SALE_DATE_KEYS)
    else:
        text = clean_text(values_or_text)
    try:
        return parse_date(text.replace(" ", ""))
    except ValueError:
        return None


def merge_parcel_rows(group: list[dict[str, str]]) -> dict[str, str]:
    """일괄매각 물건은 필지마다 목록 행이 하나씩 나온다.

    같은 키의 행들을 대표 소재지(정렬 최솟값) 기준으로 하나로 합치고,
    나머지 필지는 소재지목록으로 보존한다. 수집 순서와 무관하게 결정적이어야
    해시가 출렁이지 않는다.
    """
    if len(group) == 1:
        return group[0]
    addresses = sorted({first_value(values, ADDRESS_KEYS) for values in group} - {""})
    representative = min(
        group,
        key=lambda values: (first_value(values, ADDRESS_KEYS) or "￿", json_dumps(values)),
    )
    merged = dict(representative)
    if len(addresses) > 1:
        merged[PARCEL_LIST_KEY] = " | ".join(addresses)
    return merged


def aggregate_items_by_key(items: list[AuctionItem]) -> list[tuple[str, dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    order: list[str] = []
    for item in items:
        values = item.normalized()
        if not is_valid_auction_item(values):
            continue
        key = build_item_key(values)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(values)
    return [(key, merge_parcel_rows(grouped[key])) for key in order]


def retire_sale(
    conn: sqlite3.Connection,
    item_key: str,
    extracted: dict[str, Any],
    existing: sqlite3.Row,
    now: str,
) -> bool:
    """되살아난 물건의 낙찰을 이력(auction_sale_results)으로 옮기고 현재 낙찰을 비운다.

    **정보를 버리는 게 아니다.** 실측한 18건 중 6건은 이력 쪽에 금액이 없어서,
    그냥 비우면 낙찰가를 영영 잃는다. 먼저 이력에 넣고 나서 비운다.

    새 기일이 낙찰일보다 뒤일 때만 은퇴시킨다. 낙찰 직후 아직 처리 중인 물건을
    건드리지 않기 위해서다(backfill_sale_results의 되살아남 방지 가드와 같은 뜻).
    """
    sold_date = str(existing["sold_date"] or "")
    if not sold_date or not (str(extracted["sale_date"] or "") > sold_date):
        return False
    conn.execute(
        """
        INSERT INTO auction_sale_results(
            court, case_no, item_no, sale_date, result,
            sale_amount, minimum_bid, appraisal, item_key, raw_json, collected_at)
        VALUES(?, ?, ?, ?, '매각', ?, NULL, NULL, ?, '{}', ?)
        ON CONFLICT(court, case_no, item_no, sale_date) DO UPDATE SET
            sale_amount = COALESCE(auction_sale_results.sale_amount, excluded.sale_amount),
            result = CASE WHEN auction_sale_results.result = '' THEN excluded.result
                          ELSE auction_sale_results.result END,
            item_key = CASE WHEN auction_sale_results.item_key = '' THEN excluded.item_key
                            ELSE auction_sale_results.item_key END
        """,
        (
            extracted["court"],
            representative_case_no(extracted["case_no"]),
            extracted["item_no"],
            sold_date,
            existing["sold_amount"],
            item_key,
            now,
        ),
    )
    conn.execute(
        # sold_date는 NOT NULL DEFAULT ''다. 빈 문자열이 '낙찰 아님'의 표현이다.
        "UPDATE auction_items SET sold_amount = NULL, sold_date = '', updated_at = ? WHERE item_key = ?",
        (now, item_key),
    )
    return True


def is_active_status(status: str) -> bool:
    clean_status = clean_text(status)
    if not clean_status:
        return True
    if "유찰" in clean_status or "신건" in clean_status:
        return True
    return not any(keyword in clean_status for keyword in TERMINAL_STATUS_KEYWORDS)


def calculate_next_check_at(status: str, sale_date: str, changed: bool, now: str) -> str:
    base = parse_iso_datetime(now)
    if not is_active_status(status):
        return (base + timedelta(days=7)).isoformat(timespec="seconds")

    status_text = clean_text(status)
    if "유찰" in status_text:
        delta = timedelta(hours=6)
    else:
        remaining = days_until_sale(sale_date, base.date())
        if remaining is None:
            delta = timedelta(hours=12)
        elif remaining <= 0:
            delta = timedelta(hours=1)
        elif remaining <= 2:
            delta = timedelta(hours=1)
        elif remaining <= 7:
            delta = timedelta(hours=6)
        elif remaining <= 30:
            delta = timedelta(days=1)
        else:
            delta = timedelta(days=3)

    if changed:
        delta = min(delta, timedelta(hours=6))
    return (base + delta).isoformat(timespec="seconds")


def calculate_crawl_priority(status: str, sale_date: str) -> int:
    if not is_active_status(status):
        return -100
    score = 0
    remaining = days_until_sale(sale_date, date.today())
    if remaining is not None:
        if remaining <= 1:
            score += 100
        elif remaining <= 3:
            score += 70
        elif remaining <= 7:
            score += 50
        elif remaining <= 30:
            score += 20
    if "유찰" in clean_text(status):
        score += 30
    return score


def detail_retry_at(now: datetime, retry_hours: int, sale_date: str) -> str:
    """상세 재시도 시각을 정하되, 기일을 넘겨 잡지 않는다.

    백오프는 실패할수록 배증해 최대 7일까지 간다. 그런데 기일은 보통 2주 앞이라
    몇 번만 헛걸음해도 다음 방문이 기일 뒤로 밀린다. 그러면 그 물건은 팔릴
    때까지 다시 안 가보게 된다 — 실패 원인을 잘못 판단했을 때 조용히 놓치는
    경로가 정확히 여기다. 기일 하루 전으로 당겨 최소 한 번은 더 확인한다.
    (기일이 이미 지났거나 못 읽으면 백오프 그대로 둔다.)"""
    scheduled = now + timedelta(hours=retry_hours)
    remaining = days_until_sale(sale_date, now.date())
    if remaining is not None and remaining > 0:
        deadline = now + timedelta(days=remaining - 1) if remaining > 1 else now + timedelta(hours=1)
        scheduled = min(scheduled, max(deadline, now + timedelta(hours=1)))
    return scheduled.isoformat(timespec="seconds")


def days_until_sale(value: str, today: date) -> int | None:
    try:
        parsed = parse_date(value.replace(" ", ""))
    except ValueError:
        return None
    if parsed is None:
        return None
    return (parsed - today).days


def parse_iso_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.now(timezone.utc)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
