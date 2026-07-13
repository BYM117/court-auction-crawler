from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
import re
import signal
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .crawler import collect_sync
from .detail_crawler import collect_details_sync
from .geocoder import geocode_address
from .models import SearchOptions, SyncSummary
from .store import AuctionStore


STATIC_DIR = Path(__file__).parent / "web_static"
PARTITION_RE = re.compile(r"\[(\d+)/(\d+)\]\s+(.+)")
BRACKET_RE = re.compile(r"\[([^\]]+)]")
PAREN_RE = re.compile(r"\(([^)]*)\)")
MONEY_RE = re.compile(r"[\d,]+")
PERCENT_RE = re.compile(r"\((\d+(?:\.\d+)?)%\)")
AREA_RE = re.compile(r"([\d,.]+)\s*㎡")
FAIL_COUNT_RE = re.compile(r"유찰\s*(\d+)")
LOT_NUMBER_RE = re.compile(r"(?:산\s*)?\d+(?:-\d+)?")
TERMINAL_STATUS_KEYWORDS = ("낙찰", "매각", "취하", "기각", "정지", "취소", "종결", "배당")
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
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self.last_error = ""

    @property
    def enabled(self) -> bool:
        return self.enabled_path.exists()

    @property
    def running(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    def start(self) -> bool:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.enabled_path.write_text("enabled\n", encoding="utf-8")
        self._stop.clear()
        if self._thread and self._thread.is_alive():
            return False
        self._thread = threading.Thread(target=self._loop, name="auction-collector-control", daemon=True)
        self._thread.start()
        return True

    def start_if_enabled(self) -> None:
        if self.enabled:
            self.start()

    def stop(self) -> bool:
        was_enabled = self.enabled
        self._stop.set()
        try:
            self.enabled_path.unlink()
        except FileNotFoundError:
            pass
        self._terminate_process()
        self.last_error = ""
        self._write_log("===== 자동 수집 중지 요청 =====")
        return was_enabled or self.running

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

    def shutdown(self) -> None:
        self._stop.set()
        self._terminate_process()

    def _loop(self) -> None:
        while self.enabled and not self._stop.is_set():
            with self._lock:
                if not self.enabled or self._stop.is_set():
                    break
                self._run_once()
            if not self.enabled or self._stop.is_set():
                break
            self._write_log(f"===== 다음 자동 수집까지 {self.interval_seconds}초 대기 =====")
            self._stop.wait(self.interval_seconds)

    def _run_once(self) -> None:
        run_kind = self._next_run_kind()
        window = self._collection_window(run_kind)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.err_path.parent.mkdir(parents=True, exist_ok=True)
        self.err_path.write_text("", encoding="utf-8")
        self._write_log(
            f"===== 자동 수집 시작 {time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"mode={run_kind} current={window['current_start']}~{window['current_end']} "
            f"scheduled={window['scheduled_start']}~{window['scheduled_end']} ====="
        )

        command = self._collect_command(window)
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONPATH"] = "src"
        env.setdefault("PLAYWRIGHT_BROWSERS_PATH", ".playwright-browsers")

        with self.log_path.open("a", encoding="utf-8") as log, self.err_path.open("a", encoding="utf-8") as err:
            self._process = subprocess.Popen(command, cwd=self.project_root, env=env, stdout=log, stderr=err)
            self.pid_path.write_text(f"{self._process.pid}\n", encoding="utf-8")
            exit_code = self._process.wait()
        self._process = None
        try:
            self.pid_path.unlink()
        except FileNotFoundError:
            pass

        if exit_code == 0:
            self.last_error = ""
            if run_kind == "full":
                self.last_full_path.write_text(str(time.time()), encoding="utf-8")
            try:
                lifecycle = self.store.apply_lifecycle()
                self._write_log(
                    f"===== 생명주기 정리: 활성 {lifecycle['checked']}개 중 "
                    f"{lifecycle['deactivated']}개 종결 처리 ====="
                )
            except Exception as exc:
                self._write_log(f"===== 생명주기 정리 실패: {exc} =====")
        else:
            self.last_error = f"수집 프로세스 종료 코드 {exit_code}"
        self._write_log(
            f"===== 자동 수집 종료 {time.strftime('%Y-%m-%d %H:%M:%S')} exit={exit_code}; "
            f"{self.interval_seconds}초 후 재시작 ====="
        )

    def _collect_command(self, window: dict[str, str]) -> list[str]:
        return [
            sys.executable,
            "-m",
            "court_auction_crawler.cli",
            "collect-all",
            "--db",
            str(self.store.db_path),
            "--mode",
            "both",
            "--current-start-date",
            window["current_start"],
            "--current-end-date",
            window["current_end"],
            "--scheduled-start-date",
            window["scheduled_start"],
            "--scheduled-end-date",
            window["scheduled_end"],
            "--date-chunk-days",
            "31",
            "--max-pages",
            "50",
            "--delay",
            "2.0",
            "--geocode-limit",
            "2000",
            # 상세 수집은 전용 프로세스(run_detail_collector.sh)가 담당한다.
            # 여기서 --details를 붙이면 3시간 주기가 상세 작업에 막혀 목록
            # 신선도가 무너지고, 전용 수집기와 이중 실행 충돌이 난다.
        ]

    def _collection_window(self, run_kind: str) -> dict[str, str]:
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

    def _next_run_kind(self) -> str:
        try:
            last_full = float(self.last_full_path.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError):
            return "full"
        if time.time() - last_full >= self.full_interval_seconds:
            return "full"
        return "quick"

    def _terminate_process(self) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        try:
            self.pid_path.unlink()
        except FileNotFoundError:
            pass

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


def run_server(
    store: AuctionStore,
    host: str,
    port: int,
    runner: WatchRunner | None = None,
) -> None:
    collector_runner = CollectorControlRunner(store)
    collector_runner.start_if_enabled()
    handler = type(
        "ConfiguredAuctionWebHandler",
        (AuctionWebHandler,),
        {"store": store, "runner": runner, "collector_runner": collector_runner},
    )
    server = ThreadingHTTPServer((host, port), handler)
    if runner:
        runner.start()
    previous_sigint = signal.getsignal(signal.SIGINT)
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def handle_shutdown_signal(signum: int, _frame: Any) -> None:
        if runner:
            runner.stop()
        collector_runner.shutdown()
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, handle_shutdown_signal)
    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    try:
        print(f"웹 대시보드: http://{host}:{port}")
        server.serve_forever()
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
        if runner:
            runner.stop()
        collector_runner.shutdown()
        server.server_close()


