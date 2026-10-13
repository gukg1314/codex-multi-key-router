import copy
import json
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import keys_store
import router
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


def policy(*models, excluded=None, overrides=None):
    return {
        "version": 1,
        "upstream_base_url": toolctl.LOCAL_UPSTREAM,
        "models": list(models) or [local_model()],
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


def keys_payload(entries, priority=None):
    return keys_store.validate_keys({
        "version": 1,
        "keys": entries,
        "model_priority": {} if priority is None else priority,
    })


def key_entry(key_id, name, api_key, order=0, selected=None, enabled=True, upstream=None):
    entry = {
        "id": key_id,
        "name": name,
        "api_key": api_key,
        "enabled": enabled,
        "order": order,
        "selected_models": selected,
    }
    if upstream:
        entry["upstream_base_url"] = upstream
    return entry


def gateway_for(discovery, declared=None):
    """按密钥区分来源的假网关：/models 返回该 Key 的清单，探测返回工具调用。

    `declared` 按模型 ID 覆盖 supported_endpoint_types；默认只声明 openai（走 Chat 桥接）。
    /responses 返回 404：这个假上游没有直通端点，Chat 探测通过时也不会走到这里。
    """
    declared = declared or {}

    def gateway(_method, path, key, payload=None):
        if path == "/models":
            return 200, {"data": [
                {"id": model_id, "supported_endpoint_types": declared.get(model_id, ["openai"])}
                for model_id in discovery.get(key, [])
            ]}
        if path == "/responses":
            return 404, {"error": {"message": "responses endpoint unavailable"}}
        if payload["messages"][-1]["role"] == "tool":
            return 200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        return 200, {"choices": [{"message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_probe",
                "type": "function",
                "function": {"name": "codex_model_probe", "arguments": '{"input":"ok"}'},
            }],
        }}]}

    return gateway


DISCOVERY = {
    "sk-aaa": ["local/shared", "local/only-a"],
    "sk-bbb": ["local/shared", "local/only-b", "official-only"],
}


