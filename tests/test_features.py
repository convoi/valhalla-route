"""Unit tests for tile-aligned companion feature extractor and spatial index."""
import tempfile
from pathlib import Path
import numpy as np
import pandas as pd
import pytest

from valhallaroute.features import FeatureTileIndexer, extract_traffic_features_from_pbf
from valhallaroute.storage import (
    get_feature_tile_relative_path,
    get_valhalla_tile_id,
    StorageManager,
)


def test_get_feature_tile_relative_path():
    # Tile ID 818676 at Level 2
    rel_gph = "2/000/818/676.gph"
    rel_feat = get_feature_tile_relative_path(818676, level=2)
    assert rel_feat == "2/000/818/676.features.parquet"


def test_feature_tile_indexer_with_synthetic_tiles():
    with tempfile.TemporaryDirectory() as tmp_dir:
        t_dir = Path(tmp_dir)
        tile_id = get_valhalla_tile_id(52.52, 13.40, level=2)
        rel_path = get_feature_tile_relative_path(tile_id, level=2)
        feat_file = t_dir / rel_path
        feat_file.parent.mkdir(parents=True, exist_ok=True)

        # Create synthetic features within this tile
        df = pd.DataFrame([
            {
                "osm_id": 1001,
                "feature_type": "stop",
                "subtype": None,
                "direction": "forward",
                "latitude": 52.5200,
                "longitude": 13.4000,
            },
            {
                "osm_id": 1002,
                "feature_type": "give_way",
                "subtype": None,
                "direction": None,
                "latitude": 52.5250,
                "longitude": 13.4050,
            },
            {
                "osm_id": 1003,
                "feature_type": "crossing",
                "subtype": "zebra",
                "direction": None,
                "latitude": 52.5300,
                "longitude": 13.4100,
            },
            {
                "osm_id": 1004,
                "feature_type": "traffic_signals",
                "subtype": None,
                "direction": None,
                "latitude": 52.5350,
                "longitude": 13.4150,
            },
        ])
        df.to_parquet(feat_file, index=False)

        # Initialize indexer
        indexer = FeatureTileIndexer(tile_dir=t_dir, utm_crs="EPSG:32633")
        assert indexer.has_features is True

        # Query points:
        # Pt 0: ~5m from stop sign
        # Pt 1: ~5m from give_way sign
        # Pt 2: ~5m from crossing
        # Pt 3: Far away from everything
        q_lats = np.array([52.52004, 52.52504, 52.53004, 52.6000])
        q_lons = np.array([13.40004, 13.40504, 13.41004, 13.5000])

        res = indexer.query_points(q_lats, q_lons, radius_meters=15.0)

        # Pt 0: Stop sign
        assert res["has_stop_sign"][0] == True
        assert res["has_yield_sign"][0] == False
        assert res["is_crossing"][0] == False

        # Pt 1: Give way / yield
        assert res["has_stop_sign"][1] == False
        assert res["has_yield_sign"][1] == True

        # Pt 2: Zebra crossing
        assert res["is_crossing"][2] == True
        assert res["crossing_type"][2] == "zebra"

        # Pt 3: Far away
        assert res["has_stop_sign"][3] == False
        assert res["has_yield_sign"][3] == False
        assert res["is_crossing"][3] == False


def test_ensure_tiles_for_points_fetches_feature_companion():
    with tempfile.TemporaryDirectory() as remote_dir, tempfile.TemporaryDirectory() as cache_dir:
        r_path = Path(remote_dir)
        c_path = Path(cache_dir)

        mgr = StorageManager(f"file://{r_path.as_posix()}")

        tile_id = get_valhalla_tile_id(52.52, 13.40, level=2)
        rel_gph = f"2/000/{tile_id // 1000:03d}/{tile_id % 1000:03d}.gph"
        rel_feat = get_feature_tile_relative_path(tile_id, level=2)

        # Place fake .gph and fake .features.parquet in remote storage
        (r_path / rel_gph).parent.mkdir(parents=True, exist_ok=True)
        (r_path / rel_gph).write_bytes(b"dummy_gph_data")

        (r_path / rel_feat).parent.mkdir(parents=True, exist_ok=True)
        (r_path / rel_feat).write_bytes(b"dummy_parquet_data")

        cached = mgr.ensure_tiles_for_points(
            lats=[52.52],
            lons=[13.40],
            local_cache_dir=c_path,
            include_features=True,
        )

        assert (c_path / rel_gph).exists()
        assert (c_path / rel_feat).exists()
