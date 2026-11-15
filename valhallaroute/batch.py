"""Batch job planner and indexed worker execution for continental/region-wise tile generation.

Features:
- Tile-aware workload balancing: estimates Valhalla Level-2 tiles (0.25° x 0.25°) per region.
- Auto-subdivision: automatically splits mega-regions (> max_tiles_per_job) into child regions.
- Bin-packing: groups small sub-regions (< min_tiles_per_job) into balanced worker tasks.
- Integrated admin boundaries: optionally compiles admin.sqlite from Overture Maps GeoParquet
  and links it to workers for accurate cross-border and driving-side attribution.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .admin_builder import build_admin_sqlite_from_overture
from .storage import StorageManager, get_tiles_for_bbox, uri_to_local_path
from .tile_manager import build_valhalla_tiles, download_file, fetch_geofabrik_index

LOGGER = logging.getLogger(__name__)

# Common Geofabrik region slug to ISO-3166-1 alpha-2 mapping
GEOFABRIK_SLUG_TO_ISO = {
    "germany": "DE", "france": "FR", "italy": "IT", "spain": "ES", "united-kingdom": "GB",
    "netherlands": "NL", "belgium": "BE", "austria": "AT", "switzerland": "CH", "poland": "PL",
    "czech-republic": "CZ", "slovakia": "SK", "hungary": "HU", "denmark": "DK", "sweden": "SE",
    "norway": "NO", "finland": "FI", "portugal": "PT", "greece": "GR", "turkey": "TR",
    "ireland-and-northern-ireland": "IE", "luxembourg": "LU", "iceland": "IS", "us": "US",
    "canada": "CA", "mexico": "MX", "brazil": "BR", "australia": "AU", "japan": "JP",
    "south-africa": "ZA",
}


def extract_bbox_from_geometry(geometry: dict[str, Any]) -> Tuple[float, float, float, float] | None:
    """Extract (min_lat, min_lon, max_lat, max_lon) from a GeoJSON geometry."""
    coords = geometry.get("coordinates", [])
    if not coords:
        return None
    lons: list[float] = []
    lats: list[float] = []

    def _walk(c: Any):
        if isinstance(c, (list, tuple)) and len(c) >= 2 and isinstance(c[0], (int, float)):
            lons.append(float(c[0]))
            lats.append(float(c[1]))
        elif isinstance(c, (list, tuple)):
            for sub in c:
                _walk(sub)

    _walk(coords)
    if not lons or not lats:
        return None
    return min(lats), min(lons), max(lats), max(lons)


def estimate_tiles_for_feature(feature_or_props: dict[str, Any]) -> int:
    """Estimate the number of Level-2 (0.25° x 0.25°) Valhalla tiles for a Geofabrik feature or properties."""
    geom = feature_or_props.get("geometry", {})
    bbox = extract_bbox_from_geometry(geom)
    if not bbox:
        return 1
    min_lat, min_lon, max_lat, max_lon = bbox
    tiles = get_tiles_for_bbox(min_lat, min_lon, max_lat, max_lon, levels=(2,), buffer_degrees=0.0)
    return max(1, len(tiles))


def infer_iso_country(region_id: str, parent: str = "") -> str | None:
    """Infer ISO-3166-1 alpha-2 country code from Geofabrik region ID or parent."""
    parts = [p.lower() for p in region_id.split("/") if p]
    if parent:
        parts.insert(0, parent.lower())
    for part in parts:
        if part in GEOFABRIK_SLUG_TO_ISO:
            return GEOFABRIK_SLUG_TO_ISO[part]
        if len(part) == 2 and part.isalpha():
            return part.upper()
    return None


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
                        item = props.copy()
                        item["geometry"] = feat.get("geometry", {})
                        resolved.append(item)
                        matched_any = True
            else:
                if q_clean in (fid, fname) or f"/{q_clean}" in fid or fid.endswith(f"/{q_clean}"):
                    if fid not in seen_ids:
                        seen_ids.add(fid)
                        item = props.copy()
                        item["geometry"] = feat.get("geometry", {})
                        resolved.append(item)
                        matched_any = True
                    break

        if not matched_any and not is_wildcard:
            raise ValueError(f"Could not find any Geofabrik region matching '{q}' in index.")

    return resolved


def auto_subdivide_regions(
    features: Sequence[dict[str, Any]],
    index: dict[str, Any],
    max_tiles: int = 300,
) -> list[dict[str, Any]]:
    """Recursively subdivide regions exceeding max_tiles into their child regions if available."""
    all_index_features = index.get("features", [])
    result: list[dict[str, Any]] = []

    for feat in features:
        fid = feat.get("id") or feat.get("properties", {}).get("id", "")
        est_tiles = estimate_tiles_for_feature(feat)

        if est_tiles > max_tiles:
            # Check for children
            children: list[dict[str, Any]] = []
            for f in all_index_features:
                p = f.get("properties", {})
                if p.get("parent") == fid and p.get("urls", {}).get("pbf"):
                    item = p.copy()
                    item["geometry"] = f.get("geometry", {})
                    children.append(item)

            if children:
                LOGGER.info(
                    f"Auto-subdividing mega-region '{fid}' ({est_tiles} est. tiles > max {max_tiles}) "
                    f"into {len(children)} child regions."
                )
                # Recursively process children
                subdivided_children = auto_subdivide_regions(children, index, max_tiles=max_tiles)
                result.extend(subdivided_children)
                continue

        result.append(feat)

    return result


def bin_pack_regions(
    features: Sequence[dict[str, Any]],
    target_tiles: int = 200,
    min_tiles: int = 50,
) -> list[list[dict[str, Any]]]:
    """Bin-pack small regions (< min_tiles) into balanced groups up to target_tiles."""
    large_or_medium: list[list[dict[str, Any]]] = []
    small: list[dict[str, Any]] = []

    for feat in features:
        est_tiles = estimate_tiles_for_feature(feat)
        if est_tiles >= min_tiles:
            large_or_medium.append([feat])
        else:
            small.append(feat)

    # Sort small regions descending by tile count (First-Fit Decreasing)
    small.sort(key=lambda f: estimate_tiles_for_feature(f), reverse=True)

    bins: list[list[dict[str, Any]]] = []
    bin_weights: list[int] = []

    for feat in small:
        w = estimate_tiles_for_feature(feat)
        placed = False
        for i, current_bin in enumerate(bins):
            if bin_weights[i] + w <= target_tiles:
                current_bin.append(feat)
                bin_weights[i] += w
                placed = True
                break
        if not placed:
            bins.append([feat])
            bin_weights.append(w)

    return large_or_medium + bins


def plan_batch(
    regions: Sequence[str] | None = None,
    regions_file: str | Path | None = None,
    output_manifest_uri: str = "manifest.jsonl",
    tile_storage_uri: str = "s3://valhalla-tiles/global/",
    geofabrik_index: dict[str, Any] | None = None,
    auto_subdivide: bool = True,
    max_tiles_per_job: int = 300,
    bin_pack: bool = True,
    target_tiles_per_job: int = 200,
    min_tiles_per_job: int = 50,
    build_admins: bool = False,
    admin_countries: Optional[Sequence[str]] = None,
    admin_levels: Sequence[int] = (2, 4),
    overture_release: str = "2026-10-21.0",
    verbose: bool = True,
) -> list[dict[str, Any]]:
    """Generate a balanced task manifest (JSONL) with optional admin.sqlite compilation.

    Args:
        regions: List of region names, slugs, or wildcards (e.g. ['bayern', 'berlin', 'germany/*']).
        regions_file: Path or URI to text file containing region names (one per line).
        output_manifest_uri: Destination URI or path for the generated task manifest (JSONL).
        tile_storage_uri: Destination blob storage prefix where .gph tiles will be uploaded.
        geofabrik_index: Optional pre-loaded Geofabrik index dict.
        auto_subdivide: If True, splits regions exceeding max_tiles_per_job into subregions.
        max_tiles_per_job: Upper threshold of tiles per task before auto-subdividing. Default: 300.
        bin_pack: If True, groups small regions (< min_tiles_per_job) into balanced worker tasks.
        target_tiles_per_job: Target tile capacity per worker pod. Default: 200.
        min_tiles_per_job: Lower threshold before bundling small regions together. Default: 50.
        build_admins: If True, compiles admin.sqlite from Overture Maps and uploads to storage.
        admin_countries: Explicit list of ISO country codes for admin DB. If None, auto-detected.
        admin_levels: Admin levels for admin.sqlite (default: [2, 4]).
        overture_release: Overture release version for admin DB. Default: '2026-10-21.0'.
        verbose: Display logging.

    Returns:
        List of generated task dictionaries.
    """
    if geofabrik_index is None:
        geofabrik_index = fetch_geofabrik_index()

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

    matched_feats = resolve_regions(all_queries, index=geofabrik_index)
    if not matched_feats:
        raise ValueError(f"No regions could be resolved from queries: {all_queries}")

    # 1. Auto-subdivide mega-regions if enabled
    if auto_subdivide:
        matched_feats = auto_subdivide_regions(matched_feats, geofabrik_index, max_tiles=max_tiles_per_job)

    # 2. Bin-pack small regions if enabled
    if bin_pack:
        feature_groups = bin_pack_regions(
            matched_feats, target_tiles=target_tiles_per_job, min_tiles=min_tiles_per_job
        )
    else:
        feature_groups = [[f] for f in matched_feats]

    clean_tile_uri = tile_storage_uri.rstrip("/") + "/"
    admin_db_uri: str | None = None

    # 3. Compile admin.sqlite if requested
    if build_admins:
        target_countries: set[str] = set()
        if admin_countries:
            target_countries = {c.upper().strip() for c in admin_countries}
        else:
            # Auto-infer country codes from resolved regions
            for feat in matched_feats:
                props = feat.get("properties", {})
                iso = infer_iso_country(props.get("id", ""), props.get("parent", ""))
                if iso:
                    target_countries.add(iso)

        if verbose:
            print(f"[Planner] Compiling administrative database for countries: {sorted(list(target_countries))}...")

        with tempfile.TemporaryDirectory() as tmp_admin_dir:
            local_admin_file = Path(tmp_admin_dir) / "admin.sqlite"
            build_admin_sqlite_from_overture(
                output_path=local_admin_file,
                countries=sorted(list(target_countries)) if target_countries else None,
                admin_levels=admin_levels,
                release=overture_release,
                verbose=verbose,
            )

            # Upload admin.sqlite to tile storage root
            admin_db_uri = f"{clean_tile_uri}admin.sqlite"
            local_storage = uri_to_local_path(clean_tile_uri)
            if local_storage is not None:
                local_dest = local_storage / "admin.sqlite"
                local_dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(local_admin_file, local_dest)
            else:
                out_mgr = StorageManager.from_uri(clean_tile_uri)
                out_mgr.write_bytes("admin.sqlite", local_admin_file.read_bytes())

            if verbose:
                print(f"[Planner] Successfully uploaded admin.sqlite to {admin_db_uri}")

    # 4. Generate Task Manifest
    tasks: list[dict[str, Any]] = []
    for i, group in enumerate(feature_groups):
        region_ids = [f.get("id") or f.get("properties", {}).get("id") for f in group if (f.get("id") or f.get("properties", {}).get("id"))]
        region_names = [f.get("name") or f.get("properties", {}).get("name") for f in group if (f.get("name") or f.get("properties", {}).get("name"))]
        osm_urls = [
            f.get("urls", {}).get("pbf") or f.get("properties", {}).get("urls", {}).get("pbf")
            for f in group
            if (f.get("urls", {}).get("pbf") or f.get("properties", {}).get("urls", {}).get("pbf"))
        ]
        if not osm_urls:
            continue

        est_tiles = sum(estimate_tiles_for_feature(f) for f in group)

        task: dict[str, Any] = {
            "task_id": i,
            "task_type": "build_tiles",
            "regions": region_ids,
            "region_names": region_names,
            "osm_urls": osm_urls,
            "estimated_tiles": est_tiles,
            "tile_storage_uri": clean_tile_uri,
            # Backwards-compatible single-item fields
            "region": region_ids[0] if region_ids else "",
            "region_name": region_names[0] if region_names else "",
            "osm_url": osm_urls[0] if osm_urls else "",
        }
        if admin_db_uri:
            task["admin_db_uri"] = admin_db_uri

        tasks.append(task)

    # 5. Serialize manifest to JSONL
    manifest_lines = [json.dumps(t) for t in tasks]
    manifest_content = "\n".join(manifest_lines) + "\n"

    local_manifest = uri_to_local_path(output_manifest_uri)
    if local_manifest is not None:
        local_manifest.parent.mkdir(parents=True, exist_ok=True)
        local_manifest.write_text(manifest_content, encoding="utf-8")
    else:
        out_mgr = StorageManager.from_uri(output_manifest_uri)
        out_mgr.write_bytes("", manifest_content.encode("utf-8"))

    if verbose:
        print(f"[Planner] Generated {len(tasks)} balanced tasks in manifest: {output_manifest_uri}")

    return tasks


def run_worker(
    manifest_uri: str,
    task_index: int | None = None,
    tile_storage_uri: str | None = None,
    admin_db_uri: str | None = None,
    temp_dir: str | Path | None = None,
    keep_temp: bool = False,
    verbose: bool = True,
) -> dict[str, Any]:
    """Execute a single tile-building task from the manifest.

    Supports:
    - Single or multi-PBF inputs (for bin-packed tasks).
    - Automatic admin.sqlite retrieval and Valhalla boundary enrichment.
    - Concurrent .gph upload to cloud storage.
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
    regions = task.get("regions") or [task.get("region", f"task_{task_index}")]
    osm_urls = task.get("osm_urls") or ([task.get("osm_url")] if task.get("osm_url") else [])
    effective_storage_uri = tile_storage_uri or task.get("tile_storage_uri")
    effective_admin_uri = admin_db_uri or task.get("admin_db_uri")

    if not osm_urls:
        raise ValueError(f"Task {task_index} for regions '{regions}' does not contain any 'osm_urls'.")

    regions_str = ", ".join(regions)
    print(f"[ValhallaRoute Worker {task_index}] Starting tile generation for: {regions_str}")

    # Set up temporary working directory
    base_tmp = Path(temp_dir) if temp_dir else Path(tempfile.gettempdir())
    sanitized_name = "_".join(r.replace("/", "_") for r in regions)[:40]
    work_dir = base_tmp / f"valhalla_worker_{task_index}_{sanitized_name}"
    work_dir.mkdir(parents=True, exist_ok=True)

    tiles_dir = work_dir / "tiles"
    tar_dest = work_dir / f"tiles_{task_index}.tar"

    try:
        t0 = time.time()

        # 1. Download admin.sqlite if configured
        local_admin_db: Path | None = None
        if effective_admin_uri:
            admin_dest = work_dir / "admin.sqlite"
            local_admin_source = uri_to_local_path(effective_admin_uri)
            if local_admin_source is not None and local_admin_source.exists():
                local_admin_db = local_admin_source
            else:
                if verbose:
                    print(f"[ValhallaRoute Worker {task_index}] Downloading admin.sqlite from {effective_admin_uri}...")
                adm_mgr = StorageManager.from_uri(effective_admin_uri)
                admin_bytes = adm_mgr.read_bytes("")
                admin_dest.write_bytes(admin_bytes)
                local_admin_db = admin_dest

        # 2. Download PBF(s)
        pbf_paths: list[Path] = []
        for i, url in enumerate(osm_urls):
            fname = url.split("/")[-1].split("?")[0]
            pbf_dest = work_dir / f"{i}_{fname}"
            if verbose:
                print(f"[ValhallaRoute Worker {task_index}] Downloading {url}...")
            download_file(url=url, dest_path=pbf_dest, verbose=verbose)
            pbf_paths.append(pbf_dest)

        t_download = time.time() - t0

        # 3. Build Valhalla tiles
        t1 = time.time()
        if verbose:
            print(f"[ValhallaRoute Worker {task_index}] Building tiles into {tiles_dir}...")
        build_valhalla_tiles(
            osm_pbf_path=pbf_paths,
            tile_dir=tiles_dir,
            tile_extract=tar_dest,
            admin_db_path=local_admin_db,
            verbose=verbose,
        )
        t_build = time.time() - t1

        # 4. Discover and upload all generated .gph files
        gph_files = list(tiles_dir.rglob("*.gph"))
        if verbose:
            print(
                f"[ValhallaRoute Worker {task_index}] Generated {len(gph_files)} .gph tiles. "
                f"Uploading to {effective_storage_uri}..."
            )

        t2 = time.time()
        local_store_path = uri_to_local_path(effective_storage_uri)
        if local_store_path is not None:
            for gph in gph_files:
                rel = gph.relative_to(tiles_dir).as_posix()
                dest = local_store_path / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(gph, dest)
        else:
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
            "region": regions[0] if regions else "",
            "regions": regions,
            "tiles_count": len(gph_files),
            "download_time_s": round(t_download, 2),
            "build_time_s": round(t_build, 2),
            "upload_time_s": round(t_upload, 2),
            "total_time_s": round(total_time, 2),
        }
        print(
            f"[ValhallaRoute Worker {task_index}] Finished {regions_str}: {len(gph_files)} tiles in {total_time:.1f}s "
            f"(download: {t_download:.1f}s, build: {t_build:.1f}s, upload: {t_upload:.1f}s)"
        )
        return summary
    finally:
        if not keep_temp:
            shutil.rmtree(work_dir, ignore_errors=True)
