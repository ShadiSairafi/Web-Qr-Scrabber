"""Atomic JSON, QR SVG, product image, and web bundle exporter + catalog verifier."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from PIL import Image, UnidentifiedImageError

from qr_scrabber import __version__
from qr_scrabber.brands.registry import BrandRegistry
from qr_scrabber.gs1 import is_restricted_circulation, verify_gs1_check_digit
from qr_scrabber.harvesters.cache import atomic_write_json
from qr_scrabber.harvesters.images import ProductImageDownloader
from qr_scrabber.models import (
    BrandFilePayload,
    BrandManifestEntry,
    BrandsManifest,
    ComparisonReport,
    ImageDownloadStats,
    ProductRecord,
    QuarantineReport,
    SourceStatusMap,
    SummaryReport,
    UpcitemdbQuota,
)
from qr_scrabber.qr import decode_qr_file, render_qr_code
from qr_scrabber.reconcile import ReconciliationResult

WEB_SRC_DIR = Path(__file__).resolve().parent / "web"
WEB_BUNDLE_FILES: tuple[str, ...] = ("index.html", "index.css", "app.js")
REQUIRED_WEB_HOOKS: tuple[str, ...] = (
    'id="search-input"',
    'id="results-grid"',
    'id="qr-modal"',
    'id="exact-match-card"',
    'id="regex-status"',
    'id="results-count"',
    'id="load-more-btn"',
    'id="empty-state"',
)


def _atomic_copy_file(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    data = src.read_bytes()
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{dst.name}.",
        suffix=".tmp",
        dir=str(dst.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, dst)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return dst


def export_web_assets(output_dir: Path) -> list[Path]:
    """Atomically copy `src/qr_scrabber/web/{index.html,index.css,app.js}` into `output_dir`."""
    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    exported: list[Path] = []
    for fname in WEB_BUNDLE_FILES:
        src = WEB_SRC_DIR / fname
        if src.is_file():
            exported.append(_atomic_copy_file(src, out_root / fname))
    return exported


def render_qr_for_products(
    products: list[ProductRecord],
    output_dir: Path,
    *,
    fmt: Literal["svg", "png"] = "svg",
    limit: int = 0,
    brand_slug_filter: str | None = None,
) -> tuple[list[ProductRecord], int]:
    """Render ISO 18004 QR files for products and update `qr_svg_path` / `qr_png_path`."""
    out_root = Path(output_dir)
    updated: list[ProductRecord] = []
    rendered = 0
    for rec in products:
        if brand_slug_filter and rec.brand.slug != brand_slug_filter:
            updated.append(rec)
            continue
        if limit > 0 and rendered >= limit:
            updated.append(rec)
            continue
        rel = f"qr/{rec.brand.slug}/{rec.gtin14}.{fmt}"
        target = out_root / rel
        if not target.is_file():
            render_qr_code(rec.qr.gs1_digital_link, target, fmt=fmt)
        rendered += 1
        qr_update = {"qr_svg_path": rel} if fmt == "svg" else {"qr_png_path": rel}
        updated.append(rec.model_copy(update={"qr": rec.qr.model_copy(update=qr_update)}))
    return updated, rendered


def export_catalog(
    *,
    registry: BrandRegistry,
    reconciliation: ReconciliationResult,
    output_dir: Path,
    source_status: SourceStatusMap,
    upc_quota: UpcitemdbQuota,
    download_images: bool = False,
    image_cache_dir: Path = Path("data/cache/images"),
    max_images: int = 0,
    image_concurrency: int = 12,
    render_qr: bool = True,
    render_qr_limit: int = 0,
    export_web: bool = True,
    generated_at: str | None = None,
) -> SummaryReport:
    """Export all canonical JSON catalogs, brand files, local images, QR SVGs, and web bundle."""
    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    brands_dir = out_root / "brands"
    brands_dir.mkdir(parents=True, exist_ok=True)

    products = list(reconciliation.products)

    # 1. Optional product thumbnail downloads
    img_stats = ImageDownloadStats()
    if download_images and products:
        downloader = ProductImageDownloader(
            cache_dir=image_cache_dir,
            concurrency=image_concurrency,
        )
        products, img_stats = downloader.download_for_products(
            products,
            out_root,
            max_images=max_images,
        )

    # 2. Optional ISO 18004 SVG QR rendering
    qr_rendered_count = 0
    if render_qr and products:
        products, qr_rendered_count = render_qr_for_products(
            products,
            out_root,
            fmt="svg",
            limit=render_qr_limit,
        )

    # Deterministic final sort
    products.sort(key=lambda r: (r.brand.slug, r.gtin14))

    # Group products by canonical brand slug
    products_by_brand: dict[str, list[ProductRecord]] = {}
    for rec in products:
        products_by_brand.setdefault(rec.brand.slug, []).append(rec)

    # 3. Write per-brand JSON files for every brand in the directory (zero duplicate GTINs!)
    manifest_entries: list[BrandManifestEntry] = []
    brands_with_matches = 0
    brands_zero_matches = 0

    for entry in registry.entries:
        rel_file = f"brands/{entry.slug}.json"
        canonical_slug = registry.resolve_canonical_slug(entry.slug)
        canonical_products = products_by_brand.get(canonical_slug, [])

        if entry.alias_of is not None:
            # Alias brand file carries 0 duplicated products and points to canonical slug
            brand_products: list[ProductRecord] = []
            cross_count = 0
        else:
            brand_products = canonical_products
            cross_count = sum(
                1
                for p in brand_products
                if p.verification.status
                in ("verified_cross_source", "verified_cross_source_subbrand_resolved")
            )

        if len(brand_products) > 0:
            brands_with_matches += 1
        else:
            brands_zero_matches += 1

        payload = BrandFilePayload(
            brand_slug=entry.slug,
            display_name=entry.display_name,
            letter_section=entry.letter_section,
            is_most_loved=entry.is_most_loved,
            is_store_own_brand=entry.is_store_own_brand,
            alias_of=entry.alias_of,
            glow_and_glare_vendor_url=entry.glow_and_glare_vendor_url,
            product_count=len(brand_products),
            canonical_product_count=len(canonical_products),
            cross_source_verified_count=cross_count,
            products=brand_products,
        )
        atomic_write_json(out_root / rel_file, payload.model_dump(mode="json"))

        manifest_entries.append(
            BrandManifestEntry(
                slug=entry.slug,
                display_name=entry.display_name,
                letter_section=entry.letter_section,
                is_most_loved=entry.is_most_loved,
                is_store_own_brand=entry.is_store_own_brand,
                alias_of=entry.alias_of,
                glow_and_glare_vendor_url=entry.glow_and_glare_vendor_url,
                product_count=len(brand_products),
                canonical_product_count=len(canonical_products),
                file_path=rel_file,
            )
        )

    # 4. Write all_products.json, brands_manifest.json, comparison_report.json, quarantine.json
    atomic_write_json(
        out_root / "all_products.json",
        [p.model_dump(mode="json") for p in products],
    )

    manifest = BrandsManifest(
        total_vendor_entries=registry.total_vendor_entries,
        distinct_brand_entities=registry.distinct_brand_entities,
        external_brand_entities=registry.external_brand_entities,
        most_loved_count=registry.most_loved_count,
        brands_with_matches=brands_with_matches,
        brands_zero_matches=brands_zero_matches,
        brands=manifest_entries,
    )
    atomic_write_json(out_root / "brands_manifest.json", manifest.model_dump(mode="json"))
    atomic_write_json(
        out_root / "comparison_report.json",
        reconciliation.comparison_report.model_dump(mode="json"),
    )
    atomic_write_json(
        out_root / "quarantine.json",
        reconciliation.quarantine_report.model_dump(mode="json"),
    )

    # 5. Export web UI bundle when available
    output_files: dict[str, str] = {
        "all_products": "all_products.json",
        "brands_manifest": "brands_manifest.json",
        "comparison_report": "comparison_report.json",
        "quarantine": "quarantine.json",
        "summary": "summary.json",
        "brands_dir": "brands/",
    }
    if render_qr:
        output_files["qr_dir"] = "qr/"
    if download_images or any(p.local_image_path for p in products):
        output_files["images_dir"] = "images/"
    if export_web:
        exported_web = export_web_assets(out_root)
        if exported_web:
            output_files["web_index"] = "index.html"

    # 6. Write summary.json
    cross_total = (
        reconciliation.comparison_report.verified_cross_source_count
        + reconciliation.comparison_report.verified_cross_source_subbrand_resolved_count
    )
    with_remote_img = sum(1 for p in products if p.image_url is not None)
    with_local_img = sum(1 for p in products if p.local_image_path is not None)
    actual_qr_count = sum(1 for p in products if p.qr.qr_svg_path is not None)

    summary = SummaryReport(
        generated_at=generated_at or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        pipeline_version=__version__,
        total_brands_in_directory=registry.total_vendor_entries,
        distinct_brand_entities=registry.distinct_brand_entities,
        total_unique_verified_products=len(products),
        products_with_name_count=len(products),
        products_with_remote_image_count=with_remote_img,
        local_images_downloaded_count=max(with_local_img, img_stats.downloaded),
        cross_source_verified_count=cross_total,
        quarantined_count=reconciliation.quarantine_report.total_quarantined,
        duplicate_gtin14_count=reconciliation.comparison_report.duplicate_gtin14_count,
        qr_files_rendered_count=actual_qr_count or qr_rendered_count,
        source_status=source_status,
        upcitemdb_quota=upc_quota,
        output_files=output_files,
    )
    atomic_write_json(out_root / "summary.json", summary.model_dump(mode="json"))
    return summary


def rewrite_catalog_products(
    output_dir: Path,
    products: list[ProductRecord],
) -> SummaryReport:
    """Update `all_products.json`, `brands/*.json`, `brands_manifest.json`, and `summary.json`."""
    out_root = Path(output_dir)
    products.sort(key=lambda r: (r.brand.slug, r.gtin14))
    by_brand: dict[str, list[ProductRecord]] = {}
    for p in products:
        by_brand.setdefault(p.brand.slug, []).append(p)

    atomic_write_json(
        out_root / "all_products.json",
        [p.model_dump(mode="json") for p in products],
    )

    manifest_raw = json.loads((out_root / "brands_manifest.json").read_text(encoding="utf-8"))
    manifest = BrandsManifest.model_validate(manifest_raw)

    known_slugs: set[str] = set()
    updated_manifest_entries: list[BrandManifestEntry] = []
    canonical_counts: dict[str, int] = {slug: len(items) for slug, items in by_brand.items()}

    for b_entry in manifest.brands:
        known_slugs.add(b_entry.slug)
        b_path = out_root / b_entry.file_path
        if b_entry.alias_of is None:
            new_prods = by_brand.get(b_entry.slug, [])
            cross_count = sum(
                1
                for p in new_prods
                if p.verification.status
                in ("verified_cross_source", "verified_cross_source_subbrand_resolved")
            )
            if b_path.is_file():
                bf = BrandFilePayload.model_validate(json.loads(b_path.read_text(encoding="utf-8")))
                bf = bf.model_copy(
                    update={
                        "product_count": len(new_prods),
                        "canonical_product_count": len(new_prods),
                        "cross_source_verified_count": cross_count,
                        "products": new_prods,
                    }
                )
                atomic_write_json(b_path, bf.model_dump(mode="json"))
            updated_manifest_entries.append(
                b_entry.model_copy(
                    update={
                        "product_count": len(new_prods),
                        "canonical_product_count": len(new_prods),
                    }
                )
            )
        else:
            canon_count = canonical_counts.get(b_entry.alias_of, b_entry.canonical_product_count)
            updated_manifest_entries.append(
                b_entry.model_copy(update={"canonical_product_count": canon_count})
            )

    # Support newly persisted external brands discovered via on-demand live lookup
    for slug, brand_prods in sorted(by_brand.items()):
        if slug in known_slugs:
            continue
        sample = brand_prods[0]
        rel_file = f"brands/{slug}.json"
        first_char = (sample.brand.display_name or slug)[0].upper()
        letter = first_char if "A" <= first_char <= "Z" else "#"
        cross_count = sum(
            1
            for p in brand_prods
            if p.verification.status
            in ("verified_cross_source", "verified_cross_source_subbrand_resolved")
        )
        bf = BrandFilePayload(
            brand_slug=slug,
            display_name=sample.brand.display_name,
            letter_section=letter,
            is_most_loved=False,
            is_store_own_brand=False,
            alias_of=None,
            glow_and_glare_vendor_url=sample.brand.glow_and_glare_vendor_url,
            product_count=len(brand_prods),
            canonical_product_count=len(brand_prods),
            cross_source_verified_count=cross_count,
            products=brand_prods,
        )
        atomic_write_json(out_root / rel_file, bf.model_dump(mode="json"))
        updated_manifest_entries.append(
            BrandManifestEntry(
                slug=slug,
                display_name=sample.brand.display_name,
                letter_section=letter,
                is_most_loved=False,
                is_store_own_brand=False,
                alias_of=None,
                glow_and_glare_vendor_url=sample.brand.glow_and_glare_vendor_url,
                product_count=len(brand_prods),
                canonical_product_count=len(brand_prods),
                file_path=rel_file,
            )
        )

    brands_with_matches = sum(1 for e in updated_manifest_entries if e.product_count > 0)
    brands_zero_matches = len(updated_manifest_entries) - brands_with_matches
    updated_manifest = manifest.model_copy(
        update={
            "brands_with_matches": brands_with_matches,
            "brands_zero_matches": brands_zero_matches,
            "brands": updated_manifest_entries,
        }
    )
    atomic_write_json(out_root / "brands_manifest.json", updated_manifest.model_dump(mode="json"))

    summary_raw = json.loads((out_root / "summary.json").read_text(encoding="utf-8"))
    summary = SummaryReport.model_validate(summary_raw)
    local_img_count = sum(1 for p in products if p.local_image_path is not None)
    qr_count = sum(
        1 for p in products if (p.qr.qr_svg_path or p.qr.qr_png_path) is not None
    )
    cross_total = sum(
        1
        for p in products
        if p.verification.status
        in ("verified_cross_source", "verified_cross_source_subbrand_resolved")
    )
    updated_files = dict(summary.output_files)
    if local_img_count > 0:
        updated_files["images_dir"] = "images/"
    if qr_count > 0:
        updated_files["qr_dir"] = "qr/"
    updated_summary = summary.model_copy(
        update={
            "total_unique_verified_products": len(products),
            "products_with_name_count": len(products),
            "products_with_remote_image_count": sum(1 for p in products if p.image_url is not None),
            "local_images_downloaded_count": local_img_count,
            "cross_source_verified_count": cross_total,
            "qr_files_rendered_count": qr_count,
            "output_files": updated_files,
        }
    )
    atomic_write_json(out_root / "summary.json", updated_summary.model_dump(mode="json"))
    return updated_summary


def verify_output_dir(
    output_dir: Path,
    *,
    check_qr_files: bool = False,
    check_images: bool = False,
    check_web: bool = False,
) -> list[str]:
    """Validate all JSON schemas, GS1/name/uniqueness invariants, QR files, images, and web UI."""
    errors: list[str] = []
    out_root = Path(output_dir)
    if not out_root.is_dir():
        return [f"Output directory does not exist: {out_root}"]

    required_json = (
        "all_products.json",
        "brands_manifest.json",
        "comparison_report.json",
        "quarantine.json",
        "summary.json",
    )
    for fname in required_json:
        if not (out_root / fname).is_file():
            errors.append(f"Missing required output file: {fname}")
    if errors:
        return errors

    try:
        raw_products = json.loads((out_root / "all_products.json").read_text(encoding="utf-8"))
        products = [ProductRecord.model_validate(item) for item in raw_products]
    except Exception as exc:
        return [f"Invalid all_products.json: {exc}"]

    try:
        manifest = BrandsManifest.model_validate(
            json.loads((out_root / "brands_manifest.json").read_text(encoding="utf-8"))
        )
        comparison = ComparisonReport.model_validate(
            json.loads((out_root / "comparison_report.json").read_text(encoding="utf-8"))
        )
        QuarantineReport.model_validate(
            json.loads((out_root / "quarantine.json").read_text(encoding="utf-8"))
        )
        summary = SummaryReport.model_validate(
            json.loads((out_root / "summary.json").read_text(encoding="utf-8"))
        )
    except Exception as exc:
        return [f"Invalid report/manifest JSON schema: {exc}"]

    # 1. Global uniqueness & GS1 & product_name checks on all_products.json
    seen_gtins: set[str] = set()
    for rec in products:
        if rec.gtin14 in seen_gtins:
            errors.append(f"Duplicate GTIN-14 in all_products.json: {rec.gtin14}")
        seen_gtins.add(rec.gtin14)

        if not rec.product_name.strip() or rec.product_name != rec.product.name:
            errors.append(f"Empty or mismatched product_name for {rec.gtin14}")

        if not verify_gs1_check_digit(rec.gtin14):
            errors.append(f"Invalid GS1 Modulo-10 check digit: {rec.gtin14}")

        if is_restricted_circulation(rec.barcode.primary_code, rec.gtin14):
            errors.append(f"Restricted circulation barcode leaked into clean catalog: {rec.gtin14}")

    if summary.duplicate_gtin14_count != 0 or comparison.duplicate_gtin14_count != 0:
        errors.append("Summary or comparison report reports non-zero duplicate_gtin14_count.")

    if summary.total_unique_verified_products != len(products):
        errors.append(
            f"summary.total_unique_verified_products ({summary.total_unique_verified_products}) "
            f"!= len(all_products.json) ({len(products)})"
        )

    # 2. Validate every per-brand file in manifest & zero duplicates across brand files
    brand_gtins: set[str] = set()
    for entry in manifest.brands:
        bpath = out_root / entry.file_path
        if not bpath.is_file():
            errors.append(f"Missing per-brand file: {entry.file_path}")
            continue
        try:
            bf = BrandFilePayload.model_validate(json.loads(bpath.read_text(encoding="utf-8")))
        except Exception as exc:
            errors.append(f"Invalid brand file {entry.file_path}: {exc}")
            continue

        if bf.alias_of is not None and bf.products:
            errors.append(f"Alias brand file {entry.file_path} must not duplicate products.")
        if bf.product_count != len(bf.products):
            errors.append(f"product_count mismatch in {entry.file_path}")

        for prod in bf.products:
            if prod.gtin14 in brand_gtins:
                errors.append(f"Duplicate GTIN-14 across brands/*.json: {prod.gtin14}")
            brand_gtins.add(prod.gtin14)

    if brand_gtins != seen_gtins:
        errors.append(
            f"Mismatch between all_products.json GTINs ({len(seen_gtins)}) "
            f"and union of brands/*.json ({len(brand_gtins)})"
        )

    # 3. Optional QR optical decode verification
    if check_qr_files:
        qr_items = [p for p in products if p.qr.qr_svg_path or p.qr.qr_png_path]
        for p in qr_items:
            rel = p.qr.qr_svg_path or p.qr.qr_png_path
            assert rel is not None
            qpath = out_root / rel
            if not qpath.is_file():
                errors.append(f"Missing rendered QR file: {rel}")
        # Optically decode all when <= 100, or a representative 60-item stride when large
        if len(qr_items) <= 100:
            decode_targets = qr_items
        else:
            step = max(1, len(qr_items) // 60)
            decode_targets = qr_items[::step][:60]
        for p in decode_targets:
            rel = p.qr.qr_svg_path or p.qr.qr_png_path
            assert rel is not None
            qpath = out_root / rel
            if not qpath.is_file():
                continue
            try:
                decoded = decode_qr_file(qpath)
                if p.qr.gs1_digital_link not in decoded:
                    errors.append(
                        f"QR decode mismatch for {rel}: expected {p.qr.gs1_digital_link}, "
                        f"got {decoded}"
                    )
            except Exception as exc:
                errors.append(f"QR optical decode failed for {rel}: {exc}")

    # 4. Optional local image Pillow verification
    if check_images:
        for p in products:
            if p.local_image_path is None:
                continue
            img_path = out_root / p.local_image_path
            if not img_path.is_file():
                errors.append(f"Missing local product image: {p.local_image_path}")
                continue
            try:
                with Image.open(img_path) as im:
                    im.load()
                    if im.width <= 0 or im.height <= 0:
                        errors.append(f"Zero-dimension local image: {p.local_image_path}")
            except (OSError, UnidentifiedImageError) as exc:
                errors.append(f"Corrupt local image {p.local_image_path}: {exc}")

    # 5. Optional Web UI bundle verification
    if check_web:
        for fname in WEB_BUNDLE_FILES:
            fpath = out_root / fname
            if not fpath.is_file():
                errors.append(f"Missing web UI bundle file: {fname}")
        index_path = out_root / "index.html"
        if index_path.is_file():
            html_text = index_path.read_text(encoding="utf-8")
            for hook in REQUIRED_WEB_HOOKS:
                if hook not in html_text:
                    errors.append(f"Web UI index.html missing required DOM hook: {hook}")

    return errors


__all__ = [
    "REQUIRED_WEB_HOOKS",
    "WEB_BUNDLE_FILES",
    "export_catalog",
    "export_web_assets",
    "render_qr_for_products",
    "rewrite_catalog_products",
    "verify_output_dir",
]
