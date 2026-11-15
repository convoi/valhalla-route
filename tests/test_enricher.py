"""Unit tests for ValhallaRoute enrichment."""
import io
import pandas as pd
import pytest

from valhallaroute.client import ActorBackend, HttpBackend, ValhallaBackend
from valhallaroute.enricher import enrich_gps_points, read_gps_csv


class MockBackend(ValhallaBackend):
    def __init__(self, trace_attributes_response: dict):
        self.response = trace_attributes_response
        self.calls = []

    def trace_attributes(self, payload: dict) -> dict:
        self.calls.append(payload)
        return self.response


def test_read_gps_csv_auto_detect_columns():
    csv_data = """lat,lon,other_info
52.52,13.40,test1
52.53,13.41,test2
"""
    df, lat_col, lon_col, time_col = read_gps_csv(io.StringIO(csv_data))
    assert lat_col == "lat"
    assert lon_col == "lon"
    assert time_col is None
    assert len(df) == 2


def test_read_gps_csv_case_insensitive_and_alternate_names():
    csv_data = """Latitude,LONGITUDE,Timestamp
48.13,11.58,2026-01-01T00:00:00Z
"""
    df, lat_col, lon_col, time_col = read_gps_csv(io.StringIO(csv_data))
    assert lat_col == "Latitude"
    assert lon_col == "LONGITUDE"
    assert time_col == "Timestamp"


def test_enrich_with_mock_backend():
    csv_data = """lat,lon
52.5200,13.4050
52.5210,13.4060
52.5220,13.4070
"""
    mock_resp = {
        "admins": [
            {"country_code": "DE", "country_text": "Germany", "state_code": "BE"}
        ],
        "edges": [
            {
                "names": ["Tunnelway"],
                "road_class": "motorway",
                "tunnel": True,
                "bridge": False,
                "country_crossing": False,
                "admin_index": 0,
            },
            {
                "names": ["Bridgestreet"],
                "road_class": "primary",
                "tunnel": False,
                "bridge": True,
                "country_crossing": True,
                "admin_index": 0,
            },
        ],
        "matched_points": [
            {"type": "matched", "edge_index": 0},
            {"type": "matched", "edge_index": 1},
            {"type": "unmatched"},
        ],
    }

    mock_backend = MockBackend(mock_resp)

    res_df = enrich_gps_points(
        io.StringIO(csv_data),
        backend=mock_backend,
    )

    assert len(res_df) == 3
    # Check column existence
    assert "is_tunnel" in res_df.columns
    assert "is_bridge" in res_df.columns
    assert "is_crossing" in res_df.columns
    assert "crossing_type" in res_df.columns
    assert "is_intersection" in res_df.columns
    assert "intersection_type" in res_df.columns
    assert "city" in res_df.columns
    assert "country" in res_df.columns
    assert "road_name" in res_df.columns
    assert "road_type" in res_df.columns
    assert "road name" not in res_df.columns
    assert "road type" not in res_df.columns

    # Point 0: Tunnel
    assert bool(res_df.loc[0, "is_tunnel"]) is True
    assert bool(res_df.loc[0, "is_bridge"]) is False
    assert bool(res_df.loc[0, "is_crossing"]) is False
    assert res_df.loc[0, "road_name"] == "Tunnelway"
    assert res_df.loc[0, "road_type"] == "motorway"
    assert res_df.loc[0, "country"] == "Germany"

    # Point 1: Bridge & border crossing
    assert bool(res_df.loc[1, "is_tunnel"]) is False
    assert bool(res_df.loc[1, "is_bridge"]) is True
    assert bool(res_df.loc[1, "is_crossing"]) is True
    assert res_df.loc[1, "crossing_type"] == "border_crossing"
    assert res_df.loc[1, "road_name"] == "Bridgestreet"
    assert res_df.loc[1, "road_type"] == "primary"

    # Point 2: Unmatched
    assert bool(res_df.loc[2, "is_tunnel"]) is False
    assert bool(res_df.loc[2, "is_bridge"]) is False
    assert bool(res_df.loc[2, "is_crossing"]) is False
    assert pd.isna(res_df.loc[2, "road_name"]) or res_df.loc[2, "road_name"] is None
    assert pd.isna(res_df.loc[2, "road_type"]) or res_df.loc[2, "road_type"] is None
    # Offline geocoding should still populate city & country for Berlin coords
    assert res_df.loc[2, "city"] is not None
    assert res_df.loc[2, "country"] is not None


