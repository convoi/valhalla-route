"""Unit and integration tests for distributed batch job planning and execution."""
import json
import os
from pathlib import Path

import pandas as pd
import pytest

from valhallaroute.batch import plan_batch, run_worker
from valhallaroute.cli import main
from valhallaroute.storage import StorageManager


def test_plan_batch_single_file_trip_split(tmp_path: Path):
    # Create sample CSV with 3 trips
    df = pd.DataFrame({
        "trip_id": [101, 101, 102, 102, 103, 103],
        "latitude": [52.4, 52.41, 52.42, 52.43, 52.44, 52.45],
        "longitude": [9.7, 9.71, 9.72, 9.73, 9.74, 9.75],
    })
    input_csv = tmp_path / "raw_trips.csv"
    df.to_csv(input_csv, index=False)

    manifest_path = tmp_path / "manifest.jsonl"
    out_prefix = f"file://{tmp_path.as_posix()}/enriched"

    # Batch size 1 -> 3 tasks
    tasks = plan_batch(
        input_uri=str(input_csv),
        output_manifest_uri=str(manifest_path),
        output_prefix=out_prefix,
        tile_storage_uri="s3://valhalla-tiles/v1",
        batch_size=1,
        trip_col="trip_id",
        output_ext="parquet",
    )

    assert len(tasks) == 3
    assert tasks[0]["trip_ids"] == [101]
    assert tasks[1]["trip_ids"] == [102]
    assert tasks[2]["trip_ids"] == [103]
    assert tasks[0]["output_uri"].endswith("part_00000.parquet")
    assert tasks[0]["tile_storage_uri"] == "s3://valhalla-tiles/v1"

    # Verify manifest file on disk
    assert manifest_path.exists()
    lines = manifest_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    t0 = json.loads(lines[0])
    assert t0["task_id"] == 0
    assert t0["trip_ids"] == [101]


def test_plan_batch_multiple_files(tmp_path: Path):
    in_dir = tmp_path / "incoming"
    in_dir.mkdir()
    (in_dir / "trip_a.csv").write_text("trip_id,latitude,longitude\n1,52.4,9.7\n")
    (in_dir / "trip_b.csv").write_text("trip_id,latitude,longitude\n2,52.4,9.7\n")
    (in_dir / "trip_c.csv").write_text("trip_id,latitude,longitude\n3,52.4,9.7\n")

    manifest_path = tmp_path / "manifest.jsonl"
    out_prefix = f"file://{tmp_path.as_posix()}/enriched"

    # Batch size 2 -> 2 tasks
    tasks = plan_batch(
        input_uri=str(in_dir),
        output_manifest_uri=str(manifest_path),
        output_prefix=out_prefix,
        batch_size=2,
        output_ext="csv",
    )

    assert len(tasks) == 2
    assert len(tasks[0]["input_uris"]) == 2
    assert len(tasks[1]["input_uris"]) == 1
    assert tasks[0]["output_uri"].endswith("part_00000.csv")


def test_run_worker_with_env_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Setup test data
    df = pd.DataFrame({
        "trip_id": [1, 1, 2, 2],
        "latitude": [52.4, 52.401, 52.5, 52.501],
        "longitude": [9.7, 9.701, 9.8, 9.801],
    })
    input_file = tmp_path / "trips.parquet"
    df.to_parquet(input_file, index=False)

    manifest_file = tmp_path / "tasks.jsonl"
    out_0 = tmp_path / "output_0.parquet"
    out_1 = tmp_path / "output_1.parquet"

    tasks = [
        {
            "task_id": 0,
            "input_uri": str(input_file),
            "trip_ids": [1],
            "trip_col": "trip_id",
            "output_uri": str(out_0),
        },
        {
            "task_id": 1,
            "input_uri": str(input_file),
            "trip_ids": [2],
            "trip_col": "trip_id",
            "output_uri": str(out_1),
        },
    ]
    manifest_file.write_text("\n".join(json.dumps(t) for t in tasks) + "\n")

    # Mock enrich_gps_points to avoid requiring actual Valhalla tiles in this fast unit test
    def mock_enrich_gps_points(csv_input, **kwargs):
        if isinstance(csv_input, pd.DataFrame):
            df_res = csv_input.copy()
        else:
            df_res = pd.read_parquet(csv_input)
        df_res["road_name"] = "Mock Road"
        df_res["speed_limit_kmh"] = 50.0
        return df_res

    monkeypatch.setattr("valhallaroute.batch.enrich_gps_points", mock_enrich_gps_points)
    monkeypatch.setenv("JOB_COMPLETION_INDEX", "1")

    # Run worker without explicit task_index -> reads from env var 1
    result_df = run_worker(manifest_uri=str(manifest_file))

    assert len(result_df) == 2
    assert (result_df["trip_id"] == 2).all()
    assert out_1.exists()
    saved = pd.read_parquet(out_1)
    assert len(saved) == 2
    assert "road_name" in saved.columns


def test_cli_plan_batch_and_run_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    df = pd.DataFrame({
        "trip_id": [1, 2],
        "latitude": [52.4, 52.5],
        "longitude": [9.7, 9.8],
    })
    input_file = tmp_path / "cli_trips.csv"
    df.to_csv(input_file, index=False)

    manifest_file = tmp_path / "cli_manifest.jsonl"
    out_prefix = f"file://{tmp_path.as_posix()}/cli_out"

    # Test plan-batch via CLI
    ret = main([
        "plan-batch",
        "-i", str(input_file),
        "-m", str(manifest_file),
        "-o", out_prefix,
        "--batch-size", "1",
        "--trip-col", "trip_id",
    ])
    assert ret == 0
    assert manifest_file.exists()

    def mock_enrich_gps_points(csv_input, **kwargs):
        df_res = csv_input.copy() if isinstance(csv_input, pd.DataFrame) else pd.read_csv(csv_input)
        df_res["enriched"] = True
        return df_res

    monkeypatch.setattr("valhallaroute.batch.enrich_gps_points", mock_enrich_gps_points)

    # Test run-worker via CLI
    ret = main([
        "run-worker",
        "-m", str(manifest_file),
        "--task-index", "0",
    ])
    assert ret == 0
