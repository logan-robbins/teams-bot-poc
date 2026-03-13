// =============================================================================
// teams-bot-poc infrastructure — exported from Azure 2026-03-13, cleaned up
// Usage: az deployment group create -g rg-teams-media-bot-poc -f azure-export.bicep
// =============================================================================

// --- Parameters ---
param location string = 'eastus'

param accounts_aoai_talestral_poc_name string = 'aoai-talestral-poc'
param accounts_speech_teams_bot_poc_name string = 'speech-teams-bot-poc'
param containerapps_ca_talestral_api_name string = 'ca-talestral-api'
param containerapps_ca_talestral_ui_name string = 'ca-talestral-ui'
param managedEnvironments_cae_talestral_poc_name string = 'cae-talestral-poc'
param networkInterfaces_vm_tbot_prodVMNic_name string = 'vm-tbot-prodVMNic'
param networkInterfaces_vm_teams_bot_prodVMNic_name string = 'vm-teams-bot-prodVMNic'
param networkSecurityGroups_vm_tbot_prodNSG_name string = 'vm-tbot-prodNSG'
param networkSecurityGroups_vm_teams_bot_prodNSG_name string = 'vm-teams-bot-prodNSG'
param publicIPAddresses_vm_tbot_prodPublicIP_name string = 'vm-tbot-prodPublicIP'
param publicIPAddresses_vm_teams_bot_prodPublicIP_name string = 'vm-teams-bot-prodPublicIP'
param registries_caf4a2db536cacr_name string = 'caf4a2db536cacr'
param vaults_vault832_name string = 'vault832'
param virtualMachines_vm_tbot_prod_name string = 'vm-tbot-prod'
param virtualNetworks_vm_teams_bot_prodVNET_name string = 'vm-teams-bot-prodVNET'

@secure()
param azureOpenAIKey string

@secure()
param containerRegistryPassword string

// --- Container Apps Environment ---
resource managedEnvironments_cae_talestral_poc_name_resource 'Microsoft.App/managedEnvironments@2025-10-02-preview' = {
  location: 'East US'
  name: managedEnvironments_cae_talestral_poc_name
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: '265602f5-9d01-4dfe-869a-6d4df75ebed2'
        dynamicJsonColumns: false
      }
    }
    customDomainConfiguration: {}
    daprConfiguration: {}
    kedaConfiguration: {}
    peerAuthentication: {
      mtls: { enabled: false }
    }
    peerTrafficConfiguration: {
      encryption: { enabled: false }
    }
    publicNetworkAccess: 'Enabled'
    workloadProfiles: [
      {
        enableFips: false
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    zoneRedundant: false
  }
}

// --- Cognitive Services ---
resource accounts_aoai_talestral_poc_name_resource 'Microsoft.CognitiveServices/accounts@2025-10-01-preview' = {
  kind: 'OpenAI'
  location: location
  name: accounts_aoai_talestral_poc_name
  properties: {
    allowProjectManagement: false
    apiProperties: {}
    customSubDomainName: accounts_aoai_talestral_poc_name
    publicNetworkAccess: 'Enabled'
  }
  sku: { name: 'S0' }
}

resource accounts_speech_teams_bot_poc_name_resource 'Microsoft.CognitiveServices/accounts@2025-10-01-preview' = {
  kind: 'SpeechServices'
  location: location
  name: accounts_speech_teams_bot_poc_name
  properties: {
    allowProjectManagement: false
    publicNetworkAccess: 'Enabled'
  }
  sku: { name: 'S0' }
}

resource accounts_aoai_talestral_poc_name_Default 'Microsoft.CognitiveServices/accounts/defenderForAISettings@2025-10-01-preview' = {
  parent: accounts_aoai_talestral_poc_name_resource
  name: 'Default'
  properties: { state: 'Disabled' }
}

