"""Quellen-Aufloesung: fehlende Modelle -> verifizierte Download-Kandidaten.

Empirisch abgesicherte Pipeline (siehe Chat-Tests gegen die Live-APIs):
- Civitai-Suche matcht Modellnamen, nie Dateinamen -> Query-Destillation
  aus dem Dateinamen, gestuft (voller Stem -> Kern-Tokens -> laengster Token).
- `nsfw=true` ist noetig, um NSFW-Modelle in Suchergebnissen zu sehen
  (Opt-in via config.include_nsfw); Domain (.com/.red) ist API-seitig egal.
- Die API liefert transiente Fehler -> Retry mit Backoff, und strikte
  Trennung von "Fehler" und "leeres Ergebnis".
- Ein Kandidat zaehlt nur bei Exact-Filename-Match (case-insensitiv,
  mit Vermerk) in den Dateilisten der Model-Versionen.
- Der Zielordner-Typ aus der Analyse filtert Fehlkandidaten
  (loras-Referenz -> kein 19-GB-Checkpoint).
- Dedup ueber SHA256: gleiche Datei auf Civitai und HF = ein Kandidat
  mit mehreren Quellen.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Awaitable, Callable

CIVITAI_BASE = "https://civitai.com/api/v1"
HF_BASE = "https://huggingface.co"

# folder_paths-Ordnertyp -> plausible Civitai-Modelltypen
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

# Tokens, die fuer die Kern-Query entfernt werden (Versionen/Formate)
_NOISE_TOKEN = re.compile(
    r"^(v?\d+([._]\d+)*|fp8|fp16|fp32|bf16|e4m3fn|e5m2|gguf|q\d.*|scaled"
    r"|\d+step[s]?|\d+|final\d*|noobai|il|xl)$",
    re.IGNORECASE,
)

Transport = Callable[[str, dict, dict, int], Awaitable[tuple[int, Any]]]

# Browser-/OS-Anhaengsel, die NUR lokal entstehen und auf keiner Quelle
# vorkommen: " (2)", " (13)", " copy", " - Copy", " - Kopie", " kopie".
# Diese werden vor der Suche UND beim Exact-Match-Vergleich entfernt -
# aber NICHT beim Einsortieren (Zielname bleibt wie im Workflow).
_LOCAL_SUFFIX = re.compile(
    r"(\s*\(\d+\)|\s*-?\s*(copy|kopie))+$", re.IGNORECASE
)


def clean_local_suffix(filename: str) -> str:
    """Entfernt Browser-/OS-Duplikat-Anhaengsel aus dem Stem (Endung bleibt)."""
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
    """-> ("ok", data) | ("error", beschreibung). Retry bei 429/5xx/Netzfehler.

    Wichtig: 200 mit unerwartetem Body zaehlt als Fehler, nicht als leer -
    die Civitai-API liefert nachweislich transiente Antworten ohne `items`.
    """
    last = "unbekannter Fehler"
    for attempt in range(attempts):
        try:
            status, data = await transport(url, params, headers, timeout)
        except Exception as exc:  # noqa: BLE001
            last = f"Netzwerkfehler: {exc}"
        else:
            if status == 200 and data is not None:
                return "ok", data
            last = f"HTTP {status}"
            if status not in (429,) and not 500 <= status < 600 and data is None:
                last = f"HTTP {status}, kein JSON"
            if 400 <= status < 500 and status != 429:
                return "error", last  # Client-Fehler: Retry sinnlos
        if attempt < attempts - 1:
            await asyncio.sleep(1 + attempt * 2)
    return "error", last


def build_queries(filename: str) -> list[str]:
    """Gestufte Suchqueries aus einem Dateinamen destillieren.

    Browser-/OS-Anhaengsel (' (2)', ' copy', ...) werden zuvor entfernt -
    sie existieren nur lokal, nie auf Civitai/HF.
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
    # Dedup bei Erhalt der Reihenfolge
    seen: set[str] = set()
    return [q for q in queries if q and not (q.lower() in seen or seen.add(q.lower()))]


def _type_match(folder_type: str | None, civitai_type: str | None) -> bool | None:
    """True/False bei bekannter Zuordnung, None wenn nicht beurteilbar."""
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
            continue  # loras-Referenz -> Checkpoints etc. hart aussortieren
        for ver in item.get("modelVersions", []) or []:
            for f in ver.get("files", []) or []:
                fname = str(f.get("name", ""))
                fbase = os.path.basename(fname)
                # Match auf bereinigten Namen: lokales " (2)" darf den
                # Treffer nicht verhindern.
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
            break  # gestuft: erste Query mit verifiziertem Treffer gewinnt
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
            errors.append(f"hf-suche '{q}': {repos}")
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
            # Detail-Abfrage fuer Groesse + SHA256 (LFS-oid) via tree-Endpoint
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
                    "nsfw": None,  # HF kennt kein NSFW-Flag im Civitai-Sinn
                    "civitai_type": None,
                    "type_match": None,
                    "case_exact": os.path.basename(path) == target,
                })
        if out:
            break
    return out


# ------------------------------------------------------------ Orchestrierung

def _dedup_by_sha(cands: list[dict]) -> list[dict]:
    """Gleiche SHA256 aus mehreren Quellen -> ein Kandidat, Quellen gebuendelt."""
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
    # Exakter Dateiname ohne Typ-WIDERSPRUCH = high. type_match None
    # (HF liefert keinen Modelltyp) darf nicht bestrafen - der exakte
    # Match plus SHA256 ist die eigentliche Verifikation.
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
            # Jede Quelle*Query fehlgeschlagen -> "nicht gefunden" waere
            # nicht belegbar. Fehler != leeres Ergebnis.
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
    """True, wenn jede Quelle*Query fehlgeschlagen ist (kein einziges sauberes
    Leerergebnis) - dann ist "not_found" nicht belegbar."""
    active_sources = sum(1 for v in cfg["sources"].values() if v)
    return len(errors) >= active_sources * len(queries)
