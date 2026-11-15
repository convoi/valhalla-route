"""Core route enrichment engine using Valhalla and offline geocoding."""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Sequence, Union

import pandas as pd

from .client import ActorBackend, HttpBackend, ValhallaBackend, resolve_backend
from .geocoder import lookup_coordinates


# Common column name heuristics for coordinates and timestamps
LAT_CANDIDATES = ("lat", "latitude", "lat_deg", "y", "lat_dd", "point_lat", "north_utm", "northing", "y_utm")
LON_CANDIDATES = ("lon", "lng", "longitude", "long", "lon_deg", "x", "lon_dd", "point_lon", "east_utm", "easting", "x_utm")
TIME_CANDIDATES = ("time", "timestamp", "datetime", "date_time", "t", "epoch", "unixtime")
TRIP_CANDIDATES = ("trip_id", "trip", "trajectory_id", "track_id")

ROAD_CLASS_HIERARCHY = {
    "motorway": 1,
    "trunk": 2,
    "primary": 3,
    "secondary": 4,
    "tertiary": 5,
    "unclassified": 6,
    "residential": 7,
    "service_other": 8,
}


def _find_column(columns: Sequence[str], candidates: Sequence[str]) -> str | None:
    """Find a column matching one of the candidate names (case-insensitive)."""
    col_map = {str(c).lower().strip(): str(c) for c in columns}
    for cand in candidates:
        if cand.lower() in col_map:
            return col_map[cand.lower()]
    return None


def read_gps_csv(
    csv_input: Union[str, Path, io.StringIO, io.TextIOBase, pd.DataFrame],
    lat_col: str | None = None,
    lon_col: str | None = None,
    time_col: str | None = None,
    utm_crs: str = "EPSG:32632",
) -> tuple[pd.DataFrame, str, str, str | None]:
    """
    Read GPS points from various input sources into a DataFrame and resolve coordinate columns.
    Automatically handles projected UTM coordinates by converting them to WGS84.

    Args:
        csv_input: Path to CSV, file-like object, raw CSV string, or DataFrame.
        lat_col: Explicit name of latitude column.
        lon_col: Explicit name of longitude column.
        time_col: Explicit name of time/timestamp column.
        utm_crs: Coordinate reference system for projected UTM data (default EPSG:32632).

    Returns:
        (df, resolved_lat_col, resolved_lon_col, resolved_time_col)
    """
    if isinstance(csv_input, pd.DataFrame):
        df = csv_input.copy()
    elif isinstance(csv_input, (str, Path)):
        # Check if str is a filepath or raw CSV string
        input_str = str(csv_input)
        if "\n" in input_str or "\r" in input_str:
            df = pd.read_csv(io.StringIO(input_str))
        else:
            df = pd.read_csv(csv_input)
    elif hasattr(csv_input, "read"):
        df = pd.read_csv(csv_input)
    else:
        raise TypeError(f"Unsupported csv_input type: {type(csv_input)}")

    # Detect coordinate columns if not provided
    actual_lat_col = lat_col or _find_column(df.columns, LAT_CANDIDATES)
    actual_lon_col = lon_col or _find_column(df.columns, LON_CANDIDATES)
    actual_time_col = time_col or _find_column(df.columns, TIME_CANDIDATES)

    if not actual_lat_col:
        raise ValueError(
            f"Could not identify latitude column in CSV. Found columns: {list(df.columns)}. "
            f"Specify lat_col explicitly."
        )
    if not actual_lon_col:
        raise ValueError(
            f"Could not identify longitude column in CSV. Found columns: {list(df.columns)}. "
            f"Specify lon_col explicitly."
        )

    # Ensure coordinates are numeric
    df[actual_lat_col] = pd.to_numeric(df[actual_lat_col], errors="coerce")
    df[actual_lon_col] = pd.to_numeric(df[actual_lon_col], errors="coerce")

    # Detect if coordinates are projected UTM (e.g. northing > 90 or easting > 180)
    is_projected = (
        "utm" in actual_lat_col.lower()
        or "utm" in actual_lon_col.lower()
        or (df[actual_lat_col].dropna().abs() > 90).any()
        or (df[actual_lon_col].dropna().abs() > 180).any()
    )

    if is_projected:
        try:
            import pyproj
            transformer = pyproj.Transformer.from_crs(utm_crs, "EPSG:4326", always_xy=True)
            # transformer expects (x, y) = (easting, northing)
            lons, lats = transformer.transform(df[actual_lon_col].values, df[actual_lat_col].values)
            df["latitude"] = lats
            df["longitude"] = lons
            actual_lat_col = "latitude"
            actual_lon_col = "longitude"
        except ImportError:
            raise ImportError("pyproj is required to convert projected UTM coordinates. Install with: pip install pyproj")

    return df, actual_lat_col, actual_lon_col, actual_time_col


