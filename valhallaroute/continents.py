"""Single-node continental tile pack builder for global Valhalla routing and feature parity."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import shutil
import time
from typing import Any, Sequence

from .admin_builder import build_admin_sqlite_from_overture
from .storage import StorageManager, uri_to_local_path
from .tile_manager import build_valhalla_tiles, download_file

LOGGER = logging.getLogger(__name__)

CONTINENT_PBF_URLS: dict[str, str] = {
    "europe": "https://download.geofabrik.de/europe-latest.osm.pbf",
    "north-america": "https://download.geofabrik.de/north-america-latest.osm.pbf",
    "south-america": "https://download.geofabrik.de/south-america-latest.osm.pbf",
    "australia-oceania": "https://download.geofabrik.de/australia-oceania-latest.osm.pbf",
    "oceania": "https://download.geofabrik.de/australia-oceania-latest.osm.pbf",
    "africa": "https://download.geofabrik.de/africa-latest.osm.pbf",
    "asia": "https://download.geofabrik.de/asia-latest.osm.pbf",
    "central-america": "https://download.geofabrik.de/central-america-latest.osm.pbf",
    "antarctica": "https://download.geofabrik.de/antarctica-latest.osm.pbf",
}

DEFAULT_CONTINENTS = ("europe", "north-america", "south-america", "oceania")


def normalize_continent_names(continents: Sequence[str] | str) -> list[str]:
    """Parse and normalize continent aliases (e.g. 'oceania' -> 'australia-oceania')."""
    if isinstance(continents, str):
        raw_list = [c.strip().lower() for c in continents.split(",") if c.strip()]
    else:
        raw_list = [str(c).strip().lower() for c in continents if str(c).strip()]

    if not raw_list or "all" in raw_list:
        raw_list = list(DEFAULT_CONTINENTS)

    normalized: list[str] = []
    for c in raw_list:
        if c == "oceania":
            c = "australia-oceania"
        if c in CONTINENT_PBF_URLS:
            if c not in normalized:
                normalized.append(c)
        else:
            valid_keys = ", ".join(sorted(CONTINENT_PBF_URLS.keys()))
            raise ValueError(f"Unknown continent '{c}'. Available: {valid_keys}")

    return normalized


def build_continent_pack(
    continents: Sequence[str] | str = DEFAULT_CONTINENTS,
    destination_storage_uri: str = "valhalla_tiles_global",
    build_admins: bool = True,
    admin_countries: Sequence[str] | None = None,
    admin_levels: Sequence[int] = (2, 4, 6, 8),
    work_dir: Path | str | None = None,
    cleanup_after_continent: bool = True,
    verbose: bool = True,
) -> dict[str, Any]:
    """Build routing graphs and companion feature parquets for isolated continental landmasses on a single node.

    Continents are compiled sequentially to avoid Level 0/1 tile collisions while enabling
    high-speed ephemeral local NVMe execution and automated cloud object storage synchronization.

    Args:
        continents: Sequence of continent slugs ('europe', 'north-america', 'south-america', 'oceania') or comma-separated string.
        destination_storage_uri: Target blob storage prefix (e.g. az://account/container/prefix, s3://..., or local directory).
        build_admins: Whether to compile global admin.sqlite from Overture Maps before building tiles.
        admin_countries: Optional explicit list of ISO country codes for admin.sqlite (None = planet).
        admin_levels: Administrative division levels to compile (default: 2, 4, 6, 8).
        work_dir: Fast local SSD scratch workspace.
        cleanup_after_continent: Delete local PBF and generated tiles after upload to preserve local disk space.
        verbose: Display execution progress.

    Returns:
        Summary dictionary with timing, tile counts, and destination details.
    """
    t_start = time.time()
    continent_list = normalize_continent_names(continents)

    root_work = Path(work_dir) if work_dir else Path("./valhalla_continent_work")
    root_work.mkdir(parents=True, exist_ok=True)
    pbf_dir = root_work / "pbfs"
    pbf_dir.mkdir(parents=True, exist_ok=True)

    if verbose:
        print("=" * 70)
        print("VALHALLAROUTE CONTINENTAL PACK BUILDER")
        print(f"Target Continents:     {', '.join(continent_list)}")
        print(f"Destination Storage:   {destination_storage_uri}")
        print(f"Scratch Directory:     {root_work.resolve()}")
        print(f"Cleanup After Upload:  {cleanup_after_continent}")
        print("=" * 70)

    # 1. Compile and upload global admin.sqlite
    local_admin_db: Path | None = None
    if build_admins:
        local_admin_db = root_work / "admin.sqlite"
        if verbose:
            print("\n[Step 1/3] Compiling Global admin.sqlite from Overture Maps GeoParquet...")
        t_admin = time.time()
        build_admin_sqlite_from_overture(
            output_path=local_admin_db,
            countries=admin_countries,
            admin_levels=admin_levels,
            verbose=verbose,
        )
        if verbose:
            print(f"Compiled admin.sqlite in {time.time() - t_admin:.1f}s. Uploading to {destination_storage_uri}...")

        # Upload admin.sqlite to destination if generated
        if local_admin_db.exists() and local_admin_db.stat().st_size > 0:
            local_dest = uri_to_local_path(destination_storage_uri)
            if local_dest is not None:
                local_dest.mkdir(parents=True, exist_ok=True)
                shutil.copy2(local_admin_db, local_dest / "admin.sqlite")
            else:
                storage_mgr = StorageManager.from_uri(destination_storage_uri)
                storage_mgr.write_bytes("admin.sqlite", local_admin_db.read_bytes())

    # 2. Sequential continental builds
    results_by_continent: dict[str, dict[str, Any]] = {}
    total_gph_count = 0
    total_feat_count = 0

    local_store_path = uri_to_local_path(destination_storage_uri)
    remote_mgr = StorageManager.from_uri(destination_storage_uri) if local_store_path is None else None

    for idx, continent in enumerate(continent_list, 1):
        if verbose:
            print(f"\n[{idx}/{len(continent_list)}] Processing Continent: {continent.upper()}...")

        t_cont_start = time.time()
        pbf_url = CONTINENT_PBF_URLS[continent]
        pbf_path = pbf_dir / f"{continent}-latest.osm.pbf"

        # Download continental PBF
        if not pbf_path.exists() or pbf_path.stat().st_size == 0:
            if verbose:
                print(f"Downloading {continent} extract from {pbf_url}...")
            t_down = time.time()
            download_file(url=pbf_url, dest_path=pbf_path, verbose=verbose)
            download_s = time.time() - t_down
        else:
            if verbose:
                print(f"Using existing PBF: {pbf_path} ({pbf_path.stat().st_size / (1024*1024):.1f} MB)")
            download_s = 0.0

        # Build Valhalla routing tiles and companion feature parquets
        tiles_dir = root_work / "tiles" / continent
        tiles_dir.mkdir(parents=True, exist_ok=True)
        tar_extract = root_work / "extracts" / f"{continent}_tiles.tar"
        tar_extract.parent.mkdir(parents=True, exist_ok=True)

        if verbose:
            print(f"Building Valhalla graph & companion feature tiles into {tiles_dir}...")
        t_build = time.time()
        build_valhalla_tiles(
            osm_pbf_path=pbf_path,
            tile_dir=tiles_dir,
            tile_extract=tar_extract,
            admin_db_path=local_admin_db,
            verbose=verbose,
        )
        build_s = time.time() - t_build

        # Discover tiles
        gph_files = list(tiles_dir.rglob("*.gph"))
        feat_files = list(tiles_dir.rglob("*.features.parquet"))
        all_upload_files = gph_files + feat_files

        if verbose:
            print(
                f"Generated {len(gph_files):,} .gph tiles and {len(feat_files):,} .features.parquet files. "
                f"Uploading to {destination_storage_uri}..."
            )

        t_upload = time.time()
        if local_store_path is not None:
            for fpath in all_upload_files:
                rel = fpath.relative_to(tiles_dir).as_posix()
                dest = local_store_path / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(fpath, dest)
        else:
            assert remote_mgr is not None

            async def _upload_all(files: list[Path], src_dir: Path, mgr: StorageManager):
                sem = asyncio.Semaphore(32)

                async def _upload_one(f: Path):
                    async with sem:
                        rel = f.relative_to(src_dir).as_posix()
                        data = f.read_bytes()
                        await mgr.write_bytes_async(rel, data)

                tasks = [_upload_one(f) for f in files]
                await asyncio.gather(*tasks)

            asyncio.run(_upload_all(all_upload_files, tiles_dir, remote_mgr))

        upload_s = time.time() - t_upload
        cont_total_s = time.time() - t_cont_start

        total_gph_count += len(gph_files)
        total_feat_count += len(feat_files)

        results_by_continent[continent] = {
            "gph_tiles": len(gph_files),
            "feature_parquet_tiles": len(feat_files),
            "download_time_s": round(download_s, 1),
            "build_time_s": round(build_s, 1),
            "upload_time_s": round(upload_s, 1),
            "total_time_s": round(cont_total_s, 1),
        }

        # Cleanup continent scratch data to free disk space for next continent
        if cleanup_after_continent:
            if verbose:
                print(f"Cleaning up scratch data for {continent} to reclaim disk space...")
            shutil.rmtree(tiles_dir, ignore_errors=True)
            if pbf_path.exists():
                pbf_path.unlink()
            if tar_extract.exists():
                tar_extract.unlink()

    total_pipeline_s = time.time() - t_start

    summary = {
        "continents": continent_list,
        "destination_storage_uri": destination_storage_uri,
        "total_gph_tiles": total_gph_count,
        "total_feature_parquet_tiles": total_feat_count,
        "total_pipeline_time_s": round(total_pipeline_s, 1),
        "by_continent": results_by_continent,
    }

    if verbose:
        print("\n" + "=" * 70)
        print("CONTINENTAL BUILD COMPLETE")
        print(f"Total .gph Tiles:            {total_gph_count:,}")
        print(f"Total Feature Parquet Tiles: {total_feat_count:,}")
        print(f"Total Execution Time:        {total_pipeline_s / 60:.1f} minutes")
        print("=" * 70)

    return summary
