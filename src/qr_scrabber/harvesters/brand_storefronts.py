"""Official brand Shopify storefront harvester for manufacturer-authoritative GS1 barcodes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from qr_scrabber.brands.registry import BrandRegistry
from qr_scrabber.gs1 import normalize_barcode
from qr_scrabber.harvesters.cache import atomic_write_json
from qr_scrabber.harvesters.openbeautyfacts import HarvestedCandidate, SourceHarvestResult
from qr_scrabber.http_client import make_async_client
from qr_scrabber.models import QuarantineRecord

# Verified official brand Shopify storefronts exposing GS1 barcodes in /products/<handle>.js
OFFICIAL_BRAND_STOREFRONTS: dict[str, str] = {
    "about-face": "https://aboutface.com",
    "anastasia-beverly-hills": "https://www.anastasiabeverlyhills.com",
    "anua": "https://anua.us",
    "bh-cosmetics": "https://www.bhcosmetics.com",
    "danessa-myricks-beauty": "https://danessamyricksbeauty.com",
    "fable-and-mane": "https://fableandmane.com",
    "fenty-beauty": "https://fentybeauty.com",
    "frank-body": "https://www.frankbody.com",
    "gisou": "https://us.gisou.com",
    "grande-cosmetics": "https://grandecosmetics.com",
    "hourglass": "https://www.hourglasscosmetics.com",
    "ilia": "https://iliabeauty.com",
    "juvias-place": "https://www.juviasplace.com",
    "k18": "https://www.k18hair.com",
    "kitsch": "https://www.mykitsch.com",
    "kosas": "https://kosas.com",
    "kylie-cosmetics": "https://kyliecosmetics.com",
    "lanolips": "https://lanolips.com",
    "lime-crime": "https://limecrime.com",
    "naturium": "https://naturium.com",
    "ofra": "https://ofracosmetics.com",
    "onesize": "https://www.onesizebeauty.com",
    "ouai": "https://theouai.com",
    "patrick-ta": "https://patrickta.com",
    "pixi": "https://www.pixibeauty.com",
    "refy": "https://refybeauty.com",
    "rhode": "https://www.rhodeskin.com",
    "rodial": "https://www.rodial.com",
    "saie": "https://saiehello.com",
    "stila": "https://www.stilacosmetics.com",
    "summer-fridays": "https://summerfridays.com",
    "sunday-riley": "https://sundayriley.com",
    "tarte": "https://tartecosmetics.com",
    "touchland": "https://touchland.com",
    "tower28": "https://www.tower28beauty.com",
    "wonderskin": "https://wonderskin.com",
}

DEFAULT_STOREFRONT_CACHE = Path("data/brand_storefronts_cache.json")


def _clean_url(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    u = url.strip()
    if u.startswith("//"):
        u = "https:" + u
    if u.startswith(("http://", "https://")):
        return u
    return None


def _clean_text(val: Any) -> str | None:
    if not isinstance(val, str):
        return None
    s = " ".join(val.split()).strip()
    return s or None


async def _harvest_single_store(
    client: Any,
    sem: asyncio.Semaphore,
    brand_slug: str,
    base_url: str,
    *,
    max_products_per_brand: int = 120,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    handles: list[str] = []

    # Paginate /products.json?limit=250&page=1..2
    for page in (1, 2):
        async with sem:
            try:
                r = await client.get(
                    f"{base_url}/products.json",
                    params={"limit": 250, "page": page},
                )
                if r.status_code != 200 or "json" not in r.headers.get("content-type", "").lower():
                    break
                prods = r.json().get("products") or []
            except Exception:
                break
        if not prods:
            break
        for p in prods:
            h = p.get("handle")
            if isinstance(h, str) and h and h not in handles:
                handles.append(h)
        if len(prods) < 250 or len(handles) >= max_products_per_brand:
            break

    handles = handles[:max_products_per_brand]

    async def _fetch_product_js(handle: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        async with sem:
            try:
                r = await client.get(f"{base_url}/products/{handle}.js")
                if r.status_code != 200:
                    return out
                pj = r.json()
            except Exception:
                return out

        if not isinstance(pj, dict):
            return out
        prod_title = _clean_text(pj.get("title")) or ""
        prod_type = _clean_text(pj.get("type"))
        prod_img = _clean_url(pj.get("featured_image"))
        if not prod_img:
            raw_imgs = pj.get("images")
            if isinstance(raw_imgs, list) and raw_imgs:
                prod_img = _clean_url(raw_imgs[0])

        variants = pj.get("variants")
        if not isinstance(variants, list):
            return out

        for v in variants:
            if not isinstance(v, dict):
                continue
            barcode = str(v.get("barcode") or "").strip()
            if not barcode:
                continue
            v_name = _clean_text(v.get("name"))
            v_title = _clean_text(v.get("title"))
            if v_name and v_name.lower() != "default title":
                full_name = v_name
            elif prod_title and v_title and v_title.lower() != "default title":
                full_name = f"{prod_title} - {v_title}"
            else:
                full_name = prod_title or v_name or ""
            full_name = full_name.strip()
            if len(full_name) < 2:
                continue

            v_img_obj = v.get("featured_image")
            v_img = _clean_url(v_img_obj.get("src")) if isinstance(v_img_obj, dict) else None
            img = v_img or prod_img
            out.append(
                {
                    "brand_slug": brand_slug,
                    "barcode": barcode,
                    "product_name": full_name,
                    "category": prod_type,
                    "image_url": img,
                    "source_url": f"{base_url}/products/{handle}",
                }
            )
        return out

    batches = await asyncio.gather(*(_fetch_product_js(h) for h in handles))
    for b in batches:
        records.extend(b)
    return records


async def _harvest_all_stores_async(
    stores: dict[str, str],
    *,
    concurrency: int = 24,
    max_products_per_brand: int = 120,
) -> list[dict[str, Any]]:
    sem = asyncio.Semaphore(concurrency)
    headers = {
        "User-Agent": "WebQrScrabber/0.1 (+https://github.com/ShadiSairafi/Web-Qr-Scrabber)",
        "Accept": "application/json",
    }
    async with make_async_client(
        timeout=10.0,
        follow_redirects=True,
        headers=headers,
    ) as client:
        results = await asyncio.gather(
            *(
                _harvest_single_store(
                    client,
                    sem,
                    slug,
                    url,
                    max_products_per_brand=max_products_per_brand,
                )
                for slug, url in stores.items()
            )
        )
    all_rows: list[dict[str, Any]] = []
    for rows in results:
        all_rows.extend(rows)
    return all_rows


def harvest_brand_storefronts(
    *,
    registry: BrandRegistry,
    cache_path: Path = DEFAULT_STOREFRONT_CACHE,
    offline: bool = False,
    max_products_per_brand: int = 120,
    refresh: bool = False,
) -> SourceHarvestResult:
    """Harvest official GS1 barcodes & studio images from brand Shopify storefronts (cached)."""
    result = SourceHarvestResult(source="brand_storefront")
    cache_file = Path(cache_path)

    raw_rows: list[dict[str, Any]] = []
    if cache_file.is_file() and not refresh:
        try:
            loaded = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                raw_rows = loaded
        except (OSError, json.JSONDecodeError):
            raw_rows = []
    elif not offline:
        active_stores = {
            slug: url
            for slug, url in OFFICIAL_BRAND_STOREFRONTS.items()
            if registry.get_canonical_entry(slug) is not None
        }
        if active_stores:
            raw_rows = asyncio.run(
                _harvest_all_stores_async(
                    active_stores,
                    max_products_per_brand=max_products_per_brand,
                )
            )
            atomic_write_json(cache_file, raw_rows)

    for row in raw_rows:
        result.rows_scanned += 1
        brand_slug = str(row.get("brand_slug") or "")
        entry = registry.get_canonical_entry(brand_slug)
        if entry is None:
            continue
        raw_code = str(row.get("barcode") or "").strip()
        title = _clean_text(row.get("product_name"))
        norm = normalize_barcode(raw_code)
        if not norm.is_valid:
            result.quarantined.append(
                QuarantineRecord(
                    raw_code=raw_code,
                    gtin14=norm.gtin14 or None,
                    reason=norm.quarantine_reason or "NON_NUMERIC_OR_INVALID_LENGTH",
                    source="brand_storefront",
                    raw_brand=entry.display_name,
                    raw_title=title,
                )
            )
            continue
        if not title or len(title) < 2:
            result.quarantined.append(
                QuarantineRecord(
                    raw_code=raw_code,
                    gtin14=norm.gtin14,
                    reason="MISSING_PRODUCT_NAME",
                    source="brand_storefront",
                    raw_brand=entry.display_name,
                    raw_title=None,
                )
            )
            continue

        result.raw_matched_count += 1
        img = _clean_url(row.get("image_url"))
        cat = _clean_text(row.get("category"))
        result.candidates.append(
            HarvestedCandidate(
                source="brand_storefront",
                gtin14=norm.gtin14,
                barcode=norm,
                raw_codes=[raw_code],
                brand_slug=entry.slug,
                brand_display_name=entry.display_name,
                glow_and_glare_vendor_url=entry.glow_and_glare_vendor_url,
                matched_raw_brand=entry.display_name,
                match_method="official_brand_storefront",
                match_confidence=0.98,
                product_name=title,
                alternate_names=[],
                quantity=None,
                categories=[cat] if cat else ["Beauty"],
                description=None,
                primary_image_url=img,
                thumbnail_url=img,
                image_urls=[img] if img else [],
                source_url=_clean_url(row.get("source_url")),
                last_modified_t=0,
            )
        )

    return result


__all__ = [
    "DEFAULT_STOREFRONT_CACHE",
    "OFFICIAL_BRAND_STOREFRONTS",
    "harvest_brand_storefronts",
]
