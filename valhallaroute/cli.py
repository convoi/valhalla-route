"""Command-line interface for ValhallaRoute."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .enricher import enrich_gps_points


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Enrich ordered GPS points from CSV using Valhalla."
    )
    parser.add_argument("input_csv", help="Path to input GPS CSV file.")
    parser.add_argument(
        "-o", "--output",
        dest="output_csv",
        help="Path for output enriched CSV file (defaults to stdout or input_enriched.csv).",
    )
    parser.add_argument(
        "--tile-extract",
        dest="tile_extract",
        help="Path to Valhalla .tar tile extract for local pyvalhalla Actor.",
    )
    parser.add_argument(
        "--tile-dir",
        dest="tile_dir",
        help="Path to directory containing Valhalla tiles for local pyvalhalla Actor.",
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
        help="Explicit latitude column name in CSV.",
    )
    parser.add_argument(
        "--lon-col",
        dest="lon_col",
        help="Explicit longitude column name in CSV.",
    )
    parser.add_argument(
        "--trip-col",
        dest="trip_col",
        help="Explicit trip ID column name in CSV (for grouping).",
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

    args = parser.parse_args(argv)

    input_path = Path(args.input_csv)
    if not input_path.exists():
        print(f"Error: input file '{input_path}' not found.", file=sys.stderr)
        return 1

    try:
        enriched_df = enrich_gps_points(
            csv_input=input_path,
            tile_extract=args.tile_extract,
            tile_dir=args.tile_dir,
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
        out_path = Path(args.output_csv)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        enriched_df.to_csv(out_path, index=False)
        print(f"Successfully wrote {len(enriched_df)} enriched points to {out_path}")
    else:
        # Print preview to console
        print(enriched_df.to_string())

    return 0


if __name__ == "__main__":
    sys.exit(main())
