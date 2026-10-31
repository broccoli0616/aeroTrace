"""AeroTrace. Importing the package loads KEY=value pairs from ./.env (variables already set in the shell win)."""
import os
from pathlib import Path


def _load_dotenv(path: Path = Path(".env")):
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip("'\"")
        if value:
            os.environ.setdefault(key.strip(), value)


_load_dotenv()
