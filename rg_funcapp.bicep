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

// Storage account names must be between 3 and 24 characters, and unique, so
// generate a unique name.
@description('The name of the storage account to use')
param storage_account_name string = 'debianrepo${suffix}'

// Choose the package container name. This will be passed to the function app.
var package_container_name = 'packages'

// Create a container for the Python code
var python_container_name = 'python'

// The version of Python to run with
var python_version = '3.11'

// The name of the hosting plan, application insights, and function app
var functionAppName = appName
var hostingPlanName = appName
var applicationInsightsName = appName

// Existing resources
resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'uami${suffix}'
}
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' existing = {
  name: storage_account_name
}
resource defBlobServices 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' existing = {
  parent: storageAccount
  name: 'default'
}
resource packageContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' existing = {
  parent: defBlobServices
  name: package_container_name
}
resource pythonContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' existing = if (!use_shared_keys) {
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
resource hostingPlan 'Microsoft.Web/serverfarms@2023-01-01' = {
  name: hostingPlanName
  location: location
  sku: {
    name: 'Y1'
    tier: 'Dynamic'
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
    name: 'FUNCTIONS_EXTENSION_VERSION'
    value: '~4'
  }
  {
    name: 'APPINSIGHTS_INSTRUMENTATIONKEY'
    value: applicationInsights.properties.InstrumentationKey
  }
  {
    name: 'FUNCTIONS_WORKER_RUNTIME'
    value: 'python'
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
    value: 'DefaultEndpointsProtocol=https;AccountName=${storageAccount.name};EndpointSuffix=${environment().suffixes.storage};AccountKey=${storageAccount.listKeys().keys[0].value}'
  }
  {
    name: 'WEBSITE_CONTENTAZUREFILECONNECTIONSTRING'
    value: 'DefaultEndpointsProtocol=https;AccountName=${storageAccount.name};EndpointSuffix=${environment().suffixes.storage};AccountKey=${storageAccount.listKeys().keys[0].value}'
  }
  {
    name: 'WEBSITE_CONTENTSHARE'
    value: toLower(functionAppName)
  }
]) : concat(common_settings, [
  {
    name: 'AzureWebJobsStorage__accountName'
    value: storageAccount.name
  }
  {
    name: 'WEBSITE_RUN_FROM_PACKAGE'
    value: 'https://${storageAccount.name}.blob.${environment().suffixes.storage}/${pythonContainer.name}/function_app.zip'
  }
  // Pass the container URL to the function app for the `from_container_url` call.
  {
    name: 'BLOB_CONTAINER_URL'
      value: 'https://${storageAccount.name}.blob.${environment().suffixes.storage}/${packageContainer.name}/'
  }
])

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
      linuxFxVersion: 'Python|${python_version}'
      pythonVersion: python_version
      appSettings: app_settings
      ftpsState: 'FtpsOnly'
      minTlsVersion: '1.2'
    }
    httpsOnly: true
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

// Output useful values
output function_app_name string = functionApp.name
