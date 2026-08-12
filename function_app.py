# Copyright (c) Alianza, Inc. All rights reserved.
# Licensed under the MIT License.
"""A function app to manage a Debian repository in Azure Blob Storage."""

import contextlib
import hashlib
import io
import logging
import lzma
import os
import tempfile
from collections.abc import Generator
from email.utils import formatdate
from pathlib import Path

import azure.functions as func
import pydpkg
from azure.identity import DefaultAzureCredential
from azure.storage.blob import ContainerClient

app = func.FunctionApp()
log = logging.getLogger("apt-package-function")
log.addHandler(logging.NullHandler())

# Turn down logging for azure functions
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(
    logging.WARNING
)

CONTAINER_NAME = os.environ["BLOB_CONTAINER"]
DEB_CHECK_KEY = "DebLastModified"

# Signing is enabled iff a GPG private key is provided (via a Key Vault
# reference app setting). When set, the repository Release file is signed.
GPG_PRIVATE_KEY = os.environ.get("GPG_PRIVATE_KEY")


@contextlib.contextmanager
def temporary_filename() -> Generator[str]:
    """Create a temporary file and return the filename."""
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            temporary_name = f.name
        yield temporary_name
    finally:
        if temporary_name:
            os.unlink(temporary_name)


class PackageBlob:
    """A class to manage a Debian package in a storage account."""

    def __init__(self, container_client: ContainerClient, name: str) -> None:
        """Create a PackageBlob object."""
        self.path = Path(name)

        # Get a Blob Client for the given name
        self.blob_client = container_client.get_blob_client(name)
        self.package_properties = self.blob_client.get_blob_properties()
        self.last_modified = str(self.package_properties.last_modified)

        # Create a Blob Client for the metadata file
        self.metadata_path = self.path.with_suffix(".package")
        self.metadata_blob_client = container_client.get_blob_client(
            str(self.metadata_path)
        )

    def check(self) -> None:
        """Check the package and metadata file."""
        log.info("Checking package: %s", self.path)

        # Check if the metadata file exists and if it doesn't, create it
        if not self.metadata_blob_client.exists():
            log.error("Metadata file missing for: %s", self.path)
            self.create_metadata()
            return

        # The metadata file exists. First, check the BlobProperties metadata
        # to make sure that the LastModified time of the package is the same as
        # the LastModified metadata variable on the metadata file.
        metadata_properties = self.metadata_blob_client.get_blob_properties()

        if DEB_CHECK_KEY not in metadata_properties.metadata:
            log.error("Metadata file missing DebLastModified for: %s", self.path)
            self.create_metadata()
            return

        if self.last_modified != metadata_properties.metadata[DEB_CHECK_KEY]:
            log.error("Metadata file out of date for: %s", self.path)
            self.create_metadata()
            return

    def create_metadata(self) -> None:
        """Create the metadata file for the package."""
        log.info("Creating metadata file for: %s", self.path)

        # Get a temporary filename to work with
        with temporary_filename() as temp_filename:
            # Download the package to the temporary file
            with open(temp_filename, "wb") as f:
                stream = self.blob_client.download_blob()
                f.write(stream.readall())

            # Now with the package on disc, load it with pydpkg.
            pkg = pydpkg.Dpkg(temp_filename)

            # Construct the metadata file, which is:
            # - the data in the control file
            # - the filename
            # - the MD5sum of the package
            # - the SHA1 of the package
            # - the SHA256 of the package
            # - the size of the package
            contents = f"""{pkg.control_str.rstrip()}
Filename: {self.path}
MD5sum: {pkg.md5}
SHA1: {pkg.sha1}
SHA256: {pkg.sha256}
Size: {pkg.filesize}

"""
            # Log the metadata information
            log.info("Metadata info for %s: %s", self.path, contents)

            # Upload the metadata information to the metadata file. Make sure
            # the DebLastModified metadata variable is set to the LastModified
            # time of the package.
            self.metadata_blob_client.upload_blob(
                contents,
                metadata={DEB_CHECK_KEY: self.last_modified},
                overwrite=True,
            )


