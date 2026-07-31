"""Source resolution: missing models -> verified download candidates.

Empirically validated pipeline (see test conversations against live APIs):
- Civitai search matches model names, never filenames -> query distillation
  from filename, staged (full stem -> core tokens -> longest token).
- `nsfw=true` is required to see NSFW models in search results
  (opt-in via config.include_nsfw); domain (.com/.red) is irrelevant server-side.
- The API delivers transient errors -> retry with backoff, with strict
  separation of "error" vs "empty result".
- A candidate counts only on exact-filename match (case-insensitive,
  with notation) in the file lists of model versions.
- The target folder type from analysis filters false candidates
  (loras reference -> no 19GB checkpoint).
- Dedup via SHA256: same file on Civitai and HF = one candidate
  with multiple sources.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Awaitable, Callable

CIVITAI_BASE = "https://civitai.com/api/v1"
HF_BASE = "https://huggingface.co"

# folder_paths folder type -> plausible Civitai model types
CIVITAI_TYPE_MAP: dict[str, set[str]] = {
    "loras": {"LORA", "LoCon", "DoRA", "Lycoris"},
    "checkpoints": {"Checkpoint"},
    "diffusion_models": {"Checkpoint"},
    "unet": {"Checkpoint"},
    "vae": {"VAE"},
    "controlnet": {"Controlnet"},
    "upscale_models": {"Upscaler"},
    "embeddings": {"TextualInversion"},
}

# Tokens to remove from core query (versions/formats)
_NOISE_TOKEN = re.compile(
    r"^(v?\d+([._]\d+)*|fp8|fp16|fp32|bf16|e4m3fn|e5m2|gguf|q\d.*|scaled"
    r"|\d+step[s]?|\d+|final\d*|noobai|il|xl)$",
    re.IGNORECASE,
)

Transport = Callable[[str, dict, dict, int], Awaitable[tuple[int, Any]]]

# Browser/OS suffixes that only appear locally and never on sources:
# " (2)", " (13)", " copy", " - Copy", " - Kopie", " kopie".
# These are removed before search AND during exact-match comparison -
# but NOT during sorting (target name remains as specified in workflow).
_LOCAL_SUFFIX = re.compile(
    r"(\s*\(\d+\)|\s*-?\s*(copy|kopie))+$", re.IGNORECASE
)


def clean_local_suffix(filename: str) -> str:
    """Remove browser/OS duplicate suffixes from stem (extension unchanged)."""
    base = os.path.basename(str(filename).replace("\\", "/"))
    stem, ext = os.path.splitext(base)
    return _LOCAL_SUFFIX.sub("", stem).strip() + ext


async def _default_transport(
    url: str, params: dict, headers: dict, timeout: int
) -> tuple[int, Any]:
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, params=params, headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:  # noqa: BLE001
                data = None
            return resp.status, data


async def _get_with_retry(
    transport: Transport, url: str, params: dict, headers: dict,
    timeout: int, attempts: int = 3,
) -> tuple[str, Any]:
    """-> ("ok", data) | ("error", description). Retry on 429/5xx/network errors.

    Important: 200 with unexpected body counts as error, not empty -
    Civitai API provably delivers transient responses without `items`.
    """
    last = "unknown error"
    for attempt in range(attempts):
        try:
            status, data = await transport(url, params, headers, timeout)
        except Exception as exc:  # noqa: BLE001
            last = f"Network error: {exc}"
        else:
            if status == 200 and data is not None:
                return "ok", data
            last = f"HTTP {status}"
            if status not in (429,) and not 500 <= status < 600 and data is None:
                last = f"HTTP {status}, no JSON"
            if 400 <= status < 500 and status != 429:
                return "error", last  # Client error: retry pointless
        if attempt < attempts - 1:
            await asyncio.sleep(1 + attempt * 2)
    return "error", last


def build_queries(filename: str) -> list[str]:
    """Distill staged search queries from a filename.

    Browser/OS suffixes (' (2)', ' copy', ...) are removed first -
    they exist only locally, never on Civitai/HF.
    """
    cleaned = clean_local_suffix(filename)
    stem = os.path.splitext(os.path.basename(cleaned.replace("\\", "/")))[0]
    spaced = re.sub(r"[_\-.]+", " ", stem).strip()
    tokens = [t for t in spaced.split() if t]
    core = [t for t in tokens if not _NOISE_TOKEN.match(t)]
    queries = [spaced]
    if core and core != tokens:
        queries.append(" ".join(core))
    if len(core) > 1:
        queries.append(max(core, key=len))
    elif len(tokens) > 1:
        queries.append(max(tokens, key=len))
    # Dedup while preserving order
    seen: set[str] = set()
    return [q for q in queries if q and not (q.lower() in seen or seen.add(q.lower()))]


def _type_match(folder_type: str | None, civitai_type: str | None) -> bool | None:
    """True/False for known mapping, None if not determinable."""
    if not folder_type or not civitai_type:
        return None
    expected = CIVITAI_TYPE_MAP.get(folder_type)
    if expected is None:
        return None
    return civitai_type in expected


# --------------------------------------------------------------- Civitai ---

def _civitai_headers(cfg: dict) -> dict:
    key = cfg.get("civitai_api_key") or ""
    return {"Authorization": f"Bearer {key}"} if key else {}


def _civitai_extract(data: Any, target: str, folder_type: str | None) -> list[dict]:
    out: list[dict] = []
    tbase = os.path.basename(target.replace("\\", "/"))
    tclean = clean_local_suffix(tbase).lower()
    for item in (data or {}).get("items", []) or []:
        mtype = item.get("type")
        tm = _type_match(folder_type, mtype)
        if tm is False:
            continue  # loras reference -> hard-exclude checkpoints etc.
        for ver in item.get("modelVersions", []) or []:
            for f in ver.get("files", []) or []:
                fname = str(f.get("name", ""))
                fbase = os.path.basename(fname)
                # Match on cleaned name: local " (2)" must not block
                # the hit.
                if clean_local_suffix(fbase).lower() != tclean:
                    continue
                sha = ((f.get("hashes") or {}).get("SHA256") or "").lower()
                size_kb = f.get("sizeKB")
                out.append({
                    "source": "civitai",
                    "model_name": item.get("name"),
                    "model_id": item.get("id"),
                    "version_name": ver.get("name"),
                    "file_name": fname,
                    "size_bytes": int(size_kb * 1024) if size_kb else None,
                    "sha256": sha or None,
                    "download_url": f.get("downloadUrl"),
                    "nsfw": bool(item.get("nsfw")),
                    "civitai_type": mtype,
                    "type_match": tm,
                    "case_exact": fbase == tbase,
                })
    return out


async def _resolve_civitai(
    entry: dict, cfg: dict, transport: Transport, errors: list[str],
    queries: list[str],
) -> list[dict]:
    headers = _civitai_headers(cfg)
    params_base: dict[str, Any] = {"limit": cfg["search_limit"]}
    if cfg.get("include_nsfw"):
        params_base["nsfw"] = "true"
    candidates: list[dict] = []
    for q in queries:
        status, data = await _get_with_retry(
            transport, f"{CIVITAI_BASE}/models",
            {**params_base, "query": q}, headers, cfg["request_timeout"],
        )
        if status == "error":
            errors.append(f"civitai '{q}': {data}")
            continue
        candidates = _civitai_extract(data, entry["filename"], entry.get("folder_type"))
        if candidates:
            break  # Staged: first query with verified hit wins
    return candidates


# --------------------------------------------------------- Hugging Face ---

def _hf_headers(cfg: dict) -> dict:
    tok = cfg.get("hf_token") or ""
    return {"Authorization": f"Bearer {tok}"} if tok else {}


async def _resolve_hf(
    entry: dict, cfg: dict, transport: Transport, errors: list[str],
    queries: list[str],
) -> list[dict]:
    headers = _hf_headers(cfg)
    target = os.path.basename(entry["filename"].replace("\\", "/"))
    tclean = clean_local_suffix(target).lower()
    out: list[dict] = []
    for q in queries:
        status, repos = await _get_with_retry(
            transport, f"{HF_BASE}/api/models",
            {"search": q, "limit": cfg["search_limit"], "full": "true"},
            headers, cfg["request_timeout"],
        )
        if status == "error":
            errors.append(f"hf-search '{q}': {repos}")
            continue
        for repo in repos or []:
            repo_id = repo.get("id") or repo.get("modelId")
            siblings = repo.get("siblings") or []
            hit_paths = [
                s.get("rfilename") for s in siblings
                if clean_local_suffix(os.path.basename(str(s.get("rfilename", "")))).lower()
                == tclean
            ]
            if not repo_id or not hit_paths:
                continue
            # Detail query for size + SHA256 (LFS-oid) via tree endpoint
            st, tree = await _get_with_retry(
                transport, f"{HF_BASE}/api/models/{repo_id}/tree/main",
                {"recursive": "true"}, headers, cfg["request_timeout"],
            )
            meta = {}
            if st == "ok":
                for node in tree or []:
                    if node.get("path") in hit_paths:
                        meta[node["path"]] = node
            for path in hit_paths:
                node = meta.get(path, {})
                lfs = node.get("lfs") or {}
                out.append({
                    "source": "huggingface",
                    "model_name": repo_id,
                    "model_id": repo_id,
                    "version_name": None,
                    "file_name": path,
                    "size_bytes": node.get("size") or lfs.get("size"),
                    "sha256": (lfs.get("oid") or "").lower() or None,
                    "download_url": f"{HF_BASE}/{repo_id}/resolve/main/{path}",
                    "nsfw": None,  # HF has no NSFW flag in Civitai sense
                    "civitai_type": None,
                    "type_match": None,
                    "case_exact": os.path.basename(path) == target,
                })
        if out:
            break
    return out


# ------------------------------------------------------------ Orchestration

def _dedup_by_sha(cands: list[dict]) -> list[dict]:
    """Same SHA256 from multiple sources -> one candidate, sources bundled."""
    by_sha: dict[str, dict] = {}
    result: list[dict] = []
    for c in cands:
        sha = c.get("sha256")
        if not sha:
            result.append({**c, "also_available": []})
            continue
        if sha in by_sha:
            by_sha[sha]["also_available"].append(
                {"source": c["source"], "model_name": c["model_name"],
                 "download_url": c["download_url"]}
            )
        else:
            by_sha[sha] = {**c, "also_available": []}
            result.append(by_sha[sha])
    return result


def _confidence(c: dict) -> str:
    # Exact filename without type contradiction = high. type_match None
    # (HF provides no model type) must not penalize - exact match
    # plus SHA256 is the real verification.
    if c.get("case_exact") and c.get("type_match") is not False:
        return "high"
    if c.get("case_exact") or c.get("type_match") is True:
        return "medium"
    return "low"


async def resolve_missing(
    missing: list[dict], cfg: dict, transport: Transport | None = None
) -> list[dict]:
    transport = transport or _default_transport
    results: list[dict] = []
    for entry in missing:
        queries = build_queries(entry["filename"])
        errors: list[str] = []
        cands: list[dict] = []
        if cfg["sources"].get("civitai"):
            cands += await _resolve_civitai(entry, cfg, transport, errors, queries)
        if cfg["sources"].get("huggingface"):
            cands += await _resolve_hf(entry, cfg, transport, errors, queries)
        cands = _dedup_by_sha(cands)
        for c in cands:
            c["confidence"] = _confidence(c)
        cands.sort(key=lambda c: {"high": 0, "medium": 1, "low": 2}[c["confidence"]])
        if cands:
            status = "resolved"
        elif errors and _all_failed(errors, queries, cfg):
            # Every source*query failed -> "not found" would not be
            # verifiable. Error ≠ empty result.
            status = "error"
        else:
            status = "not_found"
        results.append({
            "filename": entry["filename"],
            "folder_type": entry.get("folder_type"),
            "status": status,
            "candidates": cands,
            "queries_tried": queries,
            "errors": errors,
        })
    return results


def _all_failed(errors: list[str], queries: list[str], cfg: dict) -> bool:
    """True if every source*query failed (no single clean empty result) -
    then "not found" is not verifiable."""
    active_sources = sum(1 for v in cfg["sources"].values() if v)
    return len(errors) >= active_sources * len(queries)
