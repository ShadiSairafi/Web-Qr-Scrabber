# Project: Web-Qr-Scrabber

## Architecture

`Web-Qr-Scrabber` (`qr-scrabber`) is a modular Python 3.11+ CLI application and data pipeline that extracts the authoritative **163-brand directory** from **Glow and Glare** (`/home/antigravity/image.png` / `https://glowandglare.com/pages/shop-by-brand`), harvests external product barcodes across all brands from **Open Beauty Facts** (+ optional **Open Products Facts**) bulk dumps and **UPCitemdb** (consolidated multi-UPC batch cross-verification + budgeted search with persistent local disk caching), validates and canonicalizes GS1 barcodes (`GTIN-14`, `EAN-13`, `UPC-A`, `EAN-8`) alongside **ISO 18004 / GS1 Digital Link 2D QR representations**, performs cross-source reconciliation and strict deduplication (`0` duplicate GTINs), and exports clean downstream-ready JSON files under `output/`.

### System Data Flow
```text
[Glow & Glare Shop-by-Brand HTML / brands_seed.json]
        │ (163 A–Z vendor entries -> 161 distinct brand entities, 0 product-page scraping)
        ▼
[Brand Registry & 4-Tier Matcher]
  - NFKD + collapsed/spaced punctuation slugs
  - Alias pairs: REN -> ren-clean-skincare, Sweet Chief -> sweet-chef
  - Store brand flag: Glow and Glare -> is_store_own_brand=true
  - OBF Leaf-Brand Rule (blocks parent-conglomerate leaks e.g. L'Oréal -> Maybelline/Lancôme)
  - Risk-Tier B/C beauty/fragrance positive + negative category gates
        │
        ├──────────────────────────────────────────────┐
        ▼                                              ▼
[Open Beauty Facts (+ OPF) Bulk Harvester]    [UPCitemdb Consolidated Harvester]
  - Streams openbeautyfacts-products.jsonl.gz   - Single keep-alive HTTP session + persistent cache
  - Optional CSV/OPF enrichment                 - Batch Lookup: /prod/trial/lookup?upc=A,B (2/req, >=10.5s)
  - Splits concatenated 26/39-char EANs         - Budgeted Search: /prod/trial/search (<=16/run, >=16s, 5/page)
        │                                              │
        └──────────────────────┬───────────────────────┘
                               ▼
            [GS1 Normalizer + Cross-Source Reconciler]
              - Modulo-10 check digit + GTIN-14 canonicalization
              - Quarantines invalid check digits, placeholders, restricted prefixes (02/04/20-29/0000)
              - Intra-source & cross-source GTIN-14 join + rapidfuzz title similarity
              - Parent/sub-brand resolution + cross-brand conflict quarantine
              - Global uniqueness check: duplicate_gtin14_count == 0
                               │
                               ▼
              [Atomic JSON + Product Image + QR SVG + Web UI Exporter]
                - output/brands/<slug>.json (163 deterministic files)
                - output/all_products.json
                - output/brands_manifest.json
                - output/comparison_report.json
                - output/quarantine.json
                - output/summary.json
                - output/images/<slug>/<gtin14>.jpg (downloaded & Pillow-verified product thumbnails)
                - output/qr/<slug>/<gtin14>.svg (rendered ISO 18004 QR codes)
                - output/{index.html, index.css, app.js} + `qr-scrabber serve` (single-search-bar web app)
```

### Documented Upstream Source Constraint (`barcodelookup.com`)
Empirical verification confirms `https://www.barcodelookup.com/<query>` returns `HTTP/2 403 Forbidden` with `server: cloudflare`, `cf-mitigated: challenge` (`<title>Barcode Lookup Security Verification</title>`, Cloudflare Managed Challenge `cType: 'managed'`). Per `ORIGINAL_REQUEST.md` Turns 4–6, the pipeline uses keyless **Open Beauty Facts** bulk exports + **UPCitemdb** consolidated trial endpoints, while `barcodelookup_probe.py` / `qr-scrabber check-sources` records live source reachability metadata transparently without bypassing security gates or fabricating records.

### Workspace & Scratch Hygiene Invariants
- **NEVER use `/tmp`** anywhere in scripts, harvesters, web server, Playwright browsers, tests, or subagent probes. Always set `TMPDIR` / scratch space under `/home/antigravity/.gemini/jetski/scratch/teamwork_tmp/<agent_or_run_id>` (and pass `--basetemp=...` to `pytest`), cleaning up temporary directories when finished.
- `.agents/` is strictly gitignored in `.gitignore`; never create `AGENTS.md` or `GEMINI.md`.
- **Headless Browser Rule**: Always launch system `/usr/lib/chromium/chromium` (`executable_path="/usr/lib/chromium/chromium"`, `args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]`) with `TMPDIR` pointed at scratch space — NEVER run `playwright install` (saves 400+ MB on `/` and `/home`).
- **UPCitemdb Quota Protection**: Unit tests (`tests/unit/`) and E2E tests (`tests/e2e/`) MUST make **0 live network requests** to `api.upcitemdb.com` (use local mock transports, loopback servers, or committed `data/upcitemdb_cached_responses.json`). Only Milestone 4's cached production harvest touches live `api.upcitemdb.com`, immediately persisting all responses to disk.

