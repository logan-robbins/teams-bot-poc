#!/bin/bash
# =====================================================================
# Teams Media Bot - Azure Agent Deployment Script
# Deploys FastAPI transcript sink + React Dossier UI to Azure Container Apps
# with Azure OpenAI backend
# =====================================================================

set -e

echo "========================================"
echo "Teams Media Bot - Azure Agent Deployment"
echo "Target: agent.qmachina.com"
echo "========================================"
echo ""

# Configuration
RG_NAME="rg-teams-media-bot-poc"
LOCATION="eastus"
OPENAI_NAME="aoai-alfred-poc"
OPENAI_DEPLOYMENT_NAME="gpt-5-mini"
CONTAINER_ENV_NAME="cae-alfred-poc"
FASTAPI_APP_NAME="ca-alfred-api"
WEB_APP_NAME="ca-alfred-web"
DOMAIN="qmachina.com"
SUBDOMAIN="agent"
FQDN="${SUBDOMAIN}.${DOMAIN}"

# Container configuration (minimal for POC)
FASTAPI_CPU="0.25"
FASTAPI_MEMORY="0.5Gi"
WEB_CPU="0.25"
WEB_MEMORY="0.5Gi"

# ALFRED runtime env. Required by transcript_sink.load_runtime_config().
ALFRED_VARIANT_ID="alfred"
ALFRED_INSTANCE_ID="alfred"
ALFRED_PRODUCT_SPEC_PATH="/app/legionmeet_platform/specs/alfred.yaml"

# Optional: URL of the C# bot's /api/send-chat endpoint. Set to enable the
# send_to_meeting_chat agent tool. Leave empty for dry-run mode (sink logs
# the would-be message and appends to the ledger but does not POST).
BOT_SEND_CHAT_URL="${BOT_SEND_CHAT_URL:-}"

# Check if logged into Azure
echo "Checking Azure CLI login..."
if ! az account show > /dev/null 2>&1; then
    echo "❌ Not logged into Azure CLI"
    echo "Please run: az login"
    exit 1
fi

TENANT_ID=$(az account show --query tenantId -o tsv)
SUBSCRIPTION_ID=$(az account show --query id -o tsv)

echo "✅ Azure CLI authenticated"
echo "Tenant: $TENANT_ID"
echo "Subscription: $SUBSCRIPTION_ID"
echo ""

# Verify resource group exists
echo "Verifying resource group..."
if ! az group show --name "$RG_NAME" > /dev/null 2>&1; then
    echo "❌ Resource group $RG_NAME does not exist"
    echo "Run deploy-azure-vm.sh first to create infrastructure"
    exit 1
fi
echo "✅ Resource group exists: $RG_NAME"
echo ""

# =====================================================================
# Step 1: Create Azure OpenAI Resource
# =====================================================================
echo "📦 Step 1: Creating Azure OpenAI Resource..."

# Check if OpenAI resource exists
if az cognitiveservices account show --name "$OPENAI_NAME" --resource-group "$RG_NAME" > /dev/null 2>&1; then
    echo "⚠️  Azure OpenAI resource already exists: $OPENAI_NAME"
else
    echo "Creating Azure OpenAI resource..."
    az cognitiveservices account create \
        --name "$OPENAI_NAME" \
        --resource-group "$RG_NAME" \
        --location "$LOCATION" \
        --kind OpenAI \
        --sku S0 \
        --custom-domain "$OPENAI_NAME" \
        --output none
    
    echo "✅ Azure OpenAI resource created"
fi

# Get Azure OpenAI endpoint and key
AOAI_ENDPOINT=$(az cognitiveservices account show \
    --name "$OPENAI_NAME" \
    --resource-group "$RG_NAME" \
    --query properties.endpoint -o tsv)

AOAI_KEY=$(az cognitiveservices account keys list \
    --name "$OPENAI_NAME" \
    --resource-group "$RG_NAME" \
    --query key1 -o tsv)

echo "  Endpoint: $AOAI_ENDPOINT"
echo ""

# Deploy gpt-4o model
echo "Deploying gpt-4o model..."
if az cognitiveservices account deployment show \
    --name "$OPENAI_NAME" \
    --resource-group "$RG_NAME" \
    --deployment-name "$OPENAI_DEPLOYMENT_NAME" > /dev/null 2>&1; then
    echo "⚠️  Model deployment already exists: $OPENAI_DEPLOYMENT_NAME"
else
    az cognitiveservices account deployment create \
        --name "$OPENAI_NAME" \
        --resource-group "$RG_NAME" \
        --deployment-name "$OPENAI_DEPLOYMENT_NAME" \
        --model-name "gpt-5-mini" \
        --model-version "2025-08-07" \
        --model-format OpenAI \
        --sku-capacity 10 \
        --sku-name GlobalStandard \
        --output none
    
    echo "✅ Model deployed: $OPENAI_DEPLOYMENT_NAME"
