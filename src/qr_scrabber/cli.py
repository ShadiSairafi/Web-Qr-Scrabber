"""Command-line interface for `qr-scrabber`."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from qr_scrabber import __version__
from qr_scrabber.brands.extractor import (
    DEFAULT_SHOP_BY_BRAND_URL,
    extract_brands_from_html,
    fetch_live_brands,
    load_brands_seed,
)
from qr_scrabber.brands.matcher import BrandMatcher
from qr_scrabber.brands.registry import BrandRegistry
from qr_scrabber.exporter import (
    export_catalog,
    export_web_assets,
    render_qr_for_products,
    rewrite_catalog_products,
    verify_output_dir,
)
from qr_scrabber.harvesters.barcodelookup_probe import probe_sources
from qr_scrabber.harvesters.brand_storefronts import (
    DEFAULT_STOREFRONT_CACHE,
    harvest_brand_storefronts,
)
from qr_scrabber.harvesters.cache import atomic_write_json, download_bulk_file
from qr_scrabber.harvesters.images import ProductImageDownloader
from qr_scrabber.harvesters.openbeautyfacts import (
    OBF_CSV_URL,
    OBF_JSONL_URL,
    OPF_CSV_URL,
    SourceHarvestResult,
    harvest_open_facts,
)
from qr_scrabber.harvesters.upcitemdb import UPCItemDBHarvester
from qr_scrabber.models import (
    BrandManifestEntry,
    BrandsManifest,
    ProductRecord,
)
from qr_scrabber.reconcile import reconcile_sources
from qr_scrabber.server import run_server

DEFAULT_BRANDS_URL = DEFAULT_SHOP_BY_BRAND_URL
DEFAULT_BRANDS_SEED = Path("data/brands_seed.json")
DEFAULT_UPC_CACHE = Path("data/upcitemdb_cached_responses.json")
DEFAULT_UPC_BASE_URL = "https://api.upcitemdb.com/prod/trial"
DEFAULT_OUTPUT_DIR = Path("output")
DEFAULT_RAW_DUMPS_DIR = Path("data/raw_dumps")
DEFAULT_IMAGE_CACHE_DIR = Path("data/cache/images")


class QRScrabberError(Exception):
    """Domain or runtime error formatted cleanly to stderr with exit code 1."""


def _add_pipeline_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Target output directory (default: output).",
    )
    parser.add_argument(
        "--brands-html",
        type=Path,
        default=None,
        help="Optional local HTML snapshot of Glow and Glare Shop-by-Brand.",
    )
    parser.add_argument(
        "--brands-seed",
        type=Path,
        default=DEFAULT_BRANDS_SEED,
        help="Deterministic brand seed JSON path (default: data/brands_seed.json).",
    )
    parser.add_argument(
        "--obf-jsonl",
        type=Path,
        default=None,
        help="Optional local Open Beauty Facts .jsonl or .jsonl.gz file.",
    )
    parser.add_argument(
        "--obf-csv",
        type=Path,
        default=None,
        help="Optional local Open Beauty Facts .csv or .csv.gz file.",
    )
    parser.add_argument(
        "--opf-jsonl",
        type=Path,
        default=None,
        help="Optional local Open Products Facts .jsonl or .jsonl.gz file.",
    )
    parser.add_argument(
        "--opf-csv",
        type=Path,
        default=None,
        help="Optional local Open Products Facts .csv or .csv.gz file.",
    )
    parser.add_argument(
        "--no-opf",
        action="store_true",
        help="Disable optional secondary Open Products Facts enrichment.",
    )
    parser.add_argument(
        "--upc-cache",
        type=Path,
        default=DEFAULT_UPC_CACHE,
        help="Persistent UPCitemdb response cache path.",
    )
    parser.add_argument(
        "--upc-base-url",
        type=str,
        default=DEFAULT_UPC_BASE_URL,
        help="UPCitemdb base URL (supports local mock HTTP server overrides).",
    )
    parser.add_argument(
        "--max-upc-lookups",
        type=int,
        default=30,
        help="Maximum consolidated UPCitemdb batch lookup requests (default: 30).",
    )
    parser.add_argument(
        "--max-upc-searches",
        type=int,
        default=15,
        help="Maximum consolidated UPCitemdb search requests (default: 15).",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Run offline with zero outbound network requests.",
    )
    parser.add_argument(
        "--download-images",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Download and Pillow-verify compact product thumbnails into "
            "<output-dir>/images/<slug>/<gtin14>.<ext> (default: enabled when not --offline)."
        ),
    )
    parser.add_argument(
        "--max-images",
        type=int,
        default=0,
        help="Optional cap on downloaded product images (0 means all products with image URLs).",
    )
    parser.add_argument(
        "--image-concurrency",
        type=int,
        default=12,
        help="Bounded async concurrency for product image downloads (default: 12).",
    )
    parser.add_argument(
        "--render-qr",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Render ISO 18004 SVG QR codes under <output-dir>/qr/<slug>/<gtin14>.svg.",
    )
    parser.add_argument(
        "--brand-storefronts",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Include official brand Shopify storefront GS1 barcodes "
            "(default: enabled unless custom --obf-jsonl/--obf-csv fixture is supplied)."
        ),
    )
    parser.add_argument(
        "--storefront-cache",
        type=Path,
        default=DEFAULT_STOREFRONT_CACHE,
        help="Persistent cache path for official brand storefront harvests.",
    )
    parser.add_argument(
        "--refresh-storefronts",
        action="store_true",
        help="Force a live refresh of official brand Shopify storefronts.",
    )
    parser.add_argument(
        "--render-qr-limit",
        type=int,
        default=0,
        help="Optional cap on rendered QR files (0 means all matched products).",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level `qr-scrabber` CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="qr-scrabber",
        description=(
            "Extract the Glow and Glare brand directory, harvest and reconcile external "
            "GS1 barcodes and GS1 Digital Link QR representations, and export JSON catalogs."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="<subcommand>")

    sync_parser = subparsers.add_parser(
        "sync-brands",
        help="Extract and normalize the 163 Glow and Glare brands.",
    )
    source_group = sync_parser.add_mutually_exclusive_group()
    source_group.add_argument(
        "--html-file",
        type=Path,
        default=None,
        help="Local HTML snapshot of Glow and Glare Shop-by-Brand.",
    )
    source_group.add_argument(
        "--url",
        type=str,
        default=None,
        help=f"Live Shop-by-Brand URL (default: {DEFAULT_BRANDS_URL}).",
    )
    sync_parser.add_argument(
        "--seed-file",
        type=Path,
        default=DEFAULT_BRANDS_SEED,
        help="Fallback deterministic seed JSON path.",
    )
    sync_parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUTPUT_DIR / "brands_manifest.json",
        help="Output manifest JSON path (default: output/brands_manifest.json).",
    )
    sync_parser.add_argument(
        "--offline",
        action="store_true",
        help="Use local --html-file or --seed-file without outbound HTTP requests.",
    )

    check_parser = subparsers.add_parser(
        "check-sources",
        help="Probe reachability of Open Beauty Facts, UPCitemdb, and barcodelookup.com.",
    )
    check_parser.add_argument(
        "--offline",
        action="store_true",
        help="Report cached/configured source status without outbound network requests.",
    )
    check_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON diagnostics to stdout.",
    )
    check_parser.add_argument(
        "--upc-base-url",
        type=str,
        default=DEFAULT_UPC_BASE_URL,
        help="UPCitemdb base URL to check.",
    )

    harvest_parser = subparsers.add_parser(
        "harvest",
        help="Harvest raw candidate barcodes from Open Beauty Facts and UPCitemdb.",
    )
    _add_pipeline_arguments(harvest_parser)

    build_catalog_parser = subparsers.add_parser(
        "build-catalog",
        help="Reconcile harvested barcodes, deduplicate GTINs, and export JSON files.",
    )
    _add_pipeline_arguments(build_catalog_parser)

    run_all_parser = subparsers.add_parser(
        "run-all",
        help="Run end-to-end brand sync, barcode harvest, reconciliation, and export.",
    )
    _add_pipeline_arguments(run_all_parser)

    fetch_images_parser = subparsers.add_parser(
        "fetch-images",
        help="Download and Pillow-verify local product thumbnails for exported products.",
    )
    fetch_images_parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Catalog output directory containing all_products.json (default: output).",
    )
    fetch_images_parser.add_argument(
        "--brand",
        type=str,
        default=None,
        help="Optional brand slug filter.",
    )
    fetch_images_parser.add_argument(
        "--max-images",
        type=int,
        default=0,
        help="Optional maximum number of product images to download (0 means unlimited).",
    )
    fetch_images_parser.add_argument(
        "--concurrency",
        type=int,
        default=12,
        help="Bounded async concurrency for image downloads (default: 12).",
    )

    render_qr_parser = subparsers.add_parser(
        "render-qr",
        help="Render ISO 18004 SVG or PNG QR code files for exported products.",
    )
    render_qr_parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Catalog output directory containing all_products.json.",
    )
    render_qr_parser.add_argument(
        "--brand",
        type=str,
        default=None,
        help="Optional brand slug filter.",
    )
    render_qr_parser.add_argument(
        "--format",
        choices=("svg", "png"),
        default="svg",
        help="QR output image format (default: svg).",
    )
    render_qr_parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional maximum number of QR files to render (0 means unlimited).",
    )

    build_web_parser = subparsers.add_parser(
        "build-web",
        help="Export static single-search-bar web application bundle into <output-dir>/.",
    )
    build_web_parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Target catalog output directory (default: output).",
    )

    serve_parser = subparsers.add_parser(
        "serve",
        help="Start multi-threaded HTTP server serving the web UI, JSON catalogs, images, and QRs.",
    )
    serve_parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Catalog output directory to serve (default: output).",
    )
    serve_parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Bind host interface (default: 127.0.0.1).",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Bind TCP port (default: 8765; 0 selects an ephemeral free port).",
    )

    verify_parser = subparsers.add_parser(
        "verify",
        help=(
            "Validate output catalog schemas, product names, GS1 check digits, uniqueness, "
            "QR files, local images, and web UI bundle."
        ),
    )
    verify_parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Catalog output directory to verify (default: output).",
    )
    verify_parser.add_argument(
        "--check-qr-files",
        action="store_true",
        help="Optically decode rendered QR files to verify GS1 Digital Link payloads.",
    )
    verify_parser.add_argument(
        "--check-images",
        action="store_true",
        help="Verify every non-null local_image_path decodes cleanly via Pillow.",
    )
    verify_parser.add_argument(
        "--check-web",
        action="store_true",
        help="Verify index.html, index.css, app.js, and required DOM hooks exist in <output-dir>/.",
    )

    return parser


def _is_local_url(url: str) -> bool:
    lowered = url.lower()
    return "127.0.0.1" in lowered or "localhost" in lowered or "[::1]" in lowered


def _load_registry(
    *,
    brands_html: Path | None,
    brands_seed: Path,
    brands_url: str | None = None,
    offline: bool = False,
) -> BrandRegistry:
    if brands_html is not None:
        if not brands_html.is_file():
            raise QRScrabberError(f"Brands HTML file not found: {brands_html}")
        entries = extract_brands_from_html(brands_html.read_text(encoding="utf-8"))
        return BrandRegistry.from_entries(entries)

    if brands_url is not None:
        if offline:
            raise QRScrabberError("Cannot fetch --url when --offline is enabled.")
        entries = fetch_live_brands(url=brands_url)
        return BrandRegistry.from_entries(entries)

    if brands_seed.is_file():
        return BrandRegistry.from_entries(load_brands_seed(brands_seed))

    if offline:
        raise QRScrabberError(f"Offline mode active and brand seed file not found: {brands_seed}")

    entries = fetch_live_brands(url=DEFAULT_BRANDS_URL)
    return BrandRegistry.from_entries(entries)


def _select_upc_lookup_barcodes(
    registry: BrandRegistry,
    obf_result: SourceHarvestResult,
    max_lookups: int,
) -> list[str]:
    """Select diverse OBF barcodes (prioritizing US UPC-A & Most Loved brands) for batch lookup."""
    if max_lookups <= 0 or not obf_result.candidates:
        return []
    target_count = max_lookups * 2

    most_loved_slugs = {
        e.slug for e in registry.entries if e.alias_of is None and e.is_most_loved
    }
    by_brand: dict[str, list[str]] = {}
    seen_codes: set[str] = set()

    # Prefer UPC-A (12-digit, highest UPCitemdb hit rate), then EAN-13
    sorted_candidates = sorted(
        obf_result.candidates,
        key=lambda c: (
            0 if c.brand_slug in most_loved_slugs else 1,
            0 if c.barcode.upc_a else 1,
            0 if c.primary_image_url else 1,
            c.brand_slug,
            c.gtin14,
        ),
    )

    for cand in sorted_candidates:
        code = cand.barcode.upc_a or cand.barcode.ean13 or cand.barcode.primary_code
        if not code or code in seen_codes:
            continue
        seen_codes.add(code)
        by_brand.setdefault(cand.brand_slug, []).append(code)

    # Round-robin across brands so cross-verification spans as many Glow & Glare brands as possible
    brand_order = sorted(
        by_brand.keys(),
        key=lambda slug: (0 if slug in most_loved_slugs else 1, slug),
    )
    selected: list[str] = []
    round_idx = 0
    while len(selected) < target_count:
        added_any = False
        for slug in brand_order:
            codes = by_brand[slug]
            if round_idx < len(codes):
                selected.append(codes[round_idx])
                added_any = True
                if len(selected) >= target_count:
                    break
        if not added_any:
            break
        round_idx += 1

    return selected


def _select_upc_search_queries(
    registry: BrandRegistry,
    obf_result: SourceHarvestResult,
    max_searches: int,
) -> list[str]:
    """Select consolidated UPCitemdb search queries targeting Most Loved & low-OBF brands."""
    if max_searches <= 0:
        return []

    obf_counts: dict[str, int] = {}
    for cand in obf_result.candidates:
        obf_counts[cand.brand_slug] = obf_counts.get(cand.brand_slug, 0) + 1

    eligible = [
        entry
        for entry in registry.entries
        if not entry.is_store_own_brand and entry.alias_of is None
    ]

    # Prioritize: Most Loved with 0/few OBF items, then other well-known low-OBF brands
    eligible.sort(
        key=lambda e: (
            0 if (e.is_most_loved and obf_counts.get(e.slug, 0) < 5) else 1,
            0 if e.is_most_loved else 1,
            0 if (e.risk_tier == "A" and obf_counts.get(e.slug, 0) == 0) else 1,
            obf_counts.get(e.slug, 0),
            e.slug,
        )
    )

    queries: list[str] = []
    for entry in eligible:
        if len(queries) >= max_searches:
            break
        queries.append(entry.display_name)
    return queries


def _cmd_sync_brands(args: argparse.Namespace) -> int:
    if args.html_file is not None:
        if not args.html_file.is_file():
            raise QRScrabberError(f"HTML file not found: {args.html_file}")
        entries = extract_brands_from_html(args.html_file.read_text(encoding="utf-8"))
    elif args.url is not None:
        if args.offline:
            raise QRScrabberError("Cannot use --url with --offline.")
        entries = fetch_live_brands(url=args.url)
    elif args.offline:
        if not args.seed_file.is_file():
            raise QRScrabberError(f"Seed file not found: {args.seed_file}")
        entries = load_brands_seed(args.seed_file)
    else:
        entries = fetch_live_brands(url=DEFAULT_BRANDS_URL)

    registry = BrandRegistry.from_entries(entries)
    manifest = BrandsManifest(
        total_vendor_entries=registry.total_vendor_entries,
        distinct_brand_entities=registry.distinct_brand_entities,
        external_brand_entities=registry.external_brand_entities,
        most_loved_count=registry.most_loved_count,
        brands_with_matches=0,
        brands_zero_matches=registry.total_vendor_entries,
        brands=[
            BrandManifestEntry(
                slug=e.slug,
                display_name=e.display_name,
                letter_section=e.letter_section,
                is_most_loved=e.is_most_loved,
                is_store_own_brand=e.is_store_own_brand,
                alias_of=e.alias_of,
                glow_and_glare_vendor_url=e.glow_and_glare_vendor_url,
                product_count=0,
                canonical_product_count=0,
                file_path=f"brands/{e.slug}.json",
            )
            for e in registry.entries
        ],
    )
    atomic_write_json(args.out, manifest.model_dump(mode="json"))
    print(
        f"Synced {registry.total_vendor_entries} Glow & Glare brand entries "
        f"({registry.distinct_brand_entities} distinct entities) -> {args.out}"
    )
    return 0


def _cmd_check_sources(args: argparse.Namespace) -> int:
    status_map = probe_sources(offline=args.offline, upc_base_url=args.upc_base_url)
    if args.json:
        print(json.dumps(status_map.model_dump(mode="json"), indent=2))
    else:
        for name, entry in status_map.model_dump(mode="json").items():
            http_part = f" (HTTP {entry['http_status']})" if entry.get("http_status") else ""
            print(f"{name}: {entry['status']}{http_part} — {entry.get('detail', '')}")
    return 0


def _merge_open_facts(
    primary: SourceHarvestResult,
    secondary: SourceHarvestResult,
) -> SourceHarvestResult:
    return SourceHarvestResult(
        source=primary.source,
        rows_scanned=primary.rows_scanned + secondary.rows_scanned,
        raw_matched_count=primary.raw_matched_count + secondary.raw_matched_count,
        candidates=[*primary.candidates, *secondary.candidates],
        quarantined=[*primary.quarantined, *secondary.quarantined],
    )


def _cmd_pipeline(args: argparse.Namespace, *, export: bool) -> int:
    registry = _load_registry(
        brands_html=args.brands_html,
        brands_seed=args.brands_seed,
        offline=args.offline,
    )
    matcher = BrandMatcher(registry)

    custom_obf_supplied = args.obf_jsonl is not None or args.obf_csv is not None
    obf_jsonl_path = args.obf_jsonl
    obf_csv_path = args.obf_csv

    if not custom_obf_supplied:
        default_jsonl = DEFAULT_RAW_DUMPS_DIR / "openbeautyfacts-products.jsonl.gz"
        default_csv = DEFAULT_RAW_DUMPS_DIR / "en.openbeautyfacts.org.products.csv.gz"
        if args.offline:
            if default_jsonl.is_file():
                obf_jsonl_path = default_jsonl
            if default_csv.is_file():
                obf_csv_path = default_csv
        else:
            obf_jsonl_path = download_bulk_file(
                OBF_JSONL_URL,
                default_jsonl,
                offline=False,
            ).path
            try:
                obf_csv_path = download_bulk_file(
                    OBF_CSV_URL,
                    default_csv,
                    offline=False,
                ).path
            except Exception:
                obf_csv_path = default_csv if default_csv.is_file() else None

    if obf_jsonl_path is not None and not obf_jsonl_path.is_file():
        raise QRScrabberError(f"Open Beauty Facts JSONL file not found: {obf_jsonl_path}")
    if obf_csv_path is not None and not obf_csv_path.is_file():
        raise QRScrabberError(f"Open Beauty Facts CSV file not found: {obf_csv_path}")

    obf_result = harvest_open_facts(
        matcher=matcher,
        jsonl_path=obf_jsonl_path,
        csv_path=obf_csv_path,
        source_name="openbeautyfacts",
    )

    # Optional Open Products Facts secondary bulk enrichment
    opf_result: SourceHarvestResult | None = None
    if not args.no_opf:
        opf_jsonl: Path | None = args.opf_jsonl
        opf_csv: Path | None = args.opf_csv
        if opf_jsonl is None and opf_csv is None and not custom_obf_supplied:
            default_opf = DEFAULT_RAW_DUMPS_DIR / "en.openproductsfacts.org.products.csv.gz"
            if args.offline:
                opf_csv = default_opf if default_opf.is_file() else None
            else:
                try:
                    opf_csv = download_bulk_file(
                        OPF_CSV_URL,
                        default_opf,
                        offline=False,
                    ).path
                except Exception:
                    opf_csv = default_opf if default_opf.is_file() else None
        if opf_jsonl is not None and not opf_jsonl.is_file():
            raise QRScrabberError(f"Open Products Facts JSONL file not found: {opf_jsonl}")
        if opf_csv is not None and not opf_csv.is_file():
            raise QRScrabberError(f"Open Products Facts CSV file not found: {opf_csv}")
        if opf_jsonl is not None or opf_csv is not None:
            opf_result = harvest_open_facts(
                matcher=matcher,
                jsonl_path=opf_jsonl,
                csv_path=opf_csv,
                source_name="openproductsfacts",
            )

    # Consolidated UPCitemdb batch lookup + budgeted search + persistent cache replay
    combined_obf_for_selection = (
        _merge_open_facts(obf_result, opf_result) if opf_result is not None else obf_result
    )
    lookup_upcs = _select_upc_lookup_barcodes(
        registry, combined_obf_for_selection, args.max_upc_lookups
    )
    search_queries = _select_upc_search_queries(
        registry, combined_obf_for_selection, args.max_upc_searches
    )
    upc_harvester = UPCItemDBHarvester(
        cache_path=args.upc_cache,
        base_url=args.upc_base_url,
        offline=args.offline,
    )
    upc_out = upc_harvester.harvest(
        matcher=matcher,
        lookup_upcs=lookup_upcs,
        search_queries=search_queries,
        max_lookups=args.max_upc_lookups,
        max_searches=args.max_upc_searches,
        replay_all_cached=True,
    )

    # Optional official brand Shopify storefront harvest (cached for offline replay)
    storefront_result: SourceHarvestResult | None = None
    use_storefronts = (
        args.brand_storefronts
        if args.brand_storefronts is not None
        else (not custom_obf_supplied)
    )
    if use_storefronts:
        storefront_result = harvest_brand_storefronts(
            registry=registry,
            cache_path=args.storefront_cache,
            offline=args.offline,
            refresh=args.refresh_storefronts,
        )

    if not export:
        sf_count = len(storefront_result.candidates) if storefront_result is not None else 0
        print(
            f"Harvest complete: OBF candidates={len(obf_result.candidates)} "
            f"(quarantined={len(obf_result.quarantined)}), "
            f"Storefront candidates={sf_count}, "
            f"UPCitemdb candidates={len(upc_out.harvest.candidates)} "
            f"(requests={upc_out.quota.requests_made_this_run}, "
            f"cache_hits={upc_out.quota.cache_hits})"
        )
        return 0

    reconciliation = reconcile_sources(
        registry=registry,
        obf_result=obf_result,
        upc_result=upc_out.harvest,
        opf_result=opf_result,
        storefront_result=storefront_result,
    )

    should_probe_offline = args.offline or _is_local_url(args.upc_base_url) or custom_obf_supplied
    source_status = probe_sources(offline=should_probe_offline, upc_base_url=args.upc_base_url)

    download_images = (
        args.download_images if args.download_images is not None else (not args.offline)
    )

    summary = export_catalog(
        registry=registry,
        reconciliation=reconciliation,
        output_dir=args.output_dir,
        source_status=source_status,
        upc_quota=upc_out.quota,
        download_images=download_images,
        image_cache_dir=DEFAULT_IMAGE_CACHE_DIR,
        max_images=args.max_images,
        image_concurrency=args.image_concurrency,
        render_qr=args.render_qr,
        render_qr_limit=args.render_qr_limit,
        export_web=True,
    )

    print(
        f"Catalog exported to {args.output_dir}: "
        f"{summary.total_unique_verified_products} unique verified products, "
        f"{summary.cross_source_verified_count} cross-source verified, "
        f"{summary.local_images_downloaded_count} local images, "
        f"{summary.qr_files_rendered_count} QR SVGs, "
        f"{summary.quarantined_count} quarantined, "
        f"{summary.duplicate_gtin14_count} duplicates."
    )
    return 0


def _load_catalog_products(output_dir: Path) -> list[ProductRecord]:
    all_path = output_dir / "all_products.json"
    if not all_path.is_file():
        raise QRScrabberError(f"Catalog file not found: {all_path}")
    raw = json.loads(all_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise QRScrabberError(f"Invalid all_products.json payload in {all_path}")
    return [ProductRecord.model_validate(item) for item in raw]


def _cmd_fetch_images(args: argparse.Namespace) -> int:
    products = _load_catalog_products(args.output_dir)
    downloader = ProductImageDownloader(
        cache_dir=DEFAULT_IMAGE_CACHE_DIR,
        concurrency=args.concurrency,
    )
    updated, stats = downloader.download_for_products(
        products,
        args.output_dir,
        max_images=args.max_images,
        brand_slug_filter=args.brand,
    )
    rewrite_catalog_products(args.output_dir, updated)
    print(
        f"Product images updated in {args.output_dir}: "
        f"downloaded={stats.downloaded}, cached={stats.cache_hits}, "
        f"failed={stats.failed}, unavailable={stats.unavailable}"
    )
    return 0


def _cmd_render_qr(args: argparse.Namespace) -> int:
    products = _load_catalog_products(args.output_dir)
    updated, count = render_qr_for_products(
        products,
        args.output_dir,
        fmt=args.format,
        limit=args.limit,
        brand_slug_filter=args.brand,
    )
    rewrite_catalog_products(args.output_dir, updated)
    print(f"Rendered {count} QR {args.format.upper()} files under {args.output_dir / 'qr'}")
    return 0


def _cmd_build_web(args: argparse.Namespace) -> int:
    out_dir = Path(args.output_dir)
    for req in ("all_products.json", "summary.json"):
        if not (out_dir / req).is_file():
            raise QRScrabberError(
                f"Cannot build web bundle: missing {out_dir / req}. Run `run-all` first."
            )
    exported = export_web_assets(out_dir)
    if not exported:
        raise QRScrabberError("No web UI source assets found to export.")
    print(f"Exported web UI bundle ({len(exported)} files) -> {out_dir}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    out_dir = Path(args.output_dir)
    if not out_dir.is_dir():
        raise QRScrabberError(f"Output directory does not exist: {out_dir}")
    if not (out_dir / "index.html").is_file() and (out_dir / "all_products.json").is_file():
        export_web_assets(out_dir)
    run_server(output_dir=out_dir, host=args.host, port=args.port)
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    errors = verify_output_dir(
        args.output_dir,
        check_qr_files=args.check_qr_files,
        check_images=args.check_images,
        check_web=args.check_web,
    )
    if errors:
        for err in errors:
            print(f"verify: FAIL: {err}", file=sys.stderr)
        raise QRScrabberError(f"verification failed with {len(errors)} error(s).")
    print(f"verify: OK ({args.output_dir})")
    return 0


def _dispatch_command(args: argparse.Namespace) -> int:
    cmd = args.command
    if cmd == "sync-brands":
        return _cmd_sync_brands(args)
    if cmd == "check-sources":
        return _cmd_check_sources(args)
    if cmd == "harvest":
        return _cmd_pipeline(args, export=False)
    if cmd in ("build-catalog", "run-all"):
        return _cmd_pipeline(args, export=True)
    if cmd == "fetch-images":
        return _cmd_fetch_images(args)
    if cmd == "render-qr":
        return _cmd_render_qr(args)
    if cmd == "build-web":
        return _cmd_build_web(args)
    if cmd == "serve":
        return _cmd_serve(args)
    if cmd == "verify":
        return _cmd_verify(args)
    raise QRScrabberError(f"Unknown subcommand: {cmd}")


def main(argv: Sequence[str] | None = None) -> int:
    """Execute the `qr-scrabber` CLI and return a POSIX exit code (`0`, `1`, or `2`)."""
    parser = build_parser()
    arg_list = list(argv) if argv is not None else None
    try:
        args = parser.parse_args(arg_list)
    except SystemExit as exc:
        if isinstance(exc.code, int):
            return exc.code
        return 0 if exc.code is None else 1

    if args.command is None:
        parser.print_help(sys.stderr)
        return 2

    try:
        return _dispatch_command(args)
    except (QRScrabberError, OSError, ValueError) as exc:
        print(f"qr-scrabber: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
