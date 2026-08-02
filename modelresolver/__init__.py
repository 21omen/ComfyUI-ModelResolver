"""Backend modules of the ModelResolver.

Structure (filled in over the following steps):
- api.py        HTTP routes on the PromptServer
- analysis.py   Workflow analysis: active graph -> missing models
- resolver.py   Source resolution: Civitai / HF / CivArchive
- downloader.py Download queue with SHA256 verification
"""

__version__ = "0.11.1"
