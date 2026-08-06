"""수집한 데이터를 웹(객체 스토리지)으로 증분 푸시한다.

크롤러를 클라우드에서 돌리는 대신 맥이 계속 수집하고, 바뀐 것만 웹으로 밀어 올린다.
맥이 꺼져 있어도 웹은 마지막으로 올라간 내용으로 계속 서비스된다.

올리는 것은 세 가지다.
- snapshot: 지도·목록용 요약 한 덩어리(gzip). 매번 통째로 교체한다.
- item: 물건별 상세 JSON(v1 공개 스키마). 페이로드 해시가 바뀐 것만 올린다.
- asset: 사진 원본. sha256이 파일 내용이라 그대로 변경 판정에 쓴다.

무엇을 어떤 내용으로 올렸는지는 web_sync 테이블이 기억한다. 이게 없으면 매 실행마다
사진 12G를 다시 올리게 된다. 중간에 죽어도 다음 실행이 남은 것부터 이어받는다.

업로드 대상은 Uploader로 추상화했다. 계정 없이 LocalDirUploader로 파이프라인 전체를
검증할 수 있고, 나중에 사진만 다른 스토리지로 옮겨도 이 파일 밖은 바뀌지 않는다."""
from __future__ import annotations

from dataclasses import dataclass, field
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from .common import utc_now
from .enrichment import public_auction_detail, public_auction_summary
from .store import AuctionStore

SNAPSHOT_KEY = "v1/snapshot.json.gz"
ITEM_KEY_TEMPLATE = "v1/items/{digest}.json"
ASSET_KEY_TEMPLATE = "v1/assets/{digest}{suffix}"

# 푸시 기록을 몇 건씩 모아 쓸지. 객체마다 쓰기 트랜잭션을 열면 수집 데몬과 락을 다툰다.
MARK_BATCH = 100


class Uploader(Protocol):
    """객체 하나를 올린다. 같은 key면 덮어쓴다."""

    def put(self, key: str, data: bytes, content_type: str) -> None: ...


class LocalDirUploader:
    """로컬 디렉터리에 그대로 쓴다. 계정 없이 파이프라인을 끝까지 검증할 때 쓴다."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def put(self, key: str, data: bytes, content_type: str) -> None:
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


class S3Uploader:
    """R2를 포함한 S3 호환 스토리지. boto3는 여기서만 쓰므로 지연 임포트한다."""

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str = "auto",
    ) -> None:
        try:
            import boto3  # noqa: PLC0415 - 선택 의존성
        except ImportError as exc:  # pragma: no cover - 설치 안내용
            raise RuntimeError(
                "S3/R2 업로드에는 boto3가 필요하다: .venv/bin/pip install boto3"
            ) from exc
        self.bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)


def build_uploader(dest: str, **credentials: Any) -> Uploader:
    """dest 문자열로 업로더를 고른다. local:///경로 또는 s3://버킷."""
    parsed = urlparse(dest)
    if parsed.scheme in ("", "local", "file"):
        return LocalDirUploader(parsed.path or dest)
    if parsed.scheme in ("s3", "r2"):
        return S3Uploader(parsed.netloc, **credentials)
    raise ValueError(f"알 수 없는 업로드 대상: {dest}")


def payload_digest(payload: Any) -> str:
    """페이로드 내용 해시. 이게 web_sync에 남아 다음 실행의 변경 판정 기준이 된다."""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def item_object_key(item_key: str) -> str:
    """물건 키를 그대로 경로에 쓰면 한글·콜론 때문에 스토리지마다 다르게 취급된다.
    해시로 고정해 어디서든 같은 경로가 나오게 한다."""
    digest = hashlib.sha256(item_key.encode("utf-8")).hexdigest()
    return ITEM_KEY_TEMPLATE.format(digest=digest)


def asset_object_key(asset: dict[str, Any]) -> str:
    suffix = Path(str(asset.get("file_path", ""))).suffix.lower()
    return ASSET_KEY_TEMPLATE.format(digest=asset.get("sha256", ""), suffix=suffix)


@dataclass
class PushSummary:
    snapshot_pushed: bool = False
    items_checked: int = 0
    items_pushed: int = 0
    items_skipped: int = 0
    assets_checked: int = 0
    assets_pushed: int = 0
    bytes_pushed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_pushed": self.snapshot_pushed,
            "items_checked": self.items_checked,
            "items_pushed": self.items_pushed,
            "items_skipped": self.items_skipped,
            "assets_checked": self.assets_checked,
            "assets_pushed": self.assets_pushed,
            "bytes_pushed": self.bytes_pushed,
            "errors": self.errors[:20],
        }


def build_snapshot(store: AuctionStore) -> dict[str, Any]:
    """지도·목록용 요약. 활성이고 좌표가 있는 물건만 담는다."""
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = store.list_items(active=True, require_coordinates=True, limit=500, offset=offset)
        rows = page["items"]
        if not rows:
            break
        items.extend(public_auction_summary(row) for row in rows)
        offset += len(rows)
        if offset >= page["total"]:
            break
    return {"generated_at": utc_now(), "total": len(items), "items": items}


