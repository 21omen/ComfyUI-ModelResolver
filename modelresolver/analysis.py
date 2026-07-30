"""Workflow-Analyse: aktiver Graph -> wirklich fehlende Modelle.

Arbeitet auf dem API-/Prompt-Format (Export "Save (API Format)" bzw.
app.graphToPrompt()). Dieses Format ist bereits ComfyUIs Ausfuehrungssicht:
muted Nodes fehlen, Bypass ist durchverdrahtet.

Darauf aufbauend:
1. Reachability: rueckwaerts von allen OUTPUT_NODEs -> nur erreichbare
   Nodes erzeugen Modell-Anforderungen (tote Inseln zaehlen nicht).
2. Widget-Erkennung: pro Node-Klasse INPUT_TYPES() auswerten; Combo-Inputs,
   deren Optionsliste einer folder_paths-Dateiliste entspricht, sind
   Modellreferenzen. Das liefert zugleich den Zielordner-Typ.
3. Fehlend = Wert nicht in der aktuellen Optionsliste. Zusatzcheck:
   gleicher Basename unter anderem Pfad lokal vorhanden -> "found_as"
   (Umbenennen/Ummappen statt Download).
"""

from __future__ import annotations

import os
import re
from typing import Any

MODEL_EXTS = {
    ".safetensors", ".sft", ".ckpt", ".pt", ".pt2", ".pth",
    ".bin", ".gguf", ".onnx", ".vae",
}

# Browser-/OS-Duplikat-Anhaengsel, die NUR lokal entstehen: " (2)",
# " copy", " - Kopie". Ein Workflow-Autor mit " (2)" im Namen hat die
# Datei doppelt heruntergeladen - lokal liegt sie unter dem sauberen Namen.
_LOCAL_SUFFIX = re.compile(r"(\s*\(\d+\)|\s*-?\s*(copy|kopie))+$", re.IGNORECASE)


def clean_local_suffix(filename: str) -> str:
    """Entfernt lokale Duplikat-Anhaengsel aus dem Stem (Endung bleibt)."""
    base = os.path.basename(str(filename).replace("\\", "/"))
    stem, ext = os.path.splitext(base)
    return _LOCAL_SUFFIX.sub("", stem).strip() + ext

# Fallback, wenn die Optionsliste keiner folder_paths-Liste zugeordnet
# werden kann (z.B. weil der lokale Ordner komplett leer ist).
# Achtung: bewusst nur eindeutige Namen - "clip_name" fehlt absichtlich,
# da er sowohl von CLIPLoader (text_encoders) als auch CLIPVisionLoader
# (clip_vision) verwendet wird; solche Faelle loest das Options-Matching.
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
    """Pfad-Normalisierung fuer Vergleiche (Windows-Workflows: Backslashes)."""
    return str(p).replace("\\", "/") if isinstance(p, str) else p


def _folder_index() -> dict[str, list[str]]:
    """Aktuelle Dateilisten aller registrierten Modellordner."""
    import folder_paths

    idx: dict[str, list[str]] = {}
    for name in list(folder_paths.folder_names_and_paths.keys()):
        try:
            idx[name] = folder_paths.get_filename_list(name)
        except Exception:  # noqa: BLE001 - kaputte Eintraege ignorieren
            continue
    return idx


def _match_folder(options: list[str], folder_idx: dict[str, list[str]]) -> str | None:
    """Ordnet eine Combo-Optionsliste einem folder_paths-Ordnertyp zu.

    Exakte Mengengleichheit gewinnt; sonst kleinster Ordner, der alle
    Optionen enthaelt (manche Nodes filtern ihre Liste).
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
    """Extrahiert Combo-Optionen aus einem INPUT_TYPES-Eintrag, sonst None."""
    if not isinstance(spec, (tuple, list)) or not spec:
        return None
    head = spec[0]
    if isinstance(head, (list, tuple)):
        return [o for o in head if isinstance(o, str)]
    # Neuere V3-Schreibweise: ("COMBO", {"options": [...]})
    if head == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
        opts = spec[1].get("options")
        if isinstance(opts, (list, tuple)):
            return [o for o in opts if isinstance(o, str)]
    return None


def _reachable_from_outputs(prompt: dict[str, Any]) -> tuple[set[str], bool]:
    """IDs aller Nodes, von denen aus ein OUTPUT_NODE erreichbar ist.

    Rueckgabe: (ids, outputs_gefunden). Ohne erkennbaren Output-Node
    (z.B. unbekannter Custom-Output) gilt konservativ alles als aktiv.
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
            # Link-Referenz im Prompt-Format: [quell_node_id, output_slot]
            if (
                isinstance(value, list)
                and len(value) == 2
                and isinstance(value[0], (str, int))
                and isinstance(value[1], int)
            ):
                stack.append(str(value[0]))
    return seen, True


def analyze_prompt(prompt: dict[str, Any]) -> dict[str, Any]:
    """Hauptanalyse. Erwartet das Prompt-Dict (node_id -> node)."""
    import nodes as comfy_nodes

    mappings = comfy_nodes.NODE_CLASS_MAPPINGS
    folder_idx = _folder_index()
    # Basename -> (ordnertyp, relpfad) fuer den exakten "found_as"-Check
    basename_idx: dict[str, tuple[str, str]] = {}
    # Bereinigter Basename -> (ordnertyp, relpfad): erkennt lokal vorhandene
    # Dateien, die der Workflow nur mit " (2)"/Copy-Suffix referenziert.
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
                continue  # Links und Zahlen sind keine Dateireferenzen
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
                    # Nur echte Modellreferenzen listen - sonstige Combos
                    # (Sampler, Scheduler, ...) sind hier Rauschen.
                    entry["folder_type"] = folder
                    present.append(entry)
                continue
            # Nicht in der Liste -> nur relevant, wenn es wie eine
            # Modelldatei aussieht (sonst: sonstige String-Combos)
            ext = os.path.splitext(norm_val)[1].lower()
            if ext not in MODEL_EXTS:
                continue
            folder = _match_folder(options, folder_idx) or INPUT_NAME_HINTS.get(iname)
            entry["folder_type"] = folder
            base = os.path.basename(norm_val)
            # 1) Exakt gleicher Basename lokal vorhanden (evtl. anderer Ordner)
            #    -> vorhanden, kein Download. Nutzer waehlt die Datei im Node.
            found = basename_idx.get(base)
            if found is not None:
                entry["available_as"] = {"folder_type": found[0], "filename": found[1]}
                entry["match"] = "exact"
                available_renamed.append(entry)
                continue
            # 2) Lokal vorhanden nach Suffix-Bereinigung: der Workflow
            #    referenziert " (2)"/Copy, die Datei liegt sauber benannt vor.
            clean_hit = clean_idx.get(clean_local_suffix(base).lower())
            if clean_hit is not None:
                entry["available_as"] = {
                    "folder_type": clean_hit[0], "filename": clean_hit[1],
                }
                entry["match"] = "renamed"
                available_renamed.append(entry)
                continue
            missing.append(entry)

    return {
        "missing": missing,
        "available_renamed": available_renamed,
        "present": present,
        "unknown_node_types": sorted(unknown_nodes),
        "nodes_total": len(prompt),
        "nodes_active": len(active),
        "outputs_found": outputs_found,
    }
