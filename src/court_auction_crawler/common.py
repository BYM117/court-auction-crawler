"""여러 프로세스(서버·목록수집·상세수집)가 공유하는 작은 유틸.

utc_now·pid 락·프로세스 생존 확인·자가재시작이 그동안 store/web/cli/detail_crawler에
제각기 복붙돼 있었다. 한 곳으로 모아 중복과 표류를 없앤다."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import mimetypes
import os
from pathlib import Path
import re
from typing import Iterator
import unicodedata

# 낙찰·취하 등 '더 진행되지 않는' 상태 키워드. 활성 판정과 스크리닝이 공유한다.
TERMINAL_STATUS_KEYWORDS = ("낙찰", "매각", "취하", "기각", "정지", "취소", "종결", "배당")

# 사건번호. store(대표번호 추출)와 detail_crawler(법원 검색창 입력)가 함께 쓴다.
# 두 곳에 따로 두면 한쪽만 고쳐 반만 낫는다(실제로 그랬다).
#
# 중복사건은 '2026타경100160 2026타경100346(중복)'처럼 여러 개가 붙어 오는데
# 사이트가 공백 없이 줄 때가 있다. \d+ 를 그냥 두면 뒤 사건의 연도까지 삼켜
# '2026타경1001602026' 이라는 없는 번호가 만들어진다(실측 66건). 뒤에 또
# '연도+타경'이 이어지면 거기서 멈춘다.
CASE_NO_RE = re.compile(r"(?P<year>\d{4})타경(?P<number>\d+?)(?=\d{4}타경|\D|$)")

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


# integrity_check 출력 중 REINDEX로 안전하게 고쳐지는 손상. 인덱스는 테이블 데이터에서
# 다시 만들어지므로 이 부류는 자동 복구해도 잃을 게 없다. 페이지·테이블 손상은 여기 없다.
INDEX_ONLY_PROBLEM_RE = re.compile(
    r"(?:missing from index|wrong # of entries in index|non-unique entry in index)\s+(\S+)"
)


def index_problems(problems: list[str]) -> list[str] | None:
    """integrity_check 결과가 '인덱스 한정 손상'이면 재생성할 인덱스 이름들을 돌려준다.
    한 줄이라도 그 밖의 손상(페이지·테이블 등)이 섞여 있으면 None — 자동 복구하면 안 된다."""
    names: list[str] = []
    for line in problems:
        match = INDEX_ONLY_PROBLEM_RE.search(line)
        if not match:
            return None
        name = match.group(1)
        if name not in names:
            names.append(name)
    return names


def asset_object_name(sha256: str, content_type: str) -> str:
    """객체 스토리지에 올릴 사진 파일 이름. 내용 해시가 곧 이름이라 같은 사진은 한 번만
    올라간다.

    올리는 쪽(push-web)과 공개 payload에 이름을 싣는 쪽(enrichment)이 반드시 같은
    문자열을 만들어야 웹이 사진을 찾는다. 그래서 한 함수로 모았다. 확장자를 파일 경로가
    아니라 content_type에서 뽑는 이유는, 공개 payload에는 서버 내부 파일 경로를 싣지
    않기 때문이다(양쪽 모두 가진 정보로만 만들어야 한다)."""
    digest = str(sha256 or "").strip()
    if not digest:
        return ""
    suffix = mimetypes.guess_extension(str(content_type or "").split(";", 1)[0].strip() or "") or ""
    if suffix == ".jpe":  # mimetypes가 image/jpeg에 대해 내주는 값이 환경마다 갈린다
        suffix = ".jpg"
    return f"{digest}{suffix}"


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
