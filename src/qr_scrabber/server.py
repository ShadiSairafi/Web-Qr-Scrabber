"""Multi-threaded static + QR/Catalog API HTTP server for the Glow & Glare lookup web app."""

from __future__ import annotations

import gzip
import io
import json
import mimetypes
import re
import socket
import threading
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import segno

from qr_scrabber.gs1 import normalize_barcode, verify_gs1_check_digit
from qr_scrabber.live_lookup import LiveBarcodeLookupService, _extract_digits

_API_QR_RE = re.compile(r"^/api/qr/(\d{14})\.svg$")
_COMPRESSIBLE_EXTS = frozenset({".json", ".html", ".css", ".js", ".svg"})


def _slim_product_for_ui(p: dict[str, Any]) -> dict[str, Any]:
    """Project a full ProductRecord dict into a compact UI payload preserving all UI fields."""
    brand = p.get("brand") or {}
    product = p.get("product") or {}
    barcode = p.get("barcode") or {}
    qr = p.get("qr") or {}
    verif = p.get("verification") or {}
    return {
        "gtin14": p.get("gtin14"),
        "product_name": p.get("product_name"),
        "image_url": p.get("image_url"),
        "local_image_path": p.get("local_image_path"),
        "brand": {
            "slug": brand.get("slug"),
            "display_name": brand.get("display_name"),
        },
        "product": {
            "name": product.get("name"),
            "alternate_names": (product.get("alternate_names") or [])[:4],
            "quantity": product.get("quantity"),
            "categories": (product.get("categories") or [])[:4],
            "primary_image_url": product.get("primary_image_url"),
            "thumbnail_url": product.get("thumbnail_url"),
            "local_image_path": product.get("local_image_path"),
        },
        "barcode": {
            "gtin14": barcode.get("gtin14"),
            "ean13": barcode.get("ean13"),
            "upc_a": barcode.get("upc_a"),
            "ean8": barcode.get("ean8"),
            "primary_code": barcode.get("primary_code"),
            "barcode_type": barcode.get("barcode_type"),
            "raw_codes": (barcode.get("raw_codes") or [])[:4],
        },
        "qr": {
            "gs1_digital_link": qr.get("gs1_digital_link"),
            "qr_svg_path": qr.get("qr_svg_path"),
        },
        "verification": {
            "status": verif.get("status"),
            "sources": verif.get("sources") or [],
        },
    }


