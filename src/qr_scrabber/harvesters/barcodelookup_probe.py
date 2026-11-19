"""Source reachability diagnostics including transparent barcodelookup.com gate detection."""

from __future__ import annotations

import httpx

from qr_scrabber.http_client import make_sync_client
from qr_scrabber.models import SourceStatusEntry, SourceStatusMap

OBF_BULK_URL = "https://static.openbeautyfacts.org/data/openbeautyfacts-products.jsonl.gz"
OPF_BULK_URL = "https://static.openproductsfacts.org/data/en.openproductsfacts.org.products.csv.gz"
BARCODELOOKUP_PROBE_URL = "https://www.barcodelookup.com/Huda-Beauty"
DEFAULT_UPC_BASE_URL = "https://api.upcitemdb.com/prod/trial"


def probe_sources(
    *,
    offline: bool = False,
    upc_base_url: str = DEFAULT_UPC_BASE_URL,
    timeout: float = 12.0,
) -> SourceStatusMap:
    """Inspect source availability cleanly without bypassing security gates or wasting API quota."""
    if offline:
        return SourceStatusMap(
            openbeautyfacts=SourceStatusEntry(
                status="offline_cached",
                mode="bulk_dump_local",
                detail="Offline mode active; reads local/cached Open Beauty Facts dump.",
            ),
            openproductsfacts=SourceStatusEntry(
                status="offline_cached",
                mode="bulk_dump_local",
                detail="Offline mode active; optional local Open Products Facts dump.",
            ),
            upcitemdb=SourceStatusEntry(
                status="offline_cached",
                mode="persistent_cache_replay",
                detail="Offline mode active; replays persistent local UPCitemdb response cache.",
            ),
            barcodelookup=SourceStatusEntry(
                status="blocked_cloudflare_challenge",
                http_status=403,
                mode="not_bypassed",
                detail=(
                    "barcodelookup.com brand/search URLs return HTTP 403 Cloudflare Managed "
                    "Challenge (cf-mitigated: challenge); not bypassed."
                ),
            ),
        )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }
    with make_sync_client(follow_redirects=True, timeout=timeout, headers=headers) as client:
        # 1. Open Beauty Facts bulk dump HEAD
        try:
            r_obf = client.head(OBF_BULK_URL)
            obf_entry = SourceStatusEntry(
                status="available" if r_obf.status_code == 200 else "degraded",
                http_status=r_obf.status_code,
                mode="bulk_dump_jsonl_gz",
                detail=f"Content-Length={r_obf.headers.get('content-length', 'unknown')}",
            )
        except httpx.HTTPError as exc:
            obf_entry = SourceStatusEntry(status="unreachable", detail=str(exc))

        # 2. Open Products Facts bulk dump HEAD
        try:
            r_opf = client.head(OPF_BULK_URL)
            opf_entry = SourceStatusEntry(
                status="available" if r_opf.status_code == 200 else "degraded",
                http_status=r_opf.status_code,
                mode="bulk_dump_csv_gz",
                detail=f"Content-Length={r_opf.headers.get('content-length', 'unknown')}",
            )
        except httpx.HTTPError as exc:
            opf_entry = SourceStatusEntry(status="unreachable", detail=str(exc))

        # 3. UPCitemdb reachability via free Swagger spec (0 daily API quota spent!)
        swagger_url = (
            f"{upc_base_url.rstrip('/')}/health"
            if ("127.0.0.1" in upc_base_url or "localhost" in upc_base_url)
            else "https://www.upcitemdb.com/api/upcitemdb-trial.json"
        )
        try:
            r_upc = client.head(swagger_url)
            upc_ok = r_upc.status_code in (200, 304, 405)
            upc_entry = SourceStatusEntry(
                status="available" if upc_ok else "degraded",
                http_status=r_upc.status_code,
                mode="trial_batch_lookup_and_search",
                detail="Zero daily API quota consumed during reachability probe.",
            )
        except httpx.HTTPError as exc:
            upc_entry = SourceStatusEntry(status="unreachable", detail=str(exc))

        # 4. barcodelookup.com transparent gate check (never bypasses Cloudflare challenge)
        try:
            r_bl = client.get(BARCODELOOKUP_PROBE_URL)
            cf_mit = r_bl.headers.get("cf-mitigated", "").lower()
            if (
                r_bl.status_code == 403
                or "challenge" in cf_mit
                or "security verification" in r_bl.text.lower()
            ):
                bl_entry = SourceStatusEntry(
                    status="blocked_cloudflare_challenge",
                    http_status=r_bl.status_code,
                    mode="not_bypassed",
                    detail=(
                        "Detected Cloudflare Managed Challenge (cf-mitigated: challenge); "
                        "site is not bypassed."
                    ),
                )
            else:
                bl_entry = SourceStatusEntry(
                    status="reachable",
                    http_status=r_bl.status_code,
                    mode="probe_only",
                    detail="Responded without Cloudflare challenge.",
                )
        except httpx.HTTPError as exc:
            bl_entry = SourceStatusEntry(
                status="blocked_or_unreachable",
                detail=str(exc),
            )

    return SourceStatusMap(
        openbeautyfacts=obf_entry,
        openproductsfacts=opf_entry,
        upcitemdb=upc_entry,
        barcodelookup=bl_entry,
    )


__all__ = [
    "BARCODELOOKUP_PROBE_URL",
    "OBF_BULK_URL",
    "OPF_BULK_URL",
    "probe_sources",
]
