"""Cross-source barcode reconciliation, product-name selection, and strict deduplication."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from rapidfuzz import fuzz

from qr_scrabber.brands.registry import BrandRegistry, fold_unicode
from qr_scrabber.harvesters.openbeautyfacts import HarvestedCandidate, SourceHarvestResult
from qr_scrabber.models import (
    ALL_QUARANTINE_REASONS,
    BarcodeInfo,
    BrandConflict,
    BrandInfo,
    ComparisonReport,
    CrossSourceMatch,
    ParentSubbrandResolution,
    ProductDetails,
    ProductRecord,
    ProvenanceInfo,
    QuarantineRecord,
    QuarantineReport,
    SourceBreakdown,
    VerificationInfo,
    VerificationStatus,
    default_quarantine_counts,
)
from qr_scrabber.qr import build_qr_metadata


@dataclass
class ReconciliationResult:
    """Complete reconciled, deduplicated catalog and audit reports."""

    products: list[ProductRecord]
    comparison_report: ComparisonReport
    quarantine_report: QuarantineReport


def _clean_name(value: str | None) -> str:
    return " ".join((value or "").split()).strip()


def _name_quality_score(name: str) -> tuple[int, int, int]:
    """Rank candidate product names: prefer readable mixed-case descriptive titles."""
    stripped = _clean_name(name)
    if not stripped:
        return (0, 0, 0)
    is_not_all_caps = 1 if (not stripped.isupper() or len(stripped) <= 6) else 0
    word_count = len(stripped.split())
    # Reward multi-word descriptive names without letting 300-char spammy SEO strings dominate
    bounded_len = min(len(stripped), 120)
    return (is_not_all_caps, min(word_count, 16), bounded_len)


def _choose_canonical_name(
    obf_name: str | None,
    upc_name: str | None,
    extra_names: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Select the cleanest canonical product name and collect distinct alternate names."""
    candidates: list[str] = []
    seen_cf: set[str] = set()
    for raw in [obf_name, upc_name, *(extra_names or [])]:
        cleaned = _clean_name(raw)
        if cleaned and cleaned.casefold() not in seen_cf:
            seen_cf.add(cleaned.casefold())
            candidates.append(cleaned)

    if not candidates:
        return "", []

    # Compare top source names by readability/richness
    primary_pool = [c for c in (_clean_name(obf_name), _clean_name(upc_name)) if c]
    if primary_pool:
        best = max(primary_pool, key=_name_quality_score)
    else:
        best = max(candidates, key=_name_quality_score)

    alternates = [c for c in candidates if c.casefold() != best.casefold()]
    return best, alternates


def _merge_candidate(
    existing: HarvestedCandidate, incoming: HarvestedCandidate
) -> HarvestedCandidate:
    """Merge duplicate records for the same `gtin14` within one upstream source."""
    raw_codes = list(dict.fromkeys([*existing.raw_codes, *incoming.raw_codes]))
    categories = list(dict.fromkeys([*existing.categories, *incoming.categories]))
    image_urls = list(dict.fromkeys([*existing.image_urls, *incoming.image_urls]))

    # Prefer higher confidence, then newer last_modified_t, then richer name
    keep_incoming = (
        incoming.match_confidence,
        incoming.last_modified_t,
        _name_quality_score(incoming.product_name or ""),
    ) > (
        existing.match_confidence,
        existing.last_modified_t,
        _name_quality_score(existing.product_name or ""),
    )
    primary = incoming if keep_incoming else existing
    secondary = existing if keep_incoming else incoming

    best_name, alts = _choose_canonical_name(
        primary.product_name,
        secondary.product_name,
        [*primary.alternate_names, *secondary.alternate_names],
    )
    primary_img = (
        primary.primary_image_url
        or secondary.primary_image_url
        or (image_urls[0] if image_urls else None)
    )
    thumb_img = primary.thumbnail_url or secondary.thumbnail_url or primary_img

    return HarvestedCandidate(
        source=primary.source,
        gtin14=primary.gtin14,
        barcode=primary.barcode,
        raw_codes=raw_codes,
        brand_slug=primary.brand_slug,
        brand_display_name=primary.brand_display_name,
        glow_and_glare_vendor_url=primary.glow_and_glare_vendor_url,
        matched_raw_brand=primary.matched_raw_brand,
        match_method=primary.match_method,
        match_confidence=max(existing.match_confidence, incoming.match_confidence),
        product_name=best_name or primary.product_name,
        alternate_names=alts,
        quantity=primary.quantity or secondary.quantity,
        categories=categories,
        description=primary.description or secondary.description,
        primary_image_url=primary_img,
        thumbnail_url=thumb_img,
        image_urls=image_urls,
        source_url=primary.source_url or secondary.source_url,
        upcitemdb_ean=primary.upcitemdb_ean or secondary.upcitemdb_ean,
        last_modified_t=max(existing.last_modified_t, incoming.last_modified_t),
    )


