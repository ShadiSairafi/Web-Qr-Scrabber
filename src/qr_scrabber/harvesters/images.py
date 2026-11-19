"""Bounded-concurrency product image downloader with Pillow validation and thumbnailing."""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from qr_scrabber.http_client import make_async_client
from qr_scrabber.models import ImageDownloadStats, ProductRecord


def _atomic_write_bytes(path: Path, data: bytes) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return target


def normalize_image_to_jpeg(raw_bytes: bytes, *, max_dim: int = 320, quality: int = 82) -> bytes:
    """Validate raster bytes via Pillow and convert into a compact RGB JPEG thumbnail."""
    with Image.open(io.BytesIO(raw_bytes)) as im:
        im.load()
        if im.width <= 0 or im.height <= 0:
            raise ValueError("Image has zero width or height.")
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[-1])
            rgb = bg
        else:
            rgb = im.convert("RGB")
        rgb.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        rgb.save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue()


class ProductImageDownloader:
    """Downloads, verifies, and caches compact product thumbnails for exported products."""

    def __init__(
        self,
        *,
        cache_dir: Path = Path("data/cache/images"),
        concurrency: int = 12,
        max_dim: int = 320,
        timeout: float = 12.0,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.concurrency = max(1, concurrency)
        self.max_dim = max(64, max_dim)
        self.timeout = timeout

    def _cache_file_for_url(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        return self.cache_dir / f"{digest}.jpg"

    async def _fetch_one(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        urls: Sequence[str],
        dest_path: Path,
    ) -> tuple[bool, bool]:
        """Try candidate URLs in order. Returns `(succeeded, was_cache_hit)`."""
        # Fast path: destination already exists and is a valid Pillow image
        if dest_path.is_file():
            try:
                with Image.open(dest_path) as im:
                    im.verify()
                return True, True
            except (OSError, UnidentifiedImageError):
                dest_path.unlink(missing_ok=True)

        # Check URL disk cache
        for url in urls:
            cached = self._cache_file_for_url(url)
            if cached.is_file():
                try:
                    with Image.open(cached) as im:
                        im.verify()
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(cached, dest_path)
                    return True, True
                except (OSError, UnidentifiedImageError):
                    cached.unlink(missing_ok=True)

        # Download over HTTP with bounded concurrency
        async with sem:
            for url in urls:
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200 or not resp.content:
                        continue
                    ctype = resp.headers.get("content-type", "").lower()
                    if ctype and not (
                        "image/" in ctype or "octet-stream" in ctype or "binary" in ctype
                    ):
                        continue
                    jpeg_bytes = normalize_image_to_jpeg(resp.content, max_dim=self.max_dim)
                    _atomic_write_bytes(self._cache_file_for_url(url), jpeg_bytes)
                    _atomic_write_bytes(dest_path, jpeg_bytes)
                    return True, False
                except (httpx.HTTPError, OSError, UnidentifiedImageError, ValueError):
                    continue
        return False, False

    async def _download_async(
        self,
        products: list[ProductRecord],
        output_dir: Path,
        *,
        max_images: int = 0,
        brand_slug_filter: str | None = None,
    ) -> tuple[list[ProductRecord], ImageDownloadStats]:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        out_root = Path(output_dir)
        sem = asyncio.Semaphore(self.concurrency)

        headers = {
            "User-Agent": "WebQrScrabber/0.1 (+https://github.com/ShadiSairafi/Web-Qr-Scrabber)",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }

        updated: list[ProductRecord] = list(products)
        attempted = 0
        downloaded = 0
        cache_hits = 0
        remote_only = 0
        unavailable = 0
        failed = 0

        work_indices: list[tuple[int, list[str], Path, str]] = []
        for idx, rec in enumerate(products):
            if brand_slug_filter and rec.brand.slug != brand_slug_filter:
                continue
            urls = list(rec.product.image_urls)
            if rec.image_url and rec.image_url not in urls:
                urls.insert(0, rec.image_url)
            if not urls:
                unavailable += 1
                continue
            if max_images > 0 and len(work_indices) >= max_images:
                remote_only += 1
                continue
            rel_path = f"images/{rec.brand.slug}/{rec.gtin14}.jpg"
            dest_path = out_root / rel_path
            work_indices.append((idx, urls, dest_path, rel_path))

        attempted = len(work_indices)

        async with make_async_client(
            follow_redirects=True,
            timeout=self.timeout,
            headers=headers,
        ) as client:
            tasks = [
                self._fetch_one(client, sem, urls, dest_path)
                for _, urls, dest_path, _ in work_indices
            ]
            results = await asyncio.gather(*tasks)

        for (idx, _urls, _dest_path, rel_path), (ok, was_cached) in zip(
            work_indices,
            results,
            strict=True,
        ):
            rec = updated[idx]
            if ok:
                if was_cached:
                    cache_hits += 1
                downloaded += 1
                new_details = rec.product.model_copy(
                    update={
                        "local_image_path": rel_path,
                        "image_status": "downloaded",
                    }
                )
                updated[idx] = rec.model_copy(
                    update={
                        "local_image_path": rel_path,
                        "product": new_details,
                    }
                )
            else:
                failed += 1
                remote_only += 1
                new_details = rec.product.model_copy(
                    update={
                        "local_image_path": None,
                        "image_status": "remote_only",
                    }
                )
                updated[idx] = rec.model_copy(
                    update={
                        "local_image_path": None,
                        "product": new_details,
                    }
                )

        stats = ImageDownloadStats(
            attempted=attempted,
            downloaded=downloaded,
            cache_hits=cache_hits,
            remote_only=remote_only,
            unavailable=unavailable,
            failed=failed,
        )
        return updated, stats

    def download_for_products(
        self,
        products: list[ProductRecord],
        output_dir: Path,
        *,
        max_images: int = 0,
        brand_slug_filter: str | None = None,
    ) -> tuple[list[ProductRecord], ImageDownloadStats]:
        """Synchronously execute bounded async downloads and return updated records + stats."""
        return asyncio.run(
            self._download_async(
                products,
                output_dir,
                max_images=max_images,
                brand_slug_filter=brand_slug_filter,
            )
        )


__all__ = ["ProductImageDownloader", "normalize_image_to_jpeg"]
