"""Dual 1D + 2D GS1 Digital Link ISO 18004 QR metadata, rendering, and optical decode engine."""

from __future__ import annotations

import codecs
import io
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Final, Literal

import segno
from PIL import Image, ImageOps

from qr_scrabber.gs1 import verify_gs1_check_digit
from qr_scrabber.models import QRInfo, QRMetadata, QRPayloadData

_DEFAULT_RUNTIME_SCRATCH = Path(
    "/home/antigravity/.gemini/jetski/scratch/teamwork_tmp/qr_scrabber_runtime"
)


def build_qr_metadata(
    gtin14: str,
    primary_code: str,
    barcode_type: str,
    brand_name: str,
    product_name: str,
    qr_svg_path: str | None = None,
    image_url: str | None = None,
    qr_png_path: str | None = None,
) -> QRMetadata:
    """Build GS1 Digital Link + ISO 18004 QR metadata for a validated GTIN-14."""
    if not (
        isinstance(gtin14, str)
        and len(gtin14) == 14
        and gtin14.isascii()
        and gtin14.isdigit()
        and verify_gs1_check_digit(gtin14)
    ):
        raise ValueError(f"Invalid GTIN-14 for GS1 Digital Link QR metadata: {gtin14!r}")
    if not isinstance(primary_code, str) or not primary_code.strip():
        raise ValueError("primary_code must be a non-empty string for QR metadata.")
    if not isinstance(barcode_type, str) or not barcode_type.strip():
        raise ValueError("barcode_type must be a non-empty string for QR metadata.")
    if not isinstance(brand_name, str) or not brand_name.strip():
        raise ValueError("brand_name must be a non-empty string for QR metadata.")
    if not isinstance(product_name, str) or not product_name.strip():
        raise ValueError("product_name must be a non-empty string for QR metadata.")
    if image_url is not None and not isinstance(image_url, str):
        raise ValueError("image_url must be a string or None for QR metadata.")

    clean_image_url = image_url.strip() or None if isinstance(image_url, str) else None
    gs1_link = f"https://id.gs1.org/01/{gtin14}"
    return QRInfo(
        gs1_digital_link=gs1_link,
        qr_payload_text=gs1_link,
        qr_payload_data=QRPayloadData(
            schema="glow-glare-qr/v1",
            gtin14=gtin14,
            code=primary_code.strip(),
            type=barcode_type.strip(),
            brand=brand_name.strip(),
            name=product_name.strip(),
            image_url=clean_image_url,
        ),
        qr_standard="GS1_DIGITAL_LINK_ISO_18004",
        qr_svg_path=qr_svg_path,
        qr_png_path=qr_png_path,
    )


