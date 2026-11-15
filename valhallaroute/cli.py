"""Command-line interface for ValhallaRoute."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .batch import plan_batch, run_worker
from .enricher import enrich_gps_points
from .storage import StorageManager, uri_to_local_path


def build_enrich_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="valhallaroute [enrich]",
        description="Enrich ordered GPS points from CSV/Parquet using Valhalla.",
    )
    parser.add_argument("input_csv", help="Path or URI to input GPS CSV or Parquet file.")
    parser.add_argument(
        "-o", "--output",
        dest="output_csv",
        help="Path or URI for output enriched file (defaults to stdout or input_enriched.csv).",
    )
    parser.add_argument(
        "--tile-extract",
        dest="tile_extract",
        help="Path to Valhalla .tar tile extract for local pyvalhalla Actor.",
    )
    parser.add_argument(
        "--tile-dir",
        dest="tile_dir",
        help="Path or URI to directory containing Valhalla tiles.",
    )
    parser.add_argument(
        "--tile-storage-uri",
        dest="tile_storage_uri",
        help="Remote blob storage URI (e.g. s3://bucket/tiles, gs://bucket/tiles) for sparse on-demand .gph tile fetching.",
    )
    parser.add_argument(
        "--tile-cache-dir",
        dest="tile_cache_dir",
        default="valhalla_tiles_cache",
        help="Local directory path to cache downloaded .gph tiles (default: valhalla_tiles_cache).",
    )
    parser.add_argument(
        "--url",
        dest="valhalla_url",
        help="URL of running Valhalla HTTP instance (e.g. http://localhost:8002).",
    )
    parser.add_argument(
        "--costing",
        default="auto",
        choices=["auto", "bicycle", "pedestrian", "motorcycle", "bus", "truck"],
        help="Valhalla costing model (default: auto).",
    )
    parser.add_argument(
        "--lat-col",
        dest="lat_col",
        help="Explicit latitude column name in input.",
    )
    parser.add_argument(
        "--lon-col",
        dest="lon_col",
        help="Explicit longitude column name in input.",
    )
    parser.add_argument(
        "--trip-col",
        dest="trip_col",
        help="Explicit trip ID column name in input (for grouping).",
    )
    parser.add_argument(
        "--utm-crs",
        dest="utm_crs",
        default="EPSG:32632",
        help="CRS for projected UTM coordinates (default: EPSG:32632).",
    )
    parser.add_argument(
        "--osm-pbf",
        dest="osm_pbf",
        help="Path to local OpenStreetMap .osm.pbf file to build tiles from.",
    )
    parser.add_argument(
        "--osm-url",
        dest="osm_url",
        help="URL to download .osm.pbf file from if missing.",
    )
    parser.add_argument(
        "--region",
        dest="region",
        help="Geofabrik region name (e.g. 'monaco', 'berlin') to auto-download if tiles are missing.",
    )
    parser.add_argument(
        "--crossing-buffer",
        dest="crossing_buffer",
        type=float,
        default=10.0,
        help="Search radius around OSM crossings in meters (default: 10.0).",
    )
    parser.add_argument(
        "--intersection-buffer",
        dest="intersection_buffer",
        type=float,
        default=10.0,
        help="Search radius around road intersections in meters (default: 10.0).",
    )
    parser.add_argument(
        "--roundabout-buffer",
        dest="roundabout_buffer",
        type=float,
        default=10.0,
        help="Search radius for entering/exiting roundabouts in meters (default: 10.0).",
    )
    parser.add_argument(
        "--no-auto-tiles",
        dest="no_auto_tiles",
        action="store_true",
        help="Disable automatic download of OSM data and tile generation when tiles are missing.",
    )
    return parser


def build_plan_batch_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="valhallaroute plan-batch",
        description="Plan distributed Kubernetes batch enrichment by generating a JSONL task manifest.",
    )
    parser.add_argument(
        "-i", "--input",
        dest="input_uri",
        required=True,
        help="Input URI or path to directory, prefix, or single CSV/Parquet file.",
    )
    parser.add_argument(
        "-m", "--manifest",
        dest="output_manifest_uri",
        required=True,
        help="Destination URI or path for the generated task manifest (JSONL).",
    )
    parser.add_argument(
        "-o", "--output-prefix",
        dest="output_prefix",
        required=True,
        help="Destination URI prefix or directory for enriched output partitions.",
    )
    parser.add_argument(
        "--tile-storage-uri",
        dest="tile_storage_uri",
        help="Remote blob URI containing Valhalla .gph tiles.",
    )
    parser.add_argument(
        "--batch-size",
        dest="batch_size",
        type=int,
        default=1,
        help="Number of files or trips per indexed task chunk (default: 1).",
    )
    parser.add_argument(
        "--trip-col",
        dest="trip_col",
        help="Optional trip ID column name for partitioning single multi-trip files.",
    )
    parser.add_argument(
        "--format",
        dest="output_ext",
        choices=["parquet", "csv"],
        default="parquet",
        help="Output file format (default: parquet).",
    )
    parser.add_argument(
        "--costing",
        dest="costing",
        default="auto",
        help="Valhalla costing model (default: auto).",
    )
    return parser


def build_run_worker_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="valhallaroute run-worker",
        description="Execute a single indexed worker task from the batch manifest.",
    )
    parser.add_argument(
        "-m", "--manifest",
        dest="manifest_uri",
        required=True,
        help="URI or path to task manifest (JSONL).",
    )
    parser.add_argument(
        "--task-index",
        dest="task_index",
        type=int,
        default=None,
        help="Task completion index (defaults to $JOB_COMPLETION_INDEX environment variable).",
    )
    parser.add_argument(
        "--tile-storage-uri",
        dest="tile_storage_uri",
        help="Remote blob storage URI for sparse on-demand .gph tile fetching.",
    )
    parser.add_argument(
        "--tile-cache-dir",
        dest="tile_cache_dir",
        default="valhalla_tiles_cache",
        help="Local directory path to cache downloaded .gph tiles (default: valhalla_tiles_cache).",
    )
    parser.add_argument(
        "--costing",
        default="auto",
        choices=["auto", "bicycle", "pedestrian", "motorcycle", "bus", "truck"],
        help="Valhalla costing model (default: auto).",
    )
    parser.add_argument(
        "--lat-col",
        dest="lat_col",
        help="Explicit latitude column name in input.",
    )
    parser.add_argument(
        "--lon-col",
        dest="lon_col",
        help="Explicit longitude column name in input.",
    )
    parser.add_argument(
        "--trip-col",
        dest="trip_col",
        help="Explicit trip ID column name in input.",
    )
    parser.add_argument(
        "--utm-crs",
        dest="utm_crs",
        default="EPSG:32632",
        help="CRS for projected UTM coordinates (default: EPSG:32632).",
    )
    parser.add_argument(
        "--crossing-buffer",
        dest="crossing_buffer",
        type=float,
        default=10.0,
        help="Search radius around OSM crossings in meters (default: 10.0).",
    )
    parser.add_argument(
        "--intersection-buffer",
        dest="intersection_buffer",
        type=float,
        default=10.0,
        help="Search radius around road intersections in meters (default: 10.0).",
    )
    parser.add_argument(
        "--roundabout-buffer",
        dest="roundabout_buffer",
        type=float,
        default=10.0,
        help="Search radius for entering/exiting roundabouts in meters (default: 10.0).",
    )
    parser.add_argument(
        "--no-auto-tiles",
        dest="no_auto_tiles",
        action="store_true",
        help="Disable automatic download of OSM data and tile generation when tiles are missing.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # Check for subcommands
    if argv and argv[0] == "plan-batch":
        parser = build_plan_batch_parser()
        args = parser.parse_args(argv[1:])
        try:
            tasks = plan_batch(
                input_uri=args.input_uri,
                output_manifest_uri=args.output_manifest_uri,
                output_prefix=args.output_prefix,
                tile_storage_uri=args.tile_storage_uri,
                batch_size=args.batch_size,
                trip_col=args.trip_col,
                output_ext=args.output_ext,
                costing=args.costing,
            )
            print(f"Successfully generated {len(tasks)} tasks in manifest: {args.output_manifest_uri}")
            return 0
        except Exception as e:
            print(f"Error planning batch: {e}", file=sys.stderr)
            return 1

    if argv and argv[0] == "run-worker":
        parser = build_run_worker_parser()
        args = parser.parse_args(argv[1:])
        try:
            run_worker(
                manifest_uri=args.manifest_uri,
                task_index=args.task_index,
                tile_storage_uri=args.tile_storage_uri,
                tile_cache_dir=args.tile_cache_dir,
                costing=args.costing,
                utm_crs=args.utm_crs,
                crossing_buffer_meters=args.crossing_buffer,
                intersection_buffer_meters=args.intersection_buffer,
                roundabout_buffer_meters=args.roundabout_buffer,
                trip_col=args.trip_col,
                lat_col=args.lat_col,
                lon_col=args.lon_col,
                auto_build_tiles=not args.no_auto_tiles,
            )
            return 0
        except Exception as e:
            print(f"Worker error: {e}", file=sys.stderr)
            return 2

    # Standard enrichment subcommand or default
    if argv and argv[0] == "enrich":
        argv = argv[1:]

    parser = build_enrich_parser()
    args = parser.parse_args(argv)

    local_input = uri_to_local_path(args.input_csv)
    if local_input is not None:
        if not local_input.exists():
            print(f"Error: input file '{local_input}' not found.", file=sys.stderr)
            return 1
        csv_input = local_input
    else:
        csv_input = args.input_csv

    try:
        enriched_df = enrich_gps_points(
            csv_input=csv_input,
            tile_extract=args.tile_extract,
            tile_dir=args.tile_dir,
            tile_storage_uri=args.tile_storage_uri,
            tile_cache_dir=args.tile_cache_dir,
            valhalla_url=args.valhalla_url,
            costing=args.costing,
            lat_col=args.lat_col,
            lon_col=args.lon_col,
            trip_col=args.trip_col,
            utm_crs=args.utm_crs,
            crossing_buffer_meters=args.crossing_buffer,
            intersection_buffer_meters=args.intersection_buffer,
            roundabout_buffer_meters=args.roundabout_buffer,
            auto_build_tiles=not args.no_auto_tiles,
            osm_pbf=args.osm_pbf,
            osm_url=args.osm_url,
            region=args.region,
        )
    except Exception as e:
        print(f"Enrichment error: {e}", file=sys.stderr)
        return 2

    if args.output_csv:
        out_target = args.output_csv
        local_out = uri_to_local_path(out_target)
        if local_out is not None:
            local_out.parent.mkdir(parents=True, exist_ok=True)
            if out_target.lower().endswith((".parquet", ".pq")):
                enriched_df.to_parquet(local_out, index=False)
            else:
                enriched_df.to_csv(local_out, index=False)
            print(f"Successfully wrote {len(enriched_df)} enriched points to {local_out}")
        else:
            out_mgr = StorageManager.from_uri(out_target)
            if out_target.lower().endswith((".parquet", ".pq")):
                out_mgr.write_parquet(enriched_df, "", index=False)
            else:
                out_mgr.write_csv(enriched_df, "", index=False)
            print(f"Successfully uploaded {len(enriched_df)} enriched points to {out_target}")
    else:
        # Print preview to console
        print(enriched_df.to_string())

    return 0


if __name__ == "__main__":
    sys.exit(main())
