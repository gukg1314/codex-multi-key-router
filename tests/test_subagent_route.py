import base64
import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import router


PARENT_ID = "01a0cc16-b5fe-7ad3-a70b-9b94d2e80b14"
CHILD_ID = "01a0cc1a-e4d7-7842-9a49-299cba855e50"
TASK_TEXT = "Generate the base image and report the result."
REPORT_TEXT = "The base image is ready."
LUNA = "gpt-5.6-luna"


def model(slug, upstream_model, source, wire="chat"):
    record = {
        "slug": slug, "upstream_model": upstream_model,
        "source": source, "priority": 10,
    }
    if wire == "responses":
        record["wire_api"] = "responses"
    return record


def manifest(wire="chat", include_parent_luna=True):
    result = {
        "version": 2,
        "sources": {
            "official": {"kind": "official"},
            "key-a": {
                "kind": "key", "upstream_base_url": "https://key-a.example/v1", "api_key": "key-a-secret",
            },
            "key-b": {
                "kind": "key", "upstream_base_url": "https://key-b.example/v1", "api_key": "key-b-secret",
            },
        },
        # The globally selected Luna alias belongs to Key B. Key A's Luna is
        # available only as a source-specific agent candidate.
        "models": [
            model("local/deepseek", "deepseek-v4.1-flash", "key-a", wire),
            model("local/luna-key-b", LUNA, "key-b", wire),
        ],
        "agent_models": [
            model("local/luna-key-a", "GPT-5.6 Luna", "key-a", wire),
        ],
        "official_models": ["gpt-5.6-sol", LUNA],
    }
    if not include_parent_luna:
        result["agent_models"] = []
    return result


def metadata(thread_id, source):
    return json.dumps({
        "session_id": thread_id, "thread_id": thread_id,
        "thread_source": source,
        "model": "gpt-5.6-luna" if source == "subagent" else "local/deepseek",
    })


def request(
    model_name, thread_id, source, input_items, parent_id="", *,
    stream=False, gzip_body=False, extra=None, extra_headers=None,
):
    headers = {
        "Authorization": "Bearer personal-account", "Cookie": "personal-cookie",
        "x-codex-turn-metadata": metadata(thread_id, source),
    }
    if parent_id:
        headers["x-codex-parent-thread-id"] = parent_id
    if extra_headers:
        headers.update(extra_headers)
    body = {
        "model": model_name, "stream": stream, "input": input_items,
        "client_metadata": {
            "session_id": thread_id, "thread_id": thread_id,
            "x-codex-turn-metadata": metadata(thread_id, source),
        },
    }
    if extra:
        body.update(extra)
    encoded = json.dumps(body).encode()
    if gzip_body:
        headers["Content-Encoding"] = "gzip"
        encoded = gzip.compress(encoded)
    return router.PreparedRequest("POST", "/v1/responses", headers, encoded, "subagent-rid")


def user_message(text="hello"):
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def agent_message(text=TASK_TEXT, *, author="/root", recipient="/root/base_worker"):
    return {
        "type": "agent_message", "author": author, "recipient": recipient,
        "content": [{"type": "input_text", "text": text}],
    }


def capture_transport(calls, response=None):
    response = response or router.UpstreamResponse(200, {}, json.dumps({
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
    }).encode())

    def transport(method, url, headers, payload, stream):
        calls.append((url, headers, json.loads(payload), stream))
        return response

    return transport


def synthetic_jwt(subject, account, version):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    claims = {
        "sub": subject,
        "iat": version,
        "https://api.openai.com/auth": {"chatgpt_account_id": account},
    }
    return encode({"alg": "HS256", "typ": "JWT"}) + "." + encode(claims) + ".signature-" + str(version)


