"""Batch job planner and indexed worker execution for continental/region-wise tile generation."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence

from .storage import StorageManager, uri_to_local_path
from .tile_manager import build_valhalla_tiles, download_file, fetch_geofabrik_index


def resolve_regions(
    region_queries: Sequence[str],
    index: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Resolve a sequence of region queries or wildcards into matching Geofabrik features.

    Supports:
    - Region id / slug: 'bayern', 'berlin', 'monaco'
    - Canonical path / name: 'Bayern', 'europe/germany/bayern'
    - Wildcards: 'germany/*', 'germany*', 'europe/*' expanding to all subregions where parent == base
    """
    if index is None:
        index = fetch_geofabrik_index()

    features = index.get("features", [])
    resolved: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for q in region_queries:
        q_clean = q.lower().strip()
        if not q_clean:
            continue

        is_wildcard = q_clean.endswith("/*") or q_clean.endswith("*")
        base = q_clean.rstrip("*").rstrip("/")

        matched_any = False
        for feat in features:
            props = feat.get("properties", {})
            fid = props.get("id", "").lower()
            fname = props.get("name", "").lower()
            parent = props.get("parent", "").lower()

            if is_wildcard:
                if parent == base or fid.startswith(base + "/") or f"/{base}/" in fid:
                    if fid not in seen_ids:
                        seen_ids.add(fid)
                        resolved.append(props)
                        matched_any = True
            else:
                if q_clean in (fid, fname) or f"/{q_clean}" in fid or fid.endswith(f"/{q_clean}"):
                    if fid not in seen_ids:
                        seen_ids.add(fid)
                        resolved.append(props)
                        matched_any = True
                    break

        if not matched_any and not is_wildcard:
            raise ValueError(f"Could not find any Geofabrik region matching '{q}' in index.")

    return resolved


