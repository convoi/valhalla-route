# ValhallaRoute

A Python tool that enriches ordered GPS trip points from CSV using Valhalla map matching and offline reverse geocoding.

The resulting pandas DataFrame includes all original columns plus the enriched attributes:
* **`valhalla_type`** (`str`): Match status (`matched`, `interpolated`, or `unmatched`).
* **`road_name`** (`str`): Name of the road (e.g. `Alt-Vinnhorst`, `Bothfelder Straße`, `A 2`).
* **`road_type`** (`str`): Road classification (`motorway`, `trunk`, `primary`, `secondary`, `tertiary`, `residential`, `service_other`, etc.).
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

## Continental & Regional Batch Tile Generation (Kubernetes & Azure Deployment)

ValhallaRoute provides a cloud-scale, two-stage distributed tile compiler capable of building entire countries, continents, or global tile networks into object storage (Azure Blob Storage, AWS S3, Google Cloud Storage, or local storage).

```
┌───────────────────────────────────────────────────────────────────┐
│                    Stage 1: Batch Planner Job                     │
│  valhallaroute plan-batch --regions "germany/*"                   │
│  - Queries Geofabrik index, expands wildcards into subregions     │
│  - Writes task manifest JSONL to Azure Blob Storage               │
└─────────────────────────────────┬─────────────────────────────────┘
                                  │
                                  ▼
┌───────────────────────────────────────────────────────────────────┐
│              Stage 2: Kubernetes Indexed Worker Job               │
│  valhallaroute run-worker -m azure://<container>/manifest.jsonl   │
│  - completionMode: Indexed, completions: N, parallelism: P        │
│  - Worker pod i pulls task i mapped from $JOB_COMPLETION_INDEX    │
│  - Compiles Valhalla routing graph tiles across CPU cores         │
│  - Concurrently uploads .gph tiles to azure://<container>/global/ │
└───────────────────────────────────────────────────────────────────┘
```

---

### Azure Workload Identity Authentication (AKS)