resource accounts_aoai_talestral_poc_name_Microsoft_Default 'Microsoft.CognitiveServices/accounts/raiPolicies@2025-10-01-preview' = {
  parent: accounts_aoai_talestral_poc_name_resource
  name: 'Microsoft.Default'
  properties: {
    contentFilters: [
      { action: 'NONE', blocking: true, enabled: true, name: 'Hate', severityThreshold: 'Medium', source: 'Prompt' }
      { action: 'NONE', blocking: true, enabled: true, name: 'Hate', severityThreshold: 'Medium', source: 'Completion' }
      { action: 'NONE', blocking: true, enabled: true, name: 'Sexual', severityThreshold: 'Medium', source: 'Prompt' }
      { action: 'NONE', blocking: true, enabled: true, name: 'Sexual', severityThreshold: 'Medium', source: 'Completion' }
      { action: 'NONE', blocking: true, enabled: true, name: 'Violence', severityThreshold: 'Medium', source: 'Prompt' }
      { action: 'NONE', blocking: true, enabled: true, name: 'Violence', severityThreshold: 'Medium', source: 'Completion' }
      { action: 'NONE', blocking: true, enabled: true, name: 'Selfharm', severityThreshold: 'Medium', source: 'Prompt' }
      { action: 'NONE', blocking: true, enabled: true, name: 'Selfharm', severityThreshold: 'Medium', source: 'Completion' }
    ]
    mode: 'Blocking'
  }
}

resource accounts_aoai_talestral_poc_name_Microsoft_DefaultV2 'Microsoft.CognitiveServices/accounts/raiPolicies@2025-10-01-preview' = {
  parent: accounts_aoai_talestral_poc_name_resource
  name: 'Microsoft.DefaultV2'
  properties: {
    contentFilters: [
      { action: 'NONE', blocking: true, enabled: true, name: 'Hate', severityThreshold: 'Medium', source: 'Prompt' }
      { action: 'NONE', blocking: true, enabled: true, name: 'Hate', severityThreshold: 'Medium', source: 'Completion' }
      { action: 'NONE', blocking: true, enabled: true, name: 'Sexual', severityThreshold: 'Medium', source: 'Prompt' }
      { action: 'NONE', blocking: true, enabled: true, name: 'Sexual', severityThreshold: 'Medium', source: 'Completion' }
      { action: 'NONE', blocking: true, enabled: true, name: 'Violence', severityThreshold: 'Medium', source: 'Prompt' }
      { action: 'NONE', blocking: true, enabled: true, name: 'Violence', severityThreshold: 'Medium', source: 'Completion' }
      { action: 'NONE', blocking: true, enabled: true, name: 'Selfharm', severityThreshold: 'Medium', source: 'Prompt' }
      { action: 'NONE', blocking: true, enabled: true, name: 'Selfharm', severityThreshold: 'Medium', source: 'Completion' }
      { action: 'NONE', blocking: true, enabled: true, name: 'Jailbreak', source: 'Prompt' }
      { action: 'NONE', blocking: true, enabled: true, name: 'Protected Material Text', source: 'Completion' }
      { action: 'NONE', blocking: false, enabled: true, name: 'Protected Material Code', source: 'Completion' }
    ]
    mode: 'Blocking'
  }
}

// --- Container Registry ---
resource registries_caf4a2db536cacr_name_resource 'Microsoft.ContainerRegistry/registries@2025-11-01' = {
  location: location
  name: registries_caf4a2db536cacr_name
  properties: {
    adminUserEnabled: true
    anonymousPullEnabled: false
    dataEndpointEnabled: false
    encryption: { status: 'disabled' }
    networkRuleBypassAllowedForTasks: false
    networkRuleBypassOptions: 'AzureServices'
    policies: {
      azureADAuthenticationAsArmPolicy: { status: 'enabled' }
      exportPolicy: { status: 'enabled' }
      quarantinePolicy: { status: 'disabled' }
      retentionPolicy: { days: 7, status: 'disabled' }
      trustPolicy: { status: 'disabled', type: 'Notary' }
    }
    publicNetworkAccess: 'Enabled'
    roleAssignmentMode: 'LegacyRegistryPermissions'
    zoneRedundancy: 'Disabled'
  }
  sku: { name: 'Basic' }
}

