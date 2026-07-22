// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

// This file creates all the resource group scope resources
targetScope = 'resourceGroup'

@description('Unique suffix')
param suffix string = uniqueString(resourceGroup().id)

@description('The location of the resources')
param location string = resourceGroup().location

@description('The name of the function app to use')
param appName string = 'debfnapp${suffix}'

@description('Using shared keys or managed identity')
param use_shared_keys bool = true

@description('Enable GPG signing of the repository Release file')
param signing_enabled bool = false

@description('ASCII-armored GPG private key used to sign the repository. Only used when signing_enabled is true.')
@secure()
param gpg_private_key string = ''

@description('ASCII-armored GPG public key, published for clients to verify against. Only used when signing_enabled is true.')
param gpg_public_key string = ''

// Storage account names must be between 3 and 24 characters, and unique, so
// generate a unique name.
@description('The name of the storage account to use')
param storage_account_name string = 'debianrepo${suffix}'

// Choose the package container name. This will be passed to the function app.
var package_container_name = 'packages'

// Create a container for the Python code
var python_container_name = 'python'

// Create a UAMI for the deployment script to access the storage account
resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: 'uami${suffix}'
  location: location
}

// Create a storage account for both package storage and function app storage
var common_storage_properties = {
  publicNetworkAccess: 'Enabled'
  allowBlobPublicAccess: false
  minimumTlsVersion: 'TLS1_2'
}
var storage_properties = use_shared_keys ? common_storage_properties : union(common_storage_properties, {
  allowSharedKeyAccess: false
})
resource storageAccount 'Microsoft.Storage/storageAccounts@2025-06-01' = {
  name: storage_account_name
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: storage_properties
}

// Create a container for the packages
resource defBlobServices 'Microsoft.Storage/storageAccounts/blobServices@2025-06-01' = {
  parent: storageAccount
  name: 'default'
}
resource packageContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-06-01' = {
  parent: defBlobServices
  name: package_container_name
  properties: {
  }
}
resource pythonContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-06-01' = {
  parent: defBlobServices
  name: python_container_name
  properties: {
  }
}

// Grant the UAMI Storage Blob Data Contributor on the storage account
@description('This is the built-in Storage Blob Data Contributor role. See https://learn.microsoft.com/en-gb/azure/role-based-access-control/built-in-roles#storage-blob-data-contributor')
resource storageBlobDataContributor 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  scope: subscription()
  name: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
}
resource storageBlobDataContributorRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, uami.id, storageBlobDataContributor.id)
  scope: storageAccount
  properties: {
    principalId: uami.properties.principalId
    roleDefinitionId: storageBlobDataContributor.id
    principalType: 'ServicePrincipal'
  }
}

// When signing is enabled, create a Key Vault holding the GPG private key.
// The function app's managed identity is granted read access (see
// rg_funcapp.bicep) and the key is surfaced to the function via a Key Vault
// reference app setting.
var key_vault_name = 'debkv${suffix}'
var gpg_secret_name = 'gpg-private-key'
var public_key_blob = 'public-key.asc'

resource keyVault 'Microsoft.KeyVault/vaults@2024-11-01' = if (signing_enabled) {
  name: key_vault_name
  location: location
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: tenant().tenantId
    // Use Azure RBAC for data-plane access so the function's managed identity
    // can be granted the built-in 'Key Vault Secrets User' role.
    enableRbacAuthorization: true
    enableSoftDelete: true
  }
}

resource gpgSecret 'Microsoft.KeyVault/vaults/secrets@2024-11-01' = if (signing_enabled) {
  parent: keyVault
  name: gpg_secret_name
  properties: {
    value: gpg_private_key
  }
}

// Create a default Packages file if it doesn't exist using a deployment script
resource deploymentScript 'Microsoft.Resources/deploymentScripts@2023-08-01' = {
  name: 'createPackagesFile${suffix}'
  dependsOn: [storageBlobDataContributorRoleAssignment]
  location: location
  kind: 'AzureCLI'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uami.id}': {}
    }
  }
  properties: {
    azCliVersion: '2.28.0'
    retentionInterval: 'PT1H'
    environmentVariables: [
      {
        name: 'AZURE_STORAGE_ACCOUNT'
        value: storageAccount.name
      }
      {
        name: 'AZURE_BLOB_CONTAINER'
        value: packageContainer.name
      }
      {
        name: 'PUBLIC_KEY'
        value: gpg_public_key
      }
      {
        name: 'PUBLIC_KEY_BLOB'
        value: public_key_blob
      }
    ]
    // This script preserves the Packages file if it exists and creates it
    // if it does not. When signing is enabled it also publishes the public key.
    // It runs as the UAMI, which holds the Storage Blob Data Contributor role,
    // so it works even when shared-key access is disabled.
    scriptContent: '''
az storage blob download --auth-mode login -f Packages -c "${AZURE_BLOB_CONTAINER}" -n Packages || echo "No existing file"
touch Packages
az storage blob upload --auth-mode login -f Packages -c "${AZURE_BLOB_CONTAINER}" -n Packages
if [ -n "${PUBLIC_KEY}" ]; then
  printf '%s' "${PUBLIC_KEY}" > public_key.asc
  az storage blob upload --auth-mode login -f public_key.asc -c "${AZURE_BLOB_CONTAINER}" -n "${PUBLIC_KEY_BLOB}"
fi
    '''
    cleanupPreference: 'OnSuccess'
  }
}

// Create the function app directly
module funcapp 'rg_funcapp.bicep' = {
  name: 'funcapp${suffix}'
  params: {
    location: location
    storage_account_name: storageAccount.name
    appName: appName
    use_shared_keys: use_shared_keys
    suffix: suffix
    signing_enabled: signing_enabled
    key_vault_name: key_vault_name
    gpg_key_name: gpg_secret_name
  }
}

// Emit the facts needed to construct an apt sources line; the client builds
// the actual string (see create_resources.py).
output public_key_blob string = public_key_blob
output function_app_name string = appName
output storage_account string = storageAccount.name
output package_container string = packageContainer.name
output python_container string = pythonContainer.name
