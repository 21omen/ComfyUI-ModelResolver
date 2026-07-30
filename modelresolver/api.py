"""HTTP-Routen des ModelResolvers auf dem ComfyUI PromptServer.

Schritt 1: nur /modelresolver/ping als Lebenszeichen.
Spaeter kommen hinzu:
  POST /modelresolver/analyze   Workflow-JSON -> fehlende Modelle
  POST /modelresolver/resolve   fehlende Modelle -> Kandidaten
  POST /modelresolver/download  bestaetigte Kandidaten -> Download-Queue
"""

from aiohttp import web

from . import __version__


def register_routes() -> None:
    """Routen am laufenden PromptServer registrieren."""
    # Import hier statt auf Modulebene: `server` existiert nur innerhalb
    # eines laufenden ComfyUI-Prozesses.
    from server import PromptServer

    routes = PromptServer.instance.routes

    @routes.get("/modelresolver/ping")
    async def ping(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "extension": "ComfyUI-ModelResolver",
                "version": __version__,
            }
        )

    @routes.post("/modelresolver/analyze")
    async def analyze(request: web.Request) -> web.Response:
        """Workflow im API-/Prompt-Format -> fehlende Modelle.

        Akzeptiert entweder direkt den API-Export ({node_id: {...}})
        oder einen Wrapper {"prompt": {...}}.
        """
        from . import analysis

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response(
                {"error": "Body is not valid JSON"}, status=400
            )

        prompt = body.get("prompt") if isinstance(body, dict) and "prompt" in body else body
        if not isinstance(prompt, dict) or not prompt:
            return web.json_response(
                {"error": "Expected workflow in API format (node_id -> node)"},
                status=400,
            )
        # Grobe Formatpruefung: Werte muessen Node-Dicts mit class_type sein
        sample = next(iter(prompt.values()))
        if not (isinstance(sample, dict) and "class_type" in sample):
            return web.json_response(
                {
                    "error": (
                        "This looks like the frontend workflow format. "
                        "Please use the API export (Workflow -> Export (API))."
                    )
                },
                status=400,
            )

        try:
            result = analysis.analyze_prompt(prompt)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": f"Analysis failed: {exc}"}, status=500)
        return web.json_response(result)

    @routes.post("/modelresolver/resolve")
    async def resolve(request: web.Request) -> web.Response:
        """Missing-Liste (aus /analyze) -> verifizierte Download-Kandidaten."""
        from . import config as cfg_mod
        from . import resolver

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "Body is not valid JSON"}, status=400)

        missing = body.get("missing") if isinstance(body, dict) else None
        if not isinstance(missing, list) or not missing:
            return web.json_response(
                {"error": "Erwarte {\"missing\": [...]} aus /modelresolver/analyze"},
                status=400,
            )
        cfg = cfg_mod.load_config()
        try:
            results = await resolver.resolve_missing(missing, cfg)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": f"Resolution failed: {exc}"}, status=500)
        return web.json_response({
            "results": results,
            "config_path": cfg_mod.config_path(),
            "include_nsfw": cfg["include_nsfw"],
        })

    @routes.post("/modelresolver/download")
    async def download(request: web.Request) -> web.Response:
        """Bestaetigte Kandidaten in die Download-Queue einreihen.

        Body: {"jobs": [{filename, folder_type, download_url | source_path,
                          sha256?, md5?, size_bytes?, source?}, ...]}
        """
        from . import config as cfg_mod
        from . import downloader

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "Body is not valid JSON"}, status=400)
        jobs = body.get("jobs") if isinstance(body, dict) else None
        if not isinstance(jobs, list) or not jobs:
            return web.json_response({"error": "Erwarte {\"jobs\": [...]}"}, status=400)
        for j in jobs:
            if not (j.get("filename") and j.get("folder_type")):
                return web.json_response(
                    {"error": "Each job needs filename and folder_type"},
                    status=400,
                )
            if not (j.get("download_url") or j.get("source_path")):
                return web.json_response(
                    {"error": "Each job needs download_url or source_path"},
                    status=400,
                )
        ids = await downloader.enqueue(jobs, cfg_mod.load_config)
        return web.json_response({"job_ids": ids})

    @routes.get("/modelresolver/download/status")
    async def download_status(request: web.Request) -> web.Response:
        from . import downloader

        return web.json_response(downloader.status())

    @routes.post("/modelresolver/download/control")
    async def download_control(request: web.Request) -> web.Response:
        """Laufende/wartende Downloads steuern.

        Body: {"job_id": "...", "action": "cancel"|"pause"|"resume"}
        - cancel: abbrechen, .part-Rest verwerfen
        - pause:  anhalten, .part behalten (spaeter fortsetzbar)
        - resume: pausierten Job wieder einreihen (nutzt Resume)
        """
        from . import config as cfg_mod
        from . import downloader

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "Body is not valid JSON"}, status=400)
        job_id = body.get("job_id")
        action = body.get("action")
        if not job_id or action not in ("cancel", "pause", "resume"):
            return web.json_response(
                {"error": "job_id and action (cancel|pause|resume) required"},
                status=400,
            )
        if action == "cancel":
            return web.json_response(downloader.cancel(job_id))
        if action == "pause":
            return web.json_response(downloader.pause(job_id))
        result = await downloader.resume(job_id, cfg_mod.load_config)
        return web.json_response(result)

    @routes.get("/modelresolver/folders")
    async def folders(request: web.Request) -> web.Response:
        """Registrierte Modell-Ordnertypen (aus folder_paths) fuer das UI."""
        try:
            import folder_paths
            names = sorted(folder_paths.folder_names_and_paths.keys())
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc), "folders": []}, status=500)
        return web.json_response({"folders": names})

    @routes.post("/modelresolver/verify")
    async def verify(request: web.Request) -> web.Response:
        """Lokale Datei per Hash gegen erwartete Hashes pruefen.

        Body: {folder_type, local_filename, sha256?, md5?}
        """
        from . import verify as verify_mod

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "Body is not valid JSON"}, status=400)
        ft = body.get("folder_type")
        lf = body.get("local_filename")
        if not ft or not lf:
            return web.json_response(
                {"error": "folder_type and local_filename are required"}, status=400)
        expected = {
            "sha256": (body.get("sha256") or "").lower(),
            "md5": (body.get("md5") or "").lower(),
        }
        try:
            result = await verify_mod.verify_local(ft, lf, expected)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": f"Verification failed: {exc}"},
                                     status=500)
        return web.json_response(result)
