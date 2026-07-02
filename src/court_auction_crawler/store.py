from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from .models import AuctionItem, SyncSummary
from .utils import clean_text, parse_date


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
LIST_HASH_KEYS = (
    "수집구분",
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
TERMINAL_STATUS_KEYWORDS = ("낙찰", "매각", "취하", "기각", "정지", "취소", "종결", "배당")
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

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

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
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_auction_items_last_seen
                    ON auction_items(last_seen_at DESC);
                CREATE INDEX IF NOT EXISTS idx_auction_items_sale_date
                    ON auction_items(sale_date);
                CREATE INDEX IF NOT EXISTS idx_auction_items_status
                    ON auction_items(status);
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
            conn.execute("UPDATE auction_items SET source = '진행' WHERE source = ''")
            conn.execute("UPDATE auction_items SET list_hash = content_hash WHERE list_hash = ''")
            conn.execute("UPDATE auction_items SET last_changed_at = updated_at WHERE last_changed_at IS NULL")
            conn.execute("UPDATE auction_items SET next_check_at = updated_at WHERE next_check_at IS NULL")
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
            self._backfill_schedule(conn)

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
            for item in items:
                values = item.normalized()
                if not is_valid_auction_item(values):
                    continue
                item_key = build_item_key(values)
                raw_json = json_dumps(values)
                content_hash = stable_hash(values)
                list_hash = stable_list_hash(values)
                existing = conn.execute(
                    "SELECT raw_json, content_hash, list_hash FROM auction_items WHERE item_key = ?",
                    (item_key,),
                ).fetchone()

                extracted = extract_common_fields(values)
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
            clauses.append(
                "(case_no LIKE ? OR item_no LIKE ? OR court LIKE ? OR address LIKE ? OR category LIKE ? OR raw_json LIKE ?)"
            )
            like = f"%{query}%"
            params.extend([like, like, like, like, like, like])
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
                       geocode_query, geocoded_at, first_seen_at,
                       last_seen_at, last_changed_at, next_check_at, is_active,
                       crawl_priority, updated_at
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

    def list_missing_coordinates(self, limit: int = 200, active: bool | None = True) -> list[dict[str, Any]]:
        clauses = ["lat IS NULL OR lng IS NULL"]
        params: list[Any] = []
        if active is not None:
            clauses.append("is_active = ?")
            params.append(1 if active else 0)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT item_key, address, coordinate_quality, geocoded_at
                  FROM auction_items
                 WHERE {' AND '.join(f'({clause})' for clause in clauses)}
                 ORDER BY crawl_priority DESC, last_seen_at DESC
                 LIMIT ?
                """,
                [*params, min(max(limit, 1), 5000)],
            ).fetchall()
        return [dict(row) for row in rows]

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

        item = dict(row)
        item["raw"] = json.loads(item.pop("raw_json") or "{}")
        item["detail"] = json.loads(item.pop("detail_json") or "{}")
        item["events"] = [dict(event) for event in events]
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

    def mark_coordinate_missing(
        self,
        item_key: str,
        *,
        normalized_address: str = "",
        geocode_query: str = "",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE auction_items
                   SET coordinate_quality = 'missing',
                       normalized_address = ?,
                       geocode_query = ?,
                       geocoded_at = ?,
                       updated_at = ?
                 WHERE item_key = ?
                """,
                (
                    normalized_address,
                    geocode_query,
                    utc_now(),
                    utc_now(),
                    item_key,
                ),
            )

    def stats(self) -> dict[str, Any]:
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
        }

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


def build_item_key(values: dict[str, str]) -> str:
    common = extract_common_fields(values)
    parts = [common["case_no"], common["item_no"]]
    if any(parts):
        if common["source"] in {"예정", "매각예정"}:
            return "auction:scheduled:" + ":".join(part or "-" for part in parts)
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
    return hashlib.sha256(json_dumps(values).encode("utf-8")).hexdigest()


def stable_list_hash(values: dict[str, str]) -> str:
    normalized = {key: clean_text(values.get(key)) for key in LIST_HASH_KEYS if clean_text(values.get(key))}
    return hashlib.sha256(json_dumps(normalized).encode("utf-8")).hexdigest()


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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
