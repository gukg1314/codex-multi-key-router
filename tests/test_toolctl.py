import base64
import io
import json
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import toolctl


def local_model(slug="local/kimi-k3"):
    return {
        "slug": slug,
        "display_name": slug,
        "description": "Local model",
        "priority": 54,
        "context_window": 200000,
        "default_reasoning_level": "medium",
        "supported_reasoning_levels": ["low", "medium", "high", "xhigh", "max"],
        "input_modalities": ["text", "image"],
        "supports_image_detail_original": True,
        "chat_options": {"explicit_tool_choice": "native"},
    }


def manifest(*models):
    return {"version": 1, "upstream_base_url": "https://api.example.com/v1", "models": list(models)}


def policy(*models, excluded=None, overrides=None):
    return {
        **manifest(*models),
        "discovery": {
            "excluded_models": excluded or [],
            "defaults": {
                "context_window": 200000,
                "input_modalities": ["text", "image"],
                "supports_image_detail_original": True,
                "chat_options": {"explicit_tool_choice": "native"},
            },
            "model_overrides": overrides or {},
        },
    }


def listed(model_id, endpoints=("openai",)):
    """构造 /models 的一条记录；endpoints=None 表示该网关不声明端点能力。"""
    item = {"id": model_id}
    if endpoints is not None:
        item["supported_endpoint_types"] = list(endpoints)
    return item


