"""Compiler for Valhalla admin.sqlite database from Overture Maps GeoParquet data.

Generates a 100% compliant SpatiaLite administrative database (admins, admin_access,
idx_admins_geom, geometry_columns, spatial_ref_sys) directly from Overture Maps
divisions data using DuckDB streaming without requiring external SpatiaLite C binaries.
"""
from __future__ import annotations

import io
import logging
import sqlite3
import struct
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

logger = logging.getLogger(__name__)

# Countries where vehicles drive on the left side of the road (drive_on_right = 0)
LEFT_HAND_DRIVE_COUNTRIES = {
    "AG", "AI", "AU", "BB", "BD", "BM", "BN", "BS", "BT", "BW", "CY", "DM", "FJ", "FK", "GB", "GD",
    "GG", "GY", "HK", "ID", "IE", "IM", "IN", "JE", "JM", "JP", "KE", "KI", "KN", "KY", "LC", "LK",
    "LS", "MO", "MS", "MT", "MU", "MV", "MW", "MY", "MZ", "NA", "NR", "NZ", "PG", "PK", "PN", "SB",
    "SC", "SG", "SH", "SR", "SZ", "TC", "TH", "TO", "TT", "TV", "TZ", "UG", "VC", "VG", "VI", "WS",
    "ZA", "ZM", "ZW"
}

# ISO-3166-1 alpha-2 default language code lookup
DEFAULT_LANGUAGES = {
    "NL": "nl", "BE": "nl", "DE": "de", "AT": "de", "CH": "de", "FR": "fr", "IT": "it",
    "ES": "es", "PT": "pt", "GB": "en", "IE": "en", "US": "en", "CA": "en", "AU": "en",
    "NZ": "en", "PL": "pl", "CZ": "cs", "SK": "sk", "HU": "hu", "RO": "ro", "BG": "bg",
    "GR": "el", "TR": "tr", "RU": "ru", "UA": "uk", "SE": "sv", "NO": "no", "DK": "da",
    "FI": "fi", "EE": "et", "LV": "lv", "LT": "lt", "LU": "lb", "IS": "is", "JP": "ja",
    "CN": "zh", "KR": "ko", "IN": "hi", "BR": "pt", "MX": "es", "AR": "es", "CL": "es",
    "CO": "es", "ZA": "en",
}

# Overture division subtype to Valhalla admin_level mapping
SUBTYPE_TO_ADMIN_LEVEL = {
    "country": 2,
    "region": 4,
    "county": 6,
    "localadmin": 8,
    "locality": 8,
}

ADMIN_LEVEL_TO_SUBTYPES = {
    2: ["country"],
    4: ["region"],
    6: ["county"],
    8: ["localadmin", "locality"],
}


