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
from apt_package_function.signing import (
    generate_private_key,
    load_private_key,
    public_key_from_private,
)

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
    parser.add_argument(
        "--subscription",
        help="The subscription to create resources in. If not provided, the active 'az' subscription is used.",
    )
    signing_group = parser.add_mutually_exclusive_group()
    signing_group.add_argument(
        "--gpg-key",
        metavar="PATH",
        help="Path to an ASCII-armored GPG private key used to sign the repository. Enables signing.",
    )
    signing_group.add_argument(
        "--autogenerate-gpg-key",
        action="store_true",
        help="Generate a new GPG key to sign the repository. Enables signing.",
    )
    args = parser.parse_args()

    if args.suffix and len(args.suffix) > 14:
        raise ValueError("Suffix must be 14 characters or fewer.")

    # Obtain the signing key (if signing is requested). The private key is
    # passed to Bicep as a secure parameter (stored in Key Vault); the public
    # key is published alongside the repository for clients to verify against.
    signing_enabled = bool(args.gpg_key or args.autogenerate_gpg_key)
    gpg_private_key = None
    gpg_public_key = None
    if args.gpg_key:
        gpg_private_key = load_private_key(args.gpg_key)
    elif args.autogenerate_gpg_key:
        gpg_private_key = generate_private_key(
            f"apt-package-function {args.resource_group}"
        )
    if gpg_private_key:
        gpg_public_key = public_key_from_private(gpg_private_key)

    # Create the resource group
    create_rg(args.resource_group, args.location, subscription=args.subscription)

    # Ensure requirements.txt exists
    extract_requirements(Path("requirements.txt"))

    # Create resources with Bicep
    #
    # Set up parameters for the Bicep deployment
    common_parameters = {}
    if args.suffix:
        common_parameters["suffix"] = args.suffix

    initial_parameters: dict = {
        "use_shared_keys": not args.no_shared_keys,
        "signing_enabled": signing_enabled,
    }
    initial_parameters.update(common_parameters)

    # Pass the keys via files ('key=@file') rather than argv. This is required
    # for the private key (secret) and convenient for the public key (which is
    # multi-line and would otherwise need escaping). A deployment script running
    # as the UAMI publishes the public key to the container.
    secure_parameters = {}
    if gpg_private_key:
        secure_parameters["gpg_private_key"] = gpg_private_key
    if gpg_public_key:
        secure_parameters["gpg_public_key"] = gpg_public_key

    # Use the same deployment name as the resource group
    deployment_name = args.resource_group

    initial_resources = BicepDeployment(
        deployment_name=deployment_name,
        resource_group_name=args.resource_group,
        template_file=Path("rg.bicep"),
        parameters=initial_parameters,
        description="initial resources",
        subscription=args.subscription,
        secure_parameters=secure_parameters,
    )
    initial_resources.create()

    outputs = initial_resources.outputs()
    log.debug("Deployment outputs: %s", outputs)
    function_app_name = outputs["function_app_name"]
    package_container = outputs["package_container"]
    storage_account = outputs["storage_account"]

    # Build the apt sources line. When signing is enabled apt verifies against
    # the published public key; otherwise the repository is trusted blindly.
    keyring = f"/etc/apt/keyrings/{storage_account}.asc"
    repo_url = f"blob://{storage_account}.blob.core.windows.net/{package_container} /"
    options = f"signed-by={keyring}" if signing_enabled else "trusted=yes"
    apt_sources = f"deb [{options}] {repo_url}"

    # Create the function app
    funcapp: FuncApp
    if not args.no_shared_keys:
        funcapp = FuncAppZip(
            name=function_app_name,
            resource_group=args.resource_group,
            subscription=args.subscription,
        )
    else:
        funcapp = FuncAppBundle(
            name=function_app_name,
            resource_group=args.resource_group,
            subscription=args.subscription,
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
        subscription=args.subscription,
    )
    event_grid_deployment.create()

    # If signing is enabled, tell the user how to install the public key.
    signing_instructions = ""
    if signing_enabled:
        public_key_blob = outputs["public_key_blob"]
        signing_instructions = f"""
This repository is signed. Install the public key so apt can verify it:

  az storage blob download --auth-mode login --account-name {storage_account} \\
    --container-name {package_container} --name {public_key_blob} \\
    --file /etc/apt/keyrings/{storage_account}.asc
"""

    # Inform the user of success!
    print(
        f"""The repository has been created!
You can upload packages to the container '{package_container}' in the storage account '{storage_account}'.
The function app '{function_app_name}' will be triggered by new packages
in that container and regenerate the repository.

To download packages, you need to have apt-transport-blob installed on your machine.
{signing_instructions}
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
