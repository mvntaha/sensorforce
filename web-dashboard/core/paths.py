"""Where FreshGuard keeps files that are neither code nor database rows.

One place decides these, so the writer and the reader cannot disagree about
where a file went. The camera frame is the first of them: the ingest worker
writes it and the web app serves it, and they run as separate processes.

Nothing here is inside the static folder. A frame is device data, not an asset
of the application, and Flask caches static files aggressively — a photograph
that updates every few seconds should not be served with those headers.
"""

from __future__ import annotations

import re
from pathlib import Path

#: Repository root, resolved from this file rather than the working directory,
#: so a script started from anywhere still finds it.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: Runtime data that is regenerated rather than kept. Git-ignored.
VAR_DIR = REPO_ROOT / "var"

#: Camera frames, one current file per device.
SNAPSHOT_DIR = VAR_DIR / "snapshots"

#: Photographs a person asked for from the Snapshots page, one file each, kept
#: until deleted. Separate from SNAPSHOT_DIR, whose single file per device is
#: replaced every few seconds.
CAPTURE_DIR = VAR_DIR / "captures"

#: Device ids reach this module from configuration and from the cloud, so they
#: are constrained rather than trusted: a device id is what names the file.
_SAFE_DEVICE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

#: A capture's file name, as the ingest worker writes it. Checked again when
#: the web app serves one, so a database row can never point outside the folder.
_SAFE_CAPTURE = re.compile(r"^[A-Za-z0-9._-]{1,64}-[A-Za-z0-9_-]{1,64}\.jpg$")


def safe_device_id(device_id: str) -> str:
    """Return the device id if it can safely name a file, else raise.

    A device id containing a slash or a parent reference would otherwise let a
    cloud payload choose where the application writes.
    """
    if not _SAFE_DEVICE.match(device_id or ""):
        raise ValueError(f"unsafe device id for a filename: {device_id!r}")
    return device_id


def snapshot_path(device_id: str) -> Path:
    """The current camera frame for one device."""
    return SNAPSHOT_DIR / f"{safe_device_id(device_id)}.jpg"


def capture_path(file_name: str, folder: Path | None = None) -> Path:
    """Where one requested photograph lives. Raises on a name that is not ours."""
    if not _SAFE_CAPTURE.match(file_name or ""):
        raise ValueError(f"unsafe capture file name: {file_name!r}")
    return (folder or CAPTURE_DIR) / file_name
