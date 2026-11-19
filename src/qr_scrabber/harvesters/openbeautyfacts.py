"""Open Beauty Facts & Open Products Facts streaming bulk dump harvester."""

from __future__ import annotations

import gzip
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from qr_scrabber.brands.matcher import BrandMatcher
from qr_scrabber.brands.registry import strip_lang_prefix
from qr_scrabber.gs1 import normalize_barcode, split_concatenated_codes
from qr_scrabber.models import (
    BrandMatch,
    NormalizedBarcode,
    QuarantineCandidate,
    QuarantineRecord,
)

OBF_JSONL_URL = "https://static.openbeautyfacts.org/data/openbeautyfacts-products.jsonl.gz"
OBF_CSV_URL = "https://static.openbeautyfacts.org/data/en.openbeautyfacts.org.products.csv.gz"
OPF_CSV_URL = "https://static.openproductsfacts.org/data/en.openproductsfacts.org.products.csv.gz"

_OBF_CODE_SPLIT_RE = re.compile(r"^(..?.?)(...)(...)(.*)$")


@dataclass
class HarvestedCandidate:
    """Normalized product candidate harvested from a single upstream source."""

    source: str
    gtin14: str
    barcode: NormalizedBarcode
    raw_codes: list[str]
    brand_slug: str
    brand_display_name: str
    glow_and_glare_vendor_url: str
    matched_raw_brand: str
    match_method: str
    match_confidence: float
    product_name: str | None
    alternate_names: list[str] = field(default_factory=list)
    quantity: str | None = None
    categories: list[str] = field(default_factory=list)
    description: str | None = None
    primary_image_url: str | None = None
    thumbnail_url: str | None = None
    image_urls: list[str] = field(default_factory=list)
    source_url: str | None = None
    upcitemdb_ean: str | None = None
    last_modified_t: int = 0


@dataclass
class SourceHarvestResult:
    """Aggregated harvest results and quarantine records for one upstream source."""

    source: str
    rows_scanned: int = 0
    raw_matched_count: int = 0
    candidates: list[HarvestedCandidate] = field(default_factory=list)
    quarantined: list[QuarantineRecord] = field(default_factory=list)


def _open_text_maybe_gzip(path: Path) -> TextIO:
    p = Path(path)
    if p.suffix.lower() == ".gz":
        return gzip.open(p, mode="rt", encoding="utf-8", errors="replace")
    return p.open(mode="r", encoding="utf-8", errors="replace")


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split()).strip()
    return cleaned or None


def _is_valid_http_url(url: Any) -> str | None:
    cleaned = _clean_text(url)
    if cleaned and cleaned.startswith(("http://", "https://")) and "/invalid/" not in cleaned:
        return cleaned
    return None


def derive_obf_image_urls(
    code: str,
    images_obj: Any,
    *,
    host: str = "https://images.openbeautyfacts.org",
) -> tuple[str | None, str | None, list[str]]:
    """Derive deterministic Open Beauty/Products Facts front image URLs from `images` JSONL dict."""
    if not isinstance(images_obj, dict) or not code or not code.isdigit():
        return None, None, []

    if len(code) <= 8:
        path_prefix = code
    else:
        padded = code.zfill(13) if len(code) < 13 else code
        m = _OBF_CODE_SPLIT_RE.match(padded)
        if not m:
            return None, None, []
        path_prefix = "/".join(m.groups())

    # Prefer front_en, then front_fr, then any front_*, then numeric raw image "1"
    front_keys = [k for k in images_obj if isinstance(k, str) and k.startswith("front_")]
    front_keys.sort(key=lambda k: (0 if k == "front_en" else 1 if k == "front_fr" else 2, k))

    primary: str | None = None
    thumb: str | None = None
    urls: list[str] = []

    for key in front_keys:
        entry = images_obj.get(key)
        if not isinstance(entry, dict):
            continue
        rev = entry.get("rev")
        if rev is None or str(rev).strip() == "":
            continue
        url_400 = f"{host}/images/products/{path_prefix}/{key}.{rev}.400.jpg"
        url_200 = f"{host}/images/products/{path_prefix}/{key}.{rev}.200.jpg"
        if primary is None:
            primary = url_400
            thumb = url_200
        for u in (url_400, url_200):
            if u not in urls:
                urls.append(u)

    if primary is None and "1" in images_obj and isinstance(images_obj["1"], dict):
        url_raw = f"{host}/images/products/{path_prefix}/1.400.jpg"
        primary = url_raw
        thumb = f"{host}/images/products/{path_prefix}/1.200.jpg"
        urls.extend([primary, thumb])

    return primary, thumb, urls


def _extract_obf_names(record: dict[str, Any]) -> tuple[str | None, list[str]]:
    """Extract cleanest product name + distinct alternate names from an OBF JSONL record."""
    ordered_keys = (
        "product_name_en",
        "product_name",
        "product_name_fr",
        "abbreviated_product_name",
        "generic_name_en",
        "generic_name",
    )
    found: list[str] = []
    seen_cf: set[str] = set()
    for key in ordered_keys:
        val = _clean_text(record.get(key))
        if val and val.casefold() not in seen_cf:
            seen_cf.add(val.casefold())
            found.append(val)
    if not found:
        return None, []
    return found[0], found[1:]


