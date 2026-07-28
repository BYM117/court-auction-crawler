from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
import re
import signal
from pathlib import Path
import threading
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .common import process_is_running, read_pid
from .crawler import collect_sync
from .detail_crawler import collect_details_sync
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
from .models import SearchOptions, SyncSummary
from .store import AuctionStore


STATIC_DIR = Path(__file__).parent / "web_static"
PARTITION_RE = re.compile(r"\[(\d+)/(\d+)\]\s+(.+)")
DEFAULT_ALLOWED_ORIGINS = {
    "http://127.0.0.1:4173",
    "http://localhost:4173",
}


class WatchRunner:
    def __init__(
        self,
        store: AuctionStore,
        options: SearchOptions,
        interval_seconds: int,
        collect_details: bool = False,
    ) -> None:
        self.store = store
        self.options = options
        self.collect_details = collect_details
        self.interval_seconds = max(interval_seconds, 60)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.is_running = False
        self.last_error = ""

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="auction-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def trigger_once(self) -> bool:
        if self._lock.locked():
            return False
        threading.Thread(target=self.sync_once, name="auction-sync-once", daemon=True).start()
        return True

    def sync_once(self) -> SyncSummary:
        if not self._lock.acquire(blocking=False):
            return SyncSummary(message="이미 동기화가 진행 중입니다.")
        run_id = self.store.start_sync()
        summary = SyncSummary()
        self.is_running = True
        self.last_error = ""
        try:
            items = collect_sync(self.options)
            result = self.store.upsert_items(items)
            if self.collect_details and items:
                collect_details_sync(self.store, limit=len(items), delay=self.options.delay)
            summary = SyncSummary(
                collected=len(items),
                inserted=result.inserted,
                updated=result.updated,
                unchanged=result.unchanged,
                message="동기화 완료",
            )
            self.store.finish_sync(run_id, "success", summary)
            return summary
        except Exception as exc:
            summary.failed = 1
            summary.message = "동기화 실패"
            self.last_error = str(exc)
            self.store.finish_sync(run_id, "failed", summary, str(exc))
            return summary
        finally:
            self.is_running = False
            self._lock.release()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.sync_once()
            self._stop.wait(self.interval_seconds)


