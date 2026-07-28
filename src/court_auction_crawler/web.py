from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
import re
import signal
from pathlib import Path
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .common import process_is_running, read_pid
# 순수 변환 함수는 enrichment.py로 분리했고, 기존 `from .web import ...` 경로를
# 유지하기 위해 여기서 re-export한다.
from .enrichment import (  # noqa: F401
    build_official_price,
    build_registry_search_hint,
    build_screening,
    infer_property_type,
    infer_registry_realty_type,
    normalize_case_number,
    normalize_date_text,
    parse_area_info,
    parse_bid_percent,
    parse_fail_count,
    parse_first_money,
    parse_item_active,
    parse_optional_bool,
    parse_optional_float,
    parse_property_address,
    parse_share_info,
    public_auction_detail,
    public_auction_enrichment,
    public_auction_list,
    public_auction_summary,
    public_stats,
    safe_external_url,
)
from .geocoder import geocode_address
# 러너 클래스는 runners.py로 분리했고, 기존 `from .web import CollectorControlRunner`
# 경로를 유지하기 위해 re-export한다.
from .runners import CollectorControlRunner, DbHealthWatchdog, WatchRunner  # noqa: F401
from .store import AuctionStore


STATIC_DIR = Path(__file__).parent / "web_static"
PARTITION_RE = re.compile(r"\[(\d+)/(\d+)\]\s+(.+)")
DEFAULT_ALLOWED_ORIGINS = {
    "http://127.0.0.1:4173",
    "http://localhost:4173",
}


