"""Command-line interface for ValhallaRoute."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .admin_builder import build_admin_sqlite_from_overture
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
        description="Plan distributed Kubernetes batch tile generation by generating a balanced JSONL task manifest for regions.",
    )
    parser.add_argument(
        "--regions",
        nargs="+",
        dest="regions",
        help="List of Geofabrik region names, slugs, or wildcards (e.g. 'bayern', 'berlin', 'germany/*').",
    )
    parser.add_argument(
        "--regions-file",
        dest="regions_file",
        help="Path or URI to text file containing region names (one per line, # comments allowed).",
    )
    parser.add_argument(
        "-m", "--manifest",
        dest="output_manifest_uri",
        required=True,
        help="Destination URI or path for the generated task manifest (JSONL).",
    )
    parser.add_argument(
        "-o", "--output", "--tile-storage-uri",
        dest="tile_storage_uri",
        default="s3://valhalla-tiles/global/",
        help="Destination blob URI prefix or local directory where generated .gph tiles will be uploaded (default: s3://valhalla-tiles/global/).",
    )
    parser.add_argument(
        "--auto-subdivide",
        dest="auto_subdivide",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Automatically subdivide mega-regions exceeding max-tiles into child regions (default: True).",
    )
    parser.add_argument(
        "--max-tiles",
        dest="max_tiles_per_job",
        type=int,
        default=300,
        help="Maximum estimated tiles per region before auto-subdividing (default: 300).",
    )
    parser.add_argument(
        "--bin-pack",
        dest="bin_pack",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Bin-pack small regions (< min-tiles) into balanced worker tasks (default: True).",
    )
    parser.add_argument(
        "--target-tiles",
        dest="target_tiles_per_job",
        type=int,
        default=200,
        help="Target tile count per worker task during bin-packing (default: 200).",
    )
    parser.add_argument(
        "--min-tiles",
        dest="min_tiles_per_job",
        type=int,
        default=50,
        help="Minimum tile threshold before small regions are bin-packed (default: 50).",
    )
    parser.add_argument(
        "--admin-boundaries",
        dest="build_admins",
        action="store_true",
        help="Compile admin.sqlite from Overture Maps and upload to tile storage root.",
    )
    parser.add_argument(
        "--admin-countries",
        dest="admin_countries",
        nargs="+",
        help="Explicit country ISO codes for admin.sqlite (defaults to auto-detected from regions).",
    )
    return parser


def build_run_worker_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="valhallaroute run-worker",
        description="Execute a single indexed worker task from the batch manifest to build and upload tiles.",
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
        "-o", "--output", "--tile-storage-uri",
        dest="tile_storage_uri",
        help="Override destination blob storage prefix or local directory for generated .gph tiles.",
    )
    parser.add_argument(
        "--admin-db-uri",
        dest="admin_db_uri",
        help="Override URI or path to admin.sqlite database.",
    )
    parser.add_argument(
        "--temp-dir",
        dest="temp_dir",
        help="Custom local temporary directory for downloading PBF and building tiles.",
    )
    parser.add_argument(
        "--keep-temp",
        dest="keep_temp",
        action="store_true",
        help="Do not delete temporary download and build files upon completion.",
    )
    return parser


def build_build_admin_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="valhallaroute build-admin",
        description="Compile a 100% compliant SpatiaLite admin.sqlite from Overture Maps GeoParquet data.",
    )
    parser.add_argument(
        "-o", "--output",
        dest="output",
        default="admin.sqlite",
        help="Destination path for compiled admin.sqlite (default: admin.sqlite).",
    )
    parser.add_argument(
        "-c", "--countries",
        dest="countries",
        nargs="+",
        help="Optional ISO-3166-1 alpha-2 country codes (e.g. -c NL BE LU or -c DE AT CH). Omit for full planet.",
    )
    parser.add_argument(
        "--admin-levels",
        dest="admin_levels",
        default="2,4",
        help="Comma-separated administrative levels to include: 2 (country), 4 (region/state), 6 (county), 8 (city) (default: 2,4).",
    )
    parser.add_argument(
        "--release",
        dest="release",
        default="2026-10-21.0",
        help="Overture Maps release version (default: 2026-10-21.0).",
    )
    parser.add_argument(
        "--s3-region",
        dest="s3_region",
        default="us-west-2",
        help="AWS region for Overture public S3 bucket (default: us-west-2).",
    )
    parser.add_argument(
        "--no-maritime",
        dest="no_maritime",
        action="store_true",
        help="Exclude maritime territorial boundaries and keep only land boundaries.",
    )
    return parser


def build_continent_pack_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="valhallaroute build-continents",
        description="Compile global/continental Valhalla routing tiles and companion feature parquets on a single node.",
    )
    parser.add_argument(
        "--continents",
        default="europe,north-america,south-america,oceania",
        help="Comma-separated continent slugs (e.g. europe,north-america,south-america,oceania or 'all').",
    )
    parser.add_argument(
        "--destination-storage",
        default="valhalla_tiles_global",
        help="Target blob storage prefix or local directory (e.g. az://account/container/prefix, s3://..., or path).",
    )
    parser.add_argument(
        "--work-dir",
        default="./valhalla_continent_work",
        help="Scratch directory on high-speed local disk/NVMe (default: ./valhalla_continent_work).",
    )
    parser.add_argument(
        "--build-admins",
        action="store_true",
        default=True,
        help="Compile global admin.sqlite from Overture Maps before building tiles (default: True).",
    )
    parser.add_argument(
        "--no-build-admins",
        dest="build_admins",
        action="store_false",
        help="Skip compilation of admin.sqlite.",
    )
    parser.add_argument(
        "--no-cleanup",
        dest="cleanup_after_continent",
        action="store_false",
        default=True,
        help="Keep downloaded PBFs and generated tiles locally after upload (default: cleans up per continent).",
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
                regions=args.regions,
                regions_file=args.regions_file,
                output_manifest_uri=args.output_manifest_uri,
                tile_storage_uri=args.tile_storage_uri,
                auto_subdivide=args.auto_subdivide,
                max_tiles_per_job=args.max_tiles_per_job,
                bin_pack=args.bin_pack,
                target_tiles_per_job=args.target_tiles_per_job,
                min_tiles_per_job=args.min_tiles_per_job,
                build_admins=args.build_admins,
                admin_countries=args.admin_countries,
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
                admin_db_uri=args.admin_db_uri,
                temp_dir=args.temp_dir,
                keep_temp=args.keep_temp,
            )
            return 0
        except Exception as e:
            print(f"Worker error: {e}", file=sys.stderr)
            return 2

    if argv and argv[0] == "build-admin":
        parser = build_build_admin_parser()
        args = parser.parse_args(argv[1:])
        levels = [int(lvl.strip()) for lvl in args.admin_levels.split(",") if lvl.strip()]
        try:
            stats = build_admin_sqlite_from_overture(
                output_path=args.output,
                countries=args.countries,
                admin_levels=levels,
                release=args.release,
                s3_region=args.s3_region,
                include_maritime=not args.no_maritime,
                verbose=True,
            )
            print(
                f"Successfully compiled {stats['output_path']}: {stats['total_records']} administrative entities "
                f"({stats['file_size_bytes'] / (1024 * 1024):.2f} MB) across {len(stats['countries'])} countries."
            )
            return 0
        except Exception as e:
            print(f"Build admin error: {e}", file=sys.stderr)
            return 3

    if argv and argv[0] in ("build-continent-pack", "build-continents"):
        from .continents import build_continent_pack
        parser = build_continent_pack_parser()
        args = parser.parse_args(argv[1:])
        try:
            summary = build_continent_pack(
                continents=args.continents,
                destination_storage_uri=args.destination_storage,
                build_admins=args.build_admins,
                work_dir=args.work_dir,
                cleanup_after_continent=args.cleanup_after_continent,
                verbose=True,
            )
            return 0
        except Exception as e:
            print(f"Continental pack build error: {e}", file=sys.stderr)
            return 4

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
