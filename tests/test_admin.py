import contextlib
import io
import json
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import admin
import keys_store
import toolctl


def official_cache():
    def model(slug, priority):
        return {
            "slug": slug,
            "display_name": slug,
            "description": slug,
            "priority": priority,
            "context_window": 200000,
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": ["low", "medium", "high"],
            "visibility": "list",
        }

    return {"models": [model("gpt-5.6-terra", 12), model("gpt-5.1", 11)]}


def key_runtime_manifest(key_id):
    return {
        "version": 2,
        "sources": {
            "official": {"kind": "official", "name": "ChatGPT"},
            key_id: {
                "kind": "key", "name": "Key A",
                "upstream_base_url": "https://upstream.example.com/v1",
                "api_key": "sk-test-000000000001",
            },
        },
        "models": [{
            "slug": "local/kimi-k3", "display_name": "kimi-k3", "description": "Key model",
            "priority": 50, "context_window": 200000, "default_reasoning_level": "medium",
            "supported_reasoning_levels": ["low", "medium"], "input_modalities": ["text"],
            "supports_image_detail_original": False, "chat_options": {"explicit_tool_choice": "native"},
            "upstream_model": "kimi-k3", "source": key_id,
        }],
    }


def write_key_runtime_manifest(paths, key_id):
    path = paths["manifest"]
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(key_runtime_manifest(key_id), ensure_ascii=False) + "\n").encode("utf-8")
    path.write_bytes(raw)
    return raw


class AdminServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.package = ROOT / "app"
        codex = self.home / ".codex"
        codex.mkdir(parents=True, exist_ok=True)
        (codex / "models_cache.json").write_text(json.dumps(official_cache()), encoding="utf-8")
        (codex / "auth.json").write_text("{}", encoding="utf-8")
        self.p = toolctl.paths(self.home, self.package)
        self.p["runtime"].mkdir(parents=True, exist_ok=True)
        self.service = admin.AdminService(self.package, self.p, home=self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def add_key(self, name, api_key="sk-test-000000000001", upstream="https://upstream.example.com/v1"):
        created = self.service.create_key({"name": name, "api_key": api_key, "upstream_base_url": upstream})
        return created["key"]["id"]

    def write_discovery(self, key_id, models):
        path = self.service.discovery_path(key_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"key_id": key_id, "fetched_at": "2026-09-21 10:00:00", "models": models},
                       ensure_ascii=False),
            encoding="utf-8",
        )

    def test_state_without_keys_is_valid_and_reports_official_models(self):
        state = self.service.state()
        self.assertTrue(state["ok"])
        self.assertEqual(state["keys"], [])
        self.assertEqual(state["models"], [])
        self.assertEqual(state["conflicts"], [])
        self.assertEqual(state["official"]["count"], 2)
        self.assertTrue(state["official"]["logged_in"])
        self.assertEqual([m["slug"] for m in state["official"]["models"]], ["gpt-5.6-terra", "gpt-5.1"])
        self.assertEqual(state["router"]["port"], 8790)
        self.assertEqual(state["settings"]["admin_port"], 8791)
        self.assertFalse(state["taken_over"])

    def test_router_expected_defaults_on_and_survives_a_restart(self):
        self.assertTrue(self.service.router_expected())
        self.service._set_router_enabled(False)
        self.assertFalse(self.service.router_expected())
        self.assertFalse(self.service.settings_view()["router_enabled"])
        restarted = admin.AdminService(self.package, self.p, home=self.home)
        self.assertFalse(restarted.router_expected())

    def run_on_start(self):
        with mock.patch.object(self.service, "heal_catalog", return_value=False), mock.patch.object(
            self.service.router, "retire_legacy_autostart", return_value=[]
        ), mock.patch.object(
            self.service, "repair_autostart", return_value={"repaired": False}
        ), mock.patch.object(
            toolctl, "_is_unified", return_value=True
        ), mock.patch.object(
            self.service.router, "listening", return_value=False
        ), mock.patch.object(
            self.service.router, "ensure_running"
        ) as ensure, mock.patch.object(toolctl, "http_health", return_value={"status": "ok"}):
            self.service.on_start()
        return ensure

    def test_boot_restores_the_router_when_the_user_still_expects_it(self):
        self.p["config"].write_text("[model_providers.codex_official_local]\n", encoding="utf-8")
        ensure = self.run_on_start()
        ensure.assert_called_once()

    def test_boot_leaves_the_router_stopped_after_the_user_switched_it_off(self):
        self.p["config"].write_text("[model_providers.codex_official_local]\n", encoding="utf-8")
        self.service._set_router_enabled(False)
        ensure = self.run_on_start()
        ensure.assert_not_called()

    def test_autostart_repair_uses_the_launcher_path(self):
        launcher = self.home / "CodexMultiKey"
        launcher.write_bytes(b"binary")
        service = admin.AdminService(self.package, self.p, home=self.home, launcher=launcher)
        with mock.patch.object(admin.autostart.Autostart, "repair", return_value=True) as repair:
            result = service.repair_autostart()
        self.assertTrue(result["repaired"])
        repair.assert_called_once()

    def test_autostart_repair_skips_an_unknown_launcher(self):
        result = self.service.repair_autostart()
        self.assertEqual(result, {"repaired": False, "reason": "launcher-unknown"})

    def test_key_crud_roundtrip_keeps_utf8_and_mode(self):
        key_id = self.add_key("主力账号")
        state = self.service.state()
        self.assertEqual(len(state["keys"]), 1)
        self.assertEqual(state["keys"][0]["name"], "主力账号")
        self.assertEqual(state["keys"][0]["masked_key"], "sk-te******0001")
        raw = self.p["keys"].read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn("主力账号", raw.decode("utf-8"))
        self.assertEqual(self.p["keys"].stat().st_mode & 0o777, 0o600)

        self.service.update_key(key_id, {"name": "备用账号 B", "enabled": False})
        state = self.service.state()
        self.assertEqual(state["keys"][0]["name"], "备用账号 B")
        self.assertFalse(state["keys"][0]["enabled"])
        self.assertTrue(state["keys"][0]["has_key"])

        self.service.update_key(key_id, {"api_key": "sk-new-key-000000000002"})
        stored = keys_store.load_keys(self.p["keys"])["keys"][0]
        self.assertEqual(stored["api_key"], "sk-new-key-000000000002")

        self.service.delete_key(key_id)
        self.assertEqual(self.service.state()["keys"], [])
        self.assertFalse(self.service.discovery_path(key_id).exists())

    def test_selected_models_all_and_none_semantics(self):
        first = self.add_key("A")
        second = self.add_key("B")
        self.service.update_selected_models(first, [])
        state = self.service.state()
        by_id = {key["id"]: key for key in state["keys"]}
        self.assertEqual(by_id[first]["selected_models"], [])
        self.assertIsNone(by_id[second]["selected_models"])
        stored = keys_store.load_keys(self.p["keys"])
        self.assertEqual(keys_store.apply_selected_models(keys_store.key_index(stored)[first], ["a", "b"]), [])
        self.assertEqual(keys_store.apply_selected_models(keys_store.key_index(stored)[second], ["a", "b"]), ["a", "b"])

    def test_priority_roundtrip_and_validation(self):
        first = self.add_key("A")
        second = self.add_key("B")
        self.service.update_priority({"gpt-5.1": second, "kimi-k3": "official"})
        state = self.service.state()
        self.assertEqual(state["model_priority"], {"gpt-5.1": second, "kimi-k3": "official"})
        with self.assertRaises(keys_store.KeysError):
            self.service.update_priority({"gpt-5.1": "missing-key"})
        self.service.delete_key(second)
        self.assertEqual(self.service.state()["model_priority"], {"kimi-k3": "official"})
        self.assertNotEqual(first, second)

    def test_reorder_keys_requires_every_id_once(self):
        first = self.add_key("A")
        second = self.add_key("B")
        third = self.add_key("C")
        self.service.reorder_keys([third, first, second])
        state = self.service.state()
        self.assertEqual([key["id"] for key in state["keys"]], [third, first, second])
        with self.assertRaises(admin.ApiError):
            self.service.reorder_keys([first])

    def test_discovery_cache_drives_conflicts_and_key_rows(self):
        first = self.add_key("A")
        second = self.add_key("B")
        self.write_discovery(first, ["gpt-5.1", "kimi-k3"])
        self.write_discovery(second, ["gpt-5.1", "glm-5.3"])
        state = self.service.state()
        rows = {key["id"]: key for key in state["keys"]}
        self.assertEqual(rows[first]["discovered_count"], 2)
        self.assertEqual(rows[first]["discovered_at"], "2026-09-21 10:00:00")
        self.assertEqual(rows[first]["discovered_models"], ["gpt-5.1", "kimi-k3"])
        conflicts = {row["model"]: row for row in state["conflicts"]}
        self.assertIn("gpt-5.1", conflicts)
        self.assertEqual(sorted(conflicts["gpt-5.1"]["candidates"]), sorted([first, second, "official"]))
        self.assertEqual(conflicts["gpt-5.1"]["chosen"], first)
        self.assertNotIn("kimi-k3", conflicts)

    def test_model_views_follow_runtime_manifest(self):
        key_id = self.add_key("A")
        manifest = {
            "version": 2,
            "sources": {"official": {"kind": "official", "name": "official"}, key_id: {
                "kind": "key", "name": "A", "upstream_base_url": "https://upstream.example.com/v1", "api_key": "sk-x",
            }},
            "models": [{
                "slug": "local/kimi-k3", "display_name": "kimi-k3", "description": "d", "priority": 50,
                "context_window": 200000, "default_reasoning_level": "medium",
                "supported_reasoning_levels": ["low", "medium"], "input_modalities": ["text"],
                "supports_image_detail_original": False, "chat_options": {"explicit_tool_choice": "native"},
                "upstream_model": "kimi-k3", "source": key_id,
            }],
        }
        self.p["manifest"].parent.mkdir(parents=True, exist_ok=True)
        self.p["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
        state = self.service.state()
        self.assertEqual([model["slug"] for model in state["models"]], ["local/kimi-k3"])
        self.assertEqual(state["models"][0]["source_name"], "A")
        self.assertEqual(state["keys"][0]["included_count"], 1)

    def test_boot_heals_a_catalog_left_over_with_duplicate_models(self):
        key_id = self.add_key("A")
        manifest = {
            "version": 2,
            "sources": {"official": {"kind": "official", "name": "official"}, key_id: {
                "kind": "key", "name": "A", "upstream_base_url": "https://upstream.example.com/v1", "api_key": "sk-x",
            }},
            "models": [{
                "slug": "local/gpt-5.6-terra", "display_name": "GPT-5.6 Terra", "description": "d", "priority": 50,
                "context_window": 200000, "default_reasoning_level": "medium",
                "supported_reasoning_levels": ["low", "medium"], "input_modalities": ["text", "image"],
                "supports_image_detail_original": True, "chat_options": {"explicit_tool_choice": "native"},
                "upstream_model": "gpt-5.6-terra", "source": key_id,
            }],
        }
        self.p["manifest"].parent.mkdir(parents=True, exist_ok=True)
        self.p["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
        # The old release wrote an official entry next to the Key entry.
        stale = {"models": [model for model in official_cache()["models"] if model["slug"] == "gpt-5.6-terra"]}
        stale["models"].append({"slug": "local/gpt-5.6-terra", "display_name": "GPT-5.6 Terra"})
        self.p["catalog"].write_text(json.dumps(stale), encoding="utf-8")

        self.assertTrue(self.service.heal_catalog())
        healed = json.loads(self.p["catalog"].read_text(encoding="utf-8"))
        self.assertEqual([model["slug"] for model in healed["models"]].count("gpt-5.6-terra"), 0)
        self.assertEqual([model["slug"] for model in healed["models"]].count("local/gpt-5.6-terra"), 1)
        # Nothing to do the second time, and a missing runtime file is not fatal.
        self.assertFalse(self.service.heal_catalog())
        self.p["manifest"].unlink()
        self.assertFalse(self.service.heal_catalog())

    def test_conflict_rows_match_official_models_by_identity(self):
        key_id = self.add_key("A")
        self.write_discovery(key_id, ["GPT-5.6 Terra", "kimi-k3"])
        rows = {row["model"]: row for row in self.service.state()["conflicts"]}
        self.assertEqual(sorted(rows["GPT-5.6 Terra"]["candidates"]), sorted([key_id, "official"]))
        self.assertNotIn("kimi-k3", rows)

    def test_official_discovery_network_failure_keeps_existing_files(self):
        key_id = self.add_key("A")
        write_key_runtime_manifest(self.p, key_id)
        self.p["catalog"].write_text(json.dumps({"models": [{"slug": "old-catalog"}]}), encoding="utf-8")
        before = {path: path.read_bytes() for path in (self.p["official_cache"], self.p["catalog"])}

        with mock.patch.object(toolctl, "fetch_official_models", side_effect=toolctl.ControlError("HTTP 401")) as fetch, \
             mock.patch.object(toolctl, "_restart_chatgpt") as restart:
            with self.assertRaises(toolctl.ControlError):
                self.service.discover_official_models()

        fetch.assert_called_once_with(self.p)
        restart.assert_not_called()
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_official_discovery_second_write_failure_restores_both_files(self):
        key_id = self.add_key("A")
        write_key_runtime_manifest(self.p, key_id)
        self.p["catalog"].write_text(json.dumps({"models": [{"slug": "old-catalog"}]}), encoding="utf-8")
        paths = (self.p["official_cache"], self.p["catalog"])
        before = {path: path.read_bytes() for path in paths}
        writes = []
        write_json = toolctl._write_json

        def fail_catalog_write(path, value, mode=None):
            writes.append(path)
            if path == self.p["catalog"]:
                raise OSError("simulated catalog write failure")
            return write_json(path, value, mode)

        fresh = {"models": [{"slug": "gpt-new", "display_name": "New official"}], "etag": "new"}
        with mock.patch.object(toolctl, "fetch_official_models", return_value=fresh), \
             mock.patch.object(toolctl, "_write_json", side_effect=fail_catalog_write), \
             mock.patch.object(toolctl, "_restart_chatgpt") as restart:
            with self.assertRaisesRegex(OSError, "simulated catalog write failure"):
                self.service.discover_official_models()

        self.assertEqual(writes, [self.p["official_cache"], self.p["catalog"]])
        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        restart.assert_not_called()

    def test_ui_search_box_rerenders_only_the_model_table(self):
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        self.assertIn('id="model-table"', html)
        handler = re.search(r"search\.oninput=\(\)=>\{(.*?)\};", script, re.S).group(1)
        self.assertIn("renderModelTable()", handler)
        self.assertNotIn("render();", handler)
        self.assertIn("compositionstart", script)
        self.assertIn("compositionend", script)
        self.assertIn("if(silent)renderQuiet();else render();", script)
        self.assertIn("DRAFT=null;", script)

    def test_ui_quiet_poll_redraws_only_when_content_changes(self):
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        self.assertIn('class="scroll" id="model-table"', html)
        # 5 秒轮询走 renderQuiet：内容没变就完全不碰 DOM；否则每 5 秒重建一次 #page，
        # 模型列表（.scroll 内部滚动容器）会被打回顶部，勾选和搜索也一起被打断。
        quiet = re.search(r"function renderQuiet\(\)\{(.*?)\n\}", script, re.S).group(1)
        self.assertIn("html===PAGE_HTML", quiet)
        self.assertIn("drawPage(html);", quiet)
        self.assertIn("editing", quiet)
        self.assertNotIn("render();", quiet)
        # 内容确实变了才重建，重建时把 .scroll 内部滚动位置搬回去。
        draw = re.search(r"function drawPage\(html\)\{(.*?)\n\}", script, re.S).group(1)
        self.assertIn("const inner=captureInnerScroll();", draw)
        self.assertIn("PAGE_HTML=html;", draw)
        self.assertIn("restoreInnerScroll(inner);", draw)
        self.assertIn("document.querySelectorAll('#page .scroll')", script)
        # 局部重画（搜索框、勾选）后同步基准内容，下一次轮询不会再整页重建。
        table = re.search(r"function renderModelTable\(\)\{(.*?)\n\}", script, re.S).group(1)
        self.assertIn("PAGE_HTML=pageHtml();", table)

    def test_overview_uses_source_model_and_service_layout(self):
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        overview = re.search(r"function pageOverview\(\)\{(.*?)\n\}", script, re.S).group(1)
        source_list = re.search(r"function sourceListHtml\(overview\)\{(.*?)\n\}", script, re.S).group(1)
        self.assertIn(".overview-sources", html)
        self.assertIn(".sources-layout", html)
        self.assertIn(".source-list", html)
        self.assertIn(".overview-model-table", html)
        self.assertIn(".overview-service", html)
        self.assertIn(".actionbar", html)
        self.assertIn('data-action="${action}"', source_list)
        self.assertIn("overview?'select-overview-source'", source_list)
        self.assertIn("if(action==='select-overview-source')", script)
        self.assertNotIn("recent-events", overview)
        self.assertNotIn(".recent-events", html)
        self.assertIn("原生直通", script)
        self.assertIn("Chat 桥接", script)
        self.assertIn("官方直连", script)

    def test_ui_moves_data_path_to_settings_and_keeps_overview_theme_aware(self):
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        foot = re.search(r"function foot\(\)\{(.*?)\n\}", script, re.S).group(1)
        settings = re.search(r"function pageSettings\(\)\{(.*?)\n\}", script, re.S).group(1)
        self.assertNotIn("settings.runtime", foot)
        self.assertIn("s.settings.runtime", settings)
        self.assertIn("html[data-theme=\"dark\"] body{color-scheme:dark}", html)
        for variable in ("--bg", "--panel", "--text", "--accent"):
            self.assertRegex(html, rf"(?s)html\[data-theme=\"dark\"\].*?{variable}:")
        self.assertIn(".overview-source.on,.keyitem.on{background:var(--accent)", html)
        self.assertIn("background:var(--selected)", html)

    def test_settings_update_writes_admin_port_and_rejects_bad_value(self):
        self.p["settings"].parent.mkdir(parents=True, exist_ok=True)
        self.p["settings"].write_text(json.dumps({"previous_model": "gpt-5.1"}), encoding="utf-8")
        result = self.service.update_settings({"admin_port": 8891})
        self.assertEqual(result["settings"]["admin_port"], 8891)
        stored = json.loads(self.p["settings"].read_text(encoding="utf-8"))
        self.assertEqual(stored["previous_model"], "gpt-5.1")
        self.assertEqual(stored["admin_port"], 8891)
        with self.assertRaises(admin.ApiError):
            self.service.update_settings({"admin_port": 80})

    def test_settings_theme_defaults_to_light_and_persists(self):
        self.assertEqual(self.service.settings_view()["theme"], "light")
        self.p["settings"].write_text(json.dumps({"previous_model": "gpt-5.1", "theme": "blue"}), encoding="utf-8")
        self.assertEqual(self.service.settings_view()["theme"], "light")
        result = self.service.update_settings({"theme": "dark"})
        self.assertEqual(result["settings"]["theme"], "dark")
        stored = json.loads(self.p["settings"].read_text(encoding="utf-8"))
        self.assertEqual(stored["theme"], "dark")
        self.assertEqual(stored["previous_model"], "gpt-5.1")
        with self.assertRaises(admin.ApiError) as ctx:
            self.service.update_settings({"theme": "blue"})
        self.assertEqual(ctx.exception.code, "bad_theme")

    def test_settings_ui_style_defaults_to_layered_and_persists_with_each_theme(self):
        self.assertEqual(self.service.settings_view()["ui_style"], "layered")
        for legacy_style in ("index", "lexicon", "old"):
            self.p["settings"].write_text(
                json.dumps({"previous_model": "gpt-5.1", "ui_style": legacy_style}), encoding="utf-8"
            )
            self.assertEqual(self.service.settings_view()["ui_style"], "layered")
        with mock.patch.object(self.service.router, "ensure_running") as start, mock.patch.object(
            self.service.router, "stop"
        ) as stop, mock.patch.object(toolctl, "_restart_chatgpt") as restart:
            for style in ("layered", "chaos", "mintlify"):
                for theme in ("light", "dark"):
                    with self.subTest(style=style, theme=theme):
                        self.service.update_settings({"theme": theme})
                        result = self.service.update_settings({"ui_style": style})
                        self.assertEqual(result["settings"]["ui_style"], style)
                        self.assertEqual(result["settings"]["theme"], theme)
                        stored = json.loads(self.p["settings"].read_text(encoding="utf-8"))
                        self.assertEqual(stored["ui_style"], style)
                        self.assertEqual(stored["previous_model"], "gpt-5.1")
                        reopened = admin.AdminService(self.package, self.p, home=self.home)
                        self.assertEqual(reopened.settings_view()["ui_style"], style)
            start.assert_not_called()
            stop.assert_not_called()
            restart.assert_not_called()

    def test_settings_ui_style_rejects_invalid_values_before_other_changes(self):
        with mock.patch.object(self.service.autostart, "enable") as enable:
            for value in ("blue", "", None, True, {}, [], "index", "lexicon"):
                with self.subTest(value=value), self.assertRaises(admin.ApiError) as ctx:
                    self.service.update_settings({"autostart": True, "theme": "dark", "ui_style": value})
                self.assertEqual(ctx.exception.code, "bad_ui_style")
                self.assertEqual(ctx.exception.status, 400)
            enable.assert_not_called()
        self.assertFalse(self.p["settings"].exists())

    def test_settings_hide_dock_icon_defaults_on_and_persists(self):
        with mock.patch.object(toolctl, "is_windows", return_value=False):
            self.assertTrue(self.service.settings_view()["hide_dock_icon"])
            self.p["settings"].write_text(json.dumps({"previous_model": "gpt-5.1"}), encoding="utf-8")
            result = self.service.update_settings({"hide_dock_icon": False})
            self.assertFalse(result["settings"]["hide_dock_icon"])
            stored = json.loads(self.p["settings"].read_text(encoding="utf-8"))
            self.assertFalse(stored["hide_dock_icon"])
            self.assertEqual(stored["previous_model"], "gpt-5.1")
            restarted = admin.AdminService(self.package, self.p, home=self.home)
            self.assertFalse(restarted.settings_view()["hide_dock_icon"])
            self.assertTrue(self.service.update_settings({"hide_dock_icon": True})["settings"]["hide_dock_icon"])
            self.assertIn("hide_dock_icon saved value=True", self.service.events_view()[0]["message"])

    def test_settings_hide_dock_icon_rejects_non_boolean_values(self):
        with mock.patch.object(toolctl, "is_windows", return_value=False):
            for value in (None, 0, 1, "false", [], {}):
                with self.subTest(value=value), self.assertRaises(admin.ApiError) as ctx:
                    self.service.update_settings({"hide_dock_icon": value})
                self.assertEqual(ctx.exception.code, "bad_dock_visibility")
            self.assertFalse(self.p["settings"].exists())

    def test_settings_hide_dock_icon_is_unavailable_on_windows(self):
        with mock.patch.object(toolctl, "is_windows", return_value=True):
            self.assertFalse(self.service.settings_view()["hide_dock_icon"])
            with self.assertRaises(admin.ApiError) as ctx:
                self.service.update_settings({"hide_dock_icon": True, "theme": "dark"})
            self.assertEqual(ctx.exception.code, "macos_only")
            self.assertFalse(self.p["settings"].exists())

    def test_ui_exposes_theme_toggle_and_dark_palette(self):
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        self.assertIn('html[data-theme="dark"]', html)
        self.assertIn("function applyTheme(", script)
        self.assertIn("applyTheme(readThemeCache());", script)
        self.assertIn("applyTheme(STATE.settings.theme);", script)
        self.assertIn('data-action="set-theme"', script)
        self.assertIn("if(action==='set-theme')", script)
        self.assertIn("localStorage", script)

    def test_ui_version_comes_from_backend_state(self):
        state = self.service.state()
        self.assertEqual(state["app_version"], admin.APP_VERSION)
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function appVersionText(state)", html)
        self.assertIn("state.app_version", html)
        self.assertNotRegex(html, r"v\d+\.\d+\.\d+")

    def test_model_override_is_written_and_exposed_through_state(self):
        self.add_key("A")
        self.p["settings"].parent.mkdir(parents=True, exist_ok=True)
        self.p["settings"].write_text(json.dumps({"previous_model": "gpt-5.1"}), encoding="utf-8")
        result = self.service.update_model_override({
            "upstream_model": "kimi-k3", "input_modalities": ["text"], "supports_image_detail_original": False,
        })
        stored = json.loads(self.p["settings"].read_text(encoding="utf-8"))
        self.assertEqual(stored["previous_model"], "gpt-5.1")
        self.assertEqual(stored["model_overrides"]["kimi-k3"], {
            "input_modalities": ["text"], "supports_image_detail_original": False,
        })
        self.assertTrue(result["state"]["ok"])
        for bad in (
            {"upstream_model": "kimi-k3", "input_modalities": []},
            {"upstream_model": "kimi-k3", "input_modalities": ["audio"]},
            {"upstream_model": "", "input_modalities": ["text"]},
            {"upstream_model": "kimi-k3", "input_modalities": ["text"], "supports_image_detail_original": "yes"},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(admin.ApiError):
                    self.service.update_model_override(bad)

    def test_settings_overrides_reach_resolved_manifest_and_state(self):
        self.add_key("A")
        self.p["settings"].parent.mkdir(parents=True, exist_ok=True)
        self.p["settings"].write_text(json.dumps({"model_overrides": {"kimi-k3": {"input_modalities": ["text"]}}}), encoding="utf-8")
        policy_dir = self.home / "pkg"
        policy_dir.mkdir(parents=True, exist_ok=True)
        (policy_dir / "local-models.json").write_text(json.dumps({
            "version": 1,
            "upstream_base_url": "https://api.example.com/v1",
            "models": [{
                "slug": "local/placeholder",
                "display_name": "local/placeholder",
                "description": "placeholder",
                "priority": 51,
                "context_window": 200000,
                "default_reasoning_level": "medium",
                "supported_reasoning_levels": ["low", "medium"],
                "input_modalities": ["text", "image"],
                "supports_image_detail_original": True,
                "chat_options": {"explicit_tool_choice": "native"},
                "upstream_model": "not-discovered",
            }],
            "discovery": {"defaults": {"context_window": 200000, "chat_options": {"explicit_tool_choice": "native"}}},
        }), encoding="utf-8")
        catalog = {"models": ["kimi-k3"], "endpoints": {"kimi-k3": ["openai"]}}
        with mock.patch.object(toolctl, "fetch_gateway_catalog", return_value=catalog), mock.patch.object(
            toolctl, "probe_gateway_model", return_value="medium"
        ):
            manifest, _report = toolctl._resolve_runtime_manifest(policy_dir, self.p)
        self.assertEqual(len(manifest["models"]), 1)
        self.assertTrue(manifest["models"][0]["slug"].startswith("local/kimi-k3"))
        self.assertEqual(manifest["models"][0]["input_modalities"], ["text"])
        self.p["manifest"].parent.mkdir(parents=True, exist_ok=True)
        self.p["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(self.service.state()["models"][0]["input_modalities"], ["text"])

    def test_state_exposes_wire_api_for_direct_responses_models(self):
        key_id = self.add_key("A")

        def model(slug, upstream_model, **extra):
            return {
                "slug": slug,
                "display_name": slug,
                "description": slug,
                "priority": 51,
                "context_window": 200000,
                "default_reasoning_level": "max",
                "supported_reasoning_levels": ["none", "low", "medium", "high", "xhigh", "max"],
                "input_modalities": ["text", "image"],
                "supports_image_detail_original": True,
                "chat_options": {"explicit_tool_choice": "native"},
                "source": key_id,
                "upstream_model": upstream_model,
                **extra,
            }

        manifest = {
            "version": 2,
            "sources": {
                "official": {"kind": "official", "name": "ChatGPT 官方账号"},
                key_id: {
                    "kind": "key", "name": "A",
                    "upstream_base_url": "https://upstream.example.com/v1",
                    "api_key": "sk-test-000000000001",
                },
            },
            "models": [
                model("local/gpt-5.6-sol", "gpt-5.6-sol", wire_api="responses"),
                model("local/kimi-k3", "kimi/kimi-k3"),
            ],
        }
        self.p["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
        rows = {row["slug"]: row for row in self.service.state()["models"]}
        self.assertEqual(rows["local/gpt-5.6-sol"]["wire_api"], "responses")
        self.assertEqual(rows["local/kimi-k3"]["wire_api"], "chat")


class AdminMainTests(unittest.TestCase):
    def test_admin_log_is_utf8_without_bom(self):
        original = list(admin.log.handlers)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                log_dir = Path(tmp) / "logs"
                admin.configure_logging(log_dir)
                admin.log.info("\u4e2d\u6587 key \u6d4b\u8bd5")
                for handler in admin.log.handlers:
                    handler.flush()
                raw = (log_dir / "admin.log").read_bytes()
                self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
                self.assertIn("\u4e2d\u6587 key \u6d4b\u8bd5", raw.decode("utf-8"))
        finally:
            for handler in admin.log.handlers:
                handler.close()
            admin.log.handlers[:] = original

    def test_print_url_reports_configured_port_without_starting_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            codex = home / ".codex"
            codex.mkdir(parents=True, exist_ok=True)
            p = toolctl.paths(home, ROOT / "app")
            p["runtime"].mkdir(parents=True, exist_ok=True)
            p["settings"].write_text(json.dumps({"admin_port": 8892}), encoding="utf-8")
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = admin.main(["--package", str(ROOT / "app"), "--home", str(home), "--print-url"])
            self.assertEqual(code, 0)
            self.assertEqual(buffer.getvalue().strip(), "http://127.0.0.1:8892")

    def stale_probe(self, port_in_use, payload, pids=(4242,), image="python3", full="python3 admin.py --package x"):
        """Build the stubs for the stale admin checks; the caller restores them."""
        return [
            mock.patch.object(admin, "_port_in_use", return_value=port_in_use),
            mock.patch.object(admin, "_health_payload", return_value=payload),
            mock.patch.object(admin, "_listening_pids", return_value=list(pids)),
            mock.patch.object(admin, "_process_command", return_value=image),
            mock.patch.object(admin, "_process_full_command", return_value=full),
            mock.patch.object(admin, "_kill_pid"),
        ]

    def test_stop_stale_replaces_an_older_admin_service(self):
        payload = {"ok": True, "version": "6", "service": "admin"}
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.ExitStack() as stack:
                for patch in self.stale_probe(True, payload):
                    stack.enter_context(patch)
                # Busy while the old service lives, free right after it is killed.
                admin._port_in_use.side_effect = [True, False]
                result = admin._stop_stale_admin(8791)
                killed = admin._kill_pid.call_args_list
        self.assertEqual(result, 0)
        self.assertEqual([call.args[0] for call in killed], [4242])

    def test_stop_stale_keeps_the_service_when_the_version_matches(self):
        payload = {
            "ok": True,
            "version": "6",
            "app_version": admin.APP_VERSION,
            "build": admin.BUILD,
            "service": "admin",
        }
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.ExitStack() as stack:
                for patch in self.stale_probe(True, payload):
                    stack.enter_context(patch)
                result = admin._stop_stale_admin(8791)
                killed = admin._kill_pid.call_args_list
        self.assertEqual(result, 0)
        self.assertEqual(killed, [])

    def test_stop_stale_replaces_a_same_version_service_with_another_build(self):
        # A hot fix keeps APP_VERSION, so only the admin.py fingerprint can tell
        # the process running in memory apart from the freshly installed files.
        payload = {"ok": True, "version": "6", "app_version": admin.APP_VERSION, "service": "admin"}
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.ExitStack() as stack:
                for patch in self.stale_probe(True, payload):
                    stack.enter_context(patch)
                admin._port_in_use.side_effect = [True, False]
                result = admin._stop_stale_admin(8791)
                killed = admin._kill_pid.call_args_list
        self.assertEqual(result, 0)
        self.assertEqual([call.args[0] for call in killed], [4242])

    def test_stop_stale_never_touches_an_unverified_port_owner(self):
        # A foreign program on the admin port must be left alone.
        foreign = {"ok": True, "hello": "world"}
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.ExitStack() as stack:
                for patch in self.stale_probe(True, foreign):
                    stack.enter_context(patch)
                result = admin._stop_stale_admin(8791)
                killed = admin._kill_pid.call_args_list
        self.assertEqual(result, 1)
        self.assertEqual(killed, [])

        # A non Python process is left alone even when the port answers like us.
        ours = {"ok": True, "version": "6", "service": "admin"}
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.ExitStack() as stack:
                for patch in self.stale_probe(True, ours, image="/opt/other/daemon", full="/opt/other/daemon"):
                    stack.enter_context(patch)
                result = admin._stop_stale_admin(8791)
                killed = admin._kill_pid.call_args_list
        self.assertEqual(result, 1)
        self.assertEqual(killed, [])

    def test_stop_stale_skips_the_work_when_the_admin_port_is_free(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.ExitStack() as stack:
                for patch in self.stale_probe(False, {}):
                    stack.enter_context(patch)
                result = admin._stop_stale_admin(8791)
                killed = admin._kill_pid.call_args_list
        self.assertEqual(result, 0)
        self.assertEqual(killed, [])


class AdminHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        codex = self.home / ".codex"
        codex.mkdir(parents=True, exist_ok=True)
        (codex / "models_cache.json").write_text(json.dumps(official_cache()), encoding="utf-8")
        p = toolctl.paths(self.home, ROOT / "app")
        p["runtime"].mkdir(parents=True, exist_ok=True)
        self.service = admin.AdminService(ROOT / "app", p, home=self.home)
        self.previous = getattr(admin.AdminHandler, "service", None)
        admin.AdminHandler.service = self.service
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), admin.AdminHandler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        if self.previous is None:
            del admin.AdminHandler.service
        else:
            admin.AdminHandler.service = self.previous
        self.tmp.cleanup()

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def request(self, path, method="GET", body=None, origin=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.url(path), data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if origin:
            request.add_header("Origin", origin)
        return urllib.request.urlopen(request, timeout=10)

    def test_http_state_ui_and_key_creation(self):
        with self.request("/api/state") as response:
            state = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.headers.get_content_type(), "application/json")
        self.assertTrue(state["ok"])

        with self.request("/") as response:
            html = response.read().decode("utf-8")
            self.assertEqual(response.headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn("Codex 路由", html)
        self.assertIn("路由优先级", html)

        with self.request("/api/keys", "POST", {"name": "中文账号", "api_key": "sk-a-000000000001"}) as response:
            created = json.loads(response.read().decode("utf-8"))
        self.assertTrue(created["ok"])
        self.assertEqual(created["key"]["name"], "中文账号")

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("/api/nope")
        self.assertEqual(ctx.exception.code, 404)
        payload = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertEqual(payload["code"], "not_found")

    def test_official_discovery_http_task_publishes_catalog_and_preserves_user_config(self):
        import time

        created = self.service.create_key({
            "name": "Key A", "api_key": "sk-test-000000000001",
            "upstream_base_url": "https://upstream.example.com/v1",
        })
        key_id = created["key"]["id"]
        manifest_before = write_key_runtime_manifest(self.service.p, key_id)
        config_before = b'model = "gpt-5.1"\nmodel_provider = "custom"\n\n[model_providers.custom]\nname = "Keep"\n'
        self.service.p["config"].write_bytes(config_before)
        self.service.p["catalog"].write_text(json.dumps({"models": [{"slug": "old-catalog"}]}), encoding="utf-8")
        keys_before = self.service.p["keys"].read_bytes()
        fresh = {
            "models": [{"slug": "gpt-new", "display_name": "New official", "priority": 20}],
            "etag": "new-etag",
        }

        with mock.patch.object(toolctl, "fetch_official_models", return_value=fresh) as fetch, \
             mock.patch.object(toolctl, "_restart_chatgpt", return_value=False) as restart, \
             mock.patch.object(self.service, "discover_key") as discover_key, \
             mock.patch.object(self.service.router, "restart") as router_restart:
            with self.request("/api/official/models/discover", "POST", {}) as response:
                self.assertEqual(response.status, 202)
                accepted = json.loads(response.read().decode("utf-8"))
            self.assertEqual(accepted["kind"], "discover_official")
            self.assertIn(accepted["status"], ("running", "done"))

            deadline = time.monotonic() + 5
            while self.service.tasks.running() and time.monotonic() < deadline:
                time.sleep(0.01)
            task = self.service.tasks.snapshot()

        self.assertEqual(task["status"], "done")
        self.assertEqual(task["kind"], "discover_official")
        self.assertEqual(task["detail"], {"discovered": 1, "restart_pending": True})
        fetch.assert_called_once_with(self.service.p)
        restart.assert_called_once_with()
        discover_key.assert_not_called()
        router_restart.assert_not_called()
        self.assertEqual(self.service.p["config"].read_bytes(), config_before)
        self.assertEqual(self.service.p["manifest"].read_bytes(), manifest_before)
        self.assertEqual(self.service.p["keys"].read_bytes(), keys_before)

        cached = json.loads(self.service.p["official_cache"].read_text(encoding="utf-8"))
        catalog = json.loads(self.service.p["catalog"].read_text(encoding="utf-8"))
        self.assertEqual(cached["etag"], "new-etag")
        self.assertEqual([model["slug"] for model in self.service.state()["official"]["models"]], ["gpt-new"])
        self.assertEqual({model["slug"] for model in catalog["models"]}, {"gpt-new", "local/kimi-k3"})

    def test_http_serves_the_ui_brand_and_favicon(self):
        with self.request("/") as response:
            html = response.read().decode("utf-8")
        self.assertIn('rel="icon" href="logo.png"', html)
        self.assertIn('class="brand"', html)
        self.assertIn('<span class="wordmark">Codex</span><span>路由</span>', html)
        for path in ("/logo.png", "/favicon.ico"):
            with self.subTest(path=path), self.request(path) as response:
                blob = response.read()
                self.assertEqual(response.headers.get_content_type(), "image/png")
                self.assertTrue(blob.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_cross_origin_write_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("/api/keys", "POST", {"name": "x", "api_key": "sk-a-000000000002"},
                         origin="http://evil.example")
        self.assertEqual(ctx.exception.code, 403)
        payload = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertEqual(payload["code"], "forbidden_origin")
        self.assertEqual(self.service.state()["keys"], [])

    def test_quit_flow_suspends_the_router_without_switching_to_direct(self):
        """Quitting the tool must never post the official direct switch."""
        import time

        stopped = []
        switched = []
        self.service.router.stop = lambda: stopped.append(True)
        self.service.go_direct = lambda: switched.append(True)
        with self.request("/api/router/suspend", "POST") as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["kind"], "suspend")
        self.assertIn(payload["status"], ("running", "done"))
        deadline = time.time() + 10
        while self.service.tasks.running() and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.service.tasks.snapshot()["detail"], {"router": "suspended"})
        self.assertEqual(stopped, [True])
        self.assertEqual(switched, [])

    def test_service_quit_stops_the_router_and_the_background_service(self):
        """Closing the tool must not leave a python process behind."""
        import time

        stopped = []
        hooks = []
        switched = []
        self.service.router.stop = lambda: stopped.append(True)
        self.service.go_direct = lambda: switched.append(True)
        self.service.set_service_quit_hook(lambda: hooks.append(True))
        with self.request("/api/service/quit", "POST") as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload, {"kind": "quit", "status": "running"})
        deadline = time.time() + 10
        while not hooks and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(stopped, [True])
        self.assertEqual(hooks, [True])
        self.assertEqual(switched, [])

    def test_healthz_announces_the_release_so_the_launcher_can_compare_it(self):
        with self.request("/healthz") as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["service"], "admin")
        self.assertEqual(payload["app_version"], admin.APP_VERSION)
        # The fingerprint lets the launcher drop a same-version stale service.
        self.assertEqual(payload["build"], admin.BUILD)
        self.assertTrue(admin.BUILD)

    def test_model_override_endpoint_writes_settings_and_returns_state(self):
        with self.request("/api/models/overrides", "PUT", {
            "upstream_model": "kimi-k3", "input_modalities": ["text"],
        }) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["state"]["ok"])
        stored = json.loads(self.service.p["settings"].read_text(encoding="utf-8"))
        self.assertEqual(stored["model_overrides"]["kimi-k3"]["input_modalities"], ["text"])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("/api/models/overrides", "PUT", {"upstream_model": "kimi-k3", "input_modalities": ["audio"]})
        self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(json.loads(ctx.exception.read().decode("utf-8"))["code"], "bad_override")

    def test_key_endpoints_validate_input(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("/api/keys", "POST", {"name": "bad", "api_key": "sk-a-1",
                                                 "upstream_base_url": "ftp://nope"})
        self.assertEqual(ctx.exception.code, 400)
        payload = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertEqual(payload["code"], "invalid_keys")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("/api/keys/missing", "DELETE")
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
