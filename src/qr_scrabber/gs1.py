"""GS1 Modulo-10 validation, multi-code splitting, canonicalization, and quarantine engine."""

from __future__ import annotations

import re
from typing import Final

from qr_scrabber.models import BarcodeType, NormalizedBarcode, QuarantineReason

VALID_GS1_LENGTHS: Final[frozenset[int]] = frozenset({8, 12, 13, 14})

_INTRA_DIGIT_SEP_RE: Final[re.Pattern[str]] = re.compile(r"(?<=[0-9])(?: +|-)(?=[0-9])")
_INTRA_DIGIT_HYPHEN_RE: Final[re.Pattern[str]] = re.compile(r"(?<=[0-9])-(?=[0-9])")
_COARSE_DELIM_RE: Final[re.Pattern[str]] = re.compile(r"[,;/|\r\n\t]+")


def _is_ascii_digits(value: str) -> bool:
    return isinstance(value, str) and value.isascii() and value.isdigit()


def _compute_gs1_check_digit(body: str) -> str:
    total = sum(int(ch) * (3 if idx % 2 == 0 else 1) for idx, ch in enumerate(reversed(body)))
    return str((10 - (total % 10)) % 10)


def _build_sequential_placeholders() -> frozenset[str]:
    placeholders: set[str] = set()
    tapes = ("0123456789" * 3, "9876543210" * 3)
    for tape in tapes:
        for start in range(10):
            for length in range(8, 15):
                raw_seq = tape[start : start + length]
                assert len(raw_seq) == length
                body = tape[start : start + length - 1]
                checked = body + _compute_gs1_check_digit(body)
                assert len(checked) == length
                for cand in (raw_seq, checked):
                    if len(cand) in VALID_GS1_LENGTHS:
                        placeholders.add(cand)
                    for target_len in (8, 12, 13, 14):
                        if len(cand) <= target_len:
                            placeholders.add(cand.zfill(target_len))
    return frozenset(placeholders)


_SEQUENTIAL_PLACEHOLDERS: Final[frozenset[str]] = _build_sequential_placeholders()


def verify_gs1_check_digit(digits: str) -> bool:
    """Verify GS1 Modulo-10 check digit across strict ASCII lengths {8, 12, 13, 14}.

    Rejects non-ASCII digits, invalid lengths, and uniform repeated-digit strings.
    """
    if not _is_ascii_digits(digits) or len(digits) not in VALID_GS1_LENGTHS:
        return False
    if len(set(digits)) == 1:
        return False
    return digits[-1] == _compute_gs1_check_digit(digits[:-1])


def is_placeholder_pattern(digits: str) -> bool:
    """Detect uniform, zero-padded uniform, single-digit, or sequential test barcodes."""
    if not _is_ascii_digits(digits) or len(digits) not in VALID_GS1_LENGTHS:
        return False
    nz = digits.lstrip("0")
    if len(nz) <= 1:
        return True
    if len(set(digits)) == 1:
        return True
    if len(set(nz)) == 1:
        return True
    if digits in _SEQUENTIAL_PLACEHOLDERS or (
        len(nz) in VALID_GS1_LENGTHS and nz in _SEQUENTIAL_PLACEHOLDERS
    ):
        return True
    return False


def is_restricted_circulation(digits: str, gtin14: str) -> bool:
    """Detect GS1 restricted circulation / internal store / variable-measure prefixes."""
    if not _is_ascii_digits(gtin14) or len(gtin14) != 14:
        return False
    cleaned_digits = _INTRA_DIGIT_SEP_RE.sub("", digits.strip()) if isinstance(digits, str) else ""
    if (
        _is_ascii_digits(cleaned_digits)
        and len(cleaned_digits) in (8, 14)
        and gtin14.startswith("000000")
    ):
        return gtin14[6:].startswith(("0", "2"))
    if gtin14[0] == "9":
        return True
    underlying_13 = gtin14[1:]
    p2 = underlying_13[:2]
    return p2 in ("02", "04") or ("20" <= p2 <= "29") or underlying_13.startswith("0000")


