import copy
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import router


def manifest():
    return {
        "version": 2,
        "sources": {
            "official": {"kind": "official"},
            "key-a": {"kind": "key", "upstream_base_url": "https://a.example/v1", "api_key": "gateway-a"},
            "key-b": {"kind": "key", "upstream_base_url": "https://b.example/v1", "api_key": "gateway-b"},
        },
        "models": [
            {"slug": "local/kimi", "source": "key-a", "upstream_model": "kimi-k3", "priority": 10},
            {"slug": "local/deepseek", "source": "key-b", "upstream_model": "deepseek-v4", "priority": 9, "wire_api": "responses"},
        ],
    }


def title_request(*, marker="header", stream=True, session_id="title-session", tool_output=False):
    body = {
        "model": "gpt-5.5", "stream": stream,
        "input": ([{"type": "function_call_output", "call_id": "call_1", "output": "done"}]
                  if tool_output else "Create a short title. User prompt: hello"),
        "client_metadata": {"session_id": session_id},
        "text": {"format": {"type": "json_schema", "name": "title", "schema": {
            "type": "object", "properties": {"title": {"type": "string"}, "description": {"type": "string"}},
            "required": ["title", "description"],
        }}},
    }
    headers = {"Authorization": "Bearer personal-account", "Cookie": "personal-cookie"}
    if marker == "header":
        headers["x-codex-turn-metadata"] = json.dumps({"thread_source": "thread_title", "session_id": session_id})
    elif marker == "body":
        body["client_metadata"]["x-codex-turn-metadata"] = {"thread_source": "thread_title"}
    return router.PreparedRequest("POST", "/v1/responses", headers, json.dumps(body).encode(), "title-rid")


def response_payload(output):
    return {"status": "completed", "output": output}


def title_output(title="网关标题"):
    return [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({
        "title": title, "description": "简短描述",
    }, ensure_ascii=False)}]}]


def gateway_response(output, stream=True):
    payload = response_payload(output)
    if stream:
        frame = router._sse("response.completed", {"type": "response.completed", "response": payload})
        return router.UpstreamResponse(200, {"Content-Type": "text/event-stream"}, iter([frame]))
    return router.UpstreamResponse(200, {"Content-Type": "application/json"}, json.dumps(payload).encode())


