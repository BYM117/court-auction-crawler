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

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, timedelta
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol
from urllib.parse import urlparse

from .common import asset_object_name, utc_now
from .enrichment import public_auction_detail, public_auction_summary
from .store import AuctionStore

SNAPSHOT_KEY = "v1/snapshot.json.gz"
SOLD_SNAPSHOT_KEY = "v1/sold.json.gz"
ITEM_KEY_TEMPLATE = "v1/items/{digest}.json"
ASSET_KEY_TEMPLATE = "v1/assets/{digest}{suffix}"

# 푸시 기록을 몇 건씩 모아 쓸지. 객체마다 쓰기 트랜잭션을 열면 수집 데몬과 락을 다툰다.
MARK_BATCH = 100
# 동시 업로드 수. 한 건씩 올리면 왕복 지연(실측 0.9초)이 그대로 쌓여 16만 객체에
# 40시간이 넘는다. 업로드는 대기가 대부분이라 동시에 띄우면 거의 그만큼 빨라진다.
DEFAULT_CONCURRENCY = 12


def _chunks(items: list[Any], size: int) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _upload_all(
    uploader: Uploader,
    uploads: list[tuple[str, bytes, str]],
    concurrency: int,
    summary: PushSummary,
    kind: str,
) -> set[str]:
    """묶음을 동시에 올리고, 실패한 객체 키를 돌려준다.

    실패분은 호출부가 기록에서 빼서 다음 실행이 다시 시도하게 한다. 한 건 실패가
    나머지를 막지 않는다."""
    if not uploads:
        return set()
    failed: set[str] = set()
    if concurrency <= 1:
        for key, body, content_type in uploads:
            try:
                uploader.put(key, body, content_type)
            except Exception as exc:  # noqa: BLE001
                failed.add(key)
                summary.errors.append(f"{kind} {key}: {str(exc)[:120]}")
        return failed
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(uploader.put, key, body, content_type): key
            for key, body, content_type in uploads
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001
                failed.add(key)
                summary.errors.append(f"{kind} {key}: {str(exc)[:120]}")
    return failed


class Uploader(Protocol):
    """객체 하나를 올린다. 같은 key면 덮어쓴다."""

    def put(self, key: str, data: bytes, content_type: str) -> None: ...

    def list_keys(self, prefix: str) -> list[str]: ...

    def delete_keys(self, keys: list[str]) -> int: ...