class CollectorControlRunner:
    # 과거 기일 검색은 지난 회차 이력만 돌려줘서(후퇴 가드가 무시) 수집 시간만 태운다.
    # 물건상세검색(진행)은 사이트가 '오늘~2주 후 기일'까지만 검색을 허용하므로
    # current 윈도우는 13일이 상한이고, 그 너머의 미래는 매각예정물건 검색이 커버한다.
    def __init__(
        self,
        store: AuctionStore,
        interval_seconds: int = 10_800,
        quick_current_days_before: int = 0,
        quick_current_days_ahead: int = 13,
        quick_scheduled_days_ahead: int = 60,
        full_current_days_before: int = 0,
        full_current_days_ahead: int = 13,
        full_scheduled_days_ahead: int = 180,
        full_interval_seconds: int = 86_400,
    ) -> None:
        self.store = store
        self.interval_seconds = max(interval_seconds, 300)
        self.quick_current_days_before = max(quick_current_days_before, 0)
        self.quick_current_days_ahead = max(quick_current_days_ahead, 1)
        self.quick_scheduled_days_ahead = max(quick_scheduled_days_ahead, 1)
        self.full_current_days_before = max(full_current_days_before, self.quick_current_days_before)
        self.full_current_days_ahead = max(full_current_days_ahead, self.quick_current_days_ahead)
        self.full_scheduled_days_ahead = max(full_scheduled_days_ahead, self.quick_scheduled_days_ahead)
        self.full_interval_seconds = max(full_interval_seconds, self.interval_seconds)
        self.project_root = Path.cwd()
        self.data_dir = store.db_path.parent
        self.enabled_path = self.data_dir / "collector.enabled"
        self.last_full_path = self.data_dir / "collector.last_full"
        self.pid_path = self.data_dir / "collect-all.pid"
        self.log_path = self.project_root / "logs" / "collect-all.log"
        self.err_path = self.project_root / "logs" / "collect-all.err.log"
        self.last_error = ""

    @property
    def enabled(self) -> bool:
        return self.enabled_path.exists()

    @property
    def running(self) -> bool:
        # 실제 수집은 독립 프로세스(collect-loop)가 담당한다. 서버는 pid 파일로
        # 그 프로세스 생존만 확인한다.
        return process_is_running(read_pid(self.pid_path))

    def start(self) -> bool:
        # 서버는 '수집 원함' 의도만 기록한다(enabled 파일). 실제 수집 루프는
        # launchd가 항상 띄워두는 collect-loop 프로세스가 이 파일을 보고 돈다.
        self.data_dir.mkdir(parents=True, exist_ok=True)
        was_enabled = self.enabled
        self.enabled_path.write_text("enabled\n", encoding="utf-8")
        if not was_enabled:
            self._write_log("===== 자동 수집 시작 요청 =====")
        return not was_enabled

    def stop(self) -> bool:
        was_enabled = self.enabled
        try:
            self.enabled_path.unlink()
        except FileNotFoundError:
            pass
        self._write_log("===== 자동 수집 중지 요청 =====")
        return was_enabled

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": self.running,
            "interval_seconds": self.interval_seconds,
            "quick_current_days_before": self.quick_current_days_before,
            "quick_current_days_ahead": self.quick_current_days_ahead,
            "quick_scheduled_days_ahead": self.quick_scheduled_days_ahead,
            "full_current_days_before": self.full_current_days_before,
            "full_current_days_ahead": self.full_current_days_ahead,
            "full_scheduled_days_ahead": self.full_scheduled_days_ahead,
            "last_error": self.last_error,
        }

    def collection_window(self, run_kind: str) -> dict[str, str]:
        if run_kind == "full":
            current_before = self.full_current_days_before
            current_ahead = self.full_current_days_ahead
            scheduled_ahead = self.full_scheduled_days_ahead
        else:
            current_before = self.quick_current_days_before
            current_ahead = self.quick_current_days_ahead
            scheduled_ahead = self.quick_scheduled_days_ahead
        return {
            "current_start": _date_after(-current_before),
            "current_end": _date_after(current_ahead),
            "scheduled_start": _date_after(0),
            "scheduled_end": _date_after(scheduled_ahead),
        }

    def next_run_kind(self) -> str:
        try:
            last_full = float(self.last_full_path.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError):
            return "full"
        if time.time() - last_full >= self.full_interval_seconds:
            return "full"
        return "quick"

    def record_full_run(self) -> None:
        self.last_full_path.write_text(str(time.time()), encoding="utf-8")

    def _write_log(self, line: str) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


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


class DbHealthWatchdog:
    """서버 프로세스의 DB 접근이 살아있는지 주기적으로 확인한다.

    맥 잠자기에서 깨어나면 오래된 서버 프로세스의 SQLite 파일 핸들이 깨져
    'unable to open database file'이 나고, 이 상태로는 재시작 전까지 회복되지
    않는다(실측: 매일 재발). 연속 실패가 임계치를 넘으면 프로세스를 종료해
    launchd KeepAlive가 깨끗한 새 프로세스로 되살리게 한다."""

    def __init__(
        self,
        store: AuctionStore,
        *,
        interval_seconds: float = 60.0,
        fail_limit: int = 3,
        on_unhealthy: Any = None,
    ) -> None:
        self.store = store
        self.interval_seconds = interval_seconds
        self.fail_limit = fail_limit
        self.on_unhealthy = on_unhealthy or (lambda: os._exit(75))
        self.consecutive_failures = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def check_once(self) -> bool:
        try:
            self.store.healthcheck()
        except Exception as exc:
            self.consecutive_failures += 1
            print(
                f"!! DB 헬스체크 실패 {self.consecutive_failures}/{self.fail_limit}: "
                f"{str(exc)[:150]}",
                flush=True,
            )
            return False
        if self.consecutive_failures:
            print("== DB 헬스체크 회복 ==", flush=True)
        self.consecutive_failures = 0
        return True

    def should_abort(self) -> bool:
        return self.consecutive_failures >= self.fail_limit

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="db-health-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self.check_once()
            if self.should_abort():
                print(
                    "!! DB 접근 불가 지속 -> 서버를 종료합니다. launchd가 재시작합니다",
                    flush=True,
                )
                self.on_unhealthy()
                return


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


def _date_after(days: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(time.time() + days * 86_400))


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


