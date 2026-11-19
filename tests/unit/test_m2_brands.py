"""Unit tests for Milestone 2: Glow & Glare brand extraction, registry, and 4-tier matcher."""

from __future__ import annotations

from pathlib import Path

import pytest

from qr_scrabber.brands import (
    BrandMatcher,
    BrandRegistry,
    canonical_brand_slug,
    extract_brands_from_html,
    load_brands_seed,
)
from qr_scrabber.models import BrandMatch, QuarantineCandidate

SEED_PATH = Path("data/brands_seed.json")


@pytest.fixture(scope="module")
def registry() -> BrandRegistry:
    return BrandRegistry.from_entries(load_brands_seed(SEED_PATH))


@pytest.fixture(scope="module")
def matcher(registry: BrandRegistry) -> BrandMatcher:
    return BrandMatcher(registry)


def test_seed_inventory_counts(registry: BrandRegistry) -> None:
    assert registry.total_vendor_entries == 163
    assert registry.distinct_brand_entities == 161
    assert registry.external_brand_entities == 160
    assert registry.most_loved_count == 12


def test_seed_aliases_and_store_own_brand(registry: BrandRegistry) -> None:
    assert registry.resolve_canonical_slug("ren") == "ren-clean-skincare"
    assert registry.resolve_canonical_slug("sweet-chief") == "sweet-chef"
    gg = registry.get_entry("glow-and-glare")
    assert gg is not None and gg.is_store_own_brand is True


def test_html_extraction_preserves_unescaped_apostrophes_and_most_loved() -> None:
    sample_html = """
    <html><body><main>
      <div class="ggb">
        <div class="t">
          <a href="/collections/vendors?q=Huda%20Beauty">Huda Beauty</a>
        </div>
        <div id="bH">
          <a href="/collections/vendors?q=Huda%20Beauty">Huda Beauty</a>
          <a href="/collections/vendors?q=Hero.">Hero.</a>
        </div>
        <div id="bJ">
          <a href="/collections/vendors?q=Juvia's%20Place">Juvia's Place</a>
        </div>
        <div id="bK">
          <a href="/collections/vendors?q=Kiehl's">Kiehl's</a>
        </div>
        <div id="bL">
          <a href="/collections/vendors?q=L'Oreal">L'Oreal</a>
        </div>
        <div id="bP">
          <a href="/collections/vendors?q=Paula's%20Choice">Paula's Choice</a>
        </div>
        <div id="bD">
          <a href="/collections/vendors?q=Dr.%20Jart%2B">Dr. Jart+</a>
        </div>
      </div>
    </main></body></html>
    """
    entries = extract_brands_from_html(sample_html)
    by_slug = {e.slug: e for e in entries}
    assert set(by_slug.keys()) == {
        "huda-beauty",
        "hero",
        "juvias-place",
        "kiehls",
        "loreal",
        "paulas-choice",
        "dr-jart-plus",
    }
    assert by_slug["huda-beauty"].is_most_loved is True
    assert by_slug["loreal"].display_name == "L'Oreal"
    assert by_slug["kiehls"].display_name == "Kiehl's"
    assert by_slug["juvias-place"].display_name == "Juvia's Place"
    assert by_slug["paulas-choice"].display_name == "Paula's Choice"
    assert by_slug["dr-jart-plus"].display_name == "Dr. Jart+"


def test_diacritic_and_punctuation_slug_normalization() -> None:
    assert canonical_brand_slug("CHĀMPO") == "champo"
    assert canonical_brand_slug("ODACITÉ") == "odacite"
    assert canonical_brand_slug("Estée Lauder") == "estee-lauder"
    assert canonical_brand_slug("Kérastase") == "kerastase"
    assert canonical_brand_slug("Lancôme") == "lancome"
    assert canonical_brand_slug("Chloé") == "chloe"
    assert canonical_brand_slug("e.l.f") == "elf"
    assert canonical_brand_slug("Dr. Jart+") == "dr-jart-plus"
    assert canonical_brand_slug("ONE/SIZE") == "one-size"
    assert canonical_brand_slug("Fable & Mane") == "fable-and-mane"
    assert canonical_brand_slug("r.e.m. beauty") == "rem-beauty"


def test_obf_leaf_brand_rule_blocks_conglomerate_leaks(matcher: BrandMatcher) -> None:
    # Maybelline is NOT in Glow & Glare; must NOT leak into L'Oreal!
    res = matcher.match_obf_record(
        brands_tags=["xx:l-oreal", "xx:l-oreal-consumer-products", "xx:maybelline"],
        brands_raw="L'Oréal,L'Oreal Consumer products,Maybelline",
        product_name="Maybelline Define A Lash Mascara Very Black",
    )
    assert res is None

    # Lancôme IS in Glow & Glare; must resolve to lancome, NOT loreal!
    res2 = matcher.match_obf_record(
        brands_tags=["xx:l-oreal", "xx:l-oreal-luxe", "xx:lancome"],
        brands_raw="L'Oréal,L'Oreal Luxe,Lancôme",
        product_name="Lancôme La Vie Est Belle Eau de Parfum",
    )
    assert isinstance(res2, BrandMatch)
    assert res2.brand_slug == "lancome"

    # Reversed contributor order [lancome, l-oreal] still prefers sub-brand lancome!
    res3 = matcher.match_obf_record(
        brands_tags=["xx:lancome", "xx:l-oreal"],
        product_name="Lancôme Hydra Zen",
    )
    assert isinstance(res3, BrandMatch)
    assert res3.brand_slug == "lancome"

    # Genuine L'Oréal Paris resolves to loreal!
    res4 = matcher.match_obf_record(
        brands_tags=["xx:l-oreal", "xx:l-oreal-paris"],
        product_name="L'Oréal Paris Revitalift Serum",
    )
    assert isinstance(res4, BrandMatch)
    assert res4.brand_slug == "loreal"


