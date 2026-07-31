"""Identity verification: check local file against online source by hash.

On button press (not on every scan - hashing large files takes time).
Confirms that a locally present file (possibly under a different name)
is actually the same as the model required by the workflow.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from typing import Any

CHUNK = 1024 * 1024


def _resolve_local_path(folder_type: str, filename: str) -> str | None:
    import folder_paths

    try:
        full = folder_paths.get_full_path(folder_type, filename)
    except Exception:  # noqa: BLE001
        full = None
    if full and os.path.isfile(full):
        return full
    # Fallback: direct basename scan across folders of this type
    base = os.path.basename(filename.replace("\\", "/"))
    for root in folder_paths.get_folder_paths(folder_type) or []:
        cand = os.path.join(root, base)
        if os.path.isfile(cand):
            return cand
    return None


async def hash_file(path: str, algos: tuple[str, ...] = ("sha256", "md5")) -> dict:
    hs = {a: hashlib.new(a) for a in algos}
    loop = asyncio.get_running_loop()

    def _read() -> None:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                for h in hs.values():
                    h.update(chunk)

    await loop.run_in_executor(None, _read)
    return {a: h.hexdigest().lower() for a, h in hs.items()}


async def verify_local(
    folder_type: str, local_filename: str, expected: dict[str, str]
) -> dict[str, Any]:
    """Hash local file and compare against expected hashes.

    expected: {"sha256": "...", "md5": "..."} - subsets allowed.
    """
    path = _resolve_local_path(folder_type, local_filename)
    if path is None:
        return {"status": "not_found_local",
                "message": f"Local file not found: {local_filename}"}

    size = os.path.getsize(path)
    actual = await hash_file(path)
    checks = {k: v.lower() for k, v in (expected or {}).items() if v}
    if not checks:
        return {"status": "hashed", "path": path, "size_bytes": size,
                "sha256": actual["sha256"], "md5": actual["md5"],
                "message": "No comparison hash provided - computed only."}

    mismatches = {a: {"expected": exp, "actual": actual.get(a)}
                  for a, exp in checks.items() if actual.get(a) != exp}
    if mismatches:
        return {"status": "mismatch", "path": path, "size_bytes": size,
                "sha256": actual["sha256"], "md5": actual["md5"],
                "mismatches": mismatches,
                "message": "Different content despite similar name - NOT the same file."}
    return {"status": "verified", "path": path, "size_bytes": size,
            "sha256": actual["sha256"], "md5": actual["md5"],
            "message": "Confirmed: same file."}
