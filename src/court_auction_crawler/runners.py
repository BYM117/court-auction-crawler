"""서버 프로세스가 띄우는 백그라운드 러너들.

- WatchRunner: `serve --watch`용 주기 동기화(단일 프로세스 수집).
- CollectorControlRunner: 목록 수집의 enabled 토글·윈도우 계산·상태 조회.
  실제 수집은 독립 collect-loop 프로세스가 담당한다.
- DbHealthWatchdog: 서버 DB 접근이 좀비화되면 프로세스를 자가재시작.

web.py가 re-export하므로 `from .web import CollectorControlRunner` 등 기존 경로는 유지된다."""
from __future__ import annotations

from pathlib import Path
import threading
import time
from typing import Any

from .common import process_is_running, read_pid, self_restart
from .crawler import collect_sync
from .detail_crawler import collect_details_sync
from .models import SearchOptions, SyncSummary
from .store import AuctionStore


def _date_after(days: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(time.time() + days * 86_400))


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
        self.on_unhealthy = on_unhealthy or self_restart
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
                self.on_unhealthy(
                    "!! DB 접근 불가 지속 -> 서버를 종료합니다. launchd가 재시작합니다"
                )
                return
