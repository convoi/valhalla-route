#!/usr/bin/env bash
# ==============================================================================
# launch_docker_spot_builder.sh
#
# Launches an ephemeral Azure Spot VM with System-Assigned Managed Identity
# to execute continental Valhalla tile compilation inside a Docker container.
#
# Authentication: Zero secrets or keys stored. Azure Managed Identity (IMDS)
# is used by the container to authenticate to Azure Blob Storage directly.
#
# Lifecycle: Ephemeral Spot VM runs the job, uploads tiles to blob storage,
# and automatically powers off to terminate billing.
# ==============================================================================
set -euo pipefail

# Default Configuration
RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:-rg-valhalla}"
LOCATION="${AZURE_LOCATION:-westeurope}"
VM_SIZE="${AZURE_VM_SIZE:-Standard_E32s_v5}" # 32 vCPUs, 256 GB RAM (~$0.45/hr on Spot)
CONTAINER_NAME="valhalla-tiles"
DATE_TAG="${AZURE_DATE_TAG:-$(date +%Y-%m-%d)}"
CONTINENTS="europe,north-america,south-america,oceania"
BUILD_ADMINS=true
DOCKER_IMAGE=""
STORAGE_ACCOUNT=""
ACR_NAME=""

print_usage() {
  cat <<EOF
Usage: $(basename "$0") --image <docker_image> --storage-account <account_name> [options]

Required Arguments:
  -i, --image             Docker image to execute (e.g. 'myacr.azurecr.io/valhalla-route:latest')
  -s, --storage-account   Azure Storage Account name for destination blob storage

Optional Arguments:
  -c, --container         Storage container name (default: valhalla-tiles)
  -d, --date-folder       Destination date folder/snapshot tag (default: current date, e.g. $(date +%Y-%m-%d))
  -r, --resource-group    Azure Resource Group (default: rg-valhalla)
  -l, --location          Azure Region (default: westeurope)
  -v, --vm-size           Azure VM Size (default: Standard_E32s_v5)
  --continents            Comma-separated continents (default: europe,north-america,south-america,oceania)
  --acr-name              Azure Container Registry name (if using private ACR for the Docker image)
  --no-build-admins       Skip compiling admin.sqlite boundary database
  -h, --help              Show this help message
EOF
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    -i|--image)
      DOCKER_IMAGE="$2"
      shift 2
      ;;
    -s|--storage-account)
      STORAGE_ACCOUNT="$2"
      shift 2
      ;;
    -c|--container)
      CONTAINER_NAME="$2"
      shift 2
      ;;
    -d|--date-folder|--date-tag)
      DATE_TAG="$2"
      shift 2
      ;;
    -r|--resource-group)
      RESOURCE_GROUP="$2"
      shift 2
      ;;
    -l|--location)
      LOCATION="$2"
      shift 2
      ;;
    -v|--vm-size)
      VM_SIZE="$2"
      shift 2
      ;;
    --continents)
      CONTINENTS="$2"
      shift 2
      ;;
    --acr-name)
      ACR_NAME="$2"
      shift 2
      ;;
    --no-build-admins)
      BUILD_ADMINS=false
      shift
      ;;
    -h|--help)
      print_usage
      exit 0
      ;;
    *)
      echo "Error: Unknown argument $1" >&2
      print_usage
      exit 1
      ;;
  esac
done

if [[ -z "$DOCKER_IMAGE" || -z "$STORAGE_ACCOUNT" ]]; then
  echo "Error: Both --image and --storage-account are required." >&2
  print_usage
  exit 1
fi

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
VM_NAME="valhalla-builder-${TIMESTAMP}"
TMP_CLOUD_INIT=$(mktemp /tmp/cloud-init-valhalla-XXXXXX.yaml)

echo "=================================================================="
echo " Launching Valhalla Docker Builder on Azure Spot VM"
echo "=================================================================="
echo "VM Name:           $VM_NAME"
echo "VM Size:           $VM_SIZE (Spot)"
echo "Resource Group:    $RESOURCE_GROUP"
echo "Location:          $LOCATION"
echo "Docker Image:      $DOCKER_IMAGE"
echo "Storage Target:    az://$STORAGE_ACCOUNT/$CONTAINER_NAME/$DATE_TAG/"
echo "Date Snapshot:     $DATE_TAG"
echo "Continents:        $CONTINENTS"
echo "Build Admins:      $BUILD_ADMINS"
echo "=================================================================="

# 1. Verify az CLI and authentication
if ! command -v az &>/dev/null; then
  echo "Error: Azure CLI ('az') is not installed or not in PATH." >&2
  exit 1
fi

echo "Verifying Azure CLI authentication..."
az account show --output none || { echo "Error: Not logged into Azure. Run 'az login' first." >&2; exit 1; }

