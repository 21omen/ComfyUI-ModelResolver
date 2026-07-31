"""Download module: confirmed candidates -> verified files in target folder.

- Sequential queue (parallel later), status tracking per job
- .part file with resume via HTTP range
- SHA256 is computed while streaming; on resume the existing
  part is hashed first
- Rename to final name only AFTER passing hash verification
- Civitai auth as ?token= in the URL (redirect to object storage breaks
  with a carried-over Authorization header); HF auth as header
- Target path strict: basename() of the reference in the first path of
  folder_type - no path traversal possible
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
import uuid
from typing import Any
from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse

CHUNK = 1024 * 1024  # 1 MiB

_jobs: dict[str, dict[str, Any]] = {}
_queue: asyncio.Queue | None = None
_workers: list[asyncio.Task] = []


def _target_path(folder_type: str, filename: str) -> str:
    import folder_paths

    paths = folder_paths.get_folder_paths(folder_type)
    if not paths:
        raise ValueError(f"No path registered for folder type '{folder_type}'")
    safe_name = os.path.basename(str(filename).replace("\\", "/"))
    if not safe_name:
        raise ValueError("Empty filename")
    return os.path.join(paths[0], safe_name)


def _prepare_request(job: dict, cfg: dict) -> tuple[str, dict]:
    """Add source-specific auth to URL and headers."""
    url = job["download_url"]
    headers: dict[str, str] = {}
    host = urlparse(url).netloc.lower()
    if "civitai" in host:
        key = cfg.get("civitai_api_key") or ""
        if key:
            parts = urlparse(url)
            q = dict(parse_qsl(parts.query))
            q.setdefault("token", key)
            url = urlunparse(parts._replace(query=urlencode(q)))
    elif "huggingface" in host or "hf.co" in host:
        tok = cfg.get("hf_token") or ""
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
    return url, headers


def _new_hashers() -> dict:
    return {"sha256": hashlib.sha256(), "md5": hashlib.md5()}


async def _hash_existing(path: str) -> tuple[dict, int]:
    """Hash existing .part content (for resume), MD5+SHA256 in parallel."""
    hs = _new_hashers()
    size = 0
    loop = asyncio.get_running_loop()

    def _read() -> None:
        nonlocal size
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                for h in hs.values():
                    h.update(chunk)
                size += len(chunk)

    await loop.run_in_executor(None, _read)
    return hs, size


async def _copy_local(source: str, part: str, hs: dict, state: dict) -> None:
    """Stream-copy a local file (import mode), computing hashes along the way."""
    loop = asyncio.get_running_loop()

    def _copy() -> None:
        with open(source, "rb") as src, open(part, "wb") as dst:
            while True:
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                dst.write(chunk)
                for h in hs.values():
                    h.update(chunk)
                state["bytes_done"] += len(chunk)

    await loop.run_in_executor(None, _copy)


def _safe_remove(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


async def _run_job(job: dict, cfg: dict) -> None:
    import aiohttp

    jid = job["id"]
    state = _jobs[jid]
    # Check before starting: was the (waiting) job already cancelled?
    ctl = state.get("control")
    if ctl == "cancel":
        state["status"] = "cancelled"
        state["finished_at"] = time.time()
        return
    if ctl == "pause":
        state["status"] = "paused"
        state["finished_at"] = time.time()
        return
    try:
        final = _target_path(job["folder_type"], job["filename"])
        part = final + ".part"
        state["target_path"] = final

        if os.path.exists(final):
            state["status"] = "skipped_exists"
            return

        os.makedirs(os.path.dirname(final), exist_ok=True)

        source_path = job.get("source_path")
        if source_path:
            # Import mode: sort in a local file (e.g. browser download)
            if not os.path.isfile(source_path):
                raise RuntimeError(f"Source file not found: {source_path}")
            hs = _new_hashers()
            state["status"] = "importing"
            state["bytes_total"] = os.path.getsize(source_path)
            state["bytes_done"] = 0
            await _copy_local(source_path, part, hs, state)
        else:
            offset = 0
            if os.path.exists(part):
                hs, offset = await _hash_existing(part)
                state["resumed_from"] = offset
            else:
                hs = _new_hashers()

            url, headers = _prepare_request(job, cfg)
            if offset:
                headers["Range"] = f"bytes={offset}-"

            state["status"] = "downloading"
            state["bytes_done"] = offset
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as resp:
                    if offset and resp.status == 200:
                        # Server ignores range -> start from scratch
                        hs = _new_hashers()
                        offset = 0
                        state["bytes_done"] = 0
                        state["resumed_from"] = 0
                    elif resp.status not in (200, 206):
                        raise RuntimeError(f"HTTP {resp.status} during download")
                    total = resp.headers.get("Content-Length")
                    if total is not None:
                        state["bytes_total"] = int(total) + offset
                    mode = "ab" if offset else "wb"
                    with open(part, mode) as fh:
                        async for chunk in resp.content.iter_chunked(CHUNK):
                            ctl = state.get("control")
                            if ctl in ("cancel", "pause"):
                                fh.close()
                                if ctl == "cancel":
                                    _safe_remove(part)
                                    state["status"] = "cancelled"
                                else:
                                    state["status"] = "paused"
                                return
                            fh.write(chunk)
                            for h in hs.values():
                                h.update(chunk)
                            state["bytes_done"] += len(chunk)

        actual = {name: h.hexdigest().lower() for name, h in hs.items()}
        state["sha256_actual"] = actual["sha256"]
        state["md5_actual"] = actual["md5"]
        expected = {
            "sha256": (job.get("sha256") or "").lower(),
            "md5": (job.get("md5") or "").lower(),
        }
        checks = {k: v for k, v in expected.items() if v}
        if checks:
            state["status"] = "verifying"
            for algo, exp in checks.items():
                if actual[algo] != exp:
                    bad = part + ".badsha"
                    os.replace(part, bad)
                    raise RuntimeError(
                        f"{algo.upper()} mismatch: expected {exp[:12]}..., "
                        f"got {actual[algo][:12]}... - file kept at {bad}"
                    )
            state["checksum_verified"] = True
            state["sha256_verified"] = "sha256" in checks  # Backward compatible
        else:
            state["checksum_verified"] = False
            state["sha256_verified"] = False

        os.replace(part, final)
        state["status"] = "done"
    except Exception as exc:  # noqa: BLE001
        state["status"] = "error"
        state["error"] = str(exc)
    finally:
        state["finished_at"] = time.time()


async def _worker_loop(cfg_loader) -> None:
    assert _queue is not None
    while True:
        job = await _queue.get()
        try:
            await _run_job(job, cfg_loader())
        finally:
            _queue.task_done()


def _prune_workers() -> None:
    global _workers
    _workers = [w for w in _workers if not w.done()]


def _ensure_workers(cfg_loader) -> None:
    """Start up to N workers in the running loop (N from config, 1-8)."""
    global _workers
    try:
        n = int((cfg_loader() or {}).get("max_parallel_downloads", 2))
    except Exception:  # noqa: BLE001
        n = 2
    n = max(1, min(n, 8))
    _prune_workers()
    loop = asyncio.get_running_loop()
    while len(_workers) < n:
        _workers.append(loop.create_task(_worker_loop(cfg_loader)))


async def enqueue(jobs: list[dict], cfg_loader) -> list[str]:
    """Enqueue jobs; start up to N parallel workers in the running loop.

    N comes from config.max_parallel_downloads (default 2). Workers that
    are finished/dead are replaced until N are running again.
    """
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()

    _ensure_workers(cfg_loader)

    ids: list[str] = []
    for j in jobs:
        jid = uuid.uuid4().hex[:12]
        job = {
            "id": jid,
            "filename": j["filename"],
            "folder_type": j["folder_type"],
            "download_url": j.get("download_url"),
            "source_path": j.get("source_path"),
            "sha256": j.get("sha256"),
            "md5": j.get("md5"),
            "source": j.get("source"),
        }
        _jobs[jid] = {
            "id": jid,
            "filename": job["filename"],
            "folder_type": job["folder_type"],
            "source": job["source"],
            "status": "queued",
            "bytes_done": 0,
            "bytes_total": j.get("size_bytes"),
            "queued_at": time.time(),
            "_job": job,  # for resume after pause
        }
        await _queue.put(job)
        ids.append(jid)
    return ids


def _set_control(job_id: str, action: str) -> dict:
    """Set cancel/pause for a job. Affects running (via loop check)
    and waiting (via worker pre-check) jobs."""
    state = _jobs.get(job_id)
    if state is None:
        return {"error": f"Unknown job id: {job_id}"}
    if state["status"] in ("done", "skipped_exists", "cancelled"):
        return {"id": job_id, "status": state["status"], "note": "already finished"}
    state["control"] = action
    # A waiting job will never reach a chunk loop -> set status immediately
    # if it is still queued (the worker confirms it when picked up).
    return {"id": job_id, "control": action, "status": state["status"]}


def cancel(job_id: str) -> dict:
    return _set_control(job_id, "cancel")


def pause(job_id: str) -> dict:
    return _set_control(job_id, "pause")


async def resume(job_id: str, cfg_loader) -> dict:
    """Re-enqueue a paused job (uses the existing .part
    via the resume mechanism)."""
    state = _jobs.get(job_id)
    if state is None:
        return {"error": f"Unknown job id: {job_id}"}
    if state["status"] != "paused":
        return {"error": f"Job is not paused (status: {state['status']})"}
    job = state.get("_job")
    if not job:
        return {"error": "Original job data unavailable, cannot resume"}
    state.pop("control", None)
    state["status"] = "queued"
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    _ensure_workers(cfg_loader)
    await _queue.put(job)
    return {"id": job_id, "status": "queued"}


def status() -> dict[str, Any]:
    jobs = []
    for s in sorted(_jobs.values(), key=lambda s: s["queued_at"]):
        jobs.append({k: v for k, v in s.items() if k not in ("_job",)})
    return {"jobs": jobs}
