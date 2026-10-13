import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))

import package_release


class ReleaseTests(unittest.TestCase):
    def make_project(self, root: Path, version: str = "1.2.11") -> None:
        sources = {
            "app/admin.py": f'APP_VERSION = "{version}"\n',
            "launcher/main.go": f'\tappVersion   = "{version}"\n',
            "installer/main_windows.go": f'\tsetupVersion   = "{version}"\n',
            "packaging/build_macos.py": f'APP_VERSION = "{version}"\n',
            "packaging/build_windows.py": f'SETUP_VERSION = "{version}"\n',
            "packaging/templates/windows-readme.txt": f"Codex 路由 v{version} 使用说明\n",
        }
        for relative, source in sources.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")

    def test_next_version_and_artifact_names(self):
        self.assertEqual(package_release.next_patch_version("1.2.11"), "1.2.12")
        self.assertEqual(package_release.next_patch_version("1.2.9"), "1.2.10")
        self.assertEqual(
            package_release.artifact_names("1.2.12"),
            ("CodexRouter-1.2.12-mac-arm64.dmg", "CodexRouter-1.2.12-win-x64.exe"),
        )
        with self.assertRaises(ValueError):
            package_release.next_patch_version("1.2")

    def test_success_publishes_both_packages_and_updates_every_display_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_project(root)

            def build(command, **kwargs):
                self.assertTrue(kwargs["check"])
                output = Path(command[-1])
                output.write_bytes(b"MZ" if output.suffix == ".exe" else b"dmg")
                return subprocess.CompletedProcess(command, 0)

            files = package_release.package_release(root, runner=build, check_host=False)
            self.assertEqual([path.name for path in files], list(package_release.artifact_names("1.2.12")))
            self.assertEqual({path.parent for path in files}, {root / "release" / "1.2.12"})
            self.assertEqual(sorted(path.name for path in files[0].parent.iterdir()), sorted(path.name for path in files))
            version, _, _ = package_release.version_sources(root)
            self.assertEqual(version, "1.2.13")

    def test_failed_second_build_restores_versions_and_does_not_publish(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_project(root)
            originals = {relative: (root / relative).read_bytes() for relative, _ in package_release.VERSION_FIELDS}

            def build(command, **_kwargs):
                output = Path(command[-1])
                if output.suffix == ".exe":
                    raise RuntimeError("Windows build failed")
                output.write_bytes(b"dmg")
                return subprocess.CompletedProcess(command, 0)

            with self.assertRaisesRegex(RuntimeError, "Windows build failed"):
                package_release.package_release(root, runner=build, check_host=False)
            for relative, original in originals.items():
                self.assertEqual((root / relative).read_bytes(), original)
            self.assertFalse((root / "release" / "1.2.12").exists())
            self.assertEqual(list((root / "release").glob(".staging-*")), [])

    def test_existing_release_advances_version_without_touching_old_packages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_project(root, "1.2.13")
            previous = root / "release" / "1.2.14"
            previous.mkdir(parents=True)
            old_packages = {}
            for name in package_release.artifact_names("1.2.14"):
                path = previous / name
                path.write_bytes(b"previous release")
                old_packages[path] = path.read_bytes()

            def build(command, **_kwargs):
                Path(command[-1]).write_bytes(b"new release")
                return subprocess.CompletedProcess(command, 0)

            files = package_release.package_release(root, runner=build, check_host=False)
            self.assertEqual({path.parent for path in files}, {root / "release" / "1.2.15"})
            self.assertTrue(all(path.read_bytes() == b"new release" for path in files))
            self.assertEqual({path: path.read_bytes() for path in old_packages}, old_packages)
            self.assertIn('APP_VERSION = "1.2.15"', (root / "app/admin.py").read_text())

    def test_source_drift_during_build_aborts_and_restores_versions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_project(root)
            originals = {relative: (root / relative).read_bytes() for relative, _ in package_release.VERSION_FIELDS}

            def build(command, **_kwargs):
                output = Path(command[-1])
                output.write_bytes(b"fake package")
                if output.suffix == ".exe":
                    (root / "app/admin.py").write_text('APP_VERSION = "9.9.9"\n', encoding="utf-8")
                return subprocess.CompletedProcess(command, 0)

            with self.assertRaisesRegex(RuntimeError, "版本源在打包过程中被修改"):
                package_release.package_release(root, runner=build, check_host=False)
            self.assertEqual({relative: (root / relative).read_bytes() for relative in originals}, originals)
            self.assertFalse((root / "release" / "1.2.12").exists())
            self.assertEqual(list((root / "release").glob(".staging-*")), [])

    def test_concurrent_invocation_cannot_mix_versions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_project(root)
            started = threading.Event()
            proceed = threading.Event()
            first_result = []
            first_error = []

            def build(command, **_kwargs):
                output = Path(command[-1])
                if output.suffix == ".dmg":
                    started.set()
                    if not proceed.wait(10):
                        raise TimeoutError("first build was not resumed")
                output.write_bytes((root / "app/admin.py").read_bytes())
                return subprocess.CompletedProcess(command, 0)

            def run_first():
                try:
                    first_result.extend(package_release.package_release(root, runner=build, check_host=False))
                except BaseException as error:
                    first_error.append(error)

            thread = threading.Thread(target=run_first, daemon=True)
            thread.start()
            try:
                self.assertTrue(started.wait(5), "first build did not reach the locked build step")
                child = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
                        "import package_release; package_release.package_release(Path(sys.argv[2]), check_host=False)",
                        str(ROOT / "packaging"),
                        str(root),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertNotEqual(child.returncode, 0)
                self.assertIn("已有一键打包进程", child.stderr)
            finally:
                proceed.set()
                thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(first_error, [])
            self.assertEqual(len(first_result), 2)
            self.assertTrue(all(path.read_bytes() == b'APP_VERSION = "1.2.12"\n' for path in first_result))
            self.assertEqual({path.parent for path in first_result}, {root / "release" / "1.2.12"})


if __name__ == "__main__":
    unittest.main()
