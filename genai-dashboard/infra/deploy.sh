#!/usr/bin/env bash
# deploy.sh — Deploy the full GenAI Adoption Dashboard stack to Azure
# Prerequisites: az CLI logged in, Docker daemon running
# Usage: bash infra/deploy.sh

set -euo pipefail

# ── USER-CONFIGURABLE VARIABLES ──────────────────────────────────────────────
RESOURCE_GROUP="genai-dashboard-rg"
LOCATION="westeurope"
ACR_NAME="genaidashboardacr"
CONTAINER_APP_NAME="genai-dashboard-api"
CONTAINER_APP_ENV="genai-dashboard-env"
STATIC_APP_NAME="genai-dashboard-ui"
KEY_VAULT_NAME="genai-dashboard-kv"
STORAGE_ACCOUNT="genaidashboardsa"
IMAGE_TAG="latest"
# ─────────────────────────────────────────────────────────────────────────────

IMAGE_NAME="${ACR_NAME}.azurecr.io/genai-dashboard-api:${IMAGE_TAG}"

echo "==> 1/9  Resource group"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

echo "==> 2/9  Container Registry"
az acr create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$ACR_NAME" \
  --sku Basic \
  --admin-enabled true \
  --output none

echo "==> 3/9  Storage account + tables"
az storage account create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$STORAGE_ACCOUNT" \
  --location "$LOCATION" \
  --sku Standard_LRS \
  --kind StorageV2 \
  --output none

STORAGE_CONN=$(az storage account show-connection-string \
  --resource-group "$RESOURCE_GROUP" \
  --name "$STORAGE_ACCOUNT" \
  --query connectionString -o tsv)

for TABLE in MetricsCache MetricsRaw; do
  az storage table create \
    --name "$TABLE" \
    --connection-string "$STORAGE_CONN" \
    --output none || true
done

echo "==> 4/9  Key Vault"
az keyvault create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$KEY_VAULT_NAME" \
  --location "$LOCATION" \
  --output none

az keyvault secret set \
  --vault-name "$KEY_VAULT_NAME" \
  --name "azure-storage-connection-string" \
  --value "$STORAGE_CONN" \
  --output none

echo "==> 5/9  Build + push Docker image"
az acr login --name "$ACR_NAME"
docker build -t "$IMAGE_NAME" ./backend
docker push "$IMAGE_NAME"

echo "==> 6/9  Container Apps environment"
az containerapp env create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$CONTAINER_APP_ENV" \
  --location "$LOCATION" \
  --output none

echo "==> 7/9  Container App"
FRONTEND_URL="https://${STATIC_APP_NAME}.azurestaticapps.net"

az containerapp create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$CONTAINER_APP_NAME" \
  --environment "$CONTAINER_APP_ENV" \
  --image "$IMAGE_NAME" \
  --registry-server "${ACR_NAME}.azurecr.io" \
  --registry-identity system \
  --target-port 8000 \
  --ingress external \
  --min-replicas 1 \
  --max-replicas 3 \
  --env-vars \
    "KEY_VAULT_NAME=${KEY_VAULT_NAME}" \
    "CORS_ORIGINS=${FRONTEND_URL},http://localhost:3000" \
  --output none

echo "==> 8/9  Static Web App"
az staticwebapp create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$STATIC_APP_NAME" \
  --location "$LOCATION" \
  --sku Free \
  --output none

echo "==> 9/9  Done!"
BACKEND_URL=$(az containerapp show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$CONTAINER_APP_NAME" \
  --query "properties.configuration.ingress.fqdn" -o tsv)

echo ""
echo "  Backend URL : https://${BACKEND_URL}"
echo "  Frontend URL: ${FRONTEND_URL}"
echo ""
echo "Next step: edit frontend/index.html and set:"
echo "  const BACKEND_URL = 'https://${BACKEND_URL}';"
echo "Then deploy the frontend folder to the Static Web App."
