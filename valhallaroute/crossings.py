"""OSM Crossing extraction and spatial lookup index."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence, Union

import numpy as np


class CrossingIndexer:
    """
    Extracts, caches, and indexes pedestrian and railway crossings from OSM data.
    Provides sub-second nearest-neighbor queries within a buffer radius.
    """

    def __init__(
        self,
        crossings: list[tuple[float, float, str]] | None = None,
        osm_pbf: Union[str, Path, None] = None,
        utm_crs: str = "EPSG:32632",
    ):
        self.utm_crs = utm_crs
        self._tree = None
        self._types: list[str] = []
        self._crossings = crossings or []

        if not self._crossings and osm_pbf:
            self._crossings = self.load_or_extract_from_pbf(osm_pbf)

        if self._crossings:
            self._build_index()

    @staticmethod
    def load_or_extract_from_pbf(osm_pbf_path: Union[str, Path]) -> list[tuple[float, float, str]]:
        """Extract crossings from an OSM .pbf file or load from adjacent cache."""
        pbf_path = Path(osm_pbf_path)
        if not pbf_path.exists():
            return []

        cache_path = pbf_path.with_name(f"{pbf_path.stem}.crossings.json")
        if cache_path.exists():
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return [(float(c[0]), float(c[1]), str(c[2])) for c in data]
            except Exception:
                pass

        try:
            import osmium
        except ImportError:
            return []

        class CrossingHandler(osmium.SimpleHandler):
            def __init__(self):
                super().__init__()
                self.crossings = []

            def node(self, n):
                tags = n.tags
                if "highway" in tags and tags["highway"] == "crossing":
                    ctype = tags.get("crossing")
                    if not ctype or ctype == "yes":
                        markings = tags.get("crossing:markings") or tags.get("crossing_ref") or ""
                        if "zebra" in markings:
                            ctype = "zebra"
                        elif tags.get("traffic_signals") == "yes":
                            ctype = "traffic_signals"
                        else:
                            ctype = "pedestrian"
                    elif ctype == "uncontrolled" and ("zebra" in (tags.get("crossing_ref") or "") or "zebra" in (tags.get("crossing:markings") or "")):
                        ctype = "zebra"
                    self.crossings.append((float(n.location.lat), float(n.location.lon), ctype))
                elif "railway" in tags and tags["railway"] in ("level_crossing", "crossing"):
                    self.crossings.append((float(n.location.lat), float(n.location.lon), tags["railway"]))

        handler = CrossingHandler()
        try:
            handler.apply_file(str(pbf_path), locations=True)
            crossings = handler.crossings
            # Save cache
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(crossings, f)
            return crossings
        except Exception:
            return []

    def _build_index(self) -> None:
        if not self._crossings:
            return

        import pyproj
        from scipy.spatial import cKDTree

        lats = np.array([c[0] for c in self._crossings], dtype=float)
        lons = np.array([c[1] for c in self._crossings], dtype=float)
        self._types = [c[2] for c in self._crossings]

        transformer = pyproj.Transformer.from_crs("EPSG:4326", self.utm_crs, always_xy=True)
        east, north = transformer.transform(lons, lats)
        self._tree = cKDTree(np.column_stack((east, north)))

    def query_points(
        self,
        lats: Sequence[float],
        lons: Sequence[float],
        radius_meters: float = 10.0,
    ) -> tuple[list[bool], list[str | None]]:
        """
        Query points within radius_meters of a crossing.
        Returns: (is_crossing_list, crossing_type_list)
        """
        if self._tree is None or len(lats) == 0:
            return [False] * len(lats), [None] * len(lats)

        import pyproj

        transformer = pyproj.Transformer.from_crs("EPSG:4326", self.utm_crs, always_xy=True)
        east, north = transformer.transform(np.asarray(lons, dtype=float), np.asarray(lats, dtype=float))
        pts_xy = np.column_stack((east, north))

        dists, indices = self._tree.query(pts_xy, distance_upper_bound=radius_meters)
        matched_mask = dists <= radius_meters

        types_array = np.array(self._types, dtype=object)
        matched_indices = np.clip(indices, 0, len(self._types) - 1)

        is_crossing = matched_mask.tolist()
        crossing_types = [
            types_array[idx] if is_match else None
            for idx, is_match in zip(matched_indices, matched_mask)
        ]

        return is_crossing, crossing_types