def plan_batch(
    regions: Sequence[str] | None = None,
    regions_file: str | Path | None = None,
    output_manifest_uri: str = "manifest.jsonl",
    tile_storage_uri: str = "s3://valhalla-tiles/global/",
    geofabrik_index: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Generate an indexed task manifest (JSONL) for continental/region-wise tile generation.

    Args:
        regions: List of region names, slugs, or wildcards (e.g. ['bayern', 'berlin', 'germany/*']).
        regions_file: Path or URI to text file containing region names (one per line).
        output_manifest_uri: Destination URI or path for the generated task manifest (JSONL).
        tile_storage_uri: Destination blob storage prefix where .gph tiles will be uploaded.
        geofabrik_index: Optional pre-loaded Geofabrik index dict.

    Returns:
        List of generated task dictionaries.
    """
    all_queries: list[str] = []

    if regions:
        all_queries.extend(regions)

    if regions_file:
        local_rf = uri_to_local_path(regions_file)
        if local_rf is not None:
            content = local_rf.read_text(encoding="utf-8")
        else:
            mgr = StorageManager.from_uri(str(regions_file))
            content = mgr.read_bytes("").decode("utf-8")

        for line in content.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                all_queries.append(line)

    if not all_queries:
        raise ValueError("No regions provided. Specify --regions or --regions-file.")

    matched_props = resolve_regions(all_queries, index=geofabrik_index)
    if not matched_props:
        raise ValueError(f"No regions could be resolved from queries: {all_queries}")

    tasks: list[dict[str, Any]] = []
    clean_tile_uri = tile_storage_uri.rstrip("/") + "/"

    for i, p in enumerate(matched_props):
        pbf_url = p.get("urls", {}).get("pbf")
        if not pbf_url:
            continue
        task = {
            "task_id": i,
            "task_type": "build_tiles",
            "region": p.get("id"),
            "region_name": p.get("name"),
            "osm_url": pbf_url,
            "tile_storage_uri": clean_tile_uri,
        }
        tasks.append(task)

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
    temp_dir: str | Path | None = None,
    keep_temp: bool = False,
    verbose: bool = True,
) -> dict[str, Any]:
    """
    Execute a single tile-building task from the manifest corresponding to task_index (or $JOB_COMPLETION_INDEX).

    Args:
        manifest_uri: URI or path to the task manifest JSONL file.
        task_index: Zero-indexed integer task number. If None, read from JOB_COMPLETION_INDEX env var.
        tile_storage_uri: Optional override for destination blob storage prefix.
        temp_dir: Optional custom temporary working directory.
        keep_temp: If True, does not delete temporary download/build directory upon completion.
        verbose: Print progress.

    Returns:
        Summary dict containing task details and count of uploaded tiles.
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
    region = task.get("region", f"task_{task_index}")
    osm_url = task.get("osm_url")
    effective_storage_uri = tile_storage_uri or task.get("tile_storage_uri")

    if not osm_url:
        raise ValueError(f"Task {task_index} for region '{region}' does not contain 'osm_url'.")

    print(f"[ValhallaRoute Worker {task_index}] Starting tile generation for region: {region} ({osm_url})")

    # Set up temporary working directory
    base_tmp = Path(temp_dir) if temp_dir else Path(tempfile.gettempdir())
    work_dir = base_tmp / f"valhalla_worker_{task_index}_{region}"
    work_dir.mkdir(parents=True, exist_ok=True)

    pbf_dest = work_dir / f"{region}.osm.pbf"
    tiles_dir = work_dir / "tiles"
    tar_dest = work_dir / f"{region}_tiles.tar"

    try:
        t0 = time.time()
        # 1. Download PBF
        if verbose:
            print(f"[ValhallaRoute Worker {task_index}] Downloading {osm_url}...")
        download_file(url=osm_url, dest_path=pbf_dest, verbose=verbose)
        t_download = time.time() - t0

        # 2. Build Valhalla tiles
        t1 = time.time()
        if verbose:
            print(f"[ValhallaRoute Worker {task_index}] Building tiles into {tiles_dir}...")
        build_valhalla_tiles(
            osm_pbf_path=pbf_dest,
            tile_dir=tiles_dir,
            tile_extract=tar_dest,
            verbose=verbose,
        )
        t_build = time.time() - t1

        # 3. Discover and upload all generated .gph files
        gph_files = list(tiles_dir.rglob("*.gph"))
        if verbose:
            print(f"[ValhallaRoute Worker {task_index}] Generated {len(gph_files)} .gph tiles. Uploading to {effective_storage_uri}...")

        t2 = time.time()
        local_store_path = uri_to_local_path(effective_storage_uri)
        if local_store_path is not None:
            # Copy to local directory
            for gph in gph_files:
                rel = gph.relative_to(tiles_dir).as_posix()
                dest = local_store_path / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(gph, dest)
        else:
            # Upload to remote cloud storage via StorageManager concurrently
            out_mgr = StorageManager.from_uri(effective_storage_uri)

            async def _upload_all():
                async def _upload_one(gph: Path):
                    rel = gph.relative_to(tiles_dir).as_posix()
                    data = gph.read_bytes()
                    await out_mgr.write_bytes_async(rel, data)

                tasks_upload = [_upload_one(g) for g in gph_files]
                await asyncio.gather(*tasks_upload)

            asyncio.run(_upload_all())

        t_upload = time.time() - t2
        total_time = time.time() - t0

        summary = {
            "task_id": task_index,
            "region": region,
            "tiles_count": len(gph_files),
            "download_time_s": round(t_download, 2),
            "build_time_s": round(t_build, 2),
            "upload_time_s": round(t_upload, 2),
            "total_time_s": round(total_time, 2),
        }
        print(
            f"[ValhallaRoute Worker {task_index}] Finished {region}: {len(gph_files)} tiles in {total_time:.1f}s "
            f"(download: {t_download:.1f}s, build: {t_build:.1f}s, upload: {t_upload:.1f}s)"
        )
        return summary
    finally:
        if not keep_temp:
            shutil.rmtree(work_dir, ignore_errors=True)
