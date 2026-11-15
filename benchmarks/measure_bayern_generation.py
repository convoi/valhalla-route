"""Detailed profiling script to measure exact CPU seconds, total seconds, and max RAM for Bavaria tile generation."""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

import psutil

from valhallaroute.batch import resolve_regions
from valhallaroute.tile_manager import download_file

# Win32 Performance Structures
class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def run_monitored_command(
    cmd: list[str],
    env: dict[str, str] | None = None,
    log_file_path: Path | None = None,
) -> dict[str, float]:
    """Execute a command and accurately record wall-clock seconds, CPU seconds, and peak RAM."""
    t_start = time.perf_counter()

    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = proc.communicate()
    t_end = time.perf_counter()
    wall_seconds = t_end - t_start

    if log_file_path:
        log_file_path.write_bytes(stdout_bytes + b"\n" + stderr_bytes)

    # Get OS-level exact stats via Win32 API before closing handle
    creation_time = FILETIME()
    exit_time = FILETIME()
    kernel_time = FILETIME()
    user_time = FILETIME()

    user_sec = 0.0
    kernel_sec = 0.0
    peak_ram_bytes = 0
    if sys.platform == "win32" and hasattr(proc, "_handle"):
        ctypes.windll.kernel32.GetProcessTimes(
            proc._handle,
            ctypes.byref(creation_time),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        )
        k = (kernel_time.dwHighDateTime << 32) + kernel_time.dwLowDateTime
        u = (user_time.dwHighDateTime << 32) + user_time.dwLowDateTime
        user_sec = u / 1e7
        kernel_sec = k / 1e7

        pmc = PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        ctypes.windll.psapi.GetProcessMemoryInfo(proc._handle, ctypes.byref(pmc), pmc.cb)
        peak_ram_bytes = pmc.PeakWorkingSetSize

    total_cpu_sec = user_sec + kernel_sec

    if proc.returncode != 0:
        err_msg = stderr_bytes.decode("utf-8", errors="replace")[-1000:]
        out_msg = stdout_bytes.decode("utf-8", errors="replace")[-1000:]
        raise RuntimeError(
            f"Command {cmd[0]} failed with return code {proc.returncode}.\nSTDOUT:\n{out_msg}\nSTDERR:\n{err_msg}"
        )

    return {
        "wall_seconds": wall_seconds,
        "user_cpu_seconds": user_sec,
        "kernel_cpu_seconds": kernel_sec,
        "total_cpu_seconds": total_cpu_sec,
        "peak_ram_bytes": peak_ram_bytes,
        "peak_ram_mb": peak_ram_bytes / (1024 * 1024),
        "peak_ram_gb": peak_ram_bytes / (1024 * 1024 * 1024),
    }


