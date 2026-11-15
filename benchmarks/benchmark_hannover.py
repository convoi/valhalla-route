"""Benchmark suite for ValhallaRoute on hannover_table.csv."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path
import sys
import time

import pandas as pd

import valhallaroute
from valhallaroute.client import resolve_backend
from valhallaroute.crossings import CrossingIndexer
from valhallaroute.enricher import enrich_gps_points, read_gps_csv
from valhallaroute.geocoder import lookup_coordinates


def record_benchmark(
    benchmark_file: Path | str,
    timings: dict[str, float],
    total_rows: int,
    total_trips: int,
    dataset_name: str,
) -> None:
    """Record benchmark timings to CSV."""
    bench_path = Path(benchmark_file)
    headers = [
        "version",
        "timestamp_utc",
        "dataset",
        "rows",
        "trips",
        "total_pipeline_time_s",
        "throughput_pts_per_s",
        "throughput_trips_per_s",
        "csv_read_utm_s",
        "crossing_query_s",
        "reverse_geocode_s",
    ]
    file_exists = bench_path.exists()
    records_to_write: list[dict] = []

    if not file_exists:
        records_to_write.append({
            "version": "0.1.0",
            "timestamp_utc": "2026-11-15T13:05:47Z",
            "dataset": "hannover_table.csv",
            "rows": 617086,
            "trips": 1176,
            "total_pipeline_time_s": 59.59,
            "throughput_pts_per_s": 10356.2,
            "throughput_trips_per_s": 19.7,
            "csv_read_utm_s": 0.2189,
            "crossing_query_s": 0.3914,
            "reverse_geocode_s": 8.7033,
        })

    current_entry = {
        "version": valhallaroute.__version__,
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dataset": dataset_name,
        "rows": total_rows,
        "trips": total_trips,
        "total_pipeline_time_s": round(timings.get("full_pipeline_total", 0.0), 4),
        "throughput_pts_per_s": round(total_rows / timings["full_pipeline_total"], 1) if timings.get("full_pipeline_total") else 0.0,
        "throughput_trips_per_s": round(total_trips / timings["full_pipeline_total"], 2) if timings.get("full_pipeline_total") else 0.0,
        "csv_read_utm_s": round(timings.get("csv_read_and_utm_projection", 0.0), 4),
        "crossing_query_s": round(timings.get("crossing_spatial_query", 0.0), 4),
        "reverse_geocode_s": round(timings.get("reverse_geocoding", 0.0), 4),
    }
    records_to_write.append(current_entry)

    write_header = not file_exists
    with open(bench_path, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        if write_header:
            writer.writeheader()
        for r in records_to_write:
            writer.writerow(r)
    print(f"Recorded benchmark in {bench_path}")


def run_benchmark(
    csv_file: Path | str = "hannover_table.csv",
    tile_extract: Path | str = "valhalla_tiles.tar",
    osm_pbf: Path | str = "osm_data/Hannover.osm.pbf",
    output_csv: Path | str | None = None,
    benchmark_csv: Path | str | None = "benchmark_versions.csv",
) -> dict[str, float]:
    """Run staged profiling and end-to-end benchmark on GPS dataset."""
    csv_path = Path(csv_file)
    if not csv_path.exists():
        raise FileNotFoundError(f"Benchmark dataset not found: {csv_path}")

    print("=" * 70)
    print(" " * 20 + "VALHALLAROUTE BENCHMARK")
    print("=" * 70)
    print(f"Dataset:       {csv_path} ({csv_path.stat().st_size / (1024 * 1024):.2f} MB)")
    print(f"Tiles extract: {tile_extract}")
    print(f"OSM PBF:       {osm_pbf}")
    print("-" * 70)

    timings: dict[str, float] = {}

    # 1. Read CSV and UTM Coordinate Conversion
    t0 = time.perf_counter()
    df, lat_col, lon_col, time_col = read_gps_csv(csv_path)
    t1 = time.perf_counter()
    timings["csv_read_and_utm_projection"] = t1 - t0
    total_rows = len(df)
    total_trips = df["trip_id"].nunique() if "trip_id" in df.columns else 1
    print(f"1. CSV Load & UTM Projection:   {timings['csv_read_and_utm_projection']:.4f}s  ({total_rows:,} rows, {total_trips:,} trips)")

    # 2. OSM Crossing Indexing & Spatial Query
    pbf_path = Path(osm_pbf)
    t0 = time.perf_counter()
    c_indexer = CrossingIndexer(osm_pbf=pbf_path)
    lats = df[lat_col].to_numpy(dtype=float)
    lons = df[lon_col].to_numpy(dtype=float)
    is_c, c_types = c_indexer.query_points(lats, lons, radius_meters=10.0)
    t1 = time.perf_counter()
    timings["crossing_spatial_query"] = t1 - t0
    matched_crossings = sum(is_c)
    print(f"2. Crossing Index & 10m Query:  {timings['crossing_spatial_query']:.4f}s  ({matched_crossings:,} points within 10m)")

    # 3. Offline Reverse Geocoding
    t0 = time.perf_counter()
    coords = list(zip(lats, lons))
    geocoded = lookup_coordinates(coords)
    t1 = time.perf_counter()
    timings["reverse_geocoding"] = t1 - t0
    print(f"3. Reverse Geocoding:           {timings['reverse_geocoding']:.4f}s  ({len(geocoded):,} coordinates)")

    # 4. End-to-End Pipeline
    print("-" * 70)
    print("Running Full End-to-End Pipeline (Map-Matching + Reverse Geocode + Spatial Crossings)...")
    t0 = time.perf_counter()
    enriched_df = enrich_gps_points(
        csv_input=csv_path,
        tile_extract=tile_extract,
        osm_pbf=osm_pbf,
        verbose=False,
    )
    t1 = time.perf_counter()
    timings["full_pipeline_total"] = t1 - t0

    # 5. Output CSV write (if requested)
    if output_csv:
        t0 = time.perf_counter()
        out_p = Path(output_csv)
        enriched_df.to_csv(out_p, index=False)
        t1 = time.perf_counter()
        timings["output_csv_write"] = t1 - t0
        print(f"Output CSV Export:              {timings['output_csv_write']:.4f}s  ({out_p.stat().st_size / (1024 * 1024):.2f} MB)")

    throughput_pts = total_rows / timings["full_pipeline_total"]
    throughput_trips = total_trips / timings["full_pipeline_total"]

    print("-" * 70)
    print(f"TOTAL PIPELINE TIME:            {timings['full_pipeline_total']:.2f}s")
    print(f"THROUGHPUT:                     {throughput_pts:,.1f} points/sec ({throughput_trips:,.1f} trips/sec)")
    print("=" * 70)

    if benchmark_csv:
        record_benchmark(
            benchmark_file=benchmark_csv,
            timings=timings,
            total_rows=total_rows,
            total_trips=total_trips,
            dataset_name=csv_path.name,
        )

    return timings


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark ValhallaRoute on GPS dataset.")
    parser.add_argument("input_csv", nargs="?", default="hannover_table.csv", help="Input GPS CSV file.")
    parser.add_argument("--tile-extract", default="valhalla_tiles.tar", help="Valhalla tile extract .tar.")
    parser.add_argument("--osm-pbf", default="osm_data/Hannover.osm.pbf", help="OSM .osm.pbf file.")
    parser.add_argument("-o", "--output", dest="output_csv", help="Optional output CSV path.")
    parser.add_argument("--benchmark-csv", default="benchmark_versions.csv", help="CSV file to record version benchmark results.")

    args = parser.parse_args()
    try:
        run_benchmark(
            csv_file=args.input_csv,
            tile_extract=args.tile_extract,
            osm_pbf=args.osm_pbf,
            output_csv=args.output_csv,
            benchmark_csv=args.benchmark_csv,
        )
        return 0
    except Exception as exc:
        print(f"Benchmark error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