---

## Feature Inventory

Every feature discovered during Step 0 Survey (`survey_explorer_1`, `survey_spec_miner_1`, `survey_explorer_2`) and `ORIGINAL_REQUEST.md` (Turns 1–7 + Follow-ups 1, 2 & 3) is assigned to an implementation milestone (none unassigned):

| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| F1 | Cloud & Repo Environment Setup | Idempotent `/home/antigravity/env-setup.sh` (`set -euo pipefail`, exits `0` on consecutive runs, installs `libzbar0`, `zbar-tools`, `qrencode` via `sudo apt-get`, syncs `.venv` via `uv` including `playwright` Python driver for system `/usr/lib/chromium/chromium`, exports `.venv/bin` in `~/.bashrc`), plus repo `pyproject.toml` and `.gitignore` excluding `.agents/`, `.venv/`, caches, and raw dumps. | M1 | Turn 1, `survey_explorer_1`, `survey_explorer_2` |
| F2 | Authoritative Glow & Glare Brand Extraction | Extracts all **163 unique A–Z brands** (and identifies the 12 "Most Loved" featured brands across 175 vendor anchors / 24 letters) from `https://glowandglare.com/pages/shop-by-brand` matching `/home/antigravity/image.png` 100%, safely parsing unescaped apostrophes (`L'Oreal`, `Kiehl's`, `Juvia's Place`, `Paula's Choice`) via `BeautifulSoup`/`lxml` with deterministic offline fallback `data/brands_seed.json`, without scraping Glow & Glare product listings. | M2 | Turns 2 & 5, `survey_explorer_1`, `survey_explorer_2` |
| F3 | 4-Tier Brand Normalization, Alias Linking & Disambiguation | Reconciles intra-list duplicates/typos (`REN` -> `alias_of: "ren-clean-skincare"`, `Sweet Chief` -> `alias_of: "sweet-chef"`), flags house brand (`Glow and Glare` -> `is_store_own_brand: true`), normalizes diacritics (`CHĀMPO`, `Lancôme`, `Chloé`, `Estée Lauder`, `Kérastase`, `ODACITÉ`) and punctuation (`L'Oreal`↔`L'Oréal`, `Dr. Jart+`, `Hero.`, `e.l.f`, `ONE/SIZE`, `&`↔`and`), enforces the **OBF Leaf-Brand Rule** (blocking conglomerate leaks), and applies Risk-Tier B/C beauty/fragrance category gates against fashion/tech/food collisions. | M2 | Turns 2 & 6, `survey_explorer_1`, `survey_explorer_2` |
| F4 | GS1 Barcode Validation, Unification & Quarantine | Validates GS1 Modulo-10 check digits across EAN-8 (`8`), UPC-A (`12`), EAN-13 (`13`), and GTIN-14 (`14`); splits valid concatenated 26/39-char multi-EAN strings; normalizes all valid codes to canonical `gtin14` (unifying 12-digit UPC-A and `0`-padded EAN-13 while preserving EAN-8); routes non-numeric, invalid check-digit, uniform/sequential placeholder, and GS1 Restricted Circulation (`02`, `04`, `20`–`29`, `0000`) codes to quarantine. | M1 | Turn 6, `survey_spec_miner_1`, `survey_explorer_2` |
| F5 | Dual 1D Barcode + 2D GS1 Digital Link QR Representation | Emits structured 1D barcode fields (`gtin14`, `ean13`, `upc_a`, `ean8`, `primary_code`, `barcode_type`) AND 2D QR fields (`gs1_digital_link`: `https://id.gs1.org/01/{gtin14}`, `qr_payload_text`, `qr_payload_data`, `qr_standard`, `qr_svg_path`), plus renders ISO 18004 SVG/PNG QR codes via `segno` (with UTF-8 ECI for non-ASCII) optically verifiable back to `gs1_digital_link` via `zbarimg`/`pyzbar`. | M1 | Turns 2 & 5, `survey_explorer_1`, `survey_explorer_2` |
| F6 | Open Beauty Facts (+ OPF) Full Bulk Dataset Harvester | Downloads/streams `openbeautyfacts-products.jsonl.gz` + `en.openbeautyfacts.org.products.csv.gz` (plus optional `en.openproductsfacts.org.products.csv.gz` enrichment) in a single bulk pass with conditional ETag/local file caching, extracting product names (`product_name_en`, `product_name`, `generic_name`) and remote product pictures (`image_url`, `image_small_url`, and deterministic `images.openbeautyfacts.org` paths from `images.front_<lc>.rev`), with custom `--obf-jsonl` / `--obf-csv` path overrides. | M3 | Turns 4–6, Follow-ups 1 & 2, `survey_spec_miner_1` |
| F7 | UPCitemdb Consolidated Batch & Search Harvester with Persistent Cache | Single keep-alive HTTP session backed by SQLite/JSON disk cache (`data/upcitemdb_cached_responses.json`), executing **consolidated 2-UPC comma-separated batch lookups** (`/prod/trial/lookup?upc=A,B`, ≥10.5s pacing) for cross-source verification and **consolidated/budgeted searches** (`/prod/trial/search`, ≤16 calls, ≥16s pacing, dynamic 5-item `offset` pagination, graceful `429` handling, `--offline` replay), capturing titles, merchant offer titles, and product `images`. | M3 | Turns 4–6, Follow-ups 1 & 2, `survey_spec_miner_1` |
| F8 | Source Reachability Diagnostics & `barcodelookup.com` Gate Handling | Adapter/probe (`check-sources` + `summary.json["source_status"]`) that checks source status cleanly, detects `barcodelookup.com` HTTP 403 `cf-mitigated: challenge` without crashing or bypassing, and documents active vs blocked sources. | M3 | Turns 2–4, `survey_spec_miner_1` |
| F9 | Cross-Source Comparison, Title Similarity & Conflict Resolution | Full outer join on canonical `gtin14` across OBF and UPCitemdb; computes `rapidfuzz` title similarity (`0.0–100.0`); classifies `verified_cross_source`, `verified_cross_source_subbrand_resolved`, `verified_single_source`, and `inferred_title_prefix`; resolves conglomerate/sub-brand matches to the specific leaf sub-brand; quarantines true cross-brand GTIN conflicts into `quarantine.json` and logs full analytics in `comparison_report.json`. | M4 | Turn 6, `survey_explorer_2` |
| F10 | Strict Intra-Source & Global Deduplication Invariant | Deduplicates repeated records within OBF, within UPCitemdb, across 12-digit UPC-A vs 13-digit `0`-prefixed EAN-13, and across alias brand files (`ren` / `ren-clean-skincare`, `sweet-chief` / `sweet-chef`), enforcing `duplicate_gtin14_count == 0` across `all_products.json` and the union of all 163 `output/brands/*.json` files. | M4 | Turn 6, `survey_explorer_2` |
| F11 | Downstream-Ready Per-Brand & Master JSON Dataset Export | Atomically writes UTF-8 formatted JSON (`indent=2`, deterministic sorting) to `output/brands/<slug>.json` (all 163 Glow & Glare brands accounted for), `output/all_products.json`, `output/brands_manifest.json`, `output/comparison_report.json`, `output/quarantine.json`, and `output/summary.json`, and executes the real full production harvest across all 163 brands. | M4 | Turns 2, 6 & 7, `survey_explorer_2` |
| F12 | User-Facing CLI, Offline Replayability, Validation & Documentation | Provides `qr-scrabber` / `python -m qr_scrabber` CLI (`sync-brands`, `harvest`, `build-catalog`, `fetch-images`, `render-qr`, `build-web`, `serve`, `verify`, `check-sources`, `run-all`, `--help`, `--version`), clean non-zero error exit codes (no raw tracebacks on user input errors), offline replay mode (`--offline`), and comprehensive `README.md`. | M4, M5 | Turns 1–7, Follow-up 3 |
| F13 | Product Picture Harvesting, Local Thumbnail Download & Barcode Image Linking | Captures remote product picture URLs (`image_url` / `primary_image_url`, `thumbnail_url`, `image_urls`) from Open Beauty Facts and UPCitemdb on every barcode record AND downloads/verifies compact local product images (`max 320x320` via `Pillow`, rejecting non-image/corrupt HTTP payloads) under `output/images/<brand_slug>/<gtin14>.<ext>` linked via `local_image_path` and `image_status` (`"downloaded"`, `"remote_only"`, `"unavailable"`), with bounded async concurrency, disk caching, and graceful fallback when a remote image is missing/unreachable. | M3, M4 | Follow-up 1 (`2026-11-18T23:43:23Z`) |
| F14 | Product Name Extraction, Cleaning & Cross-Source Reconciliation | Ensures every barcode JSON record prominently includes the clean canonical product name (`product_name` at top level and `product.name`), reconciled across Open Beauty Facts (`product_name_en`, `product_name`, `abbreviated_product_name`, `generic_name`) and UPCitemdb (`title`, merchant `offers[*].title`), while preserving per-source titles in `product.source_names` and distinct variants in `product.alternate_names` (and inside `qr.qr_payload_data.name`). | M3, M4 | Follow-up 2 (`2026-11-18T23:44:25Z`) |
| F15 | Modern Light-on-the-Eyes Single-Search-Bar Web UI (Serial + Regex Search) | Semantic HTML5 + Vanilla CSS (`index.css` HSL design system, warm alabaster/ivory palette, refined typography, generous whitespace, zero dark/cliché dashboard tropes per `web-app-development` skill) + Vanilla ES JS (`app.js`) centered on a **single unified search bar** (`#search-input`): (1) exact lookup by any QR/barcode serial (`gtin14`, `ean13`, `upc_a`, `ean8`, hyphenated/spaced serial, or `https://id.gs1.org/01/...` GS1 Digital Link) highlighting the product with picture, name, brand, serials, scannable QR code & zoom modal (`#qr-modal`), and verification badge; (2) case-insensitive **regex search** across product names/brands displaying multiple matching products cleanly with graceful fallback + inline `#regex-status` indicator on incomplete regex syntax and smooth paginated/load-more (`#load-more-btn`) performance across thousands of items. | M5 | Follow-up 3 (`2026-11-19T00:05:52Z`) |
| F16 | Static Output Web Bundle & `qr-scrabber serve` HTTP Server | Exports `index.html`, `index.css`, and `app.js` directly into `<output-dir>/` (via `run-all` and `build-web`) so `<output-dir>/` works over any static HTTP server, AND provides `qr-scrabber serve [--output-dir <dir>] [--host 127.0.0.1] [--port 8765]` serving JSON catalogs, local product thumbnails, rendered QR SVGs (with dynamic `/api/qr/<gtin14>.svg` fallback and `/api/health` endpoint), verified in real headless Chromium (`/usr/lib/chromium/chromium` via `playwright`) with zero console errors and zero HTTP 404s. | M5 | Follow-up 3 (`2026-11-19T00:05:52Z`) |