def main():
    region = "bayern"
    output_dir = Path("bayern_benchmark_build").resolve()
    if output_dir.exists():
        shutil.rmtree(output_dir, ignore_errors=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 75)
    print(" " * 16 + "VALHALLA TILE GENERATION - HARD NUMBERS BENCHMARK")
    print("=" * 75)
    print(f"Target Region:       {region.upper()} (Bavaria, Germany)")
    print(f"CPU Hardware:        {os.cpu_count()} CPU Logical Cores")
    print(f"Total System Memory: {psutil.virtual_memory().total / (1024**3):.2f} GB RAM")
    print(f"Working Directory:   {output_dir}")

    # Resolve Geofabrik URL
    resolved = resolve_regions([region])
    props = resolved[0]
    pbf_url = props["urls"]["pbf"]
    print(f"PBF Source URL:      {pbf_url}")

    # Step 1: Ensure PBF is downloaded
    pbf_cache = Path("osm_data") / "bayern-latest.osm.pbf"
    if pbf_cache.exists() and pbf_cache.stat().st_size > 800 * 1024 * 1024:
        print(f"\nUsing cached PBF extract: {pbf_cache} ({pbf_cache.stat().st_size / (1024*1024):.2f} MB)")
        pbf_path = pbf_cache.resolve()
    else:
        print(f"\n[1/3] Downloading Bavaria PBF extract...")
        t_dl_0 = time.perf_counter()
        download_file(pbf_url, pbf_cache, verbose=True)
        t_dl = time.perf_counter() - t_dl_0
        print(f"PBF downloaded in {t_dl:.2f} s ({pbf_cache.stat().st_size / (1024*1024):.2f} MB)")
        pbf_path = pbf_cache.resolve()

    pbf_size_mb = pbf_path.stat().st_size / (1024 * 1024)

    # Setup Valhalla Config and Environment
    tiles_dir = output_dir / "valhalla_tiles"
    tar_file = output_dir / "valhalla_tiles.tar"
    tiles_dir.mkdir(parents=True, exist_ok=True)

    import valhalla
    from valhalla.config import default_config, _sanitize_config

    cfg = _sanitize_config(default_config.copy())
    cfg["mjolnir"]["tile_dir"] = str(tiles_dir).replace("\\", "/")
    cfg["mjolnir"]["tile_extract"] = str(tar_file).replace("\\", "/")
    cfg["mjolnir"]["admin"] = ""
    cfg["mjolnir"]["data_processing"]["use_admin_db"] = False
    cfg["logging"]["type"] = "std_out"

    cfg_file = output_dir / "valhalla.json"
    with open(cfg_file, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

    valhalla_pkg_dir = Path(valhalla.__file__).parent
    libs_dir = valhalla_pkg_dir.parent / "pyvalhalla.libs"
    env = os.environ.copy()
    if libs_dir.exists():
        env["PATH"] = str(libs_dir) + os.pathsep + env.get("PATH", "")

    bin_dir = valhalla_pkg_dir / "bin"
    build_tiles_exe = bin_dir / ("valhalla_build_tiles.exe" if sys.platform == "win32" else "valhalla_build_tiles")

    # Step 2: Run valhalla_build_tiles
    print(f"\n[2/2] Running valhalla_build_tiles across all {os.cpu_count()} CPU cores...")
    tiles_log = output_dir / "build_tiles.log"
    tiles_stats = run_monitored_command(
        [str(build_tiles_exe), "-c", str(cfg_file), str(pbf_path)],
        env=env,
        log_file_path=tiles_log,
    )
    print(f"  -> Wall-clock time: {tiles_stats['wall_seconds']:.2f} s ({tiles_stats['wall_seconds']/60:.2f} min)")
    print(f"  -> CPU time:        {tiles_stats['total_cpu_seconds']:.2f} s ({tiles_stats['total_cpu_seconds']/60:.2f} CPU min)")
    print(f"  -> Peak RAM:        {tiles_stats['peak_ram_mb']:.2f} MB ({tiles_stats['peak_ram_gb']:.3f} GB)")

    # Compute overall generation metrics
    total_gen_wall = tiles_stats["wall_seconds"]
    total_gen_cpu = tiles_stats["total_cpu_seconds"]
    max_gen_ram_mb = tiles_stats["peak_ram_mb"]
    max_gen_ram_gb = tiles_stats["peak_ram_gb"]
    cpu_utilization_ratio = (total_gen_cpu / total_gen_wall) if total_gen_wall > 0 else 0.0

    # Count generated tiles
    gph_files = list(tiles_dir.rglob("*.gph"))
    total_gph_mb = sum(f.stat().st_size for f in gph_files) / (1024 * 1024)

    # Print Official Report
    print("\n" + "=" * 75)
    print(" " * 22 + "OFFICIAL HARD NUMBERS REPORT")
    print("=" * 75)
    print(f"Region:                      Bayern (Bavaria), Germany")
    print(f"OSM PBF Input Size:          {pbf_size_mb:.2f} MB")
    print(f"Generated Tiles Count:       {len(gph_files):,} .gph tiles")
    print(f"Generated Tiles Volume:      {total_gph_mb:.2f} MB uncompressed")
    print("-" * 75)
    print(f"TOTAL CPU SECONDS:           {total_gen_cpu:.2f} s ({total_gen_cpu/60:.2f} CPU minutes)")
    print(f"  - User CPU Seconds:        {tiles_stats['user_cpu_seconds']:.2f} s")
    print(f"  - Kernel CPU Seconds:      {tiles_stats['kernel_cpu_seconds']:.2f} s")
    print("-" * 75)
    print(f"TOTAL WALL-CLOCK SECONDS:    {total_gen_wall:.2f} s ({total_gen_wall/60:.2f} minutes)")
    print(f"Multi-Core Scaling Factor:   {cpu_utilization_ratio:.2f}x (across {os.cpu_count()} cores)")
    print("-" * 75)
    print(f"MAX RAM USAGE (WORKING SET): {max_gen_ram_mb:.2f} MB ({max_gen_ram_gb:.3f} GB)")
    print("=" * 75)

    # Cleanup build scratch dir
    print("\nCleaning up temporary benchmark build directory...")
    shutil.rmtree(output_dir, ignore_errors=True)
    print("Done.")


if __name__ == "__main__":
    main()
