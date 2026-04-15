#!/usr/bin/env bash
# keyvault.sh — Populate Azure Key Vault with any additional secrets
# Run this after deploy.sh if you need to store extra secrets.
# The storage connection string is already set by deploy.sh.
# Usage: bash infra/keyvault.sh

set -euo pipefail

KEY_VAULT_NAME="${KEY_VAULT_NAME:-genai-dashboard-kv}"

echo "Populating Key Vault: $KEY_VAULT_NAME"
echo "Leave a value blank to skip that secret."
echo ""

set_secret() {
  local name="$1"
  local prompt="$2"
  read -r -p "${prompt}: " value
  if [[ -n "$value" ]]; then
    az keyvault secret set \
      --vault-name "$KEY_VAULT_NAME" \
      --name "$name" \
      --value "$value" \
      --output none
    echo "  ✓ Set $name"
  else
    echo "  – Skipped $name"
  fi
}

# These are reserved for future live-API integration
set_secret "anthropic-admin-api-key"  "Anthropic Admin API key (sk-ant-admin-...)"
set_secret "openai-admin-api-key"     "OpenAI Admin API key"
set_secret "azure-client-id"          "Azure AD App Client ID (for Graph API)"
set_secret "azure-client-secret"      "Azure AD App Client Secret"
set_secret "azure-tenant-id"          "Azure Tenant ID"

echo ""
echo "Done. Secrets stored in Key Vault '$KEY_VAULT_NAME'."
