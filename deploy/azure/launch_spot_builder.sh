#!/usr/bin/env bash
# ==============================================================================
# Launch an ephemeral Azure Spot VM for Valhalla Continental Tile Generation
# ==============================================================================
set -euo pipefail

# Configuration variables (modify or pass as environment variables)
RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:-rg-valhalla}"
LOCATION="${AZURE_LOCATION:-westeurope}"
VM_NAME="valhalla-builder-$(date +%Y%m%d-%H%M)"
VM_SIZE="${AZURE_VM_SIZE:-Standard_E32s_v5}" # 32 vCPUs, 256 GB RAM (~$0.45/hr on Spot)
STORAGE_ACCOUNT="${AZURE_STORAGE_ACCOUNT:-mystorageaccount}"
CONTAINER_NAME="${AZURE_STORAGE_CONTAINER:-valhalla-tiles}"
DATE_TAG="${AZURE_DATE_TAG:-$(date +%Y-%m-%d)}"
CLOUD_INIT_PATH="$(dirname "$0")/cloud-init.yaml"

echo "=================================================================="
echo "Launching Valhalla Continental Builder on Azure Spot VM"
echo "VM Name:           $VM_NAME"
echo "VM Size:           $VM_SIZE (Spot)"
echo "Resource Group:    $RESOURCE_GROUP"
echo "Location:          $LOCATION"
echo "Storage Target:    az://$STORAGE_ACCOUNT/$CONTAINER_NAME/$DATE_TAG/"
echo "Date Snapshot:     $DATE_TAG"
echo "=================================================================="

# 1. Retrieve Storage Account Resource ID for RBAC role assignment
STORAGE_ID=$(az storage account show \
  --name "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --query id \
  --output tsv)

# 2. Launch Spot VM with Managed Identity and cloud-init execution
az vm create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$VM_NAME" \
  --location "$LOCATION" \
  --image "Ubuntu2404" \
  --size "$VM_SIZE" \
  --priority Spot \
  --eviction-policy Deallocate \
  --max-price -1 \
  --assign-identity [system] \
  --role "Storage Blob Data Contributor" \
  --scope "$STORAGE_ID" \
  --custom-data "@$CLOUD_INIT_PATH" \
  --no-wait

echo "------------------------------------------------------------------"
echo "VM deployment initiated successfully in background."
echo "The VM will:"
echo "  1. Download continental PBFs (Europe, Americas, Oceania)"
echo "  2. Compile routing graph tiles and companion feature parquets"
echo "  3. Upload everything to az://$CONTAINER_NAME/global/"
echo "  4. Automatically shut down and deallocate itself upon completion."
echo "------------------------------------------------------------------"
echo "To monitor status:"
echo "  az vm get-instance-view -g $RESOURCE_GROUP -n $VM_NAME --query instanceView.statuses"
