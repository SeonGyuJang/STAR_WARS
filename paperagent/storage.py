"""Where card images, captions and extracted figures are kept.

Keys look like "cards/12/card_01.png" or "figures/12/fig3.png".
  * local - files under DATA_DIR (default for local runs)
  * db    - rows in the blobs table (default when DATABASE_URL is set, e.g. on
            Heroku, whose dyno filesystem is wiped on every restart)
"""
import mimetypes
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import config, db


def _local_path(key: str) -> Path:
    path = (config.DATA_DIR / key).resolve()
    if not str(path).startswith(str(config.DATA_DIR.resolve())):
        raise ValueError(f"bad storage key: {key}")
    return path


def _type(key: str) -> str:
    return mimetypes.guess_type(key)[0] or "application/octet-stream"


def put(key: str, data: bytes, content_type: str | None = None):
    if config.STORAGE_BACKEND == "db":
        db.put_blob(key, data, content_type or _type(key))
    else:
        path = _local_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def put_file(key: str, path) -> str:
    put(key, Path(path).read_bytes())
    return key


def get(key: str):
    """Returns (bytes, content_type) or None."""
    if config.STORAGE_BACKEND == "db":
        return db.get_blob(key)
    path = _local_path(key)
    if not path.is_file():
        return None
    return path.read_bytes(), _type(key)


def stored_at(key: str):
    """When an object was last written (UTC ISO), or None."""
    if config.STORAGE_BACKEND == "db":
        return db.blob_updated_at(key)
    path = _local_path(key)
    if not path.is_file():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).replace(microsecond=0).isoformat()


def exists(key: str) -> bool:
    return get(key) is not None


def delete_prefix(prefix: str):
    if config.STORAGE_BACKEND == "db":
        db.delete_blobs(prefix)
    else:
        path = _local_path(prefix)
        if path.is_dir():
            shutil.rmtree(path)


def materialize(key: str, dest_dir: Path) -> Path | None:
    """Copy a stored object to a local file (e.g. so Chromium can load it)."""
    item = get(key)
    if item is None:
        return None
    dest = Path(dest_dir) / key.replace("/", "_")
    dest.write_bytes(item[0])
    return dest