def _extract_categories(categories_tags: Any, categories_raw: Any) -> list[str]:
    cats: list[str] = []
    seen: set[str] = set()
    if isinstance(categories_tags, list):
        for tag in categories_tags:
            if isinstance(tag, str) and tag.strip():
                nice = strip_lang_prefix(tag).replace("-", " ").strip().title()
                if nice and nice.casefold() not in seen:
                    seen.add(nice.casefold())
                    cats.append(nice)
    elif isinstance(categories_raw, str) and categories_raw.strip():
        for part in categories_raw.split(","):
            nice = part.strip()
            if nice and nice.casefold() not in seen:
                seen.add(nice.casefold())
                cats.append(nice)
    return cats[:12]


def load_csv_image_map(csv_path: Path) -> dict[str, tuple[str | None, str | None]]:
    """Extract `code -> (image_url, image_small_url)` map from an OBF/OPF TSV `.csv(.gz)` dump."""
    image_map: dict[str, tuple[str | None, str | None]] = {}
    with _open_text_maybe_gzip(csv_path) as fh:
        header_line = fh.readline()
        if not header_line:
            return image_map
        headers = header_line.rstrip("\r\n").split("\t")
        expected_cols = len(headers)
        col_idx = {name: i for i, name in enumerate(headers)}
        code_i = col_idx.get("code")
        img_i = col_idx.get("image_url")
        small_i = col_idx.get("image_small_url")
        if code_i is None or (img_i is None and small_i is None):
            return image_map

        for line in fh:
            parts = line.rstrip("\r\n").split("\t")
            if len(parts) != expected_cols:
                continue
            code = parts[code_i].strip()
            if not code:
                continue
            img = _is_valid_http_url(parts[img_i]) if img_i is not None else None
            small = _is_valid_http_url(parts[small_i]) if small_i is not None else None
            if img or small:
                image_map[code] = (img, small)
    return image_map


def _iter_obf_jsonl_rows(jsonl_path: Path) -> Iterator[dict[str, Any]]:
    with _open_text_maybe_gzip(jsonl_path) as fh:
        for line in fh:
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def _iter_obf_csv_rows(csv_path: Path) -> Iterator[dict[str, Any]]:
    with _open_text_maybe_gzip(csv_path) as fh:
        header_line = fh.readline()
        if not header_line:
            return
        headers = header_line.rstrip("\r\n").split("\t")
        expected_cols = len(headers)
        col_idx = {name: i for i, name in enumerate(headers)}

        def _get(parts: list[str], name: str) -> str:
            idx = col_idx.get(name)
            return parts[idx].strip() if idx is not None and idx < len(parts) else ""

        for line in fh:
            parts = line.rstrip("\r\n").split("\t")
            if len(parts) != expected_cols:
                continue
            b_tags_str = _get(parts, "brands_tags")
            c_tags_str = _get(parts, "categories_tags")
            # Note: OBF CSV has inverted last_modified_datetime (unix) vs last_modified_t (ISO)
            lm_raw = _get(parts, "last_modified_datetime") or _get(parts, "last_modified_t")
            lm_int = int(lm_raw) if lm_raw.isdigit() else 0
            yield {
                "code": _get(parts, "code"),
                "url": _get(parts, "url"),
                "product_name": _get(parts, "product_name"),
                "abbreviated_product_name": _get(parts, "abbreviated_product_name"),
                "generic_name": _get(parts, "generic_name"),
                "quantity": _get(parts, "quantity"),
                "brands": _get(parts, "brands") or _get(parts, "brands_en"),
                "brands_tags": [t.strip() for t in b_tags_str.split(",") if t.strip()],
                "categories": _get(parts, "categories_en") or _get(parts, "categories"),
                "categories_tags": [t.strip() for t in c_tags_str.split(",") if t.strip()],
                "image_url": _get(parts, "image_url"),
                "image_small_url": _get(parts, "image_small_url"),
                "last_modified_t": lm_int,
            }