def test_blocked_brand_tags_rejected(matcher: BrandMatcher) -> None:
    assert (
        matcher.match_obf_record(brands_tags=["xx:desert-essence"], product_name="Tea Tree Wash")
        is None
    )
    assert (
        matcher.match_obf_record(brands_tags=["xx:herbal-essences"], product_name="Shampoo") is None
    )
    assert (
        matcher.match_obf_record(brands_tags=["xx:valentino-rudy"], product_name="Parfum") is None
    )


def test_obf_empty_brand_title_prefix_fallback(matcher: BrandMatcher) -> None:
    # Glossier Body Hero matches Glossier (index 0), NOT Hero.
    res = matcher.match_obf_record(
        brands_tags=[],
        brands_raw="",
        product_name="Glossier Body Hero Daily Perfecting Cream",
    )
    assert isinstance(res, BrandMatch)
    assert res.brand_slug == "glossier"
    assert res.match_method == "title_prefix_fallback"

    # Tier C bare common words NEVER match empty-brand title fallback
    res2 = matcher.match_obf_record(
        brands_tags=[],
        brands_raw="",
        product_name="Hero Baby Fruit Puree",
    )
    assert res2 is None


def test_alias_resolution_unifies_ren_and_sweet_chief(matcher: BrandMatcher) -> None:
    r1 = matcher.match_obf_record(
        brands_tags=["xx:ren"],
        product_name="REN Ready Steady Glow Daily AHA Tonic Skincare",
    )
    assert isinstance(r1, BrandMatch)
    assert r1.brand_slug == "ren-clean-skincare"

    r2 = matcher.match_obf_record(
        brands_tags=["xx:sweet-chief"],
        product_name="Sweet Chef Ginger Vitamin C Serum Shot",
    )
    assert isinstance(r2, BrandMatch)
    assert r2.brand_slug == "sweet-chef"


def test_upc_tier_b_and_c_category_gates(matcher: BrandMatcher) -> None:
    # Tier B luxury eyewear -> quarantined
    bad_dior = matcher.match_upc_item(
        brand="Dior",
        title="Dior Black Acetate Sunglasses 54mm",
        category="Apparel & Accessories > Sunglasses",
    )
    assert isinstance(bad_dior, QuarantineCandidate)
    assert bad_dior.reason == "CATEGORY_MISMATCH_EXCLUDED"

    # Tier B luxury fragrance -> accepted
    good_dior = matcher.match_upc_item(
        brand="Christian Dior",
        title="Dior Sauvage Eau de Parfum Spray 100ml",
        category="Health & Beauty > Fragrance",
    )
    assert isinstance(good_dior, BrandMatch)
    assert good_dior.brand_slug == "dior"

    # Tier C MAC computer -> quarantined; MAC lipstick -> accepted
    bad_mac = matcher.match_upc_item(
        brand="MAC",
        title="MacBook Pro 14-inch Laptop Charger",
        category="Electronics > Computers",
    )
    assert isinstance(bad_mac, QuarantineCandidate)

    good_mac = matcher.match_upc_item(
        brand="MAC",
        title="MAC Matte Lipstick Ruby Woo 3g",
        category="Health & Beauty > Cosmetics > Makeup > Lip Makeup",
    )
    assert isinstance(good_mac, BrandMatch)
    assert good_mac.brand_slug == "mac-cosmetics"

    # Tier C Replica toy vs Maison Margiela Replica perfume
    bad_rep = matcher.match_upc_item(
        brand="Replica",
        title="Replica 1:18 Scale Diecast Metal Car Toy",
        category="Toys & Games",
    )
    assert isinstance(bad_rep, QuarantineCandidate)

    good_rep = matcher.match_upc_item(
        brand="Maison Margiela",
        title="REPLICA By the Fireplace Eau de Toilette 100ml",
        category="Health & Beauty > Fragrance",
    )
    assert isinstance(good_rep, BrandMatch)
    assert good_rep.brand_slug == "replica"

    # Tier C Spectrum cable vs Spectrum makeup brush
    bad_spec = matcher.match_upc_item(
        brand="Spectrum",
        title="Spectrum High Speed HDMI Cable 6ft",
        category="Electronics",
    )
    assert isinstance(bad_spec, QuarantineCandidate)

    good_spec = matcher.match_upc_item(
        brand="Spectrum Collections",
        title="10 Piece Essential Makeup Brush Set",
        category="Health & Beauty > Cosmetics > Brushes",
    )
    assert isinstance(good_spec, BrandMatch)
    assert good_spec.brand_slug == "spectrum"

    # Valentino hot sauce -> quarantined
    bad_val = matcher.match_upc_item(
        brand="Valentino",
        title="Valentino Mexican Hot Sauce 12 oz",
        category="Food & Beverages",
    )
    assert isinstance(bad_val, QuarantineCandidate)
