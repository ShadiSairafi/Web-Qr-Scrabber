"""Glow and Glare brand extraction, registry, and 4-tier disambiguation package."""

from qr_scrabber.brands.extractor import (
    DEFAULT_SHOP_BY_BRAND_URL,
    extract_brands_from_html,
    fetch_live_brands,
    load_brands_seed,
    resolve_brand_entries,
    save_brands_seed,
)
from qr_scrabber.brands.matcher import BrandMatcher
from qr_scrabber.brands.registry import (
    BrandRegistry,
    apply_curated_metadata,
    brand_slug_variants,
    canonical_brand_slug,
    compact_brand_key,
    fold_unicode,
    spaced_brand_slug,
    strip_lang_prefix,
)

__all__ = [
    "BrandMatcher",
    "BrandRegistry",
    "DEFAULT_SHOP_BY_BRAND_URL",
    "apply_curated_metadata",
    "brand_slug_variants",
    "canonical_brand_slug",
    "compact_brand_key",
    "extract_brands_from_html",
    "fetch_live_brands",
    "fold_unicode",
    "load_brands_seed",
    "resolve_brand_entries",
    "save_brands_seed",
    "spaced_brand_slug",
    "strip_lang_prefix",
]
