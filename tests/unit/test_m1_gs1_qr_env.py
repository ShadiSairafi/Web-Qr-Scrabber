"""Comprehensive Milestone 1 unit tests (`F1`, `F4`, `F5`): Env, Models, GS1 & QR Engine."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from qr_scrabber import __version__
from qr_scrabber.cli import build_parser
from qr_scrabber.cli import main as cli_main
from qr_scrabber.gs1 import (
    _SEQUENTIAL_PLACEHOLDERS,
    VALID_GS1_LENGTHS,
    _compute_gs1_check_digit,
    is_placeholder_pattern,
    is_restricted_circulation,
    normalize_barcode,
    split_concatenated_codes,
    verify_gs1_check_digit,
)
from qr_scrabber.models import (
    ALL_QUARANTINE_REASONS,
    BarcodeInfo,
    BrandConflict,
    BrandFilePayload,
    BrandInfo,
    BrandManifestEntry,
    BrandMatch,
    BrandSeedEntry,
    BrandsManifest,
    ComparisonReport,
    CrossSourceMatch,
    ImageDownloadStats,
    ParentSubbrandResolution,
    ProductDetails,
    ProductRecord,
    ProvenanceInfo,
    QRInfo,
    QRMetadata,
    QRPayloadData,
    QuarantineCandidate,
    QuarantineRecord,
    QuarantineReport,
    SourceBreakdown,
    SourceStatusEntry,
    SourceStatusMap,
    SummaryReport,
    UpcitemdbQuota,
    VerificationInfo,
    default_quarantine_counts,
)
from qr_scrabber.qr import (
    _decode_with_zbarimg,
    _is_svg_file,
    _safe_scratch_env,
    build_qr_metadata,
    decode_qr_file,
    render_qr_code,
)

REPO_ROOT = Path("/home/antigravity/ShadiSairafi/Web-Qr-Scrabber")
ENV_SETUP_SCRIPT = Path("/home/antigravity/env-setup.sh")


def _safe_test_env(scratch_dir: Path) -> dict[str, str]:
    scratch_dir.mkdir(parents=True, exist_ok=True)
    return {
        **os.environ,
        "TMPDIR": str(scratch_dir),
        "MAGICK_TEMPORARY_PATH": str(scratch_dir),
        "MAGICK_TMPDIR": str(scratch_dir),
    }


# ---------------------------------------------------------------------------
# F4: GS1 Check-Digit, Unicode Guard, Normalization, Placeholders & Splitting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "valid_code",
    [
        "96385074",  # EAN-8 (8 digits)
        "42438915",  # EAN-8 (German retail)
        "887167485471",  # UPC-A (12 digits, Estee Lauder ANR)
        "041554007503",  # UPC-A (12 digits, NS 0 Maybelline)
        "0887167485471",  # EAN-13 (0-padded UPC-A)
        "3600523959648",  # EAN-13 (13 digits, L'Oreal)
        "00887167485471",  # GTIN-14 (00-padded UPC-A)
        "03600523959648",  # GTIN-14 (0-padded EAN-13)
        "37001947163524",  # GTIN-14 (indicator 3, Kiehl's)
    ],
)
def test_verify_gs1_check_digit_valid_lengths(valid_code: str) -> None:
    assert verify_gs1_check_digit(valid_code) is True
    # Flipping final check digit must fail Modulo-10 verification
    wrong_last = str((int(valid_code[-1]) + 1) % 10)
    assert verify_gs1_check_digit(valid_code[:-1] + wrong_last) is False


@pytest.mark.parametrize(
    "bad_input",
    [
        "",
        "1234567",  # len 7
        "123456789",  # len 9
        "12345678901",  # len 11
        "123456789012345",  # len 15
        "١٢٣٤٥٦٧٠",  # Arabic-Indic digits (pass str.isdigit & int() if unguarded)
        "８８７１６７４８５４７１",  # Fullwidth digits (pass str.isdigit & int() if unguarded)
        "1234567²",  # Superscript digit
        "①②③④⑤⑥⑦⑧",  # Circled digits
        "88716748547A",  # Alphabetic suffix
        "00000000",  # Uniform zeros
        "55555555",  # Uniform 5s (8-digit raw Modulo-10 pass if unguarded)
        "555555555555",  # Uniform 5s (12-digit raw Modulo-10 pass if unguarded)
    ],
)
def test_verify_gs1_check_digit_rejects_non_ascii_invalid_lengths_and_uniform(
    bad_input: str,
) -> None:
    assert verify_gs1_check_digit(bad_input) is False


def test_normalize_barcode_upc_a_ean13_gtin14_unification_and_idempotency() -> None:
    from_upc12 = normalize_barcode("887167485471")
    from_ean13 = normalize_barcode("0887167485471")
    from_gtin14 = normalize_barcode("00887167485471")
    from_hyphenated = normalize_barcode("0-88716-74854-71")
    from_spaced = normalize_barcode("0 887167 485471")

    assert from_upc12.is_valid is True
    assert from_upc12.gtin14 == "00887167485471"
    assert from_upc12.upc_a == "887167485471"
    assert from_upc12.ean13 == "0887167485471"
    assert from_upc12.ean8 is None
    assert from_upc12.primary_code == "887167485471"
    assert from_upc12.barcode_type == "UPC-A"
    assert from_upc12.gs1_check_digit_valid is True
    assert from_upc12.gs1_restricted_circulation is False
    assert from_upc12.quarantine_reason is None

    assert from_ean13 == from_upc12
    assert from_gtin14 == from_upc12
    assert from_hyphenated == from_upc12
    assert from_spaced == from_upc12
    assert normalize_barcode(from_upc12.gtin14) == from_upc12
    assert normalize_barcode(from_upc12.primary_code) == from_upc12


def test_normalize_barcode_us_ns0_upc_a_preserved() -> None:
    for raw in ("041554007503", "0041554007503", "00041554007503"):
        nb = normalize_barcode(raw)
        assert nb.is_valid is True
        assert nb.barcode_type == "UPC-A"
        assert nb.upc_a == "041554007503"
        assert nb.ean13 == "0041554007503"
        assert nb.gtin14 == "00041554007503"
        assert normalize_barcode(nb.gtin14) == nb

    # 2-leading-zero US NS 0 UPC-A / 3-leading-zero EAN-13 (z=4 in GTIN-14) is valid UPC-A
    for raw_dove in ("001000000410", "0001000000410", "00001000000410"):
        dove = normalize_barcode(raw_dove)
        assert dove.is_valid is True
        assert dove.barcode_type == "UPC-A"
        assert dove.upc_a == "001000000410"
        assert dove.ean13 == "0001000000410"
        assert dove.ean8 is None
        assert dove.gtin14 == "00001000000410"
        assert dove.gs1_restricted_circulation is False
        assert dove.quarantine_reason is None
        assert normalize_barcode(dove.gtin14) == dove


def test_normalize_barcode_ean13_ean8_and_gtin14_symbologies_and_idempotency() -> None:
    # Non-zero-prefixed EAN-13
    ean13 = normalize_barcode("3600523959648")
    ean13_from_14 = normalize_barcode("03600523959648")
    assert ean13.is_valid is True
    assert ean13.barcode_type == "EAN-13"
    assert ean13.gtin14 == "03600523959648"
    assert ean13.ean13 == "3600523959648"
    assert ean13.upc_a is None
    assert ean13.ean8 is None
    assert ean13.primary_code == "3600523959648"
    assert ean13_from_14 == ean13
    assert normalize_barcode(ean13.gtin14) == ean13
    assert normalize_barcode(ean13.primary_code) == ean13

    # EAN-8 strictly distinct from UPC-A and EAN-13, and idempotent on GTIN-14
    ean8 = normalize_barcode("96385074")
    ean8_spaced = normalize_barcode("9638 5074")
    ean8_from_14 = normalize_barcode("00000096385074")
    assert ean8.is_valid is True
    assert ean8.barcode_type == "EAN-8"
    assert ean8.gtin14 == "00000096385074"
    assert ean8.ean8 == "96385074"
    assert ean8.upc_a is None
    assert ean8.ean13 is None
    assert ean8.primary_code == "96385074"
    assert ean8_spaced == ean8
    assert ean8_from_14 == ean8
    assert normalize_barcode(ean8.gtin14) == ean8
    assert normalize_barcode(ean8.primary_code) == ean8

    # Packaging indicator 1-8 GTIN-14
    gtin14 = normalize_barcode("37001947163524")
    assert gtin14.is_valid is True
    assert gtin14.barcode_type == "GTIN-14"
    assert gtin14.gtin14 == "37001947163524"
    assert gtin14.primary_code == "37001947163524"
    assert gtin14.ean13 is None
    assert gtin14.upc_a is None
    assert gtin14.ean8 is None
    assert normalize_barcode(gtin14.gtin14) == gtin14


@pytest.mark.parametrize(
    "malformed",
    [
        "",
        "   ",
        "not-a-barcode",
        "12345",
        "-887167485471",
        "887167485471-",
        "887167--485471",
        "١٢٣٤٥٦٧٠",
        "８８７１６７４８５４７１",
        "1234567²",
    ],
)
def test_normalize_barcode_quarantines_non_numeric_or_invalid_length(malformed: str) -> None:
    nb = normalize_barcode(malformed)
    assert nb.is_valid is False
    assert nb.gtin14 == ""
    assert nb.barcode_type == "INVALID"
    assert nb.gs1_check_digit_valid is False
    assert nb.quarantine_reason == "NON_NUMERIC_OR_INVALID_LENGTH"


@pytest.mark.parametrize(
    "placeholder",
    [
        "00000000",
        "00000001",
        "0000000000001",
        "55555555",
        "555555555555",
        "005555555555",
        "0555555555555",
        "0005555555555",
        "00555555555555",
        "00005555555555",
        "00000055555555",
        "01234565",
        "12345678",
        "12345670",
        "34567890",
        "76543210",
        "23456789",
        "23456785",
        "65432109",
        "65432105",
        "98765430",
        "000012345670",
        "000034567890",
        "000076543210",
        "001234567895",
        "0001234567895",
        "001234567890",
        "023456789015",
        "012345678905",
        "123456789012",
        "1234567890128",
        "901234567890",
        "9012345678906",
        "09012345678906",
        "89012345678902",
        "10987654321095",
        "90123456789015",
        "987654321098",
        "0987654321098",
        "00987654321098",
        "12345678901231",
    ],
)
def test_placeholder_detection_and_quarantine_precedence(placeholder: str) -> None:
    assert is_placeholder_pattern(placeholder) is True
    nb = normalize_barcode(placeholder)
    assert nb.is_valid is False
    assert nb.barcode_type == "INVALID"
    assert nb.gs1_check_digit_valid is False
    assert nb.quarantine_reason == "PLACEHOLDER_PATTERN"


def test_sequential_placeholders_mathematical_closure_760_cases_and_restricted_guard() -> None:
    assert len(_SEQUENTIAL_PLACEHOLDERS) == 644
    assert all(len(code) in VALID_GS1_LENGTHS for code in _SEQUENTIAL_PLACEHOLDERS)

    checked_cases = 0
    for step in (1, -1):
        for d0 in range(10):
            for length in range(8, 15):
                raw_seq = "".join(str((d0 + idx * step) % 10) for idx in range(length))
                checked_seq = raw_seq[:-1] + _compute_gs1_check_digit(raw_seq[:-1])
                for base in (raw_seq, checked_seq):
                    for target_len in (8, 12, 13, 14):
                        if len(base) <= target_len:
                            padded = base.zfill(target_len)
                            checked_cases += 1
                            assert is_placeholder_pattern(padded) is True
                            nb = normalize_barcode(padded)
                            assert nb.is_valid is False
                            assert nb.quarantine_reason == "PLACEHOLDER_PATTERN"
    assert checked_cases == 760

    # RCN-8 02345673 / 00000002345673 must remain RESTRICTED_CIRCULATION_PREFIX, not hijacked
    for rcn8 in ("02345673", "00000002345673"):
        nb_rcn = normalize_barcode(rcn8)
        assert is_placeholder_pattern(rcn8) is False
        assert nb_rcn.barcode_type == "EAN-8"
        assert nb_rcn.quarantine_reason == "RESTRICTED_CIRCULATION_PREFIX"

    # Direct-call hardening on is_restricted_circulation with mismatched/non-numeric digits
    assert is_restricted_circulation("96385074", "02000276610040") is True
    assert is_restricted_circulation("abcdefgh", "02000276610040") is True


def test_normalize_barcode_quarantines_invalid_check_digit() -> None:
    nb = normalize_barcode("887167485472")
    assert nb.is_valid is False
    assert nb.gtin14 == "00887167485472"
    assert nb.barcode_type == "INVALID"
    assert nb.gs1_check_digit_valid is False
    assert nb.quarantine_reason == "INVALID_CHECK_DIGIT"


@pytest.mark.parametrize(
    ("restricted_raw", "expected_type"),
    [
        ("02345673", "EAN-8"),  # EAN-8 prefix 0
        ("20481933", "EAN-8"),  # EAN-8 prefix 2
        ("248159260313", "UPC-A"),  # UPC-A NS 2 (02 in EAN-13)
        ("448159260317", "UPC-A"),  # UPC-A NS 4 (04 in EAN-13)
        ("0200027661004", "UPC-A"),  # 02 restricted variable-measure
        ("0400027661008", "UPC-A"),  # 04 internal store
        ("2048159260317", "EAN-13"),  # EAN-13 prefix 20
        ("2948159260310", "EAN-13"),  # EAN-13 prefix 29
        ("0000527000057", "EAN-13"),  # OBF 0000 internal prefix (z=5 in GTIN-14)
        ("93660150501002", "GTIN-14"),  # GTIN-14 indicator 9 variable-measure
        ("10200027661001", "GTIN-14"),  # GTIN-14 indicator 1 wrapping restricted 02 EAN-13
        ("80000000286790", "GTIN-14"),  # GTIN-14 indicator 8 wrapping restricted 0000 EAN-13
    ],
)
def test_restricted_circulation_detection_and_quarantine(
    restricted_raw: str,
    expected_type: str,
) -> None:
    nb = normalize_barcode(restricted_raw)
    assert nb.is_valid is False
    assert nb.barcode_type == expected_type
    assert nb.gs1_check_digit_valid is True
    assert nb.gs1_restricted_circulation is True
    assert nb.quarantine_reason == "RESTRICTED_CIRCULATION_PREFIX"
    assert is_restricted_circulation(restricted_raw, nb.gtin14) is True
    assert normalize_barcode(nb.gtin14) == nb


@pytest.mark.parametrize(
    ("raw_restricted_0000", "expected_ean13"),
    [
        ("0000096385074", "0000096385074"),
        ("0000019283746", "0000019283746"),
        ("000096385074", "0000096385074"),
        ("000527000057", "0000527000057"),
        ("00000-9638-5074", "0000096385074"),
    ],
)
def test_restricted_0000_12_and_13_digit_do_not_leak_into_ean8(
    raw_restricted_0000: str,
    expected_ean13: str,
) -> None:
    nb = normalize_barcode(raw_restricted_0000)
    assert nb.is_valid is False
    assert nb.barcode_type == "EAN-13"
    assert nb.ean13 == expected_ean13
    assert nb.upc_a is None
    assert nb.ean8 is None
    assert nb.primary_code == expected_ean13
    assert nb.gs1_check_digit_valid is True
    assert nb.gs1_restricted_circulation is True
    assert nb.quarantine_reason == "RESTRICTED_CIRCULATION_PREFIX"
    assert is_restricted_circulation(raw_restricted_0000, nb.gtin14) is True
    assert normalize_barcode(nb.primary_code) == nb

    # Genuine 8-digit EAN-8 and its canonical 14-digit GTIN-14 remain valid EAN-8
    for valid_ean8_raw in ("96385074", "00000096385074"):
        ean8 = normalize_barcode(valid_ean8_raw)
        assert ean8.is_valid is True
        assert ean8.barcode_type == "EAN-8"
        assert ean8.ean8 == "96385074"
        assert ean8.ean13 is None
        assert ean8.upc_a is None
        assert ean8.gs1_restricted_circulation is False
        assert ean8.quarantine_reason is None
        assert normalize_barcode(ean8.gtin14) == ean8
        assert normalize_barcode(ean8.primary_code) == ean8

    # GTIN-14 packaging indicator 1 wrapping 0000096385074 is also restricted
    wrapped_gtin14 = normalize_barcode("10000096385071")
    assert wrapped_gtin14.is_valid is False
    assert wrapped_gtin14.barcode_type == "GTIN-14"
    assert wrapped_gtin14.gs1_restricted_circulation is True
    assert wrapped_gtin14.quarantine_reason == "RESTRICTED_CIRCULATION_PREFIX"


def test_quarantine_reason_precedence_order() -> None:
    # 1. NON_NUMERIC_OR_INVALID_LENGTH precedes PLACEHOLDER_PATTERN (e.g. 7 zeros)
    assert normalize_barcode("0000000").quarantine_reason == "NON_NUMERIC_OR_INVALID_LENGTH"
    # 2. PLACEHOLDER_PATTERN precedes INVALID_CHECK_DIGIT (e.g. 12345678 fails Modulo-10)
    assert normalize_barcode("12345678").quarantine_reason == "PLACEHOLDER_PATTERN"
    # 3. INVALID_CHECK_DIGIT precedes RESTRICTED_CIRCULATION_PREFIX (prefix 20 with bad check digit)
    assert normalize_barcode("20481937").quarantine_reason == "INVALID_CHECK_DIGIT"
    # 4. RESTRICTED_CIRCULATION_PREFIX when check digit is valid
    assert normalize_barcode("20481933").quarantine_reason == "RESTRICTED_CIRCULATION_PREFIX"


def test_split_concatenated_codes_handles_26_39_delimiters_and_24_digit_trap() -> None:
    # Real 26-digit OBF concatenation -> 2 valid 13-digit EANs
    assert split_concatenated_codes("33378759043153337875904308") == [
        "3337875904315",
        "3337875904308",
    ]
    # Real 39-digit OBF concatenation -> 3 valid 13-digit EANs
    assert split_concatenated_codes("360054262473236005424480935410103510514") == [
        "3600542624732",
        "3600542448093",
        "5410103510514",
    ]
    # Mixed delimiters + space-separated tokens
    assert split_concatenated_codes(
        "887167485471, 3600523959648; 96385074 / 37001947163524 | 041554007503\n3337875904315"
    ) == [
        "887167485471",
        "3600523959648",
        "96385074",
        "37001947163524",
        "041554007503",
        "3337875904315",
    ]
    # Space-separated pair of codes splits cleanly
    assert split_concatenated_codes("887167485471 3600523959648") == [
        "887167485471",
        "3600523959648",
    ]
    # Single formatted barcode with spaces or hyphens is NOT split apart
    assert split_concatenated_codes("0 887167 485471") == ["0 887167 485471"]
    assert split_concatenated_codes("0-88716-74854-71") == ["0-88716-74854-71"]
    assert split_concatenated_codes("9638 5074") == ["9638 5074"]

    # 26-digit string where second chunk has invalid check digit MUST remain unsplit
    bad_26 = "33378759043153337875904309"
    assert split_concatenated_codes(bad_26) == [bad_26]

    # 26-digit strings containing zero-padded uniform or sequential placeholders MUST remain unsplit
    for placeholder_26 in (
        "05555555555550555555555555",
        "00055555555550005555555555",
        "33378759043150005555555555",
        "90123456789069012345678906",
        "33378759043159012345678906",
        "00012345678953337875904315",
    ):
        assert split_concatenated_codes(placeholder_26) == [placeholder_26]

    # 24-digit string (lcm(8, 12) = 24 collision from OBF) MUST NOT split as 2x12
    s24 = "424389154220752842204121"
    assert split_concatenated_codes(s24) == [s24]
    assert normalize_barcode(s24).quarantine_reason == "NON_NUMERIC_OR_INVALID_LENGTH"

    # Empty / delimiter-only vs non-empty garbage
    assert split_concatenated_codes("") == []
    assert split_concatenated_codes(" , ; | \n\t ") == []
    assert split_concatenated_codes("abc123") == ["abc123"]


# ---------------------------------------------------------------------------
# Canonical Pydantic v2 Models (`models.py`)
# ---------------------------------------------------------------------------


def test_pydantic_models_contract_roundtrip_and_zero_warnings() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")

        assert QRMetadata is QRInfo

        norm = normalize_barcode("887167485471")
        with pytest.raises(ValidationError):
            norm.gtin14 = "00000000000000"  # type: ignore[misc]

        binfo = BarcodeInfo.from_normalized(norm, raw_codes=["0887167485471", "887167485471"])
        qr_info = build_qr_metadata(
            gtin14=norm.gtin14,
            primary_code=norm.primary_code,
            barcode_type=norm.barcode_type,
            brand_name="Estée Lauder",
            product_name="Advanced Night Repair Synchronized Multi-Recovery Complex",
        )
        assert qr_info.qr_payload_data.schema == "glow-glare-qr/v1"
        assert list(qr_info.qr_payload_data.model_dump().keys())[0] == "schema"
        assert "schema" in QRPayloadData.model_json_schema()["properties"]

        record = ProductRecord(
            gtin14="00887167485471",
            brand=BrandInfo(
                slug="estee-lauder",
                display_name="Estée Lauder",
                glow_and_glare_vendor_url="https://glowandglare.com/collections/vendors?q=Est%C3%A9e%20Lauder",
                matched_raw_brands=["Estée Lauder", "xx:estee-lauder"],
            ),
            product=ProductDetails(
                name="Advanced Night Repair Synchronized Multi-Recovery Complex",
                quantity="50 ml",
                categories=["Skincare", "Face Serums"],
                description="Synchronized Multi-Recovery Complex serum",
                image_urls=["https://images.openbeautyfacts.org/..."],
            ),
            barcode=binfo,
            qr=qr_info,
            verification=VerificationInfo(
                status="verified_cross_source",
                confidence_score=1.0,
                sources=["openbeautyfacts", "upcitemdb"],
                match_methods={
                    "openbeautyfacts": "brands_tags_leaf",
                    "upcitemdb": "exact_brand_field",
                },
                cross_source_title_similarity=94.5,
            ),
            provenance=ProvenanceInfo(
                openbeautyfacts_url="https://world.openbeautyfacts.org/product/0887167485471",
                upcitemdb_ean="0887167485471",
                harvested_at="2026-11-18T22:45:00Z",
            ),
        )

        dumped = json.loads(record.model_dump_json())
        assert list(dumped.keys()) == [
            "gtin14",
            "product_name",
            "image_url",
            "local_image_path",
            "brand",
            "product",
            "barcode",
            "qr",
            "verification",
            "provenance",
        ]
        assert dumped["product_name"] == "Advanced Night Repair Synchronized Multi-Recovery Complex"
        assert dumped["image_url"] == "https://images.openbeautyfacts.org/..."
        assert dumped["local_image_path"] is None
        assert list(dumped["product"].keys()) == [
            "name",
            "source_names",
            "alternate_names",
            "quantity",
            "categories",
            "description",
            "primary_image_url",
            "thumbnail_url",
            "image_urls",
            "local_image_path",
            "image_status",
        ]
        assert dumped["product"]["primary_image_url"] == "https://images.openbeautyfacts.org/..."
        assert dumped["product"]["image_status"] == "remote_only"
        assert set(dumped["qr"].keys()) == {
            "gs1_digital_link",
            "qr_payload_text",
            "qr_payload_data",
            "qr_standard",
            "qr_svg_path",
        }
        assert dumped["qr"]["qr_svg_path"] is None
        assert list(dumped["qr"]["qr_payload_data"].keys()) == [
            "schema",
            "gtin14",
            "code",
            "type",
            "brand",
            "name",
            "image_url",
        ]
        assert dumped["qr"]["qr_payload_data"] == {
            "schema": "glow-glare-qr/v1",
            "gtin14": "00887167485471",
            "code": "887167485471",
            "type": "UPC-A",
            "brand": "Estée Lauder",
            "name": "Advanced Night Repair Synchronized Multi-Recovery Complex",
            "image_url": "https://images.openbeautyfacts.org/...",
        }
        assert set(dumped["provenance"].keys()) == {
            "openbeautyfacts_url",
            "upcitemdb_ean",
            "harvested_at",
        }
        assert ProductRecord.model_validate(dumped) == record
        # Original qr_info passed into ProductRecord was not mutated in place
        assert qr_info.qr_payload_data.image_url is None

        # Optional qr_png_path and openproductsfacts_url appear when explicitly set
        png_rel = "qr/estee-lauder/00887167485471.png"
        qr_with_png = qr_info.model_copy(update={"qr_png_path": png_rel})
        assert qr_with_png.model_dump()["qr_png_path"] == png_rel
        opf_url = "https://world.openproductsfacts.org/product/0887167485471"
        prov_with_opf = record.provenance.model_copy(update={"openproductsfacts_url": opf_url})
        assert prov_with_opf.model_dump()["openproductsfacts_url"] == opf_url

        # Misaligned GTIN-14 across top-level / barcode / qr must fail validation
        with pytest.raises(ValidationError):
            ProductRecord(
                gtin14="03600523959648",
                brand=record.brand,
                product=record.product,
                barcode=record.barcode,
                qr=record.qr,
                verification=record.verification,
                provenance=record.provenance,
            )

        # BarcodeInfo.from_normalized rejects quarantined barcodes
        with pytest.raises(ValueError, match="Cannot build BarcodeInfo"):
            BarcodeInfo.from_normalized(normalize_barcode("00000000"))


def test_f13_f14_bidirectional_sync_invariants_and_conflict_rejection() -> None:
    norm = normalize_barcode("887167485471")
    binfo = BarcodeInfo.from_normalized(norm)
    brand = BrandInfo(
        slug="estee-lauder",
        display_name="Estée Lauder",
        glow_and_glare_vendor_url="https://glowandglare.com/collections/vendors?q=Est%C3%A9e%20Lauder",
    )
    verif = VerificationInfo(status="verified_single_source", confidence_score=0.9)
    prov = ProvenanceInfo(harvested_at="2026-11-19T00:00:00Z")
    base_qr = build_qr_metadata(
        gtin14=norm.gtin14,
        primary_code=norm.primary_code,
        barcode_type=norm.barcode_type,
        brand_name="Estée Lauder",
        product_name="Advanced Night Repair",
    )

    # 1. Unavailable image status & model_fields_set preservation on ProductDetails
    bare_prod = ProductDetails(name="  Advanced Night Repair  ")
    assert bare_prod.name == "Advanced Night Repair"
    assert bare_prod.image_status == "unavailable"
    assert bare_prod.model_fields_set == {"name"}

    rec_unavailable = ProductRecord(
        gtin14=norm.gtin14,
        brand=brand,
        product=bare_prod,
        barcode=binfo,
        qr=base_qr,
        verification=verif,
        provenance=prov,
    )
    assert rec_unavailable.product_name == "Advanced Night Repair"
    assert rec_unavailable.image_url is None
    assert rec_unavailable.local_image_path is None
    assert rec_unavailable.product.image_status == "unavailable"
    assert rec_unavailable.qr.qr_payload_data.image_url is None

    # 2. Reverse sync: passing top-level image_url and local_image_path with bare_prod
    # must NOT mutate bare_prod and must set downloaded status cleanly
    rec_downloaded = ProductRecord(
        gtin14=norm.gtin14,
        image_url="https://images.openbeautyfacts.org/anr.jpg",
        local_image_path="images/estee-lauder/00887167485471.jpg",
        brand=brand,
        product=bare_prod,
        barcode=binfo,
        qr=base_qr,
        verification=verif,
        provenance=prov,
    )
    assert bare_prod.primary_image_url is None
    assert bare_prod.local_image_path is None
    assert bare_prod.image_status == "unavailable"
    assert rec_downloaded.product.primary_image_url == "https://images.openbeautyfacts.org/anr.jpg"
    assert rec_downloaded.product.image_urls == ["https://images.openbeautyfacts.org/anr.jpg"]
    assert rec_downloaded.product.local_image_path == "images/estee-lauder/00887167485471.jpg"
    assert rec_downloaded.product.image_status == "downloaded"
    assert rec_downloaded.qr.qr_payload_data.image_url == (
        "https://images.openbeautyfacts.org/anr.jpg"
    )

    # 3. Dict-based reverse sync: product dict omitting name uses top-level product_name
    input_dict = {
        "gtin14": norm.gtin14,
        "product_name": "Advanced Night Repair",
        "image_url": "https://images.openbeautyfacts.org/anr.jpg",
        "brand": brand.model_dump(),
        "product": {
            "source_names": {"openbeautyfacts": "Advanced Night Repair"},
            "alternate_names": ["ANR Serum", "ANR Serum"],
        },
        "barcode": binfo.model_dump(),
        "qr": base_qr.model_dump(),
        "verification": verif.model_dump(),
        "provenance": prov.model_dump(),
    }
    rec_from_dict = ProductRecord.model_validate(input_dict)
    assert "name" not in input_dict["product"]  # caller dict not mutated
    assert rec_from_dict.product.name == "Advanced Night Repair"
    assert rec_from_dict.product.alternate_names == ["ANR Serum"]
    assert rec_from_dict.product.image_status == "remote_only"

    # 4. Required schema property check: product_name remains required in JSON Schema
    assert "product_name" in ProductRecord.model_json_schema()["required"]

    # 5. Conflict & empty-string rejections
    with pytest.raises(ValidationError):
        ProductDetails(name="   ")
    with pytest.raises(ValidationError):
        ProductDetails(name="ANR", image_status="downloaded")  # no local_image_path
    with pytest.raises(ValidationError):
        ProductDetails(name="ANR", primary_image_url=None, image_urls=["https://example.com/a.jpg"])
    with pytest.raises(ValidationError):
        ProductRecord(
            gtin14=norm.gtin14,
            product_name="Different Name",
            brand=brand,
            product=bare_prod,
            barcode=binfo,
            qr=base_qr,
            verification=verif,
            provenance=prov,
        )
    with pytest.raises(ValidationError):
        ProductRecord(
            gtin14=norm.gtin14,
            image_url="https://example.com/conflict.jpg",
            brand=brand,
            product=ProductDetails(name="Advanced Night Repair", primary_image_url=None),
            barcode=binfo,
            qr=base_qr,
            verification=verif,
            provenance=prov,
        )
    with pytest.raises(ValidationError):
        ProductRecord(
            gtin14=norm.gtin14,
            local_image_path="images/estee-lauder/conflict.jpg",
            brand=brand,
            product=ProductDetails(
                name="Advanced Night Repair",
                local_image_path="images/estee-lauder/other.jpg",
            ),
            barcode=binfo,
            qr=base_qr,
            verification=verif,
            provenance=prov,
        )
    with pytest.raises(ValidationError):
        ProductRecord(
            gtin14=norm.gtin14,
            image_url="https://example.com/right.jpg",
            brand=brand,
            product=bare_prod,
            barcode=binfo,
            qr=build_qr_metadata(
                gtin14=norm.gtin14,
                primary_code=norm.primary_code,
                barcode_type=norm.barcode_type,
                brand_name="Estée Lauder",
                product_name="Advanced Night Repair",
                image_url="https://example.com/wrong.jpg",
            ),
            verification=verif,
            provenance=prov,
        )


def test_report_manifest_and_cross_milestone_models() -> None:
    brand_file = BrandFilePayload(
        brand_slug="ren",
        display_name="REN",
        letter_section="R",
        is_most_loved=False,
        is_store_own_brand=False,
        alias_of="ren-clean-skincare",
        glow_and_glare_vendor_url="https://glowandglare.com/collections/vendors?q=REN",
        product_count=0,
        canonical_product_count=12,
        cross_source_verified_count=0,
        products=[],
    )
    assert set(brand_file.model_dump().keys()) == {
        "brand_slug",
        "display_name",
        "letter_section",
        "is_most_loved",
        "is_store_own_brand",
        "alias_of",
        "glow_and_glare_vendor_url",
        "product_count",
        "canonical_product_count",
        "cross_source_verified_count",
        "products",
    }

    manifest_entry = BrandManifestEntry(
        slug="ren",
        display_name="REN",
        letter_section="R",
        alias_of="ren-clean-skincare",
        product_count=0,
        canonical_product_count=12,
        file_path="brands/ren.json",
    )
    assert "glow_and_glare_vendor_url" not in manifest_entry.model_dump()
    manifest = BrandsManifest(
        total_vendor_entries=163,
        distinct_brand_entities=161,
        external_brand_entities=160,
        most_loved_count=12,
        brands_with_matches=1,
        brands_zero_matches=162,
        brands=[manifest_entry],
    )
    assert manifest.total_vendor_entries == 163

    csm_auto = CrossSourceMatch(
        gtin14="00887167485471",
        resolved_brand_slug="estee-lauder",
        resolved_brand_display_name="Estée Lauder",
        openbeautyfacts_title="Advanced Night Repair",
        upcitemdb_title="Estee Lauder Advanced Night Repair 50ml",
        title_similarity=94.5,
    )
    assert csm_auto.product_name == "Advanced Night Repair"
    csm_alias = CrossSourceMatch.model_validate(
        {
            "gtin14": "00887167485471",
            "resolved_product_name": "Advanced Night Repair Serum",
            "resolved_brand_slug": "estee-lauder",
            "resolved_brand_display_name": "Estée Lauder",
            "openbeautyfacts_title": "Advanced Night Repair",
            "upcitemdb_title": "Estee Lauder Advanced Night Repair 50ml",
            "title_similarity": 94.5,
        }
    )
    assert csm_alias.product_name == "Advanced Night Repair Serum"
    with pytest.raises(ValidationError):
        CrossSourceMatch(
            gtin14="00887167485471",
            product_name="   ",
            resolved_brand_slug="estee-lauder",
            resolved_brand_display_name="Estée Lauder",
            openbeautyfacts_title="Advanced Night Repair",
            upcitemdb_title="Estee Lauder Advanced Night Repair 50ml",
            title_similarity=94.5,
        )

    comp = ComparisonReport(
        total_unique_products=1,
        verified_cross_source_count=1,
        source_breakdown=SourceBreakdown(
            openbeautyfacts_raw_matched=2,
            openbeautyfacts_unique_valid=1,
            upcitemdb_raw_matched=1,
            upcitemdb_unique_valid=1,
            intra_source_duplicates_removed=1,
            cross_source_exact_gtin_matches=1,
        ),
        cross_source_matches=[csm_auto],
        parent_subbrand_resolutions=[
            ParentSubbrandResolution(
                gtin14="03600523959648",
                openbeautyfacts_brand_slug="loreal",
                upcitemdb_brand_slug="loreal",
                resolved_brand_slug="loreal",
                title_similarity=90.0,
            )
        ],
        brand_conflicts=[
            BrandConflict(
                gtin14="00887167485471",
                openbeautyfacts_brand_slug="estee-lauder",
                openbeautyfacts_title="ANR",
                upcitemdb_brand_slug="clinique",
                upcitemdb_title="Other",
                title_similarity=12.0,
            )
        ],
        duplicate_gtin14_count=0,
    )
    assert comp.duplicate_gtin14_count == 0

    assert len(ALL_QUARANTINE_REASONS) == 7
    assert "MISSING_PRODUCT_NAME" in ALL_QUARANTINE_REASONS
    assert len(default_quarantine_counts()) == 7
    q_report = QuarantineReport(
        total_quarantined=2,
        records=[
            QuarantineRecord(
                raw_code="00000000",
                gtin14="00000000000000",
                reason="PLACEHOLDER_PATTERN",
                source="openbeautyfacts",
            ),
            QuarantineRecord(
                raw_code="887167485471",
                gtin14="00887167485471",
                reason="MISSING_PRODUCT_NAME",
                source="openbeautyfacts",
            ),
        ],
    )
    assert tuple(q_report.counts_by_reason.keys()) == ALL_QUARANTINE_REASONS

    source_map = SourceStatusMap(
        openbeautyfacts=SourceStatusEntry(status="ok", mode="bulk_jsonl"),
        openproductsfacts="disabled",
        upcitemdb=SourceStatusEntry(status="ok", http_status=200),
        barcodelookup=SourceStatusEntry(
            status="blocked_cloudflare_challenge",
            http_status=403,
        ),
    )
    summary = SummaryReport(
        generated_at="2026-11-18T23:00:00Z",
        total_unique_verified_products=25,
        products_with_remote_image_count=20,
        local_images_downloaded_count=18,
        source_status=source_map,
        upcitemdb_quota=UpcitemdbQuota(
            requests_made_this_run=0,
            cache_hits=5,
            rate_limit_remaining=97,
        ),
        output_files={"all_products": "output/all_products.json"},
    )
    assert list(summary.model_dump().keys()) == [
        "generated_at",
        "pipeline_version",
        "total_brands_in_directory",
        "distinct_brand_entities",
        "total_unique_verified_products",
        "products_with_name_count",
        "products_with_remote_image_count",
        "local_images_downloaded_count",
        "cross_source_verified_count",
        "quarantined_count",
        "duplicate_gtin14_count",
        "qr_files_rendered_count",
        "source_status",
        "upcitemdb_quota",
        "output_files",
    ]
    assert summary.products_with_name_count == 25
    assert summary.duplicate_gtin14_count == 0
    with pytest.raises(ValidationError):
        SummaryReport(
            generated_at="2026-11-18T23:00:00Z",
            total_unique_verified_products=25,
            products_with_name_count=24,
            source_status=source_map,
        )

    img_stats = ImageDownloadStats(
        attempted=10,
        downloaded=8,
        cache_hits=2,
        remote_only=1,
        unavailable=1,
        failed=0,
    )
    assert img_stats.downloaded == 8

    seed = BrandSeedEntry(
        slug="estee-lauder",
        display_name="Estée Lauder",
        letter_section="E",
        glow_and_glare_vendor_url="https://glowandglare.com/collections/vendors?q=Est%C3%A9e%20Lauder",
    )
    match = BrandMatch(
        brand_slug=seed.slug,
        display_name=seed.display_name,
        glow_and_glare_vendor_url=seed.glow_and_glare_vendor_url,
        matched_raw_brand="Estée Lauder",
        match_method="brands_tags_leaf",
    )
    qcand = QuarantineCandidate(candidate_brand_slug="apple", raw_title="USB-C Cable")
    assert match.brand_slug == "estee-lauder"
    assert qcand.reason == "CATEGORY_MISMATCH_EXCLUDED"


# ---------------------------------------------------------------------------
# F5: Dual 1D + 2D GS1 Digital Link QR Rendering & Optical Decoding (`qr.py`)
# ---------------------------------------------------------------------------


def test_qr_metadata_validation_and_atomic_svg_png_optical_roundtrip(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Invalid GTIN-14"):
        build_qr_metadata(
            gtin14="00887167485472",  # bad check digit
            primary_code="887167485472",
            barcode_type="UPC-A",
            brand_name="Estée Lauder",
            product_name="ANR",
        )

    meta = build_qr_metadata(
        gtin14="00887167485471",
        primary_code="887167485471",
        barcode_type="UPC-A",
        brand_name="Estée Lauder",
        product_name="Advanced Night Repair",
        qr_svg_path="qr/estee-lauder/00887167485471.svg",
    )
    expected_url = "https://id.gs1.org/01/00887167485471"
    assert meta.gs1_digital_link == expected_url
    assert meta.qr_payload_text == expected_url
    assert meta.qr_standard == "GS1_DIGITAL_LINK_ISO_18004"

    svg_path = tmp_path / "qr" / "estee-lauder" / "00887167485471.svg"
    png_path = tmp_path / "qr" / "estee-lauder" / "00887167485471.png"

    assert render_qr_code(meta.gs1_digital_link, svg_path, fmt="svg", scale=6) == svg_path
    assert render_qr_code(meta.gs1_digital_link, png_path, fmt="png", scale=6) == png_path

    assert svg_path.is_file() and svg_path.stat().st_size > 100
    assert png_path.is_file() and png_path.stat().st_size > 100
    # Ensure atomic sibling write cleaned up any temporary files
    leftover_tmps = list(svg_path.parent.glob("*.tmp"))
    assert leftover_tmps == []

    # Real optical decoding of both SVG and PNG back to exact GS1 Digital Link URL
    assert decode_qr_file(svg_path) == [expected_url]
    assert decode_qr_file(png_path) == [expected_url]

    # Short payload test: confirms micro=False (Micro QR cannot be decoded by ZBar)
    short_png = tmp_path / "short.png"
    render_qr_code("01", short_png, fmt="png", scale=6)
    assert decode_qr_file(short_png) == ["01"]

    # Direct CLI `zbarimg` check + `_decode_with_zbarimg` fallback check with safe env
    safe_env = _safe_test_env(tmp_path / "zbar_scratch")
    zbarimg_bin = shutil.which("zbarimg") or "/usr/bin/zbarimg"
    assert Path(zbarimg_bin).is_file()
    proc = subprocess.run(
        [zbarimg_bin, "--quiet", "--raw", "-Sdisable", "-Sqrcode.enable", str(png_path)],
        capture_output=True,
        check=True,
        env=safe_env,
    )
    assert proc.stdout.decode("utf-8").strip() == expected_url

    with Image.open(png_path) as pil_img:
        assert _decode_with_zbarimg(
            pil_img.convert("RGB"),
            tmp_path / "zbar_scratch",
            safe_env,
        ) == [expected_url]


def test_qr_error_paths_and_non_qr_image(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        render_qr_code("   ", tmp_path / "empty.svg")
    with pytest.raises(ValueError, match="Unsupported QR format"):
        render_qr_code("https://id.gs1.org/01/00887167485471", tmp_path / "bad.jpg", fmt="jpg")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="scale"):
        render_qr_code("https://id.gs1.org/01/00887167485471", tmp_path / "bad.svg", scale=0)

    # build_qr_metadata positional & keyword image_url wiring + empty text rejection
    meta_pos = build_qr_metadata(
        "00887167485471",
        "887167485471",
        "UPC-A",
        "Estée Lauder",
        "Advanced Night Repair",
        "qr/estee-lauder/00887167485471.svg",
        "https://images.openbeautyfacts.org/anr.jpg",
        "qr/estee-lauder/00887167485471.png",
    )
    assert meta_pos.qr_payload_data.image_url == "https://images.openbeautyfacts.org/anr.jpg"
    assert meta_pos.qr_svg_path == "qr/estee-lauder/00887167485471.svg"
    assert meta_pos.qr_png_path == "qr/estee-lauder/00887167485471.png"

    with pytest.raises(ValueError, match="product_name"):
        build_qr_metadata("00887167485471", "887167485471", "UPC-A", "Estée Lauder", "   ")
    with pytest.raises(ValueError, match="brand_name"):
        build_qr_metadata("00887167485471", "887167485471", "UPC-A", "", "ANR")

    with pytest.raises(FileNotFoundError):
        decode_qr_file(tmp_path / "does_not_exist.svg")

    blank_png = tmp_path / "blank.png"
    Image.new("RGB", (200, 200), color=(255, 255, 255)).save(blank_png, format="PNG")
    assert decode_qr_file(blank_png) == []


@pytest.mark.parametrize(
    ("case_id", "payload"),
    [
        ("brand_estee_nfc", "Estée Lauder"),
        ("brand_estee_nfd", "Este\u0301e Lauder"),
        ("brand_loreal", "L'Oréal"),
        ("brand_lancome", "Lancôme"),
        ("brand_chloe", "Chloé"),
        ("brand_kerastase", "Kérastase"),
        ("brand_odacite", "ODACITÉ"),
        ("brand_champo", "CHĀMPO"),
        ("latin1_gbp", "£19.99"),
        ("latin1_plusminus", "±5%"),
        ("latin1_creme", "Crème Fraîche"),
        ("latin1_celsius", "100°C"),
        ("script_emoji", "✨🧴"),
        ("script_arabic", "عطر فاخر للعناية بالبشرة"),
        ("script_cjk", "資生堂 アルティミューン 美容液 50ml"),
        ("script_cyrillic", "Увлажняющий крем для лица"),
        (
            "payload_json_utf8",
            json.dumps(
                build_qr_metadata(
                    "00887167485471",
                    "887167485471",
                    "UPC-A",
                    "Estée Lauder",
                    "Crème Fraîche ±5% £19.99 100°C ✨🧴",
                    image_url="https://images.openbeautyfacts.org/anr.jpg",
                ).qr_payload_data.model_dump(),
                ensure_ascii=False,
            ),
        ),
        ("multiline_lf", "line1\nline2"),
        ("multiline_crlf", "line1\r\nline2"),
        ("multiline_xml_cdata_trap", "line1\r\na < b ]]> c\r\nEstée ✨\n"),
        ("padded_spaces", "  Estée Lauder  "),
    ],
)
def test_qr_utf8_eci_unicode_multiline_and_scale1_optical_roundtrip(
    tmp_path: Path,
    case_id: str,
    payload: str,
) -> None:
    safe_env = _safe_test_env(tmp_path / f"zbar_{case_id}")
    zbarimg_bin = shutil.which("zbarimg") or "/usr/bin/zbarimg"
    assert Path(zbarimg_bin).is_file()

    svg_path = tmp_path / f"{case_id}.svg"
    png_path = tmp_path / f"{case_id}.png"
    png_scale1 = tmp_path / f"{case_id}_s1.png"

    render_qr_code(payload, svg_path, fmt="svg", scale=6)
    render_qr_code(payload, png_path, fmt="png", scale=6)
    render_qr_code(payload, png_scale1, fmt="png", scale=1)

    assert decode_qr_file(svg_path) == [payload]
    assert decode_qr_file(png_path) == [payload]
    assert decode_qr_file(png_scale1) == [payload]

    with Image.open(png_path) as im6:
        assert _decode_with_zbarimg(im6.convert("RGB"), tmp_path / f"zbar_{case_id}", safe_env) == [
            payload
        ]
    with Image.open(png_scale1) as im1:
        assert _decode_with_zbarimg(im1.convert("RGB"), tmp_path / f"zbar_{case_id}", safe_env) == [
            payload
        ]

    proc = subprocess.run(
        [zbarimg_bin, "--raw", "-Sdisable", "-Sqrcode.enable", str(png_path)],
        capture_output=True,
        check=True,
        env=safe_env,
    )
    assert proc.stdout.removesuffix(b"\n").decode("utf-8") == payload


def test_qr_safe_scratch_realpath_and_extensionless_bom_doctype_svg_decode(
    tmp_path: Path,
) -> None:
    # 1. Verify _safe_scratch_env redirects POSIX "//tmp", "/tmp/..", and symlinks to /tmp
    sym_to_sys_tmp = tmp_path / "sym_tmp_target"
    sym_to_sys_tmp.symlink_to("/tmp")
    orig_tmpdir = os.environ.get("TMPDIR")
    try:
        for hostile_tmp in ("//tmp", "///tmp/sub", "/tmp/../tmp", str(sym_to_sys_tmp)):
            os.environ["TMPDIR"] = hostile_tmp
            scratch_base, safe_env, created_fallback = _safe_scratch_env()
            try:
                resolved = os.path.realpath(str(scratch_base))
                assert resolved != "/tmp"
                assert not resolved.startswith("/tmp/")
                assert safe_env["TMPDIR"] == str(scratch_base)
                assert safe_env["MAGICK_TEMPORARY_PATH"] == str(scratch_base)
                assert safe_env["MAGICK_TMPDIR"] == str(scratch_base)
            finally:
                if created_fallback:
                    shutil.rmtree(scratch_base, ignore_errors=True)
    finally:
        if orig_tmpdir is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = orig_tmpdir

    # 2. Verify extensionless BOM + comment + <!DOCTYPE svg> SVG optical decoding
    expected_url = "https://id.gs1.org/01/00887167485471"
    clean_svg = tmp_path / "base.svg"
    render_qr_code(expected_url, clean_svg, fmt="svg", scale=6)
    svg_text = clean_svg.read_text(encoding="utf-8")
    svg_body_start = svg_text.index("<svg")
    bare_svg = svg_text[svg_body_start:]

    preamble = (
        b"\xef\xbb\xbf"
        b"<!-- Glow & Glare Est\xc3\xa9e Lauder QR -->\n"
        b'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" '
        b'"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">\n'
    )
    extless_bom_doctype_svg = tmp_path / "qr_no_extension_bom_doctype"
    extless_bom_doctype_svg.write_bytes(preamble + bare_svg.encode("utf-8"))
    assert _is_svg_file(extless_bom_doctype_svg) is True
    assert decode_qr_file(extless_bom_doctype_svg) == [expected_url]

    # 3. Verify PNG misnamed or extensionless is NOT treated as SVG and still decodes cleanly
    misnamed_png = tmp_path / "actually_png_without_ext"
    render_qr_code(expected_url, tmp_path / "real.png", fmt="png", scale=6)
    misnamed_png.write_bytes((tmp_path / "real.png").read_bytes())
    assert _is_svg_file(misnamed_png) is False
    assert decode_qr_file(misnamed_png) == [expected_url]


# ---------------------------------------------------------------------------
# F1: Cloud & Repo Environment Setup, `.gitignore` & CLI Contract
# ---------------------------------------------------------------------------


def test_env_setup_script_and_gitignore_hygiene(tmp_path: Path) -> None:
    assert ENV_SETUP_SCRIPT.is_file()
    assert os.access(ENV_SETUP_SCRIPT, os.X_OK)
    script_text = ENV_SETUP_SCRIPT.read_text(encoding="utf-8")
    assert "set -euo pipefail" in script_text
    assert "UV_SYSTEM_CERTS=true" in script_text
    assert "MAGICK_TEMPORARY_PATH" in script_text
    assert "MAGICK_TMPDIR" in script_text
    assert "sudo -n" not in script_text

    safe_env = _safe_test_env(tmp_path / "git_scratch")

    def is_ignored(rel_path: str) -> bool:
        res = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", rel_path],
            env=safe_env,
            check=False,
        )
        return res.returncode == 0

    assert is_ignored(".agents/teamwork") is True
    assert is_ignored(".venv/pyvenv.cfg") is True
    assert is_ignored("data/raw_dumps/openbeautyfacts-products.jsonl.gz") is True
    assert is_ignored("data/cache/http.sqlite") is True

    assert is_ignored("data/brands_seed.json") is False
    assert is_ignored("data/upcitemdb_cached_responses.json") is False
    assert is_ignored("output/summary.json") is False


def test_cli_entry_points_and_exit_codes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli_main(["--version"]) == 0
    ver_out = capsys.readouterr().out
    assert __version__ in ver_out

    assert cli_main(["--help"]) == 0
    help_out = capsys.readouterr().out
    for subcmd in (
        "sync-brands",
        "check-sources",
        "harvest",
        "build-catalog",
        "run-all",
        "fetch-images",
        "render-qr",
        "build-web",
        "serve",
        "verify",
    ):
        assert subcmd in help_out

    parser = build_parser()
    run_defaults = parser.parse_args(["run-all"])
    assert run_defaults.download_images is None
    assert run_defaults.max_images == 0
    assert run_defaults.image_concurrency == 12
    assert run_defaults.render_qr is True
    assert parser.parse_args(["run-all", "--download-images"]).download_images is True
    assert parser.parse_args(["run-all", "--no-download-images"]).download_images is False
    assert parser.parse_args(["run-all", "--no-render-qr"]).render_qr is False
    harvest_args = parser.parse_args(["harvest", "--download-images", "--max-images", "10"])
    assert harvest_args.max_images == 10
    assert parser.parse_args(["build-catalog", "--image-concurrency", "6"]).image_concurrency == 6

    fetch_args = parser.parse_args(
        [
            "fetch-images",
            "--output-dir",
            "output",
            "--brand",
            "ren",
            "--max-images",
            "5",
            "--concurrency",
            "4",
        ]
    )
    assert fetch_args.brand == "ren"
    assert fetch_args.max_images == 5
    assert fetch_args.concurrency == 4

    web_args = parser.parse_args(["build-web", "--output-dir", "output"])
    assert web_args.output_dir == Path("output")

    serve_defaults = parser.parse_args(["serve"])
    assert serve_defaults.host == "127.0.0.1"
    assert serve_defaults.port == 8765
    serve_custom = parser.parse_args(
        ["serve", "--output-dir", "output", "--host", "0.0.0.0", "--port", "0"]
    )
    assert serve_custom.port == 0

    verify_args = parser.parse_args(
        ["verify", "--output-dir", "output", "--check-qr-files", "--check-images", "--check-web"]
    )
    assert verify_args.check_qr_files is True
    assert verify_args.check_images is True
    assert verify_args.check_web is True

    assert cli_main(["--definitely-invalid-flag"]) == 2
    capsys.readouterr()

    assert cli_main([]) == 2
    capsys.readouterr()

    assert cli_main(["verify", "--output-dir", str(tmp_path)]) == 1
    err_out = capsys.readouterr().err
    assert "qr-scrabber: error:" in err_out
    assert "Traceback" not in err_out

    # Subprocess checks for `python -m qr_scrabber` and `.venv/bin/qr-scrabber`
    safe_env = _safe_test_env(tmp_path / "cli_scratch")
    mod_proc = subprocess.run(
        [sys.executable, "-m", "qr_scrabber", "--version"],
        capture_output=True,
        text=True,
        check=True,
        env=safe_env,
    )
    assert f"qr-scrabber {__version__}" in mod_proc.stdout

    console_bin = REPO_ROOT / ".venv" / "bin" / "qr-scrabber"
    assert console_bin.is_file()
    bin_proc = subprocess.run(
        [str(console_bin), "--help"],
        capture_output=True,
        text=True,
        check=True,
        env=safe_env,
    )
    assert "run-all" in bin_proc.stdout
    assert "serve" in bin_proc.stdout
    assert "fetch-images" in bin_proc.stdout


def test_playwright_driver_and_system_chromium_available() -> None:
    import playwright.sync_api as pw_sync

    assert hasattr(pw_sync, "sync_playwright")
    chromium_bin = Path("/usr/lib/chromium/chromium")
    assert chromium_bin.is_file()
    assert os.access(chromium_bin, os.X_OK)
