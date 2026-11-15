"""Tile-aligned traffic control and physical infrastructure feature extractor and spatial index."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Sequence, Union

import numpy as np
import pandas as pd
import pyproj
from scipy.spatial import cKDTree

from .storage import (
    get_feature_tile_relative_path,
    get_valhalla_tile_id,
    get_valhalla_tile_relative_path,
)

# Supported traffic control and road feature types
FEATURE_TYPES = {
    "stop",
    "give_way",
    "traffic_signals",
    "crossing",
    "railway_crossing",
    "speed_bump",
    "speed_camera",
    "toll_booth",
}


def extract_traffic_features_from_pbf(
    osm_pbf: Union[str, Path],
    tile_dir: Union[str, Path],
    level: int = 2,
    verbose: bool = False,
) -> list[Path]:
    """
    Extract traffic controls (stop signs, give-way signs, traffic lights, crossings)
    from an OSM PBF file and partition them into Valhalla Level-2 tile-aligned Parquet files.

    Args:
        osm_pbf: Path to input .osm.pbf file.
        tile_dir: Destination directory where tile hierarchy is stored (e.g. valhalla_tiles/).
        level: Valhalla hierarchy level (default: 2 = 0.25 deg x 0.25 deg).
        verbose: Print progress.

    Returns:
        List of generated .features.parquet file paths.
    """
    pbf_path = Path(osm_pbf)
    out_dir = Path(tile_dir)
    if not pbf_path.exists():
        raise FileNotFoundError(f"OSM PBF file not found: {pbf_path}")

    try:
        import osmium
    except ImportError as exc:
        raise ImportError(
            "osmium is required to extract features from OSM PBF. Install with: pip install osmium"
        ) from exc

    class FeatureHandler(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.records = []

        def node(self, n):
            hw = n.tags.get("highway")
            rw = n.tags.get("railway")
            tc = n.tags.get("traffic_calming")
            b = n.tags.get("barrier")

            ftype = None
            subtype = None
            direction = n.tags.get("direction")

            if hw == "stop":
                ftype = "stop"
            elif hw in ("give_way", "yield"):
                ftype = "give_way"
            elif hw == "traffic_signals":
                ftype = "traffic_signals"
            elif hw == "crossing":
                ftype = "crossing"
                subtype = n.tags.get("crossing") or n.tags.get("crossing_ref")
            elif rw in ("crossing", "level_crossing"):
                ftype = "railway_crossing"
                subtype = rw
            elif tc:
                ftype = "speed_bump"
                subtype = tc
            elif hw == "speed_camera":
                ftype = "speed_camera"
            elif b == "toll_booth":
                ftype = "toll_booth"

            if ftype:
                lat = n.location.lat
                lon = n.location.lon
                self.records.append((n.id, ftype, subtype, direction, lat, lon))

    if verbose:
        print(f"Extracting traffic features from {pbf_path.name}...")

    handler = FeatureHandler()
    handler.apply_file(str(pbf_path))

    if not handler.records:
        if verbose:
            print(f"No traffic control features found in {pbf_path.name}.")
        return []

    # Build DataFrame
    df = pd.DataFrame(
        handler.records,
        columns=["osm_id", "feature_type", "subtype", "direction", "latitude", "longitude"],
    )

    # Calculate Level-2 Tile ID for each point
    lats = df["latitude"].to_numpy(dtype=float)
    lons = df["longitude"].to_numpy(dtype=float)
    tile_ids = [get_valhalla_tile_id(lat, lon, level=level) for lat, lon in zip(lats, lons)]
    df["tile_id"] = tile_ids

    generated_paths: list[Path] = []

    # Write companion Parquet for each tile
    for t_id, grp in df.groupby("tile_id"):
        rel_path = get_feature_tile_relative_path(int(t_id), level=level)
        dest_file = out_dir / rel_path
        dest_file.parent.mkdir(parents=True, exist_ok=True)

        feature_cols = ["osm_id", "feature_type", "subtype", "direction", "latitude", "longitude"]
        grp_to_write = grp[feature_cols].copy()

        # If tile already exists (from an adjacent extract), merge and deduplicate
        if dest_file.exists():
            try:
                existing_df = pd.read_parquet(dest_file)
                merged = pd.concat([existing_df, grp_to_write], ignore_index=True)
                grp_to_write = merged.drop_duplicates(subset=["osm_id"])
            except Exception:
                pass

        grp_to_write.to_parquet(dest_file, index=False, engine="pyarrow", compression="snappy")
        generated_paths.append(dest_file)

    if verbose:
        print(f"Generated {len(generated_paths)} tile-aligned companion feature files in {out_dir}.")

    return generated_paths


class FeatureTileIndexer:
    """
    Spatial index over tile-aligned companion feature Parquet files.
    Loads companion tiles in memory and provides sub-millisecond nearest-neighbor lookups.
    """

    def __init__(
        self,
        tile_dir: Union[str, Path, None] = None,
        feature_files: Sequence[Union[str, Path]] | None = None,
        utm_crs: str = "EPSG:32632",
    ):
        self.utm_crs = utm_crs
        self._tree: cKDTree | None = None
        self._features_df: pd.DataFrame | None = None
        self._transformer = pyproj.Transformer.from_crs("EPSG:4326", self.utm_crs, always_xy=True)

        files_to_load: list[Path] = []
        if feature_files:
            files_to_load.extend(Path(f) for f in feature_files if Path(f).exists())
        elif tile_dir:
            t_dir = Path(tile_dir)
            if t_dir.exists():
                files_to_load.extend(t_dir.rglob("*.features.parquet"))

        if files_to_load:
            self._load_files(files_to_load)

    @property
    def has_features(self) -> bool:
        """True if the index contains any loaded features."""
        return self._tree is not None and self._features_df is not None and len(self._features_df) > 0

    def _load_files(self, files: Sequence[Path]) -> None:
        dfs = []
        for f in files:
            try:
                dfs.append(pd.read_parquet(f))
            except Exception:
                pass

        if not dfs:
            return

        combined = pd.concat(dfs, ignore_index=True)
        self._features_df = combined.drop_duplicates(subset=["osm_id"])

        lons = self._features_df["longitude"].to_numpy(dtype=float)
        lats = self._features_df["latitude"].to_numpy(dtype=float)
        xs, ys = self._transformer.transform(lons, lats)
        self._tree = cKDTree(np.column_stack([xs, ys]))

    def query_points(
        self,
        lats: np.ndarray,
        lons: np.ndarray,
        radius_meters: float = 15.0,
    ) -> dict[str, Any]:
        """
        Query traffic control features within radius_meters for a set of GPS coordinates.

        Returns a dictionary of matched boolean masks and metadata:
            - "has_stop_sign": bool np.ndarray
            - "has_yield_sign": bool np.ndarray
            - "has_traffic_signal": bool np.ndarray
            - "is_crossing": bool np.ndarray
            - "crossing_type": list[str | None]
            - "has_speed_bump": bool np.ndarray
        """
        n = len(lats)
        result = {
            "has_stop_sign": np.zeros(n, dtype=bool),
            "has_yield_sign": np.zeros(n, dtype=bool),
            "has_traffic_signal": np.zeros(n, dtype=bool),
            "is_crossing": np.zeros(n, dtype=bool),
            "crossing_type": [None] * n,
            "has_speed_bump": np.zeros(n, dtype=bool),
        }

        if not self.has_features or n == 0:
            return result

        pts_x, pts_y = self._transformer.transform(lons, lats)
        coords = np.column_stack([pts_x, pts_y])

        # Query all neighbors within radius_meters
        neighbors = self._tree.query_ball_point(coords, r=radius_meters)

        ftypes = self._features_df["feature_type"].to_numpy()
        subtypes = self._features_df["subtype"].to_numpy()

        for i, idx_list in enumerate(neighbors):
            if not idx_list:
                continue

            for idx in idx_list:
                ft = ftypes[idx]
                if ft == "stop":
                    result["has_stop_sign"][i] = True
                elif ft == "give_way":
                    result["has_yield_sign"][i] = True
                elif ft == "traffic_signals":
                    result["has_traffic_signal"][i] = True
                elif ft in ("crossing", "railway_crossing"):
                    result["is_crossing"][i] = True
                    st = subtypes[idx]
                    result["crossing_type"][i] = str(st) if pd.notna(st) and st else ft
                elif ft == "speed_bump":
                    result["has_speed_bump"][i] = True

        return result