def allowed_cors_origin(origin: str) -> str:
    configured = os.environ.get("AUCTION_API_ALLOWED_ORIGINS", "")
    allowed = {value.strip() for value in configured.split(",") if value.strip()} or DEFAULT_ALLOWED_ORIGINS
    if "*" in allowed:
        return "*"
    return origin if origin in allowed else ""


def safe_external_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return text


def control_api_key_valid(expected: str, provided: str, authorization: str) -> bool:
    expected = str(expected or "").strip()
    if not expected:
        return True
    token = str(provided or "").strip()
    auth = str(authorization or "").strip()
    if not token and auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    return hmac.compare_digest(token, expected)


def public_auction_list(payload: dict[str, Any]) -> dict[str, Any]:
    items = [public_auction_summary(item) for item in payload.get("items", [])]
    total = int(payload.get("total", 0))
    offset = int(payload.get("offset", 0))
    limit = int(payload.get("limit", len(items)))
    return {
        "total": total,
        "count": len(items),
        "limit": limit,
        "offset": offset,
        "sort": payload.get("sort", "last_seen_desc"),
        "items": items,
        "has_more": offset + len(items) < total,
    }


def public_stats(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": __version__,
        "total": payload.get("total", 0),
        "active": payload.get("active", 0),
        "due": payload.get("due", 0),
        "by_detail_status": payload.get("by_detail_status", []),
        "by_document_status": payload.get("by_document_status", []),
        "asset_count": payload.get("asset_count", 0),
        "latest_sync": payload.get("latest_sync"),
        "by_source": payload.get("by_source", []),
        "by_status": payload.get("by_status", []),
    }


def public_auction_summary(item: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "id": item.get("item_key", ""),
        "source": item.get("source", ""),
        "case_no": item.get("case_no", ""),
        "item_no": item.get("item_no", ""),
        "court": item.get("court", ""),
        "address": item.get("address", ""),
        "category": item.get("category", ""),
        "appraisal": item.get("appraisal", ""),
        "minimum_bid": item.get("minimum_bid", ""),
        "sale_date": item.get("sale_date", ""),
        "status": item.get("status", ""),
        "detail_url": safe_external_url(item.get("detail_url", "")),
        "lat": parse_optional_float(item.get("lat")),
        "lng": parse_optional_float(item.get("lng")),
        "pnu": item.get("pnu", ""),
        "coordinate_source": item.get("coordinate_source", ""),
        "coordinate_quality": item.get("coordinate_quality", "missing"),
        "normalized_address": item.get("normalized_address", ""),
        "geocoded_at": item.get("geocoded_at", ""),
        "last_seen_at": item.get("last_seen_at", ""),
        "updated_at": item.get("updated_at", ""),
        "detail_status": item.get("detail_status", "pending"),
        "detail_collected_at": item.get("detail_collected_at", ""),
    }
    summary.update(public_auction_enrichment(item))
    return summary


