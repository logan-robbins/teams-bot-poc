#!/bin/bash
# =====================================================================
# Azure Infrastructure Setup Script (Mac)
# Teams Media Bot POC
# Run this on your Mac to set up all Azure resources
# =====================================================================

set -e  # Exit on error

echo "========================================"
echo "Teams Media Bot POC - Azure Setup"
echo "========================================"
echo ""

# Check if logged into Azure
echo "Checking Azure CLI login..."
if ! az account show > /dev/null 2>&1; then
    echo "❌ Not logged into Azure CLI"
    echo "Please run: az login"
    exit 1
fi

echo "✅ Azure CLI authenticated"
echo ""

# Get tenant and subscription info
TENANT_ID=$(az account show --query tenantId -o tsv)
SUBSCRIPTION_ID=$(az account show --query id -o tsv)
USER_EMAIL=$(az account show --query user.name -o tsv)

echo "Tenant ID: $TENANT_ID"
echo "Subscription: $SUBSCRIPTION_ID"
echo "User: $USER_EMAIL"
echo ""

# Configuration
RG_NAME="rg-teams-media-bot-poc"
LOCATION="eastus"
BOT_NAME="teams-media-bot-poc"
SPEECH_NAME="speech-teams-bot-poc"
APP_DISPLAY_NAME="TeamsMediaBotPOC"

echo "Configuration:"
echo "  Resource Group: $RG_NAME"
echo "  Location: $LOCATION"
echo "  Bot Name: $BOT_NAME"
echo "  Speech Service: $SPEECH_NAME"
echo ""
read -p "Continue? (y/n) " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 1
fi

# Create resource group (if not exists)
echo ""
echo "📦 Creating resource group..."
az group create --name "$RG_NAME" --location "$LOCATION" --output none
echo "✅ Resource group ready"

# Check if app registration already exists
echo ""
echo "🔐 Checking for existing app registration..."
EXISTING_APP_ID=$(az ad app list --display-name "$APP_DISPLAY_NAME" --query "[0].appId" -o tsv 2>/dev/null || echo "")

if [ -n "$EXISTING_APP_ID" ]; then
    echo "⚠️  App registration already exists: $EXISTING_APP_ID"
    read -p "Use existing app? (y/n) " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        APP_ID="$EXISTING_APP_ID"
        echo "Using existing app registration"
    else
        echo "Aborted. Please delete the existing app registration first."
        exit 1
    fi
else
    echo "Creating new app registration..."
    APP_JSON=$(az ad app create --display-name "$APP_DISPLAY_NAME" --sign-in-audience AzureADMyOrg)
    APP_ID=$(echo "$APP_JSON" | jq -r '.appId')
    echo "✅ App registration created: $APP_ID"
    
    echo "Creating service principal..."
    az ad sp create --id "$APP_ID" --output none
    echo "✅ Service principal created"
    
    echo "Creating client secret..."
    SECRET_JSON=$(az ad app credential reset --id "$APP_ID" --append --display-name "POC-Secret" --years 1)
    CLIENT_SECRET=$(echo "$SECRET_JSON" | jq -r '.password')
    echo "✅ Client secret created"
    
    echo ""
    echo "🔑 SAVE THESE CREDENTIALS:"
    echo "APP_CLIENT_ID=$APP_ID"
    echo "APP_CLIENT_SECRET=$CLIENT_SECRET"
    echo ""
    
    # Add Microsoft Graph permissions
    echo "Adding Microsoft Graph permissions..."
    
    # Calls.AccessMedia.All
    az ad app permission add --id "$APP_ID" \
        --api 00000003-0000-0000-c000-000000000000 \
        --api-permissions a7a681dc-756e-4909-b988-f160edc6655f=Role \
        --output none
    
    # Calls.JoinGroupCall.All
    az ad app permission add --id "$APP_ID" \
        --api 00000003-0000-0000-c000-000000000000 \
        --api-permissions f6b49018-60ab-4f81-83bd-22caeabfed2d=Role \
        --output none
    
    echo "✅ Permissions added"
    
    echo "Granting admin consent..."
    az ad app permission admin-consent --id "$APP_ID"
    echo "✅ Admin consent granted"
fi

# Check Speech Service provider registration
echo ""
echo "🧠 Checking Speech Service provider..."
SPEECH_PROVIDER_STATE=$(az provider show --namespace Microsoft.CognitiveServices --query "registrationState" -o tsv 2>/dev/null || echo "NotRegistered")

