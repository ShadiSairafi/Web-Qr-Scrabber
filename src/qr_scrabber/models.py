"""Canonical Pydantic v2 data models and interface contracts for `qr-scrabber`."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    model_serializer,
    model_validator,
)

ValidBarcodeType = Literal["UPC-A", "EAN-13", "EAN-8", "GTIN-14"]
BarcodeType = Literal["UPC-A", "EAN-13", "EAN-8", "GTIN-14", "INVALID"]

BarcodeQuarantineReason = Literal[
    "NON_NUMERIC_OR_INVALID_LENGTH",
    "PLACEHOLDER_PATTERN",
    "INVALID_CHECK_DIGIT",
    "RESTRICTED_CIRCULATION_PREFIX",
]

QuarantineReason = Literal[
    "NON_NUMERIC_OR_INVALID_LENGTH",
    "PLACEHOLDER_PATTERN",
    "INVALID_CHECK_DIGIT",
    "RESTRICTED_CIRCULATION_PREFIX",
    "CATEGORY_MISMATCH_EXCLUDED",
    "CROSS_SOURCE_BRAND_CONFLICT",
    "MISSING_PRODUCT_NAME",
]

ALL_QUARANTINE_REASONS: tuple[QuarantineReason, ...] = (
    "NON_NUMERIC_OR_INVALID_LENGTH",
    "PLACEHOLDER_PATTERN",
    "INVALID_CHECK_DIGIT",
    "RESTRICTED_CIRCULATION_PREFIX",
    "CATEGORY_MISMATCH_EXCLUDED",
    "CROSS_SOURCE_BRAND_CONFLICT",
    "MISSING_PRODUCT_NAME",
)

ImageStatus = Literal["downloaded", "remote_only", "unavailable"]

VerificationStatus = Literal[
    "verified_cross_source",
    "verified_cross_source_subbrand_resolved",
    "verified_single_source",
    "inferred_title_prefix",
]

RiskTier = Literal["A", "B", "C"]


def default_quarantine_counts() -> dict[str, int]:
    """Return a fresh zero-initialized quarantine reason counter dictionary."""
    return {reason: 0 for reason in ALL_QUARANTINE_REASONS}


class NormalizedBarcode(BaseModel):
    """Immutable canonical GS1 normalization result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gtin14: str
    ean13: str | None = None
    upc_a: str | None = None
    ean8: str | None = None
    primary_code: str
    barcode_type: BarcodeType
    gs1_check_digit_valid: bool
    gs1_restricted_circulation: bool
    quarantine_reason: QuarantineReason | None = None

    @property
    def is_valid(self) -> bool:
        """Return True iff the barcode passed all GS1 checks and is unrestricted."""
        return (
            self.gs1_check_digit_valid
            and not self.gs1_restricted_circulation
            and self.quarantine_reason is None
            and self.barcode_type != "INVALID"
            and len(self.gtin14) == 14
        )