class RepoManager:
    """A class which manages a Debian repository in a storage account."""

    def __init__(self) -> None:
        """Create a RepoManager object."""
        if "AzureWebJobsStorage" in os.environ:
            # Use a connection string to access the storage account
            self.connection_string = os.environ["AzureWebJobsStorage"]
            self.container_client = ContainerClient.from_connection_string(
                conn_str=self.connection_string, container_name=CONTAINER_NAME
            )
        else:
            # Use credentials to access the container. Used when shared-key
            # access is disabled.
            self.credential = DefaultAzureCredential()
            self.container_client = ContainerClient.from_container_url(
                container_url=os.environ["BLOB_CONTAINER_URL"],
                credential=self.credential,
            )

        self.package_file = self.container_client.get_blob_client("Packages")
        self.package_file_xz = self.container_client.get_blob_client("Packages.xz")

    def check_metadata(self) -> None:
        """Iterate over the packages and check the metadata file."""
        # Get the list of all blobs in the container
        blobs = self.container_client.list_blobs()

        # Get all of the Debian packages
        for blob in blobs:
            if not blob.name.endswith(".deb"):
                continue

            # Create a PackageBlob object and check it
            pb = PackageBlob(self.container_client, blob.name)
            pb.check()

    def create_packages(self) -> None:
        """Iterate over all metadata files to create a Packages file."""
        # Get the list of all blobs in the container
        blobs = self.container_client.list_blobs()

        # Get all of the metadata files
        packages_stream = io.BytesIO()

        for blob in blobs:
            if not blob.name.endswith(".package"):
                continue

            log.info("Processing metadata file: %s", blob.name)

            # Get the contents of the metadata file
            metadata_blob_client = self.container_client.get_blob_client(blob.name)
            num_bytes = metadata_blob_client.download_blob().readinto(packages_stream)
            log.info("Read %d bytes from %s", num_bytes, blob.name)

        # The stream now contains all of the metadata files.
        # Read out as bytes
        packages_stream.seek(0)
        packages_bytes = packages_stream.read()

        # Upload the data to the Packages file
        self.package_file.upload_blob(packages_bytes, overwrite=True)
        log.info("Created Packages file")

        # Compress the Packages file using lzma and then upload it to the
        # Packages.xz file
        compressed_data = lzma.compress(packages_bytes)
        self.package_file_xz.upload_blob(compressed_data, overwrite=True)
        log.info("Created Packages.xz file")

        # If signing is enabled, generate and sign a Release file over the
        # Packages files we just produced.
        if GPG_PRIVATE_KEY:
            self.create_release(
                {"Packages": packages_bytes, "Packages.xz": compressed_data},
                GPG_PRIVATE_KEY,
            )

    def create_release(self, files: dict, armored_private_key: str) -> None:
        """Build, sign and upload the Release / InRelease / Release.gpg files."""
        release = build_release(files)
        inrelease, release_gpg = sign_release(release, armored_private_key)

        for name, data in (
            ("Release", release),
            ("InRelease", inrelease),
            ("Release.gpg", release_gpg),
        ):
            self.container_client.get_blob_client(name).upload_blob(
                data, overwrite=True
            )
            log.info("Created %s file", name)


def build_release(files: dict) -> str:
    """Build a flat-repository Release file over the given {name: bytes}.

    Includes size and MD5/SHA1/SHA256 digests for each file, as apt requires.
    """
    lines = [
        "Origin: apt-package-function",
        "Label: apt-package-function",
        f"Date: {formatdate(usegmt=True)}",
    ]
    for algo_name, algo in (("MD5Sum", "md5"), ("SHA1", "sha1"), ("SHA256", "sha256")):
        lines.append(f"{algo_name}:")
        for name, data in files.items():
            digest = hashlib.new(algo, data).hexdigest()
            lines.append(f" {digest} {len(data)} {name}")
    return "\n".join(lines) + "\n"


def sign_release(release: str, armored_private_key: str) -> tuple:
    """Sign a Release file, returning (InRelease bytes, Release.gpg bytes).

    InRelease is an inline (cleartext) signed document; Release.gpg is a
    detached armored signature. Imported lazily so the module still loads if
    signing is disabled and pgpy is unavailable.
    """
    import pgpy

    key, _ = pgpy.PGPKey.from_blob(armored_private_key)

    detached = key.sign(release)

    message = pgpy.PGPMessage.new(release, cleartext=True)
    message |= key.sign(message)

    return str(message).encode(), str(detached).encode()


@app.function_name(name="eventGridTrigger")
@app.event_grid_trigger(arg_name="event")
def event_grid_trigger(event: func.EventGridEvent) -> None:
    """Process an event grid trigger for a new blob in the container."""
    log.info("Processing event %s", event.id)
    rm = RepoManager()
    rm.check_metadata()
    rm.create_packages()
    log.info("Done processing event %s", event.id)
