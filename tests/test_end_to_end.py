"""Offline integration coverage for the complete Deerstalker scan flow.

The test deliberately uses a deterministic model stub: it validates the real
scanner, archive expansion, extractors, both analysis passes, SQLite storage,
output copying/reporting, API static mount, and local Uvicorn readiness
without requiring Ollama or a desktop GUI in CI.
"""

from __future__ import annotations

import base64
import socket
import sys
import tempfile
import time
import types
import urllib.request
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import main
from app import api, portable_devices
from app.config import Settings
from app.pipeline.orchestrator import Orchestrator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = PROJECT_ROOT / "test_assets"
_ONE_PIXEL_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADUlEQVR42mNk+M/wHwAF/gL+3MxZ5wAAAABJRU5ErkJggg=="
)


class FixedModel:
    """Small duck-typed Ollama replacement for deterministic integration tests."""

    @staticmethod
    def _result() -> dict[str, object]:
        return {
            "relevance_score": 0.92,
            "tags": ["integration-fixture"],
            "summary": "Fixture identified by the integration test model.",
            "reasoning": "The deterministic test model marks every fixture as a match.",
            "flagged_interesting": False,
        }

    def generate_text(self, system: str, prompt: str, json_mode: bool = True, model: str | None = None) -> dict[str, object]:
        return self._result()

    def generate_vision(
        self,
        system: str,
        prompt: str,
        images_b64: list[str],
        json_mode: bool = True,
        model: str | None = None,
    ) -> dict[str, object]:
        return self._result()


class _EventList(list):
    def __iadd__(self, callback: object):
        self.append(callback)
        return self


class _FakeWindow:
    def __init__(self) -> None:
        self.events = types.SimpleNamespace(closed=_EventList())


class _FakeWebview:
    def __init__(self) -> None:
        self.window = _FakeWindow()
        self.title: str | None = None
        self.url: str | None = None
        self.started = False

    def create_window(self, title: str, url: str, **_kwargs: object) -> _FakeWindow:
        self.title = title
        self.url = url
        return self.window

    def start(self) -> None:
        self.started = True


def _write_minimal_pdf(path: Path) -> None:
    """Create a standards-shaped single-page PDF without a fixture dependency."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length 48 >>\nstream\nBT /F1 12 Tf 30 120 Td (Deerstalker fixture) Tj ET\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    document = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, object_body in enumerate(objects, start=1):
        offsets.append(len(document))
        document.extend(f"{number} 0 obj\n".encode("ascii"))
        document.extend(object_body)
        document.extend(b"\nendobj\n")
    xref_offset = len(document)
    document.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    document.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        document.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    document.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    path.write_bytes(document)


def _write_minimal_docx(path: Path) -> None:
    """Create the smallest useful DOCX package using only the stdlib."""
    content_types = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">
  <Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>
  <Default Extension=\"xml\" ContentType=\"application/xml\"/>
  <Override PartName=\"/word/document.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml\"/>
</Types>"""
    relationships = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">
  <Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" Target=\"word/document.xml\"/>
