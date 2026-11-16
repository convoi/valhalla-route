"""Storage and tile resolution abstraction using obstore."""
from __future__ import annotations

import asyncio
import io
import math
import os
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlparse

import obstore as obs
import pandas as pd

# Valhalla hierarchy tile sizes in degrees
VALHALLA_TILE_SIZES = {
    0: 4.0,   # Level 0 (Highways / Transit)
    1: 1.0,   # Level 1 (Arterial roads)
    2: 0.25,  # Level 2 (Local streets)
}


def get_valhalla_tile_id(lat: float, lon: float, level: int) -> int:
    """Compute the integer tile ID for a given coordinate and Valhalla hierarchy level."""
    size = VALHALLA_TILE_SIZES[level]
    col = int(math.floor((lon + 180.0) / size))
    row = int(math.floor((lat + 90.0) / size))
    n_cols = int(360.0 / size)
    return row * n_cols + col


def get_valhalla_tile_relative_path(tile_id: int, level: int) -> str:
    """Return the relative path for a tile ID at a given level."""
    if level == 0:
        return f"0/{tile_id // 1000:03d}/{tile_id % 1000:03d}.gph"
    elif level == 1:
        return f"1/{tile_id // 1000:03d}/{tile_id % 1000:03d}.gph"
    elif level == 2:
        return f"2/{tile_id // 1000000:03d}/{(tile_id // 1000) % 1000:03d}/{tile_id % 1000:03d}.gph"
    raise ValueError(f"Invalid Valhalla hierarchy level: {level}")


def get_feature_tile_relative_path(tile_id: int, level: int = 2) -> str:
    """Return the relative path for a companion feature tile (default level 2)."""
    rel = get_valhalla_tile_relative_path(tile_id, level)
    return rel[:-4] + ".features.parquet"


def get_tiles_for_bbox(
    min_lat: float,
    min_lon: float,
    max_lat: float,
    max_lon: float,
    levels: Sequence[int] = (0, 1, 2),
    buffer_degrees: float = 0.05,
) -> set[tuple[int, int, str]]:
    """
    Compute all (level, tile_id, relative_path) tuples required to cover a geographic bounding box.

    Args:
        min_lat: Minimum latitude.
        min_lon: Minimum longitude.
        max_lat: Maximum latitude.
        max_lon: Maximum longitude.
        levels: Valhalla hierarchy levels (default: 0, 1, 2).
        buffer_degrees: Padding in degrees added to bounding box (default ~5 km).

    Returns:
        Set of tuples: (level, tile_id, relative_tile_path)
    """
    tiles = set()
    b_min_lat = max(-90.0, min_lat - buffer_degrees)
    b_max_lat = min(90.0, max_lat + buffer_degrees)
    b_min_lon = max(-180.0, min_lon - buffer_degrees)
    b_max_lon = min(180.0, max_lon + buffer_degrees)

    for level in levels:
        size = VALHALLA_TILE_SIZES[level]
        n_cols = int(360.0 / size)
        n_rows = int(180.0 / size)

        min_col = max(0, int(math.floor((b_min_lon + 180.0) / size)))
        max_col = min(n_cols - 1, int(math.floor((b_max_lon + 180.0) / size)))
        min_row = max(0, int(math.floor((b_min_lat + 90.0) / size)))
        max_row = min(n_rows - 1, int(math.floor((b_max_lat + 90.0) / size)))

        for row in range(min_row, max_row + 1):
            for col in range(min_col, max_col + 1):
                tile_id = row * n_cols + col
                rel_path = get_valhalla_tile_relative_path(tile_id, level)
                tiles.add((level, tile_id, rel_path))

    return tiles


def uri_to_local_path(uri_or_path: str | Path) -> Path | None:
    """
    Convert a URI or path string to a local Path object if it is a local path or file:// URI.
    Returns None if the URI uses a remote cloud scheme (e.g. s3://, gs://, memory://).
    """
    s = str(uri_or_path).strip()
    if s.startswith("file://"):
        cleaned = s[7:]
        # On Windows, file:///C:/path or file://C:/path might leave leading /
        if cleaned.startswith("/") and len(cleaned) > 2 and cleaned[2] == ":":
            cleaned = cleaned[1:]
        return Path(cleaned)
    if "://" in s:
        return None
    return Path(s)


