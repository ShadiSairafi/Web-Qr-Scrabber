# Web-Qr-Scrabber (`qr-scrabber`)

An end-to-end Python 3.11+ barcode & QR harvesting, cross-source verification, and interactive lookup system for all **163 Glow & Glare brands** (`https://glowandglare.com/pages/shop-by-brand`).

## What It Does

1. **Authoritative Brand Directory Extraction**
   - Extracts all **163 A–Z Glow & Glare brand entries** (161 distinct brand entities, 160 external brands, 12 Most Loved) from the Shop-by-Brand directory into `data/brands_seed.json` without scraping Glow & Glare product listings.
   - Deduplicates intra-directory aliases (`REN` → `ren-clean-skincare`, `Sweet Chief` → `sweet-chef`) and applies a 4-tier brand matcher with the **Open Beauty Facts Leaf-Brand Rule** (preventing parent-conglomerate leaks like `L'Oréal` absorbing `Maybelline` or `Lancôme`) and beauty/fragrance category disambiguation gates.

2. **Multi-Source External Barcode, Name & Image Harvesting**
   - **Open Beauty Facts (+ Open Products Facts)**: Streams the full bulk dumps (`openbeautyfacts-products.jsonl.gz`, `en.openbeautyfacts.org.products.csv.gz`, and `en.openproductsfacts.org.products.csv.gz`) with ETag caching.
   - **UPCitemdb**: Uses a single keep-alive HTTP session with **consolidated 2-UPC comma-separated batch lookups** (`/prod/trial/lookup?upc=A,B`) for cross-source verification, budgeted `/prod/trial/search` queries for high-priority brands, rate-limit pacing, and a persistent local disk cache (`data/upcitemdb_cached_responses.json`) for deterministic offline replay.
   - **Product Pictures & Names**: Reconciles clean human-readable product names (`product_name`, `product.name`, `source_names`, `alternate_names`), captures remote image URLs, and downloads/verifies compact local thumbnails (`<= 320x320` JPEG via Pillow) into `output/images/<brand>/<gtin14>.jpg`.
   - **Documented Upstream Gate (`barcodelookup.com`)**: `https://www.barcodelookup.com/<query>` responds with `HTTP 403` Cloudflare Managed Challenge (`cf-mitigated: challenge`). `qr-scrabber check-sources` records this transparently without bypassing security gates.

3. **GS1 Normalization, 2D QR Generation & Cross-Source Reconciliation**
   - Validates GS1 Modulo-10 check digits across `EAN-8`, `UPC-A`, `EAN-13`, and `GTIN-14`, splits valid concatenated multi-EAN fields, unifies all codes to canonical `gtin14`, and quarantines invalid/placeholder/restricted-circulation/conflicting records in `output/quarantine.json`.
   - Emits both 1D barcode fields and **ISO/IEC 18004 / GS1 Digital Link** 2D QR metadata (`https://id.gs1.org/01/<gtin14>`) plus rendered SVG QR files (`output/qr/<brand>/<gtin14>.svg`) optically verifiable with `pyzbar`/`zbarimg`.
   - Enforces **strict zero-duplicate uniqueness** (`duplicate_gtin14_count == 0`) across `output/all_products.json` and all 163 `output/brands/<slug>.json` files.

4. **Single-Search-Bar Lookup Web App**
   - Self-contained Vanilla HTML5 / HSL CSS / ES JS search page exported directly into `output/{index.html,index.css,app.js}` and served via `qr-scrabber serve`.
   - **Exact Serial Lookup**: Paste or type any `GTIN-14`, `EAN-13`, `UPC-A`, `EAN-8`, hyphenated/spaced serial, or GS1 Digital Link URL (`https://id.gs1.org/01/<gtin14>`) to spotlight the matching product with its photo, name, brand, serials, verification badge, and scannable QR code.
   - **Regex Name & Brand Search**: Type any case-insensitive regular expression (e.g. `serum|cleanser`, `^lip`) to browse multiple matching products with pictures, names, serials, and clickable enlarged QR previews (`#qr-modal`), with graceful literal fallback on incomplete regex syntax.

---

## Quick Start

### Environment Setup

```bash
bash /home/antigravity/env-setup.sh
```

Or manually with `uv`:

```bash
uv sync --python 3.11
source .venv/bin/activate
```

### CLI Commands

```bash
# Check upstream reachability (including transparent barcodelookup.com status)
qr-scrabber check-sources --json

# Sync Glow & Glare 163-brand directory
qr-scrabber sync-brands --offline --out output/brands_manifest.json

# Run full harvest, reconciliation, image download, QR SVG rendering, and web UI export
qr-scrabber run-all --output-dir output --download-images --render-qr

# Deterministic offline replay using local cached dumps & UPCitemdb cache
qr-scrabber run-all --offline --output-dir output

# Verify JSON schemas, GS1 check digits, zero duplicates, optical QR decode, Pillow images & web bundle
qr-scrabber verify --output-dir output --check-qr-files --check-images --check-web

# Serve the interactive single-search-bar web app locally
qr-scrabber serve --output-dir output --host 127.0.0.1 --port 8765
```

---

## Output Directory Layout (`output/`)

| Path | Description |
|------|-------------|
| `output/all_products.json` | Canonical deduplicated array of all verified products sorted by `(brand.slug, gtin14)` |
| `output/brands/<slug>.json` | 163 per-brand JSON files (alias files like `ren.json` point via `alias_of` with `0` duplicate items) |
| `output/brands_manifest.json` | Master brand index with counts, Most Loved flags, and relative file paths |
| `output/comparison_report.json` | Cross-source match analytics, `rapidfuzz` title similarities, sub-brand resolutions & conflicts |
| `output/quarantine.json` | Invalid check digits, placeholders, restricted prefixes, missing names, and brand conflicts |
| `output/summary.json` | High-level harvest stats, image/QR counts, source statuses, and UPCitemdb quota log |
| `output/images/<brand>/<gtin14>.jpg` | Downloaded and Pillow-verified compact product thumbnails |
| `output/qr/<brand>/<gtin14>.svg` | Rendered ISO 18004 GS1 Digital Link SVG QR codes |
| `output/{index.html,index.css,app.js}` | Static single-search-bar lookup web application |

---

## Running Tests

```bash
.venv/bin/ruff check src tests
.venv/bin/pytest -q --basetemp=/home/antigravity/.cache/pytest_tmp tests/unit/
```
