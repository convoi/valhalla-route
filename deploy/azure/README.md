# Monthly Valhalla Continental Tile Generation on Azure

This directory contains automated tooling to run monthly continental Valhalla tile compilations (Europe, North America, South America, Oceania) using a single Docker image on Azure with **Zero Secret Management** (via Azure System-Assigned Managed Identity).

---

## Architecture: Why Ephemeral Spot VM with Docker?

When generating Valhalla tiles for entire continents (such as Europe from a 28 GB `.osm.pbf` file), the compilation process requires:
1. **High Memory:** 64–128 GB RAM for graph tile node hierarchies.
2. **Massive Disk I/O:** Millions of small `.gph` file read/writes during graph extraction.
3. **High-Speed Ephemeral Disk:** Valhalla tile extraction on network storage (e.g. Azure Files or SMB/NFS mounts) suffers heavy latency and throttling.

| Environment | Max vCPU / RAM | Temporary Scratch Disk | Spot Pricing? | Monthly Cost (8 hrs) | Viability for Continental Valhalla |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Azure Container Instances (ACI)** | 4 vCPU / 16 GB | 40 GB limit | No | ~$15 | ❌ OOM & disk full on Europe PBF |
| **Azure Container Apps (ACA)** | 8 vCPU / 32 GB | Network volume (slow) | Limited | ~$25 | ❌ Heavy I/O throttling on tile writes |
| **Ephemeral Spot VM + Docker** (`Standard_E32s_v5`) | **32 vCPU / 256 GB** | **Fast Local NVMe SSD** | **Yes (90% off)** | **~$3.60** | ** Optimal: finishes entire planet in ~8 hrs** |

By running your Docker container on an ephemeral Azure Spot VM, the container has direct access to 32 vCPUs, 256 GB RAM, and the local NVMe SSD. On job completion, the VM automatically executes `poweroff` to deallocate itself and stop all compute charges.

---

## Authentication: Zero Secrets with Managed Identity

The launcher script sets up a **System-Assigned Managed Identity** for the VM:
1. Azure grants the VM's identity the `Storage Blob Data Contributor` role on your Azure Storage Account.
2. If your Docker image resides in a private Azure Container Registry (ACR), the script also grants the `AcrPull` role to the VM's identity.
3. The Docker container runs with `--net host`. The Python application (`obstore`) accesses the Azure Instance Metadata Service (IMDS) at `169.254.169.254` to acquire OAuth tokens automatically.
4. **No storage keys, SAS tokens, or credentials are ever stored in code, config, or disks.**

---

## Quickstart: Launching the Build Job

### 1. Build and Push Your Docker Image
Build the container image using the included root `Dockerfile`:
```bash
docker build -t myacr.azurecr.io/valhalla-route:latest .
docker push myacr.azurecr.io/valhalla-route:latest
```

### 2. Launch with a Single Command

#### Using Bash / WSL / Cloud Shell:
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

---

## Date-Based Snapshot Versioning & Historical Recalculations

To support monthly updates while allowing historical trips to be recalculated on the exact road network of that time, **all generated tiles are placed into a date-stamped folder by default** (format: `YYYY-MM-DD`, e.g. `2026-11-16`):

```
az://mystorageaccount/valhalla-tiles/
├── latest.txt                 # Pointer containing the latest date string, e.g. "2026-11-16"
├── 2026-10-15/                # October snapshot (reproducible historical map data)
│   ├── 0/ ...                 # Level 0 tiles
│   ├── 1/ ...                 # Level 1 tiles
│   ├── 2/ ...                 # Level 2 tiles & .features.parquet
│   ├── admin.sqlite
│   ├── manifest.json
│   └── logs/build-*.log
└── 2026-11-16/                # November snapshot
    ├── 0/ ...
    ├── 1/ ...
    ├── 2/ ...
    ├── admin.sqlite
    ├── manifest.json
    └── logs/build-*.log
```

### Reproducing Historical Analyses
To enrich or re-calculate historical trips using past map data:
```python
from valhallaroute import enrich_gps_points

# Route against exact October 2026 road network
df = enrich_gps_points(
    "historical_trip.csv",
    tile_storage_uri="az://mystorageaccount/valhalla-tiles/2026-10-15/",
)
```

### Always Using the Latest Version
When you specify `.../latest/`, ValhallaRoute automatically reads `latest.txt` and resolves to the most recent snapshot without needing code changes:
```python
# Automatically resolves to the newest monthly snapshot
df = enrich_gps_points(
    "trip.csv",
    tile_storage_uri="az://mystorageaccount/valhalla-tiles/latest/",
)
```

---

## What Happens Automatically

1. **Spot VM Provisioned:** Azure launches an Ubuntu 24.04 Spot VM (`Standard_E32s_v5`) with Managed Identity.
2. **Scratch Disk Formatted:** The VM formats the fast local NVMe SSD at `/mnt/valhalla_work`.
3. **Container Runs:** The container runs `valhallaroute build-continents`:
   - Downloads `europe-latest.osm.pbf`, compiles Level 0/1/2 routing tiles and companion feature Parquets.
   - Compiles `admin.sqlite` from Geofabrik boundaries.
   - Uploads compiled tiles directly to `az://mystorageaccount/valhalla-tiles/<YYYY-MM-DD>/`.
   - Cleans up scratch disk after each continent, repeating for `north-america`, `south-america`, and `oceania`.
4. **Log & Pointer Uploaded:** Execution log is uploaded to `<YYYY-MM-DD>/logs/build-<timestamp>.log`, and `latest.txt` is updated in the container root.
5. **Self-Termination:** VM executes `poweroff` to deallocate and stop billing immediately.

---

## Monitoring Progress

Check VM instance state:
```bash
az vm get-instance-view -g rg-valhalla -n <vm-name> --query instanceView.statuses
```

Download execution log:
```bash
az storage blob download \
  --account-name mystorageaccount \
  -c valhalla-tiles \
  -n "<YYYY-MM-DD>/logs/build-<timestamp>.log" \
  --file build.log \
  --auth-mode login
```

Check current latest snapshot pointer:
```bash
az storage blob download \
  --account-name mystorageaccount \
  -c valhalla-tiles \
  -n "latest.txt" \
  --file latest.txt \
  --auth-mode login
cat latest.txt
```