def wkb_to_spatialite_blob(
    wkb: bytes,
    minx: float,
    miny: float,
    maxx: float,
    maxy: float,
    srid: int = 4326,
) -> bytes:
    """Convert an OGC WKB (Polygon or MultiPolygon) into a SpatiaLite MULTIPOLYGON BLOB.

    Binary layout compliant with SpatiaLite specification:
      [0..38]: Header (0x00, 0x01 LE, uint32 srid, 4x double bbox, 0x7C MBR_END)
      [39..42]: uint32 geom_type = 6 (GAIA_MULTIPOLYGON)
      [43..46]: uint32 num_polygons
      For each polygon:
        [offset]: 0x69 (GAIA_MARK_ENTITY)
        [offset+1..offset+4]: uint32 poly_type = 3 (GAIA_POLYGON)
        [offset+5..offset+8]: uint32 num_rings
        For each ring:
          uint32 num_points
          num_points * 16 bytes (double x, double y)
      [last]: 0xFE (GAIA_MARK_END)
    """
    stream = io.BytesIO(wkb)
    endian_byte = stream.read(1)[0]
    endian = "<" if endian_byte == 1 else ">"

    geom_type = struct.unpack(f"{endian}I", stream.read(4))[0]

    out = bytearray()
    # 39-byte SpatiaLite header
    out.extend(struct.pack("<BBI", 0x00, 0x01, srid))
    out.extend(struct.pack("<dddd", minx, miny, maxx, maxy))
    out.append(0x7C)  # GAIA_MARK_MBR

    # GAIA_MULTIPOLYGON (6)
    out.extend(struct.pack("<I", 6))

    if geom_type == 3:  # Single Polygon
        num_polys = 1
        out.extend(struct.pack("<I", num_polys))
        num_rings = struct.unpack(f"{endian}I", stream.read(4))[0]
        out.append(0x69)  # GAIA_MARK_ENTITY
        out.extend(struct.pack("<II", 3, num_rings))
        for _ in range(num_rings):
            num_points = struct.unpack(f"{endian}I", stream.read(4))[0]
            out.extend(struct.pack("<I", num_points))
            coords = stream.read(num_points * 16)
            out.extend(coords)
    elif geom_type == 6:  # MultiPolygon
        num_polys = struct.unpack(f"{endian}I", stream.read(4))[0]
        out.extend(struct.pack("<I", num_polys))
        for _ in range(num_polys):
            p_endian = "<" if stream.read(1)[0] == 1 else ">"
            p_type = struct.unpack(f"{p_endian}I", stream.read(4))[0]
            num_rings = struct.unpack(f"{p_endian}I", stream.read(4))[0]
            out.append(0x69)  # GAIA_MARK_ENTITY
            out.extend(struct.pack("<II", 3, num_rings))
            for _ in range(num_rings):
                num_points = struct.unpack(f"{p_endian}I", stream.read(4))[0]
                out.extend(struct.pack("<I", num_points))
                coords = stream.read(num_points * 16)
                out.extend(coords)
    else:
        raise ValueError(f"Unsupported geometry type in WKB: {geom_type} (expected Polygon=3 or MultiPolygon=6)")

    out.append(0xFE)  # GAIA_MARK_END
    return bytes(out)


def init_valhalla_admin_sqlite(db_path: Path) -> sqlite3.Connection:
    """Initialize a brand-new SQLite database matching Valhalla's official admin schema."""
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA synchronous=NORMAL;")

    # 1. Admins table
    cur.execute("""
    CREATE TABLE admins (
        admin_level INTEGER NOT NULL,
        iso_code TEXT,
        parent_admin INTEGER,
        name TEXT NOT NULL,
        name_en TEXT,
        drive_on_right INTEGER NOT NULL,
        allow_intersection_names INTEGER NOT NULL,
        default_language TEXT,
        supported_languages TEXT,
        "geom" MULTIPOLYGON
    );
    """)

    # 2. Admin access table
    cur.execute("""
    CREATE TABLE admin_access (
        admin_id INTEGER NOT NULL,
        iso_code TEXT,
        trunk INTEGER DEFAULT NULL,
        trunk_link INTEGER DEFAULT NULL,
        track INTEGER DEFAULT NULL,
        footway INTEGER DEFAULT NULL,
        pedestrian INTEGER DEFAULT NULL,
        bridleway INTEGER DEFAULT NULL,
        cycleway INTEGER DEFAULT NULL,
        path INTEGER DEFAULT NULL,
        motorroad INTEGER DEFAULT NULL
    );
    """)

    # 3. Spatial index R-Tree
    cur.execute("""
    CREATE VIRTUAL TABLE "idx_admins_geom" USING rtree(
        pkid,
        xmin,
        xmax,
        ymin,
        ymax
    );
    """)

    # 4. SpatiaLite metadata tables
    cur.execute("""
    CREATE TABLE spatial_ref_sys (
        srid INTEGER NOT NULL PRIMARY KEY,
        auth_name VARCHAR(256) NOT NULL,
        auth_srid INTEGER NOT NULL,
        srtext VARCHAR(2048) NOT NULL,
        proj4text VARCHAR(2048) NOT NULL
    );
    """)
    cur.execute("""
    INSERT INTO spatial_ref_sys (srid, auth_name, auth_srid, srtext, proj4text)
    VALUES (4326, 'epsg', 4326,
    'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","6326"]],AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],AXIS["Latitude",NORTH],AXIS["Longitude",EAST],AUTHORITY["EPSG","4326"]]',
    '+proj=longlat +datum=WGS84 +no_defs');
    """)

    cur.execute("""
    CREATE TABLE geometry_columns (
        f_table_name VARCHAR(256) NOT NULL,
        f_geometry_column VARCHAR(256) NOT NULL,
        geometry_type INTEGER NOT NULL,
        coord_dimension INTEGER NOT NULL,
        srid INTEGER NOT NULL,
        spatial_index_enabled INTEGER NOT NULL,
        CONSTRAINT pk_geom_cols PRIMARY KEY (f_table_name, f_geometry_column)
    );
    """)
    cur.execute("""
    INSERT INTO geometry_columns (f_table_name, f_geometry_column, geometry_type, coord_dimension, srid, spatial_index_enabled)
    VALUES ('admins', 'geom', 6, 2, 4326, 1);
    """)

    conn.commit()
    return conn