class MultiKeyResolveTests(unittest.TestCase):
    def resolve(self, priority=None):
        payload = keys_payload([
            key_entry("kaaa000001", "A", "sk-aaa", order=0),
            key_entry("kbbb000002", "B", "sk-bbb", order=1),
        ], priority)
        return toolctl.resolve_multi_key_manifest(
            policy(), payload, request_json_factory=lambda _base: gateway_for(DISCOVERY),
        )

    def test_explicit_priority_wins_and_official_targets_stay_official(self):
        manifest, report = self.resolve(priority={"local/shared": "kbbb000002", "official-only": "official"})
        by_slug = {model["slug"]: model for model in manifest["models"]}
        self.assertEqual(manifest["version"], 2)
        self.assertEqual(by_slug["local/shared"]["source"], "kbbb000002")
        self.assertEqual(by_slug["local/only-a"]["source"], "kaaa000001")
        self.assertEqual(by_slug["local/only-b"]["source"], "kbbb000002")
        self.assertNotIn("local/official-only", by_slug)
        self.assertIn("official-only", report["official"])
        self.assertEqual(report["included"], 3)

    def test_key_order_decides_when_priority_is_not_configured(self):
        manifest, report = self.resolve()
        by_slug = {model["slug"]: model for model in manifest["models"]}
        self.assertEqual(by_slug["local/shared"]["source"], "kaaa000001")
        conflict = next(item for item in report["conflicts"] if item["model"] == "local/shared")
        self.assertEqual(conflict["chosen"], "kaaa000001")
        self.assertEqual(conflict["candidates"], ["kaaa000001", "kbbb000002"])

    def test_promoted_cached_agent_model_reserves_priority_before_new_models(self):
        previous = previous_manifest([])
        previous["agent_models"] = [{
            **local_model("local/promoted-kaaa000001"),
            "upstream_model": "local/promoted",
            "priority": 53,
            "source": "kaaa000001",
        }]
        manifest, _report = toolctl.resolve_multi_key_manifest(
            policy(), one_key(), previous,
            request_json_factory=lambda _base: gateway_for({
                "sk-aaa": ["local/promoted", "local/new-model"],
            }),
        )

        priorities = {
            model.get("upstream_model", model["slug"]): model["priority"]
            for model in manifest["models"]
        }
        self.assertEqual(priorities["local/promoted"], 53)
        self.assertEqual(priorities["local/new-model"], 52)
        self.assertEqual(len(set(priorities.values())), len(priorities))

    def test_same_model_keeps_per_key_wire_for_the_global_owner_and_agent_candidate(self):
        model_id = "GPT-6 Luna"
        payload = keys_payload([
            key_entry("kaaa000001", "A", "sk-aaa", order=0, selected=[model_id], upstream="https://a.example.com/v1"),
            key_entry("kbbb000002", "B", "sk-bbb", order=1, selected=[model_id], upstream="https://b.example.com/v1"),
        ], {model_id: "kbbb000002"})
        calls = []

        def factory(base):
            key = "sk-aaa" if base == "https://a.example.com/v1" else "sk-bbb"

            def gateway(_method, path, _api_key, payload=None):
                calls.append((key, path))
                if path == "/models":
                    endpoints = ["openai", "openai-response"] if key == "sk-aaa" else ["openai"]
                    return 200, {"data": [{"id": model_id, "supported_endpoint_types": endpoints}]}
                if key == "sk-aaa":
                    self.assertEqual(path, "/responses")
                    return 200, {"status": "completed", "output": [
                        {"type": "custom_tool_call", "name": "codex_custom_probe", "input": "ok"},
                    ]}
                self.assertEqual(path, "/chat/completions")
                if payload["messages"][-1]["role"] == "tool":
                    return 200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
                return 200, {"choices": [{"message": {
                    "role": "assistant", "content": None,
                    "tool_calls": [{
                        "id": "call_probe", "type": "function",
                        "function": {"name": "codex_model_probe", "arguments": '{"input":"ok"}'},
                    }],
                }}]}

            return gateway

        manifest, report = toolctl.resolve_multi_key_manifest(policy(), payload, request_json_factory=factory)

        self.assertEqual(len(manifest["models"]), 1)
        self.assertEqual(manifest["models"][0]["source"], "kbbb000002")
        self.assertNotIn("wire_api", manifest["models"][0])
        self.assertEqual(len(manifest["agent_models"]), 1)
        self.assertEqual(manifest["agent_models"][0]["source"], "kaaa000001")
        self.assertEqual(manifest["agent_models"][0]["wire_api"], "responses")
        self.assertEqual(manifest["agent_models"][0]["upstream_model"], model_id)
        self.assertEqual(len({item["slug"] for item in manifest["models"] + manifest["agent_models"]}), 2)
        self.assertEqual(report["included"], 1)
        self.assertIn(("sk-aaa", "/responses"), calls)
        self.assertFalse(any(key == "sk-aaa" and path == "/chat/completions" for key, path in calls))
        self.assertIn(("sk-bbb", "/chat/completions"), calls)
        self.assertFalse(any(key == "sk-bbb" and path == "/responses" for key, path in calls))

    def test_official_priority_keeps_selected_key_model_hidden(self):
        model_id = "GPT-6 Luna"
        payload = keys_payload([
            key_entry("kaaa000001", "A", "sk-aaa", order=0, selected=[model_id]),
        ], {model_id: "official"})
        manifest, report = toolctl.resolve_multi_key_manifest(
            policy(), payload, request_json_factory=lambda _base: gateway_for({"sk-aaa": [model_id]}),
        )

        self.assertEqual(manifest["models"], [])
        self.assertEqual(len(manifest["agent_models"]), 1)
        self.assertEqual(manifest["agent_models"][0]["source"], "kaaa000001")
        self.assertEqual(report["keys"][0]["selected"], 1)
        self.assertEqual(report["keys"][0]["included"], 0)
        self.assertEqual(report["official"], [model_id])
        self.assertEqual(report["included"], 0)
        catalog = toolctl.merge_catalog({"models": [{"slug": "gpt-6-luna"}]}, manifest)
        self.assertEqual([model["slug"] for model in catalog["models"]], ["gpt-6-luna"])

    def test_same_identity_model_keeps_exactly_one_entry_and_switches_back_to_official(self):
        discovery = {"sk-aaa": ["GPT-5.6 Sol"]}
        official = {"models": [{"slug": "gpt-5.6-sol", "display_name": "GPT-5.6 Sol", "priority": 10}]}

        def resolve(priority=None):
            payload = keys_payload([key_entry("kaaa000001", "A", "sk-aaa", order=0)], priority)
            return toolctl.resolve_multi_key_manifest(
                policy(), payload, request_json_factory=lambda _base: gateway_for(discovery),
            )

        manifest, _report = resolve()
        local = manifest["models"][0]
        self.assertEqual(local["upstream_model"], "GPT-5.6 Sol")
        merged = toolctl.merge_catalog(official, manifest)
        self.assertEqual([model["slug"] for model in merged["models"]], [local["slug"]])

        manifest, report = resolve(priority={"GPT-5.6 Sol": "official"})
        self.assertEqual(manifest["models"], [])
        self.assertEqual(report["official"], ["GPT-5.6 Sol"])
        merged = toolctl.merge_catalog(official, manifest)
        self.assertEqual([model["slug"] for model in merged["models"]], ["gpt-5.6-sol"])

    def test_selected_models_limits_what_a_key_contributes(self):
        payload = keys_payload([
            key_entry("kaaa000001", "A", "sk-aaa", order=0, selected=[]),
            key_entry("kbbb000002", "B", "sk-bbb", order=1, selected=["local/only-b"]),
        ])
        manifest, report = toolctl.resolve_multi_key_manifest(
            policy(), payload, request_json_factory=lambda _base: gateway_for(DISCOVERY),
        )
        self.assertEqual([model["slug"] for model in manifest["models"]], ["local/only-b"])
        self.assertEqual([entry["selected"] for entry in report["keys"]], [0, 1])

    def test_disabled_keys_are_ignored(self):
        payload = keys_payload([
            key_entry("kaaa000001", "A", "sk-aaa", order=0, enabled=False),
            key_entry("kbbb000002", "B", "sk-bbb", order=1),
        ])
        manifest, _report = toolctl.resolve_multi_key_manifest(
            policy(), payload, request_json_factory=lambda _base: gateway_for(DISCOVERY),
        )
        self.assertEqual({model["source"] for model in manifest["models"]}, {"kbbb000002"})

    def test_every_key_failure_is_blocked_with_a_clear_error(self):
        payload = keys_payload([key_entry("kaaa000001", "A", "sk-aaa", order=0)])

        def broken(_method, path, _key, payload=None):
            raise toolctl.ControlError("无法连接 Local 网关：URLError")

        with self.assertRaises(toolctl.ControlError):
            toolctl.resolve_multi_key_manifest(
                policy(), payload, request_json_factory=lambda _base: broken,
            )

    def test_key_without_models_keeps_official_routing_alive(self):
        payload = keys_payload([
            key_entry("kaaa000001", "A", "sk-aaa", order=0, selected=["local/missing"]),
        ])
        manifest, report = toolctl.resolve_multi_key_manifest(
            policy(), payload, request_json_factory=lambda _base: gateway_for(DISCOVERY),
        )
        self.assertEqual(manifest["models"], [])
        self.assertEqual(report["included"], 0)
        self.assertEqual(manifest["sources"]["kaaa000001"]["kind"], "key")


