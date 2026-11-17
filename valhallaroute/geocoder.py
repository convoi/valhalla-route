"""Offline reverse geocoding utility using Valhalla / Overture admin.sqlite."""
from __future__ import annotations

import logging
from pathlib import Path
import sqlite3
import struct
import threading
from typing import Any, Sequence

LOGGER = logging.getLogger(__name__)


def parse_spatialite_rings(blob: bytes) -> list[list[list[tuple[float, float]]]]:
    """Parse a SpatiaLite GAIA_MULTIPOLYGON or GAIA_POLYGON BLOB into lists of polygon rings.

    Returns:
        List of polygons, where each polygon is a list of rings [exterior, hole1, hole2, ...],
        and each ring is a list of (x, y) float tuples (where x=lon, y=lat).
    """
    if len(blob) < 43:
        return []

    # Check for SpatiaLite header: [0]=0x00, [1]=endian (1=LE, 0=BE)
    endian_byte = blob[1]
    endian = "<" if endian_byte == 1 else ">"

    # Byte 38 is GAIA_MARK_MBR (0x7C) in standard SpatiaLite
    if blob[0] == 0x00 and len(blob) > 38 and blob[38] == 0x7C:
        geom_type = struct.unpack_from(f"{endian}I", blob, 39)[0]
        offset = 43
    elif blob[0] in (0x00, 0x01):  # Standard OGC WKB without SpatiaLite header
        endian = "<" if blob[0] == 1 else ">"
        geom_type = struct.unpack_from(f"{endian}I", blob, 1)[0]
        offset = 5
    else:
        return []

    all_polys: list[list[list[tuple[float, float]]]] = []

    if geom_type == 6:  # GAIA_MULTIPOLYGON or WKB MultiPolygon
        num_polys = struct.unpack_from(f"{endian}I", blob, offset)[0]
        offset += 4
        for _ in range(num_polys):
            if offset >= len(blob):
                break
            # Skip GAIA_MARK_ENTITY (0x69) if present
            if blob[offset] == 0x69:
                offset += 1
            # Read poly_type & num_rings
            if offset + 8 > len(blob):
                break
            _, num_rings = struct.unpack_from(f"{endian}II", blob, offset)
            offset += 8
            rings: list[list[tuple[float, float]]] = []
            for _ in range(num_rings):
                if offset + 4 > len(blob):
                    break
                num_points = struct.unpack_from(f"{endian}I", blob, offset)[0]
                offset += 4
                pts_len = num_points * 16
                if offset + pts_len > len(blob):
                    break
                pts_bytes = blob[offset : offset + pts_len]
                offset += pts_len
                coords = struct.unpack(f"{endian}{num_points * 2}d", pts_bytes)
                rings.append(list(zip(coords[0::2], coords[1::2])))
            if rings:
                all_polys.append(rings)

    elif geom_type == 3:  # GAIA_POLYGON or WKB Polygon
        if offset >= len(blob):
            return []
        if blob[offset] == 0x69:
            offset += 1
        num_rings = struct.unpack_from(f"{endian}I", blob, offset)[0]
        offset += 4
        rings = []
        for _ in range(num_rings):
            if offset + 4 > len(blob):
                break
            num_points = struct.unpack_from(f"{endian}I", blob, offset)[0]
            offset += 4
            pts_len = num_points * 16
            if offset + pts_len > len(blob):
                break
            pts_bytes = blob[offset : offset + pts_len]
            offset += pts_len
            coords = struct.unpack(f"{endian}{num_points * 2}d", pts_bytes)
            rings.append(list(zip(coords[0::2], coords[1::2])))
        if rings:
            all_polys.append(rings)

    return all_polys


def point_in_polygon_ring(x: float, y: float, ring: Sequence[tuple[float, float]]) -> bool:
    """Standard ray-casting algorithm to test whether point (x, y) is inside a polygon ring."""
    inside = False
    n = len(ring)
    if n < 3:
        return False
    # If the ring is closed (ring[0] == ring[-1]), test n - 1 segments; else test n segments
    limit = (n - 1) if (ring[0] == ring[-1]) else n
    for i in range(limit):
        p1x, p1y = ring[i]
        p2x, p2y = ring[(i + 1) % n]
        if (p1y > y) != (p2y > y):
            xinters = (p2x - p1x) * (y - p1y) / (p2y - p1y) + p1x
            if x < xinters:
                inside = not inside
    return inside


