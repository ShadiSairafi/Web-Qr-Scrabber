"""Consolidated UPCitemdb batch lookup & budgeted search harvester with persistent caching."""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from qr_scrabber.brands.matcher import BrandMatcher
from qr_scrabber.gs1 import normalize_barcode
from qr_scrabber.harvesters.cache import PersistentResponseCache
from qr_scrabber.harvesters.openbeautyfacts import HarvestedCandidate, SourceHarvestResult
from qr_scrabber.http_client import make_sync_client
from qr_scrabber.models import BrandMatch, QuarantineCandidate, QuarantineRecord, UpcitemdbQuota

DEFAULT_UPC_BASE_URL = "https://api.upcitemdb.com/prod/trial"


def _is_local_url(url: str) -> bool:
    lowered = url.lower()
    return "127.0.0.1" in lowered or "localhost" in lowered or "[::1]" in lowered


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split()).strip()
    return cleaned or None


def _extract_upc_names(item: dict[str, Any]) -> tuple[str | None, list[str]]:
    """Extract cleanest product name and alternate names from a UPCitemdb item + offers."""
    names: list[str] = []
    seen: set[str] = set()

    primary_title = _clean_text(item.get("title"))
    if primary_title:
        names.append(primary_title)
        seen.add(primary_title.casefold())

    offers = item.get("offers")
    if isinstance(offers, list):
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            offer_title = _clean_text(offer.get("title"))
            if offer_title and offer_title.casefold() not in seen:
                seen.add(offer_title.casefold())
                names.append(offer_title)

    if not names:
        return None, []
    return names[0], names[1:6]


def _extract_upc_images(item: dict[str, Any]) -> tuple[str | None, list[str]]:
    urls: list[str] = []
    raw_imgs = item.get("images")
    if isinstance(raw_imgs, list):
        for u in raw_imgs:
            cleaned = _clean_text(u)
            if cleaned and cleaned.startswith(("http://", "https://")) and cleaned not in urls:
                urls.append(cleaned)
    return (urls[0] if urls else None), urls


@dataclass
class UPCItemDBHarvestOutput:
    harvest: SourceHarvestResult
    quota: UpcitemdbQuota
    rate_limited: bool = False