fi
echo ""

# =====================================================================
# Step 2: Create Container Apps Environment
# =====================================================================
echo "📦 Step 2: Creating Container Apps Environment..."

# Check if environment exists
if az containerapp env show --name "$CONTAINER_ENV_NAME" --resource-group "$RG_NAME" > /dev/null 2>&1; then
    echo "⚠️  Container Apps environment already exists: $CONTAINER_ENV_NAME"
else
    echo "Creating Container Apps environment..."
    az containerapp env create \
        --name "$CONTAINER_ENV_NAME" \
        --resource-group "$RG_NAME" \
        --location "$LOCATION" \
        --output none
    
    echo "✅ Container Apps environment created"
fi
echo ""

# =====================================================================
# Step 3: Build and Deploy FastAPI Container
# =====================================================================
echo "📦 Step 3: Deploying FastAPI Transcript Sink..."

# Get the script directory and project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON_DIR="$PROJECT_ROOT/python"

# Build the env-var list for the FastAPI sink. Includes ALFRED runtime
# requirements; conditionally includes BOT_SEND_CHAT_URL when set.
FASTAPI_ENV_VARS=(
    "AZURE_OPENAI_ENDPOINT=$AOAI_ENDPOINT"
    "AZURE_OPENAI_KEY=$AOAI_KEY"
    "AZURE_OPENAI_DEPLOYMENT=$OPENAI_DEPLOYMENT_NAME"
    "OPENAI_API_TYPE=azure"
    "OPENAI_REASONING_EFFORT=low"
    "VARIANT_ID=$ALFRED_VARIANT_ID"
    "INSTANCE_ID=$ALFRED_INSTANCE_ID"
    "PRODUCT_SPEC_PATH=$ALFRED_PRODUCT_SPEC_PATH"
    "SINK_PORT=8765"
)
if [ -n "$BOT_SEND_CHAT_URL" ]; then
    FASTAPI_ENV_VARS+=("BOT_SEND_CHAT_URL=$BOT_SEND_CHAT_URL")
fi

# Check if FastAPI app exists
if az containerapp show --name "$FASTAPI_APP_NAME" --resource-group "$RG_NAME" > /dev/null 2>&1; then
    echo "⚠️  FastAPI container app already exists, updating..."

    az containerapp update \
        --name "$FASTAPI_APP_NAME" \
        --resource-group "$RG_NAME" \
        --set-env-vars "${FASTAPI_ENV_VARS[@]}" \
        --output none

    echo "✅ FastAPI container updated"
else
    echo "Creating FastAPI container app..."

    # Create the container app with source code deployment
    az containerapp create \
        --name "$FASTAPI_APP_NAME" \
        --resource-group "$RG_NAME" \
        --environment "$CONTAINER_ENV_NAME" \
        --source "$PYTHON_DIR" \
        --ingress external \
        --target-port 8765 \
        --cpu "$FASTAPI_CPU" \
        --memory "$FASTAPI_MEMORY" \
        --min-replicas 0 \
        --max-replicas 3 \
        --env-vars "${FASTAPI_ENV_VARS[@]}" \
        --output none

    echo "✅ FastAPI container app created"
fi

# Get FastAPI FQDN
FASTAPI_FQDN=$(az containerapp show \
    --name "$FASTAPI_APP_NAME" \
    --resource-group "$RG_NAME" \
    --query properties.configuration.ingress.fqdn -o tsv)

echo "  FastAPI URL: https://$FASTAPI_FQDN"
echo ""

# =====================================================================
# Step 4: Build and Deploy React Dossier UI (the product surface)
# =====================================================================
echo "📦 Step 4: Deploying React Dossier UI..."

# nginx in the React container proxies /sink/* to the FastAPI sink, mirroring
# the Vite dev proxy in vite.config.ts. SINK_URL is substituted into the nginx
# config via NGINX_ENVSUBST_FILTER at container start.
WEB_DIR="$PROJECT_ROOT/web"

WEB_ENV_VARS=(
    "SINK_URL=https://$FASTAPI_FQDN"
    "NGINX_ENVSUBST_FILTER=^SINK_URL$"
)

if az containerapp show --name "$WEB_APP_NAME" --resource-group "$RG_NAME" > /dev/null 2>&1; then
    echo "⚠️  Web container app already exists, updating..."

    az containerapp update \
        --name "$WEB_APP_NAME" \
        --resource-group "$RG_NAME" \
        --set-env-vars "${WEB_ENV_VARS[@]}" \
        --output none

    echo "✅ Web container updated"