def point_in_rings(x: float, y: float, rings: list[list[tuple[float, float]]]) -> bool:
    """Ray casting point-in-polygon test with interior hole subtraction."""
    if not rings:
        return False

    # Check exterior ring
    if not point_in_polygon_ring(x, y, rings[0]):
        return False

    # Check holes
    for hole in rings[1:]:
        if point_in_polygon_ring(x, y, hole):
            return False

    return True


def point_in_multipolygon(x: float, y: float, polys: list[list[list[tuple[float, float]]]]) -> bool:
    """Test if point (x, y) is inside any polygon in the multipolygon collection."""
    for rings in polys:
        if point_in_rings(x, y, rings):
            return True
    return False


try:
    import numpy as np
    import shapely
    from shapely.geometry import MultiPolygon, Polygon

    HAS_SHAPELY = True
except ImportError:
    HAS_SHAPELY = False

GEOCODE_BATCH_SIZE: int = 8192


class AdminGeocoder:
    """Thread-safe reverse geocoder querying Valhalla/Overture administrative boundaries (SQLite or GeoParquet)."""

    def __init__(self, admin_db_path: Path | str | None = None) -> None:
        self.db_path = self._resolve_db_path(admin_db_path)
        self.is_parquet = bool(self.db_path and self.db_path.suffix.lower() == ".parquet")
        self._geom_cache: dict[Any, Any] = {}
        self._cache_lock = threading.Lock()
        self._local = threading.local()

    @staticmethod
    def _resolve_db_path(db_path: Path | str | None) -> Path | None:
        if db_path is not None:
            p = Path(db_path)
            if p.exists() and p.stat().st_size > 0:
                return p
            return None

        candidates = [
            Path("valhalla_tiles/admins.parquet"),
            Path("valhalla_tiles/admin.parquet"),
            Path("valhalla_tiles_cache/admins.parquet"),
            Path("valhalla_tiles_cache/admin.parquet"),
            Path("admins.parquet"),
            Path("admin.parquet"),
            Path("valhalla_tiles/admin.sqlite"),
            Path("valhalla_tiles_cache/admin.sqlite"),
            Path("valhalla_tiles/admins.sqlite"),
            Path("admin.sqlite"),
            Path("admins.sqlite"),
        ]
        for cand in candidates:
            if cand.exists() and cand.stat().st_size > 0:
                return cand
        return None

    def _get_connection(self) -> sqlite3.Connection | None:
        if not self.db_path or not self.db_path.exists():
            return None

        conn = getattr(self._local, "conn", None)
        if conn is None:
            # Open SQLite in read-only URI mode for thread-safety and zero file locking
            uri = f"file:{self.db_path.resolve().as_posix()}?mode=ro"
            try:
                conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
                self._local.conn = conn
            except Exception as e:
                LOGGER.warning(f"Could not open admin.sqlite at {self.db_path}: {e}")
                return None
        return conn

    def _get_duckdb_connection(self):
        con = getattr(self._local, "duckdb_conn", None)
        if con is None:
            import duckdb
            con = duckdb.connect()
            self._local.duckdb_conn = con
        return con

    @property
    def has_admin_db(self) -> bool:
        """Check whether a valid admin database (SQLite or Parquet) with tables is available."""
        if not self.db_path or not self.db_path.exists():
            return False

        if self.is_parquet:
            return self.db_path.stat().st_size > 0

        conn = self._get_connection()
        if not conn:
            return False
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('admins', 'idx_admins_geom')")
            tables = [r[0] for r in cur.fetchall()]
            return "admins" in tables and "idx_admins_geom" in tables
        except Exception:
            return False

    def _query_sqlite_candidates(
        self,
        min_lon: float,
        min_lat: float,
        max_lon: float,
        max_lat: float,
    ) -> list[tuple[Any, ...]]:
        conn = self._get_connection()
        if not conn:
            return []
        cur = conn.cursor()
        query = """
        SELECT a.rowid, a.admin_level, a.name, a.iso_code, a.geom,
               idx.xmin, idx.ymin, idx.xmax, idx.ymax
        FROM idx_admins_geom idx
        JOIN admins a ON idx.pkid = a.rowid
        WHERE idx.xmin <= ? AND idx.xmax >= ?
          AND idx.ymin <= ? AND idx.ymax >= ?
        ORDER BY a.admin_level DESC
        """
        cur.execute(query, (max_lon, min_lon, max_lat, min_lat))
        return cur.fetchall()

    def _query_parquet_candidates(
        self,
        min_lon: float,
        min_lat: float,
        max_lon: float,
        max_lat: float,
    ) -> list[tuple[Any, ...]]:
        con = self._get_duckdb_connection()
        if not con or not self.db_path:
            return []
        p_path = self.db_path.resolve().as_posix().replace("'", "''")
        query = f"""
        SELECT division_id, admin_level, name, iso_code, geom, xmin, ymin, xmax, ymax
        FROM '{p_path}'
        WHERE xmin <= ? AND xmax >= ?
          AND ymin <= ? AND ymax >= ?
        ORDER BY admin_level DESC
        """
        return con.execute(query, [max_lon, min_lon, max_lat, min_lat]).fetchall()

    def lookup_coordinates(
        self,
        coords: Sequence[tuple[float, float]],
        precision_decimals: int | None = None,
    ) -> list[dict[str, Any]]:
        """Look up city, county, state, and country for a sequence of (latitude, longitude) coordinates.

        Args:
            coords: Sequence of (lat, lon) float tuples.
            precision_decimals: Optional decimal places to round coordinates for deduplication.
                Defaults to None (exact coordinates; deduplicating only identical points).
                Pass precision_decimals=3 to restore legacy behavior (~100m coordinate rounding).

        Returns:
            List of dicts with keys: 'city', 'county', 'country_code', 'country', 'admin1', 'admin2'.
        """
        if not coords:
            return []

        default_result = {
            "city": None,
            "county": None,
            "country_code": None,
            "country": None,
            "admin1": None,
            "admin2": None,
        }

        if not self.has_admin_db:
            return [default_result.copy() for _ in range(len(coords))]

        # Deduplicate points using exact coordinates by default, or rounded coordinates if precision_decimals is set
        unique_keys: list[tuple[float, float]] = []
        seen: dict[tuple[float, float], int] = {}
        indices: list[int] = []

        if precision_decimals is None:
            for c in coords:
                key = (c[0], c[1])
                if key not in seen:
                    seen[key] = len(unique_keys)
                    unique_keys.append(key)
                indices.append(seen[key])
        else:
            for c in coords:
                key = (round(c[0], precision_decimals), round(c[1], precision_decimals))
                if key not in seen:
                    seen[key] = len(unique_keys)
                    unique_keys.append(key)
                indices.append(seen[key])

        min_lat = min(k[0] for k in unique_keys)
        max_lat = max(k[0] for k in unique_keys)
        min_lon = min(k[1] for k in unique_keys)
        max_lon = max(k[1] for k in unique_keys)

        if self.is_parquet:
            raw_candidates = self._query_parquet_candidates(min_lon, min_lat, max_lon, max_lat)
        else:
            raw_candidates = self._query_sqlite_candidates(min_lon, min_lat, max_lon, max_lat)

        if not raw_candidates:
            return [default_result.copy() for _ in range(len(coords))]

        # Prepare geometries once for all candidate admins covering the trajectory
        prepared_candidates = []
        for cache_key, admin_level, name, iso_code, geom_blob, b_xmin, b_ymin, b_xmax, b_ymax in raw_candidates:
            if not geom_blob:
                continue

            geom_obj = self._geom_cache.get(cache_key)
            if geom_obj is None:
                if HAS_SHAPELY:
                    try:
                        geom_obj = shapely.from_wkb(bytes(geom_blob))
                        shapely.prepare(geom_obj)
                    except Exception:
                        geom_obj = None

                if geom_obj is None:
                    rings = parse_spatialite_rings(geom_blob)
                    if HAS_SHAPELY:
                        polys = []
                        for r_list in rings:
                            if r_list:
                                ext = r_list[0]
                                holes = r_list[1:] if len(r_list) > 1 else None
                                polys.append(Polygon(ext, holes))
                        geom_obj = MultiPolygon(polys) if polys else None
                        if geom_obj:
                            shapely.prepare(geom_obj)
                    else:
                        geom_obj = rings

                with self._cache_lock:
                    self._geom_cache[cache_key] = geom_obj

            if geom_obj is not None:
                prepared_candidates.append(
                    (cache_key, admin_level, name, iso_code, geom_obj, (b_xmin, b_ymin, b_xmax, b_ymax))
                )

        if not prepared_candidates:
            return [default_result.copy() for _ in range(len(coords))]

        unique_results: list[dict[str, Any]] = [default_result.copy() for _ in range(len(unique_keys))]

        if HAS_SHAPELY:
            cand_geoms = np.array([c[4] for c in prepared_candidates])
            tree = shapely.STRtree(cand_geoms)

            num_unique = len(unique_keys)
            for batch_start in range(0, num_unique, GEOCODE_BATCH_SIZE):
                batch_end = min(batch_start + GEOCODE_BATCH_SIZE, num_unique)
                batch_keys = unique_keys[batch_start:batch_end]
                batch_len = len(batch_keys)

                batch_lats = np.fromiter((k[0] for k in batch_keys), dtype=np.float64, count=batch_len)
                batch_lons = np.fromiter((k[1] for k in batch_keys), dtype=np.float64, count=batch_len)

                pts = shapely.points(batch_lons, batch_lats)
                pt_indices, geom_indices = tree.query(pts)

                if len(pt_indices) == 0:
                    continue

                contained = shapely.contains_xy(
                    cand_geoms[geom_indices],
                    batch_lons[pt_indices],
                    batch_lats[pt_indices],
                )

                valid_pts = pt_indices[contained]
                valid_geoms = geom_indices[contained]

                if len(valid_pts) == 0:
                    continue

                order = np.lexsort((valid_geoms, valid_pts))

                for p_idx, g_idx in zip(valid_pts[order], valid_geoms[order]):
                    res = unique_results[batch_start + p_idx]
                    if res["city"] and res["county"] and res["admin1"] and res["country_code"]:
                        continue

                    _, admin_level, name, iso_code, _, _ = prepared_candidates[g_idx]
                    if admin_level in (7, 8, 9, 10) and not res["city"]:
                        res["city"] = name
                    elif admin_level in (5, 6):
                        if not res["county"]:
                            res["county"] = name
                        if not res["admin2"]:
                            res["admin2"] = name
                    elif admin_level in (3, 4) and not res["admin1"]:
                        res["admin1"] = name
                    elif admin_level == 2:
                        if not res["country_code"]:
                            res["country_code"] = iso_code
                        if not res["country"]:
                            res["country"] = name
        else:
            for i, (lat, lon) in enumerate(unique_keys):
                res = unique_results[i]
                for _, admin_level, name, iso_code, rings, (b_minx, b_miny, b_maxx, b_maxy) in prepared_candidates:
                    if not (b_minx <= lon <= b_maxx and b_miny <= lat <= b_maxy):
                        continue
                    if point_in_multipolygon(lon, lat, rings):
                        if admin_level in (7, 8, 9, 10) and not res["city"]:
                            res["city"] = name
                        elif admin_level in (5, 6):
                            if not res["county"]:
                                res["county"] = name
                            if not res["admin2"]:
                                res["admin2"] = name
                        elif admin_level in (3, 4) and not res["admin1"]:
                            res["admin1"] = name
                        elif admin_level == 2:
                            if not res["country_code"]:
                                res["country_code"] = iso_code
                            if not res["country"]:
                                res["country"] = name
                        if res["city"] and res["county"] and res["admin1"] and res["country_code"]:
                            break

        return [unique_results[idx] for idx in indices]