class UPCItemDBHarvester:
    """Single keep-alive session UPCitemdb client with 2-UPC batching, pacing, and disk cache."""

    def __init__(
        self,
        *,
        cache_path: Path,
        base_url: str = DEFAULT_UPC_BASE_URL,
        offline: bool = False,
        lookup_delay_s: float | None = None,
        search_delay_s: float | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.cache = PersistentResponseCache(cache_path)
        self.base_url = base_url.rstrip("/")
        self.offline = offline
        is_local = _is_local_url(self.base_url)
        self.lookup_delay_s = (
            0.0 if is_local else (10.6 if lookup_delay_s is None else lookup_delay_s)
        )
        self.search_delay_s = (
            0.0 if is_local else (16.2 if search_delay_s is None else search_delay_s)
        )
        self.timeout = timeout

        self.requests_made_this_run: int = 0
        self.cache_hits: int = 0
        self.rate_limit_remaining: int | None = None
        self.rate_limited: bool = False
        self._last_request_monotonic: float | None = None

    def _pace(self, delay_s: float) -> None:
        if delay_s <= 0 or self._last_request_monotonic is None:
            return
        elapsed = time.monotonic() - self._last_request_monotonic
        remaining = delay_s - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _fetch_endpoint(
        self,
        client: httpx.Client | None,
        endpoint: str,
        params: dict[str, Any],
        *,
        delay_s: float,
    ) -> dict[str, Any] | None:
        key = self.cache.make_key(endpoint, params)
        cached = self.cache.get(key)
        if cached is not None:
            self.cache_hits += 1
            payload = cached.get("payload")
            return payload if isinstance(payload, dict) else None

        if self.offline or self.rate_limited or client is None:
            return None

        self._pace(delay_s)
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        try:
            resp = client.get(url, params=params)
        except httpx.HTTPError:
            return None
        finally:
            self._last_request_monotonic = time.monotonic()

        self.requests_made_this_run += 1
        rem_hdr = resp.headers.get("x-ratelimit-remaining")
        if rem_hdr is not None and rem_hdr.isdigit():
            self.rate_limit_remaining = int(rem_hdr)

        if resp.status_code == 429:
            self.rate_limited = True
            return None

        try:
            payload = resp.json()
        except ValueError:
            payload = {"code": f"HTTP_{resp.status_code}", "items": []}

        if resp.status_code in (200, 404) and isinstance(payload, dict):
            self.cache.set(
                key,
                status_code=resp.status_code,
                payload=payload,
                endpoint=endpoint,
                params=params,
            )
            return payload
        return None

    def _ingest_items(
        self,
        items: Sequence[dict[str, Any]],
        *,
        matcher: BrandMatcher,
        result: SourceHarvestResult,
    ) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            result.rows_scanned += 1
            raw_codes: list[str] = []
            for code_key in ("ean", "upc", "gtin"):
                val = _clean_text(item.get(code_key))
                if val and val not in raw_codes:
                    raw_codes.append(val)
            if not raw_codes:
                continue

            primary_name, alt_names = _extract_upc_names(item)
            raw_brand = _clean_text(item.get("brand"))
            raw_category = _clean_text(item.get("category"))
            raw_desc = _clean_text(item.get("description"))

            match = matcher.match_upc_item(
                brand=raw_brand,
                title=primary_name,
                category=raw_category,
                description=raw_desc,
            )
            if match is None:
                continue

            if isinstance(match, QuarantineCandidate):
                norm_q = normalize_barcode(raw_codes[0])
                result.quarantined.append(
                    QuarantineRecord(
                        raw_code=raw_codes[0],
                        gtin14=norm_q.gtin14 or None,
                        reason=match.reason,
                        source="upcitemdb",
                        raw_brand=raw_brand,
                        raw_title=primary_name,
                    )
                )
                continue

            assert isinstance(match, BrandMatch)
            result.raw_matched_count += 1

            # Validate the best barcode among ean/upc/gtin
            valid_norm = None
            first_norm = None
            for rc in raw_codes:
                n = normalize_barcode(rc)
                if first_norm is None:
                    first_norm = n
                if n.is_valid:
                    valid_norm = n
                    break

            if valid_norm is None:
                result.quarantined.append(
                    QuarantineRecord(
                        raw_code=raw_codes[0],
                        gtin14=(first_norm.gtin14 or None) if first_norm else None,
                        reason=(
                            (first_norm.quarantine_reason or "NON_NUMERIC_OR_INVALID_LENGTH")
                            if first_norm
                            else "NON_NUMERIC_OR_INVALID_LENGTH"
                        ),
                        source="upcitemdb",
                        raw_brand=raw_brand,
                        raw_title=primary_name,
                    )
                )
                continue

            if not primary_name:
                result.quarantined.append(
                    QuarantineRecord(
                        raw_code=raw_codes[0],
                        gtin14=valid_norm.gtin14,
                        reason="MISSING_PRODUCT_NAME",
                        source="upcitemdb",
                        raw_brand=raw_brand,
                        raw_title=None,
                    )
                )
                continue

            primary_img, all_imgs = _extract_upc_images(item)
            categories = (
                [part.strip() for part in raw_category.split(">") if part.strip()]
                if raw_category
                else []
            )
            size_val = _clean_text(item.get("size")) or _clean_text(item.get("weight"))

            result.candidates.append(
                HarvestedCandidate(
                    source="upcitemdb",
                    gtin14=valid_norm.gtin14,
                    barcode=valid_norm,
                    raw_codes=list(raw_codes),
                    brand_slug=match.brand_slug,
                    brand_display_name=match.display_name,
                    glow_and_glare_vendor_url=match.glow_and_glare_vendor_url,
                    matched_raw_brand=match.matched_raw_brand,
                    match_method=match.match_method,
                    match_confidence=match.confidence,
                    product_name=primary_name,
                    alternate_names=list(alt_names),
                    quantity=size_val,
                    categories=categories,
                    description=raw_desc,
                    primary_image_url=primary_img,
                    thumbnail_url=primary_img,
                    image_urls=all_imgs,
                    upcitemdb_ean=_clean_text(item.get("ean"))
                    or valid_norm.ean13
                    or valid_norm.primary_code,
                    last_modified_t=0,
                )
            )

    def harvest(
        self,
        *,
        matcher: BrandMatcher,
        lookup_upcs: Sequence[str] = (),
        search_queries: Sequence[str] = (),
        max_lookups: int = 30,
        max_searches: int = 15,
        replay_all_cached: bool = True,
    ) -> UPCItemDBHarvestOutput:
        """Execute consolidated 2-UPC batch lookups, budgeted searches, and cache replay."""
        result = SourceHarvestResult(source="upcitemdb")
        seen_cache_keys: set[str] = set()

        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "User-Agent": "WebQrScrabber/0.1 (+https://github.com/ShadiSairafi/Web-Qr-Scrabber)",
        }

        client: httpx.Client | None = None
        if not self.offline:
            client = make_sync_client(timeout=self.timeout, headers=headers)

        try:
            # 1. Consolidated 2-UPC comma-separated batch lookups (/lookup?upc=A,B)
            deduped_upcs = list(dict.fromkeys(u.strip() for u in lookup_upcs if u and u.strip()))
            batches = [deduped_upcs[i : i + 2] for i in range(0, len(deduped_upcs), 2)]
            live_lookups_used = 0
            for batch in batches:
                params = {"upc": ",".join(batch)}
                key = self.cache.make_key("lookup", params)
                is_cached = self.cache.get(key) is not None
                if not is_cached:
                    if self.offline or self.rate_limited or live_lookups_used >= max_lookups:
                        continue
                    live_lookups_used += 1
                seen_cache_keys.add(key)
                payload = self._fetch_endpoint(
                    client,
                    "lookup",
                    params,
                    delay_s=self.lookup_delay_s,
                )
                if payload and isinstance(payload.get("items"), list):
                    self._ingest_items(payload["items"], matcher=matcher, result=result)

            # 2. Consolidated/budgeted searches (/search?s=...&type=product&match_mode=1&offset=...)
            live_searches_used = 0
            for query in dict.fromkeys(q.strip() for q in search_queries if q and q.strip()):
                offset = 0
                # Single page per consolidated query unless additional cached/budgeted pages fit
                params: dict[str, Any] = {
                    "s": query,
                    "type": "product",
                    "match_mode": "1",
                }
                if offset > 0:
                    params["offset"] = str(offset)
                key = self.cache.make_key("search", params)
                is_cached = self.cache.get(key) is not None
                if not is_cached:
                    if self.offline or self.rate_limited or live_searches_used >= max_searches:
                        continue
                    live_searches_used += 1
                seen_cache_keys.add(key)
                payload = self._fetch_endpoint(
                    client,
                    "search",
                    params,
                    delay_s=self.search_delay_s,
                )
                if payload and isinstance(payload.get("items"), list):
                    self._ingest_items(payload["items"], matcher=matcher, result=result)

            # 3. Replay any remaining previously cached responses (deterministic offline replay)
            if replay_all_cached:
                for entry in self.cache.all_entries():
                    endpoint = str(entry.get("endpoint") or "")
                    params_obj = entry.get("params")
                    if not endpoint or not isinstance(params_obj, dict):
                        continue
                    key = self.cache.make_key(endpoint, params_obj)
                    if key in seen_cache_keys:
                        continue
                    seen_cache_keys.add(key)
                    self.cache_hits += 1
                    payload = entry.get("payload")
                    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
                        self._ingest_items(payload["items"], matcher=matcher, result=result)
        finally:
            if client is not None:
                client.close()

        return UPCItemDBHarvestOutput(
            harvest=result,
            quota=UpcitemdbQuota(
                requests_made_this_run=self.requests_made_this_run,
                cache_hits=self.cache_hits,
                rate_limit_remaining=self.rate_limit_remaining,
            ),
            rate_limited=self.rate_limited,
        )


__all__ = [
    "DEFAULT_UPC_BASE_URL",
    "UPCItemDBHarvestOutput",
    "UPCItemDBHarvester",
]
