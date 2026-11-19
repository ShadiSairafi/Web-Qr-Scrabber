"""Multi-threaded static + QR API HTTP server for the Glow & Glare lookup web app."""

from __future__ import annotations

import io
import json
import mimetypes
import re
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import segno

from qr_scrabber.gs1 import verify_gs1_check_digit

_API_QR_RE = re.compile(r"^/api/qr/(\d{14})\.svg$")


def _load_total_products(output_dir: Path) -> int:
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
    """Serves static catalog files plus `/api/health` and dynamic `/api/qr/<gtin14>.svg`."""

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

    def __init__(self, *args: Any, directory: str | None = None, **kwargs: Any) -> None:
        self._output_dir = Path(directory or ".").resolve()
        super().__init__(*args, directory=str(self._output_dir), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Keep stdout/stderr clean during automated tests and daemon runs
        return

    def _send_bytes(
        self,
        body: bytes,
        *,
        status: int = HTTPStatus.OK,
        content_type: str = "application/octet-stream",
        cache_control: str = "no-cache",
        head_only: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _handle_api(self, *, head_only: bool = False) -> bool:
        path = unquote(urlsplit(self.path).path)

        if path == "/api/health":
            payload = {
                "status": "ok",
                "total_products": _load_total_products(self._output_dir),
            }
            encoded = json.dumps(payload).encode("utf-8")
            self._send_bytes(
                encoded,
                status=HTTPStatus.OK,
                content_type="application/json; charset=utf-8",
                cache_control="no-store",
                head_only=head_only,
            )
            return True

        qr_match = _API_QR_RE.match(path)
        if qr_match:
            gtin14 = qr_match.group(1)
            if not verify_gs1_check_digit(gtin14):
                err = json.dumps({"error": f"Invalid GS1 GTIN-14 check digit: {gtin14}"}).encode(
                    "utf-8"
                )
                self._send_bytes(
                    err,
                    status=HTTPStatus.BAD_REQUEST,
                    content_type="application/json; charset=utf-8",
                    cache_control="no-store",
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
            )
            return True

        return False

    def do_GET(self) -> None:  # noqa: N802
        if self._handle_api(head_only=False):
            return
        super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802
        if self._handle_api(head_only=True):
            return
        super().do_HEAD()

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
) -> ReusableThreadingHTTPServer:
    """Create a configured multi-threaded HTTP server bound to `(host, port)`."""
    root = Path(output_dir).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Output directory does not exist: {root}")

    def _handler(*args: Any, **kwargs: Any) -> CatalogRequestHandler:
        return CatalogRequestHandler(*args, directory=str(root), **kwargs)

    return ReusableThreadingHTTPServer((host, port), _handler)


def run_server(
    output_dir: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    """Run the catalog HTTP server until interrupted, printing the bound URL immediately."""
    server = create_server(output_dir=output_dir, host=host, port=port)
    bound_host, bound_port = server.server_address[:2]
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
    "run_server",
]