resource registries_caf4a2db536cacr_name_repositories_admin 'Microsoft.ContainerRegistry/registries/scopeMaps@2025-11-01' = {
  parent: registries_caf4a2db536cacr_name_resource
  name: '_repositories_admin'
  properties: {
    actions: [
      'repositories/*/metadata/read'
      'repositories/*/metadata/write'
      'repositories/*/content/read'
      'repositories/*/content/write'
      'repositories/*/content/delete'
    ]
    description: 'Can perform all read, write and delete operations on the registry'
  }
}

resource registries_caf4a2db536cacr_name_repositories_pull 'Microsoft.ContainerRegistry/registries/scopeMaps@2025-11-01' = {
  parent: registries_caf4a2db536cacr_name_resource
  name: '_repositories_pull'
  properties: {
    actions: [ 'repositories/*/content/read' ]
    description: 'Can pull any repository of the registry'
  }
}

resource registries_caf4a2db536cacr_name_repositories_pull_metadata_read 'Microsoft.ContainerRegistry/registries/scopeMaps@2025-11-01' = {
  parent: registries_caf4a2db536cacr_name_resource
  name: '_repositories_pull_metadata_read'
  properties: {
    actions: [ 'repositories/*/content/read', 'repositories/*/metadata/read' ]
    description: 'Can perform all read operations on the registry'
  }
}

resource registries_caf4a2db536cacr_name_repositories_push 'Microsoft.ContainerRegistry/registries/scopeMaps@2025-11-01' = {
  parent: registries_caf4a2db536cacr_name_resource
  name: '_repositories_push'
  properties: {
    actions: [ 'repositories/*/content/read', 'repositories/*/content/write' ]
    description: 'Can push to any repository of the registry'
  }
}

resource registries_caf4a2db536cacr_name_repositories_push_metadata_write 'Microsoft.ContainerRegistry/registries/scopeMaps@2025-11-01' = {
  parent: registries_caf4a2db536cacr_name_resource
  name: '_repositories_push_metadata_write'
  properties: {
    actions: [
      'repositories/*/metadata/read'
      'repositories/*/metadata/write'
      'repositories/*/content/read'
      'repositories/*/content/write'
    ]
    description: 'Can perform all read and write operations on the registry'
  }
}

// --- Network Security Groups (rules defined inline only, no child resources) ---
resource networkSecurityGroups_vm_tbot_prodNSG_name_resource 'Microsoft.Network/networkSecurityGroups@2024-07-01' = {
  location: location
  name: networkSecurityGroups_vm_tbot_prodNSG_name
  properties: {
    securityRules: [
      {
        name: 'open-port-443'
        properties: {
          access: 'Allow'
          destinationAddressPrefix: '*'
          destinationPortRange: '443'
          direction: 'Inbound'
          priority: 1000
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
        }
      }
      {
        name: 'open-port-8445'
        properties: {
          access: 'Allow'
          destinationAddressPrefix: '*'
          destinationPortRange: '8445'
          direction: 'Inbound'
          priority: 1001
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
        }
      }
      {
        name: 'open-port-3389'
        properties: {
          access: 'Allow'
          destinationAddressPrefix: '*'
          destinationPortRange: '3389'
          direction: 'Inbound'
          priority: 1002
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
        }
      }
      {
        name: 'port-6516'
        properties: {
          access: 'Allow'
          destinationAddressPrefix: '*'
          destinationPortRange: '6516'
          direction: 'Inbound'
          priority: 1012
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
        }
      }
      {
        name: 'port-9443'
        properties: {
          access: 'Allow'
          destinationAddressPrefix: '*'
          destinationPortRange: '9443'
          direction: 'Inbound'
          priority: 1022
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
        }
      }
    ]
  }
}

resource networkSecurityGroups_vm_teams_bot_prodNSG_name_resource 'Microsoft.Network/networkSecurityGroups@2024-07-01' = {
  location: location
  name: networkSecurityGroups_vm_teams_bot_prodNSG_name
  properties: { securityRules: [] }
}

// --- Public IPs ---
resource publicIPAddresses_vm_tbot_prodPublicIP_name_resource 'Microsoft.Network/publicIPAddresses@2024-07-01' = {
  location: location
  name: publicIPAddresses_vm_tbot_prodPublicIP_name
  properties: {
    idleTimeoutInMinutes: 4
    publicIPAddressVersion: 'IPv4'
    publicIPAllocationMethod: 'Static'
  }
  sku: { name: 'Standard', tier: 'Regional' }
}

