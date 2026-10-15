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

        self.assertEqual(ico_sizes(icons.APP_ICO), [16, 24, 32, 48, 64, 128, 256])
        self.assertEqual(ico_sizes(icons.TRAY_ICO), [16, 32, 48])
        logo = icons.read_png(icons.UI_LOGO)
        self.assertEqual((logo.width, logo.height), (icons.LOGO_PX, icons.LOGO_PX))
        for path, size in ((icons.STATUS_PNG, 18), (icons.STATUS_PNG_2X, 36)):
            status = icons.read_png(path)
            self.assertEqual((status.width, status.height), (size, size))
        source = icons.read_png(icons.SOURCE_IMAGE)
        self.assertEqual((source.width, source.height), (1024, 1024))
        self.assertIn(0, source.pixels[3::4])
        self.assertIn(255, source.pixels[3::4])

    def test_frozen_main_icon_keeps_pixels_and_canvas_when_exported(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(icons, "log_line"):
            root = Path(tmp)
            source = root / "app-icon.png"
            pixels = bytearray(b"\x40\x80\xc0\xff" * (1024 * 1024))
            pixels[:4] = b"\x1f\x59\xa7\x33"
            pixels[-4:] = b"\x0b\x16\x21\x00"
            icons.write_png(source, icons.Image(1024, 1024, pixels))
            original = source.read_bytes()
            image = icons.app_icon_image(source)
            self.assertEqual(image.pixels, pixels)
            plan = (("icon_32x32.png", 32), ("icon_512x512@2x.png", 1024))
            with mock.patch.object(icons, "ICONSET_PLAN", plan):
                paths = icons.write_iconset(icons.Resizer(image), root / "iconset", force=True, source=source)
            small, full = (icons.read_png(path) for path in paths)
            self.assertEqual((full.width, full.height), (1024, 1024))
            self.assertEqual(full.pixels, pixels)
            self.assertEqual((small.width, small.height), (32, 32))
            self.assertGreater(small.pixels[3], 0)
            center = (16 * 32 + 16) * 4
            self.assertEqual(small.pixels[center : center + 4], b"\x40\x80\xc0\xff")
            self.assertEqual(source.read_bytes(), original)

    def test_statusbar_exports_keep_the_two_approved_alpha_masks(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(icons, "log_line"):
            root = Path(tmp)
            source = root / "app-icon.png"
            icons.write_png(source, icons.Image(1024, 1024, b"\x40\x80\xc0\xff" * (1024 * 1024)))
            sources = (root / "statusbar-source.png", root / "statusbar-source@2x.png")
            targets = (root / "statusbar.png", root / "statusbar@2x.png")
            expected = []
            for path, size in zip(sources, (18, 36)):
                pixels = bytearray(b"\xff\xff\xff\x00" * (size * size))
                alpha = bytes((index * 37 + size) % 256 for index in range(size * size))
                pixels[3::4] = alpha
                icons.write_png(path, icons.Image(size, size, pixels))
                expected.append((path.read_bytes(), alpha))
            with mock.patch.multiple(
                icons,
                SOURCE_IMAGE=source,
                STATUS_SOURCE_PNG=sources[0],
                STATUS_SOURCE_PNG_2X=sources[1],
                STATUS_PNG=targets[0],
                STATUS_PNG_2X=targets[1],
                APP_ICO=root / "app.ico",
                TRAY_ICO=root / "tray.ico",
                UI_LOGO=root / "logo.png",
            ):
                icons.ensure_assets(force=True)
            for source_path, target, (original, alpha) in zip(sources, targets, expected):
                output = icons.read_png(target)
                self.assertEqual(output.pixels[3::4], alpha)
                for channel in range(3):
                    self.assertFalse(any(output.pixels[channel::4]))
                self.assertEqual(source_path.read_bytes(), original)
            logo = icons.read_png(root / "logo.png")
            self.assertEqual(logo.pixels[:4], b"\x40\x80\xc0\xff")

    def test_icon_export_stops_for_missing_or_invalid_masters(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(icons, "log_line"):
            root = Path(tmp)
            source = root / "app-icon.png"
            with mock.patch.object(icons, "SOURCE_IMAGE", source):
                with self.assertRaisesRegex(SystemExit, "图标母版缺失"):
                    icons.ensure_assets(force=True)
            icons.write_png(source, icons.Image(1, 1, b"\xff\xff\xff\xff"))
            with self.assertRaisesRegex(SystemExit, "图标母版尺寸不符"):
                icons.app_icon_image(source)
            status = root / "statusbar-source.png"
            icons.write_png(status, icons.Image(18, 18, b"\x80\x80\x80\xff" * (18 * 18)))
            with self.assertRaisesRegex(SystemExit, "纯白线条"):
                icons.statusbar_template(status, 18)

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
        # macOS 专用 Objective-C 不参与 Windows 编译，使用 UTF-8。
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