</Relationships>"""
    document = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">
  <w:body><w:p><w:r><w:t>Deerstalker DOCX fixture</w:t></w:r></w:p><w:sectPr/></w:body>
</w:document>"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("word/document.xml", document)


def build_test_assets() -> None:
    """Create the mixed fixture set described by the Segment 17 contract."""
    ASSET_DIR.mkdir(exist_ok=True)
    (ASSET_DIR / "case_notes.txt").write_text("A short local investigation note.", encoding="utf-8")
    (ASSET_DIR / "sample_image.png").write_bytes(base64.b64decode(_ONE_PIXEL_PNG))
    _write_minimal_pdf(ASSET_DIR / "sample_document.pdf")
    _write_minimal_docx(ASSET_DIR / "sample_document.docx")
    (ASSET_DIR / "unknown.evidence").write_bytes(b"\x00\x01unknown fixture bytes")
    (ASSET_DIR / "empty_file.dat").write_bytes(b"")
    nested_zip = ASSET_DIR / "nested_case.zip"
    with zipfile.ZipFile(nested_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("nested_note.txt", "A nested archive clue.")
    with zipfile.ZipFile(ASSET_DIR / "case_bundle.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("archive_note.txt", "A top-level archive clue.")
        archive.write(nested_zip, arcname="nested_case.zip")
    nested_zip.unlink(missing_ok=True)


class EndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        build_test_assets()

    def test_complete_scan_produces_report(self) -> None:
        expected_assets = {
            "case_notes.txt",
            "sample_image.png",
            "sample_document.pdf",
            "sample_document.docx",
            "case_bundle.zip",
            "unknown.evidence",
            "empty_file.dat",
        }
        self.assertTrue(expected_assets.issubset({path.name for path in ASSET_DIR.iterdir()}))

        with tempfile.TemporaryDirectory(prefix="deerstalker-e2e-") as temporary_directory:
            settings = Settings(
                ollama_host="http://127.0.0.1:11434",
                ollama_model="test-vision-model",
                ollama_fast_model=None,
                ollama_detailed_model=None,
                ollama_timeout_seconds=5,
                ollama_max_concurrency=1,
                app_data_dir=temporary_directory,
                max_archive_depth=3,
                max_archive_total_mb=50,
                max_file_read_mb=2,
                app_port=8731,
            )
            orchestrator = Orchestrator(client=FixedModel())
            job = orchestrator.start_job(
                roots=[str(ASSET_DIR)],
                intent_ids=["work_docs", "interesting"],
                custom_query=None,
                settings=settings,
            )

            deadline = time.monotonic() + 30.0
            progress = orchestrator.get_progress(job.id)
            while progress["status"] not in {"done", "error", "cancelled"} and time.monotonic() < deadline:
                time.sleep(0.05)
                progress = orchestrator.get_progress(job.id)

            self.assertEqual(progress["status"], "done", progress)
            self.assertGreaterEqual(progress["counts"]["total"], len(expected_assets))
            self.assertGreater(progress["counts"]["matched"], 0)
            report_path = Path(progress["report_path"])
            self.assertTrue(report_path.is_file())
            self.assertIn("Deerstalker Investigation Report", report_path.read_text(encoding="utf-8"))

    def test_local_backend_reaches_readiness_endpoint(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as socket_probe:
            socket_probe.bind(("127.0.0.1", 0))
            port = socket_probe.getsockname()[1]
        settings = replace(
            Settings.load(env_file=None),
            app_port=port,
        )
        server, thread = main.start_backend(settings)
        try:
            self.assertTrue(main.wait_for_backend(port, timeout_seconds=10.0))
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2.0) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b"DEERSTALKER", response.read())
        finally:
            main.stop_backend(server, thread)

    def test_desktop_shell_opens_local_url_and_stops_cleanly(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as socket_probe:
            socket_probe.bind(("127.0.0.1", 0))
            port = socket_probe.getsockname()[1]
        settings = replace(Settings.load(env_file=None), app_port=port)
        fake_webview = _FakeWebview()
        with patch.object(main, "get_settings", return_value=settings), patch.dict(
            sys.modules, {"webview": fake_webview}
        ):
            main.run()

        self.assertTrue(fake_webview.started)
        self.assertEqual(fake_webview.title, "Deerstalker - Local File Investigation")
        self.assertEqual(fake_webview.url, f"http://localhost:{port}")
        self.assertEqual(len(fake_webview.window.events.closed), 1)

    def test_native_folder_picker_route_returns_a_scan_root(self) -> None:
        with patch.object(api, "_choose_local_folder", return_value=str(ASSET_DIR)):
            selected = api.choose_folder()
        self.assertTrue(selected["selected"])
        self.assertEqual(selected["root"]["path"], str(ASSET_DIR.resolve()))
        self.assertEqual(selected["root"]["kind"], "folder")
        with patch.object(api, "_choose_local_folder", return_value=None):
            self.assertEqual(api.choose_folder(), {"selected": False})

    def test_portable_device_root_identifier_round_trips(self) -> None:
        shell_parsing_path = r"::{20D04FE0-3AEA-1069-A2D8-08002B30309D}\\?\usb#vid_2717&pid_ff40#phone"
        root = portable_devices.encode_root(shell_parsing_path)
        self.assertTrue(portable_devices.is_portable_root(root))
        self.assertEqual(portable_devices.decode_root(root), shell_parsing_path)

    def test_portable_storage_reopens_through_its_explorer_item(self) -> None:
        class Item:
            def __init__(self, name: str, path: str, is_folder: bool = True) -> None:
                self.Name = name
                self.Path = path
                self.IsFolder = is_folder

        class Folder:
            def __init__(self, items: list[Item]) -> None:
                self._items = items

            def Items(self) -> list[Item]:
                return self._items

        class Shell:
            def __init__(self, device: Item, storage: Item, device_folder: Folder, storage_folder: Folder) -> None:
                self._device = device
                self._storage = storage
                self._device_folder = device_folder
                self._storage_folder = storage_folder
                self._this_pc = Folder([device])

            def NameSpace(self, value: object) -> Folder | None:
                if value == 17:
                    return self._this_pc
                if value is self._device:
                    return self._device_folder
                if value is self._storage:
                    return self._storage_folder
                return None

        parsing_path = r"::{20D04FE0-3AEA-1069-A2D8-08002B30309D}\\?\usb#vid_2717&pid_ff40#phone\SID-{storage}"
        device = Item("Phone", r"::{20D04FE0-3AEA-1069-A2D8-08002B30309D}\\?\usb#vid_2717&pid_ff40#phone")
        storage = Item("Internal storage", parsing_path)
        storage_folder = Folder([])
        shell = Shell(device, storage, Folder([storage]), storage_folder)

        self.assertIs(portable_devices._find_storage_folder(shell, parsing_path), storage_folder)


if __name__ == "__main__":
    unittest.main(verbosity=2)