class BrandInfo(BaseModel):
    """Brand metadata embedded inside a `ProductRecord`."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    display_name: str
    glow_and_glare_vendor_url: str
    matched_raw_brands: list[str] = Field(default_factory=list)


class ProductDetails(BaseModel):
    """Descriptive product attributes embedded inside a `ProductRecord`."""

    model_config = ConfigDict(extra="forbid")

    name: str
    source_names: dict[str, str] = Field(default_factory=dict)
    alternate_names: list[str] = Field(default_factory=list)
    quantity: str | None = None
    categories: list[str] = Field(default_factory=list)
    description: str | None = None
    primary_image_url: str | None = None
    thumbnail_url: str | None = None
    image_urls: list[str] = Field(default_factory=list)
    local_image_path: str | None = None
    image_status: ImageStatus = "unavailable"

    @model_validator(mode="after")
    def _validate_and_sync_details(self) -> ProductDetails:
        clean_name = self.name.strip()
        if not clean_name:
            raise ValueError("ProductDetails.name must be a non-empty string.")
        if clean_name != self.name:
            object.__setattr__(self, "name", clean_name)

        cleaned_source_names: dict[str, str] = {}
        for raw_key, raw_val in self.source_names.items():
            sk = raw_key.strip()
            sv = raw_val.strip()
            if not sk or not sv:
                raise ValueError(
                    "ProductDetails.source_names keys and values must be non-empty strings."
                )
            cleaned_source_names[sk] = sv
        if cleaned_source_names != self.source_names:
            object.__setattr__(self, "source_names", cleaned_source_names)

        cleaned_alt_names: list[str] = []
        for alt in self.alternate_names:
            sa = alt.strip()
            if not sa:
                raise ValueError(
                    "ProductDetails.alternate_names entries must be non-empty strings."
                )
            if sa not in cleaned_alt_names:
                cleaned_alt_names.append(sa)
        if cleaned_alt_names != self.alternate_names:
            object.__setattr__(self, "alternate_names", cleaned_alt_names)

        for optional_field in ("primary_image_url", "thumbnail_url", "local_image_path"):
            raw_opt = getattr(self, optional_field)
            if raw_opt is not None:
                stripped_opt = raw_opt.strip()
                if not stripped_opt:
                    raise ValueError(
                        f"ProductDetails.{optional_field} must not be empty or whitespace-only."
                    )
                if stripped_opt != raw_opt:
                    object.__setattr__(self, optional_field, stripped_opt)

        cleaned_urls: list[str] = []
        for raw_url in self.image_urls:
            stripped_url = raw_url.strip()
            if not stripped_url:
                raise ValueError("ProductDetails.image_urls entries must be non-empty strings.")
            if stripped_url not in cleaned_urls:
                cleaned_urls.append(stripped_url)

        if self.primary_image_url is None:
            if cleaned_urls:
                if "primary_image_url" in self.model_fields_set:
                    raise ValueError(
                        "ProductDetails.primary_image_url cannot be explicitly None "
                        "when image_urls is non-empty."
                    )
                object.__setattr__(self, "primary_image_url", cleaned_urls[0])
            elif (
                self.thumbnail_url is not None and "primary_image_url" not in self.model_fields_set
            ):
                object.__setattr__(self, "primary_image_url", self.thumbnail_url)
                cleaned_urls.append(self.thumbnail_url)
        else:
            if self.primary_image_url not in cleaned_urls:
                cleaned_urls.insert(0, self.primary_image_url)
            if (
                self.thumbnail_url is not None
                and "image_urls" not in self.model_fields_set
                and self.thumbnail_url not in cleaned_urls
            ):
                cleaned_urls.append(self.thumbnail_url)

        if cleaned_urls != self.image_urls:
            object.__setattr__(self, "image_urls", cleaned_urls)

        if self.local_image_path is not None:
            expected_status: ImageStatus = "downloaded"
        elif self.primary_image_url is not None:
            expected_status = "remote_only"
        else:
            expected_status = "unavailable"

        if "image_status" in self.model_fields_set:
            if self.image_status != expected_status:
                raise ValueError(
                    f"Inconsistent ProductDetails.image_status={self.image_status!r}; "
                    f"expected {expected_status!r} for "
                    f"primary_image_url={self.primary_image_url!r}, "
                    f"local_image_path={self.local_image_path!r}."
                )
        else:
            object.__setattr__(self, "image_status", expected_status)

        return self


class BarcodeInfo(BaseModel):
    """Structured 1D GS1 barcode representations embedded inside a `ProductRecord`."""

    model_config = ConfigDict(extra="forbid")

    gtin14: str
    ean13: str | None = None
    upc_a: str | None = None
    ean8: str | None = None
    primary_code: str
    barcode_type: ValidBarcodeType
    raw_codes: list[str] = Field(default_factory=list)
    gs1_check_digit_valid: bool = True
    gs1_restricted_circulation: bool = False

    @classmethod
    def from_normalized(
        cls,
        norm: NormalizedBarcode,
        raw_codes: list[str] | None = None,
    ) -> BarcodeInfo:
        """Build `BarcodeInfo` from a validated, unrestricted `NormalizedBarcode`."""
        if not norm.is_valid or norm.barcode_type == "INVALID":
            raise ValueError(
                f"Cannot build BarcodeInfo from invalid barcode: {norm.quarantine_reason}"
            )
        codes = list(dict.fromkeys(raw_codes or [norm.primary_code]))
        return cls(
            gtin14=norm.gtin14,
            ean13=norm.ean13,
            upc_a=norm.upc_a,
            ean8=norm.ean8,
            primary_code=norm.primary_code,
            barcode_type=norm.barcode_type,
            raw_codes=codes,
            gs1_check_digit_valid=norm.gs1_check_digit_valid,
            gs1_restricted_circulation=norm.gs1_restricted_circulation,
        )


class QRPayloadData(BaseModel):
    """Structured 2D QR payload descriptor with zero-warning `schema` key handling."""

    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    schema_: str = Field(
        default="glow-glare-qr/v1",
        alias="schema",
        validation_alias=AliasChoices("schema", "schema_"),
        serialization_alias="schema",
    )
    gtin14: str
    code: str
    type: str
    brand: str
    name: str
    image_url: str | None = None

    @model_validator(mode="after")
    def _validate_non_empty_text_fields(self) -> QRPayloadData:
        clean_name = self.name.strip()
        if not clean_name:
            raise ValueError("QRPayloadData.name must be a non-empty string.")
        if clean_name != self.name:
            object.__setattr__(self, "name", clean_name)

        clean_brand = self.brand.strip()
        if not clean_brand:
            raise ValueError("QRPayloadData.brand must be a non-empty string.")
        if clean_brand != self.brand:
            object.__setattr__(self, "brand", clean_brand)

        if self.image_url is not None:
            clean_url = self.image_url.strip()
            if not clean_url:
                raise ValueError("QRPayloadData.image_url must not be empty or whitespace-only.")
            if clean_url != self.image_url:
                object.__setattr__(self, "image_url", clean_url)
        return self

    @property
    def schema(self) -> str:  # type: ignore[override]
        """Return the QR payload schema identifier without shadowing warnings."""
        return self.schema_

    @model_serializer(mode="wrap")
    def _serialize_with_schema_key(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if "schema_" in data:
            return {"schema": data.pop("schema_"), **data}
        return data


class QRInfo(BaseModel):
    """GS1 Digital Link / ISO 18004 2D QR metadata inside a `ProductRecord`."""

    model_config = ConfigDict(extra="forbid")

    gs1_digital_link: str
    qr_payload_text: str
    qr_payload_data: QRPayloadData
    qr_standard: str = "GS1_DIGITAL_LINK_ISO_18004"
    qr_svg_path: str | None = None
    qr_png_path: str | None = None

    @model_serializer(mode="wrap")
    def _serialize_optional_png(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.qr_png_path is None:
            data.pop("qr_png_path", None)
        return data


QRMetadata = QRInfo


class VerificationInfo(BaseModel):
    """Cross-source verification and confidence metadata for a `ProductRecord`."""

    model_config = ConfigDict(extra="forbid")

    status: VerificationStatus
    confidence_score: float = Field(ge=0.0, le=1.0)
    sources: list[str] = Field(default_factory=list)
    match_methods: dict[str, str] = Field(default_factory=dict)
    cross_source_title_similarity: float | None = Field(default=None, ge=0.0, le=100.0)


class ProvenanceInfo(BaseModel):
    """Upstream source provenance metadata for a `ProductRecord`."""

    model_config = ConfigDict(extra="forbid")

    openbeautyfacts_url: str | None = None
    upcitemdb_ean: str | None = None
    harvested_at: str
    openproductsfacts_url: str | None = None

    @model_serializer(mode="wrap")
    def _serialize_optional_opf(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.openproductsfacts_url is None:
            data.pop("openproductsfacts_url", None)
        return data


class ProductRecord(BaseModel):
    """Canonical deduplicated product record in `all_products.json` and `brands/*.json`."""

    model_config = ConfigDict(extra="forbid")

    gtin14: str
    product_name: str
    image_url: str | None = None
    local_image_path: str | None = None
    brand: BrandInfo
    product: ProductDetails
    barcode: BarcodeInfo
    qr: QRInfo
    verification: VerificationInfo
    provenance: ProvenanceInfo

    @model_validator(mode="before")
    @classmethod
    def _sync_before_validation(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        raw_prod = out.get("product")

        top_has_pn = "product_name" in out
        top_pn = out.get("product_name")
        if top_has_pn and isinstance(top_pn, str):
            clean_top_pn = top_pn.strip()
            if not clean_top_pn:
                raise ValueError("ProductRecord.product_name must be a non-empty string.")
            out["product_name"] = clean_top_pn

        top_has_img = "image_url" in out
        top_img = out.get("image_url")
        if top_has_img and isinstance(top_img, str):
            clean_top_img = top_img.strip()
            if not clean_top_img:
                raise ValueError("ProductRecord.image_url must not be empty or whitespace-only.")
            out["image_url"] = clean_top_img

        top_has_local = "local_image_path" in out
        top_local = out.get("local_image_path")
        if top_has_local and isinstance(top_local, str):
            clean_top_local = top_local.strip()
            if not clean_top_local:
                raise ValueError(
                    "ProductRecord.local_image_path must not be empty or whitespace-only."
                )
            out["local_image_path"] = clean_top_local

        if isinstance(raw_prod, ProductDetails):
            prod_fields_set = set(raw_prod.model_fields_set)
            prod_kwargs: dict[str, Any] = {
                field_name: getattr(raw_prod, field_name)
                for field_name in raw_prod.model_fields_set
            }
            rebuild_prod = False

            if not top_has_pn:
                out["product_name"] = raw_prod.name

            if not top_has_img:
                out["image_url"] = raw_prod.primary_image_url
            elif (
                "primary_image_url" not in prod_fields_set
                and raw_prod.primary_image_url is None
                and out["image_url"] is not None
            ):
                prod_kwargs["primary_image_url"] = out["image_url"]
                rebuild_prod = True

            if not top_has_local:
                out["local_image_path"] = raw_prod.local_image_path
            elif (
                "local_image_path" not in prod_fields_set
                and raw_prod.local_image_path is None
                and out["local_image_path"] is not None
            ):
                prod_kwargs["local_image_path"] = out["local_image_path"]
                rebuild_prod = True

            if rebuild_prod:
                out["product"] = ProductDetails(**prod_kwargs)

        elif isinstance(raw_prod, dict):
            prod_dict = dict(raw_prod)
            prod_has_name = "name" in prod_dict
            if prod_has_name and isinstance(prod_dict["name"], str):
                clean_prod_name = prod_dict["name"].strip()
                if not clean_prod_name:
                    raise ValueError("ProductDetails.name must be a non-empty string.")
                prod_dict["name"] = clean_prod_name
                if not top_has_pn:
                    out["product_name"] = clean_prod_name
            elif not prod_has_name and top_has_pn and isinstance(out["product_name"], str):
                prod_dict["name"] = out["product_name"]

            if (
                top_has_img
                and "primary_image_url" not in prod_dict
                and not prod_dict.get("image_urls")
                and not prod_dict.get("thumbnail_url")
                and out["image_url"] is not None
            ):
                prod_dict["primary_image_url"] = out["image_url"]

            if (
                top_has_local
                and "local_image_path" not in prod_dict
                and out["local_image_path"] is not None
            ):
                prod_dict["local_image_path"] = out["local_image_path"]

            validated_prod = ProductDetails(**prod_dict)
            out["product"] = validated_prod
            if not top_has_img:
                out["image_url"] = validated_prod.primary_image_url
            if not top_has_local:
                out["local_image_path"] = validated_prod.local_image_path

        return out

    @model_validator(mode="after")
    def _validate_record_alignment(self) -> ProductRecord:
        if not self.product_name.strip():
            raise ValueError("ProductRecord.product_name must be a non-empty string.")
        if self.product_name != self.product.name:
            raise ValueError(
                f"Conflicting product_name ({self.product_name!r}) and "
                f"product.name ({self.product.name!r})."
            )
        if self.image_url != self.product.primary_image_url:
            raise ValueError(
                f"Conflicting image_url ({self.image_url!r}) and "
                f"product.primary_image_url ({self.product.primary_image_url!r})."
            )
        if self.local_image_path != self.product.local_image_path:
            raise ValueError(
                f"Conflicting local_image_path ({self.local_image_path!r}) and "
                f"product.local_image_path ({self.product.local_image_path!r})."
            )
        if self.gtin14 != self.barcode.gtin14 or self.gtin14 != self.qr.qr_payload_data.gtin14:
            raise ValueError(
                f"Inconsistent gtin14 across ProductRecord ({self.gtin14}), "
                f"barcode ({self.barcode.gtin14}), and qr ({self.qr.qr_payload_data.gtin14})"
            )
        if self.image_url is not None and self.qr.qr_payload_data.image_url is None:
            updated_payload = self.qr.qr_payload_data.model_copy(
                update={"image_url": self.image_url}
            )
            updated_qr = self.qr.model_copy(update={"qr_payload_data": updated_payload})
            object.__setattr__(self, "qr", updated_qr)
        elif (
            self.qr.qr_payload_data.image_url is not None
            and self.qr.qr_payload_data.image_url != self.image_url
        ):
            raise ValueError(
                f"Conflicting qr.qr_payload_data.image_url "
                f"({self.qr.qr_payload_data.image_url!r}) and image_url ({self.image_url!r})."
            )
        return self


class BrandFilePayload(BaseModel):
    """Per-brand catalog file payload (`output/brands/<slug>.json`)."""

    model_config = ConfigDict(extra="forbid")

    brand_slug: str
    display_name: str
    letter_section: str
    is_most_loved: bool = False
    is_store_own_brand: bool = False
    alias_of: str | None = None
    glow_and_glare_vendor_url: str
    product_count: int = Field(default=0, ge=0)
    canonical_product_count: int = Field(default=0, ge=0)
    cross_source_verified_count: int = Field(default=0, ge=0)
    products: list[ProductRecord] = Field(default_factory=list)


class BrandManifestEntry(BaseModel):
    """Per-brand summary entry in `output/brands_manifest.json`."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    display_name: str
    letter_section: str
    is_most_loved: bool = False
    is_store_own_brand: bool = False
    alias_of: str | None = None
    glow_and_glare_vendor_url: str | None = None
    product_count: int = Field(default=0, ge=0)
    canonical_product_count: int = Field(default=0, ge=0)
    file_path: str

    @model_serializer(mode="wrap")
    def _serialize_optional_vendor_url(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.glow_and_glare_vendor_url is None:
            data.pop("glow_and_glare_vendor_url", None)
        return data


class BrandsManifest(BaseModel):
    """Top-level manifest payload (`output/brands_manifest.json`)."""

    model_config = ConfigDict(extra="forbid")

    total_vendor_entries: int = Field(ge=0)
    distinct_brand_entities: int = Field(ge=0)
    external_brand_entities: int = Field(ge=0)
    most_loved_count: int = Field(default=0, ge=0)
    brands_with_matches: int = Field(default=0, ge=0)
    brands_zero_matches: int = Field(default=0, ge=0)
    brands: list[BrandManifestEntry] = Field(default_factory=list)


class SourceBreakdown(BaseModel):
    """Harvest and deduplication counts per source inside `ComparisonReport`."""

    model_config = ConfigDict(extra="forbid")

    openbeautyfacts_raw_matched: int = Field(default=0, ge=0)
    openbeautyfacts_unique_valid: int = Field(default=0, ge=0)
    upcitemdb_raw_matched: int = Field(default=0, ge=0)
    upcitemdb_unique_valid: int = Field(default=0, ge=0)
    intra_source_duplicates_removed: int = Field(default=0, ge=0)
    cross_source_exact_gtin_matches: int = Field(default=0, ge=0)


class CrossSourceMatch(BaseModel):
    """Matched GTIN comparison entry in `output/comparison_report.json`."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    gtin14: str
    product_name: str = Field(
        default="",
        validation_alias=AliasChoices("product_name", "resolved_product_name"),
    )
    resolved_brand_slug: str
    resolved_brand_display_name: str
    openbeautyfacts_title: str
    upcitemdb_title: str
    title_similarity: float = Field(ge=0.0, le=100.0)
    verification_status: VerificationStatus = "verified_cross_source"

    @model_validator(mode="after")
    def _sync_product_name(self) -> CrossSourceMatch:
        if "product_name" in self.model_fields_set:
            cleaned = self.product_name.strip()
            if not cleaned:
                raise ValueError("CrossSourceMatch.product_name must be a non-empty string.")
            if cleaned != self.product_name:
                object.__setattr__(self, "product_name", cleaned)
        else:
            fallback = self.openbeautyfacts_title.strip() or self.upcitemdb_title.strip()
            if not fallback:
                raise ValueError(
                    "CrossSourceMatch requires a non-empty product_name or source title."
                )
            object.__setattr__(self, "product_name", fallback)
        return self


class ParentSubbrandResolution(BaseModel):
    """Parent conglomerate to sub-brand resolution entry in `ComparisonReport`."""

    model_config = ConfigDict(extra="forbid")

    gtin14: str
    openbeautyfacts_brand_slug: str
    upcitemdb_brand_slug: str
    resolved_brand_slug: str
    title_similarity: float = Field(default=0.0, ge=0.0, le=100.0)


class BrandConflict(BaseModel):
    """Cross-source brand conflict entry in `ComparisonReport`."""

    model_config = ConfigDict(extra="forbid")

    gtin14: str
    openbeautyfacts_brand_slug: str
    openbeautyfacts_title: str
    upcitemdb_brand_slug: str
    upcitemdb_title: str
    title_similarity: float = Field(default=0.0, ge=0.0, le=100.0)
    resolution: str = "quarantined"


class ComparisonReport(BaseModel):
    """Cross-source reconciliation analytics (`output/comparison_report.json`)."""

    model_config = ConfigDict(extra="forbid")

    total_unique_products: int = Field(default=0, ge=0)
    verified_cross_source_count: int = Field(default=0, ge=0)
    verified_cross_source_subbrand_resolved_count: int = Field(default=0, ge=0)
    verified_single_source_count: int = Field(default=0, ge=0)
    inferred_title_prefix_count: int = Field(default=0, ge=0)
    source_breakdown: SourceBreakdown = Field(default_factory=SourceBreakdown)
    cross_source_matches: list[CrossSourceMatch] = Field(default_factory=list)
    parent_subbrand_resolutions: list[ParentSubbrandResolution] = Field(default_factory=list)
    brand_conflicts: list[BrandConflict] = Field(default_factory=list)
    duplicate_gtin14_count: int = Field(default=0, ge=0)


class QuarantineRecord(BaseModel):
    """Quarantined barcode or conflicting candidate record (`output/quarantine.json`)."""

    model_config = ConfigDict(extra="forbid")

    raw_code: str
    gtin14: str | None = None
    reason: QuarantineReason
    source: str
    raw_brand: str | None = None
    raw_title: str | None = None


class QuarantineReport(BaseModel):
    """Top-level quarantine report (`output/quarantine.json`)."""

    model_config = ConfigDict(extra="forbid")

    total_quarantined: int = Field(default=0, ge=0)
    counts_by_reason: dict[str, int] = Field(default_factory=default_quarantine_counts)
    records: list[QuarantineRecord] = Field(default_factory=list)


class SourceStatusEntry(BaseModel):
    """Reachability diagnostic entry for an upstream barcode source."""

    model_config = ConfigDict(extra="forbid")

    status: str
    http_status: int | None = None
    mode: str | None = None
    detail: str | None = None


class SourceStatusMap(BaseModel):
    """Status map across all four tracked barcode sources."""

    model_config = ConfigDict(extra="forbid")

    openbeautyfacts: SourceStatusEntry | str
    openproductsfacts: SourceStatusEntry | str
    upcitemdb: SourceStatusEntry | str
    barcodelookup: SourceStatusEntry | str


class UpcitemdbQuota(BaseModel):
    """UPCitemdb request and cache accounting metrics."""

    model_config = ConfigDict(extra="forbid")

    requests_made_this_run: int = Field(default=0, ge=0)
    cache_hits: int = Field(default=0, ge=0)
    rate_limit_remaining: int | None = None


class SummaryReport(BaseModel):
    """Top-level pipeline run summary (`output/summary.json`)."""

    model_config = ConfigDict(extra="forbid")

    generated_at: str
    pipeline_version: str = "0.1.0"
    total_brands_in_directory: int = Field(default=163, ge=0)
    distinct_brand_entities: int = Field(default=161, ge=0)
    total_unique_verified_products: int = Field(default=0, ge=0)
    products_with_name_count: int = Field(default=0, ge=0)
    products_with_remote_image_count: int = Field(default=0, ge=0)
    local_images_downloaded_count: int = Field(default=0, ge=0)
    cross_source_verified_count: int = Field(default=0, ge=0)
    quarantined_count: int = Field(default=0, ge=0)
    duplicate_gtin14_count: int = Field(default=0, ge=0)
    qr_files_rendered_count: int = Field(default=0, ge=0)
    source_status: SourceStatusMap
    upcitemdb_quota: UpcitemdbQuota = Field(default_factory=UpcitemdbQuota)
    output_files: dict[str, str] | list[str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _sync_products_with_name_count(self) -> SummaryReport:
        if "products_with_name_count" in self.model_fields_set:
            if self.products_with_name_count != self.total_unique_verified_products:
                raise ValueError(
                    f"SummaryReport.products_with_name_count ({self.products_with_name_count}) "
                    f"must equal total_unique_verified_products "
                    f"({self.total_unique_verified_products})."
                )
        else:
            object.__setattr__(
                self,
                "products_with_name_count",
                self.total_unique_verified_products,
            )
        return self


class ImageDownloadStats(BaseModel):
    """Summary counters returned by `ProductImageDownloader.download_for_products`."""

    model_config = ConfigDict(extra="forbid")

    attempted: int = Field(default=0, ge=0)
    downloaded: int = Field(default=0, ge=0)
    cache_hits: int = Field(default=0, ge=0)
    remote_only: int = Field(default=0, ge=0)
    unavailable: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)


class BrandSeedEntry(BaseModel):
    """Authoritative Glow and Glare brand seed entry (`data/brands_seed.json`)."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    display_name: str
    letter_section: str
    glow_and_glare_vendor_url: str
    is_most_loved: bool = False
    is_store_own_brand: bool = False
    alias_of: str | None = None
    risk_tier: RiskTier = "A"
    aliases: list[str] = Field(default_factory=list)
    required_positive_keywords: list[str] = Field(default_factory=list)
    negative_keywords: list[str] = Field(default_factory=list)
    blocked_brand_tags: list[str] = Field(default_factory=list)


class BrandMatch(BaseModel):
    """Positive brand match emitted by `BrandMatcher`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    brand_slug: str
    display_name: str
    glow_and_glare_vendor_url: str
    matched_raw_brand: str
    match_method: str
    confidence: float = Field(default=0.85, ge=0.0, le=1.0)


class QuarantineCandidate(BaseModel):
    """Category-gated exclusion candidate emitted by `BrandMatcher`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_brand_slug: str
    reason: QuarantineReason = "CATEGORY_MISMATCH_EXCLUDED"
    raw_brand: str | None = None
    raw_title: str | None = None
    detail: str | None = None


__all__ = [
    "ALL_QUARANTINE_REASONS",
    "BarcodeInfo",
    "BarcodeQuarantineReason",
    "BarcodeType",
    "BrandConflict",
    "BrandFilePayload",
    "BrandInfo",
    "BrandManifestEntry",
    "BrandMatch",
    "BrandSeedEntry",
    "BrandsManifest",
    "ComparisonReport",
    "CrossSourceMatch",
    "ImageDownloadStats",
    "ImageStatus",
    "NormalizedBarcode",
    "ParentSubbrandResolution",
    "ProductDetails",
    "ProductRecord",
    "ProvenanceInfo",
    "QRInfo",
    "QRMetadata",
    "QRPayloadData",
    "QuarantineCandidate",
    "QuarantineReason",
    "QuarantineRecord",
    "QuarantineReport",
    "RiskTier",
    "SourceBreakdown",
    "SourceStatusEntry",
    "SourceStatusMap",
    "SummaryReport",
    "UpcitemdbQuota",
    "ValidBarcodeType",
    "VerificationInfo",
    "VerificationStatus",
    "default_quarantine_counts",
]