resource publicIPAddresses_vm_teams_bot_prodPublicIP_name_resource 'Microsoft.Network/publicIPAddresses@2024-07-01' = {
  location: location
  name: publicIPAddresses_vm_teams_bot_prodPublicIP_name
  properties: {
    idleTimeoutInMinutes: 4
    publicIPAddressVersion: 'IPv4'
    publicIPAllocationMethod: 'Static'
  }
  sku: { name: 'Standard', tier: 'Regional' }
}

// --- Virtual Network (subnet defined inline only, no child resource) ---
resource virtualNetworks_vm_teams_bot_prodVNET_name_resource 'Microsoft.Network/virtualNetworks@2024-07-01' = {
  location: location
  name: virtualNetworks_vm_teams_bot_prodVNET_name
  properties: {
    addressSpace: {
      addressPrefixes: [ '10.0.0.0/16' ]
    }
    enableDdosProtection: false
    subnets: [
      {
        name: 'vm-teams-bot-prodSubnet'
        properties: {
          addressPrefix: '10.0.0.0/24'
          privateEndpointNetworkPolicies: 'Disabled'
          privateLinkServiceNetworkPolicies: 'Enabled'
        }
      }
    ]
  }
}

// --- Recovery Services Vault ---
resource vaults_vault832_name_resource 'Microsoft.RecoveryServices/vaults@2025-08-01' = {
  location: location
  name: vaults_vault832_name
  properties: {
    publicNetworkAccess: 'Enabled'
    redundancySettings: {
      crossRegionRestore: 'Disabled'
      standardTierStorageRedundancy: 'GeoRedundant'
    }
    restoreSettings: {
      crossSubscriptionRestoreSettings: { crossSubscriptionRestoreState: 'Enabled' }
    }
    securitySettings: {
      softDeleteSettings: {
        enhancedSecurityState: 'Enabled'
        softDeleteRetentionPeriodInDays: 14
        softDeleteState: 'Enabled'
      }
      sourceScanConfiguration: { state: 'Disabled' }
    }
  }
  sku: { name: 'RS0', tier: 'Standard' }
}

resource vaults_vault832_name_DefaultPolicy 'Microsoft.RecoveryServices/vaults/backupPolicies@2025-08-01' = {
  parent: vaults_vault832_name_resource
  name: 'DefaultPolicy'
  properties: {
    backupManagementType: 'AzureIaasVM'
    instantRPDetails: {}
    instantRpRetentionRangeInDays: 2
    policyType: 'V1'
    protectedItemsCount: 0
    retentionPolicy: {
      dailySchedule: {
        retentionDuration: { count: 30, durationType: 'Days' }
        retentionTimes: [ '2026-01-30T16:00:00Z' ]
      }
      retentionPolicyType: 'LongTermRetentionPolicy'
    }
    schedulePolicy: {
      schedulePolicyType: 'SimpleSchedulePolicy'
      scheduleRunFrequency: 'Daily'
      scheduleRunTimes: [ '2026-01-30T16:00:00Z' ]
      scheduleWeeklyFrequency: 0
    }
    timeZone: 'UTC'
  }
}

resource vaults_vault832_name_defaultAlertSetting 'Microsoft.RecoveryServices/vaults/replicationAlertSettings@2025-08-01' = {
  parent: vaults_vault832_name_resource
  name: 'defaultAlertSetting'
  properties: {
    customEmailAddresses: []
    sendToOwners: 'DoNotSend'
  }
}

// --- Managed Certificates ---
resource managedEnvironments_cae_talestral_poc_name_mc_agent 'Microsoft.App/managedEnvironments/managedCertificates@2025-10-02-preview' = {
  parent: managedEnvironments_cae_talestral_poc_name_resource
  location: 'East US'
  name: 'mc-cae-talestral--agent-qmachina-c-2151'
  properties: {
    domainControlValidation: 'CNAME'
    subjectName: 'agent.qmachina.com'
  }
}

