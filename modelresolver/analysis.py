"""Workflow analysis: active graph -> models truly missing.

Operates on the API/prompt format (export "Save (API Format)" or
app.graphToPrompt()). This format is already ComfyUI's execution view:
muted nodes are absent, bypass is wired through.

Building on that:
1. Reachability: backward from all OUTPUT_NODEs -> only reachable
   nodes generate model requirements (dead islands don't count).
2. Widget detection: evaluate INPUT_TYPES() per node class; combo inputs
   whose options list matches a folder_paths file list are
   model references. This also yields the target folder type.
3. Missing = value not in the current options list. Additional check:
   same basename present locally under a different path -> "found_as"
   (rename/remap instead of download).
4. Nodes that arrived in the prompt but are NOT reachable from an output
   are reported separately as "not_connected". They are never downloaded,
   but ComfyUI still marks them red when their value is unknown - listing
   them explains that mismatch instead of silently dropping them.
"""

from __future__ import annotations

import os
import re
from typing import Any

MODEL_EXTS = {
    ".safetensors", ".sft", ".ckpt", ".pt", ".pt2", ".pth",
    ".bin", ".gguf", ".onnx", ".vae",
}

# Browser/OS duplicate suffixes that only appear locally: " (2)",
# " copy", " - Kopie". A workflow author with " (2)" in the name
# downloaded the file twice - locally it exists under the clean name.
_LOCAL_SUFFIX = re.compile(r"(\s*\(\d+\)|\s*-?\s*(copy|kopie))+$", re.IGNORECASE)


def clean_local_suffix(filename: str) -> str:
    """Remove local duplicate suffixes from stem (extension unchanged)."""
    base = os.path.basename(str(filename).replace("\\", "/"))
    stem, ext = os.path.splitext(base)
    return _LOCAL_SUFFIX.sub("", stem).strip() + ext

# Fallback for when the options list cannot be matched to any
# folder_paths list (e.g. because the local folder is completely empty).
# Note: deliberately only unambiguous names - "clip_name" is intentionally
# absent, since it is used both by CLIPLoader (text_encoders) and
# CLIPVisionLoader (clip_vision); such cases are resolved by options matching.
INPUT_NAME_HINTS = {
    "ckpt_name": "checkpoints",
    "lora_name": "loras",
    "vae_name": "vae",
    "unet_name": "diffusion_models",
    "control_net_name": "controlnet",
    "style_model_name": "style_models",
    "gligen_name": "gligen",
    "ipadapter_file": "ipadapter",
}


def _norm(p: Any) -> str:
    """Path normalization for comparisons (Windows workflows: backslashes)."""
    return str(p).replace("\\", "/") if isinstance(p, str) else p


def _folder_index() -> dict[str, list[str]]:
    """Current file lists of all registered model folders."""
    import folder_paths

    idx: dict[str, list[str]] = {}
    for name in list(folder_paths.folder_names_and_paths.keys()):
        try:
            idx[name] = folder_paths.get_filename_list(name)
        except Exception:  # noqa: BLE001 - ignore broken entries
            continue
    return idx


def _match_folder(options: list[str], folder_idx: dict[str, list[str]]) -> str | None:
    """Match a combo options list to a folder_paths folder type.

    Exact set equality wins; otherwise the smallest folder that
    contains all options (some nodes filter their list).
    """
    opt = {_norm(o) for o in options if isinstance(o, str)}
    if not opt:
        return None
    best: tuple[int, str] | None = None
    for name, files in folder_idx.items():
        fset = {_norm(f) for f in files}
        if not fset:
            continue
        if opt == fset:
            return name
        if opt <= fset and (best is None or len(fset) < best[0]):
            best = (len(fset), name)
    return best[1] if best else None


def _combo_options(spec: Any) -> list[str] | None:
    """Extract combo options from an INPUT_TYPES entry, else None."""
    if not isinstance(spec, (tuple, list)) or not spec:
        return None
    head = spec[0]
    if isinstance(head, (list, tuple)):
        return [o for o in head if isinstance(o, str)]
    # Newer V3 notation: ("COMBO", {"options": [...]})
    if head == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
        opts = spec[1].get("options")
        if isinstance(opts, (list, tuple)):
            return [o for o in opts if isinstance(o, str)]
    return None


def _reachable_from_outputs(prompt: dict[str, Any]) -> tuple[set[str], bool]:
    """IDs of all nodes from which an OUTPUT_NODE is reachable.

    Returns: (ids, outputs_found). Without a recognizable output node
    (e.g. unknown custom output), everything is conservatively treated as active.
    """
    import nodes as comfy_nodes

    mappings = comfy_nodes.NODE_CLASS_MAPPINGS
    out_ids = [
        nid
        for nid, nd in prompt.items()
        if getattr(mappings.get(nd.get("class_type")), "OUTPUT_NODE", False)
    ]
    if not out_ids:
        return set(prompt.keys()), False

    seen: set[str] = set()
    stack = list(out_ids)
    while stack:
        nid = stack.pop()
        if nid in seen or nid not in prompt:
            continue
        seen.add(nid)
        for value in (prompt[nid].get("inputs") or {}).values():
            # Link reference in prompt format: [source_node_id, output_slot]
            if (
                isinstance(value, list)
                and len(value) == 2
                and isinstance(value[0], (str, int))
                and isinstance(value[1], int)
            ):
                stack.append(str(value[0]))
    return seen, True