class AuctionWebHandler(BaseHTTPRequestHandler):
    store: AuctionStore
    runner: WatchRunner | None = None
    collector_runner: CollectorControlRunner | None = None

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_static("index.html", "text/html; charset=utf-8", head_only=True)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/":
            self._send_static("index.html", "text/html; charset=utf-8")
            return
        if path == "/app.js":
            self._send_static("app.js", "application/javascript; charset=utf-8")
            return
        if path == "/styles.css":
            self._send_static("styles.css", "text/css; charset=utf-8")
            return
        if path == "/api/stats":
            payload = self.store.stats()
            collector_payload = collect_log_status()
            if self.runner:
                payload["runner"] = {
                    "enabled": True,
                    "running": self.runner.is_running,
                    "last_error": self.runner.last_error,
                    "interval_seconds": self.runner.interval_seconds,
                }
            else:
                payload["runner"] = {"enabled": False, "running": False, "last_error": "", "interval_seconds": None}
            collector_control = (
                self.collector_runner.status()
                if self.collector_runner
                else {"enabled": False, "running": False, "interval_seconds": None, "last_error": ""}
            )
            if not collector_control.get("enabled") and not collector_control.get("running"):
                collector_payload["state"] = "idle"
                collector_payload["state_label"] = "수집 중지"
                collector_payload["current"] = "시작 버튼을 누르면 다시 수집합니다"
            payload["collector"] = collector_payload
            payload["collector_control"] = collector_control
            self._send_json(payload)
            return
        if path == "/api/items":
            payload = self.store.list_items(
                query=first_query(query, "query"),
                status=first_query(query, "status"),
                source=first_query(query, "source"),
                region=first_query(query, "region"),
                limit=parse_int(first_query(query, "limit"), 100),
                offset=parse_int(first_query(query, "offset"), 0),
            )
            self._send_json(payload)
            return
        if path.startswith("/api/items/"):
            item_key = unquote(path.removeprefix("/api/items/"))
            item = self.store.get_item(item_key)
            if item is None:
                self._send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            else:
                self._send_json(item)
            return
        if path == "/api/v1/health":
            self._send_json({"ok": True, "version": __version__})
            return
        if path == "/api/v1/stats":
            self._send_json(public_stats(self.store.stats()))
            return
        if path == "/api/v1/openapi.json":
            self._send_json(openapi_schema())
            return
        if path == "/api/v1/auctions":
            self._send_json(self._public_auction_list_payload(query))
            return
        if path == "/api/v1/regions":
            self._send_json({"regions": self.store.regions()})
            return
        if path == "/api/v1/collector/status":
            self._send_json(self._collector_status_payload())
            return
        if path.startswith("/api/v1/assets/"):
            asset_id = parse_int(path.removeprefix("/api/v1/assets/"), 0)
            self._send_managed_file(self.store.get_asset(asset_id))
            return
        if path.startswith("/api/v1/documents/"):
            document_id = parse_int(path.removeprefix("/api/v1/documents/"), 0)
            self._send_managed_file(self.store.get_document(document_id))
            return
        if path.startswith("/api/v1/auctions/"):
            item_key = unquote(path.removeprefix("/api/v1/auctions/"))
            item = self.store.get_item(item_key)
            if item is None:
                self._send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            else:
                self._send_json(public_auction_detail(item))
            return

        self._send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self._require_control_auth():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/refresh":
            if self.runner is None:
                self._send_json({"ok": False, "message": "서버가 watch 옵션 없이 실행되었습니다."}, HTTPStatus.CONFLICT)
                return
            started = self.runner.trigger_once()
            self._send_json({"ok": started, "message": "동기화를 시작했습니다." if started else "이미 동기화 중입니다."})
            return
        if parsed.path == "/api/collector/start":
            if self.collector_runner is None:
                self._send_json({"ok": False, "message": "수집 제어기가 없습니다."}, HTTPStatus.CONFLICT)
                return
            self._send_json(self._start_collector_payload())
            return
        if parsed.path == "/api/collector/stop":
            if self.collector_runner is None:
                self._send_json({"ok": False, "message": "수집 제어기가 없습니다."}, HTTPStatus.CONFLICT)
                return
            self._send_json(self._stop_collector_payload())
            return
        if parsed.path == "/api/v1/collector/start":
            if self.collector_runner is None:
                self._send_json({"ok": False, "message": "수집 제어기가 없습니다."}, HTTPStatus.CONFLICT)
                return
            self._send_json(self._start_collector_payload())
            return
        if parsed.path == "/api/v1/collector/stop":
            if self.collector_runner is None:
                self._send_json({"ok": False, "message": "수집 제어기가 없습니다."}, HTTPStatus.CONFLICT)
                return
            self._send_json(self._stop_collector_payload())
            return
        self._send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def _require_control_auth(self) -> bool:
        expected = os.environ.get("AUCTION_API_KEY", "").strip()
        if control_api_key_valid(
            expected,
            self.headers.get("X-API-Key", ""),
            self.headers.get("Authorization", ""),
        ):
            return True
        self._send_json({"error": "unauthorized", "message": "수집 제어 권한이 없습니다."}, HTTPStatus.UNAUTHORIZED)
        return False

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send_static(self, filename: str, content_type: str, head_only: bool = False) -> None:
        path = STATIC_DIR / filename
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self._send_cors_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_managed_file(self, record: dict[str, Any] | None) -> None:
        if record is None:
            self._send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        path = Path(str(record.get("file_path", ""))).resolve()
        configured_root = os.environ.get("AUCTION_ASSET_DIR", "").strip()
        allowed_root = Path(configured_root).resolve() if configured_root else (
            self.store.db_path.parent / "auction-assets"
        ).resolve()
        try:
            path.relative_to(allowed_root)
        except ValueError:
            self._send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        if not path.is_file():
            self._send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self._send_cors_headers()
        self.send_header("Content-Type", record.get("content_type") or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400, immutable")
        self.end_headers()
        self.wfile.write(data)

    def _send_cors_headers(self) -> None:
        origin = self.headers.get("Origin", "")
        allowed_origin = allowed_cors_origin(origin)
        if allowed_origin:
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-API-Key")

    def _public_auction_list_payload(self, query: dict[str, list[str]]) -> dict[str, Any]:
        search_query = first_query(query, "query") or first_query(query, "q")
        payload = self.store.list_items(
            query=search_query,
            status=first_query(query, "status"),
            source=first_query(query, "source"),
            region=first_query(query, "region"),
            sale_date_from=first_query(query, "sale_date_from"),
            sale_date_to=first_query(query, "sale_date_to"),
            active=parse_optional_bool(first_query(query, "active")),
            require_coordinates=parse_optional_bool(first_query(query, "require_coordinates")) is True,
            sw_lat=parse_optional_float(first_query(query, "swLat") or first_query(query, "sw_lat")),
            sw_lng=parse_optional_float(first_query(query, "swLng") or first_query(query, "sw_lng")),
            ne_lat=parse_optional_float(first_query(query, "neLat") or first_query(query, "ne_lat")),
            ne_lng=parse_optional_float(first_query(query, "neLng") or first_query(query, "ne_lng")),
            sort=first_query(query, "sort") or "last_seen_desc",
            limit=parse_int(first_query(query, "limit"), 100),
            offset=parse_int(first_query(query, "offset"), 0),
        )
        return public_auction_list(payload)

    def _ensure_coordinates(self, items: list[dict[str, Any]]) -> None:
        for item in items:
            if item.get("lat") and item.get("lng"):
                continue
            result = geocode_address(item.get("address", ""))
            if result is None:
                item["coordinate_quality"] = "missing"
                continue
            item.update(
                {
                    "lat": result.lat,
                    "lng": result.lng,
                    "pnu": result.pnu,
                    "coordinate_source": result.source,
                    "coordinate_quality": result.quality,
                    "normalized_address": result.normalized_address,
                    "geocode_query": result.query,
                    "geocoded_at": "",
                }
            )
            self.store.update_coordinates(
                item.get("item_key", ""),
                lat=result.lat,
                lng=result.lng,
                pnu=result.pnu,
                coordinate_source=result.source,
                coordinate_quality=result.quality,
                normalized_address=result.normalized_address,
                geocode_query=result.query,
            )

    def _start_collector_payload(self) -> dict[str, Any]:
        started = self.collector_runner.start() if self.collector_runner else False
        return {
            "ok": True,
            "started": started,
            "message": "자동 수집을 시작했습니다." if started else "자동 수집이 이미 켜져 있습니다.",
            "collector_control": self.collector_runner.status() if self.collector_runner else {},
        }

    def _stop_collector_payload(self) -> dict[str, Any]:
        stopped = self.collector_runner.stop() if self.collector_runner else False
        return {
            "ok": True,
            "stopped": stopped,
            "message": "자동 수집을 종료했습니다.",
            "collector_control": self.collector_runner.status() if self.collector_runner else {},
        }

    def _collector_status_payload(self) -> dict[str, Any]:
        collector_payload = collect_log_status()
        collector_control = (
            self.collector_runner.status()
            if self.collector_runner
            else {"enabled": False, "running": False, "interval_seconds": None, "last_error": ""}
        )
        if not collector_control.get("enabled") and not collector_control.get("running"):
            collector_payload["state"] = "idle"
            collector_payload["state_label"] = "수집 중지"
            collector_payload["current"] = "시작 버튼을 누르면 다시 수집합니다"
        return {
            "collector": collector_payload,
            "collector_control": collector_control,
        }


def run_server(
    store: AuctionStore,
    host: str,
    port: int,
    runner: WatchRunner | None = None,
) -> None:
    # 실제 목록 수집은 독립 프로세스(collect-loop)가 담당한다. 서버는 이 컨트롤러로
    # enabled 토글과 상태 조회만 한다(수집을 서버 스레드에 묶지 않는다).
    collector_runner = CollectorControlRunner(store)
    handler = type(
        "ConfiguredAuctionWebHandler",
        (AuctionWebHandler,),
        {"store": store, "runner": runner, "collector_runner": collector_runner},
    )
    server = ThreadingHTTPServer((host, port), handler)
    if runner:
        runner.start()
    watchdog = DbHealthWatchdog(store)
    watchdog.start()
    previous_sigint = signal.getsignal(signal.SIGINT)
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def handle_shutdown_signal(signum: int, _frame: Any) -> None:
        watchdog.stop()
        if runner:
            runner.stop()
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, handle_shutdown_signal)
    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    try:
        print(f"웹 대시보드: http://{host}:{port}")
        server.serve_forever()
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
        watchdog.stop()
        if runner:
            runner.stop()
        server.server_close()