resource managedEnvironments_cae_talestral_poc_name_mc_interview 'Microsoft.App/managedEnvironments/managedCertificates@2025-10-02-preview' = {
  parent: managedEnvironments_cae_talestral_poc_name_resource
  location: 'East US'
  name: 'mc-cae-talestral--interview-qmachi-6822'
  properties: {
    domainControlValidation: 'CNAME'
    subjectName: 'interview.qmachina.com'
  }
}

// --- Container Apps ---
resource containerapps_ca_talestral_api_name_resource 'Microsoft.App/containerapps@2025-10-02-preview' = {
  identity: { type: 'None' }
  location: 'East US'
  name: containerapps_ca_talestral_api_name
  properties: {
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        allowInsecure: false
        customDomains: [
          {
            bindingType: 'SniEnabled'
            certificateId: managedEnvironments_cae_talestral_poc_name_mc_agent.id
            name: 'agent.qmachina.com'
          }
        ]
        external: true
        targetPort: 8765
        traffic: [ { latestRevision: true, weight: 100 } ]
        transport: 'Auto'
      }
      registries: [
        {
          passwordSecretRef: 'acr-password'
          server: '${registries_caf4a2db536cacr_name}.azurecr.io'
          username: registries_caf4a2db536cacr_name
        }
      ]
      secrets: [
        { name: 'acr-password', value: containerRegistryPassword }
      ]
    }
    environmentId: managedEnvironments_cae_talestral_poc_name_resource.id
    managedEnvironmentId: managedEnvironments_cae_talestral_poc_name_resource.id
    template: {
      containers: [
        {
          env: [
            { name: 'AZURE_OPENAI_ENDPOINT', value: 'https://${accounts_aoai_talestral_poc_name}.openai.azure.com/' }
            { name: 'AZURE_OPENAI_KEY', value: azureOpenAIKey }
            { name: 'AZURE_OPENAI_DEPLOYMENT', value: 'gpt-5-mini' }
            { name: 'OPENAI_API_TYPE', value: 'azure' }
            { name: 'OPENAI_REASONING_EFFORT', value: 'low' }
          ]
          image: '${registries_caf4a2db536cacr_name}.azurecr.io/${containerapps_ca_talestral_api_name}:latest'
          name: containerapps_ca_talestral_api_name
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
      scale: {
        cooldownPeriod: 300
        maxReplicas: 10
        pollingInterval: 30
      }
    }
    workloadProfileName: 'Consumption'
  }
}

resource containerapps_ca_talestral_ui_name_resource 'Microsoft.App/containerapps@2025-10-02-preview' = {
  identity: { type: 'None' }
  location: 'East US'
  name: containerapps_ca_talestral_ui_name
  properties: {
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        allowInsecure: false
        customDomains: [
          {
            bindingType: 'SniEnabled'
            certificateId: managedEnvironments_cae_talestral_poc_name_mc_interview.id
            name: 'interview.qmachina.com'
          }
        ]
        external: true
        targetPort: 8501
        traffic: [ { latestRevision: true, weight: 100 } ]
        transport: 'Auto'
      }
      registries: [
        {
          passwordSecretRef: 'acr-password'
          server: '${registries_caf4a2db536cacr_name}.azurecr.io'
          username: registries_caf4a2db536cacr_name
        }
      ]
      secrets: [
        { name: 'acr-password', value: containerRegistryPassword }
      ]
    }
    environmentId: managedEnvironments_cae_talestral_poc_name_resource.id
    managedEnvironmentId: managedEnvironments_cae_talestral_poc_name_resource.id
    template: {
      containers: [
        {
          env: [
            { name: 'SINK_URL', value: 'https://agent.qmachina.com' }
          ]
          image: '${registries_caf4a2db536cacr_name}.azurecr.io/${containerapps_ca_talestral_ui_name}:latest'
          name: containerapps_ca_talestral_ui_name
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
        }
      ]
      scale: {
        cooldownPeriod: 300
        maxReplicas: 2
        minReplicas: 0
        pollingInterval: 30
      }
    }
    workloadProfileName: 'Consumption'
  }
}