def previous_manifest(models):
    return {
        "version": 2,
        "sources": {
            "official": {"kind": "official", "name": "ChatGPT 官方账号"},
            "kaaa000001": {
                "kind": "key", "name": "A",
                "upstream_base_url": "https://a.example.com/v1", "api_key": "sk-aaa",
            },
        },
        "models": models,
    }


def one_key():
    return keys_payload([key_entry(
        "kaaa000001", "A", "sk-aaa", order=0, upstream="https://a.example.com/v1",
    )])


class ResponsesWireTests(unittest.TestCase):
    """Key 模型按 /models 声明选接线：声明优先，Chat 探测不通时补一次 /responses 兜底。"""

    def test_declared_openai_response_model_uses_single_round_probe(self):
        calls = []

        def gateway(_method, path, _key, payload=None):
            calls.append(path)
            if path == "/models":
                return 200, {"data": [{
                    "id": "gpt-5.6-sol",
                    "supported_endpoint_types": ["openai", "openai-response"],
                }]}
            self.assertEqual(path, "/responses")
            self.assertEqual(payload["tool_choice"], "required")
            self.assertEqual(payload["reasoning"], {"effort": "max"})
            self.assertEqual([tool["type"] for tool in payload["tools"]], ["custom", "function"])
            self.assertEqual(payload["tools"][0]["name"], "codex_custom_probe")
            return 200, {"status": "completed", "output": [
                {"type": "custom_tool_call", "name": "codex_custom_probe", "input": "ok"},
            ]}

        manifest, _report = toolctl.resolve_multi_key_manifest(
            policy(), one_key(), request_json_factory=lambda _base: gateway,
        )
        model = manifest["models"][0]
        self.assertEqual(model["upstream_model"], "gpt-5.6-sol")
        self.assertEqual(model["wire_api"], "responses")
        self.assertEqual(model["default_reasoning_level"], "max")
        self.assertEqual(
            model["supported_reasoning_levels"],
            ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
        )
        self.assertEqual(calls, ["/models", "/responses"])

    def test_declared_chat_only_model_stays_on_chat_when_probe_passes(self):
        """声明只支持 openai 的模型：Chat 探测通过就走桥接，不额外打 /responses。"""
        calls = []

        def gateway(_method, path, _key, payload=None):
            calls.append(path)
            if path == "/models":
                return 200, {"data": [{
                    "id": "claude-opus-4-8",
                    "supported_endpoint_types": ["anthropic", "openai"],
                }]}
            if path == "/responses":
                raise AssertionError("Chat 探测已通过，不该再探测 /responses")
            if payload["messages"][-1]["role"] == "tool":
                return 200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
            return 200, {"choices": [{"message": {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "call_probe", "type": "function",
                    "function": {"name": "codex_model_probe", "arguments": '{"input":"ok"}'},
                }],
            }}]}

        manifest, _report = toolctl.resolve_multi_key_manifest(
            policy(), one_key(), request_json_factory=lambda _base: gateway,
        )
        self.assertNotIn("wire_api", manifest["models"][0])
        self.assertEqual(calls.count("/chat/completions"), 2)

    def test_declared_chat_only_model_falls_back_to_responses_when_chat_tools_fail(self):
        """只声明 openai 的模型在 Chat 上带不了工具时，应改用 /responses 直通接线。

        复现 gpt-6-astra：上游 /models 只声明 openai，Chat 明确拒绝带工具的 reasoning
        请求，但 /responses 能正常返回 custom_tool_call。
        """
        calls = []

        def gateway(_method, path, _key, payload=None):
            calls.append(path)
            if path == "/models":
                return 200, {"data": [{
                    "id": "gpt-6-astra",
                    "supported_endpoint_types": ["openai"],
                }]}
            if path == "/responses":
                self.assertEqual([tool["type"] for tool in payload["tools"]], ["custom", "function"])
                return 200, {"status": "completed", "output": [
                    {"type": "custom_tool_call", "name": "codex_custom_probe", "input": "ok"},
                ]}
            self.assertEqual(path, "/chat/completions")
            return 400, {"error": {"message": (
                "Function tools with reasoning_effort are not supported for gpt-6-astra "
                "in /v1/chat/completions. To use function tools, use /v1/responses "
                "or set reasoning_effort to 'none'."
            )}}

        manifest, _report = toolctl.resolve_multi_key_manifest(
            policy(), one_key(), request_json_factory=lambda _base: gateway,
        )
        model = manifest["models"][0]
        self.assertEqual(model["upstream_model"], "gpt-6-astra")
        self.assertEqual(model["wire_api"], "responses")
        self.assertEqual(model["default_reasoning_level"], "max")
        self.assertEqual(calls[0], "/models")
        self.assertIn("/chat/completions", calls)
        self.assertEqual(calls[-1], "/responses")

    def test_missing_endpoint_declaration_falls_back_to_chat_probe(self):
        calls = []

        def gateway(_method, path, _key, payload=None):
            calls.append(path)
            if path == "/models":
                return 200, {"data": [{"id": "mystery-model"}]}
            if path == "/responses":
                return 404, {"error": {"message": "responses endpoint unavailable"}}
            if payload["messages"][-1]["role"] == "tool":
                return 200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
            return 200, {"choices": [{"message": {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "call_probe", "type": "function",
                    "function": {"name": "codex_model_probe", "arguments": '{"input":"ok"}'},
                }],
            }}]}

        manifest, _report = toolctl.resolve_multi_key_manifest(
            policy(), one_key(), request_json_factory=lambda _base: gateway,
        )
        self.assertNotIn("wire_api", manifest["models"][0])
        self.assertEqual(calls, ["/models", "/responses", "/chat/completions", "/chat/completions"])

    def test_cached_model_without_wire_marker_is_upgraded_to_responses(self):
        cached_sol = local_model("local/gpt-5.6-sol-419255f2")
        cached_sol.update({
            "upstream_model": "gpt-5.6-sol",
            "source": "kaaa000001",
            "default_reasoning_level": "none",
            "supported_reasoning_levels": ["none"],
        })

        def gateway(_method, path, _key, payload=None):
            if path == "/models":
                return 200, {"data": [{
                    "id": "gpt-5.6-sol",
                    "supported_endpoint_types": ["openai", "openai-response"],
                }]}
            self.assertEqual(path, "/responses")
            return 200, {"status": "completed", "output": [
                {"type": "custom_tool_call", "name": "codex_custom_probe", "input": "ok"},
            ]}

        manifest, _report = toolctl.resolve_multi_key_manifest(
            policy(), one_key(), previous_manifest([cached_sol]), request_json_factory=lambda _base: gateway,
        )
        model = manifest["models"][0]
        self.assertEqual(model["slug"], "local/gpt-5.6-sol-419255f2")
        self.assertEqual(model["wire_api"], "responses")
        self.assertEqual(model["default_reasoning_level"], "max")
        self.assertEqual(model["source"], "kaaa000001")

    def test_cached_chat_only_astra_upgrades_when_chat_tools_fail(self):
        cached = local_model("local/gpt-6-astra-576a599a")
        cached.update({"upstream_model": "gpt-6-astra", "source": "kaaa000001"})
        calls = []

        def gateway(_method, path, _key, payload=None):
            calls.append(path)
            if path == "/models":
                return 200, {"data": [{"id": "gpt-6-astra", "supported_endpoint_types": ["openai"]}]}
            if path == "/chat/completions":
                return 400, {"error": {"message": "Use /responses for tools"}}
            self.assertEqual(payload["tools"][0]["type"], "custom")
            return 200, {"status": "completed", "output": [
                {"type": "custom_tool_call", "name": "codex_custom_probe", "input": "ok"},
            ]}

        manifest, _report = toolctl.resolve_multi_key_manifest(
            policy(), one_key(), previous_manifest([cached]), request_json_factory=lambda _base: gateway,
        )
        self.assertEqual(manifest["models"][0]["wire_api"], "responses")
        self.assertEqual(manifest["models"][0]["default_reasoning_level"], "max")
        self.assertEqual(calls[0], "/models")
        self.assertIn("/chat/completions", calls)
        self.assertEqual(calls[-1], "/responses")

    def test_cached_chat_only_record_keeps_wire_on_temporary_probe_error(self):
        cached = local_model("local/gpt-6-astra-576a599a")
        cached.update({"upstream_model": "gpt-6-astra", "source": "kaaa000001"})
        calls = []

        def gateway(_method, path, _key, _payload=None):
            calls.append(path)
            if path == "/models":
                return 200, {"data": [{"id": "gpt-6-astra", "supported_endpoint_types": ["openai"]}]}
            return 429, {"error": {"message": "rate limited"}}

        manifest, _report = toolctl.resolve_multi_key_manifest(
            policy(), one_key(), previous_manifest([cached]), request_json_factory=lambda _base: gateway,
        )
        self.assertNotIn("wire_api", manifest["models"][0])
        self.assertEqual(calls, ["/models", "/chat/completions"])

    def test_failed_upgrade_keeps_cached_model_untouched(self):
        cached_sol = local_model("local/gpt-5.6-sol-419255f2")
        cached_sol.update({
            "upstream_model": "gpt-5.6-sol",
            "source": "kaaa000001",
            "default_reasoning_level": "none",
            "supported_reasoning_levels": ["none"],
        })

        def gateway(_method, path, _key, _payload=None):
            if path == "/models":
                return 200, {"data": [{
                    "id": "gpt-5.6-sol",
                    "supported_endpoint_types": ["openai", "openai-response"],
                }]}
            return 500, {"error": {"message": "upstream busy"}}

        manifest, _report = toolctl.resolve_multi_key_manifest(
            policy(), one_key(), previous_manifest([cached_sol]), request_json_factory=lambda _base: gateway,
        )
        model = manifest["models"][0]
        self.assertNotIn("wire_api", model)
        self.assertEqual(model["supported_reasoning_levels"], ["none"])

    def test_responses_probe_rejects_function_syntax_and_falls_back_to_chat(self):
        calls = []

        def gateway(_method, path, _key, payload=None):
            calls.append(path)
            if path == "/models":
                return 200, {"data": [{
                    "id": "deepseek-v4-pro",
                    "supported_endpoint_types": ["openai", "openai-response"],
                }]}
            if path == "/responses":
                return 200, {"status": "completed", "output": [
                    {"type": "function_call", "name": "codex_custom_probe", "arguments": '{"input":"ok"}'},
                ]}
            if payload["messages"][-1]["role"] == "tool":
                return 200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
            return 200, {"choices": [{"message": {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "call_probe", "type": "function",
                    "function": {"name": "codex_model_probe", "arguments": '{"input":"ok"}'},
                }],
            }}]}

        manifest, _report = toolctl.resolve_multi_key_manifest(
            policy(), one_key(), request_json_factory=lambda _base: gateway,
        )
        model = manifest["models"][0]
        self.assertNotIn("wire_api", model)
        self.assertEqual(model["default_reasoning_level"], "max")
        self.assertEqual(calls, ["/models", "/responses", "/chat/completions", "/chat/completions"])

    def test_cached_responses_model_falls_back_to_chat_when_syntax_rejected(self):
        cached = local_model("local/deepseek-v4-pro")
        cached.update({
            "upstream_model": "deepseek-v4-pro",
            "source": "kaaa000001",
            "wire_api": "responses",
            "default_reasoning_level": "none",
            "supported_reasoning_levels": ["none"],
        })

        def gateway(_method, path, _key, payload=None):
            if path == "/models":
                return 200, {"data": [{
                    "id": "deepseek-v4-pro",
                    "supported_endpoint_types": ["openai", "openai-response"],
                }]}
            if path == "/responses":
                return 200, {"status": "completed", "output": [
                    {"type": "function_call", "name": "codex_custom_probe", "arguments": '{"input":"ok"}'},
                ]}
            if payload["messages"][-1]["role"] == "tool":
                return 200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
            return 200, {"choices": [{"message": {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "call_probe", "type": "function",
                    "function": {"name": "codex_model_probe", "arguments": '{"input":"ok"}'},
                }],
            }}]}

        manifest, _report = toolctl.resolve_multi_key_manifest(
            policy(), one_key(), previous_manifest([cached]), request_json_factory=lambda _base: gateway,
        )
        model = manifest["models"][0]
        self.assertNotIn("wire_api", model)
        self.assertEqual(model["default_reasoning_level"], "max")

    def test_responses_probe_unavailable_keeps_cached_responses_marker(self):
        cached = local_model("local/deepseek-v4-pro")
        cached.update({
            "upstream_model": "deepseek-v4-pro",
            "source": "kaaa000001",
            "wire_api": "responses",
            "default_reasoning_level": "none",
            "supported_reasoning_levels": ["none"],
        })

        def gateway(_method, path, _key, _payload=None):
            if path == "/models":
                return 200, {"data": [{
                    "id": "deepseek-v4-pro",
                    "supported_endpoint_types": ["openai", "openai-response"],
                }]}
            return 429, {"error": {"message": "rate limited"}}

        manifest, _report = toolctl.resolve_multi_key_manifest(
            policy(), one_key(), previous_manifest([cached]), request_json_factory=lambda _base: gateway,
        )
        model = manifest["models"][0]
        self.assertEqual(model["wire_api"], "responses")
        self.assertEqual(model["supported_reasoning_levels"], ["none"])


