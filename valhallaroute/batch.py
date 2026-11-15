"""Batch job planner and indexed worker execution for distributed cloud workflows."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from .enricher import enrich_gps_points, read_gps_csv
from .storage import StorageManager, uri_to_local_path


def plan_batch(
    input_uri: str,
    output_manifest_uri: str,
    output_prefix: str,
    tile_storage_uri: str | None = None,
    batch_size: int = 1,
    trip_col: str | None = None,
    output_ext: str = "parquet",
    costing: str = "auto",
) -> list[dict[str, Any]]:
    """
    Generate an indexed task manifest (JSONL) for distributed Kubernetes batch processing.

    Args:
        input_uri: URI or path to input directory, prefix, or single CSV/Parquet file.
        output_manifest_uri: Destination URI or path for the generated JSONL manifest.
        output_prefix: Destination blob prefix or directory for enriched files.
        tile_storage_uri: Remote blob URI containing Valhalla .gph tiles.
        batch_size: Number of files or trips per indexed task chunk (default: 1).
        trip_col: Optional trip column name (if splitting trips within a single file).
        output_ext: Output file extension, 'parquet' or 'csv' (default: 'parquet').
        costing: Valhalla costing model (default: 'auto').

    Returns:
        List of generated task dictionaries.
    """
    is_remote_input = "://" in input_uri and not input_uri.startswith("file://")
    discovered_files: list[str] = []

    # Check if input is a single file or a collection of files
    if is_remote_input:
        in_mgr = StorageManager.from_uri(input_uri)
        if input_uri.lower().endswith((".csv", ".parquet", ".pq")):
            discovered_files = [input_uri]
        else:
            files = in_mgr.list_files()
            valid_exts = (".csv", ".parquet", ".pq")
            matching = [f for f in files if f.lower().endswith(valid_exts)]
            # Reconstruct full URIs
            base = input_uri.rstrip("/")
            discovered_files = [f"{base}/{f}" for f in matching]
    else:
        p = Path(input_uri)
        if p.is_file():
            discovered_files = [str(p.resolve())]
        elif p.is_dir():
            valid_exts = (".csv", ".parquet", ".pq")
            files = [str(f.resolve()) for f in p.iterdir() if f.is_file() and f.name.lower().endswith(valid_exts)]
            discovered_files = sorted(files)
        else:
            # File might not exist or be a glob
            discovered_files = [str(p)]

    if not discovered_files:
        raise ValueError(f"No valid input files found at '{input_uri}'")

    tasks: list[dict[str, Any]] = []
    task_id = 0
    clean_out_prefix = output_prefix.rstrip("/")

    # If single file and trip_col is provided, split by trip_ids
    if len(discovered_files) == 1 and trip_col:
        single_file = discovered_files[0]
        df, _, _, _ = read_gps_csv(single_file)
        if trip_col in df.columns:
            unique_trips = df[trip_col].dropna().unique().tolist()
            # Chunk unique trips by batch_size
            for i in range(0, len(unique_trips), max(1, batch_size)):
                trip_chunk = unique_trips[i : i + max(1, batch_size)]
                task = {
                    "task_id": task_id,
                    "input_uri": single_file,
                    "input_uris": [single_file],
                    "trip_ids": trip_chunk,
                    "trip_col": trip_col,
                    "output_uri": f"{clean_out_prefix}/part_{task_id:05d}.{output_ext}",
                    "costing": costing,
                }
                if tile_storage_uri:
                    task["tile_storage_uri"] = tile_storage_uri
                tasks.append(task)
                task_id += 1

    # Otherwise partition by files
    if not tasks:
        for i in range(0, len(discovered_files), max(1, batch_size)):
            file_chunk = discovered_files[i : i + max(1, batch_size)]
            task = {
                "task_id": task_id,
                "input_uri": file_chunk[0],
                "input_uris": file_chunk,
                "output_uri": f"{clean_out_prefix}/part_{task_id:05d}.{output_ext}",
                "costing": costing,
            }
            if trip_col:
                task["trip_col"] = trip_col
            if tile_storage_uri:
                task["tile_storage_uri"] = tile_storage_uri
            tasks.append(task)
            task_id += 1

    # Serialize manifest to JSONL
    manifest_lines = [json.dumps(t) for t in tasks]
    manifest_content = "\n".join(manifest_lines) + "\n"

    local_manifest = uri_to_local_path(output_manifest_uri)
    if local_manifest is not None:
        local_manifest.parent.mkdir(parents=True, exist_ok=True)
        local_manifest.write_text(manifest_content, encoding="utf-8")
    else:
        out_mgr = StorageManager.from_uri(output_manifest_uri)
        out_mgr.write_bytes("", manifest_content.encode("utf-8"))

    return tasks


def run_worker(
    manifest_uri: str,
    task_index: int | None = None,
    tile_storage_uri: str | None = None,
    tile_cache_dir: str | Path = "valhalla_tiles_cache",
    costing: str = "auto",
    utm_crs: str = "EPSG:32632",
    crossing_buffer_meters: float = 10.0,
    intersection_buffer_meters: float = 10.0,
    roundabout_buffer_meters: float = 10.0,
    trip_col: str | None = None,
    lat_col: str | None = None,
    lon_col: str | None = None,
    auto_build_tiles: bool = True,
    osm_pbf: str | None = None,
    osm_url: str | None = None,
    region: str | None = None,
) -> pd.DataFrame:
    """
    Execute a single task from the manifest corresponding to task_index (or $JOB_COMPLETION_INDEX).

    Args:
        manifest_uri: Destination URI or path to the JSONL manifest file.
        task_index: Zero-indexed integer task number. If None, read from JOB_COMPLETION_INDEX env var.
        tile_storage_uri: Remote blob URI containing Valhalla .gph tiles.
        tile_cache_dir: Local path to cache Valhalla tiles.
        costing: Valhalla costing model.
        utm_crs: Coordinate reference system for UTM projection.
        crossing_buffer_meters: Search radius around OSM crossings.
        intersection_buffer_meters: Search radius around intersections.
        roundabout_buffer_meters: Search radius around roundabouts.
        trip_col: Trip column name override.
        lat_col: Latitude column name override.
        lon_col: Longitude column name override.
        auto_build_tiles: Enable auto OSM download if tiles are missing.

    Returns:
        The enriched pandas DataFrame for this task.
    """
    if task_index is None:
        env_val = (
            os.environ.get("JOB_COMPLETION_INDEX")
            or os.environ.get("TASK_INDEX")
            or os.environ.get("BATCH_INDEX")
        )
        if env_val is None:
            raise ValueError(
                "task_index was not provided and neither JOB_COMPLETION_INDEX, "
                "TASK_INDEX, nor BATCH_INDEX is set in the environment."
            )
        task_index = int(env_val)

    # Read manifest
    local_manifest = uri_to_local_path(manifest_uri)
    if local_manifest is not None:
        raw_manifest = local_manifest.read_text(encoding="utf-8")
    else:
        mgr = StorageManager.from_uri(manifest_uri)
        raw_manifest = mgr.read_bytes("").decode("utf-8")

    lines = [line.strip() for line in raw_manifest.splitlines() if line.strip()]
    if not lines:
        raise ValueError(f"Manifest at '{manifest_uri}' is empty.")
    if task_index < 0 or task_index >= len(lines):
        raise IndexError(
            f"task_index {task_index} is out of range. Manifest contains {len(lines)} tasks (0 to {len(lines) - 1})."
        )

    task = json.loads(lines[task_index])
    print(f"[ValhallaRoute Worker {task_index}] Loaded task: {task}")

    # Read input data
    input_uris = task.get("input_uris") or task.get("input_uri")
    if not input_uris:
        raise ValueError(f"Task {task_index} has no input_uri or input_uris specified.")

    if isinstance(input_uris, str):
        input_list = [input_uris]
    else:
        input_list = list(input_uris)

    dfs: list[pd.DataFrame] = []
    for u in input_list:
        part_df, _, _, _ = read_gps_csv(u)
        dfs.append(part_df)
    df = pd.concat(dfs, ignore_index=True)

    # Filter trips if specified
    assigned_trips = task.get("trip_ids")
    eff_trip_col = task.get("trip_col") or trip_col
    if assigned_trips and eff_trip_col:
        if eff_trip_col in df.columns:
            df = df[df[eff_trip_col].isin(assigned_trips)].copy()

    if df.empty:
        print(f"[ValhallaRoute Worker {task_index}] Warning: Input data is empty after filtering.")
        return df

    eff_tile_uri = task.get("tile_storage_uri") or tile_storage_uri
    eff_costing = task.get("costing") or costing

    enriched_df = enrich_gps_points(
        csv_input=df,
        tile_storage_uri=eff_tile_uri,
        tile_cache_dir=tile_cache_dir,
        costing=eff_costing,
        lat_col=lat_col,
        lon_col=lon_col,
        trip_col=eff_trip_col,
        utm_crs=utm_crs,
        crossing_buffer_meters=crossing_buffer_meters,
        intersection_buffer_meters=intersection_buffer_meters,
        roundabout_buffer_meters=roundabout_buffer_meters,
        auto_build_tiles=auto_build_tiles,
        osm_pbf=osm_pbf,
        osm_url=osm_url,
        region=region,
    )

    # Write output
    output_uri = task.get("output_uri")
    if output_uri:
        is_parquet = output_uri.lower().endswith((".parquet", ".pq"))
        local_out = uri_to_local_path(output_uri)
        if local_out is not None:
            local_out.parent.mkdir(parents=True, exist_ok=True)
            if is_parquet:
                enriched_df.to_parquet(local_out, index=False)
            else:
                enriched_df.to_csv(local_out, index=False)
        else:
            out_mgr = StorageManager.from_uri(output_uri)
            if is_parquet:
                out_mgr.write_parquet(enriched_df, "", index=False)
            else:
                out_mgr.write_csv(enriched_df, "", index=False)

        print(
            f"[ValhallaRoute Worker {task_index}] Successfully wrote {len(enriched_df)} enriched points to {output_uri}"
        )

    return enriched_df