---

## Milestones

| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Environment Setup, Scaffolding, GS1 & QR Core Engine | `/home/antigravity/env-setup.sh`, `.gitignore`, `pyproject.toml` (incl. `playwright` Python driver), `.venv`, `models.py`, `gs1.py`, `qr.py`, unit tests (`F1`, `F4`, `F5`, `F13`/`F14` models) | none | IN_PROGRESS (`3fe886c10364d90ec2bea25332ba419d`) |
| M2 | Glow & Glare Brand Extractor, 163-Brand Registry & 4-Tier Matcher | `data/brands_seed.json`, `brands/extractor.py`, `brands/registry.py`, `brands/matcher.py`, unit tests (`F2`, `F3`) | M1 | PLANNED |
| M3 | Cached Bulk OBF Harvester, Consolidated UPCitemdb Client, Image Downloader & Source Probe | `harvesters/cache.py`, `harvesters/openbeautyfacts.py`, `harvesters/upcitemdb.py`, `harvesters/images.py`, `harvesters/barcodelookup_probe.py`, offline/mock unit tests (`F6`, `F7`, `F8`, `F13` harvester, `F14` extraction) | M1, M2 | PLANNED |
| M4 | Reconciliation, Name/Image Linking, Deduplication, Exporter, CLI & Full Production Harvest | `reconcile.py`, `exporter.py`, `cli.py`, `__main__.py`, live cached harvest -> `data/upcitemdb_cached_responses.json` + complete `output/` (`brands/`, `images/`, `qr/`) + `README.md` (`F9`, `F10`, `F11`, `F12`, `F13` export, `F14` reconciliation) | M1, M2, M3 | PLANNED |
| M5 | Modern Single-Search-Bar Web UI & `qr-scrabber serve` HTTP Server | `src/qr_scrabber/web/{index.html,index.css,app.js}`, `src/qr_scrabber/server.py`, `build-web` & `serve` CLI integration + `output/{index.html,index.css,app.js}`, Playwright headless Chromium verification (`F15`, `F16`, `F12` web CLI) | M4 | PLANNED |
| E2E | E2E Testing Track (Opaque-Box Tiers 1–4) | `TEST_INFRA.md`, `tests/e2e/` (≥176 opaque-box requirement tests across all 16 features `F1`–`F16` Tiers 1–4, including Playwright headless Chromium UI E2E tests), `TEST_READY.md` | M1 (for `.venv`/`pytest`/`zbarimg`/`playwright`) | PLANNED |
| M6 | Final Milestone: E2E Pass (Tiers 1–4) + Adversarial Hardening (Tier 5) | Phase 1: 100% E2E pass across Tiers 1→2→3→4 + whole-project audit; Phase 2: Tier 5 white-box challenger→worker→reviewer hardening across backend & web UI | M5, E2E (`TEST_READY.md`) | PLANNED |

