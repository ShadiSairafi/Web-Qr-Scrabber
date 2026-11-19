"""Unit & headless Chromium (Playwright) tests for M5 server and single-search-bar web UI."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import httpx
from PIL import Image
from playwright.sync_api import sync_playwright

from qr_scrabber.exporter import export_web_assets
from qr_scrabber.gs1 import normalize_barcode
from qr_scrabber.models import (
    BarcodeInfo,
    BrandInfo,
    ProductDetails,
    ProductRecord,
    ProvenanceInfo,
    VerificationInfo,
)
from qr_scrabber.qr import build_qr_metadata, decode_qr_file, render_qr_code
from qr_scrabber.server import create_server

CHROMIUM_PATH = "/usr/lib/chromium/chromium"


def _gs1_check_digit(body: str) -> str:
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return str((10 - (total % 10)) % 10)


def _make_product(
    index: int,
    *,
    brand_slug: str,
    brand_name: str,
    product_name: str,
    out_dir: Path,
    with_local_image: bool = True,
    with_qr_file: bool = True,
) -> ProductRecord:
    body12 = f"08871674{index:04d}"
    ean13 = body12 + _gs1_check_digit(body12)
    norm = normalize_barcode(ean13)
    assert norm.is_valid

    local_img_rel: str | None = None
    if with_local_image:
        local_img_rel = f"images/{brand_slug}/{norm.gtin14}.jpg"
        img_target = out_dir / local_img_rel
        img_target.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (240, 240), color=(212, 168, 152)).save(img_target, format="JPEG")

    qr_rel: str | None = None
    if with_qr_file:
        qr_rel = f"qr/{brand_slug}/{norm.gtin14}.svg"
        render_qr_code(f"https://id.gs1.org/01/{norm.gtin14}", out_dir / qr_rel, fmt="svg")

    qr_meta = build_qr_metadata(
        gtin14=norm.gtin14,
        primary_code=norm.primary_code,
        barcode_type=norm.barcode_type,
        brand_name=brand_name,
        product_name=product_name,
        qr_svg_path=qr_rel,
    )

    return ProductRecord(
        gtin14=norm.gtin14,
        product_name=product_name,
        image_url=None,
        local_image_path=local_img_rel,
        brand=BrandInfo(
            slug=brand_slug,
            display_name=brand_name,
            glow_and_glare_vendor_url=f"https://glowandglare.com/collections/vendors?q={brand_name}",
            matched_raw_brands=[brand_name],
        ),
        product=ProductDetails(
            name=product_name,
            source_names={"openbeautyfacts": product_name},
            alternate_names=[],
            quantity="50 ml",
            categories=["Skincare"],
            local_image_path=local_img_rel,
            image_status="downloaded" if local_img_rel else "unavailable",
        ),
        barcode=BarcodeInfo(
            gtin14=norm.gtin14,
            ean13=norm.ean13,
            upc_a=norm.upc_a,
            ean8=norm.ean8,
            primary_code=norm.primary_code,
            barcode_type=norm.barcode_type,
            raw_codes=[ean13],
            gs1_check_digit_valid=True,
            gs1_restricted_circulation=False,
        ),
        qr=qr_meta,
        verification=VerificationInfo(
            status="verified_cross_source" if index == 1 else "verified_single_source",
            confidence_score=1.0 if index == 1 else 0.9,
            sources=["openbeautyfacts", "upcitemdb"] if index == 1 else ["openbeautyfacts"],
            match_methods={"openbeautyfacts": "brands_tags_leaf"},
        ),
        provenance=ProvenanceInfo(harvested_at="2026-11-19T00:00:00Z"),
    )


def _build_fixture_catalog(tmp_path: Path, count: int = 54) -> tuple[Path, list[ProductRecord]]:
    out_dir = tmp_path / "site"
    out_dir.mkdir(parents=True, exist_ok=True)

    products: list[ProductRecord] = [
        _make_product(
            1,
            brand_slug="estee-lauder",
            brand_name="Estée Lauder",
            product_name="Advanced Night Repair Serum",
            out_dir=out_dir,
            with_local_image=True,
            with_qr_file=True,
        ),
        _make_product(
            2,
            brand_slug="cerave",
            brand_name="CeraVe",
            product_name="Foaming Facial Cleanser",
            out_dir=out_dir,
            with_local_image=True,
            with_qr_file=True,
        ),
        _make_product(
            3,
            brand_slug="fenty-beauty",
            brand_name="Fenty Beauty",
            product_name="Gloss Bomb Universal Lip Luminizer",
            out_dir=out_dir,
            with_local_image=False,  # exercises clean placeholder fallback
            with_qr_file=False,  # exercises /api/qr/<gtin14>.svg fallback
        ),
    ]
    for i in range(4, count + 1):
        products.append(
            _make_product(
                i,
                brand_slug="the-ordinary",
                brand_name="The Ordinary",
                product_name=f"Botanical Hydration Drops #{i}",
                out_dir=out_dir,
                with_local_image=False,
                with_qr_file=False,
            )
        )

    (out_dir / "all_products.json").write_text(
        json.dumps([p.model_dump(mode="json") for p in products], indent=2),
        encoding="utf-8",
    )
    (out_dir / "summary.json").write_text(
        json.dumps(
            {
                "total_brands_in_directory": 163,
                "total_unique_verified_products": len(products),
                "cross_source_verified_count": 1,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    export_web_assets(out_dir)
    return out_dir, products


def test_server_endpoints_and_dynamic_qr_optical_decode(tmp_path: Path) -> None:
    out_dir, products = _build_fixture_catalog(tmp_path, count=5)
    srv = create_server(out_dir, host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{srv.server_port}"
        with httpx.Client(trust_env=False) as client:
            health = client.get(f"{base}/api/health")
            assert health.status_code == 200
            assert health.json() == {"status": "ok", "total_products": 5}

            target = products[2]  # no static QR file; served dynamically
            qr_resp = client.get(f"{base}/api/qr/{target.gtin14}.svg")
            assert qr_resp.status_code == 200
            assert "image/svg+xml" in qr_resp.headers["content-type"]

            svg_file = tmp_path / "dynamic_qr.svg"
            svg_file.write_bytes(qr_resp.content)
            decoded = decode_qr_file(svg_file)
            assert target.qr.gs1_digital_link in decoded

            bad_qr = client.get(f"{base}/api/qr/00000000000001.svg")
            assert bad_qr.status_code == 400
    finally:
        srv.shutdown()
        srv.server_close()


def test_web_ui_playwright_serial_regex_pagination_and_modal(tmp_path: Path) -> None:
    out_dir, products = _build_fixture_catalog(tmp_path, count=54)
    srv = create_server(out_dir, host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    scratch_tmp = Path("/home/antigravity/.cache/pw")
    scratch_tmp.mkdir(parents=True, exist_ok=True)
    prev_tmpdir = os.environ.get("TMPDIR")
    os.environ["TMPDIR"] = str(scratch_tmp)

    try:
        base = f"http://127.0.0.1:{srv.server_port}"
        console_errors: list[str] = []
        page_errors: list[str] = []
        failed_requests: list[str] = []

        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=CHROMIUM_PATH,
                headless=True,
                args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
            )
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
            )
            page.on("pageerror", lambda err: page_errors.append(str(err)))
            page.on(
                "response",
                lambda resp: failed_requests.append(f"{resp.status} {resp.url}")
                if resp.status >= 400
                else None,
            )

            page.goto(base, wait_until="networkidle")
            page.wait_for_selector("article.product-card")

            # 1. Soft light-on-the-eyes background check (luminance > 0.90)
            luminance = page.evaluate(
                """() => {
                    const bg = getComputedStyle(document.body).backgroundColor;
                    const m = bg.match(/\\d+/g).map(Number);
                    return (0.2126*m[0] + 0.7152*m[1] + 0.0722*m[2]) / 255;
                }"""
            )
            assert luminance > 0.90, f"Expected soft light background, got luminance={luminance}"

            # 2. Pagination: 54 products -> 48 initially + #load-more-btn -> 54 after click
            assert page.locator("article.product-card").count() == 48
            assert page.locator("#load-more-btn").is_visible()
            page.locator("#load-more-btn").click()
            assert page.locator("article.product-card").count() == 54
            assert not page.locator("#load-more-btn").is_visible()

            # 3. Exact barcode serial search -> #exact-match-card visible with image & QR
            target_p1 = products[0]
            page.fill("#search-input", target_p1.barcode.primary_code)
            page.wait_for_selector("#exact-match-card:not([hidden])")
            assert "Advanced Night Repair Serum" in page.locator("#exact-match-card").inner_text()
            exact_img_width = page.locator("#exact-match-card .exact-product-img").evaluate(
                "el => el.complete ? el.naturalWidth : 0"
            )
            assert exact_img_width > 0

            # 4. Exact GS1 Digital Link URL search
            page.fill("#search-input", target_p1.qr.gs1_digital_link)
            page.wait_for_selector("#exact-match-card:not([hidden])")
            assert "Exact Serial Match" in page.locator("#search-mode-indicator").inner_text()

            # 5. Regex multi-product search (`serum|cleanser`)
            page.fill("#search-input", "serum|cleanser")
            page.wait_for_timeout(120)
            assert page.locator("#exact-match-card").is_hidden()
            assert page.locator("article.product-card").count() == 2
            assert "Regex Search" in page.locator("#search-mode-indicator").inner_text()

            # 6. Invalid / incomplete regex safety (`[unclosed(`)
            page.fill("#search-input", "[unclosed(")
            page.wait_for_timeout(120)
            assert "Literal fallback" in page.locator("#regex-status").inner_text()
            assert page.locator("#empty-state").is_visible()

            # 7. Clear search + open & close QR modal
            page.locator("#search-clear").click()
            page.wait_for_selector("article.product-card")
            page.locator("article.product-card .qr-thumb-btn").first.click()
            assert page.locator("#qr-modal").is_visible()
            page.locator("#qr-modal-close").click()
            assert not page.locator("#qr-modal").is_visible()

            browser.close()

        assert console_errors == [], f"Unexpected browser console errors: {console_errors}"
        assert page_errors == [], f"Unexpected uncaught page errors: {page_errors}"
        assert failed_requests == [], f"Unexpected HTTP 4xx/5xx responses: {failed_requests}"
    finally:
        if prev_tmpdir is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = prev_tmpdir
        srv.shutdown()
        srv.server_close()
