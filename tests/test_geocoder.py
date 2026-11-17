"""Unit tests for offline reverse geocoder using admin.sqlite."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import struct

import pytest

from valhallaroute.admin_builder import init_valhalla_admin_sqlite, wkb_to_spatialite_blob
from valhallaroute.geocoder import (
    AdminGeocoder,
    lookup_coordinates,
    parse_spatialite_rings,
    point_in_multipolygon,
    point_in_rings,
)


def create_wkb_polygon(minx: float, miny: float, maxx: float, maxy: float) -> bytes:
    """Create a standard OGC WKB little-endian rectangular Polygon."""
    out = bytearray()
    out.append(1)  # little endian
    out.extend(struct.pack("<I", 3))  # Polygon (3)
    out.extend(struct.pack("<I", 1))  # 1 ring
    out.extend(struct.pack("<I", 5))  # 5 points
    coords = [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny)]
    for x, y in coords:
        out.extend(struct.pack("<dd", x, y))
    return bytes(out)


def create_wkb_polygon_with_hole(
    ext_minx: float,
    ext_miny: float,
    ext_maxx: float,
    ext_maxy: float,
    hole_minx: float,
    hole_miny: float,
    hole_maxx: float,
    hole_maxy: float,
) -> bytes:
    """Create a standard OGC WKB Polygon with one interior hole."""
    out = bytearray()
    out.append(1)  # little endian
    out.extend(struct.pack("<I", 3))  # Polygon (3)
    out.extend(struct.pack("<I", 2))  # 2 rings (exterior + hole)
    # Exterior ring
    out.extend(struct.pack("<I", 5))
    for x, y in [
        (ext_minx, ext_miny),
        (ext_maxx, ext_miny),
        (ext_maxx, ext_maxy),
        (ext_minx, ext_maxy),
        (ext_minx, ext_miny),
    ]:
        out.extend(struct.pack("<dd", x, y))
    # Hole ring
    out.extend(struct.pack("<I", 5))
    for x, y in [
        (hole_minx, hole_miny),
        (hole_maxx, hole_miny),
        (hole_maxx, hole_maxy),
        (hole_minx, hole_maxy),
        (hole_minx, hole_miny),
    ]:
        out.extend(struct.pack("<dd", x, y))
    return bytes(out)


def create_wkb_multipolygon(boxes: list[tuple[float, float, float, float]]) -> bytes:
    """Create a standard OGC WKB MultiPolygon from rectangular boxes."""
    out = bytearray()
    out.append(1)  # little endian
    out.extend(struct.pack("<I", 6))  # MultiPolygon (6)
    out.extend(struct.pack("<I", len(boxes)))
    for minx, miny, maxx, maxy in boxes:
        out.append(1)
        out.extend(struct.pack("<I", 3))  # Polygon (3)
        out.extend(struct.pack("<I", 1))  # 1 ring
        out.extend(struct.pack("<I", 5))
        for x, y in [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny)]:
            out.extend(struct.pack("<dd", x, y))
    return bytes(out)


def test_spatialite_ring_parsing_and_point_in_polygon():
    # Box from (9.0, 52.0) to (10.0, 53.0)
    wkb = create_wkb_polygon(9.0, 52.0, 10.0, 53.0)
    blob = wkb_to_spatialite_blob(wkb, 9.0, 52.0, 10.0, 53.0, srid=4326)

    polys = parse_spatialite_rings(blob)
    assert len(polys) == 1
    assert len(polys[0]) == 1  # 1 exterior ring

    # Test points
    assert point_in_multipolygon(9.5, 52.5, polys) is True
    assert point_in_multipolygon(8.5, 52.5, polys) is False
    assert point_in_multipolygon(9.5, 53.5, polys) is False


def test_point_in_rings_with_hole():
    # Outer ring: (0, 0) to (10, 10)
    exterior = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]
    # Hole: (3, 3) to (7, 7)
    hole = [(3.0, 3.0), (7.0, 3.0), (7.0, 7.0), (3.0, 7.0), (3.0, 3.0)]
    rings = [exterior, hole]

    assert point_in_rings(1.0, 1.0, rings) is True
    assert point_in_rings(5.0, 5.0, rings) is False  # inside hole
    assert point_in_rings(12.0, 12.0, rings) is False  # outside exterior


@pytest.fixture
def synthetic_admin_db(tmp_path: Path) -> Path:
    db_file = tmp_path / "test_admins.sqlite"
    conn = init_valhalla_admin_sqlite(db_file)
    cur = conn.cursor()

    # 1. Country: Deutschland (covering 6.0 to 15.0 lon, 47.0 to 55.0 lat)
    wkb_country = create_wkb_polygon(6.0, 47.0, 15.0, 55.0)
    blob_country = wkb_to_spatialite_blob(wkb_country, 6.0, 47.0, 15.0, 55.0)
    cur.execute(
        """
        INSERT INTO admins (admin_level, iso_code, parent_admin,
                            name, name_en, drive_on_right, allow_intersection_names, geom)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (2, "DE", None, "Deutschland", "Germany", 1, 1, blob_country),
    )
    country_id = cur.lastrowid
    cur.execute(
        "INSERT INTO idx_admins_geom (pkid, xmin, xmax, ymin, ymax) VALUES (?, ?, ?, ?, ?)",
        (country_id, 6.0, 15.0, 47.0, 55.0),
    )

    # 2. State: Niedersachsen (covering 8.0 to 11.5 lon, 51.3 to 53.9 lat)
    wkb_state = create_wkb_polygon(8.0, 51.3, 11.5, 53.9)
    blob_state = wkb_to_spatialite_blob(wkb_state, 8.0, 51.3, 11.5, 53.9)
    cur.execute(
        """
        INSERT INTO admins (admin_level, iso_code, parent_admin,
                            name, name_en, drive_on_right, allow_intersection_names, geom)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (4, "DE-NI", country_id, "Niedersachsen", "Lower Saxony", 1, 1, blob_state),
    )
    state_id = cur.lastrowid
    cur.execute(
        "INSERT INTO idx_admins_geom (pkid, xmin, xmax, ymin, ymax) VALUES (?, ?, ?, ?, ?)",
        (state_id, 8.0, 11.5, 51.3, 53.9),
    )

    # 3. County: Region Hannover (covering 9.5 to 10.1 lon, 52.1 to 52.6 lat)
    wkb_county = create_wkb_polygon(9.5, 52.1, 10.1, 52.6)
    blob_county = wkb_to_spatialite_blob(wkb_county, 9.5, 52.1, 10.1, 52.6)
    cur.execute(
        """
        INSERT INTO admins (admin_level, iso_code, parent_admin,
                            name, name_en, drive_on_right, allow_intersection_names, geom)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (6, "DE", state_id, "Region Hannover", "Hanover Region", 1, 1, blob_county),
    )
    county_id = cur.lastrowid
    cur.execute(
        "INSERT INTO idx_admins_geom (pkid, xmin, xmax, ymin, ymax) VALUES (?, ?, ?, ?, ?)",
        (county_id, 9.5, 10.1, 52.1, 52.6),
    )

    # 4. City: Hannover (covering 9.6 to 9.9 lon, 52.3 to 52.5 lat)
    wkb_city = create_wkb_polygon(9.6, 52.3, 9.9, 52.5)
    blob_city = wkb_to_spatialite_blob(wkb_city, 9.6, 52.3, 9.9, 52.5)
    cur.execute(
        """
        INSERT INTO admins (admin_level, iso_code, parent_admin,
                            name, name_en, drive_on_right, allow_intersection_names, geom)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (8, "DE", county_id, "Hannover", "Hanover", 1, 1, blob_city),
    )
    city_id = cur.lastrowid
    cur.execute(
        "INSERT INTO idx_admins_geom (pkid, xmin, xmax, ymin, ymax) VALUES (?, ?, ?, ?, ?)",
        (city_id, 9.6, 9.9, 52.3, 52.5),
    )

    conn.commit()
    conn.close()
    return db_file


def test_admin_geocoder_lookup(synthetic_admin_db: Path):
    geocoder = AdminGeocoder(admin_db_path=synthetic_admin_db)
    assert geocoder.has_admin_db is True

    # Point inside Hannover: (lat=52.37, lon=9.74)
    # Point outside Hannover but inside Region Hannover: (lat=52.25, lon=9.55)
    # Point outside Region Hannover but inside Niedersachsen: (lat=52.8, lon=9.5)
    # Point outside Germany: (lat=45.0, lon=2.0)
    coords = [
        (52.37, 9.74),
        (52.25, 9.55),
        (52.8, 9.5),
        (45.0, 2.0),
    ]

    results = geocoder.lookup_coordinates(coords)
    assert len(results) == 4

    # Inside Hannover
    assert results[0]["city"] == "Hannover"
    assert results[0]["county"] == "Region Hannover"
    assert results[0]["admin2"] == "Region Hannover"
    assert results[0]["admin1"] == "Niedersachsen"
    assert results[0]["country_code"] == "DE"
    assert results[0]["country"] == "Deutschland"

    # Outside Hannover, inside Region Hannover
    assert results[1]["city"] is None
    assert results[1]["county"] == "Region Hannover"
    assert results[1]["admin2"] == "Region Hannover"
    assert results[1]["admin1"] == "Niedersachsen"
    assert results[1]["country_code"] == "DE"

    # Outside Region Hannover, inside Niedersachsen
    assert results[2]["city"] is None
    assert results[2]["county"] is None
    assert results[2]["admin1"] == "Niedersachsen"
    assert results[2]["country_code"] == "DE"

    # Outside Germany
    assert results[3]["city"] is None
    assert results[3]["county"] is None
    assert results[3]["admin1"] is None
    assert results[3]["country_code"] is None


def test_admin_geocoder_multithreading(synthetic_admin_db: Path):
    """Verify that AdminGeocoder performs concurrent reads across multiple threads safely."""
    geocoder = AdminGeocoder(admin_db_path=synthetic_admin_db)

    def worker_task(thread_id: int):
        coords = [
            (52.37 + (thread_id * 0.001), 9.74 + (thread_id * 0.001))
            for _ in range(50)
        ]
        res = geocoder.lookup_coordinates(coords)
        assert len(res) == 50
        assert all(r["city"] == "Hannover" for r in res)
        assert all(r["country_code"] == "DE" for r in res)
        return True

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(worker_task, i) for i in range(16)]
        for f in futures:
            assert f.result() is True


def test_lookup_coordinates_missing_db(tmp_path: Path):
    non_existent = tmp_path / "non_existent.sqlite"
    coords = [(52.37, 9.74), (48.85, 2.35)]
    results = lookup_coordinates(coords, admin_db_path=non_existent)
    assert len(results) == 2
    assert results[0]["city"] is None
    assert results[0]["country_code"] is None


@pytest.fixture
def synthetic_admin_parquet(tmp_path: Path) -> Path:
    import duckdb

    parquet_file = tmp_path / "test_admins.parquet"
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")

    con.execute("""
    CREATE TABLE admins_staging AS
    SELECT 
        'div_country' AS division_id,
        2 AS admin_level,
        'DE' AS iso_code,
        'Deutschland' AS name,
        'Germany' AS name_en,
        6.0 AS xmin,
        47.0 AS ymin,
        15.0 AS xmax,
        55.0 AS ymax,
        ST_AsWKB(ST_GeomFromText('POLYGON ((6.0 47.0, 15.0 47.0, 15.0 55.0, 6.0 55.0, 6.0 47.0))')) AS geom
    UNION ALL
    SELECT
        'div_state' AS division_id,
        4 AS admin_level,
        'DE' AS iso_code,
        'Niedersachsen' AS name,
        'Lower Saxony' AS name_en,
        8.0 AS xmin,
        51.3 AS ymin,
        11.5 AS xmax,
        53.9 AS ymax,
        ST_AsWKB(ST_GeomFromText('POLYGON ((8.0 51.3, 11.5 51.3, 11.5 53.9, 8.0 53.9, 8.0 51.3))')) AS geom
    UNION ALL
    SELECT
        'div_county' AS division_id,
        6 AS admin_level,
        'DE' AS iso_code,
        'Region Hannover' AS name,
        'Hanover Region' AS name_en,
        9.5 AS xmin,
        52.1 AS ymin,
        10.1 AS xmax,
        52.6 AS ymax,
        ST_AsWKB(ST_GeomFromText('POLYGON ((9.5 52.1, 10.1 52.1, 10.1 52.6, 9.5 52.6, 9.5 52.1))')) AS geom
    UNION ALL
    SELECT
        'div_city' AS division_id,
        8 AS admin_level,
        'DE' AS iso_code,
        'Hannover' AS name,
        'Hanover' AS name_en,
        9.6 AS xmin,
        52.3 AS ymin,
        9.9 AS xmax,
        52.5 AS ymax,
        ST_AsWKB(ST_GeomFromText('POLYGON ((9.6 52.3, 9.9 52.3, 9.9 52.5, 9.6 52.5, 9.6 52.3))')) AS geom
    ;
    """)
    p_path = parquet_file.as_posix().replace("'", "''")
    con.execute(f"COPY admins_staging TO '{p_path}' (FORMAT PARQUET);")
    con.close()
    return parquet_file


def test_admin_geocoder_parquet_lookup(synthetic_admin_parquet: Path):
    geocoder = AdminGeocoder(admin_db_path=synthetic_admin_parquet)
    assert geocoder.has_admin_db is True
    assert geocoder.is_parquet is True

    # Point inside Hannover: (lat=52.37, lon=9.74)
    # Point outside Hannover but inside Region Hannover: (lat=52.25, lon=9.55)
    # Point outside Region Hannover but inside Niedersachsen: (lat=52.8, lon=9.5)
    # Point outside Germany: (lat=45.0, lon=2.0)
    coords = [
        (52.37, 9.74),
        (52.25, 9.55),
        (52.8, 9.5),
        (45.0, 2.0),
    ]
    results = geocoder.lookup_coordinates(coords)
    assert len(results) == 4

    # Inside Hannover
    assert results[0]["city"] == "Hannover"
    assert results[0]["county"] == "Region Hannover"
    assert results[0]["admin2"] == "Region Hannover"
    assert results[0]["admin1"] == "Niedersachsen"
    assert results[0]["country_code"] == "DE"
    assert results[0]["country"] == "Deutschland"

    # Outside Hannover, inside Region Hannover
    assert results[1]["city"] is None
    assert results[1]["county"] == "Region Hannover"
    assert results[1]["admin2"] == "Region Hannover"
    assert results[1]["admin1"] == "Niedersachsen"
    assert results[1]["country_code"] == "DE"

    # Outside Region Hannover, inside Niedersachsen
    assert results[2]["city"] is None
    assert results[2]["county"] is None
    assert results[2]["admin1"] == "Niedersachsen"
    assert results[2]["country_code"] == "DE"

    # Outside Germany
    assert results[3]["city"] is None
    assert results[3]["county"] is None
    assert results[3]["admin1"] is None
    assert results[3]["country_code"] is None


def test_admin_geocoder_parquet_boundaries_and_legacy_rounding(synthetic_admin_parquet: Path):
    """Verify strict containment and exact coordinates on GeoParquet."""
    geocoder = AdminGeocoder(admin_db_path=synthetic_admin_parquet)

    # Southern boundary of Hannover city: lon [9.6, 9.9], lat [52.3, 52.5]
    coords = [
        (52.30, 9.75),   # Exactly on southern boundary: city excluded
        (52.3006, 9.75), # Strictly inside Hannover
    ]
    res_exact = geocoder.lookup_coordinates(coords, precision_decimals=None)
    assert res_exact[0]["city"] is None
    assert res_exact[0]["county"] == "Region Hannover"
    assert res_exact[1]["city"] == "Hannover"


def test_exact_coordinates_boundaries(synthetic_admin_db: Path):
    """Verify strict containment: polygon boundaries and corners are excluded."""
    geocoder = AdminGeocoder(admin_db_path=synthetic_admin_db)

    # Hannover city bounds: lon [9.6, 9.9], lat [52.3, 52.5]
    # Region Hannover bounds: lon [9.5, 10.1], lat [52.1, 52.6]
    coords = [
        (52.35, 9.75),   # Strictly inside Hannover
        (52.30, 9.75),   # Exactly on southern boundary of Hannover city
        (52.50, 9.75),   # Exactly on northern boundary of Hannover city
        (52.30, 9.60),   # Exactly on south-west corner of Hannover city
        (52.29999, 9.75),  # Just outside southern boundary of Hannover city
    ]
    results = geocoder.lookup_coordinates(coords, precision_decimals=None)

    # Strictly inside
    assert results[0]["city"] == "Hannover"
    assert results[0]["county"] == "Region Hannover"

    # Exactly on boundary / corner: city must be excluded by strict containment
    assert results[1]["city"] is None
    assert results[1]["county"] == "Region Hannover"  # (52.3, 9.75) is strictly inside Region Hannover [52.1, 52.6]

    assert results[2]["city"] is None
    assert results[2]["county"] == "Region Hannover"

    assert results[3]["city"] is None
    assert results[3]["county"] == "Region Hannover"

    # Just outside
    assert results[4]["city"] is None
    assert results[4]["county"] == "Region Hannover"


def test_legacy_rounding_vs_exact_coordinates(tmp_path: Path):
    """Verify default exact deduplication vs explicit legacy precision_decimals=3 rounding."""
    db_file = tmp_path / "test_rounding_admins.sqlite"
    conn = init_valhalla_admin_sqlite(db_file)
    cur = conn.cursor()

    # City covering lon [9.6, 9.8], lat [52.3000, 52.3008]
    wkb_city = create_wkb_polygon(9.6, 52.3000, 9.8, 52.3008)
    blob_city = wkb_to_spatialite_blob(wkb_city, 9.6, 52.3000, 9.8, 52.3008)
    cur.execute(
        """
        INSERT INTO admins (admin_level, iso_code, parent_admin, name, name_en, drive_on_right, allow_intersection_names, geom)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (8, "DE", None, "BoundaryCity", "BoundaryCity", 1, 1, blob_city),
    )
    city_id = cur.lastrowid
    cur.execute(
        "INSERT INTO idx_admins_geom (pkid, xmin, xmax, ymin, ymax) VALUES (?, ?, ?, ?, ?)",
        (city_id, 9.6, 9.8, 52.3000, 52.3008),
    )
    conn.commit()
    conn.close()

    geocoder = AdminGeocoder(admin_db_path=db_file)

    # Point at (52.3006, 9.70):
    # - Exact coordinate: 52.3006 is inside [52.3000, 52.3008]
    # - Rounded (precision_decimals=3): round(52.3006, 3) = 52.301, which is outside > 52.3008!
    coords = [(52.3006, 9.70)]

    # Default exact coordinates
    res_exact = geocoder.lookup_coordinates(coords, precision_decimals=None)
    assert res_exact[0]["city"] == "BoundaryCity"

    # Explicit legacy 3-decimal rounding
    res_rounded = geocoder.lookup_coordinates(coords, precision_decimals=3)
    assert res_rounded[0]["city"] is None