class TitleRouteTests(unittest.TestCase):
    def test_deepseek_responses_title_uses_gateway_and_preserves_format(self):
        seen = []

        def transport(method, url, headers, body, stream):
            seen.append((url, headers, json.loads(body), stream))
            return gateway_response(title_output(), stream)

        result = router.route_request(title_request(), manifest(), None, transport)
        self.assertEqual(result.status, 200)
        self.assertEqual(len(seen), 1)
        url, headers, body, stream = seen[0]
        self.assertEqual(url, "https://b.example/v1/responses")
        self.assertEqual(headers["Authorization"], "Bearer gateway-b")
        self.assertNotIn("Cookie", headers)
        self.assertEqual(body["model"], "deepseek-v4")
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertTrue(stream)
        self.assertIn("网关标题", result.body.decode())

    def test_chat_title_uses_json_mode_then_plain_mode_if_unsupported(self):
        data = manifest()
        data["models"] = data["models"][:1]
        seen = []

        def transport(method, url, headers, body, stream):
            chat = json.loads(body)
            seen.append((url, headers, chat))
            if "response_format" in chat:
                return router.UpstreamResponse(400, {}, b"{}")
            return router.UpstreamResponse(200, {}, json.dumps({"choices": [{"message": {
                "content": '{"title":"Kimi 标题","description":"描述"}'}, "finish_reason": "stop"}]}).encode())

        result = router.route_request(title_request(stream=False), data, None, transport)
        self.assertEqual(result.status, 200)
        self.assertEqual([bool(call[2].get("response_format")) for call in seen], [True, False])
        self.assertEqual({call[0] for call in seen}, {"https://a.example/v1/chat/completions"})
        self.assertEqual({call[1]["Authorization"] for call in seen}, {"Bearer gateway-a"})
        self.assertIn("Kimi 标题", result.body.decode())

    def test_chat_title_omits_unbridgeable_codex_tools(self):
        data = manifest()
        data["models"] = data["models"][:1]
        request = title_request(stream=False)
        body = json.loads(request.body)
        body["tools"] = [
            {"type": "web_search"},
            {"type": "function", "name": "lookup", "parameters": {"type": "object", "properties": {}}},
        ]
        request.body = json.dumps(body).encode()
        seen = []

        def transport(method, url, headers, payload, stream):
            seen.append(json.loads(payload))
            return router.UpstreamResponse(200, {}, json.dumps({"choices": [{"message": {
                "content": '{"title":"Kimi 标题","description":"描述"}'}, "finish_reason": "stop"}]}).encode())

        result = router.route_request(request, data, None, transport)
        self.assertEqual(result.status, 200)
        self.assertEqual([tool["function"]["name"] for tool in seen[0]["tools"]], ["lookup"])

    def test_multiple_keys_retry_and_invalid_title(self):
        data = manifest()
        data["models"][0]["wire_api"] = "responses"
        seen = []

        def transport(method, url, headers, body, stream):
            seen.append(url)
            if "a.example" in url:
                return gateway_response(title_output(""), stream)
            return gateway_response(title_output("第二个 Key"), stream)

        result = router.route_request(title_request(), data, None, transport)
        self.assertEqual(result.status, 200)
        self.assertEqual(seen, ["https://a.example/v1/responses", "https://b.example/v1/responses"])
        self.assertIn("第二个 Key", result.body.decode())

    def test_all_gateways_fail_without_official_fallback(self):
        seen = []

        def transport(method, url, headers, body, stream):
            seen.append(url)
            return router.UpstreamResponse(503, {}, b"{}")

        result = router.route_request(title_request(), manifest(), None, transport)
        self.assertEqual(result.status, 400)
        self.assertTrue(seen)
        self.assertTrue(all("chatgpt.com" not in url for url in seen))
        self.assertEqual(json.loads(result.body)["error"]["code"], "title_gateway_unavailable")

    def test_no_enabled_gateway_model_returns_terminal_error(self):
        data = manifest()
        data["models"] = []
        transport = mock.Mock()
        result = router.route_request(title_request(), data, None, transport)
        self.assertEqual(result.status, 400)
        transport.assert_not_called()

    def test_title_deadline_stops_before_next_candidate(self):
        seen = []

        def transport(method, url, headers, body, stream):
            seen.append(url)
            time.sleep(0.01)
            return gateway_response(title_output(), stream)

        with mock.patch.object(router, "TITLE_TIMEOUT_SECONDS", 0.001):
            result = router.route_request(title_request(), manifest(), None, transport)
        self.assertEqual(result.status, 400)
        self.assertEqual(len(seen), 1)

    def test_marker_in_body_and_schema_compatibility(self):
        for marker in ("body", "none"):
            with self.subTest(marker=marker):
                seen = []

                def transport(method, url, headers, body, stream):
                    seen.append(url)
                    return gateway_response(title_output(), stream)

                result = router.route_request(title_request(marker=marker), manifest(), None, transport)
                self.assertEqual(result.status, 200)
                self.assertEqual(seen, ["https://b.example/v1/responses"])

    def test_tool_roundtrip_keeps_selected_key_without_session_id(self):
        data = manifest()
        seen = []

        def transport(method, url, headers, body, stream):
            seen.append(url)
            if len(seen) == 1:
                return gateway_response([{"type": "function_call", "name": "lookup", "call_id": "call_1", "arguments": "{}"}], stream)
            return gateway_response(title_output(), stream)

        first = router.route_request(title_request(session_id=""), data, None, transport)
        second = router.route_request(title_request(session_id="", tool_output=True), data, None, transport)
        self.assertEqual((first.status, second.status), (200, 200))
        self.assertEqual(seen, ["https://b.example/v1/responses"] * 2)

    def test_ordinary_official_request_preserves_personal_route(self):
        request = title_request(marker="none")
        body = json.loads(request.body)
        body.pop("text")
        body["input"] = "ordinary question"
        request.body = json.dumps(body).encode()
        seen = []

        def transport(method, url, headers, body, stream):
            seen.append((url, headers))
            return router.UpstreamResponse(200, {}, b"{}")

        result = router.route_request(request, manifest(), None, transport)
        self.assertEqual(result.status, 200)
        self.assertEqual(seen[0][0], router.OFFICIAL_BASE_URL + "/responses")
        self.assertEqual(seen[0][1]["Authorization"], "Bearer personal-account")

    def test_ordinary_gateway_request_keeps_selected_model(self):
        request = title_request(marker="none", stream=False)
        body = json.loads(request.body)
        body.pop("text")
        body["model"] = "local/deepseek"
        body["input"] = "ordinary question"
        request.body = json.dumps(body).encode()
        seen = []

        def transport(method, url, headers, payload, stream):
            seen.append((url, headers, json.loads(payload)))
            return gateway_response(title_output(), stream)

        result = router.route_request(request, manifest(), None, transport)
        self.assertEqual(result.status, 200)
        self.assertEqual(seen[0][0], "https://b.example/v1/responses")
        self.assertEqual(seen[0][2]["model"], "deepseek-v4")


if __name__ == "__main__":
    unittest.main()
