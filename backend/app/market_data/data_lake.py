"""Private content-addressed storage for lossless public provider pages."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Protocol

from app.schemas.data_lake import (
    BackfillRawPageLink,
    RawDataObject,
    RawProviderPage,
    raw_page_id,
)

_SAFE_SOURCE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")


class RawDataLakeRepository(Protocol):
    async def save_backfill_raw_page(
        self,
        raw_object: RawDataObject,
        link: BackfillRawPageLink,
    ) -> None: ...


class LocalContentAddressedBlobStore:
    """Filesystem adapter with deterministic gzip objects and atomic writes.

    The interface is intentionally small so a private Supabase Storage or S3
    adapter can replace it without changing provider or backfill code.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    @staticmethod
    def canonical_json(document: dict) -> bytes:
        return json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    async def put_page(self, page: RawProviderPage) -> RawDataObject:
        if not _SAFE_SOURCE.fullmatch(page.source):
            raise ValueError("raw page source is not safe for object storage")
        uncompressed = self.canonical_json(page.model_dump(mode="json"))
        object_hash = hashlib.sha256(uncompressed).hexdigest()
        fetched_at = page.fetched_at
        relative = Path(
            "raw",
            page.source,
            f"{fetched_at.year:04d}",
            f"{fetched_at.month:02d}",
            f"{fetched_at.day:02d}",
            object_hash[:2],
            f"{object_hash}.json.gz",
        )
        destination = (self._root / relative).resolve()
        if not destination.is_relative_to(self._root):
            raise ValueError("raw object path escaped the data-lake root")
        stored = gzip.compress(uncompressed, compresslevel=6, mtime=0)
        await asyncio.to_thread(self._write_once, destination, stored)
        return RawDataObject(
            object_hash=object_hash,
            object_uri=f"lake://{relative.as_posix()}",
            uncompressed_bytes=len(uncompressed),
            stored_bytes=len(stored),
            created_at=page.fetched_at,
        )

    @staticmethod
    def _write_once(destination: Path, content: bytes) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            return
        temporary = destination.with_name(
            f".{destination.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()

    async def read_json(self, raw_object: RawDataObject) -> dict:
        prefix = "lake://"
        if not raw_object.object_uri.startswith(prefix):
            raise ValueError("unsupported raw object URI")
        relative = Path(raw_object.object_uri.removeprefix(prefix))
        source = (self._root / relative).resolve()
        if not source.is_relative_to(self._root):
            raise ValueError("raw object URI escaped the data-lake root")
        compressed = await asyncio.to_thread(source.read_bytes)
        uncompressed = gzip.decompress(compressed)
        if hashlib.sha256(uncompressed).hexdigest() != raw_object.object_hash:
            raise ValueError("raw object content hash does not match metadata")
        return json.loads(uncompressed)


class RawDataLake:
    """Archives bytes first, then records immutable lineage in PostgreSQL."""

    def __init__(
        self,
        repository: RawDataLakeRepository,
        blob_store: LocalContentAddressedBlobStore,
    ) -> None:
        self._repository = repository
        self._blob_store = blob_store

    async def archive_page(
        self,
        *,
        job_id: str,
        attempt_count: int,
        page: RawProviderPage,
    ) -> BackfillRawPageLink:
        if attempt_count < 1:
            raise ValueError("attempt_count must be positive")
        raw_object = await self._blob_store.put_page(page)
        link = BackfillRawPageLink(
            page_id=raw_page_id(
                job_id=job_id,
                attempt_count=attempt_count,
                page_index=page.page_index,
                object_hash=raw_object.object_hash,
            ),
            job_id=job_id,
            attempt_count=attempt_count,
            page_index=page.page_index,
            object_hash=raw_object.object_hash,
            source=page.source,
            endpoint=page.endpoint,
            request_params=page.request_params,
            fetched_at=page.fetched_at,
            created_at=page.fetched_at,
        )
        await self._repository.save_backfill_raw_page(raw_object, link)
        return link
