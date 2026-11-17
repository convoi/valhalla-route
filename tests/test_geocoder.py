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

