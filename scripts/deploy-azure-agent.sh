#!/bin/bash
# =====================================================================
# Teams Media Bot - Azure Agent Deployment Script
# Deploys FastAPI transcript sink + Streamlit UI to Azure Container Apps
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
OPENAI_NAME="aoai-talestral-poc"
OPENAI_DEPLOYMENT_NAME="gpt-5-mini"
CONTAINER_ENV_NAME="cae-talestral-poc"
FASTAPI_APP_NAME="ca-talestral-api"
STREAMLIT_APP_NAME="ca-talestral-ui"
DOMAIN="qmachina.com"
SUBDOMAIN="agent"
FQDN="${SUBDOMAIN}.${DOMAIN}"

# Container configuration (minimal for POC)
FASTAPI_CPU="0.25"
FASTAPI_MEMORY="0.5Gi"
STREAMLIT_CPU="0.25"
STREAMLIT_MEMORY="0.5Gi"

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

# Check if FastAPI app exists
if az containerapp show --name "$FASTAPI_APP_NAME" --resource-group "$RG_NAME" > /dev/null 2>&1; then
    echo "⚠️  FastAPI container app already exists, updating..."
    
    az containerapp update \
        --name "$FASTAPI_APP_NAME" \
        --resource-group "$RG_NAME" \
        --set-env-vars \
            "AZURE_OPENAI_ENDPOINT=$AOAI_ENDPOINT" \
            "AZURE_OPENAI_KEY=$AOAI_KEY" \
            "AZURE_OPENAI_DEPLOYMENT=$OPENAI_DEPLOYMENT_NAME" \
            "OPENAI_API_TYPE=azure" \
            "OPENAI_REASONING_EFFORT=low" \
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
        --env-vars \
            "AZURE_OPENAI_ENDPOINT=$AOAI_ENDPOINT" \
            "AZURE_OPENAI_KEY=$AOAI_KEY" \
            "AZURE_OPENAI_DEPLOYMENT=$OPENAI_DEPLOYMENT_NAME" \
            "OPENAI_API_TYPE=azure" \
            "OPENAI_REASONING_EFFORT=low" \
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
# Step 4: Build and Deploy Streamlit Container
# =====================================================================
echo "📦 Step 4: Deploying Streamlit UI..."

# Check if Streamlit app exists
if az containerapp show --name "$STREAMLIT_APP_NAME" --resource-group "$RG_NAME" > /dev/null 2>&1; then
    echo "⚠️  Streamlit container app already exists, updating..."
    
    az containerapp update \
        --name "$STREAMLIT_APP_NAME" \
        --resource-group "$RG_NAME" \
        --set-env-vars \
            "SINK_URL=https://$FASTAPI_FQDN" \
        --output none
    
    echo "✅ Streamlit container updated"
else
    echo "Creating Streamlit container app..."
    
    # Create the container app with source code deployment
    # Uses Dockerfile.streamlit (separate from the default Dockerfile which targets FastAPI)
    az containerapp create \
        --name "$STREAMLIT_APP_NAME" \
        --resource-group "$RG_NAME" \
        --environment "$CONTAINER_ENV_NAME" \
        --source "$PYTHON_DIR" \
        --dockerfile Dockerfile.streamlit \
        --ingress external \
        --target-port 8501 \
        --cpu "$STREAMLIT_CPU" \
        --memory "$STREAMLIT_MEMORY" \
        --min-replicas 0 \
        --max-replicas 2 \
        --env-vars \
            "SINK_URL=https://$FASTAPI_FQDN" \
        --output none
    
    echo "✅ Streamlit container app created"
fi

# Get Streamlit FQDN
STREAMLIT_FQDN=$(az containerapp show \
    --name "$STREAMLIT_APP_NAME" \
    --resource-group "$RG_NAME" \
    --query properties.configuration.ingress.fqdn -o tsv)

echo "  Streamlit URL: https://$STREAMLIT_FQDN"
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
echo "  FastAPI: https://$FASTAPI_FQDN"
echo "  Streamlit: https://$STREAMLIT_FQDN"
echo ""
echo "========================================"
echo "📝 GODADDY DNS CONFIGURATION"
echo "========================================"
echo ""
echo "Go to GoDaddy DNS Management for $DOMAIN:"
echo "  https://dcc.godaddy.com/manage/dns"
echo ""
echo "Create the following CNAME record:"
echo ""
echo "┌────────────────────────────────────────────────────────────┐"
echo "│  Type:  CNAME                                              │"
echo "│  Name:  $SUBDOMAIN                                         │"
echo "│  Value: $FASTAPI_FQDN                                      │"
echo "│  TTL:   600 (10 minutes)                                   │"
echo "└────────────────────────────────────────────────────────────┘"
echo ""
echo "For Streamlit UI (optional separate subdomain):"
echo ""
echo "┌────────────────────────────────────────────────────────────┐"
echo "│  Type:  CNAME                                              │"
echo "│  Name:  interview                                          │"
echo "│  Value: $STREAMLIT_FQDN                                    │"
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
echo "Update the C# bot's appsettings.json TranscriptSink section:"
echo ""
echo '  "TranscriptSink": {'
echo "    \"PythonEndpoint\": \"https://$FQDN/transcript\""
echo '  }'
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
