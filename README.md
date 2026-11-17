# ValhallaRoute

A Python tool that enriches ordered GPS trip points from CSV using Valhalla map matching and offline reverse geocoding.

The resulting pandas DataFrame includes all original columns plus the enriched attributes:
* **`valhalla_type`** (`str`): Match status (`matched`, `interpolated`, or `unmatched`).
* **`road_name`** (`str`): Name of the road (e.g. `Alt-Vinnhorst`, `Bothfelder Straße`, `A 2`).
* **`road_type`** (`str`): Road classification (`motorway`, `trunk`, `primary`, `secondary`, `tertiary`, `residential`, `service_other`, etc.).
* **`surface`** (`str` | `None`): Road surface composition (`paved_smooth`, `paved`, `paved_rough`, `compacted`, `dirt`, `gravel`, `path`, etc.).
* **`road_density`** (`int` | `None`): Relative street network density around the road segment (0–15).
* **`speed_limit`** (`float` | `None`): Posted legal speed limit in km/h.
* **`free_flow_speed`** (`float` | `None`): Routing baseline speed in km/h modeled for the road segment.
* **`oneway`** (`bool`): Whether the road is a one-way street.
* **`sidewalk`** (`str` | `None`): Pedestrian walkway infrastructure (`none`, `left`, `right`, `both`).
* **`cycle_lane`** (`str` | `None`): Bicycle infrastructure (`none`, `shared`, `dedicated`, `separated`).
* **`distance_to_next_intersection`** (`float` | `None`): Distance in meters along the road segment to the upcoming intersection node.
* **`intersection_edge_count`** (`int` | `None`): Number of road legs meeting at the upcoming intersection (e.g. 3 for T-junction, 4 for crossroad).
* **`has_stop_sign`** (`bool`): Whether the point is approaching or stopped at a stop sign.
* **`has_yield_sign`** (`bool`): Whether the point is approaching or stopped at a give-way / yield sign.
* **`has_traffic_signal`** (`bool`): Proximity to traffic lights.
* **`is_crossing`** (`bool`): Proximity to a pedestrian, bicycle, or railroad crossing.
* **`crossing_type`** (`str` | `None`): Classification of crossing (e.g. `traffic_signals`, `zebra`, `unmarked`).
* **`curvature`** (`int` | `None`): Valhalla road curvature index (0–15).
* **`lane_count`** (`int` | `None`): Number of traffic lanes on the matched road segment.
* **`is_turn`** (`bool`): Whether a turning maneuver occurs.
* **`turn_direction`** (`str`): Turn direction (`straight`, `slight_right`, `right`, `sharp_right`, `u_turn`, etc.).
* **`turn_angle`** (`float`): Absolute turn angle between consecutive segments in degrees (0–180°).
* **`is_roundabout`** (`bool`): Whether the road segment is part of a traffic circle / roundabout.
* **`roundabout_action`** (`str` | `None`): Action at roundabout (`entering`, `exiting`, `inside`).
* **`is_tunnel`** (`bool`): Whether the point is located inside a tunnel.
* **`is_bridge`** (`bool`): Whether the point is located on a bridge.
* **`city`** (`str`): Resolved city / municipality name.
* **`county`** (`str`): Resolved county / administrative district name.
* **`country`** (`str`): Country name (e.g., from Valhalla administrative bounds or ISO code).

### Enriched Result Excerpt

Below is an excerpt of real rows from our benchmark trip dataset (`hannover_table.csv`) showing how Valhalla map-matching and companion feature tiles resolve complex urban driving contexts:

| road_name | road_type | dist_to_intersection | intersection_edges | stop_sign | yield_sign | traffic_signal | is_crossing (type) | curvature | turn_direction | city |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :--- | :---: | :--- | :--- |
| **Alt-Vinnhorst** | tertiary | `0.0 m` | 3-way | **True** | False | False | False | 3 | straight | Hannover |
| **Godshorner Str.** | secondary | `5.7 m` | 3-way | False | **True** | False | **True** (`unmarked`) | 0 | straight | Langenhagen |
| **Bothfelder Str.** | secondary | `9.0 m` | 4-way | False | False | **True** | **True** (`traffic_signals`) | 0 | straight | Langenhagen |
| **Engelbosteler Damm** | residential | `5.0 m` | 4-way | False | **True** | **True** | **True** (`traffic_signals`) | 15 (sharp) | slight_right | Hannover |
| **Weidetorkreisel** | secondary | `70.3 m` | 4-way | False | False | False | False | 15 (circle) | entering *(roundabout)* | Hannover |

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

### 4. Fast Offline Reverse Geocoding (`AdminGeocoder`)

ValhallaRoute includes a high-throughput, 100% offline reverse geocoder backed by `admin.sqlite` (or `admin.parquet` compiled from Overture Maps / OSM administrative boundaries):
* **Thread-Safe & Process-Safe:** Read-only SQLite with R-tree spatial index (`idx_admins_geom`) or GeoParquet via DuckDB.
* **Vectorized STRtree Spatial Indexing:** Uses Shapely `STRtree` spatial indexing over candidate boundaries processed in bounded coordinate batches (8,192 points), with vectorized C-accelerated `contains_xy` evaluation.
* **Exact Coordinates by Default:** Defaults to exact coordinates (`precision_decimals=None`), deduplicating only identical points without boundary drift (pass `precision_decimals=3` for legacy ~100m rounded deduplication).
* **Hierarchical Resolution:** Resolves country, ISO-3166 code, state/province (`admin1`), county (`admin2`), and municipality/city.

