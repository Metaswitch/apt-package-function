# Copyright (c) Alianza, Inc. All rights reserved.
# Licensed under the MIT License.
"""Functions for interacting with az cli"""

import json
import logging
import subprocess
from typing import Any

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())


class AzCmd:
    """Base class for running Azure CLI commands"""

    OUTPUT: str

    def __init__(self, cmd: list[str], subscription: str | None = None) -> None:
        """Create an AzCmd object"""
        self.cmd = cmd
        if subscription:
            self.cmd = [*cmd, "--subscription", subscription]

    def _run_cmd(self, cmd: list[str]) -> Any:  # noqa: ANN401
        """Runs a command and may return output"""
        raise NotImplementedError

    def _az_cmd(self) -> str:
        """Run an Azure CLI command"""
        cmd = self.cmd[:]

        # Need to make sure the command has the right output modifier.
        delimiter = "@@@"
        full_cmd = delimiter.join(cmd)
        if (
            f"-o{delimiter}{self.OUTPUT}" not in full_cmd
            and f"-o{self.OUTPUT}" not in full_cmd
            and f"--output{delimiter}{self.OUTPUT}" not in full_cmd
        ):
            # Need to add the output modifier.
            cmd.extend(["--output", self.OUTPUT])

        log.debug("Running %s", cmd)
        return self._run_cmd(cmd)


class AzCmdNone(AzCmd):
    """Class for running Azure CLI commands that don't return anything"""

    OUTPUT = "none"

    def run(self) -> None:
        """Run the Azure CLI command"""
        self._az_cmd()

    def _run_cmd(self, cmd: list[str]) -> None:
        """Run a command but don't capture the output"""
        subprocess.run(cmd, check=True)


class AzCmdJson(AzCmd):
    """Class for running Azure CLI commands that return JSON"""

    OUTPUT = "json"

    def run(self) -> Any:  # noqa: ANN401
        """Run the Azure CLI command and return the result."""
        data = self._az_cmd()
        return json.loads(data)

    def _run_cmd(self, cmd: list[str]) -> str:
        return subprocess.check_output(cmd, encoding="utf-8")

    def run_expect_dict(self) -> dict[str, Any]:
        """Run the Azure CLI command and return the result as a dictionary"""
        data: dict[str, Any] = self.run()
        if not isinstance(data, dict):
            raise TypeError(
                f"Expected a dictionary, got {data.__class__.__name__}: {data}"
            )
        return data

    def run_expect_list(self) -> list[str]:
        """Run the Azure CLI command and return the result as a list of strings"""
        data: list[str] = self.run()
        if not isinstance(data, list):
            raise TypeError(f"Expected a list, got {data}")
        return data
