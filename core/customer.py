"""Customer policy and reviewed mappings from source exports to canonical fields."""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "business.json"


def load(path: Path | None = None) -> dict:
    path = path or Path(os.environ.get("DOCKSIDE_CONFIG", DEFAULT_CONFIG))
    return json.loads(path.read_text())


def supplier_path(sources: dict, folder: str, key: str) -> str:
    paths = sources["paths"]
    return (Path(paths["supplier_root"]) / folder / paths[key]).as_posix()


def supplier_id(sources: dict, folder: str) -> str:
    separator = sources.get("supplier_folder_separator")
    return folder.split(separator, 1)[0] if separator else folder
