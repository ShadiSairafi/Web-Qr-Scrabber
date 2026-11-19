"""External barcode, product image, and source reachability harvesters."""

from qr_scrabber.harvesters.barcodelookup_probe import probe_sources
from qr_scrabber.harvesters.cache import (
    BulkDownloadResult,
    PersistentResponseCache,
    atomic_write_json,
    download_bulk_file,
)
from qr_scrabber.harvesters.images import ProductImageDownloader, normalize_image_to_jpeg
from qr_scrabber.harvesters.openbeautyfacts import (
    OBF_CSV_URL,
    OBF_JSONL_URL,
    OPF_CSV_URL,
    HarvestedCandidate,
    SourceHarvestResult,
    derive_obf_image_urls,
    harvest_open_facts,
    load_csv_image_map,
)
from qr_scrabber.harvesters.upcitemdb import (
    DEFAULT_UPC_BASE_URL,
    UPCItemDBHarvester,
    UPCItemDBHarvestOutput,
)

__all__ = [
    "BulkDownloadResult",
    "DEFAULT_UPC_BASE_URL",
    "HarvestedCandidate",
    "OBF_CSV_URL",
    "OBF_JSONL_URL",
    "OPF_CSV_URL",
    "PersistentResponseCache",
    "ProductImageDownloader",
    "SourceHarvestResult",
    "UPCItemDBHarvestOutput",
    "UPCItemDBHarvester",
    "atomic_write_json",
    "derive_obf_image_urls",
    "download_bulk_file",
    "harvest_open_facts",
    "load_csv_image_map",
    "normalize_image_to_jpeg",
    "probe_sources",
]