def allowed_cors_origin(origin: str) -> str:
    configured = os.environ.get("AUCTION_API_ALLOWED_ORIGINS", "")
    allowed = {value.strip() for value in configured.split(",") if value.strip()} or DEFAULT_ALLOWED_ORIGINS
    if "*" in allowed:
        return "*"
    return origin if origin in allowed else ""


def control_api_key_valid(expected: str, provided: str, authorization: str) -> bool:
    expected = str(expected or "").strip()
    if not expected:
        return True
    token = str(provided or "").strip()
    auth = str(authorization or "").strip()
    if not token and auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    return hmac.compare_digest(token, expected)


def first_query(query: dict[str, list[str]], name: str) -> str:
    return query.get(name, [""])[0].strip()


def parse_int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def openapi_schema() -> dict[str, Any]:
    auction_summary = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "source": {"type": "string", "description": "진행 또는 예정"},
            "case_no": {"type": "string"},
            "item_no": {"type": "string"},
            "court": {"type": "string"},
            "address": {"type": "string"},
            "category": {"type": "string"},
            "appraisal": {"type": "string"},
            "minimum_bid": {"type": "string"},
            "sale_date": {"type": "string"},
            "status": {"type": "string"},
            "detail_url": {"type": "string"},
            "last_seen_at": {"type": "string"},
            "updated_at": {"type": "string"},
            "detail_status": {"type": "string"},
            "detail_collected_at": {"type": "string"},
            "case": {"type": "object", "description": "사건번호, 법원, 물건번호를 묶은 정규화 블록"},
            "auction": {"type": "object", "description": "매각기일, 진행상태, 유찰횟수, 활성 여부"},
            "property": {"type": "object", "description": "주소, 유형, 면적, 지분, 등기 검색 힌트"},
            "price": {"type": "object", "description": "감정가/최저가 숫자값과 최저가율"},
            "map": {"type": "object", "description": "좌표 및 좌표 품질 정보"},
            "screening": {"type": "object", "description": "꽁지맵 선별용 점수와 위험 플래그"},
        },
    }
    list_parameters = [
        {"name": "q", "in": "query", "schema": {"type": "string"}, "description": "통합 검색어"},
        {"name": "query", "in": "query", "schema": {"type": "string"}, "description": "q와 동일"},
        {"name": "status", "in": "query", "schema": {"type": "string"}},
        {"name": "source", "in": "query", "schema": {"type": "string", "enum": ["진행", "예정"]}},
        {"name": "region", "in": "query", "schema": {"type": "string"}},
        {"name": "sale_date_from", "in": "query", "schema": {"type": "string", "example": "2026-07-01"}},
        {"name": "sale_date_to", "in": "query", "schema": {"type": "string", "example": "2026-07-31"}},
        {"name": "active", "in": "query", "schema": {"type": "boolean"}},
        {
            "name": "sort",
            "in": "query",
            "schema": {
                "type": "string",
                "enum": ["last_seen_desc", "last_seen_asc", "sale_date_asc", "sale_date_desc", "priority_desc"],
            },
        },
        {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 100, "maximum": 500}},
        {"name": "offset", "in": "query", "schema": {"type": "integer", "default": 0}},
    ]
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Court Auction Crawler API",
            "version": __version__,
            "description": "SQLite에 저장된 법원 경매 물건을 조회하고 수집 상태를 확인하는 로컬 API",
        },
        "paths": {
            "/api/v1/health": {
                "get": {
                    "summary": "Health check",
                    "responses": {"200": {"description": "API is running"}},
                }
            },
            "/api/v1/stats": {
                "get": {
                    "summary": "Collection statistics",
                    "responses": {"200": {"description": "Summary counts and latest sync state"}},
                }
            },
            "/api/v1/auctions": {
                "get": {
                    "summary": "List auction items",
                    "parameters": list_parameters,
                    "responses": {
                        "200": {
                            "description": "Auction list",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "total": {"type": "integer"},
                                            "count": {"type": "integer"},
                                            "limit": {"type": "integer"},
                                            "offset": {"type": "integer"},
                                            "sort": {"type": "string"},
                                            "has_more": {"type": "boolean"},
                                            "items": {"type": "array", "items": auction_summary},
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/api/v1/auctions/{id}": {
                "get": {
                    "summary": "Get one auction item",
                    "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {
                        "200": {"description": "Auction detail"},
                        "404": {"description": "Not found"},
                    },
                }
            },
            "/api/v1/assets/{id}": {
                "get": {
                    "summary": "Get an auction image asset",
                    "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                    "responses": {
                        "200": {"description": "Image content"},
                        "404": {"description": "Not found"},
                    },
                }
            },
            "/api/v1/documents/{id}": {
                "get": {
                    "summary": "Get a collected court document",
                    "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                    "responses": {
                        "200": {"description": "Court document content"},
                        "404": {"description": "Not found"},
                    },
                }
            },
            "/api/v1/regions": {
                "get": {
                    "summary": "List region buckets",
                    "responses": {"200": {"description": "Region names and counts"}},
                }
            },
            "/api/v1/collector/status": {
                "get": {
                    "summary": "Get collector status",
                    "responses": {"200": {"description": "Collector status and progress"}},
                }
            },
            "/api/v1/collector/start": {
                "post": {
                    "summary": "Start background collector",
                    "responses": {"200": {"description": "Collector start result"}},
                }
            },
            "/api/v1/collector/stop": {
                "post": {
                    "summary": "Stop background collector",
                    "responses": {"200": {"description": "Collector stop result"}},
                }
            },
            "/api/v1/openapi.json": {
                "get": {
                    "summary": "OpenAPI schema",
                    "responses": {"200": {"description": "OpenAPI 3 schema"}},
                }
            },
        },
    }


