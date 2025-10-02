#!/usr/bin/env python3
# Copyright (c) Alianza, Inc. All rights reserved.
# Licensed under the MIT License.
"""Creates resources for the apt package function in Azure."""

import argparse
import logging
import sys
from pathlib import Path

from apt_package_function import common_logging
from apt_package_function.bicep_deployment import BicepDeployment
from apt_package_function.func_app import (
    FuncApp,
    FuncAppBundle,
    FuncAppZip,
)
from apt_package_function.poetry import extract_requirements
from apt_package_function.resource_group import create_rg

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())


def main() -> None:
    """Create resources."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "resource_group", help="The name of the resource group to create resources in."
    )
    parser.add_argument(
        "--location",
        default="eastus",
        help="The location of the resources to create. A list of location names can be obtained by running 'az account list-locations --query \"[].name\"'",
    )
    parser.add_argument(
        "--no-shared-keys",
        action="store_true",
        help="Use managed identities for accessing storage containers instead of shared access keys.",
    )
    parser.add_argument(
        "--suffix",
        help="Unique suffix for the repository name. If not provided, a random suffix will be generated. Must be 14 characters or fewer.",
    )
    args = parser.parse_args()

    if args.suffix and len(args.suffix) > 14:
        raise ValueError("Suffix must be 14 characters or fewer.")

    # Create the resource group
    create_rg(args.resource_group, args.location)

    # Ensure requirements.txt exists
    extract_requirements(Path("requirements.txt"))

    # Create resources with Bicep
    #
    # Set up parameters for the Bicep deployment
    common_parameters = {}
    if args.suffix:
        common_parameters["suffix"] = args.suffix

    initial_parameters = {
        "use_shared_keys": not args.no_shared_keys,
    }
    initial_parameters.update(common_parameters)

    # Use the same deployment name as the resource group
    deployment_name = args.resource_group

    initial_resources = BicepDeployment(
        deployment_name=deployment_name,
        resource_group_name=args.resource_group,
        template_file=Path("rg.bicep"),
        parameters=initial_parameters,
        description="initial resources",
    )
    initial_resources.create()

    outputs = initial_resources.outputs()
    log.debug("Deployment outputs: %s", outputs)
    apt_sources = outputs["apt_sources"]
    function_app_name = outputs["function_app_name"]
    package_container = outputs["package_container"]
    storage_account = outputs["storage_account"]

    # Create the function app
    funcapp: FuncApp
    if not args.no_shared_keys:
        funcapp = FuncAppZip(name=function_app_name, resource_group=args.resource_group)
    else:
        funcapp = FuncAppBundle(
            name=function_app_name, resource_group=args.resource_group
        )

    with funcapp as cm:
        cm.deploy()
        cm.wait_for_event_trigger()

    # At this point the function app exists and the event trigger exists, so the
    # event grid deployment can go ahead.
    event_grid_deployment = BicepDeployment(
        deployment_name=f"{deployment_name}_eg",
        resource_group_name=args.resource_group,
        template_file=Path("rg_add_eventgrid.bicep"),
        parameters=common_parameters,
        description="Event Grid trigger configuration",
    )
    event_grid_deployment.create()

    # Inform the user of success!
    print(
        f"""The repository has been created!
You can upload packages to the container '{package_container}' in the storage account '{storage_account}'.
The function app '{function_app_name}' will be triggered by new packages
in that container and regenerate the repository.

To download packages, you need to have apt-transport-blob installed on your machine.
Next, add this line to /etc/apt/sources.list:

  {apt_sources}

Ensure that you have a valid Azure credential, (either by logging in with 'az login' or
by setting the AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, and AZURE_TENANT_ID environment variables).
That credential must have 'Storage Blob Data Reader' access to the storage account.
Then you can use apt-get update and apt-get install as usual."""
    )


def run() -> None:
    """Entrypoint which sets up logging."""
    common_logging(__name__, __file__, stream=sys.stderr)
    main()


if __name__ == "__main__":
    run()
