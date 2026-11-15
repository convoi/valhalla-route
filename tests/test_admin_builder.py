"""Unit tests for Valhalla admin.sqlite compiler from Overture Maps GeoParquet."""
import sqlite3
import struct
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from valhallaroute.admin_builder import (
    LEFT_HAND_DRIVE_COUNTRIES,
    build_admin_sqlite_from_overture,
    init_valhalla_admin_sqlite,
    wkb_to_spatialite_blob,
)
from valhallaroute.cli import main


def create_sample_wkb_polygon() -> bytes:
    """Create a standard OGC WKB little-endian Polygon ((0 0, 1 0, 1 1, 0 1, 0 0))."""
    out = bytearray()
    out.append(1)  # little endian
    out.extend(struct.pack("<I", 3))  # Polygon (3)
    out.extend(struct.pack("<I", 1))  # 1 ring
    out.extend(struct.pack("<I", 5))  # 5 points
    coords = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0, 0.0)]
    for x, y in coords:
        out.extend(struct.pack("<dd", x, y))
    return bytes(out)


def create_sample_wkb_multipolygon() -> bytes:
    """Create a standard OGC WKB little-endian MultiPolygon with 1 polygon."""
    poly_wkb = create_sample_wkb_polygon()
    out = bytearray()
    out.append(1)  # little endian
    out.extend(struct.pack("<I", 6))  # MultiPolygon (6)
    out.extend(struct.pack("<I", 1))  # 1 polygon
    out.extend(poly_wkb)
    return bytes(out)


def test_wkb_to_spatialite_blob_polygon():
    wkb = create_sample_wkb_polygon()
    blob = wkb_to_spatialite_blob(wkb, 0.0, 0.0, 1.0, 1.0, srid=4326)

    # 1. Header validation
    start, endian, srid = struct.unpack("<BBI", blob[:6])
    assert start == 0x00
    assert endian == 0x01
    assert srid == 4326

    minx, miny, maxx, maxy = struct.unpack("<dddd", blob[6:38])
    assert (minx, miny, maxx, maxy) == (0.0, 0.0, 1.0, 1.0)
    assert blob[38] == 0x7C

    # 2. GAIA_MULTIPOLYGON validation
    geom_type, num_polys = struct.unpack("<II", blob[39:47])
    assert geom_type == 6
    assert num_polys == 1

    # 3. Polygon entity validation
    marker = blob[47]
    assert marker == 0x69  # GAIA_MARK_ENTITY
    poly_type, num_rings, num_pts = struct.unpack("<III", blob[48:60])
    assert poly_type == 3
    assert num_rings == 1
    assert num_pts == 5

    # 4. End marker
    assert blob[-1] == 0xFE


def test_wkb_to_spatialite_blob_multipolygon():
    wkb = create_sample_wkb_multipolygon()
    blob = wkb_to_spatialite_blob(wkb, 0.0, 0.0, 1.0, 1.0, srid=4326)

    assert blob[0] == 0x00
    assert blob[38] == 0x7C
    geom_type, num_polys = struct.unpack("<II", blob[39:47])
    assert geom_type == 6
    assert num_polys == 1
    assert blob[47] == 0x69
    assert blob[-1] == 0xFE


def test_init_valhalla_admin_sqlite(tmp_path: Path):
    db_file = tmp_path / "test_admin.sqlite"
    conn = init_valhalla_admin_sqlite(db_file)
    cur = conn.cursor()

    tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    assert "admins" in tables
    assert "admin_access" in tables
    assert "idx_admins_geom" in tables
    assert "geometry_columns" in tables
    assert "spatial_ref_sys" in tables

    # Verify geometry_columns entry
    geom_entry = cur.execute("SELECT * FROM geometry_columns").fetchall()
    assert len(geom_entry) == 1
    assert geom_entry[0][0] == "admins"
    assert geom_entry[0][1] == "geom"
    assert geom_entry[0][2] == 6  # MULTIPOLYGON

    # Verify spatial_ref_sys 4326 entry
    srs_entry = cur.execute("SELECT srid, auth_name FROM spatial_ref_sys WHERE srid=4326").fetchone()
    assert srs_entry == (4326, "epsg")

    conn.close()