class ConcurrentDiscoveryTests(unittest.TestCase):
    """多 Key 拉取必须并发，且限流时只回退受影响的 Key。"""

    def keys(self):
        return keys_payload([
            key_entry("kaaa000001", "A", "sk-aaa", order=0),
            key_entry("kbbb000002", "B", "sk-bbb", order=1),
            key_entry("kccc000003", "C", "sk-ccc", order=2),
        ])

    def two_keys(self):
        return keys_payload([
            key_entry("kaaa000001", "A", "sk-aaa", order=0),
            key_entry("kbbb000002", "B", "sk-bbb", order=1),
        ])

    def test_three_keys_are_fetched_concurrently_and_keep_key_order(self):
        gateway = gateway_for({"sk-aaa": ["local/a"], "sk-bbb": ["local/b"], "sk-ccc": ["local/c"]})

        def factory(_base):
            def slow(method, path, key, payload=None):
                if path == "/models":
                    time.sleep(0.15)
                return gateway(method, path, key, payload)
            return slow

        started = time.monotonic()
        manifest, report = toolctl.resolve_multi_key_manifest(
            policy(), self.keys(), request_json_factory=factory,
        )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.40, f"三个 Key 应该并发拉取，实际耗时 {elapsed:.2f}s")
        self.assertEqual([entry["id"] for entry in report["keys"]], ["kaaa000001", "kbbb000002", "kccc000003"])
        self.assertEqual({model["source"] for model in manifest["models"]}, {"kaaa000001", "kbbb000002", "kccc000003"})

    def test_rate_limited_key_is_retried_serially_and_other_keys_are_untouched(self):
        gateway = gateway_for(DISCOVERY)
        attempts = {"sk-aaa": 0}

        def factory(_base):
            def flaky(method, path, key, payload=None):
                if path == "/models" and key == "sk-aaa":
                    attempts["sk-aaa"] += 1
                    if attempts["sk-aaa"] == 1:
                        return 429, {"error": {"message": "rate limited"}}
                return gateway(method, path, key, payload)
            return flaky

        with mock.patch.object(toolctl, "DISCOVERY_RETRY_BACKOFF_SECONDS", 0):
            manifest, report = toolctl.resolve_multi_key_manifest(
                policy(), self.two_keys(), request_json_factory=factory,
            )
        self.assertEqual(attempts["sk-aaa"], 2)
        self.assertEqual([entry["error"] for entry in report["keys"]], [None, None])
        self.assertEqual({model["source"] for model in manifest["models"]}, {"kaaa000001", "kbbb000002"})

    def test_auth_failure_is_not_retried(self):
        attempts = {"sk-aaa": 0}

        def factory(_base):
            def unauthorized(_method, path, key, payload=None):
                if path == "/models":
                    attempts[key] = attempts.get(key, 0) + 1
                    return 401, {"error": {"message": "bad key"}}
                raise AssertionError("探测不应该被触发")
            return unauthorized

        with mock.patch.object(toolctl, "DISCOVERY_RETRY_BACKOFF_SECONDS", 0):
            with self.assertRaises(toolctl.ControlError):
                toolctl.resolve_multi_key_manifest(policy(), self.two_keys(), request_json_factory=factory)
        self.assertEqual(attempts, {"sk-aaa": 1, "sk-bbb": 1})


