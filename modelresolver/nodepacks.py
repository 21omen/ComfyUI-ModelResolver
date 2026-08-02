"""Resolve and install missing custom-node packs from the Comfy Registry."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
from typing import Any
from urllib.parse import quote, urlencode, urlparse
import zipfile

import aiohttp

REGISTRY_BASE = "https://api.comfy.org"
MAX_REFERENCES = 5000
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_EXTRACTED_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_FILES = 20000

_FRONTEND_ONLY = frozenset({
    "Note",
    "PrimitiveNode",
    "Reroute",
    "MarkdownNote",
})
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_install_lock = asyncio.Lock()


class NodePackError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _registered_node_types() -> set[str]:
    import nodes

    return set(nodes.NODE_CLASS_MAPPINGS)


def _clean_reference(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    class_type = raw.get("class_type")
    if not isinstance(class_type, str) or not class_type or len(class_type) > 300:
        return None
    node_id = raw.get("node_id")
    cnr_id = raw.get("cnr_id")
    aux_id = raw.get("aux_id")
    version = raw.get("version")
    return {
        "class_type": class_type,
        "node_id": str(node_id)[:100] if node_id is not None else None,
        "cnr_id": cnr_id[:200] if isinstance(cnr_id, str) else None,
        "aux_id": aux_id[:200] if isinstance(aux_id, str) else None,
        "version": version[:100] if isinstance(version, str) else None,
        "frontend_available": raw.get("frontend_available") is True,
    }


async def _get_json(
    session: aiohttp.ClientSession, path: str, timeout: int
) -> dict[str, Any] | None:
    url = f"{REGISTRY_BASE}{path}"
    async with session.get(
        url, timeout=aiohttp.ClientTimeout(total=timeout)
    ) as response:
        if response.status == 404:
            return None
        if response.status != 200:
            text = (await response.text())[:300]
            raise NodePackError(
                f"Comfy Registry returned HTTP {response.status}: {text}", 502
            )
        data = await response.json(content_type=None)
        if not isinstance(data, dict):
            raise NodePackError("Comfy Registry returned an invalid response", 502)
        return data


def _pack_summary(data: dict[str, Any], match: str) -> dict[str, Any] | None:
    pack_id = data.get("id")
    latest = data.get("latest_version") or {}
    publisher = data.get("publisher") or {}
    if (
        not isinstance(pack_id, str)
        or data.get("status") != "NodeStatusActive"
        or not isinstance(latest, dict)
        or latest.get("status") != "NodeVersionStatusActive"
    ):
        return None
    return {
        "id": pack_id,
        "name": data.get("name") or pack_id,
        "description": data.get("description") or "",
        "repository": data.get("repository") or "",
        "latest_version": latest.get("version"),
        "downloads": data.get("downloads"),
        "publisher": publisher.get("name", "") if isinstance(publisher, dict) else "",
        "tags_admin": data.get("tags_admin") or [],
        "match": match,
    }


async def _resolve_reference(
    session: aiohttp.ClientSession, reference: dict[str, Any], timeout: int
) -> dict[str, Any]:
    pack_id = reference["cnr_id"]
    if pack_id:
        data = await _get_json(session, f"/nodes/{quote(pack_id, safe='')}", timeout)
        match = "workflow_metadata"
    elif reference["aux_id"]:
        expected_repository = f"https://github.com/{reference['aux_id']}"
        repository_name = reference["aux_id"].split("/", 1)[-1]
        data = await _get_json(
            session, f"/nodes/{quote(repository_name, safe='')}", timeout
        )
        if data and _normalize_repository(data.get("repository")) != _normalize_repository(
            expected_repository
        ):
            data = None
        match = "workflow_repository"
    else:
        class_type = quote(reference["class_type"], safe="")
        data = await _get_json(session, f"/comfy-nodes/{class_type}/node", timeout)
        match = "registry_class"
    pack = _pack_summary(data, match) if data else None
    return {**reference, "pack": pack}


def _normalize_repository(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.rstrip("/").removesuffix(".git").casefold()


async def find_missing_node_packs(
    references: list[Any], timeout: int = 20
) -> dict[str, Any]:
    if len(references) > MAX_REFERENCES:
        raise NodePackError(f"At most {MAX_REFERENCES} workflow nodes can be checked")

    installed_types = _registered_node_types()
    missing_by_class: dict[str, dict[str, Any]] = {}
    installed_count = 0
    for raw in references:
        ref = _clean_reference(raw)
        if ref is None:
            continue
        class_type = ref["class_type"]
        if class_type in installed_types or ref["frontend_available"]:
            installed_count += 1
            continue
        if ref["cnr_id"] == "comfy-core" or class_type in _FRONTEND_ONLY:
            continue
        existing = missing_by_class.get(class_type)
        if existing is None:
            ref["node_ids"] = []
            missing_by_class[class_type] = ref
            existing = ref
        if ref["node_id"] and ref["node_id"] not in existing["node_ids"]:
            existing["node_ids"].append(ref["node_id"])
        if not existing["cnr_id"] and ref["cnr_id"]:
            existing["cnr_id"] = ref["cnr_id"]
        if not existing["aux_id"] and ref["aux_id"]:
            existing["aux_id"] = ref["aux_id"]
        if not existing["version"] and ref["version"]:
            existing["version"] = ref["version"]

    missing = list(missing_by_class.values())
    if not missing:
        return {
            "packs": [],
            "unresolved": [],
            "missing_node_count": 0,
            "installed_node_count": installed_count,
        }

    timeout_cfg = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
        resolved = await asyncio.gather(
            *(_resolve_reference(session, ref, timeout) for ref in missing),
            return_exceptions=True,
        )

    packs: dict[str, dict[str, Any]] = {}
    unresolved: list[dict[str, Any]] = []
    for ref, result in zip(missing, resolved):
        if isinstance(result, Exception):
            unresolved.append({
                **ref,
                "reason": str(result),
            })
            continue
        pack = result["pack"]
        if pack is None:
            unresolved.append({
                **ref,
                "reason": "No active Comfy Registry pack matched this node class",
            })
            continue
        entry = packs.setdefault(
            pack["id"], {**pack, "node_types": [], "requested_versions": []}
        )
        entry["node_types"].append(ref["class_type"])
        if ref["version"] and ref["version"] not in entry["requested_versions"]:
            entry["requested_versions"].append(ref["version"])

    for pack in packs.values():
        semvers = [v for v in pack["requested_versions"] if _SEMVER.fullmatch(v)]
        pack["install_version"] = semvers[0] if len(set(semvers)) == 1 else None
        pack["node_types"].sort(key=str.casefold)

    return {
        "packs": sorted(packs.values(), key=lambda item: item["name"].casefold()),
        "unresolved": unresolved,
        "missing_node_count": len(missing),
        "installed_node_count": installed_count,
    }


def _custom_nodes_root() -> Path:
    import folder_paths

    roots = folder_paths.get_folder_paths("custom_nodes") or []
    if not roots:
        raise NodePackError("ComfyUI has no registered custom_nodes folder", 500)
    root = Path(roots[0]).resolve()
    if not root.is_dir():
        raise NodePackError(f"Custom-nodes folder does not exist: {root}", 500)
    return root


def _destination_name(pack_id: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", pack_id).strip(" .-")
    if not name or name in {".", ".."}:
        raise NodePackError("Registry pack ID cannot form a safe folder name")
    return name


def _safe_extract(archive: Path, output: Path) -> None:
    total = 0
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ARCHIVE_FILES:
            raise NodePackError("Node-pack archive contains too many files")
        for info in infos:
            path = PurePosixPath(info.filename.replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise NodePackError(f"Unsafe path in node-pack archive: {info.filename}")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise NodePackError(
                    f"Symlinks are not allowed in node-pack archives: {info.filename}"
                )
            total += info.file_size
            if total > MAX_EXTRACTED_BYTES:
                raise NodePackError("Node-pack archive expands beyond the 1 GiB limit")
            target = output.joinpath(*path.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as source, open(target, "wb") as destination:
                shutil.copyfileobj(source, destination)


def _payload_root(payload: Path) -> Path:
    entries = list(payload.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return payload


async def _download_archive(url: str, destination: Path, timeout: int) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "cdn.comfy.org":
        raise NodePackError("Registry returned an untrusted node-pack download URL", 502)
    request_timeout = aiohttp.ClientTimeout(total=None, sock_connect=timeout, sock_read=120)
    size = 0
    async with aiohttp.ClientSession(timeout=request_timeout) as session:
        async with session.get(url) as response:
            if response.url.scheme != "https" or response.url.host != "cdn.comfy.org":
                raise NodePackError(
                    "Node-pack download redirected to an untrusted URL", 502
                )
            if response.status != 200:
                raise NodePackError(
                    f"Node-pack download returned HTTP {response.status}", 502
                )
            content_length = response.content_length
            if content_length and content_length > MAX_ARCHIVE_BYTES:
                raise NodePackError("Node-pack archive exceeds the 512 MiB limit")
            with open(destination, "wb") as handle:
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_ARCHIVE_BYTES:
                        raise NodePackError("Node-pack archive exceeds the 512 MiB limit")
                    handle.write(chunk)


async def _install_requirements(node_root: Path) -> dict[str, Any]:
    requirements = node_root / "requirements.txt"
    if not requirements.is_file():
        return {"status": "not_present", "returncode": None, "output": ""}
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "-r",
            str(requirements),
            cwd=str(node_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return {
            "status": "failed",
            "returncode": None,
            "output": str(exc),
        }
    output, _ = await process.communicate()
    text = output.decode("utf-8", errors="replace")[-8000:]
    return {
        "status": "installed" if process.returncode == 0 else "failed",
        "returncode": process.returncode,
        "output": text,
    }


async def install_node_pack(
    pack_id: str, version: str | None = None, timeout: int = 20
) -> dict[str, Any]:
    if not isinstance(pack_id, str) or not pack_id or len(pack_id) > 200:
        raise NodePackError("pack_id is required")
    if version is not None and not _SEMVER.fullmatch(version):
        raise NodePackError("version must use MAJOR.MINOR.PATCH form")

    async with _install_lock:
        root = _custom_nodes_root()
        destination = root / _destination_name(pack_id)
        if destination.exists():
            raise NodePackError(
                f"Destination already exists: {destination.name}. Update or repair it manually.",
                409,
            )

        query = f"?{urlencode({'version': version})}" if version else ""
        timeout_cfg = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
            record = await _get_json(
                session, f"/nodes/{quote(pack_id, safe='')}/install{query}", timeout
            )
        if record is None:
            raise NodePackError("The requested Registry node pack or version was not found", 404)
        if (
            record.get("node_id") != pack_id
            or record.get("status") != "NodeVersionStatusActive"
            or not isinstance(record.get("downloadUrl"), str)
        ):
            raise NodePackError("Registry did not return an active installable node pack", 409)

        staging = Path(tempfile.mkdtemp(prefix=".modelresolver-", dir=root))
        archive = staging / "node.zip"
        payload = staging / "payload"
        payload.mkdir()
        try:
            await _download_archive(record["downloadUrl"], archive, timeout)
            await asyncio.to_thread(_safe_extract, archive, payload)
            source = _payload_root(payload)
            if not any(source.iterdir()):
                raise NodePackError("Node-pack archive is empty")
            try:
                os.replace(source, destination)
            except FileExistsError as exc:
                raise NodePackError(
                    f"Destination was created during installation: {destination.name}", 409
                ) from exc
            requirements = await _install_requirements(destination)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        status = "installed" if requirements["status"] != "failed" else "requirements_failed"
        return {
            "status": status,
            "pack_id": pack_id,
            "version": record.get("version"),
            "folder": destination.name,
            "requirements": requirements,
            "restart_required": True,
        }