class ToolctlTests(unittest.TestCase):
    def test_gateway_discovery_adds_compatible_models_with_max_reasoning_and_images(self):
        requests = []

        def gateway(method, path, key, payload=None):
            requests.append((method, path, key, payload))
            if path == "/models":
                return 200, {"data": [
                    listed("auto"),
                    listed("local/deepseek-v4.1-flash"),
                    listed("local/Qwen-Chat"),
                    listed("local/Qwen3-Embedding-8B"),
                ]}
            if path == "/responses":
                # 该假网关没有直通端点：Chat 探测失败后的兜底探测会打到这里。
                return 404, {"error": {"message": "responses endpoint unavailable"}}
            self.assertEqual(path, "/chat/completions")
            if payload["model"] == "local/Qwen3-Embedding-8B":
                return 404, {"error": {"message": "not a chat model"}}
            self.assertEqual(payload.get("reasoning_effort"), "max")
            if payload["messages"][-1]["role"] == "tool":
                return 200, {"choices": [{"message": {"role": "assistant", "content": "probe ok"}}]}
            return 200, {"choices": [{"message": {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "call_probe", "type": "function",
                    "function": {"name": "codex_model_probe", "arguments": '{"input":"ok"}'},
                }],
            }}]}

        known_auto = local_model("auto")
        known_auto["default_reasoning_level"] = "max"
        known_deepseek = local_model("local/deepseek-v4.1-flash")
        known_deepseek["default_reasoning_level"] = "max"
        resolved, report = toolctl.resolve_gateway_manifest(
            policy(known_auto, known_deepseek), "secret", request_json=gateway,
        )

        self.assertEqual(report, {
            "discovered": 4, "included": 3, "added": 1,
            "excluded": [], "skipped": ["local/Qwen3-Embedding-8B"],
        })
        self.assertEqual([item[1] for item in requests].count("/models"), 1)
        qwen = next(item for item in resolved["models"] if item.get("upstream_model") == "local/Qwen-Chat")
        self.assertRegex(qwen["slug"], r"^local/qwen-chat-[0-9a-f]{8}$")
        self.assertEqual(qwen["display_name"], "local/Qwen-Chat")
        self.assertEqual(qwen["default_reasoning_level"], "max")
        self.assertEqual(qwen["supported_reasoning_levels"], ["none", "minimal", "low", "medium", "high", "xhigh", "max"])
        self.assertEqual(qwen["input_modalities"], ["text", "image"])
        self.assertTrue(qwen["supports_image_detail_original"])
        self.assertEqual(
            [item[3]["messages"][-1]["role"] for item in requests if item[1] == "/chat/completions" and item[3]["model"] == "local/Qwen-Chat"],
            ["user", "tool"],
        )
        self.assertFalse(
            any(
                "parallel_tool_calls" in item[3]
                for item in requests
                if item[1] == "/chat/completions" and item[3]["messages"][-1]["role"] == "tool"
            ),
            "第二轮 chat 探测不应再带 parallel_tool_calls",
        )

    def test_gateway_discovery_upgrades_declared_responses_model(self):
        cached_sol = local_model("local/gpt-5.6-sol")
        cached_sol.update({
            "upstream_model": "gpt-5.6-sol",
            "default_reasoning_level": "none",
            "supported_reasoning_levels": ["none"],
        })

        def gateway(_method, path, _key, payload=None):
            if path == "/models":
                return 200, {"data": [listed("gpt-5.6-sol", ("openai", "openai-response"))]}
            self.assertEqual(path, "/responses")
            self.assertEqual(payload["tool_choice"], "required")
            self.assertEqual(payload["reasoning"], {"effort": "max"})
            return 200, {"status": "completed", "output": [
                {"type": "custom_tool_call", "name": "codex_custom_probe", "input": "ok"},
            ]}

        resolved, _report = toolctl.resolve_gateway_manifest(
            policy(local_model("auto")), "secret",
            previous_manifest=manifest(cached_sol), request_json=gateway,
        )
        sol = next(model for model in resolved["models"] if model["slug"] == "local/gpt-5.6-sol")
        self.assertEqual(sol["wire_api"], "responses")
        self.assertEqual(sol["default_reasoning_level"], "max")
        self.assertEqual(
            sol["supported_reasoning_levels"],
            ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
        )

    def test_gateway_discovery_exposes_every_reasoning_level_up_to_detected_maximum(self):
        def gateway(_method, path, _key, payload=None):
            if path == "/models":
                return 200, {"data": [listed("auto"), listed("local/qwen38-flash-next")]}
            if payload.get("reasoning_effort") == "max":
                return 400, {"error": {"message": "max is unsupported"}}
            if payload["messages"][-1]["role"] == "tool":
                return 200, {"choices": [{"message": {"role": "assistant", "content": "probe ok"}}]}
            return 200, {"choices": [{"message": {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "call_probe", "type": "function",
                    "function": {"name": "codex_model_probe", "arguments": '{"input":"ok"}'},
                }],
            }}]}

        resolved, _report = toolctl.resolve_gateway_manifest(
            policy(local_model("auto")), "secret", request_json=gateway,
        )

        qwen = next(model for model in resolved["models"] if model["slug"] == "local/qwen38-flash-next")
        self.assertEqual(qwen["default_reasoning_level"], "xhigh")
        self.assertEqual(
            qwen["supported_reasoning_levels"],
            ["none", "minimal", "low", "medium", "high", "xhigh"],
        )

    def test_gateway_discovery_upgrades_cached_single_reasoning_level(self):
        cached_qwen = local_model("local/qwen38-flash-next")
        cached_qwen["default_reasoning_level"] = "xhigh"
        cached_qwen["supported_reasoning_levels"] = ["xhigh"]

        def gateway(_method, path, _key, _payload=None):
            self.assertEqual(path, "/models")
            return 200, {"data": [listed("auto"), listed("local/qwen38-flash-next")]}

        resolved, _report = toolctl.resolve_gateway_manifest(
            policy(local_model("auto")),
            "secret",
            previous_manifest=manifest(local_model("auto"), cached_qwen),
            request_json=gateway,
        )

        qwen = next(model for model in resolved["models"] if model["slug"] == "local/qwen38-flash-next")
        self.assertEqual(
            qwen["supported_reasoning_levels"],
            ["none", "minimal", "low", "medium", "high", "xhigh"],
        )

    def test_gateway_discovery_applies_exclusion_and_image_override(self):
        def gateway(_method, _path, _key, _payload=None):
            return 200, {"data": [listed("auto"), listed("local/deepseek-v4.1-flash")]}

        deepseek = local_model("local/deepseek-v4.1-flash")
        resolved, report = toolctl.resolve_gateway_manifest(
            policy(
                deepseek,
                excluded=["auto"],
                overrides={"local/deepseek-v4.1-flash": {
                    "input_modalities": ["text"],
                    "supports_image_detail_original": False,
                }},
            ),
            "secret",
            request_json=gateway,
        )

        self.assertEqual([item["slug"] for item in resolved["models"]], ["local/deepseek-v4.1-flash"])
        self.assertEqual(resolved["models"][0]["input_modalities"], ["text"])
        self.assertFalse(resolved["models"][0]["supports_image_detail_original"])
        self.assertEqual(report["excluded"], ["auto"])

    def test_refresh_command_fetches_gateway_and_writes_resolved_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "router.py").write_text("# router\n", encoding="utf-8")
            (source / "local-models.json").write_text(
                json.dumps(policy(local_model("local/deepseek-v4.1-flash"))), encoding="utf-8",
            )
            p = toolctl.paths(root)
            p["config"].parent.mkdir(parents=True)
            p["config"].write_text('model_provider = "openai"\n', encoding="utf-8")
            p["official_cache"].write_text(json.dumps({"models": [{
                "slug": "gpt-a", "display_name": "A", "priority": 10,
                "default_reasoning_level": "low",
                "supported_reasoning_levels": [{"effort": "low", "description": "Low"}],
            }]}), encoding="utf-8")
            toolctl.write_local_key(p["key"], "secret")

            def gateway(_method, path, _key, payload=None):
                if path == "/models":
                    return 200, {"data": [listed("local/deepseek-v4.1-flash"), listed("local/new-chat")]}
                if payload["messages"][-1]["role"] == "tool":
                    return 200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
                return 200, {"choices": [{"message": {
                    "role": "assistant", "content": None,
                    "tool_calls": [{
                        "id": "call_probe", "type": "function",
                        "function": {"name": "codex_model_probe", "arguments": '{"input":"ok"}'},
                    }],
                }}]}

            with mock.patch.object(toolctl, "gateway_requester", return_value=gateway) as request, mock.patch("builtins.print"):
                toolctl.command_refresh(source, p)

            runtime_manifest = json.loads(p["manifest"].read_text(encoding="utf-8"))
            self.assertEqual(runtime_manifest["version"], 2)
            self.assertEqual(
                [item["slug"] for item in runtime_manifest["models"]],
                ["local/deepseek-v4.1-flash", "local/new-chat"],
            )
            self.assertEqual(len({item["source"] for item in runtime_manifest["models"]}), 1)
            self.assertIn(runtime_manifest["models"][0]["source"], runtime_manifest["sources"])
            self.assertNotIn("discovery", runtime_manifest)
            self.assertTrue(request.called)

    def test_prepare_runtime_records_official_cache_without_publishing_agent_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "router.py").write_text("# router\n", encoding="utf-8")
            p = toolctl.paths(root)
            hidden = local_model("local/gpt-6-luna-kaaa000001")
            hidden.update({"upstream_model": "GPT-6 Luna", "source": "kaaa000001"})
            runtime_manifest = {
                "version": 2,
                "sources": {
                    "official": {"kind": "official"},
                    "kaaa000001": {
                        "kind": "key", "name": "A",
                        "upstream_base_url": "https://a.example.com/v1", "api_key": "sk-aaa",
                    },
                },
                "models": [],
                "agent_models": [hidden],
            }
            official_cache = {"models": [{
                "slug": "gpt-6-luna", "display_name": "GPT-6 Luna", "priority": 10,
                "default_reasoning_level": "low",
                "supported_reasoning_levels": [{"effort": "low", "description": "Low"}],
            }]}

            toolctl._prepare_runtime(source, p, runtime_manifest, official_cache)

            written_manifest = json.loads(p["manifest"].read_text(encoding="utf-8"))
            catalog = json.loads(p["catalog"].read_text(encoding="utf-8"))
            self.assertEqual(written_manifest["official_models"], ["gpt-6-luna"])
            self.assertEqual(written_manifest["official_cache_path"], str(p["official_cache"].resolve()))
            self.assertEqual(written_manifest["models"], [])
            self.assertEqual([model["slug"] for model in written_manifest["agent_models"]], [hidden["slug"]])
            self.assertEqual([model["slug"] for model in catalog["models"]], ["gpt-6-luna"])

    def test_catalog_merge_preserves_official_shape_order_and_appends_unique_local(self):
        official = {"models": [
            {"slug": "gpt-a", "display_name": "A", "priority": 10, "default_reasoning_level": "low", "supported_reasoning_levels": [{"effort": "low", "description": "Low"}], "unknown": {"keep": True}},
            {"slug": "local/kimi-k3", "display_name": "stale"},
            {"slug": "gpt-b", "display_name": "B", "priority": 20, "default_reasoning_level": "medium", "supported_reasoning_levels": [{"effort": "medium", "description": "Medium"}]},
        ], "etag": "keep"}
        result = toolctl.merge_catalog(official, manifest(local_model(), local_model("local/future")))
        self.assertEqual([m["slug"] for m in result["models"]], ["gpt-a", "gpt-b", "local/kimi-k3", "local/future"])
        self.assertEqual(result["models"][0], official["models"][0])
        self.assertEqual(result["etag"], "keep")
        self.assertEqual(result["models"][-1]["supported_reasoning_levels"][0]["effort"], "low")

    def test_auto_gateway_model_is_valid_and_added_to_catalog(self):
        auto = local_model("auto")
        value = manifest(local_model(), auto)

        self.assertIs(toolctl.validate_manifest(value), value)
        result = toolctl.merge_catalog({"models": [{
            "slug": "gpt-a", "display_name": "A", "priority": 10,
            "default_reasoning_level": "low",
            "supported_reasoning_levels": [{"effort": "low", "description": "Low"}],
        }]}, value)

        self.assertEqual([model["slug"] for model in result["models"]], ["gpt-a", "local/kimi-k3", "auto"])

    def test_catalog_merge_drops_official_models_with_the_same_identity(self):
        official = {"models": [
            {"slug": "gpt-5.6-sol", "display_name": "GPT-5.6 Sol", "priority": 10},
            {"slug": "gpt-5.6-terra", "display_name": "GPT-5.6 Terra", "priority": 11},
        ]}
        local = {
            **local_model("local/gpt-5.6-sol-419255f2"),
            "display_name": "GPT-5.6 Sol",
            "upstream_model": "GPT-5.6 Sol",
        }
        result = toolctl.merge_catalog(official, manifest(local))
        self.assertEqual([model["slug"] for model in result["models"]], ["gpt-5.6-terra", "local/gpt-5.6-sol-419255f2"])
        self.assertEqual(toolctl.model_identity("gpt-5.6-sol"), toolctl.model_identity("GPT-5.6 Sol"))
        self.assertEqual(toolctl.model_identity("gpt5.6.sol"), toolctl.model_identity("gpt_5.6_sol"))

    def test_catalog_merge_keeps_the_official_entry_when_the_key_does_not_own_it(self):
        official = {"models": [{"slug": "gpt-5.6-sol", "display_name": "GPT-5.6 Sol", "priority": 10}]}
        empty = {"version": 2, "sources": {"official": {"kind": "official", "name": "ChatGPT"}}, "models": []}
        result = toolctl.merge_catalog(official, empty)
        self.assertEqual([model["slug"] for model in result["models"]], ["gpt-5.6-sol"])

    def test_manifest_rejects_high_risk_invalid_inputs(self):
        cases = {}
        cases["version"] = {**manifest(local_model()), "version": 2}
        cases["upstream"] = {**manifest(local_model()), "upstream_base_url": "https://example.invalid/v1"}
        cases["duplicate"] = manifest(local_model(), local_model())
        cases["official-name"] = manifest({**local_model(), "slug": "gpt-5"})
        cases["uppercase"] = manifest({**local_model(), "slug": "local/Kimi"})
        cases["context"] = manifest({**local_model(), "context_window": 0})
        cases["effort"] = manifest({**local_model(), "supported_reasoning_levels": ["turbo"]})
        cases["default"] = manifest({**local_model(), "default_reasoning_level": "ultra"})
        cases["modality"] = manifest({**local_model(), "input_modalities": ["audio"]})
        cases["chat-option"] = manifest({**local_model(), "chat_options": {"unknown": True}})
        for name, value in cases.items():
            with self.subTest(name=name), self.assertRaises(toolctl.ValidationError):
                toolctl.validate_manifest(value)

    def test_unified_config_patch_only_changes_managed_top_level_keys(self):
        source = '''model = "old"\nmodel_reasoning_effort = "low"\napproval_policy = "never"\n\n[model_providers.custom]\nname = "Keep"\nmodel = "nested-must-stay"\n\n[projects."/tmp/demo"]\ntrust_level = "trusted"\n'''
        patched = toolctl.patch_config(source, {
            "model": "gpt-a",
            "model_reasoning_effort": "medium",
            "model_provider": toolctl.MODEL_PROVIDER_ID,
            "openai_base_url": None,
            "model_catalog_json": "/runtime/merged-models.json",
        }, toolctl.PROVIDER_CONFIG)
        self.assertIn('approval_policy = "never"', patched)
        self.assertIn('[model_providers.custom]\nname = "Keep"\nmodel = "nested-must-stay"', patched)
        self.assertNotIn("openai_base_url =", patched)
        self.assertEqual(patched.count("[model_providers.codex_official_local]"), 1)
        self.assertEqual(toolctl.read_provider_config(patched), toolctl.PROVIDER_CONFIG)
        values = toolctl.read_managed_config(patched)
        self.assertEqual(values["model_provider"], toolctl.MODEL_PROVIDER_ID)
        self.assertEqual(values["model"], "gpt-a")
        self.assertNotEqual(values.get("model"), "nested-must-stay")
        self.assertEqual(
            toolctl.patch_config(patched, {
                "model": "gpt-a", "model_reasoning_effort": "medium",
                "model_provider": toolctl.MODEL_PROVIDER_ID, "openai_base_url": None,
                "model_catalog_json": "/runtime/merged-models.json",
            }, toolctl.PROVIDER_CONFIG),
            patched,
        )

    def test_direct_config_removes_router_and_selects_valid_fallback(self):
        cache = {"models": [
            {"slug": "gpt-top", "priority": 100, "visibility": "list", "default_reasoning_level": "high", "supported_reasoning_levels": [{"effort": "medium"}, {"effort": "high"}]},
            {"slug": "gpt-5.6-terra", "priority": 10, "visibility": "list", "default_reasoning_level": "medium", "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}]},
        ]}
        model, effort = toolctl.select_fallback(cache, {"model": "gpt-5.6-terra", "reasoning_effort": "low"})
        self.assertEqual((model, effort), ("gpt-5.6-terra", "low"))
        source = '''openai_base_url = "http://127.0.0.1:8790"\nmodel_catalog_json = "/x"\nfoo = 1\n\n[model_providers.custom]\nname = "Keep"\n\n[model_providers.codex_official_local]\nname = "Old"\nbase_url = "http://127.0.0.1:8790/v1"\n'''
        patched = toolctl.patch_config(source, {
            "model_provider": "openai", "model": model, "model_reasoning_effort": effort,
            "openai_base_url": None, "model_catalog_json": None,
        }, None)
        self.assertNotIn("openai_base_url", patched)
        self.assertNotIn("model_catalog_json", patched)
        self.assertNotIn("model_providers.codex_official_local", patched)
        self.assertIn('[model_providers.custom]\nname = "Keep"', patched)
        self.assertIn("foo = 1", patched)

    def test_enable_transaction_health_or_verify_failure_restores_and_unloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            config.write_text('model = "before"\n', encoding="utf-8")
            controller = mock.Mock()
            controller.start.return_value = True
            controller.health.return_value = {"status": "bad"}
            with self.assertRaises(toolctl.ControlError):
                toolctl.enable_transaction(
                    config,
                    root / "backups",
                    controller,
                    {"model": "gpt-a", "model_reasoning_effort": "medium", "model_provider": "openai", "openai_base_url": "http://127.0.0.1:8790", "model_catalog_json": "/x"},
                    expected_model_count=2,
                )
            self.assertEqual(config.read_text(encoding="utf-8"), 'model = "before"\n')
            controller.stop.assert_called_once()

            controller.reset_mock()
            controller.start.return_value = True
            controller.health.return_value = {
                "status": "ok", "version": toolctl.VERSION, "transport": "http-only",
                "bridge": toolctl.BRIDGE_NAME, "local_model_count": 2,
            }
            with mock.patch.object(toolctl, "verify_config", side_effect=toolctl.ControlError("bad verify")):
                with self.assertRaises(toolctl.ControlError):
                    toolctl.enable_transaction(
                        config, root / "backups", controller, {"model": "gpt-a"}, 2,
                        toolctl.PROVIDER_CONFIG,
                    )
            self.assertEqual(config.read_text(encoding="utf-8"), 'model = "before"\n')
            controller.stop.assert_called_once()

    def test_key_permissions_and_launchagent_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key_path = root / "local-api-key.secret"
            toolctl.write_local_key(key_path, "  secret-value\n")
            self.assertEqual(key_path.read_text(encoding="utf-8"), "secret-value")
            self.assertEqual(os.stat(key_path).st_mode & 0o777, 0o600)
            with self.assertRaises(toolctl.ValidationError):
                toolctl.write_local_key(key_path, "bad\nkey")
            plist_path = root / "agent.plist"
            runtime = root / "runtime"
            toolctl.write_launchagent(plist_path, runtime)
            data = plistlib.loads(plist_path.read_bytes())
            self.assertEqual(data["Label"], toolctl.LAUNCH_LABEL)
            self.assertEqual(data["ProgramArguments"][1], str(runtime / "router.py"))
            self.assertTrue(data["RunAtLoad"])
            self.assertEqual(data["KeepAlive"], {"SuccessfulExit": False})
            self.assertGreaterEqual(data["ThrottleInterval"], 5)

            with mock.patch.object(toolctl, "LaunchController") as controller_type, mock.patch("builtins.print"):
                toolctl.command_stop(toolctl.paths(root))
                controller_type.assert_called_once_with(root / "Library" / "LaunchAgents" / f"{toolctl.LAUNCH_LABEL}.plist", root / "Library" / "Application Support" / "CodexOfficialLocalRouter")
                controller_type.return_value.stop.assert_called_once_with()

            with mock.patch.object(toolctl.shutil, "which", return_value=None), mock.patch.object(
                toolctl.Path, "is_file", return_value=False
            ), mock.patch.dict(sys.modules, {"compression.zstd": None, "zstandard": None}):
                with self.assertRaises(toolctl.ControlError):
                    toolctl._check_zstd_decoder()


class OfficialModelFetchTests(unittest.TestCase):
    def _paths_with_auth(self, root, tokens):
        p = toolctl.paths(root)
        p["official_cache"].parent.mkdir(parents=True)
        (p["official_cache"].parent / "auth.json").write_text(
            json.dumps({"auth_mode": "chatgpt", "tokens": tokens}), encoding="utf-8",
        )
        p["official_cache"].write_text('{"models":[{"slug":"cached-model"}]}', encoding="utf-8")
        return p

    def test_fetch_official_models_preserves_catalog_and_sends_optional_account_header(self):
        model = {
            "slug": "gpt-5.6-sol",
            "display_name": "GPT-5.6 Sol",
            "description": "Official model",
            "priority": 10,
            "context_window": 256000,
            "max_output_tokens": 32768,
            "default_reasoning_level": "high",
            "supported_reasoning_levels": [{"effort": "high", "description": "High reasoning"}],
            "input_modalities": ["text", "image"],
            "supports_image_detail_original": True,
            "chat_options": {"explicit_tool_choice": "native"},
        }
        payload = {"models": [model], "identity": {"account": "must be removed"}, "catalog_version": 7}
        raw_cache = '{"models":[{"slug":"cached-model"}]}'

        for account_id in ("acct-test-123", None):
            with self.subTest(account_id=account_id), tempfile.TemporaryDirectory() as tmp:
                p = self._paths_with_auth(Path(tmp), {
                    "access_token": "test-access-token",
                    **({"account_id": account_id} if account_id else {}),
                })
                response = mock.MagicMock()
                response.status = 200
                response.read.return_value = json.dumps(payload).encode("utf-8")
                response.headers.get.return_value = '"models-v7"'
                response_context = mock.MagicMock()
                response_context.__enter__.return_value = response

                with mock.patch.object(toolctl, "_official_client_version", return_value="1.2.3"), mock.patch.object(
                    toolctl.urllib.request, "urlopen", return_value=response_context,
                ) as urlopen:
                    result = toolctl.fetch_official_models(p)

                request = urlopen.call_args.args[0]
                parsed = toolctl.urllib.parse.urlsplit(request.full_url)
                self.assertEqual(parsed.path, "/backend-api/codex/models")
                self.assertEqual(toolctl.urllib.parse.parse_qs(parsed.query), {"client_version": ["1.2.3"]})
                headers = {name.lower(): value for name, value in request.header_items()}
                self.assertEqual(headers["authorization"], "Bearer test-access-token")
                self.assertEqual(headers.get("chatgpt-account-id"), account_id)
                self.assertEqual(headers["user-agent"], "codex_cli_rs/1.2.3")
                self.assertEqual(result["models"], [model])
                self.assertEqual(result["catalog_version"], 7)
                self.assertNotIn("identity", result)
                self.assertEqual(result["etag"], '"models-v7"')
                self.assertEqual(result["client_version"], "1.2.3")
                self.assertTrue(result["fetched_at"])
                self.assertEqual(p["official_cache"].read_text(encoding="utf-8"), raw_cache)

    def test_fetch_official_models_failures_keep_cache_and_hide_sensitive_details(self):
        failures = (
            ("401", lambda: toolctl.urllib.error.HTTPError("https://chatgpt.com", 401, "token leaked", {}, None)),
            ("403", lambda: toolctl.urllib.error.HTTPError("https://chatgpt.com", 403, "token leaked", {}, None)),
            ("network", lambda: toolctl.urllib.error.URLError("test-access-token")),
            ("invalid-json", None),
        )
        for label, failure in failures:
            with self.subTest(failure=label), tempfile.TemporaryDirectory() as tmp:
                p = self._paths_with_auth(Path(tmp), {"access_token": "test-access-token"})
                original_cache = p["official_cache"].read_bytes()
                if failure is None:
                    response = mock.MagicMock()
                    response.status = 200
                    response.read.return_value = b'{"models": ["test-access-token"'
                    response.headers.get.return_value = None
                    response_context = mock.MagicMock()
                    response_context.__enter__.return_value = response
                    urlopen = mock.patch.object(toolctl.urllib.request, "urlopen", return_value=response_context)
                else:
                    urlopen = mock.patch.object(toolctl.urllib.request, "urlopen", side_effect=failure())

                with mock.patch.object(toolctl, "_official_client_version", return_value="1.2.3"), urlopen:
                    with self.assertRaises(toolctl.ControlError) as raised:
                        toolctl.fetch_official_models(p)

                self.assertNotIn("test-access-token", str(raised.exception))
                self.assertNotIn("token leaked", str(raised.exception))
                self.assertEqual(p["official_cache"].read_bytes(), original_cache)

    def test_official_client_version_hides_cli_window_and_falls_back_to_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = toolctl.paths(Path(tmp))
            p["official_cache"].parent.mkdir(parents=True)
            p["official_cache"].write_text(json.dumps({"client_version": "9.8.7"}), encoding="utf-8")
            with mock.patch.object(toolctl.Path, "exists", return_value=False), mock.patch.object(
                toolctl.shutil, "which", return_value="/fake/codex",
            ), mock.patch.object(toolctl, "hidden_subprocess_kwargs", return_value={"creationflags": 123}), mock.patch.object(
                toolctl.subprocess, "run", return_value=mock.Mock(returncode=1, stdout=""),
            ) as run:
                self.assertEqual(toolctl._official_client_version(p), "9.8.7")

            self.assertEqual(run.call_args.kwargs["creationflags"], 123)
            self.assertEqual(run.call_args.kwargs["timeout"], 5)


class FakeWinreg:
    """最小可用的 Windows 注册表替身。"""

    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 1
    KEY_QUERY_VALUE = 2
    REG_SZ = 1

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def __init__(self):
        self.values = {}

    def CreateKeyEx(self, *_args):
        return self._Key()

    def OpenKey(self, *_args):
        return self._Key()

    def SetValueEx(self, _key, name, _reserved, _type, value):
        self.values[name] = value

    def QueryValueEx(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def DeleteValue(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


class WindowsCompatTests(unittest.TestCase):
    def test_windows_paths_keep_runtime_inside_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            package = root / "Codex官方本地模型工具"
            package.mkdir()
            with mock.patch.object(toolctl, "is_windows", return_value=True):
                p = toolctl.paths(home, package)
            self.assertEqual(p["package"], package)
            self.assertEqual(p["runtime"], package / "runtime")
            self.assertEqual(p["key"], package / "runtime" / "local-api-key.secret")
            self.assertEqual(p["logs"], package / "runtime" / "logs")
            self.assertEqual(p["config"], home / ".codex" / "config.toml")
            self.assertEqual(p["official_cache"], home / ".codex" / "models_cache.json")

    def test_launch_controller_factory_picks_platform(self):
        p = {"runtime": Path("/tmp/pkg/runtime"), "launchagent": Path("/tmp/pkg/agent.plist")}
        with mock.patch.object(toolctl, "is_windows", return_value=False):
            self.assertIsInstance(toolctl.launch_controller(p), toolctl.LaunchController)
        with mock.patch.object(toolctl, "is_windows", return_value=True):
            self.assertIsInstance(toolctl.launch_controller(p), toolctl.WindowsController)

    def test_windows_autostart_registry_lifecycle_and_spawn_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "pkg"
            runtime = package / "runtime"
            fake = FakeWinreg()
            controller = toolctl.WindowsController(runtime, python_exe=package / "python" / "python.exe")
            self.assertEqual(controller.package_dir, package)
            self.assertEqual(
                controller.autostart_command(),
                f'"{package / "python" / "python.exe"}" "{package / "toolctl.py"}" autostart',
            )

            with mock.patch.object(toolctl, "winreg", fake), mock.patch.object(controller, "ensure_running") as ensure:
                self.assertTrue(controller.start())
                ensure.assert_called_once_with()
                self.assertTrue(controller.is_enabled())
            self.assertEqual(fake.values[toolctl.WINDOWS_AUTOSTART_NAME], controller.autostart_command())

            with mock.patch.object(toolctl, "winreg", fake), mock.patch.object(controller, "_stop_router_processes") as stop:
                controller.stop()
                stop.assert_called_once_with()
                self.assertFalse(controller.is_enabled())

            with mock.patch.object(toolctl, "_port_listening", return_value=True), mock.patch.object(
                controller, "_spawn_router"
            ) as spawn:
                controller.ensure_running()
                spawn.assert_not_called()
            with mock.patch.object(toolctl, "_port_listening", return_value=False), mock.patch.object(
                controller, "_spawn_router"
            ) as spawn:
                controller.ensure_running()
                spawn.assert_called_once_with()

    def test_windows_port_owner_parsing_and_foreign_process_guard(self):
        netstat = (
            "  TCP    127.0.0.1:8790         0.0.0.0:0              LISTENING       4321\r\n"
            "  TCP    127.0.0.1:8791         0.0.0.0:0              LISTENING       9999\r\n"
            "  TCP    127.0.0.1:8790         127.0.0.1:55000        ESTABLISHED     4321\r\n"
        )
        with mock.patch.object(toolctl.subprocess, "run", return_value=mock.Mock(stdout=netstat)):
            self.assertEqual(toolctl._windows_listening_pids(8790), [4321])
        with mock.patch.object(
            toolctl.subprocess, "run", return_value=mock.Mock(stdout='"python.exe","4321","Console","1","1 K"\r\n')
        ):
            self.assertEqual(toolctl._windows_process_image(4321), "python.exe")
        with mock.patch.object(
            toolctl.subprocess, "run", return_value=mock.Mock(stdout="INFO: No tasks are running.\r\n")
        ):
            self.assertEqual(toolctl._windows_process_image(4321), "")

        controller = toolctl.WindowsController(Path("/tmp/pkg/runtime"))
        with mock.patch.object(toolctl, "_windows_listening_pids", return_value=[7]), mock.patch.object(
            toolctl, "_windows_process_image", return_value="chrome.exe"
        ):
            with self.assertRaises(toolctl.ControlError):
                controller._stop_router_processes()

    def test_powershell_bridge_encodes_utf16_script(self):
        script = "Write-Output '启动 路由'"
        with mock.patch.object(toolctl.shutil, "which", return_value=None), mock.patch.object(
            toolctl.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")
        ) as run:
            toolctl._run_powershell(script)
        args = run.call_args.args[0]
        self.assertEqual(args[:2], ["powershell.exe", "-NoProfile"])
        self.assertEqual(args[-2], "-EncodedCommand")
        self.assertEqual(base64.b64decode(args[-1]).decode("utf-16-le"), script)

    def test_configure_stdio_replaces_missing_streams(self):
        with mock.patch.object(toolctl.sys, "stdout", None), mock.patch.object(toolctl.sys, "stderr", None):
            toolctl._configure_stdio()
            self.assertIsNotNone(toolctl.sys.stdout)
            self.assertIsNotNone(toolctl.sys.stderr)
            toolctl.sys.stdout.write("中文输出不报错\n")
            toolctl.sys.stderr.write("中文错误不报错\n")
            toolctl.sys.stdout.close()
            toolctl.sys.stderr.close()

    def test_codex_restart_judges_by_exit_code_not_text(self):
        # PowerShell 中文输出在不同代码页下会乱码，重启结果只能看退出码。
        with mock.patch.object(toolctl.subprocess, "run"), mock.patch.object(
            toolctl, "_run_powershell", return_value=mock.Mock(returncode=0, stdout="")
        ):
            with mock.patch.object(toolctl.sys, "stdout", io.StringIO()) as out:
                self.assertTrue(toolctl._restart_codex_windows())
            self.assertNotIn("未能自动重启", out.getvalue())
        with mock.patch.object(toolctl.subprocess, "run"), mock.patch.object(
            toolctl, "_run_powershell", return_value=mock.Mock(returncode=3, stdout="")
        ):
            with mock.patch.object(toolctl.sys, "stdout", io.StringIO()) as out:
                self.assertFalse(toolctl._restart_codex_windows())
            self.assertIn("未能自动重启", out.getvalue())

    def test_bundled_zstd_lands_next_to_the_router(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "app"
            source.mkdir()
            runtime = Path(tmp) / "runtime"
            runtime.mkdir()
            self.assertIsNone(toolctl._bundled_zstd(source))
            (source / "zstd").write_bytes(b"decoder")
            self.assertEqual(toolctl._bundled_zstd(source).name, "zstd")
            toolctl._install_bundled_zstd(source, runtime)
            target = runtime / ("zstd.exe" if toolctl.is_windows() else "zstd")
            self.assertEqual(target.read_bytes(), b"decoder")
            if not toolctl.is_windows():
                self.assertTrue(os.access(target, os.X_OK))
            with mock.patch.object(toolctl, "is_windows", return_value=True):
                toolctl._install_bundled_zstd(source, runtime)
            self.assertEqual((runtime / "zstd.exe").read_bytes(), b"decoder")


class WindowsConsoleHidingTests(unittest.TestCase):
    """Windows 上所有短命助手进程都必须隐藏控制台，否则桌面会闪黑框。"""

    def setUp(self):
        self.calls = []

    def fake_run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command[0] == "netstat":
            row = "  TCP    127.0.0.1:8790    0.0.0.0:0    LISTENING    4242\n"
            return subprocess.CompletedProcess(command, 0, row, "")
        if command[0] == "tasklist":
            return subprocess.CompletedProcess(command, 0, '"python.exe","4242","Console","1","1,234 K"\n', "")
        return subprocess.CompletedProcess(command, 0, "", "")

    def test_helper_commands_use_create_no_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "local-key.txt"
            key.write_text("sk-test", encoding="utf-8")
            controller = toolctl.WindowsController(Path(tmp) / "runtime")
            with mock.patch.object(toolctl, "is_windows", return_value=True), mock.patch.object(
                toolctl, "_port_listening", return_value=False
            ), mock.patch.object(toolctl.subprocess, "run", side_effect=self.fake_run), mock.patch.dict(
                os.environ, {"USERNAME": "tester"}
            ):
                toolctl._windows_listening_pids(8790)
                toolctl._windows_process_image(4242)
                toolctl._run_powershell("Get-Date")
                toolctl._restrict_key_acl(key)
                controller._stop_router_processes()

        names = [command[0] for command, _ in self.calls]
        for helper in ("netstat", "tasklist", "taskkill", "icacls"):
            self.assertIn(helper, names)
        for command, kwargs in self.calls:
            self.assertEqual(kwargs.get("creationflags"), 0x08000000, command)


if __name__ == "__main__":
    unittest.main()
