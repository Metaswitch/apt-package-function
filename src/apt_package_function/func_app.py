# Copyright (c) Alianza, Inc. All rights reserved.
# Licensed under the MIT License.
"""Management of function applications"""

import json
import logging
import tempfile
import time
import urllib.request
from enum import IntEnum
from pathlib import Path
from subprocess import CalledProcessError
from types import TracebackType
from typing import Self
from zipfile import ZipFile

from apt_package_function.azcmd import AzCmdJson, AzCmdNone

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())

# The files that make up the deployable function app package.
FUNCTION_APP_FILES = [
    Path("host.json"),
    Path("requirements.txt"),
    Path("function_app.py"),
]


class DeployStatus(IntEnum):
    """Kudu deployment status codes (from /api/deployments/latest)."""

    PENDING = 0
    BUILDING = 1
    DEPLOYING = 2
    FAILED = 3
    SUCCESS = 4


class FuncApp:
    """Basic class for managing function apps."""

    def __init__(
        self,
        name: str,
        resource_group: str,
        output_path: Path,
        subscription: str | None = None,
    ) -> None:
        """Create a FuncApp object."""
        self.name = name
        self.resource_group = resource_group
        self.output_path = output_path
        self.subscription = subscription

    def build_function_zip(self) -> None:
        """Write the function app package to the output path."""
        with ZipFile(self.output_path, "w") as zipf:
            for path in FUNCTION_APP_FILES:
                zipf.write(path, path.name)

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
            ],
            subscription=self.subscription,
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

    def __enter__(self) -> Self:
        """Return the object for use in a context manager."""
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _exc_traceback: TracebackType | None,
    ) -> None:
        """Clean up the object."""
        if self.output_path.exists():
            self.output_path.unlink()

    def deploy(self) -> None:
        """Deploy the function app code."""
        raise NotImplementedError("Subclasses must implement deploy method")


class FuncAppZip(FuncApp):
    """Class for managing zipped function apps."""

    def __init__(
        self, name: str, resource_group: str, subscription: str | None = None
    ) -> None:
        """Create a FuncAppZip object."""
        # delete=False so the path outlives the handle: we only want a unique
        # temp path to build the zip into. FuncApp.__exit__ unlinks it.
        self.tempfile = tempfile.NamedTemporaryFile(  # noqa: SIM115
            suffix=".zip", delete=False
        )
        super().__init__(
            name, resource_group, Path(self.tempfile.name), subscription=subscription
        )

        self.build_function_zip()
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
            ],
            subscription=self.subscription,
        )
        log.info("Deploying function app code to %s", self.name)
        cmd.run()
        log.info("Function app code deployed to %s", self.name)


class FuncAppBundle(FuncApp):
    """Publishes the function app via the Azure "One Deploy" endpoint.

    Used when shared-key access is disabled. Both 'az functionapp deployment
    source config-zip' and 'az functionapp deploy' insist on fetching SCM basic
    publishing credentials, which are disabled on such apps, so they fail with
    HTTP 403. Instead we POST the package straight to the One Deploy endpoint
    with an AAD bearer token, which is accepted. This needs only 'az' (for the
    token); no Docker image or Azure Functions Core Tools are required.
    """

    # Poll the deployment for at most this long (remote build can be slow).
    _DEPLOY_TIMEOUT_S = 600
    _DEPLOY_POLL_INTERVAL_S = 15

    def __init__(
        self, name: str, resource_group: str, subscription: str | None = None
    ) -> None:
        """Create a FuncAppBundle object."""
        # delete=False so the path outlives the handle: we only want a unique
        # temp path to build the zip into. FuncApp.__exit__ unlinks it.
        self.tempfile = tempfile.NamedTemporaryFile(  # noqa: SIM115
            suffix=".zip", delete=False
        )
        super().__init__(
            name, resource_group, Path(self.tempfile.name), subscription=subscription
        )
        self.build_function_zip()
        self.tempfile.close()

    def _access_token(self) -> str:
        """Get an AAD access token for the deployment endpoint."""
        token: str = AzCmdJson(
            ["az", "account", "get-access-token", "--query", "accessToken"],
            subscription=self.subscription,
        ).run()
        return token

    def deploy(self) -> None:
        """Deploy the function app via One Deploy with a bearer token."""
        log.info("Deploying function app code to %s", self.name)
        token = self._access_token()

        data = self.output_path.read_bytes()
        # RemoteBuild=true runs the build on Azure (Oryx) at the app's own
        # runtime, so nothing local needs to match the target Python version.
        url = (
            f"https://{self.name}.scm.azurewebsites.net/api/publish"
            "?type=zip&RemoteBuild=true"
        )
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/zip",
            },
        )
        with urllib.request.urlopen(request) as response:  # noqa: S310
            log.info("Deployment accepted (HTTP %s), awaiting build", response.status)

        self._wait_for_deployment(token)
        log.info("Function app code published to %s", self.name)

    def _wait_for_deployment(self, token: str) -> None:
        """Poll the latest deployment until it succeeds, or raise on failure."""
        url = f"https://{self.name}.scm.azurewebsites.net/api/deployments/latest"
        deadline = time.monotonic() + self._DEPLOY_TIMEOUT_S
        while time.monotonic() < deadline:
            request = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {token}"}
            )
            with urllib.request.urlopen(request) as response:  # noqa: S310
                info = json.loads(response.read())

            status = info.get("status")
            try:
                status_name = DeployStatus(status).name
            except ValueError:
                status_name = f"Unknown({status})"
            log.info(
                "Deployment status: %s %s", status_name, info.get("status_text", "")
            )
            if status == DeployStatus.SUCCESS:
                return
            if status == DeployStatus.FAILED:
                raise RuntimeError(
                    f"Deployment of {self.name} failed: {info.get('status_text', '')}"
                )
            time.sleep(self._DEPLOY_POLL_INTERVAL_S)

        raise TimeoutError(
            f"Deployment of {self.name} did not complete within "
            f"{self._DEPLOY_TIMEOUT_S}s"
        )
