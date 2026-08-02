import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from modelresolver import nodepacks


PACK = {
    "id": "example-pack",
    "name": "Example Pack",
    "description": "",
    "repository": "https://github.com/example/example-pack",
    "latest_version": "2.0.0",
    "downloads": 10,
    "publisher": "Example",
    "tags_admin": [],
    "match": "workflow_metadata",
}


class ArchiveTests(unittest.TestCase):
    def test_safe_extract_rejects_parent_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "bad.zip"
            output = Path(temp) / "output"
            output.mkdir()
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("../outside.py", "bad")

            with self.assertRaises(nodepacks.NodePackError):
                nodepacks._safe_extract(archive, output)
            self.assertFalse((Path(temp) / "outside.py").exists())

    def test_safe_extract_writes_regular_files(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "pack.zip"
            output = Path(temp) / "output"
            output.mkdir()
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("pack/__init__.py", "VALUE = 1\n")

            nodepacks._safe_extract(archive, output)

            self.assertEqual(
                (output / "pack" / "__init__.py").read_text(), "VALUE = 1\n"
            )
            self.assertEqual(nodepacks._payload_root(output), output / "pack")


class ResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_frontend_registered_nodes_are_not_reported_missing(self):
        references = [{
            "class_type": "FrontendOnly",
            "node_id": 1,
            "frontend_available": True,
        }]
        with patch.object(nodepacks, "_registered_node_types", return_value=set()):
            result = await nodepacks.find_missing_node_packs(references)

        self.assertEqual(result["missing_node_count"], 0)
        self.assertEqual(result["installed_node_count"], 1)

    async def test_aux_id_requires_exact_registry_repository(self):
        reference = nodepacks._clean_reference({
            "class_type": "GetNode",
            "aux_id": "kijai/ComfyUI-KJNodes",
        })
        registry = {
            "id": "comfyui-kjnodes",
            "name": "ComfyUI-KJNodes",
            "status": "NodeStatusActive",
            "repository": "https://github.com/kijai/ComfyUI-KJNodes.git",
            "publisher": {},
            "latest_version": {
                "version": "1.4.8",
                "status": "NodeVersionStatusActive",
            },
        }
        with patch.object(nodepacks, "_get_json", new=AsyncMock(return_value=registry)) as get:
            result = await nodepacks._resolve_reference(object(), reference, 20)

        self.assertEqual(result["pack"]["id"], "comfyui-kjnodes")
        self.assertEqual(result["pack"]["match"], "workflow_repository")
        self.assertIn("/nodes/ComfyUI-KJNodes", get.await_args.args[1])

    async def test_groups_missing_classes_by_registry_pack(self):
        references = [
            {
                "class_type": "MissingOne",
                "node_id": 1,
                "cnr_id": "example-pack",
                "version": "1.2.3",
            },
            {
                "class_type": "MissingTwo",
                "node_id": 2,
                "cnr_id": "example-pack",
                "version": "1.2.3",
            },
            {"class_type": "Installed", "node_id": 3},
        ]

        async def resolve(_session, reference, _timeout):
            return {**reference, "pack": dict(PACK)}

        with (
            patch.object(nodepacks, "_registered_node_types", return_value={"Installed"}),
            patch.object(nodepacks, "_resolve_reference", side_effect=resolve),
        ):
            result = await nodepacks.find_missing_node_packs(references)

        self.assertEqual(result["missing_node_count"], 2)
        self.assertEqual(result["installed_node_count"], 1)
        self.assertEqual(len(result["packs"]), 1)
        self.assertEqual(result["packs"][0]["node_types"], ["MissingOne", "MissingTwo"])
        self.assertEqual(result["packs"][0]["install_version"], "1.2.3")


class InstallationTests(unittest.IsolatedAsyncioTestCase):
    async def test_requirements_install_is_non_interactive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "requirements.txt").write_text("example==1.0\n")
            process = AsyncMock()
            process.returncode = 0
            process.communicate.return_value = (b"ok", None)

            with patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=process),
            ) as create_process:
                result = await nodepacks._install_requirements(root)

            args = create_process.await_args.args
            self.assertIn("--disable-pip-version-check", args)
            self.assertIn("--no-input", args)
            self.assertEqual(result["status"], "installed")

    async def test_requirements_start_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "requirements.txt").write_text("example==1.0\n")
            with patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(side_effect=OSError("pip unavailable")),
            ):
                result = await nodepacks._install_requirements(root)

            self.assertEqual(result["status"], "failed")
            self.assertIsNone(result["returncode"])
            self.assertIn("pip unavailable", result["output"])

    async def test_installs_registry_archive_and_requirements(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            record = {
                "node_id": "example-pack",
                "status": "NodeVersionStatusActive",
                "downloadUrl": "https://cdn.comfy.org/example/example-pack/1.2.3/node.zip",
                "version": "1.2.3",
            }

            async def download(_url, destination, _timeout):
                with zipfile.ZipFile(destination, "w") as zf:
                    zf.writestr("example-pack/__init__.py", "VALUE = 1\n")
                    zf.writestr("example-pack/requirements.txt", "example==1.0\n")

            requirements = {
                "status": "installed",
                "returncode": 0,
                "output": "ok",
            }
            with (
                patch.object(nodepacks, "_custom_nodes_root", return_value=root),
                patch.object(nodepacks, "_get_json", new=AsyncMock(return_value=record)) as get,
                patch.object(nodepacks, "_download_archive", side_effect=download),
                patch.object(
                    nodepacks,
                    "_install_requirements",
                    new=AsyncMock(return_value=requirements),
                ) as install_requirements,
            ):
                result = await nodepacks.install_node_pack("example-pack", "1.2.3")

            destination = root / "example-pack"
            self.assertEqual((destination / "__init__.py").read_text(), "VALUE = 1\n")
            self.assertEqual(result["status"], "installed")
            self.assertTrue(result["restart_required"])
            install_requirements.assert_awaited_once_with(destination)
            self.assertIn("version=1.2.3", get.await_args.args[1])


if __name__ == "__main__":
    unittest.main()