class StorageManager:
    """
    Unified storage manager backed by obstore.
    Enables transparent file access across local disk and S3, GCS, Azure Blob Storage.
    """

    def __init__(self, base_uri: str, **store_options: Any):
        self.base_uri = base_uri.rstrip("/")
        self.parsed = urlparse(self.base_uri)
        self.store, self.prefix = self._create_store(self.base_uri, self.parsed, store_options)
        # If prefix ends with 'latest', automatically resolve latest.txt snapshot pointer if present
        if self.prefix == "latest" or self.prefix.endswith("/latest"):
            if self.prefix == "latest":
                parent_key = ""
                pointer_key = "latest.txt"
            else:
                parent_key = self.prefix[:-len("/latest")]
                pointer_key = f"{parent_key}/latest.txt"

            try:
                resp = obs.get(self.store, pointer_key)
                pointer = bytes(resp.bytes()).decode("utf-8").strip()
                if pointer:
                    self.prefix = f"{parent_key}/{pointer}".strip("/")
            except Exception:
                pass

    @classmethod
    def from_uri(cls, uri: str, **kwargs: Any) -> StorageManager:
        """Create a StorageManager instance from a URI string."""
        return cls(uri, **kwargs)

    @staticmethod
    def _create_store(
        base_uri: str, parsed: Any, options: dict[str, Any]
    ) -> tuple[obs.store.ObjectStore, str]:
        scheme = parsed.scheme.lower()
        if scheme in ("s3", "s3a"):
            bucket = parsed.netloc
            prefix = parsed.path.lstrip("/")
            return obs.store.S3Store(bucket=bucket, **options), prefix
        elif scheme in ("gs", "gcs"):
            bucket = parsed.netloc
            prefix = parsed.path.lstrip("/")
            return obs.store.GCSStore(bucket=bucket, **options), prefix
        elif scheme in ("az", "azure", "abfs"):
            # Flexible Azure Blob / ADLS Gen2 URL resolution:
            # 1. abfs://container@account.dfs.core.windows.net/prefix
            # 2. az://account.blob.core.windows.net/container/prefix
            # 3. az://account/container/prefix
            # 4. az://container/prefix (with AZURE_STORAGE_ACCOUNT / AZURE_STORAGE_ACCOUNT_NAME in env or options)
            account_name = (
                options.pop("account_name", None)
                or options.pop("account", None)
                or os.environ.get("AZURE_STORAGE_ACCOUNT")
                or os.environ.get("AZURE_STORAGE_ACCOUNT_NAME")
            )
            netloc = parsed.netloc
            raw_path = parsed.path.lstrip("/")

            if "@" in netloc:
                container, host = netloc.split("@", 1)
                if not account_name:
                    account_name = host.split(".")[0]
                prefix = raw_path
            elif ".blob.core.windows.net" in netloc or ".dfs.core.windows.net" in netloc:
                if not account_name:
                    account_name = netloc.split(".")[0]
                parts = raw_path.split("/", 1)
                container = parts[0]
                prefix = parts[1] if len(parts) > 1 else ""
            else:
                if account_name:
                    container = netloc
                    prefix = raw_path
                else:
                    parts = raw_path.split("/", 1)
                    account_name = netloc
                    container = parts[0] if parts else ""
                    prefix = parts[1] if len(parts) > 1 else ""

            store_kwargs = dict(options)
            if account_name:
                store_kwargs["account_name"] = account_name
            return obs.store.AzureStore(container_name=container, **store_kwargs), prefix
        elif scheme == "memory":
            prefix = parsed.path.lstrip("/")
            return obs.store.MemoryStore(), prefix
        elif scheme in ("", "file"):
            # Local filesystem
            raw_path = parsed.path
            if parsed.netloc and not raw_path.startswith("/"):
                raw_path = f"/{parsed.netloc}/{raw_path}"
            elif parsed.netloc:
                raw_path = f"{parsed.netloc}{raw_path}"

            if not raw_path:
                raw_path = base_uri

            local_path = Path(raw_path).resolve()
            # If path ends in 'latest', resolve pointer file latest.txt in parent directory if it exists
            if local_path.name == "latest" and (local_path.parent / "latest.txt").is_file():
                try:
                    pointer = (local_path.parent / "latest.txt").read_text("utf-8").strip()
                    if pointer:
                        local_path = (local_path.parent / pointer).resolve()
                except Exception:
                    pass

            local_path.mkdir(parents=True, exist_ok=True)
            return obs.store.LocalStore(prefix=str(local_path)), ""
        else:
            raise ValueError(f"Unsupported storage protocol: {scheme} in {base_uri}")

    def _resolve_key(self, relative_path: str) -> str:
        """Resolve full object key inside store."""
        cleaned = relative_path.replace("\\", "/").lstrip("/")
        if self.prefix:
            return f"{self.prefix}/{cleaned}"
        return cleaned

    def read_bytes(self, relative_path: str) -> bytes:
        """Read raw bytes from object store."""
        key = self._resolve_key(relative_path)
        resp = obs.get(self.store, key)
        return bytes(resp.bytes())

    async def read_bytes_async(self, relative_path: str) -> bytes:
        """Read raw bytes asynchronously from object store."""
        key = self._resolve_key(relative_path)
        resp = await obs.get_async(self.store, key)
        b = await resp.bytes_async()
        return bytes(b)

    def write_bytes(self, relative_path: str, data: bytes) -> None:
        """Write raw bytes to object store."""
        key = self._resolve_key(relative_path)
        obs.put(self.store, key, data)

    async def write_bytes_async(self, relative_path: str, data: bytes) -> None:
        """Write raw bytes asynchronously to object store."""
        key = self._resolve_key(relative_path)
        await obs.put_async(self.store, key, data)

    def exists(self, relative_path: str) -> bool:
        """Check if an object exists in storage."""
        key = self._resolve_key(relative_path)
        try:
            obs.head(self.store, key)
            return True
        except Exception:
            return False

    def fetch_tile(
        self,
        relative_tile_path: str,
        local_cache_dir: Path | str,
        overwrite: bool = False,
    ) -> Path | None:
        """
        Download a single Valhalla .gph tile from storage to local cache if missing.

        Returns:
            Path to downloaded local tile, or None if tile does not exist in store.
        """
        cache_dir = Path(local_cache_dir)
        target_path = cache_dir / relative_tile_path

        if target_path.exists() and not overwrite and target_path.stat().st_size > 0:
            return target_path

        try:
            tile_bytes = self.read_bytes(relative_tile_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(tile_bytes)
            return target_path
        except Exception:
            return None

    def ensure_tiles_for_points(
        self,
        lats: Sequence[float],
        lons: Sequence[float],
        local_cache_dir: Path | str,
        levels: Sequence[int] = (0, 1, 2),
        buffer_degrees: float = 0.05,
        include_features: bool = True,
        verbose: bool = False,
    ) -> list[Path]:
        """
        Ensure all Valhalla tiles covering the trajectory bounding box exist in local_cache_dir.
        Downloads missing .gph files and companion .features.parquet files from remote blob storage on demand.

        Args:
            lats: Sequence of latitude coordinates.
            lons: Sequence of longitude coordinates.
            local_cache_dir: Local directory serving as Valhalla's tile_dir.
            levels: Valhalla hierarchy levels (default: 0, 1, 2).
            buffer_degrees: Padding around bounding box.
            include_features: Also fetch companion .features.parquet files for Level-2 tiles.
            verbose: If True, prints download progress.

        Returns:
            List of Path objects for all cached local tiles.
        """
        if not lats or not lons:
            return []

        min_lat = min(lats)
        max_lat = max(lats)
        min_lon = min(lons)
        max_lon = max(lons)

        required_tiles = get_tiles_for_bbox(
            min_lat=min_lat,
            min_lon=min_lon,
            max_lat=max_lat,
            max_lon=max_lon,
            levels=levels,
            buffer_degrees=buffer_degrees,
        )

        cache_dir = Path(local_cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)

        downloaded_paths: list[Path] = []
        missing_rel_paths: list[str] = []

        for lvl, tile_id, rel_path in required_tiles:
            local_tile = cache_dir / rel_path
            if local_tile.exists() and local_tile.stat().st_size > 0:
                downloaded_paths.append(local_tile)
            else:
                missing_rel_paths.append(rel_path)

            if include_features and lvl == 2:
                rel_feat = get_feature_tile_relative_path(tile_id, level=2)
                local_feat = cache_dir / rel_feat
                if local_feat.exists() and local_feat.stat().st_size > 0:
                    downloaded_paths.append(local_feat)
                else:
                    missing_rel_paths.append(rel_feat)

        if not missing_rel_paths:
            return downloaded_paths

        if verbose:
            print(f"Fetching {len(missing_rel_paths)} missing Valhalla & feature tiles from {self.base_uri}...")

        # Concurrently fetch missing tiles using async loop
        async def _fetch_all(paths: list[str]) -> list[Path]:
            results: list[Path] = []

            async def _fetch_one(p: str) -> Path | None:
                try:
                    b = await self.read_bytes_async(p)
                    out_p = cache_dir / p
                    out_p.parent.mkdir(parents=True, exist_ok=True)
                    out_p.write_bytes(b)
                    return out_p
                except Exception:
                    # Tile might not exist in sparse extract (e.g. ocean or unmapped area)
                    return None

            tasks = [_fetch_one(p) for p in paths]
            fetched = await asyncio.gather(*tasks)
            for res in fetched:
                if res is not None:
                    results.append(res)
            return results

        newly_fetched = asyncio.run(_fetch_all(missing_rel_paths))
        downloaded_paths.extend(newly_fetched)

        if verbose:
            print(f"Successfully cached {len(newly_fetched)} tiles in {cache_dir}.")

        return downloaded_paths

    def read_csv(self, relative_path: str, **kwargs: Any) -> pd.DataFrame:
        """Read CSV directly from storage into a pandas DataFrame."""
        data = self.read_bytes(relative_path)
        return pd.read_csv(io.BytesIO(data), **kwargs)

    def write_csv(self, df: pd.DataFrame, relative_path: str, **kwargs: Any) -> None:
        """Write pandas DataFrame directly to storage as CSV."""
        buf = io.StringIO()
        df.to_csv(buf, **kwargs)
        self.write_bytes(relative_path, buf.getvalue().encode("utf-8"))

    def read_parquet(self, relative_path: str, **kwargs: Any) -> pd.DataFrame:
        """Read Parquet directly from storage into a pandas DataFrame."""
        data = self.read_bytes(relative_path)
        return pd.read_parquet(io.BytesIO(data), **kwargs)

    def write_parquet(self, df: pd.DataFrame, relative_path: str, **kwargs: Any) -> None:
        """Write pandas DataFrame directly to storage as Parquet."""
        buf = io.BytesIO()
        df.to_parquet(buf, **kwargs)
        self.write_bytes(relative_path, buf.getvalue())

    def list_files(self, relative_prefix: str = "") -> list[str]:
        """
        List all object paths under relative_prefix in storage.
        Returns paths relative to base_uri.
        """
        search_key = self._resolve_key(relative_prefix)
        prefix_arg = search_key if search_key else None
        results: list[str] = []
        try:
            for page in obs.list(self.store, prefix=prefix_arg):
                for item in page:
                    path_str = item["path"]
                    if self.prefix:
                        if path_str.startswith(self.prefix):
                            path_str = path_str[len(self.prefix):].lstrip("/")
                    results.append(path_str)
        except Exception:
            pass
        return sorted(results)
