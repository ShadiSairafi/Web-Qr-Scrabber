"""End-to-end verification tests against the real generated `output/` catalog and live web UI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest
from PIL import Image
from playwright.sync_api import sync_playwright

from qr_scrabber.exporter import verify_output_dir
from qr_scrabber.qr import decode_qr_file
from qr_scrabber.server import create_server

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = REPO_ROOT / "output"
CHROMIUM_PATH = "/usr/lib/chromium/chromium"


pytestmark = pytest.mark.skipif(
    not (OUTPUT_DIR / "all_products.json").is_file(),
    reason="Production output/ catalog has not been generated yet.",
)


def test_real_output_passes_full_verifier_and_invariants() -> None:
    errors = verify_output_dir(
        OUTPUT_DIR,
        check_qr_files=True,
        check_images=True,
        check_web=True,
    )
    assert errors == [], f"verify_output_dir reported errors: {errors}"

    summary = json.loads((OUTPUT_DIR / "summary.json").read_text(encoding="utf-8"))
    manifest = json.loads((OUTPUT_DIR / "brands_manifest.json").read_text(encoding="utf-8"))
    all_products = json.loads((OUTPUT_DIR / "all_products.json").read_text(encoding="utf-8"))
    comparison = json.loads((OUTPUT_DIR / "comparison_report.json").read_text(encoding="utf-8"))

    assert summary["total_brands_in_directory"] == 163
    assert summary["distinct_brand_entities"] == 161
    assert summary["duplicate_gtin14_count"] == 0
    assert comparison["duplicate_gtin14_count"] == 0
    assert summary["total_unique_verified_products"] == len(all_products)
    assert summary["products_with_name_count"] == len(all_products)
    assert len(all_products) >= 500
    assert len(manifest["brands"]) == 163

    # Verify alias brand files have zero duplicated products
    ren_alias = json.loads((OUTPUT_DIR / "brands" / "ren.json").read_text(encoding="utf-8"))
    sweet_alias = json.loads(
        (OUTPUT_DIR / "brands" / "sweet-chief.json").read_text(encoding="utf-8")
    )
    assert ren_alias["alias_of"] == "ren-clean-skincare"
    assert ren_alias["products"] == []
    assert sweet_alias["alias_of"] == "sweet-chef"
    assert sweet_alias["products"] == []

    # Verify barcodelookup Cloudflare gate is documented transparently
    assert summary["source_status"]["barcodelookup"]["status"] == "blocked_cloudflare_challenge"

    # Spot-check real downloaded local thumbnails and optically decode real QR SVGs
    with_local_img = [p for p in all_products if p.get("local_image_path")]
    assert len(with_local_img) >= 50
    for rec in with_local_img[:25]:
        img_path = OUTPUT_DIR / rec["local_image_path"]
        assert img_path.is_file()
        with Image.open(img_path) as im:
            im.load()
            assert im.width > 0 and im.height > 0
            assert max(im.width, im.height) <= 320

    with_qr = [p for p in all_products if p["qr"].get("qr_svg_path")]
    assert len(with_qr) == len(all_products)
    for rec in with_qr[:15]:
        decoded = decode_qr_file(OUTPUT_DIR / rec["qr"]["qr_svg_path"])
        assert rec["qr"]["gs1_digital_link"] in decoded


def test_real_output_live_web_ui_in_headless_chromium() -> None:
    all_products = json.loads((OUTPUT_DIR / "all_products.json").read_text(encoding="utf-8"))
    sample_with_img = next((p for p in all_products if p.get("local_image_path")), all_products[0])

    srv = create_server(OUTPUT_DIR, host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    scratch_tmp = Path("/home/antigravity/.cache/pw_e2e")
    scratch_tmp.mkdir(parents=True, exist_ok=True)
    prev_tmpdir = os.environ.get("TMPDIR")
    os.environ["TMPDIR"] = str(scratch_tmp)

    try:
        base = f"http://127.0.0.1:{srv.server_port}"

        with httpx.Client(trust_env=False) as client:
            health = client.get(f"{base}/api/health")
            assert health.status_code == 200
            assert health.json()["status"] == "ok"
            assert health.json()["total_products"] == len(all_products)

        console_errors: list[str] = []
        page_errors: list[str] = []
        local_4xx_5xx: list[str] = []

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
                lambda resp: local_4xx_5xx.append(f"{resp.status} {resp.url}")
                if resp.url.startswith(base) and resp.status >= 400
                else None,
            )

            page.goto(base, wait_until="networkidle")
            page.wait_for_selector("article.product-card")

            # 1. Exact random product QR/barcode serial lookup -> #exact-match-card
            serial = sample_with_img["barcode"]["primary_code"]
            page.fill("#search-input", serial)
            page.wait_for_selector("#exact-match-card:not([hidden])")
            card_text = page.locator("#exact-match-card").inner_text()
            assert sample_with_img["product_name"] in card_text
            assert sample_with_img["brand"]["display_name"] in card_text

            # Verify product photo loaded cleanly
            img_loaded = page.locator("#exact-match-card .exact-product-img").evaluate(
                "el => el.complete && el.naturalWidth > 0"
            )
            assert img_loaded

            # 2. Exact GS1 Digital Link lookup
            page.fill("#search-input", sample_with_img["qr"]["gs1_digital_link"])
            page.wait_for_selector("#exact-match-card:not([hidden])")
            assert "Exact Serial Match" in page.locator("#search-mode-indicator").inner_text()

            # 3. Regex multi-product name search (`serum|cream`)
            page.fill("#search-input", "serum|cream")
            page.wait_for_timeout(150)
            assert page.locator("#exact-match-card").is_hidden()
            assert page.locator("article.product-card").count() >= 5
            assert "Regex Search" in page.locator("#search-mode-indicator").inner_text()

            # 4. Incomplete regex graceful literal fallback
            page.fill("#search-input", "[unclosed(")
            page.wait_for_timeout(150)
            assert "Literal fallback" in page.locator("#regex-status").inner_text()

            # 5. Clear search + open QR zoom modal
            page.locator("#search-clear").click()
            page.wait_for_selector("article.product-card")
            page.locator("article.product-card .qr-thumb-btn").first.click()
            assert page.locator("#qr-modal").is_visible()
            page.locator("#qr-modal-close").click()
            assert not page.locator("#qr-modal").is_visible()

            browser.close()

        assert console_errors == [], f"Console errors: {console_errors}"
        assert page_errors == [], f"Page errors: {page_errors}"
        assert local_4xx_5xx == [], f"Local HTTP 4xx/5xx responses: {local_4xx_5xx}"
    finally:
        if prev_tmpdir is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = prev_tmpdir
        srv.shutdown()
        srv.server_close()


def test_cli_verify_subprocess_exits_zero_on_real_output() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "qr_scrabber",
            "verify",
            "--output-dir",
            str(OUTPUT_DIR),
            "--check-qr-files",
            "--check-images",
            "--check-web",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"CLI verify failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
