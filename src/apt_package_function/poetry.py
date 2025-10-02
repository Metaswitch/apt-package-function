# Copyright (c) Alianza, Inc. All rights reserved.
# Licensed under the MIT License.
"""Extracts requirements from a poetry project."""

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())


def extract_requirements(requirements_path: Path) -> None:
    """Extract requirements from a poetry.lock file using poetry export"""
    cmd = [
        "poetry",
        "export",
        "--format",
        "requirements.txt",
        "--output",
        str(requirements_path),
    ]
    log.debug("Running %s", cmd)
    subprocess.run(cmd, check=True)
    log.info("Extracted requirements to %s", requirements_path)

    if not requirements_path.exists():
        raise FileNotFoundError(f"Failed to create {requirements_path}")
