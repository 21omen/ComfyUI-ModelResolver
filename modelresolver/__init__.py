"""Backend-Module des ModelResolvers.

Struktur (wird in den folgenden Schritten gefuellt):
- api.py        HTTP-Routen auf dem PromptServer
- analysis.py   Workflow-Analyse: aktiver Graph -> fehlende Modelle
- resolver.py   Quellen-Auflösung: Civitai / HF / CivArchive
- downloader.py Download-Queue mit SHA256-Verifikation
"""

__version__ = "0.9.1"