def build_official_price(item: dict[str, Any]) -> dict[str, Any] | None:
    """사전 계산해 DB에 저장한 공시기준가를 공개 스키마로 내보낸다. 없으면 None."""
    value = parse_optional_float(item.get("official_price"))
    if not value or value <= 0:
        return None
    detail_raw = item.get("official_price_detail", "")
    try:
        detail = json.loads(detail_raw) if detail_raw else {}
    except (ValueError, TypeError):
        detail = {}
    return {
        "value": value,
        "type": item.get("official_price_type", ""),
        "year": item.get("official_price_year", ""),
        "detail": detail,
    }


def public_auction_enrichment(item: dict[str, Any]) -> dict[str, Any]:
    appraisal = parse_first_money(item.get("appraisal"))
    minimum_bid = parse_first_money(item.get("minimum_bid"))
    minimum_bid_percent = parse_bid_percent(item.get("minimum_bid"), appraisal, minimum_bid)
    address_info = parse_property_address(item.get("address", ""))
    fail_count = parse_fail_count(item.get("status", ""))
    active = parse_item_active(item)
    screening = build_screening(item, appraisal, minimum_bid, minimum_bid_percent, address_info, fail_count)

    return {
        "case": {
            "court": item.get("court", ""),
            "case_no": normalize_case_number(item.get("case_no", ""), item.get("court", "")),
            "display_case_no": item.get("case_no", ""),
            "item_no": item.get("item_no", ""),
            "source": item.get("source", ""),
        },
        "auction": {
            "sale_date": normalize_date_text(item.get("sale_date", "")),
            "sale_date_raw": item.get("sale_date", ""),
            "status": item.get("status", ""),
            "fail_count": fail_count,
            "is_active": active,
            "detail_url": safe_external_url(item.get("detail_url", "")),
        },
        "property": {
            "category": item.get("category", ""),
            "type_guess": infer_property_type(item.get("category", ""), item.get("address", "")),
            "address": address_info,
            "area": parse_area_info(address_info),
            "share": parse_share_info(address_info),
            "registry_search_hint": build_registry_search_hint(address_info, item.get("category", "")),
        },
        "price": {
            "appraisal": appraisal,
            "minimum_bid": minimum_bid,
            "minimum_bid_rate": round(minimum_bid_percent / 100, 4) if minimum_bid_percent is not None else None,
            "minimum_bid_percent": minimum_bid_percent,
            "official": build_official_price(item),
            "raw": {
                "appraisal": item.get("appraisal", ""),
                "minimum_bid": item.get("minimum_bid", ""),
            },
        },
        "map": {
            "lat": parse_optional_float(item.get("lat")),
            "lng": parse_optional_float(item.get("lng")),
            "pnu": item.get("pnu", ""),
            "coordinate_source": item.get("coordinate_source", "") if item.get("lat") and item.get("lng") else "none",
            "coordinate_quality": item.get("coordinate_quality", "") if item.get("lat") and item.get("lng") else "missing",
            "normalized_address": item.get("normalized_address", ""),
            "geocoded_at": item.get("geocoded_at", ""),
        },
        "screening": screening,
    }


def normalize_case_number(case_no: str, court: str) -> str:
    text = str(case_no or "").strip()
    court_text = str(court or "").strip()
    if court_text and text.startswith(court_text):
        return text.removeprefix(court_text).strip()
    return text


def parse_item_active(item: dict[str, Any]) -> bool:
    value = item.get("is_active")
    if value is not None:
        return bool(value)
    status = str(item.get("status", ""))
    return not any(keyword in status for keyword in TERMINAL_STATUS_KEYWORDS)


def parse_first_money(value: Any) -> int | None:
    match = MONEY_RE.search(str(value or ""))
    if not match:
        return None
    return int(match.group(0).replace(",", ""))


def parse_bid_percent(raw_value: Any, appraisal: int | None, minimum_bid: int | None) -> float | None:
    match = PERCENT_RE.search(str(raw_value or ""))
    if match:
        return round(float(match.group(1)), 2)
    if appraisal and minimum_bid:
        return round(minimum_bid / appraisal * 100, 2)
    return None