def build_admin_sqlite_from_overture(
    output_path: Union[str, Path],
    countries: Optional[Sequence[str]] = None,
    admin_levels: Optional[Sequence[int]] = None,
    release: str = "2026-10-21.0",
    s3_region: str = "us-west-2",
    include_maritime: bool = True,
    verbose: bool = True,
) -> Dict[str, Any]:
    """Build a Valhalla-compliant admin.sqlite from Overture Maps GeoParquet divisions data.

    Args:
        output_path: Destination path for admin.sqlite.
        countries: Optional list of ISO-3166-1 alpha-2 codes (e.g. ['NL', 'DE', 'BE']). None = planet.
        admin_levels: Sequence of admin levels (e.g. [2, 4], [2, 4, 6], or [2, 4, 6, 8]). Default: [2, 4].
        release: Overture Maps release string (default: '2026-10-21.0').
        s3_region: AWS region for Overture public S3 bucket (default: 'us-west-2').
        include_maritime: Whether to dissolve maritime territorial bounds with land. Default: True.
        verbose: Print progress logs.

    Returns:
        Summary dict containing statistics about created admin records.
    """
    import duckdb

    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if admin_levels is None:
        admin_levels = [2, 4]

    # Resolve required subtypes
    subtypes: List[str] = []
    for lvl in admin_levels:
        subtypes.extend(ADMIN_LEVEL_TO_SUBTYPES.get(lvl, []))

    if not subtypes:
        raise ValueError(f"No valid subtypes found for admin_levels: {admin_levels}")

    if verbose:
        logger.info(f"Connecting to DuckDB for Overture release {release}...")

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute(f"""
    CREATE OR REPLACE SECRET overture_secret (
        TYPE S3,
        PROVIDER config,
        REGION '{s3_region}',
        SCOPE 's3://overturemaps-us-west-2'
    );
    """)

    # Build WHERE clause
    where_clauses = []
    subtypes_sql = ", ".join(f"'{s}'" for s in subtypes)
    where_clauses.append(f"subtype IN ({subtypes_sql})")

    if not include_maritime:
        where_clauses.append("class = 'land'")

    if countries:
        countries_cleaned = [c.upper().strip() for c in countries]
        countries_sql = ", ".join(f"'{c}'" for c in countries_cleaned)
        where_clauses.append(f"country IN ({countries_sql})")

    where_stmt = " AND ".join(where_clauses)

    query = f"""
    SELECT 
        division_id,
        subtype,
        country,
        names.primary AS name,
        COALESCE(names.common['en'], names.primary) AS name_en,
        ST_XMin(geom) AS minx,
        ST_YMin(geom) AS miny,
        ST_XMax(geom) AS maxx,
        ST_YMax(geom) AS maxy,
        ST_AsWKB(geom) AS wkb
    FROM (
        SELECT 
            division_id,
            subtype,
            country,
            names,
            ST_Union_Agg(geometry) AS geom
        FROM read_parquet('s3://overturemaps-us-west-2/release/{release}/theme=divisions/type=division_area/*', hive_partitioning=1)
        WHERE {where_stmt}
        GROUP BY division_id, subtype, country, names
    )
    ORDER BY 
        CASE subtype
            WHEN 'country' THEN 1
            WHEN 'region' THEN 2
            WHEN 'county' THEN 3
            ELSE 4
        END,
        country, name;
    """

    if verbose:
        logger.info("Executing Overture divisions aggregation query via DuckDB...")

    rows = con.execute(query).fetchall()
    if verbose:
        logger.info(f"Retrieved {len(rows)} aggregated administrative entities from Overture.")

    # Initialize SQLite target
    conn = init_valhalla_admin_sqlite(output_path)
    cur = conn.cursor()

    stats = {
        "output_path": str(output_path),
        "total_records": 0,
        "by_admin_level": {},
        "countries": set(),
        "file_size_bytes": 0,
    }

    country_rowid_map: Dict[str, int] = {}
    region_rowid_map: Dict[str, int] = {}

    cur.execute("BEGIN TRANSACTION;")

    for row in rows:
        division_id, subtype, country, name, name_en, minx, miny, maxx, maxy, wkb = row
        admin_lvl = SUBTYPE_TO_ADMIN_LEVEL.get(subtype, 8)
        iso_code = country.upper() if country else None

        if iso_code:
            stats["countries"].add(iso_code)

        # Parent admin resolution
        parent_admin = None
        if admin_lvl == 4 and iso_code in country_rowid_map:
            parent_admin = country_rowid_map[iso_code]
        elif admin_lvl in (6, 8):
            if iso_code in region_rowid_map:
                parent_admin = region_rowid_map[iso_code]
            elif iso_code in country_rowid_map:
                parent_admin = country_rowid_map[iso_code]

        # Driving side
        drive_on_right = 0 if (iso_code and iso_code in LEFT_HAND_DRIVE_COUNTRIES) else 1

        # Language
        default_lang = DEFAULT_LANGUAGES.get(iso_code, "en") if iso_code else "en"

        # Pack geometry to SpatiaLite BLOB
        try:
            sp_blob = wkb_to_spatialite_blob(wkb, minx, miny, maxx, maxy, srid=4326)
        except Exception as e:
            logger.warning(f"Skipping entity {division_id} ({name}): failed to pack SpatiaLite blob: {e}")
            continue

        # Insert into admins
        cur.execute(
            """
            INSERT INTO admins (
                admin_level, iso_code, parent_admin, name, name_en,
                drive_on_right, allow_intersection_names, default_language,
                supported_languages, geom
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                admin_lvl,
                iso_code,
                parent_admin,
                name or "",
                name_en or name or "",
                drive_on_right,
                1,  # allow_intersection_names
                default_lang,
                None,  # supported_languages
                sp_blob,
            ),
        )
        rowid = cur.lastrowid

        # Cache rowids for hierarchy linking
        if admin_lvl == 2 and iso_code:
            country_rowid_map[iso_code] = rowid
            # Also populate admin_access for country
            cur.execute(
                """
                INSERT INTO admin_access (
                    admin_id, iso_code, trunk, trunk_link, track, footway,
                    pedestrian, bridleway, cycleway, path, motorroad
                ) VALUES (?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
                """,
                (rowid, iso_code),
            )
        elif admin_lvl == 4 and iso_code:
            region_rowid_map[iso_code] = rowid

        # Insert bounding box into R-Tree spatial index
        cur.execute(
            "INSERT INTO idx_admins_geom (pkid, xmin, xmax, ymin, ymax) VALUES (?, ?, ?, ?, ?);",
            (rowid, minx, maxx, miny, maxy),
        )

        stats["total_records"] += 1
        stats["by_admin_level"][admin_lvl] = stats["by_admin_level"].get(admin_lvl, 0) + 1

    conn.commit()

    # Index optimization
    cur.execute("CREATE INDEX IF NOT EXISTS idx_admins_iso ON admins(iso_code);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_admins_level ON admins(admin_level);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_access_admin ON admin_access(admin_id);")
    conn.commit()

    if verbose:
        logger.info("Optimizing admin.sqlite database (VACUUM and ANALYZE)...")
    cur.execute("VACUUM;")
    cur.execute("ANALYZE;")
    conn.close()

    stats["countries"] = sorted(list(stats["countries"]))
    stats["file_size_bytes"] = output_path.stat().st_size

    if verbose:
        logger.info(
            f"Successfully compiled {output_path.name}: {stats['total_records']} admins "
            f"({stats['file_size_bytes'] / (1024 * 1024):.2f} MB) across {len(stats['countries'])} countries."
        )

    return stats