---

## Interface Contracts

### 1. CLI Entry Point & Subcommands (`qr-scrabber` / `python -m qr_scrabber`)

Installed into `/home/antigravity/ShadiSairafi/Web-Qr-Scrabber/.venv/bin/qr-scrabber` (and on `PATH` via `~/.bashrc`).
Exit codes:
- `0`: Success (including `verify` when all dataset invariants pass).
- `1`: Domain / verification / runtime validation failure (clean human-readable error message to `stderr`, never an uncaught Python traceback).
- `2`: Invalid CLI flag / argument usage (`argparse` / CLI usage error).

Subcommands:
1. `qr-scrabber --version` / `qr-scrabber --help`
2. `qr-scrabber sync-brands [--html-file <path> | --url <url>] [--seed-file <path>] [--out <path>] [--offline]`
   - Extracts Glow & Glare brands from `--html-file` (if given), live `--url` (default `https://glowandglare.com/pages/shop-by-brand` when not `--offline`), or `--seed-file` (default `data/brands_seed.json`), and writes normalized 163-brand catalog JSON to `--out` (default `output/brands_manifest.json`).
3. `qr-scrabber check-sources [--offline] [--json]`
   - Probes/reports availability of `openbeautyfacts`, `upcitemdb`, and `barcodelookup` (reporting `blocked_cloudflare_challenge` / HTTP 403 on `barcodelookup.com`; in `--offline` mode reports cached/configured status without network calls).
