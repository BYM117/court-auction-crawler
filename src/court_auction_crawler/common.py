"""여러 프로세스(서버·목록수집·상세수집)가 공유하는 작은 유틸.

utc_now·pid 락·프로세스 생존 확인·자가재시작이 그동안 store/web/cli/detail_crawler에
제각기 복붙돼 있었다. 한 곳으로 모아 중복과 표류를 없앤다."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Iterator
import unicodedata

# 낙찰·취하 등 '더 진행되지 않는' 상태 키워드. 활성 판정과 스크리닝이 공유한다.
TERMINAL_STATUS_KEYWORDS = ("낙찰", "매각", "취하", "기각", "정지", "취소", "종결", "배당")

# launchd KeepAlive가 깨끗한 새 프로세스로 되살리도록 하는 자가재시작 종료 코드.
RESTART_EXIT_CODE = 75


class RateLimitError(Exception):
    """공공데이터포털 일일 트래픽 한도 초과(resultCode 22). 한도 초과를 '데이터 없음'으로
    오분류하면 수천 건이 재시도에서 제외되므로, 백필 루프가 이 예외를 잡아 즉시 멈춘다."""


def is_rate_limited(body: str) -> bool:
    """공공데이터포털 응답 본문이 한도 초과/게이트웨이 에러인지 판별한다.
    한도 초과는 _type=json이어도 게이트웨이가 XML로 돌려주는 경우가 있다."""
    return (
        "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS" in body
        or "<returnReasonCode>22</returnReasonCode>" in body
        or '"resultCode":"22"' in body
        or '"resultCode": "22"' in body
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalized_path(path: str | Path) -> Path:
    """한글 경로를 NFC로 정규화한 Path. macOS는 같은 '경매물건 크롤링'을 폴더마다
    NFC/NFD 어느 쪽으로도 저장하므로(파일 접근은 둘 다 되지만 문자열 비교는 깨진다),
    DB에 적힌 자산 경로와 실행 시 계산한 루트를 비교하기 전에 형태를 맞춰야 한다.
    맥을 옮기면 같은 물건의 사진 경로만 형태가 갈려 서빙이 404로 죽는다."""
    return Path(unicodedata.normalize("NFC", str(path)))


def path_is_within(path: str | Path, root: str | Path) -> bool:
    """path가 root 안에 있는지 정규화 형태 차이에 흔들리지 않게 판정한다."""
    try:
        normalized_path(path).relative_to(normalized_path(root))
    except ValueError:
        return False
    return True


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
