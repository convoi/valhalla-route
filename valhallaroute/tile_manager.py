"""Tile and OpenStreetMap data management: downloading PBFs and building Valhalla tiles."""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence, Union

import requests

LOGGER = logging.getLogger(__name__)

GEOFABRIK_INDEX_URL = "https://download.geofabrik.de/index-v1.json"


def download_file(
    url: str,
    dest_path: Union[str, Path],
    chunk_size: int = 1024 * 1024,  # 1 MB chunks
    timeout: float = 60.0,
    verbose: bool = True,
) -> Path:
    """
    Download a file from a URL to a local destination path.

    Args:
        url: Source HTTP/HTTPS URL.
        dest_path: Destination file path.
        chunk_size: Streaming chunk size in bytes.
        timeout: Request timeout in seconds.
        verbose: Print progress to stdout.

    Returns:
        Path to the downloaded file.
    """
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    part_path = dest.with_suffix(dest.suffix + ".part")

    if verbose:
        print(f"Downloading from {url} to {dest}...")

    resp = requests.get(url, stream=True, timeout=timeout)
    resp.raise_for_status()

    total_size = int(resp.headers.get("content-length", 0))
    downloaded = 0

    with open(part_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=chunk_size):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if verbose and total_size > 0:
                    pct = (downloaded / total_size) * 100
                    mb_down = downloaded / (1024 * 1024)
                    mb_tot = total_size / (1024 * 1024)
                    print(f"\r  -> Progress: {mb_down:.1f} MB / {mb_tot:.1f} MB ({pct:.1f}%)", end="", flush=True)

    if verbose and total_size > 0:
        print()  # newline after progress

    part_path.replace(dest)
    if verbose:
        print(f"Downloaded {dest.name} ({dest.stat().st_size / (1024 * 1024):.2f} MB)")

    return dest


def fetch_geofabrik_index(
    cache_path: Union[str, Path, None] = None,
    max_age_days: int = 7,
) -> dict[str, Any]:
    """
    Fetch and cache the Geofabrik index of regions and extract URLs.

    Args:
        cache_path: Optional path to save/load cached JSON index.
        max_age_days: Days before refreshing cached index.

    Returns:
        GeoJSON FeatureCollection dictionary.
    """
    if cache_path is None:
        cache_path = Path("osm_data") / "geofabrik_index.json"
    else:
        cache_path = Path(cache_path)

    if cache_path.exists():
        import time
        file_age = (time.time() - cache_path.stat().st_mtime) / (3600 * 24)
        if file_age < max_age_days:
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass

    # Download index
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(GEOFABRIK_INDEX_URL, timeout=30.0)
    resp.raise_for_status()
    data = resp.json()

    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    return data


def _get_geometry_bbox(geometry: dict[str, Any]) -> tuple[float, float, float, float] | None:
    """Extract (min_lon, min_lat, max_lon, max_lat) from a GeoJSON geometry."""
    geom_type = geometry.get("type")
    coords = []
    if geom_type == "Polygon":
        for ring in geometry.get("coordinates", []):
            coords.extend(ring)
    elif geom_type == "MultiPolygon":
        for poly in geometry.get("coordinates", []):
            for ring in poly:
                coords.extend(ring)
    else:
        return None

    if not coords:
        return None

    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return min(lons), min(lats), max(lons), max(lats)