class _CatalogCache:
    """Thread-safe mtime-validated in-memory cache of compact UI catalog records."""

    def __init__(self, output_dir: Path) -> None:
        self._output_dir = output_dir
        self._lock = threading.Lock()
        self._mtime_ns: int = -1
        self._items: list[dict[str, Any]] = []
        self._by_gtin14: dict[str, dict[str, Any]] = {}

    def invalidate(self) -> None:
        with self._lock:
            self._mtime_ns = -1

    def get_items(self) -> list[dict[str, Any]]:
        all_path = self._output_dir / "all_products.json"
        if not all_path.is_file():
            return []
        try:
            stat = all_path.stat()
        except OSError:
            return []
        with self._lock:
            if stat.st_mtime_ns != self._mtime_ns:
                raw = json.loads(all_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    self._items = [_slim_product_for_ui(p) for p in raw if isinstance(p, dict)]
                else:
                    self._items = []
                self._by_gtin14 = {
                    str(item["gtin14"]): item for item in self._items if item.get("gtin14")
                }
                self._mtime_ns = stat.st_mtime_ns
            return self._items

    def find_by_gtin14(self, gtin14: str) -> dict[str, Any] | None:
        self.get_items()
        with self._lock:
            return self._by_gtin14.get(gtin14)


def _load_total_products(output_dir: Path, cache: _CatalogCache | None = None) -> int:
    summary_path = output_dir / "summary.json"
    if summary_path.is_file():
        try:
            data = json.loads(summary_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(
                data.get("total_unique_verified_products"), int
            ):
                return int(data["total_unique_verified_products"])
        except (OSError, json.JSONDecodeError):
            pass

    if cache is not None:
        return len(cache.get_items())

    all_path = output_dir / "all_products.json"
    if all_path.is_file():
        try:
            items = json.loads(all_path.read_text(encoding="utf-8"))
            if isinstance(items, list):
                return len(items)
        except (OSError, json.JSONDecodeError):
            pass
    return 0


def _render_qr_svg_bytes(gtin14: str) -> bytes:
    payload = f"https://id.gs1.org/01/{gtin14}"
    qr = segno.make_qr(payload, error="m")
    buf = io.BytesIO()
    qr.save(
        buf,
        kind="svg",
        scale=6,
        border=4,
        dark="#000000",
        light="#ffffff",
        xmldecl=True,
    )
    return buf.getvalue()


class CatalogRequestHandler(SimpleHTTPRequestHandler):
    """Serves static catalog assets with gzip plus `/api/{health,catalog,lookup,qr}`."""

    protocol_version = "HTTP/1.1"

    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".svg": "image/svg+xml; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".webp": "image/webp",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
    }

    def __init__(
        self,
        *args: Any,
        directory: str | None = None,
        catalog_cache: _CatalogCache | None = None,
        live_lookup: LiveBarcodeLookupService | None = None,
        **kwargs: Any,
    ) -> None:
        self._output_dir = Path(directory or ".").resolve()
        self._catalog_cache = catalog_cache
        self._live_lookup = live_lookup
        super().__init__(*args, directory=str(self._output_dir), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Keep stdout/stderr clean during automated tests and daemon runs
        return

    def _accepts_gzip(self) -> bool:
        ae = self.headers.get("Accept-Encoding", "")
        return "gzip" in ae.lower()

    def _send_bytes(
        self,
        body: bytes,
        *,
        status: int = HTTPStatus.OK,
        content_type: str = "application/octet-stream",
        cache_control: str = "no-cache",
        head_only: bool = False,
        allow_gzip: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        use_gzip = allow_gzip and len(body) >= 512 and self._accepts_gzip()
        payload = gzip.compress(body, compresslevel=6) if use_gzip else body

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-QR-Server", "1")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.end_headers()
        if not head_only:
            self.wfile.write(payload)

    def _send_json(
        self,
        payload: dict[str, Any],
        *,
        status: int = HTTPStatus.OK,
        cache_control: str = "no-store",
        head_only: bool = False,
        allow_gzip: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self._send_bytes(
            body,
            status=status,
            content_type="application/json; charset=utf-8",
            cache_control=cache_control,
            head_only=head_only,
            allow_gzip=allow_gzip,
            extra_headers=extra_headers,
        )

    def _handle_api(self, *, head_only: bool = False) -> bool:
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)

        if path == "/api/health":
            self._send_json(
                {
                    "status": "ok",
                    "total_products": _load_total_products(self._output_dir, self._catalog_cache),
                },
                status=HTTPStatus.OK,
                cache_control="no-store",
                head_only=head_only,
                allow_gzip=False,
                extra_headers={"X-Live-Lookup": "1" if self._live_lookup is not None else "0"},
            )
            return True

        if path == "/api/catalog":
            qs = parse_qs(parsed.query)
            try:
                offset = max(0, int((qs.get("offset") or ["0"])[0]))
                limit = int((qs.get("limit") or ["700"])[0])
                limit = max(1, min(limit, 1000))
            except ValueError:
                self._send_json(
                    {"error": "Invalid offset or limit parameter."},
                    status=HTTPStatus.BAD_REQUEST,
                    head_only=head_only,
                )
                return True

            items = self._catalog_cache.get_items() if self._catalog_cache is not None else []
            page_items = items[offset : offset + limit]
            self._send_json(
                {
                    "total": len(items),
                    "offset": offset,
                    "limit": limit,
                    "items": page_items,
                },
                status=HTTPStatus.OK,
                cache_control="no-cache",
                head_only=head_only,
                allow_gzip=True,
            )
            return True

        if path == "/api/lookup":
            qs = parse_qs(parsed.query)
            raw_code = (qs.get("code") or [""])[0].strip()
            if not raw_code:
                self._send_json(
                    {"found": False, "error": "Missing required `code` query parameter."},
                    status=HTTPStatus.BAD_REQUEST,
                    head_only=head_only,
                )
                return True

            digits = _extract_digits(raw_code)
            norm = normalize_barcode(digits)
            if not norm.is_valid:
                reason = norm.quarantine_reason or "invalid_format"
                self._send_json(
                    {
                        "found": False,
                        "code": digits or raw_code,
                        "error": f"Invalid GS1 barcode ({reason}).",
                    },
                    status=HTTPStatus.BAD_REQUEST,
                    head_only=head_only,
                )
                return True

            if self._catalog_cache is not None:
                existing = self._catalog_cache.find_by_gtin14(norm.gtin14)
                if existing is not None:
                    self._send_json(
                        {
                            "found": True,
                            "added": False,
                            "source": "local_catalog",
                            "product": existing,
                        },
                        status=HTTPStatus.OK,
                        head_only=head_only,
                    )
                    return True

            if self._live_lookup is None:
                self._send_json(
                    {
                        "found": False,
                        "code": norm.primary_code,
                        "gtin14": norm.gtin14,
                        "message": "Live barcode lookup is disabled on this server.",
                    },
                    status=HTTPStatus.OK,
                    head_only=head_only,
                )
                return True

            result = self._live_lookup.lookup_and_persist(raw_code)
            if result.get("found") and isinstance(result.get("product"), dict):
                if self._catalog_cache is not None:
                    self._catalog_cache.invalidate()
                self._send_json(
                    {
                        "found": True,
                        "added": bool(result.get("added", True)),
                        "source": result.get("source", "live_lookup"),
                        "product": _slim_product_for_ui(result["product"]),
                    },
                    status=HTTPStatus.OK,
                    head_only=head_only,
                )
                return True

            self._send_json(result, status=HTTPStatus.OK, head_only=head_only)
            return True

        qr_match = _API_QR_RE.match(path)
        if qr_match:
            gtin14 = qr_match.group(1)
            if not verify_gs1_check_digit(gtin14):
                self._send_json(
                    {"error": f"Invalid GS1 GTIN-14 check digit: {gtin14}"},
                    status=HTTPStatus.BAD_REQUEST,
                    head_only=head_only,
                )
                return True

            svg_bytes = _render_qr_svg_bytes(gtin14)
            self._send_bytes(
                svg_bytes,
                status=HTTPStatus.OK,
                content_type="image/svg+xml; charset=utf-8",
                cache_control="public, max-age=86400",
                head_only=head_only,
                allow_gzip=True,
            )
            return True

        return False

    def _serve_static(self, *, head_only: bool = False) -> None:
        fs_path = Path(self.translate_path(self.path))
        if fs_path.is_dir():
            index_file = fs_path / "index.html"
            if index_file.is_file():
                fs_path = index_file
            else:
                self._send_bytes(
                    b"Not Found",
                    status=HTTPStatus.NOT_FOUND,
                    content_type="text/plain; charset=utf-8",
                    cache_control="no-store",
                    head_only=head_only,
                    allow_gzip=False,
                )
                return

        if not fs_path.is_file():
            self._send_bytes(
                b"Not Found",
                status=HTTPStatus.NOT_FOUND,
                content_type="text/plain; charset=utf-8",
                cache_control="no-store",
                head_only=head_only,
                allow_gzip=False,
            )
            return

        # Guard against path traversal outside output_dir
        try:
            fs_path.resolve().relative_to(self._output_dir)
        except ValueError:
            self._send_bytes(
                b"Forbidden",
                status=HTTPStatus.FORBIDDEN,
                content_type="text/plain; charset=utf-8",
                cache_control="no-store",
                head_only=head_only,
                allow_gzip=False,
            )
            return

        raw = fs_path.read_bytes()
        ctype = self.guess_type(str(fs_path))
        ext = fs_path.suffix.lower()
        cache_ctrl = (
            "public, max-age=86400"
            if ext in (".jpg", ".jpeg", ".png", ".webp", ".svg")
            else "no-cache"
        )
        self._send_bytes(
            raw,
            status=HTTPStatus.OK,
            content_type=ctype,
            cache_control=cache_ctrl,
            head_only=head_only,
            allow_gzip=(ext in _COMPRESSIBLE_EXTS),
        )

    def do_GET(self) -> None:  # noqa: N802
        if self._handle_api(head_only=False):
            return
        self._serve_static(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802
        if self._handle_api(head_only=True):
            return
        self._serve_static(head_only=True)

    def guess_type(self, path: str) -> str:
        ext = Path(path).suffix.lower()
        if ext in self.extensions_map:
            return self.extensions_map[ext]
        guessed, _ = mimetypes.guess_type(path)
        return guessed or "application/octet-stream"


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def create_server(
    output_dir: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    live_lookup: LiveBarcodeLookupService | None = None,
    enable_live_lookup: bool = True,
) -> ReusableThreadingHTTPServer:
    """Create a configured multi-threaded HTTP server bound to `(host, port)`."""
    root = Path(output_dir).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Output directory does not exist: {root}")

    cache = _CatalogCache(root)
    lookup_service = (
        live_lookup
        if live_lookup is not None
        else (LiveBarcodeLookupService(output_dir=root) if enable_live_lookup else None)
    )

    def _handler(*args: Any, **kwargs: Any) -> CatalogRequestHandler:
        return CatalogRequestHandler(
            *args,
            directory=str(root),
            catalog_cache=cache,
            live_lookup=lookup_service,
            **kwargs,
        )

    return ReusableThreadingHTTPServer((host, port), _handler)


def detect_lan_ips() -> list[str]:
    """Return non-loopback IPv4 addresses reachable on the local network (LAN/Wi-Fi)."""
    ips: list[str] = []

    # 1. Primary outbound interface IP (no packets sent for UDP connect)
    for probe_ip in ("10.255.255.255", "192.168.1.1", "8.8.8.8"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((probe_ip, 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
        except OSError:
            pass
        finally:
            sock.close()

    # 2. Fallback via hostname resolution
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = str(info[4][0])
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass

    return ips


def run_server(
    output_dir: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    """Run the catalog HTTP server until interrupted, printing local & LAN URLs immediately."""
    server = create_server(output_dir=output_dir, host=host, port=port)
    bound_host, bound_port = server.server_address[:2]
    if bound_host in ("0.0.0.0", "::", ""):
        print(f"Local:   http://127.0.0.1:{bound_port}/", flush=True)
        lan_ips = detect_lan_ips()
        if lan_ips:
            for ip in lan_ips:
                print(f"Network: http://{ip}:{bound_port}/  (share on same Wi-Fi/LAN)", flush=True)
        else:
            print(f"Listening on http://{bound_host}:{bound_port}/", flush=True)
    else:
        print(f"Listening on http://{bound_host}:{bound_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


__all__ = [
    "CatalogRequestHandler",
    "ReusableThreadingHTTPServer",
    "create_server",
    "detect_lan_ips",
    "run_server",
]