def harvest_open_facts(
    *,
    matcher: BrandMatcher,
    jsonl_path: Path | None = None,
    csv_path: Path | None = None,
    source_name: str = "openbeautyfacts",
) -> SourceHarvestResult:
    """Stream Open Beauty Facts or Open Products Facts dumps and return matched candidates."""
    is_beauty_db = source_name == "openbeautyfacts"
    host = (
        "https://images.openbeautyfacts.org"
        if is_beauty_db
        else "https://images.openproductsfacts.org"
    )
    web_host = (
        "https://world.openbeautyfacts.org"
        if is_beauty_db
        else "https://world.openproductsfacts.org"
    )

    result = SourceHarvestResult(source=source_name)
    csv_images: dict[str, tuple[str | None, str | None]] = {}

    if jsonl_path is not None:
        if csv_path is not None and Path(csv_path).is_file():
            csv_images = load_csv_image_map(csv_path)
        row_iter = _iter_obf_jsonl_rows(jsonl_path)
    elif csv_path is not None:
        row_iter = _iter_obf_csv_rows(csv_path)
    else:
        return result

    for rec in row_iter:
        result.rows_scanned += 1
        raw_code_field = str(rec.get("code") or rec.get("_id") or "").strip()
        if not raw_code_field:
            continue

        primary_name, alt_names = _extract_obf_names(rec)
        brands_tags = rec.get("brands_tags")
        brands_raw = rec.get("brands") or rec.get("brands_en")
        categories_tags = rec.get("categories_tags")
        categories_raw = rec.get("categories") or rec.get("categories_en")

        match = matcher.match_obf_record(
            brands_tags=brands_tags,
            brands_raw=brands_raw,
            product_name=primary_name,
            categories_tags=categories_tags,
            categories_raw=categories_raw if isinstance(categories_raw, str) else None,
            is_beauty_db=is_beauty_db,
        )
        if match is None:
            continue

        if isinstance(match, QuarantineCandidate):
            norm_q = normalize_barcode(raw_code_field)
            result.quarantined.append(
                QuarantineRecord(
                    raw_code=raw_code_field,
                    gtin14=norm_q.gtin14 or None,
                    reason=match.reason,
                    source=source_name,
                    raw_brand=match.raw_brand,
                    raw_title=primary_name,
                )
            )
            continue

        assert isinstance(match, BrandMatch)
        result.raw_matched_count += 1

        # Split concatenated 26/39-char EANs or delimiter-separated code lists
        split_codes = split_concatenated_codes(raw_code_field)
        if not split_codes:
            split_codes = [raw_code_field]

        # Resolve remote product images (explicit fields -> CSV map -> deterministic JSONL)
        img_urls: list[str] = []
        primary_img = _is_valid_http_url(rec.get("image_url"))
        thumb_img = _is_valid_http_url(rec.get("image_small_url"))
        if raw_code_field in csv_images:
            c_img, c_small = csv_images[raw_code_field]
            primary_img = primary_img or c_img
            thumb_img = thumb_img or c_small

        derived_primary, derived_thumb, derived_all = derive_obf_image_urls(
            raw_code_field,
            rec.get("images"),
            host=host,
        )
        primary_img = primary_img or derived_primary
        thumb_img = thumb_img or derived_thumb
        for candidate_url in (primary_img, thumb_img, *derived_all):
            if candidate_url and candidate_url not in img_urls:
                img_urls.append(candidate_url)

        categories = _extract_categories(categories_tags, categories_raw)
        quantity = _clean_text(rec.get("quantity"))
        description = _clean_text(rec.get("generic_name_en") or rec.get("generic_name"))
        raw_lm = rec.get("last_modified_t", 0)
        try:
            lm_t = int(raw_lm)
        except (TypeError, ValueError):
            lm_t = 0

        for sub_code in split_codes:
            norm = normalize_barcode(sub_code)
            if not norm.is_valid:
                result.quarantined.append(
                    QuarantineRecord(
                        raw_code=sub_code,
                        gtin14=norm.gtin14 or None,
                        reason=norm.quarantine_reason or "NON_NUMERIC_OR_INVALID_LENGTH",
                        source=source_name,
                        raw_brand=match.matched_raw_brand,
                        raw_title=primary_name,
                    )
                )
                continue

            if not primary_name:
                result.quarantined.append(
                    QuarantineRecord(
                        raw_code=sub_code,
                        gtin14=norm.gtin14,
                        reason="MISSING_PRODUCT_NAME",
                        source=source_name,
                        raw_brand=match.matched_raw_brand,
                        raw_title=None,
                    )
                )
                continue

            product_url = _is_valid_http_url(rec.get("url")) or f"{web_host}/product/{sub_code}"
            result.candidates.append(
                HarvestedCandidate(
                    source=source_name,
                    gtin14=norm.gtin14,
                    barcode=norm,
                    raw_codes=[sub_code],
                    brand_slug=match.brand_slug,
                    brand_display_name=match.display_name,
                    glow_and_glare_vendor_url=match.glow_and_glare_vendor_url,
                    matched_raw_brand=match.matched_raw_brand,
                    match_method=match.match_method,
                    match_confidence=match.confidence,
                    product_name=primary_name,
                    alternate_names=list(alt_names),
                    quantity=quantity,
                    categories=list(categories),
                    description=description,
                    primary_image_url=primary_img,
                    thumbnail_url=thumb_img,
                    image_urls=list(img_urls),
                    source_url=product_url,
                    last_modified_t=lm_t,
                )
            )

    return result


__all__ = [
    "HarvestedCandidate",
    "OBF_CSV_URL",
    "OBF_JSONL_URL",
    "OPF_CSV_URL",
    "SourceHarvestResult",
    "derive_obf_image_urls",
    "harvest_open_facts",
    "load_csv_image_map",
]
