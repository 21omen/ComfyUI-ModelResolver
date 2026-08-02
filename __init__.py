"""ComfyUI-ModelResolver

Detects missing models and custom-node packs in ComfyUI workflows.

No custom nodes are needed for the resolver itself: NODE_CLASS_MAPPINGS stays
empty, and the extension works through API routes plus its frontend panel.
"""

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

# Register server routes. Import guard so the package remains importable
# in environments without a running PromptServer (e.g. unit tests).
try:
    from .modelresolver import api as _api
    from .modelresolver import config as _config
    _config.load_config()  # creates config.json with defaults if missing
    _api.register_routes()
    print("[ModelResolver] Loaded, routes registered; Config:", _config.config_path())
except Exception as exc:  # noqa: BLE001 - never take down ComfyUI on load
    print(f"[ModelResolver] Route registration skipped: {exc}")
