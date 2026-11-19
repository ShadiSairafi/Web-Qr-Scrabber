(() => {
  "use strict";

  const PAGE_SIZE = 48;
  const GS1_LINK_RE = /(?:https?:\/\/)?id\.gs1\.org\/01\/(\d{8,14})\b/i;
  const SERIAL_CANDIDATE_RE = /^#?\s*[\d\s-]{8,24}\s*$/;

  const els = {
    catalogStats: document.getElementById("catalog-stats"),
    searchInput: document.getElementById("search-input"),
    searchClear: document.getElementById("search-clear"),
    modeIndicator: document.getElementById("search-mode-indicator"),
    regexStatus: document.getElementById("regex-status"),
    resultsCount: document.getElementById("results-count"),
    exactMatchCard: document.getElementById("exact-match-card"),
    resultsGrid: document.getElementById("results-grid"),
    emptyState: document.getElementById("empty-state"),
    emptyResetBtn: document.getElementById("empty-reset-btn"),
    loadMoreBtn: document.getElementById("load-more-btn"),
    sampleSerialChip: document.getElementById("sample-serial-chip"),
    quickHints: document.getElementById("quick-hints"),
    qrModal: document.getElementById("qr-modal"),
    qrModalClose: document.getElementById("qr-modal-close"),
    qrModalBrand: document.getElementById("qr-modal-brand"),
    qrModalTitle: document.getElementById("qr-modal-title"),
    qrModalImg: document.getElementById("qr-modal-img"),
    qrModalPrimary: document.getElementById("qr-modal-primary"),
    qrModalGtin14: document.getElementById("qr-modal-gtin14"),
    qrModalLink: document.getElementById("qr-modal-link"),
    qrModalCopy: document.getElementById("qr-modal-copy"),
    qrModalDownload: document.getElementById("qr-modal-download"),
  };

  let allProducts = [];
  let defaultOrder = [];
  const serialIndex = new Map();
  let currentMatches = [];
  let renderedCount = 0;
  let activeHighlightRe = null;
  let activeModalProduct = null;
  let searchDebounceTimer = null;

  function escapeHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function makePlaceholderDataUri(brandName, productName) {
    const letters = (brandName || productName || "GG")
      .trim()
      .split(/\s+/)
      .slice(0, 2)
      .map((w) => w[0] || "")
      .join("")
      .toUpperCase() || "GG";
    const safeBrand = escapeHtml((brandName || "Glow & Glare").slice(0, 24));
    const svg =
      `<svg xmlns="http://www.w3.org/2000/svg" width="320" height="320" viewBox="0 0 320 320">` +
      `<rect width="320" height="320" rx="20" fill="#F2EDE6"/>` +
      `<circle cx="160" cy="138" r="54" fill="#E6DDD2"/>` +
      `<text x="160" y="153" text-anchor="middle" font-family="Georgia, serif" font-size="40" fill="#6F5648">${escapeHtml(letters)}</text>` +
      `<text x="160" y="232" text-anchor="middle" font-family="sans-serif" font-size="15" fill="#7D6F66">${safeBrand}</text>` +
      `</svg>`;
    return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  }

  function getQrSrc(product) {
    if (product?.qr?.qr_svg_path) {
      return `./${product.qr.qr_svg_path}`;
    }
    return `/api/qr/${product.gtin14}.svg`;
  }

  function getImageCandidates(product) {
    const candidates = [];
    const localPath = product?.local_image_path || product?.product?.local_image_path;
    if (localPath) {
      candidates.push(`./${localPath}`);
    }
    const remotePrimary = product?.image_url || product?.product?.primary_image_url;
    if (remotePrimary && /^https?:\/\//i.test(remotePrimary)) {
      candidates.push(remotePrimary);
    }
    const remoteThumb = product?.product?.thumbnail_url;
    if (remoteThumb && /^https?:\/\//i.test(remoteThumb) && !candidates.includes(remoteThumb)) {
      candidates.push(remoteThumb);
    }
    candidates.push(makePlaceholderDataUri(product?.brand?.display_name, product?.product_name));
    return candidates;
  }

  function attachSafeImageFallback(imgEl, product) {
    const candidates = getImageCandidates(product);
    let idx = 0;
    imgEl.src = candidates[0];
    imgEl.addEventListener("error", () => {
      idx += 1;
      if (idx < candidates.length) {
        imgEl.src = candidates[idx];
      }
    });
  }

  function formatVerificationBadge(status) {
    if (status === "verified_cross_source") {
      return { text: "Cross-Source Verified", cls: "badge-cross" };
    }
    if (status === "verified_cross_source_subbrand_resolved") {
      return { text: "Cross-Source (Sub-Brand)", cls: "badge-cross" };
    }
    if (status === "inferred_title_prefix") {
      return { text: "Title-Matched", cls: "badge-inferred" };
    }
    return { text: "Verified", cls: "badge-single" };
  }

  function highlightText(text, regex) {
    const safe = escapeHtml(text);
    if (!regex) return safe;
    try {
      const globalRe = new RegExp(regex.source, "gi");
      let out = "";
      let lastIndex = 0;
      let match;
      let safety = 0;
      while ((match = globalRe.exec(text)) !== null && safety < 24) {
        safety += 1;
        if (match[0].length === 0) {
          globalRe.lastIndex += 1;
          continue;
        }
        out += escapeHtml(text.slice(lastIndex, match.index));
        out += `<mark>${escapeHtml(match[0])}</mark>`;
        lastIndex = match.index + match[0].length;
      }
      out += escapeHtml(text.slice(lastIndex));
      return out || safe;
    } catch {
      return safe;
    }
  }

  async function copyToClipboard(text, btnEl) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
      }
    } catch {
      // Clipboard may be restricted in headless contexts; still provide UI feedback
    }
    if (btnEl) {
      const original = btnEl.dataset.originalLabel || btnEl.textContent;
      btnEl.dataset.originalLabel = original;
      btnEl.textContent = "Copied";
      btnEl.classList.add("copied");
      window.setTimeout(() => {
        btnEl.textContent = original;
        btnEl.classList.remove("copied");
      }, 1400);
    }
  }

  function openQrModal(product) {
    activeModalProduct = product;
    const qrSrc = getQrSrc(product);
    const primarySerial = product?.barcode?.primary_code || product.gtin14;
    els.qrModalBrand.textContent = product?.brand?.display_name || "";
    els.qrModalTitle.textContent = product?.product_name || product?.product?.name || "";
    els.qrModalImg.src = qrSrc;
    els.qrModalImg.alt = `QR code for ${product.product_name} (${product.gtin14})`;
    els.qrModalPrimary.textContent = primarySerial;
    els.qrModalGtin14.textContent = product.gtin14;
    els.qrModalLink.textContent = product?.qr?.gs1_digital_link || `https://id.gs1.org/01/${product.gtin14}`;
    els.qrModalDownload.href = qrSrc;
    els.qrModalDownload.setAttribute("download", `${product.gtin14}.svg`);

    if (typeof els.qrModal.showModal === "function") {
      if (!els.qrModal.open) {
        els.qrModal.showModal();
      }
    } else {
      els.qrModal.setAttribute("open", "open");
    }
  }

  function closeQrModal() {
    if (typeof els.qrModal.close === "function" && els.qrModal.open) {
      els.qrModal.close();
    } else {
      els.qrModal.removeAttribute("open");
    }
  }

  function buildSearchHaystack(p) {
    const parts = [
      p.product_name,
      p.brand?.display_name,
      p.brand?.slug,
      ...(p.product?.alternate_names || []),
      p.product?.quantity,
      ...(p.product?.categories || []),
      p.gtin14,
      p.barcode?.primary_code,
      p.barcode?.ean13,
      p.barcode?.upc_a,
      p.barcode?.ean8,
      p.qr?.gs1_digital_link,
    ];
    return parts.filter(Boolean).join(" \n ");
  }

  function indexProducts(products) {
    serialIndex.clear();
    for (const p of products) {
      p._haystack = buildSearchHaystack(p);
      p._haystackLower = p._haystack.toLowerCase();

      const keys = new Set();
      const addDigits = (val) => {
        if (!val) return;
        const d = String(val).replace(/\D/g, "");
        if (d.length >= 8 && d.length <= 14) {
          keys.add(d);
          keys.add(d.padStart(14, "0"));
        }
      };
      addDigits(p.gtin14);
      addDigits(p.barcode?.primary_code);
      addDigits(p.barcode?.ean13);
      addDigits(p.barcode?.upc_a);
      addDigits(p.barcode?.ean8);
      if (Array.isArray(p.barcode?.raw_codes)) {
        for (const rc of p.barcode.raw_codes) {
          addDigits(rc);
        }
      }
      for (const k of keys) {
        if (!serialIndex.has(k)) {
          serialIndex.set(k, p);
        }
      }
    }

    // Default gallery order: prioritize cross-source verified & items with pictures
    defaultOrder = [...products].sort((a, b) => {
      const score = (item) => {
        let s = 0;
        if (
          item.verification?.status === "verified_cross_source" ||
          item.verification?.status === "verified_cross_source_subbrand_resolved"
        ) {
          s += 4;
        }
        if (item.local_image_path) s += 2;
        else if (item.image_url) s += 1;
        return s;
      };
      const diff = score(b) - score(a);
      if (diff !== 0) return diff;
      return `${a.brand?.slug || ""}:${a.gtin14}`.localeCompare(`${b.brand?.slug || ""}:${b.gtin14}`);
    });
  }

  function extractExactSerialMatch(query) {
    const trimmed = query.trim();
    if (!trimmed) return null;

    const gs1Match = trimmed.match(GS1_LINK_RE);
    if (gs1Match) {
      const digits = gs1Match[1];
      return serialIndex.get(digits) || serialIndex.get(digits.padStart(14, "0")) || null;
    }

    if (SERIAL_CANDIDATE_RE.test(trimmed)) {
      const digits = trimmed.replace(/\D/g, "");
      if (digits.length >= 8 && digits.length <= 14) {
        return serialIndex.get(digits) || serialIndex.get(digits.padStart(14, "0")) || null;
      }
    }

    return null;
  }

  function renderExactMatchCard(product) {
    if (!product) {
      els.exactMatchCard.hidden = true;
      els.exactMatchCard.innerHTML = "";
      return;
    }

    const badge = formatVerificationBadge(product.verification?.status);
    const primaryCode = product.barcode?.primary_code || product.gtin14;
    const barcodeType = product.barcode?.barcode_type || "GTIN-14";
    const qrSrc = getQrSrc(product);
    const sourcesText = (product.verification?.sources || []).join(" + ");

    const serialTiles = [
      { label: `${barcodeType} Serial`, value: primaryCode },
      { label: "GTIN-14", value: product.gtin14 },
    ];
    if (product.barcode?.ean13 && product.barcode.ean13 !== primaryCode) {
      serialTiles.push({ label: "EAN-13", value: product.barcode.ean13 });
    }
    if (product.barcode?.upc_a && product.barcode.upc_a !== primaryCode) {
      serialTiles.push({ label: "UPC-A", value: product.barcode.upc_a });
    }
    if (product.barcode?.ean8 && product.barcode.ean8 !== primaryCode) {
      serialTiles.push({ label: "EAN-8", value: product.barcode.ean8 });
    }

    els.exactMatchCard.innerHTML = `
      <div class="exact-layout">
        <div class="exact-photo-stage">
          <img class="exact-product-img" alt="${escapeHtml(product.product_name)}">
        </div>
        <div class="exact-details">
          <div class="exact-eyebrow">
            <span class="verification-badge ${badge.cls}">${escapeHtml(badge.text)}</span>
            ${sourcesText ? `<span class="mode-pill mode-browse">${escapeHtml(sourcesText)}</span>` : ""}
            ${product.product?.quantity ? `<span class="mode-pill mode-browse">${escapeHtml(product.product.quantity)}</span>` : ""}
          </div>
          <h2 class="exact-title">${escapeHtml(product.product_name)}</h2>
          <p class="exact-brand">${escapeHtml(product.brand?.display_name || "")}</p>
          <div class="serial-grid">
            ${serialTiles
              .map(
                (t) => `
              <div class="serial-tile">
                <span class="serial-tile-label">${escapeHtml(t.label)}</span>
                <span class="serial-tile-value">${escapeHtml(t.value)}</span>
              </div>`
              )
              .join("")}
          </div>
          <div class="exact-actions">
            <button type="button" class="primary-btn exact-copy-serial">Copy ${escapeHtml(primaryCode)}</button>
            <button type="button" class="secondary-btn exact-copy-link">Copy GS1 Digital Link</button>
          </div>
        </div>
        <div class="exact-qr-panel">
          <button type="button" class="exact-qr-btn" aria-label="Enlarge QR code">
            <img class="exact-qr-img" src="${escapeHtml(qrSrc)}" alt="Scannable QR code for ${escapeHtml(product.gtin14)}">
          </button>
          <span class="exact-qr-caption">Click QR to enlarge</span>
        </div>
      </div>
    `;

    const exactImg = els.exactMatchCard.querySelector(".exact-product-img");
    attachSafeImageFallback(exactImg, product);

    const copySerialBtn = els.exactMatchCard.querySelector(".exact-copy-serial");
    copySerialBtn.addEventListener("click", () => copyToClipboard(primaryCode, copySerialBtn));

    const copyLinkBtn = els.exactMatchCard.querySelector(".exact-copy-link");
    copyLinkBtn.addEventListener("click", () =>
      copyToClipboard(product.qr?.gs1_digital_link || `https://id.gs1.org/01/${product.gtin14}`, copyLinkBtn)
    );

    const qrBtn = els.exactMatchCard.querySelector(".exact-qr-btn");
    qrBtn.addEventListener("click", () => openQrModal(product));

    els.exactMatchCard.hidden = false;
  }

  function createProductCard(product, highlightRe) {
    const article = document.createElement("article");
    article.className = "product-card";
    article.dataset.gtin14 = product.gtin14;

    const badge = formatVerificationBadge(product.verification?.status);
    const primaryCode = product.barcode?.primary_code || product.gtin14;
    const barcodeType = product.barcode?.barcode_type || "GTIN-14";
    const qrSrc = getQrSrc(product);
    const submetaParts = [
      product.product?.quantity,
      (product.verification?.sources || []).join(" + "),
    ].filter(Boolean);

    article.innerHTML = `
      <div class="card-media">
        <span class="verification-badge ${badge.cls}">${escapeHtml(badge.text)}</span>
        <img class="product-image" loading="lazy" alt="${escapeHtml(product.product_name)}">
      </div>
      <div class="card-body">
        <p class="product-brand">${highlightText(product.brand?.display_name || "", highlightRe)}</p>
        <h3 class="product-title">${highlightText(product.product_name || "", highlightRe)}</h3>
        ${submetaParts.length ? `<p class="product-submeta">${escapeHtml(submetaParts.join(" · "))}</p>` : ""}
      </div>
      <div class="card-footer">
        <div class="serial-block">
          <span class="serial-type-label">${escapeHtml(barcodeType)} · GTIN-14 ${escapeHtml(product.gtin14)}</span>
          <div class="serial-row">
            <code class="product-serial">${escapeHtml(primaryCode)}</code>
            <button type="button" class="copy-serial-btn" aria-label="Copy serial ${escapeHtml(primaryCode)}">Copy</button>
          </div>
        </div>
        <button type="button" class="qr-thumb-btn" aria-label="Enlarge QR code for ${escapeHtml(product.product_name)}">
          <img class="product-qr" loading="lazy" src="${escapeHtml(qrSrc)}" alt="QR code ${escapeHtml(product.gtin14)}">
        </button>
      </div>
    `;

    const imgEl = article.querySelector(".product-image");
    attachSafeImageFallback(imgEl, product);

    const copyBtn = article.querySelector(".copy-serial-btn");
    copyBtn.addEventListener("click", () => copyToClipboard(primaryCode, copyBtn));

    const qrBtn = article.querySelector(".qr-thumb-btn");
    qrBtn.addEventListener("click", () => openQrModal(product));

    return article;
  }

  function renderNextPage() {
    const slice = currentMatches.slice(renderedCount, renderedCount + PAGE_SIZE);
    const frag = document.createDocumentFragment();
    for (const p of slice) {
      frag.appendChild(createProductCard(p, activeHighlightRe));
    }
    els.resultsGrid.appendChild(frag);
    renderedCount += slice.length;

    const remaining = currentMatches.length - renderedCount;
    if (remaining > 0) {
      els.loadMoreBtn.hidden = false;
      els.loadMoreBtn.textContent = `Load more (${remaining.toLocaleString()} remaining)`;
    } else {
      els.loadMoreBtn.hidden = true;
    }
  }

  function setMode(modeLabel, modeClass, statusText) {
    els.modeIndicator.textContent = modeLabel;
    els.modeIndicator.className = `mode-pill ${modeClass}`;
    els.regexStatus.textContent = statusText;
  }

  function runSearch(rawQuery) {
    const query = (rawQuery || "").trim();
    els.searchClear.hidden = query.length === 0;
    els.resultsGrid.innerHTML = "";
    renderedCount = 0;
    activeHighlightRe = null;

    if (!query) {
      renderExactMatchCard(null);
      currentMatches = defaultOrder;
      setMode("Browsing Catalog", "mode-browse", "Ready for serial or regex search");
      els.resultsCount.textContent = `${currentMatches.length.toLocaleString()} product${currentMatches.length === 1 ? "" : "s"}`;
      els.emptyState.hidden = currentMatches.length > 0;
      renderNextPage();
      return;
    }

    // 1. Exact QR/Barcode Serial or GS1 Digital Link match
    const exact = extractExactSerialMatch(query);
    if (exact) {
      renderExactMatchCard(exact);
      currentMatches = [];
      setMode("Exact Serial Match", "mode-exact", `Matched GTIN-14 ${exact.gtin14}`);
      els.resultsCount.textContent = "1 exact match";
      els.emptyState.hidden = true;
      els.loadMoreBtn.hidden = true;
      return;
    }

    renderExactMatchCard(null);

    // 2. Case-insensitive Regex Search with graceful literal fallback
    let compiledRe = null;
    try {
      compiledRe = new RegExp(query, "i");
    } catch {
      compiledRe = null;
    }

    if (compiledRe) {
      activeHighlightRe = compiledRe;
      currentMatches = defaultOrder.filter((p) => compiledRe.test(p._haystack));
      setMode("Regex Search", "mode-regex", `Pattern /${query}/i`);
    } else {
      const needle = query.toLowerCase();
      currentMatches = defaultOrder.filter((p) => p._haystackLower.includes(needle));
      setMode("Literal Fallback", "mode-fallback", "Literal fallback (incomplete regex)");
    }

    els.resultsCount.textContent = `${currentMatches.length.toLocaleString()} matching product${currentMatches.length === 1 ? "" : "s"}`;
    els.emptyState.hidden = currentMatches.length > 0;
    if (currentMatches.length > 0) {
      renderNextPage();
    } else {
      els.loadMoreBtn.hidden = true;
    }
  }

  function bindEvents() {
    els.searchInput.addEventListener("input", () => {
      const val = els.searchInput.value;
      window.clearTimeout(searchDebounceTimer);
      // Instant response for serials or short inputs; gentle 50ms debounce for typing regexes
      if (!val.trim() || SERIAL_CANDIDATE_RE.test(val.trim()) || GS1_LINK_RE.test(val.trim())) {
        runSearch(val);
      } else {
        searchDebounceTimer = window.setTimeout(() => runSearch(val), 50);
      }
    });

    els.searchInput.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        window.clearTimeout(searchDebounceTimer);
        runSearch(els.searchInput.value);
      } else if (ev.key === "Escape") {
        els.searchInput.value = "";
        runSearch("");
      }
    });

    els.searchClear.addEventListener("click", () => {
      els.searchInput.value = "";
      runSearch("");
      els.searchInput.focus();
    });

    els.emptyResetBtn.addEventListener("click", () => {
      els.searchInput.value = "";
      runSearch("");
      els.searchInput.focus();
    });

    els.loadMoreBtn.addEventListener("click", () => {
      renderNextPage();
    });

    els.quickHints.addEventListener("click", (ev) => {
      const btn = ev.target.closest(".hint-chip");
      if (!btn || !btn.dataset.query) return;
      els.searchInput.value = btn.dataset.query;
      runSearch(btn.dataset.query);
      els.searchInput.focus();
    });

    els.qrModalClose.addEventListener("click", closeQrModal);
    els.qrModal.addEventListener("click", (ev) => {
      if (ev.target === els.qrModal) {
        closeQrModal();
      }
    });
    els.qrModalCopy.addEventListener("click", () => {
      if (!activeModalProduct) return;
      const serial = activeModalProduct.barcode?.primary_code || activeModalProduct.gtin14;
      copyToClipboard(serial, els.qrModalCopy);
    });
  }

  async function loadCatalogChunked() {
    const CHUNK_SIZE = 700;
    const firstResp = await fetch(`./api/catalog?offset=0&limit=${CHUNK_SIZE}`);
    if (!firstResp.ok) {
      throw new Error(`Failed to load /api/catalog (HTTP ${firstResp.status})`);
    }
    const firstPage = await firstResp.json();
    const total = Number(firstPage.total || 0);
    const items = Array.isArray(firstPage.items) ? [...firstPage.items] : [];

    if (total > items.length) {
      const offsets = [];
      for (let off = CHUNK_SIZE; off < total; off += CHUNK_SIZE) {
        offsets.push(off);
      }
      const pages = await Promise.all(
        offsets.map(async (off) => {
          const resp = await fetch(`./api/catalog?offset=${off}&limit=${CHUNK_SIZE}`);
          if (!resp.ok) {
            throw new Error(`Failed to load catalog chunk at offset ${off} (HTTP ${resp.status})`);
          }
          const data = await resp.json();
          return Array.isArray(data.items) ? data.items : [];
        })
      );
      for (const pageItems of pages) {
        items.push(...pageItems);
      }
    }
    return items;
  }

  async function init() {
    bindEvents();
    try {
      const [summaryResp, healthResp] = await Promise.all([
        fetch("./summary.json").catch(() => null),
        fetch("./api/health").catch(() => null),
      ]);
      let summary = null;
      if (summaryResp && summaryResp.ok) {
        summary = await summaryResp.json();
      }

      let hasChunkedApi = false;
      if (healthResp && healthResp.ok) {
        try {
          const health = await healthResp.json();
          hasChunkedApi = Boolean(health && health.status === "ok");
        } catch {
          hasChunkedApi = false;
        }
      }

      if (hasChunkedApi) {
        allProducts = await loadCatalogChunked();
      } else {
        const productsResp = await fetch("./all_products.json");
        if (!productsResp.ok) {
          throw new Error(`Failed to load all_products.json (HTTP ${productsResp.status})`);
        }
        allProducts = await productsResp.json();
      }

      indexProducts(allProducts);

      const brandCount = summary?.total_brands_in_directory ?? 163;
      const crossCount = summary?.cross_source_verified_count ?? 0;
      const statsBits = [
        `${brandCount} Brands`,
        `${allProducts.length.toLocaleString()} Verified Products`,
      ];
      if (crossCount > 0) {
        statsBits.push(`${crossCount.toLocaleString()} Cross-Verified`);
      }
      els.catalogStats.textContent = statsBits.join(" · ");

      if (defaultOrder.length > 0 && els.sampleSerialChip) {
        const sample = defaultOrder[0];
        const sampleSerial = sample.barcode?.primary_code || sample.gtin14;
        els.sampleSerialChip.dataset.query = sampleSerial;
        els.sampleSerialChip.textContent = sampleSerial;
      } else if (els.sampleSerialChip) {
        els.sampleSerialChip.hidden = true;
      }

      const initialQuery = new URLSearchParams(window.location.search).get("q") || "";
      if (initialQuery) {
        els.searchInput.value = initialQuery;
      }
      runSearch(initialQuery);
    } catch (err) {
      els.catalogStats.textContent = "Catalog unavailable";
      setMode("Load Error", "mode-fallback", String(err.message || err));
      els.emptyState.hidden = false;
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