4. `qr-scrabber run-all [options]` (and granular `harvest` / `build-catalog`):
   - `--output-dir <dir>` (default: `output`)
   - `--brands-html <path>` (optional local HTML snapshot of Glow & Glare Shop-by-Brand)
   - `--brands-seed <path>` (default: `data/brands_seed.json`)
   - `--obf-jsonl <path>` (optional local gzipped or plain `.jsonl`/`.jsonl.gz` Open Beauty Facts input; if omitted and not `--offline`, downloads/uses cached `data/raw_dumps/openbeautyfacts-products.jsonl.gz`)
   - `--obf-csv <path>` (optional local `.csv`/`.csv.gz` TSV enrichment input)
   - `--opf-jsonl <path>` / `--opf-csv <path>` / `--no-opf` (optional secondary Open Products Facts input)
   - `--upc-cache <path>` (default: `data/upcitemdb_cached_responses.json`)
   - `--upc-base-url <url>` (default: `https://api.upcitemdb.com/prod/trial` — allows local mock HTTP server testing in E2E/unit suites without touching live `api.upcitemdb.com`!)
   - `--max-upc-lookups <int>` (default: `30`, max 2 comma-separated UPCs per request)
   - `--max-upc-searches <int>` (default: `15`, spaced ≥16s on live host, `0s` when `--upc-base-url` is `127.0.0.1`/`localhost`)
   - `--offline` (boolean flag: makes ZERO outbound network requests; uses `--brands-html` or `--brands-seed`, local `--obf-jsonl`/`--obf-csv` or cached dump, `--upc-cache`, and cached product images if present)
   - `--download-images` / `--no-download-images` (default: enabled when not `--offline`, or uses local/mock URLs when explicitly `--download-images` is passed; downloads & `Pillow`-normalizes compact product thumbnails `max 320x320` into `<output-dir>/images/<brand_slug>/<gtin14>.<ext>`, setting `local_image_path` and `image_status`; `--max-images <int>` optional cap, default `0` = all products with valid image URLs; `--image-concurrency <int>` default `12`)
   - `--render-qr` / `--no-render-qr` (default: enabled — renders SVG QR files under `<output-dir>/qr/<brand_slug>/<gtin14>.svg` and populates `qr.qr_svg_path` relative to `<output-dir>`; `--render-qr-limit <int>` optional cap, default `0` = all matched products)
   - Also writes `<output-dir>/{index.html, index.css, app.js}` when web assets are present so `<output-dir>/` is immediately browsable!
5. `qr-scrabber fetch-images --output-dir <dir> [--brand <slug>] [--max-images <int>] [--concurrency <int>]`
   - Downloads/verifies local product pictures (`<output-dir>/images/<brand_slug>/<gtin14>.<ext>`) for products in `<output-dir>/all_products.json` that have remote `image_urls`, updating `local_image_path`, `image_status`, per-brand JSON files, and `summary.json` atomically with graceful fallback on 404/timeout/invalid-image payloads.
6. `qr-scrabber render-qr --output-dir <dir> [--brand <slug>] [--format svg|png] [--limit <int>]`
   - Generates ISO 18004 QR code image files (`<output-dir>/qr/<brand_slug>/<gtin14>.<ext>`) encoding `qr.gs1_digital_link` for products already in `<output-dir>/all_products.json` and updates `qr_svg_path` / `qr_png_path`.
7. `qr-scrabber build-web --output-dir <dir>`
   - Exports the self-contained Vanilla HTML/CSS/JS search application (`index.html`, `index.css`, `app.js`) into `<output-dir>/` and verifies `<output-dir>/all_products.json` + `<output-dir>/summary.json` exist.
8. `qr-scrabber serve [--output-dir <dir>] [--host <host>] [--port <port>]`
   - Starts a clean, fast multi-threaded HTTP server (`default: 127.0.0.1:8765`, supporting `--port 0` ephemeral port printing `Listening on http://<host>:<actual_port>/` with `flush=True`) serving `<output-dir>/` (`/` -> `index.html`, `/all_products.json`, `/summary.json`, `/brands/*.json`, `/images/...`, `/qr/...`), plus `/api/health` (`{"status": "ok", "total_products": N}`) and dynamic `/api/qr/<gtin14>.svg` fallback if an SVG file is missing on disk.
9. `qr-scrabber verify --output-dir <dir> [--check-qr-files] [--check-images] [--check-web]`
   - Validates `<output-dir>` schema compliance, non-empty `product_name` / `product.name` on 100% of emitted products, all 163 brand files present (or matching manifest count when custom `--brands-html`/`--brands-seed` was used), GS1 Modulo-10 check digits on 100% of emitted products, zero restricted-circulation codes in clean catalog, `duplicate_gtin14_count == 0` across `all_products.json` and all `brands/*.json`, (with `--check-qr-files`) decodes rendered QR images via `pyzbar`/`zbarimg`, (with `--check-images`) verifies every non-null `local_image_path` exists inside `<output-dir>` and decodes cleanly as a valid raster image via `Pillow` (`width > 0`, `height > 0`), and (with `--check-web`) verifies `index.html`, `index.css`, `app.js`, and required DOM hooks (`#search-input`, `#results-grid`, `#qr-modal`) exist in `<output-dir>/`. Exits `0` if valid, `1` if any check fails.

### 2. Web Search UI DOM & Behavior Contract (`output/index.html`, `index.css`, `app.js`)

To allow deterministic opaque-box Playwright / headless browser E2E testing without coupling to internal JS functions, the web page at `/` (`index.html`) exposes these stable semantic elements and behaviors:
- **Document & Aesthetics**:
  - `<title>` non-empty & descriptive; `<meta name="description" ...>`; single `<h1>`; Vanilla CSS (`index.css`, no Tailwind); soft light-on-the-eyes background (high luminance `L > 0.90` warm alabaster/ivory, dark readable text contrast `>= 7:1`, zero dark-mode purple tropes, single hero search bar rather than multi-panel admin dashboard).
