"""ModelResolver configuration.

Configuration is stored as config.json in the extension root directory and is
created with defaults on first access. The configuration file is read fresh on
every request - changes to the file do not require a ComfyUI restart.

The Civitai API key should be stored here, never in code.
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
    "hf_token": "",           # Optional, only needed for gated Hugging Face repos
    "include_nsfw": False,     # Opt-in: search Civitai with nsfw=true
    "sources": {"civitai": True, "huggingface": True},
    "search_limit": 20,        # Results per query and source
    "request_timeout": 20,     # Seconds
    "max_parallel_downloads": 2,  # Concurrent downloads (1-8)
}


def load_config() -> dict[str, Any]:
    if not os.path.exists(_CONFIG_PATH):
        save_config(DEFAULTS)
        return dict(DEFAULTS)
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001 - Corrupted file: use defaults, don't crash
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
