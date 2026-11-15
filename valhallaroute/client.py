"""Valhalla client backends: Local Actor (pyvalhalla) and HTTP service."""
from __future__ import annotations

import abc
import os
from pathlib import Path
from typing import Any, Union

import requests


class ValhallaBackend(abc.ABC):
    """Abstract base class for Valhalla communication."""

    @abc.abstractmethod
    def trace_attributes(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send a trace_attributes request and return parsed JSON dictionary."""
        raise NotImplementedError

def _sanitize_payload(obj: Any) -> Any:
    """Recursively convert numpy types and non-standard types to Python primitives."""
    if isinstance(obj, dict):
        return {k: _sanitize_payload(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_payload(v) for v in obj]
    if hasattr(obj, "item"):  # numpy scalar types (e.g. np.int64, np.float64, np.bool_)
        return obj.item()
    return obj


class ActorBackend(ValhallaBackend):
    """
    Local in-process backend using pyvalhalla Actor.
    Directly evaluates map matching in C++ without HTTP overhead.
    """

    def __init__(
        self,
        actor: Any = None,
        *,
        tile_extract: Union[str, Path, None] = None,
        tile_dir: Union[str, Path, None] = None,
        config: Union[dict[str, Any], str, Path, None] = None,
        verbose: bool = False,
    ):
        """
        Initialize the ActorBackend.

        Args:
            actor: An already initialized valhalla.Actor instance.
            tile_extract: Path to the .tar tile extract file.
            tile_dir: Path to directory containing Valhalla tiles.
            config: A full Valhalla config dictionary or path to valhalla.json.
            verbose: Enable verbose logging from Valhalla.
        """
        if actor is not None:
            self._actor = actor
            return

        try:
            from valhalla import Actor, get_config
        except ImportError as exc:
            raise ImportError(
                "pyvalhalla is required for ActorBackend. Install it with: pip install pyvalhalla"
            ) from exc

        if config is not None:
            self._actor = Actor(config)
            return

        # Prepare configuration using tile_extract or tile_dir
        extract_arg = "" if tile_extract is None else str(tile_extract)
        dir_arg = "" if tile_dir is None else str(tile_dir)

        if not extract_arg and not dir_arg:
            # Check environment variables
            extract_arg = os.environ.get("VALHALLA_TILES_EXTRACT", "")
            dir_arg = os.environ.get("VALHALLA_TILES_DIR", "")

        if not extract_arg and not dir_arg:
            # Look for default local files
            if Path("valhalla_tiles.tar").exists():
                extract_arg = "valhalla_tiles.tar"
            elif Path("valhalla_tiles").is_dir():
                dir_arg = "valhalla_tiles"
            else:
                raise ValueError(
                    "No Valhalla tiles specified. Please provide tile_extract (e.g. 'valhalla_tiles.tar'), "
                    "tile_dir (e.g. 'valhalla_tiles/'), an existing config, or an initialized Actor instance."
                )

        cfg = get_config(
            tile_extract=extract_arg,
            tile_dir=dir_arg,
            verbose=verbose,
        )
        self._actor = Actor(cfg)

    def trace_attributes(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Execute trace_attributes query through local Actor."""
        payload = _sanitize_payload(payload)
        res = self._actor.trace_attributes(payload)
        if isinstance(res, str):
            import json
            return json.loads(res)
        return res


class HttpBackend(ValhallaBackend):
    """HTTP client backend for remote or dockerized Valhalla services."""

    def __init__(
        self,
        base_url: str = "http://localhost:8002",
        timeout: float = 60.0,
        headers: dict[str, str] | None = None,
    ):
        """
        Initialize the HttpBackend.

        Args:
            base_url: Valhalla server base URL (e.g. 'http://localhost:8002').
            timeout: Request timeout in seconds.
            headers: Optional HTTP headers (e.g. User-Agent or Authorization).
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = headers or {"Content-Type": "application/json"}

    def trace_attributes(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send POST request to /trace_attributes."""
        payload = _sanitize_payload(payload)
        endpoint = f"{self.base_url}/trace_attributes"
        resp = requests.post(
            endpoint,
            json=payload,
            headers=self.headers,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()


def resolve_backend(
    backend: ValhallaBackend | None = None,
    actor: Any = None,
    tile_extract: Union[str, Path, None] = None,
    tile_dir: Union[str, Path, None] = None,
    config: Union[dict[str, Any], str, Path, None] = None,
    valhalla_url: str | None = None,
) -> ValhallaBackend:
    """
    Resolve and construct the appropriate ValhallaBackend instance based on provided arguments.
    """
    if backend is not None:
        return backend

    if actor is not None or tile_extract is not None or tile_dir is not None or config is not None:
        return ActorBackend(
            actor=actor,
            tile_extract=tile_extract,
            tile_dir=tile_dir,
            config=config,
        )

    # Check for HTTP URL parameter or environment variable
    url = valhalla_url or os.environ.get("VALHALLA_URL")
    if url:
        return HttpBackend(base_url=url)

    # Check for Actor environment variables or local files
    tiles_env = os.environ.get("VALHALLA_TILES_EXTRACT") or os.environ.get("VALHALLA_TILES_DIR")
    config_env = os.environ.get("VALHALLA_CONFIG")
    if tiles_env or config_env:
        return ActorBackend(
            tile_extract=os.environ.get("VALHALLA_TILES_EXTRACT"),
            tile_dir=os.environ.get("VALHALLA_TILES_DIR"),
            config=config_env,
        )

    if Path("valhalla_tiles.tar").exists() or Path("valhalla_tiles").is_dir():
        return ActorBackend()

    # Default fallback: try local HTTP endpoint
    return HttpBackend(base_url="http://localhost:8002")
