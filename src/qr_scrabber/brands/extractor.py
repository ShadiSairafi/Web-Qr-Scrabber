"""Authoritative Glow and Glare Shop-by-Brand HTML extractor and deterministic seed loader."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from qr_scrabber.brands.registry import apply_curated_metadata, canonical_brand_slug
from qr_scrabber.http_client import make_sync_client
from qr_scrabber.models import BrandSeedEntry

DEFAULT_SHOP_BY_BRAND_URL = "https://glowandglare.com/pages/shop-by-brand"
DEFAULT_BASE_URL = "https://glowandglare.com"


def _normalize_vendor_url(href: str, base_url: str = DEFAULT_BASE_URL) -> str:
    return urljoin(base_url, href.strip())


def extract_brands_from_html(
    html: str,
    base_url: str = DEFAULT_BASE_URL,
) -> list[BrandSeedEntry]:
    """Extract all unique A-Z Glow and Glare brands and Most Loved flags from Shop-by-Brand HTML.

    Uses BeautifulSoup (`lxml`) so unescaped apostrophes inside double-quoted `href` attributes
    (e.g. `L'Oreal`, `Kiehl's`, `Juvia's Place`, `Paula's Choice`) are preserved intact.
    """
    soup = BeautifulSoup(html, "lxml")

    # Locate the Shop-by-Brand root container (Shopify `.ggb` section or `<main>` fallback)
    container = soup.select_one("div.ggb") or soup.find("main") or soup

    # 1. Identify "Most Loved" featured brand names
    most_loved_names: set[str] = set()
    most_loved_box = container.select_one("div.t")
    if most_loved_box is not None:
        for anchor in most_loved_box.find_all("a", href=True):
            href = str(anchor["href"])
            if "/collections/vendors" in href:
                text = " ".join(anchor.get_text(" ", strip=True).split())
                if text:
                    most_loved_names.add(text.casefold())

    # 2. Extract A-Z brand sections (`id="bA"` .. `id="bZ"`)
    entries: list[BrandSeedEntry] = []
    seen_slugs: set[str] = set()

    letter_blocks = [
        el
        for el in container.find_all(id=True)
        if isinstance(el.get("id"), str)
        and len(el["id"]) == 2
        and el["id"].startswith("b")
        and el["id"][1].isalpha()
        and el["id"][1].isupper()
    ]

    if letter_blocks:
        for block in letter_blocks:
            letter = str(block["id"])[1]
            for anchor in block.find_all("a", href=True):
                href = str(anchor["href"])
                if "/collections/vendors" not in href:
                    continue
                display_name = " ".join(anchor.get_text(" ", strip=True).split())
                if not display_name:
                    continue
                slug = canonical_brand_slug(display_name)
                if not slug or slug in seen_slugs:
                    continue
                seen_slugs.add(slug)
                raw_entry = BrandSeedEntry(
                    slug=slug,
                    display_name=display_name,
                    letter_section=letter,
                    glow_and_glare_vendor_url=_normalize_vendor_url(href, base_url=base_url),
                    is_most_loved=display_name.casefold() in most_loved_names,
                )
                entries.append(apply_curated_metadata(raw_entry))
    else:
        # Fallback for simplified HTML fixtures without `#bA..#bZ` wrappers
        for anchor in container.find_all("a", href=True):
            href = str(anchor["href"])
            if "/collections/vendors" not in href:
                continue
            if most_loved_box is not None and anchor in most_loved_box.descendants:
                continue
            display_name = " ".join(anchor.get_text(" ", strip=True).split())
            if not display_name:
                continue
            slug = canonical_brand_slug(display_name)
            if not slug or slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            letter = display_name[0].upper() if display_name[0].isalpha() else "#"
            raw_entry = BrandSeedEntry(
                slug=slug,
                display_name=display_name,
                letter_section=letter,
                glow_and_glare_vendor_url=_normalize_vendor_url(href, base_url=base_url),
                is_most_loved=display_name.casefold() in most_loved_names,
            )
            entries.append(apply_curated_metadata(raw_entry))

    if not entries:
        raise ValueError("No Glow and Glare brand vendor links were found in the supplied HTML.")

    return entries


def fetch_live_brands(
    url: str = DEFAULT_SHOP_BY_BRAND_URL,
    timeout: float = 20.0,
) -> list[BrandSeedEntry]:
    """Fetch the live Glow and Glare Shop-by-Brand HTML page and extract all brand entries."""
    headers = {
        "User-Agent": "WebQrScrabber/0.1 (+https://github.com/ShadiSairafi/Web-Qr-Scrabber)",
        "Accept": "text/html,application/xhtml+xml",
    }
    with make_sync_client(follow_redirects=True, timeout=timeout, headers=headers) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return extract_brands_from_html(resp.text, base_url=DEFAULT_BASE_URL)


def load_brands_seed(seed_path: Path) -> list[BrandSeedEntry]:
    """Load and validate deterministic brand seed entries from JSON."""
    path = Path(seed_path)
    if not path.is_file():
        raise FileNotFoundError(f"Brand seed file not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "brands" in raw:
        items = raw["brands"]
    elif isinstance(raw, list):
        items = raw
    else:
        raise ValueError(f"Unsupported brand seed JSON structure in {path}")
    return [BrandSeedEntry.model_validate(item) for item in items]


def save_brands_seed(entries: list[BrandSeedEntry], out_path: Path) -> Path:
    """Atomically write brand seed entries as formatted UTF-8 JSON."""
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = [entry.model_dump(mode="json") for entry in entries]
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


def resolve_brand_entries(
    *,
    html_file: Path | None = None,
    url: str | None = None,
    seed_file: Path | None = None,
    offline: bool = False,
) -> list[BrandSeedEntry]:
    """Load authoritative Glow and Glare brand entries from HTML file, live URL, or seed JSON."""
    if html_file is not None:
        path = Path(html_file)
        if not path.is_file():
            raise FileNotFoundError(f"Brands HTML file not found: {path}")
        return extract_brands_from_html(path.read_text(encoding="utf-8"))

    if not offline:
        target_url = url or DEFAULT_SHOP_BY_BRAND_URL
        try:
            return fetch_live_brands(target_url)
        except (httpx.HTTPError, ValueError):
            if seed_file is not None and Path(seed_file).is_file():
                return load_brands_seed(seed_file)
            raise

    if seed_file is not None:
        return load_brands_seed(seed_file)
    raise FileNotFoundError(
        "Offline mode requires either --brands-html or an existing --brands-seed."
    )
