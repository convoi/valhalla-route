# ValhallaRoute

A Python tool that enriches ordered GPS trip points from CSV using Valhalla map matching and offline reverse geocoding.

The resulting pandas DataFrame includes all original columns plus the enriched attributes:
* **`is_tunnel`** (`bool`): Whether the point is located inside a tunnel.
* **`is_bridge`** (`bool`): Whether the point is located on a bridge.
* **`is_crossing`** (`bool`): Whether the point represents a country border crossing or pedestrian crossing.
* **`is_roundabout`** (`bool`): Whether the road segment is part of a traffic circle / roundabout.
* **`is_on_ramp`** (`bool`): Whether the road segment is a highway entrance on-ramp (link).
* **`is_exit_ramp`** (`bool`): Whether the road segment is a highway exit ramp (link).
* **`lanes`** (`int` | `None`): Number of traffic lanes on the matched road segment.
* **`turn_degree`** (`float`): Absolute turn angle between consecutive segments in degrees (0–180°).
* **`turn_direction`** (`str`): Categorized turn direction (`straight`, `slight_right`, `right`, `sharp_right`, `u_turn`, `slight_left`, `left`, `sharp_left`).
* **`valhalla_type`** (`str`): Match status (`matched`, `interpolated`, or `unmatched`).
* **`city`** (`str`): Resolved city / municipality name.
* **`country`** (`str`): Country name (e.g., from Valhalla administrative bounds or ISO code).
* **`road_name`** (`str`): Name of the road (e.g. `Karl-Liebknecht-Straße`, `A 2`).
* **`road_type`** (`str`): Road classification (`motorway`, `trunk`, `primary`, `secondary`, `tertiary`, `residential`, `service_other`, etc.).

---

## Installation

Using `uv`:
```bash
uv sync
```

Or using standard `pip`:
```bash
pip install -e .
```

---

## Automatic Tile & OpenStreetMap Data Generation

If local routing tiles (`valhalla_tiles.tar` or `valhalla_tiles/`) are missing, **ValhallaRoute can automatically download the required OpenStreetMap source file (`.osm.pbf`) and generate the tiles**.

### 1. Auto-detection from GPS coordinates
By default (`auto_build_tiles=True`), if no tiles are found, ValhallaRoute computes the bounding box of your trip coordinates, queries the Geofabrik index, downloads the smallest enclosing regional `.osm.pbf` extract, and builds the tiles:

```python
from valhallaroute import enrich_gps_points

# Automatically downloads OSM data & builds tiles if missing!
df = enrich_gps_points("trip.csv")
```

### 2. Specifying a Region or Custom Download URL
You can also specify a region name (e.g. `monaco`, `berlin`, `liechtenstein`) or direct `.osm.pbf` URL:

```python
from valhallaroute import enrich_gps_points

# Auto-downloads 'monaco' extract if tiles are missing
df = enrich_gps_points("trip.csv", region="monaco")

# Or specify a direct URL
df = enrich_gps_points(
    "trip.csv",
    osm_url="https://download.geofabrik.de/europe/monaco-latest.osm.pbf",
)
```

### 3. Using Standalone Tile Management Functions
You can also download OSM source files and build tiles explicitly:

```python
from valhallaroute import download_osm_pbf, build_valhalla_tiles, ensure_tiles_exist

# 1. Ensure tiles exist (downloads OSM data and builds tiles if missing)
tar_path = ensure_tiles_exist(
    tile_extract="valhalla_tiles.tar",
    region="monaco",
)

# 2. Or download an OSM PBF file explicitly
pbf_path = download_osm_pbf(region="berlin", output_dir="osm_data")

# 3. Build Valhalla tiles from any .osm.pbf file
build_valhalla_tiles(
    osm_pbf_path=pbf_path,
    tile_dir="valhalla_tiles",
    tile_extract="valhalla_tiles.tar",
)
```

---

## Python Usage

### 1. Using Local Valhalla Tiles (`pyvalhalla` Actor)

You can pass a local Valhalla tile extract (`.tar`) or tile directory directly:

```python
from valhallaroute import enrich_gps_points

df = enrich_gps_points(
    "trip.csv",
    tile_extract="valhalla_tiles.tar",
)

print(df[["latitude", "longitude", "is_tunnel", "is_bridge", "is_crossing", "city", "country", "road_name", "road_type"]])
```

You can also pass an already initialized `valhalla.Actor` instance:

```python
from valhalla import Actor, get_config
from valhallaroute import enrich_gps_points

actor = Actor(get_config(tile_extract="valhalla_tiles.tar", tile_dir=""))
df = enrich_gps_points("trip.csv", actor=actor)
```

### 2. Using Valhalla HTTP Endpoint (Local Docker or Remote Server)

```python
from valhallaroute import enrich_gps_points

df = enrich_gps_points(
    "trip.csv",
    valhalla_url="http://localhost:8002",
)
```

### 3. Flexible CSV Inputs

`enrich_gps_points` accepts:
* A file path (`str` or `pathlib.Path`)
* A raw CSV string
* A file-like object (`io.StringIO`, `open(...)`)
* An existing `pandas.DataFrame`

It automatically detects coordinate column variants (`lat`, `latitude`, `lat_deg`, `y`, `lon`, `lng`, `longitude`, `x`), or you can pass `lat_col="custom_lat"` and `lon_col="custom_lon"`.