- **Interactive Elements & IDs**:
  - `#search-input` (`<input type="search">` — single unified hero search bar; handles both barcode/QR serials and regex name/brand queries).
  - `#search-clear` (`<button>` — clears `#search-input` and resets view).
  - `#search-mode-indicator` / `#regex-status` — displays current match mode (e.g., `"Exact Serial Match"`, `"Regex Search"`, or `"Literal fallback (incomplete regex)"` without throwing uncaught JS errors when `#search-input` contains invalid regex syntax like `[unclosed(`).
  - `#results-count` — shows total matching products count.
  - `#exact-match-card` — rendered/visible when `#search-input` matches a product's barcode serial (`gtin14`, `ean13`, `upc_a`, `ean8`, `primary_code`, hyphenated/spaced digits, or `https://id.gs1.org/01/<gtin14>` GS1 Digital Link), displaying the product's picture (`<img>`), product name, brand name, barcode serials, scannable QR code (`<img>` pointing to `qr/<brand>/<gtin14>.svg` or `/api/qr/<gtin14>.svg`), and verification status.
  - `#results-grid` — responsive container of product cards (`article.product-card[data-gtin14="<gtin14>"]`), each displaying:
    - `.product-title` (product name)
    - `.product-brand` (brand display name)
    - `.product-image` (`<img>` loading `local_image_path` -> fallback `image_url` -> clean typographic placeholder on error, zero broken image icons / unhandled 404s)
    - `.product-serial` (primary barcode / GTIN-14 serial + copy button `.copy-serial-btn`)
    - `.product-qr` (`<img>` scannable QR SVG + click to open `#qr-modal` enlarged scannable QR preview)
    - `.verification-badge` (source match badge)
  - `#load-more-btn` — progressive pagination button (`PAGE_SIZE` e.g. `48` cards per page) so broad regex queries like `.*` render instantaneously without freezing the browser DOM.
  - `#empty-state` — clean friendly empty state when 0 products match.
  - `#qr-modal` (`<dialog>` or modal container with `#qr-modal-close`) — enlarged scannable QR view with product name & serials.

### 3. Canonical JSON Output Contracts (`output/`)

1. **`output/all_products.json`**: JSON array of `ProductRecord` objects sorted deterministically by `(brand.slug, gtin14)`:
   ```json
   {
     "gtin14": "00887167485471",
     "product_name": "Advanced Night Repair Synchronized Multi-Recovery Complex",
     "image_url": "https://images.openbeautyfacts.org/images/products/088/716/748/5471/front_en.4.400.jpg",
     "local_image_path": "images/estee-lauder/00887167485471.jpg",
     "brand": {
       "slug": "estee-lauder",
       "display_name": "Estée Lauder",
       "glow_and_glare_vendor_url": "https://glowandglare.com/collections/vendors?q=Est%C3%A9e%20Lauder",
       "matched_raw_brands": ["Estée Lauder", "xx:estee-lauder"]
     },
     "product": {
       "name": "Advanced Night Repair Synchronized Multi-Recovery Complex",
       "source_names": {
         "openbeautyfacts": "Advanced Night Repair Synchronized Multi-Recovery Complex",
         "upcitemdb": "Estee Lauder Advanced Night Repair Serum 1.7 oz"
       },
       "alternate_names": ["Estee Lauder Advanced Night Repair Serum 1.7 oz"],
       "quantity": "50 ml",
       "categories": ["Skincare", "Face Serums"],
       "description": "Synchronized Multi-Recovery Complex serum",
       "primary_image_url": "https://images.openbeautyfacts.org/images/products/088/716/748/5471/front_en.4.400.jpg",
       "thumbnail_url": "https://images.openbeautyfacts.org/images/products/088/716/748/5471/front_en.4.200.jpg",
       "image_urls": [
         "https://images.openbeautyfacts.org/images/products/088/716/748/5471/front_en.4.400.jpg",
         "https://images.openbeautyfacts.org/images/products/088/716/748/5471/front_en.4.200.jpg"
       ],
       "local_image_path": "images/estee-lauder/00887167485471.jpg",
       "image_status": "downloaded"
     },
     "barcode": {
       "gtin14": "00887167485471",
       "ean13": "0887167485471",
       "upc_a": "887167485471",
       "ean8": null,
       "primary_code": "887167485471",
       "barcode_type": "UPC-A",
       "raw_codes": ["0887167485471", "887167485471"],
       "gs1_check_digit_valid": true,
       "gs1_restricted_circulation": false
     },
     "qr": {
       "gs1_digital_link": "https://id.gs1.org/01/00887167485471",
       "qr_payload_text": "https://id.gs1.org/01/00887167485471",
       "qr_payload_data": {
         "schema": "glow-glare-qr/v1",
         "gtin14": "00887167485471",
         "code": "887167485471",
         "type": "UPC-A",
         "brand": "Estée Lauder",
         "name": "Advanced Night Repair Synchronized Multi-Recovery Complex",
         "image_url": "https://images.openbeautyfacts.org/images/products/088/716/748/5471/front_en.4.400.jpg"
       },
       "qr_standard": "GS1_DIGITAL_LINK_ISO_18004",
       "qr_svg_path": "qr/estee-lauder/00887167485471.svg"
     },
     "verification": {
       "status": "verified_cross_source",
       "confidence_score": 1.0,
       "sources": ["openbeautyfacts", "upcitemdb"],
       "match_methods": {
         "openbeautyfacts": "brands_tags_leaf",
         "upcitemdb": "exact_brand_field"
       },
       "cross_source_title_similarity": 94.5
     },
     "provenance": {
       "openbeautyfacts_url": "https://world.openbeautyfacts.org/product/0887167485471",
       "upcitemdb_ean": "0887167485471",
       "harvested_at": "2026-11-18T22:45:00Z"
     }
   }
   ```
   *(Invariants: `product_name == product.name` and is always a clean non-empty string; `image_url == product.primary_image_url`; top-level `local_image_path == product.local_image_path`. When no remote picture exists upstream, `image_url` and `local_image_path` are `null` and `product.image_status == "unavailable"`; when remote URLs exist but download was disabled/offline or unreachable, `image_url` is populated, `local_image_path` is `null`, and `product.image_status == "remote_only"`.)*
