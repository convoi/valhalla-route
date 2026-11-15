"""Offline reverse geocoding utility using reverse_geocoder."""
from __future__ import annotations

from typing import Any, Sequence
import reverse_geocoder as rg


def lookup_coordinates(
    coords: Sequence[tuple[float, float]],
    precision_decimals: int = 3,
) -> list[dict[str, Any]]:
    """
    Look up nearest city and country for a sequence of (latitude, longitude) coordinates.
    Deduplicates points using rounded coordinates for massive performance optimization on large traces.

    Args:
        coords: Sequence of (lat, lon) float tuples.
        precision_decimals: Decimal places to round for caching (~100m at 3 decimals).

    Returns:
        List of dicts with keys: 'city', 'country_code', 'admin1', 'admin2'.
    """
    if not coords:
        return []

    # Find unique rounded coordinates
    unique_keys: list[tuple[float, float]] = []
    seen: dict[tuple[float, float], int] = {}
    indices: list[int] = []

    for c in coords:
        key = (round(c[0], precision_decimals), round(c[1], precision_decimals))
        if key not in seen:
            seen[key] = len(unique_keys)
            unique_keys.append(key)
        indices.append(seen[key])

    # Search only the unique locations
    raw_results = rg.search(unique_keys, verbose=False)
    unique_dicts: list[dict[str, Any]] = []
    for res in raw_results:
        unique_dicts.append({
            "city": res.get("name") or None,
            "country_code": res.get("cc") or None,
            "admin1": res.get("admin1") or None,
            "admin2": res.get("admin2") or None,
        })

    # Map back to full sequence
    return [unique_dicts[idx] for idx in indices]