```python
from valhallaroute import AdminGeocoder, enrich_gps_points, lookup_coordinates

# Direct geocoding lookup (exact coordinates by default)
results = lookup_coordinates([(52.3759, 9.7320)], admin_db_path="admin.sqlite")
print(results[0])
# {'city': 'Hannover', 'county': 'Region Hannover', 'admin1': 'Niedersachsen', 'admin2': 'Region Hannover', 'country': 'Deutschland', 'country_code': 'DE'}

# Used automatically during GPS point enrichment
df = enrich_gps_points("trip.csv", admin_db_path="admin.sqlite")
```

---

## Command Line Interface (CLI)

```bash
# Enrich using local tiles (auto-downloads OSM and builds tiles if missing)
valhallaroute input_trip.csv -o enriched_trip.csv

# Custom column mapping for arbitrary CSV files
valhallaroute input_trip.csv -o enriched_trip.csv \
  --trip trip_id \
  --time recorded_at \
  --lat latitude \
  --lon longitude

# Preserve original GPS coordinates and store matched coordinates in valhalla_lat / valhalla_lon
valhallaroute input_trip.csv -o enriched_trip.csv --preserve-coords

# Specify custom administrative database path for reverse geocoding
valhallaroute input_trip.csv -o enriched_trip.csv --admin-db /path/to/admin.parquet

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

## Building Administrative Boundaries (`admin.sqlite`)

Valhalla routing uses an SQLite/SpatiaLite database (`admin.sqlite`) during tile compilation (`valhalla_build_tiles`) to determine country and state boundaries, driving side (left vs. right), road access defaults, and border crossing detection.

ValhallaRoute includes a high-performance admin compiler that streams directly from **Overture Maps GeoParquet** (`theme=divisions`) via DuckDB and generates a 100% compliant SpatiaLite database without requiring external SpatiaLite C shared libraries.

### Usage

```bash
# Compile administrative database for specific countries (e.g. Germany, Austria, Switzerland)
uv run valhallaroute build-admin -o admin.sqlite -c DE AT CH

# Compile Netherlands with country (2) and regional/provincial (4) boundaries
uv run valhallaroute build-admin -o admin.sqlite -c NL --admin-levels 2,4

# Compile the entire planet
uv run valhallaroute build-admin -o planet_admin.sqlite --admin-levels 2,4

# Include county (6) and municipality/locality (8) boundaries
uv run valhallaroute build-admin -o admin_detailed.sqlite -c NL --admin-levels 2,4,6,8
```

### Options

| Flag | Default | Description |
|---|---|---|
| `-o`, `--output` | `admin.sqlite` | Destination SQLite database file path. |
| `-c`, `--countries` | *all* (planet) | Space-separated ISO-3166-1 alpha-2 country codes. |
| `--admin-levels` | `2,4` | Administrative levels: `2` (country), `4` (state/province), `6` (county), `8` (city). |
| `--release` | `2026-10-21.0` | Overture Maps release version. |
| `--s3-region` | `us-west-2` | AWS region for Overture public S3 bucket. |
| `--no-maritime` | *false* | Exclude maritime territorial bounds and retain only land boundaries. |

---

## Continental Tile Pack Builder (`build-continents`)

For global or multi-continental routing without Level 0/1 tile collisions, ValhallaRoute includes a single-node continental builder. Because continents (Europe, Americas, Oceania) do not share roads, each continent's graph tiles (`.gph`) and companion feature parquets (`.features.parquet`) can be compiled cleanly in sequence on high-speed local NVMe storage and uploaded directly into a shared cloud object storage prefix (Azure Blob Storage, S3, or GCS).

```bash
# Build Europe, North America, South America, and Oceania into Azure Blob Storage (dated snapshot)
valhallaroute build-continents \
  --continents europe,north-america,south-america,oceania \
  --destination-storage az://mystorageaccount/valhalla-tiles/$(date +%Y-%m-%d)/ \
  --work-dir /mnt/nvme/valhalla \
  --build-admins
```

### Automated Monthly Build via Docker & Azure Managed Identity

You can launch an ephemeral **Azure Spot VM** (`Standard_E32s_v5`, 32 vCPUs, 256 GB RAM, ~$0.45/hr) running your Docker container with a single command. The container authenticates **passwordlessly** using an Azure System-Assigned Managed Identity (no storage account keys or secrets), compiles the continents on fast local NVMe storage, streams the tiles into Azure Blob Storage under a date-stamped folder (`<YYYY-MM-DD>/` by default), updates a `latest.txt` pointer, and **automatically powers off to stop billing immediately**:

#### Using Bash / WSL:
```bash
./deploy/azure/launch_docker_spot_builder.sh \
  --image myacr.azurecr.io/valhalla-route:latest \
  --storage-account mystorageaccount \
  --container valhalla-tiles \
  --resource-group rg-valhalla \
  --acr-name myacr
```

#### Using PowerShell (Windows / pwsh):
```powershell
.\deploy\azure\launch_docker_spot_builder.ps1 `
  -Image "myacr.azurecr.io/valhalla-route:latest" `
  -StorageAccount "mystorageaccount" `
  -Container "valhalla-tiles" `
  -ResourceGroup "rg-valhalla" `
  -AcrName "myacr"
```

> [!TIP]
> **Historical Reproducibility & Versioning:** Every run preserves its own date-stamped folder (e.g. `az://mystorageaccount/valhalla-tiles/2026-11-16/`). When re-calculating historical trips from previous months, you can point directly to past snapshot dates. When passing `.../latest/`, ValhallaRoute automatically resolves the `latest.txt` pointer to use the newest month's map data. See [deploy/azure/README.md](file:///C:/Users/jhees/PycharmProjects/ValhallaRoute/deploy/azure/README.md) for full details.

---

## Running Tests

```bash
uv run pytest
```