def _deduplicate_source(
    candidates: list[HarvestedCandidate],
) -> tuple[dict[str, HarvestedCandidate], int]:
    """Deduplicate harvested candidates by canonical `gtin14`, returning `(map, dups_removed)`."""
    by_gtin: dict[str, HarvestedCandidate] = {}
    dups = 0
    for cand in candidates:
        if cand.gtin14 in by_gtin:
            dups += 1
            by_gtin[cand.gtin14] = _merge_candidate(by_gtin[cand.gtin14], cand)
        else:
            by_gtin[cand.gtin14] = cand
    return by_gtin, dups


def reconcile_sources(
    *,
    registry: BrandRegistry,
    obf_result: SourceHarvestResult,
    upc_result: SourceHarvestResult,
    opf_result: SourceHarvestResult | None = None,
    harvested_at: str | None = None,
) -> ReconciliationResult:
    """Reconcile OBF (+ optional OPF) and UPCitemdb on canonical `gtin14` with zero duplicates."""
    ts = harvested_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")

    quarantine_records: list[QuarantineRecord] = [
        *obf_result.quarantined,
        *(opf_result.quarantined if opf_result is not None else []),
        *upc_result.quarantined,
    ]

    # Combine OBF and optional OPF into the Open Facts pool (OBF takes precedence)
    open_facts_candidates = list(obf_result.candidates)
    if opf_result is not None:
        open_facts_candidates.extend(opf_result.candidates)

    obf_by_gtin, obf_dups = _deduplicate_source(open_facts_candidates)
    upc_by_gtin, upc_dups = _deduplicate_source(upc_result.candidates)
    intra_dups = obf_dups + upc_dups

    all_gtins = sorted(set(obf_by_gtin.keys()) | set(upc_by_gtin.keys()))

    products: list[ProductRecord] = []
    cross_source_matches: list[CrossSourceMatch] = []
    parent_subbrand_resolutions: list[ParentSubbrandResolution] = []
    brand_conflicts: list[BrandConflict] = []

    verified_cross_count = 0
    verified_subbrand_count = 0
    verified_single_count = 0
    inferred_prefix_count = 0

    for gtin14 in all_gtins:
        obf = obf_by_gtin.get(gtin14)
        upc = upc_by_gtin.get(gtin14)

        if obf is not None and upc is not None:
            obf_slug = registry.resolve_canonical_slug(obf.brand_slug)
            upc_slug = registry.resolve_canonical_slug(upc.brand_slug)
            obf_title = _clean_name(obf.product_name)
            upc_title = _clean_name(upc.product_name)
            sim = round(
                float(fuzz.token_set_ratio(fold_unicode(obf_title), fold_unicode(upc_title))),
                2,
            )

            status: VerificationStatus
            if obf_slug == upc_slug:
                resolved_slug = obf_slug
                status = "verified_cross_source"
                confidence = 1.0 if sim >= 60.0 else 0.95
                verified_cross_count += 1
            else:
                resolved_sub = registry.resolve_parent_subbrand(obf_slug, upc_slug)
                if resolved_sub is not None:
                    resolved_slug = resolved_sub
                    status = "verified_cross_source_subbrand_resolved"
                    confidence = 0.92
                    verified_subbrand_count += 1
                    parent_subbrand_resolutions.append(
                        ParentSubbrandResolution(
                            gtin14=gtin14,
                            openbeautyfacts_brand_slug=obf_slug,
                            upcitemdb_brand_slug=upc_slug,
                            resolved_brand_slug=resolved_slug,
                            title_similarity=sim,
                        )
                    )
                else:
                    brand_conflicts.append(
                        BrandConflict(
                            gtin14=gtin14,
                            openbeautyfacts_brand_slug=obf_slug,
                            openbeautyfacts_title=obf_title,
                            upcitemdb_brand_slug=upc_slug,
                            upcitemdb_title=upc_title,
                            title_similarity=sim,
                            resolution="quarantined",
                        )
                    )
                    quarantine_records.append(
                        QuarantineRecord(
                            raw_code=obf.raw_codes[0],
                            gtin14=gtin14,
                            reason="CROSS_SOURCE_BRAND_CONFLICT",
                            source="openbeautyfacts+upcitemdb",
                            raw_brand=f"{obf.matched_raw_brand} vs {upc.matched_raw_brand}",
                            raw_title=f"{obf_title} | {upc_title}",
                        )
                    )
                    continue

            brand_entry = registry.get_canonical_entry(resolved_slug)
            if brand_entry is None:
                continue

            canonical_name, alt_names = _choose_canonical_name(
                obf_title,
                upc_title,
                [*obf.alternate_names, *upc.alternate_names],
            )
            if not canonical_name:
                quarantine_records.append(
                    QuarantineRecord(
                        raw_code=obf.raw_codes[0],
                        gtin14=gtin14,
                        reason="MISSING_PRODUCT_NAME",
                        source="openbeautyfacts+upcitemdb",
                        raw_brand=brand_entry.display_name,
                        raw_title=None,
                    )
                )
                continue

            source_names: dict[str, str] = {}
            if obf_title:
                source_names[obf.source] = obf_title
            if upc_title:
                source_names["upcitemdb"] = upc_title

            raw_codes = list(dict.fromkeys([*obf.raw_codes, *upc.raw_codes]))
            matched_raw = list(dict.fromkeys([obf.matched_raw_brand, upc.matched_raw_brand]))
            categories = list(dict.fromkeys([*obf.categories, *upc.categories]))
            image_urls = list(dict.fromkeys([*obf.image_urls, *upc.image_urls]))
            primary_img = (
                obf.primary_image_url
                or upc.primary_image_url
                or (image_urls[0] if image_urls else None)
            )
            thumb_img = obf.thumbnail_url or upc.thumbnail_url or primary_img

            sources_list = list(dict.fromkeys([obf.source, "upcitemdb"]))
            match_methods = {
                obf.source: obf.match_method,
                "upcitemdb": upc.match_method,
            }

            barcode_info = BarcodeInfo.from_normalized(obf.barcode, raw_codes=raw_codes)
            qr_info = build_qr_metadata(
                gtin14=gtin14,
                primary_code=barcode_info.primary_code,
                barcode_type=barcode_info.barcode_type,
                brand_name=brand_entry.display_name,
                product_name=canonical_name,
                image_url=primary_img,
            )

            product_record = ProductRecord(
                gtin14=gtin14,
                product_name=canonical_name,
                image_url=primary_img,
                local_image_path=None,
                brand=BrandInfo(
                    slug=brand_entry.slug,
                    display_name=brand_entry.display_name,
                    glow_and_glare_vendor_url=brand_entry.glow_and_glare_vendor_url,
                    matched_raw_brands=matched_raw,
                ),
                product=ProductDetails(
                    name=canonical_name,
                    source_names=source_names,
                    alternate_names=alt_names,
                    quantity=obf.quantity or upc.quantity,
                    categories=categories,
                    description=obf.description or upc.description,
                    primary_image_url=primary_img,
                    thumbnail_url=thumb_img,
                    image_urls=image_urls,
                    local_image_path=None,
                ),
                barcode=barcode_info,
                qr=qr_info,
                verification=VerificationInfo(
                    status=status,
                    confidence_score=confidence,
                    sources=sources_list,
                    match_methods=match_methods,
                    cross_source_title_similarity=sim,
                ),
                provenance=ProvenanceInfo(
                    openbeautyfacts_url=obf.source_url if obf.source == "openbeautyfacts" else None,
                    openproductsfacts_url=(
                        obf.source_url if obf.source == "openproductsfacts" else None
                    ),
                    upcitemdb_ean=upc.upcitemdb_ean,
                    harvested_at=ts,
                ),
            )
            products.append(product_record)
            cross_source_matches.append(
                CrossSourceMatch(
                    gtin14=gtin14,
                    product_name=canonical_name,
                    resolved_brand_slug=brand_entry.slug,
                    resolved_brand_display_name=brand_entry.display_name,
                    openbeautyfacts_title=obf_title,
                    upcitemdb_title=upc_title,
                    title_similarity=sim,
                    verification_status=status,
                )
            )
        else:
            cand = obf if obf is not None else upc
            assert cand is not None
            resolved_slug = registry.resolve_canonical_slug(cand.brand_slug)
            brand_entry = registry.get_canonical_entry(resolved_slug)
            if brand_entry is None:
                continue

            canonical_name, alt_names = _choose_canonical_name(
                cand.product_name,
                None,
                cand.alternate_names,
            )
            if not canonical_name:
                quarantine_records.append(
                    QuarantineRecord(
                        raw_code=cand.raw_codes[0],
                        gtin14=gtin14,
                        reason="MISSING_PRODUCT_NAME",
                        source=cand.source,
                        raw_brand=cand.matched_raw_brand,
                        raw_title=None,
                    )
                )
                continue

            if cand.match_method == "title_prefix_fallback":
                status = "inferred_title_prefix"
                inferred_prefix_count += 1
            else:
                status = "verified_single_source"
                verified_single_count += 1

            image_urls = list(dict.fromkeys(cand.image_urls))
            primary_img = cand.primary_image_url or (image_urls[0] if image_urls else None)
            thumb_img = cand.thumbnail_url or primary_img

            barcode_info = BarcodeInfo.from_normalized(cand.barcode, raw_codes=cand.raw_codes)
            qr_info = build_qr_metadata(
                gtin14=gtin14,
                primary_code=barcode_info.primary_code,
                barcode_type=barcode_info.barcode_type,
                brand_name=brand_entry.display_name,
                product_name=canonical_name,
                image_url=primary_img,
            )
            products.append(
                ProductRecord(
                    gtin14=gtin14,
                    product_name=canonical_name,
                    image_url=primary_img,
                    local_image_path=None,
                    brand=BrandInfo(
                        slug=brand_entry.slug,
                        display_name=brand_entry.display_name,
                        glow_and_glare_vendor_url=brand_entry.glow_and_glare_vendor_url,
                        matched_raw_brands=[cand.matched_raw_brand],
                    ),
                    product=ProductDetails(
                        name=canonical_name,
                        source_names={cand.source: canonical_name},
                        alternate_names=alt_names,
                        quantity=cand.quantity,
                        categories=list(cand.categories),
                        description=cand.description,
                        primary_image_url=primary_img,
                        thumbnail_url=thumb_img,
                        image_urls=image_urls,
                        local_image_path=None,
                    ),
                    barcode=barcode_info,
                    qr=qr_info,
                    verification=VerificationInfo(
                        status=status,
                        confidence_score=cand.match_confidence,
                        sources=[cand.source],
                        match_methods={cand.source: cand.match_method},
                        cross_source_title_similarity=None,
                    ),
                    provenance=ProvenanceInfo(
                        openbeautyfacts_url=(
                            cand.source_url if cand.source == "openbeautyfacts" else None
                        ),
                        openproductsfacts_url=(
                            cand.source_url if cand.source == "openproductsfacts" else None
                        ),
                        upcitemdb_ean=(cand.upcitemdb_ean if cand.source == "upcitemdb" else None),
                        harvested_at=ts,
                    ),
                )
            )

    products.sort(key=lambda r: (r.brand.slug, r.gtin14))
    unique_gtin_count = len({p.gtin14 for p in products})
    dup_gtin_count = len(products) - unique_gtin_count

    counts_by_reason = default_quarantine_counts()
    for q in quarantine_records:
        if q.reason in counts_by_reason:
            counts_by_reason[q.reason] += 1
        elif q.reason in ALL_QUARANTINE_REASONS:
            counts_by_reason[q.reason] = 1

    comparison_report = ComparisonReport(
        total_unique_products=len(products),
        verified_cross_source_count=verified_cross_count,
        verified_cross_source_subbrand_resolved_count=verified_subbrand_count,
        verified_single_source_count=verified_single_count,
        inferred_title_prefix_count=inferred_prefix_count,
        source_breakdown=SourceBreakdown(
            openbeautyfacts_raw_matched=(
                obf_result.raw_matched_count
                + (opf_result.raw_matched_count if opf_result is not None else 0)
            ),
            openbeautyfacts_unique_valid=len(obf_by_gtin),
            upcitemdb_raw_matched=upc_result.raw_matched_count,
            upcitemdb_unique_valid=len(upc_by_gtin),
            intra_source_duplicates_removed=intra_dups,
            cross_source_exact_gtin_matches=len(cross_source_matches),
        ),
        cross_source_matches=cross_source_matches,
        parent_subbrand_resolutions=parent_subbrand_resolutions,
        brand_conflicts=brand_conflicts,
        duplicate_gtin14_count=dup_gtin_count,
    )

    quarantine_report = QuarantineReport(
        total_quarantined=len(quarantine_records),
        counts_by_reason=counts_by_reason,
        records=quarantine_records,
    )

    return ReconciliationResult(
        products=products,
        comparison_report=comparison_report,
        quarantine_report=quarantine_report,
    )


__all__ = ["ReconciliationResult", "reconcile_sources"]