def parse_fail_count(status: str) -> int:
    match = FAIL_COUNT_RE.search(str(status or ""))
    return int(match.group(1)) if match else 0


def normalize_date_text(value: Any) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", text):
        return text.replace(".", "-")
    if re.fullmatch(r"\d{4}/\d{2}/\d{2}", text):
        return text.replace("/", "-")
    return text


def parse_property_address(address: str) -> dict[str, Any]:
    raw = str(address or "").strip()
    bracket_parts = [part.strip() for part in BRACKET_RE.findall(raw) if part.strip()]
    clean = BRACKET_RE.sub(" ", raw)
    building_hint = ""
    for part in PAREN_RE.findall(clean):
        if "," in part or any(word in part for word in ("동", "아파트", "빌라", "오피스텔")):
            building_hint = part.split(",")[-1].strip()
    clean = re.sub(r"\s+", " ", clean).strip()
    tokens = clean.split()
    sido = tokens[0] if tokens else ""
    sigungu = next((token for token in tokens[1:] if token.endswith(("시", "군", "구"))), "")
    eup_myeon_dong = next(
        (
            token
            for token in tokens[1:]
            if token.endswith(("읍", "면", "동", "리")) and not is_unit_dong_token(token)
        ),
        "",
    )
    lot_number = find_lot_number(clean) or find_lot_number(" ".join(bracket_parts))

    return {
        "raw": raw,
        "clean": clean,
        "detail": " ".join(bracket_parts),
        "sido": sido,
        "sigungu": sigungu,
        "eup_myeon_dong": eup_myeon_dong,
        "lot_number": lot_number,
        "building_name": building_hint,
        "dong": find_unit_part(raw, "동"),
        "ho": find_unit_part(raw, "호"),
    }


def find_lot_number(value: str) -> str:
    matches = re.findall(
        r"(?:[가-힣A-Za-z0-9]+(?:읍|면|동|리))\s+((?:산\s*)?\d+(?:-\d+)?)(?=$|\s|[,)\]])",
        str(value or ""),
    )
    return matches[-1].replace(" ", "") if matches else ""


def is_unit_dong_token(value: str) -> bool:
    token = str(value or "").strip(" ,./")
    return bool(re.fullmatch(r"(?:제)?(?:\d+[A-Za-z]?|[A-Za-z]|[가-힣])동", token))


def find_unit_part(value: str, suffix: str) -> str:
    for token in re.split(r"\s+", str(value or "")):
        token = token.strip(" ,./()[]")
        if suffix == "동" and is_unit_dong_token(token):
            return token
        if suffix == "호" and re.fullmatch(r"(?:제)?\d+[A-Za-z]?호", token):
            return token
    return ""


def parse_area_info(address_info: dict[str, Any]) -> dict[str, Any]:
    detail = str(address_info.get("detail") or "")
    raw = str(address_info.get("raw") or "")
    area_text = detail or raw
    values = [float(value.replace(",", "")) for value in AREA_RE.findall(area_text)]
    land_values = [float(value.replace(",", "")) for value in AREA_RE.findall(detail) if "토지" in detail[: detail.find(value) + len(value) + 2]]
    building_values = [
        float(value.replace(",", ""))
        for value in AREA_RE.findall(detail)
        if any(word in detail[: detail.find(value) + len(value) + 2] for word in ("건물", "집합건물", "층", "구조"))
    ]
    return {
        "raw": area_text,
        "total_sqm": round(sum(values), 2) if values else None,
        "land_sqm": round(sum(land_values), 2) if land_values else None,
        "building_sqm": round(sum(building_values), 2) if building_values else None,
        "values_sqm": values,
    }


def parse_share_info(address_info: dict[str, Any]) -> dict[str, Any]:
    text = f"{address_info.get('raw', '')} {address_info.get('detail', '')}"
    is_share = any(word in text for word in ("지분", "공유자", "분의"))
    fraction = ""
    match = re.search(r"(\d+)\s*분의\s*(\d+)", text)
    if match:
        fraction = f"{match.group(2)}/{match.group(1)}"
    return {
        "is_share_sale": is_share,
        "fraction": fraction,
        "raw": text if is_share else "",
    }


def build_registry_search_hint(address_info: dict[str, Any], category: str) -> dict[str, Any]:
    type_guess = infer_registry_realty_type(category, address_info.get("detail", ""))
    return {
        "realty_type_guess": type_guess,
        "address_for_search": address_info.get("clean", ""),
        "sido": address_info.get("sido", ""),
        "sigungu": address_info.get("sigungu", ""),
        "eup_myeon_dong": address_info.get("eup_myeon_dong", ""),
        "lot_number": address_info.get("lot_number", ""),
        "building_name": address_info.get("building_name", ""),
        "dong": address_info.get("dong", ""),
        "ho": address_info.get("ho", ""),
    }