def push_snapshot(store: AuctionStore, uploader: Uploader, *, dry_run: bool = False) -> tuple[bool, int]:
    payload = build_snapshot(store)
    digest = payload_digest(payload)
    body = gzip.compress(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    if dry_run:
        return True, len(body)
    uploader.put(SNAPSHOT_KEY, body, "application/gzip")
    store.mark_pushed("snapshot", SNAPSHOT_KEY, hash_value=digest, remote_key=SNAPSHOT_KEY, size=len(body))
    return True, len(body)


def push_items(
    store: AuctionStore,
    uploader: Uploader,
    *,
    limit: int = 500,
    include_inactive: bool = True,
    dry_run: bool = False,
    summary: PushSummary,
    on_progress: Callable[[int, int], None] | None = None,
) -> None:
    candidates = store.pending_item_pushes(limit=limit, include_inactive=include_inactive)
    marks: list[tuple[str, str, str, str, int]] = []
    for index, row in enumerate(candidates, start=1):
        item_key = row["item_key"]
        summary.items_checked += 1
        try:
            item = store.get_item(item_key)
            if item is None:
                continue
            payload = public_auction_detail(item)
            digest = payload_digest(payload)
            key = item_object_key(item_key)
            if digest == row.get("pushed_hash"):
                # 시각만 갱신됐고 내용은 그대로다. 다시 올리지 않되 pushed_at을 밀어
                # 다음 실행에서 이 물건이 또 후보로 잡히지 않게 한다.
                marks.append(("item", item_key, digest, key, 0))
                summary.items_skipped += 1
            else:
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                if not dry_run:
                    uploader.put(key, body, "application/json; charset=utf-8")
                marks.append(("item", item_key, digest, key, len(body)))
                summary.items_pushed += 1
                summary.bytes_pushed += len(body)
        except Exception as exc:  # noqa: BLE001 - 한 건 실패로 전체를 멈추지 않는다
            summary.errors.append(f"item {item_key}: {str(exc)[:120]}")
        if len(marks) >= MARK_BATCH and not dry_run:
            store.mark_pushed_many(marks)
            marks.clear()
        if on_progress:
            on_progress(index, len(candidates))
    if not dry_run:
        store.mark_pushed_many(marks)


def push_assets(
    store: AuctionStore,
    uploader: Uploader,
    *,
    limit: int = 500,
    include_inactive: bool = True,
    dry_run: bool = False,
    summary: PushSummary,
    on_progress: Callable[[int, int], None] | None = None,
) -> None:
    candidates = store.pending_asset_pushes(limit=limit, include_inactive=include_inactive)
    marks: list[tuple[str, str, str, str, int]] = []
    for index, asset in enumerate(candidates, start=1):
        summary.assets_checked += 1
        try:
            path = Path(str(asset.get("file_path", "")))
            if not path.is_file():
                # 파일이 없으면 올릴 게 없다. 재시도해도 같으니 조용히 넘긴다.
                continue
            key = asset_object_key(asset)
            if not dry_run:
                body = path.read_bytes()
                uploader.put(key, body, str(asset.get("content_type") or "application/octet-stream"))
                marks.append(("asset", str(asset["id"]), str(asset.get("sha256", "")), key, len(body)))
                summary.bytes_pushed += len(body)
            summary.assets_pushed += 1
        except Exception as exc:  # noqa: BLE001
            summary.errors.append(f"asset {asset.get('id')}: {str(exc)[:120]}")
        if len(marks) >= MARK_BATCH and not dry_run:
            store.mark_pushed_many(marks)
            marks.clear()
        if on_progress:
            on_progress(index, len(candidates))
    if not dry_run:
        store.mark_pushed_many(marks)


def push_once(
    store: AuctionStore,
    uploader: Uploader,
    *,
    item_limit: int = 500,
    asset_limit: int = 500,
    include_inactive: bool = True,
    skip_snapshot: bool = False,
    skip_assets: bool = False,
    dry_run: bool = False,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> PushSummary:
    """스냅샷·물건·사진을 한 번 밀어 올린다. 중단돼도 다음 실행이 남은 것부터 이어받는다."""
    summary = PushSummary()
    if not skip_snapshot:
        pushed, size = push_snapshot(store, uploader, dry_run=dry_run)
        summary.snapshot_pushed = pushed
        summary.bytes_pushed += size
    push_items(
        store,
        uploader,
        limit=item_limit,
        include_inactive=include_inactive,
        dry_run=dry_run,
        summary=summary,
        on_progress=(lambda i, n: on_progress("item", i, n)) if on_progress else None,
    )
    if not skip_assets:
        push_assets(
            store,
            uploader,
            limit=asset_limit,
            include_inactive=include_inactive,
            dry_run=dry_run,
            summary=summary,
            on_progress=(lambda i, n: on_progress("asset", i, n)) if on_progress else None,
        )
    return summary