2. **`output/brands/<slug>.json`** (1 file per brand in manifest; **163 files** for full Glow & Glare catalog):
   ```json
   {
     "brand_slug": "ren",
     "display_name": "REN",
     "letter_section": "R",
     "is_most_loved": false,
     "is_store_own_brand": false,
     "alias_of": "ren-clean-skincare",
     "glow_and_glare_vendor_url": "https://glowandglare.com/collections/vendors?q=REN",
     "product_count": 0,
     "canonical_product_count": 12,
     "cross_source_verified_count": 0,
     "products": []
   }
   ```
   *(For canonical brands, `"alias_of": null` and `"product_count" == len(products) == "canonical_product_count"`.)*
3. **`output/brands_manifest.json`**:
   - Top-level keys: `total_vendor_entries` (`163` for full catalog), `distinct_brand_entities` (`161`), `external_brand_entities` (`160`), `most_loved_count` (`12`), `brands_with_matches`, `brands_zero_matches`, `brands` (list of 163 brand summary objects with `slug`, `display_name`, `letter_section`, `is_most_loved`, `is_store_own_brand`, `alias_of`, `product_count`, `canonical_product_count`, `file_path`).
4. **`output/comparison_report.json`**:
   - Top-level keys: `total_unique_products`, `verified_cross_source_count`, `verified_cross_source_subbrand_resolved_count`, `verified_single_source_count`, `inferred_title_prefix_count`, `source_breakdown` (`openbeautyfacts_raw_matched`, `openbeautyfacts_unique_valid`, `upcitemdb_raw_matched`, `upcitemdb_unique_valid`, `intra_source_duplicates_removed`, `cross_source_exact_gtin_matches`), `cross_source_matches` (list of matched GTIN comparisons with OBF title, UPCitemdb title, `title_similarity`, resolved canonical `product_name`, resolved brand), `parent_subbrand_resolutions`, `brand_conflicts`, `duplicate_gtin14_count` (must be `0`).
5. **`output/quarantine.json`**:
   - Top-level keys: `total_quarantined`, `counts_by_reason` (`NON_NUMERIC_OR_INVALID_LENGTH`, `PLACEHOLDER_PATTERN`, `INVALID_CHECK_DIGIT`, `RESTRICTED_CIRCULATION_PREFIX`, `CATEGORY_MISMATCH_EXCLUDED`, `CROSS_SOURCE_BRAND_CONFLICT`, `MISSING_PRODUCT_NAME`), `records` (quarantined items with `raw_code`, `gtin14`, `reason`, `source`, `raw_brand`, `raw_title`). *(Note: upstream items that lack any recognizable product name across all sources and fallback fields are routed to `MISSING_PRODUCT_NAME` quarantine so 100% of verified catalog products have genuine human-readable product names!)*
6. **`output/summary.json`**:
   - Top-level keys: `generated_at`, `pipeline_version`, `total_brands_in_directory`, `distinct_brand_entities`, `total_unique_verified_products`, `products_with_name_count` (equals `total_unique_verified_products`), `products_with_remote_image_count`, `local_images_downloaded_count`, `cross_source_verified_count`, `quarantined_count`, `duplicate_gtin14_count` (`0`), `qr_files_rendered_count`, `source_status` (`openbeautyfacts`, `openproductsfacts`, `upcitemdb`, `barcodelookup`), `upcitemdb_quota` (`requests_made_this_run`, `cache_hits`, `rate_limit_remaining`), `output_files`.

### 4. Internal Module Interfaces (`src/qr_scrabber/`)
- `gs1.py` ↔ `harvesters/*` & `reconcile.py`:
  - `verify_gs1_check_digit(digits: str) -> bool`
  - `split_concatenated_codes(raw: str) -> list[str]` (splits 26/39-digit strings into 13-digit chunks if and only if each 13-digit chunk passes `verify_gs1_check_digit`; otherwise returns `[raw]`)
  - `normalize_barcode(raw: str) -> NormalizedBarcode`