ValhallaRoute uses [`obstore`](https://github.com/developmentseed/obstore) (powered by Rust's `object_store`) for cloud storage operations. On **Azure Kubernetes Service (AKS)**, it authenticates passwordlessly via **Azure Workload Identity**:

#### How It Works:
1. **OIDC Token Projection:** AKS projects an OIDC service account token signed by the cluster's OIDC issuer into the pod at `/var/run/secrets/azure/tokens/azure-identity-token`.
2. **Mutating Webhook:** The Azure Workload Identity webhook reads the pod's ServiceAccount annotations (`azure.workload.identity/client-id`) and automatically injects:
   * `AZURE_CLIENT_ID`: Client ID of the User-Assigned Managed Identity.
   * `AZURE_TENANT_ID`: Azure Entra ID Tenant ID.
   * `AZURE_FEDERATED_TOKEN_FILE`: `/var/run/secrets/azure/tokens/azure-identity-token`.
   * `AZURE_AUTHORITY_HOST`: `https://login.microsoftonline.com/`.
3. **Passwordless Entra ID Exchange:** When `obstore` accesses `azure://<container>/...`, it exchanges the projected OIDC token for an Azure OAuth token with Microsoft Entra ID.
4. **RBAC Authorization:** The Managed Identity accesses the Storage Account using Azure RBAC (`Storage Blob Data Contributor`) with **zero secrets** stored in Kubernetes secrets or config files.

#### 1. Enable Workload Identity on your AKS cluster:
```bash
az aks update \
  --resource-group <RESOURCE_GROUP> \
  --name <CLUSTER_NAME> \
  --enable-workload-identity \
  --enable-oidc-issuer
```

#### 2. Create Managed Identity and Grant Storage Permissions:
```bash
# Create User-Assigned Managed Identity
az identity create \
  --name valhallaroute-identity \
  --resource-group <RESOURCE_GROUP> \
  --location westeurope

export USER_ASSIGNED_CLIENT_ID="$(az identity show -g <RESOURCE_GROUP> -n valhallaroute-identity --query clientId -o tsv)"
export STORAGE_ID="$(az storage account show -n <STORAGE_ACCOUNT> -g <RESOURCE_GROUP> --query id -o tsv)"

# Grant Storage Blob Data Contributor role
az role assignment create \
  --role "Storage Blob Data Contributor" \
  --assignee "$USER_ASSIGNED_CLIENT_ID" \
  --scope "$STORAGE_ID"
```

#### 3. Create Federated Identity Credential:
```bash
export AKS_OIDC_ISSUER="$(az aks show -g <RESOURCE_GROUP> -n <CLUSTER_NAME> --query "oidcIssuerProfile.issuerUrl" -o tsv)"

az identity federated-credential create \
  --name valhallaroute-federated-cred \
  --identity-name valhallaroute-identity \
  --resource-group <RESOURCE_GROUP> \
  --issuer "$AKS_OIDC_ISSUER" \
  --subject "system:serviceaccount:default:valhallaroute" \
  --audience "api://AzureADTokenExchange"
```

---

### Pipeline Orchestration (Airflow / Dagster / Kubernetes)

Because batch tile generation uses dynamic task balancing (auto-subdivision of mega-regions and bin-packing of micro-regions), the exact number of worker tasks is determined at runtime by the planner.

The recommended orchestration pattern is a two-stage fan-out DAG managed by Airflow, Dagster, Argo Workflows, or a Python script:

1. **Stage 1 (Admin & Planning):**
   Run `valhallaroute build-admin` (optional, e.g. monthly) and `valhallaroute plan-batch` to output a balanced `manifest.jsonl` to cloud storage.
2. **Stage 2 (Dynamic Fan-out / Workers):**
   The orchestrator reads the manifest line count $N$ and dynamically dispatches $N$ worker tasks (e.g., via Airflow Dynamic Task Mapping, Dagster Dynamic Partitions, or submitting a Kubernetes Indexed Job with `completions: N, parallelism: P`).

#### Example: Dynamic Kubernetes Indexed Job Submission
```python
from kubernetes import client, config
import smart_open

# 1. Read task count directly from the generated manifest
with smart_open.open("s3://valhalla-tiles/manifests/europe.jsonl", "r") as f:
    task_count = sum(1 for line in f if line.strip())

# 2. Programmatically launch Kubernetes Indexed Job with exact completions
job = {
    "apiVersion": "batch/v1",
    "kind": "Job",
    "metadata": {"name": "valhalla-workers-batch"},
    "spec": {
        "completionMode": "Indexed",
        "completions": task_count,  # Exact number of balanced tasks!
        "parallelism": 16,          # Max concurrent worker pods
        "template": {
            "spec": {
                "restartPolicy": "Never",
                "containers": [{
                    "name": "worker",
                    "image": "ghcr.io/valhallaroute:latest",
                    "command": [
                        "valhallaroute", "run-worker",
                        "-m", "s3://valhalla-tiles/manifests/europe.jsonl",
                        "--admin-db-uri", "s3://valhalla-tiles/admin.sqlite"
                    ],
                    "resources": {
                        "requests": {"cpu": "8", "memory": "16Gi"},
                        "limits": {"cpu": "8", "memory": "16Gi"}
                    }
                }]
            }
        }
    }
}
config.load_kube_config()
client.BatchV1Api().create_namespaced_job(namespace="default", body=job)
```
---

### Worker Hardware & Pod Resource Sizing Guide

Based on real OS kernel performance benchmarks measured for the **Bavaria (`bayern`)** region (818.3 MB PBF, 70,500 km²):

| Metric | Measured Hard Number | Kubernetes Recommendation (Guaranteed QoS) |
| :--- | :--- | :--- |
| **Worker Compilation Time** | `208.48 s` (~3.47 min) | Fast turnaround across 16 cores |
| **Total CPU Consumption** | `857.20 s` (~14.3 CPU min) | Set requests and limits equal: `cpu: 8` |
| **Peak Working Set RAM** | `7,378 MB` (~`7.21 GB`) | Set requests and limits equal: `memory: 16Gi` |
| **Output Volume** | `212` tiles (`990.67 MB`) | Ephemeral local disk >= `10Gi` |

> [!TIP]
> **Guaranteed Quality of Service (QoS):** Setting resource `requests` equal to `limits` places the worker pods into Kubernetes' `Guaranteed` QoS class. This guarantees dedicated CPU allocation without noisy-neighbor throttling and prevents Linux kernel OOM kills under node memory pressure.
>
> **Sub-Extract Administrative DB:** For sub-extracts (states/provinces), country boundary polygons are clipped by extract borders. ValhallaRoute automatically disables the admin DB for regional builds (`build_admins: false`), ensuring fast, crash-free execution across all worker nodes.

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
# Build Europe, North America, South America, and Oceania into Azure Blob Storage
valhallaroute build-continents \
  --continents europe,north-america,south-america,oceania \
  --destination-storage az://valhalla-tiles/global/ \
  --work-dir /mnt/nvme/valhalla \
  --build-admins
```

### Launching on Azure via Azure CLI (`az cli`)

You can launch an ephemeral **Azure Spot VM** (`Standard_E32s_v5`, ~$0.45/hr) on a monthly cadence using `az cli`. The VM installs ValhallaRoute, downloads extracts, compiles tiles and companion feature parquets, streams them to Azure Blob Storage, and **automatically shuts down / deallocates upon completion**:

```bash
# Set your target Azure storage account & resource group
export AZURE_RESOURCE_GROUP="rg-valhalla"
export AZURE_STORAGE_ACCOUNT="mystorageaccount"
export AZURE_STORAGE_CONTAINER="valhalla-tiles"

# Launch using the provided deployment script:
./deploy/azure/launch_spot_builder.sh
```

Or run the one-line `az cli` command directly:

```bash
az vm create \
  --resource-group rg-valhalla \
  --name valhalla-builder-$(date +%Y%m%d) \
  --image Ubuntu2404 \
  --size Standard_E32s_v5 \
  --priority Spot \
  --eviction-policy Deallocate \
  --max-price -1 \
  --assign-identity [system] \
  --role "Storage Blob Data Contributor" \
  --scope $(az storage account show -n mystorageaccount -g rg-valhalla --query id -o tsv) \
  --custom-data @deploy/azure/cloud-init.yaml
```

---

## Running Tests

```bash
uv run pytest
```

