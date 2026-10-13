import io
import json
import os
import plistlib
import re
import ssl
import sys
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))

import packager
import build_macos
import build_windows
import icons


class PackagerTests(unittest.TestCase):
    def test_batch_scripts_must_stay_ascii_with_crlf(self):
        with self.assertRaises(SystemExit):
            packager.ascii_crlf("echo \u4e2d\u6587\n".encode("utf-8"), "bad.bat")
        self.assertEqual(packager.ascii_crlf(b"@echo off\npause\n", "ok.bat"), b"@echo off\r\npause\r\n")
        self.assertEqual(
            packager.ascii_crlf(b"@echo off\r\npause\r\n", "ok.bat"), b"@echo off\r\npause\r\n"
        )

    def test_readme_gets_utf8_bom_and_crlf(self):
        data = packager.utf8_bom_crlf("\u8bf4\u660e\n".encode("utf-8"), "\u4f7f\u7528\u8bf4\u660e.txt")
        self.assertEqual(data, "\ufeff\u8bf4\u660e\r\n".encode("utf-8"))
        with self.assertRaises(SystemExit):
            packager.utf8_bom_crlf(b"\xff\xfe", "bad.txt")

    def test_embedded_stdlib_must_be_complete(self):
        def archive_with(members):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as nested:
                for member in members:
                    nested.writestr(member, b"")
            return buffer.getvalue()

        with mock.patch.object(packager, "log_line"):
            packager.check_embedded_stdlib(
                archive_with(packager.EMBEDDED_STDLIB_REQUIRED), "python312.zip"
            )
        with self.assertRaises(SystemExit):
            packager.check_embedded_stdlib(archive_with(["json/__init__.pyc"]), "python312.zip")
        with self.assertRaises(SystemExit):
            packager.check_embedded_stdlib(b"not a zip", "python312.zip")

    def test_package_paths_stay_under_the_project(self):
        self.assertEqual(packager.DIST_DIR, packager.PROJECT_ROOT / "dist-output")
        self.assertTrue(packager.PROJECT_ROOT.joinpath("app", "keys_store.py").is_file())

    def test_release_versions_stay_in_sync(self):
        def source_version(relative, pattern):
            text = (ROOT / relative).read_text(encoding="utf-8")
            match = re.search(pattern, text, re.MULTILINE)
            self.assertIsNotNone(match, relative)
            return match.group(1)

        versions = {
            "admin": source_version("app/admin.py", r'^APP_VERSION = "([^"]+)"$'),
            "launcher": source_version("launcher/main.go", r'appVersion\s+= "([^"]+)"'),
            "installer": source_version("installer/main_windows.go", r'setupVersion\s+= "([^"]+)"'),
            "mac_builder": build_macos.APP_VERSION,
            "windows_builder": build_windows.SETUP_VERSION,
            "windows_readme": source_version(
                "packaging/templates/windows-readme.txt", r'^Codex 路由 v([^ ]+) 使用说明$'
            ),
        }
        self.assertEqual(len(set(versions.values())), 1, versions)

    def test_macos_bundle_starts_without_a_dock_tile(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(build_macos, "log_line"):
            target = Path(folder) / "Info.plist"
            build_macos.write_info_plist(target)
            with target.open("rb") as stream:
                self.assertIs(plistlib.load(stream)["LSUIElement"], True)

    def test_pe_header_detection(self):
        pe = b"MZ" + b"\x00" * 0x3A + (0x40).to_bytes(4, "little") + b"PE\x00\x00"
        self.assertTrue(build_windows.is_pe_file(pe))
        self.assertFalse(build_windows.is_pe_file(b"MZ" + b"\x00" * 100))
        self.assertFalse(build_windows.is_pe_file(b""))

    def test_winres_spec_uses_icon_resource_id_one(self):
        spec = json.loads(build_windows.winres_spec(Path("spec"), Path("assets/app.ico"), "CodexMultiKey.exe"))
        self.assertEqual(list(spec["RT_GROUP_ICON"]), ["#1"])
        version = spec["RT_VERSION"]["#1"]["0000"]
        self.assertEqual(version["fixed"]["file_version"], build_windows.SETUP_VERSION + ".0")
        self.assertEqual(version["info"]["0409"]["OriginalFilename"], "CodexMultiKey.exe")

    def test_iconset_uses_the_names_iconutil_accepts(self):
        self.assertEqual(
            [name for name, _ in icons.ICONSET_PLAN],
            [
                "icon_16x16.png",
                "icon_16x16@2x.png",
                "icon_32x32.png",
                "icon_32x32@2x.png",
                "icon_128x128.png",
                "icon_128x128@2x.png",
                "icon_256x256.png",
                "icon_256x256@2x.png",
                "icon_512x512.png",
                "icon_512x512@2x.png",
            ],
        )
        self.assertEqual(max(size for _, size in icons.ICONSET_PLAN), 1024)

    def test_shipped_icons_match_the_expected_sizes(self):
        def ico_sizes(path):
            data = path.read_bytes()
            count = int.from_bytes(data[4:6], "little")
            return [data[6 + index * 16] or 256 for index in range(count)]

        self.assertEqual(ico_sizes(icons.APP_ICO), [16, 32, 48, 64, 128, 256])
        self.assertEqual(ico_sizes(icons.TRAY_ICO), [16, 32, 48])
        logo = icons.read_png(icons.UI_LOGO)
        self.assertEqual((logo.width, logo.height), (icons.LOGO_PX, icons.LOGO_PX))
        status = icons.read_png(icons.STATUS_PNG)
        self.assertEqual((status.width, status.height), (icons.STATUS_PX, icons.STATUS_PX))
        source = icons.read_png(icons.SOURCE_IMAGE)
        self.assertEqual((source.width, source.height), (1254, 1254))
        self.assertEqual(source.pixels[3], 0)
        center = ((1254 // 2) * 1254 + 1254 // 2) * 4
        self.assertEqual(source.pixels[center + 3], 255)

    def test_app_icon_uses_the_dock_source_and_keeps_the_plain_mark(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = root / "packaging" / "assets"
            (root / "icon.png").write_bytes(b"plain mark")
            dock = root / "icondock.png"
            icons.write_png(dock, icons.Image(64, 64, b"\xff\xff\xff\xff" * (64 * 64)))
            with mock.patch.object(icons, "PROJECT_ROOT", root), mock.patch.object(
                icons, "ASSETS_DIR", assets
            ), mock.patch.object(icons, "REPO_IMAGE", root / "icon.png"), mock.patch.object(
                icons, "REPO_DOCK_IMAGE", dock
            ), mock.patch.object(
                icons, "SOURCE_IMAGE", assets / "app-icon.png"
            ), mock.patch.object(
                icons, "MARK_IMAGE", assets / "app-mark.png"
            ):
                app = icons.copy_source(force=True)
                mark = icons.copy_mark_source(force=True)
                self.assertEqual(mark.read_bytes(), b"plain mark")
                plate = icons.read_png(app)
                self.assertEqual(plate.pixels[3], 0)
                self.assertEqual(plate.pixels[(32 * 64 + 32) * 4 + 3], 255)
                # The punched plate must survive as the app icon; rebuilding it
                # from the mark would drop the dock artwork's rounded outline.
                packed = icons.app_icon_image(app, mark)
                self.assertEqual(packed.width, icons.APP_ICON_CANVAS)

    def test_app_icon_keeps_the_official_macos_plate_share(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dock = root / "icondock.png"
            icons.write_png(dock, icons.Image(1024, 1024, b"\xff\xff\xff\xff" * (1024 * 1024)))
            plate = root / "app-icon.png"
            icons.write_png(plate, icons.dock_plate(icons.read_png(dock)))
            packed = icons.app_icon_image(plate)
            box = icons.mark_box(packed)
            self.assertIsNotNone(box)
            left, top, right, bottom = box
            expected = round(icons.APP_ICON_CANVAS * icons.MACOS_PLATE_RATIO)
            self.assertLessEqual(abs(right - left - expected), 2)
            self.assertLessEqual(abs(bottom - top - expected), 2)
            # The 100 px margin Apple leaves on each side must stay transparent.
            self.assertEqual(packed.pixels[(packed.width // 2) * 4 + 3], 0)
            self.assertEqual(packed.pixels[3], 0)

    def test_app_icon_rebuilds_an_opaque_square_instead_of_shipping_square_corners(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mark = root / "icon.png"
            icons.write_png(mark, icons.Image(64, 64, b"\x00\x00\x00\xff" * (64 * 64)))
            source = root / "app-icon.png"
            icons.write_png(source, icons.Image(64, 64, b"\xff\xff\xff\xff" * (64 * 64)))
            rebuilt = icons.app_icon_image(source, mark)
            size = icons.APP_ICON_CANVAS
            self.assertEqual((rebuilt.width, rebuilt.height), (size, size))
            corner = 3
            self.assertEqual(rebuilt.pixels[corner], 0)
            center = ((size // 2) * size + size // 2) * 4
            self.assertEqual(tuple(rebuilt.pixels[center : center + 4]), (0, 0, 0, 255))

    def test_app_icon_falls_back_to_the_rounded_plate_when_the_dock_image_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = root / "packaging" / "assets"
            (root / "icon.png").write_bytes(b"plain mark")
            (root / "icon\u5706\u89d2.png").write_bytes(b"white plate")
            with mock.patch.object(icons, "PROJECT_ROOT", root), mock.patch.object(
                icons, "ASSETS_DIR", assets
            ), mock.patch.object(icons, "REPO_IMAGE", root / "icon.png"), mock.patch.object(
                icons, "REPO_DOCK_IMAGE", root / "missing.png"
            ), mock.patch.object(
                icons, "REPO_ROUNDED_IMAGE", root / "icon\u5706\u89d2.png"
            ), mock.patch.object(
                icons, "SOURCE_IMAGE", assets / "app-icon.png"
            ):
                app = icons.copy_source(force=True)
                self.assertEqual(app.read_bytes(), b"white plate")

    def test_app_icon_falls_back_to_the_plain_mark_when_the_plate_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = root / "packaging" / "assets"
            (root / "icon.png").write_bytes(b"plain mark")
            with mock.patch.object(icons, "PROJECT_ROOT", root), mock.patch.object(
                icons, "ASSETS_DIR", assets
            ), mock.patch.object(icons, "REPO_IMAGE", root / "icon.png"), mock.patch.object(
                icons, "REPO_DOCK_IMAGE", root / "missing-dock.png"
            ), mock.patch.object(
                icons, "REPO_ROUNDED_IMAGE", root / "missing.png"
            ), mock.patch.object(
                icons, "SOURCE_IMAGE", assets / "app-icon.png"
            ):
                app = icons.copy_source(force=True)
                self.assertEqual(app.read_bytes(), b"plain mark")

    def test_app_icon_lands_on_a_white_rounded_plate(self):
        icon = icons.compose_app_icon(icons.read_png(icons.MARK_IMAGE))
        self.assertEqual((icon.width, icon.height), (icons.APP_ICON_CANVAS, icons.APP_ICON_CANVAS))
        size = icon.width

        def pixel(x, y):
            offset = (y * size + x) * 4
            return tuple(icon.pixels[offset : offset + 4])

        self.assertEqual(pixel(0, 0)[3], 0)
        self.assertEqual(pixel(size - 1, 0)[3], 0)
        top_middle = pixel(size // 2, 1)
        self.assertEqual(top_middle[3], 255)
        self.assertGreaterEqual(min(top_middle[:3]), 250)

        def coloured(x, y):
            red, green, blue, alpha = pixel(x, y)
            return alpha > 0 and min(255 - red, 255 - green, 255 - blue) > 12

        self.assertFalse(any(coloured(0, y) for y in range(0, size, 8)))
        self.assertFalse(any(coloured(x, 0) for x in range(0, size, 8)))
        self.assertFalse(any(coloured(size - 1, y) for y in range(0, size, 8)))

    def test_pe_subsystem_is_read_from_the_optional_header(self):
        header = bytearray(0x100)
        header[0:2] = b"MZ"
        header[0x3C:0x40] = (0x40).to_bytes(4, "little")
        header[0x40:0x44] = b"PE\x00\x00"
        header[0x40 + 4 + 20 + 68 : 0x40 + 4 + 20 + 70] = (2).to_bytes(2, "little")
        self.assertEqual(build_windows.pe_subsystem(bytes(header)), 2)
        header[0x40 + 4 + 20 + 68 : 0x40 + 4 + 20 + 70] = (3).to_bytes(2, "little")
        self.assertEqual(build_windows.pe_subsystem(bytes(header)), 3)

    def test_windows_payload_self_check_reports_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "CodexMultiKey.exe").write_bytes(b"MZ")
            with self.assertRaises(SystemExit):
                build_windows.verify_payload_dir(path)

    def test_windows_smoke_check_does_not_add_mac_bytecode_to_payload(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            build_windows.subprocess, "run",
            return_value=mock.Mock(returncode=0, stdout="http://127.0.0.1:8790", stderr=""),
        ) as run:
            build_windows.smoke_run_admin(Path(tmp))
            self.assertEqual(run.call_args.kwargs["env"]["PYTHONDONTWRITEBYTECODE"], "1")

    def test_nsis_compiler_uses_the_staged_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "installer.nsi"
            script.write_text("OutFile test.exe\n", encoding="ascii")
            output = root / "setup.exe"
            output.write_bytes(b"stale setup")
            with mock.patch.object(build_windows, "NSIS_SCRIPT", script), mock.patch.object(
                build_windows, "PAYLOAD_DIR", root / "payload"
            ), mock.patch.object(build_windows.subprocess, "run") as run:
                build_windows.compile_nsis(output, "makensis", root / "installctl.exe")
            self.assertFalse(output.exists())
            command = run.call_args.args[0]
            self.assertIn(("/" if os.name == "nt" else "-") + "WX", command)
            self.assertIn(f"DAPP_VERSION={build_windows.SETUP_VERSION}", " ".join(command))
            self.assertIn(f"DPAYLOAD_DIR={(root / 'payload').resolve()}", " ".join(command))
            self.assertIn(f"DOUTPUT_FILE={output.resolve()}", " ".join(command))
            self.assertIn(f"DHELPER_EXE={(root / 'installctl.exe').resolve()}", " ".join(command))
            self.assertEqual(command[-1], str(script))
            self.assertTrue(run.call_args.kwargs["check"])

    def test_nsis_version_metadata_has_required_copyright(self):
        script = build_windows.NSIS_SCRIPT.read_text(encoding="utf-8")
        self.assertRegex(script, r'(?m)^VIAddVersionKey "LegalCopyright" "[^"]+"$')

    def test_nsis_setup_check_rejects_missing_overlay_signature(self):
        data = bytearray(0x400)
        data[:2] = b"MZ"
        data[0x3C:0x40] = (0x40).to_bytes(4, "little")
        data[0x40:0x44] = b"PE\x00\x00"
        data[0x46:0x48] = (2).to_bytes(2, "little")
        data[0x54:0x56] = (0xE0).to_bytes(2, "little")
        data[0x40 + 24 + 68 : 0x40 + 24 + 70] = (2).to_bytes(2, "little")
        data[0x40 + 24 + 32 : 0x40 + 24 + 36] = (0x1000).to_bytes(4, "little")
        data[0x40 + 24 + 56 : 0x40 + 24 + 60] = (0x3000).to_bytes(4, "little")
        sections = 0x40 + 24 + 0xE0
        data[sections : sections + 6] = b".ndata"
        data[sections + 8 : sections + 12] = (0x100).to_bytes(4, "little")
        data[sections + 12 : sections + 16] = (0x1000).to_bytes(4, "little")
        data[sections + 40 : sections + 45] = b".rsrc"
        data[sections + 40 + 16 : sections + 40 + 20] = (0x100).to_bytes(4, "little")
        data[sections + 40 + 20 : sections + 40 + 24] = (0x200).to_bytes(4, "little")
        self.assertEqual(build_windows.nsis_header_offset(data), 0x300)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(build_windows, "MIN_SETUP_BYTES", 0x300):
            setup = Path(tmp) / "setup.exe"
            setup.write_bytes(data)
            with self.assertRaisesRegex(SystemExit, "NSIS payload header"):
                build_windows.verify_setup(setup)
            data[0x304 : 0x304 + len(build_windows.NSIS_SIGNATURE)] = build_windows.NSIS_SIGNATURE
            data[0x318 : 0x31C] = (0x100).to_bytes(4, "little")
            data[-4:] = zlib.crc32(data[512:-4]).to_bytes(4, "little")
            setup.write_bytes(data)
            build_windows.verify_setup(setup)
            data[0x320] ^= 1
            setup.write_bytes(data)
            with self.assertRaisesRegex(SystemExit, "CRC mismatch"):
                build_windows.verify_setup(setup)
            data[0x320] ^= 1
            data[0x300:0x304] = (4).to_bytes(4, "little")
            data[-4:] = zlib.crc32(data[512:-4]).to_bytes(4, "little")
            setup.write_bytes(data)
            with self.assertRaisesRegex(SystemExit, "CRC checking is disabled"):
                build_windows.verify_setup(setup)
            data[0x300:0x304] = bytes(4)
            data[0x40 + 24 + 56 : 0x40 + 24 + 60] = (0x1000).to_bytes(4, "little")
            setup.write_bytes(data)
            with self.assertRaisesRegex(SystemExit, "SizeOfImage"):
                build_windows.verify_setup(setup)

    def test_windows_readme_documents_the_runtime_data_dir(self):
        # The launcher and toolctl.paths must agree on one writable data folder.
        # toolctl puts it at package_dir/runtime, and the packaged app folder is
        # the package dir, so the readme has to point users at that same place.
        app_entry = next(
            name for name in build_windows.PAYLOAD_REQUIRED if name.endswith("/app/admin.py")
        )
        data_dir = app_entry[: -len("/admin.py")] + "/runtime"
        readme = build_windows.README_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn(data_dir.replace("/", "\\") + "\\logs\\", readme)

    def test_ssl_context_survives_missing_certificate_bundle(self):
        with mock.patch.dict(os.environ, {"SSL_CERT_FILE": "/nonexistent/cert.pem"}):
            self.assertIsInstance(packager._ssl_context(), ssl.SSLContext)

    def test_no_batch_or_powershell_scripts_anywhere(self):
        # Build output (dist-output) unpacks third party sources such as the
        # zstd VS helpers; only tracked project files have to stay script free.
        ignored_parts = {"dist-output", "__pycache__"}
        offenders = [
            str(path.relative_to(packager.PROJECT_ROOT))
            for path in packager.PROJECT_ROOT.rglob("*")
            if path.is_file()
            and path.suffix.lower() in {".bat", ".cmd", ".ps1"}
            and not ignored_parts.intersection(path.parts)
        ]
        self.assertEqual(offenders, [])

    def test_windows_facing_sources_stay_pure_ascii(self):
        sources = sorted(packager.PROJECT_ROOT.glob("installer/*.go"))
        sources += sorted(packager.PROJECT_ROOT.glob("launcher/*.go"))
        sources += sorted(packager.PROJECT_ROOT.glob("launcher/*.m"))
        sources += sorted(packager.PROJECT_ROOT.glob("packaging/build_windows.py"))
        self.assertGreaterEqual(len(sources), 5)
        for path in sources:
            data = path.read_bytes()
            self.assertFalse(data.startswith(b"\xef\xbb\xbf"), f"{path.name} must not carry a BOM")
            self.assertTrue(all(byte < 128 for byte in data), f"{path.name} must stay ASCII")

    def test_runtime_python_sources_are_valid_utf8_without_bom(self):
        sources = sorted(packager.PROJECT_ROOT.glob("app/*.py"))
        self.assertGreaterEqual(len(sources), 4)
        for path in sources:
            data = path.read_bytes()
            self.assertFalse(data.startswith(b"\xef\xbb\xbf"), f"{path.name} must not carry a BOM")
            compile(data.decode("utf-8"), str(path), "exec")


if __name__ == "__main__":
    unittest.main()
