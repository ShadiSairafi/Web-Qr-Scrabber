"""Persistent disk caches for UPCitemdb API responses and Open Facts bulk dumps."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from qr_scrabber.http_client import make_sync_client


def atomic_write_json(path: Path, payload: Any) -> Path:
    """Atomically write a UTF-8 formatted JSON file using a same-directory temp file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(encoded)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return target


class PersistentResponseCache:
    """Deterministic on-disk JSON cache keyed by `(endpoint, canonical_params)`."""

    def __init__(self, cache_path: Path) -> None:
        self.path = Path(cache_path)
        self._data: dict[str, Any] = {"version": 1, "entries": {}}
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("entries"), dict):
                    self._data = loaded
            except (OSError, json.JSONDecodeError):
                self._data = {"version": 1, "entries": {}}

    @staticmethod
    def make_key(endpoint: str, params: dict[str, Any]) -> str:
        canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return f"{endpoint}?{canonical}"

    def get(self, key: str) -> dict[str, Any] | None:
        entry = self._data["entries"].get(key)
        return entry if isinstance(entry, dict) else None

    def set(
        self,
        key: str,
        *,
        status_code: int,
        payload: dict[str, Any],
        endpoint: str,
        params: dict[str, Any],
    ) -> None:
        self._data["entries"][key] = {
            "endpoint": endpoint,
            "params": params,
            "status_code": status_code,
            "payload": payload,
            "cached_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        self.save()

    def all_entries(self) -> list[dict[str, Any]]:
        entries = self._data.get("entries", {})
        return [entries[k] for k in sorted(entries.keys()) if isinstance(entries[k], dict)]

    def save(self) -> Path:
        return atomic_write_json(self.path, self._data)


@dataclass(frozen=True)
class BulkDownloadResult:
    path: Path
    downloaded: bool
    status_code: int | None
    etag: str | None


def download_bulk_file(
    url: str,
    dest_path: Path,
    *,
    offline: bool = False,
    timeout: float = 120.0,
) -> BulkDownloadResult:
    """Download a bulk `.gz` dump with conditional `If-None-Match` ETag caching."""
    target = Path(dest_path)
    meta_path = target.with_suffix(target.suffix + ".meta.json")
    existing_meta: dict[str, Any] = {}
    if meta_path.is_file():
        try:
            existing_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing_meta = {}

    if offline:
        if not target.is_file():
            raise FileNotFoundError(f"Offline mode enabled but cached bulk dump missing: {target}")
        return BulkDownloadResult(
            path=target,
            downloaded=False,
            status_code=None,
            etag=existing_meta.get("etag"),
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    headers: dict[str, str] = {
        "User-Agent": "WebQrScrabber/0.1 (+https://github.com/ShadiSairafi/Web-Qr-Scrabber)",
    }
    if target.is_file() and isinstance(existing_meta.get("etag"), str):
        headers["If-None-Match"] = existing_meta["etag"]

    with make_sync_client(follow_redirects=True, timeout=timeout, headers=headers) as client:
        with client.stream("GET", url) as resp:
            if resp.status_code == 304 and target.is_file():
                return BulkDownloadResult(
                    path=target,
                    downloaded=False,
                    status_code=304,
                    etag=existing_meta.get("etag"),
                )
            resp.raise_for_status()
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=str(target.parent),
            )
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                        if chunk:
                            fh.write(chunk)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_path, target)
            except BaseException:
                tmp_path.unlink(missing_ok=True)
                raise

            etag = resp.headers.get("etag")
            atomic_write_json(
                meta_path,
                {
                    "url": url,
                    "etag": etag,
                    "last_modified": resp.headers.get("last-modified"),
                    "downloaded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                },
            )
            return BulkDownloadResult(
                path=target,
                downloaded=True,
                status_code=resp.status_code,
                etag=etag,
            )


__all__ = [
    "BulkDownloadResult",
    "PersistentResponseCache",
    "atomic_write_json",
    "download_bulk_file",
]
