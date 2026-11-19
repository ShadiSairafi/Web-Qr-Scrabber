"""4-Tier Brand Normalization, OBF Leaf-Brand Rule, and Risk-Tier Disambiguation Engine."""

from __future__ import annotations

import re
from collections.abc import Sequence

from qr_scrabber.brands.registry import (
    BEAUTY_POSITIVE_KEYWORDS,
    GENERAL_NON_BEAUTY_NEGATIVES,
    OBF_CONGLOMERATE_CONTAINERS,
    PARENT_SUBBRAND_GROUPS,
    BrandRegistry,
    brand_slug_variants,
    compact_brand_key,
    fold_unicode,
    strip_lang_prefix,
)
from qr_scrabber.models import BrandMatch, BrandSeedEntry, QuarantineCandidate

# Parent brand slugs that frequently appear as conglomerate umbrellas in OBF tags
_PARENT_SLUGS: frozenset[str] = frozenset(PARENT_SUBBRAND_GROUPS.keys())

# Multi-word specific aliases for Tier C brands that are unambiguous on their own
# (e.g., "hero cosmetics", "essence cosmetics", "benefit cosmetics", "maison margiela replica")
_SPECIFIC_TIER_C_SLUGS: frozenset[str] = frozenset(
    {
        "about-face-beauty",
        "benefit-cosmetics",
        "duo-lash",
        "duo-adhesive",
        "duo-eyelash-adhesive",
        "ardell-duo",
        "e-l-f-cosmetics",
        "elf-cosmetics",
        "e-l-f-skin",
        "elf-skin",
        "eyes-lips-face",
        "essence-cosmetics",
        "essence-makeup",
        "hero-cosmetics",
        "mighty-patch",
        "k18-hair",
        "k18-biomimetic-hairscience",
        "my-kitsch",
        "mac-cosmetics",
        "m-a-c-cosmetics",
        "one-size-by-patrick-starrr",
        "one-size-beauty",
        "osea-malibu",
        "pixi-beauty",
        "pixi-by-petra",
        "ren-clean-skincare",
        "ren-skincare",
        "maison-margiela",
        "maison-margiela-replica",
        "replica-maison-margiela",
        "maison-martin-margiela",
        "rhode-skin",
        "roc-skincare",
        "saie-beauty",
        "spectrum-collections",
        "spectrum-brushes",
        "my-topicals",
        "versed-skin",
        "versed-skincare",
        "virtue-labs",
        "virtue-hair",
        "wishful-skin",
        "wishful-by-huda-beauty",
        "huda-beauty-wishful",
    }
)