def split_concatenated_codes(raw: str) -> list[str]:
    """Split delimiter-separated barcode lists and valid 13*k concatenated EAN-13 strings.

    Preserves space/hyphen-grouped single barcodes (compact length in {8, 12, 13, 14})
    and refuses to split 24-digit strings as 2*12 due to the 3*8 EAN-8 collision.
    """
    if not isinstance(raw, str) or not raw.strip():
        return []

    output: list[str] = []
    for raw_seg in _COARSE_DELIM_RE.split(raw.strip()):
        seg = raw_seg.strip()
        if not seg:
            continue
        compact = _INTRA_DIGIT_SEP_RE.sub("", seg)
        if _is_ascii_digits(compact) and len(compact) in VALID_GS1_LENGTHS:
            tokens = [seg]
        else:
            tokens = [tok for tok in seg.split(" ") if tok]

        for tok in tokens:
            tok_digits = _INTRA_DIGIT_HYPHEN_RE.sub("", tok)
            if _is_ascii_digits(tok_digits) and len(tok_digits) >= 26 and len(tok_digits) % 13 == 0:
                chunks = [tok_digits[i : i + 13] for i in range(0, len(tok_digits), 13)]
                if all(
                    verify_gs1_check_digit(chunk) and not is_placeholder_pattern(chunk)
                    for chunk in chunks
                ):
                    output.extend(chunks)
                else:
                    output.append(tok)
            else:
                output.append(tok)
    return output


def normalize_barcode(raw: str) -> NormalizedBarcode:
    """Validate and canonicalize a raw barcode string to an idempotent `NormalizedBarcode`."""
    stripped = raw.strip() if isinstance(raw, str) else ""
    cleaned = _INTRA_DIGIT_SEP_RE.sub("", stripped)

    if not _is_ascii_digits(cleaned) or len(cleaned) not in VALID_GS1_LENGTHS:
        return NormalizedBarcode(
            gtin14="",
            ean13=None,
            upc_a=None,
            ean8=None,
            primary_code=cleaned,
            barcode_type="INVALID",
            gs1_check_digit_valid=False,
            gs1_restricted_circulation=False,
            quarantine_reason="NON_NUMERIC_OR_INVALID_LENGTH",
        )

    if is_placeholder_pattern(cleaned):
        return NormalizedBarcode(
            gtin14=cleaned.zfill(14),
            ean13=None,
            upc_a=None,
            ean8=None,
            primary_code=cleaned,
            barcode_type="INVALID",
            gs1_check_digit_valid=False,
            gs1_restricted_circulation=False,
            quarantine_reason="PLACEHOLDER_PATTERN",
        )

    if not verify_gs1_check_digit(cleaned):
        return NormalizedBarcode(
            gtin14=cleaned.zfill(14),
            ean13=None,
            upc_a=None,
            ean8=None,
            primary_code=cleaned,
            barcode_type="INVALID",
            gs1_check_digit_valid=False,
            gs1_restricted_circulation=False,
            quarantine_reason="INVALID_CHECK_DIGIT",
        )

    gtin14 = cleaned.zfill(14)
    ean13: str | None
    upc_a: str | None
    ean8: str | None
    btype: BarcodeType

    if len(cleaned) in (8, 14) and gtin14.startswith("000000"):
        ean8 = gtin14[6:]
        upc_a = None
        ean13 = None
        primary_code = ean8
        btype = "EAN-8"
    elif gtin14.startswith("00") and not gtin14.startswith("00000"):
        upc_a = gtin14[2:]
        ean13 = gtin14[1:]
        ean8 = None
        primary_code = upc_a
        btype = "UPC-A"
    elif gtin14.startswith("0"):
        ean13 = gtin14[1:]
        upc_a = None
        ean8 = None
        primary_code = ean13
        btype = "EAN-13"
    else:
        ean13 = None
        upc_a = None
        ean8 = None
        primary_code = gtin14
        btype = "GTIN-14"

    restricted = is_restricted_circulation(cleaned, gtin14)
    q_reason: QuarantineReason | None = "RESTRICTED_CIRCULATION_PREFIX" if restricted else None

    return NormalizedBarcode(
        gtin14=gtin14,
        ean13=ean13,
        upc_a=upc_a,
        ean8=ean8,
        primary_code=primary_code,
        barcode_type=btype,
        gs1_check_digit_valid=True,
        gs1_restricted_circulation=restricted,
        quarantine_reason=q_reason,
    )


__all__ = [
    "VALID_GS1_LENGTHS",
    "is_placeholder_pattern",
    "is_restricted_circulation",
    "normalize_barcode",
    "split_concatenated_codes",
    "verify_gs1_check_digit",
]
