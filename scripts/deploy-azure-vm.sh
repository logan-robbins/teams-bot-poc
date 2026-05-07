#!/bin/bash
# Canonical Alfred VM finalization path.
# Runs each VM phase as a named Azure managed Run Command resource. Managed
# Run Command is the documented Azure path for VM bootstrap/deployment scripts
# because it supports explicit timeouts, protected parameters, status polling,
# and deletion of an in-progress command.

set -Eeuo pipefail
trap 'echo "ERROR: deploy-azure-vm.sh failed at line $LINENO." >&2' ERR
trap 'status=$?; echo "deploy-azure-vm.sh exit status: $status"' EXIT

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RG_NAME="${RG_NAME:-rg-alfred-poc}"
VM_NAME="${VM_NAME:-vm-alfred}"
PROJECT_ROOT_WINDOWS="${PROJECT_ROOT_WINDOWS:-C:\\teams-bot-poc}"
CONFIG_PATH_WINDOWS="${CONFIG_PATH_WINDOWS:-C:\\teams-bot-poc\\src\\Config\\appsettings.production.json}"
REPO_URL="${REPO_URL:-git@github.com:logan-robbins/alfred-teams-bot.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
DEPLOY_KEY_FILE="${DEPLOY_KEY_FILE:-/tmp/alfred-deploy-key}"
ADMIN_USER="${ADMIN_USER:-azureuser}"
TENANT_ID="${TENANT_ID:-2843abed-8970-461e-a260-a59dc1398dbf}"
APP_SECRET_FILE="${APP_SECRET_FILE:-/tmp/app-secret.json}"
VM_ADMIN_PASS_FILE="${VM_ADMIN_PASS_FILE:-/tmp/vm-admin-pass.txt}"
SPEECH_KEY_FILE="${SPEECH_KEY_FILE:-/tmp/speech-key.txt}"
FASTAPI_APP_NAME="${FASTAPI_APP_NAME:-ca-alfred-api}"
BOT_HOSTNAME="${BOT_HOSTNAME:-alfred-disney-bot.eastus.cloudapp.azure.com}"
MEDIA_HOSTNAME="${MEDIA_HOSTNAME:-alfred-disney-bot.eastus.cloudapp.azure.com}"
STT_PROVIDER="${STT_PROVIDER:-AzureSpeech}"
AZURE_SPEECH_REGION="${AZURE_SPEECH_REGION:-eastus}"
AZURE_SPEECH_LANGUAGE="${AZURE_SPEECH_LANGUAGE:-en-US}"
BOT_LISTEN_PORT="${BOT_LISTEN_PORT:-443}"
MEDIA_PORT="${MEDIA_PORT:-8445}"
VM_READY_TIMEOUT_SECONDS="${VM_READY_TIMEOUT_SECONDS:-900}"
SKIP_REPO_SYNC="${SKIP_REPO_SYNC:-1}"

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "Required command '$1' was not found."
}

read_required_file() {
    local path="$1"
    local label="$2"
    local value

    [ -s "$path" ] || fail "$label file is missing or empty at $path."
    IFS= read -r value < "$path"
    [ -n "$value" ] || fail "$label value is empty."
    printf '%s' "$value"
}

assert_single_line_secret() {
    local label="$1"
    local value="$2"

    case "$value" in
        *$'\n'*|*$'\r'*)
            fail "$label must be a single-line value."
            ;;
    esac
}

ensure_boot_diagnostics_enabled() {
    local current
    current="$(az vm show -g "$RG_NAME" -n "$VM_NAME" --query "diagnosticsProfile.bootDiagnostics.enabled" -o tsv 2>/dev/null || echo "")"
    if [ "$current" != "true" ]; then
        echo "Enabling boot diagnostics on $VM_NAME (managed storage)..."
        az vm boot-diagnostics enable -g "$RG_NAME" -n "$VM_NAME" --output none
    fi
}

ensure_recovery_nsg_rules() {
    local nic_id nsg_id nsg_name entry rule_name port priority
    nic_id="$(az vm show -g "$RG_NAME" -n "$VM_NAME" --query "networkProfile.networkInterfaces[0].id" -o tsv)"
    nsg_id="$(az network nic show --ids "$nic_id" --query "networkSecurityGroup.id" -o tsv 2>/dev/null || true)"
    [ -n "$nsg_id" ] || { echo "WARN: VM has no NSG; skipping recovery rules"; return; }
    nsg_name="${nsg_id##*/}"

    for entry in "AllowSSH:22:1005" "AllowWinRMHTTPS:5986:1004"; do
        IFS=: read -r rule_name port priority <<< "$entry"
        if ! az network nsg rule show -g "$RG_NAME" --nsg-name "$nsg_name" --name "$rule_name" >/dev/null 2>&1; then
            echo "Adding recovery NSG rule $rule_name (port $port)..."
            az network nsg rule create -g "$RG_NAME" --nsg-name "$nsg_name" --name "$rule_name" \
                --priority "$priority" --direction Inbound --access Allow --protocol Tcp \
                --destination-port-ranges "$port" --source-address-prefixes "*" --output none
        fi
    done
}

