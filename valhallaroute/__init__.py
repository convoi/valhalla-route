"""ValhallaRoute: GPS trip route enrichment using Valhalla."""
__version__ = "0.4.0"

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

from .storage import (
    StorageManager,
    get_tiles_for_bbox,
    get_valhalla_tile_id,
    get_valhalla_tile_relative_path,
    uri_to_local_path,
)

from .admin_builder import build_admin_sqlite_from_overture, wkb_to_spatialite_blob
from .continents import build_continent_pack

__all__ = [
    "build_continent_pack",
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
    "StorageManager",
    "get_tiles_for_bbox",
    "get_valhalla_tile_id",
    "get_valhalla_tile_relative_path",
    "build_admin_sqlite_from_overture",
    "wkb_to_spatialite_blob",
]
