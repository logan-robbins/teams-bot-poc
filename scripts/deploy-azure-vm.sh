#!/bin/bash
# =====================================================================
# Teams Media Bot - Azure VM Deployment Script
# Creates production VM for qmachina.com (no ngrok)
# Run this from Mac terminal
# =====================================================================

set -e

echo "========================================"
echo "Teams Media Bot - Azure VM Deployment"
echo "Domain: qmachina.com (no ngrok)"
echo "========================================"
echo ""

# Configuration
RG_NAME="rg-teams-media-bot-poc"
VM_NAME="vm-teams-bot-prod"
LOCATION="eastus"
VM_SIZE="Standard_D4s_v3"
ADMIN_USER="azureuser"
ADMIN_PASS="SecureTeamsBot2026!"  # Change this!

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

# Check if VM already exists
echo "Checking if VM exists..."
if az vm show --name "$VM_NAME" --resource-group "$RG_NAME" > /dev/null 2>&1; then
    echo "⚠️  VM already exists: $VM_NAME"
    read -p "Delete and recreate? (y/n) " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "Deleting existing VM..."
        az vm delete --name "$VM_NAME" --resource-group "$RG_NAME" --yes --no-wait
        sleep 10
    else
        echo "Using existing VM"
        PUBLIC_IP=$(az vm show -d --name "$VM_NAME" --resource-group "$RG_NAME" --query publicIps -o tsv)
        echo "VM Public IP: $PUBLIC_IP"
        exit 0
    fi
fi

# Create VM with static public IP
echo ""
echo "📦 Creating Azure VM..."
echo "  Name: $VM_NAME"
echo "  Size: $VM_SIZE (4 vCPU, 16GB RAM)"
echo "  OS: Windows Server 2022"
echo "  Location: $LOCATION"
echo ""

az vm create \
  --resource-group "$RG_NAME" \
  --name "$VM_NAME" \
  --image Win2022Datacenter \
  --size "$VM_SIZE" \
  --admin-username "$ADMIN_USER" \
  --admin-password "$ADMIN_PASS" \
  --location "$LOCATION" \
  --public-ip-sku Standard \
  --public-ip-address-allocation static \
  --nsg-rule NONE \
  --output json > /tmp/vm-creation.json

echo "✅ VM created"

# Get public IP
PUBLIC_IP=$(az vm show -d \
  --name "$VM_NAME" \
  --resource-group "$RG_NAME" \
  --query publicIps -o tsv)

echo "✅ VM Public IP: $PUBLIC_IP"

# Create Network Security Group rules
echo ""
echo "🔒 Configuring Network Security Group..."

# Allow HTTPS (443)
az vm open-port \
  --resource-group "$RG_NAME" \
  --name "$VM_NAME" \
  --port 443 \
  --priority 1000 \
  --output none

echo "  ✅ Port 443 (HTTPS) opened"

# Allow Media (8445)
az vm open-port \
  --resource-group "$RG_NAME" \
  --name "$VM_NAME" \
  --port 8445 \
  --priority 1001 \
  --output none

echo "  ✅ Port 8445 (Media) opened"

# Allow RDP (3389)
az vm open-port \
  --resource-group "$RG_NAME" \
  --name "$VM_NAME" \
  --port 3389 \
  --priority 1002 \
  --output none

echo "  ✅ Port 3389 (RDP) opened"

# Wait for VM to be fully ready
echo ""
echo "⏳ Waiting for VM to be fully ready (30 seconds)..."
sleep 30

# Run deployment script on VM
echo ""
echo "📦 Deploying bot to VM..."
echo "This will take 5-10 minutes..."
echo ""

az vm run-command invoke \
  --resource-group "$RG_NAME" \
  --name "$VM_NAME" \
  --command-id RunPowerShellScript \
  --scripts @deploy-production.ps1 \
  --output none

echo "✅ Deployment script completed"

# Get deployment logs
echo ""
echo "📋 Checking deployment status..."
LOGS=$(az vm run-command invoke \
  --resource-group "$RG_NAME" \
  --name "$VM_NAME" \
  --command-id RunPowerShellScript \
  --scripts "Get-Content C:\teams-bot-poc\logs\service-output.log -Tail 20 -ErrorAction SilentlyContinue" \
  --query 'value[0].message' -o tsv 2>/dev/null || echo "Logs not yet available")

if [ ! -z "$LOGS" ]; then
    echo "$LOGS"
fi

# Summary
echo ""
echo "========================================"
echo "✅ DEPLOYMENT COMPLETE!"
echo "========================================"
echo ""
echo "📊 VM Details:"
echo "  Name: $VM_NAME"
echo "  Public IP: $PUBLIC_IP"
echo "  Admin User: $ADMIN_USER"
echo "  Admin Password: $ADMIN_PASS"
echo ""
echo "🌐 RDP Connection:"
echo "  1. Open Microsoft Remote Desktop (Mac)"
echo "  2. Add PC: $PUBLIC_IP"
echo "  3. Username: $ADMIN_USER"
echo "  4. Password: $ADMIN_PASS"
echo ""
echo "📝 DNS Configuration Required:"
echo "  Go to your DNS provider for qmachina.com and create:"
echo ""
echo "  Record 1:"
echo "    Type: A"
echo "    Name: teamsbot"
echo "    Value: $PUBLIC_IP"
echo "    TTL: 300"
echo ""
echo "  Record 2:"
echo "    Type: A"
echo "    Name: media"
echo "    Value: $PUBLIC_IP"
echo "    TTL: 300"
echo ""
echo "🔒 SSL Certificates Required:"
echo "  You need SSL certificates for:"
echo "    1. teamsbot.qmachina.com"
echo "    2. media.qmachina.com"
echo ""
echo "  Options:"
echo "    A) Use existing *.qmachina.com wildcard cert"
echo "    B) Get Let's Encrypt certs (free)"
echo "    C) Purchase from CA"
echo ""
echo "📖 Next Steps:"
echo "  1. Create DNS records (see above)"
echo "  2. Wait for DNS to propagate (5-15 minutes)"
echo "  3. Get/install SSL certificates"
echo "  4. RDP to VM and update cert thumbprint in appsettings.json"
echo "  5. Restart service: Restart-Service TeamsMediaBot"
echo "  6. Update Azure Bot webhook:"
echo "     https://teamsbot.qmachina.com/api/calling"
echo ""
echo "💰 Estimated Cost:"
echo "  VM (D4s_v3): ~$140/month"
echo "  Speech Service: ~$1-5/month"
echo "  Total: ~$145/month"
echo ""
echo "🛑 To delete everything:"
echo "  az vm delete --name $VM_NAME --resource-group $RG_NAME --yes"
echo ""
echo "📚 See ARCHITECTURE-PRODUCTION.md for complete guide"
echo ""
