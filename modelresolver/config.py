"""Konfiguration des ModelResolvers.

Liegt als config.json im Extension-Wurzelverzeichnis und wird beim ersten
Zugriff mit Defaults angelegt. Wird pro Request frisch gelesen - Aenderungen
an der Datei brauchen keinen ComfyUI-Neustart.

Der Civitai-API-Key gehoert hierhin, niemals in den Code.
"""

from __future__ import annotations

import json
import os
from typing import Any

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"
)

DEFAULTS: dict[str, Any] = {
    "civitai_api_key": "",
    "hf_token": "",           # optional, nur fuer gated Repos noetig
    "include_nsfw": False,     # Opt-in: Civitai-Suche mit nsfw=true
    "sources": {"civitai": True, "huggingface": True},
    "search_limit": 20,        # Treffer pro Query und Quelle
    "request_timeout": 20,     # Sekunden
    "max_parallel_downloads": 2,  # gleichzeitige Downloads (1-8)
}


def load_config() -> dict[str, Any]:
    if not os.path.exists(_CONFIG_PATH):
        save_config(DEFAULTS)
        return dict(DEFAULTS)
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001 - kaputte Datei: Defaults, nicht crashen
        return dict(DEFAULTS)
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in data.items() if k in DEFAULTS})
    if isinstance(data.get("sources"), dict):
        merged["sources"] = {**DEFAULTS["sources"], **data["sources"]}
    return merged


def save_config(cfg: dict[str, Any]) -> None:
    with open(_CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def config_path() -> str:
    return _CONFIG_PATH
