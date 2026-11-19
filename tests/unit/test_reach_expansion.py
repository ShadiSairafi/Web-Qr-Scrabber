"""Unit & Playwright tests for official brand storefronts & automatic on-demand live lookup."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

from qr_scrabber.brands.extractor import load_brands_seed
from qr_scrabber.brands.registry import BrandRegistry
from qr_scrabber.exporter import export_catalog, verify_output_dir
from qr_scrabber.gs1 import normalize_barcode
from qr_scrabber.harvesters.brand_storefronts import harvest_brand_storefronts
from qr_scrabber.harvesters.openbeautyfacts import HarvestedCandidate, SourceHarvestResult
from qr_scrabber.live_lookup import LiveBarcodeLookupService
from qr_scrabber.models import SourceStatusEntry, SourceStatusMap, UpcitemdbQuota
from qr_scrabber.reconcile import reconcile_sources
from qr_scrabber.server import create_server

CHROMIUM_PATH = "/usr/lib/chromium/chromium"
SEPHORA_EAN13 = "3378872203964"
SEPHORA_GTIN14 = "03378872203964"


def _minimal_status_map() -> SourceStatusMap:
    ok = SourceStatusEntry(status="cached_offline", mode="offline")
    return SourceStatusMap(
        openbeautyfacts=ok,
        openproductsfacts=ok,
        upcitemdb=ok,
        barcodelookup=SourceStatusEntry(status="blocked_cloudflare", http_status=403),
    )


def _gs1_check_digit(body: str) -> str:
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return str((10 - (total % 10)) % 10)


def test_brand_storefront_harvest_and_cross_source_reconciliation(tmp_path: Path) -> None:
    registry = BrandRegistry.from_entries(load_brands_seed(Path("data/brands_seed.json")))
    fenty_upc = "81076303001" + _gs1_check_digit("81076303001")
    rhode_upc = "81010539001" + _gs1_check_digit("81010539001")
    bad_upc = "81010539002" + str((int(_gs1_check_digit("81010539002")) + 1) % 10)

    cache_path = tmp_path / "brand_storefronts_cache.json"
    cache_path.write_text(
        json.dumps(
            [
                {
                    "brand_slug": "fenty-beauty",
                    "barcode": fenty_upc,
                    "product_name": "Gloss Bomb Universal Lip Luminizer - Fenty Glow",
                    "category": "Lip Gloss",
                    "image_url": "https://cdn.shopify.com/fenty-gloss.jpg",
                    "source_url": "https://fentybeauty.com/products/gloss-bomb",
                },
                {
                    "brand_slug": "rhode",
                    "barcode": rhode_upc,
                    "product_name": "Peptide Lip Treatment - Unscented",
                    "category": "Lip Care",
                    "image_url": "https://cdn.shopify.com/rhode-lip.jpg",
                    "source_url": "https://www.rhodeskin.com/products/peptide-lip-treatment",
                },
                {
                    "brand_slug": "rhode",
                    "barcode": bad_upc,  # invalid check digit -> quarantined
                    "product_name": "Bad Barcode Item",
                    "category": "Lip Care",
                    "image_url": None,
                    "source_url": None,
                },
            ]
        ),
        encoding="utf-8",
    )

    sf_res = harvest_brand_storefronts(
        registry=registry,
        cache_path=cache_path,
        offline=True,
    )
    assert len(sf_res.candidates) == 2
    assert len(sf_res.quarantined) == 1
    assert sf_res.quarantined[0].reason == "INVALID_CHECK_DIGIT"

    # Cross-verify the Fenty GTIN against UPCitemdb
    fenty_norm = normalize_barcode(fenty_upc)
    assert fenty_norm.is_valid
    fenty_entry = registry.get_canonical_entry("fenty-beauty")
    assert fenty_entry is not None
    upc_res = SourceHarvestResult(
        source="upcitemdb",
        rows_scanned=1,
        raw_matched_count=1,
        candidates=[
            HarvestedCandidate(
                source="upcitemdb",
                gtin14=fenty_norm.gtin14,
                barcode=fenty_norm,
                raw_codes=[fenty_upc],
                brand_slug=fenty_entry.slug,
                brand_display_name=fenty_entry.display_name,
                glow_and_glare_vendor_url=fenty_entry.glow_and_glare_vendor_url,
                matched_raw_brand="Fenty Beauty",
                match_method="exact_brand_name",
                match_confidence=0.95,
                product_name="Fenty Beauty Gloss Bomb Universal Lip Luminizer Fenty Glow",
                categories=["Makeup"],
                upcitemdb_ean=fenty_norm.ean13,
            )
        ],
    )
    obf_res = SourceHarvestResult(source="openbeautyfacts")

    rec = reconcile_sources(
        registry=registry,
        obf_result=obf_res,
        upc_result=upc_res,
        storefront_result=sf_res,
    )
    assert rec.comparison_report.duplicate_gtin14_count == 0
    assert rec.comparison_report.total_unique_products == 2
    assert rec.comparison_report.verified_cross_source_count == 1
    by_gtin = {p.gtin14: p for p in rec.products}
    fenty_prod = by_gtin[fenty_norm.gtin14]
    assert fenty_prod.verification.status == "verified_cross_source"
    assert set(fenty_prod.verification.sources) == {"brand_storefront", "upcitemdb"}


def _build_verified_catalog(tmp_path: Path) -> Path:
    registry = BrandRegistry.from_entries(load_brands_seed(Path("data/brands_seed.json")))
    obf_res = SourceHarvestResult(source="openbeautyfacts")
    upc_res = SourceHarvestResult(source="upcitemdb")
    rec = reconcile_sources(registry=registry, obf_result=obf_res, upc_result=upc_res)
    out_dir = tmp_path / "catalog"
    export_catalog(
        registry=registry,
        reconciliation=rec,
        output_dir=out_dir,
        source_status=_minimal_status_map(),
        upc_quota=UpcitemdbQuota(),
        download_images=False,
        render_qr=True,
        export_web=True,
    )
    return out_dir


def _make_mock_live_service(out_dir: Path, tmp_path: Path) -> LiveBarcodeLookupService:
    def _handler(request: httpx.Request) -> httpx.Response:
        if "/lookup" in request.url.path and request.url.params.get("upc") == SEPHORA_EAN13:
            return httpx.Response(
                200,
                json={
                    "code": "OK",
                    "total": 1,
                    "items": [
                        {
                            "ean": SEPHORA_EAN13,
                            "title": (
                                "SEPHORA COLLECTION Best Skin Ever Full Coverage "
                                "Multi-Use Hydrating Concealer, Size: 0.3 Oz, 26n"
                            ),
                            "brand": "SEPHORA COLLECTION",
                            "category": "Health & Beauty > Personal Care > Cosmetics",
                            "size": "0.3 Oz",
                            "images": [],
                        }
                    ],
                },
            )
        return httpx.Response(404, json={"status": 0})

    return LiveBarcodeLookupService(
        output_dir=out_dir,
        brands_seed_path=Path("data/brands_seed.json"),
        upc_cache_path=tmp_path / "upc_cache.json",
        image_cache_dir=tmp_path / "img_cache",
        download_images=False,
        http_transport=httpx.MockTransport(_handler),
    )


def test_live_lookup_endpoint_and_catalog_persistence(tmp_path: Path) -> None:
    out_dir = _build_verified_catalog(tmp_path)
    live_svc = _make_mock_live_service(out_dir, tmp_path)
    srv = create_server(out_dir, host="127.0.0.1", port=0, live_lookup=live_svc)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{srv.server_port}"
        with httpx.Client(trust_env=False) as client:
            # 1. Invalid check digit -> 400
            bad = client.get(f"{base}/api/lookup", params={"code": "3378872203960"})
            assert bad.status_code == 400
            assert bad.json()["found"] is False

            # 2. First lookup hits mocked UPCitemdb and persists cleanly into output/
            first = client.get(f"{base}/api/lookup", params={"code": SEPHORA_EAN13})
            assert first.status_code == 200
            data = first.json()
            assert data["found"] is True
            assert data["added"] is True
            assert data["source"] == "upcitemdb"
            assert data["product"]["gtin14"] == SEPHORA_GTIN14
            assert data["product"]["brand"]["slug"] == "sephora"
            assert "Best Skin Ever" in data["product"]["product_name"]

            # 3. Persisted catalog passes full verify_output_dir including QR decode!
            errors = verify_output_dir(out_dir, check_qr_files=True, check_web=True)
            assert errors == [], f"verify_output_dir failed after live lookup: {errors}"

            # 4. Repeat lookup hits local catalog cache instantly with added=False
            second = client.get(f"{base}/api/lookup", params={"code": SEPHORA_EAN13})
            assert second.status_code == 200
            data2 = second.json()
            assert data2["found"] is True
            assert data2["added"] is False
            assert data2["source"] == "local_catalog"

            # 5. Valid unknown barcode returns 200 found=False
            missing = client.get(f"{base}/api/lookup", params={"code": "4006381333931"})
            assert missing.status_code == 200
            assert missing.json()["found"] is False
    finally:
        srv.shutdown()
        srv.server_close()


def test_live_lookup_playwright_ui_auto_fetch(tmp_path: Path) -> None:
    out_dir = _build_verified_catalog(tmp_path)
    live_svc = _make_mock_live_service(out_dir, tmp_path)
    srv = create_server(out_dir, host="127.0.0.1", port=0, live_lookup=live_svc)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    scratch_tmp = Path("/home/antigravity/.cache/pw")
    scratch_tmp.mkdir(parents=True, exist_ok=True)
    prev_tmpdir = os.environ.get("TMPDIR")
    os.environ["TMPDIR"] = str(scratch_tmp)

    try:
        base = f"http://127.0.0.1:{srv.server_port}"
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=CHROMIUM_PATH,
                headless=True,
                args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
            )
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(base, wait_until="networkidle")

            page.fill("#search-input", SEPHORA_EAN13)
            page.wait_for_selector("#exact-match-card:not([hidden])", timeout=10000)

            card_text = page.locator("#exact-match-card").inner_text()
            assert "Best Skin Ever" in card_text
            assert "Live Lookup · Saved to Catalog" in card_text
            assert SEPHORA_GTIN14 in card_text
            assert page.locator("#empty-state").is_hidden()

            browser.close()
    finally:
        if prev_tmpdir is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = prev_tmpdir
        srv.shutdown()
        srv.server_close()
