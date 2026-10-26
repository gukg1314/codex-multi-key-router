import base64
import copy
import gzip
import http.client
import io
import json
import re
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from unittest import mock

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import router


MANIFEST = {
    "version": 1,
    "upstream_base_url": "https://api.example.com/v1",
    "models": [
        {
            "slug": "local/kimi-k3",
            "display_name": "local/kimi-k3",
            "description": "Kimi",
            "priority": 54,
            "context_window": 200000,
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": ["low", "medium", "high", "xhigh", "max"],
            "input_modalities": ["text", "image"],
            "supports_image_detail_original": True,
            "chat_options": {
                "temperature": 1.0,
                "top_p": 0.95,
                "explicit_tool_choice": "required_single",
            },
        },
        {
            "slug": "local/deepseek-v4.1-flash",
            "display_name": "local/deepseek-v4.1-flash",
            "description": "DeepSeek",
            "priority": 53,
            "context_window": 200000,
            "default_reasoning_level": "xhigh",
            "supported_reasoning_levels": ["low", "medium", "high", "xhigh", "max"],
            "input_modalities": ["text", "image"],
            "supports_image_detail_original": True,
            "chat_options": {"explicit_tool_choice": "native"},
        },
    ],
}

FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "codex_custom_tool_roundtrip.json").read_text(encoding="utf-8"))


def sse_frame(value):
    if value == "[DONE]":
        return b"data: [DONE]\n\n"
    return ("data: " + json.dumps(value, separators=(",", ":")) + "\n\n").encode()


def response_events(payload: bytes):
    return [json.loads(line[6:]) for line in payload.decode().splitlines() if line.startswith("data: ")]


