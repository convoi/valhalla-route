# ValhallaRoute Helm Chart

Helm chart for deploying the two-stage distributed Valhalla tile generation pipeline on Kubernetes (AKS, EKS, GKE, or bare-metal).

By default, this chart is configured for **Azure Kubernetes Service (AKS)** using **Azure Workload Identity** for zero-secret, passwordless access to Azure Blob Storage.

---

## Architecture Overview

The distributed tile generation pipeline runs in two stages:

```
+-------------------------------------------------------------------+
|                        Stage 1: Batch Planner Job                |
|  valhallaroute plan-batch --regions "germany/*"                   |
|  - Queries Geofabrik index, expands wildcards                     |
|  - Writes task manifest JSONL to Azure Blob Storage               |
+---------------------------------+---------------------------------+
                                  |
                                  v
+---------------------------------+---------------------------------+
|                        Stage 2: Indexed Worker Job               |
|  valhallaroute run-worker -m azure://<container>/manifest.jsonl   |
|  - Kubernetes Indexed Job (completionMode: Indexed)               |
|  - Worker pod i executes task i ($JOB_COMPLETION_INDEX)           |
|  - Compiles Valhalla tiles with multi-threaded C++ engine         |
|  - Concurrently uploads .gph tiles to azure://<container>/global/ |
+-------------------------------------------------------------------+
```

---

## Prerequisites

1. **Kubernetes 1.24+** (with support for `completionMode: Indexed`).
2. **Azure Kubernetes Service (AKS)** with Azure Workload Identity enabled:
   ```bash
   az aks update -g <RESOURCE_GROUP> -n <CLUSTER_NAME> --enable-workload-identity --enable-oidc-issuer
   ```
3. **Azure Blob Storage Account** & container created:
   ```bash
   az storage account create -n <STORAGE_ACCOUNT> -g <RESOURCE_GROUP> -l westeurope --sku Standard_LRS
   az storage container create --account-name <STORAGE_ACCOUNT> -n valhalla-tiles --auth-mode login
   ```

---

## Azure Workload Identity Setup

Azure Workload Identity allows Kubernetes ServiceAccounts to access Azure resources securely without storing API keys or connection strings in cluster secrets.

### 1. Create a User-Assigned Managed Identity
```bash
az identity create \
  --name valhallaroute-identity \
  --resource-group <RESOURCE_GROUP> \
  --location westeurope

# Retrieve Client ID and OIDC Issuer URL
export USER_ASSIGNED_CLIENT_ID="$(az identity show --resource-group <RESOURCE_GROUP> --name valhallaroute-identity --query clientId -o tsv)"
export AKS_OIDC_ISSUER="$(az aks show --resource-group <RESOURCE_GROUP> --name <CLUSTER_NAME> --query "oidcIssuerProfile.issuerUrl" -o tsv)"
```

### 2. Grant Blob Storage Permissions (RBAC)
Grant `Storage Blob Data Contributor` on the storage account:
```bash
export STORAGE_ID="$(az storage account show -n <STORAGE_ACCOUNT> -g <RESOURCE_GROUP> --query id -o tsv)"

az role assignment create \
  --role "Storage Blob Data Contributor" \
  --assignee "$USER_ASSIGNED_CLIENT_ID" \
  --scope "$STORAGE_ID"
```

### 3. Establish Federated Identity Credential
Link the Kubernetes ServiceAccount to the Azure Managed Identity:
```bash
az identity federated-credential create \
  --name valhallaroute-federated-cred \
  --identity-name valhallaroute-identity \
  --resource-group <RESOURCE_GROUP> \
  --issuer "$AKS_OIDC_ISSUER" \
  --subject "system:serviceaccount:default:valhallaroute" \
  --audience "api://AzureADTokenExchange"
```

---

## Installation via Helm

### Quick Install with Azure Workload Identity:

```bash
helm install valhallaroute ./charts/valhallaroute \
  --set azure.storageAccountName="<STORAGE_ACCOUNT>" \
  --set azure.container="valhalla-tiles" \
  --set azure.workloadIdentity.clientId="$USER_ASSIGNED_CLIENT_ID" \
  --set serviceAccount.name="valhallaroute"
```

### Custom Regions Install:

To compile a custom list of regions or continents:

```bash
helm install valhallaroute ./charts/valhallaroute \
  --set azure.storageAccountName="<STORAGE_ACCOUNT>" \
  --set azure.workloadIdentity.clientId="$USER_ASSIGNED_CLIENT_ID" \
  --set planner.regions="{bayern,berlin,hamburg}" \
  --set worker.completions=3 \
  --set worker.parallelism=3
```

---

## Configuration Reference

| Parameter | Description | Default |
| :--- | :--- | :--- |
| `image.repository` | Container image repository | `ghcr.io/valhallaroute` |
| `image.tag` | Container image tag | `v0.4.0` |
| `azure.storageAccountName` | Azure Storage Account name | `"valhallastorage"` |
| `azure.container` | Target Azure Blob Storage container | `"valhalla-tiles"` |
| `azure.workloadIdentity.enabled` | Enable Azure Workload Identity labels and token injection | `true` |
| `azure.workloadIdentity.clientId` | Client ID of User-Assigned Managed Identity | `""` |
| `azure.workloadIdentity.tenantId` | Azure Entra Tenant ID (optional) | `""` |
| `serviceAccount.create` | Create ServiceAccount with Workload Identity annotations | `true` |
| `serviceAccount.name` | ServiceAccount name | `""` (defaults to release fullname) |
| `planner.enabled` | Enable Stage 1 batch planning job | `true` |
| `planner.regions` | List of regions or wildcards (`germany/*`, `europe/monaco`) | `["germany/*"]` |
| `planner.manifestUri` | Manifest JSONL target URI | `azure://valhalla-tiles/manifests/tiles_tasks.jsonl` |
| `planner.outputUri` | Destination cloud prefix for tiles | `azure://valhalla-tiles/global/` |
| `planner.resources.requests.cpu` | Planner CPU request | `1` |
| `planner.resources.requests.memory` | Planner memory request | `2Gi` |
| `planner.resources.limits.cpu` | Planner CPU limit | `1` |
| `planner.resources.limits.memory` | Planner memory limit | `2Gi` |
| `worker.enabled` | Enable Stage 2 indexed worker job | `true` |
| `worker.completions` | Total tasks in manifest to execute | `16` |
| `worker.parallelism` | Max concurrent worker pods | `4` |
| `worker.manifestUri` | URI of manifest JSONL file to read | `azure://valhalla-tiles/manifests/tiles_tasks.jsonl` |
| `worker.tileStorageUri` | Destination cloud storage URI prefix | `azure://valhalla-tiles/global/` |
| `worker.resources.requests.cpu` | Worker CPU request | `8` |
| `worker.resources.requests.memory`| Worker memory request | `16Gi` |
| `worker.resources.limits.cpu` | Worker CPU limit | `8` |
| `worker.resources.limits.memory` | Worker memory limit | `16Gi` |