```python
import io
from valhallaroute import enrich_gps_points

csv_content = """lat,lon,timestamp
52.5200,13.4050,2026-11-15T10:00:00Z
52.5205,13.4055,2026-11-15T10:00:30Z
52.5210,13.4060,2026-11-15T10:01:00Z
"""

df = enrich_gps_points(io.StringIO(csv_content), tile_extract="valhalla_tiles.tar")
```

---

## Command Line Interface (CLI)

```bash
# Enrich using local tiles (auto-downloads OSM and builds tiles if missing)
valhallaroute input_trip.csv -o enriched_trip.csv

# Specify region to auto-download if tiles are missing
valhallaroute input_trip.csv -o enriched_trip.csv --region monaco

# On-demand sparse tile resolution from cloud blob storage (S3 / GCS / Azure)
valhallaroute input_trip.csv -o enriched_trip.parquet \
  --tile-storage-uri s3://my-valhalla-tiles/global/ \
  --tile-cache-dir valhalla_tiles_cache

# Using custom OSM .osm.pbf file
valhallaroute input_trip.csv -o enriched_trip.csv --osm-pbf osm_data/berlin.osm.pbf

# Using Valhalla HTTP URL
valhallaroute input_trip.csv -o enriched_trip.csv --url http://localhost:8002
```

---

## Cloud-Scale Batch Processing (Kubernetes Indexed Jobs)

ValhallaRoute supports two-stage distributed batch processing across S3, Google Cloud Storage, Azure Blob Storage, and local disks using `obstore`.

### Stage 1: Generate Task Manifest
Partition a directory or dataset into tasks and write an indexed `manifest.jsonl`:
```bash
valhallaroute plan-batch \
  -i s3://telematics/raw_trips/ \
  -m s3://telematics/manifests/job_tasks.jsonl \
  -o s3://telematics/enriched_trips/ \
  --tile-storage-uri s3://valhalla-tiles/global/ \
  --batch-size 10 \
  --format parquet
```

### Stage 2: Run Worker
Worker pods in a Kubernetes Indexed Job (`completionMode: Indexed`) execute their assigned slice mapped from `$JOB_COMPLETION_INDEX`:
```bash
valhallaroute run-worker \
  -m s3://telematics/manifests/job_tasks.jsonl \
  --tile-cache-dir /mnt/tile-cache
```

Workers determine the minimal set of `.gph` tiles needed for the trip coordinates and fetch only missing tiles (~15–35 files / ~5–20 MB for a 100 km corridor) instead of downloading multi-gigabyte country archives.

---

## Benchmark Dataset: Hannover Trajectories

The benchmark suite (`benchmarks/benchmark_hannover.py`) uses the real-world dataset **GPS Trajectory Dataset of the Region of Hannover, Germany** provided by the *Institut für Kartographie und Geoinformatik (IKG)* at *Leibniz Universität Hannover*.

* **Dataset Landing Page:** [https://data.uni-hannover.de/dataset/single-user-trajectory-collection-for-the-region-of-hannover](https://data.uni-hannover.de/dataset/single-user-trajectory-collection-for-the-region-of-hannover)
* **License:** Creative Commons Attribution-NonCommercial 3.0 (CC BY-NC 3.0)
* **Direct Download (`GPS_Points.zip` ~22.6 MB):**
  ```
  https://data.uni-hannover.de/dataset/379ed322-a9ea-48f3-bc13-2f5ea3174470/resource/45c47bcc-97df-49a3-870a-eefa11fb432b/download/gps_points.zip
  ```

### How to Download & Prepare `hannover_table.csv`

1. **Download and Extract `GPS_Points.zip`:**
   - **Linux / macOS:**
     ```bash
     curl -L -o gps_points.zip "https://data.uni-hannover.de/dataset/379ed322-a9ea-48f3-bc13-2f5ea3174470/resource/45c47bcc-97df-49a3-870a-eefa11fb432b/download/gps_points.zip"
     unzip gps_points.zip
     ```
   - **Windows (PowerShell):**
     ```powershell
     Invoke-WebRequest -Uri "https://data.uni-hannover.de/dataset/379ed322-a9ea-48f3-bc13-2f5ea3174470/resource/45c47bcc-97df-49a3-870a-eefa11fb432b/download/gps_points.zip" -OutFile "gps_points.zip"
     Expand-Archive -Path "gps_points.zip" -DestinationPath "."
     ```

2. **Convert the Shapefile to CSV (`hannover_table.csv`):**
   The extracted archive contains `GPS_Points.shp` and `GPS_Points.dbf` (projected in UTM Zone 32N / EPSG:32632). Convert it to CSV using Python:
   ```python
   import geopandas as gpd

   gdf = gpd.read_file("GPS_Points.shp")
   gdf.to_csv("hannover_table.csv", index=False)
   ```
   Or using GDAL / `ogr2ogr`:
   ```bash
   ogr2ogr -f CSV hannover_table.csv GPS_Points.shp -lco GEOMETRY=AS_XY
   ```

3. **Run the Benchmark:**
   Once `hannover_table.csv` is in the repository root, run:
   ```bash
   python benchmarks/benchmark_hannover.py
   ```
   Performance metrics and throughput numbers across versions are recorded in `benchmark_versions.csv`.

---

## Running Tests

```bash
uv run pytest
```