class LocalDirUploader:
    """로컬 디렉터리에 그대로 쓴다. 계정 없이 파이프라인을 끝까지 검증할 때 쓴다."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def put(self, key: str, data: bytes, content_type: str) -> None:
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def list_keys(self, prefix: str) -> list[str]:
        base = self.root / prefix
        if not base.exists():
            return []
        return [str(p.relative_to(self.root)) for p in base.rglob("*") if p.is_file()]

    def delete_keys(self, keys: list[str]) -> int:
        removed = 0
        for key in keys:
            target = self.root / key
            if target.is_file():
                target.unlink()
                removed += 1
        return removed


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

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token = None
        while True:
            params: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                params["ContinuationToken"] = token
            response = self._client.list_objects_v2(**params)
            keys.extend(item["Key"] for item in response.get("Contents", []))
            if not response.get("IsTruncated"):
                return keys
            token = response["NextContinuationToken"]

    def delete_keys(self, keys: list[str]) -> int:
        removed = 0
        for batch in _chunks(keys, 1000):  # S3 delete_objects는 한 번에 1000개까지다
            self._client.delete_objects(
                Bucket=self.bucket, Delete={"Objects": [{"Key": key} for key in batch]}
            )
            removed += len(batch)
        return removed


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
    """공개 payload에 실리는 이름과 반드시 같아야 한다(common.asset_object_name 공유)."""
    return "v1/assets/" + asset_object_name(
        asset.get("sha256", ""), asset.get("content_type", "")
    )


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


def _snapshot_page(store: AuctionStore, **filters: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = store.list_items(require_coordinates=True, limit=500, offset=offset, **filters)
        rows = page["items"]
        if not rows:
            break
        items.extend(public_auction_summary(row) for row in rows)
        offset += len(rows)
        if offset >= page["total"]:
            break
    return items


def build_snapshot(store: AuctionStore) -> dict[str, Any]:
    """지도·목록용 요약. 지금 입찰할 수 있는 물건만 담는다.

    낙찰된 물건은 여기 섞지 않는다. 옥션원처럼 '진행 물건'과 '낙찰 물건'을
    나눠 봐야 목록이 끝난 물건으로 덮이지 않는다. 낙찰분은 sold 스냅샷에 있다."""
    return {"generated_at": utc_now(), "total": 0, "items": []} | _wrap(_snapshot_page(store, active=True))


def build_sold_snapshot(store: AuctionStore) -> dict[str, Any]:
    """낙찰 물건 전용 목록. 기간을 자르지 않는다.

    한 번 받아온 낙찰가는 시세 판단의 근거라 오래될수록 오히려 값지다.
    진행 목록과 섞이지 않으므로 쌓여도 목록을 어지럽히지 않는다."""
    return _wrap(_snapshot_page(store, sold_since="1900.01.01"))


def _wrap(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"generated_at": utc_now(), "total": len(items), "items": items}


def push_snapshot(store: AuctionStore, uploader: Uploader, *, dry_run: bool = False) -> tuple[bool, int]:
    """진행 물건 스냅샷과 낙찰 물건 스냅샷을 함께 올린다."""
    total = 0
    for key, payload in (
        (SNAPSHOT_KEY, build_snapshot(store)),
        (SOLD_SNAPSHOT_KEY, build_sold_snapshot(store)),
    ):
        digest = payload_digest(payload)
        body = gzip.compress(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        total += len(body)
        if dry_run:
            continue
        uploader.put(key, body, "application/gzip")
        store.mark_pushed("snapshot", key, hash_value=digest, remote_key=key, size=len(body))
    return True, total


def push_items(
    store: AuctionStore,
    uploader: Uploader,
    *,
    limit: int = 500,
    include_inactive: bool = True,
    dry_run: bool = False,
    concurrency: int = DEFAULT_CONCURRENCY,
    summary: PushSummary,
    on_progress: Callable[[int, int], None] | None = None,
) -> None:
    candidates = store.pending_item_pushes(limit=limit, include_inactive=include_inactive)
    done = 0
    for chunk in _chunks(candidates, MARK_BATCH):
        uploads: list[tuple[str, bytes, str]] = []
        marks: list[tuple[str, str, str, str, int]] = []
        for row in chunk:
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
                    continue
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                uploads.append((key, body, "application/json; charset=utf-8"))
                marks.append(("item", item_key, digest, key, len(body)))
            except Exception as exc:  # noqa: BLE001 - 한 건 실패로 전체를 멈추지 않는다
                summary.errors.append(f"item {item_key}: {str(exc)[:120]}")
        if not dry_run:
            failed = _upload_all(uploader, uploads, concurrency, summary, "item")
            marks = [m for m in marks if m[3] not in failed]
            store.mark_pushed_many(marks)
        summary.items_pushed += sum(1 for m in marks if m[4] > 0)
        summary.bytes_pushed += sum(m[4] for m in marks)
        done += len(chunk)
        if on_progress:
            on_progress(done, len(candidates))


def push_assets(
    store: AuctionStore,
    uploader: Uploader,
    *,
    limit: int = 500,
    include_inactive: bool = True,
    dry_run: bool = False,
    concurrency: int = DEFAULT_CONCURRENCY,
    summary: PushSummary,
    on_progress: Callable[[int, int], None] | None = None,
) -> None:
    candidates = store.pending_asset_pushes(limit=limit, include_inactive=include_inactive)
    done = 0
    for chunk in _chunks(candidates, MARK_BATCH):
        uploads: list[tuple[str, bytes, str]] = []
        marks: list[tuple[str, str, str, str, int]] = []
        for asset in chunk:
            summary.assets_checked += 1
            try:
                path = Path(str(asset.get("file_path", "")))
                if not path.is_file():
                    # 파일이 없으면 올릴 게 없다. 재시도해도 같으니 조용히 넘긴다.
                    continue
                key = asset_object_key(asset)
                if dry_run:
                    summary.assets_pushed += 1
                    continue
                body = path.read_bytes()
                uploads.append((key, body, str(asset.get("content_type") or "application/octet-stream")))
                marks.append(("asset", str(asset["id"]), str(asset.get("sha256", "")), key, len(body)))
            except Exception as exc:  # noqa: BLE001
                summary.errors.append(f"asset {asset.get('id')}: {str(exc)[:120]}")
        if not dry_run:
            failed = _upload_all(uploader, uploads, concurrency, summary, "asset")
            marks = [m for m in marks if m[3] not in failed]
            store.mark_pushed_many(marks)
            summary.assets_pushed += len(marks)
            summary.bytes_pushed += sum(m[4] for m in marks)
        done += len(chunk)
        if on_progress:
            on_progress(done, len(candidates))


@dataclass
class PruneReport:
    remote_total: int = 0
    orphan_items: list[str] = field(default_factory=list)
    orphan_assets: list[str] = field(default_factory=list)
    orphan_sync_rows: int = 0
    deleted: int = 0
    refused: str = ""

    @property
    def orphans(self) -> list[str]:
        return self.orphan_items + self.orphan_assets


def plan_prune(store: AuctionStore, uploader: Uploader, *, min_expected: int = 1_000) -> PruneReport:
    """R2에 남았지만 DB 어디에서도 참조하지 않는 객체를 찾는다(삭제는 하지 않는다).

    DB가 비었거나 읽기에 실패한 상태로 돌리면 버킷을 통째로 비우게 된다. 기대 목록이
    비정상적으로 적으면 아무것도 지우지 않고 사유만 돌려준다."""
    report = PruneReport()
    with store.connect() as conn:
        item_keys = [row[0] for row in conn.execute("SELECT item_key FROM auction_items")]
        asset_hashes = {
            row[0] for row in conn.execute("SELECT DISTINCT sha256 FROM auction_assets") if row[0]
        }
    if len(item_keys) < min_expected:
        report.refused = (
            f"DB에서 읽은 물건이 {len(item_keys)}건뿐이라 중단합니다"
            f"(기대 최소 {min_expected}건). DB 경로를 확인하세요."
        )
        return report

    expected_items = {item_object_key(key) for key in item_keys}
    remote = uploader.list_keys("v1/")
    report.remote_total = len(remote)
    for key in remote:
        if key == SNAPSHOT_KEY:
            continue
        if key.startswith("v1/items/"):
            if key not in expected_items:
                report.orphan_items.append(key)
        elif key.startswith("v1/assets/"):
            digest = Path(key).stem
            if digest not in asset_hashes:
                report.orphan_assets.append(key)
    with store.connect() as conn:
        report.orphan_sync_rows = conn.execute(
            """
            SELECT COUNT(*) FROM web_sync w
             WHERE (w.kind = 'item'
                    AND NOT EXISTS (SELECT 1 FROM auction_items i WHERE i.item_key = w.ref))
                OR (w.kind = 'asset'
                    AND NOT EXISTS (SELECT 1 FROM auction_assets a WHERE CAST(a.id AS TEXT) = w.ref))
            """
        ).fetchone()[0]
    return report


def apply_prune(store: AuctionStore, uploader: Uploader, report: PruneReport) -> PruneReport:
    """찾아둔 고아를 실제로 지운다. R2 객체와 web_sync 기록을 함께 정리한다."""
    if report.refused:
        return report
    if report.orphans:
        report.deleted = uploader.delete_keys(report.orphans)
    with store.connect() as conn:
        conn.execute(
            """
            DELETE FROM web_sync
             WHERE (kind = 'item'
                    AND NOT EXISTS (SELECT 1 FROM auction_items i WHERE i.item_key = web_sync.ref))
                OR (kind = 'asset'
                    AND NOT EXISTS (SELECT 1 FROM auction_assets a WHERE CAST(a.id AS TEXT) = web_sync.ref))
            """
        )
    return report


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
    concurrency: int = DEFAULT_CONCURRENCY,
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
        concurrency=concurrency,
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
            concurrency=concurrency,
            summary=summary,
            on_progress=(lambda i, n: on_progress("asset", i, n)) if on_progress else None,
        )
    return summary