class RouterTests(unittest.TestCase):
    def test_stream_transport_forwards_terminal_before_truncated_http_end(self):
        frame = sse_frame({"type": "response.completed", "response": {"status": "completed"}})
        wire = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + f"{len(frame):x}\r\n".encode() + frame + b"\r\n"
        sock = mock.Mock()
        sock.makefile.return_value = io.BytesIO(wire)
        response = http.client.HTTPResponse(sock)
        response.begin()
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(router.http.client, "HTTPConnection", return_value=connection):
            output = router.default_transport("POST", "http://upstream.test/responses", {}, b"{}", True)
            self.assertEqual(next(output.body), frame)
            with self.assertRaises(http.client.IncompleteRead):
                next(output.body)
        connection.close.assert_called_once()

    def test_stream_transport_preserves_clean_chunked_bytes(self):
        frames = [sse_frame({"type": "response.created"}), sse_frame({"type": "response.completed"})]
        wire = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        wire += b"".join(f"{len(frame):x}\r\n".encode() + frame + b"\r\n" for frame in frames) + b"0\r\n\r\n"
        sock = mock.Mock()
        sock.makefile.return_value = io.BytesIO(wire)
        response = http.client.HTTPResponse(sock)
        response.begin()
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(router.http.client, "HTTPConnection", return_value=connection):
            output = router.default_transport("POST", "http://upstream.test/responses", {}, b"{}", True)
            self.assertEqual(b"".join(output.body), b"".join(frames))
        connection.close.assert_called_once()

    def test_gateway_alias_uses_original_upstream_model_id(self):
        record = {
            **copy.deepcopy(MANIFEST["models"][1]),
            "slug": "local/qwen-chat-1234abcd",
            "display_name": "local/Qwen-Chat",
            "upstream_model": "local/Qwen-Chat",
        }
        carrier = router._pack_reasoning(record["slug"], {"reasoning_content": "saved state"})
        chat = router.build_chat_request({
            "model": record["slug"], "stream": False,
            "input": [
                {"type": "reasoning", "encrypted_content": carrier, "summary": []},
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hello"}]},
            ],
        }, record)

        self.assertEqual(chat["model"], "local/Qwen-Chat")
        assistant = next(message for message in chat["messages"] if message["role"] == "assistant")
        self.assertEqual(assistant["reasoning_content"], "saved state")

    def test_auto_manifest_model_uses_local_upstream(self):
        manifest = copy.deepcopy(MANIFEST)
        manifest["models"].append({**copy.deepcopy(MANIFEST["models"][1]), "slug": "auto", "display_name": "auto"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "local-models.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            loaded = router.load_manifest(path)

        captured = {}

        def transport(method, url, headers, body, stream):
            captured.update(method=method, url=url, payload=json.loads(body), stream=stream)
            return router.UpstreamResponse(200, {}, json.dumps({
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            }).encode())

        body = json.dumps({
            "model": "auto", "stream": False,
            "input": [{"type": "message", "role": "user", "content": "hello"}],
        }).encode()
        response = router.route_request(
            router.PreparedRequest("POST", "/v1/responses", {"Content-Type": "application/json"}, body),
            loaded, "local-secret", transport,
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(captured["url"], "https://api.example.com/v1/chat/completions")
        self.assertEqual(captured["payload"]["model"], "auto")

    def test_official_proxy_preserves_path_body_auth_and_compression(self):
        raw = json.dumps({"model": "gpt-official", "input": "hello"}).encode()
        compressed = gzip.compress(raw)
        captured = {}

        def transport(method, url, headers, body, stream):
            captured.update(method=method, url=url, headers=headers, body=body, stream=stream)
            return router.UpstreamResponse(200, {"content-type": "text/event-stream"}, [b"data: ok\n\n"])

        result = router.route_request(
            router.PreparedRequest(
                "POST", "/v1/responses?foo=1",
                {"Authorization": "Bearer official", "Content-Encoding": "gzip"}, compressed,
            ),
            MANIFEST, None, transport,
        )
        self.assertEqual(result.status, 200)
        self.assertEqual(captured["url"], "https://chatgpt.com/backend-api/codex/responses?foo=1")
        self.assertEqual(captured["body"], compressed)
        self.assertEqual(captured["headers"]["Authorization"], "Bearer official")
        self.assertEqual(list(result.body), [b"data: ok\n\n"])

        with mock.patch.object(router, "_stdlib_zstd", None), mock.patch.object(
            router, "_third_party_zstd", None
        ), mock.patch.object(router.shutil, "which", return_value="/test/zstd"), mock.patch.object(
            router.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=raw)
        ) as run:
            zstd_result = router.route_request(
                router.PreparedRequest("POST", "/v1/responses", {"Content-Encoding": "zstd"}, b"zstd"),
                MANIFEST, None, transport,
            )
        self.assertEqual(zstd_result.status, 200)
        self.assertEqual(captured["body"], b"zstd")
        self.assertEqual(run.call_args.kwargs["input"], b"zstd")

        # Windows 上 zstd 助手必须隐藏控制台：pythonw 下的路由进程否则会闪黑框。
        with mock.patch.object(router, "_stdlib_zstd", None), mock.patch.object(
            router, "_third_party_zstd", None
        ), mock.patch.object(router.shutil, "which", return_value="/test/zstd"), mock.patch.object(
            router, "_is_windows", return_value=True
        ), mock.patch.object(
            router.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=raw)
        ) as hidden_run:
            router._decompress_zstd(b"zstd")
        self.assertEqual(hidden_run.call_args.kwargs["creationflags"], 0x08000000)

    def test_official_passthrough_logs_account_usage_without_secrets(self):
        raw = json.dumps({"model": "gpt-5.6-luna", "input": "hello"}).encode()

        def transport(_method, _url, _headers, _body, _stream):
            return router.UpstreamResponse(200, {"content-type": "text/event-stream"}, [b"data: ok\n\n"])

        with self.assertLogs(router.log, level="INFO") as records:
            response = router.route_request(
                router.PreparedRequest(
                    "POST", "/v1/responses",
                    {
                        "Authorization": "Bearer personal-secret",
                        "x-codex-turn-metadata": json.dumps({"thread_source": "desktop"}),
                        "x-codex-client-version": "1.2.3",
                    },
                    raw, "rid-official-1",
                ),
                MANIFEST, None, transport,
            )
        list(response.body)
        logs = "\n".join(records.output)
        self.assertIn("official account usage rid=rid-official-1", logs)
        self.assertIn("model=gpt-5.6-luna", logs)
        self.assertIn("target=chatgpt.com", logs)
        self.assertIn("credential=authorization", logs)
        self.assertIn("x-codex-turn-metadata", logs)
        self.assertIn("thread_source=desktop", logs)
        self.assertIn(f"body_bytes={len(raw)}", logs)
        self.assertNotIn("personal-secret", logs)

    def test_official_priority_models_reads_official_mark_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keys.json"
            path.write_text(json.dumps({
                "version": 1,
                "keys": [],
                "model_priority": {
                    "gpt-5.6-luna": "official",
                    "local/kimi-k3": "k1234567890",
                    "gpt-5.6-sol": "official",
                },
            }), encoding="utf-8")
            self.assertEqual(router.official_priority_models(path), ["gpt-5.6-luna", "gpt-5.6-sol"])
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(router.official_priority_models(path), [])
            self.assertEqual(router.official_priority_models(Path(tmp) / "missing.json"), [])

    def test_real_additional_tools_build_registry_and_isolate_auth(self):
        body = copy.deepcopy(FIXTURE["first_turn"])
        body.update(model="local/kimi-k3", max_output_tokens=123, reasoning={"effort": "high"})
        body["tool_choice"] = {"type": "custom", "name": "exec"}
        registry = router.ToolRegistry.from_body(body)
        self.assertEqual(registry.metrics(), {
            "additional_groups": 1, "namespaces": 1, "functions": 1, "custom": 1,
            "history_only": 0, "total": 2,
        })
        chat = router.build_chat_request(body, MANIFEST["models"][0], registry)
        self.assertEqual(chat["tool_choice"], "required")
        self.assertEqual([tool["function"]["name"] for tool in chat["tools"]], ["exec"])
        self.assertEqual(chat["tools"][0]["function"]["parameters"]["required"], ["input"])
        self.assertEqual((chat["temperature"], chat["top_p"]), (1.0, 0.95))
        self.assertEqual(chat["max_tokens"], 123)
        self.assertEqual(chat["reasoning_effort"], "high")
        self.assertEqual(chat["messages"][-2], {
            "role": "system", "content": router.LOCAL_REASONING_LANGUAGE_INSTRUCTION,
        })
        self.assertEqual(chat["messages"][-1]["role"], "user")

        captured = {}

        def transport(_method, url, headers, payload, stream):
            captured.update(url=url, headers=headers, body=json.loads(payload), stream=stream)
            return router.UpstreamResponse(200, {}, [
                sse_frame({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}),
                sse_frame("[DONE]"),
            ])

        headers = {
            "content-type": "application/json",
            "Authorization": "Bearer official-secret",
            "Cookie": "session=private",
            "ChatGPT-Account-Id": "acct",
            "OpenAI-Organization": "org",
            "X-Safe": "must-not-forward",
        }
        response = router.route_request(
            router.PreparedRequest("POST", "/v1/responses", headers, json.dumps(body).encode()),
            MANIFEST, "local-secret", transport,
        )
        list(response.body)
        self.assertEqual(captured["url"], "https://api.example.com/v1/chat/completions")
        self.assertEqual(captured["headers"], {"Content-Type": "application/json", "Authorization": "Bearer local-secret"})
        self.assertNotIn("official-secret", repr(captured))

    def test_top_level_function_allowed_choice_and_image_tool_output(self):
        body = {
            "model": "local/deepseek-v4.1-flash",
            "stream": True,
            "parallel_tool_calls": False,
            "tools": [{
                "type": "function", "name": "inspect_image", "description": "Inspect an image",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            }],
            "tool_choice": {
                "type": "allowed_tools", "mode": "required",
                "tools": [{"type": "function", "name": "inspect_image"}],
            },
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "inspect"}]},
                {"type": "function_call", "call_id": "call_image", "name": "inspect_image", "arguments": "{\"path\":\"/tmp/a.png\"}"},
                {"type": "function_call_output", "call_id": "call_image", "output": [
                    {"type": "input_text", "text": "image result"},
                    {"type": "input_image", "image_url": "data:image/png;base64,YQ=="},
                ]},
            ],
        }
        registry = router.ToolRegistry.from_body(body)
        chat = router.build_chat_request(body, MANIFEST["models"][1], registry)
        self.assertEqual(chat["tool_choice"], "required")
        self.assertFalse(chat["parallel_tool_calls"])
        self.assertEqual([tool["function"]["name"] for tool in chat["tools"]], ["inspect_image"])
        self.assertEqual(chat["messages"][-1]["role"], "tool")
        self.assertEqual(chat["messages"][-1]["content"][-1]["type"], "image_url")

        same_name = copy.deepcopy(FIXTURE["first_turn"])
        same_name["tools"] = [{"type": "function", "name": "exec", "parameters": {}}]
        aliases = router.ToolRegistry.from_body(same_name)
        self.assertEqual(len({spec.upstream_name for spec in aliases.specs}), 3)
        names = [aliases.original(kind, "exec").upstream_name for kind in ("function", "custom")]
        self.assertIn("exec", names)
        self.assertEqual(sum(bool(re.fullmatch(r"exec_[0-9a-f]{10}", name)) for name in names), 1)
        repeated = router.ToolRegistry.from_body(same_name)
        self.assertEqual(
            [spec.upstream_name for spec in aliases.specs],
            [spec.upstream_name for spec in repeated.specs],
        )

        duplicate_namespaces = {
            "tools": [
                {"type": "namespace", "name": namespace, "description": namespace, "tools": [
                    {"type": "function", "name": "lookup", "parameters": {"type": "object"}},
                ]}
                for namespace in ("alpha", "beta")
            ],
        }
        namespaced = router.ToolRegistry.from_body(duplicate_namespaces)
        self.assertEqual(len({spec.upstream_name for spec in namespaced.specs}), 2)
        self.assertNotEqual(
            namespaced.original("function", "lookup", "alpha").upstream_name,
            namespaced.original("function", "lookup", "beta").upstream_name,
        )
        with self.assertRaises(router.ProtocolError) as ambiguous:
            namespaced.original("function", "lookup")
        self.assertEqual(ambiguous.exception.code, "ambiguous_tool_name")
        namespaced_history = {
            **duplicate_namespaces,
            "model": "local/deepseek-v4.1-flash",
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "lookup"}]},
                {"type": "function_call", "call_id": "call_ns", "namespace": "beta", "name": "lookup", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call_ns", "namespace": "beta", "name": "lookup", "output": "ok"},
            ],
        }
        namespaced_chat = router.build_chat_request(namespaced_history, MANIFEST["models"][1])
        self.assertEqual(
            namespaced_chat["messages"][-2]["tool_calls"][0]["function"]["name"],
            namespaced.original("function", "lookup", "beta").upstream_name,
        )

        conflict = copy.deepcopy(same_name)
        conflict["tools"].append({
            "type": "function", "name": "exec",
            "parameters": {"type": "object", "properties": {"value": {"type": "string"}}},
        })
        with self.assertRaises(router.ProtocolError) as error:
            router.ToolRegistry.from_body(conflict)
        self.assertEqual(error.exception.code, "duplicate_tool_name")

        grammar_conflict = copy.deepcopy(FIXTURE["first_turn"])
        changed_custom = copy.deepcopy(grammar_conflict["input"][0]["tools"][0])
        changed_custom["tools"][0]["format"] = {
            "type": "grammar", "syntax": "lark", "definition": 'start: "changed"',
        }
        grammar_conflict["input"].append({"type": "additional_tools", "tools": [changed_custom]})
        with self.assertRaises(router.ProtocolError) as error:
            router.ToolRegistry.from_body(grammar_conflict)
        self.assertEqual(error.exception.code, "duplicate_tool_name")

    def test_description_only_duplicate_tools_keep_aliases_and_route_history(self):
        grammar = {
            "type": "grammar", "syntax": "lark",
            "definition": "start: SOURCE\nSOURCE: /[\\s\\S]+/",
        }
        parameters = {
            "type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"],
        }
        first_tools = [
            {"type": "namespace", "name": "functions", "description": "Functions", "tools": [{
                "type": "custom", "name": "exec", "description": "Old custom description", "format": grammar,
            }]},
            {"type": "namespace", "name": "mcp__cua_repl", "description": "REPL", "tools": [{
                "type": "function", "name": "js", "description": "Old function description", "parameters": parameters,
            }]},
        ]
        updated_tools = copy.deepcopy(first_tools)
        updated_tools[0]["tools"][0]["description"] = "Latest custom description SYNTHETIC_PRIVATE_DESCRIPTION_MARKER"
        updated_tools[1]["tools"][0]["description"] = "Latest function description SYNTHETIC_PRIVATE_DESCRIPTION_MARKER"
        body = {
            "model": "local/deepseek-v4.1-flash", "stream": True, "tools": first_tools,
            "input": [
                {"type": "additional_tools", "tools": updated_tools},
                {"type": "custom_tool_call", "call_id": "old_exec", "name": "exec", "namespace": "functions", "input": "pwd"},
                {"type": "custom_tool_call_output", "call_id": "old_exec", "name": "exec", "namespace": "functions", "output": "/tmp"},
                {"type": "function_call", "call_id": "old_js", "name": "js", "namespace": "mcp__cua_repl", "arguments": "{\"code\":\"1+1\"}"},
                {"type": "function_call_output", "call_id": "old_js", "name": "js", "namespace": "mcp__cua_repl", "output": "2"},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Continue."}]},
            ],
        }
        first_registry = router.ToolRegistry.from_body({"tools": first_tools})
        registry = router.ToolRegistry.from_body(body)
        custom = registry.original("custom", "exec", "functions")
        function = registry.original("function", "js", "mcp__cua_repl")

        self.assertEqual(len(registry.specs), 2)
        self.assertEqual(len(registry.by_original), 2)
        self.assertEqual(len(registry.by_upstream), 2)
        for spec in (custom, function):
            identity = (spec.kind, spec.namespace, spec.name)
            self.assertIs(registry.by_original[identity], spec)
            self.assertIs(registry.by_upstream[spec.upstream_name], spec)
            self.assertEqual(sum(item is spec for item in registry.specs), 1)
            self.assertEqual(
                first_registry.original(spec.kind, spec.name, ".".join(spec.namespace)).upstream_name,
                spec.upstream_name,
            )
        self.assertIn("Latest custom description", custom.description)
        self.assertNotIn("Old custom description", custom.description)
        self.assertIn("Latest function description", function.description)
        self.assertNotIn("Old function description", function.description)

        captured = {}

        def transport(_method, url, _headers, payload, stream):
            captured.update(url=url, payload=json.loads(payload), stream=stream)
            return router.UpstreamResponse(200, {}, [
                sse_frame({"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "new_exec", "type": "function", "function": {
                        "name": custom.upstream_name, "arguments": "{\"input\":\"echo done\"}",
                    }},
                    {"index": 1, "id": "new_js", "type": "function", "function": {
                        "name": function.upstream_name, "arguments": "{\"code\":\"done()\"}",
                    }},
                ]}}]}),
                sse_frame({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
                sse_frame("[DONE]"),
            ])

        with self.assertLogs(router.log, level="INFO") as records:
            response = router.route_request(
                router.PreparedRequest("POST", "/v1/responses", {}, json.dumps(body).encode()),
                MANIFEST, "test-key", transport,
            )
        self.assertEqual(response.status, 200)
        self.assertNotIn("SYNTHETIC_PRIVATE_DESCRIPTION_MARKER", "\n".join(records.output))
        self.assertEqual(captured["url"], "https://api.example.com/v1/chat/completions")
        self.assertTrue(captured["stream"])
        sent_tools = {tool["function"]["name"]: tool["function"] for tool in captured["payload"]["tools"]}
        self.assertEqual(set(sent_tools), {custom.upstream_name, function.upstream_name})
        self.assertIn("Latest custom description", sent_tools[custom.upstream_name]["description"])
        self.assertIn("Latest function description", sent_tools[function.upstream_name]["description"])
        messages = captured["payload"]["messages"]
        history_calls = [call for message in messages for call in message.get("tool_calls", [])]
        self.assertEqual(
            [call["function"]["name"] for call in history_calls],
            [custom.upstream_name, function.upstream_name],
        )
        self.assertEqual([message["tool_call_id"] for message in messages if message["role"] == "tool"], ["old_exec", "old_js"])
        events = response_events(b"".join(response.body))
        self.assertEqual(events[-1]["type"], "response.completed")
        output = events[-1]["response"]["output"]
        self.assertEqual(
            [(item["type"], item["name"], item.get("namespace")) for item in output],
            [("custom_tool_call", "exec", "functions"), ("function_call", "js", "mcp__cua_repl")],
        )
        self.assertEqual(output[0]["input"], "echo done")
        self.assertEqual(output[1]["arguments"], "{\"code\":\"done()\"}")

    def test_stream_maps_fragmented_custom_and_function_calls(self):
        body = FIXTURE["first_turn"]
        registry = router.ToolRegistry.from_body(body)
        custom_arguments = json.dumps({"input": "text('ok')"}, separators=(",", ":"))
        function_arguments = json.dumps({"path": "/tmp/a.png"}, separators=(",", ":"))
        chunks = [
            sse_frame({"choices": [{"delta": {"reasoning_content": "check"}}]}),
            sse_frame({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_exec", "function": {"name": "exec", "arguments": custom_arguments[:9]}},
                {"index": 1, "id": "call_image", "function": {"name": "view_image", "arguments": function_arguments[:8]}},
            ]}}]}),
            sse_frame({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": custom_arguments[9:]}},
                {"index": 1, "function": {"arguments": function_arguments[8:]}},
            ]}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 2, "completion_tokens": 3}}),
            sse_frame("[DONE]"),
        ]
        payload = b"".join(router.chat_stream_to_responses(chunks, body["model"], registry))
        events = response_events(payload)
        event_types = [event["type"] for event in events]
        self.assertIn("response.custom_tool_call_input.delta", event_types)
        self.assertIn("response.custom_tool_call_input.done", event_types)
        self.assertIn("response.function_call_arguments.delta", event_types)
        self.assertIn("response.function_call_arguments.done", event_types)
        self.assertEqual([event["sequence_number"] for event in events], list(range(len(events))))
        completed = events[-1]
        self.assertEqual(completed["type"], "response.completed")
        calls = [item for item in completed["response"]["output"] if item["type"].endswith("tool_call") or item["type"] == "function_call"]
        self.assertEqual([item["type"] for item in calls], ["custom_tool_call", "function_call"])
        self.assertEqual(calls[0]["input"], "text('ok')")
        self.assertEqual(calls[0]["call_id"], "call_exec")
        self.assertEqual([item["namespace"] for item in calls], ["functions", "functions"])
        self.assertEqual(calls[1]["arguments"], function_arguments)

    def test_full_context_custom_tool_output_roundtrip(self):
        body = copy.deepcopy(FIXTURE["second_turn"])
        registry = router.ToolRegistry.from_body(body)
        chat = router.build_chat_request(body, MANIFEST["models"][1], registry)
        self.assertEqual([message["role"] for message in chat["messages"]][-3:], ["user", "assistant", "tool"])
        assistant = chat["messages"][-2]
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "exec")
        wrapped = json.loads(assistant["tool_calls"][0]["function"]["arguments"])
        self.assertIn("tools.exec_command", wrapped["input"])
        self.assertEqual(chat["messages"][-1]["tool_call_id"], "call_fixture")
        self.assertIn("/Users/example/project", chat["messages"][-1]["content"])

        captured = []

        def transport(_method, _url, headers, payload, _stream):
            captured.append({"headers": headers, "body": json.loads(payload)})
            return router.UpstreamResponse(200, {}, [
                sse_frame({"choices": [{"delta": {"content": "Directory confirmed."}, "finish_reason": "stop"}]}),
                sse_frame("[DONE]"),
            ])

        with self.assertLogs(router.log, level="INFO") as records:
            response = router.route_request(
                router.PreparedRequest("POST", "/v1/responses", {}, json.dumps(body).encode()),
                MANIFEST, "private-local-key", transport,
            )
            payload = b"".join(response.body)
        self.assertEqual(response_events(payload)[-1]["type"], "response.completed")
        logs = "\n".join(records.output)
        self.assertIn("custom=1", logs)
        self.assertIn("tool_output_history=True", logs)
        self.assertNotIn("private-local-key", logs)
        self.assertNotIn("Directory confirmed", logs)

    def test_local_chat_skips_web_search_and_keeps_function_custom_tools(self):
        body = {
            "model": "local/deepseek-v4.1-flash",
            "stream": False,
            "tools": [
                {"type": "web_search"},
                {"type": "namespace", "name": "net", "tools": [
                    {"type": "web_search"},
                    {"type": "function", "name": "lookup", "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}},
                ]},
            ],
            "tool_choice": {"type": "allowed_tools", "mode": "required", "tools": [
                {"type": "web_search"},
                {"type": "function", "name": "lookup", "namespace": "net"},
                {"type": "custom", "name": "exec"},
            ]},
            "input": [
                {"type": "additional_tools", "role": "developer", "tools": [
                    {"type": "web_search"},
                    {"type": "custom", "name": "exec"},
                ]},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "look up and run"}]},
            ],
        }
        captured = {}

        def transport(method, url, headers, payload, stream):
            captured.update(method=method, url=url, payload=json.loads(payload), stream=stream)
            return router.UpstreamResponse(200, {}, json.dumps({
                "choices": [{
                    "message": {"tool_calls": [
                        {"id": "call_lookup", "type": "function", "function": {"name": "lookup", "arguments": "{\"q\":\"x\"}"}},
                        {"id": "call_exec", "type": "function", "function": {"name": "exec", "arguments": "{\"input\":\"pwd\"}"}},
                    ]},
                    "finish_reason": "tool_calls",
                }],
            }).encode())

        with self.assertLogs(router.log, level="INFO") as records:
            response = router.route_request(
                router.PreparedRequest("POST", "/v1/responses", {}, json.dumps(body).encode()),
                MANIFEST, "key", transport,
            )

        self.assertEqual(response.status, 200)
        self.assertEqual(captured["url"], "https://api.example.com/v1/chat/completions")
        self.assertFalse(captured["stream"])
        sent = captured["payload"]
        self.assertEqual([tool["function"]["name"] for tool in sent["tools"]], ["lookup", "exec"])
        self.assertEqual(sent["tool_choice"], "required")
        output = json.loads(response.body)["output"]
        self.assertEqual([item["type"] for item in output], ["function_call", "custom_tool_call"])
        self.assertEqual((output[0]["name"], output[0]["namespace"], output[0]["arguments"]), ("lookup", "net", "{\"q\":\"x\"}"))
        self.assertEqual(output[0]["call_id"], "call_lookup")
        self.assertEqual(json.loads(output[0]["arguments"]), {"q": "x"})
        self.assertEqual((output[1]["name"], output[1]["input"]), ("exec", "pwd"))
        self.assertEqual(output[1]["call_id"], "call_exec")
        logs = "\n".join(records.output)
        self.assertIn("web_search_skipped=3", logs)
        self.assertIn("web_search_history_skipped=0", logs)
        self.assertIn("web_search_choices_skipped=1", logs)

    def test_web_search_tool_choices_are_dropped_without_widening_allowed_tools(self):
        base = {
            "model": "local/kimi-k3",
            "tools": [
                {"type": "web_search"},
                {"type": "function", "name": "lookup", "parameters": {"type": "object", "properties": {}}},
                {"type": "custom", "name": "exec"},
            ],
            "input": "hello",
        }
        cases = [
            ("forced_search", {"type": "web_search"}, ["lookup", "exec"], None),
            ("mixed_allowed", {"type": "allowed_tools", "mode": "required", "tools": [
                {"type": "web_search"}, {"type": "function", "name": "lookup"},
            ]}, ["lookup"], "required"),
            ("search_only_allowed", {"type": "allowed_tools", "mode": "auto", "tools": [
                {"type": "web_search"},
            ]}, [], None),
        ]
        for name, choice, expected_names, expected_choice in cases:
            with self.subTest(name=name):
                body = {**copy.deepcopy(base), "tool_choice": choice}
                chat = router.build_chat_request(body, MANIFEST["models"][0])
                self.assertEqual(
                    [tool["function"]["name"] for tool in chat.get("tools", [])],
                    expected_names,
                )
                if expected_choice is None:
                    self.assertNotIn("tool_choice", chat)
                else:
                    self.assertEqual(chat["tool_choice"], expected_choice)

        search_required = {"model": "local/kimi-k3", "tools": [{"type": "web_search"}], "tool_choice": "required", "input": "hello"}
        self.assertNotIn("tools", router.build_chat_request(search_required, MANIFEST["models"][0]))
        for invalid_choice in (
            "required",
            {"type": "allowed_tools", "mode": "auto", "tools": []},
            {"type": "function", "name": "missing"},
        ):
            with self.subTest(invalid_choice=invalid_choice):
                invalid = {"model": "local/kimi-k3", "tool_choice": invalid_choice, "input": "hello"}
                with self.assertRaises(router.ProtocolError):
                    router.build_chat_request(invalid, MANIFEST["models"][0])

    def test_web_search_history_is_skipped_while_function_outputs_stay_paired(self):
        body = {
            "model": "local/kimi-k3",
            "stream": True,
            "tools": [
                {"type": "function", "name": "lookup", "parameters": {"type": "object", "properties": {}}},
                {"type": "custom", "name": "exec"},
            ],
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "search then use tools"}]},
                {"type": "function_call", "call_id": "call_lookup", "name": "lookup", "arguments": "{}"},
                {"type": "web_search_call", "id": "ws_1", "status": "completed"},
                {"type": "function_call_output", "call_id": "call_lookup", "output": "found"},
                {"type": "custom_tool_call", "call_id": "call_exec", "name": "exec", "input": "pwd"},
                {"type": "custom_tool_call_output", "call_id": "call_exec", "output": "/tmp"},
            ],
        }
        captured = {}

        def transport(_method, _url, _headers, payload, _stream):
            captured.update(json.loads(payload))
            return router.UpstreamResponse(200, {}, [
                sse_frame({"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}),
                sse_frame("[DONE]"),
            ])

        with self.assertLogs(router.log, level="INFO") as records:
            response = router.route_request(
                router.PreparedRequest("POST", "/v1/responses", {}, json.dumps(body).encode()),
                MANIFEST, "key", transport,
            )
            events = response_events(b"".join(response.body))

        self.assertEqual(response.status, 200)
        messages = captured["messages"]
        self.assertEqual([message["role"] for message in messages][-4:], ["assistant", "tool", "assistant", "tool"])
        self.assertEqual([message["tool_call_id"] for message in messages if message["role"] == "tool"], ["call_lookup", "call_exec"])
        self.assertEqual(messages[-4]["tool_calls"][0]["function"]["arguments"], "{}")
        self.assertEqual(messages[-3]["content"], "found")
        self.assertEqual(json.loads(messages[-2]["tool_calls"][0]["function"]["arguments"]), {"input": "pwd"})
        self.assertEqual(messages[-1]["content"], "/tmp")
        self.assertEqual(events[-1]["type"], "response.completed")
        self.assertIn("web_search_history_skipped=1", "\n".join(records.output))

    def test_responses_wire_keeps_web_search_definitions_choice_and_history(self):
        payload = {
            "stream": False,
            "tools": [
                {"type": "web_search", "search_context_size": "medium"},
                {"type": "namespace", "name": "search", "tools": [{"type": "web_search"}]},
            ],
            "tool_choice": {"type": "web_search"},
            "input": [
                {"type": "additional_tools", "role": "developer", "tools": [{"type": "web_search"}]},
                {"type": "web_search_call", "id": "ws_1", "status": "completed"},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "search"}]},
            ],
        }
        captured = {}

        def transport(method, url, headers, body, stream):
            captured.update(method=method, url=url, payload=json.loads(body), stream=stream)
            return router.UpstreamResponse(200, {"content-type": "application/json"}, b'{"id":"resp_web"}')

        response = router.route_request(
            self.responses_request(payload), self.responses_manifest(), "local-secret", transport,
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(captured["url"], "https://api.example.com/v1/responses")
        self.assertEqual(captured["payload"], {"model": "gpt-5.6-sol", **payload})
        self.assertEqual(response.body, b'{"id":"resp_web"}')

    def test_compaction_replays_custom_history_without_registering_tool(self):
        body = copy.deepcopy(FIXTURE["second_turn"])
        body["input"] = [item for item in body["input"] if item.get("type") != "additional_tools"]
        registry = router.ToolRegistry.from_body(body)
        chat = router.build_chat_request(body, MANIFEST["models"][1], registry)

        self.assertNotIn("tools", chat)
        self.assertEqual(registry.metrics()["history_only"], 1)
        self.assertEqual(chat["messages"][-2]["tool_calls"][0]["function"]["name"], "exec")
        self.assertEqual(chat["messages"][-1]["tool_call_id"], "call_fixture")
        with self.assertRaisesRegex(router.ProtocolError, "Local 模型返回了未注册的工具调用"):
            registry.decode_call("exec", json.dumps({"input": "pwd"}))

        captured = {}

        def transport(_method, _url, _headers, payload, _stream):
            captured.update(json.loads(payload))
            return router.UpstreamResponse(200, {}, [
                sse_frame({"choices": [{"delta": {"content": "summary"}, "finish_reason": "stop"}]}),
                sse_frame("[DONE]"),
            ])

        with self.assertLogs(router.log, level="INFO") as records:
            response = router.route_request(
                router.PreparedRequest("POST", "/v1/responses", {}, json.dumps(body).encode()),
                MANIFEST, "key", transport,
            )
            payload = b"".join(response.body)
        self.assertEqual(response.status, 200)
        self.assertNotIn("tools", captured)
        self.assertEqual(response_events(payload)[-1]["type"], "response.completed")
        self.assertIn("history_only=1", "\n".join(records.output))

    def test_invalid_tool_calls_degrade_to_text_and_never_fail_the_turn(self):
        registry = router.ToolRegistry.from_body(FIXTURE["first_turn"])
        cases = {
            "unknown": {"name": "missing", "arguments": "{}"},
            "bad-custom": {"name": "exec", "arguments": "{}"},
            "alias-suffix": {"name": "missing_0f1e2d3c4b", "arguments": "{}"},
        }
        for name, case in cases.items():
            with self.subTest(name=name):
                chunks = [
                    sse_frame({"choices": [{"delta": {"tool_calls": [{
                        "index": 0, "id": "call_bad", "function": {
                            "name": case["name"], "arguments": case["arguments"],
                        },
                    }]}, "finish_reason": "tool_calls"}]}),
                    sse_frame("[DONE]"),
                ]
                with self.assertLogs(router.log, level="WARNING") as records:
                    events = response_events(b"".join(
                        router.chat_stream_to_responses(chunks, FIXTURE["first_turn"]["model"], registry)
                    ))
                self.assertEqual(events[-1]["type"], "response.completed")
                self.assertNotIn("response.failed", [event["type"] for event in events])
                logs = "\n".join(records.output)
                self.assertIn("downgraded tool call", logs)
                self.assertNotIn("arguments=" + case["arguments"], logs)
                item = events[-1]["response"]["output"][-1]
                self.assertEqual(item["type"], "message")
                text = item["content"][0]["text"]
                self.assertIn(FIXTURE["first_turn"]["model"], text)
                self.assertIn(case["name"], text)

    def test_downgraded_tool_log_records_length_without_arguments(self):
        registry = router.ToolRegistry.from_body(FIXTURE["first_turn"])
        arguments = "x" * 250
        chunks = [
            sse_frame({"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "call_bad",
                "function": {"name": "exec", "arguments": arguments},
            }]}, "finish_reason": "tool_calls"}]}),
            sse_frame("[DONE]"),
        ]
        with self.assertLogs(router.log, level="WARNING") as records:
            response_events(b"".join(
                router.chat_stream_to_responses(chunks, FIXTURE["first_turn"]["model"], registry)
            ))
        logs = "\n".join(records.output)
        self.assertIn("arguments_chars=250", logs)
        self.assertNotIn("arguments=", logs)
        self.assertNotIn("x" * 10, logs)

    def test_chat_upstream_error_log_excludes_request_and_response_body(self):
        body = copy.deepcopy(FIXTURE["second_turn"])
        error_body = b'{"error":{"message":"overloaded","code":"server_error"}}'
        for stream in (False, True):
            with self.subTest(stream=stream):
                payload = iter([error_body]) if stream else error_body
                transport = mock.Mock(return_value=router.UpstreamResponse(
                    500, {"content-type": "application/json"}, payload,
                ))
                request = router.PreparedRequest(
                    "POST", "/v1/responses", {},
                    json.dumps({**body, "stream": stream}).encode(),
                )
                with self.assertLogs(router.log, level="WARNING") as records:
                    response = router.route_request(request, MANIFEST, "key", transport)
                self.assertEqual(response.status, 500)
                logs = "\n".join(records.output)
                self.assertIn("local upstream rejected", logs)
                self.assertIn("status=500", logs)
                self.assertIn("payload_bytes=", logs)
                self.assertNotIn("overloaded", logs)
                self.assertNotIn("Run pwd", logs)
                self.assertNotIn("/Users/example/project", logs)

    def test_misnamed_tool_call_is_recovered_by_normalized_name(self):
        registry = router.ToolRegistry.from_body(FIXTURE["first_turn"])
        chunks = [
            sse_frame({"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "call_fix",
                "function": {"name": "View.Image", "arguments": json.dumps({"path": "a.png"})},
            }]}, "finish_reason": "tool_calls"}]}),
            sse_frame("[DONE]"),
        ]
        events = response_events(b"".join(
            router.chat_stream_to_responses(chunks, FIXTURE["first_turn"]["model"], registry)
        ))
        self.assertEqual(events[-1]["type"], "response.completed")
        item = events[-1]["response"]["output"][-1]
        self.assertEqual(item["type"], "function_call")
        self.assertEqual(item["name"], "view_image")
        self.assertEqual(json.loads(item["arguments"]), {"path": "a.png"})

    def test_ambiguous_or_unknown_names_are_never_guessed(self):
        registry = router.ToolRegistry.from_body({"tools": [
            {"type": "function", "name": "my tool!", "parameters": {"type": "object", "properties": {}}},
        ]})
        alias = registry.specs[0].upstream_name
        self.assertNotEqual(alias, "my tool!")
        self.assertIs(registry.recover_call(alias.upper()), registry.specs[0])
        registry._walk(
            {"type": "function", "name": "my-tool?", "parameters": {"type": "object", "properties": {}}}, (), (),
        )
        self.assertIsNone(registry.recover_call("mytool"))

    def test_image_parts_are_replaced_when_model_disables_image_input(self):
        body = copy.deepcopy(FIXTURE["first_turn"])
        body["input"].append({"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "look"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
        ]})
        blocked = router.build_chat_request(body, {**copy.deepcopy(MANIFEST["models"][1]), "input_modalities": ["text"]})
        encoded = json.dumps(blocked, ensure_ascii=False)
        self.assertIn(router.IMAGE_OMITTED_TEXT, encoded)
        self.assertNotIn("image_url", encoded)
        allowed = json.dumps(router.build_chat_request(body, MANIFEST["models"][1]), ensure_ascii=False)
        self.assertIn("image_url", allowed)

    def test_invalid_request_bodies_are_rejected(self):
        invalid_bodies = []
        unmatched = copy.deepcopy(FIXTURE["first_turn"])
        unmatched["input"].append({"type": "custom_tool_call_output", "call_id": "missing", "output": "x"})
        invalid_bodies.append((unmatched, "unmatched_tool_output"))
        unsupported = copy.deepcopy(FIXTURE["first_turn"])
        unsupported["input"][0]["tools"][0]["tools"].append({"type": "computer_use_preview"})
        invalid_bodies.append((unsupported, "unsupported_tool_type"))
        invalid_history = copy.deepcopy(FIXTURE["second_turn"])
        invalid_history["input"][2]["name"] = ""
        invalid_bodies.append((invalid_history, "invalid_tool_history"))
        unsupported_history = copy.deepcopy(FIXTURE["first_turn"])
        unsupported_history["input"].append({"type": "computer_call", "id": "cu_1"})
        invalid_bodies.append((unsupported_history, "unsupported_input_type"))
        wrong_namespace = copy.deepcopy(FIXTURE["second_turn"])
        wrong_namespace["input"][2]["namespace"] = "wrong"
        wrong_namespace["input"][3]["namespace"] = "different"
        invalid_bodies.append((wrong_namespace, "unmatched_tool_output"))
        incomplete_parallel = copy.deepcopy(FIXTURE["first_turn"])
        incomplete_parallel["input"].append({
            "type": "custom_tool_call", "call_id": "call_pending", "name": "exec", "input": "text('x')",
        })
        invalid_bodies.append((incomplete_parallel, "invalid_tool_history"))
        for body, code in invalid_bodies:
            with self.subTest(code=code):
                response = router.route_request(
                    router.PreparedRequest("POST", "/v1/responses", {}, json.dumps(body).encode()),
                    MANIFEST, "key", mock.Mock(),
                )
                self.assertEqual(response.status, 400)
                self.assertEqual(json.loads(response.body)["error"]["code"], code)

    def test_reasoning_only_retries_once_but_not_after_tool_output(self):
        first_turn = copy.deepcopy(FIXTURE["first_turn"])
        registry = router.ToolRegistry.from_body(first_turn)
        exec_name = registry.original("custom", "exec").upstream_name
        wrapper = json.dumps({"input": "text('done')"}, separators=(",", ":"))
        responses = [
            [sse_frame({"choices": [{"delta": {"reasoning_content": "plan"}, "finish_reason": "stop"}]}), sse_frame("[DONE]")],
            [sse_frame({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call1", "function": {"name": exec_name, "arguments": wrapper}}]}, "finish_reason": "tool_calls"}]}), sse_frame("[DONE]")],
        ]
        requests = []

        def transport(_method, _url, _headers, payload, _stream):
            requests.append(json.loads(payload))
            return router.UpstreamResponse(200, {}, responses.pop(0))

        result = router.route_request(
            router.PreparedRequest("POST", "/v1/responses", {}, json.dumps(first_turn).encode()),
            MANIFEST, "key", transport,
        )
        events = response_events(b"".join(result.body))
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1]["messages"][-1]["content"], router.RECOVERY_MESSAGE)
        self.assertEqual(events[-1]["type"], "response.completed")
        self.assertEqual(events[-1]["response"]["output"][-1]["type"], "custom_tool_call")

        second_turn = copy.deepcopy(FIXTURE["second_turn"])
        calls = []

        def no_retry_transport(*args):
            calls.append(args)
            return router.UpstreamResponse(200, {}, [
                sse_frame({"choices": [{"delta": {"reasoning_content": "plan only"}, "finish_reason": "stop"}]}),
                sse_frame("[DONE]"),
            ])

        result = router.route_request(
            router.PreparedRequest("POST", "/v1/responses", {}, json.dumps(second_turn).encode()),
            MANIFEST, "key", no_retry_transport,
        )
        events = response_events(b"".join(result.body))
        self.assertEqual(len(calls), 1)
        self.assertEqual(events[-1]["type"], "response.failed")

    def test_nonstream_custom_call_and_reasoning_state(self):
        body = copy.deepcopy(FIXTURE["first_turn"])
        body["stream"] = False
        registry = router.ToolRegistry.from_body(body)
        wrapper = json.dumps({"input": "text('ok')"}, separators=(",", ":"))
        upstream = json.dumps({
            "choices": [{"message": {
                "reasoning_content": "checked",
                "reasoning_details": [{"id": "r1"}],
                "tool_calls": [{"id": "call1", "type": "function", "function": {"name": "exec", "arguments": wrapper}}],
            }, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4},
        }).encode()
        result = json.loads(router.chat_response_to_responses(upstream, body["model"], registry))
        self.assertEqual(result["status"], "completed")
        self.assertEqual([item["type"] for item in result["output"]], ["reasoning", "custom_tool_call"])
        self.assertEqual(result["output"][1]["input"], "text('ok')")
        restored = router._unpack_reasoning(result["output"][0]["encrypted_content"], body["model"])
        self.assertEqual(restored["reasoning_details"], [{"id": "r1"}])

    def test_nonstream_preserves_calls_to_different_functions_without_indexes(self):
        body = {
            "model": "local/deepseek-v4.1-flash",
            "tools": [
                {"type": "function", "name": "lookup", "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}},
                {"type": "function", "name": "fetch", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}}},
            ],
            "input": "find and fetch",
        }
        registry = router.ToolRegistry.from_body(body)
        calls = [
            {"id": "call_lookup", "type": "function", "function": {"name": "lookup", "arguments": "{\"q\":\"x\"}"}},
            {"id": "call_fetch", "type": "function", "function": {"name": "fetch", "arguments": "{\"url\":\"https://example.test\"}"}},
        ]
        upstream = json.dumps({"choices": [{"message": {"tool_calls": calls}, "finish_reason": "tool_calls"}]}).encode()

        result = json.loads(router.chat_response_to_responses(upstream, body["model"], registry))
        output = result["output"]
        self.assertEqual([item["type"] for item in output], ["function_call", "function_call"])
        self.assertEqual([item["name"] for item in output], ["lookup", "fetch"])
        self.assertEqual([item["call_id"] for item in output], ["call_lookup", "call_fetch"])
        self.assertEqual([item["arguments"] for item in output], [call["function"]["arguments"] for call in calls])
        self.assertEqual([json.loads(item["arguments"]) for item in output], [{"q": "x"}, {"url": "https://example.test"}])

    def test_nonstream_keeps_two_calls_to_same_function_independent(self):
        body = {
            "model": "local/deepseek-v4.1-flash",
            "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}],
            "input": "find two things",
        }
        registry = router.ToolRegistry.from_body(body)
        calls = [
            {"id": "call_first", "type": "function", "function": {"name": "lookup", "arguments": "{\"q\":\"first\"}"}},
            {"id": "call_second", "type": "function", "function": {"name": "lookup", "arguments": "{\"q\":\"second\"}"}},
        ]
        upstream = json.dumps({"choices": [{"message": {"tool_calls": calls}, "finish_reason": "tool_calls"}]}).encode()

        result = json.loads(router.chat_response_to_responses(upstream, body["model"], registry))
        output = result["output"]
        self.assertEqual([item["type"] for item in output], ["function_call", "function_call"])
        self.assertEqual([item["name"] for item in output], ["lookup", "lookup"])
        self.assertEqual([item["call_id"] for item in output], ["call_first", "call_second"])
        self.assertEqual([item["arguments"] for item in output], [call["function"]["arguments"] for call in calls])
        self.assertEqual([json.loads(item["arguments"]) for item in output], [{"q": "first"}, {"q": "second"}])

    def responses_manifest(self):
        record = {
            **copy.deepcopy(MANIFEST["models"][0]),
            "slug": "local/gpt-5.6-sol",
            "upstream_model": "gpt-5.6-sol",
            "wire_api": "responses",
        }
        return {**copy.deepcopy(MANIFEST), "models": [record]}

    def responses_request(self, payload, headers=None):
        return router.PreparedRequest(
            "POST", "/v1/responses",
            {
                "Authorization": "Bearer local-secret",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "session_id": "abc",
                **(headers or {}),
            },
            json.dumps({"model": "local/gpt-5.6-sol", **payload}).encode(),
        )

    def test_responses_wire_model_passes_through_with_model_rewrite_only(self):
        captured = {}

        def transport(method, url, headers, payload, stream):
            captured.update(method=method, url=url, headers=headers, payload=json.loads(payload), stream=stream)
            return router.UpstreamResponse(200, {"content-type": "application/json"}, b'{"id":"resp_1"}')

        response = router.route_request(
            self.responses_request({"stream": False, "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            ]}),
            self.responses_manifest(), "local-secret", transport,
        )

        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["url"], "https://api.example.com/v1/responses")
        self.assertEqual(captured["payload"]["model"], "gpt-5.6-sol")
        self.assertEqual(captured["payload"]["input"][0]["content"][0]["text"], "hi")
        self.assertFalse(captured["stream"])
        self.assertEqual(captured["headers"]["Authorization"], "Bearer local-secret")
        self.assertEqual(captured["headers"]["Content-Type"], "application/json")
        self.assertNotIn("session_id", captured["headers"])
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b'{"id":"resp_1"}')

    def test_responses_wire_model_keeps_prewarm_and_previous_id_guards(self):
        transport = mock.Mock()
        prewarm = router.route_request(
            self.responses_request({"generate": False}), self.responses_manifest(), "local-secret", transport,
        )
        bare_previous = router.route_request(
            self.responses_request({"previous_response_id": "resp_old"}), self.responses_manifest(), "local-secret", transport,
        )
        self.assertEqual((prewarm.status, bare_previous.status), (400, 400))
        self.assertEqual(json.loads(prewarm.body)["error"]["code"], "unsupported_prewarm")
        self.assertEqual(json.loads(bare_previous.body)["error"]["code"], "previous_response_not_found")
        transport.assert_not_called()

    def test_responses_wire_streams_chunks_and_forwards_upstream_errors(self):
        chunks = iter([b'data: {"type":"response.completed"}\n\n', b"data: [DONE]\n\n"])
        transport = mock.Mock(return_value=router.UpstreamResponse(
            200, {"content-type": "text/event-stream"}, chunks, "OK",
        ))
        response = router.route_request(
            self.responses_request({"stream": True}), self.responses_manifest(), "local-secret", transport,
        )
        self.assertIs(response.body, chunks)
        self.assertTrue(transport.call_args.args[4])

        error_body = b'{"error":{"message":"slow down","code":"rate_limit_exceeded"}}'
        rejected = router.route_request(
            self.responses_request({"stream": False}), self.responses_manifest(), "local-secret",
            mock.Mock(return_value=router.UpstreamResponse(429, {"content-type": "application/json"}, error_body)),
        )
        self.assertEqual(rejected.status, 429)
        self.assertEqual(rejected.body, error_body)

    def test_responses_imagegen_alias_preserves_tool_call_and_history(self):
        image_tool = {"type": "namespace", "name": "image_gen", "tools": [{
            "type": "function", "name": "imagegen", "description": "Generate an image",
            "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]},
        }]}
        payload = {
            "stream": False,
            "client_metadata": {"turn_id": "01a0c84e-3036-7e91-96b1-74bb1cd34b04"},
            "tools": [{"type": "tool_search"}, {"type": "function", "name": "deferred", "defer_loading": True}],
            "tool_choice": {"type": "allowed_tools", "mode": "required", "tools": [
                {"type": "function", "name": "imagegen", "namespace": "image_gen"},
            ]},
            "input": [
                {"type": "additional_tools", "role": "developer", "tools": [image_tool]},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "draw"}]},
            ],
        }
        captured = []

        def transport(_method, _url, _headers, body, _stream):
            captured.append(json.loads(body))
            response = {"id": "resp_image", "status": "completed", "output": [{
                "type": "function_call", "name": router.IMAGEGEN_ALIAS,
                "call_id": "call_image", "arguments": '{"prompt":"blue circle"}',
            }]}
            return router.UpstreamResponse(200, {}, json.dumps(response).encode())

        result = router.route_request(
            self.responses_request(payload), self.responses_manifest(), "local-secret", transport,
        )
        sent = captured[0]
        self.assertEqual([tool["type"] for tool in sent["tools"]], ["tool_search", "function", "function"])
        self.assertEqual(sent["tools"][-1]["name"], router.IMAGEGEN_ALIAS)
        self.assertNotIn("additional_tools", [item.get("type") for item in sent["input"]])
        self.assertEqual(sent["tool_choice"]["tools"][0]["name"], router.IMAGEGEN_ALIAS)
        self.assertNotIn("namespace", sent["tool_choice"]["tools"][0])
        self.assertEqual(json.loads(self.responses_request(payload).body)["input"][0]["tools"][0]["name"], "image_gen")
        call = json.loads(result.body)["output"][0]
        self.assertEqual((call["name"], call["namespace"], call["call_id"]), ("imagegen", "image_gen", "call_image"))
        self.assertEqual(call["arguments"], '{"prompt":"blue circle"}')

        payload["input"] += [
            {"type": "function_call", "name": "imagegen", "namespace": "image_gen", "call_id": "call_image", "arguments": call["arguments"]},
            {"type": "function_call_output", "name": "imagegen", "namespace": "image_gen", "call_id": "call_image", "output": "done"},
        ]
        router.route_request(self.responses_request(payload), self.responses_manifest(), "local-secret", transport)
        self.assertEqual([item["name"] for item in captured[1]["input"][-2:]], [router.IMAGEGEN_ALIAS] * 2)
        self.assertTrue(all("namespace" not in item for item in captured[1]["input"][-2:]))

    def test_responses_imagegen_alias_restored_in_stream_events(self):
        image_tool = {"type": "namespace", "name": "image_gen", "tools": [{
            "type": "function", "name": "imagegen", "parameters": {"type": "object", "properties": {}},
        }]}
        call = {"id": "fc_image", "type": "function_call", "name": router.IMAGEGEN_ALIAS,
                "call_id": "call_image", "arguments": '{"prompt":"blue circle"}'}
        chunks = iter([
            sse_frame({"type": "response.output_item.added", "output_index": 0, "item": call}),
            sse_frame({"type": "response.function_call_arguments.done", "item_id": "fc_image", "output_index": 0, "arguments": call["arguments"]}),
            sse_frame({"type": "response.output_item.done", "output_index": 0, "item": call}),
            sse_frame({"type": "response.completed", "response": {"output": [call]}}),
        ])
        transport = mock.Mock(return_value=router.UpstreamResponse(200, {"content-type": "text/event-stream"}, chunks))
        result = router.route_request(
            self.responses_request({"stream": True, "tools": [image_tool], "input": "draw",
                                    "client_metadata": {"turn_id": "01a0c84e-3036-7e91-96b1-74bb1cd34b04"}}),
            self.responses_manifest(), "local-secret", transport,
        )
        events = response_events(b"".join(result.body))
        calls = [events[0]["item"], events[2]["item"], events[3]["response"]["output"][0]]
        self.assertTrue(all((item["name"], item["namespace"], item["call_id"]) ==
                            ("imagegen", "image_gen", "call_image") for item in calls))
        self.assertEqual(events[1]["arguments"], call["arguments"])

    def test_image_tool_without_turn_id_is_rejected_before_company_request(self):
        transport = mock.Mock()
        response = router.route_request(
            self.responses_request({"tools": [{"type": "function", "name": "image_gen.imagegen",
                                             "parameters": {"type": "object"}}], "input": "draw"}),
            self.responses_manifest(), "company-secret", transport,
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(response.body)["error"]["code"], "image_turn_id_missing")
        transport.assert_not_called()

    def test_chat_bridge_expands_deferred_tools_without_tool_search_error(self):
        body = {"model": "local/deepseek-v4.1-flash", "stream": False,
                "tools": [{"type": "tool_search"}], "input": [
                    {"type": "additional_tools", "tools": [{"type": "namespace", "name": "functions", "tools": [
                        {"type": "function", "name": "ping", "parameters": {"type": "object", "properties": {}}},
                    ]}]},
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                    {"type": "tool_search_call", "id": "search_1"},
                    {"type": "tool_search_output", "id": "search_1", "output": "ping"},
                ]}
        registry = router.ToolRegistry.from_body(body)
        chat = router.build_chat_request(body, MANIFEST["models"][1], registry)
        self.assertEqual(registry.tool_search_count, 1)
        self.assertEqual([tool["function"]["name"] for tool in chat["tools"]], ["ping"])
        self.assertEqual(chat["messages"][-1]["content"], "hi")

    def test_official_route_does_not_alias_imagegen(self):
        body = {"model": "gpt-5.5", "input": "draw", "tools": [
            {"type": "function", "name": "image_gen.imagegen", "parameters": {"type": "object"}},
        ]}
        encoded = json.dumps(body).encode()
        captured = {}

        def transport(_method, url, _headers, payload, _stream):
            captured.update(url=url, payload=payload)
            return router.UpstreamResponse(200, {}, b'{}')

        router.route_request(router.PreparedRequest("POST", "/v1/responses", {}, encoded), MANIFEST, "local-secret", transport)
        self.assertEqual(captured["url"], "https://chatgpt.com/backend-api/codex/responses")
        self.assertEqual(captured["payload"], encoded)

    def test_image_generation_uses_the_company_key_from_its_local_turn(self):
        turn_id = "01a0c84e-3036-7e91-96b1-74bb1cd34b04"
        routes = router.ImageTurnRoutes()
        manifest = self.responses_manifest()
        manifest["version"] = 2
        manifest.pop("upstream_base_url")
        manifest["sources"] = {"company": {
            "kind": "key", "upstream_base_url": "https://company.example/v1", "api_key": "company-secret",
        }, "second": {
            "kind": "key", "upstream_base_url": "https://second.example/v1", "api_key": "second-secret",
        }}
        manifest["models"][0]["source"] = "company"
        manifest["models"].append({**copy.deepcopy(manifest["models"][0]),
                                   "slug": "local/gpt-6-astra", "upstream_model": "gpt-6-astra", "source": "second"})
        calls = []

        def transport(method, url, headers, body, stream):
            calls.append((method, url, headers, json.loads(body), stream))
            return router.UpstreamResponse(200, {"content-type": "application/json"}, b'{}')

        image_tool = {"type": "namespace", "name": "image_gen", "tools": [{
            "type": "function", "name": "imagegen", "parameters": {"type": "object"},
        }]}
        local = self.responses_request({
            "client_metadata": {"turn_id": turn_id}, "tools": [image_tool], "input": "draw",
        }, {"x-codex-turn-metadata": json.dumps({"turn_id": turn_id})})
        image = router.PreparedRequest(
            "POST", "/v1/images/generations",
            {"Authorization": "Bearer personal-secret", "chatgpt-account-id": "personal-account",
             "x-codex-image-turn-id": turn_id},
            json.dumps({"model": "gpt-image-2", "prompt": "blue circle"}).encode(), "rid-image",
        )
        with self.assertLogs(router.log, level="INFO") as records:
            self.assertEqual(router.route_request(local, manifest, None, transport, routes).status, 200)
            self.assertEqual(router.route_request(image, manifest, None, transport, routes).status, 200)
        self.assertEqual(calls[1][1], "https://company.example/v1/images/generations")
        self.assertEqual(calls[1][2]["Authorization"], "Bearer company-secret")
        self.assertNotIn("chatgpt-account-id", calls[1][2])
        self.assertEqual(calls[1][3], {"model": "gpt-image-2", "prompt": "blue circle"})
        self.assertFalse(calls[1][4])
        self.assertIn("local image request rid=rid-image", "\n".join(records.output))
        self.assertNotIn("blue circle", "\n".join(records.output))
        self.assertNotIn("personal-secret", "\n".join(records.output))

        second_turn = "01a0c84e-3036-7e91-96b1-74bb1cd34b06"
        second_body = json.loads(local.body)
        second_body["model"] = "local/gpt-6-astra"
        second_body["client_metadata"]["turn_id"] = second_turn
        second_local = router.PreparedRequest("POST", local.path, {}, json.dumps(second_body).encode())
        second_image = router.PreparedRequest(
            "POST", image.path, {**image.headers, "x-codex-image-turn-id": second_turn}, image.body,
        )
        router.route_request(second_local, manifest, None, transport, routes)
        router.route_request(second_image, manifest, None, transport, routes)
        self.assertEqual(calls[-1][1], "https://second.example/v1/images/generations")
        self.assertEqual(calls[-1][2]["Authorization"], "Bearer second-secret")

        other_turn = router.PreparedRequest(
            "POST", image.path, {**image.headers, "x-codex-image-turn-id": "01a0c84e-3036-7e91-96b1-74bb1cd34b05"}, image.body,
        )
        router.route_request(other_turn, manifest, None, transport, routes)
        self.assertEqual(calls[-1][1], "https://chatgpt.com/backend-api/codex/images/generations")
        self.assertEqual(calls[-1][2]["Authorization"], "Bearer personal-secret")

        official = router.PreparedRequest(
            "POST", "/v1/responses", {"Authorization": "Bearer personal-secret"},
            json.dumps({"model": "gpt-5.5", "client_metadata": {"turn_id": turn_id}, "input": "draw"}).encode(),
        )
        router.route_request(official, manifest, None, transport, routes)
        router.route_request(image, manifest, None, transport, routes)
        self.assertEqual(calls[-1][1], "https://chatgpt.com/backend-api/codex/images/generations")

    def test_responses_passthrough_rewrites_function_call_for_custom_tool(self):
        upstream = json.dumps({
            "id": "resp_1",
            "status": "completed",
            "output": [
                {"id": "ctc_1", "type": "custom_tool_call", "status": "completed", "call_id": "call_1", "name": "exec", "input": "pwd"},
                {"id": "fc_2", "type": "function_call", "status": "completed", "call_id": "call_2", "name": "view_image", "arguments": '{"path":"a.png"}'},
                {"id": "fc_3", "type": "function_call", "status": "completed", "call_id": "call_3", "name": "exec", "arguments": '{"cmd":"echo hi"}'},
            ],
        }).encode()
        transport = mock.Mock(return_value=router.UpstreamResponse(
            200, {"content-type": "application/json"}, upstream,
        ))
        response = router.route_request(
            self.responses_request({
                "stream": False,
                "input": [
                    {"type": "additional_tools", "role": "developer", "tools": [
                        {"type": "namespace", "name": "functions", "description": "client tools", "tools": [
                            {"type": "custom", "name": "exec", "description": "run a command"},
                            {"type": "function", "name": "view_image", "description": "see an image", "parameters": {"type": "object", "properties": {}}},
                        ]},
                    ]},
                ],
            }),
            self.responses_manifest(), "local-secret", transport,
        )
        output = json.loads(response.body)["output"]
        self.assertEqual([item["type"] for item in output], ["custom_tool_call", "function_call", "custom_tool_call"])
        self.assertEqual(output[2]["input"], "echo hi")
        self.assertNotIn("arguments", output[2])
        self.assertEqual(output[1]["arguments"], '{"path":"a.png"}')

    def test_responses_passthrough_rewrites_stream_for_custom_tool(self):
        def frame(event, payload):
            return (f"event: {event}\n" + "data: " + json.dumps(payload, separators=(",", ":")) + "\n\n").encode()

        call_item = {
            "id": "fc_1", "type": "function_call", "status": "in_progress",
            "call_id": "call_1", "name": "exec", "arguments": "",
        }
        chunks = iter([
            frame("response.output_item.added", {"type": "response.output_item.added", "output_index": 1, "item": call_item}),
            frame("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "output_index": 1, "delta": '{"input":"echo'}),
            frame("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "output_index": 1, "delta": ' hi"}'}),
            frame("response.function_call_arguments.done", {"type": "response.function_call_arguments.done", "item_id": "fc_1", "output_index": 1, "arguments": '{"input":"echo hi"}'}),
            frame("response.output_item.done", {"type": "response.output_item.done", "output_index": 1, "item": {**call_item, "status": "completed", "arguments": '{"input":"echo hi"}'}}),
            frame("response.completed", {"type": "response.completed", "response": {"id": "resp_1", "output": [{**call_item, "status": "completed", "arguments": '{"input":"echo hi"}'}]}}),
        ])
        transport = mock.Mock(return_value=router.UpstreamResponse(
            200, {"content-type": "text/event-stream"}, chunks, "OK",
        ))
        response = router.route_request(
            self.responses_request({"stream": True, "input": [
                {"type": "additional_tools", "role": "developer", "tools": [
                    {"type": "namespace", "name": "functions", "tools": [
                        {"type": "custom", "name": "exec", "description": "run"},
                    ]},
                ]},
            ]}),
            self.responses_manifest(), "local-secret", transport,
        )
        parser = router.SSEBuffer()
        events = []
        for chunk in response.body:
            events.extend(parser.feed(chunk))
        events.extend(parser.finish())
        payloads = [json.loads(event["data"]) for event in events]
        self.assertEqual([item["type"] for item in payloads], [
            "response.output_item.added",
            "response.custom_tool_call_input.delta",
            "response.custom_tool_call_input.done",
            "response.output_item.done",
            "response.completed",
        ])
        self.assertEqual(payloads[0]["item"]["type"], "custom_tool_call")
        self.assertEqual(payloads[1]["delta"], "echo hi")
        self.assertEqual(payloads[2]["input"], "echo hi")
        self.assertEqual(payloads[3]["item"]["input"], "echo hi")
        self.assertEqual(payloads[4]["response"]["output"][0]["type"], "custom_tool_call")

    def test_images_manifest_routing_health_and_stateless_guards(self):
        model = MANIFEST["models"][1]
        legacy_payload = json.dumps({
            "version": 1, "kind": "chat", "model": model["slug"],
            "fields": {"reasoning_content": "provider state", "reasoning_details": [{"id": "r1"}]},
        }, separators=(",", ":")).encode()
        carrier = router.LEGACY_REASONING_PREFIX + base64.urlsafe_b64encode(zlib.compress(legacy_payload)).decode()
        chat = router.build_chat_request({
            "model": model["slug"], "reasoning": {"effort": "max"},
            "input": [
                {"type": "reasoning", "encrypted_content": carrier},
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "working"}]},
                {"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": "https://example.invalid/a.png"}]},
            ],
        }, model)
        self.assertEqual(chat["reasoning_effort"], "max")
        self.assertEqual(chat["messages"][1]["reasoning_details"], [{"id": "r1"}])
        self.assertEqual(chat["messages"][-1]["content"][0]["type"], "image_url")
        self.assertNotIn("temperature", chat)

        health = router.health_payload(MANIFEST, None)
        self.assertEqual(health["version"], router.VERSION)
        self.assertEqual((health["transport"], health["bridge"]), ("http-only", router.BRIDGE_NAME))
        self.assertFalse(health["local_key_present"])
        self.assertNotIn(str(Path.home()), repr(health))

        transport = mock.Mock(return_value=router.UpstreamResponse(200, {}, b"{}"))
        unknown = router.route_request(
            router.PreparedRequest("POST", "/v1/responses", {}, b'{"model":"local/not-listed"}'),
            MANIFEST, "key", transport,
        )
        bare_previous = router.route_request(
            router.PreparedRequest("POST", "/v1/responses", {}, b'{"model":"local/kimi-k3","previous_response_id":"resp_old"}'),
            MANIFEST, "key", transport,
        )
        prewarm = router.route_request(
            router.PreparedRequest("POST", "/v1/responses", {}, b'{"model":"local/kimi-k3","generate":false}'),
            MANIFEST, "key", transport,
        )
        self.assertEqual((unknown.status, bare_previous.status, prewarm.status), (400, 400, 400))
        transport.assert_not_called()

    def test_http_server_rejects_websocket_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            router.RouterHandler.manifest = MANIFEST
            router.RouterHandler.runtime_dir = Path(tmp)
            server = router.ThreadingHTTPServer(("127.0.0.1", 0), router.RouterHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                connection.request("GET", "/v1/responses", headers={"Upgrade": "websocket"})
                response = connection.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(response.status, 426)
                self.assertEqual(payload["error"]["code"], "unsupported_transport")
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