def _classify_turn(angle: float) -> str:
    """Classify turn direction from signed angle in degrees (-180 to +180)."""
    abs_angle = abs(angle)
    if abs_angle > 165.0:
        return "u_turn"
    if angle > 135.0:
        return "sharp_right"
    if angle < -135.0:
        return "sharp_left"
    if angle > 45.0:
        return "right_turn"
    if angle < -45.0:
        return "left_turn"
    if angle > 20.0:
        return "slight_right"
    if angle < -20.0:
        return "slight_left"
    return "straight"


def _process_trace_attributes_response(
    response: dict[str, Any],
    num_points: int,
    intersection_buffer_meters: float = 10.0,
    roundabout_buffer_meters: float = 10.0,
) -> list[dict[str, Any]]:
    """
    Extract point-level attribution from Valhalla trace_attributes response.
    """
    matched_points = response.get("matched_points", [])
    edges = response.get("edges", [])
    admins = response.get("admins", [])

    # Pre-analyze ramp sequences in edges
    # edge_ramp_info[edge_idx] = (ramp_mode: "on" | "exit" | "interchange" | "none", cum_dist_before: float, total_ramp_len: float)
    edge_ramp_info: dict[int, tuple[str, float, float]] = {}
    e_idx = 0
    num_edges = len(edges)
    while e_idx < num_edges:
        if edges[e_idx].get("use") == "ramp":
            start_r = e_idx
            while e_idx < num_edges and edges[e_idx].get("use") == "ramp":
                e_idx += 1
            end_r = e_idx

            prev_e = edges[start_r - 1] if start_r > 0 else None
            next_e = edges[end_r] if end_r < num_edges else None

            prev_rank = ROAD_CLASS_HIERARCHY.get(prev_e.get("road_class"), 9) if prev_e else 9
            next_rank = ROAD_CLASS_HIERARCHY.get(next_e.get("road_class"), 9) if next_e else 9

            total_ramp_len = sum(edges[k].get("length", 0.0) for k in range(start_r, end_r)) * 1000.0

            if prev_rank > next_rank:
                ramp_mode = "on"
            elif prev_rank < next_rank:
                ramp_mode = "exit"
            elif prev_rank == next_rank and prev_rank <= 4:
                ramp_mode = "interchange"
            else:
                has_exit_sign = any(
                    bool(edges[k].get("sign") and any(sk in edges[k]["sign"] for sk in ("exit_number", "exit_toward", "exit_name", "exit_branch")))
                    for k in range(start_r, end_r)
                )
                if has_exit_sign:
                    ramp_mode = "exit"
                elif prev_rank <= 3:
                    ramp_mode = "exit"
                elif next_rank <= 3:
                    ramp_mode = "on"
                else:
                    ramp_mode = "none"

            cum_dist = 0.0
            for k in range(start_r, end_r):
                edge_ramp_info[k] = (ramp_mode, cum_dist, total_ramp_len)
                cum_dist += edges[k].get("length", 0.0) * 1000.0
        else:
            e_idx += 1

    results: list[dict[str, Any]] = []

    for i in range(num_points):
        pt = matched_points[i] if i < len(matched_points) else {}
        pt_type = pt.get("type", "unmatched")
        edge_index = pt.get("edge_index")

        is_on_ramp = False
        is_exit_ramp = False
        is_tunnel = False
        is_bridge = False
        is_crossing = False
        crossing_type = None
        is_intersection = False
        intersection_type = None
        has_traffic_signal = False
        lane_count = None
        is_turn = False
        turn_direction = None
        turn_angle = None
        is_roundabout = False
        roundabout_action = None
        road_name = None
        road_type = None
        country = None
        city = None

        if pt_type in ("matched", "interpolated") and edge_index is not None and 0 <= edge_index < len(edges):
            edge = edges[edge_index]
            is_tunnel = bool(edge.get("tunnel", False))
            is_bridge = bool(edge.get("bridge", False))

            # Crossing flag from Valhalla edge use or country crossing
            if edge.get("use") == "crossing":
                is_crossing = True
                crossing_type = "pedestrian"
            elif edge.get("country_crossing"):
                is_crossing = True
                crossing_type = "border_crossing"

            # Lane count
            if edge.get("lane_count") is not None:
                lane_count = int(edge["lane_count"])

            # Traffic signals on edge
            if edge.get("traffic_signal"):
                has_traffic_signal = True

            # Distance calculations along edge
            elen_m = edge.get("length", 0.0) * 1000.0
            dalong_ratio = pt.get("distance_along_edge", 0.0)
            d_from_begin = dalong_ratio * elen_m
            d_to_end = (1.0 - dalong_ratio) * elen_m

            # On-ramp and exit-ramp classification
            if edge_index in edge_ramp_info:
                rmode, dist_offset, total_ramp_len = edge_ramp_info[edge_index]
                if rmode == "on":
                    is_on_ramp = True
                elif rmode == "exit":
                    is_exit_ramp = True
                elif rmode == "interchange":
                    pt_along_seq = dist_offset + d_from_begin
                    if pt_along_seq < total_ramp_len / 2.0:
                        is_exit_ramp = True
                    else:
                        is_on_ramp = True

            # Intersection and turn detection
            if d_to_end <= intersection_buffer_meters and "end_node" in edge:
                en = edge["end_node"]
                is_intersection = True
                intersection_type = en.get("type", "street_intersection")
                if en.get("traffic_signal"):
                    has_traffic_signal = True

                if edge_index + 1 < len(edges):
                    h_in = edge.get("end_heading")
                    h_out = edges[edge_index + 1].get("begin_heading")
                    if h_in is not None and h_out is not None:
                        diff = (h_out - h_in + 180) % 360 - 180
                        turn_angle = round(float(diff), 1)
                        is_turn = bool(abs(diff) > 20.0)
                        turn_direction = _classify_turn(diff)

            elif d_from_begin <= intersection_buffer_meters and edge_index > 0 and "end_node" in edges[edge_index - 1]:
                bn = edges[edge_index - 1]["end_node"]
                is_intersection = True
                intersection_type = bn.get("type", "street_intersection")
                if bn.get("traffic_signal"):
                    has_traffic_signal = True

                h_in = edges[edge_index - 1].get("end_heading")
                h_out = edge.get("begin_heading")
                if h_in is not None and h_out is not None:
                    diff = (h_out - h_in + 180) % 360 - 180
                    turn_angle = round(float(diff), 1)
                    is_turn = bool(abs(diff) > 20.0)
                    turn_direction = _classify_turn(diff)

            # Roundabout detection & entering/traversing/exiting classification
            is_curr_rb = bool(edge.get("roundabout", False) or edge.get("use") == "roundabout")
            is_prev_rb = bool(edges[edge_index - 1].get("roundabout", False) or edges[edge_index - 1].get("use") == "roundabout") if edge_index > 0 else False
            is_next_rb = bool(edges[edge_index + 1].get("roundabout", False) or edges[edge_index + 1].get("use") == "roundabout") if edge_index + 1 < len(edges) else False

            if is_curr_rb:
                is_roundabout = True
                if not is_prev_rb and d_from_begin <= roundabout_buffer_meters:
                    roundabout_action = "entering"
                elif not is_next_rb and d_to_end <= roundabout_buffer_meters:
                    roundabout_action = "exiting"
                else:
                    roundabout_action = "traversing"
            else:
                if is_next_rb and d_to_end <= roundabout_buffer_meters:
                    is_roundabout = True
                    roundabout_action = "entering"
                elif is_prev_rb and d_from_begin <= roundabout_buffer_meters:
                    is_roundabout = True
                    roundabout_action = "exiting"

            # Road name: take primary name if available
            names = edge.get("names")
            if names and isinstance(names, list) and len(names) > 0:
                road_name = names[0]

            # Road type: road_class (e.g. motorway, primary, secondary) or use
            road_type = edge.get("road_class") or edge.get("use")

            # Country from admins
            admin_index = edge.get("admin_index")
            if admin_index is None and "end_node" in edge:
                admin_index = edge["end_node"].get("admin_index")

            if admin_index is not None and 0 <= admin_index < len(admins):
                admin_obj = admins[admin_index]
                c_text = admin_obj.get("country_text") or admin_obj.get("country_code")
                if c_text and c_text != "None":
                    country = c_text
                c_city = admin_obj.get("city")
                if c_city and c_city != "None":
                    city = c_city

        results.append({
            "valhalla_type": pt_type,
            "is_on_ramp": is_on_ramp,
            "is_exit_ramp": is_exit_ramp,
            "is_tunnel": is_tunnel,
            "is_bridge": is_bridge,
            "is_crossing": is_crossing,
            "crossing_type": crossing_type,
            "is_intersection": is_intersection,
            "intersection_type": intersection_type,
            "has_traffic_signal": has_traffic_signal,
            "lane_count": lane_count,
            "is_turn": is_turn,
            "turn_direction": turn_direction,
            "turn_angle": turn_angle,
            "is_roundabout": is_roundabout,
            "roundabout_action": roundabout_action,
            "city": city,
            "country": country,
            "road_name": road_name,
            "road_type": road_type,
        })

    return results


