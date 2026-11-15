"""ValhallaRoute: GPS trip route enrichment using Valhalla."""
__version__ = "0.3.0"

from .client import ActorBackend, HttpBackend, ValhallaBackend, resolve_backend
from .crossings import CrossingIndexer
from .enricher import enrich_gps_points, read_gps_csv
from .geocoder import lookup_coordinates
from .tile_manager import (
    build_valhalla_tiles,
    download_file,
    download_osm_pbf,
    ensure_tiles_exist,
    fetch_geofabrik_index,
    find_matching_geofabrik_region,
)

__all__ = [
    "enrich_gps_points",
    "read_gps_csv",
    "lookup_coordinates",
    "CrossingIndexer",
    "ValhallaBackend",
    "ActorBackend",
    "HttpBackend",
    "resolve_backend",
    "build_valhalla_tiles",
    "download_file",
    "download_osm_pbf",
    "ensure_tiles_exist",
    "fetch_geofabrik_index",
    "find_matching_geofabrik_region",
]
