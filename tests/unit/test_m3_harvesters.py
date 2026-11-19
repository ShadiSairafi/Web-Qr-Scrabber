"""Offline & loopback mock unit tests for Milestone 3 harvesters (0 live UPCitemdb calls)."""

from __future__ import annotations

import gzip
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from PIL import Image

from qr_scrabber.brands import BrandMatcher, BrandRegistry, load_brands_seed
from qr_scrabber.gs1 import normalize_barcode
from qr_scrabber.harvesters import (
    ProductImageDownloader,
    UPCItemDBHarvester,
    derive_obf_image_urls,
    harvest_open_facts,
    probe_sources,
)
from qr_scrabber.models import (
    BarcodeInfo,
    BrandInfo,
    ProductDetails,
    ProductRecord,
    ProvenanceInfo,
    VerificationInfo,
)
from qr_scrabber.qr import build_qr_metadata

SEED_PATH = Path("data/brands_seed.json")


@pytest.fixture(scope="module")
def matcher() -> BrandMatcher:
    reg = BrandRegistry.from_entries(load_brands_seed(SEED_PATH))
    return BrandMatcher(reg)


def test_derive_obf_image_urls() -> None:
    primary, thumb, all_urls = derive_obf_image_urls(
        "0887167485471",
        {"front_en": {"rev": "4"}, "front_fr": {"rev": "2"}},
    )
    assert (
        primary
        == "https://images.openbeautyfacts.org/images/products/088/716/748/5471/front_en.4.400.jpg"
    )
    assert (
        thumb
        == "https://images.openbeautyfacts.org/images/products/088/716/748/5471/front_en.4.200.jpg"
    )
    assert len(all_urls) == 4


