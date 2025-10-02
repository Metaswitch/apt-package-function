# Copyright (c) Alianza, Inc. All rights reserved.
# Licensed under the MIT License.
"""Management of function applications"""

import json
import logging
import subprocess
import tempfile
import time
from pathlib import Path
from subprocess import CalledProcessError
from types import TracebackType
from typing import Optional, Type
from zipfile import ZipFile

from apt_package_function.azcmd import AzCmdJson, AzCmdNone

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())


class FuncApp:
    """Basic class for managing function apps."""

    def __init__(self, name: str, resource_group: str, output_path: Path) -> None:
        """Create a FuncApp object."""
        self.name = name
        self.resource_group = resource_group
        self.output_path = output_path

    def wait_for_event_trigger(self) -> None:
        """Wait until the function app has an eventGridTrigger function."""
        cmd = AzCmdJson(
            [
                "az",
                "functionapp",
                "function",
                "list",
                "-n",
                self.name,
                "-g",
                self.resource_group,
                "--query",
                "[].name",
            ]
        )
        log.info("Awaiting event trigger on function app %s", self.name)

        while True:
            try:
                functions = cmd.run_expect_list()
                log.info("App functions (%s): %s", self.name, functions)

                for function in functions:
                    if "eventGridTrigger" in function:
                        log.info("Found Event Grid trigger: %s", function)
                        return

            except json.JSONDecodeError as e:
                log.warning("Error decoding JSON: %s", e)
            except CalledProcessError as e:
                log.debug("Error running command: %s", e)

            time.sleep(5)

    def __enter__(self) -> "FuncApp":
        """Return the object for use in a context manager."""
        return self

    def __exit__(
        self,
        _exc_type: Optional[Type[BaseException]],
        _exc_value: Optional[BaseException],
        _exc_traceback: Optional[TracebackType],
    ) -> None:
        """Clean up the object."""
        if self.output_path.exists():
            self.output_path.unlink()

    def deploy(self) -> None:
        """Deploy the function app code."""
        raise NotImplementedError("Subclasses must implement deploy method")


class FuncAppZip(FuncApp):
    """Class for managing zipped function apps."""

    def __init__(self, name: str, resource_group: str) -> None:
        """Create a FuncAppZip object."""
        self.tempfile = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        super().__init__(name, resource_group, Path(self.tempfile.name))

        self.zip_paths = [
            Path("host.json"),
            Path("requirements.txt"),
            Path("function_app.py"),
        ]

        with ZipFile(self.tempfile, "w") as zipf:
            for path in self.zip_paths:
                zipf.write(path, path.name)

        self.tempfile.close()

    def deploy(self) -> None:
        """Deploy the zipped function app."""
        cmd = AzCmdNone(
            [
                "az",
                "functionapp",
                "deployment",
                "source",
                "config-zip",
                "--resource-group",
                self.resource_group,
                "--name",
                self.name,
                "--src",
                str(self.output_path),
                "--build-remote",
                "true",
            ]
        )
        log.info("Deploying function app code to %s", self.name)
        cmd.run()
        log.info("Function app code deployed to %s", self.name)


class FuncAppBundle(FuncApp):
    """Publishes the function app using the core-tools tooling."""

    def __init__(self, name: str, resource_group: str) -> None:
        """Create a FuncAppBundle object."""
        super().__init__(name, resource_group, Path("function_app.zip"))

    def deploy(self) -> None:
        """Deploy the function application."""
        log.info("Deploying function app code")
        cwd = Path.cwd()
        home = Path.home()
        azure_config = home / ".azure"

        # Publish the application using the core-tools tooling
        cmd = [
            "docker",
            "run",
            "-it",
            "--rm",
            "-v",
            f"{azure_config}:/root/.azure",
            "-v",
            f"{cwd}:/function_app",
            "-w",
            "/function_app",
            "mcr.microsoft.com/azure-functions/python:4-python3.11-core-tools",
            "bash",
            "-c",
            f"func azure functionapp publish {self.name} --python --build remote",
        ]
        log.debug("Running %s", cmd)
        subprocess.run(cmd, check=True)
        log.info("Function app code published to %s", self.name)