def analyze_prompt(prompt: dict[str, Any]) -> dict[str, Any]:
    """Main analysis. Expects the prompt dict (node_id -> node)."""
    import nodes as comfy_nodes

    mappings = comfy_nodes.NODE_CLASS_MAPPINGS
    folder_idx = _folder_index()
    # Basename -> (folder_type, relpath) for the exact "found_as" check
    basename_idx: dict[str, tuple[str, str]] = {}
    # Cleaned basename -> (folder_type, relpath): detects locally present
    # files that the workflow only references with a " (2)"/copy suffix.
    clean_idx: dict[str, tuple[str, str]] = {}
    for fname, files in folder_idx.items():
        for f in files:
            b = os.path.basename(_norm(f))
            basename_idx.setdefault(b, (fname, f))
            clean_idx.setdefault(clean_local_suffix(b).lower(), (fname, f))

    active, outputs_found = _reachable_from_outputs(prompt)

    missing: list[dict[str, Any]] = []
    available_renamed: list[dict[str, Any]] = []
    present: list[dict[str, Any]] = []
    unknown_nodes: set[str] = set()

    for nid in sorted(active, key=str):
        node = prompt.get(nid) or {}
        ctype = node.get("class_type", "")
        cls = mappings.get(ctype)
        if cls is None:
            unknown_nodes.add(ctype)
            continue
        try:
            itypes = cls.INPUT_TYPES()
        except Exception:  # noqa: BLE001
            continue

        spec_map: dict[str, Any] = {}
        for section in ("required", "optional"):
            sec = itypes.get(section)
            if isinstance(sec, dict):
                spec_map.update(sec)

        for iname, value in (node.get("inputs") or {}).items():
            if not isinstance(value, str):
                continue  # Links and numbers are not file references
            options = _combo_options(spec_map.get(iname))
            if options is None:
                continue
            norm_val = _norm(value)
            norm_opts = {_norm(o) for o in options}
            entry = {
                "filename": value,
                "node_id": nid,
                "class_type": ctype,
                "input_name": iname,
            }
            if norm_val in norm_opts:
                folder = _match_folder(options, folder_idx)
                if folder is not None:
                    # Only list real model references - other combos
                    # (sampler, scheduler, ...) are noise here.
                    entry["folder_type"] = folder
                    present.append(entry)
                continue
            # Not in the list -> only relevant if it looks like a
            # model file (otherwise: other string combos)
            ext = os.path.splitext(norm_val)[1].lower()
            if ext not in MODEL_EXTS:
                continue
            folder = _match_folder(options, folder_idx) or INPUT_NAME_HINTS.get(iname)
            entry["folder_type"] = folder
            base = os.path.basename(norm_val)
            # 1) Exact same basename present locally (possibly a different folder)
            #    -> present, no download. User selects the file in the node.
            found = basename_idx.get(base)
            if found is not None:
                entry["available_as"] = {"folder_type": found[0], "filename": found[1]}
                entry["match"] = "exact"
                available_renamed.append(entry)
                continue
            # 2) Present locally after suffix cleanup: the workflow
            #    references " (2)"/copy, the file exists under a clean name.
            clean_hit = clean_idx.get(clean_local_suffix(base).lower())
            if clean_hit is not None:
                entry["available_as"] = {
                    "folder_type": clean_hit[0], "filename": clean_hit[1],
                }
                entry["match"] = "renamed"
                available_renamed.append(entry)
                continue
            missing.append(entry)

    not_connected: list[dict[str, Any]] = []
    # Only meaningful if reachability actually ran; without a recognizable
    # output node every node counts as active and this set is empty anyway.
    if outputs_found:
        for nid in sorted(set(prompt.keys()) - active, key=str):
            node = prompt.get(nid) or {}
            ctype = node.get("class_type", "")
            cls = mappings.get(ctype)
            if cls is None:
                continue
            try:
                itypes = cls.INPUT_TYPES()
            except Exception:  # noqa: BLE001
                continue

            spec_map = {}
            for section in ("required", "optional"):
                sec = itypes.get(section)
                if isinstance(sec, dict):
                    spec_map.update(sec)

            for iname, value in (node.get("inputs") or {}).items():
                if not isinstance(value, str):
                    continue
                options = _combo_options(spec_map.get(iname))
                if options is None:
                    continue
                norm_val = _norm(value)
                in_options = norm_val in {_norm(o) for o in options}
                ext = os.path.splitext(norm_val)[1].lower()
                # Skip non-model combos (sampler, scheduler, ...)
                if not in_options and ext not in MODEL_EXTS:
                    continue
                folder = _match_folder(options, folder_idx) or INPUT_NAME_HINTS.get(iname)
                if in_options and folder is None:
                    continue
                base = os.path.basename(norm_val)
                found_locally = (
                    in_options
                    or basename_idx.get(base) is not None
                    or clean_idx.get(clean_local_suffix(base).lower()) is not None
                )
                not_connected.append({
                    "filename": value,
                    "node_id": nid,
                    "class_type": ctype,
                    "input_name": iname,
                    "folder_type": folder,
                    "status": "present" if found_locally else "missing",
                })

    return {
        "missing": missing,
        "available_renamed": available_renamed,
        "present": present,
        "not_connected": not_connected,
        "unknown_node_types": sorted(unknown_nodes),
        "nodes_total": len(prompt),
        "nodes_active": len(active),
        "outputs_found": outputs_found,
    }
