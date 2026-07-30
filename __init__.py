"""ComfyUI-ModelResolver

Erkennt fehlende Modelle im aktiven Workflow-Graphen und loest sie
ueber Civitai / Hugging Face auf.

Schritt 1 (Geruest): registriert nur die Server-Routen und das
Frontend-Verzeichnis. Keine eigenen Nodes noetig - NODE_CLASS_MAPPINGS
bleibt leer, die Extension arbeitet rein ueber API-Routen + Frontend.
"""

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

# Server-Routen registrieren. Import-Guard, damit das Paket auch in
# Umgebungen ohne laufenden PromptServer (z.B. Unit-Tests) importierbar bleibt.
try:
    from .modelresolver import api as _api
    from .modelresolver import config as _config
    _config.load_config()  # legt config.json mit Defaults an, falls fehlend
    _api.register_routes()
    print("[ModelResolver] Geladen, Routen registriert; Config:", _config.config_path())
except Exception as exc:  # noqa: BLE001 - beim Laden nie ComfyUI abschiessen
    print(f"[ModelResolver] Routen-Registrierung uebersprungen: {exc}")
