"""Unit & integration tests for M4 reconciliation, atomic export, and CLI subcommands."""

from __future__ import annotations

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from PIL import Image

from qr_scrabber.brands.extractor import load_brands_seed
from qr_scrabber.brands.matcher import BrandMatcher
from qr_scrabber.brands.registry import BrandRegistry
from qr_scrabber.cli import main as cli_main
from qr_scrabber.gs1 import normalize_barcode
from qr_scrabber.harvesters.openbeautyfacts import HarvestedCandidate, SourceHarvestResult
from qr_scrabber.reconcile import reconcile_sources

REPO_ROOT = Path(__file__).resolve().parents[2]
BRANDS_SEED = REPO_ROOT / "data" / "brands_seed.json"


def _make_jpeg_bytes(color: tuple[int, int, int] = (210, 145, 130)) -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGB", (400, 400), color=color)
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _cand(
    *,
    source: str,
    code: str,
    brand_slug: str,
    brand_display: str,
    name: str | None,
    confidence: float = 0.95,
    method: str = "brands_tags_leaf",
    image_url: str | None = None,
) -> HarvestedCandidate:
    norm = normalize_barcode(code)
    assert norm.is_valid
    return HarvestedCandidate(
        source=source,
        gtin14=norm.gtin14,
        barcode=norm,
        raw_codes=[code],
        brand_slug=brand_slug,
        brand_display_name=brand_display,
        glow_and_glare_vendor_url=f"https://glowandglare.com/collections/vendors?q={brand_display}",
        matched_raw_brand=brand_display,
        match_method=method,
        match_confidence=confidence,
        product_name=name,
        primary_image_url=image_url,
        thumbnail_url=image_url,
        image_urls=[image_url] if image_url else [],
        source_url=f"https://world.openbeautyfacts.org/product/{code}"
        if source == "openbeautyfacts"
        else None,
        upcitemdb_ean=norm.ean13 if source == "upcitemdb" else None,
    )


def test_reconcile_cross_source_dedup_subbrand_and_conflict() -> None:
    registry = BrandRegistry.from_entries(load_brands_seed(BRANDS_SEED))

    obf = SourceHarvestResult(
        source="openbeautyfacts",
        rows_scanned=5,
        raw_matched_count=5,
        candidates=[
            # 1. Exact cross-source match (Estée Lauder) + intra-source duplicate
            _cand(
                source="openbeautyfacts",
                code="0887167485471",
                brand_slug="estee-lauder",
                brand_display="Estée Lauder",
                name="Advanced Night Repair Synchronized Multi-Recovery Complex",
            ),
            _cand(
                source="openbeautyfacts",
                code="887167485471",
                brand_slug="estee-lauder",
                brand_display="Estée Lauder",
                name="ADVANCED NIGHT REPAIR",
                confidence=0.80,
            ),
            # 2. Parent/sub-brand resolution: L'Oreal (parent) vs Lancôme (sub-brand)
            _cand(
                source="openbeautyfacts",
                code="3614272049529",
                brand_slug="loreal",
                brand_display="L'Oreal",
                name="La Vie Est Belle Eau de Parfum",
            ),
            # 3. True cross-brand conflict: CeraVe vs NARS on same valid EAN-13
            _cand(
                source="openbeautyfacts",
                code="3337875597388",
                brand_slug="cerave",
                brand_display="CeraVe",
                name="CeraVe Foaming Facial Cleanser",
            ),
            # 4. Missing product name -> quarantined as MISSING_PRODUCT_NAME
            _cand(
                source="openbeautyfacts",
                code="3600523959853",
                brand_slug="loreal",
                brand_display="L'Oreal",
                name="   ",
            ),
        ],
    )

    upc = SourceHarvestResult(
        source="upcitemdb",
        rows_scanned=3,
        raw_matched_count=3,
        candidates=[
            # Matches Estée Lauder via 12-digit UPC-A -> same canonical GTIN-14
            _cand(
                source="upcitemdb",
                code="887167485471",
                brand_slug="estee-lauder",
                brand_display="Estée Lauder",
                name="Estee Lauder Advanced Night Repair Serum 50ml",
                method="exact_brand_field",
            ),
            # Sub-brand Lancôme resolves parent L'Oreal
            _cand(
                source="upcitemdb",
                code="3614272049529",
                brand_slug="lancome",
                brand_display="Lancôme",
                name="Lancome La Vie Est Belle Eau de Parfum Spray",
                method="exact_brand_field",
            ),
            # Unrelated brand conflict against CeraVe
            _cand(
                source="upcitemdb",
                code="3337875597388",
                brand_slug="nars",
                brand_display="NARS",
                name="NARS Radiant Creamy Concealer",
                method="exact_brand_field",
            ),
        ],
    )

    res = reconcile_sources(registry=registry, obf_result=obf, upc_result=upc)
    assert res.comparison_report.duplicate_gtin14_count == 0
    assert res.comparison_report.source_breakdown.intra_source_duplicates_removed == 1
    assert res.comparison_report.verified_cross_source_count == 1
    assert res.comparison_report.verified_cross_source_subbrand_resolved_count == 1
    assert len(res.comparison_report.brand_conflicts) == 1
    assert len(res.comparison_report.parent_subbrand_resolutions) == 1

    by_gtin = {p.gtin14: p for p in res.products}
    assert len(by_gtin) == 2

    el = by_gtin["00887167485471"]
    assert el.brand.slug == "estee-lauder"
    assert el.verification.status == "verified_cross_source"
    assert el.product_name == el.product.name
    assert set(el.product.source_names.keys()) == {"openbeautyfacts", "upcitemdb"}
    assert len(el.product.alternate_names) >= 1

    lan = by_gtin["03614272049529"]
    assert lan.brand.slug == "lancome"
    assert lan.verification.status == "verified_cross_source_subbrand_resolved"

    q_reasons = res.quarantine_report.counts_by_reason
    assert q_reasons["CROSS_SOURCE_BRAND_CONFLICT"] == 1
    assert q_reasons["MISSING_PRODUCT_NAME"] == 1


