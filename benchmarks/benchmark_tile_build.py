"""Benchmark script to measure OpenStreetMap PBF download and Valhalla tile compilation performance."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

from valhallaroute.batch import resolve_regions
from valhallaroute.tile_manager import build_valhalla_tiles, download_file


def run_tile_benchmark(
    region: str = "bayern",
    output_dir: Path | str = "benchmark_tiles",
    keep_output: bool = False,
) -> dict[str, float]:
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print(" " * 18 + "VALHALLA TILE GENERATION BENCHMARK")
    print("=" * 70)
    print(f"Target Region:    {region}")
    print(f"Working Dir:      {out_dir}")
    print(f"CPU Cores:        {os.cpu_count()}")

    # 1. Resolve region
    resolved = resolve_regions([region])
    if not resolved:
        raise ValueError(f"Could not resolve region: {region}")
    props = resolved[0]
    pbf_url = props.get("urls", {}).get("pbf")
    region_id = props.get("id")
    print(f"Matched Region:   {props.get('name')} (ID: {region_id})")
    print(f"PBF Download URL: {pbf_url}")

    pbf_file = out_dir / f"{region_id}.osm.pbf"
    tiles_dir = out_dir / "valhalla_tiles"
    tar_file = out_dir / "valhalla_tiles.tar"

    # 2. Download PBF
    t0 = time.time()
    if pbf_file.exists() and pbf_file.stat().st_size > 0:
        print(f"PBF file already present: {pbf_file} ({pbf_file.stat().st_size / (1024*1024):.2f} MB)")
        t_download = 0.0
    else:
        print("\n[Phase 1/2] Downloading OpenStreetMap PBF extract...")
        download_file(url=pbf_url, dest_path=pbf_file, verbose=True)
        t_download = time.time() - t0

    pbf_size_mb = pbf_file.stat().st_size / (1024 * 1024)
    dl_speed = (pbf_size_mb / t_download) if t_download > 0 else 0.0

    # 3. Build Valhalla tiles
    print(f"\n[Phase 2/2] Compiling Valhalla graph tiles using 16 cores...")
    t1 = time.time()
    build_valhalla_tiles(
        osm_pbf_path=pbf_file,
        tile_dir=tiles_dir,
        tile_extract=tar_file,
        overwrite=True,
        verbose=True,
    )
    t_build = time.time() - t1

    # 4. Analyze generated tiles
    gph_files = list(tiles_dir.rglob("*.gph"))
    total_gph_bytes = sum(f.stat().st_size for f in gph_files)
    total_gph_mb = total_gph_bytes / (1024 * 1024)

    total_time = t_download + t_build

    print("\n" + "=" * 70)
    print(" " * 24 + "BENCHMARK RESULTS")
    print("=" * 70)
    print(f"PBF Size:                {pbf_size_mb:.2f} MB")
    print(f"PBF Download Time:       {t_download:.2f} s ({dl_speed:.2f} MB/s)" if t_download > 0 else f"PBF Download Time:       (cached locally)")
    print(f"Tile Build Time:         {t_build:.2f} s ({t_build/60:.2f} min)")
    print(f"Total Compilation Rate:  {pbf_size_mb / t_build:.2f} MB PBF / sec")
    print(f"Generated Tiles:         {len(gph_files):,} .gph files ({total_gph_mb:.2f} MB)")
    print(f"Total End-to-End Time:   {total_time:.2f} s ({total_time/60:.2f} min)")
    print("=" * 70)

    if not keep_output:
        print("Cleaning up temporary benchmark tile directory...")
        shutil.rmtree(out_dir, ignore_errors=True)

    return {
        "pbf_size_mb": round(pbf_size_mb, 2),
        "download_time_s": round(t_download, 2),
        "build_time_s": round(t_build, 2),
        "total_time_s": round(total_time, 2),
        "tiles_count": len(gph_files),
        "tiles_size_mb": round(total_gph_mb, 2),
    }


def main():
    parser = argparse.ArgumentParser(description="Benchmark Valhalla tile generation for a region.")
    parser.add_argument("region", nargs="?", default="bayern", help="Geofabrik region (default: bayern).")
    parser.add_argument("--output-dir", default="benchmark_tiles", help="Directory for downloaded PBF and tiles.")
    parser.add_argument("--keep", action="store_true", help="Keep downloaded PBF and tiles.")
    args = parser.parse_args()

    run_tile_benchmark(
        region=args.region,
        output_dir=args.output_dir,
        keep_output=args.keep,
    )


if __name__ == "__main__":
    main()