class ManifestValidationTests(unittest.TestCase):
    def test_version_two_manifest_requires_key_sources(self):
        model = {**local_model("local/only-a"), "source": "kaaa000001"}
        toolctl.validate_manifest({
            "version": 2,
            "sources": {"kaaa000001": {"kind": "key", "name": "A", "upstream_base_url": "https://a.example.com/v1", "api_key": "sk-aaa"}},
            "models": [model],
        })
        with self.assertRaises(toolctl.ValidationError):
            toolctl.validate_manifest({
                "version": 2,
                "sources": {"kaaa000001": {"kind": "key", "name": "A", "upstream_base_url": "https://a.example.com/v1"}},
                "models": [model],
            })
        with self.assertRaises(toolctl.ValidationError):
            toolctl.validate_manifest({"version": 2, "sources": {"official": {"kind": "official"}}, "models": [model]})

    def test_version_two_manifest_allows_an_empty_model_list(self):
        toolctl.validate_manifest({"version": 2, "sources": {"official": {"kind": "official"}}, "models": []})
        with self.assertRaises(toolctl.ValidationError):
            toolctl.validate_manifest({"version": 1, "upstream_base_url": toolctl.LOCAL_UPSTREAM, "models": []})

    def test_agent_models_reject_bad_source_duplicate_slug_and_empty_upstream(self):
        key_source = {
            "kind": "key", "name": "A",
            "upstream_base_url": "https://a.example.com/v1", "api_key": "sk-aaa",
        }
        base = {
            "version": 2,
            "sources": {"official": {"kind": "official"}, "kaaa000001": key_source},
            "models": [{**local_model("local/visible"), "source": "kaaa000001"}],
            "agent_models": [{
                **local_model("local/hidden"), "upstream_model": "gpt-hidden", "source": "kaaa000001",
            }],
        }
        toolctl.validate_manifest(base)

        broken = []
        wrong_source = copy.deepcopy(base)
        wrong_source["agent_models"][0]["source"] = "missing"
        broken.append(wrong_source)
        duplicate_slug = copy.deepcopy(base)
        duplicate_slug["agent_models"][0]["slug"] = "local/visible"
        broken.append(duplicate_slug)
        empty_upstream = copy.deepcopy(base)
        empty_upstream["agent_models"][0]["upstream_model"] = ""
        broken.append(empty_upstream)
        for value in broken:
            with self.subTest(agent_model=value["agent_models"][0]):
                with self.assertRaises(toolctl.ValidationError):
                    toolctl.validate_manifest(value)

    def test_legacy_single_key_file_is_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = toolctl.paths(root)
            p["runtime"].mkdir(parents=True)
            toolctl.write_local_key(p["key"], "legacy-secret")
            data = toolctl.load_runtime_keys(p)
            self.assertEqual(len(data["keys"]), 1)
            self.assertEqual(data["keys"][0]["api_key"], "legacy-secret")
            self.assertEqual(data["keys"][0]["upstream_base_url"], toolctl.LOCAL_UPSTREAM)
            self.assertTrue(p["keys"].is_file())
            self.assertEqual(toolctl.load_runtime_keys(p), data)


