"""Reading the configuration file. The only place a YAML parser is used.

Kept out of ``core/`` so the decision layer stays dependency-free: this
module turns a path into a dict, and ``core.config.load`` does the rest.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .core.config import SystemConfig, load

FILENAME = "multisplit.yaml"


def load_file(path: str | Path) -> SystemConfig:
    """Read and validate the configuration file."""
    with open(path, encoding="utf-8") as handle:
        return load(yaml.safe_load(handle))
