# Copyright (c) Alianza, Inc. All rights reserved.
# Licensed under the MIT License.
"""Manages Bicep deployments."""

import logging
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Dict, Optional

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
        subscription: Optional[str] = None,
        secure_parameters: Optional[Dict[str, str]] = None,
    ) -> None:
        """Create a BicepDeployment object.

        secure_parameters are values (e.g. a private key) that must not appear
        on the command line or in logs. Each is passed to 'az' via the
        'key=@file' syntax so the value is read from a temporary file rather
        than argv, and is never logged.
        """
        self.deployment_name = deployment_name
        self.resource_group_name = resource_group_name
        self.template_file = template_file
        self.description = description
        self.subscription = subscription
        self.secure_parameters = secure_parameters or {}

        # Convert the set of parameters to a list of flags
        self.parameters = []
        for key, value in parameters.items():
            self.parameters.extend(["--parameter", f"{key}={value}"])

    def create(self) -> None:
        """Create the deployment."""
        with ExitStack() as stack:
            # Write each secure parameter to a temporary file and reference it
            # with 'key=@file' so the value never appears in argv or logs.
            secure_flags = []
            for key, value in self.secure_parameters.items():
                tmp = stack.enter_context(
                    tempfile.NamedTemporaryFile("w", suffix=".param", encoding="utf-8")
                )
                tmp.write(value)
                tmp.flush()
                secure_flags.extend(["--parameter", f"{key}=@{tmp.name}"])

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
                    *secure_flags,
                ],
                subscription=self.subscription,
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
            ],
            subscription=self.subscription,
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