def test_empty_dataframe():
    empty_df = pd.DataFrame(columns=["lat", "lon"])
    res = enrich_gps_points(empty_df, backend=MockBackend({}))
    assert len(res) == 0
    assert "is_tunnel" in res.columns
    assert "is_bridge" in res.columns
    assert "is_crossing" in res.columns
    assert "crossing_type" in res.columns
    assert "is_intersection" in res.columns
    assert "intersection_type" in res.columns
    assert "city" in res.columns
    assert "country" in res.columns
    assert "road_name" in res.columns
    assert "road_type" in res.columns


def test_single_point_graceful():
    single_csv = "lat,lon\n52.52,13.40\n"
    mock_resp = {
        "admins": [{"country_text": "Germany"}],
        "edges": [{"names": ["Solo Road"], "road_class": "residential", "admin_index": 0}],
        "matched_points": [{"type": "matched", "edge_index": 0}, {"type": "matched", "edge_index": 0}],
    }
    res = enrich_gps_points(io.StringIO(single_csv), backend=MockBackend(mock_resp))
    assert len(res) == 1
    assert res.loc[0, "road_name"] == "Solo Road"


def test_chunking_execution():
    # Test that chunking breaks down requests properly
    points_csv = "\n".join(["lat,lon"] + [f"52.{i:04d},13.{i:04d}" for i in range(15)])
    mock_resp = {
        "admins": [{"country_text": "Germany"}],
        "edges": [{"names": ["Road"], "road_class": "secondary", "admin_index": 0}],
        "matched_points": [{"type": "matched", "edge_index": 0}] * 15,
    }
    mock_backend = MockBackend(mock_resp)
    res = enrich_gps_points(
        io.StringIO(points_csv),
        backend=mock_backend,
        chunk_size=10,
    )
    assert len(res) == 15
    # Should have called backend multiple times due to chunk_size=10
    assert len(mock_backend.calls) >= 2


def test_actor_backend_mock_actor():
    class DummyActor:
        def trace_attributes(self, req):
            return {
                "admins": [],
                "edges": [],
                "matched_points": [{"type": "unmatched"}, {"type": "unmatched"}],
            }

    actor = DummyActor()
    backend = ActorBackend(actor=actor)
    csv_data = "lat,lon\n52.52,13.40\n52.53,13.41\n"
    res = enrich_gps_points(io.StringIO(csv_data), backend=backend)
    assert len(res) == 2
    assert res.loc[0, "city"] is not None


def test_intersection_detection():
    # 2 points on an edge of 100m length (0.1km)
    # Point 0 is 95m along edge (5m from end node) -> within 10m buffer of intersection
    # Point 1 is 50m along edge (50m from both nodes) -> NOT an intersection
    mock_resp = {
        "admins": [{"country_text": "Germany"}],
        "edges": [
            {
                "length": 0.1,  # 100 meters
                "end_node": {"type": "street_intersection", "traffic_signal": True},
            }
        ],
        "matched_points": [
            {"type": "matched", "edge_index": 0, "distance_along_edge": 0.95},
            {"type": "matched", "edge_index": 0, "distance_along_edge": 0.50},
        ],
    }
    csv_data = "lat,lon\n52.52,13.40\n52.521,13.401\n"
    res = enrich_gps_points(
        io.StringIO(csv_data),
        backend=MockBackend(mock_resp),
        intersection_buffer_meters=10.0,
    )
    assert bool(res.loc[0, "is_intersection"]) is True
    assert res.loc[0, "intersection_type"] == "traffic_signals"
    assert bool(res.loc[1, "is_intersection"]) is False
    assert pd.isna(res.loc[1, "intersection_type"]) or res.loc[1, "intersection_type"] is None


def test_crossing_indexer():
    from valhallaroute.crossings import CrossingIndexer

    # Mock crossings list: (lat, lon, type)
    crossings = [
        (52.52000, 13.40500, "zebra"),
        (52.52500, 13.40500, "traffic_signals"),
    ]
    indexer = CrossingIndexer(crossings=crossings, utm_crs="EPSG:32633")

    # Point very close to zebra crossing (~2m away)
    # Point far away from any crossing (~100m away)
    lats = [52.52002, 52.52100]
    lons = [13.40500, 13.40500]

    is_c, c_types = indexer.query_points(lats, lons, radius_meters=10.0)
    assert is_c[0] is True
    assert c_types[0] == "zebra"
    assert is_c[1] is False
    assert c_types[1] is None
