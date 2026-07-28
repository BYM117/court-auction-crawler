"""여러 프로세스(서버·목록수집·상세수집)가 공유하는 작은 유틸.

utc_now·pid 락·프로세스 생존 확인·자가재시작이 그동안 store/web/cli/detail_crawler에
제각기 복붙돼 있었다. 한 곳으로 모아 중복과 표류를 없앤다."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Iterator

# 낙찰·취하 등 '더 진행되지 않는' 상태 키워드. 활성 판정과 스크리닝이 공유한다.
TERMINAL_STATUS_KEYWORDS = ("낙찰", "매각", "취하", "기각", "정지", "취소", "종결", "배당")

# launchd KeepAlive가 깨끗한 새 프로세스로 되살리도록 하는 자가재시작 종료 코드.
RESTART_EXIT_CODE = 75


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


@contextmanager
def singleton_lock(lock_path: str | Path) -> Iterator[bool]:
    """pid 파일 기반 단일 실행 보장. 이미 살아있는 다른 프로세스가 잡고 있으면
    False를 yield하고, 아니면 내 pid를 기록하고 True를 yield한다. 종료 시 내 것이면
    파일을 지운다. 목록/상세 수집 데몬이 공유하며, 이 pid는 상태 조회에도 쓰인다."""
    lock_path = Path(lock_path)
    existing = read_pid(lock_path)
    if existing and existing != os.getpid() and process_is_running(existing):
        yield False
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    try:
        yield True
    finally:
        if read_pid(lock_path) == os.getpid():
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass


def self_restart(message: str = "") -> None:
    """프로세스를 자가재시작 코드로 즉시 종료한다. launchd가 깨끗하게 되살린다.
    좀비(DB 핸들 깨짐)·정체 등 in-process로 회복 불가한 상태의 최후 수단."""
    if message:
        print(message, flush=True)
    os._exit(RESTART_EXIT_CODE)
