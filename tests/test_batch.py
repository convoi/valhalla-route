"""Unit and integration tests for distributed region-based tile batch planning and execution."""
import json
import os
from pathlib import Path

import pytest

from valhallaroute.batch import plan_batch, resolve_regions, run_worker
from valhallaroute.cli import main
from valhallaroute.storage import StorageManager


def test_resolve_regions_single_and_wildcard():
    # Test resolving explicit region
    resolved = resolve_regions(["bayern", "berlin"])
    ids = [r["id"] for r in resolved]
    assert "bayern" in ids
    assert "berlin" in ids

    # Test wildcard expansion: germany/* expands to all 16 federal states
    wildcard_res = resolve_regions(["germany/*"])
    w_ids = [r["id"] for r in wildcard_res]
    assert len(w_ids) == 16
    assert "bayern" in w_ids
    assert "berlin" in w_ids
    assert "hamburg" in w_ids


def test_plan_batch_with_regions(tmp_path: Path):
    manifest_path = tmp_path / "manifest.jsonl"
    tile_store_uri = f"file://{tmp_path.as_posix()}/tiles_out"

    tasks = plan_batch(
        regions=["bayern", "berlin"],
        output_manifest_uri=str(manifest_path),
        tile_storage_uri=tile_store_uri,
    )

    assert len(tasks) == 2
    assert tasks[0]["region"] == "bayern"
    assert tasks[0]["osm_url"].endswith("bayern-latest.osm.pbf")
    assert tasks[0]["tile_storage_uri"].endswith("tiles_out/")
    assert tasks[1]["region"] == "berlin"

    # Verify manifest JSONL on disk
    assert manifest_path.exists()
    lines = manifest_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    t0 = json.loads(lines[0])
    assert t0["task_id"] == 0
    assert t0["region"] == "bayern"


def test_plan_batch_with_regions_file(tmp_path: Path):
    regions_file = tmp_path / "regions.txt"
    regions_file.write_text(
        "# List of European regions\n"
        "monaco\n"
        "\n"
        "# Germany capital\n"
        "berlin\n"
    )

    manifest_path = tmp_path / "manifest_file.jsonl"
    tasks = plan_batch(
        regions_file=str(regions_file),
        output_manifest_uri=str(manifest_path),
        tile_storage_uri="s3://valhalla-tiles/global/",
        bin_pack=False,
    )

    assert len(tasks) == 2
    regions = [t["region"] for t in tasks]
    assert "monaco" in regions
    assert "berlin" in regions


def test_run_worker_with_env_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tile_output_dir = tmp_path / "global_tiles"
    tile_output_dir.mkdir()

    manifest_file = tmp_path / "tasks.jsonl"
    tasks = [
        {
            "task_id": 0,
            "task_type": "build_tiles",
            "region": "mock_region",
            "osm_url": "https://example.com/mock.osm.pbf",
            "tile_storage_uri": f"file://{tile_output_dir.as_posix()}",
        }
    ]
    manifest_file.write_text(json.dumps(tasks[0]) + "\n")

    # Mock download_file to create a dummy PBF
    def mock_download_file(url, dest_path, **kwargs):
        p = Path(dest_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"mock-pbf-content")
        return p

    # Mock build_valhalla_tiles to create dummy .gph tiles
    def mock_build_valhalla_tiles(osm_pbf_path, tile_dir, **kwargs):
        td = Path(tile_dir)
        tile1 = td / "0" / "003" / "197.gph"
        tile2 = td / "1" / "051" / "309.gph"
        tile1.parent.mkdir(parents=True, exist_ok=True)
        tile2.parent.mkdir(parents=True, exist_ok=True)
        tile1.write_bytes(b"mock-tile-0")
        tile2.write_bytes(b"mock-tile-1")

    monkeypatch.setattr("valhallaroute.batch.download_file", mock_download_file)
    monkeypatch.setattr("valhallaroute.batch.build_valhalla_tiles", mock_build_valhalla_tiles)
    monkeypatch.setenv("JOB_COMPLETION_INDEX", "0")

    summary = run_worker(manifest_uri=str(manifest_file), verbose=False)

    assert summary["task_id"] == 0
    assert summary["region"] == "mock_region"
    assert summary["tiles_count"] == 2

    # Verify tiles were uploaded/copied to tile_output_dir
    assert (tile_output_dir / "0" / "003" / "197.gph").exists()
    assert (tile_output_dir / "1" / "051" / "309.gph").exists()