probe_agent_via_run_command() {
    local probe_name="canonical-agent-health-probe"
    az vm run-command delete -g "$RG_NAME" --vm-name "$VM_NAME" --run-command-name "$probe_name" --yes >/dev/null 2>&1 || true
    if az vm run-command create -g "$RG_NAME" --vm-name "$VM_NAME" --run-command-name "$probe_name" \
        --location "$VM_LOCATION" --script 'Write-Host probe-ok' --async-execution false \
        --timeout-in-seconds 60 --output none >/dev/null 2>&1; then
        az vm run-command delete -g "$RG_NAME" --vm-name "$VM_NAME" --run-command-name "$probe_name" --yes --output none >/dev/null 2>&1 || true
        return 0
    fi
    return 1
}

wait_for_vm_ready() {
    local timeout_seconds="$1"
    local deadline
    local state

    deadline=$((SECONDS + timeout_seconds))

    echo "Waiting for $VM_NAME provisioning state to settle..."
    while [ "$SECONDS" -lt "$deadline" ]; do
        state="$(az vm show --resource-group "$RG_NAME" --name "$VM_NAME" --query provisioningState -o tsv)"
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $VM_NAME provisioningState=$state"
        if [ "$state" = "Succeeded" ]; then
            return
        fi

        sleep 15
    done

    echo "VM did not settle before timeout. Probing agent directly via Run Command (instance view can lag 5-10 min)..."
    if probe_agent_via_run_command; then
        echo "Agent responded to probe; treating instance view as stale and continuing."
        return
    fi

    echo "Agent probe failed. Reapplying VM goal state per Microsoft VM extension troubleshooting guidance..."
    az vm reapply --resource-group "$RG_NAME" --name "$VM_NAME" --no-wait --output none

    deadline=$((SECONDS + timeout_seconds))
    while [ "$SECONDS" -lt "$deadline" ]; do
        state="$(az vm show --resource-group "$RG_NAME" --name "$VM_NAME" --query provisioningState -o tsv)"
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $VM_NAME provisioningState=$state"
        if [ "$state" = "Succeeded" ]; then
            return
        fi

        sleep 15
    done

    if probe_agent_via_run_command; then
        echo "Agent responded to probe after reapply; continuing despite Updating provisioningState."
        return
    fi

    fail "VM provisioning state did not reach Succeeded after reapply and agent did not respond to Run Command probe."
}

delete_managed_run_command_if_present() {
    local run_command_name="$1"

    if az vm run-command show \
        --resource-group "$RG_NAME" \
        --vm-name "$VM_NAME" \
        --run-command-name "$run_command_name" >/dev/null 2>&1; then
        echo "Deleting existing managed Run Command resource: $run_command_name"
        az vm run-command delete \
            --resource-group "$RG_NAME" \
            --vm-name "$VM_NAME" \
            --run-command-name "$run_command_name" \
            --yes \
            --output none
    fi
}

run_vm_script_file() {
    local label="$1"
    local run_command_name="$2"
    local timeout_seconds="$3"
    local script_path="$4"
    shift 4
    local parameters=("$@")
    local public_parameters=()
    local protected_parameters=()
    local parameter_mode="public"
    local create_args=()
    local status_json
    local execution_state
    local exit_code
    local output
    local error

    echo ""
    echo "==> $label"

    [ -s "$script_path" ] || fail "Script file not found or empty: $script_path"

    if [ "${#parameters[@]}" -gt 0 ]; then
        for parameter in "${parameters[@]}"; do
            if [ "$parameter" = "--protected" ]; then
                parameter_mode="protected"
                continue
            fi

            if [ "$parameter_mode" = "protected" ]; then
                protected_parameters+=("$parameter")
            else
                public_parameters+=("$parameter")
            fi
        done
    fi

    delete_managed_run_command_if_present "$run_command_name"

    create_args=(
        --resource-group "$RG_NAME"
        --vm-name "$VM_NAME"
        --run-command-name "$run_command_name"
        --location "$VM_LOCATION"
        --script "$(< "$script_path")"
        --async-execution false
        --timeout-in-seconds "$timeout_seconds"
        --output none
    )

    if [ "${#public_parameters[@]}" -gt 0 ]; then
        create_args+=(--parameters "${public_parameters[@]}")
    fi

    if [ "${#protected_parameters[@]}" -gt 0 ]; then
        create_args+=(--protected-parameters "${protected_parameters[@]}")
    fi

    az vm run-command create "${create_args[@]}"

    status_json="$(az vm run-command show \
        --resource-group "$RG_NAME" \
        --vm-name "$VM_NAME" \
        --run-command-name "$run_command_name" \
        --instance-view \
        -o json)"

    execution_state="$(jq -r '(.instanceView // .properties.instanceView // {}).executionState // empty' <<<"$status_json")"
    exit_code="$(jq -r '(.instanceView // .properties.instanceView // {}).exitCode // empty' <<<"$status_json")"
    output="$(jq -r '(.instanceView // .properties.instanceView // {}).output // empty' <<<"$status_json")"
    error="$(jq -r '(.instanceView // .properties.instanceView // {}).error // empty' <<<"$status_json")"

    if [ -n "$output" ]; then
        printf '%s\n' "$output"
    fi

    if [ -n "$error" ]; then
        printf '%s\n' "$error" >&2
    fi

    [ "$execution_state" = "Succeeded" ] || fail "$run_command_name execution state was '$execution_state'."
    [ -z "$exit_code" ] || [ "$exit_code" = "0" ] || fail "$run_command_name exit code was '$exit_code'."
}

