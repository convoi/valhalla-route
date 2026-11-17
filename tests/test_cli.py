"""CLI tests for ValhallaRoute."""
from pathlib import Path
from unittest.mock import patch
import pandas as pd

from valhallaroute.cli import main


def test_cli_execution(tmp_path: Path):
    input_file = tmp_path / "trip.csv"
    output_file = tmp_path / "enriched.csv"

    input_file.write_text("latitude,longitude\n52.5200,13.4050\n52.5205,13.4055\n", encoding="utf-8")

    mock_resp = {
        "admins": [{"country_text": "Germany"}],
        "edges": [{"names": ["Sample Road"], "road_class": "secondary", "admin_index": 0, "surface": "asphalt", "density": 8}],
        "matched_points": [{"type": "matched", "edge_index": 0}, {"type": "matched", "edge_index": 0}],
    }

    with patch("valhallaroute.client.HttpBackend.trace_attributes", return_value=mock_resp):
        exit_code = main([
            str(input_file),
            "-o", str(output_file),
            "--url", "http://fake-valhalla:8002",
        ])

    assert exit_code == 0
    assert output_file.exists()

    df = pd.read_csv(output_file)
    assert len(df) == 2
    assert "valhalla_type" in df.columns
    assert "is_on_ramp" in df.columns
    assert "is_exit_ramp" in df.columns
    assert "is_tunnel" in df.columns
    assert "is_bridge" in df.columns
    assert "is_crossing" in df.columns
    assert "city" in df.columns
    assert "county" in df.columns
    assert "country" in df.columns
    assert "road_name" in df.columns
    assert "road_type" in df.columns
    assert "surface" in df.columns
    assert "road_density" in df.columns
    assert "distance_to_next_intersection" in df.columns
    assert "intersection_edge_count" in df.columns
    assert "has_stop_sign" in df.columns
    assert "has_yield_sign" in df.columns
    assert "curvature" in df.columns
    assert df.loc[0, "road_name"] == "Sample Road"
    assert df.loc[0, "surface"] == "asphalt"
    assert df.loc[0, "road_density"] == 8
    assert df.loc[0, "valhalla_type"] == "matched"


def test_cli_custom_columns_and_preserve_coords(tmp_path: Path):
    """Test CLI with custom column mapping (--trip, --time, --lat, --lon), --admin-db, and --preserve-coords."""
    input_file = tmp_path / "custom_trip.csv"
    output_file = tmp_path / "custom_enriched.csv"
    admin_db = tmp_path / "dummy_admin.sqlite"
    admin_db.touch()

    input_file.write_text(
        "my_trip,my_time,my_lat,my_lon\n"
        "trip_1,2026-11-17T12:00:00Z,52.5200,13.4050\n"
        "trip_1,2026-11-17T12:00:05Z,52.5205,13.4055\n",
        encoding="utf-8",
    )

    mock_resp = {
        "admins": [{"country_text": "Germany"}],
        "edges": [{"names": ["Custom Road"], "road_class": "primary", "admin_index": 0}],
        "matched_points": [
            {"type": "matched", "edge_index": 0, "lat": 52.5201, "lon": 13.4051},
            {"type": "interpolated", "edge_index": 0, "lat": 52.5206, "lon": 13.4056},
        ],
    }

    with patch("valhallaroute.client.HttpBackend.trace_attributes", return_value=mock_resp), \
         patch("valhallaroute.enricher.lookup_coordinates") as mock_geo:
        mock_geo.return_value = [{"city": "Berlin"}, {"city": "Berlin"}]
        exit_code = main([
            str(input_file),
            "-o", str(output_file),
            "--url", "http://fake-valhalla:8002",
            "--lat", "my_lat",
            "--lon", "my_lon",
            "--time", "my_time",
            "--trip", "my_trip",
            "--admin-db", str(admin_db),
            "--preserve-coords",
        ])

    assert exit_code == 0
    assert output_file.exists()

    df = pd.read_csv(output_file)
    assert len(df) == 2

    # Original columns preserved
    assert "my_lat" in df.columns
    assert "my_lon" in df.columns
    assert df.loc[0, "my_lat"] == 52.5200
    assert df.loc[0, "my_lon"] == 13.4050

    # Renamed produced coordinates
    assert "valhalla_lat" in df.columns
    assert "valhalla_lon" in df.columns
    assert df.loc[0, "valhalla_lat"] == 52.5201
    assert df.loc[0, "valhalla_lon"] == 13.4051
    assert df.loc[1, "valhalla_lat"] == 52.5206
    assert df.loc[1, "valhalla_lon"] == 13.4056


def test_cli_overwrite_coords_by_default(tmp_path: Path):
    """Test CLI without --preserve-coords updates the coordinate columns with matched coordinates."""
    input_file = tmp_path / "trip_overwrite.csv"
    output_file = tmp_path / "enriched_overwrite.csv"

    input_file.write_text(
        "my_lat,my_lon\n"
        "52.5200,13.4050\n",
        encoding="utf-8",
    )

    mock_resp = {
        "admins": [{"country_text": "Germany"}],
        "edges": [{"names": ["Road"], "road_class": "primary"}],
        "matched_points": [
            {"type": "matched", "edge_index": 0, "lat": 52.5209, "lon": 13.4059},
        ],
    }

    with patch("valhallaroute.client.HttpBackend.trace_attributes", return_value=mock_resp), \
         patch("valhallaroute.enricher.lookup_coordinates", return_value=[{"city": "Berlin"}]):
        exit_code = main([
            str(input_file),
            "-o", str(output_file),
            "--url", "http://fake-valhalla:8002",
            "--lat-col", "my_lat",
            "--lon-col", "my_lon",
        ])

    assert exit_code == 0
    df = pd.read_csv(output_file)
    assert len(df) == 1
    # Overwritten with matched coordinates
    assert df.loc[0, "my_lat"] == 52.5209
    assert df.loc[0, "my_lon"] == 13.4059
    assert "valhalla_lat" not in df.columns