def test_overlapping_polygons_and_admin_precedence(tmp_path: Path):
    """Verify first-match rule and level precedence when candidate polygons overlap."""
    db_file = tmp_path / "test_precedence.sqlite"
    conn = init_valhalla_admin_sqlite(db_file)
    cur = conn.cursor()

    # Two overlapping level 8 cities in the same region: [9.0, 52.0] to [10.0, 53.0]
    wkb1 = create_wkb_polygon(9.0, 52.0, 10.0, 53.0)
    blob1 = wkb_to_spatialite_blob(wkb1, 9.0, 52.0, 10.0, 53.0)
    cur.execute(
        """
        INSERT INTO admins (admin_level, iso_code, parent_admin, name, name_en, drive_on_right, allow_intersection_names, geom)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (8, "DE", None, "FirstCity", "FirstCity", 1, 1, blob1),
    )
    id1 = cur.lastrowid
    cur.execute(
        "INSERT INTO idx_admins_geom (pkid, xmin, xmax, ymin, ymax) VALUES (?, ?, ?, ?, ?)",
        (id1, 9.0, 10.0, 52.0, 53.0),
    )

    wkb2 = create_wkb_polygon(9.0, 52.0, 10.0, 53.0)
    blob2 = wkb_to_spatialite_blob(wkb2, 9.0, 52.0, 10.0, 53.0)
    cur.execute(
        """
        INSERT INTO admins (admin_level, iso_code, parent_admin, name, name_en, drive_on_right, allow_intersection_names, geom)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (8, "DE", None, "SecondCity", "SecondCity", 1, 1, blob2),
    )
    id2 = cur.lastrowid
    cur.execute(
        "INSERT INTO idx_admins_geom (pkid, xmin, xmax, ymin, ymax) VALUES (?, ?, ?, ?, ?)",
        (id2, 9.0, 10.0, 52.0, 53.0),
    )

    # Level 6 county covering the same area
    wkb_county = create_wkb_polygon(9.0, 52.0, 10.0, 53.0)
    blob_county = wkb_to_spatialite_blob(wkb_county, 9.0, 52.0, 10.0, 53.0)
    cur.execute(
        """
        INSERT INTO admins (admin_level, iso_code, parent_admin, name, name_en, drive_on_right, allow_intersection_names, geom)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (6, "DE", None, "PrimaryCounty", "PrimaryCounty", 1, 1, blob_county),
    )
    id3 = cur.lastrowid
    cur.execute(
        "INSERT INTO idx_admins_geom (pkid, xmin, xmax, ymin, ymax) VALUES (?, ?, ?, ?, ?)",
        (id3, 9.0, 10.0, 52.0, 53.0),
    )

    conn.commit()
    conn.close()

    geocoder = AdminGeocoder(admin_db_path=db_file)
    results = geocoder.lookup_coordinates([(52.5, 9.5)])
    assert len(results) == 1
    # First candidate in traversal order wins
    assert results[0]["city"] == "FirstCity"
    assert results[0]["county"] == "PrimaryCounty"


def test_polygon_holes_and_multipolygons(tmp_path: Path):
    """Verify geometry parsing and containment for polygon holes and multipolygons."""
    db_file = tmp_path / "test_holes.sqlite"
    conn = init_valhalla_admin_sqlite(db_file)
    cur = conn.cursor()

    # 1. City with a hole: exterior [0, 0] to [10, 10], hole [3, 3] to [7, 7]
    wkb_hole = create_wkb_polygon_with_hole(0.0, 0.0, 10.0, 10.0, 3.0, 3.0, 7.0, 7.0)
    blob_hole = wkb_to_spatialite_blob(wkb_hole, 0.0, 0.0, 10.0, 10.0)
    cur.execute(
        """
        INSERT INTO admins (admin_level, iso_code, parent_admin, name, name_en, drive_on_right, allow_intersection_names, geom)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (8, "DE", None, "DonutCity", "DonutCity", 1, 1, blob_hole),
    )
    id_hole = cur.lastrowid
    cur.execute(
        "INSERT INTO idx_admins_geom (pkid, xmin, xmax, ymin, ymax) VALUES (?, ?, ?, ?, ?)",
        (id_hole, 0.0, 10.0, 0.0, 10.0),
    )

    # 2. MultiPolygon state with two disjoint islands: [20, 20] to [22, 22] and [25, 25] to [27, 27]
    wkb_mp = create_wkb_multipolygon([(20.0, 20.0, 22.0, 22.0), (25.0, 25.0, 27.0, 27.0)])
    blob_mp = wkb_to_spatialite_blob(wkb_mp, 20.0, 20.0, 27.0, 27.0)
    cur.execute(
        """
        INSERT INTO admins (admin_level, iso_code, parent_admin, name, name_en, drive_on_right, allow_intersection_names, geom)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (4, "DE-IS", None, "IslandState", "IslandState", 1, 1, blob_mp),
    )
    id_mp = cur.lastrowid
    cur.execute(
        "INSERT INTO idx_admins_geom (pkid, xmin, xmax, ymin, ymax) VALUES (?, ?, ?, ?, ?)",
        (id_mp, 20.0, 27.0, 20.0, 27.0),
    )

    conn.commit()
    conn.close()

    geocoder = AdminGeocoder(admin_db_path=db_file)

    coords = [
        (1.0, 1.0),   # Inside DonutCity exterior
        (5.0, 5.0),   # Inside DonutCity hole
        (21.0, 21.0), # Island 1
        (26.0, 26.0), # Island 2
        (23.5, 23.5), # In between islands
    ]
    results = geocoder.lookup_coordinates(coords)

    assert results[0]["city"] == "DonutCity"
    assert results[1]["city"] is None

    assert results[2]["admin1"] == "IslandState"
    assert results[3]["admin1"] == "IslandState"
    assert results[4]["admin1"] is None


def test_edge_cases_empty_no_candidates_duplicates(synthetic_admin_db: Path):
    """Verify empty input, no candidates, and duplicate coordinates."""
    geocoder = AdminGeocoder(admin_db_path=synthetic_admin_db)

    # Empty coords
    assert geocoder.lookup_coordinates([]) == []

    # Coordinates with no candidates (e.g. far offshore)
    no_cand_results = geocoder.lookup_coordinates([(0.0, 0.0), (-30.0, 120.0)])
    assert len(no_cand_results) == 2
    assert all(r["city"] is None and r["country_code"] is None for r in no_cand_results)

    # Duplicate coordinates
    dup_coords = [(52.37, 9.74), (45.0, 2.0), (52.37, 9.74), (52.25, 9.55), (45.0, 2.0)]
    dup_results = geocoder.lookup_coordinates(dup_coords)
    assert len(dup_results) == 5
    assert dup_results[0]["city"] == "Hannover"
    assert dup_results[1]["city"] is None
    assert dup_results[2]["city"] == "Hannover"
    assert dup_results[3]["county"] == "Region Hannover"
    assert dup_results[4]["city"] is None


def test_inputs_spanning_multiple_batches(synthetic_admin_db: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify coordinate batches crossing GEOCODE_BATCH_SIZE chunk boundaries."""
    import valhallaroute.geocoder as gc_mod

    # Set batch size small to force multiple batches
    monkeypatch.setattr(gc_mod, "GEOCODE_BATCH_SIZE", 5)

    geocoder = AdminGeocoder(admin_db_path=synthetic_admin_db)

    # Generate 23 distinct coordinates alternating inside and outside Hannover
    coords = []
    for i in range(23):
        if i % 2 == 0:
            coords.append((52.35 + (i * 0.001), 9.75 + (i * 0.001)))  # Inside Hannover
        else:
            coords.append((45.0 + (i * 0.1), 2.0 + (i * 0.1)))       # Outside Germany

    results = geocoder.lookup_coordinates(coords)
    assert len(results) == 23
    for i, r in enumerate(results):
        if i % 2 == 0:
            assert r["city"] == "Hannover"
            assert r["country_code"] == "DE"
        else:
            assert r["city"] is None
            assert r["country_code"] is None


def test_no_shapely_fallback(synthetic_admin_db: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify fallback pure-Python ray casting when Shapely is unavailable."""
    import valhallaroute.geocoder as gc_mod

    monkeypatch.setattr(gc_mod, "HAS_SHAPELY", False)

    geocoder = AdminGeocoder(admin_db_path=synthetic_admin_db)
    coords = [
        (52.37, 9.74),   # Inside Hannover
        (52.25, 9.55),   # Outside Hannover, inside Region Hannover
        (52.8, 9.5),     # Inside Niedersachsen
        (45.0, 2.0),     # Outside
    ]
    results = geocoder.lookup_coordinates(coords)
    assert len(results) == 4
    assert results[0]["city"] == "Hannover"
    assert results[0]["county"] == "Region Hannover"
    assert results[1]["city"] is None
    assert results[1]["county"] == "Region Hannover"
    assert results[2]["city"] is None
    assert results[2]["county"] is None
    assert results[2]["admin1"] == "Niedersachsen"
    assert results[3]["country_code"] is None


def test_indexed_vs_scalar_containment_reference(synthetic_admin_db: Path):
    """Verify that vectorized STRtree lookup produces identical results to scalar containment."""
    import shapely

    geocoder = AdminGeocoder(admin_db_path=synthetic_admin_db)

    # Generate a grid of 60 test points
    coords = []
    for lat_i in range(10):
        for lon_i in range(6):
            lat = 52.1 + lat_i * 0.06
            lon = 9.4 + lon_i * 0.12
            coords.append((lat, lon))

    # Fast indexed batch lookup
    indexed_results = geocoder.lookup_coordinates(coords, precision_decimals=None)

    # Reference scalar point-by-point containment lookup
    raw_candidates = geocoder._query_sqlite_candidates(
        min(c[1] for c in coords),
        min(c[0] for c in coords),
        max(c[1] for c in coords),
        max(c[0] for c in coords),
    )
    ref_candidates = []
    for rowid, admin_level, name, iso_code, geom_blob, xmin, ymin, xmax, ymax in raw_candidates:
        geom = geocoder._geom_cache.get(rowid)
        if geom is None:
            try:
                geom = shapely.from_wkb(bytes(geom_blob))
            except Exception:
                rings = parse_spatialite_rings(geom_blob)
                polys = [Polygon(r[0], r[1:] if len(r) > 1 else None) for r in rings if r]
                geom = MultiPolygon(polys) if polys else None
        ref_candidates.append((admin_level, name, iso_code, geom, (xmin, ymin, xmax, ymax)))

    scalar_results = []
    for lat, lon in coords:
        res = {
            "city": None,
            "county": None,
            "country_code": None,
            "country": None,
            "admin1": None,
            "admin2": None,
        }
        for admin_level, name, iso_code, geom, (xmin, ymin, xmax, ymax) in ref_candidates:
            if not (xmin <= lon <= xmax and ymin <= lat <= ymax):
                continue
            if shapely.contains_xy(geom, lon, lat):
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
        scalar_results.append(res)

    assert len(indexed_results) == len(scalar_results)
    for idx, (res_indexed, res_scalar) in enumerate(zip(indexed_results, scalar_results)):
        assert res_indexed == res_scalar, f"Mismatch at index {idx} ({coords[idx]}): {res_indexed} != {res_scalar}"