def _execute_single_trace(
    backend: ValhallaBackend,
    shape: list[dict[str, Any]],
    costing: str = "auto",
    shape_match: str = "map_snap",
    intersection_buffer_meters: float = 10.0,
    roundabout_buffer_meters: float = 10.0,
) -> list[dict[str, Any]]:
    """Execute a single trace_attributes request with graceful fallbacks."""
    payload = {
        "shape": shape,
        "costing": costing,
        "shape_match": shape_match,
    }
    try:
        resp = backend.trace_attributes(payload)
        return _process_trace_attributes_response(
            resp, len(shape), intersection_buffer_meters, roundabout_buffer_meters
        )
    except Exception:
        # If map_snap fails, try walk_or_snap
        if shape_match != "walk_or_snap":
            try:
                payload["shape_match"] = "walk_or_snap"
                resp = backend.trace_attributes(payload)
                return _process_trace_attributes_response(
                    resp, len(shape), intersection_buffer_meters, roundabout_buffer_meters
                )
            except Exception:
                pass

        # If both fail, return unmatched defaults
        return [
            {
                "valhalla_type": "unmatched",
                "is_on_ramp": False,
                "is_exit_ramp": False,
                "is_tunnel": False,
                "is_bridge": False,
                "is_crossing": False,
                "crossing_type": None,
                "is_intersection": False,
                "intersection_type": None,
                "has_traffic_signal": False,
                "lane_count": None,
                "is_turn": False,
                "turn_direction": None,
                "turn_angle": None,
                "is_roundabout": False,
                "roundabout_action": None,
                "city": None,
                "country": None,
                "road_name": None,
                "road_type": None,
            }
            for _ in range(len(shape))
        ]