def infer_property_type(category: str, address: str) -> str:
    text = f"{category} {address}"
    if "오피스텔" in text:
        return "오피스텔"
    if "아파트" in text:
        return "아파트"
    if any(word in text for word in ("다세대", "연립", "빌라")):
        return "빌라"
    if any(word in text for word in ("상가", "근린", "점포")):
        return "상가"
    if any(word in text for word in ("임야", "전 ", "답 ", "도로", "대지", "토지")):
        return "토지"
    if any(word in text for word in ("단독주택", "주택")):
        return "단독주택"
    return str(category or "").strip() or "기타"


def infer_registry_realty_type(category: str, detail: str) -> str:
    text = f"{category} {detail}"
    has_land = any(word in text for word in ("토지", "임야", "대지", "전 ", "답 ", "도로"))
    has_building = any(word in text for word in ("건물", "집합건물", "아파트", "오피스텔", "주택", "상가"))
    if "집합건물" in text or any(word in text for word in ("아파트", "오피스텔", "다세대", "연립")):
        return "집합건물"
    if has_land and has_building:
        return "토지+건물"
    if has_land:
        return "토지"
    if has_building:
        return "건물"
    return "확인 필요"


def build_screening(
    item: dict[str, Any],
    appraisal: int | None,
    minimum_bid: int | None,
    minimum_bid_percent: float | None,
    address_info: dict[str, Any],
    fail_count: int,
) -> dict[str, Any]:
    flags: list[str] = []
    score = 50
    if fail_count:
        flags.append(f"유찰 {fail_count}회")
        score -= min(fail_count * 4, 24)
    if minimum_bid_percent is not None:
        flags.append(f"최저가율 {minimum_bid_percent:g}%")
        if minimum_bid_percent <= 30:
            score -= 12
        elif minimum_bid_percent <= 50:
            score -= 6
    share = parse_share_info(address_info)
    if share["is_share_sale"]:
        flags.append("지분 매각 의심")
        score -= 15
    if not address_info.get("clean"):
        flags.append("주소 확인 필요")
        score -= 10
    if not appraisal or not minimum_bid:
        flags.append("가격 정보 확인 필요")
        score -= 8
    if any(keyword in str(item.get("status", "")) for keyword in TERMINAL_STATUS_KEYWORDS):
        flags.append("종료성 상태")
        score -= 20
    flags.append("권리확인 필요")
    risk_level = "낮음" if score >= 65 else "보통" if score >= 40 else "높음"
    return {
        "score": max(0, min(100, score)),
        "risk_level": risk_level,
        "flags": flags,
    }


def public_auction_detail(item: dict[str, Any]) -> dict[str, Any]:
    summary = public_auction_summary(item)
    summary.update(
        {
            "first_seen_at": item.get("first_seen_at", ""),
            "last_changed_at": item.get("last_changed_at", ""),
            "next_check_at": item.get("next_check_at", ""),
            "is_active": bool(item.get("is_active")),
            "crawl_priority": item.get("crawl_priority", 0),
            "raw": item.get("raw", {}),
            "detail": item.get("detail", {}),
            "events": item.get("events", []),
            "detail_collection": {
                "status": item.get("detail_status", "pending"),
                "collected_at": item.get("detail_collected_at", ""),
                "checked_at": item.get("detail_checked_at", ""),
                "next_retry_at": item.get("detail_next_retry_at", ""),
                "fail_count": item.get("detail_fail_count", 0),
                "error": item.get("detail_error", ""),
            },
            "documents": [
                {
                    **document,
                    "url": (
                        f"/api/v1/documents/{document.get('id')}"
                        if document.get("status") == "collected" and document.get("file_size")
                        else ""
                    ),
                }
                for document in item.get("documents", [])
            ],
            "assets": [
                {**asset, "url": f"/api/v1/assets/{asset.get('id')}"}
                for asset in item.get("assets", [])
            ],
        }
    )
    return summary


def parse_optional_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_optional_bool(value: str) -> bool | None:
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized in {"1", "true", "yes", "y", "on", "활성"}:
        return True
    if normalized in {"0", "false", "no", "n", "off", "비활성"}:
        return False
    return None


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


def read_pid(path: str | Path) -> int | None:
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def process_is_running(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
