"""Resolve bundled audio tools even when the server starts without the launcher."""
from pathlib import Path
import os
import shutil


def find_executable(name: str) -> str | None:
    path = shutil.which(name)
    if path:
        return path
    if Path(name).name == name:
        local = Path(__file__).resolve().parent / '.tools' / 'bin' / name
        if local.is_file() and os.access(local, os.X_OK):
            return str(local)
    return None
