"""HTTP routes of the ModelResolver on the ComfyUI PromptServer.

Step 1: only /modelresolver/ping as a liveness check.
Added later:
  POST /modelresolver/analyze   Workflow JSON -> missing models
  POST /modelresolver/resolve   Missing models -> candidates
  POST /modelresolver/download  Confirmed candidates -> download queue
  POST /modelresolver/nodes/analyze  Workflow node classes -> missing packs
  POST /modelresolver/nodes/install  Registry pack -> custom_nodes + requirements
"""

from aiohttp import web

from . import __version__


def register_routes() -> None:
    """Register routes on the running PromptServer."""
    # Import here instead of at module level: `server` only exists inside
    # a running ComfyUI process.
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
        """Workflow in API/prompt format -> missing models.

        Accepts either the API export directly ({node_id: {...}})
        or a wrapper {"prompt": {...}}.
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
        # Rough format check: values must be node dicts with class_type
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
        """Missing list (from /analyze) -> verified download candidates."""
        from . import config as cfg_mod
        from . import resolver

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "Body is not valid JSON"}, status=400)

        missing = body.get("missing") if isinstance(body, dict) else None
        if not isinstance(missing, list) or not missing:
            return web.json_response(
                {"error": "Expected {\"missing\": [...]} from /modelresolver/analyze"},
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

    @routes.post("/modelresolver/nodes/analyze")
    async def analyze_nodes(request: web.Request) -> web.Response:
        """Find missing custom-node classes and their Registry packs."""
        from . import config as cfg_mod
        from . import nodepacks

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "Body is not valid JSON"}, status=400)
        references = body.get("nodes") if isinstance(body, dict) else None
        if not isinstance(references, list):
            return web.json_response(
                {
                    "error": (
                        'Expected {"nodes": [{class_type, node_id?, cnr_id?, '
                        "aux_id?, version?}]}"
                    )
                },
                status=400,
            )
        try:
            cfg = cfg_mod.load_config()
            result = await nodepacks.find_missing_node_packs(
                references, int(cfg["request_timeout"])
            )
        except nodepacks.NodePackError as exc:
            return web.json_response({"error": str(exc)}, status=exc.status)
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"error": f"Custom-node analysis failed: {exc}"}, status=500
            )
        return web.json_response(result)

    @routes.post("/modelresolver/nodes/install")
    async def install_node_pack(request: web.Request) -> web.Response:
        """Install one confirmed Comfy Registry pack and its requirements.txt."""
        from . import config as cfg_mod
        from . import nodepacks

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "Body is not valid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "Expected an object body"}, status=400)
        try:
            cfg = cfg_mod.load_config()
            result = await nodepacks.install_node_pack(
                body.get("pack_id"),
                body.get("version"),
                int(cfg["request_timeout"]),
            )
        except nodepacks.NodePackError as exc:
            return web.json_response({"error": str(exc)}, status=exc.status)
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"error": f"Custom-node installation failed: {exc}"}, status=500
            )
        return web.json_response(result)

    @routes.post("/modelresolver/download")
    async def download(request: web.Request) -> web.Response:
        """Enqueue confirmed candidates into the download queue.

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
            return web.json_response({"error": "Expected {\"jobs\": [...]}"}, status=400)
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
        """Control running/pending downloads.

        Body: {"job_id": "...", "action": "cancel"|"pause"|"resume"}
        - cancel: abort, discard remaining .part file
        - pause:  halt, keep .part (resumable later)
        - resume: re-enqueue paused job (uses resume)
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
        """Registered model folder types (from folder_paths) for the UI."""
        try:
            import folder_paths
            names = sorted(folder_paths.folder_names_and_paths.keys())
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc), "folders": []}, status=500)
        return web.json_response({"folders": names})

    @routes.post("/modelresolver/verify")
    async def verify(request: web.Request) -> web.Response:
        """Check local file against expected hashes.

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