def render_qr_code(
    payload: str,
    out_path: Path,
    fmt: Literal["svg", "png"] = "svg",
    scale: int = 6,
) -> Path:
    """Atomically render a standard ISO/IEC 18004 QR symbol (`micro=False`) to SVG or PNG."""
    if not isinstance(payload, str) or not payload.strip():
        raise ValueError("QR payload must be a non-empty string.")
    if fmt not in ("svg", "png"):
        raise ValueError(f"Unsupported QR format {fmt!r}; expected 'svg' or 'png'.")
    if scale < 1:
        raise ValueError(f"QR scale must be >= 1; got {scale}.")

    target_path = Path(out_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    use_utf8_eci = not payload.isascii()
    qr = segno.make_qr(
        payload,
        error="m",
        encoding="utf-8" if use_utf8_eci else None,
        eci=use_utf8_eci,
    )
    buf = io.BytesIO()
    if fmt == "svg":
        qr.save(
            buf,
            kind="svg",
            scale=scale,
            border=4,
            dark="#000000",
            light="#ffffff",
            xmldecl=True,
        )
    else:
        qr.save(
            buf,
            kind="png",
            scale=scale,
            border=4,
            dark="#000000",
            light="#ffffff",
        )
    data = buf.getvalue()

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target_path.name}.",
        suffix=".tmp",
        dir=str(target_path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    return target_path


def _safe_scratch_env() -> tuple[Path, dict[str, str], bool]:
    """Return a guaranteed non-`/tmp` scratch directory and subprocess environment."""
    raw_tmp = os.environ.get("TMPDIR", "").strip()
    resolved = os.path.realpath(raw_tmp) if raw_tmp else ""
    if resolved and resolved != "/tmp" and not resolved.startswith("/tmp/"):
        scratch_base = Path(resolved)
        created_fallback = False
    else:
        scratch_base = _DEFAULT_RUNTIME_SCRATCH
        created_fallback = not scratch_base.exists()

    scratch_base.mkdir(parents=True, exist_ok=True)
    safe_env = {
        **os.environ,
        "TMPDIR": str(scratch_base),
        "MAGICK_TEMPORARY_PATH": str(scratch_base),
        "MAGICK_TMPDIR": str(scratch_base),
    }
    return scratch_base, safe_env, created_fallback


def _rasterize_svg(svg_bytes: bytes, scratch_base: Path, safe_env: dict[str, str]) -> Image.Image:
    """Rasterize SVG bytes to an RGB Pillow image without touching `/tmp`."""
    rsvg_bin = shutil.which("rsvg-convert") or "/usr/bin/rsvg-convert"
    if Path(rsvg_bin).is_file():
        proc = subprocess.run(
            [rsvg_bin, "--background-color=white", "--zoom=4", "--format=png"],
            input=svg_bytes,
            capture_output=True,
            check=True,
            env=safe_env,
        )
        with Image.open(io.BytesIO(proc.stdout)) as im:
            return im.convert("RGB")

    convert_bin = shutil.which("convert") or "/usr/bin/convert"
    with tempfile.TemporaryDirectory(dir=str(scratch_base)) as td:
        svg_in = Path(td) / "input.svg"
        png_out = Path(td) / "output.png"
        svg_in.write_bytes(svg_bytes)
        subprocess.run(
            [convert_bin, "-background", "white", "-flatten", str(svg_in), str(png_out)],
            capture_output=True,
            check=True,
            env=safe_env,
        )
        with Image.open(png_out) as im:
            return im.convert("RGB")


_ZBAR_SCANNED_RE: Final[re.Pattern[bytes]] = re.compile(rb"scanned (\d+) barcode symbols")


def _upscale_for_zbar(img: Image.Image) -> Image.Image:
    return ImageOps.expand(
        img.resize((img.width * 2, img.height * 2), Image.Resampling.NEAREST),
        border=16,
        fill=(255, 255, 255),
    )


def _parse_zbarimg_raw(stdout: bytes, stderr: bytes) -> list[str]:
    if not stdout:
        return []
    m = _ZBAR_SCANNED_RE.search(stderr)
    symbol_count = int(m.group(1)) if m else 1
    if symbol_count == 0:
        return []
    trimmed = stdout[:-1] if stdout.endswith(b"\n") else stdout
    if symbol_count == 1:
        return [trimmed.decode("utf-8")]
    return [part.decode("utf-8") for part in trimmed.split(b"\n")]


def _decode_with_pyzbar(img: Image.Image) -> list[str]:
    from pyzbar.pyzbar import ZBarSymbol
    from pyzbar.pyzbar import decode as zbar_decode

    decoded = zbar_decode(img, symbols=[ZBarSymbol.QRCODE])
    if decoded:
        return [item.data.decode("utf-8") for item in decoded]

    upscaled = _upscale_for_zbar(img)
    decoded_retry = zbar_decode(upscaled, symbols=[ZBarSymbol.QRCODE])
    return [item.data.decode("utf-8") for item in decoded_retry]


def _decode_with_zbarimg(
    img: Image.Image,
    scratch_base: Path,
    safe_env: dict[str, str],
) -> list[str]:
    zbarimg_bin = shutil.which("zbarimg") or "/usr/bin/zbarimg"
    if not Path(zbarimg_bin).is_file():
        return []
    with tempfile.TemporaryDirectory(dir=str(scratch_base)) as td:
        probe_png = Path(td) / "probe.png"
        img.save(probe_png, format="PNG")
        proc = subprocess.run(
            [zbarimg_bin, "--raw", "-Sdisable", "-Sqrcode.enable", str(probe_png)],
            capture_output=True,
            check=False,
            env=safe_env,
        )
        if proc.returncode == 0:
            return _parse_zbarimg_raw(proc.stdout, proc.stderr)
        if proc.returncode == 4:
            retry_png = Path(td) / "probe_upscaled.png"
            _upscale_for_zbar(img).save(retry_png, format="PNG")
            proc_retry = subprocess.run(
                [zbarimg_bin, "--raw", "-Sdisable", "-Sqrcode.enable", str(retry_png)],
                capture_output=True,
                check=False,
                env=safe_env,
            )
            if proc_retry.returncode == 0:
                return _parse_zbarimg_raw(proc_retry.stdout, proc_retry.stderr)
            if proc_retry.returncode == 4:
                return []
            proc_retry.check_returncode()
            return []
        proc.check_returncode()
        return []


_RASTER_MAGIC_PREFIXES: Final[tuple[bytes, ...]] = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"BM",
    b"II*\x00",
    b"MM\x00*",
)


def _is_svg_file(path: Path, raw_bytes: bytes | None = None) -> bool:
    """Detect SVG XML files reliably across `.svg` and extensionless/misnamed paths."""
    if raw_bytes is None:
        with Path(path).open("rb") as fh:
            sample = fh.read(1024)
    else:
        sample = raw_bytes[:1024]

    if sample.startswith(_RASTER_MAGIC_PREFIXES) or (
        len(sample) >= 12 and sample[:4] == b"RIFF" and sample[8:12] == b"WEBP"
    ):
        return False

    if Path(path).suffix.lower() == ".svg":
        return True

    if sample.startswith(b"\xef\xbb\xbf"):
        sample = sample[3:]
    sample = sample.lstrip(b" \t\r\n\f\v")
    if not sample or b"\x00" in sample:
        return False

    try:
        header_text = codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
    except UnicodeDecodeError:
        return False

    lowered = header_text.lstrip().lower()
    if not lowered.startswith("<"):
        return False
    return "<svg" in lowered or "<?xml" in lowered or "<!doctype svg" in lowered


def decode_qr_file(image_or_svg_path: Path) -> list[str]:
    """Optically decode an SVG or raster QR file into its encoded UTF-8 payload strings."""
    path = Path(image_or_svg_path)
    if not path.is_file():
        raise FileNotFoundError(f"QR file not found: {path}")

    scratch_base, safe_env, created_fallback = _safe_scratch_env()
    try:
        raw_bytes = path.read_bytes()
        if _is_svg_file(path, raw_bytes):
            img = _rasterize_svg(raw_bytes, scratch_base, safe_env)
        else:
            with Image.open(io.BytesIO(raw_bytes)) as im:
                img = im.convert("RGB")

        try:
            results = _decode_with_pyzbar(img)
        except (ImportError, OSError):
            results = []

        if not results:
            results = _decode_with_zbarimg(img, scratch_base, safe_env)
        return results
    finally:
        if created_fallback:
            shutil.rmtree(scratch_base, ignore_errors=True)


__all__ = [
    "QRInfo",
    "QRMetadata",
    "QRPayloadData",
    "build_qr_metadata",
    "decode_qr_file",
    "render_qr_code",
]
