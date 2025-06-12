# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Manages Bicep deployments."""

import logging
from pathlib import Path
from typing import Any, Dict

from apt_package_function.azcmd import AzCmdJson, AzCmdNone

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())


class BicepDeployment:
    """Class to manage a Bicep deployment."""

    def __init__(
        self,
        deployment_name: str,
        resource_group_name: str,
        template_file: Path,
        parameters: Dict[str, Any],
        description: str,
    ) -> None:
        """Create a BicepDeployment object."""
        self.deployment_name = deployment_name
        self.resource_group_name = resource_group_name
        self.template_file = template_file
        self.description = description

        # Convert the set of parameters to a list of flags
        self.parameters = []
        for key, value in parameters.items():
            self.parameters.extend(["--parameter", f"{key}={value}"])

    def create(self) -> None:
        """Create the deployment."""
        cmd = AzCmdNone(
            [
                "az",
                "deployment",
                "group",
                "create",
                "--name",
                self.deployment_name,
                "--resource-group",
                self.resource_group_name,
                "--template-file",
                str(self.template_file),
                *self.parameters,
            ]
        )
        log.info(
            "Deploying: %s (in resource group: %s)",
            self.description,
            self.resource_group_name,
        )
        cmd.run()
        log.info("Finished deploying %s", self.description)

    def outputs(self) -> Dict[str, Any]:
        """Get the outputs of the deployment."""
        cmd = AzCmdJson(
            [
                "az",
                "deployment",
                "group",
                "show",
                "--name",
                self.deployment_name,
                "--resource-group",
                self.resource_group_name,
                "--query",
                "properties.outputs",
            ]
        )
        data = cmd.run_expect_dict()

        # The returned outputs are a dictionary of dictionaries, with type
        # information and values. We just want the values.
        outputs = {}
        for key, info in data.items():
            if info["type"] == "String":
                outputs[key] = str(info["value"])
            else:
                raise ValueError(f"Unsupported value type: {info['type']}")

        return outputs