- `qr.py` ↔ `exporter.py`, `server.py` & `cli.py`:
  - `build_qr_metadata(gtin14: str, primary_code: str, barcode_type: str, brand_name: str, product_name: str, qr_svg_path: str | None = None, image_url: str | None = None) -> QRMetadata`
  - `render_qr_code(payload: str, out_path: Path, fmt: Literal["svg", "png"] = "svg") -> Path`
  - `decode_qr_file(image_or_svg_path: Path) -> list[str]`
- `brands/registry.py` & `brands/matcher.py` ↔ `harvesters/*` & `reconcile.py`:
  - `BrandRegistry.from_entries(entries: list[BrandSeedEntry]) -> BrandRegistry`
  - `BrandMatcher(registry: BrandRegistry).match_obf_record(...) -> BrandMatch | QuarantineCandidate | None`
  - `BrandMatcher(registry: BrandRegistry).match_upc_item(...) -> BrandMatch | QuarantineCandidate | None`
- `harvesters/images.py` ↔ `exporter.py` & `cli.py`:
  - `ProductImageDownloader(cache_dir: Path, concurrency: int = 12, max_dim: int = 320).download_for_products(products: list[ProductRecord], output_dir: Path, max_images: int = 0) -> ImageDownloadStats`
- `server.py` & `web/*` ↔ `exporter.py` & `cli.py`:
  - `export_web_assets(output_dir: Path) -> list[Path]` (copies `src/qr_scrabber/web/{index.html,index.css,app.js}` atomically to `output_dir`)
  - `run_server(output_dir: Path, host: str = "127.0.0.1", port: int = 8765) -> None`

---

## Code Layout

```text
/home/antigravity/ShadiSairafi/Web-Qr-Scrabber/
├── .gitignore                        # Ignores .agents/, .venv/, __pycache__/, .pytest_cache/, .ruff_cache/, data/raw_dumps/, data/cache/
├── pyproject.toml                    # Package config + dependencies + `qr-scrabber` console_script
├── README.md                         # Architecture, setup, CLI guide, Web UI guide, schema docs, Cloudflare constraint note
├── PROJECT.md                        # Living project architecture & milestone source of truth
├── TEST_INFRA.md                     # Created by E2E Testing Orchestrator
├── TEST_READY.md                     # Published by E2E Testing Orchestrator upon E2E suite completion
├── data/
│   ├── brands_seed.json              # Authoritative 163 Glow & Glare brands + aliases + Risk Tiers
│   ├── upcitemdb_cached_responses.json # Committed deterministic cache of real UPCitemdb responses
│   ├── cache/                        # Gitignored local HTTP/image cache
│   └── raw_dumps/                    # Gitignored bulk .gz downloads (openbeautyfacts / openproductsfacts)
├── src/
│   └── qr_scrabber/
│       ├── __init__.py
│       ├── __main__.py
│       ├── models.py                 # Owned by M1 (incl. F13/F14 fields)
│       ├── gs1.py                    # Owned by M1
│       ├── qr.py                     # Owned by M1
│       ├── brands/
│       │   ├── __init__.py           # Owned by M2
│       │   ├── extractor.py          # Owned by M2
│       │   ├── registry.py           # Owned by M2
│       │   └── matcher.py            # Owned by M2
│       ├── harvesters/
│       │   ├── __init__.py           # Owned by M3
│       │   ├── cache.py              # Owned by M3
│       │   ├── openbeautyfacts.py    # Owned by M3
│       │   ├── upcitemdb.py          # Owned by M3
│       │   ├── images.py             # Owned by M3
│       │   └── barcodelookup_probe.py# Owned by M3
│       ├── reconcile.py              # Owned by M4
│       ├── exporter.py               # Owned by M4 (extended in M5 to auto-export web bundle)
│       ├── cli.py                    # Owned by M4 (extended in M5 with `serve`, `build-web`, `verify --check-web`)
│       ├── server.py                 # Owned by M5 (multi-threaded HTTP server + `/api/health` + `/api/qr/<gtin14>.svg`)
│       └── web/                      # Owned by M5 (Vanilla HTML5 + HSL CSS + ES JS single-search-bar app)
│           ├── index.html
│           ├── index.css
│           └── app.js
├── output/                           # Generated production JSON dataset, product pictures, QR SVGs & Web App (Owned by M4 & M5)
│   ├── index.html
│   ├── index.css
│   ├── app.js
│   ├── summary.json
│   ├── brands_manifest.json
│   ├── comparison_report.json
│   ├── quarantine.json
│   ├── all_products.json
│   ├── brands/                       # 163 per-brand JSON files
│   ├── images/                       # Downloaded & Pillow-verified local product thumbnails (<brand_slug>/<gtin14>.jpg)
│   └── qr/                           # Rendered ISO 18004 QR SVG files (<brand_slug>/<gtin14>.svg)
└── tests/
    ├── unit/                         # Owned by Implementation Track (M1–M5)
    │   ├── test_m1_gs1_qr_env.py
    │   ├── test_m2_brands.py
    │   ├── test_m3_harvesters.py
    │   ├── test_m4_reconcile_cli.py
    │   └── test_m5_web_ui.py
    └── e2e/                          # Owned EXCLUSIVELY by E2E Testing Track (Tiers 1–4) + Tier 5
```