// --- Network Interfaces ---
resource networkInterfaces_vm_tbot_prodVMNic_name_resource 'Microsoft.Network/networkInterfaces@2024-07-01' = {
  kind: 'Regular'
  location: location
  name: networkInterfaces_vm_tbot_prodVMNic_name
  properties: {
    enableAcceleratedNetworking: false
    enableIPForwarding: false
    ipConfigurations: [
      {
        name: 'ipconfigvm-tbot-prod'
        properties: {
          primary: true
          privateIPAddressVersion: 'IPv4'
          privateIPAllocationMethod: 'Dynamic'
          publicIPAddress: {
            id: publicIPAddresses_vm_tbot_prodPublicIP_name_resource.id
          }
          subnet: {
            id: '${virtualNetworks_vm_teams_bot_prodVNET_name_resource.id}/subnets/vm-teams-bot-prodSubnet'
          }
        }
      }
    ]
    networkSecurityGroup: {
      id: networkSecurityGroups_vm_tbot_prodNSG_name_resource.id
    }
  }
}

resource networkInterfaces_vm_teams_bot_prodVMNic_name_resource 'Microsoft.Network/networkInterfaces@2024-07-01' = {
  kind: 'Regular'
  location: location
  name: networkInterfaces_vm_teams_bot_prodVMNic_name
  properties: {
    enableAcceleratedNetworking: false
    enableIPForwarding: false
    ipConfigurations: [
      {
        name: 'ipconfigvm-teams-bot-prod'
        properties: {
          primary: true
          privateIPAddressVersion: 'IPv4'
          privateIPAllocationMethod: 'Dynamic'
          publicIPAddress: {
            id: publicIPAddresses_vm_teams_bot_prodPublicIP_name_resource.id
          }
          subnet: {
            id: '${virtualNetworks_vm_teams_bot_prodVNET_name_resource.id}/subnets/vm-teams-bot-prodSubnet'
          }
        }
      }
    ]
    networkSecurityGroup: {
      id: networkSecurityGroups_vm_teams_bot_prodNSG_name_resource.id
    }
  }
}

// --- Virtual Machine ---
resource virtualMachines_vm_tbot_prod_name_resource 'Microsoft.Compute/virtualMachines@2025-04-01' = {
  location: location
  name: virtualMachines_vm_tbot_prod_name
  properties: {
    hardwareProfile: { vmSize: 'Standard_D4s_v3' }
    networkProfile: {
      networkInterfaces: [
        { id: networkInterfaces_vm_tbot_prodVMNic_name_resource.id }
      ]
    }
    osProfile: {
      adminUsername: 'azureuser'
      allowExtensionOperations: true
      computerName: virtualMachines_vm_tbot_prod_name
      windowsConfiguration: {
        enableAutomaticUpdates: true
        patchSettings: {
          assessmentMode: 'ImageDefault'
          patchMode: 'AutomaticByOS'
        }
        provisionVMAgent: true
      }
    }
    securityProfile: {
      securityType: 'TrustedLaunch'
      uefiSettings: { secureBootEnabled: true, vTpmEnabled: true }
    }
    storageProfile: {
      imageReference: {
        offer: 'WindowsServer'
        publisher: 'MicrosoftWindowsServer'
        sku: '2022-datacenter-g2'
        version: 'latest'
      }
      osDisk: {
        caching: 'ReadWrite'
        createOption: 'FromImage'
        deleteOption: 'Detach'
        name: '${virtualMachines_vm_tbot_prod_name}_OsDisk'
        osType: 'Windows'
      }
    }
  }
}

resource virtualMachines_vm_tbot_prod_name_enablevmAccess 'Microsoft.Compute/virtualMachines/extensions@2025-04-01' = {
  parent: virtualMachines_vm_tbot_prod_name_resource
  location: location
  name: 'enablevmAccess'
  properties: {
    autoUpgradeMinorVersion: true
    publisher: 'Microsoft.Compute'
    settings: { userName: 'azureuser' }
    type: 'VMAccessAgent'
    typeHandlerVersion: '2.0'
  }
}
