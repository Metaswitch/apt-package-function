# Copyright (c) Alianza, Inc. All rights reserved.
# Licensed under the MIT License.
"""Manages resource groups."""

import logging

from apt_package_function.azcmd import AzCmdNone

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())


def create_rg(resource_group: str, location: str) -> None:
    """Create a resource group."""
    log.debug("Creating resource group %s in location %s", resource_group, location)

    cmd = AzCmdNone(
        ["az", "group", "create", "--name", resource_group, "--location", location]
    )
    cmd.run()
    log.info("Created resource group %s in location %s", resource_group, location)