def test_cli_end_to_end_with_local_mocks_and_offline_replay(tmp_path: Path) -> None:
    jpeg_bytes = _make_jpeg_bytes()
    upc_calls: list[str] = []

    class _MockHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            qs = parse_qs(parsed.query)
            if parsed.path == "/img/anr.jpg":
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpeg_bytes)))
                self.end_headers()
                self.wfile.write(jpeg_bytes)
                return

            if parsed.path == "/lookup":
                upc_calls.append(self.path)
                payload = {
                    "code": "OK",
                    "total": 1,
                    "items": [
                        {
                            "ean": "0887167485471",
                            "upc": "887167485471",
                            "brand": "Estee Lauder",
                            "title": "Estee Lauder Advanced Night Repair Serum 50ml",
                            "category": "Health & Beauty > Personal Care > Cosmetics > Skin Care",
                            "images": [f"http://127.0.0.1:{server.server_port}/img/anr.jpg"],
                        }
                    ],
                }
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-RateLimit-Remaining", "92")
                self.end_headers()
                self.wfile.write(raw)
                return

            if parsed.path == "/search":
                upc_calls.append(self.path)
                q = (qs.get("s") or [""])[0]
                items = []
                if "huda" in q.lower():
                    items.append(
                        {
                            "ean": "6291106035421",
                            "brand": "Huda Beauty",
                            "title": "Huda Beauty Easy Bake Loose Baking Powder",
                            "category": "Health & Beauty > Cosmetics > Makeup",
                            "images": [f"http://127.0.0.1:{server.server_port}/img/anr.jpg"],
                        }
                    )
                raw = json.dumps({"code": "OK", "total": len(items), "items": items}).encode(
                    "utf-8"
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-RateLimit-Remaining", "91")
                self.end_headers()
                self.wfile.write(raw)
                return

            self.send_response(404)
            self.end_headers()

    server = HTTPServer(("127.0.0.1", 0), _MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        obf_jsonl = tmp_path / "obf_sample.jsonl"
        obf_rows = [
            {
                "code": "0887167485471",
                "brands": "Estée Lauder",
                "brands_tags": ["estee-lauder"],
                "product_name_en": "Advanced Night Repair Serum",
                "image_url": f"{base_url}/img/anr.jpg",
                "quantity": "50 ml",
            },
            {
                "code": "5060389240010",
                "brands": "REN",
                "brands_tags": ["ren"],
                "categories_tags": ["en:skincare", "en:face-toners"],
                "product_name_en": "Ready Steady Glow Daily AHA Tonic",
                "image_url": f"{base_url}/img/anr.jpg",
                "quantity": "250 ml",
            },
        ]
        obf_jsonl.write_text(
            "\n".join(json.dumps(r) for r in obf_rows) + "\n",
            encoding="utf-8",
        )

        upc_cache = tmp_path / "upc_cache.json"
        out_dir = tmp_path / "output"

        # 1. check-sources --offline --json
        assert cli_main(["check-sources", "--offline", "--json"]) == 0

        # 2. sync-brands --offline
        manifest_out = tmp_path / "synced_manifest.json"
        assert (
            cli_main(
                [
                    "sync-brands",
                    "--offline",
                    "--seed-file",
                    str(BRANDS_SEED),
                    "--out",
                    str(manifest_out),
                ]
            )
            == 0
        )
        synced = json.loads(manifest_out.read_text(encoding="utf-8"))
        assert synced["total_vendor_entries"] == 163

        # 3. run-all against local mock UPCitemdb & local mock image server
        rc = cli_main(
            [
                "run-all",
                "--output-dir",
                str(out_dir),
                "--brands-seed",
                str(BRANDS_SEED),
                "--obf-jsonl",
                str(obf_jsonl),
                "--no-opf",
                "--upc-cache",
                str(upc_cache),
                "--upc-base-url",
                base_url,
                "--max-upc-lookups",
                "2",
                "--max-upc-searches",
                "2",
                "--download-images",
                "--render-qr",
            ]
        )
        assert rc == 0
        assert len(upc_calls) >= 1

        # 4. Full verify including QR optical decode, Pillow image validation, and web UI hooks
        assert (
            cli_main(
                [
                    "verify",
                    "--output-dir",
                    str(out_dir),
                    "--check-qr-files",
                    "--check-images",
                    "--check-web",
                ]
            )
            == 0
        )

        # Verify alias brand file (`ren.json`) has 0 products and points to `ren-clean-skincare`
        ren_alias = json.loads((out_dir / "brands" / "ren.json").read_text(encoding="utf-8"))
        ren_canon = json.loads(
            (out_dir / "brands" / "ren-clean-skincare.json").read_text(encoding="utf-8")
        )
        assert ren_alias["alias_of"] == "ren-clean-skincare"
        assert ren_alias["product_count"] == 0
        assert ren_alias["products"] == []
        assert ren_canon["product_count"] == 1
        assert ren_alias["canonical_product_count"] == 1

        # Verify summary invariants
        summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
        assert summary["duplicate_gtin14_count"] == 0
        assert summary["total_brands_in_directory"] == 163
        assert summary["cross_source_verified_count"] >= 1
        assert summary["local_images_downloaded_count"] >= 2
        assert summary["qr_files_rendered_count"] >= 2

        # 5. Offline replay makes ZERO additional HTTP calls to mock UPCitemdb
        calls_before = len(upc_calls)
        offline_out = tmp_path / "offline_output"
        assert (
            cli_main(
                [
                    "run-all",
                    "--offline",
                    "--output-dir",
                    str(offline_out),
                    "--brands-seed",
                    str(BRANDS_SEED),
                    "--obf-jsonl",
                    str(obf_jsonl),
                    "--no-opf",
                    "--upc-cache",
                    str(upc_cache),
                    "--no-download-images",
                    "--render-qr",
                ]
            )
            == 0
        )
        assert len(upc_calls) == calls_before
        assert (
            cli_main(
                [
                    "verify",
                    "--output-dir",
                    str(offline_out),
                    "--check-qr-files",
                    "--check-web",
                ]
            )
            == 0
        )
    finally:
        server.shutdown()
        server.server_close()


def test_brand_matcher_smoke_on_seed() -> None:
    registry = BrandRegistry.from_entries(load_brands_seed(BRANDS_SEED))
    matcher = BrandMatcher(registry)
    res = matcher.match_obf_record(
        brands_raw="REN",
        brands_tags=["en:ren"],
        categories_tags=["en:skincare"],
    )
    assert res is not None
    assert hasattr(res, "brand_slug")
    assert res.brand_slug == "ren-clean-skincare"