require_dns_points_to_vm() {
    local hostname="$1"
    local expected_ip="$2"
    local actual_ip

    actual_ip="$(dig +short "$hostname" A | tail -n 1)"
    [ "$actual_ip" = "$expected_ip" ] || fail "$hostname resolves to '$actual_ip', expected '$expected_ip'. Update DNS before requesting the certificate."
}

require_command az
require_command jq
require_command dig
require_command curl

az account show >/dev/null || fail "Azure CLI is not logged in. Run 'az login' first."

[ -s "$APP_SECRET_FILE" ] || fail "App secret JSON not found at $APP_SECRET_FILE."
APP_ID="$(jq -r '.appId // empty' "$APP_SECRET_FILE")"
APP_SECRET="$(jq -r '.password // empty' "$APP_SECRET_FILE")"
[ -n "$APP_ID" ] || fail "No appId found in $APP_SECRET_FILE."
[ -n "$APP_SECRET" ] || fail "No password found in $APP_SECRET_FILE."

RUN_AS_PASSWORD="$(read_required_file "$VM_ADMIN_PASS_FILE" "VM admin password")"
AZURE_SPEECH_KEY="$(read_required_file "$SPEECH_KEY_FILE" "Azure Speech key")"
assert_single_line_secret "App secret" "$APP_SECRET"
assert_single_line_secret "VM admin password" "$RUN_AS_PASSWORD"
assert_single_line_secret "Azure Speech key" "$AZURE_SPEECH_KEY"

# Read deploy key (required for SSH-based private repo clone). Multi-line, so
# don't run it through assert_single_line_secret.
DEPLOY_KEY=""
if [ -s "$DEPLOY_KEY_FILE" ]; then
    DEPLOY_KEY="$(< "$DEPLOY_KEY_FILE")"
fi
case "$REPO_URL" in
    git@*|ssh://*)
        [ -n "$DEPLOY_KEY" ] || fail "REPO_URL='$REPO_URL' is SSH but DEPLOY_KEY_FILE='$DEPLOY_KEY_FILE' is empty or missing. Generate one with ssh-keygen, register the public half as a deploy key on the repo, and save the private half here."
        ;;
esac

echo "Checking target Azure resources..."
az vm show --resource-group "$RG_NAME" --name "$VM_NAME" >/dev/null || fail "VM '$VM_NAME' was not found in resource group '$RG_NAME'."

PUBLIC_IP="$(az vm show -d --resource-group "$RG_NAME" --name "$VM_NAME" --query publicIps -o tsv)"
[ -n "$PUBLIC_IP" ] || fail "Could not resolve public IP for $VM_NAME."
VM_LOCATION="$(az vm show --resource-group "$RG_NAME" --name "$VM_NAME" --query location -o tsv)"
[ -n "$VM_LOCATION" ] || fail "Could not resolve Azure location for $VM_NAME."

ensure_boot_diagnostics_enabled
ensure_recovery_nsg_rules

FASTAPI_FQDN="$(az containerapp show --name "$FASTAPI_APP_NAME" --resource-group "$RG_NAME" --query properties.configuration.ingress.fqdn -o tsv)"
[ -n "$FASTAPI_FQDN" ] || fail "Could not resolve Container App FQDN for $FASTAPI_APP_NAME."
BOOTSTRAP_CONSUMER_URL="https://$FASTAPI_FQDN/events"