def collect_log_status(
    log_path: str | Path = "logs/collect-all.log",
    err_path: str | Path = "logs/collect-all.err.log",
    pid_path: str | Path = "data/collect-all.pid",
) -> dict[str, Any]:
    log = Path(log_path)
    err = Path(err_path)
    pid = read_pid(pid_path)
    process_running = process_is_running(pid)
    now = time.time()
    status: dict[str, Any] = {
        "available": log.exists(),
        "state": "unknown",
        "state_label": "상태 확인 중",
        "current": "",
        "last_result": "",
        "last_error": "",
        "progress_current": None,
        "progress_total": None,
        "progress_percent": None,
        "last_log_at": None,
        "seconds_since_log": None,
        "pid": pid,
        "process_running": process_running,
    }

    lines = _tail_lines(log, 160)
    err_lines = _tail_lines(err, 40)
    if err_lines:
        status["last_error"] = err_lines[-1]

    current_line = ""
    result_line = ""
    for line in lines:
        if line.startswith("[") and "/" in line:
            current_line = line
        elif "->" in line:
            result_line = line.strip()

    if current_line:
        status["current"] = current_line
        match = PARTITION_RE.search(current_line)
        if match:
            current = int(match.group(1))
            total = int(match.group(2))
            status["progress_current"] = current
            status["progress_total"] = total
            status["progress_percent"] = round((current - 1) / total * 100, 1) if total else None

    status["last_result"] = result_line

    last_line = lines[-1] if lines else ""

    if log.exists():
        mtime = log.stat().st_mtime
        elapsed = max(0, int(now - mtime))
        status["last_log_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(mtime))
        status["seconds_since_log"] = elapsed
        if process_running:
            status["state"] = "running"
            status["state_label"] = "수집 중"
        elif elapsed <= 180 and pid is None:
            status["state"] = "running"
            status["state_label"] = "수집 중"
        elif current_line:
            status["state"] = "stale"
            status["state_label"] = "최근 로그 없음"
        else:
            status["state"] = "idle"
            status["state_label"] = "대기"
    elif err_lines:
        status["state"] = "error"
        status["state_label"] = "오류"

    if lines and "수집 완료" in lines[-1]:
        status["state"] = "done"
        status["state_label"] = "수집 완료"
        status["current"] = "전체 구간 완료"
        status["last_result"] = lines[-1]
        status["progress_percent"] = 100
    elif "후 재시작" in last_line or "다음 자동 수집까지" in last_line:
        status["state"] = "idle"
        status["state_label"] = "다음 수집 대기"
        status["current"] = "3시간 주기 대기 중"
        status["last_result"] = last_line
        status["progress_percent"] = 100
    elif "자동 수집 중지 요청" in last_line:
        status["state"] = "idle"
        status["state_label"] = "수집 중지"
        status["current"] = "시작 버튼을 누르면 다시 수집합니다"
        status["last_result"] = last_line

    return status


def _tail_lines(path: Path, max_lines: int) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return [
                line.replace("\x00", "").rstrip()
                for line in handle.readlines()[-max_lines:]
                if line.replace("\x00", "").strip()
            ]
    except OSError:
        return []


