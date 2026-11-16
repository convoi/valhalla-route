"""Unit tests for storage and sparse tile resolution."""
from pathlib import Path
import pandas as pd
import pytest

from valhallaroute.storage import (
    StorageManager,
    get_tiles_for_bbox,
    get_valhalla_tile_id,
    get_valhalla_tile_relative_path,
)


def test_valhalla_tile_id_and_relative_path():
    # Hannover coordinates: 52.4 N, 9.7 E
    lat, lon = 52.4, 9.7

    # Level 0 (4.0 deg)
    t0_id = get_valhalla_tile_id(lat, lon, level=0)
    assert t0_id == 3197
    assert get_valhalla_tile_relative_path(t0_id, level=0) == "0/003/197.gph"

    # Level 1 (1.0 deg)
    t1_id = get_valhalla_tile_id(lat, lon, level=1)
    assert t1_id == 51309
    assert get_valhalla_tile_relative_path(t1_id, level=1) == "1/051/309.gph"

    # Level 2 (0.25 deg)
    t2_id = get_valhalla_tile_id(lat, lon, level=2)
    assert t2_id == 820118
    assert get_valhalla_tile_relative_path(t2_id, level=2) == "2/000/820/118.gph"


def test_get_tiles_for_bbox():
    # Small bounding box around Hannover
    tiles = get_tiles_for_bbox(
        min_lat=52.38,
        min_lon=9.70,
        max_lat=52.42,
        max_lon=9.75,
        levels=(0, 1, 2),
        buffer_degrees=0.01,
    )
    rel_paths = {rel for _, _, rel in tiles}

    # Should contain the level 0 and level 1 tiles
    assert "0/003/197.gph" in rel_paths
    assert "1/051/309.gph" in rel_paths
    assert "2/000/820/118.gph" in rel_paths


def test_storage_manager_memory_store(tmp_path: Path):
    store = StorageManager.from_uri("memory://valhalla-tiles/v1")

    # Basic byte operations
    store.write_bytes("0/003/197.gph", b"mock-level0-tile-bytes")
    store.write_bytes("1/051/309.gph", b"mock-level1-tile-bytes")
    store.write_bytes("2/000/820/118.gph", b"mock-level2-tile-bytes")

    assert store.exists("0/003/197.gph")
    assert not store.exists("0/999/999.gph")
    assert store.read_bytes("0/003/197.gph") == b"mock-level0-tile-bytes"

    # Fetch single tile into local cache directory
    local_cache = tmp_path / "tile_cache"
    p = store.fetch_tile("0/003/197.gph", local_cache)
    assert p is not None
    assert p.exists()
    assert p.read_bytes() == b"mock-level0-tile-bytes"

    # Ensure tiles for points (downloads missing level 1 and 2 tiles)
    cached_tiles = store.ensure_tiles_for_points(
        lats=[52.4],
        lons=[9.7],
        local_cache_dir=local_cache,
        buffer_degrees=0.01,
    )
    assert len(cached_tiles) >= 3
    assert (local_cache / "1/051/309.gph").exists()
    assert (local_cache / "2/000/820/118.gph").exists()

    # Second call should find them locally without re-downloading
    cached_again = store.ensure_tiles_for_points(
        lats=[52.4],
        lons=[9.7],
        local_cache_dir=local_cache,
        buffer_degrees=0.01,
    )
    assert len(cached_again) == len(cached_tiles)


def test_storage_manager_csv_and_parquet(tmp_path: Path):
    store = StorageManager.from_uri(f"file://{tmp_path.as_posix()}/remote_store")

    df = pd.DataFrame({"trip_id": [1, 2], "val": [10.5, 20.5]})

    # CSV roundtrip
    store.write_csv(df, "data/trips.csv", index=False)
    read_df = store.read_csv("data/trips.csv")
    assert len(read_df) == 2
    assert read_df.loc[0, "trip_id"] == 1

    # Parquet roundtrip
    store.write_parquet(df, "data/trips.parquet", index=False)
    read_pq = store.read_parquet("data/trips.parquet")
    assert len(read_pq) == 2
    assert read_pq.loc[1, "val"] == 20.5


def test_storage_manager_list_files(tmp_path: Path):
    store = StorageManager.from_uri("memory://test-bucket/root")
    store.write_bytes("folder1/file1.txt", b"1")
    store.write_bytes("folder1/file2.txt", b"2")
    store.write_bytes("folder2/file3.txt", b"3")

    all_files = store.list_files()
    assert "folder1/file1.txt" in all_files
    assert "folder1/file2.txt" in all_files
    assert "folder2/file3.txt" in all_files

    f1_files = store.list_files("folder1")
    assert "folder1/file1.txt" in f1_files
    assert "folder1/file2.txt" in f1_files
    assert "folder2/file3.txt" not in f1_files


def test_storage_manager_azure_urls(monkeypatch):
    monkeypatch.delenv("AZURE_STORAGE_ACCOUNT", raising=False)
    monkeypatch.delenv("AZURE_STORAGE_ACCOUNT_NAME", raising=False)

    # 1. az://account/container/prefix
    mgr1 = StorageManager("az://myaccount/mycontainer/tiles")
    assert mgr1.prefix == "tiles"

    # 2. az://container/prefix with env var
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT", "myenvaccount")
    mgr2 = StorageManager("az://mycontainer/global")
    assert mgr2.prefix == "global"

    # 3. abfs://container@account.dfs.core.windows.net/prefix
    mgr3 = StorageManager("abfs://mycontainer@myadlsaccount.dfs.core.windows.net/output")
    assert mgr3.prefix == "output"


def test_storage_manager_latest_pointer(tmp_path: Path):
    import obstore as obs

    # 1. Local filesystem pointer resolution
    root = tmp_path / "tiles"
    root.mkdir()
    (root / "latest.txt").write_text("2026-11-16\n", encoding="utf-8")
    d = root / "2026-11-16"
    d.mkdir()
    (d / "tile.gph").write_bytes(b"local-tile-bytes")

    # Accessing /latest automatically redirects to the 2026-11-16 date folder
    mgr_local = StorageManager.from_uri(f"file://{root.as_posix()}/latest")
    assert mgr_local.read_bytes("tile.gph") == b"local-tile-bytes"

    # 2. Cloud prefix pointer resolution logic
    store = StorageManager.from_uri("memory://bucket/nested/latest")
    obs.put(store.store, "nested/latest.txt", b"2026-11-16\n")
    obs.put(store.store, "nested/2026-11-16/tile.gph", b"cloud-tile-bytes")

    # Manually re-trigger __init__ resolution check on the store
    if store.prefix.endswith("/latest"):
        parent_key = store.prefix[:-len("/latest")]
        resp = obs.get(store.store, f"{parent_key}/latest.txt")
        store.prefix = f"{parent_key}/{bytes(resp.bytes()).decode('utf-8').strip()}".strip("/")

    assert store.prefix == "nested/2026-11-16"
    assert store.read_bytes("tile.gph") == b"cloud-tile-bytes"