# 2. Verify Storage Account and get Resource ID
echo "Resolving Storage Account ID..."
STORAGE_ID=$(az storage account show \
  --name "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --query id \
  --output tsv 2>/dev/null) || {
    echo "Error: Storage account '$STORAGE_ACCOUNT' not found in resource group '$RESOURCE_GROUP'." >&2
    exit 1
}

# Optional ACR verification
ACR_ID=""
if [[ -n "$ACR_NAME" ]]; then
  echo "Resolving ACR ID for $ACR_NAME..."
  ACR_ID=$(az acr show --name "$ACR_NAME" --query id --output tsv 2>/dev/null || true)
fi

# 3. Generate dynamic cloud-init script
ADMIN_FLAG=""
if [[ "$BUILD_ADMINS" == "true" ]]; then
  ADMIN_FLAG="--build-admins"
fi

ACR_LOGIN_STEP=""
if [[ -n "$ACR_NAME" ]]; then
  ACR_LOGIN_STEP="
  # Log in to private Azure Container Registry via Instance Metadata Service
  - az login --identity
  - az acr login --name ${ACR_NAME}
"
fi

cat <<EOF > "$TMP_CLOUD_INIT"
#cloud-config
package_update: true
packages:
  - curl
  - ca-certificates
  - docker.io
  - azure-cli

runcmd:
  # 1. Prepare local high-speed SSD scratch volume
  - mkdir -p /mnt/valhalla_work
  - chmod 777 /mnt/valhalla_work
  - systemctl start docker
  - systemctl enable docker
$ACR_LOGIN_STEP
  # 2. Pull user Docker image
  - docker pull ${DOCKER_IMAGE}

  # 3. Execute Valhalla continental build inside Docker container
  # --net host connects to Azure IMDS (169.254.169.254) for Managed Identity auth
  - |
    docker run --rm \
      --net host \
      -v /mnt/valhalla_work:/work \
      -e AZURE_STORAGE_ACCOUNT="${STORAGE_ACCOUNT}" \
      ${DOCKER_IMAGE} \
      build-continents \
      --continents "${CONTINENTS}" \
      --destination-storage "az://${STORAGE_ACCOUNT}/${CONTAINER_NAME}/${DATE_TAG}/" \
      --work-dir /work \
      ${ADMIN_FLAG} > /mnt/valhalla_work/build.log 2>&1

  # 4. Upload execution log to date folder
  - |
    az storage blob upload \
      --account-name "${STORAGE_ACCOUNT}" \
      --container-name "${CONTAINER_NAME}" \
      --auth-mode login \
      --file /mnt/valhalla_work/build.log \
      --name "${DATE_TAG}/logs/build-${TIMESTAMP}.log" \
      --overwrite || true

  # 5. Update latest.txt pointer at container root
  - |
    echo "${DATE_TAG}" > /mnt/valhalla_work/latest.txt
    az storage blob upload \
      --account-name "${STORAGE_ACCOUNT}" \
      --container-name "${CONTAINER_NAME}" \
      --auth-mode login \
      --file /mnt/valhalla_work/latest.txt \
      --name "latest.txt" \
      --overwrite || true

  # 6. Automatically shut down Spot VM to stop billing immediately
  - poweroff
EOF

# 4. Launch Azure Spot VM with System-Assigned Managed Identity
echo "Creating Spot VM and assigning Managed Identity..."
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
  --custom-data "@$TMP_CLOUD_INIT" \
  --no-wait

# If ACR is used, grant AcrPull role to VM's Managed Identity
if [[ -n "$ACR_ID" ]]; then
  echo "Granting AcrPull role on $ACR_NAME to VM Managed Identity..."
  VM_PRINCIPAL_ID=$(az vm show -g "$RESOURCE_GROUP" -n "$VM_NAME" --query identity.principalId -o tsv)
  if [[ -n "$VM_PRINCIPAL_ID" ]]; then
    az role assignment create \
      --assignee-object-id "$VM_PRINCIPAL_ID" \
      --assignee-principal-type ServicePrincipal \
      --role "AcrPull" \
      --scope "$ACR_ID" \
      --output none || true
  fi
fi

rm -f "$TMP_CLOUD_INIT"

echo "------------------------------------------------------------------"
echo "VM '$VM_NAME' initiated successfully in background."
echo ""
echo "Monitoring Commands:"
echo "  VM Status: az vm get-instance-view -g $RESOURCE_GROUP -n $VM_NAME --query instanceView.statuses"
echo "  Build Log: az storage blob download --account-name $STORAGE_ACCOUNT -c $CONTAINER_NAME -n ${DATE_TAG}/logs/build-${TIMESTAMP}.log --file build.log --auth-mode login"
echo "  Pointer:   az storage blob download --account-name $STORAGE_ACCOUNT -c $CONTAINER_NAME -n latest.txt --file latest.txt --auth-mode login"
echo "------------------------------------------------------------------"