def _split_csv_or_list(value: str | Sequence[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,;|]+", value) if part.strip()]
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _contains_keyword(haystack_folded: str, keywords: Sequence[str]) -> str | None:
    """Match keywords with alphanumeric lookarounds so short tokens never match substrings."""
    for kw in keywords:
        folded_kw = fold_unicode(kw)
        if not folded_kw:
            continue
        pattern = rf"(?<![a-z0-9]){re.escape(folded_kw)}(?![a-z0-9])"
        if re.search(pattern, haystack_folded):
            return kw
    return None


class BrandMatcher:
    """4-tier brand resolver enforcing the OBF Leaf-Brand Rule and Risk-Tier category gates."""

    def __init__(self, registry: BrandRegistry) -> None:
        self.registry = registry
        self._title_prefix_rules: list[tuple[re.Pattern[str], str, str]] = (
            self._compile_title_prefix_rules()
        )
        self._upc_title_prefix_rules: list[tuple[re.Pattern[str], str, str]] = (
            self._compile_upc_title_prefix_rules()
        )

    def _compile_title_prefix_rules(self) -> list[tuple[re.Pattern[str], str, str]]:
        """Compile anchored index-0 title-prefix patterns for Tier A/B brands (OBF empty-brand)."""
        candidates: list[tuple[int, str, str]] = []
        for entry in self.registry.entries:
            if entry.alias_of is not None or entry.is_store_own_brand:
                continue
            if entry.risk_tier == "C":
                continue
            aliases = self.registry.canonical_aliases.get(entry.slug, [entry.display_name])
            for alias in aliases:
                folded = fold_unicode(alias)
                if len(folded) < 4:
                    continue
                candidates.append((len(folded), folded, entry.slug))
        # Longest first so "Charlotte Tilbury" wins over shorter prefixes
        candidates.sort(key=lambda item: (-item[0], item[1]))
        rules: list[tuple[re.Pattern[str], str, str]] = []
        seen: set[tuple[str, str]] = set()
        for _, folded, slug in candidates:
            if (folded, slug) in seen:
                continue
            seen.add((folded, slug))
            pat = re.compile(rf"^{re.escape(folded)}(?![a-z0-9])(?:\s*[-:]|\s+|$)")
            rules.append((pat, slug, folded))
        return rules

    def _compile_upc_title_prefix_rules(self) -> list[tuple[re.Pattern[str], str, str]]:
        """Compile anchored title-prefix rules for UPCitemdb items missing a brand field."""
        candidates: list[tuple[int, str, str]] = []
        for entry in self.registry.entries:
            if entry.alias_of is not None or entry.is_store_own_brand:
                continue
            aliases = self.registry.canonical_aliases.get(entry.slug, [entry.display_name])
            for alias in aliases:
                folded = fold_unicode(alias)
                slug_forms = brand_slug_variants(alias)
                if entry.risk_tier == "C" and not (slug_forms & _SPECIFIC_TIER_C_SLUGS):
                    continue
                if len(folded) < 4:
                    continue
                candidates.append((len(folded), folded, entry.slug))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        rules: list[tuple[re.Pattern[str], str, str]] = []
        seen: set[tuple[str, str]] = set()
        for _, folded, slug in candidates:
            if (folded, slug) in seen:
                continue
            seen.add((folded, slug))
            pat = re.compile(rf"^{re.escape(folded)}(?![a-z0-9])(?:\s*[-:]|\s+|$)")
            rules.append((pat, slug, folded))
        return rules

    def _lookup_tag_slug(self, raw_tag: str) -> tuple[str | None, bool]:
        """Resolve a single brand tag to `(canonical_slug, is_specific_alias)`."""
        stripped = strip_lang_prefix(raw_tag)
        if not stripped:
            return None, False
        variants = brand_slug_variants(stripped)
        for v in variants:
            if v in OBF_CONGLOMERATE_CONTAINERS:
                return None, False
        # Check blocked brand tags globally first
        for _slug, blocked_set in self.registry.blocked_by_slug.items():
            if variants & blocked_set:
                return None, False

        for v in variants:
            target = self.registry.variant_to_canonical.get(v)
            if target is not None:
                is_specific = bool(variants & _SPECIFIC_TIER_C_SLUGS)
                return target, is_specific

        comp = compact_brand_key(stripped)
        if len(comp) >= 5:
            target = self.registry.compact_to_canonical.get(comp)
            if target is not None:
                is_specific = bool(variants & _SPECIFIC_TIER_C_SLUGS)
                return target, is_specific

        return None, False

    def _is_container_tag(self, raw_tag: str) -> bool:
        stripped = strip_lang_prefix(raw_tag)
        if not stripped:
            return True
        variants = brand_slug_variants(stripped)
        return bool(variants & OBF_CONGLOMERATE_CONTAINERS)

    def _check_category_gate(
        self,
        entry: BrandSeedEntry,
        *,
        is_specific_alias: bool,
        context_text: str,
        is_beauty_db: bool,
        raw_brand: str | None,
        raw_title: str | None,
    ) -> QuarantineCandidate | None:
        """Apply Risk-Tier A/B/C positive and negative keyword gates."""
        folded_ctx = fold_unicode(context_text)

        # Always reject unmistakable non-beauty goods (especially in UPCitemdb)
        neg_hit = _contains_keyword(folded_ctx, entry.negative_keywords)
        if neg_hit is not None:
            return QuarantineCandidate(
                candidate_brand_slug=entry.slug,
                reason="CATEGORY_MISMATCH_EXCLUDED",
                raw_brand=raw_brand,
                raw_title=raw_title,
                detail=f"Matched brand negative keyword: {neg_hit}",
            )

        if not is_beauty_db:
            gen_neg = _contains_keyword(folded_ctx, GENERAL_NON_BEAUTY_NEGATIVES)
            if gen_neg is not None:
                return QuarantineCandidate(
                    candidate_brand_slug=entry.slug,
                    reason="CATEGORY_MISMATCH_EXCLUDED",
                    raw_brand=raw_brand,
                    raw_title=raw_title,
                    detail=f"Matched general non-beauty keyword: {gen_neg}",
                )

        if entry.risk_tier == "A":
            return None

        if entry.risk_tier == "B":
            # In OBF (already beauty-scoped), allow unless negative keyword fired above
            if is_beauty_db:
                return None
            # In general retail DB (UPCitemdb), require beauty/fragrance/personal-care signal
            pos_pool = tuple(entry.required_positive_keywords) + BEAUTY_POSITIVE_KEYWORDS
            if _contains_keyword(folded_ctx, pos_pool) is None:
                return QuarantineCandidate(
                    candidate_brand_slug=entry.slug,
                    reason="CATEGORY_MISMATCH_EXCLUDED",
                    raw_brand=raw_brand,
                    raw_title=raw_title,
                    detail="Tier B brand missing positive beauty/fragrance context in general DB",
                )
            return None

        # Tier C: unambiguous multi-word aliases (e.g. "Hero Cosmetics", "Essence Cosmetics") pass
        if is_specific_alias:
            return None

        # Bare Tier C token (e.g. "Hero.", "Essence", "DUO", "MAC", "Replica", "Spectrum")
        pos_keywords = entry.required_positive_keywords or list(BEAUTY_POSITIVE_KEYWORDS)
        if _contains_keyword(folded_ctx, pos_keywords) is None:
            # In OBF (already a beauty database), also allow general beauty signals for
            # beauty-specific cosmetics lines except ultra-ambiguous words
            if is_beauty_db and entry.slug not in {"duo", "hero", "replica", "spectrum"}:
                if _contains_keyword(folded_ctx, BEAUTY_POSITIVE_KEYWORDS) is not None:
                    return None
            return QuarantineCandidate(
                candidate_brand_slug=entry.slug,
                reason="CATEGORY_MISMATCH_EXCLUDED",
                raw_brand=raw_brand,
                raw_title=raw_title,
                detail=f"Tier C bare token for '{entry.slug}' failed positive category gate",
            )
        return None

    def match_obf_record(
        self,
        *,
        brands_tags: str | Sequence[str] | None = None,
        brands_raw: str | Sequence[str] | None = None,
        product_name: str | None = None,
        categories_tags: str | Sequence[str] | None = None,
        categories_raw: str | None = None,
        is_beauty_db: bool = True,
    ) -> BrandMatch | QuarantineCandidate | None:
        """Resolve an Open Beauty Facts / Open Products Facts record via the Leaf-Brand Rule."""
        tags = _split_csv_or_list(brands_tags)
        raw_list = _split_csv_or_list(brands_raw)
        non_container_tags = [t for t in tags if not self._is_container_tag(t)]
        non_container_raw = [t for t in raw_list if not self._is_container_tag(t)]

        cat_parts = _split_csv_or_list(categories_tags)
        cat_clean = " ".join(strip_lang_prefix(c).replace("-", " ") for c in cat_parts)
        context_text = " ".join(
            part
            for part in (
                product_name or "",
                categories_raw or "",
                cat_clean,
                " ".join(raw_list),
            )
            if part
        )

        # 1. Structured OBF `brands_tags` (Leaf-Brand Rule)
        if non_container_tags:
            # Check if any tag is explicitly blocked (e.g. desert-essence)
            resolved_tags: list[tuple[str, str | None, bool]] = [
                (tag, *self._lookup_tag_slug(tag)) for tag in non_container_tags
            ]
            # Rightmost (leaf) tag first
            leaf_raw, leaf_slug, leaf_specific = resolved_tags[-1]
            chosen: tuple[str, str, bool, str] | None = None

            if leaf_slug is not None:
                # Reversed order like [lancome, l-oreal]: prefer specific sub-brand
                if leaf_slug in _PARENT_SLUGS and len(resolved_tags) > 1:
                    for earlier_raw, earlier_slug, earlier_spec in reversed(resolved_tags[:-1]):
                        if (
                            earlier_slug is not None
                            and earlier_slug != leaf_slug
                            and self.registry.resolve_parent_subbrand(leaf_slug, earlier_slug)
                            == earlier_slug
                        ):
                            chosen = (earlier_raw, earlier_slug, earlier_spec, "brands_tags_leaf")
                            break
                if chosen is None:
                    chosen = (leaf_raw, leaf_slug, leaf_specific, "brands_tags_leaf")
            else:
                # Rightmost leaf tag is NOT in our registry!
                # Enforce Leaf-Brand Rule: NEVER fall back to an earlier parent conglomerate tag
                # (e.g. [l-oreal, maybelline] -> must return None, NOT l-oreal!).
                # Only allow earlier tag if it is NOT a parent conglomerate and rightmost looks
                # like a duplicate/locale/product-line suffix of that exact brand.
                for earlier_raw, earlier_slug, earlier_spec in reversed(resolved_tags[:-1]):
                    if earlier_slug is None:
                        continue
                    if earlier_slug in _PARENT_SLUGS:
                        # Blocked conglomerate leak!
                        return None
                    # Only allow if leaf tag starts with or contains the earlier brand's slug
                    leaf_norm = fold_unicode(strip_lang_prefix(leaf_raw))
                    earlier_entry = self.registry.get_canonical_entry(earlier_slug)
                    if (
                        earlier_entry is not None
                        and fold_unicode(earlier_entry.display_name) in leaf_norm
                    ):
                        chosen = (earlier_raw, earlier_slug, earlier_spec, "brands_tags_leaf")
                        break
                if chosen is None:
                    return None

            raw_matched, target_slug, is_specific, method = chosen
            entry = self.registry.get_canonical_entry(target_slug)
            if entry is None or entry.is_store_own_brand:
                return None

            quarantine = self._check_category_gate(
                entry,
                is_specific_alias=is_specific,
                context_text=context_text,
                is_beauty_db=is_beauty_db,
                raw_brand=", ".join(raw_list or tags) or raw_matched,
                raw_title=product_name,
            )
            if quarantine is not None:
                return quarantine

            return BrandMatch(
                brand_slug=entry.slug,
                display_name=entry.display_name,
                glow_and_glare_vendor_url=entry.glow_and_glare_vendor_url,
                matched_raw_brand=raw_matched,
                match_method=method,
                confidence=0.95 if is_beauty_db else 0.90,
            )

        # 2. Raw `brands` / `brands_en` field when `brands_tags` was absent
        if non_container_raw:
            resolved_raw = [(tok, *self._lookup_tag_slug(tok)) for tok in non_container_raw]
            leaf_raw, leaf_slug, leaf_specific = resolved_raw[-1]
            if leaf_slug is None:
                return None
            chosen_raw, target_slug, is_specific = leaf_raw, leaf_slug, leaf_specific
            if leaf_slug in _PARENT_SLUGS and len(resolved_raw) > 1:
                for earlier_raw, earlier_slug, earlier_spec in reversed(resolved_raw[:-1]):
                    if (
                        earlier_slug is not None
                        and earlier_slug != leaf_slug
                        and self.registry.resolve_parent_subbrand(leaf_slug, earlier_slug)
                        == earlier_slug
                    ):
                        chosen_raw, target_slug, is_specific = (
                            earlier_raw,
                            earlier_slug,
                            earlier_spec,
                        )
                        break
            entry = self.registry.get_canonical_entry(target_slug)
            if entry is None or entry.is_store_own_brand:
                return None
            quarantine = self._check_category_gate(
                entry,
                is_specific_alias=is_specific,
                context_text=context_text,
                is_beauty_db=is_beauty_db,
                raw_brand=", ".join(raw_list),
                raw_title=product_name,
            )
            if quarantine is not None:
                return quarantine
            return BrandMatch(
                brand_slug=entry.slug,
                display_name=entry.display_name,
                glow_and_glare_vendor_url=entry.glow_and_glare_vendor_url,
                matched_raw_brand=chosen_raw,
                match_method="brands_raw_leaf",
                confidence=0.90,
            )

        # 3. Empty-Brand Title-Prefix Fallback (OBF beauty rows only; Tier A/B only; index 0)
        if is_beauty_db and product_name and product_name.strip():
            folded_title = fold_unicode(product_name)
            for pattern, target_slug, matched_alias in self._title_prefix_rules:
                if pattern.search(folded_title):
                    entry = self.registry.get_canonical_entry(target_slug)
                    if entry is None or entry.is_store_own_brand:
                        return None
                    quarantine = self._check_category_gate(
                        entry,
                        is_specific_alias=False,
                        context_text=context_text,
                        is_beauty_db=is_beauty_db,
                        raw_brand=None,
                        raw_title=product_name,
                    )
                    if quarantine is not None:
                        return quarantine
                    return BrandMatch(
                        brand_slug=entry.slug,
                        display_name=entry.display_name,
                        glow_and_glare_vendor_url=entry.glow_and_glare_vendor_url,
                        matched_raw_brand=matched_alias,
                        match_method="title_prefix_fallback",
                        confidence=0.65,
                    )

        return None

    def match_upc_item(
        self,
        *,
        brand: str | None = None,
        title: str | None = None,
        category: str | None = None,
        description: str | None = None,
    ) -> BrandMatch | QuarantineCandidate | None:
        """Resolve a UPCitemdb item with strict general-retail category gating."""
        context_text = " ".join(
            part for part in (brand or "", title or "", category or "", description or "") if part
        )
        brand_clean = (brand or "").strip()

        # 1. Direct match on `brand` field
        if brand_clean:
            target_slug, is_specific = self._lookup_tag_slug(brand_clean)
            matched_raw = brand_clean
            method = "exact_brand_field"

            if target_slug is None:
                # Handle noisy brand strings like "Huda Beauty, Cosmetics" or "Dior Parfums"
                for token in _split_csv_or_list(brand_clean):
                    t_slug, t_spec = self._lookup_tag_slug(token)
                    if t_slug is not None:
                        target_slug, is_specific = t_slug, t_spec
                        matched_raw = token
                        method = "brand_field_token"
                        break

            if target_slug is None:
                # Anchored prefix on brand field (e.g. "Fenty Beauty by Rihanna")
                folded_brand = fold_unicode(brand_clean)
                for pattern, cand_slug, cand_alias in self._upc_title_prefix_rules:
                    if pattern.search(folded_brand):
                        target_slug = cand_slug
                        is_specific = bool(brand_slug_variants(cand_alias) & _SPECIFIC_TIER_C_SLUGS)
                        matched_raw = brand_clean
                        method = "brand_field_prefix"
                        break

            if target_slug is not None:
                entry = self.registry.get_canonical_entry(target_slug)
                if entry is not None and not entry.is_store_own_brand:
                    # If brand field is a parent/retailer (e.g. "Sephora" or "L'Oreal") and title
                    # starts with a more specific sub-brand, refine to the specific sub-brand!
                    if entry.slug in _PARENT_SLUGS and title:
                        folded_title = fold_unicode(title)
                        for pattern, sub_slug, _ in self._upc_title_prefix_rules:
                            if sub_slug != entry.slug and pattern.search(folded_title):
                                if (
                                    self.registry.resolve_parent_subbrand(entry.slug, sub_slug)
                                    == sub_slug
                                ):
                                    sub_entry = self.registry.get_canonical_entry(sub_slug)
                                    if sub_entry is not None:
                                        entry = sub_entry
                                        method = "brand_subbrand_refined"
                                        is_specific = True
                                        break

                    quarantine = self._check_category_gate(
                        entry,
                        is_specific_alias=is_specific,
                        context_text=context_text,
                        is_beauty_db=False,
                        raw_brand=brand_clean,
                        raw_title=title,
                    )
                    if quarantine is not None:
                        return quarantine
                    return BrandMatch(
                        brand_slug=entry.slug,
                        display_name=entry.display_name,
                        glow_and_glare_vendor_url=entry.glow_and_glare_vendor_url,
                        matched_raw_brand=matched_raw,
                        match_method=method,
                        confidence=0.92 if method == "exact_brand_field" else 0.85,
                    )

        # 2. Title-prefix fallback when brand field is empty/unknown on UPCitemdb
        if title and title.strip():
            folded_title = fold_unicode(title)
            for pattern, target_slug, matched_alias in self._upc_title_prefix_rules:
                if pattern.search(folded_title):
                    entry = self.registry.get_canonical_entry(target_slug)
                    if entry is None or entry.is_store_own_brand:
                        return None
                    is_specific = bool(brand_slug_variants(matched_alias) & _SPECIFIC_TIER_C_SLUGS)
                    quarantine = self._check_category_gate(
                        entry,
                        is_specific_alias=is_specific,
                        context_text=context_text,
                        is_beauty_db=False,
                        raw_brand=brand_clean or None,
                        raw_title=title,
                    )
                    if quarantine is not None:
                        return quarantine
                    return BrandMatch(
                        brand_slug=entry.slug,
                        display_name=entry.display_name,
                        glow_and_glare_vendor_url=entry.glow_and_glare_vendor_url,
                        matched_raw_brand=matched_alias,
                        match_method="upc_title_prefix",
                        confidence=0.75,
                    )

        return None


__all__ = ["BrandMatcher"]
