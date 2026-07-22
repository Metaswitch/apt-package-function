// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

// This file creates a function app
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

@description('The name of the Key Vault holding the GPG private key')
param key_vault_name string = ''

@description('The Key Vault secret name (not value) holding the GPG private key')
param gpg_key_name string = ''

// Storage account names must be between 3 and 24 characters, and unique, so
// generate a unique name.
@description('The name of the storage account to use')
param storage_account_name string = 'debianrepo${suffix}'

// Choose the package container name. This will be passed to the function app.
var package_container_name = 'packages'

// Create a container for the Python code
var python_container_name = 'python'

var storage_connection_string = 'DefaultEndpointsProtocol=https;AccountName=${storageAccount.name};EndpointSuffix=${environment().suffixes.storage};AccountKey=${storageAccount.listKeys().keys[0].value}'

// The version of Python to run with
var python_version = '3.11'

// The name of the hosting plan, application insights, and function app
var functionAppName = appName
var hostingPlanName = appName
var applicationInsightsName = appName

// Existing resources
resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: 'uami${suffix}'
}
resource storageAccount 'Microsoft.Storage/storageAccounts@2025-08-01' existing = {
  name: storage_account_name
}
resource defBlobServices 'Microsoft.Storage/storageAccounts/blobServices@2025-08-01' existing = {
  parent: storageAccount
  name: 'default'
}
resource packageContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-08-01' existing = {
  parent: defBlobServices
  name: package_container_name
}
resource pythonContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-08-01' existing = {
  parent: defBlobServices
  name: python_container_name
}
@description('This is the built-in Storage Blob Data Contributor role. See https://learn.microsoft.com/en-gb/azure/role-based-access-control/built-in-roles#storage-blob-data-contributor')
resource storageBlobDataContributor 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  scope: subscription()
  name: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
}
resource storageBlobDataContributorRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' existing = {
  name: guid(storageAccount.id, uami.id, storageBlobDataContributor.id)
  scope: storageAccount
}

// Create a hosting plan for the function app
// Using Flex Consumption plan for serverless hosting with enhanced features
// Reference: https://learn.microsoft.com/en-us/azure/azure-functions/flex-consumption-plan
resource hostingPlan 'Microsoft.Web/serverfarms@2024-11-01' = {
  name: hostingPlanName
  location: location
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

// Create application insights
resource applicationInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: applicationInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    Request_Source: 'rest'
  }
}

// Construct the app settings
var common_settings = [
  {
    name: 'APPINSIGHTS_INSTRUMENTATIONKEY'
    value: applicationInsights.properties.InstrumentationKey
  }
  // Pass the blob container name to the function app - this is the
  // container which is monitored for new packages.
  {
    name: 'BLOB_CONTAINER'
    value: packageContainer.name
  }
]
// Construct the application settings
// If using shared keys, include the shared key settings. Otherwise, include the managed identity settings.
var app_settings = use_shared_keys ? concat(common_settings, [
  {
    name: 'AzureWebJobsStorage'
    value: storage_connection_string
  }
  {
    name: 'DEPLOYMENT_STORAGE_CONNECTION_STRING'
    value: storage_connection_string
  }
]) : concat(common_settings, [
  {
    name: 'AzureWebJobsStorage__accountName'
    value: storageAccount.name
  }
  {
    name: 'BLOB_CONTAINER_URL'
      value: 'https://${storageAccount.name}.blob.${environment().suffixes.storage}/${packageContainer.name}/'
  }
])

// When signing is enabled, surface the GPG private key to the function via a
// Key Vault reference. The platform resolves this using the function app's
// managed identity (granted 'Key Vault Secrets User' below); the secret value
// never appears in app configuration.
var signing_settings = signing_enabled ? [
  {
    name: 'GPG_PRIVATE_KEY'
    value: '@Microsoft.KeyVault(VaultName=${key_vault_name};SecretName=${gpg_key_name})'
  }
] : []
var final_app_settings = concat(app_settings, signing_settings)

var function_runtime = {
  name: 'python'
  version: python_version
}

var deployment_storage_value = 'https://${storageAccount.name}.blob.${environment().suffixes.storage}/${pythonContainer.name}'

var deployment_authentication = use_shared_keys ? {
  type: 'StorageAccountConnectionString'
  storageAccountConnectionStringName: 'DEPLOYMENT_STORAGE_CONNECTION_STRING'
} : {
  type: 'SystemAssignedIdentity'
}

var flex_deployment_configuration = {
  storage: {
    type: 'blobContainer'
    value: deployment_storage_value
    authentication: deployment_authentication
  }
}

var flex_scale_and_concurrency = {
  maximumInstanceCount: 100
  instanceMemoryMB: 2048
}

var function_app_config = {
  runtime: function_runtime
  scaleAndConcurrency: flex_scale_and_concurrency
  deployment: flex_deployment_configuration
}

// Create the function app.
resource functionApp 'Microsoft.Web/sites@2024-11-01' = {
  name: functionAppName
  dependsOn: [storageBlobDataContributorRoleAssignment]
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  kind: 'functionapp,linux'
  properties: {
    serverFarmId: hostingPlan.id
    siteConfig: {
      appSettings: final_app_settings
      ftpsState: 'FtpsOnly'
      minTlsVersion: '1.2'
    }
    httpsOnly: true
    functionAppConfig: function_app_config
  }
}

// Grant the Function App Storage Blob Data Contributor on the storage account
// so it can access the package. Only necessary when using managed identity.
resource funcAppRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!use_shared_keys) {
  name: guid(storageAccount.id, functionApp.id, storageBlobDataContributor.id)
  scope: storageAccount
  properties: {
    principalId: functionApp.identity.principalId
    roleDefinitionId: storageBlobDataContributor.id
    principalType: 'ServicePrincipal'
  }
}

// When signing is enabled, grant the function app's managed identity read
// access to the Key Vault so the GPG_PRIVATE_KEY Key Vault reference resolves.
resource keyVault 'Microsoft.KeyVault/vaults@2024-11-01' existing = if (signing_enabled) {
  name: key_vault_name
}
@description('Built-in Key Vault Secrets User role. See https://learn.microsoft.com/en-gb/azure/role-based-access-control/built-in-roles#key-vault-secrets-user')
resource keyVaultSecretsUser 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  scope: subscription()
  name: '4633458b-17de-408a-b874-0445c86b69e6'
}
resource funcAppKvRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (signing_enabled) {
  name: guid(key_vault_name, functionApp.id, keyVaultSecretsUser.id)
  scope: keyVault
  properties: {
    principalId: functionApp.identity.principalId
    roleDefinitionId: keyVaultSecretsUser.id
    principalType: 'ServicePrincipal'
  }
}

// Output useful values
output function_app_name string = functionApp.name
