// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

// This file adds an event grid subscription to a storage account that triggers
// when a blob is added
targetScope = 'resourceGroup'

@description('Unique suffix')
param suffix string = uniqueString(resourceGroup().id)

@description('The location of the resources')
param location string = resourceGroup().location

@description('The name of the function app to use')
param appName string = 'debfnapp${suffix}'

@description('The name of the storage account to use')
param storage_account_name string = 'debianrepo${suffix}'

var package_container_name = 'packages'

// Define all existing resources
resource storageAccount 'Microsoft.Storage/storageAccounts@2024-01-01' existing = {
  name: storage_account_name
}
resource defBlobServices 'Microsoft.Storage/storageAccounts/blobServices@2024-01-01' existing = {
  parent: storageAccount
  name: 'default'
}
resource packageContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-01-01' existing = {
  parent: defBlobServices
  name: package_container_name
}
resource functionApp 'Microsoft.Web/sites@2024-11-01' existing = {
  name: appName
}

// Now create an event grid subscription so that when a blob is created,
// it triggers the function app
resource systemTopic 'Microsoft.EventGrid/systemTopics@2025-02-15' = {
  name: 'blobscreated${suffix}'
  location: location
  properties: {
    source: storageAccount.id
    topicType: 'Microsoft.Storage.StorageAccounts'
  }
}

// Construct the resource ID to use for the event subscription
var functionId = '${functionApp.id}/functions/eventGridTrigger'

resource eventSubscription 'Microsoft.EventGrid/systemTopics/eventSubscriptions@2025-02-15' = {
  parent: systemTopic
  name: 'blobscreated${suffix}'
  properties: {
    destination: {
      endpointType: 'AzureFunction'
      properties: {
        resourceId: functionId
        maxEventsPerBatch: 1
      }
    }
    filter: {
      includedEventTypes: [
        'Microsoft.Storage.BlobCreated'
      ]
      // Filter in Debian files only
      subjectBeginsWith: '/blobServices/default/containers/${packageContainer.name}/'
      subjectEndsWith: '.deb'
    }
  }
}