def find_matching_geofabrik_region(
    coords: Sequence[tuple[float, float]],
    index: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """
    Find the smallest Geofabrik region that encloses all the given (lat, lon) coordinates.

    Args:
        coords: Sequence of (latitude, longitude) tuples.
        index: Geofabrik index dict (will fetch if None).

    Returns:
        Matching feature dict with 'properties' and 'geometry', or None.
    """
    if not coords:
        return None

    if index is None:
        index = fetch_geofabrik_index()

    trip_min_lat = min(c[0] for c in coords)
    trip_max_lat = max(c[0] for c in coords)
    trip_min_lon = min(c[1] for c in coords)
    trip_max_lon = max(c[1] for c in coords)

    matching_candidates: list[tuple[float, dict[str, Any]]] = []

    for feature in index.get("features", []):
        geom = feature.get("geometry")
        if not geom:
            continue
        bbox = _get_geometry_bbox(geom)
        if not bbox:
            continue

        min_lon, min_lat, max_lon, max_lat = bbox

        # Check if trip bbox is enclosed in region bbox
        if (
            min_lon <= trip_min_lon
            and max_lon >= trip_max_lon
            and min_lat <= trip_min_lat
            and max_lat >= trip_max_lat
        ):
            # Area approximation in degrees^2 for selecting smallest enclosing region
            area = (max_lon - min_lon) * (max_lat - min_lat)
            matching_candidates.append((area, feature))

    if not matching_candidates:
        return None

    # Pick the smallest enclosing region
    matching_candidates.sort(key=lambda x: x[0])
    return matching_candidates[0][1]


def download_osm_pbf(
    dest_path: Union[str, Path, None] = None,
    *,
    url: str | None = None,
    region: str | None = None,
    coords: Sequence[tuple[float, float]] | None = None,
    output_dir: Union[str, Path] = "osm_data",
    verbose: bool = True,
) -> Path:
    """
    Ensure an OpenStreetMap .osm.pbf file is available locally, downloading it if missing.

    Resolution strategy:
    1. If dest_path exists -> return it.
    2. If url is provided -> download from url.
    3. If region is provided -> look up region in Geofabrik index and download.
    4. If coords are provided -> auto-detect enclosing Geofabrik region and download.
    5. Fallback: raises FileNotFoundError with helpful instructions.

    Returns:
        Path to the local .osm.pbf file.
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if dest_path is not None:
        p = Path(dest_path)
        if p.exists() and p.stat().st_size > 0:
            return p

    download_url = url

    if not download_url and (region or coords):
        index = fetch_geofabrik_index(cache_path=out_dir / "geofabrik_index.json")

        if region:
            q = region.lower().strip()
            for feat in index.get("features", []):
                props = feat.get("properties", {})
                fid = props.get("id", "").lower()
                fname = props.get("name", "").lower()
                if q in (fid, fname) or f"/{q}" in fid:
                    download_url = props.get("urls", {}).get("pbf")
                    if verbose:
                        print(f"Matched region '{region}' -> {props.get('name')} ({download_url})")
                    break

        if not download_url and coords:
            match = find_matching_geofabrik_region(coords, index=index)
            if match:
                props = match.get("properties", {})
                download_url = props.get("urls", {}).get("pbf")
                if verbose:
                    print(f"Auto-detected Geofabrik region for coordinates: {props.get('name')} ({download_url})")

    if not download_url:
        # Check if there is already an existing .osm.pbf file in output_dir
        existing = list(out_dir.glob("*.osm.pbf"))
        if existing:
            return existing[0]

        raise FileNotFoundError(
            "No OpenStreetMap (.osm.pbf) file found and insufficient information to auto-download. "
            "Please specify 'osm_pbf' path, 'osm_url', 'region' (e.g. 'monaco', 'berlin'), or GPS coordinates."
        )

    # Determine destination filename from URL
    filename = download_url.split("/")[-1].split("?")[0]
    final_dest = Path(dest_path) if dest_path else out_dir / filename

    if final_dest.exists() and final_dest.stat().st_size > 0:
        if verbose:
            print(f"Using existing OSM source: {final_dest}")
        return final_dest

    return download_file(download_url, final_dest, verbose=verbose)


def build_valhalla_tiles(
    osm_pbf_path: Union[str, Path, Sequence[Union[str, Path]]],
    *,
    tile_dir: Union[str, Path] = "valhalla_tiles",
    tile_extract: Union[str, Path] = "valhalla_tiles.tar",
    config_path: Union[str, Path, None] = None,
    admin_db_path: Union[str, Path, None] = None,
    build_admins: bool = False,
    overwrite: bool = False,
    verbose: bool = True,
) -> Path:
    """
    Build Valhalla routing tiles from one or more OpenStreetMap .osm.pbf files and package into a .tar extract.

    Args:
        osm_pbf_path: Path or sequence of paths to source .osm.pbf files.
        tile_dir: Directory where individual .gph tiles will be generated.
        tile_extract: Path to the target consolidated .tar tile archive.
        config_path: Path to write the temporary or permanent valhalla.json config.
        admin_db_path: Optional path to compiled admin.sqlite database for boundary/access attribution.
        build_admins: If True, compiles administrative boundaries db via valhalla_build_admins.
        overwrite: Overwrite existing tiles if present.
        verbose: Display build logging.

    Returns:
        Path to the generated tile_extract (.tar file).
    """
    if isinstance(osm_pbf_path, (str, Path)):
        pbf_list = [Path(osm_pbf_path).resolve()]
    else:
        pbf_list = [Path(p).resolve() for p in osm_pbf_path]

    for pbf in pbf_list:
        if not pbf.exists():
            raise FileNotFoundError(f"OSM PBF file not found: {pbf}")

    t_dir = Path(tile_dir).resolve()
    t_extract = Path(tile_extract).resolve()

    if t_extract.exists() and not overwrite:
        if verbose:
            print(f"Tile extract already exists at {t_extract}")
        return t_extract

    t_dir.mkdir(parents=True, exist_ok=True)
    t_extract.parent.mkdir(parents=True, exist_ok=True)

    # Import pyvalhalla config utilities
    try:
        import valhalla
        from valhalla.config import default_config, _sanitize_config
    except ImportError as e:
        raise ImportError("pyvalhalla is required to build tiles. Install with: pip install pyvalhalla") from e

    # Build sanitized config dictionary
    cfg = _sanitize_config(default_config.copy())
    cfg["mjolnir"]["tile_dir"] = str(t_dir).replace("\\", "/")
    cfg["mjolnir"]["tile_extract"] = str(t_extract).replace("\\", "/")

    if admin_db_path:
        admin_p = Path(admin_db_path).resolve()
        cfg["mjolnir"]["admin"] = str(admin_p).replace("\\", "/")
        cfg["mjolnir"]["data_processing"]["use_admin_db"] = True
    elif build_admins:
        cfg["mjolnir"]["admin"] = str(t_dir / "admin.sqlite").replace("\\", "/")
        cfg["mjolnir"]["data_processing"]["use_admin_db"] = True
    else:
        cfg["mjolnir"]["admin"] = ""
        cfg["mjolnir"]["data_processing"]["use_admin_db"] = False
    cfg["logging"]["type"] = "std_out" if verbose else ""

    cfg_file = Path(config_path) if config_path else t_dir.parent / "valhalla.json"
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg_file, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

    # Set up environment with pyvalhalla.libs in PATH (essential for Windows binaries)
    valhalla_pkg_dir = Path(valhalla.__file__).parent
    libs_dir = valhalla_pkg_dir.parent / "pyvalhalla.libs"
    env = os.environ.copy()
    if libs_dir.exists():
        env["PATH"] = str(libs_dir) + os.pathsep + env.get("PATH", "")

    bin_dir = valhalla_pkg_dir / "bin"
    build_tiles_exe = bin_dir / ("valhalla_build_tiles.exe" if sys.platform == "win32" else "valhalla_build_tiles")
    build_admins_exe = bin_dir / ("valhalla_build_admins.exe" if sys.platform == "win32" else "valhalla_build_admins")

    # 1. Run valhalla_build_admins (only if requested and no external admin_db_path provided)
    if build_admins and not admin_db_path and build_admins_exe.exists():
        if verbose:
            print("Building administrative boundaries (valhalla_build_admins)...")
        try:
            subprocess.run(
                [str(build_admins_exe), "-c", str(cfg_file), *[str(p) for p in pbf_list]],
                env=env,
                capture_output=not verbose,
                check=False,
            )
        except Exception as admin_err:
            LOGGER.warning(f"valhalla_build_admins skipped or failed: {admin_err}")

    # 2. Run valhalla_build_tiles
    if not build_tiles_exe.exists():
        raise FileNotFoundError(f"Valhalla build binary not found at {build_tiles_exe}")

    names_str = ", ".join(p.name for p in pbf_list)
    if verbose:
        print(f"Building Valhalla graph tiles from {names_str}...")

    res = subprocess.run(
        [str(build_tiles_exe), "-c", str(cfg_file), *[str(p) for p in pbf_list]],
        env=env,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"valhalla_build_tiles failed with code {res.returncode}.\n"
            f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        )

    # 3. Consolidate tiles into a .tar extract with index.bin
    if verbose:
        print(f"Creating tile extract archive {t_extract}...")

    res_extract = subprocess.run(
        [sys.executable, "-m", "valhalla.valhalla_build_extract", "-c", str(cfg_file), "-O"],
        env=env,
        capture_output=True,
        text=True,
    )
    if res_extract.returncode != 0:
        raise RuntimeError(
            f"valhalla_build_extract failed with code {res_extract.returncode}.\n"
            f"STDOUT:\n{res_extract.stdout}\nSTDERR:\n{res_extract.stderr}"
        )

    if verbose:
        print(f"Successfully generated Valhalla tile extract: {t_extract} ({t_extract.stat().st_size / (1024 * 1024):.2f} MB)")

    return t_extract


def ensure_tiles_exist(
    tile_extract: Union[str, Path, None] = None,
    tile_dir: Union[str, Path, None] = None,
    *,
    osm_pbf: Union[str, Path, None] = None,
    osm_url: str | None = None,
    region: str | None = None,
    coords: Sequence[tuple[float, float]] | None = None,
    osm_dir: Union[str, Path] = "osm_data",
    verbose: bool = True,
) -> Path:
    """
    Ensure Valhalla tiles are ready. If missing, automatically downloads the OSM PBF and builds them.

    Returns:
        Path to the valid valhalla_tiles.tar extract.
    """
    target_extract = Path(tile_extract or "valhalla_tiles.tar")
    target_dir = Path(tile_dir) if tile_dir else target_extract.parent / "valhalla_tiles"

    # If tile_extract exists and is valid, return it
    if target_extract.exists() and target_extract.is_file() and target_extract.stat().st_size > 0:
        return target_extract

    # If tile_dir was provided and has .gph files, pack them or use them
    if tile_dir and target_dir.exists() and any(target_dir.rglob("*.gph")):
        # Tile directory exists; build extract if tar is missing
        if not target_extract.exists():
            build_valhalla_tiles(
                osm_pbf_path=osm_pbf or (target_dir / "source.osm.pbf"),
                tile_dir=target_dir,
                tile_extract=target_extract,
                verbose=verbose,
            )
        return target_extract

    if verbose:
        print(f"Valhalla tiles not found at {target_extract}. Preparing automatic tile generation...")

    # 1. Download OpenStreetMap source file if missing
    pbf_file = download_osm_pbf(
        dest_path=osm_pbf,
        url=osm_url,
        region=region,
        coords=coords,
        output_dir=osm_dir,
        verbose=verbose,
    )

    # 2. Build the tiles from OSM PBF
    return build_valhalla_tiles(
        osm_pbf_path=pbf_file,
        tile_dir=target_dir,
        tile_extract=target_extract,
        verbose=verbose,
    )