class SubagentRouteTests(unittest.TestCase):
    def test_key_parent_routes_explicit_other_key_alias_to_its_hidden_candidate(self):
        routes = router.ModelRoutes()
        calls = []
        transport = capture_transport(calls)

        parent = router.route_request(
            request("local/deepseek", PARENT_ID, "user", [user_message()]),
            manifest(), None, transport, thread_routes=routes,
        )
        child = router.route_request(
            request("local/luna-key-b", CHILD_ID, "subagent", [agent_message()], PARENT_ID),
            manifest(), None, transport, thread_routes=routes,
        )

        self.assertEqual((parent.status, child.status), (200, 200))
        self.assertEqual(calls[1][0], "https://key-a.example/v1/chat/completions")
        self.assertEqual(calls[1][1]["Authorization"], "Bearer key-a-secret")
        self.assertNotIn("Cookie", calls[1][1])
        self.assertEqual(calls[1][2]["model"], "GPT-5.6 Luna")
        task = next(message for message in calls[1][2]["messages"] if message["role"] == "user")
        self.assertEqual(task["content"], TASK_TEXT)

        exact_config = manifest()
        exact_config["agent_models"].append(model("local/luna-key-a-exact", LUNA, "key-a"))
        exact = router.route_request(
            request("local/luna-key-b", CHILD_ID, "subagent", [agent_message()], PARENT_ID),
            exact_config, None, transport, thread_routes=routes,
        )
        self.assertEqual(exact.status, 200)
        self.assertEqual(calls[2][2]["model"], LUNA)

    def test_official_parent_maps_explicit_local_gpt_child_to_official_model(self):
        routes = router.ModelRoutes()
        calls = []
        transport = capture_transport(calls, router.UpstreamResponse(200, {}, b"{}"))
        parent_token = synthetic_jwt("user-1", "account-1", 1)
        refreshed_token = synthetic_jwt("user-1", "account-1", 2)

        parent = router.route_request(
            request(
                "gpt-5.6-sol", PARENT_ID, "user", [user_message()],
                extra_headers={"Authorization": "Bearer " + parent_token},
            ),
            manifest(), None, transport, thread_routes=routes,
        )
        child = router.route_request(
            request(
                "local/luna-key-b", CHILD_ID, "subagent", [user_message(TASK_TEXT)], PARENT_ID,
                extra_headers={"Authorization": "Bearer " + refreshed_token},
            ),
            manifest(), None, transport, thread_routes=routes,
        )

        self.assertEqual((parent.status, child.status), (200, 200))
        self.assertEqual(calls[1][0], router.OFFICIAL_BASE_URL + "/responses")
        self.assertEqual(calls[1][1]["Authorization"], "Bearer " + refreshed_token)
        self.assertEqual(calls[1][2]["model"], LUNA)
        self.assertEqual(calls[1][2]["input"][0]["content"][0]["text"], TASK_TEXT)

        changed_account = synthetic_jwt("user-1", "account-2", 3)
        rejected = router.route_request(
            request(
                "local/luna-key-b", CHILD_ID, "subagent", [user_message(TASK_TEXT)], PARENT_ID,
                extra_headers={"Authorization": "Bearer " + changed_account},
            ),
            manifest(), None, transport, thread_routes=routes,
        )
        self.assertEqual(rejected.status, 400)
        self.assertEqual(len(calls), 2)

    def test_official_parent_reads_refreshed_catalog_for_hidden_local_alias(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "models_cache.json"
            cache_path.write_text(json.dumps({"models": [{"slug": LUNA}]}), encoding="utf-8")
            config = manifest()
            config["models"][1]["upstream_model"] = "GPT-5.6 Luna"
            config["official_models"] = ["gpt-5.6-sol"]
            config["official_cache_path"] = str(cache_path)

            routes = router.ModelRoutes()
            calls = []
            transport = capture_transport(calls, router.UpstreamResponse(200, {}, b"{}"))
            router.route_request(
                request("gpt-5.6-sol", PARENT_ID, "user", [user_message()]),
                config, None, transport, thread_routes=routes,
            )

            first = router.route_request(
                request("local/luna-key-b", CHILD_ID, "subagent", [user_message(TASK_TEXT)], PARENT_ID),
                config, None, transport, thread_routes=routes,
            )
            self.assertEqual(first.status, 200)
            self.assertEqual(calls[1][2]["model"], LUNA)

            cache_path.write_text(json.dumps({"models": [{"slug": "GPT-5.6-LUNA"}]}), encoding="utf-8")
            second = router.route_request(
                request("local/luna-key-b", CHILD_ID, "subagent", [user_message(TASK_TEXT)], PARENT_ID),
                config, None, transport, thread_routes=routes,
            )
            self.assertEqual(second.status, 200)
            self.assertEqual(calls[2][2]["model"], "GPT-5.6-LUNA")

    def test_title_schema_subagent_stays_on_parent_key(self):
        config = manifest()
        config["sources"] = {
            "official": config["sources"]["official"],
            "key-b": config["sources"]["key-b"],
            "key-a": config["sources"]["key-a"],
        }
        routes = router.ModelRoutes()
        calls = []
        transport = capture_transport(calls)
        router.TITLE_SESSION_ROUTES.forget(CHILD_ID)
        router.route_request(
            request("local/deepseek", PARENT_ID, "user", [user_message()]),
            config, None, transport, thread_routes=routes,
        )
        title_schema = {
            "format": {
                "type": "json_schema", "name": "title",
                "schema": {
                    "type": "object", "properties": {"title": {"type": "string"}, "description": {"type": "string"}},
                    "required": ["title", "description"],
                },
            },
        }
        child = router.route_request(
            request(
                "local/luna-key-b", CHILD_ID, "subagent", "Create a title. User prompt: hello.", PARENT_ID,
                extra={"text": title_schema},
            ),
            config, None, transport, thread_routes=routes,
        )
        router.TITLE_SESSION_ROUTES.forget(CHILD_ID)

        self.assertEqual(child.status, 200)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0], "https://key-a.example/v1/chat/completions")
        self.assertEqual(calls[1][1]["Authorization"], "Bearer key-a-secret")
        self.assertEqual(calls[1][2]["model"], "GPT-5.6 Luna")

    def test_subagent_images_stay_on_parent_key_and_fail_closed(self):
        payload = b'{ "model" : "gpt-image-2", "prompt": "blue circle" }'
        image_turn = "01a0cc1f-3b7d-7456-bd0f-111111111111"
        other_turn = "01a0cc1f-3b7d-7456-bd0f-222222222222"

        def image_request(turn_id=None):
            headers = {
                "Authorization": "Bearer personal-account", "Cookie": "personal-cookie",
                "x-codex-parent-thread-id": PARENT_ID,
                "x-codex-turn-metadata": metadata(CHILD_ID, "subagent"),
            }
            if turn_id:
                headers["x-codex-image-turn-id"] = turn_id
            return router.PreparedRequest("POST", "/v1/images/generations", headers, payload, "image-rid")

        def image_transport(calls):
            def transport(method, url, headers, body, stream):
                calls.append((url, headers, json.loads(body), body, stream))
                if url.endswith("/chat/completions"):
                    response = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
                    return router.UpstreamResponse(200, {}, json.dumps(response).encode())
                return router.UpstreamResponse(200, {}, b"{}")
            return transport

        for label in ("same source", "different Key", "missing turn"):
            with self.subTest(label=label):
                config = manifest()
                routes = router.ModelRoutes()
                image_routes = router.ImageTurnRoutes()
                calls = []
                transport = image_transport(calls)
                router.route_request(
                    request("local/deepseek", PARENT_ID, "user", [user_message()]),
                    config, None, transport, thread_routes=routes,
                )
                if label != "missing turn":
                    image_routes.remember(image_turn, "local/deepseek" if label == "same source" else "local/luna-key-b")
                result = router.route_request(
                    image_request(None if label == "missing turn" else image_turn),
                    config, None, transport, image_routes, routes,
                )
                if label == "same source":
                    self.assertEqual(result.status, 200)
                    self.assertEqual(calls[1][0], "https://key-a.example/v1/images/generations")
                    self.assertEqual(calls[1][1]["Authorization"], "Bearer key-a-secret")
                    self.assertEqual(calls[1][2], {"model": "gpt-image-2", "prompt": "blue circle"})
                else:
                    self.assertEqual(result.status, 400)
                    expected_code = "subagent_image_source_changed" if label == "different Key" else "subagent_image_source_unbound"
                    self.assertEqual(json.loads(result.body)["error"]["code"], expected_code)
                    self.assertEqual(len(calls), 1)

        routes = router.ModelRoutes()
        calls = []
        transport = image_transport(calls)
        router.route_request(
            request("gpt-5.6-sol", PARENT_ID, "user", [user_message()]),
            manifest(), None, transport, thread_routes=routes,
        )
        official_image = image_request(other_turn)
        result = router.route_request(official_image, manifest(), None, transport, router.ImageTurnRoutes(), routes)
        self.assertEqual(result.status, 200)
        self.assertEqual(calls[1][0], router.OFFICIAL_BASE_URL + "/images/generations")
        self.assertEqual(calls[1][3], payload)

    def test_missing_parent_binding_or_parent_candidate_never_falls_back(self):
        child = request("local/luna-key-b", CHILD_ID, "subagent", [agent_message()], PARENT_ID)
        cases = (
            ("empty route table", manifest(), router.ModelRoutes(), child),
            ("no parent id", manifest(), router.ModelRoutes(), request(
                "local/luna-key-b", CHILD_ID, "subagent", [agent_message()],
            )),
        )
        for label, config, routes, child_request in cases:
            with self.subTest(label=label):
                calls = []
                result = router.route_request(child_request, config, None, capture_transport(calls), thread_routes=routes)
                self.assertIn(result.status, (400, 409, 503))
                self.assertEqual(calls, [])

        # A live parent binding cannot switch to Key B when Key A loses the
        # matching model during manifest refresh.
        routes = router.ModelRoutes()
        calls = []
        transport = capture_transport(calls)
        router.route_request(
            request("local/deepseek", PARENT_ID, "user", [user_message()]),
            manifest(), None, transport, thread_routes=routes,
        )
        changed = manifest(include_parent_luna=False)
        result = router.route_request(child, changed, None, transport, thread_routes=routes)
        self.assertIn(result.status, (400, 409, 503))
        self.assertEqual(len(calls), 1)

        changed_source = manifest()
        changed_source["models"][0]["source"] = "key-b"
        result = router.route_request(child, changed_source, None, transport, thread_routes=routes)
        self.assertIn(result.status, (400, 409, 503))
        self.assertEqual(len(calls), 1)

        changed_key = manifest()
        changed_key["sources"]["key-a"]["api_key"] = "rotated-key-a"
        result = router.route_request(child, changed_key, None, transport, thread_routes=routes)
        self.assertIn(result.status, (400, 409, 503))
        self.assertEqual(len(calls), 1)

        # A process restart loses the in-memory binding and must fail closed.
        restarted_routes = router.ModelRoutes()
        result = router.route_request(child, manifest(), None, transport, thread_routes=restarted_routes)
        self.assertIn(result.status, (400, 409, 503))
        self.assertEqual(len(calls), 1)

    def test_chat_context_keeps_task_tool_result_and_report_for_parent_and_child(self):
        routes = router.ModelRoutes()
        calls = []
        transport = capture_transport(calls)
        config = manifest()

        router.route_request(
            request("local/deepseek", PARENT_ID, "user", [user_message("Summarize the assignment.")]),
            config, None, transport, thread_routes=routes,
        )
        task = agent_message()
        router.route_request(
            request("local/luna-key-b", CHILD_ID, "subagent", [task], PARENT_ID),
            config, None, transport, thread_routes=routes,
        )
        tool_context = [
            task,
            {"type": "function_call", "call_id": "call_read", "name": "read_task", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_read", "output": "The image task is complete."},
            agent_message(REPORT_TEXT, author="/root/base_worker", recipient="/root"),
        ]
        router.route_request(
            request(
                "local/luna-key-b", CHILD_ID, "subagent", tool_context, PARENT_ID,
                extra={"tools": [{
                    "type": "function", "name": "read_task", "description": "Read-only task lookup.",
                    "parameters": {"type": "object", "properties": {}},
                }]},
            ),
            config, None, transport, thread_routes=routes,
        )
        parent_report = router.route_request(
            request(
                "local/deepseek", PARENT_ID, "user",
                [user_message("Summarize the assignment."), agent_message(
                    REPORT_TEXT, author="/root/base_worker", recipient="/root",
                )],
            ),
            config, None, transport, thread_routes=routes,
        )

        self.assertEqual(parent_report.status, 200)
        child_messages = calls[2][2]["messages"]
        task_index = next(i for i, item in enumerate(child_messages) if item == {"role": "user", "content": TASK_TEXT})
        call_index = next(i for i, item in enumerate(child_messages) if item.get("tool_calls"))
        tool_index = next(i for i, item in enumerate(child_messages) if item.get("tool_call_id") == "call_read")
        report_index = next(i for i, item in enumerate(child_messages) if item == {"role": "user", "content": REPORT_TEXT})
        self.assertEqual(child_messages[call_index]["role"], "assistant")
        self.assertEqual(child_messages[call_index]["tool_calls"][0]["function"]["name"], "read_task")
        self.assertEqual(child_messages[tool_index], {"role": "tool", "tool_call_id": "call_read", "content": "The image task is complete."})
        self.assertEqual([task_index, call_index, tool_index, report_index], sorted([task_index, call_index, tool_index, report_index]))
        parent_messages = calls[3][2]["messages"]
        self.assertEqual(parent_messages[-1], {"role": "user", "content": REPORT_TEXT})

    def test_responses_keeps_encrypted_agent_and_reasoning_through_gzip_stream(self):
        routes = router.ModelRoutes()
        calls = []
        sse = [b"event: response.completed\ndata: {\"type\":\"response.completed\"}\n\n"]
        transport = capture_transport(calls, router.UpstreamResponse(200, {"content-type": "text/event-stream"}, sse))
        config = manifest("responses")
        router.route_request(
            request("local/deepseek", PARENT_ID, "user", [user_message()]),
            config, None, transport, thread_routes=routes,
        )

        plaintext = agent_message("Plain task details")
        encrypted = {
            "type": "agent_message", "author": "/root", "recipient": "/root/base_worker",
            "content": [{"type": "encrypted_content", "encrypted_content": "opaque-agent-ciphertext"}],
        }
        reasoning = {"type": "reasoning", "encrypted_content": "opaque-reasoning-ciphertext", "summary": []}
        child = router.route_request(
            request(
                "local/luna-key-b", CHILD_ID, "subagent", [plaintext, encrypted, reasoning], PARENT_ID,
                stream=True, gzip_body=True,
                extra_headers={"OpenAI-Beta": "responses=v1,custom_beta=v2"},
            ),
            config, None, transport, thread_routes=routes,
        )

        self.assertEqual(child.status, 200)
        self.assertEqual(calls[1][0], "https://key-a.example/v1/responses")
        self.assertEqual(calls[1][1]["Authorization"], "Bearer key-a-secret")
        self.assertEqual(calls[1][1]["OpenAI-Beta"], "responses=v1,custom_beta=v2,responses_multi_agent=v1")
        self.assertTrue(calls[1][3])
        sent_input = calls[1][2]["input"]
        self.assertEqual(sent_input[0], {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "Plain task details"}],
        })
        self.assertEqual(sent_input[1], encrypted)
        self.assertEqual(sent_input[2], reasoning)
        self.assertEqual(list(child.body), sse)

    def test_chat_rejects_encrypted_agent_message_instead_of_forwarding_carrier(self):
        routes = router.ModelRoutes()
        calls = []
        transport = capture_transport(calls)
        config = manifest()
        router.route_request(
            request("local/deepseek", PARENT_ID, "user", [user_message()]),
            config, None, transport, thread_routes=routes,
        )
        cipher = router.REASONING_PREFIX + "private-encrypted-payload"
        result = router.route_request(
            request("local/luna-key-b", CHILD_ID, "subagent", [{
                "type": "agent_message", "author": "/root", "recipient": "/root/base_worker",
                "content": [{"type": "encrypted_content", "encrypted_content": cipher}],
            }], PARENT_ID),
            config, None, transport, thread_routes=routes,
        )

        self.assertEqual(result.status, 400)
        error = json.loads(result.body)["error"]
        self.assertRegex((error["code"] + error["message"]).lower(), "encrypted|cipher|密文|加密")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