_GLOBAL_GEOCODER: AdminGeocoder | None = None
_GEOCODER_LOCK = threading.Lock()


def lookup_coordinates(
    coords: Sequence[tuple[float, float]],
    admin_db_path: Path | str | None = None,
    precision_decimals: int | None = None,
) -> list[dict[str, Any]]:
    """Look up city, county, and country for a sequence of (latitude, longitude) coordinates.

    Args:
        coords: Sequence of (lat, lon) float tuples.
        admin_db_path: Optional explicit path to admin database (.sqlite or .parquet).
        precision_decimals: Optional decimal places to round coordinates for deduplication.
            Defaults to None (exact coordinates; deduplicating only identical points).
            Pass precision_decimals=3 to restore legacy behavior (~100m coordinate rounding).

    Returns:
        List of dicts with keys: 'city', 'county', 'country_code', 'country', 'admin1', 'admin2'.
    """
    global _GLOBAL_GEOCODER
    if admin_db_path:
        geocoder = AdminGeocoder(admin_db_path=admin_db_path)
        return geocoder.lookup_coordinates(coords, precision_decimals=precision_decimals)

    if _GLOBAL_GEOCODER is None:
        with _GEOCODER_LOCK:
            if _GLOBAL_GEOCODER is None:
                _GLOBAL_GEOCODER = AdminGeocoder()

    return _GLOBAL_GEOCODER.lookup_coordinates(coords, precision_decimals=precision_decimals)