def test_build_admin_sqlite_mocked(tmp_path: Path):
    output_db = tmp_path / "custom_admin.sqlite"
    sample_wkb = create_sample_wkb_polygon()

    mock_rows = [
        # division_id, subtype, country, name, name_en, minx, miny, maxx, maxy, wkb
        ("div-1", "country", "NL", "Nederland", "Netherlands", 3.0, 50.0, 7.0, 53.0, sample_wkb),
        ("div-2", "region", "NL", "Noord-Holland", "North Holland", 4.5, 52.0, 5.2, 53.0, sample_wkb),
        ("div-3", "country", "GB", "United Kingdom", "United Kingdom", -8.0, 49.0, 2.0, 60.0, sample_wkb),
    ]

    mock_duckdb = MagicMock()
    mock_con = MagicMock()
    mock_duckdb.connect.return_value = mock_con
    mock_con.execute.return_value.fetchall.return_value = mock_rows

    with patch.dict("sys.modules", {"duckdb": mock_duckdb}):
        stats = build_admin_sqlite_from_overture(
            output_path=output_db,
            countries=["NL", "GB"],
            admin_levels=[2, 4],
            verbose=False,
        )

    assert stats["total_records"] == 3
    assert stats["by_admin_level"][2] == 2
    assert stats["by_admin_level"][4] == 1
    assert "NL" in stats["countries"]
    assert "GB" in stats["countries"]
    assert output_db.exists()

    # Query compiled SQLite
    conn = sqlite3.connect(output_db)
    cur = conn.cursor()

    # 1. Driving side check (NL right=1, GB left=0)
    nl_drive = cur.execute("SELECT drive_on_right FROM admins WHERE iso_code='NL' AND admin_level=2").fetchone()[0]
    gb_drive = cur.execute("SELECT drive_on_right FROM admins WHERE iso_code='GB' AND admin_level=2").fetchone()[0]
    assert nl_drive == 1
    assert gb_drive == 0

    # 2. Parent-child admin linking
    nl_id = cur.execute("SELECT rowid FROM admins WHERE iso_code='NL' AND admin_level=2").fetchone()[0]
    nh_parent = cur.execute("SELECT parent_admin FROM admins WHERE name='Noord-Holland'").fetchone()[0]
    assert nh_parent == nl_id

    # 3. Spatial index query simulation
    query = """
    SELECT a.name, a.admin_level, a.iso_code
    FROM admins a
    WHERE a.rowid IN (
        SELECT pkid FROM idx_admins_geom
        WHERE xmin <= ? AND xmax >= ? AND ymin <= ? AND ymax >= ?
    )
    ORDER BY a.admin_level;
    """
    matches = cur.execute(query, (4.8, 4.8, 52.3, 52.3)).fetchall()
    assert len(matches) == 2  # Matches both Netherlands (country) and Noord-Holland (region)
    assert matches[0] == ("Nederland", 2, "NL")
    assert matches[1] == ("Noord-Holland", 4, "NL")

    # 4. Admin access rows
    access_rows = cur.execute("SELECT * FROM admin_access").fetchall()
    assert len(access_rows) == 2  # 1 for NL country, 1 for GB country

    conn.close()


def test_cli_build_admin(tmp_path: Path):
    output_db = tmp_path / "cli_admin.sqlite"
    sample_wkb = create_sample_wkb_polygon()
    mock_rows = [
        ("div-de", "country", "DE", "Deutschland", "Germany", 5.8, 47.2, 15.0, 55.0, sample_wkb),
    ]

    mock_duckdb = MagicMock()
    mock_con = MagicMock()
    mock_duckdb.connect.return_value = mock_con
    mock_con.execute.return_value.fetchall.return_value = mock_rows

    with patch.dict("sys.modules", {"duckdb": mock_duckdb}):
        exit_code = main([
            "build-admin",
            "-o", str(output_db),
            "-c", "DE",
            "--admin-levels", "2",
        ])

    assert exit_code == 0
    assert output_db.exists()