require_dns_points_to_vm "$BOT_HOSTNAME" "$PUBLIC_IP"
require_dns_points_to_vm "$MEDIA_HOSTNAME" "$PUBLIC_IP"

wait_for_vm_ready "$VM_READY_TIMEOUT_SECONDS"
echo "$VM_NAME is ready for managed Run Command deployment."

BOOTSTRAP_PARAMETERS=(
    "ProjectRoot=$PROJECT_ROOT_WINDOWS"
    "ConfigPath=$CONFIG_PATH_WINDOWS"
    "RepoUrl=$REPO_URL"
    "RepoBranch=$REPO_BRANCH"
    "AppId=$APP_ID"
    "TenantId=$TENANT_ID"
    "NotificationUrl=https://$BOT_HOSTNAME/api/calling"
    "ServiceFqdn=$MEDIA_HOSTNAME"
    "InstancePublicIPAddress=$PUBLIC_IP"
    "BootstrapConsumerUrl=$BOOTSTRAP_CONSUMER_URL"
    "RunAsUser=$ADMIN_USER"
    "SttProvider=$STT_PROVIDER"
    "AzureSpeechRegion=$AZURE_SPEECH_REGION"
    "AzureSpeechRecognitionLanguage=$AZURE_SPEECH_LANGUAGE"
    "BotListenPort=$BOT_LISTEN_PORT"
    "MediaPort=$MEDIA_PORT"
    "BootstrapOnly=1"
    "SkipRepositorySync=$SKIP_REPO_SYNC"
    "--protected"
    "AppSecret=$APP_SECRET"
    "RunAsPassword=$RUN_AS_PASSWORD"
    "AzureSpeechKey=$AZURE_SPEECH_KEY"
)
if [ -n "$DEPLOY_KEY" ]; then
    BOOTSTRAP_PARAMETERS+=("DeployKey=$DEPLOY_KEY")
fi
echo "Managed Run Command phases prepared."

run_vm_script_file \
    "Phase 1/5: write production config and publish bot" \
    "alfred-bootstrap-config-publish" \
    1200 \
    "$SCRIPT_DIR/bootstrap-production-vm.ps1" \
    "${BOOTSTRAP_PARAMETERS[@]}"

run_vm_script_file "Phase 2/5: open Windows firewall ports" "alfred-open-firewall" 300 "$SCRIPT_DIR/vm-open-firewall.ps1"
run_vm_script_file "Phase 3/5: install win-acme" "alfred-install-win-acme" 900 "$SCRIPT_DIR/vm-install-win-acme.ps1"

# Build the unique host list for the cert. For deployments where bot and media
# share a single hostname (e.g. an Azure-managed FQDN), this collapses to one.
CERT_HOSTNAMES="$BOT_HOSTNAME"
if [ "$MEDIA_HOSTNAME" != "$BOT_HOSTNAME" ]; then
    CERT_HOSTNAMES="$BOT_HOSTNAME,$MEDIA_HOSTNAME"
fi
CERT_FRIENDLY_NAME="${CERT_FRIENDLY_NAME:-alfred-bot-cert}"
CERT_EMAIL="${CERT_EMAIL:-Logan.Robbins@disney.com}"

run_vm_script_file "Phase 4/5: request Let's Encrypt certificate" "alfred-request-letsencrypt-cert" 900 \
    "$SCRIPT_DIR/vm-request-letsencrypt-cert.ps1" \
    "RunAsUser=$ADMIN_USER" \
    "Hostnames=$CERT_HOSTNAMES" \
    "EmailAddress=$CERT_EMAIL" \
    "FriendlyName=$CERT_FRIENDLY_NAME"

FINALIZE_PARAMETERS=(
    "ProjectRoot=$PROJECT_ROOT_WINDOWS"
    "ConfigPath=$CONFIG_PATH_WINDOWS"
    "RunAsUser=$ADMIN_USER"
    "CertSubjectHosts=$CERT_HOSTNAMES"
    "CertFriendlyNamePattern=$CERT_FRIENDLY_NAME*"
    "--protected"
    "RunAsPassword=$RUN_AS_PASSWORD"
)

run_vm_script_file \
    "Phase 5/5: install and start TeamsMediaBot service" \
    "alfred-finalize-service" \
    600 \
    "$SCRIPT_DIR/vm-finalize-bootstrap.ps1" \
    "${FINALIZE_PARAMETERS[@]}"

echo ""
echo "Verifying public bot health..."
curl --fail --show-error --silent --max-time 30 "https://$BOT_HOSTNAME/api/calling/health"
echo ""
echo "VM deployment complete."
echo "Bot health URL: https://$BOT_HOSTNAME/api/calling/health"
echo "Bootstrap consumer URL: $BOOTSTRAP_CONSUMER_URL"