def test_harvest_open_facts_jsonl_splitting_and_quarantine(
    tmp_path: Path,
    matcher: BrandMatcher,
) -> None:
    jsonl_gz = tmp_path / "obf.jsonl.gz"
    rows = [
        # Valid Estee Lauder with derived image
        {
            "code": "0887167485471",
            "product_name_en": "Advanced Night Repair Serum",
            "brands_tags": ["xx:estee-lauder"],
            "categories_tags": ["en:face-serums"],
            "images": {"front_en": {"rev": "7"}},
        },
        # Concatenated 26-digit double valid EAN-13 for Fenty Beauty
        {
            "code": "08166570224520816657022452",
            "product_name": "Killawatt Freestyle Highlighter",
            "brands_tags": ["xx:fenty-beauty"],
        },
        # Conglomerate leak (Maybelline under L'Oreal) -> ignored completely
        {
            "code": "0041554007503",
            "product_name": "Maybelline Define A Lash",
            "brands_tags": ["xx:l-oreal", "xx:l-oreal-consumer-products", "xx:maybelline"],
        },
        # Missing product name -> quarantined as MISSING_PRODUCT_NAME
        {
            "code": "0816657022452",
            "product_name": "",
            "brands_tags": ["xx:fenty-beauty"],
        },
        # Restricted circulation prefix (20...) with valid GS1 check digit -> quarantined
        {
            "code": "2000000000015",
            "product_name": "Internal Store Tester",
            "brands_tags": ["xx:lancome"],
        },
    ]
    with gzip.open(jsonl_gz, "wt", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    res = harvest_open_facts(matcher=matcher, jsonl_path=jsonl_gz)
    assert res.rows_scanned == 5
    # 1 Estee Lauder + 2 split Fenty codes = 3 candidates
    assert len(res.candidates) == 3
    el = [c for c in res.candidates if c.brand_slug == "estee-lauder"][0]
    assert el.gtin14 == "00887167485471"
    assert el.primary_image_url is not None and "front_en.7.400.jpg" in el.primary_image_url
    q_reasons = {q.reason for q in res.quarantined}
    assert "MISSING_PRODUCT_NAME" in q_reasons
    assert "RESTRICTED_CIRCULATION_PREFIX" in q_reasons


def test_upcitemdb_batch_lookup_caching_and_rate_limit(
    tmp_path: Path,
    matcher: BrandMatcher,
) -> None:
    calls: list[tuple[str, dict[str, list[str]]]] = []
    trigger_429 = {"enabled": False}

    class MockUPCHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            calls.append((parsed.path, qs))
            if trigger_429["enabled"]:
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-Ratelimit-Remaining", "0")
                self.end_headers()
                self.wfile.write(json.dumps({"code": "TOO_FAST"}).encode())
                return

            if parsed.path.endswith("/lookup"):
                upcs = qs.get("upc", [""])[0].split(",")
                assert len(upcs) <= 2
                items = [
                    {
                        "ean": "0887167485471",
                        "upc": "887167485471",
                        "title": "Estee Lauder Advanced Night Repair Synchronized Serum",
                        "brand": "Estee Lauder",
                        "category": "Health & Beauty > Personal Care > Cosmetics > Skin Care",
                        "images": ["https://example.com/el.jpg"],
                    }
                ]
                payload = {"code": "OK", "total": len(items), "offset": 0, "items": items}
            elif parsed.path.endswith("/search"):
                items = [
                    {
                        "ean": "0600173536223",
                        "upc": "600173536223",
                        "title": "Huda Beauty Lip Strobe Metallic Lip Gloss",
                        "brand": "Huda Beauty",
                        "category": "Health & Beauty > Cosmetics > Lip Makeup",
                        "images": ["https://example.com/huda.jpg"],
                    }
                ]
                payload = {"code": "OK", "total": 1, "offset": 0, "items": items}
            else:
                self.send_response(404)
                self.end_headers()
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Ratelimit-Remaining", "88")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), MockUPCHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        cache_file = tmp_path / "upc_cache.json"
        base_url = f"http://127.0.0.1:{port}/prod/trial"

        # Run 1: live against local mock server (2 UPCs batched into 1 lookup + 1 search = 2 reqs)
        h1 = UPCItemDBHarvester(cache_path=cache_file, base_url=base_url)
        out1 = h1.harvest(
            matcher=matcher,
            lookup_upcs=["0887167485471", "0816657022452"],
            search_queries=["Huda Beauty"],
            max_lookups=5,
            max_searches=5,
        )
        assert out1.quota.requests_made_this_run == 2
        assert out1.quota.rate_limit_remaining == 88
        assert len(out1.harvest.candidates) == 2
        assert len(calls) == 2
        # Verify 2-UPC comma-separated batching happened in 1 single lookup call
        lookup_calls = [c for c in calls if c[0].endswith("/lookup")]
        assert len(lookup_calls) == 1
        assert lookup_calls[0][1]["upc"] == ["0887167485471,0816657022452"]

        # Run 2: offline cache replay makes 0 HTTP requests and recovers both products
        h2 = UPCItemDBHarvester(cache_path=cache_file, base_url=base_url, offline=True)
        out2 = h2.harvest(matcher=matcher, lookup_upcs=[], search_queries=[])
        assert out2.quota.requests_made_this_run == 0
        assert out2.quota.cache_hits >= 2
        assert len(out2.harvest.candidates) == 2
        assert len(calls) == 2

        # Run 3: graceful 429 handling on uncached query
        trigger_429["enabled"] = True
        h3 = UPCItemDBHarvester(cache_path=cache_file, base_url=base_url)
        out3 = h3.harvest(matcher=matcher, search_queries=["Rare Beauty"])
        assert out3.rate_limited is True
    finally:
        server.shutdown()
        server.server_close()


def test_product_image_downloader_resizes_and_handles_failures(tmp_path: Path) -> None:
    # Build an 800x600 test PNG in memory
    img_buf = io.BytesIO()
    Image.new("RGBA", (800, 600), (220, 120, 140, 255)).save(img_buf, format="PNG")
    valid_png = img_buf.getvalue()

    class MockImgHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/good.png":
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.end_headers()
                self.wfile.write(valid_png)
            elif self.path == "/corrupt.jpg":
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.end_headers()
                self.wfile.write(b"<html>not a jpeg</html>")
            else:
                self.send_response(404)
                self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), MockImgHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        norm1 = normalize_barcode("0887167485471")
        norm2 = normalize_barcode("0816657022452")
        p1 = ProductRecord(
            gtin14=norm1.gtin14,
            product_name="Serum Good Image",
            brand=BrandInfo(
                slug="estee-lauder",
                display_name="Estée Lauder",
                glow_and_glare_vendor_url="https://glowandglare.com/collections/vendors?q=Estee",
            ),
            product=ProductDetails(
                name="Serum Good Image",
                primary_image_url=f"http://127.0.0.1:{port}/good.png",
            ),
            barcode=BarcodeInfo.from_normalized(norm1),
            qr=build_qr_metadata(
                norm1.gtin14,
                norm1.primary_code,
                norm1.barcode_type,
                "Estée Lauder",
                "Serum Good Image",
            ),
            verification=VerificationInfo(status="verified_single_source", confidence_score=0.9),
            provenance=ProvenanceInfo(harvested_at="2026-11-18T00:00:00Z"),
        )
        p2 = ProductRecord(
            gtin14=norm2.gtin14,
            product_name="Highlighter Corrupt Image",
            brand=BrandInfo(
                slug="fenty-beauty",
                display_name="Fenty Beauty",
                glow_and_glare_vendor_url="https://glowandglare.com/collections/vendors?q=Fenty",
            ),
            product=ProductDetails(
                name="Highlighter Corrupt Image",
                primary_image_url=f"http://127.0.0.1:{port}/corrupt.jpg",
            ),
            barcode=BarcodeInfo.from_normalized(norm2),
            qr=build_qr_metadata(
                norm2.gtin14,
                norm2.primary_code,
                norm2.barcode_type,
                "Fenty Beauty",
                "Highlighter Corrupt Image",
            ),
            verification=VerificationInfo(status="verified_single_source", confidence_score=0.9),
            provenance=ProvenanceInfo(harvested_at="2026-11-18T00:00:00Z"),
        )

        out_dir = tmp_path / "output"
        downloader = ProductImageDownloader(cache_dir=tmp_path / "img_cache", max_dim=320)
        updated, stats = downloader.download_for_products([p1, p2], out_dir)

        assert stats.downloaded == 1
        assert stats.failed == 1
        assert updated[0].local_image_path == "images/estee-lauder/00887167485471.jpg"
        assert updated[0].product.image_status == "downloaded"
        saved_file = out_dir / updated[0].local_image_path
        assert saved_file.is_file()
        with Image.open(saved_file) as saved_im:
            assert max(saved_im.size) <= 320
            assert saved_im.format == "JPEG"

        assert updated[1].local_image_path is None
        assert updated[1].product.image_status == "remote_only"
    finally:
        server.shutdown()
        server.server_close()


def test_probe_sources_offline_documents_cloudflare_gate() -> None:
    status = probe_sources(offline=True)
    assert not isinstance(status.barcodelookup, str)
    assert status.barcodelookup.status == "blocked_cloudflare_challenge"
    assert status.barcodelookup.http_status == 403
