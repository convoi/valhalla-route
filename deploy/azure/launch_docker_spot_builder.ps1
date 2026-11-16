<#
.SYNOPSIS
    Launches an ephemeral Azure Spot VM with System-Assigned Managed Identity
    to run continental Valhalla tile compilation inside a Docker container.

.DESCRIPTION
    Zero secrets or passwords required. Uses Azure Managed Identity (IMDS)
    for authentication to Blob Storage and optional Azure Container Registry.
    Mounts local high-speed SSD to the container, and automatically terminates
    the VM when the job completes.

.PARAMETER Image
    Docker image to run (e.g., 'myacr.azurecr.io/valhalla-route:latest').
.PARAMETER StorageAccount
    Azure Storage Account name.
.PARAMETER Container
    Storage container name (default: 'valhalla-tiles').
.PARAMETER ResourceGroup
    Azure Resource Group name (default: 'rg-valhalla').
.PARAMETER Location
    Azure Region (default: 'westeurope').
.PARAMETER VmSize
    VM size (default: 'Standard_E32s_v5' - 32 vCPUs, 256 GB RAM).
.PARAMETER Continents
    Comma-separated list of continents (default: 'europe,north-america,south-america,oceania').
.PARAMETER AcrName
    Optional ACR name if pulling from private Azure Container Registry.
.PARAMETER SkipAdmins
    Switch to skip compiling admin.sqlite boundary database.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Image,

    [Parameter(Mandatory = $true)]
    [string]$StorageAccount,

    [string]$Container = "valhalla-tiles",
    [string]$ResourceGroup = "rg-valhalla",
    [string]$Location = "westeurope",
    [string]$VmSize = "Standard_E32s_v5",
    [string]$Continents = "europe,north-america,south-america,oceania",
    [string]$AcrName = "",
    [switch]$SkipAdmins
)

$ErrorActionPreference = "Stop"

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$vmName = "valhalla-builder-$timestamp"
$adminFlag = if ($SkipAdmins) { "" } else { "--build-admins" }

Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host " Launching Valhalla Docker Builder on Azure Spot VM" -ForegroundColor Cyan
Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host "VM Name:           $vmName"
Write-Host "VM Size:           $VmSize (Spot)"
Write-Host "Resource Group:    $ResourceGroup"
Write-Host "Location:          $Location"
Write-Host "Docker Image:      $Image"
Write-Host "Storage Target:    az://$StorageAccount/$Container/global/"
Write-Host "Continents:        $Continents"
Write-Host "Build Admins:      $(-not $SkipAdmins)"
Write-Host "=================================================================="

# 1. Verify az CLI
if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    Write-Error "Azure CLI ('az') is not installed or not in PATH."
}

# 2. Verify login & storage account
az account show --output none
if ($LASTEXITCODE -ne 0) {
    Write-Error "Not logged into Azure. Please run 'az login' first."
}

$storageId = az storage account show --name $StorageAccount --resource-group $ResourceGroup --query id -o tsv
if (-not $storageId) {
    Write-Error "Could not find storage account '$StorageAccount' in resource group '$ResourceGroup'."
}

$acrId = ""
$acrLoginStep = ""
if ($AcrName) {
    $acrId = az acr show --name $AcrName --query id -o tsv
    $acrLoginStep = @"
  # Log in to private Azure Container Registry via Instance Metadata Service
  - az login --identity
  - az acr login --name $AcrName
"@
}

# 3. Create Cloud-Init YAML
$cloudInitContent = @"
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
$acrLoginStep
  # 2. Pull user Docker image
  - docker pull $Image

  # 3. Execute Valhalla continental build inside Docker container
  # --net host connects to Azure IMDS (169.254.169.254) for Managed Identity auth
  - |
    docker run --rm \
      --net host \
      -v /mnt/valhalla_work:/work \
      -e AZURE_STORAGE_ACCOUNT="$StorageAccount" \
      $Image \
      build-continents \
      --continents "$Continents" \
      --destination-storage "az://$StorageAccount/$Container/global/" \
      --work-dir /work \
      $adminFlag > /mnt/valhalla_work/build.log 2>&1

  # 4. Upload execution log to blob storage
  - |
    az storage blob upload \
      --account-name "$StorageAccount" \
      --container-name "$Container" \
      --auth-mode login \
      --file /mnt/valhalla_work/build.log \
      --name "global/logs/build-$timestamp.log" \
      --overwrite || true

  # 5. Automatically shut down Spot VM to stop billing immediately
  - poweroff
"@

$tempCloudInit = [System.IO.Path]::GetTempFileName() + ".yaml"
[System.IO.File]::WriteAllText($tempCloudInit, $cloudInitContent)

# 4. Launch Spot VM
Write-Host "Creating Spot VM with Managed Identity..." -ForegroundColor Yellow
az vm create `
  --resource-group $ResourceGroup `
  --name $vmName `
  --location $Location `
  --image "Ubuntu2404" `
  --size $VmSize `
  --priority Spot `
  --eviction-policy Deallocate `
  --max-price -1 `
  --assign-identity [system] `
  --role "Storage Blob Data Contributor" `
  --scope $storageId `
  --custom-data "@$tempCloudInit" `
  --no-wait

# If ACR used, grant AcrPull
if ($acrId) {
    Write-Host "Granting AcrPull role to VM Managed Identity..."
    Start-Sleep -Seconds 5
    $vmPrincipalId = az vm show -g $ResourceGroup -n $vmName --query identity.principalId -o tsv
    if ($vmPrincipalId) {
        az role assignment create `
          --assignee-object-id $vmPrincipalId `
          --assignee-principal-type ServicePrincipal `
          --role "AcrPull" `
          --scope $acrId `
          --output none
    }
}

Remove-Item -Force $tempCloudInit

Write-Host "------------------------------------------------------------------" -ForegroundColor Green
Write-Host "VM '$vmName' successfully launched in background." -ForegroundColor Green
Write-Host ""
Write-Host "To monitor status:"
Write-Host "  az vm get-instance-view -g $ResourceGroup -n $vmName --query instanceView.statuses"
Write-Host "To check logs once finished:"
Write-Host "  az storage blob download --account-name $StorageAccount -c $Container -n global/logs/build-$timestamp.log --file build.log --auth-mode login"
Write-Host "------------------------------------------------------------------"