def _call_backend_in_chunks(
    backend: ValhallaBackend,
    shape: list[dict[str, Any]],
    costing: str = "auto",
    shape_match: str = "map_snap",
    chunk_size: int = 5000,
    overlap: int = 5,
    intersection_buffer_meters: float = 10.0,
    roundabout_buffer_meters: float = 10.0,
) -> list[dict[str, Any]]:
    """
    Send shape points to Valhalla backend, handling chunking for large traces.
    """
    total_points = len(shape)
    if total_points == 0:
        return []

    # If only 1 point, duplicate it to satisfy trace_attributes minimum requirement (>=2 points)
    if total_points == 1:
        single_shape = [shape[0], shape[0]]
        res = _execute_single_trace(
            backend, single_shape, costing, shape_match, intersection_buffer_meters, roundabout_buffer_meters
        )
        return [res[0]]

    if total_points <= chunk_size:
        return _execute_single_trace(
            backend, shape, costing, shape_match, intersection_buffer_meters, roundabout_buffer_meters
        )

    # Chunking for long traces
    accumulated_results: list[dict[str, Any]] = []
    start_idx = 0

    while start_idx < total_points:
        end_idx = min(start_idx + chunk_size, total_points)
        chunk_shape = shape[start_idx:end_idx]

        chunk_res = _execute_single_trace(
            backend, chunk_shape, costing, shape_match, intersection_buffer_meters, roundabout_buffer_meters
        )

        if start_idx == 0:
            accumulated_results.extend(chunk_res)
        else:
            # Skip the overlapping points from the beginning of this chunk
            accumulated_results.extend(chunk_res[overlap:])

        if end_idx >= total_points:
            break

        start_idx = end_idx - overlap

    return accumulated_results[:total_points]