def test_cli_plan_batch_and_run_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manifest_file = tmp_path / "cli_manifest.jsonl"
    tile_output_dir = tmp_path / "cli_tiles"

    # Test plan-batch via CLI
    ret = main([
        "plan-batch",
        "--regions", "monaco", "berlin",
        "-m", str(manifest_file),
        "-o", f"file://{tile_output_dir.as_posix()}",
    ])
    assert ret == 0
    assert manifest_file.exists()

    # Mock worker execution functions
    def mock_download_file(url, dest_path, **kwargs):
        p = Path(dest_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"mock-pbf")
        return p

    def mock_build_valhalla_tiles(osm_pbf_path, tile_dir, **kwargs):
        td = Path(tile_dir)
        t = td / "0" / "000" / "001.gph"
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_bytes(b"data")

    monkeypatch.setattr("valhallaroute.batch.download_file", mock_download_file)
    monkeypatch.setattr("valhallaroute.batch.build_valhalla_tiles", mock_build_valhalla_tiles)

    # Test run-worker via CLI with explicit --task-index
    ret = main([
        "run-worker",
        "-m", str(manifest_file),
        "--task-index", "0",
    ])
    assert ret == 0
    assert (tile_output_dir / "0" / "000" / "001.gph").exists()


def test_auto_subdivide_and_bin_pack(tmp_path: Path):
    manifest_path = tmp_path / "balanced_manifest.jsonl"
    # When germany is requested with auto_subdivide=True and max_tiles=300:
    # Germany has 1,216 tiles, so it will subdivide into its 16 Bundesländer.
    # Bremen, Saarland, Berlin, etc. have < 50 tiles, so bin-packing will group them.
    tasks = plan_batch(
        regions=["germany"],
        output_manifest_uri=str(manifest_path),
        tile_storage_uri="s3://valhalla-tiles/global/",
        auto_subdivide=True,
        max_tiles_per_job=300,
        bin_pack=True,
        target_tiles_per_job=200,
        min_tiles_per_job=50,
        verbose=False,
    )

    # All 16 states are accounted for, but bundled into fewer than 16 tasks due to bin packing!
    all_regions = []
    for t in tasks:
        all_regions.extend(t["regions"])
    assert len(all_regions) == 16
    assert len(tasks) < 16  # Small states were bin-packed together!
    assert "bayern" in all_regions
    assert "bremen" in all_regions


def test_plan_batch_with_admin_boundaries(tmp_path: Path):
    manifest_path = tmp_path / "admin_manifest.jsonl"
    tile_storage = tmp_path / "tiles_storage"
    tile_storage.mkdir()

    # Mock build_admin_sqlite_from_overture
    def mock_build_admin(output_path, **kwargs):
        p = Path(output_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"mock-admin-sqlite")
        return {"output_path": str(p), "total_records": 1}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("valhallaroute.batch.build_admin_sqlite_from_overture", mock_build_admin)
        tasks = plan_batch(
            regions=["bayern"],
            output_manifest_uri=str(manifest_path),
            tile_storage_uri=f"file://{tile_storage.as_posix()}",
            build_admins=True,
            admin_countries=["DE"],
            verbose=False,
        )

    assert len(tasks) == 1
    assert "admin_db_uri" in tasks[0]
    assert tasks[0]["admin_db_uri"].endswith("admin.sqlite")
    assert (tile_storage / "admin.sqlite").exists()
    assert (tile_storage / "admin.sqlite").read_bytes() == b"mock-admin-sqlite"


def test_worker_with_admin_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tile_output = tmp_path / "tiles_out"
    tile_output.mkdir()
    admin_file = tmp_path / "source_admin.sqlite"
    admin_file.write_bytes(b"admin-db-data")

    task = {
        "task_id": 0,
        "task_type": "build_tiles",
        "regions": ["bremen", "hamburg"],
        "osm_urls": ["https://example.com/bremen.pbf", "https://example.com/hamburg.pbf"],
        "admin_db_uri": f"file://{admin_file.as_posix()}",
        "tile_storage_uri": f"file://{tile_output.as_posix()}",
    }
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(json.dumps(task) + "\n")

    captured_build_args = {}

    def mock_download(url, dest_path, **kwargs):
        p = Path(dest_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"fake-pbf")
        return p

    def mock_build(osm_pbf_path, tile_dir, admin_db_path=None, **kwargs):
        captured_build_args["pbfs"] = osm_pbf_path
        captured_build_args["admin_db"] = admin_db_path
        t = Path(tile_dir) / "0" / "000" / "001.gph"
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_bytes(b"tile-data")

    monkeypatch.setattr("valhallaroute.batch.download_file", mock_download)
    monkeypatch.setattr("valhallaroute.batch.build_valhalla_tiles", mock_build)
    monkeypatch.setenv("JOB_COMPLETION_INDEX", "0")

    summary = run_worker(manifest_uri=str(manifest), verbose=False)

    assert summary["tiles_count"] == 1
    assert len(captured_build_args["pbfs"]) == 2  # Multi-PBF bin packed!
    assert captured_build_args["admin_db"] is not None
    assert captured_build_args["admin_db"].read_bytes() == b"admin-db-data"

