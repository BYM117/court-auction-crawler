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

from .common import TERMINAL_STATUS_KEYWORDS, utc_now
from .models import AuctionItem, SyncSummary
from .utils import clean_text, parse_date


SCHEMA_VERSION = 4


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
CASE_NO_RE = re.compile(r"\d{4}타경\d+")
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
                    "SELECT raw_json, content_hash, list_hash, sale_date FROM auction_items WHERE item_key = ?",
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
                            json_dumps(extract_detail_fields(values)),
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
                f"""
                SELECT item_key, source, case_no, item_no, court, address, category, appraisal,
                       minimum_bid, sale_date, status, detail_url, lat, lng, pnu,
                       coordinate_source, coordinate_quality, normalized_address,
                       geocode_query, geocoded_at,
                       official_price, official_price_type, official_price_year,
                       official_price_detail, official_price_status, official_price_at,
                       first_seen_at,
                       last_seen_at, last_changed_at, next_check_at, is_active,
                       crawl_priority, detail_status, detail_collected_at,
                       detail_checked_at, detail_next_retry_at, detail_fail_count,
                       updated_at
                  FROM auction_items
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
    ) -> list[dict[str, Any]]:
        clauses = ["court != ''", "case_no != ''", "item_no != ''"]
        params: list[Any] = []
        if not include_inactive:
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
                        last_changed_at IS NOT NULL
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
                )
                """
            )
            now = utc_now()
            params.extend([now, now, now])
        row_limit = 1_000_000 if limit is None or limit <= 0 else min(limit, 1_000_000)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT item_key, court, case_no, item_no, sale_date, status,
                       detail_status, detail_collected_at, detail_next_retry_at,
                       detail_fail_count, last_changed_at
                  FROM auction_items
                 WHERE {' AND '.join(f'({clause})' for clause in clauses)}
                 ORDER BY (detail_collected_at IS NULL) DESC,
                          (REPLACE(SUBSTR(sale_date, 1, 10), '.', '-') > ?) DESC,
                          (sale_date IS NULL OR sale_date = '') ASC,
                          sale_date ASC,
                          detail_fail_count ASC,
                          crawl_priority DESC,
                          last_seen_at DESC
                 LIMIT ?
                """,
                [*params, date.today().isoformat(), row_limit],
            ).fetchall()
        return [dict(row) for row in rows]

    def save_item_detail(self, item_key: str, detail: dict[str, Any]) -> None:
        now = utc_now()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT detail_json FROM auction_items WHERE item_key = ?",
                (item_key,),
            ).fetchone()
            if row is None:
                raise KeyError(item_key)
            try:
                merged = json.loads(row["detail_json"] or "{}")
            except (TypeError, ValueError):
                merged = {}
            merged.update(detail)
            conn.execute(
                """
                UPDATE auction_items
                   SET detail_json = ?, detail_status = 'collected',
                       detail_collected_at = ?, detail_checked_at = ?,
                       detail_next_retry_at = NULL, detail_fail_count = 0,
                       detail_error = '', updated_at = ?
                 WHERE item_key = ?
                """,
                (json_dumps(merged), now, now, now, item_key),
            )

    def mark_detail_failure(self, item_key: str, error: str) -> None:
        now = datetime.now(timezone.utc)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT detail_fail_count, detail_collected_at FROM auction_items WHERE item_key = ?",
                (item_key,),
            ).fetchone()
            if row is None:
                return
            fail_count = int(row["detail_fail_count"] or 0) + 1
            retry_hours = min(24 * 7, 2 ** min(fail_count - 1, 7))
            next_retry = (now + timedelta(hours=retry_hours)).isoformat(timespec="seconds")
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

        item = dict(row)
        item["raw"] = json.loads(item.pop("raw_json") or "{}")
        item["detail"] = json.loads(item.pop("detail_json") or "{}")
        item["events"] = [dict(event) for event in events]
        item["documents"] = []
        for document in documents:
            document_payload = dict(document)
            document_payload["metadata"] = json.loads(document_payload.pop("metadata_json") or "{}")
            item["documents"].append(document_payload)
        item["assets"] = [dict(asset) for asset in assets]
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

    def mark_coordinate_missing(
        self,
        item_key: str,
        *,
        normalized_address: str = "",
        geocode_query: str = "",
        quality: str = "missing",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE auction_items
                   SET coordinate_quality = ?,
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
        next_check = (now_dt + timedelta(days=7)).isoformat(timespec="seconds")
        # 기일이 유예기간 넘게 지났고 새 기일이 없거나, 기일 없는 물건이 오래 미목격이면 종결.
        expired_clause = (
            "is_active = 1 AND ("
            "  (COALESCE(sale_date, '') != '' "
            "   AND REPLACE(SUBSTR(sale_date, 1, 10), '.', '-') < ?)"
            "  OR (COALESCE(sale_date, '') = '' AND COALESCE(last_seen_at, '') < ?)"
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
                (now_text, grace_cutoff, unseen_cutoff),
            )
            cursor = conn.execute(
                f"""
                UPDATE auction_items
                   SET is_active = 0, crawl_priority = -100, next_check_at = ?
                 WHERE {expired_clause}
                """,
                (next_check, grace_cutoff, unseen_cutoff),
            )
            deactivated = cursor.rowcount
        return {"checked": checked, "deactivated": deactivated}

    def record_court_stats(
        self,
        counts: dict[tuple[str, str], int],
        *,
        drop_ratio: float = 0.3,
        min_baseline: int = 20,
    ) -> list[str]:
        """전체 수집의 법원별 건수를 기록하고, 직전 기록 대비 급감·누락 경고를 돌려준다.

        사이트 개편으로 추출이 조용히 깨지는 것이 가장 위험한 누락 시나리오라서,
        법원 단위 건수 급감을 감지해 로그로 드러낸다."""
        if not counts:
            return []
        now = utc_now()
        warnings: list[str] = []
        with self.connect() as conn:
            previous: dict[tuple[str, str], int] = {}
            rows = conn.execute(
                """
                SELECT mode, court, item_count FROM court_run_stats
                 WHERE finished_at = (SELECT MAX(finished_at) FROM court_run_stats)
                """
            ).fetchall()
            for row in rows:
                previous[(row["mode"], row["court"])] = row["item_count"]

            for (mode, court), count in sorted(counts.items()):
                conn.execute(
                    "INSERT INTO court_run_stats(finished_at, mode, court, item_count) VALUES(?, ?, ?, ?)",
                    (now, mode, court, count),
                )
                baseline = previous.get((mode, court))
                if baseline is not None and baseline >= min_baseline and count < baseline * drop_ratio:
                    warnings.append(f"{court} {mode} 수집 급감: {baseline}건 -> {count}건")
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
        "address": first_value(values, ADDRESS_KEYS),
        "category": first_value(values, CATEGORY_KEYS),
        "appraisal": first_value(values, APPRAISAL_KEYS),
        "minimum_bid": first_value(values, MINIMUM_KEYS),
        "sale_date": first_value(values, SALE_DATE_KEYS),
        "status": first_value(values, STATUS_KEYS),
        "detail_url": first_value(values, DETAIL_URL_KEYS),
    }


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
    marker = " "
    if marker in case_no:
        prefix = case_no.split(marker, 1)[0].strip()
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