class MultiSourceRouteTests(unittest.TestCase):
    def manifest(self):
        return {
            "version": 2,
            "sources": {
                "official": {"kind": "official", "name": "ChatGPT 官方账号"},
                "kaaa000001": {
                    "kind": "key", "name": "A",
                    "upstream_base_url": "https://a.example.com/v1", "api_key": "sk-aaa",
                },
                "kbbb000002": {
                    "kind": "key", "name": "B",
                    "upstream_base_url": "https://b.example.com/v1", "api_key": "sk-bbb",
                },
            },
            "models": [
                {**local_model("local/alpha"), "source": "kaaa000001"},
                {**local_model("local/beta"), "source": "kbbb000002"},
            ],
        }

    def route(self, manifest, model, local_key=None):
        captured = {}

        def transport(method, url, headers, body, stream):
            captured.update(method=method, url=url, headers=headers, payload=json.loads(body), stream=stream)
            return router.UpstreamResponse(200, {}, json.dumps({
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            }).encode())

        body = json.dumps({
            "model": model, "stream": False,
            "input": [{"type": "message", "role": "user", "content": "hello"}],
        }).encode()
        response = router.route_request(
            router.PreparedRequest("POST", "/v1/responses", {"Content-Type": "application/json"}, body),
            manifest, local_key, transport,
        )
        return response, captured

    def test_manifest_round_trip_keeps_every_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "local-models.json"
            path.write_text(json.dumps(self.manifest()), encoding="utf-8")
            loaded = router.load_manifest(path)
        self.assertEqual(loaded["sources"]["kbbb000002"]["api_key"], "sk-bbb")
        health = router.health_payload(loaded, None)
        self.assertEqual(health["source_count"], 2)
        self.assertTrue(health["local_key_present"])

    def test_each_model_uses_its_own_key_and_upstream(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "local-models.json"
            path.write_text(json.dumps(self.manifest()), encoding="utf-8")
            loaded = router.load_manifest(path)
        _response, captured = self.route(loaded, "local/beta")
        self.assertEqual(captured["url"], "https://b.example.com/v1/chat/completions")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer sk-bbb")
        _response, captured = self.route(loaded, "local/alpha")
        self.assertEqual(captured["url"], "https://a.example.com/v1/chat/completions")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer sk-aaa")

    def test_missing_source_is_reported_instead_of_routing_blind(self):
        manifest = self.manifest()
        manifest["sources"].pop("kbbb000002")
        response, captured = self.route(manifest, "local/beta")
        self.assertEqual(response.status, 503)
        self.assertEqual(json.loads(response.body)["error"]["code"], "source_unavailable")
        self.assertEqual(captured, {})

    def test_empty_source_key_is_reported(self):
        manifest = copy.deepcopy(self.manifest())
        manifest["sources"]["kbbb000002"]["api_key"] = ""
        response, _captured = self.route(manifest, "local/beta")
        self.assertEqual(response.status, 503)
        self.assertEqual(json.loads(response.body)["error"]["code"], "key_missing")

    def test_official_models_still_proxy_to_the_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "local-models.json"
            path.write_text(json.dumps(self.manifest()), encoding="utf-8")
            loaded = router.load_manifest(path)
        captured = {}

        def transport(method, url, headers, body, stream):
            captured.update(method=method, url=url, headers=headers, body=body, stream=stream)
            return router.UpstreamResponse(200, {}, b"{}")

        body = json.dumps({"model": "gpt-5.1-codex", "stream": True, "input": []}).encode()
        response = router.route_request(
            router.PreparedRequest("POST", "/v1/responses", {"Authorization": "Bearer account-token"}, body),
            loaded, None, transport,
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(captured["url"], router.OFFICIAL_BASE_URL + "/responses")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer account-token")


if __name__ == "__main__":
    unittest.main()