if [ "$SPEECH_PROVIDER_STATE" != "Registered" ]; then
    echo "Registering Microsoft.CognitiveServices provider (this may take 2-3 minutes)..."
    az provider register --namespace Microsoft.CognitiveServices --wait
    echo "✅ Provider registered"
else
    echo "✅ Provider already registered"
fi

# Create Speech Service
echo ""
echo "🗣️  Creating Azure Speech Service..."
if az cognitiveservices account show --name "$SPEECH_NAME" --resource-group "$RG_NAME" > /dev/null 2>&1; then
    echo "⚠️  Speech service already exists"
else
    az cognitiveservices account create \
        --name "$SPEECH_NAME" \
        --resource-group "$RG_NAME" \
        --kind SpeechServices \
        --sku S0 \
        --location "$LOCATION" \
        --yes \
        --output none
    echo "✅ Speech service created"
fi

# Get Speech Service keys
SPEECH_KEY=$(az cognitiveservices account keys list \
    --name "$SPEECH_NAME" \
    --resource-group "$RG_NAME" \
    --query "key1" -o tsv)

echo "✅ Speech service ready"

# Check Bot Service provider registration
echo ""
echo "🤖 Checking Bot Service provider..."
BOT_PROVIDER_STATE=$(az provider show --namespace Microsoft.BotService --query "registrationState" -o tsv 2>/dev/null || echo "NotRegistered")

if [ "$BOT_PROVIDER_STATE" != "Registered" ]; then
    echo "Registering Microsoft.BotService provider (this may take 2-3 minutes)..."
    az provider register --namespace Microsoft.BotService --wait
    echo "✅ Provider registered"
else
    echo "✅ Provider already registered"
fi

# Create Azure Bot
echo ""
echo "🤖 Creating Azure Bot..."
if az bot show --name "$BOT_NAME" --resource-group "$RG_NAME" > /dev/null 2>&1; then
    echo "⚠️  Bot already exists"
else
    az bot create \
        --resource-group "$RG_NAME" \
        --name "$BOT_NAME" \
        --app-type SingleTenant \
        --appid "$APP_ID" \
        --tenant-id "$TENANT_ID" \
        --endpoint "https://placeholder.ngrok-free.app/api/calling" \
        --sku F0 \
        --output none
    echo "✅ Bot created"
fi

# Enable Teams channel
echo ""
echo "📱 Enabling Teams channel..."
if az bot msteams show --name "$BOT_NAME" --resource-group "$RG_NAME" > /dev/null 2>&1; then
    echo "⚠️  Teams channel already enabled"
else
    az bot msteams create --name "$BOT_NAME" --resource-group "$RG_NAME" --output none
    echo "✅ Teams channel enabled"
fi

# Summary
echo ""
echo "========================================"
echo "✅ SETUP COMPLETE!"
echo "========================================"
echo ""
echo "📋 Configuration Summary:"
echo ""
echo "TENANT_ID=$TENANT_ID"
echo "APP_CLIENT_ID=$APP_ID"
if [ -n "$CLIENT_SECRET" ]; then
    echo "APP_CLIENT_SECRET=$CLIENT_SECRET"
fi
echo "SPEECH_KEY=$SPEECH_KEY"
echo "SPEECH_REGION=$LOCATION"
echo ""
echo "📝 Next Steps:"
echo ""
echo "1. Update src/Config/appsettings.json with the values above"
echo ""
echo "2. Set up DNS:"
echo "   Create CNAME: 0.botpoc.YOURDOMAIN.com → 0.tcp.ngrok.io"
echo ""
echo "3. Get SSL certificate for: *.botpoc.YOURDOMAIN.com"
echo ""
echo "4. Set up Windows VM (local Parallels or Azure)"
echo ""
echo "5. Transfer code to Windows VM (Git or shared folder)"
echo ""
echo "6. Install certificate on Windows VM"
echo ""
echo "7. Update scripts/ngrok.yml with your ngrok authtoken"
echo ""
echo "8. Start ngrok, update appsettings.json with ngrok URLs"
echo ""
echo "9. Update Azure Bot calling webhook in portal:"
echo "   https://portal.azure.com → Bot → Channels → Teams → Calling"
echo "   Set webhook to: https://YOUR-NGROK.ngrok-free.app/api/calling"
echo ""
echo "10. Build and run in Visual Studio on Windows VM"
echo ""
echo "See SETUP-GUIDE.md for detailed instructions!"
echo ""