def enrich_gps_points(
    csv_input: Union[str, Path, io.StringIO, io.TextIOBase, pd.DataFrame],
    *,
    backend: ValhallaBackend | None = None,
    actor: Any = None,
    tile_extract: Union[str, Path, None] = None,
    tile_dir: Union[str, Path, None] = None,
    config: Union[dict[str, Any], str, Path, None] = None,
    valhalla_url: str | None = None,
    costing: str = "auto",
    shape_match: str = "map_snap",
    lat_col: str | None = None,
    lon_col: str | None = None,
    time_col: str | None = None,
    trip_col: str | None = None,
    utm_crs: str = "EPSG:32632",
    reverse_geocode_cities: bool = True,
    crossing_buffer_meters: float = 10.0,
    intersection_buffer_meters: float = 10.0,
    roundabout_buffer_meters: float = 10.0,
    chunk_size: int = 5000,
    auto_build_tiles: bool = True,
    osm_pbf: Union[str, Path, None] = None,
    osm_url: str | None = None,
    region: str | None = None,
    osm_dir: Union[str, Path] = "osm_data",
    verbose: bool = True,
) -> pd.DataFrame:
    """
    Enrich a list of ordered GPS points with Valhalla road and infrastructure data.

    Output columns added:
        - is_tunnel (bool)
        - is_bridge (bool)
        - is_crossing (bool)
        - crossing_type (str or None)
        - is_intersection (bool)
        - intersection_type (str or None)
        - has_traffic_signal (bool)
        - lane_count (int or None)
        - is_turn (bool)
        - turn_direction (str or None)
        - turn_angle (float or None)
        - is_roundabout (bool)
        - roundabout_action (str or None)
        - city (str or None)
        - country (str or None)
        - road_name (str or None)
        - road_type (str or None)

    Args:
        csv_input: CSV filepath, file buffer, CSV string, or DataFrame.
        backend: Explicit ValhallaBackend instance.
        actor: An initialized valhalla.Actor instance.
        tile_extract: Path to Valhalla .tar tile extract.
        tile_dir: Path to directory containing Valhalla tiles.
        config: Custom Valhalla config dictionary or json filepath.
        valhalla_url: HTTP URL to Valhalla server (e.g. 'http://localhost:8002').
        costing: Costing model ('auto', 'bicycle', 'pedestrian', etc.).
        shape_match: Shape matching method ('map_snap' or 'walk_or_snap').
        lat_col: Custom name for latitude column.
        lon_col: Custom name for longitude column.
        time_col: Custom name for timestamp column.
        trip_col: Custom name for trip ID column (groups points by trip).
        utm_crs: Coordinate reference system for projected UTM data (default EPSG:32632).
        reverse_geocode_cities: If True, resolves city/country using offline reverse geocoding.
        crossing_buffer_meters: Search radius around OSM crossings (default 10m).
        intersection_buffer_meters: Search radius around road intersections (default 10m).
        roundabout_buffer_meters: Buffer radius for roundabout entry/exit detection (default 10m).
        chunk_size: Maximum shape points per Valhalla request.
        auto_build_tiles: If True and local tiles are missing, downloads OSM data and builds them.
        osm_pbf: Path to local .osm.pbf file.
        osm_url: URL to download .osm.pbf file if missing.
        region: Geofabrik region name or ID (e.g. 'monaco', 'berlin') to download if missing.
        osm_dir: Directory where downloaded OSM source files are saved.
        verbose: Print progress messages during download and tile generation.

    Returns:
        pd.DataFrame: Original DataFrame enriched with Valhalla information.
    """
    df, resolved_lat_col, resolved_lon_col, resolved_time_col = read_gps_csv(
        csv_input,
        lat_col=lat_col,
        lon_col=lon_col,
        time_col=time_col,
        utm_crs=utm_crs,
    )

    if df.empty:
        # Return empty dataframe with target columns
        res_df = df.copy()
        res_df["valhalla_type"] = pd.Series(dtype=str)
        res_df["is_on_ramp"] = pd.Series(dtype=bool)
        res_df["is_exit_ramp"] = pd.Series(dtype=bool)
        res_df["is_tunnel"] = pd.Series(dtype=bool)
        res_df["is_bridge"] = pd.Series(dtype=bool)
        res_df["is_crossing"] = pd.Series(dtype=bool)
        res_df["crossing_type"] = pd.Series(dtype=str)
        res_df["is_intersection"] = pd.Series(dtype=bool)
        res_df["intersection_type"] = pd.Series(dtype=str)
        res_df["has_traffic_signal"] = pd.Series(dtype=bool)
        res_df["lane_count"] = pd.Series(dtype="Int64")
        res_df["is_turn"] = pd.Series(dtype=bool)
        res_df["turn_direction"] = pd.Series(dtype=str)
        res_df["turn_angle"] = pd.Series(dtype=float)
        res_df["is_roundabout"] = pd.Series(dtype=bool)
        res_df["roundabout_action"] = pd.Series(dtype=str)
        res_df["city"] = pd.Series(dtype=str)
        res_df["country"] = pd.Series(dtype=str)
        res_df["road_name"] = pd.Series(dtype=str)
        res_df["road_type"] = pd.Series(dtype=str)
        return res_df

    # Extract coordinates for geocoding and tile region detection
    coords_for_geocoding: list[tuple[float, float]] = [
        (float(lat), float(lon))
        for lat, lon in zip(df[resolved_lat_col], df[resolved_lon_col])
    ]

    # If backend is not explicitly provided, and no URL or Actor is set, check if tiles need generation
    if backend is None and actor is None and valhalla_url is None and config is None:
        target_tar = Path(tile_extract or "valhalla_tiles.tar")
        target_d = Path(tile_dir or "valhalla_tiles")
        tiles_exist = (target_tar.exists() and target_tar.stat().st_size > 0) or (target_d.exists() and any(target_d.rglob("*.gph")))

        if not tiles_exist and auto_build_tiles:
            from .tile_manager import ensure_tiles_exist

            tile_extract = ensure_tiles_exist(
                tile_extract=target_tar,
                tile_dir=target_d,
                osm_pbf=osm_pbf,
                osm_url=osm_url,
                region=region,
                coords=coords_for_geocoding,
                osm_dir=osm_dir,
                verbose=verbose,
            )

    # Resolve backend
    active_backend = resolve_backend(
        backend=backend,
        actor=actor,
        tile_extract=tile_extract,
        tile_dir=tile_dir,
        config=config,
        valhalla_url=valhalla_url,
    )

    # Check if trip column exists for grouping
    actual_trip_col = trip_col or _find_column(df.columns, TRIP_CANDIDATES)

    default_attr = {
        "valhalla_type": "unmatched",
        "is_on_ramp": False,
        "is_exit_ramp": False,
        "is_tunnel": False,
        "is_bridge": False,
        "is_crossing": False,
        "crossing_type": None,
        "is_intersection": False,
        "intersection_type": None,
        "has_traffic_signal": False,
        "lane_count": None,
        "is_turn": False,
        "turn_direction": None,
        "turn_angle": None,
        "is_roundabout": False,
        "roundabout_action": None,
        "city": None,
        "country": None,
        "road_name": None,
        "road_type": None,
    }

    matched_results: list[dict[str, Any]] = [default_attr.copy() for _ in range(len(df))]

    if actual_trip_col:
        trips = list(df.groupby(actual_trip_col, sort=False))
        total_trips = len(trips)
        if verbose and total_trips > 1:
            print(f"Processing {total_trips} trips through Valhalla map matcher...")

        has_time = resolved_time_col is not None and df[resolved_time_col].notna().any()

        for trip_idx, (t_id, grp) in enumerate(trips):
            lats = grp[resolved_lat_col].to_numpy(dtype=float)
            lons = grp[resolved_lon_col].to_numpy(dtype=float)
            if has_time:
                times = grp[resolved_time_col].to_numpy()
                shape = []
                for lat, lon, t in zip(lats, lons, times):
                    pt_dict: dict[str, Any] = {"lat": float(lat), "lon": float(lon)}
                    if pd.notna(t):
                        pt_dict["time"] = t.item() if hasattr(t, "item") else t
                    shape.append(pt_dict)
            else:
                shape = [{"lat": float(lat), "lon": float(lon)} for lat, lon in zip(lats, lons)]

            trip_matched = _call_backend_in_chunks(
                backend=active_backend,
                shape=shape,
                costing=costing,
                shape_match=shape_match,
                chunk_size=chunk_size,
                intersection_buffer_meters=intersection_buffer_meters,
                roundabout_buffer_meters=roundabout_buffer_meters,
            )

            # Assign back by row index in DataFrame
            for local_idx, row_idx in enumerate(grp.index):
                matched_results[row_idx] = trip_matched[local_idx]

            if verbose and total_trips > 1 and ((trip_idx + 1) % 50 == 0 or (trip_idx + 1) == total_trips):
                pct = ((trip_idx + 1) / total_trips) * 100
                print(f"\r  -> Trips completed: {trip_idx + 1}/{total_trips} ({pct:.1f}%)", end="", flush=True)

        if verbose and total_trips > 1:
            print()
    else:
        lats = df[resolved_lat_col].to_numpy(dtype=float)
        lons = df[resolved_lon_col].to_numpy(dtype=float)
        has_time = resolved_time_col is not None and df[resolved_time_col].notna().any()
        if has_time:
            times = df[resolved_time_col].to_numpy()
            shape = []
            for lat, lon, t in zip(lats, lons, times):
                pt_dict = {"lat": float(lat), "lon": float(lon)}
                if pd.notna(t):
                    pt_dict["time"] = t.item() if hasattr(t, "item") else t
                shape.append(pt_dict)
        else:
            shape = [{"lat": float(lat), "lon": float(lon)} for lat, lon in zip(lats, lons)]

        matched_results = _call_backend_in_chunks(
            backend=active_backend,
            shape=shape,
            costing=costing,
            shape_match=shape_match,
            chunk_size=chunk_size,
            intersection_buffer_meters=intersection_buffer_meters,
            roundabout_buffer_meters=roundabout_buffer_meters,
        )

    # OSM Crossings lookup within radius buffer
    resolved_osm_pbf = None
    if osm_pbf and Path(osm_pbf).exists():
        resolved_osm_pbf = Path(osm_pbf)
    elif Path(osm_dir).exists():
        found_pbfs = list(Path(osm_dir).glob("*.osm.pbf"))
        if found_pbfs:
            resolved_osm_pbf = found_pbfs[0]

    if resolved_osm_pbf:
        from .crossings import CrossingIndexer
        try:
            indexer = CrossingIndexer(osm_pbf=resolved_osm_pbf, utm_crs=utm_crs)
            if indexer._crossings:
                lats = df[resolved_lat_col].to_numpy(dtype=float)
                lons = df[resolved_lon_col].to_numpy(dtype=float)
                c_matches, c_types = indexer.query_points(
                    lats=lats,
                    lons=lons,
                    radius_meters=crossing_buffer_meters,
                )
                for i, (is_c, c_type) in enumerate(zip(c_matches, c_types)):
                    if is_c:
                        matched_results[i]["is_crossing"] = True
                        matched_results[i]["crossing_type"] = c_type
                        if c_type == "traffic_signals":
                            matched_results[i]["has_traffic_signal"] = True
        except Exception as exc:
            if verbose:
                print(f"Warning: Crossing lookup failed: {exc}")

    # Offline reverse geocoding for city & country fallback
    if reverse_geocode_cities and coords_for_geocoding:
        geocoded = lookup_coordinates(coords_for_geocoding)
        for i, item in enumerate(matched_results):
            geo = geocoded[i] if i < len(geocoded) else {}
            if not item.get("city") or item.get("city") == "None":
                item["city"] = geo.get("city")
            if not item.get("country") or item.get("country") == "None":
                item["country"] = geo.get("country_code")

    # Construct output columns
    enriched_df = df.copy()
    enriched_df["valhalla_type"] = [r.get("valhalla_type", "unmatched") for r in matched_results]
    enriched_df["is_on_ramp"] = [r["is_on_ramp"] for r in matched_results]
    enriched_df["is_exit_ramp"] = [r["is_exit_ramp"] for r in matched_results]
    enriched_df["is_tunnel"] = [r["is_tunnel"] for r in matched_results]
    enriched_df["is_bridge"] = [r["is_bridge"] for r in matched_results]
    enriched_df["is_crossing"] = [r["is_crossing"] for r in matched_results]
    enriched_df["crossing_type"] = [r["crossing_type"] for r in matched_results]
    enriched_df["is_intersection"] = [r["is_intersection"] for r in matched_results]
    enriched_df["intersection_type"] = [r["intersection_type"] for r in matched_results]
    enriched_df["has_traffic_signal"] = [r["has_traffic_signal"] for r in matched_results]
    enriched_df["lane_count"] = [r["lane_count"] for r in matched_results]
    enriched_df["is_turn"] = [r["is_turn"] for r in matched_results]
    enriched_df["turn_direction"] = [r["turn_direction"] for r in matched_results]
    enriched_df["turn_angle"] = [r["turn_angle"] for r in matched_results]
    enriched_df["is_roundabout"] = [r["is_roundabout"] for r in matched_results]
    enriched_df["roundabout_action"] = [r["roundabout_action"] for r in matched_results]
    enriched_df["city"] = [r["city"] for r in matched_results]
    enriched_df["country"] = [r["country"] for r in matched_results]
    enriched_df["road_name"] = [r["road_name"] for r in matched_results]
    enriched_df["road_type"] = [r["road_type"] for r in matched_results]

    return enriched_df