else
    echo "Creating Web container app..."

    az containerapp create \
        --name "$WEB_APP_NAME" \
        --resource-group "$RG_NAME" \
        --environment "$CONTAINER_ENV_NAME" \
        --source "$WEB_DIR" \
        --ingress external \
        --target-port 80 \
        --cpu "$WEB_CPU" \
        --memory "$WEB_MEMORY" \
        --min-replicas 0 \
        --max-replicas 2 \
        --env-vars "${WEB_ENV_VARS[@]}" \
        --output none

    echo "✅ Web container app created"
fi

# Get Web FQDN
WEB_FQDN=$(az containerapp show \
    --name "$WEB_APP_NAME" \
    --resource-group "$RG_NAME" \
    --query properties.configuration.ingress.fqdn -o tsv)

echo "  Web URL: https://$WEB_FQDN"
echo ""

# =====================================================================
# Step 5: Configure Custom Domain
# =====================================================================
echo "📦 Step 5: Configure Custom Domain..."

# Get Container Apps Environment default domain for CNAME target
ENV_DEFAULT_DOMAIN=$(az containerapp env show \
    --name "$CONTAINER_ENV_NAME" \
    --resource-group "$RG_NAME" \
    --query properties.defaultDomain -o tsv)

echo ""
echo "========================================"
echo "✅ DEPLOYMENT COMPLETE!"
echo "========================================"
echo ""
echo "📊 Azure OpenAI:"
echo "  Resource: $OPENAI_NAME"
echo "  Endpoint: $AOAI_ENDPOINT"
echo "  Deployment: $OPENAI_DEPLOYMENT_NAME"
echo ""
echo "📊 Container Apps:"
echo "  FastAPI sink: https://$FASTAPI_FQDN"
echo "  React UI:     https://$WEB_FQDN"
echo ""
echo "========================================"
echo "📝 OPTIONAL DNS CONFIGURATION"
echo "========================================"
echo ""
echo "Default Container Apps FQDNs above are publicly trusted (managed cert)."
echo "Skip this section if you want to use them as-is."
echo ""
echo "If you want a custom domain on $DOMAIN, create CNAMEs at your DNS host:"
echo ""
echo "┌────────────────────────────────────────────────────────────┐"
echo "│  Type:  CNAME   Name: $SUBDOMAIN   Value: $FASTAPI_FQDN     │"
echo "│  Type:  CNAME   Name: web          Value: $WEB_FQDN         │"
echo "│  TTL:   600 (10 minutes)                                   │"
echo "└────────────────────────────────────────────────────────────┘"
echo ""
echo "========================================"
echo "🔒 CUSTOM DOMAIN BINDING (After DNS propagation)"
echo "========================================"
echo ""
echo "After DNS propagates (5-15 minutes), run:"
echo ""
echo "# Verify DNS propagation"
echo "nslookup $FQDN"
echo ""
echo "# Add custom domain to FastAPI app"
echo "az containerapp hostname add \\"
echo "  --name $FASTAPI_APP_NAME \\"
echo "  --resource-group $RG_NAME \\"
echo "  --hostname $FQDN"
echo ""
echo "# Bind managed certificate"
echo "az containerapp hostname bind \\"
echo "  --name $FASTAPI_APP_NAME \\"
echo "  --resource-group $RG_NAME \\"
echo "  --hostname $FQDN \\"
echo "  --environment $CONTAINER_ENV_NAME \\"
echo "  --validation-method CNAME"
echo ""
echo "========================================"
echo "🔧 UPDATE C# BOT CONFIGURATION"
echo "========================================"
echo ""
echo "Update the C# bot's appsettings.json TranscriptSink section to point"
echo "at the FastAPI sink. Use the Container Apps FQDN by default; swap in"
echo "your custom domain only after the optional DNS+binding steps above."
echo ""
echo '  "TranscriptSink": {'
echo "    \"PythonEndpoint\": \"https://$FASTAPI_FQDN/transcript\","
echo "    \"ChatEndpoint\":   \"https://$FASTAPI_FQDN/chat\""
echo '  }'
echo ""
echo "Once the C# bot is running on its public hostname, re-run this script"
echo "with BOT_SEND_CHAT_URL set so the send_to_meeting_chat tool posts:"
echo ""
echo "  BOT_SEND_CHAT_URL=https://<bot-host>/api/send-chat \\"
echo "    ./scripts/deploy-azure-agent.sh"
echo ""
echo "Then restart the bot service on the Windows VM:"
echo "  Restart-Service TeamsMediaBot"
echo ""
echo "========================================"
echo "💰 ESTIMATED MONTHLY COST (POC)"
echo "========================================"
echo ""
echo "  Azure OpenAI (gpt-5-mini, low reasoning): ~\$10-30/month (usage-based)"
echo "  Container Apps (2 apps, scale-to-zero): ~\$0-10/month"
echo "  Total: ~\$10-50/month"
echo ""
echo "📚 Test endpoints:"
echo "  curl https://$FASTAPI_FQDN/health"
echo "  curl https://$FASTAPI_FQDN/stats"
echo ""
